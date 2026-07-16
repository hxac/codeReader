# SPI 主机与从机

## 1. 本讲目标

本讲讲解 Open Logic 的两个 SPI 接口实体：`olo_intf_spi_master`（SPI 主机）与 `olo_intf_spi_slave`（SPI 从机）。学完本讲后，读者应该能够：

- 理解 SPI 协议的四种时钟模式（CPOL/CPHA 组合），并能说明主机与从机为何必须配置成同一模式。
- 掌握 `olo_intf_spi_master` 的命令/响应接口、多从机选择、可变传输位宽与 `Cmd_CsHold` 的用法。
- 掌握 `olo_intf_spi_slave` 的 RX/TX/Response 三组接口、连续事务、三态 MISO 与输入同步机制。
- 理解 LSB/MSB 两种位序在源码中是如何用一个移位寄存器实现的。
- 能够把主机与从机对接，完成一次全双工传输并验证交换的数据正确。

## 2. 前置知识

在进入源码前，先用通俗语言建立几个基础概念。

**SPI 是什么。** SPI（Serial Peripheral Interface，串行外设接口）是一种**同步、串行、全双工**的通信协议。它用四根线：

- **SCLK**：主机产生的串行时钟，所有数据位的收发都按这个节拍进行。
- **MOSI**（Master Out Slave In）：主机发、从机收。
- **MISO**（Master In Slave Out）：从机发、主机收。
- **CS_n / SS**（Chip Select，低有效）：主机拉低某一根 CS_n，就选中了对应的从机。

**全双工的含义。** SPI 没有独立的“读”和“写”阶段——每个时钟周期里，主机通过 MOSI 发一个比特的**同时**，从机通过 MISO 发回一个比特。两边各持有一个移位寄存器，时钟每跳一次，两个寄存器就交换一比特。所以一次 N 位的 SPI 传输结束后，主机发出了 N 位、也收到了 N 位，这是理解本讲“为什么主从要对得齐”的关键。

**谁是主、谁是从。** 主机（master）**产生 SCLK 并控制 CS_n**，决定什么时候通信、时钟多快；从机（slave）被动接收 SCLK 与 CS_n，按主机的节拍收发。这意味着：主机的 SCLK 是它自己用系统时钟 `Clk` 分频出来的（数据信号，不走 PLL），所以它不必同步输入；而从机的 SCLK/MOSI/CS_n 全是外部异步信号，**必须先过同步器**（参见 [u7-l1](u7-l1-sync-debounce-clkmeas.md)）。

**两进程法与 AXI-S 握手。** 本讲两个实体都采用 Open Logic 全库通用的「两进程法 + record」（参见 [u2-l2](u2-l2-pipeline-stage-handshake.md)）：组合进程 `p_comb` 只算下一拍状态 `r_next`，时序进程 `p_seq` 只打拍并以进程末尾覆盖实现同步高有效复位。用户侧的数据接口遵循 AXI-S 的 Valid/Ready 握手（参见 [u1-l5](u1-l5-conventions-and-anatomy.md)）。

## 3. 本讲源码地图

| 文件 | 作用 |
| :--- | :--- |
| [src/intf/vhdl/olo_intf_spi_master.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd) | SPI 主机实体。一个 5 状态 FSM，把命令翻译成 SCLK/MOSI/CS_n 时序，回采 MISO。 |
| [src/intf/vhdl/olo_intf_spi_slave.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd) | SPI 从机实体。同步 SPI 输入，按采样沿收发，管理 TX 锁存与 Response。 |
| [doc/intf/olo_intf_spi_master.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_spi_master.md) | 主机文档，含 CPOL/CPHA 时序图与 CS 处理说明。 |
| [doc/intf/olo_intf_spi_slave.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_spi_slave.md) | 从机文档，含时序余量与协议示例。 |
| [test/intf/olo_intf_spi_master/olo_intf_spi_master_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_master/olo_intf_spi_master_tb.vhd) | 主机 VUnit 测试台，用 `olo_test_spi_slave_vc` 模拟从机。 |
| [test/intf/olo_intf_spi_slave/olo_intf_spi_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_slave/olo_intf_spi_slave_tb.vhd) | 从机 VUnit 测试台，用 `olo_test_spi_master_vc` 模拟主机。 |
| [sim/test_configs/olo_intf.py](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py) | 按区域组织 generic 组合，遍历 CPOL/CPHA/位序/位宽等。 |

> 说明：本仓库的测试台**没有**直接把主机 RTL 接到从机 RTL，而是各自用 VUnit 验证组件（VC）模拟对端。第 5 节的综合实践会带你亲手把两个真实实体对接。

## 4. 核心概念与源码讲解

### 4.1 CPOL/CPHA：SPI 的四种时钟模式

#### 4.1.1 概念说明

SPI 没有像 UART 那样在数据里嵌起始位，收发双方靠 SCLK 的**边沿**对齐每一比特。但“哪个边沿采样、哪个边沿换数、空闲时电平是高还是低”会因器件而异。SPI 标准用两个参数把所有可能收敛成 4 种模式：

- **CPOL（Clock Polarity，时钟极性）**：空闲时 SCLK 的电平。CPOL=0 空闲低，CPOL=1 空闲高。
- **CPHA（Clock Phase，时钟相位）**：在哪个边沿采样数据。
  - CPHA=0：在**第一个**边沿（leading edge）采样，第二个边沿换数。
  - CPHA=1：在**第二个**边沿（trailing edge）采样，第一个边沿换数。

