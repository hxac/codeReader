# 移植到新板卡：适配流程指南

## 1. 本讲目标

学完本讲，你应该能够：

- 理解把 pcileech-fpga 移植到一块**新 Artix-7 板卡**到底要改什么、不改什么。
- 准确列出移植时必须修改的 4 类文件（约束 `xdc`、顶层 `*_top.sv`、PCIe IP `pcie_7x_0.xci`、工程生成脚本 `vivado_generate_project*.tcl`）。
- 看懂约束文件里的引脚映射（`PACKAGE_PIN`/`IOSTANDARD`）与时序约束，并知道哪些是「板卡相关必须改」、哪些是「逻辑相关不要动」。
- 认识 PCIe 的硬性硬件依赖：专用 GTP 收发器、差分参考时钟、`PERST`/`PRSNT` 侧带信号，理解为什么这些约束值必须和板卡走线严格一致。
- 学会处理 FPGA 型号与封装差异（例如 `xc7a35tfgg484` 与 `xc7a35tcsg325`、`xc7a75t`、`xc7a100t` 的区别），并最终独立为一块新板卡起草一份移植 checklist。

## 2. 前置知识

本讲是专家层「设备变种与二次开发」单元的第 2 篇，承接以下已建立的概念（不再重复展开）：

- **三大子系统骨架不变**（来自 u1-l4、u6-l1）：任何 pcileech-fpga 工程都是 `com → fifo → pcie_a7` 的结构，`fifo` 永远不变，真正「可插拔」的只有两处——**通信核心**（FT601/以太网/FT2232H/雷电桥）与 **PCIe 核**（`pcie_a7` x1 或 `pcie_a7x4` x4）。这是移植代价小的根本原因。
- **约束文件三类指令**（来自 u5-l2）：`PACKAGE_PIN`/`IOSTANDARD`/`LOC`/`SLEW`/`IOB` 属物理实现约束；`create_clock`/`set_input_delay`/`set_output_delay` 属时序约束；`set_false_path`/`set_multicycle_path` 属时序例外。
- **FPGA 基础概念**：
  - **FPGA 型号（part）**：例如 `xc7a35tfgg484-2`，其中 `xc7a35t` 是逻辑容量（35K 逻辑单元的 Artix-7），`fgg484` 是封装（484 焊球 BGA），`-2` 是速度等级。
  - **引脚（pin）/ 焊球（ball）**：FPGA 芯片对外有多少根金属引脚，每根在封装上有一个唯一名字（如 `H4`、`F6`）。
  - **IOSTANDARD（电气标准）**：决定一根引脚的电压与协议，例如 `LVCMOS33` 表示 3.3V 单端 CMOS。
  - **GTP 收发器**：Artix-7 内部专做高速串行（如 PCIe）的硬核电路，叫 `GTPE2_CHANNEL`，它有固定的硅片位置，不是普通引脚。
  - **PERST / PRSNT**：PCIe 的两根侧带信号，PERST 是复位、PRSNT（present）表示板卡插在槽里。

如果你对「约束文件怎么绑定引脚」「GTP 是什么」完全陌生，建议先回看 u5-l2 再继续。

## 3. 本讲源码地图

本讲以官方主参考工程 **PCIeSquirrel**（Artix-7 XC7A35T，x1 + FT601 USB3）为基准，并大量引用仓库自带的 **CaptainDMA 家族**作为「同一套代码移植到多种 FPGA 型号/封装」的真实范例。

| 文件 | 作用 | 移植时是否要改 |
| --- | --- | --- |
| [PCIeSquirrel/src/pcileech_squirrel.xdc](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc) | 物理与时序约束：把顶层每个端口绑到焊球、设电气标准、声明时钟、定位 GTP | **几乎必改**（引脚映射随板卡走线而变） |
| [PCIeSquirrel/src/pcileech_squirrel_top.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv) | 顶层模块：声明对外端口、例化 com/fifo/pcie 三大子系统 | **通常不改逻辑**，仅当端口数量变化（如 x1→x4）时才动 |
| [PCIeSquirrel/vivado_generate_project.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl) | 工程生成脚本：指定 FPGA 型号、导入源码/IP/约束、创建综合实现 run | **必改 part 字符串**（FPGA 型号/封装/速度等级不同时） |
| [PCIeSquirrel/build.md](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md) | 构建/定制说明文档 | 移植后应同步更新说明 |

辅助引用的真实移植样本（CaptainDMA 家族）：

| 文件 | 说明 |
| --- | --- |
| [CaptainDMA/35t484_x1/vivado_generate_project_captaindma_35t.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t484_x1/vivado_generate_project_captaindma_35t.tcl) | 同为 35T+FGG484，part 与 Squirrel 相同 |
| [CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl) | 35T 但换 CSG325 封装 |
| [CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl) | 升级到 75T |
| [CaptainDMA/100t484-1/vivado_generate_project_captaindma_100t.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/100t484-1/vivado_generate_project_captaindma_100t.tcl) | 升级到 100T |

## 4. 核心概念与源码讲解

### 4.1 移植总览：基准工程、文件清单与差异分类

#### 4.1.1 概念说明

「移植（porting）」在这里的含义很具体：**让同一套 pcileech-fpga 逻辑，在一块物理上不同的 Artix-7 板卡上正确跑起来**。它不是重写功能，而是「重新接线」——告诉综合工具，每个逻辑端口在新的这块芯片上应该落在哪根焊球、用哪种电气标准、连到哪条硬件走线。

