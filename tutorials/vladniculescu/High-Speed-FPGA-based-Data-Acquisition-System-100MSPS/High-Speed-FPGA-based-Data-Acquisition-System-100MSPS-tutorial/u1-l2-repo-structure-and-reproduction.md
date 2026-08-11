# 仓库结构与复现方式

> 本讲是入门单元（Unit 1）的第二篇。上一篇（u1-l1）我们已经知道「这个项目是什么」：一个跑在 Nexys 4 DDR（Artix-7）FPGA 上、集示波器／心率／皮电响应于一体的高速数据采集系统。本篇不写一行新代码，只解决两个问题——**打开仓库后我该看哪些文件？** 以及 **我想让它真在板子上跑起来，要按什么顺序操作？**

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 一眼分辨出仓库里哪些是**人类可读的源码**（Verilog / VHDL 文本），哪些是**二进制产物**（比特流、PCB、LabVIEW 工程），并知道它们各自要用什么工具打开。
2. 看懂 readme 里给出的「**三步复现法**」：烧比特流 → 复现 PCB → 打开 GUI，并理解为什么顺序不能乱。
3. 理解这是一个 **Verilog + VHDL 混合语言工程**，知道顶层模块在哪、子模块散落在哪两个文件夹里。
4. 掌握一个重要防坑点：**文件名和它内部的模块名不一定一致**，读工程时要以文件内部的 `module` / `entity` 声明为准。

---

## 2. 前置知识

在开始前，请确认你已经了解上一篇（u1-l1）引入的几个概念。这里只做最简短的回顾：

- **FPGA（现场可编程门阵列）**：一块可以通过代码重新配置内部电路的芯片。本项目的逻辑全写给它。
- **比特流（bitstream，`.bit` 文件）**：把 HDL 代码「编译」后烧进 FPGA 的二进制文件，类比成 FPGA 的「可执行程序」。
- **HDL（硬件描述语言）**：描述数字电路的语言。本项目用了两种——**Verilog**（`.v` 文件）和 **VHDL**（`.vhd` 文件）。两者做的事一样，只是语法和写法不同。
- **Vivado**：Xilinx 官方的 FPGA 开发工具，负责把 HDL 综合、实现并生成比特流。本项目使用 **Vivado 2016.1**。
- **LabVIEW**：图形化编程环境，本项目用它写上位机（PC 端）的显示界面，文件后缀是 `.vi`。
- **Altium Designer**：PCB（印制电路板）设计软件，本项目用 `.PcbDoc` / `.SchDoc` 存放电路板文件。

一句话总结这条链：**HDL 源码 →（Vivado 综合）→ 比特流 →（烧进 FPGA）→ FPGA 通过 PCB 上的模拟前端采集信号 →（USB/串口）→ LabVIEW GUI 显示**。本讲的任务，就是把仓库里这条链上的每一环都「对号入座」。

---

## 3. 本讲源码地图

本讲几乎不深入代码逻辑，主要带你「读目录」和「读说明」。涉及的关键文件只有两个：

| 文件 / 目录 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `readme.md` | 项目自述文件，包含目录结构说明和复现步骤 | 作为目录与复现流程的「官方说明书」 |
| `verilog files/TOP.v` | 顶层 Verilog 模块，把所有子模块连起来 | 确认顶层模块在哪、它声明了哪些对外端口 |
| `verilog files/`、`vhdl files/` | 两个源码文件夹 | 练习清点源码、区分语言 |
| `FPGA bit file/`、`LabView GUI/`、`Electronic boards design/` | 三个产物目录 | 练习区分可读源码与二进制产物 |

> 提示：本仓库**没有** `Makefile`、`package.json` 之类的构建脚本——FPGA 工程的「构建」是在 Vivado 图形工程里完成的，源码目录里只放了从工程里「提取出来」的模块文本文件。这一点和软件项目很不一样，后面会讲。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：先识别目录结构（4.1），再理解混合语言工程的组织方式（4.2），最后走通复现三步法（4.3）。

### 4.1 目录结构识别：可读源码与二进制产物

#### 4.1.1 概念说明

一个硬件项目仓库，通常**同时**包含三类东西：

