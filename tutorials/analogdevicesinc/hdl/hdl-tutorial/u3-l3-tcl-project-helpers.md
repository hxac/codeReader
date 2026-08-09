# Tcl 工程助手：adi_project_xilinx.tcl

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `adi_project` / `adi_project_create` / `adi_project_files` / `adi_project_run` 这四个 Tcl 过程各自的职责，以及它们的**调用顺序**。
- 理解 `adi_project` 如何仅凭「工程名」就推断出目标 FPGA 器件型号和板卡 BSP。
- 看懂 `adi_project_create` 内部如何把 Vivado 一连串琐碎命令（建工程、设板卡、搭块设计、生成 wrapper）封装成一次调用。
- 解释 `adi_project_run` 如何驱动综合、实现、写比特流并产出 `.xsa`，以及它如何处理「时序不达标」这一现实情况。
- 能够对照一个真实工程 `system_project.tcl`，准确指出 `adi_project_run` 之前必须先完成哪些前置调用。

## 2. 前置知识

本讲假设你已经学过：

- **u1-l4 / u3-l2**：知道工程目录里的 `make` 最终会执行 `vivado -mode batch -source system_project.tcl`，而 `system_project.tcl` 就是 Vivado 的 Tcl 入口脚本。
- **u2-l2**：知道一个 Xilinx 工程的「标准五件套」（`Makefile`、`system_project.tcl`、`system_bd.tcl`、`system_constr.xdc`、`system_top.v`），以及 `system_top.v` 会例化工具自动生成的 `system_wrapper`。
- **u1-l3**：知道 `scripts/adi_env.tcl` 是工具版本与环境的「唯一事实来源」，其中定义了 `required_vivado_version`、`IGNORE_VERSION_CHECK` 等全局变量。

几个本讲会用到的 Tcl 概念，先用一句话解释：

- **过程（proc）**：Tcl 里用 `proc 名字 {参数} { 函数体 }` 定义的函数。本讲的 `adi_project` 等都是 `proc`。
- **全局变量（global）**：过程内用 `global 变量名` 声明后，就能读写过程外定义的变量。`adi_env.tcl` 设置的 `ad_hdl_dir`、`required_vivado_version` 就是通过 `global` 在各过程间共享的。
- **正则匹配（regexp）**：`regexp "_zcu102" $project_name` 用来判断工程名字符串里是否含有 `_zcu102`，这是本讲「按名字猜器件」的核心手法。

本讲不要求你写过 Vivado Tcl，但会经常把「ADI 的助手过程」与「等价的原始 Vivado 命令」对照，让你体会封装带来的简化。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [projects/scripts/adi_project_xilinx.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl) | **本讲主角**。定义 `adi_project` / `adi_project_create` / `adi_project_files` / `adi_project_run` 等过程，封装 Xilinx 工程的「创建→加文件→综合实现→出比特流」全流程。 |
| [projects/fmcomms2/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl) | 一个真实的工程入口脚本，示范了上述四个过程「该按什么顺序、传什么参数」被调用。 |
| [scripts/adi_env.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl) | 提供 `ad_hdl_dir`（仓库根）、`required_vivado_version`（要求版本）、`IGNORE_VERSION_CHECK`（是否跳过版本拦截）等全局变量，被工程助手过程读取。 |

另外有两处被引用但本讲不展开的脚本：`adi_board.tcl`（块设计连线原语，见 u3-l4）、`auto_timing_fix_xilinx.tcl`（布线后时序自动修复脚本，见 u8-l3）。它们在本讲里只是「被装进流程的零件」。

---

## 4. 核心概念与源码讲解

### 4.1 adi_project / adi_project_create：从工程名到建好块设计

#### 4.1.1 概念说明

Vivado 原生建一个工程，你需要手工写出器件型号（如 `xczu9eg-ffvb1156-2-e`）、板卡 BSP 名、IP 仓库路径、然后创建块设计、source 块设计脚本、保存校验、生成 wrapper……这一长串命令既繁琐又容易写错。

