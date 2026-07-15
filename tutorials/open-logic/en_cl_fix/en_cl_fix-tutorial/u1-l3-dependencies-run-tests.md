# 依赖、安装与运行测试

## 1. 本讲目标

本讲解决一个最实际的问题：**拿到 en_cl_fix 这个仓库后，如何把它跑起来。**

学完后你应当能够：

1. 说出 en_cl_fix 的三类依赖（Python、MATLAB、VHDL 仿真器）分别是什么、用来干什么。
2. 用 `requirements.txt` 安装 Python 依赖，并独立运行一个 Python 单元测试。
3. 看懂 `sim/run.py` 与 `sim/common.py` 是如何用 VUnit 把 RTL、测试台、cosim 黄金数据装配成一个可运行仿真工程的。
4. 知道如何用 `--simulator` / `--simulator-path` 指定四种仿真器（GHDL、NVC、Modelsim、Questa）中的任意一种。

本讲是「环境与运行」这一层，不涉及定点算法本身。算法语义请参看前置讲义（Python 主接口、目录结构与三语言镜像架构）。

## 2. 前置知识

在开始前，请先回忆前置讲义建立的两个关键认知：

- **三语言镜像架构**：en_cl_fix 的算法在 VHDL（金标准）与 Python（参考模型）中各维护一份，MATLAB 只是薄封装。因此「跑测试」其实有三种独立的入口：Python 测试、MATLAB 测试、VHDL 仿真测试，分别对应三种语言。
- **验证闭环**：VHDL 仿真并不是凭空跑的。它依赖 `bittrue/cosim/` 下的 Python cosim 脚本**先生成黄金输入与期望输出数据文件**，再由 `tb/` 下的测试台逐拍比对 UUT 输出，最后由 `sim/run.py` 把这些零件装配起来交给 VUnit 执行。

理解了「依赖」分三语言、而 VHDL 仿真还要「先跑 cosim 生成数据」这一点，本讲的很多设计就顺理成章了。

此外，请确保你本机已有：

- 一个能联网的终端（用于 `pip install`）。
- （可选）任一支持的 VHDL 仿真器，用于跑 VUnit 仿真。没有也不影响本讲前半部分的 Python 实践。

> 名词速查：
> - **VUnit**：开源的 VHDL 验证框架，负责「编译源码 → 管理测试用例 → 调用底层仿真器 → 汇报结果」。
> - **UUT（Unit Under Test）**：被测单元，这里指 en_cl_fix 的 RTL 实体。
> - **cosim（co-simulation）**：协同仿真，这里特指「用 Python 参考模型生成黄金数据，再喂给 VHDL 仿真比对」。

## 3. 本讲源码地图

本讲涉及的关键文件都在仓库顶层，且都很短小，适合通读：

| 文件 | 行数级 | 作用 |
| --- | --- | --- |
| `requirements.txt` | 2 行 | 锁定 Python 依赖的精确版本。 |
| `README.md` | — | `Dependencies` 与 `Running Tests` 两节是权威的安装/运行说明。 |
| `sim/common.py` | ~108 行 | 解析自定义命令行参数、设置仿真器环境变量、选择 VHDL 标准。 |
| `sim/run.py` | ~253 行 | 用 VUnit 装配整个仿真工程（库、源文件、每个 TB 的配置、各仿真器选项）。 |
| `sim/cosim_runner.py` | ~73 行 | 线程安全地「最多执行一次」cosim 脚本的封装，被 `run.py` 调用。 |
| `bittrue/tests/python/cl_fix_round_test.py` | ~135 行 | 本讲实践的 Python 单元测试，用 numpy 参考实现校验 `cl_fix_round`。 |

> 本讲只读不改这些文件。后续讲义会深入它们的算法细节，本讲只关心「它们如何被装起来跑」。

## 4. 核心概念与源码讲解

### 4.1 Python 依赖与 requirements.txt

#### 4.1.1 概念说明

en_cl_fix 的 Python 实现既是「参考模型」（给 VHDL 当黄金对照），也是可以直接拿来用的定点计算库。要让它跑起来，需要两类 Python 依赖：

