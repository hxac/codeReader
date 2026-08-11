# SPI 发送 spi_tx

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `spi_tx` 这个模块**做什么**：把一组并行数据（如一个字节）按 SPI 协议逐比特搬到一根串行线上。
- 读懂它的**串行化进程**：如何用 `tx_data_valid` / `tx_data_ack` 握手捕获数据，再用 `bit_index` 一拍推一位地把数据移出去。
- 理解它为什么用 **`case generate` 手写每种 SPI 模式的边沿对齐**，而不是直接调用通用包里的边沿函数（答案：Xilinx 综合会把通用边沿函数综合成锁存器）。
- 看懂它如何复用 [u5-l1](u5-l1-clock-enable-gating.md) 讲过的 `clock_enable`，通过门控 `spi_clk_in` 来产生主机的 SPI 时钟 `spi_clk_out`。
- 能够参照测试台 `tb_spi_tx` 设计一个发送固定 `0xA5` 的用例，并在波形上核对数据与时钟的边沿对齐关系。

本讲承接 [u10-l1](u10-l1-spi-pkg-modes.md)（`spi_pkg` 通用包与 SPI 四种模式）与 [u5-l1](u5-l1-clock-enable-gating.md)（`clock_enable` 时钟门控），是阅读 SPI 子系统的第二步。

## 2. 前置知识

### 2.1 SPI 一句话回顾

SPI 是一种主从、全双工、同步串行协议：主机（controller）给出时钟 `SCLK` 和片选 `CS_n`，在每个时钟沿上，主机通过 `MOSI` 发一位、从机通过 `MISO` 回一位。本讲的 `spi_tx` 只负责**发送方向（MOSI）**。

四种 SPI 模式由两个参数组合而成（详见 [u10-l1](u10-l1-spi-pkg-modes.md)）：

| 模式 | CPOL | CPHA | 时钟空闲电平 | 数据变化沿（TX） |
|------|------|------|--------------|------------------|
| 0 | 0 | 0 | 低 | 下降沿 |
| 1 | 0 | 1 | 低 | 上升沿 |
| 2 | 1 | 0 | 高 | 上升沿（本库近似） |
| 3 | 1 | 1 | 高 | 下降沿 |

### 2.2 本讲会用到的前置术语

- **entity / architecture**：[u2-l1](u2-l1-multi-architecture-pattern.md) 讲过的「端口契约 + 实现」。本模块只有一套 `behavioural` 架构，厂商差异下放到它例化的 `clock_enable` 里。
- **generic package（通用包）**：[u10-l1](u10-l1-spi-pkg-modes.md) 讲过，`spi_pkg` 带 `DATA_WIDTH` / `MSB_FIRST_AND_NOT_LSB` 两个类属，使用方各自 `new` 一份。
- **时钟门控 / BUFGCE**：[u5-l1](u5-l1-clock-enable-gating.md) 讲过，用 `clock_enable` 在传输期间放行时钟、空闲时关断。
- **`?? ` 条件运算符**：VHDL-2008 把 `std_ulogic` 转 `boolean` 的运算符，`?? tx_data_valid` 等价于「`tx_data_valid` 为真」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|------|------|----------|
| [ip/communication/spi/spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd) | 设计源码（可综合） | 本讲主角：串行化进程、`case generate` 对齐、`clock_enable` 例化都在这里 |
| [ip/communication/spi/spi_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd) | 通用包 | 提供 `reset_bit_index` / `last_bit_index` / `update_bit_index` 等过程，驱动 `bit_index` |
| [ip/communication/spi/tb/tb_spi_tx.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd) | 测试台（仅仿真） | 实践任务的参照蓝本，演示握手与片选时序 |
| [ip/clock_enable/clock_enable.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd) | 设计源码（可综合） | 被 `spi_tx` 例化，产生 `spi_clk_out` |

## 4. 核心概念与源码讲解

### 4.1 spi_tx 模块全景：端口、握手与整体职责

#### 4.1.1 概念说明

`spi_tx` 解决的问题很直接：**给定一个并行宽度的字（默认 8 位），把它变成一串随时间变化的单比特流，并配上正确的 SPI 时钟与片选信号。**

它有一个关键设计选择：本模块**自身只有一套 `behavioural` 架构**，厂商相关的部分（时钟门控是否用 BUFGCE）通过例化 `clock_enable` 并透传两个 generic 来实现。这和 [u2-l1](u2-l1-multi-architecture-pattern.md) 的「同一 entity 多 architecture」是同一思想的另一种表达——**换厂商只改 generic，不改 RTL**。

#### 4.1.2 端口与类属

`spi_tx` 的类属分为三组：SPI 模式、数据格式、时钟门控策略。

