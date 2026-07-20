# 参考设计：Vivado 工程与时钟域

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `refdesign/ZCU102/project.tcl` 是如何「凭空」重建出一个完整 Vivado 工程的，并能解释 `origin_dir` 机制如何让脚本在任何机器上都能找到源文件。
- 理解 `src/system_wrapper.vhd` 作为 Block Design（BD）顶层外壳的角色：它本身几乎「空无一物」，真正的电路在 BD 生成的 `system` 实体里。
- 读懂 `constraints/timing.xdc` 里的 `set_max_delay -datapath_only` 约束，并能从注释反推出三条时钟的频率。
- 解释为什么 IP 允许每条 AXI-Stream 流自带独立时钟（`Str00_Clk`、`Str01_Clk`…），而跨时钟域处理交给 IP 内部完成——这正是 `psi_ms_daq` 「多流」能力的物理基础。

本讲是单元 5（端到端集成）的第一讲，**只看硬件工程与 clock domain crossing（CDC）**；下一讲 u5-l2 才进入 `main.c` 的 C 应用与中断回读。本讲依赖 u1-l4（IP 打包流程总览），因为参考设计里被例化的正是 u1-l4 打包出来的 `psi_ms_daq_axi` IP。

## 2. 前置知识

在开始之前，先用通俗语言把几个本讲会反复出现的术语说清楚。

- **ZCU102**：Xilinx（现 AMD）的一款 Zynq UltraScale+ MPSoC 评估板。它一块芯片里同时有 **PS**（Processing System，含 4 个 ARM A53 核、DDR 控制器、外设）和 **PL**（Programmable Logic，即传统 FPGA fabric）。本仓库的 IP 跑在 PL 侧，由 PS 侧的 CPU 通过 AXI 总线配置、由 PS 侧的 DDR 承接采集数据。
- **Block Design（BD）**：Vivado 里的图形化连线方式。你把一个个 IP 像积木一样拖进去、用线连起来，Vivado 把整张图存成一个 `system.bd`。本讲读的 `project.tcl` 里，就有一大段 `cr_bd_system` 过程把整张 BD 用 Tcl 命令「画」出来。
- **时钟域（clock domain）与时钟域跨越（CDC, Clock Domain Crossing）**：同一块 FPGA 里常常同时存在多个不同频率的时钟。一个触发器（flip-flop）输出要被另一个时钟的触发器采样时，两者之间就构成一次 CDC。CDC 不做约束就会产生**亚稳态（metastability）**，导致数据采样错误。`set_max_delay -datapath_only` 是 Vivado 里给 CDC 打「虚假时序约束」的标准手段，下文会详解。
- **`set_max_delay`**：告诉综合/实现工具「这段路径的延迟不要超过 N 纳秒」。它常被用来约束那些**本来就不该用普通时序分析（setup/hold）去衡量**的异步跨时钟路径。
- **PL 时钟（pl_clk0/1/2）**：PS 里的 PLL 会产出若干路可输出到 PL 侧的时钟，称为 PL 时钟。本参考设计用了 PL0/PL1/PL2 三路，分别给 AXI 总线和两条数据流使用。
- **`origin_dir`**：project.tcl 里一个 Tcl 变量，表示「源文件相对哪个目录定位」。它让重建脚本不依赖绝对路径，从而能进版本控制、跨机器复现。

如果你对 AXI4 / AXI-Stream 协议本身还不熟，建议先回看 u2-l1（封装实体的端口分组）与 u3-l1（寄存器映射），本讲默认你已经知道 IP 有 AXI Slave（寄存器）、AXI Master（写 DDR）、AXI-Stream 输入三类接口。

## 3. 本讲源码地图

本讲聚焦于参考设计的「硬件工程」三件套，外加 README 里对参考设计的一句话说明。

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [refdesign/ZCU102/project.tcl](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl) | Vivado 工程重建脚本（约 1088 行） | 目录/工程名设定、`origin_dir` 机制、添加源文件与约束、内部 `cr_bd_system` 过程如何「画」出整张 BD、三条 PL 时钟如何分发给 AXI 与各条流 |
| [refdesign/ZCU102/src/system_wrapper.vhd](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/src/system_wrapper.vhd) | BD 的 HDL 顶层外壳（24 行） | 为什么它几乎是空的、它例化的 `system` 实体从哪里来、为什么 entity 没有端口 |
| [refdesign/ZCU102/constraints/timing.xdc](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/constraints/timing.xdc) | 时序约束文件（17 行） | `set_max_delay -datapath_only` 的两段 CDC 约束、注释里隐藏的时钟频率信息 |
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/README.md) | 项目说明 | 第 62 行声明「提供了一个参考设计」 |

参考设计的目录全貌（供建立地图，软件部分 `Sdk/` 留给 u5-l2）：

```
refdesign/ZCU102/
├── project.tcl                 ← 本讲：工程重建脚本
├── src/
│   └── system_wrapper.vhd      ← 本讲：BD 顶层 wrapper
├── constraints/
│   └── timing.xdc              ← 本讲：CDC 时序约束
└── Sdk/                        ← 软件部分（下一讲 u5-l2）
    ├── app/src/main.c
    ├── bsp/
    └── hw/system.hdf
```

## 4. 核心概念与源码讲解

### 4.1 project.tcl：用脚本重建整个 Vivado 工程

#### 4.1.1 概念说明

Vivado 工程本身是一大堆二进制/缓存文件，不适合直接放进 Git。业界标准做法是写一个 **Tcl 重建脚本**：它只包含「创建工程、设器件、加源文件、加约束、定义 BD、创建 synth/impl run」等命令。任何人在任何机器上 `source project.tcl`，就能得到一个功能等价的干净工程。本仓库的 `refdesign/ZCU102/project.tcl` 就是这样一份脚本，文件头明确写明它由 Vivado v2018.2 生成、用 Tcl Shell 执行即可复现工程。

