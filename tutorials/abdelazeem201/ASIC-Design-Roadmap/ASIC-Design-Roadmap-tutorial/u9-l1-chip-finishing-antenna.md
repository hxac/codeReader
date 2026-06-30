# 芯片收尾：天线、填充与金属填充

## 1. 本讲目标

布线（routing）结束、版图已经"连通"，并不等于可以交付代工厂。在 `route_opt` 与最终 `write_gds` 之间，还有一段被称为 **chip finishing（芯片收尾）** 的工序：修复天线违例、插入 filler 单元、补金属填充、清理 DRC，最后吐出 GDSII 等一整套交付物。

本讲学完后，你应该能够：

- 说清**天线效应**的物理起因，以及 `jumper` 与 `diode` 两种修复手段的差别。
- 解释 **filler cell** 为什么"无逻辑却必须留"，以及多尺寸 filler 的**百分比填充算法**。
- 区分 **metal fill（虚拟金属）** 与 filler cell，理解它服务于 **CMP 密度规则**。
- 读懂"改一步、查一步、必要时修一步"的 **DRC 迭代清理**流程，并能解释 filler 插入后为何要重新 `check_drc` 甚至重跑布线修复。
- 列出收尾阶段产出的**全部交付物**及其下游用途。

> 与 u4-l7 的分工：u4-l7 讲过 ICC2 `PnR.tcl` 极简模板里的"两次插 filler + check_lvs + 多视图 Verilog + write_gds"。本讲**不再重复**那部分，而是 (1) 把每一步背后的**物理原理**讲透，(2) 用 Mentor `4_export.tcl` 这份**生产级完整流程**对照极简模板，让你看清"能跑"与"能签核"之间的差距。

## 2. 前置知识

阅读本讲前，建议你已经掌握：

- **u4-l7 收尾与输出**：ICC2 的 filler 两次插入、`connect_pg_net`、`check_lvs`、多视图 Verilog 与 `write_gds` 的基本含义。
- **u4-l6 布线**：知道 `route_opt` 之后版图已经物理连通，但还未做"收尾打磨"。
- **标准单元与版图层栈**：知道标准单元摆放在行（row）里、M1 是最底层金属、电源通过 M1 横轨馈入（见 u3-l1）。
- **DRC 与 LVS**：DRC（Design Rule Check，设计规则检查）查"画得合不合法"；LVS（Layout Versus Schematic）查"版图与原理图是否一致"。

几个本讲要用到的新术语：

| 术语 | 含义 |
|---|---|
| **天线效应 (Antenna Effect)** | 制造过程中，未连通的长金属收集等离子电荷，击穿栅氧的可靠性隐患 |
| **PAR (Process Antenna Ratio)** | 天线比＝挂在栅极上的金属面积 ÷ 栅极面积，超过工艺阈值即违例 |
| **jumper / diode** | 两种天线修复法：跳层打断 / 加反偏二极管泄放 |
| **filler cell** | 无逻辑、只补物理的标准单元行填充 |
| **metal fill (dummy metal)** | 大面积空白区上的虚拟金属图形，服务于 CMP 密度 |
| **CMP** | Chemical Mechanical Polishing，化学机械抛光，磨平金属的工序 |
| **chip finishing** | 布线后、交付前的一系列收尾打磨工序的总称 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `mentor_scripts/4_export.tcl` | **本讲主样本**。Mentor Nitro 参考流程的 export 阶段，一份生产级、DRC 驱动的完整收尾脚本。 |
| `IC Compiler II/PnR.tcl` | ICC2 极简 PnR 模板。用它对照 Mentor，体会"极简可跑"与"完整签核"的差距。 |
| `IC Compiler II/Scripts/03_PnR_setup.tcl` | ICC2 完整 PnR 模板。其 Finishing/Output 段（u4-l7 已讲）用于补充对照。 |

## 4. 核心概念与源码讲解

### 4.1 天线效应与天线修复

#### 4.1.1 概念说明

天线效应（Antenna Effect），又叫**工艺天线效应（Process Antenna Effect, PAE）**，是一种**制造期可靠性**隐患——它和你的 RTL 逻辑对不对毫无关系，而是发生在代工厂的等离子刻蚀/沉积工序里。

直觉可以这样理解：工厂是一层一层往上堆金属的。当某一层金属被刻蚀出来时，如果它只连到了晶体管的**栅极（gate）**、却还没连到任何**源漏扩散区（diffusion）**，这条"悬空"的金属就像一根天线，会疯狂收集等离子体里的带电粒子。电荷无处泄放，电压越抬越高，最终可能**击穿薄薄的栅氧化层**，让这只晶体管永久失效或提前老化。

衡量指标是**天线比 PAR**：

\[
\text{PAR} \;=\; \frac{\text{某工艺步骤中挂在栅极上的金属面积}}{\text{栅极面积}}
\]

