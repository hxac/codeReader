# ICC2 设计初始化与 MCMM 设置

## 1. 本讲目标

从本讲开始,我们正式进入 **U4:ICC2 物理设计主流程(RTL→GDSII)**。这是整本手册的核心单元,而本讲又是这一单元的起点——**设计初始化(setup)**。

读完本讲,你应当能够:

1. 说清楚 ICC2 把一个设计"加载进来"的完整初始化链路:`create_lib` → `read_verilog` → `link_block` → 读寄生 → 设 site/层方向 → MCMM。
2. 解释 `create_lib` 为什么同时需要 `-technology`(工艺文件)和 `-ref_libs`(NDM 参考库),并把它和 U3 的 NDM 库生成(u3-l2)接上。
3. 理解 TLU+ 寄生文件如何被读入、如何被绑定到不同的工艺角(corner)。
4. 用一句话讲清 **MCMM(多角多模)** 中 **corner / mode / scenario** 三个概念,以及为什么时序签核必须用 MCMM。
5. 看懂 ICC2 用 `save_block` / `open_block` / `copy_block` 管理设计版本的"存档-读档-另存为"机制。

> 本讲只讲 **setup 阶段**,即布图规划(floorplan)之前的全部准备。布局、电源、CTS、布线留给后续讲义。

---

## 2. 前置知识

本讲默认你已经读过 [u3-l1 标准单元库与物理数据基础](u3-l1-standard-cell-libraries.md) 与 [u3-l2 创建 NDM 参考库](u3-l2-ndm-library-creation.md)。下面这些概念是本讲的"地基",先快速回顾:

- **标准单元库**分两面:**时序面**(Liberty `.db`,给出单元延迟)和**物理面**(LEF,给出尺寸/引脚)。ICC2 把它们合并成统一的 **NDM** 参考库(见 u3-l2)。
- **PVT 角(process / voltage / temperature)**:同一份电路在不同工艺偏差、电压、温度下表现不同。本仓库用 **Nangate 45nm FreePDK45**,典型两角是 `ss0p95v125c`(慢工艺、0.95V、125℃,worst **setup**)和 `ff1p25v0c`(快工艺、1.25V、0℃,worst **hold**)。
- **寄生(parasitics)**:布线金属产生的电阻/电容,会让信号变慢。ICC2 用预计算的 **TLU+** 查表(`.tlup`)来快速估算寄生,分 max/min 两份。
- **setup(建立)/hold(保持)检查**:寄存器要在时钟沿之前足够早(setup)、之后足够久(hold)收到数据。这是 STA 的核心,详见 [u2-l3](u2-l3-sdc-timing-constraints.md)。
- **SDC(设计约束)**:`create_clock`、`set_input_delay` 等 Tcl 命令,告诉工具外部时序环境。

几个本讲新引入的术语,先记个印象:

| 术语 | 一句话解释 |
| --- | --- |
| **MCMM** | Multi-Corner Multi-Mode,多工艺角 × 多工作模式,让一次分析覆盖芯片所有"最坏情况"。 |
| **corner(角)** | 一个 PVT 工作点 + 一组寄生条件,如 slow/fast。 |
| **mode(模式)** | 芯片的一种功能配置,如功能模式(func)、测试/扫描模式(scan)。 |
| **scenario(场景)** | 一个 corner 与一个 mode 的组合,是时序分析的最小单元。 |
| **block** | ICC2 中"一块设计"的数据库单位,可存档/读档/另存。 |

---

## 3. 本讲源码地图

本讲涉及三个核心文件,加一个"对照版"模板:

| 文件 | 角色 | 本讲怎么看 |
| --- | --- | --- |
| [`IC Compiler II/PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | 一份**实际运行**用的精简主流程脚本(211 行,设计名 `ChipTop`)。它的 **L1–L34 就是 setup 阶段**。 | 看它如何把 setup 串成一条线。 |
| [`IC Compiler II/Scripts/01_common_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) | **变量定义中心**:库名、工艺文件、TLU+ 文件、电源网络名等全部集中在此。 | 看 setup 用到的所有路径变量从哪来。 |
| [`IC Compiler II/Scripts/02_mcmm_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl) | **MCMM 专用脚本**:建角、建模式、建场景、把寄生绑到角、给每个场景灌 SDC。 | 这是模块 4.3 的主角。 |
| `IC Compiler II/Scripts/03_PnR_setup.tcl`(对照) | 官方 **Synopsys P-2020.03-SP4 模板**(537 行,设计名 `pit_top`),注释里明确把流程分成 8 个编号阶段。 | 当 `PnR.tcl` 写得"精简到看不全"时,用它对照看标准写法(尤其 `link_block` 与 `save_block`)。 |

> ⚠️ **重要提醒(模板特性)**:仓库里有**两套** PnR 脚本,对应**两个不同设计**:
> - `PnR.tcl` 针对 `ChipTop`(多电压 MVT、带 SRAM 宏单元),它 `source ./input/common_setup.tcl` 与 `source ../scripts/mcmm.tcl` —— 但这两个文件**并不在本仓库内**,且它用的 `$LINK_LIBRARY_FILES_MVT` / `$NDM_REFERENCE_LIB_DIRS_MVT` 等变量也**未在仓库的 `01_common_setup.tcl` 中定义**(那里只有不带 `_MVT` 后缀的 `NDM_REFERENCE_LIB_DIRS`)。
> - `Scripts/03_PnR_setup.tcl` 针对 `pit_top`,完整 `source` 了仓库自带的 `01_common_setup.tcl` 与 `02_mcmm_setup.tcl`,是**自洽的教科书模板**。
>
> 这是 EDA 脚本的常态:**它们是模板,不是开箱即用的可执行程序**。每个真实项目都要按自己的设计名、库路径、变量改一遍。本讲会同时引用两套,让你既能看懂"实际跑的脚本",也能对照"标准写法"。

---

## 4. 核心概念与源码讲解

setup 阶段可以拆成四个最小模块:

1. **create_lib / read_verilog / link_block:设计初始化链路** —— 把设计"装"进 ICC2。
2. **读入 TLU+ 寄生并设置 site 与布线层方向** —— 告诉工具"电阻电容怎么算"和"金属怎么走"。
3. **MCMM 多角多模:corner / mode / scenario** —— 一次性分析所有最坏情况。
4. **save_block 与版本管理** —— 像"游戏存档"一样管理设计快照。

### 4.1 create_lib / read_verilog / link_block:设计初始化链路

#### 4.1.1 概念说明

在 PnR 之前,综合工具(Design Compiler)已经把 RTL 翻译成了**门级网表**(gate-level netlist)——一份只由标准单元(AND、DFF、…)和连线组成的 Verilog 文件(见 [u1-l2](u1-l2-asic-flow-panorama.md) 的流程图)。PnR 工具**不吃 RTL,只吃网表**。

所以 setup 阶段第一件事,就是把这份网表"装"进 ICC2。这件事分三步:

- **建库(`create_lib`)**:为**你的设计**创建一个全新的设计库(design library / block library),并把它挂靠到工艺文件(`.tf`)和已有的标准单元 NDM 参考库上。
- **读网表(`read_verilog`)**:把门级网表读进这个库,形成一个 block。
- **链接(`link_block` / `current_design`)**:把网表里引用的每个单元名(如 `DFFHQX1`)解析(resolve)到 NDM 参考库里对应的真实单元,完成"逻辑名 → 物理实体"的绑定。

> 🔗 **与 u3-l2 的衔接**:u3-l2 里 `create_workspace`/`commit_workspace` 生产的是**标准单元的 NDM 参考库**(原料库)。本讲的 `create_lib` 则是**消费**这些参考库,再创建一个**承载你设计**的库。两者是"造砖"与"盖房"的关系:`ref_libs` 是砖,`create_lib` 建的是房子。

#### 4.1.2 核心流程

setup 初始化的伪代码链路:

```
读变量 (source common_setup.tcl)        # 库名、工艺文件、TLU+、电源名…
└─ create_lib                            # 建你的设计库,挂 .tf + NDM 参考库
   └─ read_verilog -top <顶层> <网表>      # 读门级网表,自动初步链接
      └─ link_block / current_design     # 显式重新链接,把单元名对到参考库
         └─ (report_ref_libs / check)    # 检查引用是否齐全
```

关键点:`create_lib` 的两个参数缺一不可——

- `-technology $TECH_FILE`:工艺文件定义**金属层栈、site(放置格)、布线规则**,是"物理世界的规则书"。
- `-ref_libs $NDM_REFERENCE_LIB_DIRS`:NDM 参考库提供**每个单元的时序+物理数据**,是"零件库"。

#### 4.1.3 源码精读

**① 变量定义中心——`01_common_setup.tcl`**

所有 setup 用到的路径变量都集中在这里。先看设计名与库的基础信息:

[IC Compiler II/Scripts/01_common_setup.tcl:8-13](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L8-L13) —— 定义设计名 `pit_top`、Nangate 库根目录与 `search_path`(工具找文件的搜索路径)。

[IC Compiler II/Scripts/01_common_setup.tcl:14-15](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L14-L15) —— **NDM 参考库**,正是 `create_lib -ref_libs` 要用的。注意这里是**两个角**:`ss0p95v125c`(slow)与 `ff1p25v0c`(fast),这就是后面 MCMM 的种子。

[IC Compiler II/Scripts/01_common_setup.tcl:17](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L17) —— `TECH_FILE`(工艺文件),正是 `create_lib -technology` 要用的。

[IC Compiler II/Scripts/01_common_setup.tcl:34-36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L34-L36) —— 综合目录 `SYN_DIR`、门级网表 `$VERILOG_NETLIST_FILES`、SDC 约束。这三者把"前端综合的产物"接进来。

**② 三步初始化——`PnR.tcl` 的实际写法**

[IC Compiler II/PnR.tcl:1-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L1-L9) —— 这是 setup 的开头:开 16 核、`source` 外部的 `common_setup.tcl`、设 `link_library`/`target_library`、定电源/地、然后 `create_lib -ref_libs $NDM_REFERENCE_LIB_DIRS_MVT -technology $TECH_FILE ../work/chiptop`。

注意这行 `create_lib` 同时给出了 `-ref_libs`(参考库)和 `-technology`(工艺文件),末尾的 `../work/chiptop` 是要**新建的设计库路径/名字**。

[IC Compiler II/PnR.tcl:14-18](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L14-L18) —— 读网表:`TOP_DESIGN ChipTop`,`gate_verilog` 指向前端综合输出 `../../dc/output/compile.v`,然后 `read_verilog -top $TOP_DESIGN $gate_verilog` 读入,再用 `current_design $TOP_DESIGN` 把它设为当前设计。

> 🔍 **注意**:`PnR.tcl` 用 `current_design` 完成设定;而标准模板 `03_PnR_setup.tcl` 用 `link_block` 显式重链接。两者都合法——`read_verilog` 读入时已自动链接,`link_block` 则在你改动参考库后**重新解析**一遍引用。下面看标准写法。

**③ 标准写法——`03_PnR_setup.tcl` 的对照**

[IC Compiler II/Scripts/03_PnR_setup.tcl:27-33](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L27-L33) —— 教科书式三连:`create_lib -technology $TECH_FILE -ref_libs $NDM_REFERENCE_LIB_DIRS ${DESIGN_NAME}.dlib`(建库)→ `read_verilog -top ${DESIGN_NAME} $VERILOG_NETLIST_FILES`(读网表)→ `link_block`(链接)→ `report_ref_libs`(报告所有引用到的参考库,便于核对)。

这里的设计库叫 `${DESIGN_NAME}.dlib`(如 `pit_top.dlib`),与 `PnR.tcl` 的 `../work/chiptop` 是同一角色,只是命名与路径惯例不同。

#### 4.1.4 代码实践

**实践目标**:把"变量 → create_lib → read_verilog → link"的依赖关系手工串一遍。

**操作步骤(源码阅读型)**:

1. 打开 [`01_common_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl),列出 `create_lib` 需要的两个变量(`TECH_FILE`、`NDM_REFERENCE_LIB_DIRS`)分别在第几行定义。
2. 打开 [`PnR.tcl:9`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L9),确认 `create_lib` 这一行确实用到了这两个变量。
3. 对比 [`03_PnR_setup.tcl:27`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L27),写出两份脚本在设计库命名上的差异(`../work/chiptop` vs `${DESIGN_NAME}.dlib`)。

