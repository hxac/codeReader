# 工具链、依赖与获取方式

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `vivadoIP_spi_simple` 这个 IP **不是孤立项目**，它复用了 PSI（Paul Scherrer Institute）开源的整套 FPGA 库家族中的哪些成员。
- 列出本 IP 的**完整依赖清单**：哪些是「使用 IP 就必需」的，哪些是「只在开发（仿真、打包）时才需要」的，以及各自要求的**最低版本**。
- 理解为什么项目要求一个**固定的目录结构**，以及 `psi_fpga_all` 聚合仓库如何用 Git 子模块（submodule）一次性满足这个结构。
- 读懂 `scripts/dependencies.py` 这个只有十几行的脚本：它如何把 README 当作「唯一数据源」解析，再借助 `PsiFpgaLibDependencies` 包自动拉取依赖。

本讲只读两份文件：`README.md`（依赖契约）和 `scripts/dependencies.py`（自动拉取入口）。不涉及任何 VHDL 源码，是纯粹的「工程化/工具链」主题。

## 2. 前置知识

本讲承接 [u1-l1 项目定位与整体概览](u1-l1-project-overview.md)，默认你已经知道：

- 这是一个**基于 AXI4 寄存器接口的 SPI Master IP-core**，让 FPGA 内的 AXI 主机读写一组寄存器即可完成 SPI 收发。
- 它是 **PSI HDL Library** 家族的一员（家族里还有 `psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage` 等）。

此外，本讲会用到几个软件工程的基础概念，先通俗解释：

- **依赖（dependency）**：一个项目在编译、仿真或运行时，需要用到的**别人写好的代码库**。比如本项目里 SPI 引擎、AXI 从接口、FIFO 这些底层组件都不是从头写的，而是直接调用 `psi_common` 里现成的模块，那么 `psi_common` 就是本项目的依赖。
- **最低版本（minimum version）**：依赖库会持续演进。本项目声明「需要 `psi_common` 3.0.0 或更高版本」，意思是低于 3.0.0 的旧版可能缺少本项目用到的接口，不能保证可用。
- **Git 子模块（submodule）**：Git 仓库 A 可以把另一个 Git 仓库 B「嵌」进自己里面，记录 B 的某个固定提交。这样 clone A 时能连同 B 一起拿到，且 B 的版本被钉死。`psi_fpga_all` 就是用这种方式把一堆相关仓库组织在一起。
- **Python 包（package）**：一段可被 `import` 复用的 Python 代码。本讲的 `PsiFpgaLibDependencies` 就是一个需要先安装的 Python 包，它提供了「解析 README + 拉取依赖」的全部能力。
- **单一数据源（Single Source of Truth, SSOT）**：同一份信息只在**一个地方**维护，其它地方都去读它，避免多处手写导致不一致。本项目把依赖清单写在 README 里，脚本去解析 README，而不是再维护一份独立的依赖列表——这就是 SSOT 思想。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
| --- | --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md) | 项目说明，其中 `Dependencies` 段是**人类与脚本共同读取的依赖契约** | 看依赖清单、目录结构要求、`psi_fpga_all` 说明 |
| [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py) | **自动拉取依赖的入口脚本**（仅 16 行） | 看它如何解析 README 并调用 `PsiFpgaLibDependencies` |

辅助参考文件（不在本讲精读范围内，但与依赖主题相关）：

- [Changelog.md](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md)：记录了 1.1.0 版本「新增依赖解析脚本」这一历史节点。

---

## 4. 核心概念与源码讲解

### 4.1 PSI FPGA 库生态与依赖清单

#### 4.1.1 概念说明

`vivadoIP_spi_simple` 看起来是一个独立的 IP，但它**大量复用**了 PSI HDL Library 家族里其他成员的能力：

- SPI 主控时序引擎、AXI 从接口、同步 FIFO 这些底层组件，来自 VHDL 组件库 `psi_common`；
- 测试平台（testbench）里用的 AXI BFM、断言工具，来自测试库 `psi_tb`；
- 仿真流程（`sim/run.tcl` 背后的框架）由 Tcl 工具 `PsiSim` 提供；
- 把 RTL 打包成 Vivado IP（生成 `component.xml`、GUI）的能力，由 Tcl 工具 `PsiIpPackage` 提供。

