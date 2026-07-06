# 布局布线：Innovus 物理实现流程

## 1. 本讲目标

在 u6-l1~u6-l3 中，我们把 32 点 FFT 处理器从 RTL「翻译」成了 UMC 130nm 工艺下的门级网表，并读懂了综合后的时序/面积/功耗报告。但门级网表只是一张「元件清单 + 连线表」——它告诉你用了哪些标准单元、谁连谁，却没说这些单元在硅片上**摆在哪里、用什么金属层连线、时钟怎么走、电源怎么供**。把这些物理细节落实，正是**布局布线（Place and Route，简称 P&R 或 PnR）**的任务。

本讲带读者走完 Cadence **Innovus** 物理实现流程的全部 6 个阶段：floorplan（版图规划）→ 电源网络 → 布局 → CTS（时钟树综合）→ 详细布线 → 签核输出，最终产出可流片的 `FFT.gds` 版图和带真实寄生参数的门级时序文件 `FFT.sdf`。学完本讲，读者应当能够：

- 读懂 `Pnr/scripts/pnr.tcl` 的 60 行脚本，理解每个物理阶段的代表性命令。
- 理解 floorplan 利用率、电源环/条带的设计意图，以及 CTS 如何把一个理想时钟变成真实时钟树。
- 说出 `timeDesign` 在 `prePlace / preCTS / postCTS / postRoute` 四个节点的物理含义与差异。
- 读懂 `Pnr/scripts/MMMC.tcl` 如何用 SS/FF 双角做建立/保持时间分析。

## 2. 前置知识

在进入本讲前，读者需要先理解几个 ASIC 后端的通用概念。本讲尽量用通俗语言解释，不默认读者做过版图。

- **门级网表（gate-level netlist）**：u6-l1 综合的产物，是一份文本文件（本项目里是 `SYN/output/FFT.v`），里面是标准单元实例（如 `BUFCKEHD`、`DFQ`）和它们之间的连线。它没有坐标、没有金属层，是 P&R 的**输入**。
- **标准单元（standard cell）**：工艺厂（foundry）预先做好的、高度固定、逻辑功能固定的小电路块（反相器、寄存器、与门……）。P&R 的核心工作之一就是把这些单元排进一个矩形「核心区」。
- **金属层（metal layer）**：芯片内部像多层高架路，本项目工艺有 8 层金属（`metal1`~`metal8`，见 `Default.globals` 的 LEF 配置）。低层（metal1/2）细而密，做单元内部连线；高层（metal7/8）粗而宽，做电源和长距离信号。
- **物理库 LEF / 时序库 lib**：LEF 描述单元的物理尺寸与连线规则，lib 描述单元的时序/功耗。它们由 `Default.globals` 和 `MMMC.tcl` 在流程启动时载入。
- **setup（建立时间）/ hold（保持时间）**：寄存器要在时钟沿到来前 `setup` 时间内把数据稳住，在时钟沿后 `hold` 时间内保持不变。综合阶段用的是理想时钟，P&R 阶段要逐步用真实时钟树和真实连线寄生去复算。
- **工艺角（corner）**：同一工艺在不同 PVT（工艺偏差/电压/温度）下速度不同。慢角 **SS**（ss1p08v125c，1.08V/125℃）速度最慢、查 setup 最严苛；快角 **FF**（ff1p32vm40c，1.32V/−40℃）速度最快、查 hold 最严苛。这套「双角分析」就是 u6-l2 提到的 MMMC，本讲会看到它在 Innovus 里的落地。
- **SDF / GDS**：SDF（Standard Delay Format）把每个连线和单元的延迟写成数值表，供门级仿真反标；GDS（GDSII）是版图的工业标准二进制格式，送交晶圆代工流片。

