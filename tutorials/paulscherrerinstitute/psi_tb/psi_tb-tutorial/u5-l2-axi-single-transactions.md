# AXI 单次事务：single_write / read / expect

## 1. 本讲目标

学完本讲，你应该能够：

- 把 `axi_single_write` 的执行过程拆成「同步 → 驱动 AW → 握手 → 驱动 W → 握手 → 收 B → 校验 bresp」七步，并说清它在末尾用 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)` 自动把关——只要从机回的不是 OKAY，就自动打印 `###ERROR###`，CI 随之判失败。
- 区分 `axi_single_write` 的两个重载：整数重载用 `to_signed(value, ...)` 把整数搬上数据线（故负数以补码写入）；字符串重载用 `base`（10/16）选择 `decimal_string_to_signed` / `hex_string_to_signed`，从而能写超过 32 位的值。
- 讲清 `axi_single_read` 的 `msb` / `lsb` / `sex` 三个参数如何「先从 `rdata` 截取窗口 [msb:lsb]、右移到第 0 位、再按 `sex` 决定零扩展或符号扩展」，并能用一个具体数值算出读回的整数。
- 解释 `axi_single_expect` 是「`axi_single_read` + 一次比较」的组合：整数重载用 `IntCompare`（受 32 位整数限制），字符串重载用 `SignCompare2`（按 nibble 显示十六进制、支持任意位宽）——并理解为什么字符串重载内部调的是 `axi_single_read` 的 **signed** 重载。
- 看懂「响应错误」与「数据不符」是两条独立的报错路径：前者由 `StdlvCompareStdlv` 抓 `rresp`/`bresp`，后者由 `IntCompare`/`SignCompare2` 抓读回值；它们都沿用 `###ERROR###` 前缀，都被 CI 的 `run_check_errors "###ERROR###"` 统一捕获。

## 2. 前置知识

本讲是 [u5-l1 AXI 类型、常量、初始化与字符串转换](u5-l1-axi-types-and-init.md) 的直接续集——那一讲搭好的「地基」本讲全部用上。请先确认你已经了解：

- **`axi_ms_r` / `axi_sm_r` 两条 record**（[u5-l1](u5-l1-axi-types-and-init.md)）：主机驱动的信号（含 `rready`/`bready`）在 `axi_ms_r`，从机驱动的在 `axi_sm_r`。本讲的三个过程参数表都只有 `signal ms : out axi_ms_r`（我驱动）与 `signal sm : in axi_sm_r`（我观察）两个总线参数——这种简洁正是 record 化带来的。
- **三组常量与 AxSIZE 编码**（[u5-l1](u5-l1-axi-types-and-init.md)）：`xRESP_OKAY_c = "00"` 是 BFM 眼里的「成功」；`AxSIZE` 满足 \( \text{bytes} = 2^{\text{AxSIZE}} \)。本讲会看到 `axi_single_write`/`read` 用 `log2(ms.wstrb'length)` 由数据宽度**反算** AxSIZE，自动填到 `awsize`/`arsize`。
- **字符串转任意位宽数值**（[u5-l1](u5-l1-axi-types-and-init.md)）：`decimal_string_to_signed` / `hex_string_to_signed` 用 Horner 法把字符串解析成任意宽 `signed`，绕开 VHDL `integer` 只有 32 位的限制。本讲的 string 重载正是它们的「主战场」。
- **比较包的复用与 32 位天花板**（[u3-l1 基础比较过程](u3-l1-compare-basic.md)、[u3-l2 signed/unsigned 比较与容差](u3-l2-compare-signed-unsigned.md)）：`StdlvCompareStdlv` 做位精确比较、消息含二进制+十六进制；`IntCompare` 比较整数、受 32 位限制；`SignCompare2` 比较 `signed`、用 `hstr` 显示十六进制从而**不受 32 位拖累**。本讲三个过程的「自动校验」全部建立在这三个过程上。
- **`###ERROR###` 前缀与 CI 联动**（[u1-l3 仿真环境与 CI 构建流程](u1-l3-simulation-and-ci.md)）：比较过程默认前缀 `###ERROR###: `，`severity error` 只打印不中断仿真，由 `run_check_errors "###ERROR###"` 在仿真末尾扫描。所以本讲里任何一次「响应错」或「数据不符」都会自动变成 CI 失败。

> 一个贯穿全讲的直觉：psi_tb 把一次 AXI 单拍事务（single transfer，即 `len=0`、只传 1 拍）的全部握手细节封装进**一个过程调用**。你在 testbench 里只需写 `axi_single_write(地址, 数据, ms, sm, clk)`，BFM 就替你完成「驱动地址、等 ready、驱动数据、等 ready、收响应、校验响应码」这一长串动作。本讲要回答的核心问题是：**这一长串动作在源码里到底是怎么编排的？哪里会自动报错？**

## 3. 本讲源码地图

本讲只涉及一个源文件，但会反复跳到它依赖的比较包去印证「报错从哪里来」：

| 文件 | 作用 |
| --- | --- |
| [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd) | AXI 总线功能模型包。本讲精读其中 6 个过程：`axi_single_write` 的整数重载（实现第 519–550 行）与字符串重载（第 552–592 行）；`axi_single_read` 的整数重载（第 594–630 行）与 signed 重载（第 632–668 行）；`axi_single_expect` 的整数重载（第 670–686 行）与字符串重载（第 688–716 行）。声明集中第 124–177 行。 |
| [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) | 比较包。本讲引用其中三个过程：`StdlvCompareStdlv`（第 143–156 行，校验响应码）、`IntCompare`（第 197–209 行，整数 expect 用）、`SignCompare2`（第 242–254 行，字符串 expect 用）。这三个过程是本讲所有 `###ERROR###` 的真正发源地。 |

AXI 包在头部 `use work.psi_tb_compare_pkg.all`（[hdl/psi_tb_axi_pkg.vhd:16](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L16)），所以下面这些比较过程能「裸名」调用，不必加包名前缀。

本讲三个最小模块与源码行号的对照：

| 模块 | 关键过程 | 源码位置 |
| --- | --- | --- |
| `axi_single_write`（integer / string 重载） | `axi_single_write` × 2 | 声明 125–136、实现 519–592 |
| `axi_single_read`（integer / signed 重载） | `axi_single_read` × 2 | 声明 138–154、实现 594–668 |
| `axi_single_expect`（integer / string 重载） | `axi_single_expect` × 2 | 声明 156–177、实现 670–716 |

