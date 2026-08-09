# SPI Engine 框架

## 1. 本讲目标

本讲深入解析 ADI HDL 中用于控制低速 SPI 器件（如精密 SAR ADC、DAC、Σ-Δ 转换器）的 **SPI Engine 框架**。读完本讲你应当能够：

- 说清 SPI Engine 为什么「可编程」：它把一段 SPI 时序抽象成一条条 16 位**指令**，由执行模块翻译成 SCK/MOSI/CS/MISO 物理波形；
- 理解框架的「积木式」拆分：`execution`（执行）、`interconnect`（命令源选择）、`offload`（硬件卸载）、`axi_spi_engine`（软件 AXI 入口）四个独立 IP 各司其职，通过统一的 `spi_engine_ctrl` 接口拼接；
- 掌握 `spi_engine_execution` 的指令执行模型与 SCLK 生成原理；
- 理解 `offload` 如何用一块 RAM 预存指令、在触发信号到来时以极低延迟自动重放，把 CPU 从「逐拍喂指令」中解放出来；
- 看懂 `axi_spi_engine` 作为顶层封装如何用 `up_axi` 桥和四组 FIFO 把软件的寄存器读写翻译成命令流。

## 2. 前置知识

在进入本讲前，请先具备以下概念（本手册 u4-l5 已建立其中大部分）：

- **SPI 总线**：一种主从式、同步串行协议，核心是四类物理信号——`SCLK`（时钟）、`CS`（片选，通常低有效）、`MOSI/SDO`（主出从入）、`MISO/SDI`（主入从出）。CPOL/CPHA 两个位决定时钟极性与采样沿（即「SPI 模式 0/1/2/3」）。
- **AXI4-Lite 与 `up_axi` 桥**：CPU 如何用一组寄存器读写（`up_wreq/up_wack`、`up_rreq/up_rack`）控制硬件 IP。本讲中 `axi_spi_engine` 正是通过 `up_axi` 把 AXI 总线翻译成内部寄存器访问（详见 u4-l5）。
- **AXI-Stream 握手**：`valid`/`ready` 同时拉高时一次数据传递完成。SPI Engine 内部的命令流（CMD）、发送数据流（SDO）、接收数据流（SDI）、同步事件流（SYNC）都遵循这套握手。
- **FIFO 与跨时钟域（CDC）**：`ASYNC_SPI_CLK` 参数决定软件时钟与 SPI 时钟是否异步，异步时需用 `util_axis_fifo` + `sync_bits` 等原语跨域（详见 u5-l3）。

一个直觉：传统 SPI 控制器是「写一个寄存器触发一次固定字节传输」；SPI Engine 不同——CPU 只需往**命令 FIFO** 里丢一串指令（「先拉低 CS，再按某时钟发 3 个字、同时读 3 个字，最后拉高 CS」），执行模块会自动把它们翻译成一整段完整时序。这让 SPI 时序变成了**可编程的数据流**，从而支持各种 ADI 器件千差万别的访问规则。

## 3. 本讲源码地图

本讲涉及的关键文件集中在 `library/spi_engine/` 下：

| 文件 | 角色 |
| --- | --- |
| [library/spi_engine/spi_engine_execution/spi_engine_execution.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v) | **执行模块**：框架的「心脏」。把命令流翻译成 SCK/SDO/CS/SDI 物理时序。 |
| [library/spi_engine/spi_engine_execution/spi_engine_execution_shiftreg.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution_shiftreg.v) | 执行模块内的**移位寄存器**子模块：负责 SDO 并串转换、SDI 串并采样。 |
| [library/spi_engine/spi_engine_interconnect/spi_engine_interconnect.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_interconnect/spi_engine_interconnect.v) | **互连模块**：2 选 1 多路复用，在软件命令源与 offload 命令源之间切换。 |
| [library/spi_engine/spi_engine_offload/spi_engine_offload.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v) | **卸载模块**：把命令序列预存进 RAM，触发后自动重放，无需 CPU 介入。 |
| [library/spi_engine/axi_spi_engine/axi_spi_engine.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v) | **AXI 顶层**：寄存器映射 + 四组 FIFO + up_axi 桥，给执行模块提供软件控制的命令源。 |
| [projects/ad5766_sdz/common/ad5766_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad5766_sdz/common/ad5766_bd.tcl) | 真实工程块设计：演示四个 IP 如何拼接成完整 SPI 通路。 |
| [docs/library/spi_engine/index.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/spi_engine/index.rst) | 框架官方文档总入口（含子模块页与指令集规范）。 |

> ⚠️ **关键架构事实**：`axi_spi_engine.v` **并不**在内部例化 execution / interconnect / offload。它只产出/消费 `spi_engine_ctrl` 接口信号（`cmd_valid/cmd_data`、`sdo_data`、`sdi_data`、`sync` 等）。真正把这四块拼起来的是**工程的块设计 Tcl**（如 `ad5766_bd.tcl`）。这一点是初学者最易误解之处，本讲第 4 节会反复强调。

## 4. 核心概念与源码讲解

### 4.1 SPI Engine 执行模型（execution）

#### 4.1.1 概念说明

`spi_engine_execution` 是整个框架的核心。它接收一个**命令流**（command stream），每条命令是 16 位，编码了一种「动作」：

- **传输指令（Transfer）**：产生若干拍 SCLK，可同时设定「写（w，把 SDO 数据移出去）」和「读（r，把 SDI 采样进来）」；
- **片选指令（Chip-Select）**：更新 CS 引脚电平，并在切换前后插入设定延时；
- **配置写指令（Config Write）**：动态修改分频、CPOL/CPHA、字长、SDI/SDO 通道掩码；
- **同步指令（Sync）**：在 SYNC 流上产生一个事件 ID，软件借此知道「命令执行到这里了」；
- **睡眠指令（Sleep）**：暂停命令流若干个分频周期。

这套设计的好处是：**SPI 时序本身被参数化成数据**。同一块硬件，既可以发「先写后读」的 ADC 采样序列，也可以发「只读」的 DAC 回放序列，全靠往命令流里写什么指令决定。

执行模块对外有两类接口：

