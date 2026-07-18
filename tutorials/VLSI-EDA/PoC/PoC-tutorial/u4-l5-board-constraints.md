# 板级约束与 FPGA 目标

## 1. 本讲目标

前面几讲我们关注的是「RTL 写得对不对」——综合出来能不能正确推断出 BRAM、`generate` 会不会选对厂商子实体、同步器够不够深。但一个 FPGA 设计要真正落到某块开发板上跑起来，还差最后一公里：**这些顶层端口究竟该连到芯片的哪只管脚上？电平标准是什么？时钟频率是多少？哪些路径根本不可能时序收敛、必须提前告诉工具别去算？**

这些问题都由**约束文件（constraint file）**回答。本讲学完后，你应当：

1. 能说出 PoC 在 `ucf/` 目录下用哪三种约束格式、分别对应哪个厂商工具。
2. 能分清「给某块开发板的约束」和「给某个 PoC 核的约束」这两类文件的组织差异。
3. 读懂 [`ucf/MetaStability.ucf`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf) 的三行，并解释它如何与同步器核里的 `_async` / `_meta` 信号命名约定配合，把亚稳态路径从时序分析中摘除。
4. 理解 `my_config.vhdl` 里的 `MY_BOARD` 常量如何同时驱动「VHDL 层的器件/厂商解析」和「约束层选哪块板子的管脚文件」，从而保证两边始终一致。

---

## 2. 前置知识

本讲是第 4 单元（仿真、综合与目标平台）的一环，建立在三讲之上，会反复用到它们的结论：

- **u1-l3 获取、运行与配置 PoC**：`my_config.vhdl.template` 提供 `MY_BOARD` / `MY_DEVICE` 两个常量，复制去掉 `.template` 后缀后填写。本讲要把这两个常量「接到」约束文件上。
- **u2-l3 配置机制**：`MY_BOARD`（或 `MY_DEVICE`）经 `src/common/config.vhdl` 解析成 `T_DEVICE_INFO` 记录，其中的 `Vendor` 字段被下游核用 `if generate` 消费。本讲会看到 `config.vhdl` 里还有一张 `C_BOARD_INFO_LIST` 表，正是它把板名映射到器件型号。
- **u3-l2 厂商选择与可移植机制** + **u3-l6 时钟域穿越**：同步器 `sync_Bits` 用多级 D-FF 链抑制亚稳态，链上信号刻意命名为 `Data_async` / `Data_meta` / `Data_sync`。本讲的亚稳态约束完全依赖这套命名约定来「认人」。

如果你对「亚稳态为什么需要约束」「`generate` 如何选厂商」还有疑问，建议先回看这两讲。

> 名词速查：
> - **约束文件**：告诉综合/布局布线工具「管脚位置、电平标准、时钟频率、哪些路径别管」的附加文件，不是 VHDL 源码。
> - **亚稳态（metastability）**：触发器在违反建立/保持时间时输出停留在 0/1 之间的不稳定电平。
> - **TIG / false path**：Timing IGnore / 伪路径，让时序分析器跳过某条注定无法收敛的路径。
> - **UCF / XDC / SDC**：三种约束文件格式，分别用于 Xilinx ISE、Xilinx Vivado、Altera Quartus。

---

## 3. 本讲源码地图

本讲涉及的文件都不大，但彼此配合紧密：

| 文件 | 作用 |
| --- | --- |
| [ucf/README.md](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md) | 约束目录的总说明：列三种格式、分「核约束」和「板约束」两类清单。 |
| [ucf/MetaStability.ucf](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf) | 全库通用的亚稳态约束模板，只有 3 行，是本讲的主角之一。 |
| [ucf/KC705/Default.ucf](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/Default.ucf) | 板级约束示例：声明整块板的器件型号。 |
| [ucf/KC705/Clock.SystemClock.ucf](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/Clock.SystemClock.ucf) | 板级约束示例：系统时钟的管脚、电平、频率。 |
| [ucf/misc/sync/sync_Bits_Xilinx.ucf](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.ucf) 与 [sync_Bits_Xilinx.xdc](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.xdc) | 专给 `sync_Bits_Xilinx` 核的约束（UCF 与 Vivado XDC 两套）。 |
| [src/misc/sync/sync_Bits.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl) | 通用同步器：定义 `Data_async` / `Data_meta` 命名约定与 `ASYNC_REG` 属性。 |
| [src/misc/sync/sync_Bits_Xilinx.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl) | Xilinx 专用同步器：触发器实例显式命名为 `FF1_METASTABILITY_FFS`，便于通配匹配。 |
| [src/common/my_config.vhdl.template](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template) | 配置模板：`MY_BOARD` 常量同时被 VHDL 解析层和约束选择层读取。 |
| [src/common/config.vhdl](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl) | `C_BOARD_INFO_LIST` 表：把板名映射到 FPGA 器件型号。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先看约束文件**长什么样、放哪里**（4.1 约束文件组织），再聚焦全库最重要的那份通用约束——**亚稳态约束**（4.2 亚稳态约束），最后把约束层与 VHDL 配置层**用 `MY_BOARD` 串起来**（4.3 板级配置配套）。