```vhdl
generic (
    SPI_CLK_POLARITY: bit := '0';                      -- CPOL
    SPI_CLK_PHASE: bit := '0';                         -- CPHA
    DATA_WIDTH: natural := 8;
    CONTROLLER_AND_NOT_PERIPHERAL: boolean := true;    -- true=主机
    MSB_FIRST_AND_NOT_LSB: boolean := true;            -- 位序
    ENABLE_INTERNAL_CLOCK_GATING: boolean := true;     -- 透传给 clock_enable
    USE_XILINX_CLK_GATE_AND_NOT_INTERNAL: boolean := false
);
```

> 引用：[spi_tx.vhd:32-40](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L32-L40) 声明三组类属。

端口契约如下（注意两个不寻常的类型选择）：

```vhdl
port (
    spi_clk_in: in std_ulogic;            -- 输入参考时钟
    rst_n: in std_ulogic;                 -- 低有效复位
    tx_data: in std_ulogic_vector(DATA_WIDTH - 1 downto 0);
    tx_data_valid: in std_ulogic;         -- 握手：数据有效
    tx_data_ack: out std_ulogic;          -- 握手：已接收
    spi_clk_out: out std_ulogic;          -- 主机产生的 SPI 时钟
    serial_data_out: out std_logic;       -- 串行 MOSI，用 std_logic 才能输出 'Z'
    spi_chip_select_n: inout std_ulogic;  -- 片选，inout 兼容从机模式
    tx_is_ongoing: out std_ulogic         -- 传输进行中状态
);
```

> 引用：[spi_tx.vhd:41-54](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L41-L54) 端口声明。

两个细节值得记住：

- `serial_data_out` 用 `std_logic` 而不是全库常见的 `std_ulogic`，是因为内部默认值是 `'Z'`（高阻）。`std_ulogic` 不含 `'Z'` 的解析语义，`std_logic` 才支持三态总线驱动——这是为真实总线（多驱动、上拉、开漏）留的口子。
- `spi_chip_select_n` 是 `inout`，因为同一端口在**主机模式**下作输出、在**从机模式**下作输入（从机的 CS 由主机决定）。

#### 4.1.3 核心流程：一次发送的全貌

一次发送经历三个阶段：

1. **空闲**：`tx_started=false`，每拍默认输出 `'Z'` / 片选释放 / 时钟被门控关断。
2. **捕获 + 移位**：用户拉高 `tx_data_valid`，下一个上升沿模块回一个 `tx_data_ack` 脉冲并锁存数据，随后逐拍把比特搬到 `serial_data_out`，期间片选保持有效、SPI 时钟放行。
3. **结束**：最后一个比特送出后，`tx_started` 复位，回到空闲。

握手时序可概括为：

```
用户:  tx_data_valid ___|‾‾‾‾‾‾‾‾|________________   （看到 ack 后撤销）
模块:  tx_data_ack    ___|‾|_______________________   （单拍脉冲）
模块:  tx_is_ongoing  ___|‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾|___  （持续整个传输）
```

> 注意：`tx_data_ack` 是**单拍脉冲**，仅在捕获那一拍为 `'1'`；用户应在看到 `tx_data_ack`（或 `tx_is_ongoing`）后撤销 `tx_data_valid`，否则会在本次传输结束的下一拍立刻触发下一次发送。

#### 4.1.4 代码实践：读懂握手

**实践目标**：确认「`tx_data_ack` 是单拍脉冲」这件事。

**操作步骤**：
1. 打开 [tb_spi_tx.vhd:154-176](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L154-L176)（`test_single_word_transmission`）。
2. 注意它先 `tx_data_valid <= '1'`，再 `wait until tx_is_ongoing`，然后立刻 `tx_data_valid <= '0'`。

**需要观察的现象**：测试台并不等 `tx_data_ack` 拉低，而是用一个 `wait` 隐式跨过那一拍。

**预期结果**：握手成立，传输正常完成（测试通过）。

**待本地验证**：在波形上数 `tx_data_ack` 高电平持续了几个 `spi_clk` 周期——预期恰好 1 个。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `serial_data_out` 用 `std_logic` 而不是 `std_ulogic`？

**参考答案**：因为模块内部把它的默认值设为 `'Z'`（高阻），用于三态总线场景。`std_ulogic` 的值集里没有适合多驱动解析的 `'Z'` 语义，`std_logic`（即 `std_ulogic` 的 resolved 子类型）才支持。

**练习 2**：`spi_chip_select_n` 为什么声明成 `inout`？

**参考答案**：同一端口在主机模式（`CONTROLLER_AND_NOT_PERIPHERAL=true`）下由模块驱动（输出），在从机模式下由外部主机驱动（输入）。`inout` 让一份端口契约同时服务两种角色。

---

### 4.2 串行化进程与 bit_index 推进

#### 4.2.1 概念说明

「串行化（serialization）」就是把一个 N 位的字，在第 0 拍送出第 1 位、第 1 拍送出第 2 位……直到 N 位全部送完。`spi_tx` 用一个**单进程 + 一个变量 `bit_index`** 完成这件事，`bit_index` 指向「当前该送哪一位」。

`bit_index` 的推进规则由 [u10-l1](u10-l1-spi-pkg-modes.md) 讲过的通用包 `spi_pkg` 提供的三个过程/函数驱动：

