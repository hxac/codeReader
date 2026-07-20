# 参数化与 GUI 配置

## 1. 本讲目标

本讲聚焦于 `vivadoIP_axi_mm_reader` 这个 IP 核的**可配置参数**与 **Vivado GUI**。学完后你应当能够：

- 说出全部六个可配置参数（`AxiSlaveAddrWidth_g`、`ClkFrequencyHz`、`TimeoutUs_g`、`MaxRegCount_g`、`MinBuffers_g`、`Output_g`）的默认值、取值范围与对硬件的实际影响。
- 看懂 GUI 参数是如何一步步映射到 VHDL `generic`、最终在综合时定死硬件的（`update_MODELPARAM_VALUE` 回调）。
- 区分 `package.tcl`（声明参数、范围、控件）与 `xgui/*.tcl`（Vivado 原生运行时 GUI 脚本）这两处“住所”的分工。
- 自行推导 `s00_axi` 地址宽度的下限公式 \(\lceil \log_2(\text{MaxRegisters}\times 4 + 32) \rceil\)，并理解它为什么由“寄存器区 + RegTable 内存区”共同决定。

本讲依赖 [u1-l4（IP 打包与 Vivado 集成）](u1-l4-ip-packaging.md)建立的“打包流水线”认知，以及 [u2-l1（整体架构与数据流）](u2-l1-architecture-dataflow.md)建立的接口与数据通路心智模型，不再重复 RTL 内部流程。

## 2. 前置知识

在进入正文前，先用三段话把几个关键术语对齐。

**VHDL `generic`（类属）。** `generic` 是 VHDL 实体在**综合时**才定死的“编译期常量”。它和软件里的宏/模板参数类似：综合前可以改，综合后就是硬件本身的一部分，运行时不能再改。本 IP 核所有“可由用户在 Vivado 里配置”的行为，最终都落到一两个 `generic` 上。

**Vivado 自定义 IP 与 IP-XACT。** 一个裸的 `.vhd` 文件只是 HDL 源码，Vivado 并不把它当“IP”看待。要让它能像官方 IP 一样被拖进 Block Design、带一个“定制参数”对话框，需要用 **IP-XACT** 标准（一份 XML 形式的 `component.xml` 总账本）把它包装起来。`scripts/package.tcl`（经 PsiIpPackage 库）做的就是这件事（详见 [u1-l4](u1-l4-ip-packaging.md)）。

**`xgui` 脚本。** `component.xml` 里会挂一个 TCL 脚本（本项目即 `xgui/axi_mm_reader_v1_0.tcl`），当用户在 Vivado 里双击打开 IP 的“Re-customize IP”对话框时，Vivado 就会执行这个脚本里的 `init_gui`、`update_PARAM_VALUE.*`、`validate_PARAM_VALUE.*`、`update_MODELPARAM_VALUE.*` 等回调，来**构建页面、联动校验、把值写进 RTL `generic`**。本讲要精读的就是这些回调。

一句话总结：`generic` 是“硬件的旋钮”，GUI 是“给用户拧旋钮的面板”，`xgui` 脚本是“面板背后的接线”。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
|---|---|
| `scripts/package.tcl` | 用 PsiIpPackage 命令**声明**六个 GUI 参数的名字、说明、取值范围与控件类型，是参数的“第一处住所”。 |
| `xgui/axi_mm_reader_v1_0.tcl` | Vivado 原生**运行时** GUI 脚本：`init_gui` 构建页面，三类回调实现联动/校验/映射，是参数的“第二处住所”。 |
| `hdl/axi_mm_reader_wrp.vhd` | wrapper 实体的 `generic` 声明，给出每个参数的**默认值**，并展示参数如何在 wrapper 内部被消费（地址宽度、超时常量、generate 分支）。 |
| `hdl/axi_mm_reader.vhd` | 纯逻辑核心的 `generic`，展示 `MaxRegCount_g`/`MinBuffers_g` 真正落到了哪些硬件结构（RAM 深度、FIFO 深度）。 |
| `hdl/definitions_pkg.vhd` | 共享常量包，提供寄存器个数 `RegCount_c`、内存偏移 `MemOffs_c`，是推导地址宽度公式的事实依据。 |
| `doc/Documentation.md` | 官方对每个 GUI 参数的口径说明，含地址宽度公式与 `Output_g` 两种取值的描述。 |

## 4. 核心概念与源码讲解

### 4.1 可配置参数：六个 generic 及其硬件影响

#### 4.1.1 概念说明

`vivadoIP_axi_mm_reader` 一共对外暴露**六个**可配置参数。它们回答的是六个不同的问题：

1. **`AxiSlaveAddrWidth_g`** —— 软件配置总线 `s00_axi` 的地址有多宽？（决定软件能寻址多大空间）
2. **`ClkFrequencyHz`** —— IP 工作时钟是多少赫兹？（只用来算超时）
3. **`TimeoutUs_g`** —— 多久没触发就自动读一次？（周期性读取的“心跳”）
4. **`MaxRegCount_g`** —— 一次读周期最多读多少个寄存器？（配置表 RegTable 的容量上限）
5. **`MinBuffers_g`** —— 要缓冲几个完整读周期的数据？（内部 FIFO 的深度因子）
6. **`Output_g`** —— 读回来的数据怎么交出去？（AXI-Stream 直出，还是映射到寄存器由软件读）

