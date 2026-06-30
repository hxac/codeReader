# PrimeTime STA 基本流程

## 1. 本讲目标

学完本讲，你应当能够：

- 说出静态时序分析（STA）在「RTL→GDSII」流程里的**签核（sign-off）**地位，并解释它为什么必须吃 PnR 之后的网表和寄生。
- 看懂 `PrimeTime/` 目录下四个脚本的分工：`common_setup.tcl` 写公共变量、`pt_setup.tcl` 写运行时变量、`pt.tcl` 是完整流程、`RUN.tcl` 是最小化范例。
- 追踪 `pt.tcl` 的执行顺序，准确指出**链接库（link_path）、网表、寄生、约束**这四类输入分别在哪里设置、哪里被读取。
- 解释 `read_verilog`/`link_design`、`read_parasitics`/`report_annotated_parasitics`、`read_sdc`/`source`、`set_propagated_clock`、`update_timing`、`report_timing`、`save_session` 这一串命令各自在做什么。
- 把本讲的 STA 流程，与 u2-l3 的 SDC 约束、u4-l7 的 PnR 交付物（网表 + SPEF）串成一条完整的签核链路。

本讲是 **U6 静态时序分析** 单元的第一讲，承接 u2-l3（SDC 时序约束）与 u4-l7（ICC2 收尾与输出）。它会告诉你：u4-l7 里 `write_verilog` 吐出的那份非 PG 网表、`write_parasitics` 吐出的那份 SPEF，到底是被谁、怎么吃进去做签核的。

## 2. 前置知识

在进入源码之前，先用通俗语言把三个概念讲清楚。

### 2.1 什么是静态时序分析（STA）

芯片做完 PnR 之后，所有逻辑门已经摆好、金属线已经连好。但「连对了」不等于「跑得动」——信号能不能在一个时钟周期内从发射寄存器（launching register）稳定传到捕获寄存器（capturing register），需要验证。验证方法有两种：

- **动态仿真**：给一组输入波形，让电路跑起来看输出。问题是它只能覆盖你「喂过」的那些输入组合，覆盖不全。
- **静态时序分析（STA, Static Timing Analysis）**：**不跑任何输入激励**，而是把电路抽象成一张「时序图（timing graph）」，穷举所有可能的寄存器到寄存器路径，逐条检查是否满足时序要求。「静态」二字就是指「不需要输入激励」。

PrimeTime 就是 Synopsys 的工业级 STA 签核工具。所谓**签核（sign-off）**，是指 PnR 工具（ICC2）内部给出的时序结论仅供参考，最终交付代工厂前，必须用独立的 PrimeTime 再算一遍，以 PrimeTime 的结论为准。

### 2.2 setup / hold 与 slack

每条时序路径要同时满足两类检查：

- **建立时间检查（setup）**：数据必须在捕获时钟沿到来**之前**的某段时间就稳定。检查的是「数据到得太晚」。
- **保持时间检查（hold）**：数据必须在捕获时钟沿之后还稳定一段时间。检查的是「数据变得太快、把旧数据冲掉」。

每条路径算出一个 **slack（裕量）**：

\[
\text{slack} = T_{\text{required}} - T_{\text{arrival}}
\]

- \(\text{slack} \geq 0\)：时序满足（met）。
- \(\text{slack} < 0\)：时序违例（violation），负得越多越危险。

setup 用最大延迟路径（max delay）算，hold 用最小延迟路径（min delay）算——这正是 u2-l3 讲过的「slow 角盯 setup、fast 角盯 hold」在 STA 里的体现。

### 2.3 为什么 STA 必须读寄生（SPEF）

信号沿金属线传播时，线本身有电阻 R 和电容 C，它们会产生额外的延迟（叫「互连延迟」）。在没有真实版图之前，PnR 工具只能**估算**这段延迟；布线完成之后，才能从真实版图里提取出精确的 R/C，写成 **SPEF**（Standard Parasitic Exchange Format）文件。

u4-l7 里 ICC2 的 `write_parasitics` 命令产出的就是这份 SPEF。PrimeTime 读入它、把每根线的精确寄生「贴」回网表的过程叫**反标（back-annotation）**。只有反标了真实寄生的 STA 结论，才能作为签核依据。这就是为什么本讲四类输入里，「寄生」是必不可少的一类。

> 术语速查：STA、sign-off（签核）、slack、setup/hold、launching/capturing register、SPEF、反标（back-annotation）、timing graph。

## 3. 本讲源码地图

本讲涉及四个文件，全部位于 `PrimeTime/` 目录：

