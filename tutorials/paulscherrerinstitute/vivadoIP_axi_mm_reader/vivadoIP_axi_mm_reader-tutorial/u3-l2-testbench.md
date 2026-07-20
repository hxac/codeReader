# 测试台架构与用例

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `tb/top_tb.vhd` 这个**自校验测试台（self-checking testbench）**的整体结构：谁来发激励、谁来扮演被读的从机、谁来核对结果。
- 理解 `p_control` 与 `p_spi` 两个并发进程如何用 `StimCase` / `RespCase` 两个信号做**进程间握手**，从而把一次回归切成 6 个互不干扰的用例。
- 理解同一个测试台如何通过 `OutputType_g` 这个 generic 在 **AXIS** 与 **AXIMM** 两种输出模式下各跑一遍，并由 `CheckResults` 分派到 `CheckResultsAxiS` / `CheckResultsAxiMM` 两条不同的校验路径。
- 读懂 6 组激励/响应用例各自验证了 IP 的什么行为（单次读、缓冲双读、超时、禁用、背压、单寄存器四次读）。

## 2. 前置知识

在进入测试台之前，请确认你已经掌握以下概念（它们在前序讲义中讲过）：

- **IP 的两类 AXI 接口**（见 [u2-l1 整体架构与数据流](u2-l1-architecture-dataflow.md)）：`s00_axi` 是 AXI **从机**，软件（测试台扮演）经它写配置、读状态；`m00_axi` 是 AXI **主机**，IP 经它主动去读别人。本讲的测试台需要在 `s00_axi` 一侧扮演主机、在 `m00_axi` 一侧扮演从机。
- **两种输出模式 AXIS / AXIMM**（见 [u2-l1](u2-l1-architecture-dataflow.md)、[u2-l7 输出模式与 FIFO](u2-l7-output-modes-fifo.md)）：AXIS 经 `m_axis` 端口直出 AXI-Stream；AXIMM 把读回值映射到 `RdData`/`RdLast` 寄存器，软件读 `RdData` 才弹出 FIFO。
- **核心 FSM 的读周期**（见 [u2-l3 核心 FSM](u2-l3-core-fsm.md)）：`Trig` 或超时启动 → FSM 遍历 RegTable → 经 `m00_axi` 逐个单拍读 → 读回值进 FIFO → 按模式输出，收齐后发一拍 `DoneIrq`。
- **寄存器地图**（见 [u2-l2 寄存器映射](u2-l2-register-map.md)）：`Ctrl(Ena)`、`RegCnt`、`RdData`、`RdLast`、`Level` 五个固定寄存器 + 从 `0x20` 起的 `Addr[]` 配置表。

本讲还会用到几个测试台领域的常识与外部依赖库（非本仓库代码，但测试台直接调用）：

