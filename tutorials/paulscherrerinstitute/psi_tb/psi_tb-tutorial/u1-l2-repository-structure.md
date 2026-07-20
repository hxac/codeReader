# 仓库结构与目录组织

## 1. 本讲目标

上一讲（u1-l1）我们已经知道 psi_tb 是一个「只服务于 testbench、不可综合」的 VHDL 工具库。本讲不再讲概念，而是带你看懂**这个仓库的物理布局**：

- 记住 `hdl/`、`testbench/`、`sim/`、`scripts/`、`doc/`、`sigasi/` 各自放什么。
- 理解 psi_tb 对 `psi_common`（VHDL 依赖）和 `PsiSim`（TCL 仿真框架）的两条依赖关系。
- 学会看懂 `sim/config.tcl` 中的 `add_sources ... -tag lib/src/tb` 三段式编译入口，并能据此定位每一个 package 的源文件。
- 动手画出「源码文件 → 编译 tag」的对照表，分清哪些文件来自 psi_common、哪些来自 psi_tb 自身。

学完本讲，你拿到仓库任意一个 `.vhd` 文件，都能立刻判断它属于哪一层、是否会被 CI 编译、编译顺序在它之前必须先编译谁。

## 2. 前置知识

本讲假设你已经读过 u1-l1，理解以下术语：

- **RTL / testbench**：RTL 是可综合、最终进芯片的逻辑；testbench 是只用于仿真、验证 RTL 是否正确的代码。psi_tb 只服务后者。
- **psi_common**：PSI 的另一个开源 VHDL 库，放的是**可综合**的通用电路代码。psi_tb 会复用其中的若干 package（如数学、数组、逻辑辅助）。
- **package**：VHDL 里用于集中存放常量、类型、函数、过程的「包」。psi_tb 采用「一个 package 一个 `.vhd` 文件」的约定。
- **PsiSim**：PSI 自研的一套 TCL 仿真框架，封装了 ModelSim / GHDL 的编译与运行流程，统一用 `init` / `compile_files` / `run_tb` 等命令操作。
- **tag（标签）**：PsiSim 里给一组源文件打的标记，用于按依赖顺序分批编译。本讲会反复出现 `lib`、`src`、`tb` 三个 tag。

如果你对「仿真器为什么必须按依赖顺序编译」还不清楚，记住一句话即可：**VHDL 要求被引用的 package 先于引用者编译**，所以 config.tcl 里源文件的排列顺序就是一条依赖链。

## 3. 本讲源码地图

| 文件 / 目录 | 作用 |
|---|---|
| `README.md` | 项目说明，含「What belongs / does not belong」边界划分与 Dependencies 章节 |
| `hdl/*.vhd` | 7 个 psi_tb package 的源码（文本工具、比较、活动、I2C、AXI、AXI 互转、文本文件） |
| `testbench/psi_tb_i2c_pkg_tb.vhd` | 唯一的示例 testbench，演示 I2C BFM 的用法 |
| `sim/config.tcl` | PsiSim 编译配置：声明库、消息抑制、三段 `add_sources`、注册 testbench 运行 |
| `sim/run.tcl` / `sim/runGhdl.tcl` | ModelSim / GHDL 的批处理运行脚本 |
| `sim/interactive.tcl` | ModelSim 交互式调试入口 |
| `sim/ci.do` | CI 用的 do 文件 |
| `scripts/ciFlow.py` / `scripts/dependencies.py` | CI 流程与依赖解析的 Python 脚本 |
| `doc/` | 文档（`psi_tb.pdf`、`psi_tb.docx`、`ToDo_NextMajorVersion.txt`） |
| `sigasi/` | Sigasi Studio（Eclipse 系）的工程映射文件 |
| `License.txt` / `LGPL2_1.txt` / `Changelog.md` | 许可证与版本记录 |

## 4. 核心概念与源码讲解

### 4.1 顶层目录布局总览与依赖关系

