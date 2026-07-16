# 同步、消抖与时钟测量

## 1. 本讲目标

本讲进入 Open Logic 的 **intf 区域**——专门处理 FPGA 与外部世界（按钮、开关、传感器时钟、其他芯片送来的信号）打交道的电路。学完本讲你应当能够：

- 说清楚为什么外部信号进 FPGA 后**必须先过同步器**，以及双级（乃至多级）同步器是如何降低亚稳态风险的；
- 读懂 [`olo_intf_sync`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd) 的 N 级同步链与它携带的一组综合属性；
- 理解按钮/开关为什么需要**消抖**，掌握 [`olo_intf_debounce`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd) 的「预分频 Tick + 每比特稳定计数器」结构，以及 `LOW_LATENCY` 与 `GLITCH_FILTER` 两种模式的差异；
- 理解 [`olo_intf_clk_meas`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd) 如何用一个**已知频率**的主时钟去测量另一个**未知频率**的时钟，并能阅读它内部的两次跨时钟域；
- 学会为真实的外部按钮/开关选择合适的同步与消抖配置，并写出对应的时序约束。

---

## 2. 前置知识

本讲假设你已学过：

- **u1-l5**：Open Logic 的编码规范——同步高有效复位、复位写在进程末尾作覆盖、命名后缀（泛型 `_g`、常量 `_c`、类型 `_t`）、可选端口/泛型带默认值。
- **u2-l1**：base 区域公共包，尤其是 `olo_base_pkg_math`（`log2ceil`、`choose`、`min`）、`olo_base_pkg_string`（`compareNoCase`、`errorMessage`）。本讲多处用到它们。
- **AXI-S 握手与两进程法**（u2-l2）：理解 record + `p_comb`/`p_seq` 的写法。

下面几个术语会反复出现，先统一解释：

| 术语 | 含义 |
| :--- | :--- |
| **时钟域（clock domain）** | 由同一个时钟驱动的所有触发器的集合。信号从一个时钟域进另一个时钟域就是「跨时钟域（CDC）」。 |
| **亚稳态（metastability）** | 当一个触发器的数据输入在时钟沿附近变化时，输出可能停留在 0 和 1 之间的非法电平上一段时间，之后才随机收敛到 0 或 1。 |
| **同步器（synchronizer）** | 一串级联触发器（典型 2 级），给亚稳态电平留出收敛时间，从而把「非法电平」变成「只是延迟了几拍」。 |
| **MTBF** | 平均无故障时间（Mean Time Between Failures）。同步器级数越多，因亚稳态导致系统出错的 MTBF 越长。 |
| **消抖（debounce）** | 机械按钮/开关在按下/松开瞬间会反复通断（抖动），消抖电路只承认「稳定持续一段时间」的电平变化。 |
| **Tick（节拍）** | 一个低频的单周期脉冲，用作多个通道共享的「采样节拍」。 |

> 提示：本讲的三个实体都属于 **intf 区域**，专门面向「外部信号」。如果你要同步的是 **FPGA 内部**两个时钟域之间的信号，Open Logic 建议改用 base 区域的 `olo_base_cc_*` 系列（见 u4 单元），它们的跨域语义更完整。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| :--- | :--- |
| [`src/intf/vhdl/olo_intf_sync.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd) | N 级（默认 2 级）同步器，含全部跨厂商综合属性。是其他两个实体的地基。 |
| [`src/intf/vhdl/olo_intf_debounce.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd) | 内部实例化 `olo_intf_sync` 做同步，再叠加每比特稳定计数器实现消抖，支持两种模式。 |
| [`src/intf/vhdl/olo_intf_clk_meas.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd) | 用已知主时钟测未知时钟频率，内部用 `olo_base_cc_pulse` 与 `olo_base_cc_simple` 做两次跨域。 |
| [`src/base/vhdl/olo_base_strobe_gen.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_strobe_gen.vhd) | 频率可控的单周期脉冲发生器。消抖用它产生 Tick，时钟测量用它产生 1 Hz 秒脉冲。 |
| [`src/intf/tcl/olo_intf_sync.tcl`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/tcl/olo_intf_sync.tcl) | AMD（Vivado）的 scoped 约束文件，告诉综合器 `olo_intf_sync` 是 CDC 路径。 |
| [`test/intf/olo_intf_debounce/olo_intf_debounce_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_debounce/olo_intf_debounce_tb.vhd) | 消抖器的 VUnit 测试台，含抖动注入用例，是本讲实践的主要参考。 |
| [`test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd) | 时钟测量器的测试台，演示了「测一个已知频率并断言误差」的模式。 |

依赖关系一目了然：

```
olo_intf_sync  ◄──── 内部被 olo_intf_debounce 实例化
olo_base_strobe_gen ◄── 被 debounce（做 Tick）与 clk_meas（做 1 Hz）共用
olo_base_cc_pulse / olo_base_cc_simple ◄── 仅被 clk_meas 使用（两次跨域）
```

---

## 4. 核心概念与源码讲解

本讲拆为四个最小模块：**双级同步器**、**消抖器**、**时钟测量**、**外部信号处理（选型与约束）**。

### 4.1 双级同步器（olo_intf_sync）

#### 4.1.1 概念说明

FPGA 外部的信号（按钮、开关、另一颗芯片送来的异步控制信号）与 FPGA 内部的系统时钟 `Clk` 之间**没有任何固定的时间关系**。如果直接把这种异步信号接到内部逻辑的触发器输入上，信号就可能在 `Clk` 的上升沿附近发生变化，触发器进入**亚稳态**：输出既不是干净的 0 也不是干净的 1，而是一个停留在中间电平、需要一段时间才随机收敛的值。一旦这个非法电平被后级逻辑扇出到多处，不同分支可能分别解释成 0 和 1，整个系统就会出错。