1. **可读源码**：纯文本，用任何编辑器都能打开、能读懂。本项目里就是 Verilog（`.v`）和 VHDL（`.vhd`）文件。这是我们要花大量时间精读的对象。
2. **二进制产物**：由工具生成的、或需要专用工具才能打开的文件。比如：
   - `.bit` 比特流：综合后给 FPGA 烧录用的二进制。
   - `.vi` LabVIEW 工程、`.PcbDoc`/`.SchDoc` Altium 电路板、`.rar` 压缩包、`.pdf` 文档：都需要各自的专业软件打开，**用文本编辑器打开是乱码**。
3. **说明文档**：`readme.md` 这种纯文本说明，告诉你仓库怎么组织、怎么用。

学会第一眼就把文件分到这三类，是阅读任何硬件工程的第一步——它决定了你「能不能直接读」。

#### 4.1.2 核心流程

阅读一个陌生硬件仓库目录的推荐顺序：

```
1. 先读 readme.md          → 拿到作者自述的目录结构与复现步骤
2. 对照实际目录树           → 验证 readme 的描述和真实结构是否一致
3. 把每个文件/目录分到三类  → 可读源码 / 二进制产物 / 说明文档
4. 锁定顶层源码文件         → 找到工程入口（本项目的 TOP.v）
```

注意第 2 步：**readme 的描述有时和真实目录对不上**（下面 4.1.3 会看到本项目的例子）。这是因为 readme 是人手写的，而目录可能在版本迭代中被改名。所以「以实际目录为准」是一条好习惯。

#### 4.1.3 源码精读

先看 readme 自己是怎么描述目录结构的（这段是作者对仓库的「官方说明」）：

**readme.md 第 22–27 行**——目录结构说明：

[readme.md:22-27](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L22-L27) —— 作者解释每个文件夹/分组里放了什么：`verilog` 和 `vhdl` 放模块源码、`hw` 放比特流、`sw` 放 LabVIEW GUI、`doc` 放文档、`Electronic boards design` 放 Altium PCB 文件。

> 关键发现（待你亲自核对）：readme 里写的标签是 `hw` / `sw` / `doc`，但**实际仓库里的目录名**却是 `FPGA bit file/`、`LabView GUI/`、`XUP Documentation.pdf`（最后一个是文件不是文件夹）。这是一个典型的「readme 与实际目录略有出入」的例子——以**实际目录**为准。这也提醒我们：文档会过期，目录结构才是 ground truth。

根据实际的仓库内容（可用 `git ls-files` 列出全部文件核实），顶层结构整理如下：

| 实际路径 | 类型 | 内容 / 用什么打开 |
| --- | --- | --- |
| `readme.md` | 说明文档（纯文本） | 项目自述，任何编辑器可读 |
| `verilog files/` | **可读源码**（Verilog） | 14 个 `.v` 文件，纯文本可读 |
| `vhdl files/` | **可读源码**（VHDL） | 6 个 `.vhd` 文件，纯文本可读 |
| `FPGA bit file/TOP.bit` | 二进制产物 | 编译好的比特流，用 Vivado 烧进 FPGA |
| `LabView GUI/nexys_serial_XUP.vi` | 二进制产物 | LabVIEW 上位机界面，用 LabVIEW 打开 |
| `Electronic boards design/` | 二进制产物 | 4 块 PCB 的 Altium 文件，用 Altium Designer 打开 |
| `Vivado Project.rar` | 二进制产物 | 压缩的完整 Vivado 工程，解压后用 Vivado 2016.1 打开 |
| `XUP Documentation.pdf` | 二进制产物（文档） | 项目文档，用 PDF 阅读器打开 |

可以看到：**真正能让你用编辑器逐行读的，只有 `readme.md` 加上 `verilog files/`、`vhdl files/` 两个目录**。后面整个手册的源码精读，几乎都在这两个目录里展开。

#### 4.1.4 代码实践

**实践目标**：亲手把仓库顶层文件分成「可读源码 / 二进制产物 / 说明文档」三类，建立直觉。

**操作步骤**：

1. 在仓库根目录列出所有顶层条目（注意文件夹名带空格，命令里要用引号）。
2. 对每一条，尝试判断：用普通文本编辑器打开会是有意义的文字，还是乱码？
3. 把结果填进一张三列表格。

**需要观察的现象**：

- `readme.md`、`verilog files/*.v`、`vhdl files/*.vhd` 用编辑器打开后是结构化的代码或文字。
- `FPGA bit file/TOP.bit`、`*.vi`、`*.PcbDoc`、`*.SchDoc`、`*.rar` 打开后是乱码或无法以文本方式查看。

