# AXI 类型、常量、初始化与字符串转换

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `axi_ms_r` 与 `axi_sm_r` 这两条 record 是**按“谁驱动这条信号”来分组**的，而不是按通道分组：主机驱动的所有信号（含 `rready`/`bready`）都在 `axi_ms_r` 里，从机驱动的所有信号都在 `axi_sm_r` 里，并能据此预测任意一个 AXI 信号属于哪条 record。
- 默写三组命名常量的含义：`xRESP_*`（响应码）、`xBURST_*`（突发类型）、`AxSIZE_*`（每拍字节数的编码），并用 \( \text{bytes} = 2^{\text{AxSIZE}} \) 解释 `AxSIZE_4_c = "010"` 为什么代表 4 字节。
- 解释 `axi_master_init` / `axi_slave_init` 把整条总线拉回“全 0、所有 valid/ready 为 0”的安全空闲态的做法，看出它们是 `signal ... out` 过程（用信号赋值 `<=`），并理解为什么 BFM 里每个事务结束后都要**重新调用一次 init** 来撤销 valid。
- 讲清 `decimal_string_to_*` / `hex_string_to_*` 四个函数存在的根本原因——**VHDL 的 `integer` 只有 32 位有符号**，装不下 AXI 常见的 64 位数据——以及它们“逐字符乘基加值、用 `resize` 截到目标位宽”的算法，并知道它们**不解析正负号**、负数只能用十六进制补码传入。
- 写出一个最小 testbench：声明 `axi_ms_r`/`axi_sm_r` 信号并调用两个 init 过程，再用 `hex_string_to_unsigned` 把一个十六进制字符串解析成 64 位 `unsigned` 并打印校验。

## 2. 前置知识

本讲承接 [u3-l1 基础比较过程](u3-l1-compare-basic.md)，并开始进入本仓库**最复杂**的一个 package。请先确认你已经了解：

- **`###ERROR###` 前缀与 CI 联动**（[u1-l3 仿真环境与 CI 构建流程](u1-l3-simulation-and-ci.md)）：psi_tb 全库统一的错误标记，由 `run_check_errors "###ERROR###"` 扫描。本讲的 init 过程本身不报错，但本 package 后续所有 BFM 事务（`axi_single_*`、`axi_expect_*`）在响应码不对时会通过 `StdlvCompareStdlv` 打印 `###ERROR###`——这些过程正是建立在本讲定义的类型与常量之上。
- **比较包的复用**（[u3-l1](u3-l1-compare-basic.md)）：本 package 头部 `use work.psi_tb_compare_pkg.all`（[hdl/psi_tb_axi_pkg.vhd:16](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L16)），`axi_single_write` 等会调用 `StdlvCompareStdlv`、`axi_expect_*` 会调用 `StdlvCompareInt`/`StdlCompare`。本讲先把“骨架”搭起来，事务里如何复用 compare 留给 [u5-l2](u5-l2-axi-single-transactions.md)。
- **`str` / `hstr` 字符串转换**（[u2-l1 字符串与数值转换函数](u2-l1-txt-util-conversions.md)）：本 package 头部 `use work.psi_tb_txt_util.all`（[hdl/psi_tb_axi_pkg.vhd:17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L17)）。本讲的实践里用 `to_string`/`hstr` 打印解析结果——回顾一句话：`to_string(integer)` 输出十进制，`hstr(slv)` 输出十六进制（MSB 在左）。
- **testbench 不可综合**（[u1-l1](u1-l1-project-overview.md)）：所以本 package 可以放心使用 VHDL record、`numeric_std` 的 `unsigned`/`signed`/`resize`、`to_unsigned`/`to_signed`、以及无限等待 `wait until rising_edge(clk)` 这些只为仿真存在的特性。

需要先建立的直觉：**AXI 是一个“五通道、每通道一对 valid/ready 握手”的总线**。这五条通道是：读地址（AR）、读数据（R）、写地址（AW）、写数据（W）、写响应（B）。其中 AR/AW/W 三条通道的方向是“主机 → 从机”，R/B 两条通道的方向是“从机 → 主机”。但每条通道内部又分“谁发 valid+载荷”和“谁发 ready”。本讲的核心洞察就是：psi_tb 没有按通道、也没有按方向去拆 record，而是按 **“这条信号由哪一端驱动”** 去拆——主机驱动的全进 `axi_ms_r`，从机驱动的全进 `axi_sm_r`。理解了这一点，五六十个 AXI 信号就只剩“两束线”了。

> 名词约定：本讲里 “ms” = master 端驱动的信号束（`axi_ms_r`），“sm” = slave 端驱动的信号束（`axi_sm_r`）。这是 psi_tb 的命名习惯，源码里没有给出字母含义注释，但下文 4.1 会用代码本身证明这个分组规则。

## 3. 本讲源码地图

本讲只涉及一个源文件，它是整个 AXI BFM 的“地基”——本讲只读它的**类型/常量/初始化/字符串转换**部分，事务过程留给后续讲义：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd) | AXI 总线功能模型包。本讲取其中 4 块“地基”：常量（第 24–40 行）、`axi_ms_r`/`axi_sm_r` 记录类型（第 42–100 行）、`decimal/hex_string_to_*` 转换函数（声明第 103–117 行、实现第 339–465 行）、`axi_master_init`/`axi_slave_init`（第 467–517 行）。其余 `axi_single_*`/`axi_apply_*`/`axi_expect_*` 事务过程留给 [u5-l2](u5-l2-axi-single-transactions.md) 与 [u5-l3](u5-l3-axi-partial-and-burst.md)。 |

文件沿用 psi_tb 的“声明 + 实现”成对组织：`package psi_tb_axi_pkg is`（第 22–332 行）放常量、类型、函数/过程**声明**；`package body`（第 337–1274 行）放**实现**。注意 [hdl/psi_tb_axi_pkg.vhd:1-5](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1-L5) 的版权注释：作者 Oliver Bruendler，时间 2017 年。

它在编译链里依赖三个包（[hdl/psi_tb_axi_pkg.vhd:14-17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L14-L17)）：

```vhdl
library work;
use work.psi_common_math_pkg.all;  -- 提供 log2（事务过程里算 AxSIZE 用，本讲不直接用到）
use work.psi_tb_compare_pkg.all;   -- 提供 StdlvCompareStdlv / StdlvCompareInt / StdlCompare 等
use work.psi_tb_txt_util.all;      -- 提供 str / hstr / print / to_string
```

> 注意：`psi_common_math_pkg` 来自外部依赖 **psi_common**（[u1-l2](u1-l2-repository-structure.md) 讲过的同级目录），不在本仓库内。它的 `log2` 只在 `axi_single_*`/`axi_apply_*` 里被用来“按数据总线宽度自动算出 AxSIZE”，本讲覆盖的四块地基**不调用** `log2`，因此本讲可以在不依赖 psi_common 细节的情况下读懂。

