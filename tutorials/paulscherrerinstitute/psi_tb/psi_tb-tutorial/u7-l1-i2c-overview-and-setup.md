# I2C 概览、寻址与总线初始化

## 1. 本讲目标

本讲是 I2C 单元（u7）的第一讲，带你走进 psi_tb 的 I2C 总线功能模型 `psi_tb_i2c_pkg`。学完本讲你应该能够：

- 说清楚 I2C 的「两根线 + 开漏 + 上拉」物理模型，以及它在 VHDL 仿真里如何用 `std_logic` 的多驱动解析来近似。
- 记住 `I2c_ACK` / `I2c_NACK` 两个常量为什么是 `0` / `1`，并理解 `I2c_Transaction_t` 类型的作用。
- 手算 `I2cGetAddr` 的返回值，明白它把「7 位地址 + R/W 位」打包成一个整数字节的原理，以及它为何只是一个面向用户的辅助函数。
- 知道在跑任何 I2C 事务之前必须先做「总线初始化」：用 `I2cPullup` 接上拉、用 `I2cBusFree` 让自己的进程释放总线、用 `I2cSetFrequency` 设定仿真位时序。

本讲**只**讲「地基」：常量、类型、寻址函数与三个初始化过程。具体的 Start / Stop / 发字节 / 收字节等主机事务在 u7-l2，从机事务与时钟拉伸在 u7-l3，完整 testbench 在 u7-l4。

## 2. 前置知识

### 2.1 I2C 总线是什么

I2C（Inter-Integrated Circuit）是一种两线制串行总线，用两根线通信：

- **SCL**：时钟线（Serial Clock），由主机驱动。
- **SDA**：数据线（Serial Data），主机和从机分时驱动。

一个总线上挂一个主机、一个或多个从机，每个从机有一个地址。通信总是由主机发起：主机先发「Start」，再发一个「地址 + 读/写位」字节选中某个从机，然后按方向收发数据字节，每个字节后接收方回一个 ACK/NACK，最后主机发「Stop」结束。

I2C 的关键物理特性是**开漏（open-drain）**：任何器件都只能把线「拉低」到 0，不能自己把线「拉高」；要让线变高，器件必须「松手」（高阻），由总线上的**上拉电阻**把它拉高。这正是本讲后面 `I2cPullup` 要建模的东西。

### 2.2 开漏在 VHDL 仿真里怎么表达

`std_logic` 有一套**多驱动解析表（resolution function）**：当一根线上有多个驱动源时，最终值由下表决定（节选）：

| 驱动 A ＼ 驱动 B | `'0'`（强低） | `'H'`（弱高） | `'Z'`（高阻） |
|---|---|---|---|
| `'0'`（强低） | `'0'` | `'0'` | `'0'` |
| `'H'`（弱高） | `'0'` | `'H'` | `'H'` |
| `'Z'`（高阻） | `'0'` | `'H'` | `'Z'` |

把这张表对应到 I2C：

- 器件**拉低**线 → 驱动 `'0'`。
- 器件**松手**（释放） → 驱动 `'Z'`。
- **上拉电阻** → 用一个常驻的 `'H'`（weak high）驱动来模拟。

于是「强低 `0`」会盖过「弱高 `H`」，松手（`Z`）时弱高 `H` 显现出来——正好就是开漏 + 上拉的行为。这就是为什么 psi_tb 用 `'H'` 表示「总线空闲高电平」，而用 `'Z'` 表示「我这个进程当前不驱动」。代码里凡是要判断「线是不是高」，都必须**同时接受 `'1'` 和 `'H'`**（见 4.3.3 的 `LevelCheck`）。

### 2.3 承接的前置讲义

- **u3-l1**：psi_tb 全库的检查过程都拼 `###ERROR###: ` 前缀，比较失败用 `assert ... severity error` 只打印不中断。本讲的 I2C 包沿用同一套前缀约定。
- **u4-l1**：`CheckLastActivity`（快照版活动检查）被 I2C 包的位级原语用来校验「SCL 高电平期间 SDA 是否稳定」。本讲会引用它，但不重复其内部细节。

