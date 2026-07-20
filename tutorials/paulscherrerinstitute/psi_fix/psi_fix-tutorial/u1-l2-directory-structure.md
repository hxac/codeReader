# 目录结构与源码组织

## 1. 本讲目标

本讲承接 [u1-l1 项目概览与定位](u1-l1-project-overview.md)。在上一讲里，我们知道了 psi_fix 是一个「位真双模型」的定点 DSP VHDL 库：每个 VHDL 组件必须配套一个逐位一致的 Python 模型，并由自检测试台比对。本讲不再讲理念，而是带你走进仓库内部，看清楚：

- 顶层有哪些目录，每个目录各自负责什么。
- 一个组件（比如 `psi_fix_mov_avg`）的 VHDL 源码、Python 模型、测试台、文档分别放在哪里，它们是如何「一一对应」的。
- `model/snippets` 与 `model/matlab` 这两个容易被忽略的子目录是干什么的。
- 文档与回归脚本是如何把上述所有文件串起来跑的。

学完后，你应该能在不借助搜索的情况下，凭命名规则直接定位任何一个组件的五件套（VHDL / 模型 / 测试台 / preScript / 文档），并为后续每一讲的源码精读建立一张「地图」。

## 2. 前置知识

阅读本讲前，你只需要理解上一讲引入的两个概念：

- **位真双模型（bittrue dual model）**：同一个信号处理算法有两个实现——可综合的 VHDL 与逐位一致的 Python 模型，Python 模型充当黄金参考。
- **自检测试台（self-checking testbench）**：测试台运行时调用 Python 模型生成期望结果，再与 VHDL 输出逐位比对，不一致就打印 `###ERROR###`。

本讲全部是「认识目录」的轻量内容，不涉及 VHDL 语法和定点运算细节。会用到的两个小工具命令是 `git ls-files`（列出 git 跟踪的文件）和 `Read`/`Grep`（读文件、搜内容）。

## 3. 本讲源码地图

本讲主要解读仓库的「骨架文件」，它们大多是文档与脚本，而非 DSP 算法本身：

| 文件 | 作用 |
|:-----|:-----|
| [README.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md) | 仓库根说明：库定位、许可证、依赖、贡献规则。规定了「一个 .vhd 文件对应一个实体」的组织原则。 |
| [doc/files/introduction.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/introduction.md) | 详细文档首页：工作副本结构、外部依赖、仿真方式、贡献规则、握手约定。 |
| [doc/README.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/README.md) | 文档目录索引：一张「组件 → VHDL 源码 → 文档」的对照表。 |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) | 回归测试配置：声明全部源码与测试台、注册每个测试台运行方式。 |

此外，我们会以 `psi_fix_mov_avg`（移动平均）作为贯穿全文的「样例组件」，引用它的：

| 文件 | 作用 |
|:-----|:-----|
| [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) | VHDL 实现（综合用）。 |
| [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py) | Python 位真模型。 |
| [testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd) | VHDL 自检测试台。 |
| [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) | 协同仿真预脚本：跑 Python 模型，生成输入/期望输出文本。 |
| [doc/files/psi_fix_mov_avg.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_mov_avg.md) | 组件文档。 |

## 4. 核心概念与源码讲解

先用一张表建立全局印象。在仓库根目录执行 `git ls-files | cut -d/ -f1 | sort | uniq -c`，可以看到 git 跟踪文件的顶层分布（数量会随版本变化，此处为当前 HEAD 的统计）：

| 顶层条目 | 文件数 | 职责 |
|:---------|:------:|:-----|
| `doc/` | ~119 | 文档：组件说明、入门、设计流程、图示 |
| `testbench/` | ~101 | VHDL 自检测试台 + 每个 TB 的 `Scripts/preScript.py` |
| `hdl/` | ~43 | 可综合 VHDL 源码（包 + 各组件实体） |
| `model/` | ~35 | Python 位真模型 + 代码生成器 + 模板 |
| `scripts/` | ~9 | CI、依赖检出、文档生成、重构脚本 |
| `sim/` | ~7 | 仿真回归脚本（Modelsim / GHDL） |
| `sigasi/` | ~4 | Sigasi VHDL IDE 工程配置 |
| `unittest/` | ~2 | Python 包的单元测试 |
| 根目录文件 | — | `README.md`、`Changelog.md`、`License.txt`、`LGPL2_1.txt`、`.gitignore` |

