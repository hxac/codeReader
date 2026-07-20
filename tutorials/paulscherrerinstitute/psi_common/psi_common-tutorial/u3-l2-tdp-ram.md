# 真双口 RAM：tdp_ram 与 tdp_ram_be

## 1. 本讲目标

本讲在 u3-l1（`sdp_ram` / `sp_ram_be`）的基础上，继续讲解 psi_common 存储层的另外两个组件：

- `psi_common_tdp_ram`：真双口 RAM（True Dual-Port RAM）
- `psi_common_tdp_ram_be`：带字节使能的真双口 RAM

学完本讲后，读者应该能够：

1. 说清楚「真双口」与「简单双口」的本质区别——两个端口是否都具备独立的读/写能力。
2. 看懂 `tdp_ram` 两个对称端口（A/B）的时钟、地址、读/写数据信号如何组织。
3. 理解 `tdp_ram_be` 的字节使能（byte enable）写入逻辑，并能预测一次部分字节写入后的读回值。
4. 知道 `tdp_ram` 在综合属性上与 `sdp_ram` 的差异（**没有** `ram_style_g`），以及跨时钟使用时必须添加的时序约束。
5. 准确说出 `tdp_ram` 在库内的真实使用者是 `ping_pong`（乒乓缓冲），并纠正「异步 FIFO 也用它」的常见误解。

## 2. 前置知识

本讲默认读者已经掌握 u3-l1 的内容，尤其是以下几点：

- **简单双口 RAM（sdp_ram）**：一个端口只写、一个端口只读；通过 `is_async_g` 让读端口用独立时钟，从而支持跨时钟域。
- **`shared variable` 建模存储**：变量赋值 `:=` 立即生效，因此可以在同一个进程内靠「语句先后顺序」实现「读前写（RBW）」或「写前读（WBR）」。
- **`behavior_g`（RBW/WBR）**：用于匹配不同 FPGA 存储资源（BRAM 多为 RBW、LUT-RAM 常为 WBR）的原生语义。
- **地址位宽自动推导**：地址端口写成 `std_logic_vector(log2ceil(depth_g) - 1 downto 0)`，宽度随 `depth_g` 自动变化。
- **`ram_style` 综合属性**：`sdp_ram` 用它指定资源类型（`auto`/`distributed`/`block`）。

如果对上述任何一点不熟悉，建议先回到 u3-l1 复习。本讲只读两个 `.vhd` 源码文件、它们的文档与一个测试平台，不涉及上层 FIFO 的内部实现（那是 u4 的内容）。

## 3. 本讲源码地图

| 文件 | 作用 | 是否有专属 TB |
|:-----|:-----|:-------------|
| [hdl/psi_common_tdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd) | 真双口 RAM，A/B 两个对称读/写端口，各自独立时钟 | 无（文档标注 N.A） |
| [hdl/psi_common_tdp_ram_be.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd) | 在 `tdp_ram` 基础上增加字节使能 | 有：`psi_common_tdp_ram_be_tb` |
| [hdl/psi_common_sdp_ram.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd) | 简单双口 RAM（对照用，u3-l1 已讲） | 无 |
| [hdl/psi_common_ping_pong.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd) | 乒乓缓冲，是 `tdp_ram` 在库内的真实使用者 | 有 |
| [testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd) | 字节使能真双口 RAM 的自校验 TB，用 180 MHz / 25 MHz 两个异步时钟 | — |
| [doc/files/psi_common_tdp_ram.md](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_tdp_ram.md) | 组件说明与跨时钟约束示例 | — |

> 提示：`tdp_ram` 本身没有专属测试平台，但 `tdp_ram_be` 有。由于两者代码结构几乎一致（只差字节使能一段），后者同时也是理解前者的最佳「可运行样例」。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**双端口端口模型**、**字节使能**、**综合属性**、**在缓冲中的角色**。

### 4.1 双端口端口模型

#### 4.1.1 概念说明

「真双口（True Dual-Port）」与「简单双口（Simple Dual-Port）」的区别不在于端口数量（都是两个），而在于**每个端口的能力**：

| 类型 | 端口 A | 端口 B | 典型场景 |
|:-----|:-------|:-------|:---------|
| 简单双口（sdp_ram） | 只写 | 只读 | FIFO 式的「一端写入、另一端读出」 |
| 真双口（tdp_ram） | 可读可写 | 可读可写 | 两个独立主机共享同一块存储 |