这里有一个**初学者最容易忽略的关键点**：并非每个 GUI 参数都和核心 RTL 的 `generic` 一一对应。准确地说：

- **只影响 wrapper、不传入核心**的参数：`AxiSlaveAddrWidth_g`（只决定 `s00_axi` 地址引脚宽度和从机解码器宽度）、`ClkFrequencyHz` 与 `TimeoutUs_g`（二者只在 wrapper 里被合并成一个派生常量 `TimeoutCkCycles_c`，再传给核心）、`Output_g`（只决定 wrapper 里 `g_axis`/`g_naxis` 两个 `generate` 块综合哪一个）。
- **真正传入核心 RTL** 的参数：`MaxRegCount_g`、`MinBuffers_g`（核心还额外接收一个派生量 `TimeoutCkCycles_g`）。

记住这条线索，后面看源码时就能分清“改这个参数到底动了哪块硬件”。

#### 4.1.2 核心流程：一个 GUI 值如何变成硬件

从用户在 Vivado 对话框里敲下一个数字，到它变成硅片（或比特流）上的实际电路，链路是这样的：

```text
用户在 GUI 改参数
      │  （Vivado 执行 xgui 回调）
      ▼
PARAM_VALUE.<参数>          ← TCL 层参数值（对话框里看到的）
      │  update_MODELPARAM_VALUE.<参数>
      ▼
MODELPARAM_VALUE.<参数>      ← 对应到 VHDL entity 的 generic
      │  （综合时定死）
      ▼
wrapper 实体的 generic        ← 例如 AxiSlaveAddrWidth_g = 14
      │  （部分再传递/派生）
      ▼
核心实体的 generic / 内部常量  ← 例如 TimeoutCkCycles_g、FIFO 深度
      │
      ▼
最终硬件（引脚宽度、RAM 深度、综合哪个 generate 块）
```

也就是说：GUI 上的参数本质上是一串 TCL 变量（`PARAM_VALUE.*`），必须经过 `update_MODELPARAM_VALUE.*` 这一步“接线”，才会变成 RTL 的 `generic`（`MODELPARAM_VALUE.*`）。这条链路的下半段（generic → 硬件）就是 [u2-l1](u2-l1-architecture-dataflow.md) 讲过的数据通路；本讲负责上半段（GUI → generic）。

#### 4.1.3 源码精读

**(1) 参数声明：`package.tcl` 的 GUI Parameters 段。**

PsiIpPackage 提供了一组 `gui_*` 命令来声明参数。每个参数的标准写法是“创建 → 设范围/控件 → 加入页面”三步：

```tcl
# scripts/package.tcl:72
gui_add_page "Configuration"

# scripts/package.tcl:74-76
gui_create_parameter "AxiSlaveAddrWidth_g" "Address with of the s00_axi interface"
gui_parameter_set_range 8 24
gui_add_parameter
```

这段定义了 `AxiSlaveAddrWidth_g`：显示说明是“Address with of the s00_axi interface”（原文如此，with 应为 width 的笔误），取值范围 **8–24**。注意 `gui_parameter_set_range` 这一步是**范围校验的真正来源**——后面会看到 `xgui` 里的 `validate_*` 全部直接 `return true`，说明校验并没有写在这些回调里，而是由这里的 range 落到 `component.xml` 后由 Vivado 强制。

其余五个参数集中在 [scripts/package.tcl:78-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L78-L94)：

```tcl
# scripts/package.tcl:78-79   —— 注意：ClkFrequencyHz 没有 set_range
gui_create_parameter "ClkFrequencyHz" "Clock frequency in Hz"
gui_add_parameter

# scripts/package.tcl:81-83   —— TimeoutUs_g 范围 1..10000
gui_create_parameter "TimeoutUs_g" "Timeout in us (...)"
gui_parameter_set_range 1 10000
gui_add_parameter

# scripts/package.tcl:85-90   —— MaxRegCount_g / MinBuffers_g 均无范围
gui_create_parameter "MaxRegCount_g" "Maximum number or registers to read for each cycle"
gui_add_parameter
gui_create_parameter "MinBuffers_g" "Buffer space for this number of read cycles is reserved"
gui_add_parameter

# scripts/package.tcl:92-94   —— Output_g 是下拉框，两个选项
gui_create_parameter "Output_g" "Output type (...)"
gui_parameter_set_widget_dropdown {"AXIMM" "AXIS"}
gui_add_parameter
```

这里有三个值得注意的细节：

