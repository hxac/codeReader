# 扫描引擎：Sweep.vhd 与 Synchronizer

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐状态讲清 `Sweep.vhd` 这个十状态有限状态机如何自主完成一次多点、多阶段扫描：何时重载 PLL、何时打开激励、何时触发采样、何时上报新数据。
2. 说清「点循环」与「阶段循环」两条回路在哪里分叉，以及为什么阶段切换不用重载 PLL、点切换必须重载。
3. 解释频率切换后必须等待的两层原因（PLL 寻锁 + 模拟环路稳定），并指出等待行为在代码里的三个具体落点。
4. 理解亚稳态与双触发器同步器的原理，能逐拍推演 `Synchronizer.vhd` 的移位行为，说出 top.vhd 中 8 个同步器实例各自防的是什么异步源。
5. 依据代码参数（102.4 MHz 时钟、MAX2871 的 CLK_DIV=6）手算一次 PLL 寄存器重载的耗时，并能参照 `Test_PLL.vhd` 的骨架为 Sweep 设计一个 testbench。

## 2. 前置知识

本讲是手册第一批进入 FPGA 内部数据通路的讲义（单元 6），假定你已完成 u6-l1，知道 `top.vhd` 是顶层、SweepConfigMem 是一块双口 RAM、MCU 通过 SPI 命令口（SPICommands）配置 FPGA。此外还需要几个本讲要用的基础概念：

- **有限状态机（FSM）与 VHDL 的写法**：把系统行为拆成有限个「状态」，每个时钟沿根据当前状态和输入跳到下一状态，同时驱动输出。VHDL 里的典型写法是：`type ... is (状态A, 状态B, ...)` 定义状态枚举，然后一个 `process(CLK)` 内部 `case state` 逐状态描述行为。所有输出都在 `rising_edge(CLK)` 内赋值，即「寄存器输出」，这样输出不会出现组合逻辑毛刺。
- **时钟域与异步信号**：FPGA 内所有逻辑由 `clk_pll`（本设计中为 102.4 MHz，见后文证据）驱动，这是一个「时钟域」。但有些信号来自域外：MCU 的 GPIO、PLL 芯片的锁定检测引脚、另一台设备的触发线。它们的变化时刻与本时钟沿没有固定关系，称为**异步输入**。
- **亚稳态（metastability）**：一个触发器如果在时钟沿恰好落在输入信号的建立/保持窗口内采样，输出可能在一段时间内既非 0 也非 1（悬在中间电平），之后随机倒向某一边。若这个「坏值」一路扩散进状态机，行为就不可预测。
- **同步器（synchronizer）**：把异步信号先打进一串（通常两级以上）背靠背的触发器再使用。第一级可能亚稳，但极大概率在一个周期内自行收敛，第二级采到的是已收敛的电平。代价只是几拍延迟，收益是失效概率指数级下降。
- **PLL 为什么需要锁定时间**：锁相环换频后，鉴相器发现参考与反馈失配，经电荷泵、环路滤波器逐渐把 VCO 拉到新频率，这个过程需要微秒级时间；即便频率「数字上」到位，模拟通路（放大器、滤波器、电桥）达到新稳态还要额外时间。在稳定之前采样，测到的 S 参数带有系统性误差。
- **S 参数测量的「阶段（stage）」**：双端口 VNA 测一 个频点需要分别激励端口 1、端口 2 各测一次。同一频率下的这两次测量叫两个阶段；频率推进才叫换点。这个概念在 u4-l2（VNADatapoint 的掩码编码）和 u5-l4（固件暂停点）都出现过，本讲你会看到它在 FPGA 里的硬件实现。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [FPGA/VNA/Sweep.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd) | 本讲主角：扫描状态机，逐点推进频率、逐阶段切换激励端口、指挥采样 |
| [FPGA/VNA/Synchronizer.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Synchronizer.vhd) | 通用的多级触发器同步器，全文件仅 53 行 |
| [FPGA/VNA/top.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd) | 顶层：Sweep 的实例化、8 个 Synchronizer 实例、PLL 握手信号的汇聚 |
| [FPGA/VNA/MAX2871.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd) | Sweep 的握手对端：把 4 个 32 位寄存器串行移位送进 PLL 芯片 |
| [Documentation/DeveloperInfo/FPGA_protocol.tex](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex) | 96 位 SweepConfig 的位域权威定义，可与代码位切片互查 |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) | MCU 侧对端：写扫描配置、启动/停止扫描 |
| [Software/VNA_embedded/Application/VNA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp) | 固件如何组装 settling time（用户驻留 + 设备标定的 PLL 延迟） |
| [FPGA/VNA/Test_PLL.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd) | 仓库中最简 testbench 骨架，是本讲实践的模板 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**Sweep 状态机**、**跨时钟域同步**、**握手时序估算与测试台验证思路**。

### 4.1 Sweep 状态机：FPGA 自主扫描的总指挥

#### 4.1.1 概念说明

u5-l4 已经从固件视角得出结论：VNA 扫描采用「MCU 预编程 + FPGA 自主执行」。本讲从 FPGA 侧看清这件事的全部细节。

为什么扫描要由 FPGA 而不是 MCU 推动？一次扫描是几百上千个频点的重复循环，每个点内要做「换频率 → 等 PLL 锁定 → 等信号稳定 → 开激励 → 触发三路 ADC → 等采样完成 → 标记结果有效 → 换下一个阶段/点」。这条链条里每一步的时序都是微秒级，而且三路采样与片上加窗/DFT 必须严格同步。若由 MCU 经 SPI 逐步指挥，USB/SPI 的时延抖动会直接进测量结果。所以分工是：MCU 事先把每个点的配置（频率、功率、衰减、波段、采样数）写进 SweepConfigMem 这块 RAM，然后只按一次「开始」键；剩下的循环完全由 `Sweep.vhd` 里的状态机自己走，MCU 只在需要干预时（USB 反压、暂停点）通过 halt/resume 介入。

`Sweep.vhd` 因此是整个 VNA 数据通路的「节拍器」：它不处理任何采样数据本身，但它决定所有别的模块什么时候干活。

#### 4.1.2 核心流程

状态机共有十个状态，可以分成两层循环嵌套：

```text
复位释放
   │
   ▼
WaitInitialLow ──(触发线已低/同步关闭)──► TriggerSetup ──(PLL接受重载请求)──► SettingUp
   (等触发线干净)                          (拉高RELOAD_PLL_REGS)              (等移位完成且锁定)
                                                                              │ (halt位=0 或收到resume)
                                                                              ▼
        ┌─────────────────────────────────────────────────────────────── Settling
        │                     (按阶段号开端口激励，倒数SETTLING_TIME)         │ 计数到0
        │                                                                     ▼
        │  ┌──(阶段号递增，频率不变，不重载PLL)◄── SamplingDone ◄──────── WaitTriggerLow
        │  │        (NEW_DATA发单拍脉冲)            ▲    ▲                    ▲
        │  │                                        │    │                    │
        │  │                            Exciting ───┘    │            WaitTriggerHigh
        │  │                       (等采样忙结束,         │            (等触发到/不同步,
        │  │                        锁存RESULT_INDEX)     │             拉START_SAMPLING)
        │  │                                               │                    ▲
        │  └───────────────────────────────────────────────┴────────────────────┘
        │
        └──(点号递增，换频率)── NextPoint ──(所有点完成)──► Done (停机，等MCU重新拉AUX3)
```

