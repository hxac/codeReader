# 时序约束与 74.25MHz 时钟（SDC）

## 1. 本讲目标

在 u1-l3 里，我们第一次提到 `sharp.sdc` 里有一条 `create_clock`，它把 74.25 MHz、720p 视频时钟告诉 Quartus，并得知当时序收敛时 Setup Slack 是 +0.658 ns。但当时我们只把它当成「工程里的一行配置」带过，并没有真正读它。

本讲要打开这个文件逐行精读。读完你应该能够：

1. 说清楚 SDC（Synopsys Design Constraints）约束文件在 FPGA 流程里扮演什么角色、为什么必须有它。
2. 读懂 `create_clock`（输入时钟）与 `create_generated_clock`（输出派生时钟）两条约束的每一个参数，并把 13.46 ns 周期换算回 74.25 MHz、720p 视频时钟。
3. 读懂 `set_input_delay` / `set_output_delay` 的 `-max` / `-min` / `-rise` / `-fall` 含义，理解 max 用于建立时间（setup）、min 用于保持时间（hold）。
4. 理解 `derive_clock_uncertainty` 为什么会吃掉时序余量。
5. 会用「把周期改小」的方法人为制造时序违例（negative slack），从而直观体会时序收敛。

本讲对应的最小模块是：**输入/输出时钟约束**、**IO 延迟约束**、**时钟不确定度**，外加一节**时序收敛与 Slack** 把三者串起来。

## 2. 前置知识

### 静态时序分析（STA）是干什么的

FPGA 内部有成千上万个触发器（flip-flop），它们之间用组合逻辑相连。组合逻辑需要时间传播信号：门有延迟、连线有延迟。**静态时序分析（Static Timing Analysis, STA）** 不跑仿真、不施加激励，而是把电路里所有「触发器 → 组合逻辑 → 触发器」的路径都穷举出来，检查每条路径上的数据能否在时钟周期内「按时到达」。

要做这个检查，分析器必须知道两件事：

- **时钟长什么样**：周期多少、相位如何。这就是 `create_clock` 提供的。
- **芯片外部世界长什么样**：外部器件给 FPGA 的数据相对于时钟沿提前或落后多少？FPGA 输出的数据要被外部器件在多严格的窗口里接收？这就是 `set_input_delay` / `set_output_delay` 提供的。

如果没有这些约束，分析器就不知道该用多长的「标尺」去量路径，也就无法报「通过 / 违例」。**SDC 就是这把标尺的说明书。**

### 建立时间与保持时间

- **建立时间（setup time, \(t_{\text{su}}\)）**：在捕获时钟沿到来**之前**，数据必须已经稳定的最短时间。检查的是「数据到得太晚」——对应**慢路径（max delay）**分析。
- **保持时间（hold time, \(t_{\text{h}}\)）**：在捕获时钟沿之后，数据必须继续稳定的最短时间。检查的是「数据到得太早、把上一拍数据冲掉」——对应**快路径（min delay）**分析。

### Slack（余量）

时序分析用 **slack** 表达余量：

\[ \text{Slack} = \text{要求时间（Required）} - \text{到达时间（Arrival）} \]

- Slack ≥ 0：**满足（met）**，路径跑得过来。
- Slack < 0：**违例（violation）**，路径可能跑不过来，需要改设计或放宽周期。

对内部寄存器到寄存器的建立时间检查（同一时钟、下一拍捕获），可以写成：

\[ \text{Slack}_{\text{setup}} = T - t_{\text{data}} - t_{\text{su}} - t_{\text{uncertainty}} + t_{\text{skew}} \]

其中 \(T\) 是时钟周期，\(t_{\text{data}}\) 是组合路径延迟，\(t_{\text{uncertainty}}\) 是时钟不确定度，\(t_{\text{skew}}\) 是 launch 与 capture 之间的时钟偏斜。这条公式是本讲第 4.4 节分析「为什么把周期改小会违例」的依据。