### 4.1 约束文件组织

#### 4.1.1 概念说明

VHDL 源码描述的是**逻辑行为**（这个寄存器在时钟上升沿采样这个信号），但它**不描述物理实现**：顶层端口 `clk` 到底落在 Kintex-7 的 `AD12` 引脚还是 `K19` 引脚？它是 LVDS 差分还是 LVCMOS 单端？它对应 200 MHz 还是 50 MHz？这些都由综合/布局布线工具读取的**约束文件**补充。

不同 FPGA 厂商的工具读不同格式的约束：

- Xilinx **ISE**（老工具，≤14.7）读 **UCF**（User Constraint File）。
- Xilinx **Vivado**（新工具）读 **XDC**（Xilinx Design Constraints，本质是 Tcl 脚本）。
- Altera **Quartus II** 读 **SDC**（Synopsys Design Constraints，也是 Tcl 脚本）。

PoC 的策略是「同一份信息，三种格式都提供」，让同一份核或同一块板的约束能在三套工具间无缝切换。这些文件统一收在仓库根目录的 `ucf/` 下。

#### 4.1.2 核心流程

`ucf/` 目录用**两种维度**切分文件，读者要分清：

```
ucf/
├── <BoardName>/          ← 按开发板组织：管脚/电平/时钟约束
│   ├── Default.ucf/.xdc       （声明整板的器件型号 CONFIG PART）
│   ├── Clock.SystemClock.*    （系统时钟管脚+频率）
│   ├── GPIO.LED.* / GPIO.Switch.* / GPIO.Button.*   （LED/拨码/按键管脚）
│   ├── UART.* / Bus.IIC.*     （串口/IIC 总线管脚）
│   └── EthernetPHY.*          （以太网 PHY 管脚）
│
├── misc/  arith/  fifo/  net/   ← 按 PoC 核组织：核级约束（亚稳态、布线）
│   └── sync/sync_Bits_Xilinx.ucf/.xdc
│
├── MetaStability.ucf            ← 全库通用：所有同步器的亚稳态约束
└── Xilinx/                      ← 收尾：禁用某些收发器的 DRC 检查
```

两类文件的**作用对象不同**：

- **板级约束**（`KC705/`、`Atlys/`、`ML505/`、`DE4/` 等）回答「这块板上，某某功能（Clock / GPIO / UART / Ethernet）的管脚在哪、电平是什么」。文件名遵循 `<类别>.<功能>.<格式>` 的约定，例如 `GPIO.LED.ucf`、`Clock.SystemClock.xdc`。同一个类别通常三种格式都齐全，让你在 ISE / Vivado / Quartus 之间切换时不必改顶层端口名。
- **核级约束**（`misc/`、`net/`、`fifo/`、`arith/`）回答「这个 PoC 核内部某些寄存器需要特殊布线或时序豁免」。例如 `misc/sync/sync_Bits_Xilinx.xdc` 把同步链的两级 D-FF 标成 `ASYNC_REG` 并打 `false_path`。注意：**核级约束只针对需要它们的厂商工具**——`sync_Bits_Xilinx.*` 顾名思义只给 Xilinx，因为厂商子实体本身已经由 `generate` 选过了（见 u3-l2）。

> 关键区分：板级约束绑「物理世界」（管脚号），核级约束绑「RTL 内部」（寄存器实例名）。前者每块板一份，后者每个核一份。

#### 4.1.3 源码精读

