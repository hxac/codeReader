# ROM 与 textio 文件初始化

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 ROM（只读存储器）与 RAM 的本质区别，以及本库为什么用 VHDL 的 `constant` 来实现「只读」。
- 读懂 `rom.vhd` 如何用 `impure function load_rom` 在 **elaboration（精化）期**读取一个外部 hex 文件，把内容「烧」进一个常量 ROM。
- 理解 VHDL `std.textio` 文件 I/O 三件套：`file_open` / `readline` / `hread` / `endfile`。
- 解释为什么常量 ROM 在运行时**不可改写**，以及 `SIMULATION_MODE` + `force` 如何在仿真里临时覆盖 ROM 内容。
- 自己动手准备一个 hex 文件、加载它、按地址读回校验。

## 2. 前置知识

本讲建立在 u6-l1（单口 RAM）和 u3-l1（`memories_pkg` 与非约束数组类型）之上。如果你还没读，至少需要知道：

- **ROM vs RAM**：RAM 可读可写，ROM 一旦初始化后内容固定、只能按地址读。在第 6 单元里，RAM 的存储体是一个可写的数组；本讲的 ROM 存储体是一个 **`constant`（常量）**——这正是「只读」在源码层面的体现。
- **非约束数组类型 `rom_t`**：在 [memories_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/memories_pkg.vhd#L13-L15) 中定义为 `type rom_t is array (natural range <>) of std_ulogic_vector;`，深度和位宽两个维度都留空，推迟到使用时再约束（回顾 u3-l1 的「双维度约束」）。
- **elaboration（精化）期**：VHDL 从「源码」到「可仿真/可综合的电路」要经过 **编译 → 精化（elaboration）→ 仿真** 三步。精化期会创建设计层次、分配信号、并**一次性求值所有 `constant`**。这一步发生在仿真时间 0 之前。本讲的核心就是「在精化期读文件」。
- **`subtype` 与 `'subtype` 属性**：`rom_reg'subtype` 表示常量 `rom_reg` 最终被约束成的那个具体子类型，可让别的对象「照抄」它的尺寸，而不必重新写一遍边界。

> 术语提示：`std.textio` 是 VHDL 标准库自带的文本文件读写包（`use std.textio.all`），与本库无关、属于语言本身。`hread`（hex read，读十六进制）是 VHDL-2008 起加入 `std.textio` 的，所以本项目依赖 VHDL-2008（回顾 u1-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `ip/memories/rom/rom.vhd` | ROM 设计源码：含 `load_rom` 文件加载函数、常量 ROM、SIMULATION_MODE 双路径 | ✅ 主角 |
| `ip/memories/memories_pkg.vhd` | 定义共享类型 `rom_t`（u3-l1 已讲，本讲复用） | ✅ 简要回顾 |
| `ip/memories/rom/tb/tb_rom.vhd` | ROM 的 VUnit 测试台：用 `force` 注入随机数据并读回校验 | ✅ 精读 force 用法 |

注意：`rom` 与第 6 单元的 RAM 不同——它**只有一套 `behavioural` 架构**，没有 Xilinx/Intel/自研三套实现（回顾 u2-l1 的「同一实体多架构」）。ROM 本就是厂商无关的行为级描述，综合时由工具自动推断成片上 BRAM/ROM 资源。

## 4. 核心概念与源码讲解

### 4.1 ROM 模块整体与端口契约

#### 4.1.1 概念说明

ROM（Read-Only Memory，只读存储器）的存储内容在「出厂」时就固定下来，之后只能按地址读出，不能改写。在 FPGA 里，ROM 通常用片上 BRAM（块 RAM）配置成「只读」、或者用 LUT 资源实现，初始化数据在综合时写入比特流。

本库的 `rom` 模块用一个**单进程、单架构**的简洁设计来实现 ROM，核心思想是：**把存储体声明为 `constant`（常量）**。常量在精化期求值后就「冻结」，代码里没有任何地方对它赋值，因此天然只读——不需要额外的写保护逻辑。

它与 RAM 的对比：

| 特性 | RAM（u6） | ROM（本讲） |
| --- | --- | --- |
| 存储体 | 可写的数组（signal / variable） | `constant` 常量 |
| 写操作 | 有 | 无 |
| 内容来源 | 运行时写入 | 精化期从文件加载 |
| 典型推断 | BRAM（双口/单口） | BRAM（只读）或 LUT-ROM |

#### 4.1.2 核心流程

ROM 的运行时行为非常简单（注意输出 `q` 寄存了一拍）：

```
每个 sys_clk 上升沿：
  若 sys_rst_n = '0'：q <= 0
  否则：              q <= rom_reg(to_integer(address))
```

也就是说：**先给地址 → 下一个上升沿 → `q` 给出该地址的内容**，读出数据比地址晚一个时钟周期。这和单口 RAM 的「读出寄存一拍」一致（回顾 u6-l1）。

#### 4.1.3 源码精读

先看 entity 的端口与 generic（[rom.vhd:L15-L27](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L15-L27)）：

```vhdl
generic (
    DATA_WIDTH: positive := 8;
    MEM_INIT_FILE_PATH: string := "";        -- 初始化文件路径，默认空
    SIMULATION_MODE: boolean := false        -- 是否走「可 force」的仿真路径
);
port (
    sys_clk: in std_ulogic;
    sys_rst_n: in std_ulogic;
    address: in unsigned;                    -- 非约束！位宽由例化决定
    q: out std_ulogic_vector(DATA_WIDTH - 1 downto 0)
);
```

注意 `address: in unsigned` 是**非约束端口**（回顾 u6-l1）：它在 entity 里不写范围，真实位宽推迟到例化时由外部连线决定。存储深度 `ROM_DEPTH` 在 architecture 内部反推（[rom.vhd:L30-L31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L30-L31)）：

```vhdl
constant ROM_DEPTH: natural := 2**address'length;   -- 深度 = 2^(地址位数)
subtype rom_depth_t is natural range 0 to ROM_DEPTH - 1;
```

即深度满足

\[
\text{ROM\_DEPTH} = 2^{\text{address'length}}
\]

例如 16 位地址 → `ROM_DEPTH = 65536`（这也是测试台注释里「2^16 iterations」的由来）。`rom_depth_t` 把自然数限定到 `0 .. ROM_DEPTH-1`，后面用它当数组下标。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：体会「地址位宽 → ROM 深度」的自动推导。
2. **步骤**：打开 `tb_rom.vhd`，找到常量声明（[tb_rom.vhd:L53-L54](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L53-L54)），`ADDRESS_WIDTH := 16`、`DATA_WIDTH := 8`。
3. **观察**：据此算出例化后 `ROM_DEPTH` 应为多少。
4. **预期结果**：`ROM_DEPTH = 2^16 = 65536`，每个元素 8 位。
5. 待本地验证：把 `ADDRESS_WIDTH` 改成 12，重新跑 `tb_rom`，确认仿真仍通过（深度变 4096，遍历更快）。

#### 4.1.5 小练习与答案

**Q1**：如果把 `address` 例化成 8 位，`ROM_DEPTH` 是多少？  
**A1**：`2^8 = 256`。

**Q2**：为什么 `rom` 模块不需要写保护逻辑？  
**A2**：因为存储体是 `constant`，源码里没有任何对它的赋值语句，「只读」由语言本身保证。

---

### 4.2 textio 文件 I/O：file / readline / hread / endfile

#### 4.2.1 概念说明

要让 ROM「开机即有内容」，最灵活的办法是从一个外部文本文件读取数据。VHDL 的 `std.textio` 包提供了基础的文本文件读写能力，核心要素有四个：

| 元素 | 含义 |
| --- | --- |
| `file` 类型对象 | 代表一个打开的文件句柄；`file instructions_file: text;` |
| `text` 类型 | 即「文本文件」类型，`file of string` 的别名 |
| `line` 类型 | 指向一行字符串的访问类型（缓冲区） |
| `readline(f, L)` | 从文件 `f` 读一行到 `line` 变量 `L` |
| `hread(L, v)` | 从 `line` 中读一个**十六进制**值到 `std_ulogic_vector v`（VHDL-2008） |
| `endfile(f)` | 文件是否已读到末尾 |
| `file_open(f, path, read_mode)` | 以读模式打开路径 `path` |
| `file_close(f)` | 关闭文件 |

> 小知识：`std.textio` 还有 `read`（读二进制/整数/字符串）、`bread`（二进制）、`oread`（八进制）。本库只用了 `hread`，每行一个十六进制数。

#### 4.2.2 核心流程

把一个文件「逐行读成数组」的通用模式是：

```
file_open(file, path, read_mode)
while not endfile(file) loop
    readline(file, row)      -- 取一行到 row
    hread(row, value)        -- 把该行解析为 hex，存进 value
    -- 处理 value（本讲：写进 ROM 对应地址）
end loop
file_close(file)
```

这是一个典型的「逐行扫描」循环，依赖 `endfile` 作为终止条件。

#### 4.2.3 源码精读

这段文件读取逻辑出现在 `load_rom` 函数里（[rom.vhd:L44-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L44-L53)）：

```vhdl
file_open(instructions_file, path, read_mode);
while not endfile(instructions_file) loop
    readline(instructions_file, row);
    hread(row, rom_reg(address));     -- 把一行 hex 写进当前地址
    address := address + 1;
end loop;
file_close(instructions_file);
```

中文说明：
- 第 1 行用传入的 `path` 以读模式打开文件。
- 循环里 `readline` 取一行，`hread` 把这一行的十六进制文本（如 `A5`）解析成一个 `std_ulogic_vector`，直接存入 `rom_reg(address)` 这个数组元素。
- `address` 从 0 开始递增，于是文件第 1 行 → ROM 地址 0、第 2 行 → 地址 1……

关键约定：**文件每一行就是一个地址的数据**。对于 `DATA_WIDTH = 8`，每行是 2 个十六进制字符（如 `A5`、`3F`、`FF`、`00`）。

源码注释（[rom.vhd:L45-L46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L45-L46)）特意说明：**故意不检查文件行数是否超过 `ROM_DEPTH`**。如果文件太大，`address`（类型 `rom_depth_t`，范围 `0..ROM_DEPTH-1`）会越界，仿真器在越界赋值时报错——这本身就是对用户的「文件太大」警告。反之，如果文件行数**少于** `ROM_DEPTH`，剩下地址保持初值 `0`（见 4.3.3 的全零初始化）。

#### 4.2.4 代码实践（示例代码）

下面是一段**示例代码**（非项目原有文件），让你单独体会 `hread` 的解析规则：

```vhdl
-- 示例代码：演示 hread 如何把文本行解析成向量
use std.textio.all;
use ieee.std_logic_1164.all;
...
process
    file f: text;
    variable L: line;
    variable v: std_ulogic_vector(7 downto 0);
begin
    file_open(f, "demo.hex", read_mode);   -- demo.hex 内含三行：A5 / 3F / 00
    for i in 0 to 2 loop
        readline(f, L);
        hread(L, v);                       -- 第1次 v=10100101，第2次 v=00111111，第3次 v=00000000
        report "v = " & to_hstring(v);     -- 打印 A5 / 3F / 00
    end loop;
    file_close(f);
    wait;
end process;
```

1. **目标**：理解 `hread` 对大小写、位数的处理。
2. **步骤**：在上述示例里把 `demo.hex` 的某行写成 `a5`（小写）、`3`（一位）、`FF extra`（带尾随文本），分别观察 `v` 的值。
3. **观察**：`hread` 跳过前导空白；位数不足时左补零；读取到刚好填满目标宽度即停止。
4. **预期结果**：`a5` → `A5`；`3` → `03`；`FF extra` 只取 `FF`。
5. 待本地验证：不同仿真器（NVC / ModelSim）对 `hread` 边界情况的处理可能略有差异。

#### 4.2.5 小练习与答案

**Q1**：`readline` 和 `hread` 各自的职责是什么？为什么需要两步？  
**A1**：`readline` 把文件的一整行取到 `line` 缓冲区；`hread` 再从这个缓冲区里解析出十六进制数值。分两步是因为「取行」和「解析数值」是两种独立操作，一行里还可以连续调用多次 `hread` 读多个值。

**Q2**：如果 `DATA_WIDTH = 16`，每行需要几个十六进制字符？  
**A2**：4 个（16 位 = 4 个 hex 位），如 `ABCD`。

---

### 4.3 impure function load_rom：在精化期把文件「烧」进常量

#### 4.3.1 概念说明

把文件读取逻辑写进函数 `load_rom`，再让它去初始化一个常量，是本讲最巧妙的设计。这里有两个关键概念：

**（1）`impure`（非纯）函数。** VHDL 的函数默认是 **pure（纯）**：同样的入参必须返回同样的结果，且不能有副作用（不能读写文件、不能读写外部信号）。`load_rom` 要**读文件**，这是副作用，因此必须标 `impure`。两者的区别：

| | pure（默认） | impure |
| --- | --- | --- |
| 同输入是否同输出 | 必须是 | 不保证（可依赖外部状态） |
| 文件/信号 I/O | 禁止 | 允许 |
| 调用次数与结果 | 与调用次数无关 | 可能每次不同 |

**（2）常量在精化期求值。** 当 architecture 里写

```vhdl
constant rom_reg: rom_t := load_rom(path => MEM_INIT_FILE_PATH);
```

`load_rom` 会在**精化期被调用一次**（仿真时间 0 之前），读文件、填数组，把结果「冻结」进常量 `rom_reg`。之后整个仿真期间 `rom_reg` 永远是这份内容——这就模拟了一块「出厂时烧好」的 ROM。代价是：**常量一旦冻结就不能在运行时改写**（见 4.4）。

#### 4.3.2 核心流程

```
精化期（仿真时间 0 之前）：
  1. 求值 constant rom_reg := load_rom(MEM_INIT_FILE_PATH)
  2. load_rom 执行：
       a. 路径为空？→ 警告，返回全零数组
       b. 否则打开文件，逐行 hread 填充，返回数组
  3. rom_reg 被冻结为这份内容
仿真期：
  4. 每个上升沿按 address 读 rom_reg，不再调用 load_rom
```

注意步骤 4：**运行时只是查表，文件 I/O 只在精化期发生一次**。

#### 4.3.3 源码精读

`load_rom` 的完整声明与实现（[rom.vhd:L33-L55](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L33-L55)）：

```vhdl
impure function load_rom(path: string) return rom_t is
    file instructions_file: text;
    variable address: rom_depth_t;
    variable rom_reg: rom_t(rom_depth_t)(q'range) := (others => (others => '0'));  -- 全零初值
    variable row: line;
begin
    if path'length = 0 then
        assert false report "No memory initialisation file provided!" severity warning;
        return rom_reg;                       -- 路径空 → 返回全零
    end if;
    file_open(instructions_file, path, read_mode);
    while not endfile(instructions_file) loop
        readline(instructions_file, row);
        hread(row, rom_reg(address));
        address := address + 1;
    end loop;
    file_close(instructions_file);
    return rom_reg;
end function;
```

中文要点：
- `impure` 关键字必不可少（读文件 = 副作用）。
- 局部 `variable rom_reg` 用「双维度约束」`rom_t(rom_depth_t)(q'range)`：第一维 `(rom_depth_t)` 限定深度为 `0..ROM_DEPTH-1`，第二维 `(q'range)` 限定每个元素位宽等于端口 `q` 的宽度（即 `DATA_WIDTH`）。初值 `(others => (others => '0'))` 把整个数组清零——所以文件没覆盖到的地址都是 0。
- `q'range` 能在函数里用，是因为 `q` 是 entity 的端口、对 architecture 声明区可见（函数就声明在声明区里）。
- 路径为空时只发一条 `warning` 并返回全零数组，**不报错**——这正是默认测试台传 `MEM_INIT_FILE_PATH => ""` 时不会崩的原因。

随后，函数结果被冻结进常量（[rom.vhd:L57](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L57)）：

```vhdl
constant rom_reg: rom_t := load_rom(path => MEM_INIT_FILE_PATH);
```

注意：整个设计里 `load_rom` **只在第 57 行被调用一次**——这印证了「文件 I/O 只发生在精化期」。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：确认 `load_rom` 只在精化期调用一次，运行时不再读文件。
2. **步骤**：在 `load_rom` 的 `file_open` 之后临时加一行 `report "load_rom called, path=" & path severity note;`（**仅本地实验，勿提交**），然后跑 `tb_rom`。
3. **观察**：这条报告在仿真开始前（时间 0 之前）只打印一次，之后整个仿真不再出现。
4. **预期结果**：「load_rom called」只出现 1 次。
5. 待本地验证。

#### 4.3.5 小练习与答案

**Q1**：把 `impure` 关键字删掉会怎样？  
**A1**：编译报错——pure 函数不允许做文件 I/O，必须用 `impure` 声明副作用。

**Q2**：为什么 `load_rom` 里的局部变量能写成 `rom_t(rom_depth_t)(q'range)`？  
**A2**：因为 `rom_t` 是双维度非约束数组（u3-l1），这里同时给出深度（`rom_depth_t`）和元素位宽（`q'range`）两个约束，得到一个完全确定的数组类型。

**Q3**：如果 `MEM_INIT_FILE_PATH` 传空字符串，`load_rom` 返回什么？  
**A3**：返回一个全零数组，并打印一条 warning（见 [rom.vhd:L39-L42](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L39-L42)）。

---

### 4.4 MEM_INIT_FILE_PATH / SIMULATION_MODE：双路径与 force 覆盖

#### 4.4.1 概念说明

`rom` 有两个控制行为的 generic：`MEM_INIT_FILE_PATH`（文件路径）和 `SIMULATION_MODE`（布尔）。后者的存在回答了一个矛盾：

- **常量 ROM 不可运行时改写**：`rom_reg` 是 `constant`，精化期冻结后，测试台**无法**用 `force` 改它。如果测试想用随机数据覆盖整块 ROM，常量这条路走不通。
- **但仿真需要灵活注入数据**：比如 `tb_rom` 想用随机数据填满 65536 个地址再逐个读回校验，又不想真去生成一个 65536 行的 hex 文件。

解决办法是**双存储路径**：除了常量 `rom_reg`，再准备一个**信号** `rom_reg_only_for_simulation`。信号可以被测试台 `force`。用 `SIMULATION_MODE` 在二者间切换：

| `SIMULATION_MODE` | 读进程使用的数据源 | 可否运行时改写 |
| --- | --- | --- |
| `false`（默认，真实/综合用） | 常量 `rom_reg`（从文件加载） | 否（只读） |
| `true`（仿真用） | 信号 `rom_reg_only_for_simulation` | 可（测试台 `force` 覆盖） |

#### 4.4.2 核心流程

设计侧切换（[rom.vhd:L70-L74](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L70-L74)）：

```vhdl
if not SIMULATION_MODE then
    q <= rom_reg(to_integer(address));                    -- 常量路径
else
    q <= rom_reg_only_for_simulation(to_integer(address)); -- 信号路径（可 force）
end if;
```

测试侧覆盖（`tb_rom` 里）：

```
1. 用 hierarchical 别名把 DUT 内部的 rom_reg_only_for_simulation 引出来
2. 用 <= force 注入一组随机数据
3. 逐个地址写入、读回，用 check_equal 校验
```

为什么用 `if/else` 而不是 `if generate`？源码注释（[rom.vhd:L69](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L69)）解释：`if generate` 只能写在进程**外面**，而这里不想为了分支把读 ROM 的进程整段复制两份，所以用进程内的普通 `if/else`。

#### 4.4.3 源码精读

仿真专用信号的声明（[rom.vhd:L57-L59](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd#L57-L59)）：

```vhdl
constant rom_reg: rom_t := load_rom(path => MEM_INIT_FILE_PATH);
-- Only for simulation purpose as it's overriddable via force
signal rom_reg_only_for_simulation: rom_reg'subtype := (others => (others => '0'));
```

注意 `rom_reg'subtype`：它「照抄」常量 `rom_reg` 最终的约束（深度 + 位宽），所以这个信号和常量尺寸完全一致，无需重写边界。该信号在设计代码里**只被读、从不被赋值**——它的唯一驱动者来自测试台的 `force`。

测试台如何触达这个内部信号？用 VHDL-2008 的层次化信号别名（[tb_rom.vhd:L99](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L99)）：

```vhdl
alias rom_reg_only_for_simulation is
    << signal .tb_rom.DuT.rom_reg_only_for_simulation : rom_t(0 to 2**ADDRESS_WIDTH - 1)(q'range) >>
;
```

`<< signal .tb_rom.DuT.rom_reg_only_for_simulation ... >>` 是跨层次引用：从测试台顶层 `tb_rom` 一路钻进 `DuT` 实例的内部信号。有了别名后，就可以用 `force` 一次性覆盖整块 ROM（[tb_rom.vhd:L115-L135](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L115-L135)）：

```vhdl
procedure test_full_rom is
    variable rom_reg_random: rom_t(0 to 2**ADDRESS_WIDTH - 1)(q'range);
begin
    ...
    for i in rom_reg_random'range loop
        rom_reg_random(i) := random.RandSlv(rom_reg_random(i)'length);   -- 生成随机内容
    end loop;
    rom_reg_only_for_simulation <= force rom_reg_random;                  -- ★ force 覆盖整块 ROM
    for i in rom_reg_random'range loop
        address <= to_unsigned(i, address'length);
        wait_sys_clk_cycles(1);
        check_equal(q, rom_reg_random(i), "q");                           -- 逐地址读回校验
    end loop;
end procedure;
```

`<= force` 是 VHDL-2008 的强制赋值：它让测试台「夺过」该信号的驱动权，把随机内容压进 ROM，于是读回的 `q` 应等于写入的随机值。DUT 例化时显式开启仿真模式（[tb_rom.vhd:L154-L165](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/tb/tb_rom.vhd#L154-L165)）：`SIMULATION_MODE => true`、`MEM_INIT_FILE_PATH => ""`。

#### 4.4.4 代码实践（完整动手实践）★★★

这是本讲的主实践：**让 ROM 真正从文件加载，并按地址读回校验**。注意现有 `tb_rom` 走的是 `force` 随机数据这条路（`SIMULATION_MODE => true`、空路径），**没有**测试文件加载。本实践要新建一条「文件加载」路径。

1. **目标**：准备一个 hex 文件，用 `MEM_INIT_FILE_PATH` 加载，读回校验；并理解常量不可改写、`SIMULATION_MODE` 提供 force 的原因。
2. **操作步骤**：
   1. 在 `ip/memories/rom/tb/` 下新建一个文本文件 `my_rom_init.hex`，写入 4 行（对应地址 0~3，每行 2 个 hex 字符）：
      ```
      A5
      3C
      0F
      FF
      ```
   2. 复制 `tb_rom.vhd` 为 `tb_rom_file.vhd`（**仅本地实验**），把 DUT 例化改成走文件加载路径：
      ```vhdl
      DuT: entity work.rom
          generic map (
              DATA_WIDTH        => DATA_WIDTH,
              MEM_INIT_FILE_PATH => tb_path & "my_rom_init.hex",  -- 用 VUnit 给的 tb_path 拼相对路径
              SIMULATION_MODE  => false                           -- ★ 走常量路径，读文件
          )
          port map (...);
      ```
   3. 写一个最简用例（替代 `test_full_rom`）：依次给 `address = 0,1,2,3`，每个地址后 `wait_sys_clk_cycles(1)`，用 `check_equal(q, x"A5")` 等校验读回值。
   4. 用 `test_runner.py` 跑这个新测试台。
3. **需要观察的现象**：精化期会打印一条「load_rom called」之类的报告（如果你按 4.3.4 加了）；仿真期 `q` 在地址 0→`A5`、1→`3C`、2→`0F`、3→`FF`，全部 `check_equal` 通过。
4. **预期结果**：4 个地址读回值与文件内容一一对应。
5. 待本地验证（文件路径拼接、仿真器对相对路径的处理因环境而异）。

**讨论题（本实践第二部分）**：

- **为什么常量 ROM 不能在运行时改写？** 因为 `rom_reg` 是 `constant`，精化期冻结后，VHDL 语义禁止任何对它的赋值，测试台的 `force` 也对常量无效——这正是「只读」的语义保证，也与真实 ROM/BRAM「比特流烧录后固定」的物理特性一致。
- **为什么 `SIMULATION_MODE` + 信号能提供覆盖能力？** 因为 `rom_reg_only_for_simulation` 是**信号**（非 `constant`），且设计代码里没有它的驱动者，于是测试台可以用 `<= force` 抢占驱动权注入任意内容，从而在不生成大文件的前提下灵活测试。代价是这条路径**仅用于仿真**（综合时该信号无人驱动、内容为全零），所以真实综合必须用 `SIMULATION_MODE => false` 的常量路径。

#### 4.4.5 小练习与答案

**Q1**：如果例化时 `SIMULATION_MODE => false` 但 `MEM_INIT_FILE_PATH => ""`，ROM 读出的内容是什么？  
**A1**：全零。`load_rom` 检测到空路径后返回全零数组（`warning` 提示），常量 `rom_reg` 冻结为全零。

**Q2**：为什么 `rom_reg_only_for_simulation` 用 `rom_reg'subtype` 而不是重新写 `rom_t(0 to ROM_DEPTH-1)(q'range)`？  
**A2**：为了「单一真相」——直接继承常量已约束好的子类型，避免重复书写边界、也避免两边尺寸不一致的风险。

**Q3**：`<< signal .tb_rom.DuT.rom_reg_only_for_simulation ... >>` 这种写法依赖哪个 VHDL 标准？  
**A3**：VHDL-2008 的层次化信号引用（external name / hierarchical reference），本库基于 VHDL-2008（回顾 u1-l1）。

## 5. 综合实践

把本讲四个模块串起来，完成一个「自制带文件初始化的小 ROM 查表器」：

1. **设计一个查表场景**：用 ROM 存一张 16 项 × 8 位的表，内容是你自定义的 16 个值（例如某函数的查表结果）。
2. **生成 hex 文件**：写一个 16 行的 `.hex` 文件，每行一个 8 位十六进制值。
3. **例化 `rom`**：`DATA_WIDTH => 8`、`address` 约束成 4 位（深度 16）、`MEM_INIT_FILE_PATH => tb_path & "你的文件.hex"`、`SIMULATION_MODE => false`。
4. **写一个遍历用例**：地址从 0 扫到 15，用 `check_equal` 把读回值与你手写的期望值逐一比对。
5. **扩展思考**：
   - 如果把 `address` 改成 5 位（深度 32）但文件只有 16 行，地址 16~31 读回应该是什么？（答：`00`，因为 `load_rom` 的局部数组初值是全零，文件只覆盖了前 16 项。）
   - 如果想在仿真里临时把地址 0 的内容改成 `00`，该用哪条路径？（答：`SIMULATION_MODE => true` + `force` 覆盖 `rom_reg_only_for_simulation`。）

完成后再回头读一遍 [rom.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/memories/rom/rom.vhd)，确认你能向别人讲清「文件 → `load_rom` → 常量 → 读进程」这条数据流，以及「常量 vs 信号」两条路径的取舍。

## 6. 本讲小结

- ROM 的「只读」在源码层面由 **`constant` 存储体**保证——精化期冻结后不可改写，与 RAM 的可写数组形成对比。
- `rom` 是厂商无关的单 `behavioural` 架构，深度 `ROM_DEPTH = 2^address'length` 由非约束地址端口反推。
- 文件加载靠 `std.textio` 四件套：`file_open` → `while not endfile` → `readline` → `hread`（每行一个十六进制值）；文件行数少于深度时剩余地址为 0，多于深度则地址越界报错。
- `load_rom` 必须是 **`impure` 函数**（文件 I/O 是副作用），它**只在精化期被调用一次**，把文件内容「烧」进常量 `rom_reg`。
- `MEM_INIT_FILE_PATH` 决定加载哪个文件；`SIMULATION_MODE` 在「常量路径（真实/综合，只读）」与「信号路径（仿真，可 `force` 覆盖）」间切换，解决了「常量不可改 vs 仿真要灵活注入」的矛盾。
- 测试台用 VHDL-2008 层次化别名 `<< signal ... >>` 钻进 DUT 内部，用 `<= force` 注入随机数据做全覆盖校验。

## 7. 下一步学习建议

- **第 8 单元（时钟域跨域同步器）**：ROM 是单时钟域模块；接下来学习多比特信号如何安全地跨时钟域传递，重点是 `ff_synchroniser_vector`——它是后续异步 FIFO 跨域同步指针的基石。
- **第 9 单元（FIFO 设计）**：ROM 给出了「常量存储」的范式；FIFO 则是「可变存储 + 指针管理 + 跨域同步」的综合应用，会把本讲和 u6 的 RAM 知识全部串起来。
- **延伸阅读**：在你常用的综合工具手册里查「如何从常量数组推断初始化的 BRAM/ROM」（Vivado / Quartus 的推断规则），体会 `load_rom` 在精化期求值的常量是如何映射到真实硬件的初始比特的——这部分行为依赖工具，建议本地验证。