> 与上一讲的关系：u6-l3 读到综合后 `clk` 路径关键路径 9.68 ns、slack 0.00。那是**综合后、布线前**的时序——用的是 wireload 模型估算连线延迟。本讲走完 P&R 后，`FFT.sdf` 里装的是**真实布线寄生**算出的延迟，门级仿真（u5-l1 的 GATE 模式）正是用它来最终签核。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 行数 | 作用 |
|------|------|------|
| `Pnr/scripts/pnr.tcl` | 60 行 | **主流程脚本**， Innovus 按行执行，串起 6 个物理阶段。本讲的绝对主角。 |
| `Pnr/scripts/MMMC.tcl` | 14 行 | 多模多角（MMMC）配置：定义 FF/SS 两个 RC 角、两个 library set、两个 analysis view，并指定 SS 查 setup、FF 查 hold。 |
| `Pnr/scripts/Clock.ctstch` | 58 行 | CTS 约束文件，声明时钟根 `clk`、周期 10ns、最大 skew 0.1ns、可用时钟缓冲器型号。 |
| `Pnr/scripts/Default.globals` | 31 行 | Innovus 全局配置，声明 LEF/lib 路径、电源/地网络名（VCC/GND）、顶层单元 `FFT`、输入网表 `../syn/output/FFT.v`。 |
| `Pnr/output/FFT.gds` | ~12 MB / 124413 行 | **最终版图**，GDSII 二进制文本，streamOut 产物，送交流代工。 |
| `Pnr/output/FFT.sdf` | ~12.7 MB | 布线后真实寄生算出的门级时序文件，供 GATE 仿真反标。 |
| `Pnr/output/FFT.v` | ~1.5 MB | 布线后含电源地的网表（`-includePowerGround`）。 |

> 工程注意：`pnr.tcl` 第 2 行 `loadConfig ./Default.conf` 引用的 `Default.conf` 并不在仓库里（仓库只有 `Default.globals`）；`pnr.tcl` 第 59 行的 `streamOut.map` 路径是作者本机绝对路径 `/home/abdelhay_ali/Desktop/...`。这些都是**机器相关、换机必须改**的设置，与 u6-l1 综合脚本里 `search_path` 的性质一致。本讲分析时把它们当作环境占位符处理。

## 4. 核心概念与源码讲解

本讲把 60 行的 `pnr.tcl` 按物理流程拆成 4 个最小模块。读者可以把整张脚本理解成一条「流水线」：网表从顶部进入，每经过一段就被赋予更多物理属性，最后从底部流出 GDS 版图。

### 4.1 Floorplan 与电源网络

#### 4.1.1 概念说明

综合后的网表里，几万个标准单元像散落一地的乐高积木，没有坐标。**Floorplan（版图规划）**就是先在硅片上画一个矩形「核心区（core）」，规定单元能放在哪、电源环怎么绕、IO 引脚在哪。一个好的 floorplan 要预留足够空间给连线（否则后续拥塞），又不能太浪费面积。本项目用 70% 利用率——即核心区里 70% 面积摆单元、30% 留给布线通道，是数字模块的常见经验值。

电源网络是 floorplan 之后第一件要事。芯片里的 VCC（电源）和 GND（地）不能靠一条线供给所有单元，否则离电源远的单元会因 IR 压降（电流×寄生电阻）而电压不足、时序崩盘。标准做法是**先沿核心区四周打一圈电源环（ring），再在高层金属上铺一排排电源条带（stripe）**，让每个单元附近都能「就近取电」。

#### 4.1.2 核心流程

floorplan + 电源网络阶段的执行顺序是：

1. 载入配置与 MMMC，设工艺节点。
2. `floorPlan` 画核心区（长宽比、利用率、四周边距）。
3. `addRing` 沿核心四周打 VCC/GND 环。
4. `addStripe` 在高层金属打 VCC/GND 条带（一层水平、一层垂直，交织成网）。
5. `sroute` 把标准单元的电源地引脚连到环/条带上。
6. `addEndCap` 在每一行两端塞「端帽单元」防止工艺缺陷。
7. `timeDesign -prePlace`：在还没摆任何单元前，先打一次时序基线。

#### 4.1.3 源码精读

流程启动的前 4 行，载入全局配置、提交配置、载入 MMMC 多角设置、声明 130nm 工艺：

[Pnr/scripts/pnr.tcl:L2-L5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L2-L5) —— 载入 `Default.conf`、提交、`source MMMC.tcl`、设 130nm 工艺节点。`commitConfig` 把 MMMC 视图「冻结」进设计，之后才能做时序分析。

接下来是 floorplan 的核心一行：

[Pnr/scripts/pnr.tcl:L7-L8](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L7-L8) —— `floorPlan -r 1 0.7 20.0 20.0 20.0 20.0`。`-r` 表示 ratio 模式，参数依次是：**长宽比 = 1**（核心区接近正方形）、**利用率 = 0.7**（70%）、**四边各留 20µm 核心到芯片边的间距**。Innovus 据此自动算出核心区尺寸；第 8 行 `#loadIoFile FFT.io` 被注释，说明本项目没有自定义 IO 引脚文件（IO 由 pad ring 另外处理或留默认）。