- **BFM（Bus Functional Model，总线功能模型）**：把一套总线协议（这里是 AXI4）的逐拍握手封装成「一次事务」层次的调用。本测试台没有实例化一个独立的 `axi_bfm` 元件，而是用**两对 AXI record 信号 + 过程调用**实现等价的 BFM 行为。
- **psi_tb**：PSI 的测试台支持库，提供本讲用到的所有过程——`axi_single_write` / `axi_single_read` / `axi_single_expect`（在 AXI 总线上做单拍事务并自校验）、`axi_expect_ar` / `axi_apply_rresp_single`（扮演 AXI 从机，消费 AR 命令、回送 R 数据）、`StdlvCompareInt` / `StdlCompare`（比较，不符即向 transcript 打 `###ERROR###`）、`PulseSig` / `ClockedWaitTime` / `CheckNoActivity` / `WaitForValueStdl`（时序控制）、`print`（向 transcript 打印）。它们的**可观察行为**从调用方式即可推断，本讲按用法描述。
- **自校验**：测试台自己既产生激励、又检查结果，发现不符就报 `###ERROR###`，被 [u1-l3](u1-l3-running-simulation.md) 讲过的 `run_check_errors "###ERROR###"` 捕获而让 CI 失败。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tb/top_tb.vhd` | 唯一的测试台，自校验。实体带一个 generic `OutputType_g`；内部实例化 DUT、生成时钟、跑 `p_control` 与 `p_spi` 两个并发进程，共 6 组用例。 |
| `sim/config.tcl` | PsiSim 仿真配置。用 `-gOutputType_g=AXIS` 与 `-gOutputType_g=AXIMM` 让同一个 `top_tb` 跑两遍。 |
| `hdl/definitions_pkg.vhd` | 提供寄存器字索引常量（`RegIdx_*_c`、`MemOffs_c`），测试台与 RTL 共用，保证两边地址一致。 |

## 4. 核心概念与源码讲解

### 4.1 AXI BFM：两对 record 总线与 DUT 的连线

#### 4.1.1 概念说明

这个 IP 有两个 AXI 接口，方向相反：

- `s00_axi`：DUT 是**从机**。要驱动它，测试台得扮演**主机**——经它写 `Ctrl`/`RegCnt`/`Addr[]`、读 `Level`/`RdData`/`RdLast`。
- `m00_axi`：DUT 是**主机**。要让读周期有数据可读，测试台得扮演**从机**——等着 DUT 发来的读命令（AR 通道），按约定地址回送数据（R 通道）。

所以测试台同时是「`s00_axi` 上的主机」和「`m00_axi` 上的从机」。它用**两对 AXI record 信号**分别建模这两条总线。record 类型 `axi_ms_r` / `axi_sm_r` 来自 `psi_tb_axi_pkg`，把 AXI 的五通道信号（AR/AW/W/B/R）打包成一条记录，避免端口列表写几十行。

> 记忆口诀：`_ms` = master side（主机侧输出），`_sm` = slave side（从机侧输出）；带 `_m` 后缀的属于 `m00_axi`（DUT 主机端口那一侧），不带后缀的属于 `s00_axi`（DUT 从机端口那一侧）。

#### 4.1.2 核心流程

- `axi_ms` / `axi_sm`：`s00_axi` 一侧的总线，地址宽度 8 位，用于配置/状态寄存器访问。
- `axi_ms_m` / `axi_sm_m`：`m00_axi` 一侧的总线，地址宽度 32 位，用于 DUT 主机的读通路。

DUT 实例化时，`s00_axi` 全部信号接到 `axi_ms`/`axi_sm`，`m00_axi` 信号接到 `axi_ms_m`/`axi_sm_m`，`m_axis`（仅 AXIS 模式有效）接到独立的 `m_axis_*` 信号。注意 generic 把 `Output_g => OutputType_g`——这正是「同一测试台跑两种模式」的入口。

#### 4.1.3 源码精读

总线宽度常量与四条 record 的声明（`s00_axi` 用 8 位地址、`m00_axi` 用 32 位地址）：

[tb/top_tb.vhd:38-68](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L38-L68) —— 声明 `axi_ms`/`axi_sm`（配置侧，窄地址）与 `axi_ms_m`/`axi_sm_m`（读通路侧，32 位地址）四条 AXI record 总线；`axi_ms_m.araddr` 直接声明为 `31 downto 0`，因为被读地址（如 `0x00AB0000`）是 32 位系统地址。

DUT 的实例化与 generic 映射（注意 `Output_g => OutputType_g` 与 `MaxRegCount_g => 16`、`MinBuffers_g => 2`，故内部 FIFO 深度为 \(16 \times 2 = 32\)）：

[tb/top_tb.vhd:157-165](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L157-L165) —— DUT `axi_mm_reader_wrp` 的 generic 映射；`Output_g` 由测试台 generic `OutputType_g` 透传，决定走 AXIS 还是 AXIMM；`ClkFrequencyHz => integer(ClockFrequencyAxi_c)` 把 125.0e6 的 real 转成整数 125 000 000 传给 DUT 参与超时换算。

DUT 端口到两条总线的连接（节选 `m00_axi` 读通路部分）：

[tb/top_tb.vhd:210-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L210-L223) —— `m00_axi` 的 AR/R 通道接到 `axi_ms_m`/`axi_sm_m`；DUT 主机只声明了读通道（AR+R）、没有写通道，与 [u2-l6](u2-l6-axi-master-read.md) 讲的「只读配置」一致。

时钟进程（125 MHz 方波，由 `TbRunning` 控制停摆）：

[tb/top_tb.vhd:238-248](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L238-L248) —— 产生周期 8 ns 的方波；激励进程在结尾把 `TbRunning <= false` 后，`while TbRunning loop` 退出、走到 `wait;` 永久挂起，时钟停止、仿真结束。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：确认「测试台在两个 AXI 接口上扮演的角色」。

**操作步骤**：

1. 打开 [tb/top_tb.vhd:166-232](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L166-L232) 的端口映射。
2. 对 `s00_axi_araddr`、`s00_axi_arready`、`s00_axi_rdata` 三个端口，写出它接的是 `axi_ms` 还是 `axi_sm` 的哪个字段。
3. 对 `m00_axi_araddr`、`m00_axi_arready`、`m00_axi_rdata` 三个端口，同样写出它接的是 `axi_ms_m` 还是 `axi_sm_m`。
4. 回答：为什么 `m00_axi` 一侧没有 `aw*`/`w*`/`b*` 这些写通道端口？

**预期结果**：

- `s00_axi_araddr` ← `axi_ms.araddr`（主机给出的地址）；`s00_axi_arready` ← `axi_sm.arready`（从机给主机的就绪）；`s00_axi_rdata` ← `axi_sm.rdata`（从机回送的数据）。印证测试台是 `s00_axi` 上的**主机**。
- `m00_axi_araddr` ← `axi_ms_m.araddr`（DUT 作为主机给出的地址）；`m00_axi_arready` ← `axi_sm_m.arready`（测试台扮演的从机给的就绪）；`m00_axi_rdata` ← `axi_sm_m.rdata`（测试台回送的数据）。印证测试台是 `m00_axi` 上的**从机**。
- 第 4 问：DUT 的主机被配置成「只读不写」（`ImplWrite_g=false`，见 [u2-l6](u2-l6-axi-master-read.md)），wrapper 实体根本没声明写通道引脚，测试台自然也不必连。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `axi_ms`/`axi_sm` 的地址是 8 位，而 `axi_ms_m`/`axi_sm_m` 是 32 位？

**参考答案**：`s00_axi` 只映射很小的寄存器空间（DUT 实例化时 `AxiSlaveAddrWidth_g => 8`，对应 256 字节，足够放下 5 个寄存器 + 16 项配置表）；而 `m00_axi` 要去读外部寄存器，外部地址空间是完整的 32 位（测试台里写的目标地址如 `0x00AB0000`），所以用 32 位。

**练习 2**：如果要让仿真跑得更快（少占机时），本测试台最现成的手段是什么？

**参考答案**：调整时钟频率。源码注释写明 `ClockFrequencyAxi_c := 125.0e6` 旁有一句 `-- Use slow clocks to speed up simulation`——这里「快」指仿真器实时，慢时钟让 `1 us`、`8 us` 这类人类友好的延时更好用。但改这个值必须同步意识到它经 `ClkFrequencyHz` 透传给 DUT、会影响超时周期换算（见 4.3 超时用例）。

---

### 4.2 双进程模型与 StimCase / RespCase 握手

#### 4.2.1 概念说明

测试台的核心难点是**并发协调**：`p_control` 发激励（写配置、触发、收结果），`p_spi` 扮演 `m00_axi` 从机（DUT 读到谁、就回送什么数据）。DUT 的读周期一旦启动，两个进程必须**同时**工作——一边 DUT 发 AR、另一边立刻回 R，读回值再流到输出被 `p_control` 核对。这做不到「先全部激励、再全部响应」的串行方式。

作者用了一个常见技巧：把测试切成 6 个用例，用两个**整数信号**当「信箱」做进程间同步：

- `StimCase`：`p_control` → `p_spi`，表示「我现在开始第 N 个用例」。
- `RespCase`：`p_spi` → `p_control`，表示「我这个用例的响应已经回送完毕」。

> **源码阅读小提示**：`p_spi` 这个名字（注释写作 `SPI Emulation`）是历史遗留——它实际扮演的是 `m00_axi` 一侧的 **AXI 从机**，没有任何 SPI 时序。而且进程头声明了一批 SPI 相关变量却从未使用（见下方源码点），显然是从一个 SPI 设备测试模板改写来的。读源码时把 `p_spi` 在脑子里改写成 `p_axi_subordinate_emu` 就不会误解。

#### 4.2.2 核心流程

每个用例的固定节拍如下（以用例 N 为例）：

```text
p_control:                               p_spi:
  StimCase <= N;                           wait until rising_edge(aclk) and StimCase = N;
  ... 发激励（PulseSig(Trig) 等）...        for ... loop
  CheckResults(...)    -- 收输出              axi_expect_ar(...)      -- 消费 AR 并断言
  wait until RespCase = N; -- 屏障            axi_apply_rresp_single  -- 回送 R
                                           end loop;
                                           RespCase <= N;
  StimCase <= N+1;   -- 进入下一用例