- 只有 `AxiSlaveAddrWidth_g`（8–24）和 `TimeoutUs_g`（1–10000）设了范围；`ClkFrequencyHz`、`MaxRegCount_g`、`MinBuffers_g` **没有范围约束**，用户可以填任意自然数（填得离谱时只能靠综合失败或资源爆炸来暴露问题）。
- `Output_g` 用 `gui_parameter_set_widget_dropdown` 声明为**下拉框**，候选值是 `{"AXIMM" "AXIS"}`（注意顺序：AXIMM 在前，与默认值一致）。
- `Output_g` 还会触发**可选端口**的条件启用——这正是 [u1-l4](u1-l4-ip-packaging.md) 讲过的 `m_axis` 仅在 `AXIS` 时出现：

```tcl
# scripts/package.tcl:106-107
add_port_enablement_condition m_axis_*      "\$Output_g == \"AXIS\""
add_interface_enablement_condition m_axis   "\$Output_g == \"AXIS\""
```

**(2) 默认值：wrapper 的 generic 声明。**

GUI 参数的**默认值**不在 `package.tcl` 里，而在 wrapper 实体的 `generic` 声明里（VHDL 实体的 `:= 默认值` 即 Vivado 抓取的初始值）：

```vhdl
-- hdl/axi_mm_reader_wrp.vhd:24-36
generic
(
    -- Config Parameters
    ClkFrequencyHz      : natural   := 100_000_000;
    TimeoutUs_g         : natural   := 100;
    MaxRegCount_g       : natural   := 1024;
    MinBuffers_g        : natural   := 4;
    Output_g            : string    := "AXIMM";
    AxiSlaveAddrWidth_g : natural   := 14;

    -- AXI Parameters
    C_S00_AXI_ID_WIDTH  : integer := 1
);
```

把这段和上面的 `package.tcl` 对照看，就能得到完整的“参数 → 默认值 → 范围/控件”三元组（详见 4.1.4 的表）。注意 `C_S00_AXI_ID_WIDTH` 虽然也出现在 `xgui` 回调里，但它**不是用户在 GUI 上手动配的**，而是由 Block Design 钩子 `bd/bd.tcl` 自动传播（详见 [u3-l4](u3-l4-ipxact-block-design.md)），本讲把它视作“自动管理参数”，不列入六个用户参数。

**(3) 参数如何被消费：wrapper 内部。**

看几个参数到底“咬”在了哪根硬件线上：

- `AxiSlaveAddrWidth_g` 决定 `s00_axi` 读/写地址引脚宽度，并传给从机解码器：

```vhdl
-- hdl/axi_mm_reader_wrp.vhd:56
s00_axi_araddr : in std_logic_vector(AxiSlaveAddrWidth_g-1 downto 0);
-- hdl/axi_mm_reader_wrp.vhd:199   （传给 axi_slave_ipif）
AxiAddrWidth_g => AxiSlaveAddrWidth_g
```

- `ClkFrequencyHz` 与 `TimeoutUs_g` **不单独传入核心**，而是先在 wrapper 里合并成周期数常量 `TimeoutCkCycles_c`，再以 `TimeoutCkCycles_g` 的名字喂给核心：

```vhdl
-- hdl/axi_mm_reader_wrp.vhd:134
constant TimeoutCkCycles_c : natural := integer(real(ClkFrequencyHz)*real(TimeoutUs_g)/1.0e6);
-- hdl/axi_mm_reader_wrp.vhd:315   （传给核心）
TimeoutCkCycles_g => TimeoutCkCycles_c,
```

换算关系是：

\[
\text{TimeoutCkCycles\_c} = \left\lfloor \frac{\text{ClkFrequencyHz} \times \text{TimeoutUs\_g}}{10^{6}} \right\rfloor
\]

默认 100 MHz、100 µs 代入，得 \(\lfloor 10^{8}\times 100/10^{6}\rfloor = 10\,000\) 拍（与 [u2-l4](u2-l4-trigger-timeout.md) 一致）。

- `MaxRegCount_g`、`MinBuffers_g` 真正进入核心，并直接决定两块存储的规模：

```vhdl
-- hdl/axi_mm_reader.vhd:206-211   RegTable 双端口 RAM 深度
i_ram : entity work.psi_common_tdp_ram
    generic map ( Depth_g => MaxRegCount_g, ... );

-- hdl/axi_mm_reader.vhd:226-229   读数据 FIFO 深度
i_rdfifo : entity work.psi_common_sync_fifo
    generic map ( Depth_g => MaxRegCount_g*MinBuffers_g, ... );
```

即 RegTable 深度 = `MaxRegCount_g`；FIFO 深度 = `MaxRegCount_g × MinBuffers_g`（默认 \(1024\times 4 = 4096\) 项）。`doc/Documentation.md` 第 51 行也明确写到：默认配置下 FIFO 最多缓冲 4096 个值，软件须在四个读周期内取走数据（[doc/Documentation.md:50-51](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L50-L51)）。

#### 4.1.4 代码实践：参数速查表

**实践目标**：把六个参数整理成一张可直接查阅的“默认值 → 范围 → 作用”表，建立肌肉记忆。

**操作步骤**：