本讲四块“地基”一览：

| 模块 | 在源码中的位置 | 解决什么 |
| --- | --- | --- |
| 三组常量 `xRESP_*` / `xBURST_*` / `AxSIZE_*` | 第 24–40 行 | 给 AXI 协议规定的 2/3 位编码起**可读名字**，避免满代码里散落 `"00"`/`"01"` |
| `axi_ms_r` / `axi_sm_r` 记录 | 第 42–100 行 | 把 ~40 个 AXI 信号**按驱动方**捆成两束，让 BFM 过程只传两个参数 |
| `axi_master_init` / `axi_slave_init` | 第 467–517 行 | 把整束信号拉回“全 0、valid/ready 全 0”的安全空闲态 |
| `decimal/hex_string_to_*` 四函数 | 声明 103–117、实现 339–465 | 绕开 32 位 `integer`，用字符串表达**任意位宽**的有/无符号整数 |

## 4. 核心概念与源码讲解

### 4.1 `axi_ms_r` / `axi_sm_r`：按“谁驱动这条信号”打包 AXI 五通道

#### 4.1.1 概念说明

AXI4 接口的信号多到让人头疼：光一个完整接口就有 `arid`/`araddr`/`arlen`/`arsize`/`arburst`/…/`arvalid`/`arready`、`rdata`/`rresp`/`rvalid`/`rready`/…、`aw*`、`w*`、`b*` 几十个。如果在 testbench 里把它们一条条当独立 `signal` 声明，再一条条传给每个 BFM 过程，过程参数表会膨胀到几十项，根本没法维护。

psi_tb 的解法和大多数现代 VHDL BFM 一样：**用 record（记录类型）把相关信号捆成一束**。但捆法有讲究——它没有按“通道”捆（那样会有 5 束），而是按 **“这条信号由哪一端驱动”** 捆成**两束**：

- `axi_ms_r`：**主机（master）驱动**的全部信号。
- `axi_sm_r`：**从机（slave）驱动**的全部信号。

这里的“驱动”是关键字。回忆 AXI 每条通道都有一对 valid/ready 握手，其中一端发 valid+载荷、另一端发 ready。把“发 valid+载荷的那端”和“发 ready 的那端”分别归到两束里，就得到：

- **AR 通道**（主机发起读地址）：`arvalid`/`araddr`/`arlen`/… 是主机驱动 → 进 `axi_ms_r`；`arready` 是从机驱动 → 进 `axi_sm_r`。
- **R 通道**（从机回读数据）：`rvalid`/`rdata`/`rresp`/… 是从机驱动 → 进 `axi_sm_r`；但 `rready`（主机表示“我能接收读数据”）是**主机**驱动 → 进 `axi_ms_r`。
- **AW 通道**（主机发起写地址）：`awvalid`/`awaddr`/… 主机驱动 → `axi_ms_r`；`awready` 从机驱动 → `axi_sm_r`。
- **W 通道**（主机发写数据）：`wvalid`/`wdata`/`wstrb`/… 主机驱动 → `axi_ms_r`；`wready` 从机驱动 → `axi_sm_r`。
- **B 通道**（从机回写响应）：`bvalid`/`bresp`/… 从机驱动 → `axi_sm_r`；但 `bready`（主机表示“我能接收写响应”）是**主机**驱动 → 进 `axi_ms_r`。

注意两个“反直觉”的归属：`rready` 和 `bready` 虽然分别属于 R/B 通道（数据方向是“从机→主机”），但它们由**主机**驱动，所以落在 `axi_ms_r` 里。判断归属的唯一标准是“谁把这条信号赋值出去”，而不是“这条信号在哪条通道”。

这样切分的好处直接体现在 BFM 过程的参数表上。看 `axi_single_write` 的声明（[hdl/psi_tb_axi_pkg.vhd:125-129](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L125-L129)）：

```vhdl
procedure axi_single_write(address    : in integer;
                           value      : in integer;
                           signal ms  : out axi_ms_r;   -- 主机 BFM 驱动 ms（输出）
                           signal sm  : in axi_sm_r;     -- 主机 BFM 观察 sm（输入）
                           signal clk : in std_logic);
```

一个主机侧 BFM 过程，只需 `ms : out`（我要驱动的信号束）和 `sm : in`（我要观察的信号束）两个总线参数，外加一个时钟。这就是 record 化带来的简洁性——也是为什么本讲必须先讲清楚这两条 record。

#### 4.1.2 核心流程

把五通道 × 两驱动方画成一张表，就能一眼看清两条 record 各装了什么（✓ 表示该信号在这一束里）：

| 通道 | 方向 | 主机驱动的信号（→ `axi_ms_r`） | 从机驱动的信号（→ `axi_sm_r`） |
| --- | --- | --- | --- |
| AR（读地址） | 主→从 | `arvalid`,`arid`,`araddr`,`arlen`,`arsize`,`arburst`,`arlock`,`arcache`,`arprot`,`arqos`,`arregion`,`aruser` | `arready` |
| R（读数据） | 从→主 | `rready` | `rvalid`,`rid`,`rdata`,`rresp`,`rlast`,`ruser` |
| AW（写地址） | 主→从 | `awvalid`,`awid`,`awaddr`,`awlen`,`awsize`,`awburst`,`awlock`,`awcache`,`awprot`,`awqos`,`awregion`,`awuser` | `awready` |
| W（写数据） | 主→从 | `wvalid`,`wdata`,`wstrb`,`wlast`,`wuser` | `wready` |
| B（写响应） | 从→主 | `bready` | `bvalid`,`bid`,`bresp`,`buser` |

在 testbench 里的接线逻辑因此非常简单：

```
若 DUT 是从机（slave）：
    testbench 里的 master BFM   ──驱动 ms──>  DUT.slave_port
    testbench 里的 master BFM   <──观察 sm──  DUT.slave_port
    （ms 的每一根接到 DUT 的同名 AXI 输入，sm 的每一根接到 DUT 的同名 AXI 输出）

若 DUT 是主机（master）：
    DUT.master_port  ──驱动 ms──>  testbench 里的 slave BFM（它把 ms 当 in 观察）
    DUT.master_port  <──观察 sm──  testbench 里的 slave BFM（它把 sm 当 out 驱动）
```

这正是后续 `axi_apply_*`（驱动一端）与 `axi_expect_*`（观察并校验另一端）两类过程能成对工作的前提：master 侧过程把 `ms` 当 `out`、`sm` 当 `in`；slave 侧过程反过来，把 `ms` 当 `in`、`sm` 当 `out`（对比 [hdl/psi_tb_axi_pkg.vhd:223-229](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L223-L229) 的 `axi_expect_aw` 参数方向）。

