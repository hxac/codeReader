# 收尾与输出：finishing 阶段与 RTL→GDSII 的最后交付

## 1. 本讲目标

本讲是 U4「ICC2 物理设计主流程」的最后一讲。布线（u4-l6）结束后，芯片在逻辑和物理上已经「成型」，但还不能直接交给代工厂（foundry）。`route_opt` 跑完的那一刻，单元之间还留着缝隙、刚加进去的电源连接还没校验、下游工具（PrimeTime 做 STA、Calibre 做 LVS、代工厂读 GDSII）各自需要不同格式的交付文件。**收尾（finishing）阶段**就是把这些「最后一公里」全部做完。

学完本讲，你应当能够：

- 说清**填充单元（filler cell）**为什么必须插、插在哪里、为什么本仓库脚本里插了两次。
- 理解 **`connect_pg_net`** 的「自动连接」与「指定 net + 引脚集合」两种写法，以及 filler 插入后为什么必须**再连一次**电源。
- 理解 **`check_lvs`** 在 ICC2 内部做了什么、它和外部 LVS 工具的关系。
- 解释为什么收尾时一次 `write_verilog` 不够，要输出 **PG / 非 PG / LVS / 专用 PT** 多种 Verilog 视图。
- 看懂 **SPEF / DEF / GDSII** 等交付物分别由哪条 `write_*` 命令产生、各自喂给谁。

> 一个贯穿全讲的提醒：本讲的指定主源是 [`IC Compiler II/PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl)，它是一个**极简模板**，省略了 `check_lvs`、`write_sdc`、`write_def`、`.lvs.v` 等「完整流程该有但模板没写」的步骤。前序讲义（u4-l1）已确立：遇到 `PnR.tcl` 看不全的地方，用官方完整模板 [`IC Compiler II/Scripts/03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl)（Synopsys P-2020.03-SP4，~537 行，分成 8 个编号阶段）做对照。本讲会**如实地**把两个文件都摆出来——凡 `PnR.tcl` 没有的，我会明确指出它在完整模板的哪一行。

## 2. 前置知识

在进入收尾之前，请确认你已经具备以下认知（本讲会直接复用，不再重复讲解）：

- **标准单元（standard cell）**：来自库的、固定高度的逻辑单元（反相器、寄存器、buffer……）。每个单元都带 VDD/VSS 两个电源管脚，且底部有一条横跨单元宽度的 M1 电源轨。详见 u3-l1。
- **PG 网络（power/ground network）**：VDD/VSS 这两张全局电源网。它在 floorplan 阶段用 `create_net` 建立、用 `connect_pg_net` 把每个单元的电源管脚挂上去、再用金属（mesh/ring/rail）穿出来。详见 u4-l3。
- **block 版本管理**：ICC2 用 `save_block -as <名字>` 给设计存「快照」，可随时 `open_block` 回退。收尾阶段会把最终版另存为 `${TOP_DESIGN}_Final`，GDS 就从这一份写出来。详见 u4-l1。
- **STA 与签核**：布线完成后要用 PrimeTime 基于 SPEF（寄生文件）做静态时序签核。详见 u6-l1（后续讲义）。
- **LVS（Layout Versus Schematic）**：把「物理版图抽出的网表」与「逻辑网表」做对比，确认两者器件与连线完全一致——这是流片前的一道关键校验。本讲会用到这个概念。