## 3. 本讲源码地图

本讲只涉及一个源文件，外加一个 testbench 作为「真实用法」参照。

| 文件 | 角色 |
|---|---|
| [hdl/psi_tb_i2c_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd) | I2C BFM 包本体，声明（package header，L24–143）与实现（package body，L148–739）成对组织。本讲只关心其中的常量、类型、`I2cGetAddr` 和三个初始化过程。 |
| [testbench/psi_tb_i2c_pkg_tb.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd) | 仓库里唯一的 I2C 示例 TB，演示了 `I2cPullup` / `I2cBusFree` / `I2cSetFrequency` 的真实调用位置。 |

包的整体结构（你在阅读源码时应先建立这张全景图）：

- **常量与类型**（L28–31）：`I2c_ACK` / `I2c_NACK`、`I2c_Transaction_t`。
- **一个函数**（L36–37 / L390–394）：`I2cGetAddr`。
- **初始化**（L42–48 / L399–416）：`I2cPullup`、`I2cBusFree`、`I2cSetFrequency`。
- **主机事务**（L53–89 声明）：Start / RepeatedStart / Stop / SendAddr / SendByte / ExpectByte —— u7-l2 详解。
- **从机事务**（L94–141 声明）：WaitStart / WaitStop / ExpectAddr / ExpectByte / SendByte —— u7-l3 详解。
- **私有辅助**（L153–385）：消息生成 `GenMessage`、电平检查 `LevelCheck` / `LevelWait`、时序计算 `ClkPeriod` 等、位/字节传输原语 `SendBitInclClock` 等。本讲只挑其中与初始化直接相关的部分讲解。

依赖关系：包体 `use work.psi_tb_compare_pkg.all`、`work.psi_tb_activity_pkg.all`、`work.psi_tb_txt_util.all`（本库三件套），以及 `work.psi_common_logic_pkg.all`、`work.psi_common_math_pkg.all`（来自 psi_common，提供 `choose`、`to_01X` 等辅助）。

## 4. 核心概念与源码讲解

### 4.1 ACK/NACK 常量与 I2c_Transaction_t 类型

#### 4.1.1 概念说明

I2C 协议里，每传完一个字节（8 位），**接收方**要回一位应答：

- **ACK（应答）**：接收方把 SDA 拉低，表示「我收到了，继续」。
- **NACK（不应答）**：接收方松手让 SDA 保持高，表示「我不想继续 / 没收到」。

注意这是**低有效**：拉低 = ACK，高 = NACK。这一点和很多「高有效」的直觉相反，是 I2C 最容易记错的细节之一。

另外，主机发起一次事务时要声明方向——读还是写。psi_tb 把这两个方向定义成一个枚举类型 `I2c_Transaction_t`，供 `I2cGetAddr` 等地方使用。

#### 4.1.2 核心流程

- 接收方在 ACK 时隙把 SDA 拉到 `I2c_ACK`（低），发送方据此判定成功。
- 接收方若回 `I2c_NACK`（高），发送方通常随后发 Stop 结束。
- 主机用 `I2c_READ` / `I2c_WRITE` 表明事务方向，`I2cGetAddr` 会据此把 R/W 位置 1 或 0（见 4.2）。

#### 4.1.3 源码精读

常量与类型的定义非常短，但含义关键：

[hdl/psi_tb_i2c_pkg.vhd:28-31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L28-L31) — 定义 `I2c_ACK='0'`、`I2c_NACK='1'`（低有效应答）和 `I2c_Transaction_t` 枚举（读/写两种方向）。

```vhdl
constant I2c_ACK 	: std_logic := '0';
constant I2c_NACK 	: std_logic := '1';

type I2c_Transaction_t is (I2c_READ, I2c_WRITE);
```