#### 4.1.3 源码精读

先看主机束 `axi_ms_r` 的定义：

[hdl/psi_tb_axi_pkg.vhd:42-79](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L42-L79) —— 定义 `axi_ms_r`，按 AR / R / AW / W / B 五段注释分组，包含主机驱动的全部信号（含 `rready`、`bready`）。

关键细节：字段里有些写成了 `std_logic_vector;`（**没有范围**），有些写成了 `std_logic_vector(7 downto 0)`（有范围）。前者是 **VHDL-2008 的“未约束记录字段”（unconstrained record element）**，宽度在使用端（声明信号时）才指定。下面这些是未约束的（宽度可变、由实例决定）：

| 字段 | 含义 | 为什么宽度可变 |
| --- | --- | --- |
| `arid`/`awid`/`rid`/`bid` | 事务 ID tag | 不同 AXI 实现的 ID 宽度不同（4/8/16 位都常见） |
| `araddr`/`awaddr` | 地址 | 地址总线宽度可变（32/64 位） |
| `aruser`/`awuser`/`wuser`/`ruser`/`buser` | 用户自定义侧带信号 | 完全由 SoC 设计者定义，宽度任意 |
| `wdata`/`rdata` | 数据 | 数据总线宽度可变（32/64/128/… 位） |
| `wstrb` | 写字节使能 | 宽度恒等于 `wdata` 的字节数 = `wdata'length/8` |

而 `arlen`(8 位)、`arsize`(3 位)、`arburst`(2 位)、`arcache`(4 位)、`arprot`(3 位)、`arqos`(4 位)、`arregion`(4 位)、各种单比特 `std_logic` 字段——这些都是 AXI 协议**固定宽度**的，所以直接在类型里写死了范围。

再看从机束 `axi_sm_r`：

[hdl/psi_tb_axi_pkg.vhd:81-100](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L81-L100) —— 定义 `axi_sm_r`，包含从机驱动的全部信号：`arready`、读数据通道的 `rid`/`rdata`/`rresp`/`rlast`/`ruser`/`rvalid`、`awready`、`wready`、写响应通道的 `bid`/`bresp`/`buser`/`bvalid`。

把两条 record 的字段数一下：`axi_ms_r` 的未约束字段有 9 个（`arid`/`araddr`/`aruser`/`awid`/`awaddr`/`awuser`/`wdata`/`wstrb`/`wuser`），`axi_sm_r` 的未约束字段有 5 个（`rid`/`rdata`/`ruser`/`bid`/`buser`）。**这意味着声明 `axi_ms_r`/`axi_sm_r` 类型的信号时，必须给这些未约束字段显式指定宽度**——具体语法见 4.1.4 的实践。

#### 4.1.4 代码实践

**实践目标**：亲手声明两条 record 类型的信号，验证“未约束字段必须在声明处指定宽度”这一 VHDL-2008 特性，并观察 record 如何把一大把信号收敛成两个名字。

**操作步骤**（示例代码，**非项目原有**——本仓库没有注册 AXI testbench，故无现成样例）：

```vhdl
-- 示例代码：声明一个 32 位数据、4 位 ID 的 AXI master/slave 信号对
-- 注意：VHDL-2008 record 子类型约束语法，需在仿真器中启用 VHDL-2008
signal ms : axi_ms_r(
    arid(7 downto 0),     -- ID 宽度
    araddr(31 downto 0),  -- 地址宽度
    aruser(7 downto 0),
    awid(7 downto 0),
    awaddr(31 downto 0),
    awuser(7 downto 0),
    wdata(31 downto 0),   -- 数据宽度
    wstrb(3 downto 0),    -- 字节使能 = wdata 字节数
    wuser(7 downto 0)
);

signal sm : axi_sm_r(
    rid(7 downto 0),
    rdata(31 downto 0),
    ruser(7 downto 0),
    bid(7 downto 0),
    buser(7 downto 0)
);
```

**需要观察的现象**：

1. 编译时，若你漏掉任意一个未约束字段（例如忘了给 `wstrb` 指定范围），仿真器会报“record element unconstrained / must be constrained”之类的错误——这正是 4.1.3 说的“宽度由使用端决定”。
2. `wstrb` 的宽度必须等于 `wdata'length/8`（这里 32 位 → 4 字节 → 4 位），否则 AXI 协议不一致；源码里 `axi_single_write` 用 `log2(ms.wstrb'length)` 反推 AxSIZE（[hdl/psi_tb_axi_pkg.vhd:531](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L531)），就默认了这一关系。

**预期结果**：能通过 VHDL-2008 编译，两个信号 `ms`/`sm` 出现在波形窗里，展开后能看到全部 AXI 字段。

> 待本地验证：不同仿真器（ModelSim、GHDL、Vivado xvhdl）对“在信号声明处直接写 record 子类型约束”这一 VHDL-2008 语法的支持与书写细节可能略有差异；若你的工具不接受上面的内联写法，常见替代做法是先定义一个带约束的子类型 `subtype axi_ms_my_t is axi_ms_r(...);` 再声明信号。本仓库未提供现成样例，故标注待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：AXI5 里有一个 `bresp`（写响应码）信号。它属于 `axi_ms_r` 还是 `axi_sm_r`？为什么？

**答案**：属于 `axi_sm_r`。因为 `bresp` 是**从机**在 B（写响应）通道上驱动给主机的（写完之后从机告诉主机“这次写成功/失败”），凡是“从机驱动”的信号都在 `axi_sm_r` 里（见 [hdl/psi_tb_axi_pkg.vhd:97](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L97)）。

**练习 2**：`rready` 在 R 通道上，而 R 通道的数据方向是“从机→主机”。为什么 `rready` 却在 `axi_ms_r` 里？

**答案**：record 的分组标准是“谁驱动”，不是“数据方向”。`rready` 表示“主机准备好接收读数据”，是**主机**驱动的，所以归 `axi_ms_r`（见 [hdl/psi_tb_axi_pkg.vhd:57](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L57)）。同理 `bready` 也在 `axi_ms_r`（[hdl/psi_tb_axi_pkg.vhd:78](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L78)）。

---

### 4.2 三组命名常量：`xRESP_*` / `xBURST_*` / `AxSIZE_*`

#### 4.2.1 概念说明

AXI 协议把若干控制字段编码成短短的 2 位或 3 位向量。例如响应码是 2 位：`"00"`=OKAY、`"01"`=EXOKAY、`"10"`=SLVERR、`"11"`=DECERR。如果在 testbench 里到处写裸字面量 `"10"`，读代码的人根本看不出这是“从机报了 SLVERR”还是“某个地址的高两位”。更危险的是，写错一位（`"10"` 写成 `"01"`）编译器不会报错，却把语义从“错误”变成了“Exclusive OK”。