1. 打开 [hdl/axi_mm_reader_wrp.vhd:24-36](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L24-L36) 抄下每个 `generic` 的默认值。
2. 打开 [scripts/package.tcl:72-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L72-L94) 抄下每个参数的范围或控件类型。
3. 用 [doc/Documentation.md:41-54](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L41-L54) 校对作用描述。

**参考结果（可直接核对）**：

| 参数 | 默认值 | GUI 范围/控件 | 影响哪块硬件 |
|---|---|---|---|
| `AxiSlaveAddrWidth_g` | 14 | 范围 8–24 | `s00_axi` 地址引脚宽度 + 从机解码器宽度（仅 wrapper） |
| `ClkFrequencyHz` | 100_000_000 | 无范围 | 仅参与算 `TimeoutCkCycles_c`（wrapper 内常量） |
| `TimeoutUs_g` | 100 | 范围 1–10000 | 与 `ClkFrequencyHz` 合成超时周期数，传入核心 `TimeoutCkCycles_g` |
| `MaxRegCount_g` | 1024 | 无范围 | RegTable 深度、FIFO 深度因子、地址宽度下限（传入核心） |
| `MinBuffers_g` | 4 | 无范围 | FIFO 深度因子 = `MaxRegCount_g × MinBuffers_g`（传入核心） |
| `Output_g` | `"AXIMM"` | 下拉 {AXIMM, AXIS} | wrapper 选哪个 `generate` 块、`m_axis` 端口是否存在（仅 wrapper） |

**需要观察的现象**：表里“影响哪块硬件”一列应当让你一眼看出——只有 `MaxRegCount_g`、`MinBuffers_g`（外加派生的 `TimeoutCkCycles_g`）真正进入核心 RTL；其余三个只影响 wrapper。这与 4.1.1 的结论互相印证。

**预期结果**：上表即为答案；无需运行任何工具即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `Output_g` 设成 `AXIS`，wrapper 里哪段代码会被综合、哪段会被剔除？

> **答**：[hdl/axi_mm_reader_wrp.vhd:170-176](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L176) 的 `g_axis : if Output_g = "AXIS" generate` 会被综合（直连 `m_axis`）；[hdl/axi_mm_reader_wrp.vhd:178-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L178-L184) 的 `g_naxis` 块被剔除。同时 `package.tcl:106-107` 的启用条件成立，`m_axis` 端口出现。

**练习 2**：为什么 `ClkFrequencyHz` 没有传给核心实体？

> **答**：因为核心只关心“多少个时钟周期算超时”，不关心绝对时间。wrapper 用 `ClkFrequencyHz` 和 `TimeoutUs_g` 算出周期数 `TimeoutCkCycles_c`（[hdl/axi_mm_reader_wrp.vhd:134](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L134)），再以 `TimeoutCkCycles_g` 传给核心。时间→周期数的换算属于“平台相关性”，被隔离在 wrapper 一侧。

### 4.2 GUI 页面构建：init_gui 与 xgui 脚本

#### 4.2.1 概念说明

上一节看到 `package.tcl` 用 `gui_*` 命令**声明**了参数；本节看 Vivado 真正执行的那份 GUI 脚本 `xgui/axi_mm_reader_v1_0.tcl`。这份脚本里最核心的过程是 `init_gui`——它负责“用户打开 IP 定制对话框时，页面上长什么样”。

需要先区分两件容易混淆的事：

- **参数的范围/控件类型**（如 8–24、下拉框）——由 `package.tcl` 的 `gui_parameter_set_range` / `gui_parameter_set_widget_dropdown` 决定，写入 `component.xml`，由 Vivado 框架强制。
- **参数在页面上的排布**（放在哪个页、用什么 widget 渲染）——由 `xgui` 脚本的 `init_gui` 决定。

两者必须列同一批参数，否则会出现“声明了但页面上看不到”或“页面上有但 component.xml 不认”的矛盾。这也是 [u1-l4](u1-l4-ip-packaging.md) 所说的“GUI 参数有两处住所”的由来。

#### 4.2.2 核心流程

`init_gui` 的执行模型很简单：

```text
Vivado 打开 IP 定制对话框
   │  调用 init_gui $IPINST
   ▼
1. 先加 Component_Name（IP 实例名，Vivado 强制要求）
2. 创建一个 Page，名为 "Configuration"
3. 逐个把六个参数 add_param 到该 Page 下
   └─ Output_g 额外指定 -widget comboBox（下拉框渲染）
   ▼
页面渲染完成，用户看到可编辑控件
```

页面布局是**单页**结构：所有六个参数都在 “Configuration” 这一个页里，没有分组标签页。这是个小 IP，参数少，单页足够。

#### 4.2.3 源码精读

`init_gui` 全文只有十几行（[xgui/axi_mm_reader_v1_0.tcl:2-14](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L2-L14)）：