换句话说，`tdp_ram` 的两个端口是**完全对称**的：每个端口都自带时钟、地址、写使能、写数据、读数据。任意一个端口都可以在任意时刻选择读或写。两个端口的时钟可以是**完全异步**的两个时钟（频率、相位都无关）。

这种结构恰好对应 FPGA 里「真双口 BRAM」原语（primitive）的天然形态——两个独立的读写端口共享同一存储体，因此综合工具能很干净地把它推断为一块 Block-RAM。

#### 4.1.2 核心流程

`tdp_ram` 内部只有一块共享存储 `mem` 和两个几乎相同的进程（端口 A、端口 B）。每个端口的单拍行为如下：

```
每个端口 P（P ∈ {A, B}），在 P_clk_i 的上升沿：
  若 behavior_g = "RBW"：
      先把 mem[P_addr] 读到 P_dat_o          # 读先发生
  若 P_wr = '1'：
      把 P_dat_i 写入 mem[P_addr]             # 写后发生（变量赋值立即生效）
  若 behavior_g = "WBR"：
      再把 mem[P_addr] 读到 P_dat_o          # 读在写之后，读到的是新值
```

要点：

- **读写同地址时**，`behavior_g` 决定读回的是旧值（RBW）还是新值（WBR）。
- 两个端口共享同一个 `shared variable mem`，因此端口 A 写入的数据，端口 B 在之后的读操作中能直接看到（TB 正是利用这一点做跨端口回读校验）。
- 两个端口的进程分别挂在各自的时钟上，互不干扰——这就是「两个完全异步时钟」得以支持的物理基础。

#### 4.1.3 源码精读

实体声明清晰展示了两个对称端口（[hdl/psi_common_tdp_ram.vhd:19-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L19-L33)）：

```vhdl
entity psi_common_tdp_ram is
  generic(depth_g    : positive := 1024;
          width_g    : positive := 32;
          behavior_g : string   := "RBW");
  port(   a_clk_i  : in  std_logic                                        := '0';
          a_addr_i : in  std_logic_vector(log2ceil(depth_g) - 1 downto 0) := (others => '0');
          a_wr_i   : in  std_logic                                        := '0';
          a_dat_i  : in  std_logic_vector(width_g - 1 downto 0)           := (others => '0');
          a_dat_o  : out std_logic_vector(width_g - 1 downto 0);
          b_clk_i  : in  std_logic                                        := '0';
          b_addr_i : in  std_logic_vector(log2ceil(depth_g) - 1 downto 0) := (others => '0');
          b_wr_i   : in  std_logic                                        := '0';
          b_dat_i  : in  std_logic_vector(width_g - 1 downto 0)           := (others => '0');
          b_dat_o  : out std_logic_vector(width_g - 1 downto 0));
end entity;
```

注意端口 A（`a_*`）和端口 B（`b_*`）的字段完全对称，每个端口都有 `clk/addr/wr/dat_i/dat_o` 五件套。地址宽度同样由 `log2ceil(depth_g)` 自动推导。

存储体用 `shared variable` 建模，两个进程共享它（[hdl/psi_common_tdp_ram.vhd:36-42](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L36-L42)）：

```vhdl
type mem_t is array (depth_g - 1 downto 0) of std_logic_vector(width_g - 1 downto 0);
shared variable mem : mem_t := (others => (others => '0'));
```

端口 A 进程实现 RBW/WBR 的读改写顺序（[hdl/psi_common_tdp_ram.vhd:47-60](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L47-L60)）：

```vhdl
porta_p : process(a_clk_i)
begin
  if rising_edge(a_clk_i) then
    if behavior_g = "RBW" then
      a_dat_o <= mem(to_integer(unsigned(a_addr_i)));   -- 读先：拿到旧值
    end if;
    if a_wr_i = '1' then
      mem(to_integer(unsigned(a_addr_i))) := a_dat_i;   -- 写：变量立即生效
    end if;
    if behavior_g = "WBR" then
      a_dat_o <= mem(to_integer(unsigned(a_addr_i)));   -- 读后：拿到新值
    end if;
  end if;
end process;
```

