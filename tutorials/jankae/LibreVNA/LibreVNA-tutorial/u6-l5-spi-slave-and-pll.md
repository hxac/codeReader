# u6-l5 MCU-FPGA 接口：SPI 从机与 PLL 控制

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `spi_slave.vhd` 与 `SPIConfig.vhd`（实体名 `SPICommands`）如何共同构成一个「16 位命令字 + 变长数据字」的寄存器式命令接口，并说出读命令为什么必须在命令字的**前 3 位**就被解码。
2. 独立整理出完整的 FPGA 命令码表（8 条命令）与寄存器映射表（0x00–0x15），并与 `FPGA_protocol.tex` 文档、固件侧 `FPGA.cpp` 三方互相印证。
3. 读懂 `MAX2871.vhd` 如何用一个小状态机把 4 个 32 位影子寄存器按 datasheet 顺序串行移出给 PLL，并估算一次重载的耗时。
4. 读懂 `Test_SPICommands.vhd` 测试台的激励结构，掌握「无硬件验证 FPGA 逻辑」的方法。
5. 完成一个编码设计练习：为「读取当前扫描点编号」这条假想命令设计编码方案。

本讲是单元 6 的第 5 篇，承接 u6-l1（top.vhd 顶层与「SPI＋寄存器堆」总览）与 u6-l2（Sweep 状态机、PLL 握手与跨时钟域同步）。本讲下钻到那两张「网」之间的**结点**：命令究竟如何进入 FPGA、PLL 寄存器究竟如何被写出去。

## 2. 前置知识

### 2.1 SPI 四线制与 CPOL/CPHA

SPI 是主从式同步串行总线，四根信号线：

- **SCK/SCLK**：时钟，由主设备（这里是 STM32 MCU）驱动。
- **MOSI**：主出从入，主设备发给从设备的数据。
- **MISO**：主入从出，从设备（FPGA）发回的数据。
- **NSS/CS**：片选，低电平有效，一次传输以拉低开始、拉高结束。

LibreVNA 用的是「时钟空闲低、上升沿采样」的模式。一个关键常识：**SPI 是全双工的**——主设备发 1 个字的同时必然收 1 个字，哪怕内容是无关的填充。本讲的命令接口正是利用了这一点：MCU 发命令字的同一个 16 位时间里，FPGA 把中断状态字送回来，一次传输两不耽误。

### 2.2 寄存器式命令接口

「寄存器式」协议指：外设对外呈现为一组可读写的寄存器（外加少数特殊命令），主设备通过「写寄存器改配置、读寄存器取状态」控制外设。这与 u4 单元讲的 USB 二进制协议是同一思想在不同总线上的投影。FPGA 侧的特殊之处：**时序极其紧张**——主设备下一个字的第一位马上就到，从设备必须提前把响应数据备好，这是本讲反复出现的主题。

### 2.3 本讲要用到的既有结论

来自 u6-l1：MCU 与 FPGA 之间只有一组 SPI 引脚，`AUX1`/`AUX2` 两根选择线决定这组引脚当前连到「FPGA 内部从机」还是「直通到某颗 MAX2871」。来自 u6-l2：Sweep 状态机在换点时要与 PLL 重载握手（等移位完成＋锁定），外域异步信号必须经两级同步器进入 102.4 MHz 主时钟域。

术语预告：**影子寄存器**（shadow register，先在本地攒好一整套值、再一次性提交）、**testbench**（VHDL 仿真测试台）、**MSB first**（高位先发）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [FPGA/VNA/spi_slave.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd) | 通用 SPI 从机：按位收发、跨时钟域传递、「前 N 位预解码」提示信号 |
| [FPGA/VNA/SPIConfig.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd) | 命令分发器＋寄存器堆（注意：文件名叫 SPIConfig，实体名是 `SPICommands`） |
| [FPGA/VNA/MAX2871.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd) | PLL 寄存器写入器：把 4 个 32 位寄存器串行移出给 MAX2871 芯片 |
| [FPGA/VNA/Test_SPICommands.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd) | SPICommands 的仿真测试台，六个激励场景 |
| [FPGA/VNA/top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) | 实例化以上模块，并用 AUX1/AUX2 复用同一组 SPI 引脚（本讲的接线图） |
| [FPGA/VNA/Sweep.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd) | 消费方：把「默认寄存器 + 逐点配置」拼成完整 PLL 寄存器（u6-l2 已精读） |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) / [.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp) | 主机（MCU）侧：每条命令对应的 C++ 函数，是核对编码的最佳对照物 |
| [Documentation/DeveloperInfo/FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) | 官方协议文档（LaTeX 源码），成文的命令/寄存器规范 |

## 4. 核心概念与源码讲解

### 4.1 SPI 从机与命令分发

#### 4.1.1 概念说明

MCU 指挥 FPGA 的全部入口就是 `SPIConfig.vhd` 里的 `SPICommands` 实体。它内部例化了一个通用从机 `spi_slave`，从机只干「按位收发」这件纯体力活，命令语义全部由 `SPICommands` 的状态机解释。二者职责切分非常干净：

- `spi_slave`：把 SCLK/MOSI 上的串行位流拼成 16 位字（`BUF_OUT`），把要回送的 16 位字（`BUF_IN`）拆成位流发到 MISO；并把「收到一个完整字」(`COMPLETE`) 与「收到前 `PREWIDTH` 位」(`PRE_COMPLETE`) 两个事件从 SPI 时钟域安全地递交给 102.4 MHz 系统时钟域。
- `SPICommands`：维护四状态机（`FirstWord`/`WriteSweepConfig`/`ReadResult`/`WriteRegister`），在 NSS 拉低后的第一个字（命令字）里识别高 3 位操作码，决定后续字的含义；同时维护一张约 20 项的寄存器映射表和一个中断状态聚合器。

为什么需要 `PRE_COMPLETE`？这是本模块最精妙的一点。源码注释写得很直白：

> processing the complete word after it is complete leaves very little time for read operations. Indicate when the first PREWIDTH bits are ready which allows more time to prepare the response to the next word

（[spi_slave.vhd:L44-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L44-L47)）

