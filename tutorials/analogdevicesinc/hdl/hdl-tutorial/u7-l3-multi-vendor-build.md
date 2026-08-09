# 多厂商构建：Intel 与 Lattice 工程

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一个 Intel（Quartus/Qsys）工程和一个 Lattice（Radiant + Propel Builder）工程分别由哪些文件构成、`make` 时分别会调用哪些工具。
- 对照 AMD Xilinx 工程，讲出三家在「入口脚本、约束后缀、块设计脚本、最终产物」上的差异与共性。
- 沿着 `project-intel.mk` / `project-lattice.mk` 的依赖链，解释「库依赖」如何被翻译成不同厂商的库产物（`.timestamp_intel` vs `ltt/metadata.xml` vs `component.xml`）。
- 读 `adi_project_intel.tcl` 与 `adi_project_lattice*.tcl`，理解 Tcl 助手如何把厂商原生命令封装成统一的 `adi_project` 调用风格。

本讲承接 **u3-l2（工程构建 Makefile：project-xilinx.mk）** 与 **u4-l3（Intel 与 Lattice 的 IP 打包）**：前者建立了「工程 Makefile 声明依赖 → 公共脚本翻译成库产物 → 调厂商工具」的骨架，后者说明了库侧三家打包资产的差异。本讲把视角拉回**工程侧**，看这些库产物在 Intel / Lattice 工程里是如何被消费、最终跑出比特流的。

## 2. 前置知识

在进入正题前，先统一几个名词（多数已在 u1-l3 / u2-l1 出现过，这里只补厂商相关部分）：

- **块设计（block design）**：用图形化方式把 IP 拖出来、连线、配参数，最后由工具自动生成一个顶层 Verilog wrapper。三家有不同的名字与脚本后缀——AMD 叫 **Block Design（BD）** 用 `system_bd.tcl`；Intel 叫 **Platform Designer（旧名 Qsys）** 用 `system_qsys.tcl`；Lattice 叫 **Propel Builder** 用 `system_pb.tcl`。
- **综合 / 实现（implementation）/ 比特流**：把 RTL 翻译成网表、再映射布线成可烧录文件的过程。各家产物不同：AMD 出 `.xsa`（含比特流的硬件交付文件）、Intel 出 `.sof`（SRAM Object File，FPGA 直接加载的比特流）、Lattice 出 `.bit`。
- **约束文件后缀**：`.xdc`（AMD Xilinx Constraint）、`.sdc`（Synopsys Design Constraints，Intel/Lattice 都用，描述时钟与时序路径）、`.pdc`（Physical Design Constraints，Lattice 专用的物理/布局约束）。
- **Quartus Pro vs Quartus Standard**：Intel 两条工具线，命令行参数不同；ADI 用 `QUARTUS_PRO_ISUSED` 标志自动切换（见 u1-l3）。
- **riscv-rx**：Lattice CertusPro-NX 器件里自带的软核处理器，对标 AMD 的 MicroBlaze 与 Intel 的 NIOS II。

关键直觉（先记住这张表，后面每一节都在填它）：

| 维度 | AMD Xilinx | Intel | Lattice |
|---|---|---|---|
| 厂商工具 | Vivado | Quartus（`quartus_sh`） | Radiant（`radiantc`）+ Propel Builder（`propelbldc`） |
| 块设计脚本 | `system_bd.tcl` | `system_qsys.tcl` | `system_pb.tcl` |
| 工程驱动脚本 | `system_project.tcl` | `system_project.tcl` | `system_project.tcl`（Radiant）+ `system_project_pb.tcl`（Propel Builder） |
| 时序约束 | `system_constr.xdc` | `system_constr.sdc` | `system_constr.sdc` |
| 物理/布局约束 | （并入 `.xdc`） | （并入 `.sdc` 或 assign） | `system_constr.pdc`（独立） |
| 最终产物 | `system_top.xsa` | `system_top.sof` | `<工程名>.bit`（在 `_bld/.../impl_1/`） |
| 库依赖产物 | `component.xml` | `.timestamp_intel` | `ltt/metadata.xml` |
| 工具生成顶层 | `system_wrapper.v` | `system_bd` | `<工程名>.v` |

## 3. 本讲源码地图

本讲围绕五个核心脚本与两个真实样例工程展开：

| 文件 | 作用 |
|---|---|
| [projects/scripts/project-intel.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk) | 所有 Intel 工程共享的公共 Makefile，把 `LIB_DEPS` 翻译成 `.timestamp_intel`，调用 `quartus_sh` |
| [projects/scripts/project-lattice.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk) | 所有 Lattice 工程共享的公共 Makefile，分 `pb`（块设计）/ `rd`（布线）/ `sge`（打包）三段 |
| [projects/scripts/adi_project_intel.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl) | Intel 侧 Tcl 助手，单个 `adi_project` 过程完成「建工程 + 生成 Qsys + 配赋值」 |
| [projects/scripts/adi_project_lattice.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl) | Lattice 侧 **Radiant** Tcl 助手，`adi_project` / `adi_project_files` / `adi_project_run` |
| [projects/scripts/adi_project_lattice_pb.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl) | Lattice 侧 **Propel Builder** Tcl 助手，`adi_project_pb` 负责块设计生成 |
| [docs/user_guide/architecture.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst) | 官方文档，给出三厂商各自的标准文件清单 |
| [projects/ad469x_evb/de10nano/](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad469x_evb/de10nano/) | Intel 样例工程（DE10-Nano / Cyclone V） |
| [projects/ad738x_fmc/lfcpnx/](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad738x_fmc/lfcpnx/) | Lattice 样例工程（LFCPNX-EVN / CertusPro-NX） |

对比基准则沿用 u3-l2 讲过的 [projects/scripts/project-xilinx.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk)。

---

## 4. 核心概念与源码讲解

### 4.1 Intel Qsys 工程构建

