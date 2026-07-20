# 如何运行仿真与 CI 流程

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚这个 IP 核**一次完整仿真**从头到尾要走哪些脚本、按什么顺序执行。
- 读懂 `sim/config.tcl`，理解它如何用 `-tag lib/src/tb` 把源文件分成「第三方库 / 项目源码 / 测试台」三组。
- 区分 Modelsim 回归仿真与 GHDL 仿真两种入口的差别。
- 解释 CI（`scripts/ciFlow.py` + `sim/ci.do`）是如何通过 transcript 里的两个关键字串 `###ERROR###` 与 `SIMULATIONS COMPLETED SUCCESSFULLY` 来判定一次仿真「通过 / 失败」的。

本讲只讲「怎么跑、怎么判定结果」，不深入 RTL 内部逻辑——那是第二单元的事。我们承接 [u1-l2 仓库目录结构速览](u1-l2-repo-structure.md) 已经建立的 `hdl`/`tb`/`sim` 三件套认知。

## 2. 前置知识

在开始之前，先建立几个直觉概念。它们都很简单，但对理解后面的脚本至关重要。

### 2.1 什么是「回归仿真（regression simulation）」

写完一段硬件代码后，我们需要证明它「在所有典型场景下都按预期工作」。「回归仿真」就是把**多个测试用例**一次性全部跑一遍，任何一个失败就算整体失败。这能保证你改了代码之后，以前能跑通的场景仍然能跑通——也就是「没有回归（没有把旧功能改坏）」。

本项目只有**一个**测试台文件 `tb/top_tb.vhd`，但它在内部用 6 组用例（单次读、缓冲双读、超时、禁用、背压、单寄存器四次读）覆盖了 IP 的主要行为，并且用两个不同的 generic `OutputType_g=AXIS` / `AXIMM` 跑两遍，所以一次回归仿真实际上验证了 `6 × 2 = 12` 个场景。

### 2.2 什么是 transcript

Modelsim / GHDL 这类仿真器在运行时会把所有输出（编译信息、`puts` 打印、`assert` 报错、仿真日志）写到一个文本文件里。本项目的脚本约定这个文件叫 **`Transcript.transcript`**（注意首字母大写）。CI 流程最后的「成功还是失败」就是靠扫描这个文本里的关键字串来判定的。`sim/.gitignore` 也特意把它排除在版本控制之外，因为它是本地产物。

### 2.3 什么是 PsiSim