**需要观察的现象 / 预期结果**:你会确认 setup 是"先备料(common_setup 定义变量)、再动工(create_lib)、再上料(read_verilog)、再对账(link_block / report_ref_libs)"的严格顺序。**待本地验证**:在没有真实 Nangate 库的环境下无法实跑,以上为源码阅读结论。

#### 4.1.5 小练习与答案

**练习 1**:`create_lib` 为什么必须同时给 `-technology` 和 `-ref_libs`?少给一个会怎样?

> **参考答案**:`-technology` 的 `.tf` 定义"物理世界规则"(金属层、site、布线方向),`-ref_libs` 的 NDM 提供"零件"(每个标准单元的时序+物理)。没有 `.tf`,工具不知道有哪些金属层、单元该往哪种格子上放;没有参考库,网表里的单元名无法解析成真实可放置的实体,`link` 会报"unresolved cell"。

**练习 2**:`PnR.tcl` 里 `$NDM_REFERENCE_LIB_DIRS_MVT` 这个变量,能直接在仓库的 `01_common_setup.tcl` 里找到吗?说明了什么?

> **参考答案**:找不到。仓库的 `01_common_setup.tcl` 只定义了不带 `_MVT` 的 `NDM_REFERENCE_LIB_DIRS`。这说明 `PnR.tcl` 依赖一个**仓库里没有的** `./input/common_setup.tcl`(多电压 MVT 版),它是一份**需要用户自行补全的模板**。

