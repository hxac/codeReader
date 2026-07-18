# 目录结构与四大类库

> 本讲是入门单元（u1）的第三讲。承接 u1-l1（仓库是 collection-repo）与 u1-l2（git submodule 机制与克隆方式），我们把镜头拉近，看清仓库里到底装了哪些库、它们如何分门别类地摆放，以及"目录结构为什么不能乱动"这条铁律在具体内容上的体现。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 psi_fpga_all 顶层四大目录（`VHDL/`、`TCL/`、`Python/`、`VivadoIp/`）各自聚合的是哪一类库；
- 看懂 `.gitmodules` 是仓库内容的"权威清单"，并能用它清点出全部 23 个子模块；
- 对每一类库给出大致职责与代表模块（不确定处主动标注"待确认"，而不是凭空猜测）；
- 用一句话解释"固定目录结构 + 相对路径互引"为什么让目录不能被随意改名或移动。

## 2. 前置知识

本讲默认你已经学完前两讲，建立了下面这些直觉（若还陌生，建议先回看 u1-l1、u1-l2）：

- **collection-repo（集合仓库）**：本仓库自身几乎不含业务代码，它的作用是把一批独立的 FPGA 库按固定目录结构"装配"在一起。
- **git submodule**：父仓库只保存子模块的 commit 指针（gitlink），不保存子模块内部的文件内容；指针清单写在 `.gitmodules` 里。
- **目录结构即接口**：各库之间用相对路径互相引用，所以目录一旦改动，引用就会断裂。
- **三份关键文件**：`README.md`（入门说明）、`.gitmodules`（子模块权威清单）、`Changelog.md`（每次发布固定的子模块版本）。

> 术语提示：本讲里出现的 **gitlink** 指的是 git 在父仓库里为每个子模块记录的那一行"指向某个子模块仓库某次 commit"的指针；在 `git ls-files` 的输出里，它就是一个子模块路径名（而不是普通文件）。

## 3. 本讲源码地图

本讲只读这三份文件，它们都是从仓库顶层就能直接读到的真实文件：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| [README.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md) | 项目说明：维护者、用途、克隆方式、**举例性质**的库清单 | 看清 README 的库列表只是"举例"，并不完整 |
| [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) | 23 个子模块的权威声明（path + url） | 按目录给全部子模块分类、点名 |
| [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md) | 按发布版本（2018.1~2021.1）记录每个子模块被固定的版本号 | 看清"四大类"的分组方式，以及版本随发布演进 |

> 注意：本讲只讨论"目录与分类"，不展开 Changelog 的版本号细节——那是 u3-l1 的主题。本讲引用 Changelog，只是因为它印证了"四大类"这个划分。

## 4. 核心概念与源码讲解

### 4.1 全景：四大类库与"三份清单"的关系

#### 4.1.1 概念说明

psi_fpga_all 把所有 FPGA 相关库按**用途**分成四大类，每一类放在一个同名的顶层目录里：

- `VHDL/` —— 用 VHDL 写的**核心硬件描述库**（可综合的电路模块）；
- `TCL/` —— 用 Tcl 写的**工具框架**（仿真框架、IP 打包工具）；
- `Python/` —— 用 Python 写的**自动化脚本工具链**（生成测试平台、脚本化 EDA 工具、管理依赖等）；
- `VivadoIp/` —— 已经封装好的 **Vivado IP 核**（最终用户可在 Vivado 工程里直接调用）。

理解这套仓库时最容易踩的坑是：**只读 README 会漏掉一大半库**。原因在于 README 的 "Purpose of the Repository" 一节里那份带项目符号的库列表，只是**举例性质**的，并不完整。真正"仓库里到底挂了哪些子模块"的权威答案，写在 `.gitmodules` 里。

所以我们用**三份清单**互相印证：

1. **README.md** —— 给人类看的入门介绍，列表**不完整**（只列了 9 个库，下文会逐一比对）；
2. **.gitmodules** —— 机器与 git 共同认定的**权威清单**，声明了全部 **23** 个子模块；
3. **Changelog.md** —— 按发布版本记录每个子模块被固定到的 tag，分组方式与四大类一致。

> 一个贯穿本讲的方法论：**遇到"仓库里有什么"这类问题，以 `.gitmodules` 为准**；README 用来建立直觉，Changelog 用来追溯版本。

#### 4.1.2 核心流程：如何用三份清单建立全景认知

建立全景认知的"阅读流程"可以总结为三步：

```
第 1 步：读 README 的 Purpose 一节  -->  建立四大类的直觉（知道有 TCL/VHDL/Python，README 还顺带没有展开 VivadoIp）
第 2 步：读 .gitmodules              -->  拿到权威的、完整的 23 条子模块清单，按顶层目录名归类
第 3 步：对照 Changelog 的分组       -->  复核"四大类"划分，并留意命名随时间的变化
```

