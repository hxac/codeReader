# SPI 接收 spi_rx

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `spi_rx` 的端口契约与四个 generic（`SPI_CLK_POLARITY` / `SPI_CLK_PHASE` / `DATA_WIDTH` / `MSB_FIRST_AND_NOT_LSB`）各自的作用。
- 解释「为什么接收方要在 `rx_active_edge` 这个特定边沿采样」——它与发送方改变数据的边沿错开半个时钟周期。
- 读懂单个时钟进程如何用 `bit_index` 变量逐位填入 `rx_data`，并在收满一字时拉高 `rx_data_valid`。
- 说明 `rst_n` 与 `spi_chip_select_n` 共同控制 `bit_index` 复位的机制，以及为何中途撤销片选会导致「重新对齐」。
- 读懂测试台 `tb_spi_rx` 如何用 `transmit_and_check_data` 过程构造串行激励并逐位校验。

## 2. 前置知识

本讲是 SPI 子系统的第三讲，承接两份前置认知：

- **u10-l1（SPI 模式与 `spi_pkg`）**：你已经知道 SPI 是主从全双工串行协议；`CPOL`（时钟极性）与 `CPHA`（时钟相位）组合出四种模式；`spi_pkg` 是一个 VHDL-2008 generic package，提供 `rx_active_edge`、`last_bit_index`、`reset_bit_index`、`update_bit_index` 等函数/过程。本讲会直接使用这些工具。
- **u10-l2（SPI 发送 `spi_tx`）**：你已经知道主机在 `tx_active_edge` 上**改变**串行输出数据。本讲的 `spi_rx` 正好在与之错开的另一个边沿上**采样**——这就是 SPI 收发双方的时序契约。

本讲只讲**接收方向**：从机如何把 `serial_data_in` 上逐比特出现的数据，重新拼装成一个完整的并行字 `rx_data`，并用 `rx_data_valid` 告诉上层「一个字收齐了」。

> 术语提示：SPI 总线上接收方向的数据线常被称为 **MISO**（Master In Slave Out，主入从出）。对从机而言，它在 MISO 上发送、在 **MOSI** 上接收。本模块的 `serial_data_in` 就是接在 MOSI 上的接收输入。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| [ip/communication/spi/spi_rx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd) | 设计源码（可综合） | 精读接收进程 |
| [ip/communication/spi/spi_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd) | generic package（u10-l1 已讲） | 引用 `rx_active_edge` 等工具 |
| [ip/communication/spi/tb/tb_spi_rx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd) | 测试台（仅仿真） | 实践依据 |
| [ip/communication/spi/tb/tb_spi_rx.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.do) | ModelSim 波形脚本 | 观察内部 `bit_index` |

`spi_rx` 与 `spi_tx` 一样只有一套 `behavioural` 架构，厂商无关、可开箱仿真（参考 u10-l1/u10-l2 的同实体多架构讨论）。

## 4. 核心概念与源码讲解

### 4.1 spi_rx：模块结构与端口契约

#### 4.1.1 概念说明

`spi_rx` 的职责只有一件：**串并转换**。它在 SPI 时钟的驱动下，把 `serial_data_in` 上一个一个出现的比特，按约定的位序（MSB first 或 LSB first）填进 `rx_data` 的对应位置；当最后一个比特到位时，拉高 `rx_data_valid` 一个 SPI 时钟周期，告诉上层「这 `DATA_WIDTH` 个比特已经拼好，可以取走了」。

它不产生时钟、不管片选的边沿对齐（那是 `spi_tx` 的事）、也不做跨时钟域同步——它假设 `spi_clk`、`serial_data_in`、`spi_chip_select_n` 已经和它处在同一个时钟域里（或已由上层同步好）。

#### 4.1.2 核心流程