> 注意：本仓库**没有**注册使用 `axi_single_*` 的 testbench（`sim/config.tcl` 当前只编译了 I2C 包的 TB），所以本讲没有「现成可跑的样例 TB」。第 4 节的代码实践会给出一段**标注为「示例代码」**的最小 AXI slave + master 演示，并在不能确定运行结果的环节明确标注「待本地验证」；此外另附一个**不依赖运行**的「源码阅读型实践」作为保底。

## 4. 核心概念与源码讲解

### 4.1 `axi_single_write`：把一次单拍写事务封装成一个调用

#### 4.1.1 概念说明

AXI 的一次「写」要跨**三个通道**、做**三次握手**：

1. **AW（写地址）通道**：主机把地址放到 `awaddr`、拉高 `awvalid`，等从机拉高 `awready`，二者在同一上升沿同时为高时地址被「收下」。
2. **W（写数据）通道**：主机把数据放到 `wdata`、字节使能放到 `wstrb`、拉高 `wvalid`（单拍事务还要拉高 `wlast` 表示「这是最后一拍」），等从机拉高 `wready`，数据被收下。
3. **B（写响应）通道**：从机拉高 `bvalid` 并给出响应码 `bresp`，等主机拉高 `bready`，响应被收下。主机据此判断「这次写到底成没成」。

如果你在 testbench 里手写这三段握手，每个写操作都要重复十几行 `wait until rising_edge(clk) and ... ready = '1'` 的样板代码，极易写错（比如忘了拉低 `awvalid`，从机就会以为又要写一次）。`axi_single_write` 把这一切收进**一个过程调用**：

```vhdl
axi_single_write(address => 0, value => 16#CAFE#, ms => ms, sm => sm, clk => clk);
```

调用返回时，三段握手已全部完成，并且响应码已被自动校验——如果从机回的不是 OKAY，Transcript 里就会自动出现一行 `###ERROR###`。它有两个重载，区别只在「数据怎么给」：

- **整数重载**（[hdl/psi_tb_axi_pkg.vhd:125-129](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L125-L129)）：`value : in integer`，方便写 32 位以内的值。
- **字符串重载**（[hdl/psi_tb_axi_pkg.vhd:131-136](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L131-L136)）：`value : in string; base : in integer`，用 `base=10` 或 `16` 表达超过 32 位的值。

#### 4.1.2 核心流程

两个重载的握手骨架**完全相同**，只在「`wdata` 如何取值」这一步不同。整数重载的流程是：

```
1. wait until rising_edge(clk)              -- 对齐到一个上升沿（同步起点）
2. 驱动 AW 通道：
     awid    <= 0
     awaddr  <= address（to_unsigned）
     awlen   <= 0                           -- 单拍：len=0（共 1 拍）
     awsize  <= log2(wstrb'length)          -- 由数据宽度自动算 AxSIZE
     awburst <= "01"                        -- INCR
     awvalid <= '1'
3. wait until rising_edge(clk) and sm.awready='1'   -- 等地址被收下
     awvalid <= '0'                         -- 立刻撤销地址 valid
4. 驱动 W 通道：
     wdata   <= to_signed(value, wdata'length)      -- ★ 整数重载用 to_signed
     wstrb   <= to_signed(-1, wstrb'length)         -- 全 1：所有字节都有效
     wlast   <= '1'                         -- 单拍即末拍
     wvalid  <= '1'
5. wait until rising_edge(clk) and sm.wready='1'    -- 等数据被收下
     wlast <= '0'; wvalid <= '0'; bready <= '1'     -- 撤销 W，开始收 B
6. wait until rising_edge(clk) and sm.bvalid='1'    -- 等响应到来
     bready <= '0'
7. StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp,        -- ★ 自动校验响应码
                     "axi_single_write(): received negative response!")
```

字符串重载只有第 4 步不同：它用 `case base is when 10 => decimal_string_to_signed(...); when 16 => hex_string_to_signed(...); when others => report ... severity failure`（[hdl/psi_tb_axi_pkg.vhd:571-578](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L571-L578)）把字符串解析成 `signed` 再搬上 `wdata`。

这里有三处细节值得记住（后面源码精读与练习都会用到）：

- **AxSIZE 自动算**：`awsize <= to_unsigned(log2(ms.wstrb'length), ms.awsize'length)`。因为 `wstrb` 每一位对应一个字节（[u5-l1](u5-l1-axi-types-and-init.md) 讲过的 `wstrb'length = wdata'length/8`），所以 `log2(wstrb'length)` 正是 AxSIZE 数值。32 位数据 → `wstrb` 4 位 → `log2(4)=2` → `AxSIZE_4_c`。
- **写所有字节**：`wstrb <= to_signed(-1, ms.wstrb'length)`。`to_signed(-1, N)` 是 N 位全 1，即「每个字节都有效」，所以 `axi_single_write` 总是整字写，**不支持只写部分字节**（那是 `axi_apply_wd_single` 的活，见 [u5-l3](u5-l3-axi-partial-and-burst.md)）。
- **不调用 `axi_master_init`**：与 `axi_apply_*` 系列不同，`axi_single_write` 在事务结束后**没有**调用 init（对比 [hdl/psi_tb_axi_pkg.vhd:732](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L732) 的 `axi_apply_aw` 末尾调了 init）。它是手动把 `awvalid`/`wvalid`/`bready` 拉低的——足以保证「不悬空 valid」，但 `awaddr`/`wdata`/`wstrb` 这些载荷字段会**保留上次的值**。功能上无害（下一次 single_write 会重写它们），但读源码时要意识到这一区别。

#### 4.1.3 源码精读

先看整数重载的完整实现：

[hdl/psi_tb_axi_pkg.vhd:519-550](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L519-L550) —— `axi_single_write`（整数重载）。逐段对应 4.1.2 的流程：