- `reset_bit_index`：复位到起始位（MSB 优先时是最高位）。
- `last_bit_index`：判断是否到了最后一位。
- `update_bit_index`：推进到下一位（MSB 优先时递减）。

#### 4.2.2 核心流程

模块在 `spi_tx_logic` 进程里用一个布尔变量 `tx_started` 当状态机，只有两个状态：空闲与发送中。

```
每个 spi_clk_in 上升沿：
  先打默认值：serial='Z', ack='0', cs_internal='1'
  若 rst_n='0'：复位 bit_index，tx_started=false
  否则：
    若 tx_data_valid 且 未在发送：              # 捕获
        ack<='1'; 锁存 tx_data_reg; tx_started:=true; reset_bit_index
    若 正在发送：
        cs_internal<='0'
        serial_internal <= tx_data_reg(bit_index)   # 输出当前位
        若 是最后一位：tx_started:=false             # 送完
        否则（主机模式）：update_bit_index           # 推进
```

一个关键点：捕获与输出第一位**发生在同一个上升沿**。因为 `tx_started` 是 **variable（变量）**，`:=` 立即生效——置 `tx_started:=true` 后，紧随其后的 `if tx_started then` 在**同一拍**就成立了。这一点与 [u6-l2](u6-l2-dual-port-ram.md) 里用 variable 实现 read-before-write 是同一类技巧。

#### 4.2.3 源码精读

模块先在架构里就地例化一份受约束的 `spi_pkg`，这样后面的过程/函数都带上了本模块的 `DATA_WIDTH` 与位序：

```vhdl
architecture behavioural of spi_tx is
    package spi_pkg_constrained is new work.spi_pkg
        generic map (
            DATA_WIDTH => DATA_WIDTH,
            MSB_FIRST_AND_NOT_LSB => MSB_FIRST_AND_NOT_LSB
        );
    use spi_pkg_constrained.all;
```

> 引用：[spi_tx.vhd:57-63](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L57-L63) 在架构内例化通用包。每个使用方（设计、测试台）各 `new` 一份，互不干扰（[u10-l1](u10-l1-spi-pkg-modes.md)）。

串行化进程的核心（保留关键行）：

```vhdl
spi_tx_logic: process(spi_clk_in)
    variable bit_index: natural range 0 to tx_data'subtype'high;  -- 0..7
    variable tx_data_reg: tx_data'subtype;
    variable tx_started: boolean := false;
begin
    if rising_edge(spi_clk_in) then
        serial_data_out_internal <= 'Z';
        tx_data_ack <= '0';
        spi_chip_select_n_internal <= '1';

        if rst_n = '0' then
            reset_bit_index(bit_index);
            tx_started := false;
        else
            if (?? tx_data_valid) and not tx_started then
                tx_data_ack <= '1';          -- 单拍应答
                tx_data_reg := tx_data;       -- 锁存
                tx_started := true;
                reset_bit_index(bit_index);   -- 回到 MSB
            end if;

            if tx_started then
                spi_chip_select_n_internal <= '0';
                serial_data_out_internal <= tx_data_reg(bit_index);  -- 送当前位
                if last_bit_index(bit_index) then
                    tx_started := false;      -- 送完最后一位
                elsif CONTROLLER_AND_NOT_PERIPHERAL or
                      (not CONTROLLER_AND_NOT_PERIPHERAL and (spi_chip_select_n = '0')) then
                    update_bit_index(bit_index);  -- 推进
                end if;
            end if;
        end if;
    end if;
    tx_is_ongoing <= '1' when tx_started else '0';
end process;
```

> 引用：[spi_tx.vhd:68-105](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L68-L105) 串行化进程。

逐行解读四个要点：

1. **`bit_index` 的范围由端口反推**：`natural range 0 to tx_data'subtype'high`。对 8 位数据，`tx_data'subtype` 是 `std_ulogic_vector(7 downto 0)`，`'high` 是 7，故范围为 `0 to 7`，正好匹配 `spi_pkg` 里的 `data_range_t`（`0 to DATA_WIDTH-1`）。这是 [u6-l1](u6-l1-single-port-ram.md) 讲过的「非约束端口 + 内部推导尺寸」技巧的又一次应用。
2. **`?? tx_data_valid`**：VHDL-2008 条件运算符，把 `std_ulogic` 转 `boolean`，等价于「有效」。
3. **`elsif` 里的从机条件**：主机模式（`CONTROLLER_AND_NOT_PERIPHERAL=true`）无条件推进；从机模式只有在 `spi_chip_select_n='0'`（被选中）时才推进一位——从机的移位节奏要跟着主机的片选与时钟走。
4. **`tx_is_ongoing` 写在 `rising_edge` 之外**：它直接反映变量 `tx_started`，作为「传输进行中」的状态指示。

`bit_index` 的三个过程实现非常短，值得一看（来自 [spi_pkg.vhd](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd)）：