脚本里有一条贯穿全局的「相对路径锚点」——`origin_dir`，它是整个脚本可移植的关键。

#### 4.1.2 核心流程

`project.tcl` 的执行可以拆成下面这条流水线：

1. **解析命令行参数**：支持 `--origin_dir <path>`、`--project_name <name>`、`--help`，让调用者覆盖默认值。
2. **设定 `origin_dir` 与工程名**：默认 `origin_dir` 就是脚本所在目录；工程名默认 `RefDesign`。
3. **`create_project` 建空工程**：指定目标器件 `xczu9eg-ffvb1156-2-e`（ZCU102 上的 ZU9EG 芯片）。
4. **设一大批工程属性**：板型号、VHDL-2008 开关、目标语言、XPM 库等。
5. **设 IP 仓库路径并重建索引**：把仓库根（`origin_dir/../../../`，即 PsiFpgaLib 根）加进 IP 仓库，让 Vivado 能找到 `psi.ch:PSI:psi_ms_daq_axi` 这个自定义 IP。
6. **加源文件**：把 `src/system_wrapper.vhd` 加进 `sources_1`，并设顶层为 `system_wrapper`。
7. **加约束**：把 `constraints/timing.xdc` 加进 `constrs_1`。
8. **`cr_bd_system` 画 BD**：用 Tcl 命令把 PS、复位、AXI 互联、数据发生器、`psi_ms_daq` IP、ILA 全连线，并分配地址段。
9. **创建 synth_1 / impl_1 两个 run**：注意脚本只**配置** run、不**启动**它们，综合与实现要用户手动跑。

#### 4.1.3 源码精读

**`origin_dir` 与工程名设定**——脚本的「相对路径锚点」：

[refdesign/ZCU102/project.tcl:L41-L55](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L41-L55) —— `origin_dir` 默认为 `"."`（脚本所在目录）；如果 Tcl Shell 里预先设了全局变量 `::origin_dir_loc` 就改用它。工程名同理可被 `::user_project_name` 覆盖。这两段让脚本既能直接 `source`，也能被别的脚本带参调用。

**命令行参数解析**（覆盖默认值）：

[refdesign/ZCU102/project.tcl:L88-L103](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L88-L103) —— 解析 `-tclargs` 后跟的 `--origin_dir` / `--project_name` / `--help`。这正是脚本「可移植」的实现：CI 或别人机器上可以用 `project.tcl -tclargs --origin_dir /path/to/ZCU102` 指定源码位置。

**建工程并锁器件/板**：

[refdesign/ZCU102/project.tcl:L109](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L109) —— `create_project ${_xil_proj_name_} ./${_xil_proj_name_} -part xczu9eg-ffvb1156-2-e` 把器件锁死为 ZCU102 的 ZU9EG。

[refdesign/ZCU102/project.tcl:L119](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L119) —— 把板型号设为 `xilinx.com:zcu102:part0:3.2`，让工程带上 ZCU102 的引脚约束、DDR 配置等板级预设。

**几个对 CDC 与 VHDL 关键的工程属性**：

[refdesign/ZCU102/project.tcl:L137](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L137) —— 开启 VHDL-2008（`psi_ms_daq_vivado.vhd` 的 `generate` 写法需要它）。

[refdesign/ZCU102/project.tcl:L153](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L153) —— `xpm_libraries` 设为 `XPM_CDC XPM_FIFO XPM_MEMORY`。**XPM_CDC** 是 Xilinx 提供的预置、已验证的 CDC 原语库（同步器、异步 FIFO 等）；把它打开，意味着工程里凡是需要跨时钟的地方都会用上「安全」的 XPM 原语，而不是让用户自己手写两级同步器。

**IP 仓库路径——这是参考设计能找到 `psi_ms_daq_axi` 的关键**：

[refdesign/ZCU102/project.tcl:L160-L165](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L160-L165) —— 把 `origin_dir/../../../`（即本仓库的上两级，PsiFpgaLib 根目录）加为 IP 仓库路径，然后 `update_ip_catalog -rebuild` 刷新索引。u1-l4 打包出来的 IP 就放在本仓库根目录，所以 PSI 统一布局（见 u1-l3）下参考设计能自动发现它。

**添加源文件与顶层**：

[refdesign/ZCU102/project.tcl:L167-L186](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L167-L186) —— 把 `src/system_wrapper.vhd` 加进 `sources_1`，并把 `top` 设为 `system_wrapper`。注意：这里**只**加了一个 wrapper 文件；真正的电路（BD）在后面 `cr_bd_system` 里用 Tcl 命令现场搭建。

**添加约束**：

[refdesign/ZCU102/project.tcl:L196-L202](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L196-L202) —— 把 `constraints/timing.xdc` 加进 `constrs_1`，文件类型设为 `XDC`。这份约束只有 17 行，却是本讲的另一主角（见 4.3）。

**BD 里 `psi_ms_daq` IP 的实例与参数**——参考设计的「主角」：

[refdesign/ZCU102/project.tcl:L793-L805](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L793-L805) —— 例化 `psi.ch:PSI:psi_ms_daq_axi`，并设 `Streams_g=2`、`Stream0Width_g=16`、`Stream0ClkFreqHz_g=142000000`、`Stream1ClkFreqHz_g=111000000`、`Stream0Buffer_g=1024` 等。这两个 `ClkFreqHz_g` 参数就是 u2-l3 讲过的「逐流时钟频率」，IP 内部要用它算超时（`StreamTimeout_g`）与时间戳（`StreamTsFifoDepth_g` 相关的时钟周期换算）。注意：这里的 142 MHz / 111 MHz 与下文 timing.xdc 的注释、与 PL1/PL2 实际输出频率一一对应。

**三条 PL 时钟的分发**——这一段是 4.4「每流独立时钟」的直接证据：