---

### 4.2 读入 TLU+ 寄生并设置 site 与布线层方向

#### 4.2.1 概念说明

网表装进来后,工具还不知道两件事:

1. **金属连线的电阻/电容有多大?** —— 这决定了延迟。ICC2 不在现场逐段算,而是用**预计算的 TLU+ 查表**(`.tlup`)快速估算寄生。详见 [u3-l1](u3-l1-standard-cell-libraries.md)。
2. **标准单元摆在哪、金属怎么走?** —— 这需要 **site**(放置单元的网格)和**布线层方向**(哪层水平走线、哪层垂直走线)。

布线方向遵循一个老规矩:**相邻金属层正交交替**——一层水平、下一层垂直,像织布的经纬线,这样不同层的线能交叉而不打架。

#### 4.2.2 核心流程

```
read_parasitic_tech -tlup $TLUPLUS_MAX_FILE -layermap $MAP_FILE [-name tlup_max]
read_parasitic_tech -tlup $TLUPLUS_MIN_FILE -layermap $MAP_FILE [-name tlup_min]
        │  (max/min 两份寄生表分别用于 setup/hold 的悲观估计)
        ▼
get_site_defs ; set_attribute site unit  {symmetry, is_default}   # 放置网格
set_attribute [get_layers M*] routing_direction {horizontal|vertical}  # 层方向
```

`-layermap $MAP_FILE`(层映射文件)的作用:把工艺文件 `.tf` 里的层名(metal1…)与 TLU+ 内部的层号对上号,否则寄生表读不进去。

#### 4.2.3 源码精读

**① TLU+ 文件变量——`01_common_setup.tcl`**

[IC Compiler II/Scripts/01_common_setup.tcl:18-20](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L18-L20) —— `MAP_FILE`(层映射)、`TLUPLUS_MAX_FILE`(Cmax,最大电容,偏悲观)、`TLUPLUS_MIN_FILE`(Cmin,最小电容)。`MAX` 给慢/建立检查用,`MIN` 给快/保持检查用。

**② 实际读入——`PnR.tcl`**

[IC Compiler II/PnR.tcl:11-12](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L11-L12) —— 两行 `read_parasitic_tech` 分别读 max/min TLU+。这里**没有** `-name`,是"匿名"读入全局使用;后面 MCMM 脚本会用带名字的写法(见 4.3)。

**③ site 与层方向——`PnR.tcl`**

[IC Compiler II/PnR.tcl:20-29](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L20-L29) —— 逐层设布线方向:奇数金属层(M1/M3/M5/M7/M9)设为 `vertical`,偶数层(M2/M4/M6/M8)设为 `horizontal`,正交交替。注意这里 M1 是 vertical,而 `03_PnR_setup.tcl` 与 `01_common_setup.tcl` 里 M1 是 horizontal——**两种脚本方向约定相反**,这正是"模板需按真实工艺统一"的典型坑。

[IC Compiler II/PnR.tcl:31-32](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L31-L32) —— `set_wire_track_pattern` 在 M1 上设均匀(uniform)布线轨道,基于 `unit` site,坐标 0.037、间距 0.074。这定义了 M1 上线轨的物理网格。

**④ 标准写法的 site 设置——`03_PnR_setup.tcl`**

[IC Compiler II/Scripts/03_PnR_setup.tcl:51-57](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L51-L57) —— 标准模板更完整:`get_site_defs` 列出可用 site,把 `unit` site 设为 Y 轴对称(`symmetry Y`)且为默认(`is_default true`);再用一条命令把奇数层设 horizontal、偶数层设 vertical。这里能直接对比出 `PnR.tcl` 是"逐层手写"、模板是"按奇偶批处理"的两种风格。

#### 4.2.4 代码实践

**实践目标**:核对两套脚本的"布线方向约定"是否一致,体会模板适配的重要性。

**操作步骤(源码阅读型)**:

1. 记下 [`PnR.tcl:20`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L20) 中 M1 的方向。
2. 记下 [`03_PnR_setup.tcl:55-56`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L55-L56) 中 metal1 的方向。
3. 再看 [`01_common_setup.tcl:24`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L24) 的 `ROUTING_LAYER_DIRECTION_OFFSET_LIST` 中 metal1 的方向。