关键判断点是 **README 列表 vs `.gitmodules` 的差异**。下表把 README 里出现的库名，与 `.gitmodules` 里实际声明的子模块做对照（✓=README 列出，✗=README 未列出但 `.gitmodules` 有）：

| 类别 | README 是否逐个列出 | `.gitmodules` 实际数量 |
| --- | --- | --- |
| TCL | 只列了 PsiSim（✗ 漏 PsiIpPackage） | 2 |
| VHDL | 列了 psi_common/psi_tb/psi_fix/en_cl_fix（✗ 漏 psi_multi_stream_daq） | 5 |
| Python | 列了 PsiPyUtils/IseScripting/VivadoScripting/TbGenerator（✗ 漏 PsiFpgaLibDependencies） | 5 |
| VivadoIp | **一个都没列** | 11 |
| **合计** | README 仅点名 9 个 | **23** |

结论很清楚：README 给了一个"够你入门"的最小例子，而 `.gitmodules` 才是清点全貌的依据。这也正是为什么本讲反复强调"以 `.gitmodules` 为准"。

#### 4.1.3 源码精读

**README 的 Purpose 一节与"举例性"库清单。** 注意第 14 行那句关键约束——目录结构之所以重要，正是因为库与库之间用相对路径互相引用：

[README.md:13-18](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L13-L18) —— 说明本仓库是 collection-repo、目录结构的重要性来自"库之间用相对路径互引"、以及约每 3 个月整体更新一次的策略。

[README.md:20-32](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L20-L32) —— README 给出的库清单：TCL 仅 PsiSim、VHDL 四个、Python 四个。**注意这里完全没有 VivadoIp 条目**，也漏掉了 PsiIpPackage、psi_multi_stream_daq、PsiFpgaLibDependencies。

[README.md:26-27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L26-L27) —— `en_cl_fix` 被标注为 Enclustra GmbH 提供库的 fork，并给出原始仓库链接。这是 README 里少有的"明确交代来历"的库，本讲后面 VHDL 类会用到这条事实。

**`.gitmodules` 才是权威清单。** 它用 INI 风格声明每条子模块的 `path`（本地目录，决定"目录结构即接口"）与 `url`（远程仓库，全部写成相对形式 `../../paulscherrerinstitute/<repo>.git`，详见 u1-l2）：

[.gitmodules:1-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L1-L9) —— 开头几条：`VHDL/psi_tb`、`VHDL/psi_common`、`TCL/PsiSim`。从这里就能看到"path 的第一段就是四大类目录名"。

**Changelog 的分组印证了四大类划分。** 注意每次发布（如 `## 2021.1`、`## 2020.2`）下方的子模块都是按 TCL / VHDL / Python / VivadoIP 四组排列的：

[Changelog.md:1-10](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L1-L10) —— 2021.1 发布分组：TCL（PsiIpPackage）、VHDL（en_cl_fix/psi_common/psi_fix）、VivadoIP（vivadoIP_mem_test）。注意这次发布只更新了少数子模块，并非每次都全量罗列。

[Changelog.md:11-39](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L11-L39) —— 2020.2 发布是**最完整**的一次罗列，四个分组下几乎列出了全部子模块及版本号，是复核"四大类 + 23 个子模块"的最好参照。

> 顺便发现一个真实的命名演变：2020.2 的 Changelog 第 38 行把这个 IP 写成 `vivadoIP_sync_det_edge`（链接也指向 `vivadoIP_sync_det_edge`），而 `.gitmodules` 第 64–66 行与 `git ls-files` 里它的 path 是 `vivadoIP_sync_edge_det`。说明这个 IP 在历史上经历过一次改名/路径调整。**以 `.gitmodules` 的当前 path 为准**，Changelog 记录的是历史快照。这类细节是"以 `.gitmodules` 为准"这一方法论的活样本。

#### 4.1.4 代码实践

**实践目标**：亲手用命令清点子模块数量与四大类分布，验证"23 个"这个数字，并体会"README 列表不完整"。

**操作步骤**（在仓库根目录执行，均为只读命令）：

1. 统计 `.gitmodules` 声明了多少条子模块：
   ```bash
   grep '^	path' .gitmodules | wc -l
   ```
2. 按顶层目录（四大类）统计每类各有多少条：
   ```bash
   grep '^	path' .gitmodules | cut -d/ -f1 | sort | uniq -c
   ```