端口 B 进程结构与端口 A **完全相同**，只是信号名换成 `b_*`、挂在 `b_clk_i` 上（[hdl/psi_common_tdp_ram.vhd:63-76](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L63-L76)）。开头还有一行参数校验断言（[hdl/psi_common_tdp_ram.vhd:44](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L44)），要求 `behavior_g` 只能是 `"RBW"` 或 `"WBR"`。

#### 4.1.4 代码实践：比较 sdp_ram 与 tdp_ram 的端口

这是本讲指定的实践任务。

1. **实践目标**：通过对照实体端口，说清楚简单双口与真双口的差异，并解释跨时钟场景下何时应选 `tdp_ram`。
2. **操作步骤**：
   - 打开 [hdl/psi_common_sdp_ram.vhd:18-32](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L18-L32)（`sdp_ram` 实体）。
   - 打开 [hdl/psi_common_tdp_ram.vhd:19-33](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L19-L33)（`tdp_ram` 实体）。
   - 列一张端口对照表：`sdp_ram` 的写端口有 `wr_dat_i` 但**没有** `dat_o`，读端口有 `rd_dat_o` 但**没有** `dat_i`；`tdp_ram` 的每个端口则两者都有。
3. **需要观察的现象**：`sdp_ram` 是「有方向」的（一端只写、一端只读），而 `tdp_ram` 两个端口完全对称、都能读能写。
4. **预期结果 / 结论**：
   - 两者**都支持完全异步的两个时钟**（`sdp_ram` 靠 `is_async_g` 拆成写/读两个进程；`tdp_ram` 靠两个对称进程）。
   - 因此「跨时钟」本身并不是选 `tdp_ram` 的理由。真正应选 `tdp_ram` 的场景是：**两个时钟域都需要写（且读）同一块存储**。`sdp_ram` 强制单方向，无法满足「双方都可写」。
   - 次要理由：`tdp_ram` 的双对称端口与 FPGA「真双口 BRAM」原语一一对应，综合推断更干净。
5. **待本地验证**：若你手上有开发板，可分别综合两个实体，查看工具报告里推断出的 BRAM 端口数。

#### 4.1.5 小练习与答案

**练习 1**：如果应用只需要「A 写、B 读」且 A、B 时钟不同，应该选 `sdp_ram` 还是 `tdp_ram`？为什么？

> **答案**：选 `sdp_ram`（设 `is_async_g => true`）即可。它的简单双口结构刚好匹配单向数据流，资源开销与接口都更轻。`tdp_ram` 在这里不会带来额外好处。

**练习 2**：`tdp_ram` 端口 A 和端口 B 的进程是否可以共用一个时钟？会出现什么效果？

> **答案**：可以。把 `a_clk_i` 与 `b_clk_i` 接同一个时钟即可，此时它退化成一个单时钟真双口 RAM，两个端口在同一节拍下分别独立读/写同一存储体。

**练习 3**：端口 A 写了地址 5、端口 B 在「下一个 B 时钟沿」读地址 5，B 一定能读到 A 写的值吗？

> **答案**：不一定。前提是 A 的写动作已经发生在 B 的这个读沿「之前」并被存储体采纳。由于两个时钟完全异步，设计者必须保证写动作与读动作之间的时序关系（这正是下一节「综合属性/约束」要解决的问题）。

### 4.2 字节使能

#### 4.2.1 概念说明

很多总线协议（AXI、Nios、本地寄存器接口等）支持**部分字写入**：一次只改写一个 32 位字里的若干字节，其余字节保持原值。硬件上用「字节使能（byte enable）」信号表达——每 8 位数据对应 1 位使能，使能为 1 的字节才被写入。