```text
每个 SPI 时钟周期（采样边沿上）：
  1. rx_data_valid <= '0'        （默认先清零，单拍脉冲语义）
  2. rx_data[bit_index] <= 串行输入   （把当前比特存进对应位置）
  3. 判断本拍是否「不该接收」：
       若 rst_n=0（复位） 或 spi_chip_select_n=1（未被选中）
         → bit_index 复位到起点
  4. 否则（被选中且未复位）：
       若 bit_index 已是最后一个比特：
         → rx_data_valid <= '1'   （收满一字）
       bit_index 推进到下一个比特（末尾则折回起点）
```

关键点：步骤 2 和步骤 4 的「拉高 valid」发生在**同一个**采样边沿上——也就是说，当最后一比特被存入 `rx_data` 的**同时**，`rx_data_valid` 被拉高。所以上层可以在 `rx_data_valid='1'` 的那一刻，直接读到完整的 `rx_data`。

#### 4.1.3 源码精读

先看 entity 的端口与 generic：

[ip/communication/spi/spi_rx.vhd:L14-L31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L14-L31) — 声明 4 个 generic 与 6 个端口。

- `SPI_CLK_POLARITY` / `SPI_CLK_PHASE`（`bit`）：决定在哪个边沿采样（详见 4.2）。
- `DATA_WIDTH`（`natural := 8`）：一个字的位宽。
- `MSB_FIRST_AND_NOT_LSB`（`boolean := true`）：位序，`true` 表示先收高位。
- 端口里 `serial_data_in` 是 `std_logic`（注意不是 `std_ulogic`），这通常是为了和三态总线/外部接口的驱动类型对齐；其余端口用 `std_ulogic`。

再看架构如何就地例化一份专属的 `spi_pkg`：

[ip/communication/spi/spi_rx.vhd:L33-L40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L33-L40) — 在 architecture 的说明区用 `package spi_pkg_constrained is new work.spi_pkg generic map(...)` 例化出一份与 entity generic 同步的包，随后 `use spi_pkg_constrained.all;` 把里面的函数/过程引入作用域。

> 这是 VHDL-2008 generic package 的标准用法（u10-l1 已讲）：`spi_pkg` 本身是带参的模板，每个使用者各自 `new` 出一份按自己 `DATA_WIDTH` / 位序约束好的实例，互不干扰。`bit_index` 变量的取值范围就来自这份包里的 `data_range_t`（见 4.3）。

整个接收逻辑只有一个进程：

[ip/communication/spi/spi_rx.vhd:L41-L58](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L41-L58) — `receiver` 进程，敏感于 `spi_clk`，内部用一个变量 `bit_index` 做位计数。

这段是本讲的核心，4.2 和 4.3 会逐句拆开讲。这里先建立一个整体印象：进程**只在采样边沿**（`rx_active_edge` 返回真）才真正动作，其余边沿虽然唤醒进程，却被 `if` 挡在外面什么都不做。

#### 4.1.4 代码实践

**实践目标**：在不仿真之前，先用「纸面追踪」确认你对端口和 generic 的理解。

**操作步骤**：

1. 打开 [spi_rx.vhd:L14-L31](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L14-L31)。
2. 回答：若要让本模块按「LSB first、模式 1（CPOL=0, CPHA=1）、16 位字」工作，例化时 4 个 generic 分别应填什么？
3. 对照 [tb_spi_rx.vhd:L50-L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L50-L53)，看测试台实际用的是哪一组配置。

**预期结果**：

| generic | 你的答案 | 测试台取值 |
|---------|----------|------------|
| `SPI_CLK_POLARITY` | `'0'` | `'0'` |
| `SPI_CLK_PHASE` | `'1'` | `'0'`（测试台只跑模式 0） |
| `DATA_WIDTH` | `16` | `8` |
| `MSB_FIRST_AND_NOT_LSB` | `false` | `true` |

**需要观察的现象**：你会发现测试台**只配置了模式 0**。这意味着 `rx_active_edge` 在 CPOL=1（模式 2/3）下的行为从未被仿真覆盖——这一点 u10-l1 已经标注为待补测试之处，本讲不再展开，但请记住它。

#### 4.1.5 小练习与答案

**练习 1**：`serial_data_in` 为什么用 `std_logic` 而不是 `std_ulogic`？