十个状态的职责一览：

| 状态 | 一句话职责 |
| --- | --- |
| WaitInitialLow | 等触发输入线变低，保证多机同步时大家从干净的触发线出发 |
| TriggerSetup | 拉高 `RELOAD_PLL_REGS`，等 MAX2871 模块确认「接住了」请求 |
| SettingUp | 撤销请求、装填 settling 计数器，等移位完成 **且** PLL 锁定；处理 halt 位 |
| Settling | 按当前阶段号点亮端口激励，倒数 settling 计时 |
| WaitTriggerHigh | 等触发（同步模式）或直接放行，发 `START_SAMPLING` 脉冲 |
| Exciting | 等采样模块忙完，把「阶段号+点号」锁存进 `RESULT_INDEX` |
| WaitTriggerLow | 主机降下触发线，从机等触发回落（触发菊花链回传） |
| SamplingDone | `NEW_DATA` 打一拍（MCU 据此读结果），决定走阶段循环还是点循环 |
| NextPoint | 点号 +1、阶段号清零，回 TriggerSetup 重载新频率；全部完成则进 Done |
| Done | 停机，直到 MCU 复位扫描模块 |

关键的分叉在 `SamplingDone`：**阶段循环**（同一频率、下一个激励端口）直接回 `Settling`，跳过 `TriggerSetup`——频率没变就不必重载 PLL；**点循环**（下一个频率）经 `NextPoint` 回 `TriggerSetup`——频率变了必须重载 PLL 并重新等锁定。这是理解整个状态机的钥匙。

#### 4.1.3 源码精读

**（1）端口分组：状态机看到的世界**

Sweep 的端口虽然多，但按功能分组后很清晰。[Sweep.vhd:L32-L90](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L32-L90) 定义了实体，可归为六组：

| 组 | 信号 | 方向 | 含义 |
| --- | --- | --- | --- |
| 时钟复位 | `CLK` / `RESET` | 入 | 102.4 MHz 主时钟；`RESET` 高有效（来自 MCU 的 AUX3 线，见后文） |
| 扫描规模 | `NPOINTS` / `STAGES` / `PORT1_STAGE` / `PORT2_STAGE` | 入 | 点数（存的是最后一点下标）、阶段数、两端口各自的激励阶段号 |
| 点配置 | `CONFIG_ADDRESS` / `CONFIG_DATA` / `USER_NSAMPLES` | 出/入 | 当前点的 96 位配置从 RAM 读出；用户自定义采样数 |
| 采样握手 | `START_SAMPLING` / `SAMPLING_BUSY` / `NSAMPLES` / `NEW_DATA` / `RESULT_INDEX` | 出/入/出 | 指挥 Sampling 模块并收回「结果就绪」 |
| PLL 握手 | `RELOAD_PLL_REGS` / `PLL_RELOAD_DONE` / `PLL_LOCKED`、8 个 32 位寄存器 | 出/入 | 源与一本振的寄存器拼装与重载 |
| 射频控制 | `ATTENUATOR` / `SOURCE_FILTER` / `BAND_SELECT` / `SOURCE_CE` / `PORT1_ACTIVE` / `PORT2_ACTIVE` | 出 | 该点该阶段的射频开关量 |

有一个值得注意的细节：端口里的 `SAMPLING_DONE` 在整个架构体中**从未被使用**——用 grep 全文检索只会命中实体声明那一行（[Sweep.vhd:L42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L42)）。状态机实际依靠 `SAMPLING_BUSY` 的上升与回落判断采样起止。这是一个遗留端口，读代码时不要被它误导。

**（2）寄存器拼装：96 位配置字的「解释器」**

状态机本体之前，先看一大段纯组合逻辑。[Sweep.vhd:L102](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L102) 把当前点号直接接到配置 RAM 的地址：

```vhdl
CONFIG_ADDRESS <= std_logic_vector(point_cnt);
```

于是 `config_reg <= CONFIG_DATA`（[Sweep.vhd:L154](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L154)）永远指向「当前点」的 96 位配置。接着 [Sweep.vhd:L106-L121](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L106-L121) 用位切片把这 96 位拼装成源 PLL 和 LO PLL 各四个 32 位寄存器：

```vhdl
-- source register 0: N divider and fractional division value
SOURCE_REG_0 <= MAX2871_DEF_0(31) & "000000000" & config_reg(93) & config_reg(5 downto 0) & config_reg(26 downto 15) & "000";
-- source register 1: Modulus value
SOURCE_REG_1 <= MAX2871_DEF_1(31 downto 15) & config_reg(38 downto 27) & "001";
-- LO register 0: N divider and fractional division value
LO_REG_0 <= MAX2871_DEF_0(31) & "000000000" & config_reg(94) & config_reg(54 downto 49) & config_reg(75 downto 64) & "000";
```

这段拼接逻辑与协议文档 [FPGA_protocol.tex:L558-L603](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L558-L603) 的 SweepConfig 位域图**逐位吻合**，可以互相印证。例如文档规定 96 位字的最高位（bit 95）是 HS（halt sweep）位、bit 94/93 分别是 LO 与源的 N 分频器最高位——代码里 `config_reg(95)` 正是 halt 判断（L185）、`config_reg(94)`/`config_reg(93)` 正好落在两个 REG_0 的 N[6] 位置。每个 `REG_x` 末尾的三位（`"000"`、`"001"`、`"011"`、`"100"`）是 MAX2871 的寄存器地址（0/1/3/4）。不随频率变的位（参考分频、电荷泵电流等）来自 MCU 预先写好的 `MAX2871_DEF_x` 默认寄存器，只有「随点变化」的位才占用宝贵的 96 位 RAM 空间——这是一次很值得学习的存储压缩设计。

同样在组合逻辑区，[Sweep.vhd:L123-L126](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L123-L126) 从配置字取出衰减器（0.25 dB 步进、7 位）、源滤波器波段（2 位）和波段选择位：

```vhdl
ATTENUATOR <= config_reg(45 downto 39);
SOURCE_FILTER <= config_reg(89 downto 88);
BAND_SELECT <= config_reg(48);
```

而 [Sweep.vhd:L128-L135](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L128-L135) 是一个小型查找表，把配置字里的 3 位「Samples」编码翻译成本点使用的采样数：

