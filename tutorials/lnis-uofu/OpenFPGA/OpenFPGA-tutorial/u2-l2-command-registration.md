# 命令分组与注册机制

## 1. 本讲目标

上一讲（u2-l1）我们追踪了 `openfpga` 程序的启动链路：`main()` → `OpenfpgaShell::start()`，并搞清了交互、脚本、execute 三种运行模式。但留下了一个关键问题：**当你在 shell 里敲下 `read_openfpga_arch`、`build_fabric`、`write_fabric_verilog` 这些命令时，它们是从哪里来的？谁规定了它们的名字、选项和执行顺序？**

本讲就回答这个问题。读完本讲，你应当能够：

- 说出 OpenFPGA 的**七大命令类别**（VPR、OpenFPGA setup、FPGA-Verilog、FPGA-Bitstream、FPGA-SPICE、FPGA-SDC、Basic）各自的职责，以及它们在 `OpenfpgaShell` 构造函数里的**固定注册顺序**。
- 识别每个类别里**最常用的命令**：`vpr`、`read_openfpga_arch`、`link_openfpga_arch`、`build_fabric`、`repack`、`build_architecture_bitstream`、`build_fabric_bitstream`、`write_fabric_verilog`、`write_pnr_sdc` 等。
- 看懂一条命令是**如何被注册**到 shell 的（名字 → 选项 → 归类 → 绑定执行函数 → 声明依赖）。
- 理解**命令依赖图**：为什么 `repack` 必须在 `build_fabric` 之后、`build_architecture_bitstream` 之前，以及 shell 是如何在运行时**强制检查**这些依赖的。
- 明白「Basic 组必须最后注册」这条铁律背后的原因。

## 2. 前置知识

- **Shell（命令外壳）**：一个接收用户输入字符串、解析成「命令 + 选项」、再调用对应 C++ 函数的程序框架。OpenFPGA 并没有用现成的 shell 库，而是自带了一个模板框架 `Shell<T>`（位于 `libs/libopenfpgashell`），这里的 `T` 就是上一讲提到的全局数据中枢 `OpenfpgaContext`。
- **命令类别（command class）**：把一堆命令按功能归类后贴上的标签，例如「FPGA-Verilog」「FPGA-Bitstream」。它的唯一作用是在 `help` 输出里把命令**分组显示**，方便人类阅读。类别本身不影响命令的执行逻辑。
- **命令依赖（command dependency）**：一条命令声明「我必须在另外某些命令成功执行之后才能运行」。例如 `build_fabric_bitstream` 声明依赖 `build_architecture_bitstream`。OpenFPGA shell 会**在运行时检查**这些依赖，不满足就直接报错返回。
- **MACRO 命令**：一种特殊的命令类型，它自带独立的命令行解析器，shell 不替它解析选项。`vpr` 就是 MACRO 命令——它的选项格式由 VPR 自己定义，OpenFPGA 只是把它「挂」到 shell 里。

如果你对 `OpenfpgaContext` 和 `OpenfpgaShell` 的关系还不清楚，建议先回顾 u2-l1 和 u2-l3。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `openfpga/src/base/openfpga_shell.cpp` | `OpenfpgaShell` 构造函数，按固定顺序调用 7 个 `add_*_commands()`，是命令注册的**总入口**。 |
| `openfpga/src/vpr_wrapper/vpr_command.cpp` | 注册 **VPR** 类别：`vpr` 与 `vpr_standalone`。 |
| `openfpga/src/base/openfpga_setup_command_template.h` | 注册 **OpenFPGA setup** 类别：架构读写、`link_openfpga_arch`、`build_fabric` 等绝大多数核心命令。 |
| `openfpga/src/base/openfpga_bitstream_command_template.h` | 注册 **FPGA-Bitstream** 类别：`repack`、`build_architecture_bitstream`、`build_fabric_bitstream`、`write_fabric_bitstream` 等，并演示命令依赖链。 |
| `openfpga/src/base/openfpga_verilog_command_template.h` | 注册 **FPGA-Verilog** 类别：`write_fabric_verilog` 及各类 testbench 命令。 |
| `openfpga/src/base/openfpga_sdc_command_template.h` | 注册 **FPGA-SDC** 类别：`write_pnr_sdc`、`write_analysis_sdc` 等。 |
| `openfpga/src/base/openfpga_spice_command_template.h` | 注册 **FPGA-SPICE** 类别：`write_fabric_spice`。 |
| `openfpga/src/base/basic_command.cpp` | 注册 **Basic** 类别：`exit`、`version`、`source`、`ext_exec`、`help`，并解释「help 必须最后注册」。 |
| `libs/libopenfpgashell/src/base/shell.h` / `shell.tpp` | `Shell<T>` 框架本身，定义命令类别、执行函数类型、依赖存储与运行时依赖检查。 |

> 说明：setup / verilog / bitstream / sdc / spice 这 5 组都是**模板头文件**（`*_command_template.h`），由各自极薄的包装文件（如 `openfpga_setup_command.cpp`）用 `OpenfpgaContext` 实例化。VPR 和 Basic 两组则是直接针对 `Shell<OpenfpgaContext>` 的非模板代码。

---

## 4. 核心概念与源码讲解

### 4.1 七大命令类别与注册总览

#### 4.1.1 概念说明

`openfpga` 二进制之所以能在 shell 里提供几十条命令，是因为它在**程序启动时**（而不是运行时）就把所有命令一次性注册进了 `Shell<OpenfpgaContext>` 对象。注册发生的位置非常集中——就在 `OpenfpgaShell` 的构造函数里。

OpenFPGA 把全部命令划分为 **7 个类别**，并按下面的固定顺序注册：