- `ctrl`：即 `spi_engine_ctrl` 接口（CMD/SDO/SDI/SYNC 四条流），是它与命令源（软件或 offload）对话的通道；
- `spi`：真正的物理 SPI 引脚（`sclk`、`sdo`、`sdo_t` 三态方向、`sdi`、`cs`、`three_wire`）。

#### 4.1.2 核心流程

把一条传输指令变成 SCK/MOSI 波形，执行模块内部经历这样一个循环（简化伪代码）：

```
每来一条新命令（cmd_valid && cmd_ready && idle）:
    解码 cmd[14:12] 得到 inst（指令类型）
    锁存到 cmd_d1，进入 busy（idle=0）

对于传输指令（inst == CMD_TRANSFER）:
    等待 SDO/SDI 数据就绪（io_ready）
    置 transfer_active=1
    用 clk_div_counter 按 clk_div 分频产生 trigger 脉冲
    每个 trigger:
        trigger_tx 拍：把移位寄存器最高位送上 sdo，左移一位
        trigger_rx 拍：把 sdi 引脚采入移位寄存器最低位
        翻转 sclk（cpol ^ cpha ^ ntx_rx）
    数满 word_length 个 bit → end_of_word → transfer_counter++
    transfer_counter 达到指令中的长度 n → last_transfer
    所有 word 移完 → transfer_done → 回到 idle，取下一条命令
```

SCLK 频率由分频寄存器 `clk_div` 决定，公式为：

\[
f_{sclk} = \frac{f_{clk}}{(div + 1) \times 2}
\]

即 `clk_div=0` 时 SCLK 频率最高（为模块时钟的一半），`clk_div` 越大 SCLK 越慢。片选与睡眠指令的延时也共享这套分频：

\[
delay_{before} = 2 + t \times \frac{(div + 1) \times 2}{f_{clk}}, \qquad delay_{after} = t \times \frac{(div + 1) \times 2}{f_{clk}}
\]

#### 4.1.3 源码精读

**(1) 指令类型解码。** 执行模块用一组 `localparam` 定义指令操作码，并从命令字中提取 3 位操作码字段：

```verilog
localparam CMD_TRANSFER   = 3'b000;
localparam CMD_CHIPSELECT = 3'b001;
localparam CMD_WRITE      = 3'b010;  // 配置写
localparam CMD_MISC       = 3'b011;  // sync / sleep
localparam CMD_CS_INV     = 3'b100;  // CS 反转掩码
...
wire [2:0] inst              = cmd[14:12];
wire       exec_cmd          = cmd_ready && cmd_valid;
wire       exec_transfer_cmd = exec_cmd && inst == CMD_TRANSFER;
```

参见 [spi_engine_execution.v:78-92](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L78-L92)（操作码定义）与 [spi_engine_execution.v:149-157](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L149-L157)（解码逻辑）。注意 `inst` 取自 `cmd[14:12]`，而 bit 15、11 恒为 0（保留位），这与 `instruction-format.rst` 的规定一致。

**(2) 配置写指令修改运行参数。** 当 `inst == CMD_WRITE` 时，按 `cmd[10:8]` 选择要写的配置寄存器（分频 / SPI 配置 / 字长 / SDI 掩码 / SDO 掩码）：

```verilog
if (exec_write_cmd) begin
  case (cmd[10:8])
    REG_CLK_DIV        : clk_div <= cmd[7:0];
    REG_CONFIG         : begin cpha <= cmd[0]; cpol <= cmd[1];
                               three_wire <= cmd[2]; sdo_idle_state <= cmd[3]; end
    REG_WORD_LENGTH    : begin word_length  <= cmd[7:0];
                               left_aligned <= DATA_WIDTH - cmd[7:0]; end
    REG_SDI_LANE_CONFIG: sdi_lane_mask <= cmd[7:0];
    REG_SDO_LANE_CONFIG: sdo_lane_mask <= cmd[7:0];
  endcase
end
```

参见 [spi_engine_execution.v:256-295](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L256-L295)。这解释了为何 CPOL/CPHA、字长可在运行时切换——它们存在寄存器里，由命令流动态配置。

**(3) SCLK 的分频与生成。** `clk_div_counter` 每拍到 0 就重装 `clk_div` 并发一个 `trigger` 脉冲；`trigger_tx`/`trigger_rx` 分别驱动发送与接收移位。SCLK 本身由 `cpol`、`cpha` 和收发节拍 `ntx_rx` 异或得到：

```verilog
always @(posedge clk) begin
  if (~|clk_div_counter || idle || wait_for_io) begin
    clk_div_counter <= clk_div; trigger <= 1'b1;          // 重装分频，发一个 trigger
  end else begin
    clk_div_counter <= clk_div_counter - 1'b1; trigger <= 1'b0;
  end
  ...
end
assign trigger_tx = trigger && ~ntx_rx;
assign trigger_rx = trigger &&  ntx_rx;
...
always @(posedge clk) begin
  if (transfer_active) sclk_int <= cpol ^ cpha ^ ntx_rx;  // 翻转 SCLK
  else                 sclk_int <= cpol;                  // 空闲保持 CPOL
end
```

参见 [spi_engine_execution.v:304-316](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L304-L316)（分频与 trigger）与 [spi_engine_execution.v:541-547](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L541-L547)（SCLK 生成）。`ntx_rx` 在每个 bit 周期内翻转一次，所以一个 bit 占两个 trigger——恰好对应 SCLK 的上升与下降沿，这就是 \((div+1)\times 2\) 分母中「×2」的来历。

**(4) CS 输出与空闲状态机。** CS 只在片选指令到来时更新，且与反转掩码异或以支持「逐引脚」极性；空闲状态机 `idle` 决定何时接收下一条命令：

```verilog
always @(posedge clk) begin
  if (!resetn) cs <= 'hff;                       // 复位时所有 CS 拉高（不选中）
  else if (cs_gen) cs <= cmd_d1[NUM_OF_CS-1:0] ^ cs_inv_mask_reg[NUM_OF_CS-1:0];
end
```