```vhdl
NSAMPLES <= USER_NSAMPLES when config_reg(92 downto 90) = "000" else
            std_logic_vector(to_unsigned(6, 13)) when config_reg(92 downto 90) = "001" else
            ...
            std_logic_vector(to_unsigned(5712, 13));
```

注意一个容易踩坑的单位问题：`NSAMPLES` 总线**以 16 个样本为单位**。表中数值 6/19/57/190/571/1904/5712 对应文档 [FPGA_protocol.tex:L617-L631](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L617-L631) 表格里的 96/304/912/3040/9136/30464/91392 个真实样本，正好相差 16 倍；`000` 时使用的 `USER_NSAMPLES` 也是同样单位——固件侧 [FPGA.cpp:L125-L133](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L125-L133) 写入前先 `nsamples /= 16`。这组采样数本质上是选择 IF 带宽（样本越多、DFT 积分时间越长、带宽越窄），从 10 kHz 一路到 10 Hz。

**（3）状态机逐状态精读**

状态定义在 [Sweep.vhd:L93-L95](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L93-L95)：

```vhdl
signal point_cnt : unsigned(12 downto 0);
type Point_states is (WaitInitialLow, TriggerSetup, SettingUp, Settling, WaitTriggerHigh,
                      Exciting, WaitTriggerLow, SamplingDone, NextPoint, Done);
signal state : Point_states;
```

整个状态机是一个同步复位的时钟进程（[Sweep.vhd:L156-L270](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L156-L270)），复位分支（L159-L170）把点号、阶段号清零、状态置于 `WaitInitialLow`、所有脉冲输出清零。逐状态看关键代码：

- **WaitInitialLow**（[L173-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L173-L177)）：

```vhdl
TRIGGER_OUT <= '0';
if TRIGGER_IN = '0' or SYNC_ENABLED = '0' then
    state <= TriggerSetup;
end if;
```

扫描开始前先等触发输入线已经回落。多机同步时若某台设备的触发线还悬在高电平，后面「等触发高」的逻辑会立即误触发；这一状态保证所有设备都从同一个起点出发。不用同步功能（`SYNC_ENABLED='0'`）时旁路。

- **TriggerSetup → SettingUp**（[L178-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L178-L194)）：这是与 PLL 模块的请求-确认握手的前半段：

```vhdl
when TriggerSetup =>
    RELOAD_PLL_REGS <= '1';
    if PLL_RELOAD_DONE = '0' then
        state <= SettingUp;
    end if;
when SettingUp =>
    SWEEP_HALTED <= config_reg(95);
    RELOAD_PLL_REGS <= '0';
    settling_cnt <= unsigned(SETTLING_TIME);
    if PLL_RELOAD_DONE = '1' and PLL_LOCKED = '1' then
        if config_reg(95) = '0' or SWEEP_RESUME = '1' then
            SWEEP_HALTED <= '0';
            state <= Settling;
        end if;
    end if;
```

先拉高 `RELOAD_PLL_REGS`，然后**等 `PLL_RELOAD_DONE` 变低**才撤请求——变低意味着 MAX2871 模块已经锁存了请求、开始移位（对端行为见 4.3.3），这样请求脉冲不会丢。`SettingUp` 里等 `PLL_RELOAD_DONE='1'`（移位全部完成）**且** `PLL_LOCKED='1'`（芯片锁定检测有效）双条件。这里同时是 **halt 机制**的入口：若本点配置字的 bit95（HS 位）为 1，状态机停在 `SettingUp` 不走，`SWEEP_HALTED` 输出通知 MCU，直到 MCU 经 SPI 发来 `SWEEP_RESUME`。这正是 u5-l4 讲过的「暂停点」的硬件侧：低波段换源、USB 反压都靠它实现。

- **Settling**（[L195-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L195-L220)）：按阶段号开激励并倒数：

```vhdl
NEW_DATA <= '0';
source_active <= '0';
if std_logic_vector(stage_cnt) = PORT1_STAGE then
    PORT1_ACTIVE <= '1';
    source_active <= '1';
else
    PORT1_ACTIVE <= '0';
end if;
...
if settling_cnt > 0 then
    settling_cnt <= settling_cnt - 1;
else
    state <= WaitTriggerHigh;
    if SYNC_MASTER = '1' then
        TRIGGER_OUT <= '1';   -- 主设备自己发起触发
    end if;
end if;
```

`stage_cnt` 与 `PORT1_STAGE`/`PORT2_STAGE` 的比较决定本阶段激励哪个端口；两个都不匹配的阶段里 `source_active` 保持 0，源芯片使能被关断（`SOURCE_CE`，[L126](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L126)）。倒数到零进入触发等待；若自己是同步主机，则自己把 `TRIGGER_OUT` 拉高，充当触发源。

- **WaitTriggerHigh / Exciting / WaitTriggerLow**（[Sweep.vhd:L221-L244](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L221-L244)）：这三个状态完成一次采样并维护触发菊花链：

```vhdl
when WaitTriggerHigh =>
    if TRIGGER_IN = '1' or SYNC_ENABLED = '0' then
        TRIGGER_OUT <= SYNC_ENABLED;   -- 从机把触发往下一级传
        START_SAMPLING <= '1';
        if SAMPLING_BUSY = '1' then
            state <= Exciting;
        end if;
    end if;
when Exciting =>
    START_SAMPLING <= '0';
    if SAMPLING_BUSY = '0' then
        RESULT_INDEX <= std_logic_vector(stage_cnt) & std_logic_vector(point_cnt);
        state <= WaitTriggerLow;
    end if;
when WaitTriggerLow =>
    if SYNC_MASTER = '1' then
        TRIGGER_OUT <= '0';
    end if;
    if TRIGGER_IN = '0' or SYNC_ENABLED = '0' then
        TRIGGER_OUT <= '0';
        state <= SamplingDone;
    end if;
```

各状态源码位置：[WaitTriggerHigh:L221-L228](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L221-L228)、[Exciting:L229-L235](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L229-L235)、[WaitTriggerLow:L236-L244](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L236-L244)。

触发到达后发一个 `START_SAMPLING` 脉冲，看 `SAMPLING_BUSY` 升起确认采样模块已经启动，再等它回落确认结束；结束时把 `阶段号(3位) & 点号(13位)` 共 16 位锁存进 `RESULT_INDEX`——这个索引在顶层被接进 304 位采样结果的最高 16 位（[top.vhd:L734](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L734)），与协议文档 Sampling Result 一节的 `STAGE[2:0]+POINT_NUMBER[12:0]` 布局一致，让 MCU 拿到数据的同时知道它属于哪个点哪个阶段。

触发菊花链值得多说一句：从机在 `WaitTriggerHigh` 把收到的触发转发出去（`TRIGGER_OUT <= SYNC_ENABLED`），主机在 `WaitTriggerLow` **先**降下自己的触发线，然后等 `TRIGGER_IN` 回落——这条回落沿是从机逐级传回来的。也就是说主机发起触发后要等它绕所有设备一圈回到自己才认为本轮测量同步结束，这保证每台设备看到的是同一个全局时刻。