#### 4.1.1 概念说明

一个「库」仓库通常不是一堆散乱的文件，而是按**职责**分目录。psi_tb 把目录切得很干净：

- 放**源码**的（`hdl/`）、放**用法演示**的（`testbench/`）、放**怎么编译运行**的（`sim/`）三者分离；
- 放**自动化流水线**的（`scripts/`）、放**人读文档**的（`doc/`）、放**IDE 工程**的（`sigasi/`）各自独立。

这种分离的好处是：同一个 package 源码，既能被 PsiSim 编译跑 CI，也能被 Sigasi 打开做交互式开发，两条路径互不干扰。

#### 4.1.2 核心流程

整个仓库的目录与依赖可以画成下面这张图：

```
psi_tb/                         <-- 本仓库根目录
├── hdl/                        7 个 package 源码（库本体）
├── testbench/                  示例 testbench（怎么用这个库）
├── sim/                        PsiSim 脚本：怎么编译+运行
│   └── config.tcl              编译配置（本讲重点）
├── scripts/                    CI 用的 Python 脚本
├── doc/                        PDF / docx 文档
├── sigasi/                     Sigasi IDE 工程映射
├── README.md / Changelog.md / License.txt
│
依赖（仓库之外，但编译时必须存在）：
├── psi_common/   <-- VHDL 依赖，作为同级目录存在
└── TCL/PsiSim/   <-- TCL 仿真框架依赖
```

关键依赖关系（来自 README）有两类：

1. **VHDL 依赖**：`psi_common`（3.0.0 或更高）。
2. **TCL 依赖**：`PsiSim`（2.2.0 或更高），它本身不是 VHDL，而是一套脚本框架。

#### 4.1.3 源码精读

README 的 Dependencies 章节明确列出了这两条依赖，并强调「文件夹结构必须精确匹配」：

- [README.md:33-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L33-L42)：列出 TCL 依赖 PsiSim、VHDL 依赖 psi_common，并提示可用 `psi_fpga_all` 仓库一次性获取正确的目录结构（子模块形式）。

README 的「What belongs / does not belong」则划清了 psi_tb 与 psi_common 的职责边界（上一讲已详述，这里只定位）：

- [README.md:16-25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L16-L25)：本库只收 testbench 专用代码（BFM、检查函数、激励生成），且建议「一个 package 一个 `.vhd`」。
- [README.md:27-31](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L27-L31)：可综合代码、项目专用代码不属于本库（那部分归 psi_common）。

config.tcl 里的相对路径印证了「psi_common 作为同级目录」这一结构：

- [sim/config.tcl:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L8)：`set LibPath "../.."`。`config.tcl` 位于 `sim/`，往上两级正好是 psi_tb 的父目录，所以 `$LibPath/psi_common/...` 就指向「与 psi_tb 同级」的 psi_common 仓库。

#### 4.1.4 代码实践

**实践目标**：用本仓库的相对路径，反推出 PSI 期望的磁盘目录结构。

**操作步骤**：

1. 打开 `sim/config.tcl`，记下三处相对路径：`../hdl`、`../testbench`、`$LibPath`（即 `../..`）。
2. 打开 `sim/run.tcl` 第 8 行，记下 `source ../../../TCL/PsiSim/PsiSim.tcl`。
3. 在纸上（或文本里）画一棵目录树，把 `hdl/`、`testbench/`、`sim/` 放到 psi_tb 下，把 `psi_common/`、`TCL/PsiSim/` 放到 psi_tb 的同级或更上层，使上述相对路径都能成立。

**需要观察的现象**：你会发现 `../hdl` 与 `../testbench` 要求它们与 `sim/` 同在 psi_tb 根下（这与 `git ls-files` 看到的真实结构一致）；而 `$LibPath/psi_common` 要求 psi_common 与 psi_tb 同级。