psi_tb 的做法是给这些编码**起名字**，定义成 `constant`。本 package 一开头就定义了三组常量（[hdl/psi_tb_axi_pkg.vhd:24-40](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L24-L40)）：

- **`xRESP_*`**：4 个响应码（2 位）。
- **`xBURST_*`**：3 个突发类型（2 位）。
- **`AxSIZE_*`**：8 个“每拍字节数”编码（3 位）。

有了它们，BFM 代码就能写成 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)`（“期望响应是 OKAY”）而不是 `StdlvCompareStdlv("00", sm.bresp, ...)`，可读性天差地别。这也是 [u3-l1](u3-l1-compare-basic.md) 讲过的 `StdlvCompareStdlv` 第一次“带着业务语义”出场——比较的双方一个是命名常量、一个是 DUT 实际输出的响应码。

#### 4.2.2 核心流程

三组常量的值与含义如下表。

**响应码 `xRESP_*`**（2 位，[hdl/psi_tb_axi_pkg.vhd:24-27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L24-L27)）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `xRESP_OKAY_c` | `"00"` | 正常成功（Normal access success） |
| `xRESP_EXOKAY_c` | `"01"` | 独占访问成功（Exclusive access OK） |
| `xRESP_SLVERR_c` | `"10"` | 从机错误（Slave error，从机内部出错） |
| `xRESP_DECERR_c` | `"11"` | 解码错误（Decode error，地址没有对应的从机） |

> BFM 的惯例：只有 `xRESP_OKAY_c` 视为“成功”。本 package 里 `axi_single_write` 收到 bresp 后直接 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, "received negative response!")`（[hdl/psi_tb_axi_pkg.vhd:549](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549)）——任何非 OKAY 的响应都会被当成“negative response”并打印 `###ERROR###`。

**突发类型 `xBURST_*`**（2 位，[hdl/psi_tb_axi_pkg.vhd:29-31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L29-L31)）：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `xBURST_FIXED_c` | `"00"` | 固定地址（每拍地址不变，FIFO 类访问） |
| `xBURST_INCR_c` | `"01"` | 递增（每拍地址按 size 递增，最常见） |
| `xBURST_WRAP_c` | `"10"` | 回卷（递增但到边界回绕，Cache 行访问） |

**每拍字节数 `AxSIZE_*`**（3 位，[hdl/psi_tb_axi_pkg.vhd:33-40](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L33-L40)）。AxSIZE 是一个“指数编码”：每拍传输的字节数为

\[
\text{bytes} = 2^{\text{AxSIZE}}
\]

所以：

| 常量 | 值（二进制） | AxSIZE 数值 | 每拍字节数 |
| --- | --- | --- | --- |
| `AxSIZE_1_c` | `"000"` | 0 | \(2^0 = 1\) |
| `AxSIZE_2_c` | `"001"` | 1 | \(2^1 = 2\) |
| `AxSIZE_4_c` | `"010"` | 2 | \(2^2 = 4\) |
| `AxSIZE_8_c` | `"011"` | 3 | \(2^3 = 8\) |
| `AxSIZE_16_c` | `"100"` | 4 | \(2^4 = 16\) |
| `AxSIZE_32_c` | `"101"` | 5 | \(2^5 = 32\) |
| `AxSIZE_64_c` | `"110"` | 6 | \(2^6 = 64\) |
| `AxSIZE_128_c` | `"111"` | 7 | \(2^7 = 128\) |

反过来，已知数据总线字节数求 AxSIZE 就是取以 2 为底的对数：

\[
\text{AxSIZE} = \log_2(\text{bytes})
\]

这正是 `axi_single_write` 里 `to_unsigned(log2(ms.wstrb'length), ms.awsize'length)` 的来历（[hdl/psi_tb_axi_pkg.vhd:531](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L531)）：`wstrb'length` 就是字节使能的位数 = 字节数，`log2` 它得到 AxSIZE 数值，再转成 3 位向量写进 `awsize`。（这一行的完整讲解属于 [u5-l2](u5-l2-axi-single-transactions.md)，这里只用来印证 AxSIZE 的编码。）

#### 4.2.3 源码精读

三组常量在源码里是连续定义的，没有任何依赖，纯字面量赋值：

[hdl/psi_tb_axi_pkg.vhd:24-40](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L24-L40) —— 定义三组常量。其中响应码、突发类型是 2 位 `std_logic_vector(1 downto 0)`，AxSIZE 是 3 位 `std_logic_vector(2 downto 0)`，值与 4.2.2 的表格完全对应。

命名约定也值得注意：后缀 `_c` 表示 “constant”，前缀 `x` 表示 “AXI”（避免与 VHDL 关键字或其它库冲突），`Ax` 表示 “对 AW 和 AR 通道都适用”（`awsize` 和 `arsize` 共用同一套 AxSIZE 编码）。这套命名让你一眼能判断常量的“种类与归属”。

#### 4.2.4 代码实践

**实践目标**：在三组常量里“查字典”，确认它们与 AXI 协议编码一致，并在源码里找到它们被使用的真实位置。

**操作步骤**：

1. 打开 [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd)，对照 4.2.2 的三张表，逐行核对第 24–40 行的常量值。
2. 在本文件内搜索 `xRESP_OKAY_c`，观察它出现在哪些事务过程里、扮演什么角色（例如 [第 549 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549)、[第 591 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L591)、[第 629 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L629)、[第 667 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L667) 都是“与读/写响应比较”）。
3. 搜索 `xBURST_INCR_c`，注意 `axi_master_init` 默认把 `awburst` 设成 `"01"`（INCR，[第 473 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L473)）——它没有直接引用 `xBURST_INCR_c` 这个常量名，而是写了字面量 `"01"`，但二者等价。

**需要观察的现象**：常量值与协议表格逐位吻合；`xRESP_OKAY_c` 在源码里只作为“期望响应”出现在比较的一侧。

**预期结果**：你能口头回答“SLVERR 的编码是什么”“AxSIZE_4_c 代表每拍几个字节”而不必回去翻代码。

#### 4.2.5 小练习与答案

**练习 1**：一个 64 位（8 字节）数据宽度的 AXI 接口，每拍传输 8 字节，`AxSIZE` 字段应该填哪个常量？

**答案**：`AxSIZE_8_c`（`"011"`）。因为每拍 8 字节，\( \log_2(8) = 3 \)，对应 AxSIZE 数值 3，即 `AxSIZE_8_c`。

**练习 2**：为什么 `axi_master_init` 在 [第 473 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L473) 用字面量 `"01"` 而不是 `xBURST_INCR_c`？这是 bug 吗？

