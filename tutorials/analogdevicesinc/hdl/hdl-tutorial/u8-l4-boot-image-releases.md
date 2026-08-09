# Boot 镜像生成与发布管理

## 1. 本讲目标

本讲是「测试、规范与高级主题」单元的收尾篇，回答一个具体问题：**HDL 工程构建出来的比特流（`system_top.xsa`），怎样变成 SD 卡上能让开发板上电启动的 `BOOT.BIN`？**

学完后你应该能够：

1. 说清 `BOOT.BIN` 是什么、它由哪些「分区镜像」拼装而成，以及为什么 FSBL/U-Boot/`bl31.elf` 缺一不可。
2. 逐段读懂 [`projects/scripts/adi_make_boot_bin.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl) 这个打包引擎：它接收什么输入、如何从 `.xsa` 里探测 CPU 型号、如何用 Vitis 生成 FSBL、如何写 `.bif` 并调用 `bootgen`。
3. 理解 [`projects/scripts/adi_make.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl) 的 `adi_make::boot_bin` 封装如何为 Vivado 控制台用户提供一键入口。
4. 结合 [`docs/user_guide/releases.rst`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst)，说清 release 分支与工具版本的绑定关系，并解释为什么官方「推荐用最新 release 分支、而非 main 的预构建文件」。

本讲承接 u1-l4（`make` 到比特流）与 u3-l2（`project-xilinx.mk` 产出 `system_top.xsa`）：那两讲负责「造出硬件」，本讲负责「把硬件交付物连同软件引导程序打包成可启动镜像」。

## 2. 前置知识

阅读本讲前，请先具备以下认知（前序讲义已建立）：

- **比特流与 `.xsa`**：`make` 跑完综合实现后，Vivado 产出 `system_top.xsa`（Xilinx Shell Archive），它是把比特流、块设计、器件约束等打包在一起的硬件交付文件（见 u1-l4、u3-l2）。
- **AXI 与 PS/PL**：Zynq/ZynqMP 这类器件分处理系统（PS，ARM 核）与可编程逻辑（PL，FPGA）。`BOOT.BIN` 既要装载 PL 的比特流，又要启动 PS 上的软件链。
- **Tcl 脚本**：本讲两个核心文件都是 Tcl，需要在 Xilinx 工具链（Vivado / Vitis / `xsct`）的命令行环境里运行。
- **分支与版本**：ADI HDL 每个发布分支绑定一组特定的 Vivado/Quartus 版本（见 u1-l3）。

本讲会引入的新术语：

| 术语 | 含义 |
|------|------|
| **BOOT.BIN** | 烧到 SD 卡启动分区的单一镜像文件，上电时由片上 BootROM 读取 |
| **FSBL** | First Stage Boot Loader，第一阶段引导加载程序，由 `.xsa` 经 Vitis 生成 |
| **PMUFW** | ZynqMP 平台管理单元（PMU）的固件，仅 ZynqMP 需要 |
| **`bl31.elf`** | ARM Trusted Firmware（ATF）的 BL31 阶段，仅 ZynqMP 需要 |
| **`.bif`** | Boot Image Format，描述 BOOT.BIN 由哪些分区镜像、按什么属性拼装的清单 |
| **`bootgen`** | AMD Xilinx 的镜像生成工具，吃 `.bif` 吐 `BOOT.BIN` |
| **`xsct`** | Xilinx Software Command-line Tool，Vitis 的命令行入口，可脚本化生成 FSBL |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [`projects/scripts/adi_make_boot_bin.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl) | **打包引擎**：校验输入 → 探测 CPU → 生成 FSBL → 写 `.bif` → 调 `bootgen` 出 `BOOT.BIN` |
| [`projects/scripts/adi_make.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl) | **封装层**：定义 `adi_make` 命名空间，提供 `lib`（建库）与 `boot_bin`（打包）两个过程 |
| [`docs/user_guide/build_boot_bin.rst`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/build_boot_bin.rst) | 官方「如何构建 BOOT.BIN」操作手册，**推荐**用从 wiki-scripts 下载的 bash 脚本 |
| [`docs/user_guide/releases.rst`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst) | 发布策略：分支与工具版本绑定表、预构建文件下载、版本移植注意事项 |
| [`README.md`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md) | 仓库顶层说明，含「该用哪个分支」「使用预构建文件」两条关键建议 |

