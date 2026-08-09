# 单个工程的文件剖析

## 1. 本讲目标

上一篇（u2-l1）讲清了「一个参考设计在逻辑上分三层」。本篇把镜头拉近，打开一个**具体工程目录**，逐个文件讲解它是怎么搭起来的。

读完本讲，你应该能够：

- 说清 AMD（Xilinx）工程的「标准五件套」分别是哪五个文件、各自干什么；
- 看懂 `system_top.v` 如何例化块设计顶层 `system_wrapper`、如何把物理引脚映射到内部信号；
- 区分 `system_project.tcl`（建工程、跑综合实现）与 `system_bd.tcl`（搭块设计）的职责边界；
- 读懂 `system_constr.xdc` 在引脚分配、电平标准（IOSTANDARD）与时序约束上的作用；
- 沿着「芯片 → FMC → FPGA 引脚 → 缓冲 → system_wrapper → IP → DDR」画通一条数据流向。

本篇继续以 `projects/fmcomms2/zcu102`（FMCOMMS2 射频子卡 + ZCU102 载板）作为剖析样本。

## 2. 前置知识

- **块设计（Block Design, BD）**：在 Vivado 里用图形化方式把多个 IP 拖出来连线，工具最终自动生成一段 Verilog 顶层，文件名叫 `system_wrapper.v`。我们不在仓库里手写它，它由 `system_bd.tcl` 描述、由工具生成。
- **综合（Synthesis）/ 实现（Implementation）/ 比特流（Bitstream）**：把 Verilog 翻译成 FPGA 逻辑单元（综合）、再布局布线到具体芯片（实现）、最后生成可烧录的 `.bit`。这套流程在 u1-l4 已介绍。
- **LVDS（Low-Voltage Differential Signaling）**：一种用**一对差分线**（`_p` 正端、`_n` 负端）传一路信号的接口标准，抗干扰强、速率高。ADI 的 AD9361 射频芯片与 FPGA 之间的高速数据/时钟/帧信号都走 LVDS。
- **IOSTANDARD 与 PACKAGE_PIN**：Vivado 的两类约束。`PACKAGE_PIN` 指定某个端口连到 FPGA 封装上的哪根物理引脚；`IOSTANDARD` 指定这根引脚用什么电气标准（如 `LVDS`、`LVCMOS18`）。两者一起写进 `.xdc` 约束文件。
- **EMIO**：Zynq/ZynqMP 处理器系统（PS）把一部分 GPIO 通过 PL（可编程逻辑）引出，软件看到的 GPIO 编号会在硬件编号上加一个固定偏移（Zynq-7000 是 54，ZynqMP 是 78）。

## 3. 本讲源码地图

本讲涉及的关键文件（都属于 `fmcomms2/zcu102` 工程，少数为对照样本）：

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `projects/fmcomms2/zcu102/system_top.v` | 系统特化（第三层） | HDL 顶层，例化 `system_wrapper`、连接物理引脚 |
| `projects/fmcomms2/zcu102/system_project.tcl` | 系统特化 | Vivado 入口脚本：建工程、加文件、跑综合实现 |
| `projects/fmcomms2/zcu102/system_bd.tcl` | 系统特化 | source 载板与评估板两层基设计，做组合级微调 |
| `projects/fmcomms2/zcu102/system_constr.xdc` | 系统特化 | AD9361 相关引脚/电平/时钟约束 |
| `projects/common/zcu102/zcu102_system_constr.xdc` | 载板（第一层） | 载板上按键、拨码、LED 的引脚约束 |
| `projects/fmcomms2/zcu102/Makefile` | 系统特化 | 声明库依赖与文件依赖（详见 u1-l4） |
| `projects/common/zcu102/zcu102_system_bd.tcl` | 载板（第一层） | 载板基设计：例化 PS8、时钟、复位、SPI/GPIO（对照阅读） |
| `library/common/ad_iobuf.v` | 公共库 | 参数化三态 IO 缓冲原语（对照阅读） |
| `projects/fmcomms2/zed/system_top.v` | 对照样本 | 显式例化 `ad_iobuf` 的另一份 `system_top.v` |
| `docs/user_guide/architecture.rst` | 文档 | 工程文件结构的权威说明 |

## 4. 核心概念与源码讲解

### 4.1 工程的「标准五件套」总览

#### 4.1.1 概念说明

打开任意一个 AMD（Xilinx）工程目录，你会发现里面几乎总是这五个文件。官方文档把它们称作一个工程应有的文件清单（见 `architecture.rst` 的 *Project files for AMD boards* 小节）：