```tcl
proc init_gui { IPINST } {
  ipgui::add_param $IPINST -name "Component_Name"
  #Adding Page
  set Configuration [ipgui::add_page $IPINST -name "Configuration"]
  ipgui::add_param $IPINST -name "AxiSlaveAddrWidth_g" -parent ${Configuration}
  ipgui::add_param $IPINST -name "ClkFrequencyHz"     -parent ${Configuration}
  ipgui::add_param $IPINST -name "TimeoutUs_g"        -parent ${Configuration}
  ipgui::add_param $IPINST -name "MaxRegCount_g"      -parent ${Configuration}
  ipgui::add_param $IPINST -name "MinBuffers_g"       -parent ${Configuration}
  ipgui::add_param $IPINST -name "Output_g" -parent ${Configuration} -widget comboBox
}
```

逐行解读：

- 第 3 行 `ipgui::add_param ... "Component_Name"` 是 Vivado 的**强制约定**：每个 IP 的页面最上方都要放一个实例名输入框（即用户给这个 IP 实例起的名字，如 `axi_mm_reader_0`）。
- 第 5 行创建名为 “Configuration” 的页，并把句柄存进变量 `Configuration`，后面所有参数都用 `-parent ${Configuration}` 挂到它下面。
- 第 6–10 行把五个普通参数挂上去；默认渲染成文本输入框。
- 第 11 行给 `Output_g` 加了 `-widget comboBox`，于是它在 GUI 上显示为**下拉框**而不是自由输入框——这与 `package.tcl` 里 `gui_parameter_set_widget_dropdown {"AXIMM" "AXIS"}` 的声明配套，确保用户只能二选一。

> 注意：`init_gui` 里**没有** `C_S00_AXI_ID_WIDTH`。因为它是自动管理参数（由 `bd.tcl` 传播），不需要在用户页面上露出来；它只出现在后面的 `update_*`/`validate_*` 回调里（Vivado 模板对所有 PARAM/MODELPARAM 都成对生成回调，不论是否面向用户）。

#### 4.2.4 代码实践：对照两处“住所”

**实践目标**：确认 `package.tcl` 声明的六个参数与 `xgui` 的 `init_gui` 一一对应，理解“两处住所”的一致性约束。

**操作步骤**：

1. 在 [scripts/package.tcl:72-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L72-L94) 里数出 `gui_create_parameter` 的个数与名字。
2. 在 [xgui/axi_mm_reader_v1_0.tcl:5-11](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L5-L11) 里数出 `ipgui::add_param` 的个数与名字。
3. 列出两边各自的下拉/范围声明，确认它们对同一个参数的描述一致。

**需要观察的现象**：两边都是六个同名参数，顺序一致；`Output_g` 在 `package.tcl` 用 `gui_parameter_set_widget_dropdown`、在 `xgui` 用 `-widget comboBox`，是同一件事的两种写法。

**预期结果**：完全一一对应，无遗漏、无多余。

**待本地验证**：若你在 Vivado 里实际打开该 IP 的定制对话框，应看到单一 “Configuration” 页，前五个为数值/文本输入框（其中 `AxiSlaveAddrWidth_g`、`TimeoutUs_g` 输入越界会被 Vivado 拒绝），`Output_g` 为下拉框。此现象需在装有 Vivado 的环境验证。

#### 4.2.5 小练习与答案

**练习 1**：如果想让 `MaxRegCount_g` 在 GUI 上显示成下拉框（限定几个常用档位），需要改哪两个文件？

> **答**：在 `scripts/package.tcl` 给 `MaxRegCount_g` 加 `gui_parameter_set_widget_dropdown {...}`；在 `xgui/axi_mm_reader_v1_0.tcl` 的 `init_gui` 里给对应的 `ipgui::add_param` 加 `-widget comboBox`。两处必须同时改。

**练习 2**：`init_gui` 里 `Component_Name` 这一行能删吗？

> **答**：不能。这是 Vivado 对自定义 IP 的强制要求，删掉后页面会缺少实例名输入框，Vivado 会报错或行为异常。

### 4.3 generic 映射回调：update_MODELPARAM_VALUE 与 validate

#### 4.3.1 概念说明

`xgui` 脚本里除了 `init_gui`，还有三大类回调（Vivado 为每个参数都成对/成三生成）：

| 回调前缀 | 触发时机 | 作用 |
|---|---|---|
| `update_PARAM_VALUE.X` | “当 `X` 依赖的其它参数变化时”更新 `X` | 实现参数间联动 |
| `validate_PARAM_VALUE.X` | 用户改完 `X` 后校验合法性 | 返回 `true`/`false` 决定是否接受 |
| `update_MODELPARAM_VALUE.X` | 把 TCL 参数值 `PARAM_VALUE.X` 写进 RTL `generic` `MODELPARAM_VALUE.X` | **GUI → generic 的真正接线** |

本 IP 这三类回调**几乎是空壳**：所有 `update_PARAM_VALUE.*` 体为空（说明六个参数之间互不联动），所有 `validate_PARAM_VALUE.*` 都直接 `return true`（说明范围校验交给 `component.xml` 的 range 去做）。真正干活的是第三类 `update_MODELPARAM_VALUE.*`——它就是 4.1.2 流程图里那座“从 PARAM_VALUE 跨到 MODELPARAM_VALUE 的桥”。