读命令的响应必须作为**下一个字**发回。如果等 16 位命令字全部收完才开始准备响应，到下一个字第一位只剩零点裕量；而在第 3 位就解出操作码（3 位恰好覆盖 8 种命令），后面还有 13 个位时间（约 13 × 23.5 ns ≈ 306 ns，折合 30 多个系统时钟周期）可供加载响应。

#### 4.1.2 核心流程

一次典型 SPI 事务（NSS 拉低 → 若干 16 位字 → NSS 拉高）的时序骨架：

```text
NSS 下降沿（SPICommands 在系统时钟域检测到）
 ├─ word_cnt ← 0，state ← FirstWord
 ├─ spi_buf_in ← interrupt_status      ← 命令字期间 MISO 回送的就是状态字！
 └─ 从机 bit_cnt 复位

命令字（16 位，MSB first）
 ├─ 第 3 位收完：PRE_COMPLETE 脉冲，SPICommands 解码高 3 位
 │    "010" → 回送固定识别字 0xF0A5
 │    "101" → 锁存 DFT 结果，进入 ReadResult
 │    "110" → 锁存采样结果，进入 ReadResult（并清"未读数据"标志）
 │    "111" → 锁存 ADC min/max，进入 ReadResult
 └─ 第 16 位收完：COMPLETE 脉冲，写类命令在此解码
      "000" → 进入 WriteSweepConfig，低 13 位是扫描点号
      "001" → 发出一个时钟宽的 SWEEP_RESUME 脉冲
      "011" → 发出一个时钟宽的 RESET_MINMAX 脉冲
      "100" → 进入 WriteRegister，低 5 位是寄存器地址

后续字
 ├─ WriteRegister：每字写入选中寄存器，地址自动 +1（可一串连写）
 ├─ WriteSweepConfig：移满 6 字后拼成 96 位，发 SWEEP_WRITE 脉冲
 └─ ReadResult：每个后续字的 PRE_COMPLETE 取锁存结果的下一个 16 位
```

读路径的诀窍在于 `latched_result`（288 位锁存器）：命令字解码时把大块结果一次性锁进来并送出最低 16 位；之后每个后续字到来时再把锁存器右移 16 位取下一截——数据在 FPGA 内部一字排开，MISO 一字一字往外挤。

跨时钟域方面沿用 u6-l2 的套路：`data_valid` 在 SPI 时钟域置位，经 `data_valid(2 downto 1) <= data_valid(1 downto 0)` 两级打拍进入系统时钟域，而数据本身 `data` 在标志同步到位之前早已稳定——这是典型的「标志同步器 + 稳定数据」CDC 模式。

#### 4.1.3 源码精读

**① 从机的收发与预解码**（[spi_slave.vhd:L91-L130](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L91-L130)）：

```vhdl
slave_in: process(SPI_CLK, CS)
begin
	if CS = '1' then
		bit_cnt <= 0;
		...
	elsif rising_edge(SPI_CLK) then
		-- data input process: sample on the rising edge
		if bit_cnt = PREWIDTH-1 then
			pre_data <= mosi_buffer(PREWIDTH-2 downto 0) & MOSI;  -- 前 3 位就绪
			pre_data_valid(0) <= '1';
		end if;
		if bit_cnt = W-1 then
			data <= mosi_buffer(W-2 downto 0) & MOSI;             -- 整字就绪
			data_valid(0) <= '1';
		else
			mosi_buffer <= mosi_buffer(W-3 downto 0) & MOSI;      -- 左移拼字
		end if;
		...
```

这段在 SPI 时钟域完成：CS 高电平异步复位位计数器（一次 NSS 周期就是一次字的边界对齐）；每个 SCLK 上升沿把 MOSI 左移进 `mosi_buffer`，数到第 3 位先交出 `pre_data`，数到第 16 位交出完整 `data`。

**② MISO 的发射时机**（[spi_slave.vhd:L89](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L89) 与 [L117-L128](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L117-L128)）：

```vhdl
MISO <= BUF_IN(15) when bit_cnt = 0 else miso_buffer(W-2);
...
-- data output process: data should be launched on the falling edge
-- but the delay is too large. Launch on the rising edge instead
if bit_cnt = 0 then
	miso_buffer <= BUF_IN;      -- 每个字的第 0 位时刻装载响应字
else
	miso_buffer <= miso_buffer(W-2 downto 0) & '0';
end if;
```

教科书式 SPI 从机应在 SCK 下降沿更新 MISO（给主设备半周期建立时间）。这里作者故意改为**上升沿更新**：若在下降沿更新，MISO 数据只稳定半周期就要被主设备采样，FPGA 输出路径延迟会把这半周期吃光；改为上升沿更新后，除第 0 位（NSS 一拉低就由 `BUF_IN(15)` 直通输出）外，每一位都稳定整整一个 SCK 周期。代价是相对主设备采样沿的保持裕度变小——这是 42.5 MHz 高速下的务实取舍。

**③ 状态字在命令字期间回送**（[SPIConfig.vhd:L211-L215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L211-L215)）：

```vhdl
last_NSS <= NSS;
if NSS = '0' and last_NSS = '1' then
	word_cnt <= 0;
	spi_buf_in <= interrupt_status;   -- 命令字期间 MISO 送出状态字
	state <= FirstWord;
```

而 `interrupt_status` 每个时钟周期都在重新聚合（[SPIConfig.vhd:L191](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L191)）：

```vhdl
interrupt_status <= DEBUG_STATUS(10 downto 1) & DFT_RESULT_READY
                    & SWEEP_HALTED & data_overrun & unread_sampling_data
                    & SOURCE_UNLOCKED & LO_UNLOCKED;
```

拼接从左到右对应 bit15…bit0，因此状态字位布局为：bit0=LO 失锁、bit1=源失锁、bit2=新数据(ND)、bit3=数据溢出(OR)、bit4=扫描暂停(SH)、bit5=DFT 就绪、bit15:6=透传 Sweep 的调试状态。中断线只在「状态 ∧ 屏蔽字」非零时拉起（[SPIConfig.vhd:L192-L196](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L192-L196)），屏蔽字正是 0 号寄存器。