1. **`Makefile`** —— 自动生成的依赖清单，列出本工程需要哪些 library IP（`LIB_DEPS`）和哪些设计文件（`M_DEPS`），最后 `include` 公共构建脚本。它在 u1-l4 已详细讲过。
2. **`system_project.tcl`** —— 真正的 Vivado 入口，负责创建工程、添加源文件与约束、跑综合/实现出比特流。
3. **`system_bd.tcl`** —— 描述块设计：先 source 载板基设计、再 source 评估板基设计，再做本工程特有的连线微调。
4. **`system_constr.xdc`** —— 约束文件，把 HDL 端口绑定到 FPGA 物理引脚，并定义电气标准与时钟。
5. **`system_top.v`** —— HDL 顶层，例化块设计自动生成的 `system_wrapper`，并处理 IO 缓冲等与引脚直接相关的事。

#### 4.1.2 核心流程

五件套的协作可以用下面的流水线表示（`make` 在 u1-l4 中已被还原为「最终执行 `vivado -source system_project.tcl`」）：

```
make  ──►  vivado -source system_project.tcl
              │
              ├── adi_project        ──► 内部 source system_bd.tcl
              │                            ├── source 载板基设计 (zcu102_system_bd.tcl)
              │                            ├── source 评估板基设计 (fmcomms2_bd.tcl)
              │                            └── 工程特化微调
              │                     ──► 工具生成 system_wrapper.v (块设计顶层)
              │
              ├── adi_project_files  ──► 加入 system_top.v
              │                          + system_constr.xdc
              │                          + 载板 zcu102_system_constr.xdc
              │                          + ad_iobuf.v
              │
              └── adi_project_run    ──► 综合 + 实现 (吃进 xdc 约束)
                                       ──► 比特流 system_top.bit / system_top.xsa
```

关键关系：

- `system_top.v` 是**综合的顶层**（top module）。它例化的 `system_wrapper`，则由 `system_bd.tcl` 描述、由工具自动生成。两者是「手写顶层」与「工具生成顶层」的衔接点。
- `.xdc` 约束对 `system_top.v` 的端口生效——约束里写的 `get_ports rx_clk_in_p`，指的就是 `system_top.v` 模块端口表里的 `rx_clk_in_p`。
- `system_project.tcl` 同时把 `system_top.v`、本工程约束、**载板约束**（`zcu102_system_constr.xdc`）一起加进工程。约束被拆成两份，正对应三层架构里「载板相关」与「评估板相关」的分离。

#### 4.1.3 源码精读

官方文档对五件套的逐条定义（英文原句值得对照阅读）：

[docs/user_guide/architecture.rst:1081-1115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1081-L1115) —— 列出 AMD 工程应有的五个文件，并特别说明 `system_wrapper` 是工具生成文件，路径在 `<project_name>.srcs/sources_1/bd/system/hdl/system_wrapper.v`，其端口由 `system_bd.tcl` 或评估板基设计声明。

五件套在工程目录里的实物（仍以 fmcomms2/zcu102 为例）：

[projects/fmcomms2/zcu102/system_project.tcl:12-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L12-L23) —— 一眼能看到 `adi_project`、`adi_project_files`、`adi_project_run` 三步，正好对应上图流水线的三个阶段。

#### 4.1.4 代码实践

**实践目标**：建立「文件名 ↔ 职责」的肌肉记忆。

**操作步骤**：

1. 进入 `projects/fmcomms2/zcu102/` 目录，列出文件。
2. 把每个文件对应到五件套中的一个角色，填一张表：`Makefile / system_project.tcl / system_bd.tcl / system_constr.xdc / system_top.v`。
3. 注意还有一个 `zcu102_system_constr.xdc` 不在本目录——它在 `projects/common/zcu102/`，是被 `system_project.tcl` 引用进来的载板约束。

**预期结果**：本目录应能看到上述前四个文件加 `Makefile`；载板约束与载板/评估板基设计 Tcl 都不在本目录，而是通过相对路径 `source`/引用进来。

**待本地验证**：若你手头有仓库检出，用 `ls projects/fmcomms2/zcu102/` 即可核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `system_wrapper.v` 不在仓库里，却仍然能被 `system_top.v` 例化？

**参考答案**：`system_wrapper.v` 是 Vivado 根据 `system_bd.tcl` 描述的块设计**自动生成**的（路径在 `.srcs/sources_1/bd/system/hdl/`）。综合时它已经存在，所以 `system_top.v` 可以例化它；只是它不是手写源码，不入仓库。

**练习 2**：载板的约束 `zcu102_system_constr.xdc` 为什么放在 `projects/common/zcu102/` 而不是工程目录里？

**参考答案**：因为它属于「载板相关」（第一层），多个评估板共用同一块 ZCU102 载板时，这份约束要原样复用；放进 `common/zcu102/` 才能被所有 `*/zcu102` 工程共享，避免重复。

---

### 4.2 system_top.v：HDL 顶层与 IO 缓冲职责

#### 4.2.1 概念说明