> 所谓 leading/trailing：CPOL=0 时第一个边沿是上升沿；CPOL=1 时第一个边沿是下降沿。

四种模式常被称为 Mode 0~3：

| Mode | CPOL | CPHA | 空闲电平 | 采样边沿 |
| :--: | :--: | :--: | :------: | :------- |
| 0 | 0 | 0 | 低 | 第一个（上升） |
| 1 | 0 | 1 | 低 | 第二个（下降） |
| 2 | 1 | 0 | 高 | 第一个（下降） |
| 3 | 1 | 1 | 高 | 第二个（上升） |

**最重要的结论**：主机与从机**必须**配置成同一组 CPOL/CPHA，否则边沿对不齐，数据全部错位。

> ⚠️ 一个容易踩的坑：`olo_intf_spi_master` 的默认是 `SpiCpol_g=1, SpiCpha_g=1`（Mode 3），而 `olo_intf_spi_slave` 的默认是 `SpiCpol_g=0, SpiCpha_g=0`（Mode 0）。**两者默认值不一致！** 如果你不显式设置就把它们对接，模式不匹配，传输必然失败。这是第 5 节实践里要特别注意的点。

#### 4.1.2 核心流程

主机用 `getClockLevel()` 把抽象的“active/inactive”翻译成具体电平：

```
getClockLevel(ClkActive):
  if CPOL = 0:  active -> '1', inactive -> '0'   # 空闲低，active 是高电平
  if CPOL = 1:  active -> '0', inactive -> '1'   # 空闲高，active 是低电平
```

主机的 SCLK 在 `ClkInact_s`（inactive 电平）与 `ClkAct_s`（active 电平）两个状态间来回切换，从而生成方波。CPHA 决定“输出 MOSI”和“采样 MISO”分别落在哪个状态：

- **CPHA=0**：进入 `ClkInact_s` 时**输出** MOSI（提前摆好），进入 `ClkAct_s`（active 沿）时**采样** MISO。
- **CPHA=1**：进入 `ClkInact_s`（inactive 沿）时**采样** MISO，进入 `ClkAct_s` 时**输出** MOSI。

从机一侧则用边沿检测直接识别采样沿（`SampleEdge`）与发送沿（`TransmitEdge`），规则见 4.4.2。

#### 4.1.3 源码精读

主机把 CPOL 翻译成电平的函数 [olo_intf_spi_master.vhd:117-132](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L117-L132)：

```vhdl
function getClockLevel (ClkActive : boolean) return std_logic is
begin
    if SpiCpol_g = 0 then
        if ClkActive then return '1'; else return '0'; end if;
    else
        if ClkActive then return '0'; else return '1'; end if;
    end if;
end function;
```

复位时 SCLK 被置为 inactive 电平 [olo_intf_spi_master.vhd:288](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L288)，保证空闲电平符合 CPOL 约定。

CPHA 在主机状态机里的体现——`ClkInact_s` 与 `ClkAct_s` 中对输出/采样的分工 [olo_intf_spi_master.vhd:200-234](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L200-L234)：

```vhdl
when ClkInact_s =>
    v.Spi_Sclk := getClockLevel(false);
    if r.ClkDivCnt = 0 then
        if SpiCpha_g = 0 then  v.Spi_Mosi := r.MosiNext;          -- CPHA0: 这里输出
        else                  shiftReg(..., SpiMiso_i, ...); end if; -- CPHA1: 这里采样
    end if;
    ...
when ClkAct_s =>
    v.Spi_Sclk := getClockLevel(true);
    if r.ClkDivCnt = 0 then
        if SpiCpha_g = 1 then  v.Spi_Mosi := r.MosiNext;          -- CPHA1: 这里输出
        else                  shiftReg(..., SpiMiso_i, ...); end if; -- CPHA0: 这里采样
    end if;
```

> 注意 CPHA=0 还有一个额外的 `SftComp_s`（Shift Compensation）状态 [olo_intf_spi_master.vhd:193-198](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L193-L198)：因为 CPHA=0 要求**第一个**沿之前 MOSI 就得摆好，所以在真正开始前先做一次移位，把第一个待发比特算到 `MosiNext` 里。CPHA=1 不需要这个补偿。

从机用 `SpiCpol_g = SpiCpha_g` 这一个判断推导出两个沿 [olo_intf_spi_slave.vhd:147-154](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L147-L154)：

```vhdl
if SpiCpol_g = SpiCpha_g then
    SampleEdge_v  := SclkRe_v;   -- 上升沿采样
    TrasmitEdge_v := SclkFe_v;
else
    SampleEdge_v  := SclkFe_v;   -- 下降沿采样
    TrasmitEdge_v := SclkRe_v;
end if;
```

读者可以自行验证：把 (CPOL,CPHA) 四种组合代入，得到的 `SampleEdge` 与上表完全一致。

#### 4.1.4 代码实践

1. **实践目标**：直观确认四种模式下采样沿的差异。
2. **操作步骤**：阅读 [doc/intf/olo_intf_spi_master.md](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/doc/intf/olo_intf_spi_master.md) 里的 `spi_clk_data_phases.png` 说明；然后运行主机测试台，分别用 Mode 0 和 Mode 3 各跑一次：

   ```bash
   cd sim
   python run.py --ghdl -v -p olo_intf_spi_master_tb.SpiCpol_g=0,SpiCpha_g=0
   python run.py --ghdl -v -p olo_intf_spi_master_tb.SpiCpol_g=1,SpiCpha_g=1
   ```
   > `-p` 用法依本地 VUnit 版本可能需写成完整的 `*.generic=value` 形式；若不确定，可参考 `sim/test_configs/olo_intf.py` 已注册的具名配置直接运行。