这两个常量是**给用户写 TB 时直接引用**的。例如在主机读事务里，你希望从机回 ACK，就传 `ExpectedAck => I2c_ACK`；若你故意测试 NACK 场景，就传 `I2c_NACK`。在示例 TB 中能看到两种用法对照：

[testbench/psi_tb_i2c_pkg_tb.vhd:63](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L63) — 用 `'1'`（即 NACK）作为 `ExpectedAck`，校验「主机期望收到 NACK」的场景（这里直接写字面量 `'1'`，与 `I2c_NACK` 等价）。

> 小提示：示例 TB 里多处直接写 `'0'` / `'1'` 而非 `I2c_ACK` / `I2c_NACK`，两者数值相同；用常量可读性更好，建议在自己的 TB 里优先用常量。

#### 4.1.4 代码实践

**目标**：直观确认「ACK = 拉低」这件事。

**操作**：在一个最小 TB 里，先把 `sda` 通过 `I2cPullup` 拉到 `'H'`（见 4.3），再写一句断言：

```vhdl
-- 示例代码：仅演示常量含义，不调用任何 I2c 事务
assert I2c_ACK = '0' report "ACK 应为 0" severity note;
assert I2c_NACK = '1' report "NACK 应为 1" severity note;
```

**预期**：两条 `report ... severity note` 不会触发（条件为真），仿真日志里看不到这两条消息。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `I2c_ACK` 是 `'0'` 而不是 `'1'`？

**答案**：因为 I2C 的应答是**低有效**——接收方把 SDA 拉低表示「收到」（ACK），松手让上拉把 SDA 拉高表示「不应答」（NACK）。所以 ACK 对应强低 `'0'`。

**练习 2**：`I2c_Transaction_t` 有哪两个取值？分别代表什么？

**答案**：`I2c_READ`（读事务，R/W 位 = 1）和 `I2c_WRITE`（写事务，R/W 位 = 0）。

### 4.2 I2cGetAddr 寻址编码函数

#### 4.2.1 概念说明

I2C 的「地址字节」（常称 SLA，Slave Address）并不是把 7 位地址直接发出去，而是：

\[
\text{SLA 字节} = \{\,\text{Addr}[6{:}0],\ \text{R/W}\,\}
\]

即「7 位地址放高 7 位、最低位是 R/W」。把它看成一个整数，就是：

\[
\text{SLA} = \text{Addr} \times 2 + \text{rw}, \qquad \text{rw} = \begin{cases}1 & \text{读} \\ 0 & \text{写}\end{cases}
\]

「×2」相当于左移一位，给最低位腾出 R/W 的位置。`I2cGetAddr` 就是把这个打包结果作为一个整数返回，方便用户在 TB 里日志、比对或手工拼字节。

> 注意：这个函数只描述 **7 位寻址**下「地址 + R/W」的打包。10 位寻址要发两个字节（见 4.2.3），`I2cGetAddr` 并不处理它。

#### 4.2.2 核心流程

```
输入：Addr（7 位地址整数）、Trans（读/写）
输出：整数值 = Addr*2 + (Trans==I2c_READ ? 1 : 0)
```

举例（与示例 TB 一致）：

| Addr | 方向 | R/W | `I2cGetAddr` 返回值 | 二进制（SLA 字节） |
|---|---|---|---|---|
| `16#12#`（0x12） | 读 | 1 | 0x12·2 + 1 = **0x25** | `0010_0101` |
| `16#13#`（0x13） | 写 | 0 | 0x13·2 + 0 = **0x26** | `0010_0110` |

#### 4.2.3 源码精读

[hdl/psi_tb_i2c_pkg.vhd:36-37](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L36-L37) — `I2cGetAddr` 的声明：吃一个整数地址和一个事务方向，返回整数。

[hdl/psi_tb_i2c_pkg.vhd:390-394](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L390-L394) — 函数体，一行实现地址左移 + R/W 拼接：