| 注册顺序 | 类别名（`add_command_class` 的字符串） | 注册函数 | 职责 |
|:---:|---|---|---|
| 1 | `VPR` | `add_vpr_commands` | 调用 VPR 核心引擎完成综合后网表的打包、布局、布线 |
| 2 | `OpenFPGA setup` | `add_openfpga_setup_commands` | 读取/链接 OpenFPGA 架构、修正网表、`build_fabric` 构建 fabric 模块图 |
| 3 | `FPGA-Verilog` | `add_openfpga_verilog_commands` | 生成 fabric 的 Verilog 网表与各类 testbench |
| 4 | `FPGA-Bitstream` | `add_openfpga_bitstream_commands` | repack、生成两级比特流、写出比特流文件 |
| 5 | `FPGA-SPICE` | `add_openfpga_spice_commands` | 生成晶体管级 SPICE 网表 |
| 6 | `FPGA-SDC` | `add_openfpga_sdc_commands` | 生成 PnR / 时序分析用的 SDC 约束 |
| 7 | `Basic` | `add_basic_commands` | `exit` / `version` / `source` / `help` 等通用命令 |

#### 4.1.2 核心流程

注册的总体流程可以概括为「构造函数里按序调用七个 `add_*` 函数」：

```text
OpenfpgaShell 构造函数
  ├── set_name("OpenFPGA") + add_title(...)
  ├── add_vpr_commands(shell_)              // 1. VPR
  ├── add_openfpga_setup_commands(shell_)   // 2. OpenFPGA setup
  ├── add_openfpga_verilog_commands(shell_) // 3. FPGA-Verilog
  ├── add_openfpga_bitstream_commands(shell_)// 4. FPGA-Bitstream
  ├── add_openfpga_spice_commands(shell_)   // 5. FPGA-SPICE
  ├── add_openfpga_sdc_commands(shell_)     // 6. FPGA-SDC
  └── add_basic_commands(shell_)            // 7. Basic（必须最后！）
```

**为什么顺序是固定的？** 因为命令之间存在**跨类别的依赖引用**：

- setup 组里的很多命令（如 `link_openfpga_arch`、`pb_pin_fixup`）依赖 `vpr` 命令，而 `vpr` 由 VPR 组注册。所以 VPR 组必须先注册。
- verilog / bitstream / spice / sdc 四组的命令**几乎全部依赖 `build_fabric`**，而 `build_fabric` 由 setup 组注册。所以 setup 组必须在这四组之前。

每个后续组在注册开头，都会用 `shell.command("build_fabric")` 这种「按名字反查命令 id」的方式拿到前置命令的 id，再去构造依赖边。如果注册顺序错了，反查会失败。

#### 4.1.3 源码精读

构造函数是这一切的起点。下面这段就是七大组的注册顺序，注意末尾的注释强调 Basic 必须最后注册：

[openfpga_shell.cpp:15-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L15-L42) —— `OpenfpgaShell` 构造函数：设置 shell 名字与标题后，**依次**调用 7 个 `add_*_commands()`，每个函数负责把自己那一类命令注册进 `shell_`。

每一个 `add_*_commands` 内部的第一步都是「新建一个命令类别」并拿到类别 id，随后注册的命令都会归到这个类别下。例如 VPR 组：