**需要观察的现象 / 预期结果**:你会发现三处对 M1 方向的约定**不完全一致**(PnR.tcl 为 vertical,另两处为 horizontal)。这说明:同一工艺的真实方向由 `.tf` 决定,脚本里的 `set_attribute` 只是"显式覆盖/确认";**实际项目必须让三处与 `.tf` 保持统一,否则布线会报方向冲突**。**待本地验证**(需真实 `.tf`)。

#### 4.2.5 小练习与答案

**练习 1**:`TLUPLUS_MAX_FILE` 和 `TLUPLUS_MIN_FILE` 分别偏向哪一侧?为什么需要两份?

> **参考答案**:MAX(Cmax)给出偏大的寄生电容,信号延迟偏大,用于 **setup(建立)** 检查(越慢越难满足建立);MIN(Cmin)给出偏小的寄生,用于 **hold(保持)** 检查。时序签核要同时覆盖"最慢"和"最快"两种极端,所以各取一份。

**练习 2**:相邻金属层为什么要正交(一水平一垂直)布线?

> **参考答案**:正交交替让相邻层的走线方向垂直,同层平行线之间用间距控制串扰,跨层则靠介质层隔离;这样密度高、布线资源利用率好,且便于自动布线器分配轨道。若两层同向,会大幅加剧串扰和拥塞。

---

### 4.3 MCMM 多角多模:corner / mode / scenario

#### 4.3.1 概念说明

这是本讲最核心、也最容易被初学者跳过的概念。**为什么需要 MCMM?**

芯片出厂后,可能工作在**不同的工艺角**(有的芯片晶体管偏快、有的偏慢;夏天温度高、冬天低;电池满电电压高、快没电电压低),也可能处于**不同的工作模式**(正常功能 func、或是测试/扫描 scan)。每种组合下,时序的"最坏情况"不同:

- **setup(建立)** 在**慢角**(高温、低压、慢工艺)最危险——信号太慢,赶不上时钟。
- **hold(保持)** 在**快角**(低温、高压、快工艺)最危险——信号太快,数据在寄存器来得及采样前就溜走了。

如果只在一个条件下分析,可能漏掉另一个极端的违例。**MCMM(Multi-Corner Multi-Mode)** 就是把所有"需要担心"的条件组合都建出来,**一次性**分析,确保芯片在任何情况下都达标。

三个概念严格区分:

- **corner(工艺角)**:一个 PVT 点 + 一组寄生。本仓库有 `slow`(对应 `ss0p95v125c` + max 寄生)、`fast`(对应 `ff1p25v0c` + min 寄生)。
- **mode(模式)**:一种功能配置。本仓库只有 `func`(功能模式)。真实项目常还有 `scan`(扫描测试)模式。
- **scenario(场景)**:corner × mode 的组合,是**时序分析的最小单元**。本仓库有 `func_fast`、`func_slow` 两个场景。

场景数量等于角数乘模式数:

\[
\text{场景数} = \text{corner 数} \times \text{mode 数} = 2 \times 1 = 2
\]

#### 4.3.2 核心流程

MCMM 设置的固定套路(`02_mcmm_setup.tcl` 的骨架):

```
remove_modes / remove_corners / remove_scenarios -all   # 清空旧的
create_corner slow ; create_corner fast                 # 建角
create_mode func ; current_mode func                    # 建模式
read_parasitic_tech -name tlup_max / tlup_min           # 给寄生表起名字
set_parasitics_parameters  early/late=tlup_min -> fast  # 把 min 寄生绑到 fast 角
set_parasitics_parameters  early/late=tlup_max -> slow  # 把 max 寄生绑到 slow 角
create_scenario -mode func -corner fast -name func_fast # 组合成场景
create_scenario -mode func -corner slow -name func_slow
foreach scenario: current_scenario X ; source $SDC      # 给每个场景灌约束
remove_duplicate_timing_contexts                        # 去重优化
```

**关键绑定逻辑**:为什么 `tlup_max`(最大寄生)绑到 `slow` 角?因为 slow 角本来就是 worst setup,叠加最大寄生 = 最悲观建立检查;同理 `tlup_min` 绑 fast 角 = 最悲观保持检查。这样每个场景都"盯"住自己该担心的极端。

#### 4.3.3 源码精读

**主角——`02_mcmm_setup.tcl` 全程拆解**

[IC Compiler II/Scripts/02_mcmm_setup.tcl:14-22](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L14-L22) —— 先 `remove_modes/corners/scenarios -all` 清场(防止重复加载残留),再 `create_corner slow`/`create_corner fast` 建**两个角**,`create_mode func` 并 `current_mode func` 建唯一**模式**。

