# SPI 主从

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SPI 协议的四根线（SCLK / MOSI / MISO / SS）与两个关键配置位 CPOL、CPHA 的含义，并能解释「模式 0」的采样/-launch 约定。
- 读懂 OH! 里 `spi` 顶层的「主机 + 从机 + emesh 多路选择」三件式结构，以及它如何与第 5 单元的 emesh 104 位包对接。
- 沿着 `spi_master` 的发送通路追一遍：寄存器配置 → 发送 FIFO → 状态机 → 移位输出，并解释 `spi_master_io` 里 SCLK 的产生与 MOSI/MISO 的移位时机。
- 沿着 `spi_slave` 的接收通路追一遍：从机如何用「命令字 + 地址」驱动一个可寻址寄存器堆，并把「远程取数」翻译成一次 emesh 事务。
- 理解贯穿主从的核心积木——`oh_par2ser`（并→串）与 `oh_ser2par`（串→并）移位寄存器。

本讲是第 6 单元（可配置外设 IP）的第三讲，承接 u6-l1（`.vh` 寄存器映射模式）与 u6-l2（GPIO 全解析）。GPIO 教的是「emesh 读写寄存器」最朴素的形态；SPI 则把同一套思路扩展到一个**带串行物理层协议**的外设——寄存器不再由 emesh 包直接驱动，而是先被打包成字节流、经 SPI 线串行移位、再在远端重组。

## 2. 前置知识

在进入源码前，先用最朴素的方式把 SPI 讲清楚。

### 2.1 SPI 是什么

SPI（Serial Peripheral Interface，串行外设接口）是一种**主从式、同步、全双工、串行**的通信协议。一次 SPI 传输发生在两个角色之间：

- **主机（Master）**：产生时钟 SCLK、拉低片选 SS、发起传输。
- **从机（Slave）**：被动跟随主机时钟、在 SS 被选中时参与传输。

它们之间最少用四根线相连：

| 信号 | 方向（主机视角） | 含义 |
|------|------------------|------|
| SCLK | 主机 → 从机 | 串行时钟，由主机产生 |
| MOSI | 主机 → 从机 | Master Out, Slave In（主机输出） |
| MISO | 从机 → 主机 | Master In, Slave Out（主机输入） |
| SS   | 主机 → 从机 | Slave Select，片选，**低有效** |

> 在 OH! 源码里，这些信号直接命名为 `sclk / mosi / miso / ss`，注意 `ss` 是低有效（选中从机时为 0）。

### 2.2 移位即传输

SPI 的精髓是「**边移位、边交换**」：每个时钟周期，主机把 MOSI 的一位移出、同时把 MISO 的一位移入；从机做对称的相反动作。移够 8 拍，主机和从机就**互相交换了一个字节**。所以 SPI 天然是全双工的——没有「纯发」或「纯收」，发一个字节必然同时收到一个字节（哪怕收到的没意义，这是所谓的 dummy byte）。

这就解释了为什么本讲的核心积木是**移位寄存器**：发送端用「并→串」（并行字节 → 逐位挪出），接收端用「串→并」（逐位挪入 → 拼成并行字节）。

### 2.3 CPOL 与 CPHA：四种模式

时钟有两个可调属性，组合出四种「SPI 模式」：

- **CPOL（Clock Polarity，时钟极性）**：SCLK 空闲时的电平。CPOL=0 空闲低、CPOL=1 空闲高。
- **CPHA（Clock Phase，时钟相位）**：在哪个沿采样数据。CPHA=0 在**第一个**时钟沿（前沿）采样、在第二个沿（后沿）切换数据；CPHA=1 反过来。

最常用的是 **模式 0（CPOL=0, CPHA=0）**：SCLK 空闲低，数据在**上升沿采样**、在**下降沿切换**。本讲的代码实践就是画出模式 0 的时序图。

> 一个容易混的点：「采样（capture/sample）」指接收方把数据锁存进寄存器；「切换/launch」指发送方改变数据线电平。两者必须错开半个时钟，否则接收方锁到的是正在翻转的脏数据。

### 2.4 你需要带进来的旧知识

- **emesh 104 位包**（u5-l1）：控制字节（含 write、datamode）+ dstaddr + data + srcaddr。本讲里 `spi` 顶层对外就是这套接口。
- **`.vh` 寄存器映射**（u6-l1）：用大写宏给每个寄存器分配地址编号，再用 `dstaddr` 的一段切位译码产生写选通。
- **stdlib 时序与时钟原语**（u2-l2、u2-l3）：D 触发器、`oh_clockdiv` 分频器、`oh_dsync` 同步器、`oh_rise2pulse` 边沿检测。SPI 状态机大量复用这些。
- **同步 FIFO**（u3-l2）：主机发送路径用 `oh_fifo_sync` 缓冲待发字节。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [spi/hdl/spi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v) | 顶层。把一个 `spi_master` 和一个 `spi_slave` 并排实例化，用 `emesh_mux` 合并它们对核的响应。 |
| [spi/hdl/spi_regmap.vh](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_regmap.vh) | 寄存器地址宏（`SPI_CONFIG`/`SPI_TX`/...）与命令字宏（`SPI_WR`/`SPI_RD`/`SPI_FETCH`）。主从共用。 |
| [spi/hdl/spi_master.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master.v) | 主机顶层。组装 `spi_master_regs`（配置/读回）+ `spi_master_fifo`（发送 FIFO）+ `spi_master_io`（IO 状态机）。 |
| [spi/hdl/spi_master_regs.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_regs.v) | 主机寄存器：解码 emesh 写、拆 `SPI_CONFIG` 各配置位、读回拼包。 |
| [spi/hdl/spi_master_fifo.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_fifo.v) | 把一次 emesh 写 `SPI_TX` 的 104 位包**切片成字节**写入 `oh_fifo_sync`。 |
| [spi/hdl/spi_master_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v) | 主机 IO 状态机：产生 SCLK、管理 SS、驱动 MOSI 移位、采样 MISO。**本讲代码实践的核心文件**。 |
| [spi/hdl/spi_slave.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave.v) | 从机顶层。组装 `spi_slave_regs`（寄存器堆）+ `spi_slave_io`（IO 状态机）。 |
| [spi/hdl/spi_slave_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v) | 从机 IO：解析命令字、按字节驱动寄存器堆、可发起远程 emesh 事务。 |
| [spi/hdl/spi_slave_regs.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v) | 从机寄存器堆：config/status/user 寄存器，并把全部寄存器拼成 512 位向量供读回。 |
| [stdlib/rtl/oh_par2ser.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_par2ser.v) | 并→串移位寄存器（主机发送、从机发送共用）。 |
| [stdlib/rtl/oh_ser2par.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ser2par.v) | 串→并移位寄存器（主机接收、从机接收共用）。 |
| [spi/dv/dut_spi.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/dv/dut_spi.v) | 测试用 dut 包装：把一个 `spi` 当主机、另一个当从机，环回对接，再挂一个 `ememory` 当远端存储。 |