电源环：沿核心四周用 metal1 打 VCC/GND 环，宽 3µm、间距 1µm，并用 stacked via 贯通 metal1~metal8：

[Pnr/scripts/pnr.tcl:L10-L10](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L10-L10) —— `addRing`，给 VCC/GND 两个网络各打一圈环。

电源条带：用更高层金属铺成网格，一层水平一层垂直，交织成供电网：

[Pnr/scripts/pnr.tcl:L11-L12](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L11-L12) —— 第 11 行用 `metal8` 打**水平**条带（宽 4µm、中心距 22µm），第 12 行用 `metal7` 打**垂直**条带（宽 4µm、中心距 20µm）。两层正交，形成覆盖全核心的供电网格。注意金属层越高越粗，适合走电源这种大电流网络。

接着把单元电源地引脚连到供电网，并加端帽：

[Pnr/scripts/pnr.tcl:L14-L17](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L14-L17) —— `sroute` 做特殊电源连线（注意这里写的是 `{VCC VGND}`，而上下文 ring/stripe 用的是 `{VCC GND}`，是一处命名上的小不一致）；第 16 行 `addWellTap`（衬底 tap 单元）被注释；第 17 行 `addEndCap` 在每行首尾塞 `FILLER4EHD` 端帽。

> 工程观察：`sroute` 的网络名是 `VGND`，而 `addRing/addStripe` 用的是 `GND`，二者不一致。这在实际跑通的设计里通常意味着要么 Innovus 做了别名映射，要么 `VGND` 实际未被驱动（死线）。读者在自己的流程里应统一命名，避免电源地没真正连上。

#### 4.1.4 代码实践

**实践目标**：理解 floorplan 利用率对核心区面积的影响。

**操作步骤**：

1. 打开 `Pnr/scripts/pnr.tcl` 第 7 行，确认参数为 `-r 1 0.7 20.0 20.0 20.0 20.0`。
2. 回忆 u6-l3 的面积结论：综合后组合+非组合总面积约 **202213 µm²**（≈ 0.202 mm²）。
3. 用利用率公式反推核心区面积：核心面积 ≈ 单元总面积 / 利用率 = 202213 / 0.7 ≈ **289000 µm²**。
4. 对比 README 第 89 行给出的版图后面积 **1.27 mm² = 1270000 µm²**——这远大于核心区估算，因为完整芯片还包括 pad ring、IO、电源环、填充等大量非逻辑区域。

**需要观察的现象**：核心区（放逻辑单元）只占整芯片面积的零头，芯片面积主要由 IO/pad 主导。

**预期结果**：核心区约 0.29 mm²，整芯片 1.27 mm²，说明这是一个 IO 受限（pad-limited）的小逻辑设计。这一结论与「32 点 FFT 逻辑不大、但要做 ASIC 就得配齐电源/IO」的工程直觉一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把利用率从 0.7 提高到 0.9，对后续流程有什么风险？

**参考答案**：核心区更小、面积更省，但留给布线通道的空间变少，容易引发**拥塞（congestion）**，导致 `routeDesign` 阶段布不通或需要大量绕线，时序也会变差。0.7 是为了留余量换可布线性。

**练习 2**：为什么电源条带要分别用 `metal7`（垂直）和 `metal8`（水平）两层，而不是都用同一层？

**参考答案**：同一层金属上的线不能交叉（会短路），必须用正交的两层、中间靠 via 贯通，才能织成覆盖全核心的供电网格。高层金属更粗、电阻更小，适合承担电源这种大电流网络。

---

### 4.2 布局与 CTS

#### 4.2.1 概念说明

电源网就绪后，进入**布局（placement）**：`placeDesign` 把所有标准单元按合法行（row）摆进核心区，目标是降低连线长度、满足时序、避免拥塞。布局是基于综合时的时序约束（来自 `../syn/output/FFT.sdc`，即 u6-l2 的 10ns 时钟）做的。

布局之后最关键的一步是**时钟树综合（Clock Tree Synthesis，CTS）**。综合阶段（u6-l1/u6-l2）和布局阶段，时钟网络都是**理想（ideal）**的：Innovus 假设时钟信号从时钟源「瞬间同时」到达每个寄存器，skew（时钟偏差）为零。但真实芯片里时钟要靠缓冲器一级级驱动、靠金属线传输，到不同寄存器的延迟必然不同。**CTS 的任务就是插入缓冲器、精心连线，让时钟尽可能「同时」到达所有寄存器**，把 skew 控制在约束范围内（本项目 ≤ 0.1ns）。

