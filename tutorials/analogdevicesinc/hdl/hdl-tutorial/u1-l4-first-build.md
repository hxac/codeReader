# 构建第一个工程：从 make 到比特流

## 1. 本讲目标

本讲是「入门层」的收尾篇。学完前三讲你已经知道：ADI HDL 是什么、目录怎么组织、需要什么工具链。本讲把这一切串起来，回答一个最实际的问题：

> 我在命令行敲下 `make` 之后，到底发生了什么？机器是怎么一步步把 Verilog 源码变成可烧进 FPGA 的比特流的？

学完本讲你应该能够：

1. 说清「顶层 `make`」与「在工程目录里 `make`」两种调用方式的区别与联系。
2. 看懂工程 `Makefile` 里 `LIB_DEPS` 和 `M_DEPS` 各自声明的是什么依赖、为什么要把它们分开。
3. 解释 `make` 最终是如何通过 `vivado -source system_project.tcl` 把控制权交给 Vivado 的，并说出 `system_project.tcl` 这个「Vivado 入口脚本」里依次发生了哪些事。

本讲全程以 `projects/fmcomms2/zcu102` 这个真实工程为样本，所有行号与链接均基于当前 HEAD（`e57851ff`）。

## 2. 前置知识

本讲承接前三讲，默认你已经了解：

- **library 与 projects 的分工**（u1-l1、u1-l2）：`library/` 是可复用的 IP 积木，`projects/` 是「评估板 + 载板」两层拼好的整板参考设计。
- **工程的五件套**（u1-l2）：一个 Xilinx 工程目录通常包含 `Makefile`、`system_top.v`、`system_bd.tcl`、`system_project.tcl`、`system_constr.xdc`。
- **工具链版本与环境变量**（u1-l3）：构建需要 GNU Make + 指定版本的 Vivado，版本号在 `scripts/adi_env.tcl` 里集中声明。

本讲会用到三个你可能还陌生的概念，先给一句通俗解释：

- **GNU Make（make）**：一个「依赖驱动」的构建工具。你写一条规则说「目标 A 依赖于文件 B、C，生成 A 的命令是 D」，make 就会先检查 B、C 是否存在/更新，再用命令 D 生成 A。ADI HDL 用 make 来编排整个构建流程。
- **比特流（bitstream）**：综合 + 实现之后，Vivado 产出的二进制文件（`.bit`），烧进 FPGA 后决定芯片的逻辑功能。本工程的最终交付物里还包括 `.xsa`（硬件交付文件，含比特流，供软件侧使用）。
- **Tcl**：Vivado 的脚本语言。ADI HDL 不靠在 Vivado 图形界面里点鼠标来建工程，而是把所有操作写进 `.tcl` 脚本，由 make 自动调用。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 官方给出的「如何构建一个工程」最简说明（`cd projects/fmcomms2/zcu102; make`）。 |
| `Makefile` | 仓库顶层 Makefile，负责自动发现 `projects/` 下的所有子工程并递归进入。 |
| `quiet.mk` | 定义 `build` / `skip_if_missing` / `clean` 等被反复复用的 make 宏，负责日志输出与「缺依赖就跳过」。 |
| `projects/fmcomms2/zcu102/Makefile` | 单个工程的 Makefile，声明本工程的名字、手动依赖（`M_DEPS`）和库依赖（`LIB_DEPS`）。 |
| `projects/scripts/project-xilinx.mk` | Xilinx 工程的「公共构建逻辑」，把 `LIB_DEPS` 转成 IP 打包目标、定义最终产物 `.xsa`、拼出真正的 `vivado` 命令。 |
| `projects/fmcomms2/zcu102/system_project.tcl` | Vivado 的入口脚本，由 make 调用，里面调用一系列 `adi_*` 过程完成建工程、加文件、综合实现。 |

