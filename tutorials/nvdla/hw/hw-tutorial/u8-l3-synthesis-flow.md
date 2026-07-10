# 综合流程：SDC 与 Synopsys DC

## 1. 本讲目标

本讲是配置、综合与端到端集成单元（单元 8）的第三篇，回答一个问题：**前面所有讲义里读到的 SystemVerilog RTL，是怎么变成一块能交给代工厂流片的门级网表的？**

学完后你应当能够：

1. 说清 NVDLA 为什么按 `partition_a/c/m/o/p` 五个分区**分别**综合，以及每个分区的 SDC 时序约束有什么共同点和差异。
2. 读懂 `dc_run.tcl` 这条 Design Compiler（DC）主流程脚本，掌握它从读 RTL、施约束、编译到出网表的完整阶段划分。
3. 区分 `wlm / dct / dcg / de` 四种综合模式各自调用的 shell、需要的工艺库变量与适用场景。
4. 看懂 `syn_launch.sh` 如何编排出一个干净的 sandbox，并把 RTL、约束、脚本汇聚进去再驱动 DC。
5. 独立为 `partition_c`（卷积核心）挑选一种综合模式、列出必需的 `config.sh` 变量，并解释 SDC 如何约束卷积核心时序。

## 2. 前置知识

在进入综合脚本前，先用三段话补齐基础概念。如果你已熟悉综合流程，可跳过本节。

- **综合（Synthesis）**：把寄存器传输级（RTL）的 Verilog 描述，映射到某个工艺库里真实存在的标准单元（standard cell，如与门、触发器、加法器），得到门级网表（gate-level netlist，`.gv`）的过程。工具是 Synopsys Design Compiler（DC）。综合不仅要"功能对"，还要"时序收敛"——即在目标时钟频率下，所有触发器到触发器的路径都能在一个周期内走完。
- **SDC（Synopsys Design Constraints）**：一套用 Tcl 语法写的时序约束描述，告诉综合工具：时钟是谁、多快、哪些路径不用管（false path）、哪些信号是理想复位（ideal network）。SDC 是综合、布局布线、时序分析共用的"约束单一来源"。本讲的 `syn/cons/*.sdc` 就是每个分区的 SDC。
- **分区综合（block-level / partition-level synthesis）**：NVDLA 顶层 `NV_nvdla` 太大，一次性综合既慢又难收敛。于是把它切成 `partition_a/c/m/o/p` 五个块（block），每块**独立综合**，最后在顶层再做一次组装与增量综合。这与 u1-l5 讲的顶层分区结构完全对应：a=累加器 CACC、c=卷积前段 CDMA/CBUF/CSC、m=CMAC 乘加阵列、o=中央枢纽、p=SDP 后处理。

此外需要回忆 u1-l3 讲的构建链（tmake/build.config）与 u6-l1 讲的时钟域（core 域 `nvdla_core_clk`、falcon 域 `nvdla_falcon_clk`）、u6-l3 讲的 RAM 仿真/综合双模型——本讲会反复用到这些结论。

## 3. 本讲源码地图

本讲涉及的关键文件全部在 `syn/` 目录下，共 9 个，分三类：

| 文件 | 作用 |
| --- | --- |
| `syn/README.md` | 综合使用说明：wlm 与物理综合各需哪些变量 |
| `syn/scripts/syn_launch.sh` | **编排脚本**（bash）：解析参数、建 sandbox、按模式驱动 DC |
| `syn/scripts/dc_run.tcl` | **DC 主流程**（Tcl）：读 RTL→施约束→编译→出网表 |
| `syn/scripts/dc_app_vars.tcl` | DC 应用变量调优脚本，被 dc_run.tcl source |
| `syn/scripts/dc_interactive.tcl` | 恢复 DDC 进入交互调试的脚本 |
| `syn/scripts/default_config.sh` | 所有流程变量的默认值（大多为空） |
| `syn/templates/config.sh` | **用户配置模板**：TSMC 16FF 工艺库的真实示例 |
| `syn/templates/cg_latency_lut.tcl` | 时钟门控使能路径的过约束查找表 |
| `syn/cons/NV_NVDLA_partition_{a,c,m,o,p}.sdc` | 五个分区各一份的 SDC 时序约束 |

一句话定位：`syn_launch.sh` 是"导演"，`dc_run.tcl` 是"剧本"，`config.sh` 是"道具清单"（工艺库路径），`*.sdc` 是"时序规则"，`default_config.sh` 是所有变量的兜底默认值。

## 4. 核心概念与源码讲解

### 4.1 综合流程总览：从一条命令到一份网表

#### 4.1.1 概念说明

NVDLA 的综合是"参考流程"（reference methodology）：仓库不提供工艺库（那是代工厂的保密资产），只提供一套**与工艺无关的脚本骨架**，由集成者填入自己的工艺库路径后即可运行。整套流程的入口只有一条命令：

```
<RELEASE>/syn/scripts/syn_launch.sh -mode <wlm|dct|dcg|de> -config /path/to/config.sh
```

这条命令背后是一条清晰的数据流水线：

```
syn_launch.sh
   |  1. 解析 -mode/-config/-build 等参数
   |  2. source default_config.sh（兜底默认值）
   |  3. source 用户 config.sh（工艺库路径、TOP_NAMES）
   |  4. dataprep：建 sandbox，拷 RTL/约束/脚本，生成 *.files.vc 依赖清单
   v
dc_shell / dc_shell-t / de_shell   （按 -mode 选不同 shell）
   |  5. 执行 dc_run.tcl
   |      a. 读工艺库、analyze+elaborate RTL
   |      b. read_sdc 施加分区约束
   |      c. compile_ultra / compile_exploration 综合
   |      d. write 输出网表 .gv / DDC / SDC / 报告
   v
<build>/net/<MODULE>.gv     门级网表（最终产物）
<build>/db/<MODULE>.ddc     DC 数据库（可恢复调试）
<build>/report/*.report     QoR/timing/area/power 报告
```

关键设计：`syn_launch.sh` 不直接调 DC，而是先把所有输入"摊平"进一个带时间戳的 sandbox（如 `nvdla_syn_20260709_1430/`），再让 DC 在 sandbox 内工作。这样多次综合互不干扰，产物可追溯。

#### 4.1.2 核心流程

1. 用户敲 `syn_launch.sh -mode wlm -config config.sh`。
2. 脚本先 `source` 默认变量，再 `source` 用户配置；校验 `DC_PATH`（DC 安装路径）与 `TOP_NAMES`（要综合哪些顶层）非空。
3. `dataprep()` 函数清理旧 sandbox、建子目录、把 RTL/头文件/约束/脚本拷进去，并为每个顶层模块生成一份 `.files.vc` 依赖清单（含 `+define+NV_SYNTHESIS` 等综合专用宏）。
4. 按 `-mode` 选择 DC 的可执行程序（`dc_shell`、`dc_shell-t -topographical_mode` 或 `de_shell`），对 `TOP_NAMES` 里**每个**分区依次跑 `dc_run.tcl`。
5. `dc_run.tcl` 完成读库→分析→施约束→编译→写网表，日志落在 `<build>/log/`，报告落在 `<build>/report/`。

