# 设备端三大模式：VNA 扫描、频谱分析与信号源

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出固件侧 VNA、频谱分析（SA）、信号源（Generator）三种模式共享的「包入口 → 中断取数 → 工作函数」骨架，以及 `Hardware.cpp` 在其中扮演的分发枢纽角色。
2. 完整叙述一次 VNA 扫描在固件侧的时序：`VNA::Setup` 预编程 → `FPGA::StartSweep` → NewData 中断取数 → `MeasurementDone` 聚合 → `PassOnData` 上报 → `SweepHalted` 现场修改 → 扫描结束 `Work` 重启。
3. 解释「零扫宽/点频模式」（zerospan）与普通扫描在固件里的唯一分叉位置。
4. 描述 SA 模式为何由 MCU 逐点编排扫描、信号识别（Signal ID）如何用多组本振配置甄别真假信号、检波器如何在固件里聚合数据。
5. 说明 Generator 模式为什么是最简单的模式（无测量、无中断），以及 Trigger 模块在多机同步与外部参考中的接线作用。

## 2. 前置知识

### 2.1 回顾：固件的两个执行环境

u5-l1 讲过，这个固件只有两个 FreeRTOS 任务，实时工作几乎都在**中断上下文**里完成。本讲的三个模式正是这一原则的体现：

- **USB 收包**：`App_Process` 任务里按包类型分发（u4-l1 讲过 `xTaskNotifyFromISR` 交接）。
- **测量数据**：FPGA 完成一个测量点后拉高中断线，MCU 的 EXTI（外部中断）被触发，随后在中断上下文里通过 SPI 把结果读回来。

### 2.2 回顾：STM::DispatchToInterrupt

u5-l1 讲过 `STM::DispatchToInterrupt`：把一个函数指针投递到 COMP4 软件中断里执行，用来规避「中断优先级数字小于 5 禁调内核 API」的 FreeRTOS 红线，同时让多个敏感操作（如 I2C 配时钟芯片、触发线检查）串行化、互不打断。本讲你会看到它被大量使用，例如 [VNA.cpp:398](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L398) 和 [VNA.cpp:444](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L444)。

### 2.3 回顾：stage（阶段）与 S 参数的拼装

u4-l2 讲过：`SweepSettings` 里的 `stages`、`port1Stage`、`port2Stage` 描述「一个测量点内激励端口的编排」。双端口 VNA 测 S11/S21 时激励端口 1，测 S12/S22 时激励端口 2，两类测量合起来构成两个 stage。固件每个 stage 采样一次，把三个接收机（端口 1、端口 2、参考）的读数连同 stage 号一起上报，S 参数的比值运算在 PC 端完成。

### 2.4 新术语：反压（backpressure）

当生产者（固件产生测量数据）比消费者（USB 主机取走数据）快时，需要一种机制让生产者「停下来等一等」，这就是反压。VNA 模式通过周期性暂停扫描 + 检查 USB 发送缓冲剩余空间来实现，本讲 4.2 节会精读。

### 2.5 新术语：信号识别（Signal ID）

频谱仪把信号下变频到中频再测量，混频镜像、本振泄漏等假象也会在特定配置下「看起来像信号」。Signal ID 的思路：真信号在任何本振配置下都会出现在同一频率上，假象不会。所以对每个频点用多组不同的本振/采样率配置各测一次，取**最小值**——假象在某些配置下消失，最小值就把它们滤掉了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Software/VNA_embedded/Application/VNA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp) | VNA 扫描模式全部逻辑：设置、逐点取数、暂停恢复、上报 |
| [Software/VNA_embedded/Application/SpectrumAnalyzer.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp) | 频谱分析模式：MCU 编排的逐点扫描、信号识别、检波 |
| [Software/VNA_embedded/Application/Generator.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp) | 信号源模式：纯配置，无测量回路 |
| [Software/VNA_embedded/Application/Trigger.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp) / [Trigger.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.hpp) | 触发输入/输出线与四种同步模式 |
| [Software/VNA_embedded/Application/Hardware.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp) | 中断分发枢纽：`ReadComplete`/`HW::Work` 按 `activeMode` 分派给各模式 |
| [Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp) / [FPGA.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp) | FPGA 寄存器读写、SPI 取样、扫描启停 |
| [Software/VNA_embedded/Application/App.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp) | USB 包到模式 `Setup()` 的入口分发 |

## 4. 核心概念与源码讲解

### 4.1 模式的公共骨架：包入口、中断分发与工作函数

#### 4.1.1 概念说明

三种设备端模式**不是三个任务、三套循环**，而是同一套「回调集合」的三份实现。每个模式对外只暴露少数几个函数：

- `Setup(...)`：接收 PC 下发的设置包，完成硬件配置并启动测量；
- `MeasurementDone(result)`：FPGA 报告「一个采样完成了」，在中断上下文里被调用，负责累积数据；
- `Work()`：当 `MeasurementDone` 返回 `true` 时被调度，做「不在紧急中断里做」的收尾工作（上报状态、启动下一轮）；
- `Stop()`：停止扫描、清除 `active` 标志。

`Generator` 模式连 `MeasurementDone`/`Work` 都没有——它只输出信号，不测量。谁在什么时机调用这些函数？答案是 `Hardware.cpp` 里一个以 `activeMode` 为开关的分发枢纽。理解这个骨架，三个模式的源码就变成了同一模板的三次填充。

#### 4.1.2 核心流程

一次模式切换和测量的完整时序：

```text
PC 下发设置包（SweepSettings / SpectrumAnalyzerSettings / Generator）
   │
   ▼ (USB 中断 → App_Process 任务)
App.cpp::USBPacketReceived 按 PacketType 分发
   ├── SweepSettings            → VNA::Setup(s)        + Ack
   ├── SpectrumAnalyzerSettings → SA::Setup(s)         + Ack
   └── Generator                → Generator::Setup(g)  + Ack
                                          │
                                          ▼ 硬件配置（各模式自己完成）
                              FPGA::StartSweep()  ←—— 扫描正式开始
                                          │
   ┌──────────────── FPGA 自主逐点扫描（MCU 等中断）────────────────┐
   │  FPGA 完成一个点 → 拉高 FPGA_INTR 引脚                          │
   │      │ EXTI 上升沿                                              │
   │      ▼                                                         │
   │  Hardware.cpp::FPGA_Interrupt                                  │
   │      └→ FPGA::InitiateSampleRead(ReadComplete)   [SPI DMA 40B] │
   │           └→ HAL_SPI_TxRxCpltCallback（DMA 完成）              │
   │                ├─ status&0x0004(NewData)  → ReadComplete       │
   │                │    └→ 按 activeMode 调 MeasurementDone        │
   │                │         └─ 返回 true → 调度 HW::Work          │
   │                └─ status&0x0010(SweepHalted) → HaltedCallback  │
   │                     └→ VNA::SweepHalted()（仅 VNA 模式）       │
   └─────────────────────────────────────────────────────────────────┘
```

