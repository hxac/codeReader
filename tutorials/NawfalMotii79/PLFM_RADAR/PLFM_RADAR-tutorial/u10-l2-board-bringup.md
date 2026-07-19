# 硬件 Bring-up 流程与构建产物

## 1. 本讲目标

上一篇（u10-l1）我们学了 `fpga_self_test`——它回答「FPGA 内部这块逻辑此刻是否活着」。本讲把视角拉到**整块物理板**：当第一批雷达硬件终于摆在桌上、还没有任何一块芯片被点亮过的时候，工程师按什么顺序上电、用什么证据判断「这一步过了」、出问题时如何安全退回。

学完本讲，你应该能够：

- 读懂 AERIS-10 的 bring-up 计划与板日工作表，复述「上电前门禁 → 板日冒烟测试 → 首次上电可观测目标」三段式检查清单。
- 区分两块关键的 `.bit` 流文件——心跳镜像（heartbeat）与 FT601 集成开发镜像（umft601x dev）——分别验证什么、为什么按「先心跳后 FT601」的顺序使用。
- 看懂 `adc_clk_mmcm.v` 如何用 MMCME2 对 ADC 400 MHz 时钟做抖动清洗，以及 `ila_capture.tcl` 如何用四个 ILA 探针按信号链顺序抓取真实波形。
- 在仓库里定位 `.bit` 流文件、时序报告（`.rpt`）、约束文件（`.xdc`）与 TCL 构建/烧写脚本，理解「产物（artifact）入库、过程报告留主机」的分工。

## 2. 前置知识

在进入正文前，先用大白话确认几个概念。它们大多在前置讲义里出现过，这里只做「bring-up 视角」的回顾。

- **Bring-up（点亮）**：让一块全新设计的电路板从「一堆焊好的元器件」变成「能正常工作的系统」的整个过程。它不是写代码，而是**用证据逐级证明每一个子系统可用**，一旦某级证据缺失或异常就停下来排查。
- **.bit 流文件（bitstream）**：Vivado 综合+实现后产出的、可以直接烧进 FPGA 的二进制文件。本讲会反复提到两类：心跳镜像（极简，只验证 FPGA 能配置、时钟能跑）和集成开发镜像（带 USB 真实通路）。
- **WNS / WHS / WPWS**：时序三件套。WNS（Worst Negative Slack，最差建立时间裕量）为正代表触发器来得及在时钟沿前稳定；WHS（保持时间裕量）为正代表数据在时钟沿后稳定得够久；WPWS 是脉冲宽度裕量。**三者全正 = 时序收敛（timing clean）**，是可以放心烧写的硬指标。负值意味着这颗 `.bit` 在真实时钟下可能误动作，属于「不安全镜像」。
- **.ltx 探针文件**：带 ILA（集成逻辑分析仪）的镜像配套的「探针地图」。Vivado 烧写 `.bit` 后，还要加载匹配的 `.ltx` 才能认出内部的 ILA 采样核。
- **MMCME2**：Xilinx 7 系列 FPGA 内部的混合模式时钟管理器（MMCM）。本讲里它被用来对 ADC 时钟做**1:1 抖动清洗**——频率不变，但把输入抖动从约 50 ps 压到 20–30 ps。
- **ILA（Integrated Logic Analyzer）**：FPGA 内部的「示波器」。你在 RTL 里插探针，Vivado 综合后在硬件里生成一个采样核，触发条件满足时把一段波形抓出来存成 CSV，用来在真实硬件上观察信号。
- **FMC LPC**：一种板对板连接器标准。开发阶段把 FT601 USB 子板（UMFT601X-B）插到载板（TE0701）的 FMC LPC（J10）上，临时替代最终 PCB 上的 USB 走线。

如果你对「三层固件分工」「CDC 跨时钟域」「CFAR/MTI 接收链」这些还陌生，建议先读 u2-l3、u3-l2、u4-l3 再回来——本讲频繁引用这些模块名，但不再重复其内部原理。

## 3. 本讲源码地图

本讲的「源码」并不只是 Verilog，而是一整套**硬件点亮工程产物**：文档（HTML 检查清单）、构建脚本（TCL）、约束（XDC）、流文件（.bit）与时序报告（.rpt）。下表按本讲三个最小模块归类。

| 文件 | 类型 | 在本讲的作用 |
|------|------|--------------|
| [docs/bring-up.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html) | 文档 | 板前门禁、板日冒烟测试、中止准则、可观测目标的「操作真源」 |
| [docs/board-day-worksheet.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html) | 文档 | 可打印的板日工作表，逐项记录 pass/fail 与测量值 |
| [docs/artifacts/te0713-te0701-heartbeat-2026-03-21.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-heartbeat-2026-03-21.md) | 元数据 | 心跳镜像的构建结果与用途说明 |
| [docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md) | 元数据 | FT601 集成开发镜像的构建结果、硬件接线与验证步骤 |
| [9_Firmware/9_2_FPGA/radar_system_top_te0713_dev.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_te0713_dev.v) | Verilog | 心跳镜像的顶层——一个最小计数器分频点灯 |
| [9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_dev.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_dev.tcl) | TCL | 心跳镜像的 Vivado 批处理构建脚本 |
| [9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl) | TCL | FT601 集成开发镜像的构建脚本 |
| [9_Firmware/9_2_FPGA/scripts/utils/program_fpga.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/program_fpga.tcl) | TCL | 七步烧写流程，验证 DONE 引脚 |
| [9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc) | XDC | FT601 经 FMC LPC 的引脚约束与关键跳线设置 |
| [9_Firmware/9_2_FPGA/adc_clk_mmcm.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v) | Verilog | ADC 400 MHz 时钟的 MMCME2 抖动清洗包装 |
| [9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl) | TCL | 四探针 ILA 抓取与健康自检脚本 |
| [9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/diag_log.h](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/diag_log.h) | C 头 | STM32 侧 USART3 诊断日志词汇表（bring-up 可观测层） |

