# Design Compiler 综合流程与脚本

> 本讲属于 **u5 ASIC 后端流程与 FPGA 集成** 单元的第一篇。前置讲义为 [u1-l3 顶层模块 tpu_top 与系统级数据流](u1-l3-tpu-top-datapath.md)：本讲综合的对象，正是那个只做例化、不含逻辑的 `tpu_top`。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 **RTL 是如何变成标准单元网表（gate-level netlist）的**，即 Synopsys Design Compiler（下称 DC）的综合主流程。
- 解释 `search_path` / `link_library` / `target_library` 三个工艺库变量的作用，并看懂 Nangate 库名 `ss0p95vn40c` 背后的 PVT 角点含义。
- 逐条讲出 `analyze → elaborate → uniquify → link → compile_ultra → write` 这条命令链每一步在做什么。
- 读懂 `compile_ultra` 的关键选项（`-gate_clock` / `-exact_map` / `-no_autoungroup` 等），并理解为什么脚本开了时钟门控、综合结果里却一个门控单元都没有。
- 把 `report_*` 与 `write_*` 两类命令与它们产出的 `.rpt` / `.v` / `.sdc` / `.sdf` / `.ddc` 文件一一对应起来，知道每个产物下游给谁用。

## 2. 前置知识

在进入脚本之前，先用三段话建立直觉。

**什么是「综合」（synthesis）。** 你在 [u1-l3](u1-l3-tpu-top-datapath.md) 里读到的 `tpu_top` 是 **寄存器传输级（RTL, Register Transfer Level）** 代码：它描述的是「数据在寄存器之间如何流动、如何运算」，用的是 `always`、`assign`、模块例化这些抽象。但芯片上根本没有「always」这种东西，只有一个个具体的晶体管级标准单元（standard cell）——与门、触发器、多路选择器、加法器、缓冲器。**综合就是把 RTL 翻译并优化成「由某个具体工艺库的标准单元连成的网表」的过程。** 做这件事的工具叫综合器，DC 是工业界最常用的一个。

**综合的三大输入与三大输出。**

- 输入：① RTL 源码（`rtl/*.v`）；② 工艺库（`*.db`，告诉你有哪些标准单元可用、每个单元的延迟/面积/功耗）；③ 约束（时钟周期、I/O 延迟、面积目标，即 `cons.tcl`）。
- 输出：① **门级网表**（`.v`，给布局布线工具用）；② **约束文件**（`.sdc`，把时钟等信息传给后端）；③ **延迟文件**（`.sdf`，给门级仿真用）；④ DC 内部数据库（`.ddc`，方便重新打开继续调）。

**DC 的脚本界面 `dc_shell`。** DC 提供一个 Tcl 解释器 `dc_shell`，所有综合命令（`analyze`、`compile_ultra`、`write`……）都是 Tcl 命令。所以综合流程本质上是一段 Tcl 脚本——本讲精读的 `syn/scripts/syn.tcl` 就是它。`syn/run` 这个一行脚本做的事情很简单：

