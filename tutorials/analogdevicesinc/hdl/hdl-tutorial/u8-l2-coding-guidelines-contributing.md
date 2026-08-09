# HDL 编码规范与贡献流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 ADI HDL 仓库的「编码规范（coding guidelines）」由哪些规则构成、它们约束的是什么。
- 理解仓库用 Python 脚本 `check_guideline.py` + GitHub Actions 把其中一部分规则**自动化**检查的机制：查哪些规则、在 PR 里查哪些文件、不通过会怎样。
- 看懂 `CONTRIBUTING.md` 规定的完整贡献流程：fork、分支、原子提交、`Signed-off-by`、PR 描述、合并方式。
- 能拿任意一个 `.v` 文件做一次合规自检，并知道改动后该同步更新哪些附属文件（regmap、Makefile、README、文档）。

本讲是「测试、规范与高级主题」单元的一篇，承接 u4-l1（库结构与多厂商依赖）。前置认知：你已经知道一个 library IP 由若干 `.v` 源码、`*_ip.tcl` 打包脚本和 `Makefile` 依赖桶组成，也知道工程通过 `make` 驱动 Vivado 构建。

## 2. 前置知识

在进入规则细节前，先用三句话建立直觉：

- **为什么要有编码规范？** 一个有近百个 IP、被很多人同时维护的仓库，如果每人按自己习惯写 Verilog，代码很快会无法阅读、无法自动化检查。规范的作用是把「排版、命名、文件结构」统一成一种可被脚本批量校验的形式。
- **`should` 与 `must` 的区别。** 规范把规则分成两类：**must**（必须遵守，硬性）和 *should*（建议遵守，软性）。CI 脚本只检查其中一部分 must 规则；其余靠人工 review 把关。
- **规范 ≠ 性能优化。** 规范只管「写得整齐、命名一致、文件结构标准」；至于「怎么写才能让 FPGA 跑得更快」，规范明确说那由外部白皮书（如 Xilinx wp231）负责，不在本仓库文档范围内。

下面三个术语在本讲会反复出现，先记住：

| 术语 | 含义 |
|------|------|
| **must / should 规则** | 规范用粗体 **must** 标注强制规则，用斜体 *should* 标注建议规则 |
| **guideline check（GC）** | `check_guideline.py` 脚本里、对单条规则做检查的代码段，源码注释里以 `# GC:` 标识 |
| **DCO（Developer Certificate of Origin）** | 提交者声明「这段代码是我有权提交的」，通过 commit 里的 `Signed-off-by` 行表达 |

## 3. 本讲源码地图

本讲涉及的文件分为三组：规范文档、CI 检查脚本与工作流、贡献流程文档。

| 文件 | 作用 |
|------|------|
| `docs/user_guide/hdl_coding_guidelines.rst` | 编码规范的**唯一权威来源**，列出 Layout / Naming / Comments / General 四大类规则与 Verilog/VHDL/SystemVerilog 三个文件模板 |
| `CONTRIBUTING.md` | 贡献流程的**入口文档**（仓库根目录，GitHub 会在开 PR 时自动展示） |
| `docs/user_guide/contributing.rst` | 与 `CONTRIBUTING.md` 内容对应的 Sphinx 渲染版，带交叉引用 |
| `.github/scripts/check_guideline.py` | 实际执行规则检查的 Python 脚本，约 1900 行 |
| `.github/scripts/README.md` | 说明 `check_guideline.py` 查哪些规则、怎么运行 |
| `.github/scripts/check_readme.sh` | 检查工程 README 是否齐全、标题与必含小节是否合规的 bash 脚本 |
| `.github/workflows/check_for_guideline_rules.yml` | 在 PR 上自动跑 `check_guideline.py` 的 GitHub Action |
| `.github/workflows/readme_checker.yaml` | 在 PR 上自动跑 `check_readme.sh` 的 GitHub Action |
| `.github/CODEOWNERS` | 按目录指定「代码所有者」，PR 合并前需至少一位所有者批准 |
| `README.md` | 仓库总说明，顶部徽章即指向上述 CI 工作流 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**HDL 编码规范**、**CI 的 guideline/lint 检查**、**贡献与 PR 流程**。

### 4.1 HDL 编码规范