```vhdl
function last_bit_index(bit_index: data_range_t) return boolean is begin
    if MSB_FIRST_AND_NOT_LSB then
        return bit_index = bit_index'subtype'low;   -- MSB优先：低位是末位
    else
        return bit_index = bit_index'subtype'high;
    end if;
end function;

procedure reset_bit_index(bit_index: inout data_range_t) is begin
    bit_index := bit_index'subtype'high when MSB_FIRST_AND_NOT_LSB
                                      else bit_index'subtype'low;
end procedure;

procedure update_bit_index(bit_index: inout data_range_t) is begin
    if last_bit_index(bit_index) then reset_bit_index(bit_index); return; end if;
    bit_index := bit_index - 1 when MSB_FIRST_AND_NOT_LSB else bit_index + 1;
end procedure;
```

> 引用：[spi_pkg.vhd:77-96](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L77-L96) 三个过程都用 `'subtype` 属性取边界，不写死数字，因而对任意 `DATA_WIDTH` 都成立（[u10-l1](u10-l1-spi-pkg-modes.md)）。

**为什么用过程（`procedure`）+ `inout` 变量，而不是函数返回新值？** 因为 `bit_index` 是 `variable`，过程通过 `inout` 形参直接改写它，省去了「赋返回值」的一步，写起来更像 `i++`。

#### 4.2.4 一个字（8 位）的逐拍轨迹

设 `tx_data = 0xA5`，MSB 优先、主机模式。`0xA5 = 1010_0101`，MSB 在 index 7。

| 上升沿 | bit_index | 输出 `tx_data_reg(bit_index)` | 备注 |
|--------|-----------|-------------------------------|------|
| 0（捕获） | 7 | 1（MSB） | 同拍捕获并送首位，ack=1 |
| 1 | 6 | 0 | |
| 2 | 5 | 1 | |
| 3 | 4 | 0 | |
| 4 | 3 | 0 | |
| 5 | 2 | 1 | |
| 6 | 1 | 0 | |
| 7 | 0 | 1（LSB） | `last_bit_index` 真，`tx_started:=false` |
| 8 | 0 | （回到 'Z'） | 空闲，cs_internal 释放 |

可见 8 位数据恰好占用 8 个上升沿（捕获沿即送首位），片选全程为 0。

#### 4.2.5 代码实践：用变量当状态机

**实践目标**：理解「`tx_started` 是变量，所以捕获与首位输出同拍发生」。

**操作步骤**：
1. 阅读上面的逐拍轨迹表。
2. 在 [spi_tx.vhd:84-100](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L84-L100) 找到捕获块与发送块。
3. 思考：如果把 `tx_started := true` 改成信号赋值（假设它是个 signal），会发生什么？

**需要观察的现象 / 预期结果**：若 `tx_started` 是信号，则捕获当拍它仍是 `false`，`if tx_started then` 不成立，**首位要晚一拍才输出**，整个传输会多耗一拍。这正是变量与信号的区别（[u6-l2](u6-l2-dual-port-ram.md)）。

**待本地验证**：不改源码，仅在脑中/草稿上推演即可（改动设计源码会违反本讲约束）。

#### 4.2.6 小练习与答案

**练习 1**：发送 `0xA5`、MSB 优先时，串行线上依次出现的 8 个比特是什么？

**参考答案**：`0xA5 = 1010_0101`，MSB（bit 7）在前，序列为 `1,0,1,0,0,1,0,1`。

**练习 2**：为什么 `bit_index` 用过程 + `inout` 变量推进，而不是函数返回新索引？

**参考答案**：`bit_index` 是进程内的 `variable`，过程通过 `inout` 形参直接改写它，等价于「`bit_index := bit_index - 1`」的内联写法，比「`bit_index := update(bit_index)`」更简洁，也更容易表达「到末位则折回」的复合逻辑。

---

### 4.3 case generate：串行数据与片选的多模式对齐

#### 4.3.1 概念说明

`spi_tx_logic` 进程在 `rising_edge(spi_clk_in)` 上更新 `serial_data_out_internal`。但 SPI 协议要求**数据变化的边沿随模式而定**（见本讲 2.1 节表格）：模式 0/3 在下降沿变化，模式 1 在上升沿变化。如果直接把内部信号接出去，模式就不对了。

你也许会问：[u10-l1](u10-l1-spi-pkg-modes.md) 不是已经定义了 `tx_active_edge` 函数吗，为什么不直接 `if tx_active_edge(spi_clk, ...) then`？源码顶部的注释给出了答案：

```
-- NOTE: Xilinx doesn't recognise generic functions that have the same definitions
-- like rising_edge/falling_edge, thus, it creates latches instead of FFs
-- So we've to manually state for which SPI mode at what edge has to be sampled.
-- With Intel the previous solution worked perfectly
```

> 引用：[spi_tx.vhd:107-109](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L107-L109) 说明为何手写 `case generate`。