## 4. 核心概念与源码讲解

### 4.1 上电检查清单：板前门禁与板日冒烟测试

#### 4.1.1 概念说明

「Bring-up」最忌讳的是「一上电就全开」。AERIS-10 是一台含 10 W GaN 功放、10.5 GHz 本振、多路高压电源轨的雷达——任何一步顺序错误都可能烧器件。因此项目把整个点亮过程写成两层文档：

- **bring-up.html**：操作总则。回答「板子到货**前**要准备到什么程度」「板子到货**当天**按什么顺序点亮」「什么情况下必须立即停」。
- **board-day-worksheet.html**：当天的**记录纸**。把总则拆成一张张可勾选的表格，每一步要填「预期证据」「状态」「备注」，让操作员在真实硬件前边测边记。

这两份文档的关系类似「考试大纲」与「答题卡」：总则告诉你判据，工作表让你留下证据。

#### 4.1.2 核心流程

总则把点亮切成三段：

```text
【板前门禁 6 项】──板子没到也要全部绿灯──┐
                                          │
                                          ▼
【板日冒烟测试 8 步】──从最安全配置逐步加电──┐
   每步都要有「证据」，证据缺失就停         │
                                          │
                                          ▼
【首次上电可观测目标表】──7 个子系统各自要看到什么──┐
```

**板前门禁**要求在硬件到货前就冻结好「已知良好的固件/流文件基线」。其中门禁 1 明确列出了两份入库的镜像：

> heartbeat image at `docs/artifacts/te0713-te0701-heartbeat-2026-03-21.bit`; FT601 integration dev image at `docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.bit` (WNS +0.059 ns, timing clean)

参见 [docs/bring-up.html:47](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L47)。这句话是本讲最重要的索引之一：它告诉操作员，板日第一颗要烧的不是功能镜像，而是**心跳镜像**；FT601 镜像时序已收敛（WNS +0.059 ns）才是「可安全继续」的判据。完整的六项门禁表见 [docs/bring-up.html:34-56](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L34-L56)。

**板日冒烟测试**的 8 步有一个贯穿始终的原则：**从最安全的配置开始，RF 发射通路全程禁用，直到最后才碰功放偏置与校准**。这 8 步是：

```text
1. 上电前先目检载板默认状态、稳压器使能、跳线、板级时钟源选择
2. 以最安全配置上电，禁用 RF 发射，立即记录静态电流
3. 跑 FPGA 烧写流程，确认 JTAG 枚举、DONE、INIT_COMPLETE
4. 确认复位释放与心跳/状态输出，再使能任何模拟或 RF 功能
5. 起 MCU 固件日志，确认 AD9523 状态、LO 初始化、波束控制器回读
6. 用带调试能力的 FPGA 镜像，按「原始 ADC→DDC→匹配滤波→USB」顺序确认
7. 在任何长时间流测试前，先用已知成帧预期跑通 FT601 通路
8. 以上全部通过后，才开始 PA 偏置、校准与高风险 RF 激活
```

原文见 [docs/bring-up.html:59-71](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L59-L71)。注意第 6 步的顺序——「原始 ADC → DDC → 匹配滤波 → USB」——这正是 u2-l2 信号处理流水线的真实方向，也对应后面 ILA 抓取脚本（4.3 节）四个探针的排列。

**中止准则**告诉操作员「必须停」的红线，例如：静态电流异常、稳压器不稳、温升超预期就立刻断电；LO 锁定 GPIO 与回读值反复不一致就不要继续；波束控制器 scratchpad 回读失败就停 RF 激活。完整列表见 [docs/bring-up.html:72-83](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L72-L83)。其中一条「复位时序或时钟存在性含糊时，退回心跳或调试镜像」直接呼应了「心跳优先」的策略。

**首次上电可观测目标表**把 7 个子系统各自「必须能看到什么」「预期证据是什么」列成表，例如「FPGA 配置」要看到 JTAG 枚举、DONE、INIT_COMPLETE，「时钟」要看到 AD9523 状态引脚与确定性复位释放，「LO 链」要看到 ADF4382A 初始化状态与 TX/RX 锁定状态，详见 [docs/bring-up.html:85-107](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L85-L107)。这张表是判断「这一步到底过没过」的标尺。

#### 4.1.3 源码精读：板日工作表的逐级表格

板日工作表把总则落成可填写的表格，按上电顺序分成五段。这里精读其中两段。