- **同步起点**：[第 526 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L526) `wait until rising_edge(clk)`，确保后续驱动都落在时钟沿上。
- **驱动 AW**：[第 528–533 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L528-L533) 设置 `awid`/`awaddr`/`awlen`/`awsize`/`awburst`/`awvalid`。其中 `awaddr` 用 `to_unsigned(address, ms.awaddr'length)`（地址按**无符号**处理），`awsize` 用 `log2(ms.wstrb'length)` 自动算。
- **AW 握手**：[第 535 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L535) 等地址被收下，[第 536 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L536) 拉低 `awvalid`。
- **驱动 W**：[第 537–540 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L537-L540)——这是整数重载的「特征行」：`wdata <= std_logic_vector(to_signed(value, ms.wdata'length))`。`to_signed` 意味着**负整数会以补码写入**（写 `-1` 就是全 1）。`wstrb <= to_signed(-1, ms.wstrb'length)` 是全 1（所有字节有效）。
- **W 握手 → 收 B**：[第 542–548 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L542-L548)，先等 `wready`、再置 `bready<=1`、再等 `bvalid`。
- **响应校验**：[第 549 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549) `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, "axi_single_write(): received negative response!")`——这就是「自动把关」：期望响应是 `xRESP_OKAY_c`（`"00"`），实际是 `sm.bresp`，不等就打印 `###ERROR###`。

再看字符串重载与整数重载的**唯一差别**（W 通道取值那几行）：

[hdl/psi_tb_axi_pkg.vhd:571-578](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L571-L578) —— `case base` 选择解析器：`base=10` 调 `decimal_string_to_signed(value, ms.wdata'length)`，`base=16` 调 `hex_string_to_signed(value, ms.wdata'length)`，其它值 `report "... unsupported base value" severity failure` 直接终止仿真。两个解析器都是 [u5-l1](u5-l1-axi-types-and-init.md) 讲过的 Horner 法实现，能表达超过 32 位的数据。字符串重载的其余行（AW 握手、W 握手、收 B、[第 591 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L591) 的响应校验）与整数重载逐字相同。

最后看「自动把关」用的那个比较过程到底干了什么：

[hdl/psi_tb_compare_pkg.vhd:143-156](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L143-L156) —— `StdlvCompareStdlv`：`assert Actual_c = Expected_c report Prefix & Msg & " [Expected ... Received ...]" severity error`。前缀 `Prefix` 默认 `"###ERROR###: "`，所以一旦 `sm.bresp ≠ "00"`，Transcript 就会出现形如

```
###ERROR###: axi_single_write(): received negative response! [Expected 00(0x0), Received 10(0x2)]
```

的消息（`10` 即 SLVERR）。`severity error` 不中断仿真，所以即使连续多次写都失败，每次失败都会各打印一行，最后被 CI 的 `run_check_errors "###ERROR###"` 统一捕获。

#### 4.1.4 代码实践

**实践目标**：确认「响应码不是 OKAY 时，错误消息从哪一行产生、长什么样」——这是一段**源码阅读型实践**，无需运行仿真即可完成，是后续「故意返回 SLVERR」实践的对照基准。

**操作步骤**：

1. 打开 [hdl/psi_tb_axi_pkg.vhd:549](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549)，看到整数重载写事务末尾的 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)`。
2. 跳到 `StdlvCompareStdlv` 的实现 [hdl/psi_tb_compare_pkg.vhd:143-156](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L143-L156)，确认它的 `report` 串拼接顺序：前缀 → Msg → `[Expected <二进制>(0x<十六进制>), Received <二进制>(0x<十六进制>)]`。
3. 据此**手写**出「从机返回 SLVERR（`"10"`）」时 Transcript 应当出现的完整消息文本（用 `str`/`hstr` 的「MSB 在左」规则把 `"00"` 与 `"10"` 展开）。

**需要观察的现象**：你手写的消息里，Expected 部分是 `00(0x0)`、Received 部分是 `10(0x2)`，且整行以 `###ERROR###: ` 开头。

**预期结果**：手写文本与下面一致——

```
###ERROR###: axi_single_write(): received negative response! [Expected 00(0x0), Received 10(0x2)]
```

> 这是「不依赖运行」的保底实践。第 5 节的综合实践会给出一段可在仿真器里跑、能真机观察到这行消息的示例 TB（标注「待本地验证」）。

#### 4.1.5 小练习与答案

**练习 1**：用整数重载 `axi_single_write(addr, -1, ms, sm, clk)` 在 32 位数据总线上写值，`wdata` 上实际会出现什么？

**答案**：全 1（`0xFFFFFFFF`）。因为整数重载用 `to_signed(value, ms.wdata'length)`（[第 537 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L537)），`-1` 的 32 位补码就是全 1。

**练习 2**：`axi_single_write` 在事务结束后没有调用 `axi_master_init`。这意味着 `ms.awaddr` 在事务结束后是什么值？这会造成功能问题吗？

**答案**：`ms.awaddr` 保留本次写入的地址（因为只拉低了 `awvalid`，没清 `awaddr`）。不会造成功能问题——AXI 握手只看 `awvalid`/`awready`，`awvalid` 已为 0，从机不会把残留的 `awaddr` 当成新事务；且下一次 `axi_single_write` 会重写 `awaddr`。这与 `axi_apply_aw` 末尾调用 init（[第 732 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L732)）是两种不同风格：single 系列「自包含、只管 valid」，apply 系列「每段结束后整体归零」。

**练习 3**：为什么字符串重载的 `case base` 里，`when others` 用的是 `severity failure`（[第 577 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L577)），而不是像响应校验那样用 `severity error`？

**答案**：传了不支持的 `base`（既不是 10 也不是 16）属于 **testbench 本身写错了**，没有继续跑的意义，所以用 `failure` 立即终止仿真；而响应码不符是 **DUT 行为不符合预期**，应当跑完全部用例再统一由 CI 判定，所以用 `error` 只打印不停。这与 [u5-l1](u5-l1-axi-types-and-init.md) 里字符串解析函数遇到非法字符用 `failure` 是同一套约定。

---

### 4.2 `axi_single_read`：读回数据，并用 msb/lsb/sex 截取与解释

#### 4.2.1 概念说明

AXI 的一次「读」跨**两个通道**、做**两次握手**：

1. **AR（读地址）通道**：主机给 `araddr`、拉高 `arvalid`，等从机 `arready`，地址被收下。
2. **R（读数据）通道**：从机拉高 `rvalid`、把数据放到 `rdata`、响应码放到 `rresp`（单拍还要拉高 `rlast`），等主机 `rready`，数据被收下。