也就是说：**Xilinx Vivado 综合器无法从「通用包里返回 `rising_edge(clk)` 的函数」推断出触发器，转而综合成锁存器（latch）**——这是严重的时序隐患。解决办法是用 `case generate` 在编译期为每种模式显式写出 `rising_edge` / `falling_edge`，让综合器一眼认出触发器。Intel 工具没这个问题，但为了一份 RTL 两家用，统一写成 `case generate`。

#### 4.3.2 核心流程：串行数据对齐

`serial_data_out` 的对齐分四种模式，由 `case SPI_CLK_POLARITY & SPI_CLK_PHASE generate` 选择：

```vhdl
serial_data_out_alignment: case SPI_CLK_POLARITY & SPI_CLK_PHASE generate
    when "00" | "11" =>                       -- 模式0/3：下降沿变化
        alignment: process (spi_clk_in)
        begin
            if falling_edge(spi_clk_in) then
                serial_data_out <= serial_data_out_internal;
            end if;
        end process;
    when "01" =>                               -- 模式1：上升沿变化（postpone）
        postpone: process (spi_clk_in)
        begin
            if rising_edge(spi_clk_in) then
                serial_data_out <= serial_data_out_internal;
            end if;
        end process;
    when "10" =>                               -- 模式2：组合直通
        pass_through: serial_data_out <= serial_data_out_internal;
end generate;
```

> 引用：[spi_tx.vhd:114-131](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L114-L131) 串行数据按模式对齐。

三个模式各有讲究：

- **模式 0 / 3（`"00"` / `"11"`）**：用 `falling_edge` 进程把内部信号「重打一拍」到下降沿。数据在下降沿变化，接收方在（下一）上升沿采样——符合模式 0 的标准。
- **模式 1（`"01"`）**：用 `rising_edge` 进程，且标记为 **`postpone`（推迟进程）**。推迟进程在当前仿真时刻的所有 delta 循环结束后才运行，保证 `serial_data_out_internal` 已稳定，避免同沿竞争。
- **模式 2（`"10"`）**：组合直通（连续赋值）。因为内部信号本就在上升沿更新，直通意味着 `serial_data_out` 在上升沿后一个 delta 周期跟着变，等价于「在上升沿附近变化」。

#### 4.3.3 核心流程：片选对齐

片选 `spi_chip_select_n` 的对齐更精巧。它由两个信号 `spi_chip_select_n_assertion` 与 `spi_chip_select_n_deassertion` **相与**驱动：

```vhdl
spi_chip_select_n <= spi_chip_select_n_assertion and spi_chip_select_n_deassertion;
```

> 引用：[spi_tx.vhd:155](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L155) 片选 = 断言路 与 解除断言路。

直觉解释：`X and Y` 只要任一路为 `'0'`，输出就是 `'0'`（片选有效）。两个信号分别在「该断言的边沿」和「该解除的边沿」上采样内部片选。这样设计的目的，是让片选在**正确边沿上翻转、且足够「粘」**——一旦在某沿断言，就保持到另一沿也看到解除为止，从而把片选窗口干干净净地包住整串时钟脉冲，避免在首尾产生毛刺脉冲（这同时关系到下一段时钟门控的洁净度）。

具体采样边沿由 `CPOL` 决定：

```vhdl
spi_chip_select_n_alignment: case SPI_CLK_POLARITY generate
    when '0' =>                                  -- CPOL=0
        alignment: process (spi_clk_in)
        begin
            if falling_edge(spi_clk_in) then
                spi_chip_select_n_assertion <= spi_chip_select_n_internal;   -- 下降沿断言
            elsif rising_edge(spi_clk_in) then
                spi_chip_select_n_deassertion <= spi_chip_select_n_internal; -- 上升沿解除
            end if;
        end process;
    when '1' =>                                  -- CPOL=1
        alignment: process (spi_clk_in, spi_chip_select_n_internal)
        begin
            pass_through: spi_chip_select_n_assertion <= spi_chip_select_n_internal; -- 组合断言
            if falling_edge(spi_clk_in) then
                spi_chip_select_n_deassertion <= spi_chip_select_n_internal; -- 下降沿解除
            end if;
        end process;
end generate;
```

> 引用：[spi_tx.vhd:134-153](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L134-L153) 片选按 CPOL 选择断言/解除边沿。

对 `CPOL=0`（时钟空闲低，如模式 0）：片选在下降沿断言、上升沿解除，保证它在第一个时钟上升沿（接收方采样点）之前就已经拉低。对 `CPOL=1`（空闲高）：断言路组合直通、解除路在下降沿采样。

整个对齐块包在一个 `block` 里，让两组局部信号（`..._assertion` / `..._deassertion`）的作用域只局限于此块，不污染架构其余部分。

#### 4.3.4 代码实践：核对模式 0 的边沿对齐

**实践目标**：在波形上验证模式 0 下 `serial_data_out` 确实在 `spi_clk` 下降沿变化。

