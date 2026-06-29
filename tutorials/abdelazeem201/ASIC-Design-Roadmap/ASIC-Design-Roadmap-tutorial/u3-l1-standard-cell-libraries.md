# 标准单元库与物理数据基础

## 1. 本讲目标

在前两个单元里，我们已经会读 RTL（`MY_DESIGN.v`、CMSDK SoC）和写时序约束 SDC（`My_Design.cons`）。那两讲回答的是「**设计本身长什么样、它对外部时序环境有什么要求**」。

但工具要做时序分析、布局布线，光有设计还不够——它还得知道：**每一个标准单元到底有多大的延迟、它物理上有多大、它的引脚在版图上哪个位置、连线本身会带来多少寄生电阻电容**。这些信息统称为「库数据与物理数据」，它们由晶圆厂（foundry）随工艺设计套件（PDK）提供，而本讲要讲的，就是 ICC2 的 PnR 流程**在哪里、用什么变量把它们喂给工具**。

学完本讲你应该能够：

1. 区分一个标准单元的「时序面」（Liberty `.lib/.db`）与「物理面」（LEF），理解 ICC2 为什么用统一的 NDM 把两面合并。
2. 读懂技术文件 `.tf` 在脚本中的角色，说清金属层的编号、命名与水平/垂直交替的布线方向约定。
3. 说清 TLU+ 寄生参数文件（`.tlup`）的作用，以及为什么要有 max/min 两份、它们如何与多角（corner）绑定。
4. 识别 `common_setup.tcl` 中与电源/地（PG）和布线层范围相关的变量，并理解它们在后续 floorplan、电源网络阶段被谁消费。

本讲只讲「库数据怎么描述、怎么被引用」，**不**讲 NDM 库本身是怎么生成的（那是下一讲 u3-l2 的主题）。

## 2. 前置知识

本讲默认你已经具备 u1-l2（ASIC 流程全景）和 u2-l3（SDC 时序约束）的认知。下面把要用到的几个词先用大白话过一遍。

- **标准单元（standard cell）**：晶圆厂提前画好、提前表征（characterize）好的现成逻辑门，比如 `NAND2_X1`（二输入与非，驱动强度 1）、`DFFHQX4`（触发器）。后端工程师不画晶体管，只挑单元、摆单元、连单元。
- **PPA**：功耗（Power）、性能（Performance，主要看时序/频率）、面积（Area）。库数据直接决定 PPA 能算得准不准。
- **时序分析需要两类输入**：(a) 设计 + 约束——回答「什么时候、谁驱动谁」（u2-l3 已讲）；(b) 单元库 + 寄生——回答「每次跳变要花多久」。本讲是第 (b) 类。
- **PVT 角（corner）**：同一颗单元，在不同**工艺偏差 P、电压 V、温度 T** 下表现不同。晶圆厂会给出最坏/最好等若干个「角」，工具要在所有角上都满足时序。例如 `ss0p95v125c` 表示 slow 工艺、0.95V、125℃（最慢、延迟最大），`ff1p25v0c` 表示 fast 工艺、1.25V、0℃（最快、延迟最小）。
- **寄生（parasitic）**：芯片上的金属连线不是理想导线，它有电阻 R 和电容 C，会拖慢信号。布线越长，寄生越大，延迟越高。
- **PDK（Process Design Kit）**：晶圆厂给的整套设计资料包。本仓库用的是开源的 **Nangate 45nm Open Cell Library（FreePDK45）**，10 层金属。

> 一个直观的比喻：RTL + SDC 是「**菜谱**」（要做什么菜、火候要求），而库数据是「**食材标签**」（每个单元的延迟、尺寸、寄生）。光有菜谱没有食材标签，厨房（EDA 工具）算不出这道菜到底几分钟能上桌。

## 3. 本讲源码地图

本讲只围绕三个脚本，它们是 ICC2 流程的「库数据加载链」：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `IC Compiler II/Scripts/01_common_setup.tcl` | 37 行 | **控制面板**：把所有库/物理数据文件路径写成 Tcl 变量，是后续一切脚本的输入源。 |
| `IC Compiler II/Scripts/03_PnR_setup.tcl` | 537 行（本讲只看 setup 段，约 16–60 行） | **消费者**：`source` 进 `01_common_setup.tcl`，再用 `create_lib` / `read_parasitic_tech` 等命令把变量对应的库真正读入工具。 |
| `IC Compiler II/Scripts/02_mcmm_setup.tcl` | 61 行 | **多角绑定**：把两份 TLU+ 寄生分别绑到 slow / fast 角，是 TLU+ 在多模多角下的落脚点。 |

