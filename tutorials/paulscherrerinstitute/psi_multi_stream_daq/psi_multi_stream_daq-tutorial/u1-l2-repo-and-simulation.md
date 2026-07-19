# 仓库结构与仿真/构建运行方式

## 1. 本讲目标

上一讲（u1-l1）我们在不看源码的前提下建立了对 `psi_multi_stream_daq` 的整体认识。本讲则要回答一个更"落地"的问题：**这套源码到底是怎么组织的，我又该怎么把它跑起来？**

读完本讲，你应当能够：

- 说出 `hdl/`、`driver/`、`tb/`、`sim/`、`scripts/`、`doc/` 这几个目录各自存放什么、承担什么职责。
- 理解项目依赖（`psi_common`、`psi_tb`、`PsiSim`）是如何被声明、又是如何被一个 Python 脚本自动拉取的，以及它们必须放在什么样的目录结构里。
- 读懂 `sim/config.tcl` 里"库 / 源文件 / 测试平台"三组清单，知道每个文件归到哪一组。
- 看懂 `sim/run.tcl` 的 **init → configure → compile → run → check** 五步仿真流程。
- 说明 `scripts/ciFlow.py` 如何以批处理方式跑完所有 testbench，并通过日志里的两个关键字串判定"成功"还是"失败"。

本讲是后续所有讲义的"工程地基"：只有先知道文件在哪里、仿真怎么跑，后面阅读 VHDL 源码时你才能随时用 testbench 验证自己的理解。

## 2. 前置知识

上一讲已经引入了 FPGA、VHDL、IP 核、AXI、DMA、DAQ 等术语，这里不再重复。本讲额外需要几个与"构建/仿真"相关的概念：

- **Testbench（测试平台）**：一段专门用来"驱动并检查"被测硬件的 VHDL 代码。被测的 IP 核叫 **DUT**（Design Under Test），testbench 不综合到 FPGA 里，只在仿真时运行。本项目的 `tb/` 目录就是一堆 testbench。
- **仿真器（Simulator）**：把 VHDL 代码当作"程序"逐拍执行的软件工具。本项目使用的是 **Modelsim / QuestaSim**（行业内非常常见的 HDL 仿真器）。
- **Tcl（Tool Command Language）**：一种脚本语言。Modelsim 的所有操作（建库、编译、运行、看波形）都能用 Tcl 命令驱动。本项目用一套名为 **PsiSim** 的 Tcl 框架来组织仿真。
- **回归测试（Regression Test）**：把所有 testbench 一次性全部跑一遍，确认没有任何一个用例失败。这是保证"改了代码没有改坏旧功能"的标准做法。
- **批处理（Batch）**：让仿真器在命令行里自动跑完，不需要人去点图形界面。CI（持续集成）里跑的就是批处理。

如果你暂时不清楚 PsiSim 内部是怎么实现的，没关系——本讲只把它当成一个"提供 `init`、`compile_files`、`run_tb` 等命令的工具箱"来用。

## 3. 本讲源码地图

本讲涉及的文件都不长，但它们一起构成了"项目工程骨架"：

| 文件 | 作用 | 本讲用它来回答什么 |
| --- | --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md) | 项目门面，其中有一段可被脚本解析的"依赖声明"区块 | 依赖哪些库？它们要放在什么目录结构里？怎么跑仿真？ |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/dependencies.py) | 解析 README 里的依赖区块并自动拉取依赖 | 依赖是怎么被自动获取的？ |
| [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py) | 以批处理方式调用 Modelsim 跑回归，并判定成败 | CI 是怎么判定一次仿真"通过"或"失败"的？ |
| [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl) | 声明仿真库、本 IP 核源文件、所有 testbench 及每次运行的参数 | 要编译哪些文件？每个 testbench 用什么参数跑？ |
| [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl) | 仿真的主流程脚本：初始化→编译→运行→检查 | 完整的仿真流程分几步？ |
| [sim/ci.do](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/ci.do) | 给 Modelsim 批处理用的 do 文件，串联 run.tcl | 批处理入口是怎么衔接 run.tcl 的？ |
| [sim/interactive.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/interactive.tcl) | 为交互式（图形界面）调试准备的脚本 | 日常手动调试时怎么用？ |

## 4. 核心概念与源码讲解

### 4.1 仓库目录布局与依赖契约

#### 4.1.1 概念说明

一个 FPGA IP 核项目并不只有"硬件代码"。要让它可被使用、可被验证、可被集成，至少需要四类东西同时存在：