一个直观的规律是：`hdl`、`model`、`testbench`、`doc` 四个目录的文件数大致同量级，且文件名高度同构——这正是「一组件、四件套」组织方式的结果。

下面逐个拆分。

### 4.1 hdl 源码目录

#### 4.1.1 概念说明

`hdl/` 存放所有**可综合**的 VHDL 源码。这是整个库的「正式产出物」——最终被综合到 FPGA 比特流里的代码就在这里。

README 明确规定了这里的组织纪律：

> It is suggested to use one .vhd file per Package or Entity.

也就是说，**一个 `.vhd` 文件只放一个 package 或一个 entity**，不把多个实体塞进同一文件。这让文件名本身就成了「索引」：看到一个文件名，就知道它里面是什么。

#### 4.1.2 核心流程

`hdl/` 里其实有两类文件：

1. **包（package）**：公共类型与函数定义。本仓库最核心的是 `hdl/psi_fix_pkg.vhd`，它是所有组件都要 `use` 的定点运算包（第二单元会专门精读）。此外还有两份命名规则文档 `hdl/CicNaming.txt`、`hdl/FirNaming.txt`，用纯文本记录 CIC / FIR 组件的命名约定（不是 VHDL，而是给人和脚本看的「字典」）。
2. **实体（entity）**：每个 DSP 组件一个文件，文件名即组件名，如 `hdl/psi_fix_mov_avg.vhd`、`hdl/psi_fix_cordic_rot.vhd`、`hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd`。

命名是「自描述」的：名字里编码了功能（`mov_avg`、`cordic_rot`）、结构（`dec_ser` 串行抽取、`par` 并行、`semi` 半并行）、通道方式（`nch` 多通道、`chtdm`/`chpar` 通道 TDM/并行）、系数处理（`conf` 可配置、`fix` 固定）。这些命名规则会在第六、七单元（CIC / FIR）展开。

#### 4.1.3 源码精读

README 第 31 行给出组织原则（一句话、好记）：

[README.md:31-31](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L31) —— 规定「一个 .vhd 文件对应一个 package 或 entity」。

在 [sim/config.tcl:64-105](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L64-L105) 里能看到 `hdl/` 全部实体被逐一列出（`add_sources "../hdl" {...} -tag src`）。注意第 84 行正是我们要跟踪的样例：

[sim/config.tcl:84-84](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L84) —— `psi_fix_mov_avg.vhd` 出现在项目源码列表里。

而包单独先编译（因为实体依赖包），见 [sim/config.tcl:51-53](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L51-L53)：`psi_fix_pkg.vhd` 被单独 `add_sources`，紧跟其后才是各组件实体。

#### 4.1.4 代码实践

1. **实践目标**：用工具命令自己「摸」一遍 `hdl/` 目录，验证「一文件一实体」。
2. **操作步骤**：
   - 执行 `git ls-files hdl`，列出 `hdl/` 下全部跟踪文件。
   - 数一下 `.vhd` 文件个数，与上面表格里的「~43」对照。
   - 找出其中唯一的包文件（提示：以 `_pkg.vhd` 结尾）和两份 `.txt` 命名规则文件。
3. **需要观察的现象**：文件名几乎都以 `psi_fix_` 开头；除包外，每个文件名都能望文生义说出它实现什么。
4. **预期结果**：包文件是 `hdl/psi_fix_pkg.vhd`；命名规则文件是 `hdl/CicNaming.txt` 和 `hdl/FirNaming.txt`。其余都是组件实体。
5. 本实践为只读命令，无破坏性，可在本地直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：`hdl/psi_fix_fir_dec_ser_nch_chtdm_conf.vhd` 这个名字里的 `dec`、`ser`、`nch`、`chtdm`、`conf` 各自暗示了什么？

**答案**：`dec` = decimation（抽取）；`ser` = serial（串行计算结构）；`nch` = N channels（多通道）；`chtdm` = channel TDM（通道以时分复用方式传输）；`conf` = configurable（系数可在运行时配置）。完整含义在第七单元的 FirNaming 讲义展开。

**练习 2**：为什么 `psi_fix_pkg.vhd` 要在 `config.tcl` 里被单独、提前地 `add_sources`，而不是和其他实体列在一起？

