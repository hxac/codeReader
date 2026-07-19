# GHDL/GTKWave 工作流深度实践

## 1. 本讲目标

本讲把单元 3 学到的 SAL（模拟器抽象层）知识，落到一条**真实可跑通**的链路上：用开源仿真器 GHDL 完成编译、回归与交互调试。读完本讲，你应当能够：

- 说清楚 GHDL 模式与 Modelsim 模式在**运行环境**上的本质差异，并能用独立 `tclsh` 跑通一个 GHDL 回归。
- 解释 GHDL 为什么对 VHDL-2002 文件做「先 02 再 08」的**双版本编译**，以及库产物为什么落入 `v93`/`v08` **子目录**。
- 掌握 `.ghw` 波形文件 + GTKWave 的**迭代调试**工作流，并理解为什么 PsiSim 用 `.ghw` 而非 `.vcd`。

本讲依赖 u3-l3（编译抽象 `sal_compile_file`）与 u3-l5（交互调试 `launch_tb`），是单元 4 的实战篇。

## 2. 前置知识

在进入本讲前，你需要已经掌握以下概念（前序讲义已建立）：

- **PsiSim 两文件工作流**：`config.tcl`（纯声明，登记库/源/测试运行）+ `run.tcl`（加载框架→`init`→`source config.tcl`→编译→运行→检查）。
- **SAL dispatch 模式**：每个 `sal_*` proc 开头 `variable Simulator`，用 `if/elseif` 按字符串值分派到 Modelsim/GHDL/Vivado 三套实现（见 u3-l1）。
- **`init` 是分水岭**：它选仿真器并把状态变量清零，必须是第一个 PsiSim 命令。
- **`launch_tb` 的 GHDL 路径**：与 Modelsim 不同，GHDL 批处理无法「停在设计上等人交互」，所以 GHDL 调试是把仿真跑完、落盘波形、再用 GTKWave 打开（见 u3-l5）。

几个本讲会用到的术语：

- **GHDL**：开源 VHDL 仿真器，以命令行工具形式提供（`ghdl -a` 分析、`ghdl --elab-run` 精化并运行），没有自带 GUI 或自带 TCL shell。
- **GTKWave**：开源波形查看器，常与 GHDL 搭配。
- **`.ghw`**：GHDL Waveform，GHDL 原生波形格式；**`.vcd`**：Value Change Dump，通用的、与语言无关的波形格式。
- **synopsys ieee 库**：即 `std_logic_arith`、`std_logic_unsigned` 等 Synopsys 公司早年定义、后被广泛使用的非标准 IEEE 扩展包；GHDL 用 `--ieee=synopsys` 开关启用，以贴近 Modelsim 的默认行为。

## 3. 本讲源码地图

本讲涉及的关键文件与各自作用：

| 文件 | 作用 |
| --- | --- |
| `PsiSim.tcl` | 唯一源码，全部实现都在 `namespace eval psi::sim` 内。本讲聚焦其中的 GHDL 分支：`init`、`sal_init_simulator`、`sal_print_log`、`sal_compile_file`、`sal_clean_lib`、`sal_run_tb`、`launch_tb`、`sal_open_wave`。 |
| `CommandRef.md` | 命令文档。重点读 `init` 的 `-ghdl` 参数说明与 `launch_tb` 末尾的 **GHDL/GTK Workflow** 段；注意其中 `-wave`/`-show` 仍写 `.vcd`，是过时文档。 |
| `Changelog.md` | 版本历史。GHDL 相关条目集中在 1.4.0（首次支持 GHDL）、2.4.0（launch_tb/GTK 支持、库目录化）、2.5.0（2002 双编译 work-around、库产物子目录）。 |
| `README.md` | 含 Modelsim 版 `run.tcl` 示例，本讲用它对照 GHDL 版的差异。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先解决「怎么把脚本跑起来」（运行环境），再解决「编译时发生了什么」（双版本编译与子目录产物），最后解决「怎么调试」（GTKWave 迭代工作流）。

### 4.1 GHDL 运行环境与 init -ghdl

#### 4.1.1 概念说明

Modelsim 自带一个 TCL 解释器（Modelsim 控制台），所以 PsiSim 的所有命令天然在那个解释器里执行，还能直接调用 `vcom`/`vsim` 等内建命令。GHDL 则完全不同：它只是一组**操作系统级的命令行可执行文件**（`ghdl`、`gtkwave`），本身没有 TCL 解释器。

因此 PsiSim 在 GHDL 模式下有一个硬性环境要求：