1. **硬件实现**：VHDL 源码（IP 核本体）。
2. **软件驱动**：让处理器一侧控制 IP 核、读回数据的 C 代码。
3. **验证代码**：testbench，证明硬件实现是对的。
4. **工程脚本**：告诉工具"编译哪些文件、按什么顺序、用什么参数"。

`psi_multi_stream_daq` 把这四类东西分别放在不同目录里，职责清晰。除了项目自己的代码，它还会**复用**另外几个 PSI 开源库（`psi_common`、`psi_tb`、`PsiSim`），因此还存在一套"依赖放在哪"的约定——这套约定本身就是一份契约。

#### 4.1.2 核心流程

项目的目录布局如下：

```
psi_multi_stream_daq/          <- 本仓库根目录
├── hdl/        VHDL 硬件实现（IP 核本体，7 个 .vhd 文件）
├── driver/     嵌入式 C 驱动（psi_ms_daq.c / psi_ms_daq.h）
├── tb/         VHDL 测试平台（按模块分子目录）
├── sim/        仿真流程 Tcl 脚本（config.tcl / run.tcl / ci.do ...）
├── scripts/    工程脚本（dependencies.py / ciFlow.py）
├── doc/        功能说明文档（PDF / DOCX / Doxygen）
├── README.md / Changelog.md / License.txt / LGPL2_1.txt
```

依赖库则要求放在一个**与本项目平级**的统一目录结构里（目录名必须完全匹配）：

```
<某个父目录>/
├── TCL/
│   └── PsiSim/                 <- 仿真框架（Tcl）
└── VHDL/
    ├── psi_common/             <- 通用 VHDL 组件库
    ├── psi_tb/                 <- 通用 testbench 工具库
    └── psi_multi_stream_daq/   <- 本项目自己
```