当 \( \text{PAR} \) 超过工艺给定的阈值，工具就报天线违例。注意分子是"某一层"的面积——因为每层金属的刻蚀是独立的工序，所以天线风险是**逐层评估**的。

两种主流修复手段：

- **jumper（跳层）**：在靠近栅极处，把一段长金属"跳"到更高一层金属、再绕回来。这样在刻蚀**当前层**时，挂在栅极上的本层面积变小，PAR 下降。是最优先、成本最低的手段。
- **diode（二极管）**：在网络上挂一个**反偏**二极管。正常工作时它不导通、几乎不耗电；只有当电压被天线电荷抬到超过其开启阈值时，它才正向导通、把电荷泄放掉。用于 jumper 无能为力（例如已经到了顶层金属）的情况。

#### 4.1.2 核心流程

```text
1. check_antenna           # 全片逐层扫描，计算 PAR，列出违例网络
2. fix_antenna -method jumper   # 优先用跳层修复
3. (可选) fix_antenna -method diode  # jumper 修不掉的，再上二极管
```

顺序很关键：**先 jumper 后 diode**。jumper 不增加器件、不改网表逻辑，代价小；diode 要新增物理单元、占面积、改网表，是兜底手段。

#### 4.1.3 源码精读

Mentor `4_export.tcl` 的天线修复段：

```tcl
## Fix Antenna
set MGC_fix_antenna true             ; # Mandatory to review : true|false
...
if {$MGC_fix_antenna eq true} {
    check_antenna -cpus $MGC_cpus
    fix_antenna -method jumper -cpus $MGC_cpus

    if {$MGC_fix_antenna_use_diodes eq true} {
        if {$MGC_fix_antenna_diodes != ""} {
            config_antenna -diodes $MGC_fix_antenna_diodes
        }
        fix_antenna -method diode -finish all
    }
}
```

[mentor_scripts/4_export.tcl:64-81](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L64-L81) —— 天线修复三连：先 `check_antenna` 体检，再 `fix_antenna -method jumper` 跳层，最后按开关 `fix_antenna -method diode -finish all` 补二极管（`-finish all` 表示在所有阶段都允许插 diode）。

逐行要点：

- `set MGC_fix_antenna true` 是流程开关，注释里特意写 `Mandatory to review`——提示工程师这条必须人工确认，不能盲目跑。
- `-cpus $MGC_cpus` 把计算并行化，天线扫描很耗时。
- `config_antenna -diodes ...` 允许你**指定**用哪些二极管单元，而不是让工具自己猜——签核流程里器件必须可控。

> 对照 ICC2：极简 `PnR.tcl` 与完整 `03_PnR_setup.tcl` **都没有显式的天线修复命令**。这并不意味着 ICC2 流程不需要修天线，而是这两份模板把它省略了——真实 ICC2 签核流程会用单独的 sign-off 天线命令。所以**天线修复这一节，Mentor 脚本是更完整的样本**。

#### 4.1.4 代码实践

**实践目标**：通过读脚本理解 jumper 与 diode 的调用次序与开关关系。

**操作步骤**：

1. 打开 [4_export.tcl:71-81](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L71-L81)。
2. 回答：`fix_antenna -method jumper` 在 `fix_antenna -method diode` 之内还是之外？为什么这么排？
3. 找到控制是否启用 diode 的变量名，说明它和 `MGC_fix_antenna_diodes` 各自的作用。

**需要观察的现象**：jumper 是无条件执行（只要 `MGC_fix_antenna` 为真），diode 嵌在额外一层 `if {$MGC_fix_antenna_use_diodes eq true}` 里。

**预期结果**：jumper 先行、diode 兜底；`MGC_fix_antenna_use_diodes` 决定"要不要用二极管"，`MGC_fix_antenna_diodes` 决定"用哪些二极管单元"。

> 是否真能减少违例数，取决于具体设计与工艺库，**待本地验证**（需要有 Mentor Nitro 环境与带天线规则的库）。

#### 4.1.5 小练习与答案

**Q1**：为什么 jumper 必须"跳到更高层金属"，而不是在同一层把线打断？

**A1**：在同一层打断并不减少**该层刻蚀工序**中挂在栅极上的面积——因为整层是同时被等离子体暴露的，断开的两段都还在收集电荷。只有把电流路径转到**尚未刻蚀（更高层）或已经连通扩散区**的层，才能在当前层刻蚀时降低挂在栅极上的本层面积，从而压低 PAR。

**Q2**：diode 为什么用反偏接法？

**A2**：反偏在正常工作电压下几乎不导通，既不耗电也不影响功能时序；只有当天线电荷把电压抬到超过二极管开启阈值时，它才导通泄放，相当于一个"过压泄放阀"。

---

### 4.2 filler cell 插入

#### 4.2.1 概念说明