#### 4.1.1 概念说明

Intel 工程的构建链和 AMD 工程在**骨架上几乎一样**：工程 `Makefile` 用 `LIB_DEPS` 报菜名 → 公共脚本 `project-intel.mk` 把每个 IP 翻译成一个库产物 → 调用厂商工具跑综合实现。差异只在三处：

1. **厂商工具**是 `quartus_sh`（Quartus 的 Tcl shell），不是 `vivado`；
2. **库产物**是一个空的时间戳文件 `.timestamp_intel`，而不是内容完整的 `component.xml`；
3. **块设计脚本**叫 `system_qsys.tcl`（Qsys / Platform Designer），不是 `system_bd.tcl`。

第 2 点最值得展开。回顾 u4-l3：Intel 的 IP 用 `*_hw.tcl` 描述，**这个文件是 Qsys 在工程构建时才解释执行的**（动态回调），库侧打包阶段并不真跑 Quartus，所以 `library.mk` 的 `intel` 目标只做一件事——`touch` 出一个 `.timestamp_intel` 表示「这个 IP 的 hw.tcl 已就绪」。换句话说，Intel 走的是「库轻、工程重」：真正的 IP 组装发生在 Qsys 里，而不像 Xilinx 那样在库侧就把 IP 打成 `component.xml` 再被工程引用。

#### 4.1.2 核心流程

Intel 工程从 `make` 到 `.sof` 的链路：

```
make  (工程目录)
  └─ include project-intel.mk
       ├─ M_DEPS 收集 system_top.v / system_qsys.tcl / system_project.tcl / system_constr.sdc / adi_env.tcl
       ├─ 把每个 LIB_DEPS → library/<ip>/.timestamp_intel   （模式规则，FORCE 触发库 make intel）
       └─ 目标 <PROJECT_NAME>.sof : $(M_DEPS)
            └─ quartus_sh --64bit -t system_project.tcl
                 ├─ adi_project <name> [params]        # adi_project_intel.tcl
                 │    ├─ regexp 匹配载板后缀 → family/device
                 │    ├─ 版本校验 (required_quartus_version)
                 │    ├─ 动态生成 system_qsys_script.tcl 并 qsys-generate
                 │    └─ set_global_assignment 注册 system_top.v / system_constr.sdc
                 ├─ source 载板 assign 脚本 + set_location/IO_STANDARD（引脚约束）
                 └─ execute_flow -compile              # 跑综合/适配器/汇编出 .sof
```

注意最后一步 `execute_flow -compile` **不在** `adi_project_intel.tcl` 里，而是写在工程自己的 `system_project.tcl` 末尾——`adi_project` 只负责把工程配好，真正的「编译」由工程脚本收尾。

#### 4.1.3 源码精读

先看公共 Makefile 怎么定义工具与最终产物：

```makefile
INTEL := quartus_sh --64bit -t
...
all: $(PROJECT_NAME).sof
...
$(PROJECT_NAME).sof: $(M_DEPS)
	-rm -rf $(CLEAN_TARGET)
	$(call build, $(INTEL) system_project.tcl, $(PROJECT_NAME)_quartus.log, ...)
```

`quartus_sh --64bit -t` 表示以 64 位模式启动 Quartus 的 Tcl 解释器并执行 `system_project.tcl`；最终产物是 `<工程名>.sof`。见 [project-intel.mk:43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L43)（工具定义）、[project-intel.mk:108](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L108)（`all` 目标）与 [project-intel.mk:124-L129](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L124-L129)（构建规则）。

再看依赖收集——和 Xilinx 版几乎逐行对应，只是块设计脚本换成 `system_qsys.tcl`、约束换成 `.sdc`、库产物换成 `.timestamp_intel`：

```makefile
M_DEPS += $(wildcard system_top*.v)
M_DEPS += system_qsys.tcl
M_DEPS += system_project.tcl
M_DEPS += system_constr.sdc
...
M_DEPS += $(foreach dep,$(LIB_DEPS),$(HDL_LIBRARY_PATH)$(dep)/.timestamp_intel)
```

见 [project-intel.mk:97-L105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L97-L105)。

库产物的模式规则是个**重要对照点**——它**没有 flock**，只用 `FORCE` 强制每次重跑：

```makefile
$(HDL_LIBRARY_PATH)%/.timestamp_intel: TARGET:=intel
FORCE:
$(HDL_LIBRARY_PATH)%/.timestamp_intel: FORCE
	$(MAKE) -C $(dir $@) $(TARGET) || exit $$?;
```

见 [project-intel.mk:131-L134](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L131-L134)。对比 Xilinx 的 [project-xilinx.mk:138-L146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L138-L146)：Xilinx 侧有 `flock` 串行化 + `REQUIRED_VIVADO_VERSION` 透传。为什么 Intel 不需要 flock？因为 `.timestamp_intel` 只是个空文件 `touch`，没有真正的打包产物会被并发损坏；而 Xilinx 的 `component.xml` 是 vivado 实打实生成的、多工程并发会撞车，必须加锁。这是「库轻 vs 库重」的直接体现。

接着看 Tcl 助手。`adi_project_intel.tcl` 把所有事情塞进**一个** `adi_project` 过程（没有 Xilinx 的 `adi_project_create` / `adi_project_files` / `adi_project_run` 三段式拆分）。它靠 `regexp` 匹配工程名里的载板后缀来选器件：

```tcl
# Supported carrier names are: a10gx, a10soc, c5soc, de10nano, s10soc, fm87
...
if [regexp "_de10nano" $project_name] {
  set family "Cyclone V"
  set device 5CSEBA6U23I7DK
  set system_qip_file ${ad_project_dir}/system_bd/synthesis/system_bd.qip
}
```

见 [adi_project_intel.tcl:44-L75](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L44-L75)。这与 AMD 侧 `adi_project_xilinx.tcl` 用 `regexp` 匹配后缀查表是同一套路（u3-l3 已讲）。

