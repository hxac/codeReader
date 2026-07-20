# EPICS 控制系统集成

## 1. 本讲目标

本讲讲解 `data_rec` 这个 VHDL IP 核如何被接进 **EPICS** 控制系统，让运行在主机（通常是 VME 工控机）上的 EPICS IOC（Input Output Controller）能够：配置录制参数、Arm 记录器、监听「录制完成」中断、回读每通道波形数据、并自动 re-arm 进入下一轮。

学完后你应当能够：

- 说清 `epics/` 目录下两个 Python 生成器各自生成什么、依据什么参数。
- 读懂 `CONTROL.tpl` 模板里每一条 EPICS record（`bo`/`bi`/`ao`/`ai`/`mbbo`/`mbbi`/`aai`/`calcout`/`seq`/`fanout`）对应硬件的哪个寄存器或哪段存储。
- 把模板里出现的 `:0x04`、`:0x28`、`:0x30` 这类偏移量，逐条对回到 [`hdl/data_rec_register_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) 中的地址常量。
- 描述「Done 中断 → 读状态 → 回读数据 → 自动 re-arm」这条 EPICS 侧状态链是怎么用一串 record 串起来的。

本讲是 u6（验证、集成与二次开发）单元的第三篇，承接 u2-l2（寄存器地图）与 u1-l4（IP 打包）。它的视角不再是「RTL 内部怎么跑」，而是「软件（EPICS）从外面怎么用 AXI 总线驱动这块 IP」。

## 2. 前置知识

阅读本讲前，你需要先建立以下几个概念（相关讲义已在依赖里讲过，这里只做一句话回顾）：

- **EPICS**：高能物理与大型科学装置普遍使用的分布式控制系统。其核心数据实体叫 **record**（记录），每条 record 有一个名字（PV，Process Variable）和一组 `field`。常见 record 类型：
  - `bo`/`bi`：binary output / input（一位输出/输入，带 `ZNAM`/`ONAM` 文字标签）。
  - `ao`/`ai`：analog output / input（数值输出/输入）。
  - `mbbo`/`mbbi`：multi-bit output / input（多值枚举，最多 16 个状态 `ZR..FF`）。
  - `aai`：analog array input（一次读一整个数组，用于回读波形）。
  - `calcout`/`calc`：表达式计算，`CALC` 字段里写类 C 表达式。
  - `seq`：按顺序把若干 `DO<n>` 值通过 `LNK<n>` 推给下游 record。
  - `fanout`/`dfanout`：把一次处理扇出到多条链接。
  - record 之间通过 `FLNK`（forward link）与 `PP`（process passive，顺带触发下游）链接成链。
  - `SCAN` 字段决定 record 何时被处理（`.2 second` 周期、`I/O Intr` 中断驱动、`Passive` 被动等别人触发、`PINI` 启动时先处理一次）。
- **db / template**：EPICS 的 record 定义写在 `.db` 或 `.template` 文件里（语法就是本讲会大量看到的 `record(type, "name") { field(...) ... }`）。`.template` 与 `.db` 在此项目里是同义的「模板文件」。
- **regDev**：一个 EPICS 设备支持（device support），让 record 直接读写一块寄存器/内存映射地址。在 PSI 的 VME 体系里，硬件寄存器经 VME 总线映射到一段地址，regDev 把 PV 绑定到该地址。字段 `DTYP, "regDev"` 表示这条 record 走 regDev；`INP`/`OUT` 写成 `@<base>:<offset> T=<type>` 描述地址。
- **data_rec 的寄存器地图（u2-l2）**：13 个 32 位寄存器位于 `0x0000`–`0x0030`，存储区从 `0x0080` 开始。这是本讲反复核对的那张「真相表」。
- **双时钟域与自动确认（u5-l1/u5-l2/u3-l2）**：`Done_Irq` 中断在 AXI 时钟域；读状态寄存器且处于 Done 态会**自动产生 Ack** 回到 Idle。

> 不熟悉 EPICS 的读者，只需记住一句话：**record 是 EPICS 里的一个「带名字、带类型、可被定时或事件触发处理、能读写硬件」的对象**。本讲会出现的所有 record 类型都遵循这个统一模型。

## 3. 本讲源码地图

本讲涉及的文件全部在 [`epics/`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics) 目录下，外加一个 RTL 寄存器包用于核对地址：

| 文件 | 作用 |
| --- | --- |
| [epics/GenerateDataRecTemplates.py](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py) | **db 模板生成器**。读入通道数与外部触发数，把 `CONTROL.tpl` 里的占位符替换成实际的 record，输出 `<name>.template`。 |
| [epics/TemplateInput/CONTROL.tpl](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl) | **db 模板原型**。包含绝大多数 record 的写法，留下若干 `<PLACEHOLDER>` 让生成器按通道/触发数填充。 |
| [epics/GenerateDataRecPanel.py](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecPanel.py) | **控制面板生成器**。根据通道名列表生成一个 Qt `.ui` 文件（CSI 的控制界面）。 |
| [epics/PanelInput/](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/PanelInput) | 面板模板的零件库：`Panel.tpl`（骨架）+ 三个 item 片段（通道标签、自触发使能按钮、波形通道）。 |
| [epics/test.bat](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/test.bat) | 一键调用两个生成器的示例脚本，本讲的代码实践就跑它。 |
| [epics/README.txt](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/README.txt) | 一句话说明：面板只是示例，需按应用改。 |
| [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) | 寄存器地址常量与 `MemAddr` 函数，本讲的「地址真相源」。 |

整张图的依赖关系是：

```
GenerateDataRecTemplates.py ──读──> TemplateInput/CONTROL.tpl ──替换占位符──> <name>.template (db)
GenerateDataRecPanel.py     ──读──> PanelInput/*.tpl          ──替换占位符──> <name>.ui (面板)
                                         │
                                         └── PV 名字最终指向 <name>.template 里的 record
<name>.template 里的每条 regDev record ──地址──> data_rec_register_pkg 的 Reg_*_Addr_c
```

## 4. 核心概念与源码讲解

### 4.1 db 模板生成器：GenerateDataRecTemplates.py

#### 4.1.1 概念说明

`data_rec` 是一个**参数化** IP：通道数 `NumOfInputs_g`（1–8）和外部触发数 `TrigInputs_g`（0–8）都是 generic。但 EPICS 的 `.template` 是**静态文本**——它不能在运行时「按通道数循环生成 N 条 record」。如果手写模板，通道一变就得人肉增删几十条 record，极易出错。

解决思路是**代码生成**：写一个 Python 脚本，吃两个数字（通道数、外部触发数），把一个带占位符的模板原型 `CONTROL.tpl` 展开成一份确定的 `.template`。这就是 `GenerateDataRecTemplates.py` 的全部职责——它不懂 EPICS 协议，也不读硬件，它只做**字符串模板替换**。

> 类比：这就像 C 语言的预处理器，或者网页模板引擎（Jinja）。`CONTROL.tpl` 是「含 `<%= ... %>` 占位符的母版」，Python 脚本是「渲染器」。

#### 4.1.2 核心流程

脚本流程非常线性，五步走：

1. **解析命令行参数**：`-channels`（通道数）、`-exttrigcnt`（外部触发数）、`-outpath`、`-outname`。
2. **读入模板原型** `TemplateInput/CONTROL.tpl` 到一个字符串 `content`。
3. **按通道数/触发数，生成若干段 record 文本**（自触发使能按钮、外部触发多选位、自触发掩码计算、每通道数据 record）。
4. **把 `content` 里的占位符逐个替换**成上一步生成的文本。
5. **写出** `<outpath>/<outname>.template`。

它要替换的占位符一共 5 个（都在 `CONTROL.tpl` 里，4.3 节会逐一对照）：

| 占位符 | 由什么展开 | 决定因素 |
| --- | --- | --- |
| `<SELFTRIG-ENA>` | 每通道一个 `bo` record（自触发通道使能按钮） | 通道数 |
| `<EXT-TRIG-SEL>` | 每路外部触发一对 `VL`/`ST` 字段（mbbo 的枚举位） | 外部触发数 |
| `<SELFTRIG-CALC-IN>` | 自触发掩码 calc 的 `INP` 字段（A、B、C…） | 通道数 |
| `<SELFTRIG-CALC-CALC>` | 自触发掩码的 `CALC` 表达式（`A<<0｜B<<1｜…`） | 通道数 |
| `<DATA-RECS>` + `<READ-DATA>` | 每通道一个 `aai` 数据 record + ACQUIRE 里的回读链接 | 通道数 |

#### 4.1.3 源码精读

参数解析与输出路径拼装，[epics/GenerateDataRecTemplates.py:9-16](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L9-L16)：脚本接受通道数与外部触发数两个整数，输出文件名固定加 `.template` 后缀。

读入模板原型，[epics/GenerateDataRecTemplates.py:22-23](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L22-L23)：把整个 `CONTROL.tpl` 一次性读进字符串 `content`，后续全部用 `content.replace(...)` 改它。

展开「每通道自触发使能按钮」，[epics/GenerateDataRecTemplates.py:26-40](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L26-L40)：对每个通道生成一条 `bo` record，名字 `SELFTRIG-ENA-CH{ch}`，改一条就 `FLNK` 到 `SELFTRIG-CHENA-LO` 触发掩码重算。注意 `VAL="0"`、`PINI="YES"`——上电默认不使能任何通道的自触发，但启动时各跑一次把 0 推下去。

展开「外部触发多选位」，[epics/GenerateDataRecTemplates.py:43-47](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L43-L47)：用 `MBBO_FIELDS = ["ZR","ON","TW","TH","FR","FV","SX","SV","EI","NI","TE","EL","TV","TT","FT","FF"]`（EPICS mbbo 的 16 个枚举槽位名）给每路外部触发分配一个槽，值写成 `2**trig`（第 0 路→1、第 1 路→2、第 2 路→4…）。这正是 u4-l2 讲过的「`EnableExtTrig` 按位使能」——选中第 N 路就向 `0x30` 写 `2^N`。

展开「自触发掩码计算」，[epics/GenerateDataRecTemplates.py:50-59](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L50-L59)：把每个通道映射成一个字母（A、B、C…），构造 `CALC = (A<<0)|(B<<1)|(C<<2)|...`。即「通道 0 的按钮值放第 0 位、通道 1 放第 1 位…」，合成一个 8 位掩码写入 `SelfTrigChEna` 字段（u4-l4）。

展开「每通道数据 record 与回读链接」，[epics/GenerateDataRecTemplates.py:62-81](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L62-L81)：每个通道生成一条 `aai` record（`DATA-CH{ch}`），并给 `ACQUIRE` 这个 seq record 加一条 `LNK{ch+1}` 指向它。这块地址映射是 4.4 节的重点。

#### 4.1.4 代码实践

**目标**：验证「通道数」如何决定生成出的 record 数量。

**步骤**：

1. 进 `epics/` 目录，分别用 `-channels 2` 和 `-channels 4` 跑两次生成器（先只生成模板，不生成面板）：

   ```bash
   cd epics
   python3 GenerateDataRecTemplates.py -outname test_ch2 -outpath . -channels 2 -exttrigcnt 4
   python3 GenerateDataRecTemplates.py -outname test_ch4 -outpath . -channels 4 -exttrigcnt 4
   ```

2. 在两个输出文件里数 `DATA-CH` 出现的次数，与 `SELFTRIG-ENA-CH` 出现的次数。

**观察**：`test_ch2.template` 里应有 2 条 `DATA-CH` + 2 条 `SELFTRIG-ENA-CH`；`test_ch4.template` 里各 4 条。同时 `ACQUIRE` 这个 seq record 的 `LNK1..LNKn` 条数也随通道数变化。

**预期结果**：生成器纯按 `-channels` 循环，record 数量 = 通道数，与硬件 generic `NumOfInputs_g` 必须一致（否则 EPICS 会去读不存在的通道地址）。

> 说明：本环境不一定装了 Python3，若命令无法运行，可改为「源码阅读型实践」——直接读 [epics/GenerateDataRecTemplates.py:62-81](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L62-L81) 的 `for ch in range(args.channels)` 循环，回答「通道数如何影响 `DATA-RECS` 与 `READ-DATA` 两段文本的行数」，结果一致。

#### 4.1.5 小练习与答案

**练习 1**：若外部触发数 `-exttrigcnt 3`，`EXTTRIG-SEL` 这个 mbbo record 会有几个有效枚举槽？值分别是多少？

**答案**：3 个，分别是 `ZRVL=1`（2⁰，Trigger 0）、`ONVL=2`（2¹，Trigger 1）、`TWVL=4`（2²，Trigger 2）。第 4 个槽（`TH`）不会被脚本写入，保持 EPICS 默认（通常为 0）。这与 [hdl/data_rec_register_pkg.vhd:57](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L57) 的 `Reg_EnableExtTrig_Addr_c` 按位使能语义一致。

**练习 2**：为什么 `MBBO_FIELDS` 刚好是 16 个？超过 16 路外部触发会怎样？

**答案**：EPICS 的 `mbbo`（multi-bit binary output）record 最多只支持 16 个枚举状态（`ZR..FF`）。而 `TrigInputs_g` 上限是 8，所以 16 槽绰绰有余；若真有 >16 路需求，mbbo 这种「枚举选择」UI 就不合适了，应改用 `ao` 直接写数值——但本项目不会遇到（硬件上限 8）。

---

### 4.2 控制面板生成器：GenerateDataRecPanel.py

#### 4.2.1 概念说明

光有 `.template`（PV 定义）还不够——操作员需要一个**图形界面**去点按钮、看波形。PSI 的控制界面基于 Qt，界面文件是 `.ui`（XML 格式），由 Control System Studio (CSS) / Phoebus 渲染。CSI 提供了一批「CA 控件」（Channel Access 控件，如 `caToggleButton`、`caCartesianPlot`），每个控件绑定一个 EPICS PV。

和模板一样，`.ui` 也是静态的：通道数一变，面板上的波形通道数、自触发按钮数、图例标签都得跟着变。所以同样用一个 Python 生成器 `GenerateDataRecPanel.py` 来按通道名展开。

> 注意它和模板生成器的**输入差异**：模板生成器吃「通道**数**」（整数），面板生成器吃「通道**名字列表**」（字符串数组）——因为面板上每个波形要有可读的名字标签，而 db record 只需要编号 `CH0/CH1`。

#### 4.2.2 核心流程

1. 解析参数：`-channels`（通道名列表，空格分隔）、`-exttrigcnt`、`-outpath`、`-outname`。
2. 读入面板骨架 `PanelInput/Panel.tpl`。
3. 展开三处占位符：
   - `<SELFTRIG-ENA>`：每通道一个自触发使能按钮（`caToggleButton`）。
   - `<PLOT-LABELS>`：每通道一个图例标签（`caLabel`），带颜色。
   - `<WAVE-CHANNELS>`：波形绘图控件的通道定义，**固定展开 6 路**（绘图控件本身支持 6 路），有数据的通道填 PV 名、没数据的留空。
4. 写出 `<outpath>/<outname>.ui`。

通道颜色由一个固定调色板 `CHANNEL_RGB` 给出（红/绿/蓝/品红/黄/暗黄），保证不同通道波形颜色区分。脚本还做了一个硬约束检查：通道数 > 6 直接报错，因为绘图控件 `caCartesianPlot` 最多画 6 条曲线。

#### 4.2.3 源码精读

参数与颜色调色板，[epics/GenerateDataRecPanel.py:10-26](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecPanel.py#L10-L26)：注意 `-channels` 是 `nargs="+"`（收集成字符串列表），`CH_COUNT = len(args.channels)`。

6 通道上限保护，[epics/GenerateDataRecPanel.py:29-31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecPanel.py#L29-L31)：通道数超 6 直接 `raise Exception`。这是个常被忽略的细节——硬件最多 8 通道，但**面板最多 6 通道**，二者不等价。

波形通道固定展开 6 路，[epics/GenerateDataRecPanel.py:62-77](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecPanel.py#L62-L77)：`for i in range(6)` 恒循环 6 次，`i < CH_COUNT` 时填 `$(DEV):$(SYS)-REC-DATA-CH{i}` 的 PV 名，否则填 `;`（空数据，绘图控件不画这条）。这说明绘图控件是按「槽位 1–6」配置的，缺通道留空槽即可。

自触发按钮片段，[epics/PanelInput/selftrig_ena_item.tpl:29-34](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/PanelInput/selftrig_ena_item.tpl#L29-L34)：按钮文字 `Enable Selftrig <CHANNEL>`，绑定的 PV 是 `$(DEV)$(SYS)REC-SELFTRIG-ENA-<CHANNEL>`——这正是 4.1 节生成器写进 `.template` 的那条 `bo` record 的名字。**面板控件 ↔ db record ↔ 硬件寄存器**三者的名字在此闭环。

#### 4.2.4 代码实践

**目标**：理解「面板控件如何引用 db record」。

**步骤**：

1. 读 [epics/test.bat](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/test.bat) 第 2 行，注意面板生成器传了 4 个**带名字**的通道：`FirstChannel SecondChannel ThirdChannel FourthHasDifferentName`。
2. 在 [epics/PanelInput/ch_label_item.tpl](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/PanelInput/ch_label_item.tpl) 里找到 `<CHNAME>` 出现的位置，说明它会被替换成什么。

**观察**：通道名只出现在**面板**（图例文字 `CH3 - FirstChannel`），不出现在 `.template` 里——db record 永远是匿名的 `CH0/CH1/...` 编号。

**预期结果**：「人类可读的名字」属于 UI 层，「机器用的编号」属于 db/硬件层，两者解耦。换通道名只需重跑面板生成器，不必动 db。

#### 4.2.5 小练习与答案

**练习 1**：硬件 `NumOfInputs_g=8`，但面板生成器拒绝 >6 通道。如果你真有 8 通道要画，怎么办？

**答案**：脚本 [epics/GenerateDataRecPanel.py:29-31](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecPanel.py#L29-L31) 的报错信息已给了答案：「generate a panel for less channels and modify it manually」——先按 ≤6 通道生成一份 `.ui`，再手工复制波形控件补到 8 条，或拆成两个面板。

**练习 2**：`CHANNEL_RGB` 列表只有 6 种颜色，第 7、8 通道会怎样？

**答案**：脚本 `range(6)` 循环用 `CHANNEL_RGB[i]`，若强行扩到 7 路会越界报错。这也是「6 通道上限」的另一处体现：颜色表与循环范围都写死成 6。

---

### 4.3 CONTROL.tpl：占位符与 db 记录结构

#### 4.3.1 概念说明

`CONTROL.tpl` 是整个 EPICS 集成的**心脏**——一份约 340 行的 db 模板，定义了 `data_rec` 在 EPICS 侧需要的全部 record。它由「机器填的占位符」+「人写死的固定 record」两部分组成。固定 record 描述了那些**与通道数无关**的逻辑：状态读取、Arm、触发源选择、最小录制间隔、软件触发、自触发阈值、触发计数、以及最关键的——一条「监听 Done → 回读数据 → 自动 re-arm」的状态链。

模板顶部用注释声明了 5 个**宏变量**（EPICS 在加载 db 时替换），它们把「逻辑」与「部署地址」解耦：

```
# $(DEV):                         Device name
# $(SYS):                         System name
# $(VME_ADDR_ADC_REC_REG_WRD):    Base address of the recorder (registers)
# $(VME_ADDR_ADC_REC_MEM_WRD):    Base address of the recorder (memory)
# $(DEPTH):                       Recorder memory depth (samples)
```

参见 [epics/TemplateInput/CONTROL.tpl:4-8](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L4-L8)。前两个是命名空间（同一台机器上多块 `data_rec` 靠 `DEV`/`SYS` 区分），后三个是地址与深度——同一份模板可以被不同 VME 地址、不同深度的多块 IP 复用。

#### 4.3.2 核心流程

模板里的 record 按功能分成 7 块，按「初始化 → 运行 → 回读」的时序排列：

```
┌─ Initialization (上电一次性配置) ───────────────┐
│  REC-INIT → INIT-REC / INIT-SELFTRIG / INIT-TRIG│
└─────────────────────────────────────────────────┘
                      │ (运行中)
┌─ Status (周期+中断双驱动) ──────────────────────┐
│  STATUS-SCAN(.2s) ─┐                           │
│  Done_Irq(I/O Intr)┴─► STATUS(mbbi,读0x00)      │
│                          │ FLNK                 │
│                       CHECK(A==4?) ──Done──► READ0
└─────────────────────────────────────────────────┘
                      │
┌─ Readout (Done 后的连锁) ───────────────────────┐
│  READ0 → READ1 → READ2                          │
│  READ1 → ACQUIRE(seq,读所有DATA-CH) → POSTREAD  │
│  POSTREAD(seq) → ARM.PROC   ← 自动 re-arm       │
└─────────────────────────────────────────────────┘
                      │
┌─ Configuration (操作员随时改) ──────────────────┐
│  ARM, TRIGSRC, EXTTRIG-SEL, MINTRIGPERIOD,       │
│  TOTSPLS, PRETRIG, SWTRIG, SELFTRIG-*            │
└─────────────────────────────────────────────────┘
```

**初始化链**用一个 `seq` record 把一组默认值顺序写入：最小录制间隔 200、触发源 0（Stopped）、外部触发全使能 `0xFFFFFFFF`、软件触发置 1、Arm 置 0（**不自动启动**，等操作员手动 Arm）。见 [epics/TemplateInput/CONTROL.tpl:31-42](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L31-L42)。

**状态读取链**是本模板最精巧的部分：`STATUS` 这个 mbbi 同时挂了两条触发——周期 0.2 秒的轮询（`STATUS-SCAN`）和硬件中断（`SCAN = "I/O Intr"`，由 `Done_Irq` 经 regDev 触发）。一旦读到状态值 = 4（Done），`CHECK` 这个 calcout 立刻触发 `READ0`，开启回读与自动 re-arm。

#### 4.3.3 源码精读

宏定义注释，[epics/TemplateInput/CONTROL.tpl:4-8](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L4-L8)：5 个宏，其中 `REG_WRD`（寄存器基址）与 `MEM_WRD`（存储基址）是部署相关、`DEPTH` 是硬件 generic `MemoryDepth_g` 的镜像。

初始化 seq，[epics/TemplateInput/CONTROL.tpl:31-42](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L31-L42)：`DO3="0xFFFFFFFF"` 配 `LNK3` 指向 `EXTTRIG-SEL`，即上电默认**使能所有外部触发输入**——这与 u5-l1 讲过的 `RegRstVal_c`（`EnableExtTrig` 复位默认全 1）在 EPICS 侧再次确认，属 fail-safe 设计。

状态读取与 Done 判定，[epics/TemplateInput/CONTROL.tpl:59-88](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L59-L88)：
- `STATUS` 的 `INP @$(...REG_WRD):0x00 T=uint32` 读状态寄存器，`ZRST..FRST` 把数值 0–4 映射成 `Idle / Pre-Trigger / Waiting for Trigger / Post-Trigger / Done` 文字——这 5 个值与 [hdl/data_rec_register_pkg.vhd:23-27](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L23-L27) 的 `Reg_Stat_StateIdle_c..StateDone_c` 完全一致。
- `CHECK` 的 `CALC = "A==4? 1:0"`、`OOPT = "When Non-zero"` 表示「只有状态 = 4（Done）时才把 `OUT`（`READ0.PROC`）打一拍」。`SCAN="I/O Intr"` 让 `STATUS` 在 `Done_Irq` 中断到来时被立刻处理。

回读与自动 re-arm 链，[epics/TemplateInput/CONTROL.tpl:90-104](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L90-L104) 与 [epics/TemplateInput/CONTROL.tpl:318-329](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L318-L329)：`READ0 → READ1 → ACQUIRE → POSTREAD`，`POSTREAD` 的 `DO1="1"` + `LNK1="...ARM.PROC"` 即「回读完成后自动写 ARM=1 重新启动录制」。结合 u3-l2/u5-l1 的「读状态寄存器自动产生 Ack」，整条闭环是：

> 中断到 → 读 STATUS（硬件自动 Ack 回 Idle）→ 读计数器 → 回读所有通道波形 → 写 ARM=1（硬件 Idle→PreTrig 开始下一轮）。

占位符在模板里的留白，[epics/TemplateInput/CONTROL.tpl:233](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L233)、[epics/TemplateInput/CONTROL.tpl:319](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L319)、[epics/TemplateInput/CONTROL.tpl:334](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L334)：`<SELFTRIG-ENA>`、`<READ-DATA>`、`<DATA-RECS>` 都孤零零占一行——这正是 4.1 节生成器要替换的位置。生成后的 `.template` 里这些行会被展开成多条 record。

#### 4.3.4 代码实践

**目标**：把「Done → 回读 → re-arm」这条链用笔走一遍。

**步骤**：

1. 打开 [epics/TemplateInput/CONTROL.tpl](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl)，从第 59 行 `STATUS-SCAN` 开始，沿 `FLNK`/`PP` 一条条往下追，直到第 329 行 `POSTREAD`。
2. 回答：从 `Done_Irq` 触发到 `ARM.PROC` 被执行，中间依次经过哪些 record？数据是在哪一步被读出来的？

**观察**：链路为 `STATUS → CHECK → READ0 → READ1(→ACQUIRE) → READ2`，其中 `ACQUIRE` 这个 seq record 通过 `LNK1..LNKn`（即占位符 `<READ-DATA>`）触发所有 `DATA-CHx` 的 `aai` 回读，最后 `ACQUIRE → POSTREAD → ARM.PROC`。

**预期结果**：你会看到「读状态」「读计数」「读波形」「重新 Arm」是严格分阶段串行的，而不是并发——这避免了在硬件还在 Done 态时就去写 ARM 造成状态混乱。

#### 4.3.5 小练习与答案

**练习 1**：`STATUS` record 同时有 `STATUS-SCAN`（0.2 秒周期）和 `SCAN="I/O Intr"` 两条触发路径，为什么要冗余？

**答案**：`I/O Intr` 依赖 regDev 能把硬件中断（`Done_Irq`）送到 EPICS；一旦中断路径出问题（驱动未配好、中断丢失），0.2 秒的轮询作为**兜底**，保证最迟 0.2 秒内仍能发现 Done 并启动回读。这是控制软件常见的「事件驱动 + 周期轮询」双保险。

**练习 2**：`POSTREAD` 写 `ARM=1` 重新 Arm，但此时硬件状态是什么？为什么不会冲突？

**答案**：回读发生时硬件处于 Done 态（因为整条链是 `CHECK` 判定 `A==4` 才启动的）。读 STATUS 那一步已经触发了硬件的**自动 Ack**（u5-l1：Done 态读状态寄存器即产生 `AckDone`），所以到 `POSTREAD` 时硬件已回到 Idle，此时写 ARM=1 合法地开启新一轮 Idle→PreTrig。若硬件仍在 Done，写 Arm 会被状态机忽略（参见 u3-l2 的迁移条件）。

---

### 4.4 regDev 地址映射：模板 ↔ 寄存器包对齐

#### 4.4.1 概念说明

前面三节讲了「生成器怎么展开」「模板里有哪些 record」。本节回答最关键的问题：**模板里每条 regDev record 的地址，和 RTL 里的寄存器到底对不对得上？**

这是 EPICS 集成的「最后一公里」：一条 record 写 `OUT @$(REG_WRD):0x28`，意味着「向寄存器基址 + 0x28 字节处写一个 32 位字」。而 `0x28` 必须正好是 RTL 里 `Reg_TrigEna_Addr_c` 的值——否则软件「设置触发源」会写错地方，硬件完全不响应。

核对方法很简单：把模板里所有 `@...:0xNN` 偏移量列出来，逐条与 [`hdl/data_rec_register_pkg.vhd`](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) 的 `Reg_*_Addr_c` 常量比 对。这能验证「软件视角」与「硬件视角」是否同一张地图。

> regDev 小贴士：`INP/OUT` 写成 `@<base>:<offset> T=<type>`，其中 `<offset>` 是字节偏移（与 RTL 的字节地址常量同量纲），`T=` 是数据类型（`uint32`/`int32`），决定符号处理与位宽。部分 record 还带 `U=2000`，这是 regDev 设备支持的一个参数（含义以 regDev 手册为准，**待本地验证**），本项目所有「写寄存器」类输出都带它，推测与写后保持/更新节流相关。

#### 4.4.2 核心流程

把模板里的关键 record 列成一张表，与寄存器包逐行对照：

| EPICS record | 类型 | 模板里的偏移 | RTL 常量 | RTL 值 | 读写 | 字段/说明 |
| --- | --- | --- | --- | --- | --- | --- |
| `STATUS` | mbbi | `:0x00` | `Reg_Stat_Addr_c` | `0x0000` | 读 | 状态机 0–4 |
| `ARM` | bo | `:0x04` | `Reg_Cfg_Addr_c` | `0x0004` | 写 | bit0=Arm |
| `PRETRIG` | ao | `:0x08` | `Reg_Pretrig_Addr_c` | `0x0008` | 写 | 前触发样本数 |
| `TOTSPLS` | ao | `:0x0C` | `Reg_Totspl_Addr_c` | `0x000C` | 写 | 总样本数 |
| `SELFTRIG-LO` | ao | `:0x10` | `Reg_SelftrigLo_Addr_c` | `0x0010` | 写 | `T=int32`（有符号） |
| `SELFTRIG-HI` | ao | `:0x14` | `Reg_SelftrigHi_Addr_c` | `0x0014` | 写 | `T=int32` |
| `SELFTRIG-CTRL` | ao | `:0x18` | `Reg_SelftrigCfg_Addr_c` | `0x0018` | 写 | 三字段拼装 |
| `SWTRIG` | bo | `:0x1C` | `Reg_SwTrig_Addr_c` | `0x001C` | 写 | bit0=软件触发 |
| `TRIG-CNT-FW` | ai | `:0x0020` | `Reg_TrigCnt_Addr_c` | `0x0020` | 读 | 触发计数 |
| `STAT-SW-EPICS` | ai | `:0x0024` | `Reg_DoneTime_Addr_c` | `0x0024` | 读 | Done 持续时长 |
| `TRIGSRC` | mbbo | `:0x28` | `Reg_TrigEna_Addr_c` | `0x0028` | 写 | 三类触发源掩码 |
| `MINTRIGPERIOD` | ao | `:0x2C` | `Reg_MinRecPeriod_Addr_c` | `0x002C` | 写 | 最小录制间隔 |
| `EXTTRIG-SEL` | mbbo | `:0x30` | `Reg_EnableExtTrig_Addr_c` | `0x0030` | 写 | 逐路外部触发使能 |
| `DATA-CHx` | aai | `@MEM_WRD:ch*4*DEPTH` | `MemAddr(ch,0,d)` | `0x80 + ch·2^⌈log2 d⌉·4` | 读 | 每通道波形数组 |

13 个寄存器**全部命中**，且读写方向与 u2-l2 的「只写/只读/读写」分类一致（`Cfg/SwTrig` 只写、`Stat/TrigCnt/DoneTime` 只读）。

**位字段的两处关键映射**：

1. **`TRIGSRC`（TrigEna，0x28）**：模板里 `ZRVL=0`(Stopped)、`ONVL=1`(External)、`TWVL=2`(Free-Running)、`THVL=4`(Self-Trigger)。这四个值正是 `2^Idx`：External = `2^Reg_TrigEna_ExtIdx_c` = 2⁰ = 1；Free-Running（软件触发）= `2^Reg_TrigEna_SwIdx_c` = 2¹ = 2；Self-Trigger = `2^Reg_TrigEna_SelfIdx_c` = 2² = 4。参见 [hdl/data_rec_register_pkg.vhd:50-53](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L50-L53)。注意 EPICS 里把「软件触发」叫作 **Free-Running**，呼应 u4-l3 的 sticky pending 自循环语义。

2. **`SELFTRIG-CTRL`（SelfTrigCfg，0x18）**：模板用一条 calcout 把三段拼起来，`CALC = "(A<<8)|(B<<16)|(C&0xFF)"`，其中 A=ONEXIT、B=ONENTER、C=CHENA。对照寄存器包：`Reg_SelftrigCfg_ExitSft_c=8`（OnExit 移 8 位）、`Reg_SelftrigCfg_EnterSft_c=16`（OnEnter 移 16 位）、`Reg_SelftrigCfg_ChEnaSft_c=0`（通道使能占低 8 位）。参见 [hdl/data_rec_register_pkg.vhd:38-41](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L38-L41)。**三个移位量完全对齐**。

**存储区地址的数学**：每通道数据 record 的地址是模板里最绕的一处。生成器写出 `INP @$(MEM_WRD):{}*4*$(DEPTH) T=int32`（`{}` = 通道号），即偏移 = `ch·4·DEPTH`。而 RTL 的 `MemAddr` 函数为：

\[
\text{MemAddr}(ch, spl, d) = 0x80 + (ch \cdot 2^{\lceil\log_2 d\rceil} + spl) \cdot 4
\]

当深度 \(d\) 是**二次幂**时，\(2^{\lceil\log_2 d\rceil} = d\)，取 \(spl=0\)、去掉基址 `0x80` 后，通道 \(ch\) 数据区的字节偏移正是 \(ch \cdot d \cdot 4 = ch \cdot 4 \cdot d\)，与模板的 `ch*4*DEPTH` 完全吻合。

> ⚠️ **重要 caveat**：当 `MemoryDepth_g` **不是二次幂**时，`MemAddr` 的通道间距是向上取整到二次幂的 \(2^{\lceil\log_2 d\rceil}\)（例如深度 30 → 间距 32），但模板写死的仍是 `ch·4·DEPTH`（深度 30 → 间距 30）。两者在非二次幂深度下**不一致**，会导致 EPICS 读到错位的波形。这并非笔误，而是模板**隐含假设深度为二次幂**——这也是本项目反复在非二次幂深度上出 bug（v2.1.1、v2.3.2 修复）的同一类根因。若你的应用用非二次幂深度，EPICS 模板的通道偏移需要手工修正（**待本地验证**：确认 regDev 是否会对 `ch*4*DEPTH` 做向上取整）。

#### 4.4.3 源码精读

寄存器地址常量集中定义，[hdl/data_rec_register_pkg.vhd:22-60](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L22-L60)：这张表就是 4.4.2 那张对照表的右半边来源。注意 `Mem_Addr_c = 16#0080#` 是存储区起点，所有通道数据都在它之后。

`MemAddr` 函数与通道间距，[hdl/data_rec_register_pkg.vhd:80-86](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd#L80-L86)：`ChannelSpacing_c := 2**log2ceil(memdepth)`——这就是「向上取整到二次幂」的通道间距来源，也是上面 caveat 的源头。

模板里 ARM record，[epics/TemplateInput/CONTROL.tpl:106-114](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L106-L114)：`OUT @$(REG_WRD):0x04 T=uint32`、`VAL="1"`、`PINI="YES"`、`ZNAM="Disarm"/ONAM="Arm"`。地址 `0x04` = `Reg_Cfg_Addr_c`，`VAL=1` 写的就是 `Cfg` 寄存器的 bit0（`Reg_Cfg_ArmIdx_c`），上电时先写一次（但 4.3 节的 INIT-TRIG 又把它清回 0，故最终上电不 Arm）。

模板里 TRIGSRC record，[epics/TemplateInput/CONTROL.tpl:119-131](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L119-L131)：`OUT @$(REG_WRD):0x28 T=uint32 U=2000`，四个枚举值 0/1/2/4 如上所述对应三类触发源的掩码位。

模板里自触发控制拼装，[epics/TemplateInput/CONTROL.tpl:249-262](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/TemplateInput/CONTROL.tpl#L249-L262)：`SELFTRIG-CTRL-REG` 的 `CALC="(A<<8)|(B<<16)|(C&0xFF)"` 与 `SELFTRIG-CTRL` 的 `OUT @$(REG_WRD):0x18` 配合，把 OnExit/OnEnter/通道掩码三段压进一个 32 位字写入 `Reg_SelftrigCfg_Addr_c`。

生成器写出的数据 record，[epics/GenerateDataRecTemplates.py:65-74](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/GenerateDataRecTemplates.py#L65-L74)：`aai` record，`DTYP="regDev"`、`INP @$(MEM_WRD):{ch}*4*$(DEPTH) T=int32`、`NELM="$(DEPTH)"`、`FTVL="LONG"`。`NELM` = `DEPTH` = 每通道采样数，`FTVL=LONG` = 32 位有符号（呼应 u5-l3 读出时对窄于 32 位数据做的符号扩展），`T=int32` 让 regDev 按有符号 32 位解读，保证负数波形正确。

#### 4.4.4 代码实践（本讲主实践）

**目标**：亲手把模板里的地址与寄存器包对齐，验证「软件地图 = 硬件地图」。

**步骤**：

1. 在 `epics/` 目录运行 [epics/test.bat](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/test.bat)：

   ```bash
   cd epics
   python3 GenerateDataRecTemplates.py -outname test -outpath . -channels 4 -exttrigcnt 4
   ```

   生成 `test.template`（若无 Python3，改为直接读 `TemplateInput/CONTROL.tpl` 做以下核对，因为占位符不影响寄存器地址）。

2. 在生成的 `test.template`（或 `CONTROL.tpl`）里定位下面 5 条 record，抄下它们的地址偏移：
   - `STATUS` → 找 `INP` 里的 `:0x??`
   - `ARM` → 找 `OUT` 里的 `:0x??`
   - `TRIGSRC` → 找 `OUT` 里的 `:0x??`
   - `SELFTRIG-CTRL` → 找 `OUT` 里的 `:0x??`
   - `DATA-CH0` → 找 `INP` 里的 `@...:??`

3. 打开 [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd)，按下表核对：

   | record | 模板偏移 | 应等于 | 寄存器包常量 | 实际值 |
   | --- | --- | --- | --- | --- |
   | `STATUS` | `0x00` | `Reg_Stat_Addr_c` | `16#0000#` | ✅ |
   | `ARM` | `0x04` | `Reg_Cfg_Addr_c` | `16#0004#` | ✅ |
   | `TRIGSRC` | `0x28` | `Reg_TrigEna_Addr_c` | `16#0028#` | ✅ |
   | `SELFTRIG-CTRL` | `0x18` | `Reg_SelftrigCfg_Addr_c` | `16#0018#` | ✅ |
   | `DATA-CH0` | `0*4*DEPTH` | `MemAddr(0,0,d)−0x80` | `0` | ✅（ch0 偏移恰为 0） |

4. 对 `DATA-CH0` 特别验证：通道 0 的偏移 `0*4*DEPTH = 0`，意味着它从存储基址 `0x80` 正好开始——与 `MemAddr(0,0,d) = 0x80 + 0 = 0x80` 一致。

**观察**：所有 13 个寄存器偏移与寄存器包逐一对齐；存储区通道 0 起点对齐。证明这份 EPICS 模板与当前 HEAD 的 RTL 地址地图**完全同步**。

**预期结果**：核对全部通过。若任何一条对不上，说明 EPICS 模板与 RTL 版本不同步——这正是 u6-l4（二次开发）要强调的「改寄存器地图必须三处（register_pkg、封装解码、EPICS 模板）联动」的原因。

> 待本地验证项：`U=2000` 参数的精确语义（regDev 设备支持相关）；非二次幂 `DEPTH` 下 `ch*4*DEPTH` 偏移是否需要手工修正（参见 4.4.2 的 caveat）。

#### 4.4.5 小练习与答案

**练习 1**：手算 `MemAddr(ch=2, spl=0, memdepth=128)` 的字节地址，并与模板里 `DATA-CH2` 的偏移对照。

**答案**：\( \text{MemAddr}(2,0,128) = 0x80 + (2 \cdot 2^{\lceil\log_2 128\rceil} + 0)\cdot 4 = 0x80 + (2\cdot 128)\cdot 4 = 0x80 + 1024 = 0x80 + 0x400 = 0x480 \)。模板偏移（相对 `0x80`）= `2*4*128 = 1024 = 0x400`，加基址 `0x80` 得 `0x480`。**完全一致**（因 128 是二次幂）。

**练习 2**：若把 `MemoryDepth_g` 改成 30（非二次幂），`DATA-CH1` 的模板偏移与 RTL 实际地址差多少？

**答案**：模板偏移 = `1*4*30 = 120` 字节。RTL：\( 2^{\lceil\log_2 30\rceil} = 32 \)，`MemAddr(1,0,30) − 0x80 = 1·32·4 = 128` 字节。差 `128 − 120 = 8` 字节（= 2 个样本）。所以非二次幂深度下 CH1 起点错位 8 字节，读到的是 CH0 的尾巴。这正是模板的隐含二次幂假设带来的风险。

**练习 3**：`TRIGSRC` 选 "Free-Running" 时写入的值是 2，对应硬件哪一位、哪个触发源？

**答案**：值 2 = `2^Reg_TrigEna_SwIdx_c` = bit1，使能软件触发（`SwTrig`）。结合 u4-l3，软件触发默认 `SWTRIG VAL=1`（sticky），故选 Free-Running 后每次 Done 自动 re-arm 都会立刻被软件触发，形成自循环录制。这就是 "Free-Running" 名字的由来。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「从零部署一块 data_rec 到 EPICS」的纸面推演。

**场景**：你要把一块 `NumOfInputs_g=4`、`TrigInputs_g=4`、`MemoryDepth_g=128` 的 `data_rec` 接入 EPICS，VME 寄存器基址 `0x1000`、存储基址 `0x2000`，设备名 `DEV=DEV1:`、系统名 `SYS=REC-`。

**任务**：

1. **生成 db 与面板**：写出对应的 `GenerateDataRecTemplates.py` 与 `GenerateDataRecPanel.py` 命令行（通道名自拟 4 个）。
2. **展开宏**：生成 `test.template` 后，`STATUS`、`ARM`、`DATA-CH0` 三条 record 在宏替换后的实际 `INP`/`OUT` 字段是什么？（把 `$(REG_WRD)`→`0x1000`、`$(MEM_WRD)`→`0x2000`、`$(DEPTH)`→`128`、`$(DEV)`→`DEV1:`、`$(SYS)`→`REC-` 代入）
3. **追踪一次录制闭环**：假设硬件刚完成一段录制（进入 Done），按 4.3 的链路写出从 `Done_Irq` 到下一次 Arm 之间，EPICS 侧依次处理哪些 record、各读写哪个地址。
4. **地址核对**：用 4.4 的方法核对 `STATUS`/`ARM`/`TRIGSRC`/`SELFTRIG-CTRL`/`DATA-CH0` 五条地址与寄存器包是否一致。

**参考要点**：

- 命令行就是 [epics/test.bat](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/test.bat) 的两条，通道名换成你的 4 个名字。
- 宏替换后：`STATUS` 的 `INP = @0x1000:0x00 T=uint32`；`ARM` 的 `OUT = @0x1000:0x04 T=uint32`；`DATA-CH0` 的 `INP = @0x2000:0*4*128 T=int32`（= `@0x2000:0`，即存储基址起点）。
- 闭环：`Done_Irq` → `STATUS`(`I/O Intr`，读 `0x1000:0x00`=4，硬件自动 Ack) → `CHECK`(A==4✓) → `READ0` → `READ1` → `ACQUIRE`(触发 `DATA-CH0..3` 读 `0x2000`) → `POSTREAD` → `ARM.PROC`(写 `0x1000:0x04`=1，开启下一轮)。
- 地址核对：五条全部与 [hdl/data_rec_register_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/hdl/data_rec_register_pkg.vhd) 一致。

完成此实践，你就在纸面上把「生成 → 部署 → 运行 → 地址核对」全链路走通了一遍。

## 6. 本讲小结

- `epics/` 用**代码生成**解决「通道/触发数可变 vs. db 模板静态」的矛盾：两个 Python 脚本吃数字/名字，把带 `<占位符>` 的 `.tpl` 母版展开成确定的 `.template` 与 `.ui`。
- `GenerateDataRecTemplates.py` 替换 5 个占位符（自触发使能、外部触发枚举、自触发掩码计算、数据 record、回读链接），全部按 `-channels`/`-exttrigcnt` 循环生成。
- `CONTROL.tpl` 是 EPICS 集成心脏，定义了初始化、状态读取、配置、回读四大块 record；最精巧的是「`Done_Irq` 中断（`I/O Intr`）+ 0.2 秒轮询双驱动 → `CHECK` 判 Done → 回读所有通道 → `POSTREAD` 自动 re-arm」这条闭环。
- regDev 把 PV 绑定到硬件地址：`INP/OUT @<base>:<byteoffset> T=<type>`，模板里 13 个寄存器偏移（`0x00`–`0x30`）与 `data_rec_register_pkg.vhd` 的 `Reg_*_Addr_c` **逐一对齐**。
- 位字段也对齐：`TRIGSRC` 的枚举值 1/2/4 = 三类触发源的 `2^Idx`；`SELFTRIG-CTRL` 的 `(A<<8)|(B<<16)|(C&0xFF)` 与寄存器包的 `ExitSft=8/EnterSft=16/ChEnaSft=0` 三段移位一致。
- 存储区每通道 `aai` record 偏移 `ch*4*DEPTH`，在**二次幂深度**下与 `MemAddr` 完全吻合；非二次幂深度下存在错位风险（模板隐含二次幂假设）。
- 面板（`.ui`）最多 6 通道（绘图控件限制），与硬件最多 8 通道不等价；通道「可读名字」只存在于面板层，db/硬件层只用 `CH0/CH1` 编号，两层解耦。

## 7. 下一步学习建议

- 读完本讲，你已经把「软件视角」与「硬件视角」对齐。下一步建议学 **u6-l4 二次开发：扩展通道、触发源与寄存器**——它会告诉你「新增一个寄存器/触发源」时，`register_pkg`、封装层解码、EPICS 模板（本讲的 `CONTROL.tpl` + 生成器）三处必须联动修改的完整清单。
- 若想深入 EPICS 运行机制，可对照本讲的 record 类型去读 EPICS Application Developer's Guide（`bo/bi/ao/ai/mbbo/mbbi/aai/calcout/seq/fanout` 各章），重点理解 `SCAN`、`FLNK`、`PP`、`PINI`、`DTYP` 字段。
- 若关心 regDev 的 `U=2000` 与中断 (`I/O Intr`) 如何接到 `Done_Irq`，建议结合 **u5-l2 跨时钟域**（`Done` 经 `pulse_cc` 跨到 AXI 域产生 `Done_Irq`）一起读，理解「硬件中断 → regDev → EPICS `I/O Intr` scan」的完整中断链。
- 想看 EPICS 模板被实际加载的例子，可回看 [epics/test.bat](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/test.bat) 与 [epics/README.txt](https://github.com/paulscherrerinstitute/vivadoIP_data_rec/blob/f68c9316296346ff77c2c328711309d558b72cc5/epics/README.txt)，自己跑一遍生成器并检视输出文件。
