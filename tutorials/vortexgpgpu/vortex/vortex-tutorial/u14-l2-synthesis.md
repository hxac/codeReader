# 综合流程与 PPA 分析

## 1. 本讲目标

本讲承接 u14-l1（FPGA AFU 外壳与驱动），把视线从「Vortex 怎么上板」转到「Vortex 怎么被综合、综合后怎么得到 PPA（Performance / Power / Area，性能/功耗/面积）报告」。读完本讲你应当能够：

- 说清 Vortex 四条综合流程（Xilinx Vivado、Altera Quartus、Yosys、Synopsys Design Compiler）各自定位与脚本入口。
- 复述综合流程的统一骨架：`CONFIGS` 投影成 `XCONFIGS`、`gen_sources.sh` 生成源清单、`extensions.mk` 按扩展接线、`PREFIX` 隔离多构建。
- 在任意一条流程里，从 Makefile 入口一路追到工具命令，指出 PPA 报告落在哪个目录的哪个文件。
- 独立解读一份 PPA 报告：用 WNS 算 Fmax、读出面积/资源、区分 vectorless 与 SAIF 标注的功耗。

## 2. 前置知识

- **综合（Synthesis）**：把 RTL（SystemVerilog）翻译成特定工艺/器件的门级网表的过程。FPGA 综合产出可布线的器件原语（LUT/FF/BRAM/DSP），ASIC 综合产出标准单元库（.lib/.db）里的门。
- **PPA**：芯片设计的三大目标——Performance（性能，常以最高时钟频率 Fmax 衡量）、Power（功耗，分动态/静态）、Area（面积/资源）。三者互相牵制，是评估一个 RTL 配置好坏的核心指标。
- **WNS / TNS**：Worst Negative Slack（最差负裕量）与 Total Negative Slack（总负裕量）。Slack 是时序裕量，正值表示满足约束、负值表示违例。Fmax 由「时钟周期 − WNS」反推。
- **SAIF / VCD**：两种记录仿真时信号翻转活动的文件格式。综合工具读它来把「按默认翻转率估算」的功耗升级成「按真实负载估算」的功耗。
- **`CONFIGS` 与 `VX_CFG_*`**：这是 u2-l1/u2-l2 建立的配置真相来源。综合时，`CONFIGS` 里的人类可读旋钮（如 `-DVX_CFG_NUM_CORES=4`）经 `gen_config.py` 投影成完整的 `XCONFIGS` 宏集，再注入 RTL。本讲的每条流程都建立在这套机制上。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [docs/synthesis_analysis.md](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md) | 综合与功耗分析的权威说明书，是本讲的总纲 |
| [hw/syn/common.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/common.mk) | 四条流程共享的工具路径定义（sv2v/yosys/sta/verilator） |
| [hw/syn/extensions.mk](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/extensions.mk) | 按扩展（TCU/DXA/RTU/gfx…）追加 RTL 源的「单一真相」片段 |
| [hw/scripts/gen_sources.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/gen_sources.sh) | 预处理+收集 RTL、生成 VCS 风格源清单 `sources.txt` 的共享脚本 |
| [hw/syn/xilinx/xrt/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile) | Xilinx XRT 全平台流程入口（产出 `.xclbin` 比特流） |
| [hw/syn/xilinx/dut/project.tcl](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/dut/project.tcl) | Xilinx DUT 子部件综合的 Vivado TCL 驱动（含报告生成） |
| [hw/syn/altera/opae/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/altera/opae/Makefile) | Altera OPAE 流程入口（已废弃，见 u14-l1） |
| [hw/syn/yosys/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/Makefile) + [run_synth.sh](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/run_synth.sh) | 开源 ASIC 综合流程（Yosys + ABC + OpenSTA） |
| [hw/syn/synopsys/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile) + [project.tcl](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl) | 商用 ASIC 综合流程（Synopsys DC，含 SRAM 宏单元处理） |
| [hw/scripts/xilinx_power_analysis.tcl](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/xilinx_power_analysis.tcl) | Xilinx 共享的 SAIF 标注功耗分析脚本 |

## 4. 核心概念与源码讲解

### 4.1 综合流程的统一骨架：配置、源清单与工具链

#### 4.1.1 概念说明

Vortex 的 `hw/syn/` 下有四条彼此独立的综合流程，但它们共享同一套骨架。理解骨架比记住每条命令更重要，因为四条流程的差异只在「换一个工具命令」这一层。骨架由三件事组成：

1. **配置投影**：把人类可读的 `CONFIGS`（一堆 `-D` 宏）经 `gen_config.py` 投影成 RTL 真正需要的完整 `XCONFIGS` 宏集——这是 u2-l2 讲过的「配置值流」的综合侧落点。
2. **源清单生成**：统一调 `hw/scripts/gen_sources.sh` 收集 RTL，产出一份 VCS 风格的 `sources.txt`，让各家工具各取所需。
3. **工具隔离**：用 `PREFIX` 给每个构建一个独立目录，使不同配置的构建能同机共存，互不覆盖。

这条骨架之上，`extensions.mk` 是「扩展源的单管之源」：无论开 TCU、DXA、RTU 还是图形扩展，都由它按 `XCONFIGS` 自动追加对应的 `*_pkg.sv` 与 include 路径，避免四条流程各写一遍。

#### 4.1.2 核心流程

任一条综合流程的执行都可拆成下面四段（伪代码）：