`axi_single_read` 同样把这两段握手收进一个调用，但它比 write 多出一个**独有问题**：读回来的 `rdata` 是**整条数据总线宽**（比如 32 位），而你往往只关心其中一段（比如某个 8 位寄存器占的 [7:0]，或某个 16 位字段占的 [23:8]），还想选择「把这段当无符号还是当有符号解释」。于是它提供三个参数：

| 参数 | 类型 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `msb` | `natural` | 31 | 截取窗口的最高位（在 `rdata` 中的下标） |
| `lsb` | `natural` | 0 | 截取窗口的最低位 |
| `sex` | `boolean` | `false` | `true`=符号扩展（把窗口最高位填到所有高位），`false`=零扩展 |

它也有两个重载，区别只在「读回值返回成什么类型」：

- **整数重载**（[hdl/psi_tb_axi_pkg.vhd:138-145](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L138-L145)）：`value : out integer`，返回 32 位整数。
- **signed 重载**（[hdl/psi_tb_axi_pkg.vhd:147-154](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L147-L154)）：`value : out signed`，返回与数据线等宽的 `signed`，可承载超过 32 位的数据。

#### 4.2.2 核心流程

两个重载的握手骨架相同，且与 write 的 AW 段高度对称（只是把 AW 换成 AR、把 W 换成 R）。差别在**数据回收**那几行——这是本模块的重点。以整数重载为例，握手完成后做如下「截取 + 移位 + 扩展」：

```
-- 握手：同步 → 驱动 AR → 等 arready → 撤 arvalid → 置 rready → 等 rvalid → 撤 rready
valueStdlv := sm.rdata                                   -- 复制整条读回数据
valueStdlv(msb-lsb downto 0) := valueStdlv(msb downto lsb)   -- 把窗口[msb:lsb]右移到第 0 位
if sex then
    valueStdlv(高位 downto msb-lsb+1) := (others => valueStdlv(msb))  -- 用窗口最高位符号扩展
else
    valueStdlv(高位 downto msb-lsb+1) := (others => '0')              -- 零扩展
end if
value := to_integer(signed(valueStdlv))                  -- 整数重载：转 integer
StdlvCompareStdlv(xRESP_OKAY_c, sm.rresp, "... negative response!")  -- 顺手校验 rresp
```

signed 重载只有最后一步不同：`value := signed(valueStdlv)`（[第 666 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L666)），不转 `integer`，因此**不受 32 位整数限制**。

**用一个具体数值把 msb/lsb/sex 算清楚**。设数据线 32 位，从机读回 `rdata = 0x00000080`（即 bit7=1，其余为 0）。你想读「bit 7」这个标志，于是想看 [7:0] 这一字节。

- 调用 `axi_single_read(addr, v, ms, sm, clk, msb=>7, lsb=>0, sex=>false)`：
  - `valueStdlv(7 downto 0) := valueStdlv(7 downto 0)` → 不移位，低 8 位仍是 `0x80`；
  - 零扩展：高位 [31:8] 填 0 → `valueStdlv = 0x00000080`；
  - `to_integer(signed(...))` → **+128**。
- 改成 `sex=>true`：
  - 符号扩展：高位 [31:8] 全部填 `valueStdlv(7)`=1 → `valueStdlv = 0xFFFFFF80`；
  - `to_integer(signed(...))` → **-128**（因为 `0xFFFFFF80` 是 -128 的 32 位补码）。

可见 `sex` 决定了「同一个位模式被解释成无符号还是有符号」。这正是它的用途：读「带符号的定点数」字段时打开 `sex`，读「纯无符号计数值」字段时关闭 `sex`。

> 默认 `msb=31, lsb=0` 时，`msb-lsb = 31`，于是「窗口右移」写成 `valueStdlv(31 downto 0) := valueStdlv(31 downto 0)`（不动），而「高位填充」的范围 `ms.wdata'length-1 downto msb-lsb+1` = `31 downto 32` 是个**空范围**（不填任何位）——所以整条 `rdata` 原样保留，这正是「读整字」的退化情形。

#### 4.2.3 源码精读

先看整数重载的「数据回收」三行，这是本模块最值得逐字读的代码：

[hdl/psi_tb_axi_pkg.vhd:620-629](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L620-L629) —— 截取、移位、扩展、转整数、校验响应：

- [第 621 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L621) `valueStdlv := sm.rdata`——先把整条读回数据复制到一个**局部 variable**（声明在 [第 602 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L602)：`variable valueStdlv : std_logic_vector(ms.wdata'length - 1 downto 0)`，宽度与数据线一致）。注意这是 `:=`（变量赋值，立即生效），不是 `<=`。
- [第 622 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L622) `valueStdlv(msb - lsb downto 0) := valueStdlv(msb downto lsb)`——把窗口 [msb:lsb] 搬到 [msb-lsb:0]，等效于「把窗口右移 lsb 位」。这一行只写了低位段，**窗口原位置的值并未被擦除**（这对下一步符号扩展很关键）。
- [第 623–627 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L623-L627) 的 `if sex`：符号扩展时填 `(others => valueStdlv(msb))`——注意读的是 `valueStdlv(msb)`，即**窗口原来的最高位**（上一行没动它），用它作为符号位去填充所有更高位；零扩展时填 `(others => '0')`。
- [第 628 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L628) `value := to_integer(signed(valueStdlv))`——转成整数输出。
- [第 629 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L629) `StdlvCompareStdlv(xRESP_OKAY_c, sm.rresp, "axi_single_read(): received negative response!")`——和 write 一样，自动校验读响应码。

再看握手段（与 write 的 AW 段对称）：

[hdl/psi_tb_axi_pkg.vhd:604-619](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L604-L619) —— 同步、驱动 AR（`araddr` 用 `to_unsigned`、`arsize` 同样由 `log2(ms.wstrb'length)` 自动算、`arburst<= "01"`）、等 `arready`、置 `rready`、等 `rvalid`、撤 `rready`。

最后看 signed 重载与整数重载的差别——只有「转出」那一步：

