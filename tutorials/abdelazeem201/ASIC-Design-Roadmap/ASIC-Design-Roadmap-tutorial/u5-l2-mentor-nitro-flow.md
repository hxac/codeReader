# Mentor Nitro 参考流程

## 1. 本讲目标

本讲带你走进 **Mentor（现为 Siemens EDA）Nitro** 的官方参考流程（Nitro Reference Flow，NRF）。学完本讲，你应当能够：

- 说出 Nitro 参考流程「import → place → clock → route → export」五阶段的职责，并解释它们之间通过 `.db` 文件形成的线性依赖链。
- 在 `0_import.tcl` 中定位库数据生成、网表读入、MCMM 角设置、约束施加、UPF/电源域建立这几条子链路。
- 在 `1_place.tcl` 中找到时钟 NDR 自动推导与 path group 划分的代码位置，并理解它们对布局/拥塞预估的意义。
- 在 `2_clock.tcl` 中读懂 CTS 引擎配置与 CRPR 设置，区分 pre-CTS 与 post-CTS 两种 CRPR 方法。
- 在 `3_route.tcl` 中解释 `run_route_timing -mode interleave_opt` 的「交错优化」含义。
- 在 `4_export.tcl` 中梳理天线修复、filler 插入、金属填充、DRC 清理、GDS/Verilog 输出的先后顺序。

本讲承接 [u4-l1 ICC2 设计初始化与 MCMM 设置](u4-l1-icc2-setup-mcmm.md)——你已经掌握了 ICC2 的「建库 → 读网表 → MCMM 多角多模」骨架；本讲把同一套思想放到另一个 EDA 工具里横向对照，重点看**流程组织方式**的差异，而不是逐条命令的参数细节。

## 2. 前置知识

在开始前，确保你已理解以下概念（前序讲义均已建立）：

- **PnR 不吃 RTL、只吃网表**：物理设计输入是综合器吐出的门级网表（[u1-l2](u1-l2-asic-flow-panorama.md)）。
- **MCMM（多角多模）**：corner（PVT 工艺角，如 slow/fast）× mode（功能模式）组合成 scenario，是时序分析的最小单元；slow 角盯 setup、fast 角盯 hold（[u3-l1](u3-l1-standard-cell-libraries.md)、[u4-l1](u4-l1-icc2-setup-mcmm.md)）。
- **库的时序面与物理面**：Liberty（`.lib`）给延迟、LEF 给尺寸/引脚，二者合并后才是一个完整的标准单元（[u3-l1](u3-l1-standard-cell-libraries.md)）。
- **CTS / NDR / CRPR**：时钟树综合把 ideal 时钟长成真实缓冲器树；NDR（非默认布线）给时钟网加宽加大间距；CRPR 消除时钟路径重汇聚带来的悲观（[u4-l5](u4-l5-cts.md)）。
- **finishing**：filler cell 补阱连续性、金属填充、写 GDSII（[u4-l7](u4-l7-finishing-output.md)）。

本讲用到几个 **Nitro 专属术语**，先统一解释：

| 术语 | 含义 |
|------|------|
| **NRF** | Nitro Reference Flow，Nitro 官方参考流程，是一套脚本模板 |
| **`fk_msg`** | Nitro 的消息打印命令，类似 `puts`，可分级（info/warning/error）并改 shell 提示符 |
| **`.db`** | Nitro 的设计数据库文件，每个阶段产出一份，承载该阶段的设计状态 |
| **`run_*_timing`** | Nitro 各阶段的「主引擎」命令：`run_place_timing` / `run_clock_timing` / `run_route_timing` |
| **`load_utils`** | 加载一个 Tcl 工具包（namespace），如 `nrf_utils` 是 NRF 自带的工具函数集 |
| **PTF** | Parasitic Technology File，Nitro 的寄生工艺文件，对应 Synopsys 的 TLU+ |
| **RCD** | Nitro 内部的延迟/寄生计算模型，分 `pre_cts` / `postcts` / `post_route` 多个阶段档位 |

## 3. 本讲源码地图

本讲涉及的关键文件都在 `mentor_scripts/` 目录下：

| 文件 | 作用 |
|------|------|
| [mentor_scripts/0_import.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl) | **import 阶段**：读库/网表/约束，建立 MCMM 角与电源域，产出 `import.db` 与库 db |
| [mentor_scripts/1_place.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl) | **place 阶段**：布局与 pre-CTS 时序优化，设 NDR 与 path group，产出 `place.db` |
| [mentor_scripts/2_clock.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl) | **clock 阶段**：时钟树综合（CTS）+ post-CTS CRPR，产出 `clock.db` |
| [mentor_scripts/3_route.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl) | **route 阶段**：track/detail 布线 + interleave 优化，产出 `route.db` |
| [mentor_scripts/4_export.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl) | **export 阶段**：天线/filler/金属填充/DRC 清理/GDS 输出，产出 `export.db` |
| [mentor_scripts/createpathgroup.tcl](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl) | path group 划分的样例脚本（`f2f/i2f/f2o/i2o` 四组），辅助理解 place 阶段的 `configure_path_groups` |

> ⚠️ **仓库异常提示**：`0_import.tcl` 全文被复制了两遍——第 1–422 行与第 424–847 行内容完全相同。本讲所有行号引用都指向**第一份副本（1–422 行）**。这是仓库本身的冗余，不是你的阅读问题。

> ⚠️ **可运行性提示**：这五个脚本都依赖 `import_variables.tcl` / `flow_variables.tcl`（定义 `MGC_*` 变量）以及 `scr/` 下的配套脚本（`kit_utils.tcl`、`ocv.tcl`、`config.tcl` 等），本仓库**并未提供**这些依赖，也无法提供 Nitro 许可证。因此它们是**阅读型脚本**，本讲的实践以「源码阅读 + 画图」为主，凡涉及真实运行结果均标注「待本地验证」。