注意两条中断路径的汇合点：无论 NewData 还是 SweepHalted，都来自同一次 40 字节 SPI 读取返回的**状态字**（[FPGA.cpp:329-334](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L329-L334)），FPGA 用状态位告诉 MCU「这次中断是为什么拉起来的」。

#### 4.1.3 源码精读

**入口分发**。`App.cpp` 在 `App_Process` 任务里按包类型调对应的 `Setup`：

- [App.cpp:L126-L131](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L126-L131)：收到 `SweepSettings` 包后调用 `VNA::Setup(recv_packet.settings)` 并回 `Ack`。
- [App.cpp:L146-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L146-L152)：`Generator` 包 → `Generator::Setup`。
- [App.cpp:L153-L159](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L153-L159)：`SpectrumAnalyzerSettings` 包 → `SA::Setup`。
- [App.cpp:L186-L192](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L186-L192)：`InitiateSweep` 包（standby 唤醒）→ `VNA::InitiateSweep`。

**EXTI 注册**。FPGA 的中断线接到 MCU 的一个 GPIO，`HW::Init` 里注册上升沿回调：

[Hardware.cpp:L170](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L170) 注册 `FPGA_Interrupt`；[Hardware.cpp:L73-L76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L73-L76) 是它的实现——发起 SPI 读取并记录时间戳 `lastISR`（零扫宽模式靠它拿到微秒级时间，见 4.2.3）。

**SPI 取样**。[FPGA.cpp:L286-L305](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L286-L305) 发出命令字 `0xC0` 并用 DMA 收 40 字节；[FPGA.cpp:L316-L335](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L316-L335) 在 DMA 完成回调里把 6 个 48 位定点数拼装成 `SamplingResult`（结构定义在 [FPGA.hpp:L36-L42](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L36-L42)：三路接收机的 I/Q 加 13 位点号与 3 位 stage 号），再按状态位分发。

**分发枢纽**。[Hardware.cpp:L53-L71](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L53-L71) 的 `ReadComplete` 按 `activeMode` 调用对应模式的 `MeasurementDone`，若返回 `true` 就用 `STM::DispatchToInterrupt(HW::Work)` 请求工作函数；[Hardware.cpp:L43-L51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L43-L51) 的 `HaltedCallback` 则只服务 VNA 模式的 `SweepHalted`；[Hardware.cpp:L78-L92](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L78-L92) 的 `HW::Work` 同样按 `activeMode` 分派 `Work`。

