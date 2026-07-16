# canonical 流程脚本 example_script.openfpga 全解析

## 1. 本讲目标

学完本讲，你应该能够：

- 逐行读懂 OpenFPGA 官方最典型的流程脚本 `example_script.openfpga`，说出每条命令在做什么、为什么出现在这个位置。
- 把整条流程划分成「输入 → 处理 → 输出」三大阶段，并能指出哪些命令之间存在**硬性先后顺序**（例如为什么 `repack` 必须在 `build_architecture_bitstream` 之前）。
- 把「命令的顺序」和 u2-l3 学到的 `OpenfpgaContext` 联系起来：每一步到底在 context 里**读**了什么、**写**了什么。
- 区分三种验证产物：`write_full_testbench`、`write_preconfigured_fabric_wrapper`、`write_preconfigured_testbench`，知道它们各自验证什么、代价是什么。

本讲是 u2（shell 命令）与 u3（架构文件）之后的「串讲」：前面两讲分别讲了「命令怎么注册」和「架构文件长什么样」，本讲把它们拼成一条**真实可运行**的端到端流程。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前面几讲），这里只做一句话回顾：

- **`.openfpga` 脚本**：OpenFPGA shell 的脚本模式输入文件（u2-l1）。用 `openfpga -batch -f xxx.openfpga` 逐行执行，`#` 是注释，`\` 是续行。
- **命令依赖**：命令注册时可以声明「前置命令」，运行时 shell 会检查前置命令是否已成功执行（u2-l2）。这是一种安全网，但**只检查一级、不递归**。
- **OpenfpgaContext**：贯穿全流程的全局数据中枢，命令之间靠它传递数据（u2-l3）。`const` 访问器只读、`mutable` 访问器可写。
- **两套架构文件**：VPR 架构 XML（器件结构）与 `openfpga_arch.xml`（电路级物理实现），靠同名绑定拧合（u3-l1、u3-l2）。
- **配置协议**：`cc` = scan_chain（配置链）等，决定比特流如何组织（u3-l4）。

还需要补充一个本讲会用到的关键事实：脚本里那些 `${VPR_ARCH_FILE}`、`${OPENFPGA_ARCH_FILE}` 并不是 openfpga 二进制自己认识的语法，而是 **Python 的 `string.Template` 占位符**。真正执行脚本前，`run_fpga_flow.py` 会把它们替换成真实文件路径（详见 4.1.3）。换句话说，直接把 `example_script.openfpga` 喂给 `openfpga -f` 是跑不通的——必须先经过 `run_fpga_flow.py` 的变量替换。

## 3. 本讲源码地图

本讲围绕三个官方流程脚本展开，它们是同一套流程的「不同剪裁版本」：

| 文件 | 作用 | 何时用 |
| --- | --- | --- |
| [openfpga_flow/openfpga_shell_scripts/example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga) | **全流程样板**：从 vpr 一路到比特流、Verilog、testbench、SDC | 本讲主线，逐行解析 |
| [openfpga_flow/openfpga_shell_scripts/generate_fabric_example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/generate_fabric_example_script.openfpga) | **只生成 fabric**：不生成比特流、不做 repack，只到 `write_fabric_verilog` + SDC | 只想拿 fabric 网表做后续 PnR 时 |
| [openfpga_flow/openfpga_shell_scripts/full_testbench_example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/full_testbench_example_script.openfpga) | **全流程 + full testbench**：与 example 几乎相同，但只生成 `write_full_testbench`（不生成 preconfigured） | 需要含编程阶段的完整仿真验证时 |

理解命令依赖关系时，需要对照三份命令注册模板：

| 文件 | 作用 |
| --- | --- |
| [openfpga/src/base/openfpga_setup_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h) | 注册 vpr 之后的 setup 组命令（read/link/check/fixup/build_fabric 等）及其依赖 |
| [openfpga/src/base/openfpga_bitstream_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h) | 注册 bitstream 组命令（repack/build_arch/build_fabric/write_bitstream）及其依赖 |
| [openfpga/src/base/openfpga_verilog_command_template.h](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h) | 注册 verilog 组命令（write_fabric_verilog/各 testbench）及其依赖 |

另外，理解变量替换要看流程执行器 [openfpga_flow/scripts/run_fpga_flow.py](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py)。

## 4. 核心概念与源码讲解

### 4.1 全景地图：脚本的三阶段划分与变量替换机制

#### 4.1.1 概念说明

`example_script.openfpga` 看起来是一长串命令，但它其实有非常清晰的内部结构。把 20 行左右的命令按「这步是在**收集输入**、**加工数据**还是**吐出产物**」来分，就能归成三大阶段：

- **阶段一·输入与准备（input）**：`vpr` → `read_openfpga_arch` → `read_openfpga_simulation_setting` → `link_openfpga_arch` → `check_netlist_naming_conflict` → `lut_truth_table_fixup`。这一阶段把两套架构文件和用户的 BLIF 网表都「搬进」context，并做必要的修正。
- **阶段二·加工（process）**：`build_fabric` → `repack` → `build_architecture_bitstream` → `build_fabric_bitstream`。这一阶段把「逻辑设计 + 架构」翻译成「物理 fabric 模块图 + 配置比特流」，是 OpenFPGA 真正的「核心计算」。
- **阶段三·输出（output）**：`write_fabric_bitstream` → `write_fabric_verilog` → `write_full_testbench` / `write_preconfigured_*` → `write_pnr_sdc` / `write_analysis_sdc` / `write_sdc_disable_timing_configure_ports`。这一阶段把阶段二的成果落盘成各种文件。

`generate_fabric_example_script.openfpga` 是这个结构的「瘦身版」：它砍掉了阶段二的 repack 与整个比特流子链，阶段三也只保留 fabric 网表与 SDC——印证了「三阶段」是一个可以按需裁剪的骨架，而不是死规定。

#### 4.1.2 核心流程

用一个横向流程图概括（箭头表示数据/产物向后流动）：

```
[阶段一 输入]                [阶段二 加工]                [阶段三 输出]
vpr ──┐                                                 write_fabric_bitstream
read_openfpga_arch ─┤                                   write_fabric_verilog
read_sim_setting ─┤                                    write_full_testbench
link_openfpga_arch ◄┘──► build_fabric ──► repack ──►   write_preconfigured_*
check_netlist ◄┐              │                         write_pnr_sdc
lut_tt_fixup ◄┘               └─► build_arch_bitstream  write_analysis_sdc
                                  └─► build_fabric_bitstream
                                       (两级 bitstream)
