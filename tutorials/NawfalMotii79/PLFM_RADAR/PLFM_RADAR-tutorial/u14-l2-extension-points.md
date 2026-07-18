# 二次开发扩展点与 Vivado 构建流

## 1. 本讲目标

本讲是「形式化验证与二次开发」单元的第二讲，也是整本学习手册的收尾讲义之一。前面十三单元带你把 AERIS-10 雷达从微波硬件一路读到上位机目标航迹；本讲不再介绍新的功能模块，而是回答一个更高级的问题：

> **如果我想给这台雷达加一个新功能，到底要改哪些地方、按什么顺序改、怎么保证改完不把别处改坏？**

学完本讲，你应当能够：

1. 清楚地列出「新增一条主机命令（opcode）」时，FPGA RTL、Python 协议层、GUI、跨层测试这四处必须**同步**修改的精确位置。
2. 理解仓库为什么会有多个 `radar_system_top_*.v` 顶层、多套 `constraints/*.xdc`，以及它们与 `scripts/*/build_*.tcl` 的对应关系。
3. 读懂一个 Vivado 批处理构建脚本（TCL），知道它如何通过 `USB_MODE` 这类 Verilog 参数把同一份 RTL 编译到不同板卡。
4. 独立规划一个端到端功能扩展（RTL + 协议 + GUI + 测试），并对照 `CONTRIBUTING.md` 的 CI 清单完成自检。

本讲依赖 u3-l1（FPGA 顶层）、u6-l2（主机命令协议与 Opcode 映射）、u8-l1（GUI V7 架构）、u11-l1（FPGA 回归测试），请确保你对「opcode 是跨层硬契约」和「顶层是接线员」这两个结论有印象。

---

## 2. 前置知识

在动手扩展之前，先用三段话复习几个关键认知（这些结论在前置讲义里都已建立，这里只做面向「扩展」的再聚焦）。

**第一，opcode 是跨层硬契约。** 主机发给 FPGA 的每一条控制命令都是一个 4 字节字 `{opcode, addr, value}`，其中 `opcode` 决定「写哪个寄存器」。这个编号在 FPGA 的 Verilog `case(usb_cmd_opcode)` 表里、在 Python 的 `Opcode(IntEnum)` 枚举里、在跨层契约测试的真值表里，**三处必须一字不差**。任何一处单独改动都会引发隐性故障。

**第二，顶层是「接线员」而非「计算员」。** `radar_system_top.v` 本身几乎不做运算，它的核心工作是：声明物理引脚、用 `BUFG` 缓冲时钟、把主机命令译码成一组 `host_*` 配置寄存器、再把这些寄存器扇出到各个处理子模块。所以「加一条命令」在顶层的工作量主要落在**译码表**和**寄存器扇出**这两件事上。

**第三，多板卡靠「换顶层 + 换约束 + 换 TCL」三件套适配。** AERIS-10 有 50T 量产板、200T 高端板、TE0713/TE0712 开发板等多种载体。仓库不为每块板维护一份完整 RTL 副本，而是用 `USB_MODE` 这类**编译期参数**让同一份 `radar_system_top.v` 适配不同 USB 芯片，并为开发板额外提供一层薄包装顶层。

此外，本讲会用到几个 Vivado / FPGA 工程术语，先统一解释：

| 术语 | 含义 |
|------|------|
| **顶层模块（top module）** | 综合的入口模块，其端口就是 FPGA 的物理引脚 |
| **约束文件（XDC, Vivado Design Constraints）** | 文本文件，规定「某个 RTL 端口绑定到哪个封装引脚」、电平标准、时序例外 |
| **TCL 构建脚本** | 用 Vivado 的 Tcl 命令写成的批处理脚本，自动完成「建工程→加源码→综合→实现→生成比特流→出报告」 |
| **WNS / WHS** | 最差建立时间余量 / 最差保持时间余量（Worst Negative Slack），为正才表示时序收敛 |
| **`generate` 块** | Verilog 的编译期条件结构，根据 `parameter` 在综合时选择性地例化不同子模块 |

---

## 3. 本讲源码地图

本讲涉及的文件分为四组，对应扩展工作的四个战场：

| 文件 | 作用 | 在本讲中的角色 |
|------|------|----------------|
| [9_Firmware/9_2_FPGA/radar_system_top.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v) | FPGA 顶层，命令译码与子模块例化 | 新增 opcode 的 RTL 主战场 |
| [9_Firmware/9_3_GUI/radar_protocol.py](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py) | Python 协议层，`Opcode` 枚举与命令拼装 | 新增 opcode 的 Python 镜像 |
| [9_Firmware/9_2_FPGA/mti_canceller.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/mti_canceller.v) | MTI 杂波对消器（综合实践的对象） | 扩展功能的落地模块 |
| [9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc) | TE0713 开发板 + UMFT601X 的引脚约束 | 多板卡 XDC 的范例 |
| [9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl) | TE0713 开发板批处理构建脚本 | TCL 构建流范例 |
| [9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl) | 200T 量产构建脚本 | 展示 `USB_MODE` 参数化构建 |
| [CONTRIBUTING.md](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/CONTRIBUTING.md) | 贡献指南与 CI 清单 | 扩展后的自检清单 |

