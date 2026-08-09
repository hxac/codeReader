# 工程构建 Makefile 内部：project-xilinx.mk

## 1. 本讲目标

上一篇（u3-l1）我们看清了构建的「骨架」：顶层 `Makefile` 自动发现工程、生成 `proj.board` 虚拟目标、递归 `make -C` 进入叶子工程目录。本讲则走进那个叶子目录，拆开每个 Xilinx 工程都会 `include` 的公共脚本 [projects/scripts/project-xilinx.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk)，看懂它如何把一行 `make` 变成一次完整的 Vivado 构建。

学完本讲你应该能够：

- 看懂工程 `Makefile` 中的 `M_DEPS`（文件依赖）与 `LIB_DEPS`（IP 依赖）是如何被汇总成最终产物 `system_top.xsa` 的依赖；
- 解释 `LIB_DEPS` 是怎样被翻译成一个个 `component.xml` 打包目标，从而触发对应 library IP 的重新打包；
- 说明 `flock` 锁文件机制为何能让多个工程并行构建时安全地共用同一份 IP，以及 `REQUIRED_VIVADO_VERSION` 是如何从命令行一路透传到 Tcl 的；
- 理解 `CFG` 参数化如何生成独立的「参数化工程目录」，以及 `MODE=incr` 增量编译如何用上一次的 `reference.dcp` 检查点。

## 2. 前置知识

本讲默认你已经掌握以下内容（若不熟悉，请先读对应讲义）：

- **GNU Make 的依赖驱动模型与递归 make**（u3-l1）：目标、先决条件、recipe、自动变量 `$@`/`$(@D)`/`$(@F)`、`.PHONY` 虚拟目标、`make -C` 子目录递归。
- **quiet.mk 的三个公共宏**（u3-l1）：`build`（把冗长命令输出收进日志，终端只留一行 OK/FAILED）、`clean`、`skip_if_missing`（依据 `missing_external.log` 决定是否优雅跳过）。
- **工程 Makefile 的两个字段**（u1-l4）：`M_DEPS` 收集本工程需直接引用的文件，`LIB_DEPS` 收集依赖的 library IP 名字，最后 `include project-xilinx.mk`。
- **Vivado 的 IP 打包概念**：一个 IP 在 Xilinx 工具里由一份 `component.xml` 描述（含源文件、参数、接口等元数据），把 `component.xml` 生成出来的过程俗称「打包（pack IP）」。

几个本讲会反复出现的术语，先用一句话解释：

| 术语 | 含义 |
| --- | --- |
| `component.xml` | Vivado IP 的元数据清单文件，是「这个 IP 已打包好」的标志产物 |
| `.xsa` | Xilinx 硬件交付文件（旧称 `.hdf`/bitstream 时代的产物），是工程构建的最终交付 |
| `flock` | Linux 的文件锁命令，用于让多个进程互斥地执行同一段命令 |
| `incremental_checkpoint` | Vivado 实现阶段（impl）的增量编译参考检查点，复用上一次的布局布线结果 |

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [projects/scripts/project-xilinx.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk) | **本讲主角**。每个 Xilinx 工程都 include 它，定义从依赖汇总到 `.xsa` 产物的全部规则 |
| [projects/scripts/project-toplevel.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk) | 可复用的「递归子目录」骨架，自动发现直接子目录里的 `Makefile` 并 `make -C` 进入 |
| [projects/fmcomms2/zcu102/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile) | 具体工程示例：声明 `PROJECT_NAME`、`M_DEPS`、`LIB_DEPS`，最后 include 主角脚本 |
| [quiet.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk) | 提供 `build` / `clean` / `skip_if_missing` 三个被反复 include 的宏 |
| [library/scripts/library.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk) | library 侧的打包脚本，定义 `xilinx` 目标与 `component.xml` 的真正生成规则（用于对照工程侧如何调用它） |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | Tcl 侧工程助手，最终消费 `reference.dcp` 检查点（增量编译的下游） |

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：先讲**总装配**（依赖如何汇总、最终目标如何定义、clean 怎么做），再讲 **component.xml 打包目标**，接着讲 **flock 并行安全与版本透传**，最后讲 **CFG 参数化与增量编译**。

### 4.1 工程构建的总装配：依赖、目标与递归

#### 4.1.1 概念说明

`project-xilinx.mk` 是一份被所有 Xilinx 工程「共享」的公共脚本。它本身不写死任何具体工程信息，而是读取每个工程 `Makefile` 里提前设置好的两个变量：

- `PROJECT_NAME`：工程名（如 `fmcomms2_zcu102`），决定所有产物文件名前缀；
- `M_DEPS`：本工程**直接引用的文件**（块设计 Tcl、约束、顶层 Verilog 等）；
- `LIB_DEPS`：本工程**依赖的 library IP 名字**（如 `axi_dmac`）。

公共脚本的工作就是：把这三类信息汇总成一组 GNU Make 规则，定义出最终产物 `$(PROJECT_NAME).sdk/system_top.xsa`，并声明它依赖所有 `M_DEPS` 文件和所有 `LIB_DEPS` 对应的 `component.xml`。

一句话总结：**工程 Makefile 负责「报菜名」，公共脚本负责「按菜名搭出依赖图并跑出产物」。**