**答案**：因为所有组件实体都 `use work.psi_fix_pkg.all`，包是它们的编译依赖。VHDL 要求被依赖的单元先编译进库，把包单独列在前面的源码块里，能保证编译顺序正确。

### 4.2 model/python 模型目录

#### 4.2.1 概念说明

`model/` 存放每个组件的 **Python 位真模型**，以及少量代码生成工具。这是「双模型」的另一半——不上 FPGA，却在每次回归测试里充当黄金参考。

注意：`model/` 是 Python 代码目录，不是 VHDL 目录。它的存在正是 psi_fix 区别于普通 VHDL 库的关键。

#### 4.2.2 核心流程

`model/` 内部有三层：

1. **模型包**：`model/psi_fix_pkg.py`，与 `hdl/psi_fix_pkg.vhd` 一一对应的 Python 版定点包（类型、运算函数）。这是模型世界的「地基」。
2. **组件模型**：每个组件一个 `.py`，类名与文件名一致，如 `model/psi_fix_mov_avg.py` 里定义 `class psi_fix_mov_avg`。组件模型通过 `from psi_fix_pkg import *` 复用定点运算。
3. **代码生成**：两个子目录：
   - `model/snippets/`：模板文件（`*_tmpl.vhd`），如 `psi_fix_lin_approx_tmpl.vhd`、`psi_fix_lut_tmpl.vhd`、`psi_fix_pkg_writer_tmpl.vhd`、`psi_fix_lin_approx_tb_tmpl.vhd`。生成器读模板、替换占位符，批量产出 `hdl/psi_fix_lin_approx_*.vhd` 这类「由表驱动的」组件（第八单元详解）。
   - `model/matlab/`：一组 `.m` 辅助函数（`fix_psi2cl.m`、`fix_cl2psi.m`、`vect_np2ml.m` 等）和一个 `Example.m`，让 MATLAB 也能调用这些 Python 模型，从而「只维护一套模型代码」。

一句话：`model/` 不只是「写一遍 Python」，它还承担「自动生成 VHDL」和「被 MATLAB 复用」两项职责。

#### 4.2.3 源码精读

`model/psi_fix_mov_avg.py` 的开头展示了组件模型的典型骨架——导入模型包、定义与 VHDL 同名的类与常量：