[refdesign/ZCU102/project.tcl:L835-L837](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L835-L837) ——

- `pl_clk0`（125 MHz）→ `axi_interconnect` 的所有 ACLK、`psi_ms_daq/M_Axi_Aclk`、`psi_ms_daq/S_Axi_Aclk`，即 **AXI 时钟域**。
- `pl_clk1`（142 MHz）→ `psi_ms_daq/Str00_Clk`，即 **stream0 数据时钟域**。
- `pl_clk2`（111 MHz）→ `psi_ms_daq/Str01_Clk`，即 **stream1 数据时钟域**。

这三行是整个 CDC 问题的根源：数据在 `Str00_Clk`（142 MHz）和 `Str01_Clk`（111 MHz）下产生，却要被 `S_Axi_Aclk`/`M_Axi_Aclk`（125 MHz）的 AXI 逻辑读取/写回 DDR，于是天然存在两处 CDC。

**地址段分配**（看一眼，理解数据怎么落到 DDR）：

[refdesign/ZCU102/project.tcl:L840-L844](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L840-L844) —— 给 `psi_ms_daq/M_Axi` 主口分配了 `0x00000000` 起、2 GB 的 DDR 段（这就是采集数据落点），以及一个 OCM 段；给 `psi_ms_daq/S_Axi` 从口分配了 `0x80000000` 起、64 KB 的寄存器段（CPU 通过它配置 IP）。这与 u3-l1 讲的寄存器地址模型对应。

#### 4.1.4 代码实践

**实践目标**：把 `project.tcl` 的执行流水线手写出来一遍，建立「脚本即工程」的心智模型。这是**源码阅读型实践**，不需要真的装 Vivado。

**操作步骤**：

1. 打开 [refdesign/ZCU102/project.tcl](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl)。
2. 按行号顺序定位这 9 个里程碑：L41-L55（origin_dir/工程名）、L88-L103（参数解析）、L109（建工程）、L137/L153（VHDL-2008 与 XPM_CDC）、L160-L165（IP 仓库）、L167-L186（源文件与顶层）、L196-L202（约束）、L793-L805（psi_ms_daq 实例）、L835-L837（三条时钟分发）。
3. 在一张纸上把这 9 步画成流水线箭头图，标注每步「用了 `origin_dir` 还是绝对路径」。

**需要观察的现象**：

- 全脚本**没有任何**写死的绝对路径作为源文件位置（文件头注释里那条 `D:/gfa/...` 只是导出时的历史记录，不参与运行）。所有源文件定位都基于 `origin_dir`。
- `cr_bd_system` 过程（L226 起）占了脚本一大半篇幅——这说明 BD 才是工程里最「重」的部分。

**预期结果**：你应当得到一张「建工程→设属性→加源/约束→画 BD→建 run」的 5 段流水线，其中 BD 段最长。

**待本地验证**：若你手头有 Vivado 2018.2 与 ZCU102 板文件，可在 Tcl Console 执行 `cd refdesign/ZCU102; source project.tcl` 验证工程能否干净重建；本环境无 Vivado，故标记为待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `project.tcl` 把 IP 仓库路径设成 `origin_dir/../../../`（上两级）而不是 `origin_dir` 本身？

**参考答案**：`origin_dir` 默认是 `refdesign/ZCU102/`，IP 打包产物（u1-l4 的 `package_ip` 输出，即 `component.xml` 等）位于**仓库根目录**，也就是 `refdesign/ZCU102/` 的上两级。在 PSI FPGA Library 统一布局（见 u1-l3）里，仓库根恰好就是 PsiFpgaLib 根，所有自定义 IP 都挂在那里，所以参考设计用相对上两级的路径就能一次性发现全部 PSI IP。

**练习 2**：脚本最后（L859 起）创建了 `synth_1` 和 `impl_1` 两个 run，但没有 `launch_runs`。这样设计有什么好处？

**参考答案**：重建脚本的职责是「得到一个配置正确的工程」，综合与实现耗时长、且依赖具体机器算力，应让用户自行决定何时启动。这样脚本 `source` 完很快返回，工程可重复、可移植；用户在 IDE 里点 Run Synthesis 即可。注释 L19-L21 也明确说了 runs 会被配置但不会自动启动。

---

### 4.2 system_wrapper.vhd：Block Design 的「空壳」顶层

#### 4.2.1 概念说明

当你在 Vivado BD 里画完一张图（`system.bd`）后，Vivado 会为它生成一个 HDL 顶层，名字通常是 `<bd_name>_wrapper.vhd`，这里就是 `system_wrapper.vhd`。这个 wrapper 的作用是：**把一个 BD 包装成一个普通 VHDL 顶层实体**，这样它就能像任何普通 `.vhd` 一样被设为工程 top、被综合、被实现。

令人惊讶的是，本参考设计的 wrapper 几乎「什么都没有」——entity 没有端口、architecture 里只例化了一个空的 `system` 组件。这不是写错了，而是 ZCU102 参考设计的一个深刻特征：**整个 PL 设计没有用到任何外部引脚**。

#### 4.2.2 核心流程

BD wrapper 的工作流程：

1. Vivado 执行 `generate_target system_wrapper.bd`（见文件头注释 L6 的 Command）。
2. 生成 `system.vhd`——把 BD 图展开成普通 VHDL 网表（这一层本仓库没有入库，由工具现场生成）。
3. 生成 `system_wrapper.vhd`——一层薄薄的 entity + architecture，里面 `component system` 加一个例化语句。
4. 工程把 `system_wrapper` 设为 top（见 4.1.3 的 L186），于是综合从 wrapper 进入 `system` 进入 BD 网表。

#### 4.2.3 源码精读

整份文件只有 24 行，逐段看：