[PsiSim](https://github.com/paulscherrerinstitute/PsiSim) 是 PSI 自己写的一个 **TCL 仿真框架**，它把「建库、加源文件、编译、跑测试台、检查错误」这些重复性工作封装成一组 `psi::sim::*` 命令。这样不同 IP 的仿真脚本就能长得几乎一样，降低维护成本。本项目的 `sim/config.tcl`、`sim/run.tcl` 都是基于 PsiSim 写的。

> 依赖版本：README 指明需要 PsiSim **2.4.0 或更高**（仅开发时需要）。

### 2.4 脚本之间的调用关系（先记住这张图）

下面这张「谁调用谁」的关系图是本讲的核心心智模型，建议先记住：

```
scripts/ciFlow.py        (Python，CI 入口)
        │  os.chdir 到 sim/
        │  os.system("vsim -c -do ci.do")
        ▼
sim/ci.do                (Modelsim 批处理 do 文件)
        │  source run.tcl
        │  quit
        ▼
sim/run.tcl              (Modelsim 回归仿真主脚本)
        │  source PsiSim.tcl   → 加载框架
        │  psi::sim::init
        │  source ./config.tcl → 声明库/源文件/测试台/运行参数
        │  psi::sim::compile -all -clean
        │  psi::sim::run_tb -all
        │  psi::sim::run_check_errors "###ERROR###"
        │
        ├── (人工) 在 Modelsim 里直接: source ./run.tcl
        └── (GHDL)   改用 sim/runGhdl.tcl: 仅 init 加 -ghdl
```

人工本地跑仿真时，我们直接从 `run.tcl`（Modelsim）或 `runGhdl.tcl`（GHDL）切入；CI 跑时则多套了两层外壳 `ci.do` + `ciFlow.py`，目的是「无界面、跑完自动判通过/失败并返回退出码」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `sim/config.tcl` | 仿真配置：声明库、按 tag 分组源文件、定义测试台运行参数 | `-tag lib/src/tb` 分组、两个 `OutputType_g` 运行 |
| `sim/run.tcl` | Modelsim 回归仿真主流程：加载框架→init→配置→编译→运行→查错 | `psi::sim::*` 命令序列 |
| `sim/runGhdl.tcl` | GHDL 版主流程，与 `run.tcl` 几乎相同 | 唯一差别 `init -ghdl` |
| `sim/ci.do` | Modelsim 批处理入口：`source run.tcl` 后 `quit` | 把交互式流程变成批处理 |
| `sim/interactive.tcl` / `sim/interactiveGhdl.tcl` | 交互式调试脚本：只编译不自动跑，留给你手动调试 | 调试用，非 CI 路径 |
| `scripts/ciFlow.py` | CI 总控：调起 vsim、读 transcript、判定退出码 | 两个关键字串判据 |
| `README.md` | 依赖声明与运行说明 | 依赖版本、`source ./run.tcl` 指引 |

## 4. 核心概念与源码讲解

### 4.1 PsiSim config：用 tag 把源文件分组

#### 4.1.1 概念说明

一个仿真要跑起来，仿真器必须知道「编译哪些文件、按什么顺序、哪些属于第三方库、哪些属于本项目、哪个是测试台」。`sim/config.tcl` 就是干这件事的。

PsiSim 用一个 `-tag` 参数给每一组源文件打标签。本项目用了三个约定俗成的标签：

- **`lib`**：第三方/公共库源文件（`psi_common`、`psi_tb`）。
- **`src`**：本项目自己的 RTL 源码（核心 + wrapper + 包）。
- **`tb`**：测试台。

打标签的好处是：框架知道「库要先编译、源码依赖库、测试台依赖源码」，从而自动算出正确的编译顺序，也方便后续按 tag 做增量编译或过滤。

#### 4.1.2 核心流程

`config.tcl` 的执行流程可以概括为五步：

1. 设定库根路径并导入 PsiSim 命名空间。
2. 创建一个名为 `axi_mm_reader` 的仿真库。
3. 配置编译期/运行期要抑制的多余告警（让日志干净）。
4. 用三次 `add_sources` 分别加入 `lib`/`src`/`tb` 三组文件。
5. 定义「测试台运行」：声明测试台顶层 + 两个 generic 取值，让同一测试台跑两遍。

#### 4.1.3 源码精读

先看路径与命名空间导入。`LibPath` 指向仓库根的上两级（`sim/` → 仓库根 → 再上一级放 `VHDL/`、`TCL/` 依赖目录），这是 PSI 标准目录约定：

[sim/config.tcl:8-13](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L8-L13) — 定义 `LibPath` 常量、导入 `psi::sim::*` 全部命令，并创建仿真库 `axi_mm_reader`。

接着是抑制告警。`135,1236` 等数字是 Modelsim 的 message ID，屏蔽它们只是为了让 transcript 不被无关警告刷屏，不影响功能：

[sim/config.tcl:15-17](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L15-L17) — `compile_suppress` 抑制编译期告警，`run_suppress` 抑制运行期告警。

第一组源文件是 `psi_common` 库（含本项目真正用到的 AXI 从机/主机 IPIC、同步 FIFO、双口 RAM 等），打 `lib` 标签：

[sim/config.tcl:19-30](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L19-L30) — 加入 `psi_common` 的 9 个 `.vhd`，`-tag lib`。

第二组是 `psi_tb` 测试辅助库（文本工具、比较、AXI BFM、活动检测），同样 `lib`：

[sim/config.tcl:32-38](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L32-L38) — 加入 `psi_tb` 的 4 个包，`-tag lib`。

第三组是本项目源码（定义包、核心、wrapper），打 `src`：

[sim/config.tcl:40-45](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L40-L45) — 加入 `definitions_pkg.vhd`、`axi_mm_reader.vhd`、`axi_mm_reader_wrp.vhd`，`-tag src`。

第四组是测试台，打 `tb`：

[sim/config.tcl:47-50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L47-L50) — 加入 `top_tb.vhd`，`-tag tb`。

最后定义「测试台运行」。`create_tb_run "top_tb"` 声明顶层测试台，`tb_run_add_arguments` 给出**两个** generic 取值，PsiSim 据此自动生成两次独立的仿真运行（一次 `OutputType_g=AXIS`，一次 `OutputType_g=AXIMM`），最后 `add_tb_run` 提交这次运行定义：

[sim/config.tcl:52-55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L52-L55) — 用两个 `OutputType_g` 取值让同一测试台跑两遍，覆盖 AXIS 与 AXIMM 两种输出模式。

> 这里的设计很巧妙：**一份测试台代码、两种输出模式**，靠 generic 参数复用，避免维护两份几乎相同的测试台。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `config.tcl`，验证「三组 tag 与四个依赖库」的对应关系，并理解新增一个测试台文件时要改哪里。

**操作步骤**：

1. 打开 [sim/config.tcl](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl)。
2. 数一下 `-tag lib` 一共出现了几次（答案：2 次，分别对应 `psi_common` 与 `psi_tb`）。
3. 假设你要新增一个测试台文件 `tb/top_tb_extra.vhd`，找出需要在 `config.tcl` 里修改的位置：在第 48–50 行的 `add_sources "../tb"` 块里，把 `top_tb_extra.vhd \` 追加到文件列表中（注意原有最后一项 `top_tb.vhd` 后面要补上续行符 `\`）。

**需要观察的现象**：`add_sources` 接收的第一个参数是**相对于 `config.tcl` 所在目录（即 `sim/`）的路径**，所以项目源码写 `"../hdl"`、测试台写 `"../tb"`，而依赖库写 `"$LibPath/VHDL/..."`。

**预期结果**：你能用一句话说出「`psi_common` 在 `lib` 组、本项目 RTL 在 `src` 组、`top_tb` 在 `tb` 组」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `definitions_pkg.vhd` 误放到 `-tag lib` 组里，仿真还能通过吗？为什么？

**参考答案**：通常仍能编译通过（标签只影响分组与顺序策略，不阻止编译），但它会被当成「第三方库」对待，后续按 tag 过滤或做依赖分析时会出错位；正确做法是项目源码一律放 `src` 组。

**练习 2**：`create_tb_run` 之后为什么要调用 `add_tb_run`？

**参考答案**：`create_tb_run` 只是「开始定义一次运行」（设置顶层、累积 arguments），`add_tb_run` 才把这次运行「提交」到 PsiSim 的运行列表里。两者配对使用，便于在中间用 `tb_run_add_arguments` 添加多组 generic。

---

### 4.2 run.tcl 流程：编译、运行、查错三段式

#### 4.2.1 概念说明

`config.tcl` 只负责「声明」，真正「动手干」的是 `run.tcl`。它把一次回归仿真分成清晰的三段：**编译（Compile）→ 运行（Run）→ 检查（Check）**。每段之间用 `puts` 打印分隔线，让 transcript 一眼可读。

Modelsim 版（`run.tcl`）和 GHDL 版（`runGhdl.tcl`）几乎完全一样，唯一的差别是初始化时是否带 `-ghdl` 参数。

#### 4.2.2 核心流程

```
1. source PsiSim.tcl          # 加载 PsiSim 框架（路径 ../../../TCL/PsiSim/PsiSim.tcl）
2. psi::sim::init             # 初始化仿真环境（Modelsim 默认）
3. source ./config.tcl        # 执行配置（库/源文件/测试台/运行）
4. psi::sim::compile -all -clean   # 全量清理后重新编译所有 tag 的文件
5. psi::sim::run_tb -all      # 跑 config.tcl 里声明的全部测试台运行（这里是 2 次）
6. psi::sim::run_check_errors "###ERROR###"  # 扫描 transcript，发现该串则报错
```

`compile -all -clean` 中的 `-clean` 表示先清掉旧产物再编译，保证「干净构建」。`run_tb -all` 会把 `config.tcl` 里用 `create_tb_run` / `add_tb_run` 定义的所有运行（本项目 2 个）依次跑完。

#### 4.2.3 源码精读

`run.tcl` 开头加载 PsiSim 框架。注意路径是相对于 `sim/` 目录的 `../../../TCL/PsiSim/PsiSim.tcl`，对应 PSI 标准布局里把所有 TCL 框架集中放在仓库族根的 `TCL/` 下：

[sim/run.tcl:7-11](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/run.tcl#L7-L11) — 加载 PsiSim 框架并 `init` 初始化（Modelsim 模式，不带参数）。

随后 `source ./config.tcl` 执行上一节讲过的配置：

[sim/run.tcl:13-14](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/run.tcl#L13-L14) — 引入配置。

然后是三段式主体，每段前有分隔线 `puts`：

[sim/run.tcl:16-29](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/run.tcl#L16-L29) — 依次 `compile -all -clean`、`run_tb -all`、`run_check_errors "###ERROR###"`。

最后一行是关键：

[sim/run.tcl:29](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/run.tcl#L29) — `run_check_errors "###ERROR###"`：让 PsiSim 扫描 transcript，只要出现 `###ERROR###` 这个标记就认为有断言失败并抛错。

> 这个 `###ERROR###` 标记从哪来？它来自 `psi_tb` 比较辅助库。测试台 `top_tb.vhd` 里大量调用 `StdlvCompareInt`、`StdlCompare`、`axi_single_expect` 等比较过程（见 [tb/top_tb.vhd:23](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L23) 引入的 `psi_tb_compare_pkg`），当实际值与期望值不符时，这些过程就会向 transcript 打印包含 `###ERROR###` 的错误行。也就是说：**测试台代码本身并不字面写出 `###ERROR###`，是 psi_tb 框架在断言失败时代它打印的**（这部分属于外部依赖 PsiSim/psi_tb 的行为）。

GHDL 版的唯一差别在初始化：

[sim/runGhdl.tcl:10-11](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/runGhdl.tcl#L10-L11) — `psi::sim::init -ghdl`，把后端切到 GHDL；其余 compile/run/check 完全相同。

> README 里也明确给了两条本地运行命令——在 `sim/` 目录下，Modelsim 用 `source ./run.tcl`，GHDL 用 `source ./runGhdl.tcl`：见 [README.md:45-57](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L45-L57)。

另外还有两个**交互式**脚本 `sim/interactive.tcl` 与 `sim/interactiveGhdl.tcl`，它们只做到「加载框架 → init → source config → 编译」就停下来，不自动 `run_tb`，目的是让你在 Modelsim/GHDL 的 GUI 或命令行里手动跑、加波形、单步调试：

[sim/interactive.tcl:14-19](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/interactive.tcl#L14-L19) — `init` 后 `compile_files -all -clean`，到此为止，等待你手动操作。

#### 4.2.4 代码实践

**实践目标**：本地亲自跑一次 Modelsim 回归仿真（如果没有 Modelsim，则改为「源码阅读型实践」，跟踪三段式流程）。

**操作步骤（有 Modelsim 时）**：

1. 按 [README.md:17-32](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L17-L32) 的依赖结构，把 `TCL/PsiSim`、`VHDL/psi_common`、`VHDL/psi_tb` 摆到正确相对位置（或用 `psi_fpga_all` / `scripts/dependencies.py` 一键拉取）。
2. 启动 Modelsim，把工作目录切到仓库的 `sim/` 目录。
3. 在 Modelsim 命令行执行：

   ```tcl
   source ./run.tcl
   ```

4. 观察 transcript 里依次出现 `-- Compile`、`-- Run`、`-- Check` 三段分隔线。

**需要观察的现象**：`-- Run` 段会跑两次测试台（`OutputType_g=AXIS` 和 `AXIMM` 各一次）；如果全部通过，`-- Check` 段不会报 `###ERROR###`，脚本正常结束。

**预期结果**：transcript 末尾出现 PsiSim 框架打印的「SIMULATIONS COMPLETED SUCCESSFULLY」字样（该字样由 PsiSim 在所有运行成功完成后输出）。

> 若本机没有 Modelsim/GHDL 环境，**运行结果待本地验证**。此时请改做源码阅读实践：对照 4.2.2 的流程图，逐行指出 `run.tcl` 第 8、11、14、20、24、29 行分别对应流程的哪一步。

#### 4.2.5 小练习与答案

**练习 1**：`run.tcl` 和 `runGhdl.tcl` 内容几乎一样，为什么不合并成一个文件？

**参考答案**：两者唯一差别是 `init` 是否带 `-ghdl`。分开成两个文件是为了让用户直接 `source` 对应入口即可，不用记参数；也方便 CI 或文档明确引用某一个。这是「显式优于简洁」的取舍。

**练习 2**：如果某个用例失败，`run_check_errors` 之后脚本还会继续吗？

**参考答案**：`run_check_errors "###ERROR###"` 在扫描到错误标记时会抛出 TCL 错误，导致 `run.tcl` 中止（后续命令不再执行）。这正是我们想要的「一例失败即整体失败」。

---

### 4.3 ci.do 与 ciFlow.py：用 transcript 判定通过/失败

#### 4.3.1 概念说明

本地跑仿真时，人看着 transcript 就知道过没过。但在 CI（持续集成）里，机器必须用一个**确定的退出码（exit code）**来表达结果：`0` 表示成功，非 `0` 表示失败。`scripts/ciFlow.py` 就是把「人读 transcript」这件事自动化成「Python 读 transcript + 返回退出码」。

中间还隔了一层 `sim/ci.do`：因为 CI 是用命令行模式的 Modelsim（`vsim -c`）跑的，需要一个 `.do` 文件告诉它「跑完 `run.tcl` 就退出」。

#### 4.3.2 核心流程

CI 判定的完整链路如下：

```
ciFlow.py
  ├── os.chdir(THIS_DIR + "/../sim")          # 切到 sim 目录
  ├── os.system("vsim -c -do ci.do")          # 命令行模式启动 Modelsim，执行 ci.do
  │        └── ci.do:  source run.tcl ; quit  # 跑回归仿真后立即退出 vsim
  ├── 读取 sim/Transcript.transcript          # 拿到全部输出文本
  ├── if "###ERROR###" in content:  exit(-1)  # 判据 A：有断言失败 → 失败
  ├── if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content: exit(-2)  # 判据 B：没跑完 → 失败
  └── exit(0)                                 # 两个判据都过 → 成功
```

两个判据是**互补**的，覆盖两种不同的失败方式：

| 判据 | 检查的内容 | 命中时的含义 | 退出码 |
| --- | --- | --- | --- |
| A. `###ERROR###` **存在** | 测试台里有断言失败（期望值 ≠ 实际值） | 功能错误：IP 行为不对 | `-1` |
| B. `SIMULATIONS COMPLETED SUCCESSFULLY` **不存在** | PsiSim 没有打印「全部运行成功完成」 | 流程错误：仿真崩溃、挂死、编译失败等导致没能跑完所有运行 | `-2` |

简单记忆：

- **判据 A 抓「跑完了但结果错」**。
- **判据 B 抓「根本没跑完」**（比如编译报错、仿真 hang 住、脚本异常中断，这时 transcript 里既不会有成功标记，可能也没有 `###ERROR###`，必须靠 B 兜底）。

两个判据的顺序也很重要：**先查 A 再查 B**。因为「功能错误（A）」是更具体的信息，应当优先返回 `-1`；只有当没有功能错误、却又没看到成功标记时，才返回 `-2` 表示「流程异常」。

#### 4.3.3 源码精读

`ci.do` 极其简短——它只是 `run.tcl` 的批处理外壳，跑完就 `quit` 退出 Modelsim，把控制权交回 Python：

[sim/ci.do:7-8](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/ci.do#L7-L8) — `source run.tcl` 触发完整回归仿真，`quit` 关闭 vsim。

`ciFlow.py` 先定位自身目录、切到 `sim/`，再用 `vsim -c -do ci.do` 以**无界面命令行模式**启动 Modelsim：

[scripts/ciFlow.py:7-13](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L7-L13) — `os.chdir` 到 `sim/`，`os.system` 调起 `vsim -c -do ci.do`。

然后读取 transcript 文件（注意文件名是 `Transcript.transcript`，与 `sim/.gitignore` 里 `*.transcript` 的忽略规则一致）：

[scripts/ciFlow.py:15-16](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L15-L16) — 打开并读取 `Transcript.transcript` 全文。

判据 A——发现功能错误立即返回 `-1`：

[scripts/ciFlow.py:18-20](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L18-L20) — 若 transcript 含 `###ERROR###`，`exit(-1)`。

判据 B——没看到成功标记则返回 `-2`：

[scripts/ciFlow.py:21-23](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L21-L23) — 若不含 `SIMULATIONS COMPLETED SUCCESSFULLY`，`exit(-2)`。

两个判据都通过，返回 `0` 表示成功：

[scripts/ciFlow.py:25-27](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L25-L27) — `exit(0)`。

> 关于 `SIMULATIONS COMPLETED SUCCESSFULLY`：这个字样同样**不是** `top_tb.vhd` 或本项目脚本里写出来的——它由 PsiSim 框架在 `run_tb -all` 把所有声明的运行（本项目 2 个）都成功跑完后自动打印。所以它是「流程跑到底且无中断」的信号。这部分行为属于外部依赖 PsiSim，具体打印位置待确认（不在本仓库源码内）。

#### 4.3.4 代码实践

**实践目标**：在不依赖真实 Modelsim 的前提下，亲手验证 `ciFlow.py` 的判定逻辑——构造三种 transcript，观察退出码。

**操作步骤**（纯 Python，不需要仿真器）：

1. 在 `sim/` 目录下临时创建一个假的 `Transcript.transcript`，分别写入三种内容做实验（**示例代码，非项目原有脚本**）：

   ```python
   # 示例代码：模拟 ciFlow.py 的判定逻辑（仅供理解，不要写入仓库）
   def judge(content: str) -> int:
       if "###ERROR###" in content:
           return -1
       if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
           return -2
       return 0

   print(judge("... ###ERROR### Data mismatch ..."))                 # 期望 -1
   print(judge("... (仿真中途崩溃，没有任何标记) ..."))              # 期望 -2
   print(judge("... SIMULATIONS COMPLETED SUCCESSFULLY ..."))        # 期望 0
   ```

2. 对照 [scripts/ciFlow.py:18-27](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/scripts/ciFlow.py#L18-L27)，确认你的 `judge` 与仓库里的判定顺序一致（先 A 后 B）。

**需要观察的现象**：第一种输入返回 `-1`（功能错误优先），第二种返回 `-2`（没跑完），第三种返回 `0`（成功）。

**预期结果**：三种退出码分别是 `-1`、`-2`、`0`，与 `ciFlow.py` 的语义完全吻合。

> 真正在 CI 里跑（带 Modelsim）的结果**待本地验证**，因为你需要完整的依赖目录与 `vsim -c` 环境。

#### 4.3.5 小练习与答案

**练习 1**：为什么判据 B 要写成「`not in content` 才失败」，而不是像判据 A 那样「`in content` 才失败」？

**参考答案**：因为两者语义相反。判据 A 是「错误标记出现 = 失败」（正向命中即失败）；判据 B 是「成功标记缺失 = 失败」（即 `SUCCESSFULLY` 这个**好**字样没出现才算失败）。所以 B 用 `not in` 取反。

**练习 2**：假如某次仿真既没有 `###ERROR###`，也没有 `SIMULATIONS COMPLETED SUCCESSFULLY`（比如编译就报错中断了），`ciFlow.py` 会返回什么？

**参考答案**：返回 `-2`。因为先查 A（不含 `###ERROR###`，跳过），再查 B（不含成功标记，命中）→ `exit(-2)`。这正是 B 判据兜底「流程没跑完」的作用。

**练习 3**：`ci.do` 里的 `quit` 如果漏写，会对 CI 造成什么影响？

**参考答案**：`vsim -c` 跑完 `run.tcl` 后不会自动退出，`os.system` 会一直阻塞等待 vsim 进程结束，CI 会挂住直到超时。`quit` 保证 vsim 及时退出，让 Python 继续读 transcript 判定。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「端到端理解仿真与 CI」的小任务：

**任务**：假设你要给本项目新增一个测试台文件 `tb/top_tb_extra.vhd`（用于验证某个新行为），请写出完整的「使它进入仿真与 CI」的改动清单，并解释每一步的理由。

**参考做法**：

1. **改 `sim/config.tcl`**：在 [sim/config.tcl:47-50](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L47-L50) 的 `add_sources "../tb"` 列表里追加 `top_tb_extra.vhd`（记得给原来的 `top_tb.vhd` 补续行符 `\`）。理由：PsiSim 靠 `config.tcl` 知道要编译哪些 tb 文件。

2. **决定是否新增运行**：如果新测试台也需要跑两遍（AXIS/AXIMM），仿照 [sim/config.tcl:52-55](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/sim/config.tcl#L52-L55) 再加一组 `create_tb_run "top_tb_extra"` + `tb_run_add_arguments` + `add_tb_run`。

3. **`run.tcl` / `runGhdl.tcl` / `ci.do` 不需要改**：它们用的是 `compile -all` 和 `run_tb -all`，会自动覆盖 `config.tcl` 里新声明的内容。这正是 PsiSim 「配置与流程分离」带来的好处。

4. **`ciFlow.py` 不需要改**：它只关心 transcript 里的两个标记，与新测试台无关。只要新测试台在失败时也让 psi_tb 打印 `###ERROR###`、成功时让 PsiSim 打印 `SIMULATIONS COMPLETED SUCCESSFULLY`，CI 判定就自动生效。

5. **验证**：本地 `source ./run.tcl` 跑一遍；若进 CI，确认 `ciFlow.py` 返回 `0`。

**需要观察的现象**：新测试台被编译并执行；transcript 里多出对应的运行段落；失败时返回非 0，全过时返回 0。

**预期结果**：只改 `config.tcl` 一处（必要时再加运行定义）即可让新测试台同时进入本地回归与 CI，无需触碰任何流程脚本。

> 运行结果待本地验证（取决于你是否具备 Modelsim/GHDL 环境）。

## 6. 本讲小结

- 一次回归仿真分三段：**编译 → 运行 → 检查**，由 `sim/run.tcl` 用 `psi::sim::compile -all -clean`、`run_tb -all`、`run_check_errors "###ERROR###"` 串联。
- `sim/config.tcl` 用 `-tag lib/src/tb` 把源文件分成「第三方库 / 项目源码 / 测试台」三组，并用两个 `OutputType_g` 取值让同一测试台跑 AXIS 与 AXIMM 两遍。
- Modelsim 与 GHDL 的入口分别是 `sim/run.tcl` 与 `sim/runGhdl.tcl`，二者唯一差别是 `init -ghdl`；交互式调试用 `interactive*.tcl`。
- CI 用 `sim/ci.do`（`source run.tcl` + `quit`）把交互流程批处理化，再用 `scripts/ciFlow.py` 读 `Transcript.transcript` 判定退出码。
- 两个判据互补：**判据 A**（`###ERROR###` 存在 → `-1`）抓「功能错误」，**判据 B**（`SIMULATIONS COMPLETED SUCCESSFULLY` 缺失 → `-2`）抓「流程没跑完」；都过才返回 `0`。
- `###ERROR###` 与 `SIMULATIONS COMPLETED SUCCESSFULLY` 都由外部依赖 PsiSim/psi_tb 框架产生，测试台代码本身不字面写出它们。

## 7. 下一步学习建议

到这里你已经能「跑起来并看懂结果」了。接下来建议：

1. **进入第二单元 u2-l1《整体架构与数据流》**：从仿真脚本转到 RTL 内部，理解 `s00_axi → RegTable → 核心 FSM → m00_axi → FIFO → AXIS/AXIMM` 的完整数据通路。
2. **在进入 u2 之前**，可选地用 `sim/interactive.tcl` 把 `top_tb.vhd` 加载到 Modelsim GUI 里，对照 [tb/top_tb.vhd:272-388](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/tb/top_tb.vhd#L272-L388) 的 6 组用例（`>> Trigger Single Read` 等 `print` 标记）单步跑一遍，建立对「测试台在干什么」的直觉——这会极大降低阅读 u2-l3 核心状态机时的认知负担。
3. 如果你想先了解「这套 RTL 怎么被打包成 Vivado IP」，可以跳到 [u1-l4 IP 打包与 Vivado 集成](u1-l4-ip-packaging.md)。