3. **需要观察的现象**：两种模式下传输都通过；波形里 SCLK 的空闲电平不同（Mode 0 低、Mode 3 高）。
4. **预期结果**：两组用例均 `pass`。
5. 运行结果：**待本地验证**（命令未在本文中实跑）。

#### 4.1.5 小练习与答案

**练习 1**：若主机配 Mode 0、从机配 Mode 3，会发生什么？
**答案**：CPOL 不同导致空闲电平相反，CPHA 不同导致采样沿相反，两边在错误的时刻采样，数据全部错乱；功能上表现为收到的数据与发出毫无对应关系，严重时测试台直接比对失败。

**练习 2**：为什么主机需要一个 `SftComp_s` 状态而 CPHA=1 时不需要？
**答案**：CPHA=0 要求在**第一个 SCLK 沿之前** MOSI 就已摆好第一位；`SftComp_s` 在启动传输后先做一次移位，把第一位算到 `MosiNext`。CPHA=1 在第一个沿才换数，不需要预先摆好，故无此补偿。

---

### 4.2 SPI 主机配置（olo_intf_spi_master）

#### 4.2.1 概念说明

`olo_intf_spi_master` 把“如何产生 SPI 时序”这件复杂事，封装成一个**命令/响应**接口：用户只需提交一条命令（发给哪个从机、发多少位、发什么数据），实体就自动生成 SCLK、驱动 MOSI/CS_n、回采 MISO，传输结束后用一条响应把收到的数据交还。

它有几个值得强调的设计点：

- **多从机**：`SlaveCnt_g` 个从机共享一对 MOSI/MISO，靠独立的 CS_n 位区分；每条命令通过 `Cmd_Slave` 指定目标。
- **可变位宽**：`MaxTransWidth_g` 是位宽上限，而每条命令可用 `Cmd_TransWidth` 指定这次实际发几位（数据右对齐）。
- **SCLK 由数据信号分频得到**：不走 PLL，而是用 `Clk` 分频翻转一个普通寄存器，故实际频率受整数分频限制。
- **CS 保持**：`Cmd_CsHold=1` 可让 CS_n 在事务之间保持低，适配需要连续写入的存储器件。

#### 4.2.2 核心流程

主机是一个 5 状态 FSM（[olo_intf_spi_master.vhd:75](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L75)）：

```
Idle_s  ──Cmd_Valid──▶  SftComp_s  ──▶  ClkInact_s  ◄──▶  ClkAct_s   (逐比特切换)
                                                      │
                                   全部位发完 + CsHigh 时间  ▼
                                                  CsHigh_s ──▶ Idle_s (Done 脉冲)
```

要点：

1. `Idle_s` 收到命令，锁存数据/位宽/CS 保持，**仅**把选中从机的 CS_n 拉低。
2. `SftComp_s` 做 CPHA=0 的预移位补偿（见 4.1）。
3. `ClkInact_s` ↔ `ClkAct_s` 交替产生 SCLK 半周期；用一个半周期计数器 `ClkDivCnt` 控制 SCLK 频率。
4. 每个完整 SCLK 周期 `BitCnt` 加 1，直到发完 `TransWidth` 位。
5. `CsHigh_s` 保证 CS_n 高电平至少 `CsHighTime_g`，然后回 `Idle_s` 并发 `Done`（即 `Resp_Valid`）脉冲，把回采的 `shiftReg` 作为 `Resp_Data` 输出。

**SCLK 频率公式。** 半周期计数器从 0 数到 `ClkDivThres_c`，故每个 SCLK 半周期占 \((\text{ClkDivThres\_c}+1)\) 个 `Clk` 周期，整周期占 \(2(\text{ClkDivThres\_c}+1)\) 个周期，于是

\[
F_{\text{sclk}} = \frac{F_{\text{clk}}}{2 \cdot (\text{ClkDivThres\_c}+1)}
\]

由于分频系数必须为整数，实际频率可能与请求的 `SclkFreq_g` 有偏差。实体在 elaborate 阶段断言：偏差超过 10% 就报错。

#### 4.2.3 源码精读

泛型与端口 [olo_intf_spi_master.vhd:36-66](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L36-L66)：