```vhdl
function I2cGetAddr( Addr 	: in integer;
                     Trans 	: in I2c_Transaction_t) return integer is
begin
    return Addr*2+choose(Trans=I2c_READ, 1, 0);
end function;
```

这里 `choose(cond, a, b)` 是来自 `psi_common_logic_pkg` 的三元选择函数：`cond` 为真返回 `a`，否则返回 `b`（等价于 C 的 `cond ? a : b`）。所以读事务加 1，写事务加 0。

**一个容易被忽视的事实**：`I2cGetAddr` 是一个**纯面向用户的辅助函数**，包内部并不调用它。主机过程 `I2cMasterSendAddr`（L498 起）和从机过程 `I2cSlaveExpectAddr`（L658 起）都各自重新做一遍地址拼装——它们用 `to_unsigned(Address, 10)` 得到位向量，再手工切片，而不是走 `I2cGetAddr`。这一点用 `Grep` 在全仓库搜 `I2cGetAddr` 可以验证：除了 manifest，命中只有这个函数自己的声明（L36）和实现（L390）两处。

为什么内部不用？因为 `I2cMasterSendAddr` 既要支持 7 位又要支持 10 位，还需要按位驱动 SDA，直接在位向量上切片（`AddrSlv_c(6 downto 0) & Rw_c`）更自然；`I2cGetAddr` 把结果压成一个整数反而帮不上忙。它的价值在于：**你在写自己的 TB 时**，想打印「这次事务实际发出去的 SLA 字节是几」，可以调用它。

对照看 7 位与 10 位两种拼装（这一段属于 u7-l2/u7-l3 的内容，这里只用来理解 `I2cGetAddr` 的边界）：

[hdl/psi_tb_i2c_pkg.vhd:510-521](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L510-L521) — `I2cMasterSendAddr` 内部自拼地址：7 位时发 `Addr[6:0] & Rw`；10 位时先发 `"11110" & Addr[9:8] & Rw`（保留前缀 `11110` 标识 10 位寻址），再发低 8 位 `Addr[7:0]`。完全不走 `I2cGetAddr`。

#### 4.2.4 代码实践

**目标**：手算并程序化验证 `I2cGetAddr`。

**操作**：在 TB 里调用并打印：

```vhdl
-- 示例代码
print("0x12 read  -> " & to_string(I2cGetAddr(16#12#, I2c_READ)));
print("0x13 write -> " & to_string(I2cGetAddr(16#13#, I2c_WRITE)));
```

**预期**：第一行打印 `37`（= 0x25 = 37 十进制），第二行打印 `38`（= 0x26 = 38）。**待本地验证**（注意 `to_string` 来自 `psi_tb_txt_util`，整数重载按十进制输出，回顾 u2-l1）。

#### 4.2.5 小练习与答案

**练习 1**：地址 `0x50`、写事务，`I2cGetAddr` 返回多少？对应 SLA 字节的二进制是什么？

**答案**：`0x50·2 + 0 = 0xA0 = 160`。二进制 `1010_0000`（高 7 位是 `1010_000` = 0x50，最低位 R/W = 0）。

**练习 2**：为什么 `I2cGetAddr` 用乘 2 而不是位拼接？

**答案**：因为函数返回的是 `integer`，没有位宽概念；「左移一位」在整数域就是「乘 2」，把最低位空出来放 R/W。若要返回位向量，就得指定位宽，反而不如让调用方自己决定如何使用这个整数。

**练习 3**：10 位寻址时还能用 `I2cGetAddr` 得到完整的寻址字节吗？

**答案**：不能。10 位寻址需要两个字节（`11110AA + RW` 与低 8 位），`I2cGetAddr` 只算 7 位寻址的「地址 + R/W」单字节打包。10 位场景由 `I2cMasterSendAddr` / `I2cSlaveExpectAddr` 内部手工拼装。