`system_top.v` 是整个设计面向 FPGA 物理世界的「门面」：

- 它的**模块端口**就是 FPGA 的物理引脚（综合时的 top module）；
- 它**例化 `system_wrapper`**（块设计顶层），把所有「数字逻辑」交给块设计，自己只做与引脚直接相关的事；
- 它负责**IO 缓冲**（三态、差分转单端等）以及把零散的物理信号**重组成**块设计期望的总线（如 95 位 GPIO 总线）。

一个常见的误解是「`system_top.v` 一定显式例化 IO 缓冲原语」。其实是否显式例化取决于信号类型：差分 LVDS 信号通常由 Vivado 根据 `.xdc` 里的 `IOSTANDARD=LVDS` 自动推断出 `IBUFDS`/`OBUFDS`，无需手写；而**双向三态**信号（如 GPIO、I2C）才常显式例化 `ad_iobuf`。本工程的 zcu102 版本就属于「不显式例化」的那一类，下文会与 zed 版本对照。

#### 4.2.2 核心流程

`system_top.v` 内部一般做三件事：

```
1. 端口声明：列出 FPGA 物理引脚（差分对 _p/_n、单端控制信号、SPI 等）
2. 信号重组：用 assign 把物理端口拼装/映射到块设计的内部总线（gpio_i/gpio_o）
            + 把「未使用的输入位」接到对应输出位（满足 Vivado 不留悬空输入的要求）
3. 例化 system_wrapper：把所有信号连到块设计顶层
```

关于 GPIO 总线位域，官方有一条通用规则（见 `architecture.rst` 的 *GPIOs* 小节）：

- bits \([31:0]\) 始终属于载板；
- bits \([63:32]\) 分配给 FMC 子卡上的开关/按键/LED；
- bits \([95:64]\) 在 Zynq UltraScale+ MPSoC 上使用。

\[
\text{软件 GPIO 号} = \text{硬件 GPIO 位} + \text{EMIO 偏移}
\]

其中 EMIO 偏移：PS7 = 54，PS8（ZynqMP）= 78，MicroBlaze = 0，Versal = -32。这条公式解释了「为什么软件里读到的 GPIO 号比 HDL 里大」。

#### 4.2.3 源码精读

**(1) 模块端口声明**——这些就是 FPGA 的物理引脚：