## 4. 核心概念与源码讲解

在进入每个阶段之前，先掌握一条贯穿全局的主线。

**Nitro 把整个 PnR 流程切成五个独立的 Tcl 脚本**，每个脚本的骨架完全一致：

```
1. 打印 banner，置变量 set MGC_flowStage <阶段名>
2. source 变量文件 + load_utils nrf_utils
3. read_db 读入上一阶段的 .db
4. 一大段「配置」：check_design / configure_scenarios / configure_target_libs / OCV / CRPR
5. run_*_timing  ← 该阶段真正的优化引擎，整个脚本最耗时的一行
6. write_db 写出本阶段的 .db，write_reports 出报告
```

这条骨架里**第 3 步与第 6 步**串起了五阶段的依赖链：上一阶段的 `write_db` 产出，就是下一阶段的 `read_db` 输入。于是形成一条严格的线性流水线：

```
0_import.tcl ──► dbs/import.db
                      │ read_db
1_place.tcl  ──► dbs/place.db
                      │ read_db
2_clock.tcl  ──► dbs/clock.db
                      │ read_db
3_route.tcl  ──► dbs/route.db
                      │ read_db
4_export.tcl ──► dbs/export.db  ──► GDS / Verilog / DEF
```

这种「一阶段一 db」的设计与 ICC2 的 `save_block/open_block`（[u4-l1](u4-l1-icc2-setup-mcmm.md)）异曲同工：**每个阶段都是一个可回退的快照**，某一阶段失败可以只重跑该阶段，而不必从 import 重新开始。下文按这五个阶段依次展开。

### 4.1 import 阶段：库 / 网表 / 约束与库 db 生成

#### 4.1.1 概念说明

import 是整个流程的「装填」阶段——把所有外部输入（工艺库、网表、约束、电源意图）装进 Nitro 的内存模型，并固化成 `dbs/import.db`。它要做四件事：

1. **建库**：把 LEF（物理）+ Liberty（时序）+ PTF（寄生）合并成 Nitro 可用的库，必要时生成一份「库 db」（`libs.db`），让后续阶段不必重复读原始库文件。
2. **读网表**：读综合输出的门级 Verilog，`current_design` 选定顶层。
3. **建 MCMM 角与模式**：定义 analysis corner（slow/fast），把库与寄生绑定到对应角。
4. **读约束 + 建电源域**：按 mode 读 SDC 约束；按是否多电压（MultiVoltage）读 UPF 或建默认电源域。

#### 4.1.2 核心流程

import 阶段的库处理有一个**两分支**逻辑，理解它就理解了「库 db」的存在意义：

```
若 MGC_libDbPath 为空或文件不存在（首次跑）：
    读 LEF tech + LEF cell + PTF + Liberty
    preprocess_library
    write_db → libs.db（库）或 import_libs.db（库+设计合并）
否则（已有库 db）：
    read_db MGC_libDbPath          # 直接复用，跳过建库
    软链接到 dbs/libs.db
```

这个分支对应工程上一个非常实际的考量：**读原始库文件很慢**（大工艺库动辄几分钟），所以把它做成一份独立的 `libs.db`，多次跑流程时只建一次库、之后各阶段直接 `read_db` 复用。

随后是网表与约束：

```
set_analysis_corner（逐角绑定 library/process/rc_temp）   # MCMM
read_verilog 网表 → current_design
define_design_mode / set_design_mode                       # 模式
read_constraints -modes <mode>                             # 约束按模式读
（多电压）source UPF → enable_mv → check_mv               # 电源意图
write_db → dbs/import.db
```

#### 4.1.3 源码精读

**(a) 库 db 的生成分支**——首次运行时读四类库文件并 `write_db` 落盘：

[mentor_scripts/0_import.tcl:88-107](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L88-L107) 读 tech LEF、cell LEF、PTF 寄生、Liberty 时序库；紧接着 [mentor_scripts/0_import.tcl:120-124](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L120-L124) 把库写成 db：

```tcl
if {$MGC_split_db} {
    write_db -data lib -file dbs/libs.db      ;# 库与设计分开存
} else {
    write_db -data all -file dbs/import_libs.db ;# 库+设计合并存
}
```

`MGC_split_db` 决定「库」与「设计」是否分文件存储——分开存的好处是库 db 可跨设计复用，这就是后续阶段里反复出现的 `if { [file exists dbs/libs.db] }` 判断的由来。

**(b) MCMM 角绑定**——把每个 corner 绑定到对应的时序库、寄生工艺与温度：

[mentor_scripts/0_import.tcl:143-156](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L143-L156)：

```tcl
set_analysis_corner fast -enable false
set_analysis_corner slow  -enable false
foreach corner $MGC_corners {
    set_analysis_corner -corner $corner \
        -enable true -setup true -hold true -crpr setup_hold \
        -library [get_objects electrical_lib *$MGC_CornerTiming($corner)*] \
        -process [get_libs -type process -filter ...] \
        -rc_temp $MGC_CornerTemperature($corner)
}
```

这一段是 Nitro 版的 MCMM：先关掉默认的 fast/slow，再对 `MGC_corners` 里的每个角，用通配名 `*$MGC_CornerTiming($corner)*` 选中该角的时序库、用 `-process` 绑寄生工艺、用 `-rc_temp` 绑温度，并 `-setup true -hold true` 同时开 setup/hold 检查。对应 [u4-l1](u4-l1-icc2-setup-mcmm.md) 里 ICC2 的 `create_scenario`。