#### 4.1.2 核心流程

整个 `project-xilinx.mk` 的执行可以概括为五步：

1. **定位路径**：根据自身文件位置算出 `HDL_PROJECT_PATH`（`projects/scripts/`）和 `HDL_LIBRARY_PATH`（`library/`）。
2. **解析参数化**（见 4.4）：若传了 `CFG` 或命令行变量，算出一个独立的 `DIR_NAME` 子目录，把构建产物隔离进去。
3. **汇总依赖**：往 `M_DEPS` 里追加每个工程都有的公共脚本（`system_project.tcl`、`adi_project_xilinx.tcl`、`adi_env.tcl`、`adi_board.tcl` 等），并用 `foreach` 把 `LIB_DEPS` 展开成一条条 `library/<ip>/component.xml` 依赖。
4. **定义目标**：`all` → `external_dependencies` + `$(PROJECT_NAME).sdk/system_top.xsa`；后者依赖全部 `M_DEPS`。
5. **定义 component.xml 模式规则**（见 4.2、4.3）：把每个 `component.xml` 的生成委派给对应 library 目录的 `make xilinx`，并用 `flock` 串行化。

依赖图大致如下：

```
make all
  └─ external_dependencies ── 检查 EXTERNAL_DEPS 目录是否存在（缺失则记进 missing_external.log）
  └─ $(PROJECT_NAME).sdk/system_top.xsa      ← 最终交付
        └─ M_DEPS（文件依赖）
        │     ├─ system_project.tcl / system_bd.tcl / system_top.v / system_constr.xdc   （工程自有）
        │     ├─ adi_project_xilinx.tcl / adi_env.tcl / adi_board.tcl                    （公共脚本）
        │     ├─ EXTERNAL_DEPS                                                            （外部目录）
        │     └─ foreach LIB_DEPS → library/<ip>/component.xml                            （IP 依赖）
        └─ [each] library/<ip>/component.xml   ← 见 4.2 / 4.3
```

#### 4.1.3 源码精读

先看脚本如何自报家门、定位两个关键路径，并 include 公共宏库：

[projects/scripts/project-xilinx.mk:6-10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L6-L10) —— 用 `$(lastword $(MAKEFILE_LIST))` 取到当前正在被 include 的脚本自身路径，再 `subst` 掉文件名得到 `HDL_PROJECT_PATH`，进而推出 `HDL_LIBRARY_PATH`。这样无论仓库被 clone 到哪里，路径都自动正确，不必写死。

接下来是**汇总公共依赖**的核心一段：

[projects/scripts/project-xilinx.mk:72-83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L72-L83) —— 这里做了两件事：
- 第 73–81 行把每个工程都一定需要的文件加进 `M_DEPS`：`system_project.tcl`、`system_bd.tcl`、`system_top*.v`、`system_constr*.xdc`、`system_constr*.tcl`，以及三份公共脚本和外部依赖 `$(EXTERNAL_DEPS)`。
- **第 83 行是本讲的关键一行**：`M_DEPS += $(foreach dep,$(LIB_DEPS),$(HDL_LIBRARY_PATH)$(dep)/component.xml)`。它用 `foreach` 遍历工程声明的每个 `LIB_DEPS`，拼出对应的 `library/<ip>/component.xml` 路径，加进 `M_DEPS`。也就是说，**「依赖一个 IP」在 Make 层面等价于「依赖它的 `component.xml` 打包产物」**。

再看最终产物目标与外部依赖检查：

[projects/scripts/project-xilinx.mk:87](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L87) —— `all` 依赖两样：先做 `external_dependencies`（外部目录体检），再做最终的 `.xsa`。

[projects/scripts/project-xilinx.mk:104-112](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L104-L112) —— 外部依赖机制：`external_dependencies_cleanup` 先删掉旧的 `missing_external.log`；随后对每个 `$(EXTERNAL_DEPS)`（通常是外部 IP 仓库的目录路径）检查 `[ ! -d $@ ]`，若目录不存在就把该路径追加进 `missing_external.log`。只要这份日志最终存在，后面的 `skip_if_missing` 就会让工程被优雅地 `SKIPPED`（而非报错中断），这正是 u3-l1 讲过的「缺依赖就跳过」在工程层的落地。

[projects/scripts/project-xilinx.mk:116-136](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L116-L136) —— `.xsa` 目标的 recipe（增量编译部分留到 4.4 讲）。它的主体是 `skip_if_missing` 包裹的一段：先 `rm -rf $(CLEAN_TARGET)` 清掉旧产物，再用 `build` 宏执行 `$(VIVADO) system_project.tcl`，把全部输出收进 `$(PROJECT_NAME)_vivado.log`。注意这里调用的 `$(VIVADO)` 是 `vivado -tempDir ... -mode batch -source`（[第 22 行](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L22)），即以批处理模式 source `system_project.tcl`——这就接回了 u1-l4 讲过的「`system_project.tcl` 是 Vivado 入口」。

`clean` 与 `clean-all` 也很直观：

