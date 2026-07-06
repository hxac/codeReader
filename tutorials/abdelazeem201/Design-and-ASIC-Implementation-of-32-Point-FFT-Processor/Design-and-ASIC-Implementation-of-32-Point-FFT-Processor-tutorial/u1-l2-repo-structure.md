# 仓库结构与文件地图

## 1. 本讲目标

通过上一讲（u1-l1），你已经知道这颗 32 点 FFT 处理器"是什么"。本讲要解决的问题是"它放在哪里"——也就是把仓库拆开，让你看清每一类文件在做什么、它们如何串成一条从源码到芯片版图的完整链路。

学完本讲，你应该能够：

- 看懂 `RTL / SIM / SYN / Pnr / Pics` 五大目录各自的职责分工。
- 拿到 `RTL/` 下任意一个 `.v` 文件，立刻能说出它属于"顶层 / 蝶形 / 移位 / ROM"中的哪一类。
- 复述出"写 RTL → 仿真 → 综合 → 布局布线 → 出版图"这条 ASIC 设计主线在仓库里分别对应哪些文件。
- 独立用 `git ls-files` 列出仓库文件并画出一张目录树。

## 2. 前置知识

在进入目录细节前，先建立两个直觉。

### 2.1 一颗 ASIC 是怎么"长"出来的

数字芯片设计通常按下面这条主线推进，每一阶段都对应一类文件：

| 阶段 | 做什么 | 典型产物 |
|------|--------|----------|
| 设计 (RTL) | 用 Verilog/VHDL 描述电路行为 | `.v` 源码 |
| 仿真 (Simulation) | 喂激励、看波形、对黄金数据 | testbench、波形、日志 |
| 综合 (Synthesis) | 把 RTL 翻译成工艺标准单元网表 | 门级网表、约束、报告 |
| 布局布线 (P&R) | 把单元摆进芯片、连线、出物理版图 | GDS、时序报告 |

本仓库的四大目录 `RTL / SIM / SYN / Pnr` 正好一一对应这条主线，理解了这条主线，目录命名就不再神秘。

### 2.2 流水线 FFT 的三类积木

上一讲提到，本设计是"5 级流水线"，每级都由三类模块组成一个反馈回路。请先记住这三个名词，本讲在 `RTL/` 里会反复看到它们：

- **蝶形单元 (radix2)**：做加/减法和复数乘法的运算核心。
- **移位延时 (shift)**：FIFO 式的延时寄存器，把数据"卡"住若干拍。
- **ROM 状态控制 (ROM)**：存旋转因子，并产生驱动蝶形状态机的控制信号。

## 3. 本讲源码地图

本讲涉及的关键文件，按目录归类如下：

| 路径 | 作用 |
|------|------|
| `README.md` | 项目说明、设计规格表、各模块功能描述 |
| `RTL/FFT.v` | 顶层模块，例化 5 级蝶形+移位+ROM，并内嵌排序模块 |
| `SIM/FFT_tb.v` | 仿真 testbench，喂激励并计算 SNR 判定通过 |
| `SYN/scripts/syn.tcl` | Design Compiler 综合流程脚本 |
| `Pnr/scripts/pnr.tcl` | Innovus 布局布线流程脚本 |

> 说明：本讲重在"看地图"，所以会引用以上文件作为"路标"，但不会深入讲解每个模块的内部实现——那是后续 u3 / u4 / u6 单元的任务。

## 4. 核心概念与源码讲解

本讲按目录拆成四个最小模块：`RTL` 源码清单、`SIM` 仿真与参考模型、`SYN` 综合脚本与报告、`Pnr` 布局布线脚本与版图输出。最后在综合实践里把它们连成一条主线。

### 4.1 RTL 目录：源码模块清单

#### 4.1.1 概念说明

`RTL/`（Register Transfer Level，寄存器传输级）目录里放的是描述电路行为的 Verilog 源码。它是整个设计的"源"，后面所有阶段（仿真、综合、布线）都从这里出发。

本设计的 RTL 一共 **11 个 `.v` 文件**：1 个顶层 + 10 个被顶层例化的子模块。子模块恰好是 4.2 节那三类积木——1 个蝶形 + 5 个移位 + 4 个 ROM。