```

关键直觉：**阶段一的产物是 context 里的「只读架构 + VPR 设备数据」；阶段二把它们变成「可写的 module_graph 与 bitstream」；阶段三只读取这些数据并写文件。** 因此阶段三的命令大多用「const 执行函数」（只读 context），阶段二的命令用「mutable 执行函数」（写 context）——这一点会在 4.x.3 里用源码印证。

#### 4.1.3 源码精读：脚本里的 `${}` 变量从哪来

脚本第 3 行就出现了 `${VPR_ARCH_FILE}` 这样的占位符：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L1-L9](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L1-L9) —— 这段先用 `vpr` 跑布局布线（吃 `${VPR_ARCH_FILE}` 和 `${VPR_TESTBENCH_BLIF}`），再 `read_openfpga_arch -f ${OPENFPGA_ARCH_FILE}` 读架构、`read_openfpga_simulation_setting -f ${OPENFPGA_SIM_SETTING_FILE}` 读仿真设置。这些 `${}` 都不是 openfpga 自己解析的。

真正做替换的是流程执行器里的 `run_openfpga_shell()` 函数：

[openfpga_flow/scripts/run_fpga_flow.py:L1024-L1057](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L1024-L1057) —— 这里用 `Template(...).safe_substitute(path_variables)` 把脚本模板实例化成 `<top>_run.openfpga`，变量取值见 L1028–L1046：

| 占位符 | 来源 | 含义 |
| --- | --- | --- |
| `${VPR_ARCH_FILE}` | `args.arch_file` | VPR 架构 XML |
| `${OPENFPGA_ARCH_FILE}` | `args.openfpga_arch_file` | openfpga_arch.xml |
| `${OPENFPGA_SIM_SETTING_FILE}` | `args.openfpga_sim_setting_file` | 仿真设置 |
| `${VPR_TESTBENCH_BLIF}` | `<top_module>.blif` | Yosys 综合后的 BLIF 网表 |
| `${ACTIVITY_FILE}` | `<top>_ace_out.act` | 信号翻转率文件（供功耗估算） |
| `${REFERENCE_VERILOG_TESTBENCH}` | `<top>_output_verilog.v` | 参考基准，用于自检 |

`generate_fabric_example_script.openfpga` 里还用到 `${OPENFPGA_VPR_DEVICE_LAYOUT}`、`${OPENFPGA_VPR_ROUTE_CHAN_WIDTH}`、`${OPENFPGA_VERILOG_OUTPUT_DIR}`，它们不在上面这张表里，而是通过 `update_template_vars_from_extra_args` 从 `task.conf` 透传的额外参数注入：

[openfpga_flow/scripts/run_fpga_flow.py:L508-L513](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/scripts/run_fpga_flow.py#L508-L513) —— 把每对 `--key value` 转成大写的模板变量 `KEY=value`，所以 task.conf 里写 `--vpr_route_chan_width 20` 就会替换脚本里的 `${OPENFPGA_VPR_ROUTE_CHAN_WIDTH}`。

> 注意 `safe_substitute` 的「safe」：遇到没提供值的占位符不会报错，而是原样保留。这就是为什么脚本里有些 `${...}` 看起来「没填」也能跑——只是那一段命令会带上一个没替换的占位符（通常对应可选参数）。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「变量替换」这一步确实发生在 openfpga 之外。

**操作步骤**：

1. 用 u1-l4 的方式跑任意一个 task，例如 `run-task basic_tests/full_testbench/configuration_chain`。
2. 进入最新一次运行目录（`goto-task` 或直接找 `latest` 软链接），打开里面的 `<top>_run.openfpga`（这是替换后的真实脚本）。
3. 对比仓库里的模板 `openfpga_flow/openfpga_shell_scripts/example_script.openfpga`（或 task 指定的模板）。

**需要观察的现象**：`<top>_run.openfpga` 里的 `${VPR_ARCH_FILE}` 等已变成绝对路径，而模板里仍是占位符。

**预期结果**：确认 `.openfpga` 模板经过 `run_fpga_flow.py` 实例化后才被 `openfpga -batch -f` 执行（见 run_fpga_flow.py L1057 的 `command = [..., "-batch", "-f", ... + "_run.openfpga"]`）。若你跳过 `run_fpga_flow.py` 直接 `openfpga -f example_script.openfpga`，vpr 会拿到一个名为 `${VPR_ARCH_FILE}` 的字符串当文件名而报错。

#### 4.1.5 小练习与答案

**练习 1**：`example_script.openfpga` 里出现了多少种 `${}` 占位符？哪些一定有值、哪些可能原样保留？

**参考答案**：6 种（见 4.1.3 的表）。`VPR_ARCH_FILE`、`OPENFPGA_ARCH_FILE`、`VPR_TESTBENCH_BLIF`、`REFERENCE_VERILOG_TESTBENCH` 必有值（流程必需）；`OPENFPGA_SIM_SETTING_FILE`、`ACTIVITY_FILE` 在 task 没提供时可能被 `safe_substitute` 原样保留。

**练习 2**：为什么本讲强调「直接 `openfpga -f example_script.openfpga` 跑不通」？

**参考答案**：因为 `${...}` 是 Python `string.Template` 占位符，由 `run_fpga_flow.py` 的 `safe_substitute` 替换；openfpga 二进制本身不解析它们。必须先经流程脚本实例化。

---

### 4.2 vpr 调用：流程的第一块多米诺骨牌

#### 4.2.1 概念说明

脚本第一行 `vpr ${VPR_ARCH_FILE} ${VPR_TESTBENCH_BLIF} --clock_modeling route` 调用的不是 OpenFPGA 自己的代码，而是**内嵌的 VPR 引擎**。VPR 负责「综合后网表 → 布局布线打包」：把 BLIF 里的逻辑打包进物理逻辑块（clb/io），摆到网格上（place），再把它们用布线资源连起来（route）。`--clock_modeling route` 表示时钟网络也走普通布线资源（而不是假定有专用时钟树）。

这一步最关键的副作用是：**VPR 的结果会被保留在共享的设备 context 里**。而 `OpenfpgaContext` 继承自 VPR 的 Context（u2-l3），所以 vpr 一跑完，后续所有 OpenFPGA 命令都能直接读到 grid、rr_graph、clustering 等结果。这就是为什么 `vpr` 必须是第一条命令。

#### 4.2.2 核心流程

```
vpr 命令 ──► vpr::vpr_wrapper() ──► VPR 核心引擎(pack/place/route)
                                          │
                                          ▼
                            结果写入共享设备 context(grid/rr_graph/...)
                                          │
                                          ▼
              后续 OpenFPGA 命令直接从 context 读取(无需再跑 VPR)