**(c) 约束按模式读**——约束文件按 mode 分别加载：

[mentor_scripts/0_import.tcl:325-337](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L325-L337)：

```tcl
foreach mode $MGC_modes {
    read_constraints -modes $mode \
        -file $MGC_importConstraintsFile($mode) \
        -clock_suffix _${mode}
}
```

注意 `-clock_suffix _${mode}`：同一个时钟在不同模式下会被加上模式后缀，避免多模式约束互相覆盖。

**(d) UPF / 电源域**——根据是否多电压走三条不同路径：

[mentor_scripts/0_import.tcl:346-383](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L346-L383) 展示了三选一逻辑：①`MGC_MultiVoltage` 为真 → `source UPF` + `enable_mv` + `check_mv`；②非多电压但提供了 UPF → 只读 UPF 表达电源意图；③都没有 → `create_power_domain -domain DEFAULT_PD` 建一个默认电源域。这与 [u7-l1](u7-l1-upf-low-power.md) 讲的 UPF 概念对接——Nitro 的 UPF 命令名（`create_power_domain` / `create_supply_net` / `set_domain_supply_net`）与 Synopsys 完全一致，因为 UPF 是 IEEE 标准。

最后 [mentor_scripts/0_import.tcl:397-404](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L397-L404) `write_db -data design -file dbs/import.db` 产出本阶段成果，交给 place 阶段。

#### 4.1.4 代码实践

**实践目标**：跟踪 import 阶段「库 db 复用」的两分支逻辑。

**操作步骤**：

1. 打开 `0_import.tcl`，定位第 68 行的 `if {$MGC_libDbPath =="" ...}`。
2. 沿 if 分支（68–124 行）列出建库时读了哪几类文件、最后写了哪个 db。
3. 沿 else 分支（125–141 行）看复用已有库 db 时做了什么（`read_db` + `file link -symbolic`）。