### 4.3 总线初始化：I2cPullup / I2cBusFree / I2cSetFrequency

这三个过程是跑任何 I2C 事务前的「开机动作」：`I2cPullup` 模拟上拉电阻、`I2cBusFree` 让进程释放总线、`I2cSetFrequency` 设定仿真位时序。它们共同把「两根 `inout std_logic`」变成一条可工作的 I2C 总线。

#### 4.3.1 概念说明

**为什么需要上拉（`I2cPullup`）**：开漏总线没有上拉就无法出现高电平（见 2.2）。仿真里上拉电阻被建模成一个**常驻的 `'H'` 驱动**——只要它一直驱动 `'H'`，任何器件一松手（驱动 `'Z'`），线就解析为 `'H'`（高）；一旦有器件驱动 `'0'`，强低盖过弱高，线变 `'0'`。

**为什么需要释放（`I2cBusFree`）**：在示例 TB 里，`scl` / `sda` 同时被**上拉、主机进程、从机进程**三个驱动源驱动。当一个进程不参与时，它必须把自己的驱动设成 `'Z'`（不参与解析），否则它会和正在驱动的另一侧打架。`I2cBusFree` 就是「我这个进程现在松手」的调用。

**为什么需要设频率（`I2cSetFrequency`）**：I2C 的 SCL 是**软件位打**（bit-bang）出来的，不是一个独立时钟信号。包体内部用一个共享变量 `FreqClk_v` 记住当前频率，再由几个 `impure function` 算出 SCL 的周期、半周期、四分之一周期，用来在各步骤之间 `wait for`。不调 `I2cSetFrequency` 时，频率是默认的 100 kHz。

#### 4.3.2 核心流程

```
TB 启动:
  1) 并发调用 I2cPullup(scl, sda)         -- 全程驱动 'H'，模拟上拉
  2) 每个参与进程开工时: I2cBusFree(scl,sda) -- 把本进程驱动设为 'Z'
  3) 任一进程(通常主机): I2cSetFrequency(f)  -- 设定 SCL 频率到共享变量
  ---- 之后才能调用 Start/SendAddr/... 等事务 ----
```

`I2cSetFrequency` 只做一件事：把频率写进 `FreqClk_v`；位时序由下面的函数实时换算（`impure` 是因为它们读了共享变量）：

\[
T_{\text{周期}} = \frac{1\,\text{s}}{f}, \quad T_{\text{半}} = \frac{0.5\,\text{s}}{f}, \quad T_{\text{四分}} = \frac{0.25\,\text{s}}{f}
\]

例如 400 kHz（Fast 模式）下：\(T_{\text{周期}} = 2.5\,\mu\text{s}\)，\(T_{\text{半}} = 1.25\,\mu\text{s}\)，\(T_{\text{四分}} = 625\,\text{ns}\)。

#### 4.3.3 源码精读

**默认频率与共享变量**——`FreqClk_v` 是 `shared variable`，可被架构内多个进程共享：

[hdl/psi_tb_i2c_pkg.vhd:162](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L162) — 默认频率 `100.0e3`（100 kHz，I2C 标准模式）。

```vhdl
shared variable FreqClk_v	: real	:= 100.0e3;
```

**三个初始化过程的声明**：

[hdl/psi_tb_i2c_pkg.vhd:42-48](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L42-L48) — `I2cPullup` / `I2cBusFree` / `I2cSetFrequency` 的声明。前两者参数是 `signal scl/sda : inout std_logic`，第三个只吃一个 `real` 频率。

**三个初始化过程的实现**——三者都极短，但各自的取值（`'H'` / `'Z'`）有明确含义：

[hdl/psi_tb_i2c_pkg.vhd:399-416](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L399-L416) — `I2cPullup` 把两根线驱动为 `'H'`（上拉），`I2cBusFree` 驱动为 `'Z'`（释放本进程驱动），`I2cSetFrequency` 把频率写入共享变量 `FreqClk_v`。