> 一个重要事实：`build_boot_bin.rst` **推荐**的流程是从 `wiki-scripts` 仓库下载 bash 脚本（`build_boot_bin.sh` / `build_zynqmp_boot_bin.sh` / `build_versal_boot_bin.sh`）；而仓库内自带的 `adi_make_boot_bin.tcl` 是这套打包机制的 Tcl 引擎，也是 `adi_make.tcl` 在 Vivado 控制台里调用的底层。本讲以 Tcl 引擎为精读对象，因为它揭示了 `BOOT.BIN` 到底怎么拼出来。

## 4. 核心概念与源码讲解

### 4.1 BOOT.BIN 生成流程与 .bif 分区清单

#### 4.1.1 概念说明

把比特流烧进 FPGA 只是「让 PL 干活」，但一块 Zynq/ZynqMP 板子上电时，最先运行的是芯片内部固化的 **BootROM**。BootROM 不认识 `.xsa`，也不认识裸的 `.bit`，它只认一种格式：从启动介质（SD 卡 / QSPI）的固定偏移读取一个**单一镜像文件** `BOOT.BIN`，并按其中的分区表逐个加载。

`BOOT.BIN` 本质是一个**容器**，里面顺序塞着若干「分区镜像」：

- **FSBL**（First Stage Boot Loader）：BootROM 跳转到的第一段代码，负责初始化 DDR、加载 PL 比特流、再把后续镜像（U-Boot）搬进内存。
- **比特流分区**：即你的 HDL 工程（`system_top.bit`），FSBL 把它写进 PL。
- **U-Boot**（`u-boot.elf`）：第二阶段引导加载程序，负责加载 Linux 内核。
- **PMUFW**（`pmufw.elf`）与 **`bl31.elf`**：仅 ZynqMP 需要，分别是平台管理单元固件与 ARM Trusted Firmware。

为什么这些要打包成一个文件？因为 BootROM 只会从启动介质读**一个**文件。`.bif`（Boot Image Format）就是描述「这个 `BOOT.BIN` 由哪些 `.elf`/`.bit` 组成、各自属于哪个 CPU、处于什么异常级别」的清单，`bootgen` 工具读它来组装 `BOOT.BIN`。

#### 4.1.2 核心流程

生成 `BOOT.BIN` 的整体链路：

```text
system_top.xsa ──┐
                 ├──► [adi_make_boot_bin.tcl]
u-boot.elf ──────┤        │
bl31.elf(MP) ────┘        ├─ 1. 校验三个输入文件是否存在
                         ├─ 2. hsi 打开 xsa，正则探测 CPU 型号
                         │     (ps7_cortexa9 → Zynq ; psu_cortexa53 → ZynqMP)
                         ├─ 3. 按家族写 .bif（zynq.bif 或 zynqmp.bif）
                         ├─ 4. xsct 跑 fsbl_build.tcl，由 Vitis 生成 fsbl.elf
                         │     (ZynqMP 还会生成 pmufw.elf)
                         └─ 5. bootgen 读 .bif，组装 BOOT.BIN
```

两个家族的 `.bif` 内容不同，分区清单也不同：

- **Zynq（7 系列/Zynq-7000）** 三个分区：FSBL + 比特流 + U-Boot。
- **ZynqMP** 五个分区：PMUFW + FSBL + 比特流 + ATF（`bl31.elf`）+ U-Boot，且每个分区带 `destination_cpu` / `exception_level` 等属性。

> Versal（VCK190/VPK180）另有一套流程，本仓库的 Tcl 脚本未覆盖 Versal；`build_boot_bin.rst` 用专门的 `build_versal_boot_bin.sh` 处理。

#### 4.1.3 源码精读

脚本里写 `.bif` 的两段代码直接展示了分区清单。先看 Zynq（单核 A9）的 `.bif`：

[projects/scripts/adi_make_boot_bin.tcl:129-138](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L129-L138) —— 写出 `zynq.bif`，内含 `[bootloader] ./fsbl.elf`、`./system_top.bit`、`$uboot_file` 三个分区。`[bootloader]` 标记告诉 `bootgen`：这是 BootROM 要跳转的 FSBL。