3. 用 git 自己的视角复核（`git ls-files` 会把每个子模块输出为一行 gitlink）：
   ```bash
   git ls-files | grep -E '^(VHDL|TCL|Python|VivadoIp)/' | cut -d/ -f1 | sort | uniq -c
   ```

**需要观察的现象**：三条命令应当互相印证——总数为 **23**，按类别为 **VHDL 5 / TCL 2 / Python 5 / VivadoIp 11**。

**预期结果**：

```
      5 Python
      2 TCL
      5 VHDL
     11 VivadoIp
```

（`uniq -c` 的输出顺序按目录名字母序排列；5+2+5+11 = 23。）

> 如果无法在本地运行：以上数字可直接从 [.gitmodules](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules) 逐条数出，结论一致——**待本地验证**仅指命令输出格式，结论本身是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么不能只读 README 来清点仓库里的库？请用一句话回答。

> **参考答案**：因为 README 的 Purpose 章节里的库列表是"举例性质"的、不完整（只列了 9 个，且完全没有 VivadoIp 类），仓库内容的权威清单是 `.gitmodules`。

**练习 2**：README 的库清单里完全没有出现 VivadoIp 类，这是否意味着仓库里没有 Vivado IP 核？

> **参考答案**：不是。`.gitmodules` 与 `git ls-files` 都明确声明了 11 个 `VivadoIp/vivadoIP_*` 子模块。README 只是没在入门说明里展开介绍它们。

---

### 4.2 VHDL 类：核心硬件描述库（5 个）

#### 4.2.1 概念说明

`VHDL/` 目录下的库是用 VHDL 写的**可综合硬件描述**，是整套体系的"底座"——最终跑在 FPGA 上的电路逻辑主要来自这里。它们是最"底层"的库：被 VivadoIp 类的 IP 核内部引用、被 TCL 类的 PsiSim 仿真、被 TCL 类的 PsiIpPackage 打包成 IP。

这一类共 5 个子模块：

- `en_cl_fix` —— 定点数（fixed-point）处理库，README 明确说明它是 Enclustra GmbH 提供库的 **fork**；
- `psi_common` —— 通用公共组件库（名字里的 common 暗示"被很多库共用的基础件"）；
- `psi_fix` —— 定点数信号处理库（名字里的 fix = fixed-point，与 en_cl_fix 同源主题）；
- `psi_tb` —— 测试平台（testbench）辅助库，为仿真提供验证基础设施；
- `psi_multi_stream_daq` —— 多通道数据采集（multi-stream DAQ）应用级库。

> 关于职责：`en_cl_fix` 的 fork 身份是 README 明确交代的，可视为**确定**；其余四个的职责是根据库名 + Changelog 分组 + FPGA 领域常识推断的，整体方向可靠，但具体接口/模块清单属于各子模块内部内容，本仓库未检出，**待确认**。

#### 4.2.2 核心流程：VHDL 类在依赖链中的位置

VHDL 类是**被依赖方**，在整个工作流里处于底层。可以把它在依赖/工作流中的位置画成这样：

```
VHDL/（底层硬件描述）
   │
   ├──> 被 TCL/PsiSim 仿真（自检 testbench，多数在 psi_tb 的辅助下编写）
   ├──> 被 TCL/PsiIpPackage 打包成 IP
   └──> 被 VivadoIp/* 内部引用（例如 vivadoIP_psi_ms_daq 很可能封装了 psi_multi_stream_daq）
```

VHDL 类内部也存在纵向依赖（依据命名推断，**待确认**）：`en_cl_fix` 提供定点数原语，`psi_fix` 在其上构建定点信号处理，`psi_common` 是被多处复用的公共件。这种"底层库被上层库用相对路径引用"正是"目录结构即接口"的典型场景。

#### 4.2.3 源码精读

[.gitmodules:1-6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L1-L6) —— VHDL 类的前两条：`VHDL/psi_tb`、`VHDL/psi_common`。注意 `path` 与 `url` 的最后一段名字一致，url 都是相对形式。

[.gitmodules:22-27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L22-L27) —— VHDL 类的 `en_cl_fix` 与 `psi_fix` 两条。

[.gitmodules:46-48](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L46-L48) —— VHDL 类的最后一条 `psi_multi_stream_daq`（这条在 README 的库清单里被漏掉了）。

[README.md:26-27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L26-L27) —— README 对 `en_cl_fix` 的 fork 说明（带原始仓库链接），是 VHDL 类里来历最清楚的一条。

#### 4.2.4 代码实践

**实践目标**：用 Changelog 复核 VHDL 类的成员，并体会"README 漏列了 psi_multi_stream_daq"。

**操作步骤**（源码阅读型实践）：