filler cell（填充单元）是一种**无逻辑、只有物理**的单元（physical-only cell）——它的版图里没有可工作的晶体管电路，却必须留在最终版图里。u4-l7 已经给出了它的三条存在理由，本讲把背后的物理讲透：

1. **阱连续性（well continuity）**：PMOS 待在 N 阱里、NMOS 待在 P 衬底里。如果相邻两个标准单元之间留有空隙，N 阱/P 衬底就被切断。后果是：(a) 失去正确的阱偏置；(b) 容易触发**闩锁效应（latch-up）**——寄生可控硅结构被触发，形成 VDD 到 VSS 的低阻通路，可能烧毁芯片。filler 单元把阱"补"成连续的。
2. **M1 电源轨连续**：每个标准单元自己带一小段 M1 上的 VDD/VSS 横轨。单元间留空隙 → 轨道断裂 → 行内某些单元拿不到电源/地。filler 把横轨接起来。
3. **注入区/DRC 连续性**：工艺规则要求 implant（注入区）连续并满足最小密度，filler 帮忙满足这类规则。

filler 单元通常有**多种宽度**（如 FILL1 / FILL2 / FILL4 / FILL8 ……）以填补不同长度的缝隙：大缝用宽 filler、小缝用窄 filler，才能既填满又不浪费。

当有多组 filler 可选时，Mentor 用一个**百分比算法**：工程师给每组 filler 指定一个目标百分比（例如"大 filler 占 60%、中 filler 占 30%、小 filler 占 10%"），工具在填缝隙时尽量按这个比例分配。这样做的好处是可以**控制不同 filler 的使用偏好**——比如多插带去耦电容的 filler（decap filler）来顺带补一点电源去耦电容。

#### 4.2.2 核心流程

```text
1. 把所有 filler 标记为 is_dont_use，防止前面布局/优化阶段被乱用
2. 布线后，按缝隙大小与百分比策略，place_filler_cells 填进去
3. check_unfilled_gaps 检查是否还有没填上的缝
```

Mentor 的百分比填充用 `create_filler_set` 把每组 filler 注册成一个"集合"，再在 `place_filler_cells` 时把集合名与百分比一起传进去。

#### 4.2.3 源码精读

Mentor 的多 filler 百分比算法（核心段）：

```tcl
foreach filler_group $MGC_filler_lib_cells {
    set filler_libcells   [lindex $filler_group 0]
    set filler_percentage [lindex $filler_group 1]
    ...
    create_filler_set -name FS_$filler_groupname -lib_cells [get_lib_cells ...]
    lappend fill_groups FS_$filler_groupname $filler_percentage
}
...
place_filler_cells -filler_set_prefix true \
                   -filler_set_percentages $fill_groups \
                   -cpus $MGC_cpus -respect_blockages $MGC_filler_respect_blockages
```

[mentor_scripts/4_export.tcl:118-145](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L118-L145) —— 先把每组 filler 用 `create_filler_set` 注册成命名集合（`FS_xxx`），把集合名和百分比成对收集进 `fill_groups`，最后一次性传给 `place_filler_cells -filler_set_percentages`。`-respect_blockages` 控制 filler 是否避开禁布区。

进入这段之前，脚本先做了一个很重要的预处理：

```tcl
set_property -objects [get_lib_cells -filter @is_filler] -name is_dont_use -value true
```

[mentor_scripts/4_export.tcl:99](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L99) —— 先把**所有**标记为 filler 的库单元设成 `is_dont_use`（黑名单），后面要用哪组，再单独把那组的 `is_dont_use` 置回 `false`（白名单）。这种"先全禁、再按需放行"是控制工具只用指定 filler 的标准手法。

填完之后还要查漏：

```tcl
check_unfilled_gaps -include_gaps_under_blockage ...
```

[mentor_scripts/4_export.tcl:158-167](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L158-L167) —— 检查有没有遗漏的缝隙。注意 `-include_gaps_under_blockage` 的取值取决于前面 filler 是否避开了禁布区：若避开（`hard`/`all`），则禁布区下的缝不算遗漏；若没避开（`none`），则要查包括禁布区下的缝。

**ICC2 对照**：`PnR.tcl` 用的是单组 filler，且插了两次：

```tcl
## std filler
set pnr_std_fillers "SAEDRVT14_FILL*"
...
create_stdcell_filler -lib_cell $std_fillers
connect_pg_net -net $power   [get_pins -hierarchical "*/VDD"]
connect_pg_net -net $ground  [get_pins -hierarchical "*/VSS"]
remove_cells [get_cells -filter ref_name=~"*FILL*" ]   ; # 第一次：插完就删
```

[IC Compiler II/PnR.tcl:146-153](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L146-L153) —— 布局后第一次插 filler，目的只是让随后的寄生/时序评估**更接近真实**（连续轨、连续阱），评估完立即 `remove_cells` 删掉，因为后面还要做 CTS、布线，布局会再变。

