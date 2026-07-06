# 综合流程：Design Compiler 脚本解读

## 1. 本讲目标

前面 U3、U4 我们已经把 32 点 FFT 的 RTL 逐模块读透，U5 也用 testbench 在行为级验证了功能正确。但 RTL 只是「图纸」，流片前必须把它变成「由真实晶体管构成的逻辑门网表」——这一步叫做**综合（synthesis）**。本讲带读者通读 `SYN/scripts/syn.tcl`，跑完整个综合流程。

学完后你应该能够：

- 说清**综合**与**仿真**的区别，理解 RTL 是怎样被映射到 UMC 130nm 工艺的标准单元的。
- 看懂 `syn.tcl` 里 `search_path / link_library / target_library` 三条库设置命令的含义，以及 ss/ff 两个工艺角的用途。
- 掌握 `analyze → elaborate → current_design → uniquify → link → compile` 这条综合主流程，每一步在做什么。
- 理解 `compile → optimize_registers → compile_ultra` **多轮优化**策略，为什么不是一次编译到底。
- 解释最终写出的 4 个产物（`.v` 网表 / `.sdc` / `.sdf` / `.ddc`）各自给谁用。

## 2. 前置知识

### 2.1 什么是综合

RTL（本项目的 Verilog）描述的是**行为**：`a + b`、`case`、`always` 触发器。综合工具的任务是把每个行为翻译成具体工艺库里**已有的标准单元（standard cell）**——比如一个 12 位加法变成一串 `ADDH`/`FA` 全加器单元，一个 `always @(posedge clk)` 变成一排 `D 触发器` 单元。输出是一份「门级网表（gate-level netlist）」，本质上还是 Verilog，但里面全是工艺库里真实存在的单元实例，可以直接交给后端工具做版图。

关键公式可以粗略理解为：综合把抽象行为算子映射为带面积 \(A\)、延迟 \(t\)、功耗 \(P\) 的具体单元：

\[
\text{RTL 行为} \xrightarrow{\text{综合}} \sum_{i} \text{StandardCell}_i \quad (\text{每个 cell 带 } A_i, t_i, P_i)
\]

综合的「好坏」由三条铁律权衡：**面积（Area）**、**时序（Timing，能否跑满 100 MHz）**、**功耗（Power）**。本项目目标是 10 ns 周期（100 MHz）、UMC 130nm 工艺。

### 2.2 Design Compiler（DC）与 dc_shell