**答案**：不是 bug，只是风格上的小不一致。`"01"` 与 `xBURST_INCR_c` 的值完全相同（都表示 INCR 突发）。理想情况下应该统一用常量名以提升可读性，但功能上无误——事务过程（如 [axi_single_write:532](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L532)）在真正发起写之前会重新设定 `awburst`，所以这个 init 默认值实际不会影响传输。

---

### 4.3 `axi_master_init` / `axi_slave_init`：把整条总线拉回安全空闲

#### 4.3.1 概念说明

AXI 是个“电平敏感”的握手协议：只要 `xxvalid` 为 1，对端就可能开始接受。如果 testbench 启动时 `ms.awvalid` 是 `'X'`（未初始化），或者上一次事务结束后忘了把 `awvalid` 拉低，下一次事务或 DUT 就可能误判“主机又要发地址了”。因此 BFM 必须有一个**把整束信号归零**的动作——这就是 `axi_master_init` 和 `axi_slave_init`。

这两个过程的设计有三个要点：

1. **参数是 `signal ... out`**：`procedure axi_master_init(signal ms : out axi_ms_r)`（[hdl/psi_tb_axi_pkg.vhd:120](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L120)）。这意味着它们只能作用在**信号**上（不能是 variable），且用**信号赋值 `<=`**（不是 `:=`）。
2. **不止“开机调用一次”**：它们最重要的用途其实是在**每个事务结束后**被重新调用，把 `xxvalid`/`xxready` 全部拉低，回到“总线空闲、不主动驱动任何 valid”的状态。看 `axi_apply_aw` 末尾（[hdl/psi_tb_axi_pkg.vhd:732](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L732)）和 `axi_apply_bresp` 末尾（[hdl/psi_tb_axi_pkg.vhd:1030](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1030)）——都调用了对应的 init。所以 init 是 BFM 的“复位到空闲”原语，被反复复用。
3. **所有 valid/ready 归 0，载荷归 0**：init 把所有数据/地址/响应载荷清零，所有 `xxvalid`/`xxready`/`rready`/`bready` 清零，这样总线上不会出现“悬空的 valid”。

#### 4.3.2 核心流程

`axi_master_init(ms)` 把 `ms` 的每一个字段按如下规则赋值：

```
对所有“载荷/控制”字段（awid/awaddr/awlen/…/wdata/wstrb/…/arid/araddr/…）:
    <= 0   （用 to_unsigned(0, len) 或 (others=>'0')）
对 awburst:      <= "01"   （INCR；注意这是唯一非零的默认值）
对所有 valid/ready（awvalid/arvalid/wvalid/rready/bready）:
    <= '0'
```

`axi_slave_init(sm)` 类似：

```
对所有“载荷/响应”字段（rid/rdata/ruser/bid/buser）:
    <= 0
对 rresp/bresp:   <= "00"   （OKAY）
对所有 ready/valid（arready/awready/wready/rvalid/bvalid）与 rlast:
    <= '0'
```

因为用信号赋值 `<=`，这些归零不会在“调用 init 的那一刻”立即生效，而是在**下一个 delta 周期**（同一仿真时刻的下一个求值轮次）才驱动到信号上——这正是 BFM 期望的“在本周期事务完成后、下一周期看到空闲”。

#### 4.3.3 源码精读

先看 `axi_master_init` 的实现：

[hdl/psi_tb_axi_pkg.vhd:467-500](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L467-L500) —— `axi_master_init`：把 `ms` 全部字段清零。注意三处细节：

- 宽度自适应：因为 record 含未约束字段，init 不能写死宽度，所以全程用 `ms.xxx'length` 来生成等宽的零向量，例如 `ms.awid <= std_logic_vector(to_unsigned(0, ms.awid'length));`（[第 469 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L469)）。这是 VHDL-2008 未约束 record 字段带来的一种典型写法——“用信号自身的 `'length` 属性决定赋值宽度”。
- 一个**不对称**值得留意：`ms.awburst <= "01"`（INCR，[第 473 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L473)），而 `ms.arburst <= std_logic_vector(to_unsigned(0, ms.arburst'length))`（= FIXED `"00"`，[第 485 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L485)）。即写地址通道默认 INCR、读地址通道默认 FIXED。这个不一致无害（每个事务都会显式重设 `arburst`/`awburst`，例如 [axi_single_read:611](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L611) 把 `arburst` 设成 `"01"`），但读源码时不要误以为“init 后两个 burst 都一样”。
- `ms.wdata <= (ms.wdata'length - 1 downto 0 => '0');`（[第 494 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L494)）用了一种“带范围的聚集”（range aggregate）来生成全 0 向量，与 `(others=>'0')` 等价，但显式写出了范围——风格选择而已。

再看 `axi_slave_init`：

[hdl/psi_tb_axi_pkg.vhd:502-517](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L502-L517) —— `axi_slave_init`：把 `sm` 全部字段清零，`rresp`/`bresp` 设成 `"00"`（OKAY）。同样用 `sm.xxx'length` 自适应宽度。

最后看 init 被“复用”的两个真实位置，印证 4.3.1 说的“每个事务结束后调用一次”：

- `axi_apply_ar` 末尾：[hdl/psi_tb_axi_pkg.vhd:749](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L749) —— 主机发完读地址、等 `arready` 握手成功后，立即 `axi_master_init(ms)` 把 `arvalid` 拉低。
- `axi_apply_bresp` 末尾：[hdl/psi_tb_axi_pkg.vhd:1030](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L1030) —— 从机发完写响应、等 `bready` 握手成功后，立即 `axi_slave_init(sm)` 把 `bvalid` 拉低。

这个“事务 → 握手 → init 撤销 valid”的模式贯穿整个 package，是理解后续所有 `axi_apply_*` 过程的钥匙。

#### 4.3.4 代码实践

**实践目标**：验证 init 把所有信号归零的效果，并理解信号赋值 `<=` 的“下一 delta 生效”语义。

**操作步骤**（示例代码，非项目原有）：

```vhdl
-- 示例代码：在 process 里调用两个 init，观察波形
process
begin
    -- 先给一些信号塞非零值（模拟上一次事务的残留）
    ms.awvalid <= '1';
    ms.awaddr  <= (others => '1');
    sm.rvalid  <= '1';
    sm.rdata   <= (others => '1');
    wait for 10 ns;

    -- 调用 init，把两束信号拉回安全空闲
    axi_master_init(ms);
    axi_slave_init(sm);
    wait for 10 ns;          -- 等 delta 周期生效后观察

    report "ms.awvalid = " & std_logic'image(ms.awvalid);  -- 期望 '0'
    report "sm.rvalid  = " & std_logic'image(sm.rvalid);   -- 期望 '0'
    wait;
end process;
```