[IC Compiler II/PnR.tcl:178-184](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L178-L184) —— 布线后第二次插 filler，**这次保留**，最终进入 GDSII。注意第二次用的电源变量名是 `$NDM_POWER_NET/$NDM_GROUND_NET`（与 setup 阶段一致），而第一次用的是局部变量 `$power/$ground`（脚本顶部 `set power "VDD"`）——同一个值，两种写法。

#### 4.2.4 代码实践

**实践目标**：理解 ICC2"两次插 filler"与 Mentor"一次插 filler + 百分比"的差异。

**操作步骤**：

1. 读 [PnR.tcl:146-153](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L146-L153) 与 [PnR.tcl:178-184](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L178-L184)，找出两次 filler 调用的一个关键差别（提示：结尾是否有 `remove_cells`）。
2. 读 [4_export.tcl:129-145](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L129-L145)，画出 `filler_group → (lib_cells, percentage) → FS_xxx → fill_groups` 的数据流转图。

**需要观察的现象**：ICC2 第一次 filler 是"插完即删"，第二次"保留"；Mentor 只插一次但支持多组百分比。

**预期结果**：ICC2 极简模板用单一 `SAEDRVT14_FILL*` 通配，Mentor 用 `create_filler_set` + 百分比实现精细化配比。

> 百分比的实际效果取决于库中 filler 单元的种类与缝隙分布，**待本地验证**。

#### 4.2.5 小练习与答案

**Q1**：为什么 ICC2 `PnR.tcl` 第一次插完 filler 又 `remove_cells` 删掉？

**A1**：第一次插 filler 只是为了让**布局后、布线前**的寄生/时序评估更准（连续 M1 轨、连续阱会让寄生与延迟更接近最终形态）。评估完成后，后面还要做 CTS 与布线，布局会再次变化，此时保留的 filler 反而碍事，所以删掉。布线后第二次插的 filler 才是"最终版图"的一部分，保留进 GDSII。

**Q2**：Mentor 脚本里 `set_property ... is_dont_use ... true` 这一步如果删掉，可能出什么问题？

**A2**：工具在前面 place/route 阶段就可能为了凑面积/时序把 filler 单元当普通单元插进设计里。filler 没有逻辑功能，一旦混进功能路径，会破坏网表逻辑。所以必须先全禁，等真正进入收尾阶段再按需放行。

---

### 4.3 金属填充（dummy metal）

#### 4.3.1 概念说明

金属填充（metal fill / dummy metal）和 filler cell 是**两回事**，初学者最容易混淆：

- **filler cell** 是**标准单元行**上的填充，补的是"单元之间的缝"，目的是阱/轨连续。
- **metal fill** 是大片**空白金属层区域**上的虚拟金属图形，补的是"层的密度"，目的是制造均匀性。

metal fill 的动机来自 **CMP（Chemical Mechanical Polishing，化学机械抛光）**：金属沉积完成后，工厂用化学机械把整片晶圆表面磨平。如果某区域金属很稀疏、另一区域很密，磨削量就不均匀，出现 **dishing（碟形凹陷）** 和 **erosion（侵蚀）**，导致铜厚度起伏 → 电阻和耦合电容漂移 → 时序与串扰不可控。

工艺因此对每一层金属提出**密度规则（density rule）**：在任一检查窗口内，

\[
\text{density} \;=\; \frac{\text{窗口内该层金属面积}}{\text{窗口面积}} \;\geq\; \text{density}_{\min}
\]

达不到 `density_min` 的窗口，工具就往里塞 **dummy metal**（虚拟金属图形）凑密度。这些图形必须**电气死掉**——不接任何信号网（通常浮空或接到电源/地），否则会引入额外寄生、破坏已收敛的时序。

> **关键区分**：ICC2 `PnR.tcl` 里 `write_gds -fill include` 的 `fill`，指的是**把 filler cell 写进 GDS 输出**，**不是** dummy metal。`PnR.tcl` 极简模板**没有**显式的金属填充步骤；真正做 dummy metal 的命令（Mentor 叫 `insert_metal_fill`）在极简模板里被省略了。这一点务必记牢，否则会把两个概念搅在一起。

#### 4.3.2 核心流程

```text
1. 逐层、逐窗口计算金属密度
2. 在密度低于门限的窗口里插入 dummy metal（浮空/接地）
3. 复查 DRC：dummy 不能压到信号线、不能违反间距
```

#### 4.3.3 源码精读

Mentor 的金属填充非常直白：

```tcl
## Insert Metal Fill
if { $skip_chip_finishing == false && $MGC_skip_metal_fill == false} {
     fk_msg -type info "Inserting metal fill"
     insert_metal_fill
}
```

