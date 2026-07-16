# Testbench 生成：full 与 preconfigured

## 1. 本讲目标

上一讲（u8-l1）我们把内存中的 `ModuleManager` 模块图翻译成了一套可编译的 fabric Verilog 网表。但 fabric 网表本身只是「一颗空 FPGA」，它并不能回答最关键的问题：**这颗 FPGA 配上这份比特流之后，真的实现了用户的设计吗？**

本讲就来解决这个问题——OpenFPGA 如何自动生成 testbench（测试平台），让你用仿真器（Icarus/iverilog、ModelSim、VCS 等）一键验证「fabric + 比特流」是否等价于原始设计。学完本讲，你应当能够：

- 区分两种验证策略：**full testbench**（含编程阶段的完整验证）与 **preconfigured testbench**（跳过编程阶段的快速验证）；
- 说清三条命令——`write_full_testbench`、`write_preconfigured_fabric_wrapper`、`write_preconfigured_testbench`——各自生成什么、彼此如何配合；
- 理解 preconfigured wrapper 是如何通过 `force`/`$deposit` 把比特流「内嵌」进网表、从而跳过编程阶段的；
- 认识 `reference_benchmark_file_path` 这个关键选项如何决定 testbench 是否带「自检（self-checking）」代码；
- 能对照源码讲出 full testbench 的「编程阶段 + 工作阶段」两段式仿真结构，以及自检错误计数器的工作原理。

## 2. 前置知识

在进入源码前，先用最直白的话把几个概念讲透。

### 什么是 testbench

在数字电路仿真里，被验证的电路叫 **DUT（Design Under Test，待测设计）**。testbench 是一段「包裹」DUT 的 Verilog 代码，它负责三件事：

1. **驱动**：给 DUT 的输入端口施加激励（stimulus），比如时钟翻转、随机输入向量；
2. **采样**：观察 DUT 的输出端口；
3. **判定**：把 DUT 的输出和一份「标准答案」逐拍对比，发现不一致就报错。

OpenFPGA 生成的 DUT 就是 fabric 顶层模块（`fpga_top`，由 u8-l1 的 `write_fabric_verilog` 产出）。testbench 要做的判定，是把 `fpga_top` 的输出和用户的原始设计（reference benchmark）做对比。

### FPGA 仿真独有的难点：先要「编程」

普通 ASIC 仿真上电就能跑；FPGA 不行。FPGA 上电后内部成千上万的配置位（config bit）都是未知值，必须先把**比特流（bitstream）写进配置存储器**，FPGA 才「变成」目标电路。这个写比特流的过程叫**编程阶段（programming/configuration phase）**；编程完成后才进入正常工作，叫**工作阶段（operating phase）**。

这就引出了本讲的核心权衡——

- **完整验证**：在 testbench 里老老实实地把比特流「一拍一拍」移进 FPGA（扫描链移位、存储器组寻址……），完整模拟真实芯片的编程过程。**慢**，但既验证了用户电路，也验证了配置协议电路本身（扫描链、译码器是否接对）。
- **快速验证**：跳过编程阶段，直接用 Verilog 的 `force`/`assign` 或系统任务 `$deposit` 把每个配置位的值「强行写死」，让仿真一开始 FPGA 就已经是配置好的状态。**快**，适合功能快验和形式验证，但**不验证配置协议电路**。

OpenFPGA 把这两种策略分别做成了命令，这就是本讲要拆的三条命令。

### 自检（self-checking）与 reference benchmark

「标准答案」从哪来？来自用户的原始 Verilog 设计（综合前的那个 `.v`，或综合后的网表）。在 `task.conf` 里它通常用变量 `${REFERENCE_VERILOG_TESTBENCH}` 表示。testbench 会把这个 reference 模块和 `fpga_top` **并排实例化**，喂同样的输入，逐拍比对输出。只要 reference 文件给了，testbench 就自动带自检代码；不给，就只喂随机激励、不判定（让你自己外接检查器）。

> 名词速查：**DUT**（待测设计）、**stimulus**（激励）、**programming phase**（编程阶段）、**operating phase**（工作阶段）、**self-checking**（自检）、**force/`$deposit`**（Verilog 强制赋值/注入系统任务）。

## 3. 本讲源码地图

本讲涉及的关键文件，按「命令注册 → 执行模板 → 编排 → 内核」四层组织：

| 文件 | 作用 |
| --- | --- |
| `openfpga/src/base/openfpga_verilog_command_template.h` | 注册三条 testbench 命令、定义它们的选项与依赖 |
| `openfpga/src/base/openfpga_verilog_template.h` | 三条命令的执行模板：把命令选项装进 `VerilogTestbenchOption` 并调用编排函数 |
| `openfpga/src/fpga_verilog/verilog_api.cpp` | 编排层：`fpga_verilog_full_testbench` / `fpga_verilog_preconfigured_fabric_wrapper` / `fpga_verilog_preconfigured_testbench` |
| `openfpga/src/fpga_verilog/verilog_top_testbench.cpp` | **full testbench 内核**：`print_verilog_full_testbench`，含编程/工作两阶段、时钟门控 |
| `openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp` | **preconfigured wrapper 内核**：`print_verilog_preconfig_top_module`，含 force/deposit 内嵌比特流 |
| `openfpga/src/fpga_verilog/verilog_template_testbench.cpp` | 骨架型 template testbench（`write_testbench_template` 的内核，供参考） |
| `openfpga/src/fpga_verilog/verilog_formal_random_top_testbench.cpp` | **preconfigured testbench 内核**：`print_verilog_random_top_testbench`，随机向量驱动 |
| `openfpga/src/fpga_verilog/verilog_testbench_utils.cpp` | 公共工具：自检逻辑 `print_verilog_testbench_check`、超时与 VCD、`Simulation Succeed/Failed` 打印 |
| `openfpga/src/fpga_verilog/verilog_testbench_options.cpp` / `.h` | **testbench 选项数据模型** `VerilogTestbenchOption` |
| `openfpga/src/fpga_verilog/verilog_constants.h` | 各 testbench 文件名/模块名后缀常量 |
| `openfpga_flow/openfpga_shell_scripts/iverilog_example_script.openfpga` | 三条命令联用的范例脚本 |