```

#### 4.2.3 源码精读

`vpr` 命令的注册在 vpr_wrapper 里，注释明确点出它会「保留 VPR 结果」：

[openfpga/src/vpr_wrapper/vpr_command.cpp:L15-L24](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/vpr_wrapper/vpr_command.cpp#L15-L24) —— 注册 `vpr` 命令，执行函数绑到 `vpr::vpr_wrapper`，描述里写明「this command will keep VPR results」。对比同文件 L29–L37 的 `vpr_standalone`（「will NOT keep VPR results」），就能体会「keep results」对后续流程的决定性意义。

再看 setup 组是如何把 `vpr` 当作依赖锚点的：

[openfpga/src/base/openfpga_setup_command_template.h:L1333-L1339](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1333-L1339) —— setup 组一开场就先用 `shell.command("vpr")` 取回 vpr 命令的 id，作为后续命令依赖图的根。这印证了「vpr 是整条流程的源头」。

脚本本体第 3 行（已在 4.1.3 引用）就是这条命令的实例。`generate_fabric` 版本还在其后追加了 `--device ${OPENFPGA_VPR_DEVICE_LAYOUT} --route_chan_width ${OPENFPGA_VPR_ROUTE_CHAN_WIDTH}`，显式指定器件尺寸与通道宽度——这说明不同脚本可以给同一条 `vpr` 命令配不同参数。

#### 4.2.4 代码实践

**实践目标**：体会「vpr 之后设备 context 已就绪」。

**操作步骤**：

1. 在 `example_script.openfpga` 的 `vpr` 这一行后面、`read_openfpga_arch` 之前，**临时插入**一行 `write_gsb_to_xml --file ./gsb_dump --unique`（这是一个只依赖 `link_openfpga_arch` 的命令，会失败，见下）。
2. 换个位置：把它插到 `link_openfpga_arch` 之后，再跑一次。

**需要观察的现象**：第一种插法，shell 会报「命令依赖未满足」（因为 `write_gsb` 依赖 `link_openfpga_arch`，而它还没跑）；第二种插法可以执行。

**预期结果**：理解命令依赖是「运行时硬约束」。**注意**：本实践需要本地编译好的 openfpga 与一个可跑的 task；若无法运行，标注「待本地验证」即可，重点是把 4.2.3 的依赖声明读明白。

> 提示：GSB（General Switch Block，通用开关块）是 VPR 布线结构的基本单元，u9-l5 会专门讲，这里只借用它做依赖观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vpr` 几乎必须是脚本第一条命令？给出基于 context 的解释。

**参考答案**：因为大量 setup 命令（`link_openfpga_arch`、`check_netlist_naming_conflict`、`lut_truth_table_fixup` 等）在注册时把 `vpr` 列为依赖，而它们的数据来源正是 vpr 写入设备 context 的 grid/rr_graph/clustering 结果。vpr 没跑，这些就成了无源之水。

**练习 2**：`vpr` 与 `vpr_standalone` 的关键区别是什么？为什么流程脚本用的是前者？

**参考答案**：`vpr` 保留 VPR 结果到共享 context，供后续命令复用；`vpr_standalone` 不保留。流程脚本要继续 build_fabric 等步骤，必须用保留结果的 `vpr`。

---

### 4.3 架构读取与链接：read → link → check → fixup

#### 4.3.1 概念说明

vpr 跑完后，context 里只有「VPR 视角的器件」（结构）。但 OpenFPGA 还需要「电路级物理实现」（用什么电路搭这些结构），那来自 `openfpga_arch.xml`。这一阶段做四件事：

1. **`read_openfpga_arch`**：把 `openfpga_arch.xml` 解析成 `openfpga::Arch` 对象，写入 context 的 `arch_` 分区（只读，u3-l2 讲过它解析后冻结）。
2. **`read_openfpga_simulation_setting`**：读仿真设置（操作电压、温度等），供后续 SPICE/Verilog 仿真用。
3. **`link_openfpga_arch`**：**桥梁步骤**——把上一步的 Arch 与 vpr 产生的设备数据「对账绑定」，建立 circuit model ↔ pb graph、rr graph switch/segment 的映射（同名绑定，u3-l1），并读入 activity 文件。这是 vpr 与 build_fabric 之间真正的「接线」。
4. **`check_netlist_naming_conflict` + `lut_truth_table_fixup`**：两步「修正」。前者检查 BLIF 网表里是否有 Verilog 非法标识符（如以数字开头的 net 名）并可选地自动改名（`--fix`）；后者根据打包结果修正 LUT 真值表（因为打包时引脚可能被交换）。

#### 4.3.2 核心流程

```
read_openfpga_arch ──写 arch_──► (只读 Arch 落地)
read_sim_setting   ──写 sim_setting_──►
                                          │
vpr 的设备数据 ───────────────────────────┤
                                          ▼
                        link_openfpga_arch (绑定对账, 写入各种 annotation)
                                          │
                          ┌───────────────┴───────────────┐
                          ▼                               ▼
              check_netlist_naming_conflict        lut_truth_table_fixup
              (改名 --fix, 写 netlist_renaming.xml)  (按打包结果修真值表)
```

#### 4.3.3 源码精读

脚本第 6–19 行就是这个阶段：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L11-L19](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L11-L19) —— `link_openfpga_arch --activity_file ${ACTIVITY_FILE} --sort_gsb_chan_node_in_edges` 把架构绑定到 VPR 数据库，并对 GSB 的入边排序（让结果可复现）；随后 `check_netlist_naming_conflict --fix --report ./netlist_renaming.xml` 改名并留痕，`lut_truth_table_fixup` 修真值表。