- **SamplingDone / NextPoint**（[L245-L265](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L245-L265)）：两层循环的分叉点：

```vhdl
when SamplingDone =>
    NEW_DATA <= '1';
    if stage_cnt < unsigned(STAGES) then
        stage_cnt <= stage_cnt + 1;
        state <= Settling;              -- 阶段循环：不重载 PLL
    else
        state <= NextPoint;
    end if;
    settling_cnt <= unsigned(SETTLING_TIME);
when NextPoint =>
    NEW_DATA <= '0';
    if point_cnt < unsigned(NPOINTS) then
        point_cnt <= point_cnt + 1;
        stage_cnt <= (others => '0');
        state <= TriggerSetup;          -- 点循环：重载 PLL
    else
        point_cnt <= (others => '0');
        state <= Done;
        TRIGGER_OUT <= '0';
    end if;
```

`NEW_DATA` 只在这里打一拍，顶层把它接入 SPICommands 的 `NEW_SAMPLING_DATA`（[top.vhd:L775](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L775)）去敲 MCU 中断——这就是 u6-l1 里数据通路的「每一拍」来源。注意两个计数的语义都是「含头含尾」：阶段号取 0..STAGES 共 STAGES+1 个，点号取 0..NPOINTS 共 NPOINTS+1 个点；固件侧 [FPGA.cpp:L119-L123](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L119-L123) 写寄存器前先 `npoints--`，与此严格对应。全部点完成后进入 `Done` 死等，直到 MCU 重新触发扫描（见 4.3.3）。

**（4）调试输出：为验证而生的 DEBUG_STATUS**

[Sweep.vhd:L137-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L137-L152) 把状态机编码成 4 位二进制（TriggerSetup=0000、SettingUp=0001、Settling=0010……Done=1000），再拼上 `PLL_RELOAD_DONE`、锁定、采样忙、触发输入、源使能五个实时信号，合成 11 位 `DEBUG_STATUS`。端口声明处注释直接写着 `-- Debug signals`（[L86-L88](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L86-L88)）——作者预见到这个状态机必须在硬件上可观测。4.3 的实践会用到它。

#### 4.1.4 代码实践

**实践目标**：亲手填写「状态 × 输出」驱动表，并回答「为什么换频后要等待、代码在哪里体现」。

**操作步骤**：

1. 打开 [Sweep.vhd:L156-L270](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L156-L270) 的状态机进程，为十个状态各画一行，列出该状态下被显式赋值的输出（`RELOAD_PLL_REGS`、`START_SAMPLING`、`NEW_DATA`、`TRIGGER_OUT`、`PORT1_ACTIVE`、`PORT2_ACTIVE`、`SWEEP_HALTED`、`RESULT_INDEX`）以及「保持不变」的输出。注意 VHDL 时序进程的语义：某状态没赋值的信号保持上一拍的值。
2. 填完后自查两处易错点：`START_SAMPLING` 只在 `WaitTriggerHigh` 置 1、`Exciting` 清 0（所以是恰好覆盖采样启动窗口的脉冲）；`NEW_DATA` 在 `SamplingDone` 置 1、在 `Settling`/`WaitTriggerLow`/`NextPoint` 都有清 0 动作。
3. 回答核心问题「频率切换后为什么需要等待」，在代码里找出**三个**落点：
   - 落点一：`SettingUp` 等待 `PLL_RELOAD_DONE='1' and PLL_LOCKED='1'`（[L188](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L188)）——寄存器移位完成且芯片锁定检测有效，双条件缺一不可。
   - 落点二：`Settling` 用 `settling_cnt` 倒数 `SETTLING_TIME`（[L187](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L187) 与 [L211-L220](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L211-L220)）——即便 PLL 已报锁定，模拟环路（滤波、放大、电桥、电缆）仍需时间到新稳态。
   - 落点三：固件侧 [VNA.cpp:L179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L179) `FPGA::SetSettlingTime(s.dwell_time + HW::getPLLSettlingDelay())`——总建立时间 = 用户设置的驻留时间 + **逐台标定的 PLL 建立延迟**，后者存在 flash 的设备配置区（[Hardware.cpp:L512-L514](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L512-L514)），即 u5-l3 讲过的设备级数据：每块板子的锁定快慢不同，靠出厂/用户校准弥补。
4. 对比同一状态机里「阶段切换」路径：`SamplingDone → Settling` 不经过 `TriggerSetup`，说明阶段切换为何不需要等 PLL——频率未变，只是换了激励端口的开关。

**需要观察的现象 / 预期结果**：本实践为源码阅读型，产出两张表（状态×输出表、等待三落点清单）。若后续你在仿真中跑 4.3 的 testbench，可以用 `DEBUG_STATUS` 的状态编码核对你手填的表：波形里 0001（SettingUp）的持续时间应约等于一次 PLL 重载（约 7.7 µs，见 4.3 的估算），0010（Settling）的持续时间应约等于 SETTLING_TIME 个时钟周期。

**待本地验证**：表格内容可离线完成；仿真核对需要 ISE 仿真环境。

#### 4.1.5 小练习与答案

**练习 1**：`NPOINTS` 是 13 位。一次扫描最多多少个点？协议文档说最多 4501 点，为什么不是 8192？

**答案**：13 位计数器最多编码 8192 个值；由于点号「含头含尾」是 0..NPOINTS，纯二进制上限是 8192 个点。但 4501 的限制来自别处——配置存在 SweepConfigMem 里，每个点占 96 位，块 RAM 容量与 13 位地址共同决定可用容量；协议文档 [FPGA_protocol.tex:L140-L143](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L140-L143) 明确说「最大 4501 点、最高有效下标 4500」，即块 RAM 实际只配到 4501 项。计数字段留 13 位只是向上取整到总线位宽。

**练习 2**：如果某点的 96 位配置里 HS 位（bit 95）为 1，状态机会停在哪里？MCU 怎么让它继续？

**答案**：停在 `SettingUp`。`SWEEP_HALTED <= config_reg(95)` 先置位；只有当 `PLL_RELOAD_DONE='1' and PLL_LOCKED='1'` **且**（`config_reg(95)='0'` 或 `SWEEP_RESUME='1'`）才进入 `Settling`（[L183-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L183-L194)）。`SWEEP_HALTED` 输出经 SPICommands 反映到状态寄存器（[top.vhd:L804](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L804)），MCU 检测到后经 SPI 发 resume 命令，`SWEEP_RESUME` 生效，扫描继续。

**练习 3**：`SOURCE_CE`（源芯片使能）在什么情况下为 0？这样设计有什么好处？

**答案**：`SOURCE_CE <= source_active`，而 `source_active` 只在当前阶段号等于 `PORT1_STAGE` 或 `PORT2_STAGE` 时置 1（[L198-L209](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L198-L209)）。当一个阶段两个端口都不激励（例如某些多阶段编排中的纯参考阶段）时，源被整体关断。好处是在测量间隙切断激励信号，避免源泄漏通过电桥串进接收通道影响本底。