之所以要这样"平级摆放"，是因为 `sim/config.tcl` 和 `sim/run.tcl` 里写的是**相对路径**（`../../../VHDL`、`../../../TCL/PsiSim/PsiSim.tcl`），只有满足这个布局，脚本才能找到依赖。如果嫌手动摆放麻烦，可以直接克隆汇总仓库 [psi_fpga_all](https://github.com/paulscherrerinstitute/psi_fpga_all)，它已经把所有相关仓库以子模块形式按正确结构摆好了。

> 提示：你也可以用 `scripts/dependencies.py` 自动拉取依赖，下一节会讲到。

#### 4.1.3 源码精读

依赖契约写在 [README.md:25-40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L25-L40)。注意其中两行特殊注释：

```tcl
<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->
...
<!-- END OF PARSED SECTION -->
```

这两行之间的内容会被 `dependencies.py` 当作机器可读的依赖清单来解析，所以**格式不能随便改**。清单声明了三类依赖：

- `TCL/PsiSim`（≥2.1.0，仅开发/仿真时需要）—— 仿真框架。
- `VHDL/psi_common`（≥3.0.0）—— 通用组件库，运行时必需。
- `VHDL/psi_tb`（≥3.0.0，仅开发时需要）—— testbench 工具库。

依赖的自动拉取由 [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/dependencies.py) 完成，核心只有三行：

```python
dependencies = Parse.FromReadme(THIS_DIR + "/../README.md")  # 解析 README 依赖区块
repo = os.path.abspath(THIS_DIR + "/..")                      # 本仓库根目录
Actions.ExecMain(repo, dependencies)                          # 执行拉取
```

它借助外部包 `PsiFpgaLibDependencies`（见 [dependencies.py:7](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/dependencies.py#L7) 的 `from PsiFpgaLibDependencies import *`）提供 `Parse.FromReadme` 和 `Actions.ExecMain` 两个能力：前者把 README 里的清单解析成依赖对象，后者根据依赖对象去 git 克隆/检出对应版本。使用前需要先安装这个包（README 第 48 行有说明）。

#### 4.1.4 代码实践

**实践目标**：用 `dependencies.py` 的帮助信息确认它的用法。

1. 打开终端，进入仓库的 `scripts/` 目录。
2. 执行（需要已安装 `PsiFpgaLibDependencies` 包）：

   ```bash
   python dependencies.py -help
   ```

3. **需要观察的现象**：帮助文本里会列出可用的命令行选项（例如指定目标目录、指定版本等）。
4. **预期结果**：你能看到这个脚本接受哪些参数。**如果你本地没有安装 `PsiFpgaLibDependencies` 包，会报 `ModuleNotFoundError`——这属于正常现象，本实践重点在于确认脚本入口和它对 README 的依赖，记下报错信息即可，标注为「待本地验证」。**

> 即便无法运行，你也已经掌握了关键事实：依赖清单的唯一真相来源是 `README.md` 的解析区块，`dependencies.py` 只是它的执行器。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `psi_common` 被标注为"运行时必需"，而 `psi_tb`、`PsiSim` 被标注为"仅开发时需要"？

> **答案**：`psi_common` 提供的是 IP 核综合到 FPGA 时实际用到的硬件组件（FIFO、RAM、AXI master 等），少了它 IP 核无法工作；而 `psi_tb` 是 testbench 工具库、`PsiSim` 是仿真框架，两者只在仿真验证阶段使用，不会进入最终比特流，所以只在做开发/验证时才需要。

**练习 2**：假设有人不小心修改了 `README.md` 依赖区块里的列表格式（比如把 `* TCL` 改成了 `- TCL`），会引发什么后果？

> **答案**：因为该区块被 `dependencies.py` 用 `Parse.FromReadme` 解析，格式改动很可能导致解析失败或漏掉依赖，进而让自动拉取出错。这也是 README 在区块上下方各放了一行 `DO NOT CHANGE FORMAT` / `END OF PARSED SECTION` 注释的原因。

---

### 4.2 sim/config.tcl：声明库、源文件与测试平台

#### 4.2.1 概念说明

`sim/config.tcl` 是仿真的"配料表"。仿真器在编译前必须知道三件事：

1. **要建一个叫什么名字的库**（VHDL 库，相当于代码的命名空间容器）。
2. **要把哪些文件编译进这个库**，以及这些文件**按什么依赖顺序**排列。
3. **要跑哪些 testbench**，每个 testbench 用**什么参数（generic）**跑、跑几遍。

这份配料表还顺手把文件分成了三组：依赖库文件（`-tag lib`）、本项目源文件（`-tag src`）、testbench（`-tag tb`）。这种分组让 PsiSim 能按"先库、再源码、最后 testbench"的正确顺序编译。

#### 4.2.2 核心流程

`config.tcl` 的执行流程可以概括为：

```
设置依赖库根路径 LibPath
   ↓
导入 PsiSim 命令空间 (namespace import psi::sim::*)
   ↓
add_library psi_ms_daq          ← 创建名为 psi_ms_daq 的 VHDL 库
   ↓
设置编译期/运行期要屏蔽的告警编号
   ↓
add_sources ... -tag lib        ← 声明依赖库文件（psi_tb + psi_common）
   ↓
add_sources ... -tag src        ← 声明本项目 7 个 VHDL 源文件
   ↓
add_sources ... -tag tb         ← 声明所有 testbench（含 *_tb_pkg 与各 case 文件）
   ↓
create_tb_run / tb_run_add_arguments / add_tb_run   ← 声明每个 TB 的运行配置
```

其中 input testbench 比较特别：它用 `tb_run_add_arguments` 声明了 **6 组不同的 generic 组合**，相当于把同一个 DUT 在 6 种参数下各跑一遍（参数化自检），稍后在 4.2.3 详述。

#### 4.2.3 源码精读

**(a) 库路径与库名**

[config.tcl:8](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L8) 设定依赖库的相对根路径，对应 4.1 节讲的"平级目录结构"：

```tcl
set LibPath "../../../VHDL"
```

[config.tcl:14](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L14) 创建本次仿真专用的 VHDL 库：

```tcl
add_library psi_ms_daq
```

**(b) 依赖库文件（-tag lib）**

[config.tcl:21-43](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L21-L43) 把 `psi_tb` 和 `psi_common` 里的若干 `.vhd` 文件声明为依赖。这里的**顺序就是编译顺序**——例如 `psi_common_array_pkg` 排在 `psi_common_math_pkg` 前面，是因为后者依赖前者。你可以看到本项目用到了 psi_common 的哪些积木：异步 FIFO（`psi_common_async_fifo`）、同步 FIFO（`psi_common_sync_fifo`）、双口 RAM（`psi_common_tdp_ram`、`psi_common_sdp_ram`）、跨时钟域（`psi_common_pulse_cc`/`bit_cc`/`status_cc`/`simple_cc`）、优先级仲裁器（`psi_common_arb_priority`）、AXI master/slave（`psi_common_axi_master_simple`/`_full`、`psi_common_axi_slave_ipif`）、位宽转换（`psi_common_wconv_n2xn`）、流水级（`psi_common_pl_stage`）等。这些会在后续进阶讲义里逐个遇到。

**(c) 本项目源文件（-tag src）**

[config.tcl:46-54](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L46-L54) 声明 `hdl/` 下的 7 个源文件，顺序同样重要（`psi_ms_daq_pkg` 包最先，顶层 `psi_ms_daq_axi` 最后）：

```tcl
add_sources "../hdl" {
    psi_ms_daq_pkg.vhd      ← 公共类型/常量包
    psi_ms_daq_input.vhd    ← 输入逻辑
    psi_ms_daq_daq_sm.vhd   ← 控制状态机（"大脑"）
    psi_ms_daq_daq_dma.vhd  ← DMA 引擎
    psi_ms_daq_axi_if.vhd   ← AXI 主接口
    psi_ms_daq_reg_axi.vhd  ← AXI Slave 寄存器接口
    psi_ms_daq_axi.vhd      ← 顶层，例化上面 5 个子模块
} -tag src
```

这 7 个文件正是后续 u1-l3 到 u4 讲义的主角。

**(d) 测试平台（-tag tb）**

[config.tcl:57-95](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L57-L95) 声明所有 testbench，按被测模块分了 5 个子目录：`psi_ms_daq_input/`、`psi_ms_daq_daq_sm/`、`psi_ms_daq_daq_dma/`、`psi_ms_daq_axi/`（多流顶层）、`psi_ms_daq_axi_1s/`（单流顶层变体）。每个模块通常有一个 `*_tb_pkg.vhd`（公共激励/校验过程）、若干 `*_tb_case_*.vhd`（按场景拆分的用例）和一个 `*_tb.vhd`（顶层 TB，串起所有 case）。

**(e) 参数化运行配置**

[config.tcl:98-106](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L98-L106) 给 input testbench 声明了 6 组 generic，让同一个 TB 在不同参数下各跑一次：

```tcl
create_tb_run "psi_ms_daq_input_tb"
tb_run_add_arguments \
    "-gStreamWidth_g=8 -gVldPulsed_g=false" \
    "-gStreamWidth_g=8 -gVldPulsed_g=true" \
    "-gStreamWidth_g=16 -gVldPulsed_g=false" \
    "-gStreamWidth_g=32 -gVldPulsed_g=false" \
    "-gStreamWidth_g=64 -gVldPulsed_g=false" \
    "-gStreamWidth_g=64 -gVldPulsed_g=true"
add_tb_run
```

可见它覆盖了 8/16/32/64 四种流宽度，以及在 8 位和 64 位下分别测试"Vld 连续"和"Vld 脉冲"两种有效信号风格。其余 4 个 TB（[config.tcl:108-118](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L108-L118)）只各跑一遍默认参数。

#### 4.2.4 代码实践

**实践目标**：把 `config.tcl` 的三组文件画成一张"包含关系图"。

1. 阅读完整的 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl)。
2. 在纸上或文档里画出三棵子树：
   - `psi_ms_daq` 库（根）
     - `lib`：`psi_common/*` 与 `psi_tb/*` 的文件
     - `src`：`hdl/` 下的 7 个文件
     - `tb`：5 个 TB 子目录及其文件
3. **需要观察的现象**：确认依赖库文件里 psi_common 与 psi_tb 各贡献了几个文件，src 里是不是正好 7 个。
4. **预期结果**：得到一张清晰的"库 ← 源 ← TB"分层图。你会发现 testbench 数量远多于源文件——这正是一个成熟 IP 核的常态：**验证代码量通常超过实现代码量**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `psi_ms_daq_pkg.vhd` 必须排在 `src` 列表的第一位？

> **答案**：因为它是包文件（package），定义了其余 6 个源文件都会用到的公共常量与 record 类型（如 `DaqSm2DaqDma_Cmd_t`）。VHDL 要求"被使用的包必须先编译可见"，所以它必须最先编译。

**练习 2**：input testbench 跑了 6 遍，但 `daq_sm_tb`、`daq_dma_tb`、`axi_tb`、`axi_1s_tb` 各只跑 1 遍（[config.tcl:108-118](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L108-L118)）。这说明 input 模块有什么特殊性？

> **答案**：input 模块直接处理不同位宽的流数据，其行为对 `StreamWidth_g`（8/16/32/64）和 `VldPulsed_g`（有效信号是连续电平还是脉冲）高度敏感，因此必须用多组 generic 做参数化自检；其余模块要么不直接暴露这些参数，要么已经在顶层 TB 内部用代码覆盖了不同场景，所以只需各跑一遍默认配置。

---

### 4.3 sim/run.tcl：init/compile/run/check 四步仿真流程

#### 4.3.1 概念说明

`config.tcl` 只是"配料表"，真正"按下启动键"的是 `run.tcl`。它定义了一次完整回归仿真的步骤顺序。理解这个脚本，你就掌握了以后**手动重跑某个 testbench、定位失败用例**时的入口。

#### 4.3.2 核心流程

`run.tcl` 把仿真分成清晰的三段（编译、运行、检查），加上前置的加载与初始化：

```
1. 加载 PsiSim 框架      source ../../../TCL/PsiSim/PsiSim.tcl
2. 导入命令空间          namespace import psi::sim::*
3. 初始化                init
4. 读取配料表            source ./config.tcl
   ─────────────────  Compile ─────────────────
5. 编译全部文件          compile_files -all -clean
   ─────────────────  Run ─────────────────
6. 运行全部 testbench    run_tb -all
   ─────────────────  Check ─────────────────
7. 扫描日志找错误        run_check_errors "###ERROR###"
```

`-clean` 表示编译前先清掉旧的编译产物（相当于"干净的全新构建"），保证结果可复现。

#### 4.3.3 源码精读

**加载框架与初始化**

[run.tcl:8-12](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L8-L12)：

```tcl
source ../../../TCL/PsiSim/PsiSim.tcl   # 载入 PsiSim 工具箱
namespace import psi::sim::*            # 把 psi::sim:: 下的命令暴露到当前命名空间
init                                     # PsiSim 初始化（准备工作目录等）
```

注意这里又出现了 `../../../TCL/PsiSim/...` 这个相对路径，再次印证了 4.1 节的目录结构契约——PsiSim 框架必须放在 `TCL/PsiSim/` 下。

**读取配料表**

[run.tcl:15](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L15) 执行 4.2 节讲的 `config.tcl`：

```tcl
source ./config.tcl
```

**编译**

[run.tcl:21](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L21)：

```tcl
compile_files -all -clean
```

PsiSim 会按 `config.tcl` 里声明的顺序（先 lib，再 src，最后 tb）编译全部文件，并先清理旧产物。

**运行**

[run.tcl:25](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L25)：

```tcl
run_tb -all
```

`-all` 表示把 `create_tb_run` 声明的**所有** testbench 运行（包括 input TB 的 6 组参数）全部执行一遍。

**检查**

[run.tcl:30](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L30)：

```tcl
run_check_errors "###ERROR###"
```

这一步会扫描仿真输出日志，只要任何一行出现了字串 `###ERROR###`，就认为有用例失败。换句话说，**testbench 是通过"在发现错误时主动打印 `###ERROR###`"来报告失败的**——这是本项目所有 TB 共同遵守的约定，后面讲 testbench 时你会反复看到它。

#### 4.3.4 代码实践

**实践目标**：在 Modelsim 里手动跑一次完整回归（源码阅读型 + 可选运行）。

1. 假设你已按 4.1 节摆好依赖目录结构，并打开了 Modelsim。
2. 在 Modelsim 的 Tcl 控制台里，把工作目录切到 `sim/`，然后执行 README 给出的命令（[README.md:50-56](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L50-L56)）：

   ```tcl
   cd <仓库路径>/sim
   source ./run.tcl
   ```

3. **需要观察的现象**：控制台会依次打印 `-- Compile`、`-- Run`、`-- Check` 三段分隔标题（来自 [run.tcl:18-29](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl#L18-L29) 的 `puts` 语句），最后是 check 结果。
4. **预期结果**：若一切正常，check 阶段不会报 `###ERROR###`；若某个用例失败，你会在日志里看到带 `###ERROR###` 的行，据此定位是哪个 TB / 哪个 case 出错。**如果你本地没有安装 Modelsim 与依赖库，无法实际运行，请标注为「待本地验证」，但应能口头复述这四步的顺序与各自作用。**

> 日常调试时，比起每次跑全套回归，更常用的是 [sim/interactive.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/interactive.tcl)：它只做"加载框架 → init → source config.tcl → compile_files -all -clean"（见 [interactive.tcl:11-19](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/interactive.tcl#L11-L19)），编译完就停下来，让你在图形界面里手动 `vsim` 某个 TB、看波形。本讲实践只要你理解它和 `run.tcl` 的差别：**interactive 只编译不运行，适合人盯着调；run 全自动跑完，适合回归/CI。**

#### 4.3.5 小练习与答案

**练习 1**：`compile_files -all -clean` 里的 `-clean` 去掉会怎样？

> **答案**：去掉后 PsiSim 会复用上一次的编译产物，只重新编译有变动的文件。这能加快迭代速度，但有可能残留过期的编译缓存导致"明明改了代码却行为不变"的假象；做回归测试时通常保留 `-clean` 以保证可复现。

**练习 2**：项目为什么选择用"打印 `###ERROR###`"这种基于文本约定的方式来报告失败，而不是依赖仿真器的 assertion 计数？

> **答案**：基于文本约定让结果判定与具体仿真器解耦——无论是 Modelsim、QuestaSim 还是其它仿真器，只要能把输出重定向到日志文件，就能用 `run_check_errors` 或 `ciFlow.py` 统一扫描，便于 CI 自动化和跨工具移植。

---

### 4.4 scripts/ciFlow.py 与 ci.do：批处理回归与成败判定

#### 4.4.1 概念说明

`run.tcl` 是给人用的（在 Modelsim 界面里 `source`）。但 CI（持续集成）服务器上没有人去点界面，需要一种**完全命令行、跑完即退出、并用进程退出码表达成败**的方式。这就是 `ci.do` + `scripts/ciFlow.py` 的职责。

- `ci.do` 是 Modelsim 的批处理入口脚本（`.do` 文件）。
- `ciFlow.py` 是外层驱动，负责调用 Modelsim、收集日志、根据日志内容给出退出码。

#### 4.4.2 核心流程

CI 的判定逻辑可以用一个状态机描述：

```
ciFlow.py 启动
   │
   ├── os.chdir(...) 切到 sim/ 目录
   ├── os.system("vsim -batch -do ci.do -logfile Transcript.transcript")
   │        └─ Modelsim 批处理模式启动，执行 ci.do
   │             └─ ci.do:  onerror {exit}  →  source run.tcl  →  quit
   │                          (任何 Tcl 错误都立刻退出)   (跑完整套回归)
   │
   ├── 读取日志文件 Transcript.transcript
   │
   ├── 日志含 "###ERROR###" ?           ── 是 ──→ exit(-1)   【有用例失败】
   │        └─ 否
   ├── 日志含 "SIMULATIONS COMPLETED SUCCESSFULLY" ?  ── 否 ──→ exit(-2)  【异常中断】
   │        └─ 是
   └── exit(0)   【全部通过】
```

这里有**两个层次**的错误判定，理解它们很关键：

- **"有用例失败"**（exit -1）：回归跑完了，但某个 testbench 发现了错误并打印了 `###ERROR###`。
- **"异常中断"**（exit -2）：回归**没有**正常跑完（比如编译失败、脚本异常），因此日志里既没有 `###ERROR###`，也没有成功标志串。第二个检查就是为了抓住这种"静默崩溃"。

成功标志串 `SIMULATIONS COMPLETED SUCCESSFULLY` 是 PsiSim 框架在所有 TB 都正常跑完后自己打印的——只有看到它，才能确认"确实全部跑完了"。

#### 4.4.3 源码精读

**(a) 批处理入口 ci.do**

[ci.do:7-9](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/ci.do#L7-L9)：

```tcl
onerror {exit}      # 任何 Tcl 错误都让 Modelsim 立即退出（而不是卡住）
source run.tcl      # 跑 4.3 节那套完整流程
quit                # 退出 Modelsim
```

`onerror {exit}` 这一句很关键：批处理时如果某条命令报错，没人会去手动处理，必须让它立刻退出，把控制权交还外层 `ciFlow.py`。

**(b) 外层驱动 ciFlow.py**

[ciFlow.py:9-13](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L9-L13) 先切到 `sim/` 目录再以批处理方式启动 Modelsim，并把所有输出写进日志文件：

```python
THIS_DIR = os.path.dirname(os.path.abspath(__file__))   # scripts/ 目录
os.chdir(THIS_DIR + "/../sim")                           # 切到 sim/
os.system("vsim -batch -do ci.do -logfile Transcript.transcript")
```

`-batch` 表示无图形界面、批处理运行；`-do ci.do` 指定要执行的 do 文件；`-logfile` 把输出重定向到 `Transcript.transcript`。

随后 [ciFlow.py:15-23](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L15-L23) 读取该日志并做两层判定：

```python
with open("Transcript.transcript") as f:
    content = f.read()

#Expected Errors
if "###ERROR###" in content:
    exit(-1)
#Unexpected Errors
if "SIMULATIONS COMPLETED SUCCESSFULLY" not in content:
    exit(-2)
```

最后 [ciFlow.py:27](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py#L27) 在两层都通过时返回成功：

```python
exit(0)
```

退出码含义汇总：

| 退出码 | 含义 | 触发条件 |
| --- | --- | --- |
| `0` | 全部通过 | 日志无 `###ERROR###`，且包含成功标志串 |
| `-1` | 有用例失败 | 日志中出现了 `###ERROR###` |
| `-2` | 异常中断 | 日志中没有出现成功标志串（静默崩溃/编译失败等） |

#### 4.4.4 代码实践

**实践目标**：在不实际跑仿真的前提下，亲手验证 `ciFlow.py` 的判定逻辑（源码阅读型 + 思维实验）。

1. 打开 [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py)。
2. 在 `sim/` 目录下手工构造三种"假的"日志文件，命名为 `Transcript.transcript`，分别预测 `ciFlow.py` 的退出码：
   - **场景 A**：文件内容包含一行 `SIMULATIONS COMPLETED SUCCESSFULLY`，没有任何 `###ERROR###`。
   - **场景 B**：文件内容包含 `###ERROR### Assertion failed.`，也包含 `SIMULATIONS COMPLETED SUCCESSFULLY`。
   - **场景 C**：文件内容只有 `** Error: cannot compile psi_ms_daq_pkg.vhd`，既没有 `###ERROR###` 也没有成功标志串。
3. **需要观察的现象**：对照源码的两个 `if` 分支，写出每个场景会被判成什么。
4. **预期结果**：
   - 场景 A → `exit(0)`（全部通过）。
   - 场景 B → `exit(-1)`（有用例失败，第一个 if 命中）。
   - 场景 C → `exit(-2)`（异常中断，第一个 if 未命中、第二个 if 命中）。
   - 这说明第二个检查存在的意义：编译失败不会打印 `###ERROR###`，如果没有第二道防线，CI 会把"编译都过不了"误判成"成功"。

> 若想真正运行，可临时备份 `Transcript.transcript`、写入上述内容后执行 `python scripts/ciFlow.py`，但要注意它会真的去调 `vsim`——所以更安全的做法是只做上面的思维实验，并标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ciFlow.py` 要做**两层**检查（`###ERROR###` 和成功标志串），而不是只检查其中一个？

> **答案**：只检查 `###ERROR###` 会漏掉"编译失败/脚本异常"这类静默崩溃——它们不会打印 `###ERROR###`，于是会被误判为成功。引入"必须出现 `SIMULATIONS COMPLETED SUCCESSFULLY`"这第二道防线，才能确认回归真的完整跑完了。两层一起，分别覆盖"有用例失败"和"根本没跑完"两种失败模式。

**练习 2**：假如你新增了一个 testbench，它里面忘了在检测到错误时打印 `###ERROR###`，而是只调用了 VHDL 的 `assert ... report ... severity error`。CI 还能可靠地抓住它的失败吗？

> **答案**：不能可靠抓住。`ciFlow.py` 只扫描 `###ERROR###` 这个字串，纯 `assert` 失败不会产生这个字串（最多在日志里出现 Modelsim 自己的 `** Error`/`** Failure` 字样，但不被本项目判定逻辑识别）。所以本项目所有 testbench 都遵守"发现错误就打印 `###ERROR###`"的约定——这就是为什么你在 TB 里会反复看到这个魔法字串。

---

## 5. 综合实践

把本讲的四条主线串起来，完成下面这个**端到端的工程理解任务**。

**任务**：假设团队决定新增一个假想的子模块 `psi_ms_daq_xxx`（含源文件 `hdl/psi_ms_daq_xxx.vhd` 和 testbench `tb/psi_ms_daq_xxx/psi_ms_daq_xxx_tb.vhd`），请你规划需要改动哪些工程文件，并说明改动后 CI 是如何自动覆盖它的。

请按下面的步骤产出一份"变更清单 + 流程说明"：

1. **依赖与目录**：参照 4.1 节，说明新模块是否会引入新的 `psi_*` 依赖？如果会，需要在 [README.md:25-40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L25-L40) 的解析区块里加一行，并提醒同事"不要改格式"。
2. **配料表**：在 [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl) 里：
   - 把 `hdl/psi_ms_daq_xxx.vhd` 加进 `-tag src` 那一组（注意它依赖 `psi_ms_daq_pkg`，所以要排在 pkg 之后）。
   - 把 `tb/psi_ms_daq_xxx/psi_ms_daq_xxx_tb.vhd` 加进 `-tag tb` 那一组。
   - 仿照 [config.tcl:108-110](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl#L108-L110)，新增一段 `create_tb_run "psi_ms_daq_xxx_tb"` + `add_tb_run`。
3. **仿真流程**：说明 `run.tcl` 本身**不需要改**——因为它用的是 `compile_files -all` 和 `run_tb -all`，会自动纳入 config.tcl 里新声明的文件与 TB。
4. **CI 判定**：写出 CI 跑完后，`ciFlow.py` 会如何判定这个新 TB：若新 TB 按约定在出错时打印 `###ERROR###`，则失败时退出码为 `-1`；若新 TB 没跑完（例如编译失败），则因日志缺少 `SIMULATIONS COMPLETED SUCCESSFULLY` 而退出码为 `-2`；全部正常则 `0`。
5. **产出**：画一张"包含关系图"——`psi_ms_daq` 库下分 `lib`/`src`/`tb` 三组，标出 psi_common、psi_tb、本项目 7+1 个源文件、各 TB（含新增的 xxx）分别落在哪一组；并在图旁用一句话写出 `ciFlow.py` 判定成败的两层逻辑。

> 这个任务不需要你真的有 `psi_ms_daq_xxx.vhd`——它的目的是让你把"目录结构 → 依赖声明 → config.tcl 清单 → run.tcl 流程 → ciFlow.py 判定"这条链路在脑子里完整走一遍。完成后，你就具备了"看懂任何 PSI 系列 IP 核工程骨架"的能力。

## 6. 本讲小结

- 项目按 `hdl/`（硬件实现）、`driver/`（C 驱动）、`tb/`（测试平台）、`sim/`（仿真脚本）、`scripts/`（工程脚本）、`doc/`（文档）分目录组织，职责清晰。
- 依赖（`psi_common`、`psi_tb`、`PsiSim`）的真相来源是 [README.md:25-40](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/README.md#L25-L40) 的可解析区块，由 [scripts/dependencies.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/dependencies.py) 自动拉取；它们必须放在名字固定的平级 `TCL/`、`VHDL/` 目录结构里。
- [sim/config.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/config.tcl) 是仿真"配料表"，用 `-tag lib/src/tb` 把文件分成三组、按依赖顺序声明，并用 `create_tb_run`/`tb_run_add_arguments` 配置每个 TB 的运行参数（input TB 跑了 6 组 generic）。
- [sim/run.tcl](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/sim/run.tcl) 定义了 **加载框架 → init → source config.tcl → compile_files → run_tb → run_check_errors** 的完整流程，靠扫描 `###ERROR###` 发现失败用例。
- [scripts/ciFlow.py](https://github.com/paulscherrerinstitute/psi_multi_stream_daq/blob/f4c0185f318d9f7cc9fa8edd109b00cc1764dcf9/scripts/ciFlow.py) 通过 `vsim -batch -do ci.do` 跑批处理，并用**两层日志检查**给出退出码：`###ERROR###` → `-1`（用例失败）、缺少成功标志串 → `-2`（异常中断）、两者皆过 → `0`（全部通过）。
- 日常手动调试用 `sim/interactive.tcl`（只编译不运行），CI 和回归用 `run.tcl`/`ci.do`（全自动）。

## 7. 下一步学习建议

现在你已经知道"文件在哪、仿真怎么跑"，接下来可以：

- **进入硬件本体**：下一讲 **u1-l3（顶层 IP 核 psi_ms_daq_axi：生成参数与端口）** 会打开 `hdl/psi_ms_daq_axi.vhd`，讲解顶层实体的所有 generic 与端口，建立"流输入 → 输入逻辑 → 状态机 → DMA → AXI Master → 内存"的顶层数据流印象。建议在读之前，先用本讲学到的 `interactive.tcl` 把工程编译一遍，方便随时打开波形验证。
- **了解软件侧**：如果想先看"怎么用 C 代码驱动这个 IP"，可以跳到 **u1-l4（软件驱动快速上手）**，阅读 `driver/psi_ms_daq.h` 的示例代码。
- **验证体系深入**：等到进阶/专家阶段，**u5-l1（测试平台结构与 PsiSim 仿真流程）** 会从 PsiSim 框架本身和各模块 testbench 的用例组织角度，把本讲只点到为止的 TB 体系讲透。
- **动手建议**：在本讲"综合实践"的基础上，尝试用 `interactive.tcl` 真正编译一次工程（若本地具备 Modelsim + 依赖），观察 `compile_files` 报告的编译顺序，验证你对 `config.tcl` 三组清单的理解。