**操作步骤**：
1. 打开 [tb_spi_tx.vhd:50-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L50-L51)，确认测试台固定为 `SPI_CLK_POLARITY='0'`、`SPI_CLK_PHASE='0'`（模式 0）。
2. 用 `test_runner.py` 以 `gui=True` 跑 `tb_spi_tx`（参见 [u1-l3](u1-l3-environment-and-simulation.md)）。
3. 在波形窗口对齐 `spi_clk`、`serial_data_out`、`spi_chip_select_n` 三条线。

**需要观察的现象**：`serial_data_out` 的每次翻转都发生在 `spi_clk` 的下降沿；片选在首个下降沿附近拉低。

**预期结果**：数据在下降沿变化、接收方可在上升沿稳定采样——这正是模式 0 的正确行为。

**待本地验证**：波形需在装有 ModelSim/QuestaSim 或 NVC + GUI 的环境本地观察。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接写 `if tx_active_edge(spi_clk, CPOL, CPHA) then serial_data_out <= ...`，而要展开成 `case generate`？

**参考答案**：因为 Xilinx Vivado 无法从通用包里返回 `rising_edge`/`falling_edge` 的函数推断出触发器，会综合成锁存器。用 `case generate` 在编译期为每种模式显式写出 `rising_edge`/`falling_edge`，综合器才能正确识别为边沿触发寄存器。

**练习 2**：模式 1（`"01"`）的对齐进程为什么要标 `postpone`？

**参考答案**：模式 1 在上升沿更新 `serial_data_out`，而内部信号 `serial_data_out_internal` 也在上升沿更新。`postpone` 进程在当前时刻所有 delta 循环结束后才执行，确保读到的是已稳定的内部信号，避免同沿读写竞争。

---

### 4.4 clock_enable 时钟门控产生 SPI 时钟

#### 4.4.1 概念说明

SPI 主机必须向从机提供 `SCLK`。`spi_tx` 的做法不是用分频器造一个新时钟，而是**只在传输期间把输入参考时钟 `spi_clk_in` 放行到 `spi_clk_out`，空闲时关断**。这件事交给 [u5-l1](u5-l1-clock-enable-gating.md) 讲过的 `clock_enable` 模块完成。

关键选择：**门控使能信号是 `not spi_chip_select_n`**。也就是说——片选有效（=0）时放行时钟，片选释放（=1）时关断时钟。这样时钟与片选天然同步，传输一结束时钟立刻停。

#### 4.4.2 核心流程

```vhdl
spi_clk_driver: if CONTROLLER_AND_NOT_PERIPHERAL generate
    spi_clk_enable_inst: entity work.clock_enable
        generic map (
            ENABLE_INTERNAL_CLOCK_GATING => ENABLE_INTERNAL_CLOCK_GATING,
            USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => USE_XILINX_CLK_GATE_AND_NOT_INTERNAL
        )
        port map (
            clk_in => spi_clk_in,
            clk_enable => not spi_chip_select_n,   -- 片选有效才放行时钟
            clk_out => spi_clk_out
        );
else generate
    spi_clk_out <= '-';   -- 从机不产生时钟
end generate;
```