再看 ZynqMP 的 `.bif`，分区更多且带属性：

[projects/scripts/adi_make_boot_bin.tcl:140-151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L140-L151) —— 写出 `zynqmp.bif`：`[pmufw_image] ./pmufw.elf`、`[bootloader,destination_cpu=a53-0] ./fsbl.elf`、`[destination_device=pl] ./system_top.bit`、`[destination_cpu=a53-0,exception_level=el-3,trustzone] $arm_tr_frm_file`、`[destination_cpu=a53-0,exception_level=el-2] $uboot_file`。

对比可见 ZynqMP 多了三件事：PMUFW 单独成一个分区；比特流标注 `destination_device=pl`（明确写进 PL 而非某 CPU）；`bl31.elf` 与 U-Boot 分别标注异常级别 EL-3 与 EL-2，体现 ARM 的安全启动层级。

#### 4.1.4 代码实践

**实践目标**：在不运行工具链的前提下，凭 `.bif` 内容反推一个 ZynqMP 工程的启动顺序。

**操作步骤**：

1. 打开 [adi_make_boot_bin.tcl:140-151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L140-L151)。
2. 列出 `zynqmp.bif` 的 5 个分区，按它们在文件里的出现顺序编号。
3. 给每个分区标注：它是给 **PS 的某个 CPU**、给 **PMU**、还是给 **PL**。

**需要观察的现象**：哪个分区带 `[bootloader]`？为什么 `bl31.elf` 的异常级别是 EL-3 而 U-Boot 是 EL-2？

**预期结果**：`[bootloader]` 只标在 `fsbl.elf` 上；EL-3（最高、可信）先于 EL-2 运行，ATF 负责在 EL-3 建立安全环境后再把控制权交给 EL-2 的 U-Boot。这是一条「BootROM → FSBL → ATF → U-Boot」的特权递降链。

#### 4.1.5 小练习与答案

**练习 1**：如果一台 Zynq-7000 板子的 `BOOT.BIN` 里漏掉了 `system_top.bit` 分区，会发生什么？
**答案**：FSBL 仍能启动，但 PL 不会被配置——FPGA 侧逻辑不工作，PS 侧的 AXI 外设（那些在 PL 里实现的 IP）将无法访问。板子能跑 U-Boot/Linux，但你的 HDL 设计没生效。

**练习 2**：为什么 Zynq 的 `.bif` 没有 `destination_cpu` 属性，而 ZynqMP 有？
**答案**：Zynq-7000 只有单一 A9 双核且启动模型简单，BootROM/FSBL 默认目标明确；ZynqMP 是多集群（A53 + R5 + PMU）异构 SoC，必须显式声明每个镜像归属哪个 CPU 与异常级别，否则 `bootgen` 无法正确编排。

---

### 4.2 adi_make_boot_bin.tcl：打包引擎的输入与依赖

#### 4.2.1 概念说明

`adi_make_boot_bin.tcl` 是一个**位置参数**驱动的批处理脚本，由 `xsct`（Vitis 命令行）执行。它的设计目标是：给定一个 `.xsa` 和 U-Boot，自动判断目标家族、自动生成 FSBL，最终产出 `BOOT.BIN`，把「手写 `.bif` + 手敲 `bootgen` 命令」的繁琐步骤封装成一行调用。

它的输入依赖可分为两类：

- **必填输入**：`system_top.xsa`（来自 HDL 构建）+ `u-boot.elf`（来自 Kuiper 镜像里的 `bootgen_sysfiles.tgz`）。
- **条件输入**：`bl31.elf`，仅当探测到 ZynqMP 时才需要。
- **环境依赖**：`xsct`（生成 FSBL）与 `bootgen`（组装镜像）都必须在 `$PATH` 中。

#### 4.2.2 核心流程

脚本内部的执行顺序（关键决策点）：