可以把它看成一条流水线：**顶层 `Makefile`（找路）→ 工程 `Makefile`（声明依赖）→ `project-xilinx.mk`（拼命令）→ `system_project.tcl`（Vivado 执行）**。下面三个小节依次拆解这条流水线的三段。

## 4. 核心概念与源码讲解

### 4.1 make 入口与子目录递归

#### 4.1.1 概念说明

仓库根目录下并没有一个「主程序」，构建的入口是命令行 `make`。但仓库里工程成百上千（`projects/<评估板>/<载板>/`），你显然不可能记住每个工程的位置。于是 ADI HDL 用了一个常见技巧：**让顶层 Makefile 自动发现所有工程，并为每个工程生成一个同名的 make 目标**。

这样你有两种等价的构建方式：

- **顶层调用**（在仓库根目录）：`make fmcomms2.zcu102`
- **工程内调用**（`cd` 进工程目录）：`make`

前者更省事、不用记路径；后者更直观。官方 README 推荐的是后者。

#### 4.1.2 核心流程

顶层 Makefile 的「自动发现」逻辑可以概括为三步：

1. **列出所有评估板目录**：扫描 `projects/*`，得到 `fmcomms2`、`ad9361` 等名字。
2. **对每个评估板判定它是「单板工程」还是「多载板工程」**：
   - 若该目录里直接就有 `system_project.tcl`，说明它是一个不分子目录的工程，目标名就是评估板名本身。
   - 否则，扫描它的子目录（每个含 `Makefile` 的子目录代表一块载板），目标名拼成 `评估板.载板`（如 `fmcomms2.zcu102`）。
3. **为每个目标写一条规则**：`make 目标名` 时，把名字里的 `.` 换成 `/`，得到子目录路径，然后 `make -C` 递归进入该目录。

用伪代码表示：

```
PROJECTS = projects/ 下的所有目录名
for each 评估板 in PROJECTS:
    if 存在 projects/评估板/system_project.tcl:
        目标 = "评估板"               # 单板工程
    else:
        for each 载板 in projects/评估板/*/Makefile:
            目标 = "评估板.载板"       # 多载板工程
规则: make 目标  →  make -C projects/(目标里的点换成斜杠)
```

#### 4.1.3 源码精读

顶层 `Makefile` 的核心就在这几行：

- [Makefile:24-28](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L24-L28)：先用 `$(wildcard projects/*)` 列出所有评估板目录；再用一个 `foreach` + `if` 的嵌套，按上面的「单板 / 多载板」规则生成 `SUBPROJECTS` 列表。`$(wildcard projects/$(projname)/system_project.tcl)` 这一句就是在判断「是不是单板工程」。

- [Makefile:32-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L32-L33)：关键规则。`$@` 是当前目标名（如 `fmcomms2.zcu102`），`$(subst .,/,$@)` 把点换成斜杠（变成 `fmcomms2/zcu102`），于是 `$(MAKE) -C projects/fmcomms2/zcu102` 就递归进入了工程目录。**这一行就是「顶层 make → 工程 make」的桥梁。**

- [Makefile:39-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L39-L40)：`make all` 会递归进入 `projects/`，构建所有工程。日常学习时不要随便跑 `make all`——它会构建几百个工程，耗时极长。

注意 `fmcomms2` 属于「多载板工程」：它目录下没有直接的 `system_project.tcl`，而是有 `zcu102/`、`zc702/`、`zed/` 等子目录，每个子目录才是一个具体的载板工程。所以它的目标名都是 `fmcomms2.<载板>` 这种形式。

#### 4.1.4 代码实践

**实践目标**：验证顶层 Makefile 的「自动发现」是否如预期那样把 `fmcomms2.zcu102` 映射到正确子目录。

**操作步骤**：

1. 在仓库根目录执行（**只预览、不真正构建**——用 make 的 `-n` 干跑选项，只打印命令不执行）：
   ```bash
   make -n fmcomms2.zcu102
   ```
