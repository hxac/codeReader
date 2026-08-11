# Vivado 工程模板与版本化工作流

## 1. 本讲目标

本讲解决一个具体问题：**一个 Vivado 工程那么庞大（动辄成千上万个中间文件），到底哪些文件该提交进 Git，哪些不该？**

学完本讲，你应当能够：

1. 说出 Vivado 工程「最小化版本控制（minimal versioning）」的核心思想，并知道它源自 Xilinx 官方文档 UG892。
2. 看懂 `HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/` 这套目录约定，分清 `proj/`、`src/`、`hdl/`、`repo/`、`sdk/`、`hw_handoff/`、`ip_repo/` 各自放什么。
3. 读懂 `create_project.tcl` 是如何用一段 Tcl 脚本「从无到有」把整个工程重建出来的。
4. 读懂 `cleanup.sh` / `cleanup.cmd` 两个清理脚本，并意识到**随仓库发布的脚本里可能带有 bug 和待适配的硬编码路径**，不能盲目照抄。
5. 理解 `.gitignore` 才是「最小化版本控制」的真正执行者。

本讲不涉及 AES 算法本身（那是 Unit 2 的内容），只讲「工程怎么管」。

## 2. 前置知识

在进入源码前，先建立几个直觉。

### 2.1 Vivado 工程为什么「又大又乱」

一个 Vivado 工程在磁盘上通常长这样：一个 `.xpr` 工程文件，加上 `.cache/`、`.data/`、`.runs/`、`.hw/`、`.gen/` 等一堆目录。这些目录里装的全是**综合、实现、仿真过程中生成的中间产物**——网表、日志、报告、比特流、临时文件。

这些文件有三个特点：

- **可重新生成**：只要你有源码和工程设置，工具就能再跑一遍把它们生成出来。
- **体积巨大**：很容易几百 MB 到几个 GB。
- **与机器/工具版本强相关**：换一台电脑、换一个 Vivado 版本，它们往往就不兼容了。

把这种东西提交进 Git，版本库会迅速膨胀，合并时还会频繁冲突。所以业界共识是：**只版本控制「源」和「设置」，不版本控制「生成的产物」**。

### 2.2 三个关键名词

- **Tcl**：一种脚本语言。Vivado 的所有图形界面操作背后，本质都是在执行 Tcl 命令。所以「用脚本描述工程」是完全可行的——这正是 `create_project.tcl` 做的事。
- **xpr 文件**：Vivado 的工程描述文件（XML 格式）。它记录了工程里有哪些源文件、目标器件、综合/实现策略等。**它本身不版本控制**，因为它由脚本重新生成。
- **UG892**：Xilinx 官方文档《Vivado Design Flows Overview》，其中专门讨论了「如何把 Vivado 工程纳入版本控制」的几种推荐做法，本仓库采用的「脚本重建法」就是其中之一。

### 2.3 本讲在整本手册中的位置

本讲承接 [u1-l2](u1-l2-directory-map.md)：你已经知道仓库分 `HDL/HLS/ThreePart` 三条路线，并且知道 `HDL/AesCryptoCore_1.0/` 的 RTL 在 `hdl/src/`、AXI 包装在 `ip_repo/`。本讲深入这套工程的「骨架」——它如何被组织、如何被重建。理解了它，你才能在后续 [Unit 2](u2-l1-aes-top-architecture.md)（AES 数据通路）和 [Unit 3](u3-l1-vivado-ip-structure.md)（AXI IP 封装）里知道「这段代码在工程里处于什么位置」。

## 3. 本讲源码地图

本讲只盯住 `Vivado_project/` 这个工程根目录，涉及的关键文件如下：

| 文件 / 目录 | 作用 | 本讲关注点 |
| ----------- | ---- | ---------- |
| `readme.md` | 工程模板的说明书，阐述最小化版本控制思想与目录约定 | 总纲与工作流定义 |
| `proj/create_project.tcl` | **唯一**描述工程设置的脚本，负责重建整个工程 | 重建流程的每一步 |
| `proj/cleanup.sh` | Linux 下的清理脚本 | 清理边界与脚本中的坑 |
| `proj/cleanup.cmd` | Windows 下的清理脚本 | 用「只读属性」保护关键文件 |
| `.gitignore` | 真正决定「什么文件进 Git」的规则文件 | 最小化版本控制的落地机制 |
| `src/bd/`、`src/constraints/`、`src/ip/` | 块设计、约束、IP 实例 | 目录约定实例 |
| `hdl/` | AES 核心 RTL（算法源码） | 与模板标准布局的差异 |
| `hw_handoff/`、`repo/`、`sdk/` | 硬件交接文件、板级文件、软件工程 | 目录约定实例 |