**参考答案**：`std_logic` 比 `std_ulogic` 多了 `'Z'`（高阻）等驱动状态，常用于需要挂到三态总线或与外部/顶层双向接口对接的场景。这里用 `std_logic` 是为了让接收输入的类型与总线约定保持一致；模块内部不会主动驱动它，只读取。

**练习 2**：本模块对 `rst_n` 是同步复位还是异步复位？

**参考答案**：是**同步**的。进程的敏感信号表只有 `spi_clk`（[L41](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L41)），`rst_n` 只在进程内部、且只在采样边沿到来时才被检查（[L48](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L48)）。所以 `rst_n` 拉低后，要等到下一个采样边沿才生效。

---

### 4.2 rx_active_edge：采样边沿的时序依据

#### 4.2.1 概念说明

SPI 收发能可靠工作的根本，在于**收发双方使用错开的边沿**：一方在某个边沿**改变**数据并让它稳定，另一方在**另一个**边沿上去**采样**那个已经稳定的值。两者错开半个时钟周期，这正是 SPI「时钟同步、无需握手」的简洁性来源。

对**模式 0（CPOL=0, CPHA=0）**——也是本模块测试台唯一覆盖的模式：

- 时钟空闲为低（CPOL=0）。
- CPHA=0 表示数据在**第一个**（前导）边沿被采样、在**第二个**（后导）边沿被改变。
- CPOL=0 时前导边沿是**上升沿**，所以接收方在**上升沿采样**。

`rx_active_edge` 这个函数，就是把「CPOL × CPHA → 哪个边沿」这张模式表，翻译成 `rising_edge` / `falling_edge` 调用。

#### 4.2.2 核心流程

模式 0 的一个 SPI 时钟周期内，时间轴大致如下：

```text
spi_clk:      __|‾‾‾‾|____|‾‾‾‾|____|‾‾‾‾|__   （空闲低，前导=上升沿）
                  ↑         ↑         ↑
              rx 采样     rx 采样   rx 采样
              (rising)   (rising)  (rising)

serial_data:  --< D7 ><      D6      >< D5 >--   （发送方在后导边沿换数）
```

`rx_active_edge` 在模式 0 下恒返回 `rising_edge(clk)`，于是 `spi_rx` 进程在每个上升沿把当前 `serial_data_in` 存进去。

> 数学上，相邻两个采样边沿之间的时间间隔为 SPI 时钟周期 \(T_{spi}\)，而采样点距发送方换数点恰好错开 \(T_{spi}/2\)，保证被采样的数据已稳定半个周期，满足建立/保持裕量。

#### 4.2.3 源码精读

`rx_active_edge` 的完整定义：

[ip/communication/spi/spi_pkg.vhd:L62-L75](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L62-L75) — 按 `clk_polarity & clk_phase` 拼出的两位模式码做 `case`，返回对应的边沿检测布尔值。

把这张表和上一讲的发送边沿对照：

| 模式 | CPOL | CPHA | `tx_active_edge`（改变数据） | `rx_active_edge`（采样） |
|------|------|------|------------------------------|--------------------------|
| 0 | 0 | 0 | `falling_edge` | `rising_edge` |
| 1 | 0 | 1 | `rising_edge` | `falling_edge` |
| 2 | 1 | 0 | `true`（一直有效） | `rising_edge`（见下方注意） |
| 3 | 1 | 1 | `falling_edge` | `falling_edge`（见下方注意） |

可以看到模式 0 和模式 1，收发边沿确实是错开的（一个上升一个下降）。

> **关于模式 2/3 的注意（承接 u10-l1）**：按维基百科标准模式表，CPOL=1 时前导边沿应是**下降沿**，因此模式 2 的接收采样边沿按标准应是 `falling_edge`，而本函数在 `"10"` 分支返回的是 `rising_edge`——与标准相反；模式 3 同理存在偏差。由于测试台只配置模式 0（见 4.1.4），模式 2/3 从未被仿真验证。本讲以**已被测试覆盖的模式 0**为准讲解；模式 2/3 的对错留待你日后补测试时核实。

`spi_rx` 调用它的那一行：