**需要观察的现象**：

1. 调用 `axi_master_init(ms)` 之前，`ms.awvalid='1'`；调用之后（下一个 delta），`ms.awvalid` 变为 `'0'`，`ms.awaddr` 变为全 0。
2. `axi_slave_init(sm)` 同理把 `sm.rvalid` 拉低、`sm.rdata` 清零。
3. 如果把 init 后的 `wait for 10 ns` 去掉、紧接着就 `report`，可能读到旧值——这就是信号赋值“推迟到下一 delta”的特性。

**预期结果**：Transcript 里打印出 `ms.awvalid = '0'` 与 `sm.rvalid = '0'`，波形里两束信号在 init 之后整体归零、所有 valid/ready 为低。

> 待本地验证：本示例依赖 4.1.4 中带 VHDL-2008 record 约束的 `ms`/`sm` 信号声明；若工具不支持内联约束语法，请改用子类型声明。`std_logic'image` 对 `'0'`/`'1'` 会输出带引号的字符，属正常。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `axi_master_init` 的参数写成 `signal ms : out axi_ms_r`，而不是 `variable ms : out axi_ms_r`？

**答案**：因为 AXI 信号在 testbench 里是 `signal`（要在波形里观察、要跨 process 驱动 DUT），并且过程体里用的是信号赋值 `<=`。`signal` 参数才会走“信号更新 / delta 周期”语义；若改成 `variable`，就必须用 `:=` 立即赋值，且无法直接驱动端口信号。

**练习 2**：`axi_apply_ar` 在握手成功后为什么要调用 `axi_master_init(ms)`（[第 749 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L749)）？不调会怎样？

**答案**：握手成功意味着从机已经收下了这次读地址，事务在该通道上结束。若不调 init，`ms.arvalid` 会一直保持 `'1'`，从机会误以为主机又要发起新的读地址事务，从而重复响应。调用 init 把 `arvalid`（连同地址、控制字段）拉回 0，保证“一次 apply 只产生一次有效地址”。

---

### 4.4 `decimal_string_to_*` / `hex_string_to_*`：绕开 32 位整数的“任意位宽”输入

#### 4.4.1 概念说明

VHDL 的 `integer` 类型有硬性上限：**最小 32 位有符号**，范围是 \(-2^{31}\) 到 \(2^{31}-1\)（即 -2 147 483 648 到 2 147 483 647）。这对 32 位 AXI 数据刚好够用（勉强），但 AXI 数据总线常常是 64 位甚至更宽——一个 64 位无符号数最大可达 \(2^{64}-1 \approx 1.8 \times 10^{19}\)，远远超出 `integer` 范围。

后果是：你**无法**用 `integer` 字面量在 testbench 里表达一个任意的 64 位值。比如想在 64 位数据线上写 `0xDEADBEEF12345678`，写成 `value : in integer := 160456909 ...` 直接溢出报错。

psi_tb 的解决办法是：**用字符串表达数值，在仿真时把它解析成任意位宽的 `signed`/`unsigned`**。这正是本 package 四个转换函数的用途（[hdl/psi_tb_axi_pkg.vhd:103-117](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L103-L117)）：

| 函数 | 输入 | 输出 | 典型用法 |
| --- | --- | --- | --- |
| `decimal_string_to_unsigned` | 十进制字符串 | `unsigned`（指定位宽） | 把 `"1234567890123"` 解析成 64 位无符号 |
| `decimal_string_to_signed` | 十进制字符串 | `signed`（指定位宽） | 大正数的 signed 表达 |
| `hex_string_to_unsigned` | 十六进制字符串 | `unsigned`（指定位宽） | 把 `"DEADBEEF12345678"` 解析成 64 位无符号 |
| `hex_string_to_signed` | 十六进制字符串 | `signed`（指定位宽） | 用补码十六进制表达负数 |

这四个函数在 package 里被 `axi_single_write`/`axi_single_expect` 的 **string 重载**（[hdl/psi_tb_axi_pkg.vhd:131-136](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L131-L136)、[第 167-177 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L167-L177)）大量调用，用来把 testbench 里的字符串数值转成数据线上的位模式。所以它们虽然看起来只是“小工具”，却是 AXI BFM 支持 64+ 位数据的基石。

#### 4.4.2 核心流程

四个函数的算法**完全一样**，只是“基”不同（十进制乘 10、十六进制乘 16）和“合法字符集”不同。以 `hex_string_to_unsigned` 为例：

```
result := 0                                   -- 全 0 初始化，宽度 = wanted_bitwidth
for 每个字符 ch in 字符串（从左到右）:
    character_value := 该字符表示的数值         -- '0'..'9' → 0..9，'A'..'F'/'a'..'f' → 10..15
    result := resize(result * 16, wanted_bitwidth)   -- 左移一位十六进制（×16），保持位宽
    result := result + character_value               -- 加上当前位
return result
```

这是一个经典的“**Horner 法**”逐位求值：从最高位向最低位扫描，每读一位就把已有结果乘以基、再加上当前位的值。例如解析十六进制 `"F5"`：

\[
\text{result} = ((0 \times 16 + 15) \times 16 + 5) = 245
\]

即 \(F5_{16} = 245_{10}\)。正确。

两个重要特性来自代码细节：

1. **`resize` 保证位宽恒定**：每步乘法后都 `resize(..., wanted_bitwidth)`，把结果截断/扩展回目标位宽。所以解析出的结果总是恰好 `wanted_bitwidth` 位，超出高位的部分自然丢弃（溢出回绕），这与硬件总线“按位宽截断”的行为一致。
2. **不解析正负号**：函数体里只处理数字字符（`'0'..'9'` 和 `'A'..'F'`/`'a'..'f'`），遇到任何其它字符（包括 `'-'`、空格、`"0x"` 前缀）都会 `report "...: Illegal number" severity failure` 直接让仿真失败。因此：
   - 想表达**负数**的 `signed`，不能传 `"-5"`，只能传它的**补码十六进制**——例如对 8 位 `signed`，`-5` 的补码是 `FB`，应调用 `hex_string_to_signed("FB", 8)`。
   - 字符串里**不要**带 `"0x"`、`"h"`、下划线等装饰，只能是裸数字。

> `report ... severity failure` 与 [u3-l1](u3-l1-compare-basic.md) 里 compare 用的 `severity error` 不同：`failure` 在默认配置下**会立即终止仿真**。这是合理的——传了非法字符串属于“testbench 本身写错了”，没有继续跑的意义，与“DUT 输出不符合预期”（`error`，跑完再说）是两类问题。

#### 4.4.3 源码精读

四个函数声明集中在：