```

关键点：

1. **用例内并发**：`StimCase <= N` 之后，两个进程在同一用例里并行跑——`p_spi` 边收 AR 边回 R，`p_control` 边触发边用 `CheckResults` 收输出，这正是真实数据通路的样子。
2. **用例间屏障**：`p_control` 在每个用例结尾 `wait until rising_edge(aclk) and RespCase = N`，等 `p_spi` 把该用例的所有响应回送完（`RespCase <= N`）才进入下一用例，保证用例之间互不串扰。
3. **初始值兜底**：两个信号初值都是 `-1`，`wait ... and StimCase = N`（N≥1）在 `p_control` 设置前不会误触发。
4. **`axi_expect_ar` 不只是等地址**：它会**断言**地址与 burst 属性必须匹配，所以 `p_spi` 同时承担「回数据」与「校验 DUT 发出的读事务合不合规」两件事，一旦 DUT 读错地址就报 `###ERROR###`。

#### 4.2.3 源码精读

握手信号的声明（初值 `-1`，避免上电误匹配）：

[tb/top_tb.vhd:74-78](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L74-L78) —— `StimCase` 与 `RespCase` 两个整数信号，初值 `-1`（一个不属于任何用例 1..6 的哨兵值）。

`p_control` 侧的典型用例骨架（用例 1：设 `StimCase<=1`、触发、核对、等 `RespCase=1`）：

[tb/top_tb.vhd:271-277](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L271-L277) —— `p_control` 在用例 1 里：通知 `p_spi`、触发 `Trig`、调用 `CheckResults` 收输出，最后 `wait until ... RespCase = 1` 作为用例间屏障。

`p_spi` 侧的对应骨架（等 `StimCase=1`、回送 14 笔读响应、置 `RespCase<=1`）：

[tb/top_tb.vhd:412-418](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L412-L418) —— `p_spi` 在用例 1 里：`wait until ... StimCase = 1` 后循环 14 次「期望 AR + 回送 R」，最后 `RespCase <= 1` 释放屏障。

`p_spi` 进程头那些**声明却从未使用**的 SPI 变量（模板残留，佐证命名来源）：

[tb/top_tb.vhd:401-407](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L401-L407) —— `TransWidth_c`/`SpiCPHA_c`/`SpiCPOL_c`/`LsbFirst_c`/`ShiftRegRx_v`/`ShiftRegTx_v`/`ExpLatch_v` 在进程体内再无引用，属历史模板残留；进程体实际是 AXI 从机行为。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：验证「用例间屏障」的存在与作用。

**操作步骤**：

1. 在 `p_control`（[tb/top_tb.vhd:253-395](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L253-L395)）里数 `StimCase <= N` 与 `wait until ... RespCase = N` 各出现几次、编号各是多少。
2. 在 `p_spi`（[tb/top_tb.vhd:400-466](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L400-L466)）里数 `wait until ... StimCase = N` 与 `RespCase <= N` 各出现几次、编号各是多少。
3. 把两侧画成两张表，确认 1..6 一一对齐。

**预期结果**：两侧的编号集合都是 {1,2,3,4,5,6}，且每个编号在 `p_control` 里都是「先 `StimCase<=N` 后等 `RespCase=N`」，在 `p_spi` 里都是「先等 `StimCase=N` 后 `RespCase<=N`」。若将来新增用例忘了在某一边加对应的 `StimCase`/`RespCase`，进程会**死锁**（一方永远等不到编号）——这是这套握手最容易出的错。

#### 4.2.5 小练习与答案

**练习 1**：如果在一个读周期进行中又来一个 `Trig`，`p_spi` 会服务两次吗？

**参考答案**：不会。FSM 只在 `Idle_s` 消费 `Start`（见 [u2-l4](u2-l4-trigger-timeout.md)），进行中的 `Trig` 被丢弃、不排队，所以 `m00_axi` 上不会多出额外的 AR 事务。测试台用例 5（背压）正是靠「快速多次 `Trig`」来制造 FIFO 堆积，而不是靠 `p_spi` 多服务。