1. 打开 [Changelog.md:15-20](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L15-L20)（2020.2 的 VHDL 分组），数一下这里列了几个 VHDL 库。
2. 与 `.gitmodules` 的 VHDL 条目对照，确认两边都是 5 个：en_cl_fix、psi_common、psi_multi_stream_daq、psi_tb、psi_fix。
3. 回到 README 的 [库清单 L22-L27](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L22-L27)，确认它只列了 4 个 VHDL 库、确实漏掉了 `psi_multi_stream_daq`。

**需要观察的现象**：Changelog 与 `.gitmodules` 对 VHDL 类的成员口径一致（5 个），而 README 少列 1 个。

**预期结果**：VHDL 类共 5 个，README 只点名其中 4 个（缺 `psi_multi_stream_daq`）。结论确定，可直接读源验证。

#### 4.2.5 小练习与答案

**练习 1**：`en_cl_fix` 和 `psi_fix` 名字里都有 "fix"，它们大概是什么主题的库？

> **参考答案**：都和**定点数（fixed-point）**相关。`en_cl_fix` 是定点数原语库（Enclustra 库的 fork），`psi_fix` 很可能是构建在定点数之上的信号处理库（具体接口待确认）。

**练习 2**：为什么 `psi_multi_stream_daq` 没出现在 README 的库清单里，却能确定它属于本仓库？

> **参考答案**：因为它出现在 `.gitmodules`（[L46-L48](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L46-L48)）和 Changelog 的 VHDL 分组里。README 列表不完整，不等于该库不存在。

---

### 4.3 TCL 类：仿真与 IP 打包工具框架（2 个）

#### 4.3.1 概念说明

`TCL/` 目录下的不是硬件电路，而是用 Tcl 写的**工具框架**，用来"驱动"其他库完成工程化任务。这一类只有 2 个子模块，但它们是整套自动化流程的发动机：

- `PsiSim` —— 仿真框架。它定义了一套标准化的仿真流水线（初始化 → 配置 → 编译 → 运行 testbench → 检查错误），让 VHDL 库的自检可以一键跑起来（详见 u2-l2）。
- `PsiIpPackage` —— IP 打包工具。它把 HDL 库封装成 Vivado 可识别的 IP 核（详见 u2-l4）。

#### 4.3.2 核心流程：TCL 类是"驱动方"

与 VHDL 类（被驱动）相反，TCL 类是**驱动方**。仓库根目录下的 `scripts/runModelsim.tcl`、`runGhdl.tcl`、`runVivado.tcl`、`packageAllIp.tcl` 这些驱动脚本，本质上就是"加载 PsiSim / PsiIpPackage 框架，然后对各个库逐一执行仿真或打包"。本讲只建立这个直觉，脚本细节留给 u2 单元。

```
scripts/run*.tcl（仓库自带的驱动脚本）
   │  加载并调用
   ▼
TCL/PsiSim        -->  仿真流水线（针对 VHDL/* 各库）
TCL/PsiIpPackage  -->  打包流水线（针对 VivadoIp/* 各库）
```

#### 4.3.3 源码精读

[.gitmodules:7-9](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L7-L9) —— `TCL/PsiSim` 声明。

[.gitmodules:28-30](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L28-L30) —— `TCL/PsiIpPackage` 声明（这条在 README 库清单里被漏掉了，README 的 TCL 部分只提了 PsiSim）。

[Changelog.md:11-14](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L11-L14) —— 2020.2 的 TCL 分组同时列出了 PsiSim 与 PsiIpPackage，印证 TCL 类确为这两个。

#### 4.3.4 代码实践

**实践目标**：确认 TCL 类的 2 个成员，并发现 README 又少列了一个。

**操作步骤**：

1. 在 `.gitmodules` 里找出所有 `path` 以 `TCL/` 开头的条目（应为 2 条）。
2. 对照 README 的 [库清单 L20-L21](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L20-L21)，确认 README 只列了 PsiSim。
3. 在 [Changelog.md](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md) 中检索 `PsiIpPackage`，观察它在多个 release 里都有版本号记录。

**需要观察的现象**：TCL 类成员 = {PsiSim, PsiIpPackage}；README 仅列 PsiSim。

**预期结果**：TCL 类 2 个；README 漏列 `PsiIpPackage`。结论确定。

#### 4.3.5 小练习与答案

**练习 1**：TCL 类的两个框架各自驱动什么任务？

> **参考答案**：`PsiSim` 驱动仿真（对 VHDL 库跑 testbench 自检），`PsiIpPackage` 驱动把 HDL 库打包成 Vivado IP 核。

**练习 2**：为什么说 TCL 类是"驱动方"而 VHDL 类是"被驱动方"？