一句话记忆：**`01` 定义变量 → `03` 读库 → `02` 把寄生分角**。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：时序库与物理库、技术文件与金属层、TLU+ 寄生参数、电源/地与布线层范围变量。

### 4.1 时序库与物理库（Liberty / LEF / NDM）

#### 4.1.1 概念说明

一个标准单元有「两张脸」：

- **时序面（Timing）**：这个单元带来多少延迟、输入电容多大、上升/下降转换时间多长、功耗多少。这部分由 Liberty 格式描述——文本形式是 `.lib`，经 Library Compiler 编译成二进制 `.db`。`.db` 里的延迟通常用 **NLDM（Non-Linear Delay Model）** 表格表示：延迟是「输入转换时间 × 输出负载」的二维查表。
- **物理面（Physical）**：这个单元在版图上多大（高 × 宽，单位 µm）、引脚在哪一层哪个坐标、哪些区域是金属阻挡（OBS，不能让别的线穿过）。这部分由 **LEF（Library Exchange Format）** 描述。

为什么要分两张脸？因为时序来自 SPICE 仿真表征，物理来自版图绘制，由不同团队、不同工具产出，天然是两套格式。它们都由 PDK 提供你**应该**能找到它们的位置：

`01_common_setup.tcl` 里有一条指向 Liberty 目录的变量：

```tcl
set DESIGN_REF_PATH "$Design_LIBRARY/lib/Front_End/Liberty/NLDM"
```

`Front_End/Liberty/NLDM` 目录通常就存放各 PVT 角的 `.db` 文件——但请注意，**`common_setup.tcl` 并不直接把 `.db` 喂给 ICC2**。ICC2 用的是一种把「时序面 + 物理面」合并后的统一二进制格式——**NDM（New Data Model）**。每个角对应一个 `.ndm` 文件，它已经把该角的 `.db` 时序数据和 LEF 物理数据打包在一起。`.db` 在这里只是 NDM 生成的「原料」（生成过程由下一讲的 `NDM_Creation.tcl` 完成）。

> 小结：对 ICC2 而言，时序库与物理库的最终形态是 **NDM**。Liberty `.db` 和 LEF 是它的两个上游来源。

#### 4.1.2 核心流程

库数据的准备与加载链路：

1. 晶圆厂 PDK 提供 `.lib`/`.db`（每角一份）、LEF、`.tf`、GDS。
2. 用 `NDM_Creation.tcl`（下一讲）把 `.db` + LEF + `.tf` 合成每角的 `.ndm`。
3. 在 `common_setup.tcl` 中，用变量 `NDM_REFERENCE_LIB_DIRS` 列出所有 `.ndm` 路径。
4. 在 `03_PnR_setup.tcl` 的 setup 段，`create_lib -technology ... -ref_libs $NDM_REFERENCE_LIB_DIRS ...` 把它们作为参考库挂到设计库上。

#### 4.1.3 源码精读

先看 `common_setup.tcl` 如何定义库根目录、Liberty 目录，并列出两个角的 NDM 参考库：

[IC Compiler II/Scripts/01_common_setup.tcl:10-15](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L10-L15) —— 定义 PDK 根目录 `Design_LIBRARY`、Liberty 目录 `DESIGN_REF_PATH`、技术文件目录 `DESIGN_REF_TECH_PATH`、搜索路径 `search_path`，以及最重要的参考库列表 `NDM_REFERENCE_LIB_DIRS`（含 `ss0p95v125c` 与 `ff1p25v0c` 两个角）。

其中两个 `.ndm` 文件名直接编码了 PVT 角：

- `NangateOpenCellLibrary_ss0p95v125c.ndm` → slow 工艺、0.95V、125℃，延迟最大（看 setup 用）。
- `NangateOpenCellLibrary_ff1p25v0c.ndm` → fast 工艺、1.25V、0℃，延迟最小（看 hold 用）。

再看消费者 `03_PnR_setup.tcl` 如何把这些参考库挂上：

[IC Compiler II/Scripts/03_PnR_setup.tcl:27-27](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L27-L27) —— `create_lib -technology $TECH_FILE -ref_libs $NDM_REFERENCE_LIB_DIRS ${DESIGN_NAME}.dlib`：创建名为 `pit_top.dlib` 的设计库，技术文件用 `$TECH_FILE`，参考库用刚定义的两角 NDM。这一步同时把时序（NDM 内的 `.db`）和物理（NDM 内的 LEF）一次性交给工具。