#### 4.1.3 源码精读

先看 `syn_launch.sh` 的入口：解析参数、装载两份配置。默认 `mode=wlm`、`config=./config.sh`、`build` 带时间戳：

[syn/scripts/syn_launch.sh:23-30](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L23-L30) —— 设置默认值：`mode` 默认 `wlm`，`build` 默认 `nvdla_syn_<时间戳>`。

[syn/scripts/syn_launch.sh:66-75](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L66-L75) —— 先 `source default_config.sh` 兜底，再 `source` 用户 `config.sh` 覆盖；若 config 文件不存在直接报错退出。

[syn/scripts/syn_launch.sh:83-88](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L83-L88) —— 校验 `TOP_NAMES` 非空：若用户没显式传 `-modules`，就用 `config.sh` 里的 `TOP_NAMES`（即五个分区）。

接着是 `dataprep()` 里生成 `.files.vc` 的关键片段——它给 RTL 编译注入三个综合专用宏：

[syn/scripts/syn_launch.sh:176-198](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L176-L198) —— 为每个模块生成依赖清单：`-y` 指库目录、`+incdir+` 指头文件目录、`+libext+` 指扩展名，并注入 `+define+DISABLE_TESTPOINTS`、`+define+NV_SYNTHESIS`、`+define+RAM_INTERFACE` 三个宏，最后写上顶层模块名 `<MODULE>.v`。

这三个宏正是 u6-l3 讲过的 RAM 双模型切换开关：`RAM_INTERFACE` 让 `vmod/rams/model/` 下的 RAM 文件切到"综合空壳"身份，`NV_SYNTHESIS` 关掉仿真专用断言/监视器。也就是说，综合时用的 RTL 源码与仿真时是**同一份文件**，靠宏切换身份——这与 u6-l3 的结论完全一致。

最后看模式分发，它决定调用哪个 DC shell：

[syn/scripts/syn_launch.sh:212-229](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L212-L229) —— `dct`/`dcg` 模式调 `dc_shell-t -topographical_mode`（带物理版图的拓扑模式）跑 `dc_run.tcl`。

[syn/scripts/syn_launch.sh:230-247](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L230-L247) —— `wlm` 模式调普通 `dc_shell`（非物理、用线负载模型）跑 `dc_run.tcl`。

[syn/scripts/syn_launch.sh:248-265](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L248-L265) —— `de` 模式调 `de_shell`（Design Explorer，探索模式）跑 `dc_run.tcl`。

注意三处都是"对 `modules` 里每个分区循环跑一次"，所以五个分区是**串行**依次综合的。

#### 4.1.4 代码实践

**实践目标**：在不实际安装 DC 的前提下，跑通 `syn_launch.sh` 的参数解析与 sandbox 建立阶段，观察它如何把分散的 RTL 收拢进一个目录。

**操作步骤**：

1. 复制一份模板配置到工作目录并改一个不存在的 `DC_PATH`（避免误触发真实综合）：
   ```bash
   cp syn/templates/config.sh /tmp/myconfig.sh
   # 编辑 /tmp/myconfig.sh，把 DC_PATH 改成 "/tmp/fake_dc/bin"
   ```
2. 用 `-qa_mode link_only` 之外的方式，但故意只让它跑到 dataprep 后报 `DC_PATH` 校验失败——直接运行：
   ```bash
   bash syn/scripts/syn_launch.sh -mode wlm -config /tmp/myconfig.sh
   ```
   预期它会先打印 `Sourcing default flow variables`、`Sourcing user synthesis configuration`，然后建出 `nvdla_syn_<时间戳>/` sandbox 并拷贝 RTL，最后在 DC_PATH 校验处报错退出。

**需要观察的现象**：

- sandbox 目录 `nvdla_syn_<时间戳>/` 是否被创建？其下 `src/`、`cons/`、`scripts/`、`log/` 等子目录是否齐全？
- `src/` 里是否出现了从 `vmod/nvdla/*`、`vmod/rams/synth`、`vmod/vlibs` 拷来的 `.v` 文件？
- `scripts/` 下是否生成了 5 份 `NV_NVDLA_partition_*.files.vc`？打开 `NV_NVDLA_partition_c.files.vc`，确认其中含有 `+define+NV_SYNTHESIS` 与 `+define+RAM_INTERFACE`。

**预期结果**：sandbox 与 `.files.vc` 正常生成，脚本最终因 `DC_PATH` 指向不存在的 `dc_shell` 而在 [syn/scripts/syn_launch.sh:207-210](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L207-L210) 处报 `[ERROR]: DC_PATH cannot be empty. Aborting` 或运行时报 command not found。真实 DC 综合能否跑通需安装 Synopsys DC 与 TSMC 工艺库，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`syn_launch.sh` 为什么要在 `dataprep()` 里把 `DW_*` 文件从 `src/` 删掉？

**参考答案**：`DW_*` 是 DesignWare（Synopsys 自带 IP 库）的源码文件。综合时 DesignWare 单元应由 DC 直接从 `dw_foundation.sldb` 等库里例化（见 `config.sh` 的 `LINK_LIB`），而不是用 RTL 源码展开。删掉 `src/DW_*` 是为了避免 RTL 编译时把 DesignWare 当普通源码 elaboration 导致重复定义或版本冲突。对应代码在 [syn/scripts/syn_launch.sh:169-173](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L169-L173)。

**练习 2**：`-restore` 参数与正常流程的区别是什么？