ADI 的做法是：**用「工程名」当唯一输入**。工程目录里的脚本调用 `adi_project fmcomms2_zcu102`，这个过程会从名字里读出载板关键字 `zcu102`，自动查表得到对应的器件型号与板卡 BSP，再委派给真正干活的 `adi_project_create`。`adi_project_create` 一次性完成「建工程 + 配置 + 搭块设计 + 生成 wrapper」，其中**搭块设计**这一步就是 `source system_bd.tcl`（即 u2-l1 讲的三层架构入口）。

一句话区分两者：

- `adi_project`：**翻译层**。把「人类友好的工程名」翻译成「Vivado 需要的器件串 + 板卡名」，是给工程脚本调用的便捷入口。
- `adi_project_create`：**执行层**。接收已确定好的器件串，真正去调用 `create_project`、`create_bd_design` 等命令。

#### 4.1.2 核心流程

`adi_project` 的执行过程：

```text
adi_project fmcomms2_zcu102  (mode=0, parameter_list={})
   │
   ├─ device=""  board=""           # 先清空
   ├─ 用 regexp 依次匹配工程名后缀   # _ac701 / _zcu102 / _vcu118 ...
   │    命中 "_zcu102"：
   │       device ← "xczu9eg-ffvb1156-2-e"
   │       board  ← get_board_parts 里 *zcu102* 的最后一条
   │
   └─ adi_project_create fmcomms2_zcu102 0 {}  $device $board
```

`adi_project_create` 内部的关键步骤（按代码顺序）：

```text
adi_project_create
   ├─ 解析实际工程名（受 ADI_PROJECT_DIR 环境变量影响）
   ├─ 记录 p_device / p_board 全局变量
   ├─ 据 device 前缀判定 sys_zynq（0/1/2/3）与 use_smartconnect
   ├─ ★ 版本校验：对比 Vivado 实际版本 vs required_vivado_version
   ├─ create_project（工程模式）或 create_project -in_memory（非工程模式）
   ├─ set_property board_part
   ├─ 配置 IP 仓库路径 + update_ip_catalog
   ├─ 注入 parameter_list 为综合 generic 参数
   ├─ create_bd_design "system"
   ├─ source system_bd.tcl          ← 块设计真正在这里被搭建
   ├─ save_bd_design / validate_bd_design
   ├─ generate_target + make_wrapper  ← 生成 system_wrapper.v
   ├─ import_files system_wrapper.v
   └─ （若增量编译）挂载 reference.dcp
```

注意一个**容易误解的点**：块设计（Block Design）是在 `adi_project_create`（也就是 `adi_project` 调用）阶段就被搭建好的，**不是**在 `adi_project_run` 阶段。`adi_project_run` 只负责跑综合和实现。

关于 `sys_zynq` 的取值规则（这是一张「器件家族 → 整数标记」的查表，后续脚本用它判断是否含 ARM 处理器系统）：

| 器件前缀 | 家族 | sys_zynq |
| --- | --- | --- |
| `xc7z` | Zynq-7000 | 1 |
| `xck26` / `xczu` | Zynq UltraScale+ / Kria | 2 |
| `xcv[ecmph]` | Versal | 3 |
| 其它（如 `xc7a`、`xcku`、`xcvu`） | 纯 FPGA（无 PS） | 0 |

#### 4.1.3 源码精读

**`adi_project`：靠名字猜器件。** 它的核心就是一连串 `if [regexp "_xxx" $project_name]` 分支，每命中一个载板就填好 `device` 与 `board`：

[adi_project_xilinx.tcl:65-145](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L65-L145) —— 整个 `adi_project` 过程；末尾把猜好的 `device`、`board` 转交给 `adi_project_create`。

挑出 `zcu102` 这一支看（这是本讲实例工程对应的分支）：

```tcl
if [regexp "_zcu102" $project_name] {
  set device "xczu9eg-ffvb1156-2-e"
  set board [lindex [lsearch -all -inline [get_board_parts] *zcu102*] end]
}
```

`get_board_parts` 会列出 Vivado 里所有已安装的板卡 BSP，`lsearch -all -inline ... *zcu102*` 过滤出含 `zcu102` 的，`lindex ... end` 取最后一条（通常是版本号最新的）。注意部分载板（如 `coraz7s`、`microzed`、`mitx045`）没有官方 BSP，被显式设成 `board "not-applicable"`。

**`adi_project_create`：真正建工程、搭块设计。** 它的签名告诉我们接收哪些参数：