Design Compiler 是 Synopsys 的综合工具，业界事实标准。它提供一个 TCL 解释器 `dc_shell`，用户写 `.tcl` 脚本驱动它。本项目的入口脚本 `run`（见 [SYN/run:1-3](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/run#L1-L3)）就是一句：

```tcl
dc_shell -f scripts/syn.tcl | tee log/syn.log
```

即在 `dc_shell` 里执行 `scripts/syn.tcl`，并把日志 tee 到 `log/syn.log`。本讲的主角就是 `syn.tcl` 这 42 行。

### 2.3 几个必须先懂的术语

| 术语 | 含义 |
|------|------|
| 标准单元库（.db） | 工艺厂提供的二进制 Liberty 库，描述每个逻辑门的功能/延迟/面积/功耗 |
| `target_library` | DC **映射目标**——RTL 最终被翻译成这里面的单元 |
| `link_library` | DC **可引用**的全部单元（含 target + 宏），`*` 表示先在设计自身里找 |
| 工艺角（corner） | 同一工艺在不同电压/温度下的工作点，如 ss（慢/最差）、ff（快/最佳） |
| 网表（netlist） | 综合后的门级 Verilog，全是标准单元实例 |
| SDC | Synopsys 设计约束文件，描述时钟与时序要求，供后端 P&R 用 |
| SDF | 标准延时格式，给门级仿真反标真实门延时 |
| DDC | Synopsys 自有二进制数据库，用于备份/恢复综合结果 |

> 提示：库设置和约束的细节本讲只做最小必要解释，约束文件的逐行解读在下一讲 **u6-l2** 展开。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲定位 |
|------|------|----------|
| [SYN/scripts/syn.tcl](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl) | 综合主脚本，42 行，本讲的**主角** | 全程精读 |
| [SYN/run](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/run) | 顶层启动脚本，调用 `dc_shell` | 了解如何运行 |
| [SYN/cons/cons.tcl](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl) | 时序约束文件，被 `syn.tcl` 第 19 行 `source` 进来 | 仅引用，细节见 u6-l2 |
| SYN/report/synth_qor.rpt | 综合 QoR 报告（时序/面积/单元数） | 用来验证综合结果 |
| SYN/output/FFT.{v,sdc,sdf,ddc} | 综合四大产物 | 脚本输出目标 |

---

## 4. 核心概念与源码讲解

本讲按脚本自然顺序拆成 4 个最小模块：**①库与路径设置 → ②RTL 读入 → ③多轮编译优化 → ④产物输出**。在第 5 节「综合实践」里我们再把它们重新归并成 5 个阶段。

### 4.1 工艺库与搜索路径设置

#### 4.1.1 概念说明

综合的本质是「把行为算子替换成工艺库里的真实单元」，所以**第一件事不是读 RTL，而是告诉 DC 去哪里找工艺库、用哪个库做映射**。这一段回答两个问题：

1. **search_path**：DC 找文件时要查哪些目录？这决定了它能找到 `.db` 库文件。
2. **target_library / link_library**：RTL 最终映射到哪一套标准单元？哪些单元允许被引用？

本项目用的是 **UMC 130nm 工艺的 Faraday 标准单元库**（库文件名前缀 `fsc0h_d_generic_core`，`fsc` = Faraday Standard Cell）。注意脚本里这两条库设置都指向了两个**工艺角（corner）**：

- `ss1p08v125c`：slow-slow 角，1.08 V、125 ℃，**最慢最差**，用来做建立时间（setup）分析，保证最坏情况下也能跑 100 MHz。
- `ff1p32vm40c`：fast-fast 角，1.32 V、−40 ℃，**最快最佳**，用来做保持时间（hold）分析，防止信号跑得太快冲到下一拍。

#### 4.1.2 核心流程

库设置阶段的三条命令构成一条递进的「告诉工具用什么工艺」的链路：

```
search_path  →  DC 去哪找 .db
target_library  →  RTL 映射成哪个库的单元（决定面积/延时）
link_library    →  解析引用时允许用哪些单元（含 target + 宏，* 先查自身）
```

随后 `define_design_lib work` 建立一个**工作库**目录，作为 DC 读入 RTL 后存放中间数据库的容器。

#### 4.1.3 源码精读

库与路径设置的前 10 行（[SYN/scripts/syn.tcl:1-10](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L1-L10)）：

```tcl
set design FFT                                                          ; 顶层模块名

set_app_var search_path "/home/IC/Desktop/.../UMC130nm/lib/StdCell"     ; 库文件搜索目录

set_app_var link_library "* fsc0h_d_generic_core_ss1p08v125c.db fsc0h_d_generic_core_ff1p32vm40c.db"
set_app_var target_library "fsc0h_d_generic_core_ss1p08v125c.db"        ; 映射用 ss 最差角

sh rm -rf work
sh mkdir -p work
define_design_lib work -path ./work                                     ; 工作库目录
```

要点逐条解读：

- 第 1 行 `set design FFT`：把顶层模块名存进变量 `design`，后续所有命令用 `${design}` 引用，方便换设计时只改一处。
- 第 3 行 `search_path`：注意这是一个**写死的绝对路径** `/home/IC/...`。换一台机器跑必须先改这一行，否则 DC 找不到 `.db`，综合直接失败。这是初学者最容易踩的坑。
- 第 5 行 `link_library` 里的 `*`：表示**先在设计自身已读入的内容里找引用**，再去找两个 `.db`。两个角都列进来，配合约束里的 `set_operating_conditions -min ... -max ...`（见 [cons.tcl:19](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L19)）做多角分析。
- 第 6 行 `target_library` **只给 ss 角**：映射门电路时按最差角选单元，留足余量，保证流片后最坏条件下也能跑 100 MHz。
- 第 8–10 行：用 shell 命令清空并重建 `work/` 目录，再 `define_design_lib` 把它登记为工作库。后续 `analyze` 出来的中间数据库就放在这里（`git ls-files SYN/work/` 能看到 `FFT.mr`、`RADIX2.mr`、各模块的 `.pvl/.syn` 等）。

#### 4.1.4 代码实践

1. **实践目标**：理解库路径是机器相关的，并学会定位本机库文件。
2. **操作步骤**：
   - 打开 [SYN/scripts/syn.tcl:3](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L3)，记下 `search_path` 指向的目录。
   - 在本地 DC 环境中，用 `dc_shell` 命令 `get_app_var search_path` 查看你当前机器的库搜索路径。
   - 确认 `target_library` 指向的 `fsc0h_d_generic_core_ss1p08v125c.db` 在该路径下确实存在。
3. **需要观察的现象**：若路径下没有该 `.db`，`link` 阶段会报 `Unable to resolve reference` 之类错误。
4. **预期结果**：本仓库的 `syn.tcl` 是作者本机（`/home/IC/...`）的配置，直接换机器运行多半会因找不到库而失败；需先把该行改成你本机的 UMC130nm 库路径。**待本地验证**（本仓库未提供库文件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `target_library` 只给 ss（最差）角，而 `link_library` 同时给 ss 和 ff 两个角？

> **参考答案**：`target_library` 决定**映射**——选最差角的单元尺寸最大、最慢，按它映射能保证最坏工艺偏差下仍满足时序，是保守安全的做法。`link_library` 决定**可引用**——综合时还要做多角（MMMC）时序分析（ss 查 setup、ff 查 hold），所以两个角的库都要列出来供 `set_operating_conditions` 切换使用。

**练习 2**：`link_library` 开头的 `*` 是什么意思？去掉会怎样？

> **参考答案**：`*` 表示「优先在设计自身已读入的库/网表里查找未解析的引用」。对纯 RTL 综合，去掉 `*` 通常也能跑（因为顶层 RTL 还没引用任何外部宏），但一旦设计例化了硬核宏（如 SRAM、IP），缺少 `*` 就可能解析失败。它是 DC 的通用保险写法。

---

### 4.2 RTL 读入与 elaborate

#### 4.2.1 概念说明

库设好后，第二步是把我们的 RTL 读进来。DC 读 RTL **分两步**：先 `analyze`（分析），再 `elaborate`（细化）。这不是冗余，而是 DC 的设计：

- **analyze**：逐文件做**语法分析**，把 Verilog 翻译成 DC 内部的中间表示，存进 `work` 工作库。它只检查语法、不构建层次。一个文件出错只会报这一个文件。
- **elaborate**：选定**顶层模块**，自顶向下**构建层次结构**，推断运算符（`+`→加法器、`*`→乘法器）、连线、参数，得到一个「未链接（unlinked）」的设计。此时 RTL 里的算子还是抽象的，没映射到具体门。

之后再做两件清理工作：`current_design` 锁定顶层、`uniquify` 给重名实例改名。

#### 4.2.2 核心流程

```
analyze (逐文件, 语法)  →  elaborate (顶层, 建层次, 推断算子)
        →  current_design (锁定顶层 FFT)  →  uniquify (重名实例唯一化)
```

注意 `analyze` 只读了**顶层一个文件** `../rtl/FFT.v`，但本仓库的顶层 `FFT.v` 内部用 `include` 把 `radix2.v`、5 个 `shift_N.v`、4 个 `ROM_N.v` 都拉进来了（见 [u1-l2](u1-l2-repo-structure.md) 的文件地图），所以一次 analyze 就够。如果模块是分开的独立文件，则要对每个文件各 analyze 一次。

#### 4.2.3 源码精读

读入段 4 行（[SYN/scripts/syn.tcl:13-16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L13-L16)）：

```tcl
analyze -library work -format verilog ../rtl/${design}.v   ; 读入顶层 RTL 到 work 库
elaborate $design -lib work                                ; 以 FFT 为顶层构建层次
current_design                                             ; 把 FFT 设为当前设计
uniquify                                                   ; 重名实例唯一化
```

逐条说明：

- 第 13 行 `analyze`：`-library work` 指定结果存到工作库，`-format verilog` 说明源文件是 Verilog（不是 VHDL），源文件是上一级目录的 `../rtl/FFT.v`。
- 第 14 行 `elaborate FFT`：以 `FFT` 为顶层展开整个设计层次。这一步会推断出本项目里那些 24 位加法、复数乘法（3 乘 5 加）等运算算子——此时它们还是「待映射的抽象算子」。
- 第 15 行 `current_design`：把刚 elaborate 出来的 `FFT` 设为「当前设计」，后续 `link`/`compile` 等命令默认作用于它。
- 第 16 行 `uniquify`：如果层次里有同一模块被多次例化（本项目的 `radix2` 被例化 5 次、`shift_N` 多次），uniquify 给每个实例一个唯一名字，避免后续命名冲突与后端工具混淆。

#### 4.2.4 代码实践

1. **实践目标**：体会 `analyze` 与 `elaborate` 的分工，并理解顶层 include 机制。
2. **操作步骤**：
   - 打开 [RTL/FFT.v](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v) 顶部的 `` `include `` 列表，确认它确实把 10 个子模块全拉进来了。
   - 对照 `syn.tcl` 第 13 行，确认只 analyze 了 `FFT.v` 一个文件。
   - 在 DC 里单独跑 `analyze` 后用 `get_designs` 查看 work 库里已有哪些设计；再跑 `elaborate FFT` 后再查一次，体会层次是 elaborate 阶段才构建的。
3. **需要观察的现象**：analyze 之后 work 库已能列出 `FFT`、`radix2`、`shift_16` 等模块名（语法实体）；elaborate 之后才形成带层次连接的完整设计。
4. **预期结果**：`get_designs` 在 analyze 后列出各模块，elaborate 后顶层 `FFT` 的 `get_cells` 能看到 5 个 `radix_no1~5` 实例。**待本地验证**（需 DC 环境）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `FFT.v` 改成不含 `include`、各子模块独立成文件，脚本第 13 行需要怎么改？

> **参考答案**：需要对每个 `.v` 文件各执行一次 `analyze`，例如：
> ```tcl> analyze -library work -format verilog {../rtl/FFT.v ../rtl/radix2.v ../rtl/shift_16.v ...}
> ```
> `analyze` 接受文件列表。elaborate 仍只需一次，且仍只 elaborate 顶层 `FFT`。

**练习 2**：`elaborate` 之后、`compile` 之前的 RTL 算子（比如复数乘法）处于什么状态？

> **参考答案**：处于「已推断但未映射」状态。DC 知道这里需要一个乘法/加法运算符，但还没把它替换成具体的标准单元（那要等 `compile`）。此时设计仍是「工艺无关」的抽象 GTECH 形式。

---

### 4.3 多轮编译优化

#### 4.3.1 概念说明

这是综合的**核心**。读入后的设计还是抽象算子，`compile` 才真正把算子映射成标准单元并优化。本项目用了**三轮**编译，不是一次到底：

| 阶段 | 命令 | 作用 |
|------|------|------|
| 第一轮 | `compile -map_effort medium` | 初次映射，把算子翻译成门，中等力度 |
| 寄存器优化 | `optimize_registers` | **重定时（retiming）**，前后移动寄存器以平衡流水线、减少触发器数量 |
| 第二轮 | `compile_ultra` | 高力度终极优化，用更激进算法压面积/提速度 |

为什么要分多轮？因为综合是个 NP-hard 的取舍问题，单轮 `compile` 用的启发式有限；`optimize_registers` 专门针对时序/寄存器做一轮重定时后，再让 `compile_ultra` 做高力度收尾，能比「一次 compile_ultra」得到更优的面积-时序折中，也便于观察每一步的改善。

在编译之前还有三件准备：`check_design`（结构体检）、`source cons.tcl`（载入时序约束）、`link`（把所有引用解析到 `link_library` 的真实单元）。约束是编译的「目标函数」——DC 优化时正是朝着满足约束的方向努力。

#### 4.3.2 核心流程

```
check_design  →  source cons.tcl (载约束: 10ns 时钟等)  →  link (解析引用)
        →  compile (映射门, 中等力度)
        →  optimize_registers (重定时, 平衡流水线)
        →  compile_ultra (高力度收尾)
```

约束文件 `cons.tcl` 在这里被 `source` 进来，其中定义了 10 ns 时钟、5 ns 输入/输出延迟、0.1 ns 时钟不确定性等（细节见 [u6-l2](u6-l2-synthesis-constraints.md)）。`link` 在约束之后，是因为约束里可能引用端口/时钟，需要先把设计连成整体。

#### 4.3.3 源码精读

编译优化段（[SYN/scripts/syn.tcl:18-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L18-L23)）：

```tcl
check_design                  ; 结构合法性体检（悬空端口、多驱动等）
source ./cons/cons.tcl        ; 载入 10ns 时钟与 I/O 延迟约束
link                          ; 把所有引用解析到 link_library 的真实单元
compile -map_effort medium    ; 第一轮: 算子→门, 中等力度
optimize_registers            ; 重定时: 前后搬寄存器, 平衡时序/减触发器
compile_ultra                 ; 第二轮: 高力度终极优化
```

要点：

- 第 18 行 `check_design`：编译前的「体检」，报告悬空线、多驱动、未连接端口等问题。它不修正，只报警，让你在花几十分钟编译前先发现低级错误。
- 第 19 行 `source ./cons/cons.tcl`：把约束文件载入。该文件（[SYN/cons/cons.tcl:1-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/cons/cons.tcl#L1-L23)）里 `create_clock -period 10` 设了 100 MHz 时钟，正是 DC 优化的目标。
- 第 20 行 `link`：解析所有单元引用。这一步后，设计才真正「挂」到了 UMC130nm 的标准单元上。
- 第 21 行 `compile -map_effort medium`：第一轮映射，`-map_effort medium` 是映射力度（low/medium/high），medium 是速度与质量的折中。
- 第 22 行 `optimize_registers`：寄存器优化。本项目是 5 级流水线，触发器众多（综合后 2392 个时序单元，见 QoR 报告），这一步对平衡各级流水线的时序很有价值。
- 第 23 行 `compile_ultra`：DC 的旗舰优化命令，自动做结构优化、重定时、门级优化等，力度远高于普通 `compile`，作为收尾。

综合结果可以由 QoR 报告验证（[SYN/report/synth_qor.rpt:36-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47)）：`clk` 路径组关键路径长度 **9.68 ns**、裕量（slack）**0.00**，恰好压在 10 ns 约束内通过——这正是多轮优化把时序压到极限的结果。总面积约 **202213 µm²**（[synth_qor.rpt:75-76](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L75-L76)）。

#### 4.3.4 代码实践

1. **实践目标**：观察三轮编译对时序/面积的逐步改善。
2. **操作步骤**：
   - 在第 21 行 `compile` 之后插入一行 `report_qor > ./report/qor_after_compile1.rpt`。
   - 在第 22 行 `optimize_registers` 之后插入 `report_qor > ./report/qor_after_optreg.rpt`。
   - 在第 23 行 `compile_ultra` 之后已有 `report_qor`（对应 `synth_qor.rpt`）。
   - 跑完综合后，对比三份 QoR 报告里 `clk` 组的 Critical Path Length 与 Cell Count。
3. **需要观察的现象**：第一轮 compile 后路径可能偏长/单元偏多；optimize_registers 后触发器数（Sequential Cell Count）应有变化；compile_ultra 后关键路径收窄到 9.68 ns 左右。
4. **预期结果**：三轮下来 Critical Path Length 单调下降，最终 slack ≥ 0 满足 10 ns。**待本地验证**（需 DC 与库环境；本改动是只在脚本里加报告，不动 RTL）。

#### 4.3.5 小练习与答案

**练习 1**：把 `compile`、`optimize_registers`、`compile_ultra` 三步合成一句 `compile_ultra`，理论上更省时间，为什么作者要拆成三轮？

> **参考答案**：拆轮的好处一是**可观测**——能分别看到初次映射、重定时、终极优化的效果，便于调试；二是**更优**——`optimize_registers` 在初次 compile 的门级结果上做重定时，再让 `compile_ultra` 收尾，往往比单轮更彻底地平衡流水线时序、压缩触发器数量。代价是综合时间更长（本设计 Overall Compile Wall Clock Time 约 630 s，见 QoR 报告）。

**练习 2**：`check_design` 发现了问题会自动修复吗？

> **参考答案**：不会。`check_design` 只产生警告/错误报告（如悬空端口、多驱动、未连接引脚），需要设计者自己回头改 RTL 或脚本。它的价值是在动辄几十分钟的 `compile` 之前先拦住低级结构错误，节省迭代时间。

---

### 4.4 网表/SDC/SDF/DDC 输出

#### 4.4.1 概念说明

编译完成、设计已是 UMC130nm 门级网表后，最后一步是把结果**写出来**给下游工具用。本项目脚本会产出四类文件，每类服务一个下游环节：

| 产物 | 命令 | 给谁用 |
|------|------|--------|
| 门级网表 `FFT.v` | `write -format verilog` | 后端 Innovus 布局布线；门级仿真 |
| 约束 `FFT.sdc` | `write_sdc` | 后端 P&R 的时序约束输入 |
| 延时 `FFT.sdf` | `write_sdf` | 门级仿真反标真实门延时（配合 testbench 的 GATE 模式） |
| 数据库 `FFT.ddc` | `write -f ddc` | DC 自身备份，便于后续恢复/复用 |

在写文件之前，还有 `report_*`（生成报告）和 `change_names`（重命名清理）两步。`change_names` 把单元名里 DC 内部使用的特殊字符、大小写冲突清理成合法 Verilog 标识符，避免下游 Innovus/仿真器不认识。这里有个工程细节：本项目还先 `define_name_rules no_case -case_insensitive` 再 `change_names`，是因为某些后端工具对大小写敏感，需统一。

#### 4.4.2 核心流程

```
report_area / report_qor / report_timing ...   ; 生成报告
        →  write_sdc (写出约束)
        →  define_name_rules + change_names (清理命名)
        →  write verilog (网表) / write ddc (数据库) / write_sdf (延时)
        →  exit
```

#### 4.4.3 源码精读

报告段（[SYN/scripts/syn.tcl:25-29](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L25-L29)）：

```tcl
report_area > ./report/synth_area.rpt                  ; 面积
report_cell > ./report/synth_cells.rpt                 ; 单元清单
report_qor  > ./report/synth_qor.rpt                   ; 质量(时序+面积+单元数)总览
report_resources > ./report/synth_resources.rpt        ; 运算资源
report_timing -max_paths 10 > ./report/synth_timing.rpt ; 关键路径时序(10 条)
```

写约束与改名段（[SYN/scripts/syn.tcl:31-37](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L31-L37)）：

```tcl
write_sdc  output/${design}.sdc                        ; 写出约束给后端

define_name_rules  no_case -case_insensitive           ; 定义大小写不冲突命名规则
change_names -rule no_case -hierarchy                  ; 全层次按规则改名
change_names -rule verilog -hierarchy                  ; 再按合法 Verilog 标识符改名
set verilogout_no_tri   true                           ; 输出不用 tri 线网
set verilogout_equation false                          ; 不用方程式 assign 写法
```

写出四大产物（[SYN/scripts/syn.tcl:39-42](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L39-L42)）：

```tcl
write -hierarchy -format verilog -output output/${design}.v   ; 门级网表
write -f ddc -hierarchy -output output/${design}.ddc          ; DC 二进制备份
write_sdf -version 2.1 -context verilog output/${design}.sdf  ; 标准延时格式(SDF 2.1)
exit                                                          ; 退出 dc_shell
```

要点：

- `report_qor` 是综合质量「总成绩单」，本项目 `clk` 路径组 slack=0.00、无违例（[synth_qor.rpt:36-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47)），表明恰好满足 100 MHz。报告解读是下一讲 u6-l3 的主题。
- `write_sdf` 的 `-version 2.1` 指定 SDF 版本，`-context verilog` 表示延时值按 Verilog 语义给出。这份 SDF 正是 testbench GATE 模式里 `$sdf_annotate` 要反标的文件（见 [u5-l1](u5-l1-testbench-snr-verification.md)），让门级仿真带真实门延时。
- `change_names` 必须在 `write` **之前**做，否则写出的网表里可能带下游工具不认识的命名。
- `write ... -hierarchy` 保留设计层次；`-format verilog` 输出标准 Verilog 网表，可被 Innovus 读入。
- 最后 `exit` 退出 `dc_shell`，日志由 `run` 脚本 `tee` 到 `log/syn.log`。

#### 4.4.4 代码实践

1. **实践目标**：验证四大产物都已正确生成，并理解它们各自喂给哪个下游环节。
2. **操作步骤**：
   - 用 `git ls-files SYN/output/` 列出综合产物，确认存在 `FFT.v`、`FFT.sdc`、`FFT.sdf`、`FFT.ddc` 四个文件。
   - 打开 `SYN/output/FFT.v` 前若干行，确认里面全是 `fsc0h_d_generic_core_...` 之类的标准单元实例，而不是 RTL 行为。
   - 打开 `SYN/output/FFT.sdf`，确认里面记录了每个单元的延时（IOPATH 延迟）。
3. **需要观察的现象**：`FFT.v` 里应看不到 `always`、`+`、`*` 这类行为，全是门单元实例化；`FFT.sdf` 里是 `(CELL ... (IOPATH ... ))` 形式的延时表。
4. **预期结果**：四个产物齐全，网表确实是门级。**可直接在仓库内验证**（产物已提交，无需 DC 环境）。

#### 4.4.5 小练习与答案

**练习 1**：`write_sdf` 生成的 `FFT.sdf` 最终被谁使用？

> **参考答案**：被 testbench 的 **GATE（门级）仿真模式**使用。在 `SIM/FFT_tb.v` 里，GATE 模式通过 `$sdf_annotate` 把这份 SDF 反标到门级网表上，使门级仿真带有综合后真实门延时，用来签核时序是否真的满足（见 u5-l1）。

**练习 2**：为什么 `change_names` 要做两次（先 `no_case` 再 `verilog`），并且必须在 `write` 之前？

> **参考答案**：先 `no_case` 解决大小写冲突（某些后端工具大小写不敏感，会把 `Abc` 和 `abc` 当同名），再 `verilog` 把名字清理成合法 Verilog 标识符（去掉特殊字符）。必须在 `write` 之前，否则写出的网表带非法/冲突命名，下游 Innovus 或仿真器会报错或误连。`write_sdc` 在改名之前是因为它基于设计的原始约束对象（端口/时钟名），不依赖单元改名后的名字。

---

## 5. 综合实践

**任务**：按实践要求，把 `syn.tcl` 的 42 行脚本分成 **5 个阶段（环境设置、读入、约束、优化、输出）**，用中文为每个关键命令写一句话注释，并画出综合全流程。

### 5.1 五阶段划分与注释

| 阶段 | 行号 | 关键命令 | 中文一句话注释 |
|------|------|----------|----------------|
| **① 环境设置** | 1–10 | `set design` / `search_path` / `link_library` / `target_library` / `define_design_lib` | 设定顶层名为 FFT，告诉 DC 去哪找 UMC130nm 标准单元库、用 ss 最差角做映射，并建立 work 工作库。 |
| **② 读入** | 13–16 | `analyze` / `elaborate` / `current_design` / `uniquify` | 语法分析顶层 FFT.v → 以 FFT 为顶层构建层次并推断算子 → 锁定顶层 → 重名实例唯一化。 |
| **③ 约束** | 18–20 | `check_design` / `source cons.tcl` / `link` | 编译前结构体检 → 载入 10ns 时钟与 I/O 延迟约束 → 把所有引用解析到真实标准单元。 |
| **④ 优化** | 21–23 | `compile` / `optimize_registers` / `compile_ultra` | 第一轮中等力度映射门电路 → 重定时平衡流水线/压触发器 → 高力度终极优化（最终 9.68ns 压线通过）。 |
| **⑤ 输出** | 25–42 | `report_*` / `write_sdc` / `change_names` / `write`(v,ddc) / `write_sdf` / `exit` | 生成面积/QoR/时序报告 → 写出 SDC 约束 → 清理命名 → 写出门级网表/DDC/SDF → 退出。 |

### 5.2 全流程串联图

```
┌──────── 环境设置 (L1-10) ────────┐
│ design=FFT; search_path;         │
│ link/target_library(UMC130nm);   │
│ define_design_lib work           │
└───────────────┬──────────────────┘
                ▼
┌──────── 读入 (L13-16) ───────────┐
│ analyze FFT.v → elaborate FFT    │
│ → current_design → uniquify      │
└───────────────┬──────────────────┘
                ▼
┌──────── 约束 (L18-20) ───────────┐
│ check_design → source cons.tcl   │
│ (10ns clock) → link              │
└───────────────┬──────────────────┘
                ▼
┌──────── 优化 (L21-23) ───────────┐
│ compile(medium) → optimize_regs  │
│ → compile_ultra                  │
│   结果: clk slack=0.00 通过      │
└───────────────┬──────────────────┘
                ▼
┌──────── 输出 (L25-42) ───────────┐
│ report_{area,qor,timing}         │
│ write_sdc → change_names         │
│ write FFT.v/.ddc/.sdf → exit     │
└──────────────────────────────────┘
        │       │       │
        ▼       ▼       ▼
   Innovus    门级仿真  DC备份
   (P&R)     (GATE+SDF) (.ddc)
```

### 5.3 动手验证（无需 DC 环境）

由于本仓库**已提交综合产物**，即使没有 DC 与 UMC 库，也能做源码阅读型验证：

1. 对照上表，逐行核对 [SYN/scripts/syn.tcl](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl) 的 5 个阶段行号是否吻合。
2. 打开 [SYN/report/synth_qor.rpt:36-47](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/report/synth_qor.rpt#L36-L47)，确认 `clk` 组 slack=0.00、无违例，作为「阶段④优化达标」的证据。
3. 打开 `SYN/output/FFT.v` 确认是门级网表，作为「阶段⑤输出」的证据。

### 5.4 进阶（需 DC 环境）

把 5.1 表里的中文注释**直接以 Tcl 注释形式**写回 `syn.tcl` 副本（不要改原脚本），用 `SYN/run` 重新跑一次综合，检查：
- `log/syn.log` 末尾是否正常 `exit`；
- `report/synth_qor.rpt` 的 slack 是否仍为非负；
- `output/` 下四个产物是否齐全。

> 待本地验证：本仓库不含 UMC130nm 库与 DC 工具，5.4 需在具备相应 EDA 环境的机器上完成。

---

## 6. 本讲小结

- **综合**是把 RTL 行为映射到 UMC130nm 巇标准单元、产出门级网表的过程，由 `dc_shell` 执行 `syn.tcl` 这 42 行脚本完成。
- 脚本可清晰分成**五阶段**：环境设置（库与路径）→ 读入（analyze/elaborate）→ 约束（check_design/source/link）→ 优化（compile/optimize_registers/compile_ultra）→ 输出（report + 写出 .v/.sdc/.sdf/.ddc）。
- 库设置里 `target_library` 用 **ss 最差角**做映射保余量，`link_library` 同时含 **ss/ff 两角**配合多角分析；`search_path` 是写死的本机路径，换机器必须改。
- 读入分 `analyze`（语法）与 `elaborate`（建层次、推断算子）两步，本项目靠顶层 `include` 一次 analyze 即可。
- 三轮编译（`compile` → `optimize_registers` → `compile_ultra`）层层逼近最优，最终 `clk` 路径 slack=0.00 恰好压线满足 100 MHz。
- 四大产物分工明确：`.v` 网表给后端 P&R、`.sdc` 给后端约束、`.sdf` 给门级仿真反标、`.ddc` 给 DC 自身备份。

## 7. 下一步学习建议

- **u6-l2 综合约束与时序模型**：精读 `cons.tcl` 的 23 行，搞清 10 ns 时钟、I/O 延迟、时钟不确定性、MMMC 多角、wireload 模型如何成为 DC 的优化目标。本讲的「约束」阶段在那里展开。
- **u6-l3 综合报告解读**：精读 `synth_qor/area/power/timing` 报告，理解 slack、关键路径、单元/寄存器数、面积与功耗数字。本讲引用的 9.68 ns、202213 µm²、2392 触发器将在那里详细解读。
- **u6-l4 布局布线（Innovus）**：跟踪本讲写出的 `FFT.v` 网表 + `FFT.sdc` 约束如何被 Innovus 读入，走完 floorplan→CTS→布线→GDS 的物理实现流程。
- 回顾 **u5-l1** 的 GATE 仿真模式，体会本讲写出的 `FFT.sdf` 如何被 `$sdf_annotate` 反标、完成综合后的功能+时序双重签核。