```vhdl
procedure I2cPullup(signal Scl : inout std_logic;
                    signal Sda : inout std_logic) is
begin
    Scl <= 'H';
    Sda <= 'H';
end procedure;

procedure I2cBusFree(signal Scl : inout std_logic;
                    signal Sda : inout std_logic) is
begin
    Scl <= 'Z';
    Sda <= 'Z';
end procedure;

procedure I2cSetFrequency(FrequencyHz : in real) is
begin
    FreqClk_v := FrequencyHz;
end procedure;
```

**位时序换算函数**——这三个 `impure function` 读了 `FreqClk_v`，所以 `I2cSetFrequency` 一改，全包的时序立即跟着变：

[hdl/psi_tb_i2c_pkg.vhd:222-235](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L222-L235) — 由 `FreqClk_v` 换算 SCL 的整周期 / 半周期 / 四分之一周期，供所有位级原语 `wait for` 使用。

**配套的电平检查（理解 `'H'` 处理）**——所有判断「线是否为高」的地方都要同时认 `'1'` 和 `'H'`。这是 `I2cPullup` 用 `'H'` 能正常工作的前提：

[hdl/psi_tb_i2c_pkg.vhd:186-197](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L186-L197) — `LevelCheck`：期望高时，把 `'1'` 与 `'H'` 都视为合格（`(Sig = '1') or (Sig = 'H')`）；期望值非 `0/1` 时跳过检查。

[hdl/psi_tb_i2c_pkg.vhd:199-218](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L199-L218) — `LevelWait`：在超时内等线达到期望电平，等高时同样接受 `'1'` 或 `'H'`，超时未达成则打印 `###ERROR###`。

**真实用法**——看示例 TB 怎么把它们组合起来。注意 `I2cPullup` 是**并发调用**（在 `begin` 之后、进程之外，是一句并发过程调用语句），而 `I2cBusFree` / `I2cSetFrequency` 在**进程内部**：

[testbench/psi_tb_i2c_pkg_tb.vhd:28-41](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L28-L41) — `scl/sda` 初值 `'H'`；并发调用 `I2cPullup(scl, sda)` 持续驱动上拉；主机进程开工时先 `I2cBusFree` 再 `I2cSetFrequency(400.0e3)`，然后才开始发事务。

```vhdl
signal scl 	: std_logic := 'H';
signal sda	: std_logic := 'H';
begin
    -- Pullup resistors
    I2cPullup(scl, sda);

    -- Master Process
    p_master : process
    begin
        -- Setup
        I2cBusFree(scl, sda);
        I2cSetFrequency(400.0e3);
        wait for 1 us;
        ...                          -- 之后才是 SendStart/SendAddr/...
```

注意从机进程（[L171–174](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L171-L174)）只调了 `I2cBusFree`、**没有**调 `I2cSetFrequency`——它依赖主机进程已经把共享变量设成 400 kHz。这是 `shared variable` 跨进程共享的直接体现，也是潜在的坑：如果调换两个进程的启动顺序或时序，从机可能在一个尚未设频的瞬间工作（见小练习 3）。

#### 4.3.4 代码实践

**目标**：亲手搭一个只有 `scl` / `sda` 的最小 TB，接上上拉、设 400 kHz，确认总线空闲时两根线都是高电平。

**操作步骤**：

1. 新建一个 TB（示例代码），实例化上拉并设频：

```vhdl
-- 示例代码：最小 I2C 总线初始化 TB
library ieee;
    use ieee.std_logic_1164.all;
library work;
    use work.psi_tb_i2c_pkg.all;
    use work.psi_tb_txt_util.all;

entity i2c_bus_idle_tb is
end entity;

architecture sim of i2c_bus_idle_tb is
    signal scl : std_logic := 'H';
    signal sda : std_logic := 'H';
begin
    -- 持续驱动上拉（模拟电阻）
    I2cPullup(scl, sda);

    p_stim : process
    begin
        I2cBusFree(scl, sda);          -- 本进程松手
        I2cSetFrequency(400.0e3);      -- 400 kHz Fast 模式
        wait for 1 us;
        -- 观察：此刻没有任何器件拉低，总线应为高
        print("scl = " & to_string(scl) & ", sda = " & to_string(sda));
        wait;
    end process;
end sim;
```

