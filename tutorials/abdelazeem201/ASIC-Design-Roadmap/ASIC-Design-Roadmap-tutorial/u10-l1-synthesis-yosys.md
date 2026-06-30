# 逻辑综合与开源工具 yosys

> 本讲属于 **U10 综合、开源工具与端到端实战**，定位为专家层（advanced）。
> 前置：你已经学过 [u2-l1 读懂一个简单 Verilog 设计](u2-l1-verilog-basic-design.md)，认识模块、端口、时序/组合 `always` 块与例化。
> 本讲只讲「综合是什么、怎么写能被综合的 RTL、开源综合器 yosys 的轮廓、综合与时序库的关系」，**不**真正跑综合（slack 与面积数字待本地用综合工具验证）。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 用一句话说清 **逻辑综合（Logic Synthesis）** 在 ASIC 流程里的位置：把 RTL 翻译成由具体标准单元组成的门级网表（gate-level netlist）。
2. 区分 **行为级 RTL** 与 **结构级网表**，理解综合是「RTL → 网表」的单向翻译，且翻译结果依赖目标工艺库。
3. 说出可综合 RTL 的几条核心编码风格规则，并能判断 `MY_DESIGN.v` 里哪些写法「能综合但不推荐」。
4. 描述开源综合器 **yosys** 的基本流程轮廓（`read_verilog → synth → write_verilog`），并能指出其细节需以本仓库的 `yosys_manual.pdf` 手册为准。
5. 把综合这一步与本仓库的 PnR 流程串起来：解释综合产出的网表如何被 `03_PnR_setup.tcl` 的 `read_verilog` 读入。

---

## 2. 前置知识

本讲用到的几个术语，先用大白话解释：

- **RTL（Register Transfer Level，寄存器传输级）**：用 Verilog 写的、描述「数据在寄存器之间如何流动和运算」的代码。它描述**行为**（要做什么），不描述具体用了哪个厂家的哪个门。
- **网表（netlist）**：一堆「具体单元 + 它们之间的连线」的清单，是结构化的、贴近硬件的。综合的**产物**就是网表。
- **标准单元（standard cell）**：工艺厂家提供的一个个事先设计好的「积木」，如 `AND2_X1`、`DFFR_X4`、`CLKBUF_X3`。每个单元在时序库里有延迟，在物理库里有尺寸。
- **时序库 / Liberty（`.lib`、`.db`）**：描述每个标准单元延迟、功耗、面积的数据文件（详见 [u3-l1 标准单元库与物理数据基础](u3-l1-standard-cell-libraries.md)）。综合时工具要查它才能做时序判断。
- **PPA**：Power（功耗）、Performance（性能/频率）、Area（面积）。综合就是在三者间反复权衡（详见 [u1-l1](u1-l1-project-overview.md)）。
- **SDC（Synopsys Design Constraints）**：用 Tcl 命令告诉工具时钟周期、I/O 延迟等外部时序环境（详见 [u2-l3](u2-l3-sdc-timing-constraints.md)）。综合和 PnR 都吃 SDC。

一句话定位：**综合是连接「写 RTL」和「做版图」的那座桥**——RTL 在桥这头，GDSII 在桥那头，综合负责把 RTL 变成 PnR 工具能消化的网表。