标准对策是**同步器**：在信号进入内部逻辑之前，先串接两级（或更多级）触发器。第一级触发器仍可能进入亚稳态，但只要它在第二级触发器采样之前收敛，第二级输出的就是一个干净的、只是延迟了几拍的合法电平。级数越多，留给亚稳态收敛的时间越充裕，系统的平均无故障时间（MTBF）越长。

`olo_intf_sync` 干的就是这件事，并且把「让综合工具正确实现同步器」所需的一整套综合属性都写好了——这是它真正的价值：手写两级触发器人人会，但让所有厂商工具都老老实实把它们实现成**独立的、不被优化合并、不被塞进移位寄存器**的触发器，才是难点。

> 重要约束：同步器只能用于**相互独立的单比特信号**。它**不保证**一个多位总线所有位在同一拍到达（各位的亚稳态收敛时机不同）。要传多位数据/数值，请用 base 区域的 `olo_base_cc_*` 跨域实体。

#### 4.1.2 核心流程

`olo_intf_sync` 的行为可以用一行话概括：把异步输入 `DataAsync` 串过 `SyncStages_g` 级触发器，末级的输出就是同步后的 `DataSync`。

```
DataAsync ──▶ [FF Reg0] ──▶ [FF RegN(0)] ──▶ ... ──▶ [FF RegN(top)] ──▶ DataSync
              第1级          第2级                       第 SyncStages_g 级
```

- **延迟**：输出比输入晚 `SyncStages_g` 个时钟周期（默认 2 拍）。
- **复位**：复位时把所有同步寄存器置为 `RstLevel_g`（默认 `'0'`）。
- **级数选择**：`SyncStages_g` 取值范围 2–4。绝大多数场景 2 级够用；对 MTBF 要求极高的场景可加到 3 或 4 级。

为了让综合器不破坏这条同步链，源码给寄存器加了一组属性，核心思想是三条：

1. **不要把同步链抽成移位寄存器（SRL）**——否则第一、二级可能被吸进同一个查找表资源里，物理上不再是两个独立触发器；
2. **不要把这些触发器与其他逻辑合并、不要被优化掉**；
3. **把第一级标记为「异步寄存器」**（`async_reg`），让布局工具把同步链各级摆在同一个 slice 里，缩短级间走线、提高 MTBF。

#### 4.1.3 源码精读

先看实体声明。三个泛型、一个简单的数据通路：

- [`olo_intf_sync.vhd:34-38`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L34-L38) —— `Width_g`（同步几路独立单比特，默认 1）、`RstLevel_g`（复位电平，默认 `'0'`）、`SyncStages_g`（级数，约束在 `2 to 4`）。注意端口 `Rst` 自带默认值 `'0'`（[L42](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L42)），因为同步器通常不需要复位。

```vhdl
generic (
    Width_g      : positive              := 1;
    RstLevel_g   : std_logic             := '0';
    SyncStages_g : positive range 2 to 4 := 2
);
```

存储用两类信号：第一级 `Reg0`，以及「第 2 级到末级」组成的数组 `RegN`。因为级数由泛型决定，`RegN` 用一个元素数为 `SyncStages_g - 1` 的数组实现（[L56-L60](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L56-L60)）：

```vhdl
type SyncStages_t is array(0 to SyncStages_g - 2) of std_logic_vector(Width_g - 1 downto 0);
signal Reg0 : std_logic_vector(Width_g - 1 downto 0) := (others => RstLevel_g);
signal RegN : SyncStages_t                           := (others => (others => RstLevel_g));
```

接着是一大组综合属性（[L62-L84](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L62-L84)）。这些常量都来自 `olo_base_pkg_attribute` 包（u2-l1 讲过，它用「一次声明全部厂商属性」让一份 VHDL 跨 Vivado/Quartus/Efinity/Gowin 可综合）。其中最关键的是把同步链标记为异步寄存器：

```vhdl
-- Synthesis attributes - asynchronous registers
attribute async_reg of Reg0 : signal is AsyncReg_TreatAsync_c;
attribute async_reg of RegN : signal is AsyncReg_TreatAsync_c;
```

同步与复位逻辑写在一个时序进程里（[L89-L108](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L89-L108)），完全符合 u1-l5 讲过的「复位写在进程末尾作覆盖」约定：

```vhdl
p_outff : process (Clk) is
begin
    if rising_edge(Clk) then
        Reg0    <= DataAsync;
        RegN(0) <= Reg0;
        for i in 1 to RegN'high loop       -- 第3级及以后（若 SyncStages_g>2）
            RegN(i) <= RegN(i - 1);
        end loop;
        if Rst = '1' then                  -- 复位覆盖：只发生在末尾
            Reg0 <= (others => RstLevel_g);
            RegN <= (others => (others => RstLevel_g));
        end if;
    end if;
end process;
-- 输出取末级
DataSync <= RegN(RegN'high);
```