[mentor_scripts/4_export.tcl:187-191](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L187-L191) —— 只有在前面没有触发"跳过收尾"（`skip_chip_finishing == false`）且 `MGC_skip_metal_fill` 为假时才插。`insert_metal_fill` 一条命令搞定逐层密度计算与 dummy 插入。

**ICC2 对照**：`PnR.tcl` 的 `write_gds` 带了 `-fill include`：

```tcl
write_gds -design ${TOP_DESIGN}_Final \
 ...
 -fill include \
 ...
```

[IC Compiler II/PnR.tcl:200-207](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L200-L207) —— 这里的 `-fill include` 是**让 filler cell 出现在 GDS 里**（u4-l7 已讲），**不等于**插了 dummy metal。ICC2 真实签核流程会另用 signoff 金属填充命令，但两份模板都没写出来。

#### 4.3.4 代码实践

**实践目标**：在脚本中区分"filler 进 GDS"与"dummy metal 插入"两个动作。

**操作步骤**：

1. 在 [4_export.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl) 里找出真正插入 dummy metal 的命令及其行号。
2. 在 [PnR.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl) 里找出 `-fill include`，并说明它做的是哪件事。

**需要观察的现象**：Mentor 有独立的 `insert_metal_fill` 步骤；ICC2 `PnR.tcl` 只有"filler 进 GDS"的选项，无独立金属填充命令。

**预期结果**：能准确说出 `insert_metal_fill`（dummy metal）与 `write_gds -fill include`（filler 写入 GDS）是两个不同动作。

#### 4.3.5 小练习与答案

**Q1**：dummy metal 为什么不能接到功能信号网上？

**A1**：一旦接上，dummy 就变成信号网络的一部分，会改变该网的寄生电容与电阻，引入未预期的延迟和串扰，破坏已经收敛的时序。所以 dummy 必须电气隔离（浮空或接固定电位如地）。

**Q2**：为什么 metal fill 通常放在收尾的最后几步、而不是布局阶段就做？

**A2**：metal fill 补的是金属层密度，而金属层的图形要到布线甚至 filler 插入后才基本定型。提前填会被后续步骤反复推翻；放在最后填，密度计算才反映真实版图，也避免 dummy 干扰布线优化。

---

### 4.4 DRC 清理与迭代修复

#### 4.4.1 概念说明

chip finishing 的每一步（天线修复、filler、metal fill）都在**改动版图**，而每次改动都可能引入**新的 DRC 违例**。所以收尾流程不是"一条龙走到底"，而是**"改一步、查一步、必要时修一步"的迭代**。

尤其 filler 插入后必须重新查 DRC，原因有二：

1. filler 填进缝隙，可能与邻近的信号线、过孔产生**新的间距违例**。
2. filler 自身放置若不合法（重叠、越出 core、压在禁布区上），也会报错。

Mentor 用一个清晰的"三段比较"来处理：filler 前的错误数记为 \( num0 \)，filler 后记为 \( num1 \)，修复后再记为 \( num2 \)，靠这三个数的增减决定动作：

- 若 \( num1 > 0 \)：说明还有错，需要修。
- 若 \( num1 > num0 \)：**filler 引入了新错**，必须做 DRC 清理。
- 修完再看 \( num2 \)，报告剩余。

此外，脚本在**收尾开始前**还设了一道**阈值熔断**：先查一次 DRC，如果错误数已经超过阈值（默认 100），说明设计本身就有大问题（例如严重拥塞），此时再做 chip finishing 是浪费时间——直接跳过收尾、报告错误，让工程师回去解决根本问题。

#### 4.4.2 核心流程

```text
# (A) 阈值熔断：收尾前的体检
check_drc → num0
if num0 > 阈值(100):
    skip_chip_finishing = true      # 跳过 filler / metal fill

# (B) filler 插入后...
check_drc → num1
if num1 > 0:
    clean_drc                        # 自动修简单 DRC
    run_route_timing -mode repair    # 重跑布线修 DRC（高 effort）
    check_drc → num2                 # 复查并报告剩余
```

`run_route_timing -mode repair` 是关键——它把布线引擎重新跑一遍，但**目标是修 DRC 而不是优化时序**（`-drc_effort high`），相当于"外科手术式"地清理违例。

#### 4.4.3 源码精读

**阈值熔断段**：

```tcl
## Check if design is DRC clean before starting chip finishing
set MGC_num_drc_errors_threshold 100
check_drc
set num0 [llength [get_objects -type error -filter @category==drc -quiet true]]
set skip_chip_finishing false
if { $num0 > $MGC_num_drc_errors_threshold } {
    fk_msg -type info "Number of DRC (including antenna) errors is too high ($num0 > ...). Chip finishing steps will be skipped \n"
    set skip_chip_finishing true
}
```

[mentor_scripts/4_export.tcl:83-91](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L83-L91) —— 注意注释特意写明"including antenna"：`check_drc` 的错误数里**包含**天线违例。`get_objects -filter @category==drc` 把 DRC 类错误捞出来计数，`-quiet true` 表示没有错误时也不报错。