> 提醒：这四层是 OpenFPGA 一以贯之的命令分层（u2-l2、u8-l1 已建立）——**注册层声明选项与依赖、模板层把选项装进数据模型、编排层串起目录与文件名、内核层才是真正写 Verilog 的算法**。testbench 三条命令完全遵循这个分层，所以下文每个模块都会沿这条链路自上而下讲。

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：full testbench（4.1）、preconfigured wrapper（4.2）、preconfigured testbench（4.3）、testbench 选项与自检机制（4.4）。前三个模块各对应一条命令，4.4 是贯穿三者的选项数据模型与公共自检逻辑。

### 4.1 full testbench：含编程阶段的完整验证

#### 4.1.1 概念说明

`write_full_testbench` 生成一份**自包含**的 autocheck testbench（自动检查测试平台）。所谓「full」，是指它**完整模拟了真实 FPGA 上电的全过程**：

1. **编程阶段**：按照芯片实际的配置协议（scan_chain 一位一位移位、memory_bank 按 BL/WL 地址逐位写、frame_based 按帧地址写……），把 `fabric_bitstream.bit` 文件里的比特流一拍一拍「喂」进 fabric 的配置端口。这个过程要耗费大量时钟周期（周期数 ≈ 比特流位数），所以仿真很慢。
2. **工作阶段**：编程完成后（`config_done` 信号拉高），关掉编程时钟、打开工作时钟，施加随机输入激励，并用自检逻辑逐拍比对输出。

它的价值在于：**既验证了用户电路功能，也验证了配置协议电路本身**（扫描链是否连通、BL/WL 译码器是否正确寻址）。这是与 preconfigured 方案最本质的区别——preconfigured 用 `force` 跳过了编程，自然也就无法暴露配置电路的 bug。

#### 4.1.2 核心流程

full testbench 的生成是一个「自上而下、按文件段落顺序写」的过程，整体流程可以画成：

```
write_full_testbench 命令
        │  (注册层：声明选项 + 依赖 build_fabric)
        ▼
write_full_testbench_template      ← 把选项装进 VerilogTestbenchOption
        │  (模板层：set_reference_benchmark_file_path 等)
        ▼
fpga_verilog_full_testbench        ← 编排层：拼出输出文件名 <设计名>_autocheck_top_tb.v
        │
        ▼
print_verilog_full_testbench       ← 内核：真正写 Verilog
        │
        ├── 1. 解析 fast_configuration 是否可用、决定要跳过哪个位值
        ├── 2. 打印端口（含配置协议端口：扫描链 head/tail、BL/WL、地址等）
        ├── 3. 估算编程时钟周期数 num_config_clock_cycles
        ├── 4. 生成编程接口激励（按配置协议分派：扫描链/存储器组/帧）
        ├── 5. 实例化 fpga_top（DUT）+ 连接 IO
        ├── 6. 【编程阶段】加载比特流 print_verilog_full_testbench_bitstream
        ├── 7. 【工作阶段】随机激励 print_verilog_testbench_random_stimuli
        ├── 8. 【自检】输出比对 print_verilog_testbench_check
        └── 9. 超时 + VCD + "Simulation Succeed/Failed"
```

两阶段的关键在**时钟门控**：编程时钟只在编程阶段翻转，一旦 `config_done` 拉高就被强制拉平；工作时钟在编程结束后才启动。这样仿真时间线就被清晰地切成两段。

#### 4.1.3 源码精读

**命令注册**：`write_full_testbench` 的选项在注册层定义，依赖 `build_fabric`（testbench 必须有 fabric 才能写）。

[openfpga/src/base/openfpga_verilog_command_template.h:105-159](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L105-L159) 定义了该命令的全部选项。几个最关键的：

- `--file/-f`：输出目录（必需）；
- `--bitstream`：要加载的 fabric 比特流文件（必需）——注意 full testbench 是**从文件读**比特流的；
- `--reference_benchmark_file_path`：reference 设计路径，**给了才生成自检代码**；
- `--fast_configuration`：开启快速配置（跳过部分位，详见 u7-l4）；
- `--no_self_checking`：显式关闭自检。

依赖关系见 [openfpga/src/base/openfpga_verilog_command_template.h:698-703](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L698-L703)：`full_testbench_dependent_cmds` 仅含 `build_fabric_cmd_id`，与 u2-l2 讲的「依赖只查一级」一致。

**执行模板**：模板把命令选项一一装进 `VerilogTestbenchOption`，并强制 `set_print_top_testbench(true)`。

