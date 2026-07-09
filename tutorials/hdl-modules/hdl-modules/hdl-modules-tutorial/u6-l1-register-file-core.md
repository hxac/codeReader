# 通用寄存器文件核心

## 1. 本讲目标

本讲承接 [u5-l4（AXI-Lite 子系统）](u5-l4-axi-lite-subsystem.md)，把目光收束到那套总线的终点：**寄存器文件本身**。u5-l4 已经把 CPU 一路接到 `axi_lite_register_file` 的门口（`axi_to_axi_lite` → `axi_lite_mux` → `axi_lite_cdc`），却把「寄存器文件长什么样、怎么定义、怎么读写」留给了本讲。

学完本讲，你应当能够：

- 说清 `register_file_pkg` 中 `register_mode_t` 的五种模式（`r` / `w` / `r_w` / `wpulse` / `r_wpulse`）各自的**软件侧**与**硬件侧**语义。
- 读懂 `axi_lite_register_file` 如何用一份「寄存器清单」generic 把任意寄存器阵列挂到 AXI-Lite 总线上，并说清读、写两条状态机与 `regs_up` / `regs_down` 数据流向。
- 解释 `interrupt_register` 如何用「粘滞 status + mask + clear」三件套聚合出单比特中断 `trigger`，并理解「常驻中断源被清后立即重触发」的边沿语义。
- 把这三个构建块串成一个最小的「状态 + 控制」子系统，并用 BFM 验证。

## 2. 前置知识

本讲默认你已经掌握以下概念（未掌握的可先读对应讲义）：

- **AXI-Lite 总线**（[u5-l4](u5-l4-axi-lite-subsystem.md)）：单拍事务、数据位宽 32 或 64、五条通道（AR/R/AW/W/B）、四种响应码 `OKAY`/`EXOKAY`/`SLVERR`/`DECERR`、`axi_lite_m2s_t`/`axi_lite_s2m_t` 记录类型。
- **ready/valid 握手**（[u2-l1](u2-l1-handshake-convention.md)）：`valid` 与 `ready` 同拍同 1 才完成一次 beat。
- **VHDL-2008 与 record**、**generic 参数化**、**VUnit 测试台自检模式**（[u8-l2](u8-l2-vunit-testbench-patterns.md)）。

几个本讲会用到的术语：

- **寄存器文件（register file）**：一段可被 CPU 按地址读写的存储阵列，每一项是一个 32 位寄存器，是「软件控制硬件、硬件上报状态」的标准接口。
- **fabric / 应用逻辑（application）**：寄存器文件之外的用户电路。本讲用「硬件侧」「fabric」指代它。
- **粘滞位（sticky bit）**：一旦置 1 就保持 1，直到被显式清零。中断状态位通常是粘滞的。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `modules/register_file/` 下：

| 文件 | 类别 | 作用 |
| --- | --- | --- |
| [src/register_file_pkg.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd) | 可综合 | 定义寄存器宽度、`register_mode_t` 模式枚举、`register_definition_t` 清单类型，以及一组在精化期求值的查询函数。是整个寄存器生态的「字典」。 |
| [src/axi_lite_register_file.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd) | 可综合 | 本讲主角：通用、可参数化的寄存器文件实体，把寄存器阵列挂到 AXI-Lite 总线上。 |
| [src/interrupt_register.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/interrupt_register.vhd) | 可综合 | 产生粘滞中断位的实体，常作为某个 `r` 寄存器的状态来源。 |
| [test/tb_axi_lite_register_file.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_axi_lite_register_file.vhd) | 仿真 | 用 AXI-Lite master BFM 驱动 DUT 的随机化自检测试台，是本讲实践的样板。 |
| [test/tb_interrupt_register.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_interrupt_register.vhd) | 仿真 | 验证中断粘滞、清除、边沿重触发的测试台。 |
| [rtl/axi_lite_register_file_netlist_build_wrapper.vhd](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/rtl/axi_lite_register_file_netlist_build_wrapper.vhd) | 综合 | netlist 资源回归用的顶层夹具，手工给出一份覆盖五种模式的 `regs` 清单，是「手写寄存器清单」的现实例子。 |
| [module_register_file.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/module_register_file.py) | 工具 | 把 `axi_lite_register_file` 与 `interrupt_register` 的资源占用纳入 CI 回归（详见 [u8-l3](u8-l3-resource-utilization-regression.md)）。 |