[hdl/psi_tb_axi_pkg.vhd:659-667](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L659-L667) —— 数据回收三行与整数重载**逐字相同**（同样的截取/移位/扩展），只是 [第 666 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L666) 改成 `value := signed(valueStdlv)`（返回 `signed`，不调 `to_integer`），[第 667 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L667) 同样校验 `rresp`。这一处差别是 `axi_single_expect` 字符串重载能处理 >32 位数据的根因（见 4.3）。

> 一个常被忽略的点：响应校验在 [第 629/667 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L629) 发生在「值已经赋给 `value`」**之后**。也就是说，即便 `rresp` 报错，`value` 仍会被填上（可能是垃圾）并返回。设计上这是「能报多少报多少」：一次读既可能触发「响应错」，后续的 `expect` 又会触发「数据错」，两行 `###ERROR###` 都会被打印出来。

#### 4.2.4 代码实践

**实践目标**：用一个具体 `rdata` 手算「截取 + 移位 + 扩展」的结果，验证你对 `msb`/`lsb`/`sex` 的理解与源码行为一致。这是**纸笔型实践**，无需运行。

**操作步骤**：

1. 设数据线 32 位，从机读回 `rdata = 0x00ABCD00`（即 [23:8] = `0xABCD`，其余字节为 0）。本例对应一个 16 位字段占据 [23:8]。
2. 调用 `axi_single_read(addr, v, ms, sm, clk, msb=>23, lsb=>8, sex=>false)`，按 [第 621–628 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L621-L628) 的逻辑手算 `valueStdlv` 与最终 `value`。
3. 再算一次 `sex=>true` 的情形。

**需要观察的现象**：

- `sex=>false`：窗口 [23:8]=`0xABCD` 右移到 [15:0]，高位 [31:16] 填 0 → `0x0000ABCD` → `value = 0xABCD = 43981`。
- `sex=>true`：窗口最高位 bit23（`0xAB` 的最高位 = 1）扩展到 [31:16] → `0xFFFFABCD` → `to_integer(signed(...))` → `-21555`（因为 `0xFFFFABCD` 是 -21555 的补码；也可直接由 `0xABCD` 的 16 位有符号值 = \(- (2^{16}-0xABCD)\) = \(-21555\) 得到）。

**预期结果**：`sex=false` 得 +43981，`sex=true` 得 -21555。两者**同一个位模式、不同的解释**——这就是 `sex` 的全部作用。

> 待本地验证：如想真机核对，可在示例 TB（第 5 节）里让从机在 `rdata` 上回 `0x00ABCD00`，分别用 `sex=>false/true` 各读一次并用 `print` 打印 `value`，与上面的手算值比对。

#### 4.2.5 小练习与答案

**练习 1**：`axi_single_read` 的 `arsize` 是怎么填的？为什么不交给调用者指定？

**答案**：`arsize <= to_unsigned(log2(ms.wstrb'length), ms.arsize'length)`（[第 610 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L610)），由数据线宽度自动算。因为 single 事务是「整字读」，每拍字节数就等于数据线的字节数 `wstrb'length`，没有让调用者指定的必要——这也避免了「调用者填错 AxSIZE」的风险。

**练习 2**：signed 重载为什么用 `value : out signed` 而不是 `value : out integer`？它解决了整数重载的什么短板？

**答案**：因为 `integer` 只有 32 位有符号，装不下超过 32 位的读回值（比如 64 位数据线读回的大数）。signed 重载返回与数据线等宽的 `signed`（[第 666 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L666) 用 `signed(valueStdlv)` 而非 `to_integer`），所以能承载任意位宽。这正是 `axi_single_expect` 字符串重载内部要调 signed 重载、而不是整数重载的原因。

**练习 3**：若从机读回时 `rresp` 返回了 SLVERR，`axi_single_read` 会怎样？返回的 `value` 可信吗？

**答案**：会在 [第 629/667 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L629) 打印一行 `###ERROR###: axi_single_read(): received negative response!`，但**不中断仿真**。`value` 仍按 `rdata` 计算并返回，但因为事务出错，`rdata` 很可能是垃圾，所以 `value` 不可信。通常你会紧接着用 `axi_single_expect`，于是「响应错」与「数据错」会各报一行——这正是 CI 能同时抓到两类问题的原因。

---

### 4.3 `axi_single_expect`：读后自动比较的「断言式」用法

#### 4.3.1 概念说明

`axi_single_read` 把值读回来后，你通常还要自己写一行比较，例如：

```vhdl
axi_single_read(0, v, ms, sm, clk);
IntCompare(16#CAFE#, v, "reg0 check");   -- 自己手动比较
```

`axi_single_expect` 把这两步**合并**成一个调用——「读指定地址，并与期望值自动比较，不符就报 `###ERROR###`」。它的定位类似软件测试里的断言（assert）：你声明「我期望这个寄存器现在是 0xCAFE」，BFM 替你读回来核对。调用形如：

```vhdl
axi_single_expect(0, 16#CAFE#, ms, sm, clk, name => "reg0 after write");
```

它也有两个重载，区别在「期望值怎么给、用什么过程比较」：

| 重载 | 期望值类型 | 内部调的读 | 内部调的比较 | 适用 |
| --- | --- | --- | --- | --- |
| 整数（[L156-165](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L156-L165)） | `integer` | `axi_single_read`（整数重载） | `IntCompare` | 32 位以内的期望值 |
| 字符串（[L167-177](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L167-L177)） | `string` + `base` | `axi_single_read`（**signed** 重载） | `SignCompare2` | 超过 32 位、或想看十六进制错误消息 |

两个重载都把 `msb`/`lsb`/`sex`/`tol`（容差）一路透传给底层的 read 与 compare，所以 expect 同样支持「只比较某个字段」「带符号解释」「带容差比较」。

#### 4.3.2 核心流程

两个重载的实现都极短，本质都是「先读、再比」：

整数重载：

```
axi_single_read(address, val, ms, sm, clk, msb, lsb, sex)          -- 读回整数 val
IntCompare(value, val, "axi_single_expect() received unexpected result - " & name, tol)  -- 比较
```

字符串重载：

```
axi_single_read(address, val, ms, sm, clk, msb, lsb, sex)          -- val 是 signed
case base is when 10 => tmp := decimal_string_to_signed(value, ...);
              when 16 => tmp := hex_string_to_signed(value, ...);
              when others => report ... severity failure
SignCompare2(tmp, val, "axi_single_expect() received unexpected result - " & name, tol)  -- 比较
```