```text
# 第 0 段：配置投影（所有 Makefile 都有这一行）
XCONFIGS = $(shell gen_config.py --cflags='$(CONFIGS) -DVX_CFG_XLEN=$(XLEN)')
                  ↓
# 第 1 段：按扩展追加 RTL 源（共享片段）
include hw/syn/extensions.mk      # 依 XCONFIGS 加 TCU/DXA/RTU/gfx 的 _pkg.sv 与 -I
                  ↓
# 第 2 段：生成源清单（共享脚本）
gen_sources.sh -P $(CFLAGS) -C$(BUILD_DIR)/src -O$(BUILD_DIR)/sources.txt
        # -P：用 verilator -E -P 预处理（展开 `include、裁掉条件编译块）
        # -C：拷贝到构建目录（源树零改动）
        # -O：输出 sources.txt（先 *_pkg.sv，再 *_if.sv，最后其余）
                  ↓
# 第 3 段：调用工具（每家不同）
#   Xilinx XRT：  v++ 把 .xo 链成 .xclbin
#   Yosys：       run_synth.sh 生成 synth.ys → yosys → abc → OpenSTA
#   Synopsys：    dc_shell -f project.tcl
#   Altera：      afu_synth_setup + run.sh
                  ↓
# 第 4 段：报告落在 $(BUILD_DIR)/ 下（每家命名不同，见 4.4）
```

一个贯穿全程的设计原则是「**多树共存**」：工具的绝对路径不进 `config.mk`、不依赖全局 `PATH`，而是由 `common.mk` 从 `$(TOOLDIR)` 推导后 `export`，让每个构建自带工具路径。这和 u1-l3 讲的「每棵 Vortex 树互不干扰」一脉相承。

#### 4.1.3 源码精读

先看四条流程共享的工具路径定义。`common.mk` 明确把综合工具路径与 test/sim 工具路径分开，避免泄露：

[hw/syn/common.mk:L14-L37](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/common.mk#L14-L37) —— 注释说「Tool paths are kept here rather than in config.mk to avoid leaking them into test/sim builds」，随后定义 `SV2V_PATH/YOSYS_PATH/STA_PATH/VERILATOR_PATH`，再 `export` 出绝对二进制路径 `SV2V/YOSYS/STA/VERILATOR`，使「builds are self-contained and multiple trees can coexist without a sourced env polluting PATH」。

再看四条流程都有的「配置投影」这一行（以 Yosys 为例）：

[hw/syn/yosys/Makefile:L63-L69](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/Makefile#L63-L69) —— 先 `CONFIGS += -DSYNTHESIS -DASIC -DYOSYS`（告诉 RTL「现在处于 ASIC 综合语境」，会关掉仿真专用逻辑），再调 `gen_config.py --cflags` 把 `CONFIGS` 加上 `-DVX_CFG_XLEN=$(XLEN)` 解析成完整 `XCONFIGS`。这是综合侧与配置系统的唯一接口，与 u2-l2 的值流图完全对齐。

扩展源的「单一真相」片段则在 `extensions.mk`，按 `XCONFIGS` 条件追加：

[hw/syn/extensions.mk:L25-L53](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/extensions.mk#L25-L53) —— 用 `$(filter ...,$(XCONFIGS))` 检测开关，例如开了 `-DVX_CFG_EXT_TCU_ENABLE` 就追加 `tcu/VX_tcu_pkg.sv` 与 `-I$(RTL_DIR)/tcu`，并根据 TCU 后端类型（DPI/DSP/BHF/FPNEW/TFR）继续追加 include。文件头注释自称「Single source of truth」。注意它「must be included AFTER the flow has computed XCONFIGS」——这正是每条流程先算 `XCONFIGS`、再 `include extensions.mk` 的原因。

源清单的生成在共享脚本 `gen_sources.sh` 里。其核心是 `copy_one_file`，负责预处理与参数覆写：

[hw/scripts/gen_sources.sh:L107-L145](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/gen_sources.sh#L107-L145) —— 当传了 `-P` 时，每个 `.v/.sv` 文件先用 `repl_params.py` 处理 `-G` 顶层参数覆写（只改构建副本、源树不动），再用 `verilator -E -P` 做宏预处理（展开 `` `include ``、裁掉 `ifdef`），输出到 `copy_folder`。注释特别说明「an empty but successful output is legitimate」——因为像 `VX_decompressor.sv` 在关闭 RVC 时会被条件编译裁成空文件，空输出不应判错。

最后是源清单的排序逻辑，它决定了「包先于接口、接口先于其余」的编译顺序：