> 提示：本讲引用的代码行号均基于当前 HEAD `1e33525`。Vivado 脚本和 README 偶尔会被作者改动，行号可能漂移；请以永久链接指向的版本为准。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **模块一：工程目录约定与最小化版本控制思想**（讲「为什么这么分目录」）
- **模块二：`create_project.tcl`——用脚本重建工程**（讲「工程怎么被造出来」）
- **模块三：`cleanup` 脚本——清理生成的工程内容**（讲「工程怎么被拆掉重来」）

### 4.1 工程目录约定与最小化版本控制思想

#### 4.1.1 概念说明

`Vivado_project/readme.md` 第一句话就点明了这套目录结构的设计目的：

> This directory structure serves as a template for versioning Vivado project with minimum set of sources.

也就是说，整个 `Vivado_project/` 目录是一套**模板**，目标是「用最少的源文件就能把工程纳入版本控制」。这套做法是 Xilinx 在 UG892 中推荐的方案之一。

核心思想可以用一句话概括：

> **版本控制「源」和「设置」，不版本控制「产物」。产物由脚本重新生成。**

其中：

- 「源」= 手写的 HDL、约束文件 `.xdc`、块设计的 Tcl 导出、IP 的 `.xci` 定制文件。
- 「设置」= 用一段 Tcl 脚本（`create_project.tcl`）描述的目标器件、综合/实现策略等。
- 「产物」= `.xpr` 工程文件、`.cache/`、`.runs/`、综合网表、比特流 `.bit`、日志等。

#### 4.1.2 核心流程

`readme.md` 用一张表规定了「每一类内容该怎么版本控制」。把它提炼成下面的判定流程（针对任意一个工程文件，问三个问题）：

```
这个文件是「手写源码 / 设置」吗？
├─ 是  → 提交进 Git（HDL 进 src/hdl，约束进 src/constraints，IP 定制进 src/ip/*.xci，
│        块设计导出进 src/bd/*.tcl，工程设置进 proj/create_project.tcl）
└─ 否  → 它是「可由脚本重新生成的产物」吗？
         ├─ 是 → 不要提交（靠 .gitignore 自动忽略；靠 create_project.tcl 重新生成）
         └─ 否 → 特殊处理（如 SDK 的 system.mss、硬件交接 .hwh/.hdf 需手动保留）
```

`readme.md` 还定义了两条**生命周期工作流**，贯穿整个工程：

- **Save（保存到 Git）**：导出硬件交接文件 → 导出块设计 Tcl → 手动把工程设置变动写进 `create_project.tcl` → 提交。
- **(Re)Load（从 Git 重建工程）**：用 `cleanup` 清掉生成物 → 用 Vivado 跑 `create_project.tcl` → 检查控制台无报错。

这两条工作流，正是「Save 一次、ReLoad 一次」的循环——也是为什么这种工程能在不同机器之间迁移。

#### 4.1.3 源码精读

先看 `readme.md` 如何用一句话引用官方依据：

[readme.md:1-3](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L1-L3) — 标题与 UG892 出处，说明这套目录结构是「Xilinx 官方推荐的若干做法之一」。

接着是核心的「版本化对照表」：

[readme.md:5-14](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L5-L14) — 这张表逐条规定：工程与运行结果**只**版本控制 `create_project.tcl` 一个文件；设计源在 `src/hdl`；约束在 `src/constraints`；自定义 IP 在 `repo/local`；SDK 应用工程在 `sdk/`。

注意表里这一行的措辞很关键——它揭示了 `create_project.tcl` 的「手工维护」属性：

> An example is provided in the template and must be manually updated whenever there are changes that must be versioned.

翻译：脚本里给的只是**示例**，只要工程设置有变动，就必须**手工**改这个脚本。这句话是理解本讲后续所有「为什么脚本里有硬编码路径、为什么脚本和实际目录对不上」的总钥匙。

再读两条工作流的原文：

[readme.md:18-29](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L18-L29) — Save 与 (Re)Load 两个工作流的完整步骤。注意 (Re)Load 里那句强调「Anything not saved with the Save workflow will be deleted」——清理是不可逆的，这决定了 cleanup 脚本必须非常小心。

最后，`readme.md` 给出了一份「标准模板目录树」：

[readme.md:38-96](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L38-L96) — 这是 Digilent 工程模板的**标准**布局：`hw_handoff/`（硬件交接，`.hdf`）、`proj/`（三个脚本）、`repo/`（接口与 IP）、`sdk/`（软件）、`src/`（bd/hdl/constraints/ip）。

但是！——这是**模板的标准布局**，而本仓库**实际**的目录略有出入。请把模板树和下面这张「实际布局」表对照看：