两条关键设计决策：

1. **字符串重载内部调的是 signed 重载的 read**（不是整数重载）。这是因为字符串期望值可能超过 32 位，必须用能承载任意位宽的 `signed` 通路读回，再用同样吃 `signed` 的 `SignCompare2` 比较。这是一个「同构选择」：>32 位的值从读到比一路都走 `signed`。
2. **两个重载用的比较过程不同**，因而错误消息形态不同：
   - 整数重载用 `IntCompare`，消息是十进制 `[Expected <dec>, Received <dec>, Tolerance <dec>]`，但受 32 位整数限制（[u3-l1](u3-l1-compare-basic.md)）。
   - 字符串重载用 `SignCompare2`，消息是十六进制 `[Expected 0x<hex>, Received 0x<hex>, Tolerance <dec>]`，按 nibble 显示、**支持任意位宽**（[u3-l2](u3-l2-compare-signed-unsigned.md)）——这正是 [u3-l2](u3-l2-compare-signed-unsigned.md) 里讲过的「SignCompare2 专为 >32 位数据而生」的落点。

> `name` 参数（默认 `"No Msg"`）会被拼进错误消息。在长 testbench 里给每次 expect 起个名字（如 `"reg0 after write"`、`"ch1 gain"`），失败时就能一眼定位是哪一次检查挂了——这和 [u3-l2](u3-l2-compare-signed-unsigned.md) 里 `IndexString` 给循环比较加下标是同一个可读性诉求。

#### 4.3.3 源码精读

整数重载只有两行「真逻辑」：

[hdl/psi_tb_axi_pkg.vhd:670-686](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L670-L686) —— `axi_single_expect`（整数重载）：

- [第 684 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L684) `axi_single_read(address, val, ms, sm, clk, msb, lsb, sex)`——把 `msb`/`lsb`/`sex` 原样透传。`val` 是 [第 680 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L680) 声明的 `variable val : integer`。注意这里实参 `val` 是 integer，VHDL 会按类型解析到 `axi_single_read` 的**整数重载**。
- [第 685 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L685) `IntCompare(value, val, "axi_single_expect() received unexpected result - " & name, tol)`——比较期望 `value` 与读回 `val`，容差 `tol`。消息里拼了 `name`。

字符串重载稍长，但结构同样是「读 + 解析期望值 + 比较」：

[hdl/psi_tb_axi_pkg.vhd:688-716](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L688-L716) —— `axi_single_expect`（字符串重载）：

- [第 699 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L699) `variable val : signed(ms.wdata'length - 1 downto 0)`——期望值通路是 `signed`，与数据线等宽。
- [第 702 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L702) `axi_single_read(address, val, ms, sm, clk, msb, lsb, sex)`——因为 `val` 是 `signed`，VHDL 解析到 `axi_single_read` 的 **signed 重载**（[第 632 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L632)）。这是「>32 位数据从读到比全程走 signed」的关键一步。
- [第 703–710 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L703-L710) `case base`：把字符串期望值解析成 `signed`（`10` 用 `decimal_string_to_signed`、`16` 用 `hex_string_to_signed`），非法 base 仍 `severity failure`。
- [第 711–715 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L711-L715) `SignCompare2(Expected => tmpValSign_v, Actual => val, Msg => "axi_single_expect() received unexpected result - " & name, Tolerance => tol)`——用 `SignCompare2` 比较，错误消息按十六进制显示，不受 32 位限制。

最后对照两个比较过程的「报错长相」，体会为什么要分而治之：

- `IntCompare`（[hdl/psi_tb_compare_pkg.vhd:197-209](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L197-L209)）：消息 `[Expected <十进制>, Received <十进制>, Tolerance <十进制>]`。
- `SignCompare2`（[hdl/psi_tb_compare_pkg.vhd:242-254](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L242-L254)）：消息 `[Expected 0x<十六进制>, Received 0x<十六进制>, Tolerance <十进制>]`，用 `hstr` 按 nibble 映射，可显示任意位宽。

> 于是同一个「期望 0xCAFE、实际读到 0xBABE」的不符，整数重载会报 `[Expected 51966, Received 47806, Tolerance 0]`，字符串重载会报 `[Expected 0xCAFE, Received 0xBABE, Tolerance 0]`——后者对调试 32 位以上寄存器明显更直观。

#### 4.3.4 代码实践

**实践目标**：跟踪 `axi_single_expect` 的调用链，确认「整数重载走 IntCompare、字符串重载走 SignCompare2」这一分叉，并预见两类错误消息的形态。这是**源码阅读型实践**。

**操作步骤**：

1. 从 [hdl/psi_tb_axi_pkg.vhd:685](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L685) 出发，跳到 `IntCompare` 的实现 [hdl/psi_tb_compare_pkg.vhd:197-209](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L197-L209)，记下它的 `report` 串里用了 `to_string(Expected)`（十进制）。
2. 从 [hdl/psi_tb_axi_pkg.vhd:711-715](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L711-L715) 出发，跳到 `SignCompare2` 的实现 [hdl/psi_tb_compare_pkg.vhd:242-254](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L242-L254)，记下它的 `report` 串里用了 `hstr(std_logic_vector(...))`（十六进制）。
3. 假设「期望 0xCAFE（51966）、实际读到 0xBABE（47806）、tol=0、name="reg0"」，分别手写两个重载会打印的 `###ERROR###` 消息。

**需要观察的现象**：两条消息都以 `###ERROR###: axi_single_expect() received unexpected result - reg0` 开头，但数值部分一个用十进制、一个用十六进制。

**预期结果**：

- 整数重载：`###ERROR###: axi_single_expect() received unexpected result - reg0 [Expected 51966, Received 47806, Tolerance 0]`
- 字符串重载（`base=>16`）：`###ERROR###: axi_single_expect() received unexpected result - reg0 [Expected 0xCAFE, Received 0xBABE, Tolerance 0]`

#### 4.3.5 小练习与答案

**练习 1**：`axi_single_expect` 字符串重载里的 [第 702 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L702) `axi_single_read(address, val, ...)`，编译器凭什么选中了 `axi_single_read` 的 signed 重载而不是整数重载？