**filler 后的迭代清理段**：

```tcl
## Check for any new DRC issues and fix them
check_drc
set num1 [llength [get_objects -type error -filter @category==drc]]
if { $num1 } {
    if { $num1 > $num0 } {
        fk_msg -type info "... DRC errors increased after filler cell insertion ($num1 > $num0). Running DRC clean-up \n"
    } else {
        fk_msg -type info "Running DRC clean-up for remaining $num1 errors.\n"
    }
    clean_drc
    run_route_timing -mode repair -cpus $MGC_cpus \
        -user_params "-drc_effort high -drc_accept number -dp_local_effort high"
    check_drc
    set num2 [llength [get_objects -type error -filter @category==drc]]
    if { $num2 } { fk_msg -type info "$num2 DRC errors remain.\n" }
}
```

[mentor_scripts/4_export.tcl:169-186](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L169-L186) —— 这就是本讲的主实践对象。注意它对"错误增加"和"错误减少但有残留"两种情况，分别给出不同的提示信息，但**都会**走 `clean_drc` + `run_route_timing -mode repair`。`-drc_effort high` 拉高修 DRC 的努力等级，`-dp_local_effort high` 限定在局部范围做详细修复（避免全局大动破坏时序）。

> 对照 ICC2：`PnR.tcl` 在 filler 后**只做** `connect_pg_net`，没有 `check_drc`/`clean_drc`/`route repair` 这套迭代；完整模板 `03_PnR_setup.tcl` 在 filler 后做的是 `check_legality`（查布局合法性）和 `check_lvs`（查连接性），也没有 Mentor 这种"重跑布线修 DRC"的闭环。**DRC 驱动的迭代修复是 Mentor 这份脚本最值得学的设计模式。**

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：解释为何在 filler 插入后要再次 `check_drc` 并可能运行 route repair。

**操作步骤**：

1. 阅读 [4_export.tcl:169-186](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L169-L186)，把 `num0 / num1 / num2` 三个变量在流程中的位置画成时间轴。
2. 用一段话回答下面三个问题（写在你的学习笔记里）：
   - **为什么 filler 后 DRC 错误可能变多？**（从 filler 改动版图的角度想）
   - **为什么用 `run_route_timing -mode repair` 而不是再跑一遍完整的 `route_opt`？**（从"只想修 DRC、不想动时序"的角度想）
   - **为什么把阈值熔断（`num0 > 100`）放在收尾开始前、而不是 filler 之后？**（从"先治本还是先治标"的角度想）

**需要观察的现象**：`num1 > num0` 与 `num1 <= num0` 两种分支，提示信息不同，但后续修复动作相同。

**预期结果**（参考答案，先自己想再对照）：

- filler 填进缝隙会改变局部金属图形，可能与邻近信号线/过孔产生新的间距违例，filler 自身也可能放置不合法，所以 DRC 数会涨。
- `route_opt` 是"布线 + 时序优化"的全量重跑，代价大、可能动到已经收敛的时序；`-mode repair` 只针对 DRC 违例做局部外科手术（`-dp_local_effort high`），不动时序。
- 阈值熔断放在最前面，是为了在"设计本身有大问题"时尽早止损——如果收尾前就已经 100+ 个 DRC 错误，说明根因在更上游（拥塞、布图不合理），chip finishing 这种"打磨"根本救不了，硬做只会浪费时间并掩盖问题。

> 实际能否把 `num2` 降到 0，取决于设计质量与工艺规则严格度，**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**：脚本里 `get_objects -filter @category==drc` 第一次带 `-quiet true`，第二次没带。这说明什么？

**A1**：第一次（收尾前体检）用 `-quiet true`，是为了在没有 DRC 错误时**不抛异常、安静返回空表**，让 `llength` 得到 0、流程继续；第二次（filler 后）已知大概率有错，去掉 `-quiet` 是为了在出问题时能正常报告。核心是 Tcl 对"空集合"的容错处理。

**Q2**：如果 filler 后 `num1` 比 `num0` 还小，脚本还会跑 `clean_drc` 吗？

**A2**：会。只要 `num1 > 0`（还有残留错误），不管比 `num0` 大还是小，都会进 `if { $num1 }` 分支跑 `clean_drc` + repair。`num1 > num0` 只影响**提示信息的措辞**（"增加了" vs "残留"），不影响是否修复。

---

### 4.5 交付物输出

#### 4.5.1 概念说明

收尾的最后一步，是把版图"翻译"成下游各环节需要的多种格式。不同下游对"同一颗芯片"有不同视角的需求：