| 模板标准位置（readme） | 本仓库实际位置 | 实际内容 | 是否进 Git |
| ---------------------- | -------------- | -------- | ---------- |
| `proj/` | `proj/` | 仅 3 个文件：`create_project.tcl`、`cleanup.sh`、`cleanup.cmd` | 进（这 3 个） |
| `src/hdl/` | **顶层 `hdl/`** | AES RTL（`src/`、`tb/`、`utils/`、`gf_s_box/`、`VE_sv/`、`docs/`） | 进（算法源码） |
| `src/bd/` | `src/bd/` | `aes_design.tcl`（块设计导出） | 进 |
| `src/constraints/` | `src/constraints/` | `zybo_z7.xdc` | 进 |
| `src/ip/` | `src/ip/` | 大量 `.xci` 等（块设计里的 IP 实例） | **仅 `.xci`/`.prj`** |
| `repo/` | `repo/vivado-boards/` | Zybo Z7-20 的板级定义文件 | 进 |
| `hw_handoff/` | `hw_handoff/` | `AesCrypto.hwh` + `AesCrypto_bd.tcl` | 进 |
| `sdk/` | `sdk/AesCrypto_wrapper_hw_platform_0/` | 软件工程 | 部分（见 .gitignore） |
| （模板无） | `ip_repo/AesCryptoCore_1.0/` | 打包好的自定义 AXI IP | 进（Unit 3 主角） |

两个值得留意的差异：

1. **RTL 在顶层 `hdl/` 而非 `src/hdl/`**：本仓库把 AES 算法源码单独提到顶层 `hdl/`，与模板「放进 `src/hdl`」的约定不同。这会直接影响模块二里 `create_project.tcl` 能否正确添加源文件。
2. **硬件交接文件是 `.hwh` 不是 `.hdf`**：模板 readme 写的是 `<top_level>.hdf`（老格式），而实际 `hw_handoff/` 里是 `AesCrypto.hwh`（较新格式）。这是模板文档「写于旧版本 Vivado」留下的痕迹——遇到文档与实际不符，以实际文件为准。

真正把「最小化版本控制」从口号变成现实的，是 `.gitignore`。它逐类规定哪些产物被忽略：