```vhdl
generic (
    ClkFreq_g       : real;                  -- 系统时钟频率 (Hz)，须 ≥ 4 × SclkFreq_g
    SclkFreq_g      : real := 1.0e6;         -- 期望 SPI 时钟频率
    MaxTransWidth_g : positive := 32;        -- 单次事务最大位数
    CsHighTime_g    : real := 20.0e-9;       -- 两次事务间 CS_n 最小高电平时间
    SpiCpol_g       : natural range 0 to 1 := 1;
    SpiCpha_g       : natural range 0 to 1 := 1;
    SlaveCnt_g      : positive := 1;         -- 从机数量 = Spi_Cs_n 位宽
    LsbFirst_g      : boolean := false;
    MosiIdleState_g : std_logic := '0'
);
port (
    ...
    Cmd_Valid       : in  std_logic;         -- 命令 AXI-S 握手
    Cmd_Ready       : out std_logic;
    Cmd_Slave       : in  std_logic_vector(log2ceil(SlaveCnt_g)-1 downto 0) := (others => '0');
    Cmd_Data        : in  std_logic_vector(MaxTransWidth_g-1 downto 0) := (others => '0');
    Cmd_TransWidth  : in  std_logic_vector(log2ceil(MaxTransWidth_g+1)-1 downto 0) := toUslv(MaxTransWidth_g, ...);
    Cmd_CsHold      : in  std_logic := '0';
    Resp_Valid      : out std_logic;         -- 响应（不支持反压，无 Ready）
    Resp_Data       : out std_logic_vector(MaxTransWidth_g-1 downto 0);
    Spi_Sclk        : out std_logic;
    Spi_Mosi        : out std_logic;
    Spi_Miso        : in  std_logic := '0';
    Spi_Cs_n        : out std_logic_vector(SlaveCnt_g-1 downto 0)
);
```

注意几个**带默认值**的可选端口（`Cmd_Slave`/`Cmd_Data`/`Cmd_TransWidth`/`Cmd_CsHold`/`Spi_Miso`）：单从机、固定位宽、只写场景下都可悬空，这正是 Open Logic “Ease of Use” 哲学的体现。

分频与频率常量 [olo_intf_spi_master.vhd:78-82](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L78-L82)：

```vhdl
constant ClkDiv_c         : natural := integer(round(ClkFreq_g/SclkFreq_g));
constant ClkDivThres_c    : natural := ClkDiv_c / 2 - 1;
constant CsHighCycles_c   : natural := integer(ceil(ClkFreq_g*CsHighTime_g));
constant SclkFreqResult_c : real    := ClkFreq_g/(2.0*real(ClkDivThres_c+1));
```

频率偏差断言（>10% 报 error）[olo_intf_spi_master.vhd:156-158](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L156-L158)。

`Idle_s` 里“仅选中目标从机”的逻辑——先把所有 CS_n 置 1，再把目标位置 0 [olo_intf_spi_master.vhd:184-187](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L184-L187)：

```vhdl
v.Spi_Cs_n := (others => '1');
v.Spi_Cs_n(to_integer(unsigned(Cmd_Slave))) := '0';
```

注释解释了原因：即使上一笔用了 `Cmd_CsHold` 把某个从机的 CS_n 压低，这次选了不同从机时也要先把旧的复位。

`CsHigh_s` 的 CS 保持逻辑 [olo_intf_spi_master.vhd:244-248](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L244-L248)：

```vhdl
when CsHigh_s =>
    if r.CsHold = '0' then           -- 不要求保持，则拉高 CS_n
        v.Spi_Cs_n := (others => '1');
    end if;
    ...
```

忙/完成信号到端口的映射 [olo_intf_spi_master.vhd:271-273](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L271-L273)：`Cmd_Ready <= not r.Busy;`（忙时不收新命令）、`Resp_Valid <= r.Done;`（单周期响应脉冲）。

#### 4.2.4 代码实践