[projects/scripts/project-xilinx.mk:91-102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L91-L102) —— `clean` 删掉本工程的所有 `CLEAN_TARGET`（`.cache/.data/.xpr/.runs/.srcs/.xsa/.log` 等）和参数化子目录 `$(DIR_NAME)`；`clean-all` 额外遍历 `LIB_DEPS`，对每个依赖的 library 执行 `make clean`，把 IP 打包产物也一并清掉。

最后看一眼**递归骨架**，理解「make 怎么进入这个叶子工程」：

[projects/scripts/project-toplevel.mk:11](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk#L11) —— `SUBDIRS := $(dir $(wildcard */Makefile))` 自动发现所有「直接子目录里含 `Makefile`」的目录。

[projects/scripts/project-toplevel.mk:24-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk#L24-L25) —— 一条模式规则：对 `proj/all`、`proj/clean` 这类目标，执行 `$(MAKE) -C $(@D) $(@F)`，即进入目标目录（`$(@D)`）跑对应动作（`$(@F)`）。正是它让 `make fmcomms2.zcu102` 能一路递归到 `projects/fmcomms2/zcu102/`，在那里 include 我们的 `project-xilinx.mk`。

#### 4.1.4 代码实践

**实践目标**：手工汇总一个真实工程的完整依赖清单，验证 `M_DEPS` 的来源分两批。

**操作步骤**（源码阅读型实践）：

1. 打开 [projects/fmcomms2/zcu102/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile)。
2. 记下它自己往 `M_DEPS` 里 `+=` 的全部条目（[第 9–14 行](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L9-L14)）。
3. 记下它声明的全部 `LIB_DEPS`（[第 16–25 行](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L16-L25)）。
4. 对照 [project-xilinx.mk:72-83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L72-L83)，列出公共脚本**额外自动加入** `M_DEPS` 的条目。

**需要观察的现象 / 预期结果**：

- 工程 Makefile 自己加了 6 项 `M_DEPS`（`fmcomms2_bd.tcl`、`adi_pd.tcl`、`zcu102_system_constr.xdc`、`zcu102_system_bd.tcl`、`ad_iobuf.v`、`axi_ad9361_delay.tcl`）。
- `LIB_DEPS` 共 10 个 IP（`axi_ad9361`、`axi_dmac`、`axi_sysid`、`sysid_rom`、`util_pack/util_cpack2`、`util_pack/util_upack2`、`util_rfifo`、`util_tdd_sync`、`util_wfifo`、`xilinx/util_clkdiv`），每个都会被第 83 行展开成一个 `component.xml` 依赖。
- 公共脚本自动加入的 `M_DEPS` 还有 `system_project.tcl`、`system_bd.tcl`、`system_top*.v`、`system_constr*.xdc`、`adi_project_xilinx.tcl`、`adi_env.tcl`、`adi_board.tcl` 等。
- 该工程的 `EXTERNAL_DEPS` 未设置，因此 `external_dependencies` 对它是空操作，构建不会被跳过。

> 注：以上为纯源码阅读结果，无需运行 Vivado；若想实测，可在工程目录执行 `make -n all` 做 dry-run，在打印里数 `component.xml` 出现的次数。**待本地验证** dry-run 输出。

#### 4.1.5 小练习与答案

**练习 1**：如果某个工程的 `EXTERNAL_DEPS` 里写了一个不存在的目录，构建会报错退出吗？

> **答案**：不会报错退出。`$(EXTERNAL_DEPS)` 规则（[L109-112](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L109-L112)）只是把缺失目录写进 `missing_external.log`；随后 `.xsa` recipe 里的 `skip_if_missing` 发现这份日志存在，就打印 `SKIPPED` 并跳过构建，整体流程继续。

**练习 2**：为什么 `system_project.tcl` 出现在 `M_DEPS` 里（[L73](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L73)），而不是被写进某个固定规则？

> **答案**：把它放进 `M_DEPS`，就让它成为 `.xsa` 目标的先决条件。一旦你修改了 `system_project.tcl`，Make 就会判定 `.xsa` 过期、自动重新构建——这正是「改了脚本就重跑」的依赖驱动效果。

---

### 4.2 component.xml：把 LIB_DEPS 翻译成 IP 打包目标

#### 4.2.1 概念说明

第 4.1 节我们看到，工程依赖一个 IP 等价于依赖它的 `component.xml`。但 `component.xml` 并不是工程自己生成的——它由该 IP 所在的 library 目录「打包」产生。所以 `project-xilinx.mk` 需要一条规则：**当工程需要 `library/axi_dmac/component.xml` 时，自动进入 `library/axi_dmac/` 跑一次打包，把这个 `component.xml` 造出来。**

GNU Make 的**模式规则（pattern rule）**正好胜任这件事：用 `%` 通配匹配 IP 名字，一条规则覆盖所有 IP。

#### 4.2.2 核心流程

```
工程需要 library/<ip>/component.xml
        │  （模式规则匹配 % = <ip>）
        ▼
进入 library/<ip>/，执行 make xilinx
        │  （由 library.mk 负责）
        ▼
library.mk 的 component.xml 目标：
   skip_if_missing → build(vivado <ip>_ip.tcl) → 生成 component.xml
```

两份 Makefile 各司其职：

- **工程侧**（`project-xilinx.mk`）：只负责「声明依赖 + 触发打包」，不关心 IP 内部细节；
- **库侧**（`library.mk`）：负责真正的打包动作（调用 `<ip>_ip.tcl` 让 Vivado 生成 `component.xml`）。

#### 4.2.3 源码精读

工程侧的模式规则只有三行，但信息量很大：

[projects/scripts/project-xilinx.mk:138-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L138-L146) —— 逐行拆解：

- **第 138 行** `$(HDL_LIBRARY_PATH)%/component.xml: TARGET:=xilinx`：这是一条**目标专属变量（target-specific variable）**赋值。它告诉 Make：当本规则匹配的目标（某个 `library/<ip>/component.xml`）被构建时，把变量 `TARGET` 设为 `xilinx`。这个 `TARGET` 会在 recipe 里作为传给库侧的 make 目标名。
- **第 139 行** `FORCE:`：定义一个空目标 `FORCE`。GNU Make 约定，任何依赖 `FORCE` 的目标都会被当作「永远过期」，从而**每次都重跑 recipe**。
- **第 140 行** `$(HDL_LIBRARY_PATH)%/component.xml: FORCE`：模式规则本体——目标匹配 `library/<任意>/component.xml`，先决条件是 `FORCE`。
- **第 141–146 行** recipe：用 `flock` 串行化后（详见 4.3），执行 `$(MAKE) -C $(dir $@) $(TARGET)`，即「进入目标所在目录（`$(dir $@)` = `library/<ip>/`），跑 `make xilinx`」。若上层传了 `REQUIRED_VIVADO_VERSION`，则带上它一起透传。

那么库侧的 `make xilinx` 究竟做了什么？对照看 [library/scripts/library.mk:102](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L102)：`xilinx: external_dependencies component.xml`，即库的 `xilinx` 目标最终要产出 `component.xml`。

而 `component.xml` 的真正生成规则在 [library/scripts/library.mk:116-125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125)：用 `skip_if_missing` + `build` 宏执行 `$(VIVADO) $(LIBRARY_NAME)_ip.tcl`——也就是调用该 IP 的 `*_ip.tcl` 脚本（如 `axi_dmac_ip.tcl`）让 Vivado 把 Verilog 打包成 IP，产出 `component.xml`。

把两侧串起来就清楚了：**工程 Makefile 的第 83 行声明依赖 → 第 140 行模式规则触发 → 库 `make xilinx` → 库 `component.xml` 规则跑 `*_ip.tcl` → 生成 `component.xml`。** 一个 `LIB_DEPS` 名字，就这样变成了一次实实在在的 IP 打包。

#### 4.2.4 代码实践

**实践目标**：追踪 `axi_dmac` 这个 IP 从「被工程依赖」到「`component.xml` 被打包出来」的完整调用链。

**操作步骤**（源码阅读型实践）：

1. 在 [fmcomms2/zcu102/Makefile:17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L17) 找到 `LIB_DEPS += axi_dmac`。
2. 跟到 [project-xilinx.mk:83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L83)，确认它被展开成 `library/axi_dmac/component.xml`。
3. 跟到 [project-xilinx.mk:140-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L140-L146)，确认会执行 `make -C library/axi_dmac xilinx`。
4. 跟到 [library.mk:116-125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125)，确认库侧最终跑的是 `vivado axi_dmac_ip.tcl`。
5. 确认 `axi_dmac_ip.tcl` 确实存在（它是 [axi_dmac 的 XILINX_DEPS](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_dmac/Makefile#L42) 之一）。

**需要观察的现象 / 预期结果**：

- 你应当能写出一条无歧义的链：`LIB_DEPS(axi_dmac)` →（第 83 行展开）→ `library/axi_dmac/component.xml` →（第 140 行模式规则）→ `make -C library/axi_dmac xilinx` →（库 L116）→ `vivado axi_dmac_ip.tcl` → 产出 `component.xml`。
- 关键认知：**工程 Makefile 从不直接调用 `*_ip.tcl`，它只调用库的 `make xilinx`；打包细节封装在库侧。**

> 若想实测：在工程目录执行 `make -n lib`（`lib` 目标见 [project-xilinx.mk:89](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L89)，只构建依赖、不跑最终 Vivado），dry-run 打印里应能看到每个 IP 的 `flock ... make -C ... xilinx` 行。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：模式规则用的是 `%`，为什么还要配一个 `FORCE` 先决条件？

> **答案**：仅有 `%` 的模式规则在 `component.xml` 已存在且比其源码新时，Make 会认为「不必重建」从而跳过打包。而工程希望「每次 `make` 都确保 IP 是最新的」（尤其在并行/增量场景下），所以用 `FORCE` 让目标永远过期、recipe 每次都执行；真正的「是否真的要重新跑 Vivado」由库侧 `library.mk` 根据源码时间戳再判断。

**练习 2**：第 138 行的 `TARGET:=xilinx` 为什么不直接写死在 recipe 里？

> **答案**：写成目标专属变量是一种解耦：同一份 `project-xilinx.mk` 未来若要复用于触发 Intel/Lattice 的打包（不同 `TARGET`），只需在调用处改 `TARGET`，不必改 recipe。这也让 recipe 与「具体厂商目标名」分离，便于维护。

---

### 4.3 flock 并行安全与 REQUIRED_VIVADO_VERSION 透传

#### 4.3.1 概念说明

ADI HDL 仓库支持用 `make -jN` 并行构建多个工程。但这里有一个隐患：**很多工程会依赖同一个 IP**（比如 `axi_dmac` 几乎被所有数据通路工程引用）。如果两个工程同时发现「`library/axi_dmac/component.xml` 不存在 / 需要重建」，两个并行的 `make` 就会**同时**进入 `library/axi_dmac/` 跑 Vivado 打包——两份 Vivado 进程写同一批文件，必然产生冲突、损坏产物。

`flock`（Linux 文件锁命令）就是用来解决这个问题的：它在 `library/<ip>/` 目录下放一个 `.lock` 文件，谁先拿到锁谁先打包，另一个进程在 `flock` 处**阻塞等待**，等前者打包完释放锁再进入。这样即便多个工程并行触发同一 IP 的打包，实际打包也只会发生一次（后续等待者进入时 `component.xml` 已是最新，库侧按时间戳可快速完成或直接跳过）。

与之配套的是 `REQUIRED_VIVADO_VERSION` 透传：用户在命令行用 `make REQUIRED_VIVADO_VERSION=2024.1` 覆盖版本时，这个值要一路传到 Tcl 层的 `adi_env.tcl`，让版本检查按用户指定值进行。

#### 4.3.2 核心流程

并行场景下的时序：

```
工程 A ─┐
        ├─ 同时需要 library/axi_dmac/component.xml
工程 B ─┘

工程 A 的 make：flock axi_dmac/.lock  →  拿到锁，进入打包（make xilinx）
工程 B 的 make：flock axi_dmac/.lock  →  阻塞等待……

工程 A 打包完成，释放锁
工程 B 拿到锁，进入 axi_dmac/ → 此时 component.xml 已是最新，库侧按需快速完成
```

版本透传链路：

```
用户命令行: make REQUIRED_VIVADO_VERSION=2024.1
        │  （MAKE 命令行变量，最高优先级）
        ▼
project-xilinx.mk recipe: 把 REQUIRED_VIVADO_VERSION=${...} 作为 make 参数传给库
        ▼
库 make 启动 Vivado，Vivado source adi_env.tcl
        ▼
adi_env.tcl: ::env(REQUIRED_VIVADO_VERSION) 生效，覆盖默认要求版本
```

#### 4.3.3 源码精读

回到那三行规则，重点看 recipe 里的 `flock`：

[projects/scripts/project-xilinx.mk:140-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L140-L146) —— 关键是第 141 行：

```makefile
flock $(dir $@).lock sh -c " \
if [ -n \"${REQUIRED_VIVADO_VERSION}\" ]; then \
    $(MAKE) -C $(dir $@) $(TARGET) REQUIRED_VIVADO_VERSION=${REQUIRED_VIVADO_VERSION}; \
else \
    $(MAKE) -C $(dir $@) $(TARGET); \
fi"; exit $?
```

- `$(dir $@)` 是目标所在目录（如 `library/axi_dmac/`），所以锁文件就是 `library/axi_dmac/.lock`——**锁与 IP 一一对应**，不同 IP 用不同锁、互不阻塞，只有「同一个 IP」才会被串行化。
- `flock <lockfile> sh -c "..."` 的语义：`flock` 先尝试获取 `<lockfile>` 的排他锁；拿不到就阻塞等待；拿到后才执行后面的 `sh -c "..."` 命令；命令结束（无论成功失败）自动释放锁。
- `sh -c "..."` 里包了一个 `if`：若上层设置了 `REQUIRED_VIVADO_VERSION`，就把它作为 make 参数透传给库侧的 `$(MAKE)`；否则不带这个参数。
- 末尾的 `exit $$?` 把内层 `make` 的退出码透传出来，确保打包失败时整条规则也失败。

库侧也有完全对称的 `flock` 设计，用于库与库之间的跨库依赖并行安全：

[library/scripts/library.mk:130-131](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L130-L131) —— `flock $(dir $@).lock -c "$(MAKE) -C $(dir $@) xilinx"`，同样的「按目录加锁」思路。两侧使用**同名 `.lock` 文件**（都在 `library/<ip>/.lock`），所以工程侧锁和库侧锁实际是同一把锁，能正确互斥。

版本号最终在哪里被消费？在 [scripts/adi_env.tcl:21-24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L21-L24)：`adi_env.tcl` 优先读 shell 环境变量 `::env(REQUIRED_VIVADO_VERSION)`，再读 Tcl 变量 `REQUIRED_VIVADO_VERSION`，用它覆盖默认要求版本。而 Make 命令行变量在子进程中会自动导出为同名环境变量，于是「`make REQUIRED_VIVADO_VERSION=2024.1`」最终被 `adi_env.tcl` 看到——这就是 u1-l3 讲过的「shell 环境变量最高优先级」在 Make/Tcl 两层之间的完整闭环。

#### 4.3.4 代码实践

**实践目标**：用 dry-run 直观看到 `flock` 与版本透传，并理解锁文件的作用范围。

**操作步骤**：

1. 进入 `projects/fmcomms2/zcu102/`。
2. 执行 `make -n lib`（dry-run，只打印命令不执行），在输出里找到针对 `axi_dmac` 的那一行。
3. 再执行 `make -n lib REQUIRED_VIVADO_VERSION=2024.1`，对比同一行命令的变化。

**需要观察的现象 / 预期结果**：

- 第一次 dry-run 里应能看到类似 `flock .../axi_dmac/.lock sh -c "... make -C .../axi_dmac xilinx ..."` 的命令，且 `if` 分支走 `else`（不带版本参数）。
- 第二次由于设置了 `REQUIRED_VIVADO_VERSION`，`if` 分支应走带 `REQUIRED_VIVADO_VERSION=2024.1` 的那条 `make` 命令——直观看到「命令行变量被透传进了子 make」。
- 多个不同 IP 的锁文件路径各不相同（`axi_dmac/.lock`、`axi_ad9361/.lock`……），印证「锁按 IP 隔离、互不阻塞；同 IP 才串行」。

> 注：`make -n` 只打印不执行，不会真的调用 Vivado，也不会真的创建 `.lock`，所以这是安全的纯观察实践。真实 `flock` 阻塞行为需要真正并行构建才能看到。**待本地验证** dry-run 文本（不同 make 版本变量插值细节可能略有差异）。

#### 4.3.5 小练习与答案

**练习 1**：如果去掉 `flock`、直接写 `$(MAKE) -C $(dir $@) $(TARGET)`，在 `make -j4` 同时构建两个工程时会发生什么？

> **答案**：两个工程的 make 可能同时匹配到 `library/axi_dmac/component.xml` 这条模式规则，于是两个 Vivado 进程同时进入 `library/axi_dmac/` 跑 `axi_dmac_ip.tcl`，争抢同一批中间文件（`.xpr`/`.srcs`/`component.xml`），轻则打包失败、重则产出损坏的 `component.xml`。`flock` 用一把与 IP 绑定的锁把这种临界区串行化，从根本上避免竞态。

**练习 2**：为什么工程侧（`project-xilinx.mk`）和库侧（`library.mk`）的 `flock` 都用 `$(dir $@).lock` 这个**相对目录**的锁文件名，而不是一把全局锁？

> **答案**：用「按 IP 目录」命名锁，可以做到「不同 IP 可并行打包、只有同一 IP 才互斥」。如果用一把全局锁，所有 IP 打包都会被串成一条，丧失并行加速。锁的粒度精确到「一个 IP 目录」，是并行度与安全性之间的最佳平衡点。同时两侧用同名锁，保证无论从工程触发还是从库的跨库依赖触发，对同一 IP 都是同一把锁。

---

### 4.4 CFG 参数化与增量编译 MODE

#### 4.4.1 概念说明

`project-xilinx.mk` 还支持两种「进阶用法」：

- **CFG 参数化**：同一份工程源码，可以用不同的参数配置构建出多个变体。通过 `make CFG=<config-file>` 或在命令行传变量，脚本会算出一个独立的子目录名 `DIR_NAME`，把这次构建的所有产物隔离进该子目录，互不污染。
- **增量编译 MODE=incr**：默认每次都从零综合实现；若设 `MODE=incr`，脚本会把上一次实现阶段的布线检查点（`system_top_routed.dcp`）拷成 `reference.dcp`，作为本次实现的增量参考，复用上次的布局布线以加速收敛。

这两个特性都靠 Make 变量驱动，体现了「同一套源码、参数化产出」的设计。

#### 4.4.2 核心流程

CFG 参数化的命名逻辑：

```
若 make CFG=my_config.mk：
    include 该文件 → 把其中变量 export 出去
    DIR_NAME = my_config   （取 CFG 文件名去后缀）

若命令行传了变量（如 make DMA_TYPE=1）：
    把变量名/值拼成参数字符串
    经 sed 规范化（去 JESD/LANE、去下划线）→ GEN_NAME
    DIR_NAME 在 CFG 名基础上再追加 _GEN_NAME

最终：PROJECT_NAME = DIR_NAME/PROJECT_NAME
      所有产物写进 DIR_NAME/ 子目录
```

增量编译的检查点逻辑：

```
.xsa 目标 recipe 先判断 MODE：
  if MODE == incr:
      找 */impl_1/system_top_routed.dcp（上次实现产物）
      若存在 → cp -u 拷成 ./reference.dcp（仅当更新时拷）
      若 reference.dcp 存在 → 打印"Using reference checkpoint"
  else (default):
      rm -f reference.dcp   ← 默认模式强制从零开始
然后才进入 skip_if_missing + build 跑 Vivado
```

#### 4.4.3 源码精读

先看 CFG 与命令行变量的解析：

[projects/scripts/project-xilinx.mk:13-20](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L13-L20) —— `ifdef CFG` 时：`include $(CFG)` 把配置文件里的赋值读进来；`export $(shell sed 's/=.*//' $(CFG) ...)` 把其中的变量名提取出来并全部导出为环境变量（这样 Tcl 也能读到）；`DIR_NAME` 取 CFG 文件名去掉后缀（`$(basename $(notdir $(CFG)))`）。

[projects/scripts/project-xilinx.mk:24-35](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L24-L35) —— 处理命令行传进来的变量：把 `KEY=VALUE` 形式规范化成 `KEY_VALUE`，再经 `GEN_SED`（去掉 `JESD`/`LANE` 字样和下划线，见 [第 13–14 行](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L13-L14)）拼成 `GEN_NAME`，最后追加到 `DIR_NAME` 上。注意第 25 行用 `expr $(MAKELEVEL) % 2` 根据 make 的递归层级交替选取命令变量来源——这是为了在多层递归 make 时仍能稳定捕获用户最初传入的变量。

[projects/scripts/project-xilinx.mk:37-43](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L37-L43) —— 一旦 `DIR_NAME` 非空：把 `PROJECT_NAME` 改成 `DIR_NAME/PROJECT_NAME`（产物隔离进子目录）、`mkdir` 建子目录、导出 `ADI_PROJECT_DIR`，并改写 `VIVADO` 命令让日志/临时目录也落在该子目录里。这就是「参数化变体互不污染」的实现。

再看增量编译：

[projects/scripts/project-xilinx.mk:114](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L114) —— `MODE ?= "default"`，默认非增量。

[projects/scripts/project-xilinx.mk:116-127](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L116-L127) —— `.xsa` recipe 开头先处理 `reference.dcp`：`MODE=incr` 时，用 glob `*/impl_1/system_top_routed.dcp` 找上次实现阶段的布线检查点（Vivado 会把它产在 `<project>.runs/impl_1/` 下，所以 `*/impl_1/...` 能匹配到），用 `cp -u`（仅当源更新时拷贝）拷成 `./reference.dcp`；默认模式则 `rm -f reference.dcp` 强制丢弃旧检查点。

这个 `reference.dcp` 最终被 Tcl 侧消费：

[projects/scripts/adi_project_xilinx.tcl:316-319](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L316-L319) —— 当增量编译开关 `ADI_USE_INCR_COMP == 1` 且 `./reference.dcp` 存在时，把 `reference.dcp` 设为实现阶段 `impl_1` 的 `incremental_checkpoint` 属性。Vivado 实现时会以此为参考、尽量复用未变化逻辑的布局布线，从而加速时序收敛。这就是「Make 准备检查点、Tcl 消费检查点」的协作。

最后注意 clean 会清掉它：

[projects/scripts/project-xilinx.mk:91-92](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L91-L92) —— `clean` 目标第一行就 `-rm -f reference.dcp`，确保清理后下次默认是干净的全量构建。

#### 4.4.4 代码实践

**实践目标**：通过源码阅读与（可选的）dry-run，预测 `make MODE=incr` 的行为差异。

**操作步骤**：

1. 阅读 [project-xilinx.mk:116-127](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L116-L127)，写下 `MODE=incr` 与默认模式在 recipe 开头的两条不同分支动作。
2. 阅读 [adi_project_xilinx.tcl:316-319](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L316-L319)，确认 `reference.dcp` 是如何被设进 `impl_1` 的。
3. （可选 dry-run）在工程目录执行 `make -n MODE=incr`，在打印里查找 `reference.dcp`、`cp -u`、`Using reference checkpoint` 等字样。

**需要观察的现象 / 预期结果**：

- `MODE=incr` 分支会尝试 `cp -u */impl_1/system_top_routed.dcp ./reference.dcp`；若上次没有实现产物（首次构建），则 `reference.dcp` 不存在，本次实际仍为全量构建。
- 默认模式分支会 `rm -f reference.dcp`，即「无论上次有没有检查点，默认都丢弃、从零实现」。
- 综合理解：增量编译只有在「已有一轮成功实现产物」之后才真正生效；它依赖 `<project>.runs/impl_1/system_top_routed.dcp` 的存在。

> 注：真实加速效果需在有 Vivado 环境并已完成首轮实现后才能观察。dry-run 只能看到命令意图、看不到实际加速。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `cp -u`（而非 `cp`）更适合用来拷贝 `reference.dcp`？

> **答案**：`cp -u` 只在源文件比目标文件新、或目标不存在时才拷贝。这样可以避免用旧的检查点覆盖更新过的检查点，保证 `reference.dcp` 始终是「最近一次成功实现」的结果，防止误用过期参考导致增量编译反而劣化时序。

**练习 2**：执行 `make clean` 后，下一次 `make MODE=incr` 还能立即享受增量加速吗？

> **答案**：不能。`clean` 会 `-rm -f reference.dcp`（[L92](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L92)），同时清掉 `*.runs` 等产物，于是 `*/impl_1/system_top_routed.dcp` 也不复存在。`MODE=incr` 分支找不到源检查点，`reference.dcp` 不会被创建，本次实际退化为全量构建。增量加速要在「保留上一轮实现产物」的前提下才生效。

**练习 3**：若你用 `make CFG=my_alt.mk` 构建了一个变体，它和默认构建的产物会互相覆盖吗？

> **答案**：不会。设置 `CFG` 后 `DIR_NAME` 非空（取为 `my_alt`），[第 38 行](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L38)把 `PROJECT_NAME` 改成 `my_alt/$(PROJECT_NAME)`，所有产物（`.xpr`/`.runs`/`.xsa`/日志）都落在 `my_alt/` 子目录里，与默认构建的产物物理隔离。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面的综合任务（源码阅读 + 画图为主）。

**任务**：以 `projects/fmcomms2/zcu102` 为对象，画出从用户敲 `make` 到最终 `fmcomms2_zcu102.sdk/system_top.xsa` 产出的**完整依赖链**，并解释其中两处关键机制。

**要求产出**：

1. **依赖链图**：至少包含以下节点，并标出依赖箭头——
   - `make`（顶层）→ 递归进入 `projects/fmcomms2/zcu102/`（经 [project-toplevel.mk](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-toplevel.mk)）；
   - `all` → `external_dependencies` + `fmcomms2_zcu102.sdk/system_top.xsa`（[L87](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L87)）；
   - `.xsa` → `M_DEPS`（文件依赖，含工程自有的 6 项 + 公共脚本若干项）；
   - `M_DEPS` → 每个 `LIB_DEPS` 对应的 `library/<ip>/component.xml`（[L83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L83)）；
   - 每个 `component.xml` → `flock ... make -C library/<ip> xilinx`（[L140-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L140-L146)）；
   - 库 `make xilinx` → `vivado <ip>_ip.tcl`（[library.mk:116-125](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/scripts/library.mk#L116-L125)）。
2. **flock 解释**：用一段话说明「为何 `flock` 锁文件能避免多工程并行打包同一 IP 时出错」。要点应包括：锁按 IP 目录命名（`library/<ip>/.lock`）、`flock` 排他锁会让后到者阻塞等待、工程侧与库侧共用同名锁、不同 IP 之间互不阻塞。
3. **版本透传延伸**：在图上标注，若用户用 `make REQUIRED_VIVADO_VERSION=2024.1`，这个值是如何从命令行经 [L142-143](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L142-L143) 传到库 make、再到 `adi_env.tcl` 的。

**预期结果**：得到一张清晰的「依赖图 + 三段说明」，能向另一位读者讲清「`make` 之后、Vivado 真正启动之前，Make 层面都做了哪些准备，以及为什么并行构建不会撞车」。

> 本任务以源码阅读和画图为主，无需运行 Vivado；如需佐证，可用 `make -n all` 做 dry-run 对照你画的依赖链。**待本地验证** dry-run 细节。

## 6. 本讲小结

- `project-xilinx.mk` 是所有 Xilinx 工程共享的公共构建脚本，工程 Makefile 只负责用 `PROJECT_NAME` / `M_DEPS` / `LIB_DEPS`「报菜名」，公共脚本负责汇总依赖、定义产物、驱动构建。
- **第 83 行**用 `foreach` 把 `LIB_DEPS` 展开成一条条 `library/<ip>/component.xml`，于是「依赖一个 IP」在 Make 层等价于「依赖它的打包产物」。
- 最终产物 `$(PROJECT_NAME).sdk/system_top.xsa` 依赖全部 `M_DEPS`；其 recipe 经 `skip_if_missing` + `build` 调用 `vivado -mode batch -source system_project.tcl`，把构建的真正执行交给 Tcl 入口。
- **模式规则**（[L138-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L138-L146)）把每个 `component.xml` 的生成委派给库侧的 `make xilinx`，库侧再用 `*_ip.tcl` 真正打包 IP；工程与库职责分离。
- `flock $(dir $@).lock` 用「按 IP 目录命名」的排他锁串行化打包，让多工程并行构建同一 IP 时不会争抢损坏产物；`REQUIRED_VIVADO_VERSION` 经 make 参数透传到库、再到 `adi_env.tcl`，完成跨层版本覆盖。
- `CFG` 参数化用独立子目录 `DIR_NAME` 隔离变体产物；`MODE=incr` 用上轮实现的 `system_top_routed.dcp` 拷成 `reference.dcp`，供 Tcl 侧 `impl_1` 的 `incremental_checkpoint` 复用，加速时序收敛。

## 7. 下一步学习建议

本讲只讲到「Make 层如何驱动 Vivado」，但 recipe 里反复出现的 `$(VIVADO) system_project.tcl` 仍是一个黑盒。建议接下来：

- **读 u3-l3（Tcl 工程助手：adi_project_xilinx.tcl）**：拆开 `system_project.tcl` 里 `adi_project` / `adi_project_create` / `adi_project_files` / `adi_project_run` 的调用顺序，看 Make 把控制权交给 Tcl 后，Vivado 工程是如何被一步步创建、加文件、综合、实现并写出 `.xsa` 的；尤其对照本讲引用的 [adi_project_xilinx.tcl:316-319](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L316-L319)，理解 `reference.dcp` 的下游消费。
- **读 u3-l4（板级连线助手 Tcl：adi_board.tcl）**：进入 `system_bd.tcl` 拼装块设计的细节，看 `ad_connect` / `ad_cpu_interconnect` 等原语如何抽象 Vivado BD 操作。
- **回看 u4-l1（库结构与多厂商依赖：library.mk）**：本讲多次引用 `library.mk`，下一篇会系统讲它的多厂商（GENERIC/XILINX/INTEL/LATTICE）依赖分组与跨库依赖传递，把本讲「库侧打包」这一半补全。