命令依赖关系是这一阶段的「骨架」，直接在 setup 模板里声明：

[openfpga/src/base/openfpga_setup_command_template.h:L1438-L1446](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1438-L1446) —— `link_openfpga_arch` 的依赖 = `read_openfpga_arch` + `read_openfpga_simulation_setting` + `vpr`。这精确解释了脚本里 `read_openfpga_arch`、`read_sim_setting` 必须排在 `link_openfpga_arch` 前面。

[openfpga/src/base/openfpga_setup_command_template.h:L1488-L1491](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1488-L1491) —— `check_netlist_naming_conflict` 只依赖 `vpr`（它改的是网表名，与架构无关）。

[openfpga/src/base/openfpga_setup_command_template.h:L1509-L1513](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1509-L1513) —— `lut_truth_table_fixup` 依赖 `read_openfpga_arch` + `vpr`（修真值表既要架构里的 LUT 模型，也要打包结果）。

注意一个细节：`check_netlist_naming_conflict` 和 `lut_truth_table_fixup` **彼此之间没有依赖声明**，所以理论上顺序可换；但脚本注释（L18「Apply fix-up ... based on packing results」）表明它们都属于「打包后的修正」，放在 link 之后、build_fabric 之前是合理的统一收尾。

#### 4.3.4 代码实践

**实践目标**：理解 `link_openfpga_arch` 的桥梁作用——缺了它，build_fabric 能否成功。

**操作步骤**：

1. 复制一份 `example_script.openfpga` 为本地实验脚本。
2. 注释掉 `link_openfpga_arch` 那一行（行首加 `#`）。
3. 通过 `run_fpga_flow.py`（或直接用替换好的 `<top>_run.openfpga`）执行。

**需要观察的现象**：执行到 `build_fabric` 时应报错（依赖未满足或绑定数据缺失）。

**预期结果**：证实 link 阶段是 vpr 与 build_fabric 之间不可省的桥梁。**待本地验证**（若未配好运行环境）。即便不运行，从 4.3.3 的依赖声明也能得出结论：`build_fabric` 依赖 `link_openfpga_arch`（见 4.4.3），而 link 又依赖 read_arch + vpr，跳过 link 必然断链。

#### 4.3.5 小练习与答案

**练习 1**：`link_openfpga_arch` 依赖哪三条命令？为什么？

**参考答案**：`read_openfpga_arch`（要绑定的电路架构）、`read_openfpga_simulation_setting`、`vpr`（被绑定的设备数据）。link 的工作就是把这「电路架构」和「设备数据」两边对账，所以两边都得先就绪。

**练习 2**：`check_netlist_naming_conflict --fix` 生成的 `netlist_renaming.xml` 有什么用？

**参考答案**：它记录了「原名 → 改名」的映射。因为改名后网表标识符变了，下游（如 testbench 自检、外部约束文件）需要这张映射表来对齐新旧名字。

---

### 4.4 fabric 构建：build_fabric 与 write_fabric_hierarchy

#### 4.4.1 概念说明

`build_fabric` 是整条流程的「重头戏」：它把前面准备好的「架构 + 设备数据」翻译成一张**模块图（ModuleManager）**——也就是 FPGA fabric 的完整结构描述（有哪些模块、每个模块有哪些端口、子模块如何实例化、网如何连接）。这是 u6 整个单元的主题，本讲只从「它在流程里的位置」角度讲两点：

1. **它的输入是 link 阶段的成果**（依赖 `link_openfpga_arch`），输出是 context 里的 `module_graph_`，是后续网表/比特流/SDC 生成的共同数据源。
2. **它的 `--compress_routing` 选项**会压缩重复的布线模块（识别 unique GSB），显著减少模块数量；脚本注释说这能「减少布线架构模块数量」。

`build_fabric` 之后紧跟 `write_fabric_hierarchy`，它把模块层级写成一个文本文件，供**层次化 PnR（hierarchical PnR）**流程使用——把大 fabric 拆成小 tile 分别做后端。

#### 4.4.2 核心流程

```
link_openfpga_arch 的成果(arch + device annotation)
              │
              ▼
        build_fabric
        ├── 自下而上构建: essential → mux/decoder → lut → wire → memory → grid → routing → tile → top
        └── --compress_routing: 合并等价布线模块
              │
              ▼
        写 module_graph_(可写 context)
              │
              ├──► write_fabric_hierarchy (只读, 输出 fabric_hierarchy.txt)
              └──► 供阶段三所有 write_* 命令消费
```

#### 4.4.3 源码精读

脚本第 21–28 行：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L21-L28](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L21-L28) —— `build_fabric --compress_routing` 构建模块图，`write_fabric_hierarchy --file ./fabric_hierarchy.txt` 输出层级。

依赖关系：

[openfpga/src/base/openfpga_setup_command_template.h:L1518-L1523](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1518-L1523) —— `build_fabric` 只依赖 `link_openfpga_arch`。注意：依赖只声明一级，所以虽然 build_fabric 传递性地也需要 read_arch、vpr，但模板只直接写 link。

两个关键选项的定义：

[openfpga/src/base/openfpga_setup_command_template.h:L412-L419](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L412-L419) —— `--compress_routing`（「通过识别 unique GSB 压缩唯一布线模块数量」）和 `--duplicate_grid_pin`（「复制 grid 同侧引脚」）。脚本注释「Enable pin duplication on grid modules」对应后者，但实际脚本行只开了 `--compress_routing`，`duplicate_grid_pin` 是注释里提到、可按需打开的选项。

#### 4.4.4 代码实践

**实践目标**：观察 `--compress_routing` 对产物模块数量的影响。

**操作步骤**：

1. 对同一个 task，分别用带 `--compress_routing` 与不带的两个脚本跑。
2. 跑完后看 `write_fabric_verilog` 输出的 `SRC/` 目录里的子模块网表文件数量。

**需要观察的现象**：开压缩时，布线类模块（`cbx_*`、`cby_*`、`sb_*`）文件数量明显变少（只剩 unique 的那几类）。