#### 4.3.2 核心流程

以 `Output_g` 为例，用户改值的完整时序：

```text
用户在对话框把 Output_g 改成 "AXIS"
   │
   ▼
Vivado 调 validate_PARAM_VALUE.Output_g  → return true（通过）
   │
   ▼
Vivado 调 update_PARAM_VALUE.Output_g     → （空函数，无联动）
   │
   ▼
Vivado 调 update_MODELPARAM_VALUE.Output_g
   │   set_property value [get_property value ${PARAM_VALUE.Output_g}] \
   │                      ${MODELPARAM_VALUE.Output_g}
   ▼
MODELPARAM_VALUE.Output_g = "AXIS"  ← 即 wrapper 的 generic Output_g
   │   （综合时）
   ▼
g_axis generate 块被综合，m_axis 端口出现
```

关键命令 `set_property value [get_property value A] B` 的意思是“把 A 的值读出来，写到 B 上”——也就是把对话框里的 TCL 值搬到 RTL generic 槽位里。每个 `update_MODELPARAM_VALUE.X` 都是这一行的模板复制品。

#### 4.3.3 源码精读

**(1) 空 update 与一律通过的 validate（以 `AxiSlaveAddrWidth_g` 为例）。**

```tcl
# xgui/axi_mm_reader_v1_0.tcl:16-23
proc update_PARAM_VALUE.AxiSlaveAddrWidth_g { PARAM_VALUE.AxiSlaveAddrWidth_g } {
	# Procedure called to update AxiSlaveAddrWidth_g when any of the dependent parameters ...
}
proc validate_PARAM_VALUE.AxiSlaveAddrWidth_g { PARAM_VALUE.AxiSlaveAddrWidth_g } {
	# Procedure called to validate AxiSlaveAddrWidth_g
	return true
}
```

`update` 函数体为空（没有参数联动）；`validate` 永远返回 `true`。其余参数（`ClkFrequencyHz`、`MaxRegCount_g`、`MinBuffers_g`、`Output_g`、`TimeoutUs_g`，外加自动管理的 `C_S00_AXI_ID_WIDTH`）的回调在 [xgui/axi_mm_reader_v1_0.tcl:25-77](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L25-L77)，全部是同样的空壳/`return true` 模板。

> 推论：本 IP 的参数范围校验**不在 TCL 里**，而在 `package.tcl` 通过 `gui_parameter_set_range` 写进 `component.xml` 的范围约束里。所以 `AxiSlaveAddrWidth_g` 输入 25 会被 Vivado 拒绝，靠的不是 `validate_*` 而是 range。

**(2) 真正的桥：`update_MODELPARAM_VALUE.*`。**

挑三个有代表性的看：

```tcl
# xgui/axi_mm_reader_v1_0.tcl:80-83   ClkFrequencyHz
proc update_MODELPARAM_VALUE.ClkFrequencyHz { MODELPARAM_VALUE.ClkFrequencyHz PARAM_VALUE.ClkFrequencyHz } {
	set_property value [get_property value ${PARAM_VALUE.ClkFrequencyHz}] ${MODELPARAM_VALUE.ClkFrequencyHz}
}

# xgui/axi_mm_reader_v1_0.tcl:100-103  Output_g
proc update_MODELPARAM_VALUE.Output_g { MODELPARAM_VALUE.Output_g PARAM_VALUE.Output_g } {
	set_property value [get_property value ${PARAM_VALUE.Output_g}] ${MODELPARAM_VALUE.Output_g}
}

# xgui/axi_mm_reader_v1_0.tcl:105-108  AxiSlaveAddrWidth_g
proc update_MODELPARAM_VALUE.AxiSlaveAddrWidth_g { MODELPARAM_VALUE.AxiSlaveAddrWidth_g PARAM_VALUE.AxiSlaveAddrWidth_g } {
	set_property value [get_property value ${PARAM_VALUE.AxiSlaveAddrWidth_g}] ${MODELPARAM_VALUE.AxiSlaveAddrWidth_g}
}
```

每个过程都接收两个参数：目标槽位 `MODELPARAM_VALUE.X`（对应 RTL `generic`）和源值 `PARAM_VALUE.X`（对应 GUI 输入），函数体只有一行——把后者赋给前者。完整六个用户参数加上 `C_S00_AXI_ID_WIDTH` 的映射，集中在 [xgui/axi_mm_reader_v1_0.tcl:80-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L80-L113)。

> 注意 `C_S00_AXI_ID_WIDTH` 也有 `update_MODELPARAM_VALUE`（[xgui/axi_mm_reader_v1_0.tcl:110-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L110-L113)）：它的值不是用户手填的，而是 `bd/bd.tcl` 在 Block Design 里根据上下游自动算出来再“喂”给 `PARAM_VALUE.C_S00_AXI_ID_WIDTH`，最后仍走同一座桥落到 RTL `generic`。这条链路的细节属于 [u3-l4](u3-l4-ipxact-block-design.md)。

#### 4.3.4 代码实践：推导地址宽度公式