> **参考答案**：TCL 类提供流程框架与命令，主动去编译/仿真/打包；VHDL 类是被编译、被仿真、被打包的对象。一个提供"怎么做"，一个提供"被处理的内容"。

---

### 4.4 Python 类：自动化脚本工具链（5 个）

#### 4.4.1 概念说明

`Python/` 目录放的是用 Python 写的**自动化工具**，作用是辅助 FPGA 开发流程中的各种"周边"任务（生成代码、脚本化 EDA 工具、管理依赖等）。这一类共 5 个子模块：

- `PsiPyUtils` —— Python 通用工具（Py + Utils）；
- `VivadoScripting` —— 把 Vivado 操作脚本化的辅助工具；
- `IseScripting` —— 把 Xilinx 旧工具 ISE 操作脚本化的辅助工具（Ise = ISE）；
- `TbGenerator` —— 测试平台（Testbench）生成器；
- `PsiFpgaLibDependencies` —— FPGA 库的依赖管理工具。

> 说明：以上职责是依据库名 + Changelog 分组 + FPGA 工程常识推断的方向性描述；各工具的具体命令与接口属于子模块内部内容，本仓库未检出，**待确认**。README 在这一类列了前 4 个，漏掉了 `PsiFpgaLibDependencies`。

#### 4.4.2 核心流程：Python 类是"周边自动化"

Python 类不像 VHDL 那样落在最终电路里，也不像 TCL 那样直接驱动仿真/打包，而是覆盖开发流程的**周边环节**：

```
开发流程
  代码生成/脚手架   <-- TbGenerator（生成 testbench）
  EDA 工具脚本化    <-- VivadoScripting / IseScripting（操作 Vivado / ISE）
  依赖与通用杂事    <-- PsiFpgaLibDependencies / PsiPyUtils
```

注意 `VivadoScripting` 与 `IseScripting` 同时存在，反映了工具链的历史：ISE 是 Xilinx 早期的综合工具，Vivado 是其后继者；两套脚本工具并存，说明这套库需要兼顾新旧两代工具链（具体并存策略**待确认**）。

#### 4.4.3 源码精读

[.gitmodules:10-21](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L10-L21) —— Python 类的前 4 条：`PsiPyUtils`、`TbGenerator`、`VivadoScripting`、`IseScripting`（与 README 的 Python 列表完全对应）。

[.gitmodules:52-54](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L52-L54) —— Python 类的第 5 条 `PsiFpgaLibDependencies`（README 漏列）。

[Changelog.md:21-26](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L21-L26) —— 2020.2 的 Python 分组完整列出 5 个工具及版本，是核对 Python 类成员的最佳参照。

#### 4.4.4 代码实践

**实践目标**：核对 Python 类成员，并观察 ISE/Vivado 两套脚本工具并存。

**操作步骤**：

1. 在 `.gitmodules` 中列出所有 `path` 以 `Python/` 开头的条目，确认是 5 个。
2. 阅读它们的名字，把每个工具归到"代码生成 / EDA 脚本化 / 依赖与通用"三类之一。
3. 在 Changelog 里确认 `IseScripting` 与 `VivadoScripting` 在同一发布里都被列出（例如 [L22-L23](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L22-L23)）。

**需要观察的现象**：Python 类确为 5 个；两套 EDA 脚本工具同时存在。

**预期结果**：Python 类 = {PsiPyUtils, TbGenerator, VivadoScripting, IseScripting, PsiFpgaLibDependencies}。结论确定；具体工具用法待确认。

#### 4.4.5 小练习与答案

**练习 1**：`VivadoScripting` 和 `IseScripting` 为什么同时存在？

> **参考答案**：Vivado 和 ISE 是 Xilinx 新旧两代综合/实现工具。两套脚本工具并存，说明这套 FPGA 库需要同时兼容新旧工具链（具体兼容方式待确认）。

**练习 2**：`TbGenerator` 大概率是用来做什么的？

> **参考答案**：根据 Tb = Testbench，它大概率是一个测试平台**生成器**，自动生成 testbench 脚手架代码（具体生成什么、怎么调用待确认）。

---

### 4.5 VivadoIp 类：封装好的 Vivado IP 核（11 个）

#### 4.5.1 概念说明

`VivadoIp/` 目录是整套体系**最上层的"成品"**：这里的每个子模块都是一个已经封装好的 **Vivado IP 核**，最终用户在自己的 Vivado 工程里可以直接添加、配置、调用，而不必关心底层 HDL 细节。这一类是**数量最多**的一类，共 11 个子模块，命名统一以 `vivadoIP_` 开头：

