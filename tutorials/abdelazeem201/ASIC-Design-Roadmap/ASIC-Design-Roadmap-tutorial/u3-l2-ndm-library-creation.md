# 创建 NDM 参考库

## 1. 本讲目标

上一讲（u3-l1）我们已经知道：ICC2 做物理设计时，要用一个统一的 **NDM** 文件来同时承载标准单元的「时序面」和「物理面」。本讲要回答的是——**这个 NDM 文件本身是怎么造出来的？**

读完本讲，你应当能够：

1. 说清楚 **workspace（工作区）** 和 **reference library（参考库）** 的区别与联系：一个是「施工现场」，一个是「交付成品」。
2. 掌握把 **LEF**（物理）与 **多 PVT 角的 `.db`**（时序）读进工作区、最终 `commit` 成 `.ndm` 的完整流程。
3. 理解 **site（放置格点）**、**布线方向**，以及**二极管 / 电源管脚标记**这些「物理属性标注」的作用。
4. 看懂 `check_workspace` / `commit_workspace` / `remove_workspace` 这套「检查→定稿→清理」的收尾动作。
5. 区分「标准单元建库」与「SRAM 宏单元建库」两套脚本的异同。

本讲只讲**如何生产 NDM**，不涉及用 NDM 跑 PnR（那是 U4 的内容），也不讲 `.db` 和 LEF 内部的文件格式（u3-l1 已介绍）。

## 2. 前置知识

在开始前，请确认你理解下面几个词（它们大多在 u3-l1 已建立）：

- **标准单元（standard cell）**：工艺厂提供的高度对齐、行为固定的逻辑门（与非门、触发器……），是数字 IC 的「乐高积木」。
- **`.db`（Liberty 编译库）**：描述单元**时序/功耗**的二进制文件，一个 PVT 角一份。PVT = Process（工艺偏差 ss/tt/ff）× Voltage（电压）× Temperature（温度），例如 `ss0p95v125c` 表示慢工艺、0.95V、125℃。
- **LEF（Library Exchange Format）**：描述单元**物理几何**——尺寸、引脚位置、遮挡区（OBS）——的文本文件。
- **`.tf`（technology file，技术文件）**：描述整块芯片的金属层栈、设计规则、site 定义等「工艺地基」。
- **NDM（New Data Model）**：Synopsys ICC2 的统一库格式，把上面几样东西按角（corner）合并成一个文件。
- **site（放置格点）**：版图上标准单元摆放的最小网格单元，单元高度通常是 site 高度的整数倍。
- **workspace（工作区）**：ICC2 库管理器（Library Manager）里临时开辟的「加工车间」，成品后再固化成 NDM。

一个直觉比喻：建 NDM 就像盖一栋楼——`.tf` 是**地皮与建筑规范**，`.db` 和 LEF 是**预制构件**，workspace 是**施工现场**，`commit_workspace` 是**验收并交付产证（.ndm）**，`remove_workspace` 是**拆除脚手架**。

## 3. 本讲源码地图