**需要观察的现象**：else 分支里 [mentor_scripts/0_import.tcl:134-140](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/0_import.tcl#L134-L140) 用 `file mkdir dbs` + `file link -symbolic dbs/libs.db $MGC_libDbPath` 建了一个**符号链接**。

**预期结果**：你会理解为什么后续四个阶段开头都有 `if { [file exists dbs/libs.db] }`——它们不关心库 db 是真实文件还是符号链接，只要 `dbs/libs.db` 这个路径存在即可。

**待本地验证**：符号链接的具体行为需在有 Nitro 的 Linux 环境验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 import 阶段要把库单独写成 `libs.db`，而不是和设计一起写成一份？

**参考答案**：因为同一个工艺库会被一个项目里的多个设计、以及多次流程重跑复用。把库独立成 `libs.db` 后，建库（读大量 LEF/Liberty）这件慢事只需做一次；之后所有设计、所有阶段都直接 `read_db` 这一份库 db，大幅节省时间。这就是 `MGC_split_db` 选项的设计动机。

**练习 2**：`set_analysis_corner` 里同时设了 `-setup true -hold true`，而本仓库 ICC2 的做法是 slow 角盯 setup、fast 角盯 hold（[u4-l1](u4-l1-icc2-setup-mcmm.md)）。两者矛盾吗？

**参考答案**：不矛盾。Nitro 这里对每个角都「开启」setup 与 hold 两种检查能力（允许在该角上算两种违例），至于哪个角最终用于 setup 签核、哪个用于 hold 签核，由后续 `configure_scenarios` 按阶段进一步裁剪（如 place 阶段的 `$MGC_activeCorners(place:hold)`）。开能力 ≠ 强制只用某一种。

### 4.2 place 阶段：NDR 与 path group

#### 4.2.1 概念说明

place 阶段把标准单元摆到合法位置并做 pre-CTS 时序优化，主引擎是 `run_place_timing`。本模块聚焦它**在跑引擎之前**做的两件特别的事：

- **时钟 NDR（非默认布线）**：在布局阶段就给时钟网预设好加宽/加间距的布线规则，让拥塞预估更接近 CTS 后的真实情况。这与 [u4-l5](u4-l5-cts.md) 在 ICC2 里讲过的 NDR 概念一致，只是 Nitro 用一个工具函数 `setup_clock_ndr_and_shield` 自动从工艺推导。
- **path group（路径分组）**：把设计里的时序路径按「起止类型」分成 `f2f / i2f / f2o / i2o` 四组，让优化器对每组单独分配优化预算，避免一条长路径吞光所有资源。

此外 place 阶段还定了 pre-CTS 的 CRPR 方法为 `margin_based`（见 4.3 节对比）。

#### 4.2.2 核心流程

```
read_db dbs/import.db                  # 接力上一阶段
configure_path_groups                  # 划分 f2f/i2f/f2o/i2o
setup_clock_ndr_and_shield             # 从工艺推导时钟 NDR + 屏蔽
set_rcd_models -stage pre_cts          # 选 pre-CTS 延迟模型
set_crpr_spec -method margin_based     # pre-CTS 用基于 margin 的 CRPR
source scr/ocv.tcl                     # OCV 降额（POCV → graph_based）
run_place_timing -effort $MGC_flow_effort   # 引擎
write_db → dbs/place.db
```

#### 4.2.3 源码精读

**(a) path group 划分**——一行工具函数调用，背后是经典的四组划分：

[mentor_scripts/1_place.tcl:114](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L114)：

```tcl
nrf_utils::configure_path_groups
```

这个 proc 的逻辑由同目录样例 [mentor_scripts/createpathgroup.tcl:6-9](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/createpathgroup.tcl#L6-L9) 完整展示：

```tcl
group_path -name f2f -from [all_registers] -to [all_registers] -critical_range 0.7
group_path -name i2f -from $unq_input_list  -to [all_registers] -critical_range 0.7
group_path -name f2o -from [all_registers] -to [all_outputs]    -critical_range 0.7
group_path -name i2o -from $unq_input_list  -to [all_outputs]    -critical_range 0.7
```

四组含义：`f2f`（flip-flop→flip-flop，片内寄存器到寄存器）、`i2f`（input→寄存器）、`f2o`（寄存器→output）、`i2o`（input→output，纯组合路径）。注意脚本第 1–5 行先把时钟端口从 `all_inputs` 里剔除，避免把时钟当普通输入路径分组。`-critical_range 0.7` 表示「距最差路径 0.7ns 以内的路径都算关键路径，一并优化」。

**(b) 时钟 NDR 自动推导**——带优先级的 fallback 链：

[mentor_scripts/1_place.tcl:127-132](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L127-L132)：

```tcl
if { $MGC_customNdrFile == "" } {
    fk_msg -type info "Deriving Clock NDR from technology."
    if { ![nrf_utils::setup_clock_ndr_and_shield] } {
         return -code error
    }
}
```

脚本顶部 [mentor_scripts/1_place.tcl:54-57](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L54-L57) 与这里的注释（119–126 行）共同说明了优先级：**① 用户提供自定义 NDR 文件 `MGC_customNdrFile` → ② 否则用具名规则 `MGC_CLOCK_NDR_NAME` → ③ 否则用倍率 `MGC_NdrWidthMultiplier` / `MGC_NdrSpaceMultiplier` 从工艺默认宽度/间距派生**。这正是 [u8-l1 NDR_rule.pl](u8-l1-ndr-rule-automation.md)「按倍率缩放」思想的工具内置版。NDR 限定层范围为 `MGC_CtsBottomPreferredLayer ~ MGC_MaxRouteLayer`，屏蔽线由 `MGC_applyShield` + `MGC_clock_shield_net` 控制。

**(c) pre-CTS 的 CRPR 与延迟模型**：

[mentor_scripts/1_place.tcl:96](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L96) `set_crpr_spec -method margin_based`，[mentor_scripts/1_place.tcl:134](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L134) `set_rcd_models -stage pre_cts`。pre-CTS 阶段时钟还是 ideal（直通），CRPR 用简单的 `margin_based`（基于裕量）即可；CTS 之后才切到更精确的 `graph_based`（见 4.3 节）。

**(d) 主引擎**：

[mentor_scripts/1_place.tcl:201](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L201)：

```tcl
run_place_timing -effort $MGC_flow_effort \
    -skip_precondition $MGC_skip_precondition -messages verbose
```

最后 [mentor_scripts/1_place.tcl:210-215](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L210-L215) `write_db` 产出 `place.db`。

#### 4.2.4 代码实践

**实践目标**：理解 path group 的四组划分如何覆盖所有时序路径。

**操作步骤**：

1. 打开 `mentor_scripts/createpathgroup.tcl`，阅读第 1–9 行。
2. 想象一个有 `clk`、若干输入端口、若干输出端口、若干寄存器的设计。
3. 任意一条时序路径，其起点必然是「输入端口 or 寄存器」，终点必然是「寄存器 or 输出端口」。

**需要观察的现象**：起止各 2 种可能，组合出 2×2=4 类路径，正好对应 `f2f / i2f / f2o / i2o`。

**预期结果**：你能解释为什么没有「时钟路径」这一组——因为脚本开头已把时钟端口从 `$unq_input_list` 里 `remove_from_collection` 移除，时钟路径由 CTS 单独处理。

#### 4.2.5 小练习与答案

**练习 1**：place 阶段为什么要提前给时钟网设 NDR，而 CTS 阶段（4.3 节）开头又会把 NRF 自动生成的 NDR 清掉？

**参考答案**：place 阶段提前设 NDR 是为了**让拥塞预估更真实**——时钟网占了加宽的布线资源，拥塞图才不会过度乐观；这是「预估」用途。CTS 阶段（`2_clock.tcl` 第 178–183 行）要真正长缓冲器树，此时它的缓冲器位置、走线都已确定，需要按 CTS 引擎自己的策略重新布时钟网，所以先 `set_property -name nondefault_rule -value ""` 把之前预估用的 NDR 清掉（只清 `MGC_CLK_NDR_*` 自动规则，保留用户自定义规则），再由 CTS 引擎重新施加。

**练习 2**：`-critical_range 0.7` 改大会怎样？

**参考答案**：`critical_range` 是「关键路径窗口」。改大意味着距最差路径更远的路径也被纳入优化，WNS（最差负裕量）改善不明显时 TNS（总负裕量）会更好，但优化耗时增加。这是个典型的时序质量 vs 运行时间的权衡旋钮。

### 4.3 clock 阶段：CTS 与 CRPR

#### 4.3.1 概念说明

clock 阶段做时钟树综合（CTS）+ post-CTS 优化，主引擎是 `run_clock_timing`。它要把 ideal（直通）时钟长成由缓冲器/反相器逐级扇出的真实树。本模块聚焦两点：

- **CTS 引擎调参**：skew 阈值、缓冲器剪枝目标等。
- **post-CTS 的 CRPR 方法切换**：从 place 阶段的 `margin_based` 切到更精确的 `graph_based`，因为此时时钟树已经是真实拓扑，可以沿图消除重汇聚悲观。这与 [u4-l5](u4-l5-cts.md) 讲的 CRPR 概念一致。

#### 4.3.2 核心流程

```
read_db dbs/place.db
设默认：cts_repeater_pruning_objective=delay、skew 阈值 0.50 等
清掉 place 阶段预估用的时钟 NDR（保留用户自定义）
OCV + set_crpr_spec -method graph_based   # ← 关键切换
HOLD：dont_use DLY_cells、config_flows -hold_opt_cell_list
run_clock_timing                          # CTS 主引擎
insert_tie_cells                          # 插入 tie high/low
write_db → dbs/clock.db
```

#### 4.3.3 源码精读

**(a) CTS 引擎默认调参**：

[mentor_scripts/2_clock.tcl:67-79](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L67-L79)：

```tcl
set MGC_cts_repeater_pruning_objective delay ;# 剪枝目标：延迟优先
set cts_partition_skew_threshold 0.50         ;# 分区 skew 阈值
set cts_compile_local_refine_skew_factor 0.30 ;# 局部细化 skew 因子
```

这些是 CTS 在「分区→缓冲→细化」流程里的旋钮：`cts_partition_skew_threshold` 控制何时为减小 skew 而插入缓冲器。

**(b) 清掉预估用 NDR，再由 CTS 重施**：

[mentor_scripts/2_clock.tcl:178-183](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L178-L183)：

```tcl
set candidate_nets [filter_collection [collect_clock_nets] -expression "@nondefault_rule~=MGC_CLK_NDR_*"]
if {[sizeof_collection $candidate_nets] > 0} {
    set_property -name nondefault_rule -value "" -object $candidate_nets
    set_property -name shield_rule    -value "" -object $candidate_nets
}
```

通配 `MGC_CLK_NDR_*` 只匹配 NRF 自动生成的规则，用户自定义规则不在此范围，故得以保留——这就是 4.2 练习 1 答案里提到的「只清自动、保留自定义」。

**(c) post-CTS 的 CRPR 切换**——本模块核心：

[mentor_scripts/2_clock.tcl:148-149](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L148-L149)：

```tcl
set_crpr_spec -method graph_based
set_crpr_spec -crpr_threshold $MGC_crpr_threshold
```

对比 place 阶段的 `margin_based`：CTS 之前时钟是 ideal、没有真实拓扑，CRPR 只能用一个固定裕量近似；CTS 之后时钟树已落地成真实的 buffer 链，工具能沿时钟路径**图**找到公共树干、精确抵消重汇聚点的 max/min 双重悲观，故切到 `graph_based`。`crpr_threshold` 控制多长的公共路径才值得做 CRPR 抵消。

**(d) HOLD 优化单元池**：

[mentor_scripts/2_clock.tcl:155-156](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L155-L156)：

```tcl
set_property -name is_dont_use -value true -objects [get_lib_cells $MGC_DLY_cells]
config_flows -hold_opt_cell_list [ get_lib_cells $MGC_DLY_cells ]
```

这两行看似矛盾实则配合：先把 `$MGC_DLY_cells`（延迟单元）全局标 `dont_use`，不让 place/CTS 随便用；再用 `-hold_opt_cell_list` 显式把它们指定为 **hold 修复专用单元池**——即平时禁用，只在修 hold 违例时插。这保证 hold 用最干净的延迟链而不是任意缓冲器。

**(e) 主引擎与收尾**：

[mentor_scripts/2_clock.tcl:203](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L203) `run_clock_timing -cpus $MGC_cpus`；随后 [mentor_scripts/2_clock.tcl:206-211](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L206-L211) `nrf_utils::insert_tie_cells` 插 tie high/low 单元锁死悬空输入，最后 `write_db` 出 `clock.db`。

#### 4.3.4 代码实践

**实践目标**：对比 place 与 clock 两阶段的 CRPR 方法，理解为何要切换。

**操作步骤**：

1. 在 `1_place.tcl` 第 96 行确认 pre-CTS 用 `set_crpr_spec -method margin_based`。
2. 在 `2_clock.tcl` 第 148 行确认 post-CTS 切到 `set_crpr_spec -method graph_based`。
3. 进一步看 [mentor_scripts/2_clock.tcl:143-149](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L143-L149)：若启用了 POCV（`$MGC_OCV=="pocv"`），也会强制 `graph_based`。

**需要观察的现象**：CRPR 方法不是一成不变，而是随「时钟是否已成真实树」而演进。

**预期结果**：你能用一句话回答「为什么 pre-CTS 用 margin、post-CTS 用 graph」——因为 CRPR 的本质是消除时钟重汇聚的 max/min 悲观，ideal 时钟没真实拓扑只能用裕量近似，真实树落地后才能沿图精确抵消。

#### 4.3.5 小练习与答案

**练习 1**：为什么把 `$MGC_DLY_cells` 同时设成 `dont_use` 又放进 `hold_opt_cell_list`？

**参考答案**：`dont_use` 是对**常规优化**全局禁用，避免布局/CTS 出于时序目的乱插延迟单元造成面积/功耗浪费；`hold_opt_cell_list` 是对**hold 修复**白名单豁免，只允许在修 hold 违例时使用。两者叠加 = 「平时禁用、修 hold 时专用」，让 hold 修复用最可控的延迟链。

**练习 2**：`run_clock_timing` 之后为什么还要 `insert_tie_cells`？

**参考答案**：某些标准单元的输入可能在某些模式下悬空（无驱动），悬空输入会引入噪声与漏电。tie high/low 单元把这些悬空输入钳位到稳定电平。CTS 之后、布线之前插 tie cell 是常见时机，因为此时单元布局已稳定但金属还未密集，插入代价最低。

### 4.4 route 阶段：interleave 优化

#### 4.4.1 概念说明

route 阶段做 track/detail 布线 + 时序驱动布线优化，主引擎是 `run_route_timing`。本模块聚焦它的一种特殊模式——**interleave（交错）优化**。

「交错」指的是把「布线」与「时序/寄生提取优化」**交替**进行，而不是「先全部布完线、再统一算寄生、再修时序」。布一段→提一次寄生→算一次时序→修一次→再布下一段，如此迭代。好处是：每段布线后立刻用真实（而非估算）寄生反馈给时序引擎，避免「布到底才发现时序崩了」的返工。

route 阶段还会把 RCD 延迟模型从 `postcts` 切到 `post_route`（注释见 [mentor_scripts/3_route.tcl:93-95](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L93-L95)），因为此时寄生已是真实布线寄生。

#### 4.4.2 核心流程

```
read_db dbs/clock.db
config_lib_vias、configure_scenarios/target_libs
set_crpr_spec -method graph_based           # 延续 CTS 的图法 CRPR
config_flows -preserve_rcd false            # 允许布线内部切换 rcd 模型
config_route_timing -reset_all              # 清掉旧配置
（MGC_rrt_opt 为真时）进入 interleave 分支：
    config_extraction（厚度变化）
    set_max_length
    run_route_timing -mode interleave_opt   # ← 交错优化
write_db → dbs/route.db
```

#### 4.4.3 源码精读

**(a) 两分支：interleave vs 普通**：

[mentor_scripts/3_route.tcl:128-145](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L128-L145)：

```tcl
if {$MGC_rrt_opt} {
    config_extraction -use_thickness_variation false
    config_extraction -use_thickness_variation_for_cap false
    config_extraction -use_thickness_variation_for_res true
    set_crpr_spec -crpr_threshold $MGC_crpr_threshold
    set_crpr_spec -method graph_based
    set_crpr_spec -transition same_transition
    set_max_length -length_threshold $MGC_maxLengthParam
    fk_msg "Running RRT with Interleave Optimization"
    run_route_timing -mode interleave_opt -cpus $MGC_cpus -messages verbose
} else {
    run_route_timing -cpus $MGC_cpus -messages verbose
}
```

`MGC_rrt_opt`（在脚本第 63 行被硬编码为 `true`）决定走不走交错分支。`-mode interleave_opt` 就是交错优化；`config_extraction` 一组配置控制寄生提取是否考虑金属厚度变化（电阻考虑、电容不考虑，是先进节点的常见取舍）。`set_crpr_spec -transition same_transition` 进一步约束 CRPR 用「相同翻转」匹配，提升精度。

**(b) DFM via 替换**——布线时把普通 via 换成 DFM（可制造性）via：

[mentor_scripts/3_route.tcl:101-103](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L101-L103) `config_route_timing -name dvia_mode -value dfm`，由 `$MGC_replace_dfm` 开关控制。

**(c) 合法性兜底**——布线前检查是否有非法（重叠/越界）单元：

[mentor_scripts/3_route.tcl:111-118](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L111-L118) 用 `get_illegal_cells` 取非法单元并告警，避免带着布局错误强行布线。

最后 [mentor_scripts/3_route.tcl:149-154](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L149-L154) `write_db` 出 `route.db`。

#### 4.4.4 代码实践

**实践目标**：用「换参数」理解 interleave 模式的开关。

**操作步骤**：

1. 在 `3_route.tcl` 第 63 行找到 `set MGC_rrt_opt true`。
2. 假设你把它改成 `false`，跟踪第 128 行 `if {$MGC_rrt_opt}` 的走向。
3. 对比 interleave 分支（130–142 行）与 else 分支（144 行）的差异。

**需要观察的现象**：关掉 `MGC_rrt_opt` 后，`config_extraction`、`set_max_length`、CRPR 精化等一整套配置都被跳过，`run_route_timing` 退化为不带 `-mode interleave_opt` 的普通布线。

**预期结果**：你能解释 `-mode interleave_opt` 不是孤立开关——它伴随一整套「为精确寄生/CRPR 服务」的配置，关掉它意味着回到「先布完再统一修时序」的粗放模式，运行更快但时序质量略差。

**待本地验证**：两种模式的实际 WNS/TNS 差异需在 Nitro 中跑真实设计对比。

#### 4.4.5 小练习与答案

**练习 1**：route 阶段的 `config_flows -preserve_rcd false`（第 95 行）与 place 阶段的 `config_flows -preserve_rcd true`（[1_place.tcl:151](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L151)）相反，为什么？

**参考答案**：RCD 模型有阶段档位（pre_cts/postcts/post_route）。place 阶段 `preserve_rcd true` 是要**保留** pre-CTS 的延迟模型不被内部流程覆盖；route 阶段布线过程会把寄生从估算升级为真实布线寄生，需要 `preserve_rcd false` **允许**内部把模型从 postcts 切到 post_route（注释 93–95 行正是此意）。一句话：place 要「锁住」模型，route 要「放行」模型升级。

**练习 2**：为什么寄生提取里电阻考虑金属厚度变化、电容却不考虑？

**参考答案**：金属厚度变化对**电阻**影响显著（厚度变薄→截面积变小→电阻变大），必须建模；而对**电容**的影响相对次要（电容主要由线宽与间距决定），且厚度变化的电容建模成本高、收益小，故常关闭。这是精度 vs 运行时间的工程取舍。

### 4.5 export 阶段：天线 / 填充 / 金属填充 / GDS

#### 4.5.1 概念说明

export 是收尾与交付阶段，**不跑主引擎**（没有 `run_*_timing` 的常规调用，只在修 DRC 时调 `run_route_timing -mode repair`）。它把布线后的设计加工成可流片的交付物，依次做四件事，顺序很关键：

1. **天线修复**：长金属线在工艺中会像天线一样收集电荷，击穿栅氧。用 jumper（跳线换层）或 diode（二极管）修复。
2. **filler cell 插入**：补满行间隙，保证阱连续性与电源轨贯通（概念见 [u4-l7](u4-l7-finishing-output.md)）。
3. **金属填充**：补金属密度，满足化学机械抛光（CMP）要求。
4. **输出**：Verilog/DEF/LEF + GDSII。

#### 4.5.2 核心流程

```
read_db dbs/route.db
check_antenna → fix_antenna -method jumper（必要时 +diode）
check_drc（超阈值则跳过收尾）
place_filler_cells（支持百分比 filler set）
check_unfilled_gaps → check_drc → clean_drc → run_route_timing -mode repair
insert_metal_fill
write_verilog / write_def / write_lef     # 输出网表/版图
write_stream -format gds                  # GDSII
write_db → dbs/export.db
propagate_power_and_ground_nets → write_verilog -power（电源网表）
```

#### 4.5.3 源码精读

**(a) 天线修复**——先 jumper 后 diode 的两级策略：

[mentor_scripts/4_export.tcl:71-81](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L71-L81)：

```tcl
if {$MGC_fix_antenna eq true} {
    check_antenna -cpus $MGC_cpus
    fix_antenna -method jumper -cpus $MGC_cpus      ;# 先用跳线换层
    if {$MGC_fix_antenna_use_diodes eq true} {
        config_antenna -diodes $MGC_fix_antenna_diodes
        fix_antenna -method diode -finish all       ;# 跳线修不掉再上二极管
    }
}
```

`jumper`（把长线在中段换到上层金属短路掉，减小天线比）成本低、优先用；`diode`（在栅极旁插二极管泄放电荷）面积代价大、作为兜底。

**(b) DRC 阈值兜底**——错误太多就放弃收尾，避免在「烂版图」上白费功夫：

[mentor_scripts/4_export.tcl:84-91](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L84-L91) 统计 DRC 错误数 `num0`，超过阈值（默认 100）则 `set skip_chip_finishing true`，后续 filler 与金属填充都跳过。

**(c) filler 插入**——支持「百分比 filler set」算法：

最简单的全量插入在 [mentor_scripts/4_export.tcl:107](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L107) `place_filler_cells -lib_cells $lc_list`；更精细的百分比算法在 [mentor_scripts/4_export.tcl:118-145](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L118-L145)，用 `create_filler_set` 给每种 filler 设一个占比，再 `place_filler_cells -filler_set_percentages` 按比例填。这与 [u4-l7](u4-l7-finishing-output.md) 讲的 ICC2 `create_stdcell_fillers` 思路一致，只是 Nitro 多了百分比分配。

**(d) filler 后的 DRC 二次清理**——filler 可能动出新的 DRC，要复查并修：

[mentor_scripts/4_export.tcl:170-185](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L170-L185)：

```tcl
check_drc
set num1 [llength [get_objects -type error -filter @category==drc]]
if { $num1 } {
    clean_drc
    run_route_timing -mode repair -cpus $MGC_cpus \
        -user_params "-drc_effort high -drc_accept number -dp_local_effort high"
    check_drc
}
```

`run_route_timing -mode repair` 是 export 阶段唯一的「引擎」调用，专门做 DRC 修复型局部重布。这就是 [u9-l1](u9-l1-chip-finishing-antenna.md) 讲的「filler 后要再 check_drc 并可能 route repair」的来源。

**(e) 金属填充与输出**：

[mentor_scripts/4_export.tcl:188-191](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L188-L191) `insert_metal_fill`；[mentor_scripts/4_export.tcl:213-215](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L213-L215) 输出三件套：

```tcl
write_verilog -file $dataDir/${design}.v.gz
write_def     -file $dataDir/${design}.def.gz
write_lef     -file $dataDir/${design}.lef.gz -lib_cells [get_lib_cells -of_objects [get_top_partition]]
```

**(f) GDS 与电源网表**：

[mentor_scripts/4_export.tcl:221-226](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L221-L226) 需要 `MGC_gds_layer_map_file` 做 Nitro 层 → GDS 层号的映射，再 `write_stream -format gds`；[mentor_scripts/4_export.tcl:242-243](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L242-L243) 输出电源网表：

```tcl
propagate_power_and_ground_nets
write_verilog -file $dataDir/${design}.power.v.gz -power true -well_connections true
```

这一份 `-power true` 的网表保留电源/阱连接，供电源分析与 PG-LVS 用，对应 [u4-l7](u4-l7-finishing-output.md) 讲的「PG 网表 vs 非 PG 网表」两种视图。

#### 4.5.4 代码实践

**实践目标**：梳理 export 阶段「检查—修复—复查」的嵌套节奏。

**操作步骤**：

1. 在 `4_export.tcl` 里数 `check_drc` 出现的次数与位置（第 85、170、180 行）。
2. 对照第 85 行（filler 前，得 `num0`）、第 170 行（filler 后，得 `num1`）、第 180 行（repair 后，得 `num2`）。
3. 注意第 173 行 `if { $num1 > $num0 }` 的判断：filler 后错误数不降反升时，日志措辞会不同。

**需要观察的现象**：DRC 检查不是一次性的事，而是「天线后查一次、filler 后查一次、repair 后再查一次」，每次都用前一次的错误数做基线对比。

**预期结果**：你能解释为什么 filler 插入会引入新 DRC——filler 单元挤进间隙可能挤占已有布线的间距，触发新的间距/短路违例，所以必须复查并用 `run_route_timing -mode repair` 局部修复。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `fix_antenna` 先用 jumper、后用 diode，而不是直接全用 diode？

**参考答案**：jumper 通过把长金属线在中段换层（如跳到高层金属再回来）来切断天线收集路径，几乎不增加单元、面积代价小，能修掉大多数天线违例；diode 要在栅极旁插实体二极管单元，面积与漏电代价大。先用低成本手段修掉大部分，剩下的硬骨头再用 diode 兜底，是典型的「成本递增」修复策略。

**练习 2**：`write_stream`（写 GDS）为什么必须依赖 `MGC_gds_layer_map_file`？

**参考答案**：GDSII 用数字层号（如 1, 2, 11, …）标识掩模层，而 Nitro 内部用字符串层名（如 M1, M2, VIA1）。映射文件把两者对应起来，`write_stream` 才能把 Nitro 的形状正确翻译成 GDS 的数字层。缺了映射文件，GDS 的层归属会错乱，代工厂无法正确制版，故脚本在缺失时只告警、不写 GDS。

## 5. 综合实践

**任务**：画出 Nitro 五阶段之间的 `.db` 依赖关系图，并标注每个阶段的「主引擎命令」与「关键前置/收尾动作」。

**操作步骤**：

1. 准备一张白纸或绘图工具。
2. 画出五个节点：`import.db`、`place.db`、`clock.db`、`route.db`、`export.db`，用箭头连成线性链。
3. 在每个节点上方标注该阶段的主引擎（import 无引擎、`run_place_timing`、`run_clock_timing`、`run_route_timing -mode interleave_opt`、export 无常规引擎但有 `run_route_timing -mode repair`）。
4. 在每条「阶段脚本开头」标注它读的是哪个 db（参考 [1_place.tcl:39-40](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/1_place.tcl#L39-L40) 读 `import.db`、[2_clock.tcl:39-40](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/2_clock.tcl#L39-L40) 读 `place.db`、[3_route.tcl:36-37](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/3_route.tcl#L36-L37) 读 `clock.db`、[4_export.tcl:41-42](https://github.com/abdelazeem201/ASIC-Design-Roadmap/blob/795d32ab6b5ba111e6eab968850671d824569692/mentor_scripts/4_export.tcl#L41-L42) 读 `route.db`）。
5. 标注库 db 的旁路：`libs.db` 是 import 阶段单独产出、被所有后续阶段共享的库数据。
6. 在每个节点下方标注一个该阶段的「特色动作」：import（MCMM+UPF）、place（NDR+path group）、clock（CRPR 切 graph_based）、route（interleave）、export（天线+filler+GDS）。

**参考答案**（文字版）：

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  import.db  │────►│  place.db   │────►│  clock.db   │────►│  route.db   │────►│  export.db  │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │                   │                   │
  read_db            run_place_timing    run_clock_timing   run_route_timing   fix_antenna/
  read_verilog                                          -mode interleave    place_filler/
  set_analysis_                                                                metal_fill/
  corner+UPF                                                                   write_gds
       ▲
       │ 所有阶段共享
   ┌───┴────┐
   │ libs.db │  ← import 阶段单独产出（库数据），split_db 时存在
   └────────┘
```

**预期结果**：你能指着图说明「如果 route 阶段失败，只需修复后从 `clock.db` 重跑 route，不必回到 import」——这正是 `.db` 线性链的可回退价值，对应 ICC2 的 `save_block` 版本管理（[u4-l1](u4-l1-icc2-setup-mcmm.md)）。

## 6. 本讲小结

- **五阶段线性流水线**：import→place→clock→route→export，靠 `read_db`/`write_db` 串成 `import.db→place.db→clock.db→route.db→export.db` 的依赖链，每阶段一个可回退快照。
- **统一骨架**：五脚本结构一致——置 `MGC_flowStage` → source 变量 → 读上阶段 db → 配置（scenarios/OCV/CRPR）→ `run_*_timing` → 写本阶段 db。
- **import** 读四类库（LEF/PTF/Liberty）生成可复用的 `libs.db`，用 `set_analysis_corner` 建 MCMM 角，按 mode 读约束，按多电压读 UPF。
- **place** 提前从工艺推导时钟 NDR 以真实评估拥塞，并用 `configure_path_groups` 把路径分成 `f2f/i2f/f2o/i2o` 四组；pre-CTS 用 `margin_based` CRPR。
- **clock** 把 CRPR 切到 `graph_based`（时钟已成真实树），用 DLY 单元做 hold 修复专用池，主引擎 `run_clock_timing`。
- **route** 的 `run_route_timing -mode interleave_opt` 让布线与寄生/时序优化交替迭代，用真实布线寄生反馈，时序质量更高。
- **export** 按「天线修复→filler→DRC 复查/repair→金属填充→输出」顺序收尾，产出 Verilog/DEF/LEF/GDSII 及电源网表。

## 7. 下一步学习建议

- 想深入签核侧的 STA，请读 [u6-l1 PrimeTime STA 基本流程](u6-l1-primetime-sta-flow.md)——export 产出的网表与 SPEF 正是 PrimeTime 的输入。
- 想理解天线/filler/金属填充的工程细节，请读 [u9-l1 芯片收尾：天线、填充与金属填充](u9-l1-chip-finishing-antenna.md)，它会用 ICC2 视角与本章对照。
- 想看另一套工具的同类流程，回看 [u5-l1 Synopsys ICC 传统流程](u5-l1-icc-legacy-flow.md)，体会三家工具（Synopsys ICC2 / ICC、Mentor Nitro）在「分阶段 + 中间数据库」这一核心思想上的殊途同归。
- 如果你要在本仓库基础上二次开发一套自己的 PnR 脚本，建议精读 `mentor_scripts/nitro.tcl`（一个扁平化的单文件样例）与 `createpathgroup.tcl`，它们比五阶段脚本更短、更适合作为「最小可改模板」。