> 老规矩（见 u1-l2、u6-l2）：**代码是事实，文档可能滞后**。本讲在遇到 README 与源码不一致时一律以 RTL 为准，并显式指出落差。

## 4. 核心概念与源码讲解

### 4.1 SPI 顶层与寄存器映射：一个壳，装着主从两个核

#### 4.1.1 概念说明

OH! 的 `spi` 顶层很有意思：它**同时**实例化了一个主机和一个从机，二者各自有独立的物理引脚（`m_*` 给主机、`s_*` 给从机），却**共享同一组 emesh 接口**（`access_in/packet_in/wait_in` 与 `access_out/packet_out/wait_out`）。换句话说，同一个 emesh 地址窗口写进去的事务，既可能去配置/驱动主机，也可能被从机产生的远程请求占用出口——顶层用一个 `emesh_mux` 把两边的响应合并回核。

这是一种「**可配置为主或从**」的设计：板子上同一颗 SPI IP，既可当主控去点灯/读传感器，也可当从机被远端芯片访问。主从不是互斥的两种模式，而是并存的两个子模块。

#### 4.1.2 核心流程

从 emesh 核的角度看一次访问 `spi`：

1. 核发起一个 104 位 emesh 事务（`access_in` + `packet_in`）。
2. 该事务**同时**送进 `spi_master` 和 `spi_slave`——两边各自解码地址：
   - 命中主机寄存器（`SPI_CONFIG`/`SPI_CLKDIV`/`SPI_TX` 等）→ 主机处理。
   - 命中从机寄存器（`SPI_CONFIG`/`SPI_USER` 等）→ 从机处理。
3. 两边的响应（`m_*_out` 与 `s_*_out`）进 `emesh_mux`，二选一输出给核。
4. 任一边需要反压（`wait_out`）→ 顶层 `wait_out = s_wait_out | m_wait_out` 立即拉高。

主机的物理输出（`m_sclk/m_mosi/m_ss`）驱动外部从设备；从机的物理输入（`s_sclk/s_mosi/s_ss`）接收外部主控。注意 `spi_irq`（中断）只接了从机。

#### 4.1.3 源码精读

顶层端口分三组：emesh 核接口、主机 IO、从机 IO。[spi/hdl/spi.v:8-35](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L8-L35) 用注释逐行标了每根线的含义。参数 `UREGS=13` 决定从机用户寄存器数量，`PW=104` 是 emesh 包宽。

主机和从机的实例化都用了 verilog-mode 的 `AUTO_TEMPLATE`，把端口名按规则重映射——例如把主机的 `sclk/mosi/ss/miso` 接到顶层的 `m_*` 线，把从机的同名端口接到 `s_*` 线：

[spi/hdl/spi.v:65-82](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L65-L82) 实例化主机（模板把 `.sclk (m_sclk)` 等）；[spi/hdl/spi.v:97-117](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L97-L117) 实例化从机。两边的 `access_in/packet_in` 都接同一个核侧输入，意味着地址译码「谁命中谁处理」。

合并响应与反压的两行最关键：

```verilog
assign wait_out = s_wait_out | m_wait_out;

emesh_mux #(.N(2), .AW(AW), .PW(PW))
emesh_mux ( ... .access_in({s_access_out,m_access_out}),
                .packet_in({s_packet_out,m_packet_out}) ... );
```