[adi_project_xilinx.tcl:158-322](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L158-L322) —— 整个 `adi_project_create` 过程，签名 `proc adi_project_create {project_name mode parameter_list device {board "not-applicable"}}`。

其中**版本校验**这段，正是 u1-l3 里说的「真正的版本比对与拦截在 `adi_project_*.tcl` 中执行」。它读取 `adi_env.tcl` 提供的两个全局变量：

[adi_project_xilinx.tcl:202-217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L202-L217) —— 用 `string compare` 比对 `$VIVADO_VERSION`（由 `version -short` 取得）与 `$required_vivado_version`；不匹配时，`IGNORE_VERSION_CHECK` 为真则降级为 `CRITICAL WARNING`，否则 `ERROR` 并 `exit 2`。

而**搭建块设计**就两行，但分量极重——`source system_bd.tcl`：

[adi_project_xilinx.tcl:292-308](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L292-L308) —— 先 `create_bd_design "system"`，再 `source system_bd.tcl`（u2-l1 讲的载板+评估板两层脚本就在这里被依次 source），随后 `save_bd_design` / `validate_bd_design` / `generate_target` / `make_wrapper`，最终把工具生成的 `system_wrapper.v` 导入工程。

`make_wrapper ... -top` 这一步，正是 u2-l2 里 `system_top.v` 例化的那个 `system_wrapper` 的来源——它由工具从块设计自动生成，不是手写的。

#### 4.1.4 代码实践

**实践目标**：定位 `adi_project_create` 的定义，列出它接收的关键参数及其含义。

**操作步骤**：

1. 打开 [projects/scripts/adi_project_xilinx.tcl:158](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L158)，找到 `proc adi_project_create` 的签名行。
2. 把签名里的形参逐个列出来，区分「必填」和「带默认值的可选」参数（Tcl 里 `{board "not-applicable"}` 表示可选、默认值 `not-applicable`）。
3. 在过程体里搜索每个形参第一次被使用的位置，推断它的用途。

**需要观察的现象 / 预期结果**：你应该得到下面这张表（可对照确认）：

| 形参 | 必填 | 含义 |
| --- | --- | --- |
| `project_name` | 是 | 工程名，如 `fmcomms2_zcu102`；同时用作工程目录名 |
| `mode` | 是 | 0=工程模式（`create_project`），非 0=非工程模式（`create_project -in_memory`） |
| `parameter_list` | 是 | 顶层 `system_top` 的参数列表，会被注入为综合 `generic` |
| `device` | 是 | 规整的 Xilinx 器件串，如 `xczu9eg-ffvb1156-2-e` |
| `board` | 否 | 板卡 BSP 名，默认 `not-applicable`（无官方 BSP 时用） |

**关于运行结果**：本实践是源码阅读型，无需运行 Vivado，结论可直接从源码得出。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接让工程脚本调用 `adi_project_create`，而要先经过 `adi_project` 这一层？

**参考答案**：因为工程脚本里只有「载板名」（工程名的一部分），并不知道 `xczu9eg-ffvb1156-2-e` 这种规整器件串，也不确定板卡 BSP 是否安装。`adi_project` 负责「按名字查表 + 用 `get_board_parts` 动态探测 BSP」，把人类友好的名字翻译成 Vivado 需要的器件串与板卡名，屏蔽了这些细节。

**练习 2**：`adi_project_create` 里 `sys_zynq` 被设成 `2` 对应哪类器件？它会在后面影响什么？

**参考答案**：`xck26` 或 `xczu` 前缀（ZynqMP / Kria）时 `sys_zynq=2`。它表示含 ARM 处理器系统的 ZynqMP 家族；后续 `adi_board.tcl` 会据此选择走 HPC（高带宽）端口而不是 HP 口，`adi_project_run` 里也会用到它（例如 Versal 家族 `sys_zynq==3` 时跳过 `.bin` 生成）。

---

### 4.2 adi_project_files / adi_project_run：加文件与出比特流

#### 4.2.1 概念说明

块设计建好后，工程还缺两样东西：**顶层 RTL 与约束文件**（`system_top.v`、`system_constr.xdc` 等），以及**跑综合/实现**。这两个过程分别由 `adi_project_files` 和 `adi_project_run` 负责。