2. **编译前提**：`psi_tb_i2c_pkg` 依赖 `psi_tb_compare_pkg` / `psi_tb_activity_pkg` / `psi_tb_txt_util` 以及 psi_common 的 `psi_common_logic_pkg` / `psi_common_math_pkg`，需先把它们都加入编译清单（回顾 u1-l2、u1-l3）。注意按 u1-l2 所述，I2C 包当前**不在** `sim/config.tcl` 的 CI 编译清单里，需要手动 `add_sources`。

**需要观察的现象**：

- 由于没有任何器件驱动 `'0'`，`scl` / `sda` 在上拉 `'H'` 作用下应解析为 `'H'`。
- `print` 打印的值应为 `scl = H, sda = H`（`std_logic` 的 `to_string` 原样输出字符，回顾 u2-l1）。

**预期结果**：Transcript 出现 `scl = H, sda = H`。**待本地验证**：`std_logic` 的 `to_string` 在不同仿真器（ModelSim / GHDL）下对 `'H'` 的输出是否原样打印，请以实际为准。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `I2cPullup(scl, sda)` 这句并发调用删掉，`scl` / `sda` 的初值仍是 `'H'`，总线空闲时还会是高吗？

**答案**：在没有任何进程驱动 `'0'` 的瞬间，初值 `'H'` 会让线暂时为高。但 `I2cPullup` 的作用是提供**持续**的上拉驱动；一旦事务过程中某进程驱动 `'Z'`（释放），没有 `I2cPullup` 时该线就没有高电平来源（只剩高阻 `'Z'`），开漏模型就失效了。所以上拉必须常驻。

**练习 2**：`I2cBusFree` 驱动 `'Z'`，`I2cPullup` 驱动 `'H'`。当两者同时作用于 `sda` 时，解析结果是什么？当再叠加一个进程驱动 `'0'` 呢？

**答案**：`'Z'`（进程释放）与 `'H'`（上拉）解析为 `'H'`（高）。再叠加 `'0'`（某器件拉低）时，强低 `'0'` 盖过弱高 `'H'`，解析为 `'0'`（低）。这正是开漏总线的「线与」特性。

**练习 3**：示例 TB 里从机进程没有调 `I2cSetFrequency`，为什么通常也能正常工作？什么情况下会出问题？

**答案**：因为 `FreqClk_v` 是 `shared variable`，主机进程调一次 `I2cSetFrequency` 后，从机进程读到的也是同一个值。出问题的场景：如果从机进程的某次位操作发生在主机调 `I2cSetFrequency` **之前**（例如仿真 0 时刻的竞争），它会用到默认的 100 kHz 时序。示例 TB 通过在主机侧 `wait for 1 us`、在从机侧先 `I2cBusFree` 再 `WaitStart`（会等待主机发 Start）避免了这种竞争。

## 5. 综合实践

把本讲三块内容串成一个端到端的小任务：**只初始化总线、不发任何数据，但用断言把「空闲电平」「地址打包」「默认/自定义频率」全部自检一遍**。

任务要求：

1. 仿照 4.3.4 写一个最小 TB，含 `I2cPullup` 并发调用与一个 stim 进程。
2. 进程里依次做：
   - `I2cBusFree` 后 `wait for 1 us`，断言 `scl = 'H'` 且 `sda = 'H'`（用 `(scl='H' or scl='1')` 形式，与 `LevelCheck` 一致）。
   - 调 `I2cSetFrequency(100.0e3)`，打印一个周期长度（你可以用 `now` 前后差或直接打印 `1 sec/100.0e3` 的换算结果）。
   - 用 `I2cGetAddr(16#12#, I2c_READ)` 与字面量 `16#25#` 做相等断言，验证寻址打包正确。
   - 再 `I2cSetFrequency(400.0e3)`，验证频率可随时切换。