**④ 读命令在 3 位处解码**（[SPIConfig.vhd:L217-L245](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L217-L245)），节选两条：

```vhdl
if spi_pre_complete = '1' then
case state is
when FirstWord =>
	case spi_pre_buf_out is
		when "010" => state <= FirstWord;
			spi_buf_in <= "1111000010100101";     -- 识别字 0xF0A5
		when "110" => state <= ReadResult;
			latched_result <= SAMPLING_RESULT(303 downto 16);
			spi_buf_in <= SAMPLING_RESULT(15 downto 0);  -- 先送最低字
			unread_sampling_data <= '0';
	...
when ReadResult =>
	spi_buf_in <= latched_result(15 downto 0);   -- 逐字右移取下一截
	latched_result <= "0000000000000000" & latched_result(287 downto 16);
```

注意 `"110"`（读采样结果）先送 `SAMPLING_RESULT(15 downto 0)`——**最低有效字在前**。协议文档也明说 "transmitted with the least significant word first"（[FPGA_protocol.tex:L158](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L158)），但文档的字段列表是从 bit305 往 bit0 排的，初读很容易把传输顺序弄反——固件 `HAL_SPI_TxRxCpltCallback` 里用收到的**最后一对字节**拼 `pointNum`（bit303:288 字段）、用**第一对字节**拼 RefQ（bit15:0 字段），与代码完全吻合（[FPGA.cpp:L316-L335](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L316-L335)）。这是「文档写字段序、代码定传输序」的又一个实例（u4-l2 的教训在此同样适用：核对以代码为准）。

**⑤ 写命令与寄存器表**（[SPIConfig.vhd:L246-L309](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L246-L309)），节选：

```vhdl
case spi_buf_out(15 downto 13) is
	when "000" => state <= WriteSweepConfig;
		SWEEP_ADDRESS <= spi_buf_out(12 downto 0);   -- 点号
	when "100" => state <= WriteRegister;
		selected_register <= to_integer(unsigned(spi_buf_out(4 downto 0)));
...
when WriteRegister =>
	case selected_register is
		when 0 =>  interrupt_mask <= spi_buf_out;
		when 1 =>  SWEEP_POINTS <= spi_buf_out(12 downto 0);
		when 3 =>  PORTSWITCH_EN <= spi_buf_out(0);
		          PORT1_EN <= spi_buf_out(15);  ...（外设使能集合）
		when 8 =>  MAX2871_DEF_0(15 downto 0) <= spi_buf_out;
		...
	selected_register <= selected_register + 1;      -- 地址自动递增
```

两个要点：其一，写寄存器地址只占 5 位（32 个位置，实际用到 0x15）；其二，**地址自动递增**意味着一次 NSS 低电平期间连写一串相邻寄存器只需一个命令字，固件 `FPGA::SetSettlingTime` 连写 0x14/0x15 两个寄存器时虽然各自开了事务，但协议本身支持合并（[FPGA.cpp:L135-L144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L135-L144)）。

**⑥ 扫描配置的 6 字拼装**（[SPIConfig.vhd:L310-L318](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L310-L318)）：

```vhdl
when WriteSweepConfig =>
	if word_cnt = 6 then
		SWEEP_DATA <= sweepconfig_buffer & spi_buf_out;  -- 80+16=96 位
		sweep_config_write <= '1';                        -- 写使能脉冲
	else
		sweepconfig_buffer <= sweepconfig_buffer(63 downto 0) & spi_buf_out;
	end if;
```

命令字之后连发 6 个数据字：前 5 个先暂存在 80 位 `sweepconfig_buffer` 里，第 6 个到来时拼成 96 位 `SWEEP_DATA`，配合写脉冲打进 u6-l1 讲过的双端口 `SweepConfigMem`。点号则来自命令字的低 13 位，因此每个扫描点都要单独一次这样的 7 字事务（这与 u6-l2「MCU 预编程、FPGA 自主扫描」的分工完全一致）。

#### 4.1.4 代码实践

**实践：用固件代码反推命令编码，验证「读识别字」事务**

1. **实践目标**：确认命令码表的前两行（识别字与写寄存器）在 VHDL、C++、文档三处一致。
2. **操作步骤**：
   - 打开 [FPGA.cpp:L28-L36](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L28-L36)，看 `WriteRegister` 的第一个字节 `0x80`：二进制 `100_00000`，正是操作码 `"100"` + 寄存器地址 0。
   - 打开 [FPGA.cpp:L102-L116](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L102-L116)，`FPGA::Init` 发 `{0x40,0,0,0}` 并期待收到 `0xF0A5`：`0x40` = `010_0000000000000`。
   - 对照 [FPGA_protocol.tex:L116-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L116-L131)（写寄存器）与文档 §SPI Protocol 里状态字的描述。
3. **需要观察的现象**：三处对同一条命令的二进制编码逐位相同；`0xF0A5` 这个魔术数在 VHDL 里以字面量 `"1111000010100101"` 出现一次、在 C++ 里以 `0xF0A5` 出现两次（Init 的期待值）。
4. **预期结果**：固件 `FPGA::Init` 返回 true 的前提是从机在命令字期间回送了 `0xF0A5`——这证明 NSS 边沿装载状态字 + 第 0 位直通 `BUF_IN(15)` 这条链路工作正常。运行结果待本地验证（需硬件或仿真）。

#### 4.1.5 小练习与答案

**练习 1**：为什么读命令在 `PRE_COMPLETE`（3 位）解码，写命令却在 `COMPLETE`（16 位）解码？

**答案**：读命令的响应要作为**下一个字**从 MISO 送出，而 `miso_buffer` 在下一字第 0 位（`bit_cnt = 0`）时装载 `BUF_IN`，留给解码+装载的窗口只有命令字的剩余位时间；在 3 位处解码可争取约 13 个 SCK 周期的裕量（[spi_slave.vhd:L44-L47](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L44-L47) 的注释正是这个意思）。写命令的效果（寄存器值、SWEEP_ADDRESS 等）只要在后续字到来前生效即可，晚一点解码没有时序压力，而 16 位完整解码还能顺带取到地址/点号等低位字段。