| 子模块 path | 推断的 IP 功能（依命名 + 领域常识） |
| --- | --- |
| `vivadoIP_data_rec` | 数据记录（data recorder） |
| `vivadoIP_clock_measure` | 时钟测量（clock measurement） |
| `vivadoIP_spi_simple` | 简化版 SPI 接口 |
| `vivadoIP_axis_data_gen` | AXI-Stream 数据发生器 |
| `vivadoIP_mem_test` | 存储器测试（memory test） |
| `vivadoIP_psi_ms_daq` | 多通道数据采集 IP（很可能封装了 `psi_multi_stream_daq`，**待确认**） |
| `vivadoIP_i2c_devreg` | I²C 设备寄存器接口 |
| `vivadoIP_power_sink` | 功耗负载（用于功耗分析，无 self-checking TB，详见 u2-l3） |
| `vivadoIP_fpga_base` | FPGA 基础/板级 IP |
| `vivadoIP_sync_edge_det` | 同步器 + 边沿检测 |
| `vivadoIP_axi_mm_reader` | AXI 内存映射（memory-mapped）读控制器 |

> 说明：AXI、SPI、I²C 都是业界标准接口协议，因此按名字推断功能方向是可靠的；但每个 IP 的具体端口、参数、封装方式属于子模块内部内容，本仓库未检出，**待确认**。

#### 4.5.2 核心流程：VivadoIp 类是"成品层"

VivadoIp 类处在依赖链的顶端，是面向最终用户的交付物：

```
VHDL/（底层 HDL）
   │  被封装（很可能借助 TCL/PsiIpPackage 与各 IP 内部的 package.tcl）
   ▼
VivadoIp/（成品 IP 核）  -->  最终用户在 Vivado 工程中直接调用
```

多个 IP 之间也可能共享底层：例如 `vivadoIP_psi_ms_daq` 与 VHDL 类的 `psi_multi_stream_daq` 同名主题，前者很可能是后者的 IP 封装（**待确认**）。这种"VHDL 库 ↔ 同名 IP"的对应关系，正是"相对路径互引"频繁出现的地方，也再次说明目录结构不能乱动。

#### 4.5.3 源码精读

[.gitmodules:31-45](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L31-L45) —— VivadoIp 类的前 5 条：`data_rec`、`clock_measure`、`spi_simple`、`axis_data_gen`、`mem_test`。

[.gitmodules:49-51](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L49-L51) —— `vivadoIP_psi_ms_daq`（多通道采集 IP）。

[.gitmodules:55-69](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L55-L69) —— VivadoIp 类的剩余 5 条：`i2c_devreg`、`power_sink`、`fpga_base`、`sync_edge_det`、`axi_mm_reader`。