- 必须用一个**独立的 TCL 解释器**（standalone TCL shell，文档举例 Active TCL）来 `source` 脚本，即用 `tclsh run.tcl` 运行，而不是在 Modelsim 控制台里跑。
- `ghdl` 与 `gtkwave` 必须在系统 `PATH` 中可被找到，因为 PsiSim 通过 TCL 的 `exec` 直接调用它们。

这条要求从 GHDL 首次被引入时就写明了（见 Changelog 1.4.0 与 CommandRef 的 `init` 说明），是 GHDL 模式与 Modelsim 模式最根本的运行差异。

#### 4.1.2 核心流程

一次 GHDL 回归的执行序列（与 u1-l3 的「黄金七步」完全同构，只是 `init` 带了 `-ghdl`）：

```text
tclsh run.tcl
  └─ source PsiSim.tcl          # 加载框架（定义 psi::sim 命名空间）
  └─ namespace import psi::sim::*
  └─ init -ghdl                 # 选 GHDL，状态清零（关键差异点）
       └─ sal_init_simulator    # GHDL 分支：不探测版本，只写占位串
       └─ clean_transcript      # 清空 ./Transcript.transcript
  └─ source config.tcl          # 纯声明，与 Modelsim 版完全一致
  └─ compile_files -all -clean  # 走 sal_compile_file 的 GHDL 分支
  └─ run_tb -all                # 走 sal_run_tb 的 GHDL 分支
  └─ run_check_errors "###ERROR###"
```

注意：从 `source config.tcl` 往下的所有命令，**对 GHDL 和 Modelsim 是同一份脚本**，差异只在 `init` 这一行被 SAL 吸收掉了。这正是 SAL 抽象的回报。

#### 4.1.3 源码精读

`init` 解析 `-ghdl` 开关并把 `Simulator` 设为 `"GHDL"`：

[PsiSim.tcl:351-378](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L351-L378) —— `init` 的全部实现。其中 L358-359 是 `-ghdl` 分支：`variable Simulator "GHDL"`。默认值在 L354 设为 `"Modelsim"`，后出现的开关覆盖前者。重置状态变量在 L368-373，随后 L375 调用 `sal_init_simulator`、L377 调用 `clean_transcript`。

进入 GHDL 分支后，`sal_init_simulator` 并不像 Modelsim 那样去探测版本号：

[PsiSim.tcl:140-141](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L140-L141) —— GHDL 分支只把 `SimulatorVersion` 设为占位串 `"NotImplementedForGhdl"`。对比 L123-139 的 Modelsim 分支（用「文件中转」从 `vcom -version` 抠版本号），GHDL 不做这件事，因为后续 `sal_version_specific_flags` 的版本相关 flag（`-novopt`）只对 Modelsim 有意义。

GHDL 模式下的日志输出也与 Modelsim 不同。`sal_print_log` 是 SAL 里少数把 GHDL 与 Vivado 合并处理的 proc：

[PsiSim.tcl:31-46](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L31-L46) —— Modelsim 用内建 `echo`（L35，写进 Modelsim 自己的 transcript）；GHDL/Vivado 分支（L36-42）做「控制台 + 文件」双写：先 `puts $text` 到控制台，再 `open $TranscriptFile a` 追加写文件。**因为 GHDL 没有自带 transcript，PsiSim 必须自己手动维护 `./Transcript.transcript`**，否则 `run_check_errors` 将无日志可读。

文档侧，CommandRef 明确写了 GHDL 的独立解释器要求：

[CommandRef.md:54-57](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L54-L57) —— `-ghdl` 参数说明：`For GHDL, a standalone TCL shell (e.g. Active TCL) must be used`。

这条要求的历史出处：

[Changelog.md:71-74](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L71-L74) —— 1.4.0 首次支持 GHDL：`the GHDL directory must be added to the system path and the TCL scripts must be evaluated by a standalone TCL interpreter (e.g. active TCL)`。

#### 4.1.4 代码实践

**实践目标**：对照 README 里的 Modelsim 版 `run.tcl`，写一份 GHDL 版 `run.tcl`，并指出两者的唯一区别。

**操作步骤**：