[ip/communication/spi/spi_rx.vhd:L44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L44) — `if rx_active_edge(spi_clk, SPI_CLK_POLARITY, SPI_CLK_PHASE) then`，整个接收动作都被这个条件门控。

注意 `spi_pkg.vhd` 里 `rx_active_edge` 的形参 `clk_in` 声明为 `signal`（[L62](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L62)），只有这样 `rising_edge` / `falling_edge` 才能检测到信号跳变——这是 u10-l1 强调过的细节。

> **综合视角的补充（承接 u10-l2）**：u10-l2 提到 `spi_tx` 因为 Xilinx 综合器无法从这类通用边沿函数推断触发器，而改用 `case generate` 显式写出每个模式的 `rising_edge`/`falling_edge`。`spi_rx` 这里**直接**用了通用函数 `rx_active_edge`。在测试台覆盖的模式 0（恒为 `rising_edge`）下，综合器看到的是一个稳定的上升沿条件，推断不成问题；但若要让所有四种模式都可综合，同样会遇到 `spi_tx` 那样的边沿识别难题——这是把模式差异压进 generic 的固有代价。

#### 4.2.4 代码实践

**实践目标**：在波形上确认「采样发生在上升沿、且与发送换数错开半拍」。

**操作步骤**：

1. 运行全量仿真（它会把 `tb_spi_rx` 一起编译并跑掉，详见 4.3.4 的运行方式）。
2. 用 ModelSim/QuestaSim 打开 `tb_spi_rx` 的波形，加载 [tb_spi_rx.do](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.do)。
3. 把光标对齐到 `test_single_byte_reception` 期间，放大 `spi_clk`、`serial_data_in`、`rx_data`、`rx_data_valid` 四条信号。

**需要观察的现象**：

- `serial_data_in` 的值在 **下降沿** 附近被测试台更新（见测试台 `wait_spi_clk_cycles` 等待的是 `rx_active_edge`，即上升沿，更新发生在其后）。
- `rx_data` 的对应比特在 **上升沿** 后更新。

**预期结果**：采样点（`rx_data` 变化）与激励更新点（`serial_data_in` 变化）之间相隔约半个 SPI 时钟周期。

> 若你本地无 ModelSim 许可证，可跳过波形观察，改用 4.2.5 的纸面练习理解时序。具体 GUI 加载步骤标注为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：模式 0 下，`spi_rx` 进程对 `spi_clk` 的下降沿会做什么？

**参考答案**：进程虽然对下降沿也敏感（敏感表是 `spi_clk`，两个边沿都会唤醒进程），但 [L44](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L44) 的 `rx_active_edge` 在模式 0 下只在上升沿返回真，下降沿返回假，于是进程体被 `if` 挡住，什么都不做。

**练习 2**：为什么接收采样边沿必须和发送换数边沿不同？

**参考答案**：若在同一边沿既换数又采样，会撞进建立/保持时间窗口，采到正在翻转的不稳定值。错开半拍让被采数据有整整半个周期稳定，保证采样可靠。

---

### 4.3 bit_index 位计数与 rx_data_valid 生成

#### 4.3.1 概念说明

知道了「在哪采样」，还要知道「采到的这一比特放进 `rx_data` 的哪一位」。这就是 `bit_index` 的职责。它是一个**变量**（variable），取值范围由 `spi_pkg` 里的 `data_range_t`（`0 to DATA_WIDTH-1`）决定，并通过 `rx_data'subtype'high` 反推上界，从而自动跟随 `DATA_WIDTH`。

位序由 `MSB_FIRST_AND_NOT_LSB` 控制：

- MSB first（默认）：`bit_index` 从最高位（`DATA_WIDTH-1`）开始，每拍**减 1**，收齐时落在最低位 `0`。
- LSB first：从 `0` 开始，每拍**加 1**，收齐时落在最高位。

`reset_bit_index` / `update_bit_index` / `last_bit_index` 三个过程/函数把「加减方向」「起点」「终点」全部封装起来，使 `spi_rx` 进程体里不出现任何写死的数字或方向判断——这正是 generic package 的价值。