- **numpy**：Python 实现内部大量使用 numpy 数组做向量化定点运算（例如测试里用 `np.arange` 穷举所有取值、用 `np.floor` 做舍入参考）。
- **vunit-hdl**：Python 的 VUnit 包。注意，VUnit 是一个 **Python 包 + 底层调用 VHDL 仿真器** 的组合——`pip install vunit-hdl` 装的只是 Python 这一半，真正的仿真还要靠系统里装的 GHDL/NVC/Modelsim 等。

仓库用 `requirements.txt` 把这两个依赖的版本**精确钉死**，保证所有人拿到一致的运行环境。

#### 4.1.2 核心流程

安装流程是标准 pip 流程：

1. 确认 Python 版本满足要求（README 要求 ≥ 3.10）。
2. 在仓库根目录执行 `python -m pip install -r requirements.txt`。
3. pip 读取 `requirements.txt`，逐行安装并锁定到指定版本。

为何用 `python -m pip` 而不是直接 `pip`？因为前者能保证用的是「当前 `python` 对应的那一份 pip」，避免多版本 Python 环境下装错地方。

#### 4.1.3 源码精读

整个依赖清单只有两行，极其精简：

- [requirements.txt:1-2](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/requirements.txt#L1-L2) 锁定了 `numpy==2.3.2` 与 `vunit-hdl==5.0.0.dev6` 两个精确版本。

注意 `vunit-hdl` 用的是一个 `5.0.0.dev6` 开发版（带 `.devN` 后缀），说明项目紧跟 VUnit 的开发分支。这也是为什么 README 里写的「最低测试版本」是 `>= 5.0.0.dev6`——这是经 CI 验证可用的基线。

对应的 README 安装说明在这里：

- [README.md:53-55](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L53-L55) 给出官方安装命令 `python -m pip install -r requirements.txt`。

而 Python 本身的版本要求：

- [README.md:44-49](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L44-L49) 写明 Python 3（≥ 3.10），以及 numpy（≥ 1.24.3）、vunit-hdl（≥ 5.0.0.dev6）两个包。

#### 4.1.4 代码实践

**实践目标**：确认 Python 依赖可被正确解析与安装（不一定真装，先做一次「干读」）。

**操作步骤**：

1. 在仓库根目录查看当前 Python 版本：`python --version`。
2. 不实际安装，只看 pip 会装哪些包：`python -m pip install --dry-run -r requirements.txt`。
3. （可选，真正安装）`python -m pip install -r requirements.txt`。

**需要观察的现象**：

- 第 2 步的 dry-run 会列出即将下载的 `numpy` 与 `vunit-hdl`，以及它们依赖的子包（如 vunit-hdl 会带进 `colorama` 等）。
- 如果你的 Python < 3.10，numpy 2.x 的安装可能在解析阶段就报兼容性错误。

**预期结果**：dry-run 输出中能看到 `numpy==2.3.2` 与 `vunit-hdl==5.0.0.dev6` 两个目标包。若已安装则标记为 `already satisfied`。

> 不同网络与平台下 pip 的解析结果可能略有差异，具体子包列表「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么项目要把版本写成 `==2.3.2` 而不是 `>=2.3.2`？

> **参考答案**：用 `==` 钉死精确版本，是为了让所有开发者与 CI 拿到**完全一致**的运行环境，避免 numpy/VUnit 升级引入的语义差异（定点运算是位级精确的，依赖版本的微小差异都可能让参考模型行为漂移，从而让 cosim 比对假性失败）。

**练习 2**：`vunit-hdl` 装好后，是否就能直接仿真 VHDL 了？

> **参考答案**：不能。`vunit-hdl` 只是 Python 这一半，它本身不含仿真器引擎；还必须在系统里另行安装 GHDL / NVC / Modelsim / Questa 之一，并通过 `--simulator-path` 告诉 VUnit 该仿真器二进制在哪里。

---

### 4.2 README 中的依赖全景与 Running Tests 章节

#### 4.2.1 概念说明

上一节只看了 Python 一类依赖。但 en_cl_fix 是三语言库，依赖也分三类。README 的 `Dependencies` 小节是这三类依赖的**权威清单**，而 `Running Tests` 小节则是三类测试各自的**运行入口**。这一节我们把全景一次性看清楚，作为后续两节（`common.py` 与 `run.py`）的背景。

#### 4.2.2 核心流程

三类依赖 → 三类测试的对应关系如下：

```
Python 依赖 (numpy, vunit-hdl)  ──►  Python 测试   ./bittrue/tests/python/*.py
MATLAB  (R2023b 等)             ──►  MATLAB 测试   ./bittrue/tests/matlab/*.m
VHDL 仿真器 (GHDL/NVC/Modelsim) ──►  VHDL 测试     ./tb/*.vhd，经 ./sim/run.py 跑
```

其中 Python 测试最独立——只要装好 numpy 就能直接 `python xxx_test.py` 跑。VHDL 测试最复杂，需要先有 cosim 生成的黄金数据，再由 `run.py` 装配，最后才交给仿真器。

#### 4.2.3 源码精读

**MATLAB 依赖**（最轻量，只是一个版本要求）：

- [README.md:57-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L57-L59) 写明「Tested with MATLAB R2023b, and others」。

**VHDL 仿真器依赖**（重点）：

- [README.md:61-65](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L61-L65) 说明「所有 VUnit 支持的现代 VHDL 仿真器都应可用」，并给出经过测试的清单：GHDL 4.1.0、NVC 1.17.1、Modelsim ASE/AE/ME/PE 多个版本、Questa FE 2023.4。

这里有两个免费开源仿真器（GHDL、NVC）和两个商用仿真器（Modelsim、Questa）。注意 Questa 在 VUnit 里需要特殊处理——见 4.3 节。

**Running Tests 章节**（三类测试的运行入口）：

- [README.md:192-193](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L192-L193) Python 测试位于 `./bittrue/tests/python/`，直接 `python cl_fix_round_test.py` 运行。
- [README.md:194](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L194) MATLAB 测试位于 `./bittrue/tests/matlab/`。
- [README.md:195-202](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L195-L202) VHDL 测试台位于 `./tb/`，需从 `./sim/` 目录用 `run.py` 驱动，并给出 GHDL、NVC、各 Modelsim 版本、Questa 的命令行示例。

注意示例命令里的两个参数 `--simulator` 与 `--simulator-path`——这正是 `common.py` 自定义添加的参数，下一节详讲。

还有一个贯穿全局的设计约束值得记住：

- [README.md:13-17](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/README.md#L13-L17) 说明 **RTL 代码遵循 VHDL-93**（为最大综合兼容性），**测试台遵循 VHDL-2008**。这个「RTL-93 / TB-2008」的双标准会在 4.3 节被 `common.py` 落实成具体的编译选项。

#### 4.2.4 代码实践

**实践目标**：建立「依赖与测试一一对应」的全局地图。

**操作步骤**：

1. 打开 `README.md`，定位到 `Dependencies`（约第 42 行）与 `Running Tests`（约第 190 行）两节。
2. 在本机上检查你已具备哪些依赖：`python -c "import numpy; print(numpy.__version__)"`；若装了 MATLAB 或任一 VHDL 仿真器，记录其版本。
3. 对照 README 的测试清单，确认你能跑哪一类测试。

**需要观察的现象**：你能清楚说出「我能跑 Python 测试吗？能跑 VHDL 仿真吗？」并给出依据。

**预期结果**：列出本机当前可运行的测试类别。缺哪类依赖就对应补哪类。本实践不修改任何文件，纯梳理。

#### 4.2.5 小练习与答案

**练习 1**：为什么 RTL 用 VHDL-93、而测试台用 VHDL-2008？

> **参考答案**：RTL 要被各种综合工具（Vivado/Quartus 等）吃下去，而许多综合工具对 VHDL-2008 支持有限，所以 RTL 退守到兼容性最好的 VHDL-93；测试台不需要综合，可以用 VHDL-2008 带来的便利特性（如分级层次、OSVVM、protected type 等）来简化验证代码。

**练习 2**：README 给的 GHDL 示例命令是 `python run.py --simulator=ghdl --simulator-path='C:/msys64/mingw64/bin'`，这个路径为什么是 Windows 风格？

> **参考答案**：因为 GHDL 在 Windows 上常用 MSYS2/MinGW 安装，`C:/msys64/mingw64/bin` 是 GHDL 可执行文件所在目录。`--simulator-path` 指向的就是「仿真器二进制的安装位置」，在 Linux 上则应改成类似 `/usr/bin` 的路径。

---

### 4.3 common.py：自定义命令行参数与仿真器适配

#### 4.3.1 概念说明

`sim/common.py` 是 `run.py` 的「辅助大脑」。它做三件事：

1. **导入 VUnit**：但不是简单的 `import vunit`，而是先把仓库自带的一份 VUnit（位于 `lib/FW/VHDL/vunit`）插到 `sys.path` 前面，从而优先使用项目自带的版本。
2. **添加自定义命令行参数**：VUnit 自带一套参数（如 `--list`、`--verbose`），但项目还需要 `--simulator`、`--simulator-path` 等参数——这些是 `common.py` 自己挂上去的。
3. **把参数翻译成环境变量与 VHDL 标准**：VUnit 靠环境变量（`VUNIT_SIMULATOR` 等）识别仿真器，`common.py` 负责把命令行参数写进这些环境变量；同时根据仿真器决定 RTL/TB 用哪个 VHDL 标准。

#### 4.3.2 核心流程

```
common.py 导入
        │
        ▼
注册 5 个自定义参数 (--simulator / --simulator-path / --vendor-lib / --coverage / --disable-cosim)
        │
        ▼
cli.parse_args()  ──►  校验 simulator / simulator-path 必填
        │
        ▼
设置 VUNIT_SIMULATOR / VUNIT_*_PATH 环境变量（含 questa→modelsim 的规避）
        │
        ▼
根据仿真器选定 vhdl_standard_rtl / vhdl_standard_tb（93 或 2008）
        │
        ▼
（仿真结束后）post_run 回调：合并覆盖率报告
```

每个自定义参数都支持「命令行优先、环境变量兜底」的双来源：例如 `--simulator` 没传时，会去读环境变量 `EN_SIM_NAME`。这让 CI 可以用环境变量配置，而人可以临时用命令行覆盖。

#### 4.3.3 源码精读

**自带 VUnit 的导入**（注意路径优先级）：

- [sim/common.py:25-27](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L25-L27) 用 `sys.path.insert(1, ...)` 把 `lib/FW/VHDL/vunit` 插到搜索路径前面，再 `from vunit import VUnitCLI, VUnit`。所以即便你 `pip` 装了 vunit-hdl，这里也会优先用仓库自带的那一份。

**五个自定义参数**（命令行 + 环境变量兜底）：

- [sim/common.py:31-35](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L31-L35) `--simulator`：仿真器名，取值 `modelsim/questa/nvc/ghdl`，缺省读 `EN_SIM_NAME`。
- [sim/common.py:36-41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L36-L41) `-s/--simulator-path`：仿真器二进制所在目录，缺省读 `EN_SIM_BIN`。
- [sim/common.py:42-46](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L42-L46) `--vendor-lib`：厂商预编译仿真库路径，缺省读 `EN_SIM_LIB`。
- [sim/common.py:47-53](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L47-L53) `-c/--coverage`：开关，启用仿真覆盖率。
- [sim/common.py:54-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L54-L59) `--disable-cosim`：开关，禁用 cosim 脚本自动执行（调试时用）。

**必填校验**（不传就报错退出）：

- [sim/common.py:65-68](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L65-L68) 若 `--simulator` 或 `--simulator-path` 任一为空，直接 `raise Exception` 并给出用法提示。这就是为什么 README 的示例命令两个参数都要给。

**环境变量设置 + Questa 规避**：

- [sim/common.py:71-79](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L71-L79) 把 `args.simulator` 写入 `VUNIT_SIMULATOR`，把路径分别写入 `VUNIT_MODELSIM_PATH` / `VUNIT_GHDL_PATH` / `VUNIT_NVC_PATH`。其中注释点出一个关键规避：**VUnit 当时还不接受 `questa` 作为合法仿真器名**，所以对于 Questa，环境变量里仍写成 `modelsim`，只是路径指向 Questa 的安装目录。

**VHDL 标准选择**（落实 README 的 RTL-93/TB-2008 约定）：

- [sim/common.py:82-87](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L82-L87) 对 modelsim/questa：`rtl=93, tb=2008`；对 ghdl/nvc：`rtl=2008, tb=2008`。这两个变量 `vhdl_standard_rtl` / `vhdl_standard_tb` 会被 `run.py` 在添加源文件时引用（见 4.4 节）。

**仿真后回调**（合并覆盖率）：

- [sim/common.py:92-108](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L92-L108) `post_run` 在所有测试跑完后被调用：若开启了 `--coverage` 且用的是 questa/modelsim，则合并 `.ucdb` 覆盖率数据并生成报告；若用 nvc，则打印「暂不支持覆盖率」的警告。

#### 4.3.4 代码实践

**实践目标**：不真正跑仿真，只验证 `common.py` 的参数解析与错误提示是否如预期。

**操作步骤**：

1. 进入 `sim` 目录：留意 `run.py` 第一行 `import common` 是「裸导入」，必须在 `sim/` 目录内执行才能找到 `common.py`。
2. 故意不传必填参数，触发校验报错：
   ```
   cd sim
   python run.py
   ```
3. 阅读终端打印的 `ERROR: please use --simulator ...` 提示，对照 [sim/common.py:66](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L66)。
4. （可选）只传 `--simulator=ghdl` 不传 path，观察是否触发第二处校验 [sim/common.py:67-68](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L67-L68)。

**需要观察的现象**：脚本在校验阶段就因 `Exception` 退出，不会进入真正的编译/仿真。报错信息会同时列出合法的仿真器取值与一个示例命令。

**预期结果**：看到形如 `ERROR: please use --simulator <name> ... Allowed values: modelsim, questa, nvc or ghdl` 的提示。

> 若你本机未安装任何仿真器，这一步仍可执行——因为校验发生在调用仿真器之前。具体报错文案「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `--simulator` 的默认值写成 `environ["EN_SIM_NAME"] if "EN_SIM_NAME" in environ else None`，而不是直接给一个固定默认值？

> **参考答案**：这样实现了「命令行优先、环境变量兜底」的双来源策略。CI 里可以 `export EN_SIM_NAME=ghdl`，本地开发者则用 `--simulator=xxx` 临时覆盖。若两者都没有，则为 `None`，触发后面的必填校验，避免静默用错仿真器。

**练习 2**：使用 Questa 时，`VUNIT_SIMULATOR` 环境变量被设成了什么？为什么？

> **参考答案**：被设成了 `modelsim`（而非 `questa`）。因为 VUnit 当时只识别 `modelsim` 这个名字、尚不接受 `questa` 作为合法仿真器名。规避办法是名字用 `modelsim`，但 `VUNIT_MODELSIM_PATH` 指向 Questa 的安装目录，这样 VUnit 仍会调用到 Questa 的 vsim。

---

### 4.4 run.py：VUnit 工程装配与仿真器路径参数

#### 4.4.1 概念说明

如果说 `common.py` 负责「解析参数、配置仿真器」，那么 `run.py` 就负责「用 VUnit 把整个工程装起来」。它的核心函数 `create_test_suite(vu, args)` 干了四件事：

1. 添加 VUnit 自带库（OSVVM、随机化等）与 `en_tb` 测试台基础子库。
2. 创建工程库 `lib`，把 RTL、测试台工具包、各 TB 源文件按不同 VHDL 标准加进去。
3. 为**每一个**测试台绑定一个 cosim 脚本（通过 `pre_config` 回调），让仿真前先跑 Python 生成黄金数据。
4. 为 GHDL / NVC / Modelsim / Questa 分别设置编译与仿真选项。

#### 4.4.2 核心流程

`run.py` 主入口与工程装配的简化流程：

```
__main__
  args = common.args              # 复用 common.py 解析好的参数
  vu = VUnit.from_args(args)
  vu.add_vhdl_builtins()
  create_test_suite(vu, args)     # 见下
  vu.main(post_run=common.post_run)
        │
        ▼  create_test_suite 内部
  add OSVVM / verification_components / random
  add_library("en_tb")  ←─ 不可综合测试台基础子库
  add_library("lib")
     ├─ ../hdl/*.vhd          (RTL, vhdl_standard_rtl)
     ├─ ../tb/util/*.vhd      (fileio 工具包, vhdl_standard_tb)
     └─ ../tb/*.vhd           (各 TB, vhdl_standard_tb)
  对每个 TB：add_config(pre_config=cosim_xxx.run)
  设置各仿真器 compile/sim 选项
```

关键点：**`pre_config` 回调**是 VHDL 仿真与 Python 参考模型之间的桥梁。VUnit 在跑某个 TB 前，会先调用对应的 `cosim_xxx.run`，该回调在内部用线程锁保证「最多执行一次」地运行 Python cosim 脚本，生成黄金数据文件；随后 TB 才读取这些数据进行逐拍比对。

#### 4.4.3 源码精读

**导入与主入口**：

- [sim/run.py:21-28](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L21-L28) 导入 `common`（含 `VUnit`、`VUnitCLI`、`vhdl_standard_rtl`、`vhdl_standard_tb`）与 `cosim_runner`。
- [sim/run.py:239-252](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L239-L252) `__main__`：取 `common.args`，`VUnit.from_args`，`add_vhdl_builtins`，调 `create_test_suite`，最后 `vu.main(post_run=common.post_run)`。

**添加 VUnit 库与 en_tb 子库**：

- [sim/run.py:32-34](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L32-L34) 添加 OSVVM、验证组件、随机化库。
- [sim/run.py:37-41](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L37-L41) 添加 `en_tb` 库（用 `try/except ValueError` 容错，避免重复添加）。

**创建 lib 库并按 VHDL 标准分类加入源文件**：

- [sim/run.py:44-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L44-L50) 这是「RTL-93 / TB-2008」约定的落地处：`../hdl/*.vhd` 用 `vhdl_standard_rtl`，`../tb/util/*.vhd` 与 `../tb/*.vhd` 用 `vhdl_standard_tb`。

**为每个 TB 绑定 cosim 脚本**（核心模式）：

- [sim/run.py:56-63](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L56-L63) 定义 `COSIM_PATH` 指向 `../bittrue/cosim`，并用一个内部类 `cosim` 把通用 `cosim_runner` 特化到具体子目录。
- 以 `cl_fix_round` 为例：[sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165) 取 `cl_fix_round_tb`，对 `meta_width` 取值 `[0, 8]` 各 `add_config`，并把 `pre_config=cl_fix_round_cosim.run` 绑上去。其余 `add/sub/mult/...` 等 TB 结构几乎一致（[sim/run.py:65-204](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L65-L204)）。

**cosim 只跑一次的保证**（线程安全）：

- [sim/cosim_runner.py:25-26](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L25-L26) 用一个全局 `Lock` 保证同一时刻只有一个 cosim 脚本被加入 `sys.path`（因为它们都叫 `cosim.py`，文件名会冲突）。
- [sim/cosim_runner.py:60-72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L60-L72) `run()` 用「双重检查 + 自禁用」模式：第一次执行后把 `self.enable` 置 `False`，从而即便 VUnit 多次回调也只真正跑一次。

**各仿真器的编译/仿真选项**：

- [sim/run.py:210-229](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L210-L229) 分别为 GHDL（`--warn-no-hide`、`-frelaxed` 等）、NVC（`--relaxed --check-synthesis`、堆栈 `-M 256m`）、Modelsim/Questa（`vcom_flags` 含 `-check_synthesis`、覆盖率 `+cover=sbceft` 等）设置选项，并统一 `disable_ieee_warnings`。这些选项体现了项目对不同仿真器特性的针对性适配。

#### 4.4.4 代码实践

**实践目标**：纯源码阅读型实践——画出 `cl_fix_round` 这个 TB 从「命令行参数 → cosim 数据 → UUT 仿真」的完整装配链。

**操作步骤**：

1. 在 [sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165) 找到 `cl_fix_round` 配置块，记录它生成了几个 config（提示：`meta_width` 取了几个值？）。
2. 顺着 `cl_fix_round_cosim.run` → [sim/cosim_runner.py:60-72](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/cosim_runner.py#L60-L72)，确认这个回调何时被 VUnit 调用、调用几次。
3. 回到 [sim/run.py:44-50](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L44-L50)，确认 `cl_fix_round_tb.vhd` 是被加进哪个库、用哪个 VHDL 标准编译的。

**需要观察的现象**：你能用一句话说清「为什么 `meta_width` 要取 `[0, 8]` 两个值」——因为这会让同一个 TB 以两个不同的 generic 配置各跑一遍，覆盖「无 meta 旁路」和「8 位 meta 旁路」两种情况。

**预期结果**：画出一条链路：

```
run.py main
  └─ create_test_suite
       └─ cl_fix_round_tb: add_config(meta_width_g=0/8, pre_config=cosim.run)
                              │ 仿真前回调
                              ▼
                       cosim_runner.run() ──► cosim.py 生成黄金数据 (data/*.txt)
                              │
                              ▼
                       VUnit 编译 lib(cl_fix_round_tb, VHDL-2008) + hdl(RTL, 93/2008)
                              │
                              ▼
                       仿真器逐拍比对 UUT 输出 vs 黄金数据
```

> 这一条链路的「实际仿真输出」依赖你本机的仿真器，待本地验证；但装配逻辑本身可完全通过阅读源码确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cosim_runner.run()` 要用「双重检查 + 自禁用（`self.enable = False`）」而不是简单的「执行一次」？

> **参考答案**：VUnit 可能并发（多线程）地为同一 TB 的多个 config 调用 `pre_config`，而黄金数据只需生成一次。双重检查（先看 `enable` 再加锁、加锁后再看一次）既避免了无谓的锁等待，又防止了「等待锁期间另一个线程已经跑过」导致的重复执行；执行完立刻自禁用，保证幂等。

**练习 2**：`cl_fix_round` 与 `cl_fix_add` 两个 TB 在 `run.py` 里的配置块有什么不同？

> **参考答案**：`cl_fix_add`（[sim/run.py:68-74](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L68-L74)）只配了一个默认 config（generics 为空）；而 `cl_fix_round`（[sim/run.py:156-165](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/run.py#L156-L165)）对 `meta_width_g` 取 `[0, 8]` 两个值，生成了两个 config。原因是 round/saturate/resize 这三个组件有 `meta_width_g` 这个边带位宽参数，需要分别验证。

---

## 5. 综合实践

设计一个贯穿本讲的任务：**从零跑通 Python 测试，并（在可选时）启动一次 VHDL 仿真**。

**任务目标**：在你本机上把 en_cl_fix 的「最小编译/运行闭环」跑起来，亲历三类依赖中的至少一类。

**步骤 A（必做）：运行 Python 单元测试**

1. 确认 Python ≥ 3.10：`python --version`。
2. 安装依赖：`python -m pip install -r requirements.txt`。
3. 进入测试目录并运行舍入测试：
   ```
   cd bittrue/tests/python
   python cl_fix_round_test.py
   ```
4. 观察输出。该脚本 ([bittrue/tests/python/cl_fix_round_test.py:135](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py#L135)) 成功时会打印一行 `Completed N tests.`；若任一 `assert np.array_equal(...)` 失败，则会抛出 `AssertionError` 并带堆栈退出。
5. 同样试跑 `cl_fix_saturate_test.py` 与 `format_tests.py`，体会三类测试的覆盖面。

**预期结果**：每个测试脚本都以 `Completed ... tests.` 结束、退出码为 0。具体 N 值「待本地验证」（取决于穷举的格式组合数量）。

**步骤 B（可选）：启动 VHDL 仿真装配**

> 仅当你本机装有 GHDL/NVC/Modelsim/Questa 之一时进行。

1. 确认仿真器二进制在 PATH 中，或记录其安装目录。
2. 进入 `sim` 目录并指定仿真器（以 GHDL 为例，路径换成你本机的）：
   ```
   cd sim
   python run.py --simulator=ghdl --simulator-path=/usr/bin
   ```
3. 观察终端：VUnit 会先编译 `en_tb`、RTL、各 TB，期间 `pre_config` 回调会触发各 cosim 脚本生成黄金数据，然后逐个跑 TB。
4. 若只想看有哪些测试而不真跑，加 VUnit 自带参数 `--list`；若想跑单个 TB，用 `-v` 配合 `--filter`（VUnit 标准用法，具体语法「待本地验证」）。

**预期结果**：所有测试通过，VUnit 汇报 `pass`。首次编译较慢（需编译 OSVVM、en_tb、全部 RTL/TB）；后续增量编译会快很多。

**步骤 C（选做）：用 `--disable-cosim` 隔离问题**

如果你怀疑是 cosim 数据生成出了问题，可以用 [sim/common.py:54-59](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/sim/common.py#L54-L59) 提供的 `--disable-cosim` 跳过 Python cosim 执行（此时 TB 会复用上次生成的数据文件），从而把「Python 侧问题」与「VHDL 侧问题」隔离开。请描述什么场景下你会用到这个开关。

> 参考答案：当黄金数据已生成、只想快速重跑 VHDL 仿真时；或当 cosim 脚本本身有 bug、想先确认 TB 编译是否通过时。

## 6. 本讲小结

- en_cl_fix 的依赖分三类：**Python**（numpy + vunit-hdl，由 `requirements.txt` 精确锁定）、**MATLAB**（R2023b 等）、**VHDL 仿真器**（GHDL/NVC/Modelsim/Questa，经 VUnit 驱动）。
- `requirements.txt` 只有两行 `numpy==2.3.2` 与 `vunit-hdl==5.0.0.dev6`，安装用 `python -m pip install -r requirements.txt`。
- `sim/common.py` 给 VUnit 挂上五个自定义参数（`--simulator` / `--simulator-path` / `--vendor-lib` / `--coverage` / `--disable-cosim`），并把它们翻译成 `VUNIT_SIMULATOR` 等环境变量与 RTL-93/TB-2008 的 VHDL 标准选择。
- `sim/run.py` 用 `create_test_suite` 把 OSVVM、`en_tb` 子库、RTL、各 TB 装进 VUnit 工程，并为每个 TB 绑定 `pre_config=cosim.run` 回调，使仿真前先由 Python 生成黄金数据。
- `cosim_runner` 用全局线程锁 + 双重检查 + 自禁用，保证同名 `cosim.py` 脚本线程安全且最多执行一次。
- 三类测试入口：Python 用 `python bittrue/tests/python/xxx_test.py`；VHDL 用 `cd sim && python run.py --simulator=... --simulator-path=...`。

## 7. 下一步学习建议

环境跑通后，建议按以下顺序深入：

1. **先吃透数据模型**：进入下一单元学习 `FixFormat [S,I,F]` 定点表示（对应讲义 u2-l1），这是理解一切算术与转换的前提。
2. **读懂一个 Python 测试**：回到 [bittrue/tests/python/cl_fix_round_test.py](https://github.com/open-logic/en_cl_fix/blob/e9123a9ca65d0966f0c1a567e2afbfa8443b38c6/bittrue/tests/python/cl_fix_round_test.py)，结合 u2 的舍入模式，看懂 `round_check` 如何用 numpy 实现七种舍入的参考实现——这是「Python 参考模型」最直观的范例。
3. **跟踪一个 cosim 闭环**：等学完 Python 主接口（u4）与 VHDL 包（u5）后，再回到 `sim/run.py` + `bittrue/cosim/cl_fix_round/cosim.py` + `tb/cl_fix_round_tb.vhd`，完整跟踪「Python 生成黄金数据 → VHDL 逐拍比对」这一闭环（对应专家层 u7）。
4. **如需 MATLAB**：参阅 `bittrue/models/matlab/` 下的薄封装，理解它如何经 `py.en_cl_fix_pkg.*` 调用 Python（对应 u8）。