> ⚠️ 关于本仓库的诚实说明：仓库里**并没有**一份可运行的 Synopsys Design Compiler（DC）或 yosys 综合脚本。PnR 脚本（`03_PnR_setup.tcl`）假设网表已经在外部「综合目录」里生成好了，直接 `read_verilog` 读它。所以本讲我们 **看 RTL、看 PnR 怎么接网表、讲综合的概念**，把 yosys 作为「你可以自己补上这一步」的开源选项来介绍。yosys 命令的具体开关需以 `yosys_manual.pdf` 手册为准（见 4.3）。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲怎么用 |
|------|------|-----------|
| [MY-Design/MY_DESIGN.v](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v) | 可综合 RTL 样例（顶层 `MY_DESIGN` + 子模块 `ARITH`/`COMBO`） | 作为「综合的输入」来讲可综合编码风格 |
| [IC Compiler II/Scripts/01_common_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) | ICC2 PnR 的公共变量定义 | 看它如何指向「综合产出的网表」 |
| [IC Compiler II/Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | ICC2 PnR setup 阶段（建库/读网表/link） | 看 `read_verilog` 把网表接入 PnR |
| [yosys_manual.pdf](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/yosys_manual.pdf) | yosys 官方手册（PDF，二进制） | 综合流程细节的权威出处（细节待确认） |
| [Guide to HDL Coding Styles for Synthesis/ReadMe](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Guide%20to%20HDL%20Coding%20Styles%20for%20Synthesis/ReadMe) | 目录占位文件，指向同目录的 `synco_1..5.pdf`（Synopsys HDL 编码风格指南） | 可综合编码风格的参考资料出处 |
| [README.md](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md) | 仓库总览 | 印证「先仿真再综合」的工程理念 |

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：**①综合目标 → ②可综合 RTL 编码风格 → ③yosys 综合流程概述（待确认）→ ④综合与时序库的关系**。

---

### 4.1 综合目标：把 RTL 翻译成门级网表

#### 4.1.1 概念说明

**逻辑综合** = 在给定**目标工艺库**和**时序约束（SDC）**下，把行为级的 RTL 自动翻译成由标准单元构成的结构级网表。

打个比方：RTL 是「我要一个能根据 `sel` 选择做加法还是减法的电路」这种**需求描述**；网表是「用 1 个 `FA_X2` 全加器 + 1 个 `MUX2_X2` + 1 个反相器，按下图连起来」这种**购物清单 + 接线图**。综合器就是那个把需求翻成清单的翻译官，但翻译官手边必须有一本「这家工厂有什么积木」的目录——也就是**目标库**。

关键认知：

- 综合是**有损、有依赖**的翻译：同一个 RTL，换一个工艺库，得到的网表完全不同（积木不同）。
- 综合不是「跑一遍就完」。它要反复做 **mapping（映射到单元）→ sizing（选大小/驱动强度）→ buffering（插 buffer 修时序）**，在 PPA 之间权衡。
- 综合的**输入**：RTL + SDC + 目标库（Liberty）。**输出**：门级网表（`.v`）+ 综合后的 SDC + 面积/时序报告。

#### 4.1.2 核心流程

一个典型的综合流程（工具无关）：

```
读 RTL ──► 读 SDC（时钟/I/O 延迟） ──► 读目标库（Liberty）
        │
        ▼
   逻辑优化 & 技术映射（RTL → 门）
        │   - 展开位运算、状态机提取
        │   - 选具体单元（AND/OR/DFF/…）
        │   - 时序驱动：插 buffer、换大驱动单元修 setup
        ▼
   输出门级网表（.v） + 报告（时序/面积/功耗）
```

最终网表会被下游 PnR 的 `read_verilog` 读入——这就是综合在整个 RTL→GDSII 主线里的位置。

#### 4.1.3 源码精读：综合产物的「消费点」

本仓库没有综合脚本，但**有综合产物的消费点**。看 `01_common_setup.tcl` 怎么指向网表：

[IC Compiler II/Scripts/01_common_setup.tcl:34-36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L34-L36) —— 定义 `SYN_DIR`（外部综合目录）、`VERILOG_NETLIST_FILES`（网表路径 `$SYN_DIR/output/${DESIGN_NAME}.v`）和 `SDC_CONSTRAINTS`（综合后 SDC）。**这一行就是「综合」与「PnR」的物理交接点**：综合工具把网表写到这个路径，PnR 工具从这读。

> 设计名本身在 [01_common_setup.tcl:8](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L8-L8) 定义为 `pit_top`——注意它和教学用的 `MY_DESIGN` 不是一回事，模板是按一个真实项目（`pit_top`）配的。

注意 `set DESIGN_NAME "pit_top"` 与网表文件名 `${DESIGN_NAME}.v` 是**约定耦合**的：综合工具输出的顶层模块名和文件名，必须与 PnR 这里设的 `DESIGN_NAME` 一致，否则 `read_verilog -top` 会找不到顶层。

#### 4.1.4 代码实践（源码阅读型）

**目标**：确认本仓库「假设网表已存在」的事实，并找出交接变量。

**步骤**：

1. 打开 `IC Compiler II/Scripts/01_common_setup.tcl`。
2. 找到 `VERILOG_NETLIST_FILES` 这一行，记下它引用的变量链：`SYN_DIR` → `output/` → `${DESIGN_NAME}.v`。
3. 在仓库根目录用 `git ls-files` 查找：是否存在 `syn/` 目录或任何 `*.v` 的**网表**文件（注意区分网表与 RTL——`MY_DESIGN.v`、`cmsdk/*.v` 是 RTL，不是网表）。

**需要观察的现象**：你会发现仓库里**没有** `syn/output/pit_top.v` 这个文件——证实了「仓库只做 PnR，不做综合」。

**预期结果**：`git ls-files | grep -i syn` 只会命中 `synco_*.pdf`（编码风格指南）和 `synthesis` 这个词所在的 README 行，**没有**综合输出目录。

> 若无法在本地运行 `git ls-files`，可改用 GitHub 网页浏览仓库根目录确认：无 `syn/` 目录。**待本地验证。**

#### 4.1.5 小练习与答案

**练习 1**：综合的输入和输出分别是什么？
**答**：输入 = RTL + SDC + 目标库（Liberty）；输出 = 门级网表（`.v`）+ 综合后 SDC + 时序/面积/功耗报告。

**练习 2**：为什么「同一个 RTL，综合两次结果可能不同」？
**答**：因为综合依赖目标工艺库与 SDC，换库（积木变了）或换约束（时序目标变了），映射/选型/插 buffer 的结果就不同；即使库不变，综合器内部的启发式优化也可能随版本/种子有微小差异。

---

### 4.2 可综合 RTL 编码风格

#### 4.2.1 概念说明

**「可综合」** 意味着这段 RTL 能被综合器翻译成真实硬件（门 + 触发器）。Verilog 本身是「既能仿真也能综合」的语言，但有相当一部分构造**只能仿真、不能综合**（或不同工具行为不一致）。写出可靠综合的 RTL，需要遵守一套行业通用的编码风格（coding style）。

> 仓库里的权威参考是 [Guide to HDL Coding Styles for Synthesis/ReadMe](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/Guide%20to%20HDL%20Coding%20Styles%20for%20Synthesis/ReadMe) 所在目录下的 `synco_1.pdf … synco_5.pdf`（Synopsys HDL Compiler 编码风格指南，PDF，本讲无法逐页引用，请以原 PDF 为准）。下面讲的是**所有综合器（含 yosys、DC）通用**的几条铁律。

通用可综合铁律：

1. **时序逻辑用非阻塞 `<=`，组合逻辑用阻塞 `=`。** 不要混用。
2. **组合 `always` 块用完整敏感列表**，现代写法推荐 `always @(*)`。
3. **组合 `always` 块里每个输出在每个分支都要有赋值**（或给默认值），否则综合出 **latch**（锁存器）——几乎总是 bug。
4. **`case` 要覆盖全部分支或加 `default`**，原因同上。
5. **不要用 `initial` 给寄存器赋初值**（FPGA 可以，ASIC 综合通常忽略；ASIC 用复位）。
6. **不要用** `#delay`（延时被忽略）、`initial`（ASIC）、`fork/join`、`real`、事件、动态索引越界、无限循环等——它们要么被忽略，要么不可综合。

工程上还有一条贯穿全流程的纪律，README 把它写得很直白：

[README.md:209](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L209-L209) —— 「Simulate everything before synthesizing.」（综合前先把所有东西仿真清楚）。意思是：RTL 的功能正确性要在**仿真阶段**解决，别指望综合器帮你修逻辑 bug。

#### 4.2.2 核心流程

判断一段 RTL 是否「好综合」的检查流程：

```
逐块看 always：
  ├─ 敏感列表完整？ ──否──► 可能漏信号（仿真/综合不一致）
  ├─ 是时序（posedge clk）？ ──是──► 用 <= ?  否则风格错误
  ├─ 是组合？           ──是──► 用 = ?  否则风格错误
  └─ 组合块所有输出每分支都赋值？ ──否──► 综合出 latch（警告）
看 case：
  └─ 有 default 或全分支？ ──否──► 综合 latch 风险
看复位：
  └─ 同步/异步复位风格一致？
```

#### 4.2.3 源码精读：用 `MY_DESIGN.v` 做正反例

`MY_DESIGN.v` 是一份**能综合**的小设计，但它的写法并非都是「推荐风格」——正好拿来讲可综合编码。

**正例 1：时序逻辑用非阻塞 `<=`（推荐）**

[MY-Design/MY_DESIGN.v:13-19](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L13-L19) —— `always @(posedge clk)` 块里 `R1 <= arth_o;` 等，全部非阻塞。**这是标准写法**，综合成一排触发器（`R1..R4`），它们在时钟沿同时采样、互不依赖赋值顺序。

**反例 1：组合逻辑也用了非阻塞 `<=`（能综合但不推荐）**

[MY-Design/MY_DESIGN.v:21-26](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L21-L26) —— 这个 `always` 块敏感列表是电平（`out2, R1, R3, R4`），是**组合逻辑**，却用了 `<=`（非阻塞）。

```verilog
always @ (out2, R1, R3, R4)   // 电平敏感 → 组合逻辑
  begin
    out1 <= R1 + R3;          // 组合逻辑却用 <= ：能综合，但不推荐
    out2 <= R3 & R4;
    out3 <= out2 - R3;        // 注意：本行用了 out2 的「旧值」
  end
```

为什么「能综合但不推荐」：综合器对纯组合逻辑的 `<=` 通常仍能正确推断成组合门（它看的是数据依赖，不是赋值符号）；但这种写法在**仿真**里会因非阻塞的「延迟一拍更新」让人误读 `out3 <= out2 - R3` 到底用的是 `out2` 新值还是旧值，埋下仿真与预期不一致的坑。行业规范要求组合块一律用阻塞 `=`。同样的反例出现在 [ARITH 模块:40-47](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L40-L47) 和 [COMBO 模块:66-69](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L66-L69)。

**正例 2：`case` 覆盖全分支（推荐）**

[MY-Design/MY_DESIGN.v:43-46](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L43-L46) —— `case({sel})` 对 1 位信号列了 `1'b0` 和 `1'b1` 两个分支，**全覆盖**，所以不会综合出 latch，干净地变成一个 2 选 1 多路器 + 加减法器。

**正例 3：命名端口例化（推荐）**

[MY-Design/MY_DESIGN.v:9-10](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/MY-Design/MY_DESIGN.v#L9-L10) —— `ARITH U1_ARITH ( .a(data1), .b(data2), … )` 用 `.端口(信号)` 命名连接，顺序无关、可读、可综合，是层次化设计的标准写法（详见 [u2-l1](u2-l1-verilog-basic-design.md)）。

#### 4.2.4 代码实践（源码阅读型）

**目标**：用「可综合铁律」体检 `MY_DESIGN.v`。

**步骤**：

1. 打开 `MY-Design/MY_DESIGN.v`。
2. 对每个 `always` 块（行 13、21、40、66）填一张表：

   | 行号 | 时序/组合 | 用 `<=` 还是 `=` | 是否符合铁律 |
   |------|-----------|------------------|--------------|
   | 13   | 时序      | `<=`             | ✅ 符合       |
   | 21   | 组合      | `<=`             | ⚠️ 能综合但不推荐 |
   | …    | …         | …                | …             |

3. 检查组合块的输出是否「每个分支都赋值」（行 21-26 的 `out1/out2/out3` 都赋了，所以无 latch；但如果把某行删掉，就会出 latch）。

**需要观察的现象**：你会发现 `MY_DESIGN.v` 的**时序块**风格正确，但**所有组合块**都误用了 `<=`。

**预期结果**：完成上表，结论是「这份代码功能可综合，但组合逻辑的赋值风格不符合行业推荐，应把 `<=` 改成 `=`」。

#### 4.2.5 小练习与答案

**练习 1**：为什么组合 `always` 块里「某个分支忘了给输出赋值」很危险？
**答**：综合器为了保持上一个值，会推断出一个锁存器（latch），这通常不是你想要的，且会引入时序复杂度；仿真里也可能与综合行为不一致。解决方法是给默认值或补全分支/`default`。

**练习 2**：`always @(*)` 相比手写敏感列表有什么好处？
**答**：工具自动把块内所有读到的信号加进敏感列表，避免「漏写信号」导致仿真与综合不一致；它是现代可综合组合逻辑的推荐写法。

**练习 3**：ASIC 设计里为什么不该用 `initial` 给寄存器赋初值？
**答**：ASIC 没有「上电即随机但确定」的初值机制，`initial` 在 ASIC 综合中通常被忽略；正确做法是用复位（同步或异步）把寄存器恢复到已知值。（FPGA 因有专门的上电初值寄存器，可用 `initial`。）

---

### 4.3 yosys 综合流程概述（细节待确认）

> ⚠️ 本节为「概述」。`yosys_manual.pdf` 是压缩 PDF，本讲无法逐页引用其命令选项，**具体子命令、开关、版本差异请以手册原文为准（待本地确认）**。下面只讲命令名级别的、稳定公开的内容。

#### 4.3.1 概念说明

**yosys** 是一个开源的 RTL 综合框架（由 Claire Wolf 发起，现由 YosysHQ 维护），广泛用于开源 EDA 流程（如 OpenROAD、SymbiFlow）、FPGA 综合，也能做面向 ASIC 标准单元的综合。

它和商用综合器（Synopsys Design Compiler、Cadence Genus）的根本区别：

- **目标库来源**：商用 DC 用 Liberty `.db`（加密格式）；yosys 直接吃文本 `.lib`（Liberty）。
- **流程可脚本化**：yosys 用 Tcl 或自带命令脚本，把综合拆成可见的中间步骤（每个 pass 都能看到 RTLIL 中间表示），便于教学与调试。
- **生态**：yosys 是 `picorv32`、`VexRiscv` 等开源 RISC-V 内核的综合工具（README 里也列了 [picorv32](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L133-L133) 等仓库）。

> 商用 DC 综合的参考学习路径，README 已给出：[README.md:80-81](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/README.md#L80-L81) —— 「Advanced Logic Synthesis … Includes Synopsys DC/PT Labs」。

#### 4.3.2 核心流程

yosys 的最小综合三步（命令名级别，**选项待确认**）：

```
read_verilog  MY_DESIGN.v        # 1. 读 RTL → 解析成 RTLIL 中间表示
synth -top    MY_DESIGN          # 2. 跑通用综合流程（一个高层脚本）
write_verilog MY_DESIGN_netlist.v # 3. 输出门级网表
```

其中 `synth` 是一个**打包好的高层命令**，内部大致依次跑（**具体顺序与开关以手册为准，待确认**）：

| pass（子步骤） | 作用 | 直觉 |
|----------------|------|------|
| `hierarchy` | 展开/检查层次，确定 top | 把「谁是谁的子模块」理清 |
| `proc` | 把 `always`/`if`/`case` 翻成多路器与触发器 | 行为 → 数据通路 |
| `opt` | 逻辑优化（死代码消除、常量折叠等） | 化简 |
| `memory` / `memory_map` | 把数组/存储器映射成触发器或 RAM | 存储单元落地 |
| `techmap` | 把通用逻辑替换成工艺相关单元 | 走向具体积木 |
| `abc` | 技术映射 + 面积/时序优化（借 ABC 引擎） | 选具体门、优化 |

> 对 **ASIC 标准单元**映射，关键是用 `abc` 接一个 Liberty 文件，例如 `abc -liberty NangateOpenCellLibrary.lib`（**开关名待确认**），让 yosys 映射到你目标工艺的单元。映射后 `write_verilog` 出来的网表里，引用的就是该库的单元名（如 `AND2_X1`）。

`synth` 还有一族针对特定器件的变体，如 `synth_ice40`、`synth_xilinx`、`synth_intel`、`synth_ecp5` 等（**清单与支持器件以手册为准，待确认**）；做 ASIC 则多用通用 `synth` + `abc -liberty`。

#### 4.3.3 源码精读：手册是权威出处

本节没有可逐行引用的「源码」，权威出处就是仓库里的 PDF：

[yosys_manual.pdf](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/yosys_manual.pdf) —— yosys 官方手册。所有命令名（`read_verilog`、`synth`、`abc`、`techmap`、`write_verilog` 等）的**确切语义、开关、版本**请查此手册（待本地确认）。

> 重要：`read_verilog` / `write_verilog` 这两个名字在 **yosys** 和 **ICC2/PrimeTime** 里都存在，但**不是同一个命令**！
> - yosys 的 `read_verilog`：**综合的入口**，吃 RTL，吐 RTLIL。
> - ICC2 的 `read_verilog`（见 4.4.3）：**PnR 的入口**，吃**网表**（不是 RTL），建 block。
> - PrimeTime 的 `read_verilog`：**STA 的入口**，吃网表，建 timing graph。
> 三者同名但角色完全不同，初学者最容易混淆。

#### 4.3.4 代码实践（源码阅读型）

**目标**：从手册目录里确认「read → synth → write_verilog」三步，并理解每步的角色。

**步骤**：

1. 打开 `yosys_manual.pdf`，定位到目录（Table of Contents）和 `synth` 命令所在章节（待本地确认页码）。
2. 在手册里找到这几个命令的条目，各记一句话作用：
   - `read_verilog`
   - `synth`（注意它内部会调用哪些 pass）
   - `write_verilog`
3. 思考：如果要 yosys 输出**能被本仓库 ICC2 PnR 消费**的网表，`abc` 需要接哪个 Liberty 文件？（提示：和 [u3-l1](u3-l1-standard-cell-libraries.md) 里 Nangate45 的库一致。）

**需要观察的现象**：手册里 `synth` 是一个「脚本命令」，它本身不直接做映射，而是串联多个 pass；真正的「选具体单元」发生在 `abc`/`techmap`。

**预期结果**：列出 `read_verilog → synth -top → write_verilog` 三步，并写出「要让 ICC2 能 link，yosys 的 `abc` 必须映射到与 NDM 参考库同名的单元」这一结论。具体命令选项**待本地验证**。

> 如果本地未安装 yosys，可在 [EDA Playground](https://www.edaplayground.com/)（README 工具清单里有）选 yosys 在线试跑一段最小 RTL，观察综合日志里的 pass 顺序。

#### 4.3.5 小练习与答案

**练习 1**：yosys 的 `synth` 命令和 `abc` 命令分别负责什么？
**答**：`synth` 是一个高层脚本命令，串联 hierarchy/proc/opt/techmap 等通用 pass；`abc` 负责技术映射与逻辑优化，把逻辑门映射到具体工艺单元（接 Liberty 文件时映射到标准单元）。**具体开关待确认。**

**练习 2**：为什么说「yosys 的 `read_verilog` 和 ICC2 的 `read_verilog` 同名但不同」？
**答**：分属不同工具、不同阶段、吃不同输入——yosys 的吃 RTL（综合入口），ICC2 的吃网表（PnR 入口）。同名只是历史巧合。

---

### 4.4 综合与时序库的关系

#### 4.4.1 概念说明

综合不是「随便翻成门」——它要**按时序约束做时序驱动（timing-driven）的综合**：在满足 setup 的前提下，尽量省面积、省功耗。这就要求综合器**随时能查到每个候选单元的延迟**，而延迟数据就在**时序库（Liberty）**里。

所以综合与时序库的关系是：

- **库 = 积木目录**：综合器只能「选」库里有的单元，选不到就用不了。
- **库 = 延迟/功耗/面积表**：每选一个单元、每插一个 buffer，综合器都要查库算延迟，判断是否违例。
- **网表里的单元名必须和后续 PnR/STA 的库一致**：综合选的 `DFFR_X4`，到 PnR 的 NDM、到 PrimeTime 的 `.db` 里必须是同一个名字——否则 `link` 失败。

商用 DC 的典型设置（概念，本仓库无脚本）：

- `target_library`：综合**用来映射**的单元集（标准单元 `.db`）。
- `link_library`：综合后**链接**用的全集（含 target + 宏单元如 SRAM）。

yosys 对应的概念是 `abc -liberty file.lib`（**开关待确认**）——把目标库喂给映射引擎。

#### 4.4.2 核心流程

时序驱动综合的循环：

```
读 Liberty（拿延迟表）
   │
   ▼
选一个单元/插一个 buffer ──► 查库算这条路径的延迟
   │
   ▼
算 slack = 要求时间 − 到达时间
   │
   ├─ slack ≥ 0：满足 setup，可接受
   └─ slack < 0：违例 → 换更大驱动单元 / 再插 buffer / 重映射 → 再查库
```

动态功耗与电压平方成正比，这正是综合在「选单元大小」时权衡 PPA 的依据：

\[
P_{\text{dyn}} \;\propto\; \alpha \cdot C \cdot V^{2} \cdot f
\]

其中 \(C\) 是节点电容（驱动越大的单元 \(C\) 越大、越耗电但越快）。综合器选「够快但尽量小」的单元，就是在 \(P_{\text{dyn}}\)（功耗）、延迟（性能）、单元面积三者间找平衡。

#### 4.4.3 源码精读：PnR 如何「链接」综合选出的单元

综合选出的单元名，到 PnR 阶段必须能被解析到 NDM 参考库里的实体。看 `03_PnR_setup.tcl` 的 setup 三连：

[IC Compiler II/Scripts/03_PnR_setup.tcl:27-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L27-L31) —— 逐行作用：

- `create_lib -technology $TECH_FILE -ref_libs $NDM_REFERENCE_LIB_DIRS ${DESIGN_NAME}.dlib`：建一个本设计库，并挂上**参考库**（NDM，即标准单元的物理+时序统一视图，详见 [u3-l2](u3-l2-ndm-library-creation.md)）。
- `read_verilog -top ${DESIGN_NAME} $VERILOG_NETLIST_FILES`：**读综合产出的网表**（注意：这里吃的是网表 `$VERILOG_NETLIST_FILES`，不是 RTL）。
- `link_block`：把网表里出现的每个单元名（如 `AND2_X1`）**解析到参考库里的实体**——这一步能成功的前提，就是综合阶段选用的单元名和 NDM 参考库里的名字**完全一致**。

这就是「综合 ↔ 时序库 ↔ PnR」三者咬合的齿轮：综合按 Liberty 选单元 → 网表记下单元名 → PnR 用同名 NDM 把它实体化 → PrimeTime 用同名 `.db` 算延迟。**名字对不上，`link_block` 就报 unresolved reference。**

> 回到 4.3 的结论：如果你用 yosys 替代 DC 做综合，那么 yosys `abc` 所映射的那个 Liberty 库的单元名，必须和本仓库 `NDM_REFERENCE_LIB_DIRS`（Nangate45）里的单元名一致——否则接不上 PnR。

#### 4.4.4 代码实践（源码阅读型）

**目标**：把「综合选单元 → PnR link 单元」这条链在仓库里走一遍。

**步骤**：

1. 在 [01_common_setup.tcl:14-15](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L14-L15) 找到 `NDM_REFERENCE_LIB_DIRS`，记下两个参考库：`...ss0p95v125c.ndm`（slow 角）和 `...ff1p25v0c.ndm`（fast 角）——这正是综合时 slow/fast 两个 PVT 角的对应物（详见 [u3-l1](u3-l1-standard-cell-libraries.md)）。
2. 在 [03_PnR_setup.tcl:29](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L29-L29) 确认 `read_verilog` 读的是 `$VERILOG_NETLIST_FILES`（网表），不是 `.v` 的 RTL。
3. 在 [03_PnR_setup.tcl:31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L31-L31) 找到 `link_block`，理解它就是把网表单元名「对账」到参考库。

**需要观察的现象**：综合（无论 DC 还是 yosys）必须用与 `NDM_REFERENCE_LIB_DIRS` 同名的单元库，否则 `link_block` 失败。

**预期结果**：写一句话总结——「综合是时序库驱动的；它的产物网表里的单元名，是 PnR `link_block` 能否成功的唯一硬约束」。

#### 4.4.5 小练习与答案

**练习 1**：如果综合用了库 A 的单元，但 PnR 的参考库是库 B，会发生什么？
**答**：`link_block` 会报 unresolved reference（网表里的单元名在参考库里找不到实体），PnR 无法继续。解决：综合与 PnR/STA 必须用「同名」的库（同一套标准单元的时序面/物理面）。

**练习 2**：为什么综合要读 slow（ss）和 fast（ff）两个角的库？
**答**：因为芯片要在最慢（高温低压）和最快（低温高压）两种极端下都能工作。slow 角盯 setup（最坏延迟），fast 角盯 hold（最快翻转）。这就是 MCMM/多角综合的由来（详见 [u4-l1](u4-l1-icc2-setup-mcmm.md)）。

**练习 3**：动态功耗公式 \(P_{\text{dyn}} \propto \alpha C V^{2} f\) 对综合选单元有什么指导意义？
**答**：驱动越大的单元电容 \(C\) 越大、越耗电但越快。综合器在「时序够用」的路径上应选**更小**的单元以省功耗，只在关键路径上用大驱动单元——这就是 gate sizing 的权衡。

---

## 5. 综合实践

**综合任务**：把本讲 4 个模块串起来，画出「RTL → 综合 → 网表 → PnR read_verilog → link_block」的完整交接链，并补出综合那一步的开源替代（yosys）。

**步骤**：

1. **列综合基本步骤**：参考 `yosys_manual.pdf` 目录（待本地确认页码），写下综合三步——`read_verilog`（读 RTL）→ `synth -top <模块名>`（综合）→ `write_verilog`（出网表）。

2. **画出交接链**（填空）：

   ```
   MY_DESIGN.v (RTL)
        │  ◄── yosys read_verilog / DC analyze
        ▼
   [ 综合：read SDC + read Liberty → synth/compile ]
        │  ◄── abc -liberty Nangate45.lib（yosys，开关待确认）
        ▼
   pit_top.v / MY_DESIGN.v (门级网表)   ← 写到 SYN_DIR/output/
        │  ◄── 对应 01_common_setup.tcl 的 VERILOG_NETLIST_FILES
        ▼
   03_PnR_setup.tcl: read_verilog -top  (读网表, 非 RTL)
        │
        ▼
   03_PnR_setup.tcl: link_block  (网表单元名 → NDM 参考库实体)
        │
        ▼
   进入 floorplan → ... → GDSII（详见 U4）
   ```

3. **回答关键问题**（用仓库源码佐证）：
   - 综合输出如何喂给 PnR？→ 通过 [01_common_setup.tcl:35](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L35-L35) 的 `VERILOG_NETLIST_FILES` 变量，被 [03_PnR_setup.tcl:29](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L29-L29) 的 `read_verilog` 读入。
   - 为什么综合和 PnR 必须用同名单元库？→ 因为 [03_PnR_setup.tcl:31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L31-L31) 的 `link_block` 要把网表单元名解析到 NDM 参考库实体。
   - 仓库里有没有可运行的综合脚本？→ **没有**。仓库只做 PnR/STA，假设网表已在外部 `SYN_DIR` 生成（待本地用 `git ls-files` 验证）。

**预期结果**：一张完整交接图 + 三段回答。结论应包含：「本仓库不包含综合脚本；yosys 是可自补的开源综合选项，但其命令开关须以 `yosys_manual.pdf` 为准（待确认）」。

---

## 6. 本讲小结

- **综合** = 在目标库 + SDC 约束下，把 RTL 翻译成门级网表；输入是 RTL/SDC/Liberty，输出是网表 + 报告。
- 本仓库**不含**综合脚本，只在 [01_common_setup.tcl:34-36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L34-L36) 用 `VERILOG_NETLIST_FILES` 指向「外部已综合好的网表」——这是综合与 PnR 的交接点。
- 可综合 RTL 的铁律：时序用 `<=`、组合用 `=`、敏感列表完整、组合块/case 全分支赋值、不用 `initial`/`#delay`。`MY_DESIGN.v` 时序块合规，但组合块误用 `<=`（能综合但不推荐）。
- **yosys** 是开源综合器，最小流程 `read_verilog → synth -top → write_verilog`；面向 ASIC 标准单元需用 `abc -liberty`；**具体开关以 `yosys_manual.pdf` 为准（待确认）**。
- 综合是**时序库驱动**的：选/插单元都要查 Liberty 算延迟；动态功耗 \(P_{\text{dyn}} \propto \alpha C V^{2} f\) 指导 gate sizing 的 PPA 权衡。
- 综合选的单元名必须与 PnR 的 NDM、PrimeTime 的 `.db` 同名，否则 [03_PnR_setup.tcl:31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L31-L31) 的 `link_block` 报 unresolved reference。

---

## 7. 下一步学习建议

- **下一讲 [u10-l2 全流程实战：RTL 到 GDSII 综合演练](u10-l2-rtl-to-gdsii-capstone.md)**：把本讲的综合与 U4 的 ICC2 PnR 全流程、U6 的 PrimeTime STA 串成一次端到端 capstone，建议学完本讲后直接进入。
- **回头加深库的理解**：综合与时序库的关系详见 [u3-l1 标准单元库与物理数据基础](u3-l1-standard-cell-libraries.md) 与 [u3-l2 创建 NDM 参考库](u3-l2-ndm-library-creation.md)；理解了 Liberty/NDM，才真正理解综合「选单元」的依据。
- **动手试 yosys**：在 [EDA Playground](https://www.edaplayground.com/) 选 yosys，把 `MY_DESIGN.v` 综合成网表，对照 `yosys_manual.pdf` 看日志里 `proc/opt/techmap/abc` 的 pass 顺序（命令开关**待本地确认**）。
- **读编码风格原 PDF**：`Guide to HDL Coding Styles for Synthesis/synco_1.pdf … synco_5.pdf` 是 Synopsys 官方可综合编码指南，比本讲 4.2 的通则更细，建议挑 latch、复位、FSM 三章精读。