> 引用：[spi_tx.vhd:160-180](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_tx.vhd#L160-L180) 主机模式例化 `clock_enable`，从机模式输出无关值。

四个要点：

1. **主机才产生时钟**：`if CONTROLLER_AND_NOT_PERIPHERAL generate`。从机模式下 `spi_clk_out <= '-'`（don't care），因为从机的时钟来自外部主机。
2. **两个 generic 透传**：`ENABLE_INTERNAL_CLOCK_GATING` 与 `USE_XILINX_CLK_GATE_AND_NOT_INTERNAL` 直接透传给 `clock_enable`，于是厂商策略（Xilinx 用 BUFGCE、Intel 不直接门控）由使用方在例化 `spi_tx` 时一次性决定（[u5-l1](u5-l1-clock-enable-gating.md)）。
3. **使能取自片选**：`clk_enable => not spi_chip_select_n`。注意这里用的是**已对齐的输出片选**（4.3 节 `and` 的结果），而不是内部 `spi_chip_select_n_internal`，因此时钟的启停也沾了片选对齐的好处——在正确的边沿上启停，避免首尾毛刺。
4. **`spi_clk_out` 与 `serial_data_out` 的协作**：时钟只在传输期间翻转，数据对齐（4.3 节）又把数据变化沿对到正确的边沿。两者共同保证从机看到的 `SCLK` 与 `MOSI` 满足所选 SPI 模式。

回顾 `clock_enable` 的内部（[u5-l1](u5-l1-clock-enable-gating.md)）：两层 `if generate` 裁剪出三种实现。

```vhdl
clk_gating: if ENABLE_INTERNAL_CLOCK_GATING generate
    xilinx_clk_gate: if USE_XILINX_CLK_GATE_AND_NOT_INTERNAL generate
        BUFGCE_inst: BUFGCE port map (O => clk_out, CE => clk_enable, I => clk_in);
    else generate
        clk_out <= clk_in when clk_enable = '1' else '0';   -- 通用门控
    end generate;
else generate
    clk_out <= clk_in;                                       -- 直通（Intel 推荐）
end generate;
```

> 引用：[clock_enable.vhd:45-61](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/clock_enable/clock_enable.vhd#L45-L61)。

`tb_spi_tx` 例化时用的是 `ENABLE_INTERNAL_CLOCK_GATING => true`、`USE_XILINX_CLK_GATE_AND_NOT_INTERNAL => false`，即走「通用门控」分支（`clk_out <= clk_in when enable='1' else '0'`），不依赖 Xilinx 厂商库即可仿真。

> 引用：[tb_spi_tx.vhd:336-345](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L336-L345) 测试台选择通用门控配置。

#### 4.4.3 一个边界提醒：CPOL 与空闲电平

「通用门控」分支在 `enable='0'` 时输出 `'0'`（低电平）。这与 `CPOL=0`（空闲低）一致，故模式 0/1 的仿真里 `spi_clk_out` 空闲为低、传输时翻转，符合预期。但若 `CPOL=1`（空闲应高），通用门控会把空闲电平拉成低，与本模块对「空闲高」的期望不符。因此：

- **模式 0 / 1（CPOL=0）**：通用门控完全适用，`tb_spi_tx` 覆盖的就是模式 0。
- **CPOL=1 的模式**：宜改用 Xilinx BUFGCE（其在 CE 关断时保持上一电平）或改用寄存器使能策略，并需要专门的测试覆盖。

> 这是 [u10-l1](u10-l1-spi-pkg-modes.md) 提到的「现有测试台只覆盖模式 0」这一现象在时钟侧的具体体现。模式 1/2/3 的完整时序待本地验证。

#### 4.4.4 代码实践：观察时钟门控

**实践目标**：确认 `spi_clk_out` 只在 `spi_chip_select_n='0'` 期间翻转。

**操作步骤**：
1. 用 GUI 模式跑 `tb_spi_tx`。
2. 把 `spi_clk_out` 与 `spi_chip_select_n` 上下对齐放置。

**需要观察的现象**：片选拉低期间 `spi_clk_out` 是连续方波；片选拉高后 `spi_clk_out` 立刻停在低电平。

**预期结果**：传输窗口与时钟窗口精确重合，证明使能信号 `not spi_chip_select_n` 起作用。

**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `clk_enable` 接 `not spi_chip_select_n`（输出片选）而不是 `not spi_chip_select_n_internal`（内部片选）？

**参考答案**：输出片选经过了 4.3 节的边沿对齐，翻转发生在正确的时钟边沿上；用它作使能，时钟的启停也发生在正确边沿，避免首尾出现毛刺脉冲。用未对齐的内部信号则可能在半周期处启停时钟，产生窄脉冲。

**练习 2**：从机模式下 `spi_clk_out` 是什么？

**参考答案**：`'-'`（don't care）。从机不产生 SPI 时钟，时钟由外部主机提供，故输出设为无关值，便于综合器自由处理。

---

## 5. 综合实践：发送固定 0xA5 并切换模式 1

本任务把前四个模块串起来：握手捕获（4.1）、逐位移出（4.2）、边沿对齐（4.3）、时钟门控（4.4）。

### 5.1 实践目标

在 `tb_spi_tx` 中新增一个用例 `test_fixed_0xA5_msb_first`，发送固定的 `0xA5`（MSB 优先），在波形上逐位核对 `serial_data_out` 与 `spi_clk_out` 的边沿对齐；再切换到模式 1（CPOL=0, CPHA=1）观察对齐变化。

### 5.2 操作步骤

**第 1 步：新增用例骨架。** 在 [tb_spi_tx.vhd:315-327](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L315-L327) 的 `test_suite` 循环里加一支：

```vhdl
elsif run("test_fixed_0xA5_msb_first") then
    test_fixed_0xA5_msb_first;
```

并在 `checker` 进程的说明区新增下面这个过程（**示例代码**，遵循现有测试台的握手风格）：

```vhdl
-- 示例代码：发送固定 0xA5，逐位核对
procedure test_fixed_0xA5_msb_first is
    constant PATTERN: std_ulogic_vector(7 downto 0) := x"A5";
begin
    info("5.0) Testing fixed pattern 0xA5 (MSB first)");

    rst_n <= '1';
    tx_data <= PATTERN;
    tx_data_valid <= '1';
    wait until tx_data_ack;          -- 等捕获应答
    tx_data_valid <= '0';

    -- 期望 MSB-first 序列：1,0,1,0,0,1,0,1
    wait_tx_spi_clk_cycles(DATA_WIDTH);

    wait_spi_clk_cycles(1);
    check_equal(got => tx_is_ongoing, expected => '0',
                msg => "TX should be done after 8 bits");
    info("Fixed 0xA5 test passed" & LF);
end procedure;
```

> 说明：`wait_tx_spi_clk_cycles` 已在 [tb_spi_tx.vhd:119-124](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L119-L124) 定义，它在 `tx_active_edge` 上计数。

**第 2 步：模式 0 波形核对。** 以 `gui=True` 跑该用例，在波形上把 `spi_clk`、`spi_clk_out`、`serial_data_out`、`spi_chip_select_n` 对齐。按 4.2.4 节的逐拍表，确认串行序列为 `1,0,1,0,0,1,0,1`，且每次数据翻转发生在 `spi_clk` 下降沿。

**第 3 步：切换模式 1。** 把测试台常量 [tb_spi_tx.vhd:50-51](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/tb/tb_spi_tx.vhd#L50-L51) 改成 `SPI_CLK_PHASE: bit := '1';`，重新跑同一用例。

### 5.3 需要观察的现象与预期结果

| 观察项 | 模式 0（CPHA=0） | 模式 1（CPHA=1） |
|--------|------------------|------------------|
| 数据变化沿 | `spi_clk` 下降沿 | `spi_clk` 上升沿（推迟进程） |
| 首位（`1`）出现的时刻 | 捕获拍后对齐到下降沿 | 捕获拍后对齐到上升沿 |
| `spi_clk_out` 空闲电平 | 低（CPOL=0） | 低（CPOL=0） |
| 串行序列 | `1,0,1,0,0,1,0,1` | `1,0,1,0,0,1,0,1` |

切换模式 1 后，**序列内容不变**（`0xA5` 决定），但**数据相对时钟的相位移动了半个周期**——数据从「下降沿变化」变成「上升沿变化」。这就是 CPHA 改变对齐的直观效果。

### 5.4 待本地验证

- 上述波形观察需在带 GUI 仿真器的环境本地完成；模式 1 的完整通过性尚无现成测试覆盖（[u10-l1](u10-l1-spi-pkg-modes.md) 已指出测试台只配了模式 0），如发现对齐异常，请结合 4.3 节的 `postpone` 进程与 `tx_active_edge` 函数返回值（[spi_pkg.vhd:51-60](https://github.com/nselvara/HDL-Core-Library/blob/45eae77c279d259b0681e3f52732fd0d2b229c61/ip/communication/spi/spi_pkg.vhd#L51-L60)）核对。

## 6. 本讲小结

- `spi_tx` 把一个并行字逐比特搬到 `serial_data_out`，并用 `tx_data_valid`/`tx_data_ack` 做单拍握手；捕获与首位输出发生在同一上升沿，靠的是 `tx_started` 这个 **variable**。
- 移位节奏由通用包 `spi_pkg` 的 `reset_bit_index` / `last_bit_index` / `update_bit_index` 三个过程驱动 `bit_index`，边界用 `'subtype` 属性取，不写死数字，适配任意 `DATA_WIDTH` 与 MSB/LSB 位序。
- 因为 **Xilinx 综合器无法从通用边沿函数推断触发器**（会变成锁存器），串行数据与片选的对齐都用 `case generate` 为每种 SPI 模式显式写出 `rising_edge`/`falling_edge`；模式 1 额外用 `postpone` 进程规避同沿竞争。
- 片选由「断言路」与「解除断言路」相与产生，采样边沿随 CPOL 变化，目的是把片选窗口干净地包住整串时钟脉冲。
- 主机的 SPI 时钟由例化 `clock_enable` 门控 `spi_clk_in` 得到，使能信号 `not spi_chip_select_n`——片选有效才放行时钟；从机模式不产生时钟。
- 厂商差异（Xilinx BUFGCE vs Intel 不直接门控）通过透传两个 generic 下放给 `clock_enable`，`spi_tx` 本体保持单一架构。现有测试台只覆盖模式 0 且用随机数据，模式 1/2/3 与固定模式字尚需补测试。

## 7. 下一步学习建议

- **继续 SPI 子系统**：下一讲 [u10-l3](u10-l3-spi-rx.md) 讲接收方向 `spi_rx`，它在本讲对齐机制的基础上，在 `rx_active_edge` 采样串行输入，可对照本讲的发送方向理解全双工的另一侧。
- **看顶层整合**：[u10-l4](u10-l4-spi-interface-fsm.md) 的 `spi_interface` 把 `spi_tx`、`spi_rx`、异步 FIFO 与多片选 FSM 组合起来，是本讲模块的最终消费者，阅读它能验证你对握手与 `tx_is_ongoing` 的理解。
- **回查两个基础**：若对 `case generate` 的编译期裁剪或 `clock_enable` 的三种实现还有疑问，可分别回看 [u2-l1](u2-l1-multi-architecture-pattern.md) 与 [u5-l1](u5-l1-clock-enable-gating.md)。
- **补测试**：本讲实践暴露了「模式 1/2/3 缺测试」这一空白，结合 [u11-l1](u11-l1-vunit-testbench-structure.md) 与 [u11-l2](u11-l2-osvvm-random-assertions.md) 学到的 VUnit/OSVVM 方法，为 `spi_tx` 补一组覆盖四种模式的回归用例，是很实际的练手方向。