> 名词提示：Quartus 里做 STA 的工具早期叫 **TimeQuest**，新版 Quartus Prime 里叫 **Timing Analyzer**，本质都是 STA 引擎。本讲统一称「时序分析器 / STA」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲解读的部分 |
|------|------|----------------|
| `FPGA-Design/sharp.sdc` | **本讲主角**：全文件仅 25 行，定义时钟、派生时钟、IO 延迟、时钟不确定度 | 第 10、11、14–21、24 行 |
| `FPGA-Design/sharp.vhd` | 顶层实体，定义了 `clk`（输入时钟）与 `clk_o`（输出时钟）端口 | 第 13、118 行（`clk_o <= clk` 直通） |
| `FPGA-Design/FIR.qsf` | 工程配置；其中 `SDC_FILE` 决定 SDC 是否被纳入编译 | 第 126 行 |

一句话总览：`sharp.sdc` 把顶层 `sharp` 实体的**每一个端口**都分进了一个时序类别——`clk` 当主时钟、`clk_o` 当派生时钟、`reset_n` 与 `*_in*` 当输入延迟、`*_out*` 与 `led*` 当输出延迟。分工干净，没有遗漏也没有重叠。

## 4. 核心概念与源码讲解

### 4.1 输入与输出时钟约束（create_clock / create_generated_clock）

#### 4.1.1 概念说明

时钟是整个时序分析的「节拍器」。STA 要先把节拍器定义出来，才能用「一个节拍（一个周期）」去度量路径。定义时钟的命令是 `create_clock`，它告诉分析器：

- **在哪个端口上有时钟**（`[get_ports {clk}]`）；
- **周期多长**（`-period`）；
- **叫什么名字**（`-name`，方便后续命令引用它）。

本项目只有一个外部时钟 `clk`，它直接来自板上的 74.25 MHz 视频时钟源。此外顶层有一个 `clk_o` 端口，它在硬件里是 `clk` 的组合直通（`clk_o <= clk;`，见 `sharp.vhd:118`），用来给下游器件当同步时钟。因为 `clk_o` 也是一个真实存在的、对外输出的时钟信号，分析器需要把它「登记」成一个**派生时钟（generated clock）**，这样输出路径的时序检查才有捕获沿可用。登记派生时钟的命令是 `create_generated_clock`。

关键直觉：`clk` 是「源头时钟（master）」，`clk_o` 是「从源头派生出来的时钟」，两者同频同相（因为硬件上是直通），但在 SDC 里必须分别声明，因为它们分别管「输入侧」和「输出侧」的时序检查。

#### 4.1.2 核心流程

时钟约束进入分析器的流程：

1. Quartus 编译时，按 `FIR.qsf` 里的 `set_global_assignment -name SDC_FILE sharp.sdc` 加载 SDC。
2. STA 引擎读到 `create_clock`，在端口 `clk` 上建立一个周期为 13.46 ns 的时钟，命名为 `input_clk`，并自动推出它的频率。
3. 读到 `create_generated_clock`，在端口 `clk_o` 上建立一个派生时钟，命名为 `output_clk`，并指明它的源是 `clk`、主时钟是 `input_clk`。
4. 此后所有寄存器都被这两把「尺子」之一度量；IO 路径分别用 `input_clk`（输入）和 `output_clk`（输出）做检查。

把周期换算回频率：

\[ T = \frac{1}{f} = \frac{1}{74.25 \times 10^{6}\ \text{Hz}} \approx 13.468\ \text{ns} \]

SDC 里写的是 13.46 ns，四舍五入后的 74.25 MHz、720p 视频标准时钟。720p（1280×720）逐行扫描的像素时钟正是 74.25 MHz，所以这条约束**不是随便取的数**，而是与视频格式锁死的。

#### 4.1.3 源码精读

输入时钟约束：

[sharp.sdc:10](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L10)

```tcl
create_clock -name input_clk -period 13.46ns [get_ports {clk}]
```

- `-name input_clk`：给这个时钟起名 `input_clk`，后面 `set_input_delay` 就用这个名字引用它。
- `-period 13.46ns`：周期 13.46 ns（74.25 MHz）。
- `[get_ports {clk}]`：作用对象是顶层端口 `clk`。`get_ports` 是 SDC/Tcl 里「按名字取端口对象」的命令。