**练习 2**：为什么 `StimCase`/`RespCase` 用 `integer` 且初值取 `-1`？`wait until rising_edge(aclk) and StimCase = N` 里 `rising_edge(aclk)` 能不能省？

**参考答案**：用 `integer` 表达「用例编号」这种小整数，比较时直接 `StimCase = 1` 比 `std_logic_vector(to_unsigned(1, …))` 干净。初值 `-1` 是哨兵值，保证进程启动时 `wait … StimCase = N`（N≥1）不会立即通过，必须等 `p_control` 真正发出第一个编号才解除阻塞。`rising_edge(aclk)` 把求值时刻钉死在时钟上升沿，与 `p_control` 里其它等上升沿的语句在同一时刻点对齐，避免 delta cycle 里的微妙时序问题，不能省。

---

### 4.3 六组激励/响应用例

#### 4.3.1 概念说明

整个测试在 `p_control` 开头一次性配置好 RegTable（14 个目标地址 `0x00AB0000 + 16*i`、`RegCnt=14`、使能），然后用 6 个用例分别打 IP 的不同行为面。每个用例都遵循 4.2 的握手节拍。

数据值遵循一个统一的「绕一圈」约定：`p_control` 写进 RegTable 的是**地址**（`0x00AB0000+16*i`），DUT 去读这些地址，`p_spi` 回送的**数据**是 `i`（或 `i+x*32`），`CheckResults` 校验的也是 `i`。三者通过同一下标 `i` 串起来，任何一环错了都能被 `axi_expect_ar` 或 `CheckResults` 抓到。

下表是总览（除非另注，每个用例在 AXIS 与 AXIMM 两种模式下各跑一次）：

| # | 用例名 | p_control 关键动作 | p_spi 关键动作 | 验证的 IP 行为 |
| --- | --- | --- | --- | --- |
| 1 | 单次读 (Trigger Single Read) | 触发一次 | 14 笔：值 = `i` | 基本「触发→读 14 个→输出」主链路 |
| 2 | 缓冲双读 (Buffered Double Read) | 1 µs 内连续触发两次、期间不取数 | 28 笔：第一包 `i`、第二包 `32+i` | **缓冲**：两包同时存在 FIFO 不丢数据 |
| 3 | 超时 (Timeout) | 不触发、等超时 | 14 笔：值 = `i` | 仅靠超时自动启动读周期 |
| 4 | 禁用 (Disabled) | `Ctrl=0` 后触发 | 无（不服务） | `Enable` 门控：禁用时连超时也不读 |
| 5 | 背压 (Back Pressure) | 1 µs 间隔触发 6 次 | 循环服务直到 10 µs 无 AR | FIFO 背压下仍完整交付所有包 |
| 6 | 单寄存器四次读 (Single Reg Four Times) | `RegCnt=1`，触发 4 次 | 4 笔：值 = `i` | 单寄存器包（`Last` 恒为 1）+ 重复触发 |

#### 4.3.2 核心流程（按用例挑重点）

- **公共配置（用例开始前）**：写 `RegCnt=14`、写 14 项 `Addr[]`（`0x00AB0000+16*i`）、写 `Ctrl=1` 使能。这段决定了后续 `p_spi` 收到的 AR 地址必须是 `0x00AB0000+16*i`。
- **用例 2 缓冲双读（本讲重点，见 4.3.4 实践）**：连续触发两次但**先不取数**，断言 `Level = 14*2 = 28`（两包都在 FIFO 里），再依次核对两包。
- **用例 3 超时**：DUT generic `TimeoutUs_g=10`、`ClkFrequencyHz=125e6`，故超时阈值换算为
  \[ T_{\text{cycles}} = \left\lfloor \frac{f_{\text{clk}} \cdot T_{\text{us}}}{10^{6}} \right\rfloor = \left\lfloor \frac{125\times10^{6} \times 10}{10^{6}} \right\rfloor = 1250 \;\text{拍} \]
  按 8 ns/拍即 \(1250 \times 8\,\text{ns} = 10\,\mu\text{s}\)。AXIS 模式下 `CheckNoActivity(m_axis_tvalid, 8 us, 0)` 先确认 8 µs 内毫无输出，再 `WaitForValueStdl(m_axis_tvalid, '1', 3 us, …)` 在剩余窗口里等到超时触发的输出；AXIMM 模式下则轮询 `Level` 直到 `>0`。这与 [u2-l4](u2-l4-trigger-timeout.md) 讲的超时换算公式完全一致。
- **用例 4 禁用**：写 `Ctrl=0`、触发、`CheckNoActivity(m_axis_tvalid, 12 us, 0)`——禁用后即使过了超时阈值（~10 µs < 12 µs）也不应有任何读；再写 `Ctrl=1` 后又 `CheckNoActivity 2 us`（刚使能、还没到下一个超时点，也不应有输出）。这验证了「禁用时硬拉 FSM 回 Idle、冻结超时计数器」。
- **用例 5 背压**：`p_spi` 这一侧用了带超时的循环 `wait until axi_ms_m.arvalid='1' … for 10 us`，循环服务完整一包（14 笔），直到 DUT 不再发 AR（说明 FIFO 排空、剩余包已交完）。`p_control` 侧先把已就绪的包逐个核对干净，最后断言 FIFO 被清空。
- **用例 6 单寄存器四次读**：改 `RegCnt=1`，触发 4 次，期望 4 个**长度为 1** 的包（每包 `Last=1`），验证「单寄存器 + 多次触发」的边界。

#### 4.3.3 源码精读

公共配置（写 RegCnt、写 14 项 Addr、使能）：