CTS 是一条分水岭：CTS 之前用理想时钟看时序，CTS 之后用真实时钟树看时序——很多隐藏的 setup/hold 违例要到 CTS 后才暴露。

#### 4.2.2 核心流程

布局 + CTS 阶段（含中间优化与多个时序检查点）的顺序是：

1. `setPlaceMode` 配置布局选项（时钟门控感知、时序驱动、拥塞努力）。
2. `placeDesign` 做标准单元布局。
3. `timeDesign -preCTS`：布局后、CTS 前，用理想时钟看时序。
4. `optDesign -preCTS`：基于 preCTS 时序做优化（调单元尺寸、移动单元）。
5. `setCTSMode` 配置 CTS 选项。
6. `clockDesign`：依据 `Clock.ctstch` 约束综合时钟树。
7. `timeDesign -postCTS`：CTS 后，用真实时钟树看时序（setup + hold）。
8. `optDesign -postCTS`：基于 postCTS 时序再优化。

#### 4.2.3 源码精读

布局前的模式设置与布局命令：

[Pnr/scripts/pnr.tcl:L22-L23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L22-L23) —— `setPlaceMode` 打开 `clkGateAware`（时钟门控感知）、`timingDriven`（时序驱动）、`congEffort medium`（中等拥塞努力）、`ignoreScan`（忽略扫描链）；`placeDesign` 执行布局。`saveDesign ./saving/FFT_Placed.enc` 把布局结果存盘（`.enc` 是 Innovus 的设计存档目录）。

CTS 配置与综合：

[Pnr/scripts/pnr.tcl:L32-L33](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L32-L33) —— `setCTSMode` 配置 CTS：`opt true`（开启优化）、`routeClkNet true`（CTS 顺便把时钟网也布了）、时钟网限定在 metal1~metal4 走线；`clockDesign -specFile ./Clock.ctstch` 读 CTS 约束文件做时钟树综合。

CTS 约束文件的关键内容——声明时钟根、周期、skew 目标、可用缓冲器：