[IC Compiler II/Scripts/02_mcmm_setup.tcl:28-36](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L28-L36) —— 这里的 `read_parasitic_tech` **带了 `-name`**:max 寄生命名为 `tlup_max`,min 寄生命名为 `tlup_min`。起了名字才能在下一步精确绑定到角。

[IC Compiler II/Scripts/02_mcmm_setup.tcl:37-45](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L37-L45) —— **核心绑定**:`set_parasitics_parameters -early_spec tlup_min -late_spec tlup_min -corners {fast}` 把 `tlup_min` 绑到 fast 角;`… -early_spec tlup_max -late_spec tlup_max -corners {slow}` 把 `tlup_max` 绑到 slow 角。(`early` 用于 hold/保持,`late` 用于 setup/建立。)

[IC Compiler II/Scripts/02_mcmm_setup.tcl:49-50](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L49-L50) —— **组合成场景**:`create_scenario -mode func -corner fast -name func_fast` 与 `… -corner slow -name func_slow`。注意名字的命名规则:`<mode>_<corner>`,一眼可读。

[IC Compiler II/Scripts/02_mcmm_setup.tcl:52-56](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L52-L56) —— **给每个场景灌约束**:`current_scenario func_fast` 后 `source $SDC_CONSTRAINTS`,再切到 `func_slow` 灌同一份 SDC。同一份 SDC 在两个场景下作用,但底层寄生/工艺角不同,所以分析结果不同。

[IC Compiler II/Scripts/02_mcmm_setup.tcl:60](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L60) —— `remove_duplicate_timing_contexts` 去除重复的时序上下文,缩减分析规模、提速。

**它在主流程里何时被调用?**

[IC Compiler II/PnR.tcl:34](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L34) —— `PnR.tcl` 在 setup 末尾 `source ../scripts/mcmm.tcl` 调用 MCMM 脚本(⚠️ 该文件不在仓库内,功能等价于本仓库的 `02_mcmm_setup.tcl`)。

[IC Compiler II/Scripts/03_PnR_setup.tcl:93-94](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L93-L94) —— 标准模板在 floorplan 阶段 `source $SDC_CONSTRAINTS` 后紧接 `source 02_mcmm_setup.tcl`,把约束与 MCMM 一起落地。注意它还在 [L90](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L90) 用 `set_parasitic_parameters -late_spec tlup_max -early_spec tlup_min` 设了一个"全局默认",再由 `02_mcmm_setup.tcl` 按角细化。

#### 4.3.4 代码实践(本讲指定实践任务)

**实践目标**:解释 `02_mcmm_setup.tcl` 中 slow/fast 角与 func 模式如何组合成 scenario,并说明为何需要 MCMM。

**操作步骤(源码阅读型)**:

1. 画一张 2×1 的表:行是 corner(`slow`/`fast`),列是 mode(`func`),每个格子里填对应的 scenario 名。答案应在 [`02_mcmm_setup.tcl:49-50`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L49-L50) 体现。
2. 追踪绑定关系:在 [`02_mcmm_setup.tcl:37-45`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L37-L45) 找出 `tlup_max` 绑哪个角、`tlup_min` 绑哪个角,并用一句话解释"为什么这么绑"。
3. 回答:如果删掉 `func_fast` 场景,会漏掉哪一类违例?

**需要观察的现象 / 预期结果**:

| 角 \ 模式 | func |
| --- | --- |
| slow | **func_slow**(绑 tlup_max →盯 setup) |
| fast | **func_fast**(绑 tlup_min →盯 hold) |

**为何需要 MCMM**:芯片在快/慢两种 PVT 极端下都会遇到时序风险——慢角下信号太慢难满足 setup,快角下信号太快难满足 hold。单角分析只能抓一种极端,MCMM 把两种极端都建成独立场景一次性分析,才能保证流片后任何一颗芯片、任何工况下都达标。

**待本地验证**:以上为源码阅读结论,真实 PVT 数值需配合 Nangate 库实跑 STA 报告确认。

#### 4.3.5 小练习与答案

**练习 1**:`create_scenario -mode func -corner fast -name func_fast` 中,mode 和 corner 各代表什么?为何名字写成 `func_fast` 而不是 `fast_func`?

> **参考答案**:mode = 功能模式(func),corner = PVT 角(fast)。命名采用 `<mode>_<corner>` 是团队约定(本脚本如此),目的是一眼读出"哪个模式下的哪个角";顺序本身不影响功能,只要全工程统一即可。

**练习 2**:为什么 `tlup_max`(最大寄生)绑到 `slow` 角,而不是 `fast` 角?