[tb/top_tb.vhd:264-269](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L264-L269) —— 一次写 `RegCnt=14`、循环写 14 项 `Addr[i] = 0x00AB0000+16*i`、写 `Ctrl=1` 使能；这段定义了全部后续用例的目标地址表。

用例 2（缓冲双读）在 `p_control` 的完整段落：

[tb/top_tb.vhd:279-291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291) —— 先断言 `Level=0`、连续两次 `PulseSig(Trig)`（中间不取数）、断言 `Level=14*2=28`、再依次核对第一包 `(0,1)` 与第二包 `(32,1)`，最后 `wait … RespCase = 2`。

用例 2 在 `p_spi` 的对应段落（回送两包）：

[tb/top_tb.vhd:420-428](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L420-L428) —— 双重循环 `x in 0..1` × `i in 0..13`，对每个 AR 期望地址 `0x00AB0000+16*i`、回送数据 `i + x*32`（第一包 0..13、第二包 32..45）。

用例 4（禁用）在 `p_spi` 侧「什么都不期望」：

[tb/top_tb.vhd:438-440](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L438-L440) —— `p_spi` 在用例 4 直接 `RespCase <= 4`、不期望任何 AR；这本身就是断言——若 DUT 在禁用态错误发读，多余的 `arvalid` 会挂住没人应答，在后续用例暴露为时序错乱。

用例 5（背压）在 `p_spi` 的循环服务：

[tb/top_tb.vhd:442-454](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L442-L454) —— `wait until arvalid … for 10 us`，若 10 µs 内没有 AR 则 `exit`，否则服务完整一包（14 笔）；循环直到 DUT 停止发 AR。

#### 4.3.4 代码实践（本讲指定实践：缓冲双读）

**实践目标**：逐步说明用例 2「缓冲双读」中 `p_control` 与 `p_spi` 如何通过 `StimCase`/`RespCase` 配合，并指出它验证了 IP 的什么行为。

**操作步骤（按拍阅读以下两段代码，左右对照）**：

1. 读 [tb/top_tb.vhd:279-291](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L279-L291)（`p_control` 侧）。按顺序记录每条语句的**意图**：
   - `StimCase <= 2`：通知 `p_spi`「进入用例 2」。
   - `axi_single_expect(Level*4, 0, …)`：触发前 FIFO 应为空。
   - 两次 `PulseSig(Trig)`（中间 `ClockedWaitTime(1 us)`）：**关键**——两次触发之间 `p_control` **不去取数**，让第一包留在 FIFO 里。
   - `axi_single_expect(Level*4, 14*2, …)`：断言两包都已落进 FIFO（`28 ≤ 32`，未溢出）。
   - 两次 `CheckResults(0,1,…)` 与 `CheckResults(32,1,…)`：按序取走第一包（0..13）与第二包（32..45）。
   - `wait … RespCase = 2`：屏障。
2. 读 [tb/top_tb.vhd:420-428](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L420-L428)（`p_spi` 侧）。确认它回送的是「两包、各 14 笔」，地址依次为 `0x00AB0000+16*i`，数据依次为 `i` 与 `32+i`。
3. 把两边对上：`p_control` 触发 → DUT 经 `m00_axi` 发 AR → `p_spi` 回 R → 数据落 FIFO → `p_control` 经 `CheckResults` 取走。`StimCase=2` 启动这一切，`RespCase=2` 收尾。

**需要观察的现象 / 预期结果**：

- 两次触发后 `Level` 应为 **28**（不是 14），证明第一包没被覆盖、FIFO 真的在**缓冲**。
- 取数时第一包是 `0,1,…,13`、第二包是 `32,33,…,45`，顺序与边界（`Last` 在第 14 项为 1）都正确，证明**包边界不丢失、数据不串包**。

**结论（这个用例验证了什么）**：它验证了 IP 的**缓冲能力**——在下游（AXIS 的 `tready` 或 AXIMM 的软件读取）暂时不取数时，连续多个完整读周期的数据能逐包堆叠在 FIFO 中（深度 \(16 \times 2 = 32\)，两包 28 项刚好放得下）而不丢失、不乱序，且每包的末尾标记 `Last` 都正确。这正是 `MinBuffers_g` 这个 generic 的意义。

> 说明：上述「现象」是据源码断言推断的预期结果。若要在本地实际观察波形，请按 [u1-l3 如何运行仿真](u1-l3-running-simulation.md) 用 PsiSim 跑 `top_tb`，并在用例 2 区间观察 `Level` 与 `m_axis_tdata`/`RdData`，运行结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：用例 2 里 `axi_single_expect(Level*4, 14*2, …)` 的 `14*2` 如果改成 `14`，会怎样？

**参考答案**：会触发 `###ERROR###` 并终止仿真（CI 据此判失败）。因为两次触发都已落 FIFO，实际 `Level` 是 28；期望 14 与实际 28 不符，`psi_tb` 的比较过程会报错。

**练习 2**：用例 4（禁用）为什么 `CheckNoActivity` 的时长选 12 µs（大于 ~10 µs 的超时阈值）？

**参考答案**：为了**覆盖一个完整的超时周期**。禁用时超时计数器被冻结、不会自动触发读，所以即便等过了原本会触发的 ~10 µs 也不应有输出；用 12 µs（>10 µs）才能确证「连超时都被禁用门控住了」，而不是「只是还没到超时点」。

---

### 4.4 双模式校验过程：CheckResults 的两条路径

#### 4.4.1 概念说明

同一个测试台要在 AXIS 与 AXIMM 两种输出模式下都能核对结果，但两种模式的「取数方式」完全不同：