#### 4.1.4 代码实践

**实践目标**：学会从 `.ndm` 文件名反推 PVT 角的含义，建立「文件名即角」的直觉。

**操作步骤**：

1. 打开 `IC Compiler II/Scripts/01_common_setup.tcl`，找到 `NDM_REFERENCE_LIB_DIRS` 那两行。
2. 把两个文件名拆成 `工艺_电压_温度` 三段，填进下表：

| 文件名片段 | 工艺 P | 电压 V | 温度 T | 含义（最快/最慢） |
| --- | --- | --- | --- | --- |
| `ss0p95v125c` | ss（slow） | 0.95v | 125c（125℃） | 最慢角，主导 setup 检查 |
| `ff1p25v0c` | ? | ? | ? | ? |

**需要观察的现象 / 预期结果**：`ff1p25v0c` 应拆为 fast 工艺、1.25V、0℃，是「最快角」，主导 hold 检查。注意命名约定中的 `0p95` 表示 0.95（小数点用 `p` 替代），`125c` 的 `c` 代表摄氏度。这是 Synopsys/晶圆厂文件名的通用写法。

> 说明：本仓库不附带真实的 PDK 二进制（`.ndm`/`.db`/LEF 都需要各自去 Nangate 官网下载），所以**无法在本仓库内实际运行 ICC2**；本实践为「源码阅读型实践」，重在读懂变量与文件名的对应关系。

#### 4.1.5 小练习与答案

**练习 1**：为什么 ICC2 不直接读 `.db`，而要把它先合成 NDM？

> **参考答案**：`.db` 只有时序面，缺物理面（尺寸/引脚位置/OBS）。PnR 既要算时序又要摆单元、布线，必须同时拿到两张脸。NDM 把每角的 `.db` 时序与 LEF 物理合并成一个二进制，让工具一次加载即可同时用于时序与物理操作，效率与一致性都更高。

**练习 2**：如果某设计有三档电压（0.9V/1.0V/1.1V），`NDM_REFERENCE_LIB_DIRS` 大致要列几个 `.ndm`？

> **参考答案**：每个 PVT 角一个 `.ndm`。若每档电压都要在 slow/fast 工艺与典型温度下表征，则至少要准备相应数量的 `.ndm` 并全部列入该变量；工具会据此做多角时序分析。

---

### 4.2 技术文件与金属层（.tf）

#### 4.2.1 概念说明

技术文件（`.tf`，technology file）描述的是**整颗芯片的「金属层栈」（metal stack）**，而不是某个具体单元。它回答：

- 一共有哪些层、它们的**编号**和**名字**（如 `metal1`~`metal10`、各层间的过孔 via）。
- 每层的**首选布线方向**是水平（horizontal）还是垂直（vertical）。
- 每层的**默认线宽 / 最小间距**规则。
- **site（放置点）定义**——标准单元摆放的最小行单元（本仓库的 site 名为 `unit`），它定义了单元高度与对齐网格。

这里有一个贯穿全流程的物理约定——**相邻金属层布线方向交替（正交）**：

- 奇数层 `metal1, metal3, metal5, metal7, metal9` → 水平（horizontal）。
- 偶数层 `metal2, metal4, metal6, metal8, metal10` → 垂直（vertical）。

为什么必须正交？因为相邻两层若同向，一条线要跨过另一层时无处「借道」；正交后，水平线和垂直线在不同层交叉，过孔在交点垂直连通即可，布线资源才不会大面积互相阻塞。

#### 4.2.2 核心流程

技术文件的加载与层方向确认：

1. `common_setup.tcl` 用变量 `TECH_FILE` 指向 `.tf`，同时用 `ROUTING_LAYER_DIRECTION_OFFSET_LIST` 显式写下 metal1–metal10 的方向交替表（作为脚本里的可读冗余记录）。
2. `03_PnR_setup.tcl` 的 `create_lib -technology $TECH_FILE` 把 `.tf` 作为设计库的工艺基础。
3. 随后 `set_attribute [get_layers {...}] routing_direction ...` 用命令再次显式设定每层方向（与 `.tf` 内一致，起强调/覆盖作用）。
4. `get_site_defs` + `set_attribute ... symmetry/is_default` 把名为 `unit` 的 site 设为默认放置行，并允许 Y 轴对称翻转。

#### 4.2.3 源码精读