> 真实设计里，`registers` 与 `default_values` 这两个 generic 通常**不是手写**的，而是由外部工具 `hdl-registers` 从一份 toml 描述生成（实体的源码注释明确指出这一点）。手写清单只出现在测试与本模块自己的 netlist 夹具里。这条「生成链」会在 [u7-l3](u7-l3-dma-registers-and-cpp-driver.md) 与 [u9-l1](u9-l1-documentation-generation.md) 展开，本讲先聚焦「实体本身如何工作」。

## 4. 核心概念与源码讲解

### 4.1 register_file_pkg：寄存器类型与访问模式定义

#### 4.1.1 概念说明

寄存器文件的核心抽象是：**每个寄存器都有一种「访问模式」，模式决定了软件能不能读、能不能写、写下去的值在硬件侧以什么形式出现**。`register_file_pkg` 就是这套抽象的「字典」——它不描述任何具体电路，只定义类型、常量和一组纯查询函数，供下游实体（`axi_lite_register_file`、`interrupt_register`，以及 hdl-registers 生成的代码）统一引用。

为什么要把「模式」做成枚举而不是一堆布尔 generic？因为模式之间是**互斥且语义自洽**的：一个寄存器要么是「只读」要么是「读+写脉冲」，组合有限。用枚举可以在精化期用 `assert` 与查询函数把非法配置挡在综合之前，也让人一眼读懂寄存器的用途。

#### 4.1.2 核心流程

包里定义了一条「模式 → 四个布尔属性」的映射，下游实体据此决定如何接线：

| 模式 | 软件可读？`is_read_mode` | 软件可写？`is_write_mode` | 写为单周期脉冲？`is_write_pulse_mode` | 读值由 fabric 提供？`is_application_gives_value_mode` |
| --- | :---: | :---: | :---: | :---: |
| `r` | ✅ | ❌ | ❌ | ✅（读硬件状态） |
| `w` | ❌ | ✅ | ❌ | ❌（写值给硬件用） |
| `r_w` | ✅ | ✅ | ❌ | ❌（读=回读写的值） |
| `wpulse` | ❌ | ✅ | ✅ | ❌ |
| `r_wpulse` | ✅ | ✅ | ✅ | ✅（读硬件状态 + 写脉冲） |

读这张表的方法：

- `is_read_mode` 为真 ⇒ 总线能读它；读值要么来自 fabric（`is_application_gives_value_mode` 为真，如 `r`/`r_wpulse`），要么是「写下去的值的回环」（如 `r_w`）。
- `is_write_mode` 为真 ⇒ 总线能写它；写下去的值会出现在 `regs_down` 给 fabric 用。
- `is_write_pulse_mode` 为真 ⇒ 写值在 fabric 侧只亮一拍（脉冲），用于「触发」「启动」这类一次性动作。

地址译码所需的位数也由这个包算出：寄存器宽度恒为 32 位 = 4 字节，故地址最低 2 位是字节偏移，丢弃；剩余的索引位数 \(n\) 取决于寄存器个数：

\[ n = \lceil \log_2(N) \rceil, \quad N = \text{max\_index} + 1 \]

寄存器索引 \(i\) 的字节地址为 \(4i\)，从地址取索引用 `addr[n+1 : 2]`。

#### 4.1.3 源码精读

先看宽度与基础类型，寄存器一律是 32 位：