版本校验逻辑也和另外两家同构——字符串比较，不匹配默认 `exit 2`，设了 `IGNORE_VERSION_CHECK` 则降级为 `CRITICAL WARNING`。见 [adi_project_intel.tcl:79-L94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L79-L94)。

Intel 侧最特别的一段，是它在运行时**动态生成**一个 `system_qsys_script.tcl` 包装脚本，再交给 `qsys-script` / `qsys-generate` 执行。这段包装脚本里 `package require qsys`、设模块属性、`source system_qsys.tcl`、并按 Pro/Std 注入不同的 `qsys_mm` 互联参数，最后 `save_system {system_bd.qsys}`：

```tcl
set QFILE [open "system_qsys_script.tcl" "w"]
puts $QFILE "package require qsys"
puts $QFILE "set_module_property NAME {system_bd}"
...
puts $QFILE "source system_qsys.tcl"
...
puts $QFILE "save_system {system_bd.qsys}"
```

见 [adi_project_intel.tcl:126-L156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L126-L156)。之后按 `quartus_pro_isused` 分两路调用 `qsys-script` + `qsys-generate`，标准版还会额外打开 `ENABLE_ADVANCED_IO_TIMING`，见 [adi_project_intel.tcl:146-L180](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L146-L180)。

最后把 `system_top.v`、`system_constr.sdc`、顶层实体名登记进工程：

```tcl
set_global_assignment -name VERILOG_FILE system_top.v
set_global_assignment -name SDC_FILE system_constr.sdc
set_global_assignment -name TOP_LEVEL_ENTITY system_top
```

见 [adi_project_intel.tcl:191-L198](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L191-L198)。注意 Intel 的引脚约束（`set_location_assignment PIN_xxx -to ...` / `set_instance_assignment -name IO_STANDARD`）**写在工程自己的 `system_project.tcl` 里**，而不是 `.sdc`——这是 Intel 与 AMD 的一个用法差异（AMD 把引脚放在 `.xdc`）。

拿真实工程 `ad469x_evb/de10nano` 印证。它的 `Makefile` 极简，典型「报菜名 + include」结构：

```makefile
PROJECT_NAME := ad469x_evb_de10nano
M_DEPS += ../common/ad469x_qsys.tcl
M_DEPS += ../../common/de10nano/de10nano_system_qsys.tcl
...
LIB_DEPS += axi_dmac
LIB_DEPS += axi_sysid
LIB_DEPS += spi_engine/axi_spi_engine
...
include ../../scripts/project-intel.mk
```