本讲围绕两个真实脚本展开，外加两个「消费端」文件作为对照：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| [NDM_Creation.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl) | 为**标准单元库**建 NDM（含多 PVT 角、site、布线方向、二极管标记） | 主线精读 |
| [Memory_NDM_Generation.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl) | 为**多个 SRAM 宏单元**批量建 NDM | 对比精读 |
| [Scripts/01_common_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) | 定义 `NDM_REFERENCE_LIB_DIRS`、`TECH_FILE` 等变量 | 看「成品被谁引用」 |
| [Scripts/03_PnR_setup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | 用 `create_lib -ref_libs` 把 NDM 挂进设计库 | 验证「生产→消费」闭环 |

> 提醒：两个建库脚本里的库名、层名（如 `xxx.tf`、`lib_name`、`MEM1`、`M0…M11`）都是**占位符**，是教学模板；真实值要替换成你工艺厂给的文件。这一点本讲会反复强调。

## 4. 核心概念与源码讲解

### 4.1 NDM workspace 与 reference library

#### 4.1.1 概念说明

ICC2 把库数据按「施工态」和「成品态」分成两个概念：

- **workspace（工作区）**：一个临时的、可读写的加工区域。你在这里 `read_ndm` / `read_lef` / `read_db` 把各种原料拼到一起，随时可以查、可以改。它**还不是**可以直接被 PnR 引用的库。
- **reference library（参考库 / `.ndm`）**：workspace 经过 `commit_workspace` 固化后的**只读成品**。它才是 PnR 在 `create_lib -ref_libs` 里真正挂载的对象。

为什么要分两层？因为生产一个 NDM 需要反复试错（漏了某个 `.db`、层方向设反了……），这些试错都发生在 workspace 这个「草稿纸」里；一旦 `commit`，产出的 `.ndm` 就被锁成不可变的交付件，保证下游 PnR 每次拿到的库内容一致、可复现。

#### 4.1.2 核心流程

建一个标准单元 NDM 的骨架是固定四步：

```
1. create_workspace  <名> -tech <.tf>          # 开工：圈一块地，登记技术文件
2. read_ndm / read_lef / read_db ...            # 进料：读物理数据 + 多角时序
3. （标注 site / 布线方向 / 二极管 / 电源管脚）   # 装修：补物理属性
4. check_workspace → commit_workspace → remove_workspace   # 验收交付 + 拆脚手架
```

在脚本里，开工这一步还伴随一大段 `lib.workspace.*` 应用选项，它们控制「多个库如何合并、如何命名、是否允许缺失物理视图」等行为，相当于施工前的「工艺参数设定」。

#### 4.1.3 源码精读

**开工：创建工作区并登记技术文件。** `create_workspace` 用 `-tech` 把工艺地基 `.tf` 绑进来；`-flow normal` 表示走标准建库流程。

[NDM_Creation.tcl:61-61](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L61) —— 以 `xxx.tf` 为技术文件开辟名为 `STD` 的工作区。

紧随其后的是一组 `lib.workspace.*` 选项，它们决定了 workspace 合并库时的策略（命名方式、是否允许缺物理视图等）。两个建库脚本的这段几乎一字不差，可以当作「建库通用头部」记忆：

[NDM_Creation.tcl:34-39](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L34-L39) —— `group_libs_naming_strategies` 列出多种命名策略、`allow_missing_related_pg_pins true` 允许缺电源管脚、`link.require_physical true` 要求链上的单元必须有物理视图。

**交付与清理：把工作区固化为 `.ndm`。**

[NDM_Creation.tcl:125-131](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L125-L131) —— `check_workspace -allow_missing` 自检，`commit_workspace -output "${lib_name}.ndm"` 输出成品，`remove_workspace` 释放工作区。

**对照消费端：产出的 `.ndm` 最终被谁用？** 在 `01_common_setup.tcl` 里，变量 `NDM_REFERENCE_LIB_DIRS` 指向两份成品 NDM（慢角 `ss0p95v125c` 与快角 `ff1p25v0c`）：

[01_common_setup.tcl:14-15](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L14-L15) —— 这正是本讲 `commit_workspace` 产出的那种 `.ndm` 文件的引用方。

而 PnR 真正「挂载」它们的命令是 `create_lib -ref_libs`：

[03_PnR_setup.tcl:27-27](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L27) —— `-ref_libs $NDM_REFERENCE_LIB_DIRS` 把本讲造出的 NDM 当作参考库挂进设计库。

> 闭环总结：本讲的 `commit_workspace`（**生产**）→ `NDM_REFERENCE_LIB_DIRS`（**登记**）→ `create_lib -ref_libs`（**消费**）。三者串起来就是 ICC2 库管理的完整链路。

#### 4.1.4 代码实践（源码阅读型）

**目标**：把「workspace → reference library」的转换关系看清。

**步骤**：

1. 打开 `NDM_Creation.tcl`，数一下从 `create_workspace`（L61）到 `commit_workspace`（L128）之间一共做了哪几类动作（读数据、设属性、标记单元……）。
2. 打开 `Memory_NDM_Generation.tcl`，找出与标准单元脚本**完全相同**的那段 `lib.workspace.*` 头部（对照 [Memory_NDM_Generation.tcl:44-58](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L44-L58)）。
3. 打开 `03_PnR_setup.tcl:27`，确认 `commit` 出来的 `.ndm` 通过哪个变量、哪条命令被 PnR 消费。

**需要观察的现象**：标准单元建库只产 1 个 NDM，而 Memory 脚本会在循环里产多个 NDM（每个宏单元一个）。

**预期结果**：你能用一句话讲清「workspace 是过程，`.ndm` 是结果，`create_lib -ref_libs` 是结果的使用者」。

#### 4.1.5 小练习与答案

**练习 1**：如果 `commit_workspace` 之后忘了 `remove_workspace`，会怎样？
**参考答案**：工作区仍占着库管理器的会话资源；下次再 `create_workspace` 同名空间时可能冲突或报「workspace already exists」。`remove_workspace` 是规范的收尾，释放临时空间。

**练习 2**：`-flow normal` 是什么意思？去掉它行不行？
**参考答案**：`-flow normal` 指定走标准（常规）建库流程（区别于某些特殊流程）。去掉后工具会按默认流程处理；脚本显式写出是为了行为可复现。具体可选项需对照所用 ICC2 版本的 Library Manager 手册——**待本地按版本核对**。

---

### 4.2 读入 LEF 与多 PVT `.db`

#### 4.2.1 概念说明

workspace 开好后要「进料」。进的是两类原料：

1. **物理数据**：标准单元脚本用 `read_ndm` 读一份预制的「物理专用 NDM」（只含版图/引脚，无时序）；SRAM 脚本则直接用 `read_lef` 读 LEF 文本。
2. **时序数据**：用 `read_db` 读 Liberty `.db`，**每个 PVT 角一份**。

关键概念是 **`process_label`（工艺标签）**：同一个单元在不同 PVT 角下延迟不同，因此一份 `.db` 只代表一个角。`read_db` 用 `-process_label <名>` 给这份时序数据打上「角标签」，NDM 内部就能把同一单元的多份时序视图区分开。下游 MCMM 设置（见 u3-l1）正是靠这个标签，把 slow 角绑定到某份视图、fast 角绑定到另一份。

> 为什么标准单元用 `read_ndm` 而宏单元用 `read_lef`？标准单元量大、物理视图常由工艺厂以物理专用 NDM 形式提供；SRAM 宏单元则更常见以 LEF + 多份 `.db` 形式交付。脚本只是反映了这两种真实交付习惯。

#### 4.2.2 核心流程

**标准单元**（`NDM_Creation.tcl`）：

```
read_ndm  _physicalonly.ndm              # 一次性读入物理专用 NDM
foreach pvt {pvt1 pvt2} {
    db = glob  db/${lib}${pvt}_ccs.db    # 按角名拼 .db 文件名
    if 文件存在:
        read_db db -process_label $pvt   # 读入并打角标签
    else:
        打印警告
}
```

**SRAM 宏单元**（`Memory_NDM_Generation.tcl`，对每个宏循环）：

```
foreach mem in MEM_LIST:
    create_workspace $mem -tech ...
    read_lef  SRAM/LEF/${mem}.lef         # 直接读 LEF
    foreach 模式 in {ssgnp0p72vm40c.db ...}:   # 5 个 PVT 角的 glob 模式
        收集匹配的 .db 文件
    foreach db in 收集到的文件:
        从文件名拆出角名作为 label        # 文件名格式: MEM_<view>_<label>.db
        read_db db -process_label $label
    check → commit → remove
```

注意 Memory 脚本是从**文件名**反解出角名当 label；而标准单元脚本是直接用循环变量 `pvt` 当 label。两种取标签的方式都合法，体现了「标签只是个名字，只要下游 MCMM 对得上即可」。

#### 4.2.3 源码精读

**标准单元：读物理专用 NDM。**

[NDM_Creation.tcl:64-64](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L64) —— `read_ndm $physical_ndm` 读入 `_physicalonly.ndm`（脚本里该变量见 L14）。

**标准单元：按 PVT 角循环读 `.db`，带 `process_label`。**

[NDM_Creation.tcl:69-76](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L69-L76) —— `glob` 按角名拼出 `.db` 路径，`file exists` 判存在，`read_db ... -process_label $pvt` 读入并打标签；缺失则打印警告但不中断（配合 L27 的 `sh_continue_on_error true`）。

> 这里的 `{pvt1 pvt2}` 和 `${lib_name}${pvt}_ccs.db` 都是占位符。真实使用时，比如 Nangate 45nm，会写成具体的角名（如 `ss0p95v125c`）和真实库名前缀。

**SRAM 宏单元：直接读 LEF，缺失则跳过该宏。**

[Memory_NDM_Generation.tcl:71-78](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L71-L78) —— 先判 `file exists`，不存在就 `continue` 跳到下一个宏，存在才 `read_lef`。

**SRAM 宏单元：用 glob 收集 5 个 PVT 角的 `.db`。** 这段展示了「一种角一个 glob 模式」的批量收集写法，`-nocomplain` 保证没匹配到也不报错：

[Memory_NDM_Generation.tcl:81-92](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L81-L92) —— 对 5 种角名模式逐一 `glob`，命中则 `lappend` 进 `db_files` 列表。

**SRAM 宏单元：从文件名反解角名作 `process_label`。**

[Memory_NDM_Generation.tcl:95-107](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L95-L107) —— 注释说明文件名格式为 `MEMNAME_<view>_<label>.db`；用 `split $db_name "_"` 切片，取第 3 段（下标 2）作 label，再去掉 `.db` 后缀；切片不足 3 段则跳过（容错）。

> 这里有个细节值得品味：`lindex $tokens 2` 取的是第 3 个下划线段。若你的 SRAM `.db` 命名不是「名字\_视图\_角」三段式，这段解析会落到 `else` 分支被跳过——这也是脚本里特意做容错的原因。

#### 4.2.4 代码实践（源码阅读型）

**目标**：理解 `process_label` 如何把「角」和「时序视图」绑在一起。

**步骤**：

1. 在 `NDM_Creation.tcl` 的 `foreach pvt {pvt1 pvt2}` 循环里，把 `pvt1`/`pvt2` 想象成真实的 `ss0p95v125c` / `ff1p25v0c`，写出对应的 `.db` 文件名会是什么。
2. 在 `Memory_NDM_Generation.tcl` 里，对一个名为 `MEM1` 的宏，假设 LEF 目录下有文件 `MEM1_view_ssgnp0p72vm40c.db`，手动走一遍 `split` / `lindex` / 去 `.db` 的过程，确认解析出的 label 是 `ssgnp0p72vm40c`。
3. 回顾 u3-l1 的 MCMM：slow 角与 fast 角分别要拿到哪份带标签的视图？

**需要观察的现象**：两份 `.db` 读进同一个 workspace 后，单元并没有重复——而是同一个单元上挂了两个时序视图（view），靠 label 区分。

**预期结果**：你能解释「`process_label` 是 NDM 内部多角时序视图的索引键」。

#### 4.2.5 小练习与答案

**练习 1**：标准单元脚本里，如果某个角的 `.db` 文件丢失，脚本会崩溃吗？
**参考答案**：不会。`file exists` 判断 + `sh_continue_on_error true`，缺失时只打印 `Warning: Missing DB file ...` 然后继续。代价是该角没有时序视图，下游 MCMM 若引用它会出问题。

**练习 2**：Memory 脚本为什么用 `glob -nocomplain` 而不直接写死文件名？
**参考答案**：不同 SRAM 可能只提供部分角的 `.db`（并非每个宏都有全部 5 个角）。`glob -nocomplain` 让「有哪个读哪个」，缺角不报错；收集到的列表再逐个 `read_db`，灵活适配不同宏的交付完整度。

---

### 4.3 site、布线方向与二极管 / 电源管脚标记

#### 4.3.1 概念说明

读入数据之后，还要给 workspace 补几项「物理属性」，否则 PnR 没法正确摆放和布线：

- **site（放置格点）**：标准单元必须摆在对齐的格点上。`unit` 是工艺文件里定义的一个 site 名；把它设为默认 site、并声明对称性，工具才知道单元能否镜像翻转。
- **布线方向（routing direction）**：每层金属有首选走向（水平 horizontal / 垂直 vertical）。相邻金属层正交（一横一竖）是 ASIC 布线的基本约定，这样上下层信号才能用通孔（via）交叉互连而不短路。
- **电源管脚标记**：某些特殊单元（如 well-tap / 衬底接触单元）带有 `VPP`/`VBB` 等体偏置电源脚，要把它们标成 `power` / `primary`，工具才会正确建电源网络。
- **二极管标记**：名字里含 `ANTENNA` 的单元是「天线二极管」单元，标记后布线器可在长金属上自动插入它们，泄放制造工艺中的电荷（防天线效应，详见 u9-l1）。

#### 4.3.2 核心流程

```
# site
set_attribute [get_site_defs unit] is_default true      # unit 作为默认放置格点
set_attribute [get_site_defs unit] symmetry Y           # 允许绕 Y 轴（左右）镜像

# 布线方向：把金属层分成「垂直组」和「水平组」分别赋值
set_attribute [get_layers {垂直组}]  routing_direction vertical
set_attribute [get_layers {水平组}]  routing_direction horizontal

# 电源管脚：匹配 TAP 单元上的 VPP 脚，标为 power/primary
foreach pattern {...VPP...} {
    set_attribute [get_lib_pins $pattern] port_type power
    set_attribute [get_lib_pins $pattern] pg_type   primary
}

# 二极管：匹配 ANTENNA 单元，标记 is_diode / is_diode_cell
set diode_cells [get_object_name [get_lib_cells */ANTENNA*]]
foreach diode $diode_cells { ... 标记 ... }
```

> 关于 `symmetry Y`：site 的对称性决定工具能给单元做哪些翻转。`symmetry Y` 通常表示可关于 Y 轴翻转（即左右镜像），从而支持 `FN`/`FS` 等单元朝向变体，提高布局密度。具体语义以工艺厂 LEF/site 定义为准——**待本地核对**。

#### 4.3.3 源码精读

**site 默认值与对称性。**

[NDM_Creation.tcl:83-84](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L83-L84) —— 把 `unit` site 设为默认、声明 Y 对称。

**布线方向分组赋值。** 注意这里用的是模板占位层名 `M0…M11`、`AP`，与 `common_setup.tcl` 里的真实 FreePDK45 层名（`metal1…metal10`）不同——这再次说明 `NDM_Creation.tcl` 是通用模板：

[NDM_Creation.tcl:91-96](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L91-L96) —— 定义两组层名，分别批量赋 `routing_direction vertical / horizontal`。

> 对照 [01_common_setup.tcl:24-24](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L24) 里的 `ROUTING_LAYER_DIRECTION_OFFSET_LIST`：真实 FreePDK45 是 `metal1 横、metal2 竖、metal3 横……` 严格正交交替。建库脚本里设的方向必须与 `.tf` / 消费端一致，否则 PnR 布线会乱。

**电源管脚标记（VPP）。**

[NDM_Creation.tcl:103-106](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L103-L106) —— 两个匹配模式命中 TAP 单元上的 `VPP` 脚，`-o`（override）强制覆盖，把 `port_type` 设为 `power`、`pg_type` 设为 `primary`。

**二极管单元标记。**

[NDM_Creation.tcl:113-118](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L113-L118) —— 先 `get_lib_cells */ANTENNA*` 拿到所有天线二极管单元名；逐个把其 `I` 脚标 `is_diode true`、整个单元标 `is_diode_cell true`。`-q`（quiet）让匹配不到时不报错。

#### 4.3.4 代码实践（源码阅读型）

**目标**：弄清「层方向正交」与「二极管标记」的工程意义。

**步骤**：

1. 把 [NDM_Creation.tcl:91-92](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L91-L92) 的两组层名抄下来，逐层标注每个 `Mx` 是横还是竖，检查相邻层是否正交。
2. 对照 `01_common_setup.tcl:24` 的真实层方向列表，体会模板与真实 PDK 的差异。
3. 读 [NDM_Creation.tcl:113-118](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L113-L118)，回答：被标记为 `is_diode_cell` 的单元，在后续流程（u9-l1 天线修复）里会被怎么用？

**需要观察的现象**：模板里的层名（M0/M11/AP）与真实 FreePDK45（metal1…metal10）对不上——这正是你需要替换的地方。

**预期结果**：你能说出「布线方向必须和 `.tf` 一致、二极管单元靠名字匹配并被标记以供天线修复」。

**待本地验证**：模板中的 `M0…M11` 分组是否符合某个具体工艺的真实层栈，需用真实 `.tf` 核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `get_lib_pins` 那两行要加 `-o`？
**参考答案**：`-o` = override，强制覆盖已存在的属性值。VPP 脚可能原本被识别成普通信号脚，不加 `-o` 时若属性已存在工具会拒绝覆盖，导致电源脚没被正确分类。

**练习 2**：把 `symmetry Y` 删掉会对布局有什么影响？
**参考答案**：工具不再允许该 site 上的单元做对应镜像翻转，可用的单元朝向变体减少，布局密度和合法性可能下降。symmetry 是为支持镜像摆放而设。

---

### 4.4 check / commit workspace

#### 4.4.1 概念说明

进料和标注都做完后，进入收尾三连：

- **`check_workspace`**：自检工作区的一致性——单元是否同时具备所需视图、库之间是否冲突等。`-allow_missing` 表示「允许缺少部分视图」（例如某些单元只有物理没有时序，或反之），把硬错误降级为可继续。
- **`commit_workspace -output <file>.ndm`**：把工作区固化为只读成品 NDM，写入指定路径。
- **`remove_workspace`**：清理工作区，释放会话资源。

这套「检查→交付→拆场」是 ICC2 建库的固定范式，标准单元和宏单元脚本都用它，只是宏单元把它放在 `foreach` 循环里，每个宏做一遍。

#### 4.4.2 核心流程

```
check_workspace  -allow_missing                    # 自检（允许缺视图）
commit_workspace -output  ${lib_name}.ndm          # 固化成品
remove_workspace                                  # 清理
```

宏单元版本（循环内）：

```
check_workspace  -allow_missing
commit_workspace -output  ./ndm/${mem}.ndm         # 每个宏一个 NDM
remove_workspace
```

#### 4.4.3 源码精读

**标准单元：收尾三连。**

[NDM_Creation.tcl:125-131](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L125-L131) —— `check_workspace -allow_missing` → `commit_workspace -output "${lib_name}.ndm"` → `remove_workspace`。

**SRAM 宏单元：循环内同样的收尾。**

[Memory_NDM_Generation.tcl:110-112](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L110-L112) —— 每个宏 `check` → `commit` 到 `./ndm/${mem}.ndm` → `remove`，紧接着打印 `✅ Finished: $mem`。

> 注意输出路径差异：标准单元把 `.ndm` 直接写在当前目录（`${lib_name}.ndm`），宏单元集中写到 `./ndm/` 子目录（`${mem_ndm_dir}/${mem}.ndm`，变量见 [Memory_NDM_Generation.tcl:31-31](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Memory_NDM_Generation.tcl#L31)）。批量产物收进专门目录是好习惯。

#### 4.4.4 代码实践（源码阅读型）

**目标**：理解 `check_workspace -allow_missing` 的「宽容」从何而来。

**步骤**：

1. 读 [NDM_Creation.tcl:125-125](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/NDM_Creation.tcl#L125)，结合 4.2 节「某个角 `.db` 缺失只打警告」的事实，思考：缺了角的 `.db` 之后，某些单元可能没有该角的时序视图，`check_workspace` 为什么还要 `-allow_missing`？
2. 在 `Memory_NDM_Generation.tcl` 的循环里，确认每个宏都独立经历一次 check/commit/remove——为什么不能把三个宏的数据塞进同一个 workspace？
3. 设想：如果 `check_workspace` 不加 `-allow_missing`，对「只有物理没有时序」的物理专用单元会怎样？

**需要观察的现象**：`-allow_missing` 让「物理专用单元」「角不全的单元」也能进 NDM，否则会被卡在检查这一步。

**预期结果**：你能解释「`-allow_missing` 是为兼容物理专用 / 角不全的库而设的宽容开关，但缺角时序问题要到下游 MCMM 才暴露」。

**待本地验证**：`check_workspace` 在你的 ICC2 版本上对「缺时序视图」具体报什么、是否真的能过，需在真实库上验证。

#### 4.4.5 小练习与答案

**练习 1**：标准单元脚本的 `commit` 输出在当前目录，宏单元脚本的 `commit` 输出到 `./ndm/`。哪种更好？为什么？
**参考答案**：宏单元的写法更好。批量产物（几十个宏各一个 `.ndm`）集中到一个子目录，便于管理和被 `NDM_REFERENCE_LIB_DIRS` 统一引用；散落在当前目录会和其它文件混杂。

**练习 2**：`commit_workspace` 之后再 `remove_workspace`，已经写出的 `.ndm` 会受影响吗？
**参考答案**：不会。`commit` 已把成品写到磁盘，`remove_workspace` 只清理内存中的工作区会话，不删除已交付的 `.ndm` 文件。

---

## 5. 综合实践

**任务**：模仿 `NDM_Creation.tcl`，为一个**新的虚拟标准单元库**改写配置，并说明需要准备哪些输入文件。

假设你要建一个名为 `my_std_cell` 的库，工艺文件是 `mytech.tf`，有 3 个 PVT 角：`ss_0p9v_125c`、`tt_0p9v_25c`、`ff_1p1v_m40c`。

**第 1 步：列出你需要准备的输入文件（写到一张清单里）。**

- 技术文件：`mytech.tf`
- 物理专用 NDM：`my_std_cell_physicalonly.ndm`（物理视图来源；如果没有，则需要各单元的 LEF，并改用 `read_lef`）
- 时序 `.db`（3 个角，CCS 库）：
  - `db/my_std_cellss_0p9v_125c_ccs.db`
  - `db/my_std_celltt_0p9v_25c_ccs.db`
  - `db/my_std_cellff_1p1v_m40c_ccs.db`

**第 2 步：改写 `NDM_Creation.tcl` 的配置段（示例代码，非项目原有）。**

```tcl
# —— 示例代码：基于 NDM_Creation.tcl 改写的配置段 ——
set tech_file     "mytech.tf"
set physical_ndm  "my_std_cell_physicalonly.ndm"
set lef_dir       "LEF"
set db_dir        "db"
set lib_name      "my_std_cell"
set bus_delimiter {[]}
set sh_continue_on_error true

# ...（保留原有 lib.workspace.* / lib.logic_model.* / lib.physical_model.* 选项段...）

create_workspace STD -tech $tech_file -flow normal
read_ndm $physical_ndm

# 三个 PVT 角
foreach pvt {ss_0p9v_125c tt_0p9v_25c ff_1p1v_m40c} {
    set db_file [glob $db_dir/${lib_name}${pvt}_ccs.db]
    if {[file exists $db_file]} {
        read_db $db_file -process_label $pvt
    } else {
        puts "Warning: Missing DB file for $pvt: $db_file"
    }
}

# ...（保留 site / 布线方向 / 电源脚 / 二极管 段，按真实 .tf 的层名替换 M0..M11...）

check_workspace -allow_missing
commit_workspace -output "${lib_name}.ndm"
remove_workspace
```

**第 3 步：自查。**

1. 你改的 `lib_name` 是否在 `read_db` 的 `glob` 路径、`commit_workspace` 的输出里都同步更新了？
2. 三个 `.db` 文件名能否被 `${lib_name}${pvt}_ccs.db` 这个模板正确拼出？（注意库名与角名之间没有分隔符，与你工艺厂的命名约定是否一致——**待按真实命名核对**）
3. 布线方向那段用的还是模板层名 `M0…M11` 吗？要不要换成你 `mytech.tf` 里真实的层名？

**预期结果**：你能交出一份针对 `my_std_cell` 的建库脚本骨架，并讲清每个输入文件对应脚本里的哪条命令、缺了会怎样。

> 说明：本实践是「源码阅读 + 配置改写」型，不要求真的跑通 ICC2（那需要授权工具与真实库数据）。重点是让你把「输入文件 ↔ 脚本变量 ↔ 命令」三者的对应关系理顺。

## 6. 本讲小结

- **workspace 是过程，`.ndm` 是成品**：`create_workspace` 开工、`commit_workspace` 固化、`remove_workspace` 收尾；产出的 `.ndm` 经 `NDM_REFERENCE_LIB_DIRS` 登记、被 `create_lib -ref_libs` 消费。
- **两类原料**：物理数据（标准单元用 `read_ndm` 读物理专用 NDM；SRAM 宏用 `read_lef` 读 LEF）+ 多 PVT 角 `.db`（`read_db -process_label`，标签是 NDM 内多角时序视图的索引键）。
- **`process_label` 的两种取法**：标准单元脚本直接用循环变量当标签；SRAM 脚本从文件名 `split "_"` 反解第 3 段当标签——都合法，只要和下游 MCMM 对得上。
- **物理属性标注**：`unit` site 设默认 + 对称性、金属层分水平/垂直组设布线方向、VPP 脚标 power/primary、ANTENNA 单元标 `is_diode_cell`——这些让 PnR 能正确摆放、布线、建电源网、修天线。
- **两脚本是模板**：库名 / 层名（`xxx.tf`、`lib_name`、`M0…M11`）全是占位符，真实值要按你的 PDK 替换，且布线方向必须与 `.tf` 及消费端一致。
- **标准单元 vs 宏单元建库的差异**：一个产单个 NDM、一个在循环里产多个 NDM 并集中写到 `./ndm/`；收尾三连（check/commit/remove）两者通用。

## 7. 下一步学习建议

- **横向补充**：读 `LEF2FRAM/lef_layer_tf_number_mapper.pl`（u3-l3 会精讲），看 LEF 的层名如何映射成 Milkyway/FRAM 的层号——它处理的是另一套（旧 ICC Milkyway）物理库准备流程，与本讲的 NDM 流程对照着看，能加深对「物理数据格式」的理解。
- **纵向深入**：进入 U4——`create_lib -ref_libs` 把本讲产出的 NDM 挂进设计库后，ICC2 的 PnR 主流程（setup → floorplan → … → finishing）才真正开始。建议先读 u4-l1「ICC2 设计初始化与 MCMM 设置」，看 `process_label` 在 `02_mcmm_setup.tcl` 里如何被角（corner）引用。
- **回头验证**：等你有真实库数据时，按本讲综合实践改写脚本、在 Library Manager 里实际 `commit` 一次，对照 `check_workspace` 的报告体会 `-allow_missing` 的作用。