[syn/run:1-3](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/run#L1-L3) —— 清空旧的 `log/report/output` 目录，再用 `dc_shell -f scripts/syn.tcl` 跑综合，并把屏幕输出通过 `tee` 同时存进 `log/syn.log`。

> 一个常用术语：**GTECH**。`elaborate` 之后、`compile` 之前，DC 把 RTL 翻译成一种与工艺无关的中间表示叫 GTECH（Generic TECHnology）。`compile_ultra` 才把 GTECH 映射（map）到具体工艺库的标准单元。记住「先翻译成与工艺无关的中间态，再映射到具体单元」这个两段式，就抓住了综合的核心节奏。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用来看什么 |
| --- | --- | --- |
| [syn/scripts/syn.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl) | **综合主脚本**，共 67 行 | 本讲的绝对主角，逐行精读 |
| [syn/cons/cons.tcl](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl) | 时序/面积约束（被 `source` 进来） | 看 `compile_ultra` 优化时所对照的目标（时钟 3.01ns）；细节留到 u5-l2 |
| [syn/report/synth_resources.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_resources.rpt) | `report_resources` 的产物 | 观察「报告产物长什么样」，并解释它为什么是空的 |
| [syn/report/synth_qor.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt) | Quality-of-Results 总报告 | 用来佐证综合确实跑完、规模多大（时序细读留到 u5-l2） |
| [syn/report/synth_area.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt) | 层次化面积报告 | 佐证 `-no_autoungroup` 保住了模块层次 |
| [syn/report/clock_gating.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/clock_gating.rpt) | 时钟门控报告 | 佐证 `-gate_clock` 实际没插入门控单元 |
| `syn/run` | 启动脚本 | 看 `dc_shell` 是怎么被调用的 |

`syn/output/` 下还有本次综合的四个产物 `tpu_top.v` / `tpu_top.sdc` / `tpu_top.sdf` / `tpu_top.ddc`，本讲第 4.4 节会逐一对应到产生它们的 `write` 命令。

---

## 4. 核心概念与源码讲解

`syn.tcl` 虽然只有 67 行，但可以清晰地切成四段。本讲按这四段拆成四个最小模块：

1. **工艺库设置**（第 4–11 行）：告诉 DC 用哪个工艺库。
2. **读入、展开与链接**（第 14–23 行）：把 RTL 读进来、展成 GTECH、解决重名、链上库。
3. **编译与优化选项**（第 25–43 行）：`compile_ultra` 及其前后处理。
4. **报告与输出产物**（第 45–65 行）：生成报告、写出网表与约束。

### 4.1 工艺库设置（search_path / link_library / target_library）

#### 4.1.1 概念说明

综合要「映射到具体工艺」，就必须先告诉 DC：① 去哪个目录找库文件；② 把逻辑映射进哪个库（目标库）；③ 解析已实例化单元引用时去查哪些库（链接库）。这三件事由三个 `set_app_var` 变量控制。本设计用的是 **Nangate Open Cell Library**——学术界广泛使用的开源 45nm 标准单元库。

库文件名 `NangateOpenCellLibrary_ss0p95vn40c` 不是随便起的，它编码了该库的 **PVT 角点**（Process 工艺 / Voltage 电压 / Temperature 温度）：

- `ss` = slow-slow（NMOS 和 PMOS 都取慢工艺角）；
- `0p95` = 0.95 V 电源电压；
- `n40c` = −40 °C。

「慢工艺 + 低电压 + 低温」是延迟较坏的组合之一（低温下 PMOS 反偏退化使某些角变慢），**综合时盯住较坏的角点，是为留余量**——综合在这套库下能跑过，换到典型角就更稳。这种「worst-case 综合」是数字后端的常规做法。

#### 4.1.2 核心流程

设置工艺库的顺序与职责：

1. `search_path` ← 指向存放 `.db`（二进制 Liberty）文件的目录，DC 找库/文件时就搜这里。
2. `target_library` ← **目标库**，即综合要把逻辑映射进去的那套标准单元库（综合的「终点工艺」）。
3. `link_library` ← **链接库**，用于解析设计中已经实例化的单元引用；开头的 `*` 表示「先在当前设计的内存里找，找不到再去后面的库找」。
4. 建一个本地设计库 `work`（`define_design_lib`），作为 `analyze` 产物（中间表示）的存放处。

#### 4.1.3 源码精读

[syn/scripts/syn.tcl:2-7](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L2-L7) —— 设置顶层设计名与三个库变量：

```tcl
set design tpu_top
set_app_var search_path "/home/standard_cell_libraries/NangateOpenCellLibrary_PDKv1_3_v2010_12/lib/Front_End/Liberty/NLDM"
set_app_var link_library "* NangateOpenCellLibrary_ss0p95vn40c.db"
set_app_var target_library "NangateOpenCellLibrary_ss0p95vn40c.db"
```

要点：

- 第 2 行把字符串 `tpu_top` 存进 Tcl 变量 `design`，后面 `analyze ../rtl/${design}.v` 等处复用它，换设计只改这一行。
- 第 4 行 `search_path` 指到 Nangate 的 `Liberty/NLDM` 目录。**NLDM = Non-Linear Delay Model**，是 Liberty 库里用二维查表（输入转换时间 × 输出负载）描述单元延迟的模型。
- 第 5 行 `link_library` 的值 `"* NangateOpenCellLibrary_ss0p95vn40c.db"` 是一个 **空格分隔的列表**：第一个元素 `*` 是「内存中当前设计」，第二个是工艺库 `.db`。DC 解析引用时按此顺序查。
- 第 6 行 `target_library` 只写库名（不带 `*`，因为目标库就是纯映射终点）。注意它和 `link_library` 指向同一个 `.db`，这对单一工艺的综合是常态。

[syn/scripts/syn.tcl:9-11](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L9-L11) —— 建工作库：

```tcl
sh rm -rf work
sh mkdir -p work
define_design_lib work -path ./work
```

`sh` 表示把命令丢给底层 shell 执行：每次综合先把 `work/` 清干净，避免上次残留干扰。`define_design_lib work -path ./work` 把逻辑名 `work` 绑到 `./work` 目录——你能在仓库里看到 `syn/work/` 下确实躺着 `tpu_top-verilog.syn`、`systolic-verilog.syn` 等 `analyze` 的中间产物文件。

#### 4.1.4 代码实践

**实践目标**：理解库名编码的 PVT 信息，并区分三个库变量。

**操作步骤**：

1. 打开 [syn.tcl:4-7](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L4-L7)。
2. 把 `NangateOpenCellLibrary_ss0p95vn40c` 拆成「库族 + PVT」两部分，写下来。
3. 想象把角点换成 `tt1p1v25c`（典型工艺、1.1V、25°C），判断：相比当前脚本，这个角点下综合出的网表时序会偏「更乐观」还是「更悲观」？

**需要观察的现象 / 预期结果**：`tt` 角延迟更小，关键路径更短，slack 会变大（更乐观）。所以用 `ss0p95vn40c` 这种坏角综合，是在刻意给自己「出难题」留余量。本步骤为推理练习，无需运行工具（待本地验证：若你有 Nangate 多角点库，可分别综合对比 `synth_qor.rpt` 的 Critical Path Slack）。

#### 4.1.5 小练习与答案

**练习 1**：`link_library` 开头的 `*` 是什么意思？去掉它会怎样？
**答案**：`*` 代表「当前设计内存」。有了它，DC 解析引用时先在当前设计里找，再查外部库；这对解析设计内部互相例化的子模块很关键。去掉后，DC 只会去 `.db` 里找引用，可能把本应来自当前设计的子模块当成找不到的外部单元，导致 `link` 报 `unresolved reference`。

**练习 2**：`target_library` 与 `link_library` 在本脚本里指向同一个 `.db`，这是巧合吗？
**答案**：不是巧合，是单工艺综合的常态。`target_library` 决定「综合映射到哪个工艺」，`link_library` 决定「解析既有引用查哪些库」；单一标准单元工艺下二者自然相同。多工艺（如含硬宏 IP）时才会不同。

---

### 4.2 读入、展开与链接（analyze / elaborate / uniquify / link）

#### 4.2.1 概念说明

库设好后，下一步把你的 RTL「喂」给 DC。这一步不是一条命令，而是经典的三连：**`analyze`（读入解析）→ `elaborate`（展成 GTECH）→ `link`（链接解析）**。三者各管一段：

- `analyze`：把 Verilog 文件读进来，做语法检查，把结果存进工作库（`work`）。它只解析、不构建。
- `elaborate`：从指定顶层出发，递归展开模块例化、求值 `parameter`/`localparam`、把行为级 RTL 翻译成与工艺无关的 **GTECH** 中间网表。**参数在这里被「冻结」成具体数值。**
- `uniquify`：给被多次例化的同一模块生成唯一名字，解决「同名多实例」问题。
- `link`：把 GTECH 里所有单元引用与 `link_library`/`target_library` 对照解析，确保没有悬空引用；这是 `compile` 前的最后一道完整性检查。

#### 4.2.2 核心流程

```
analyze ../rtl/tpu_top.v   ──►  语法 OK？ 存入 work/
        │
elaborate tpu_top          ──►  递归展开、冻结参数、生成 GTECH
        │
uniquify                   ──►  每个实例得唯一名（命名风格 %s_mydesign_%d）
        │
current_design + link      ──►  设顶层、解析所有引用 → 可编译状态
```

一个关键证据链：综合后的模块名（见 [synth_area.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt)）是 `systolic_ARRAY_SIZE8_SRAM_DATA_WIDTH32_DATA_WIDTH8`、`quantize_ARRAY_SIZE8_SRAM_DATA_WIDTH32_DATA_WIDTH8_OUTPUT_DATA_WIDTH16` 这种「模块名 + 参数值」的形式。这正是 `elaborate` 冻结参数的铁证——它在 [u1-l4](u1-l4-parameterization-fixedpoint.md) 里讲过的 `ARRAY_SIZE=8`、`DATA_WIDTH=8` 等参数，在综合时被钉死成了这些数字后缀。

#### 4.2.3 源码精读

[syn/scripts/syn.tcl:14-23](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L14-L23) —— 读入、展开、去重、链接：

```tcl
analyze -library work -format verilog ../rtl/${design}.v
elaborate $design -lib work

# Solve Multiple Instance
set uniquify_naming_style "%s_mydesign_%d"
uniquify

# link the design
current_design $TOPLEVEL
link
```

逐行：

- 第 14 行 `analyze ... -format verilog ../rtl/${design}.v`：读 `../rtl/tpu_top.v`，注意它走的是 **上一级目录的 `rtl/`**——这印证了 [u1-l2](u1-l2-repo-structure.md) 的结论：`rtl/` 是权威综合输入，仿真目录的副本不是。`-library work` 指定解析结果存进 `work` 库。
- 第 15 行 `elaborate $design`：从 `tpu_top` 出发展开整个设计。
- 第 18–19 行设置去重命名风格并执行 `uniquify`。命名模板 `%s_mydesign_%d` 含义：`%s`=原名、固定串 `_mydesign_`、`%d`=序号。被多次例化的模块（例如 `systolic` 里 64 个 cell 用到的子结构、或多个加法器实例）会得到各自唯一的名字，方便 DC 与后续工具区分。注释 `# Solve Multiple Instance` 直接点明了目的。
- 第 22 行 `current_design $TOPLEVEL`：把当前设计设为顶层。

> **一个值得留意的细节**：脚本第 2 行定义的是变量 `design`，但第 22 行引用的却是 `$TOPLEVEL`，而 **本脚本以及整个 `syn/` 目录里没有任何一处 `set TOPLEVEL ...`**（已用全文检索确认）。这意味着 `TOPLEVEL` 很可能来自 DC 启动时自动读取的 `.synopsys_dc.setup` 文件——而该 setup 文件并未提交到本仓库（**待确认**）。在没有该 setup 的环境下，`$TOPLEVEL` 在 Tcl 里会对未定义变量报错。这是真实工程脚本里常见的「依赖未入库的环境配置」现象，复现时需留意。

- 第 23 行 `link`：解析全部引用。`link` 失败（出现 unresolved cell）会让后续 `compile` 的结果不可信，所以必须在 `compile` 前确保它干净通过。

#### 4.2.4 代码实践

**实践目标**：用综合产物反推「参数在 elaborate 阶段被冻结」这一事实。

**操作步骤**：

1. 打开 [synth_area.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt) 的层次表（约第 40–44 行）。
2. 找到 `systolic`、`quantize`、`write_out`、`systolic_controll` 这几行，记录它们在报告里的完整名字。
3. 对照 [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) 里这几个模块例化时传入的 `ARRAY_SIZE` / `DATA_WIDTH` / `OUTPUT_DATA_WIDTH` 取值，验证名字后缀与之一一对应。

**预期结果**：例如报告里 `quantize` 全名为 `quantize_ARRAY_SIZE8_SRAM_DATA_WIDTH32_DATA_WIDTH8_OUTPUT_DATA_WIDTH16`，正好对应 [u1-l4](u1-l4-parameterization-fixedpoint.md) 讲过的参数取值；而 `write_out` 名字里只带 `ARRAY_SIZE8_OUTPUT_DATA_WIDTH16`，说明它例化时只传了这两个参数。这就是 `elaborate` 把参数钉死的物证。

#### 4.2.5 小练习与答案

**练习 1**：`analyze` 和 `elaborate` 能合并成一条命令吗？为什么本脚本要分开写？
**答案**：DC 也提供 `analyze`+`elaborate` 合一的便捷读入方式，但分开写更灵活、更利于排错：`analyze` 先做纯语法检查并缓存到 `work`，语法错能立即定位到文件；`elaborate` 再做展开与参数求值。分开后，大设计还可以分别 `analyze` 多个文件再统一 `elaborate` 顶层。

**练习 2**：如果不执行 `uniquify`，后续可能遇到什么问题？
**答案**：同一模块被多次例化时，若不去重，DC 在做层次相关优化、生成报告、或写给某些后端工具时，可能因「同名多实例」产生歧义或告警（Multiple Instance 警告）。`uniquify` 给每个实例一个唯一名字，从根上消除这种歧义。

---

### 4.3 编译与优化选项（compile_ultra 及前后处理）

#### 4.3.1 概念说明

这是整个综合里最吃算力的一步：`compile_ultra` 把 GTECH 映射到 Nangate 标准单元，并反复优化以满足 `cons.tcl` 给定的时序/面积目标。理解这一段，要抓住三组概念：

- **综合前的「净化」设置**：`set_fix_multiple_port_nets` 让网表更干净，便于后端；`set case_analysis_with_logic_constants` 让常量在分析中传播。
- **约束加载与再链接**：`source cons.tcl` 注入时钟周期等约束；随后再 `link` 一次确保约束施加在完整设计上。
- **`compile_ultra` 的选项**：每面旗子都改变优化行为。本讲重点讲 `-gate_clock` / `-exact_map` / `-no_autoungroup` / `-no_seq_output_inversion` / `-no_boundary_optimization`。
- **综合后的清理**：`remove_unconnected_ports` 删掉优化残留的悬空端口，产出干净网表。

`compile_ultra` 优化时所对照的时间预算，可由 `cons.tcl` 算出：时钟周期 \(T_{\text{clk}} = 3.01\,\text{ns}\)，时钟不确定性 \(T_{\text{unc}} = 0.20\,\text{ns}\)，则留给逻辑实际可用的时间约为

\[
T_{\text{logic}} = T_{\text{clk}} - T_{\text{unc}} = 3.01 - 0.20 = 2.81\,\text{ns}.
\]

（I/O 延迟还会进一步切掉边界预算，时序细读留到 [u5-l2](u5-l2-constraints-qor.md)。）

#### 4.3.2 核心流程

```
[综合前设置]
 set case_analysis_with_logic_constants true
 set_fix_multiple_port_nets -feedthroughs -outputs -constants -buffer_constants
        │
[约束] check_design → source cons.tcl → link → check_design/check_timing 落盘
        │
[门控风格] set_clock_gating_style -max_fanout 10
        │
[核心编译]
 compile_ultra -gate_clock -exact_map -no_autoungroup \
               -no_seq_output_inversion -no_boundary_optimization
        │
[综合后清理] remove_unconnected_ports（两遍：普通 + -blast_buses）
```

#### 4.3.3 源码精读

[syn/scripts/syn.tcl:25-31](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L25-L31) —— 综合前设置与约束加载：

```tcl
# before synthesis settings
set case_analysis_with_logic_constants true
set_fix_multiple_port_nets -feedthroughs -outputs -constants -buffer_constants

check_design
source ./cons/cons.tcl
link
```

- 第 26 行让 DC 在做时序分析时，把接到常量（0/1）上的逻辑按常量传播处理，得到更真实的路径状态。
- 第 27 行 `set_fix_multiple_port_nets` 是一条 **网表卫生（netlist hygiene）** 命令：它禁止「一条内部 net 同时驱动多个端口/常量」等情形，通过插入缓冲或拆分 net 来隔离。四个开关：`-feedthroughs`（打断「输入直连输出」的穿透 net）、`-outputs`（不让单 net 驱动多个输出端口）、`-constants`（隔离被常量驱动的 net）、`-buffer_constants`（对常量 net 插缓冲）。结果是网表对布局布线更友好（详见第 5 节实践）。
- 第 29 行 `check_design` 在加约束前做一次设计体检；第 30 行 `source ./cons/cons.tcl` 注入时钟/I/O 延迟/面积等约束（见 [cons.tcl:4-35](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L4-L35)）；第 31 行再 `link` 一次，确保约束施加在完整解析的设计上。

[syn/scripts/syn.tcl:33-39](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L33-L39) —— 体检落盘与核心编译：

```tcl
####check design####
check_design > ./log/check_design.log
check_timing > ./log/check_timing.log

set_clock_gating_style -max_fanout 10

compile_ultra -gate_clock -exact_map -no_autoungroup -no_seq_output_inversion -no_boundary_optimization
```

- 第 34–35 行把 `check_design` / `check_timing` 的输出写进 `log/`，便于事后核查约束是否齐全、有无遗留问题。
- 第 37 行配置 **时钟门控（clock gating）** 的插入风格，`-max_fanout 10` 限制一个门控单元驱动的寄存器扇出上限为 10。
- 第 39 行 `compile_ultra` 是核心。各选项含义：

  | 选项 | 作用 |
  | --- | --- |
  | `-gate_clock` | 允许 DC 自动插入门控时钟单元（把「寄存器使能」转成「时钟门控」以降功耗） |
  | `-exact_map` | 强制精确映射到单元，不做近似替换 |
  | `-no_autoungroup` | **禁止自动打平层次**，保留 `systolic`/`quantize`/`write_out` 等模块边界 |
  | `-no_seq_output_inversion` | 不对时序单元（触发器）输出做取反优化 |
  | `-no_boundary_optimization` | **不做跨模块边界优化**，各模块独立优化 |

  `-no_autoungroup` 与 `-no_boundary_optimization` 是一对「保守」组合：它们刻意放弃一部分跨边界优化机会，换取 **可读、可调试、层次清晰的网表**。证据就在 [synth_area.rpt:42-44,239](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt#L42-L239)：综合后 `systolic`、`quantize`、`write_out`、`systolic_controll`、`addr_sel` 仍作为独立层次节点存在，各自有面积统计。

> **一个反直觉但真实的观察**：脚本既开了 `set_clock_gating_style`、`compile_ultra` 又带了 `-gate_clock`，但 [clock_gating.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/clock_gating.rpt) 显示 **门控寄存器数 = 0，全部 2828 个寄存器 100% 未门控**。也就是说：**工具「被允许」插门控，但最终「选择不插」**。常见原因是寄存器的使能信号不满足门控插入的判据（如使能逻辑过于复杂、或时序/面积代价不划算）。这是一个很好的教训——**开启某项优化 ≠ 该优化一定会发生**，要以报告为准。

`compile_ultra` 的代价不小。[synth_qor.rpt:60-67](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L60-L67) 记录：Mapping Optimization 耗时约 2008 秒，整体编译 CPU 时间约 2035 秒（墙上时间约 2084 秒，≈35 分钟）。规模上，[synth_qor.rpt:24-34](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L24-L34) 显示叶单元 54885 个、其中缓冲/反相器 33656 个（占六成以上，说明工具为修时序插了大量 buffer）、时序单元 2828 个。

[syn/scripts/syn.tcl:41-43](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L41-L43) —— 综合后清理悬空端口：

```tcl
# remove dummy ports
remove_unconnected_ports [get_cells -hierarchical *]
remove_unconnected_ports [get_cells -hierarchical *] -blast_buses
```

- 第 42 行移除层次内所有单元上 **没连任何东西的端口**（dummy ports）——这些是优化过程中残留的、对功能无影响但会让网表冗长的端口。
- 第 43 行加 `-blast_buses`：把总线端口拆成位级再清理，确保连总线位级别的悬空端口也被去掉。两遍合起来产出一份干净、无悬空的网表交给后端。

#### 4.3.4 代码实践

**实践目标**：把 `compile_ultra` 的五个选项与综合报告里的现象对应起来，理解「保守优化」的可观测后果。

**操作步骤**：

1. 对照 [syn.tcl:39](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L39) 的 `-no_autoungroup`，打开 [synth_area.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_area.rpt) 层次表，确认 `systolic` 等子模块仍以独立层次出现（未被展平）。
2. 对照 `-gate_clock`，打开 [clock_gating.rpt](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/clock_gating.rpt)，记录「门控寄存器数 / 总寄存器数」。
3. 在 [synth_qor.rpt:28-29](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L28-L29) 找到 Buf/Inv Cell Count（33656），计算其占 Leaf Cell Count（54885）的比例。

**预期结果**：① 层次保留；② 门控寄存器 0 / 2828；③ buffer+反相器占比约 \(33656 / 54885 \approx 61.3\%\)——说明工具为了修 3.01ns 的紧张时序，插入了大量缓冲器（这也是为什么 Buf/Inv Area 在面积里占比很高）。本步骤为源码/报告阅读，无需运行工具。

#### 4.3.5 小练习与答案

**练习 1**：`-no_autoungroup` 和 `-no_boundary_optimization` 都让优化「更保守」。如果去掉它们（允许自动展平与跨边界优化），最可能带来什么变化？
**答案**：工具获得更大优化自由度，通常能把面积/时序做得稍好（跨边界共享逻辑、重定时等），代价是 **网表层次被打散、可读性下降、调试与 ECO 更困难**，且与某些需要保留层次的下游流程不兼容。本设计选择保守，是工程上「可维护性优先」的取舍。

**练习 2**：脚本开了 `-gate_clock`，为什么 `clock_gating.rpt` 里门控单元仍是 0？
**答案**：`-gate_clock` 只是「允许」插门控，是否真插取决于 DC 对每个寄存器使能信号的判据（复杂度、功耗收益、时序/面积代价）。本设计中各寄存器的使能不满足插入判据，或收益不足，于是 DC 一个都没插。结论：优化开关是「授权」而非「强制」，必须查报告确认实际效果。

**练习 3**：为什么 `remove_unconnected_ports` 要跑两遍、第二遍加 `-blast_buses`？
**答案**：第一遍按「端口」粒度清理；但总线端口（如 `data[31:0]`）作为一个整体可能部分位悬空、部分位在用，第一遍不会动它。`-blast_buses` 把总线炸成单 bit 后，才能逐位清掉那些悬空的 bit，做到彻底无残留。

---

### 4.4 报告与输出产物（report_* / write_*）

#### 4.4.1 概念说明

综合跑完后，DC 不会自动把成果交给你——你要主动让它 **出报告** 和 **写产物**。这两类命令的目的不同：

- **报告（`report_*`）**：给 **人** 看，用来判断综合质量（时序过了没、面积多大、有多少违例），写到 `syn/report/*.rpt`。
- **写出（`write_*`）**：给 **下游工具** 用，写到 `syn/output/`，包括门级网表 `.v`、约束 `.sdc`、延迟 `.sdf`、DC 数据库 `.ddc`。

四个输出产物各自的去向：

| 产物 | 命令 | 格式含义 | 下游谁用 |
| --- | --- | --- | --- |
| `tpu_top.v` | `write -format verilog` | 门级 Verilog 网表 | 布局布线 ICC2 / 门级仿真 |
| `tpu_top.sdc` | `write_sdc` | Synopsys Design Constraints（时钟、I/O 延迟） | 后端 PnR 的时序约束输入 |
| `tpu_top.sdf` | `write_sdf` | Standard Delay Format（单元/互连延迟） | 门级仿真反标延迟 |
| `tpu_top.ddc` | `write -f ddc` | DC 二进制数据库 | 重新打开设计继续调 |

#### 4.4.2 核心流程

```
[出报告]（→ report/*.rpt）
 report_area -hier          面积（层次化）
 report_power -hier         功耗
 report_cell                用到的单元清单
 report_qor                 综合质量总览（时序/面积/单元数）
 report_resources           DesignWare 资源共享情况
 report_timing -delay min   保持时间（hold）路径
 report_timing -delay max   建立时间（setup）路径
 report_timing -path full   最差路径细节
 report_constraint          约束违例
        │
[写约束/延迟]（→ output/*.sdc *.sdf）
 write_sdc / write_sdf
        │
[改名 + 输出选项]
 change_names（no_case / verilog 两条规则）
 set verilogout_no_tri / verilogout_equation
        │
[写网表/数据库]（→ output/*.v *.ddc）
 write -hierarchy -format verilog
 write -f ddc -hierarchy
```

注意「改名」放在 `write` **之前**：先把所有对象名改成大小写不敏感且 Verilog 合法，再写网表，避免下游因大小写或非法字符出问题。

#### 4.4.3 源码精读

[syn/scripts/syn.tcl:45-53](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L45-L53) —— 出报告：

```tcl
report_area -hier > ./report/synth_area.rpt
report_power -hier > ./report/synth_power.rpt
report_cell > ./report/synth_cells.rpt
report_qor  > ./report/synth_qor.rpt
report_resources > ./report/synth_resources.rpt
report_timing -delay min -max_paths 4 > ./report/synth_Hold.rpt 
report_timing -delay max -max_paths 4 > ./report/synth_Setup.rpt
report_timing -path full -delay max -max_paths 1 -nworst 1 -significant_digits 4 > ./report/synth_timing.rpt
report_constraint -all_violators > ./report/report_violation.rpt
```

- `-hier`：让 `report_area` / `report_power` 按模块层次展开（这正是第 4.3 节看到 `systolic` 占 89.6% 面积的来源）。
- `report_timing` 的三连：`-delay min` 看 **hold**（保持时间，最快角）、`-delay max` 看 **setup**（建立时间，最慢角）、`-path full` 输出 **单条最差路径的完整细节**（`-nworst 1` 取最差 1 条端点、`-max_paths 1` 取 1 条路径、`-significant_digits 4` 保留 4 位小数）。
- `report_constraint -all_violators` 列出所有违例。

> **又一个真实工程的「坑」——脚本与已提交报告的命名对不上。** 仔细比对会发现：脚本写的是 `synth_Hold.rpt` / `synth_Setup.rpt` / `synth_timing.rpt` / `report_violation.rpt`，而仓库 `syn/report/` 下实际提交的文件却是 `synth_hold.rpt` / `synth_setup.rpt` / `synth_time.rpt` / `synth_violation.rpt`（大小写不同、`timing`↔`time`、前缀不同）；此外 `clock_gating.rpt` 在 `syn/report/` 里存在，但 `syn.tcl` 中并没有写它的 `report` 命令。这说明 **当前提交的 `syn.tcl` 与 `report/` 里的报告并非同一次运行的产物**，脚本或报告在提交前被各自改过。复现时若发现「按脚本跑完，文件名对不上」，原因就在这里——这是真实工程里常见的「版本漂移」，以实际文件为准即可。

[syn/report/synth_resources.rpt:1-15](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_resources.rpt) —— 一份「空的」资源报告：

```
No resource sharing information to report.
No implementations to report
No multiplexors to report
```

`report_resources` 汇报的是 **DesignWare 资源共享**（比如多个加法器共享一个大加法器、多个乘法共享硬件）和算子实现情况。这里全部为「无」，说明 DC 没有保留下可报告的共享资源或复用器。注意这并不代表优化没干活——[synth_qor.rpt:62](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/report/synth_qor.rpt#L62) 显示 Resource Sharing 阶段确实花了约 13.79 秒 CPU，只是最终没有形成可报告的共享结构。

[syn/scripts/syn.tcl:55-65](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L55-L65) —— 写约束、改名、出网表与数据库：

```tcl
write_sdc  output/${design}.sdc 
write_sdf -version 1.0  -context verilog  output/${design}.sdf

define_name_rules  no_case -case_insensitive
change_names -rule no_case -hierarchy
change_names -rule verilog -hierarchy
set verilogout_no_tri	 true
set verilogout_equation  false

write -hierarchy -format verilog -output output/${design}.v 
write -f ddc -hierarchy -output output/${design}.ddc   
```

- 第 55–56 行：先写 `.sdc`（约束）与 `.sdf`（延迟，`-version 1.0` 指定 SDF 版本，`-context verilog` 使其适配 Verilog 仿真反标）。
- 第 58–60 行：`define_name_rules no_case -case_insensitive` 定义一条「大小写不敏感」命名规则，再用 `change_names` 按此规则（以及随后的 `verilog` 规则，确保标识符合法）**逐层改名**。这一步对 Verilog 网表尤其重要——Verilog 标识符大小写敏感，但某些下游工具不敏感，统一改名避免 `Cell` 与 `cell` 撞名。
- 第 61 行 `set verilogout_no_tri true`：写网表时不输出 tri 类型 net（转成普通 wire）；第 62 行 `set verilogout_equation false`：用赋值语句而非方程形式输出逻辑。
- 第 64 行 `write -hierarchy -format verilog`：**写出带层次的门级 Verilog 网表** `output/tpu_top.v`——这是交给 PnR 的主交付物。`-hierarchy` 与第 4.3 节的 `-no_autoungroup` 呼应：保住层次才需要带层次地写。
- 第 65 行 `write -f ddc -hierarchy`：写出 DC 二进制数据库 `output/tpu_top.ddc`，下次可用 `read_ddc` 直接载入，免去重新综合。

最后 [syn.tcl:67](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L67) 的 `exit` 结束 `dc_shell`。

#### 4.4.4 代码实践

**实践目标**：建立「命令 ↔ 产物文件」的牢固映射，并学会看一份「空报告」传达的信息。

**操作步骤**：

1. 打开 `syn/output/` 目录，确认存在 `tpu_top.v` / `tpu_top.sdc` / `tpu_top.sdf` / `tpu_top.ddc` 四个文件。
2. 回到 [syn.tcl:55-65](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L55-L65)，为这四个文件各找到产生它的那一条命令。
3. 用文本方式打开 `syn/output/tpu_top.sdc` 的前几行，确认里面出现 `create_clock` 字样——它正是 `cons.tcl` 里 `create_clock -name clk -period 3.01`（[cons.tcl:4](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/cons/cons.tcl#L4)）经过综合后传给后端的形态。

**预期结果**：`.v`←`write -format verilog`；`.sdc`←`write_sdc`；`.sdf`←`write_sdf`；`.ddc`←`write -f ddc`。`.sdc` 里能找到时钟定义，证明约束被正确导出。本步骤为文件阅读，无需运行工具。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `change_names` 必须在 `write -format verilog` **之前**执行？
**答案**：`write` 是按当前内存里的对象名落盘的。若先写网表再改名，写出去的网表仍是旧名，下游会拿到大小写可能冲突或非法的标识符。先改名、再写出，才能保证落盘的网表已经是大小写不敏感且 Verilog 合法的名字。

**练习 2**：`.sdc` 和 `.sdf` 都描述「延迟相关」信息，它们有何不同、各给谁用？
**答案**：`.sdc` 描述 **设计意图约束**（时钟周期、I/O 延迟、false path 等），没有具体单元延迟，主要给 **PnR 工具** 做布局布线时的时序驱动；`.sdf` 描述 **具体延迟数值**（每个 cell/互连的真实延迟），给 **门级仿真器** 反标，用于仿出真实时序下的功能。前者是「目标」，后者是「结果」。

**练习 3**：`synth_resources.rpt` 报告「No resource sharing / No implementations / No multiplexors」，是否意味着综合失败？
**答案**：否。它只表示 DC 没有保留下可报告的 DesignWare 资源共享结构或复用器。综合是否成功要看 `synth_qor.rpt`（时序是否满足、有无违例）和 `check_design.log`。事实上本设计综合正常完成、规模达 5 万余叶单元，资源报告为空只是结构特征，不是错误。

---

## 5. 综合实践

把全讲串起来，完成下面这个贯穿任务（本讲的核心实践）。

### 实践目标

独立还原 `syn.tcl` 的完整命令时序，并讲清两条容易被忽略的「网表卫生」命令到底在干什么——这正是本讲规格里要求的实践任务。

### 操作步骤

**第一部分：命令时序表**

1. 通读 [syn/scripts/syn.tcl](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl)（共 67 行）。
2. 从「读入 RTL」到「产出 `.v` / `.sdc` / `.ddc`」，按出现顺序列出关键命令，做成下面这张表（自行填满）：

   | 序号 | 命令 | 所属阶段 | 作用（一句话） | 产出/影响 |
   | --- | --- | --- | --- | --- |
   | 1 | `set_app_var target_library ...` | 工艺库设置 | 指定映射终点工艺库 | — |
   | 2 | `analyze ...` | 读入 | 解析 RTL 语法 | `work/*.syn` |
   | 3 | `elaborate ...` | 展开 | … | GTECH |
   | 4 | `uniquify` | 去重 | … | … |
   | 5 | `link` | 链接 | … | — |
   | … | `compile_ultra ...` | 编译优化 | … | 映射后的网表 |
   | … | `write ...` | 输出 | … | `output/tpu_top.v` |
   | … | `write_sdc` / `write -f ddc` | 输出 | … | `output/*.sdc` `*.ddc` |

3. 在表上标注：哪些命令产生 **给人看的报告**（`report_*`），哪些产生 **给下游工具用的产物**（`write_*`）。

**第二部分：解释两条卫生命令**

4. 针对 [syn.tcl:27](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L27) 的 `set_fix_multiple_port_nets -feedthroughs -outputs -constants -buffer_constants`，用一段话说明它在综合里起什么作用、为什么后端工具喜欢它。
5. 针对 [syn.tcl:42-43](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/scripts/syn.tcl#L42-L43) 的 `remove_unconnected_ports`（两遍），说明它在综合里起什么作用、为什么要跑两遍。

### 需要观察的现象 / 预期结果

**`set_fix_multiple_port_nets` 的作用**：它禁止「一条 net 同时驱动多个输出端口、做 feedthrough 穿透、或被常量驱动」等情形，通过插入缓冲、拆分 net 来隔离。作用是 **让每个端口由独立的、干净的 net 驱动**，避免一条 net 同时连到多个顶层端口。这在后端（尤其布局布线与时序签收）里很关键：多端口共享 net 会让缓冲插入、ECO、时序分析变复杂；隔离后，后端工具能独立处理每个端口的负载与路径。一句话：它是为 **网表的可综合性、可布线性与可签收性** 服务的预处理，不改变功能，只改结构卫生。

**`remove_unconnected_ports` 的作用**：综合优化后，一些单元会留下「没连任何东西」的悬空端口（dummy ports），它们对功能无影响但会让网表冗长、可能触发下游工具告警。该命令逐层次删掉这些悬空端口。跑两遍的原因：第一遍按端口粒度清理；第二遍加 `-blast_buses` 把总线端口炸成单 bit，清掉那些「部分位悬空」的总线端口，做到彻底干净。一句话：它产出 **无悬空端口的精简网表**，是写给 PnR 的标准收尾动作。

> 这两条命令的共同主题是 **「网表卫生」**：它们都不改变电路功能，只让网表结构更干净、更利于下游工具处理。功能正确性由 RTL 与 `compile_ultra` 保证；能否顺利走完后端，往往取决于这些卫生设置。

### 进阶（可选，待本地验证）

如果你有 DC 与 Nangate 库的环境，可尝试：

- 备份 `syn.tcl`，注释掉第 27 行 `set_fix_multiple_port_nets`，重新跑 `./run`，用 `diff` 对比新旧 `output/tpu_top.v`：观察是否出现「一条 net 驱动多端口」的结构。
- 注释掉第 42–43 行 `remove_unconnected_ports`，对比新旧网表的行数与悬空端口数量。
- 在不改动 RTL 的前提下，仅把 `cons.tcl` 的时钟周期从 3.01ns 收紧到 2.5ns 重跑，观察 `synth_qor.rpt` 中 Buf/Inv Cell Count 与 Critical Path Slack 的变化（这是 [u5-l2](u5-l2-constraints-qor.md) 的伏笔）。

## 6. 本讲小结

- **综合 = 把 RTL 翻译并优化成某工艺标准单元网表**。`syn.tcl` 用一段 67 行 Tcl 把这件事切成四段：设库 → 读入展开链接 → 编译优化 → 报告与输出。
- **工艺库三件套**：`search_path`（去哪找库）、`target_library`（映射到哪个工艺，终点）、`link_library`（解析引用查哪些库，开头 `*` 指当前设计）。本设计用 Nangate 45nm 的 `ss0p95vn40c` 坏角点库做保守综合。
- **读入三连** `analyze → elaborate → link`：`elaborate` 把 `ARRAY_SIZE=8` 等参数冻结成模块名后缀（如 `systolic_ARRAY_SIZE8_...`），`uniquify` 给多实例唯一名，`link` 确保无悬空引用。
- **`compile_ultra` 的选项是「授权」不是「强制」**：开了 `-gate_clock` 却一个门控单元都没插（2828 个寄存器 100% 未门控），必须以报告为准。
- **`-no_autoungroup` + `-no_boundary_optimization`** 是保守组合：放弃部分跨边界优化，换取 `systolic`/`quantize`/`write_out` 等清晰保留的层次（`systolic` 占 89.6% 面积）。
- **产物各有去处**：`report_*` 给人（`.rpt`），`write_*` 给下游——`tpu_top.v`（网表→PnR）、`tpu_top.sdc`（约束→PnR）、`tpu_top.sdf`（延迟→门仿）、`tpu_top.ddc`（数据库→重载）。脚本里还藏着两处真实工程的「坑」：`$TOPLEVEL` 未在本脚本定义、报告文件名与脚本写出的名字存在版本漂移。

## 7. 下一步学习建议

- 下一讲 **[u5-l2 时序约束 cons.tcl 与综合结果解读](u5-l2-constraints-qor.md)** 会钻进本讲只点到为止的 `cons.tcl`（`create_clock -period 3.01`、`set_clock_uncertainty 0.20`、`set_false_path`、`set_max_area 0` 等），并教你怎么读 `synth_qor.rpt` / `synth_timing.rpt` 判断时序到底过没过——届时本讲提到的「slack = 0.03、0 条违例路径、Buf/Inv 33656 个」会得到完整解释。
- 想从源头理解被综合的设计，回到 **[u1-l3 顶层模块 tpu_top](u1-l3-tpu-top-datapath.md)** 与 **[u1-l4 参数化与定点数](u1-l4-parameterization-fixedpoint.md)**：本讲的 `elaborate` 冻结参数、`systolic` 占绝大部分面积，都根植于那里的设计。
- 想继续向后看物理实现，预习 **u5-l3 ICC2 布局布线六阶段流程**：本讲产出的 `tpu_top.v` + `tpu_top.sdc` 正是 PnR 的两大输入，你会看到综合产物如何被后端工具消费。
- 若手头没有 DC license，可先读 [syn/log/check_design.log](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/log/check_design.log) 与 [syn/command.log](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/syn/command.log)，它们记录了真实运行时 DC 打印的每一条命令与告警，是无需 license 即可「旁观」一次真实综合的最好材料。
