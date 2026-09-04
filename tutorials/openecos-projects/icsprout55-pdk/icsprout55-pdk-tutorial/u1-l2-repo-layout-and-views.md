# u1-l2 仓库目录结构与多视图文件族

## 1. 本讲目标

上一讲（u1-l1）我们建立了两个关键认知：**PDK 不是软件，而是一族多视图数据文件**；ICS55 是 preview 状态的开源 55nm 工艺包。本讲我们打开仓库本身，回答三个问题：

1. ICS55 的目录树长什么样？IP/IO、IP/STD_cell、prtech 三大块各自装了什么？
2. 每个库下面的 `cdl`、`cell_list`、`doc`、`lef`、`liberty`、`verilog`、`gds` 七种目录，分别对应哪种 EDA 视图、被哪类工具消费？
3. 哪些文件真的在 git 仓库里，哪些必须执行 `make unzip` 从 GitHub Release 下载？这条「git 与大文件」的边界画在哪里？

学完本讲，你应该能徒手画出仓库目录树、说清每种视图的用途，并在任何一台新克隆的机器上快速判断「我手头的数据全不全」。

## 2. 前置知识

本讲默认你已读过 u1-l1。这里补充几个新概念：

- **标准单元库（Standard Cell Library）**：把反相器、与门、触发器等常用逻辑电路预先设计、验证好，做成「货架商品」。综合工具（如 yosys）把 RTL 映射成这些单元的组合。ICS55 的标准单元库家族叫 `H7C`。
- **阈值电压家族（HVT/LVT/RVT）**：同一套逻辑功能，用不同阈值电压（Threshold Voltage）的晶体管实现，得到三个「口味」：
  - **HVT**（High-VT）：阈值高、速度慢、漏电低，适合非关键路径；
  - **LVT**（Low-VT）：阈值低、速度快、漏电高，适合关键路径；
  - **RVT**（Regular-VT）：常规折中。
  同一个功能（如一位全加器）在三套库里各有一个版本，名字只有结尾不同（`ADDFX1H7H` / `ADDFX1H7L` / `ADDFX1H7R`）。详细的命名拆解是 u3-l1 的主题，本讲只需记住「三套库 = 三种阈值」。
- **IO 库 / pad（压焊盘）**：芯片内核是 1.2V 的薄氧化层晶体管，而封装引脚要耐 3.3V 并驱动板上电容，所以芯片边缘需要一圈专门的 IO 单元（pad）做电平转换和驱动，IO 库就是这些 pad 的集合。
- **视图（View）**：同一个单元在不同 EDA 阶段的「侧面」——时序模型（liberty）、物理抽象（LEF）、版图（GDS）、电路网表（CDL）、行为模型（Verilog）。u1-l1 已给出各视图对应的流程阶段，本讲看它们在磁盘上的落位。
- **git 与大文件的边界**：git 适合跟踪文本diff，对大二进制（GDS 动辄几十上百 MB）不友好。ICS55 的策略是：**git 仓库只跟踪文本视图 + 小文件，liberty（标准单元）与全部 GDS 通过 Makefile 从 GitHub Release 下载**。

## 3. 本讲源码地图

| 文件/目录 | 作用 |
| --- | --- |
| [README.md](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md) | 项目门面：用法、Contents 目录树、状态与许可 |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt` | HVT 库单元清单（748 行） |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt` | LVT 库单元清单（747 行） |
| `IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt` | RVT 库单元清单（747 行） |
| `IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt` | IO 库单元清单（23 行，全部单元） |
| `prtech/techLEF/N551P6M.lef` | 工艺 LEF（布线技术文件），本讲只看文件头 |
| `IP/STD_cell/.../ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v` | 标准单元 Verilog 仿真模型，本讲只看开头 |
| `IP/STD_cell/.../ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef` | 标准单元 LEF 抽象，本讲只数 MACRO 数量 |
| [Makefile](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile) | 大文件下载与解压逻辑（深入解析留给 u1-l3） |
| [.gitignore](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore) | 4 行规则画出 git 与下载文件的边界 |

> 约定：为节省篇幅，下文用 `...` 代替 `IP/STD_cell/ics55_LLSC_H7C_V1p10C100`。

## 4. 核心概念与源码讲解

### 4.1 模块一：目录结构——三大块怎么读

#### 4.1.1 概念说明

一个 PDK 仓库通常按「**库（library）→ 视图（view）**」两级组织：先把 IP 分成标准单元库、IO 库等逻辑块，每个库内部再用固定名字的子目录区分视图。这样任何 EDA 工具或脚本都能按约定路径找到所需文件——目录名本身就是接口。

ICS55 顶层分三大块：

1. **`IP/STD_cell/ics55_LLSC_H7C_V1p10C100/`** —— 标准单元库，一个版本目录（V1p10 = version 1.10，README 第 79 行注释 "standard cell library version 1.10"）下并列三套阈值库 `ics55_LLSC_H7CH/H7CL/H7CR`；
2. **`IP/IO/ICsprout_55LLULP1233_IO_251013/`** —— IO 库（pad 库），只有一套；
3. **`prtech/techLEF/`** —— 布线工艺文件（Place & Route technology），不属于任何单元库，描述的是「工艺本身」：金属层、过孔、布线网格。