| 文件 | 行数 | 作用 |
|------|------|------|
| [PrimeTime/common_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/common_setup.tcl) | 87 | **公共变量**：设计名、库文件、技术文件、TLU+、电源/地等。可在 DC/PrimeTime/ICC2 间共享。 |
| [PrimeTime/pt_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt_setup.tcl) | 101 | **PrimeTime 运行时变量**：网表文件、寄生文件、约束文件、search_path、link_path、报告目录。 |
| [PrimeTime/pt.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl) | 116 | **完整的 STA 主流程脚本**：变量驱动，含读网表、反标、读约束、传播时钟、报告时序、保存会话。 |
| [PrimeTime/RUN.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/RUN.tcl) | 38 | **最小化范例脚本**：把 pt.tcl 的骨架写成硬编码的十几行，适合快速理解流程。 |

一个重要背景：脚本里的设计名是 **ORCA**，引用的技术文件是 `cb13_6m.tf`（TSMC 0.13µm 6 层金属，Artisan 库）。ORCA 是 Synopsys Reference Methodology（RM，参考方法论）套件里经典的参考设计，这套脚本即改编自 Synopsys PrimeTime 的 RM 模板。脚本里大量出现的 `../ref/...` 路径指向参考数据，**这些数据本身不在本仓库内**——它们来自完整的 Synopsys RM 套件。本仓库提供的是「脚本骨架」，输入数据需自行准备（这一点很重要，否则你会在脚本里看到一堆找不到的文件）。

## 4. 核心概念与源码讲解

### 4.1 变量与库设置：common_setup 与 pt_setup

#### 4.1.1 概念说明

Synopsys 的工具链有个一贯的好习惯：**把「换项目就要改的东西」（变量）和「流程怎么跑」（命令）分开写**。PrimeTime 这里分成两层：

- `common_setup.tcl`：跨工具共享的公共变量（设计名、库名、技术文件、TLU+）。u3-l1 讲 ICC2 时你已经见过同名的 `01_common_setup.tcl`，思路完全一致。
- `pt_setup.tcl`：只跟 PrimeTime 运行相关的变量（网表、寄生、约束、报告目录、`search_path`/`link_path` 的拼装）。

这两个文件只 `set` 变量、不执行任何分析命令。真正的「读数据、算时序」由 `pt.tcl` 来做。这种分离让你换设计时只改这两个 setup 文件，`pt.tcl` 一字不动。

#### 4.1.2 库的两条线索：search_path 与 link_path

理解 PrimeTime 的库设置，关键是区分两条「线索」：

- **`search_path`（搜索路径）**：告诉 PrimeTime「去哪些目录下找文件」。它管的是**文件在哪里**。
- **`link_path`（链接库列表）**：告诉 PrimeTime「设计里的每个单元，到哪些 `.db` 库里去找它的实体」。它管的是**逻辑从哪里解析**。

这两条线索在两个 setup 文件里被拼出来，再由 `pt.tcl` 启用。

#### 4.1.3 源码精读

**common_setup.tcl 的核心变量**：

设计名与参考数据根路径——所有库/数据路径都以 `DESIGN_REF_DATA_PATH`（`../ref`）为前缀：