2. 观察输出里 `make -C` 后面跟的路径。
3. 再执行 `make -n all 2>/dev/null | grep -m5 'make -C projects/'`，看 `make all` 会递归进哪些目录。

**需要观察的现象**：干跑会打印出一条形如 `make -C projects/fmcomms2/zcu102` 的命令，证明点被换成了斜杠、路径拼接正确。

**预期结果**：`make -n fmcomms2.zcu102` 输出的第一条实质命令就是进入 `projects/fmcomms2/zcu102`。如果你没有安装 Vivado，`make -n` 仍然可用，因为它不真正执行构建。

> 待本地验证：不同 make 版本对 `-n` 的输出顺序可能略有差异，但 `make -C projects/fmcomms2/zcu102` 这一行必然出现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `fmcomms2` 的目标名是 `fmcomms2.zcu102` 而不是单纯的 `fmcomms2`？

**参考答案**：因为 `projects/fmcomms2/` 目录下没有直接的 `system_project.tcl`，顶层 Makefile 走的是「多载板」分支，把每块载板（`zcu102`、`zc702`、`zed` 等）都拼成 `fmcomms2.<载板>` 的目标名。

**练习 2**：如果想从仓库根目录直接构建 `projects/fmcomms2/zed`，对应的 make 命令是什么？