[hdl/psi_tb_axi_pkg.vhd:102-117](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L102-L117) —— 四个转换函数的声明。签名统一：接受一个 `string` 和一个 `wanted_bitwidth : positive`（目标位宽，必须为正整数），返回 `unsigned` 或 `signed`。

实现以 `hex_string_to_unsigned` 为代表：

[hdl/psi_tb_axi_pkg.vhd:391-427](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L391-L427) —— `hex_string_to_unsigned` 实现。要点：

- 结果变量初始化为全 0：`variable tmp_unsigned : unsigned(wanted_bitwidth - 1 downto 0) := (others => '0');`（[第 394 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L394)）。
- `case` 语句把每个字符映射成 0–15，且**同时接受大小写** `'A'..'F'` 与 `'a'..'f'`（[第 398-422 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L398-L422)），所以 `"deadbeef"` 和 `"DEADBEEF"` 等价。
- 每步 `tmp_unsigned := resize(tmp_unsigned * 16, wanted_bitwidth);` 再 `+ character_value`（[第 423-424 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L423-L424)）——即 4.4.2 描述的 Horner 法。
- `when others => report ("hex_string_to_unsigned: Illegal number") severity failure;`（[第 421 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L421)）——非法字符直接 `failure` 终止仿真。

其余三个函数结构完全相同，只是基不同：

- `decimal_string_to_unsigned`：[hdl/psi_tb_axi_pkg.vhd:339-363](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L339-L363)，乘 10，字符集 `'0'..'9'`。
- `decimal_string_to_signed`：[hdl/psi_tb_axi_pkg.vhd:365-389](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L365-L389)，乘 10，返回 `signed`。
- `hex_string_to_signed`：[hdl/psi_tb_axi_pkg.vhd:429-465](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L429-L465)，乘 16，返回 `signed`。

它们在事务过程中的真实调用点：`axi_single_write` 的 string 重载在 `base = 16` 时调 `hex_string_to_signed` 把字符串值搬上数据线（[hdl/psi_tb_axi_pkg.vhd:573-575](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L573-L575)）；`axi_apply_wd_burst` 的 string 重载用它们解析起始值与步进值（[第 825-829 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L825-L829)）。这些是 [u5-l2](u5-l2-axi-single-transactions.md) 与 [u5-l3](u5-l3-axi-partial-and-burst.md) 的内容，这里只用来证明“本讲这四个函数确实被 BFM 核心流程所依赖”。

#### 4.4.4 代码实践

**实践目标**：用 `hex_string_to_unsigned` 解析一个超出 32 位范围的十六进制字符串，验证它得到的 64 位 `unsigned` 值正确，并体会“字符串能表达 integer 装不下的数”。

**操作步骤**（这是一段**自包含**的示例代码，不依赖 record 声明，可直接在一个最小 testbench 里运行）：

```vhdl
-- 示例代码（非项目原有）：解析一个 64 位十六进制字符串并打印校验
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
use std.textio.all;
library work;
use work.psi_tb_axi_pkg.all;    -- hex_string_to_unsigned
use work.psi_tb_txt_util.all;   -- hstr / to_string / print

entity hex_parse_demo is
end entity;

architecture sim of hex_parse_demo is
begin
    process
        variable u64 : unsigned(63 downto 0);
        variable l : line;
    begin
        -- 解析一个 64 位十六进制数（远超 integer 上限 2^31-1）
        u64 := hex_string_to_unsigned("DEADBEEF12345678", 64);

        -- 用 hstr 转回十六进制打印，应原样输出 DEADBEEF12345678
        print("hex  = 0x" & hstr(std_logic_vector(u64)));
        -- 用 to_string 打印无符号十进制，是一个约 1.6e19 的 20 位数
        -- （远超 integer 上限 2^31-1 = 2147483647，无法用任何 integer 字面量写出）
        print("dec  = " & to_string(u64));

        -- 反例：故意传一个非法字符，观察 severity failure 终止仿真
        -- 取消下行注释后会立即仿真失败：
        -- u64 := hex_string_to_unsigned("0xDEAD", 64);  -- 含 'x'，Illegal number

        wait;
    end process;
end architecture;
```

**需要观察的现象**：

1. 第一行打印 `hex = 0xDEADBEEF12345678`——与输入逐位吻合，证明 Horner 解析正确。
2. 第二行打印一个 20 位左右的十进制数（约 \(1.6 \times 10^{19}\)）——这个数远超 `integer` 上限（\(2^{31}-1 = 2\,147\,483\,647\)），你**无法**用任何 `integer` 字面量写出它，只能通过字符串绕过。
3. 取消注释那行非法输入后重新仿真，应在 Transcript 看到 `hex_string_to_unsigned: Illegal number` 并因 `severity failure` **立即停止**（区别于 compare 过程的 `severity error` 只打印不停）。

**预期结果**：前两行按上述打印；非法字符行触发 failure 终止。

> 待本地验证：具体十进制输出值请以本机仿真器实际打印为准（`to_string` 对 `unsigned` 输出无符号十进制，这是 [u2-l1](u2-l1-txt-util-conversions.md) 讲过的重载语义）。若你的 `to_string` 重载行为不同，可只核对 `hstr` 那一行。

#### 4.4.5 小练习与答案

**练习 1**：调用 `hex_string_to_unsigned("100", 16)` 得到的十进制值是多少？请用 Horner 法手算。

**答案**：\( ((0 \times 16 + 1) \times 16 + 0) \times 16 + 0 = 256 \)。十六进制 `100` 即十进制 256。

**练习 2**：想用 `decimal_string_to_signed` 得到 `-5`，能传 `"-5"` 吗？为什么？正确做法是什么？

**答案**：不能。函数只认 `'0'..'9'`，遇到 `'-'` 会 `report "...: Illegal number" severity failure` 终止仿真。负数没有十进制字符串入口；正确做法是用补码十六进制：对 8 位 `signed`，`-5` 的补码是 `FB`，应调 `hex_string_to_signed("FB", 8)`（结果即 `signed` 的 -5）。

**练习 3**：为什么 `wanted_bitwidth` 的类型是 `positive` 而不是 `integer`？

**答案**：位宽必须 $\geq 1$，写成 `positive`（`integer` 的子类型，范围 1 到 `integer'high`）让类型系统在编译期就拒绝 0 或负位宽，比运行期判错更安全。这也决定了结果向量 `unsigned(wanted_bitwidth - 1 downto 0)` 的下标不会出现非法范围。

## 5. 综合实践

把本讲四块地基串成一个完整的小任务：**声明一个 64 位数据的 AXI master/slave 信号对，用 init 把它拉回空闲，再用字符串函数把一个 64 位十六进制数“搬”到 `ms.wdata` 上，并打印校验**。

**任务要求**：