#### 4.3.2 核心流程

```text
进入采样边沿：
  rx_data_valid <= '0'                      ；先默认清零（信号赋值，延后生效）
  rx_data(bit_index) <= serial_data_in      ；把当前比特塞进当前位置

  if (rst_n = '0') 或 (spi_chip_select_n = '1')：   ；不该接收
      bit_index := 起点                       ；复位（变量赋值，立即生效）
  elsif (spi_chip_select_n = '0')：           ；正常接收
      if last_bit_index(bit_index)：          ；已是最后一比特
          rx_data_valid <= '1'                ；覆盖前面的 '0'，拉高（最后赋值胜出）
      bit_index := update(bit_index)          ；推进到下一比特（末尾则折回起点）
```

三条要点：

1. **`rx_data_valid` 是单拍脉冲**：每拍开头先赋 `'0'`，只有收齐那一拍在末尾被覆盖成 `'1'`，下一拍又回到 `'0'`。所以每个字恰好产生一个采样周期宽的有效脉冲。
2. **「最后赋值胜出」语义**：[L45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L45) 和 [L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L53) 在同一拍都对 `rx_data_valid` 赋值，VHDL 规定同一进程内对同一信号的多次赋值，**最后一次**生效。这正是最后一比特能把 valid 拉高的原理。
3. **顺序至关重要**：源码 [L51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L51) 有一行注释 `-- NOTE: Order of operations is important here`。因为 `bit_index` 是**变量**（立即更新），必须先用它完成 `rx_data` 写入和 `last_bit_index` 判定，**最后**才 `update_bit_index`；若顺序反过来，`bit_index` 被提前推进/折回，写入位置和「是否最后一比特」的判断就全错了。

#### 4.3.3 源码精读

变量声明与进程主体：

[ip/communication/spi/spi_rx.vhd:L41-L58](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L41-L58) — 注意 [L42](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L42) 用 `rx_data'subtype'high` 反推 `bit_index` 的上界，使范围随 `DATA_WIDTH` 自动伸缩。

复位/撤销片选的分支：

[ip/communication/spi/spi_rx.vhd:L48-L49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L48-L49) — `if rst_n = '0' or spi_chip_select_n = '1' then reset_bit_index(bit_index);`。这一行是本讲的实践重点：**只要片选被撤销（拉高）或复位有效，`bit_index` 立刻回到起点**。这意味着传输中途主机若临时撤销片选，正在进行的字节会被作废，下一次重新选中时从头开始接收——这正是「重新对齐」的来源。

收满一字拉高 valid 的分支：

[ip/communication/spi/spi_rx.vhd:L50-L55](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L50-L55) — 正常接收时，先判断 `last_bit_index` 决定是否拉高 `rx_data_valid`，再 `update_bit_index` 推进。注意两处对 `bit_index` 的使用都发生在 `update` 之前。

支撑这三个调用的包内实现：

- [spi_pkg.vhd:L19](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L19) — `subtype data_range_t is natural range 0 to DATA_WIDTH - 1;`，定义 `bit_index` 的合法范围。
- [spi_pkg.vhd:L77-L83](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L77-L83) — `last_bit_index`：MSB first 时终点是 `'subtype'low`（即 0），否则是 `'subtype'high`。
- [spi_pkg.vhd:L85-L87](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L85-L87) — `reset_bit_index`：把 `bit_index` 设到起点（MSB first 为 high，否则 low）。
- [spi_pkg.vhd:L89-L96](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L89-L96) — `update_bit_index`：到末尾则折回起点，否则 MSB first 减 1、LSB first 加 1。

可以看到，所有方向与边界判断都被收进了包里，`spi_rx` 进程体因此极其精简。

#### 4.3.4 代码实践

**实践目标**：用 `tb_spi_rx` 验证两件事——(a) 收齐 `DATA_WIDTH` 位后 `rx_data_valid` 恰好拉高、`rx_data` 等于发送值；(b) 中途拉高 `chip_select_n` 会导致 `bit_index` 复位、重新对齐。