1. 阅读 README 中的示例执行脚本：
   [README.md:138-167](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/README.md#L138-L167) —— 这是 Modelsim 版 `run.tcl`，第 147 行是 `init`（无参）。
2. 复制该脚本，把第 147 行改成 `init -ghdl`，其余**一字不改**。示例代码（GHDL 版 run.tcl）如下：

   ```tcl
   # --- 示例代码：GHDL 版 run.tcl ---
   #Load dependencies
   source ../../../TCL/PsiSim/PsiSim.tcl
   namespace import psi::sim::*

   #Initialize Simulation （唯一与 Modelsim 版不同的行）
   init -ghdl

   #Configure
   source ./config.tcl

   #Run Simulation
   compile_files -all -clean
   run_tb -all
   run_check_errors "###ERROR###"
   ```

3. 在装有 GHDL 与独立 `tclsh` 的机器上，把 `ghdl`、`gtkwave` 加入 `PATH`，然后在项目目录执行 `tclsh run.tcl`（而非在 Modelsim 控制台执行）。

**需要观察的现象**：

- 控制台会先打印 `Initialize PsiSim`，随后每条 `sal_print_log` 的内容同时出现在控制台和 `./Transcript.transcript` 里（GHDL 双写）。
- 编译期会看到 `ghdl -a ...` 命令被 `sal_print_log`/`exec` 触发；运行期会看到 `ghdl --elab-run ...` 命令。
- 最后 `run_check_errors` 读取 `./Transcript.transcript`，打印 `SIMULATIONS COMPLETED SUCCESSFULLY` 或 `!!! ERRORS OCCURED IN SIMULATIONS !!!`。

**预期结果**：脚本文件内容上，GHDL 版与 Modelsim 版**唯一区别就是 `init` 多了 `-ghdl`**。运行环境上则不同：Modelsim 版在 Modelsim 控制台执行，GHDL 版用独立 `tclsh` 执行。

> 若你本地没有 GHDL/`tclsh` 环境，**待本地验证**上述输出。也可以仅做「源码阅读型实践」：在 PsiSim.tcl 里把 `init` 走一遍，确认 `Simulator` 被设为 `"GHDL"`，并跟踪一次 `puts` 如何同时落到控制台与 transcript 文件。

#### 4.1.5 小练习与答案

**练习 1**：如果把 Modelsim 版 `run.tcl` 直接拿到 GHDL 环境用 `tclsh` 跑（即忘了加 `-ghdl`），会发生什么？

**参考答案**：`init` 默认把 `Simulator` 设为 `"Modelsim"`，于是 `compile_files`/`run_tb` 会走 Modelsim 分支，去调用 `vcom`/`vsim`/`echo` 等 Modelsim 内建命令。而独立 `tclsh` 里并没有这些命令，脚本会在第一个 `sal_compile_file` 或 `sal_print_log` 处报「unknown command」错误。这正说明 `init -ghdl` 是切换 dispatch 的唯一开关。

**练习 2**：为什么 GHDL 模式下 `sal_print_log` 必须「控制台 + 文件」双写，而 Modelsim 不用？

**参考答案**：Modelsim 有自带的 transcript 机制，`echo` 写入的内容会被 Modelsim 自动记进 transcript 文件；而 GHDL 只是一组命令行工具，没有任何 transcript，PsiSim 若不手动 `open ... a` 追加写文件，`run_check_errors` 将无日志可判，回归判错链路就断了。

---

### 4.2 双版本编译与库子目录产物

#### 4.2.1 概念说明

GHDL 用 `ghdl -a`（analyze）做编译。它有一条关键限制：**同一个库内不允许混用不同 VHDL 标准的编译产物**。也就是说，如果库里有文件按 `--std=02` 编译、另一些按 `--std=08` 编译，精化（elaborate）时会因标准不一致而出错。

PsiSim 的测试台大多假设 VHDL-2008，因此仿真必须永远从 2008 启动。但又想**保证那些声明为 2002 的文件确实没有偷用 2008 语法**。于是 PsiSim 采取了一个 work-around：

- 对声明为 `2002` 的文件，**先用 `--std=02` 编译一遍**（如果它偷偷用了 2008 特性，这一遍就会报错），**再用 `--std=08` 编译一遍**。
- 所有文件最终都进入 **2008** 那份库，仿真从 2008 精化启动。
- 两套不同标准的产物**物理隔离到子目录**，避免 GHDL「禁止混标准」的限制。

这条机制对应 Changelog 2.5.0 的两条修复：「`GHDL: work-around for language version 2002`」与「`GHDL: install library products into subdirs`」。

#### 4.2.2 核心流程

设库名为 `lib`、某文件声明版本为 `2002`，`sal_compile_file` 的 GHDL 分支执行顺序：

```text
若 langVersion == 2002：
   1. file mkdir lib/v93
   2. exec ghdl -a --ieee=synopsys --std=02 -fexplicit -frelaxed-rules
                 -Wno-shared -Wno-hide --workdir=lib/v93 --work=lib -P.  $path
（若 langVersion 既非 2002 也非 2008：打印错误，跳过）
（总是）
   3. file mkdir lib/v08
   4. exec ghdl -a --ieee=synopsys --std=08 -frelaxed-rules
                 -Wno-shared -Wno-hide --workdir=lib/v08 --work=lib -P.  $path
```

也就是说：

- **2002 文件**编译 2 次（v93 一次、v08 一次）；
- **2008 文件**（默认）只编译 1 次（仅 v08）；
- 仿真阶段 `sal_run_tb` 永远用 `--workdir=lib/v08`，从 2008 那份库精化运行。

库目录布局最终长这样：

```text
lib/
 ├── v93/   ← 仅当存在 2002 文件时出现，存放 --std=02 产物（用于查 2008 误用）
 └── v08/   ← 存放 --std=08 产物，仿真真正消费这一份
```

> 关于子目录名：代码用 `v93` 作为「非 2008 标准桶」的标签，用 `v08` 作为「2008 桶」的标签。源码未注释为何 2002 编译产物落在名为 `v93` 的目录（而不是 `v02`），这属于框架的命名约定；本讲对此不展开推测，仅如实描述代码行为。

`--ieee=synopsys` 这个开关贯穿两次编译，它让 GHDL 使用 Synopsys 版的 IEEE 扩展库（`std_logic_arith` 等），从而与 Modelsim 的默认行为对齐——这就是本讲主题里提到的「synopsys ieee 库的使用」。

清理时，GHDL 分支直接删掉整个库目录（含 `v93`/`v08`）：

```text
sal_clean_lib（GHDL）： file delete -force $lib
```

#### 4.2.3 源码精读

`sal_compile_file` 的 GHDL 分支是本模块的核心：

[PsiSim.tcl:175-190](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L175-L190) —— GHDL 编译分支。其中：

- L177-181 是 2002 的「先 02」编译：`file mkdir $lib/v93` 后 `exec ghdl -a --ieee=synopsys --std=02 -fexplicit -frelaxed-rules -Wno-shared -Wno-hide --workdir=$lib/v93 --work=$lib -P. $path`。
- L178-179 的注释直接点明了动机：`compile for 2002 (to make sure no 2008 features are used) but compile again for 2008 since we assume most testbenches will use that and ghdl does not support mixing versions`。
- L182-184：若版本既不是 2002 也不是 2008，打印错误 `VHDL Version $langVersion not supported for GHDL`。
- L185-186 是「总是再 08」编译：`file mkdir $lib/v08` 后 `exec ghdl -a --ieee=synopsys --std=08 -frelaxed-rules -Wno-shared -Wno-hide --workdir=$lib/v08 --work=$lib -P. $path`。
- L187-190：Verilog 在 GHDL 下不支持，打印错误。

注意 2002 那次编译多了 `-fexplicit`，2008 那次没有；两次都带 `--ieee=synopsys`、`-frelaxed-rules`、`-Wno-shared`、`-Wno-hide`，这些是为了让 GHDL 更「宽松」、更贴近 Modelsim 行为（对应 Changelog 2.3.0 的 `Made GHDL more permissive to better match Modelsim behavior`）。

仿真阶段消费的正是 `v08` 子目录：

[PsiSim.tcl:254](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L254) —— `sal_run_tb` 的 GHDL 分支里，精化运行命令 `ghdl --elab-run ... --workdir=$lib/v08 --work=$lib ...`，明确从 `v08` 取产物。这也解释了为什么所有文件最终都必须有一份 2008 编译：仿真只认 `v08`。

清理逻辑：

[PsiSim.tcl:149-160](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L149-L160) —— `sal_clean_lib`。Modelsim 走 `vlib`/`vdel`/`vlib` 三连（L151-154），GHDL/Vivado 合并分支（L155-156）直接 `file delete -force $lib`，把整个库目录连同 `v93`/`v08` 一起删掉。

历史依据：

[Changelog.md:1-8](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L1-L8) —— 2.5.0 的两条 GHDL 修复（L6-7）：`GHDL: work-around for language version 2002` 与 `GHDL: install library products into subdirs`，分别对应本模块的「双编译」与「子目录产物」。

[Changelog.md:10-20](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L10-L20) —— 2.4.0 的 `Changed compile settings for GHDL to place data in library name named folder`（L18），是「库目录化」的更早一步；2.5.0 进一步细化为 `v93`/`v08` 子目录。

#### 4.2.4 代码实践

**实践目标**：给定一个声明为 2002 的文件，跟踪它在 GHDL 下被编译几次、产物落到哪里。

**操作步骤**：

1. 准备一个最小 `config.tcl`，把某文件显式标为 2002（`-version 2002`）：

   ```tcl
   # --- 示例代码：config.tcl 片段 ---
   add_library mylib
   add_sources "../hdl" "foo.vhd" -tag src -version 2002
   create_tb_run "foo_tb"
   add_tb_run
   ```

2. 在 `PsiSim.tcl` 的 L181 与 L186 各加一行临时日志（**仅用于本实践观察，用完应还原，不要提交**），例如在 L181 后插入 `puts ">>> compiled 2002 into $lib/v93"`、在 L186 后插入 `puts ">>> compiled 2008 into $lib/v08"`。
3. 用 `tclsh run.tcl`（`init -ghdl`）跑 `compile_files -all`。

**需要观察的现象**：

- 对这一个 2002 文件，控制台应先后出现「compiled 2002 into mylib/v93」与「compiled 2008 into mylib/v08」两条日志，即编译 2 次。
- 磁盘上出现 `mylib/v93/` 与 `mylib/v08/` 两个子目录，里面各有 GHDL 的 `cf`/对象文件。
- 若把 `foo.vhd` 偷偷改成用了 VHDL-2008 独有语法（如条件赋值中的 `??`、外部名等），`--std=02` 那次编译（v93）应报错，从而在 v08 编译之前就拦下「2002 文件误用 2008」。

**预期结果**：2002 文件被双编译，产物分落 `v93`/`v08`；2008 文件（默认）只进 `v08`；仿真从 `v08` 启动。

> 无 GHDL 环境时为「待本地验证」。也可做纯阅读实践：在 PsiSim.tcl:175-190 用铅笔跟踪一次 `langVersion=="2002"` 的控制流，确认 `file mkdir` 与 `exec ghdl -a` 各执行两次。

#### 4.2.5 小练习与答案

**练习 1**：如果把一个本应是 2008 的文件误登记成 `-version 2002`，会发生什么？会有什么副作用？

**参考答案**：它会先按 `--std=02` 编译进 `v93`，再按 `--std=08` 编译进 `v08`。由于它本就是 2008 文件、大概率不含 2008 独有语法问题，`--std=02` 那次也能过；副作用是白白多编译一次（`v93` 多一份无用的产物），仿真仍从 `v08` 正常启动。换句话说，「误标 2002」只是浪费一次编译，不会直接出错。

**练习 2**：为什么 `sal_run_tb` 固定用 `--workdir=$lib/v08`，而不是根据文件版本选择？

**参考答案**：因为 GHDL 禁止库内混标准，而 PsiSim 保证所有文件在 `v08` 都有一份 2008 编译产物（2002 文件也会被再编一次 08）。仿真需要一个「统一标准」的库来精化，`v08` 就是那个统一为 2008 的库，所以固定从 `v08` 启动。

**练习 3**：`compile_suppress 135,1236`（来自 README 示例）在 GHDL 模式下还有效吗？

**参考答案**：无效。`CompileSuppress` 只在 `sal_compile_file` 的 **Modelsim 分支**（L167 的 `-suppress $CompileSuppress`）被消费；GHDL 分支（L175-190）完全不读这个变量。命令本身不会报错（它只是往字符串里拼接编号），但 GHDL 不会真正抑制任何消息——这是「命令执行成功 ≠ 特性生效」的又一例。

---

### 4.3 GTKWave 迭代调试

#### 4.3.1 概念说明

GHDL 是批处理仿真器，不能像 Modelsim 那样「加载设计后停在时间 0 等你交互」。所以 PsiSim 的 `launch_tb` 在 GHDL 下走的是另一条路（见 u3-l5）：

1. 把测试台**跑到底**，把全部信号变化落盘成一个波形文件；
2. 可选地用 GTKWave 打开这个波形文件；
3. 靠**文件名稳定**实现迭代：改代码后重跑，覆盖同一个波形文件，再在 GTKWave 里 `Reload Waveform` 刷新，无需重开窗口。

这条工作流的关键支点是「同一个 `-argidx` 反复生成同一个文件名」，所以 GTKWave 的 reload 才有意义。

#### 4.3.2 核心流程

`launch_tb` 的 GHDL 分支（接口层）：

```text
对每个 run（命中第一个 -contains 即 return）：
   若 wave 被启用： set wave "<runName>_<argidx>.ghw"   ← 文件名由 run 名与 argidx 决定
   sal_run_tb(runLib, runName, argsToUse, timeLimit, RunSuppress, wave)
       └─ GHDL 分支： ... --wave=<wave> 拼到 ghdl --elab-run 末尾
   若 -show 启用： sal_open_wave $wave
       └─ exec gtkwave -f $wave &   ← 末尾 & 后台 fork，tclsh 不阻塞
   return （只处理第一个匹配）
```

迭代调试循环（对应 CommandRef 的 GHDL/GTK Workflow）：

```text
1. launch_tb -contains <tb> -wave -show      # 首次：跑仿真 + 打开 GTKWave
2. （在 GTKWave 里加信号、调显示）
3. 改 VHDL 代码
4. launch_tb -contains <tb> -wave            # 重跑：不带 -show，避免再开一个 GTKWave
5. GTKWave 菜单： File → Reload Waveform     # 刷新同一窗口
6. （可选）换 -argidx 对比不同 generics 的波形（会生成不同文件名，需另开窗口）
```

#### 4.3.3 源码精读

`launch_tb` 先在接口层做白名单检查，只放行 Modelsim 与 GHDL（Vivado 无调试路径）：

[PsiSim.tcl:856-859](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L856-L859) —— 若 `Simulator` 既不是 Modelsim 也不是 GHDL，打印 `launch_tb: this command is only implemented for Modelsim and GHDL` 并 `return`。

GHDL 分支确定波形文件名并复用 `sal_run_tb`：

[PsiSim.tcl:945-956](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L945-L956) —— GHDL 分支。关键在 L947-950：当 `wave` 被启用时，`set wave "$runName\_$argidx\.ghw"`，即文件名为 `<run名>_<argidx>.ghw`。这里用的是 **`.ghw`**（不是 `.vcd`）。L952 把这个 `wave` 作为第 6 个参数传给 `sal_run_tb`；L953-955 在 `-show` 启用时调 `sal_open_wave`。

`sal_run_tb` 把 `wave` 拼成 GHDL 的命令行开关：

[PsiSim.tcl:251-254](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L251-L254) —— L251-253：若 `wave != ""`，`set wave " --wave=$wave"`；L254 的 `ghdl --elab-run` 命令末尾拼接 `$wave`，于是最终命令带上 `--wave=<runName>_<argidx>.ghw`，GHDL 据此落盘波形。

`sal_open_wave` 用末尾 `&` 把 GTKWave 推到后台：

[PsiSim.tcl:335-342](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/PsiSim.tcl#L335-L342) —— GHDL 分支 `exec gtkwave -f $wave &`（L338）。末尾的 `&` 让 GTKWave 被 fork，tclsh 不被阻塞，这正是 CommandRef 所说「forked to keep tclsh interaction active」的来源，也是后续能在同一 tclsh 里重跑 `launch_tb` 的前提。

文档侧的迭代工作流（权威步骤）：

[CommandRef.md:537-542](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L537-L542) —— `GHDL/GTK Workflow` 四步：首次带 `-wave -show` 打开 GTKWave → 重跑不带 `-show` → GTKWave 里 `File → Reload Waveform` → 可选换 `-argidx` 对比。

**关于 `.ghw` vs `.vcd`（重要）**：

代码（PsiSim.tcl:948）落盘的是 `.ghw`，但 CommandRef 的参数表仍写 `.vcd`：

[CommandRef.md:528-529](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/CommandRef.md#L528-L529) —— `-wave` 参数说明写着 `a vcd file is generated with the naming pattern <tb_name><argidx|default>.vcd`；L533 的 `-show` 也写 `the generated vcd file`。**这是过时文档，与代码不一致，应以代码（`.ghw`）为准。**

那么为什么 GHDL 用 `.ghw` 而非 `.vcd`？技术原因在于两种格式对 VHDL 类型的表达能力：

- **`.vcd`（Value Change Dump）** 是一种与硬件描述语言无关的通用格式，源自 Verilog 生态。它只描述「信号在何时发生 0/1/x/z 翻转」，**不保留 VHDL 的类型结构**。对于 VHDL 的复合类型——尤其是 **record（记录类型）**、枚举、复合类型的数组——VCD 会把它们展平或丢失语义，GTKWave 里看到的可能只是一堆无名的比特。
- **`.ghw`（GHDL Waveform）** 是 GHDL 原生格式，**保留 VHDL 的类型层级**，record 的各个字段、枚举值的符号名都能被 GTKWave 正确还原。因此对于大量使用 record 的 PSI 测试台，`.ghw` 才能让调试真正可读。

> **关于 practice_task 提到的「Changelog 中 records 修复记录」**：经核查，当前 HEAD（2.5.0）的 `Changelog.md` 并**没有**一条明确写「records」的修复条目；与 GHDL 波形/调试相关的历史条目是 2.4.0 的 `GHDL support for launch_tb` 与 `GTK support for launch_tb`（见 [Changelog.md:13-14](https://github.com/paulscherrerinstitute/PsiSim/blob/434f6a9bd8c79c7d20522344996635516fc95392/Changelog.md#L13-L14)）。因此「用 `.ghw` 以保留 record 等复合类型」这一理由，**依据来自代码（PsiSim.tcl:948 用 `.ghw`）以及 GHDL 的已知特性，而非某条 Changelog 记录**。本讲如实说明，不编造不存在的 Changelog 条目。

#### 4.3.4 代码实践

**实践目标**：复述并验证 GHDL/GTKWave 的迭代调试步骤，理解文件名稳定如何支撑 reload。

**操作步骤**：

1. 准备一个含多组 generics 的测试运行（这样 `-argidx` 才有意义）：

   ```tcl
   # --- 示例代码：config.tcl 片段 ---
   create_tb_run "foo_tb"
   tb_run_add_arguments "-gClockRatio_g=3" "-gClockRatio_g=1.01"
   add_tb_run
   ```

2. 在 `tclsh` 里执行首次调试：`launch_tb -contains foo_tb -argidx 0 -wave -show`。
3. 在弹出的 GTKWave 窗口里，把感兴趣的信号（尤其含 record 类型的信号）拖进波形区，调整显示。
4. 故意改一行 VHDL，重新编译（`compile_files -all`），再在**同一个 tclsh** 里执行 `launch_tb -contains foo_tb -argidx 0 -wave`（**不带** `-show`）。
5. 在 GTKWave 菜单选 `File → Reload Waveform`。

**需要观察的现象**：

- 步骤 2 会在磁盘生成 `foo_tb_0.ghw`（文件名 = `runName_argidx.ghw`），GTKWave 打开它，record 字段应能正确展开显示。
- 步骤 4 不带 `-show`，故不会再弹新 GTKWave 窗口，但 `foo_tb_0.ghw` 被新数据覆盖（文件名相同）。
- 步骤 5 的 reload 让原窗口刷新为新波形，信号布局保持不变——这就是「迭代」的精髓。
- 若改用 `-argidx 1`，会生成 `foo_tb_1.ghw`（不同文件名），需另开窗口对比，不会覆盖 `foo_tb_0.ghw`。

**预期结果**：同一 `-argidx` 反复覆盖同一 `.ghw`，配合 GTKWave reload 实现无重开的迭代；不同 `-argidx` 生成不同文件用于对比。record 等复合类型在 `.ghw` 下可读，换 `.vcd` 则会丢失结构（可由 GTKWave 对比观察，**待本地验证**）。

> 无 GTKWave/GHDL 环境时，可做纯阅读实践：在 PsiSim.tcl:945-956 跟踪 `-argidx 0` 与 `-argidx 1` 两次调用，写出各自生成的 `.ghw` 文件名，验证「同名覆盖、异名并存」的结论。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `sal_open_wave` 的命令末尾要带 `&`？去掉会怎样？

**参考答案**：`&` 让 TCL 的 `exec` 把 GTKWave fork 到后台、立即返回，tclsh 得以继续接受输入。若去掉 `&`，`exec gtkwave -f $wave` 会一直阻塞，直到 GTKWave 窗口被关闭才返回——那样你就无法在同一个 tclsh 里重跑 `launch_tb`，迭代调试链路就断了。

**练习 2**：CommandRef 写的是 `.vcd`，代码用的是 `.ghw`，你以哪个为准？为什么会有这种不一致？

**参考答案**：以**代码（`.ghw`）为准**。文档（CommandRef.md:528/L533）是过时描述，未随实现更新。判断依据是直接读 PsiSim.tcl:948 的 `set wave "$runName\_$argidx\.ghw"`——运行时真正落盘的就是 `.ghw`。这也是「读源码胜过读文档」的一个实例。

**练习 3**：为什么迭代调试强调「重跑时不带 `-show`」？

**参考答案**：`-show` 会触发 `sal_open_wave` 再开一个 GTKWave 窗口（PsiSim.tcl:953-955）。迭代时我们想复用已经布置好信号布局的旧窗口，所以重跑只生成新 `.ghw`（`-wave`）、用 reload 刷新旧窗口，而不是每次都开新窗口。只有首次或换 `-argidx`（生成新文件名）时才需要 `-show`。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次「端到端」的 GHDL 调试小任务。

**任务背景**：你有一个使用 record 类型返回多通道结果的 VHDL 测试台 `multichannel_tb`，想用 GHDL + GTKWave 调试它，并对比两种 generics 配置下的输出。

**要求**：

1. 写一份 `config.tcl`，声明一个库 `dut_lib`，加入设计源（默认 2008）与测试台，并定义一个 `multichannel_tb` 的 run，用 `tb_run_add_arguments` 给出两组 generics（如 `-gChannels_g=2` 与 `-gChannels_g=4`）。
2. 写一份 GHDL 版 `run.tcl`（`init -ghdl`），先用 `compile_files -all -clean` + `run_tb -all` + `run_check_errors "###ERROR###"` 跑一次完整回归，确认无错。
3. 然后用 `launch_tb` 调试：先 `launch_tb -contains multichannel -argidx 0 -wave -show` 打开 GTKWave 观察 record 字段；再改一处代码、`compile_files -all`、`launch_tb -contains multichannel -argidx 0 -wave`（不带 `-show`），在 GTKWave 里 `Reload Waveform`。
4. 最后用一句话回答：在整个过程中，`config.tcl` 与 Modelsim 模式相比是否需要改动？`run.tcl` 呢？

**参考要点**：

- `config.tcl` **完全不需要改动**——库、源、测试运行、suppress 的声明都是仿真器无关的（suppress 在 GHDL 下虽不生效，但放着无害）。
- `run.tcl` 只需把 `init` 改为 `init -ghdl`，其余命令一字不改。
- record 字段之所以能在 GTKWave 里展开，是因为代码用了 `.ghw`（保留类型结构），而非 `.vcd`。
- 若本地无 GHDL/GTKWave，则把上述脚本写出来、把 PsiSim.tcl 对应分支的执行轨迹画成流程图作为交付，并标注「待本地验证」。

## 6. 本讲小结

- **GHDL 模式的运行环境**与 Modelsim 根本不同：必须用独立 `tclsh`（如 Active TCL）执行脚本，`ghdl`/`gtkwave` 需在 `PATH`；`init -ghdl` 是切换 dispatch 的唯一开关，`run.tcl` 与 Modelsim 版的唯一文件差异就在这一行。
- **GHDL 没有自带 transcript**，`sal_print_log` 用「控制台 + 文件」双写手动维护 `./Transcript.transcript`，供 `run_check_errors` 判错；`sal_init_simulator` 对 GHDL 不探测版本，只写占位串。
- **双版本编译**是 GHDL「禁止库内混标准」限制下的 work-around：2002 文件先 `--std=02` 进 `v93`（查 2008 误用），所有文件再 `--std=08` 进 `v08`，仿真固定从 `v08` 精化；产物分落子目录，对应 Changelog 2.5.0 的两条修复。
- `--ieee=synopsys` 让 GHDL 使用 Synopsys 版 IEEE 扩展库，以贴近 Modelsim 行为；`-frelaxed-rules`/`-Wno-*` 等开关进一步放宽差异。
- **GTKWave 迭代调试**靠「文件名 = `runName_argidx.ghw` 稳定可覆盖」+ GTKWave `Reload Waveform` 实现无重开刷新；`sal_open_wave` 末尾的 `&` 把 GTKWave fork 到后台，保住 tclsh 交互。
- 代码用 **`.ghw`**（GHDL 原生格式，保留 record 等复合类型），而 CommandRef 仍写 `.vcd` 属过时文档；practice_task 所指的「Changelog records 修复记录」在当前 HEAD 不存在，本讲据实说明、以代码为准。

## 7. 下一步学习建议

- 下一篇 **u4-l3 扩展新模拟器与架构取舍**：当你想加入一个全新仿真器（如 Icarus Verilog）时，需要改 `init` 的开关解析以及**每一个** `sal_*` proc 的 dispatch 分支——本讲对 GHDL 分支的逐行理解，正是做这件事的基础。
- 继续阅读源码：把本讲涉及的 `sal_compile_file`（L162-206）、`sal_run_tb`（L224-307）、`launch_tb`（L852-965）三处 GHDL 分支与对应 Modelsim/Vivado 分支并排对照，体会 SAL「同一意图、三种说法」的取舍。
- 动手实验：若有 GHDL 环境，尝试把 PsiSim.tcl:948 的 `.ghw` 临时改成 `.vcd`，重新调试一个含 record 的测试台，亲眼对比 GTKWave 里 record 字段的显示差异，从而验证「为什么用 `.ghw`」。