输出派生时钟约束：

[sharp.sdc:11](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L11)

```tcl
create_generated_clock -name output_clk -source [get_ports {clk}] -master_clock input_clk -add [get_ports {clk_o}]
```

- `-name output_clk`：派生时钟命名为 `output_clk`。
- `-source [get_ports {clk}]`：派生时钟的**源点**是 `clk` 端口，即告诉分析器 `clk_o` 的波形由哪里传播过来。
- `-master_clock input_clk`：指明它派生自哪个主时钟（`input_clk`），用于继承周期与相位关系。
- `-add`：以「追加」方式添加，避免覆盖既有约束（在多时钟设计里很常用）。
- `[get_ports {clk_o}]`：派生时钟作用对象是输出端口 `clk_o`。

这条约束之所以成立，是因为硬件上 `clk_o` 就是 `clk` 的直通：

[sharp.vhd:118](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.vhd#L118)

```vhdl
clk_o <= clk;
```

这是**组合赋值（连续赋值）**，没有触发器，所以 `clk_o` 与 `clk` 同频同相、只差一段连线延迟。正因为是直通，派生时钟不需要指定分频或移相（没有 `-divide_by` / `-multiply_by` / `-phase` 选项），分析器默认它和主时钟一致。

> 小知识：如果 `clk_o` 不是直通，而是经过了一个 PLL 或分频器，`create_generated_clock` 就必须用 `-divide_by` / `-multiply_by` 说明变换关系。本项目用直通是最省资源的做法——没有占用任何 PLL。

加载 SDC 的入口在 QSF：

[FIR.qsf:126](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/FIR.qsf#L126)

```tcl
set_global_assignment -name SDC_FILE sharp.sdc
```

这一行和 `VHDL_FILE` 并列，是 Quartus 决定「编译时把哪些约束喂给 STA」的唯一依据。漏写这一行，`sharp.sdc` 就会被无视，分析器只能用默认约束，结果很可能是「时序看起来全过」，但下载到板子上却跑不起来。

#### 4.1.4 代码实践

**目标**：验证 13.46 ns 与 74.25 MHz 的换算，并确认 `clk_o` 确实是 `clk` 的组合直通。

**操作步骤**：

1. 用计算器或 Octave 算 \(1/74.25\text{e}6 \times 1\text{e}9\)，应得到约 13.47 ns，与 SDC 的 13.46 ns 吻合。
2. 打开 `sharp.vhd`，定位到第 118 行 `clk_o <= clk;`，确认它在 `process` 之外、是并发赋值语句（没有 `wait until rising_edge`），所以是组合直通而非寄存器。
3. 在 Quartus 里编译工程后，打开 **Timing Analyzer**，在左侧 Tasks 里执行 `Report Clocks`（或看 Clock Summary 报告），应能看到 `input_clk`（周期 13.46 ns）与 `output_clk`（派生自 input_clk）两条。

**需要观察的现象**：Clock Summary 里两条时钟周期相同、相位一致；`clk_o` 被标注为 generated。

**预期结果**：两个时钟频率都显示约 74.25 MHz。具体报告数值**待本地验证**（取决于 Quartus 版本）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `-period 13.46ns` 改成 `-period 20.000ns`，对应的频率是多少？时序会更容易过还是更难过？

> **答案**：20 ns 对应 50 MHz。周期变长，时序预算变大，建立时间检查会**更容易**满足（slack 增大），但视频像素率会降到 50 MHz，不再是 720p 标准时序。

**练习 2**：为什么 `create_generated_clock` 要显式给出 `-master_clock input_clk`，能不能省略？

> **答案**：`-master_clock` 指明派生关系来自哪个主时钟。本项目源端口 `clk` 上只有 `input_clk` 一个时钟，省略时分析器也能推断；但显式写出更安全、可读性更好，尤其是在源端口存在多个时钟叠加（多周期、多时钟域）时，省略会导致歧义。

---

### 4.2 IO 延迟约束（set_input_delay / set_output_delay）

#### 4.2.1 概念说明

时钟定义好之后，分析器能检查「FPGA 内部寄存器到寄存器」的路径。但 FPGA 还要和外部世界打交道：

- **输入**：外部视频源把像素数据 `r_in/g_in/b_in`、同步信号 `vs_in/hs_in/de_in`、复位 `reset_n`、使能 `enable_in` 送进 FPGA。这些数据相对于时钟沿，存在一个外部延迟。
- **输出**：FPGA 把处理后的 `r_out/g_out/b_out`、同步信号、`led` 送出去，下游器件用 `clk_o` 当时钟来采样，同样存在一个外部延迟窗口。

`set_input_delay` 描述「外部数据相对于时钟沿，多久之后到达 FPGA 输入端口」；`set_output_delay` 描述「FPGA 输出数据相对于时钟沿，要满足下游器件多大的建立/保持窗口」。这两个命令把**芯片外部的板级时序**建模进 STA。

关键点：

- **`-max`**：最大延迟，描述最慢情况，用于**建立时间（setup）**检查。
- **`-min`**：最小延迟，描述最快情况，用于**保持时间（hold）**检查。
- **`-rise` / `-fall`**：分别约束上升沿、下降沿跳变；都不写则两者都约束。

#### 4.2.2 核心流程

`set_input_delay` 对建立时间的影响：外部延迟越大，数据到达 FPGA 内部第一个捕获寄存器的时间越晚，留给 FPGA 内部路径的时间就越少，越容易 setup 违例。所以 setup 用 `-max`。

`set_output_delay` 对建立时间的影响：它代表下游器件的建立窗口需求，本质是从「输出数据有效」到「下游采样沿」的可用时间被压缩了多少。`-max` 越大，输出路径越紧张。

本项目里，输入延迟 max = 0.1 ns、min = 0.05 ns，量级很小，说明 SDC 作者假定外部视频源与 FPGA 几乎共用同一个时钟域、板级走线延迟可忽略。这是一个**保守且宽裕**的假设。

#### 4.2.3 源码精读

最大延迟（同时覆盖 rise 和 fall，因为没有写 `-rise/-fall`）：

[sharp.sdc:14-L15](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L14-L15)

```tcl
set_input_delay  -clock input_clk  -max 0.1 [get_ports {reset_n *_in*}]
set_output_delay -clock output_clk -max 0.1 [get_ports {*_out* led*}]
```

- `set_input_delay`：约束 `reset_n` 与所有名字含 `_in` 的端口（`vs_in/hs_in/de_in/r_in/g_in/b_in/enable_in`）。注意 `enable_in` 也匹配 `*_in*`，因为它含子串 `_in`。
  - `-clock input_clk`：这些输入路径由 `input_clk` 发射。
  - `-max 0.1`：外部最大延迟 0.1 ns，用于 setup。
- `set_output_delay`：约束所有名字含 `_out` 的端口（`vs_out/hs_out/de_out/r_out/g_out/b_out`）与 `led`。注意 `clk_o` **不**匹配 `*_out*`（它是 `clk_o` 不是 `clk_out`），所以输出延迟不会去约束时钟端口，分工干净。
  - `-clock output_clk`：输出路径由派生时钟 `output_clk` 捕获——也就是下游器件用 `clk_o` 采样，这与硬件一致。
  - `-max 0.1`：最大 0.1 ns，用于 setup。

最小延迟（分别给 rise 和 fall 各加一条，`-add_delay` 表示追加而非覆盖）：

[sharp.sdc:17-L21](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L17-L21)

```tcl
set_input_delay  -add_delay -clock input_clk  -fall -min 0.05 [get_ports {reset_n *_in*}]
set_output_delay -add_delay -clock output_clk -fall -min 0.05 [get_ports {*_out* led*}]

set_input_delay  -add_delay -clock input_clk  -rise -min 0.05 [get_ports {reset_n *_in*}]
set_output_delay -add_delay -clock output_clk -rise -min 0.05 [get_ports {*_out* led*}]
```

综合第 14、17、20 三条 input 约束，每个输入端口的最终延迟模型是：

| 跳变沿 | max（setup 用） | min（hold 用） |
|--------|-----------------|----------------|
| rise   | 0.1 ns（来自第 14 行） | 0.05 ns（来自第 20 行） |
| fall   | 0.1 ns（来自第 14 行） | 0.05 ns（来自第 17 行） |

输出端口同理（第 15、18、21 行）。这种「一条 max + 两条 min(rise/fall)」的写法虽然啰嗦，但明确地把 setup（max）和 hold（min）在两个跳变沿上分别建模，是一份很规范的 IO 约束。

> 一致性提示：`FIR.qsf` 第 190 行有一条针对 `clk_n_o` 的 IO 标准约束，但 `clk_n_o` 并不出现在 `sharp.vhd` 的实体端口里（u6-l1 已指出这是「孤儿约束」）。它自然也不会被 `*_out*` 之类的 SDC 通配符匹配到。本讲的 IO 延迟约束只覆盖真实存在的端口，无需关心它。

#### 4.2.4 代码实践

**目标**：用通配符清单核对「每个端口被分进了哪个时序类别」。

**操作步骤**：

1. 打开 `sharp.vhd` 第 12–33 行的实体声明，列出全部端口。
2. 按 SDC 的四类（clock / generated clock / input delay / output delay）给每个端口归类：
   - `clk` → clock（第 10 行）
   - `clk_o` → generated clock（第 11 行）
   - `reset_n`、`vs_in`、`hs_in`、`de_in`、`r_in`、`g_in`、`b_in`、`enable_in` → input delay（`reset_n *_in*`）
   - `vs_out`、`hs_out`、`de_out`、`r_out`、`g_out`、`b_out`、`led` → output delay（`*_out* led*`）
3. 检查是否每个端口都恰好归入一类、无遗漏无重叠。

**需要观察的现象**：所有端口都能对号入座；`clk_o` 因名字不含 `_out` 而不会被输出延迟误伤。

**预期结果**：四类相加应等于实体里的全部端口（`clk_n_o` 是孤儿，不算）。

#### 4.2.5 小练习与答案

**练习 1**：为什么输入延迟用 `-clock input_clk`，而输出延迟用 `-clock output_clk`，而不是都用 `input_clk`？

> **答案**：输入数据是外部视频源**用 `clk`（即 input_clk）**驱动的，所以输入路径的发射时钟是 `input_clk`；输出数据是被下游器件**用 `clk_o`（即 output_clk）**采样的，所以输出路径的捕获时钟是 `output_clk`。两者对应不同的时钟域端点，必须分开。

**练习 2**：如果把 `set_input_delay` 的 `-max 0.1` 改成 `-max 5.0`，setup 检查会变松还是变紧？

> **答案**：变紧。`-max` 越大，表示外部数据到得越晚，留给 FPGA 内部从输入端口到第一个捕获寄存器的时间越少，建立时间 slack 越小，越容易违例。

---

### 4.3 时钟不确定度（derive_clock_uncertainty）

#### 4.3.1 概念说明

理想时钟是完美方波，周期恒定、跳变沿精准。真实时钟有**抖动（jitter）**——周期会在标称值附近随机晃动几十皮秒。此外，时钟经过片上时钟树到达不同触发器也会有偏斜（skew）。这些非理想性会**吃掉时序余量**。

**时钟不确定度（clock uncertainty）** 就是对这些非理想性的总度量。STA 在做建立/保持检查时，会从周期里**先减掉**不确定度，再判断路径是否满足：

\[ \text{可用周期} = T - t_{\text{uncertainty}} \]

所以不确定度越大，可用周期越小，slack 越紧。这解释了为什么 u1-l3 里 74.25 MHz 的 Setup Slack 只是 +0.658 ns 而不是很大——其中一部分就是被不确定度压缩后的结果。

#### 4.3.2 核心流程

手写 uncertainty 需要查阅器件手册、估算抖动均方根、再换算成峰值，很繁琐。SDC 里的 `derive_clock_uncertainty` 命令让 Quartus **根据器件模型自动算出**每个时钟的不确定度，并应用到对应路径上。它会综合考虑：

- 输入时钟的抖动；
- 时钟树插入延迟差异（skew）；
- PLL / 时钟网络带来的附加抖动。

对本项目，因为没有 PLL（`clk` 直入），不确定度主要来自外部时钟抖动与片上时钟树，数值通常在几十到一百多皮秒量级（**待本地验证具体值**）。

#### 4.3.3 源码精读

[sharp.sdc:24](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L24)

```tcl
# Automatically calculate clock uncertainty to jitter and other effects.
derive_clock_uncertainty
```

这一行没有任何参数，调用即生效。它对前面 `create_clock` / `create_generated_clock` 建立的所有时钟自动派生不确定度。这一步在 SDC 顺序里放在时钟与 IO 约束**之后**，因为它要先知道有哪些时钟，才能为它们算不确定度。

> 注意：`derive_clock_uncertainty` 是 Quartus/Intel 工具链特有的便捷命令；在通用 SDC 里等价的做法是用 `set_clock_uncertainty` 手动逐个指定。两者效果一样——都是把一个不确定度值加到对应时钟的 setup/hold 检查里。

#### 4.3.4 代码实践

**目标**：在 Quartus 报告里看到 `derive_clock_uncertainty` 实际产生的不确定度数值。

**操作步骤**：

1. 编译工程后打开 **Timing Analyzer**。
2. 查看某条 setup 或 hold 路径的详细报告（`Report Paths` 或在 Slack 报告里展开一条路径），定位到 **Clock Uncertainty** 这一栏。
3. 也可以在时钟总结报告里查找每个时钟对应的 uncertainty 值。

**需要观察的现象**：每条路径的时序余量计算里都有一项 Clock Uncertainty（带正负号），数值非零。

**预期结果**：uncertainty 大约在几十到一百多皮秒；具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `derive_clock_uncertainty` 这一行删掉，时序报告会怎样变化？删掉是安全的做法吗？

> **答案**：删掉后 STA 不再扣除不确定度，slack 数值会**变大**（看起来更好）。但这是虚假的好结果——真实芯片上仍有抖动，板子可能跑不起来。所以一般**不应**删掉，除非你用 `set_clock_uncertainty` 手动给定了等价值。

**练习 2**：时钟不确定度对 setup 检查和 hold 检查的影响方向一样吗？

> **答案**：都会让检查变紧。setup 时可用周期被减去 uncertainty，要求更严；hold 检查同样会因为沿抖动而要求更大的保持窗口。两者都更难满足。

---

### 4.4 时序收敛与 Slack：把周期改小会发生什么

#### 4.4.1 概念说明

**时序收敛（timing closure）** 指经过约束、综合、布局布线后，STA 报告所有路径的 setup 和 hold slack 都 ≥ 0。u1-l3 已经给出本设计在 74.25 MHz 下收敛、Setup Slack = +0.658 ns。

这条 +0.658 ns 是一个非常薄的余量。把它代入第 2 节的 setup slack 公式：

\[ \text{Slack}_{\text{setup}} = T - t_{\text{data}} - t_{\text{su}} - t_{\text{uncertainty}} + t_{\text{skew}} \]

注意 slack 与周期 \(T\) 是**线性关系**：周期每缩短 1 ns，所有路径的 setup slack 也近似减少 1 ns（假设 \(t_{\text{data}}\) 等不变）。本设计的关键路径主要在 `sharp_arith` 的定点乘加进位链（u4-l2 讲过的 `sum` 表达式与饱和截断逻辑），这条链在 13.46 ns 里恰好跑完，只多出 0.658 ns。

#### 4.4.2 核心流程

预测「周期改小」的后果：

- 当前：\(T = 13.46\) ns，slack ≈ +0.658 ns。
- 改为：\(T = 10.0\) ns（100 MHz），周期缩短了 \(13.46 - 10.0 = 3.46\) ns。
- 预测新 slack ≈ \(0.658 - 3.46 \approx -2.8\) ns，即出现约 **−2.8 ns 的 setup 违例**。

这是一个粗略估计（实际还取决于布局布线后 \(t_{\text{data}}\) 是否变化），但方向是确定的：**周期一旦短到不足以覆盖最长组合路径，就必然违例。**

#### 4.4.3 源码精读

违例最可能发生在哪个模块？是运算最重的 `sharp_arith`：

[sharp_arith.vhd（参见 u4-l2 讲解）](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp_arith.vhd)

它在单个时钟沿内要完成「5 次乘法 + 累加 + 加 16 舍入 + 饱和截断」的组合逻辑，进位链最长，是全设计的关键路径。把周期压到 10 ns 后，STA 报告里最差的几条路径大概率都落在 `sharp_arith` 内部或它到下一级寄存器之间。

另一类可能违例的是 IO 路径：因为 IO 延迟 `-max 0.1` + 内部布线 + setup，预算也会随周期一起收紧。

#### 4.4.4 代码实践

这是一个**源码阅读 + 预测型实践**（不要求实际改源码，但鼓励在本地 Quartus 验证）。

**目标**：在不真正改设计逻辑的前提下，仅用「收紧时钟」体会时序收敛的边界。

**操作步骤**：

1. 想象把 [sharp.sdc:10](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L10) 的 `-period 13.46ns` 改成 `-period 10.000ns`。
2. 用第 4.4.2 节的公式估算最差路径 slack 会从 +0.658 ns 变成约 −2.8 ns。
3. 回顾 `sharp_arith` 的乘加表达式，确认它是最可能的违例来源。
4. （可选，本地验证）在 Quartus 里改 SDC、重新编译、打开 Timing Analyzer 的 Setup Summary，看最差 slack 与违例路径分布。

**需要观察的现象**：Setup Slack 由正变负；违例路径集中在 `sharp_arith` 相关节点。

**预期结果**：出现 negative setup slack，量级与预测的 −2.8 ns 接近。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：当出现 setup 违例时，从「放宽时序」的角度，改 SDC 的哪一行最直接有效？这会带来什么副作用？

> **答案**：把 `create_clock` 的 `-period` 调大（降频）最直接——周期变长，slack 立刻变正。副作用是像素吞吐下降，不再是 720p 标准时序，视频可能掉帧或分辨率降低。

**练习 2**：如果不允许降频，还有哪些手段可以让设计时序收敛？

> **答案**：从设计侧入手——给 `sharp_arith` 的乘加流水线化（拆成两级寄存器，缩短单级组合深度）；用 DSP Block 实现乘法代替 LUT；优化饱和截断逻辑；或让 Quartus 用更高速度等级的器件。这些是 u6-l3 二次开发会涉及的取舍。

---

## 5. 综合实践

**任务**：把 `sharp.sdc` 的输入时钟周期从 13.46 ns 改成 10.0 ns（100 MHz），重新做一次完整的静态时序分析，定位违例，并解释原因。这个任务把本讲的「时钟约束 → IO 延迟 → 不确定度 → slack」四个概念全部串起来。

**操作步骤**：

1. **备份**当前 `sharp.sdc`（这是工程文件，本讲义只做阅读型实践；若你真的修改它，请先备份，练习结束后还原）。
2. 把 [sharp.sdc:10](https://github.com/automatesolutions/FPGA_FIR-FILTER/blob/3f7aef90f4d34fa62f00ca72c260fc49c3c04a8c/FPGA-FIR-Filter-master/FPGA-Design/sharp.sdc#L10) 的 `-period 13.46ns` 改为 `-period 10.000ns`。其余约束（派生时钟、IO 延迟、`derive_clock_uncertainty`）保持不变——注意派生时钟会自动跟随主时钟周期变化。
3. 在 Quartus 里重新编译（Synthesis → Fitter → Timing 全跑一遍）。
4. 打开 **Timing Analyzer**，查看：
   - **Setup Summary**：最差（worst-case）Setup Slack 的数值与正负。
   - 违例路径列表（negative slack paths）：它们落在哪些模块、哪些节点之间。
5. 展开一条违例路径的细节，观察它的 **Data Arrival Time**、**Clock Uncertainty**、**Required Time**，体会周期缩短是如何把 Arrival 顶过 Required 的。
6. 还原 SDC，重新编译，确认 slack 回到 +0.658 ns 附近。

**需要观察的现象**：

- Setup Slack 由正变负。
- 违例路径大多指向 `sharp_arith` 的乘加/饱和逻辑（u4-l2 讲过的 `sum` 计算与 `if/elsif/else` 限幅）。
- Clock Uncertainty 仍按 `derive_clock_uncertainty` 自动计算并体现在每条路径里。

**预期结果**：

- 最差 Setup Slack 约在 **−2 ~ −3 ns** 量级（按 13.46 ns 处 +0.658 ns 线性外推）。
- hold slack 通常不受周期影响，多半仍为正。

**具体数值待本地验证**——本讲义未实际运行 Quartus，上述数字是基于 u1-l3 给出的 +0.658 ns 余量做的预测。

**解释（为什么违例）**：周期从 13.46 ns 缩到 10.0 ns，可用时间预算少了 3.46 ns，而设计里最长的组合路径（`sharp_arith` 的乘加 + 饱和截断进位链）本身延迟并没有变短。当周期不足以覆盖这条路径的 \(t_{\text{data}} + t_{\text{su}} + t_{\text{uncertainty}}\) 时，slack 跌破 0，时序不再收敛。这正是时序约束「周期」作为时序预算标尺的直接体现。

## 6. 本讲小结

- **SDC 是 STA 的标尺**：`sharp.sdc` 告诉 Quartus 时钟长什么样、外部世界多严苛，没有它就无法判断时序通过与否；它通过 `FIR.qsf` 的 `SDC_FILE` 被纳入编译。
- **两条时钟约束分工明确**：`create_clock` 在 `clk` 上定义 13.46 ns（74.25 MHz、720p）主时钟 `input_clk`；`create_generated_clock` 在 `clk_o`（硬件上是 `clk` 的组合直通）上定义派生时钟 `output_clk`，供输出侧检查。
- **IO 延迟建模外部世界**：`set_input_delay` / `set_output_delay` 用 `-max`（setup）与 `-min`（hold）、`-rise/-fall` 把每个端口的板级延迟建模进 STA；通配符 `reset_n *_in*` 与 `*_out* led*` 把全部端口干净分类。
- **时钟不确定度吃掉余量**：`derive_clock_uncertainty` 自动为每个时钟估算抖动/偏斜，并从可用周期里扣除，使 slack 更贴近真实芯片。
- **周期决定时序预算**：setup slack 与周期近似线性相关；本设计 74.25 MHz 下仅 +0.658 ns 余量，把周期压到 10 ns 会得到约 −2.8 ns 违例，违例集中在 `sharp_arith`。
- **收敛的两条出路**：放宽周期（降频）或从设计侧缩短关键路径（流水线化、用 DSP、优化逻辑）——前者损失吞吐，后者是 u6-l3 二次开发的内容。

## 7. 下一步学习建议

- 下一讲 **u6-l3 架构取舍与二次开发实践** 会从「设计侧」回应本讲提出的违例问题：当你不能降频时，如何通过修改系数、拆分流水线、复用运算单元来让设计在新约束下重新收敛，并用 u5 的自校验测试台验证效果。
- 若想深入 STA 本身，建议在 Quartus 的 Timing Analyzer 里逐条展开 setup/hold 路径，读懂 **Data Arrival / Data Required / Slack** 三段计算，把本讲的公式与真实报告对上号。
- 推荐对照阅读 `sharp.sdc`（25 行，最小可运行 SDC 样板）与 Intel Quartus 时序约束手册中 `create_clock` / `set_input_delay` / `derive_clock_uncertainty` 的官方说明，理解每个可选参数（如 `-waveform`、`-clock_fall`、多周期约束 `set_multicycle_path`）在更复杂设计里的作用。