#### 4.1.1 概念说明

`hdl_coding_guidelines.rst` 是全仓 Verilog/VHDL/SystemVerilog 代码的「宪法」。它的目标用一句话讲：**规定版式、命名、注释和文件结构，使代码统一、可读、可被脚本批量校验**。

规范明确把自己的边界划清：它**不**教你怎么写出高性能 FPGA 逻辑——那是外部白皮书（Xilinx wp231、Peter Chambers「十诫」）的事。规范只管「长相」。

规则按四类组织：

1. **A. Layout（版式）**：空行、缩进、空格、括号、`begin/end`、对齐、模块声明与例化的书写格式。条目最多（A1–A22），是脚本检查的主战场。
2. **B. Naming Conventions（命名）**：英文、小写下划线、参数大写、信号/端口后缀含义（`_p`/`_n`/`_ns` 等）、跨层级时钟复位同名。
3. **C. Comments（注释）**：注释要描述功能、罕见实现必须解释、综合属性必须注明用途。
4. **D. General（通用）**：一文件一模块、端口逐一声明、位宽必须匹配、组合逻辑必须完备、warning 当 error、license 头必含。

理解这四类的一个关键是分清 **must / should**：

- 规范原文 [hdl_coding_guidelines.rst:20-22](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L20-L22) 写明：**must** 是强制，*should* 是建议。后续每条规则都标注了归属。

#### 4.1.2 核心流程

把规范当成一个「文件检查清单」来理解，按一个 `.v` 文件从上到下的物理结构梳理 must 规则：

```text
文件首尾 ── A1: 不以空行开头/结尾，恰好一个换行结尾
         ── D13: 必含 license 头；改文件时应更新年份
license 头 ── Annex 1: 固定版权注释块 + `timescale
模块声明 ── A12.1: Verilog-2001 风格 #( parameter ... ) ( 端口 )
         ── A17: 时钟/复位端口先声明
         ── A14: 每个端口单独一行，带方向与类型
         ── A16: 按接口分组，方向顺序 input→inout→output
内部声明 ── A20: localparam 先于 reg/wire
         ── A18: reg 段在前，wire 段在后
逻辑体   ── A5: begin/end 永远写全
         ── A6: 缩进体现嵌套（2 空格倍数）
         ── A9: 布尔式与复杂表达式一律加括号
         ── D7: 组合逻辑须对全部输入组合赋值
模块例化 ── A11.1: 每个参数/端口单独一行
         ── A11.2: 实例标签单独一行
         ── D9: 列出全部 I/O，未用输入接 0/1
文件命名 ── B4: 文件名 = 模块名；一文件一模块（D1）
         ── B3: 模块/信号名小写下划线；B5: 参数大写
```

命名规则有一条对阅读源码特别有用的约定——**信号后缀语义**（[hdl_coding_guidelines.rst:601-626](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L601-L626)）：

- `_ns` 状态机次态；`_l` 锁存输出；`_p`/`_n` 差分正/负或低有效；`_m1/_m2` 同步器两级寄存（如 `up_ack_m1`、`up_ack_m2`）；`_s` 限定线。

读到 `up_ack_m1` 你就该知道这是跨时钟域同步的第一级——这条命名规则在 u4-l5 讲的 `up_axi`、u5-l1 讲的 `axi_dmac` 响应通路里都会反复出现。

#### 4.1.3 源码精读

**（1）版式规则举例：A1 / A2 / A5 / A6**

这几条是脚本直接检查的 must 规则。

- A1（[hdl_coding_guidelines.rst:39-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L39-L40)）：源文件**不得**以空行开头或结尾，但**必须**以恰好一个换行符结尾。
- A2（[hdl_coding_guidelines.rst:44-48](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L44-L48)）：用空格代替 tab，行尾不留空格，编辑器统一 *Tab Size: 2, Indent Size: 2*。
- A5（[hdl_coding_guidelines.rst:93-95](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L93-L95)）：`begin/end` 块**必须**始终写出，即使只有一条语句——这样以后加代码不易出错。
- A6（[hdl_coding_guidelines.rst:99-101](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L99-L101)）：用缩进层级体现嵌套。

A6 的正反例很能说明规范的风格（[hdl_coding_guidelines.rst:110-136](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L110-L136)）：错误写法里 `else` 后直接跟单条语句、缩进错乱；正确写法里 `end else begin` 同行、每层 +2 空格、末尾收尾规范。

**（2）模块声明与例化格式：A11.2 / A12.1**

A11.2（[hdl_coding_guidelines.rst:300-320](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L300-L320)）规定模块例化的标准长相：实例标签单独成行，参数表的右括号与端口表的左括号同行，端口表右括号紧贴最后一个端口。

A12.1（[hdl_coding_guidelines.rst:333-334](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L333-L334)）要求 Verilog 模块用 Verilog-2001 风格的参数声明（`module name #( parameter ... ) ( 端口 )`）。仓库里几乎所有文件都遵循它，例如 `up_axi.v` 的模块头：