**参考答案**：`make fmcomms2.zed`。点号会被 [Makefile:32-33](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/Makefile#L32-L33) 换成斜杠，得到 `projects/fmcomms2/zed`。

---

### 4.2 工程 Makefile 中的 LIB_DEPS / M_DEPS

#### 4.2.1 概念说明

递归进入工程目录后，make 读到的是该工程自己的 `Makefile`。这个文件很短，核心只做三件事：

1. **声明工程名**（`PROJECT_NAME`）。
2. **声明两类依赖**，分别用两个变量收集：
   - `LIB_DEPS`：本工程要用到哪些 **library IP 积木**（如 `axi_dmac`、`axi_ad9361`）。
   - `M_DEPS`：本工程要用到的 **其他文件**（块设计脚本、约束、顶层 Verilog、校准 Tcl 等）。
3. **`include` 公共构建逻辑** `project-xilinx.mk`，把实际的「怎么打包 IP、怎么跑 Vivado」交给它。

为什么要分成 `LIB_DEPS` 和 `M_DEPS` 两类？因为 library IP 需要被**打包**（Xilinx 下是生成 `component.xml`，让 Vivado 把它识别为一个可拖入块设计的 IP），而普通文件只需要被**直接引用**。两者的构建方式完全不同，所以分开声明。

#### 4.2.2 核心流程

工程 Makefile 的执行流程：

```
PROJECT_NAME := fmcomms2_zcu102          # 工程名（下划线连接，不含点）
M_DEPS  += 一堆「直接引用」的文件          # bd.tcl、约束、ad_iobuf.v、校准脚本……
LIB_DEPS += 一堆「IP 积木」名字            # axi_dmac、axi_ad9361、util_cpack2……
include ../../scripts/project-xilinx.mk   # 引入公共逻辑
```

被 include 的 `project-xilinx.mk` 接手后，会做这几件与本节相关的事：

1. 给 `M_DEPS` 追加一批「所有工程通用的文件」（`system_project.tcl`、`system_bd.tcl`、`system_top.v`、`adi_env.tcl` 等）。
2. **把每个 `LIB_DEPS` 转换成一个对应的 `component.xml` 目标**——这是关键一步：依赖一个 IP，就等于依赖它的打包产物 `component.xml`。
3. 定义最终产物目标 `<工程名>.sdk/system_top.xsa`，它依赖于上面所有 `M_DEPS`。

也就是说，`LIB_DEPS` 是「声明意图」，`project-xilinx.mk` 把它翻译成「具体的构建目标」。

#### 4.2.3 源码精读

先看工程自身的 [projects/fmcomms2/zcu102/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile)：

- [projects/fmcomms2/zcu102/Makefile:7](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L7)：`PROJECT_NAME := fmcomms2_zcu102`。注意用的是下划线 `fmcomms2_zcu102`（给 Vivado 当工程名），而顶层 make 目标用的是点 `fmcomms2.zcu102`（仅用于定位目录），两者不要混淆。

- [projects/fmcomms2/zcu102/Makefile:9-14](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L9-L14)：`M_DEPS`，收集「直接引用」的文件，包括评估板块设计 `fmcomms2_bd.tcl`、载板约束与块设计 `zcu102_system_constr.xdc` / `zcu102_system_bd.tcl`、IO 缓冲源码 `ad_iobuf.v`、以及 AD9361 的延时校准脚本 `axi_ad9361_delay.tcl`。

- [projects/fmcomms2/zcu102/Makefile:16-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L16-L25)：`LIB_DEPS`，列出本工程依赖的全部 library IP。**这就是本练习要找的「该工程依赖了哪些 library 模块」的答案**：`axi_ad9361`、`axi_dmac`、`axi_sysid`、`sysid_rom`、`util_pack/util_cpack2`、`util_pack/util_upack2`、`util_rfio`、`util_tdd_sync`、`util_wfifo`、`xilinx/util_clkdiv`。

- [projects/fmcomms2/zcu102/Makefile:27](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L27)：`include ../../scripts/project-xilinx.mk`，把控制权交给公共构建逻辑。

再看公共逻辑 `project-xilinx.mk` 里的关键转换：

- [projects/scripts/project-xilinx.mk:73-83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L73-L83)：先给 `M_DEPS` 追加所有工程通用的文件（第 73–81 行），然后在第 83 行用 `foreach` 把每个 `LIB_DEPS` 翻译成 `$(HDL_LIBRARY_PATH)$(dep)/component.xml`。例如 `LIB_DEPS` 里的 `axi_dmac` 会变成 `.../library/axi_dmac/component.xml`——**依赖一个 IP 积木 = 依赖它的 IP 打包产物**。

- [projects/scripts/project-xilinx.mk:138-146](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L138-L146)：这是「如何生成 `component.xml`」的规则。它通过 `$(MAKE) -C $(dir $@) xilinx` 递归进入对应 library 目录执行 `make xilinx`，从而触发 IP 打包（打包的细节在第 4 单元 u4-l2 详讲）。这里出现的 `flock` 是为并行构建时的安全加锁，本讲只需知道「它会确保多个工程同时打包同一个 IP 时不会互相踩踏」即可。

> 名字由来小贴士：`M_DEPS` 的 **M** 常被理解为 Manual（手动列出的文件依赖），`LIB_DEPS` 则是 Library（库依赖）。两者都只是 make 变量，最终都被并进同一条依赖链。

#### 4.2.4 代码实践

**实践目标**：亲手读出 `fmcomms2/zcu102` 依赖的全部 library 模块，并理解它们如何被转成打包目标。

**操作步骤**：

1. 打开 [projects/fmcomms2/zcu102/Makefile:16-25](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/Makefile#L16-L25)，把 `LIB_DEPS` 列出的 10 个模块抄成一张表，并到 `library/` 下确认每个目录确实存在。
2. 对照 [project-xilinx.mk:83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L83)，写出 `axi_dmac` 被展开后的完整 `component.xml` 路径。
3.（可选，需 make）在工程目录里干跑：`cd projects/fmcomms2/zcu102 && make -n`，在输出里找 `make -C .../library/axi_dmac xilinx` 这样的行，验证 IP 打包确实被触发。

**需要观察的现象**：`LIB_DEPS` 的每一项都对应 `library/` 下一个真实存在的目录；干跑输出里能看到为每个 IP 调用 `make -C <lib> xilinx` 的命令。

**预期结果**：`axi_dmac` 展开后约为 `../../../library/axi_dmac/component.xml`（路径前缀取决于 make 的启动位置）。这 10 个 IP 在构建本工程时都会被先打包成 Vivado IP。

> 待本地验证：`make -n` 的具体输出顺序受 make 版本影响，但每个 `LIB_DEPS` 项对应的 `make -C ... xilinx` 行都会出现。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `axi_dmac` 从 `LIB_DEPS` 里删掉，构建会发生什么？

**参考答案**：`project-xilinx.mk` 不会再为 `axi_dmac` 生成 `component.xml` 目标，Vivado 在块设计里就找不到这个 IP，打包/综合阶段会报「找不到 IP」的错误。所以 `LIB_DEPS` 必须完整声明工程用到的所有 library IP。

**练习 2**：`M_DEPS` 和 `LIB_DEPS` 在 `project-xilinx.mk` 里被「合并」的那一行是哪一行？合并后产生了什么类型的目标？

**参考答案**：是 [project-xilinx.mk:83](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L83)，`M_DEPS += $(foreach dep,$(LIB_DEPS),.../component.xml)`。它把库依赖合并成了一组 `component.xml`（IP 打包产物）目标，追加进 `M_DEPS` 这条统一的依赖链。

---

### 4.3 system_project.tcl 作为 Vivado 入口

#### 4.3.1 概念说明

到目前为止，make 还只是在「安排依赖、触发 IP 打包」。真正要综合、实现、生成比特流，必须由 Vivado 来干。make 的做法很直接：**拼出一条 `vivado -source system_project.tcl` 命令并执行它**——也就是启动 Vivado 的批处理模式，让它把 `system_project.tcl` 当成「脚本入口」逐行执行。

所以 `system_project.tcl` 就是整个工程的 **Vivado 入口脚本**：它不写具体的 RTL，而是「指挥」Vivado——创建工程、导入源码、搭建块设计、跑综合实现、写出最终交付物。它通过调用一系列封装好的 `adi_*` 过程（这些过程定义在被 `source` 进来的 `adi_project_xilinx.tcl` / `adi_board.tcl` 里）来保持简短可读。

#### 4.3.2 核心流程

`system_project.tcl` 的执行可以分成四个阶段：

```
阶段 0  准备：source adi_env.tcl（环境/版本）、
                source adi_project_xilinx.tcl（adi_* 过程定义）、
                source adi_board.tcl（块设计连线原语）
阶段 1  adi_project <工程名>
          → 根据工程名里的载板关键字（如 _zcu102）查到 FPGA 型号与板卡
          → adi_project_create：create_project、create_bd_design、source system_bd.tcl
            （真正搭建块设计）、validate_bd_design、generate_target、make_wrapper
阶段 2  adi_project_files <工程名> {文件列表}
          → 把 system_top.v、约束 xdc、ad_iobuf.v 等加入工程
阶段 3  adi_project_run <工程名>
          → launch_runs synth_1（综合）
          → launch_runs impl_1 -to_step write_bitstream（实现 + 生成比特流）
          → write_hw_platform ... system_top.xsa（写出含比特流的硬件交付文件）
```

其中阶段 1 里的 `source system_bd.tcl` 才是真正「拼装整板设计」的地方（用 `ad_connect` 等 Tcl 原语把各个 IP 连起来），这部分在 u2-l1、u3-l4 详讲；本讲只需把握「`system_project.tcl` 是入口、它按四阶段调度 Vivado」这条主线。

#### 4.3.3 源码精读

先看 make 是在哪一行拼出 vivado 命令的：

- [projects/scripts/project-xilinx.mk:22](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L22)：`VIVADO := vivado -tempDir .Xil -mode batch -source`。注意末尾的 `-source` 还没跟文件名，它会在下面被拼接。

- [projects/scripts/project-xilinx.mk:116-136](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L116-L136)：这是最终产物 `system_top.xsa` 的规则。它先依赖 `$(M_DEPS)`（确保所有 IP 已打包、所有源文件就绪），第 116 行声明目标；第 117–127 行处理可选的增量编译（`MODE=incr`，本讲从略）；真正调用 Vivado 的是第 128–136 行的 `$(call build, $(VIVADO) system_project.tcl, ...)`，展开后即 `vivado -tempDir .Xil -mode batch -source system_project.tcl`。

- [projects/scripts/project-xilinx.mk:87](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L87)：`all: external_dependencies $(PROJECT_NAME).sdk/system_top.xsa`。`make`（默认目标 `all`）会先确保外部依赖，再构建 `.xsa`。**这就是「make 最终调用 Vivado」的整条依赖链顶端。**

> 日志去向小贴士：上面用到的 `build` 宏定义在 [quiet.mk:42-52](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/quiet.mk#L42-L52)，它把 vivado 的全部输出重定向到 `<工程名>_vivado.log`，构建结束打印绿色 `OK` 或红色 `FAILED`。所以构建时屏幕很安静，真正的细节要去这个日志里看。

再看入口脚本本身 [projects/fmcomms2/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl)：

- [system_project.tcl:6-8](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L6-L8)：阶段 0，依次 source 三个脚本——`adi_env.tcl`（u1-l3 讲过的环境与版本）、`adi_project_xilinx.tcl`（提供 `adi_project` / `adi_project_files` / `adi_project_run` 等过程）、`adi_board.tcl`（提供块设计连线原语，u3-l4 详讲）。

- [system_project.tcl:9-10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L9-L10)：设置一个「布线后脚本」`auto_timing_fix_xilinx.tcl`（u8-l3 详讲），并声明板名 `zcu102`。

- [system_project.tcl:12](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L12)：阶段 1，`adi_project fmcomms2_zcu102`。这一句的威力在于 `adi_project` 内部会用正则匹配工程名里的 `_zcu102`，从而查到对应 FPGA 型号 `xczu9eg-ffvb1156-2-e` 和板卡，详见 [adi_project_xilinx.tcl:107-110](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L107-L110)。随后 `adi_project_create` 会 `create_project`、`create_bd_design`、`source system_bd.tcl` 搭块设计、再 `generate_target`、`make_wrapper`（[adi_project_xilinx.tcl:292-308](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L292-L308)）。

- [system_project.tcl:13-17](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L13-L17)：阶段 2，`adi_project_files` 把 `system_top.v`（顶层）、两份 `system_constr.xdc`（约束）、`ad_iobuf.v`（IO 缓冲）加入工程。其中 `.xdc` 文件会被 `adi_project_files` 自动放进约束文件集（[adi_project_xilinx.tcl:333-339](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L333-L339)）。

- [system_project.tcl:21](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L21)：把实现策略设为 `Congestion_SpreadLogic_high`，用来缓解 fmcomms2 设计里某些路径的 hold time 违例——一个典型的「工程特化调优」。

- [system_project.tcl:23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L23)：阶段 3，`adi_project_run fmcomms2_zcu102`。它内部依次 `launch_runs synth_1`（综合）、`launch_runs impl_1 -to_step write_bitstream`（实现并生成比特流），最后 `write_hw_platform ... system_top.xsa` 写出含比特流的硬件交付文件（[adi_project_xilinx.tcl:386-416](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L386-L416) 与 [adi_project_xilinx.tcl:602](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L602)）。**这一步产出的 `system_top.xsa`，正是 make 那条依赖链苦苦等待的最终产物。**

- [system_project.tcl:24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L24)：综合实现完成后，再 source AD9361 的延时校准脚本 `axi_ad9361_delay.tcl`，对数据通路做延时校准（u5-l2 详讲）。

#### 4.3.4 代码实践

**实践目标**：把「make → Vivado」这条链的最后一环看清楚——找到拼出 vivado 命令的确切位置，并验证入口脚本的调用顺序。

**操作步骤**：

1. 打开 [project-xilinx.mk:22](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L22) 与 [project-xilinx.mk:128-136](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/project-xilinx.mk#L128-L136)，把 `VIVADO` 变量与 `$(VIVADO) system_project.tcl` 拼起来，写出最终执行的完整命令。
2. 打开 [system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl)，按本节「四阶段」给它每一行标注属于哪个阶段（阶段 0/1/2/3）。
3.（可选，需已安装指定版本 Vivado）真正构建一次：
   ```bash
   cd projects/fmcomms2/zcu102
   make
   ```
   构建耗时可能数十分钟到数小时。结束后在 `fmcomms2_zcu102.sdk/` 下应能看到 `system_top.xsa`（若时序违例则会是 `system_top_bad_timing.xsa`）。

**需要观察的现象**：

- 步骤 1 拼出的命令应为 `vivado -tempDir .Xil -mode batch -source system_project.tcl`。
- 步骤 2 应能看到清晰的 `source adi_env.tcl` → `adi_project` → `adi_project_files` → `adi_project_run` 顺序。
- 步骤 3（若执行）屏幕主要显示 `build` 宏打印的进度行，详细日志在 `fmcomms2_zcu102_vivado.log`。

**预期结果**：四阶段顺序与 4.3.2 的伪代码一致；最终产物为 `system_top.xsa`（含比特流）。

> 待本地验证：真实构建是否成功取决于是否安装了 u1-l3 要求的 Vivado 版本（当前分支为 `2025.1`）；版本不匹配会在 `adi_project_create` 里报 `ERROR: vivado version mismatch` 并 `exit 2`（见 [adi_project_xilinx.tcl:209-216](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L209-L216)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 make 不直接调用 `vivado`，而要 `-source system_project.tcl`？

**参考答案**：因为建工程、加文件、搭块设计、综合实现这一长串操作需要用 Tcl 精确描述才能复现。`-source system_project.tcl` 让 Vivado 进入批处理模式、把 `system_project.tcl` 当入口脚本逐行执行，从而实现「无人值守、可重复」的命令行构建，而不依赖在图形界面里手动点选。

**练习 2**：`system_project.tcl` 里如果把 `adi_project_run` 那一行注释掉，会发生什么？

**参考答案**：工程会被创建、块设计会搭好、源文件会加入，但**不会跑综合和实现，也不会生成比特流和 `.xsa`**。于是 make 等待的最终产物 `system_top.xsa` 不会产生，构建链无法完成。`adi_project_run` 是「把设计变成比特流」的关键一步。

---

## 5. 综合实践

把本讲三节串起来，完成下面这个「全链路追踪」小任务（不需要真正跑 Vivado，纯源码阅读即可）：

**任务**：画出从用户敲下 `make fmcomms2.zcu102` 到 Vivado 启动之间的完整调用链，并标注每一步对应的源码位置。

**要求产出一张这样的流程图（文字版即可）**：

```
1. 用户: make fmcomms2.zcu102
        └─ 顶层 Makefile（Makefile:32-33）
2. make -C projects/fmcomms2/zcu102
        └─ 进入工程目录，读 projects/fmcomms2/zcu102/Makefile
3. 设置 PROJECT_NAME；收集 M_DEPS / LIB_DEPS（Makefile:7-25）
        └─ include ../../scripts/project-xilinx.mk（Makefile:27）
4. project-xilinx.mk 把 LIB_DEPS → component.xml 目标（project-xilinx.mk:83）
        └─ 先递归 make -C <lib> xilinx 把每个 IP 打包（project-xilinx.mk:138-146）
5. 默认目标 all → 构建 system_top.xsa（project-xilinx.mk:87）
        └─ xsa 规则执行（project-xilinx.mk:116-136）
6. 执行 vivado -mode batch -source system_project.tcl（project-xilinx.mk:22,134）
        └─ system_project.tcl 四阶段调度 Vivado（system_project.tcl:6-24）
7. adi_project_run 产出 system_top.xsa（adi_project_xilinx.tcl:602）
```

**额外要求**：

1. 在第 3 步，列出本工程依赖的全部 library 模块（答案见 4.2.3）。
2. 在第 6 步，标出 `system_project.tcl` 里 `source system_bd.tcl` 是在哪一行被调用的（提示：它不在 `system_project.tcl` 里直接出现，而是被 `adi_project_create` 在 [adi_project_xilinx.tcl:293](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L293) 调用）。
3. 用一句话解释：为什么这条链叫「依赖驱动」——即 `system_top.xsa` 这条规则为什么能自动带动前面所有步骤发生。

**参考答案要点**：

- 全部 library 依赖：`axi_ad9361`、`axi_dmac`、`axi_sysid`、`sysid_rom`、`util_pack/util_cpack2`、`util_pack/util_upack2`、`util_rfio`、`util_tdd_sync`、`util_wfifo`、`xilinx/util_clkdiv`。
- `system_bd.tcl` 由 `adi_project_create` 在 [adi_project_xilinx.tcl:293](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/scripts/adi_project_xilinx.tcl#L293) source，而非在 `system_project.tcl` 里直接出现。
- 「依赖驱动」是因为 `system_top.xsa` 依赖 `$(M_DEPS)`，而 `M_DEPS` 又含每个 IP 的 `component.xml`；make 为了造 `.xsa`，必须先把所有 `component.xml`（即 IP 打包）造出来，从而自动带动了第 4 步的 IP 打包和第 6 步的 Vivado 执行。

## 6. 本讲小结

- 顶层 `Makefile` 用 `wildcard` + `foreach` 自动发现 `projects/` 下所有工程，并用「点换斜杠」的技巧把目标名 `fmcomms2.zcu102` 映射到目录 `projects/fmcomms2/zcu102`，再 `make -C` 递归进入。
- 工程 `Makefile` 用 `PROJECT_NAME` 声明工程名，用 `M_DEPS` 收集「直接引用」的文件、用 `LIB_DEPS` 收集「IP 积木」名字，最后 `include project-xilinx.mk`。
- `project-xilinx.mk` 把每个 `LIB_DEPS` 翻译成 `component.xml` 目标（即「依赖一个 IP = 依赖它的打包产物」），并定义最终产物 `system_top.xsa` 的规则。
- 真正调用 Vivado 的那一行是 `vivado -mode batch -source system_project.tcl`，由 `build` 宏包裹、日志写入 `<工程名>_vivado.log`。
- `system_project.tcl` 是 Vivado 入口脚本，按「准备 → adi_project 建工程搭块设计 → adi_project_files 加源码 → adi_project_run 综合实现出比特流」四阶段调度 Vivado。
- 整条链是「依赖驱动」的：make 为了产出 `.xsa`，会自动先打包所有依赖的 IP、再启动 Vivado，读者无需手动逐步操作。

## 7. 下一步学习建议

本讲你已经走通了「从 make 到比特流」的完整入口流程。建议接下来：

1. **进入第 2 单元 u2-l1（三层工程架构）**：本讲提到了 `system_bd.tcl` 负责拼装整板设计，但它内部其实分「载板基设计 + 评估板基设计」两层 source。u2-l1 会讲清这个三层模型。
2. **进入第 2 单元 u2-l2（单个工程的文件剖析）**：逐个精读 `system_top.v`、`system_project.tcl`、`system_bd.tcl`、`system_constr.xdc` 这五件套各自的职责。
3. **后续深入构建系统（第 3 单元）**：本讲对 `project-xilinx.mk` 只讲了依赖链与 vivado 调用，u3-l1/u3-l2 会拆解 `quiet.mk` 的宏、`flock` 并行安全、`MODE=incr` 增量编译等更深层机制；u3-l3/u3-l4 会精讲 `adi_project_xilinx.tcl` 与 `adi_board.tcl` 里的 Tcl 原语。