- **AXIS**：数据从 `m_axis` 端口流出（`tvalid`/`tready`/`tlast`/`tdata`），直接在端口上抓。
- **AXIMM**：数据映射到 `RdData`/`RdLast` 寄存器，要经 `s00_axi` 读出来，且必须**先读 `RdLast`（peek）再读 `RdData`（pop）**（见 [u2-l2](u2-l2-register-map.md)、[u2-l7](u2-l7-output-modes-fifo.md)）。

作者把「取数 + 比对」封装成三个 **VHDL 过程（procedure）**，用一个**分派器**按 `OutputType_g` 二选一。过程（而非进程）的好处是它在 `p_control` 进程内**顺序**执行，自然串在「触发 → 校验 → 等对方完成」的链条里，共享 `p_control` 的上下文与 `aclk`。

#### 4.4.2 核心流程

`CheckResults(start, step, …)` 是分派器：

```text
if OutputType_g = "AXIS"  → CheckResultsAxiS(...)   盯 m_axis 端口
else                       → CheckResultsAxiMM(...)  读 RdData/RdLast/Level 寄存器
```

两条路径都收 14 项、用 `start + i*step` 作为期望值、在第 14 项（`i=13`）期望 `Last=1`，但取数通道不同：

| 维度 | CheckResultsAxiS | CheckResultsAxiMM |
| --- | --- | --- |
| 取数通道 | `m_axis_tvalid/data/last` 信号 | `s00_axi` 读 `RdData`/`RdLast`/`Level` 寄存器 |
| 节奏控制 | 测试台拉 `m_axis_tready='1'` 主动取 | 轮询 `Level>0` 后逐字读 `RdData` |
| Last 判定 | 直接看 `m_axis_tlast` 信号 | 读 `RdLast` 寄存器（peek，不弹） |
| 每字比较 | `StdlvCompareInt(start+i*step, data, …)` | `axi_single_expect(RdData*4, start+i*step, …)` |

`start` / `step` 两个参数让同一过程能复用于不同用例：用例 1 用 `(0, 1)` 得 `0..13`；用例 2 第二包用 `(32, 1)` 得 `32..45`。两路径都硬编码 `for i in 0 to 13`，与公共配置的 `RegCnt=14` 一致；用例 6 把 `RegCnt` 改成 1 后，就不再走 `CheckResults` 而是手写 4 次校验。

#### 4.4.3 源码精读

`CheckResultsAxiS`（AXIS 路径，直接读端口）：

[tb/top_tb.vhd:96-111](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L96-L111) —— 拉高 `rdy`，循环 14 次，在 `vld='1'` 时比较 `data = start+i*step`、`last = choose(i=13,1,0)`（`choose` 是三元函数：`choose(条件, 真值, 假值)`）。

`CheckResultsAxiMM`（AXIMM 路径，经 `s00_axi` 读寄存器，注意读序）：

[tb/top_tb.vhd:113-133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133) —— 每项先轮询 `Level>0`，再先 `axi_single_expect(RdLast,…)` 后 `axi_single_expect(RdData,…)`，体现「先 peek `RdLast`、再 pop `RdData`」。

分派器 `CheckResults`：

[tb/top_tb.vhd:135-150](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L135-L150) —— 按 `OutputType_g` 在两条校验路径间二选一；`p_control` 里所有用例都只调 `CheckResults`，模式差异被封装在这里。

地址常量来自 RTL 包，确保测试台与 DUT 用同一张地图：

[hdl/definitions_pkg.vhd:25-35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L35) —— `RegIdx_Level_c=4`、`RegIdx_RdLast_c=3`、`RegIdx_RdData_c=2`，测试台里 `*4` 换算成字节地址即 `Level@0x10`、`RdLast@0x0C`、`RdData@0x08`，与 [u2-l2](u2-l2-register-map.md) 文档表完全吻合。

#### 4.4.4 代码实践（源码阅读型）

**实践目标**：亲眼看两条路径对「同一组期望数据」的两种取法。

**操作步骤**：

1. 打开 [tb/top_tb.vhd:96-111](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L96-L111)（AXIS）与 [tb/top_tb.vhd:113-133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L113-L133)（AXIMM）。
2. 假设调用是 `CheckResults(0, 1, …)`，分别写出两条路径在第 `i=5` 拍期望的数据值与 `Last` 值。
3. 在 AXIMM 路径里，如果把「先 `RdLast` 后 `RdData`」改成「先 `RdData` 后 `RdLast`」，描述会发生什么。

**预期结果**：

- `i=5` 时数据 = `start + i*step = 0 + 5*1 = 5`；`Last = choose(5=13, 1, 0) = 0`（非末拍）。两条路径期望完全相同。
- 颠倒顺序后：先读 `RdData` 会把 FIFO 弹出当前字，紧接着读 `RdLast` 看到的已经是**下一个**字的末值标志；于是从第二拍起 `Last` 与 `Data` 错位，校验大概率报错。这正是约定「必须先 `RdLast` 后 `RdData`」的原因。

#### 4.4.5 小练习与答案

**练习 1**：`CheckResults` 为什么写成过程（`procedure`）而不是独立进程（`process`）？

**参考答案**：因为校验是 `p_control` 用例流程里**顺序的一环**——做完校验才能进入下一动作。写成过程，它就在调用方进程内按顺序执行，自然串在「触发 → 校验 → 等对方完成」的链条里；若写成独立进程，还得再引入一对类似 `StimCase`/`RespCase` 的信号去同步「开始校验/校验完成」，徒增复杂度。

**练习 2**：AXIS 路径里 `rdy <= '1'` 在循环之前一次性置位、循环结束才 `rdy <= '0'`，即「全速取数」。如果改成「每收到一个字就插一拍 `rdy='0'`」，会触发 IP 的什么行为？