注意一个细节：当 `SyncStages_g = 2` 时，`RegN'high = 0`，`for i in 1 to 0` 循环体不执行，结构退化为标准的「`Reg0` → `RegN(0)`」两级同步器。输出取数组最高下标（[L111](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_sync.vhd#L111)），所以无论级数多少，末级永远是输出。

#### 4.1.4 代码实践：阅读并运行同步器测试台

**实践目标**：直观看到「输出比输入晚 `SyncStages_g` 拍」，并确认复位电平生效。

**操作步骤**：

1. 打开 [`olo_intf_sync_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_sync/olo_intf_sync_tb.vhd)。注意它把 `Time_MaxDel_c` 设为 `(SyncStages_g + 0.1) * Clk_Period_c`（[L48](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_sync/olo_intf_sync_tb.vhd#L48)），即「每级 1 拍」的延迟上界，并据此断言 `DataSync` 在窗口内追上 `DataAsync`。
2. 运行该测试台（在 `sim/` 目录，GHDL 为默认仿真器）：

   ```bash
   python3 run.py -p=4 --ghdl "*olo_intf_sync_tb*"
   ```

   `sim/test_configs/olo_intf.py` 为它注册了 `SyncStages_g ∈ {2,4}` 与 `RstLevel_g ∈ {0,1}` 的组合（[L48-L52](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L48-L52)），所以一次运行会跑多个用例。

**需要观察的现象**：在 `SimpleTransfer` 用例里，`DataAsync` 写入 `x"AB"` 后，`DataSync` 会在不超过 `SyncStages_g` 拍后变成 `x"AB"`；`ResetValue` 用例里复位后 `DataSync` 等于全 `RstLevel_g`。

**预期结果**：所有配置（2 级/4 级、复位电平 0/1）全部通过。若你在波形里手动测量，`DataSync` 相对 `DataAsync` 的延迟应恰好等于 `SyncStages_g` 个时钟周期。

> 待本地验证：具体命令能否一次跑通取决于你本机的 GHDL/VUnit 安装；若 `-p=4` 报错可去掉并发参数。本讲不假装已替你执行过命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `olo_intf_sync` 的文档强调「不要用它传多位总线」？

**参考答案**：同步器对每一位独立做亚稳态收敛，不同位的收敛结果与时机互不相同，可能导致同一总线的各比特不在同一拍更新，后级读到「半新半旧」的值。总线跨域必须用能保证一致性的实体（如 `olo_base_cc_simple` 或异步 FIFO）。

**练习 2**：把 `SyncStages_g` 从 2 改成 4，输出延迟和数据正确性分别会如何变化？

**参考答案**：数据仍然正确，但输出延迟从 2 拍增加到 4 拍；同时亚稳态收敛窗口更长，MTBF 提升，适合对可靠性要求极高的场景。

---

### 4.2 消抖器（olo_intf_debounce）

#### 4.2.1 概念说明

机械按钮和开关在动作瞬间会产生**抖动（bounce）**：触点在几毫秒内反复通断十几次，反映到信号上就是一串密集的毛刺。如果直接把这种信号送给内部逻辑（比如用按钮做计数、做中断），按一下可能被识别成很多次。

**消抖（debounce）** 的思路是：只有当信号在新电平上**稳定持续一段设定的时间（`DebounceTime_g`）** 之后，才承认这次电平变化有效。`olo_intf_debounce` 把「同步」和「消抖」合在一个实体里：内部先实例化 `olo_intf_sync` 把外部信号同步进来，再做消抖。

它提供两种工作模式（`Mode_g`）：

- **`LOW_LATENCY`（默认）**：信号边沿一来就**立刻**转发到输出（延迟小于 5 拍）；但转发完一次后，必须等信号稳定 `DebounceTime_g` 才能识别下一个边沿。副作用是：**单周期输入脉冲会被拉长成约 `DebounceTime_g` 宽度的脉冲**。适合按钮——人对按钮延迟敏感，能容忍脉冲被拉宽。
- **`GLITCH_FILTER`**：任何电平变化都必须在新电平上稳定 `DebounceTime_g` 之后才转发。副作用是：**短于 `DebounceTime_g` 的毛刺（包括单周期脉冲）会被完全抑制**。适合滤除真正的干扰毛刺、对延迟不敏感的开关量。

#### 4.2.2 核心流程

整体数据通路是「同步 → 共享 Tick → 每比特稳定计数器 → 按模式输出」：

```
DataAsync ──▶ olo_intf_sync ──▶ DataSync ──▶ [每比特稳定计数器] ──▶ DataOut
                                   ▲
                                   │ 每比特各自计数：信号不变则 +1，变化则清 0
                                   │ 计满 DebounceTicks_c → IsStable=1
.Tick（共享预分频节拍）──▶ 控制计数节奏
```

资源优化的关键在于 **Tick 预分频器共享**：消抖时间通常很长（毫秒级），而时钟很快（百兆级），如果每比特都用一个大计数器去数时钟周期，多位信号时资源浪费严重。这里改用一个小巧的 `olo_base_strobe_gen` 产生一个低频的 **Tick** 节拍，所有比特的计数器都按 Tick 计数，于是每个计数器只需要数到大约 31（5 位即可），把「做大分频」的开销集中到一个共享的预分频器上。

Tick 频率的推导（设计目标是让每比特计数器落在 15–31 的舒适区，代码注释见 [L58-L59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L58-L59)）：

\[
f_{\text{tick,target}} = \frac{31}{T_{\text{debounce}}}
\]

\[
N_{\text{cycles}} = \left\lceil \frac{f_{\text{clk}}}{f_{\text{tick,target}}} \right\rceil ,\qquad
f_{\text{tick,actual}} = \frac{f_{\text{clk}}}{N_{\text{cycles}}}
\]

\[
N_{\text{debounce}} = \left\lceil T_{\text{debounce}} \cdot f_{\text{tick,actual}} \right\rceil
\]

即：目标 Tick 频率取「每消抖时间 31 个节拍」，再由时钟频率算出每个 Tick 含多少个时钟周期（向上取整），由此得到实际 Tick 频率，最后反推需要数多少个 Tick 才覆盖 `DebounceTime_g`——这步内部消化了取整误差，所以用户只需给 `DebounceTime_g` 一个时间值即可。

每比特稳定计数器的判定逻辑：

- 每个 Tick：若计数已达 `DebounceTicks_c`，置 `IsStable=1`；否则计数 +1。
- 一旦检测到 `DataSync` 与上一拍 `LastState` 不同（信号变了），立刻清 0 计数、`IsStable=0`。
- `IsStable=1` 即代表「信号已稳定足够久，可以接受/转发新状态」。

两种模式只在「输出怎么更新」上不同（见 4.2.3）。

#### 4.2.3 源码精读

实体声明（[L34-L41](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L34-L41)）。注意 `ClkFrequency_g` 没有默认值（必须由用户提供时钟频率，单位 Hz），`DebounceTime_g` 默认 20 ms：

```vhdl
generic (
    ClkFrequency_g  : real;
    DebounceTime_g  : real      := 20.0e-3;
    Width_g         : positive  := 1;
    IdleLevel_g     : std_logic := '0';
    Mode_g          : string    := "LOW_LATENCY"    -- LOW_LATENCY or GLITCH_FILTER
);
```

上节那组常量在 [L60-L65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L60-L65)。还有一个开关 `UseStrobeDiv_c`（[L65](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L65)）：当 `TickCycles_c < 2`（消抖时间极短，Tick 本就该每拍都发）时，干脆不实例化预分频器，直接令 `Tick <= '1'`。

状态用两进程法的 record 收纳（[L78-L84](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L78-L84)），每个比特一组 `(StableCnt, LastState, IsStable, DataOut)`：

```vhdl
type TwoProcess_r is record
    StableCnt : Cnt_a;                                 -- 每比特的稳定计数（数组）
    LastState : std_logic_vector(Bits_c-1 downto 0);   -- 上一拍的同步后电平
    IsStable  : std_logic_vector(Bits_c-1 downto 0);   -- 每比特是否已稳定
    DataOut   : std_logic_vector(Bits_c-1 downto 0);   -- 消抖后输出
end record;
```

两个断言把关（[L90-L97](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L90-L97)）：`Mode_g` 必须是二者之一；`DebounceTicks_c >= 10`（对应文档「`DebounceTime_g` 至少 10 个时钟周期」）。

组合进程 `p_comb` 是核心。计数与稳定判定（[L108-L115](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L108-L115)）：

```vhdl
if Tick = '1' then
    if r.StableCnt(i) = DebounceTicks_c then
        v.IsStable(i) := '1';
    else
        v.StableCnt(i) := r.Stablecnt(i) + 1;
    end if;
end if;
```

检测到电平变化就清计数（[L117-L121](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L117-L121)）：

```vhdl
if DataSync(i) /= r.LastState(i) then
    v.StableCnt(i) := 0;
    v.IsStable(i)  := '0';
end if;
```

两种模式的差异就在输出更新上。`GLITCH_FILTER`：只有稳定后，才把**当时已稳定下来的电平** `LastState` 送出（[L123-L129](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L123-L129)）；`LOW_LATENCY`：一旦稳定可接受新值，就把**当前同步电平** `DataSync` 直接送出（[L131-L137](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L131-L137)）：

```vhdl
if compareNoCase(Mode_g, "LOW_LATENCY") then
    if r.IsStable(i) = '1' then
        v.DataOut(i) := DataSync(i);   -- 立刻跟随当前电平
    end if;
end if;
```

> 读懂这个区别是理解两种模式行为的关键：`LOW_LATENCY` 在「准备好」时输出紧跟实时电平，所以边沿几乎立刻可见；`GLITCH_FILTER` 输出的是「已经被证明稳定」的电平，所以必须等满 `DebounceTime_g`。

时序进程 `p_seq` 打拍并复位（[L150-L164](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L150-L164)）。注意复位初值的巧思——`IsStable` 复位为全 `'1'`（[L160](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L160)），意思是「复位后立刻就处于可接受新边沿的状态」，这正是 `LOW_LATENCY` 能在复位后立刻转发第一个边沿的原因：

```vhdl
if Rst = '1' then
    r.StableCnt <= (others => DebounceTicks_c);
    r.LastState <= (others => IdleLevel_g);
    r.IsStable  <= (others => '1');        -- 复位后即可接受第一个边沿
    r.DataOut   <= (others => IdleLevel_g);
end if;
```

最后是两个实例化。先同步（[L167-L178](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L167-L178)），注意它把 `olo_intf_sync` 的 `RstLevel_g` 设成 `IdleLevel_g`，避免复位后头两拍出现伪脉冲（与 4.1 讲的 `RstLevel_g` 用途一致）。再用条件 generate 决定是否实例化预分频器（[L180-L198](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_debounce.vhd#L180-L198)）：

```vhdl
g_use_strobe : if UseStrobeDiv_c generate
    i_tickgen : entity work.olo_base_strobe_gen
        generic map (FreqClkHz_g => ClkFrequency_g, FreqStrobeHz_g => ActualTickFrequency_c)
        port map (Clk => Clk, Rst => Rst, Out_Valid => Tick);
end generate;

g_no_strobe : if not UseStrobeDiv_c generate
    Tick <= '1';      -- Tick 周期 < 2 拍时，每拍都是 Tick
end generate;
```

#### 4.2.4 代码实践：用测试台注入抖动并观察两种模式

**实践目标**：亲眼看到 `LOW_LATENCY` 与 `GLITCH_FILTER` 对抖动信号的不同响应。

**操作步骤**：

1. 打开 [`olo_intf_debounce_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_debounce/olo_intf_debounce_tb.vhd)。重点关注 `BouncPulse` 用例（[L166-L210](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_debounce/olo_intf_debounce_tb.vhd#L166-L210)）：它在一段时间内反复翻转 `DataAsync(0)`（模拟抖动），随后令其稳定。对两种模式分别断言了「抖动期间输出应是什么」：
   - `LOW_LATENCY`：抖动期间 `DataOut(0)` 跟着翻转（边沿立刻转发）；
   - `GLITCH_FILTER`：抖动期间 `DataOut(0)` 始终保持旧值（毛刺被抑制）。
2. 运行（`olo_intf.py` 已为两种模式 × 多个 `DebounceCycles_g` 注册了组合，[L26-L32](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L26-L32)）：

   ```bash
   python3 run.py --ghdl "*olo_intf_debounce_tb*"
   ```

**需要观察的现象**：`ShortPulse` 用例中，给一个短脉冲：`LOW_LATENCY` 下 `DataOut` 会被拉高一段时间（脉冲被拉宽），`GLITCH_FILTER` 下 `DataOut` 几乎不动（脉冲被吃掉）。

**预期结果**：所有模式与消抖周期组合（包括特意覆盖 31/32、63/64 分频边界的取值）全部通过。

> 待本地验证：完整用例集较大、跑完需要可观时间；可先用 `*/BouncPulse*` 之类过滤只跑感兴趣的用例（具体过滤语法依 VUnit 版本而定）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `olo_intf_debounce` 要把预分频器（Tick 发生器）做成所有比特共享，而不是每比特各做一个大计数器？

**参考答案**：消抖时间远大于时钟周期，若每比特都直接数时钟周期，多位信号需要多个宽位计数器，资源浪费。共享一个低频 Tick 后，每比特只需一个小计数器（数到约 31，5 位即可），把分频开销集中到单一预分频器，总资源最低。

**练习 2**：复位时 `IsStable` 被设成全 `'1'` 而不是 `'0'`，这会对 `LOW_LATENCY` 模式的「第一个边沿」产生什么影响？

**参考答案**：设成 `'1'` 表示复位后即处于「可接受新边沿」状态，因此复位后到来的第一个边沿无需等待 `DebounceTime_g` 就会被转发，实现低延迟。若设成 `'0'`，第一个边沿也要等满消抖时间才转发。

**练习 3**：一个会持续 1 拍的窄毛刺，分别送进 `LOW_LATENCY` 和 `GLITCH_FILTER` 的 `olo_intf_debounce`，输出分别是什么？

**参考答案**：`LOW_LATENCY` 下，该毛刺会被拉宽成约 `DebounceTime_g` 宽度的脉冲出现在输出；`GLITCH_FILTER` 下，该毛刺因达不到稳定时间而被完全抑制，输出不变。

---

### 4.3 时钟测量（olo_intf_clk_meas）

#### 4.3.1 概念说明

很多 FPGA 系统的主时钟来自 PS（处理系统）或外部晶振，频率是**已知且可信**的；而板上其他时钟（送给某颗 ADC、某条恢复时钟）的频率需要**验证**是否被正确配置。`olo_intf_clk_meas` 解决的就是：**用一个已知频率的主时钟 `Clk`，去测量另一个待测时钟 `ClkTest` 的频率**。

原理非常直白——**门控计数法**：在主时钟域产生一个**精确的 1 Hz 秒脉冲**（因为主时钟频率已知，产生 1 Hz 是平凡的），把这个秒脉冲跨到待测时钟域；在待测时钟域里，数两个秒脉冲之间 `ClkTest` 跳变了多少次。因为采样窗口恰好是 1 秒，所以数到的跳变次数就是频率（单位 Hz）：

\[
f_{\text{test}} \;[\text{Hz}] \;=\; \text{1 秒窗口内 } \textit{ClkTest} \text{ 的周期数}
\]

结果再跨回主时钟域输出。这个实体内部因此包含**两次跨时钟域**（秒脉冲过去、结果回来），复用了 base 区域的 `olo_base_cc_pulse` 与 `olo_base_cc_simple`（见 u4 单元）。

#### 4.3.2 核心流程

整体由主时钟域、待测时钟域两段逻辑 + 三处实例化构成：

```
【主时钟域 Clk】
  olo_base_strobe_gen(1 Hz) ──▶ SecPulse_M
                                  │
                  ┌───────────────┼───────────────┐
                  ▼ (cc_pulse)                    ▼ (cc_simple 回传结果)
【待测时钟域 ClkTest】                         Result_M, ResultValid_M
  SecPulse_T ──▶ 计数器 CntrTest_T            主域锁存 → Freq_Hz, Freq_Valid
                  数 ClkTest 周期；每到 SecPulse_T：
                  结果 = CntrTest_T，计数器清 1
```

主时钟域还要额外干一件事：**检测待测时钟是否停摆**。如果连续两个秒脉冲之间，待测域没有把任何结果回传过来（说明 `ClkTest` 没在跳），主域就判定频率为 0 Hz。这是个很巧的旁路——因为待测域逻辑本身同步于 `ClkTest`，时钟一旦停了，那边的逻辑自然不会再产生结果，必须由主域来发现这件事。

关于精度：因为要靠跨域，主时钟太慢（< 100 Hz）时无法保证测量精度，所以实体用断言强制 `ClkFrequency_g >= 100 Hz`。

#### 4.3.3 源码精读

实体声明（[L33-L46](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L33-L46)）。`ClkFrequency_g` 必填，`MaxClkTestFrequency_g` 默认 1 GHz——它决定计数器与内部结果总线的位宽：

```vhdl
generic (
    ClkFrequency_g          : real;
    MaxClkTestFrequency_g   : real := 1.0e9
);
port (
    Clk        : in  std_logic;                       -- 已知频率的主时钟
    Rst        : in  std_logic;
    ClkTest    : in  std_logic;                       -- 待测时钟
    Freq_Hz    : out std_logic_vector(31 downto 0);   -- 测得频率（Hz）
    Freq_Valid : out std_logic                        -- 每秒脉冲一次：有新结果
);
```

结果位宽由 `MaxClkTestFrequency_g` 决定，用 `log2ceil` 推导（[L54-L57](https://github.com/open-logic/open-logic/blob/ecca8af95e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L54-L57)），最后 `resize` 到对外固定的 32 位：

```vhdl
constant MaxClkTestFrequencyInt_c : integer := integer(MaxClkTestFrequency_g);
constant ResultWidth_c            : integer := log2ceil(integer(MaxClkTestFrequencyInt_c)+1);
```

两个断言强制两个频率都 >= 100 Hz（[L74-L80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L74-L80)）。

主时钟域进程 `p_control`（[L85-L118](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L85-L118)）。它默认每拍把 `Freq_Valid` 拉低，仅在「锁存到新结果」或「判定时钟停摆」时拉高一拍。停摆检测的逻辑是：每个秒脉冲置位 `AwaitResult_M`（表示「我在等这一秒的结果」），如果下一个秒脉冲到来时 `AwaitResult_M` 仍为 `'1'`（即上一秒没等到任何结果），说明待测时钟停了：

```vhdl
if SecPulse_M = '1' then
    AwaitResult_M <= '1';
    if AwaitResult_M = '1' then            -- 上一秒没回结果 → 时钟停摆
        Freq_Hz    <= (others => '0');
        Freq_Valid <= '1';
    end if;
end if;

if ResultValid_M = '1' then                -- 收到待测域回传的结果
    Freq_Hz       <= std_logic_vector(resize(unsigned(Result_M), Freq_Hz'length));
    AwaitResult_M <= '0';                  -- 清掉「等待」标志
    Freq_Valid    <= '1';
end if;
```

> 文档特别说明：`Freq_Hz` 在两次 `Freq_Valid` 脉冲之间**保持上一次的测得值**不变（因为它只在上述两处被赋值），这点与 AXI-S「数据在 Valid 期间有效」的约定一致。

待测时钟域进程 `p_meas`（[L123-L147](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L123-L147)）在 `ClkTest` 上升沿工作。每到秒脉冲就「抓拍」计数器、清零（清成 1，因为抓拍这一拍本身就是一个 `ClkTest` 周期）；否则计数 +1，并封顶防溢出：

```vhdl
if SecPulse_T = '1' then
    Result_T      <= toUslv(CntrTest_T, ResultWidth_c);
    CntrTest_T    <= 1;                     -- 抓拍这一沿已隐含到达
    ResultValid_T <= '1';
elsif CntrTest_T /= MaxClkTestFrequencyInt_c then
    CntrTest_T <= CntrTest_T + 1;           -- 封顶，防止溢出
end if;
```

三处实例化（[L153-L188](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L153-L188)）正好对应流程图：`olo_base_strobe_gen` 产生 1 Hz（[L153-L162](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L153-L162)）、`olo_base_cc_pulse` 把秒脉冲跨到待测域（[L165-L173](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L165-L173)）、`olo_base_cc_simple` 把结果跨回主域（[L176-L188](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/vhdl/olo_intf_clk_meas.vhd#L176-L188)）。注意 `cc_pulse` 顺带输出了 `Rst_T`（待测域的复位），这正是 u4-l1 强调的「复位要随 CDC 一起穿越」的体现。

#### 4.3.4 代码实践：测一个已知频率并验证误差

**实践目标**：把一个已知频率的时钟喂给 `olo_intf_clk_meas`，验证读数与已知值一致（容许 ±1 Hz 的跨域抖动误差）。

**操作步骤**：

1. 打开 [`olo_intf_clk_meas_tb.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd)。它的核心是 `checkFrequency` 过程（[L62-L80](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd#L62-L80)）：设置 `TestFrequencyReal`，等两次 `Freq_Valid`（第一次可能受切换影响，丢弃），然后断言读数与期望之差 <= 1。`Zero` 用例（[L161-L173](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd#L161-L173)）则验证「停摆时钟被识别为 0 Hz」。
2. 这个测试台为了避免仿真跑真实秒级时长，故意用了很小的频率（`ClkFrequency_g` 与 `MaxClkTestFrequency_g` 取 100/123/7837 这类小整数，见 [`olo_intf.py` L57-L59](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/sim/test_configs/olo_intf.py#L57-L59)）。运行：

   ```bash
   python3 run.py --ghdl "*olo_intf_clk_meas_tb*"
   ```

**需要观察的现象**：每次 `Freq_Valid` 脉冲到来时，`Freq_Hz` 更新为一个新值；该值等于（或 ±1 等于）`TestFrequencyReal`。当把 `TestFrequencyReal` 设成接近 0（停摆）时，`Freq_Hz` 变为 0。

**预期结果**：`Lower`、`Between0AndLower`、`BetweenLowerAndupper`、`Upper`、`MaxTestFrequency` 等用例全部通过；`AboveMaxTestFrequency` 用例下计数器封顶、读数等于 `MaxClkTestFrequency_g`；`Zero` 用例下读数为 0。

> 待本地验证：`Zero` 用例含 30 秒 watchdog（[L109](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/test/intf/olo_intf_clk_meas/olo_intf_clk_meas_tb.vhd#L109)），即便用了小频率仍需相当仿真墙钟时间，运行前请有心理预期。

#### 4.3.5 小练习与答案

**练习 1**：为什么「在 1 秒窗口里数 `ClkTest` 的周期数」就等于待测频率？这个方法对主时钟的精度有何假设？

**参考答案**：因为采样窗口由主时钟产生、恰好为 1 秒，窗口内的周期数即「每秒周期数」= 频率（Hz）。它假设主时钟 `Clk` 的频率**精确等于** `ClkFrequency_g`——主时钟本身的频率误差会等比例地变成测量误差。

**练习 2**：如果待测时钟停了，`Freq_Hz` 会变成什么？主时钟域是**如何发现**这件事的？

**参考答案**：`Freq_Hz` 变为 0。因为待测域逻辑同步于 `ClkTest`，时钟停了它就不再回传结果；主域用 `AwaitResult_M` 标志检测——若某个秒脉冲到来时上一秒的「等待」标志仍未被结果清除，就判定时钟停摆并输出 0。

**练习 3**：`MaxClkTestFrequency_g` 这个泛型影响实体的哪些部分？

**参考答案**：它决定待测域计数器 `CntrTest_T` 的范围（封顶值，防溢出）和内部结果总线 `ResultWidth_c` 的位宽（`log2ceil(MaxClkTestFrequency+1)`）。测高于该值的频率时计数器会封顶，读数等于该最大值。

---

### 4.4 外部信号处理：选型、级联与时序约束

#### 4.4.1 概念说明

把前面三个积木放回真实工程里，会遇到两个工程化问题：**该选哪一个**，以及**约束怎么写**。

选型上，三个实体其实是层层叠加的关系，按「外部信号长什么样」来挑：

| 外部信号特征 | 推荐实体 | 理由 |
| :--- | :--- | :--- |
| 已经干净的异步单比特（如另一芯片送来的控制位） | `olo_intf_sync` | 只需跨域到 `Clk`，无需消抖。 |
| 按钮、开关、机械触点（有抖动） | `olo_intf_debounce` | 自带同步 + 消抖，一步到位。 |
| 需要知道某根时钟线/某路输出时钟的实际频率 | `olo_intf_clk_meas` | 已知主时钟即可测频。 |

约束上，4.1 讲过同步器**必须电路 + 约束配套**——同步器电路降低亚稳态概率，时序约束告诉工具「这条从外部端口到第一级触发器的路径是异步 CDC 路径，不要按常规时序去硬收敛，而是用 `set_max_delay` 限制它的最大延迟」。Open Logic 对 AMD（Vivado）提供了自动 scoped 约束，其他厂商需手写。

#### 4.4.2 核心流程

一个典型的「按钮 → 内部脉冲」处理链路是：

```
物理按钮 ──▶ olo_intf_debounce(LOW_LATENCY) ──▶ 干净的电平 ──▶ 边沿检测 ──▶ 单周期脉冲
                                                                   (用于计数/中断)
```

而约束侧的流程是：

```
综合前：为每个 olo_intf_sync / olo_intf_debounce 实例施加 CDC 约束
        ├─ AMD：read_xdc -ref <实体名> olo_intf_sync.tcl（自动 scoped）
        └─ 其他厂商：手写一对 set_max_delay
```

#### 4.4.3 源码精读：AMD 的 scoped 约束

[`olo_intf_sync.tcl`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/tcl/olo_intf_sync.tcl) 是给 `olo_intf_sync` 用的 Vivado 约束。它做三件事：找到第一级触发器 `Reg0` 的 D 端、回溯到与之相连的顶层输入端口、对这条「端口→第一级 FF」路径设一个不超过一个时钟周期的最大延迟（[L9-L17](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/tcl/olo_intf_sync.tcl#L9-L17)）：

```tcl
set in_ffs   [get_pins -of_objects [get_cells *Reg0*] -filter {REF_PIN_NAME == D}]
set in_ports [get_ports -scoped_to_current_instance -prop_thru_buffers -of_objects [get_nets -of_objects $in_ffs]]
set latch_clk [get_clocks -of_objects [get_cells *Reg0*]]
set_max_delay -datapath_only [get_property -min PERIOD $latch_clk] -from $in_ports
```

还有一条容易忽略但很重要的约束（[L20](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/tcl/olo_intf_sync.tcl#L20)）：**禁止把第一级 FF 放进 IOB（I/O Block）**，迫使两级 FF 都落在同一个 slice 内，缩短级间走线、提升 MTBF：

```tcl
set_property IOB FALSE [get_cells *Reg0*]
```

> `olo_intf_debounce` 内部实例化了 `olo_intf_sync`，因此施加在 `olo_intf_sync` 上的 scoped 约束会自动覆盖消抖器里的同步器实例——这就是 Open Logic 在 Vivado 下能「零手写约束」的原因。聚合脚本为 [`olo_intf_constraints_amd.tcl`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/intf/tcl/olo_intf_constraints_amd.tcl)。其他厂商需照此手写 `set_max_delay`。

#### 4.4.4 代码实践：级联同步与消抖，并写一份约束

**实践目标**：把 `olo_intf_sync` 与 `olo_intf_debounce` 的关系从「源码里读到」变成「自己搭出来」，并写一份对应的时序约束。

**操作步骤**：

1. 写一个顶层，把外部按钮 `ButtonAsync_i` 接到 `olo_intf_debounce`（`Mode_g = "LOW_LATENCY"`，`ClkFrequency_g` 填你的真实时钟频率，`DebounceTime_g` 取 10–20 ms）：
   ```vhdl
   i_btn : entity olo.olo_intf_debounce
       generic map (ClkFrequency_g => 100.0e6, DebounceTime_g => 20.0e-3,
                    IdleLevel_g => '0', Mode_g => "LOW_LATENCY")
       port map (Clk => Clk, Rst => Rst, DataAsync => ButtonAsync_i, DataOut => ButtonClean);
   ```
2. 思考：这已经包含了一次 `olo_intf_sync`（在 `olo_intf_debounce` 内部）。如果你还有**另一路**已经干净的外部信号（例如一颗 IC 送来的 `ReadyAsync_i`），则单独实例化一个 `olo_intf_sync`。
3. 为 AMD 工具写约束：在工程里 `read_xdc -ref olo_intf_sync <repo>/src/intf/tcl/olo_intf_sync.tcl`，让 scoped 约束自动覆盖所有同步器实例。
4. 为非 AMD 工具手写：对每个外部输入端口，写一条 `set_max_delay -datapath_only <Clk周期> -from [get_ports {ButtonAsync_i}]`。

**需要观察的现象**：综合后查看时序报告，这些外部输入路径应被归类为 CDC/最大延迟约束路径，而非普通的建立时间失败路径。

**预期结果**：按钮输入在内部表现为干净的、去抖后的电平；时序报告中无由外部异步输入引起的违例。

> 待本地验证：综合报告的具体查看路径依厂商工具而定；scoped 约束在 Vivado 2024.2 之前的版本对 Verilog 实例化场景才完全自动生效（见 sync 文档说明）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 scoped 约束里要 `set_property IOB FALSE`？

**参考答案**：默认情况下工具可能把第一级触发器吸收进 IOB（紧贴输入引脚），导致第一级和第二级落在不同区域、级间走线变长，亚稳态收敛时间被压缩、MTBF 下降。设 `IOB FALSE` 强制两级 FF 共处一个 slice，走线最短。

**练习 2**：如果一个外部信号你**既不确定它干不干净、又不确定它抖不抖**，最稳妥的做法是用 `olo_intf_sync` 还是 `olo_intf_debounce`？

**参考答案**：用 `olo_intf_debounce`。它内部已包含 `olo_intf_sync`，既同步又消抖；哪怕信号其实没抖，消抖也只是引入一个可接受的延迟，不会出错。反过来若只用 `olo_intf_sync` 而信号真有抖动，则抖动会原样进入内部逻辑。

---

## 5. 综合实践

把本讲三个实体串成一个完整的「外部信号调理 + 自检」小系统，对应讲义规格要求的核心实践任务。

**任务**：设计一个顶层，包含两路外部输入——一个**模拟抖动的按钮**和一个**已知频率的待测时钟**——并用本讲实体分别处理它们。

**建议结构**（示例代码，非项目原有文件）：

```vhdl
-- 1) 按钮：同步 + 消抖（LOW_LATENCY），再做上升沿检测得到单周期脉冲
i_btn : entity olo.olo_intf_debounce
    generic map (ClkFrequency_g => 100.0e6, DebounceTime_g => 20.0e-3, Mode_g => "LOW_LATENCY")
    port map (Clk => Clk, Rst => Rst, DataAsync => ButtonAsync_i, DataOut => ButtonClean);

-- 上升沿检测（ButtonClean 打一拍异或）
BtnPulse <= ButtonClean and not ButtonClean_d;

-- 2) 时钟测量：用已知的 Clk 测 ClkTest_i，期望它等于设计值（如 25 MHz）
i_meas : entity olo.olo_intf_clk_meas
    generic map (ClkFrequency_g => 100.0e6, MaxClkTestFrequency_g => 50.0e6)
    port map (Clk => Clk, Rst => Rst, ClkTest => ClkTest_i,
              Freq_Hz => ClkTestFreq_s, Freq_Valid => ClkTestFreqValid_s);
```

**验证步骤**：

1. **按钮侧**：在测试台里用类似 `olo_intf_debounce_tb` 中 `BouncPulse` 用例的写法，让 `ButtonAsync_i` 在前若干毫秒内反复翻转（模拟抖动），随后稳定为高。断言 `BtnPulse` 在抖动期间**不产生多个脉冲**、只在稳定后产生恰好一个单周期脉冲。
2. **时钟侧**：让 `ClkTest_i` 运行在一个已知频率（例如仿真里取较小整数避免长跑），等两次 `Freq_Valid` 后，断言 `ClkTestFreq_s` 与已知频率之差在允许范围内（参考 `checkFrequency` 的 ±1 容差思路）。
3. **停摆侧**：把 `ClkTest_i` 停掉，等一个以上秒周期，断言 `ClkTestFreq_s` 变为 0。
4. **约束**：为所有外部异步输入（`ButtonAsync_i`、`ClkTest_i`）写好 CDC 约束（AMD 用 scoped，其他手写 `set_max_delay`）。

**预期结果**：按钮侧每按一次（即使带抖动）只产生一个脉冲；时钟侧读数与已知频率一致（误差在容差内）；时钟停掉后读数为 0。

> 待本地验证：综合实践的完整运行结果取决于你本机的仿真器与时钟设置；本讲提供的是设计与断言思路，不声称已执行。

---

## 6. 本讲小结

- 外部信号与系统时钟无固定时间关系，进 FPGA 前必须过**同步器**，否则会引入亚稳态；`olo_intf_sync` 是 N 级（默认 2 级）同步器，并带齐了让所有厂商工具正确实现它的综合属性。
- 同步器**只能用于相互独立的单比特**，不能传多位总线——后者要用 base 区域的 `olo_base_cc_*`。
- 机械按钮/开关会抖动，`olo_intf_debounce` 用「`olo_intf_sync` 同步 + 共享 Tick 预分频 + 每比特稳定计数器」一步完成同步与消抖，资源开销极低。
- 消抖有两种模式：`LOW_LATENCY` 边沿几乎立刻转发、但会把短脉冲拉宽；`GLITCH_FILTER` 必须稳定满 `DebounceTime_g` 才转发、能吃掉短毛刺。
- `olo_intf_clk_meas` 用已知主时钟产生精确 1 Hz 秒脉冲，在待测时钟域数 1 秒内的周期数即得频率；内部含两次跨域，还能检测待测时钟停摆并报 0 Hz。
- 三个实体是叠加关系：消抖内含同步；约束上 AMD 靠 scoped `.tcl` 自动覆盖（含关键的 `IOB FALSE`），其他厂商需手写 `set_max_delay`。

---

## 7. 下一步学习建议

- **深入跨时钟域**：本讲的 `olo_intf_clk_meas` 已经用到了 `olo_base_cc_pulse` 与 `olo_base_cc_simple`。建议接着学 **u4-l1（跨时钟域原理与约束）** 和 **u4-l2（cc_pulse/cc_simple/cc_status）**，搞清这些 CDC 实体的翻转协议与复位穿越约定。
- **strobe 家族**：消抖与测频都依赖 `olo_base_strobe_gen` 产生节拍。可在 **u5-l1（时序相关实体）** 里系统了解 strobe 生成、分频与速率限制。
- **intf 区域后续**：本讲是 intf 区域的第一讲。接下来 **u7-l2（UART）**、**u7-l3（SPI 主从）**、**u7-l4（I2C 主机）** 会进入更复杂的外设协议，它们同样建立在「外部信号先同步」的基础上，并大量复用 AXI-S 握手。
- **推荐阅读源码**：对照阅读 [`olo_base_cc_simple.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_simple.vhd) 与 [`olo_base_cc_pulse.vhd`](https://github.com/open-logic/open-logic/blob/ecca8af95295e798b8a3bc6610cb52be9688ae01/src/base/vhdl/olo_base_cc_pulse.vhd)，理解 `olo_intf_clk_meas` 里那两次跨域在底层是怎么实现的。