见 [ad469x_evb/de10nano/Makefile:7-L26](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad469x_evb/de10nano/Makefile#L7-L26)。对应的 `system_project.tcl` 是「source 两个脚本 + adi_project + 引脚约束 + execute_flow」：

```tcl
adi_project ad469x_evb_de10nano [list SPI_4WIRE [get_env_param SPI_4WIRE 0]]
...
set_location_assignment PIN_AH8 -to ad469x_spi_cnv   ; ## P4.7 Arduino_IO07
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ad469x_spi_cnv
...
execute_flow -compile
```

见 [ad469x_evb/de10nano/system_project.tcl:24-L63](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad469x_evb/de10nano/system_project.tcl#L24-L63)。而块设计脚本 `system_qsys.tcl` 同样遵循 u2-l1 的三层架构——先 source 载板基设计、再 source 评估板基设计：

```tcl
source $ad_hdl_dir/projects/common/de10nano/de10nano_system_qsys.tcl   ; # 载板层
...
source ../../common/ad469x_qsys.tcl                                    ; # 评估板层
```

见 [ad469x_evb/de10nano/system_qsys.tcl:6-L13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad469x_evb/de10nano/system_qsys.tcl#L6-L13)。这印证了一个贯穿全仓的结论：**三层架构与厂商无关，只有文件后缀在变**（`bd` → `qsys` → `pb`）。

#### 4.1.4 代码实践

**实践目标**：亲手沿 Intel 工程的 Make 依赖链走一遍，确认「库依赖 = `.timestamp_intel`」且无 flock。

**操作步骤（源码阅读型）**：
1. 打开 [project-intel.mk:124-L134](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L124-L134)。
2. 对比 Xilinx 的 [project-xilinx.mk:138-L146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L138-L146)。
3. 在 `ad469x_evb/de10nano/Makefile` 里数 `LIB_DEPS` 列了几个 IP，预测每个会触发哪个库目录被 `make intel`。

**需要观察的现象**：
- Intel 的库模式规则只有 `FORCE`、没有 `flock`；Xilinx 的库模式规则同时有 `flock` 和 `REQUIRED_VIVADO_VERSION` 透传。
- 工具命令分别是 `quartus_sh --64bit -t` 与 `vivado ... -mode batch -source`。

**预期结果**：你能用一句话解释「为什么 Intel 不需要 flock 而 Xilinx 需要」——因为 `.timestamp_intel` 是空文件，而 `component.xml` 是真实产物。本步为纯源码阅读，**待本地验证**：若你装了 Quartus，可在 `projects/ad469x_evb/de10nano` 下跑 `make` 观察日志里 `qsys-generate` 与 `execute_flow` 的先后顺序。

#### 4.1.5 小练习与答案

**练习 1**：Intel 工程的引脚约束（`set_location_assignment`）写在哪里？为什么不在 `.sdc` 里？

**参考答案**：写在工程自己的 `system_project.tcl` 里（见 [system_project.tcl:37-L61](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad469x_evb/de10nano/system_project.tcl#L37-L61)）。`.sdc` 只描述时钟与时序路径约束，引脚分配属于 Quartus 的 assignment 体系，用 `set_*_assignment` 命令登记——这是 Quartus 与 Vivado（引脚放 `.xdc`）的用法差异。

**练习 2**：`adi_project_intel.tcl` 里为什么要动态生成 `system_qsys_script.tcl`，而不是直接 source `system_qsys.tcl`？

**参考答案**：因为 Qsys 需要 `package require qsys` 环境、需要先设模块属性与器件属性、并要按 Pro/Std 版本注入不同的 `qsys_mm` 互联参数（maxAdditionalLatency / clockCrossingAdapter / burstAdapterImplementation），最后还要 `save_system`。这些前置/后置动作拼成一个临时脚本交给 `qsys-script` 执行最干净；直接 source 会缺少 Qsys 运行环境。见 [adi_project_intel.tcl:126-L156](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_intel.tcl#L126-L156)。

---

### 4.2 Lattice Radiant / Propel 构建

#### 4.2.1 概念说明

Lattice 工程是三家里面**最特别**的，因为它把「块设计」和「综合布线」拆给了**两个不同的工具**：

- **Propel Builder**（命令行 `propelbldc`）：负责块设计——把 IP 拖出来连线、生成顶层 Verilog wrapper。对标 AMD 的 Vivado Block Design 与 Intel 的 Qsys。
- **Radiant**（命令行 `radiantc`）：负责综合、映射、布局布线（PAR）、导出比特流。对标 Vivado 的 synth_1/impl_1 与 Quartus 的 `execute_flow`。

因此一个 Lattice 工程的 `make` 实际上要**两次**工具调用：先 `propelbldc` 出块设计与顶层 wrapper，再 `radiantc` 把它跑成 `.bit`。这直接导致 Lattice 工程比另外两家多一个脚本（`system_project_pb.tcl`）和一整套独立的 Make 目标（`pb` / `rd` / `sge` / `sim`）。

另外两个 Lattice 独有点：

1. **约束拆成两份**——`system_constr.sdc`（时序）与 `system_constr.pdc`（物理布局）。因为 Radiant 规定只能有一个活跃的 Pre-Synthesis 约束文件（`.sdc`）和一个活跃的 Post-Synthesis 约束文件（`.pdc`），多份约束要在加入工程前**合并成单文件**。
2. **参数化机制不同**——Lattice 的 `project-lattice.mk` 里**没有** Xilinx/Intel 那套 `CFG` / `DIR_NAME` 子目录隔离逻辑，参数改由 `system_project_pb.tcl` 的 `-parameter_list` 传入，落到 `system_top_parameters.txt`，再被 Radiant 侧消费（见 u7-l2 的 CFG 讨论）。

#### 4.2.2 核心流程

```
make                       # all → sge (打包成 zip)
  ├─ pb  → Propel Builder 块设计
  │     propelbldc system_project_pb.tcl
  │       └─ adi_project_pb <name> -parameter_list {...}
  │            ├─ 版本校验 (Propel Builder 版本，从 components.xml 解析)
  │            ├─ sbp_create_project + source system_pb.tcl (载板+评估板块设计)
  │            ├─ sbp_design generate            # 生成 <工程名>.v 顶层 wrapper
  │            └─ sbp_design pge sge             # 生成 BSP / sge 目录
  │
  ├─ rd  → Radiant 综合布线
  │     radiantc system_project.tcl
  │       ├─ adi_project <name>                  # adi_project_lattice.tcl (Radiant)
  │       │    └─ adi_project_create → prj_create .rdf
  │       ├─ adi_project_files_default / adi_project_files   # 加源码+合并 sdc/pdc
  │       └─ adi_project_run                     # prj_run Export → .bit
  │
  └─ sge → 把 sge 目录 + .bit 打包成 sge.zip
```

最终比特流落在 `_bld/<工程名>/impl_1/<工程名>_impl_1.bit`。

#### 4.2.3 源码精读

先看工具定义与默认目标。`project-lattice.mk` 一上来声明**两套**命令行工具，外加仿真器：

```makefile
RADIANT := radiantc
RADIANT_GUI := radiant
PROPEL_BUILDER := propelbldc
PROPEL_BUILDER_GUI := propelbld
SIMULATOR ?= vsim
```

见 [project-lattice.mk:44-L50](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L44-L50)。默认 `all` 走 `sge`（把构建产物打成 zip 交付）：

```makefile
ALL_RULES ?= sge
all: $(ALL_RULES)
```

见 [project-lattice.mk:153-L155](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L153-L155)。

依赖收集比另外两家更宽——同时声明了块设计脚本、Radiant 脚本、两份约束，以及**Propel Builder 生成的顶层 wrapper**（注意 `_bld/...` 这一项，它在 `pb` 完成后才存在）：

```makefile
M_DEPS += system_project_pb.tcl
M_DEPS += system_pb.tcl
M_DEPS += system_project.tcl
...
M_DEPS += system_top.v
M_DEPS += _bld/$(PROJECT_NAME)/$(PROJECT_NAME)/$(PROJECT_NAME).v
M_DEPS += $(wildcard *system_constr.pdc)
M_DEPS += $(wildcard *system_constr.sdc)
```

见 [project-lattice.mk:53-L62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L53-L62)。库依赖被翻译成 `ltt/metadata.xml`（u4-l3 讲过，Lattice 用 `tclsh` 产出 `lsccip` 格式元数据供 Radiant/Propel 消费）：

```makefile
LIB_TARGETS := $(foreach dep,$(LIB_DEPS),${HDL_LIBRARY_PATH}$(dep)/ltt/metadata.xml)
```

见 [project-lattice.mk:245](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L245)。和 Xilinx 一样，这里用了 `flock`——因为 `metadata.xml` 是 `tclsh` 实打实生成的真实产物，并发会损坏：

```makefile
$(HDL_LIBRARY_PATH)%/metadata.xml: FORCE
	flock $(patsubst %/ltt/,%/,$(dir $@)).lock sh -c " \
	$(MAKE) -C $(patsubst %/ltt/,%/,$(dir $@)) $(TARGET)"; exit $$?
```

见 [project-lattice.mk:257-L261](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L257-L261)。

`pb` 目标驱动 Propel Builder。这里有个**值得记住的工程 hack**——Propel Builder 命令行不论成败都不返回非零退出码，所以脚本无法靠 `&&` 判断失败；解法是在工具跑完后**逐个检查期望产物是否存在**，缺失则手动打印红色 `FAILED` 并 `exit 2`：

```makefile
$(PB_STAMP_FILE): $(PB_DEPS) $(LIB_TARGETS)
	$(call skip_if_missing, ...)
	$(call build, $(PROPEL_BUILDER) system_project_pb.tcl, ...)
	@for file in $(PB_TARGETS); do \
		if [ ! -e $$file ]; then \
			echo "No [$(HL)$$file$(NC)] found. ... $(RED)FAILED$(NC)"; \
			exit 2; \
		fi \
	done
	@touch $(PB_STAMP_FILE)
```

见 [project-lattice.mk:267-L284](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L267-L284)，设计动机的注释在 [project-lattice.mk:30-L35](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L30-L35)。

`rd` 目标驱动 Radiant 跑出比特流，目标产物是 `_bld/<工程名>/impl_1/<工程名>_impl_1.bit`：

```makefile
DEFAULT_BIT_TARGET := _bld/$(PROJECT_NAME)/impl_1/$(PROJECT_NAME)_impl_1.bit
...
$(R_STAMP_FILE): $(R_DEPS)
	-rm -f $(PROJECT_NAME)_radiant.log
	$(call build, $(RADIANT) system_project.tcl, $(PROJECT_NAME)_radiant.log, ...)
	@for file in $(R_TARGETS); do ... 检查产物 ... done
	@touch $(R_STAMP_FILE)
```

见 [project-lattice.mk:124-L125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L124-L125)（比特流目标）与 [project-lattice.mk:286-L305](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L286-L305)（`rd` 规则）。`rd` 同样用「检查产物存在性」兜底退出码。

再看 Tcl 侧。**Propel Builder** 助手 `adi_project_lattice_pb.tcl` 的 `adi_project_create_pb` 完成版本校验（这次是从 `$env(TOOLRTF)/../../components.xml` 用正则抠出版本号）、建项目、执行 `cmd_list`（其中默认 `source ./system_pb.tcl`）、然后 `sbp_design generate` + `sbp_design pge sge` 生成顶层 wrapper 与 BSP：

```tcl
sbp_create_project -name "$project_name" -path $ppath -device $device ...
...
foreach cmd $cmd_list { ...; eval $cmd }   ; # source ./system_pb.tcl
sbp_design save
sbp_design generate
sbp_design pge sge -i ".../$project_name.sbx" -o "$ppath/$project_name"
```

见 [adi_project_lattice_pb.tcl:195-L213](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl#L195-L213)（版本校验）与 [adi_project_lattice_pb.tcl:222-L262](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl#L222-L262)（建项目 + 生成）。注意 Propel Builder 的 IP 实例化用 `sbp_config_ip` / `sbp_add_component` 配合 VLNV，见 [adi_project_lattice_pb.tcl:588-L608](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl#L588-L608)。

**Radiant** 助手 `adi_project_lattice.tcl` 则更像 Xilinx 的三段式（`adi_project` / `adi_project_files` / `adi_project_run`）。`adi_project_create` 用 `prj_create` 建 `.rdf` 工程，版本用 `sys_install_version` 取：

```tcl
set RADIANT_VERSION [string range [sys_install_version] 0 ...]
if {[string compare $RADIANT_VERSION $required_lattice_version] != 0} { ... exit 2 }
...
prj_create -name "$project_name" -impl $impl -dev $device ...
```

见 [adi_project_lattice.tcl:127-L166](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl#L127-L166)。`adi_project_files` 有一段针对 Radiant 约束限制的**合并逻辑**——遍历文件列表，遇到 `.pdc` 就追加进单个 `system_constr.pdc`、遇到 `.sdc` 就追加进单个 `system_constr.sdc`，其余走 `prj_add_source`：

```tcl
# In Lattice Radiant there can be only one active .sdc and one active .pdc.
if {[regexp {^.+\.pdc$} $pfile]} {
	add_update_constraint_file $pfile $project_dir pdc $radiant_project $opt_args
} elseif {[regexp {^.+\.sdc$} $pfile]} {
	add_update_constraint_file $pfile $project_dir sdc $radiant_project $opt_args
} else {
	prj_add_source $opt_args $pfile
}
```

见 [adi_project_lattice.tcl:524-L540](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl#L524-L540)。`adi_project_run` 则按模式分发 `prj_run Export/Synthesis/Map/PAR`，见 [adi_project_lattice.tcl:676-L691](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl#L676-L691)。

拿真实工程 `ad738x_fmc/lfcpnx` 印证。它的 `Makefile` 与 Intel 版结构一致，但 `include` 的是 `project-lattice.mk`，约束多了 `.pdc`：

```makefile
PROJECT_NAME := ad738x_fmc_lfcpnx
M_DEPS += system_constr.pdc
M_DEPS += ../../common/lfcpnx/lfcpnx_system_constr.pdc
...
LIB_DEPS += axi_dmac
LIB_DEPS += spi_engine/axi_spi_engine
...
include ../../scripts/project-lattice.mk
```

见 [ad738x_fmc/lfcpnx/Makefile:7-L25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad738x_fmc/lfcpnx/Makefile#L7-L25)。两个工程脚本分工明确——`system_project_pb.tcl` 只管 Propel Builder 块设计与参数：

```tcl
adi_project_pb ad738x_fmc_lfcpnx -parameter_list [list \
  ALERT_SPI_N [get_env_param ALERT_SPI_N 0] \
  NUM_OF_SDIO [get_env_param NUM_OF_SDIO 1] \
  DATA_WIDTH  [get_env_param DATA_WIDTH 16] ...]
```

见 [ad738x_fmc/lfcpnx/system_project_pb.tcl:9-L13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad738x_fmc/lfcpnx/system_project_pb.tcl#L9-L13)。`system_project.tcl` 管 Radiant 综合布线：

```tcl
adi_project ad738x_fmc_lfcpnx
adi_project_files_default ad738x_fmc_lfcpnx
adi_project_files ad738x_fmc_lfcpnx -flist [list system_top.v ... system_constr.pdc]
adi_project_run ad738x_fmc_lfcpnx -cmd_list { ... prj_set_strategy_value ... }
```

见 [ad738x_fmc/lfcpnx/system_project.tcl:10-L26](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad738x_fmc/lfcpnx/system_project.tcl#L10-L26)。而块设计脚本 `system_pb.tcl` 同样遵循三层架构——先载板、再评估板：

```tcl
source $ad_hdl_dir/projects/common/lfcpnx/lfcpnx_system_pb.tcl   ; # 载板层
source $ad_hdl_dir/projects/ad738x_fmc/common/ad738x_pb.tcl      ; # 评估板层
```

见 [ad738x_fmc/lfcpnx/system_pb.tcl:6-L14](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/ad738x_fmc/lfcpnx/system_pb.tcl#L6-L14)。

#### 4.2.4 代码实践

**实践目标**：搞清 Lattice 工程里 `pb` 与 `rd` 两阶段的产物衔接，以及为何需要「检查产物」兜底。

**操作步骤（源码阅读型）**：
1. 读 [project-lattice.mk:263-L305](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L263-L305)，对照 `pb` 与 `rd` 两条规则。
2. 在 `M_DEPS`（[L53-L62](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L53-L62)）里找到那项 `_bld/$(PROJECT_NAME)/$(PROJECT_NAME)/$(PROJECT_NAME).v`——它是 Propel Builder 生成的顶层 wrapper，`rd` 阶段综合的就是它。
3. 读 [adi_project_lattice_pb.tcl:222-L262](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl#L222-L262)，确认 `sbp_design generate` + `pge sge` 才产出该 wrapper。

**需要观察的现象**：
- `pb` 产物（`.sbx`、顶层 `.v`、`sge` 目录）正是 `rd` 的输入；两阶段串行，先块设计后布线。
- 两条规则末尾都有 `for file in ... ; do [ ! -e $$file ] && FAILED && exit 2 ; done`。

**预期结果**：你能画出 `propelbldc → <工程名>.v → radiantc → <工程名>.bit` 的数据流，并解释「检查产物存在性」是为了弥补 Propel Builder 不返回错误码的缺陷。本步为纯源码阅读；**待本地验证**：装了 Radiant + Propel Builder 后可在 `projects/ad738x_fmc/lfcpnx` 下分别跑 `make pb` 与 `make rd` 观察两阶段产物。

#### 4.2.5 小练习与答案

**练习 1**：为什么 Lattice 工程的库模式规则用了 `flock`，而 Intel 的没有？

**参考答案**：Lattice 库产物是 `ltt/metadata.xml`——`tclsh` 实打实生成的真实文件，多工程并发打包同一个 IP 会损坏它，所以要像 Xilinx 的 `component.xml` 一样加 `flock` 串行化（[project-lattice.mk:257-L261](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L257-L261)）。Intel 的 `.timestamp_intel` 只是个 `touch` 出来的空时间戳，没有真实内容会被损坏，所以只需 `FORCE` 触发、无需加锁。

**练习 2**：`project-lattice.mk` 里没有 Xilinx/Intel 那套 `CFG` / `DIR_NAME` 逻辑，Lattice 工程怎么实现参数化构建？

**参考答案**：参数走 Tcl 侧——`system_project_pb.tcl` 把 `-parameter_list` 传给 `adi_project_pb`，后者把它写进 `system_top_parameters.txt` 并暴露成全局 `ad_project_params` 数组供 `system_pb.tcl` 读取（[adi_project_lattice_pb.tcl:80-L102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice_pb.tcl#L80-L102)）；Radiant 侧再用 `update_verilog_parameters` 改写 `system_top.v` 的 parameter（[adi_project_lattice.tcl:292-L369](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_lattice.tcl#L292-L369)）。

---

### 4.3 三厂商工程文件差异

#### 4.3.1 概念说明

前两节填完了 Intel 与 Lattice 的内部机制，本节把它们和 AMD 摆在一起做**横向对照**。这套对照的权威来源是官方文档 `docs/user_guide/architecture.rst` 的「File structure of a project」一节——它给三家各列了一份标准文件清单。掌握这份清单后，你看到一个工程目录就能立刻判断它面向哪家厂商、`make` 时会走哪条链路。

#### 4.3.2 核心流程（三厂商标准文件清单）

`architecture.rst` 给出的官方清单（逐字对照）：

| 文件 | AMD Xilinx | Intel | Lattice |
|---|---|---|---|
| 构建脚本 | `Makefile` | `Makefile` | `Makefile` |
| 工程驱动 | `system_project.tcl`（建工程+综合实现） | `system_project.tcl`（建工程+Qsys+引脚+`execute_flow`） | `system_project.tcl`（Radiant）+ `system_project_pb.tcl`（Propel Builder） |
| 块设计 | `system_bd.tcl` | `system_qsys.tcl` | `system_pb.tcl`（linker，被 `adi_project_pb` source） |
| 时序约束 | （`system_constr.xdc`） | `system_constr.sdc` | `system_constr.sdc` |
| 物理约束 | （并入 `.xdc`） | （assign 脚本） | `system_constr.pdc` |
| 顶层 HDL | `system_top.v` | `system_top.v` | `system_top.v` |
| 工具生成顶层 | `system_wrapper.v` | `system_bd` | `<工程名>.v` |

文档原文见 AMD [architecture.rst:1081-L1115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1081-L1115)、Intel [architecture.rst:1116-L1138](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1116-L1138)、Lattice [architecture.rst:1149-L1175](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1149-L1175)。文档还特别说明：某些载板文件名有变体，例如 A10SoC 把约束拆成 `a10soc_plddr4_assign.tcl`（PL 侧）与 `a10soc_system_assign.tcl`（PS 侧），见 [architecture.rst:1140-L1148](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1140-L1148)。

把构建侧的关键差异再压缩成一张「机制对照表」：

| 维度 | AMD Xilinx | Intel | Lattice |
|---|---|---|---|
| 工具调用次数 | 1（`vivado`） | 1（`quartus_sh`） | 2（`propelbldc` + `radiantc`） |
| 最终产物 | `system_top.xsa` | `system_top.sof` | `<工程名>.bit` |
| 库依赖产物 | `component.xml` | `.timestamp_intel` | `ltt/metadata.xml` |
| 库规则加锁 | `flock` | 无（`FORCE`） | `flock` |
| 版本透传 | `REQUIRED_VIVADO_VERSION` | 无（库不打包） | 无（库不打包需透传） |
| 引脚约束位置 | `system_constr.xdc` | `system_project.tcl`（assign） | `system_constr.pdc` |
| CFG/DIR_NAME 参数化 | 有 | 有 | 无（走 `system_top_parameters.txt`） |
| Tcl 助手拆分 | `adi_project`/`_files`/`_run` | 单个 `adi_project` | Radiant 三段式 + PB 单独 `adi_project_pb` |

共性也值得显式总结，避免把差异当成全部：

1. **三层架构不变**——三家都是「载板基设计 + 评估板基设计 + 系统特化」，块设计脚本都先 source 载板再 source 评估板（仅后缀 `bd`/`qsys`/`pb` 不同）。
2. **工程 Makefile 三段式不变**——`PROJECT_NAME` + `M_DEPS`/`LIB_DEPS` + `include project-<厂商>.mk`。
3. **`quiet.mk` 的 `build`/`clean`/`skip_if_missing` 宏三家共用**，终端输出风格一致。
4. **版本校验三段逻辑同构**——字符串比较、不匹配 `exit 2`、`IGNORE_VERSION_CHECK` 降级为警告。

#### 4.3.3 源码精读

把三家最终产物的目标定义并排看一眼，差异最直观。

Xilinx（[project-xilinx.mk:116](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L116)）：

```makefile
$(PROJECT_NAME).sdk/system_top.xsa: $(M_DEPS)
	... $(VIVADO) system_project.tcl ...
```

Intel（[project-intel.mk:108](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L108)）：

```makefile
all: $(PROJECT_NAME).sof
```

Lattice（[project-lattice.mk:124-L125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L124-L125)）：

```makefile
DEFAULT_BIT_TARGET := _bld/$(PROJECT_NAME)/impl_1/$(PROJECT_NAME)_impl_1.bit
```

三家的库依赖翻译规则并排（这是与 u4-l1/u4-l3 衔接的关键点）：

- Xilinx：`component.xml`（[project-xilinx.mk:83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L83)）
- Intel：`.timestamp_intel`（[project-intel.mk:105](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-intel.mk#L105)）
- Lattice：`ltt/metadata.xml`（[project-lattice.mk:245](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-lattice.mk#L245)）

约束后缀的官方定义直接来自 `architecture.rst`：Intel 的 `system_constr.sdc`「contains clock definitions and other path constraints」（[architecture.rst:1132-L1133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1132-L1133)）；Lattice 多一份 `system_constr.pdc`「contains clock definitions and other path constraints + physical constraints」（[architecture.rst:1168-L1170](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1168-L1170)）。

#### 4.3.4 代码实践

**实践目标**：把三家工程文件差异整理成一张可备查的表，并能在真实工程目录里逐项核对。

**操作步骤**：
1. 打开三个真实工程目录，列出各自文件：
   - AMD：`projects/fmcomms2/zcu102/`（u2-l2 已剖析）
   - Intel：`projects/ad469x_evb/de10nano/`
   - Lattice：`projects/ad738x_fmc/lfcpnx/`
2. 对照 [architecture.rst:1081-L1175](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1081-L1175) 的三份官方清单。
3. 在三个 `Makefile` 里分别找到 `include` 的公共脚本名与 `LIB_DEPS` 翻译产物。
4. 完成下表（示例答案见「预期结果」）：

  | 维度 | fmcomms2/zcu102 | ad469x_evb/de10nano | ad738x_fmc/lfcpnx |
  |---|---|---|---|
  | include 的公共脚本 | | | |
  | 块设计脚本 | | | |
  | 约束文件 | | | |
  | 最终产物 | | | |

**需要观察的现象**：Lattice 目录比另外两家多出 `system_project_pb.tcl` 与 `system_constr.pdc`；Intel 目录有 `system_qsys.tcl` 而无 `system_bd.tcl`。

**预期结果**（示例答案）：

  | 维度 | fmcomms2/zcu102 | ad469x_evb/de10nano | ad738x_fmc/lfcpnx |
  |---|---|---|---|
  | include 的公共脚本 | `project-xilinx.mk` | `project-intel.mk` | `project-lattice.mk` |
  | 块设计脚本 | `system_bd.tcl` | `system_qsys.tcl` | `system_pb.tcl` |
  | 约束文件 | `system_constr.xdc` | `system_constr.sdc` | `system_constr.sdc` + `system_constr.pdc` |
  | 最终产物 | `system_top.xsa` | `system_top.sof` | `<工程名>.bit` |

本步为纯源码阅读与目录比对，无需工具链即可完成。

#### 4.3.5 小练习与答案

**练习 1**：给定一个工程目录只含 `Makefile`、`system_project.tcl`、`system_pb.tcl`、`system_project_pb.tcl`、`system_top.v`、`system_constr.sdc`、`system_constr.pdc`，判断它面向哪家厂商、面向哪块载板族。

**参考答案**：面向 **Lattice**——`system_pb.tcl` + `system_project_pb.tcl` 是 Propel Builder 两件套，`.pdc` 是 Lattice 专属物理约束（[architecture.rst:1149-L1175](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/architecture.rst#L1149-L1175)）。载板族通常是 CertusPro-NX（目前 Lattice 唯一支持的载板是 `lfcpnx`）。

**练习 2**：为什么说「Intel 走库轻工程重，Xilinx/Lattice 走库重工程轻」？

**参考答案**：Intel 的 IP 用 `*_hw.tcl` 描述，由 Qsys 在**工程构建时**动态解释（ELABORATION/VALIDATION 回调），库侧只 `touch` 一个 `.timestamp_intel` 占位，所以「库轻」；真正的 IP 组装发生在工程侧的 Qsys 里，所以「工程重」。Xilinx 与 Lattice 在库侧就用 vivado / tclsh 把 IP 真打成了 `component.xml` / `metadata.xml`（库重），工程侧只是引用已打包产物（工程轻）。这条不对称在 u4-l3 已建立，本讲从工程侧消费的产物类型（`.timestamp_intel` vs `component.xml` / `metadata.xml`）再次印证。

---

## 5. 综合实践

**任务**：为一个假设的新数据转换器评估板，起草它在三家厂商下的工程文件骨架，并用一张图说明它们共享什么、差异在哪。

请按以下步骤完成（纯文档/源码阅读型，无需工具链）：

1. 选定一个已有评估板的「评估板基设计」作为蓝本，例如 `projects/ad738x_fmc/common/`（它同时有 `ad738x_bd.tcl`、`ad738x_qsys.tcl`、`ad738x_pb.tcl` 三种块设计脚本——正好覆盖三家）。浏览该目录确认三份脚本的存在。
2. 假设你的新评估板叫 `myadc`，要分别适配 `zcu102`（AMD）、`de10nano`（Intel）、`lfcpnx`（Lattice）三块载板。写出三套工程目录应有的文件清单（参考 4.3.4 的表格与 `architecture.rst` 的官方清单）。
3. 对每套工程，写出其 `Makefile` 里 `include` 的是哪个公共脚本、`LIB_DEPS` 会被翻译成哪种库产物。
4. 写出三套工程各自的 `make` 调用链（工具命令 + 最终产物），指出 Lattice 多出来的那个「块设计」阶段。
5. 最后，用一段话总结：尽管三家文件后缀与工具不同，**哪三件事是它们共同遵守的**（提示：三层架构、Makefile 三段式、quiet.mk 宏与版本校验）。

**验收标准**：

- 三份文件清单与 `architecture.rst` 官方清单一致。
- 能正确说出 Intel 的库产物是 `.timestamp_intel` 且不加 flock、Lattice 的是 `ltt/metadata.xml` 且加 flock、Xilinx 的是 `component.xml` 且加 flock。
- 能画出 Lattice `propelbldc → <工程名>.v → radiantc → .bit` 的两阶段数据流。

---

## 6. 本讲小结

- Intel 工程用 `quartus_sh` 一次调用 `system_project.tcl` 出 `.sof`；块设计脚本是 `system_qsys.tcl`，约束是 `.sdc`，引脚约束写在 `system_project.tcl` 的 assignment 里。
- Intel 的库依赖翻译成 `.timestamp_intel` 空时间戳，模式规则只用 `FORCE`、**不加 flock**——因为库侧不真跑 Quartus，IP 由 Qsys 在工程期动态解释（「库轻工程重」）。
- Lattice 把构建拆成 **Propel Builder（`propelbldc`）出块设计** 与 **Radiant（`radiantc`）出 `.bit`** 两阶段，多一个 `system_project_pb.tcl` 脚本；约束拆 `.sdc` + `.pdc` 两份。
- Lattice 的库依赖翻译成 `ltt/metadata.xml`，**加 flock**；Propel Builder 不返回错误码，故 `project-lattice.mk` 用「检查产物存在性」兜底失败判定。
- 三家在文件后缀（`bd`/`qsys`/`pb`、`xdc`/`sdc`/`pdc`）、工具（`vivado`/`quartus_sh`/`radiantc`+`propelbldc`）、产物（`xsa`/`sof`/`bit`）上不同，但都遵守三层架构、Makefile 三段式与 `quiet.mk` 宏。
- Tcl 助手风格各异：Intel 把全部逻辑塞进单个 `adi_project`；Lattice 分 Radiant 三段式（`adi_project`/`_files`/`_run`）与 Propel Builder 单独的 `adi_project_pb`；Xilinx 是 `adi_project`/`_files`/`_run`。

## 7. 下一步学习建议

- **u7-l1（移植工程到新载板）**：本讲 focused 在三家工程文件差异；如果你要为 Intel/Lattice 新载板做移植，去读 `adi_project_intel.tcl` 的载板 `regexp` 表与 Lattice 的 `adi_lattice_dev_select.tcl`，并对照 u7-l1 的 base design 制作步骤。
- **u8-l3（收发器、时钟与时序约束）**：本讲只点了约束后缀的差异；时序收敛细节（`adi_tquest.tcl`、`auto_timing_fix_xilinx.tcl`、sdc/pdc 写法）在那里深入。
- **继续阅读源码**：`projects/common/de10nano/`（Intel 载板 base design）与 `projects/common/lfcpnx/`（Lattice 载板 base design）是理解载板层如何为各自厂商定制的最佳样本；再对照 `projects/common/zcu102/` 看三家载板层脚本的同构性。