`common_setup.tcl` 定义技术文件路径，并用一条长变量把层方向表写下来：

[IC Compiler II/Scripts/01_common_setup.tcl:17-24](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L17-L24) —— `TECH_FILE` 指向 `FreePDK45_10m.tf`（FreePDK45、10 层金属的技术文件）；`ROUTING_LAYER_DIRECTION_OFFSET_LIST` 把 `metal1`~`metal10` 的水平/垂直方向逐一列出。

再看 `03_PnR_setup.tcl` 的 setup 段如何落地这些层信息：

[IC Compiler II/Scripts/03_PnR_setup.tcl:51-59](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L51-L59) —— `get_site_defs` 取出 site 定义并把 `unit` 设为默认、Y 对称；随后对奇数金属层设 `routing_direction horizontal`、对偶数金属层设 `routing_direction vertical`；最后 `report_ignored_layers` 报告被忽略的层。

> 注意第 57 行 `get_attribute [get_layers metal?] routing_direction` 中的通配符 `metal?` 只匹配单字符，因此恰好命中 `metal1`~`metal9`（共 9 层），不会匹配到 `metal10`——这是 Tcl/Synopsys collection 通配符的一个细节，改脚本时要留意。

#### 4.2.4 代码实践

**实践目标**：把「层名 ↔ 方向」的对应关系亲手对一遍，验证正交约定。

**操作步骤**：

1. 在 `01_common_setup.tcl` 第 24 行的 `ROUTING_LAYER_DIRECTION_OFFSET_LIST` 中，数一共有几对 `{metalN direction}`。
2. 在 `03_PnR_setup.tcl` 第 55–56 行，确认 `set_attribute` 把哪些层设为 horizontal、哪些设为 vertical。
3. 自行判断：如果要把 `metal10` 单独改成 horizontal，会破坏什么约定？

**预期结果**：共 10 对 `{metalN direction}`；奇数层 horizontal、偶数层 vertical 的约定在两处一致。若强行让 `metal10` 也变 horizontal，则 `metal9`（horizontal）与 `metal10`（horizontal）相邻同向，违反正交约定，高层布线（尤其电源 mesh）会冲突加剧。

> 待本地验证：上述方向一致性可由在 ICC2 中执行 `get_attribute [get_layers metal?] routing_direction` 后看输出确认。

#### 4.2.5 小练习与答案

**练习 1**：`.tf` 和 LEF 各自描述「层」的什么不同内容？

> **参考答案**：`.tf` 描述**整个金属层栈的全局规则**（层名/编号、首选方向、默认宽距、site 定义），是芯片级的；LEF 里的 `LAYER`/`MACRO` 描述的是**每个单元**如何使用这些层（单元的外框、引脚落在哪层哪点、OBS 阻挡区）。一个是「公路网规则」，一个是「每辆车停在哪」。

**练习 2**：`metal?` 为什么匹配不到 `metal10`？

> **参考答案**：`?` 是单字符通配符，`metal10` 在 `metal` 后有两个字符 `10`，需要用 `metal??` 或 `metal*` 才能匹配。所以第 57 行的查询会漏掉第 10 层，调试方向属性时要单独处理 `metal10`。

---

### 4.3 TLU+ 寄生参数

#### 4.3.1 概念说明

连线的寄生电阻 R 和电容 C 决定了**互连延迟**。信号从驱动单元出发，要给整条连线充电/放电，线越长，R、C 越大，到达接收端的时间越晚。一段连线的简化延迟（Elmore 模型）可写成：

\[
t_{\text{wire}} \;\approx\; 0.69 \cdot R_{\text{wire}} \cdot C_{\text{load}}
\]

其中连线电阻 \(R_{\text{wire}} = \rho \cdot L / (W \cdot H)\) 随长度 \(L\) 线性增长，电容 \(C_{\text{load}}\) 也随长度增加。总单元路径延迟大致是：

\[
t_{\text{pd}} \;\approx\; t_{\text{cell}} \;+\; t_{\text{wire}}
\]

布线完成后，工具要算出每段连线的真实 R/C，这叫**寄生提取**。但 PnR 过程中布线在不断变化，每次都做全量精确提取太慢。**TLU+（`.tlup`）** 是 Synopsys 预先做好的**查表**：给定（层、宽度、间距），秒查单位长度的 R/C，速度快到能在 PnR 内部反复调用。

为什么要 **max / min 两份**？因为芯片制造有工艺波动（OCV，片上变异），同一根线在最坏情况下电容偏大（信号更慢）、最好情况下电容偏小（信号更快）：