[projects/fmcomms2/zcu102/system_top.v:38-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_top.v#L38-L69) —— 声明了 AD9361 的 LVDS 差分数据/时钟/帧（`rx_clk_in_p/n`、`rx_data_in_p/n[5:0]`、`tx_*`），单端控制信号（`enable`、`txnrx`、`gpio_*`、`spi_*`），以及载板 GPIO（`gpio_bd_i[12:0]`、`gpio_bd_o[7:0]`）。注意每个差分信号都拆成 `_p` 和 `_n` 两个端口。

**(2) GPIO 总线重组与「悬空输入回接」**：

[projects/fmcomms2/zcu102/system_top.v:79-90](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_top.v#L79-L90) —— 这里把物理信号映射进 95 位的 `gpio_i`/`gpio_o` 总线。例如 `gpio_o[46:46]` 对外驱动 `gpio_resetb`（复位 AD9361），`gpio_i[39:32]` 接收芯片回送的 `gpio_status[7:0]`。第 85、87 行的 `gpio_i[94:40] = gpio_o[94:40]`、`gpio_i[31:13] = gpio_o[31:13]` 正是「未用输入位回接输出」的实现，让 Vivado 不报「input not driven」警告。

**(3) 例化块设计顶层**：

[projects/fmcomms2/zcu102/system_top.v:94-124](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_top.v#L94-L124) —— 例化 `system_wrapper i_system_wrapper`，把 LVDS 差分对、SPI、GPIO 总线、`enable`/`txnrx` 等全部接上。注意 `spi1_*`、`tdd_sync_*` 这些本工程不用的端口被接到空 `()` 或常量 `1'b0`。这一段就是「物理引脚 ↔ 块设计」的衔接点。

**(4) 对照：显式例化 ad_iobuf 的版本**。本 zcu102 设计没有显式例化 IO 缓冲；而同一 fmcomms2 在 zed 载板上的版本则显式用了 `ad_iobuf` 处理双向 GPIO/I2C：

[projects/fmcomms2/zed/system_top.v:135-167](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zed/system_top.v#L135-L167) —— 例化了三个 `ad_iobuf`：`i_iobuf_gpio`（49 位 GPIO）、`i_iobuf_iic_scl`、`i_iobuf_iic_sda`，把双向三态引脚经缓冲后接到 `system_wrapper`。

那么 `ad_iobuf` 到底是什么？它是一个参数化的三态缓冲：

[library/common/ad_iobuf.v:38-54](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/ad_iobuf.v#L38-L54) —— 用 `genvar` 对每一位生成：当方向控制 `dio_t[n]==1` 时把外部引脚 `dio_p[n]` 置高阻（作输入读 `dio_o`），否则把内部 `dio_i[n]` 驱动出去。综合时它会映射成 FPGA 的 `IOBUF` 原语。这也回答了「为什么 zcu102 版本没用它」——zcu102 版本没有需要双向三态的外部 GPIO/I2C 引脚经 `system_top` 中转（相关双向口已在块设计内部处理），所以只用 `assign` 直连。

> 说明：上面 zed 的 `ad_iobuf` 片段属于「对照样本」，用于帮助理解 IO 缓冲的典型写法；本讲剖析的主样本仍是 zcu102 版本。

#### 4.2.4 代码实践

**实践目标**：在 `system_top.v` 中找到 `system_wrapper` 的例化与 IO/缓冲相关逻辑，写出一条从芯片到 FPGA 引脚的数据流向说明。

**操作步骤**：

1. 在 [projects/fmcomms2/zcu102/system_top.v:94-124](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_top.v#L94-L124) 找到 `system_wrapper i_system_wrapper` 例化，确认 `rx_clk_in_p`/`rx_data_in_p` 等差分对直接连到块设计端口。
2. 确认本文件**没有**显式 `IBUFDS`/`OBUFDS`/`ad_iobuf` 例化——记下这个观察：LVDS 差分缓冲由 Vivado 根据 `.xdc` 的 `IOSTANDARD=LVDS` 自动推断。
3. 追踪一路 GPIO 控制信号，例如 `enable`：在 L56 声明为输出，在 L95 连到 `i_system_wrapper.enable`；它是 PS 通过 `gpio_o[47]`（即 `up_enable`，见 L123）经块设计驱动出来的。
4. 画一条数据流：`PS 软件 → EMIO GPIO → sys_ps8 → gpio_o[47] → system_wrapper.up_enable → enable 端口 → PACKAGE_PIN Y12（见 xdc L41）→ FMC_HPC0_LA16_P → AD9361 ENABLE 引脚`。

**需要观察的现象**：

- 差分信号成对出现（`_p`/`_n`），单端信号只有一个端口；
- `gpio_i`/`gpio_o` 是 95 位宽总线，物理信号按位「嵌」进总线；
- 第 85、87 行的回接赋值是「未用输入位」的处理手法。

**预期结果**：你应该得出结论——zcu102 的 `system_top.v` 几乎只做「连线 + GPIO 总线重组」，逻辑非常薄；真正的数字逻辑都在 `system_wrapper`（块设计）里。这正是 ADI 设计的风格：顶层保持简单。

#### 4.2.5 小练习与答案

**练习 1**：`system_top.v` 里 `gpio_i[31:13] = gpio_o[31:13]`（L87）这一行删掉会怎样？

**参考答案**：这些 GPIO 位在本工程未使用、没有物理引脚驱动，但 `system_wrapper` 的 `gpio_i` 端口需要完整的 95 位输入。删掉后这些输入位会悬空，Vivado 在综合/实现阶段会针对「未驱动的输入」产生警告甚至把对应逻辑优化掉，可能影响 PS EMIO GPIO 读回的正确性。回接输出是官方推荐的规避手法（见 `architecture.rst` *GPIOs* 小节的说明）。

**练习 2**：同样是 fmcomms2，为什么 zed 的 `system_top.v` 显式例化 `ad_iobuf`，zcu102 却没有？

**参考答案**：zed 载板需要把 GPIO、I2C 这些**双向三态**信号经 `system_top` 引到 FMC 物理引脚，必须显式插入三态缓冲（`ad_iobuf` → `IOBUF`）；zcu102 版本没有这类需经顶层中转的双向口（双向处理已在块设计内部完成），差分 LVDS 缓冲又由工具按 `IOSTANDARD` 自动推断，所以顶层只剩 `assign` 直连，无需显式 `ad_iobuf`。

---

### 4.3 system_project.tcl 与 system_bd.tcl：建工程 vs 搭块设计

#### 4.3.1 概念说明

这两个 Tcl 文件名字相近，职责却完全不同：

- **`system_project.tcl`** —— 「工程调度器」。它直接被 `vivado -source` 调用，负责创建工程、添加源文件和约束、设置实现策略、启动综合/实现。它是**流程**脚本。
- **`system_bd.tcl`** —— 「块设计图纸」。它在 `system_project.tcl` 调用 `adi_project` 时被**间接 source**，负责把载板基设计和评估板基设计拼到一起，再做本工程特有的参数微调。它是**结构**脚本。

一句话区分：`system_project.tcl` 回答「**怎么把设计跑起来**」，`system_bd.tcl` 回答「**设计长什么样**」。

#### 4.3.2 核心流程

`system_project.tcl` 的标准四阶段（u1-l4 已提，这里补全文件层面）：

```
0. 准备：source adi_env.tcl        （设环境变量、定位仓库）
         source adi_project_xilinx.tcl（adi_project 等过程定义）
         source adi_board.tcl         （ad_connect 等连线原语定义）
1. adi_project <name>          → 创建工程，内部 source system_bd.tcl → 生成块设计
2. adi_project_files <name> …  → 加入 system_top.v / system_constr.xdc / 载板约束 / ad_iobuf.v
3. （可选）设置实现策略
4. adi_project_run <name>      → 综合 + 实现 + 写比特流；之后可 source 校准脚本
```

`system_bd.tcl` 的标准三步（承接 u2-l1 的三层架构）：

```
1. source 载板基设计       （zcu102_system_bd.tcl —— 例化 PS8、时钟、复位、SPI/GPIO）
2. source 评估板基设计     （../common/fmcomms2_bd.tcl —— 例化 axi_ad9361、dmac、pack/unpack 并连线）
3. 工程特化微调            （改 sysid ROM、调 axi_ad9361 参数等，仅本组合需要）
```

注意第 1 步用绝对路径 `$ad_hdl_dir/projects/common/zcu102/...`，第 2 步用相对路径 `../common/fmcomms2_bd.tcl`——因为评估板基设计与本工程在同一 `fmcomms2/` 目录树下。

#### 4.3.3 源码精读

**`system_project.tcl` 全文**（这份脚本很短，值得整体读）：

[projects/fmcomms2/zcu102/system_project.tcl:6-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L6-L25) —— 逐行对应：

- L6 `source adi_env.tcl`：设好 `ad_hdl_dir` 等环境（u1-l3 讲过）。
- L7-8：引入 `adi_project`/`ad_connect` 等过程定义。
- L9：把 `auto_timing_fix_xilinx.tcl` 设为实现后的时序修复脚本（POST_ROUTE_SCRIPT，u8-l3 会详讲）。
- L12 `adi_project fmcomms2_zcu102`：创建并搭块设计（内部会 source 本目录的 `system_bd.tcl`）。
- L13-17 `adi_project_files`：把四类文件加入工程——`system_top.v`（顶层）、`system_constr.xdc`（本工程约束）、`ad_iobuf.v`（公共缓冲原语，给块设计里的双向口用）、`zcu102_system_constr.xdc`（载板约束）。**载板约束就是在这里被「引用进来」的**，呼应 4.1 节的悬念。
- L21 `set_property strategy Congestion_SpreadLogic_high`：注释说明 fmcomms2 在某些路径有 hold time 违例，故把实现策略改为「Spread Logic」帮助修 hold。
- L23 `adi_project_run`：综合 + 实现 + 出比特流。
- L24 `source axi_ad9361_delay.tcl`：跑完后 source AD9361 的时延校准脚本（u5-l2 会用到）。

**`system_bd.tcl` 全文**（同样很短）：

[projects/fmcomms2/zcu102/system_bd.tcl:6-20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_bd.tcl#L6-L20) ——

- L6-8：三连 source——载板基设计、评估板基设计、`adi_pd.tcl`（电源域/功耗相关辅助）。
- L11-15：调 `axi_sysid_0`/`rom_sys_0` 的参数，并调用 `sysid_gen_sys_init_file` 生成系统标识文件（用于软件运行时校验硬件版本）。
- L17-20：本工程特有的 `util_ad9361_divclk`（设 `SIM_DEVICE ULTRASCALE`）与 `axi_ad9361`（设 `ADC_INIT_DELAY 11`、`DELAY_REFCLK_FREQUENCY 500`）参数微调。

**载板基设计里有什么**（对照阅读，帮助理解第一层）：

[projects/common/zcu102/zcu102_system_bd.tcl:27-48](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_bd.tcl#L27-L48) —— 例化 `zynq_ultra_ps_e sys_ps8` 并配置 PS8 的 PL 时钟（`pl_clk0`=100MHz、`pl_clk1`=250MHz、`pl_clk2`=500MHz）、中断、EMIO GPIO、SPI0/SPI1。这正是 u2-l1 说的「载板基设计负责处理器与时钟」。

#### 4.3.4 代码实践

**实践目标**：把「建工程」与「搭块设计」的职责区分落到具体代码行。

**操作步骤**：

1. 在 `system_project.tcl` 里找出「加入载板约束」的那一行（提示：`adi_project_files` 列表里带 `${BOARD_NAME}_system_constr.xdc`）。
2. 在 `system_project.tcl` 里找出「块设计是在哪一步被搭起来的」（提示：`adi_project` 调用，块设计 Tcl 在其内部被 source）。
3. 在 `system_bd.tcl` 里找出 source 载板基设计与评估板基设计的两行，确认它们的路径前缀（一个 `$ad_hdl_dir/...`、一个 `../common/...`）。
4. 解释：为什么 `set_property strategy Congestion_SpreadLogic_high` 出现在 `system_project.tcl` 而不是 `system_bd.tcl`？

**需要观察的现象**：

- `system_project.tcl` 里没有任何 `create_bd_cell`/`ad_connect` 之类的连线指令——它只调度流程；
- `system_bd.tcl` 里没有 `synth_design`/`impl_design`——它只描述结构；
- 工程特有的参数微调（`ADC_INIT_DELAY` 等）集中在 `system_bd.tcl` 末尾。

**预期结果**：你能用一句话回答本节标题——`system_project.tcl` 管「跑综合实现的流程」，`system_bd.tcl` 管「块设计里有哪些 IP、怎么连、参数多少」。

#### 4.3.5 小练习与答案

**练习 1**：如果我想给 AD9361 改一个初始化时延参数，应该改 `system_project.tcl` 还是 `system_bd.tcl`？为什么？

**参考答案**：改 `system_bd.tcl`。因为 `ADC_INIT_DELAY` 是块设计里 `axi_ad9361` 这个 IP 的实例参数（见 `system_bd.tcl` L19），属于「设计长什么样」；而 `system_project.tcl` 只负责建工程和跑流程，不描述 IP 参数。

**练习 2**：载板约束 `zcu102_system_constr.xdc` 是通过哪条路径进入工程的？

**参考答案**：由 `system_project.tcl` 的 `adi_project_files` 列表加入（文件名 `$ad_hdl_dir/projects/common/${BOARD_NAME}/${BOARD_NAME}_system_constr.xdc`，见 `system_project.tcl` L17）。它不在本工程目录，但通过这条引用被纳入工程，与 `system_constr.xdc` 一起参与综合/实现的约束。

---

### 4.4 system_constr.xdc：引脚、电平与时序约束

#### 4.4.1 概念说明

`.xdc`（Xilinx Design Constraint）是 Vivado 的约束文件。在本工程里它做三件事：

1. **引脚分配（PACKAGE_PIN）**：把 `system_top.v` 的每个端口绑定到 FPGA 封装上的某根物理引脚。注释里还标了这根引脚对应的 FMC 连接器信号名（如 `FMC_HPC0_LA00_CC_P`），方便对照硬件原理图。
2. **电平标准（IOSTANDARD）**：声明每根引脚的电气标准——AD9361 高速信号用 `LVDS`（并带 100Ω 差分终端 `DIFF_TERM_ADV TERM_100`），控制信号/SPI/GPIO 用 `LVCMOS18`，载板上按键/LED 用 `LVCMOS33`。
3. **时序约束（create_clock）**：声明输入时钟的周期，让时序引擎据此检查建立/保持时间。

约束被拆成两份，分别对应三层架构的两层：

| 约束文件 | 位置 | 内容 | 对应层 |
| --- | --- | --- | --- |
| `system_constr.xdc` | 工程目录 | AD9361 的 LVDS 数据/时钟/帧、控制信号、SPI、本工程时钟 | 评估板相关 |
| `zcu102_system_constr.xdc` | `common/zcu102/` | 载板拨码开关、按键、LED，PS SPI 时钟定义 | 载板相关 |

#### 4.4.2 核心流程

差分 LVDS 接收通路的约束流程（以 `rx_clk_in_p` 为例）：

```
system_top.v 端口 rx_clk_in_p
        │  （差分对的正端，负端 rx_clk_in_n）
        ▼
.xdc: set_property PACKAGE_PIN Y4        ← 绑定到 FMC_HPC0_LA00_CC_P 物理引脚
.xdc: set_property IOSTANDARD LVDS       ← 声明差分电平标准
.xdc: set_property DIFF_TERM_ADV TERM_100 ← 启用 FPGA 内部 100Ω 差分终端
.xdc: create_clock -period 4.00 rx_clk_in_p ← 声明 4ns 周期 = 250MHz
        │
        ▼
Vivado 据此自动推断 IBUFDS，把差分对转成单端信号送入 system_wrapper
```

时钟周期与频率的换算：

\[
f = \frac{1}{T}, \quad T = 4.00\,\text{ns} \Rightarrow f = 250\,\text{MHz}
\]

这正是 AD9361 LVDS 接口的典型时钟频率。差分终端 `TERM_100` 的物理意义是：在 FPGA 输入端并一个 100Ω 电阻到差分对之间，与差分线的差分阻抗匹配，消除反射——这是 LVDS 接收的硬件要求。

#### 4.4.3 源码精读

**(1) AD9361 差分接收时钟/数据/帧的引脚与电平**：

[projects/fmcomms2/zcu102/system_constr.xdc:9-24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L9-L24) —— 每行约束一个差分端口（注意只对 `_p` 端写 `PACKAGE_PIN`，`_n` 端由 Vivado 自动配对）。所有接收端都带 `DIFF_TERM_ADV TERM_100`。行尾注释 `## G06 FMC_HPC0_LA00_CC_P` 表示该引脚连到 FMC 连接器的 LA00_CC_P（G06 是 AD9361 那侧的网名），用于对照子卡原理图。

**(2) AD9361 差分发送端**（注意不带差分终端）：

[projects/fmcomms2/zcu102/system_constr.xdc:25-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L25-L40) —— `tx_*` 发送端只设 `IOSTANDARD LVDS`，**没有** `DIFF_TERM_ADV`——因为终端电阻只在接收端需要，发送端不需要（差分终端应在 AD9361 那侧的接收端，或此处为发送故不加）。

**(3) 单端控制信号与 SPI**：

[projects/fmcomms2/zcu102/system_constr.xdc:41-63](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L41-L63) —— `enable`/`txnrx`/`gpio_*`/`spi_*` 全用 `LVCMOS18`。注意 `spi_csn`（L60）额外带 `PULLTYPE PULLUP`——片选空闲时上拉，防止 AD9361 误响应总线上的噪声。

**(4) 本工程时钟约束**：

[projects/fmcomms2/zcu102/system_constr.xdc:67](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L67) —— `create_clock -name rx_clk -period 4.00 [get_ports rx_clk_in_p]`，定义 250MHz 输入参考时钟。

**(5) 载板约束（载板相关层）**：

[projects/common/zcu102/zcu102_system_constr.xdc:9-34](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/common/zcu102/zcu102_system_constr.xdc#L9-L34) —— 载板上的拨码开关 `gpio_bd_i[0:7]`、方向按键 `gpio_bd_i[8:12]`、LED `gpio_bd_o[0:7]`，全部 `LVCMOS33`（载板 Bank 电压 3.3V，与 AD9361 那侧的 1.8V 不同）。L33-34 用 `get_pins` 在 PS 的 SPI 时钟引脚上定义 25MHz（40ns）时钟。

> 对比记忆：`system_constr.xdc` 里是 `LVCMOS18`（AD9361 子卡 1.8V），`zcu102_system_constr.xdc` 里是 `LVCMOS33`（载板 3.3V）。电平标准必须与该 Bank 的供电电压一致，否则综合会报错或硬件无法工作。

#### 4.4.4 代码实践

**实践目标**：读懂一份 `.xdc`，把每条约束映射到硬件行为。

**操作步骤**：

1. 在 [system_constr.xdc:9-10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc#L9-L10) 找到 `rx_clk_in_p`/`rx_clk_in_n` 的约束，记下引脚号（Y4/Y3）和注释里的 FMC 信号名（`FMC_HPC0_LA00_CC_P/N`）。
2. 对比 L9（接收端，带 `TERM_100`）与 L25（发送端 `tx_clk_out_p`，不带 `TERM_100`），解释差异。
3. 在载板约束里找到 LED `gpio_bd_o[0]` 对应的引脚（L23，`AG14`）和电平（`LVCMOS33`）。
4. 把约束和 `system_top.v` 端口对应起来：`.xdc` 里的 `get_ports enable` ↔ `system_top.v` L56 的 `output enable` ↔ L95 例化时连到 `i_system_wrapper.enable`。

**需要观察的现象**：

- 差分端口只写 `_p` 端的 `PACKAGE_PIN`；
- 接收端带差分终端，发送端不带；
- 同一份工程里同时出现 `LVCMOS18` 与 `LVCMOS33`，分属不同 Bank。

**预期结果**：你能独立说出「`set_property PACKAGE_PIN/IOSTANDARD` + `create_clock`」三类约束各自的作用，并解释 `DIFF_TERM_ADV TERM_100` 的硬件意义。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `rx_clk_in_n`（L10）没有单独写 `PACKAGE_PIN`，但仍然能绑定到正确的差分负端引脚 Y3？

**参考答案**：LVDS 差分对的正负端是 FPGA 引脚 bank 内的固定配对。Vivado 只要给 `_p` 端指定了 `PACKAGE_PIN` 和 `IOSTANDARD LVDS`，会自动找到该 bank 内配对的负端引脚。注释里写明 `## G07 FMC_HPC0_LA00_CC_N` 只是给人对照原理图用。所以差分约束只对 `_p` 端写 `PACKAGE_PIN` 是惯例。

**练习 2**：把 `rx_clk` 的周期从 4.00ns 改成 5.00ns（即 200MHz），综合/实现会发生什么？硬件会怎样？

**参考答案**：**约束层面**：时序引擎会按 200MHz 检查路径，原本在 250MHz 边缘紧张的路径现在变宽松，时序报告更好看——但这只是「告诉工具放宽要求」，并不会让设计更快。**硬件层面**：实际时钟频率由 AD9361 那侧决定，约束改不了真实硬件时钟；如果硬件上仍是 250MHz，而约束写 200MHz，就会导致时序检查与实际不符，可能在真实 250MHz 下出现建立/保持时间违例而误码。所以约束周期必须如实反映硬件时钟。**待本地验证**：实际时序报告需在 Vivado 中运行后查看。

---

## 5. 综合实践

**任务**：为 fmcomms2/zcu102 工程画一张完整的「端口 → 引脚 → 芯片」对应表，并用一段话把五件套串成一条数据通路。

**要求**：

1. 从 [system_top.v:38-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_top.v#L38-L69) 选 3 个端口（建议：一个差分接收对 `rx_clk_in_p/n`、一个控制输出 `enable`、一个 GPIO 输入 `gpio_status[0]`）。
2. 在 [system_constr.xdc](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_constr.xdc) 中查出它们的 `PACKAGE_PIN`、`IOSTANDARD` 与 FMC 信号名，填入表格：

   | system_top 端口 | 方向 | PACKAGE_PIN | IOSTANDARD | FMC 信号 | 对端（AD9361/载板） |
   | --- | --- | --- | --- | --- | --- |
   | rx_clk_in_p | in | Y4 | LVDS + TERM_100 | FMC_HPC0_LA00_CC_P | AD9361 RX frame/clk |
   | enable | out | … | … | … | … |
   | gpio_status[0] | in | … | … | … | … |

3. 用一段话描述一条**接收数据通路**：`AD9361 → FMC_HPC0 → FPGA 引脚 Y4（受 xdc 约束为 LVDS+TERM_100）→ Vivado 推断的 IBUFDS → system_wrapper.rx_clk_in_p（system_top.v L94 例化）→ 块设计里的 axi_ad9361（由 system_bd.tcl 经 fmcomms2_bd.tcl 例化）→ util_cpack2 → axi_dmac → PS DDR`。
4. 最后用一句话点出每个环节由五件套中的哪个文件负责（端口定义↔`system_top.v`，引脚/电平↔`system_constr.xdc`，IP 例化↔`system_bd.tcl`，跑流程↔`system_project.tcl`，依赖清单↔`Makefile`）。

**预期结果**：一张对应表 + 一段串起五件套的数据通路描述。完成后，你对「一个工程目录里这五个文件如何协作」就有了从代码到硬件的完整理解。

## 6. 本讲小结

- AMD 工程的**标准五件套**是 `Makefile`、`system_project.tcl`、`system_bd.tcl`、`system_constr.xdc`、`system_top.v`，外加被引用进来的载板约束/基设计。
- `system_top.v` 是综合顶层，例化工具生成的 `system_wrapper`，并做物理引脚与内部总线（95 位 GPIO）的重组；它通常很「薄」，zcu102 版本甚至不显式例化任何 IO 缓冲（LVDS 缓冲由工具按 `IOSTANDARD` 推断，双向三态才用 `ad_iobuf`，如 zed 版本）。
- `system_project.tcl` 是**流程脚本**（建工程、加文件、跑综合实现），`system_bd.tcl` 是**结构脚本**（source 两层基设计 + 工程特化微调）；两者职责不重叠。
- `.xdc` 约束分两份：`system_constr.xdc`（评估板相关，AD9361 的 LVDS 与 1.8V 控制）与 `zcu102_system_constr.xdc`（载板相关，3.3V 按键/LED），正对应三层架构的分离。
- 载板约束由 `system_project.tcl` 的 `adi_project_files` 引用进工程，不在本工程目录里。
- 约束周期必须如实反映硬件时钟（`rx_clk` 4ns=250MHz），否则时序检查与真实硬件脱节。

## 7. 下一步学习建议

本篇讲清了「工程目录里有什么、各文件干什么」。接下来：

- 想深入**块设计连线**的细节（`ad_connect`、`ad_cpu_interconnect`、`ad_ip_instance`），请读 **u3-l4（板级连线助手 Tcl：adi_board.tcl）**，那里会拆开 `fmcomms2_bd.tcl` 里每条连线原语的语义。
- 想理解**库 IP 如何被打包**成 `system_bd.tcl` 里能拖用的 IP（即 `LIB_DEPS` 背后的事），请读 **u4-l2（Xilinx IP 打包：adi_ip_xilinx.tcl）**。
- 想看**约束与时序收敛的进阶**（POST_ROUTE_SCRIPT、`auto_timing_fix_xilinx.tcl`），请读 **u8-l3（收发器、时钟与时序约束）**。
- 如果你的目标是**移植或新建工程**，建议接着读 **u7-l1（移植工程到新载板）** 与 **u7-l2（创建与定制新工程）**——本篇的文件清单正是它们的模板。