**参考答案**：正常流程跑 `dc_run.tcl` 从头综合；`-restore <db>` 跳过 dataprep，跑 `dc_interactive.tcl`，用 `read_ddc` 恢复一个已保存的 DDC 数据库进入交互式调试。对应 [syn/scripts/dc_interactive.tcl:78](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_interactive.tcl#L78)。

---

### 4.2 分区 SDC 约束：按分区施加时序约束

#### 4.2.1 概念说明

SDC 是综合的"时序契约"。NVDLA 给五个分区各写了一份 SDC，放在 `syn/cons/NV_NVDLA_partition_*.sdc`。`dc_run.tcl` 在综合到"施约束"阶段会用 `read_sdc` 把对应分区的 SDC 读进来。

为什么按分区单独写？因为各分区的内容、时钟域、含不含 RAM 都不同——SDC 必须精确反映该分区的真实情况，否则要么过松（时序不收敛）、要么过紧（面积膨胀）。本模块的核心就是对比这五份 SDC 的**同与异**，并理解每条约束背后的硬件含义。

#### 4.2.2 核心流程

一份分区 SDC 大致做四件事：

1. **面积目标**：`set_max_area 0` —— 让 DC 在时序满足的前提下尽量压面积。
2. **复位/测试网络理想化**：用 `set_ideal_network` 把复位、测试模式等异步控制信号标为"理想"（无延迟、不计入时序），避免工具去优化本不该被时序约束的复位树。
3. **创建时钟**：`create_clock` 在时钟端口上定义时钟周期与波形——这是整份 SDC 的核心，决定了该分区跑多快。
4. **切断不该分析的路径**：`set_false_path` 把复位、测试、时钟门控覆盖、RAM 电源等异步或慢速信号的路径从时序分析中剔除。

#### 4.2.3 源码精读

先读卷积核心分区 `partition_c` 的完整 SDC（本讲重点）：

[syn/cons/NV_NVDLA_partition_c.sdc:10-14](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_c.sdc#L10-L14) —— `set_max_area 0` 设面积目标；接着把四个异步/控制信号标为 ideal network：外部复位端口 `direct_reset_`、`dla_reset_rstn`、测试模式 `test_mode`，以及内部复位网 `nvdla_core_rstn`（注意它用 `get_nets` 而非 `get_ports`，且带 `-no_propagate`）。

[syn/cons/NV_NVDLA_partition_c.sdc:15-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_c.sdc#L15-L19) —— 在 `nvdla_core_clk` 端口创建时钟，周期 `0.9` ns、波形 `{0 0.45}`（0 ns 上升、0.45 ns 下降，50% 占空比）；并用 `set_clock_transition` 给时钟设定 0.05 ns 的上升/下降翻转时间（min/max 各一组），供静态时序分析用。

[syn/cons/NV_NVDLA_partition_c.sdc:20-26](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_c.sdc#L20-L26) —— 七条 `set_false_path`：从 `direct_reset_`、`dla_reset_rstn`、`test_mode`、`pwrbus_ram_pd*`（RAM 电源引脚）、`tmc2slcg_disable_clock_gating`（关时钟门控的测试控制）、`global_clk_ovr_on`、`nvdla_clk_ovr_on`（时钟覆盖）出发的路径都不做时序分析。

**时钟周期的数学含义**：周期 0.9 ns 对应频率

\[
f = \frac{1}{T} = \frac{1}{0.9\,\text{ns}} \approx 1.11\,\text{GHz}
\]

这正是 u1-l1 讲的"峰值算力 = MAC 数 × 核心频率"里的那个核心频率。2048 个 INT8 MAC 在 1.11 GHz 下的峰值算力约 \(2048 \times 1.11 \times 10^{9} \approx 2.27 \times 10^{12}\) OP/s，即约 2.27 TOPS。SDC 里的 `-period 0.9` 一行，直接定义了这块硬件的算力天花板。

**关于 `nvdla_core_rstn` 的处理**：它不是端口，而是分区内部由复位同步器（u6-l1 讲的 `NV_NVDLA_core_reset`/`sync3d`）产生的同步复位网。可在 [vmod/nvdla/top/NV_NVDLA_partition_c.v:1261](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/vmod/nvdla/top/NV_NVDLA_partition_c.v#L1261) 看到 `wire nvdla_core_rstn;`，并在该文件 1553 行被复位同步器的 `synced_rstn` 驱动。SDC 用 `get_nets`（取内部网）而非 `get_ports`（取端口）来引用它，并加 `-no_propagate` 让"理想"属性不向下传播——这正是仓库最近一次提交（`eb2564c` "synthesis: update constraints to be more careful about identifying reset nets"）的目的：把外部复位端口与内部同步复位网分别、更精确地标识为 ideal，避免误伤下游时序逻辑。

现在对比五份 SDC 的差异。下表把每份 SDC 的"独有内容"列出（其余部分与 partition_c 完全相同）：

| 分区 SDC | 时钟 | false_path 特殊点 | 硬件含义 |
| --- | --- | --- | --- |
| `partition_a.sdc` | 仅 core @0.9ns | 与 c 相同（含 `pwrbus_ram_pd*`） | CACC：含 assembly/delivery buffer SRAM，故有 RAM 电源引脚 |
| `partition_c.sdc` | 仅 core @0.9ns | 含 `pwrbus_ram_pd*` | CDMA+CBUF+CSC：CBUF 是 512KB 大 SRAM，故有 RAM 电源引脚 |
| `partition_m.sdc` | 仅 core @0.9ns | **无 `pwrbus_ram_pd*`** | CMAC：纯乘加阵列，无 SRAM，故无 RAM 电源引脚 |
| `partition_o.sdc` | core @0.9ns **+ falcon @1.516ns** | 含 `pwrbus_ram_pd*`；**外加 core↔falcon 双向 false_path** | 中央枢纽：跨 core/falcon 双时钟域（csb_master、glb），故双时钟 + 跨域切割 |
| `partition_p.sdc` | 仅 core @0.9ns | 含 `pwrbus_ram_pd*` | SDP：含 LUT 表 SRAM，故有 RAM 电源引脚 |

关键差异的源码佐证。先看 `partition_o` 独有的 falcon 时钟与跨域 false_path：

[syn/cons/NV_NVDLA_partition_o.sdc:20-24](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_o.sdc#L20-L24) —— partition_o 额外创建 `nvdla_falcon_clk`，周期 1.516 ns（约 660 MHz），并设定其时钟翻转时间。

[syn/cons/NV_NVDLA_partition_o.sdc:31-32](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_o.sdc#L31-L32) —— 两条跨时钟域 false_path：core→falcon 与 falcon→core 都不分析。这与 u2-l2/u6-l1 的结论吻合——partition_o 内的 csb_master、glb 横跨 core 与 falcon 两域，跨域信号靠异步 FIFO 或 sync3d 同步（见 u6-l1），不应被单周期时序分析约束。

falcon 域的周期 1.516 ns 对应频率约 660 MHz，比 core 域（1.11 GHz）慢——配置总线（CSB）本就是低带宽路径，用较慢的 falcon 时钟既能满足 CPU 配置访问需求，又能省功耗。

再看 `partition_m` 缺失 `pwrbus_ram_pd*` 的事实：

[syn/cons/NV_NVDLA_partition_m.sdc:20-25](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/cons/NV_NVDLA_partition_m.sdc#L20-L25) —— partition_m 的 false_path 只有六条，**没有** `pwrbus_ram_pd*`。因为 CMAC 是纯组合/时序逻辑构成的乘加阵列（u3-l5），不含 SRAM 宏，自然没有 RAM 电源引脚可供约束。这是一个"SDC 精确反映分区硬件内容"的典型例证。

> 说明：上表只列了有把握的差异。`partition_o.sdc` 中还省略了 `nvdla_clk_ovr_on` 这条 false_path（其余四份都有），确切原因需对照该分区 RTL 端口表确认，**待确认**。

#### 4.2.4 代码实践

**实践目标**：用文本对比工具精确找出五份 SDC 的差异，并把每条差异与对应分区的硬件内容对应起来。

**操作步骤**：

1. 在仓库根目录执行逐字段对比（示例代码，使用 `diff`）：
   ```bash
   cd syn/cons
   diff NV_NVDLA_partition_c.sdc NV_NVDLA_partition_a.sdc   # 预期：几乎无差异
   diff NV_NVDLA_partition_c.sdc NV_NVDLA_partition_m.sdc   # 预期：m 少了 pwrbus_ram_pd*
   diff NV_NVDLA_partition_c.sdc NV_NVDLA_partition_o.sdc   # 预期：o 多了 falcon 时钟与跨域 false_path
   diff NV_NVDLA_partition_c.sdc NV_NVDLA_partition_p.sdc   # 预期：几乎无差异
   ```
2. 对每个差异，回到对应分区 RTL（`vmod/nvdla/top/NV_NVDLA_partition_*.v`）用 `grep` 核实端口是否存在。例如确认 partition_m 没有 `pwrbus_ram_pd` 端口：
   ```bash
   grep -n 'pwrbus_ram_pd' ../../vmod/nvdla/top/NV_NVDLA_partition_m.v   # 预期无输出
   grep -n 'pwrbus_ram_pd' ../../vmod/nvdla/top/NV_NVDLA_partition_c.v   # 预期有输出
   ```

**需要观察的现象**：

- `c` 与 `a`、`p` 的 diff 是否只剩行序差异（实质相同）？
- `c` 与 `m` 的 diff 是否正好是 `pwrbus_ram_pd*` 那一行？
- `c` 与 `o` 的 diff 是否包含 falcon 时钟创建与两条跨域 false_path？
- RTL 端口核实是否与 SDC 一致（有 RAM 的分区才有 `pwrbus_ram_pd`）？

**预期结果**：SDC 差异与分区硬件内容一一对应——含 SRAM 的分区（a/c/o/p）有 `pwrbus_ram_pd*` 约束，纯逻辑分区（m）没有；只有跨双时钟域的 partition_o 有 falcon 时钟与跨域 false_path。这些 diff 与 grep 都可在纯文本环境验证，无需任何 EDA 工具。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `nvdla_core_rstn` 用 `get_nets` 而 `direct_reset_` 用 `get_ports`？

**参考答案**：`direct_reset_` 是分区对外的输入端口，故用 `get_ports`；`nvdla_core_rstn` 是分区内部由复位同步器产生的网线（在 partition_c.v 第 1261 行声明为 `wire`），不是端口，故用 `get_nets`。加 `-no_propagate` 是为了让 ideal 属性只停在该网本身、不向下游传播，这正是最近提交"more careful about identifying reset nets"的改动要点。

**练习 2**：如果把 partition_c 的时钟周期从 0.9 改成 1.8（降频一半），会对综合结果产生什么影响？

**参考答案**：周期变大→时序更松→DC 有更多余量做面积优化，网表面积通常变小、功耗降低，但峰值算力也减半（约 1.14 TOPS）。这体现了"频率↔面积↔算力"的三角权衡。注意改 SDC 周期只影响综合目标，不改变 RTL 本身的算力规格（2048 MAC）。

**练习 3**：partition_o 为什么需要 core→falcon 与 falcon→core 两条 false_path 都设？

**参考答案**：跨时钟域路径两个方向都要切。core→falcon 方向（如引擎 done 中断上报到 glb 的 falcon 侧）和 falcon→core 方向（如 CSB 配置下发到 core 侧引擎）都不可能在单周期内完成，真正同步靠异步 FIFO（csb2falcon/falcon2csb，见 u2-l2）与 sync3d（见 u6-l1）。设 false_path 是告诉 STA"这些路径由跨域同步器保证，不要按单周期时序报违例"。

---

### 4.3 dc_run.tcl：DC 综合主流程

#### 4.3.1 概念说明

`dc_run.tcl` 是综合的"剧本"——一份约 520 行的 Tcl 脚本，被 DC shell 逐行执行。它定义了从"读工艺库"到"写网表"的完整阶段顺序。理解它的关键不是逐行背诵，而是抓住**七个阶段**的骨架，以及一个贯穿全程的变量优先级规则。

#### 4.3.2 核心流程

`dc_run.tcl` 的七阶段骨架：

```
(1) 环境与变量     profileSystem + setVar（env > tcl > default 三级优先）
(2) 库与搜索路径   link_path / target_library / search_path / SVF
(3) 读 RTL         analyze(-f sverilog, 用 .files.vc) -> elaborate -> current_design -> link
(4) 物理库(可选)   仅 topo/de 模式：create_mw_lib + set_tlu_plus_files + 布线方向
(5) 施约束         read_sdc(<MODULE>.sdc) + source(<MODULE>.tcl 可选) + CG 过约束
(6) 编译           compile_ultra / compile_exploration（按模式）+ 增量编译 + 面积恢复
(7) 写产物         DDC + SDC + 网表 .gv + DEF(仅 dcg) + 各类报告
```

贯穿全程的变量优先级规则由 `setVar` proc 实现：**环境变量 > 已有 Tcl 变量 > 默认值**。这让 `syn_launch.sh` 通过 `export` 传进来的值（如 `MODULE`、`SYN_MODE`、`CONS_DIR`）优先级最高，覆盖脚本内的默认值。

#### 4.3.3 源码精读

先看 `setVar` 与报告生成这两个工具 proc：

[syn/scripts/dc_run.tcl:22-36](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L22-L36) —— `setVar` 实现"env > tcl > default"三级优先：先看 `$::env($var)`，再看 Tcl 变量本身，最后用传入默认值。这是 `syn_launch.sh` 能用 `export MODULE=...` 控制 DC 行为的机制基础。

[syn/scripts/dc_run.tcl:51-84](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L51-L84) —— `writeReports` 汇总所有质量报告：`report_qor`（质量总览）、`report_timing`（关键路径）、`report_constraint`（约束违例）、`report_congestion`（拥塞，仅 topo）、`report_resources`（资源）、`report_power`（功耗）、`report_design` 等，全部追加写进 `<REPORT_DIR>/<MODULE>.<prefix>.report`。

阶段①②：变量与库设置。

[syn/scripts/dc_run.tcl:99-102](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L99-L102) —— 用 `setVar` 取 `SYN_MODE`（默认 wlm）与 `MODULE`（当前分区名，由 syn_launch.sh export 注入）。

[syn/scripts/dc_run.tcl:150-157](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L150-L157) —— 设置 `link_path`/`target_library`（来自 config.sh 的 LINK_LIB/TARGET_LIB），并 `set_svf` 启动 Setup Verification Framework 录制，供后续 Formality 形式验证用。

[syn/scripts/dc_run.tcl:168-171](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L168-L171) —— 若存在 `dc_app_vars.tcl` 就 source 它（一组 DC 应用变量调优，如 `hdlin_preserve_sequential`、`enable_recovery_removal_arcs` 等，用于提升 QoR）。

阶段③：读 RTL——`analyze` + `elaborate`。

[syn/scripts/dc_run.tcl:191-201](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L191-L201) —— `analyze -format sverilog` 用 `RTL_DEPS`（即 `.files.vc`）读入 RTL 并做语法分析；成功后 `elaborate ${MODULE}` 把顶层模块展开成门级结构，`current_design` 切到该模块。`analyze` 失败则直接 `exit 1`。

阶段④：物理库（仅 topographical / exploration 模式）。

[syn/scripts/dc_run.tcl:215-245](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L215-L245) —— 仅 `shell_is_in_topographical_mode` 为真时执行：创建 Milkyway 设计库 `create_mw_lib`、载入 TLU+ 寄生参数 `set_tlu_plus_files`、设定布线层方向 `set_preferred_routing_direction`。wlm 模式跳过整段。

[syn/scripts/dc_run.tcl:250-258](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L250-L258) —— `link` 解析所有例化引用到库单元；失败则退出。若 `QA_MODE==link_only` 则链接成功后直接优雅退出（用于快速验证 RTL 可综合而不真跑编译）。

阶段⑤：施约束——本讲核心衔接点。

[syn/scripts/dc_run.tcl:333-341](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L333-L341) —— 用 `read_sdc` 读入 `<CONS_DIR>/<MODULE>.sdc`（即上一模块讲的分区 SDC）；若存在 `<MODULE>.tcl` 则额外 source 它（放非 SDC 的额外约束）。这是 SDC 与 DC 主流程的汇合点。

阶段⑤续：时钟门控使能路径过约束。

[syn/scripts/dc_run.tcl:344-408](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L344-L408) —— 当 `TIGHTEN_CGE==1` 且存在 `CGLUT_FILE` 时，读入查找表 `cg_latency_lut.tcl` 中的 `timingDeltaTableForCgEnable`，按门控扇出大小给时钟门控使能路径施加负延迟（过约束），模拟 CTS 后插入延迟，使综合出的 CG 路径更有余量。

该查找表的内容见 [syn/templates/cg_latency_lut.tcl:11-19](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/cg_latency_lut.tcl#L11-L19)：扇出 0/4/16/64/256/1024/4096/16384/65536 对应延迟 106/108/112/127/162/216/280/347/415（单位 ps）——扇出越大、门控使能路径需预留的延迟越多。

阶段⑥：编译——按模式选不同命令。

[syn/scripts/dc_run.tcl:450-462](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L450-L462) —— 选择 compile 命令的核心逻辑：
- dcg（topo + graphical）：`compile_ultra -no_seq_output_inversion -gate_clock -spg -scan`
- dct（topo）：`compile_ultra -no_seq_output_inversion -gate_clock -scan`
- de（exploration）：`compile_exploration -no_seq_output_inversion -gate_clock`
- wlm（非物理）：`compile_ultra -no_seq_output_inversion -no_autoungroup -scan`

各选项含义：`-gate_clock` 启用自动时钟门控插入（对应 u6-l1 讲的 slcg 等省电机制的综合侧实现）；`-spg` 启用 SPG（Synopsys Physical Guidance）物理导向综合；`-scan` 串接扫描链（DFT）；`-no_autoungroup` 保留层次（wlm 模式下保层次以便调试）。

[syn/scripts/dc_run.tcl:468-489](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L468-L489) —— 按 `INCREMENTAL_RECOMPILE_COUNT` 做增量编译循环（`-incremental`），随后若 `AREA_RECOVERY==1` 则 `ungroup -all -flatten` + `optimize_netlist -area` 做面积恢复。

阶段⑦：写产物。

[syn/scripts/dc_run.tcl:491-498](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L491-L498) —— 写出全部产物：`<MODULE>.ddc`（DC 数据库）、`<MODULE>.sdc`（综合后回写的约束）、`<MODULE>.gv`（门级网表，最终交付物）；仅 dcg 模式额外 `write_def` 写出带版图信息的 DEF。

[syn/scripts/dc_run.tcl:500-513](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L500-L513) —— `writeReports "final"` 生成最终报告，`check_design`/`check_timing` 做设计/时序自检，关闭 SVF 录制，最后打印产物清单。

#### 4.3.4 代码实践

**实践目标**：跟踪 `partition_c` 综合时 SDC 被读入的完整调用链，理解"约束施加"在七阶段中的位置与时序。

**操作步骤**：

1. 打开 `syn/scripts/dc_run.tcl`，在 [L333](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L333) 的 `read_sdc` 行上方 mental 标注："此时 MODULE=NV_NVDLA_partition_c，CONS_DIR=<build>/cons，故读入的是 `<build>/cons/NV_NVDLA_partition_c.sdc`"。
2. 确认这个 SDC 是 `dataprep()` 从 `syn/cons/` 拷过去的：回看 [syn/scripts/syn_launch.sh:135-138](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L135-L138)，`CONS` 目录（默认 config，模板里改成 `cons`）的内容被整体拷进 sandbox 的 `cons/`。
3. 顺着时序梳理：`read_sdc`（L335）发生在 `link`（L250）之后、`compile_ultra`（L462）之前——即"先建好网、再施约束、最后编译优化"。这正是 DC 的标准顺序：约束必须在 compile 之前施加，否则工具无目标可优化。

**需要观察的现象**：

- `read_sdc` 之前 DC 是否已经 `elaborate` + `link` 成功？（约束施加在已链接的设计上才有意义）
- `read_sdc` 与 `compile` 之间是否还夹着 CG 过约束（TIGHTEN_CGE）与 dont_ungroup 设置？
- 若 `CONS_DIR` 下没有对应 `.sdc`，脚本会怎样？（看 L333 的 `file exists` 判断——会静默跳过，不报错，但时序将无约束）

**预期结果**：SDC 经 `syn_launch.sh` 的 `dataprep` 从 `syn/cons/` 拷入 sandbox，再由 `dc_run.tcl` 的 `read_sdc` 在 link 后、compile 前施加。这条链路完整可从源码跟踪，无需运行 DC。若实际运行 DC，`<build>/report/NV_NVDLA_partition_c.final.report` 里的 `report_qor` 会显示 WNS（最差负裕量）是否为正——**待本地验证**（需 DC 与工艺库）。

#### 4.3.5 小练习与答案

**练习 1**：`setVar` 的三级优先级为什么把环境变量放最高？

**参考答案**：因为 `syn_launch.sh` 在 bash 侧用 `export MODULE=...`、`export SYN_MODE=...`、`export CONS_DIR=...` 等把"这次综合的具体配置"注入环境。`setVar` 优先读环境变量，意味着同一份 `dc_run.tcl` 可被不同 sandbox、不同分区、不同模式复用而互不干扰——脚本本身是无状态的，状态全由环境变量携带。

**练习 2**：wlm 模式与 dcg 模式的 compile 命令差了 `-spg`，这个选项的作用是什么？为什么 wlm 不加？

**参考答案**：`-spg`（Synopsys Physical Guidance）让 DC 在综合时利用物理版图信息（布局、布线资源）做导向优化，能得到更贴近最终布线结果的时序/拥塞估计。它只在 topographical 模式（有 Milkyway 库与 TLU+ 寄生参数）下有意义。wlm 是非物理模式（用线负载模型估算连线延迟），没有物理信息可用，故不加 `-spg`。

**练习 3**：`QA_MODE=link_only` 有什么用？

**参考答案**：它让 `dc_run.tcl` 在 `link` 成功后立即退出（[L255-258](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L255-L258)），跳过耗时的 compile。用于快速验证"RTL 能否被 DC 正确读入并链接到库单元"，而不真正做综合优化——是排查 RTL 可综合性问题的轻量手段。

---

### 4.4 综合模式与工艺库配置

#### 4.4.1 概念说明

`dc_run.tcl` 里到处是 `shell_is_in_topographical_mode`、`shell_is_in_exploration_mode` 的分支判断——这些"模式"由 `syn_launch.sh -mode` 选择的 shell 决定，进而决定走物理综合还是非物理综合、需要哪些工艺库变量。本模块把四种模式与它们所需的工艺库配置一次性理清。

工艺库是综合的"原料"。DC 需要知道：

- **TARGET_LIB / LINK_LIB**：标准单元的逻辑/时序库（`.db`），告诉 DC 有哪些门可用、各自的延迟与面积。
- **MW_LIB / TF_FILE / TLUPLUS_***：物理库（Milkyway 参考库、技术文件、寄生参数），仅物理综合需要，提供版图与连线寄生信息。
- **WIRELOAD_MODEL_NAME**：线负载模型，仅 wlm 模式需要，用统计模型估算连线电容。

#### 4.4.2 核心流程

四种模式对照：

| 模式 | 调用 shell | 物理性 | 必需工艺库变量 | compile 命令 | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| `wlm` | `dc_shell` | 非物理（线负载） | TARGET_LIB, LINK_LIB, WIRELOAD_MODEL_NAME, DC_PATH | `compile_ultra ... -no_autoungroup -scan` | 早期评估、无物理库时快速出网表 |
| `dct` | `dc_shell-t -topographical_mode` | 物理（拓扑） | TARGET_LIB, LINK_LIB, MW_LIB, TF_FILE, TLUPLUS_*, 路由层, DC_PATH | `compile_ultra ... -gate_clock -scan` | 有物理库、需较准时序但不需图形化布线 |
| `dcg` | `dc_shell-t -topographical_mode` | 物理（图形化） | 同 dct | `compile_ultra ... -spg -scan`；额外 `write_def` | 需图形化物理导向、产出 DEF 供 ICC 衔接 |
| `de` | `de_shell` | 探索 | 同 dct | `compile_exploration ... -gate_clock` | 多配置并行探索、找最优 QoR |

> 注：上表中"dct 与 dcg 用同一 shell（`dc_shell-t -topographical_mode`），区别在 compile 命令是否带 `-spg`"——见 [syn/scripts/syn_launch.sh:212-229](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L212-L229) 的分支与 [syn/scripts/dc_run.tcl:451-454](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L451-L454) 的命令选择。

工艺库变量在两处定义：`default_config.sh` 给全空默认值（兜底），`templates/config.sh` 给一份 TSMC 16FF 的真实示例（被集成者拷去改成自己的库路径）。

#### 4.4.3 源码精读

先看 `default_config.sh` 的默认值——全是空串，强迫用户必须在 config.sh 里填：

[syn/scripts/default_config.sh:41-53](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/default_config.sh#L41-L53) —— 所有工艺库变量默认空：`TARGET_LIB`、`LINK_LIB`、`MW_LIB`、`TF_FILE`、`TLUPLUS_FILE`、`TLUPLUS_MAPPING_FILE`、路由层、线负载模型等。`DC_PATH` 也默认空（syn_launch.sh 会校验非空）。

[syn/scripts/default_config.sh:60-69](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/default_config.sh#L60-L69) —— 综合调参默认值：`DC_NUM_CORES=1`、`TIGHTEN_CGE=0`（默认关 CG 过约束）、`AREA_RECOVERY=1`、`INCREMENTAL_RECOMPILE_COUNT=1`。

再看 `templates/config.sh` 这份 TSMC 16FF 示例——这是本模块的重点：

[syn/templates/config.sh:15](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L15) —— `TOP_NAMES` 列出五个分区，与 default 一致。

[syn/templates/config.sh:20-34](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L20-L34) —— RTL 搜索路径：`vmod/nvdla/*`（所有引擎）、`vmod/rams/synth`（综合用 RAM wrapper，见 u6-l3）、`vmod/vlibs`（库原语，见 u6-l2）；头文件搜 `vmod/include`；并把 `NV_NVDLA_XXIF_libs.v` 单独列入 `EXTRA_RTL`（因该文件由 arbgen2 生成、模块名与文件名不匹配，需 `-v` 显式指定）。

[syn/templates/config.sh:51](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L51) —— `DC_PATH` 指向 Synopsys DC 2016.12 安装目录的 `bin`。

[syn/templates/config.sh:60-72](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L60-L72) —— `TARGET_LIB` 是单个标准单元库（po4svt，SSG 工艺角、0℃、0.72V）；`LINK_LIB` 是一组库：多种阈值电压（po4svt/hvt/svt/sup）的标准单元库 + RAM 库 + DesignWare 基础库（`dw_foundation.sldb`、`gtech.db`、`standard.sldb`）。`TARGET_LIB` 用于映射（综合时把 RTL 映射到这里的单元），`LINK_LIB` 用于链接（解析已存在例化的引用）。

[syn/templates/config.sh:74-90](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L74-L90) —— 物理库：`MW_LIB`（Milkyway 参考库，多种阈值电压 + RAM）、`TF_FILE`（技术文件，14 层金属的 ICC 工艺）、`TLUPLUS_FILE`/`TLUPLUS_MAPPING_FILE`（寄生参数与映射）、`MIN/MAX_ROUTING_LAYER`（M2A~M12E）、`HORIZONTAL_LAYERS`/`VERTICAL_LAYERS`（水平/垂直布线层分配）。这些**仅 dct/dcg/de 物理模式**需要。

[syn/templates/config.sh:91-92](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L91-L92) —— `WIRELOAD_MODEL_NAME="tsmc16ff_from_smq"`：线负载模型名，**仅 wlm 模式**需要（见 dc_run.tcl L267-279 的 wireload 设置分支，条件是 `!topographical && !exploration`）。

[syn/templates/config.sh:99-113](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/templates/config.sh#L99-L113) —— 调参：`DC_NUM_CORES=8`（多核并行）、`TIGHTEN_CGE=1`（开 CG 过约束）、`AREA_RECOVERY=1`、`INCREMENTAL_RECOMPILE_COUNT=2`（两轮增量编译）；`COMMAND_PREFIX` 是 LSF 作业队列提交前缀（`qsub`，企业集群用）；`CGLUT_FILE` 指向 `cg_latency_lut.tcl`。

最后看 `dc_run.tcl` 如何按模式切换 wireload 与物理库设置——这是"模式决定变量"的运行时体现：

[syn/scripts/dc_run.tcl:267-279](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L267-L279) —— 仅当 `!topographical && !exploration`（即 wlm 模式）才设线负载模型：可选读 `WIRELOAD_MODEL_FILE` 更新库，再 `set_wire_load_model -name $WIRELOAD_MODEL_NAME` + `set_wire_load_mode top`。物理模式跳过，改用 TLU+ 实测寄生。

[syn/scripts/dc_run.tcl:302-322](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L302-L322) —— 仅 topo 模式：读 DEF 版图、source macroplacement.tcl（若存在）、设 `set_ignored_layers` 限定最小/最大布线层。wlm 模式跳过。

#### 4.4.4 代码实践

**实践目标**：为 `partition_c` 选 wlm 模式，从 `templates/config.sh` 出发裁出一份"刚好够 wlm 用"的最小 config，并列出关键变量。

**操作步骤**：

1. 拷模板：`cp syn/templates/config.sh /tmp/wlm_c_config.sh`。
2. 因为只跑 wlm（非物理），可把物理库变量留空或注释掉：`MW_LIB`、`TF_FILE`、`TLUPLUS_FILE`、`TLUPLUS_MAPPING_FILE`、`MIN/MAX_ROUTING_LAYER`、`HORIZONTAL/VERTICAL_LAYERS` 都不是 wlm 必需（见 README 与 dc_run.tcl 的分支条件）。
3. 确认 wlm 必需变量齐全（参照 [syn/README.md:19-26](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/README.md#L19-L26)）：
   - `WIRELOAD_MODEL_NAME`（已设 `tsmc16ff_from_smq`）
   - `TARGET_LIB`（已设 po4svt 库）
   - `LINK_LIB`（已设多库）
   - `DC_PATH`（已设 DC 安装路径）
4. 若只综合 `partition_c` 一个分区，可把 `TOP_NAMES` 缩成单个：
   ```bash
   export TOP_NAMES="NV_NVDLA_partition_c"
   ```
   或运行时用 `-modules` 覆盖：
   ```bash
   bash syn/scripts/syn_launch.sh -mode wlm -config /tmp/wlm_c_config.sh -modules NV_NVDLA_partition_c
   ```

**需要观察的现象**：

- `syn_launch.sh` 是否因 `TOP_NAMES` 单值而只跑一次 DC？（看 [L83-88](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L83-L88) 的 modules 处理）
- wlm 分支是否调 `dc_shell`（非 `dc_shell-t`）？（看 [L230-247](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/syn_launch.sh#L230-L247)）
- 日志 `<build>/log/NV_NVDLA_partition_c_wlm.log` 里是否出现 `Setting wireload model to tsmc16ff_from_smq`？（对应 dc_run.tcl L277）

**预期结果**：wlm 模式下 partition_c 综合所需的最小变量集为 `DC_PATH`、`TARGET_LIB`、`LINK_LIB`、`WIRELOAD_MODEL_NAME` 四项（加上设计相关的 `TOP_NAMES`、`RTL_SEARCH_PATH`、`RTL_INCLUDE_SEARCH_PATH`、`EXTRA_RTL`）。物理库变量可全留空。真实运行需 Synopsys DC 许可与 TSMC 16FF 工艺库，**待本地验证**。

**SDC 如何约束卷积核心时序**（衔接本模块与 4.2）：partition_c 综合时，`dc_run.tcl` 在 [L335](https://github.com/nvdla/hw/blob/8e06b1b9d85aab65b40d43d08eec5ea4681ff715/syn/scripts/dc_run.tcl#L335) 读入 `NV_NVDLA_partition_c.sdc`，其中 `create_clock nvdla_core_clk -period 0.9` 把卷积核心（CDMA/CBUF/CSC）的目标频率定在约 1.11 GHz；`set_max_area 0` 让 DC 在满足该频率下尽量压面积；七条 `set_false_path` 把复位/测试/RAM 电源/时钟门控覆盖等异步路径剔除，使 DC 聚焦优化卷积数据通路（CBUF 读写、CSC 节拍分发）的真正时序关键路径。最终 `report/NV_NVDLA_partition_c.final.report` 的 `report_qor` 会给出 WNS/TNS——若 WNS≥0 则卷积核心在 1.11 GHz 下时序收敛。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `TARGET_LIB` 只列一个库，而 `LINK_LIB` 列了一长串？

**参考答案**：`TARGET_LIB` 是综合映射的目标——DC 把 RTL 映射成这里的单元，通常只指定一个主标准单元库（本例 po4svt）以控制单元选择范围。`LINK_LIB` 是链接库——用于解析设计中已例化的引用（包括其他阈值电压单元、RAM 宏、DesignWare 基础单元），故需把所有可能被引用的库都列上。两者职责不同：TARGET 决定"用什么新单元"，LINK 决定"认得哪些已有单元"。

**练习 2**：若集成者只有标准单元 `.db` 库、没有任何物理库（无 Milkyway/TLU+），该用哪种模式？

**参考答案**：只能用 `wlm`。物理模式（dct/dcg/de）都需要 MW_LIB、TF_FILE、TLUPLUS_* 等物理库变量（见 README L37-46）。wlm 用线负载模型（`WIRELOAD_MODEL_NAME`）统计估算连线延迟，不依赖物理版图，是"只有逻辑库"时的唯一选择——代价是时序估计不如物理模式准。

**练习 3**：`TIGHTEN_CGE` 在 default 与 template 里取值不同（0 vs 1），这个差异意味着什么？

**参考答案**：`default_config.sh` 里 `TIGHTEN_CGE=0`（默认关闭 CG 使能路径过约束），`templates/config.sh` 里 `TIGHTEN_CGE=1`（开启，并配 `CGLUT_FILE=cg_latency_lut.tcl`）。开启后，dc_run.tcl（L344-408）会按门控扇出给 CG 使能路径施加负延迟过约束，模拟 CTS 后的真实插入延迟，使综合出的时钟门控逻辑在后续布局布线后仍有余量。这是 NVDLA 参考流程为追求时序质量而开启的一项高级调优，集成者可按需关闭以缩短综合时间。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个"为 partition_c 规划一次 wlm 综合"的完整任务。

**任务**：假设你是 SoC 集成者，需要在没有物理库的情况下快速评估卷积核心 partition_c 的综合可行性。请完成以下步骤并交付一份简短说明。

1. **选模式**：从四种模式中选出适合"无物理库、快速评估"的，并说明理由。
2. **裁配置**：基于 `syn/templates/config.sh`，列出 wlm 模式下 partition_c 综合所需的**全部必需变量**及其取值来源（哪些来自模板、哪些需你自己填）。
3. **查约束**：打开 `syn/cons/NV_NVDLA_partition_c.sdc`，回答：卷积核心的目标频率是多少？哪几类路径被设为 false_path？为什么 `nvdla_core_rstn` 用 `get_nets`？
4. **跟踪链**：画出从 `syn_launch.sh` 到 `dc_run.tcl read_sdc` 再到 `compile_ultra` 的调用顺序，标注 SDC 在哪个阶段被施加。
5. **看产物**：说明综合成功后会生成哪些产物文件、分别落在 sandbox 的哪个子目录。

**参考要点**：

1. 选 `wlm`：无物理库（缺 MW_LIB/TF_FILE/TLUPLUS），只能用线负载模型；wlm 调 `dc_shell`、compile 带 `-no_autoungroup`，适合快速评估。
2. 必需变量：`DC_PATH`（填自己的 DC 安装路径）、`TARGET_LIB`+`LINK_LIB`（填自己的 `.db` 库）、`WIRELOAD_MODEL_NAME`（填工艺库自带的线负载模型）、`TOP_NAMES="NV_NVDLA_partition_c"`、`RTL_SEARCH_PATH`（指向 `vmod/nvdla/*`、`vmod/rams/synth`、`vmod/vlibs`）、`RTL_INCLUDE_SEARCH_PATH`（`vmod/include`）、`EXTRA_RTL`（`XXIF_libs.v`）。物理库变量可留空。
3. 目标频率 ≈1.11 GHz（周期 0.9ns）；false_path 覆盖复位（direct_reset_、dla_reset_rstn）、test_mode、RAM 电源（pwrbus_ram_pd*）、时钟门控覆盖（tmc2slcg_disable_clock_gating、global_clk_ovr_on、nvdla_clk_ovr_on）；`nvdla_core_rstn` 是内部同步复位网（非端口），故用 `get_nets` 并带 `-no_propagate`。
4. 顺序：`syn_launch.sh`（解析参数+dataprep 建 sandbox+生成 .files.vc）→ `dc_shell -f dc_run.tcl`（analyze→elaborate→link→**read_sdc**→compile_ultra→write）。SDC 在 link 之后、compile 之前施加。
5. 产物：`<build>/net/NV_NVDLA_partition_c.gv`（网表）、`<build>/db/NV_NVDLA_partition_c.ddc`（数据库）、`<build>/net/NV_NVDLA_partition_c.sdc`（回写约束）、`<build>/report/NV_NVDLA_partition_c.final.report`（QoR/时序报告）、`<build>/log/NV_NVDLA_partition_c_wlm.log`（日志）。

实际能否跑通取决于是否有 Synopsys DC 许可与标准单元库，**待本地验证**。

## 6. 本讲小结

- NVDLA 综合是"参考流程"：仓库提供与工艺无关的脚本骨架（`syn_launch.sh` + `dc_run.tcl` + SDC），集成者用 `config.sh` 填入工艺库路径后运行。
- 综合按 `partition_a/c/m/o/p` 五个分区**分别**进行，每个分区一份 SDC；五份 SDC 共享"core 时钟 0.9ns（≈1.11GHz）+ 理想复位 + false_path"骨架，差异精确反映各分区硬件：partition_o 独有 falcon 时钟与跨域 false_path，partition_m 因无 SRAM 而缺 `pwrbus_ram_pd*`。
- `dc_run.tcl` 是七阶段主流程（环境变量→库→读 RTL→物理库→施约束→编译→写产物），用 `setVar` 实现"env > tcl > default"三级变量优先级；SDC 在 `link` 后、`compile` 前由 `read_sdc` 施加。
- 四种模式 `wlm/dct/dcg/de` 分别调 `dc_shell`/`dc_shell-t`/`de_shell`，决定是否物理综合、`compile_ultra` 是否带 `-spg`、是否写 DEF，并决定需要哪些工艺库变量（wlm 需线负载模型，物理模式需 MW_LIB/TF_FILE/TLUPLUS）。
- `templates/config.sh` 是 TSMC 16FF 真实示例：`TARGET_LIB` 单库映射、`LINK_LIB` 多库链接、`RTL_SEARCH_PATH` 收拢 `vmod/nvdla/*`+`rams/synth`+`vlibs`，并注入 `NV_SYNTHESIS`/`RAM_INTERFACE` 宏切换 RAM 综合身份。
- SDC 的 `-period 0.9` 一行直接定义卷积核心频率，进而决定峰值算力（2048 MAC × 1.11GHz ≈ 2.27 TOPS），把"时序约束"与"硬件算力规格"在源码层面联系了起来。

## 7. 下一步学习建议

本讲把综合流程讲完，建议接着读本单元最后一篇 **u8-l4 端到端：编程一个网络层与集成指南**，把配置链（单元 2）、卷积主流水线（单元 3）、存储接口（单元 4）、后处理（单元 5）与综合产物（本讲）串成一个完整的 SoC 集成视角。

若想继续深挖综合相关源码，建议：

- 对照 `syn/scripts/dc_app_vars.tcl` 逐条查阅 DC 应用变量的含义（如 `hdlin_preserve_sequential`、`enable_recovery_removal_arcs`、`compile_ultra_ungroup_dw`），理解 QoR 调优旋钮。
- 读 `syn/scripts/dc_interactive.tcl`，理解如何用 `read_ddc` 恢复数据库做交互调试。
- 回看 u6-l3（RAM 仿真/综合双模型）与本讲的 `+define+RAM_INTERFACE`，确认综合时 CBUF 等大缓冲被替换成 `rams/synth` wrapper 而非行为模型。
- 回看 u6-l1（时钟域/复位）与本讲的 SDC，确认 `nvdla_core_rstn` 的 ideal 标注与 `sync3d` 复位同步器在源码层面对应。