| 交付物 | 命令 | 用途 |
|---|---|---|
| **GDSII** | `write_stream` / `write_gds` | 版图全量图形，交**代工厂流片** |
| **Verilog（网表）** | `write_verilog` | 门级网表，交 **LVS / 仿真 / STA** |
| **DEF** | `write_def` | 完整物理设计描述（单元位置、走线），交其它工具 |
| **LEF** | `write_lef` | 把本设计当作**宏单元**对外暴露的物理库 |
| **SPEF** | `write_spef` | 寄生参数，交 **PrimeTime** 做 STA 反标 |
| **SDF** | `write_sdf` | 时序延迟，交**门级仿真** |
| **DB** | `write_db` | 工具自身数据库，便于**回退 / 重入**下一阶段 |
| **Power netlist** | `write_verilog -power` | 含电源网络的网表，交**功耗分析 / PG-LVS** |

其中 GDSII 是流片的**最终终点**（见 u1-l2 全景）；SPEF 是交给 u6-l1 PrimeTime 的**签核输入**。

#### 4.5.2 核心流程

```text
1. 设定输出目录 output/
2. 按格式逐个 write_*（Verilog/DEF/LEF/SPEF/SDF/...）
3. write_stream / write_gds 出 GDSII
4. write_db 保存数据库
5. propagate_power_and_ground_nets + write_verilog -power 出电源网表
6. write_reports 出收尾报告
```

#### 4.5.3 源码精读

Mentor 的输出分两个分支：`MGC_tanner_flow`（Tanner 流程，输出解压的明文 + SDF/SPEF 多角）与默认分支（输出 `.gz` 压缩 + LEF）。

```tcl
} else {
    write_verilog -file $dataDir/${design}.v.gz
    write_def    -file $dataDir/${design}.def.gz
    write_lef    -file $dataDir/${design}.lef.gz -lib_cells [get_lib_cells -of_objects [get_top_partition]]
}
```

[mentor_scripts/4_export.tcl:193-216](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L193-L216) —— 默认分支出 Verilog/DEF/LEF 三件套（`.gz` 压缩）。Tanner 分支（第 199-211 行）则额外按 corner/mode 组合循环出 SDF、按 corner 出带耦合的 SPEF。

GDS 输出：

```tcl
if { $MGC_gds_layer_map_file != "" } {
    source $MGC_gds_layer_map_file
    write_stream -format gds -file $dataDir/${design}.gds
} elseif { $MGC_gds_layer_map_file == "" } {
    fk_msg -type warning "No GDS layer-map file specified ... GDS will not be written"
}
```

[mentor_scripts/4_export.tcl:218-226](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L218-L226) —— 出 GDS 必须先有**层映射文件（layer map）**，把工具内部层名翻译成 GDS 的层号/数据类型；没有映射文件就**拒绝写 GDS**并报警。这是 GDS 输出的硬前提（参见 u3-l3 讲过的层映射思想）。

电源网表输出：

```tcl
propagate_power_and_ground_nets
write_verilog -file $dataDir/${design}.power.v.gz -power true -well_connections true
```

[mentor_scripts/4_export.tcl:237-245](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L237-L245) —— 先 `propagate_power_and_ground_nets` 把电源/地网络在整个层次结构里传播连通，再 `-power true` 出一份带电源网络的 Verilog（`-well_connections true` 同时保留阱连接信息），供功耗分析与 PG-LVS 使用。这对应 u4-l7 讲过的"PG 网表"概念。

**ICC2 对照**（u4-l7 已详讲，这里只点出差异）：

[IC Compiler II/PnR.tcl:185-210](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L185-L210) —— ICC2 极简模板出 `report_design/report_timing/report_power` 三份报告 + SPEF + 两份 Verilog（PG 与非 PG）+ GDS。它**没有** DEF/LEF/SDF/DB/电源网表这些——再次体现"极简可跑"与"完整交付"的差距。

#### 4.5.4 代码实践

**实践目标**：在两份脚本里盘点各自产出的交付物清单。

**操作步骤**：