仓库里还存在一组「同族文件」，理解它们的命名规律就能快速定位：

- **多套顶层**：`radar_system_top.v`（量产逻辑核心）、`radar_system_top_50t.v`（50T 物理包装）、`radar_system_top_te0713_umft601x_dev.v`、`radar_system_top_te0712_dev.v`、`radar_system_top_te0713_dev.v`（开发板包装）。
- **多套 XDC**：`xc7a200t_fbg484.xdc`（200T 量产）、`xc7a50t_ftg256.xdc`（50T 量产）、`te0713_te0701_umft601x.xdc`（开发板 FMC）、`adc_clk_mmcm.xdc`、`debug_ila.xdc`（辅助）。
- **多套 TCL**：`scripts/200t/`、`scripts/50t/`、`scripts/te0713/`、`scripts/te0712/`、`scripts/utils/`（ILA 抓取、烧录、CDC 网表等工具脚本）。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**新增 opcode**、**多板卡顶层/XDC**、**TCL 构建流**。最后用「综合实践」把它们串成一个端到端扩展。

### 4.1 新增 opcode：FPGA 与 Python 的同步手术

#### 4.1.1 概念说明

AERIS-10 的所有主机控制都收敛到一张「opcode → `host_*` 寄存器」的映射表。要给系统加一个可配置参数（比如「MTI 滤波器阶数」），本质上就是在这张表里**新增一行**，并让这一行同时存在于三个地方：

1. **FPGA 侧**：`radar_system_top.v` 的 `case(usb_cmd_opcode)` 里加一个分支，把命令 `value` 写进一个新的 `host_*` 寄存器；再把这个寄存器扇出到对应处理模块。
2. **Python 侧**：`radar_protocol.py` 的 `Opcode` 枚举里加一个同编号、同语义的成员。
3. **测试侧**：跨层契约测试（`test_cross_layer_contract.py`）的真值表里登记这个新 opcode，让 CI 守住「三层一致」。

`CONTRIBUTING.md` 把这条规则写成了硬性约定：

> The FPGA RTL (`radar_system_top.v`) is the single source of truth for opcode values, bit widths, reset defaults, and valid ranges. All other layers must align to it.

即 **FPGA RTL 是唯一真值源**，Python 与测试必须向它对齐。这条原则决定了扩展时的修改顺序：先改 FPGA，再让其它层跟上。

#### 4.1.2 核心流程

新增一条 opcode 的标准流程（以「新增 MTI 阶数寄存器 `host_mti_order`，opcode 选 `0x32`」为例）：

```
1. 选一个空闲 opcode 编号
   └─ 查 case 表，避开已用编号（0x01-0x04, 0x10-0x16, 0x20-0x27,
       0x28-0x2C, 0x30-0x31, 0xFF）；0x05-0x0F / 0x17-0x1F / 0x32-0xFE 空闲

2. FPGA: radar_system_top.v 四处改动
   ├─ 声明寄存器        reg [1:0] host_mti_order;        （INTERNAL SIGNALS 段）
   ├─ 复位默认值        host_mti_order <= 2'd2;          （reset 分支）
   ├─ case 译码分支     8'h32: host_mti_order <= usb_cmd_value[1:0];
   └─ 扇出到子模块      .host_mti_order(host_mti_order)  （rx_inst 端口映射，
       再经 radar_receiver_final.v 透传到 mti_canceller）

3. Python: radar_protocol.py 一处改动
   └─ Opcode 枚举加    MTI_ORDER = 0x32

4. GUI: dashboard.py 加一行参数控件（可选但推荐）

5. 测试: 跨层契约测试登记新 opcode；FPGA 回归加 testbench
```

这套流程的精髓是「**同步**」：任何一处漏改，CI 的跨层契约测试都会报错（见 u11-l3）。正因为有这道防线，开发者才敢在一张已经定义了 30 多个 opcode 的表上继续追加。

#### 4.1.3 源码精读

**FPGA 侧的译码表**位于 `radar_system_top.v` 的命令解码 `always` 块里。整张表用一个 `case(usb_cmd_opcode)` 串起所有 opcode，每个分支把 `usb_cmd_value` 的相应位段写入一个 `host_*` 寄存器：

[radar_system_top.v:950-999](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L950-L999) — 命令译码 `case` 表，opcode 到 `host_*` 寄存器的唯一映射真值源。新 opcode 就是在这张表里插一行。