`prtech` 是 "P&R tech" 的缩写。它放在 `IP/` 之外，正说明「工艺规则」与「单元数据」是两类东西：前者全局一份，后者每库一份。

#### 4.1.2 核心流程

阅读这个仓库的推荐顺序：

```text
README Contents 目录树（确认骨架）
        │
        ▼
prtech/techLEF          ← 先看工艺：有几层金属、什么布线网格（单元二精读）
        │
        ▼
IP/STD_cell/三套阈值库   ← 再看标准单元：cell_list → lef → verilog → cdl（单元三/五）
        │
        ▼
IP/IO/IO 库             ← 最后看 pad：cell_list → lef → liberty（单元四）
        │
        ▼
make unzip 补齐 liberty/gds（本讲 4.3 + u1-l3）
```

#### 4.1.3 源码精读

**（1）README 自带的目录树。** README 的 Contents 一节画出了完整骨架，这就是官方「地图」：

- [README.md:L63-L79](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L63-L79) —— 从 `## Contents` 开始，`IP/IO` 下挂 `ICsprout_55LLULP1233_IO_251013`（注释 "Specific IO library"），`IP/STD_cell` 下挂 `ics55_LLSC_H7C_V1p10C100`（注释 "55nm LLSC H7C standard cell library version 1.10"）。
- [README.md:L80-L106](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/README.md#L80-L106) —— 三套阈值库并列展开，每套都有同样的七个子目录：`cdl / cell_list / doc / gds / lef / liberty / verilog`，最右列注释 `HVT / LVT / RVT standard cells` 一锤定音；`prtech/techLEF` 收尾。

注意一个「README 树 vs 磁盘现实」的差异：树里画了 `liberty` 和 `gds` 目录，但**新克隆的仓库里这两类目录（标准单元的 liberty、所有 gds）并不存在**，需要 `make unzip` 生成——这正是 4.3 节的主题。

**（2）目录名解码表。** ICS55 的命名高度模式化，掌握规律后看文件名就能定位：

| 名字片段 | 含义 | 依据 |
| --- | --- | --- |
| `ics55` | ICS55 PDK 前缀 | 项目名 |
| `LLSC` | 标准单元库家族名（缩写全称未在 README 展开，**待确认**） | README L79 |
| `H7C` | 单元库家族代号 | README L79 |
| `V1p10C100` | 版本 1.10（`p`=point）；`C100` 含义**待确认** | README L79 注释 "version 1.10" |
| `H7CH` / `H7CL` / `H7CR` | HVT / LVT / RVT 三种阈值 | README L80/L88/L96 注释 |
| `55LLULP1233` | 55nm；`LLULP` 推断为低泄漏/超低功耗家族（**待确认**）；`1233` 对应核 1.2V + IO 3.3V 双电压 | 电压对与 liberty 文件名 `tt_1p2_3p3_25c` 中的 `1p2_3p3` 一致 |
| `IO_251013` | IO 库，推断为 2025-10-13 日期戳（**待确认**） | 命名习惯 |
| `N551P6M` | N55=55nm；`1P6M` 推断为 1 层多晶硅 + 6 层金属的行业惯用缩写（具体层叠在 u2-l1 验证） | tech LEF 文件名 |
| `3P3`、`1P6M1TM` | 3.3V IO 电压；`1TM` 推断为 1 层顶层厚金属（**待确认**） | IO LEF 文件名 |

> 解码表中标「待确认」的推断都是**基于文件名与数据交叉验证的合理猜测**，不是官方说明——这也是读 PDK 的日常：文件名是最先到手的元数据，先建立假设，再用数据验证。

**（3）用 git 看骨架，比 ls 更可靠。** `git ls-files` 列出全部 41 个被跟踪文件（本仓库 HEAD 恰好 41 个），三大块的落位一目了然：

- `prtech/techLEF/` 下只有两个文件：`N551P6M.lef` 与 `N551P6M_ecos.lef`；
- 每套标准单元库被跟踪 9 个文件：`cdl`、`cell_list`、`doc` 各 1，`lef` 3 个（普通版 / `_ant` 版 / `_ecos` 版），`verilog` 1 个；
- IO 库被跟踪 11 个文件：`cdl`、`cell_list`、`doc`、`verilog` 各 1，`lef` 2 个（普通版 / `_ecos` 版），`liberty` 6 个。

`_ecos` 后缀文件是 ECOS Team 为开源工具链适配的平行版本（补 RC 参数、电源引脚等），是本仓库最有特色的机制，单元二、三、六会逐一拆解，本讲只记文件命名规律：`<库名>_<变体>.lef`。

#### 4.1.4 代码实践

**实践：把 README 的目录树「实测」一遍。**

1. **实践目标**：不靠记忆，用命令确认三大块目录结构，并发现 README 树与磁盘的差异。
2. **操作步骤**：

   ```bash
   # ① 顶层三大块
   ls IP prtech
   # ② 标准单元：一个版本目录、三套阈值库
   ls IP/STD_cell IP/STD_cell/ics55_LLSC_H7C_V1p10C100
   # ③ 每套库的子目录（以 H7CH 为例，另两套应完全同构）
   ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH
   # ④ git 眼中的仓库（共 41 个文件，可数一数）
   git ls-files | wc -l
   ```

3. **需要观察的现象**：步骤 ③ 的输出里**没有** `liberty` 和 `gds`；三套阈值库的子目录完全一致；IO 库的输出里**有** `liberty`（但也没有 `gds`）。
4. **预期结果**（已在本仓库 HEAD `68d89ed` 上逐条核对）：
   - 步骤 ③ 输出 `cdl  cell_list  doc  lef  verilog`（5 个子目录）；
   - `IP/IO/ICsprout_55LLULP1233_IO_251013/` 输出 `cdl  cell_list  doc  lef  liberty  verilog`（6 个）；
   - `git ls-files | wc -l` 输出 `41`。
   - 若你已执行过 `make unzip`，步骤 ③ 会多出 `liberty`、`gds` 两个目录——它们来自下载解压，不在 git 内（见 4.3）。脚本本身的输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prtech/techLEF` 放在 `IP/` 目录外面，而不是像标准单元那样放在某个库目录里？

**答案**：tech LEF 描述的是**工艺本身**（金属层、过孔、布线网格、SITE），全芯片全局唯一，被所有库共享；而 `IP/` 下放的是「具体单元数据」，每套库一份。把它们分开，说明「工艺规则」与「单元实例」是两类数据、两个维护周期——换一套单元库不需要动 tech LEF，反之亦然。

**练习 2**：不看 README，如何用一条命令确认 `ics55_LLSC_H7CH` 和 `ics55_LLSC_H7CL` 两套库的目录结构是同构的？

**答案**：分别 `ls` 两个目录并对比输出，例如 `ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH` 与 `ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL`——两套都输出 `cdl cell_list doc lef verilog`。也可以 `diff <(ls ...) <(ls ...)` 一条命令完成（输出为空即同构）。

**练习 3**：`IP/STD_cell` 下为什么还有一层 `ics55_LLSC_H7C_V1p10C100` 版本目录，而不是让三套库直接放在 `STD_cell` 下？

**答案**：多套库同属一个**发布版本**（V1p10），版本目录把「同一版本的 H7CH/H7CL/H7CR 三套库」捆绑在一起。将来若发布 V1p11，可并存一个 `..._V1p11C100` 目录，工具脚本按版本目录整体切换，避免三套库版本错配。这也提示我们：**混用不同版本的三套阈值库是危险操作**。

### 4.2 模块二：视图文件族——七个目录各是什么

#### 4.2.1 概念说明

README 目录树里每个库下有七个固定名字的子目录。它们是同一些单元在 EDA 各阶段的「七视图」：

| 目录 | 内容 | 主要消费者 | 在流程中的位置 |
| --- | --- | --- | --- |
| `cell_list/` | 单元名清单，每行一个，纯文本 | 人、脚本、流程管理 | 全流程的「目录页」 |
| `liberty/` | 时序/功耗/面积模型（`.lib`，Liberty 格式） | 综合器（yosys 等）、静态时序分析 | 逻辑综合、STA |
| `lef/` | 物理抽象（`.lef`）：单元边界、引脚位置与层、布线障碍 | 布局布线器（OpenROAD 等） | 布局、布线 |
| `verilog/` | 仿真模型（`.v`）：门级功能 + 路径延迟 | 逻辑仿真器（iverilog 等） | 门级仿真、后仿 |
| `cdl/` | 晶体管级网表（`.cdl`）：MOS 管连接关系 | SPICE 类工具、LVS | 电路仿真、物理验证 |
| `gds/` | 版图数据库（`.gds`，二进制）：真实几何图形 | 版图工具（KLayout/Magic）、代工厂 | 流片交付 |
| `doc/` | 数据手册 PDF | 人 | 参考资料 |

`cell_list` 值得单独强调：它是**最轻量的视图**，一个库「有哪些单元」的权威清单，常被流程脚本用来做交叉核对（某单元在 liberty、LEF 里是否齐全）。它本身不含任何电气或几何信息，只是一份名单。

七种视图**必须描述同一批单元**——这是 PDK 数据一致性的核心要求，也是单元五一致性检查的出发点。

#### 4.2.2 核心流程

把视图放进数字后端流程（u1-l1 已给出阶段，这里落到具体文件）：

```text
RTL (.v)
  │  读取 liberty（时序模型）→ 工艺映射
  ▼
门级网表 (.v)  ←───────────── 综合器（yosys）
  │  读取 lef（物理抽象）+ techLEF（工艺规则）
  ▼
布局/布线 (DEF) ←──────────── 布线器（OpenROAD）
  │  读取 verilog 仿真模型（门级功能/延迟）
  ▼
门级仿真/后仿  ←───────────── 仿真器（iverilog）
  │  读取 gds（版图）+ cdl（晶体管网表）
  ▼
物理验证 & 流片 ←──────────── LVS/DRC、代工厂
```

`cell_list` 与 `doc` 不进入任何工具，服务于人和脚本，是流程的「旁路」元数据。

#### 4.2.3 源码精读

**（1）cell_list：三套标准单元库的名单。** 每行一个单元名，按字母序排列。HVT 库开头是全加器系列：

- [ics55_LLSC_H7CH.txt:L1-L6](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L1-L6) —— `ADDFX1H7H` 到 `ADDHX2H7H`：`ADDF`/`ADDH` 是全加器（和位/进位位），`X1/X1P4/X2` 是驱动强度，结尾 `H7H` 标记 HVT 库。
- [ics55_LLSC_H7CH.txt:L744-L748](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L744-L748) —— 文件以 `XOR3X*H7H` 系列收尾，全文件共 **748 行 = 748 个单元**。
- [ics55_LLSC_H7CL.txt:L1-L3](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/cell_list/ics55_LLSC_H7CL.txt#L1-L3) 与 [ics55_LLSC_H7CR.txt:L1-L3](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR/cell_list/ics55_LLSC_H7CR.txt#L1-L3) —— 同样从 `ADDFX1` 开始，只是结尾换成 `H7L` / `H7R`；两文件各 **747 行**。

三库清单除结尾后缀外几乎逐行对应（H7CH 比 H7CL/H7CR 多 1 个单元，具体差异属于 u3-l1 的统计任务）。名单里除了逻辑门，还有几类「物理辅助单元」：[L264-L267](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L264-L267) 的 `FILLCAP*`（填充/去耦电容单元）、[L704-L705](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt#L704-L705) 的 `TIEHI/TIELO`（常高/常低连接单元）——它们不实现逻辑功能，是版图完整性（填充间隙、电源去耦、悬空输入固定）的组成部分。

**（2）IO 库的 cell_list：23 行就是全部家当。** IO 库比标准单元库小两个数量级：

- [ICSIOA_N55_3P3.txt:L1-L23](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/ICSIOA_N55_3P3.txt#L1-L23) —— 前 14 行是功能与电源 pad：`CORNER`（拐角）、`CUT`（切割）、`PAR`/`PAR_5`（并联电阻保护）、`PBMUX`/`PWE`（功能 pad）、`VDD1/VDD1A/VDD3/VDDIO3`、`VSS1/VSS1A/VSS3/VSSIO3`（多组电源/地 pad）；后 9 行是 `FILLER50` 到 `FILLER0005` 共 8 种宽度的填充单元，用来把 pad 环的剩余间隙精确填满。pad 家族的详细职责在 u4-l1 展开。

**（3）techLEF：工艺视图的文件头。** `prtech/techLEF/N551P6M.lef` 开头 13 行是 Apache-2.0 license 头（u1-l1 讲过的逐文件声明，LEF 用 `#` 作注释符），随后进入正文：

- [N551P6M.lef:L15-L29](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/prtech/techLEF/N551P6M.lef#L15-L29) —— `VERSION 5.7` 声明 LEF 语法版本；`PROPERTYDEFINITIONS` 预定义若干 `LEF58_*` 扩展属性；`UNITS ... DATABASE MICRONS 1000` 规定坐标单位（1 数据库单位 = 1/1000 μm = 1nm）；`MANUFACTURINGGRID 0.001` 规定制造网格 1nm——所有版图坐标必须落在该网格上。层定义（`LAYER`）从第 30 行开始，属于单元二的内容。

**（4）verilog 视图：仿真模型长什么样。** 标准单元 Verilog 文件同样以 license 头开头（`/* ... */` 块注释，第 1–15 行），第一个模块就是天线二极管单元：

- [ics55_LLSC_H7CH.v:L17-L34](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/verilog/ics55_LLSC_H7CH.v#L17-L34) —— `` `timescale 1ns/1ps`` 与 `` `celldefine`` 是单元库模型的标准开场；`module ANT2H7H (A)` 只有一个 `inout` 端口，函数体为空，唯一的结构是 `` `ifdef functional`` 包裹的空 `specify` 块——ANT 单元没有逻辑行为，纯粹为天线效应检查存在。功能单元（与门、触发器）的建模方式（门原语 + specify 路径延迟）在 u3-l5 精读。

**（5）一个真实的「跨视图差异」：LEF 比 cell_list 多 37 个单元。** 用 `grep -c '^MACRO '` 统计，三个 LEF 变体（普通/`_ant`/`_ecos`）各含 **785 个 MACRO**，而 cell_list 只有 748 项，差 37 个。典型例子是天线二极管：

- [ics55_LLSC_H7CH.lef:L3048](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3048) `MACRO ANT2H7H` 与 [L3084](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/ics55_LLSC_H7CH.lef#L3084) `MACRO ANT4H7H` —— LEF 中有定义；
- 但在 cell_list 里 `grep -c '^ANT'` 的结果是 **0**（可自行验证）；
- 而 verilog 视图又给它建了模型（上文第（4）点）。

结论：**「cell_list = 全部单元」这个假设在本仓库不成立**，ANT 类物理修复单元在 LEF/verilog/cdl 中存在却未列入 cell_list（是否为有意的清单口径差异，**待确认**）。这正说明跨视图核对不能靠想当然——单元五会写脚本系统化地做这件事。对比之下，IO 库没有这个问题：IO LEF 两个变体各含 23 个 MACRO，与 cell_list 的 23 行严格一致。

#### 4.2.4 代码实践

**实践：亲手复算「cell_list 行数 vs LEF MACRO 数」。**

1. **实践目标**：用两条命令验证 4.2.3 第（5）点声称的数字，体会「视图间单元集合可能不一致」。
2. **操作步骤**：

   ```bash
   # ① 四个库的 cell_list 单元数
   wc -l IP/STD_cell/ics55_LLSC_H7C_V1p10C100/*/cell_list/*.txt \
         IP/IO/ICsprout_55LLULP1233_IO_251013/cell_list/*.txt

   # ② LEF 里的 MACRO 数（-c 表示只输出匹配行数）
   grep -c "^MACRO " IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/lef/*.lef \
                   IP/IO/ICsprout_55LLULP1233_IO_251013/lef/*.lef

   # ③ cell_list 里数 ANT 单元（预期为 0）
   grep -c "^ANT" IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/cell_list/ics55_LLSC_H7CH.txt
   ```

3. **需要观察的现象**：H7CH/H7CL/H7CR/IO 四个 cell_list 分别是 748/747/747/23 行；H7CH 三个 LEF 变体各 785 个 MACRO，IO 两个 LEF 变体各 23 个；步骤 ③ 输出 0。
4. **预期结果**：上述数字已在本仓库 HEAD `68d89ed` 上用等价命令核对（`wc -l` 与 `grep -c`），你的输出应与之相同；`grep -c` 在无匹配时返回退出码 1 属正常现象（输出仍为 `0`）。完整脚本的运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么综合器需要 liberty 而不能直接读 LEF？两者都描述了单元，差别在哪？

**答案**：liberty 描述**电气/时序行为**（延迟随输入斜率与负载电容的查找表、功耗、面积估计），综合器据它做时序驱动的工艺映射；LEF 描述**物理几何**（边界、引脚位置、布线障碍），对「这个门有多快」一无所知。信息维度不同：综合关心「快不快、省不省」，布线关心「放哪、怎么连」。

**练习 2**：`cell_list` 里一个单元名都没有额外信息，这 748 行纯文本有什么存在价值？至少说出两种用法。

**答案**：① 作**目录页/索引**：人和脚本快速回答「库里有没有 X 单元」；② 作**流程核对基准**：脚本遍历清单，逐个检查 liberty/LEF/verilog 是否都包含该单元（单元五的一致性检查正是这种用法）；③ 作**版本 diff 的最小载体**：比较两个版本的 cell_list 立刻知道单元增删。它的价值不在信息量，而在**权威性与机器可读性**。

**练习 3**：GDS 是二进制格式，为什么本讲一个字都没引用它的内容？

**答案**：因为 GDS 根本不在 git 仓库里——所有 `gds/` 目录都要 `make unzip` 下载（见 4.3）。这也解释了为什么 `doc/` 里的 PDF 数据手册是 git 中唯一的二进制大文件：视图数据的「文本优先」是仓库刻意维持的属性，便于 diff、grep 与代码评审。

### 4.3 模块三：git 跟踪与大文件下载的边界

#### 4.3.1 概念说明

本仓库 41 个被跟踪文件全是**文本或小体积文件**：tech LEF 两个（671/675 行）、标准单元每库的 LEF/CDL/verilog（几万行文本）、cell_list、Makefile 与文档；唯一的例外是 `doc/` 的 PDF（H7CH 数据手册约 20.9 MB，仍被 git 跟踪——说明边界的划分标准是「能否通过 Release 更好地分发」而非严格的体积阈值）。

两类数据被划到 git 之外，改由 GitHub Release 以 `tar.bz2` 包分发：

1. **标准单元库的 liberty**：三套库 × 多个工艺角，体积大且随版本频繁更新；
2. **全部 GDS**：二进制版图数据库，体积最大。

IO 库的 liberty 是反例佐证：它只有 6 个工艺角、每个约 51 KB（23 个 pad 的模型很小），所以**留在 git 内**。边界不是「liberty 一律不进 git」，而是「标准单元的大 liberty 不进 git」——这个精度值得注意，否则会误以为 `IP/IO/.../liberty/` 也需要下载。

#### 4.3.2 核心流程

`make unzip` 的完整流水线（Makefile 逻辑的深入拆解是 u1-l3 的任务，这里只看数据流向）：

```text
make unzip
  ├─ start        ：打印提示
  ├─ clean-dir    ：先删除旧的 STD_cell/**/liberty 与 IP/**/gds 目录（保证幂等）
  ├─ $(DECOMP_DIR) ：解压目标，逐个依赖对应的 tar.bz2
  │     │  若 tar.bz2 不存在 → 触发下载规则：
  │     │    curl 查 GitHub API 拿 browser_download_url
  │     │    → curl/wget 下载为 .part → 校验后改名（支持 PROXY_USE 代理加速）
  │     ▼
  │    模式规则：tar -xjvf 解压到对应库目录（liberty/ 或 gds/）
  └─ clean-bz2    ：删除所有 *.tar.bz2 压缩包
```

关键映射（模式规则展开，详见 u1-l3）：

```text
ics55_LLSC_H7CH_liberty.tar.bz2  →  IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH/liberty/
ics55_LLSC_H7CL_gds.tar.bz2      →  IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL/gds/
ICsprout_55LLULP1233_IO_gds.tar.bz2 → IP/IO/ICsprout_55LLULP1233_IO_251013/gds/
```

即：**下载产物精确落位到 README 目录树中「缺失」的那几个子目录**——README 画的树是「完整 PDK」的形状，git 仓库是它的「文本子集」。

#### 4.3.3 源码精读

**（1）Makefile：哪些包、解到哪。**

- [Makefile:L11-L20](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L11-L20) —— `RELEASE_FILE_LIB` 列出三套标准单元库的 liberty 包，`RELEASE_FILE_GDS` 列出三套标准单元 GDS 包加一个 IO GDS 包，共 **7 个 tar.bz2**。这就是「git 外数据」的完整清单。
- [Makefile:L22-L30](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L22-L30) —— `patsubst` 把包名映射成解压目标目录：`ics55_LLSC_H7CH_liberty.tar.bz2` 中的 `%` 捕获库名 `ics55_LLSC_H7CH`，拼出 `$(DECOMP_DIR_LIB_P)/ics55_LLSC_H7CH/liberty`；IO 的 GDS 则拼到 `IP/IO/ICsprout_55LLULP1233_IO_251013/gds`。
- [Makefile:L62-L66](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L62-L66) —— liberty 的模式规则：`mkdir -p` 建目录、`tar -xjvf` 解压、`touch $@` 标记目标已完成（`j` 表示 bzip2，这也是 README 要求先安装 `bzip2` 的原因）。[L68-L78](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L68-L78) 是 GDS 的两条同构规则。
- [Makefile:L92-L95](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/Makefile#L92-L95) —— `clean-dir` 用 `find` 删除 `IP/STD_cell` 下所有名为 `liberty` 的目录和 `IP` 下所有名为 `gds` 的目录。注意它**只清下载产物**，git 跟踪的文件一个都不动——下载内容与 git 内容的目录名约定使这条清理规则可以如此大胆。

**（2）.gitignore：边界的一句话版本。**

- [.gitignore:L1-L4](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/.gitignore#L1-L4) —— 四行规则：`/**/STD_cell/**/liberty/`（标准单元 liberty 目录）、`/**/gds/`（所有 GDS 目录，含 IO）、`*.tar.bz2`（下载的压缩包）、`*.mk`（ incidental 产物）。注意第一条只匹配 `STD_cell` 路径下的 liberty——所以 IO 的 liberty 目录不被忽略、能被 git 跟踪，与 4.3.1 的结论互为印证。

**（3）IO liberty 在 git 内的直接证据。**

- [ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib](https://github.com/openecos-projects/icsprout55-pdk/blob/68d89edb47847671e18f9e65d66c0cd883995e05/IP/IO/ICsprout_55LLULP1233_IO_251013/liberty/ICSIOA_N55_3P3_tt_1p2_3p3_25c.lib) 等 6 个 `.lib` 均在 `git ls-files` 输出中，覆盖 `tt`/`ff`/`ss` 三种工艺角 × 不同电压温度组合（如 `ff_1p32_3p63_125c` = 快角 / 核 1.32V / IO 3.63V / 125°C）。liberty 文件名的命名法属于 u3-l6。

#### 4.3.4 代码实践

**实践：用 git 三连问画出「跟踪 vs 下载」边界。**

1. **实践目标**：不依赖 README 与本讲义，仅用 git 命令判断每种视图是「git 内」还是「需下载」。
2. **操作步骤**：

   ```bash
   # ① 问：git 里有没有任何 gds 文件？（预期：无输出）
   git ls-files | grep -i gds

   # ② 问：git 里有几个 liberty 文件？都在哪个库？（预期：仅 IO 库 6 个）
   git ls-files | grep liberty

   # ③ 问：LEF、CDL、verilog、cell_list、doc 是否齐全？
   git ls-files | grep -E "\.(lef|cdl|v|txt|pdf)$" | wc -l

   # ④（可选，需网络）真的下载一次，再看磁盘变化
   make -n unzip          # -n 只打印不执行，先看会做什么
   ```

3. **需要观察的现象**：① 无任何输出；② 恰好 6 行，全部位于 `IP/IO/.../liberty/`；③ 输出一个数字，加上 Makefile/README/LICENSE 等非视图文件后应与「41 个跟踪文件」自洽；④ `make -n` 打印出 7 个包的下载与解压计划而不实际执行。
4. **预期结果**：①②③ 的结论已在本仓库 HEAD `68d89ed` 上核对（IO liberty 恰为 6 个文件：`tt_1p2_3p3_25c`、`ff_1p32_3p63_125c`、`ff_1p32_3p63_m40c`、`ff_1p32_3p63v_0c`、`ss_1p08_2p97_125c`、`ss_1p08_2p97_m40c`）。`make -n` 的实际输出**待本地验证**；执行 `make unzip` 需网络与 `bzip2`，无法联网时可跳过，不影响本讲后续学习（liberty 相关内容将在 u3-l6 用 IO 库自带的 6 个文件学习）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `/**/gds/` 用全局通配，而 liberty 只忽略 `STD_cell` 路径下的？

**答案**：因为**所有** GDS（含 IO 库）都走 Release 分发，没有任何例外，可以放心全局忽略；而 liberty 只有三套标准单元库的大文件需要分发，IO 库的 6 个小 `.lib` 留在 git 内更方便（克隆即得、diff 可见）。两条规则的「宽窄」精确反映了实际分发策略的差异。

**练习 2**：同事说：「我在新机器上克隆了仓库，直接跑综合成功了，所以这个 PDK 不需要 `make unzip`。」他可能对在哪里？

**答案**：如果他的综合目标是把 RTL 映射到标准单元，**必须**读标准单元 liberty——那不在 git 内，不执行 `make unzip` 就不可能成功。可能的解释：他其实用的是 IO 库的 liberty 或其他库；或者他曾在本机执行过下载、目录残留；或者他把「综合」与「仅语法解析/仿真」混淆了。判断依据正是本讲的边界：标准单元 liberty 与全部 gds 一律需要下载。

**练习 3**：`make unzip` 之前要先 `clean-dir` 删掉旧目录再解压，而不是直接覆盖，这样设计有什么好处？

**答案**：保证**幂等与干净**：若上次解压残留了旧版本文件（比如旧 Release 中存在、新 Release 已删除的文件），直接覆盖不会清掉它们，目录里会混入「幽灵文件」，工具读到的单元集合与本次 Release 不一致。先删后解压，确保目录内容严格等于本次下载包的内容——这对 PDK 这种「数据即真相」的仓库尤其重要。

## 5. 综合实践

**任务：生成 ICS55 的「视图 × 库」可用性矩阵。** 这是本讲的总实践，把三个模块（目录结构、视图族、git 边界）串成一个可复用的体检脚本。每次拿到新的克隆环境，跑一遍它，就知道手头数据能支撑哪些流程阶段。

1. **实践目标**：写一个脚本，完成两件事——统计四个库的 cell_list 单元数；对四个库 × 七种视图，输出「磁盘是否存在 / git 是否跟踪」的矩阵，并标注「需 make unzip」的格子。

2. **操作步骤**：

   先用 bash 快速版热身（示例代码，非项目自带）：

   ```bash
   # cell_list 单元数（grep -c . 统计非空行，兼容无结尾空行的文件）
   find IP -path "*/cell_list/*.txt" -exec sh -c 'printf "%-95s %s\n" "$1" "$(grep -c . "$1")"' _ {} \;

   # 各库现有子目录
   ls IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH
   ls IP/IO/ICsprout_55LLULP1233_IO_251013
   ```

   再用 Python 完整版（示例代码，保存为 `view_matrix.py`，在仓库根目录运行）：

   ```python
   #!/usr/bin/env python3
   """统计 ICS55 各库 cell_list 单元数，输出视图×库可用性矩阵。示例代码"""
   import os, subprocess

   LIBS = {  # 库显示名 → 库目录
       "H7CH": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CH",
       "H7CL": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CL",
       "H7CR": "IP/STD_cell/ics55_LLSC_H7C_V1p10C100/ics55_LLSC_H7CR",
       "IO":   "IP/IO/ICsprout_55LLULP1233_IO_251013",
   }
   VIEWS = ["cell_list", "lef", "cdl", "verilog", "doc", "liberty", "gds"]

   tracked = set(subprocess.check_output(["git", "ls-files"], text=True).splitlines())

   print("== cell_list 单元数 ==")
   for name, path in LIBS.items():
       cl = os.path.join(path, "cell_list")
       for fn in sorted(os.listdir(cl)):
           n = sum(1 for line in open(os.path.join(cl, fn)) if line.strip())
           print(f"{name:5s} {fn}: {n}")

   print("\n== 视图×库矩阵（磁盘/git） ==")
   print(f"{'view':10s}" + "".join(f"{n:^16s}" for n in LIBS))
   for view in VIEWS:
       row = f"{view:10s}"
       for name, path in LIBS.items():
           vpath = os.path.join(path, view)
           disk = "盘√" if os.path.isdir(vpath) else "盘×"
           git_ = "git√" if any(t.startswith(vpath + "/") for t in tracked) else "git×"
           note = "需下载" if disk == "盘×" and git_ == "git×" else ""
           row += f"{disk+git_+' '+note:^16s}"
       print(row)
   ```

3. **需要观察的现象**：单元数一节输出 748/747/747/23；矩阵中标准单元三库的 `liberty`、`gds` 行与 IO 库的 `gds` 行均为「盘× git× 需下载」；IO 库 `liberty` 为「盘√ git√」；其余视图均为「盘√ git√」。若你执行过 `make unzip`，`liberty`/`gds` 会变成「盘√ git×」——目录在磁盘上存在，但 git 从未跟踪。

4. **预期结果**（数值已用 `wc -l`、`grep -c`、`git ls-files`、`ls` 逐项独立核对；脚本整体输出**待本地验证**）：

   | 视图 | H7CH | H7CL | H7CR | IO |
   | --- | --- | --- | --- | --- |
   | cell_list | 盘√ git√（748 单元） | 盘√ git√（747） | 盘√ git√（747） | 盘√ git√（23） |
   | lef | 盘√ git√（3 个 LEF） | 盘√ git√（3 个） | 盘√ git√（3 个） | 盘√ git√（2 个） |
   | cdl | 盘√ git√ | 盘√ git√ | 盘√ git√ | 盘√ git√ |
   | verilog | 盘√ git√ | 盘√ git√ | 盘√ git√ | 盘√ git√ |
   | doc | 盘√ git√（PDF 数据手册） | 盘√ git√ | 盘√ git√ | 盘√ git√ |
   | liberty | 盘× 需下载 | 盘× 需下载 | 盘× 需下载 | 盘√ git√（6 工艺角） |
   | gds | 盘× 需下载 | 盘× 需下载 | 盘× 需下载 | 盘× 需下载 |

   这张矩阵直接翻译成流程能力清单：**不执行 `make unzip`，可以做**门级仿真（verilog）、布局布线（lef）、版图阅读（tech LEF）、以 IO 库为对象的综合实验（IO liberty 在 git 内）；**不能做**标准单元综合（缺标准单元 liberty）与任何版图交付（缺 gds）。

## 6. 本讲小结

- 仓库顶层三大块：`IP/STD_cell/ics55_LLSC_H7C_V1p10C100/`（三套阈值标准单元库 H7CH/H7CL/H7CR = HVT/LVT/RVT）、`IP/IO/ICsprout_55LLULP1233_IO_251013/`（23 个 pad）、`prtech/techLEF/`（全局唯一的工艺规则，独立于任何库）。
- 每个库内部用固定名字的七个子目录组织视图：`cell_list`（名单）、`liberty`（时序→综合）、`lef`（物理→布线）、`verilog`（行为→仿真）、`cdl`（晶体管→SPICE/LVS）、`gds`（版图→流片）、`doc`（数据手册）。
- 目录名本身是可解码的元数据：`H7CH/H7CL/H7CR` ↔ 阈值家族，`1233` ↔ 1.2V/3.3V 双电压，`1p2_3p3_25c` ↔ 工艺角/电压/温度；带 `_ecos` 后缀的文件是开源工具链适配变体。
- git 只跟踪 41 个文本/小文件；标准单元 liberty 与全部 gds 共 7 个 `tar.bz2` 包由 `make unzip` 从 GitHub Release 下载并解压回目录树；`.gitignore` 四行规则与这一边界严格对应（IO 的 6 个小 liberty 因体积小留在 git 内）。
- cell_list 不一定是全集：H7CH 的 LEF 有 785 个 MACRO，比 cell_list 的 748 项多 37 个（如 `ANT2H7H`/`ANT4H7H` 天线二极管在 LEF/verilog/cdl 中存在、却不在 cell_list 中）——跨视图一致性要靠脚本核对，不能想当然。

## 7. 下一步学习建议

- **u1-l3（Makefile 与大文件分发机制）**：本讲 4.3 只看了数据流向，下一讲逐行拆解 `patsubst` 模式规则、`RELEASE_TAG` 版本固定、`PROXY_USE`/`TOOL` 参数与 GitHub API 查询，有网络条件的话跟着执行一次 `make unzip`。
- **单元二（u2-l1 起）**：进入 `prtech/techLEF/N551P6M.lef` 正文，精读金属层栈、过孔与 SITE——本讲只读了它的前 29 行。
- **单元三（u3-l1 起）**：系统拆解标准单元命名语法与三套阈值库的功能覆盖差异（本讲遗留的问题：H7CH 比 H7CL/H7CR 多的那 1 个单元是谁）。
- 若你急于动手：先用 git 内的 IO liberty（6 个 `.lib`）+ yosys 做一个小实验也是可行的，但更平滑的路径还是按大纲顺序推进。