- `adi_project_files`：把一组文件按后缀自动分流——`.xdc` 进约束文件集 `constrs_1`，其它进源文件集 `sources_1`；同时把（可选的）布线后钩子脚本装进 `utils_1`；最后把综合顶层**硬编码**为 `system_top`。这就是为什么每个工程的顶层文件都必须叫 `system_top.v`。
- `adi_project_run`：驱动 `synth_1`（综合）与 `impl_1`（实现）两条 Vivado run，实现阶段一口气跑到 `write_bitstream`，并最终写出硬件交付文件 `.xsa`。它还内置了一套「时序是否达标」的判断：达标写 `system_top.xsa`，不达标写 `system_top_bad_timing.xsa` 并报错退出。

#### 4.2.2 核心流程

`adi_project_files` 的执行过程：

```text
adi_project_files fmcomms2_zcu102 {system_top.v  system_constr.xdc  ad_iobuf.v  zcu102_system_constr.xdc}
   ├─ foreach 文件：
   │     后缀==xdc → add_files -fileset constrs_1   # 约束
   │     否则      → add_files -fileset sources_1   # 源码
   ├─ 若存在 ADI_POST_ROUTE_POD_PRE_SCRIPT → 加入 utils_1
   ├─ 若存在 ADI_POST_ROUTE_SCRIPT         → 加入 utils_1   # auto_timing_fix_xilinx.tcl
   └─ set_property top system_top           # 顶层硬编码
```

`adi_project_run` 的核心执行过程（省略可选报告生成）：

```text
adi_project_run fmcomms2_zcu102
   ├─ （可选）设 maxThreads
   ├─ （可选）ADI_SKIP_SYNTHESIS → 直接 return
   ├─ launch_runs synth_1 (+ OOC 子综合) → wait_on_run → open_run
   ├─ 设比特流压缩 BITSTREAM.GENERAL.COMPRESS
   ├─ 挂载 POST_ROUTE 脚本到 impl_1 的 TCL.POST
   ├─ launch_runs impl_1 -to_step write_bitstream → wait_on_run → open_run
   ├─ report_timing_summary → 写 timing_impl.log
   ├─ 检查 no_clock（无时钟寄存器）
   ├─ 判定时序：
   │     达标   → write_hw_platform ... system_top.xsa
   │     违例   → write_hw_platform ... system_top_bad_timing.xsa + return -code error
   └─ （可选）ADI_GENERATE_BIN → 额外写 .bin
```

#### 4.2.3 源码精读

**文件分流。** 这一小段把 `.xdc` 与其它文件分别送进不同的 fileset：

[adi_project_xilinx.tcl:329-350](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L329-L350) —— `adi_project_files` 全过程。关键点：用 `string last .` 取出后缀判断；结尾 `set_property top system_top [current_fileset]`，注释明确写「top file name is always system_top」。

**布线后钩子的「装填」。** 注意它读的是 `ADI_POST_ROUTE_SCRIPT` 这个全局变量——这个变量是在工程脚本 `system_project.tcl` 里设置的（见 4.3.3），指向 `auto_timing_fix_xilinx.tcl`：

[adi_project_xilinx.tcl:341-346](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L341-L346) —— 把布线后脚本加进 `utils_1` 文件集，**此处只是登记，真正挂到 impl_1 的 `TCL.POST` 是在 `adi_project_run` 里完成的**。

**跑综合。** `ADI_USE_OOC_SYNTHESIS` 开启时，块设计里每个 IP 会先各自单独综合（Out-Of-Context），并行度由 `ADI_MAX_OOC_JOBS` 控制：

[adi_project_xilinx.tcl:386-393](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L386-L393) —— OOC 模式下 `launch_runs -jobs $ADI_MAX_OOC_JOBS system_*_synth_1 synth_1`，否则只 `launch_runs synth_1`；之后 `open_run synth_1` 并把时序摘要写到 `timing_synth.log`。

**挂钩子到 impl_1 并跑实现。** 这里把上一步登记的 `ADI_POST_ROUTE_SCRIPT` 用 `get_files` 取出，挂到 `impl_1` 的 `STEPS.ROUTE_DESIGN.TCL.POST` 属性上，然后一口气跑到 `write_bitstream`：