1. **实践目标**：体会“命令→响应”的解耦与多从机选择。
2. **操作步骤**：阅读测试台 [olo_intf_spi_master_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_master/olo_intf_spi_master_tb.vhd)，重点看 `sendCommand()` 与 `checkResponse()` 两个过程，以及 `SlaveSelection` 用例（[olo_intf_spi_master_tb.vhd:189-201](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_master/olo_intf_spi_master_tb.vhd#L189-L201)）如何对从机 1、从机 0 各发一笔。
3. **需要观察的现象**：发命令时 `Cmd_Ready` 为 1，握手后立即变 0（`Busy` 置位）；传输完成后 `Resp_Valid` 脉冲一拍，`Resp_Data` 正好等于 VC 注入的 MISO 数据。
4. **预期结果**：用例 `FullWidthTransfer`、`ReducedWidthTransfer`、`SlaveSelection`、`ContinuousTransfersWithCsLow` 均通过。
5. 运行结果：**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Cmd_TransWidth` 端口的默认值是什么？含义是什么？
**答案**：默认值为 `toUslv(MaxTransWidth_g, ...)`，即每次都发满 `MaxTransWidth_g` 位。所以“所有事务等宽”时该端口可不连。

**练习 2**：连续两笔都设 `Cmd_CsHold=1`，但第一笔发给从机 0、第二笔发给从机 1，CS_n 会怎样？
**答案**：第二笔注入时，`Idle_s` 会先把全部 CS_n 置 1 再选中从机 1（见上文源码），所以从机 0 的 CS_n 会被拉高——这正是注释里强调的“换从机时可检测到 CS 变化”。测试台 `SlaveSwitchWithCsHold` 用例覆盖了这一情形。

---

### 4.3 LSB/MSB 顺序与移位寄存器实现

#### 4.3.1 概念说明

SPI 一次传 N 位，但“先发高位还是先发低位”没有强制规定，由 `LsbFirst_g` 决定：

- **MSB first（`LsbFirst_g=false`，默认）**：先发最高位（bit N-1），最后发 bit 0。
- **LSB first（`LsbFirst_g=true`）**：先发最低位（bit 0），最后发 bit N-1。

主从双方的位序也必须一致，否则即使 CPOL/CPHA 对齐，比特顺序也会反过来。

Open Logic 用一个**双向移位寄存器**实现两种位序：同一份存储阵列，根据 `LsbFirst_g` 选择移位方向。这样只多花一点点选择逻辑，就避免了维护两套数据通路。

#### 4.3.2 核心流程

主机把“输出一比特 + 采入一比特”封装成一个过程 `shiftReg`：

```
shiftReg(BeforeShift, InputBit, TransWidth) -> (AfterShift, OutputBit):
  if LsbFirst:                       # LSB 先发
      OutputBit = BeforeShift(0)                  # 取最低位发出
      AfterShift = '0' & BeforeShift(high downto 1)   # 整体右移
      AfterShift(TransWidth-1) = InputBit            # 收到的比特放回有效域最高位
  else:                              # MSB 先发
      OutputBit = BeforeShift(TransWidth-1)       # 取有效域最高位发出
      AfterShift = BeforeShift(high-1 downto 0) & InputBit  # 整体左移，收到的放最低位
```

关键点：移位只在**有效域** `0 .. TransWidth-1` 内进行，因此支持“可变位宽”——未用的高位被一次性右移挤掉，不参与收发。

从机的移位逻辑在 `WaitSampleEdge_s` 里就地实现（不走过程），原理相同，只是它按 `TransWidth_g` 固定位宽移满整个寄存器。

#### 4.3.3 源码精读

主机的 `shiftReg` 过程 [olo_intf_spi_master.vhd:134-149](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_master.vhd#L134-L149)：

```vhdl
procedure shiftReg (...) is
begin
    if LsbFirst_g then
        OutputBit                := BeforeShift(0);                       -- 发 LSB
        AfterShift               := '0' & BeforeShift(BeforeShift'high downto 1);
        AfterShift(TransWidth-1) := InputBit;                             -- 采入位放回有效域顶端
    else
        OutputBit  := BeforeShift(TransWidth-1);                          -- 发有效域 MSB
        AfterShift := BeforeShift(BeforeShift'high - 1 downto 0) & InputBit;
    end if;
end procedure;
```

从机在采样沿就地移位 [olo_intf_spi_slave.vhd:203-211](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L203-L211)：

```vhdl
when WaitSampleEdge_s =>
    if SampleEdge_v = '1' then
        if LsbFirst_g = false then
            v.ShiftReg    := r.ShiftReg(r.ShiftReg'high-1 downto 0) & SpiMosi_i;  -- 左移，MOSI 进 bit0
            v.SpiMisoData := r.ShiftReg(r.ShiftReg'high-1);                        -- 下一个发 MSB
        else
            v.ShiftReg    := SpiMosi_i & r.ShiftReg(r.ShiftReg'high downto 1);    -- 右移，MOSI 进顶端
            v.SpiMisoData := r.ShiftReg(1);                                        -- 下一个发 LSB
        end if;
```

从机首发比特由常量 `TxIdx_c` 选定 [olo_intf_spi_slave.vhd:105](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L105)：`constant TxIdx_c : integer := choose(LsbFirst_g, 0, TransWidth_g - 1);`——MSB 先发时取最高位，LSB 先发时取 bit 0。

#### 4.3.4 代码实践

1. **实践目标**：验证 MSB/LSB 两种位序下，收发比特顺序相反但数据语义正确。
2. **操作步骤**：查看 `sim/test_configs/olo_intf.py` 中主机位序遍历 [olo_intf.py:102-103](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L102-L103) 与从机位序遍历 [olo_intf.py:71-72](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L71-L72)；各跑一次：

   ```bash
   cd sim
   python run.py --ghdl -p '*olo_intf_spi_master_tb*LsbFirst_g=True*'
   python run.py --ghdl -p '*olo_intf_spi_master_tb*LsbFirst_g=False*'
   ```
3. **需要观察的现象**：两种位序下测试均通过（因为主从 VC 也配了相同位序）。
4. **预期结果**：两组用例 `pass`。
5. 运行结果：**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么主机移位时要区分“整个寄存器”和“有效域 `TransWidth`”？
**答案**：因为位宽可变。`Cmd_Data` 右对齐，只有低 `TransWidth` 位有效；移位时把有效域之外的位当作可丢弃的填充，逐位挤出，从而保证无论传几位，有效数据都正确收发。

**练习 2**：主机 MSB-first 发 `0x80`（8 位）时，第一个 SCLK 周期 MOSI 上是什么电平？
**答案**：`0x80` = `1000_0000`，MSB-first 先发最高位，故 MOSI 第一拍为 `1`。

---

### 4.4 SPI 从机配置（olo_intf_spi_slave）

#### 4.4.1 概念说明

`olo_intf_spi_slave` 与主机互补：它不产生时钟，而是**被动响应**外部主机的 SCLK/CS_n。因为 SPI 是全双工，从机在收 MOSI 的同时要把待发的 TX 数据从 MISO 送出。这带来几个特有设计：

- **三组用户接口**：RX（收到的数据）、TX（要发的数据）、Response（告知本次事务结局）。
- **TX 提前锁存**：因为 MISO 第一位必须准时出现，TX 数据在事务一开始（CS_n 下降沿）就被锁进移位寄存器，来不及就发 0。
- **输入同步**：SCLK/MOSI/CS_n 全是异步输入，内部用 `olo_intf_sync` 同步，导致约 4 个 `Clk` 周期的传播延迟。
- **同沿收发**：与标准 SPI 不同，从机**采样 MOSI 与更新 MISO 在同一个沿**——这样能让 MISO 尽早出现，最大化可支持的 SCLK 频率。
- **连续事务**：`ConsecutiveTransactions_g=true` 时支持 CS_n 不抬高的多笔事务（典型如某些存储器读时序）。
- **MISO 三态**：内部三态（`Spi_Miso` 直接驱动，含 `'Z'`）或外部三态（`Spi_Miso_o` + `Spi_Miso_t`），适配不同 FPGA 的 IO 结构。

> ⚠️ SCLK 频率上限：`Clk` 必须至少是 SCLK 的 **10 倍**（用些技巧可达 8 倍），否则同步+边沿检测的 4 拍延迟会让 MISO 来不及摆好。

#### 4.4.2 核心流程

从机是 5 状态 FSM（[olo_intf_spi_slave.vhd:74](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L74)）：

```
Idle_s  ──CS_n下降沿──▶  LatchTx_s  ──数据就绪/发送沿──▶  WaitSampleEdge_s
                                                            │
                                       (每位) 采样 MOSI、更新 MISO、BitCnt++
                                                            │
                                              未到最后一位 ──▶ WaitInactiveEdge_s ──发送沿──┐
                                                            ▲                               │
                                                            └───────────────────────────────┘
                                              最后一位采样完
                                                            │
                                  (Consecutive)──▶ LatchTx_s      (否则)──▶ WaitCsHigh_s ──CS_n高──▶ Idle_s
```

关键细节：

1. **CS_n 下降沿触发** `LatchTx_s`，拉高 `Tx_Ready` 向用户索要 TX 数据。
2. **TX 锁存窗口**：用户必须在 `Tx_Ready` 还高时给 `Tx_Valid`；错过就把 ShiftReg 填 0。CPHA=0 时窗口只有 1 拍（MISO 必须随 CS_n 下降立即有效），CPHA=1 或连续事务的非首笔窗口更长。
3. **采样沿**（`WaitSampleEdge_s`）：采入 MOSI、更新 MISO；最后一位采样完发 `Resp_Sent`，并（若开启连续）回到 `LatchTx_s`，否则等 CS_n 抬高。
4. **CS_n 抬高检测**（FSM 之后统一处理）：若在 `WaitCsHigh_s`/`Idle_s` 之外被抬高，说明事务被主机中途打断 → `Resp_Aborted`；正常结束 → `Resp_CleanEnd`。
5. **RX 输出**：`Rx_Valid` 为**单周期**脉冲，**不支持反压**，用户必须当拍取走。

#### 4.4.3 源码精读

泛型与端口 [olo_intf_spi_slave.vhd:32-65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L32-L65)。注意默认 Mode 0、`ConsecutiveTransactions_g=false`、`InternalTriState_g=true`，且 `Tx_Valid`/`Tx_Data` 都有默认值（只收不发时可悬空）：

```vhdl
generic (
    TransWidth_g              : positive := 32;
    SpiCpol_g                 : natural range 0 to 1 := 0;
    SpiCpha_g                 : natural range 0 to 1 := 0;
    LsbFirst_g                : boolean := false;
    ConsecutiveTransactions_g : boolean := false;
    InternalTriState_g        : boolean := true
);
```

TX 锁存与首发比特 [olo_intf_spi_slave.vhd:170-201](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L170-L201)：

```vhdl
when LatchTx_s =>
    if Tx_Valid = '1' and r.Tx_Ready = '1' then
        v.ShiftReg    := Tx_Data;        -- 锁存待发数据
        v.Tx_Ready    := '0';
        v.DataLatched := '1';
        v.SpiMisoData := Tx_Data(TxIdx_c);  -- 立即摆好第一位
    end if;
    if LeaveState_v then
        if r.DataLatched = '0' and Tx_Valid = '0' then
            v.ShiftReg := (others => '0');   -- 来不及给数据 -> 发 0
        end if;
        v.SpiMisoData     := v.ShiftReg(TxIdx_c);
        v.SpiMisoTristate := '0';            -- 开始驱动 MISO
    end if;
```

最后一位采样 + Response [olo_intf_spi_slave.vhd:212-229](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L212-L229)：抑制末尾多余的 MISO 翻转、置 `Resp_Sent`，并按是否连续决定下一状态。

CS_n 抬高与中止判定（FSM 之后）[olo_intf_spi_slave.vhd:258-271](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L258-L271)：MISO 三态收回；若事务未正常结束且有数据在发 → `Resp_Aborted`，否则 `Resp_CleanEnd`。

MISO 三态两种实现 [olo_intf_spi_slave.vhd:289-297](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L289-L297)：

```vhdl
g_int_tristate : if InternalTriState_g generate
    Spi_Miso <= r.SpiMisoData when r.SpiMisoTristate = '0' else 'Z';
end generate;
g_ext_tristate : if not InternalTriState_g generate
    Spi_Miso_o <= r.SpiMisoData;
    Spi_Miso_t <= r.SpiMisoTristate;   -- '1'=高阻, '0'=驱动
end generate;
```

输入同步——把 SCLK/MOSI/CS_n 打包成 3 位向量过 `olo_intf_sync`（`RstLevel_g='1'`，保证复位期 CS_n 视为未选中）[olo_intf_spi_slave.vhd:320-345](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_spi_slave.vhd#L320-L345)：

```vhdl
b_sync : block is
    signal SyncIn, SyncOut : std_logic_vector(2 downto 0);
begin
    SyncIn(0) <= Spi_Sclk;
    SyncIn(1) <= Spi_Mosi;
    SyncIn(2) <= Spi_Cs_n;
    i_sync : entity work.olo_intf_sync
        generic map ( Width_g => 3, RstLevel_g => '1' )
        port map ( Clk => Clk, Rst => Rst, DataAsync => SyncIn, DataSync => SyncOut );
    SpiSclk_i <= SyncOut(0);
    SpiMosi_i <= SyncOut(1);
    SpiCsn_i  <= SyncOut(2);
end block;
```

> 这正解释了 4 拍传播延迟（2 拍同步 + 1 拍边沿检测 + 1 拍输出），也解释了为什么文档要求**主机在 CS_n 下降沿与第一个 SCLK 采样沿之间至少留 5 个 `Clk`**。

#### 4.4.4 代码实践

1. **实践目标**：理解 TX 锁存窗口与中止响应。
2. **操作步骤**：阅读从机测试台 [olo_intf_spi_slave_tb.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_slave/olo_intf_spi_slave_tb.vhd) 的 `CSnHighDuringTransaction` 用例（[olo_intf_spi_slave_tb.vhd:439-479](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_spi_slave/olo_intf_spi_slave_tb.vhd#L439-L479)）：VC 故意在传输到 `TransWidth_g-2` 位时抬 CS_n，期望 `Resp_Aborted`。
3. **需要观察的现象**：中止那笔只看到 `Resp_Aborted` 脉冲、没有 `Rx_Valid`（数据未收满）；紧接着的正常事务则有 `Resp_Sent` + `Resp_CleanEnd` + 正确 `Rx_Data`。
4. **预期结果**：用例通过，Response 队列按 `Aborted`、`Sent`、`CleanEnd` 顺序匹配。
5. 运行结果：**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么从机采样和发送用同一个沿，而标准 SPI 是分开的？
**答案**：从机内部 SCLK→MISO 有 4 拍延迟，MISO 的建立时间（setup）是瓶颈、保持时间（hold）很富余。让 MISO 在采样沿后立刻更新（4 拍后出现在线上），能给主机尽可能长的采样窗口，从而支持更高的 SCLK 频率。

**练习 2**：`ConsecutiveTransactions_g=false` 时，主从做了 3 笔连发（CS_n 始终低），从机会怎样？
**答案**：只有第一笔能正常收发；之后由于 CS_n 未抬高，从机不会回到 `LatchTx_s` 锁存新数据，后续比特被忽略，最终 CS_n 抬高时报 `Resp_Aborted`（或行为不确定）。需要连发就必须开启该泛型。

**练习 3**：`Spi_Miso` 空闲时是什么状态？
**答案**：高阻 `'Z'`（`SpiMisoTristate='1'`），复位值也是 `'Z'`（见测试台 `ResetValues` 用例对 `Spi_Miso='Z'` 的断言）。

## 5. 综合实践

把本讲知识串起来：**亲手把真实的主机 RTL 与从机 RTL 对接**，跑一次全双工传输，验证“主机发出的数据 = 从机收到（RX）的数据”、“从机发出的数据 = 主机收到（Resp）的数据”。

### 5.1 实践目标

- 巩固“主从 CPOL/CPHA/位序必须一致”。
- 亲手完成 SPI 四线（SCLK/MOSI/MISO/CS_n）的对接。
- 理解主机的命令/响应与从机的 TX/RX 接口的对应关系。

### 5.2 操作步骤

写一个最小 VUnit 测试台 `olo_intf_spi_loopback_tb.vhd`（**示例代码**，非仓库原有文件，仅作示意，未实跑）：

```vhdl
-- 示例代码：仅示意主从对接的接线，省略 VUnit 框架细节
constant CLK_FREQ_c : real := 100.0e6;          -- 100 MHz
constant SCLK_FREQ_c: real := 1.0e6;            -- 1 MHz（满足从机 Clk ≥ 10×SCLK，且 CS 建立余量充足）
constant WIDTH_c    : positive := 8;

signal Clk : std_logic := '0';
signal Rst : std_logic := '0';

-- 主机命令/响应
signal m_Cmd_Valid, m_Cmd_Ready, m_Resp_Valid : std_logic;
signal m_Cmd_Data, m_Resp_Data : std_logic_vector(WIDTH_c-1 downto 0);

-- 从机 TX/RX
signal s_Tx_Valid, s_Tx_Ready, s_Rx_Valid : std_logic;
signal s_Tx_Data, s_Rx_Data : std_logic_vector(WIDTH_c-1 downto 0);

-- SPI 四线
signal Spi_Sclk, Spi_Mosi, Spi_Miso : std_logic;
signal Spi_Cs_n : std_logic_vector(0 downto 0);

-- *** 主机：显式配 Mode 0，覆盖默认的 Mode 3 ***
i_master : entity olo.olo_intf_spi_master
    generic map (
        ClkFreq_g       => CLK_FREQ_c,
        SclkFreq_g      => SCLK_FREQ_c,
        MaxTransWidth_g => WIDTH_c,
        SpiCpol_g       => 0,            -- 与从机一致！
        SpiCpha_g       => 0,            -- 与从机一致！
        LsbFirst_g      => false,
        SlaveCnt_g      => 1)
    port map (
        Clk => Clk, Rst => Rst,
        Cmd_Valid => m_Cmd_Valid, Cmd_Ready => m_Cmd_Ready, Cmd_Data => m_Cmd_Data,
        Resp_Valid => m_Resp_Valid, Resp_Data => m_Resp_Data,
        Spi_Sclk => Spi_Sclk, Spi_Mosi => Spi_Mosi, Spi_Miso => Spi_Miso,
        Spi_Cs_n => Spi_Cs_n);

-- *** 从机：默认即 Mode 0，这里也显式写出 ***
i_slave : entity olo.olo_intf_spi_slave
    generic map (
        TransWidth_g => WIDTH_c,
        SpiCpol_g    => 0, SpiCpha_g => 0, LsbFirst_g => false)
    port map (
        Clk => Clk, Rst => Rst,
        Rx_Valid => s_Rx_Valid, Rx_Data => s_Rx_Data,
        Tx_Valid => s_Tx_Valid, Tx_Ready => s_Tx_Ready, Tx_Data => s_Tx_Data,
        Spi_Sclk => Spi_Sclk, Spi_Mosi => Spi_Mosi, Spi_Cs_n => Spi_Cs_n(0),
        Spi_Miso => Spi_Miso);   -- InternalTriState_g=true，直连即可

-- SPI 四线对接关系：
--   Spi_Sclk    : 主机 out -> 从机 in
--   Spi_Mosi    : 主机 out -> 从机 in
--   Spi_Cs_n(0) : 主机 out -> 从机 in
--   Spi_Miso    : 从机 out -> 主机 in
```

激励流程（伪代码）：

1. 复位；给从机 `Tx_Data=0x5A`、`Tx_Valid=1`（等待 `Tx_Ready` 握手后撤掉）。
2. 给主机 `Cmd_Data=0xA5`、`Cmd_TransWidth=8`、`Cmd_Valid=1`，握手后撤掉。
3. 等到 `m_Resp_Valid` 脉冲：检查 `m_Resp_Data = 0x5A`（主机收到的 = 从机发出的）。
4. 检查从机 `s_Rx_Valid` 脉冲：`s_Rx_Data = 0xA5`（从机收到的 = 主机发出的）。

### 5.3 需要观察的现象

- 全双工：同一笔 8 位传输里，`0xA5` 从主机流向从机、`0x5A` 从从机流向主机，互不干扰。
- 主机发完才出 `Resp_Valid`；从机在最后一位采样沿后才出 `Rx_Valid`。

### 5.4 预期结果

- `m_Resp_Data = 0x5A`、`s_Rx_Data = 0xA5`，两者都满足则全双工交换正确。
- 若故意把主机改成 `SpiCpha_g=1`（其余不变），应观察到数据错乱——以此反证“模式必须一致”。

### 5.5 运行结果

**待本地验证**。本实践为示例代码，未在本文中实跑；建议在 `sim/` 下仿照现有 TB 注册一个用例运行 `python run.py`。也可先运行仓库自带的主、从测试台确认环境就绪：

```bash
cd sim
python run.py --ghdl -p '*olo_intf_spi_master_tb*'
python run.py --ghdl -p '*olo_intf_spi_slave_tb*'
```

## 6. 本讲小结

- SPI 是同步、串行、全双工协议，靠 SCLK 边沿对齐；主机产生 SCLK 与 CS_n，从机被动响应。
- **CPOL/CPHA** 定义 4 种时钟模式，主机用 `getClockLevel()` 与状态分工实现，从机用 `SpiCpol_g=SpiCpha_g` 判采样沿；**主从必须配同一模式**——注意主机默认 Mode 3、从机默认 Mode 0，默认值不一致。
- **主机** `olo_intf_spi_master`：命令/响应接口，支持多从机（`SlaveCnt_g`）、可变位宽（`Cmd_TransWidth`）、CS 保持（`Cmd_CsHold`）；SCLK 由 `Clk` 整数分频得到，频率偏差 >10% 会断言报错。
- **位序** LSB/MSB 由一个双向移位寄存器实现（主机用 `shiftReg` 过程、从机就地移位），主从位序也须一致。
- **从机** `olo_intf_spi_slave`：RX/TX/Response 三接口，TX 在 CS_n 下降沿提前锁存，输入经 `olo_intf_sync` 同步（约 4 拍延迟），采样与发送同沿以提速；支持连续事务与内/外三态 MISO。
- 主从对接四线：SCLK/MOSI/CS_n 主→从，MISO 从→主；`Clk` 至少为 SCLK 的 10 倍。

## 7. 下一步学习建议

- **横向扩展接口**：继续学习 [u7-l4 I2C 主机](u7-l4-i2c-master.md)，对比 I2C 的多主仲裁/时钟拉伸与 SPI 的全双工主从模型差异。
- **夯实基础**：若对从机内部的同步器与边沿检测不够熟，回顾 [u7-l1 同步、消抖与时钟测量](u7-l1-sync-debounce-clkmeas.md) 中的 `olo_intf_sync`。
- **工程化验证**：学完本区域后可进入 [u10-l1 VUnit 测试台与验证组件](u10-l1-vunit-tb-and-vcs.md)，理解本讲引用的 `olo_test_spi_master_vc` / `olo_test_spi_slave_vc` 这类 VC 是如何用 VUnit 通信机制模拟对端的。
- **源码延伸阅读**：对比 [olo_intf_uart.vhd](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_uart.vhd)（异步、靠波特率各自计时）与本讲 SPI（同步、共享时钟），体会“同步 vs 异步”接口在设计上的取舍。