**练习 2**：MCU 一次也没发读命令，为什么 `FPGA::Init` 还是「TransmitReceive」而不是「Transmit」？

**答案**：SPI 全双工，发命令字的同时 MISO 上必然移出从机准备好的字。NSS 下降沿时 `SPICommands` 把 `interrupt_status` 装入 `spi_buf_in`（[SPIConfig.vhd:L214](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L214)），若命令是 `"010"`，3 位解码后再改载 `0xF0A5`。所以「只写不读」的事务其实每次都在免费捎带一个状态字——`ResumeHaltedSweep` 正是靠读这个字来轮询暂停是否解除（[FPGA.cpp:L443-L460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L443-L460)）。

**练习 3**：状态字的 bit6–15 是什么？如果想在 GUI 上显示「FPGA 扫描状态机当前停在哪个状态」，不新增任何命令能否做到？

**答案**：bit6–15 透传 `DEBUG_STATUS(10 downto 1)`（[SPIConfig.vhd:L191](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L191)），其来源是 Sweep 状态机编码（TriggerSetup=0…Done=8，见 [Sweep.vhd:L137-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L137-L152)）。能：任何一次 SPI 事务的命令字期间都能收到状态字，MCU 直接解析高 10 位即可，这正是 u6-l2 提过的 DEBUG_STATUS 在线观测手段，无需新命令。

### 4.2 PLL 寄存器写入

#### 4.2.1 概念说明

u6-l2 讲过：扫描开始后 MCU 出环，**FPGA 自己驱动两颗 MAX2871 PLL**（源 PLL 与一本振 PLL）。`MAX2871.vhd` 就是 FPGA 侧的「PLL 写入器」：它不计算任何频率，只是把 4 个 32 位影子寄存器（`REG4/REG3/REG1/REG0`）按 MAX2871 的三线接口（SCLK/MOSI/LE）逐位移出。

这里存在一个容易混淆的双通道结构，必须先厘清——**同一物理引脚，两条写 PLL 的路**：

1. **初始化路径（MCU 直通）**：MCU 把 `AUX1`（源）或 `AUX2`（LO）拉高，top.vhd 里的复用器把 MCU 的 SCK/MOSI/NSS **原样转发**到对应 PLL（[top.vhd:L746-L761](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L746-L761)），此时 FPGA 内部从机被 `fpga_select <= '1'` 屏蔽。固件 `FPGA::SetMode` 就是在这三种模式间切换并相应调整 SPI 速率（[FPGA.cpp:L356-L385](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L356-L385)）。这条路用来写扫频中**不变**的寄存器（含寄存器 2、5 等）。
2. **扫描路径（FPGA 自主）**：Sweep 状态机在换点时用逐点配置字段**拼装**出寄存器 0/1/3/4，交给 `MAX2871.vhd` 移出。这条路只搬运扫频中**会变**的字段（N、FRAC、M、VCO、DIV_A、功率）。

协议文档的引脚表把这一点写成了真值表（[FPGA_protocol.tex:L59-L91](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L59-L91)）：AUX1/AUX2 双低=FPGA 通信，AUX1 高=直通源 PLL，AUX2 高=直通 LO PLL，双高=非法。

而「默认寄存器」`MAX2871_DEF_0/1/3/4` 正是两条路的**桥**：MCU 通过命令接口的 0x08–0x0F 寄存器把它们写进 FPGA（固件入口 `FPGA::WriteMAX2871Default`，只写索引 0、1、3、4，见 [FPGA.cpp:L201-L210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L201-L210)）；Sweep 每到一个新点，就把逐点字段嵌进这套默认值，得到完整寄存器再交给写入器。

#### 4.2.2 核心流程

`MAX2871.vhd` 是一个「空闲→装载→移位→锁存→下一个→空闲」的线性状态机：

```text
DONE='1'（空闲）
  └─ RELOAD='1'？ → latched_regs ← REG4 & REG3 & REG1 & REG0（128 位）
                     reg_cnt←0, bit_cnt←0, DONE←'0'
每个 CLK_DIV/2 个时钟推进一步：
  bit_cnt < 32：
      SCLK 低→高：只翻转时钟
      SCLK 高→低：移出下一位（latched_regs 左移，MISO 取 bit127），
                   bit_cnt+1
  bit_cnt = 32：一个寄存器移完
      LE 拉高 → LE 拉低（锁存脉冲），reg_cnt+1，bit_cnt 清零
  reg_cnt = 3 完成：DONE←'1'
```

注意拼接顺序 `REG4 & REG3 & REG1 & REG0`：REG4 落在 128 位矢量的最高位段（bit127:96），而移位从 `latched_regs(127)` 开始——所以发送顺序是 **R4 → R3 → R1 → R0，以 R0 结尾**。这与 MAX2871 的要求一致：R0（含 FRAC/N 的字）必须最后写，其寄存器地址位触发锁存，本次 PLL 更新生效。

时序估算（generic `CLK_DIV => 6`，见 top.vhd 的例化 [top.vhd:L564-L578](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L564-L578)，LO1 同构始于 [top.vhd:L579](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L579)）：

\[ f_{SCLK} = \frac{102.4\,\text{MHz}}{CLK\_DIV} = \frac{102.4}{6} \approx 17.07\,\text{MHz} < 20\,\text{MHz（MAX2871 上限）} \]

一次完整重载 = 128 位 × 6 时钟 + 4 次锁存脉冲 × 6 时钟 ≈ 792 个主时钟：

\[ t_{reload} \approx 792 \times \frac{1}{102.4\,\text{MHz}} \approx 7.73\,\mu\text{s} \]

这正是 u6-l2 里「一次 PLL 重载约 7.7 µs」的出处——那讲从 Sweep 握手视角看这个数，本讲从写入器内部把它算出来。

#### 4.2.3 源码精读

**① 空闲装载与移位**（[MAX2871.vhd:L63-L115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L63-L115)）：