> 关于 manifest 里说的"10 个源文件"：指的是被例化的 10 个子模块（`radix2` + 5 个 `shift` + 4 个 `ROM`），加上顶层 `FFT.v` 就是 11 个文件。两种说法指的是同一件事。

#### 4.1.2 核心流程

顶层 `FFT.v` 通过 10 条 `\`include` 把所有子模块拉进来，再用 14 次模块例化（5 个蝶形 + 5 个移位 + 4 个 ROM）把它们搭成 5 级流水线：

```text
FFT.v（顶层）
  ├── include 10 个子模块文件
  ├── 例化 radix_no1 ~ radix_no5   （5 个蝶形）
  ├── 例化 shift_16/8/4/2/1        （5 个移位）
  └── 例化 rom16/8/4/2             （4 个 ROM，第 5 级用固定旋转因子，故少 1 个）
```

为什么只有 4 个 ROM？因为第 5 级（radix_no5）的旋转因子被硬编码成常数 `w_r=256, w_i=0`，不再需要 ROM 提供查表值。这个细节会在 4.1.3 的源码里看到，深入分析留到 u4。

#### 4.1.3 源码精读

**顶层模块的端口与时钟复位**——这是整个芯片对外暴露的接口：12 位有符号输入、16 位有符号输出。

[RTL/FFT.v:25-34](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L25-L34) 定义了 `clk / reset / in_valid / din_r / din_i / out_valid / dout_r / dout_i`，其中 `din_r/din_i` 是 12 位有符号、`dout_r/dout_i` 是 16 位有符号。

**include 列表**——10 条 include 正好对应 10 个子模块文件，可据此一眼数清依赖关系：

[RTL/FFT.v:14-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L14-L23) 依次 include 了 `shift_16/8/4/2/1.v`、`radix2.v`、`ROM_16/8/4/2.v`。

**第一级例化（蝶形+移位+ROM 三件套）**——这是流水线一级的标准结构，后面几级只是"复制粘贴 + 改名字"：

- [RTL/FFT.v:95-108](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L95-L108) 例化 `radix_no1`（第一级蝶形）。
- [RTL/FFT.v:110-117](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L110-L117) 例化 `shift_16`（第一级移位延时，深度 16）。
- [RTL/FFT.v:119-126](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L119-L126) 例化 `rom16`（第一级 ROM，提供旋转因子与状态）。

**第 5 级的特殊处理**——旋转因子被写死成常数，因此第 5 级没有 ROM 例化：