[hw/scripts/gen_sources.sh:L219-L246](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/gen_sources.sh#L219-L246) —— 先 `find *_pkg.sv`，再 `find *_if.sv`，最后是其余 `.v/.sv`。SystemVerilog 要求 package 先编译，这里用文件名约定把顺序固化进 filelist。

#### 4.1.4 代码实践

**实践目标**：从一份 Makefile 入口，亲手追出「配置 → 源清单 → 工具命令」三段，验证四条流程共享同一骨架。

**操作步骤**（源码阅读型，无需运行 EDA 工具）：

1. 打开 [hw/syn/yosys/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/Makefile)，定位第 69 行的 `XCONFIGS :=`，确认它调的是 `gen_config.py`。
2. 往下找到第 87 行 `include $(VORTEX_HOME)/hw/syn/extensions.mk`，确认 Yosys 流程也用了共享扩展片段。
3. 看 `synthesis` 目标（第 132–140 行）：它 `cd $(BUILD_DIR)` 后设置一堆环境变量（`TOP/SRC_FILE/...`），最后调 `$(SRC_DIR)/run_synth.sh`。
4. 对比 [hw/syn/synopsys/Makefile:L157-L172](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile#L157-L172)：同样 `cd $(BUILD_DIR)`、设环境变量，但最后一行换成 `dc_shell -f project.tcl`——骨架一致，只换了工具命令。

**需要观察的现象**：四条流程在「算 `XCONFIGS` → include `extensions.mk` → 调 `gen_sources.sh`」三步上几乎逐字相同；差异集中在最后一行（`v++` / `yosys` / `dc_shell` / `afu_synth_setup`）。

**预期结果**：你能画出一张「三段共享 + 第四段各异」的对比表，并能解释为何 `extensions.mk` 必须在 `XCONFIGS` 之后 include。

#### 4.1.5 小练习与答案

**练习 1**：为什么综合工具路径放在 `hw/syn/common.mk` 而不是顶层 `config.mk`？
**答案**：`config.mk` 会被 test/sim 构建也 include；把综合专用工具（sv2v/yosys/sta）放在 `common.mk` 里，只有综合后端才会引入，避免把综合工具路径泄露进无关构建，也保证综合树自包含。

**练习 2**：`extensions.mk` 里检测扩展用的是 `$(XCONFIGS)` 而不是 `$(CONFIGS)`，为什么？
**答案**：`CONFIGS` 是用户手写的原始宏，可能含 `-G` 参数覆写或未展开的表达式；`XCONFIGS` 是 `gen_config.py` 投影后的完整、已解析的宏集，能可靠地用 `$(filter ...)` 检测某个扩展是否真正启用（包括自动派生的 `_ENABLED` 镜像）。

---

### 4.2 FPGA 流程：Xilinx Vivado 与 Altera Quartus

#### 4.2.1 概念说明

FPGA 综合的目标是产出一个可下载到板卡的**比特流**（Xilinx 叫 `.xclbin`，Altera 叫 AFU 镜像）。Vortex 维护两条 FPGA 流程：

- **Xilinx XRT 流程**（`hw/syn/xilinx/xrt/`）：主路径，受支持（见 u14-l1）。它把 Vortex RTL 包装成 Vitis RTL kernel，经 Vivado 综合/布局布线后由 `v++` 链成 `.xclbin`，支持 Alveo U50/U55C/U200/U250/U280 与 Versal VCK5000。
- **Altera OPAE 流程**（`hw/syn/altera/opae/`）：已废弃，仅 Arria 10 / Stratix 10。脚本仍在，但 u14-l1 已明确 XRT 是受支持主路径。

此外，两家都提供 **DUT（Device Under Test）流程**——把单个子部件（core、cache、fpu、tcu、dxa 等）剥离平台外壳单独综合，用来快速评估某个单元的 PPA，而不必每次综合整个带 AFU 的设计。这正是「大设计里抠小块来评估」的工程手段。

#### 4.2.2 核心流程

Xilinx XRT 全平台流程是一条流水线：

```text
gen-sources  →  gen-xo（Vivado 把 RTL 综合成 .xo 网表容器）
            →  gen-bin（v++ 把 .xo 链成 .xclbin）
            →  report（从 _x/ 拷出 utilization/timing 报告到 bin/）
            →  power（可选：用 SAIF 重算功耗）
```

关键约定：构建目录 `$(PREFIX)_$(XSA)_$(TARGET)`，其中 `XSA` 由 `PLATFORM` 推导、`TARGET` 是 `hw`（真硬件）或 `hw_emu`（硬件仿真）。`NUM_CORES=N` 是个语法糖，自动展开成 cluster/core/L2 配置宏。

DUT 流程则简单得多：直接用 Vivado project-mode（`project.tcl`）对单个顶层模块跑一遍 synth→impl→report，时钟约束在 TCL 里现场生成，报告直接落到当前目录的 `.rpt` 文件。

#### 4.2.3 源码精读

XRT 流程的入口是 [hw/syn/xilinx/xrt/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile)。看 `NUM_CORES` 语法糖与 `CONFIGS` 注入：

[hw/syn/xilinx/xrt/Makefile:L76-L94](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile#L76-L94) —— `NUM_CORES=N` 选预定义配置（如 `CONFIGS_4c := -DVX_CFG_NUM_CLUSTERS=1 -DVX_CFG_NUM_CORES=4`），再追加 `-DSYNTHESIS -DVIVADO`，最后第 94 行同样用 `gen_config.py` 算 `XCONFIGS`。注意 XRT 流程没有 `-DASIC`（它是 FPGA，不是 ASIC），这与 Yosys/Synopsys 的 `-DASIC` 形成对照。

流水线核心是 `gen-xo` 与 `gen-bin` 两个目标：

[hw/syn/xilinx/xrt/Makefile:L195-L201](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile#L195-L201) —— `gen-xo` 调 Vivado batch 跑 `gen_xo.tcl`，产出 `vortex_afu.xo`（综合后的网表容器）；`gen-bin` 调 `$(VPP)`（即 `v++`）加 `VPP_FLAGS`（含 `--target --platform --kernel_frequency`）把 `.xo` 链成 `.xclbin`。`KERNEL_FREQ ?= 300`（第 118 行）是默认 300 MHz 的内核时钟目标。

PPA 报告的「搬运工」是 `report` 目标：

[hw/syn/xilinx/xrt/Makefile:L207-L215](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile#L207-L215) —— Vivado/Vitis 把中间产物埋在很深的 `_x/...` 路径下，`report` 目标把它们 `cp` 到 `bin/`：综合利用率报告 `ulp_vortex_afu_1_0_utilization_synth.rpt`、布局布线后利用率与 timing summary。这是 XRT 流程 PPA 报告的最终落点。

DUT 流程的报告生成则在 `project.tcl` 的 `run_report` 里，它还顺手算出 Fmax 写进 `synth_summary.csv`：

[hw/syn/xilinx/dut/project.tcl:L287-L297](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/dut/project.tcl#L287-L297) —— 这段是全讲义最重要的 PPA 公式落点。它从 utilization 报告里抠出 `CLB LUTs/CLB Registers/Block RAM Tile/DSPs`，从 timing 里取 `WNS`，然后用

\[ \text{Fmax} = \frac{1000}{T_{\text{clk}} - \text{WNS}} \quad (\text{MHz, } T \text{ 单位 ns}) \]

算出 Fmax，连同资源数写进 `synth_summary.csv`。这一行 Tcl 把整个 DUT 流程的 PPA 浓缩成一张机器可读的表。

Altera OPAE 流程（已废弃）入口在 [hw/syn/altera/opae/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/altera/opae/Makefile)：

[hw/syn/altera/opae/Makefile:L124-L160](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/altera/opae/Makefile#L124-L160) —— `all: swconfig ip-gen setup build` 四步：先生成 IP 缓存（`altera_ip_gen.sh`）、再做 setup（`afu_synth_setup` 或仿真用 `afu_sim_setup`）、最后 `build` 调 `$(RUN_SYNTH)`（本地是 OPAE 的 `run.sh`，有 `qsub-synth` 集群时换成它）。它同样先 `gen_sources.sh`、同样 `include extensions.mk`、同样算 `XCONFIGS`（第 72 行）——印证 4.1 的统一骨架。

#### 4.2.4 代码实践

**实践目标**：沿 XRT 流程入口追出「从 RTL 到 `.xclbin`」的完整命令链，并标出 PPA 报告落点。

**操作步骤**（源码阅读型）：

1. 从 [hw/syn/xilinx/xrt/Makefile:L181](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/xrt/Makefile#L181) 的 `all` 目标出发：`all: check-devices emconfig $(XCLBIN_CONTAINER) report`。
2. 跟 `$(XCLBIN_CONTAINER)` → 第 199–201 行 `gen-bin` → 依赖 `$(XO_CONTAINER)` → 第 196–197 行 `gen-xo` → 依赖 `$(BUILD_DIR)/sources.txt` → 第 184–185 行 `gen-sources`（调 `gen_sources.sh`）。
3. 在每个节点记下调用的工具：`gen_sources.sh` → `vivado ... gen_xo.tcl` → `v++`。
4. 最后看 `report`（第 207–215 行），记下它把哪些 `.rpt` 拷到 `bin/`。

**需要观察的现象**：依赖链是 `gen-sources → gen-xo → gen-bin → report`，自底向上；`PLATFORM` 必须显式给出（`check-devices` 会在缺失时报错退出）。

**预期结果**：画出一条从 `make` 到 `.xclbin` 的命令链，标注「PPA 报告在 `$(BUILD_DIR)/bin/` 下的 `*_utilization_synth.rpt` 与 `*_timing_summary_routed.rpt`」。如果你手头有装好 Vitis 的机器，可用 `hw/syn/xilinx/README` 里的 `PREFIX=build_base_1c NUM_CORES=1 TARGET=hw_emu PLATFORM=xilinx_u55c_... make` 跑一次 `hw_emu` 验证；否则标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：XRT 流程的 `CONFIGS` 里有 `-DVIVADO`，而 Yosys 流程是 `-DYOSYS -DASIC`。这些「后端名宏」在 RTL 里大概会起什么作用？
**答案**：它们是条件编译开关，让 RTL 在不同后端/语境下选择不同的实现片段。例如 ASIC 语境会例化 `VX_dp_ram_asic`（可综合的 SRAM 黑盒）而非仿真用的行为 RAM；`-DVIVADO` 可能绕开 Vivado 不支持的某些 SystemVerilog 语法。这与 4.1 的「源树零改动、靠宏分支」一脉相承。

**练习 2**：为什么 DUT 流程要单独存在，而不是直接综合整个 `top`？
**答案**：综合整个带 AFU 平台的 `top` 极慢（要带 PCIe 接口、内存控制器等），而研究者常只想评估「单个 core 多大面积」「加了 TCU 后 Fmax 掉多少」。DUT 流程把单个子部件剥离平台外壳单独综合，几十分之一的时间就能拿到该单元的 PPA，是快速迭代微架构参数的关键手段。

---

### 4.3 ASIC 流程：Yosys 开源与 Synopsys Design Compiler 商用

#### 4.3.1 概念说明

ASIC 综合不产出比特流，而产出一个**工艺映射后的门级网表**（`.v`）和一套时序/功耗/面积报告。Vortex 提供两条 ASIC 流程：

- **Yosys 流程**（`hw/syn/yosys/`）：全开源。Yosys 做综合、ABC 做工艺映射、OpenSTA 做静态时序分析。默认用 NanGate 15nm 开放单元库。适合无商用 EDA 授权的场景与 CI。
- **Synopsys Design Compiler 流程**（`hw/syn/synopsys/`）：商用 ASIC 事实标准。支持 NanGate 15nm、ASAP7 7nm、SAED 14nm 三套库，QoR 更高（`compile_ultra -retime`）。

两条流程面临一个共同难题：**SRAM 怎么算**。RTL 里的 `VX_dp_ram_asic`（双口）/ `VX_sp_ram_asic`（单口）是行为模型，标准单元库里没有现成的 RAM。Vortex 用三种策略处理它，这三种策略正是两条流程的核心差异所在（详见 4.3.3）。

#### 4.3.2 核心流程

Yosys 流程由 `run_synth.sh` 驱动，分三个可独立触发的阶段：

```text
RUN_SYNTH=1：generic 综合（proc/opt/fsm/memory/memory_map/techmap）→ _syn.v
RUN_MAP=1  ：工艺映射（dfflibmap + abc -liberty）→ _mapped.v + stat_lib.rpt
RUN_STA=1  ：OpenSTA 静态时序 → sta.log；可选 SAIF 功耗 → power.rpt
            + sram_cost.py 从 JSON 网表估算 SRAM 面积 → sram_area.rpt
```

注意 Yosys 不能直接读 SystemVerilog，要先用 `sv2v` 转成 Verilog（见 Yosys Makefile 的 `sv2v` 目标）。

Synopsys 流程由 `project.tcl` 驱动 `dc_shell`，一条龙完成 analyze→elaborate→link→compile→report：

```text
parse sources.txt → analyze（严格模式，逐文件）→ elaborate $TOP → link
   → （SRAM 黑盒面积估算 or 真实宏单元）→ compile_ultra -retime
   → report_qor/area/timing/power → write .mapped.{v,ddc,sdf}
```

时钟约束由 `CLOCK_FREQ`（MHz）反推周期，再乘以 `DELAY_UNC`（默认 2%）得时钟不确定性、乘以 `DELAY_IO`（默认 5%）得 I/O 延迟。两套 ASIC 流程共享这套约束推导。

#### 4.3.3 源码精读

先看 SRAM 的核心公式（两套流程都用），面积由位宽 × 深度估算：

[hw/syn/synopsys/project.tcl:L496-L498](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl#L496-L498) —— 定义 `SRAM_BIT_AREA=0.1 um²/bit`、`SRAM_OH_AREA=100.0 um²`，随后在黑盒流程里用

\[ \text{Area}_{\text{SRAM}} = (\text{width} \times \text{depth} \times \text{SRAM\_BIT\_AREA}) + \text{SRAM\_OVERHEAD} \]

给每个 RAM 实例赋面积属性（见第 772–783 行），最后打印 `Total Estimated SRAM Area`。Yosys 侧的 `sram_cost.py` 用完全相同的公式，只是从 Yosys JSON 网表而非 DC 属性里推断 width/depth。

Synopsys 的三种 SRAM 策略直接对应三个 Make 目标：

[hw/syn/synopsys/Makefile:L157-L205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile#L157-L205) —— 三者只差一个变量：
- `synthesis`：传 `MEM_LIBS="$(SRAM_DB)"`，走真实宏单元流程——`project.tcl` 从 SRAM `.db` 自省出引脚、自动生成 `VX_sp_ram_asic.v`/`VX_dp_ram_asic.v` wrapper（见 `gen_sram_wrappers`，第 317–470 行）。
- `synthesis-nosram`：不传 `MEM_LIBS` 也不传 `BB_MODULES`，让 DC 自己推断 RAM。
- `synthesis-estsram`：传 `BB_MODULES="VX_dp_ram_asic,VX_sp_ram_asic"`，黑盒掉 RAM 后用上面的位宽×深度公式估面积。

库选择由 `LIB_TYPE` 驱动一张表：

[hw/syn/synopsys/Makefile:L22-L54](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile#L22-L54) —— `LIB_TYPE` 取 `DEFAULT/ASAP7/SAED14`，对应三套库的 `LIB_ROOT/SRAM_LIB/SRAM_DB/NAME`，第 47–54 行用 `$($(LIB_TYPE)_LIB_ROOT)` 选中。默认 `DEFAULT` 用仓库自带的 NanGate 15nm，ASAP7/SAED14 指向 `/mnt/nas0/eda.libs/`（待本地确认这些 NAS 路径在你的环境是否可达）。

时钟约束的推导在 `project.tcl`，它会处理库时间单位的换算：

[hw/syn/synopsys/project.tcl:L842-L857](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl#L842-L857) —— 由 `CLOCK_FREQ`（MHz）算 `period_ns = 1000 / CLOCK_FREQ`，再按库时间单位（ns/ps/fs）乘 `NS_TO_LIB` 缩放，得 `target_period`、`target_uncertainty = period × DELAY_UNC`、`target_io_delay = period × DELAY_IO`。这段是「一个 `CLOCK_FREQ` 旋钮驱动所有时序约束」的落点。

综合优化等级 `OPT_LEVEL` 在两条流程里都被标准化为同一语义：

[hw/syn/synopsys/project.tcl:L874-L894](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl#L874-L894) —— `OPT_LEVEL` 0/1/2/3 分别映射 `compile -map_effort low` / `medium` / `compile_ultra` / `compile_ultra -retime`（默认 3，最高 QoR、最慢）。综合后还检查是否残留未映射的 `GTECH` 单元（第 891–894 行），有则报 FATAL——这是「综合没跑完」的硬护栏。

Yosys 侧的等价物是 `run_synth.sh` 生成的 `synth.ys` 脚本：

[hw/syn/yosys/run_synth.sh:L182-L199](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/run_synth.sh#L182-L199) —— `RUN_SYNTH` 阶段依次 `proc; opt` / `fsm; opt` / `memory; opt` / `memory_map; opt` / `alumacc; wreduce; share; opt` / `techmap; opt`，写 `_syn.v`；`RUN_MAP` 阶段 `dfflibmap` + `abc -D $(ABC_PERIOD) -liberty` 做工艺映射，写 `_mapped.v` 与 `stat_lib.rpt`。这里的 `ABC_PERIOD` 就是时钟周期（ns）。

最后，两条流程的报告落点对照（详见 [docs/synthesis_analysis.md:L411-L525](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L411-L525)）：Yosys 在 `<BUILD_DIR>/reports/`（`stat_lib.rpt` 面积、`sta.log` 时序、`power.rpt` 功耗），Synopsys 在 `<BUILD_DIR>/reports/`（`area.rpt`、`qor.rpt`、`timing_max.rpt`、`power_active.rpt`/`power_vectorless.rpt`）。

#### 4.3.4 代码实践

**实践目标**：用 Synopsys 的三个 Make 目标，理解 SRAM 的三种处理策略如何只换一个变量就切换。

**操作步骤**（源码阅读型）：

1. 打开 [hw/syn/synopsys/Makefile:L157-L205](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile#L157-L205)，把三个目标（`synthesis`/`synthesis-nosram`/`synthesis-estsram`）的环境变量逐行 diff。
2. 你会发现唯一的差别是：`synthesis` 多了 `MEM_LIBS="$(SRAM_DB)"`，`synthesis-estsram` 多了 `BB_MODULES="VX_dp_ram_asic,VX_sp_ram_asic"`，`synthesis-nosram` 两者都没有。
3. 再到 [project.tcl:L590-L621](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl#L590-L621) 看 `project.tcl` 如何据这两个变量分叉：`MEM_LIBS` 非空走真实宏单元（`gen_sram_wrappers`），否则走黑盒估算（`BB_MODULES`）。

**需要观察的现象**：三个目标共享 `dc_shell -f project.tcl`，TCL 内部用 `use_mem_libs` 与 `BB_MODULES` 两个开关二选一分支。这是「同一引擎、不同 SRAM 策略」的干净设计。

**预期结果**：写出一张「目标名 → 多传的变量 → project.tcl 走的分支 → SRAM 面积来源」对照表（如 `synthesis-estsram` → `BB_MODULES` → 黑盒+估算 → 来自 `width×depth×0.1+100` 公式）。若你有 dc_shell 授权，可 `PREFIX=test make synthesis-estsram` 验证 `Total Estimated SRAM Area` 打印；否则标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：Yosys 流程为什么要先用 `sv2v`？
**答案**：Yosys 对 SystemVerilog 的支持有限（尤其 interface、包、部分语法），而 Vortex RTL 是 SystemVerilog。`sv2v` 把 `.sv` 转成可被 Yosys 消化的 Verilog，随后 `run_synth.sh` 还会用 `sed` 删掉 `$fatal/$error/$warning/$info/$stop` 这些 Yosys 不能综合的系统任务（这些在 cvfpu 源码里出现）。

**练习 2**：`CLOCK_FREQ=800`（默认）意味着什么？它和最终 Fmax 是什么关系？
**答案**：`CLOCK_FREQ` 是**目标**频率（800 MHz），综合工具据此设时钟周期约束（1/800 GHz = 1.25 ns）并尽力收敛。最终实际能达到的 **Fmax** 要看综合后时序报告里的 WNS：若 WNS≥0 说明 800 MHz 达标；若 WNS<0 则实际 Fmax = 1/(period − WNS) < 800 MHz。`CLOCK_FREQ` 是「请求」，Fmax 是「结果」。

---

### 4.4 PPA 报告解读：Fmax、面积与功耗

#### 4.4.1 概念说明

PPA 的三件事分别读不同的报告：

- **Performance（Fmax）**：读时序报告，取最差建立路径的 WNS，反推 Fmax。
- **Area（面积/资源）**：ASIC 读面积报告（`area.rpt`/`stat_lib.rpt`，单位 µm²），FPGA 读利用率报告（LUT/FF/BRAM/DSP 个数）。
- **Power（功耗）**：读功耗报告，分动态（内部 + 开关）与静态（漏电）。

功耗分析有一条贯穿四条流程的关键二分法：**vectorless vs. 活动标注**。vectorless 让工具假设一个默认翻转率（典型 12.5%）和静态概率 0.5，给出粗略基线；活动标注（SAIF/VCD）则用仿真捕获的真实翻转活动，给出贴近特定负载的功耗。两者的差距往往很大，所以规范的做法是「先 vectorless 建基线、再用 SAIF 标注真实负载」，并 diff 两份报告找出与默认假设偏离最大的模块。

#### 4.4.2 核心流程

生成 SAIF 标注功耗的标准链路（见 [docs/synthesis_analysis.md:L79-L117](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L79-L117)）：

```text
1. 用 SAIF=1 编译仿真器：  make -C sim/rtlsim SAIF=1
                          或 ./ci/blackbox.sh --driver=rtlsim --app=sgemm --saif
2. 跑一个有代表性的负载：   仿真器写 trace.saif
3. 用 SAIF 重算功耗（不重新综合）：
      Xilinx： make power SAIF_FILE=... SAIF_INST=TOP.vortex_afu_shim.vortex_afu
      Synopsys：make synthesis SAIF_FILE=... SAIF_INST=...
4. diff vectorless vs SAIF 两份报告
```

关键概念是 `SAIF_INST`：仿真产生的 SAIF 信号名带 testbench 层级前缀（如 `TOP.rtlsim_shim.vortex.<sig>`），综合网表的信号名没有这个前缀。`SAIF_INST` 指定要剥掉的前缀，使两侧信号对齐；若 SAIF 根作用域已与顶层模块同名，则留空。

PPA 三类报告的读取口径统一为下表（Fmax 公式两套流程通用）：

| 指标 | Xilinx | Synopsys | Yosys |
|------|--------|----------|-------|
| Fmax | `timing.rpt` 的 WNS → 1000/(period−WNS) | `timing_max.rpt` 首路径 slack | `sta.log` 的 `report_wns` |
| 面积 | `post_impl_util.rpt`（LUT/FF/BRAM/DSP） | `area.rpt` 的 `Total cell area`（µm²） | `stat_lib.rpt` 的 `Chip area` + `sram_area.rpt` |
| 功耗 | `power_saif.rpt` / `power_vectorless.rpt` | `power_active.rpt` / `power_vectorless.rpt` | `power.rpt` / `power_hier.rpt` |

#### 4.4.3 源码精读

Xilinx 共享功耗脚本是理解 vectorless/SAIF 二分的最佳入口：

[hw/scripts/xilinx_power_analysis.tcl:L142-L171](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/xilinx_power_analysis.tcl#L142-L171) —— 先 `reset_switching_activity -all` + `set_switching_activity -default_toggle_rate 0.125 -default_static_probability 0.5` 生成 vectorless 基线 `power_vectorless.rpt`；再 `read_saif -strip_path $strip_path` 读真实活动、生成 `power_saif.rpt`。脚本结尾还提示「diff the two reports to see which modules' activity differs most from the 12.5% vectorless assumption」——这正是规范工作流。它还能在 `SAIF_INST` 未设时自动解析 SAIF 文件推断前缀（第 112–140 行）。

这个脚本被 DUT 流程和 XRT 流程共用，只是 checkpoint 路径解析不同：

[hw/scripts/xilinx_power_analysis.tcl:L49-L66](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/scripts/xilinx_power_analysis.tcl#L49-L66) —— 优先取显式 `DCP_FILE`，其次从 `BUILD_DIR` 下 `_x/link/vivado/vpl/prj/prj.runs/impl_1/` 搜 routed checkpoint（XRT 流程），最后回退到当前目录 `post_impl.dcp`（DUT 流程）。一份脚本适配两条流程。

DUT 流程里 `run_power_report` 还多支持一种 VCD 标注：

[hw/syn/xilinx/dut/project.tcl:L201-L244](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/xilinx/dut/project.tcl#L201-L244) —— vectorless 总会生成（`power_vectorless.rpt`）；若设了 `VCD_FILE` 则额外用 `read_vcd -strip_path $VCD_INST` 生成 `power_vcd.rpt`。注意它始终 `set_switching_activity -deassert_resets`，避免复位脉冲虚高功耗。

Synopsys 侧的功耗分支在 `project.tcl` 末尾，逻辑与 Xilinx 镜像：

[hw/syn/synopsys/project.tcl:L905-L927](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/project.tcl#L905-L927) —— 有 `SAIF_FILE` 则 `read_saif -instance_name $SAIF_INST` + `update_power` + `report_power -hierarchy > power_active.rpt`，并写 `saif_annotation_coverage.rpt`（标注覆盖率）；否则 `report_power > power_vectorless.rpt`。这里的 `-instance_name` 与 Xilinx 的 `-strip_path` 是同一目的的两种 API。

最后看功耗的构成拆解（四条流程通用，权威说明见 [docs/synthesis_analysis.md:L534-L543](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L534-L543)）：

\[ P_{\text{total}} = \underbrace{P_{\text{internal}} + P_{\text{switching}}}_{\text{动态功耗}} + \underbrace{P_{\text{leakage}}}_{\text{静态功耗}} \]

动态功耗来自信号翻转（内部短路电流 + 负载电容充放电），静态功耗来自亚阈值/栅漏电。FPGA 功耗报告还多出 clocking、I/O、BRAM、DSP 等器件专属项，ASIC 则没有。

#### 4.4.4 代码实践

**实践目标**：给定一份综合报告，独立提取 Fmax、资源/面积、功耗三类指标，并解释 vectorless 与 SAIF 的差异来源。

**操作步骤**（源码阅读型，模拟「拿到报告后怎么读」）：

1. **Fmax**：打开 [docs/synthesis_analysis.md:L263-L269](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L263-L269) 的「Finding Key Metrics」段。确认 Xilinx 的口径：在 `timing.rpt` 找 WNS，Fmax = 1/(clock_period − WNS)。这与 4.2.3 里 `project.tcl` 第 290 行的公式完全一致——你可以把那行 Tcl 当作权威定义。
2. **面积/资源**：读同段对 LUT/DSP/BRAM 的定位说明（Xilinx 在 `post_impl_util.rpt` 找 `CLB LUTs`/`DSPs`/`Block RAM Tile` 行）。
3. **功耗**：读 [docs/synthesis_analysis.md:L544-L549](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L544-L549) 的「Vectorless vs. Activity-Annotated」。vectorless 假设 12.5% 翻转率，SAIF 用真实活动；「Signals not covered by the SAIF/VCD fall back to the default toggle rate」——这就是两份报告数字不同的根因。

**需要观察的现象**：Fmax 完全由 WNS 决定；面积口径因 ASIC（µm²）/FPGA（器件个数）而异；功耗数字在 vectorless 与 SAIF 间可能差几倍，差异最大的模块往往是缓存、FPU、TCU 等高活动块。

**预期结果**：写出一张「指标 → 报告文件 → 在文件里找哪一行/哪个关键词 → 单位」的速查表。例如「Fmax → Xilinx `timing.rpt` → WNS 行 → 用 1000/(period−WNS) 算 MHz」。若你能在本机跑 `./ci/blackbox.sh --driver=rtlsim --app=sgemm --cores=4 --saif` 生成 `trace.saif`，再用某条 `make power` 流程消费它，即可得到真实数字；否则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：向量less 功耗报告显示 5 W，SAIF 标注后显示 2 W，哪个更可信？为什么？
**答案**：对于你跑的那个特定负载，SAIF 标注的 2 W 更可信，因为它用了真实翻转活动。但 vectorless 的 5 W 仍有价值——它是一个不依赖负载的「上界式」基线，反映「在最悲观默认翻转率下」的功耗。两者都看，并 diff 找出偏离最大的模块，才是完整分析。

**练习 2**：`SAIF_INST` 设错（漏掉一层或多了层）会发生什么？
**答案**：信号名无法对齐，工具会把绝大多数信号当作「未标注」回退到默认翻转率，导致 `power_saif.rpt` 退化得接近 vectorless、失去意义。护栏是检查 `saif_annotation_coverage.rpt`（Synopsys）或 `read_saif_mismatch.rpt`（Xilinx）的标注覆盖率；Xilinx 脚本还能在 `SAIF_INST` 未设时自动解析推断（见 4.4.3）。

---

## 5. 综合实践

设计一个贯穿本讲的迷你任务：**用一条流程，从配置到 PPA 报告走一遍，并比较两个不同微架构配置的 PPA 差异。**

背景：你想评估「把 L2 缓存关掉、只留单核」相比「4 核带 L2」在面积和频率上的代价。请完成以下步骤：

1. **选流程**：若你有 dc_shell/yosys 授权，选 [hw/syn/synopsys/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/synopsys/Makefile) 或 [hw/syn/yosys/Makefile](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/hw/syn/yosys/Makefile)；否则只做源码阅读。
2. **构造两个配置**（用 `NUM_CORES` 语法糖 + `PREFIX` 隔离，参见 [docs/synthesis_analysis.md:L57-L75](https://github.com/vortexgpgpu/vortex/blob/d76b7f24e658867ab57e3942d7c648c3e6af072d/docs/synthesis_analysis.md#L57-L75)）：
   - 配置 A：`PREFIX=build_4c NUM_CORES=4 make synthesis`（4 核带 L2）。
   - 配置 B：`PREFIX=build_1c NUM_CORES=1 make synthesis`（单核无 L2）。
3. **提取 PPA**：从两份 `reports/` 里读 `area.rpt`（或 `stat_lib.rpt`）的面积、`timing_max.rpt`（或 `sta.log`）的 WNS 算 Fmax、`power_vectorless.rpt` 的总功耗。
4. **对比**：填一张三列表（面积 µm² / Fmax MHz / 功耗 W），解释「4 核带 L2」相比单核增加了多少面积、Fmax 是否因缓存路径掉下来。
5. **若无法运行**：标注「待本地验证」，但必须完成「读源码指出每个指标对应哪个报告文件、哪一行」的纸面作业——这正是 4.4 的速查表。

这个任务把「配置系统（u2）→ 综合流程（4.1–4.3）→ PPA 解读（4.4）」串成一条链：改一个 `NUM_CORES` 旋钮，经 `gen_config.py` 流到 RTL，经综合变成网表，最后在报告里体现为可量化的 PPA 差异。

## 6. 本讲小结

- 四条综合流程（Xilinx/Altera FPGA、Yosys/Synopsys ASIC）共享同一骨架：`CONFIGS → gen_config.py → XCONFIGS`、`extensions.mk` 按扩展接线、`gen_sources.sh` 生成源清单、`PREFIX` 隔离多构建——差异只在最后一行的工具命令。
- Xilinx XRT 是受支持主路径（`gen-xo → gen-bin → .xclbin`），Altera OPAE 已废弃；两者都额外提供 DUT 子部件流程用于快速评估单个单元的 PPA。
- Yosys（开源）与 Synopsys DC（商用）是 ASIC 双轨，核心差异在 SRAM 处理（黑盒估算 / DC 推断 / 真实宏单元 wrapper）与库选择（`LIB_TYPE` 驱动 NanGate/ASAP7/SAED14）。
- PPA 三件套：Fmax 由 WNS 反推（`1000/(period−WNS)`）、面积读资源/面积报告、功耗分动态/静态；功耗分析的关键二分是 vectorless（默认 12.5% 翻转率基线）vs SAIF 标注（真实负载），用 `SAIF_INST` 剥 testbench 前缀对齐信号。
- `OPT_LEVEL`（0–3）在四条流程里被标准化为同一语义——从最快编译到最高 QoR（`compile_ultra -retime` / Vivado 性能策略）。
- 综合流程是配置系统的下游消费者：`gen_config.py --cflags` 是综合与 `VX_config.toml` 真相来源的唯一接口，这保证了「改 toml → 重 configure → 综合」的一致性。

## 7. 下一步学习建议

- **走向二次开发**：本讲是 u14-3「扩展 Vortex：自定义 ISA 扩展」的直接前置——新增一个自定义加速器 FuncUnit 后，你需要用本讲的流程综合它、用 DUT 流程评估其 PPA，并用 `extensions.mk` 把新扩展的源接入四条流程。
- **深入 PPA 闭环**：结合 u13-3（性能计数器与 roofline 分析），把「微架构性能（IPC/roofline）」与「物理 PPA（Fmax/面积/功耗）」合成一个完整的评估闭环——前者在 SimX 上量，后者在本讲的综合流程上量。
- **继续阅读的源码**：`hw/syn/synopsys/project.tcl` 的 SRAM wrapper 自动生成（`gen_sram_wrappers`，第 317–470 行）是本讲最复杂的一段，值得单独精读；`hw/syn/yosys/run_synth.sh` 的 `synth.ys` 生成则展示了开源综合的完整 pass 序列。建议两相对照，理解「同一 RTL、两种工具链」的映射差异。