[model/psi_fix_mov_avg.py:10-25](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py#L10-L25) —— `from psi_fix_pkg import *` 复用定点包；`class psi_fix_mov_avg` 内定义 `GAINCORR_NONE/ROUGH/EXACT` 三个常量，对应 VHDL 里 `gain_corr_g` 的三种取值。

README 在「MATLAB」一节说明了 `model/matlab` 的存在意义：

[README.md:85-89](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L85-L89) —— 「Not having separate MATLAB models allows maintaining only one code base」，并指向 `model/matlab`。

#### 4.2.4 代码实践

1. **实践目标**：验证「VHDL 与 Python 模型 API 同构」。
2. **操作步骤**：
   - 打开 [model/psi_fix_mov_avg.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/model/psi_fix_mov_avg.py) 的构造函数（`__init__`，约第 30 行起），记下它的参数 `inFmt`、`outFmt`、`Taps`、`gainCorr` 等。
   - 打开 [hdl/psi_fix_mov_avg.vhd](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/hdl/psi_fix_mov_avg.vhd) 的 `generic` 声明，对照 `in_fmt_g`、`out_fmt_g`、`taps_g`、`gain_corr_g`。
   - 执行 `git ls-files model/snippets model/matlab`，确认两个子目录的实际内容。
3. **需要观察的现象**：Python 构造函数的参数与 VHDL generics 几乎一一对应（只是类型不同：Python 传 `psi_fix_fmt_t` 对象，VHDL 传 generic）。
4. **预期结果**：两侧的「可配置项」完全一致，这正是「位真双模型」能做到逐位比对的根基。
5. 本实践为源码阅读型，不运行模型；若要实际运行模型，需先安装 SciPy/NumPy（见 u1-l1）。

#### 4.2.5 小练习与答案

**练习 1**：`model/snippets/` 里的文件以 `_tmpl.vhd` 结尾，但它们在 `hdl/` 目录里吗？为什么？

**答案**：不在 `hdl/`。`snippets/` 存放的是**模板**，不是最终组件。Python 代码生成器（如 `model/psi_fix_lin_approx.py`）读取这些模板、填入针对 sin/sqrt/inv/gaussify 等不同函数生成的表，才产出 `hdl/psi_fix_lin_approx_sin18b.vhd` 这类最终 VHDL 文件。

**练习 2**：既然已经有 Python 模型，为什么还要 `model/matlab/`？

**答案**：为了让 MATLAB 用户也能复用同一套位真模型，而不必再维护一份 MATLAB 版本。`model/matlab/` 提供桥接函数（定点格式互转、数组在 NumPy 与 MATLAB 之间传递），README 明确说目标是「只维护一套代码」。

### 4.3 testbench 与 sim 目录

#### 4.3.1 概念说明

这是「验证」一侧。`testbench/` 存放 VHDL 测试台与协同仿真脚本；`sim/` 存放把所有测试台串起来跑回归的 TCL 脚本。两者配合，实现「一条命令跑完整个库的位真校验」。

每个组件的测试台都住在一个**以组件名加 `_tb` 命名的子目录**里，例如 `testbench/psi_fix_mov_avg_tb/`。这是 testbench 目录最关键的命名约定。

#### 4.3.2 核心流程

一个测试台子目录的典型结构是：

```
testbench/psi_fix_mov_avg_tb/
├── psi_fix_mov_avg_tb.vhd      # VHDL 自检测试台（DUT 例化 + stim/check 进程）
└── Scripts/
    └── preScript.py            # 协同仿真预脚本（跑 Python 模型）
```

协同仿真（co-simulation）的数据流是：

```text
preScript.py  --跑 Python 模型-->  Data/input.txt, Data/output_*.txt
      (编译前由 config.tcl 调用)
                                        │
                                        ▼
psi_fix_mov_avg_tb.vhd  --读回文本-->  与 VHDL DUT 输出逐位比对
                                        │ 不一致则打印 ###ERROR###
                                        ▼
                                    sim/run.tcl 扫描 transcript 判定通过/失败
```

关键点：`preScript.py` 在**编译之前**就被执行（因为它要生成测试台读的数据，甚至可能生成待编译的代码）。`sim/config.tcl` 用 `tb_run_add_pre_script` 把这一步挂上去。

`sim/` 目录里的脚本职责分工：

| 文件 | 作用 |
|:-----|:-----|
| `sim/config.tcl` | 声明源码、测试台，注册每个测试台的运行参数与 pre_script |
| `sim/run.tcl` | Modelsim 入口：`source ./run.tcl` 跑全部回归 |
| `sim/runGhdl.tcl` | GHDL 入口（开源仿真器，同类回归） |
| `sim/interactive.tcl` / `interactiveGhdl.tcl` | 交互式开发：选择性地编译/运行单个测试台 |
| `sim/ci.do` | CI 用的 Modelsim do 文件 |

（回归脚本如何判定成功/失败、如何跑，是下一讲 [u1-l3 仿真与回归测试框架](u1-l3-simulation-regression.md) 的主题，本讲只关注「目录与注册」。）

#### 4.3.3 源码精读

测试台子目录命名一一对应组件，见 [sim/config.tcl:108-160](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L108-L160) 的 `add_sources "../testbench" {...} -tag tb` 列表，其中第 135 行就是样例：

[sim/config.tcl:135-135](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L135) —— `psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd`。

而 `psi_fix_mov_avg_tb` 的「运行方式」在更下方定义，注意它挂着 pre_script、并跑三组不同参数：

[sim/config.tcl:298-304](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl#L298-L304) —— `create_tb_run "psi_fix_mov_avg_tb"`，第 299 行 `tb_run_add_pre_script "python3" "preScript.py" "../testbench/psi_fix_mov_avg_tb/Scripts"`，随后三组 `-ggain_corr_g=...` 参数分别覆盖 NONE/EXACT/ROUGH 三种增益校正。

preScript 把模型结果写成整数文本，是「Python → 文本 → VHDL」协同仿真的衔接点：

[testbench/psi_fix_mov_avg_tb/Scripts/preScript.py:7-15](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L7-L15) —— 第 7 行 `sys.path.append("../../../model")` 把模型目录加入搜索路径；第 9–10 行导入 `psi_fix_pkg` 与 `psi_fix_mov_avg`；第 15 行把输出目录指向 `../Data`。

[testbench/psi_fix_mov_avg_tb/Scripts/preScript.py:58-60](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py#L58-L60) —— 用 `psi_fix_get_bits_as_int(...)` 把浮点结果转成定点整数位，`np.savetxt` 写出 `Data/input.txt` 与 `Data/output_*.txt`。

测试台本身是脚本生成的（文件头有说明），它例化 DUT、定义可由 generic 注入的参数：

[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd:7-9](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L7-L9) —— 注释写明「Testbench generated by TbGen.py」。

[testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd:29-36](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd#L29-L36) —— 实体的 `generic`：`gain_corr_g`、`file_folder_g`、`duty_cycle_g`、`out_regs_g`，这些正是 `config.tcl` 在三组 `tb_run_add_arguments` 里注入的参数。

#### 4.3.4 代码实践

1. **实践目标**：完整跟踪一个组件「从 config.tcl 注册到 preScript 生成数据」的路径。
2. **操作步骤**：
   - 在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/sim/config.tcl) 中搜索 `mov_avg`，定位三处：源码列表（L84）、测试台列表（L135）、测试台运行块（L298–L304）。
   - 打开 [preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py)，确认它写入的文件名是 `input.txt` 和 `output_none.txt` / `output_rough.txt` / `output_exact.txt`（注意第 60 行 `gc.lower()`）。
   - 对照测试台 generic `file_folder_g`，理解 `config.tcl` 第 300 行 `set dataDir [file normalize "../testbench/psi_fix_mov_avg_tb/Data"]` 把这个目录传进测试台。
3. **需要观察的现象**：`config.tcl` 第 299 行指定的 Scripts 路径，与 preScript.py 第 15 行的 `../Data` 输出路径，恰好指向同一个 `psi_fix_mov_avg_tb/Data/` 目录——数据在那里「交接」。
4. **预期结果**：能画出「config.tcl → preScript.py(写 Data/*.txt) → tb.vhd(读 Data/*.txt 并比对)」的闭环。
5. 本实践为源码阅读型；实际运行回归需要在装有 Modelsim 或 GHDL 的环境里 `source ./run.tcl`（详见下一讲），本地若缺仿真器可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `preScript.py` 必须在「编译之前」运行，而不能在仿真启动后再跑？

**答案**：因为它生成的 `Data/*.txt` 是测试台运行时要读的输入和期望输出；如果编译/仿真已经开始才生成，测试台读到的就是空文件或旧文件。`config.tcl` 用 `tb_run_add_pre_script` 把它挂在编译前，保证数据先就位。对于会生成 VHDL 代码的 preScript（如 `psi_fix_lut_gen_tb`），更必须在编译前跑完。

**练习 2**：`testbench/psi_fix_mov_avg_tb/` 子目录名为什么是组件名加 `_tb`，而不是随便起名？

**答案**：这是全库一致的命名约定，让 `config.tcl`、`hdl2md`、以及人都能用「组件名 + `_tb`」机械地推导出测试台路径，无需查表。`config.tcl` 里成百上千行路径都遵循这一规则。

### 4.4 scripts/doc 工具目录

#### 4.4.1 概念说明

`scripts/` 与 `doc/` 是「幕后」目录：前者放自动化脚本（CI、依赖、文档生成、重构），后者放文档产出。它们不参与综合，但决定了「库能不能被可信地维护和发布」。

#### 4.4.2 核心流程

**`scripts/`** 内的脚本按职责分组：

| 脚本 | 作用 |
|:-----|:-----|
| `scripts/ciFlow.py` | CI 主流程：跑回归 + Python 单测，解析 transcript 判定通过/失败 |
| `scripts/dependencies.py` | 依赖检出：按 README 里被解析的依赖段，拉取 en_cl_fix/psi_common/psi_tb/PsiSim |
| `scripts/hdl2md.py` / `hdl2md_all.py` | 从 VHDL 实体自动生成组件文档（解析 generics/ports 写成 Markdown 表） |
| `scripts/refactoring/` | 一次性重构脚本（如 v2→v3 的 camelCase→snake_case 迁移），含 JSON 数据库 |

**`doc/`** 内部分层：

| 子项 | 作用 |
|:-----|:-----|
| `doc/README.md` | 文档总索引：一张「组件 → 源码 → 文档」对照表 |
| `doc/files/` | 每个组件一份 `.md`（多为 `hdl2md` 生成）+ 入门/技巧/设计流程 + 图示 `.png` |
| `doc/old/` | 旧版 PDF 文档（历史留存） |
| `doc/visio/` | 图示源文件（`.vsd`） |

`doc/README.md` 的核心是一张表，把每个组件名链接到它的文档页和 VHDL 源码——这正是「组件四件套」里的「文档」一环。

#### 4.4.3 源码精读

`doc/README.md` 的组件表把文档与源码成对列出，样例组件的两行如下：

[doc/README.md:46-46](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/README.md#L46) —— `[Moving average filter](files/psi_fix_mov_avg.md)` 指向文档页，`[psi_fix_mov_avg.vhd](../hdl/psi_fix_mov_avg.vhd)` 指向源码。

文档页本身是「由 VHDL 生成」的——头部直接给出源码与测试台链接：

[doc/files/psi_fix_mov_avg.md:8-9](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/files/psi_fix_mov_avg.md#L8-L9) —— `VHDL source` 与 `Testbench source` 两条链接，把文档、源码、测试台三方绑在一起。

而 `hdl2md.py` 的文件头说明它正是这类文档的生成器：

[scripts/hdl2md.py:1-6](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py#L1-L6) —— 「Script that generates one MD file table for an entity port of HDL file」，并注明只处理 RTL 实体（不处理 pkg 与 testbench）。

README 里被 `dependencies.py` 解析的依赖段（带特殊注释标记）：

[README.md:45-62](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/README.md#L45-L62) —— `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->` 与 `<!-- END OF PARSED SECTION -->` 之间的内容会被 `scripts/dependencies.py` 读取，用来检出 en_cl_fix/psi_common/psi_tb/PsiSim。

#### 4.4.4 代码实践

1. **实践目标**：验证文档是「由 VHDL 自动生成」的，并理解文档索引机制。
2. **操作步骤**：
   - 执行 `git ls-files scripts`，确认 CI/依赖/文档/重构四类脚本都在。
   - 打开 [scripts/hdl2md.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/scripts/hdl2md.py)，阅读其 `hdl2md()` 函数注释，确认它读 VHDL 实体、产出 Markdown。
   - 打开 [doc/README.md](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/doc/README.md)，数一下组件表里有多少行，是否与 `hdl/` 下实体数量大致吻合。
3. **需要观察的现象**：`doc/files/psi_fix_mov_avg.md` 里的 Generics 表（如 `in_fmt_g`、`taps_g`）与 `hdl/psi_fix_mov_avg.vhd` 实体的 generic 声明一致——这正是 `hdl2md` 解析出来的。
4. **预期结果**：理解「改 VHDL 实体 → 重跑 hdl2md → 文档自动更新」的链路；文档不是手写、而是生成产物。
5. 本实践为源码阅读型；若想实跑 `hdl2md`，需安装其依赖（如 `pandas`），具体命令「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`scripts/hdl2md.py` 注释里说它「Do not work for pkg & testbench」，这对文档目录结构意味着什么？

**答案**：意味着 `doc/files/` 下绝大多数 `.md` 都是组件实体的文档（由 hdl2md 生成），而包（`psi_fix_pkg`）和测试台没有自动生成的文档——包的说明（如 `doc/files/psi_fix_pkg.md`）需要手工维护或用其他方式生成。

**练习 2**：为什么 README 的「Dependencies (Library)」段要包裹在 `<!-- DO NOT CHANGE FORMAT ... -->` 注释里？

**答案**：因为 `scripts/dependencies.py` 会用固定格式去解析这一段，自动检出所需的外部库（en_cl_fix/psi_common/psi_tb/PsiSim）。注释是给人看的提醒：不要改格式，否则脚本解析失败。这是一个「文档即配置」的典型做法。

## 5. 综合实践

把本讲全部内容串起来，完成下面这份「目录职责思维导图 + 样例组件文件映射」。

**任务**：

1. **画一张目录职责思维导图**。以仓库根为中心，向下列出 `hdl`、`model`、`testbench`、`sim`、`scripts`、`doc`、`unittest`、`sigasi` 八个分支，每个分支旁用一句话写清职责，并在 `model` 下再分出 `snippets` 与 `matlab` 两个子节点。

2. **为 `psi_fix_mov_avg` 完成五件套文件映射表**。在思维导图之外，单独画一张表，把样例组件的每一件产出物定位到具体文件（含相对路径）：

   | 产出物 | 文件路径 |
   |:------|:---------|
   | VHDL 实现 | （自己填） |
   | Python 位真模型 | （自己填） |
   | VHDL 自检测试台 | （自己填） |
   | 协同仿真 preScript | （自己填） |
   | 组件文档 | （自己填） |

3. **加一条「注册线」**：在表里补一行，写出 `psi_fix_mov_avg_tb` 在 `sim/config.tcl` 中的哪几行被注册（源码列表、测试台列表、运行块三处分别给出行号）。

**参考答案**（建议先自己填再对照）：

| 产出物 | 文件路径 |
|:------|:---------|
| VHDL 实现 | `hdl/psi_fix_mov_avg.vhd` |
| Python 位真模型 | `model/psi_fix_mov_avg.py` |
| VHDL 自检测试台 | `testbench/psi_fix_mov_avg_tb/psi_fix_mov_avg_tb.vhd` |
| 协同仿真 preScript | `testbench/psi_fix_mov_avg_tb/Scripts/preScript.py` |
| 组件文档 | `doc/files/psi_fix_mov_avg.md` |
| config.tcl 注册 | 源码列表 L84、测试台列表 L135、运行块 L298–L304 |

完成这张图与表后，你已经在脑中建好了整个仓库的「索引」：今后看到任何一个 `psi_fix_*` 组件名，都能机械地推导出它在这五个目录里的全部对应文件。

## 6. 本讲小结

- 仓库顶层八个目录分工清晰：`hdl`（可综合 VHDL）、`model`（Python 位真模型 + 代码生成）、`testbench`（自检测试台 + preScript）、`sim`（回归脚本）、`scripts`（CI/依赖/文档/重构）、`doc`（文档）、`unittest`（Python 单测）、`sigasi`（IDE 配置）。
- 核心组织纪律是「一个 `.vhd` 文件对应一个实体/package」，加上「测试台子目录 = 组件名 + `_tb`」，让文件名即索引。
- 一个组件有五件套：VHDL 实现、Python 模型、VHDL 测试台、preScript 协同仿真脚本、文档，分布在四个目录里且命名同构。
- `preScript.py` 在编译前运行，跑 Python 模型生成 `Data/*.txt`，测试台读回比对，形成「Python → 文本 → VHDL」的协同仿真闭环。
- `model/snippets` 是代码生成模板，`model/matlab` 是让 MATLAB 复用 Python 模型的桥接函数——`model/` 不只是「再写一遍模型」。
- `scripts/hdl2md.py` 把 VHDL 实体自动生成 `doc/files/*.md`；README 的依赖段被 `scripts/dependencies.py` 解析——文档与依赖都是「生成/解析」产物，不是纯手写。

## 7. 下一步学习建议

本讲建立了「仓库地图」，但还没真正「跑」起来。建议按以下顺序继续：

1. **下一讲 [u1-l3 仿真与回归测试框架](u1-l3-simulation-regression.md)**：精读 `sim/run.tcl`、`sim/runGhdl.tcl` 与 PsiSim 的 `add_sources`/`create_tb_run`/`tb_run_add_pre_script` API，理解回归如何判定 `###ERROR###`，并实际跑一次回归（如果有仿真器）。
2. **再下一讲 [u1-l4 定点数格式与握手约定](u1-l4-fixpoint-format-handshaking.md)**：掌握 `[s,i,f]` 定点格式、位增长规则和 AXI-S 风格的 `vld`/`rdy` 握手——这是读懂任何 `hdl/*.vhd` 接口的前置知识。
3. 想提前熟悉「五件套闭环」的读者，可以直接读 [testbench/psi_fix_mov_avg_tb/Scripts/preScript.py](https://github.com/paulscherrerinstitute/psi_fix/blob/821049e0375cb950a74f0ccec4b4993d5d960899/testbench/psi_fix_mov_avg_tb/Scripts/preScript.py) 全文，它是全库最短、最典型的协同仿真脚本，第三单元 [u3-l2 测试台与协同仿真流程](u3-l2-testbench-cosimulation.md) 会逐行精读它。