[Pnr/scripts/Clock.ctstch:L32-L46](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/Clock.ctstch#L32-L46) —— 时钟根 `clk`、周期 `10ns`、最大 skew `0.1ns`、SinkMaxTran（末端翻转时间）`0.8ns`、允许使用 `BUFCKEHD/BUFCHD/BUFCKGHD` 三种时钟缓冲器。注意 `# default value` 注释说明这些 skew/transition 数值是 Innovus 默认，作者未做收紧。

> 衔接：这个 10ns 周期与 u6-l2 综合约束的 `create_clock 10ns` 完全一致，CTS 的 0.1ns skew 目标也与 u6-l2 的 `set_clock_uncertainty 0.1` 对应——前者是 CTS 要达到的目标，后者是综合阶段预留的裕量，二者数值上是同一笔「时钟不确定度预算」。

CTS 之后的时序检查与优化：

[Pnr/scripts/pnr.tcl:L34-L40](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L34-L40) —— `timeDesign -postCTS`（注意：CTS 后**不再加 `-idealClock`**，时钟已是真实传播的树）分别查 setup（第 34 行）和 hold（第 35 行）；`optDesign -postCTS` 做优化；再查一次 postCTS 时序。

#### 4.2.4 代码实践

**实践目标**：理解「CTS 前理想时钟」与「CTS 后真实时钟」的本质差异。

**操作步骤**：

1. 在 `pnr.tcl` 中对比第 19、24 行与第 34 行的 `timeDesign` 命令。
2. 观察：第 19、24 行都带 `-idealClock`，第 34 行不带。
3. 打开 `Clock.ctstch` 第 38 行确认周期是 `10ns`，第 41 行确认 skew 目标是 `0.1ns`。
4. 思考：CTS 前的时序报告里，时钟到每个寄存器的延迟被当成多少？

**需要观察的现象**：CTS 前，时钟网络延迟假设为 0（或一个 ideal 值），所有寄存器时钟沿「同时」到达；CTS 后，每条时钟路径有了真实缓冲器延迟，时序报告里会出现真实的时钟延迟与 skew。

**预期结果**：CTS 前 setup 看起来很好（理想时钟高估了可用时间），CTS 后 setup slack 通常会下降——因为真实 skew 吃掉了一部分时钟周期。这就是为什么脚本在每个关键节点都重新 `timeDesign`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CTS 之后 `timeDesign` 不再加 `-idealClock`？

**参考答案**：CTS 已经插入了真实的时钟缓冲器和连线，时钟网络不再是理想源，而是有真实延迟和 skew 的树。此时必须用「传播时钟（propagated clock）」来分析时序，`-idealClock` 反而会忽略刚建好的时钟树，失去 CTS 后分析的意义。

**练习 2**：`Clock.ctstch` 第 41 行 `MaxSkew 0.1ns` 和综合时 `set_clock_uncertainty 0.1` 是什么关系？

**参考答案**：综合阶段还没有时钟树，只能用 `uncertainty` 预留 0.1ns 的 skew+jitter 裕量来收紧约束；CTS 阶段则要把真实 skew 控制在这 0.1ns 以内。两者是「预算」与「实现」的关系——综合按 0.1ns 裕量收口，CTS 按 0.1ns 目标实现。

---

### 4.3 详细布线与优化

#### 4.3.1 概念说明

布局把单元摆好了，但单元之间还只是「逻辑连线」，没有变成真正的金属轨道。**布线（routing）**就是用各层金属线和通孔（via），把每一条逻辑连线变成物理可制造的金属图形。布线分两步：先做**全局布线（global route）**规划每条线走哪层、走哪个通道，再做**详细布线（detail route）**把线落到具体的金属轨道上、打上通孔，并检查设计规则（DRC）。

布线完成后才有了真实的金属长度和寄生电容/电阻，此时的时序分析最接近流片真实情况。布线通常还会引入新的拥塞、串扰和 DRC 违例，所以布线后还要再做一轮 `optDesign` 优化（修 setup、修 hold、修 DRC），并塞入填充单元（filler）填补行内间隙以保持工艺连续性。

#### 4.3.2 核心流程

布线与优化阶段的顺序是：

1. `routeDesign` 一次性完成全局 + 详细布线，并优化通孔和连线。
2. `timeDesign -postRoute`：用布线后估算的寄生查 setup + hold。
3. `optDesign -postRoute`（修 setup/面积/DRC）+ `optDesign -postRoute -hold`（专修 hold）。
4. `timeDesign -postRoute`：优化后再查一次。
5. `addFiller`：在行内缝隙塞填充单元。

#### 4.3.3 源码精读

布线命令：

[Pnr/scripts/pnr.tcl:L42-L42](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L42-L42) —— `routeDesign -globalDetail -viaOpt -wireOpt`，`-globalDetail` 表示连续做全局+详细布线，`-viaOpt`/`-wireOpt` 在布线中优化通孔和连线。

布线后时序检查、双轮优化、再检查、塞填充：

[Pnr/scripts/pnr.tcl:L43-L52](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L43-L52) —— 第 43~44 行 `timeDesign -postRoute` 分别查 setup（`-drvReports`）和 hold；第 46~47 行两轮 `optDesign`（一轮主修 setup/面积，一轮 `-hold` 专修保持时间，这是布线后修 hold 的标准做法，因为 hold 违例要等真实布线寄生才准）；第 48 行存盘 `FFT_route.enc`；第 49~50 行再查一次 postroute 时序；第 52 行 `addFiller` 塞入 8 种尺寸的 `FILLER*HD` 填充单元，把行内所有缝隙填满，保证制造时多晶硅/扩散层连续。

> 工程要点：`optDesign -postRoute -hold`（第 47 行）是修 hold 违例的关键命令。hold 违例往往是「数据太快、抢在时钟前到达」，修法是给数据路径加延迟（插缓冲器、换慢单元）。hold 必须在布线后修，因为只有真实寄生才能算准数据路径延迟——这正解释了为什么脚本把 hold 优化专门放在 postRoute。

#### 4.3.4 代码实践

**实践目标**：通过阅读脚本理解「为什么 hold 检查要分两次、放在不同阶段」。

**操作步骤**：

1. 在 `pnr.tcl` 中用搜索定位所有带 `-hold` 的 `timeDesign` 与 `optDesign`。
2. 列出它们出现的行号：第 20、25、30、35、40、44、47、50 行。
3. 观察 hold 检查从 `prePlace` 一直做到 `postRoute`，但**专门修 hold 的 `optDesign -hold` 只在第 47 行（postRoute）出现一次**。
4. 思考：为什么 preCTS 阶段只查 hold 而不专门修？

**需要观察的现象**：每个节点都查 hold（`timeDesign ... -hold`），但只有 postRoute 才真正动手修（`optDesign -postRoute -hold`）。

**预期结果**：因为 CTS 前/CTS 后的 hold 数值还不可信（时钟树和布线寄生都没定），提前修反而白费力气、甚至越修越乱；只有布线后寄生确定，修 hold 才有意义。这是后端流程「先观察、后定点修复」的标准节奏。

#### 4.3.5 小练习与答案

**练习 1**：`addFiller`（第 52 行）塞填充单元的工艺目的是什么？

**参考答案**：标准单元按行摆放，行内相邻单元之间常有缝隙。填充单元（FILLER）填满这些缝隙，保证扩散层（N-well/substrate）连续、符合代工的 DRC 规则，提升可制造性和成品率，并不承担逻辑功能。

**练习 2**：为什么布线后要跑**两轮** `optDesign`（第 46、47 行），一轮不带 `-hold`、一轮带 `-hold`？

**参考答案**：setup 和 hold 是一对矛盾——修 setup 倾向于「让数据更快」，修 hold 倾向于「让数据更慢」。先跑主优化（setup/面积/DRC）确定主时序，再单独跑 `-hold` 在不破坏 setup 的前提下定点加延迟，避免两类优化互相打架。

---

### 4.4 RC 提取与 GDS 输出（签核输出）

#### 4.4.1 概念说明

布线 + 填充完成后，芯片的物理版图已经完整。但到此为止，时序分析用的寄生参数仍是 Innovus **布线过程中内部估算**的。**签核（signoff）**阶段要做两件收尾的事：

第一，**RC 提取（extractRC）**：根据最终金属图形精确计算每条连线的电阻和电容，生成一个统一的寄生模型供后续所有时序分析使用。提取后的时序是流片前最准的数值。

第二，**导出交付物**：把设计以各种格式交出去——
- `write_sdf`：把布线后真实延迟写成 SDF，供门级仿真（u5-l1 的 GATE 模式）反标。
- `saveNetlist`：导出含电源地的网表，供 LVS（版图与网表一致性检查）。
- `report_power`：基于真实寄生重报功耗。
- `saveDesign`：存盘整个 Innovus 设计，供日后 ECO（工程变更）。
- `streamOut`：把版图导出成 **GDSII** 文件，这是送交晶圆代工流片的工业标准格式。

至此，RTL→综合→P&R 的 ASIC 主线全部走完，产出的 `FFT.gds` 就是可以拿去流片的版图。

#### 4.4.2 核心流程

签核输出阶段的顺序是：

1. `extractRC` 精确提取寄生 RC。
2. `write_sdf` 导出布线后 SDF。
3. `saveNetlist -includePowerGround` 导出含电源地网表。
4. `report_power -leakage` 报告含漏电的功耗。
5. `saveDesign` 存盘。
6. `streamOut` 导出 GDSII 版图。

#### 4.4.3 源码精读

签核输出的一组命令：

[Pnr/scripts/pnr.tcl:L54-L60](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L54-L60) —— 逐行：
- 第 54 行 `extractRC -outfile ./FFT_rc`：精确提取寄生 RC，输出到 `FFT_rc`。
- 第 55 行 `write_sdf FFT.sdf`：写布线后 SDF（这就是 `Pnr/output/FFT.sdf`，约 12.7 MB，u5-l1 GATE 仿真要用的那个）。
- 第 56 行 `saveNetlist ./FFT.v -includePowerGround`：导出含电源地连线的网表。
- 第 57 行 `report_power -leakage -outfile FFT.pwr`：报告含漏电功耗。
- 第 58 行 `saveDesign ./saving/FFT.enc`：最终存盘。
- 第 59~60 行 `streamOut ./FFT.gds -units 100 -mapFile $map`：用 streamOut 映射文件（`streamOut.map`，定义每个 LEF 层到 GDS 层号的映射）把版图导成 GDSII，单位 100（即 0.01µm/database unit）。`$map` 是作者本机绝对路径，换机必须改。

最终产物的体量（来自仓库实测）：

[Pnr/output/FFT.gds](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/output/FFT.gds) —— `streamOut` 产出的 GDSII 版图，约 12 MB、124413 行，内含所有金属层图形与通孔，是送交流代工的最终版图。

SDF 文件的头部信息能印证它是布线后产物：

[Pnr/output/FFT.sdf:L1-L19](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/output/FFT.sdf#L1-L19) —— `PROGRAM "Innovus"`、`VOLTAGE 1.08`、`TEMPERATURE 125`、`TIMESCALE 1.0 ns`，确认这是 Innovus 在 SS 角（1.08V/125℃）下、布线后提取的真实延迟文件，正是 u5-l1 GATE 仿真 `$sdf_annotate` 反标的对象。

> 工程注解：`streamOut.map`（仓库中 3590 行）是一张「LEF 层名 → GDS 层号」的对照表，例如 `metal1 NET 1 0`、`metal2 NET 22 0`（见 `Pnr/output/streamOut.map` 开头）。它告诉 `streamOut` 每个 Innovus 的逻辑层在 GDS 里对应的层号/数据类型。代工厂会提供官方 map 文件，本仓库里这份是 UMC 130nm 的对应表。

#### 4.4.4 代码实践

**实践目标**：把 60 行脚本按 6 个物理阶段切分，建立完整的 P&R 全流程地图。

**操作步骤**：

1. 打开 `Pnr/scripts/pnr.tcl`，按下表把每一行归入对应阶段（这是本讲的综合实践预热）：

| 阶段 | 代表性行 | 代表性命令 |
|------|----------|------------|
| ① 环境载入 | 2~5 | `loadConfig` / `source MMMC.tcl` / `setDesignMode` |
| ② Floorplan + 电源 | 7~20 | `floorPlan` / `addRing` / `addStripe` / `sroute` / `timeDesign -prePlace` |
| ③ 布局 | 22~30 | `setPlaceMode` / `placeDesign` / `optDesign -preCTS` |
| ④ CTS | 32~40 | `setCTSMode` / `clockDesign` / `optDesign -postCTS` |
| ⑤ 布线 + 优化 | 42~52 | `routeDesign` / `optDesign -postRoute` / `addFiller` |
| ⑥ 签核输出 | 54~60 | `extractRC` / `write_sdf` / `saveNetlist` / `streamOut` |

2. 单独把 4 个 `timeDesign` 节点摘出来，对比它们的 `-idealClock` 有无：

| 节点 | 行号 | `-idealClock`？ | 物理含义 |
|------|------|-----------------|----------|
| prePlace | 19 | 有 | floorplan+电源完成，单元未摆，时钟理想 |
| preCTS | 24 | 有 | 布局完成，时钟仍理想 |
| postCTS | 34 | **无** | 时钟树已建，真实时钟 |
| postRoute | 43 | **无** | 布线完成，真实时钟 + 真实寄生 |

3. 思考：为什么 prePlace 和 preCTS 都加 `-idealClock`，而 postCTS/postRoute 不加？

**需要观察的现象**：`-idealClock` 只出现在 CTS 之前的两个节点；CTS 一过就改用真实传播时钟。

**预期结果**：因为 `-idealClock` 的字面含义就是「把时钟当理想源」，CTS 之前时钟树确实还不存在，只能理想化；CTS 之后时钟树已是真实电路，必须用传播时钟分析，否则等于无视了 CTS 的成果。

#### 4.4.5 小练习与答案

**练习 1**：`extractRC`（第 54 行）产出的寄生模型，与 `write_sdf`（第 55 行）产出的 SDF 是什么关系？

**参考答案**：`extractRC` 提取的是原始寄生 RC（电阻电容），是 Innovus 内部的时序计算依据；`write_sdf` 则把基于这些寄生算出的**延迟值**写成标准格式文件（SDF）交给仿真器。前者是「因」（寄生），后者是「果」（延迟）。两个动作一前一后，缺一不可。

**练习 2**：README 第 89 行说版图后「面积 1.27 mm、功耗 28 mW」，而 u6-l3 综合报告是 202213 µm²、9.95 mW。为什么版图后数值更大？

**参考答案**：① 面积增大：版图含电源环/条带、填充单元、时钟缓冲器、CTS 新增单元、pad/IO 等综合阶段不存在的物理结构；② 功耗增大：版图后基于真实连线寄生重算，时钟树缓冲器和长线负载带来额外翻转功耗，且 `report_power` 用了更高精度。综合阶段（u6-l3）用 `-analysis_effort low` + wireload 模型偏保守偏低，版图后才是更接近流片的真实数值。

---

## 5. 综合实践

把本讲全部知识串起来，完成下面的「P&R 全流程解读」任务。

**任务**：以 `Pnr/scripts/pnr.tcl` 为对象，产出一份《32 点 FFT 布局布线流程解读》，包含三部分。

**第一部分：6 阶段切分表**

按「环境载入 → Floorplan+电源 → 布局 → CTS → 布线+优化 → 签核输出」6 个阶段，列出每个阶段的起止行号、代表性命令（至少 2 条）、该阶段产出的物理结构（如「电源环/条带」「时钟树」「金属布线」「GDS 版图」）。可参考 4.4.4 节的表格扩展。

**第二部分：4 节点 timeDesign 对比**

写出 `prePlace / preCTS / postCTS / postRoute` 四个节点各自的：
- 对应行号；
- 是否带 `-idealClock`，以及**为什么**（用「时钟树是否存在」解释）；
- 该节点用的时钟模型（理想 vs 真实传播）；
- 该节点用的寄生模型（无 vs 估算 vs 精确提取）。

并解释：为什么 hold 的**定点修复**（`optDesign -hold`）只出现在 postRoute？

**第三部分：MMMC 双角落地**

打开 `Pnr/scripts/MMMC.tcl`，回答：
- 第 3~4 行定义了哪两个 RC 角？温度各是多少？
- 第 5~6 行的 `WCCOM` 和 `BCCOM` 操作条件分别对应哪个 lib 文件（ss 还是 ff）？
- 第 14 行 `set_analysis_view -setup {SS} -hold {FF}` 的含义是什么？为什么 setup 用慢角、hold 用快角？

**预期产出**：一份约 500 字的解读，覆盖 6 阶段命令、4 时序节点、MMMC 双角，能说清「时钟何时从理想变真实」「寄生何时从估算变精确」两个关键转折点。如果手头没有 Innovus 环境，不必实际运行，重点是把脚本读透。

## 6. 本讲小结

- `pnr.tcl` 是一条 60 行的物理流水线，把综合网表变成可流片的 GDS 版图，可切为 **6 个阶段**：环境载入 → Floorplan+电源 → 布局 → CTS → 布线+优化 → 签核输出。
- **Floorplan** 用 `-r 1 0.7` 设 70% 利用率与正方形核心；**电源网络**用 `addRing`（metal1 环）+ `addStripe`（metal7 垂直 / metal8 水平条带）织成供电网，`sroute` 把单元电源地接上网。
- **CTS 是分水岭**：CTS 前用 `-idealClock`（时钟理想、skew=0），CTS 后改用真实传播时钟树（skew 目标 0.1ns，见 `Clock.ctstch`）；`timeDesign` 在 prePlace/preCTS/postCTS/postRoute 四个节点复查时序。
- **布线后**才修 hold（`optDesign -postRoute -hold`，第 47 行），因为只有真实布线寄生才能算准 hold；`addFiller` 塞填充单元保证工艺连续性。
- **签核输出**用 `extractRC` 精确提寄生、`write_sdf` 出布线后 SDF、`streamOut` 出 GDSII 版图；产出的 `FFT.sdf`（SS 角/1.08V/125℃）正是 u5-l1 GATE 仿真反标对象。
- **MMMC 双角**：`set_analysis_view -setup {SS} -hold {FF}`——慢角 SS 查 setup 最严、快角 FF 查 hold 最严，与 u6-l2 综合阶段的多角策略一脉相承。

## 7. 下一步学习建议

本讲走完了 RTL→综合→P&R 的 ASIC 主线，产出了 GDS 版图与布线后 SDF。建议读者接下来：

1. **回头验证版图功能**：结合 u5-l1 的 GATE 仿真模式，用本讲产出的 `Pnr/output/FFT.sdf` 反标到门级网表 `Pnr/output/FFT.v` 上跑 testbench，确认布线后时序仍能满足 SNR≥40 dB（这是 README 第 85 行「all the test-bench data passed」的落地证据）。
2. **进入架构反思**：u7-l2（架构取舍与设计权衡）会综合本讲的版图后面积（1.27 mm²）、功耗（28 mW）与 u6-l3 的综合结果，讨论 SDC 流水线在面积/功耗/吞吐上的权衡，把 u6 的数据用起来。
3. **延伸阅读**：若想深入物理实现，可阅读 Innovus 官方文档的 `setPlaceMode`/`setCTSMode`/`routeDesign` 各项参数含义；对比 `Pnr/output/FFT.gds`（核心版图）与 `FFT_CHIP.gds`（含 pad 的整芯片版图，约 15.7 MB），理解核心与整芯片的差别。