[refdesign/ZCU102/src/system_wrapper.vhd:L1-L9](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/src/system_wrapper.vhd#L1-L9) —— 文件头注释。最关键的是第 6 行 `--Command : generate_target system_wrapper.bd`，它说明这份 wrapper 是 `generate_target` 命令的产物；第 8 行 `--Purpose : IP block netlist` 说明它是 BD 的网表顶层。

[refdesign/ZCU102/src/system_wrapper.vhd:L14-L15](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/src/system_wrapper.vhd#L14-L15) —— `entity system_wrapper is ... end system_wrapper;`。注意：**entity 没有任何 `port` 声明**。这意味着 wrapper 对外不暴露任何信号。

[refdesign/ZCU102/src/system_wrapper.vhd:L17-L23](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/src/system_wrapper.vhd#L17-L23) —— 架构体声明了一个 `component system`（同样无端口），然后例化它：

```vhdl
system_i: component system
 ;
```

这条例化连 `port map` 都没有，因为双方都没有端口。

**为什么可以「空」？** 这才是要害。ZCU102 是一颗 PS+PL 的 SoC：DDR、UART、调试口等所有「对外」的物理引脚都由 PS 侧经专用硬连线接管，根本不进 PL。PL 设计与外界的唯一交互是通过 PS-PL 内部接口——PS 把 DDR 的一段地址暴露给 PL 的 AXI Master（HP 口）、PS 的 CPU 经 AXI Slave 配置 PL、PL 用中断线（`pl_ps_irq0`）回报 PS。这些连接**全部是芯片内部连接**，在 BD 内部就接好了（见 4.1.3 的 L823、L829），不需要任何 PL 引脚约束。于是 wrapper 自然就没有端口。

**这与传统 FPGA 设计的区别**：传统设计里 wrapper 一定带一串 `clk`、`rst_n`、`data_in`、`data_out` 等端口，并在 `.xdc` 里对它们做 `set_property PACKAGE_PIN`。ZCU102 参考设计把「引脚」的概念整个交给了 PS 的 BD 配置（`zynq_ultra_ps_e` 那一大段 `PSU__...` 参数，见 project.tcl L493-L716），PL wrapper 因此「空无一物」。

#### 4.2.4 代码实践

**实践目标**：通过对比 wrapper 与 BD 内部连接，理解「端口去哪了」。

**操作步骤**：

1. 读 [system_wrapper.vhd:L14-L23](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/src/system_wrapper.vhd#L14-L23)，确认 entity 与 component `system` 都无端口。
2. 回到 [project.tcl:L823](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L823) 与 [project.tcl:L829](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L829)，找到 `psi_ms_daq/M_Axi` 接 `proc/S_AXI_HP0_FPD`、`psi_ms_daq/Irq` 接 `proc/pl_ps_irq0`。这两条线就是 PL 与 PS 的「数据出口」和「中断出口」，但它们都终结在 PS IP `zynq_ultra_ps_e` 内部，不出芯片。

**需要观察的现象**：`M_Axi`、`S_Axi`、`Irq`、`Str00`、`Str01` 这些 IP 端口（u2-l1 讲过的）在 BD 里都被连到了 BD 内部的其它 cell，没有任何一个被提升为 BD 顶层端口。

**预期结果**：你应能解释——正因为所有 IP 端口都「对内消化」在 BD 内，wrapper 才无需声明任何 `port`。

**待本地验证**：在 Vivado GUI 里打开重建后的 BD，目视确认 `psi_ms_daq` IP 周围没有任何 External Port（菱形的顶层端口符号），全部是内部连接。

#### 4.2.5 小练习与答案

**练习 1**：如果把这套设计从 ZCU102（带硬核 PS）搬到一个纯 FPGA（如 Kintex-7，没有 PS），`system_wrapper.vhd` 还能不能是空壳？

**参考答案**：不能。纯 FPGA 没有 PS 来接管 DDR 控制器和外部引脚，必须用 PL 引脚（MIG DDR 接口、时钟输入引脚等）对外，于是 wrapper 必须声明这些端口、并在约束里分配引脚。空壳 wrapper 是 ZCU102 这类「PS 接管所有外部 IO」架构的专属特征。

**练习 2**：wrapper 里写的是 `component system`，但仓库里并没有一个叫 `system.vhd` 的源文件（被加进工程的只有 `system_wrapper.vhd`）。那么综合时 `system` 实体从哪来？

**参考答案**：`system` 实体由 Vivado 对 `system.bd` 执行 `generate_target` 时现场生成（产物落在工程缓存目录下），并不在源码树里。所以工程只要把 wrapper 设为 top，综合时就能自动找到工具生成的 `system.vhd` 并继续展开。文件头 L6 的 `generate_target system_wrapper.bd` 注释正揭示了这条生成链。

---

### 4.3 timing.xdc：异步时钟域跨越（CDC）约束

#### 4.3.1 概念说明

这是本讲最核心、也最短（17 行）的文件。它要解决的问题是：**两个不同时钟域之间的数据路径，不能用普通 setup/hold 时序分析来衡量**。

普通同步时序分析假设「发送触发器和采样触发器共享同一时钟」，工具据此算 setup/hold 余量。但 CDC 路径的发送端和采样端时钟**异步**（频率不同、相位关系不固定），工具若硬算，会报出大量「假违例」（false violations），既挡不住真正的亚稳态风险，又会淹没真问题。

业界标准做法是两步：

1. **物理上**：在 CDC 处插入同步器（通常是两级触发器，或异步 FIFO），把亚稳态概率压到可忽略。本工程开了 `XPM_CDC`（见 4.1.3 的 L153），IP 内部跨时钟正是走 XPM 原语。
2. **约束上**：用 `set_max_delay -datapath_only -from <clkA> -to <clkB> <值>` 告诉工具「这条跨时钟路径我已用同步器处理，你别按常规分析，只保证数据路径延迟不超过给定值即可」。`-datapath_only` 表示「只约束数据路径、不计时钟偏斜」，这正是 CDC 的正确约束方式。

**取值原则**：注释里写得明明白白——「maxdelay of 1 clock cycle of the **faster** clock」（两个时钟里**更快**那个的 1 个周期）。下文 4.3.3 会用具体数字验证。

#### 4.3.2 核心流程

CDC 约束的推理流程：

1. 列出工程里所有时钟域。本工程有三个：`clk_pl_0`（AXI）、`clk_pl_1`（stream0）、`clk_pl_2`（stream1）。
2. 找出哪些时钟对之间存在数据路径。本工程里数据从两条流进入 IP、又被 AXI 域读出，所以存在 `(pl_0, pl_1)` 与 `(pl_0, pl_2)` 两组跨时钟关系。
3. 对每组关系算出「更快时钟的周期」，作为 `set_max_delay` 的值。
4. 约束要**双向**写——因为控制信号（如 `TReady`、应答）可能双向跨越。

时钟周期与频率的换算关系：

\[
T_{\text{period}} = \frac{1}{f}
\]

频率单位用 MHz 时，周期单位就是 ns。例如 125 MHz：

\[
T = \frac{1}{125\,\text{MHz}} = \frac{1}{125 \times 10^{6}}\,\text{s} = 8\,\text{ns}
\]

#### 4.3.3 源码精读

整份文件如下，逐行拆解：

[refdesign/ZCU102/constraints/timing.xdc:L6-L8](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/constraints/timing.xdc#L6-L8) —— 注释标题点明设计意图：「Clock Crossings (maxdelay of 1 clock cycle of the faster clock)」。这一行就是 4.3.1 取值原则的权威来源。

**第一组：clk_axi ↔ clk_stream0**

[refdesign/ZCU102/constraints/timing.xdc:L10-L12](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/constraints/timing.xdc#L10-L12) ——

```
# clk_axi (125 MHz) <-> clk_stream0 (142MHz), 1 faster clock cycles
set_max_delay -datapath_only -from clk_pl_0 -to clk_pl_1 7.000
set_max_delay -datapath_only -from clk_pl_1 -to clk_pl_0 7.000
```

注释给出两个时钟：125 MHz 与 142 MHz。更快的是 142 MHz，其周期：

\[
T_{142} = \frac{1}{142\,\text{MHz}} \approx 7.04\,\text{ns}
\]

`set_max_delay` 取 7.000 ns，正好是「更快时钟的 1 个周期」。两条命令一正一反，覆盖双向 CDC。这里的 `clk_pl_0` 就是 4.1.3 讲的 AXI 域（接 `S_Axi_Aclk`/`M_Axi_Aclk`），`clk_pl_1` 就是 stream0 域（接 `Str00_Clk`）。

**第二组：clk_axi ↔ clk_stream1**

[refdesign/ZCU102/constraints/timing.xdc:L14-L16](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/constraints/timing.xdc#L14-L16) ——

```
# clk_axi (125 MHz) <-> clk_stream1 (111MHz), 1 faster clock cycles
set_max_delay -datapath_only -from clk_pl_0 -to clk_pl_2 8.000
set_max_delay -datapath_only -from clk_pl_2 -to clk_pl_0 8.000
```

注释给出 125 MHz 与 111 MHz。**更快的是 125 MHz**（不再是流时钟！），其周期 8 ns：

\[
T_{125} = \frac{1}{125\,\text{MHz}} = 8.00\,\text{ns}
\]

`set_max_delay` 取 8.000 ns，恰好等于 AXI 时钟的 1 个周期。这一组是个绝佳的反例提醒：**「更快时钟」不一定是流时钟**——当流时钟（111 MHz）比 AXI 时钟（125 MHz）慢时，更快的是 AXI 时钟。

**这两组数字的相互印证**：注意 `clk_pl_0`/`clk_pl_1`/`clk_pl_2` 是 Vivado 给三条 PL 时钟自动起的网表名，与 project.tcl 里 PL0/PL1/PL2 的实际频率对得上（见 4.4.3）。注释里的频率并非凭空写就，而是从工程时钟实际频率反算来的。

**关于 `-datapath_only` 的细节**：这个开关告诉工具「计算延迟时只算数据路径（LUT/连线/触发器），不要把时钟路径偏斜算进去」。这对 CDC 是必须的——异步时钟之间本来就没有确定的时钟偏斜关系，硬算反而失真。注意：`-datapath_only` 不允许与 `setup/hold` 的常规分析混用，所以它必须配合「先 `set_clock_groups -asynchronous` 或先 false path」之类的使用习惯；本工程没有显式写 `set_clock_groups`，而是直接用 `set_max_delay -datapath_only` 给 CDC 打上「按最大延迟单周期」的约束，这是 PSI 的常用写法。

#### 4.3.4 代码实践

**实践目标**：从 timing.xdc 的注释**反推**三条时钟的频率，并验证「更快时钟 1 周期」取值原则。这是本讲的核心代码实践任务。

**操作步骤**：

1. 打开 [timing.xdc](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/constraints/timing.xdc)。
2. 从 L10 与 L14 两条注释直接读出：`clk_axi` = 125 MHz、`clk_stream0` = 142 MHz、`clk_stream1` = 111 MHz。
3. 对每组算「更快时钟的周期」：
   - 组 1（125 vs 142）：更快 = 142 MHz → \(T = 1/142\,\text{MHz} \approx 7.04\) ns，约束值 7.000，吻合。
   - 组 2（125 vs 111）：更快 = 125 MHz → \(T = 1/125\,\text{MHz} = 8.00\) ns，约束值 8.000，吻合。
4. 回到 [project.tcl:L592-L606](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L592-L606) 验证：PS 的 `PL0_REF_CTRL` 实际频率 124.998750 MHz ≈ 125 MHz、`PL1_REF_CTRL` 142.855714 MHz ≈ 142 MHz、`PL2_REF_CTRL` 111.110 MHz ≈ 111 MHz——与约束注释完全一致。

**需要观察的现象**：约束值 7.000 与 8.000 不是「拍脑袋定的固定纳秒」，而是各自等于「该 CDC 跨越所涉两个时钟里**更快**那个的 1 个周期」。

**预期结果**：能口算出「同样是从 AXI 域跨出去，stream0 那边用 7 ns、stream1 那边用 8 ns」，并解释这是因为 stream0（142 MHz）比 AXI（125 MHz）快，而 stream1（111 MHz）比 AXI 慢。

**关于「为什么用更快时钟的 1 周期而非固定值」的解释**（这是实践题的后半问）：

- **为什么不固定纳秒**：固定一个值（如统一 7 ns）意味着对于 stream1↔AXI 这组（更快周期 8 ns），你会**无故收紧** 1 ns 余量，让工具为满足一个本不需要的更紧约束而过度优化、浪费面积/功耗，甚至报出虚假违例。反之若统一用 8 ns，对 stream0↔AXI 这组（更快周期 7 ns）就**放得太松**，无法保证数据在更快时钟的一个周期内稳定，留下亚稳态风险。
- **为什么是 1 个周期**：CDC 处一般接的是异步 FIFO 或握手同步器，数据有效的「刷新粒度」就是源端时钟一个周期；要求跨越路径延迟不超过更快端 1 个周期，既给同步器留足采样窗口，又不过度收紧，是 CDC 工程的通用经验值。
- **为什么取「更快」**：更快时钟的周期更短，用更短的周期作上限更保守、更安全。两端里取严者，自动覆盖慢端的约束需求。

**待本地验证**：在 Vivado 实现后用 `report_timing -from [get_clocks clk_pl_0] -to [get_clocks clk_pl_1]` 查看这组 CDC 路径的余量分布，应看到所有此类路径被归到 max_delay 约束下、且不再作为 intra-clock setup 违例报出。

#### 4.3.5 小练习与答案

**练习 1**：如果将来把 stream1 的时钟从 111 MHz 提到 150 MHz，timing.xdc 的第二组约束应该怎么改？

**参考答案**：此时 pair 为（AXI 125 MHz, stream1 150 MHz），更快者是 150 MHz，周期 \(1/150\,\text{MHz} \approx 6.67\) ns。应把 L15-L16 的 8.000 改为约 6.700（或 6.670），并同步更新 L14 注释里的频率与「faster clock」说明。同时别忘了在 project.tcl 里把 `Stream1ClkFreqHz_g` 与 PL2 输出频率也改成 150 MHz 对应值，三处必须保持一致。

**练习 2**：`set_max_delay -datapath_only` 与直接 `set_false_path` 有什么区别？为什么这里选前者？

**参考答案**：`set_false_path` 完全放弃对路径的任何时序检查（延迟多大都行），适合「纯控制、无所谓延迟」的 CDC；而 `set_max_delay -datapath_only` 仍要求「数据路径延迟 ≤ N」，保留了「数据要在更快时钟 1 周期内传过」这一物理要求。本工程跨的是数据采集路径（流数据 → AXI），数据延迟仍有意义，所以用 `set_max_delay` 给出 1 周期上限，既屏蔽异步时钟的假违例、又不放任数据延迟无限大。

---

### 4.4 每流独立时钟与 IP 内部跨时钟处理

#### 4.4.1 概念说明

最后一个最小模块回答贯穿全讲的一个问题：**为什么 `psi_ms_daq` 要允许每条流自带时钟，而这些时钟又互不相同、还和 AXI 时钟不同？**

答案是 `psi_ms_daq` 的核心卖点（README 第 58 行）——「最多 16 路、**不同位宽**的 AXI-Stream 输入」。在真实采集场景里，不同数据源（ADC、传感器、数字下变频后的 IQ 流……）往往由不同速率的参考时钟驱动，强行把它们同步到同一时钟会丢失数据或浪费带宽。于是 IP 的设计契约是：

- **每条流自带一个 `StrNN_Clk`**（u2-l1 讲过的 16 路流端口里就有 `_Clk`），流的 `TData/TValid/TReady/TLast/Ts` 全部对这个时钟同步。
- **IP 内部**把每条流的时钟域统一搬到 AXI 时钟域（或内部数据时钟域），用异步 FIFO / 握手同步器完成 CDC。
- **AXI Slave 与 AXI Master 都跑在 AXI 时钟域**，CPU 侧只需面对一个统一的时钟。

参考设计用 2 条流做了最朴素的演示：故意让 stream0（142 MHz）与 stream1（111 MHz）频率不同，且都与 AXI（125 MHz）不同，从而把「每流独立时钟 + IP 内部 CDC」这一能力具象化。timing.xdc 的两段约束，正是这条契约在时序约束层的投影。

#### 4.4.2 核心流程

每流独立时钟从「频率声明」到「CDC 约束」的完整链条：

1. **硬件层**：PS 的 PLL 输出 PL0/PL1/PL2 三路不同频率时钟（project.tcl L592-L606）。
2. **连线层**：BD 把 PL0 接 AXI 域、PL1 接 stream0、PL2 接 stream1（project.tcl L835-L837）。
3. **参数层**：BD 给 IP 写 `Stream0ClkFreqHz_g=142000000`、`Stream1ClkFreqHz_g=111000000`（project.tcl L801、L803），让 IP 内部知道每条流的实际频率，以便算超时与时间戳（见 u2-l3 的单位换算）。
4. **CDC 物理层**：IP 内部用 `XPM_CDC` 原语（工程级开启，project.tcl L153）把流时钟域搬到 AXI 域。
5. **CDC 约束层**：timing.xdc 用 `set_max_delay -datapath_only` 给这三对（实际两组双向）CDC 路径打约束。

#### 4.4.3 源码精读

**PS 输出的三路 PL 时钟实际频率**——这是「每流独立时钟」的物理源头：

[refdesign/ZCU102/project.tcl:L592-L606](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L592-L606) —— 三段 `PSU__CRL_APB__PLn_REF_CTRL` 配置：

- PL0：`ACT_FREQMHZ 124.998750`、`FREQMHZ 125` → 125 MHz（AXI 域）
- PL1：`ACT_FREQMHZ 142.855714`、`FREQMHZ 72`（此 FREQMHZ 为请求值的离散档位，实际由分频器决定，见 `DIVISOR0 7`） → ≈142 MHz（stream0 域）
- PL2：`ACT_FREQMHZ 111.110000`、`DIVISOR0 9` → ≈111 MHz（stream1 域）

注意 `ACT_FREQMHZ`（actual）才是真实输出频率，它就是 timing.xdc 注释里 125/142/111 的依据。`FREQMHZ` 字段是 Vivado GUI 里「期望值」，因 PLL 分频只能取整数比，实际值会与期望略有偏差（如 PL1 期望 72 MHz 但实际由 RPLL 经 /7 分频得到 142.856 MHz——这是 BD 作者刻意选择了 142 MHz）。

**IP 的逐流时钟频率参数**——把物理频率告诉 IP：

[refdesign/ZCU102/project.tcl:L801-L803](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L801-L803) —— `Stream0ClkFreqHz_g {142000000}`、`Stream1ClkFreqHz_g {111000000}`。这两个泛型（u2-l1 讲过的逐流 7 泛型之一）让 IP 内部知道：stream0 数据每 1/142 µs 一个周期、stream1 每 1/111 µs 一个周期。IP 据此把「以流时钟周期计数」的超时与时间戳换算成 SI 单位（u2-l3 讲过的 `StreamNClkFreqHz_g → FreqReal_c → StreamClkFreq_g` 链条）。

**时钟分发连线**——把物理时钟接到 IP 的对应端口：

[refdesign/ZCU102/project.tcl:L835-L837](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L835-L837) —— 见 4.1.3 已详述：`pl_clk0→AXI`、`pl_clk1→Str00_Clk`、`pl_clk2→Str01_Clk`。

**XPM_CDC 库的开启**——IP 内部 CDC 走的是 Xilinx 预置原语：

[refdesign/ZCU102/project.tcl:L153](https://github.com/paulscherrerinstitute/vivadoIP_psi_ms_daq/blob/c210e3ff7e1ea1066502a5cbc8be4f33e0fae3f7/refdesign/ZCU102/project.tcl#L153) —— `xpm_libraries` 含 `XPM_CDC`。这意味着上游 `psi_ms_daq_axi` 在做流时钟→AXI 时钟跨越时，调用的是 `xpm_cdc_*` / `xpm_fifo_*` 等已验证原语，而非手写同步器。这也是 timing.xdc 敢用 `set_max_delay -datapath_only` 的前提——CDC 处已有物理同步器兜底。

**综合起来**：参考设计用三段代码（时钟源 L592-L606、时钟连线 L835-L837、IP 频率参数 L801-L803）+ 一份约束（timing.xdc）完整演示了「每流独立时钟 + IP 内部 CDC」机制。改任何一处都必须同步改其它三处，否则要么 IP 时间戳算错、要么 CDC 约束失配。

#### 4.4.4 代码实践

**实践目标**：把「时钟频率」在四处的一致性核对一遍，体会「每流独立时钟」机制对配置一致性的要求。

**操作步骤**：

1. 填下面这张一致性表（针对 stream0）：

| 出处 | 代码位置 | 数值 |
| --- | --- | --- |
| PS 实际输出频率 | project.tcl L597-L601（PL1 `ACT_FREQMHZ`） | ≈142 MHz |
| BD 连线 | project.tcl L836（`pl_clk1 → Str00_Clk`） | — |
| IP 泛型 | project.tcl L801（`Stream0ClkFreqHz_g`） | 142000000 |
| CDC 约束 | timing.xdc L10-L12（注释 + 7.000） | 142 MHz / 7 ns |

2. 对 stream1（PL2 / `Str01_Clk` / `Stream1ClkFreqHz_g` / timing.xdc L14-L16）再填一张。
3. 假设要把 stream0 改成 100 MHz，列出需要修改的所有位置。

**需要观察的现象**：四处数值应当彼此吻合（允许 `ACT_FREQMHZ` 有小数尾差，如 142.856 ≈ 142）。

**预期结果**：得到一份「改频率要动 4 个地方」的清单——PS PL1 分频寄存器、BD 连线（不变，仍是 pl_clk1）、IP 的 `Stream0ClkFreqHz_g`、timing.xdc 注释与 `set_max_delay` 值（更快时钟若变，7.000 要重算）。

**待本地验证**：把 stream0 改 100 MHz 后，在 Vivado 重新综合实现，确认 `report_clocks` 显示 `clk_pl_1` = 100 MHz、且 timing.xdc 中 `(pl_0, pl_1)` 组的 max_delay 应改为 8.000（更快时钟变为 125 MHz，周期 8 ns）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 CPU 侧的驱动代码（u3-l1～u4-l4）完全不需要关心「stream0 是 142 MHz、stream1 是 111 MHz」？

**参考答案**：因为 IP 内部已经把每条流的数据通过 CDC 搬到了 AXI 时钟域，CPU 经 AXI Slave 读写的所有寄存器、经 AXI Master 写入 DDR 的所有数据，都处在统一的 AXI 域。流时钟频率只是 IP 内部用来「把周期计数换算成秒」（超时、时间戳）的参数（经 `StreamNClkFreqHz_g` 注入），对 CPU 侧的寄存器接口透明。

**练习 2**：参考设计只用了 2 条流，却启用了 PL1 和 PL2 两路独立 PL 时钟。能不能让两条流共用同一个时钟，从而省掉一路 PL 时钟和一个 CDC？

**参考答案**：能。只要把两条流的 `StrNN_Clk` 都接到同一个 PL 时钟（例如都接 `pl_clk1`），并把 timing.xdc 的两组约束合并成一组，就能减少一组 CDC。代价是失去「演示两条流频率不同」的教学价值。在真实工程里，若多个数据源本来就同步，确实应该共用时钟以减少 CDC 数量、降低面积与亚稳态风险——这正体现了 `psi_ms_daq`「每流独立时钟」是**可选能力**而非强制要求。

---

## 5. 综合实践

**综合任务**：为本参考设计新增一条「stream2」数据通道（让 `Streams_g` 从 2 变成 3），并把整条「时钟 → 连线 → IP 参数 → CDC 约束」四件套配齐。

**要求**：

1. **选时钟**：复用现有三路 PL 时钟中的某一路（例如让 stream2 也用 125 MHz 的 `pl_clk0`，即与 AXI 同域），或说明若改用新频率需要怎么改 PS 配置。
2. **改 BD 连线**：在 project.tcl 的 `cr_bd_system` 里，给 `psi_ms_daq` 增加一条 AXI-Stream 输入（参考现有 `str0_testgen`/`str1_testgen` 的写法），把数据源接到 `psi_ms_daq/Str02`，并把所选 PL 时钟接到 `psi_ms_daq/Str02_Clk`。
3. **改 IP 参数**：把 `Streams_g` 改为 3，新增 `Stream2Width_g`、`Stream2ClkFreqHz_g` 等泛型（参考 u2-l1 的逐流泛型）。
4. **改 CDC 约束**：若 stream2 用了与 AXI 不同的时钟，在 timing.xdc 增加对应的 `set_max_delay` 双向约束，并算出正确取值；若与 AXI 同域，说明为何不需要新增约束。
5. **自检**：写出「这四处必须彼此一致」的检查清单。

**预期产出**：一张改动 diff 草图 + 一段「为什么这样取 `set_max_delay` 值」的说明。本任务为**设计型源码阅读实践**，不要求真在 Vivado 里跑通；标注「待本地验证」处为必须在真实工程里确认的点（如 BD 连线能否合法生成、综合是否过时序）。

**提示**：本任务把本讲的四个最小模块（工程脚本、wrapper、CDC 约束、每流时钟）与 u2-l1（IP 端口/泛型）、u1-l4（IP 打包）全部串起来，是单元 5 端到端集成的第一次小演练。下一讲 u5-l2 会从软件（`main.c`）侧再次端到端串联。

## 6. 本讲小结

- `project.tcl` 是一份**可进版本控制的工程重建脚本**：靠 `origin_dir` 相对路径锚点 + 命令行参数覆盖，它能在任何机器上凭空重建出器件为 `xczu9eg`（ZCU102）、顶层为 `system_wrapper` 的完整工程，并现场「画」出整张 BD。
- `system_wrapper.vhd` 是 BD 的**薄顶层外壳**：entity 无端口、只例化一个空的 `component system`。这反映了 ZCU102 的特质——所有外部 IO 由 PS 硬核接管，PL 设计没有外部引脚，故 wrapper 「空无一物」。
- `timing.xdc` 只有两段、共 4 条 `set_max_delay -datapath_only` 约束，却讲清了 CDC 的全部要点：异步时钟跨越要约束**双向**、取值用「**更快**时钟的 1 个周期」（组 1 = 7 ns 对应 142 MHz、组 2 = 8 ns 对应 125 MHz）。
- 「更快时钟 1 周期」而非固定纳秒，是为了既不虚假违例、又不放任数据延迟——`-datapath_only` 只约束数据路径、忽略异步时钟偏斜。
- `psi_ms_daq` 允许**每条流自带独立时钟**（`Str00_Clk` 142 MHz、`Str01_Clk` 111 MHz，AXI 125 MHz），CDC 由 IP 内部用 `XPM_CDC` 原语完成；CPU 侧因此只面对统一的 AXI 时钟域。
- 改任何一条流的时钟，必须**四处同步**：PS 的 PL 时钟分频寄存器、BD 时钟连线、IP 的 `StreamNClkFreqHz_g` 泛型、timing.xdc 的注释与 `set_max_delay` 取值。

## 7. 下一步学习建议

本讲只完成了「硬件工程与时钟域」这一半。下一讲 **u5-l2（参考设计：端到端 C 应用主程序）** 会进入 `refdesign/ZCU102/Sdk/app/src/main.c`，把 u3（C 驱动）和 u4（中断、窗口回读）的知识用到这份 ZCU102 工程上：你会看到 `PsiMsDaq_Init` 如何用 `XPAR_PSI_MS_DAQ_BASEADDR`（来自 u5-l3 要讲的 BSP 生成）初始化 IP、`XScuGic` 如何挂载电平中断、回调里为何要先 `Xil_DCacheInvalidateRange` 再 `GetDataUnwrapped`。

若你想把本讲的约束层吃得更透，建议接着读 **u5-l3（IP-XACT 描述与驱动 BSP 集成）**，它会解释 `project.tcl` 里 `cr_bd_system` 用的 `psi.ch:PSI:psi_ms_daq_axi` 这个 VLNV 是怎么从 u1-l4 的 `package_ip` 产物 `component.xml` 进入 IP Catalog 的，以及 BSP 如何据此生成 `xparameters.h`。

对 CDC 本身感兴趣的读者，可进一步在 Xilinx UG949（UltraFast 设计方法学）里查阅 `set_max_delay -datapath_only` 与 `set_clock_groups -asynchronous` 的配合使用规范——本讲只覆盖了 PSI 工程的实际写法。