之所以移植代价可控，是因为 u6-l1 已经确立的关键事实：**所有设备变种骨架一致（com→fifo→pcie），`fifo` 永远不变**。所以真正需要为新板卡操心的只有四类东西：

1. **物理引脚**（板卡走线决定的，每块板都不一样）→ 改 `xdc`。
2. **FPGA 型号/封装**（芯片本身的容量与焊球排布）→ 改工程生成脚本的 `part`。
3. **PCIe IP 的 lane 数与硬核位置**（若新板要做 x4 或 GTP 通道不同）→ 在 Vivado GUI 里改 `pcie_7x_0.xci` 并重生。
4. **对外端口清单**（仅当通信核心或 lane 数改变，例如换 FT2232、改 x4）→ 改顶层 `*_top.sv`。

一句话：**逻辑层基本不动，物理层（引脚、型号、GTP 位置）几乎全动。**

#### 4.1.2 核心流程

移植的标准步骤可以画成一条单向链：

```text
1. 选基准工程 ── 按 FPGA 型号 + 通信核心 + lane 数 选最接近的现有工程
        │
2. 建 fork ── 复制该工程目录，改名（顶层 *_top.sv、xdc、tcl、工程名）
        │
3. 改 part ── 改 generate_project.tcl 的 -part 与各处 part 引用
        │
4. 重映射引脚 ── 拿新板原理图，逐根核对 xdc 的 PACKAGE_PIN / IOSTANDARD
        │
5. 核对 PCIe 硬件依赖 ── GTP LOC、参考时钟引脚、PERST/PRSNT
        │
6. 调顶层端口（按需）── 若换通信核心或 lane 数才改 *_top.sv 端口
        │
7. 改 PCIe IP（按需）── lane 数变了在 GUI 改 pcie_7x_0.xci 并重生
        │
8. 生成工程 + 构建 ── source generate_project.tcl → source build.tcl
        │
9. 烧录 + 验证 ── 烧到新板，在目标机 lspci 看是否枚举出设备
```

「选基准工程」是最关键的一步：选得越接近，后面改动越少。仓库里就有现成的选择树——下表把 CaptainDMA 家族当作「同一逻辑、不同型号/封装」的基准对照：

| 你的新板 | 推荐基准 | 原因 |
| --- | --- | --- |
| Artix-7 35T + FGG484 + USB3 + x1 | PCIeSquirrel 或 CaptainDMA/35t484_x1 | part 完全相同，引脚最接近 |
| Artix-7 35T + CSG325（小封装）+ x1 | CaptainDMA/35t325_x1 | 同 die、同封装，引脚已就绪 |
| Artix-7 75T 或 100T + FGG484 + x1 | CaptainDMA/75t484_x1 或 100t484-1 | 同封装、升级 die |
| 需要 x4 lane | CaptainDMA/35t325_x4 | 已是 x4 工程 |

#### 4.1.3 源码精读

四类要改的文件在 Squirrel 工程里的对应位置：

- **约束**：[pcileech_squirrel.xdc:L1-L116](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L1-L116) —— 整个文件都是「板卡相关」，引脚与电气标准全在这里。
- **顶层**：[pcileech_squirrel_top.sv:L13-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52) 的端口声明段 —— 端口名字必须与 `xdc` 里的 `get_ports` 名字逐字一致。
- **工程脚本 part**：[vivado_generate_project.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74) `create_project ... -part xc7a35tfgg484-2` —— 指定了芯片。
- **构建说明**：[build.md:L5-L14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L5-L14) —— 标准构建步骤与「路径过长」注意事项。

#### 4.1.4 代码实践

**实践目标**：用仓库自带的真实样本，验证「同 die + 同封装 → part 字符串相同」这一选基准规则。

**操作步骤**：