[vpr_command.cpp:11-13](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/vpr_wrapper/vpr_command.cpp#L11-L13) —— `add_vpr_commands` 首先调用 `shell.add_command_class("VPR")` 创建名为 `VPR` 的类别，返回的 `vpr_cmd_class` 后面会用在每条命令的 `set_command_class` 上。

setup 组同理，只是类别名不同：

[openfpga_setup_command_template.h:1337-1339](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1337-L1339) —— setup 组在注册开头先反查 `vpr` 命令的 id（`shell.command("vpr")`），用于给后续命令构造依赖边；随后创建 `"OpenFPGA setup"` 类别。

「按名字反查前置命令」的模式在 bitstream 组也出现，它反查的是 `build_fabric`：

[openfpga_bitstream_command_template.h:304-310](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L304-L310) —— bitstream 组开头反查 `build_fabric` 命令 id，再创建 `"FPGA-Bitstream"` 类别。这正是「setup 必须先于 bitstream 注册」的代码证据。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「七大类别的注册顺序」与「类别名字符串」，不运行程序也能做到。

**操作步骤**：

1. 打开 `openfpga/src/base/openfpga_shell.cpp`，找到构造函数，记下 7 个 `add_*` 调用的出现顺序。
2. 对每个 `add_*` 函数，跳转到它的定义，找到 `shell.add_command_class("...")` 那一行，记录类别名字符串。

**需要观察的现象**：你会得到一张「调用顺序 → 类别名」的对照表，它应当与本讲 4.1.1 的表格完全一致。

**预期结果**：7 个类别名分别是 `VPR`、`OpenFPGA setup`、`FPGA-Verilog`、`FPGA-Bitstream`、`FPGA-SPICE`、`FPGA-SDC`、`Basic`。这张表也正是你在 shell 里输入 `help` 后看到的分组顺序。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `openfpga_shell.cpp` 构造函数里 `add_openfpga_setup_commands` 和 `add_vpr_commands` 两行**交换顺序**，会发生什么？

**参考答案**：setup 组在注册开头会调用 `shell.command("vpr")` 去反查 `vpr` 命令的 id。如果 setup 先注册，此时 `vpr` 还不存在，反查会返回无效 id，导致 setup 组里所有依赖 `vpr` 的命令（`link_openfpga_arch`、`check_netlist_naming_conflict`、`pb_pin_fixup`、`lut_truth_table_fixup` 等）的依赖边指向错误目标，程序行为异常甚至崩溃。这就是注册顺序不可随意调整的根本原因。

**练习 2**：类别（class）和命令（command）是什么关系？删掉一个类别会影响命令本身的执行吗？

**参考答案**：类别只是给命令贴的「分组标签」，用于 `help` 输出的排版展示。命令的执行由「绑定的执行函数」决定，与它属于哪个类别无关。但每条命令都必须归属某个类别（通过 `set_command_class` 设置），否则无法正常展示。

---

### 4.2 单条命令是如何注册的（注册四步法）

#### 4.2.1 概念说明

要理解 setup / bitstream / verilog 这些大组，先得看懂**单条命令**的注册套路。OpenFPGA 里几乎所有命令都遵循同一个「四步法」模板：

1. **构造 `Command` 对象**：用命令名（如 `"read_openfpga_arch"`）创建一个 `Command`，并给它添加若干**选项**（options），每个选项有名字、是否需要带值、说明文字，可选拥有短名（如 `-f`）。
2. **加入 shell**：`shell.add_command(shell_cmd, 描述, 是否隐藏)` 把命令注册进去，返回一个 `ShellCommandId`。
3. **归类**：`shell.set_command_class(cmd_id, class_id)` 把命令挂到某个类别下。
4. **绑定执行函数 + 声明依赖**：`set_command_execute_function`（或只读版本 `set_command_const_execute_function`）绑定真正干活的 C++ 函数；`set_command_dependency` 声明它依赖哪些前置命令。

#### 4.2.2 核心流程

以 `read_openfpga_arch` 为例的注册流程：

```text
Command shell_cmd("read_openfpga_arch")        // 1. 建命令
  └── add_option("file", require_value=true)   //    加选项 --file / -f
  └── set_option_short_name("f")
  └── set_option_require_value(OPT_STRING)
shell.add_command(shell_cmd, "read OpenFPGA architecture file", hidden=false)  // 2. 加入
  → 返回 shell_cmd_id
shell.set_command_class(shell_cmd_id, cmd_class_id)   // 3. 归到 "OpenFPGA setup"
shell.set_command_execute_function(shell_cmd_id,       // 4. 绑定执行函数
                                   read_openfpga_arch_template<T>)
// read_openfpga_arch 没有前置依赖，故不调用 set_command_dependency
```

**执行函数有多种类型**。`Shell<T>` 在 `shell.h` 里定义了一个枚举 `e_exec_func_type`，区分函数「是否需要读写 context、是否需要命令行选项、是否自带解析器」：

[shell.h:66-85](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.h#L66-L85) —— `e_exec_func_type` 枚举：`CONST_STANDARD`（只读 context + 命令选项）、`STANDARD`（可写 context + 命令选项）、`BUILTIN`（无需任何参数，如 `exit`）、`FLOATING`（与 context 无关）、`MACRO`（自带命令行解析器的黑盒，如 `vpr`）、`PLUGIN`（基于其他命令）等。

理解两类最常用的区别即可：
- **`set_command_const_execute_function`**：绑定的函数只**读** context（`const T&`），用于「查询 / 输出」类命令，如 `write_fabric_hierarchy`、`report_reference`。
- **`set_command_execute_function`**：绑定的函数可**写** context（`T&`），用于会修改 fabric / 比特流状态的命令，如 `read_openfpga_arch`、`build_fabric`、`repack`。

#### 4.2.3 源码精读

`read_openfpga_arch` 是最简单的注册样例（无依赖、可写 context）：

[openfpga_setup_command_template.h:30-50](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L30-L50) —— 完整展示四步法：建 `Command` → 加 `--file/-f` 选项 → `add_command` 拿 id → `set_command_class` 归类 → `set_command_execute_function` 绑定 `read_openfpga_arch_template<T>`。注意它没有调用 `set_command_dependency`，因为读架构文件没有任何前置条件。

`Shell<T>` 暴露的关键注册 API 都集中在头文件的一段里：

[shell.h:111-185](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.h#L111-L185) —— `add_command`（注册并返回 id）、`set_command_class`（归类）、`set_command_execute_function` / `set_command_const_execute_function`（绑定执行函数，按是否修改 `T` 重载了多份）、`set_command_dependency`（声明依赖）、`add_command_class`（创建类别）。

#### 4.2.4 代码实践

**实践目标**：用四步法「读」懂一条陌生命令的注册。

**操作步骤**：

1. 在 `openfpga_setup_command_template.h` 中找到 `add_build_fabric_command_template`（约 401 行起）。
2. 对照四步法，分别找出：它建了哪些选项（`--frame_view`、`--compress_routing`、`--load_fabric_key` 等）、它的执行函数是什么、它有没有声明依赖。

**需要观察的现象**：`build_fabric` 的选项非常多，但注册结构与 `read_openfpga_arch` 完全一致，只是 `add_option` 调用更多。

**预期结果**：你能说出 `build_fabric` 的执行函数是 `build_fabric_template<T>`，且（在 `add_setup_command_templates` 里）它依赖 `link_openfpga_arch`。

#### 4.2.5 小练习与答案

**练习 1**：`write_fabric_hierarchy` 用的是 `set_command_const_execute_function`，而 `build_fabric` 用的是 `set_command_execute_function`。为什么？

**参考答案**：`write_fabric_hierarchy` 只是把已经建好的模块层级**打印**到文件，不修改 context，所以用只读（const）版本；`build_fabric` 会**写入** context 里的 `module_graph_`，改变全局状态，所以用可写版本。这体现了「输出类命令用 const、构建类命令用非 const」的约定。

**练习 2**：一个命令的「描述字符串」（`add_command` 的第二个参数）在哪里会被用到？

**参考答案**：它会作为该命令的说明文字显示在 `help` 输出和该命令自己的 `--help`（即 `print_command_options`）里。它纯粹是面向人类的文档，不参与执行逻辑。

---

### 4.3 VPR 命令组：把 VPR 接入 shell

#### 4.3.1 概念说明

VPR（Versatile Place and Route）是 OpenFPGA 依赖的第三方布局布线引擎（作为 git 子模块引入，见 u1-l3）。OpenFPGA 并没有重写 VPR，而是把 VPR 的主函数**以 MACRO 命令的形式**挂进自己的 shell。于是用户可以在同一个 shell 里先敲 `vpr` 完成布局布线，紧接着敲 OpenFPGA 自己的命令。

VPR 组只注册了两条命令：
- **`vpr`**：调用 VPR 核心引擎，**保留** VPR 的布局布线结果（这些结果会被后续 `link_openfpga_arch` 等命令使用）。
- **`vpr_standalone`**：同样调用 VPR，但**不保留**结果，用于独立的、一次性的 VPR 调用。

#### 4.3.2 核心流程

```text
add_vpr_commands(shell)
  ├── add_command_class("VPR")                  // 建类别
  ├── Command("vpr")
  │     ├── add_command(...) → shell_cmd_vpr_id
  │     ├── set_command_class(..., vpr_cmd_class)
  │     └── set_command_execute_function(..., vpr::vpr_wrapper)   // MACRO，自带解析器
  └── Command("vpr_standalone")
        └── set_command_execute_function(..., vpr::vpr_standalone_wrapper)
```

注意 `vpr` 是上一讲提到的 **MACRO 类型**命令：它的选项格式（`vpr arch.xml circuit.blif --route_chan_width 100 ...`）由 VPR 自己解析，OpenFPGA shell 只负责把整行参数透传给 `vpr::vpr_wrapper`。这也是为什么 shell 在执行 MACRO 命令时会**跳过自己的选项解析**（详见 4.5 节引用的 `shell.tpp`）。

#### 4.3.3 源码精读

[vpr_command.cpp:11-38](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/vpr_wrapper/vpr_command.cpp#L11-L38) —— `add_vpr_commands` 的全部内容：创建 `VPR` 类别后注册 `vpr`（绑定 `vpr::vpr_wrapper`，描述强调「will keep VPR results」）和 `vpr_standalone`（绑定 `vpr::vpr_standalone_wrapper`，描述强调「will NOT keep VPR results」）。两条命令都没加选项、都没声明依赖——因为 VPR 是整条流水线的**起点**。

#### 4.3.4 代码实践

**实践目标**：理解 `vpr` 与 `vpr_standalone` 的唯一区别。

**操作步骤**：

1. 读 `vpr_command.cpp` 里两条命令的描述字符串。
2. （可选，待本地验证）如果你已经编译好 `openfpga`，在交互模式里分别执行 `vpr --help` 和 `vpr_standalone --help`，观察 VPR 自己打印的帮助。

**需要观察的现象**：两条命令的描述只差「keep / NOT keep VPR results」。

**预期结果**：在正常的 OpenFPGA 流程脚本里，**只应该用 `vpr`**（因为后续命令需要 VPR 结果）；`vpr_standalone` 适合调试或独立运行 VPR。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `vpr` 命令没有用 `shell_cmd.add_option(...)` 添加任何选项，但实际使用时却能接受 `--route_chan_width 100` 这样的大量参数？

**参考答案**：因为 `vpr` 是 MACRO 类型命令，shell 不替它解析选项，而是把整行 token 透传给 `vpr::vpr_wrapper`，由 VPR 自己的命令行解析器处理。所以 OpenFPGA 这边不需要、也不应该预先声明 VPR 的选项。

**练习 2**：`vpr` 是整个依赖图的「根」（没有任何命令在它之前），代码里是如何体现这一点的？

**参考答案**：`vpr` 注册时没有调用 `set_command_dependency`；反过来，setup 组里多条命令（`link_openfpga_arch`、`check_netlist_naming_conflict` 等）都把 `vpr` 的 id 放进了自己的依赖列表，证明 `vpr` 是它们的前置条件。

---

### 4.4 OpenFPGA setup 命令组：架构读取与 fabric 构建

#### 4.4.1 概念说明

setup 组是**命令最多、最核心**的一组。它覆盖了从「读架构文件」到「构建 fabric 模块图」的整段流程，是后续 verilog / bitstream / sdc / spice 四组的共同前置。setup 组的命令大致可以按职能再细分：

| 子职能 | 代表命令 |
|---|---|
| 物理约束转换 | `pcf2place`、`pcf2sdc`、`pcf2bitstream_setting` |
| 架构 / 设置读写 | `read_openfpga_arch`、`write_openfpga_arch`、`read_openfpga_simulation_setting`、`read_openfpga_bitstream_setting`、`read_mif` |
| 时钟架构 | `read_openfpga_clock_arch`、`append_clock_rr_graph`、`route_clock_rr_graph` |
| 架构链接 | `link_openfpga_arch` |
| 网表修正 | `check_netlist_naming_conflict`、`pb_pin_fixup`、`lut_truth_table_fixup` |
| **fabric 构建** | `build_fabric`、`add_fpga_core_to_fabric` |
| fabric 信息输出 | `write_fabric_key`、`write_fabric_hierarchy`、`write_fabric_io_info`、`rename_modules`、`report_reference`、`read_unique_blocks` / `write_unique_blocks` |

其中最关键的三条命令构成了一条主链：`read_openfpga_arch` → `link_openfpga_arch` → `build_fabric`。

#### 4.4.2 核心流程

setup 组内部通过精心构造的依赖边，把命令串成一条**必须按序执行**的链：

```text
vpr ──┐
      ├──► link_openfpga_arch ──► build_fabric ──► {write_fabric_key,
read_openfpga_arch ─┘                                    write_fabric_hierarchy,
                                                         add_fpga_core_to_fabric,
                                                         rename_modules, ...}
```

- `link_openfpga_arch` 依赖 `read_openfpga_arch`、`read_openfpga_simulation_setting`、`vpr`——它要把 OpenFPGA 架构**绑定**到 VPR 跑完后的 device context 上，所以三者缺一不可。
- `build_fabric` 依赖 `link_openfpga_arch`——必须先完成架构链接，才能构建模块图。
- 一大批「fabric 信息输出」命令（`write_fabric_key`、`write_fabric_hierarchy` 等）都依赖 `build_fabric`。

这些依赖边不是文档约定，而是写死在注册代码里、并由 shell 在运行时强制检查的（见 4.5.2）。

#### 4.4.3 源码精读

`link_openfpga_arch` 的依赖声明清晰地展示了「跨命令、跨前置条件」的依赖构造：

[openfpga_setup_command_template.h:1436-1446](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1436-L1446) —— 构造 `link_arch_dependent_cmds` 列表，依次放入 `read_arch_cmd_id`、`read_sim_setting_cmd_id`、`vpr_cmd_id`，再传给 `add_link_arch_command_template`。这正是「link 必须在 read_arch / read_sim_setting / vpr 之后」的代码证据。

`build_fabric` 依赖 `link_openfpga_arch`：

[openfpga_setup_command_template.h:1518-1523](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L1518-L1523) —— 把 `link_arch_cmd_id` 放进 `build_fabric_dependent_cmds`，传给 `add_build_fabric_command_template`。

`build_fabric` 自身的选项非常丰富（压缩布线、分组 tile、fabric key 等），但注册结构与 4.2 讲的四步法完全一致：

[openfpga_setup_command_template.h:401-472](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command_template.h#L401-L472) —— `add_build_fabric_command_template`：依次添加 `--frame_view`、`--compress_routing`、`--duplicate_grid_pin`、`--load_fabric_key`、`--write_fabric_key`、`--group_tile`、`--generate_random_fabric_key` 等选项，最后绑定执行函数 `build_fabric_template<T>`。

包装层把模板实例化为针对 `OpenfpgaContext` 的具体注册函数：

[openfpga_setup_command.cpp:13-15](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_setup_command.cpp#L13-L15) —— `add_openfpga_setup_commands` 只有一行：调用 `add_setup_command_templates<OpenfpgaContext>(shell)`，把模板里的 `<T>` 全部替换为 `OpenfpgaContext`。verilog / bitstream / sdc / spice 组的包装文件都是同样的极薄结构。

#### 4.4.4 代码实践

**实践目标**：在源码里还原 setup 组「主链」的依赖关系。

**操作步骤**：

1. 打开 `openfpga_setup_command_template.h`，定位到 `add_setup_command_templates` 函数（约 1330 行起）。
2. 依次找出这些代码块，并记录每条命令的 `dependent_cmds` 列表：`link_openfpga_arch`、`build_fabric`、`write_fabric_key`、`write_fabric_hierarchy`、`add_fpga_core_to_fabric`。

**需要观察的现象**：你会看到这些命令的依赖层层递进——后一组命令依赖 `build_fabric`，`build_fabric` 依赖 `link_openfpga_arch`，`link_openfpga_arch` 又依赖更前面的读架构 / vpr 命令。

**预期结果**：画出一张 setup 组内部的依赖 DAG（有向无环图），叶子是 `vpr` / `read_openfpga_arch`，根是各输出命令。

#### 4.4.5 小练习与答案

**练习 1**：`build_fabric` 依赖 `link_openfpga_arch`，而 `link_openfpga_arch` 又依赖 `vpr`。如果你在脚本里**跳过** `link_openfpga_arch` 直接运行 `build_fabric`，shell 会怎么处理？

**参考答案**：shell 在执行 `build_fabric` 前会检查它的依赖列表，发现 `link_openfpga_arch` 从未执行（状态为 `CMD_EXEC_NONE`），于是打印 `Command 'link_openfpga_arch' is required to be executed before command 'build_fabric'!`，打印 `build_fabric` 的选项帮助，并把本次执行标记为致命错误返回（见 4.5.2 引用的 `shell.tpp`）。注意：shell **只直接检查一级依赖**，不会递归替你跑 `vpr`——所以正确做法是按顺序把 `vpr`、`read_openfpga_arch`、`link_openfpga_arch` 都写进脚本。

**练习 2**：setup 组里 `pcf2place` 这类命令注册时没有传 `dependent_cmds`，这意味着它可以在任何时刻运行吗？

**参考答案**：注册时不声明依赖，只代表 shell 不做前置检查；但该命令在运行时仍可能因为缺少必要输入文件（如 `.pcf`、`.blif`、io location map）而失败。「不声明依赖」≠「真的没有前置条件」，只是这些前置条件是数据文件而非其他 shell 命令。

---

### 4.5 FPGA-Bitstream 命令组与命令依赖图

#### 4.5.1 概念说明

bitstream 组命令不多，但它是**理解命令依赖机制最好的教材**，因为它内部有一条教科书级别的线性依赖链。该组注册了 6 条命令：

| 命令 | 作用 | 直接依赖 |
|---|---|---|
| `repack` | 在物理 pb 上重新打包 netlist | `build_fabric` |
| `build_architecture_bitstream` | 生成与 fabric 无关的 device 级比特流 | `repack` |
| `report_bitstream_distribution` | 报告比特流分布 | （注册时未显式声明） |
| `build_fabric_bitstream` | 把 device 比特流重组为 fabric 级比特流 | `build_architecture_bitstream` |
| `write_fabric_bitstream` | 把 fabric 比特流写成文件 | `build_fabric_bitstream` |
| `write_io_mapping` | 输出 IO 映射信息 | `build_fabric` |

这条链 `repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream` 正是 u7（比特流生成）要深入的主题，本讲只关注它如何被**注册和约束**。

#### 4.5.2 核心流程

依赖链的构造与运行时检查：

```text
注册阶段（构造依赖边）：
  repack                      → 依赖 build_fabric
  build_architecture_bitstream → 依赖 repack
  build_fabric_bitstream      → 依赖 build_architecture_bitstream
  write_fabric_bitstream      → 依赖 build_fabric_bitstream

运行阶段（execute_command 里强制检查）：
  对每条命令，遍历它的依赖列表；
  若任一前置命令「从未执行」或「执行失败」
    → 打印 "Command 'X' is required to be executed before command 'Y'!"
    → 打印当前命令的选项帮助
    → 标记当前命令为 CMD_EXEC_FATAL_ERROR 并返回
```

关键点：**依赖是运行时硬约束，不是注释**。shell 会真的拦截并报错。

#### 4.5.3 源码精读

bitstream 组开头反查 `build_fabric` 并创建类别：

[openfpga_bitstream_command_template.h:300-310](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L300-L310) —— `add_bitstream_command_templates` 反查 `build_fabric` 的 id，创建 `"FPGA-Bitstream"` 类别。

`repack` 依赖 `build_fabric`：

[openfpga_bitstream_command_template.h:315-319](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L315-L319) —— 构造 `cmd_dependency_repack` 只含 `build_fabric` 的 id，注册 `repack`。

整条链的依赖依次构造：

[openfpga_bitstream_command_template.h:322-368](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_bitstream_command_template.h#L322-L368) —— 依次注册 `build_architecture_bitstream`（依赖上一条 `repack`）、`build_fabric_bitstream`（依赖 `build_architecture_bitstream`）、`write_fabric_bitstream`（依赖 `build_fabric_bitstream`），形成一条严格递进的依赖链。

依赖的运行时强制检查位于 `Shell<T>` 的执行入口：

[shell.tpp:594-605](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L594-L605) —— `execute_command` 在真正调用执行函数之前，遍历 `command_dependencies_[cmd_id]`，若任一前置命令的状态是 `CMD_EXEC_NONE`（未运行）或 `CMD_EXEC_FATAL_ERROR`（失败），就打印告警、打印选项帮助、把当前命令标记为致命错误并返回。这就是「依赖」具有强制力的根源。

（顺带一说，MACRO 命令的「跳过 shell 选项解析」也发生在同一个 `execute_command` 里，紧随依赖检查之后：）

[shell.tpp:607-628](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/libs/libopenfpgashell/src/base/shell.tpp#L607-L628) —— 当命令类型为 `MACRO` 时，shell 直接把 token 转成 `argv` 传给宏函数，不做选项解析——这解释了 4.3 节 `vpr` 命令为何无需预先声明选项。

#### 4.5.4 代码实践

**实践目标**：亲手验证「依赖是运行时硬约束」。

**操作步骤**（待本地验证，需先编译好 `openfpga`）：

1. 启动交互模式：`openfpga -i`。
2. **不**运行任何前置命令，直接输入 `write_fabric_bitstream -f bitstream.txt`。
3. 观察输出。

**需要观察的现象**：shell 会打印类似 `Command 'build_fabric_bitstream' is required to be executed before command 'write_fabric_bitstream'!` 的告警，随后打印 `write_fabric_bitstream` 的选项说明，命令以错误结束，**不会**生成 `bitstream.txt`。

**预期结果**：你亲眼看到依赖检查拦截了一次非法调用。这正是 4.5.3 引用的 `shell.tpp:594-605` 的运行时表现。

**源码阅读型替代实践**（无需编译）：在 `shell.tpp` 的 `execute_command` 中，找到第 594-605 行的依赖检查循环，对照 `command_status_` 的取值（`CMD_EXEC_NONE` / `CMD_EXEC_FATAL_ERROR` / 成功），解释为什么「前置命令失败」也会阻塞当前命令。

#### 4.5.5 小练习与答案

**练习 1**：为什么 repack 必须在 `build_architecture_bitstream` **之前**，而不能之后？

**参考答案**：`build_architecture_bitstream` 要根据**物理打包后的 netlist** 来解码每个 LUT 的真值表和布线 mux 的选择位（详见 u7-l2、u9-l3）。repack 负责把逻辑 netlist 重新打包到物理 pb 上并修正真值表。如果先生成比特流再 repack，比特流里记录的就是「错误的、未对齐到物理 pb 的」配置位。因此依赖图强制 `repack → build_architecture_bitstream` 的顺序。

**练习 2**：`report_bitstream_distribution` 在注册时没有显式声明依赖（见 `add_bitstream_command_templates` 里它传入的依赖向量为空），这会不会导致它在 `build_architecture_bitstream` 之前被调用时「静默成功但输出错误」？

**参考答案**：有可能。因为 shell 不会拦截它，它会直接进入执行函数；如果此时 device 比特流尚未生成，执行函数内部应当自行检查并报错（或输出空报告）。这说明「依赖声明」只能覆盖「命令级」前置条件，「数据级」前置条件仍需各命令的执行函数自行保障。这也是 OpenFPGA 流程脚本（如 `example_script.openfpga`）要严格按顺序书写的原因。

---

### 4.6 FPGA-Verilog / FPGA-SDC / FPGA-SPICE 命令组：产物输出

#### 4.6.1 概念说明

这三组的共同点是：它们**几乎不改变 fabric 状态，而是把 `build_fabric` 之后的结果「翻译」成各种下游需要的产物文件**。因此它们都依赖 `build_fabric`，并且大量使用只读（const）执行函数。

- **FPGA-Verilog**：生成 fabric 的 Verilog 网表（`write_fabric_verilog`）和各类验证用 testbench（`write_full_testbench`、`write_preconfigured_fabric_wrapper`、`write_preconfigured_testbench`、`write_mock_fpga_wrapper`、`write_testbench_template`、`write_testbench_io_connection`、`write_simulation_task_info`）。
- **FPGA-SDC**：生成时序约束——`write_pnr_sdc`（给后端布局布线用）、`write_analysis_sdc`（给静态时序分析用）、`write_configuration_chain_sdc`、`write_sdc_disable_timing_configure_ports`。
- **FPGA-SPICE**：目前只注册了 `write_fabric_spice`（生成晶体管级 SPICE 网表），其余 testbench 命令在源码里是 TODO，尚未实现。

#### 4.6.2 核心流程

三组的注册结构高度一致：

```text
add_<group>_command_templates(shell)
  ├── const build_fabric_id = shell.command("build_fabric")   // 反查 build_fabric
  ├── add_command_class("<FPGA-Verilog|FPGA-SDC|FPGA-SPICE>")
  └── 对每条命令：
        dependent_cmds = { build_fabric_id }
        add_xxx_command_template(shell, class_id, dependent_cmds, hidden)
```

也就是说，这三组里的绝大多数命令都把 `build_fabric` 作为唯一前置条件。

#### 4.6.3 源码精读

FPGA-Verilog 组反查 `build_fabric` 并注册 `write_fabric_verilog`：

[openfpga_verilog_command_template.h:676-693](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L676-L693) —— `add_verilog_command_templates` 反查 `build_fabric` id、创建 `"FPGA-Verilog"` 类别，然后注册 `write_fabric_verilog`（依赖 `build_fabric`）。随后同一函数里依次注册各 testbench 命令，全部依赖 `build_fabric`。

FPGA-SDC 组的类别与四条命令：

[openfpga_sdc_command_template.h:265-314](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_sdc_command_template.h#L265-L314) —— `add_openfpga_sdc_command_templates` 创建 `"FPGA-SDC"` 类别，注册 `write_pnr_sdc`、`write_configuration_chain_sdc`、`write_sdc_disable_timing_configure_ports`、`write_analysis_sdc`，每条都依赖 `build_fabric`。

FPGA-SPICE 组（目前仅一条命令）：

[openfpga_spice_command_template.h:72-99](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_spice_command_template.h#L72-L99) —— `add_spice_command_templates` 创建 `"FPGA-SPICE"` 类别，注册 `write_fabric_spice`（依赖 `build_fabric`），其余 testbench 命令以 TODO 注释占位、尚未注册。

值得对比的是 verilog 组的 `write_pnr_sdc` 用了 `set_command_const_execute_function`（只读，因为只是输出约束文件），而 `write_fabric_verilog` 用了可写的 `set_command_execute_function`——这反映出 verilog 生成过程会把网表信息存入 context 的 `NetlistManager`（供后续 testbench 命令复用），而 SDC 输出是纯查询。

#### 4.6.4 代码实践

**实践目标**：验证「三组的命令都以 `build_fabric` 为前置依赖」。

**操作步骤**：

1. 分别打开 verilog / sdc / spice 三个 `*_command_template.h`，跳到各自的顶层注册函数（`add_verilog_command_templates`、`add_openfpga_sdc_command_templates`、`add_spice_command_templates`）。
2. 数一下每个函数里 `push_back(build_fabric_id)` 或等价依赖构造出现的次数。

**需要观察的现象**：几乎每条命令的依赖列表里都包含 `build_fabric`。

**预期结果**：你得出结论——在这三组里，`build_fabric` 是几乎所有命令的共同「闸门」。这也是为什么 OpenFPGA 的流程脚本里 `build_fabric` 总出现在 verilog / bitstream / sdc / spice 命令之前。

#### 4.6.5 小练习与答案

**练习 1**：FPGA-SPICE 组在源码注释里列了一长串 TODO（`write_spice_top_testbench` 等），但 `help` 里看不到它们，为什么？

**参考答案**：因为这些命令**尚未注册**——它们只是写在注释里作为未来计划。只有真正调用 `add_command` 注册过的命令才会出现在 `help` 和依赖图里。`help` 只展示已注册（且非 hidden）的命令。

**练习 2**：`write_pnr_sdc` 和 `write_analysis_sdc` 都属于 FPGA-SDC 组，都依赖 `build_fabric`。它们的区别是什么？

**参考答案**：`write_pnr_sdc` 生成的是**约束后端布局布线工具**的 SDC（告诉工具哪些路径要约束、配置端口时序如何处理）；`write_analysis_sdc` 生成的是**静态时序分析**用的 SDC（针对已经布局布线完成、映射了具体 benchmark 的 fabric）。两者服务的前端阶段不同，详见 u8-l4。

---

### 4.7 Basic 命令组与「必须最后注册」的铁律

#### 4.7.1 概念说明

Basic 组注册的是与 FPGA 业务无关的通用 shell 命令：`exit`（退出）、`version`（打印版本）、`hidden_version`（隐藏的内部版本命令）、`source`（执行一段命令字符串或脚本文件）、`ext_exec`（执行外部系统命令）、`help`（打印所有命令的帮助）。

这一组有两条铁律：

1. **Basic 必须是最后一个注册的组**——`openfpga_shell.cpp` 构造函数里专门用注释标出 `This MUST be the last command group to be added!`。
2. **`help` 必须是 Basic 组内最后注册的命令**——`basic_command.cpp` 里同样有注释说明。

#### 4.7.2 核心流程

```text
add_basic_commands(shell)
  ├── add_command_class("Basic")
  ├── exit        (BUILTIN，lambda 调 shell.exit())
  ├── version     (BUILTIN，print_openfpga_version_info)
  ├── hidden_version (hidden=true)
  ├── source      (可执行命令字符串/文件，支持 --from_file、--batch_mode)
  ├── ext_exec    (执行外部系统命令)
  └── help        (BUILTIN，lambda 调 shell.print_commands())  ← 必须最后！
```

`help` 之所以必须最后注册，是因为它的执行函数要「枚举所有已注册命令」来打印帮助桌面；若在它之前还有命令没注册，帮助输出就不完整。

#### 4.7.3 源码精读

构造函数里对「最后注册」的强调：

[openfpga_shell.cpp:37-41](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_shell.cpp#L37-L41) —— 注释明确：Basic 是最后一个被添加的命令组。

Basic 组内部，`help` 必须最后注册的原因：

[basic_command.cpp:83-128](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/basic_command.cpp#L83-L128) —— `add_basic_commands`：创建 `"Basic"` 类别，依次注册 `exit`、`version`、`hidden_version`、`source`、`ext_exec`，最后注册 `help`。注释（118-121 行）说明 `help` 必须最后添加，因为绑定其执行函数时会对 shell 的命令集做一次快照。

`exit`、`version`、`help` 都用 BUILTIN 类型的执行函数（无参数 lambda）：

[basic_command.cpp:87-100](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/basic_command.cpp#L87-L100) —— `exit` 与 `version` 的注册：二者都不需要 context、不需要命令选项，是典型的 BUILTIN 命令，执行函数分别是 `[shell]() { shell.exit(); }` 和 `print_openfpga_version_info`。

#### 4.7.4 代码实践

**实践目标**：确认 Basic 组的命令清单与 `help` 的「最后注册」约束。

**操作步骤**：

1. 读 `basic_command.cpp` 的 `add_basic_commands`，按代码顺序列出注册的命令。
2. 找到 118-121 行的注释，理解 `help` 必须最后注册的原因。

**需要观察的现象**：`help` 是该函数里最后一条 `add_command` 调用。

**预期结果**：Basic 组共 6 条命令（`exit`、`version`、`hidden_version`、`source`、`ext_exec`、`help`），其中 `hidden_version` 是隐藏命令（不出现在普通 `help` 里），`help` 最后注册。

#### 4.7.5 小练习与答案

**练习 1**：如果你在 `add_basic_commands` 里把 `help` 的注册移到 `exit` 之前，会怎样？

**参考答案**：`help` 绑定执行函数时会对当前已注册的命令集做快照。若 `help` 提前注册，此后注册的 `source`、`ext_exec` 等命令就不会出现在帮助输出里，`help` 的内容将不完整。这就是注释强调「help MUST be the last to add」的原因。

**练习 2**：`source` 命令（Basic 组）和启动时的脚本模式（`openfpga -f script.openfpga`，见 u2-l1）有什么关系？

**参考答案**：二者本质都是「按行读取并执行一段命令序列」。启动脚本模式是在 `start()` 里通过 `shell_.run_script_mode(...)` 进入的；而 `source` 命令则允许在**已运行的 shell 内部**再加载一段命令字符串或脚本文件（`--from_file` 控制来源、`--batch_mode` 控制是否批处理）。它们复用了同一套「逐行执行命令」的内核，只是入口不同。

---

## 5. 综合实践

**任务**：用 `help` 命令亲手验证本讲解析的「七大类别 + 命令依赖」模型，并把它整理成一张可查阅的速查表。

**操作步骤**（待本地验证，需先按 u1-l3 编译出 `openfpga`）：

1. `source openfpga.sh` 后运行 `openfpga -i` 进入交互模式。
2. 输入 `help`，你会看到按七大类别分组列出的全部命令。把输出**按类别**抄进一张表，每类至少记录 2-3 条命令。
3. 对每个类别**各挑一条命令**，用 `<命令名> --help`（或直接敲命令名看 shell 打印的选项帮助）查看其选项。例如：
   - VPR：`vpr_standalone`（观察它如何透传 VPR 自己的帮助）
   - OpenFPGA setup：`build_fabric --help`（观察 `--compress_routing`、`--load_fabric_key` 等选项）
   - FPGA-Bitstream：`write_fabric_bitstream --help`（观察 `--format`、`--fast_configuration`）
   - FPGA-Verilog：`write_fabric_verilog --help`（观察 `--explicit_port_mapping`、`--include_timing`）
   - FPGA-SDC：`write_pnr_sdc --help`
   - FPGA-SPICE：`write_fabric_spice --help`
   - Basic：`source --help`（观察 `--from_file`、`--batch_mode`）
4. 把这张表与第 4 节源码里 `add_*_command_template` 注册的选项逐一对照，确认**源码里 `add_option` 的每一个选项都出现在 `--help` 输出里**。
5. 进阶验证依赖：在干净状态下（不跑前置命令）直接执行 `write_fabric_verilog -f ./out`，确认 shell 因缺少 `build_fabric` 前置而拒绝执行（对应 4.5.3 的依赖检查）。

**预期结果**：你得到一张「类别 → 命令 → 主要选项」的速查表，并亲眼看到「命令依赖被运行时强制」的现象。这张表会成为你后续阅读 `example_script.openfpga`（u4-l1）时的最佳导航。

> 若尚未编译 `openfpga`，可改为**源码阅读型实践**：对第 4 节列出的每个 `add_*_command_template.h`，用 Grep 统计 `add_option(` 出现的次数，自制一张「命令 → 选项数量」表，效果等价。

## 6. 本讲小结

- OpenFPGA 的全部命令在 `OpenfpgaShell` 构造函数里按 **VPR → setup → verilog → bitstream → spice → sdc → Basic** 的固定顺序注册，顺序不可调换，因为后续组要反查前置组注册的命令（`vpr`、`build_fabric`）来构造依赖边。
- 每条命令都遵循**四步注册法**：建 `Command` + 加选项 → `add_command` → `set_command_class` 归类 → 绑定执行函数（`set_command_execute_function` 可写 / `set_command_const_execute_function` 只读）+ 可选 `set_command_dependency`。
- `vpr` 是 **MACRO** 类型命令，shell 不替它解析选项，而是把整行参数透传给 VPR；它是整条依赖图的根。
- setup 组是核心，主链为 `read_openfpga_arch → link_openfpga_arch → build_fabric`；bitstream 组有一条教科书级依赖链 `repack → build_architecture_bitstream → build_fabric_bitstream → write_fabric_bitstream`。
- **命令依赖是运行时硬约束**：`shell.tpp` 的 `execute_command` 会在执行前检查依赖，前置命令未运行或失败时直接报错返回。
- Basic 组必须最后注册，且其中 `help` 必须最后添加，因为它要对已注册命令集做快照来打印帮助桌面。

## 7. 下一步学习建议

- **下一讲 u2-l3（OpenfpgaContext）**：本讲反复提到执行函数会「读写 context」，下一讲将打开 `OpenfpgaContext` 这个全局数据中枢，看清 `read_openfpga_arch` 写进去的 `arch_`、`build_fabric` 写进去的 `module_graph_`、bitstream 命令写进去的 `bitstream_manager_` 究竟长什么样，从而把「命令」和「数据」对应起来。
- **u4-l1（example_script.openfpga 全解析）**：把本学到的七大类别命令放进一条真实流程脚本里，按「输入 → 处理 → 输出」的顺序再走一遍，体会命令依赖链在实战中的体现。
- **u10-l4（扩展 Shell：新增命令）**：如果你想仿照四步法注册一条自己的命令，那一讲会完整讲解 `Shell<T>` 模板、`Command` / `CommandContext` / 执行函数类型与退出码，是本讲「注册机制」的进阶实操。
- 继续阅读建议：直接打开 `openfpga/src/base/openfpga_setup_command_template.h` 的 `add_setup_command_templates` 函数通读一遍，它是理解整个命令体系最浓缩的一份「目录」。