```vhdl
if done_int = '1' then
	if RELOAD = '1' then            -- Sweep 发起重载
		done_int <= '0';
		latched_regs <= REG4 & REG3 & REG1 & REG0;
		reg_cnt <= 0; bit_cnt <= 0; clk_cnt <= 0;
end if;
else
	if clk_cnt < (CLK_DIV/2) - 1 then
		clk_cnt <= clk_cnt + 1;     -- 3 个时钟翻转一次 SCLK
	else
		clk_cnt <= 0;
		if bit_cnt < 32 then
			if sclk = '0' then
				sclk <= '1';
			else
				sclk <= '0';        -- 下降沿换下一位
				latched_regs <= latched_regs(126 downto 0) & "0";
				bit_cnt <= bit_cnt + 1;
```

配套输出（[MAX2871.vhd:L58-L61](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L58-L61)）：`MOSI <= latched_regs(127)` 永远输出矢量的最高位，左移即逐位送出（MSB first）。`DONE` 是给 Sweep 的握手信号——u6-l2 的状态机在换点时等待「移位完成＋PLL 锁定」双条件，其中前者就是这里的 `DONE`。

**② Sweep 侧的寄存器拼装**（[Sweep.vhd:L104-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L104-L121)），看一条即可领会模式：

```vhdl
-- source register 0: N divider and fractional division value
SOURCE_REG_0 <= MAX2871_DEF_0(31) & "000000000" & config_reg(93)
                & config_reg(5 downto 0) & config_reg(26 downto 15) & "000";
-- source register 3: VCO selection
SOURCE_REG_3 <= config_reg(11 downto 6) & MAX2871_DEF_3(25 downto 3) & "011";
```

纯组合逻辑：把 96 位逐点配置 `config_reg` 里的 N/FRAC/M/VCO/DIV_A 字段，逐位嵌进默认寄存器的「不动区」，末尾补上寄存器地址（`"011"` = R3）。固件侧 `FPGA::WriteSweepConfig` 负责把这些字段从 MAX2871 驱动算出的完整 32 位寄存器里**抽出来**塞进 6 个 16 位字（[FPGA.cpp:L212-L264](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L212-L264)），FPGA 侧再**拼回去**——一拆一装，两端共享同一套位布局，这正是 u6-l2 讲过的 SweepConfig 96 位格式。

**③ 引脚复用器**（[top.vhd:L746-L761](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L746-L761)）：

```vhdl
-- only select FPGA SPI slave when both AUX1 and AUX2 are low
fpga_select <= nss_sync when aux1_sync = '0' and aux2_sync = '0' else '1';
-- direct connection between MCU and SOURCE when AUX1 is high
SOURCE_CLK <= MCU_SCK when aux1_sync = '1' else fpga_source_SCK;
SOURCE_MOSI <= MCU_MOSI when aux1_sync = '1' else fpga_source_MOSI;
SOURCE_LE  <= MCU_NSS  when aux1_sync = '1' else fpga_source_LE;
```

三选一复用：MCU 直通、或 FPGA 写入器驱动，同一组 PLL 引脚。AUX1/AUX2/NSS 都先经两级 `Synchronizer` 同步进主时钟域（[top.vhd:L506-L526](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L506-L526)），呼应 u6-l2 的 CDC 纪律。

#### 4.2.4 代码实践

**实践：算一笔「重载耗时」账，并用两处源码交叉验证**

1. **实践目标**：把 4.2.2 的两个公式亲手算一遍，确认它们与 u6-l2 的结论、固件的速率设置互相咬合。
2. **操作步骤**：
   - 从 [top.vhd:L565](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L565) 取 `CLK_DIV => 6`，代入 \( f_{SCLK} = 102.4\,\text{MHz}/6 \)。
   - 数一数 [MAX2871.vhd:L81-L109](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L81-L109)：每 3 个时钟翻转一次 SCLK（半个周期），故每位占 6 个时钟；每个寄存器后有一个高-低锁存脉冲（又是 6 个时钟）。
   - 打开 [FPGA.cpp:L364-L381](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L364-L381)，对比三种模式下的 SPI 分频注释（FPGA 模式 42.5 MHz，PLL 直通模式显著放慢，注释注明 MAX2871 上限 20 MHz）。
3. **需要观察的现象**：FPGA 自驱的 17.07 MHz 落在 MAX2871 的 20 MHz 限制之内；MCU 直通模式刻意降速也是为了同一限制。
4. **预期结果**：\( 792 \times 9.765625\,\text{ns} \approx 7.73\,\mu s \)，与 u6-l2「一次 PLL 重载约 7.7 µs」一致。若你把 CLK_DIV 改成 4（假设综合与芯片允许），重载应缩短到约 5.2 µs，但 SCLK 升到 25.6 MHz、超出芯片规格——这解释了作者为何选 6。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MAX2871_DEF` 只有 0、1、3、4 四套，没有 2 和 5？

**答案**：这四项覆盖扫频中会变的字段（R0 的 N/FRAC、R1 的 M、R3 的 VCO、R4 的 DIV_A 与输出功率）。R2、R5 在整个扫描期间不变，由 MCU 在初始化时经 AUX1/AUX2 直通路径直接写入芯片（[FPGA.cpp:L356-L385](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L356-L385)），FPGA 无需为它们耗费寄存器与移位时间。协议文档也注明这些默认值寄存器中 N/FRAC/M/VCO/DIV_A 字段是「don't care」（[FPGA_protocol.tex:L471-L472](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L471-L472)）。

**练习 2**：`MAX2871.vhd` 里的移位为什么发生在 SCLK 的**下降沿**，而 `spi_slave.vhd` 采样 MOSI 在**上升沿**？

**答案**：两者是同一枚硬币的两面。`spi_slave.vhd` 作为从机在上升沿采样主机数据（[spi_slave.vhd:L97-L98](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/spi_slave.vhd#L97-L98)）；`MAX2871.vhd` 作为（PLL 眼中的）主机在下降沿更新 MOSI（[MAX2871.vhd:L90-L94](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L90-L94)），让数据在下一个上升沿被芯片采样前有半个周期建立时间——这正是 4.1.3 ② 里「教科书做法」的方向；那边因为从机要抢周期才反其道而行。

**练习 3**：如果 Sweep 在 `MAX2871.vhd` 仍忙时（DONE='0'）又拉高一次 RELOAD，会发生什么？

**答案**：什么也不会发生。状态机只在 `done_int = '1'` 的分支里响应 RELOAD（[MAX2871.vhd:L71-L79](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L71-L79)），忙期间的 RELOAD 被忽略。所以 u6-l2 的 Sweep 状态机必须在「等移位完成＋等锁定」之后才推进到下一阶段——两侧靠 DONE 握手，而不是靠重试。

### 4.3 SPI 测试台

#### 4.3.1 概念说明

`Test_SPICommands.vhd` 是 `SPICommands` 的 testbench：没有真实 MCU，它自己扮演主机，产生 SCLK/MOSI/NSS 激励并（原则上）观察输出。它与被测件的关系是「组件声明 + 例化 + 激励进程」三件套，是 u6-l6 的前菜。仓库里每个 FPGA 模块都配一个 `Test_*.vhd`，改 VHDL 先跑仿真，是这个项目的开发纪律。

这个测试台还有一层特殊价值：它是**命令码表的机器可读版**。每个激励场景就是一条命令的用法示例，二进制字面量直接写在调用里。

#### 4.3.2 核心流程

testbench 的固定骨架：

```text
1. 声明与被测件一模一样的 COMPONENT（端口表复制）
2. 例化 uut，把信号接上（未用的输入绑常量，如 ADC_MINMAX => (others => '0')）
3. 两个并发进程：
   a. 时钟进程：CLK 翻转，周期 9.765625 ns（= 102.4 MHz，与真实主时钟一致）
   b. 激励进程：按时间线驱动 RESET/NSS/MOSI/SCLK，最后 wait; 挂起