[adi_project_xilinx.tcl:406-417](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L406-L417) —— 把布线后脚本挂到 `ROUTE_DESIGN.TCL.POST`，再 `launch_runs impl_1 -to_step write_bitstream`。

**按是否达标写不同的 xsa。** 这是 ADI 对「时序失败」的工程化处理——违例也照样产出一份带 `_bad_timing` 后缀的 xsa 供调试，但脚本仍以 `return -code error` 报错，让构建流程感知失败：

[adi_project_xilinx.tcl:588-611](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L588-L611) —— 字符串匹配 `*VIOLATED*` / `*Timing constraints are not met*` 判定；达标写 `system_top.xsa`，违例写 `system_top_bad_timing.xsa` 并 `return -code error`。

#### 4.2.4 代码实践

**实践目标**：理解 `adi_project_files` 的文件分流逻辑，并验证「顶层硬编码为 system_top」这一约束的来源。

**操作步骤**：

1. 打开 [adi_project_xilinx.tcl:333-339](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L333-L339)，阅读 foreach 循环里 `string range` 取后缀的写法。
2. 在 `system_project.tcl`（见 4.3.3）里找到 `adi_project_files` 的调用，数一数传入的 4 个文件各有几个 `.xdc`、几个 `.v`，预测它们分别进哪个 fileset。
3. 找到第 349 行 `set_property top system_top`，回答：如果你把工程的顶层 Verilog 改名为 `my_top.v`，这里会发生什么？

**需要观察的现象 / 预期结果**：`fmcomms2/zcu102` 传入的 4 个文件里，`system_constr.xdc` 与 `zcu102_system_constr.xdc` 两个 `.xdc` 进 `constrs_1`；`system_top.v` 与 `ad_iobuf.v` 两个 `.v` 进 `sources_1`。若顶层文件改名而不改这一行，Vivado 会找不到名为 `system_top`的顶层模块而报错——这也是为什么全仓所有工程的顶层都统一叫 `system_top`。

**关于运行结果**：源码阅读型，结论可由阅读直接得出，无需运行 Vivado（实际运行 Vivado 属于硬件构建，耗时较长）。

#### 4.2.5 小练习与答案

**练习 1**：`adi_project_run` 在时序违例时，为什么还要写一个 `system_top_bad_timing.xsa` 再报错，而不是直接什么都不写就退出？

**参考答案**：为了让开发者拿到一份「虽然时序没过、但布局布线已完成」的工程产物用于调试（例如在 Vivado 里打开看哪些路径违例、调整约束后重跑）。直接退出会丢掉这次布线结果，浪费时间。写完调试产物后再 `return -code error`，则保证 CI/Make 流程仍能感知这次构建是失败的。

**练习 2**：`ADI_USE_OOC_SYNTHESIS`（OOC 综合）开启与关闭，对 `adi_project_run` 里的综合命令有什么具体影响？

**参考答案**：开启时调用 `launch_runs -jobs $ADI_MAX_OOC_JOBS system_*_synth_1 synth_1`，即先把块设计里各 IP 的 `*_synth_1` 子综合并行跑起来（最多 `ADI_MAX_OOC_JOBS` 个并发），再跑顶层 `synth_1`；关闭时只 `launch_runs synth_1`，所有逻辑一起综合。OOC 的好处是 IP 综合结果可被 IP 缓存复用，加速增量构建。

---

### 4.3 工程创建与运行的封装流程：把三步串起来

#### 4.3.1 概念说明

前面两节分别讲了「建工程+搭块设计」和「加文件+出比特流」。本节把它们串成一条完整的流水线，并对照真实工程脚本 `system_project.tcl`，让你看清**整条流水线的入口、顺序与每一步依赖什么前置条件**。

核心直觉：ADI 把 Vivado 一次批处理构建拆成了**三行核心调用**——

```tcl
adi_project        <名字>            ;# 建工程 + 搭块设计 + 生成 wrapper
adi_project_files  <名字> <文件列表> ;# 加顶层 RTL/约束 + 装填布线后钩子
adi_project_run    <名字>            ;# 综合 + 实现 + 写 xsa
```