先看总说明。[`ucf/README.md:3-6`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md#L3-L6) 一句话点明三种格式与三家工具的对应：

```text
 -  User Constraint Files (*.ucf) for Xilinx ISE ≤14.7
 -  Xilinx Design Constraints (*.xdc) for Xilinx Vivado
 -  Synopsis Design Constraints (*.sdc) for Altera Quartus-II
```

紧接着它把目录内容分成两节——[`ucf/README.md:8-18`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md#L8-L18)「Constraints for PoC Entities」列核约束（`sync_Bits_Xilinx.ucf`、`eth_RSLayer_GMII_GMII_KC705.ucf`、`MetaStability.ucf`），[`ucf/README.md:20-41`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/README.md#L20-L41)「Constraints for Evaluation Boards」按 Cyclone/Stratix/Spartan/Artix/Kintex/Virtex/Zynq 系列罗列开发板（`DE0`、`DE4`、`S3SK`、`Atlys`、`KC705`、`ML505`、`XUPV5`、`ML605`、`VC707`、`ZedBoard`、`ZC706`）。这与我们上面画的目录树完全吻合。

板级约束从「整板型号声明」开始。[`ucf/KC705/Default.ucf:16`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/Default.ucf#L16) 用一行 UCF 锁定整块 KC705 的器件、封装、速度等级：

```text
CONFIG PART = XC7K325T-FFG900-2;
```

这正是 `MY_DEVICE` 之类常量会在 VHDL 侧描述的同一颗芯片——约束侧用 `CONFIG PART` 再次声明一次，两边必须一致，否则 Vivado/ISE 会报器件不匹配。

再看时钟约束的「UCF 版」与「XDC 版」如何描述同一只 200 MHz 差分时钟。UCF 版 [`ucf/KC705/Clock.SystemClock.ucf:25-30`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/Clock.SystemClock.ucf#L25-L30)：

```text
NET "KC705_SystemClock_200MHz_p"   LOC = "AD12";
NET "KC705_SystemClock_200MHz_n"   LOC = "AD11";
NET "KC705_SystemClock_200MHz_?"   IOSTANDARD = LVDS;
NET "KC705_SystemClock_200MHz_p"   TNM_NET = "PIN_SystemClock_200MHz";
TIMESPEC "TS_SystemClock" = PERIOD "PIN_SystemClock_200MHz" 200 MHz HIGH 50 %;
```

`LOC` 锁管脚号，`IOSTANDARD` 定电平标准，`TIMESPEC ... PERIOD` 定时钟周期。XDC 版 [`ucf/KC705/Clock.SystemClock.xdc:25-30`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/Clock.SystemClock.xdc#L25-L30) 把同样的信息改写成 Vivado 的 Tcl 语法：

```tcl
set_property PACKAGE_PIN  AD12  [get_ports KC705_SystemClock_200MHz_p]
set_property PACKAGE_PIN  AD11  [get_ports KC705_SystemClock_200MHz_n]
set_property IOSTANDARD   LVDS  [get_ports -regexp {KC705_SystemClock_200MHz_[p|n]}]
create_clock -period 5.000 -name PIN_SystemClock_200MHz [get_ports KC705_SystemClock_200MHz_p]
```

`set_property PACKAGE_PIN` 等价于 UCF 的 `LOC`，`create_clock -period 5.000`（5 ns = 200 MHz）等价于 `TIMESPEC ... PERIOD ... 200 MHz`。注意到端口名都带板名前缀 `KC705_`，这是 PoC 的命名约定，避免多板设计里端口撞名。

Altera 的 SDC 版（以 DE4 为例）走的是 Tcl 语法，且用 `if {$TimingConstraints == 0}` 做条件开关——[`ucf/DE4/Clock.SystemClock.sdc:13-18`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/DE4/Clock.SystemClock.sdc#L13-L18)。SDC 主要管「时序」（频率、伪路径），管脚号在 Quartus 里另由引脚分配表给出，所以 PoC 的 SDC 文件里多见 `create_clock` / `set_false_path`，少见 `set_location_assignment`。

#### 4.1.4 代码实践

**实践目标**：直观感受「三种格式描述同一信息」与「板约束的命名约定」。

**操作步骤**：

1. 打开 [`ucf/KC705/`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705) 目录，找出 `Clock.SystemClock` 的三种格式（`.ucf` / `.xdc`，SDC 在 Altera 板才有，KC705 没有）。
2. 对照 `Clock.SystemClock.ucf` 与 `Clock.SystemClock.xdc`，逐行把 UCF 写法翻译成 XDC 写法（`LOC`→`PACKAGE_PIN`，`IOSTANDARD`→`set_property IOSTANDARD`，`TIMESPEC PERIOD`→`create_clock`）。
3. 打开 [`ucf/KC705/GPIO.LED.xdc:30-31`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705/GPIO.LED.xdc#L30-L31)，观察末尾对 LED 端口打的 `set_false_path`。

**需要观察的现象**：同一个系统时钟，UCF 用 `NET`+`TIMESPEC`，XDC 用 `set_property`+`create_clock`，关键词不同但信息一一对应；LED 这种慢速输出端口被 `set_false_path` 豁免时序。

**预期结果**：你能把任一 `.ucf` 行手工改写成等价的 `.xdc` 行，且理解板约束文件名 `<类别>.<功能>.<格式>` 的含义。

#### 4.1.5 小练习与答案

**练习 1**：`ucf/misc/sync/sync_Bits_Xilinx.ucf` 为什么没有对应的 `.sdc` 版？
**答案**：因为它只约束 Xilinx 专用的 `sync_Bits_Xilinx` 子实体，而这个子实体只在 `VENDOR_XILINX` 下被 `generate` 选中（u3-l2）。Altera 板根本不会编译它，自然不需要 SDC 版。

**练习 2**：板级目录里的 `Default.ucf` 起什么作用？它和 `MY_DEVICE` 常量是什么关系？
**答案**：`Default.ucf` 用 `CONFIG PART = <器件-封装-速度等级>` 声明整块板的目标芯片。它与 `my_config.vhdl` 里的 `MY_DEVICE` 描述同一颗芯片，约束侧与 VHDL 侧必须一致，否则工具报器件不匹配。

---

### 4.2 亚稳态约束

#### 4.2.1 概念说明

这是本讲最核心的一节。回顾 u3-l6：当信号从一个时钟域进入另一个独立时钟域时，捕获它的第一级触发器可能违反建立/保持时间，输出陷入**亚稳态**——电平长时间停在 0 和 1 之间，需要经过一两级 D-FF 才能「决断」回稳定的 0 或 1。这就是 PoC 同步器（`sync_Bits` 等）存在的意义：用 `SYNC_DEPTH` 级 D-FF 串行级联，给亚稳态留出决断时间，让最终输出可靠。

同步器的可靠性（平均无故障时间 MTBF）随决断时间指数增长，可用下式直觉化：

\[
\mathrm{MTBF} \;\propto\; \frac{e^{\,T_{\text{res}}/\tau}}{f_{\text{clk}}\cdot f_{\text{data}}}
\]

其中 \(T_{\text{res}}\) 是给亚稳态的决断时间，\(\tau\) 是器件相关的时间常数。但这里有个时序分析的「哲学冲突」：

- 时序分析器（STA）默认要保证**每条路径**都满足建立/保持时间。
- 而同步器的**输入路径注定无法满足**——它来自异步时钟域，本就是来制造亚稳态的。

如果让 STA 死磕这条路径，它会永远报违例、永远「无法收敛」，而且**白白浪费努力**——我们根本不指望它收敛，我们要的是把这条路径**从分析里摘出去**，转而依赖同步链的物理决断能力。这就需要约束告诉工具：**「这条路径别算（timing ignore），但请把捕获它的那对触发器当异步路径对待、就近摆放。」**

PoC 把这套规则提炼成一份全库通用的 [`ucf/MetaStability.ucf`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf)，并配套一套贯穿所有同步器核的**信号命名约定**，让约束能用通配符「认出」亚稳态路径。

#### 4.2.2 核心流程

整个机制靠「命名约定 + 通配约束」双向咬合：

1. **RTL 侧约定**：每个同步器核把跨域输入网线命名为 `*_async`，把第一级（亚稳态易发）捕获触发器命名为 `*_meta`（或显式把实例命名为 `*METASTABILITY_FFS*`）。
2. **约束侧通配**：`MetaStability.ucf` 用三行通配把这两类对象分别处理。
3. **结果**：异步输入被 `TIG`（跳过时序分析），亚稳态触发器被分组并 `TIG`（从任何 FF 到它都不计路径），同时 RTL 里用 `ASYNC_REG` 属性告诉布局器「这对 FF 是异步链、请贴近摆放、别合并/优化」。

三行约束（UCF 语法）做的事：

```text
NET "*_async"                       TIG;                          # 1) 异步输入网线：跳过时序
INST "*_meta*"                      TNM = "METASTABILITY_FFS";    # 2) 亚稳态 FF：归入计时分组
TIMESPEC "TS_MetaStability" = FROM FFS TO "METASTABILITY_FFS" TIG;  # 3) 从任意 FF 到该分组：跳过时序
```

- 第 1 行：任何名字以 `_async` 结尾的网线（即跨域输入那段连线）一律 `TIG`（Timing IGnore）。
- 第 2 行：任何名字含 `_meta` 的触发器实例归入计时分组 `METASTABILITY_FFS`（TNM = Timing NaMe）。
- 第 3 行：声明一个时间规范（TIMESPEC），规定「从任何 FF 出发、到达 `METASTABILITY_FFS` 分组的所有路径」全部 `TIG`。

> **为什么第 1 行还不够，还要第 2、3 行？** 第 1 行豁免的是「`*_async` 这条网线所在的」那一条直接路径。但大型设计里，到达亚稳态 FF 的路径可能经过缓冲、被工具改名、或来自多个源，单靠网线名抓不全。第 2、3 行换了个更稳的抓手——**按目的触发器（sink）分组**，凡进入这个分组的路径一律豁免，无论它从哪来、中间叫什么名。两层叠加 = 防漏网。

#### 4.2.3 源码精读

**先看约定是怎么在 RTL 里埋下的。** 通用同步器 [`src/misc/sync/sync_Bits.vhdl:19-22`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L19-L22) 的文档头直接写明这套契约：

```vhdl
-- Constraints:
--   General:
--     Please add constraints for meta stability to all '_meta' signals and
--     timing ignore constraints to all '_async' signals.
```

其 `genGeneric`（通用厂商）分支里把约定落实成具体信号名与属性，见 [`src/misc/sync/sync_Bits.vhdl:92-101`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits.vhdl#L92-L101)：

```vhdl
signal Data_async : std_logic;
signal Data_meta  : std_logic := INIT_I(i);
...
-- Mark register DataSync_async's input as asynchronous and ignore timings (TIG)
attribute ASYNC_REG      of Data_meta : signal is "TRUE";
-- Prevent XST from translating two FFs into SRL plus FF
attribute SHREG_EXTRACT  of Data_meta : signal is "NO";
attribute SHREG_EXTRACT  of Data_sync : signal is "NO";
```

注意三个名字：`Data_async`（跨域输入）、`Data_meta`（第一级、亚稳态易发）、`Data_sync`（后续级）。`ASYNC_REG = "TRUE"` 告诉综合器 `Data_meta` 寄存的是异步信号；`SHREG_EXTRACT = "NO"` 防止工具把这两级 FF 合并成移位寄存器（SRL），破坏同步链结构。

**Xilinx 专用版更进一步，把触发器实例显式命名以便通配。** [`src/misc/sync/sync_Bits_Xilinx.vhdl:124-167`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L124-L167)：

```vhdl
signal Data_async : std_logic;
signal Data_meta  : std_logic;
...
attribute ASYNC_REG   of Data_meta : signal is "TRUE";
attribute SHREG_EXTRACT of Data_meta : signal is "NO";
attribute RLOC of Data_meta : signal is "X0Y0";   -- 两个 FF 贴近摆放，最小布线延迟
attribute RLOC of Data_sync : signal is "X0Y0";
...
FF1_METASTABILITY_FFS : FD        -- ← 实例名含 METASTABILITY_FFS
  port map (C => Clock, D => Data_async, Q => Data_meta);
FF2 : FD
  port map (C => Clock, D => Data_meta, Q => Data_sync);
```

这里第一级触发器实例被刻意命名为 `FF1_METASTABILITY_FFS`——名字里直接带 `METASTABILITY_FFS`，使得核级约束 `sync_Bits_Xilinx.ucf` 的通配 `INST "*FF1_METASTABILITY_FFS"` 能精确命中它。`RLOC = "X0Y0"` 是相对位置约束，强制两级 FF 落在同一个 slice，把级间布线延迟压到最小（决断时间 \(T_{\text{res}}\) 越大、MTBF 越高）。

**对照核级约束文件**（UCF 版与 Vivado XDC 版各一份）。UCF 版 [`ucf/misc/sync/sync_Bits_Xilinx.ucf:1-2`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.ucf#L1-L2) 只需把 `FF1` 归组（其余由全库 `MetaStability.ucf` 兜底）：

```text
INST "*FF1_METASTABILITY_FFS"  TNM = "METASTABILITY_FFS";
```

Vivado XDC 版 [`ucf/misc/sync/sync_Bits_Xilinx.xdc:4-6`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.xdc#L4-L6) 用 Tcl 把等价语义写全：

```tcl
set_property ASYNC_REG true [get_cells -regexp {gen\[\d+\]\.Sync/FF2}]
set_property ASYNC_REG true [get_cells -regexp {gen\[\d+\]\.Sync/FF1_METASTABILITY_FFS}]
set_false_path -from [all_clocks] -to [get_pins -regexp {gen\[\d+\]\.Sync/FF1_METASTABILITY_FFS/D}]
```

- 前两行把同步链的两级 FF（`FF1`、`FF2`）都标 `ASYNC_REG`（注意 XDC 里这个属性是直接写在 XDC、而非靠 RTL 属性，双保险）。
- 第三行用 `set_false_path` 从「所有时钟」到「`FF1` 的 D 端」打伪路径，等价于 UCF 的 `NET "*_async" TIG` + `TIMESPEC ... TIG`。
- 头部注释要求把该 XDC 用 `SCOPED_TO_REF = sync_Bits_Xilinx` 绑定到该核的所有实例，并用 `PROCESSING_ORDER` 保证它在时钟定义 XDC **之后**加载（伪路径要先有时钟才能引用）。

> 一句话总结：UCF 靠「网线/实例名通配 + TNM 分组 + TIMESPEC」三件套表达「豁免亚稳态路径」，XDC 靠「`get_cells`/`get_pins` 正则 + `set_false_path` + `ASYNC_REG`」表达同样的事。两者咬合的是同一套 `_async` / `_meta` / `METASTABILITY_FFS` 命名约定。

#### 4.2.4 代码实践

**实践目标**：亲手验证「命名约定 → 通配匹配」这条链，理解三行约束各抓什么对象。

**操作步骤**：

1. 打开 [`src/misc/sync/sync_Bits_Xilinx.vhdl:147-167`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/misc/sync/sync_Bits_Xilinx.vhdl#L147-L167)，找到 `Data_async` 网线与 `FF1_METASTABILITY_FFS` 实例。
2. 打开 [`ucf/MetaStability.ucf`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf)，逐行把通配符与上一步的对象对号入座：
   - `NET "*_async"` 命中哪条网线？（`Data_async`，即 `Input` 到 `FF1` 的 D 端那段）
   - `INST "*_meta*"` / `INST "*FF1_METASTABILITY_FFS"` 命中哪个实例？（第一级捕获 FF）
3. 打开 [`ucf/misc/sync/sync_Bits_Xilinx.xdc:6`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.xdc#L6)，看 `set_false_path` 的 `-to` 是否指向同一个 `FF1` 的 `D` 引脚。

**需要观察的现象**：UCF 的 `NET "*_async" TIG` 与 XDC 的 `set_false_path ... -to FF1.../D` 描述的是**同一条物理路径**——异步输入到第一级 FF 的那条；只是 UCF 抓源端网线名、XDC 抓目的端引脚。

**预期结果**：你能说出「三行 UCF 各豁免了哪类对象」，并解释为何第一级 FF（`_meta`/`FF1`）是豁免的落点、而第二级 FF（`FF2`）需要正常参与时序分析。如果无法本地运行 Vivado/ISE，这是纯源码阅读型实践，标注为「待本地验证」综合效果。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `Data_meta` 重命名为 `Data_first`，会发生什么？
**答案**：`INST "*_meta*"` 不再命中它，亚稳态 FF 不会被归入 `METASTABILITY_FFS` 分组，第 3 行 `TIMESPEC` 失效，时序分析器会重新尝试收敛这条注定失败的异步路径并报违例。这正是 u3-l6 强调的「改名即破约束」。

**练习 2**：为什么 `set_false_path` 只打到 `FF1` 的 D 端，不打到 `FF2`？
**答案**：亚稳态只发生在**捕获异步信号的那一级**（`FF1`）。`FF1`→`FF2` 是同一个同步时钟域内的普通寄存器间路径，应当正常分析时序——它正是给亚稳态「决断」的一拍，必须保证 `FF1` 的输出在 `FF2` 采样前稳定。把它也豁免反而会放任同步链失效。

**练习 3**：`SHREG_EXTRACT = "NO"` 与亚稳态约束的目的是否相同？
**答案**：不同。`SHREG_EXTRACT` 防工具把两级 FF 合并进一个移位寄存器原语（SRL），是**保护同步链结构**；`ASYNC_REG`/`TIG` 是**告知异步语义、豁免时序**。两者都为同步链服务，但前者管「别拆」、后者管「别算」。

---

### 4.3 板级配置配套

#### 4.3.1 概念说明

到这里出现了一个**一致性问题**：约束层有「板级约束目录」（`KC705/`、`ML505/`、`Atlys/`…），VHDL 层有器件/厂商解析（u2-l3 的 `config.vhdl`）。这两层**必须由同一个开关驱动**，否则会出现「约束给的是 KC705 的管脚，但 VHDL 当成 Spartan-3 来综合」的灾难性错配。

PoC 的解法很优雅：用 `my_config.vhdl` 里的**一个常量 `MY_BOARD`** 同时驱动两层。

- 在 VHDL 层，`MY_BOARD` 经 `config.vhdl` 的 `C_BOARD_INFO_LIST` 表查出对应的 FPGA 器件型号，再解析出 `Vendor` / `Device`，喂给 `generate` 选厂商子实体。
- 在约束层，`MY_BOARD` 的取值（如 `"KC705"`）直接对应 `ucf/<BoardName>/` 目录名，决定工程加载哪一套管脚/时钟约束。

换句话说，`MY_BOARD = "KC705"` 这一行同时决定了「综合器按 Kintex-7 的资源理解 RTL」和「布局器把端口锁到 KC705 板上那些真实的引脚」。一处填写、两层一致。

> 回顾 u1-l3：如果设 `MY_DEVICE="None"`，则 VHDL 层回退到由 `MY_BOARD` 推断器件；这正是 `C_BOARD_INFO_LIST` 派上用场的时刻。

#### 4.3.2 核心流程

把两层串起来看 `MY_BOARD` 的旅行：

```
my_config.vhdl                          ucf/
─────────────────                       ─────────────────
constant MY_BOARD := "KC705";           ucf/KC705/  ←─ 板级约束目录
        │                                     │
        ▼ (VHDL 层)                            ▼ (约束层)
config.vhdl:                             工程按 MY_BOARD 加载
  C_BOARD_INFO_LIST 表                   ucf/KC705/Default.ucf
  "KC705" → FPGADevice                   ucf/KC705/Clock.SystemClock.*
        │                                ucf/KC705/GPIO.LED.* ...
        ▼
  器件型号 → 解析前缀
  "XC7..." → VENDOR_XILINX
        │
        ▼
  T_DEVICE_INFO.Vendor
        │
        ▼ (RTL 层)
  sync_Bits 的 if generate 选
  sync_Bits_Xilinx 子实体
  → 触发 sync_Bits_Xilinx.xdc 约束
```

关键点：这张图里**两个分支共享同一个输入**（`MY_BOARD`），所以它们永远一致。这也解释了为什么核级约束（如 `sync_Bits_Xilinx.xdc`）**间接**受 `MY_BOARD` 控制——只有当选出 Xilinx 器件、`generate` 实例化了 `sync_Bits_Xilinx` 时，那份核约束才有意义。

#### 4.3.3 源码精读

**起点**：模板文件 [`src/common/my_config.vhdl.template:46-53`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/my_config.vhdl.template#L46-L53) 给出两个待填常量：

```vhdl
package my_config is
  constant MY_BOARD  : string  := "CHANGE THIS"; -- e.g. Custom, ML505, KC705, Atlys
  constant MY_DEVICE : string  := "CHANGE THIS"; -- e.g. None, XC5VLX50T-1FF1136, EP2SGX90FF1508C3
  ...
end package;
```

注释里列出的 `Custom`、`ML505`、`KC705`、`Atlys` 正是 `ucf/` 下真实存在的板目录名——这不是巧合，而是约束层与配置层的契约。

**桥梁**：`MY_BOARD` 如何变成器件型号？答案在 [`src/common/config.vhdl:158-189`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L158-L189) 的 `C_BOARD_INFO_LIST` 表，每个条目把 `BoardName` 映射到 `FPGADevice` 及板上外设（UART、以太网）信息：

```vhdl
constant C_BOARD_INFO_LIST : T_BOARD_INFO_VECTOR := (
  ( BoardName => conf("GENERIC"), FPGADevice => conf("GENERIC"), ... ),
  -- Altera boards
  ( BoardName => conf("DE0"),      FPGADevice => conf("EP3C16F484"),     ... ),
  ( BoardName => conf("DE4"),      FPGADevice => conf("EP4SGX230KF40C2"), ... ),
  ...
```

查表逻辑见 [`src/common/config.vhdl:725-732`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L725-L732)：遍历 `C_BOARD_INFO_LIST`，用 `str_imatch` 不区分大小写地比对 `BOARD_NAME`，命中则返回该条目的器件型号。于是 `MY_BOARD="DE4"` 在 VHDL 层解析成 `EP4SGX230KF40C2`，前缀 `EP` 又被 u2-l3 的厂商识别逻辑判为 `VENDOR_ALTERA`——同时，约束层会去 `ucf/DE4/` 取管脚文件。三层（板表 → 器件 → 厂商 → 核约束）环环相扣。

#### 4.3.4 代码实践

**实践目标**：体会「改一个常量、三层联动」。

**操作步骤**：

1. 假设你已按 u1-l3 复制出本地 `my_config.vhdl`。
2. 把 `MY_BOARD` 设为 `"DE4"`、`MY_DEVICE` 设为 `"None"`。
3. 追踪这条链：
   - 在 [`config.vhdl:158`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/src/common/config.vhdl#L158) 的表里查 `DE4`，得到 `FPGADevice = "EP4SGX230KF40C2"`。
   - 按 u2-l3，前缀 `EP` → `VENDOR_ALTERA`。
   - 据 u3-l2，`sync_Bits` 的 `genAltera` 分支被选中，编译 `sync_Bits_Altera` 子实体（而非 `_Xilinx`）。
   - 约束层则应加载 `ucf/DE4/` 下的板约束（如 [`ucf/DE4/Clock.SystemClock.sdc`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/DE4/Clock.SystemClock.sdc)）。

**需要观察的现象**：`MY_BOARD` 一变，VHDL 层选的厂商子实体与约束层选的板目录**同时**变化，且两者厂商一致（DE4→Altera，故 RTL 选 Altera 同步器、约束用 SDC）。

**预期结果**：你能解释为什么切到 Altera 板后，`sync_Bits_Xilinx.xdc` 不再起作用（因为 `sync_Bits_Xilinx` 子实体根本没被编译）。本实践为源码阅读型，约束加载效果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`MY_BOARD="Custom"` 时，约束层该用哪个板目录？
**答案**：没有现成板目录可对应（`ucf/` 下没有 `Custom/`）。`Custom` 表示自定义板，你需要自己写一份管脚约束（参照某块相近板改），并在 VHDL 层用 `MY_DEVICE` 显式给器件型号（而非靠板表推断）。

**练习 2**：为什么 `C_BOARD_INFO_LIST` 里每个字段都用 `conf(...)` 包裹？
**答案**：回顾 u2-l3，`config_private` 用填充符 `C_POC_NUL('~')` 把变长字符串补成定长以便入查找表做模式匹配；`conf(...)` 就是这个定长化处理的封装。它保证不同长度的板名/器件名能在统一的定长类型上比较。

---

## 5. 综合实践

**任务**：为一个实例化的 `sync_Bits` 核，写出需要在 [`ucf/MetaStability.ucf`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf) 框架下补充的 `_meta` / `_async` 约束思路，并解释为什么要对异步信号打 timing ignore。

**背景**：假设你在顶层例化了同步器：

```vhdl
SyncCmd : entity PoC.sync_Bits
  generic map ( BITS => 4, SYNC_DEPTH => 3 )
  port map ( Clock => clk_rx, Input => cmd_async, Output => cmd_sync );
```

`cmd_async` 来自发送时钟域，是异步输入。综合后，工具会按 `sync_Bits` 内部的命名约定生成 `Data_async`（每个 bit 一条）、`Data_meta`、`Data_sync` 网线与触发器实例。

**步骤**：

1. **识别豁免对象**。参照 4.2，确认通配约束要抓两类对象：
   - 异步输入网线：`*_async`（即每 bit 的 `Data_async`，本质是 `Input` 端口到第一级 FF 的 D 端）。
   - 第一级捕获触发器：`*_meta`（或 Xilinx 子实体里的 `FF1_METASTABILITY_FFS`）。
2. **写出 UCF 思路**。其实全库通用的 [`ucf/MetaStability.ucf:4-6`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf#L4-L6) 已经覆盖了所有同步器——只要你的核遵守命名约定，**无需为每个实例单独写约束**。把它纳入工程即可：

   ```text
   NET "*_async"  TIG;
   INST "*_meta*"  TNM = "METASTABILITY_FFS";
   TIMESPEC "TS_MetaStability" = FROM FFS TO "METASTABILITY_FFS" TIG;
   ```

   若用 Vivado，则对 Xilinx 子实体额外加载 [`sync_Bits_Xilinx.xdc:4-6`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Bits_Xilinx.xdc#L4-L6)（用 `SCOPED_TO_REF = sync_Bits_Xilinx` 绑到所有实例）。

3. **回答「为什么要 timing ignore 异步信号」**：
   - 异步输入与捕获时钟无固定相位关系，**注定在某些时刻违反建立/保持时间**，第一级 FF 必然偶发亚稳态——这是物理事实，不是设计缺陷。
   - 时序分析器若试图让这条路径「满足建立/保持」，会因无解而**永远报违例**，且其努力毫无意义（我们靠的是同步链的物理决断，不是 STA 的路径预算）。
   - 因此用 `TIG` / `set_false_path` 把它**从时序分析中摘除**，让 STA 把精力放在真正能收敛的同步域内路径上。代价是把可靠性交给 `SYNC_DEPTH` 决定的决断时间——这恰是 u3-l6 计算 MTBF 的依据。
4. **自检**：确认你的核没有把 `Data_meta` 改名（否则 `INST "*_meta*"` 失配），确认 XDC 的 `PROCESSING_ORDER` 让它在时钟定义之后加载（`set_false_path -from [all_clocks]` 需要先有时钟）。

**预期结果**：你产出的不是「为某个实例新写一堆约束」，而是「确认该核遵循命名约定 → 直接复用 `MetaStability.ucf` + 厂商核级 XDC」的认知。这正体现了 PoC 用命名约定换约束复用的设计哲学。综合后的时序报告里，异步路径应显示为 `TIG`/`false path` 而非违例——「待本地验证」。

---

## 6. 本讲小结

- `ucf/` 用三种格式（`.ucf`/`.xdc`/`.sdc`）描述同一份约束，分别服务 Xilinx ISE、Xilinx Vivado、Altera Quartus；目录按「开发板」与「PoC 核」两类切分。
- 板级约束（`<BoardName>/`）回答「管脚在哪、电平多高、时钟多快」，文件名遵循 `<类别>.<功能>.<格式>`；`Default.ucf` 用 `CONFIG PART` 锁定整板器件型号。
- 核级约束（`misc/`、`net/`…）回答「核内部某些寄存器如何布线/豁免」，且只针对相关厂商工具（如 `sync_Bits_Xilinx.*`）。
- 亚稳态是同步器输入路径的物理事实，**注定无法满足建立/保持**，必须用 `TIG`/`set_false_path` 把它从时序分析中摘除。
- [`ucf/MetaStability.ucf`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/MetaStability.ucf) 三行 + `_async`/`_meta` 命名约定 + `ASYNC_REG`/`SHREG_EXTRACT`/`RLOC` 属性，共同构成 PoC 全库通用的亚稳态约束体系；改名即破约束。
- `MY_BOARD` 一个常量同时驱动 VHDL 层（经 `C_BOARD_INFO_LIST` 解析器件/厂商）与约束层（选 `ucf/<BoardName>/` 目录），保证两层永远一致。

---

## 7. 下一步学习建议

- **进入第 5 单元（专家层）**：本讲是第 4 单元收尾，下一讲 u5-l1「pyIPCMI 基础设施与命令行前端」会揭示 `poc.sh`/`poc.ps1` 如何在命令行层面把 `.files`、约束文件、综合工具串起来自动化调度——你会看到本讲那些约束文件是被 pyIPCMI 自动选择并喂给工具的。
- **补全同步器视角**：若想看 `sync_Reset` / `sync_Pulse` 等其他同步器各自的约束差异，回看 u3-l6 并对比 [`ucf/misc/sync/sync_Reset_Xilinx.xdc`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/misc/sync/sync_Reset_Xilinx.xdc)（注意它对 `FF2`/`FF3` 的 `PRE` 端打 `false_path`，因为复位走异步置位/同步释放路径）。
- **动手扩展**：尝试为自己的一块板（参照 [`ucf/KC705/`](https://github.com/VLSI-EDA/PoC/blob/8c39b2407a97ec81b8601c4b64e4d2e2141ab9cf/ucf/KC705) 的文件命名约定）写一份最小管脚+时钟约束，并在 `my_config.vhdl` 里把 `MY_BOARD` 指向它，验证 4.3 所述的「一处填写、两层一致」。