[spi/hdl/spi.v:123-136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi.v#L123-L136)：反压取两边之或（任一忙就反压）；响应用第 5 单元讲过的 `emesh_mux`（固定优先级 N 选 1，见 u5-l2）二选一。`emesh_mux` 在仓库中实位于 [emesh/hdl/emesh_mux.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/emesh/hdl/emesh_mux.v)。

寄存器地图在 [spi/hdl/spi_regmap.vh:11-18](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_regmap.vh#L11-L18)，沿用 u6-l1 讲过的 `ifndef/define/endif` 守卫 + 大写宏模式。注意地址**允许跳号**：

```verilog
`define SPI_CONFIG   6'd0   // 配置
`define SPI_STATUS   6'd1   // 状态
`define SPI_CLKDIV   6'd2   // 波特率（主机）
`define SPI_CMD      6'd3   // 手动 SS 控制（主机）
`define SPI_TX       6'd8   // 发送 FIFO / 返回数据
`define SPI_RX0      6'd16  // 接收低 32 位
`define SPI_RX1      6'd20  // 接收高 32 位
`define SPI_USER     6'd32  // 用户寄存器（从机）
```

跳号是为了给每个「宽寄存器」预留一组字节地址（如 `SPI_TX=8` 后面 8 个字节是 TX 的 0~7 号字节）。同一个 `regmap.vh` 还定义了**命令字**（不是寄存器地址，而是串行协议里从机解析的命令编码），见 [spi/hdl/spi_regmap.vh:21-23](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_regmap.vh#L21-L23)：

```verilog
`define SPI_WR    2'b00   // 写
`define SPI_RD    2'b10   // 读
`define SPI_FETCH 2'b11   // 远程取数（触发一次 emesh 事务）
```

命令字占命令寄存器的高 2 位 `[7:6]`，这个细节在 4.4 讲从机时要用。

#### 4.1.4 代码实践

**目标**：用纸笔（或文本编辑器）画出 `spi` 顶层的「盒子图」，把主、从、`emesh_mux` 三者的连线理清。

**步骤**：
1. 画出 `spi` 顶层方框，左侧是 emesh 核接口（`access_in/packet_in/wait_in` 进，`access_out/packet_out/wait_out` 出）。
2. 在框内并排画 `spi_master` 和 `spi_slave` 两个子框。
3. 把 `access_in/packet_in` **同时**连到两个子框的输入。
4. 两个子框的 `access_out/packet_out` 汇入一个 `emesh_mux` 方框，输出到顶层右侧。
5. 两个子框的 `wait_out` 经一个或门得到顶层 `wait_out`。
6. 分别从主框引出 `m_sclk/m_mosi/m_ss/m_miso`，从从框引出 `s_sclk/s_mosi/s_ss/s_miso`。

**预期结果**：你会清楚看到「emesh 接口共享、物理 IO 分离」的结构——这正是 `spi.v` 把主从做成共存而非互斥的关键。

#### 4.1.5 小练习与答案

**练习 1**：顶层 `wait_out = s_wait_out | m_wait_out`。为什么用「或」而不是 `emesh_mux` 那样的二选一？

**答案**：反压必须保守——只要主、从任一边还没准备好接收核的下一次访问，就必须告诉核「先别发」，否则会丢事务。而 `access_out/packet_out` 是响应数据，同一拍只可能有一边产生有效响应，所以用 `emesh_mux` 二选一即可。两者性质不同：反压是「能否继续」，响应是「谁的数据」。

**练习 2**：`SPI_TX` 的宏值是 `6'd8`，但物理上对应 8 个字节地址。这 8 个字节地址分别是哪些值？

**答案**：`6'd8` 到 `6'd15`（即 8~15）。因为地址低 3 位在这组里是「字节编号」，主机的 `spi_master_fifo` 正是用 `dstaddr_in[5:0]==SPI_TX`（=8）来选通整组，再用 `datamode` 决定切几个字节（见 4.3.3）。

---

### 4.2 移位寄存器原语：oh_par2ser 与 oh_ser2par

#### 4.2.1 概念说明

SPI 的一切都建立在「逐位移位」上。OH! 把这个动作抽象成两个 stdlib 原语，主从两机都在用：

- **`oh_par2ser`（并→串）**：送入一个并行宽字 `din[PW-1:0]`，每个 `shift` 拍把一比特从 `dout[SW-1:0]` 挪出去。主机用它把字节推上 MOSI，从机用它把字节推上 MISO。
- **`oh_ser2par`（串→并）**：送入串行 `din[SW-1:0]`，每个 `shift` 拍把一比特挪进 `dout[PW-1:0]`，移够 `PW/SW` 拍得到完整并行字。主机用它从 MISO 收字节，从机用它从 MOSI 收字节。

二者是互逆过程：一个拆、一个拼；一个发、一个收。理解了它们，主从的移位就不再神秘。

参数 `PW`（parallel width）与 `SW`（serial width，通常 1）决定移位次数，序列化因子 \( \text{CW} = \lceil\log_2(\text{PW}/\text{SW})\rceil \) 是移位计数器位宽，需要移 \( \text{PW}/\text{SW} \) 次才能搬完一个字。

#### 4.2.2 核心流程

`oh_par2ser` 的发送循环：

1. `load` 有效且不忙 → 把 `din` 整体装入 `shiftreg`，同时把 `datasize` 装入计数器 `count`。
2. 此后每个 `shift` 拍：`count` 减 1，`shiftreg` 向「输出口」方向挪 `SW` 位，空出的位填 `fill`。
3. `count` 减到 0 → `busy` 变 0，`access_out`（数据有效）拉低，一次发送结束。

`lsbfirst` 决定挪位方向：LSB 先发则向低位方向挪、从最低位吐出；MSB 先发则向高位方向挪、从最高位吐出。

`oh_ser2par` 更简单：没有计数器，每个 `shift` 拍把 `din` 挪进 `dout` 的一端，方向同样由 `lsbfirst` 决定。

#### 4.2.3 源码精读

`oh_par2ser` 的核心是这段移位逻辑（注意 LSB/MSB 两个分支）：

[stdlib/rtl/oh_par2ser.v:55-67](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_par2ser.v#L55-L67)：LSB 先发时 `shiftreg = {{(SW){fill}}, shiftreg[PW-1:SW]}`（整体右移、高位补 `fill`，从 `shiftreg[SW-1:0]` 吐出）；MSB 先发时 `{shiftreg[PW-SW-1:0],{(SW){fill}}}`（整体左移、低位补 `fill`，从 `shiftreg[PW-1:PW-SW]` 吐出）。

计数器与「忙」标志在 [stdlib/rtl/oh_par2ser.v:37-52](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_par2ser.v#L37-L52)：`start_transfer = load & ~wait_in & ~busy`；`busy = |count`（计数器非零即忙）；`access_out = busy`（忙期间数据有效）；`wait_out = wait_in | busy`（忙期间反压上游）。

`oh_ser2par` 的全部移位逻辑只有几行：

```verilog
always @ (posedge clk)
  if(shift & lsbfirst)
    dout[PW-1:0] <= {din[SW-1:0], dout[PW-1:SW]};   // 右移，新位进高端
  else if(shift)
    dout[PW-1:0] <= {dout[PW-SW-1:0], din[SW-1:0]};  // 左移，新位进低端
```

[stdlib/rtl/oh_ser2par.v:23-27](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/stdlib/rtl/oh_ser2par.v#L23-L27)。注意它**没有复位**（`always @ (posedge clk)` 不带 `negedge nreset`），上电初值是 `'x`——这要求使用方在采样前先保证移满完整位数，否则会读到脏位。这一点主从都需小心处理（见 4.3.4、4.4.3）。

#### 4.2.4 代码实践

**目标**：用一个最小 testbench 验证 `oh_par2ser` 的 MSB 先发与 LSB 先发，确认吐位顺序。

**步骤**：
1. 新建一个仿真顶层，例化 `oh_par2ser #(.PW(8), .SW(1))`。
2. 令 `din = 8'b1011_0010`、`datasize = 7`（移 8 次）、`load` 给一拍脉冲。
3. 每个 `clk` 拉高 `shift`，观察 `dout`。
4. 分两次：`lsbfirst=0`（MSB 先发）和 `lsbfirst=1`（LSB 先发）。

**需要观察**：
- MSB 先发时，`dout` 依次为 `1,0,1,1,0,0,1,0`（即 `1011_0010` 从高位到低位）。
- LSB 先发时，`dout` 依次为 `0,1,0,0,1,1,0,1`（即从低位到高位）。

**预期结果**：`dout` 的位序与 `lsbfirst` 严格对应；`busy`（`access_out`）在 `count` 归零后落下。

**待本地验证**：若直接用 OH! 的 `scripts/build.sh`+`sim.sh` 跑，需先把 `oh_par2ser` 包成 `dut` 契约（见 u4-l3），或写一个独立的自包含 testbench。本仓库脚本存在路径遗留问题（见 u1-l3），开箱即跑不保证成功。

#### 4.2.5 小练习与答案

**练习 1**：`oh_par2ser` 的 `datasize` 为什么是 `8'd7` 而不是 `8'd8` 来表示「移 8 位」？

**答案**：因为 `datasize` 装入 `count` 后，每移一位 `count` 减 1，当 `count` 从 7 减到 0 共经历 8 个值（7,6,...,0），对应 8 次移位。`busy = |count` 在 `count=0` 时落下，正好移完 8 位。所以「移 N 位」就写 `N-1`。

**练习 2**：`oh_ser2par` 为什么不带复位？这会带来什么风险？

**答案**：它是纯组合移位的累计器，复位会清掉已收到的内容；设计上靠「移满整字再读」保证有效。风险是：若使用方在未移满时就读 `dout`，会读到上电 `'x` 或上一次残留的脏位。主从代码里都通过「按完整字节/事务移位后再采样」来规避。

---

### 4.3 SPI 主机：配置、FIFO 与发送状态机

#### 4.3.1 概念说明

主机要做的事可以拆成三段，正好对应 `spi_master` 内的三个子模块：

1. **配置与读回（`spi_master_regs`）**：接收 emesh 事务，写 `SPI_CONFIG`（设 CPOL/CPHA/LSBFIRST/SS 模式）、`SPI_CLKDIV`（设波特率）；读时把 `SPI_RX0/RX1`（收到的东西）拼包回送。
2. **发送 FIFO（`spi_master_fifo`）**：写 `SPI_TX` 时，把 104 位 emesh 包**切成字节**压进一个同步 FIFO，缓冲待发数据。
3. **IO 状态机（`spi_master_io`）**：从 FIFO 取字节，产生 SCLK，按 SS 协议逐位移出 MOSI、同时采样 MISO；传完一个完整事务后把 MISO 收到的 64 位回送给 regs。

#### 4.3.2 核心流程

一次主机发送的生命周期：

```
写 SPI_CLKDIV ──► 设波特率
写 SPI_CONFIG ──► 设模式（CPOL/CPHA/LSBFIRST/SS 模式）
写 SPI_TX(多次)──► 字节进 FIFO
                    │
   spi_master_io 状态机：
   IDLE ──fifo非空──► SETUP ──相位到──► DATA ──移完8×N位──► HOLD ──► MARGIN ──► IDLE
                          │                  │
                    期间 SCLK 翻转          MOSI 逐位移出
                                          MISO 逐位采样
                                          SS 保持低
                    │
   传完（SS 上升沿）──► rx_access 脉冲 ──► 把 64 位 MISO 结果锁进 RX 寄存器
```

SCLK 由 `oh_clockdiv`（u2-l3 讲过的可编程分频器）产生。波特率由 `SPI_CLKDIV` 寄存器控制，经 `clkdiv_reg` 喂给分频器。注意 README 给的公式 `Ratio=1<<clkdiv` 与 `oh_clockdiv` 实际的 `clkdiv+1` 分频比并不一致，**以仿真为准**（待本地验证）。

#### 4.3.3 源码精读

**三段式组装** 在 [spi/hdl/spi_master.v:62-141](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master.v#L62-L141)：`spi_master_regs`、`spi_master_fifo`、`spi_master_io` 三者用一组 `wire` 两两相连。例如 regs 输出的 `cpol/cpha/lsbfirst/spi_en/manual_mode/send_data/clkdiv_reg` 全部喂给 io；io 输出的 `spi_state/rx_data/rx_access` 回喂给 regs；fifo 的 `fifo_dout/fifo_empty/fifo_read` 与 io 对接。

**配置位拆解** 在 `spi_master_regs` 里。先解码 emesh 写（[spi/hdl/spi_master_regs.v:94-102](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_regs.v#L94-L102)），再用 `config_reg` 的位段产出各配置信号（[spi/hdl/spi_master_regs.v:108-120](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_regs.v#L108-L120)）：

```verilog
assign spi_en       = hw_en & ~config_reg[0]; // bit0: 禁用（默认开）
assign irq_en       = config_reg[1];          // bit1: 中断使能
assign cpol         = config_reg[2];          // bit2: CPOL
assign cpha         = config_reg[3];          // bit3: CPHA
assign lsbfirst     = config_reg[4];          // bit4: LSB 先发
assign manual_mode  = config_reg[5];          // bit5: 手动 SS 模式
assign send_data    = config_reg[6];          // bit6: 手动模式下的 SS 位
```

这套位定义与 README 的 `SPI_CONFIG` 表一致。注意 bit0 是「**禁用**」且低有效语义（`~config_reg[0]`），默认 0 = 开启。

**FIFO 切片** 是个精妙设计。`spi_master_fifo` 不是简单存字节——它用了一个 `oh_par2ser` 把整包 104 位**按 `datamode` 决定的字节数**切成字节，逐字节写入 `oh_fifo_sync`（[spi/hdl/spi_master_fifo.v:94-110](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_fifo.v#L94-L110)）。`datasize = 1<<datamode` 决定切几字节：

```verilog
assign datasize[7:0] = (1<<datamode_in[1:0]);   // datamode=0→1字节, 1→2, 2→4, 3→8
```

[spi/hdl/spi_master_fifo.v:72](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_fifo.v#L72)。于是写一次 `SPI_TX`（带不同 `datamode`）就能把 1~8 个字节自动压进 FIFO，随后由 IO 状态机逐字节移出。底层 FIFO 是 u3-l2 讲过的 `oh_fifo_sync`（[spi/hdl/spi_master_fifo.v:116-129](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_fifo.v#L116-L129)）。

**IO 状态机** 是主机的心脏，定义在 [spi/hdl/spi_master_io.v:84-105](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L84-L105)：

```verilog
`define SPI_IDLE    3'b000  // SS=1（不选中）
`define SPI_SETUP   3'b001  // 建立时间
`define SPI_DATA    3'b010  // 发送/接收数据
`define SPI_HOLD    3'b011  // 保持时间
`define SPI_MARGIN  3'b100  // 间隔
```

`IDLE→SETUP` 在 `fifo_read` 时触发（FIFO 非空、可读）；`SETUP→DATA` 等相位匹配；`DATA` 停留到 `data_done`（FIFO 空、当前字节移完）；`HOLD/MARGIN` 是收尾节拍。读 FIFO 的时机见 [spi/hdl/spi_master_io.v:108](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L108)：`fifo_read = ~fifo_empty & ~spi_wait & phase_match`。

**SCLK 产生** 用 `oh_clockdiv`（[spi/hdl/spi_master_io.v:60-78](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L60-L78)），`period_match`（分频时钟上升沿）与 `phase_match`（下降沿）是两条节拍线。SCLK 电平由这两条线驱动（[spi/hdl/spi_master_io.v:128-134](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L128-L134)）：`period_match` 拉高、`phase_match` 拉低，复位为 0——这正是 **CPOL=0（空闲低）**。

**移位方向** 决定了 CPHA 语义：

```verilog
assign tx_shift = phase_match & (spi_state==`SPI_DATA);  // MOSI 在下降沿切换
assign rx_shift = (spi_state==`SPI_DATA) & period_match; // MISO 在上升沿采样
```

[spi/hdl/spi_master_io.v:141](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L141) 与 [spi/hdl/spi_master_io.v:165](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L165)。「下降沿切换、上升沿采样」正是 **CPHA=0**。

MOSI 由一个 8 位 `oh_par2ser` 驱动（[spi/hdl/spi_master_io.v:143-158](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L143-L158)），MISO 由一个 64 位 `oh_ser2par` 接收（[spi/hdl/spi_master_io.v:167-175](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L167-L175)）——主机最多一次事务收 64 位（8 字节）。传完一个事务，在 SS 上升沿产生 `rx_access` 脉冲（[spi/hdl/spi_master_io.v:178-184](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L178-L184)），把结果锁进 RX 寄存器供核读取。

> ⚠️ **一个源码真相**：`spi_master_io` 的端口声明了 `cpol`、`cpha` 输入（[spi/hdl/spi_master_io.v:12-13](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L12-L13)），但**函数体里从未引用它们**——SCLK 极性与移位时机是写死的模式 0。换句话说，写 `SPI_CONFIG` 的 bit2/bit3 改变了 `cpol/cpha` 这两根线，但 IO 状态机并不据此改变行为。这与从机 `spi_slave_io.v:3` 顶部注释「only cpol=0, cpha=0 supported for now」相呼应：**整套 SPI 当前实际只工作在模式 0**。配置位是「预留接口」，真正多模式支持是 TODO。读源码时以这个事实为准，别被端口列表误导。

#### 4.3.4 代码实践（本讲主实践）

**目标**：阅读 `spi_master_io.v`，画出 **CPOL=0 / CPHA=0（模式 0）** 下连续移位 8 位（1 字节）的 SCLK、SS、MOSI、MISO 时序图。

**步骤**：
1. 重读 [spi/hdl/spi_master_io.v:128-175](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_master_io.v#L128-L175)，确认三件事：
   - SCLK 复位为 0、`period_match` 拉高、`phase_match` 拉低（→ 空闲低 = CPOL=0）。
   - `tx_shift` 在 `phase_match`（SCLK 即将下降时）有效 → MOSI 在**下降沿切换**。
   - `rx_shift` 在 `period_match`（SCLK 即将上升时）有效 → MISO 在**上升沿采样**。
2. 在纸上画时间轴（横轴 = 核时钟拍）。假设发送字节 `0xB4 = 1011_0100`，MSB 先发（`lsbfirst=0`）。
3. 画出 SS：进入 `SPI_DATA` 前拉低、`MARGIN` 后拉高。
4. 画出 SCLK：8 个脉冲，空闲为 0。
5. 在 SCLK 每个**下降沿**之后画出 MOSI 的下一位（1,0,1,1,0,1,0,0）。
6. 在 SCLK 每个**上升沿**画出 MISO 的采样点（用箭头标「采样」）。

**需要观察的现象**（ASCII 示意，节选关键段，`↓`=下降沿切换 MOSI，`↑`=上升沿采样 MISO）：

```
SS    ‾‾‾‾‾\__________________________________________________/‾‾‾‾
SCLK  ____‾‾\__/\__/\__/\__/\__/\__/\__/\__/\__________________
          ↑ ↓  ↑ ↓  ↑ ↓  ↑ ↓  ↑ ↓  ↑ ↓  ↑ ↓  ↑ ↓
MOSI      D7   D6   D5   D4   D3   D2   D1   D0
          1    0    1    1    0    1    0    0      (0xB4, MSB先)
MISO(sel)   ↑    ↑    ↑    ↑    ↑    ↑    ↑    ↑    (上升沿锁存)
```

（上图每对 `↑ ↓` 表示一个 SCLK 周期内「上升沿采样、下降沿切换」的标准模式 0 节奏；实际 SCLK 高低电平持续多个核周期，此处简化为示意。）

**预期结果**：
- SS 在整个 8 拍期间为低，前后为高。
- SCLK 空闲低，共 8 个上升沿、8 个下降沿。
- MOSI 在第 1 个下降沿前已备好 D7，之后每个下降沿切到下一位。
- MISO 在每个上升沿被 `oh_ser2par` 锁进 64 位接收移位寄存器。
- 8 拍后 SS 上升沿 → `rx_access` 脉冲 → 数据锁存。

**待本地验证**：用 `spi/dv/dut_spi.v`（主从环回）+ `spi/dv/tests/test_write.emf` 跑仿真，在 gtkwave 里对齐 `m_sclk/m_mosi/m_ss`，核对是否与你的手画图一致。注意仓库脚本路径遗留（u1-l3），可能需手动补 `libs.cmd` 的 `-y` 搜索路径。

#### 4.3.5 小练习与答案

**练习 1**：为什么是「下降沿切换 MOSI、上升沿采样 MISO」，而不是反过来？

**答案**：因为二者必须错开。若在同一沿既切换又采样，接收端锁到的是发送端正在翻转的不稳定值。模式 0 让发送端在下降沿把数据放稳，接收端在随后的上升沿（半个周期后）采样，保证锁到的是已稳定的数据。

**练习 2**：主机接收用的是 64 位 `oh_ser2par`（PW=64），但一个字节只有 8 位。多出来的位会是什么？

**答案**：一次主机事务可能连续移多个字节（FIFO 有多少就发多少），MISO 上对应收到多字节，全部拼进同一个 64 位移位寄存器（最多 8 字节）。事务结束（SS 上升沿）时整组 64 位一次性锁存进 RX，供核分 `SPI_RX0`（低 32 位）/`SPI_RX1`（高 32 位）读取。所以「64 位」对应一次事务最多 8 字节的吞吐。

---

### 4.4 SPI 从机：命令解析、寄存器堆与远程事务

#### 4.4.1 概念说明

从机的角色与主机对称：它**被动**接收主机的 SCLK/MOSI/SS，在 SS 拉低期间逐位收 MOSI、同时往 MISO 回吐数据。但 OH! 的从机比「一个移位寄存器」丰富得多——它定义了一套**串行命令协议**：

- 第 1 个字节是「**命令字 + 地址**」：高 2 位 `[7:6]` 是命令（`SPI_WR`/`SPI_RD`/`SPI_FETCH`），低 6 位是寄存器地址。
- 之后若干字节是数据。
- 命令为 `SPI_FETCH` 时，从机会向 emesh 核**发起一次远程事务**（读远端存储），把结果回送给主控——这就是 README 里的「REMOTE SPI」能力。

从机内部维护一个**可寻址寄存器堆**（config/status/若干 user 寄存器），主控通过串行线就能读写它们。

#### 4.4.2 核心流程

```
SS 下降沿 ──► 从机状态机复位到 IDLE，bit_count 清 0
IDLE ──► CMD（收 8 位命令字）
CMD  ──收满 1 字节(byte_done)──► 解析 command_reg[7:6]：
        ├─ 若是数据态，进入 DATA，按 command_reg[5:0] 寻址寄存器堆
        ├─ 每收满一字节，写信号 spi_write 选通对应 user 寄存器
        └─ 地址自增（command_reg[5:0]+1），支持连续写
DATA ──► 持续到 SS 拉高
SS 上升沿 ──► 若命令是 SPI_FETCH，产生单拍脉冲 spi_fetch
            ──► access_out 拉高，向核发起 emesh 读
            ──► 核返回数据写入 core_regs，status 置位「data ready」
```

注意从机状态机工作在 **SPI 时钟域（`sclk`）**，而与核的交互在**核时钟域（`clk`）**——所以中间有 `oh_dsync`（同步 SS）和 `oh_rise2pulse`（边沿转脉冲）做跨域，正是 u2-l4 讲过的 CDC 范式。

#### 4.4.3 源码精读

**模式写死** 顶部注释就说清楚：[spi/hdl/spi_slave_io.v:3](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L3)「NOTE: only cpol=0, cpha=0 supported for now!!」。对应的 assign 在 [spi/hdl/spi_slave_io.v:63-68](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L63-L68)：

```verilog
assign shift   = ~ss & spi_en;   // 选中即移位
assign rx_clk  = sclk;           // MOSI 在 sclk 上升沿采样
assign tx_clk  = ~sclk;          // MISO 在 sclk 下降沿切换
```

`rx_clk=sclk`、`tx_clk=~sclk`——接收用上升沿、发送用下降沿，与主机的模式 0 严格对得上（主机的 `period_match` 对应此处 `sclk` 上升）。`cpol`/`cpha` 同样是声明了却未用的输入（[spi/hdl/spi_slave_io.v:20-21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L20-L21)）。

**状态机与位计数** 在 [spi/hdl/spi_slave_io.v:74-100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L74-L100)。状态机用 `posedge sclk or posedge ss`——注意这里的「异步复位」是 `ss`（SS 拉高即回到 IDLE），是 SPI 协议的典型写法。`bit_count` 每个 `sclk` 加 1，用 `bit_count[2:0]==3'b000` 判「新字节开始」、`==3'b111` 判「一字节收满」（`byte_done`）。

**命令字与地址自增** 在 [spi/hdl/spi_slave_io.v:104-111](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L104-L111)：CMD 态收满一字节时把它装入 `command_reg`；DATA 态每收满一字节，地址低 6 位 `+1`（连续寻址）。最终地址 `spi_addr = command_reg[5:0]`（[spi/hdl/spi_slave_io.v:153](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L153)）。

**写选通** 把命令、地址、字节完成信号组合起来（[spi/hdl/spi_slave_io.v:155-159](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L155-L159)）：

```verilog
assign spi_write = spi_en & byte_done & ~ss
                 & (command_reg[7:6]==`SPI_WR)
                 & (spi_state==`SPI_DATA_STATE);
```

只有「写命令 + 数据态 + 一字节收满 + SS 选中」才产生一次寄存器写。

**接收/发送移位** 仍由 `oh_ser2par`（收 MOSI，[spi/hdl/spi_slave_io.v:117-125](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L117-L125)）与 `oh_par2ser`（发 MISO，[spi/hdl/spi_slave_io.v:132-145](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L132-L145)）完成，与主机对称。发送数据来自寄存器堆回读的 `spi_rdata`。

**寄存器堆** 在 `spi_slave_regs`。config 位定义（[spi/hdl/spi_slave_regs.v:104-109](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L104-L109)）与主机几乎一样（bit0 禁用、bit2 CPOL、bit3 CPHA、bit4 LSBFIRST），但多了 bit5=`valid`（user 寄存器使能）。user 寄存器写入（[spi/hdl/spi_slave_regs.v:115-117](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L115-L117)）按 `spi_addr[4:0]` 选址。

最有趣的是 [spi/hdl/spi_slave_regs.v:143-161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L143-L161)：把所有寄存器**拼成一个 512 位大向量** `spi_regs`（config 在 7:0、status 在 15:8、core 返回数据在 191:128、user 在 256+），读回时直接用 `spi_regs[8*spi_addr +: 8]` 按地址取字节（[spi/hdl/spi_slave_regs.v:161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L161)）。这是一种「扁平化」的寄存器堆实现，省去了逐个写 `case`。

**远程事务（FETCH）** 在 [spi/hdl/spi_slave_io.v:184-205](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L184-L205)：SS 上升沿经 `oh_dsync` 同步到核时钟域、再经 `oh_rise2pulse` 转成单拍脉冲；若命令是 `SPI_FETCH`，则产生 `spi_fetch`，进而拉高 `access_out` 向核请求一次 emesh 读。核的返回数据在 `spi_slave_regs` 里被锁进 `core_regs`（[spi/hdl/spi_slave_regs.v:134-136](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L134-L136)）并把 status 置位「data ready」（[spi/hdl/spi_slave_regs.v:123-127](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_regs.v#L123-L127)）。

> ⚠️ **编译期遗留**：`spi_master_regs.v`、`spi_master_fifo.v`、`spi_slave_regs.v` 都实例化了 `packet2emesh`/`emesh2packet`（用于 104 位包与字段互转），但这两个模块**在当前仓库里不存在**（现行 emesh 包互转模块叫 `emesh_pack`/`emesh_unpack`，见 u5-l3）。这是与 GPIO 讲义（u6-l2）里 `enoc_pack/enoc_pack` 改名漂移同源的迁移遗留——SPI 模块当前**不能原样编译**，阅读与讲解一律以源码文本为准，仿真需先补齐这些依赖或用 `DV_SPI_BYPASS`（见 [spi/hdl/spi_slave_io.v:168-171](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L168-L171)）绕开慢速 SPI。

#### 4.4.4 代码实践

**目标**：对照 `spi/dv/tests/test_basic.emf` 与 `spi/dv/dut_spi.v`，追一次「主机写 → 从机 user 寄存器」的完整事务流。

**步骤**：
1. 打开 [spi/dv/tests/test_basic.emf](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/dv/tests/test_basic.emf)。前两行是配置（设 CLKDIV、设 LSBFIRST）；后面一串 `dstaddr=00000008`（=`SPI_TX`）的写是把命令字与数据字节压进主机 FIFO。
2. 打开 [spi/dv/dut_spi.v:73-137](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/dv/dut_spi.v#L73-L137)：注意它实例化了**两个** `spi`——`master` 的 `m_sclk/m_mosi/m_ss` 直接连到 `slave` 的 `s_sclk/s_mosi/s_ss`，形成片内环回；从机的核侧又挂了一个 `ememory` 当远端存储。
3. 追链路：emesh 写 `SPI_TX` → `spi_master_fifo` 切字节入 FIFO → `spi_master_io` 逐位移出 `m_mosi` → 经环回进入从机 `s_mosi` → 从机 `oh_ser2par` 收字节 → CMD 态解析命令、DATA 态按 `spi_write` 写入 `user_regs`。

**需要观察**：主机发出去的「命令字（`SPI_WR` + 地址）」会被从机的 `command_reg` 捕获（[spi/hdl/spi_slave_io.v:104-111](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/hdl/spi_slave_io.v#L104-L111)），随后的数据字节按自增地址写入 user 寄存器。

**预期结果**：在波形里应能看到 `slave.user_regs[...]` 被逐字节写入主机发送的值；`m_ss` 每拉低一次对应一次 SPI 事务。

**待本地验证**：由于前述 `packet2emesh` 缺失，本测试**当前无法直接编译运行**。实践可退化为「源码阅读型」：在纸上把 4.4.2 的流程图与 `dut_spi.v` 的连线对应起来，确认环回路径无误。

#### 4.4.5 小练习与答案

**练习 1**：从机状态机为什么用 `posedge ss` 作为异步复位，而不是核时钟？

**答案**：因为从机工作在 SPI 时钟域（`sclk`），SS 是协议级「传输边界」信号。SS 拉高表示一次传输结束，必须**立刻**回到 IDLE 并清位计数器，不能等核时钟。用 `posedge ss` 异步复位状态机和 `bit_count`，保证无论 `sclk` 状态如何，下次 SS 拉低都从干净初态开始。

**练习 2**：`spi_fetch`（远程取数）为什么要先 `oh_dsync` 同步 SS 再 `oh_rise2pulse`？能不能直接用 SS 上升沿？

**答案**：不能。SS 是 SPI 时钟域的信号，直接送给核时钟域的 `access_out` 会引发亚稳态（u2-l4）。必须先用 `oh_dsync` 把 SS 同步到核时钟域，再用 `oh_rise2pulse` 把「SS 上升」转成核时钟域的单拍脉冲，才能安全地驱动一次 emesh 事务。这是「先同步、再取沿」标准范式。

---

## 5. 综合实践

**任务**：在 `dut_spi.v` 的主从环回拓扑下，设计一组 `.emf` 激励，让主机通过 SPI 把 4 个字节（如 `11 22 33 44`）写入从机的 user 寄存器（起始地址自定），再发一次读命令把它们读回，最后主机读 `SPI_RX0` 验证收到的内容。

**要求**：

1. 对照 [spi/README.md](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/spi/README.md) 的「Examples」与 `spi/dv/tests/test_basic.emf` 的格式，写出每一行 `.emf`（五段：`srcaddr_datahi _ datalo _ dstaddr _ ctrlmode _ access`）。
2. 第一行先写 `SPI_CLKDIV` 设波特率；第二行写 `SPI_CONFIG` 确认模式 0（CPOL=0/CPHA=0，即 config=0）。
3. 发送「命令字」字节：高 2 位 = `SPI_WR`（`2'b00`），低 6 位 = 起始 user 地址。
4. 连续写 4 个数据字节到 `SPI_TX`（注意 `datamode` 决定每次压几字节）。
5. 再发一个「读命令」（`SPI_RD`，`2'b10`）+ dummy 字节以产生 SCLK、回读数据。
6. 最后发一个 emesh 读 `SPI_RX0`（dstaddr=`0x10`）把主机收到的内容读回核对。

**验证**：

- 在 `dut_spi.v` 的环回里，主机 MOSI → 从机 MOSI，从机把命令解析后在 DATA 态写 `user_regs`。
- 反向：从机 MISO → 主机 MISO，读命令期间从机把 `user_regs` 内容回吐，主机经 `oh_ser2par` 收进 64 位 RX 寄存器。
- 读 `SPI_RX0` 应看到之前写入的字节。

**说明**：本任务结合了本讲全部三个最小模块——主机的 FIFO 与状态机（4.3）、从机的命令解析与寄存器堆（4.4）、二者共用的移位寄存器（4.2）。由于仓库 `packet2emesh` 缺失，完整跑通需先补齐依赖；若环境不允许，至少完成纸面激励设计与链路追踪。

## 6. 本讲小结

- `spi` 顶层用「主机 + 从机 + `emesh_mux`」三件式，让同一 IP 既可当主控也可当从机，二者共享 emesh 接口、分离物理 IO，反压取两边之或。
- `spi_regmap.vh` 用跳号的大写宏给主从共用的寄存器分配地址，还定义了串行命令字（`SPI_WR`/`SPI_RD`/`SPI_FETCH`）。
- 移位是一切的基础：`oh_par2ser`（并→串）与 `oh_ser2par`（串→并）是主从共用的核心积木，靠 `lsbfirst` 控制方向、靠 `datasize`/位数控制移位次数。
- 主机发送通路 = 配置（`spi_master_regs`）+ 字节切片 FIFO（`spi_master_fifo` 用 `oh_par2ser` 把整包切字节入 `oh_fifo_sync`）+ IO 状态机（`spi_master_io` 产生 SCLK、管 SS、驱动 MOSI/采样 MISO）。
- 模式 0 的时序由两条节拍线写死：`phase_match`（下降沿）切换 MOSI、`period_match`（上升沿）采样 MISO；`cpol`/`cpha` 虽是端口但未被引用，整套 SPI 实际只支持模式 0。
- 从机用串行命令协议驱动一个扁平化的 512 位寄存器堆，地址自增支持连续读写；`SPI_FETCH` 命令经 `oh_dsync`+`oh_rise2pulse` 跨域后向核发起远程 emesh 读。
- 阅读警示：`packet2emesh`/`emesh2packet` 在当前仓库缺失，SPI 模块无法原样编译；仿真可借 `DV_SPI_BYPASS` 绕过慢速 SPI，一切以源码文本为准。

## 7. 下一步学习建议

- **横向巩固「外设 = regmap + emesh 接口」模式**：继续读 u6-l4（emailbox/emmu/etrace），对比它们与 SPI 在「寄存器映射 + 包接口」上的同构性。
- **深入串行链路的物理层**：本讲的 SPI 是「字节级移位」，第 7 单元的 elink 则是「包级、DDR、源同步 LVDS 链路」。建议接着读 u7-l1（elink 总体架构）与 u7-l2（etx 发送流水线），看 OH! 如何把同样的「并→串→解串」思想放大到高速链路。
- **补齐编译依赖**：若想真正仿真 SPI，需解决 `packet2emesh`/`emesh2packet` 缺失问题——可参照 u5-l3 的 `emesh_pack`/`emesh_unpack` 写一对适配壳，或在测试平台用 `DV_SPI_BYPASS` 直接驱动 `packet_out`/`access_out`。
- **扩展练习**：尝试修改 `spi_master_io`，让它**真正**读取 `cpha` 信号、支持模式 1（在第二个沿采样），并写出对应的激励验证——这是把「预留接口」变成「真实功能」的二次开发练手。