[modules/register_file/src/register_file_pkg.vhd:20-23](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd#L20-L23) —— 定义 `register_width=32`、`register_t` 子类型与 `register_vec_t` 数组类型（`regs_up`/`regs_down`/`default_values` 都用它）。

接着是本包的灵魂——五种访问模式的枚举，每条注释就是它的「人设」：

[modules/register_file/src/register_file_pkg.vhd:25-38](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd#L25-L38) —— `register_mode_t`，`r`（软件读硬件给的值）/ `w`（软件写给硬件用）/ `r_w`（读写回环）/ `wpulse`（软件写一拍脉冲）/ `r_wpulse`（读硬件值 + 写脉冲）。

四个查询函数把枚举翻译成布尔属性，逻辑极简但正是下游接线所依赖的「合同」：

[modules/register_file/src/register_file_pkg.vhd:74-92](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd#L74-L92) —— `is_read_mode`/`is_write_mode`/`is_write_pulse_mode`/`is_application_gives_value_mode` 的实现，逐字对应 4.1.2 的表。

单个寄存器的「元数据」用 record 描述：它在清单里的下标、模式、实际用到多少位（`utilized_width`，未用的位实现可忽略，避免误导）：

[modules/register_file/src/register_file_pkg.vhd:52-61](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd#L52-L61) —— `register_definition_t` 及其数组类型 `register_definition_vec_t`。整个 `axi_lite_register_file` 就被这一个数组参数化。

最后是地址位数计算。注意函数自带两条 `assert`：清单必须从下标 0 开始、且下标连续递增，否则精化期直接 `failure`：

[modules/register_file/src/register_file_pkg.vhd:94-109](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/register_file_pkg.vhd#L94-L109) —— `get_highest_index` 校验清单完整性；`num_address_bits_needed` 用 `ceil(log2(max_index+1))` 算索引位数（注释提醒：不含低 2 位对齐位）。特判 `max_index=0` 时返回 1，避免 `log2(1)=0` 退化。

> 真实手写清单长什么样？看 netlist 夹具里这份覆盖全部五种模式、`utilized_width` 各异的例子：[modules/register_file/rtl/axi_lite_register_file_netlist_build_wrapper.vhd:43-77](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/rtl/axi_lite_register_file_netlist_build_wrapper.vhd#L43-L77)。注释里还顺手标了「Sum of utilized_widths: 268」——这是资源估算的速记。

#### 4.1.4 代码实践（源码阅读型 + 精化期验证）

**实践目标**：验证你真的读懂了模式表，而不是靠猜。

**操作步骤**：

1. 打开 `register_file_pkg.vhd` 的四个查询函数（L74–L92）。
2. 对下表每一行，**先合上源码**，凭直觉填出四个布尔值；再打开源码核对。
3. 选一个手写清单（如上面的 netlist 夹具 L43–L59，共 15 个寄存器），用公式 \(n=\lceil\log_2(15)\rceil=4\) 手算索引位数，再用地址 \(4\times 14=56=0x38\) 验证 `[5:2]` 取出的是 `1110`=14。

**需要观察的现象**：你预测的布尔表应当与源码逐一吻合；`max_index=14` 时 `num_address_bits_needed` 应返回 4。

**预期结果**：五条模式行全部正确，地址位数算得 4。如果你的预测与源码不符，回去重读 4.1.2 的表与模式枚举注释。

> 本实践为源码阅读型，无需运行仿真；若想在精化期动态验证，可在一个 VUnit testbench 里 `report boolean'image(is_read_mode(r_w));` 打印结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `r_w` 的 `is_application_gives_value_mode` 是假，而 `r` 是真？

> **答案**：`r` 模式软件读到的值由 fabric 通过 `regs_up` 提供（硬件状态）；`r_w` 模式软件读到的值是它自己刚才写下去的值（回环），不来自 fabric，所以为假。

**练习 2**：若一份寄存器清单的下标写成 `0,1,2,4`（跳过 3），精化期会发生什么？

> **答案**：`get_highest_index` 里的 `assert registers(idx).index = idx` 类约束会触发 `severity failure`，精化期直接报错终止。清单必须从 0 起且连续。

**练习 3**：`wpulse` 与 `r_wpulse` 都满足 `is_write_pulse_mode`，二者差别在哪？

> **答案**：`wpulse` 软件不可读（纯写脉冲）；`r_wpulse` 软件可读，且读值来自 fabric（`is_application_gives_value_mode` 为真），即「既能看硬件状态，又能写一个脉冲去触发动作」。

---

### 4.2 axi_lite_register_file：把寄存器阵列挂到 AXI-Lite 总线

#### 4.2.1 概念说明

有了「字典」，下一步是真正能被 CPU 读写的东西。`axi_lite_register_file` 是一个**通用、可参数化**的实体：你只给它两样东西——一份寄存器清单 `registers`（每个寄存器的下标、模式、用到的位宽）和一份默认值 `default_values`——它就能自动生成一个完整的 AXI-Lite slave，负责地址译码、读/写握手、错误响应，并把每个寄存器的值在「总线侧」与「fabric 侧」之间搬运。

它对外暴露四组关键信号，理解了它们就理解了实体：

- `axi_lite_m2s` / `axi_lite_s2m`：AXI-Lite slave 端口（CPU 进、响应出）。
- `regs_up`：**fabric → 总线**。硬件要给软件「看」的值（`r`、`r_wpulse` 模式寄存器的读值）从这里喂进来。
- `regs_down`：**总线 → fabric**。软件写下去的值（`w`/`r_w`/`wpulse`/`r_wpulse` 模式）从这里给硬件用。
- `reg_was_read` / `reg_was_written`：每比特对应一个寄存器，在该寄存器被读/写的那一拍（写是次拍）脉冲一周期，供 fabric 做「副作用触发」（如「软件读了状态寄存器就清中断」）。

实体还自带完善的错误处理与复位策略（源码顶部注释有专门两节），不需用户操心。

#### 4.2.2 核心流程

实体内部分三个并发块，互不依赖：

**① `assign_down`（组合）**：对每个可写寄存器，把内部存储 `reg_values[idx]` 的「用到的那几位」驱动到 `regs_down[idx]`。只写 `utilized_width` 位，避免未用位产生歧义。

**② `read_block`（读通道状态机 + 组合数据选择）**：

```
状态 ar：拉高 AR ready；
        若 AR 握手 → 锁存地址为 read_index，转 r
状态 r ：拉高 R valid；
        若 R 握手 → 转 ar
（组合 set_status：遍历清单，命中 read_index 且可读的寄存器
   → resp=OKAY、reg_was_read 脉冲、r.data 取 regs_up 或 reg_values）
```

读数据的来源由 `is_application_gives_value_mode` 决定：为真取 `regs_up`（fabric 状态），为假取 `reg_values`（写值回环，即 `r_w`）。默认 `resp=SLVERR`、`data=0`，所以**地址不命中或读了只写寄存器，自动回 SLVERR**。

**③ `write_block`（写通道状态机 + 时序锁存）**：

```
状态 aw：拉高 AW ready；
        若 AW 握手 → 锁存地址为 write_index，拉高 W ready，转 w
状态 w ：若 W 握手 → 锁存 w.data 进 reg_values[idx]，拉高 B valid，转 b
状态 b ：若 B 握手 → 转 aw
（时序 set_status：默认 b.resp=SLVERR；
   命中 write_index 且可写 → resp=OKAY、reg_was_written 次拍脉冲；
   写脉冲模式每拍先把 reg_values 复位到 default，再在 W 握手拍写入 → 净效果是单周期脉冲）
```

写状态机刻意保留为「串行三段」而非「最优并行」，源码注释解释了原因：优化反而会让 LUT 翻倍，所以维持原样。

时序对齐要点：`reg_was_read` 在 R 握手**当拍**组合产生；`reg_was_written` 在 W 握手**次拍**寄存器产生，恰好与 `regs_down` 更新到新值的那一拍对齐——这样 fabric 看到 `reg_was_written(idx)` 脉冲时，`regs_down(idx)` 已经是新值。

#### 4.2.3 源码精读

先看实体接口，注意 `registers` 是唯一的「配置入口」，`default_values` 与 `regs_up` 的端口初值都绑到它：

[modules/register_file/src/axi_lite_register_file.vhd:57-85](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L57-L85) —— generic `registers`/`default_values`；端口含 AXI-Lite、`regs_up`/`regs_down`、`reg_was_read`/`reg_was_written`。注释点明 `reset` 可悬空（代码用初值，初始复位非必需）。

地址译码的范围计算，低 2 位丢弃：

[modules/register_file/src/axi_lite_register_file.vhd:89-90](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L89-L90) —— `num_address_bits` 与 `address_range = [num_address_bits+1 downto 2]`，状态机用它从 `ar.addr`/`aw.addr` 切出索引。

`regs_down` 的组合驱动，只动可写寄存器的 `utilized_width` 位：

[modules/register_file/src/axi_lite_register_file.vhd:97-107](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L97-L107) —— `assign_down` 进程。

读通道的组合数据选择与 SLVERR/OKAY 判定（默认 SLVERR + 数据 0，命中可读寄存器才改写）：

[modules/register_file/src/axi_lite_register_file.vhd:118-151](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L118-L151) —— `set_status`：`reg_was_read` 在 R 握手当拍脉冲；读值按 `is_application_gives_value_mode` 二选一（`regs_up` 或 `reg_values`）。

读通道的两态握手状态机：

[modules/register_file/src/axi_lite_register_file.vhd:155-189](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L155-L189) —— `read_process`：`ar` 锁存地址并采样 AR、`r` 给出 R，复位回到 `ar`。

写通道的时序锁存——注意写脉冲模式每拍先把 `reg_values` 复位到默认值（这是脉冲的来源），再在 W 握手拍写入新值：

[modules/register_file/src/axi_lite_register_file.vhd:202-240](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L202-L240) —— 写 `set_status`：默认 `b.resp=SLVERR`；命中可写寄存器才 `OKAY` 并在 W 握手锁存 `w.data`；`reg_was_written` 与 `reg_values` 同拍更新（即 W 握手次拍）。

写通道三态握手状态机（aw→w→b），含那段「不优化反而更省 LUT」的注释：

[modules/register_file/src/axi_lite_register_file.vhd:247-289](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/axi_lite_register_file.vhd#L247-L289) —— `write_process`。

> 真实设计的用法样板：`dma_axi_write_simple_axi_lite` 把核心逻辑与一个由 hdl-registers 生成的寄存器文件实体并接，`regs_up`/`regs_down` 在二者间直连，CPU 侧只暴露 `regs_m2s`/`regs_s2m`：[modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd:58-101](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/dma_axi_write_simple/src/dma_axi_write_simple_axi_lite.vhd#L58-L101)。生成的 `*_regs_up_t`/`*_regs_down_t` 类型其实就是 `register_vec_t` 的具名别名。

#### 4.2.4 代码实践（仿真型，对应本讲任务）

**实践目标**：定义一组含 `r`/`w`/`r_w` 的寄存器，实例化 `axi_lite_register_file`，用 AXI-Lite master BFM 写入并回读，验证各模式行为。

**操作步骤**：本实践直接复用现成测试台 `tb_axi_lite_register_file.vhd` 作为运行载体，重点观察它如何构造清单与驱动 BFM。

1. 阅读清单构造函数，看它如何把前几个下标固定为 `r`/`w`/`r_w` 以便定向测试：[modules/register_file/test/tb_axi_lite_register_file.vhd:45-83](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_axi_lite_register_file.vhd#L45-L83)（`index<2 → r`、`index<4 → w`、`index<6 → r_w`，其余随机）。
2. 看 BFM 与 DUT 的实例化与连线：[modules/register_file/test/tb_axi_lite_register_file.vhd:308-341](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_axi_lite_register_file.vhd#L308-L341)。BFM 用 `bfm.axi_lite_bfm_pkg` 的 `write_bfm`/`check_bfm` 过程，以寄存器**下标**（不是字节地址）寻址。
3. 运行该 testbench（需先按 [u1-l3](u1-l3-toolchain-and-deps.md) 装好 VUnit/tsfpga），例如：
   ```
   python tools/simulate.py register_file.tb_axi_lite_register_file
   ```
4. 重点跟随 `test_random_write_then_read` 用例（L190–L194 + `reg_stimuli`/`reg_data_check` L121–L174）：对每个寄存器，若是写模式则 `write_bfm` 写入一个随机值，若是 `application_gives_value` 模式则驱动 `regs_up`，再回读校验。

**需要观察的现象**：

- `r_w` 寄存器：写入 `bus_values` 后回读，得到同一个 `bus_values`（回环）。
- `r` 寄存器：读回的是 `regs_up` 上你驱动的 `fabric_values`（硬件状态），与是否写过无关。
- `w` 寄存器：`write_bfm` 后 `regs_down(idx)` 变成写入值；试图读它会得到 `SLVERR`（见用例 `test_read_from_non_read_type_register`，L224–L227）。

**预期结果**：所有 `check_equal` / `check_bfm` 通过，VUnit 报告该 testbench 的全部用例（含两个 `SLVERR` 用例、两个复位用例）成功。若你无法本地运行，**待本地验证**——但可静态确认：`r` 的读值断言在 L165–L166（取 `fabric_values`），`r_w` 的读值断言在 L168（取 `bus_values`）。

#### 4.2.5 小练习与答案

**练习 1**：CPU 向一个 `r`（只读）寄存器发起写事务，B 通道会回什么响应？为什么？

> **答案**：`SLVERR`。写 `set_status` 默认 `b.resp=SLVERR`，只有 `is_write_mode` 为真且 `write_index` 命中时才改 `OKAY`；`r` 不满足 `is_write_mode`，故保持 `SLVERR`。用例 `test_write_to_non_write_type_register`（L229–L232）正是验证这一点。

**练习 2**：为什么 `reg_was_written` 要延迟到 W 握手的**下一拍**才脉冲，而 `reg_was_read` 是在 R 握手**当拍**就脉冲？

> **答案**：写值在 W 握手那拍才锁存进 `reg_values`，`regs_down` 要到次拍才反映新值；`reg_was_written` 同步到次拍，fabric 看到脉冲时 `regs_down` 已是新值，时序自洽。读路径的 `r.data` 与 `reg_was_read` 都是组合产生、当拍有效，所以当拍脉冲即可。

**练习 3**：`enable_output_register` 之类的特性在这个实体里**没有**——它如何在不增加输出寄存器的情况下保证时序？

> **答案**：它把读/写各做成独立状态机、数据选择用组合 `set_status`，并通过「只动 `utilized_width` 位」「默认值兜底」控制组合路径宽度。源码注释提到写握手刻意不「优化」以避免 LUT 翻倍。若需改善时序，可在实体外加 [u5-l4](u5-l4-axi-lite-subsystem.md) 的 `axi_lite_pipeline`（底层是 [u2-l1](u2-l1-handshake-convention.md) 的 `handshake_pipeline`）。

---

### 4.3 interrupt_register：粘滞中断的聚合与清除

#### 4.3.1 概念说明

很多外设都需要向 CPU 请求中断：FIFO 满、DMA 写完、链路错误……这些原始中断「源（sources）」往往几十路、转瞬即逝。直接把它们拉到 CPU 不现实——CPU 需要一个** remembers**「发生过什么」的寄存器。`interrupt_register` 就是干这件事的：它把若干中断源**粘滞（sticky）** 地聚合到一个 `status` 寄存器，再用一个 `mask` 寄存器选出哪些状态真正去拉 `trigger` 中断脚，并提供 `clear` 通道让 CPU 处理完后清掉已确认的中断。

它与本讲主角天然搭配：把 `status` 接到一个 `r` 模式寄存器的 `regs_up`，CPU 就能读到中断状态；把一个 `wpulse` 寄存器的 `regs_down` 接到 `clear`，CPU「写 1 清中断」就成了一次脉冲写。

#### 4.3.2 核心流程

每个时钟沿，对每一位 `idx` 计算 `status` 的下一拍值，逻辑是「优先清、其次置、否则保持」：

\[
\text{status\_next}[i] =
\begin{cases}
0 & \text{若 } \text{clear}[i]=1 \quad\text{(清优先)}\\
1 & \text{若 } \text{sources}[i]=1 \quad\text{(源置位，sticky)}\\
\text{status}[i] & \text{否则}\quad\text{(保持)}
\end{cases}
\]

`trigger` 是 `status` 与 `mask` 按位与之后再归约：

\[
\text{trigger} = \bigvee_i \bigl(\text{status\_next}[i] \wedge \text{mask}[i]\bigr)
\]

要点：

- `status` 一旦被某源置位就**粘住**，直到 `clear` 把它清掉；源消失不会自动清。
- `trigger` 只看「被 mask 放行的 status」。清掉所有已置位、或 mask 屏蔽掉所有位，都会让 `trigger` 拉低。
- `mask` 默认全 1（不屏蔽），`sources`/`clear` 默认全 0——悬空时不会误触发。
- **边沿重触发**：若某源是常驻 1（如很多 IP 的中断脚是边沿触发、置位后保持），CPU 清中断后该位会在**下一拍重新被源置位**，于是 `trigger` 拉高一拍又拉低——这模拟了「中断源未消除则清后立即重触发」。测试台专门覆盖了这一点。

#### 4.3.3 源码精读

实体接口极简，`sources`/`mask`/`clear` 进，`status`/`trigger` 出，默认值都选了「安全侧」（`mask` 全 1、源与清全 0）：

[modules/register_file/src/interrupt_register.vhd:30-41](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/interrupt_register.vhd#L30-L41) —— 端口定义。

核心进程就一个，逐位套用上面的三分支优先级，最后用 `or (status_next and mask)` 归约：

[modules/register_file/src/interrupt_register.vhd:47-65](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/src/interrupt_register.vhd#L47-L65) —— `main` 进程。注意 `status_next` 用变量（variable）先算出全部位再同时赋给 `status` 与参与 `trigger` 归约，保证二者用的是同一拍的新值、没有 off-by-one。

边沿重触发的验证就在测试台里，注释解释了为何 PCIe 等 IP 的边沿中断需要这个语义：

[modules/register_file/test/tb_interrupt_register.vhd:103-119](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_interrupt_register.vhd#L103-L119) —— `test_clearing_a_constantly_active_source_should_result_in_edge`：源保持 1，清一拍后 `trigger` 先 `check_no_trigger` 再 `check_trigger`。

> 资源回归显示该实体约 39 LUT / 33 FF / 逻辑级数 5：[modules/register_file/module_register_file.py:59-71](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/module_register_file.py#L59-L71)。

#### 4.3.4 代码实践（仿真型）

**实践目标**：亲手验证粘滞、mask 门控、clear 清除与边沿重触发四件事。

**操作步骤**：复用 `tb_interrupt_register.vhd`。

1. 跟随测试台主流程前段（L74–L85）：把 `sources` 的 bit0/1/2 置 1 若干拍，确认 `status` 的 bit0/1/2 被**粘住**（源撤销后仍为 1），但此时 `mask` 全 0 ⇒ `trigger` 为 0。
2. 把 `mask(0)` 置 1，确认 `trigger` 立刻拉高（被门控放行）。
3. 运行 `test_clear_register_wipes_trigger`（L87–L94）：`clear(0)` 置一拍，确认 `status(0)` 与 `trigger` 同时清零。
4. 运行 `test_changing_mask_wipes_trigger`（L96–L101）：`mask(0)` 拉低，确认 `trigger` 清零但 `status(0)` 仍在（mask 只门控 trigger，不清状态）。
5. 运行边沿用例（L103–L119）：源保持 1 时清一拍，观察清的当拍 `trigger` 为 0、**下一拍又为 1**。

**运行**：`python tools/simulate.py register_file.tb_interrupt_register`

**需要观察的现象**：状态位的粘滞特性；`trigger` 严格等于「status 与 mask 同时为 1 的位之或」；清后源若仍在则下一拍重触发。

**预期结果**：三个用例全部 `check` 通过。若无法本地运行，**待本地验证**，但可由 L47–L65 的进程逻辑静态推断上述行为一定成立。

#### 4.3.5 小练习与答案

**练习 1**：`status` 某位被置位后，对应的 `sources` 位回到 0，`status` 会怎样？

> **答案**：保持 1。`status_next` 在「clear=0 且 sources=0」分支取 `status(idx)`，即保持原值。这正是「粘滞」的含义——必须显式 `clear` 才会清。

**练习 2**：把 `mask` 全部置 0，`status` 还会变化吗？`trigger` 呢？

> **答案**：`status` 照常随源置位与清零（mask 不影响 status）；`trigger` 恒为 0（按位与全 0 后归约为 0）。mask 只门控 trigger 输出，不改变状态记录。

**练习 3**：为什么进程里要先算变量 `status_next`，而不是直接读信号 `status` 来算 `trigger`？

> **答案**：若直接用信号 `status`，则 `trigger` 用的是上一拍的旧状态、而 `status` 即将更新为新值，二者差一拍，会出现「状态已清但 trigger 仍高」或反之的瞬态不一致。用变量 `status_next` 让本拍的状态更新与 trigger 归约引用**同一个新值**，保证语义自洽。

---

## 5. 综合实践

把本讲三个构建块串成一个最小的「中断型状态/控制」子系统，巩固「清单定义 → 总线读写 → 中断聚合」的完整链路。

**任务**：设计一个寄存器文件，含 4 个寄存器（手工写 `regs` 清单即可）：

| 下标 | 模式 | 用途 |
| :---: | --- | --- |
| 0 | `r` | 中断状态（读 `interrupt_register` 的 `status`） |
| 1 | `r_w` | 中断屏蔽 `mask`（软件读写，回环） |
| 2 | `wpulse` | 清中断（写脉冲接到 `interrupt_register` 的 `clear`） |
| 3 | `r` | 自由计数器（fabric 侧一个计数器接到 `regs_up(3)`） |

**要求**：

1. 实例化 `axi_lite_register_file` 与 `interrupt_register`，按下述数据流连线：
   - `interrupt_register.status` → `regs_up(0)`；`regs_down(1)` → `interrupt_register.mask`；`regs_down(2)` → `interrupt_register.clear`。
   - 给 `interrupt_register.sources` 接一个 fabric 产生的脉冲源（如计数器溢出）。
2. 用 AXI-Lite master BFM（参照 [tb_axi_lite_register_file.vhd:308-319](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/test/tb_axi_lite_register_file.vhd#L308-L319) 的实例化方式）完成一个完整场景：
   - 产生一个源脉冲 → 读寄存器 0，确认对应 status 位置位；
   - 写寄存器 1 设 mask → 确认 `trigger` 拉高；
   - 写寄存器 2（清中断）→ 读寄存器 0，确认 status 已清、`trigger` 拉低。
3. 在 testbench 里用 `check_equal` 断言每一步，并复用 4.2.4 的运行方式（`python tools/simulate.py <your_tb>`）跑通。

**验收标准**：全部断言通过；能说清「软件写 mask 是 `r_w` 回环、写 clear 是 `wpulse` 单拍脉冲、读 status 是 `r` 取 fabric 值」分别对应本讲哪条机制。这一步若暂无仿真环境，**待本地验证**，但连线关系与模式选择可静态完成。

## 6. 本讲小结

- `register_file_pkg` 是寄存器生态的字典：`register_mode_t` 五种模式 + 四个查询函数，把「软件可读/可写/写脉冲/读值来源」四属性固化；`register_definition_vec_t` 一份清单即参数化整个实体。
- `axi_lite_register_file` 用两份 generic（`registers` + `default_values`）生成完整 AXI-Lite slave：读/写各一套状态机，默认响应 `SLVERR`，命中且模式匹配才 `OKAY`，错误处理无需用户操心。
- 数据流三件套：`regs_up`（fabric→总线，供 `r`/`r_wpulse` 读）、`regs_down`（总线→fabric，供写模式用，只动 `utilized_width` 位）、`reg_was_read/written`（访问脉冲，写延迟一拍以对齐新值）。
- `wpulse`/`r_wpulse` 靠「每拍先把内部值复位到 default、写拍再置新值」实现单周期脉冲。
- `interrupt_register` 用「清优先、源置位、否则保持」的粘滞 status + mask 门控 + 归约出单比特 `trigger`，并支持「源常驻时清后立即重触发」的边沿语义。
- 三个构建块天然组合：`status`/`mask`/`clear` 正好对应 `r`/`r_w`/`wpulse` 三种模式寄存器，构成完整的 CPU 中断接口。

## 7. 下一步学习建议

- **真实生成链**：本讲的 `registers`/`default_values` 在真实工程里由 `hdl-registers` 从 toml 生成。读 [u7-l3（DMA 寄存器定义与 C++ 驱动）](u7-l3-dma-registers-and-cpp-driver.md) 看 `regs_dma_axi_write_simple.toml` 如何生成 VHDL 寄存器包与 C++ 头文件；再读 [u9-l1（文档生成）](u9-l1-documentation-generation.md) 看 `build_docs.py` 如何驱动这些 generator。
- **把寄存器文件接进总线**：本讲的实体是总线终点。回到 [u5-l4](u5-l4-axi-lite-subsystem.md) 把 `axi_to_axi_lite` → `axi_lite_mux` → `axi_lite_cdc` → 本实体串起来，理解一份完整 CPU 寄存器拓扑；需要跨时钟域时复习 [u3-l1](u3-l1-resync-basics.md)。
- **把资源占用纳入回归**：`axi_lite_register_file` 的 LUT/FF 在 [module_register_file.py](https://github.com/hdl-modules/hdl-modules/blob/0271e3b128e7fec80bb0991c31c294cb38d2cb31/modules/register_file/module_register_file.py) 被 `EqualTo` 断言。读 [u8-l3（资源占用回归）](u8-l3-resource-utilization-regression.md) 理解 netlist 构建如何守护面积。
- **验证方法**：本讲的两个 testbench 是 BFM + VUnit 自检的范例，[u8-l1（BFM 仿真模型）](u8-l1-bfm-simulation-models.md) 与 [u8-l2（VUnit 测试台模式）](u8-l2-vunit-testbench-patterns.md) 系统讲解其背后的方法论。