**预期结果**：印证压缩思想——很多 GSB 在拓扑上完全等价，只需实例化一份。模块构建顺序（essential→mux→…→top）的细节留到 u6-l2，本讲只关注「它在流程里的输入输出」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`build_fabric` 在 context 里写哪个分区？它的直接依赖是谁？

**参考答案**：写 `module_graph_`（ModuleManager）。直接依赖是 `link_openfpga_arch`（只声明一级，不递归到 read_arch/vpr）。

**练习 2**：`write_fabric_hierarchy` 输出的文件主要服务哪个下游场景？

**参考答案**：层次化 PnR（hierarchical placement & routing）。把大 fabric 拆成可复用的 tile 子模块，分别做后端，降低单次 PnR 规模。

---

### 4.5 repack：逻辑到物理 pb 的重打包（必须在比特流之前）

#### 4.5.1 概念说明

`repack` 是本讲**最重要的顺序约束**之一。VPR 的打包（pack）是在「逻辑 pb」上做的——它只关心逻辑等价性，把 LUT/FF 塞进逻辑块。但 OpenFPGA 的 fabric 用的是「物理 pb」——物理实现里 LUT 的引脚可能被重排、可能有 mode 切换、可能有额外的物理资源。`repack` 把网表从逻辑 pb **重新打包到物理 pb**，并在物理 pb 上重做局部布线。

为什么它必须在比特流生成之前？因为 `build_architecture_bitstream` 是**从打包后的结果解码配置位**的——它要读 LUT 的真值表和布线 mux 的选择位。如果还没 repack 到物理 pb，解码出来的配置位会反映「逻辑」而非「物理」实现，导致比特流错误。脚本注释把这条约束写得很直白：

> `# Repack the netlist to physical pbs`
> `# This must be done before bitstream generator and testbench generation`

repack 的内部机制（lb_router、physical_pb、物理 lb rr graph、真值表修正）是 u9-l3 的主题，本讲只锁定它在流程中的位置约束。

#### 4.5.2 核心流程

```
build_fabric(module_graph_) ──► repack
                                   │
                    在物理 pb 上重打包 netlist + 重做局部布线
                                   │
                  ┌────────────────┼─────────────────┐
                  ▼                ▼                 ▼
        修正后的真值表       物理引脚映射      供比特流解码用
                                   │
                                   ▼
                 (此时 context 里打包结果已是「物理版」)
```

#### 4.5.3 源码精读

脚本第 30–33 行（含那段关键注释）：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L30-L33](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L30-L33) —— repack 紧跟在 build_fabric 与 write_fabric_hierarchy 之后，注释三次强调「必须在 bitstream generator 和 testbench generation 之前」。

依赖声明：

[openfpga/src/base/openfpga_bitstream_command_template.h:L315-L319](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L315-L319) —— `repack` 的依赖 = `build_fabric`。这从代码层面保证「repack 不能在 build_fabric 之前」。

[openfpga/src/base/openfpga_bitstream_command_template.h:L324-L331](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L324-L331) —— `build_architecture_bitstream` 的依赖 = `repack`。**这两条依赖首尾相接，正好把 `build_fabric → repack → build_architecture_bitstream` 这条链焊死**。这就是 u2-l2 提到的「教科书级依赖链」的开头。

repack 命令本身的选项（支持设计约束等）：

[openfpga/src/base/openfpga_bitstream_command_template.h:L28-L49](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_bitstream_command_template.h#L28-L49) —— repack 支持 `--design_constraints`（设计约束文件）、`--ignore_global_nets_on_pins`、`--verbose`，执行函数是 `repack_template<T>`（可写 context）。

#### 4.5.4 代码实践

**实践目标**：验证「repack 必须在 build_architecture_bitstream 之前」这条约束。

**操作步骤**：

1. 复制 `example_script.openfpga`，把 `repack` 这一行**移动到** `build_architecture_bitstream` 之后。
2. 用替换好的脚本执行（经 `run_fpga_flow.py`）。

**需要观察的现象**：执行 `build_architecture_bitstream` 时报「依赖 repack 未执行」（因为此时 repack 还没跑）。

**预期结果**：shell 的依赖检查会直接拒绝执行 `build_architecture_bitstream`。即便绕过依赖检查（比如手动构造），生成的比特流也会因缺少物理重打包而错误。**待本地验证**。这条实践直接对应本讲的 practice_task。

#### 4.5.5 小练习与答案

**练习 1**：用一句话解释「为什么 repack 必须在比特流生成之前」。

**参考答案**：比特流是从「打包后的结果」解码配置位的，而 repack 把网表从逻辑 pb 重打包到物理 pb；不 repack，解码出的配置位反映的是逻辑实现而非物理实现，比特流就是错的。

**练习 2**：`repack` 的直接依赖是谁？它与 `build_architecture_bitstream` 的依赖关系在源码里如何体现？

**参考答案**：repack 直接依赖 `build_fabric`；而 `build_architecture_bitstream` 直接依赖 `repack`。两条声明首尾相接，焊出 `build_fabric → repack → build_architecture_bitstream` 的强制顺序。

---

### 4.6 比特流生成：device 级 → fabric 级两级链

#### 4.6.1 概念说明

OpenFPGA 的比特流是**两级模型**（u7-l1 详讲）：

- **device 级（fabric 无关）**：`build_architecture_bitstream` 生成。它遍历布局布线结果，解码出每个 LUT 的配置位、每个布线 mux 的选择位，组织成「层级块 + 配置位」的树（`BitstreamManager`），输出 `fabric_independent_bitstream.xml`。它只关心「器件该配成什么样」，不关心 fabric 模块怎么连。
- **fabric 级（fabric 相关）**：`build_fabric_bitstream` 把上一级的 device 比特流，按 fabric 模块层级和**配置协议**（scan_chain / memory_bank / frame_based，u3-l4）重组，加上协议相关的寻址信息（如 memory_bank 的 BL/WL 地址），得到 `FabricBitstream`。

最后 `write_fabric_bitstream` 把 fabric 比特流落盘成 `fabric_bitstream.bit`（plain_text 格式）。这个 `.bit` 文件会被阶段三的 testbench 命令当作输入（`write_full_testbench --bitstream fabric_bitstream.bit`）。

这四级（build_arch → build_fabric_bitstream → write → 被 testbench 消费）之间是**严格的串行依赖**。

#### 4.6.2 核心流程

```
repack 之后
   │
   ▼
build_architecture_bitstream  (写 device 级 BitstreamManager)
   │   --write_file fabric_independent_bitstream.xml
   ▼
build_fabric_bitstream        (device→fabric 重组, 加协议寻址)
   │   按 config_protocol(scan_chain/memory_bank/...) 组织
   ▼
write_fabric_bitstream        (只读, 落盘)
       --file fabric_bitstream.bit --format plain_text
   │
   ▼
(被阶段三 write_full_testbench --bitstream ... 消费)
```

#### 4.6.3 源码精读

脚本第 35–43 行：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L35-L43](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L35-L43) —— 三步串行：`build_architecture_bitstream --write_file fabric_independent_bitstream.xml` → `build_fabric_bitstream` → `write_fabric_bitstream --file fabric_bitstream.bit --format plain_text`。