所以「装好这个 IP」其实意味着「装好它背后的一整条供应链」。这就是为什么 README 要专门用一节列出依赖清单——这份清单同时服务两类读者：

1. **人类工程师**：照着清单去 clone 对应仓库、放进正确目录。
2. **脚本**：`dependencies.py` 会**机器解析**这份清单，自动 checkout 对应版本。

关键设计：README 在依赖清单前后各放了一行 HTML 注释作为「解析边界标记」，明确告诉脚本「这段 markdown 的格式不能改，改了脚本就解析不出来」。

#### 4.1.2 核心流程

依赖被分成三层（这与 PSI 生态的分层完全对应）：

| 层 | 依赖仓库 | 最低版本 | 用途 | 是否仅开发用 |
| --- | --- | --- | --- | --- |
| **TCL** | PsiSim | 2.1.0 或更高 | 仿真框架（驱动 `sim/run.tcl`） | ✅ 是（for development only） |
| **TCL** | PsiIpPackage | 2.0.0 | IP 打包框架（驱动 `scripts/package.tcl`） | ✅ 是 |
| **VHDL** | psi_common | 3.0.0 或更高 | 可复用 VHDL 组件（AXI slave、FIFO、SPI master 等） | ❌ 否（使用 IP 也必需） |
| **VHDL** | psi_tb | 3.0.0 或更高 | 测试平台辅助库（BFM、断言） | ✅ 是 |
| **VivadoIp** | vivadoIP_spi_simple | — | 本 IP 自身 | ❌ 否 |

> 一个特别值得注意的区分：**只有 `psi_common` 是「使用 IP 时也必需」的运行期依赖**。`PsiSim`、`PsiIpPackage`、`psi_tb` 三个都标注了 `for development only`——也就是说，如果你只是**拿到打包好的 IP 去用**（在自己的 Vivado 工程里例化），并不需要装这三个；只有当你要**跑仿真回归**或**重新打包 IP** 时才需要。

这条区分对实践很重要：它决定了「最小使用依赖」与「完整开发依赖」的边界。

#### 4.1.3 源码精读

README 中被脚本解析的整段依赖契约，由两行 HTML 注释夹住：

[README.md:18-35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18-L35) —— 这就是「单一数据源」。第 18 行的开头注释 `<!-- DO NOT CHANGE FORMAT: ... -->`（[README.md:18](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18)）明确警告：这段格式被脚本解析，不能随意改动。

真正的依赖清单是其中第 26–33 行的三层列表：

[README.md:26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L26-L33) —— 注意每一项都带有**最低版本**和**是否仅开发用**两个标签，这两个标签就是 4.1.2 表格的信息来源，脚本解析时也会读取它们。

#### 4.1.4 代码实践

> **实践目标**：不依赖任何脚本，纯靠阅读 README 把依赖清单整理成「使用 vs 开发」两类。

1. 打开 [README.md:26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L26-L33)。
2. 把每一行依赖拆成四列：**层 / 仓库名 / 最低版本 / 是否 for development only**。
3. 用是否包含 `for development only` 字样，把仓库分成两组。

**预期结果**：

- 「使用 IP 必需」组只有 **psi_common (≥3.0.0)**。
- 「开发必需」组有 **PsiSim (≥2.1.0)、PsiIpPackage (2.0.0)、psi_tb (≥3.0.0)**。
- `vivadoIP_spi_simple` 自身属于 VivadoIp 层，就是当前仓库。

> 不需要运行命令，这是纯阅读实践；分组结论可直接对照 4.1.2 的表格。

#### 4.1.5 小练习与答案

**练习 1**：某同事只想在你的 Vivado 工程里**例化**这个打包好的 IP，并不打算跑仿真或重新打包。他必须 clone 哪些外部仓库？

> **答案**：只需要 `psi_common`（≥3.0.0）。PsiSim、PsiIpPackage、psi_tb 都是 `for development only`，使用成品 IP 时不必安装。

**练习 2**：README 里 PsiIpPackage 写的是 `(2.0.0, for development only )`——注意结尾**没有「or higher」**，而 psi_common 写的是 `(3.0.0 or higher)`。这两种写法在语义上有什么潜在差别？