**答案**：凭第二个参数 `val` 的类型。在字符串重载里 `val` 声明为 `signed`（[第 699 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L699)），而整数重载的 `value : out integer`、signed 重载的 `value : out signed`。VHDL 按实参类型重载解析，`signed` 实参匹配 signed 重载。这是 VHDL 重载机制替我们「按类型选通路」的典型例子。

**练习 2**：为什么整数重载用 `IntCompare`、字符串重载却要用 `SignCompare2`？能否统一用 `IntCompare`？

**答案**：不能统一。整数重载的期望值与读回值都是 `integer`（32 位），`IntCompare` 刚好够用且消息可读。但字符串重载面向**超过 32 位**的期望值，若硬转 `integer` 会溢出；所以它一路走 `signed`，并用同样吃 `signed`、按十六进制显示的 `SignCompare2` 比较。这正是 [u3-l2](u3-l2-compare-signed-unsigned.md) 里「SignCompare2 专为 >32 位而生」结论的直接应用。

**练习 3**：一次 `axi_single_expect` 最多可能打印几行 `###ERROR###`？分别来自哪里？

**答案**：最多**两行**。第一行来自 `axi_single_read` 内部的响应校验（[第 629/667 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L629) 的 `StdlvCompareStdlv(... sm.rresp ...)`，当 `rresp` 非 OKAY）；第二行来自 expect 自身的值比较（`IntCompare`/`SignCompare2`，当读回值与期望不符）。因为 `severity error` 不中断仿真，两行都会被打印并被 CI 捕获。

---

## 5. 综合实践

把本讲三个过程串成一个完整任务：**用一个最小 AXI slave 模型，让 master BFM 先 `axi_single_write` 写入、再 `axi_single_expect` 读回校验；然后让 slave 在写响应里返回 SLVERR，观察 master 自动打印的 `###ERROR###`**。

本仓库没有现成 AXI testbench，故下面是**示例代码（非项目原有）**，演示「master BFM + slave 模型」如何成对工作，以及 SLVERR 注入点。其中 slave 的逐拍握手时序对仿真器敏感，标注「待本地验证」。

```vhdl
-- 示例代码（非项目原有）：最小 AXI slave + master BFM 的 single 写/读/expect 演示
-- 启用 VHDL-2008（record 含未约束字段）。record 子类型约束语法因工具而异，待本地验证。
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;
library work;
use work.psi_tb_axi_pkg.all;     -- axi_single_* / axi_master_init / axi_slave_init / xRESP_*_c
use work.psi_tb_txt_util.all;    -- print

entity axi_single_demo is
end entity;

architecture sim of axi_single_demo is
    signal clk  : std_logic := '0';
    signal done : boolean   := false;
    -- 32 位数据、4 位 ID；未约束字段全部在声明处约束
    signal ms : axi_ms_r(arid(3 downto 0), araddr(31 downto 0), aruser(7 downto 0),
                         awid(3 downto 0), awaddr(31 downto 0), awuser(7 downto 0),
                         wdata(31 downto 0), wstrb(3 downto 0), wuser(7 downto 0));
    signal sm : axi_sm_r(rid(3 downto 0), rdata(31 downto 0), ruser(7 downto 0),
                         bid(3 downto 0), buser(7 downto 0));
begin
    -- 100 MHz 时钟
    clk <= not clk after 5 ns when not done else '0';

    -- ===== master BFM 侧 =====
    p_master : process is
        variable wrRespIsSlver : boolean := false;   -- 拨动它来注入 SLVERR
    begin
        axi_master_init(ms);
        wait until rising_edge(clk);

        -- (a) 写 0xCAFE 到地址 0
        axi_single_write(0, 16#CAFE#, ms, sm, clk);

        -- (b) 读回地址 0 并自动比较（期望 0xCAFE）。读整字用默认 msb=31,lsb=0,sex=false
        axi_single_expect(0, 16#CAFE#, ms, sm, clk, name => "reg0 after write");

        -- (c) 想观察 SLVERR 错误消息时，把下行注释打开：
        --     它会让 slave 在写响应里回 SLVERR，从而触发 axi_single_write 末尾的报错。
        -- wrRespIsSlver := true;
        -- axi_single_write(0, 16#0001#, ms, sm, clk);  -- 预期打印 ###ERROR###: ... negative response!

        done <= true;
        wait;
    end process;

    -- ===== minimal slave 侧：单个 32 位寄存器 @ 地址 0 =====
    -- 说明：这是一个「行为级」最小模型，逐通道顺序响应 master 的 single 事务。
    --       它假定 master 严格按 AW→W→B、AR→R 的顺序发起（single 系列正是如此）。
    p_slave : process is
        variable reg : std_logic_vector(31 downto 0) := (others => '0');
    begin
        axi_slave_init(sm);
        loop
            -- 处理一次写：AW 握手
            sm.awready <= '0';
            wait until rising_edge(clk) and ms.awvalid = '1';
            sm.awready <= '1';
            wait until rising_edge(clk);            -- 这一沿 AW 握手成立
            sm.awready <= '0';

            -- W 握手并采样数据
            wait until rising_edge(clk) and ms.wvalid = '1';
            sm.wready <= '1';
            reg := ms.wdata;                         -- 采样写入值
            wait until rising_edge(clk);
            sm.wready <= '0';

            -- B：回写响应（OKAY，或被 p_master 拨成 SLVERR）
            sm.bvalid <= '1';
            if done then                             -- 仅作示意；实际 SLVERR 注入请用专门信号
                sm.bresp <= xRESP_SLVERR_c;
            else
                sm.bresp <= xRESP_OKAY_c;
            end if;
            wait until rising_edge(clk) and ms.bready = '1';
            sm.bvalid <= '0';

            -- 处理一次读：AR 握手
            wait until rising_edge(clk) and ms.arvalid = '1';
            sm.arready <= '1';
            wait until rising_edge(clk);
            sm.arready <= '0';

            -- R：回读数据 + 响应
            sm.rvalid <= '1';
            sm.rdata  <= reg;
            sm.rresp  <= xRESP_OKAY_c;
            sm.rlast  <= '1';
            wait until rising_edge(clk) and ms.rready = '1';
            sm.rvalid <= '0';
            sm.rlast  <= '0';
        end loop;
    end process;
end architecture;
```