**「上电与配置检查」段**——对应冒烟测试第 2–4 步：

```text
初始上电      | 静态电流在规划包络内，无温度异常
JTAG 枚举     | 硬件管理器里能看到目标器件
烧写流文件     | DONE = HIGH, INIT_COMPLETE = 已置位
可选探针加载   | 期望的 ILA 核枚举出来
复位/心跳自检  | 确定性的复位释放与状态活动
```

原文见 [docs/board-day-worksheet.html:70-91](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html#L70-L91)。注意「DONE = HIGH」这一格——它是后面 `program_fpga.tcl` 第七步在软件里读回的同一个寄存器位（`REGISTER.CONFIG_STATUS.DONE`），表格与脚本用的是同一把尺子。

**「FPGA 数据通路与 USB 检查」段**——对应冒烟测试第 6–7 步，按信号链顺序排列：

```text
原始 ADC 可见性    | ILA 或状态证据显示预期时钟上有活动
DDC/匹配滤波活动   | 观察到有效选通与非平坦输出
USB 成帧自检       | 帧头、负载长度、帧尾保持一致
FT601 行为         | 无明显的背压或总线方向异常
持续流测试         | 无立即锁死、成帧漂移或复位事件
```

原文见 [docs/board-day-worksheet.html:116-137](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html#L116-L137)。这张表的五行顺序，正好映射到 `ila_capture.tcl` 的 adc / ddc / mf 三个探针（USB/FT601 由主机侧抓包脚本负责）。也就是说：工作表的每一行都对应一个具体的工具去取证据。

工作表还要求记录关键测量值（载板/模块静态电流、5V/3V3 轨、LO 锁定指示、ADAR 温度、PA IDQ 抽检、USB 枚举/吞吐），见 [docs/board-day-worksheet.html:139-161](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html#L139-L161)，以及「是否触发了停止条件」，见 [docs/board-day-worksheet.html:162-184](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html#L162-L184)。这些字段把「测了什么、结果如何」固化下来，方便事后复盘。

#### 4.1.4 代码实践

1. **实践目标**：把板日工作表的检查顺序内化成自己的「点亮心智模型」，并理解每个子系统由哪类工具取证据。
2. **操作步骤**：
   - 打开 [docs/board-day-worksheet.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/board-day-worksheet.html)，把五段表格（上电前检查、上电与配置、固件与控制通路、FPGA 数据通路与 USB、测量记录）按顺序抄成一份清单。
   - 打开 [docs/bring-up.html:85-107](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L85-L107) 的可观测目标表，给清单里每一行标注「这条证据由谁产生」：USART3 日志、`program_fpga.tcl` 的 DONE 回读、ILA 抓取、主机 USB 抓包、万用表/示波器。
3. **需要观察的现象**：你会看到工作表的行与可观测目标表的子系统一一对应，且每行都能落到一个具体工具上；找不到工具取证据的行就是「板前门禁第 4 项（让首次上电行为可观测）」还缺的拼图。
4. **预期结果**：得到一份「检查项 → 工具」对照表。例如「JTAG 枚举/DONE」→ `program_fpga.tcl`；「原始 ADC 活动」→ `ila_capture.tcl adc`；「AD9523 状态」→ USART3 DIAG 日志（`diag_log.h`）。
5. 本实践为源码阅读型，无需硬件。

#### 4.1.5 小练习与答案

**练习 1**：为什么板日工作表把「RF 发射通路禁用」放在最前面、把「PA 偏置与校准」放在最后？
> **答案**：因为风险随能量单调上升。RF 与 PA 涉及高功率与不可逆损坏，必须先证明低风险的数字通路（FPGA 配置、时钟、复位、数据通路、USB）全部正常，确认没有会让 PA 误触发的失控状态，才轮到 PA。前置步骤一旦异常，停下来的代价最小。

**练习 2**：板前门禁第 3 项要求「回归测试在板到货前保持绿灯」（15/15 MCU、18/18 FPGA）。如果到板当天为了赶进度跳过回归，会丢失什么？
> **答案**：丢失「基线可信」这个前提。bring-up 的所有判据都建立在「当前固件/流文件在仿真层已知良好」之上；跳过回归意味着一旦板日看到异常，你无法区分是硬件问题还是固件新引入的回归，调试成本急剧上升。

### 4.2 开发板构建流程：心跳镜像与 FT601 集成镜像

#### 4.2.1 概念说明

量产目标是固定焊在 PCB 上的 XC7A200T；但 bring-up 阶段用**开发板**（Trenz TE0713 SoM 插在 TE0701 载板上）来分摊风险。项目为开发板准备了两颗镜像，按「由简到繁」递进：

- **心跳镜像（heartbeat）**：顶层是 `radar_system_top_te0713_dev`，逻辑只有一个计数器分频点灯。它**不包含任何雷达信号处理**，唯一目的是证明「这块 FPGA 能被配置、主时钟能跑、引脚能翻转」。WNS 高达 +17.863 ns（几乎没时序压力），是最低风险的第一上电镜像。
- **FT601 集成开发镜像（umft601x dev）**：顶层是 `radar_system_top_te0713_umft601x_dev`，例化了完整的 `usb_data_interface.v`（FT601 USB 数据通路），但仍用**合成测试数据**（计数器异或图案）而非真实 ADC 数据。它验证「USB 通路在真实硬件上能枚举、能回主机命令、能成帧」。WNS +0.059 ns，时序刚好收敛。

两颗镜像构成了一个「最小可证伪链」：心跳过了 → FPGA 与板级基础没问题；FT601 dev 过了 → USB 这条跨芯片高速通路没问题；之后才轮到带真实 ADC 数据的完整镜像。这种逐级追加、每级都有独立判据的做法，是 bring-up 区别于「直接烧完整镜像碰运气」的核心。

#### 4.2.2 核心流程

构建一颗镜像的 Vivado 批处理流程是统一的：

```text
create_project(指定 part)  ──► add_files(顶层 + 子模块)
                            ──► add_files XDC(约束)
                            ──► set top(顶层模块名)
                            ──► launch_runs impl_1 -to_step write_bitstream
                            ──► wait_on_run
                            ──► open_run → report_*（生成时序/CDC/DRC/资源报告）
```

差别只在三处：**顶层文件、约束文件、实现策略**。

心跳镜像用最朴素配置：顶层 `radar_system_top_te0713_dev.v` + 约束 `te0713_te0701_minimal.xdc`，不指定特殊策略，见 [build_te0713_dev.tcl:15-31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_dev.tcl#L15-L31)。

FT601 镜像多两件事：一是顶层换成 `radar_system_top_te0713_umft601x_dev.v` 并额外加入 `usb_data_interface.v`，约束换成 `te0713_te0701_umft601x.xdc`（FT601 经 FMC LPC 的引脚）；二是指定 `Performance_ExplorePostRoutePhysOpt` 实现策略来啃下 USB 源同步时序，见 [build_te0713_umft601x_dev.tcl:12-33](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl#L12-L33)。两个脚本结尾都跑同一套 `report_*` 命令，把 clocks、timing_summary、cdc、drc、utilization 等报告写进 `reports/` 目录，见 [build_te0713_umft601x_dev.tcl:45-51](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl#L45-L51)。

#### 4.2.3 源码精读

**心跳镜像顶层**——极简到可以全读：

```verilog
module radar_system_top_te0713_dev (
    input wire clk_100m,        // TE0713 FIFO0CLK (actually 50 MHz)
    output wire [3:0] user_led,
    output wire [3:0] system_status
);
reg [31:0] hb_counter = 32'd0;
always @(posedge clk_buf) begin
    hb_counter <= hb_counter + 1'b1;
end
assign user_led[0] = hb_counter[24];   // 50 MHz / 2^25 ≈ 1.49 Hz
```

见 [radar_system_top_te0713_dev.v:16-42](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_te0713_dev.v#L16-L42)。它的全部逻辑就是一个 32 位计数器，把高位bit接给 LED 与状态引脚——LED 以约 1.49 Hz/0.75 Hz/0.37 Hz/0.19 Hz 闪烁。**只要灯在按这个频率闪，就证明 FPGA 配置成功、BUFG 起作用、主时钟在跑、I/O bank 供电正常**。注意端口名 `clk_100m` 实际接的是 TE0713 的 50 MHz FIFO0CLK（见注释），这是开发板上的一个小坑：命名是历史遗留，以约束文件和注释为准。

心跳镜像的构建结果记录在元数据文件里：

```text
- WNS: +17.863 ns
- WHS: +0.265 ns
- Purpose: Lowest-risk first-power image ... Verifies FPGA configuration,
  primary clock path, and heartbeat/status outputs before FT601 or radar-path bring-up
```

见 [te0713-te0701-heartbeat-2026-03-21.md:17-22](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-heartbeat-2026-03-21.md#L17-L22)。WNS +17.863 ns 是一个「宽到几乎不可能失败」的裕量，正符合「最低风险首上电镜像」的定位。

**FT601 集成开发镜像**的元数据则描述了它「做什么」与「怎么验」：

```text
- Instantiates usb_data_interface.v (full FT601 USB data path)
- Generates synthetic test data: range profile packets (counter XOR pattern)
- Responds to USB host commands (stream control 0x04, status request 0xFF)
- Drives ft601_gpio0 with a ~6 Hz heartbeat (counter bit 24)
```

见 [te0713-te0701-umft601x-dev-2026-03-21.md:22-31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md#L22-L31)。注意它复用了 u6-l1 里学过的 opcode：`0x04` 流控、`0xFF` 状态请求——也就是说这颗镜像在硬件上验证的就是主机命令协议。它的构建时序 WNS +0.059 ns、0 失败端点、0 DRC 错误，见同文件 [第 10-20 行的构建汇总表](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md#L10-L20)。

这颗镜像还附带了关键的**硬件接线说明**与**时序收敛建议**。接线部分要求 TE0701 的 VIOTB 设为 3.3V、UMFT601X-B 一组跳线设成 245 同步 FIFO 模式，见 [第 32-41 行](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md#L32-L41)；这些跳线设置在约束文件头部被重复声明为「CRITICAL SETUP」，见 [te0713_te0701_umft601x.xdc:14-20](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc#L14-L20)——这种「.md 与 .xdc 双重声明」是有意为之，因为跳错跳线会让 LVCMOS33 电平错配，是 bring-up 常见陷阱。

时序收敛建议记录了一个真实的踩坑史：早期版本因为 FT601 时钟经 IBUF+BUFG 引入约 5 ns 插入延迟，`set_output_delay` 相对 `ft601_clk_in` 造成了虚假的 5 ns 偏移惩罚；改用 `set_max_delay -datapath_only` 直接约束寄存器到引脚的路径（7.5 ns 预算）后收敛，见 [第 63-72 行](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md#L63-L72)。这是「源同步时钟的输出约束要避开插入延迟假象」的典型教训。

**烧写流程**由 `program_fpga.tcl` 的七步完成，其中第六步真正烧写、第七步读回 DONE 引脚：

```tcl
# Step 6: Program the bitstream
set_property PROGRAM.FILE $bitstream_path $target_device
program_hw_devices $target_device
# Step 7: Verify DONE pin
refresh_hw_device $target_device
set done_status [get_property REGISTER.CONFIG_STATUS.DONE $target_device]
```

见 [program_fpga.tcl:239-285](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/program_fpga.tcl#L239-L285)。`DONE == 1` 才判定 PASS，并把 INIT_COMPLETE 与 ILA 探针枚举情况一起写进汇总表。这把板日工作表里「DONE = HIGH」那一格落到了可执行的脚本。

#### 4.2.4 代码实践

1. **实践目标**：理解两颗镜像「验证什么」的边界，并能从元数据与构建脚本里读出差别。
2. **操作步骤**：
   - 对照 [te0713-te0701-heartbeat-2026-03-21.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-heartbeat-2026-03-21.md) 与 [te0713-te0701-umft601x-dev-2026-03-21.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md)，列一张对比表：顶层模块、约束文件、例化的关键子模块、WNS、验证判据。
   - 解释心跳镜像 WNS +17.863 ns 与 FT601 镜像 WNS +0.059 ns 的巨大差距来自哪里。
3. **需要观察的现象**：心跳镜像没有 USB、没有 DSP、没有 BRAM，关键路径只是一条计数器寄存器回写，所以裕量巨大；FT601 镜像的关键路径落在 `ft601_clk_in` 域的 USB 读 FSM（参见 reports.html 的 Build 25 报告，最差路径在 USB FSM），裕量被源同步时序吃紧。
4. **预期结果**：得到一张「心跳 = 验证 FPGA 配置+时钟+I/O」「FT601 dev = 验证 USB 枚举+主机命令+成帧」的对照表，并理解为什么必须先心跳后 FT601。
5. 本实践为源码阅读型，无需硬件；构建命令 `vivado -mode batch -source scripts/build_te0713_dev.tcl` 的实际运行待本地具备 Vivado 2025.2 环境后验证（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：FT601 镜像用「合成测试数据（counter XOR pattern）」而不是真实 ADC 数据，为什么？
> **答案**：为了把「USB 通路是否通」与「ADC/接收链是否对」这两个变量解耦。合成数据是确定已知图案，主机侧能立刻判断收到的包是否成帧正确；若用真实 ADC 数据，一旦包异常，无法区分是 USB 传输出错还是 ADC 链本身没数据。逐级隔离变量是 bring-up 的基本方法。

**练习 2**：`program_fpga.tcl` 默认烧写的是 `build/bitstream/radar_system_top_build21.bit`（见脚本第 30 行），但板日要烧的是 `docs/artifacts/` 下的心跳镜像。怎么解决？
> **答案**：用 `-bit` 参数覆盖默认路径，例如 `vivado -mode batch -source program_fpga.tcl -tclargs -bit docs/artifacts/te0713-te0701-heartbeat-2026-03-21.bit`。脚本的 `parse_args` 支持 `-bit`、`-ltx`、`-server`、`-port`、`-no_probes`、`-force` 等覆盖项。

### 4.3 时钟/MMCM 与 ILA 调试

#### 4.3.1 概念说明

板日工作表「FPGA 数据通路」段要求按「原始 ADC → DDC → 匹配滤波 → USB」顺序看到活动。要在真实硬件上「看到」这些内部信号，需要两件工具：

- **干净的采样时钟**：ADC（AD9484）以 400 MHz 的 DCO（数据时钟输出）驱动 FPGA。这个时钟直接来自 ADC，抖动较大（约 50 ps）。如果直接用它驱动 400 MHz 域的所有逻辑（尤其 CIC 抽取这条关键路径），时钟不确定性会吃掉本就紧张的建立时间裕量。`adc_clk_mmcm.v` 用一片 MMCME2 把它「清洗」成抖动更小的同频时钟。
- **内部示波器（ILA）**：在 RTL 里预设探针，`ila_capture.tcl` 在硬件上按触发条件抓波形并导出 CSV，相当于把四段信号链（adc/ddc/mf/doppler）分别接上示波器探头。

二者配合：MMCM 让时钟稳定可靠，ILA 在这个稳定时钟下抓取真实数据，共同把「仿真里对的东西」验证为「硬件上也对」。

#### 4.3.2 核心流程

**MMCM 抖动清洗**的数学很简单——输入输出同频（400 MHz），靠 PLL 反馈环路滤波掉输入抖动：

\[ f_{VCO} = f_{in} \cdot \frac{\text{CLKFBOUT\_MULT\_F}}{\text{DIVCLK\_DIVIDE}} = 400 \times \frac{2.0}{1} = 800 \text{ MHz} \]

\[ f_{OUT} = \frac{f_{VCO}}{\text{CLKOUT0\_DIVIDE\_F}} = \frac{800}{2.0} = 400 \text{ MHz} \]

VCO 必须落在 600–1200 MHz 的工作区间，所以选倍频 2.0 到 800 MHz、再除 2.0 回到 400 MHz。反馈走 BUFG 内部回环（`CLKFBOUT → BUFG → CLKFBIN`），由 MMCM 反馈环路补偿 Vivado 时钟网络插入延迟，达到最佳抖动性能。文档预期它能把输入抖动从约 50 ps 压到 20–30 ps，并在 400 MHz CIC 关键路径上带来 +20–40 ps 的 WNS 改善，见 [adc_clk_mmcm.v:18-24](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L18-L24)。

**ILA 抓取**的流程是：连硬件服务器 → 打开 JTAG 目标 → 校验已配置（DONE=1）→ 加载 `.ltx` 探针 → 找到对应 ILA 核 → 配触发条件 → 武装（arm）→ 等触发 → 上传波形 → 导出 CSV → 算统计量。

```text
connect_hw_server ──► open_hw_target ──► 选设备并校验 DONE
   ──► set_property PROBES.FILE xxx.ltx
   ──► resolve_ila(hw_ila_N) ──► configure_trigger(触发网, 边沿)
   ──► run_hw_ila ──► wait_on_hw_ila(超时)
   ──► upload_hw_ila_data ──► write_hw_ila_data -csv_file xxx.csv
```

四个探针对应信号链四个阶段，触发条件都是对应模块的 `*_valid` 选通信号的上升沿，见下方配置表。

#### 4.3.3 源码精读

**MMCM 配置核心**——`MMCME2_ADV` 原语的 VCO 与输出参数：

```verilog
.DIVCLK_DIVIDE      (1),        // 输入分频 = 1
.CLKFBOUT_MULT_F    (2.0),      // 反馈倍频 → VCO = 800 MHz
.CLKOUT0_DIVIDE_F   (2.0),      // 800 / 2.0 = 400 MHz
.CLKOUT0_PHASE      (0.0),      // 与输入相位对齐
.BANDWIDTH          ("HIGH"),   // 高带宽 = 最大抖动抑制
```

见 [adc_clk_mmcm.v:119-150](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L119-L150)。`BANDWIDTH("HIGH")` 是为了「最大抖动抑制」——带宽越高，PLL 对输入抖动的跟踪越快、抑制越强。`mmcm_locked` 输出在 PLL 锁定后置 1，可被上层用于复位时序（只有时钟稳定后才释放复位）。

输出端的 BUFG 上挂了 `DONT_TOUCH`，注释解释了一个真实的踩坑史：

```verilog
(* DONT_TOUCH = "TRUE" *)
BUFG bufg_clk400m ( .I(clk_mmcm_out0), .O(clk_400m_out) );
// DONT_TOUCH prevents phys_opt_design AggressiveExplore from replicating this
// BUFG into a cascaded chain (4 BUFGs in series observed in Build 26), which
// added ~243ps of clock insertion delay and caused -187ps clock skew on the
// NCO→DSP mixer critical path.
```

见 [adc_clk_mmcm.v:213-223](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L213-L223)。这是非常典型的「工具自作主张」案例：`phys_opt_design` 的 `AggressiveExplore` 把单个 BUFG 复制成 4 个串联 BUFG，本意是优化，结果加了 243 ps 插入延迟，在 NCO→DSP 混频器关键路径上造成 −187 ps 时钟偏斜，直接毁掉时序。`DONT_TOUCH` 锁死这个 BUFG 不许工具动。这印证了前置讲义反复强调的一条——「以代码为准，注释里的工程教训往往比文档更真实」。

模块用 `ifdef SIMULATION` 在仿真与综合间切换：仿真路径把时钟直接穿透（iverilog 没有 MMCME2 原语），并用一个 4096 拍计数器模拟约 10 µs 的 MMCM 锁定时间；综合路径才例化真实原语，见 [adc_clk_mmcm.v:55-96](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L55-L96) 与 [第 97-227 行综合路径](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L97-L227)。这与 u3-l2 讲过的「仿真与综合用 `ifdef SIMULATION` 分离」一脉相承。

**ILA 抓取脚本**的四探针配置表是本模块的索引：

```tcl
array set ila_config {
    adc     { ... trigger_net "radar_system_top/rx_inst/adc_if/adc_valid"      clock_mhz 400 ... }
    ddc     { ... trigger_net "radar_system_top/rx_inst/ddc_inst/ddc_valid"    clock_mhz 100 ... }
    mf      { ... trigger_net "radar_system_top/rx_inst/mf_chain/mf_valid"     clock_mhz 100 ... }
    doppler { ... trigger_net "radar_system_top/rx_inst/doppler_proc/doppler_valid" clock_mhz 100 ... }
}
```

见 [ila_capture.tcl:49-82](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl#L49-L82)。四个探针的层次路径 `rx_inst/adc_if`、`rx_inst/ddc_inst`、`rx_inst/mf_chain`、`rx_inst/doppler_proc` 正好对应 u2-l2 接收链的四个阶段，时钟也从 400 MHz（ADC 域）降到 100 MHz（DDC 之后的处理域），与 u4-l1 讲的「DDC 是 400→100 的分水岭」一致。触发条件用对应 `*_valid` 的上升沿（`trigger_val "R"`）。

脚本支持的 scenario 包括 `adc | ddc | mf | doppler | all | health`，其中 `health` 是「快速健康自检」：强制使用立即触发（free-running，不等条件），跑完四个探针，对每个探针判断「数据是否非全零」，最后给出 `Overall PASS/FAIL (n/4 passed)` 汇总，见 [ila_capture.tcl:610-697](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl#L610-L697)。这正好服务于板日工作表「FPGA 数据通路」段——一句 `vivado -mode batch -source ila_capture.tcl -tclargs health` 就能快速回答「四段信号链是否都有数据活动」。

脚本在连接硬件时先校验设备已配置：

```tcl
set done [get_property REGISTER.CONFIG_STATUS.DONE $target_device]
if {$done != 1} {
    log_error "FPGA is not configured (DONE=LOW). Program the bitstream first."
    return -code error "DEVICE_NOT_CONFIGURED"
}
```

见 [ila_capture.tcl:255-261](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl#L255-L261)。这与 `program_fpga.tcl` 第七步读的是同一个 DONE 位——两个脚本对「FPGA 是否就绪」用同一把尺子，构成 4.1 节检查清单与 4.2 节烧写流程的闭环。

最后值得一提：板日工作表「固件与控制通路」段的证据（AD9523 状态、LO 初始化、ADAR1000 回读、温度）来自 STM32 的 USART3 日志，其词汇表由 `diag_log.h` 的 `DIAG`/`DIAG_WARN`/`DIAG_ERR`/`DIAG_REG` 等宏定义，输出形如 `[  12345 ms] LO: TX init returned -2`，见 [diag_log.h:43-64](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/diag_log.h#L43-L64)。它是纯观测层（observation-only），不改变任何行为——这保证日志本身不会成为 bring-up 的干扰变量。

#### 4.3.4 代码实践

1. **实践目标**：把 ILA 四探针映射到接收链阶段，并能解释 MMCM 为什么要 DONT_TOUCH。
2. **操作步骤**：
   - 读 [ila_capture.tcl:49-82](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/ila_capture.tcl#L49-L82) 的配置表，画一条横向的信号链：`adc_if(400M) → ddc_inst(100M) → mf_chain(100M) → doppler_proc(100M)`，每个节点标触发网与场景名。
   - 读 [adc_clk_mmcm.v:213-223](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/adc_clk_mmcm.v#L213-L223)，复述如果去掉 `DONT_TOUCH`，`phys_opt_design` 可能做什么、后果是什么。
3. **需要观察的现象**：四探针的时钟频率在 ddc 节点从 400 MHz 跳到 100 MHz，这正是 DDC 抽取发生的边界；DONT_TOUCH 阻止的是工具把 BUFG 串成链、引入额外插入延迟、毁掉 NCO→DSP 关键路径时序。
4. **预期结果**：一条标注了触发网与时钟域的信号链图；一段「去掉 DONT_TOUCH → BUFG 被串成 4 级 → +243 ps 插入延迟 → NCO 关键路径 −187 ps 偏斜 → 时序失败」的因果链。
5. 实际抓取命令 `vivado -mode batch -source ila_capture.tcl -tclargs health` 的运行需要真实硬件与匹配的 `.ltx`，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 ILA 四探针里只有 `adc` 是 400 MHz，其余三个都是 100 MHz？
> **答案**：因为 DDC（数字下变频）在 `ddc_inst` 内部用 CIC 抽取把 400 MSPS 降到 100 MSPS（u4-l1）。ADC 接口跑在 400 MHz 域，DDC 之后所有处理（匹配滤波、Doppler）都在 100 MHz 域，所以 `ddc_valid`/`mf_valid`/`doppler_valid` 都是 100 MHz 信号。ILA 必须用各自信号所在时钟域的时钟采样，否则抓到的波形无意义。

**练习 2**：`adc_clk_mmcm.v` 在仿真路径里不例化 MMCME2，而是直接穿透时钟。如果仿真里也强行用真实 MMCME2 会怎样？
> **答案**：仿真器（iverilog）没有 Xilinx 原语库，会报 `MMCME2_ADV` 未定义错误，仿真无法编译。所以用 `ifdef SIMULATION` 让仿真走行为级穿透、综合走真实原语，是仿真可移植性与硬件保真度的折中。同时仿真路径用 4096 拍计数器模拟 MMCM 锁定时间，让 `mmcm_locked` 的时序行为也接近真实。

## 5. 综合实践

把本讲三块知识串起来，模拟一次完整的「板日第一天」决策流程。**无需真实硬件**，全部基于仓库文档与脚本完成。

**任务背景**：你是板日操作员。TE0713 SoM 刚插到 TE0701 载板上，UMFT601X-B 还没接。请按下面顺序产出决策与清单。

1. **板前确认（4.1）**：打开 [docs/bring-up.html:34-56](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html#L34-L56) 的六项门禁，逐项写出本仓库对应的证据文件路径。例如门禁 1 的「心跳镜像」→ `docs/artifacts/te0713-te0701-heartbeat-2026-03-21.bit`，门禁 3 的「FPGA 回归」→ `9_Firmware/9_2_FPGA/run_regression.sh`（参考 reports.html 的 23/23）。

2. **选第一颗镜像（4.2）**：写出板日要烧的第一颗镜像文件名、它的顶层模块、约束文件、以及它**验证什么 / 不验证什么**。要求明确写出「它不验证 USB，也不验证 ADC 数据通路」。

3. **烧写与判据（4.2）**：写出用 `program_fpga.tcl` 烧心跳镜像的完整命令（含 `-bit` 覆盖），并列出判定 PASS 的两个寄存器位。提示：见 [program_fpga.tcl:275-285](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/utils/program_fpga.tcl#L275-L285)。

4. **升级到 FT601 镜像（4.2 + 4.1）**：心跳灯正常闪烁后，按 [te0713-te0701-umft601x-dev-2026-03-21.md:32-41](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/artifacts/te0713-te0701-umft601x-dev-2026-03-21.md#L32-L41) 列出接 UMFT601X-B 前必须设置的跳线，并说明这颗镜像在主机侧要看到的三件事（GPIO0 心跳、USB 枚举 VID/PID、包头 `0xAE10xxxx`）。

5. **数据通路自检（4.3）**：FT601 镜像跑通后，假设后续换上带真实 ADC 数据与 ILA 探针的镜像，写出快速验证四段信号链是否有数据活动的命令，并解释为什么 `adc` 探针的时钟是 400 MHz 而其它三个是 100 MHz。

**交付物**：一份「板日决策表」，每一行是「步骤 → 用到的文件/脚本 → 判据 → 若失败的退回动作」。完成后，你应该能向一个没读过本仓库的人解释清楚：为什么 AERIS-10 的第一次上电必须从一颗「只会闪灯」的镜像开始。

## 6. 本讲小结

- **bring-up 是用证据逐级证明的过程**，由 `bring-up.html`（操作总则）与 `board-day-worksheet.html`（可填写工作表）两份文档驱动，分板前门禁、板日冒烟测试、首次上电可观测目标三段，RF 与 PA 永远最后才碰。
- **两颗关键镜像递进验证**：心跳镜像（`radar_system_top_te0713_dev`，WNS +17.863 ns）只证 FPGA 配置+时钟+I/O；FT601 集成开发镜像（`radar_system_top_te0713_umft601x_dev`，WNS +0.059 ns）用合成数据证 USB 枚举+主机命令+成帧。
- **构建脚本统一**：`build_te0713_dev.tcl` 与 `build_te0713_umft601x_dev.tcl` 流程相同，差别只在顶层、约束与实现策略；`program_fpga.tcl` 七步烧写并以 DONE 引脚作 PASS 判据。
- **MMCM 清洗 ADC 时钟**：`adc_clk_mmcm.v` 用 MMCME2 做 1:1 抖动清洗（VCO=800 MHz、输出 400 MHz），输出 BUFG 加 `DONT_TOUCH` 防止工具把它串成链而毁掉 NCO 关键路径时序。
- **ILA 是硬件内示波器**：`ila_capture.tcl` 的四探针（adc@400M、ddc/mf/doppler@100M）对应接收链四阶段，`health` 场景一句命令给四段信号链的活跃度体检。
- **观测层只读不扰**：`diag_log.h` 的 DIAG 宏经 USART3 输出带时间戳的子系统日志，是纯观测层，保证日志本身不成为 bring-up 的干扰变量；多个脚本对「FPGA 就绪」统一用 DONE 位判定，形成闭环。

## 7. 下一步学习建议

- **横向收口测试体系**：本讲的「逐级验证」思想在 u11（测试与验证体系）有更完整的体现，建议接着读 u11-l1（FPGA 回归与 cosim）与 u11-l3（跨层契约测试），理解仿真层如何为板日的「已知良好基线」背书。
- **纵向深入构建流**：若想了解多板卡顶层、约束与 TCL 构建脚本如何扩展，读 u14-l2（二次开发扩展点与 Vivado 构建流），那里会讲如何为新板卡添加 `radar_system_top_*` 顶层与 `.xdc`。
- **回到信号链细节**：本讲反复引用的 adc/ddc/mf/doppler 四段，其内部原理分别在 u4-1（DDC）、u4-2（匹配滤波）、u4-4（Doppler）有完整讲解；如果你在板日 ILA 抓到的波形不符合预期，回到这三篇对照寄存器与时序。
- **继续阅读源码**：通读 [docs/bring-up.html](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/docs/bring-up.html) 的「Known open risks before board arrival」一节（第 135-155 行），它列出了板日之前所有「未在硬件上证伪」的风险，是理解「为什么 bring-up 仍需保守」的最佳材料。