1. **解析 4 个位置参数**：`xsa`、`uboot`、`build_dir`、`bl31.elf`，缺失时回退默认值。
2. **校验**：`.xsa` 与 `u-boot.elf` 必须存在，否则报错退出。
3. **准备构建目录**：删除并重建 `build_dir`，把 `.xsa` 与 `u-boot.elf` 拷进去。
4. **探测 CPU**：用 `hsi open_hw_design` 打开 `.xsa`，正则匹配处理器名，分派到 `Zynq FSBL` 或 `Zynq MP FSBL`；MicroBlaze（`sys_mb`）与未知型号直接报错。
5. **写 `.bif`**：按家族生成 `zynq.bif` 或 `zynqmp.bif`（见 4.1.3）。
6. **生成 FSBL**：写一个临时 `fsbl_build.tcl`，用 `xsct` 跑它，由 Vitis `platform create` 产出 `fsbl.elf`（ZynqMP 还会产出 `pmufw.elf`）。
7. **组装**：`cd` 进构建目录，调 `bootgen` 读 `.bif` 生成 `BOOT.BIN`。

#### 4.2.3 源码精读

**(a) 位置参数解析与默认值回退**

[projects/scripts/adi_make_boot_bin.tcl:32-36](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L32-L36) —— 用 `lindex $argv N` 取出 4 个位置参数，顺序固定（脚本注释强调 *The order of script arguments is mandatory*）。

**(b) 输入校验**

[projects/scripts/adi_make_boot_bin.tcl:45-60](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L45-L60) —— `.xsa` 不存在则 `ERROR + return`；`u-boot.elf` 缺失时先尝试 `glob ./u-boot*.elf` 自动找，找不到则提示去查阅构建文档。注意 U-Boot **是 FPGA 专属的**（`build_boot_bin.rst` 反复强调），不能随便拿一个通用的来用。

**(c) CPU 型号探测——决定走哪条家族分支**

[projects/scripts/adi_make_boot_bin.tcl:91-109](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L91-L109) —— `hsi open_hw_design` 打开 `.xsa`，用 `hsi get_cells -filter {IP_TYPE==PROCESSOR}` 取出处理器实例，再正则匹配：

[projects/scripts/adi_make_boot_bin.tcl:98-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L98-L104) —— `psu_cortexa53..` → `Zynq MP FSBL`；`ps7_cortexa9..` → `Zynq FSBL`；`sys_mb`（MicroBlaze）直接报错退出，因为该脚本「is design for arm processors」。这一步是整个脚本的决策枢纽：它决定了写哪种 `.bif`、要不要 `bl31.elf`。

**(d) 生成 FSBL 的临时脚本**

[projects/scripts/adi_make_boot_bin.tcl:159-170](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L159-L170) —— 动态写出 `fsbl_build.tcl`，内容是 `platform create -name hw0 -hw system_top.xsa -os standalone -proc $cpu_name` + `platform generate`，再用 `[exec xsct fsbl_build.tcl]` 执行。这就是「FSBL 不是手写的，而是 Vitis 从 `.xsa` 自动生成」的实现。

**(e) 调用 bootgen 组装镜像**

[projects/scripts/adi_make_boot_bin.tcl:174-179](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L174-L179) —— Zynq 走 `bootgen -image zynq.bif -w -o i BOOT.BIN`；ZynqMP 走 `bootgen -image zynqmp.bif -arch zynqmp -o BOOT.BIN -w`，并先把 `pmufw.elf` 拷进当前目录。注意 ZynqMP 多了 `-arch zynqmp`，告诉 `bootgen` 按多分区异构格式组装。

#### 4.2.4 代码实践

**实践目标**：依据脚本逻辑，列出 ZynqMP 工程生成 `BOOT.BIN` 的完整输入清单与产出。

**操作步骤**：