- **`TLUPLUS_MAX_FILE`（`..._Cmax.tlup`）**：电容偏大 → 延迟偏大 → 用于 **setup（建立时间）** 检查的 late（晚到）路径，绑定到 **slow 角**。
- **`TLUPLUS_MIN_FILE`（`..._Cmin.tlup`）**：电容偏小 → 延迟偏小 → 用于 **hold（保持时间）** 检查的 early（早到）路径，绑定到 **fast 角**。

此外还需要一个 **层映射文件 `.map`（`MAP_FILE`）**：因为 `.tf` 用的层名（如 `metal3`）和 TLU+ 内部用的层号不一定一致，`.map` 负责两层「字典」之间的翻译，提取寄生时才能对上号。

#### 4.3.2 核心流程

TLU+ 的定义、读入与分角绑定：

1. `common_setup.tcl` 定义三件套：`MAP_FILE`（层映射）、`TLUPLUS_MAX_FILE`、`TLUPLUS_MIN_FILE`。
2. `03_PnR_setup.tcl` 用 `read_parasitic_tech -tlup ... -layermap $MAP_FILE -name tlup_max/tlup_min` 把两份 TLU+ 读入工具并各自命名。
3. `02_mcmm_setup.tcl` 用 `set_parasitics_parameters` 把 `tlup_max` 绑到 slow 角、`tlup_min` 绑到 fast 角。
4. 之后每次算时序，工具对 setup/late 用 max 寄生，对 hold/early 用 min 寄生。

#### 4.3.3 源码精读

`common_setup.tcl` 一次性定义 TLU+ 三件套（注意行尾中文注释点明用途）：

[IC Compiler II/Scripts/01_common_setup.tcl:18-20](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L18-L20) —— `MAP_FILE`（TLUplus 的层映射）、`TLUPLUS_MAX_FILE`（Max TLUplus，注释明示）、`TLUPLUS_MIN_FILE`（Min TLUplus）。

消费者 `03_PnR_setup.tcl` 读入这两份 TLU+：

[IC Compiler II/Scripts/03_PnR_setup.tcl:45-49](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L45-L49) —— 两次 `read_parasitic_tech` 分别读入 max/min 的 `.tlup`，都带上 `-layermap $MAP_FILE` 做层名翻译，并命名为 `tlup_max` / `tlup_min`；随后 `report_lib -parasitic_tech` 汇报已加载的寄生技术。

最后看 `02_mcmm_setup.tcl` 如何把两份寄生绑到对应角：