[.gitignore:1-15](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/.gitignore#L1-L15) — 忽略所有典型的 Vivado 产物：`*.xpr`（工程文件）、`*.cache`、`*.runs`、`*.bit`（比特流）、`*.elf`、`*.log`、`*.jou` 等。注意第一行就把工程文件本身 `.xpr` 排除了——这正是「工程可重建，所以不版本控制」的体现。

针对 `proj/` 目录，`.gitignore` 用「先全忽略、再白名单」的手法，只放出三个生成器：

```
# ignore everything in project folder
proj/*
# except this file and project generators
!proj/create_project.tcl
!proj/cleanup.cmd
!proj/cleanup.sh
```

同样的「白名单」手法用于 `src/ip/`：忽略一切，只放行 `.xci` 和 `.prj`——这正好对应 readme 表里那句「Only `*.xci` and `*.prj` files are checked in from `src/ip/`」。

#### 4.1.4 代码实践

1. **实践目标**：建立「目录约定 → 版本控制边界」的直觉，并发现文档与实际的差异。
2. **操作步骤**：
   - 打开 `readme.md` 的标准目录树（[readme.md:38-96](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/readme.md#L38-L96)）。
   - 用文件浏览器或 `ls` 走一遍实际的 `Vivado_project/` 目录。
   - 列一张「实际位置 vs 模板位置」对照表（可参考上面那张表）。
   - 再打开 `.gitignore`，为每一个实际目录在 `.gitignore` 里找到对应规则，说明它「全进 / 全忽略 / 白名单」。
3. **需要观察的现象**：`proj/` 目录里**只有 3 个文件**；`hdl/` 在顶层而非 `src/` 下；`hw_handoff/` 里是 `.hwh` 而非 `.hdf`。
4. **预期结果**：你能用一句话回答「为什么 `proj/` 里没有 `.xpr` 文件」——因为它被 `.gitignore` 忽略了，工程靠脚本重建。
5. **待本地验证**：若你本机装了 Git，可在仓库根目录执行 `git ls-files HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/`，确认 Git 实际跟踪的文件只有那 3 个脚本。

#### 4.1.5 小练习与答案

**练习 1**：模板 readme 说硬件交接文件是 `.hdf`，但实际仓库里是 `.hwh`。这两者是什么关系？

> **答案**：`.hdf`（Hardware Definition File）是较老版本 Vivado/SDK 使用的硬件交接格式；`.hwh`（Hardware Handoff）是较新版本（Vivado 2017+ 起）使用的格式，内容更结构化（基于 JSON）。它们承担同一个角色——把 PL（可编程逻辑）侧的地址映射、外设信息交给 PS（处理器系统）侧的软件使用。文档里写 `.hdf` 是模板写于旧版本留下的痕迹，以仓库里实际存在的 `AesCrypto.hwh` 为准。

**练习 2**：为什么 `.gitignore` 要忽略 `.xpr` 文件？如果强行提交它会怎样？

> **答案**：`.xpr` 是 Vivado 的工程描述文件，包含大量机器相关、版本相关的路径与状态，可由 `create_project.tcl` 完全重新生成。强行提交它会造成：仓库体积膨胀、不同开发者之间频繁冲突、跨版本不兼容。正确做法是忽略它，靠脚本重建。

---

### 4.2 `create_project.tcl`：用脚本重建工程

#### 4.2.1 概念说明

`create_project.tcl` 是整个工程模板的「心脏」。它的角色是：**给定一套手写源码和设置，用一段 Tcl 脚本把一个完整的 Vivado 工程从零造出来**。

这之所以可行，是因为 Vivado 的图形界面操作背后全是 Tcl 命令——`File → New Project` 对应 `create_project`，添加源文件对应 `add_files`，创建综合运行对应 `create_run`……把这些命令串起来写成脚本，就能一键复现工程。

理解这个脚本有两个层次：

1. **结构层**：它依次做了哪几大类事情（建工程 → 设器件 → 加源 → 建综合/实现 run → 建块设计）。
2. **陷阱层**：作为「模板示例」，它带有哪些必须手工适配的地方。

#### 4.2.2 核心流程

`create_project.tcl` 的执行流程可以概括为 8 步：

```
1. 确定目标目录（dest_dir）并 cd 进去
2. 定义工程常量：工程名、块设计名、目标器件 part、板级 board_part
3. create_project：创建空工程
4. 设置工程属性：默认库、器件、板级、仿真/目标语言
5. 建文件集 sources_1 / constrs_1；设置 IP 仓库路径；刷新 IP 目录
6. 添加源文件：src/hdl（HDL）、src/ip/*.xci（IP）、src/constraints（约束）
7. 建综合 run（synth_1）与实现 run（impl_1）
8. source 块设计 Tcl，生成 wrapper，设为顶层
```

其中第 1～7 步对任何工程都通用，第 8 步只对「基于块设计（Block Design）」的工程才有——而本工程恰恰是一个块设计工程（顶层是 `aes_design_wrapper`）。

#### 4.2.3 源码精读

**第 1 步：目标目录——注意硬编码路径。**

[create_project.tcl:1-12](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L1-L12) — 脚本开头先设了一个全局变量 `::create_path`：

```tcl
set ::create_path "C:\Users\Catalina\Desktop\AES_CryptopCore\rep_g\aes_cryptocore"
```

这里有三个**坑**，是你日后运行此脚本时必须先处理的：

1. 这是作者本机的 **Windows 桌面路径**，Linux/Mac 上完全不存在。
2. 因为第 4 行无条件设了 `::create_path`，下面 `if {[info exists ::create_path]}` 永远为真，`else` 分支（用脚本所在目录）**永远不会执行**。也就是说，照原样运行，工程会被建到这个不存在的 Windows 路径里。
3. 路径里 `AES_CryptopCore` 还有个拼写错误（多了个 `p`）。

这正印证了 4.1 里那句话：脚本只是「示例，须手工更新」。**你若要运行它，第一步就是把第 4 行改成你自己的目录，或直接注释掉，让脚本回退到「在脚本所在目录建工程」的分支。**

**第 2 步：工程常量——揭示了目标硬件。**

[create_project.tcl:14-19](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L14-L19) — 定义了四个常量：

```tcl
set proj_name "AES_CryptoCore"
set bd_name "aes_design"
set part "xc7z020clg400-1"
set board_part "digilentinc.com:zybo-z7-20:part0:1.0"
```

读这两行就能知道目标板：`xc7z020` 是 **Zynq-7020**（Xilinx 的 ARM+FPGA SoC），`zybo-z7-20` 是 Digilent 的 **Zybo Z7-20** 开发板。这个信息很关键——它解释了为什么后续 Unit 3 会出现 AXI 接口和 `processing_system7`：因为 Zynq 芯片内置了一个 ARM 处理器核（PS），AES IP 通过 AXI 总线挂到这个处理器上。

**第 3、4 步：建工程并设属性。**

[create_project.tcl:30-45](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L30-L45) — 先 `create_project`，再用一连串 `set_property` 设工程属性：

```tcl
create_project $proj_name $dest_dir
...
set_property "default_lib" "xil_defaultlib" $obj
set_property "part" $part $obj
set_param board.repoPaths [list $repo_dir/vivado_boards/new/board_files]
if { $board_part != "" } {
   set_property -name "board_part" -value $board_part -objects $obj
}
set_property "simulator_language" "Mixed" $obj
set_property "target_language" "VHDL" $obj
```

注意两点：

- `simulator_language` 设为 `Mixed`（混合，允许 Verilog 和 VHDL 一起仿真），`target_language` 设为 `VHDL`（综合的「目标语言」首选 VHDL）。这看起来有点反直觉——AES 核心明明是 Verilog 写的——但这是 Vivado 对「目标语言」的默认偏好设置，并不阻止 Verilog 源被综合。
- 这里又有一个**小坑**：`board.repoPaths` 指向 `$repo_dir/vivado_boards/new/board_files`（下划线 `vivado_boards`），而仓库里实际的目录名是 `repo/vivado-boards`（连字符 `vivado-boards`）。下划线 vs 连字符不匹配，板级文件可能找不到——这也是「脚本需手工适配」的又一处证据。

**第 5、6 步：文件集与添加源文件。**

[create_project.tcl:47-71](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L47-L71) — 创建 `sources_1`、`constrs_1` 两个文件集，设置 IP 仓库路径并刷新 IP 目录，然后添加源文件：

```tcl
add_files -quiet $src_dir/hdl                 ;# src/hdl
add_files -quiet [glob -nocomplain ../src/ip/*.xci]   ;# src/ip 下的 xci
add_files -fileset constrs_1 -quiet $src_dir/constraints  ;# src/constraints
```

注意 `$src_dir/hdl` 展开后是 `../src/hdl`（即 `Vivado_project/src/hdl`），而 4.1 已经指出：**本仓库的实际 RTL 在顶层 `hdl/`，不在 `src/hdl/`**。所以这一行照原样跑会找不到 AES 的算法源码。这进一步说明：AES 算法 RTL 并不是通过「松散源文件」进工程的，而是先被打包成 `ip_repo/` 里的自定义 IP，再在块设计里实例化（Unit 3 的主线）。块设计 Tcl（`src/bd/aes_design.tcl`）和约束（`src/constraints/zybo_z7.xdc`）这两项则与实际目录**对得上**。

`update_ip_catalog -rebuild` 这一步很重要：它让 Vivado 扫描 `ip_repo/` 里的自定义 IP（也就是打包好的 AES IP），使其能在块设计中被调用。

**第 7 步：综合与实现 run。**

[create_project.tcl:73-114](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L73-L114) — 创建 `synth_1`（综合）和 `impl_1`（实现）两个 run，并指定流程版本：

```tcl
create_run -name synth_1 -part $part -flow {Vivado Synthesis 2018} -strategy "Vivado Synthesis Defaults" ...
create_run -name impl_1 -part $part -flow {Vivado Implementation 2018} -strategy "Vivado Implementation Defaults" ...
```

注意 `Vivado Synthesis 2018` / `Vivado Implementation 2018`——这暗示工程是按 **Vivado 2018** 版本的流程导出的。换用差别较大的 Vivado 版本时，这里的 flow 名称可能需要调整。

**第 8 步：块设计与顶层 wrapper。**

[create_project.tcl:118-127](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L118-L127) — 脚本末尾 source 块设计 Tcl，生成 wrapper 并设为顶层：

```tcl
# Comment the following section, if there is no block design
source $origin_dir/src/bd/$bd_name.tcl
set design_name [current_bd_design]
add_files -norecurse [make_wrapper -files [get_files $design_name.bd] -top -force]
set obj [get_filesets sources_1]
set_property "top" "${design_name}_wrapper" $obj
```

`make_wrapper ... -top` 会为块设计自动生成一个 Verilog 顶层包装（`<设计名>_wrapper`），并把它设为工程顶层。这解释了为什么工程的顶层模块叫 `aes_design_wrapper`——它是块设计的自动包装，而不是手写代码。

#### 4.2.4 代码实践

> 本实践的依据来自 `readme.md` 的 (Re)Load 工作流与「Process for setting up a new project」一节。

1. **实践目标**：把 `create_project.tcl` 的执行流程「讲清楚」，并定位出它在你机器上跑之前必须改的几处。
2. **操作步骤（阅读型）**：
   - 通读 [create_project.tcl:1-127](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L1-L127)。
   - 用自己的话，按 4.2.2 的 8 步，写出每一步对应的 Tcl 命令和行号。
   - 列出「必须先手工改才能在本机运行」的清单（至少 3 处）。
3. **操作步骤（可选上板，待本地验证）**：
   - 把第 4 行 `set ::create_path "..."` 改成你自己的空目录（或注释掉这一行，让脚本回退到「在脚本所在目录建工程」）。
   - 打开 Vivado → Tcl Console（或 GUI 的 `Tools → Run Tcl Script`）。
   - `cd` 到 `proj/` 目录后执行 `source create_project.tcl`。
   - 观察控制台输出，重点看是否报 `add_files` 找不到 `src/hdl` 的警告。
4. **需要观察的现象**：控制台依次打印 `INFO: Creating new project in ...`、`INFO: Project created:AES_CryptoCore`、`INFO: Block design created: aes_design.bd`。
5. **预期结果**：若一切正常，`proj/`（或你指定的目录）下会生成 `AES_CryptoCore.xpr` 工程文件及 `.cache/`、`.runs/` 等目录，并在 Vivado GUI 中自动打开工程。
6. **待本地验证**：是否真能成功重建，取决于你是否修正了硬编码路径、`vivado_boards` 下划线问题、以及 `src/hdl` 路径问题。若没有 Vivado 环境，可只完成「阅读型」部分，并据此画出重建流程图（见综合实践）。

#### 4.2.5 小练习与答案

**练习 1**：脚本第 6～10 行明明有「如果 `::create_path` 存在就用它，否则用脚本目录」的逻辑，为什么说「照原样跑，工程会建到一个不存在的 Windows 路径」？

> **答案**：因为第 4 行已经无条件地 `set ::create_path "C:\Users\Catalina\..."`，所以 `[info exists ::create_path]` 恒为真，`else` 分支（用脚本所在目录）永远走不到。要启用「在脚本目录建工程」的回退分支，必须把第 4 行注释掉或删掉。

**练习 2**：脚本里 `target_language` 被设成 `VHDL`，但 AES 核心是 Verilog 写的，这会冲突吗？

> **答案**：不会。`target_language` 只是告诉 Vivado「生成新文件、生成 wrapper 时优先用哪种语言」，并不阻止工程里既有 VHDL 又有 Verilog。配合 `simulator_language = Mixed`，Verilog 和 VHDL 源都能正常综合与混合仿真。AES 的 wrapper 由 `make_wrapper` 生成，是个 Verilog 文件，设成顶层同样没问题。

**练习 3**：从哪两行可以推断出「这个工程是基于块设计的，且顶层不是手写代码」？

> **答案**：从 [create_project.tcl:120](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/create_project.tcl#L120) 的 `source $origin_dir/src/bd/$bd_name.tcl`（加载块设计）和第 124 行的 `make_wrapper ... -top`（生成 wrapper），以及第 127 行把顶层设成 `${design_name}_wrapper`。这说明顶层是块设计的自动包装 `aes_design_wrapper`，而非手写模块。

---

### 4.3 `cleanup` 脚本：清理生成的工程内容

#### 4.3.1 概念说明

`cleanup` 脚本是 (Re)Load 工作流的第一步：**把工程目录里所有「可重新生成的产物」删掉，只留下手写源和脚本本身**。它的存在意义是——当你从 Git 拉取工程、准备重建时，先清场，避免新旧产物混杂。

模板提供了两个版本：

- `cleanup.sh`：Linux / macOS 用（bash 脚本）。
- `cleanup.cmd`：Windows 用（批处理）。

两份脚本**思路不同**，而且——很重要的一点——**仓库里发布的这两份脚本都带有注释掉的代码和潜在 bug**。本模块的核心教学点恰恰是：**不要假设开源脚本就是「能直接跑」的成品；读脚本时要分清「被注释的逻辑」和「真正生效的逻辑」。**

#### 4.3.2 核心流程

两份脚本的「设计意图」（来自 readme）是：

```
进入 proj/ 目录
保留：create_project.tcl、cleanup.sh、cleanup.cmd、.gitignore
删除：proj/ 内所有其它文件和子目录（这些都是产物）
```

但两份脚本各自的**实际**实现路径不同：

- `cleanup.cmd`（Windows）：用「只读属性」当保护标记——先给要保留的 4 个文件加上只读属性，再 `del` 删除所有非只读文件，最后解除只读。
- `cleanup.sh`（Linux）：仓库里的版本把通用的「find 删除」逻辑**注释掉了**，只留了 4 条针对特定目录的 `rm -rf`。

#### 4.3.3 源码精读

**`cleanup.cmd`：用只读属性保护关键文件。**

[cleanup.cmd:1-20](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/cleanup.cmd#L1-L20) — 真正生效的核心是这三段：

```bat
rem mark read only those we wish to keep
attrib +R .\create_project.tcl
attrib +R .\cleanup.sh
attrib +R .\cleanup.cmd
attrib +R .\.gitignore

rem delete all non read-only
del /Q /A:-R .\*

rem unmark read-only
attrib -R .\*
```

逻辑很巧妙：`del /Q /A:-R .\*` 表示「静默删除当前目录下所有**非只读**文件」。因为先给 4 个保留文件加了只读属性，它们逃过一劫，其余产物全被删掉；最后 `attrib -R` 把只读属性还原。

注意一个细节：开头那两行「删除子文件夹」的循环（`for /d /r ...` 和 `for /d %%i ...`）是**被注释掉的**（`rem`）。所以这份脚本实际上**只删除 `proj/` 里的文件，不删子目录**。这与「彻底清空」的意图略有出入，读脚本时要留意。

**`cleanup.sh`：注释掉的通用逻辑 + 几条硬编码 rm。**

[cleanup.sh:1-21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/cleanup.sh#L1-L21) — 这份脚本更需要警惕。先看被注释掉的「通用逻辑」：

```bash
# Remove directories/subdirectories
#find . -mindepth 1 -type d -exec rm -rf {} +
# Remove any other files than:
#find . -type f ! -name 'cleanup.sh' \
               ! -name 'cleanup.cmd' \
               ! -name 'create_project.tcl' \
               ! -name '.gitignore' \
               -exec rm -rf {} +
```

这两条被 `#` 注释的 `find` 命令，才是 readme 描述的「保留 4 个文件、删除其余一切」的**标准做法**（标准 Digilent 模板就是用这两条 find）。但它们没生效。

真正生效的是文件末尾的 4 条硬编码 `rm`：

```bash
 rm -rf ../AES_CryptoCore/*
 rm -rf ../src/bd/*
 rm -rf ../src/constraints/*
 rm -rf ../repo/vivado-bords/*
```

这 4 条至少有**三个问题**，读脚本时务必看清：

1. **`vivado-bords` 拼写错误**：实际目录是 `repo/vivado-boards`（连字符 + boards），而脚本写的是 `vivado-bords`（漏了 `a`）。这条 `rm` 实际上匹配不到任何东西，等于空操作。
2. **会删掉版本控制的源文件**：`rm -rf ../src/bd/*` 和 `rm -rf ../src/constraints/*` 会把 `src/bd/aes_design.tcl`（块设计导出）和 `src/constraints/zybo_z7.xdc`（约束）一并删掉——而这两个是**要进 Git 的源文件**！这与 readme「只删非版本控制内容」的意图相悖。
3. **`../AES_CryptoCore/*`**：对应 `create_project.tcl` 里 `proj_name = AES_CryptoCore` 生成的工程目录，这条倒是合理的（删掉生成的工程目录）。

结论：**这份 `cleanup.sh` 是一份被作者临时改过、且带有 bug 的脚本，不能直接信任。** 如果你要在 Linux 上清理，更安全的做法是恢复那两条被注释的 `find`（或干脆依赖 `.gitignore` + `git clean`）。

> 小结一句方法论：读任何随项目分发的「工具脚本」时，先扫一遍哪些行被注释、哪些行生效，再判断它是否与文档描述一致。文档说一套、脚本做另一套的情况很常见。

#### 4.3.4 代码实践

1. **实践目标**：学会「批判地读脚本」——区分注释逻辑与生效逻辑，并发现脚本与文档、脚本与实际目录的不一致。
2. **操作步骤**：
   - 打开 [cleanup.sh:1-21](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/cleanup.sh#L1-L21)，把每一行归类为「生效 / 被注释」。
   - 对每一条生效的 `rm`，去实际目录里查它会不会误删版本控制文件。
   - 打开 [cleanup.cmd:1-20](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/proj/cleanup.cmd#L1-L20)，解释「只读属性保护法」为什么能工作。
3. **需要观察的现象**：`cleanup.sh` 里真正起作用的只有 4 行 `rm`，其中一行因拼写错误而无效，两行会误删源文件。
4. **预期结果**：你能写出一份「此脚本的安全修订建议」，例如：恢复被注释的 `find` 通用清理、修正 `vivado-bords` → `vivado-boards`、把 `src/bd` 和 `src/constraints` 从删除列表里去掉。
5. **待本地验证**：**不要**在本地直接执行 `cleanup.sh`——它会删除 `src/bd`、`src/constraints` 下的文件。建议只在「阅读」层面完成本实践；真要清理时改用 `git clean -ndx`（先预览）再 `git clean -fdx`（限定在 `.gitignore` 忽略的范围内）。

#### 4.3.5 小练习与答案

**练习 1**：`cleanup.cmd` 为什么要先 `attrib +R` 再 `del /A:-R`？

> **答案**：`del /A:-R` 的含义是「只删除非只读文件」。先把要保留的 4 个文件（脚本和 `.gitignore`）设为只读，就等于给它们贴了「免死金牌」；随后的删除命令只动那些没贴金牌的产物文件；最后再 `attrib -R` 还原。这是一种巧妙的「白名单保护」实现，无需逐个列出要删的文件。

**练习 2**：`cleanup.sh` 里的 `rm -rf ../repo/vivado-bords/*` 实际上什么也没删，为什么？

> **答案**：因为路径名拼错了——实际目录是 `vivado-boards`（中间是 `boards`），脚本写成了 `vivado-bords`（漏了 `a`）。`rm -rf` 对一个不存在的路径不会报错也不会删任何东西，所以这条命令是静默失效的空操作。这也是「不要盲信随项目分发的脚本」的一个实例。

**练习 3**：readme 说 cleanup 应该「只删非版本控制内容」，但 `cleanup.sh` 却会删 `src/bd/*`。这俩矛盾吗？该怎么取舍？

> **答案**：矛盾。`src/bd/aes_design.tcl` 是要版本控制的块设计导出，不应被清理。这说明这份 `cleanup.sh` 偏离了 readme 描述的标准做法（标准做法是那两条被注释的 `find`，它们只保留 4 个脚本文件、删 `proj/` 内其余内容，并不会去碰 `src/bd`）。取舍：以 readme 的意图和 `.gitignore` 规则为「标准」，把这份 `cleanup.sh` 视为带 bug 的临时改版；真要清理，优先恢复 `find` 通用逻辑或用 `git clean`。

---

## 5. 综合实践

**任务：画出本工程的「重建流程图」，并标注每一处「必须手工适配」的地方。**

把本讲三个模块串起来，完成下面这个贯穿性任务：

1. **画重建流程图**：从「`git clone` 仓库」开始，到「Vivado 里打开可综合的工程」结束，画出完整的 (Re)Load 流程。流程中至少要包含：
   - 清理（cleanup）这一步，并标注「按 readme 意图该做什么 / 实际脚本做了什么」。
   - 执行 `create_project.tcl`，按 4.2.2 的 8 步展开成子步骤。
   - 每一步产出的目录或文件（例如 `create_project` 产出 `AES_CryptoCore.xpr`，`make_wrapper` 产出 `aes_design_wrapper.v`）。

2. **标注「必须手工适配」清单**：在流程图上用红色（或星号）标出所有「照原样跑会失败、必须先改」的点。至少应包括：
   - `create_project.tcl` 第 4 行的 Windows 硬编码路径。
   - `board.repoPaths` 里 `vivado_boards`（下划线）与实际 `vivado-boards`（连字符）不一致。
   - `add_files $src_dir/hdl` 找不到实际位于顶层 `hdl/` 的 RTL。
   - `cleanup.sh` 的拼写错误 `vivado-bords` 和误删 `src/bd`、`src/constraints`。

3. **回答一个反思题**：既然 `create_project.tcl` 有这么多「坑」，为什么这套「脚本重建法」仍被 Xilinx UG892 推荐、并被广泛使用？请从「可重建性」「跨机器可移植」「版本库体积」三个角度各写一句话。

**预期产出**：一张流程图（手绘或工具画均可）+ 一份适配清单 + 三句反思。这个任务会逼你把「目录约定 → 重建脚本 → 清理脚本」三者连成一条完整的工作流，这正是本讲的核心。

> 说明：若你没有 Vivado 环境，本实践以「阅读 + 画图 + 文字分析」的形式完成即可，不必真跑工具。所有结论都应能在本讲引用的源码里找到依据。

## 6. 本讲小结

- Vivado 工程体积庞大是因为充满「可重新生成的产物」；正确做法是**只版本控制「源 + 设置」，不版本控制「产物」**，产物由脚本重建——这是 Xilinx UG892 推荐的「脚本重建法」。
- `Vivado_project/` 的目录约定：`proj/` 只放脚本，`src/bd|constraints|ip` 放块设计/约束/IP 实例，`hdl/` 放算法 RTL，`ip_repo/` 放打包好的自定义 IP，`hw_handoff/`、`repo/`、`sdk/` 各司其职。**`.gitignore`** 才是「最小化版本控制」真正的执行者。
- `create_project.tcl` 用一段 Tcl 串起「建工程 → 设器件 → 加源 → 建 run → 建块设计」8 步，能把工程从零重建；目标器件是 **Zynq-7020 / Zybo Z7-20**，工程基于**块设计**，顶层是自动生成的 `aes_design_wrapper`。
- **关键警示**：随仓库发布的脚本是「示例，须手工适配」。`create_project.tcl` 带有 Windows 硬编码路径、`vivado_boards` 下划线/连字符不一致、`src/hdl` 与实际顶层 `hdl/` 不符等问题；`cleanup.sh` 带有拼写错误 `vivado-bords` 并会误删 `src/bd`、`src/constraints`。
- **方法论**：读任何随项目分发的工具脚本时，先区分「注释逻辑」与「生效逻辑」，再与文档对照，发现不一致就以实际代码和 `.gitignore` 为准。

## 7. 下一步学习建议

本讲讲清了「工程怎么管」，接下来就该钻进「工程里装的是什么」了：

1. **Unit 2（AES 数据通路）**：进入顶层 `hdl/src/`，从 [aes_top.v](https://github.com/suisuisi/FPGA_Library/blob/1e33525198872d63ced48e8f0cebaa2419b9eb22/HDL/AesCryptoCore_1.0/PROJECT/Vivado_project/hdl/src/aes_top.v) 开始，看 AES-128 的轮函数如何在硬件上展开。你会发现本讲的 `hdl/` 目录就是 Unit 2 的主战场。
2. **Unit 3（AXI IP 封装）**：进入 `ip_repo/AesCryptoCore_1.0/`，看本讲提到的「打包好的自定义 IP」是如何用 `component.xml` 描述、如何用 AXI4-Lite 暴露给 Zynq 处理器的。本讲的块设计 `aes_design` 正是 Unit 3 之 IP 的「容器」。
3. **延伸阅读**：若想更系统地理解 Vivado 工程的版本控制，可阅读 Xilinx 官方文档 **UG892**《Vivado Design Flows Overview》中关于「Version Control」的章节——本仓库的整套目录约定都源自它。

> 一句话定位：本讲是 Unit 2/3 的「地图与脚手架」——先知道 AES 工程怎么搭起来，再去读里面的算法和接口，就不会迷路。