```verilog
module up_axi #(

  parameter   AXI_ADDRESS_WIDTH = 16
) (
```

对应源码 [up_axi.v:38-41](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L38-L41)。

**（3）license 头与文件模板：D13 + Annex 1**

D13（[hdl_coding_guidelines.rst:790-793](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L790-L793)）：每个文件**必须**含 license 头；修改文件时，开 PR 应把版权年份更新到当前年。

Annex 1（[hdl_coding_guidelines.rst:798-853](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L798-L853)）给出 Verilog 文件的完整模板：固定的双星号版权块（L804-837）、`timescale` 行（L839）、按「localparam → reg → wire → function → always → 例化」分节的模块体。`up_axi.v` 的开头就是这个模板的真实落地——版权块见 [up_axi.v:1-34](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L1-L34)，版权年份行 `Copyright (C) 2014-2023` 见 [up_axi.v:3](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v#L3)。

> 注意：规范对版权年份的要求是「**改文件时**更新到当前年」（should）。所以一个写着 `2014-2023` 的文件本身不一定违规——只有当你这次 PR 修改了它，才需要把年份补到当前年。CI 也只检查 PR 中**被改动的**文件（见 4.2）。

#### 4.1.4 代码实践

**实践目标**：用一个真实文件，手动套用规范做一次自检，建立「规则 ↔ 代码」的对应感。

**操作步骤**：

1. 打开 [library/common/up_axi.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/common/up_axi.v)。
2. 对照规范，逐条核查下表（只看前 ~120 行即可覆盖大多数条目）：

   | 规范条目 | 检查内容 | 在 up_axi.v 中的位置 |
   |----------|----------|----------------------|
   | D13 + Annex 1 | 是否有标准 license 头 | L1-34 |
   | D10 | 是否有 `timescale` | L36 |
   | A12.1 | 模块是否用 `#( parameter ) ( 端口 )` 风格 | L38-41 |
   | A17 | 时钟/复位端口是否最先声明 | 紧随 `) (` 之后 |
   | A14 / A16 | 端口是否每行一个、按接口分组、方向顺序正确 | 端口列表区 |
   | A20 | localparam 是否在 reg/wire 之前 | 模块体开头 |

3. 再打开 [hdl_coding_guidelines.rst:298-320](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L298-L320)（A11.2 正例），到 `up_axi.v` 内部找一处子模块例化，核对它的标签是否单独成行、端口是否每行一个。

**需要观察的现象**：规范的每一条都能在真实代码里找到「正确范例」；这不是巧合，而是因为这些文件正是按规范写的。

**预期结果**：你会确认 `up_axi.v` 在 license 头、timescale、模块声明、端口分组、缩进上都符合 must 规则。

**说明**：本实践为源码阅读型，不运行任何命令；具体某文件是否完全合规以 4.2 的脚本输出为准，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：规范为什么要求「一文件一模块、且文件名等于模块名」（B4/D1）？

> **参考答案**：便于按文件名定位模块、便于脚本（如 `check_guideline.py`）自动比对「文件名 vs 模块名」是否一致，也便于构建系统按模块名收集依赖。仓库规定这种比对仅在 `library/` 下强制（见 4.2.3）。

**练习 2**：`should` 规则和 `must` 规则在 CI 层面有什么实际差别？

> **参考答案**：CI 脚本只检查**部分 must** 规则（如版权年份、空行、行尾空格、缩进、模块声明/例化位置、文件名=模块名等）；其余 must 规则与全部 should 规则靠人工 code review 把关。不满足被脚本覆盖的 must 规则会直接让 PR 的 GitHub Action 失败。

**练习 3**：读到信号名 `up_ack_m1`、`up_ack_m2`，依据规范你能推断什么？

> **参考答案**：依据 B6 的后缀语义（[hdl_coding_guidelines.rst:617-620](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/hdl_coding_guidelines.rst#L617-L620)），`_m1/_m2` 表示「同步器的两级寄存」。所以这是把 `up_ack` 跨时钟域打两拍同步的中间寄存器，常见于 `up_axi` 的应答通路。

### 4.2 CI 的 guideline 与 lint 检查

#### 4.2.1 概念说明

规范文档很长，靠人工逐条 review 不现实。仓库用两套自动化检查把其中**可机械判定**的规则变成 CI 门禁：

- **`check_guideline.py`**（Python，~1900 行）：扫描 `.v`/`.sv` 源码，检查版式、命名一致性、版权头、模块声明与例化格式等。这是「guideline check」。
- **`check_readme.sh`**（bash）：检查每个工程目录的 README 是否齐全、标题格式、必含小节是否合规。
- **Verilator**（外部工具）：lint 级别的语法/可疑构造检查。`CONTRIBUTING.md` 把「Run Verilator」列为提交前**人工**步骤。

三者关系：脚本查「长相」，Verilator 查「语义可疑」，人工 review 查「逻辑与 should 规则」。

关键认知：**CI 只检查 PR 里被改动的文件**，而不是全仓扫描。这既是为了速度，也呼应了 D13「改文件时才需更新版权年份」的语义。

#### 4.2.2 核心流程

 guideline check 在 PR 上的自动执行流程：

```text
开发者向 main 开 PR（改动 library/** 或 projects/**）
        │
        ▼
GitHub Action: check_for_guideline_rules.yml 触发
        │  - setup-python 3.10
        │  - checkout 代码
        │  - 用 get-changed-files 取出本次 PR 改动的文件清单
        ▼
python check_guideline.py -p <改动文件相对路径列表>
        │  逐文件：
        │    detect_file_unit_ → 判定是 module 还是 package
        │    get_and_check_module / get_and_check_package → 填充 lw（告警列表）
        │    find_occurrences → 在改动文件里找模块例化，检查例化格式
        │    check_project_name_vs_path → system_project.tcl 里工程名是否匹配路径
        ▼
若任一文件的 lw 非空 → guideline_ok=False
        │
        ▼
guideline_ok?  是 → sys.exit(0)（CI 绿）   否 → 打印 "GUIDELINE RULES ARE NOT FOLLOWED" → sys.exit(1)（CI 红，PR 不可合并）
```

退出码就是门禁：`sys.exit(1)` 让 GitHub Action 标红，`CONTRIBUTING.md` 明确「CI 上构建/检查失败的东西不能合并」。

README 检查走另一条独立的 Action（`readme_checker.yaml`），只关心 `projects/**` 下的 README。

#### 4.2.3 源码精读

**（1）触发条件：只看 PR、只看改动文件**

工作流 [check_for_guideline_rules.yml:9-15](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/workflows/check_for_guideline_rules.yml#L9-L15) 声明：仅在向 `main` 的 pull_request、且路径命中 `library/**` 或 `projects/**` 时触发。

随后用 `Ana06/get-changed-files` 取出本次改动的文件（[check_for_guideline_rules.yml:30-39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/workflows/check_for_guideline_rules.yml#L30-L39)），再用 `-p` 把它们作为相对路径传给脚本（[check_for_guideline_rules.yml:43-44](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/workflows/check_for_guideline_rules.yml#L43-L44)）：

```yaml
run: |
  python ./.github/scripts/check_guideline.py -p ${{ steps.changed_files.outputs.added_modified_renamed }}
```

`-p` 的含义见脚本说明 [README.md:277-279](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L277-L279)：按相对路径指定文件。

**（2）脚本主循环与门禁：guideline_ok / sys.exit**

主循环在文件尾段。对每个改动文件，调用模块或包检查器把告警填进 `lw` 列表；只要任一文件 `lw` 非空，全局标志 `guideline_ok` 置假（[check_guideline.py:1855-1859](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1855-L1859)）：

```python
if (len(lw) > 0):
    guideline_ok = False
    print ("\n -> For %s in:" % file_path)
    for message in lw:
        print(message)
```

此外还有一条专门规则：在 `library/` 下，若**文件名 ≠ 模块名**，记入 `error_files` 并置 `guideline_ok=False`（[check_guideline.py:1837-1842](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1837-L1842) 与 [check_guideline.py:1884-1900](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1884-L1900)）。注释说明：对 `projects/` 目录这条不强制（工程顶层文件命名较自由）。

最终门禁（[check_guideline.py:1902-1906](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1902-L1906)）：

```python
if (not guideline_ok):
    print("\nGUIDELINE RULES ARE NOT FOLLOWED\n")
    sys.exit(1)
else:
    sys.exit(0)
```

**（3）被检查的规则集：脚本 README 与 GC 标记**

脚本支持哪些规则，由 [README.md:124-239](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L124-L239) 列出，共 10 组，对应规范里的：license 头年份、空行（连续/首尾）、行尾空格、`endmodule`/`endpackage` 之后的多余行、模块声明括号位置、缩进（2 空格倍数）、模块例化位置、SystemVerilog package 格式、typedef 格式、`system_project.tcl` 工程名 vs 路径。

在脚本源码里，每条规则的检查段都用 `# GC:`（Guideline Check）注释标识，方便定位，例如行尾空格检查在 [check_guideline.py:981](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L981)，缩进检查在 [check_guideline.py:1105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1105)，模块声明位置在 [check_guideline.py:1123](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L1123)。

**（4）版权头检查与例外名单**

版权检查函数 `check_copyright` 在 [check_guideline.py:560](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L560)，它用 `datetime.now().year` 取「当前年」与文件里的年份比对（变换规则见 [README.md:136-156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L136-L156)）。

有少数文件被豁免，由名单 `avoid_list` 控制（[check_guideline.py:538-548](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/check_guideline.py#L538-L548)）：

```python
avoid_list = []
avoid_list.append("fir_interp")
avoid_list.append("cic_interp")

def header_check_allowed (module_path):
    for str in avoid_list:
        if (module_path.find(str) != -1):
            return False
    return True
```

即路径里含 `fir_interp`、`cic_interp` 的文件不做版权头检查（这些通常是第三方来源的滤波器 IP）。

**（5）`-e` 自动修复模式**

脚本带 `-e` 时不仅能检查，还能**自动改**（见 [README.md:241-253](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L241-L253)）：修版权年份、删多余空行/行尾空格、删 `endmodule` 后的多余行、归位模块声明括号、重写 typedef 与 package 格式、校正工程名。CI 跑的是**不带 `-e`** 的纯检查模式。

#### 4.2.4 代码实践

**实践目标**：在本地对一个真实文件跑一次检查器，观察它如何按规则输出告警。

**操作步骤**：

1. 在仓库根目录执行（纯检查，不修改文件）：

   ```sh
   python3 .github/scripts/check_guideline.py -p ./library/common/up_axi.v
   ```

2. 观察输出里是否出现版权年份相关的告警。
3. 想看自动修复会怎么改，可对**副本**试跑（避免改源码）：先复制一份到临时目录再对副本加 `-e`。本讲义**不**要求你修改仓库源码。

**需要观察的现象**：

- 若运行时的「当前年」晚于 `up_axi.v` 版权行里的 `2023` 超过 1 年，脚本会按 [README.md:136-145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L136-L145) 的规则报告：建议把 `2014-2023` 扩成范围或追加年份（如追加 `, 当前年`）。
- 退出码：有告警时为 `1`，无告警时为 `0`。

**预期结果**（基于文档规则的推断，**待本地验证**）：`up_axi.v` 的版权行 `2014-2023` 在「当前年 − 2023 > 1」时会触发版权告警；版式/缩进/模块声明部分应基本合规（该文件长期被脚本维护）。

**说明**：本讲义编写时未实际执行该命令（运行需授权），上述预期是基于 [README.md:136-156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L136-L156) 文档规则的推断；请以你本地实际运行结果为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CI 用 `get-changed-files` 只把改动的文件传给脚本，而不是全仓扫描？

> **参考答案**：一是速度（全仓近千个 `.v` 文件）；二是语义一致——D13 要求「改文件时才更新版权年份」，只检查改动文件恰好对应「谁改谁负责补年份」，避免让没动过的历史文件批量报红。

**练习 2**：`avoid_list` 里为什么要把 `fir_interp`、`cic_interp` 排除在版权检查外？

> **参考答案**：这些通常是来源特殊（如第三方或自动生成）的滤波器 IP，其版权头格式与 ADI 标准模板不一致，强行检查会一直误报，故单独豁免。

**练习 3**：`README.md`（仓库根目录说明）顶部有一个指向 `test_n_lint.yml` 的徽章，但当前 HEAD 的 `.github/workflows/` 下并没有这个文件。这说明什么？你该如何对待它？

> **参考答案**：该工作流可能在某次重构中被改名或移除，而徽章链接未同步更新。`CONTRIBUTING.md` 仍把「Run Verilator」当作**人工**提交前步骤。所以 Verilator lint 当前更可能依赖贡献者本地执行与人工 review，而非一条仍在生效的 CI 工作流；以仓库实际存在的 `check_for_guideline_rules.yml` 与 `readme_checker.yaml` 为准。

### 4.3 贡献与 PR 流程

#### 4.3.1 概念说明

`CONTRIBUTING.md`（根目录）与 `docs/user_guide/contributing.rst`（Sphinx 渲染版）是同一份内容的两个载体。它把「如何给本仓库贡献代码」拆成四段：

1. **PR 规则（Pull request rules）**：commit 必须带 `Signed-off-by`（DCO）、首次贡献要签 CLA、commit 要原子、commit 标题要带文件路径、PR 要有简明描述、需 code owner 批准。
2. **开 PR 之前（Before opening）**：fork、分支、对照代码检查清单、更新 regmap 与 Makefile、跑 `check_guideline.py`、跑 Verilator、构建须通过（Critical Warnings 不接受）、硬件测试、rebase 到最新 main。
3. **开 PR 时（When opening）**：PR 描述要写动机/关联 issue/关联 PR、勾选清单、加 label、看 Actions 结果；review 后**禁止 force-push**、每次改动至少一个新 commit、冲突在终端解决（不要用 GUI，避免产生 merge commit）。
4. **代码检查清单（Code-related check list）**：遵循编码规范、跑脚本、检查所用公共 IP 是否变了端口、新 IP 要加进 Makefile 依赖、更新 README。

一条贯穿全程的硬约束：**仓库不要 merge commit**。这就是为什么冲突必须在命令行 rebase 解决、而不是用 GUI 合并。

#### 4.3.2 核心流程

一次合规贡献的时间线：

```text
① 先在 issue tracker 讨论你要做的改动（避免白做）
② fork 仓库 → 在分支上开发，定期 rebase 到 main
③ 开发中对照「代码检查清单」：
     - 改了寄存器？→ 同步更新 docs/regmap 文本 + 语义化版本号
     - 用了新 IP？→ 加进工程 Makefile 的 LIB_DEPS
     - 公共 IP 端口变了？→ 更新所有例化
     - 改了行为？→ 更新/新建对应文档与 README
④ 本地自检：python3 check_guideline.py -p <改动文件> + Verilator + 构建（无 Critical Warning）
⑤ 硬件验证（尽可能多套环境）
⑥ 开 PR：描述写动机/issue/依赖 PR；勾清单、加 label
⑦ 等 code owner review；有修改就追加新 commit（不 force-push）
⑧ 批准后：Rebase and merge / Squash and merge / 本地 squash 后 force-push（无代码改动则无需重审）
```

#### 4.3.3 源码精读

**（1）DCO 与原子提交**

PR 规则第一条（[CONTRIBUTING.md:32-37](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L32-L37)）：commit message 必须含 `Signed-off-by: [name] <email>`，表示你同意 [Developer Certificate of Origin](https://developercertificate.org/)——即声明你有权提交这段代码。不能同意 DCO 就别开 PR。`git commit -s` 会自动加上这行。

原子提交要求（[CONTRIBUTING.md:41-43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L41-L43)）：一个 commit 只做一件事；只有当修 bug/实现特性确实需要时，PR 才含多个 commit。

**（2）commit 标题格式**

[CONTRIBUTING.md:47-49](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L47-L49) 给出标题范式：先写改动文件的路径，再用几个词说明做了什么，例如：

```
projects/ad9081_fmca_ebz/zcu102: Add missing clock constraint
```

这与本仓库 git log 完全一致（如近期提交 `projects/adrv903x: Add XCVR automation support`）。

**（3）code owner 审批**

[CONTRIBUTING.md:51-53](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L51-L53)：PR 只有在被 review、测试、并由 [`.github/CODEOWNERS`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/CODEOWNERS) 里的代码所有者批准后才会合并。该文件按目录分配所有者——PR 改到哪个目录，对应所有者会被自动加为评审。

**（4）提交前的代码检查清单**

[CONTRIBUTING.md:69-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L69-L74) 列出关键自检项：

- 跑 `check_guideline.py`；
- 跑 Verilator；
- 受影响工程必须构建通过；**Warnings 会被 review，Critical Warnings 不被接受**，且要在 Windows 和 Linux 都能构建——CI 上构建失败者不可合并。

代码检查清单段（[CONTRIBUTING.md:123-140](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L123-L140)）补充三点：遵循编码规范并跑脚本（L128-133）；检查所用公共 IP（如 `up_adc_common`、`up_delay_cntrl`）端口是否变化（L134-137）；新用到的 IP 要加进工程 Makefile 依赖（L138-139）——这条直接对应 u4-l1 讲的 `LIB_DEPS` 机制。

**（5）寄存器与版本：Devicetree 绑定**

[CONTRIBUTING.md:64-69](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L64-L69) 要求改寄存器时同步更新 `docs/regmap` 文本，且 IP 遵循 [语义化版本 2.0.0](https://semver.org/)。版本号还会传到软件侧：devicetree compatible 取主版本号加 `v` 前缀（如 `axi_my_ip` v1.2.3 的 compatible 是 `adi,axi-my-ip-v1`），驱动应解析 `VERSION` 寄存器做特性适配（[CONTRIBUTING.md:142-157](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L142-L157)）。这正是 hdl ↔ no-OS/Linux 三仓经寄存器映射对接的接口约定（承接 u4-l5）。

#### 4.3.4 代码实践

**实践目标**：把贡献流程具象化为一次可操作的「提交演练」——不真的开 PR，而是产出合规的 commit 与配套清单。

**操作步骤**：

1. 假设你给 `library/common/up_axi.v` 做了一个小修改（例如修一处注释）。按规范，你需要同步把版权年份更新到当前年（D13）。
2. 用规范的 commit 标题与 DCO 签名提交（示例命令，**示例代码**）：

   ```sh
   git commit -s library/common/up_axi.v \
     -m "library/common/up_axi: Update comment and copyright year"
   ```

   `git commit -s` 会自动追加 `Signed-off-by:` 行。用 `git log -1 --format=%B` 核对 message 是否含该行。
3. 按 [CONTRIBUTING.md:55-81](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L55-L81) 的清单逐项自检：是否需要改 regmap？是否动到公共 IP 端口？是否需要更新 README？

**需要观察的现象**：`git log -1` 显示的 message 末尾应有 `Signed-off-by: 你的名字 <你的邮箱>`；标题以改动文件路径开头。

**预期结果**：你能产出一条原子、标题合规、带 DCO 签名的 commit，并说清它需要连带更新哪些附属文件。

**说明**：本实践不要求推送或开真实 PR；如要在本地试跑 `git commit`，请在 fork 上进行，避免影响本仓库。命令的具体行为以你本地 git 版本为准，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：reviewer 要求你改一个拼写错误，你应该怎么做？

> **参考答案**：**不要** force-push 覆盖原 commit；新开一个 commit 修拼写（[CONTRIBUTING.md:97-101](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L97-L101)），并留一条评论说明改了什么。批准后再选 Rebase/Squash 合并。

**练习 2**：你的分支和 main 上的别人改动冲突了，能用 GitHub 的 GUI 合并按钮解决吗？

> **参考答案**：不能。GUI 解决冲突会插入 merge commit，而本仓库**不要 merge commit**（[CONTRIBUTING.md:117-121](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/CONTRIBUTING.md#L117-L121)）。应在终端用 `git rebase` 解决。

**练习 3**：你给某个 IP 新增了一个寄存器位，PR 里至少要同步改哪些非源码文件？

> **参考答案**：（1）`docs/regmap` 下该 IP 对应的寄存器文本；（2）若改动影响软件，按语义化版本升 IP 版本号，并视情况更新 devicetree compatible 与驱动对 `VERSION` 寄存器的解析；（3）受影响工程的 README 与（若引入新依赖）Makefile 的 `LIB_DEPS`。

## 5. 综合实践

把三个模块串起来，做一次完整的「合规改造 + 提交演练」。

**任务**：挑选 `library/` 下任意一个 `.v` 文件（例如 `library/common/up_axi.v`），完成以下全部步骤：

1. **规范自检**：对照 `hdl_coding_guidelines.rst`，列出该文件在 license 头、timescale、模块声明（A12.1）、端口分组（A14/A16/A17）、localparam 先行（A20）上的合规情况，填一张表。
2. **脚本验证**：运行 `python3 .github/scripts/check_guideline.py -p <该文件相对路径>`，把脚本输出与你的人工判断对比，记录差异。**待本地验证**。
3. **版权年份判定**：读出该文件版权行的年份，依据 [README.md:136-156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/.github/scripts/README.md#L136-L156) 的规则，推断「假设本次 PR 修改了它、当前年为今年」时脚本会建议把它改成什么。
4. **提交演练**：为「修改该文件并更新版权年份」起草一条合规 commit——标题含路径、message 含动机、用 `git commit -s` 带 DCO 签名；并写出该改动连带需要检查的附属文件清单（regmap / Makefile `LIB_DEPS` / README）。
5. **门禁推理**：说明如果这条 PR 触发了 `check_for_guideline_rules.yml`，哪些情况会让 CI 标红、退出码是多少、对合并有什么影响。

**验收标准**：你能用一句话说清「规范文档 → 检查脚本 → CI 工作流 → PR 流程」这四者如何首尾相连地把一段代码送进 main。

## 6. 本讲小结

- 编码规范（`hdl_coding_guidelines.rst`）分 Layout / Naming / Comments / General 四类，区分 **must**（强制）与 *should*（建议），只管「版式、命名、文件结构」，不管性能优化。
- 关键 must 规则包括：一文件一模块且文件名=模块名、Verilog-2001 参数风格、时钟/复位端口先行、端口按接口分组且逐行声明、localparam 先于 reg/wire、license 头必含且改动时更新年份。
- CI 用 `check_guideline.py` 把规范中**可机械判定**的子集自动化，仅在 PR 上检查**改动文件**，任一告警即 `sys.exit(1)` 让 Action 标红、PR 不可合并。
- 脚本支持 `-e` 自动修复；版权检查对 `fir_interp`/`cic_interp` 等有 `avoid_list` 豁免；`library/` 下强制文件名=模块名，`projects/` 下不强制。
- 贡献流程核心四件事：先在 issue 讨论、fork+分支开发、本地跑脚本+Verilator+构建（无 Critical Warning）、开带 DCO 签名的原子 commit PR，由 CODEOWNERS 批准后合并。
- 两条硬约束：commit 带 `Signed-off-by`（DCO）；**不要 merge commit**——冲突在终端 rebase 解决，review 后不 force-push、追加新 commit。

## 7. 下一步学习建议

- 想看规范在「最难维护」的代码里如何落地，建议精读 u8-l1（仿真与测试平台）涉及的 `library/axi_dmac/tb/` 与 `tb_base.v`，并用本讲的脚本对它们自检（注意：路径含 `tb` 的文件被脚本忽略，可对比「为何 testbench 被豁免」）。
- 想深入「性能与时序」这条规范明确划出去的主题，接着学 u8-l3（收发器、时钟与时序约束），看 `auto_timing_fix_xilinx.tcl` 与 xdc/sdc 如何在构建期收敛时序。
- 准备真正提交改动时，回到本讲 4.3，按 `CONTRIBUTING.md` 的检查清单逐项过一遍，并结合 u7-l2（创建与定制新工程）理解新 IP 如何登记进 Makefile `LIB_DEPS` 与 README 模板。