**预期结果**：得到一棵类似下面的结构（PsiSim 的精确层级以 README 指向的 `psi_fpga_all` 为准，**待本地验证**）：

```
<workspace>/
├── psi_tb/        <- ../hdl, ../testbench 相对 sim/ 解析到这里
│   ├── hdl/  testbench/  sim/
├── psi_common/    <- $LibPath(../..)/psi_common 解析到这里
└── TCL/PsiSim/    <- run.tcl 中引用的仿真框架
```

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hdl/` 和 `testbench/` 必须是 psi_tb 根目录的直接子目录，而不能再嵌套一层？

**答案**：因为 `sim/config.tcl` 用 `../hdl` 和 `../testbench` 引用它们——这是相对于 `sim/` 向上一级。如果再嵌套一层，路径就解析不到，编译会找不到源文件。

**练习 2**：如果不想手动摆放 psi_common 和 PsiSim 的目录，README 给出了什么捷径？

**答案**：使用 `psi_fpga_all` 仓库，它以子模块形式包含了所有 FPGA 相关仓库并已摆好正确的目录结构（见 [README.md:35-37](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/README.md#L35-L37)）。

---

### 4.2 hdl/ 目录：7 个 package 清单与内部依赖

#### 4.2.1 概念说明

`hdl/` 是 psi_tb 的**库本体**，所有可被外部 testbench 引用的 package 都在这里。当前共有 7 个 `.vhd` 文件，每个文件恰好一个 package（遵循 README「一 package 一文件」的约定）。

这 7 个 package 并非彼此独立——它们之间有明确的 `use` 依赖。理解这条依赖链，是看懂 config.tcl 编译顺序的前提。

#### 4.2.2 核心流程

通过检索每个 package 的 `use work.*` 子句，可得到如下依赖关系（箭头表示「依赖 / 需要」）：

```
psi_tb_txt_util          （最底层：纯字符串/数值/文件工具）
   └─> psi_tb_compare_pkg       （依赖 txt_util 做消息拼接）
         └─> psi_tb_activity_pkg （依赖 compare + txt_util）
               └─> psi_tb_i2c_pkg （依赖 activity + compare + txt_util + psi_common_*）

psi_tb_axi_pkg           （依赖 compare + txt_util + psi_common_math）
   └─> psi_tb_axi_conv_pkg （依赖 axi_pkg + psi_common_axi）