其中与 MTI 直接相关的是这两行（注意位宽切片 `[0]` 只取最低位）：

[radar_system_top.v:985-986](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L985-L986) — `0x26` 写 `host_mti_enable`（1 位），`0x27` 写 `host_dc_notch_width`（3 位）。这告诉我们新寄存器的位宽完全由「切片几位」决定。

寄存器本身在文件上方的 `INTERNAL SIGNALS` 段声明，注释里同时写明了 opcode 编号和默认值，这是 FPGA 侧的「字段说明书」：

[radar_system_top.v:269-271](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L269-L271) — MTI 相关寄存器声明，`host_mti_enable`（opcode 0x26）与 `host_dc_notch_width`（opcode 0x27）。

复位默认值在同一个 `always` 块的 `if (!sys_reset_n)` 分支里集中给出，保证上电行为确定：

[radar_system_top.v:934-936](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L934-L936) — MTI 默认 `enable=0`、`dc_notch_width=0`，即「上电不改变旧行为」的向后兼容策略。

最后一处是扇出：`host_mti_enable` 经由 `rx_inst`（`radar_receiver_final`）的端口映射送进接收链内部的 `mti_canceller`：

[radar_system_top.v:556-557](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L556-L557) — 把 `host_mti_enable` 与 `host_dc_notch_width` 接到接收机的配置端口。

**Python 侧的镜像**是 `Opcode(IntEnum)` 枚举。它的 docstring 直接写明「must match `radar_system_top.v case(usb_cmd_opcode)`」，并附了一张人工转录的对照表：

[radar_protocol.py:53-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103) — Python `Opcode` 枚举，FPGA 译码表的 Python 镜像。MTI 相关成员在 L89-L91。

> ⚠️ **读代码不读注释（再次提醒）**：这个 docstring 里写着「FPGA truth table (from radar_system_top.v lines 902-944)」，但当前 HEAD 的 `case` 表实际从第 950 行才开始。注释会随代码演进而滞后，**真值永远是 RTL 里的 `case` 语句本身**，这也是本讲反复强调的「RTL 是唯一真值源」。

命令拼装函数 `build_command` 把 opcode、addr、value 拼成一个 32 位大端字，新 opcode 一旦登记进枚举就能直接复用这个函数，无需改协议层逻辑：

[radar_protocol.py:168-175](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L168-L175) — `build_command`，opcode 恒为最高字节，与 Verilog 读 FSM 收到的首字节天然对齐。

#### 4.1.4 代码实践

**实践目标**：在不实际改 RTL 的前提下，学会「在译码表里找空闲槽位」并核对三层一致性。

**操作步骤**：