1. 打开 [vivado_generate_project.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74)，记下 Squirrel 的 part。
2. 打开 [CaptainDMA/35t484_x1/vivado_generate_project_captaindma_35t.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t484_x1/vivado_generate_project_captaindma_35t.tcl#L74)，对比 part。
3. 再打开 [CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl:L82](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl#L82) 与 [CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl#L74)。

**需要观察的现象**：

| 工程 | part 字符串 | die | 封装 | 速度等级 |
| --- | --- | --- | --- | --- |
| PCIeSquirrel | `xc7a35tfgg484-2` | 35T | FGG484 | -2 |
| CaptainDMA/35t484_x1 | `xc7a35tfgg484-2` | 35T | FGG484 | -2 |
| CaptainDMA/35t325_x1 | `xc7a35tcsg325-2` | 35T | **CSG325** | -2 |
| CaptainDMA/75t484_x1 | `xc7a75tfgg484-2` | **75T** | FGG484 | -2 |
| CaptainDMA/100t484-1 | `xc7a100tfgg484-2` | **100T** | FGG484 | -2 |

**预期结果**：Squirrel 与 CaptainDMA/35t484_x1 的 part 完全相同（同为 35T+FGG484-2），所以两者互为最佳基准；换成 CSG325 封装或 75T/100T die，part 字符串才会变。**待本地验证**：在没有 Vivado 的环境下无法亲眼看到综合报错，但 part 字符串本身就是证据。

#### 4.1.5 小练习与答案

**练习 1**：你拿到一块「Artix-7 35T、CSG325 封装、USB3、x1」的新板，应选哪个现有工程做基准？为什么？
> **答案**：选 CaptainDMA/35t325_x1。因为它 part 是 `xc7a35tcsg325-2`，与新板的 die 与封装完全一致，引脚布局最接近，移植改动最小。

**练习 2**：移植时，`pcileech_fifo.sv`、`pcileech_com.sv` 这类公共源文件要不要改？为什么？
> **答案**：不要改。它们是与板卡无关的逻辑（路由中枢与通信状态机），靠 interface 与物理层解耦；移植只动物理映射（xdc、part、GTP 位置），不碰这些公共 HDL。

### 4.2 xdc 引脚重映射（最小模块：xdc 引脚）

#### 4.2.1 概念说明

`xdc`（Xilinx Design Constraints）是连接「逻辑端口」与「物理芯片」的契约文件。它回答三个问题：

- **哪根端口绑到哪个焊球？** 用 `PACKAGE_PIN`。
- **这根引脚用什么电气标准？** 用 `IOSTANDARD`。
- **时钟/数据该怎么采样？** 用 `create_clock` / `set_input_delay` / `set_output_delay`。

对移植而言，**引脚映射是改动最密集、最容易出错、也最致命的部分**——绑错一根线，板子轻则功能错乱，重则烧器件（比如把 3.3V 信号设成 2.5V 标准）。Squirrel 的 `pcileech_squirrel.xdc` 把端口分成几大组，每组对应顶层的一类对外接口，逐组核对即可。

#### 4.2.2 核心流程

移植时 `xdc` 的核对顺序，按「风险从高到低、依赖从硬到软」排：

```text
1. PCIe 收发器与参考时钟  ── 硬走线决定，错了链路起不来（见 4.4）
2. 系统时钟 clk            ── 全局复位与所有状态机的心跳
3. FT601 数据/控制/通信时钟 ── 与主机通信的全部引脚
4. LED / 按键 / ft2232_rst  ── 人机交互与板上复位，相对次要
5. 时序约束（delay/false_path）── 电气标准一致时通常可照搬
6. 比特流配置项（CFGBVS/SPI）── 与供电/烧录方式相关
```

一条重要经验：**电气标准（IOSTANDARD、电压）必须和新板原理图一致**。Squirrel 大量使用 `LVCMOS33`（3.3V）；若新板某组 Bank 供电是 2.5V 或 1.8V，对应引脚就要改成 `LVCMOS25` / `LVCMOS18`，否则综合会报 DRC 错误或上电异常。

#### 4.2.3 源码精读

**(1) FT601 数据线**：32 位 `ft601_data` 逐位绑定焊球。
[pcileech_squirrel.xdc:L5-L36](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L5-L36) 把 `ft601_data[0]..[31]` 逐一指定到 `N14`、`N15`、…、`AB20`。移植时这 32 行的焊球名都要按新板原理图重填。

**(2) FT601 控制与字节使能**：[pcileech_squirrel.xdc:L1-L4](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L1-L4)（`ft601_be[0..3]`）与 [pcileech_squirrel.xdc:L37-L43](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L37-L43)（`oe_n/rd_n/rxf_n/siwu_n/txe_n/wr_n/rst_n`）。

**(3) 电气标准与压摆率**：
[pcileech_squirrel.xdc:L44-L48](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L44-L48) 把 FT601 全部引脚设成 `LVCMOS33` 且 `SLEW FAST`。移植时核对 Bank 电压是否仍为 3.3V。

**(4) LED 与按键**：[pcileech_squirrel.xdc:L50-L54](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L50-L54) 绑定 `user_ld1/ld2/sw1_n/sw2_n`。新板若 LED/按键数量不同，这里要增删。

**(5) 系统时钟与通信时钟**：
[pcileech_squirrel.xdc:L59-L67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L59-L67)：
```tcl
# SYSCLK
set_property PACKAGE_PIN H4 [get_ports clk]
create_clock -period 10.000 -name net_clk ...
# FT601 CLK
create_clock -period 10.000 -name net_ft601_clk ...
set_property PACKAGE_PIN W19 [get_ports ft601_clk]
```
这两段声明了 100MHz 系统时钟（`H4`）与 FT601 输出的通信时钟（`W19`）。`-period 10.000`（单位 ns）= 100MHz。**移植重点**：新板的晶振引脚、频率必须重填；若新板系统时钟不是 100MHz（例如 50MHz 或 125MHz），`-period` 也要相应改成 20.000 或 8.000，否则时序分析与实际不符。

**(6) FT601 时序约束（input/output delay）**：
[pcileech_squirrel.xdc:L69-L79](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L69-L79) 描述 FT601 并行总线相对 `net_ft601_clk` 的建立/保持关系。只要新板仍用 FT601 芯片、走线长度相近，这些值通常可照搬；若换其他 USB 桥（如 FT2232H），整段要重写。

**(7) IOB 寄存器打包与时序例外**：
[pcileech_squirrel.xdc:L81-L90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L81-L90) 把 FT601 输出寄存器强制打到 IOB（I/O Block）里以缩短延迟，并放过 `tickcount64`、`_pcie_core_config` 等慢速/异步路径。这部分路径名（如 `i_pcileech_com/i_pcileech_ft601/...`）与模块例化层级强绑定，**只要例化名不变就可照搬**。

**(8) 比特流配置**：[pcileech_squirrel.xdc:L111-L116](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L111-L116) 设 `CFGBVS Vcco`、`CONFIG_VOLTAGE 3.3`、SPI 烧录宽度 4、压缩等。`CONFIG_VOLTAGE` 必须与新板的配置 Bank 供电一致。

#### 4.2.4 代码实践

**实践目标**：把 Squirrel 的 FT601 引脚映射和新板做对照，建立「逐根核对」的肌肉记忆。

**操作步骤**：

1. 从 [pcileech_squirrel.xdc:L1-L48](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L1-L48) 提取 FT601 全部引脚（32 数据 + 4 BE + 7 控制 + 1 时钟 = 44 根）。
2. 建一张表：`端口名 | Squirrel 焊球 | IOSTANDARD | SLEW | 新板焊球（待填）| 新板电压（待填）`。
3. 拿到新板原理图，逐根把「新板焊球」「新板电压」两列填上；电压若非 3.3V，标记需改 `IOSTANDARD`。

**需要观察的现象**：你会直观看到——FT601 数据线占了整整 32 行，是核对工作量的大头；而控制线只有 7 根但漏一根就通信失败。

**预期结果**：得到一张完整的「FT601 引脚迁移表」。**待本地验证**：最终对错只能在新板综合通过、且 `lspci`/PCILeech 能枚举设备时才确认。

#### 4.2.5 小练习与答案

**练习 1**：新板的 100MHz 晶振接在 `R4` 而非 `H4`，该怎么改？
> **答案**：把 [pcileech_squirrel.xdc:L60](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L60) 的 `PACKAGE_PIN H4` 改成 `R4`；`create_clock` 的 `-period 10.000` 保持不变（频率没变）。

**练习 2**：新板某 Bank 供电是 1.8V，但 xdc 里仍写 `LVCMOS33`，综合会怎样？
> **答案**：会触发 DRC（设计规则检查）错误或上电异常，因为 `LVCMOS33` 要求 Bank 供 3.3V。应改成与该 Bank 电压匹配的 `LVCMOS18`。

### 4.3 顶层模块端口与电气依赖（最小模块：顶层模块）

#### 4.3.1 概念说明

顶层模块（`*_top.sv`）是整块 FPGA 的「门面」：它声明所有对外端口，并在内部例化 com/fifo/pcie 三大子系统。移植时，顶层有两层含义需要分清：

- **端口名 = 契约**：顶层声明的端口名（如 `ft601_data`、`pcie_rx_p`）必须和 `xdc` 里 `get_ports` 的名字**逐字一致**，否则综合报「端口无约束」。所以改 xdc 引脚时，端口名通常不动。
- **端口清单 = 板卡能力**：端口的数量与位宽由板卡硬件能力决定。换通信核心（FT601→FT2232）、改 lane 数（x1→x4）时，端口清单才需要增删。

换句话说：**改引脚不改端口名；改硬件能力才改端口清单。**

#### 4.3.2 核心流程

判断顶层要不要改的决策树：

```text
新板与基准相比，通信核心变了吗？（FT601↔以太网↔FT2232↔雷电桥）
├─ 否 → 端口清单基本不动，只核对名字与 xdc 一致
└─ 是 → 改对应端口组（如 ft601_* 换成对应的 eth_* / ft2232_*）
        └─ 同时换通信核心源文件（如 pcileech_ft601.sv 换成 pcileech_eth.sv）

新板 PCIe lane 数变了吗？（x1↔x4）
├─ 否 → pcie_tx_p/n、pcie_rx_p/n 仍为 [0:0]
└─ 是 → 改成 [3:0]，并改用 pcileech_pcie_a7x4 与对应 src128/dst128
```

此外，顶层的**复位与 LED 逻辑**（如 `tickcount64`、长按 5 秒重载）通常可原样保留，因为它依赖的是 `clk` 与按键，与具体引脚无关。

#### 4.3.3 源码精读

Squirrel 顶层端口声明在 [pcileech_squirrel_top.sv:L13-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52)，按组看：

**(1) 系统时钟**：
[pcileech_squirrel_top.sv:L20-L21](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L20-L21)
```verilog
input           clk,        // 系统域 100MHz
input           ft601_clk,  // 通信域（FT601 输出）
```
这两个时钟对应 xdc 里 `net_clk` 与 `net_ft601_clk`，是移植必查项。

**(2) LED 与按键**：[pcileech_squirrel_top.sv:L24-L29](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L24-L29) 声明 `user_ld1/ld2`（LED）、`user_sw1_n/sw2_n`（按键）、`ft2232_rst_n`（板上 FT2232 复位）。新板若没有 FT2232，`ft2232_rst_n` 可悬空或删除（同时删 xdc 对应行）。

**(3) PCIe 金手指**：
[pcileech_squirrel_top.sv:L31-L40](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L31-L40)
```verilog
output  [0:0]   pcie_tx_p,   // x1：仅 1 对差分发送
output  [0:0]   pcie_tx_n,
input   [0:0]   pcie_rx_p,   // x1：仅 1 对差分接收
input   [0:0]   pcie_rx_n,
input           pcie_clk_p,  // 100MHz 差分参考时钟
input           pcie_clk_n,
input           pcie_present, // PRSNT：板卡在位
input           pcie_perst_n, // PERST：PCIe 复位
output reg      pcie_wake_n = 1'b1;
```
注意 `[0:0]` 表示 x1（1 对）。移植到 x4 时，`pcie_tx_p/n`、`pcie_rx_p/n` 要改成 `[3:0]`，且 GTP 占用 4 个通道。这几根是 PCIe 硬依赖的核心（见 4.4）。

**(4) FT601 端口**：[pcileech_squirrel_top.sv:L43-L51](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L43-L51) 声明 32 位 `inout ft601_data`、4 位 `ft601_be` 与若干控制信号。换通信核心时这整组要替换。

**例化不变量**：[pcileech_squirrel_top.sv:L98-L164](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L98-L164) 例化 `i_pcileech_com`、`i_pcileech_fifo`、`i_pcileech_pcie_a7`，并通过 5 个 interface 把它们连起来。这段「接线」与板卡无关，移植时基本不动——这正是骨架一致的体现。

#### 4.3.4 代码实践

**实践目标**：验证「每个顶层端口都能在 xdc 找到对应约束」，建立端口-约束一致性意识。

**操作步骤**：

1. 从 [pcileech_squirrel_top.sv:L13-L52](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L13-L52) 列出全部端口名。
2. 在 [pcileech_squirrel.xdc](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc) 中搜索每个端口名。
3. 标记哪些端口有 `PACKAGE_PIN`，哪些没有。

**需要观察的现象**：差分时钟与 GTP 收发器（`pcie_rx_p/n`、`pcie_tx_p/n`、`pcie_clk_p/n`）有引脚约束，但实际的「收发器位置」由 `LOC GTPE2_CHANNEL_X0Y2`（4.4 节）单独指定，二者配合。

**预期结果**：得到一张「端口 → xdc 行号 → 焊球」对照表，确认无端口漏约束。**待本地验证**：综合时的 DRC/IO 检查报告是最终裁判。

#### 4.3.5 小练习与答案

**练习 1**：把 Squirrel 从 x1 改成 x4，顶层端口要改哪几处？
> **答案**：[pcileech_squirrel_top.sv:L32-L35](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L32-L35) 的 `pcie_tx_p/n`、`pcie_rx_p/n` 从 `[0:0]` 改成 `[3:0]`；同时把例化的 `pcileech_pcie_a7` 换成 x4 版本（`pcileech_pcie_a7x4`），并相应改 xdc 的 GTP lane 约束。

**练习 2**：移植后综合报 `pcie_clk_p has no IO constraint`，最可能原因是什么？
> **答案**：顶层端口名与 xdc 里 `get_ports` 名字不一致（如顶层叫 `pcie_clk_p` 但 xdc 写成 `pcie_refclk_p`），或 xdc 对应行被误删。两者名字必须逐字相同。

### 4.4 PCIe GTP 通道、参考时钟与 PERST/PRSNT（关键硬件依赖）

#### 4.4.1 概念说明

PCIe 是高速串行差分协议，不能跑在普通 FPGA 引脚上，必须用芯片内部的**专用收发器硬核（GTP，即 `GTPE2_CHANNEL`）**。这是移植中最「硬」的依赖，有三个特点：

1. **位置固定**：GTP 通道在硅片上的位置由芯片版图决定，不能任意摆放，只能用 `LOC` 指定用哪一个。
2. **走线绑定**：板卡上 PCIe 金手指的差分对，是固定连到某几个 GTP 通道的，硬件走线决定了一切，**软件改不动**。
3. **参考时钟必填**：GTP 需要一个低抖动的差分参考时钟（通常 100MHz，称 PCIe 参考时钟），由板卡经专用引脚喂进来。

此外还有两根「侧带（side-band）」信号：

- **PERST**（`pcie_perst_n`）：PCIe 复位，主机拉低复位设备。FPGA 必须把它接进 PCIe 核做硬复位。
- **PRSNT**（`pcie_present`）：表示板卡物理插在槽里（PRSNT1# 与 PRSNT2# 短接），主机据此判断在位。

这三类东西（GTP、参考时钟、PERST/PRSNT）是 PCIe 能否「上线」的命脉，移植时必须按新板原理图逐个核对。

#### 4.4.2 核心流程

PCIe 硬依赖的核对清单：

```text
1. GTP 通道位置 ── xdc 的 LOC GTPE2_CHANNEL_X0Yn，n 由板走线决定
2. 收发差分对   ── pcie_tx_p/n[0]、pcie_rx_p/n[0] 的 PACKAGE_PIN
3. 参考时钟     ── pcie_clk_p/n 的 PACKAGE_PIN（专用 GTP 参考时钟引脚）
4. PERST        ── pcie_perst_n 的 PACKAGE_PIN
5. PRSNT        ── pcie_present 的 PACKAGE_PIN（可按需）
6. 参考时钟频率 ── create_clock -period 与硬件晶振一致（通常 100MHz）
```

一个关键经验（仓库真实数据支撑）：**只要还在 Artix-7 家族内（35T/75T/100T），收发器原语始终是 `GTPE2_CHANNEL`**——CaptainDMA 的 35T、75T、100T 三个工程用的都是 `GTPE2_CHANNEL_X0Y2`。需要改变量的是 `LOC` 后面的 `X0Yn`（哪个通道）和差分对的 `PACKAGE_PIN`（哪根焊球），这些全由板卡走线决定。

#### 4.4.3 源码精读

**(1) GTP 通道定位**：
[pcileech_squirrel.xdc:L93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L93)
```tcl
set_property LOC GTPE2_CHANNEL_X0Y2 [get_cells {.../pipe_lane[0].gt_wrapper_i/gtp_channel.gtpe2_channel_i}]
```
这行把 PCIe lane 0 锁定到硅片上 `X0Y2` 位置的 GTP 通道。这是**与板卡走线严格绑定**的约束：板厂把金手指的 Rx/Tx 差分对布到哪个 GTP，这里就必须填哪个坐标，**移植时不可凭感觉乱填**，必须查新板原理图或约束参考。

仓库内的对照证据（全部为 `GTPE2_CHANNEL_X0Y2`，原语与坐标都一致）：
- [CaptainDMA/35t484_x1/.../pcileech_35t484_x1_captaindma_35t.xdc:L93](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t484_x1/src/pcileech_35t484_x1_captaindma_35t.xdc#L93)
- [CaptainDMA/35t325_x1/.../pcileech_35t325_x1_captaindma_m2.xdc:L95](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/src/pcileech_35t325_x1_captaindma_m2.xdc#L95)

**(2) PERST / PRSNT / WAKE 引脚**：
[pcileech_squirrel.xdc:L94-L99](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L94-L99)
```tcl
set_property PACKAGE_PIN A13 [get_ports pcie_present]   # PRSNT
set_property PACKAGE_PIN B13 [get_ports pcie_perst_n]   # PERST
set_property PACKAGE_PIN A14 [get_ports pcie_wake_n]
set_property IOSTANDARD LVCMOS33 [get_ports {pcie_present pcie_perst_n pcie_wake_n}]
```

**(3) 收发差分对**：
[pcileech_squirrel.xdc:L101-L104](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L101-L104) 把 `pcie_rx_n/p[0]`、`pcie_tx_n/p[0]` 绑到 `A10/B10/A6/B6`。这些焊球就是连到 GTP 收发器的专用引脚。

**(4) 参考时钟差分对**：
[pcileech_squirrel.xdc:L106-L109](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L106-L109)
```tcl
set_property PACKAGE_PIN E6  [get_ports pcie_clk_n]
set_property PACKAGE_PIN F6  [get_ports pcie_clk_p]
create_clock -name pcie_sys_clk_p -period 10.0 [get_nets pcie_clk_p]   # 100MHz
```
`pcie_clk_p/n` 是给 GTP 用的差分参考时钟。注意不同板这根的焊球完全不同，真实数据如下：

| 工程 | `pcie_clk_p` 焊球 | die + 封装 |
| --- | --- | --- |
| PCIeSquirrel | `F6` | 35T + FGG484 |
| CaptainDMA 35t484_x1 | `F6` | 35T + FGG484（同封装，引脚相同）|
| CaptainDMA 35t325_x1 | `D6` | 35T + CSG325（换封装，引脚变）|
| CaptainDMA 75t/100t | `F10` | 75T/100T + FGG484（换 die，引脚变）|

这张表是移植最直观的教训：**同 die 同封装引脚可复用；换封装或换 die，参考时钟焊球必变。**

**(5) PERST 进入硬核**：在顶层，`pcie_perst_n` 经 [pcileech_squirrel_top.sv:L156](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel_top.sv#L156) 接入 `i_pcileech_pcie_a7`，作为 PCIe 核硬复位的来源之一（见 u3-l1 的 `rst_pcie` 复位层级）。新板若把 PERST 接到别的焊球，必须在 xdc 同步改。

#### 4.4.4 代码实践

**实践目标**：用仓库真实数据，建立「换封装/换 die → PCIe 参考时钟引脚必变」的判断能力。

**操作步骤**：

1. 打开 Squirrel 的 [pcileech_squirrel.xdc:L106-L107](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_squirrel.xdc#L106-L107)，记下 `pcie_clk_p = F6`。
2. 打开 [CaptainDMA/35t325_x1/.../pcileech_35t325_x1_captaindma_m2.xdc:L117](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/src/pcileech_35t325_x1_captaindma_m2.xdc#L117)，看 `pcie_clk_p` 是多少。
3. 打开 [CaptainDMA/75t484_x1/.../pcileech_75t484_x1_captaindma_75t.xdc:L106](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/75t484_x1/src/pcileech_75t484_x1_captaindma_75t.xdc#L106)，看 `pcie_clk_p` 是多少。

**需要观察的现象**：同为 35T 但封装从 FGG484 换成 CSG325，`pcie_clk_p` 从 `F6` 变成 `D6`；die 从 35T 升到 75T，`pcie_clk_p` 变成 `F10`。

**预期结果**：确认「引脚迁移表」中 PCIe 参考时钟必须逐板重填，不能照搬。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GTP 通道用 `LOC` 而不是 `PACKAGE_PIN` 约束？
> **答案**：`PACKAGE_PIN` 约束的是普通 IO 焊球；而 GTP 是芯片内部的高速串行收发器硬核，不是普通 IO，它在硅片上有固定坐标（如 `X0Y2`），所以用 `LOC` 指定用哪一个 GTP 通道实例。

**练习 2**：移植时 GTP 的 `LOC GTPE2_CHANNEL_X0Y2` 能不能直接照搬？
> **答案**：取决于新板走线。若新板把 PCIe 差分对也布到了 `X0Y2` 通道，可照搬；否则必须改成原理图对应的坐标（如 `X0Y0`）。坐标填错会导致链路训练失败、`lspci` 看不到设备。

### 4.5 工程生成脚本与 FPGA 型号/封装差异（最小模块：工程生成脚本）

#### 4.5.1 概念说明

`vivado_generate_project.tcl` 是「工程的施工图」：它告诉 Vivado 用哪颗芯片（`-part`）、导入哪些源码/IP/约束、创建哪些综合实现 run。移植时，这个脚本最关键的一处是 **`-part` 字符串**——它必须和新板的 FPGA 完全一致，否则综合要么报错、要么生成不能用的比特流。

`part` 字符串由三段组成，以 `xc7a35tfgg484-2` 为例：

| 段 | 含义 | 例子 |
| --- | --- | --- |
| die（逻辑容量） | Artix-7 的容量档 | `xc7a35t`、`xc7a75t`、`xc7a100t` |
| 封装 | 焊球排布与数量 | `fgg484`（484 球）、`csg325`（325 球）|
| 速度等级 | 工艺速度档 | `-1`、`-2`（越大约快）|

移植时这三段都可能变：换更大容量的 die（35T→100T）、换更小封装（FGG484→CSG325）、或换速度档。

#### 4.5.2 核心流程

改工程脚本的标准动作：

```text
1. 查新板 FPGA 丝印/数据手册，确定完整 part 字符串（die+封装+速度等级）
2. 改 generate_project.tcl 中所有 -part 与 part 属性（create_project 与各 run 段）
3. 按需改工程名/顶层名（若你 fork 后重命名了模块）
4. 确认源码、IP、约束的导入路径仍正确
5. 确认 bin_file=1（产出可烧录 .bin）
6. source generate_project.tcl → source build.tcl
```

注意 `generate_project.tcl` 里 `part` 字符串**出现多次**（`create_project`、constrset 的 `target_part`、`synth_1`/`impl_1` run 的 part），换芯片时要全部同步，否则工程内部不一致。

#### 4.5.3 源码精读

**(1) 主 part 指定**：
[vivado_generate_project.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L74)
```tcl
create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part xc7a35tfgg484-2
```
以及 [vivado_generate_project.tcl:L86](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L86) 的 `set_property -name "part" -value "xc7a35tfgg484-2"`。换芯片时这两处（及后面 run 段）都要改。

**仓库内不同 part 的真实样本**：
- 35T+FGG484：`xc7a35tfgg484-2`（Squirrel、CaptainDMA 35t484_x1）
- 35T+CSG325：`xc7a35tcsg325-2`（[CaptainDMA/35t325_x1/...tcl:L82](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl#L82)）
- 75T+FGG484：`xc7a75tfgg484-2`（[CaptainDMA/75t484_x1/...tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl#L74)）
- 100T+FGG484：`xc7a100tfgg484-2`（[CaptainDMA/100t484-1/...tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/100t484-1/vivado_generate_project_captaindma_100t.tcl#L74)）

**(2) 源码导入**：
[vivado_generate_project.tcl:L108-L120](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L108-L120) 导入 11 个源文件。移植后若换通信核心（如把 `pcileech_ft601.sv` 换成 `pcileech_eth.sv`），要在这里同步替换文件名。

**(3) IP 导入与升级**：
脚本逐一导入约 20 个 `.xci`（FIFO/BRAM/PCIe 等存储类与硬核 IP），并在 [vivado_generate_project.tcl:L593](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L593) `upgrade_ip [get_ips *]` 把旧版 IP 升到当前 Vivado 版本。换 Vivado 版本或换 die 时，IP（尤其 `pcie_7x_0.xci`）可能需要重新生成。

**(4) 顶层与综合实现 run**：
[vivado_generate_project.tcl:L174](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L174) 设顶层为 `pcileech_squirrel_top`。若你重命名了顶层模块，这里要改。`synth_1`（[L603-L608](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L603-L608)）与 `impl_1`（[L629-L634](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L629-L634)）各带一个 `-part` 参数，换芯片也要同步。`impl_1` 在 [L839](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl#L839) 设 `bin_file=1`，保证产出可烧录的 `.bin`。

**(5) 构建注意事项**：[build.md:L14](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/build.md#L14) 提示「目录路径过长会导致构建失败」，移植时建议把工程放在短路径（如 `C:\Temp`）下构建。

#### 4.5.4 代码实践

**实践目标**：把 Squirrel 工程的 part 从 35T 改成 75T，体验「改 part 要同步多处」。

**操作步骤**：

1. 在 [vivado_generate_project.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/vivado_generate_project.tcl) 中搜索 `xc7a35tfgg484-2`，数出它出现的次数（至少 `create_project`、`part` 属性、`synth_1`、`impl_1` 四处）。
2. 假设要改成 75T，把所有 `xc7a35tfgg484-2` 替换成 `xc7a75tfgg484-2`。
3. 对照 [CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl:L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/75t484_x1/vivado_generate_project_captaindma_75t.tcl#L74) 验证你的替换是否与官方 75T 工程一致。

**需要观察的现象**：part 字符串不是一处，而是工程脚本里反复出现的全局常量；漏改任何一处都会导致工程内部矛盾。

**预期结果**：所有 part 引用统一为新值，与官方 75T 工程的脚本对齐。**待本地验证**：最终由 `source generate_project.tcl` 成功创建工程、`source build.tcl` 产出 `.bin` 来确认。

#### 4.5.5 小练习与答案

**练习 1**：`xc7a35tfgg484-2` 与 `xc7a35tcsg325-2` 有什么相同与不同？
> **答案**：die 相同（都是 `xc7a35t`，35K 逻辑单元）、速度等级相同（`-2`）；封装不同（`fgg484` 是 484 球 BGA，`csg325` 是 325 球 BGA）。封装不同导致可用焊球与引脚排布完全不同，xdc 引脚映射必须重做。

**练习 2**：换了更大的 die（如 35T→100T）后，只改 `generate_project.tcl` 的 part 就够了吗？
> **答案**：不一定够。part 要全处同步改；此外 100T 的 GTP 通道可用数量与版图位置可能与 35T 不同，若新板 PCIe 差分对走了不同通道，GTP 的 `LOC` 也要相应核对。建议以官方 100T 工程（CaptainDMA/100t484-1）为基准比对。

## 5. 综合实践

**任务：为一块假设的新板卡「AcmeDMA-35T」起草一份完整移植 checklist。**

假设新板规格：Artix-7 XC7A35T、CSG324 封装、速度等级 -1、FT601 USB3 通信、PCIe x1、板上 100MHz 晶振接 `K3`、FT601 参考时钟接 `L19`、无 FT2232、1 颗 LED 接 `P14`、1 个按键接 `N14`。

请按下面的框架，产出一份可交付的移植 checklist（直接填写）：

### 5.1 基准工程选择
- 推荐 fork 的基准工程：________（提示：die=35T、通信=FT601、lane=x1，最接近 Squirrel 或 CaptainDMA/35t484_x1）
- 你的新板封装 CSG324 与基准封装 FGG484 不同，预期影响：________

### 5.2 文件改动清单
| 文件 | 改动类型 | 具体内容 |
| --- | --- | --- |
| `*_top.sv` | 改端口清单 / 不动 | 删除 `ft2232_rst_n`（无 FT2232）；LED 改 1 颗 |
| `*.xdc` | 改引脚 | 系统时钟 → `K3`；ft601_clk → `L19`；LED → `P14`；按键 → `N14`；删 ft2232 行 |
| `*.xdc` | 改 PCIe | GTP `LOC` 按新板原理图填；`pcie_clk_p/n`、`pcie_perst_n`、`pcie_present` 重填 |
| `*.xdc` | 改电气 | 核对各 Bank 电压，CSG324 引脚名全变，逐根重填 |
| `generate_project*.tcl` | 改 part | `xc7a35tcsg324-1`（封装 CSG324、速度 -1），全处同步 |

### 5.3 四类必须核对的约束项（本讲核心交付）

1. **系统时钟引脚**：新 `clk` 焊球 = `K3`；确认频率仍是 100MHz（`-period 10.000` 不变）；`IOSTANDARD` 与该 Bank 电压一致。
2. **FT601 数据/控制引脚**：32 数据 + 4 BE + 7 控制 + `ft601_clk`(`L19`)，全部按 CSG324 原理图重填；保留 `SLEW FAST` 与 input/output delay（芯片未换，时序可照搬）。
3. **PCIe 收发器与参考时钟**：GTP `LOC GTPE2_CHANNEL_X0Yn` 按新板走线填（不可照搬）；`pcie_clk_p/n`、`pcie_rx/tx_p/n[0]`、`pcie_perst_n`、`pcie_present` 逐根重填。
4. **复位与 LED**：按键 → `N14`（注意 `user_sw2_n` 是全局复位源，绑错会无法复位）；LED → `P14`；删掉 `ft2232_rst_n` 相关行。

### 5.4 验证步骤
1. `source generate_project.tcl -notrace` 成功创建工程，无 part 不一致报错。
2. `source build.tcl` 产出 `.bin`（注意放在短路径）。
3. 烧录后插目标机：`lspci -d ::` 应枚举出默认 `10ee:0666` 设备；若看不到，优先查 GTP `LOC` 与参考时钟引脚。
4. **待本地验证**：最终以链路训练成功、PCILeech 能通信为通过标准。

## 6. 本讲小结

- **移植 = 重新接线，不是重写逻辑**：三大子系统骨架（com→fifo→pcie）不变，`fifo` 永远不动，真正要改的只有 xdc、顶层端口、PCIe IP、工程脚本四类物理/配置文件。
- **选对基准工程是关键**：尽量选 die、通信核心、lane 数都相同的现有工程；仓库内 CaptainDMA 家族覆盖了 35T/75T/100T 与 FGG484/CSG325 多种组合，是现成的移植样本。
- **xdc 引脚映射是改动最密集处**：FT601 占 44 根引脚（32 数据 + 4 BE + 7 控制 + 1 时钟），需按新板原理图逐根重填，电气标准（`IOSTANDARD`）必须与 Bank 电压一致。
- **PCIe 是最硬的依赖**：GTP 收发器（`GTPE2_CHANNEL`）位置由板走线决定，用 `LOC` 锁定；参考时钟、PERST、PRSNT 缺一不可，填错则链路起不来、`lspci` 看不到设备。
- **part 字符串要全处同步**：`generate_project.tcl` 里的 die+封装+速度等级（如 `xc7a35tcsg325-2`）在 `create_project`、`part` 属性、`synth_1`、`impl_1` 多处出现，换芯片必须一并改。
- **真实数据规律**：同 die 同封装引脚可复用（Squirrel 与 CaptainDMA 35t484 的 `pcie_clk_p` 都是 `F6`）；换封装（→`D6`）或换 die（→`F10`）则引脚必变。

## 7. 下一步学习建议

- **回看 u6-l1（设备变种对比）**：把本讲的「移植 checklist」与 u6-l1 的「端口级 diff」能力结合，能更高效地选择基准并预判改动。
- **继续 u6-l3（LTSSM、链路状态与调试）**：移植烧录后链路若起不来，下一步就是用 LTSSM 状态、链路状态寄存器与 LED 诊断来定位是 GTP 位置错、参考时钟错，还是 PERST 没接对——u6-l3 正是这块的排错工具箱。
- **动手阅读一份真实移植脚本**：精读 [CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/vivado_generate_project_captaindma_m2x1.tcl) 与对应的 [pcileech_35t325_x1_captaindma_m2.xdc](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/CaptainDMA/35t325_x1/src/pcileech_35t325_x1_captaindma_m2.xdc)，把它当作「官方做的一次完整移植」逐行对照 Squirrel，巩固本讲的全部概念。