**操作步骤**：

1. 准备环境（承接 u1-l3）：创建 Python venv、`pip install -r ip/requirements.txt`，并执行 `git submodule update --init` 拉取 `ip/vhdl_utils` 子模块（否则编译失败）。
2. 运行仿真。本仓库的 `ip/test_runner.py` 是 VUnit 的薄包装，但它把参数**硬编码**为跑全部 `tb_*.vhd`：

```bash
python ip/test_runner.py
```

   这会编译并执行所有测试台，`tb_spi_rx` 的 5 个用例会出现在输出里。若只想单独排查 `tb_spi_rx`，可临时把 [test_runner.py:L22](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/test_runner.py#L22) 的 `tb_pattern` 收窄（例如指向 `**/tb_spi_rx*`），或直接用支持过滤的 VUnit 调用方式——具体语法标注为「待本地验证」。

3. 阅读测试台里与本实践直接相关的两段：

   - [tb_spi_rx.vhd:L115-L132](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L115-L132) — `transmit_and_check_data` 过程：拉低片选、按 `data_range_t` 逐位送出 `data(current_bit_index)`，并在最后一位用 `check_equal` 同时校验 `rx_data_valid='1'` 与 `rx_data=data`。这就是验证目标 (a) 的代码。
   - [tb_spi_rx.vhd:L184-L209](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L184-L209) — `test_interrupted_reception` 过程：先只送出高位半字，然后**中途拉高 `spi_chip_select_n`**（[L199](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L199)），确认此刻 `rx_data_valid` 保持 `'0'`；随后再调用 `transmit_and_check_data` 完整重发，验证能重新对齐并正确接收。这就是验证目标 (b) 的代码。

**需要观察的现象**：

- 在 `test_single_byte_reception`（[L154-L165](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L154-L165)）里，第 8 比特送出后，`rx_data_valid` 出现一个采样周期的高电平，且 `rx_data` 等于随机生成的 `expected_data`。
- 在 `test_interrupted_reception` 里，片选拉高后，DUT 内部 `bit_index` 回到起点（可在波形里用 [tb_spi_rx.do:L12](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.do#L12) 探查的 `/tb_spi_rx/DUT/receiver/bit_index` 信号确认）。

**预期结果**：全部 5 个用例通过（VUnit 输出 `Passed`），无 `check_equal` 失败。

> 说明：本测试台用 OSVVM 的 `random.RandSlv` 生成随机激励（[L157](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L157)、[L173](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L173)），并用 `random_test`（[L211-L252](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L211-L252)）做 1000 轮带随机片选抖动的压力测试。其中 `RESET_WEIGHT` 常量（[L226](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L226)）来自 `utils_pkg` 子模块，本仓库未检入其定义（待确认，参考 u3-l2）。

#### 4.3.5 小练习与答案

**练习 1**：若把 [L55](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L55) 的 `update_bit_index(bit_index);` 移到 `if last_bit_index(...) then rx_data_valid <= '1'; end if;` **之前**，会发生什么？

**参考答案**：`bit_index` 是变量，提前 `update` 会让它先推进/折回。于是 [L46](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L46) 的 `rx_data(bit_index)` 写入的是**错误位置**，`last_bit_index` 判定的也是推进后的值——结果数据被存错位、valid 提前或滞后拉高。这正是 [L51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L51) 那行「顺序很重要」注释所警告的。

**练习 2**：为什么 `rx_data_valid` 每个字只高一个采样周期，而不是持续保持？

**参考答案**：因为 [L45](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L45) 在每个采样边沿开头无条件赋 `'0'`，只有收齐那一拍在 [L53](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L53) 被覆盖成 `'1'`。下一拍开头又回到 `'0'`。这种「脉冲式 valid」便于上层用边沿/单拍握手取数，避免重复消费。

**练习 3**：传输到第 5 个比特时主机突然把 `spi_chip_select_n` 拉高，下一拍会发生什么？

**参考答案**：[L48-L49](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_rx.vhd#L48-L49) 命中，`bit_index` 被复位到起点，`rx_data_valid` 保持 `'0'`。这前 5 个比特作废；等片选再次拉低，从起点重新接收一个完整字。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个端到端的小任务（基于 `tb_spi_rx`，不改动设计源码）：

**任务**：手动追踪一次「正常接收 → 中途打断 → 重新对齐再接收」的全过程，并用测试台验证。

**步骤**：

1. **选定一个字节**，例如 `0xA5`（即 `1010_0101`，MSB first）。
2. **画出时序**：在纸上画出模式 0 下的 `spi_clk`（空闲低）、`spi_chip_select_n`、`serial_data_in`、内部 `bit_index`、`rx_data`、`rx_data_valid` 六条线，送出 `0xA5` 的高 4 位（`1,0,1,0`）。标注每个上升沿 `bit_index` 的取值（应从 7 递减到 4）。
3. **在第 5 个比特前打断**：把 `spi_chip_select_n` 拉高一拍。在图上标出此刻 `bit_index` 跳回 7（起点），`rx_data_valid` 保持 0。
4. **重新对齐**：片选再次拉低，完整送出 `0xA5` 全 8 位。标出第 8 个上升沿 `bit_index=0`、`rx_data_valid` 出现一个高脉冲、`rx_data=0xA5`。
5. **用仿真复核**：运行 `python ip/test_runner.py`，确认 `tb_spi_rx` 的 `test_interrupted_reception`（[L184-L209](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L184-L209)）与 `test_single_byte_reception`（[L154-L165](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_rx.vhd#L154-L165)）均通过。

**验收标准**：

- 手画图上，打断点处 `bit_index` 确实回到起点，`rx_data_valid` 全程未误拉高。
- 重发后第 8 拍 `rx_data_valid` 高一拍、`rx_data` 正确。
- 仿真输出这两个用例 `Passed`。

## 6. 本讲小结

- `spi_rx` 只做一件事：在 SPI 时钟驱动下把 `serial_data_in` 逐位拼装成 `rx_data`，收满一字拉高 `rx_data_valid` 一拍。
- 采样边沿由 `rx_active_edge(spi_clk, CPOL, CPHA)` 决定；模式 0（测试台唯一覆盖的模式）下为**上升沿**，与发送方的换数边沿错开半拍。
- 整个逻辑只有一个对 `spi_clk` 敏感的进程，内部用变量 `bit_index`（范围来自 `spi_pkg` 的 `data_range_t`，随 `DATA_WIDTH` 自动伸缩）做位计数。
- `rst_n='0'` 或 `spi_chip_select_n='1'` 都会让 `bit_index` 复位到起点——这是「撤销片选即重新对齐」的根源。
- `rx_data_valid` 是单拍脉冲：每拍开头赋 `'0'`，仅收齐那一拍靠「最后赋值胜出」语义被覆盖为 `'1'`。
- 源码注释「顺序很重要」警示：`bit_index` 是变量，必须先写入 `rx_data`、判定末位，最后才 `update_bit_index`。

## 7. 下一步学习建议

- 下一讲 **u10-l4（SPI 顶层接口：多片选状态机与 TX FIFO）** 会把 `spi_tx`（u10-l2）与本讲的 `spi_rx` 一起组装进 `spi_interface`，用一个六状态 FSM 轮询多片从机，并用异步 FIFO 缓存待发数据。你将看到 `spi_rx`/`spi_tx` 的握手信号如何在顶层被串起来。
- 若想巩固「单进程 + 变量位计数」的范式，可回头对比 [spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd) 里 `bit_index` 的用法，体会收发两侧的对称性。
- 验证方法学方面，可结合 **u11-l2（OSVVM 随机化与断言校验）** 深读本测试台的 `random_test`（1000 轮随机片选抖动），理解 `RandomPType` + `DistSl` 如何对「中途打断再对齐」做压力覆盖。
- 待补的开放问题：为 `rx_active_edge` 的模式 2/3 补一组成套测试（u10-l1 已标记），届时可回来核实本模块在 CPOL=1 下的真实行为。