> **参考答案**:slow 角已经是 worst-case setup(高温低压慢工艺),再叠加最大寄生电容=延迟最大,组合出最悲观的建立检查场景。若把 max 绑到 fast,反而会让 fast 角的 hold 检查失真(过悲观且无意义),所以按"极端对极端"原则配对。

**练习 3**:`remove_duplicate_timing_contexts`(L60)起什么作用?

> **参考答案**:当多个场景产生相同的时序上下文(同样的 launch/capture 路径条件)时,该命令去重,减少重复计算,在不损失覆盖的前提下加速 STA。

---

### 4.4 save_block 与版本管理(block 数据库)

#### 4.4.1 概念说明

ICC2 把"一块设计在某一时刻的状态"存成一个 **block**,block 又存在 **library** 里。整个 PnR 流程很长(floorplan → power → placement → CTS → route → finish),每一步都可能出错或需要回退。ICC2 用一套类似"游戏存档"的机制管理版本:

- **`save_block`**:存档(可 `-as` 另存为新名字)。
- **`open_block`**:读档。
- **`copy_block`**:把某个存档"另存为"一个新版本,在新版本上继续做下一阶段(原存档不动,可随时回退)。
- **`close_blocks`**:关掉当前存档,释放内存。

这种"每阶段一个 block 快照"的设计,让你可以在 CTS 出问题时,回退到 placement 的存档重做,而不必从头跑。

#### 4.4.2 核心流程

标准模板每个阶段的固定模式:

```
# 阶段开始:从上一阶段的快照另存为新版本
copy_block -from_block <上一阶段>.design -to <本阶段>.design
current_block <本阶段>.design
……做本阶段的优化……
# 阶段结束:存档
save_block            # 或 save_block -as "<名字>"
```

#### 4.4.3 源码精读

**① setup 阶段首次存档——`03_PnR_setup.tcl`**

[IC Compiler II/Scripts/03_PnR_setup.tcl:69-71](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L69-L71) —— setup 收尾:`rename_block -to_block ${DESIGN_NAME}/init_design` 把刚 link 好的设计重命名为 `init_design`,然后 `save_block` 存档、`close_blocks` 关闭。

[IC Compiler II/Scripts/03_PnR_setup.tcl:76](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L76) —— floorplan 阶段一开始 `open_block ${DESIGN_NAME}.dlib:${DESIGN_NAME}/init_design` 把刚才的存档读回来。这就是"读档"。

**② 阶段间版本演进——`copy_block` 模式**

[IC Compiler II/Scripts/03_PnR_setup.tcl:80](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L80) —— 从 `init_design` 另存为 `floorplan_design`。后续每阶段同理:`power_plan_1`(L219)、`placement.design`(L323)、`cts.design`(L345)、`route.design`(L435)、`finish.design`(L481)——一条清晰的版本链。

**③ 最终存档——`PnR.tcl`**

[IC Compiler II/PnR.tcl:190-191](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L191) —— 全流程跑完,`save_block -as "${TOP_DESIGN}_Final"` 把最终结果存成 `ChipTop_Final`,并 `save_lib` 保存整个库。这个 `_Final` block 就是后续写 GDS 的来源(见 [L200](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L200) 的 `write_gds -design ${TOP_DESIGN}_Final`)。

#### 4.4.4 代码实践

**实践目标**:画出标准模板的 block 版本链,理解"阶段快照"思想。

**操作步骤(源码阅读型)**:

1. 在 [`03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 中检索所有 `copy_block -from_block` 与 `save_block -as`,记下每个 block 名字。
2. 按出现顺序连成链:`init_design → floorplan_design → power_plan_1 → placement → cts → route → finish`。
3. 解释:为什么用 `copy_block` 而不是直接在原 block 上继续改?

**需要观察的现象 / 预期结果**:你会得到一条线性版本链,每阶段独立快照。**为何用 copy_block**:保留前一阶段的干净存档,任一阶段失败都可 `open_block` 回退到上一阶段重做,不必从 setup 重跑——这是长流程的"安全网"。**待本地验证**(需 ICC2 环境)。

#### 4.4.5 小练习与答案

**练习 1**:`save_block` 和 `save_lib` 有什么区别?

> **参考答案**:`save_block` 保存"某一块设计"的状态快照(逻辑+物理当前进度);`save_lib` 保存整个**库**(含其下所有 block 与库级设置)。block 在 lib 之内,所以保存 lib 会连带保存其 block。

**练习 2**:`PnR.tcl` 末尾 `save_block -as "${TOP_DESIGN}_Final"` 这个 `_Final` block 之后被哪条命令消费?

> **参考答案**:被 [`PnR.tcl:200`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L200) 的 `write_gds -design ${TOP_DESIGN}_Final` 消费——即从这个最终快照导出 GDSII 版图,交付代工厂。

---

## 5. 综合实践

把本讲四个模块串起来,做一次**完整的 setup 阶段 walkthrough**:

**任务**:假设你要用 `03_PnR_setup.tcl` 这套标准模板,为一个新设计 `my_chip`(只有功能模式)跑 setup。请基于本仓库真实源码,写出:

1. 你需要在 [`01_common_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) 里改哪些变量?(至少列出 `DESIGN_NAME`、`NDM_REFERENCE_LIB_DIRS`、`TECH_FILE`、`VERILOG_NETLIST_FILES`、`SDC_CONSTRAINTS`)
2. setup 的执行顺序(用箭头串):`source common_setup → create_lib → read_verilog → link_block → read_parasitic_tech ×2 → 设 site/层方向 → check_design → save_block`。
3. 如果你想加一个扫描测试模式 `scan`,需要在哪里改 [`02_mcmm_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl)?会新增几个 scenario?

**参考思路**:

1. 改 `DESIGN_NAME "my_chip"`;把 `NDM_REFERENCE_LIB_DIRS`、`TECH_FILE` 指向你工艺库的路径;`VERILOG_NETLIST_FILES` 指向综合输出的 `my_chip.v`;`SDC_CONSTRAINTS` 指向 `my_chip.sdc`。
2. 顺序见上(对应 [`03_PnR_setup.tcl:16-71`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L16-L71))。
3. 在 [`02_mcmm_setup.tcl:21`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L21) 后加 `create_mode scan`,并在场景创建处新增 `create_scenario -mode scan -corner fast -name scan_fast` 与对应的 `scan_slow` 两个,scenario 总数变为 \(2 \text{ 角} \times 2 \text{ 模式} = 4\) 个;每个新场景需 `source` 对应的扫描 SDC。

---

## 6. 本讲小结

- **setup 阶段**= 把门级网表"装"进 ICC2 的准备过程,位于 floorplan 之前,对应 `PnR.tcl` 的 L1–L34、`03_PnR_setup.tcl` 的第 1 阶段。
- **初始化三连**:`create_lib`(`-technology` 工艺文件 + `-ref_libs` NDM 参考库,建你的设计库)→ `read_verilog`(读网表)→ `link_block`/`current_design`(链接单元到参考库)。NDM 参考库由 u3-l2 的 `create_workspace` 生产。
- **TLU+ 寄生**:读入 max/min 两份查表,经 `MAP_FILE` 层映射;max 绑 setup、min 绑 hold。同时设 site(放置网格)与布线层方向(奇偶正交交替)。
- **MCMM 三要素**:corner(PVT 角,本仓库 slow/fast)、mode(模式,本仓库 func)、scenario(= corner × mode,本仓库 func_fast/func_slow),把快/慢两种极端一次性纳入分析。
- **版本管理**:ICC2 用 `save_block`/`open_block`/`copy_block` 实现"每阶段一个快照",可随时回退;最终 `save_block -as *_Final` 作为写 GDS 的来源。
- **关键提醒**:仓库里的脚本都是**模板**——`PnR.tcl` 依赖仓库内不存在的 `./input/common_setup.tcl` 与 `../scripts/mcmm.tcl`、以及未定义的 `_MVT` 变量;且两套脚本的布线方向约定相反。真实项目须按自己的工艺 `.tf` 与设计名逐项改齐。

---

## 7. 下一步学习建议

setup 完成后,设计已"装"进 ICC2,下一步就是**布图规划(floorplan)**。建议接着学:

- **[u4-l2 布局规划 Floorplan](u4-l2-floorplan.md)**:setup 之后紧接的 `initialize_floorplan`、利用率、引脚放置、虚拟布局与拥塞/时序评估——对应 `PnR.tcl` 的 L38 起与 `03_PnR_setup.tcl` 的第 2 阶段。
- 在进入 floorplan 前,可回头巩固 [u3-l1](u3-l1-standard-cell-libraries.md) 与 [u3-l2](u3-l2-ndm-library-creation.md),确保你理解 `create_lib -ref_libs` 消费的 NDM 是怎么造出来的。
- 想深入 MCMM 的签核侧应用,可预习 [u6-l1 PrimeTime STA 基本流程](u6-l1-primetime-sta-flow.md)——PrimeTime 里 corner/scenario 的概念与本讲一脉相承。