整条依赖链在一个函数里组装完毕：

[openfpga/src/base/openfpga_bitstream_command_template.h:L300-L310](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L300-L310) —— bitstream 组入口先取回 `build_fabric` 命令 id 作为依赖图根。

[openfpga/src/base/openfpga_bitstream_command_template.h:L348-L368](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L348-L368) —— 这里把 `build_fabric_bitstream` 的依赖设为 `build_architecture_bitstream`，把 `write_fabric_bitstream` 的依赖设为 `build_fabric_bitstream`。

合起来，整条链是：

```
build_fabric (来自 setup 组)
   └─► repack                          (依赖 build_fabric)
        └─► build_architecture_bitstream (依赖 repack)
             └─► build_fabric_bitstream   (依赖 build_architecture_bitstream)
                  └─► write_fabric_bitstream (依赖 build_fabric_bitstream)
```

`write_fabric_bitstream` 的格式选项：

[openfpga/src/base/openfpga_bitstream_command_template.h:L199-L208](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L199-L208) —— `--format` 支持 `plain_text`（脚本用的）或 `xml`，还有 `--fast_configuration`（u7-l4 详讲，能跳过大量相同位以加速配置）。注意脚本这一行**没开** `--fast_configuration`，是「完整比特流」。

#### 4.6.4 代码实践

**实践目标**：用依赖链定位「为什么这四步不能换序」。

**操作步骤**：

1. 打开 `openfpga_bitstream_command_template.h` 的 L315–L368。
2. 画出四步命令的依赖图（参考 4.6.3 末尾）。
3. 回答：如果误把 `write_fabric_bitstream` 移到 `build_architecture_bitstream` 之前，shell 会在哪一步报错？

**需要观察的现象**（源码阅读型，无需运行）：依赖链是单线串行的，任一步前置缺失都会被 shell 的依赖检查拦截。

**预期结果**：`write_fabric_bitstream` 依赖 `build_fabric_bitstream`，后者又依赖 `build_architecture_bitstream`……前置缺失即报错。这就是「依赖只检查一级、不递归」却仍能保证顺序的原因——每一步都被它的直接后继钉住了。

#### 4.6.5 小练习与答案

**练习 1**：device 级比特流与 fabric 级比特流的最大区别是什么？为什么需要两级？

**参考答案**：device 级（`BitstreamManager`）只描述「器件配成什么样」，与 fabric 模块连法和配置协议无关；fabric 级（`FabricBitstream`）按模块层级和配置协议重组，带寻址信息。分两级是为了把「逻辑配置内容」和「物理组织方式」解耦——同一份 device 比特流理论上可按不同协议重新组织。

**练习 2**：脚本生成的 `fabric_bitstream.bit` 在后续被谁消费？

**参考答案**：被 `write_full_testbench --bitstream fabric_bitstream.bit` 消费，用于在 testbench 里驱动完整的编程阶段仿真。

---

### 4.7 网表与 SDC 输出：verilog、三种 testbench 与 SDC 约束

#### 4.7.1 概念说明

比特流就绪后，进入阶段三「输出」。本讲把输出分成三类：

**1. fabric Verilog 网表**：`write_fabric_verilog` 遍历 ModuleManager，生成 fabric 的完整 Verilog（子模块 + grid + routing + 顶层）。脚本的 `--explicit_port_mapping` 要求显式端口映射（`.port(net)` 形式，增强可读性与兼容性），`--include_timing` 带上时序信息，`--print_user_defined_template` 打印用户自定义模板。

**2. 三种验证产物**（本讲的核心学习目标之一）：

| 命令 | 验证策略 | 是否含编程阶段 | 关键选项 |
| --- | --- | --- | --- |
| `write_full_testbench` | **完整验证**：先编程 fabric（灌入比特流），再跑核心逻辑，最后自检 | 是 | `--bitstream`、`--include_signal_init`、`--reference_benchmark_file_path` |
| `write_preconfigured_fabric_wrapper` | 生成一个 wrapper，把比特流**内嵌**进 fabric，跳过编程阶段 | 否 | `--embed_bitstream iverilog` |
| `write_preconfigured_testbench` | 配合上面的 wrapper，做**跳过编程的快速验证** | 否 | `--reference_benchmark_file_path` |

直觉上：full testbench 验证「编程 + 运行」全过程，最贴近真实芯片上电，但仿真慢；preconfigured 系列「作弊」地把配置预先内嵌，只验证核心逻辑，仿真快，适合频繁回归。`--embed_bitstream iverilog` 表示用 iverilog 兼容的方式内嵌（`iverilog` 是开源 Verilog 仿真器）。

**3. SDC 约束**：三个命令服务不同后端阶段。
- `write_pnr_sdc`：给布局布线（PnR）后端用的时序约束。
- `write_sdc_disable_timing_configure_ports`：**禁用配置端口的时序路径**——配置引脚（prog pin）在配置阶段才生效，做逻辑时序分析时应忽略，否则会报大量虚假路径。
- `write_analysis_sdc`：给静态时序分析（STA）用的约束，针对「已配置好」的 fabric。

#### 4.7.2 核心流程