1. 声明 `signal ms : axi_ms_r(...)` 与 `signal sm : axi_sm_r(...)`，数据宽度 64 位（`wdata(63 downto 0)`、`rdata(63 downto 0)`），`wstrb(7 downto 0)`（8 字节使能），ID 与 user 信号自选一个合理宽度。注意把 4.1.3 列出的全部未约束字段都约束上。
2. 写一个 `process`，先调用 `axi_master_init(ms)` 和 `axi_slave_init(sm)`，`wait` 一会儿让赋值生效。
3. 用 `hex_string_to_unsigned("1234567890ABCDEF", 64)` 解析出一个 64 位值，赋给 `ms.wdata`（注意类型转换：`std_logic_vector(...)`）。
4. 用 `hstr(ms.wdata)` 打印出来，确认与输入一致；再核对 `ms.awvalid`/`sm.rvalid` 等 valid 信号在 init 之后确为 `'0'`。
5. 把 `wanted_bitwidth` 改成 32 重跑（即 `hex_string_to_unsigned("1234567890ABCDEF", 32)`），观察并解释 `hstr` 打印的结果（提示：`resize` 截断）。

**参考思路**（伪代码，具体 VHDL-2008 record 约束语法待本地验证）：

```vhdl
-- 1. 声明（VHDL-2008 record 子类型约束）
signal ms : axi_ms_r(arid(7 downto 0), araddr(63 downto 0), aruser(7 downto 0),
                     awid(7 downto 0), awaddr(63 downto 0), awuser(7 downto 0),
                     wdata(63 downto 0), wstrb(7 downto 0), wuser(7 downto 0));
signal sm : axi_sm_r(rid(7 downto 0), rdata(63 downto 0), ruser(7 downto 0),
                     bid(7 downto 0), buser(7 downto 0));

process
    variable big : unsigned(63 downto 0);
begin
    -- 2. 归零到空闲
    axi_master_init(ms);
    axi_slave_init(sm);
    wait for 10 ns;

    -- 3. 把一个 64 位十六进制数搬到 wdata 上
    big   := hex_string_to_unsigned("1234567890ABCDEF", 64);
    ms.wdata <= std_logic_vector(big);
    wait for 10 ns;

    -- 4. 校验
    print("wdata = 0x" & hstr(ms.wdata));   -- 期望 1234567890ABCDEF
    print("awvalid = " & std_logic'image(ms.awvalid));  -- 期望 '0'
    print("rvalid  = " & std_logic'image(sm.rvalid));   -- 期望 '0'
    wait;
end process;
```

**预期现象**：第 4 步打印 `wdata = 0x1234567890ABCDEF`；valid 信号均为 `'0'`。第 5 步把位宽改成 32 后，`hstr` 只打印低 32 位 `90ABCDEF`（因为 `resize` 把超出 32 位的高位截断了）——这正好印证 4.4.2 说的“`resize` 保证位宽恒定、超出部分丢弃”。

> 这个综合实践把本讲四块全部用上：record 类型（4.1）、init 把 valid 归零（4.3）、字符串解析任意位宽（4.4），并隐含用到 AxSIZE 概念（4.2，因为 `wstrb` 8 位对应 `AxSIZE_8_c`）。它也是 [u5-l2 单次事务](u5-l2-axi-single-transactions.md) 的天然前奏——下一讲你就会看到 `axi_single_write` 如何自动完成“init → 驱动地址 → 驱动数据 → 收响应 → 校验 OKAY”这一整串动作。

## 6. 本讲小结

- psi_tb 用两条 record `axi_ms_r` / `axi_sm_r` 把 ~40 个 AXI 信号**按“谁驱动”**分成两束：主机驱动的（含 `rready`/`bready`）都在 `axi_ms_r`，从机驱动的都在 `axi_sm_r`；这让每个 BFM 过程的参数表只剩 `ms`/`sm` 两个总线参数。
- 这两条 record 含 **VHDL-2008 未约束字段**（地址/数据/ID/user/strobe 等），宽度在使用端声明信号时指定；BFM 内部则用 `xxx'length` 自适应。
- 三组命名常量 `xRESP_*`（响应码）、`xBURST_*`（突发类型）、`AxSIZE_*`（每拍字节数，编码满足 \( \text{bytes}=2^{\text{AxSIZE}} \)）给协议编码起了可读名字，是后续比较与事务过程的语义基础。
- `axi_master_init` / `axi_slave_init` 是 `signal ... out` 过程，把整束信号归零、valid/ready 全部拉低；它们不仅“开机调用一次”，更在每个 `axi_apply_*` 事务握手成功后被**重新调用**以撤销 valid，是 BFM 的“回空闲”原语。
- 四个 `decimal/hex_string_to_*` 函数用 Horner 法（逐字符“乘基加值”、`resize` 截位）把字符串解析成任意位宽的 `signed`/`unsigned`，专门解决 **VHDL `integer` 只有 32 位、装不下 64+ 位 AXI 数据**的问题；它们**不解析正负号**，负数只能用补码十六进制传入。
- 本讲的类型/常量/init/字符串函数本身不打印 `###ERROR###`，但它们是后续 `axi_single_*` / `axi_apply_*` / `axi_expect_*` 全部事务过程的地基——只有先搭好这块地基，下一讲的“单次读写 + 自动校验 OKAY”才能跑起来。

## 7. 下一步学习建议

- 接下来学 **[u5-l2 AXI 单次事务：single_write / read / expect](u5-l2-axi-single-transactions.md)**。它会把你今天认识的 `axi_ms_r`/`axi_sm_r`、`xRESP_OKAY_c`、`axi_master_init` 串成一个完整过程：`axi_single_write` 如何驱动 AW→W→B、如何在末尾用 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)` 自动校验响应；`axi_single_expect` 又如何“读后自动比较”。你会看到本讲的字符串函数（`hex_string_to_signed`）如何被 string 重载调用，从而支持 64 位写值。
- 想提前看“多拍突发”与“apply/expect 分工”，可继续到 **[u5-l3 AXI 部分事务与突发传输](u5-l3-axi-partial-and-burst.md)**，那里会大量用到本讲的 `xBURST_*` / `AxSIZE_*` 常量与 init 的“回空闲”复用模式。
- 如果你对“本 package 如何与综合侧的 psi_common AXI 类型对接”感兴趣，可以跳读 **[u5-l4 TB 与综合 AXI 类型互转](u5-l4-axi-conversion.md)**，看 `axi_ms_r`/`axi_sm_r` 的每个字段如何逐个映射到 `axi_slv_inp`/`axi_slv_oup`。
- 建议同时打开源码 [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd)，把本讲引用过的行号（24–40、42–100、102–117、339–465、467–517）对照读一遍，确认每个结论都能在源码里指到具体那一行——这是后续讲义反复引用本 package 时的“坐标”。