**预期结果**：你会得到一张和 4.1.3 那张表类似的分类表。**本步骤可在纯文件浏览下完成，无需安装任何工具链。**

> 如果无法本地克隆仓库，标注「待本地验证」即可——你也可以直接在 GitHub 网页上点开每个文件，文本文件会正常渲染，二进制文件则提示无法预览。

#### 4.1.5 小练习与答案

**练习 1**：仓库根目录下的 `Vivado Project.rar` 属于哪一类？要用什么工具打开？
> **答案**：属于「二进制产物」。它是一个压缩包，解压后得到完整的 Vivado 工程，需要用 **Vivado 2016.1** 打开（而不是文本编辑器）。

**练习 2**：为什么我们说 `verilog files/` 和 `vhdl files/` 是「可读源码」，而 `TOP.bit` 不是？
> **答案**：前者是纯文本的硬件描述语言代码，用编辑器即可逐行阅读、修改、版本管理；后者是经过综合/实现后生成的二进制比特流，是给 FPGA 烧录用的「机器码」，无法直接读懂，也几乎无法手工编辑。

---

### 4.2 混合语言工程的组织方式

#### 4.2.1 概念说明

本项目的源码**同时**用了 Verilog 和 VHDL 两种 HDL，所以叫「混合语言（mixed-language）工程」。这在 FPGA 项目里很常见——不同模块可能由不同人、在不同时期、用各自顺手的语言写成，最后在顶层拼到一起。Vivado 完全支持在一个工程里同时综合这两种语言。

为什么要知道这一点？因为它影响你「找模块」的方式：

- 你想看的模块可能在 `verilog files/`，也可能在 `vhdl files/`。
- **文件名和模块名常常对不上**——下文 4.2.3 会给出本仓库里好几个真实例子。所以查找时，不能只信文件名，要进文件里看 `module` / `entity` 声明。

#### 4.2.2 核心流程

在混合语言工程里定位一个模块的流程：

```
1. 根据功能猜它可能是 Verilog 还是 VHDL
   （本项目：DSP/存储/时钟多在 verilog files/，串口/ADC 读数多在 vhdl files/）
2. 在对应目录里找候选文件名
3. 打开文件，读第一处 module（Verilog）或 entity（VHDL）声明，确认真实模块名
4. 到顶层 TOP.v 里搜索该模块名的例化语句，看它怎么被连进系统
```

记住一个原则：**文件名是「书的封面」，`module`/`entity` 名才是「书的内容标题」**。例化（在顶层里把子模块用起来）时，Vivado 认的是模块名，不是文件名。

#### 4.2.3 源码精读

先确认顶层模块。打开 `verilog files/TOP.v`，第一处模块声明就是整个工程的入口：

[verilog files/TOP.v:22-31](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L22-L31) —— 顶层模块 `TOP` 的端口声明。它直接对接板级物理信号：`clk_in`（板载 100MHz 晶振）、`adc_read[9:0]`（ADC 并行数据）、`serial_in`/`serial_out`（串口收发）、`clock_adc_out`（给 ADC 的时钟）、`adj[2:0]`（模拟前端调节）、`leds[9:0]`（调试指示）。**判断一个模块是不是顶层，最直接的依据就是：它的端口是否对应真实的物理引脚/板级信号**——TOP 正是如此。

再来看 TOP 头部那段对整体算法的注释，它解释了信号在 FPGA 内部的流向，也间接说明了为什么需要那么多源码文件：

[verilog files/TOP.v:4-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L4-L18) —— 作者用 8 步文字描述了完整流程：采集一帧存入 ram1 → 送入 FFT → 计算 FFT → 取出结果并在途中做「实部² + 虚部²」存入 ram2 → 开方（耗时 8 个时钟周期）→ 存入 ram3 → 送 PC → LabVIEW 显示 → 等待 PC 触发下一次采集。

接下来是最重要的「防坑」内容：**文件名 vs 模块名**。下表是你清点 `verilog files/`（14 个）和 `vhdl files/`（6 个）所有文件后，对照文件内部声明的真实结果：

**`verilog files/` 下的 14 个文件（全部 Verilog）：**