### 4.2 跨时钟域同步：Synchronizer 与亚稳态

#### 4.2.1 概念说明

`Sweep.vhd` 的所有判断都基于 `CLK`（102.4 MHz）采样到的输入。但它的好几个输入根本不是这个时钟域产生的：

- `TRIGGER_IN`：来自另一台 LibreVNA 的 `TRIGGER_OUT` 引脚，经导线连过来，由对方的时钟决定翻转时刻；
- `PLL_LOCKED`：源头是 MAX2871 芯片的 LD（锁定检测）引脚，本质是芯片内部模拟比较器的输出，与任何外部时钟无关；
- 控制 Sweep 复位的 AUX 线：MCU（STM32G4，时钟体系与 FPGA 无关）的 GPIO。

如果把这些信号直接接进状态机，当时钟沿恰好落在信号翻转附近，第一级触发器可能进入亚稳态——输出长时间悬在中间电平，并随机倒向 0 或 1，更糟的是可能同时被多个下游触发器解读成不同值，导致状态机不同分支不一致。

标准对策是**多级触发器同步器**：让异步信号先经过一小串只做「打拍」的触发器，第一级承担亚稳态风险，但它在一个周期内收敛的概率随时间指数上升；等信号传到第二、三级时已是确定电平，再交给状态机使用。代价是固定的几拍延迟（本设计每拍约 9.77 ns），换来的是失效概率指数级下降。需要注意它的能力边界：**同步器只能可靠传递「电平和足够宽的边沿」**——窄于一个时钟周期的脉冲可能整体漏采；它也不能保持输入事件的精确时刻（分辨率就是一个时钟周期）。

#### 4.2.2 核心流程

`Synchronizer.vhd` 是一个长度可参数化的移位寄存器。设 `stages=2`，信号向量 `sync_line` 是 3 位（`stages downto 0`），每个时钟沿执行：

\[ \texttt{sync\_line} \;\leftarrow\; \texttt{sync\_line}(1\!\sim\!0)\,\&\,\texttt{SYNC\_IN} \]

即输入从最低位进入，逐拍向高位搬移，最高位作为输出。逐拍推演（设输入在第 0 拍后变高）：

| 时钟沿 | sync_line(0) | sync_line(1) | sync_line(2)=SYNC_OUT |
| --- | --- | --- | --- |
| 第 1 拍 | 1 | 0 | 0 |
| 第 2 拍 | 1 | 1 | 0 |
| 第 3 拍 | 1 | 1 | 1 |

所以对 `stages=2`，输出比输入**晚 3 个时钟沿**（信号实际穿过 3 个触发器）。若第一级输出亚稳，它有整整两个周期的时间收敛后才被采样进输出级——这就是可靠性来源。

#### 4.2.3 源码精读