3. 让 TB 跑完后**无 `###ERROR###`**——本任务所有失败都用 `assert ... report "###ERROR###: ..." severity error` 表达，使之符合 u1-l3 讲的 CI 通过约定。

**检查清单**：

- [ ] 上拉并发调用位置正确（进程外）。
- [ ] 进程开工先 `I2cBusFree`。
- [ ] `I2cGetAddr` 的两个参数类型分别是 `integer` 和 `I2c_Transaction_t`。
- [ ] 所有断言的 `report` 字符串以 `###ERROR###` 开头（如果你想让 CI 能抓到失败）。

> 提示：这个练习**不调用任何 Start/SendAddr 等事务**，因此即使你还没学 u7-l2 也能完整跑通；它只验证本讲的「地基」是否搭好。运行结果**待本地验证**。

## 6. 本讲小结

- I2C 是两线（SCL/SDA）、开漏 + 上拉的总线；psi_tb 用 `std_logic` 多驱动解析来近似：器件拉低驱动 `'0'`、松手驱动 `'Z'`、上拉常驻驱动 `'H'`。
- `I2c_ACK='0'` / `I2c_NACK='1'` 体现「应答低有效」；`I2c_Transaction_t = (I2c_READ, I2c_WRITE)` 表明事务方向。
- `I2cGetAddr(Addr, Trans) = Addr*2 + (读?1:0)` 把 7 位地址 + R/W 位打包成一个整数字节；它是面向用户的辅助函数，包内部各地址过程并不调用它，而是自己按位切片（且额外支持 10 位寻址）。
- 跑任何 I2C 事务前必须先初始化：并发 `I2cPullup` 接上拉、各进程开工调 `I2cBusFree` 释放、用 `I2cSetFrequency` 把频率写入 `shared variable FreqClk_v`（默认 100 kHz）。
- 所有「判高」都同时认 `'1'` 和 `'H'`（见 `LevelCheck` / `LevelWait`），这是 `'H'` 上拉模型能工作的前提；时序由 `impure function ClkPeriod/Half/Quart` 实时从 `FreqClk_v` 换算。

## 7. 下一步学习建议

本讲只搭好了「地基」（常量、类型、寻址函数、总线初始化），还没碰真正的事务。下一步建议：

- **u7-l2（I2C 主机事务）**：学习 `I2cMasterSendStart` / `SendRepeatedStart` / `SendStop` / `SendAddr` / `SendByte` / `ExpectByte`，看它们如何在已经初始化好的总线上位打 SCL、收发字节、校验 ACK/NACK。建议阅读 [hdl/psi_tb_i2c_pkg.vhd:237-264](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L237-L264) 的 `SendBitInclClock` 作为热身——它是所有位级时序的真正源头。
- **u7-l3（I2C 从机事务与时钟拉伸）**：学习从机侧的 `WaitStart` / `ExpectAddr` / `ExpectByte` / `SendByte`，重点理解 `Timeout` 与 `ClkStretch` 如何在仿真中建模超时与从机拉低 SCL。
- **u7-l4（I2C 测试平台实战）**：逐段精读 `testbench/psi_tb_i2c_pkg_tb.vhd`，看 master / slave 两个并发进程如何在共享的 `scl/sda` 上「对拍」，把本讲和 u7-l2/u7-l3 的所有原语串成一个完整自检 TB。

继续阅读建议：先通读一遍 [hdl/psi_tb_i2c_pkg.vhd](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd) 的私有辅助段（L150–385），建立「消息怎么拼、电平怎么等、位怎么打」的全景，再进入 u7-l2 就会非常顺。