**实践目标**：用源码事实解释 `doc/Documentation.md` 第 43 行给出的地址宽度下限公式 \(\lceil \log_2(\text{MaxRegisters}\times 4 + 32) \rceil\)，并代入默认值算出结果。

**操作步骤与推理**：

1. 软件经 `s00_axi` 看到的地址空间被 `psi_common_axi_slave_ipif` 切成两段（见 [u2-l5](u2-l5-axi-slave-wrapper.md)）：**寄存器区**（固定寄存器）+ **内存区**（RegTable）。
2. 寄存器区有几个字？查 [hdl/definitions_pkg.vhd:33](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L33)：`RegCount_c := RegIdx_Level_c+1 = 4+1 = 5`。但 wrapper 把它**向上取整为 2 的幂**作为实际寄存器数：

```vhdl
-- hdl/axi_mm_reader_wrp.vhd:133
constant USER_SLV_NUM_REG : integer := 2**log2ceil(RegCount_c);   -- = 2**3 = 8
```

   即寄存器区实际占 8 个 32 位字 = \(8 \times 4 = 32\) 字节，对应字节地址 `0x00..0x1F`。
3. 内存区从哪开始？查 [hdl/definitions_pkg.vhd:35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L35)：`MemOffs_c := 8`（字索引 8 = 字节 `0x20`），正是寄存器区之后。内存区含 `MaxRegCount_g` 项，每项 4 字节，共 \(\text{MaxRegCount\_g}\times 4\) 字节。
4. 两段共享同一段字节地址空间，总字节数 = 寄存器区 + 内存区：

\[
N_{\text{bytes}} = 32 + \text{MaxRegCount\_g}\times 4
\]

5. 要给 \(N_{\text{bytes}}\) 个不同字节地址编址，所需地址位数为：

\[
W_{\min} = \lceil \log_2(32 + \text{MaxRegCount\_g}\times 4) \rceil
\]

   这正是文档第 43 行的公式（[doc/Documentation.md:42-43](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L42-L43)）。

**代入默认值 `MaxRegCount_g = 1024`**：

\[
W_{\min} = \lceil \log_2(32 + 1024\times 4) \rceil = \lceil \log_2(4128) \rceil = \lceil 12.012\ldots \rceil = 13
\]

（验证：\(2^{12}=4096 < 4128\)，\(2^{13}=8192 \ge 4128\)，故需 13 位。）而 wrapper 默认 `AxiSlaveAddrWidth_g = 14`（[hdl/axi_mm_reader_wrp.vhd:32](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L32)），比下限多 1 位余量。

**需要观察的现象**：公式里的 “+32” 就是寄存器区那 8 个字（向上取整到 2 的幂后的 32 字节）；“MaxRegisters×4” 是 RegTable 内存区。两者相加才是软件可见的总地址空间。

**预期结果**：默认配置下最小地址宽度 13 位，wrapper 给的默认 14 位满足约束。

#### 4.3.5 小练习与答案

**练习 1**：既然所有 `validate_PARAM_VALUE.*` 都返回 `true`，那 `TimeoutUs_g` 输入 0 会被拒绝吗？

> **答**：会被拒绝，但不是被 `validate_*` 拒绝，而是被 `package.tcl` 里 `gui_parameter_set_range 1 10000`（[scripts/package.tcl:82](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L82)）写入 `component.xml` 的范围约束拒绝。`TimeoutUs_g` 恰好是有范围的参数之一。

**练习 2**：`update_MODELPARAM_VALUE.MaxRegCount_g` 这一行执行后，值的下一站是哪里？

> **答**：是 wrapper 实体的 `generic` `MaxRegCount_g`（[hdl/axi_mm_reader_wrp.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L29)）；随后 wrapper 又把它原样传给核心实体的 `generic`（[hdl/axi_mm_reader_wrp.vhd:316](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L316)），最终落成 RegTable RAM 深度与 FIFO 深度因子。

**练习 3**：六个用户参数里，哪些的 `update_MODELPARAM_VALUE.*` 实际上“桥”到了一个 wrapper 内部派生量、而非直接对应核心 `generic`？

> **答**：`ClkFrequencyHz` 和 `TimeoutUs_g`。它们桥到 wrapper 的 `generic` 后，并不传入核心，而是在 wrapper 内合成常量 `TimeoutCkCycles_c`，再以 `TimeoutCkCycles_g` 传入核心。

## 5. 综合实践

**任务**：你被要求把单周期最大读取量从默认的 1024 提升到 **2048**，同时把缓冲周期数从 4 调成 **2**（即 `MaxRegCount_g = 2048`、`MinBuffers_g = 2`）。请在不改动任何 HDL 的前提下，仅通过 GUI 参数回答下列问题，把本讲的知识串起来。