1. 在 [4_export.tcl:193-246](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L193-L246) 里列出所有 `write_*` 命令及其输出文件后缀。
2. 在 [PnR.tcl:185-210](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/IC%20Compiler%20II/PnR.tcl#L185-L210) 里做同样的事。
3. 做一张对比表，标出 Mentor 有而 ICC2 极简模板没有的交付物。

**需要观察的现象**：Mentor 产出 `.v / .def / .lef / .gds / .db / .power.v`（及 Tanner 分支的 `.sdf / .spef`）；ICC2 极简模板只产出 `.spef / _pg.v / .v / .gds` 加三份报告。

**预期结果**：能指出 DEF、LEF、SDF、DB、电源网表是 Mentor 多出来的交付物。

#### 4.5.5 小练习与答案

**Q1**：为什么出 GDS 前必须先 `source` 层映射文件？

**A1**：工具内部用层名（如 M1/M2/via1）描述版图，而 GDSII 用"层号 + 数据类型"编号。代工厂只认 GDS 层号。没有映射文件，工具不知道每个内部层该写成哪个 GDS 层号，所以宁可拒绝写 GDS 也不能写出层号错乱的版图。

**Q2**：电源网表（`write_verilog -power`）和普通网表有什么区别，为什么功耗分析需要它？

**A2**：电源网表额外包含 VDD/VSS 等电源/地网络以及阱连接信息，普通网表把这些物理-only 网络排除掉了。功耗分析需要知道电流从哪里流到哪儿、经过哪些电源网络和阱连接，才能算 IR drop 与功耗，所以必须用电源网表。

---

## 5. 综合实践

**任务**：为一份"布线已完成"的设计，仿照 Mentor `4_export.tcl` 的结构，写一份**一页纸的收尾检查清单（checklist）**，要求：

1. 按真实执行顺序列出 5 大步骤：天线修复 → DRC 阈值体检 → filler 插入 → filler 后 DRC 清理 → 金属填充 → 交付物输出。
2. 每一步标注：
   - 对应的**关键命令**（如 `check_antenna`、`fix_antenna -method jumper`、`place_filler_cells`、`clean_drc`、`run_route_timing -mode repair`、`insert_metal_fill`、`write_stream`）。
   - 一个**判断条件 / 跳过条件**（如 `num0 > 100` 跳过收尾、`num1 > num0` 触发清理）。
   - 该步的**物理目的**（一句话，如"降 PAR 防栅氧击穿""保阱连续防 latch-up""凑 CMP 密度"）。
3. 在清单末尾，用一段话回答本讲核心问题：**filler 插入后为什么要重新查 DRC 并可能重跑布线修复？**

**参考骨架**（自己填内容）：

| 步骤 | 关键命令 | 判断/跳过条件 | 物理目的 |
|---|---|---|---|
| 1. 天线修复 | … | `MGC_fix_antenna==true` | … |
| 2. DRC 体检 | … | … | … |
| … | … | … | … |

完成后，把这份清单和 [4_export.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl) 逐行对照，检查有没有漏掉"先全禁 filler 再按需放行""阈值熔断""filler 后三段比较"这些细节。

## 6. 本讲小结

- **天线效应**是制造期可靠性问题：未连通的长金属在等离子刻蚀时收集电荷、抬高电压、击穿栅氧。靠 `check_antenna` 查、`fix_antenna -method jumper`（跳层降 PAR）优先、`-method diode`（反偏二极管泄放）兜底。
- **filler cell** 无逻辑却必须留，为的是阱连续（防 latch-up）、M1 电源轨连续、注入/DRC 连续；多尺寸 filler 可按**百分比算法**（`create_filler_set` + `place_filler_cells -filler_set_percentages`）配比填充。
- **metal fill（dummy metal）** 与 filler 是两回事，它服务于 **CMP 密度规则**；ICC2 的 `write_gds -fill include` 是"filler 进 GDS"，**不是** dummy metal——极简模板没有显式金属填充步骤，Mentor 用 `insert_metal_fill`。
- **DRC 清理**是"改一步、查一步、必要时修一步"的迭代：filler 后用 `num0/num1/num2` 三段比较决定是否 `clean_drc` + `run_route_timing -mode repair` 重跑布线修 DRC；收尾前还有 `num0 > 100` 的**阈值熔断**，错误太多直接跳过收尾。
- **交付物**涵盖 GDSII/Verilog/DEF/LEF/SPEF/SDF/DB/电源网表，分别喂代工厂、LVS、STA、仿真、功耗分析等下游。
- **两份脚本的本质差距**：ICC2 `PnR.tcl` 是"极简可跑"的模板（无天线修复、无 DRC 迭代、交付物少），Mentor `4_export.tcl` 是"生产级、DRC 驱动、完整签核"的流程——本讲的精华大多在后者。

## 7. 下一步学习建议

- **横向扩展**：阅读 [mentor_scripts/3_route.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl)，理解布线阶段如何为收尾"铺路"——收尾的 DRC 问题大多源自布线质量。
- **纵向深入**：本讲的 SPEF 交付物会被 u6-l1 PrimeTime 直接消费做 STA 反标，建议回看 [PrimeTime/pt.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/PrimeTime/pt.tcl) 的 `read_parasitics`，把"PnR 交付 → 签核"这条链路在脑海里接通。
- **下一讲 u9-l2**：将从"芯片收尾"转到 **Cadence SKILL 版图脚本**，看如何用程序化方式在版图上画矩形阵列（`Logo.pl`），那是另一种"直接操作版图数据库"的收尾/定制手段。
- **动手建议**：找一个带天线规则与密度规则的开放 PDK（如 SkyWater 130nm），尝试在开源 OpenROAD/_magic 流程里复现"check antenna → fix → filler → metal fill → DRC"这套收尾闭环，加深对每步物理目的的理解。