一个最直观的比喻：布线结束后的芯片，就像盖完一栋楼、但还没装吊顶、没接总水表、没做竣工验收、也没画竣工图。收尾阶段就是「装吊顶（filler）→ 接表（PG）→ 验收（LVS/check）→ 出竣工图（write_*）」。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注 |
|------|------|----------|
| [`IC Compiler II/PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) | **指定主源**，极简模板（~210 行） | 178-210 行：收尾 filler、PG 重连、报告、`save_block Final`、`change_names`、SPEF、两份 Verilog、GDS。另对照 146-153 行的「布局后 filler」。 |
| [`IC Compiler II/Scripts/03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) | 官方完整模板（对照用） | §7 Finishing（478-494）：含 `check_lvs`；§8 Checks and Outputs（497-529）：四份 Verilog 视图、三份 SDC、SPEF、DEF、GDS。 |
| [`IC Compiler II/Scripts/01_common_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) | 变量定义 | `GDS_MAP_FILE`、`STD_CELL_GDS`、`NDM_POWER_NET`、`NDM_GROUND_NET` 等收尾用到的变量源头。 |

## 4. 核心概念与源码讲解

### 4.1 填充单元（filler cell）

#### 4.1.1 概念说明

布线结束后，每行标准单元之间会留下许多「缝隙」（gap）——因为逻辑单元宽度各异，摆好后不可能严丝合缝。这些缝隙看似无害，其实会引发三类问题：

1. **阱连续性断裂（well continuity）**：标准单元的衬底/阱（N-well、P-well）靠相邻单元拼接成连续的 implant 区。中间留缝，阱就断了，制造时会报 DRC（设计规则检查）错误。
2. **电源轨中断**：每个单元底部那条 M1 电源轨（VDD/VSS）是靠邻接单元首尾相接铺满整行的。有缝，轨道就断，远端单元可能供电不上。
3. **寄生/时序失真**：缝隙意味着金属覆盖不连续，寄生提取会失准。

**填充单元（filler cell）**就是用来填这些缝的「没有逻辑功能」的占位单元。它内部没有晶体管逻辑，只有电源轨和阱结构。它的「价值」全在物理：补全阱、接通电源轨。部分 filler 同时是 **decap（去耦电容）单元**，顺带给电源网络提供一点电容，压低 IR drop 瞬态毛刺。

> 关键直觉：filler 不是「装饰」，它是「物理完整性」的必需品。漏插 filler，几乎一定过不了 DRC/LVS。

#### 4.1.2 核心流程

收尾插 filler 的标准动作是「**插 → 连电 → 查冲突**」三步：

```
create_stdcell_filler / create_stdcell_fillers   # 1. 按缝隙宽度选 filler 组合填进去
connect_pg_net ...                               # 2. 给新 filler 的 VDD/VSS 引脚连电
remove_stdcell_fillers_with_violation            # 3. 把引发 DRC 冲突的 filler 拆掉
check_legality                                   # 4. 复查合法化
```

工具会根据每条缝隙的宽度，从 filler 库（如 FILL1、FILL2、FILL4……宽度成倍）里自动挑出合适的组合把缝填满——就像用不同长度的砖块拼满一段墙。

**一个容易忽略的细节：本仓库的 `PnR.tcl` 里 filler 被插了两次。** 第一次在布局（`place_opt`）之后、CTS 之前；第二次在布线（`route_opt`）之后。理解这两次的目的差异，是本小节的重点。

#### 4.1.3 源码精读

**布线后的最终 filler 插入**（本讲真正关心的那次）：

[IC Compiler II/PnR.tcl:178-184](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L178-L184) —— 收尾 filler：用通配符 `*/SAEDRVT14_FILL*` 圈出该库所有 filler 变体，`create_stdcell_filler` 插入，紧接着 `connect_pg_net` 把新 filler 的 VDD/VSS 连上。

```tcl
## std filler
set pnr_std_fillers "SAEDRVT14_FILL*"
set std_fillers ""
foreach filler $pnr_std_fillers { lappend std_fillers "*/${filler}" }
create_stdcell_filler -lib_cell $std_fillers
connect_pg_net -net $NDM_POWER_NET [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_pins -hierarchical "*/VSS"]
```

代码要点：

- `foreach ... lappend std_fillers "*/${filler}"`：给每个通配符名前缀 `*/`，让它能匹配任意层次下的实例；最终 `std_fillers` 是一个 filler 单元名集合。
- `create_stdcell_filler -lib_cell $std_fillers`：把这个集合作为「候选 filler 池」交给工具，工具自行选配填充每条缝隙。
- 这次插入之后**没有** `remove_cells`，所以这些 filler 会**永久留在设计里**，最终写进 GDSII。

**对比：布局后的第一次 filler 插入（插完就删）**：

[IC Compiler II/PnR.tcl:146-153](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L146-L153) —— 布局后、CTS 前的 filler：插入后立刻 `remove_cells` 把它们删掉。

```tcl
## std filler
...
create_stdcell_filler -lib_cell $std_fillers
connect_pg_net -net $power  [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $ground [get_pins -hierarchical "*/VSS"]
remove_cells [get_cells -filter ref_name=~"*FILL*" ]   # ← 立刻删掉
```

为什么要「插了又删」？因为 CTS 和布线阶段需要做大量优化（插 buffer、移动单元），如果缝隙里塞满固定 filler，工具反而施展不开。所以这里的 filler 只是**临时占位**——目的是让布线前的寄生提取与时序估算更接近真实（缝隙被填上后金属覆盖更连续），估算完就删，把空间还给后续优化。等布线彻底定稿（`route_opt` 之后），再插一次「不删」的 filler，作为最终交付。

> 这就是为什么 `PnR.tcl` 末尾会出现两段几乎一样的 `## std filler` 代码：第一段是「临时的」，第二段是「永久的」。

**完整模板对照（带查冲突）**：

[IC Compiler II/Scripts/03_PnR_setup.tcl:488-491](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L488-L491) —— 完整流程额外做了「删冲突 filler + 合法化检查」，比 `PnR.tcl` 严谨。

```tcl
create_stdcell_fillers -lib_cells {FILLCELL*}
connect_pg_net -automatic
remove_stdcell_fillers_with_violation   # 删除引发 DRC 的 filler
check_legality                          # 复查合法性
```

注意两处差异：(1) 模板用 `create_stdcell_fillers`（带 s），`PnR.tcl` 用 `create_stdcell_filler`（不带 s），二者都是合法的 ICC2 命令变体；(2) 模板多了 `remove_stdcell_fillers_with_violation`——某些 filler 插进去会和邻居撞 DRC（例如与相邻宏单元过近），需要拆掉重选。

> ⚠️ **模板混用不同 PDK（如实提醒）**：`PnR.tcl` 的 filler 名是 `SAEDRVT14_FILL*`（Synopsys SAED14 库），而 `01_common_setup.tcl` 的标准单元 GDS/库指向的是 **NangateOpenCellLibrary（FreePDK45）**。这两个库属于不同工艺，直接跑会「找不到单元」。这与前序讲义反复强调的一致——本仓库是**模板**，库名/层名要按真实 PDK 逐项替换，不能开箱即跑。

#### 4.1.4 代码实践

**实践目标**：亲眼看出「布局后 filler 被删、布线后 filler 保留」这一差异，并理解它对最终 GDS 的影响。

**操作步骤（源码阅读型）**：

1. 打开 [`PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl)，分别定位第 146-153 行（第一次）和第 178-184 行（第二次）两段 `## std filler`。
2. 对比两段代码：哪一段后面跟着 `remove_cells`？哪一段没有？
3. 打开 [`03_PnR_setup.tcl:488-491`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L488-L491)，看完整模板多出的 `remove_stdcell_fillers_with_violation`。

**需要观察的现象**：

- 第一次 filler 段：代码末尾的 `remove_cells [get_cells -filter ref_name=~"*FILL*" ]` 会把刚插入的所有 FILL 单元清空。
- 第二次 filler 段：没有任何 `remove_cells`，filler 留存。

**预期结果**：

- 第一次 filler 是「估算寄生用」的临时物，进 CTS 前必须清掉；
- 第二次 filler 是「交付用」的永久物，会进入 GDSII。如果你在 GDS 里看到 filler 单元，它一定来自第二次插入。

**待本地验证**：若有 ICC2 与真实库环境，可在两次 filler 后分别 `report_cell [get_cells -filter "is_filler==true"]`，第一次后续会清零、第二次会保留一长串 filler。

#### 4.1.5 小练习与答案

**练习 1**：为什么 filler 单元「没有逻辑」却必须出现在最终 GDSII 里？

> **参考答案**：filler 的作用是物理层面的——补全阱（N-well/P-well）连续性、接通每行 M1 电源轨、满足 implant/间距 DRC。代工厂按 GDSII 制造掩模时，缺了 filler 就会出现阱断裂、电源轨断开，导致 DRC 失败甚至器件失效。所以它必须留在版图里。

**练习 2**：`PnR.tcl` 146-153 行明明插了 filler 又删掉，这段「白做」的代码到底有什么用？

> **参考答案**：它是「临时占位」。布线前填上缝隙，能让此时的寄生提取与时序估算更接近布线后的真实情况（金属覆盖连续），从而让 CTS 和布线优化基于更准的时序做决策。估算完即删，是为了不占用空间、不妨碍后续 buffer 插入与单元移动。

---

### 4.2 PG 自动连接（connect_pg_net）

#### 4.2.1 概念说明

filler 单元插进去之后，每个新 filler 都带着尚未连接的 VDD/VSS 引脚——它们是「悬空」的。如果不连电，后面做电源完整性分析、LVS、流片都会出错。所以**插完 filler 必须再 `connect_pg_net` 一次**。

`connect_pg_net` 的职责是：把某个电源网（net，例如 VDD）与一批电源引脚（pin）在**逻辑连接**层面挂上。注意它管的是「逻辑连接关系」，真正把金属画出来是 `compile_pg`（u4-l3）和布线阶段的事。

ICC2 提供两种调用风格：

- **显式指定**：`connect_pg_net -net <net> <pin集合>`——你告诉它「把哪些引脚连到哪个网」。需要你自己用 `get_pins -hierarchical` 选对引脚集合。
- **自动连接**：`connect_pg_net -automatic`——工具按网名/引脚名规则自己匹配，不用逐一指定。

#### 4.2.2 核心流程

收尾阶段的 PG 连接关注两个对象：

```
标准单元的电源引脚  */VDD  */VSS   （含刚插入的 filler）
顶层端口的电源脚    顶层 VDD/VSS port
        │
        ▼
connect_pg_net -net VDD [get_pins -hierarchical "*/VDD"]   # 单元引脚
connect_pg_net ... [get_ports -physical_context "*/VDD"]   # 顶层端口（模板里才有）
```

一条经验法则：**凡是新增了带电源引脚的单元（filler、decap、well-tap、tapcell），都要重新 `connect_pg_net`。**

#### 4.2.3 源码精读

**收尾的 PG 重连（显式指定）**：

[IC Compiler II/PnR.tcl:183-184](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L183-L184) —— 收尾 filler 插入后，把全层次下所有 VDD/VSS 引脚重新挂到对应电源网。

```tcl
connect_pg_net -net $NDM_POWER_NET  [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_pins -hierarchical "*/VSS"]
```

`$NDM_POWER_NET`/`$NDM_GROUND_NET` 在 [`01_common_setup.tcl:28-30`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L28-L30) 被定义为 `"VDD"`/`"VSS"`。`get_pins -hierarchical "*/VDD"` 跨所有层次抓取名为 `VDD` 的引脚——这正好覆盖了刚插入的 filler。

回顾一下电源网最初的建立（上下文），来自 u4-l3：

[IC Compiler II/PnR.tcl:42-46](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L42-L46) —— floorplan 阶段：`create_net` 建网，第一次 `connect_pg_net` 把当时的（非 filler）单元引脚连上。

```tcl
create_net -power $NDM_POWER_NET
create_net -ground $NDM_GROUND_NET
connect_pg_net -net $NDM_POWER_NET  [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_pins -hierarchical "*/VSS"]
```

可以看到收尾的 183-184 行与这里几乎一字不差——这正是「重新连一次」的体现：filler 是后来才加的，必须再跑一遍同样的命令把新引脚也纳入。

**完整模板对照（自动连接 + 顶层端口）**：

[IC Compiler II/Scripts/03_PnR_setup.tcl:489](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L489) —— 模板用更简洁的 `-automatic` 自动连接。

```tcl
connect_pg_net -automatic
```

[IC Compiler II/Scripts/03_PnR_setup.tcl:453-456](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L453-L456) —— 模板在布线前不仅连了**单元引脚**（pins），还连了**顶层端口**（ports），这是 `PnR.tcl` 漏掉的。

```tcl
connect_pg_net -net $NDM_POWER_NET  [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_pins -hierarchical "*/VSS"]
connect_pg_net -net $NDM_POWER_NET  [get_ports -physical_context "*/VDD"]
connect_pg_net -net $NDM_GROUND_NET [get_ports -physical_context "*/VSS"]
```

> 一个 `PnR.tcl` 的**潜在隐患（如实提醒）**：它只对 `[get_pins ...]` 连电，没有对 `[get_ports -physical_context ...]` 连顶层电源端口。在真实项目里，若顶层 PG 端口没连上，LVS/电源分析会报「未连接端口」。完整模板里那两行 `get_ports` 正是补这个缺口的。

#### 4.2.4 代码实践

**实践目标**：理解「显式指定」与「自动连接」两种写法的等价性与差异，并找到 `PnR.tcl` 漏连顶层端口的位置。

**操作步骤**：

1. 在 [`PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) 中统计 `connect_pg_net` 出现的总次数与位置（提示：分别在 floorplan 段、placement filler 段、finishing filler 段）。
2. 对比 [`03_PnR_setup.tcl:453-456`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L453-L456)，注意多了 `get_ports -physical_context`。

**需要观察的现象**：`PnR.tcl` 里所有 `connect_pg_net` 的对象都是 `[get_pins ...]`，没有任何一条针对 `get_ports`。

**预期结果**：你能指出 `PnR.tcl` 缺少顶层电源端口的连接——这是极简模板的一个「省略项」，真实流程需补上。

**待本地验证**：若可运行 ICC2，连完电后用 `report_pg_connections` 或 GUI 高亮未连接 PG 引脚，应能看到 `PnR.tcl` 流程下顶层 PG 端口标记为 unconnected。

#### 4.2.5 小练习与答案

**练习 1**：`connect_pg_net -net VDD [get_pins -hierarchical "*/VDD"]` 和 `connect_pg_net -automatic` 各自的适用场景？

> **参考答案**：前者「显式指定」可控性强，适合网名/引脚名不完全规整、或只想连某一批引脚的场景，但要自己写对集合；后者「自动连接」简单省事，工具按命名规则（网名与引脚名一致）自动匹配，适合命名规范统一的设计。本仓库 filler 名/电源名都很规整，两种都能用。

**练习 2**：为什么 filler 插入后必须**重新**跑一次 `connect_pg_net`，而不能复用 floorplan 阶段那次的结果？

> **参考答案**：floorplan 阶段的 `connect_pg_net` 只覆盖了**当时存在**的单元引脚。filler 是收尾阶段才加入的新单元，它们的 VDD/VSS 引脚在那次连接之后才出现，自然没被连上。所以「插了新带电源引脚的单元 → 必须再连一次」是固定规则。

---

### 4.3 check_lvs：连接性自检

#### 4.3.1 概念说明

**LVS（Layout Versus Schematic，版图对网表）**是流片前的一道关键校验：它把「从物理版图里提取出的器件与连线」与「逻辑网表」逐器件、逐网络地比对，确认两者完全一致——没有多出来的短路、没有断开的连线、没有悬空管脚。

LVS 通常由**独立的签核工具**完成（如 Mentor/Siemens Calibre、Synopsys IC Validator/ICV）。但 ICC2 内部也带了一个**预检查命令 `check_lvs`**——它在 ICC2 自己的设计库里做一次连接性自检，提前把「明显错配」揪出来，免得把问题拖到外部 LVS 工具那里才暴露。

`check_lvs` 主要查：

- 逻辑网表与物理连接是否一致（有没有 routing 引入的开路/短路）；
- PG 连接是否完整（呼应 4.2：PG 没连好，这里会报）；
- 有没有「逻辑上连着、物理上断开」或反之的情况。

#### 4.3.2 核心流程

`check_lvs` 必须排在 **filler 插入 + PG 重连之后**，因为这两步改变了设计的连接关系（新增了 filler 单元、新增了 PG 连接）。顺序错了，检查结果就不反映最终状态：

```
create_stdcell_fillers      # 新增单元
connect_pg_net -automatic   # 新增 PG 连接
remove_stdcell_fillers_with_violation
check_legality              # 合法化
check_lvs                   # ← 此时连接性已定型，再做 LVS 自检
```

#### 4.3.3 源码精读

**`check_lvs` 只存在于完整模板**——这是 `PnR.tcl` 极简模板省略的典型步骤之一：

[IC Compiler II/Scripts/03_PnR_setup.tcl:478-494](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L478-L494) —— §7 Finishing 全段：filler → PG → 删冲突 → 合法化 → **`check_lvs`** → 存档。

```tcl
########### 7. Finishing #####################
copy_block -from_block route.design -to finish.design
current_block finish.design
...
create_stdcell_fillers -lib_cells {FILLCELL*}
connect_pg_net -automatic
remove_stdcell_fillers_with_violation
check_legality
check_lvs              # ← ICC2 内部连接性/LVS 自检
puts "finish_finish"
save_block
```

这段还顺带展示了完整的版本链范式（u4-l1 已建立）：`copy_block` 从 `route.design`（布线结果）拷出 `finish.design`，`current_block` 切过去，干完收尾活再 `save_block`——可回退、可审计。

> **如实提醒**：`PnR.tcl` 在收尾段（178-184 行）**没有** `check_lvs`，也没有 `check_legality`、`remove_stdcell_fillers_with_violation`。这意味着用极简模板跑出来的设计，ICC2 内部不会做这道自检——风险是「PG 没连好」或「连接性不一致」可能要等到外部 LVS 工具才被发现。真实项目应按完整模板补齐这几条。

#### 4.3.4 代码实践

**实践目标**：在仓库里确认 `check_lvs` 真实出现在哪一行，并理解它为何排在 filler/PG 之后。

**操作步骤**：

1. 用编辑器搜索整个 `IC Compiler II/` 目录下的 `check_lvs`，确认它**只**出现在 `Scripts/03_PnR_setup.tcl:492`，而不在 `PnR.tcl`。
2. 阅读 481-494 行，画出从 `route.design` 到 `check_lvs` 的命令顺序。

**需要观察的现象**：`check_lvs` 上方依次是 `create_stdcell_fillers` → `connect_pg_net -automatic` → `remove_stdcell_fillers_with_violation` → `check_legality`。

**预期结果**：你能解释「LVS 自检必须排在所有改变连接性的步骤之后」——否则检查的不是最终连接状态。

**待本地验证**：若有 ICC2 环境，故意注释掉 `connect_pg_net -automatic` 再跑 `check_lvs`，观察报告里是否出现 unconnected PG pin 类的告警。

#### 4.3.5 小练习与答案

**练习 1**：ICC2 的 `check_lvs` 与外部 Calibre/ICV 做的 LVS 是什么关系？能互相替代吗？

> **参考答案**：不能完全替代。`check_lvs` 是 ICC2 **内部**基于自己设计库的连接性预检查，快但不够权威；Calibre/ICV 是**独立签核工具**，从最终 GDSII/DEF 用工艺规则做严格的版图抽取与比对，是流片签字的依据。前者用来「早发现、早修」，后者用来「最终签字」，二者是「自检 vs 签核」的关系。

**练习 2**：如果把 `check_lvs` 挪到 filler 插入**之前**执行，结果会怎样？

> **参考答案**：检查结果不反映最终状态——filler 的新增引脚、PG 的新连接都没纳入，可能漏掉「filler 电源未连」「连接性不一致」等问题。所以 `check_lvs` 必须排在 filler 插入与 PG 重连**之后**。

---

### 4.4 多种 Verilog 视图输出（write_verilog）

#### 4.4.1 概念说明

收尾阶段一个反直觉的现象：**一次 `write_verilog` 不够，要写好几份不同的 Verilog**。原因是不同的下游工具对网表的需求不同，一份网表没法同时满足所有人：

| 视图 | 典型后缀 | 关键开关 | 主要用途 |
|------|----------|----------|----------|
| PG 网表 | `.pg.v` | `-include {pg_netlist ...}` | 电源/IR/EM 分析、PG 感知 STA、PG-LVS |
| 非 PG 网表 | `.v` | `-exclude {pg_netlist}` | 标准时序 STA（PrimeTime）、门级仿真 |
| LVS 网表 | `.lvs.v` | `-include "scalar_wire_declarations diode_cells spare_cells"` | 外部 LVS 工具（Calibre/ICV） |
| PrimeTime 专用 PG 网表 | `_for_pt_v.v` | `-include {pg_netlist}` | PrimeTime PG 感知签核 |

**为什么需要 PG 与非 PG 两种 Verilog**（本讲实践任务的核心问题）？

- **PG 网表**保留了 VDD/VSS 这些电源网与电源引脚的显式连接。电源分析（IR drop、EM）、PG 感知的时序分析、以及和版图做 PG-LVS 比对时，**需要**这些信息。
- **非 PG 网表**把电源网剥掉。标准 STA 和门级仿真**不关心电源**，反而电源网会带来一堆「未连接引脚」告警、让仿真器/STA 工具困惑。剥干净后网表更清爽、与综合后网表结构一致，便于比对。

一句话：**「算功耗/对版图」要 PG，「算时序/做仿真」不要 PG。**

#### 4.4.2 核心流程

写 Verilog 之前有一条**必须先做**的命令：

```
change_names -rules verilog -verbose   # ① 先把实例/网名改成 Verilog 合法名
write_verilog ...                      # ② 再写（PG / 非 PG / LVS / PT 各一份）
```

为什么先 `change_names`？ICC2 内部允许实例名、网络名包含 Verilog 语法里**非法**的字符（例如总线展开的方括号、特殊符号、超长名等）。直接 `write_verilog` 写出去的文件，下游工具（仿真器、综合器、PrimeTime）可能读不回来。`change_names -rules verilog` 按 Verilog 命名规则把这些名字改合法，`-verbose` 打印改名细节。**必须在所有 `write_verilog` 之前执行一次。**

#### 4.4.3 源码精读

**`PnR.tcl` 的 `change_names` + 两份 Verilog**：

[IC Compiler II/PnR.tcl:192-199](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L192-L199) —— 先改名，再写 PG 与非 PG 两份 Verilog。

```tcl
change_names -rules verilog -verbose
write_parasitics -output {../output/${TOP_DESIGN}.spef}     # SPEF，见 4.5
write_verilog \
    -include {pg_netlist unconnected_ports} \
    ../output/${TOP_DESIGN}_pg.v                             # PG 网表
write_verilog \
    -exclude {pg_netlist} \
    ../output/${TOP_DESIGN}.v                                # 非 PG 网表
```

`${TOP_DESIGN}` 在 [`PnR.tcl:14`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L14) 设为 `ChipTop`，所以产出 `ChipTop_pg.v` 与 `ChipTop.v`。

- `-include {pg_netlist unconnected_ports}`：包含电源网表，并保留未连接端口（PG 分析需要看到这些）。
- `-exclude {pg_netlist}`：剥掉电源网表，得到「干净」网表。

**完整模板的四份 Verilog 视图**：

[IC Compiler II/Scripts/03_PnR_setup.tcl:500-506](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L500-L506) —— 完整模板写了 4 份不同视图的 Verilog。

```tcl
write_verilog -include {pg_netlist unconnected_ports} ${DESIGN_NAME}.pg.v          # PG
write_verilog -exclude {physical_only_cells} -top_module_first ${DESIGN_NAME}.v    # 非 PG
write_verilog -include "scalar_wire_declarations diode_cells spare_cells" ${DESIGN_NAME}.lvs.v  # LVS
write_verilog -include {pg_netlist} ${DESIGN_NAME}_for_pt_v.v                      # PrimeTime PG
```

要点：

- `.lvs.v` 用 `-include "scalar_wire_declarations diode_cells spare_cells"`：标量线声明（一位一根线，便于 LVS 逐线比对）、保留二极管单元（天线修复加的 diode，普通网表可能裁掉）、保留 spare cell（冗余备用单元）——这些都是 LVS 必须看到、但 STA/仿真不在乎的「物理配件」。
- `_for_pt_v.v` 是 `-include {pg_netlist}` 的 PG 网表，**专门给 PrimeTime** 做 PG 感知签核。
- 注意模板的非 PG 用的是 `-exclude {physical_only_cells}`（剔除「仅物理」单元，如 filler/well-tap 这些没逻辑的），比 `PnR.tcl` 的 `-exclude {pg_netlist}` 更精确地剔除了物理占位单元。

#### 4.4.4 代码实践

**实践目标**：把「PG vs 非 PG」的差异在源码里定位清楚，并回答实践任务的两个问题。

**操作步骤**：

1. 在 [`PnR.tcl:194-199`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L194-L199) 找到两份 `write_verilog`，记下各自开关与输出文件名。
2. 在 [`03_PnR_setup.tcl:500-506`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L500-L506) 找到四份 `write_verilog`，对比多了哪两份（`.lvs.v`、`_for_pt_v.v`）。

**需要观察的现象**：

- `PnR.tcl`：`_pg.v` 用 `-include {pg_netlist ...}`，`.v` 用 `-exclude {pg_netlist}`。
- 模板：额外有 `.lvs.v`（含 diode/spare/scalar）和 `_for_pt_v.v`（PG，给 PT）。

**预期结果**（直接回答实践任务）：

- **为 PrimeTime 准备的 netlist 由哪条命令产生**：在 `PnR.tcl` 里，PT 用的门级网表来自 194-199 行的 `write_verilog`——做标准 STA 用非 PG 的 `ChipTop.v`；做 PG 感知 STA 用 `ChipTop_pg.v`。在完整模板里，则有专门为 PT 准备的 `_for_pt_v.v`（506 行，PG 网表）。
- **为何需要 PG 与非 PG 两种 Verilog**：PG 网表保留 VDD/VSS 电源网与电源引脚连接，供电源/IR/EM 分析与 PG-LVS 使用；非 PG 网表剥掉电源网，避免 STA 和门级仿真里出现「未连接电源引脚」的告警，与时序分析、仿真的需求更匹配。一份网表无法同时满足「要电源」与「不要电源」的两类下游工具。

**待本地验证**：用文本编辑器同时打开 `.pg.v` 与 `.v`，搜索 `VDD`/`VSS`——PG 版会出现电源网声明与连接，非 PG 版应当看不到。

#### 4.4.5 小练习与答案

**练习 1**：`change_names -rules verilog` 为什么必须放在所有 `write_verilog` 之前？

> **参考答案**：ICC2 内部命名允许 Verilog 非法字符（方括号、特殊符号、超长名等）。若不先改名，写出的 Verilog 下游工具读不回来，STA/仿真会报语法错。`change_names` 按 Verilog 规则把名字改合法，所以必须在写网表之前执行。

**练习 2**：`.lvs.v` 为什么特意「保留 diode_cells 和 spare_cells」，而 `.v` 不保留？

> **参考答案**：天线修复加的二极管（diode）和冗余备用单元（spare cell）在**逻辑功能**上不起作用，所以给 STA/仿真用的 `.v` 把它们当噪声裁掉；但 LVS 是「版图对网表」的物理比对，版图里这些单元真实存在，网表里也得有才能一一对应，否则 LVS 报「器件不匹配」。所以 `.lvs.v` 要保留它们。

---

### 4.5 SDC / SPEF / DEF / GDS 输出

#### 4.5.1 概念说明

Verilog 网表只描述了「逻辑连接」，但交付一颗芯片还需要时序约束、寄生参数、物理版图、版图交换格式等多种文件。下表把收尾输出的几类交付物一次性对照清楚：

| 文件 | 格式 | 产生命令 | 内容 | 主要消费者 |
|------|------|----------|------|-----------|
| SPEF | `.spef` | `write_parasitics` | 提取出的寄生电阻/电容 | PrimeTime（STA 反标） |
| SDC | `.sdc` / `.tcl` | `write_sdc` | 时序约束（时钟、I/O 延迟等） | PrimeTime / 下游 PnR |
| DEF | `.def` | `write_def` | 物理布局+布线（设计交换） | 外部 LVS/抽取工具、其它 PnR |
| GDSII | `.gds` | `write_gds` | 完整版图（掩模图形） | 代工厂（流片） |

几个要点：

- **SPEF（Standard Parasitic Exchange Format）**：布线完成后，金属走线的寄生电阻和电容被提取出来存成 SPEF。PrimeTime 读它来反标（back-annotate）寄生，做真实的签核 STA。这是 PnR→STA 的关键交接物。
- **SDC 输出**：把 ICC2 里当前的时序约束导出。注意不同消费者要不同 SDC——PrimeTime 需要排除某些项（如 `ideal_network`/`pvt`/`timing_derate`），所以会写多份。
- **GDSII**：这才是真正的「流片交付物」——代工厂据它制造掩模。它要把 ICC2 的设计版图与每个标准单元、SRAM 宏的**真实 GDS**（几何图形）合并（merge），并用 layer map 把 ICC2 层名翻译成 GDS 的层号/数据类型。

#### 4.5.2 核心流程

收尾输出的典型顺序（先逻辑类，后物理类）：

```
change_names                  # 改名（4.4）
write_parasitics   -> SPEF     # 寄生
write_verilog      -> *.v 等   # 网表（4.4）
write_sdc          -> *.sdc    # 约束（模板才有）
write_def          -> *.def    # 设计交换（模板才有）
write_gds          -> *.gds    # 版图流片
save_block / save_lib          # 存档收尾
```

GDS 写出时要做两件「合并」：一是把设计的几何与每个单元的 GDS 模板合并（`-merge_files`），二是把 ICC2 层名翻译为 GDS 层号（`-layer_map`）。

#### 4.5.3 源码精读

**`PnR.tcl` 的 SPEF + GDS（无 SDC、无 DEF）**：

[IC Compiler II/PnR.tcl:190-207](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190-L207) —— 存 Final 快照、写 SPEF、写两份 Verilog、最后写 GDS。

```tcl
save_block -as "${TOP_DESIGN}_Final"     # 把最终设计存为 ChipTop_Final 快照
save_lib
change_names -rules verilog -verbose
write_parasitics -output {../output/${TOP_DESIGN}.spef}        # SPEF（寄生）
write_verilog ...  ../output/${TOP_DESIGN}_pg.v                # PG 网表（4.4）
write_verilog ...  ../output/${TOP_DESIGN}.v                   # 非 PG 网表（4.4）
write_gds -design ${TOP_DESIGN}_Final \                        # 从 Final 快照写 GDS
    -layer_map $GDS_MAP_FILE \
    -keep_data_type \
    -fill include \
    -output_pin all \
    -merge_files [list $STD_CELL_GDS $SRAM_SINGLE_GDS] \
    -long_names \
    ../output/${TOP_DESIGN}.gds
```

`write_gds` 关键开关解读：

- `-design ${TOP_DESIGN}_Final`：**从 `ChipTop_Final` 这份快照写 GDS**——呼应 4.1/4.2 之前的 `save_block -as "${TOP_DESIGN}_Final"`。这就是 u4-l1 讲的 block 版本链：先存 Final，再从 Final 写交付物。
- `-layer_map $GDS_MAP_FILE`：层名→GDS 层号映射表，`$GDS_MAP_FILE` 在 [`01_common_setup.tcl:21`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L21) 定义为 `FreePDK45_10m_gdsout.map`。
- `-merge_files [list $STD_CELL_GDS $SRAM_SINGLE_GDS]`：合并标准单元 GDS（`$STD_CELL_GDS`，[01_common_setup.tcl:22](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl#L22) = `NangateOpenCellLibrary.gds`）与 SRAM GDS（`$SRAM_SINGLE_GDS`）。
- `-fill include`：把 filler 单元也写进 GDS（呼应 4.1——最终 filler 必须进版图）。
- `-keep_data_type` / `-output_pin all` / `-long_names`：保留 GDS 数据类型、写出全部引脚几何、允许长名。

> **如实提醒**：`$SRAM_SINGLE_GDS` 在 [`01_common_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/01_common_setup.tcl) 中**并未定义**（只有 `STD_CELL_GDS` 定义了），它需要在真实运行前由用户补上，指向 SRAM 宏单元的 GDS 文件。否则 `write_gds` 会因变量未定义而失败。这再次说明仓库是模板。

**完整模板的 SDC / SPEF / DEF / GDS（全套）**：

[IC Compiler II/Scripts/03_PnR_setup.tcl:508-529](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L508-L529) —— §8 Checks and Outputs：三份 SDC、SPEF、DEF、GDS 一应俱全。

```tcl
######## SDC_OUT
write_sdc -output ${DESIGN_NAME}.out.sdc                                   # 通用 SDC
write_sdc -output ${DESIGN_NAME}.pt.sdc.tcl -exclude {ideal_network pvt timing_derate}   # 给 PT，排除若干项
write_sdc -output ${DESIGN_NAME}.pt_clock_latency.sdc.tcl -include {clock_latency}        # 时钟延迟专用
####### SPEF_OUT
report_timing -crosstalk_delta
write_parasitics -format SPEF -output ${DESIGN_NAME}.out.spef              # SPEF
######DEF_OUT
write_def ${DESIGN_NAME}.out.def                                           # DEF
##########GDS_OUT
write_gds \
    -view design -lib_cell_view frame \
    -output_pin all -fill include -exclude_empty_block -long_names \
    -layer_map "$GDS_MAP_FILE" -keep_data_type \
    -merge_files "$STD_CELL_GDS" \
    ./output/${DESIGN_NAME}.gds                                            # GDS
```

要点：

- **三份 SDC**：通用 `.out.sdc`、给 PrimeTime 的 `.pt.sdc.tcl`（`-exclude {ideal_network pvt timing_derate}` 去掉 PT 不需要的项）、时钟延迟专用 `.pt_clock_latency.sdc.tcl`（`-include {clock_latency}`）。这呼应了 4.4 的「不同下游要不同视图」思路。
- `write_parasitics -format SPEF` 显式指定 SPEF 格式（`PnR.tcl` 省略了 `-format`，用默认）。
- 模板有 `write_def`（`PnR.tcl` 没有），产出 DEF 供外部工具用。
- 模板的 `write_gds` 用 `-view design -lib_cell_view frame`、`-exclude_empty_block` 等更多开关，且只 merge `$STD_CELL_GDS`（没列 SRAM）。

> 注意 `PnR.tcl` 与模板在收尾输出上的取舍：极简模板只输出 SPEF + 两份 Verilog + GDS（够走完一次基本 STA 与流片），完整模板额外输出 SDC、DEF、`.lvs.v`、PT 专用网表等「签核级」全套交付物。

#### 4.5.4 代码实践

**实践目标**：把「为 PrimeTime 准备 netlist + SPEF」的命令链在两个文件里全部找齐，并说明 PG/非 PG 的取舍。

**操作步骤**：

1. 在 [`PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) 中定位：产生 SPEF 的命令（193 行）、产生 PT 网表的命令（194-199 行）。
2. 在 [`03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 中定位：PT 专用网表（506 行 `_for_pt_v.v`）、PT 专用 SDC（511 行 `.pt.sdc.tcl`）、SPEF（515 行）。
3. 追踪这些文件如何被 PrimeTime 消费（详见 u6-l1）：PT 用 `read_verilog` 读网表、`read_parasitics` 读 SPEF、`source` 读 SDC。

**需要观察的现象**：`PnR.tcl` 用一条 `write_parasitics` 产 SPEF、用两条 `write_verilog` 产 PG/非 PG 网表；完整模板额外多出 PT 专用网表与 PT 专用 SDC。

**预期结果**（实践任务完整答案）：

- **netlist 与 SPEF 由哪些 write 命令产生**：
  - SPEF ← `write_parasitics`（[`PnR.tcl:193`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L193) / [`03_PnR_setup.tcl:515`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl#L515)）。
  - PT 网表 ← `write_verilog`（`PnR.tcl` 给 `.v` 非 PG / `_pg.v` PG；模板给 `_for_pt_v.v` PG）。
- **为何需要 PG 与非 PG 两种 Verilog**：见 4.4.4——PG 供电源/PG-LVS/PG 感知 STA，非 PG 供标准 STA 与门级仿真。

**待本地验证**：在 PrimeTime 里分别用 `ChipTop.v`（非 PG）和 `ChipTop_pg.v`（PG）跑一次 `read_verilog` + `read_parasitics`（读 SPEF），对比报告里「unconnected pin」类告警数量的差异——非 PG 版应明显更干净。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `write_gds` 要用 `-merge_files` 合并标准单元/SRAM 的 GDS？

> **参考答案**：ICC2 设计库里存的只是单元的「抽象/参考视图」（用于 PnR 布局布线），不是代工厂要的真实掩模几何。真实几何在每个标准单元、每个 SRAM 宏各自的 GDS 文件里。`write_gds` 必须把这些 GDS 模板「合并」进设计版图，拼出完整的、可直接制掩模的 GDSII。

**练习 2**：`PnR.tcl` 没有写 SDC 和 DEF，这对后续流程有什么影响？

> **参考答案**：没写 SDC，意味着 PrimeTime 签核时要么用综合阶段的 SDC、要么手工补，可能漏掉 PnR 阶段新增的约束（如 CTS 后的时钟延迟更新）；没写 DEF，外部 LVS/抽取工具拿不到设计交换格式，可能需要改从 GDSII 抽取。完整模板补了 SDC/DEF，正是为了签核环节的无缝交接。

**练习 3**：`write_gds -design ${TOP_DESIGN}_Final` 里的 `_Final` 从哪来？

> **参考答案**：来自同一段上方 [`PnR.tcl:190`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L190) 的 `save_block -as "${TOP_DESIGN}_Final"`——先把收尾完成的设计存成 `ChipTop_Final` 快照，GDS 从这份快照写出。这是 u4-l1 讲的 block 版本链在收尾的收口。

---

## 5. 综合实践

**任务**：画出 ICC2 收尾（finishing）阶段的完整命令执行流程图，并标注「每一步改变了什么 / 为哪个下游工具做准备」。

**要求**：

1. 以 [`03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 的 §7 Finishing（478-494）+ §8 Outputs（497-529）为骨架，列出从 `copy_block route.design` 到 `write_gds` 的全部命令。
2. 在每条命令旁标注：
   - 它改变了设计的什么（新增单元？新增连接？改名？产出文件？）；
   - 它产出的文件喂给谁（PrimeTime？Calibre/ICV？代工厂？）。
3. 用红笔标出 [`PnR.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) **省略**的步骤（提示：`check_lvs`、`write_sdc`、`write_def`、`.lvs.v`、`remove_stdcell_fillers_with_violation`、顶层端口 `connect_pg_net`）。
4. 回答一个串联问题：如果某次跑下来 PrimeTime 报「大量未连接电源引脚」，你会回头检查收尾阶段的哪几条命令？

**参考流程图骨架**（请自行补全标注）：

```
route.design (布线结果，来自 u4-l6)
   │ copy_block
   ▼
finish.design
   │ create_stdcell_fillers        ── 新增 filler 单元
   │ connect_pg_net -automatic     ── 给 filler 连电
   │ remove_stdcell_fillers_with_violation ── 删冲突 filler
   │ check_legality / check_lvs    ── 合法化 + 连接性自检
   │ save_block
   ▼
change_names                       ── 改 Verilog 合法名
write_verilog x4 (PG/非PG/LVS/PT)  ── 喂 PT / 仿真 / Calibre
write_sdc x3                       ── 喂 PT
write_parasitics -> SPEF           ── 喂 PT (read_parasitics)
write_def -> DEF                   ── 喂外部抽取/LVS
write_gds -> GDSII                 ── 喂代工厂
   │ save_block / save_lib
   ▼
交付完成
```

**串联问题参考答案**：PrimeTime 报大量未连接电源引脚，先怀疑收尾的 PG 连接——检查是否在 filler 插入后跑了 `connect_pg_net -automatic`（或显式连 `*/VDD`/`*/VSS`）、是否漏连了顶层端口（`get_ports -physical_context`），以及喂给 PT 的网表是否用错了视图（标准 STA 误用了 PG 网表，反而看到一堆 PG 引脚）。

## 6. 本讲小结

- **填充单元**没有逻辑，却必须留在最终版图里——补全阱连续性、接通 M1 电源轨、满足 DRC；`PnR.tcl` 插了两次 filler，布局后那次「插完即删」是为准确估算寄生，布线后那次「保留」才是交付。
- **PG 重连**：filler 插入后新增的 VDD/VSS 引脚必须用 `connect_pg_net` 再连一次；完整模板用 `-automatic`，并补连了顶层端口（`get_ports`），`PnR.tcl` 漏了顶层端口连接。
- **`check_lvs`** 是 ICC2 内部连接性自检，必须排在 filler+PG 之后；它只存在于完整模板（`03_PnR_setup.tcl:492`），`PnR.tcl` 省略了，是外部 Calibre/ICV 签核前的「早发现」环节。
- **多种 Verilog 视图**：PG 网表（`.pg.v`/`_for_pt_v.v`）供电源/PG-LVS/PG 感知 STA，非 PG 网表（`.v`）供标准 STA 与门级仿真，`.lvs.v` 保留 diode/spare 供外部 LVS——一份网表满足不了所有下游。写之前必须先 `change_names -rules verilog`。
- **SDC/SPEF/DEF/GDS**：SPEF 由 `write_parasitics` 产生（喂 PrimeTime 反标寄生）、GDS 由 `write_gds` 产生（合并单元 GDS + 层映射，喂代工厂流片）；`PnR.tcl` 只输出 SPEF+Verilog+GDS，完整模板额外输出 SDC/DEF/`.lvs.v` 等签核级交付物。
- 贯穿全讲的判断：`PnR.tcl` 是「能跑通主流程」的极简模板，`03_PnR_setup.tcl` 是「可签核交付」的完整模板——两者的差异，正是从「学习样板」到「生产流程」要补的功课。

## 7. 下一步学习建议

本讲结束，ICC2 物理设计主流程（U4，setup→floorplan→power→placement→CTS→routing→finishing）已完整走完一遍。接下来推荐：

1. **横向对比**：进入 u5，看 [`IC Compiler/`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler)（旧版 ICC）与 `mentor_scripts/`（Mentor Nitro）的收尾流程，对比不同工具在 filler/PG/LVS/输出上的命令差异，加深「流程本质相通、命令各有不同」的理解。
2. **签核侧深入**：进入 u6，看 [`PrimeTime/`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl) 如何用本讲产出的 SPEF + Verilog 做 STA 签核（`read_verilog` → `read_parasitics` → `report_timing`），把 PnR 与 STA 的交接闭环。
3. **收尾进阶**：进入 u9-l1，看 `mentor_scripts/4_export.tcl` 里更完整的天线修复、金属填充、DRC 清理流程——它们都是收尾阶段在 `PnR.tcl` 里被省略的「签核级」步骤。
4. **源码延伸阅读**：精读 [`03_PnR_setup.tcl`](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/Scripts/03_PnR_setup.tcl) 的 §7-§8（478-536 行），把它作为你将来写自己的收尾脚本的「权威模板」。