**模式切换**。[Hardware.cpp:L217-L240](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp#L217-L240)：`HW::SetMode` 先 `Stop()` 旧模式、重跑 `HW::Init()` 归位所有射频部件、`SetIdle()`，最后才改 `activeMode`。这解释了为什么每个模式的 `Setup` 开头都有一句 `HW::SetMode(...)`——它既完成清理也完成分发目标的切换。

#### 4.1.4 代码实践

**实践目标**：不看讲义，独立验证「USB 包 → 中断 → 模式回调」这条链，把每个交点的函数与行号填进表格。

**操作步骤**：

1. 打开 [App.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp)，找到 `SweepSettings` 分支，记下调用 `VNA::Setup` 的行号。
2. 打开 [Hardware.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Hardware.cpp)，依次定位 `FPGA_Interrupt`、`ReadComplete`、`HW::Work` 三个函数。
3. 打开 [FPGA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp)，定位 `InitiateSampleRead` 和 `HAL_SPI_TxRxCpltCallback`，确认 `status & 0x0004` 与 `status & 0x0010` 两个分支分别通向哪里。
4. 把上述交点整理成「事件 → 函数 → 文件:行号 → 下一跳」四列表格。

**需要观察的现象**：你会得到一条完全不含轮询的链——每个环节都是被上一个事件「推」起来的。唯一例外是 SA 模式的 `Work` 末尾主动启动下一点（见 4.3）。

**预期结果**：表格大约 7 行，覆盖 设置包入口、EXTI、SPI DMA 启动、SPI 完成回调、NewData 分支、SweepHalted 分支、Work 调度。本实践为纯源码阅读，无需硬件，「待本地验证」不适用。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Generator` 模式在 `ReadComplete` 和 `HW::Work` 的 switch 里都走 `default` 分支？

**答案**：`HW::Mode::Generator` 不产生测量数据，FPGA 不做采样、不触发 NewData 中断，`Generator::Setup` 只配置信号源输出（[Generator.hpp:L7-L8](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.hpp#L7-L8) 的注释也说明它是对手动模式的封装，无额外函数）。因此分发枢纽永远不会为它调用 `MeasurementDone`/`Work`。

**练习 2**：`MeasurementDone` 与 `Work` 的职责为什么要分开？

**答案**：`MeasurementDone` 在 SPI DMA 完成回调（中断上下文）里执行，只做轻量的数据累积和判断，尽快返回；`Work` 经 `STM::DispatchToInterrupt` 排队执行，承担可能耗时的操作（读温度、更新参考、重启扫描）。这符合 u5-l1 讲的「中断里只做最少的事」原则。

**练习 3**：若 FPGA 与 MCU 的点号计数失配（例如中断丢失），固件会怎样？

**答案**：[VNA.cpp:L369-L373](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L369-L373) 校验 `result.pointNum != pointCnt || result.stageNum != stageCnt` 时打 WARN 日志并 `FPGA::AbortSweep()` 放弃本次数据，防止错位点号污染整条迹线。

### 4.2 VNA 扫描模式：一次完整扫描的时序

#### 4.2.1 概念说明

VNA 模式的核心设计是「**FPGA 自主扫描、MCU 预编程 + 按需干预**」：

- `VNA::Setup` 阶段，MCU 把**整条扫描曲线每个点**的 PLL 寄存器值、衰减器值预先算好写入 FPGA（每个点一条「扫描配置」）。此后 FPGA 自己逐点推进频率、切换端口、触发采样，MCU 完全不参与推进。
- MCU 只响应两类中断：NewData（取一个点的结果）和 SweepHalted（扫描在某个点前主动停了下来，等 MCU 处理现场）。
- 「halt（暂停）」是这套设计的灵魂：有些事情 FPGA 干不了，必须在某个点开始前由 MCU 处理——低波段要改用 Si5351 做信号源、杂散抑制要换 PLL 参考频率、二本振要偏移、USB 缓冲快满要反压。MCU 在预编程时给这些点打上 `halt` 标志，FPGA 扫到那里就停下等指令。

这套「预编程 + 暂停点」机制的收益：稳态时（高波段、无杂散问题）两点之间 MCU 零开销，扫描速度只受 settling time（稳定时间）限制。

#### 4.2.2 核心流程

`VNA::Setup` 的决策要点（哪个点需要 halt）：

```text
对扫描的每个点 i：
    needs_halt = false
    若 频率 < 波段切换频率(25MHz)：needs_halt = true     # 低波段用 Si5351 当源
    若 i == 0：needs_halt = true                          # 首点，等 PLL 稳定
    若 setPLLFrequencies() 报告需要换参考：needs_halt = true  # 杂散抑制
    若 该点需要二本振偏移且开了抑制：needs_halt = true
    若 上一点低波段而本点高波段：needs_halt = true        # 换源
    若 连续无 halt 点数 > 40：needs_halt = true           # USB 反压
    FPGA::WriteSweepConfig(i, ..., needs_halt)
```

运行期的循环：

```text
FPGA::StartSweep()
loop:
    NewData 中断 → MeasurementDone:
        按 stage 累积 P1/P2/Ref 三路读数进 data
        stageCnt++ ；若 stageCnt > settings.stages：   # 一个点测完
            DispatchToInterrupt(PassOnData)            # 打包成 VNADatapoint 发 USB
            pointCnt++
            若 pointCnt >= points：return true         # 一轮扫描结束 → 触发 Work
    SweepHalted 中断 → SweepHalted:
        （低波段源切换 / PLL 参考切换 / 2.LO 偏移 / ADC 采样率切换）
        等 USB 缓冲 ≥ 预留值 → FPGA::ResumeHaltedSweep()
Work（一轮结束）:
    更新参考、可选上报 DeviceStatus
    standby ? 等待 InitiateSweep : FPGA::StartSweep()   # 无限重扫
```

每点样本数由 IF 带宽决定。ADC 以固定速率 \( f_{\mathrm{ADC}} \) 采样，DFT 积分时间越长带宽越窄：

\[ N_{\mathrm{samples}} = \left\lceil \frac{f_{\mathrm{ADC}}}{\mathrm{IF_{BW}}} \right\rceil_{16} \]

向上取整到 16 的倍数是因为「16 个采样分布在 5 个二中频周期」的硬件约束（[VNA.cpp:L170-L177](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L170-L177)），实际带宽再由 \( f_{\mathrm{ADC}} / N \) 反算存入 `actualBandwidth`。

频率轴的生成有两种：线性扫频按比例内插；对数扫频预计算相邻点比值

\[ m = \left( \frac{f_{\mathrm{stop}}}{f_{\mathrm{start}}} \right)^{1/(n-1)} \]

之后每点只需乘一次 `logMultiplier`（[VNA.cpp:L59-L76](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L59-L76)、[VNA.cpp:L167](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L167)）——这是嵌入式中典型的「用一次指数换每点一次乘法」优化。

#### 4.2.3 源码精读

**(a) Setup 的框架配置**。[VNA.cpp:L143-L179](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L143-L179)：先 `Stop` 旧扫描、`HW::SetMode(VNA)`、复位 PLL 参考索引；然后 `FPGA::SetMode(FPGA)` 把 SPI 总线切给 FPGA，写 ADC 预分频器和 DFT 相位增量两个基础寄存器；点数被夹在 `FPGA::MaxPoints = 4501`（[FPGA.hpp:L9](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp#L9)）以内、驻留时间夹在设备上限以内；最后按上面公式算每点样本数并写 settling time（`dwell_time + PLL 稳定延迟`）。

**(b) 幅度与二本振的预置**。[VNA.cpp:L184-L215](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L184-L215)：用 u5-l2 讲过的 `HW::GetAmplitudeSettings` 分别算高/低波段的衰减器与功率档，高波段档位通过 `FPGA::WriteMAX2871Default` 预写进 FPGA（扫描期间 FPGA 自己换功率档，不经 MCU）；三路二本振由 Si5351 的 PLL B 统一给出（u5-l2 讲过三路同源保证同相）。

**(c) 零扫宽判定**——本讲实践任务的关键。[VNA.cpp:L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L217)：

```cpp
zerospan = (s.f_start == s.f_stop) && (s.cdbm_excitation_start == s.cdbm_excitation_stop);
```

起止频率相同**且**起止功率相同才算零扫宽（点频）模式。这个布尔值一路传到 `MeasurementDone`，决定数据包里装「时间」还是装「频率/功率」。

**(d) 逐点预编程循环**。[VNA.cpp:L224-L293](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L224-L293)：对每个点算频率（含 `Cal::FrequencyCorrectionToDevice` 设备级频率校正，u5-l3）和功率，判定 `needs_halt`（判据见 4.2.2 伪代码，其中 USB 反压阈值 `maxPointsBetweenHalts = 40` 与包大小预算定义在 [VNA.cpp:L48-L51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L48-L51)：预留 `(40+2)×(66+8)` 字节），最后 `FPGA::WriteSweepConfig`（实现在 [FPGA.cpp:L212-L275](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L212-L275)，把 PLL 寄存器关键字段打包进 FPGA 的扫描配置 RAM）。注意循环里调用的 `Source.SetFrequency`/`LO1.SetFrequency` 只改 MCU 侧影子寄存器（u5-l2 的「只算不写」范式），真正写芯片发生在 FPGA 扫到该点时由 FPGA 代发，或暂停点由 MCU 补写。

**(e) 启动**。[VNA.cpp:L304-L330](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L304-L330)：设 Kaiser 窗、使能混频器/放大器/PLL 等外设、`FPGA::SetupSweep(s.stages, s.port1Stage, s.port2Stage, syncMode != 0, syncMaster)` 把 stage 编排写进 `SweepSetup` 寄存器（位装配见 [FPGA.cpp:L146-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L146-L156)）、`Trigger::SetMode` 配同步模式，最后使能 NewData 与 SweepHalted 两类中断并 `FPGA::StartSweep()`（standby 设置则挂起等待 `InitiateSweep` 包，[VNA.cpp:L334-L341](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L334-L341)）。`StartSweep` 的实现朴素得惊人——AUX3 引脚拉低再拉高（[FPGA.cpp:L338-L346](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L338-L346)）。

**(f) 取数与零扫宽分叉**。[VNA.cpp:L365-L408](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L365-L408) 的 `MeasurementDone`：

- [L375-L377](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L375-L377) 把三路读数加入 `data`，注意第三个调用给参考接收机的**源掩码**是 `Port1 | Port2 | Reference` 按位或——这正是 u4-l2 讲的「描述掩码」，PC 端据此知道参考读数对两个激励端口都有效。
- [L379-L392](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L379-L392) **就是零扫宽与普通扫描的唯一分叉**：zerospan 时填 `data.us`（用 `HW::getLastISRTimestamp()` 的微秒时间戳，首点记基准，之后填差值），X 轴是时间；否则填 `data.frequency` 与 `data.cdBm`，X 轴是频率/功率。除这两个字段的来源外，采样、stage 聚合、上报路径完全同一条——零扫宽并没有独立的扫描机制，只是 FPGA 在同一频率上反复测、MCU 给数据贴时间标签。
- [L394-L406](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L394-L406) stage 计数推进；一个点的所有 stage 测完时 `STM::DispatchToInterrupt(PassOnData)` 发包并清空缓冲，扫完全部点则 `return true` 触发 `Work`。

**(g) 上报**。[VNA.cpp:L355-L363](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L355-L363) 的 `PassOnData` 把 `data` 装进 `PacketType::VNADatapoint` 调 `Communication::Send`（u4-l2 讲过该包豁免 CRC 以保吞吐）。

**(h) 暂停点处理与反压**。[VNA.cpp:L437-L555](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L437-L555) 的 `SweepHalted` 把一大段逻辑包进 `STM::DispatchToInterrupt`（注释解释：恢复暂停需要 I2C 操作 Si5351，可能与触发输入检查冲突，串行化防互踩）。内部依次：低波段点直接用 Si5351 当源（[L452-L491](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L452-L491)，含一个巧妙的 ADC 混叠检查：一本振泄漏混频后若折叠进二中频带宽就切换备用采样率 914285.7143 Hz，常量与 `static_assert` 见 [VNA.cpp:L42-L46](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L42-L46)）；杂散抑制的参考切换与二本振偏移（[L493-L523](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L493-L523)）；最后 [L536-L553](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L536-L553) 的反压：`usb_available_buffer()`（声明于 [usb.h:L22](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/USB/usb.h#L22)）够就立即 `FPGA::ResumeHaltedSweep()`（命令字 `0x2000`，轮询直到暂停状态位清零，[FPGA.cpp:L443-L460](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L443-L460)）；不够则最多等 1 秒，超时判定「出了更严重的问题」，清缓冲、中止扫描、回 Idle。

**(i) 一轮结束**。[VNA.cpp:L410-L435](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L410-L435) 的 `Work`：更新参考（外部参考模式下跳过）、`StopSweep`（为读 PLL 温度临时保源使能）、按需发 `DeviceStatus` 包、standby 挂起或重启下一轮扫描。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：为 `VNA::Setup → 扫描运行 → 一轮结束` 写一份伪代码流程图，标出**每一个**与 FPGA 交互、与 Communication 上报的交点，并回答零扫宽与普通扫描的分叉位置。

**操作步骤**：

1. 通读 [VNA.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp) 全文（574 行），按函数切分：`Setup`、`InitiateSweep`、`PassOnData`、`MeasurementDone`、`Work`、`SweepHalted`、`Stop`。
2. 用两种记号标注：`【F】` 表示对 `FPGA::` 命名空间的调用（WriteRegister/WriteSweepConfig/SetupSweep/StartSweep/ResumeHaltedSweep/AbortSweep…），`【C】` 表示对 `Communication::Send` 的调用。
3. 画三层流程图：第一层「Setup 阶段」（从 `HW::SetMode` 到 `StartSweep`），第二层「稳态循环」（NewData → MeasurementDone → PassOnData），第三层「暂停点」（SweepHalted → ResumeHaltedSweep）。
4. 单独描红两处：`zerospan` 的赋值行与其唯一使用处（`MeasurementDone` 里 if/else），确认中间所有环节对两种模式无差别。
5. 把结果与你 4.1.4 的表格合并，得到 VNA 模式的完整交点清单。

**需要观察的现象**（交点统计应大致符合）：

- `【F】` 交点：Setup 阶段约 8 类（SetMode、WriteRegister×2、SetNumberOfPoints、SetSamplesPerPoint、SetSettlingTime、WriteMAX2871Default、WriteSweepConfig（循环 N 次）、SetupSweep、EnableInterrupt×2、StartSweep）；运行期 3 个（InitiateSampleRead 由枢纽调、ResumeHaltedSweep、AbortSweep）；Work 里 2 个（StopSweep、StartSweep）。
- `【C】` 交点：`PassOnData` 的 VNADatapoint（每点一次）与 `Work` 里的 DeviceStatus（每轮一次）。Ack 由 App.cpp 统一回，不在 VNA.cpp 内。

**预期结果**：流程图应清晰呈现「Setup 一次预编程 + 运行期零 MCU 推进 + 暂停点按需干预」的结构；零扫宽分叉唯一存在于 [VNA.cpp:L379-L392](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L379-L392) 的 if/else，分支体只差 `us`（时间戳）与 `frequency + cBm` 两个字段的填充。本实践为源码阅读型，无需硬件；若想实机观察，可在 GUI 中把起止频率设成同一值、起止功率相同，确认迹线 X 轴变成时间（行为与固件分支一致，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 USB 反压阈值是「每 40 个无暂停点强制暂停一次」，而不是等到缓冲满了才停？

**答案**：暂停点之间存在滞后——从 MCU 发现缓冲不足到扫描真正停下，FPGA 还会跑完当前点并产生数据。40 是按最坏情况预算倒推的：[VNA.cpp:L48-L51](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L48-L51) 预留 `(40+2)` 个满配双端口数据点（66 字节净荷 + 8 字节 USB 开销）的空间，保证下一个暂停点到达前缓冲绝不溢出。等满了才停就晚了。

**练习 2**：`VNA::Work` 里为什么注释强调「不要在此重置 unlevel 标志」？

**答案**：unlevel（功率达不到设定值）标志是在 `Setup` 时对扫描中点频率一次性计算的（[VNA.cpp:L184-L194](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L184-L194)），对整轮扫描有效；`Work` 每轮结束都会执行，若在那里清除，标志会在下一轮重新算出前短暂丢失，PC 端看到的状态会闪烁。SA 模式则相反，在每轮扫描结束时清（[SpectrumAnalyzer.cpp:L488-L491](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L488-L491)），因为它的功率设置逐点变化。

**练习 3**：扫描期间 Si5351（低波段源、二本振）的修改为什么都发生在 `SweepHalted` 而不是 `MeasurementDone`？

**答案**：修改顺序要求「先改配置、后测该点」。`MeasurementDone` 在一个点**测完之后**才被调用，此时 FPGA 已经准备冲向下一个点；只有暂停点（halt）能保证 FPGA 停在下一个点之前，给 MCU 留出改时钟芯片（I2C，毫秒级）的窗口。`SweepHalted` 末尾的 `ResumeHaltedSweep` 就是「改完了，放行」。

### 4.3 频谱分析模式：逐点编排、信号识别与检波

#### 4.3.1 概念说明

SA 与 VNA 的第一个本质差异：**扫描由谁推进**。VNA 把整条曲线预编程进 FPGA；SA 却在 Setup 里写 `FPGA::SetNumberOfPoints(1)`——每次只让 FPGA 测一个点，测完停下，由 MCU 在 `Work` 里决定下一个点的全部射频配置再启动。原因有二：

1. 频谱扫描的点数 \( 2\cdot\mathrm{Span}/\mathrm{RBW} \) 轻松超过 `FPGA::MaxPoints = 4501`（如 100 kHz RBW 扫 1 GHz 就是两万点），FPGA 装不下。
2. SA 每个点需要的 LO 配置不是预计算能覆盖的：Signal ID 要求同一频点用多组不同的 LO 组合反复测，还有跟踪源（TG）频率要跟着走。

第二个差异：**数据的形态**。VNA 每点上报三个接收机的复数读数（S 参数在 PC 端算）；SA 直接在固件里算出**幅度标量**，经过 Signal ID 取最小值、检波器按 bin 聚合后才上报，一个显示点一个包。

#### 4.3.2 核心流程

RBW（分辨率带宽）决定每点样本数，窗型引入修正系数 \( k_w \)（None=0.89、Hann=2.23、 flattop 代入 1.44、Kaiser 代入 3.77，见代码数组）：

\[ N = \left\lceil \frac{f_{\mathrm{ADC}} \cdot k_w}{\mathrm{RBW}} \right\rceil_{16}, \qquad \mathrm{RBW_{actual}} = \frac{f_{\mathrm{ADC}} \cdot k_w}{N} \]

内部测量点数按 2 倍过采样取整并吸附到显示点数的整数倍：

\[ n_{\mathrm{int}} = \frac{2\,\mathrm{Span}}{\mathrm{RBW_{actual}}}, \qquad n_{\mathrm{int}} \mathrel{+}= n_{\mathrm{disp}} - (n_{\mathrm{int}} \bmod n_{\mathrm{disp}}), \qquad \mathrm{binSize} = \frac{n_{\mathrm{int}}}{n_{\mathrm{disp}}} \]

每个显示点（bin）聚合 `binSize` 个内部点的检波结果。

Signal ID 状态机（`signalIDstep`）对每个内部点依次执行，配合 [SpectrumAnalyzer.cpp:L294-L306](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L294-L306) 按 RBW 选出的步数（RBW ≤ 10 kHz 用 6 步、否则 8 步）：

| step | 手段 | 目的 |
| --- | --- | --- |
| 0 | 默认 LO + 默认采样率 | 基准测量（并更新跟踪源） |
| 1 | 一本振移到信号另一侧 | 镜像频率若跟着搬家即为假象 |
| 2 | 二本振移到另一侧 | 二中频镜像甄别 |
| 3 | 一、二本振同时移侧 | 组合假象甄别 |
| 4+ | 换 ADC 采样率（预分频器 132/156 或 126/130/144/176） | ADC 混叠假象甄别 |

（step 1/3 里若目标频率超出 LO 下限，代码直接 `signalIDstep++` 跳过该步。）

完整循环：

```text
SA::Setup → StartNextSample()   # 配好第 0 点 step 0 的全部射频 + FPGA::StartSweep
loop:
    NewData → SA::MeasurementDone:
        读 DFT 结果（或单点复数幅度），除以 sampleNum
        与该 bin 已存最小值比较，取更小者          # Signal ID 核心
        return true
    HW::Work → SA::Work:
        若还有未做的 signal ID step：step++，StartNextSample()  # 同一点换 LO 再测
        否则（该内部点定案）：
            按 bin 聚合进检波器（峰/谷/采样/平均/Normal）
            若凑满一个 bin：发 SpectrumAnalyzerResult 包，频率 = 内插显示频率
            pointCnt += DFTpoints；扫完归零、清 unlevel
            step 归 0，StartNextSample()                  # 下一个内部点
```

「Normal 检波器」是传统频谱仪的折中：奇数 bin 取正峰、偶数 bin 取负峰交替（[SpectrumAnalyzer.cpp:L391-L393](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L391-L393)），兼顾噪声包络与窄信号可见性。

#### 4.3.3 源码精读

**(a) Setup 的标量计算**。[SpectrumAnalyzer.cpp:L235-L268](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L235-L268)：停旧扫描、`HW::SetMode(SA)`、`SetNumberOfPoints(1)`（单点模式的注释就写明「整条扫描太长，FPGA 一次装不下，由 MCU 逐点启动」）、按上面公式算 `sampleNum/actualRBW/points/binSize`（窗系数数组与 tek.com 的窗口函数引用在 [L248-L259](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L248-L259)），`zerospan = (s.f_start == s.f_stop)`——注意 SA 的零扫宽**只看频率**，不含功率条件（对照 VNA 的判定，这是两模式的细节差异）。

**(b) 硬件与 DFT 分支**。[SpectrumAnalyzer.cpp:L276-L323](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L276-L323)：使能两路二本振与（可选的）低波段源（跟踪源用）；`FPGA::SetupSweep` 的 stage 参数这里退化为 TG 端口选择；`Trigger::SetMode` 同样来自 `syncMode`。`UseDFT` 打开时限制 DFT 点数为 `min(30000/spacing, FPGA::DFTbins=96)`（DFT 一次看一小段带宽，太宽会把 ADC 抗混叠滤波器的滚降看进数据），并**关掉 NewData 中断**改听 DFTReady；不用 DFT 则每点只测一个频点、保留 NewData。

**(c) StartNextSample 状态机**。[SpectrumAnalyzer.cpp:L58-L233](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L58-L233)：先算本内部点频率（含设备级频率校正），再按 `signalIDstep` 选择 LO 组合（上表；step 0 内 [L82-L136](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L82-L136) 还负责跟踪源频率更新：低波段用 Si5351、高波段遍历两个 PLL 参考频率挑频差最小的一个——因为单一参考下 MAX2871 的分数分频不是所有频率都能精确命中，偏差超过 `actualRBW/20` 才继续找）；[L190-L204](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L190-L204) 把一本振的实际频差转嫁给二本振补偿（u5-l2 讲过的 LO 精度兜底策略）；[L205-L232](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L205-L232) 配 DFT、写单点扫描配置、首次采样时先手动 `Source.Update()/LO1.Update()` 让 PLL 提前锁定 20ms，最后 `FPGA::StartSweep()`。

**(d) MeasurementDone 的最小值保持**。[SpectrumAnalyzer.cpp:L332-L373](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L332-L373)：先 `AbortSweep`（单点测完就停，与 VNA 的连续扫描相反）；DFT 模式逐 bin 读 `FPGA::ReadDFTResult`，非 DFT 模式取复数幅度；都除以 `sampleNum` 归一化；`negativeDFT` 时 bin 序号反转（LO 移侧后频谱在二中频里是倒序的，[L30](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L30) 的注释解释了这个标志）；[L358-L363](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L358-L363) 与已存值比较取小——这就是 Signal ID 的落点。永远 `return true` 把后续决策交给 `Work`。

**(e) Work 的检波与上报**。[SpectrumAnalyzer.cpp:L375-L500](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L375-L500)：

- [L380](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L380) 判断 Signal ID 是否做完；没做完走 [L492-L499](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L492-L499)：`signalIDstep++` 后立即 `StartNextSample()` 换配置再测同一点。
- 做完则 [L382-L471](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L382-L471) 逐 bin 过检波器（五种 Detector 定义在 [SpectrumAnalyzer.hpp:L8-L14](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.hpp#L8-L14)），`lastPointInBin` 时发 `SpectrumAnalyzerResult` 包。零扫宽分叉在 [L447-L459](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L447-L459)：同样用 `getLastISRTimestamp()` 填 `us`，否则按 `binIndex` 内插显示频率——结构与 VNA 如出一辙。
- [L460-L467](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L460-L467) 把线性幅度除以经验常数 253000000 近似换算，并按需乘上 u5-l3 讲过的 `Cal::ReceiverCorrection` 接收机幅度校正。
- [L475-L483](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L475-L483) 每 10 个内部点附送一次 `DeviceStatus`（临时使能源芯片才能读其温度）。
- [L485-L491](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L485-L491) 推进 `pointCnt`（DFT 模式一次跳 `DFTpoints` 个内部点），扫完归零并清 unlevel。

#### 4.3.4 代码实践

**实践目标**：亲手算一遍「RBW → 每点样本数 → 内部点数 → binSize」的换算，验证你对 Setup 标量计算的理解。

**操作步骤**：

1. 设定假想参数：SPAN 从 1 MHz 到 201 MHz（span=200 MHz），显示 501 点，RBW=10 kHz，Kaiser 窗（`WindowType=1`，系数 2.23），ADC 采样率以 `HW::getADCRate()` 的典型值 800 kHz 代入（确切值取决于设备配置，可先用 800k 做量级估算）。
2. 按公式手算：\( N = \lceil 800000 \times 2.23 / 10000 \rceil_{16} \)、\( \mathrm{RBW_{actual}} \)、\( n_{\mathrm{int}} \)、吸附后的点数与 `binSize`。
3. 打开 [SpectrumAnalyzer.cpp:L250-L266](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L250-L266) 逐行核对你的每一步对应哪行代码。
4. 再回答：若把 RBW 改为 1 kHz，`sampleNum` 会变成多少？会不会触顶 `HW::MaxSamples`（[L256-L258](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L256-L258) 的夹断）？触顶后 actualRBW 将比请求值大还是小？

**需要观察的现象**：手算值与代码逻辑一致；RBW 变小 10 倍时样本数变大 10 倍（直到被 MaxSamples 夹住），夹住后实际 RBW 无法再达到请求值——测量变「钝」但设备不会出错。

**预期结果**（按 800 kHz 采样率）：Kaiser 系数 2.23 → N = ⌈178.4⌉₁₆ = 184，actualRBW ≈ 9.7 kHz；内部点数 2×200M/9.7k ≈ 41237，吸附到 501 的整数倍，binSize ≈ 82+。精确数值依赖真实 ADC 采样率，标注「待本地验证」（有设备时可用 GUI 的数据包日志读回固件实际使用的值对照）。

#### 4.3.5 小练习与答案

**练习 1**：VNA 模式靠周期性 halt 做反压，SA 模式为什么不需要？

**答案**：SA 每个内部点测完都 `AbortSweep`，下一点要等 `Work` 里重新 `StartNextSample` 才开始——扫描本来就是逐点断续推进的，MCU 天然握着节流阀。只有 VNA 的连续 FPGA 自主扫描才需要额外的暂停机制防 USB 溢出。

**练习 2**：为什么 Signal ID 取**最小值**而不是平均值？

**答案**：真信号在所有 LO 配置下都真实存在，各步测到的幅度接近，取最小损失不大；假象（镜像/泄漏/混叠）只在部分配置下落在被测 bin 上，在「甄别它」的那一步它会消失，该步测得的是底噪。最小值恰好选中「假象消失」的那一步，从而把假象压到噪声水平。平均值会让残留假象抬高结果。

**练习 3**：SA 的零扫宽判定为什么不含功率条件？

**答案**：SA 模式没有「随频率变化的功率斜坡」概念——它的激励（若有）是跟踪源，设置里只有一个 `trackingPower` 标量。VNA 的扫描设置支持起止功率不同的功率斜坡（`cdbm_excitation_start/stop`），所以必须两者都相同才算真正的点频。（对比 [VNA.cpp:L217](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L217) 与 [SpectrumAnalyzer.cpp:L268](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L268)。）

### 4.4 Generator 模式与触发

#### 4.4.1 概念说明

Generator（信号源）是三个模式里唯一的「纯输出」模式：设置一次、长期稳定，没有扫描、没有测量、没有中断回调。它的全部智慧在两处：

1. **波段与幅度翻译**：把「某频率某 dBm」翻译成低波段（Si5351 直出）或高波段（MAX2871）加合适的衰减器、驱动强度与低通滤波器——复用 u5-l2 讲过的 `HW::GetAmplitudeSettings`。
2. **硬件覆盖（OverwriteHardware）**：平时射频开关/衰减器由 FPGA 在扫描时序里自动驱动，Generator 没有扫描，于是让 FPGA 进入「硬件覆盖」模式，由 MCU 下发的一组寄存器值**长期钉住**这些控制线。

Trigger（触发）模块则是**多机同步与外参考**的接线员。LibreVNA 的 FPGA 有一条触发输出线（FPGA→MCU 方向的 `FPGA_TRIGGER_OUT`，扫描同步事件发生时翻转）和一条触发输入线（MCU→FPGA 的 `FPGA_TRIGGER_IN`）。四种 `Trigger::Mode` 决定这两条线怎么用：

| Mode | 值 | 含义 |
| --- | --- | --- |
| Off | 0 | 不同步 |
| USB_GUI | 1 | 触发事件经 USB 报给 GUI（SetTrigger/ClearTrigger 包） |
| ExtRef | 2 | 外部参考模式：触发输入线由「参考是否可用」驱动 |
| Trigger | 3 | 硬件触发同步（固件注释标注当前硬件不支持该模式的输出处理） |

多机同步（u4-l2 讲过的 syncMode/syncMaster）就建立在这之上：主机设备的 FPGA 在每个扫描点边界翻转触发输出线，从机监听这条线（经 `FPGA::SetupSweep` 的 `synchronize` 参数把扫描挂到触发上），多台的点时钟由此对齐。

#### 4.4.2 核心流程

Generator::Setup 的决策树：

```text
activePort == 0 ?  → 全部关断（源芯片/放大器/射频/端口开关），return
频率校正 → GetAmplitudeSettings(cdbm, f, 修正开关, activePort==2)
频率 < 25MHz ?  → Si5351 LowbandSource 直出（低波段）
否则            → MAX2871：设功率、频率、Update()
                  低通滤波器按频率选：<900M→947k / <1.8G→1880 / <3.5G→3500 / 更高→无
FPGA::OverwriteHardware(衰减器, 低通, 波段, 端口1开关, 端口2开关)
使能放大器 / 源射频 / 端口开关
```

Trigger 的两条驱动路径：

```text
输出方向：FPGA 翻转 TRIGGER_OUT → EXTI 双沿中断 → DispatchToInterrupt(callback)
          → App 的 TriggerOutISR → 置任务通知位
          → App_Process 读 Trigger::GetOutput() 按 Mode 分发（报 USB / 控制参考输出）
输入方向：ExtRef 模式下 FreeRTOS 空闲钩子轮询 HW::Ref::available()
          → Trigger::SetInput(可用?)   # 直接写 GPIO，把参考状态喂给 FPGA
          普通模式下 SetInput(false)（VNA::Setup 与 HW::SetIdle 里复位）
```

#### 4.4.3 源码精读

**Generator::Setup 全文只有 65 行**。[Generator.cpp:L11-L27](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L11-L27)：先关三路二本振（信号源不需要接收链）、`HW::SetMode(Generator)`；`activePort == 0` 时关断一切直接返回——GUI 里「两个端口都不输出」就是这么落地的。[Generator.cpp:L29-L58](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L29-L58)：频率经 `Cal::FrequencyCorrectionToDevice` 校正后取幅度设置；低波段分支把 Si5351 的 LowbandSource 设到目标频率并使能；高波段分支关低波段源、使能源芯片、借 u5-l2 讲过的 `FPGA::SetMode(SourcePLL)` 把 SPI 路由到源 PLL，`SetPowerOutA`/`SetFrequency`/`Update` 三连后把 SPI 还给 FPGA，再按频率段选低通滤波器（950M/1.9G/3.5G 三段，更高频率干脆不滤波）。[Generator.cpp:L60-L64](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp#L60-L64)：`FPGA::OverwriteHardware` 一次钉住衰减器、低通、波段选择和两个端口开关（寄存器装配在 [FPGA.cpp:L397-L413](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L397-L413)），随后置 unlevel 标志、使能放大器/射频/端口开关。

**Trigger 模块**。[Trigger.hpp:L7-L12](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.hpp#L7-L12) 是 Mode 枚举（协议里的 `syncMode` 字段值与它一一对应，所以 [VNA.cpp:L314](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L314) 与 [SpectrumAnalyzer.cpp:L292](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/SpectrumAnalyzer.cpp#L292) 都是一句 `(Trigger::Mode) s.syncMode` 强转）。[Trigger.cpp:L12-L18](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp#L12-L18) `Init` 把 `FPGA_TRIGGER_OUT` 引脚注册成双沿 EXTI，回调经 `STM::DispatchToInterrupt` 串行化后交给 App 传入的 `TriggerOutISR`（注册于 [App.cpp:L73](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L73)，实现在 [App.cpp:L53-L57](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L53-L57)：置 `FLAG_TRIGGER_OUT_ISR` 通知位）。任务侧 [App.cpp:L305-L327](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L305-L327) 读输出电平按模式分发：USB_GUI 模式发 `SetTrigger`/`ClearTrigger` 包让 GUI 知道扫描走到边界；ExtRef 模式用电平开关 10 MHz 参考输出；Trigger 模式注释明说硬件不支持、空操作。

[Trigger.cpp:L20-L28](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp#L20-L28) 是一个精巧的细节：`vApplicationIdleHook`（FreeRTOS 空闲钩子）在 ExtRef 模式下轮询外部参考是否可用，经 `DispatchToInterrupt` 调 `SetInput`——用「CPU 闲着也是闲着」的时间做轮询，不占任何定时器。[Trigger.cpp:L61-L67](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp#L61-L67) `SetInput` 直接写 GPIO 的 BSRR 寄存器（置位/复位各一个写入地址，无读改写、无竞态），这是 MCU 驱动 `FPGA_TRIGGER_IN` 线的唯一出口；[L30-L56](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Trigger.cpp#L30-L56) `SetMode` 处理 ExtRef 的进入/退出清理（退出时关参考输出；进入时复位参考设置并按当前触发输出电平决定 10 MHz ReferenceOut 的开关）。

#### 4.4.4 代码实践

**实践目标**：画出 Generator 模式下「GUI 拖动频率滑块 → 端口输出变化」的完整调用链，并标注 Trigger 两根线在三种模式下的状态。

**操作步骤**：

1. 从 GUI 侧出发（衔接 u7-l3 的知识）：`GeneratorSettings` 包到达 [App.cpp:L146-L152](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L146-L152)。
2. 在 [Generator.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Generator.cpp) 里分别跟踪两个频率值的路径：900 MHz（高波段、947k 低通）和 10 MHz（低波段、Si5351 直出），各写出途经的每个函数调用。
3. 补一张三行小表：模式 = VNA（syncMode=0）/ VNA（syncMode=1 且 syncMaster=1）/ Generator，`TRIGGER_OUT` 的 EXTI 回调会发生什么、`SetInput` 被谁以什么参数调用。
4. 思考并记录：为什么 Generator 模式下触发输出线即使翻转也不会引起任何测量动作？

**需要观察的现象**：高/低波段两条路径在「谁产生信号」上完全分叉（MAX2871 vs Si5351），但汇聚于同一个 `FPGA::OverwriteHardware` 调用；触发表中 Generator 行的 EXTI 回调在 `Trigger::Mode::Off` 下走进 [App.cpp:L309-L311](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/App.cpp#L309-L311) 的空分支。

**预期结果**：得到一张 10 个左右的函数调用清单（Setup → SetMode → FrequencyCorrection → GetAmplitudeSettings → 波段分支 → OverwriteHardware → Enable×3）与一张触发状态表；第 4 步的答案：Generator 没有 `SetupSweep`、没有扫描在跑，触发输出线的电平变化只反映 FPGA 内部状态，无消费者。纯源码阅读，无需硬件。

#### 4.4.5 小练习与答案

**练习 1**：Generator 为什么在开头关掉三路二本振？

**答案**：信号源模式不接收、不测量，二本振（Si5351 的 Port1LO2/Port2LO2/RefLO2 三路）只服务接收链的下变频。关掉它们既省电、也避免无用的本振信号在板上乱窜泄漏到输出端口。

**练习 2**：`Trigger::SetInput` 为什么用 `BSRR = Pin` 与 `BSRR = Pin << 16` 两个写入而不是 `HAL_GPIO_WritePin`？

**答案**：BSRR（位设置/复位寄存器）的低 16 位置位、高 16 位复位，单次写入原子生效；而经典的「读-改-写」GPIO 输出方式在多中断环境可能被更高优先级的中断穿插造成竞态。这条线可能被 `DispatchToInterrupt` 上下文和空闲钩子路径先后驱动，原子写最安全。

**练习 3**：多机同步时主机和从机的 `FPGA::SetupSweep` 参数有什么不同？

**答案**：主机 `syncMaster=true`（`Enable(Periphery::SyncMaster)` 置位，FPGA 在点边界驱动触发输出线）；双方 `synchronize=true`（SweepSetup 寄存器的 0x1000 位，扫描推进挂到触发信号上）。装配代码见 [FPGA.cpp:L146-L156](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.cpp#L146-L156)，调用点在 [VNA.cpp:L313](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/VNA.cpp#L313)（`s.syncMode != 0, s.syncMaster`）。

## 5. 综合实践

**任务：编写三种模式的「时序对照卡」并互相评审。**

把一张 A4 纸分成三栏（VNA / SA / Generator），每栏从上到下回答同样六个问题，全部以「文件:行号」为证据：

1. 入口：哪类设置包触发哪个 `Setup`？（App.cpp 的 case 分支）
2. 扫描推进者：FPGA 自主 or MCU 逐点？证据是 `SetNumberOfPoints` 的实参。
3. 中断消费者：NewData / SweepHalted / DFTReady 各自唤醒谁？
4. 每个测量点产生的数据是什么形态（复数三元组 / 幅度标量 / 无）？
5. 上报包类型与频率（每点、每 bin、每轮、每 10 点）？
6. 停下来的条件（扫描完 / USB 反压超时 / 新设置包 / activePort=0）？

完成后再做交叉检验：把 4.2.4 你画的 VNA 流程图与 SA 的逐点循环并排，用红笔圈出「结构相同但参数不同」的环节（如零扫宽分叉、`getLastISRTimestamp` 的用法），用蓝笔圈出「只有一方有」的环节（如 Signal ID、ResumeHaltedSweep）。这张对照卡就是下一单元（u6，FPGA 数据通路）的入口地图：你会带着「MCU 侧看到的 FPGA 是一组寄存器和两根中断线」的认知，去 VHDL 里找它们的对端。

有设备的话追加实机验证：用 GUI 的数据包日志（u4-l3 讲过的 DevicePacketLog）抓 30 秒 VNA 扫描，统计 VNADatapoint 包的间隔是否在两次 SweepHalted 之间出现约 40 个一组的规律，验证反压机制的真实节律（待本地验证）。

## 6. 本讲小结

- 三种设备端模式共享一套「Setup 配置 → EXTI 取样 → MeasurementDone 累积 → Work 收尾」的回调骨架，分发枢纽是 `Hardware.cpp` 的 `ReadComplete`/`HW::Work`，开关变量是 `activeMode`。
- VNA 模式是「MCU 一次性预编程 + FPGA 自主扫描 + 暂停点按需干预」：低波段换源、PLL 参考切换、二本振偏移、USB 反压四类事件靠预打的 halt 标志让 FPGA 停下来等 MCU。
- 零扫宽（点频）与普通扫描在固件里的唯一分叉在 `VNA::MeasurementDone` 填包字段处：zerospan 填微秒时间戳，否则填频率与功率；采样与聚合路径两者完全共用。
- SA 模式因点数远超 FPGA 容量且需要多组 LO 配置，改为 MCU 逐点编排；Signal ID 用多组 LO/采样率配置测同一频点取最小值甄别假象，检波器在固件里把过采样的内部点聚合成显示点。
- Generator 模式无测量回路，核心是把频率/功率翻译成波段选择 + `FPGA::OverwriteHardware` 长期钉住射频控制线。
- Trigger 模块管两根线：触发输出线（EXTI 双沿 → App 任务按四种 Mode 分发）与触发输入线（BSRR 原子写），是多机同步与外部参考的物理基础。

## 7. 下一步学习建议

本讲你反复看到 MCU 侧的 `FPGA::WriteRegister`、`WriteSweepConfig`、状态字的 `0x0004/0x0010` 位——这些只是 FPGA 对外露出的「寄存器界面」。下一讲进入 **u6-l1（FPGA 顶层设计与数据流总览）**，从 `FPGA/VNA/top.vhd` 找到这些命令的对端：SPI 从机如何解码命令、Sweep 引擎如何自主推进、NewData/SweepHalted 状态位在哪里置位。建议先读 `FPGA_protocol.tex` 与 [FPGA.hpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Drivers/FPGA/FPGA.hpp) 的枚举对照着看，再带着本讲 4.2.4 的流程图去 VHDL 里逐块认领。如果你更想留在固件侧，可以先把 [Manual.cpp](https://github.com/jankae/LibreVNA/blob/c4276df1e79c559f878ebc17e9f0bd3bd0a70f57/Software/VNA_embedded/Application/Manual.cpp)（第四种隐藏模式：PC 逐点手动控制）读了——它是理解「FPGA 能力边界」的最佳反面教材。