psi_tb_textfile_pkg      （依赖 txt_util）
```

注意三个要点：

1. **`psi_tb_txt_util` 是公共底座**，几乎所有 package 都依赖它。
2. **`psi_common` 通过 `work` 库被引用**：例如 `use work.psi_common_math_pkg.all;`（见 [hdl/psi_tb_i2c_pkg.vhd:18-19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L18-L19)）。这说明 psi_common 的 package 被编译进了**同一个库**（`work`/`psi_tb`），而不是单独的库。
3. **`psi_tb_axi_conv_pkg` 故意独立**：它把 psi_tb 的 AXI 类型与 psi_common 的综合 AXI 类型互转。作者在文件头注释里说明了「拆成独立 package」的原因——避免所有想用 TB AXI 包的人都被迫连带引入综合 AXI 包（[hdl/psi_tb_axi_conv_pkg.vhd:7-12](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L7-L12)）。

#### 4.2.3 源码精读

7 个 package 的声明位置如下（每个文件只有一个 `package ... is`）：

| package | 声明位置 | 职责（一句话） |
|---|---|---|
| `psi_tb_txt_util` | [hdl/psi_tb_txt_util.vhd:52](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L52) | 字符串/数值互转、`print`、文件读写，全库消息底座 |
| `psi_tb_compare_pkg` | [hdl/psi_tb_compare_pkg.vhd:20](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L20) | 各类数值比较过程，统一输出 `###ERROR###` 消息 |
| `psi_tb_activity_pkg` | [hdl/psi_tb_activity_pkg.vhd:22](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_activity_pkg.vhd#L22) | 信号活动检查 + 时钟同步激励/等待 |
| `psi_tb_i2c_pkg` | [hdl/psi_tb_i2c_pkg.vhd:24](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L24) | I2C 总线功能模型（master/slave） |
| `psi_tb_axi_pkg` | [hdl/psi_tb_axi_pkg.vhd:22](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_pkg.vhd#L22) | AXI 总线功能模型（类型 + 单事务 + 突发） |
| `psi_tb_axi_conv_pkg` | [hdl/psi_tb_axi_conv_pkg.vhd:27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L27) | TB AXI ↔ 综合 AXI 类型互转 |
| `psi_tb_textfile_pkg` | [hdl/psi_tb_textfile_pkg.vhd:39](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L39) | 文本文件驱动的位真仿真（读写整数列文件） |

`textfile_pkg` 的文件头注释很好地说明了它的设计意图（按列存整数、空格分隔、不能逗号）：

- [hdl/psi_tb_textfile_pkg.vhd:7-11](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_textfile_pkg.vhd#L7-L11)：说明该包用于位真仿真，约定信号值以整数形式、每列一个信号、列间用空格分隔。

`axi_conv_pkg` 的文件头注释则解释了「为什么单独成包」：

- [hdl/psi_tb_axi_conv_pkg.vhd:7-12](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L7-L12)：在 testbench 友好的 AXI 包与综合友好的 AXI 包之间做转换；拆成独立包是为了不让 TB AXI 的使用者被迫引入综合 AXI 包。

#### 4.2.4 代码实践

**实践目标**：亲手验证 4.2.2 中的依赖链，而不是盲信讲义。

**操作步骤**：

1. 在仓库根目录执行检索（只读，不改任何源码），查找每个 package 对其它 `work` 包的引用：

   ```
   # 用 ripgrep（只读）
   rg -n "^\s*use work\." hdl/
   ```

2. 针对输出，把每行整理成 `引用者 -> 被引用者` 的列表。
3. 找出：哪个 package 被引用得最多？哪个 package 完全不依赖任何其它 psi_tb 包？

**需要观察的现象**：`psi_tb_txt_util` 应当被多个 package 引用；`psi_tb_axi_conv_pkg` 应当引用 `psi_tb_axi_pkg`；同时能看到对 `psi_common_*` 的引用（说明 psi_common 编译进了同一库）。

**预期结果**：与 4.2.2 的依赖图一致。若你的检索结果与讲义不符，以源码为准（讲义基于当前 HEAD `8ee9c06`）。

#### 4.2.5 小练习与答案

**练习 1**：如果只想用 I2C BFM，最少需要编译 psi_tb 的哪几个 package？

**答案**：需要 `psi_tb_txt_util`、`psi_tb_compare_pkg`、`psi_tb_activity_pkg`、`psi_tb_i2c_pkg` 这 4 个——因为 `i2c_pkg` 依赖前三个（见 [hdl/psi_tb_i2c_pkg.vhd:15-19](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_i2c_pkg.vhd#L15-L19)）。这也正是 config.tcl 里 `-tag src` 选中的那 4 个。

**练习 2**：为什么作者把 AXI 类型转换拆成 `psi_tb_axi_conv_pkg` 而不是放进 `psi_tb_axi_pkg`？

**答案**：转换包依赖 `psi_common_axi_pkg`（综合包）。如果合并，那么任何只想用 TB AXI 包的人都会被强制引入综合 AXI 包。拆开之后，不需要转换的人可以只引用 `psi_tb_axi_pkg`（[hdl/psi_tb_axi_conv_pkg.vhd:7-12](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_axi_conv_pkg.vhd#L7-L12)）。

---

### 4.3 sim/config.tcl：编译入口与 add_sources 的 lib/src/tb 分组

#### 4.3.1 概念说明

`sim/config.tcl` 是 PsiSim 的**编译配置中枢**。它不直接编译，而是「描述」要编译什么、按什么顺序、抑制哪些无关警告、跑哪个 testbench。真正的编译动作由 `run.tcl` / `runGhdl.tcl` 调用 PsiSim 命令完成。

config.tcl 用 `add_sources ... -tag <名字>` 把源文件分成若干组，每组一个 tag。tag 的作用有两点：

1. **表达依赖层级**：tag 的排列顺序就是编译顺序——先 `lib`（外部库）、再 `src`（本项目源码）、最后 `tb`（testbench）。
2. **支持选择编译**：可以用 tag 选择只编译某一层。

#### 4.3.2 核心流程

config.tcl 的逻辑可以拆成 5 步：

```
1. 设定库路径          set LibPath "../.."
2. 声明仿真库名        add_library psi_tb
3. 抑制无关消息        compile_suppress / run_suppress
4. 分三组添加源码：
     -tag lib   <-- psi_common 的 3 个 package（依赖底座）
     -tag src   <-- psi_tb 的 4 个 package（库本体）
     -tag tb    <-- testbench（顶层，依赖 src + lib）
5. 注册要运行的 TB     create_tb_run "..."  +  add_tb_run
```

这三组的顺序恰好是一条**拓扑依赖链**：`lib`（psi_common）→ `src`（psi_tb 包，引用 lib）→ `tb`（testbench，引用 src）。

> ⚠️ 一个重要事实：config.tcl 的 `-tag src` 列表里**只有 4 个** package（`txt_util`、`compare`、`activity`、`i2c`），并没有把 `hdl/` 里的全部 7 个都列进去。`psi_tb_axi_pkg`、`psi_tb_axi_conv_pkg`、`psi_tb_textfile_pkg` 这三个虽然存在于仓库中，却不在当前 config.tcl 的编译列表里。原因是当前唯一注册的 testbench 是 I2C 的，它只需要那 4 个包；AXI/textfile 包没有对应的注册 testbench 来驱动它们进入 CI 编译。这一点**待本地验证**：你跑 `run.tcl` 时可在 Transcript 里确认是否真的只编译了 4 个 psi_tb 包。

#### 4.3.3 源码精读

逐段对应 config.tcl：

- [sim/config.tcl:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L8)：`set LibPath "../.."`——设定 psi_common 所在的上级路径。
- [sim/config.tcl:14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L14)：`add_library psi_tb`——声明本次编译产出的 VHDL 库名为 `psi_tb`。
- [sim/config.tcl:17-18](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L17-L18)：`compile_suppress` / `run_suppress` 抑制一组编号的警告/信息，避免它们淹没真正的错误。
- [sim/config.tcl:21-25](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L21-L25)：`-tag lib`——添加 psi_common 的 3 个 package（`array_pkg`、`math_pkg`、`logic_pkg`），来自 `$LibPath/psi_common/hdl/`。
- [sim/config.tcl:28-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L28-L33)：`-tag src`——添加 psi_tb 的 4 个 package，来自 `../hdl`。注意只有 4 个（见上面的说明）。
- [sim/config.tcl:36-38](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L36-L38)：`-tag tb`——添加示例 testbench `psi_tb_i2c_pkg_tb.vhd`，来自 `../testbench`。
- [sim/config.tcl:41-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L41-L42)：`create_tb_run "psi_tb_i2c_pkg_tb"` + `add_tb_run`——注册一个 testbench 运行，告诉 PsiSim 跑哪个顶层。

参考 run.tcl 如何使用这份配置（它本身只有流程，不含文件清单）：

- [sim/run.tcl:14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L14)：`init`——初始化 ModelSim 仿真。
- [sim/run.tcl:17](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L17)：`source ./config.tcl`——载入上面的配置（注意是 run.tcl 主动 source config.tcl）。
- [sim/run.tcl:23](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L23)：`compile_files -all -clean`——按 config 描述全部（重新干净地）编译。
- [sim/run.tcl:27](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L27)：`run_tb -all`——运行所有注册的 testbench。
- [sim/run.tcl:32](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/run.tcl#L32)：`run_check_errors "###ERROR###"`——扫描 Transcript，若出现 `###ERROR###` 则判定失败。

GHDL 路径几乎一致，唯一区别是初始化命令：

- [sim/runGhdl.tcl:14](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/runGhdl.tcl#L14)：`init -ghdl`——改用 GHDL 作为仿真器，其余流程复用 config.tcl。

#### 4.3.4 代码实践（本讲的主实践任务）

**实践目标**：对照 config.tcl，画出「源码文件 → 来源 → 编译 tag」三列对照表，分清 psi_common 与 psi_tb。

**操作步骤**：

1. 打开 [sim/config.tcl:21-42](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L21-L42)，逐行把每个文件填入下表。
2. 对每个文件，判断它来自 `$LibPath/psi_common/...`（来源 = psi_common）还是 `../hdl`、`../testbench`（来源 = psi_tb）。
3. 标注它的 tag（`lib` / `src` / `tb`）。

**预期结果（参考答案）**：

| 源码文件 | 来源 | tag |
|---|---|---|
| `psi_common/hdl/psi_common_array_pkg.vhd` | psi_common | lib |
| `psi_common/hdl/psi_common_math_pkg.vhd` | psi_common | lib |
| `psi_common/hdl/psi_common_logic_pkg.vhd` | psi_common | lib |
| `hdl/psi_tb_txt_util.vhd` | psi_tb | src |
| `hdl/psi_tb_compare_pkg.vhd` | psi_tb | src |
| `hdl/psi_tb_activity_pkg.vhd` | psi_tb | src |
| `hdl/psi_tb_i2c_pkg.vhd` | psi_tb | src |
| `testbench/psi_tb_i2c_pkg_tb.vhd` | psi_tb | tb |

**需要观察的现象**：表中没有任何一个 `psi_tb_axi_*` 或 `psi_tb_textfile_*` 文件——这印证了 4.3.2 的结论：它们不在当前 CI 编译列表中。

> 提示：完整的「怎么在本地真正跑一遍 config.tcl」会在下一讲 u1-l3（仿真环境与 CI 构建流程）展开。本讲只要求你**看懂并填出**这张表。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `hdl/psi_tb_txt_util.vhd` 从 `-tag src` 列表里删掉，编译会出什么错？为什么？

**答案**：会报「找不到 `psi_tb_txt_util`」之类的错误，因为 `compare_pkg`、`activity_pkg`、`i2c_pkg` 都 `use work.psi_tb_txt_util.all;`（[hdl/psi_tb_compare_pkg.vhd:15](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_compare_pkg.vhd#L15)）。VHDL 要求被引用者先编译。

**练习 2**：`-tag tb` 的文件为什么必须排在 `-tag src` 之后？

**答案**：testbench `use work.psi_tb_i2c_pkg.all;`（[testbench/psi_tb_i2c_pkg_tb.vhd:15](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L15)），它依赖 `src` 里的 package，所以 `src` 必须先编译。tag 顺序 = 编译顺序 = 依赖顺序。

---

### 4.4 testbench/ 目录与 Sigasi 工程映射

#### 4.4.1 概念说明

`testbench/` 放的是**如何使用** psi_tb 的示例。目前只有一个文件：`psi_tb_i2c_pkg_tb.vhd`，它用一个完整的 I2C 仿真演示了库的用法（master/slave 两个并发进程对拍）。这个文件既是文档，也是 CI 实际运行的唯一 testbench。

`sigasi/` 目录则是给 **Sigasi Studio**（一款基于 Eclipse 的 VHDL IDE）用的工程映射。它不含 VHDL 源码，而是用「链接（linked resources）」把仓库里的 `hdl/`、外部的 `psi_common/` 引入工程，并指定库映射。

#### 4.4.2 核心流程

testbench 与库的关系很简单：

```
testbench/psi_tb_i2c_pkg_tb.vhd
   use work.psi_tb_i2c_pkg.all;   <-- 来自 src
   use work.psi_tb_txt_util.all;  <-- 来自 src
   实例化 I2cPullup、调用 I2cMaster*/I2cSlave* 过程
```

Sigasi 的映射逻辑：

```
sigasi/.project            把 hdl/ 和 psi_common/ 作为 linked resource 引入
sigasi/.library_mapping.xml 把 psi_common/ 也映射到 work 库（与 config.tcl 一致）
```

也就是说，Sigasi 工程与 PsiSim 的 config.tcl 在「psi_common 编译进 work 库」这一点上达成一致，二者只是不同入口的同一套结构。

#### 4.4.3 源码精读

示例 testbench 的开头：声明库、引用 psi_tb 包、定义空实体：

- [testbench/psi_tb_i2c_pkg_tb.vhd:14-16](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L14-L16)：`use work.psi_tb_i2c_pkg.all;` 与 `use work.psi_tb_txt_util.all;`——只引用了这两个包（注意它没有直接 `use` compare/activity，那是通过 i2c_pkg 间接依赖）。
- [testbench/psi_tb_i2c_pkg_tb.vhd:21-22](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L21-L22)：`entity psi_tb_i2c_pkg_tb is ... end entity;`——空实体（无端口），这是 testbench 的常见写法。
- [testbench/psi_tb_i2c_pkg_tb.vhd:27-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L27-L33)：声明 `scl`/`sda` 两个 `inout` 信号并调用 `I2cPullup(scl, sda)`——这就是 config.tcl 里 `-tag tb` 注册、`create_tb_run` 指向的那个顶层。

Sigasi 工程把 `hdl/` 与 `psi_common/` 引入工程：

- [sigasi/.project:24-28](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sigasi/.project#L24-L28)：把 `hdl/` 作为 linked resource（`PARENT-1-PROJECT_LOC/hdl`，即 sigasi 的上一级就是仓库根）。
- [sigasi/.project:49-53](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sigasi/.project#L49-L53)：把 `psi_common/` 也作为 linked resource（`PARENT-2-PROJECT_LOC/psi_common`，即 sigasi 上两级、与 psi_tb 同级）——与 config.tcl 的 `$LibPath` 结构完全吻合。
- [sigasi/.library_mapping.xml:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sigasi/.library_mapping.xml#L8)：`Common Libraries/psi_common` 映射到 `work` 库——这解释了为什么源码里能写 `use work.psi_common_math_pkg.all;`。

#### 4.4.4 代码实践

**实践目标**：确认「Sigasi 映射」与「PsiSim config.tcl」描述的是同一套目录结构。

**操作步骤**：

1. 对比 [sigasi/.project:49-53](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sigasi/.project#L49-L53)（psi_common 的位置）与 [sim/config.tcl:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sim/config.tcl#L8)（`LibPath`）。
2. 对比 [sigasi/.library_mapping.xml:8](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/sigasi/.library_mapping.xml#L8)（psi_common → work）与源码里的 `use work.psi_common_*` 写法。

**需要观察的现象**：两处对 psi_common 位置的描述（与 psi_tb 同级）一致；库映射也一致（psi_common 进 work 库）。

**预期结果**：能用自己的话总结——「无论用 Sigasi 还是 PsiSim，psi_common 都被当作 work 库里的一部分来引用」。完整跑通 Sigasi 工程需要本地安装 Sigasi Studio，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`psi_tb_i2c_pkg_tb` 这个 testbench 的实体为什么没有端口？

**答案**：testbench 是仿真顶层，不被其它模块例化，所以不需要端口；它内部的信号（`scl`/`sda`）自己驱动自己（master 进程与 slave 进程互拍），见 [testbench/psi_tb_i2c_pkg_tb.vhd:21-33](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/testbench/psi_tb_i2c_pkg_tb.vhd#L21-L33)。

**练习 2**：`sigasi/.project` 里 `hdl` 的位置是 `PARENT-1-PROJECT_LOC/hdl`，为什么是「上一级」？

**答案**：Sigasi 工程文件位于 `sigasi/` 子目录内（工程根 = `sigasi/`），而 `hdl/` 在仓库根。所以从 `sigasi/` 向上一级（`PARENT-1-PROJECT_LOC`）才到仓库根，再进 `hdl/`。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这张「仓库结构全景表」。要求你**只读**源码，不修改任何文件：

1. 列出仓库根下的所有顶层目录与文件（可用 `git ls-files` 辅助）。
2. 对 `hdl/` 下的 7 个 package，标注：① 是否出现在 config.tcl 的 `-tag src` 列表里；② 它依赖哪些其它 psi_tb 包。
3. 对 config.tcl 的三段 `add_sources`，分别写出：tag 名、来源目录、文件数量、所属仓库（psi_common / psi_tb）。
4. 用一句话回答：为什么 `-tag lib` 必须排在 `-tag src` 之前，`-tag src` 又必须排在 `-tag tb` 之前？

完成后，你应该能合上讲义，凭这张表向别人解释「psi_tb 这个仓库每个目录放什么、编译时按什么顺序、依赖谁」。

> 提示：第 4 问的答案藏在 4.2 的依赖链里——testbench 依赖 src 的包，src 的包又依赖 lib 的 psi_common，而 VHDL 要求被依赖者先编译。

## 6. 本讲小结

- psi_tb 的目录按职责清晰分离：`hdl/`（库本体）、`testbench/`（用法示例）、`sim/`（PsiSim 脚本）、`scripts/`（CI）、`doc/`（文档）、`sigasi/`（IDE 映射）。
- `hdl/` 下共 7 个 package，`psi_tb_txt_util` 是公共底座，`compare` / `activity` / `i2c` 依次依赖它；`axi` / `axi_conv` / `textfile` 是另一条依赖支线。
- `sim/config.tcl` 用 `add_sources ... -tag lib/src/tb` 把源文件分成三段，tag 顺序就是编译顺序，对应一条拓扑依赖链。
- config.tcl 当前只把 4 个 psi_tb 包（txt_util/compare/activity/i2c）纳入编译——因为唯一注册的 testbench 是 I2C 的；AXI 与 textfile 包暂不在 CI 编译列表中。
- psi_common 被编译进同一个 `work`/`psi_tb` 库（故源码里写 `use work.psi_common_*`），它作为 psi_tb 的同级目录存在；PsiSim 是 TCL 仿真框架依赖。
- Sigasi 工程映射与 PsiSim 的 config.tcl 描述的是同一套目录结构，只是入口不同。

## 7. 下一步学习建议

本讲你看懂了「仓库长什么样、编译怎么配置」，但还没真正跑过一次仿真。建议接下来学习：

- **u1-l3 仿真环境与 CI 构建流程**：动手用 `sim/run.tcl`（ModelSim）或 `sim/runGhdl.tcl`（GHDL）跑通 `psi_tb_i2c_pkg_tb`，看 `init` / `compile_files` / `run_tb` / `run_check_errors` 如何串成一条流水线，并理解 `###ERROR###` 与 CI 判定的关系。
- 之后进入第二单元（u2）开始读 `psi_tb_txt_util` 的源码——它是整个库的底座，也是后续 compare / activity / BFM 的共同依赖。

如果你想在动手仿真前先熟悉某个具体 package，可以从 `hdl/psi_tb_txt_util.vhd` 的 package 声明（[第 52 行](https://github.com/paulscherrerinstitute/psi_tb/blob/8ee9c066e4a87b65865e184a966002e818dc7f65/hdl/psi_tb_txt_util.vhd#L52)）开始浏览，但精读留给 u2。