```

SPI 时钟周期取 23.52941176 ns ≈ 42.5 MHz——这**不是随便选的**：固件在 FPGA 模式下把 SPI 分频设为 42.5 MHz（[FPGA.cpp:L364-L366](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L364-L366)），testbench 用同一速率仿真，验证的才是真实工况。

#### 4.3.3 源码精读

**① 手工展开的 SPI 过程**（[Test_SPICommands.vhd:L190-L288](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd#L190-L288)）：

```vhdl
procedure SPI(data : std_logic_vector(15 downto 0)) is
begin
	MOSI <= data(15);                     -- MSB first
	data_signal <= data(14 downto 0) & "0";
	wait for SPI_CLK_period/2;
	SCLK <= '1';
	wait for SPI_CLK_period/2;
	SCLK <= '0';
	MOSI <= data_signal(15);              -- 同一段复制 16 遍
	...
```

这个过程把「放一位数据 → 半周期后 SCLK 上升沿 → 半周期后回低」重复展开了 16 次（ISE 生成风格，未用循环）。要点：MOSI 在上升沿**之前**就摆好，与真实主机行为一致；先 MSB 后 LSB。

**② 六个激励场景**（[Test_SPICommands.vhd:L289-L361](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd#L289-L361)），逐条对应命令码表：

| 场景 | 激励（NSS 低电平窗口内） | 验证的命令 |
|---|---|---|
| 1 | `SPI("0100000000000000")` + 1 个哑字 | `"010"` 读识别字，期待 MISO 回 0xF0A5 |
| 2 | `SPI("1000000000000011")`, `SPI("1111111111111111")` | `"100"` 写 3 号寄存器（系统控制）=0xFFFF，全外设使能 |
| 3 | 预置 `SAMPLING_RESULT` 后 `SPI("1100000000000000")` + 4 个哑字 | `"110"` 读采样结果的前几字 |
| 4 | `SPI("1000000000000001")`, `SPI("1111000011110000")` | 写 1 号寄存器（扫描点数） |
| 5 | `SPI("0000000000001011")` + 6 个数据字 | `"000"` 写 11 号点的扫描配置（7 字事务） |
| 6 | 脉冲 `NEW_SAMPLING_DATA` 后 `SPI("1100000000000000")`，重复两轮 | 验证 ND 标志置位/清除与 overrun 路径 |

场景 5 的命令字 `"0000000000001011"` 尤其值得看：高 3 位 `000` 是写扫描配置操作码，低 13 位 `0000000001011` = 11，即点号——与 4.1.3 ⑥ 的 `SWEEP_ADDRESS <= spi_buf_out(12 downto 0)` 严丝合缝。

#### 4.3.4 代码实践

**实践：把测试台场景翻译成命令事务清单（纯代码走读，无需仿真器）**

1. **实践目标**：不运行任何工具，仅凭 `Test_SPICommands.vhd` 与 `SPIConfig.vhd`，写出每个场景将触发的 FPGA 内部动作。
2. **操作步骤**：
   - 对场景 2 的第二个字 `1111111111111111`，对照 [SPIConfig.vhd:L275-L286](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L275-L286) 逐位写出 3 号寄存器各使能位（bit15=PORT1_EN、bit14=PORT2_EN、bit13=REF_EN、bit12 经取反驱动 AMP_SHDN……）各自的电平。
   - 对场景 6，跟踪 [SPIConfig.vhd:L205-L210](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L205-L210)：第一个 `NEW_SAMPLING_DATA` 脉冲把 `unread_sampling_data` 置 1；`"110"` 读命令把它清 0。推演第二轮**不**发读命令直接再脉冲一次会发生什么（提示：`data_overrun`）。
   - 若本机有 ISE/isim 或 ghdl：把 `Test_SPICommands.vhd` 设为顶层跑行为仿真，观察 MISO 与各输出寄存器。运行结果待本地验证。
3. **需要观察的现象**：走读结论应能回答「场景 2 之后 PORT1_EN/PORT2_EN/REF_EN 是什么电平」「场景 6 第二轮后状态字 bit3（OR）是否置位」。
4. **预期结果**：场景 2 后三个使能全为 '1'，AMP_SHDN 为 '0'（bit12 为 1 取反）；场景 6 第二轮脉冲时 `unread_sampling_data` 已是 '1'，将把 `data_overrun` 拉起且按文档只能靠复位 FPGA 清除（[FPGA_protocol.tex:L111](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L111)）。波形验证待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：testbench 里 `SOURCE_UNLOCKED`/`LO_UNLOCKED` 初始化为 '1'（[Test_SPICommands.vhd:L113-L114](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd#L113-L114)），这会让哪个输出立刻生效？

**答案**：状态字的 bit1（SU）与 bit0（LU）恒为 1（[SPIConfig.vhd:L191](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L191)）。但 `INTERRUPT_ASSERTED` 是否拉起取决于屏蔽字：仿真初期 `interrupt_mask` 复位为全 0（[SPIConfig.vhd:L176](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L176)），所以中断线并不 asserted——这正好同时测了「状态」与「屏蔽」两层逻辑的独立性。

**练习 2**：为什么 testbench 的组件声明只连了部分端口（如 `STAGES`、`SETTLING_TIME` 未出现在信号列表里）也能工作？

**答案**：VHDL 的端口映射允许省略未连接的端口（等效开路）。这个测试台写于这些端口加入之前（见其端口表确实声明了它们但激励进程没有驱动/检查的需求），仿真器对未映射输出仅不观察、对未映射输入报警告。若要补全，应像 `ADC_MINMAX => (others => '0')` 那样显式绑定（[Test_SPICommands.vhd:L153](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_SPICommands.vhd#L153)）。这也是 u6-l6 要讲的「给旧 testbench 补新用例」的典型入手点。

## 5. 综合实践

**任务：整理完整的 FPGA 命令码表，并为「读取当前扫描点编号」设计编码方案**

这是本讲规格指定的毕业实践，产出一份可直接放进团队 wiki 的文档。分两步：

### 第一步：整理命令码表与寄存器映射表

依据四处源码交叉核对：命令解码 [SPIConfig.vhd:L246-L268](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L246-L268) 与 [L217-L238](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L217-L238)；寄存器写表 [SPIConfig.vhd:L269-L308](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L269-L308)；固件对照 [FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) 与 [FPGA.hpp:L13-L34](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L13-L34)；文档 [FPGA_protocol.tex:L93-L556](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L93-L556)。参考答案（位序 bit15→bit0，操作码占 bit15:13）：

**命令码表**

| bit15:13 | 命令字示例 | 名称 | 方向 | 后续字 | 作用 | 固件入口 |
|---|---|---|---|---|---|---|
| `000` | `0_点号[12:0]` | 写扫描配置 | 写 | 6 字 | 把该点的 96 位配置写入 SweepConfigMem | `FPGA::WriteSweepConfig` |
| `001` | `0x2000` | 恢复暂停的扫描 | 写 | 无 | 让 halt 的点继续 settling+采样 | `FPGA::ResumeHaltedSweep` |
| `010` | `0x4000` | 读识别字 | 读 | 1 字 | 回 `0xF0A5`，开机自检 | `FPGA::Init` / `GetStatus` |
| `011` | `0x6000` | 复位 ADC 限值 | 写 | 无 | 三路 min/max 回初值 | `FPGA::ResetADCLimits` |
| `100` | `0x8000+地址[4:0]` | 写寄存器 | 写 | ≥1 字（地址自增） | 见下表 | `FPGA::WriteRegister` |
| `101` | `0xA000` | 读 DFT 结果 | 读 | 12 字 | 读一个 bin，重复则取下一 bin | `FPGA::ReadDFTResult` |
| `110` | `0xC000` | 读采样结果 | 读 | ≤19 字 | 读最新采样结果（最低字在前） | `FPGA::InitiateSampleRead` |
| `111` | `0xE000` | 读 ADC 限值 | 读 | 6 字 | 读三路 ADC 的 min/max | `FPGA::GetADCLimits` |

**寄存器映射表**（写寄存器命令的低 5 位）

| 地址 | 名称 | 作用 | 文档节 |
|---|---|---|---|
| 0x00 | InterruptMask | 中断屏蔽字（bit5 兼作 DFT 使能） | [L291-L311](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L291-L311) |
| 0x01 | SweepPoints | 每扫描点数−1 | [L313-L320](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L313-L320) |
| 0x02 | SamplesPerPoint | 每点采样数（16 的倍数） | [L322-L333](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L322-L333) |
| 0x03 | SystemControl | 外设使能/LED/窗选择/同步主机 | [L335-L384](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L335-L384) |
| 0x04 | ADCPrescaler | ADC 采样率分频（\( SR_{ADC} = 102.4\,\text{MHz}/\text{Presc} \)） | [L386-L399](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L386-L399) |
| 0x05 | PhaseIncrement | 单 bin DFT 相位增量 | [L401-L415](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L401-L415) |
| 0x06 | SweepSetup | stage 数/同步使能/端口 stage | [L417-L435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L417-L435) |
| 0x07 | HardwareOverwrite | 硬件覆盖（衰减器/滤波器/波段/端口路由） | [L437-L469](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L437-L469) |
| 0x08–0x0F | MAX2871Def 0/1/3/4 | PLL 默认寄存器各 32 位的低/高半字 | [L471-L506](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L471-L506) |
| 0x10、0x11 | — | 未使用（留空隙） | — |
| 0x12 | DFTFirstBin | DFT 首 bin 频率字 | [L514-L524](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L514-L524) |
| 0x13 | DFTFreqSpacing | DFT bin 间距字 | [L526-L536](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L526-L536) |
| 0x14 | SettlingTimeLow | 稳定时间低 16 位 | [L538-L548](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L538-L548) |
| 0x15 | SettlingTimeHigh | 稳定时间高 4 位 | [L549-L556](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L549-L556) |

核对时留意两处「文档 vs 代码」：0x10/0x11 的空隙只能从 VHDL 的 case 分支看出（文档只列已用地址）；写寄存器命令在固件里以 `0x80 | reg` 一字节 + 地址一字节的形式出现（[FPGA.cpp:L29](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L29)），本质是同一个 16 位命令字按大端拆成的两字节。

### 第二步：设计假想命令「读取当前扫描点编号」

**需求**：扫描进行中（Sweep 自主运转时），MCU 想知道 FPGA 当前扫到第几个点、第几个 stage，用于在 GUI 上显示扫描进度。

**先做功课——数据已经存在**：Sweep 内部有 `point_cnt`（其低位直接驱动 `CONFIG_ADDRESS`，[Sweep.vhd:L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L102)），并且已经通过 `RESULT_INDEX` 把「点号+stage」放进每条采样结果的头部（[top.vhd:L734](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L734)）；但那是**事后**的——只有采完一个点才能读到。假想命令的价值在于**实时**窥视。

**难点**：3 位操作码的 8 个编码已全部占用（见上表），没有免费的码点。三个候选方案：

**方案 A：子选择位（扩展 `"110"`）**
`"110"` 命令字的低 13 位全是 reserved（[FPGA_protocol.tex:L148-L157](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L148-L157)）。取 bit12 作子选择：`0xC000` 保持原语义（读采样结果），`0xD000` 改读一个 16 位扫描状态字 `{point_cnt[12:0], stage_cnt[2:0]}`。
- 解码时机：`PRE_COMPLETE` 只有 3 位、不够区分子命令；但响应在**下一字**才发出，而 `miso_buffer` 到下一字第 0 位才装载，所以放到 `spi_complete`（整字收完）再装载 `spi_buf_in` 仍有约一整个字时间的裕量（≈16 SCK 周期 ≈ 39 个 CLK 周期），时序成立。
- 改动：Sweep 增加一个 16 位输出（或复用 `RESULT_INDEX`），SPICommands 在 `FirstWord` 的 complete 分支加一个 `elsif spi_buf_out(12) = '1'`；固件加一个 `FPGA::GetSweepPosition()`。
- 风险：复用了保留位，属于「协议文档必须同步更新」的隐性扩展。

**方案 B：搭写命令的便车**
纯写命令 `"001"`（恢复扫描）与 `"011"`（复位限值）的响应字本来无人消费。在它们的解码分支加一行 `spi_buf_in <= 扫描状态字`，即「写命令白送一个读字」。妙处在于固件**零改动**即可受益：`ResumeHaltedSweep` 本来就是 TransmitReceive（[FPGA.cpp:L453-L455](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L453-L455)），此刻收到的字从「无意义」变成「扫描位置」。缺点是只能在调用这两条命令的时机顺带获得，不能随时查询。

**方案 C：借道 DEBUG_STATUS**
状态字 bit15:6 已透传 `DEBUG_STATUS(10 downto 1)`（[SPIConfig.vhd:L191](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/SPIConfig.vhd#L191)）。把 `point_cnt` 的低 10 位塞进 `DEBUG_STATUS`（[Sweep.vhd:L137-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L137-L152) 现放的是状态机编码），任何一次 SPI 事务都能读到，零命令开销。代价：10 位装不下 13 位点号（4501 点需 13 位），会回绕；且挤掉了原有的状态机观测位——与 u6-l2 依赖的 DEBUG_STATUS 调试手段冲突。

**推荐**：若这是要提交上游的功能，选方案 A（语义清晰、可随时查询、码点浪费最少）；若只是调试自家固件的临时手段，选方案 C（改动最小）。评估任何方案的三条标准：不破坏既有编码、解码时机在时序上可行、MCU 侧改动量与协议文档同步成本。

**验证方式**：把选定方案落成代码后，在 `Test_SPICommands.vhd` 里仿照场景 1 加一段激励（如 `SPI("1101000000000000")` + 一个哑字），断言 MISO 回送的字等于注入的 point_cnt/stage_cnt。波形验证待本地验证（需 ISE 仿真或 ghdl）。

## 6. 本讲小结

- **双层结构**：`spi_slave.vhd` 只做按位收发与跨时钟域递交，`SPIConfig.vhd`（实体 `SPICommands`）负责命令语义与寄存器堆——传输与协议彻底解耦。
- **预解码是点睛之笔**：读命令必须在命令字第 3 位（`PRE_COMPLETE`）解码，才能赶在下一字第 0 位装载响应（`miso_buf` 装载点）之前备好数据；写命令在整字收完（`COMPLETE`）解码即可。
- **全双工白捡一个状态字**：NSS 拉低即装载 `interrupt_status`，命令字期间 MISO 回送状态（bit0=LU…bit5=DFT，bit15:6 透传调试状态），中断线由「状态∧屏蔽」驱动。
- **命令面全景**：8 个操作码（写扫描配置/恢复/识别/复位限值/写寄存器/读 DFT/读采样/读限值）+ 0x00–0x15 寄存器表；写寄存器地址自增；采样结果最低字在前（文档字段序 ≠ 传输序，以代码为准）。
- **PLL 双通道**：AUX1/AUX2 复用同一组引脚——初始化走 MCU 直通（含不变的 R2/R5），扫描走 FPGA 自主（`MAX2871_DEF` 默认值 + 逐点字段拼装，R4→R3→R1→R0 以 R0 锁存结尾）；`CLK_DIV=6` 下 SCLK≈17.07 MHz，一次重载约 7.7 µs。
- **testbench 即机器可读的协议样例**：`Test_SPICommands.vhd` 的六个场景覆盖主要命令，42.5 MHz 的 SPI 时钟与固件设置一致，改 VHDL 前先在这里回归。

## 7. 下一步学习建议

- 下一讲 **u6-l6（FPGA 验证文化：Testbench 全家福）**：本讲的 `Test_SPICommands.vhd` 只是入门，那边会系统梳理 `Test_DFT/Test_Windowing/Test_PLL` 等的激励-断言模式，并动手给一个 testbench 补新用例——本讲综合实践的「验证方式」一节正好是热身。
- 回读 **u5-l2（射频硬件控制）**的 `max2871.cpp`：对照固件侧「影子寄存器＋显式提交」范式，你会看到同一颗芯片在 MCU 与 FPGA 两侧各有一个写入器，接口风格（三线 SPI＋LE）完全相同。
- 回读 **u4 单元**：USB 协议（主机-设备）与本讲 SPI 协议（MCU-FPGA）是同构的「命令字+变长数据」设计，比较两者在 CRC、ACK、字节序上的不同取舍，能加深对「协议分层」的理解。
- 若想继续核对协议文档，`FPGA_protocol.tex` 的 §SweepConfig 与 §Sampling Result 两节分别对应 u6-l2 与 u6-l3 的源码，可自行做一轮三方（文档/VHDL/C++）核对练习。