| 文件名 | 内部 `module` 名 | 是否与文件名一致 | 备注 |
| --- | --- | --- | --- |
| `TOP.v` | `TOP` | ✅ | **顶层模块** |
| `ADC_clock_mux.v` | `ADC_clock_mux` | ✅ | ADC 时钟二选一 |
| `Fourier.v` | `Fourier` | ✅ | FFT 核封装 |
| `MUX.v` | `MUX` | ✅ | 多路选择器 |
| `MUX_converter.v` | `MUX_converter` | ✅ | 编码转换用的 MUX |
| `Square.v` | `Square` | ✅ | 乘法器封装（平方） |
| `Sum.v` | `Sum` | ✅ | 加法器封装（求和） |
| `SRAM3.v` | `SRAM3` | ✅ | 第三块 RAM |
| `decoder.v` | `decoder` | ✅ | 二进制偏置解码 |
| `pll_loop.v` | `pll_loop` | ✅ | PLL 时钟封装 |
| `Radical.v` | `Root_square` | ❌ | **文件叫 Radical，模块叫 Root_square**（开方封装） |
| `SRAM.v` | `SRAM2` | ❌ | **文件叫 SRAM，模块叫 SRAM2** |
| `ram2.v` | `SRAM` | ❌ | **文件叫 ram2，模块叫 SRAM**（与上一行正好「互换」，极易混淆） |
| `Transcodor.v` | `Transcoder` | ❌ | **文件名拼写少了个 e**（Transcodor vs Transcoder） |

**`vhdl files/` 下的 6 个文件（全部 VHDL）：**

| 文件名 | 内部声明 | 类型 | 是否与文件名一致 |
| --- | --- | --- | --- |
| `serial_rx.vhd` | `entity serial_rx` | VHDL 实体（UART 接收） | ✅ |
| `serial_tx.vhd` | `entity serial_tx` | VHDL 实体（UART 字节发送） | ✅ |
| `serialt.vhd` | `entity serialt` | VHDL 实体（发送控制器） | ✅ |
| `custom_adc_ad9215.vhd` | `entity read_adc` | VHDL 实体（ADC 读数） | ❌ **文件叫 custom_adc_ad9215，实体叫 read_adc** |
| `defs.vhd` | VHDL package（包） | VHDL 包（共享类型/常量，模板） | — 是包不是实体 |
| `aux_vlad.vhd` | `package aux` | VHDL 包（ASCII↔向量转换等工具函数） | ❌ **文件叫 aux_vlad，包名是 aux** |

> 三个重点结论：
> 1. **顶层模块是 `verilog files/TOP.v` 里的 `TOP`**——它是唯一一个端口直接对应板级物理信号的模块。
> 2. **多个文件存在「文件名 ≠ 模块名」**，其中 `SRAM.v`(SRAM2) 与 `ram2.v`(SRAM) 这一对最容易看错，务必以内部声明为准。
> 3. VHDL 目录里**不全是实体**：`defs.vhd` 和 `aux_vlad.vhd` 是 **package（包）**，用来放共享的类型、常量和函数，不能被「例化」，只能被 `use` 引用。这一点 Verilog 学习者初次接触 VHDL 时容易忽略。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：亲自清点两个源码目录，给每个文件标语言、找顶层，并验证「文件名 ≠ 模块名」现象。

**操作步骤**：

1. 列出 `verilog files/` 与 `vhdl files/` 下所有文件。
2. 按**扩展名**给每个文件标语言：`.v` → Verilog，`.vhd` → VHDL。
3. 对每个文件，打开后找到**第一处** `module …` 或 `entity … is`（或 `package … is`），记录真实的模块/实体/包名。
4. 指出**顶层模块文件**是哪一个（提示：看哪个模块的端口对应板级物理信号，且没有更外层的封装）。

**需要观察的现象**：

- `.v` 文件里以 `module` 开头；`.vhd` 文件里以 `entity`、`package` 或 `library`/`use` 开头。
- 至少能复现上表里 4 处「文件名 ≠ 模块名」的情形（`Radical`/`Root_square`、`SRAM`/`SRAM2`、`ram2`/`SRAM`、`custom_adc_ad9215`/`read_adc`）。

**预期结果**：得到上面两张表；并明确指出 **`verilog files/TOP.v` 的 `TOP` 是顶层模块**。本实践无需 Vivado，纯文件阅读即可完成；若无法本地打开文件，可在 GitHub 网页上逐个点开核对，或标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：你想在顶层找到「开方」模块的例化，应该搜索 `Radical` 还是 `Root_square`？为什么？
> **答案**：搜索 **`Root_square`**。因为 Verilog 例化认的是模块名，而这个文件内部的模块名是 `Root_square`，`Radical` 只是文件名。搜 `Radical` 在 TOP.v 里大概率搜不到例化语句。