这三行之外，工程脚本还要负责几件「准备工作」：source 三个脚本（`adi_env.tcl` 提供环境、`adi_project_xilinx.tcl` 提供助手、`adi_board.tcl` 提供连线原语）、设置布线后脚本路径、设置载板名。这些准备 + 三行核心调用，就构成了一个完整的 `system_project.tcl`。

#### 4.3.2 核心流程

完整时序（以 `fmcomms2/zcu102` 为例）：

```text
vivado -mode batch -source system_project.tcl          ← make 最终调用
   │
   │  ========== 准备阶段 ==========
   ├─ source ../../../scripts/adi_env.tcl               ← 得到 ad_hdl_dir、required_vivado_version、IGNORE_VERSION_CHECK
   ├─ source adi_project_xilinx.tcl                     ← 定义 adi_project 等过程
   ├─ source adi_board.tcl                              ← 定义 ad_connect / ad_cpu_interconnect 等（u3-l4）
   ├─ set ADI_POST_ROUTE_SCRIPT auto_timing_fix_xilinx.tcl   ← 装填布线后钩子（供 adi_project_files 读）
   ├─ set BOARD_NAME zcu102
   │
   │  ========== 三步核心 ==========
   ├─ ① adi_project fmcomms2_zcu102
   │       └─ 内部 source system_bd.tcl  → 块设计搭建完成（u2-l1 三层架构）
   │
   ├─ （可选）set_property strategy Congestion_SpreadLogic_high impl_1   ← 工程特化微调
   │
   ├─ ② adi_project_files fmcomms2_zcu102 {system_top.v  system_constr.xdc  ad_iobuf.v  zcu102_system_constr.xdc}
   │       └─ 顶层 RTL/约束入工程；布线后脚本登记进 utils_1
   │
   └─ ③ adi_project_run fmcomms2_zcu102
           └─ synth_1 → impl_1(-to_step write_bitstream) → 写 system_top.xsa
   │
   │  ========== 收尾 ==========
   └─ source axi_ad9361_delay.tcl                       ← 器件 delay 校准（u5-l2）
```

这个时序回答了本节的关键问题：**`adi_project_run` 之前必须先完成哪些前置调用？** 答案是——

1. 三个 `source`（准备环境与过程定义）；
2. 设置 `ADI_POST_ROUTE_SCRIPT`（否则 `adi_project_files` 不会登记布线后脚本）；
3. `adi_project`（建工程、搭块设计、生成 `system_wrapper.v`）；
4. `adi_project_files`（把 `system_top.v` 与约束加进工程，并设置顶层为 `system_top`）。

只有这些都完成，`adi_project_run` 才能在一个「工程已建、源码已全、顶层已定」的状态下启动综合。

#### 4.3.3 源码精读

下面是本讲实例工程 `fmcomms2/zcu102` 的入口脚本**全文**（仅 25 行），它就是上面时序图的真实来源：

[system_project.tcl:6-24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L6-L24) —— 准备阶段（三个 source + 设 `ADI_POST_ROUTE_SCRIPT`）、三步核心调用（`adi_project` → `adi_project_files` → `adi_project_run`）、收尾（`source axi_ad9361_delay.tcl`）。

逐段对照：

- 第 6 行 `source ../../../scripts/adi_env.tcl`：`../../../` 是因为工程目录在 `projects/fmcomms2/zcu102/`，相对回退三级到仓库根。这一行执行后，`ad_hdl_dir`、`required_vivado_version`、`IGNORE_VERSION_CHECK` 等全局变量就可用，后续脚本才能用 `$ad_hdl_dir/projects/scripts/...` 定位其它脚本。
- 第 9 行 `set ADI_POST_ROUTE_SCRIPT .../auto_timing_fix_xilinx.tcl`：这正是 4.2.3 里 `adi_project_files` 要读取的那个全局变量。注意它必须在 `adi_project_files`（第 13 行）**之前**设置，否则 `adi_project_files` 里 `info exists ADI_POST_ROUTE_SCRIPT` 为假，布线后脚本就不会被登记。
- 第 12 行 `adi_project fmcomms2_zcu102`：工程名里的 `zcu102` 被 `adi_project` 的 regexp 命中，自动选器件 `xczu9eg-ffvb1156-2-e`，并在内部 `source system_bd.tcl` 搭好块设计。
- 第 13–17 行 `adi_project_files`：传入 4 个文件，其中两个 `.xdc` 进约束集、两个 `.v` 进源码集。
- 第 21 行 `set_property strategy Congestion_SpreadLogic_high [get_runs impl_1]`：工程特化的策略微调（注释说明 fmcomms2 在某些路径有 hold time 违例，用扩散逻辑策略缓解）——它夹在 `adi_project_files` 与 `adi_project_run` 之间，是工程脚本对实现的局部干预。
- 第 23 行 `adi_project_run fmcomms2_zcu102`：综合 + 实现出 `.xsa`。