> **答案**：`or higher` 表示「最低版本 3.0.0，更高的也可以」；而只写 `2.0.0` 不带 `or higher`，字面上更接近「要求 2.0.0 这版」。实际能否用更高版本取决于 `PsiFpgaLibDependencies` 的解析与匹配策略，无法仅凭 README 文本断定，属**待本地验证**。但作为读者，应意识到这两种标注方式传达的「版本约束严格度」不同。

---

### 4.2 要求的目录结构与 psi_fpga_all

#### 4.2.1 概念说明

光有依赖清单还不够。这些依赖仓库 clone 下来后，**放在哪个目录、用什么文件夹名**，也是有讲究的——README 明确要求「文件夹名必须精确匹配」。原因在于：

- 打包脚本 `scripts/package.tcl`（下一讲及 u3-l4 会详讲）会用形如 `add_lib_relative` 的命令，按**相对路径**去隔壁目录找 `psi_common` 等库。如果文件夹名拼错或放错位置，打包时就会找不到依赖。
- 仿真脚本 `sim/config.tcl` 同样按固定相对路径引用源码与库。

换句话说，**目录结构本身是脚本与脚本之间的契约**，名字错了工具链就断了。

但手动一个一个 clone、再保证文件夹名和相对位置完全正确，对新人很繁琐。于是 PSI 提供了一个**聚合仓库 `psi_fpga_all`**：它把所有相关的 FPGA 仓库作为 Git 子模块（submodule），按正确的目录结构组织好。你只要 clone 这一个仓库（并初始化子模块），就能一次性拿到「结构完全正确」的全部依赖。

#### 4.2.2 核心流程

获取依赖有两条等价路径：

```
路径 A（手动，精细控制）
  1. clone vivadoIP_spi_simple
  2. 按 README 要求的目录结构，逐一 clone 各依赖到「精确匹配名字」的同级文件夹
  3. 切到符合最低版本的 tag/commit

路径 B（推荐，一键到位）
  1. clone psi_fpga_all
  2. git submodule update --init --recursive
     （子模块会自动落到正确目录、固定在兼容版本）
```

两条路径最终得到的工作区布局是一致的；`psi_fpga_all` 只是把路径 A 的繁琐步骤预先替你做好了。

> 注意：本仓库自身**没有 `.gitmodules`**（已确认 `git submodule` 输出为空），即 `vivadoIP_spi_simple` 不把任何依赖作为自己的子模块。子模块关系存在于 `psi_fpga_all` 那一侧，是它把本项目（以及 psi_common 等）作为子模块纳入的。

#### 4.2.3 源码精读

README 对目录结构与 `psi_fpga_all` 的说明只有短短两行，但信息密度很高：

[README.md:22-24](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L22-L24) ——

- 第 22 行：声明存在一个**要求的目录结构**，且「**folder names must be matched exactly**」（文件夹名必须精确匹配）。这就是脚本契约的依据。

  > 说明：README 此处写「looks as given below（如下所示）」，但当前版本的 README 文本里这段「目录树」并未以纯文本形式给出（可能是早期版本的渲染图或被精简）。因此**确切的 ASCII 目录树待确认**；规范以 `psi_fpga_all` 仓库的实际布局为准。

- 第 24 行：给出 `psi_fpga_all` 的链接，并解释它「contains all FPGA related repositories as submodules in the correct folder structure」（把所有 FPGA 相关仓库作为子模块放在正确的目录结构里）。

#### 4.2.4 代码实践

> **实践目标**：通过阅读 `psi_fpga_all` 的结构，反推本 IP 期望的目录布局。

1. 用浏览器打开 README 第 24 行的链接：<https://github.com/paulscherrerinstitute/psi_fpga_all>。
2. 观察其根目录：应该能看到 `psi_common`、`psi_tb`、`PsiSim`、`PsiIpPackage`、`vivadoIP_spi_simple` 等文件夹，每个都是一个 Git 子模块。
3. 回到本仓库，确认 `scripts/package.tcl` 与 `sim/config.tcl` 中引用依赖的相对路径（例如是否形如 `../../psi_common/...`），与 `psi_fpga_all` 的布局能否对上。