**练习 2**：`vhdl files/defs.vhd` 能像 `serial_rx` 那样被「例化」成电路吗？为什么？
> **答案**：不能。`defs.vhd` 是一个 **VHDL package（包）**，里面放的是共享的类型、常量、函数，它本身不描述一块电路。它通过 VHDL 的 `use` 语句被其它实体引用，而不是被例化（instantiation）。

---

### 4.3 复现三步法：从比特流到 GUI

#### 4.3.1 概念说明

「复现（reproduce）」指的是：拿到这个仓库后，怎么让系统真的在你的硬件上跑起来、在 PC 上看到波形。readme 给出了三步，理解这三步的关键，是明白**整套系统是由多块硬件 + FPGA + PC 软件协同组成的**，缺一不可：

- FPGA 里跑的是数字逻辑（本项目的核心）。
- 但 FPGA 自己采集不到真实世界信号——需要**模拟前端 PCB** 把传感器信号调理、放大后喂给 ADC。
- FPGA 也无法直接被普通 PC 当作显示器用——需要 **LabVIEW GUI** 通过 USB/串口接收并绘图。

所以三步分别对应这三块的「就位」。

#### 4.3.2 核心流程

readme 给出的复现顺序（**顺序很重要**）：

```
Step 1  烧比特流 TOP.bit → Nexys 4 DDR 开发板        （让 FPGA 逻辑就位）
Step 2  复现并连接 PCB 模拟前端板 → Nexys 开发板      （让信号通路就位）
Step 3  打开 LabVIEW GUI → 用线缆把开发板连到 PC      （让上位机显示就位）
```

这三步背后的依赖关系：Step 1 让 FPGA 「有脑子」；Step 2 让 FPGA 「有眼睛」（能采到真实信号）；Step 3 让系统「有脸」（能在屏幕上看到结果）。如果跳过 Step 2，FPGA 仍能跑逻辑，但采到的是悬空噪声；如果跳过 Step 3，数据仍在 FPGA 里出不来。

#### 4.3.3 源码精读

readme 原文给出的三步：