```
build_fabric(module_graph_) + 已生成比特流
   │
   ├──► write_fabric_verilog          (fabric Verilog 网表, → ./SRC)
   │
   ├──► write_full_testbench          (完整编程+运行 testbench, --bitstream ...)
   ├──► write_preconfigured_fabric_wrapper  (内嵌比特流的 wrapper)
   ├──► write_preconfigured_testbench       (快速验证 testbench)
   │
   ├──► write_pnr_sdc                       (PnR 约束 → ./SDC)
   ├──► write_sdc_disable_timing_configure_ports  (禁用配置端口时序)
   └──► write_analysis_sdc                  (STA 约束 → ./SDC_analysis)
```

注意 `generate_fabric_example_script.openfpga` 在这一阶段**只保留** `write_fabric_verilog` + 两个 SDC，**完全没有 testbench 和比特流**——再次印证阶段三可按需裁剪。

#### 4.7.3 源码精读

脚本第 45–67 行（输出阶段）：

[openfpga_flow/openfpga_shell_scripts/example_script.openfpga:L45-L67](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga#L45-L67) —— 依次写 fabric Verilog、full testbench、preconfigured wrapper、preconfigured testbench，再写三类 SDC。

依赖关系（全部依赖 `build_fabric`）：

[openfpga/src/base/openfpga_verilog_command_template.h:L688-L714](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_verilog_command_template.h#L688-L714) —— `write_fabric_verilog`、`write_full_testbench`、`write_preconfigured_fabric_wrapper` 都直接依赖 `build_fabric`。

[openfpga/src/base/openfpga_verilog_command_template.h:L752-L758](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_verilog_command_template.h#L752-L758) —— `write_preconfigured_testbench` 也依赖 `build_fabric`。

> **注意一个微妙之处**：注册依赖里，testbench 命令**只**声明依赖 `build_fabric`，并**没有**声明依赖 `write_fabric_bitstream`。但脚本里 `write_full_testbench --bitstream fabric_bitstream.bit` 显然需要先有这个 `.bit` 文件。这说明：依赖检查只覆盖「context 内的数据依赖」，而「读一个磁盘文件」这种依赖要靠**脚本作者自己保证顺序**。所以你会在脚本里看到比特流写出（L43）严格排在 testbench（L55）之前——这是人工维护的顺序，不是 shell 自动保证的。

fabric Verilog 的三个关键选项：

[openfpga/src/base/openfpga_verilog_command_template.h:L46-L54](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_verilog_command_template.h#L46-L54) —— `--explicit_port_mapping`、`--include_timing`、`--print_user_defined_template` 的定义。

preconfigured wrapper 的 `--embed_bitstream` 选项：

[openfpga/src/base/openfpga_verilog_command_template.h:L268-L274](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_verilog_command_template.h#L268-L274) —— `--embed_bitstream`（决定如何内嵌比特流，如 `iverilog`）与 `--include_signal_init`。这正是「跳过编程阶段」的机制所在。

#### 4.7.4 代码实践

**实践目标**：对比三种 testbench 产物的差异。

**操作步骤**：

1. 用 `example_script.openfpga` 跑一个 task（它三个 testbench 都生成）。
2. 在 `SRC/` 目录找到：`<top>_top_tb.v`（full testbench）、`<top>_preconfig_fabric_wrapper.v`、`<top>_autocheck_top_tb.v`（preconfigured）。
3. 用 iverilog 分别仿真（仓库提供 `iverilog_example_script.openfpga` 作为参考）。

**需要观察的现象**：full testbench 仿真时间明显更长（含编程阶段），preconfigured 系列仿真快得多，但二者最终自检结果应一致（都通过）。

**预期结果**：理解「full = 慢但全、preconfigured = 快但跳过编程」的权衡。**待本地验证**（需要 iverilog 与可跑环境）。即便不仿真，对比两个 testbench 文件也能看出：full 版有一长串「灌比特流」的 initial 块，preconfigured 版没有。

#### 4.7.5 小练习与答案

**练习 1**：`write_full_testbench` 与 `write_preconfigured_testbench` 的本质区别是什么？各自适合什么场景？

**参考答案**：full 版含完整编程阶段（灌入比特流），最贴近真实上电，仿真慢，适合最终签核；preconfigured 版把配置预先内嵌（wrapper 的 `--embed_bitstream`），跳过编程，仿真快，适合频繁回归验证核心逻辑。

**练习 2**：为什么需要 `write_sdc_disable_timing_configure_ports`？

**参考答案**：配置端口只在配置阶段生效，正常运行时不参与逻辑时序。若不禁用，STA 会把它们当成正常路径分析，报出大量虚假的时序违例。

**练习 3**：testbench 命令的注册依赖只到 `build_fabric`，但脚本里它们排在比特流写出之后。为什么？

**参考答案**：依赖检查只管 context 内数据依赖；而 `write_full_testbench --bitstream ...` 读取的是磁盘上的 `.bit` 文件，这种文件级依赖 shell 不自动检查，必须由脚本作者手工保证顺序。

---

## 5. 综合实践

> 这是本讲的主体实践任务，对应规格里的 practice_task：**把 `example_script.openfpga` 的命令按「输入 → 处理 → 输出」三阶段重新分组，并标注硬性顺序约束。**

### 5.1 任务说明

请打开 [example_script.openfpga](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/example_script.openfpga)，完成下面三件事：

1. **三阶段分组**：把脚本里每一条命令（不含注释和 `exit`）填进下表的「阶段」列。
2. **硬性顺序约束**：标出哪些命令之间存在「必须先后」的硬约束，并给出依据（来自哪份命令模板的依赖声明，或脚本注释）。
3. **断链实验（思考题）**：回答——如果把 `repack` 整行删掉，流程会在哪一步、以什么原因失败？

### 5.2 参考答案（先自己填再看）

#### 5.2.1 三阶段分组表

| 命令 | 阶段 | 在 context 中的作用 |
| --- | --- | --- |
| `vpr` | 输入 | 写设备数据（grid/rr_graph/clustering） |
| `read_openfpga_arch` | 输入 | 写 `arch_` |
| `read_openfpga_simulation_setting` | 输入 | 写仿真设置 |
| `link_openfpga_arch` | 输入 | 绑定 arch↔device，写各类 annotation |
| `check_netlist_naming_conflict` | 输入 | 改名（`--fix`） |
| `lut_truth_table_fixup` | 输入 | 修真值表 |
| `build_fabric` | **处理** | 写 `module_graph_` |
| `write_fabric_hierarchy` | 输出 | 读 module_graph，写 `fabric_hierarchy.txt` |
| `repack` | **处理** | 重打包到物理 pb |
| `build_architecture_bitstream` | **处理** | 写 device 级 `BitstreamManager` |
| `build_fabric_bitstream` | **处理** | 写 fabric 级 `FabricBitstream` |
| `write_fabric_bitstream` | 输出 | 写 `fabric_bitstream.bit` |
| `write_fabric_verilog` | 输出 | 读 module_graph，写 Verilog |
| `write_full_testbench` | 输出 | 读 module_graph + `.bit`，写 testbench |
| `write_preconfigured_fabric_wrapper` | 输出 | 读 module_graph，写 wrapper |
| `write_preconfigured_testbench` | 输出 | 读 module_graph，写 testbench |
| `write_pnr_sdc` | 输出 | 写 PnR SDC |
| `write_sdc_disable_timing_configure_ports` | 输出 | 写禁用配置端口 SDC |
| `write_analysis_sdc` | 输出 | 写 STA SDC |

> 关于 `write_fabric_hierarchy` 的阶段：它写的是文件（产物），所以归「输出」；但它紧挨 build_fabric、常常和「处理」放一起。两种归类都合理，关键是理解它**只读** module_graph。`build_fabric_bitstream` 虽然名字带 build，但它是把 device 比特流「重组」成 fabric 比特流，仍是数据加工，归「处理」。

#### 5.2.2 硬性顺序约束清单

依据 = 命令注册时声明的直接依赖（来自 setup/bitstream/verilog 三份模板）或脚本注释：

| 约束（先 → 后） | 依据 | 来源 |
| --- | --- | --- |
| `vpr` → `link_openfpga_arch` | link 依赖含 vpr | setup 模板 L1438–L1446 |
| `read_openfpga_arch` → `link_openfpga_arch` | link 依赖含 read_arch | 同上 |
| `link_openfpga_arch` → `build_fabric` | build_fabric 依赖 link | setup 模板 L1518–L1523 |
| `build_fabric` → `repack` | repack 依赖 build_fabric | bitstream 模板 L315–L319 |
| `repack` → `build_architecture_bitstream` | build_arch 依赖 repack（**关键**） | bitstream 模板 L324–L331 |
| `build_architecture_bitstream` → `build_fabric_bitstream` | 后者依赖前者 | bitstream 模板 L348–L356 |
| `build_fabric_bitstream` → `write_fabric_bitstream` | 后者依赖前者 | bitstream 模板 L361–L368 |
| `build_fabric` → 所有 `write_fabric_verilog` / testbench | 都依赖 build_fabric | verilog 模板 L688–L758 |
| `write_fabric_bitstream` → `write_full_testbench` | full testbench 读 `.bit` 文件（**文件级，非 context 依赖**） | 脚本注释 + 选项 `--bitstream` |
| `repack` 必须在 testbench 之前 | 脚本注释明确 | example_script L30–L32 |

#### 5.2.3 断链实验答案

删掉 `repack` 后：执行到 `build_architecture_bitstream` 时，shell 的依赖检查会发现它依赖的 `repack` 从未执行，**直接报错并拒绝执行**（依赖只检查一级，但这里 repack 是 build_arch 的直接前置，所以会被拦）。即便绕过检查强行执行，生成的比特流也会因为缺少「逻辑→物理 pb 重打包」而错误。这正解释了为什么脚本注释反复强调 repack 的位置。

### 5.3 进阶（可选）

对比 `generate_fabric_example_script.openfpga`（无 repack、无比特流、无 testbench）与 `example_script.openfpga`，回答：哪些命令是「只要 fabric 网表」就可以省掉的？省掉它们后，阶段三还剩什么？这能帮你建立「按需裁剪流程」的直觉。

## 6. 本讲小结

- `example_script.openfpga` 是一条「输入 → 处理 → 输出」三阶段的端到端流程，命令顺序不是随意排列，而是由 `OpenfpgaContext` 的读写依赖决定的。
- `${...}` 是 Python `string.Template` 占位符，由 `run_fpga_flow.py` 的 `safe_substitute` 替换，openfpga 二进制本身不解析它们；直接 `openfpga -f` 模板跑不通。
- `vpr` 是流程第一块多米诺骨牌，它把设备数据写进共享 context，后续 setup 命令（link/check/fixup）都以它为依赖锚点。
- `link_openfpga_arch` 是 vpr 与 build_fabric 之间的桥梁，依赖 read_arch + read_sim_setting + vpr。
- 比特流是一条严格串行的依赖链：`build_fabric → repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream`，其中 **repack 必须在比特流生成之前**（物理重打包后才能正确解码配置位）。
- 三种验证产物：`write_full_testbench`（含编程的完整验证，慢）、`write_preconfigured_fabric_wrapper` + `write_preconfigured_testbench`（内嵌比特流、跳过编程的快速验证）。
- 命令依赖检查只覆盖「context 内数据依赖」且「只检查一级、不递归」；像「读磁盘 `.bit` 文件」这类依赖要靠脚本作者手工保证顺序。

## 7. 下一步学习建议

本讲把流程「串」起来了，但每个阶段内部还有很多细节没展开。建议按依赖顺序继续：

- **u6（Fabric 构建与 ModuleManager）**：深入 `build_fabric` 自下而上的构建顺序（essential→mux→…→top）、`--compress_routing` 如何识别 unique GSB，以及 ModuleManager 的模块/端口/实例/网抽象。
- **u7（比特流生成）**：展开两级比特流模型（`BitstreamManager` vs `FabricBitstream`）、grid/routing/mux 位是如何解码的，以及 `write_fabric_bitstream` 的 `--fast_configuration`。
- **u8（网表与时序约束输出）**：细化 `write_fabric_verilog` 遍历 ModuleManager 的方式、SPICE 网表生成，以及 pnr_sdc / analysis_sdc 的区别。
- 若想先动手验证本讲的顺序约束，可回到 u1-l4 跑一个 `basic_tests` 任务，再用本讲 5.2 的答案对照 `run/` 目录里的 `<top>_run.openfpga` 实测。