1. **地址宽度够不够**：计算新的 `AxiSlaveAddrWidth_g` 最小值，并判断默认的 14 位是否仍合法；若不合法，至少要设到多少（注意范围上限 24）。
2. **FIFO 深度**：算出新配置下内部读数据 FIFO 的深度，与默认配置比较，说明缓冲能力是否变化。
3. **超时心跳**：若保持 `ClkFrequencyHz = 100 MHz`、`TimeoutUs_g = 100`，写入核心的 `TimeoutCkCycles_g` 是多少？它受 `MaxRegCount_g` 改动影响吗？
4. **GUI 操作链路**：描述把 `MaxRegCount_g` 改成 2048 后，Vivado 依次调用哪几个 `xgui` 回调，最终把值落到 RTL `generic`。

**参考解答**：

1. \(W_{\min}=\lceil\log_2(32+2048\times 4)\rceil=\lceil\log_2(8224)\rceil\)。因 \(2^{13}=8192<8224\le 16384=2^{14}\)，故 \(W_{\min}=14\)。默认 14 位**恰好**合法（无余量）；稳妥起见可手动调到 15（仍在 8–24 范围内）。
2. FIFO 深度 \(= \text{MaxRegCount\_g}\times\text{MinBuffers\_g}=2048\times 2=4096\) 项，与默认 \(1024\times 4=4096\) **相同**——单次包变大，但总缓冲项数不变。
3. \(\text{TimeoutCkCycles\_c}=\lfloor 10^{8}\times 100/10^{6}\rfloor=10\,000\) 拍；它只依赖 `ClkFrequencyHz` 和 `TimeoutUs_g`，**不受** `MaxRegCount_g` 影响。
4. 依次调用：`validate_PARAM_VALUE.MaxRegCount_g`（返回 true）→ `update_PARAM_VALUE.MaxRegCount_g`（空）→ `update_MODELPARAM_VALUE.MaxRegCount_g`（把 `PARAM_VALUE.MaxRegCount_g` 赋给 `MODELPARAM_VALUE.MaxRegCount_g`，即 wrapper `generic`）→ 综合时落成 RegTable 深度与 FIFO 深度因子。

> **待本地验证**：在 Vivado 中实际打包并例化该 IP、填入上述参数、综合，观察资源报告中 BRAM 用量是否随 FIFO 深度变化；本环境无 Vivado，无法替你运行。

## 6. 本讲小结

- 本 IP 共六个用户可配参数：`AxiSlaveAddrWidth_g`、`ClkFrequencyHz`、`TimeoutUs_g`、`MaxRegCount_g`、`MinBuffers_g`、`Output_g`；默认值在 wrapper `generic` 里（[hdl/axi_mm_reader_wrp.vhd:27-32](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L27-L32)），范围/控件在 `package.tcl` 里（[scripts/package.tcl:72-94](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L72-L94)）。
- 只有 `MaxRegCount_g`、`MinBuffers_g`（加派生的 `TimeoutCkCycles_g`）真正进入核心 RTL；`AxiSlaveAddrWidth_g`、`ClkFrequencyHz`、`TimeoutUs_g`、`Output_g` 只在 wrapper 内消费。
- `package.tcl` 的 `gui_*` 命令负责**声明**参数（含范围与下拉控件，写入 `component.xml`），`xgui/axi_mm_reader_v1_0.tcl` 的 `init_gui` 负责**排布**页面——两处必须列同一批参数。
- GUI 值流向 RTL 的真正“桥”是 `update_MODELPARAM_VALUE.X`（[xgui/axi_mm_reader_v1_0.tcl:80-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/xgui/axi_mm_reader_v1_0.tcl#L80-L113)）；`update_PARAM_VALUE.*` 全空（无联动）、`validate_PARAM_VALUE.*` 全 `return true`（范围校验交给 `component.xml`）。
- `s00_axi` 地址宽度下限 \(W_{\min}=\lceil\log_2(\text{MaxRegCount\_g}\times 4 + 32)\rceil\)，因为寄存器区（向上取整到 8 字 = 32 字节）与 RegTable 内存区共享同一段字节地址空间。
- `Output_g` 还通过 `add_port_enablement_condition`/`add_interface_enablement_condition`（[scripts/package.tcl:106-107](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/package.tcl#L106-L107)）控制 `m_axis` 端口是否存在，是 GUI 参数影响对外端口的典型例子。

## 7. 下一步学习建议

- 想了解 `C_S00_AXI_ID_WIDTH` 这类**自动管理参数**是如何在 Block Design 里被自动传播的，请接着学 [u3-l4（IP-XACT 打包产物与 Block Design 集成）](u3-l4-ipxact-block-design.md)，它会讲 `bd/bd.tcl` 的 `pre_propagate`/`propagate` 钩子。
- 想把这些参数改动端到端验证一遍（改 generic → 编测试用例 → 跑回归仿真 → 同步 C 驱动），请学 [u3-l5（二次开发实践：扩展该 IP）](u3-l5-extending-ip.md)。
- 建议顺带阅读 PsiIpPackage 库的 `gui_*` 命令文档，理解 `package.tcl` 声明是如何被翻译进 `component.xml` 的，这会让“两处住所”的关系彻底清晰（路径一般在打包工具链的 `TCL/PsiIpPackage/` 下，**待确认**具体命令实现）。