**参考答案**：会触发背压。`m_axis_tready`（即 `AxiS_Rdy`）拉低后，核心 FIFO 停止出队（[u2-l7](u2-l7-output-modes-fifo.md)），FIFO 水位上涨；当 FIFO 满时 `Fifo_Rdy` 回灌成 `AxiM_RdDat_Rdy=0`，反过来背压 `m00_axi` 主机（[u2-l6](u2-l6-axi-master-read.md)）。用例 5 是用「触发过快」而非「取数过慢」来制造同类背压。

---

### 4.5 在 PsiSim 中跑两次：config.tcl 的双 generic

#### 4.5.1 概念说明

「双模式」并不是测试台自己跑两遍循环，而是**仿真框架**把同一个 `top_tb` 以两个不同的 generic 各启动一次。这件事在 `sim/config.tcl` 里完成，是连接「测试台代码」与「CI 流程」（见 [u1-l3](u1-l3-running-simulation.md)）的关键一环：`OutputType_g` 这一个 generic 同时驱动了「硬件综合分支」（DUT 的 `g_axis`/`g_naxis` generate 块）与「测试校验分支」（`CheckResults` 分派）两处选择，是贯穿本项目的「模式开关」。

#### 4.5.2 核心流程与源码精读

`config.tcl` 在声明完所有源文件（`-tag lib`/`src`/`tb`）后，创建一次 `top_tb` 的运行，并用 `tb_run_add_arguments` 同时给出两个 generic 取值：

[sim/config.tcl:53-55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L53-L55) —— 为 `top_tb` 创建一次运行并附加 `-gOutputType_g=AXIS` 与 `-gOutputType_g=AXIMM` 两个 generic，框架据此展开成两次独立仿真（6 用例 × 2 模式 = 12 个场景）。

源文件分组（`psi_common`/`psi_tb` 标 `-tag lib`、本项目 `hdl` 三件标 `-tag src`、`top_tb` 标 `-tag tb`）：

[sim/config.tcl:41-50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L41-L50) —— 项目 RTL 与测试台的加入。其中 `psi_tb_axi_pkg`（[sim/config.tcl:33-38](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L33-L38)）就是本讲所有 BFM 过程的来源；`psi_tb_compare_pkg` 提供 `StdlvCompareInt`/`StdlCompare`；`psi_tb_activity_pkg` 提供 `CheckNoActivity`/`WaitForValueStdl`/`ClockedWaitTime`/`PulseSig`。

> 结论：新增一个测试台文件只需改 `config.tcl` 一处（加一行 `add_sources` 与一条 `create_tb_run`），流程脚本 `run.tcl` 不必动——与 [u1-l3](u1-l3-running-simulation.md) 的结论一致。但如果只是给**现有** `top_tb` 加用例（本讲最常见的改动），则连 `config.tcl` 都不用改，因为 `top_tb.vhd` 已经在编译列表里。

#### 4.5.3 代码实践（源码阅读型）

**实践目标**：理解「同一测试台、两次仿真」是配置出来的，不是测试台内部循环。

**操作步骤**：

1. 读 [sim/config.tcl:53-55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L53-L55)，确认 `tb_run_add_arguments` 给出了两个 `-gOutputType_g=…`。
2. 回到 [tb/top_tb.vhd:27-31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L27-L31)，确认 `OutputType_g` 默认值是 `"AXIMM"`，但被 `config.tcl` 显式覆盖（一次 AXIS、一次 AXIMM）。
3. 思考：如果新增第三种输出模式（假设叫 `"AXILITE"`），需要在 `config.tcl` 加一行 `-gOutputType_g=AXILITE`，并在 `CheckResults` 分派器里加一支分支——而 `p_control`/`p_spi` 的用例骨架无需改动。

**预期结果**：`OutputType_g` 这一个 generic 同时驱动「硬件综合分支」与「测试校验分支」两处选择，是贯穿本项目的「模式开关」。

> 若要本地验证两次仿真的展开，请按 [u1-l3](u1-l3-running-simulation.md) 运行 `source run.tcl`（Modelsim）或 `runGhdl.tcl`（GHDL），观察 transcript 中 `top_tb` 是否被运行两次；运行结果**待本地验证**。

#### 4.5.4 小练习与答案

**练习 1**：如果把 `config.tcl` 第 54-55 行删掉、只保留 `create_tb_run "top_tb"`，会发生什么？

**参考答案**：`top_tb` 仍会跑一次，但 `OutputType_g` 取实体默认值 `"AXIMM"`，于是**只覆盖 AXIMM 一种模式**，AXIS 路径（`CheckResultsAxiS`、`g_axis` generate 块）完全得不到回归覆盖——CI 通过但漏掉了半数场景。

**练习 2**：为什么 `OutputType_g` 选作字符串类型而不是布尔？

**参考答案**：因为可读性——`"AXIS"`/`"AXIMM"` 直接表意，且天然支持将来扩展第三种模式（只需加字符串分支）；同时它在 `config.tcl` 里作为 `-gOutputType_g=…` 传入也很直观。

---

## 5. 综合实践

**任务**：为 `top_tb` 「设计」一个新用例，并把它接入现有的握手框架（**只在草稿上写，不要改动仓库源码**）。

假设你想新增一个用例 **「改变 `RegCnt` 后再触发」**——验证「软件在两次读周期之间改 `RegCnt`，IP 能按新个数读取」（注意：按 [u2-l3](u2-l3-core-fsm.md)/[u2-l4](u2-l4-trigger-timeout.md) 的约定，FSM 只在 `Idle_s` 采样 `RegCount`，所以改 `RegCnt` 前应确保 IP 处于空闲）。

请完成以下设计：