[Changelog.md:27-38](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/Changelog.md#L27-L38) —— 2020.2 的 VivadoIP 分组，几乎列出了全部 11 个 IP 及版本号。注意第 38 行把同步/边沿检测 IP 写成 `vivadoIP_sync_det_edge`，而 `.gitmodules` 里它的 path 是 `vivadoIP_sync_edge_det`——这是前文提到的命名演变，以 `.gitmodules` 当前 path 为准。

#### 4.5.4 代码实践

**实践目标**：清点 VivadoIp 类的 11 个成员，并按"接口协议/功能"给它们打标签。

**操作步骤**：

1. 运行下面命令，列出全部 VivadoIp 子模块：
   ```bash
   grep '^	path' .gitmodules | grep 'VivadoIp/' | cut -f2
   ```
2. 数一下输出行数，确认是 **11**。
3. 把 11 个 IP 按"标准接口类（SPI/I²C/AXI…）/ 功能类（测量、测试、采集…）"自行分组。

**需要观察的现象**：VivadoIp 类恰好 11 个；其中多个名字直接对应标准协议（spi、i2c、axi）。

**预期结果**：11 个 IP，与上表一致。结论确定；具体端口/参数待确认（需进入各子模块仓库查看）。

#### 4.5.5 小练习与答案

**练习 1**：`vivadoIP_psi_ms_daq` 和 VHDL 类的哪个库最可能是"同一主题的封装/被封装"关系？

> **参考答案**：与 `VHDL/psi_multi_stream_daq` 主题一致（ms_daq = multi-stream DAQ）。前者很可能是后者的 Vivado IP 封装（**待确认**）。

**练习 2**：从命名看，哪些 VivadoIp 对应业界标准接口协议？

> **参考答案**：`vivadoIP_spi_simple`（SPI）、`vivadoIP_i2c_devreg`（I²C）、`vivadoIP_axis_data_gen`（AXI-Stream）、`vivadoIP_axi_mm_reader`（AXI 内存映射）。这些都是标准协议名。

---

### 4.6 固定目录结构带来的约束：为什么不能随意改名或移动

#### 4.6.1 概念说明

把四大类看清楚之后，"目录结构即接口"这条 u1-l1 提出的铁律就有了具体落点：因为库与库之间用**相对路径**互相引用，所以一个子模块的 path（即它所在的目录）一旦被改名或移动，所有引用它的相对路径都会断裂。

具体来说，`.gitmodules` 里每条子模块的 `path` 同时承担两重身份：

1. 它是 git submodule 在本地的**检出目录**；
2. 它也是其他库通过相对路径引用该库时的**路径前缀**。

所以 path 的第一段（`VHDL` / `TCL` / `Python` / `VivadoIp`）不只是"分类标签"，而是**真实参与相对路径计算**的目录层级。把 `VHDL/psi_common` 改名成比如 `HDL/psi_common`，会让任何 `../VHDL/psi_common/...` 形式的引用全部失效。

#### 4.6.2 核心流程：改动目录会怎样传播

```
某库 A 的 path 被改名/移动
   │
   ▼
所有用相对路径引用 A 的库（B、C、…）里的路径立即失效
   │
   ▼
仿真（PsiSim）找不到源文件 / 打包（PsiIpPackage）找不到 HDL -> 流程报错
```

这也解释了 README 第 14 行那句"directory structure is important because different libraries reference to each other using relative paths"为什么用"important"这么重的词——它不是风格约定，而是**正确性约束**。

#### 4.6.3 源码精读

[README.md:13-14](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/README.md#L13-L14) —— "This repository is a collection-repo ... The directory structure is important because different libraries reference to each other using relative paths." 这一句是整条约束的权威出处。

[.gitmodules:4-6](https://github.com/paulscherrerinstitute/psi_fpga_all/blob/6b51b7c87a5bcb377a56fc05a275dcf757d03692/.gitmodules#L4-L6) —— `VHDL/psi_common` 的 path。把它当作"被引用方"的例子：path 的每一段都会进入相对路径计算。

> 想看到真实的相对路径引用，需要进入某个**已检出**的子模块内部查看它对兄弟库的引用（例如某个 VivadoIp 的 package.tcl 或某个 VHDL 库对 psi_common 的引用）。本仓库当前未检出子模块内容，具体引用形式**待确认**；但约束本身由 README 明确表述，且被仓库固定的四大类目录结构所体现。

#### 4.6.4 代码实践

**实践目标**：在不动源码的前提下，用推理 + 检索体会"改名即断裂"。

**操作步骤**（源码阅读型实践）：

1. 从 `.gitmodules` 任选一个 VHDL 库（如 `VHDL/psi_common`），写下它的完整 path。
2. 假设把它的目录从 `VHDL/psi_common` 改名为 `VHDL/psi_common_v2`，写出至少一个**可能存在**的相对路径引用会失效（例如 `../psi_common/<某文件>`）。
3. 如果本地已经用 `--recurse-submodules` 检出了子模块，可在子模块内部检索形如 `../VHDL/psi_common` 或 `../../VHDL/` 的字符串，验证真实引用是否存在；若未检出，则记为"待确认"。

**需要观察的现象**：理解 path 的每一段都参与相对路径；改名会让引用失效。

**预期结果**：能说清"path 第一段（四大类目录）+ 子模块名"共同构成被引用的相对路径前缀，因而不能随意改动。具体引用串待本地检出后确认。

#### 4.6.5 小练习与答案

**练习 1**：为什么不能为了"好看"把 `VivadoIp/` 改名成 `IP/`？

> **参考答案**：因为其他库可能用相对路径（含 `VivadoIp/` 这一段）引用这些 IP 的文件。改名会让这些相对路径失效，导致打包/仿真找不到文件。

**练习 2**：如果只是想升级某个子模块的版本（不改变目录），需要改动目录结构吗？

> **参考答案**：不需要。升级版本只是移动子模块的 commit 指针（gitlink），path 保持不变，相对路径引用不受影响。这也是 README 说"可以单独更新某个子模块"的含义。

---

## 5. 综合实践：制作"23 个子模块 × 四大类"全表

**任务**：综合 `.gitmodules` 与 `Changelog.md`，制作一张表，把全部 **23** 个子模块按四大类分组，每行写明：**submodule 名 / 所在目录 / 一句话职责**（职责可结合名字与 README 推断，不确定处标注"待确认"）。

下面给出**已填好前两列**的参考骨架（这两列是 `.gitmodules` 的纯事实，可直接采用），**第三列"一句话职责"留给你完成**：

| 类别 | 所在目录 (path) | 一句话职责（待你填写） |
| --- | --- | --- |
| VHDL | `VHDL/en_cl_fix` | _示例：定点数原语库（Enclustra en_cl_fix 的 fork）_ |
| VHDL | `VHDL/psi_common` | |
| VHDL | `VHDL/psi_fix` | |
| VHDL | `VHDL/psi_tb` | |
| VHDL | `VHDL/psi_multi_stream_daq` | |
| TCL | `TCL/PsiSim` | |
| TCL | `TCL/PsiIpPackage` | |
| Python | `Python/PsiPyUtils` | |
| Python | `Python/VivadoScripting` | |
| Python | `Python/IseScripting` | |
| Python | `Python/TbGenerator` | |
| Python | `Python/PsiFpgaLibDependencies` | |
| VivadoIp | `VivadoIp/vivadoIP_data_rec` | |
| VivadoIp | `VivadoIp/vivadoIP_clock_measure` | |
| VivadoIp | `VivadoIp/vivadoIP_spi_simple` | |
| VivadoIp | `VivadoIp/vivadoIP_axis_data_gen` | |
| VivadoIp | `VivadoIp/vivadoIP_mem_test` | |
| VivadoIp | `VivadoIp/vivadoIP_psi_ms_daq` | |
| VivadoIp | `VivadoIp/vivadoIP_i2c_devreg` | |
| VivadoIp | `VivadoIp/vivadoIP_power_sink` | |
| VivadoIp | `VivadoIp/vivadoIP_fpga_base` | |
| VivadoIp | `VivadoIp/vivadoIP_sync_edge_det` | |
| VivadoIp | `VivadoIp/vivadoIP_axi_mm_reader` | |

**完成建议与自检清单**：

- [ ] 每个类别行数分别为 5 / 2 / 5 / 11，合计 23；
- [ ] 职责列里，凡是仅凭名字推断的，都标注了"待确认"；`en_cl_fix` 的 fork 身份可写成确定结论（依据 README）；
- [ ] 在表下方补一句：哪些库之间可能是"封装/被封装"或"同名主题"关系（如 `vivadoIP_psi_ms_daq` ↔ `psi_multi_stream_daq`），并标注"待确认"；
- [ ] 用本讲 4.1.4 的命令再跑一遍，核对行数与类别分布。

> 这个表格同时也是后续 u2（脚本驱动仿真/打包）、u3（版本管理）单元的"索引页"——学完后面几讲后，你可以回过头来在这张表的"职责"列里补充更精确的工程化描述。

## 6. 本讲小结

- psi_fpga_all 顶层有四大目录：`VHDL/`（核心硬件描述库）、`TCL/`（仿真与打包工具框架）、`Python/`（自动化脚本工具链）、`VivadoIp/`（封装好的 Vivado IP 核）。
- 仓库共 **23** 个子模块，分布为 **VHDL 5 / TCL 2 / Python 5 / VivadoIp 11**；这份计数以 `.gitmodules`（权威清单）为准。
- README 的库列表只是**举例性质**、不完整（只点名 9 个，且完全没有 VivadoIp 类）；清点全貌必须看 `.gitmodules`，Changelog 的分组则印证了四大类划分。
- 每一类在依赖链中有相对固定的位置：VHDL 是底层被依赖方，TCL 是驱动方（PsiSim 仿真 / PsiIpPackage 打包），Python 是周边自动化，VivadoIp 是面向最终用户的成品层。
- `.gitmodules` 里 path 的每一段都参与库之间的相对路径引用，所以四大类目录与子模块目录**不能随意改名或移动**——这是正确性约束，不是风格约定。
- 检索过程中发现的真实细节（如 `vivadoIP_sync_edge_det` ↔ `vivadoIP_sync_det_edge` 的命名演变）说明：**当前以 `.gitmodules` 为准，Changelog 是历史快照**。

## 7. 下一步学习建议

本讲把"仓库里有什么、怎么分类"讲清楚了。接下来：

- **进入 u2（进阶层）**：从"目录"走向"动作"。建议先读 [u2-l1 脚本总览](u2-l1-scripts-overview.md)，看仓库自带的 `scripts/run*.tcl`、`packageAllIp.tcl` 如何驱动 TCL 类的 PsiSim / PsiIpPackage 去仿真与打包本章看到的这些库。
- **进入 u3（专家层）**：当你关心"某个子模块在某个发布里固定到哪个版本"，去读 [u3-l1 发布与版本固定](u3-l1-release-and-version-pinning.md)，深入 Changelog 的版本管理逻辑。
- **延伸阅读**：本讲多次标"待确认"的内容（各库具体接口、IP 端口、相对路径引用的真实形式）都需要进入对应的**子模块仓库**（如 `paulscherrerinstitute/psi_common`、`paulscherrerinstitute/PsiSim`）继续阅读——这也是 collection-repo 学习的自然下一步。