同步器本体只有 20 行有效代码。[Synchronizer.vhd:L32-L37](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Synchronizer.vhd#L32-L37) 声明实体：一个整型泛型 `stages` 和三个端口。

```vhdl
entity Synchronizer is
    Generic(stages : integer);
    Port ( CLK : in  STD_LOGIC;
           SYNC_IN : in  STD_LOGIC;
           SYNC_OUT : out STD_LOGIC);
end Synchronizer;
```

[Synchronizer.vhd:L39-L51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Synchronizer.vhd#L39-L51) 是全部逻辑：

```vhdl
signal sync_line : std_logic_vector(stages downto 0);
...
SYNC_OUT <= sync_line(stages);
process(CLK)
begin
    if rising_edge(CLK) then
        sync_line <= sync_line(stages-1 downto 0) & SYNC_IN;
    end if;
end process;
```

没有复位端——同步器无需复位，上电后最多几个周期输出就与输入一致。

它的使用全景在顶层。top.vhd 专门有一组信号注释为 `-- synchronized asynchronous inputs`（[top.vhd:L433-L439](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L433-L439)），并实例化了 **8 个** `Synchronizer`（[top.vhd:L506-L561](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L506-L561)），全部 `stages => 2`：

| 实例 | 同步的信号 | 为什么是异步的 | 同步后供给谁 |
| --- | --- | --- | --- |
| Sync_AUX1/2/3 | MCU 的三根 AUX GPIO | 由 STM32 时钟体系驱动 | SPI 复用选择、`sweep_reset`（AUX3） |
| Sync_LO_LD | LO PLL 的锁定检测引脚 | 芯片内部模拟量 | `plls_locked` → Sweep 的 `PLL_LOCKED` |
| Sync_SOURCE_LD | 源 PLL 的锁定检测引脚 | 同上 | 同上 |
| Sync_NSS | MCU 的 SPI 片选 | 由 STM32 时钟体系驱动 | SPI 从机命令口 |
| Sync_TRIGGER_IN | 外部触发输入线 | 来自另一台设备 | Sweep 的 `TRIGGER_IN` |
| Sync_TRIGGER_OUT | 本机 `sweep_trigger_out` | 已在本域（见下） | TRIGGER_OUT 引脚 |

以锁定检测为例（[top.vhd:L527-L533](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L527-L533)）：

```vhdl
Sync_LO_LD : Synchronizer
GENERIC MAP(stages => 2)
PORT MAP(
    CLK => clk_pll,
    SYNC_IN => LO1_LD,
    SYNC_OUT => lo_ld_sync
);
```

两路锁定在 [top.vhd:L594-L595](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L594-L595) 汇聚后交给 Sweep：

```vhdl
plls_reloaded <= source_reloaded and lo_reloaded;
plls_locked <= source_ld_sync and lo_ld_sync;
```

也就是说 4.1 里 Sweep 等待的 `PLL_LOCKED`，其实是「两颗 PLL 的锁定检测引脚各自经 3 拍同步后的与」。这里有一个值得品味的设计含义：锁定检测本身是慢变量（锁定要微秒级），3 拍约 29 ns 的同步延迟对它毫无影响——**同步器适合的正是这类信号**。

`Sync_TRIGGER_OUT` 是个特例：它同步的 `sweep_trigger_out` 本来就是 clk_pll 域的寄存器输出（在状态机进程里赋值），再打两拍并不改变功能，只增加 3 拍延迟并规整输出路径。可以理解为防御性/对称性写法：触发通路在本机也保持与对端一致的「过同步器」结构。真正承上启下的是 `Sync_TRIGGER_IN`（[top.vhd:L548-L554](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L548-L554)）：它把别家设备带来的异步触发沿变成 clk_pll 域的确定电平，交给 Sweep 的 `WaitTriggerHigh`/`WaitTriggerLow` 判断。这也解释了状态机里触发脉冲为什么「足够宽」：主机把 `TRIGGER_OUT` 从 settling 结束一直保持到采样完成（横跨 WaitTriggerHigh、Exciting、WaitTriggerLow 的前半段），宽度远超一个时钟周期，不会漏采。

#### 4.2.4 代码实践

**实践目标**：量化同步器的延迟与影响，确认「哪些信号必须同步、哪些不需要」。

**操作步骤**：

1. 数延迟：`clk_pll` 周期 \( T = 1/102.4\,\text{MHz} \approx 9.77\,\text{ns} \)。对 `stages=2` 的实例，按 4.2.2 的逐拍表算出 `SYNC_IN` 到 `SYNC_OUT` 的延迟是 3 拍 ≈ 29.3 ns。
2. 在 [top.vhd:L689-L735](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L689-L735) 的 SweepModule 端口映射中逐一标注每个输入的「出身」：哪些直接来自本域模块（如 `SAMPLING_BUSY` 来自同在 clk_pll 下的 Sampling），哪些必须取同步后的版本（`TRIGGER_IN`、`PLL_LOCKED`、复位）。
3. 思考题实操：若把 `Synchronizer` 的泛型改为 `stages => 1`，行为和可靠性各怎么变？（提示：向量变 2 位，信号穿 2 个触发器，延迟 2 拍；第一级亚稳后只剩 1 个周期收敛，MTBF 下降。）
4. 顺藤摸瓜找复位：`sweep_reset <= not aux3_sync`（[top.vhd:L687](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L687)）——AUX3 是 MCU 的 GPIO，先经 `Sync_AUX3` 同步再反相成 Sweep 的复位。也就是说**整台 VNA 的扫描启停开关本身就是一根被同步过的异步线**。

**需要观察的现象 / 预期结果**：这是一道推演型实践。预期你能得到一张「信号 → 出身 → 是否同步 → 供给者」的四列表，并得出结论：本域信号直接连、外域信号一律过 Synchronizer，全顶层无一例外。

**待本地验证**：延迟数值可在仿真中用波形游标确认（在 testbench 里同时观察 `SYNC_IN` 与 `SYNC_OUT`，应有恒定 3 个 clk_pll 周期相差）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `SAMPLING_BUSY` 不需要同步器，而 `PLL_LOCKED` 需要？

**答案**：`SAMPLING_BUSY` 由同在 clk_pll 域、同一个时钟驱动的 Sampling 模块产生，两者之间的数据路径是同步路径，时序由布局布线工具静态分析保证。`PLL_LOCKED` 的源头是 MAX2871 芯片的 LD 引脚，芯片内部没有与本设计对齐的时钟，翻转时刻相对 clk_pll 沿随机，必须同步。

**练习 2**：假设主机发出的触发脉冲宽度只有 5 ns（窄于一个时钟周期），从机可能发生什么？

**答案**：该脉冲可能整体落在两个采样沿之间，同步器输出端看不到任何变化，从机的 `WaitTriggerHigh` 永远等不到触发，扫描卡死。这就是同步器「不能保窄脉冲」的边界。本设计中主机把触发保持到采样完成（几百微秒量级），天然规避了这个问题。

**练习 3**：`Sync_TRIGGER_OUT` 同步的是本域信号，看起来「多余」。说出它可能的一个正面作用和一个代价。

**答案**：正面作用：保证输出到引脚的信号一定是「再打拍后」的干净寄存器输出，且让触发发送路径与接收路径结构对称（本机发出的触发与收到的触发经历相似的处理），也避免综合工具对「寄存器直连引脚」产生不一致的处理。代价：固定的 3 拍（约 29 ns）额外延迟，以及多占用 3 个触发器。对触发这种微秒级宽度的信号，29 ns 完全无感。

### 4.3 握手时序估算与测试台验证思路

#### 4.3.1 概念说明

学习目标里要求「估算一次扫描中 FPGA 与 PLL 的握手时序」。这件事的价值在于：扫描每点的总时间 = PLL 重载时间 + 建立时间 + 采样时间 + 触发往返时间，其中采样时间决定了测量速度的上限，而前三项是「 overhead（开销）」。读懂开销才能理解 GUI 里 IF 带宽、点数、settling 设置如何换算成一次扫描的墙钟时间，也才能理解为什么固件要把 `dwell_time` 和设备标定的 PLL 延迟加在一起。

同时本讲是单元 6 里第一次面对「没有现成 testbench 的模块」：仓库的 `FPGA/VNA/` 下有十个 `Test_*.vhd`，但**没有 `Test_Sweep.vhd`**——作者对扫描状态机的验证手段主要是硬件在线观测（`DEBUG_STATUS` 正是为此而设）。这并不妨碍我们用仿真验证它：仓库里现成的 testbench 骨架就是模板，照着写一个并不难。这本身就是「FPGA 验证文化」的一部分：改时序敏感的模块，先在仿真里看波形，再上硬件。

#### 4.3.2 核心流程与时序估算

先确认时钟频率的证据链（不靠猜）：协议文档 [FPGA_protocol.tex:L538-L547](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Documentation/DeveloperInfo/FPGA_protocol.tex#L538-L547) 给出

\[ t_{\text{delay}} = \frac{\texttt{SETTLING\_TIME}}{102.4\,\text{MHz}} \]

固件 [FPGA.cpp:L135-L144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L135-L144) 的换算 `value = us * 512 / 5`（即每微秒 102.4 个周期）与之互证：**clk_pll = 102.4 MHz，周期 \( T \approx 9.77\,\text{ns} \)**。

**PLL 重载一次要多久？** 看参数：MAX2871 模块例化时 `CLK_DIV => 6`（[top.vhd:L564-L593](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L564-L593)）。由 4.3.3 的移位逻辑可知 SCK 每 3 个 clk_pll 周期翻转一次（每 6 个周期一个完整 SCK 周期），每个数据位占 2 个翻转节拍（高半拍 + 低半拍），每个 32 位寄存器移完再花 2 拍打 LE 锁存脉冲。于是每寄存器 \( 32\times2+2 = 66 \) 拍，4 个寄存器共 \( 264 \) 拍，每拍 3 个 clk_pll 周期：

\[ t_{\text{reload}} = 264 \times 3 \times T = 792 \times 9.77\,\text{ns} \approx 7.7\,\mu s \]

源与 LO 两片由同一个 `RELOAD` 驱动、并行移位（[top.vhd:L594](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L594) 两个 DONE 相与），所以总重载时间就是约 7.7 µs。

**建立时间呢？** `SETTLING_TIME` 是 20 位（[Sweep.vhd:L40](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L40)），满量程

\[ t_{\text{settle,max}} = \frac{2^{20}}{102.4\,\text{MHz}} = 10.24\,\text{ms} \]

**采样时间**由本点选中的样本数与 ADC 采样节拍决定（后者由 ADC 预分频寄存器控制，属 u6-l3 的内容），本讲只给出框架：\( t_{\text{sample}} = \text{样本数} \times \text{ADC 采样周期} \)。样本数可以从 96 一直取到 91392（见 4.1.3 的查找表），所以每点总时间的数量级完全由用户选择的 IF 带宽主导，7.7 µs 的重载和典型几十微秒的 settling 只在宽带测量时占比明显。

**触发往返**（多机同步时）：从机收到触发再转发，每台设备引入 3 拍同步延迟（约 29 ns）加转发逻辑延迟，菊花链 n 台设备的总附加延迟仍是亚微秒级，可忽略。

**汇总：一次「点 × 邘段」的时间构成**

```text
换点的额外开销 = PLL 重载(≈7.7µs) + PLL 锁定等待(芯片决定,已被SETTLING_TIME覆盖)
每阶段开销    = SETTLING_TIME × 9.77ns
每阶段主体    = 样本数 × ADC采样周期
多机同步附加  ≈ n × 29ns（可忽略）
```

#### 4.3.3 源码精读

**（1）MAX2871 模块：握手对端怎么消费 RELOAD**

[MAX2871.vhd:L32-L45](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L32-L45) 声明接口：输入 4 个寄存器和 `RELOAD`，输出 SPI 三线（SCK/MOSI/LE）与 `DONE`。核心是 [MAX2871.vhd:L63-L115](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/MAX2871.vhd#L63-L115) 的三层计数状态机：

```vhdl
if done_int = '1' then
    -- can start a new reload process
    if RELOAD = '1' then
        done_int <= '0';
        latched_regs <= REG4 & REG3 & REG1 & REG0;
        ...
```

空闲（`done_int='1'）`时看到 `RELOAD` 请求，立刻锁存 128 位寄存器值并把 `DONE` 拉低——这个「拉低」正是 Sweep 的 `TriggerSetup` 在等的确认（「请求已被接住」）。随后（L81-L95）按预分节拍逐位把最高位送上 MOSI、翻转 SCK；每移完 32 位打一个 LE 锁存脉冲并推进到下一寄存器（L96-L109）；四个寄存器全部送完把 `done_int` 重新置高，Sweep 的 `SettingUp` 检测到 `PLL_RELOAD_DONE='1'` 与 `PLL_LOCKED='1'` 后才继续。

**（2）顶层：两片 PLL 的并行与信号汇聚**

[top.vhd:L564-L595](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L564-L595) 例化 `Source` 与 `LO1` 两个 MAX2871，共用 Sweep 发出的 `reload_plls`，各自输出 DONE，相与后回给 Sweep；锁定检测则取自芯片 LD 引脚的同步值。这就是 4.1 里那对握手信号在系统里的真实拓扑。

**（3）MCU 侧：扫描的「总开关」与参数装配**

固件驱动 [FPGA.cpp:L338-L346](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L338-L346) 揭示了 Sweep 复位的真身：

```cpp
void FPGA::StopSweep() {
    Low(AUX3);
}
void FPGA::StartSweep() {
    StopSweep();
    Delay::us(1);
    High(AUX3);
}
```

AUX3 拉高 = 释放扫描（`sweep_reset <= not aux3_sync`），先低后高形成一个确定的复位脉冲——`Done` 状态的扫描就是被这根线「拍醒」重跑的。[FPGA.cpp:L146-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L146-L156) 的 `SetupSweep` 则装配阶段编排（stages、两端口阶段号、同步使能/主机位），与 Sweep 的同名输入一一对应。

**（4）testbench 模板：Test_PLL.vhd 的骨架**

[Test_PLL.vhd:L35-L99](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L35-L99) 展示了仓库里最简 testbench 的三件套：无端口的 UUT 实体、时钟发生进程（`wait for CLK_IN1_period/2` 翻转）、激励进程（先复位 100 ns 再放松）。它是「骨架型」testbench——不含自动断言，主要靠人工看波形。这正好是我们为 Sweep 起步时该抄的模板。

#### 4.3.4 代码实践

**实践目标**：手算一次真实规模扫描的时间构成，并为 Sweep 写出一个可以跑起来的 testbench 骨架（含自检点设计）。

**操作步骤**：

1. **时间估算（纸面）**：设一次 VNA 扫描为 501 点、2 个阶段、每点样本数 912（IF 带宽 1 kHz）、SETTLING_TIME 设为 100 µs（即 \(100\times102.4\approx10240\) 个周期）。请算出：
   - 每阶段固定开销 ≈ settling 100 µs + 采样时间（样本数 × ADC 周期，ADC 周拍先用符号 \(T_{ADC}\) 表示，u6-l3 后再代入数值）；
   - 每点额外 PLL 重载 ≈ 7.7 µs（只发生一次，因为第 2 阶段不重载）；
   - 总时间 \( \approx 501 \times 2 \times (100\,\mu s + 912\,T_{ADC}) + 501 \times 7.7\,\mu s \)。
   与 GUI 中显示的扫描时间量级对比（有硬件时可实测验证；无硬件标注「待本地验证」）。
2. **写 Test_Sweep.vhd 骨架**（示例代码，仓库中不存在此文件，需要你自己新建；不要改动 Sweep.vhd 本身）：
   - 照抄 [Test_PLL.vhd:L74-L80](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Test_PLL.vhd#L74-L80) 的时钟进程，周期取 \(9.77\,\text{ns} \approx 10\,\text{ns} \approx 100\,\text{MHz}\)（仿真不必精确 102.4 MHz）；
   - 例化 Sweep，把 `CONFIG_DATA` 固定驱动成一组已知 96 位常量（建议 Samples 编码 `000` 用 `USER_NSAMPLES`，halt 位填 0）；
   - 给 `PLL_RELOAD_DONE`/`PLL_LOCKED` 写简单行为模型：一个计数器在 `RELOAD_PLL_REGS` 拉高后延迟约 800 个周期把 DONE 拉低再拉高（模拟 7.7 µs 移位），`PLL_LOCKED` 在 DONE 高之后几十周期置 1；
   - 给 `SAMPLING_BUSY` 写行为模型：`START_SAMPLING='1'` 后立即置 1，若干千周期后清 0；
   - 观察窗口盯住 `DEBUG_STATUS`：预期依次出现 0000(TriggerSetup) → 0001(SettingUp) → 0010(Settling) → 0011(WaitTriggerHigh) → 0100(Exciting) → 0101(WaitTriggerLow) → 0110(SamplingDone) → 0010(下一阶段) … → 0111(NextPoint) → 0000(新点) … → 1000(Done)。
3. **自检点设计**：在 testbench 里断言「`Done` 出现时 `RESULT_INDEX` 的最大点号等于 NPOINTS」和「`NEW_DATA` 脉冲总数 = 点数 × 阶段数」，把人工看波形升级为自动判定。

**需要观察的现象**：状态编码序列按预期循环；`SettingUp` 持续时间与你的行为模型延迟一致；`START_SAMPLING` 是单拍脉冲；`TRIGGER_OUT`（同步关闭时）无动作。

**预期结果**：得到一个可复用的 Sweep 仿真环境，以及一张「每点时间构成」的估算表。

**待本地验证**：本实践的数值部分（7.7 µs、10.24 ms）由代码参数推得，仿真与实测均待本地验证；testbench 需 ISE 或其他 VHDL 仿真器运行。

#### 4.3.5 小练习与答案

**练习 1**：如果不等 `PLL_RELOAD_DONE` 变低就撤掉 `RELOAD_PLL_REGS`，可能出什么问题？

**答案**：MAX2871 模块只在空闲沿采样 `RELOAD`。若请求脉冲恰好只维持到对方还没看到的那一拍就被撤回（例如请求恰好落在对方正忙的最后一个周期附近），这次重载会被完全错过，Sweep 却以为已经发起，之后在 `SettingUp` 等 `PLL_RELOAD_DONE='1'`——由于 DONE 从未被拉低过、一直就是 1，状态机可能带着**旧频率**继续走，测量整条频率轴全部错位。先确认 DONE 拉低再撤请求，是标准的四阶段握手，杜绝这种丢失。

**练习 2**：一次 501 点扫描里 PLL 重载发生多少次？如果每次重载 7.7 µs，它占总扫描时间的比例是上升还是下降（当用户把 IF 带宽从 1 kHz 改到 10 Hz）？

**答案**：501 次（每个新点一次；第 2 阶段复用频率不重载），共约 3.9 ms。IF 带宽变窄意味着每点样本数从 912 增加到 91392（约 100 倍），采样主体时间暴增，而重载开销固定不变，因此其占比显著**下降**。反过来，在极宽带（少样本）测量里，7.7 µs 的重载和 settling 占比上升，成为限速因素之一。

**练习 3**：仓库里为什么可以没有 Test_Sweep.vhd 而项目仍然可靠？说出现有的两条「代替验证」的路径。

**答案**：其一，模块自带 `DEBUG_STATUS` 观测口（[Sweep.vhd:L137-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd#L137-L152)），可在硬件上直接看状态序列与握手信号；其二，Sweep 的正确性最终由系统级行为暴露——MCU 收到的 `RESULT_INDEX` 与点号/阶段号对不上、扫描停摆、锁定丢失都会在集成测试（Software/Integrationtests 的 Python 用例）或 GUI 测量里显形。此外其握手对端 MAX2871 模块也可以单独仿真（Test_PLL.vhd 骨架可扩展）。这是「小模块仿真 + 大系统在线观测」的典型工程折中。

## 5. 综合实践

**综合任务：为一次 101 点、双阶段扫描做「全链路时序账」并设计验证方案。**

1. **配置侧**：写出这次扫描需要 MCU 预先写入 FPGA 的全部数据清单——101 项 96 位 SweepConfig（每项包含两颗 PLL 的 N/FRAC/M/VCO/DIV_A、衰减、滤波、波段、halt=0、Samples 编码）、`SweepPoints` 寄存器值（注意减一后是 100）、`SamplesPerPoint`（16 样本单位）、`SettlingTime`（换算公式在 [FPGA.cpp:L135-L144](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L135-L144)）以及 `SetupSweep` 的阶段编排。逐项核对每条数据在 [Sweep.vhd](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/Sweep.vhd) 里被哪个信号消费。
2. **时序侧**：用 4.3 的方法列出每点时间构成（重载一次 7.7 µs、每阶段 settling + 采样），算出总时间的表达式；再算「MCU 中断次数」= 点数 × 阶段数 = 202 次 `NEW_DATA` 脉冲，考虑这对应固件侧 202 次结果读取（呼应 u6-l1 的数据通路）。
3. **验证侧**：把 4.3.4 的 Test_Sweep 骨架按本任务参数具体化（NPOINTS=100、STAGES=1、PORT1_STAGE=0、PORT2_STAGE=1），写出预期的完整 `DEBUG_STATUS` 状态序列（含每个状态出现的总次数），并设计两条自动断言。
4. **反思侧**：写 200 字总结：「MCU 预编程 + FPGA 自主扫描」这种分工把哪些时序问题挡在了哪里？如果改成 MCU 逐点指挥，最坏会发生什么？

预期产出：一份配置清单、一个时间表达式、一份状态序列表和一个 testbench 文件（可选实现）。全部可离线完成，仿真部分待本地验证。

## 6. 本讲小结

- `Sweep.vhd` 是一个十状态机，用「点循环 × 阶段循环」两层嵌套自主推进整个扫描：换点必经 `TriggerSetup`/`SettingUp` 重载 PLL，换阶段直接回 `Settling`——因为频率没变。
- 每点的射频状态由 96 位 SweepConfig 驱动：组合逻辑把它拼装成两颗 MAX2871 各四个寄存器加衰减/滤波/波段等开关量，代码位切片与协议文档逐位吻合；不变化的位存在默认寄存器里，是对块 RAM 的刻意压缩。
- 频率切换后的等待有三处代码落点：`SettingUp` 等移位完成＋锁定、`Settling` 倒数 SETTLING_TIME、固件把用户驻留与逐台标定的 PLL 延迟（flash 设备配置）相加下发。
- halt/resume（配置字 bit95 + `SWEEP_RESUME`）是 MCU 干预自主扫描的唯一闸门，支撑 USB 反压与暂停点；多机同步靠触发菊花链的往返（主机发起、从机转发、主机等到回沿）。
- 所有外域信号（MCU GPIO、PLL 锁定检测、外部触发）一律经 `Synchronizer`（stages=2，实际穿 3 个触发器、延迟 3 拍约 29 ns）进入 clk_pll 域；扫描启停开关 AUX3 本身也是一根被同步的异步线。
- 依据 102.4 MHz 时钟与 CLK_DIV=6 可推得一次 PLL 重载约 7.7 µs、settling 满量程 10.24 ms；仓库没有 Test_Sweep.vhd，验证走 `DEBUG_STATUS` 在线观测 + 系统级测试，仓库的 Test_PLL.vhd 提供了补写 testbench 的现成骨架。

## 7. 下一步学习建议

Sweep 发出的 `START_SAMPLING`/`NSAMPLES` 在下一讲被消费：建议下一讲学习 **u6-l3（采样链路：Sampling.vhd 与 ADC 接口）**，看三路 MCP33131 如何在 `START_SAMPLING` 的指挥下逐样本转换、`SAMPLING_BUSY` 如何升起与回落，把本讲 4.3 留下的 \(T_{ADC}\) 数值补齐。之后是 **u6-l4（加窗与 DFT）**，理解 `NEW_DATA` 之前的片上运算。若你想先巩固本讲的同步与握手思想，可以回头精读 [top.vhd:L746-L766](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/FPGA/VNA/top.vhd#L746-L766) 的 SPI 复用逻辑（MCU 与 FPGA 分时驱动同一条 SPI 总线，是握手思想的又一实例）；对多机同步感兴趣的读者可结合固件侧 [Trigger.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp) 与协议文档的同步章节，把触发菊花链的两端对起来读。