[PrimeTime/common_setup.tcl:8-13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/common_setup.tcl#L8-L13) 定义 `DESIGN_NAME "ORCA"` 和 `DESIGN_REF_DATA_PATH "../ref"`，这是后面所有库路径的前缀。

技术库与时序库（Liberty `.db`）——目标库 `sc_max.db` 是核心标准单元的时序库，额外的链接库包含 IO、special、memory：

[PrimeTime/common_setup.tcl:24-29](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/common_setup.tcl#L24-L29) 定义 `TARGET_LIBRARY_FILES "sc_max.db"`（目标工艺逻辑库）和 `ADDITIONAL_LINK_LIB_FILES`（额外的 IO/special/mem 库），用于解析设计里对宏单元和 IO 的引用。

> 复习 u3-l1：`.db` 是 Liberty 时序库（NLDM 延迟表），描述每个单元的延迟与功耗。PrimeTime 做纯逻辑/时序分析，**只吃 `.db`，不吃 LEF/NDM**（那是物理库，归 PnR 工具）。这是 STA 与 PnR 在库需求上的根本区别。

技术文件、TLU+、层映射——这些变量在 PrimeTime 里大多**不会被直接使用**（PrimeTime 不需要 `.tf` 来算时序，它直接读 SPEF 里已经提取好的寄生），但 `common_setup.tcl` 为了「跨工具共享」依然保留它们，供 ICC2/DC 等其它工具读取：

[PrimeTime/common_setup.tcl:39-46](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/common_setup.tcl#L39-L46) 定义 `TECH_FILE`、`MAP_FILE`、`TLUPLUS_MAX/MIN_FILE`。注意它们在 PrimeTime 流程里基本是「空载」变量，体现了 setup 文件的跨工具复用意图。

**pt_setup.tcl 把变量拼成 search_path 与 link_path**：

[PrimeTime/pt_setup.tcl:35-37](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt_setup.tcl#L35-L37) 这三行是关键：把 `ADDITIONAL_SEARCH_PATH`（来自 common_setup，含 `../ref` 下若干子目录）和当前目录 `.` 拼进 `search_path`；把 `target_library` 设为 `sc_max.db`；再用一行把 `link_path` 拼成 `"* $target_library $ADDITIONAL_LINK_LIB_FILES"`。

`link_path` 开头的那个 `*` 是 Synopsys 的通配约定，意思是「连同后面用 `read_lib` 读进内存的库一起搜索」。展开后 `link_path` 大致是：

```
* sc_max.db io_max.db special.db mem_max.db
```

这串就是「设计里的单元去哪几个库里找实体」的答案。

#### 4.1.4 代码实践

**实践目标**：搞清楚 `link_path` 里到底有哪些库，以及它们的来源。

**操作步骤**：

1. 打开 `PrimeTime/common_setup.tcl`，找到 `TARGET_LIBRARY_FILES` 和 `ADDITIONAL_LINK_LIB_FILES` 两个变量，记下它们的值。
2. 打开 `PrimeTime/pt_setup.tcl` 第 36–37 行，看 `target_library` 和 `link_path` 是怎么由这两个变量拼出来的。
3. 在纸上把 `link_path` 完整展开（注意把 `\` 续行的多行字符串合并）。

**需要观察的现象**：`link_path` 里除了开头的 `*`，依次出现一个目标库和三个额外库（io / special / mem）。

**预期结果**：展开后得到 `* sc_max.db io_max.db special.db mem_max.db`。其中 `sc_max.db` 是标准单元主库，其余三个分别覆盖 IO pad、特殊单元、存储器宏——正是它们各自对应了 `common_setup.tcl` 里 `ADDITIONAL_LINK_LIB_FILES` 的三个文件名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PrimeTime 的 `common_setup.tcl` 里保留了 `TECH_FILE`、`TLUPLUS_MAX_FILE` 这些变量，但本讲的 STA 流程并不真的去读 `.tf` 和 `.tluplus`？

**答案**：因为 PrimeTime 直接读 PnR 已经提取好的 SPEF 寄生，不需要自己用 TLU+ 表去算寄生；`.tf` 是物理工艺描述，也归 PnR 工具。这些变量保留是为了让同一份 `common_setup.tcl` 能在 DC、ICC2、PrimeTime 多个工具间共享——这是 Synopsys RM「一份 setup，多工具复用」的设计意图。

**练习 2**：`link_path` 开头的 `*` 是什么意思？如果去掉它会有什么风险？

**答案**：`*` 表示「搜索当前已读入内存的库」。去掉后，PrimeTime 只会在 `link_path` 显式列出的 `.db` 文件里解析单元引用；如果设计里还有通过 `read_lib` 单独加载的库，那些单元就会变成 unresolved reference，`link_design` 报告里会出现未解析的实例。

---

### 4.2 读网表与链接设计：read_verilog / link_design

#### 4.2.1 概念说明

STA 的分析对象是**门级网表**，不是 RTL。这份网表来自综合（DC，见 u10-l1）或 PnR 后的 `write_verilog`（见 u4-l7）。PrimeTime 读网表分两步：

1. **`read_verilog`**：把 Verilog 网表文件读进内存，创建出设计的逻辑结构（单元实例 + 连线），但此时每个单元还只是个「名字」。
2. **`link_design`**：把每个单元实例的「名字」解析到 `link_path` 指定的 `.db` 库里的真实实体上，建立完整的时序图。只有 `link_design` 成功，单元才有真实的延迟/转换时间，STA 才能开始算。

`link_design` 失败的最常见原因就是某个单元在所有库里都找不到（unresolved reference）——所以 u4-l7 强调「写网表前要先 `change_names -rules verilog`」，正是为了避免名字格式不匹配导致 link 失败。

#### 4.2.2 核心流程

```
read_verilog  $NETLIST_FILES   ;# 读网表 → 得到实例化结构
current_design $DESIGN_NAME    ;# 指定顶层
link_design   -verbose         ;# 把单元解析到 .db，建立 timing graph
```

`current_design` 这一步先「选中」顶层模块，`link_design` 才知道要 link 哪个设计。

#### 4.2.3 源码精读

**pt.tcl 的「Netlist Reading Section」**：

[PrimeTime/pt.tcl:43-48](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L43-L48) 先启用拼好的 `link_path`，再 `read_verilog $NETLIST_FILES`、`current_design $DESIGN_NAME`、`link_design -verbose`。`-verbose` 让 link 报告详细输出每个未解析引用，便于排错。

而 `$NETLIST_FILES` 的值在 `pt_setup.tcl` 里：

[PrimeTime/pt_setup.tcl:41](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt_setup.tcl#L41) `set NETLIST_FILES "orca_routed.v.gz"`。注意后缀是 `.v.gz`——这是 **gzip 压缩的 Verilog 网表**，PrimeTime 能直接读压缩文件。文件名 `orca_routed` 里的 `routed` 暗示它是**布线后**的网表（即 u4-l7 PnR 吐出的那份），这正是签核该用的版本。

`$DESIGN_NAME` 来自 `common_setup.tcl` 第 8 行的 `"ORCA"`。`pt_setup.tcl` 第 44–47 行还有一段保护：如果 `DESIGN_NAME` 为空就置空，确保用 common_setup 里定义的值。

#### 4.2.4 代码实践

**实践目标**：追踪网表这一类输入，看清它「在哪里设置、在哪里被读」。

**操作步骤**：

1. 在 `common_setup.tcl` 找到 `DESIGN_NAME`（第 8 行）。
2. 在 `pt_setup.tcl` 找到 `NETLIST_FILES`（第 41 行）。
3. 在 `pt.tcl` 找到 `read_verilog` / `current_design` / `link_design`（第 45–48 行）。

**需要观察的现象**：三个文件各贡献一段，串成「顶层名 → 网表文件名 → 读入并链接」的链路。

**预期结果**：`DESIGN_NAME="ORCA"` 在 common_setup 定义；`NETLIST_FILES="orca_routed.v.gz"` 在 pt_setup 定义；pt.tcl 用 `read_verilog` 读它、`current_design ORCA` 选顶层、`link_design` 解析单元。三类输入里，**网表输入的「定义点」在 pt_setup 第 41 行，「消费点」在 pt.tcl 第 45 行**。

#### 4.2.5 小练习与答案

**练习 1**：为什么必须在 `read_verilog` 之后、报告时序之前，调用一次 `link_design`？跳过它会发生什么？

**答案**：`read_verilog` 只建立了实例化结构，单元还没有真实时序属性。`link_design` 负责把单元名解析到 `.db` 库实体，赋予延迟/转换时间，从而建成可分析的 timing graph。跳过它，PrimeTime 既不知道每个单元的延迟，也无法识别时钟引脚，后续 `update_timing`/`report_timing` 算不出任何有意义的 slack。

**练习 2**：`orca_routed.v.gz` 为什么用布线后的网表，而不是综合后（布线前）的网表做签核？

**答案**：签核 STA 的目的是反映真实流片后的时序。布线后的网表包含了真实的时钟树、buffer 插入、网名，配合 SPEF 里的真实互连寄生，才能算出可信的 slack。综合后网表里时钟树还是理想的、互连也是估算的，结论只能用于综合阶段评估，不能作为签核依据。

---

### 4.3 反标寄生：read_parasitics / report_annotated_parasitics

#### 4.3.1 概念说明

2.3 节已经讲了反标的必要性。在 PrimeTime 里，反标由两条命令配合完成：

- **`read_parasitics`**：读入 SPEF（或 SBPF/Timing Budget Format 等寄生格式），把每根线的精确 R/C「贴」到网表对应的 net 上。
- **`report_annotated_parasitics -check`**：生成一份报告，统计**有多少比例的 net 成功贴上了真实寄生、有多少仍用估算值**。这个比例是签核可信度的关键指标——如果只有一半的 net 反标成功，那 slack 再好看也不能信。

`PARASITIC_PATHS` 与 `PARASITIC_FILES` 的对应关系：每条 `PARASITIC_PATHS`（实例名/顶层名）对应一份 `PARASITIC_FILE`，用于层次化设计里给不同 block 各贴一份寄生。本仓库是单顶层（ORCA），所以两者都是单值。

#### 4.3.2 核心流程

```
read_parasitics  $PARASITIC_FILES
report_annotated_parasitics -check > $REPORTS_DIR/rap.report
```

`-check` 让报告以「检查模式」输出：哪些 net 反标成功（fully annotated）、哪些部分成功、哪些完全没贴上。

#### 4.3.3 源码精读

**pt_setup.tcl 定义寄生变量**：

[PrimeTime/pt_setup.tcl:67-72](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt_setup.tcl#L67-L72) 设置 `PARASITIC_PATHS "ORCA"` 和 `PARASITIC_FILES "ORCA.SPEF.gz"`。注释里解释了多 block 层次化设计的用法，以及单顶层时直接用顶层名。

**pt.tcl 反标并自检**：

[PrimeTime/pt.tcl:54-56](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L54-L56) 在「Back Annotation Section」里 `read_parasitics $PARASITIC_FILES`，紧接着 `report_annotated_parasitics -check` 把反标覆盖率写到 `reports/rap.report`。

注意执行顺序——**反标必须在读约束、算时序之前**。否则你算出的 slack 用的是估算寄生，毫无签核意义。

> 与 u4-l7 的衔接：u4-l7 里 ICC2 的 `write_parasitics` 命令产出的就是这份 `ORCA.SPEF.gz`。所以「寄生」这一类输入，源头是 PnR，终点是 PrimeTime 反标。

#### 4.3.4 代码实践

**实践目标**：理解反标覆盖率报告的含义。

**操作步骤**：

1. 在 `pt.tcl` 找到「Back Annotation Section」（第 50–56 行）。
2. 注意 `report_annotated_parasitics -check` 把结果重定向到 `$REPORTS_DIR/rap.report`，其中 `REPORTS_DIR` 在 `pt_setup.tcl` 第 24 行定义为 `"reports"`，并由第 25 行 `file mkdir` 提前建好。
3. 设想你打开 `reports/rap.report`，会看到类似 `fully annotated: 95%` / `not annotated: 5%` 的统计。

**需要观察的现象**：报告会按「fully / partially / not annotated」三档分类统计 net 数量与百分比。

**预期结果**：一份合格的签核 SPEF 应让 `fully annotated` 接近 100%。若 `not annotated` 比例偏高，说明 SPEF 与网表不匹配（实例名/网名对不上），此时 STA 结论不可信。**待本地验证**：真实百分比需在有完整 `../ref` 数据的环境里跑过 PrimeTime 才能看到。

#### 4.3.5 小练习与答案

**练习 1**：如果 `read_parasitics` 读入的 SPEF 里实例名与网表对不上，`report_annotated_parasitics -check` 会反映出什么？

**答案**：对不上的那些 net 会被归到 `not annotated`，比例升高。这说明反标失败，对应的互连延迟退回到 PrimeTime 的估算值（基于线长/扇出的 wireload 模型），slack 不可信。应检查 SPEF 与网表是否同源于一次 PnR、实例名是否经过 `change_names` 规范化。

**练习 2**：为什么 `REPORTS_DIR` 要在用之前先 `file mkdir`？

**答案**：因为后续多条 `report_*` 命令用 `> $REPORTS_DIR/xxx.report` 重定向写文件。若目录不存在，重定向会报错导致脚本中断。第 25 行的 `file mkdir $REPORTS_DIR` 提前确保目录存在，是稳健写法。

---

### 4.4 读约束、传播时钟与报告时序

#### 4.4.1 概念说明

库、网表、寄生都就绪后，还差最后一块拼图：**告诉 PrimeTime「该检查什么」**。这就是约束（约束来自 u2-l3 讲的 SDC）。约束文件有两类，PrimeTime 用不同命令读：

- **`.sdc` 文件**：标准 SDC，用 `read_sdc` 读。
- **Tcl 形式的约束脚本**（如本仓库的 `orca_pt_constraints.tcl`）：用 `source` 执行，里面可以直接写 PrimeTime 特有的 Tcl 命令。

读约束后，还有两个常被初学者忽略但至关重要的步骤：

- **`set_propagated_clock [all_clocks]`**：把所有时钟标记为「**传播时钟（propagated clock）**」。SDC 里 `create_clock` 定义的时钟，默认用 ideal latency（理想的时钟插入延迟）；而 STA 要算真实的时钟树延迟（时钟经过 buffer/反相器逐级到达各寄存器的真实路径），必须标记为 propagated。**注意：这里不是做 CTS**——CTS 早就在 u4-l5 的 PnR 里做完了，网表里已经有完整的时钟树；PrimeTime 只是「承认」这棵树、沿着它真实传播算延迟而已。（`pt.tcl` 第 73 行把这个 section 注释成「Clock Tree Synthesis Section」是 RM 模板沿用的历史命名，容易误导，实际它只做传播标记。）

- **`update_timing -full`**：约束和寄生都改完后，重新计算整张 timing graph。任何修改（读新约束、反标、传播时钟）之后都要 `update_timing`，结论才更新。

报告阶段有三条核心命令：

- **`report_timing`**：报告每条路径的 slack，是 STA 的「主输出」。
- **`report_clock -skew`**：报告各时钟的偏斜（skew）。
- **`report_analysis_coverage`**：报告有多少时序弧（timing arc）被真正检查了、多少被 case analysis/常量屏蔽了——这是衡量「约束覆盖度」的指标。

还有一条**约束自检**命令 `check_timing`：它在算完时序后，检查是否有「该约束却没约束」的端口（如无时钟的寄存器、无 input/output delay 的端口），把这些潜在疏漏列出来。

#### 4.4.2 核心流程

```
# 1. 读约束（按后缀分流）
foreach f $CONSTRAINT_FILES {
    if {[file extension $f] eq ".sdc"} { read_sdc -echo $f }
    else                                { source -echo $f }
}

# 2. 传播时钟（让 STA 沿真实时钟树算延迟）
set_propagated_clock [all_clocks]

# 3. 重算时序 + 约束自检
update_timing -full
check_timing -verbose > $REPORTS_DIR/ct.report

# 4. 报告
report_timing   -slack_lesser_than 0.0 -delay min_max ...  ;# 主报告
report_clock    -skew ...
report_analysis_coverage ...
```

#### 4.4.3 源码精读

**约束文件的来源**：

[PrimeTime/pt_setup.tcl:80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt_setup.tcl#L80) `set CONSTRAINT_FILES "var_Day1.tcl orca_pt_constraints.tcl"`——注意这是**多个文件**（用空格分隔），所以 pt.tcl 要用 `foreach` 逐个读。

**pt.tcl 按后缀分流读约束**：

[PrimeTime/pt.tcl:62-70](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L62-L70) 先用 `info exists CONSTRAINT_FILES` 判断变量是否存在，再 `foreach` 遍历每个约束文件，按 `.sdc` 后缀决定用 `read_sdc -echo` 还是 `source -echo`。`-echo` 把执行的命令回显到日志，便于追溯。

**传播时钟（注意误导性注释）**：

[PrimeTime/pt.tcl:73-77](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L73-L77) 注释写着「Clock Tree Synthesis Section」，但唯一的命令是 `set_propagated_clock [all_clocks]`——它把所有时钟切到传播模式，让 STA 沿网表里已有的真实时钟树算插入延迟，**并非重新做 CTS**。

**重算时序 + 约束自检**：

[PrimeTime/pt.tcl:84-87](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L84-L87) `update_timing -full` 完整重算 timing graph；`check_timing -verbose` 把约束疏漏写到 `reports/ct.report`。

**报告时序（STA 的主输出）**：

[PrimeTime/pt.tcl:93-95](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L93-L95) 三条报告命令：

- `report_timing -slack_lesser_than 0.0 -delay min_max -nosplit -input -net -sign 4`：只列 slack < 0 的违例路径（`-slack_lesser_than 0.0`），同时算 min（hold）和 max（setup）两种延迟（`-delay min_max`），`-input` 显示输入引脚转换时间、`-net` 显示网名、`-sign 4` 保留 4 位有效数字，结果写到 `reports/rt.report`。
- `report_clock -skew -attribute`：报告时钟及其 skew，写到 `rc.report`。
- `report_analysis_coverage`：报告时序弧检查覆盖度，写到 `rac.report`。

#### 4.4.4 代码实践

**实践目标**：追踪约束这一类输入，并理解「为什么传播时钟」。

**操作步骤**：

1. 在 `pt_setup.tcl` 第 80 行找到 `CONSTRAINT_FILES`，看到它是多文件列表。
2. 在 `pt.tcl` 第 62–70 行看 `foreach` 如何按 `.sdc` 后缀分流。
3. 对比 `set_propagated_clock`（第 77 行）与 u4-l5 讲过的 ICC2 CTS（`clock_opt`），体会「PnR 里建树、STA 里传播」的分工。

**需要观察的现象**：约束文件定义在 pt_setup、消费在 pt.tcl；传播时钟紧接在约束之后、`update_timing` 之前。

**预期结果**：约束输入的「定义点」在 pt_setup 第 80 行，「消费点」在 pt.tcl 第 62–70 行。若注释掉 `set_propagated_clock`，PrimeTime 会用 ideal 时钟延迟算 slack——这会让 setup 看起来比实际好（忽略了真实 skew/插入延迟），签核结论失真。

#### 4.4.5 小练习与答案

**练习 1**：`pt.tcl` 第 93 行的 `-delay min_max` 和 `-slack_lesser_than 0.0` 各自的含义是什么？

**答案**：`-delay min_max` 表示同时报告最小延迟路径（对应 hold 检查）和最大延迟路径（对应 setup 检查），一份报告覆盖两类违例。`-slack_lesser_than 0.0` 表示只列出 slack 小于 0 的路径——也就是违例路径，过滤掉已满足的路径，让报告聚焦问题。

**练习 2**：为什么 PrimeTime 里要做 `set_propagated_clock`，而 ICC2 PnR 流程（u4-l5）里时钟一开始是 ideal 的？

**答案**：PnR 流程里时钟在 CTS 前是 ideal（直通），是为了让布局阶段不被还没建好的时钟树干扰；CTS 之后时钟才切换为 propagated。STA 读的是 PnR **之后**的网表，时钟树已经存在，所以必须标记为 propagated，让 PrimeTime 沿真实树算插入延迟，slack 才反映真实硬件。

---

### 4.5 保存会话：save_session

#### 4.5.1 概念说明

一次完整的 STA 跑完之后，库、网表、寄生、约束、算好的 timing graph 全都在内存里。`save_session` 把这**整个内存现场**存成一个会话目录。它的价值是：

- **事后复查**：交付后若有人质疑某条路径的 slack，可以用 `restore_session` 一键回到当时的完整状态，重新 `report_timing` 看细节，而不必重跑整个流程。
- **交接审计**：签核的会话目录本身就是可追溯的交付物，保证「我报告的 slack 来自这个确定的现场」。

`save_session` 之前通常先用 `file delete -force` 清掉旧会话目录（避免覆盖时的残留），`RUN.tcl` 里就有这个写法。

#### 4.5.2 源码精读

**pt.tcl 收尾**：

[PrimeTime/pt.tcl:112-115](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl#L112-L115) `stop_profile`（停止性能统计）、`save_session my_savesession`（存会话到 `my_savesession` 目录）、`print_message_info`（打印本次运行的消息摘要，含 warning/error 计数）、`exit` 退出。

**RUN.tcl 的最小化收尾（含清理旧目录）**：

[PrimeTime/RUN.tcl:35-38](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/RUN.tcl#L35-L38) 先 `file delete -force orca_savesession` 清旧目录，再 `save_session orca_savesession`，最后 `quit`。这是更稳健的写法。

#### 4.5.3 RUN.tcl vs pt.tcl：最小范例与完整流程的对照

理解了上面四个模块，就能看出 `RUN.tcl` 其实是 `pt.tcl` 的「极简硬编码版」，适合第一次读流程：

| 阶段 | pt.tcl（变量驱动，完整） | RUN.tcl（硬编码，最小） |
|------|--------------------------|--------------------------|
| Setup | `source common_setup.tcl; source pt_setup.tcl`（第 25–26 行） | 同（第 8–9 行） |
| 库 | `link_path` 由 pt_setup 拼装（第 43 行） | 无显式 link_path，靠 search_path |
| 读网表 | `read_verilog $NETLIST_FILES; link_design -verbose`（第 45–48 行） | `read_verilog orca_routed.v.gz; link_design ORCA`（第 21–22 行） |
| 反标 | `read_parasitics $PARASITIC_FILES; report_annotated_parasitics`（第 54–56 行） | `read_parasitics ../ref/design_data/ORCA.SPEF.gz`（第 24 行） |
| 读约束 | `foreach ... read_sdc/source`（第 62–70 行） | `source -echo -verbose orca_pt_constraints.tcl`（第 29 行） |
| 报告 | `report_timing / report_clock / report_analysis_coverage`（第 93–95 行） | `report_analysis_coverage`（第 30 行） |
| 保存 | `save_session my_savesession`（第 113 行） | `save_session orca_savesession`（第 36 行） |

可以看出 RUN.tcl 省略了传播时钟、`update_timing`、`check_timing`、反标自检、详细 `report_timing` 等签核关键步骤，是「能跑通」的骨架；`pt.tcl` 才是「可签核」的完整流程。

#### 4.5.4 代码实践

**实践目标**：把四类输入在两个脚本里的「定义点」与「消费点」整理成一张表，完成本讲的实践任务。

**操作步骤**：

1. 仿照上表，自己列一张「四类输入追踪表」，每类填「定义文件:行号」「消费文件:行号」。
2. 用 `pt.tcl` 的注释分段（每个 `####...` 分隔的 Section）作为流程路标，把脚本读成一条直线。
3. 对比 `RUN.tcl`，找出它比 `pt.tcl` 少做了哪些签核关键步骤。

**需要观察的现象**：四类输入的「定义」全部集中在 `common_setup.tcl`/`pt_setup.tcl`，「消费」全部集中在 `pt.tcl` 的对应 Section。

**预期结果**（四类输入追踪表）：

| 输入类型 | 定义点 | 消费点（pt.tcl） |
|----------|--------|------------------|
| 链接库 link_path | `pt_setup.tcl:37`（由 `common_setup.tcl:24,27` 的库拼出） | 第 43 行启用，第 48 行 `link_design` 解析 |
| 网表 NETLIST_FILES | `pt_setup.tcl:41`（顶层名 `common_setup.tcl:8`） | 第 45 行 `read_verilog` |
| 寄生 PARASITIC_FILES | `pt_setup.tcl:72` | 第 54 行 `read_parasitics` |
| 约束 CONSTRAINT_FILES | `pt_setup.tcl:80` | 第 62–70 行 `read_sdc`/`source` |

RUN.tcl 相比 pt.tcl **至少少做了**：传播时钟（`set_propagated_clock`）、`update_timing`、`check_timing`、反标自检（`report_annotated_parasitics`）、详细 `report_timing`（`-delay min_max` 等）。

#### 4.5.5 小练习与答案

**练习 1**：`save_session` 保存的「会话」里包含哪些内容？为什么它比重跑一次脚本更省事？

**答案**：会话保存的是 PrimeTime 内存里的完整现场——已读的库、网表、反标的寄生、施加的约束、算好的 timing graph。重跑脚本要重新读 `.db`、读网表、读 SPEF、link、update_timing，可能耗时几十分钟到几小时；而 `restore_session` 把现场整体载入，几秒到几十秒就能回到当时状态，直接 `report_timing` 看任意路径。

**练习 2**：`print_message_info`（pt.tcl 第 114 行）在退出前打印什么？为什么它对签核有用？

**答案**：它打印本次运行中所有消息的统计摘要，特别是 warning 和 error 的计数与列表。签核时这是「健康度」指标——如果有未处理的 error 或关键 warning（如 unresolved reference、未约束端口），签核结论就不可靠。所以 `print_message_info` 是退出前的最后一道自检。

---

## 5. 综合实践

**任务**：把本讲的四类输入与三个脚本，画成一张「PrimeTime STA 数据流图」，并用一句话串起 u2-l3 → u4-l7 → 本讲的完整签核链路。

**操作步骤**：

1. 在纸上画三个方框：`common_setup.tcl`、`pt_setup.tcl`、`pt.tcl`，用箭头表示 `source` 依赖（pt.tcl source 前两个）。
2. 从 `common_setup.tcl` / `pt_setup.tcl` 引出四条「输入」线（库 / 网表 / 寄生 / 约束），标注各自的定义行号，连到 `pt.tcl` 内对应的 Section（Netlist Reading / Back Annotation / Reading Constraints）。
3. 在 `pt.tcl` 内部按顺序画出执行主线：`read_verilog → link_design → read_parasitics → 读约束 → set_propagated_clock → update_timing → check_timing → report_timing → save_session`。
4. 在图的最左端标出每个输入的「上游来源」：网表与 SPEF 来自 u4-l7 的 ICC2 `write_verilog`/`write_parasitics`；约束 SDC 来自 u2-l3 讲的 SDC（`create_clock`/`set_input_delay` 等）。
5. 在最右端标出「交付物」：`reports/rt.report`（slack 违例报告）+ `my_savesession`（可复查会话）。

**预期结果**：你应当能用一句话概括整条链路——

> u2-l3 写好的 SDC 约束、u4-l7 PnR 吐出的网表和 SPEF，被 `pt.tcl` 按顺序读入（库→网表→寄生→约束），反标真实寄生、传播时钟、重算时序后，由 `report_timing` 给出 slack，最终连同整个现场存进 `save_session`，完成独立于 PnR 的签核。

**待本地验证**：因为本仓库只含脚本骨架（`../ref/ORCA.SPEF.gz`、`.db` 库、约束文件均不在仓库内），上述流程的完整运行需要在装有 PrimeTime 与完整 Synopsys RM 参考数据的环境中进行。在没有这些数据时，本实践以「画图 + 源码追踪」的形式完成。

## 6. 本讲小结

- PrimeTime 做 **静态时序签核**：不跑激励，沿 timing graph 穷举路径算 slack，以它的结论（而非 PnR 工具的结论）为最终交付依据。
- 脚本严格遵循「**变量与流程分离**」：`common_setup.tcl` 写公共库变量、`pt_setup.tcl` 写 PrimeTime 运行时变量、`pt.tcl` 跑流程，换设计只改前两个文件。
- 四类输入各司其职：**库**（`link_path`，解析单元）、**网表**（`read_verilog`）、**寄生**（`read_parasitics` 反标 SPEF）、**约束**（`read_sdc`/`source`），定义点都在 setup 文件、消费点都在 `pt.tcl` 的对应 Section。
- 「**读网表 → 链接 → 反标 → 读约束 → 传播时钟 → 重算 → 报告 → 存会话**」是必须遵守的顺序；反标和传播时钟是初学者最易漏掉、却决定签核可信度的两步。
- `RUN.tcl` 是硬编码的最小骨架（缺传播时钟/update_timing/反标自检/详细报告），`pt.tcl` 才是可签核的完整流程；`report_annotated_parasitics -check` 的反标覆盖率与 `check_timing` 的约束疏漏是两道关键自检。
- 本讲串起了 u2-l3（SDC）与 u4-l7（网表 + SPEF 输出），完成了「PnR 交付 → PrimeTime 签核」的闭环。

## 7. 下一步学习建议

- **下一讲 u6-l2（PrimeTime 实用脚本：case analysis 追踪）**：深入 `PrimeTime/UsefulScripts/report_case_propagation.tcl`，学习如何用 Tcl proc 在 timing graph 上回溯 case analysis 的传播，理解时序弧与 case value 属性，是把本讲的「报告」做深做细的进阶内容。
- **建议继续阅读的源码**：
  - 重新读一遍 `PrimeTime/pt.tcl`，对照本讲的 Section 划分，确认你能在不看讲义的情况下说出每段在干什么。
  - 回顾 `IC Compiler II/03_PnR_setup.tcl`（u4-l7 用过），对比 ICC2 与 PrimeTime 在 setup 文件结构上的异同，体会 Synopsys RM 跨工具的一致性。
  - 若你对 STA 想再深入，可关注本仓库 `PrimeTime/UsefulScripts/` 目录——那里的脚本展示了 PrimeTime Tcl 二次开发的实战技巧。
- **前置回顾**：如果对 `create_clock`/`set_input_delay` 等 SDC 命令已经模糊，回到 u2-l3 复习；如果对「为什么 PnR 要输出 SPEF 和非 PG 网表」不清楚，回到 u4-l7 的收尾章节。