参见 [spi_engine_execution.v:417-423](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L417-L423)（CS 输出）与 [spi_engine_execution.v:374-405](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution.v#L374-L405)（idle 状态机：按指令类型判断完成条件）。

**(5) 移位寄存器（SDO 并→串，SDI 串→并）。** 执行模块把这部分委托给子模块 `spi_engine_execution_shiftreg`。SDO 侧用一个 generate 为每条 SDIO lane 生成一个移位寄存器，在 `first_bit` 时加载并行数据，之后每个 `trigger_tx` 左移一位，最高位送 `sdo_int`：

```verilog
for (i = 0; i < NUM_OF_SDIO; i = i + 1) begin: g_sdo_shift_reg
  reg [(DATA_WIDTH-1):0] data_sdo_shift = 0;
  always @(posedge clk) begin
    if (!sdo_enabled || !exec_cmd)
      data_sdo_shift <= {DATA_WIDTH{sdo_idle_state}};
    else if (transfer_active && trigger_tx) begin
      if (first_bit)
        data_sdo_shift <= sdo_lane_mask[i] ? aligned_sdo_data[...] : {DATA_WIDTH{sdo_idle_state}};
      else
        data_sdo_shift <= {data_sdo_shift[(DATA_WIDTH-2):0], 1'b0};  // 左移
    end
  end
  assign sdo_int[i] = data_sdo_shift[DATA_WIDTH-1];   // 最高位送出
end
```

参见 [spi_engine_execution_shiftreg.v:134-152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution_shiftreg.v#L134-L152)（SDO 移位）与 [spi_engine_execution_shiftreg.v:279-317](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_execution/spi_engine_execution_shiftreg.v#L279-L317)（SDI 采样：每个 `trigger_rx_s` 把 `sdi[i]` 移入低位，数满一个字后置 `sdi_data_valid`）。这正是「一条软件指令 → 实际 SCK/MOSI 波形」的物理落地。

#### 4.1.4 代码实践

**实践目标**：手工「翻译」一条传输指令，验证对执行模型的理解。

**操作步骤**：
1. 打开 [docs/library/spi_engine/instruction-format.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/spi_engine/instruction-format.rst)，找到「Transfer Instruction」位域表。
2. 按位拼出一条「读 1 个字（r=1, w=0, n=0）」的命令字：bit15=0、bit14:12=000（TRANSFER）、bit11=0、bit10=rv(0)、bit9(r)=1、bit8(w)=0、bit7:0(n)=00000000。
3. 把步骤 2 拼出的 16 位值与执行模块解码逻辑对照：`inst=cmd[14:12]=000`、`sdi_enabled<=cmd[9]=1`、`sdo_enabled<=cmd[8]=0`、传输字数 `n+1=1`。
4. 再拼一条「写 4 个字（r=0,w=1,n=3）」，自检 bit9/bit10/bit7:0 的取值。

**需要观察的现象**：传输指令的「读/写/长度」三个语义分别落在 `cmd[9]`、`cmd[8]`、`cmd[7:0]`，且长度是 `n+1`（即 `cmd[7:0]=0` 表示传 1 个字）。

**预期结果**：读 1 字 → 命令字 `0x0200`（`0000_0010_0000_0000`）；写 4 字 → 命令字 `0x0103`（`0000_0001_0000_0011`）。把这两个值带到源码 `case (inst)` 与 `transfer_counter == cmd_d1[7:0]` 处核对，逻辑应当自洽。

> 待本地验证：若手头有 no-OS 或 Linux 环境的 SPI Engine 工程，可在寄存器层确认软件实际下发的 CMD_FIFO 值与此一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `clk_div=0` 时 SCLK 频率是模块时钟的**一半**而不是相等？

**参考答案**：因为一个 bit 需要两个 trigger 拍（一个发、一个收，对应 SCLK 的两个沿），而 `clk_div=0` 时每个时钟周期产生一个 trigger，于是两个时钟周期才走完一个 bit、产生一个完整 SCLK 周期。代入公式 \(f_{sclk}=f_{clk}/((0+1)\times 2)=f_{clk}/2\)。

**练习 2**：执行模块如何知道一次「传输指令」已经做完所有字？

**参考答案**：`transfer_counter` 每完成一个字（`end_of_word`）加 1，并与 `cmd_d1[7:0]`（即指令里的 n）比较；当 `transfer_counter == cmd_d1[7:0]` 时 `last_transfer=1`，最终 `transfer_done=1`，`idle` 状态机回到 1，开始取下一条命令。

---

### 4.2 interconnect 多命令源切换

#### 4.2.1 概念说明

执行模块只有一个 `ctrl` 输入口，但系统里往往有**两个命令源**想驱动它：

- **软件源**：`axi_spi_engine`，由 CPU 经 AXI 寄存器动态下发命令（灵活，但有 CPU 延迟）；
- **卸载源**：`spi_engine_offload`，预存命令、由硬件触发重放（低延迟，但内容固定）。

`spi_engine_interconnect` 就是一个 **2 选 1 多路复用器**：根据一个方向信号 `s_interconnect_dir`，把 s0（软件）或 s1（offload）的 CMD/SDO/SDI/SYNC 四条流接到唯一的 master 输出 `m_*`，再送给执行模块。它本身不含状态机，几乎是纯组合逻辑——这正是它的设计目的：让两个命令源**互斥地、安全地共享**同一个执行引擎。

#### 4.2.2 核心流程

```
s_interconnect_dir = 1  → master 接收 s0（软件）一侧的命令/SDO，把 SDI/SYNC 回送给 s0
                          s1 一侧的 ready 拉为 0（禁止 offload）
s_interconnect_dir = 0  → master 接收 s1（offload）一侧，s0 的 ready 拉为 0
```

注意 SDI 与 SYNC 是**反方向**的流（从执行模块流向命令源），所以要把 master 的数据**广播给两侧**（`s0_sdi_data = m_sdi_data; s1_sdi_data = m_sdi_data;`），但 `valid`/`ready` 仍按方向独占——只有被选中的一侧才看到 `valid=1`，另一侧的 `ready` 被强制为 0。

#### 4.2.3 源码精读

互连模块用一个宏把「二选一」封装得非常紧凑：

```verilog
`define spi_engine_interconnect_mux(s0, s1) (s_interconnect_dir == 1'b1 ? s0 : s1)

assign m_offload_active = s_interconnect_dir;            // 透传方向信号给执行模块
assign m_cmd_data   = `spi_engine_interconnect_mux(s0_cmd_data,  s1_cmd_data);
assign m_cmd_valid  = `spi_engine_interconnect_mux(s0_cmd_valid, s1_cmd_valid);
assign s0_cmd_ready = `spi_engine_interconnect_mux(m_cmd_ready, 1'b0);
assign s1_cmd_ready = `spi_engine_interconnect_mux(1'b0,        m_cmd_ready);
```

参见 [spi_engine_interconnect.v:98-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_interconnect/spi_engine_interconnect.v#L98-L104)。`s_interconnect_dir=1` 选 s0、`=0` 选 s1；被放弃的一侧 `ready` 恒为 0，保证握手不会误传。

反向流（SDI/SYNC）的处理是「数据广播、握手独占」：

```verilog
assign s0_sdi_data  = m_sdi_data;        // 数据两侧都给
assign s1_sdi_data  = m_sdi_data;
assign m_sdi_ready  = `spi_engine_interconnect_mux(s0_sdi_ready, s1_sdi_ready);  // 只采被选中侧的 ready
assign s0_sdi_valid = `spi_engine_interconnect_mux(m_sdi_valid, 1'b0);           // 只向选中侧置 valid
assign s1_sdi_valid = `spi_engine_interconnect_mux(1'b0,        m_sdi_valid);
```

参见 [spi_engine_interconnect.v:111-121](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_interconnect/spi_engine_interconnect.v#L111-L121)。这种「数据多播 + 握手单播」是 AXI-Stream 多路选择的经典写法。

> 💡 谁来置 `s_interconnect_dir`？正是 offload 模块（见 4.3）。offload 在被触发并占用执行引擎时拉高 `interconnect_dir`，告诉互连「现在走硬件源」；空闲时释放，回到软件源。这个方向信号同时也透传给执行模块的 `s_offload_active`（`m_offload_active`），后者影响移位寄存器是否允许预取数据。

#### 4.2.4 代码实践

**实践目标**：理解 interconnect 在真实工程里如何被接线。

**操作步骤**：
1. 打开 [projects/ad5766_sdz/common/ad5766_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad5766_sdz/common/ad5766_bd.tcl)。
2. 定位三行 `ad_ip_instance`（实例化 execution / axi_spi_engine / interconnect）。
3. 找到把软件源、offload 源分别接到 interconnect `s0_ctrl`、`s1_ctrl` 的两行 `ad_connect`。
4. 找到把方向信号接到 `s_interconnect_ctrl`、把 master 接到 `execution/ctrl` 的两行。

**需要观察的现象**：`axi/spi_engine_ctrl → interconnect/s0_ctrl`（软件走 s0），`axi_ad5766/spi_engine_ctrl → interconnect/s1_ctrl`（offload 走 s1），`axi_ad5766/m_interconnect_ctrl → interconnect/s_interconnect_ctrl`（方向由 offload 决定），`interconnect/m_ctrl → execution/ctrl`（合并后的命令流送给执行模块）。

**预期结果**：你能画出一张「软件源 + offload 源 → interconnect(2选1) → execution → 物理引脚」的拓扑图，并标注出方向控制线的来源。

#### 4.2.5 小练习与答案

**练习 1**：为什么 SDI 数据要同时赋给 `s0_sdi_data` 和 `s1_sdi_data`，而不是只给被选中的一侧？

**参考答案**：因为这是纯组合的多路选择，被选中的那一侧由 `valid/ready` 决定。把数据同时送给两侧可以省去一个数据多路器；未被选中的一侧 `sdi_valid=0`，即使收到同样的数据线电平也不会被消费，握手语义依然正确。

**练习 2**：若 `s_interconnect_dir` 在传输过程中途被翻转，会发生什么？

**参考答案**：会立即把 `m_cmd_valid`/`m_sdo_valid` 切到另一侧，破坏当前传输。因此实际系统中 `s_interconnect_dir` 由 offload 在「完整一段命令序列的开始/结束」处翻转（且必须确保软件侧此刻没有未完成的命令），属于协议级的互斥约定，而非逐拍切换。

---

### 4.3 offload 卸载模式

#### 4.3.1 概念说明

很多 ADI 器件要求「**事件一到立刻采样**」（例如 ADC 的 `CNV` 转换启动信号一到，就要按特定时序读回数据）。如果靠 CPU 响应中断再下发 SPI 命令，延迟通常在微秒级且抖动大。`spi_engine_offload` 解决的就是这个问题：

- 软件事先把一整段命令序列（以及要发送的 SDO 数据）写进 offload 内部的 **命令 RAM**（`cmd_mem`）和 **SDO RAM**（`sdo_mem`）；
- 硬件 `trigger` 信号一到来，offload 立刻把 RAM 里的命令按地址顺序「重放」到 `spi_engine_ctrl` 流上，完全不经 CPU；
- 通过 interconnect 抢占执行模块，offload 在重放期间拉高 `interconnect_dir`，独占执行引擎。

这相当于把一段「SPI 程序」烧进一块小 RAM，由硬件事件当「播放键」。其相对软件模式的吞吐优势在于：**零 CPU 延迟、确定性时序、可被高频周期性触发**。

#### 4.3.2 核心流程

```
【编程阶段（CPU 侧，ctrl_clk 域）】
  写 OFFLOAD0_MEM_RESET → 清空 RAM 指针
  循环写 OFFLOAD0_CMD_FIFO → 每条命令进 cmd_mem[ctrl_cmd_wr_addr]，地址自增
  循环写 OFFLOAD0_SDO_FIFO  → 每个 SDO 数据进 sdo_mem[ctrl_sdo_wr_addr]，地址自增
  写 OFFLOAD0_ENABLE = 1    → 请求进入 offload 模式（经同步握手后 spi_enable=1）

【触发与执行（spi_clk 域）】
  trigger 上升沿到来 且 spi_enable=1 且 当前空闲：
      spi_active <= 1, offload_cmd_valid <= 1
      同时 interconnect_dir <= 1（抢占执行模块，经互连走 s1 通道）
  每个 spi_clk：从 cmd_mem[spi_cmd_rd_addr] 取一条命令发出，spi_cmd_rd_addr++
      当 读地址 + 2 == 写地址 → last_cmd → 本批命令放完
  所有命令执行完且 SDI 无残留 → spi_active <= 0, offload_cmd_valid <= 0
      interconnect_dir 回 0，把执行模块交还给软件源
```

此外，offload 会维护一个 **sync_id 计数器**：每遇到一条 sync 指令（`cmd[15:8]==8'h30`）就把计数器加 1 并替换命令里的 ID 字段，软件据此跟踪「这是第几次重放」。

#### 4.3.3 源码精读

**(1) 命令与 SDO 存储。** offload 用两块寄存器数组当 RAM，深度由参数决定：

```verilog
reg [CMD_MEM_ADDRESS_WIDTH-1:0] ctrl_cmd_wr_addr = 'h00;   // CPU 写指针
reg [CMD_MEM_ADDRESS_WIDTH-1:0] spi_cmd_rd_addr  = 'h00;   // 重放读指针
reg [SDO_MEM_ADDRESS_WIDTH-1:0] ctrl_sdo_wr_addr = 'h00;
reg [SDO_MEM_ADDRESS_WIDTH-1:0] spi_sdo_rd_addr  = 'h00;

reg [15:0]        cmd_mem[0:2**CMD_MEM_ADDRESS_WIDTH-1];   // 命令 RAM
reg [DATA_WIDTH-1:0] sdo_mem[0:2**SDO_MEM_ADDRESS_WIDTH-1]; // SDO 数据 RAM
```

参见 [spi_engine_offload.v:102-108](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L102-L108)。CPU 侧的写入很直接：

```verilog
always @(posedge ctrl_clk) begin
  if (ctrl_mem_reset) ctrl_cmd_wr_addr <= 'h00;
  else if (ctrl_cmd_wr_en) ctrl_cmd_wr_addr <= ctrl_cmd_wr_addr + 1'b1;
end
always @(posedge ctrl_clk) begin
  if (ctrl_cmd_wr_en) cmd_mem[ctrl_cmd_wr_addr] <= ctrl_cmd_wr_data;
end
```

参见 [spi_engine_offload.v:365-375](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L365-L375)（命令写入与写指针）。SDO 侧同理（[spi_engine_offload.v:377-387](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L377-L387)）。

**(2) 触发后的重放状态机。** 两个关键寄存器 `spi_active`（是否正在重放）和 `offload_cmd_valid`（命令是否可发）在 trigger 上升沿置位、在最后一条命令被消费时清零：

```verilog
always @(posedge spi_clk) begin
  if (!spi_resetn) spi_active <= 1'b0;
  else if (!spi_active) begin
    if (trigger_posedge && spi_enable) spi_active <= 1'b1;     // 触发，开始重放
  end else if ((last_cmd_accept || !offload_cmd_valid) &&
               !(offload_disable_pending && sdi_data_valid))
    spi_active <= 1'b0;                                        // 命令放完且 SDI 无残留，结束
end
```

参见 [spi_engine_offload.v:291-308](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L291-L308)。重放读指针在「命令被消费」时递增，并通过「读地址追上写地址」判定本批命令的末尾：

```verilog
always @(posedge spi_clk) begin
  if (!cmd_valid) spi_cmd_rd_addr <= 'h00;
  else if (cmd_ready) begin
    spi_cmd_rd_addr <= spi_cmd_rd_addr_next;                   // 读下一条
    last_cmd <= spi_cmd_rd_addr + 2'h2 == ctrl_cmd_wr_addr;    // 快追上写指针=最后一条
  end
end
assign last_cmd_accept = cmd_ready && last_cmd;
```

参见 [spi_engine_offload.v:331-343](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L331-L343)。这是一种典型的「环形缓冲读追写」判定。

**(3) 抢占执行模块的方向信号。** offload 把内部 `spi_enabled`（重放期间为 1）作为 `interconnect_dir` 输出，并设计了一段「去抖动」握手，确保使能/去使能在两个时钟域之间不会卡死：

```verilog
assign interconnect_dir = spi_enabled;            // ASYNC 情形
...
always @(posedge spi_clk) spi_enabled <= spi_enable | spi_active;
```

参见 [spi_engine_offload.v:205-258](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L205-L258)（`ctrl_enable` → `ctrl_do_enable` → `spi_enable` → `spi_enabled` → `ctrl_is_enabled` 的回环握手）。注释明确说明：`ctrl_do_enable` 在使能时立刻拉高，但**只有在 SPI 域同步回来后**才拉低，避免出现「SPI 域还使能着、控制域却以为已经关了」的悬空状态。

**(4) sync_id 计数。** 每次重放遇到 sync 指令，计数器自增并替换命令字里的 ID：

```verilog
always @(posedge spi_clk) begin
  if (!spi_resetn) spi_sync_id_counter <= 8'b0;
  else if (spi_sync_id_load_s) spi_sync_id_counter <= spi_sync_id_init_s;       // 软件设定的初值
  else if (cmd_valid && cmd_ready && (cmd[15:8] == 8'h30))
    spi_sync_id_counter <= spi_sync_id_counter + 1'b1;                          // 每次 sync 自增
end
assign cmd = (cmd_int_s[15:8] == 8'h30) ? {cmd_int_s[15:8], spi_sync_id_counter} : cmd_int_s;
```

参见 [spi_engine_offload.v:182-194](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v#L182-L194)。这让软件能区分「RAM 里同一段固定命令」被触发执行了第几次——周期性采样场景下非常有用。

#### 4.3.4 代码实践

**实践目标**：对比软件模式与 offload 模式的命令通路，理解卸载的吞吐优势来源。

**操作步骤**：
1. 在 [spi_engine_offload.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/spi_engine_offload/spi_engine_offload.v) 中找到「CPU 写命令 RAM」与「硬件重放命令」两段 always 块，分别标注它们所在的时钟域（`ctrl_clk` vs `spi_clk`）。
2. 在 [axi_spi_engine.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v) 中找到软件写命令 FIFO 的那一行（搜索 `up_waddr_s == 8'h38`），对比：软件模式下每条命令都要走一次 AXI 写事务。
3. 设想一个 1 MHz 的周期性 `trigger`：软件模式需要在每次触发前/后由 CPU 经 AXI 重填命令 FIFO；offload 模式只需在初始化时写一次 RAM，之后硬件每拍自动重放。

**需要观察的现象**：offload 的命令来源是**片上 RAM**，重放只需 `spi_cmd_rd_addr` 自增，延迟只有几个 `spi_clk`；软件模式的命令来源是**经 AXI 总线、跨时钟域 FIFO 的 CPU 写**，路径长得多。

**预期结果**：你能用一句话概括 offload 的优势——「把命令序列固化进 RAM，由硬件事件触发、零 CPU 介入地重放，从而获得确定性低延迟与可高频周期触发的能力」。

> 待本地验证：若有支持 offload 的工程（如 ad5766、cn0561），可在 no-OS 软件里找到「写 OFFLOAD0_CMD_FIFO → 置 OFFLOAD0_ENABLE → 等待 trigger」的代码片段，对照本节的状态机阅读。

#### 4.3.5 小练习与答案

**练习 1**：offload 用「读地址 + 2 == 写地址」来判定 `last_cmd`，为什么是 +2 而不是 +1 或 ==？

**参考答案**：因为判断发生在「命令被消费（`cmd_ready`）的那一拍」，此时 `spi_cmd_rd_addr` 还未更新为下一个地址。用 `当前地址 + 2` 等于写指针，意味着「再往后读一个就是写指针」，即当前正被消费的命令是倒数第二条、读出下一个就是最后一条；这是为了让 `last_cmd_accept` 能在最后一条真正被消费时精确置位，避免提前或漏判。这属于实现细节，具体偏移与流水深度相关。

**练习 2**：offload 模式下，软件还能同时用 `axi_spi_engine` 下发命令吗？

**参考答案**：不能在同一时刻。offload 重放期间 `interconnect_dir=1`，互连把软件源（s0）的 `ready` 强制为 0，软件下发的命令会被反压（停在命令 FIFO 里）。只有当 offload 重放结束、`interconnect_dir` 回 0 后，软件源才重新获得执行引擎。两者是**分时独占**关系。

---

### 4.4 axi_spi_engine 顶层封装

#### 4.4.1 概念说明

`axi_spi_engine` 是软件看得见的那一层。它的职责不是「执行 SPI」，而是把**软件的寄存器读写**翻译成**命令流**交给执行模块，并提供一组 FIFO 做缓冲、一组中断做通知。可以把它理解为「软件侧的命令源 + 数据缓冲池」。

它内部做三件事：

1. **寄存器接口**：经 `up_axi` 桥（详见 u4-l5）把 AXI4-Lite 翻译成 `up_wreq/up_rreq`；再用一个 `case` 把这些请求分派到各个寄存器与 FIFO 端口；
2. **四组 FIFO**：CMD（软件下发命令）、SDO（要发送的数据）、SDI（收到的数据）、SYNC（同步事件）；前三者跨 `s_axi_aclk` 与 `spi_clk` 时钟域；
3. **offload 控制通道**：当 `OFFLOAD_EN=1` 时，额外提供几组 FIFO/寄存器用来给 offload 模块的 `cmd_mem`/`sdo_mem` 灌数据、使能/复位 offload。

注意它有两种「存储映射接口」模式，由参数 `MM_IF_TYPE` 选择：`0` = 标准 AXI4-Lite（`S_AXI`，默认），`1` = ADI 自定义的 `UP_FIFO` 接口。源码用两个 `generate` 分支分别处理。

#### 4.4.2 核心流程

```
【CPU 写命令】
  CPU 向 CMD_FIFO 寄存器(0x38) 写一个 16 位值
    → up_wreq 拉高 → case 命中 0x38 → cmd_fifo_in_valid=1
    → i_cmd_fifo(util_axis_fifo) 把数据从 s_axi_aclk 跨到 spi_clk 域
    → 在 spi_clk 域经 spi_engine_ctrl 的 cmd_valid/cmd_data 送给（经 interconnect 的）执行模块

【CPU 读采样】
  执行模块采到数据 → sdi_data_valid 拉高 → i_sdi_fifo(util_axis_fifo_asym) 把
    NUM_OF_SDIO 路宽的数据拆成单路，跨回 s_axi_aclk 域
  CPU 读 SDI_FIFO 寄存器(0x3a) → 每次读弹出一个 lane 的数据

【中断与同步】
  执行模块执行到 sync 指令 → sync 流事件 → i_sync_fifo 跨域 → 更新 SYNC_ID 寄存器
    → 置 SYNC_EVENT 中断 → irq 拉高（与掩码相与）→ 通知 CPU
```

#### 4.4.3 源码精读

**(1) up_axi 桥的例化（MM_IF_TYPE=S_AXI 分支）。** 这正是 u4-l5 讲过的 AXI4-Lite → `up_*` 翻译桥：

```verilog
generate if (MM_IF_TYPE == S_AXI) begin
  assign clk = s_axi_aclk; assign rstn = s_axi_aresetn;
  up_axi #(.AXI_ADDRESS_WIDTH(16)) i_up_axi (
    .up_axi_awvalid(s_axi_awvalid), ... .up_axi_rready(s_axi_rready),
    .up_wreq(up_wreq_s), .up_waddr(up_waddr_s), .up_wdata(up_wdata_s), .up_wack(up_wack_ff),
    .up_rreq(up_rreq_s), .up_raddr(up_raddr_s), .up_rdata(up_rdata_ff), .up_rack(up_rack_ff));
end endgenerate
```

参见 [axi_spi_engine.v:207-250](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L207-L250)。桥把 5 通道 AXI 握手化简成 `up_wreq/up_rreq` 两对，后续逻辑只需面向这两个请求。

**(2) 寄存器读分派。** 一个 `case (up_raddr_s)` 列出所有可读寄存器，结果锁存进 `up_rdata_ff`：

```verilog
always @(posedge clk) begin
  case (up_raddr_s)
    8'h00: up_rdata_ff <= PCORE_VERSION;                 // 版本
    8'h02: up_rdata_ff <= up_scratch;                    // 刮擦寄存器
    8'h03: up_rdata_ff <= {8'b0, NUM_OF_SDIO, DATA_WIDTH};
    8'h20: up_rdata_ff <= up_irq_mask;                   // 中断掩码
    8'h21: up_rdata_ff <= up_irq_pending;                // 中断挂起
    8'h30: up_rdata_ff <= sync_id;                       // 同步 ID
    8'h34: up_rdata_ff <= cmd_fifo_room;                 // CMD FIFO 剩余空间
    8'h36: up_rdata_ff <= sdi_level_s;                   // SDI FIFO 数据量
    8'h3a: up_rdata_ff <= sdi_fifo_out_data[DATA_WIDTH-1:0]; // 读 SDI（弹出）
    8'h3c: up_rdata_ff <= sdi_fifo_out_data[DATA_WIDTH-1:0]; // PEEK（不弹出）
    ...
  endcase
end
```

参见 [axi_spi_engine.v:335-362](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L335-L362)。注意地址与 `docs/regmap/adi_regmap_spi_engine.txt` 一一对应（如 0x00 VERSION、0x34 CMD_FIFO_ROOM）。

**(3) 命令 FIFO 的写入与跨域。** 写 `0x38` 即把 16 位数据推进 CMD FIFO：

```verilog
assign cmd_fifo_in_valid = up_wreq_s == 1'b1 && up_waddr_s == 8'h38;
assign cmd_fifo_in_data  = up_wdata_s[15:0];

util_axis_fifo #(.DATA_WIDTH(16), .ADDRESS_WIDTH(CMD_FIFO_ADDRESS_WIDTH),
                 .ASYNC_CLK(ASYNC_SPI_CLK), ...) i_cmd_fifo (
  .s_axis_aclk(clk), .m_axis_aclk(spi_clk),
  .s_axis_valid(cmd_fifo_in_valid), .s_axis_data(cmd_fifo_in_data),
  .m_axis_valid(cmd_valid), .m_axis_data(cmd_data), ...);
```

参见 [axi_spi_engine.v:406-439](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L406-L439)。`util_axis_fifo`（详见 u5-l3）承担跨时钟域与缓冲，输出直接连到 `spi_engine_ctrl` 接口的 `cmd_valid/cmd_data`，这就是「软件命令 → 执行模块」的最后一公里。SDO FIFO（`0x39`）结构相同，SDI FIFO 则用 `util_axis_fifo_asym` 做位宽转换（多 lane 宽 → 单 lane 窄，[axi_spi_engine.v:507-539](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L507-L539)）。

**(4) 中断聚合。** 5 个内部中断源（命令 FIFO 将空、SDO FIFO 将空、SDI FIFO 将满、sync 事件、offload sync 事件）相或后输出 `irq`：

```verilog
assign up_irq_source = {offload_sync_id_pending, sync_id_pending,
                        up_sdi_fifo_almost_full, up_sdo_fifo_almost_empty, up_cmd_fifo_almost_empty};
assign up_irq_pending = up_irq_mask & up_irq_source;
always @(posedge clk) if (!rstn) irq <= 1'b0; else irq <= |up_irq_pending;
```

参见 [axi_spi_engine.v:271-290](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L271-L290)。这正是 [axi_spi_engine.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/spi_engine/axi_spi_engine.rst) 中 `IRQ = |(IRQ_SOURCE & IRQ_MASK)` 的硬件实现。

**(5) 软件复位与 offload 控制。** 写 `0x10` 触发软件复位（`up_sw_reset`），它同时派生出送给执行模块的 `spi_resetn`；offload 的使能、复位、命令/SDO 灌入寄存器在 `0x40`–`0x45`：

```verilog
always @(posedge clk) begin
  if (up_sw_resetn == 1'b0) begin up_irq_mask <= 'h00;
    offload0_enable_reg <= 1'b0; offload0_mem_reset_reg <= 1'b0;
  end else if (up_wreq_s) case (up_waddr_s)
    8'h20: up_irq_mask <= up_wdata_s;
    8'h40: offload0_enable_reg    <= up_wdata_s[0];     // 使能 offload
    8'h42: offload0_mem_reset_reg <= up_wdata_s[0];     // 复位 offload RAM
  endcase
end
```

参见 [axi_spi_engine.v:313-327](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L313-L327)。可见 `axi_spi_engine` 同时肩负「软件命令源」与「offload 控制台」两个角色——后者通过 `spi_engine_offload_ctrl0` 接口（见 `axi_spi_engine_ip.tcl` 中的 `adi_add_bus` 声明）连到 offload 模块。

#### 4.4.4 代码实践

**实践目标**：从寄存器表回溯到源码，验证「软件写哪个寄存器 = 触发哪条硬件通路」。

**操作步骤**：
1. 打开 [docs/regmap/adi_regmap_spi_engine.txt](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_spi_engine.txt)，记录 `VERSION`(0x00)、`SCRATCH`(0x02)、`IRQ_MASK`(0x20)、`SYNC_ID`(0x30)、`CMD_FIFO`(0x38) 等寄存器的地址。
2. 在 [axi_spi_engine.v:335-362](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/spi_engine/axi_spi_engine/axi_spi_engine.v#L335-L362) 的读 `case` 中逐一找到它们。
3. 针对 `CMD_FIFO`(0x38)，顺着 `cmd_fifo_in_valid` → `i_cmd_fifo` → `cmd_valid/cmd_data` 追踪：一次 `0x38` 写最终变成了执行模块 `ctrl` 接口上的一条命令。

**需要观察的现象**：每个寄存器地址在源码里都有两处出现——写侧（`up_waddr_s == 8'hXX` 触发某动作）和读侧（`case (up_raddr_s)` 返回某值）。

**预期结果**：你能填出一张「地址 → 写语义 → 读语义 → 连到哪个 FIFO/信号」的对照表，例如 `0x38` 写=推命令入 CMD FIFO、读=未实现（只写）；`0x34` 读=CMD FIFO 剩余空间。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CMD/SDO FIFO 用 `util_axis_fifo`，而 SDI FIFO 要用 `util_axis_fifo_asym`？

**参考答案**：CMD（16 位）与 SDO（单 lane 的 DATA_WIDTH 位）两侧位宽一致，普通 FIFO 即可；而 SDI 侧在 SPI 域是 `NUM_OF_SDIO × DATA_WIDTH` 宽（多 lane 并行），在 AXI 域只读出单 lane 的 DATA_WIDTH 位，需要**非对称位宽**转换，故用 `util_axis_fifo_asym`（详见 u5-l3 关于 `ad_mem_asym` / 宽窄位宽转换的讨论）。

**练习 2**：`SDI_FIFO`(0x3a) 与 `SDI_FIFO_PEEK`(0x3c) 都返回 `sdi_fifo_out_data`，二者有何区别？

**参考答案**：读 `0x3a` 会把 `sdi_fifo_out_ready` 拉高，从而**弹出** FIFO 一项；读 `0x3c`（PEEK）只返回队首数据**而不弹出**。软件可在不确定是否要消费时先 peek 看一眼，确认后再正式读 `0x3a`。

---

## 5. 综合实践

**任务**：以 `ad5766_sdz` 工程为样本，画出 SPI Engine 框架的**完整命令通路拓扑图**，并用本讲学到的概念解释每一跳。

**步骤**：

1. 打开 [projects/ad5766_sdz/common/ad5766_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad5766_sdz/common/ad5766_bd.tcl)，找到 `ad_ip_instance spi_engine_execution execution`、`ad_ip_instance axi_spi_engine axi`、`ad_ip_instance spi_engine_interconnect interconnect` 三行（约 22-25 行）。
2. 找到 offload 的来源：在本工程中，offload 被封装在 `axi_ad5766` 数据转换器 IP 内部，它对外暴露 `spi_engine_ctrl`（s1 命令源）与 `m_interconnect_ctrl`（方向信号）。确认 `axi/spi_engine_offload_ctrl0 → axi_ad5766/spi_engine_offload_ctrl` 这条「软件灌 offload RAM」的通路。
3. 用纸/工具画出如下拓扑并标注方向：
   - 软件源：`axi_spi_engine.spi_engine_ctrl → interconnect.s0_ctrl`
   - offload 源：`axi_ad5766.spi_engine_ctrl → interconnect.s1_ctrl`
   - 方向：`axi_ad5766.m_interconnect_ctrl → interconnect.s_interconnect_ctrl`
   - 合流：`interconnect.m_ctrl → execution.ctrl`
   - 物理引脚：`execution.spi → m_spi`（顶层 `spi` 接口）
4. 用一段话回答：当 CPU 想**手动**读一次 DAC 寄存器时，命令从哪条路走？当外部事件触发 `axi_ad5766` 的 offload 自动回放一段配置序列时，命令又从哪条路走？两条路如何互斥？

**预期产出**：一张标注清楚的拓扑图 + 一段「软件通路 vs offload 通路 vs 互斥机制」的说明。若想加深理解，可进一步打开 [docs/library/spi_engine/spi_engine.svg](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/spi_engine/spi_engine.svg) 对照官方框图。

## 6. 本讲小结

- SPI Engine 是一个**可编程** SPI 控制器框架：把 SPI 时序抽象成 16 位指令流，由执行模块翻译成物理波形，从而用同一套硬件适配各种 ADI 器件。
- 框架按职责拆成四个独立 IP：`execution`（执行引擎，框架核心）、`interconnect`（命令源 2 选 1）、`offload`（硬件重放）、`axi_spi_engine`（软件 AXI 入口）；它们经统一的 `spi_engine_ctrl` 接口（CMD/SDO/SDI/SYNC 四条 AXI-Stream）拼接。
- `spi_engine_execution` 用一个状态机 + 移位寄存器把传输/片选/配置/同步/睡眠指令变成 SCK/MOSI/CS/MISO，SCLK 频率由 \(f_{sclk}=f_{clk}/((div+1)\times 2)\) 决定。
- `spi_engine_offload` 把命令序列预存进 `cmd_mem`/`sdo_mem` RAM，触发后零 CPU 延迟重放，并通过 `interconnect_dir` 抢占执行模块——这是相对软件模式的吞吐与确定性优势所在。
- `axi_spi_engine` 是「软件看得见的那层」：`up_axi` 桥 + 寄存器 `case` 分派 + 四组 `util_axis_fifo`（跨时钟域）把软件的寄存器读写翻译成命令流，并以中断聚合（`IRQ = |(IRQ_SOURCE & IRQ_MASK)`）通知软件。
- 关键架构事实：`axi_spi_engine` 不内含 execution/interconnect/offload，四块的拼装发生在**工程块设计 Tcl**（如 `ad5766_bd.tcl`）里，理解这一点才能正确阅读任何 SPI Engine 工程。

## 7. 下一步学习建议

- **横向对比 JESD204 框架**（u6-l1）：两者都是「分层 + 接口标准化」的框架思想，但 JESD204 面向高速串行链路、SPI Engine 面向低速可编程控制，对照阅读能加深对「框架式 IP 设计」的理解。
- **阅读指令集规范全文**：[docs/library/spi_engine/instruction-format.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/spi_engine/instruction-format.rst)，把每条指令的位域与执行模块源码逐字段对应。
- **跟踪一个数据转换器工程**：如 `projects/ad40xx_fmc` 或 `projects/ad4630`（精密 SAR ADC），看它的块设计如何把 SPI Engine 与 DMA、数据通路 IP 组合，把本讲的「控制平面」与 u5 的「数据平面」联系起来。
- **结合软件驱动阅读**：no-OS 的 `drivers/axi_core/spi_engine/spi_engine.c` 或 Linux 的 `drivers/spi/spi-axi-spi-engine.c`，从软件侧反向验证本讲描述的寄存器与命令流语义。
- **进入高级主题**：随后可学习 u8 的时序约束与收发器主题，了解 SPI Engine 在高 SCLK 速率下如何用 `ECHO_SCLK`（见 `spi_engine_execution_shiftreg.v` 的 echo 分支）和 `SDI_DELAY` 改善采样裕量。