1. 打开 [radar_system_top.v:950-999](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L950-L999)，把所有已出现的 opcode 编号（`8'hXX`）抄下来。
2. 打开 [radar_protocol.py:53-103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_3_GUI/radar_protocol.py#L53-L103)，把 `Opcode` 枚举里所有成员的值抄下来。
3. 列出 `0x00`–`0xFF` 中两边都没用到的编号。
4. 选定一个空闲编号（例如 `0x32`），写出它若用于「2 位 MTI 阶数」时，FPGA case 分支与 Python 枚举成员分别该怎么写。

**需要观察的现象**：

- 两份清单应当**完全一致**——若不一致，说明已经存在跨层失配（这正是跨层契约测试要抓的 bug）。
- 空闲编号是成片出现的：`0x05-0x0F`、`0x17-0x1F`、`0x32-0xFE`。

**预期结果**：

| 检查项 | 预期 |
|--------|------|
| 已用 opcode 两层是否一致 | 完全一致 |
| 选定新编号 | 例如 `0x32`（紧邻 `0x31` 自测试，便于归类） |
| FPGA 分支写法 | `8'h32: host_mti_order <= usb_cmd_value[1:0];` |
| Python 枚举写法 | `MTI_ORDER = 0x32  # 2-pulse / 3-pulse canceller` |

> 本实践为纯源码阅读型，无需运行命令，结论可立即核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 opcode 的编号分配要「成片预留」（例如 `0x28-0x2C` 全给 AGC、`0x21-0x27` 全给 CFAR/MTI），而不是按添加顺序零散占用？

**参考答案**：成片分配让 opcode 自带语义分组，读 `case` 表时一眼就能看出某段属于哪个子系统，也方便后续在该子系统继续追加寄存器而不撞车；零散分配会让相关参数分散在整张表里，维护成本随 opcode 数量平方级上升。

**练习 2**：假设你只在 Python 的 `Opcode` 枚举里加了 `MTI_ORDER = 0x32`，却忘了改 FPGA 的 `case` 表，主机发这条命令时会发生什么？哪一道 CI 测试会先报警？

**参考答案**：FPGA 的 `case` 表没有 `0x32` 分支，会落入 `default: ;`（什么都不做），于是命令被静默丢弃、`host_mti_order` 永不更新——这是典型的「两层都编译通过却行为错误」的隐性 bug。`cross-layer-tests` job 里的 `test_cross_layer_contract.py` 会最先报警，因为它用独立推导的真值去比对每一层（见 u11-l3），能发现「Python 有而 Verilog 无」的不一致。

---

### 4.2 多板卡顶层/XDC：同一份 RTL，多块板卡

#### 4.2.1 概念说明

AERIS-10 要在多种 FPGA 板卡上运行：50T 量产板（XC7A50T，配 FT2232H USB 2.0）、200T 高端板（XC7A200T，配 FT601 USB 3.0）、以及 TE0713/TE0712 开发板（用于 bring-up，见 u10-l2）。如果为每块板维护一份完整 RTL 副本，代码会迅速分叉、bug 修不过来。

仓库的解法是「**一份逻辑核心 + 参数化切换 + 开发板薄包装**」三层结构：

1. **逻辑核心** `radar_system_top.v` 用 `parameter USB_MODE` 在 FT601 与 FT2232H 两套 USB 模块间做编译期二选一（详见 u3-l1）。
2. **量产物理包装**（如 `radar_system_top_50t.v`）为核心补上某块板特有的引脚与例化细节。
3. **开发板薄包装**（如 `radar_system_top_te0713_umft601x_dev.v`）只为 bring-up 服务，用计数器产生合成数据，验证 USB 链路本身，不跑完整雷达链。

而 **XDC 约束文件**则负责把顶层端口绑定到具体封装引脚——同一份 RTL 的端口名在不同板上对应不同的物理焊盘，所以每块板需要自己的 XDC。

#### 4.2.2 核心流程

一块新板卡从「能综合」到「能上电」需要三件配套：

```
新板卡适配三件套
├─ 顶层        radar_system_top_<board>.v   （端口 = 该板物理引脚）
├─ 约束        constraints/<board>.xdc      （端口 → 封装引脚 + 电平 + 时序）
└─ 构建        scripts/<board>/build_<board>.tcl  （指定 part、加源、加约束）
```

XDC 里最核心的三类约束：

- **引脚绑定** `set_property PACKAGE_PIN <焊盘> [get_ports {<端口>}]`：把 RTL 信号钉到 FPGA 封装的某个焊盘上。
- **电平标准** `set_property IOSTANDARD LVCMOS33 ...`：决定该 bank 的 I/O 电压与驱动协议。
- **时序** `create_clock`、`set_input_delay`、`set_max_delay`、`set_false_path`：告诉综合工具时钟周期与哪些路径不必苛求时序。

#### 4.2.3 源码精读

**参数化 USB 切换**是「一份 RTL 适配多板」的关键。`radar_system_top.v` 用一个 `generate` 块，在综合时根据 `USB_MODE` 只例化其中一个 USB 模块，另一个分支里把未用引脚 `tieoff`：

[radar_system_top.v:718-719](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L718-L719) — `USB_MODE==0` 走 FT601（32 位，200T 高端板）。

[radar_system_top.v:792-793](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L792-L793) — `else` 分支走 FT2232H（8 位，50T 量产板，也是 RTL 默认值）。

[radar_system_top.v:853-862](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L853-L862) — FT2232H 模式下，把 FT601 专用引脚 tieoff 到安全电平，避免悬空。换板只需改一个参数，不必碰这部分代码。

`USB_MODE` 的默认值在参数声明处，注释写清了两块板的对应关系：

[radar_system_top.v:145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145) — `parameter USB_MODE = 1`，默认 FT2232H/50T；200T 构建脚本会把它覆盖为 0。

**XDC 引脚绑定范例**：以 TE0713 开发板经 FMC LPC 接 UMFT601X 子卡的约束为例，每条信号都标注了「FMC LA 信号名 → FPGA 焊盘」的映射链路，便于核对原理图：

[te0713_te0701_umft601x.xdc:34-37](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc#L34-L37) — FT601 时钟输入绑定到 `J20`，电平 `LVCMOS33`，并声明 10 ns 周期时钟。

[te0713_te0701_umft601x.xdc:44-110](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc#L44-L110) — FT601 32 位数据总线逐位绑定，`SLEW FAST` + `DRIVE 8` 保证 100 MHz FIFO 时序。

这个 XDC 还示范了一个高级技巧——当某个生产 RTL 端口在开发板上没有对应引脚时，用 `set_false_path` 把它排除出时序分析：

[te0713_te0701_umft601x.xdc:299-302](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc#L299-L302) — `ft601_chip_reset_n`、`wakeup_n`、`gpio0/1` 不参与时序收敛。

**开发板薄包装顶层**示范了「如何用一个最小顶层只验证 USB 链路」。它不复用 `radar_system_top.v`，而是直接例化 `usb_data_interface`，用计数器造数据，把生产 RTL 里开发板没有的端口（`ft601_srb/swb`、`ft601_txe_n/rxf_n`）按 XDC 末尾的 RECOMMENDED 说明做 tieoff：

[radar_system_top_te0713_umft601x_dev.v:97-121](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top_te0713_umft601x_dev.v#L97-L121) — 开发板顶层例化 `usb_data_interface`，`ft601_srb/swb` 接 `2'b00`，`ft601_clk_out/txe_n/rxf_n` 接 unused，正是 XDC NOTES 段建议的「薄 FMC wrapper」写法。

这种「逻辑核心 + 板级包装」的分层，让 bring-up 阶段能先把 USB 这一段单独跑通（u10-l2 的「先心跳后 FT601」策略），再逐步接入完整雷达链。

#### 4.2.4 代码实践

**实践目标**：建立「端口名 → 焊盘 → 物理信号」的三级对应直觉。

**操作步骤**：

1. 打开 [te0713_te0701_umft601x.xdc:44-58](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/constraints/te0713_te0701_umft601x.xdc#L44-L58)，挑出 `ft601_data[0]` 这一条。
2. 顺着注释追溯三级映射：RTL 端口 `ft601_data[0]` → FMC 信号 `LA32_N` → FPGA 焊盘 `L21` → 物理上经 TE0713 B2B 连接器到 TE0701 载板再到 UMFT601X-B 的 FT601 芯片。
3. 回到 [radar_system_top.v:83](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L83) 确认 `ft601_data` 在顶层确实声明为 `inout wire [31:0]`。
4. 思考：如果换成 50T 量产板，同一个 `ft601_data` 端口会不会被使用？为什么？

**需要观察的现象**：

- 一条数据线要穿越「FPGA 焊盘 → 板间连接器 → FMC → 子卡」四级物理介质，XDC 只管第一级（焊盘），后三级由原理图保证。
- 50T 量产板用 FT2232H（8 位 `ft_data`），`USB_MODE=1` 时 `ft601_data` 这组 32 位端口会被 tieoff（见 4.2.3），所以它在 50T XDC 里根本不出现。

**预期结果**：你能用一句话讲清「为什么同一份 RTL 的端口在不同板的 XDC 里映射不同」——因为物理焊盘位置和可用 I/O bank 随封装而变，RTL 只描述逻辑连接，XDC 才描述物理落地。

> 本实践为源码阅读型，结论可直接从注释与端口声明核对。

#### 4.2.5 小练习与答案

**练习 1**：开发板包装顶层 `radar_system_top_te0713_umft601x_dev.v` 里，状态回读字段被硬编码成固定值（如 `status_self_test_flags(5'b11111)`、`status_range_mode(2'b01)`）。为什么这样做是安全的，反而有助于 bring-up？

**参考答案**：开发板包装层的目的不是跑雷达，而是验证「USB 链路通、命令协议对、主机能收到正确的包结构」。把状态字段塞成可识别的固定模式（如 `5'b11111`、`0xA5`），主机收到后一眼就能确认「字节序、位域、包长度都对」，相当于一个回环探针。真值留待量产顶层接入真实数据后再产生。

**练习 2**：如果你要为一块全新的「USB 用 FT232H（比 FT2232H 更简单的单通道芯片）」板卡做适配，按本节三件套框架，你需要新增/修改哪些文件？

**参考答案**：新增 `usb_data_interface_ft232h.v`（新 USB 模块）、在 `radar_system_top.v` 的 `generate` 块加一个 `USB_MODE==2` 分支（或新建一个 `radar_system_top_<新板>.v` 包装层）、新增 `constraints/<新板>.xdc`（FT232H 引脚约束）、新增 `scripts/<新板>/build_<新板>.tcl`。核心雷达链 RTL 一行都不用改——这正是分层架构的回报。

---

### 4.3 TCL 构建流：从 RTL 到比特流的自动化

#### 4.3.1 概念说明

Vivado 是 Xilinx 7 系列 FPGA（AERIS-10 用的 Artix-7）的官方综合工具。它既可以用图形界面点鼠标跑流程，也可以用 **TCL（Tool Command Language）脚本**做批处理。本仓库的所有构建都用 TCL 脚本，原因有三：

- **可复现**：脚本即文档，任何人执行同样命令得到同样结果，不依赖某台机器的 GUI 状态。
- **CI 友好**：CI 只需 `vivado -mode batch -source xxx.tcl`，无需显示器。
- **参数化**：TCL 可以在命令行覆盖 Verilog 参数（如 `USB_MODE`），一份脚本编译出多个变体。

一个 Vivado 批处理构建的标准五步流水线是：

1. **create_project** + **add_files**：建工程、加 RTL 源、加 .mem（BRAM 初始化）、加 XDC 约束。
2. **synth_1**（综合）：把 RTL 翻译成逻辑门级网表。
3. **impl_1**（实现）：布局布线，把网表映射到具体 FPGA 资源（LUT/FF/BRAM/DSP）。
4. **write_bitstream**：生成可烧录的 `.bit` 比特流文件。
5. **report_***：输出时序、资源、CDC、功耗等报告，供 sign-off 判定。

#### 4.3.2 核心流程

构建脚本之间的关键差异，可以用一张表概括：

| 脚本 | 目标 part | 顶层 | USB_MODE | 用途 |
|------|-----------|------|----------|------|
| `scripts/200t/build_200t.tcl` | `xc7a200tfbg484-2` | `radar_system_top` | `0`（覆盖默认） | 200T 量产，FT601 |
| `scripts/50t/build_50t.tcl` | `xc7a50tftg256` | （50T 包装） | `1`（RTL 默认） | 50T 量产，FT2232H |
| `scripts/te0713/build_te0713_umft601x_dev.tcl` | `xc7a200tfbg484-2` | `radar_system_top_te0713_umft601x_dev` | —（开发板包装层不暴露该参数） | bring-up，只验 USB |

构建完成后的 sign-off 判据是**时序三件套**：

\[ \text{WNS} \geq 0 \quad \text{且} \quad \text{WHS} \geq 0 \quad \text{且} \quad \text{TNS} = 0 \]

其中 WNS（最差建立余量）和 WHS（最差保持余量）必须非负，TNS（总负余量）必须为 0，三者全满足才算时序收敛、允许生成比特流。

#### 4.3.3 源码精读

**参数化构建的关键一行**在 200T 量产脚本里。RTL 默认 `USB_MODE=1`（FT2232H/50T），但 200T 板要用 FT601，于是脚本用 `set_property generic` 在综合时把参数覆盖为 0：

[build_200t.tcl:110-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl#L110-L113) — 通过 `set_property generic {USB_MODE=0}` 让 4.2 节的 `generate` 块在综合时选择 FT601 分支。这是「一份 RTL 适配多板」在构建侧的落点。

同一个脚本还示范了如何把 RTL 文件**显式列清单**加入工程（而不是 `add_files [glob *.v]` 全收），这样能精确控制哪些模块进综合、避免把 testbench 误纳入：

[build_200t.tcl:54-85](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl#L54-L85) — RTL 文件清单，包含 `radar_system_top.v` 及全部子模块（含 `mti_canceller.v`、`cfar_ca.v`、`fpga_self_test.v`）。新增子模块时，要把它追加进这张清单。

[build_200t.tcl:105-107](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl#L105-L107) — 加载主 XDC 与 MMCM 辅助 XDC。

构建末尾的 sign-off 逻辑直接实现了上一节的时序三件套判定，并用 `catch` 包裹可能在新版 Vivado 失败的报告命令：

[build_200t.tcl:383-419](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl#L383-L419) — 自动判定 WNS/WHS/TNS/未布线网线/比特流是否齐全，输出 `SIGNOFF: PASS/FAIL`。

**开发板构建脚本**则短得多，因为它只验 USB、不跑雷达链，源码与报告都精简：

[build_te0713_umft601x_dev.tcl:12-27](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl#L12-L27) — 只加开发板顶层 + `usb_data_interface.v` + 一个 XDC，设 `top` 为开发板包装模块。

[build_te0713_umft601x_dev.tcl:29-34](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/te0713/build_te0713_umft601x_dev.tcl#L29-L34) — 用 `Performance_ExplorePostRoutePhysOpt` 策略跑到 `write_bitstream`，是开发板快速迭代的常用配置。

两个脚本对比能看出一个重要原则：**构建脚本要与顶层匹配**。复杂顶层（200T 量产）需要长清单、多 XDC、详尽报告；简单顶层（开发板）只需最小集合。盲目复用复杂脚本会拖慢开发板迭代。

#### 4.3.4 代码实践

**实践目标**：读懂一个 TCL 构建脚本，能在不改 RTL 的前提下复现一次构建配置。

**操作步骤**：

1. 打开 [build_200t.tcl](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl)，定位 5 个阶段对应的 Tcl 命令：`create_project`、`launch_runs synth_1`、`launch_runs impl_1`、`write_bitstream`、`report_*`。
2. 找到 `USB_MODE` 是怎么被覆盖的（L113），并与 [radar_system_top.v:145](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/radar_system_top.v#L145) 的默认值对比，确认「脚本覆盖 RTL 默认」这一关系。
3. 找到 RTL 文件清单（L54-L85），确认 `mti_canceller.v` 在列——这是综合能找到该模块的前提。
4. （本地可选）若已装 Vivado 与 TE0713 板，在仓库根 `9_Firmware/9_2_FPGA/` 下执行：
   ```bash
   vivado -mode batch -source scripts/te0713/build_te0713_umft601x_dev.tcl \
       -log build/build.log -journal build/build.jou
   ```

**需要观察的现象**：

- TCL 用 `[file dirname [file normalize [info script]]]` 求脚本自身所在目录，再 `../..` 回到 FPGA 根——所以脚本不依赖你在哪个目录执行，路径都正确。
- 报告统一写到 `reports/` 或 `reports_buildNN/` 子目录，正是 u1-l2 讲过的「生成产物不进版本库」策略。

**预期结果**：你能解释「为什么改了 `USB_MODE` 默认值不用改 200T 脚本、却可能影响 50T 脚本」——因为 200T 脚本显式覆盖了该参数，50T 脚本依赖 RTL 默认值。

> 本地无 Vivado 时，前 3 步为源码阅读型；第 4 步若无环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`build_200t.tcl` 为什么要把 RTL 文件一个一个列进 `$rtl_files` 清单，而不是用 `add_files [glob *.v]` 一次性全加？

**参考答案**：仓库的 `9_2_FPGA/` 目录里同时有量产 RTL、testbench（`tb/`）、formal 包装（`formal/`）、开发板顶层等多种 `.v` 文件。全收会把 testbench 和 formal wrapper 误纳入综合，导致端口冲突或综合出多余顶层。显式清单精确控制综合范围，也便于 code review 时一眼看出「这次构建包含哪些模块」。

**练习 2**：sign-off 判据里，WNS 和 TNS 有什么区别？为什么两者都要查？

**参考答案**：WNS 是所有路径里**最差一条**的建立时间余量，反映「最险路径是否过关」；TNS 是**所有违规路径的负余量之和**，反映「违规的总量有多大」。一条路径违规（WNS<0）但 TNS 仍可能接近 0（违规很小），反之少量路径大违规会让 TNS 很负。工程上两者都查：WNS 给「最差路径定位」，TNS 给「整体健康度」。

---

## 5. 综合实践

### 设计一个端到端功能扩展：主机可配置 MTI 滤波器阶数

**任务背景**：当前 MTI 是固定的二脉冲对消器 \( H(z)=1-z^{-1} \)（见 u4-l3 与 [mti_canceller.v](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_2_FPGA/mti_canceller.v)），只能通过 `0x26` 开关。现在要让主机还能选择「二脉冲」或「三脉冲」对消。三脉冲对消器的传递函数为：

\[ H_3(z) = (1-z^{-1})^2 = 1 - 2z^{-1} + z^{-2} \]

它在零多普勒处有二阶零点（杂波凹口更深更窄），适合强地杂波场景，代价是慢时间上需要两拍历史、且首两个 chirp 无输出。

**你的交付物**：一份「同步改动位置清单」，把本讲三个最小模块的知识用上。按下表逐项填出**文件、行号锚点、改动内容**。参考答案在表后。

| # | 文件 | 锚点（参考行号） | 改动内容 |
|---|------|------------------|----------|
| 1 | `radar_system_top.v` | L269-L271（寄存器声明段） | ？ |
| 2 | `radar_system_top.v` | L934-L936（复位默认值段） | ？ |
| 3 | `radar_system_top.v` | L985-L986（case 译码表） | ？ |
| 4 | `radar_system_top.v` | L556-L557（rx_inst 端口扇出） | ？ |
| 5 | `radar_receiver_final.v` | 透传 `host_mti_order` 到 MTI 实例的端口 | ？ |
| 6 | `mti_canceller.v` | L58（`mti_enable` 输入旁） | ？ |
| 7 | `mti_canceller.v` | L70-L156（历史缓冲与减法） | ？ |
| 8 | `radar_protocol.py` | L89-L91（`MTI_ENABLE=0x26` 旁） | ？ |
| 9 | `v7/dashboard.py` | L669-L674（`sp_params` 列表） | ？ |
| 10 | `9_Firmware/9_2_FPGA/scripts/200t/build_200t.tcl` | L54-L85（RTL 清单） | 无需改（`mti_canceller.v` 已在列），仅核对 |
| 11 | `9_Firmware/tests/cross_layer/test_cross_layer_contract.py` | opcode 真值表 | ？ |
| 12 | `9_Firmware/9_2_FPGA/tb/` | MTI 相关 testbench | ？ |

### 参考答案（位置清单）

1. **声明新寄存器**：在 `host_mti_enable` 旁加 `reg [1:0] host_mti_order;  // Opcode 0x32: 2=2-pulse, 3=3-pulse`（2 位足够编码阶数，也可只用 1 位做开关式扩展）。
2. **复位默认值**：`host_mti_order <= 2'd2;`（默认二脉冲，保持旧行为不变——这是「向后兼容」铁律）。
3. **case 译码**：选空闲 opcode `0x32`，加 `8'h32: host_mti_order <= usb_cmd_value[1:0];`。
4. **扇出**：在 `rx_inst` 端口映射加 `.host_mti_order(host_mti_order)`。
5. **接收机透传**：在 `radar_receiver_final.v` 加输入端口 `host_mti_order`，并在其内部 `mti_canceller` 实例上接出。
6. **MTI 模块新端口**：在 `mti_canceller.v` L58 旁加 `input wire [1:0] mti_order,`。
7. **算法扩展**：增加第二组历史缓冲（`prev2_i/prev2_q`），在 `mti_order==3` 时计算 `current - 2*prev1 + prev2`（带饱和），`mti_order==2` 时维持现有 `current - prev1`；首两个 chirp 静音。
8. **Python 枚举**：加 `MTI_ORDER = 0x32  # 2-pulse (default) / 3-pulse canceller`。
9. **GUI 控件**：在 `sp_params` 加一行 `("MTI Order", 0x32, 2, 2, "2=2-pulse, 3=3-pulse")`，复用现有 `_add_fpga_param_row` 即可自动生成带「发送」按钮的控件。
10. **构建脚本**：无需改动，`mti_canceller.v` 已在清单内（L78）。
11. **跨层契约测试**：在 `test_cross_layer_contract.py` 的 `GROUND_TRUTH` opcode 集合里登记 `0x32 → host_mti_order`，让三层一致性校验覆盖新命令。
12. **FPGA 回归**：在 `tb/` 加一个 `tb_mti_order.v`，用合成数据分别测 `mti_order=2` 与 `=3` 的凹口深度（三脉冲应在零多普勒衰减更多），并加入 `run_regression.sh` 的信号处理阶段（见 u11-l1）。

**自检**：完成后按 `CONTRIBUTING.md` 的 CI 清单跑四遍——`uv run ruff check .`、`run_regression.sh`、`make test`、`pytest test_cross_layer_contract.py`——全绿才能提 PR（PR 目标分支是 `develop`，不是 `main`）。

> 本实践为「源码阅读 + 设计」型，不要求实际运行；若要落地，建议先在 iverilog 上单独仿真 `mti_canceller` 的三脉冲行为，再接入全链。

---

## 6. 本讲小结

- **opcode 是跨层硬契约**：新增主机命令必须在 FPGA 的 `case` 译码表、Python 的 `Opcode` 枚举、跨层契约测试三处同步修改，FPGA RTL 是唯一真值源。
- **一次 opcode 扩展在顶层有四个改动点**：寄存器声明、复位默认值、case 分支、子模块端口扇出——缺一不可。
- **多板卡靠三件套适配**：板级包装顶层（`radar_system_top_<board>.v`）、引脚约束（`constraints/<board>.xdc`）、构建脚本（`scripts/<board>/build_<board>.tcl`），核心 RTL 不为每块板复制。
- **`USB_MODE` 是参数化构建的核心**：一份 `radar_system_top.v` 用 `generate` 在 FT601/FT2232H 间二选一，构建脚本用 `set_property generic {USB_MODE=N}` 在综合时覆盖默认值。
- **TCL 构建脚本是可复现的流水线**：建工程→综合→实现→比特流→报告，sign-off 判据是 WNS≥0、WHS≥0、TNS=0 三件套同时满足。
- **改完必跑四套 CI**：Python lint/test、FPGA 回归、MCU 单测、跨层契约——这是 `CONTRIBUTING.md` 的硬性合并门禁。

---

## 7. 下一步学习建议

本讲是学习手册的最后一篇讲义之一，至此你已读完整套从微波到上位机的学习路径。接下来建议：

1. **动手做综合实践**：把第 5 节的「MTI 阶数可配置」从设计清单真正落成代码，这是检验你是否掌握全栈扩展的最好题目。可以先只做 FPGA + Python 两层，跑通 iverilog 仿真与跨层契约测试，再补 GUI。
2. **重读 u11（测试与验证体系）**：扩展功能的难点不在写新代码，而在「证明它没改坏旧的」。把 u11-l1 的真实数据 cosim、u11-l3 的跨层契约测试真正跑一遍，建立对 CI 防线的信心。
3. **通读 `CONTRIBUTING.md` 全文**：它是这个项目所有协作约定的浓缩，尤其注意「NO LEGACY COMPATIBILITY」「RTL 是唯一真值源」「对抗性测试强制」三条铁律，它们决定了你的 PR 能否被合并。
4. **横向对照 u14-l1（形式化验证）**：如果你新增的状态机或 CDC 路径有死锁/越界风险，仿照 `formal/fv_*.v` 的写法给它补一个 `.sby` 形式化验证，让「算得对」和「不卡死」双重闭环。
5. **贡献回上游**：在 `develop` 分支开 topic branch，按本讲的同步清单与 CI 门禁完成自检后，向仓库提交 PR——把你的第一个 opcode 留在 AERIS-10 的真值表里。