[IC Compiler II/Scripts/02_mcmm_setup.tcl:37-45](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/02_mcmm_setup.tcl#L37-L45) —— `set_parasitics_parameters` 把 `tlup_min` 同时作为 early/late 绑到 `fast` 角，把 `tlup_max` 同时作为 early/late 绑到 `slow` 角。这样 slow 角始终用大电容（悲观），fast 角始终用小电容。

> 提示：`02_mcmm_setup.tcl` 第 38–40 行把 `tlup_min` 同时设为 `-early_spec` 和 `-late_spec`，看上去「早到/晚到都用 min」有点反直觉——这是因为该脚本对每个角内部固定用同一份寄生表（角间已经靠 slow/fast 区分了），属于模板写法。更精细的 OCV 流程会区分同一角内的 early/late，那是进阶话题。

#### 4.3.4 代码实践

**实践目标**：追踪「哪份 TLU+ 绑到哪个角」的完整链路，理解 max/min 与 setup/hold 的对应。

**操作步骤**：

1. 在 `01_common_setup.tcl` 找到 `TLUPLUS_MAX_FILE` 和 `TLUPLUS_MIN_FILE` 的定义（第 19、20 行）。
2. 在 `03_PnR_setup.tcl` 找到它们被 `read_parasitic_tech` 读入并命名为 `tlup_max`/`tlup_min` 的地方（第 45、47 行）。
3. 在 `02_mcmm_setup.tcl` 找到 `tlup_max` → `slow`、`tlup_min` → `fast` 的绑定（第 37–45 行）。
4. 画出依赖关系：`_Cmax.tlup` → `tlup_max` → slow 角 → setup；`_Cmin.tlup` → `tlup_min` → fast 角 → hold。

**预期结果**：你会得到一条清晰的「文件 → 工具内名字 → 角 → 检查类型」四段映射。这说明 max 寄生服务于最坏建立时间，min 寄生服务于最佳保持时间，二者共同覆盖了时序收敛的两个方向。

> 待本地验证：真实数值关系需在有 PDK 的 ICC2 环境里 `report_timing -delay_type max/min` 后比对路径延迟才能看到。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TLU+ 要准备 max 和 min 两份，而不是只用一份「标称」寄生？

> **参考答案**：制造存在 OCV，连线电容会在一个范围内波动。setup 检查要求「最晚到达也满足建立」，必须用偏大的 max 寄生；hold 检查要求「最早到达也满足保持」，必须用偏小的 min 寄生。只看标称值会漏掉两端的违规。

**练习 2**：`MAP_FILE` 在 `read_parasitic_tech` 中起什么作用？如果没有它会怎样？

> **参考答案**：它把 `.tf` 的层名翻译成 TLU+ 内部的层号，让寄生表能按层查到正确的 R/C。缺了它，工具无法确定哪根线属于哪一层，寄生提取会对不上层，时序结果失真甚至报错。

---

### 4.4 电源/地与布线层范围变量

#### 4.4.1 概念说明

这一组变量不描述外部文件，而是给工具一套**逻辑命名约定**和**布线资源边界**。

- **电源/地命名（PG）**：工具需要知道这颗芯片的电源网络叫什么、地网络叫什么、单元的电源/地引脚叫什么。本仓库用 4 个变量约定：
  - `NDM_POWER_NET = "VDD"`（电源**网络**名）
  - `NDM_POWER_PORT = "VDD"`（单元电源**引脚**名）
  - `NDM_GROUND_NET = "VSS"`（地网络名）
  - `NDM_GROUND_PORT = "VSS"`（单元地引脚名）
  
  网络名和引脚名可以不同（比如网络叫 `VDD`、单元引脚叫 `VDDCE`），但本设计二者一致。后续电源网络阶段就用这些名字创建 PG 网络并把每个单元的电源/地引脚连上去。

- **布线层范围（routing layer range）**：信号布线允许用到哪几层金属，由两个变量限定：
  - `MIN_ROUTING_LAYER = "metal1"`：信号布线最低可从 metal1 起。
  - `MAX_ROUTING_LAYER = "metal10"`：信号布线最高不能超过 metal10。

  为什么要限定？因为顶层金属（如 metal9/metal10）通常又厚又宽，是留给**电源 ring/mesh** 的「高速公路」，不该被信号线占用；而 metal1 紧贴单元，常用于标准单元 rail。给信号布线划清层范围，避免与电源资源争抢。

#### 4.4.2 核心流程

这组变量的生命周期：

1. `common_setup.tcl` 定义 PG 四变量与层范围两变量。
2. floorplan 阶段（`03_PnR_setup.tcl`）：`create_net -power $NDM_POWER_NET` / `-ground $NDM_GROUND_NET` 创建逻辑电源/地网络，再用 `connect_pg_net -net $NDM_POWER_NET [get_pins -hierarchical "*/VDD"]` 把所有单元的电源引脚挂到电源网络。
3. 电源网络阶段：用 `metal9/metal10` 建 ring/mesh、用 `metal1` 建 rail（这些层选择与 `MAX_ROUTING_LAYER=metal10`、rail 用 metal1 相呼应）。
4. 信号布线阶段：router 自动遵守 `MIN/MAX_ROUTING_LAYER` 范围。

#### 4.4.3 源码精读

`common_setup.tcl` 定义布线层范围与电源/地命名：

[IC Compiler II/Scripts/01_common_setup.tcl:25-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L25-L31) —— `MIN_ROUTING_LAYER`/`MAX_ROUTING_LAYER` 限定信号布线层为 `metal1`~`metal10`；`NDM_POWER_NET/PORT=VDD`、`NDM_GROUND_NET/PORT=VSS` 约定电源/地的网络与引脚名。

消费者 `03_PnR_setup.tcl` 在 floorplan 段用这些名字接电源/地：

[IC Compiler II/Scripts/03_PnR_setup.tcl:186-190](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L186-L190) —— `create_net -power $NDM_POWER_NET` 和 `-ground $NDM_GROUND_NET` 创建电源/地网络；`connect_pg_net` 把层次化查到的所有 `*/VDD`、`*/VSS` 引脚连到对应网络。变量在这里被真正「花掉」。

> 观察：`create_net` 用的是「网络名」变量（`NDM_POWER_NET`），而 `connect_pg_net` 的 `[get_pins ... "*/VDD"]` 用的是硬编码的引脚名 `VDD`——它正好等于 `NDM_POWER_PORT`。这是脚本里隐含的对应关系，若你的单元电源引脚不叫 `VDD`，这里就要同步修改。

#### 4.4.4 代码实践

**实践目标**：列出全部 6 个 PG/层范围变量，并说清「网络名 vs 引脚名」的区别。

**操作步骤**：

1. 打开 `01_common_setup.tcl` 第 25–31 行，把 6 个变量填入下表。

| 变量名 | 值 | 属于 | 含义 |
| --- | --- | --- | --- |
| `MIN_ROUTING_LAYER` | `metal1` | 布线范围 | 信号布线下限层 |
| `MAX_ROUTING_LAYER` | ? | 布线范围 | ? |
| `NDM_POWER_NET` | ? | 电源/地 | ? |
| `NDM_POWER_PORT` | ? | 电源/地 | ? |
| `NDM_GROUND_NET` | ? | 电源/地 | ? |
| `NDM_GROUND_PORT` | ? | 电源/地 | ? |

2. 思考：如果某单元库的电源引脚叫 `VDDCE` 而不是 `VDD`，需要改脚本里的哪两处？

**预期结果**：第 2 步需要同时改 `NDM_POWER_PORT` 的值**和** `03_PnR_setup.tcl` 第 189 行 `connect_pg_net` 中 `[get_pins -hierarchical "*/VDD"]` 里的 `VDD`。这正是「网络名可自洽、引脚名需手工对齐」的坑点。

> 待本地验证：在真实 ICC2 中可用 `get_pins -hierarchical */VDD` 查返回的引脚数量，确认是否所有单元都被连上电源。

#### 4.4.5 小练习与答案

**练习 1**：`NDM_POWER_NET` 和 `NDM_POWER_PORT` 有什么区别？为什么需要两个？

> **参考答案**：`NDM_POWER_NET` 是电源**网络**（net）的名字——逻辑上一根贯穿全芯片的电源线；`NDM_POWER_PORT` 是每个单元的电源**引脚**（pin）名字。需要两个是因为网络和引脚是两个层级的概念：`create_net` 建网络用前者，`connect_pg_net` 找引脚用后者。二者名字通常相同但概念不可混。

**练习 2**：为什么要把 `MAX_ROUTING_LAYER` 设成 `metal10` 而不是放开让信号线任意用所有层？

> **参考答案**：顶层金属（metal9/metal10）又厚又低阻，是电源 ring 与 mesh 的专用资源；放开会让信号布线挤占电源通道，导致 IR drop 恶化、电源网络难以收敛。限定范围是把「信号层」与「电源层」在物理上隔离的常规做法。

---

## 5. 综合实践

把本讲四个模块串起来，做一张「**库数据变量全景表**」，这是你后续阅读整条 PnR 流程的随身索引。

**任务**：在 `01_common_setup.tcl` 中找出**所有**与库/物理数据相关的变量（提示：基本覆盖第 8–36 行），按下表分类，并写出每个变量在 `03_PnR_setup.tcl` 或 `02_mcmm_setup.tcl` 中被哪条命令消费。

| 分类 | 变量 | 指向的文件/值 | 被哪条命令消费 |
| --- | --- | --- | --- |
| 设计名 | `DESIGN_NAME` | `pit_top` | `create_lib` / `read_verilog -top` |
| 时序/物理库（NDM） | `NDM_REFERENCE_LIB_DIRS` | 两个角的 `.ndm` | `create_lib -ref_libs` |
| 技术文件 | `TECH_FILE` | `FreePDK45_10m.tf` | `create_lib -technology` |
| 寄生参数 | `TLUPLUS_MAX_FILE` / `TLUPLUS_MIN_FILE` / `MAP_FILE` | `..._Cmax.tlup` / `..._Cmin.tlup` / `..._10m.map` | `read_parasitic_tech -tlup -layermap` |
| GDS 输出 | `GDS_MAP_FILE` / `STD_CELL_GDS` | gdsout map / 单元 GDS | `write_gds -layer_map -merge_files` |
| 布线层范围 | `MIN_ROUTING_LAYER` / `MAX_ROUTING_LAYER` | `metal1` / `metal10` | router 自动遵守 |
| 电源/地 | `NDM_POWER_NET/PORT` / `NDM_GROUND_NET/PORT` | `VDD` / `VSS` | `create_net` / `connect_pg_net` |
| 前端输入 | `VERILOG_NETLIST_FILES` / `SDC_CONSTRAINTS` | 综合输出的 `.v` / `.sdc` | `read_verilog` / `source` |

**操作步骤**：

1. 通读 `01_common_setup.tcl` 全 37 行，按上表分类把变量补全（注意 `GDS_MAP_FILE`、`STD_CELL_GDS` 属于 GDS 输出分类，要到 `03_PnR_setup.tcl` 末尾的 `write_gds` 才被消费）。
2. 对每个变量，去 `03_PnR_setup.tcl` 里用编辑器搜索 `$变量名`，确认它在哪条命令里被「花掉」。
3. 给整张表加一列「**数据类型**」，标注它是时序 / 物理 / 寄生 / 电源 / 范围 / IO 中的哪一类。

**预期结果**：你会得到一张覆盖「库数据从定义到消费」的完整地图，并能一眼看出：本讲真正关心的库/物理数据（NDM、`.tf`、TLU+、PG、层范围）在 setup 段（`03_PnR_setup.tcl` 第 16–60 行）几乎被全部加载完毕，之后整个 PnR 流程都建立在这套数据之上。

> 待本地验证：若你在有 PDK 的真实环境运行，可在 setup 段后执行 `report_ref_libs`（第 33 行）和 `report_lib -parasitic_tech`（第 49 行）核对库与寄生是否加载成功。

## 6. 本讲小结

- 一个标准单元有「时序面」（Liberty `.lib/.db`，含 NLDM 延迟表）和「物理面」（LEF，含尺寸/引脚/OBS）；ICC2 把每角的二者合并成统一的 **NDM**，由 `NDM_REFERENCE_LIB_DIRS` 列出，`create_lib -ref_libs` 加载。
- 技术文件 `.tf`（`TECH_FILE`）描述金属层栈；金属层布线方向**奇数层水平、偶数层垂直**正交交替，`03_PnR_setup.tcl` 用 `set_attribute routing_direction` 显式落地，并设置默认 site `unit`。
- **TLU+**（`.tlup`）是预计算的寄生 R/C 查表，分 **max/min 两份**；经 `read_parasitic_tech` 读入后，由 `02_mcmm_setup.tcl` 把 `tlup_max` 绑 slow 角（setup）、`tlup_min` 绑 fast 角（hold），并需要 `MAP_FILE` 做层名翻译。
- 电源/地用 `NDM_POWER_NET/PORT=VDD`、`NDM_GROUND_NET/PORT=VSS` 约定网络与引脚名，供 `create_net`/`connect_pg_net` 使用；`MIN/MAX_ROUTING_LAYER` 把信号布线限定在 `metal1`~`metal10`，保护顶层电源金属。
- `01_common_setup.tcl` 是「**控制面板**」（定义变量），`03_PnR_setup.tcl` 是「**消费者**」（用命令读入），把握这条「定义→消费」主线就能举一反三读懂整套 ICC2 脚本。
- 注意几个易错点：`metal?` 通配符匹配不到 `metal10`；`connect_pg_net` 里的引脚名 `VDD` 是硬编码，改库时需与 `NDM_POWER_PORT` 同步；本仓库不含真实 PDK 二进制，多数结论属「源码阅读型」认知，数值需在真实环境验证。

## 7. 下一步学习建议

本讲只讲了「库数据**长什么样、被谁引用**」，但没讲这些 `.ndm` 是**怎么从 `.db` + LEF 生成**的。下一步建议：

1. **u3-l2 创建 NDM 参考库**：阅读 `IC Compiler II/NDM_Creation.tcl` 和 `Memory_NDM_Generation.tcl`，看 `create_workspace` / `read_lef` / `read_db` / `check_workspace` / `commit_workspace` 如何把本讲的 `.db`、LEF、`.tf` 合成 `.ndm`——这是本讲 NDM 变量的上游。
2. **u3-l3 LEF 到 FRAM 的层映射**：阅读 `LEF2FRAM/lef_layer_tf_number_mapper.pl`，看 Perl 如何解析 `.tf` 的 `maskName/layerNumber` 与 LEF 的 `LAYER/TYPE`，生成层号映射——与本讲的 `.tf` 层概念直接衔接，并解释了 TLU+ `MAP_FILE` 之外另一套层号翻译需求。
3. 如果你更想先看「库数据加载之后工具做什么」，可以跳到 **u4-l1 ICC2 设计初始化与 MCMM 设置**，那里会把本讲的 `create_lib` / `read_parasitic_tech` / MCMM 角模式串成完整的 setup 阶段。