`psi_common_tdp_ram_be` 就是在 `tdp_ram` 上加了字节使能：每个端口多了一个 `be_i` 信号，宽度为 `width_g/8`。文档同时给出一条**诚实的告诫**：它在 32 位下工作良好，但其他位宽曾观察到问题，推荐对照厂商（如 Xilinx）模板使用（见 [doc/files/psi_common_tdp_ram_be.md:13-14](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_tdp_ram_be.md#L13-L14)）。

#### 4.2.2 核心流程

字节使能写入只是在原来的「整字写入」外面套了一层按字节判断的循环：

```
每个端口 P，在 P_clk_i 上升沿、且 P_wr = '1' 时：
  for byte in 0 to (width_g/8 - 1) loop
      if P_be(byte) = '1' then
          mem[P_addr] 的第 byte 个字节  :=  P_dat_i 的第 byte 个字节
      -- 使能为 0 的字节保持原值不动
      end if;
  end loop;
```

字节编号与数据比特的对应关系是：

\[ \text{byte } b \;\leftrightarrow\; \text{bits } [8b+7 \,:\, 8b] \]

即 `be_i(0)` 控制最低字节 `bits[7:0]`，`be_i(1)` 控制 `bits[15:8]`，依此类推。

#### 4.2.3 源码精读

实体新增了两个字节使能端口 `a_be_i` / `b_be_i`，宽度为 `width_g / 8`，默认全 `'1'`（即默认整字写入，与无字节使能版本行为一致，见 [hdl/psi_common_tdp_ram_be.vhd:25](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L25) 与 [hdl/psi_common_tdp_ram_be.vhd:31](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L31)）：

```vhdl
a_be_i : in std_logic_vector(width_g / 8 - 1 downto 0) := (others => '1');  -- port a byte enable
```

架构体里先算出字节数常量，并对参数做两条断言（[hdl/psi_common_tdp_ram_be.vhd:41](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L41) 与 [hdl/psi_common_tdp_ram_be.vhd:49-50](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L49-L50)）：

```vhdl
constant BeCount_c : integer := width_g / 8;
...
assert behavior_g = "RBW" or behavior_g = "WBR" report "..." severity error;
assert width_g mod 8 = 0 report "width_g must be a multiple of 8, otherwise byte-enables do not make sense" severity error;
```

第二条断言很重要：位宽必须是 8 的整数倍，否则字节使能没有意义。端口 A 的写入循环如下（[hdl/psi_common_tdp_ram_be.vhd:59-65](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L59-L65)）：

```vhdl
if a_wr_i = '1' then
  for byte in 0 to BeCount_c - 1 loop
    if a_be_i(byte) = '1' then
      mem(to_integer(unsigned(a_addr_i)))(byte*8 + 7 downto byte*8) := a_dat_i(byte*8 + 7 downto byte*8);
    end if;
  end loop;
end if;
```

端口 B 的对应循环在 [hdl/psi_common_tdp_ram_be.vhd:79-85](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram_be.vhd#L79-L85)。

#### 4.2.4 代码实践：读 TB 预测字节使能读回值

`tdp_ram` 没有专属 TB，但 `tdp_ram_be` 有一个写得很好的自校验 TB，正好用来理解字节使能。

1. **实践目标**：根据字节使能位，预测写入后每个地址的读回值，并与 TB 里的断言对照。
2. **操作步骤**：
   - 打开 [testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd:133-151](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd#L133-L151)。这一段从端口 A 连续写 4 个地址，`width_g = 32`。
   - 逐行记录每次写的 `a_be_i` 与 `a_dat_i`：

     | 地址 | `a_be_i` | `a_dat_i` | 实际写入的字节 |
     |:-----|:---------|:----------|:---------------|
     | 0x000 | `1111` | `0x11111111` | 全部 4 字节 |
     | 0x001 | `1111` | `0x22222222` | 全部 4 字节 |
     | 0x002 | `0011` | `0x33333333` | 仅 byte0、byte1（低 16 位） |
     | 0x003 | `0100` | `0x44444444` | 仅 byte2（`bits[23:16]`） |

3. **需要观察的现象**：对于只写了部分字节、其余字节保持初值 `0x00` 的地址，预测读回值。
4. **预期结果**（与 TB 第 157–166 行的 `StdlvCompareStdlv` 断言一致）：

   | 地址 | 预测读回值 | TB 期望值 |
   |:-----|:-----------|:----------|
   | 0x000 | `0x11111111` | `0x11111111` |
   | 0x001 | `0x22222222` | `0x22222222` |
   | 0x002 | `0x00003333` | `0x00003333` |
   | 0x003 | `0x00440000` | `0x00440000` |

5. **运行验证**（可选）：该 TB 已在 `sim/config.tcl` 注册。若已按 u1-l3 搭好工作副本结构，可在 `sim/` 下跑 `run.tcl`，观察这 4 条 `StdlvCompareStdlv` 是否全部通过、终端无 `###ERROR###`。若不便运行，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：`width_g = 32` 时，要让一次写入只更新最高字节（`bits[31:24]`），`be_i` 应取何值？

> **答案**：`be_i = "1000"`（byte3 = 1，其余为 0）。因为 byte 索引从 0 起，byte3 对应 `bits[31:24]`。

**练习 2**：为什么 `tdp_ram_be` 默认把 `a_be_i` 设成 `(others => '1')` 而不是 `'0'`？

> **答案**：默认全 1 表示「整字写入」，这样不接字节使能信号时，组件行为退化成普通的整字 `tdp_ram`，不会出现「什么都写不进去」的意外。

### 4.3 综合属性

#### 4.3.1 概念说明

读者从 u3-l1 已经知道，`sdp_ram` 用一个 `ram_style` 综合属性（`ram_style_g`）告诉工具：这块存储用 `block`（BRAM）、`distributed`（LUT-RAM）还是 `auto`（工具自选）。那么 `tdp_ram` 有没有同样的属性？

**结论：没有。** `tdp_ram` 和 `tdp_ram_be` 都**不暴露** `ram_style_g`，存储资源类型完全交给综合工具的默认推断。这是它与 `sdp_ram` 在综合层面最显著的区别之一。

不过这并不意味着 `tdp_ram` 在综合侧「无可调」——它仍有两条与综合相关的重要手段：

1. **`behavior_g`（RBW/WBR）**：选择读/写先后顺序，用来匹配目标 FPGA 存储原语的原生语义（BRAM 多为 RBW、LUT-RAM 常为 WBR）。这是 `tdp_ram` 唯一的「实现风格」开关。
2. **跨时钟时序约束**：两个端口用异步时钟时，跨域路径必须由用户在约束文件里手动限制（见下文）。

#### 4.3.2 核心流程

由于没有 `ram_style` 属性，`tdp_ram` 的存储声明比 `sdp_ram` 简单——只有一个 `shared variable`，没有任何 `attribute`：

```
type mem_t is array (depth_g-1 downto 0) of std_logic_vector(width_g-1 downto 0);
shared variable mem : mem_t := (others => (others => '0'));
-- 没有 attribute ram_style ...
```

综合时，工具根据 `behavior_g`、端口的读/写模式自行决定推断成 BRAM 还是 LUT-RAM。当两个端口时钟不同时，跨时钟域的地址/数据路径必须由设计者加约束：

\[ T_{\text{max-delay}} \;\le\; T_{\text{较快时钟周期}} \]

即跨域路径的最大延迟不能超过较快时钟的一个周期。

#### 4.3.3 源码精读

`tdp_ram` 的存储声明里确实没有任何 `attribute`（[hdl/psi_common_tdp_ram.vhd:38-40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L38-L40)）：

```vhdl
-- memory array
type mem_t is array (depth_g - 1 downto 0) of std_logic_vector(width_g - 1 downto 0);
shared variable mem : mem_t := (others => (others => '0'));
```

对照 `sdp_ram` 的存储声明，那里多了两行 `ram_style` 属性（[hdl/psi_common_sdp_ram.vhd:37-40](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L37-L40)）：

```vhdl
type mem_t is array (depth_g - 1 downto 0) of std_logic_vector(width_g - 1 downto 0);
shared variable mem : mem_t := (others => (others => '0'));
attribute ram_style : string;
attribute ram_style of mem : variable is ram_style_g;   -- sdp_ram 有，tdp_ram 没有
```

跨时钟约束则在文档里以 Vivado 的 `set_max_delay` 为例给出（[doc/files/psi_common_tdp_ram.md:50-62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_tdp_ram.md#L50-L62)）。以 100 MHz（10 ns）与 33.33 MHz（30 ns）为例：

```tcl
set_max_delay --datapath_only --from <ClkA> -to <ClkB> 10.0
set_max_delay --datapath_only --from <ClkB> -to <ClkA> 10.0
```

两个方向的约束都取较快时钟的周期（10 ns）作为上限。文档同时强调：这些约束针对的是「从一个时钟域到另一个时钟域」的路径——也就是说，虽然 `mem` 是共享存储，但综合后真正需要约束的是 BRAM 内部跨端口的走线延迟。

#### 4.3.4 代码实践：对比 ram_style 与 behavior_g 的作用

1. **实践目标**：分清 `ram_style`（资源类型）与 `behavior_g`（读改写顺序）这两件不同的事。
2. **操作步骤**：
   - 在 [hdl/psi_common_sdp_ram.vhd:18-23](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_sdp_ram.vhd#L18-L23) 中找到 `ram_style_g` 与 `ram_behavior_g` 两个 generic。
   - 在 [hdl/psi_common_tdp_ram.vhd:20-22](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_tdp_ram.vhd#L20-L22) 中确认：`tdp_ram` 只有 `behavior_g`，**没有** `ram_style_g`。
   - 思考：如果你强烈需要把 `tdp_ram` 强制推断为 block RAM，但代码里没有 `ram_style`，还有什么办法？
3. **需要观察的现象**：generic 列表的长短差异；`tdp_ram` 是否完全没有资源选择开关。
4. **预期结果**：
   - `tdp_ram` 不提供资源类型开关。
   - 若必须指定资源类型，可在**工程约束文件**（如 Vivado 的 `(* ram_style = "block" *)`）里对实例加属性，或改用 `sdp_ram`（如果单向流即可）。
5. **待本地验证**：实际综合后查看资源利用率报告，确认工具默认推断成了哪一类存储。

#### 4.3.5 小练习与答案

**练习 1**：`tdp_ram` 没有 `ram_style_g`，这会带来什么实际影响？

> **答案**：用户无法在 generic 层面强制选择 BRAM 或分布式 RAM，资源类型由工具默认推断。多数情况下工具会把这种双口存储推断为 BRAM，但若设计对资源类型敏感，就需要在工程约束里手动指定，或换用提供 `ram_style_g` 的 `sdp_ram`。

**练习 2**：跨时钟约束里，为什么两个方向的 `set_max_delay` 都用较快时钟的周期，而不是各自用对方时钟的周期？

> **答案**：跨域路径若长于较快时钟一个周期，较快时钟那一侧的采样就会失败。取较快时钟周期作为双向上限，是最严格、最安全的约束。

### 4.4 在缓冲中的角色

#### 4.4.1 概念说明

讲清楚 `tdp_ram` 在 psi_common 库里到底被谁使用，是本节的重点，也是本讲最容易产生误解的地方。

**先纠正一个常见说法**：大纲里提到「真双口 RAM……作为异步 FIFO 与乒乓缓冲的底层存储」。但查源码可知，psi_common 的**异步 FIFO（`psi_common_async_fifo`）实际上用的是 `psi_common_sdp_ram`（简单双口，`is_async_g => true`），而不是 `tdp_ram`**。这一点可以在 [hdl/psi_common_async_fifo.vhd:252](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L252) 直接看到。原因是：异步 FIFO 的数据流是单向的（一端写、另一端读），简单双口刚好够用且更省。

**真正使用 `tdp_ram` 的是乒乓缓冲 `psi_common_ping_pong`**。乒乓缓冲需要一个写端口接连续输入流、一个读端口接另一时钟域的读取侧，`tdp_ram` 的双口结构正好满足。

> 一句话记忆：「**异步 FIFO → sdp_ram；乒乓缓冲 → tdp_ram**」。不要把两者搞混。

#### 4.4.2 核心流程

`ping_pong` 内部例化 `tdp_ram` 时，端口连接逻辑是：

```
端口 A（接输入时钟 clk_i）：
  a_clk_i  <= clk_i
  a_addr_i <= 写地址（dpram_add_s）
  a_wr_i   <= 写使能（dpram_wren_s）
  a_dat_i  <= 写数据（dpram_data_write_s）
  a_dat_o  => open                      -- A 口不关心读出

端口 B（接存储侧时钟 mem_clk_i）：
  b_clk_i  <= mem_clk_i
  b_addr_i <= 读地址（dpram_read_add_s）
  b_wr_i   <= '0'                       -- B 口只读，不写
  b_dat_i  <= (others => '0')
  b_dat_o  => mem_dat_o                 -- 读数据输出
```

注意 `ping_pong` 这里其实只用了「A 写、B 读」这种**单向**接法（`b_wr_i` 恒为 `'0'`、`a_dat_o` 悬空）。这在功能上简单双口也能做，但选用 `tdp_ram` 的好处是：它的双对称端口与 FPGA 真双口 BRAM 原语天然对应，综合推断更直接、更省事。

#### 4.4.3 源码精读

`ping_pong` 对 `tdp_ram` 的例化在 [hdl/psi_common_ping_pong.vhd:186-200](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L186-L200)：

```vhdl
inst_dpram_pp : entity work.psi_common_tdp_ram
  generic map(depth_g    => ram_depth_c,
              width_g    => width_g,
              behavior_g => ram_behavior_g)
  port map(a_clk_i  => clk_i,
           a_addr_i => dpram_add_s,
           a_wr_i   => dpram_wren_s,
           a_dat_i  => dpram_data_write_s,
           a_dat_o  => open,          -- A 口读出不用
           --
           b_clk_i  => mem_clk_i,
           b_addr_i => dpram_read_add_s,
           b_wr_i   => '0',           -- B 口只读
           b_dat_i  => (others => '0'),
           b_dat_o  => mem_dat_o);
```

可以看到三个 generic 都被透传：`depth_g` 用 `ping_pong` 内部算出的 `ram_depth_c`，`width_g` 直传，`behavior_g` 由上层 `ram_behavior_g` 决定（让用户能按目标 FPGA 选 RBW/WBR）。

作为对照，异步 FIFO 用的是 `sdp_ram`（[hdl/psi_common_async_fifo.vhd:252-258](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L252-L258)）：

```vhdl
i_ram : entity work.psi_common_sdp_ram
  generic map( ...
              ram_style_g    => ram_style_g,
              ram_behavior_g => ram_behavior_g)
  ...
```

注意它透传的是 `ram_style_g`——这正是 `tdp_ram` 所没有的 generic，从侧面印证了两者用的是不同组件。

#### 4.4.4 代码实践：跟踪 ping_pong 对 tdp_ram 的例化

1. **实践目标**：看清 `tdp_ram` 在真实组件里是如何被接线、如何把 generic 透传给最终用户的。
2. **操作步骤**：
   - 打开 [hdl/psi_common_ping_pong.vhd:186-200](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L186-L200)。
   - 向上找到 `ping_pong` 实体的 generic 声明（在文件开头），确认 `ram_behavior_g` 是否作为 `ping_pong` 自己的 generic 暴露给用户。
   - 跟踪 `dpram_wren_s`、`dpram_add_s`、`dpram_data_write_s` 三个信号是在哪里被驱动的（即乒乓写逻辑）。
3. **需要观察的现象**：`tdp_ram` 的 generic 如何经 `ping_pong` 透传到顶层；B 口恒为只读、A 口负责写入。
4. **预期结果**：你会看到 `ping_pong` 把 `ram_behavior_g` 直接传给 `tdp_ram` 的 `behavior_g`，让最终用户能按 FPGA 选用 RBW 或 WBR；而资源类型（`ram_style`）由于 `tdp_ram` 不支持，所以 `ping_pong` 也不暴露这个选项。
5. **待本地验证**：可在 `ping_pong` 的测试平台里观察写入一段连续数据后、从 `mem_dat_o` 读出的波形是否符合乒乓交替。

#### 4.4.5 小练习与答案

**练习 1**：既然 `ping_pong` 只用「A 写、B 读」，为什么不用 `sdp_ram`？

> **答案**：功能上 `sdp_ram`（`is_async_g => true`）也能满足单向双口需求。选 `tdp_ram` 的主要理由是与 FPGA 真双口 BRAM 原语形态一致、综合推断更直接；同时为将来「两侧都可能写」留出扩展余地。这是工程取舍，不是非此不可。

**练习 2**：如果要做一个「两个 CPU 都能读写同一块共享内存」的组件，应该选哪个 RAM？

> **答案**：必须选 `tdp_ram`（或 `tdp_ram_be`）。因为两个主机都要写，简单双口的「一端只写、一端只读」模型无法满足。

**练习 3**：为什么说「异步 FIFO 用 tdp_ram」是错的？

> **答案**：因为 `psi_common_async_fifo` 在 [hdl/psi_common_async_fifo.vhd:252](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_async_fifo.vhd#L252) 例化的是 `psi_common_sdp_ram`（简单双口，异步模式），不是 `tdp_ram`。异步 FIFO 的数据流是单向的，简单双口刚好够用。

## 5. 综合实践

把四个最小模块串起来，完成下面这个「源码阅读 + 接口设计」小任务：

**任务背景**：你要为一个跨时钟子系统挑选底层 RAM，需求是——

- 写侧时钟 `clk_wr`（200 MHz）、读侧时钟 `clk_rd`（50 MHz），两者完全异步；
- 写侧每拍写一个 32 位字；
- 读侧偶尔需要**只更新某一个字节**（用于命令寄存器式的局部修改）；
- 读侧也可能写、写侧也可能读（双向共享）。

**请完成**：

1. **选型**：在 `sdp_ram`、`tdp_ram`、`tdp_ram_be` 三者中选一个，并写出理由。
   - 提示：需求里有「双向共享」与「字节级局部修改」，因此唯一能满足的是 `tdp_ram_be`。
2. **generic 推导**：若存储深度为 1024、位宽 32，写出实例化时的 generic 与地址端口宽度。
   - 地址宽度 = \( \lceil \log_2 1024 \rceil = 10 \) 位。
3. **端口映射草图**：参考 [hdl/psi_common_ping_pong.vhd:186-200](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd#L186-L200) 的写法，把端口 A 接 `clk_wr`、端口 B 接 `clk_rd`，并给 `a_be_i`/`b_be_i` 接上各自的字节使能。
4. **约束**：参照 [doc/files/psi_common_tdp_ram.md:59-62](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/doc/files/psi_common_tdp_ram.md#L59-L62)，写出 Vivado 的 `set_max_delay` 约束（取较快时钟 200 MHz 的周期 5.0 ns 作为双向上限）。
5. **字节使能自检**：参考 4.2.4 的表格，若端口 B 用 `be_i = "0010"` 写 `0xFFFFFFFF` 到一个初值为 `0x00000000` 的地址，预测读回值。
   - 答案：`0x0000FF00`（只 byte1，即 `bits[15:8]` 被改写）。

完成后再回到 [testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd:183-219](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/testbench/psi_common_tdp_ram_be_tb/psi_common_tdp_ram_be_tb.vhd#L183-L219)，看 TB 是如何从端口 B 写、再从端口 A 跨端口读回的——这正好演示了真双口「两侧都能写、共享同一存储」的核心能力。

## 6. 本讲小结

- **真双口 = 对称**：`tdp_ram` 的两个端口都具备完整的 `clk/addr/wr/dat_i/dat_o`，任一端口都可读可写，区别于「一端只写、一端只读」的简单双口 `sdp_ram`。
- **跨时钟是两者都支持的能力**：`sdp_ram` 靠 `is_async_g` 拆双进程，`tdp_ram` 靠两个对称进程。是否选 `tdp_ram` 取决于**是否需要双向读写**，而非「是否跨时钟」。
- **字节使能**：`tdp_ram_be` 增加 `be_i`（宽度 `width_g/8`），逐字节决定是否写入；`be_i(0)` 对应最低字节；位宽必须为 8 的倍数。
- **没有 `ram_style_g`**：与 `sdp_ram` 不同，`tdp_ram` / `tdp_ram_be` 不暴露资源类型开关，唯一综合侧旋钮是 `behavior_g`（RBW/WBR）。
- **跨时钟必须加约束**：两个端口用异步时钟时，要在约束文件里对跨域路径加 `set_max_delay`（取较快时钟周期为双向上限）。
- **真实使用者**：`tdp_ram` 在库内被 `ping_pong`（乒乓缓冲）使用；异步 FIFO 用的是 `sdp_ram`，**不是** `tdp_ram`。

## 7. 下一步学习建议

- **进入 FIFO 层**：本讲讲了底层存储，下一讲 u4-l1（`sync_fifo`）会展示 `sdp_ram` 如何被包成同步 FIFO，u4-l2（`async_fifo`）会展示格雷码指针 + `sdp_ram` 如何实现跨时钟 FIFO。届时你会更清楚为什么 FIFO 选 `sdp_ram` 而不是 `tdp_ram`。
- **阅读乒乓缓冲**：学完 `tdp_ram` 后，可以直接读 [hdl/psi_common_ping_pong.vhd](https://github.com/paulscherrerinstitute/psi_common/blob/98c2fcc75fa11edfe837cf0de885da95d94c871f/hdl/psi_common_ping_pong.vhd)，看它如何围绕 `tdp_ram` 构建双 RAM 交替写入/读出逻辑。
- **CDC 专题**：本讲的「跨时钟约束」是 CDC 的工程实现细节，更系统的 CDC 理论（脉冲/状态/位跨越）在 u5 单元。
- **动手建议**：在本地按 u1-l3 的流程跑一遍 `tdp_ram_be` 的回归测试，亲眼看到 4 条字节使能断言全部通过。