[RTL/FFT.v:236-237](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/RTL/FFT.v#L236-L237) 中 `radix_no5` 的 `.w_r(24'd256), .w_i(24'd0)` 直接传入常数。

把上述信息汇总成一张分类表，这张表是你以后读 RTL 的"索引"：

| 文件 | 模块名 | 类别 | 一句话作用 |
|------|--------|------|-----------|
| `FFT.v` | `FFT` | 顶层 | 例化 5 级子模块，内嵌排序（位反转还原） |
| `radix2.v` | `radix2` | 蝶形 | SDC PE 三态机，复数乘法 3 乘 5 加 |
| `shift_16.v` | `shift_16` | 移位 | 16 点 FIFO 延时 |
| `shift_8.v` | `shift_8` | 移位 | 8 点 FIFO 延时 |
| `shift_4.v` | `shift_4` | 移位 | 4 点 FIFO 延时 |
| `shift_2.v` | `shift_2` | 移位 | 2 点 FIFO 延时 |
| `shift_1.v` | `shift_1` | 移位 | 1 点 FIFO 延时 |
| `ROM_16.v` | `ROM_16` | ROM+控制 | 16 点级旋转因子 + 状态机 |
| `ROM_8.v` | `ROM_8` | ROM+控制 | 8 点级旋转因子 + 状态机 |
| `ROM_4.v` | `ROM_4` | ROM+控制 | 4 点级旋转因子 + 状态机 |
| `ROM_2.v` | `ROM_2` | ROM+控制 | 2 点级旋转因子 + 状态机 |

> 规律：文件名后缀的数字 = 该级延时点数 / 旋转因子规模，且逐级减半 16→8→4→2→1。

#### 4.1.4 代码实践

**实践目标**：不打开文件内容，仅凭文件名和顶层例化关系，建立"文件 ↔ 模块类别"的直觉。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files RTL/`，数一下 `.v` 文件数量，应得到 11。
2. 打开 `RTL/FFT.v`，只看 14~23 行的 `include` 列表和 95~252 行的模块例化，把每个例化名（`radix_no1`、`shift_16`、`rom16`……）对应到 4.1.3 的分类表中。
3. 数一下例化次数：蝶形几个？移位几个？ROM 几个？（答案：5 / 5 / 4）

**需要观察的现象**：例化名里"数字"的递减规律——`radix_no1→5`、`shift_16→1`、`rom16→2`——逐级减半，正好对应 32 点 FFT 的 5 级分解。

**预期结果**：你能凭直觉说出"`shift_8.v` 一定是第二级的移位延时模块"，而无需阅读其内部代码。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RTL/` 下有 5 个 `shift_*.v`，却只有 4 个 `ROM_*.v`？

**参考答案**：5 级流水线每级都需要一个移位延时，所以有 5 个 shift；但第 5 级的旋转因子是常数 `256+j0`（见 `FFT.v:236-237`），不需要查表 ROM，所以 ROM 只有 4 个。

**练习 2**：`FFT.v` 里 `radix_no1` 的输入 `din_b_r` 接的是谁？这说明了顶层和子模块的什么关系？

**参考答案**：`din_b_r(din_r_wire)`，接的是经过符号扩展的外部输入（`FFT.v:99`）。说明顶层负责把外部 12 位输入扩展成内部 24 位数据通路，再喂给第一级蝶形——顶层是"数据总线调度者"，蝶形只管算。

---

### 4.2 SIM 目录：仿真与参考模型

#### 4.2.1 概念说明

`SIM/` 目录承担"验证"职责：用一组测试激励驱动 RTL，把实际输出和"黄金参考"比对，判断设计对不对。这里的文件分成四类：

| 类别 | 文件 | 作用 |
|------|------|------|
| 仿真驱动 | `FFT_tb.v` | testbench，生成时钟/复位、喂激励、算 SNR |
| 软件参考模型 | `FFT.py`、`FFT.c`、`FFT_test.c` | 用软件算出"正确答案"作为黄金数据来源 |
| 旋转因子生成 | `twiddle_gen.py` | 生成 ROM 里要存的旋转因子定点值 |
| 测试激励文本 | `Test_cases/IN_*.txt`、`OUT_*.txt` | 5 组输入 pattern 与对应黄金输出 |
| 仿真产物 | `work/`、`*.mpf`、`*.wlf` | QuestaSim 编译库、工程文件、波形（已生成） |

#### 4.2.2 核心流程

testbench 的工作流是一条"喂→等→比"的循环：

```text
对 5 组数据集 (dataset=5) 循环：
  1. 打开该组 IN_real / IN_imag 输入文件
  2. 复位 → 拉高 in_valid → 连续喂入 32 个样本
  3. 等待 out_valid 拉高（带超时保护 latency_limit=68）
  4. 打开该组 OUT_real / OUT_imag 黄金文件
  5. 逐样本比对，累加 signal_energy 与 noise_energy
  6. 计算 SNR=10·log10(signal/noise)，SNR≥40dB 或 noise=0 则通过
全部通过 → 打印 "Well Done"，输出平均延迟
```

#### 4.2.3 源码精读

**testbench 的关键参数**——一眼看出设计规模与判定标准：

[SIM/FFT_tb.v:10-16](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L10-L16) 定义了 `FFT_size=32`、`dataset=5`、`IN_width=12`、`OUT_width=16`、`latency_limit=68`、`cycle=10.0`，这与上一讲的设计规格完全对得上（12 位输入、16 位输出、10 ns 周期）。

**RTL 与 GATE 两种仿真模式**——同一份 testbench 既能跑功能仿真，也能跑带 SDF 时序反标的门级仿真：

[SIM/FFT_tb.v:31-44](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L31-L44) 用 `\`ifdef RTL / \`elsif GATE` 宏区分两种模式，GATE 模式下调用 `$sdf_annotate("FFT_SYN.sdf", FFT_CORE)` 把综合后时序反标到网表上。

**DUT 例化**——testbench 把顶层 FFT 当作被测器件（DUT），命名为 `FFT_CORE`：

[SIM/FFT_tb.v:217-226](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L217-L226) 例化 `FFT FFT_CORE(...)`,把 testbench 内部的 `clk / rst_n / in_valid / din_r / din_i` 连到 DUT 端口。

> ⚠️ 待本地验证：testbench 在 [SIM/FFT_tb.v:64-81](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L64-L81) 里用 `$fopen("../Test_pattern/input/IN_real_pattern01.txt")` 读激励，而仓库实际把激励放在 `SIM/Test_cases/` 下。这意味着原作者是在某个子目录里跑仿真的、且使用了 `Test_pattern/input` 与 `Test_pattern/output` 的目录布局。你本地复现时需要软链接或调整路径，详见 4.2.4 的实践。

**激励文件命名规律**：`SIM/Test_cases/` 下共 20 个文件 = 5 组 × 4 类（`IN_real`、`IN_imag`、`OUT_real_16`、`OUT_imag_16`）。文件名里的 `pattern01~05` 对应 testbench 的 5 个 dataset，`_16` 表示 16 位黄金输出。

#### 4.2.4 代码实践

**实践目标**：把 `SIM/` 下的文件按"参考模型 / 测试激励 / 仿真驱动 / 产物"四类归档，并理解 testbench 与激励目录的路径关系。

**操作步骤**：

1. 执行 `git ls-files SIM/ | grep -v '^SIM/work/'`，过滤掉编译产物，列出真正"人写的"文件。
2. 把列出的文件填进 4.2.1 的分类表。
3. 阅读 [SIM/FFT_tb.v:62-87](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SIM/FFT_tb.v#L62-L87)，注意它打开输入文件的相对路径前缀是 `../Test_pattern/input/`。

**需要观察的现象**：testbench 期望的目录结构（`Test_pattern/input`、`Test_pattern/output`）与仓库实际的 `SIM/Test_cases/` 不一致。

**预期结果**：你得出结论——要在本地跑通仿真，要么把 `SIM/Test_cases/` 重命名/软链成 `Test_pattern/input` 与 `Test_pattern/output` 并放到 testbench 运行目录的上一级，要么修改 testbench 里的 `$fopen` 路径。这一步属于"工程化适配"，**待本地验证**具体哪种方式在你的仿真器下可行。

#### 4.2.5 小练习与答案

**练习 1**：`SIM/` 下哪些文件是"软件参考模型"？它们为什么和 `.v` 文件放在一起？

**参考答案**：`FFT.py`、`FFT.c`、`FFT_test.c` 是参考模型，`twiddle_gen.py` 是辅助生成器。它们和 RTL 放一起是因为它们承担"产出黄金数据"的职责——软件模型算出的正确结果，正是 testbench 比对 RTL 输出的依据，二者是验证的一体两面。

**练习 2**：`Test_cases/` 下为什么有 20 个文件？

**参考答案**：5 组 dataset × 4 类文件（实部输入、虚部输入、实部黄金输出、虚部黄金输出）= 20。

---

### 4.3 SYN 目录：综合脚本与报告

#### 4.3.1 概念说明

`SYN/` 目录把 RTL 翻译成 UMC 130nm 工艺的标准单元网表，这一步由 Synopsys Design Compiler（DC）完成。目录结构反映了 DC 的标准用法：

| 子目录/文件 | 作用 |
|------------|------|
| `scripts/syn.tcl` | 综合主流程 Tcl 脚本 |
| `cons/cons.tcl` | 时序/面积约束（时钟、I/O 延迟、工况） |
| `output/` | 综合产物：门级网表 `FFT.v`、`FFT.sdc`、`FFT.sdf`、`FFT.ddc` |
| `report/` | 综合报告：面积/时序/功耗/QoR/资源/单元 |
| `work/`、`*.log`、`run` | DC 工作库与运行日志 |

#### 4.3.2 核心流程

`syn.tcl` 是一条典型的 DC 综合流水线，可以分成 5 段：

```text
1. 环境设置  → search_path / link_library / target_library（指定 UMC130nm 单元库）
2. RTL 读入  → analyze → elaborate → uniquify → link
3. 加载约束  → source cons/cons.tcl
4. 多轮优化  → compile → optimize_registers → compile_ultra
5. 输出报告  → report_* 写报告；write_sdc/write/write_sdf 输出网表与库交换文件
```

#### 4.3.3 源码精读

**工艺库设置**——这决定了网表用哪家的"乐高积木"：

[SYN/scripts/syn.tcl:3-6](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L3-L6) 把 `search_path` 指向 UMC130nm 标准单元库，`target_library` 选 `fsc0h_d_generic_core_ss1p08v125c.db`（SS 慢角），`link_library` 同时挂上 SS 与 FF 两个角。

**RTL 读入主流程**——`analyze + elaborate` 是 DC 读 Verilog 的标准两步：

[SYN/scripts/syn.tcl:13-20](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L13-L20) 依次 `analyze`（语法解析）→ `elaborate`（建构层次）→ `uniquify`（实例唯一化）→ `check_design` → `source cons/cons.tcl` → `link`。

**多轮优化**——先粗后精，逐步压榨时序与面积：

[SYN/scripts/syn.tcl:21-23](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L21-L23) 用 `compile`（中等工作量）→ `optimize_registers`（寄存器优化）→ `compile_ultra`（高级优化）三步推进。

**报告与产物输出**——综合质量好坏全看这些报告：

[SYN/scripts/syn.tcl:25-41](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/SYN/scripts/syn.tcl#L25-L41) 写出 `synth_area/cells/qor/resources/timing` 五类报告，并用 `write_sdc / write(verilog) / write(ddc) / write_sdf` 输出约束、网表、DDC 数据库和标准延时格式（SDF）。

> 链路衔接：`SYN/output/FFT.v`（门级网表）和 `FFT.sdf`（时序）会被 u5 讲的 GATE 仿真、以及 `Pnr/` 布局布线消费。

#### 4.3.4 代码实践

**实践目标**：把 42 行 `syn.tcl` 按"环境 / 读入 / 约束 / 优化 / 输出"5 段切分，并理解每段产物去向。

**操作步骤**：

1. 打开 `SYN/scripts/syn.tcl`，对照 4.3.2 的 5 段流程，用注释把脚本分成 5 块。
2. 执行 `ls SYN/report/` 与 `ls SYN/output/`，把每个文件名对应到脚本里产生它的那条命令（例如 `synth_area.rpt` ← `report_area`，`FFT.sdf` ← `write_sdf`）。

**需要观察的现象**：`report/` 下有 6 个 `.rpt`，`output/` 下有 4 个产物文件（`.v/.sdc/.sdf/.ddc`）。

**预期结果**：你能复述"哪条 Tcl 命令产生了哪个文件"，建立脚本与产物的对应表。具体报告内容（关键路径、面积、功耗数值）的解读留到 u6 单元。

#### 4.3.5 小练习与答案

**练习 1**：`SYN/output/` 下的 `FFT.v` 和 `RTL/FFT.v` 有什么本质区别？

**参考答案**：`RTL/FFT.v` 是行为级 Verilog（含 `always`、模块例化），描述"要算什么"；`SYN/output/FFT.v` 是综合后的门级网表（全由标准单元实例和连线组成），描述"用哪些工艺单元实现"。前者是设计，后者是工艺映射结果。

**练习 2**：为什么要同时设置 `target_library`（SS 角）和 `link_library`（SS+FF 两角）？

**参考答案**：`target_library` 是综合时实际用来映射单元的库（选最保守的 SS 慢角，保证时序裕量）；`link_library` 用于解析设计中已存在的单元引用，挂上 FF 角是为了后续多角（MMMC）分析做准备。深入内容见 u6-l2。

---

### 4.4 Pnr 目录：布局布线脚本与版图输出

#### 4.4.1 概念说明

`Pnr/`（Place and Route，布局布线）目录把综合出的网表"摆"到真实芯片上：规划版图、铺电源、摆单元、做时钟树、连线、提 RC、出 GDS 版图。这一步由 Cadence Innovus 完成。

| 子目录/文件 | 作用 |
|------------|------|
| `scripts/pnr.tcl` | 布局布线主流程脚本 |
| `scripts/MMMC.tcl` | 多模式多角（MMMC）时序分析配置 |
| `scripts/Clock.ctstch` | 时钟树综合（CTS）配置 |
| `scripts/Default.globals` | Innovus 全局默认参数 |
| `output/` | 物理产物：`FFT.gds`、`FFT_CHIP.gds`、`FFT.sdf`、`FFT.v`、`streamOut.map` |
| `innovus`、`innovus.cmd`、`innovus.log` | Innovus 运行入口、命令记录、日志 |

#### 4.4.2 核心流程

`pnr.tcl` 是一条物理实现主线，可粗分为 6 段：

```text
1. 环境加载    → loadConfig / commitConfig / source MMMC.tcl / setDesignMode 130nm
2. Floorplan   → floorPlan（利用率 0.7）
3. 电源网络    → addRing（电源环）+ addStripe（电源条带）+ sroute（特殊布线）
4. 布局/CTS/布线 → placeDesign → clockDesign → routeDesign（隐含在脚本后续段）
5. 时序检查    → timeDesign 在 prePlace / preCTS / postCTS / postRoute 多个节点检查
6. 签核输出    → extractRC → write_sdf → saveNetlist → streamOut(GDS)
```

> 注：上面 4.4.2 列出的 `placeDesign / clockDesign / routeDesign` 是 Innovus 物理实现的标准命令。本讲只要求你掌握"目录里有什么、各自负责什么"，命令的逐行解读留到 u6-l4。

#### 4.4.3 源码精读

**工艺节点与多角配置**——`130` 对应 README 的 UMC 130nm：

[Pnr/scripts/pnr.tcl:2-5](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L2-L5) 依次 `loadConfig` → `commitConfig` → `source MMMC.tcl` → `setDesignMode -process 130`。

**Floorplan**——决定芯片形状与利用率：

[Pnr/scripts/pnr.tcl:7](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L7) 用 `floorPlan -r 1 0.7 20.0 20.0 20.0 20.0` 设定长宽比 1、利用率 0.7、四边各留 20µm 间距。

**电源网络**——电源环 + 电源条带，给每个单元供电：

[Pnr/scripts/pnr.tcl:10-11](https://github.com/abdelazeem201/Design-and-ASIC-Implementation-of-32-Point-FFT-Processor/blob/89ce7665eca8b77a0c48e610dab11928108566b6/Pnr/scripts/pnr.tcl#L10-L11) 用 `addRing` 围核心画 VCC/GND 环，`addStripe` 在 metal8（水平）/metal7（竖直）画电源条带。

**物理产物**——最终交付给晶圆厂的版图：

`Pnr/output/FFT.gds`（核心版图）与 `FFT_CHIP.gds`（含 pad 的全芯片版图）是 GDSII 格式的物理版图，是流片交付物；`FFT.sdf` 是版图后提取的真实时序，可用于 u5 讲的 GATE 后仿真。

#### 4.4.4 代码实践

**实践目标**：把 `Pnr/` 目录的脚本与产物对应起来，理解"脚本驱动物理实现、产物落在 output/"。

**操作步骤**：

1. 执行 `git ls-files Pnr/`，把结果按"脚本 / 产物 / 日志"三类分组。
2. 打开 `Pnr/scripts/`，列出 4 个文件（`pnr.tcl`、`MMMC.tcl`、`Clock.ctstch`、`Default.globals`），用一句话写出各自职责（提示：主流程 / 多角配置 / 时钟树 / 全局默认）。
3. 执行 `ls -lh Pnr/output/FFT.gds Pnr/output/FFT_CHIP.gds`，注意 GDS 文件体积（十几 MB 起步），体会"版图数据量远大于 RTL 源码"。

**需要观察的现象**：`Pnr/output/` 同时有 `FFT.gds` 和 `FFT_CHIP.gds`，前者是核心逻辑版图，后者是带 IO ring 的完整芯片版图。

**预期结果**：你能复述 `Pnr/` 的"脚本 → 工具 → 版图产物"链路。命令的完整逐行含义**待 u6-l4 深入**，本讲不展开。

#### 4.4.5 小练习与答案

**练习 1**：`Pnr/output/FFT.sdf` 和 `SYN/output/FFT.sdf` 都叫 SDF，它们有什么不同？

**参考答案**：两者都是标准延时格式，但来源不同。`SYN/output/FFT.sdf` 是综合后基于线负载模型（wireload）估算的延时，比较粗；`Pnr/output/FFT.sdf` 是布局布线后从真实布线寄生（RC）提取的延时，更接近流片后的实际时序。后者更"真实"，是签核依据。

**练习 2**：为什么 `Pnr/scripts/` 下要单独有一个 `MMMC.tcl`？

**参考答案**：MMMC = Multi-Mode Multi-Corner，即同时在多个工艺角（SS/FF）、多个电压/温度、多个工作模式下检查时序。物理实现阶段的时序收敛必须覆盖这些角，所以专门用一个 Tcl 文件集中配置，被主脚本 `pnr.tcl` 在最开头 `source` 进来。

## 5. 综合实践

**贯穿任务：画出本仓库的"文件链路图"并标注数据流向。**

把本讲四个最小模块串起来，做一张完整的链路图：

1. **列文件**：在仓库根目录执行 `git ls-files`（可配合 `grep -vE 'SIM/work/|SYN/work/'` 过滤编译产物），得到全部源文件清单。
2. **画目录树**：按 4.1~4.4 的分类，画出五大目录的树状结构，每个目录下标注 1~3 个代表性文件。
3. **标数据流**：在树上用箭头标出主线条数据流向：
   - `RTL/*.v` →（被 `SIM/FFT_tb.v` 例化）→ 仿真波形/SNR 判定
   - `RTL/*.v` →（被 `SYN/scripts/syn.tcl` 的 analyze 读入）→ `SYN/output/FFT.v`（网表）+ `FFT.sdf`
   - `SYN/output/*` →（被 `Pnr/scripts/pnr.tcl` 读入）→ `Pnr/output/FFT.gds`（版图）+ 版图后 `FFT.sdf`
   - `SIM/Test_cases/OUT_*.txt` ←（由 `SIM/FFT.py` 等参考模型生成）→ 作为黄金数据回流给 testbench
4. **分类标注**：在 `RTL/` 树的每个 `.v` 旁标注类别（顶层/蝶形/移位/ROM）；在 `SIM/` 树旁标注（参考模型/激励/驱动/产物）。

**预期产出**：一张同时体现"模块分类"和"ASIC 主线数据流"的目录树示意图。这张图会成为你后续阅读 u3~u6 各篇讲义时的"总索引"。

> 如果无法本地运行仿真/综合工具也没关系——本任务是"源码阅读型实践"，重点是理清文件之间的依赖与数据流向，不需要真的跑工具。

## 6. 本讲小结

- 仓库由 `RTL / SIM / SYN / Pnr / Pics` 五大目录组成，前三者正好对应 ASIC 设计主线"设计 → 仿真 → 综合 → 布局布线"。
- `RTL/` 共 11 个 `.v` 文件：1 个顶层 `FFT.v` + 10 个子模块（1 蝶形 `radix2` + 5 移位 `shift_16~1` + 4 ROM `ROM_16~2`），名字后缀数字逐级减半。
- `SIM/` 同时容纳仿真驱动（`FFT_tb.v`）、软件参考模型（`FFT.py/FFT.c/FFT_test.c`）、旋转因子生成器（`twiddle_gen.py`）和 5 组共 20 个激励/黄金文本（`Test_cases/`）。
- `SYN/` 用 `scripts/syn.tcl` 走完"环境→读入→约束→优化→输出"5 段，产出网表/SDC/SDF/DDC 与 6 类报告。
- `Pnr/` 用 `scripts/pnr.tcl` 完成 floorplan→电源→布局→CTS→布线→签核，最终产出 `FFT.gds` 物理版图与版图后 SDF。
- 一个值得记住的链路：`RTL 源码 → 综合网表 → 物理版图`，每一跳都伴生一份 SDF（延时信息），分别服务于不同精度的仿真。

## 7. 下一步学习建议

- **下一讲 u1-l3（仿真快速上手）**：会用本讲建立的 `SIM/` 地图，带你真正跑一次仿真、看波形、读 SNR 输出。
- **进入 u2 算法基础**：想理解 `SIM/FFT.py` 和 `twiddle_gen.py` 到底算的是什么，先学 radix-2 DIF 与旋转因子。
- **进入 u3 RTL 拆解**：想看懂 `radix2.v / shift_*.v / ROM_*.v` 的内部实现，需要 u2 的算法基础。
- **延后阅读**：`SYN/` 与 `Pnr/` 的脚本细节集中在 u6 单元，本讲只需"知道有什么"，不必现在读懂每条 Tcl 命令。