[readme.md:28-34](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/readme.md#L28-L34) —— 「Instructions to build and test project」：Step 1 把 `bitfile` 上传进 Nexys 4 板；Step 2 复现 PCB 并连到 Nexys；Step 3 打开 GUI 并把板子连到 PC。

对应的仓库产物：

- Step 1 用的比特流在 [FPGA bit file/TOP.bit](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/FPGA%20bit%20file/TOP.bit)（注意：它和顶层模块同名都叫 `TOP`，这不是巧合——比特流通常以顶层模块命名）。
- Step 2 用的 PCB 文件在 `Electronic boards design/`，共有 4 块板：`Oscilloscope_board`（示波器前端）、`Heartbeat_measurement_board`（心率前端）、`MCP2200_board`（USB↔串口桥）、`Power Supply_board`（电源）。
- Step 3 用的上位机在 [LabView GUI/nexys_serial_XUP.vi](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/LabView%20GUI/nexys_serial_XUP.vi)。

> 与 4.1 的发现呼应：readme 把这三步用的资源分别称为 `hw`/`sw`/PCB，实际目录是 `FPGA bit file`/`LabView GUI`/`Electronic boards design`。再次说明要以实际目录为准。

#### 4.3.4 代码实践

**实践目标**：把 readme 的三步与仓库里的具体文件/目录一一对应，做一份「复现清单」。

**操作步骤**：

1. 阅读上面引用的 readme L28–34。
2. 为每一步在仓库里找到对应的文件或目录（提示：Step 1 → `FPGA bit file/`，Step 2 → `Electronic boards design/`，Step 3 → `LabView GUI/`）。
3. 思考：如果只有一块 Nexys 4 DDR 开发板、没有 PCB，系统还能完成哪几步？哪一步会失败？

**需要观察的现象**：

- 每一步都能在仓库里精确定位到一个目录/文件。
- Step 1（烧比特流）只需开发板即可完成；Step 2、Step 3 依赖额外的硬件（PCB、USB 线、PC）。

**预期结果**：得到一张三行清单，每行写明「步骤 → 仓库资源 → 所需物理设备」。**完整复现需要 Nexys 4 DDR + 复现的 PCB + 装有 LabVIEW 的 PC，缺一不可；若仅做源码学习，则三步都可跳过，直接读 `verilog files/` 与 `vhdl files/` 即可。**

#### 4.3.5 小练习与答案

**练习 1**：为什么复现必须「先烧比特流、再连 PCB」，而不能反过来？
> **答案**：烧比特流是让 FPGA 具备正确的数字逻辑（「装上大脑」），这一步不依赖外部模拟信号；而 PCB 模拟前端的作用是给 ADC 提供调理过的真实信号。如果 FPGA 还没烧好逻辑就连上 PCB，ADC 采样时钟、控制信号（如 `adc_pwdn`、`clock_adc_out`）都不对，采到的数据没有意义。所以先逻辑、后信号。

**练习 2**：如果你只想读懂这个项目的代码，完全不想买硬件，三步里你必须做哪一步？
> **答案**：**一步都不必做**。源码学习只需打开 `verilog files/` 与 `vhdl files/` 两个目录的文本文件即可。三步复现是「让系统在真实硬件上跑起来」的要求，不是「读懂代码」的前提。

---

## 5. 综合实践

把本讲三个模块串起来，做一份**《仓库导览与复现清单》**小报告（纯文档任务，无需硬件）：

1. **目录分类表**：列出仓库**顶层**全部条目，每条标注「可读源码 / 二进制产物 / 说明文档」，并写出打开它需要的工具。
2. **源码清点表**：完成 4.2.4 的核心实践——列出 `verilog files/`（14 个）与 `vhdl files/`（6 个）全部文件，标注每个属于 Verilog 还是 VHDL，记录内部真实模块/实体/包名，并圈出所有「文件名 ≠ 模块名」的条目。
3. **指认顶层**：明确写出顶层模块文件路径与模块名，并用一句话说明判断依据（端口对应板级物理信号）。
4. **复现清单**：把 readme 三步与仓库资源对应起来，写出每步所需的物理设备；并注明「仅做源码学习时三步皆可跳过」。

完成后，你应该能凭这份报告，向一个完全没接触过该仓库的人，在 5 分钟内讲清楚「东西放在哪、怎么跑起来」。

---

## 6. 本讲小结

- 仓库里**可读源码**只有 `readme.md` + `verilog files/`（14 个 `.v`）+ `vhdl files/`（6 个 `.vhd`）；其余 `FPGA bit file/`、`LabView GUI/`、`Electronic boards design/`、`Vivado Project.rar`、`XUP Documentation.pdf` 都是**二进制产物**，需要专用工具打开。
- 这是一个 **Verilog + VHDL 混合语言工程**，顶层模块是 `verilog files/TOP.v` 里的 `TOP`，它的端口直接对接板级物理信号。
- **文件名常常不等于模块名**：本项目至少有 `Radical.v`(Root_square)、`SRAM.v`(SRAM2)、`ram2.v`(SRAM)、`custom_adc_ad9215.vhd`(read_adc) 等多处不一致，查找例化时**必须以文件内部的 `module`/`entity` 声明为准**。
- readme 的「**三步复现法**」：烧 `TOP.bit` → 复现并连接 PCB → 打开 LabVIEW GUI 连 PC；分别让「FPGA 逻辑 / 信号通路 / 上位机显示」就位。
- readme 里的目录标签（`hw`/`sw`/`doc`）与实际目录名（`FPGA bit file`/`LabView GUI`/`XUP Documentation.pdf`）**略有出入，以实际目录为准**——这是读任何带文档仓库时的通用好习惯。
- 仅做源码学习时，三步复现皆可跳过；后续讲义全部围绕 `verilog files/` 与 `vhdl files/` 展开。

---

## 7. 下一步学习建议

本讲结束后，你已经知道「东西放在哪、怎么跑起来」。下一篇 **u1-l3 系统总体架构与数据流** 将带你进入 `TOP.v` 内部，把这些散落的模块串成一条 **ADC → ram1 → FFT → 平方求和 → ram2 → 开方 → ram3 → UART** 的完整数据流，画出系统级框图。建议你在进入下一篇前：

- 重读 [verilog files/TOP.v:4-18](https://github.com/vladniculescu/High-Speed-FPGA-based-Data-Acquisition-System-100MSPS/blob/72db951d9cf596d9fc44f222d1434cb5d5ed5503/verilog%20files/TOP.v#L4-L18) 那 8 步算法注释，试着用自己的话复述一遍。
- 在 `TOP.v` 里随便搜几个本讲提到的模块名（如 `Root_square`、`read_adc`、`serial_rx`），提前感受一下「顶层如何例化子模块」。