再看 `adi_env.tcl` 提供给上述流程的三个关键全局变量：

[adi_env.tcl:7-13](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L7-L13) —— 定位仓库根 `ad_hdl_dir`（并回写进环境变量 `ADI_HDL_DIR`），让所有脚本能用 `$ad_hdl_dir/...` 引用仓库内任意文件。

[adi_env.tcl:20-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L20-L25) —— 声明 `required_vivado_version "2025.1"`（可被环境变量 `REQUIRED_VIVADO_VERSION` 覆盖），供 `adi_project_create` 的版本校验读取。

[adi_env.tcl:28-32](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/scripts/adi_env.tcl#L28-L32) —— 把环境变量 `ADI_IGNORE_VERSION_CHECK` 翻译成全局 `IGNORE_VERSION_CHECK`，决定版本不匹配时是 `ERROR` 退出还是降级为 `CRITICAL WARNING`。

#### 4.3.4 代码实践

**实践目标**：对照真实工程 `system_project.tcl`，准确说明 `adi_project_run` 之前必须完成的前置调用，以及它们各自为 `adi_project_run` 提供了什么。

**操作步骤**：

1. 打开 [system_project.tcl 全文](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl)，定位第 23 行 `adi_project_run`。
2. 向上回溯，列出第 23 行之前所有语句，按「准备 / 核心三步 / 特化微调」分类。
3. 对每一个前置调用，写一句话说明：如果**删掉它**，`adi_project_run` 会在哪一步失败。

**需要观察的现象 / 预期结果**：你应该能整理出类似下面的因果表：

| 删掉的前置调用 | `adi_project_run` 受影响的后果 |
| --- | --- |
| `source adi_env.tcl`（L6） | `$ad_hdl_dir` 未定义，第 7 行 source 助手脚本就会路径解析失败 |
| `source adi_project_xilinx.tcl`（L7） | `adi_project` 等过程未定义，第 12 行直接报 `invalid command name` |
| `set ADI_POST_ROUTE_SCRIPT`（L9） | `adi_project_files` 不登记布线后脚本，`adi_project_run` 里 `ROUTE_DESIGN.TCL.POST` 不会被挂载（构建仍能进行，但缺少 `auto_timing_fix`） |
| `adi_project`（L12） | 工程与块设计不存在、`system_wrapper.v` 未生成，`adi_project_run` 找不到可综合的设计 |
| `adi_project_files`（L13） | 顶层 `system_top.v` 与约束未入工程，综合找不到 `system_top` 顶层 |

**关于运行结果**：本实践属源码阅读与因果推理，结论可由阅读脚本直接得出；若要在本地实测删除某行的效果，需运行 Vivado（耗时较长，属「待本地验证」的可选步骤）。

#### 4.3.5 小练习与答案

**练习 1**：工程脚本里第 21 行 `set_property strategy ... [get_runs impl_1]` 出现在 `adi_project_files` 之后、`adi_project_run` 之前。为什么它必须出现在 `adi_project` 之后？

**参考答案**：因为 `impl_1` 这条 run 是在 `adi_project`（进而 `adi_project_create`）建工程时才被 Vivado 创建出来的。在 `adi_project` 之前工程还不存在，`get_runs impl_1` 取不到对象，`set_property` 会失败。

**练习 2**：如果想让某个工程在综合阶段就被跳过（例如只生成块设计、不跑综合），应该怎么做？

**参考答案**：设置环境变量 `ADI_SKIP_SYNTHESIS`。`adi_project_run` 开头有判断——`if {[info exists ::env(ADI_SKIP_SYNTHESIS)]} { puts "Skipping synthesis"; return }`，会在跑综合前直接返回。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，仿照 `fmcomms2/zcu102` 写一份**最小可读**的 `system_project.tcl` 调用顺序说明，并画出「工程名 → 最终 xsa」的完整数据流。

**操作步骤**：

1. 任选仓库中另一个 Xilinx 工程目录（例如 `projects/adrv9361z7035/xvc706/` 或 `projects/pluto/pluto/`），打开它的 `system_project.tcl`。
2. 标出它的「准备阶段」「三步核心调用」「特化微调」「收尾」四段，与本讲的 `fmcomms2/zcu102` 对比异同（例如它可能没有 `set_property strategy`，或收尾脚本不同）。
3. 用一张流程图（文字版即可）画出从 `make` → `vivado -source system_project.tcl` → `adi_project`（猜器件 + 搭块设计）→ `adi_project_files`（加源码/约束）→ `adi_project_run`（综合/实现/写 xsa）→ 产出 `system_top.xsa` 的完整链路，并在每个节点标注「对应的源码行号或过程名」。
4. 回答一个总括问题：`adi_project_xilinx.tcl` 这套封装，相对于直接在 `system_project.tcl` 里手写全部 Vivado 命令，到底省去了哪些重复劳动？

**预期结果**：你会得到一份该工程的「构建流水线速查表」，并能用三句话总结封装的价值——(1) 把「按工程名猜器件/板卡」自动化；(2) 把「建工程+搭块设计+生成 wrapper」收成一次 `adi_project` 调用；(3) 把「综合/实现/出 xsa + 时序失败处理」收成一次 `adi_project_run` 调用，让每个工程脚本只剩「准备 + 三行 + 特化」的极简结构。

> 说明：本实践为源码阅读与文档型，不要求运行 Vivado；如需对照真实综合日志，运行 Vivado 属耗时操作，可标注「待本地验证」。

## 6. 本讲小结

- `adi_project` 是「翻译层」：靠 `regexp` 匹配工程名后缀，自动查表得到器件串与板卡 BSP，再转交给 `adi_project_create`。
- `adi_project_create` 是「执行层」：完成建工程、设板卡、配 IP 仓库、**版本校验**、`create_bd_design` + `source system_bd.tcl` 搭块设计、`make_wrapper` 生成 `system_wrapper.v`——块设计在这一步就已建好。
- `adi_project_files` 按后缀把 `.xdc` 与其它文件分流进 `constrs_1` / `sources_1`，登记布线后钩子脚本，并把综合顶层硬编码为 `system_top`。
- `adi_project_run` 驱动 `synth_1` 与 `impl_1 -to_step write_bitstream`，按是否时序达标分别写出 `system_top.xsa` 或 `system_top_bad_timing.xsa`（违例时报错退出）。
- 完整调用顺序是：`source` 三脚本 + 设 `ADI_POST_ROUTE_SCRIPT` → `adi_project` → `adi_project_files` → `adi_project_run`；`adi_project_run` 之前必须完成建工程、加源码、定顶层。
- 这套封装把每个工程的 `system_project.tcl` 压缩到「准备 + 三行核心 + 特化」的极简结构，是与 Vivado 原生命令的最大区别。

## 7. 下一步学习建议

- **继续拆块设计连线**：本讲只讲到 `source system_bd.tcl` 这一行，但块设计内部如何用 `ad_connect` / `ad_cpu_interconnect` 等原语拼装，是下一讲 **u3-l4（adi_board.tcl 板级连线助手）** 的主题，建议紧接着学。
- **回到工程构建脚本**：若想了解 `project-xilinx.mk` 如何在 Make 侧准备 `reference.dcp`（供 `adi_project_create` 第 316–320 行的增量编译消费），可回看 **u3-l2**。
- **深入数据通路**：本讲实例 `fmcomms2/zcu102` 在收尾处 `source axi_ad9361_delay.tcl` 做器件校准，相关 IP 的内部结构将在 **u5-l1（axi_dmac）** 与 **u5-l2（数据转换器 IP）** 展开。
- **时序与收发器**：本讲多次提到 `auto_timing_fix_xilinx.tcl` 这个布线后脚本，它的具体作用留到 **u8-l3（收发器、时钟与时序约束）** 讲解。