**需要观察的现象**：

- `psi_fpga_all` 根目录下的子模块名，应与 README 第 26–33 行列出的仓库名**一一对应**。
- 本仓库脚本里引用依赖的相对路径，应能在这个布局下被找到。

**预期结果**：能说出「本 IP 期望 `psi_common` 等库与它在同一个父目录下、且文件夹名就是仓库名」。

> 如果无法访问外网，可改为纯阅读本仓库的 `scripts/package.tcl`，看它用什么相对路径找库，从而**推断**期望布局（标注「待本地验证」即可）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 README 强调「folder names must be matched exactly」？如果有人把 `psi_common` clone 成了 `psi_common_lib`，会发生什么？

> **答案**：因为打包/仿真脚本是按**固定的相对路径和文件夹名**去找依赖的。名字拼错（如多了 `_lib`），脚本在那个路径下找不到库，综合或打包阶段就会报「找不到文件/找不到库」的错误。

**练习 2**：`vivadoIP_spi_simple` 仓库自身**不包含**任何 `.gitmodules`（已确认）。那么 `psi_fpga_all` 是怎么把本仓库纳入其结构的？

> **答案**：`psi_fpga_all` 在**它自己那一侧**把 `vivadoIP_spi_simple`（以及 psi_common 等）注册为自己的子模块，并钉在某个提交上。子模块关系存在于 `psi_fpga_all`，而不是被纳入的各个仓库里——所以本仓库的 `.gitmodules` 为空是正常的。

---

### 4.3 dependencies.py 工作原理

#### 4.3.1 概念说明

有了 4.1 的依赖清单和 4.2 的目录要求，剩下的问题就是：「我能不能不要手动一个个 clone，让脚本自动帮我拉？」

答案就是 `scripts/dependencies.py`。它的设计体现了两个好习惯：

1. **单一数据源（SSOT）**：脚本**不自己再写一份依赖列表**，而是直接去解析 README 的 `Dependencies` 段。这样以后改依赖（比如升 psi_common 到 4.0.0）只需改 README 一处，脚本自动跟上，永远不会出现「README 说 A、脚本拉 B」的不一致。
2. **能力外置**：脚本本身只有十几行，真正的「解析 + 拉取」逻辑都在一个独立的 Python 包 `PsiFpgaLibDependencies` 里。本项目只负责「告诉它 README 在哪、本仓库在哪」，剩下交给通用包。

> 代价：要运行这个脚本，你必须先安装 `PsiFpgaLibDependencies` 这个 Python 包（README 第 43 行明确指出）。它**不是** Python 标准库的一部分。

历史脉络：根据 [Changelog.md:17-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L17-L21)，依赖解析脚本是在 **1.1.0 版本**新增的（同版还把 AXI slave 改为用 `psi_common` 实现）。这说明「依赖管理自动化」是项目工程化到一定阶段才引入的能力。

#### 4.3.2 核心流程

整个脚本可以概括成三步伪代码：

```
# 1. 引入能力（解析器 Parse、执行器 Actions 都来自外部包）
from PsiFpgaLibDependencies import *

# 2. 把 README 当作「唯一数据源」解析，得到结构化的依赖描述
dependencies = Parse.FromReadme(<本仓库>/README.md)

# 3. 在本仓库根目录下，按解析结果执行拉取
Actions.ExecMain(<本仓库根目录>, dependencies)
```

`Parse.FromReadme` 内部会：

- 定位 README 中第 18 行和第 35 行那对 HTML 注释（解析边界）；
- 读出中间的三层列表，抽出每个仓库的**名称、最低版本、是否仅开发用**；
- 返回一个结构化的 `dependencies` 对象。

`Actions.ExecMain` 内部会（具体行为由 `PsiFpgaLibDependencies` 包定义）：

- 根据 `dependencies` 决定要 clone/checkout 哪些仓库、哪些版本；
- 把它们放到符合目录结构要求的位置；
- 具体是 `git clone`、切 tag、还是只做校验，取决于传给脚本的命令行参数（如 `-help` 列出的子命令）。