**任务要求**（按顺序完成）：

1. **读通调用链**：先不运行，对照源码确认 (a) 处 `axi_single_write(0, 16#CAFE#, ...)` 会走到 [第 537 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L537) 的 `to_signed`，把 `0xCAFE` 写到 `ms.wdata`；(b) 处 `axi_single_expect` 会走到 [第 684–685 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L684-L685) 的「读 + IntCompare」。
2. **预期现象（无错场景）**：slave 在 (a) 的写响应里回 `xRESP_OKAY_c`、把 `0xCAFE` 存进 `reg`；(b) 读回 `0xCAFE` 与期望相等 → Transcript **不出现** `###ERROR###`，仿真正常结束。
3. **注入 SLVERR**：取消 (c) 处注释（并改用你自己设的一个 `inject_slver` 信号去拨 slave 的 `bresp`，而不是示例里那个粗糙的 `done` 判断），重跑。预期 Transcript 出现：

   ```
   ###ERROR###: axi_single_write(): received negative response! [Expected 00(0x0), Received 10(0x2)]
   ```

   这一行正是 [第 549 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L549) 的 `StdlvCompareStdlv` 产生的。
4. **（进阶）改成字符串重载**：把 (a) 改成 `axi_single_write(0, "DEADBEEF", 16, ms, sm, clk)`、(b) 改成 `axi_single_expect(0, "DEADBEEF", 16, ms, sm, clk, name => "reg0 hex")`。此时 32 位数据刚好，但如果你把数据线声明成 64 位（`wdata(63 downto 0)` 等），就能体会到字符串重载「能表达超过 32 位」的价值——这是整数重载做不到的。预期读回相符时不报错；若故意把期望改成 `"DEADBEEE"`，会看到 `SignCompare2` 的十六进制消息 `[Expected 0x...DEADBEEE, Received 0x...DEADBEEF, ...]`。

> 待本地验证：示例中 slave 的逐拍 `wait until rising_edge(clk)` 时序在不同仿真器（ModelSim / GHDL / Vivado）上可能需要微调（例如 `awready`/`wready` 提前一拍置位）。若握手卡死，可先用 [u4-l2](u4-l2-stimulus-and-wait.md) 的 `WaitForValueStdlv` 给 slave 加超时诊断。SLVERR 注入也建议用一个独立的 `signal inject_slver : boolean := false`，由 master 在发起那次写之前置位、slave 在回 `bresp` 时读取，比示例里的 `done` 占位更干净。

## 6. 本讲小结

- `axi_single_write` 把一次单拍写事务（AW→W→B 三次握手）封装成一个调用；末尾用 `StdlvCompareStdlv(xRESP_OKAY_c, sm.bresp, ...)` 自动把关——从机回非 OKAY 即打印 `###ERROR###`。整数重载用 `to_signed(value, ...)`（负数以补码写入），字符串重载用 `case base` 选 `decimal/hex_string_to_signed`，从而支持超过 32 位的写值。
- 它总是整字写（`wstrb <= to_signed(-1, ...)` 全 1），AxSIZE 由 `log2(ms.wstrb'length)` 自动算；事务结束后**不调用 init**，只手动拉低 valid——与 `axi_apply_*` 系列「末尾调 init」是两种风格。
- `axi_single_read` 把一次单拍读事务（AR→R 两次握手）封装成一个调用；用 `msb`/`lsb`/`sex` 三参数对 `rdata` 做「截取窗口 [msb:lsb] → 右移到第 0 位 → 按 `sex` 零扩展或符号扩展」，再转成 `integer`（整数重载）或 `signed`（signed 重载）。末尾同样用 `StdlvCompareStdlv(... sm.rresp ...)` 校验读响应。
- signed 重载返回与数据线等宽的 `signed`，**不受 32 位整数限制**——这是支持 >32 位读回值的关键，也是 `axi_single_expect` 字符串重载内部要调它的原因。
- `axi_single_expect` = 「`axi_single_read` + 一次比较」：整数重载用 `IntCompare`（十进制消息、32 位限制），字符串重载用 `SignCompare2`（十六进制消息、任意位宽）。`name` 参数拼进消息，方便定位失败点；`tol` 透传给比较过程做容差比较。
- 「响应错误」（`rresp`/`bresp` 非 OKAY，由 `StdlvCompareStdlv` 抓）与「数据不符」（由 `IntCompare`/`SignCompare2` 抓）是两条**独立**的 `###ERROR###` 路径，都用 `severity error` 不中断仿真，最后被 CI 的 `run_check_errors "###ERROR###"` 统一捕获——一次 expect 最多可打印两行错误。

## 7. 下一步学习建议

- 继续学 **[u5-l3 AXI 部分事务与突发传输](u5-l3-axi-partial-and-burst.md)**。那里会把本讲「自包含的 single」拆开成 `axi_apply_*`（驱动一端）与 `axi_expect_*`（观察并校验另一端）两类过程，支持任意突发长度、字节掩码（`WstrbFirst/Last`）、节流（`VldLowCycles`/`RdyLowCycles`）。你会更清楚地看到 single 系列「整字写、不调 init」与 apply 系列「逐拍、末尾调 init」的分工。
- 如果你想把本讲的 BFM 类型接到**综合侧**的 DUT（用 psi_common 的 AXI 类型），直接看 **[u5-l4 TB 与综合 AXI 类型互转](u5-l4-axi-conversion.md)**，看 `axi_ms_r`/`axi_sm_r` 的每个字段如何逐个映射到 `axi_slv_inp`/`axi_slv_oup`，从而在一个 testbench 里同时驱动 TB BFM 与综合 DUT。
- 回顾 **[u3-l1](u3-l1-compare-basic.md)** 与 **[u3-l2](u3-l2-compare-signed-unsigned.md)**，把本讲反复出现的 `StdlvCompareStdlv` / `IntCompare` / `SignCompare2` 的消息格式与 32 位天花板再对一遍——理解了它们，本讲所有 `###ERROR###` 的来历就一目了然。
- 建议同时打开源码 [hdl/psi_tb_axi_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd)，把本讲引用的行号（125–177 声明、519–716 实现）与 [hdl/psi_tb_compare_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd) 的 143–156、197–209、242–254 对照读一遍，确认每一条结论都能指到具体那一行。