1. 读 [adi_make_boot_bin.tcl:6-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L6-L23) 的 HELP 段，记录 4 个参数。
2. 读 [docs/user_guide/build_boot_bin.rst:104-109](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/build_boot_bin.rst#L104-L109)，确认 ZynqMP 流程的输入。
3. 画一张「输入文件 → 中间产物 → 最终产物」的依赖图。

**预期结果（依据源码，待本地验证实际运行）**：

| 输入 | 来源 | 用途 |
|------|------|------|
| `system_top.xsa` | HDL 工程 `make` 产出 | 探测 CPU + 生成 FSBL/PMUFW |
| `u-boot.elf` | Kuiper 镜像 `bootgen_sysfiles.tgz` | 第二阶段引导 |
| `bl31.elf` | Kuiper 镜像 / ATF 自编 | ZynqMP 的 EL-3 固件 |
| `xsct` / `bootgen` | Vitis 工具链 `$PATH` | 生成 FSBL / 组装镜像 |

中间产物：`fsbl.elf`、`pmufw.elf`（由 Vitis 生成）、`zynqmp.bif`（脚本写出）。最终产物：`BOOT.BIN`。

#### 4.2.5 小练习与答案

**练习 1**：脚本里 `app_type` 变量在 [L28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L28) 初始化为空，又在 [L79](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L79) 被判断为 `"Zynq MP FSBL"`。这一段（L78-86）实际会被执行吗？
**答案**：不会。因为此时 `app_type` 还没被 [L99](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L99) 的探测赋值，恒为空，`if` 条件不成立。真正的 ATF 处理在 [L112-121](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L112-L121)（探测之后）。L78-86 是一段无效的「死代码」，反映了脚本多年演进留下的痕迹——读源码时要识别这类冗余。

**练习 2**：为什么 `.xsa` 校验失败用 `return`（[L47](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L47)），而 `.bif` 写不出来用 `return -code error`（[L155](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L155)）？
**答案**：`return` 只是平静退出当前脚本；`return -code error` 会向上抛出一个可被 `catch` 捕获的错误。封装层 `adi_make::boot_bin` 正是用 `eval` 调用本脚本，错误语义的差异会影响上层是否把它当失败处理。

---

### 4.3 adi_make.tcl：Vivado 控制台的 boot_bin 封装

#### 4.3.1 概念说明

`adi_make_boot_bin.tcl` 需要你手动凑齐 4 个参数、确保 `xsct` 在路径里、再敲一长串命令。`adi_make.tcl` 把它包装成一个命名空间 `adi_make`，暴露两个过程：

- `adi_make::lib`：在 Vivado 控制台里建库（等价于 `make` 的库打包部分）。
- `adi_make::boot_bin`：在 Vivado 控制台里一键打包 `BOOT.BIN`，内部自动定位 `.xsa` 与 `u-boot.elf`，再调 `xsct adi_make_boot_bin.tcl`。

这套封装主要服务于**Vivado GUI 工作流**——`build_hdl.rst` 把它放在「Building the BOOT.BIN in Vivado GUI」折叠块里，并标注为非首选（推荐的是 Linux 终端 + bash 脚本）。理解它的价值在于：它展示了「封装层如何替你省去参数准备」。

#### 4.3.2 核心流程

`adi_make::boot_bin` 的内部步骤：

1. 用 `glob` 在工程目录下自动找 `./u-boot*.elf` 与（旧 SDK 流程遗留的）`./*.sdk/system_top.xsa`。
2. 探测操作系统，决定用 `which`（Linux）还是 `where`（Windows）定位 `xsct`。
3. 检查 `xsct` 是否在 `$PATH` 中，否则报错退出。
4. 拼出命令 `xsct <root>/projects/scripts/adi_make_boot_bin.tcl <xsa> <uboot> boot_bin bl31.elf`，用 `eval` 执行。

#### 4.3.3 源码精读

**(a) 自动定位输入文件**

[projects/scripts/adi_make.tcl:218-227](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L218-L227) —— 用 `glob "./*.sdk/system_top.xsa"` 与 `glob "./u-boot*.elf"` 自动匹配，省去手填路径；找不到则报错并指向构建文档。注意它找的是 `.sdk/` 子目录下的 `.xsa`，这是旧版 SDK 导出风格的残留路径约定。

**(b) 跨平台定位 xsct**

[projects/scripts/adi_make.tcl:233-251](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L233-L251) —— `package require platform` 取操作系统类型，Windows 用 `where`、Linux 用 `which` 找 `xsct`；找不到则打印 `$PATH` 并退出。这一段保证脚本在两个平台都能定位到 Vitis 命令行工具。

**(c) 拼装并调用底层引擎**

[projects/scripts/adi_make.tcl:253-256](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L253-L256) —— 把 `root_hdl_folder` 算出的脚本绝对路径、`.xsa`、`u-boot.elf`、固定的 `boot_bin` 输出目录名与 `bl31.elf` 拼成参数串，交给 `eval` 跑。`root_hdl_folder` 的来源见 [L32-39](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L32-L39)：从当前路径里用正则砍掉 `/projects` 之后的部分，还原出仓库根目录。

**(d) 配套的 lib 过程——递归建库**

[projects/scripts/adi_make.tcl:155-208](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L155-L208) —— `build_lib` 用 `done_list` 去重，解析库 Makefile 里的 `XILINX_*_DEPS` 递归建依赖，最后 `exec vivado -mode batch -source <lib>_ip.tcl`。它本质上是用 Tcl 重写了 `make` 的库打包逻辑（u3-l2、u4-l1 讲过的 `component.xml` 流程），供 GUI 用户在没有 `make` 时也能建库。

#### 4.3.4 代码实践

**实践目标**：对照 `build_hdl.rst` 的示例，复原「Vivado 控制台打包」的完整命令序列。

**操作步骤**：

1. 读 [docs/user_guide/build_hdl.rst:477-485](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/build_hdl.rst#L477-L485) 的示例代码块。
2. 把 4 行 Tcl 命令逐一对应到本节讲的过程。

**预期结果**：四行命令 `cd <工程目录>` → `source ../../scripts/adi_make.tcl`（加载命名空间）→ `adi_make::lib all`（先建所有依赖库）→ `source ./system_project.tcl`（建工程出 `.xsa`）→ `adi_make::boot_bin`（打包 `BOOT.BIN`）。注意顺序：**必须先有 `.xsa` 才能打包**，所以 `boot_bin` 永远在最后。

**说明**：本实践为源码阅读型，实际运行需 Vivado GUI + Vitis + 已构建工程，待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`adi_make::boot_bin` 为什么固定把输出目录命名为 `boot_bin`（[L217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L217)），而 `build_boot_bin.rst` 推荐的 bash 脚本输出到 `output_boot_bin`？
**答案**：两套是不同时期、不同入口的工具。Tcl 封装是仓库内的旧 GUI 流程，bash 脚本是 wiki-scripts 仓库维护的推荐 Linux 流程。命名差异正说明它们是并行存在的两条路径，而不是同一个工具的两面。

**练习 2**：`adi_make::lib` 的 `get_libraries`（[L55-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl#L55-L74)）解析的是工程 Makefile 的哪个字段？这与 u3-l2 学的什么概念对应？
**答案**：解析 `LIB_DEPS =`。这正对应 u3-l2 讲的「工程 Makefile 用 `LIB_DEPS` 报菜名，公共脚本把它翻译成 `component.xml` 打包目标」——只不过这里用 Tcl 而非 GNU Make 来驱动同一件事。

---

### 4.4 release 分支、版本对应与预构建文件策略

#### 4.4.1 概念说明

前面三节讲的是「自己构建 `BOOT.BIN`」。但 ADI 还提供一条捷径：**直接下载已构建好的启动分区文件**。要安全地使用这些预构建文件，必须先理解仓库的发布模型：

- ADI HDL **每半年**发布一个 release 分支（如 `hdl_2026_r1`、`hdl_2023_r2`）。
- **分支存在 ≠ 已通过测试**：分支先创建、再测试，只有「正式发布」后才算稳定。
- 每个 release 分支**绑定一组固定的工具版本**（Vivado / Quartus），且只在这些版本上做过硬件验证。
- 分支里可能包含仍在开发中的工程，这些**默认视为未测试、不支持**。

#### 4.4.2 核心流程

选择与使用策略的决策树：

```text
我要上板验证一块 ADI 板子
        │
        ├─ 不想自己构建? ──► 下载预构建文件
        │     ├─ 追求稳定 ──► 用最新 release 分支的预构建文件（硬件已测）
        │     └─ 追求最新 ──► 用 main 分支预构建文件（⚠ 未做硬件测试）
        │
        └─ 想自己构建 ──► checkout 对应分支
              ├─ 查 adi_env.tcl 确认工具版本
              ├─ 装匹配的 Vivado/Quartus
              └─ make → adi_make_boot_bin.tcl → BOOT.BIN
```

版本绑定的命名规律：release 分支 `hdl_YYYY_rN` 对应 Vivado `YYYY.x`（如 `hdl_2023_r2` ↔ Vivado 2023.2），工具版本在 [`scripts/adi_env.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl) 里集中声明（u1-l3 已讲）。

#### 4.4.3 源码精读

**(a) 发布模型的核心约束**

[docs/user_guide/releases.rst:12-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L12-L23) —— 「半年一发」「分支存在不代表已测试」「只在特定工具版本上测试」「分支内未列出的工程视为不支持」。这四条是使用任何预构建文件前必须接受的前提。

**(b) main 分支预构建文件的警告**

[docs/user_guide/releases.rst:63-80](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L63-L80) —— main 的预构建文件在 HDL 或 Linux 仓库每次有新提交时就重新构建，方便尝鲜；但警告框明确写着 *they are not tested in hardware!*，并建议改用最新 release 分支。

**(c) 版本绑定表**

[docs/user_guide/releases.rst:95-106](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L95-L106) —— main 行对应 Vivado `2025.1` / Quartus Pro `25.1.0`；而正式 release `hdl_2026_r1` 同样对应这两个版本（说明 main 已对齐 2026_R1 周期）。早期分支则各自绑定旧版本，如 `hdl_2023_r2` ↔ Vivado 2023.2。

**(d) 跨版本移植的两条注意**

[docs/user_guide/releases.rst:34-50](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L34-L50) —— 若硬要把某分支跑在不匹配的工具版本上：第一，用 `export ADI_IGNORE_VERSION_CHECK=1` 关掉版本硬拦截（u1-l3 讲过）；第二，IP 核版本号需手动改（Intel 由 Quartus 自动升，但 Vivado 的 Tcl 流程不会自动升，常需先用支持版本建工程、再用新版本打开升级、再回填 Tcl）。

**(e) 仓库顶层同样强调的两条建议**

[README.md:122-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L122-L146) —— 「要稳定用最新 release 分支，要尝鲜用 main」；预构建文件 main 分支的不保证稳定，带 ⚠ 警告「未做硬件测试」。

#### 4.4.4 代码实践

**实践目标**：解释「为什么官方推荐用最新 release 分支而非 main 的预构建文件」。

**操作步骤**：

1. 读 [releases.rst:12-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L12-L23) 与 [releases.rst:77-80](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L77-L80) 的警告。
2. 读 [README.md:134-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/README.md#L134-L146) 的「Use already built files」段。
3. 用两到三句话写出推荐 release 分支的理由。

**预期结果**：main 的预构建文件随每次提交自动重建，**未经硬件验证**，可能含未测工程或不稳定改动；release 分支的预构建文件**经过硬件测试**，且绑定经过验证的工具版本，稳定性有保障。因此除非必须追最新功能，否则应优先用最新 release 分支。

#### 4.4.5 小练习与答案

**练习 1**：你 checkout 了 `hdl_2023_r2` 分支，但本机只有 Vivado 2024.1。直接 `make` 会怎样？怎么办？
**答案**：构建脚本的版本校验（u1-l3 讲的 `REQUIRED_VIVADO_VERSION` 拦截）会报错并 `exit 2`。要么装 Vivado 2023.2，要么设 `export ADI_IGNORE_VERSION_CHECK=1` 绕过——但绕过后官方不保证成功，且按 releases.rst 的说明，Vivado Tcl 流程不会自动升级 IP 核版本，可能需要手动处理。

**练习 2**：为什么 release 分支里某个工程在「支持列表」之外，就被视为不支持？
**答案**：见 [releases.rst:20-23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L20-L23)：分支可能包含仍在开发中的工程，这些未经过该 release 的硬件测试。支持列表（`:ref: downloads_insert_*`）才是该 release 真正验证过的工程集合。

---

## 5. 综合实践

**任务**：为 `projects/fmcomms2/zcu102`（ZynqMP 工程）规划一次完整的「从源码到上电」流程，产出一张端到端流程图与一份输入清单。

**要求**：

1. **构建阶段**（承接 u1-l4、u3-l2）：写出 `cd projects/fmcomms2/zcu102 && make` 后会得到哪个硬件交付文件，它对应 [adi_make_boot_bin.tcl:33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L33) 的哪个参数。
2. **打包阶段**：依据 [adi_make_boot_bin.tcl:91-109](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L91-L109) 判断该工程会被识别为哪种 `app_type`，并据此说明它会走 [L140-151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl#L140-L151) 的哪条 `.bif` 分支、需要哪些输入文件。
3. **发布阶段**：依据 [releases.rst:95-106](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L95-L106) 说明，若你不想自己打包，应从哪个分支下载 `fmcomms2/zcu102` 的预构建文件最稳妥，并说明理由。

**参考答案要点**：

- 构建产出 `system_top.xsa`，对应脚本的 `xsa_file`（第 1 个位置参数）。
- zcu102 是 ZynqMP，处理器为 `psu_cortexa53`，识别为 `Zynq MP FSBL`，走 `zynqmp.bif` 分支，需要 `u-boot.elf` + `bl31.elf`，并由 Vitis 额外生成 `pmufw.elf` 与 `fsbl.elf`。
- 预构建文件应优先取**最新 release 分支**（与 main 当前同周期的是 `hdl_2026_r1`）对应的 `BOOT partition files` 链接，因为 release 分支的文件做过硬件测试，而 main 的预构建文件未经硬件验证（[releases.rst:77-80](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/user_guide/releases.rst#L77-L80)）。

## 6. 本讲小结

- `BOOT.BIN` 是 SD 卡上供片上 BootROM 读取的**单一启动镜像容器**，由 FSBL、比特流、U-Boot（ZynqMP 还加 PMUFW 与 `bl31.elf`）等分区拼装而成，分区清单写在 `.bif` 里，由 `bootgen` 组装。
- [`adi_make_boot_bin.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make_boot_bin.tcl) 是打包引擎：吃 `.xsa` + `u-boot.elf`（+ ZynqMP 的 `bl31.elf`），用 `hsi` 探测 CPU 型号分派家族，用 `xsct`/Vitis 自动生成 FSBL，写 `.bif`，最后调 `bootgen` 出 `BOOT.BIN`。
- [`adi_make.tcl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_make.tcl) 是封装层，`adi_make::boot_bin` 自动定位输入文件、跨平台找 `xsct`，再调用底层引擎，主要服务于 Vivado 控制台工作流；官方推荐的主流路径则是 `build_boot_bin.rst` 里的 bash 脚本。
- ADI HDL 每半年发布一个 release 分支，每个分支绑定固定的 Vivado/Quartus 版本，且只在支持列表内的工程做过硬件测试。
- **官方推荐用最新 release 分支**（自己构建或下预构建文件），main 分支的预构建文件虽新但未经硬件测试，使用需自担风险。
- 跨版本移植需 `ADI_IGNORE_VERSION_CHECK=1` 绕过版本校验，并手动处理 IP 核版本升级，官方不予保证。

## 7. 下一步学习建议

本讲是学习手册最后一篇，到这里你已经走完「项目总览 → 目录结构 → 构建系统 → IP 库 → 数据通路 → JESD204/SPI Engine → 移植与定制 → 测试规范」的完整闭环。后续建议：

1. **动手闭环**：挑一块你手头有的 ADI 评估板，按本讲流程真正跑一次 `make` → 打包 `BOOT.BIN` → 烧卡上电，把全手册的知识在硬件上验证一遍。
2. **深入软件侧**：`BOOT.BIN` 只是起点，启动后真正驱动 IP 的是 no-OS（裸机）或 Linux 仓库——它们经 u4-l5 讲的 AXI 寄存器映射与你构建的硬件对接，建议结合 [no-OS](https://github.com/analogdevicesinc/no-OS) 与 [Linux](https://github.com/analogdevicesinc/linux) 仓库继续学习。
3. **关注发布动态**：订阅 [hdl releases](https://github.com/analogdevicesinc/hdl/releases)，跟踪半年一次的 release 节奏与工具版本演进，保持工程与官方支持版本对齐。
4. **回读源码**：若你想彻底掌握启动链，可对照本讲再读一遍 `adi_make_boot_bin.tcl`，并尝试用 `bootgen` 的官方手册理解每个 `.bif` 属性（`destination_cpu`、`exception_level`、`trustzone`）的硬件含义。