#### 4.3.3 源码精读

脚本全文只有 16 行，逐段看：

[scripts/dependencies.py:7-7](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L7) —— `from PsiFpgaLibDependencies import *`：把外部包里的 `Parse`、`Actions` 等名字直接导入当前作用域。这也是为什么后面能直接写 `Parse.FromReadme` 而不用加包名前缀。

[scripts/dependencies.py:11-11](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L11) —— `THIS_DIR = os.path.dirname(os.path.abspath(__file__))`：取「本脚本所在目录」的绝对路径（即 `scripts/`）。用 `__file__` 而不是写死路径，保证从任何工作目录调用脚本都能正确定位。

[scripts/dependencies.py:13-13](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L13) —— `Parse.FromReadme(THIS_DIR + "/../README.md")`：**关键的一行**。从 `scripts/` 往上一级（`..`）就是仓库根，再读 `README.md`。这一行把 README 确立为依赖的「唯一数据源」——脚本自己不维护任何依赖列表。

[scripts/dependencies.py:14-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L14-L16) —— 先算出仓库根目录 `repo`，再 `Actions.ExecMain(repo, dependencies)`：在仓库根下，根据上一步解析出的依赖描述，执行真正的拉取/校验动作。

最后，README 给出了运行方式与前置条件：

[README.md:37-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L37-L43) —— 运行命令是 `python dependencies.py -help`（注意是 `-help` 而不是 `--help`），并明确**必须先安装** `PsiFpgaLibDependencies` 包才能运行。

#### 4.3.4 代码实践

> **实践目标**：阅读 `dependencies.py` 并尝试获取它的命令行帮助，理解脚本对外暴露了哪些子命令。

**操作步骤**：

1. 阅读全文（仅 16 行）：[scripts/dependencies.py:1-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L1-L16)。
2. 在终端尝试（需要先装好 `PsiFpgaLibDependencies` 包）：

   ```bash
   cd scripts
   python dependencies.py -help
   ```

3. 如果尚未安装该包，会看到类似 `ModuleNotFoundError: No module named 'PsiFpgaLibDependencies'` 的报错——这正好印证了 README 第 43 行的前置条件。

**需要观察的现象**：

- 装好包后，`-help` 应列出可用的子命令（通常会有「拉取/更新依赖」「仅校验版本」之类的选项）。
- 没装包时，报错信息会精确指向缺失的 `PsiFpgaLibDependencies`。

**预期结果**：

- 能说出脚本「读取哪个文件、调用哪个包的哪两个对象（`Parse`、`Actions`）」。
- 能列出 `-help` 输出里至少一个子命令的名字（若无法本地运行，标注「待本地验证」即可，不要编造子命令名）。

> 注意：本实践**不要求**你真的拉取依赖（那会改动仓库外的工作区）。只要求读懂脚本、能获取帮助信息。

#### 4.3.5 小练习与答案

**练习 1**：如果以后想把 `psi_common` 的最低版本从 3.0.0 升到 4.0.0，开发者需要同时改 `dependencies.py` 吗？

> **答案**：**不需要**。`dependencies.py` 不硬编码任何版本，它是去解析 README 的。所以只需改 README 第 30 行的版本号，脚本下次运行就会按新版本解析。这正是「单一数据源」的好处。

**练习 2**：脚本第 13 行用 `THIS_DIR + "/../README.md"` 来定位 README。如果有人把 `dependencies.py` 从 `scripts/` 挪到了仓库根目录，这行还能正确工作吗？

> **答案**：**不能**。挪到根目录后，`THIS_DIR` 就是仓库根，`../README.md` 会指向**仓库根的上一级**，那里没有 README，解析会失败。这说明脚本对「自己相对于 README 的位置」有隐含假设——它必须待在 `scripts/` 下。这也是为什么项目有固定的目录结构。

**练习 3**：为什么脚本选择 `from PsiFpgaLibDependencies import *`（星号导入），而不是 `import PsiFpgaLibDependencies`？这样做有什么取舍？