[openfpga/src/base/openfpga_verilog_template.h:117-131](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h#L117-L131) 展示了这条装配链：构造一个 `VerilogTestbenchOption options;`，依次 set 各字段，最后 `options.set_print_top_testbench(true);`。注意 `--bitstream` 的值并不放进 `options`，而是作为独立参数透传给编排函数（见下文 4.4 关于「full 从文件读、wrapper 从内存读」的区别）。

**编排层**：拼出输出文件名并调用内核。

[openfpga/src/fpga_verilog/verilog_api.cpp:199-209](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L199-L209) 用后缀常量拼出 `<设计名>_autocheck_top_tb.v`（后缀定义见 [verilog_constants.h:24-26](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_constants.h#L24-L26)），再调用 `print_verilog_full_testbench`。注释明确写了「including configuration phase and operating phase」，点出两阶段本质。

**内核：fast_configuration 判定**。内核一开头就处理快速配置：

[openfpga/src/fpga_verilog/verilog_top_testbench.cpp:2665-2673](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2665-L2673) 计算 `apply_fast_configuration = fast_configuration && is_fast_configuration_applicable(...)`，并据此决定 `bit_value_to_skip`（跳 0 还是跳 1）。这套判定逻辑承接自 u7-l4 讲的 fast_configuration 机制。

**内核：估算编程周期数 + 生成编程接口激励（编程阶段的核心）**：

[openfpga/src/fpga_verilog/verilog_top_testbench.cpp:2694-2715](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2694-L2715) 先用 `calculate_num_config_clock_cycles(...)` 估算要把比特流移完需要多少个编程时钟周期（这就是 full testbench 慢的根源），再调用 `print_verilog_top_testbench_configuration_protocol_stimulus(...)` 生成编程接口激励——该函数会按 `config_protocol.type()` 分派到扫描链、存储器组、帧等不同的激励生成器（如 `verilog_top_testbench_memory_bank.h`）。这是「按配置协议定制」的体现。

**内核：加载比特流（编程阶段动作）+ 工作阶段随机激励**：

[openfpga/src/fpga_verilog/verilog_top_testbench.cpp:2769-2798](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2769-L2798) 依次写出：`print_verilog_full_testbench_bitstream(...)`（把比特流逐拍移进 fabric）、`print_verilog_testbench_random_stimuli(...)`（工作阶段的随机输入向量）。注意随机激励函数接收 `options.no_self_checking()`——若关闭自检，激励逻辑也会相应调整。

**内核：自检逻辑触发**：

[openfpga/src/fpga_verilog/verilog_top_testbench.cpp:2800-2815](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2800-L2815) 在 `!options.no_self_checking()` 时，调用 `print_verilog_testbench_check(...)` 生成输出逐拍比对，并额外调用 `print_verilog_top_testbench_check(...)` 给编程阶段也加一道自检（用 `config_all_done` 信号触发）。

**两阶段如何切换：编程时钟门控**。这是理解 full testbench 时间线的关键代码：

[openfpga/src/fpga_verilog/verilog_top_testbench.cpp:1294-1334](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L1294-L1334) 用一行 `assign` 把实际编程时钟表达为「原始编程时钟寄存器 &（前一级 config_done）&（~本级 config_done）&（~prog_reset）」。翻译成自然语言就是：**只有当配置还没完成（config_done=0）且不在编程复位时，编程时钟才真正翻转；config_done 一拉高，编程时钟立刻被钳为 0**。这正是「编程阶段结束、工作阶段开始」的硬件表达。

#### 4.1.4 代码实践

**实践目标**：亲手生成一份 full testbench，看清它的两阶段结构与自检代码。

**操作步骤**：

1. 按 u1-l3/u1-l4 编译并 `source openfpga.sh`，然后跑一个带 full testbench 的任务，例如：
   ```bash
   run-task basic_tests/full_testbench/configuration_chain
   ```
2. 用 `goto-task`（或直接进 `latest/`）进入结果目录，定位到 `SRC/` 下的 `<设计名>_autocheck_top_tb.v`。
3. 用文本编辑器打开它，按顺序找三段：
   - **编程时钟门控**：搜索 `config_done`，找到形如 `assign ... = prog_clock_reg ... & (~config_done)` 的行；
   - **比特流加载**：搜索 `configuration`，找到逐拍移入比特流的 `for`/`initial` 块；
   - **自检**：搜索 `nb_error`，找到错误计数器与 `Mismatch on` 的 `$display`。

**需要观察的现象**：

- 文件里有一段明显的「移位大循环」，循环次数与 `fabric_bitstream.bit` 的位数相当——这就是编程阶段耗时的来源；
- 错误计数器 `nb_error`（或同名变量）初始为 0，每个输出端口都有一个 `always` 块在比对 `fpga` 输出与 `benchmark` 输出。

**预期结果**：full testbench 文件比 fabric 任何一个子模块都大，因为它内联了整条比特流的移位过程。仿真日志末尾应打印 `Simulation Succeed`（设计与 fabric 一致）。

> 待本地验证：若本地未装 iverilog，无法实际仿真；此时只做「源码阅读型实践」——打开生成的 `_autocheck_top_tb.v`，数一下移位循环的迭代次数，再和 `fabric_bitstream.bit` 的行数对比，验证二者相等。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `write_full_testbench` 必须提供 `--bitstream` 选项，而 `write_preconfigured_fabric_wrapper` 不需要？

> **参考答案**：full testbench 要在仿真里真实地「把比特流移进 fabric」，所以必须有一个比特流文件作为输入数据；而 wrapper 是用 `force`/`$deposit` 直接把内存中 `bitstream_manager` 的值写死到配置端口，比特流来自 OpenfpgaContext 内存（见 4.2），不需要外部文件。

**练习 2**：在 full testbench 里，如果把 `--reference_benchmark_file_path` 去掉会发生什么？

> **参考答案**：`no_self_checking()` 将为真，内核不会调用 `print_verilog_testbench_check`，testbench 只施加随机激励、不比对输出、末尾恒打印 `Simulation Succeed`（见 4.4 的 `print_verilog_timeout_and_vcd`）。这种模式适合你想外接自定义检查器的场景。

---

### 4.2 preconfigured fabric wrapper：内嵌比特流，跳过编程

#### 4.2.1 概念说明

`write_preconfigured_fabric_wrapper` 生成的不是 testbench，而是一个**包装模块（wrapper module）**。它的端口和用户的 benchmark 设计完全一致，内部实例化 `fpga_top`，并在仿真 `initial` 块里用 Verilog 的强制赋值手段，**一次性把所有配置位的值「焊死」**——这就是所谓的「preconfigured（预配置）」。

跳过编程的关键技术有两种，按仿真器选择：

- **Icarus（iverilog）**：用 `assign`/`force` 语法强制驱动配置存储器的数据输出端口；
- **ModelSim/VCS**：用 `$deposit` 系统任务把值注入寄存器。

无论哪种，效果都是：仿真 `time = 0` 时 FPGA 就已经是配置好的目标电路，无需任何编程时钟周期。代价是：**配置协议电路（扫描链、译码器）完全不参与仿真**，它们的 bug 无法被这种 testbench 发现。

> 重要区分：full testbench 从**比特流文件**读取并按协议移位；wrapper 从 **OpenfpgaContext 内存里的 `bitstream_manager`**（device 级，u7-l1 讲的与协议无关的层级块树）读取，按模块层级路径直接定位每个配置位。所以 wrapper 不需要 `--bitstream` 文件。

#### 4.2.2 核心流程

```
write_preconfigured_fabric_wrapper 命令
        │  (选项含 --embed_bitstream：iverilog | modelsim | none)
        ▼
write_preconfigured_fabric_wrapper_template
        │  set_print_formal_verification_top_netlist(true)
        │  按 --embed_bitstream 设 embedded_bitstream_hdl_type
        ▼
fpga_verilog_preconfigured_fabric_wrapper   ← 输出 <设计名>_top_formal_verification.v
        │
        ▼
print_verilog_preconfig_top_module          ← 内核
        │
        ├── 1. 打印模块声明：端口完全对齐 benchmark 的 IO
        ├── 2. 实例化 fpga_top（实例名 U0_formal_verification）
        ├── 3. 连接全局端口、IO
        └── 4. 【核心】print_verilog_preconfig_top_module_load_bitstream
                ├── iverilog → force 语法（assign 强制）
                └── modelsim → $deposit 语法
```

注意第 4 步：wrapper 本质上是一个「把比特流内嵌进 Verilog」的模块，所以选项 `--embed_bitstream` 控制的是**用什么 HDL 风格来内嵌**，而不是「要不要内嵌」（默认就会用 modelsim 的 `$deposit` 内嵌）。

#### 4.2.3 源码精读

**命令注册与 `--embed_bitstream`**：

[openfpga/src/base/openfpga_verilog_command_template.h:266-271](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L266-L271) 定义了 `--embed_bitstream` 选项，提示语「Embed bitstream to the Verilog wrapper netlist; This may cause a large netlist file size」——内嵌会让网表变大，因为每个配置位的取值都被写进了源码。注意此命令**没有** `--bitstream` 选项，印证了 4.2.1 的论断。

**执行模板**：

[openfpga/src/base/openfpga_verilog_template.h:213-228](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_template.h#L213-L228) 先无条件 `set_print_formal_verification_top_netlist(true)`，再仅当用户给了 `--embed_bitstream` 时调用 `set_embedded_bitstream_hdl_type(...)`。如果用户没给该选项，`embedded_bitstream_hdl_type_` 保持构造默认值（见 4.4）。

**内核：包装模块的本质**。函数注释把设计意图讲得很透：

[openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp:341-372](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp#L341-L372) 的注释画出了 wrapper 的结构图（benchmark IO ↔ FPGA IO、bitstream → 内部配置端口），并明确写道：「we do NOT put this module in the module manager」——它不是一个标准模块（因为含 force/deposit 这类非综合构造），所以不进 ModuleManager，而是直接写文件。

**内核：按仿真器分派内嵌方式**：

[openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp:303-339](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp#L303-L339) `print_verilog_preconfig_top_module_load_bitstream` 按 `embedded_bitstream_hdl_type` 二分支：`IVERILOG` 调 `..._force_bitstream`，`MODELSIM` 调 `..._deposit_bitstream`（若为 `none` 则两者都不调，只打印注释）。

**force 风格（iverilog）**：

[openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp:142-213](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp#L142-L213) 遍历 `bitstream_manager.blocks()`，对每个含配置位的块，沿模块层级路径（`find_bitstream_manager_block_hierarchy`）拼出形如 `U0_formal_verification.grid[...].<mem>.dout` 的层次路径，再用 `print_verilog_force_wire_constant_values` 把位值强制写上去（data 与可选的 datab 都写）。

**`$deposit` 风格（modelsim）**：

[openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp:219-295](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp#L219-L295) 结构与 force 版几乎一致，唯一区别是用 `print_verilog_deposit_wire_constant_values` 产生 `$deposit` 系统任务调用，而非 `assign`/`force`。

> 层级路径的钥匙：force/deposit 都依赖「块名 == 实例名」这一对接约定（u7-l1 讲过：device 级配置块名严格等于 ModuleManager 实例名）。`FORMAL_VERIFICATION_TOP_MODULE_UUT_NAME = "U0_formal_verification"`（见 [verilog_constants.h:57-59](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_constants.h#L57-L59)）是 wrapper 内 fpga_top 实例的固定名字，层级路径就从它开始往下拼。

#### 4.2.4 代码实践

**实践目标**：生成 preconfigured wrapper，亲眼看到「内嵌比特流」长什么样。

**操作步骤**：

1. 在 4.1 实践的同一任务结果目录里，打开 `SRC/<设计名>_top_formal_verification.v`。
2. 跳到文件末尾的 `initial begin ... end` 块。
3. 对照 iverilog 范例脚本里 `write_preconfigured_fabric_wrapper --embed_bitstream iverilog`（[iverilog_example_script.openfpga:56](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/../openfpga_flow/openfpga_shell_scripts/iverilog_example_script.openfpga#L56)），确认生成的是 force 风格。

**需要观察的现象**：

- 模块声明段的端口名与 benchmark 的 IO 名完全一致（输入/输出对齐）；
- `initial` 块里有大量形如 `assign U0_formal_verification.xxx.dout = 1'b0;` 或 `$deposit(U0_formal_verification.xxx.dout, ...);` 的行，每行对应一个配置位。

**预期结果**：wrapper 文件大小与配置位总数成正比；和 full testbench 不同，这里**没有任何时钟移位循环**——这就是它「快」的原因。

> 待本地验证：可把 `--embed_bitstream iverilog` 改成 `modelsim` 重跑（需改 task.conf 对应脚本或直接命令行），对比生成的是 `$deposit` 而非 `assign`。

#### 4.2.5 小练习与答案

**练习 1**：用 preconfigured wrapper 验证一颗采用 memory_bank 配置协议的 FPGA，能否发现 BL 译码器接反的 bug？为什么？

> **参考答案**：不能。wrapper 用 force/deposit 直接写每个配置位的值，完全绕过了 BL/WL 译码器与寻址电路——译码器根本不参与仿真。要验证配置协议电路，必须用 full testbench（它会真正驱动 BL/WL 地址线）。这正是 full 与 preconfigured 在「覆盖范围」上的核心差异。

**练习 2**：`--embed_bitstream none` 会生成什么样的 wrapper？

> **参考答案**：`load_bitstream` 函数里 IVERILOG 与 MODELSIM 两个分支都不命中，只打印 `Begin/End load bitstream` 注释、不写任何 force/deposit。结果是 wrapper 实例化了 fpga_top 但配置位全是 `x`（未知），仿真无意义——通常只有配合外部 `$readmemh` 等手段时才会这么用。

---

### 4.3 preconfigured testbench：跳过编程的快速验证

#### 4.3.1 概念说明

4.2 的 wrapper 只是「预配置好的 DUT」，它自己不会跑——你需要一个 testbench 来驱动它。`write_preconfigured_testbench` 生成的就是这个驱动 testbench。它的工作方式是：

- 实例化 **preconfigured wrapper**（4.2 的产物，已内嵌比特流）作为 FPGA 侧 DUT；
- 实例化 **reference benchmark**（用户原始设计）作为黄金参考；
- 生成**随机输入向量**，同时喂给 FPGA 侧和参考侧；
- 用自检逻辑逐拍比对两侧输出。

因为跳过了编程阶段，整个仿真只有「工作阶段」，速度快得多，适合回归测试里大批量跑、也适合形式验证。它和 full testbench 形成「快/慢」互补：preconfigured 用于日常快速回归，full 用于发布前完整验证。

> 三者关系一句话：**wrapper 提供「预配置 DUT」，preconfigured testbench 驱动它；二者配套使用，共同实现「跳过编程的快速验证」。**

#### 4.3.2 核心流程

```
write_preconfigured_testbench 命令
        │  (选项含 --reference_benchmark_file_path)
        ▼
write_preconfigured_testbench_template
        │  set_reference_benchmark_file_path(...) → 触发链式效应
        │  set_print_preconfig_top_testbench(true)
        ▼
fpga_verilog_preconfigured_testbench   ← 输出 <设计名>_formal_random_top_tb.v
        │
        ▼
print_verilog_random_top_testbench     ← 内核
        │
        ├── 1. 打印端口：输入/输出/FPGA输出/错误检查端口 + 错误计数器 nb_error
        ├── 2. 实例化 reference benchmark（REF_DUT）+ preconfigured fabric（FPGA_DUT）
        ├── 3. 生成时钟 + 随机输入激励（两侧共享输入）
        ├── 4. 【自检】逐拍比对 FPGA_DUT 与 REF_DUT 输出，不匹配则 nb_error++
        └── 5. 末尾判定 nb_error==0 → Succeed / 否则 Failed
```

#### 4.3.3 源码精读

**命令注册**：

[openfpga/src/base/openfpga_verilog_command_template.h:535-567](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/base/openfpga_verilog_command_template.h#L535-L567) 定义了 `write_preconfigured_testbench` 的选项。注意它没有 `--bitstream`（因为不编程）、没有 `--embed_bitstream`（内嵌是 wrapper 的事），但有 `--reference_benchmark_file_path`（给了才自检）。

**编排层**：

[openfpga/src/fpga_verilog/verilog_api.cpp:409-416](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_api.cpp#L409-L416) 拼出 `<设计名>_formal_random_top_tb.v`（后缀见 [verilog_constants.h:27-28](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_constants.h#L27-L28)），调用 `print_verilog_random_top_testbench`。文件名里的「formal_random」点明它面向形式验证、用随机向量。

**内核：端口与错误计数器**：

[openfpga/src/fpga_verilog/verilog_formal_random_top_testbench.cpp:54-105](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_formal_random_top_testbench.cpp#L54-L105) 打印模块声明、默认时钟端口（benchmark 没时钟时补一个用于同步）、共享输入/输出端口，并在 `!options.no_self_checking()` 时声明 `integer nb_error = 0;`。文件顶部 [verilog_formal_random_top_testbench.cpp:35-42](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_formal_random_top_testbench.cpp#L35-L42) 定义了固定的实例名 `REF_DUT`（参考）、`FPGA_DUT`（fabric）与 `nb_error`（错误计数器）。

**自检比对逻辑（与 full testbench 共用的公共函数）**：

[openfpga/src/fpga_verilog/verilog_testbench_utils.cpp:654-776](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L654-L776) `print_verilog_testbench_check` 是 full 与 preconfigured 共用的自检生成器。其逻辑（[L711-L738](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L711-L738)）：在每个时钟下降沿，若 `!(fpga_out === bench_out) && !(bench_out === 1'bx)`，就把该端口的 check_flag 置 1；随后（[L759-L768](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L759-L768)）在 check_flag 上升沿 `nb_error = nb_error + 1` 并 `$display("Mismatch on ...")`。注意 `===`（全等，含 x/z 比较）与「跳过 bench 为 x 的拍」这两个细节，避免了上电未稳定期的误报。

**末尾判定（Succeed/Failed，同样公共）**：

[openfpga/src/fpga_verilog/verilog_testbench_utils.cpp:609-615](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L609-L615) 在仿真到达设定的 `simulation_time` 后：`if (nb_error == 0)` 打印 `Simulation Succeed`，否则 `Simulation Failed with N error(s)`。关闭自检时（[L616-L619](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L616-L619)）则恒打印 `Simulation Succeed`——因为没有计数器可判。

#### 4.3.4 代码实践

**实践目标**：比较 preconfigured testbench 与 full testbench 在同一设计下的仿真时间。

**操作步骤**：

1. 打开范例脚本 [iverilog_example_script.openfpga:55-57](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/iverilog_example_script.openfpga#L55-L57)，三条命令联用：
   ```
   write_full_testbench --file ./SRC --reference_benchmark_file_path ${REFERENCE_VERILOG_TESTBENCH} --include_signal_init --bitstream fabric_bitstream.bit
   write_preconfigured_fabric_wrapper --embed_bitstream iverilog --file ./SRC
   write_preconfigured_testbench --file ./SRC --reference_benchmark_file_path ${REFERENCE_VERILOG_TESTBENCH}
   ```
2. 跑通后，分别用 iverilog 编译并仿真两份 testbench（编译命令可参考 `openfpga_flow` 脚本生成的 `include netlist` 文件）。

**需要观察的现象**：

- full testbench 仿真要先经历漫长的「移位编程」阶段，VCD 波形前半段都是 prog_clock 在翻转、config_done 迟迟不变；
- preconfigured testbench 一开始 config 就是就绪状态，直接进入随机激励与比对。

**预期结果**：preconfigured 的仿真墙钟时间显著短于 full（通常快一个数量级以上，取决于比特流大小）；但 full 的 VCD 里能看到完整的编程过程，覆盖范围更广。

> 待本地验证：精确的仿真耗时比值依赖具体设计与机器；若本地无 iverilog，可改为「阅读型实践」——打开两份生成的 testbench，对比 full 里的移位循环长度与 preconfigured 里的随机激励拍数，定性地解释时间差。

#### 4.3.5 小练习与答案

**练习 1**：preconfigured testbench 自检用的是 `===` 而非 `==`，为什么？

> **参考答案**：`===` 是全等比较，对 `x`（未知）和 `z`（高阻）敏感：只有两边完全相同（含都是 x）才为真。上电初期 reference 输出可能为 `x`，用 `===` 配合 `!(bench_out === 1'bx)` 可以把这些「尚未稳定」的拍跳过，避免误报 mismatch。

**练习 2**：如果只跑 `write_preconfigured_testbench` 而不跑 `write_preconfigured_fabric_wrapper`，仿真能成功吗？

> **参考答案**：不能正常验证。preconfigured testbench 实例化的 FPGA_DUT 是 4.2 wrapper 生成的预配置模块；缺了 wrapper，testbench 引用的模块不存在（或引用了未预配置的裸 fpga_top，配置位全 x），自检必然大量报 mismatch。这两条命令是配套的。

---

### 4.4 testbench 选项与自检机制（VerilogTestbenchOption）

#### 4.4.1 概念说明

前三节出现了大量选项（`--reference_benchmark_file_path`、`--embed_bitstream`、`--fast_configuration`、`--no_self_checking`……）。OpenFPGA 把它们抽象成一个统一的数据模型 `VerilogTestbenchOption`——它是「命令解析」与「Verilog 生成逻辑」之间的解耦层，和 u8-l1 讲的 `FabricVerilogOption` 是同一设计套路。

这个模型有两个值得深挖的设计：

1. **`reference_benchmark_file_path` 的链式效应**：设置它会自动联动 `print_top_testbench`、`print_preconfig_top_testbench` 两个开关——因为「没有 reference 就没法自检」。
2. **默认值即约定**：构造函数里写死了一组默认值（默认 DUT 是 `fpga_top`、默认内嵌风格是 modelsim、默认仿真器是 iverilog……），这些默认值决定了「不给选项时 testbench 长什么样」。

此外，自检逻辑（`print_verilog_testbench_check`、`print_verilog_timeout_and_vcd`）是 full 与 preconfigured **共用**的公共代码，本节把它们作为一个整体来讲。

#### 4.4.2 核心流程

```
命令行选项 (Command / CommandContext)
        │
        ▼  write_*_template 里逐项 set
VerilogTestbenchOption（数据模型）
        │  ├── 普通字段：output_directory / dut_module / ...
        │  ├── 链式字段：reference_benchmark_file_path
        │  │     └─ 设置时重算 print_top_testbench / print_preconfig_top_testbench
        │  └── 派生查询：no_self_checking() = reference 为空？
        ▼
内核函数读取 options.xxx() 决定生成内容
        │  ├── options.no_self_checking()  → 是否写比对代码
        │  ├── options.embedded_bitstream_hdl_type() → force / deposit
        │  └── options.fast_configuration() → 是否快速配置
```

#### 4.4.3 源码精读

**默认值**：

[openfpga/src/fpga_verilog/verilog_testbench_options.cpp:16-39](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L16-L39) 构造函数写死了默认：`top_module_ = "top_tb"`、`dut_module_ = "fpga_top"`、`default_net_type_ = ...NONE`、`embedded_bitstream_hdl_type_ = EMBEDDED_BITSTREAM_HDL_MODELSIM`、`simulator_type_ = IVERILOG`、`time_unit_ = 1E-3`。这解释了为什么不给 `--embed_bitstream` 时 wrapper 默认用 `$deposit`。

**`no_self_checking()` 的定义**：

[openfpga/src/fpga_verilog/verilog_testbench_options.cpp:94-96](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L94-L96) 直接返回 `reference_benchmark_file_path_.empty()`——自检与否完全由 reference 路径是否为空决定，是个派生查询而非独立开关（所以 `--no_self_checking` 命令选项的本质也是清空 reference）。

**`reference_benchmark_file_path` 的链式效应**：

[openfpga/src/fpga_verilog/verilog_testbench_options.cpp:162-170](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L162-L170) 设置 reference 后，立刻重新调用 `set_print_preconfig_top_testbench(...)` 和 `set_print_top_testbench(...)`——因为这两个开关的真值要在「reference 非空」的前提下才成立（见下条）。

[openfpga/src/fpga_verilog/verilog_testbench_options.cpp:181-195](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L181-L195) `set_print_preconfig_top_testbench` 内部：`print_preconfig_top_testbench_ = enabled && (!reference_...empty())`，并且一旦它为真，就**强制把 `print_formal_verification_top_netlist_` 也置真**（并打一条警告）——因为 preconfig testbench 依赖 formal verification wrapper 模块，二者必须同生。

**访问器全貌**：

[openfpga/src/fpga_verilog/verilog_testbench_options.h:48-71](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.h#L48-L71) 列出全部 const 访问器，内核正是通过它们读取配置。注意 `dut_module()` 只允许是 `fpga_top` 或 `fpga_core`（见 [verilog_testbench_options.cpp:144-155](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L144-L155) 的 `set_dut_module` 校验），这衔接 u6-l5 讲的 fpga_core wrapper 机制。

**公共自检与末尾判定**（4.1、4.3 已引用，此处作为整体小结）：`print_verilog_testbench_check`（[verilog_testbench_utils.cpp:654](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L654)）负责逐拍比对与计数，`print_verilog_timeout_and_vcd`（[verilog_testbench_utils.cpp:568](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L568)）负责 `$dumpfile`/`$dumpvars`、超时与 `Simulation Succeed/Failed`。两者都被 full 与 preconfigured 复用，体现了「公共逻辑下沉到 utils」的模块化思想。

#### 4.4.4 代码实践

**实践目标**：通过修改一个选项，观察它如何经 `VerilogTestbenchOption` 影响最终生成的 testbench。

**操作步骤**：

1. 复制 4.1 的任务配置，改其 `openfpga_shell_script`，在 `write_full_testbench` 行去掉 `--reference_benchmark_file_path ${REFERENCE_VERILOG_TESTBENCH}`，重跑。
2. 打开新生成的 `<设计名>_autocheck_top_tb.v`，搜索 `nb_error`、`Mismatch`、`Simulation Succeed`。

**需要观察的现象**：

- 没有 reference 时，testbench 里**找不到** `nb_error` 计数器与 `Mismatch` 比对块；
- 末尾的 `$display` 恒为 `Simulation Succeed`（无 if/else 分支）。

**预期结果**：与源码完全吻合——`no_self_checking()` 为真时，内核跳过 `print_verilog_testbench_check`（[verilog_top_testbench.cpp:2800](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2800)），`print_verilog_timeout_and_vcd` 走 `no_self_checking` 分支恒打印 Succeed（[verilog_testbench_utils.cpp:616-619](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L616-L619)）。

> 待本地验证：若不便改 task.conf 重跑，可直接静态对照——把 4.1 生成的带自检 testbench 与本实践去掉 reference 后应得到的版本在脑中 diff，定位差异段落对应的正是 `print_verilog_testbench_check`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `set_print_preconfig_top_testbench(true)` 会强制开启 `print_formal_verification_top_netlist`？

> **参考答案**：preconfigured testbench 实例化的 FPGA_DUT 就是 formal verification wrapper（4.2 的产物，模块名带 `_top_formal_verification` 后缀）。没有这个 wrapper 模块，testbench 引用的模块就不存在。所以二者强绑定，选项层用「设一个自动带出另一个」保证一致性，并打警告提示用户。

**练习 2**：`dut_module` 能设成 `fpga_top` 之外的任意名字吗？

> **参考答案**：不能。`set_dut_module`（[verilog_testbench_options.cpp:144-155](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_options.cpp#L144-L155)）只接受 `fpga_top` 或 `fpga_core`，其它名字直接 `exit(1)`。这保证 DUT 一定是 OpenFPGA 构建出的合法顶层（u6-l5 的 fpga_core wrapper）。

---

## 5. 综合实践

**综合任务**：为同一个设计（如 `and2`）分别生成 full 与 preconfigured 两套验证产物，实际仿真并撰写一份「快/慢 + 覆盖范围」对比报告。

**建议步骤**：

1. 准备环境：按 u1-l3 编译 `openfpga`，`source openfpga.sh`。
2. 选一个含三命令的范例脚本作为模板——[iverilog_example_script.openfpga:55-57](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga_flow/openfpga_shell_scripts/iverilog_example_script.openfpga#L55-L57) 已把三条命令写全。
3. 跑一个 full_testbench 任务（如 `run-task basic_tests/full_testbench/configuration_chain`），收集：
   - `SRC/<设计名>_autocheck_top_tb.v`（full）；
   - `SRC/<设计名>_top_formal_verification.v`（wrapper）；
   - `SRC/<设计名>_formal_random_top_tb.v`（preconfigured）；
   - `fabric_bitstream.bit`（比特流，用于估算 full 的移位周期数）。
4. 用 iverilog 分别编译并仿真 full 与 preconfigured 两份 testbench（参考任务目录下生成的 include netlist 文件来组织编译命令）。
5. 记录并对比：
   - **仿真墙钟时间**：full vs preconfigured；
   - **VCD 波形**：full 是否能看到完整编程阶段（prog_clock 长时间翻转后 config_done 才拉高）？preconfigured 是否一开始就进入工作？
   - **覆盖范围**：full 验证了哪些配置协议电路？preconfigured 跳过了哪些？
   - **自检结论**：两者末尾都应打印 `Simulation Succeed`（设计正确时）。
6. 把结论整理成一张表（维度：仿真时间、是否验证编程电路、是否需要比特流文件、典型用途）。

**预期结论（待本地验证具体数值）**：

| 维度 | full testbench | preconfigured（wrapper + testbench） |
| --- | --- | --- |
| 仿真时间 | 慢（含完整移位编程） | 快（无编程阶段） |
| 验证配置协议电路 | 是 | 否（force/deposit 绕过） |
| 需要 `--bitstream` 文件 | 是 | 否（从内存 `bitstream_manager` 读） |
| 典型用途 | 发布前完整验证、流片前回归 | 日常快速回归、形式验证 |

> 若本地无 iverilog：降级为「源码阅读 + 静态对比」——打开两份生成的 testbench，用本讲授的源码知识解释它们的结构差异，并基于 `fabric_bitstream.bit` 的行数估算 full 的编程周期数，给出定性的时间对比。

## 6. 本讲小结

- OpenFPGA 用**两套策略**验证「fabric + 比特流」是否等价于原始设计：full（完整、慢、验证配置电路）与 preconfigured（快速、跳过编程、不验证配置电路）。
- **`write_full_testbench`** 生成自包含 autocheck testbench，包含**编程阶段 + 工作阶段**两段；编程时钟被 `config_done` 门控（[verilog_top_testbench.cpp:1294-1334](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L1294-L1334)）；比特流**从文件读**并按配置协议移位。
- **`write_preconfigured_fabric_wrapper`** 生成一个端口对齐 benchmark 的包装模块，用 `force`（iverilog）或 `$deposit`（modelsim）把内存里 `bitstream_manager` 的值**内嵌**到配置端口，从而跳过编程（[verilog_preconfig_top_module.cpp:303-339](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_preconfig_top_module.cpp#L303-L339)）。
- **`write_preconfigured_testbench`** 驱动上述 wrapper，用随机向量并排比对 FPGA 与 reference，自检靠 `nb_error` 计数器与 `===` 全等比较（[verilog_testbench_utils.cpp:654-776](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_testbench_utils.cpp#L654-L776)）。
- **`VerilogTestbenchOption`** 是命令与生成逻辑的解耦数据模型；`reference_benchmark_file_path` 有链式效应（控制自检开关、强制带出 formal verification wrapper），`no_self_checking()` 是其派生查询。
- 三条命令都依赖 `build_fabric`，且选项层、模板层、编排层、内核层四分工与 u8-l1 完全一致；wrapper 与 preconfigured testbench 必须配套使用。

## 7. 下一步学习建议

- **进入 u8-l3（FPGA-SPICE）**：本讲的 testbench 是「数字功能仿真」；下一讲转向晶体管级 SPICE 网表，理解 `technology_library` 的器件模型如何被展开成子电路，以及功耗/时序仿真与 Verilog 仿真在数据来源上的异同。
- **回顾 u7（比特流）**：本讲反复用到「full 从 fabric 比特流文件读、wrapper 从 device 级 `bitstream_manager` 读」——若对这两级比特流模型还不清晰，建议重读 u7-l1（两级模型）与 u7-l4（`write_fabric_bitstream` 输出格式）。
- **延伸阅读**：想了解 `write_full_testbench` 编程阶段对不同配置协议（scan_chain/memory_bank/frame_based）的激励差异，可顺着 [verilog_top_testbench.cpp:2707](https://github.com/lnis-uofu/OpenFPGA/blob/a1e51333d2dd85cc4a640b6e384f0c9f5d97aa4a/openfpga/src/fpga_verilog/verilog_top_testbench.cpp#L2707) 的 `print_verilog_top_testbench_configuration_protocol_stimulus` 进入 `verilog_top_testbench_memory_bank.*` 等分派文件，这与 u9-l1（存储器组/移位寄存器 bank）紧密相关。
- **动手扩展**：仿照 4.4 的实践，尝试用 `--embed_bitstream` 的三种取值（iverilog/modelsim/none）生成 wrapper 并对比，加深对「内嵌风格」选项的理解。