1. **在 `p_control` 里插入一段新用例（假设编号 7）**，按 4.2 的节拍写出伪代码，要点包括：
   - `StimCase <= 7;`
   - 用 `axi_single_write(RegIdx_RegCnt_c*4, …)` 改成新个数（例如 5）；
   - `PulseSig(Trig, aclk)`；`CheckResults(…)` 核对 5 项（注意 `CheckResults` 内部固定 14 项，所以这里要么沿用用例 6 的「手写校验」写法、要么给 `CheckResults` 增加一个表示字数的参数——请说明你选哪种）；
   - 结尾 `wait until rising_edge(aclk) and RespCase = 7;`
2. **在 `p_spi` 里插入对应的编号 7 段**：`wait until rising_edge(aclk) and StimCase = 7;` 后循环新个数次 `axi_expect_ar(...)` + `axi_apply_rresp_single(...)`；结尾 `RespCase <= 7;`
3. **决定是否要改 `sim/config.tcl`**：结论是**不用改**——新用例写在 `top_tb` 内部，两种模式各跑一次是 `config.tcl` 既有的两个 generic 自动展开的（见 4.5）。
4. **指出风险点**：改 `RegCnt` 必须在 IP 空闲时进行，否则新个数可能不生效；你的用例应保证两次触发之间 IP 已完成上一包。

**预期结果**：你能用本讲学到的「`StimCase`/`RespCase` 握手 + `CheckResults` 分派 + `config.tcl` 双 generic」三件套，在不破坏现有 6 个用例的前提下，为 IP 增加一条新的回归覆盖。运行验证**待本地验证**（需 PsiSim/Modelsim/GHDL 环境）。

## 6. 本讲小结

- `top_tb.vhd` 是一份**自校验**测试台：在 DUT 的三类对外接口上各挂替身——`s00_axi` 上挂 AXI 主机 BFM（`axi_ms`/`axi_sm`，扮演软件配置侧），`m00_axi` 上挂 AXI 从机 BFM（`axi_ms_m`/`axi_sm_m`，扮演被读设备），`m_axis` 由测试台直接驱动/采样。
- 两个并发进程 `p_control`（激励与校验）与 `p_spi`（扮演 `m00_axi` 从机回送数据，名字是历史遗留、与 SPI 无关）用一对整数信号 `StimCase`/`RespCase`（初值 `-1`）做**阻塞握手**：主侧置 `StimCase<=N` 唤醒从侧、从侧置 `RespCase<=N` 通知完成，把 6 个用例串成严格顺序。
- `CheckResults` 是个 VHDL **过程**，按 generic `OutputType_g` 在 AXIS（盯 `m_axis` 端口、`StdlvCompareInt` 比较）与 AXIMM（读 `Level` 轮询、先读 `RdLast` peek 再读 `RdData` pop）两条校验路径间分发；两路径对同一组期望值 `start+i*step` 与末拍 `Last` 做相同的断言。
- 6 个用例分别覆盖：普通单次读（1）、FIFO 多包缓冲与不丢数据（2）、纯超时周期性读取（3）、禁用时忽略触发并冻结超时（4）、触发过快下的背压与数据完整（5）、单寄存器读与 1 字包的 `Last`（6）；每个用例的「RegTable 地址 ↔ `p_spi` 回送数据 ↔ `CheckResults` 期望」通过同一下标 `i` 闭环。
- 双模式回归由 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L53-L55) 的 `create_tb_run` + `tb_run_add_arguments "-gOutputType_g=AXIS" "-gOutputType_g=AXIMM"` 实现，让同一 `top_tb` 跑两遍；BFM 与比较函数全部来自 `psi_tb` 库。
- 失败即报错：所有期望/比较函数在不符时向 transcript 打印 `###ERROR###`，被 [u1-l3](u1-l3-running-simulation.md) 讲的 `run_check_errors "###ERROR###"` 捕获而让 CI 失败——所以这份测试台是 IP 行为正确性的「自动守门员」。

## 7. 下一步学习建议

- **u3-l1 C 软件驱动**：对照本讲 `CheckResultsAxiMM` 里「先 `RdLast` 后 `RdData`」的读序，看 C 驱动 `ReadFifoPacket` 如何在嵌入式侧落实同一契约。
- **u3-l3 参数化与 GUI 配置**：本讲 DUT 实例化时手填的 `MaxRegCount_g=16`、`MinBuffers_g=2`、`TimeoutUs_g=10`、`Output_g` 等参数，在真实交付时是通过 Vivado GUI 配置的；下一讲讲这些 generic 如何在 GUI 里暴露、如何映射到 RTL，与本讲「`MinBuffers_g` 决定 FIFO 深度 → 影响缓冲双读与背压用例」直接呼应。
- **u3-l5 二次开发实践：扩展该 IP**：当你给 IP 新增一个寄存器或一种行为，本讲的 `top_tb` 是必须同步更新的地方之一——你需要为它新增一个用例（`StimCase`/`RespCase` 编号顺延到 7），并复用 `CheckResults` 或手写校验。
- **深读 `psi_tb` 库**：本讲只把 `axi_single_write`/`axi_expect_ar` 等当黑盒用。若想理解 BFM 内部如何把一次调用拆成 AR/W/R/B 通道的逐拍翻转，可去 `psi_tb` 仓库读 `psi_tb_axi_pkg.vhd` 的过程体——这是写出更复杂自校验测试台的进阶基础。
- **回看 u2-l3 / u2-l4 / u2-l7**：本讲的用例 3（超时）、4（禁用）、5（背压）分别是这三讲 FSM、触发/超时、FIFO 存储机制的「行为对照实验」；若对某个用例的预期现象有疑问，回到对应 RTL 讲义核对 FSM 状态与信号是最快的路径。