> **答案**：星号导入让脚本可以直接写 `Parse.FromReadme`、`Actions.ExecMain`，代码更短、更像「配置文件」。代价是污染了当前命名空间、可读性略差（读者要自己去找 `Parse`/`Actions` 来自哪里）。对于这种「只有十几行的薄封装脚本」，这个取舍是合理的；但在大型项目里一般不推荐 `import *`。

---

## 5. 综合实践

**任务**：假设你是一名新加入的工程师，需要在空机器上从零搭起一个**既能仿真、又能打包** `vivadoIP_spi_simple` 的完整工作区。请写一份「获取依赖」的操作清单，并标注每一步的依据来源（README 行号或 `dependencies.py` 行号）。

要求覆盖：

1. **列出必须获取的外部仓库及最低版本**：分别给出「仅使用 IP」和「完整开发」两组（依据 [README.md:26-33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L26-L33)）。
2. **选择获取方式**：说明你会用「手动按目录结构 clone」还是「直接 clone `psi_fpga_all`」（依据 [README.md:22-24](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L22-L24)），并解释 `psi_fpga_all` 为何能省事（子模块 + 正确目录结构）。
3. **用脚本自动化**：说明运行 `python dependencies.py -help` 的前置条件（必须先装 `PsiFpgaLibDependencies` 包，依据 [README.md:37-43](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L37-L43)），以及脚本内部如何用 README 作为唯一数据源（依据 [scripts/dependencies.py:13-16](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py#L13-L16)）。
4. **验证**：搭好后，如何确认依赖齐全且目录结构正确（例如检查 `scripts/package.tcl` 引用的相对路径下确实存在 `psi_common`）。

**预期产出**：一份可执行的清单，每步都标明「为什么这么干」的源码依据；并在末尾用一句话区分「最小使用依赖」与「完整开发依赖」。

## 6. 本讲小结

- `vivadoIP_spi_simple` **不是孤立项目**，它复用了 PSI HDL Library 家族的四个外部库：TCL 层的 `PsiSim`（仿真）、`PsiIpPackage`（打包）；VHDL 层的 `psi_common`（运行期组件）、`psi_tb`（测试）。
- 只有 **`psi_common` (≥3.0.0)** 是「使用 IP 时也必需」的；`PsiSim` (≥2.1.0)、`PsiIpPackage` (2.0.0)、`psi_tb` (≥3.0.0) 都标注 `for development only`，仅仿真/打包时才需要。
- 项目要求**固定的目录结构**（文件夹名必须精确匹配），因为打包与仿真脚本按相对路径找依赖；`psi_fpga_all` 聚合仓库用 Git 子模块把这套结构预先搭好，clone 一个仓库即可。
- [README.md:18-35](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/README.md#L18-L35) 的依赖段是**单一数据源**：前后两行 HTML 注释标记解析边界，既给人看也给脚本解析。
- [scripts/dependencies.py](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/scripts/dependencies.py) 只有 16 行：用 `Parse.FromReadme` 解析 README，再用 `Actions.ExecMain` 执行拉取；真正的逻辑在外部 Python 包 `PsiFpgaLibDependencies` 里（须先安装）。
- 这套依赖自动化能力在 [Changelog.md:17-21](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/Changelog.md#L17-L21) 的 **1.1.0 版本**引入。

## 7. 下一步学习建议

- 依赖装好后，紧接着就该**跑仿真回归**验证一切正常——这正是 [u1-l4 仿真与回归测试运行方式](u1-l4-simulation-and-regression.md) 的主题，其中 `sim/run.tcl` 背后的 `PsiSim` 就是本讲提到的依赖之一。
- 如果你对「这些 VHDL 依赖（`psi_common`）到底提供了什么组件」好奇，可以在进阶层 [u2 进阶：理解核心实现](u2-l2-spi-core-architecture.md) 中看到 `psi_common_spi_master`、`psi_common_sync_fifo`、`psi_common_axi_slave_ipif` 是如何被本项目直接例化的。
- 关于 `PsiIpPackage` 如何把 RTL 打包成 Vivado IP，见专家层 [u3-l4 IP 打包与发布流程](u3-l4-ip-packaging.md)。
- 想了解 CI 如何用 `PsiSim` 跑回归并自动判定成败，见 [u3-l5 CI、回归与开发工作流](u3-l5-ci-and-dev-workflow.md)。
