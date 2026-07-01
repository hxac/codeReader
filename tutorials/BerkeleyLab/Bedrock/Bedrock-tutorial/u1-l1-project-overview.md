# 项目总览：Bedrock 是什么

> 本讲是 Bedrock 学习手册的第一篇。你不默认熟悉 FPGA、Verilog 或 DSP 也能读懂。
> 本讲只解决一个问题:**Bedrock 到底是什么、由哪些部分组成、从哪里开始看起。**

---

## 1. 本讲目标

读完本讲,你应该能够:

1. 用一两句话向别人说清楚 **Bedrock 是什么**、它来自哪里(LBNL)、用来做什么。
2. 说出 Bedrock 顶层有哪些**主要子系统**,以及每个子系统负责什么(dsp / cordic / badger / localbus / rtsim / cmoc / soc / projects 等)。
3. 理解 Bedrock "**命令行批处理 + 厂家图形工具并存**"的设计哲学,以及它为什么把厂家生成物放进 `_xilinx` 这类目录。
4. 读懂 `dir_list.mk`,理解一个用 GNU Make 串起来的大型工程为什么需要一个"目录常量表"。
5. 独立打开 `README.md`,自己整理出一张"子系统职责表"。

---

## 2. 前置知识

本讲面向完全的初学者,但有几个名词最好先有个直觉。看不懂没关系,后面会反复用到。

| 术语 | 直觉解释 |
|------|----------|
| **HDL(Hardware Description Language,硬件描述语言)** | 用代码"描述"一块数字电路长什么样,而不是一步步命令计算机执行。Verilog 是最常用的 HDL 之一。 |
| **Verilog** | 一种 HDL,语法上有点像 C。Bedrock 的核心代码几乎全是 Verilog(`.v` 文件)。 |
| **FPGA(Field Programmable Gate Array,现场可编程门阵列)** | 一块"出厂后还能重新连线"的芯片。你把 Verilog"综合"成比特流烧进去,它就变成你设计的电路。 |
| **DSP(Digital Signal Processing,数字信号处理)** | 对数字化的信号做滤波、混频、变频等数学运算。Bedrock 的 `dsp/` 目录里全是这类硬件运算模块。 |
| **RF / LLRF** | RF(Radio Frequency,射频);LLRF(Low-Level RF,低电平射频控制)。Bedrock 的重要应用场景是粒子加速器里给超导谐振腔做精密 RF 控制。 |
| **LBNL** | Lawrence Berkeley National Laboratory(劳伦斯伯克利国家实验室),Bedrock 的诞生地。 |
| **GNU Make / Makefile** | 一个根据"依赖关系"自动决定编译顺序的工具。Bedrock 全程用 Make 来编译、仿真、综合。 |

> 一句话定位:**Bedrock 是 LBNL 多年积累的、平台无关的(platform-independent)Verilog 代码库,用来做 DSP / RF 控制并把设计落到 FPGA 上。** 这正是 `README.md` 开篇的原话,我们下一节逐句精读。

---

## 3. 本讲源码地图

本讲只涉及 3 个文件,它们都在仓库**根目录**下:

| 文件 | 作用 | 本讲用它来 |
|------|------|-----------|
| [README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md) | 项目的"门面",介绍 Bedrock 是什么、有哪些子系统、依赖什么工具、怎么看待图形 vs 命令行 | 理解项目定位与子系统划分 |
| [dir_list.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk) | 一个很短的 Make 片段,用变量把"Bedrock 根目录"和各子目录的绝对路径集中定义出来 | 理解大型 Make 工程的"目录常量"机制 |
| [LICENSE.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/LICENSE.md) | 版权与许可证(BSD 风格),声明版权归加州大学/LBNL | 确认这是一个开源、可自由使用的代码库 |

> 小贴士:本讲引用的所有源码链接都用当前 HEAD(`235f3e3b5...`)作为永久链接,点击即可在 GitHub 上看到带行号高亮的那段代码。

---

## 4. 核心概念与源码讲解

本讲拆成两个最小模块:

- **4.1 README 顶层说明** —— Bedrock 的定位、子系统清单、工具链与设计哲学。
- **4.2 dir_list.mk 目录常量定义** —— 一个用 Make 串联的工程如何用变量集中管理路径。

### 4.1 README 顶层说明:Bedrock 的定位与子系统划分

#### 4.1.1 概念说明

打开任何一个开源项目,第一份该读的文档就是 `README`。它通常回答三个问题:

1. 这个项目**是什么**?
2. 它由**哪些部分**组成?
3. 我**怎么运行/构建**它?

Bedrock 的 `README.md` 用非常朴素的英文回答了全部三个问题。其中最关键的一句,把整个项目的"身份"定死了:

> Bedrock is largely an accumulation of Verilog codebase written over the past several years at LBNL. It contains platform-independent Verilog, and whatever it takes to get it onto FPGA platforms like Xilinx etc.

这句话里有三个关键词,后面整本手册都围绕它们展开:

- **accumulation(积累)**:Bedrock 不是某一次重新设计的产物,而是多年实战代码的沉淀,所以风格偏实用、子系统众多。
- **platform-independent(平台无关)**:核心算法用"可移植的"Verilog 写成,尽量不绑定某个 FPGA 厂家。
- **get it onto FPGA platforms**:核心是平台无关的,但仓库里也**附带了**让它落到具体 FPGA(如 Xilinx)所需的约束和脚本。

#### 4.1.2 核心流程

读懂 Bedrock 这类"多子系统聚合体"项目的标准流程是:

```
README 顶层说明
   │
   ├── 1. 定位(是什么) ────────────────► 第 6~8 行
   ├── 2. 子系统清单(由什么组成) ──────► 第 10~28 行的无序列表
   ├── 3. 工具链/CI(怎么构建) ─────────► 第 31~42 行的编号列表
   └── 4. 图形 vs 批处理哲学 ─────────► 第 45~78 行
```

其中第 2 步"子系统清单"是本讲的重点。README 用一个无序列表把顶层子目录逐一列出,每个目录对应一个职责。后面所有讲义,本质都是在深入这些子目录里的某一个。

#### 4.1.3 源码精读

**① Bedrock 的定位(是什么)**

[README.md:6-8](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L6-L8) —— 这就是上面引用的"LBNL 多年积累的平台无关 Verilog 代码库"。读完这三行你就掌握了 Bedrock 的身份。

**② 子系统清单(由什么组成)**

[README.md:10-28](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L10-L28) —— 一个无序列表,每个条目就是一个顶层子目录加一句职责说明。下面把这一段整理成中文表(本讲综合实践会要求你自己再画一遍):

| 子目录 | README 原意(中文化) |
|--------|----------------------|
| `dsp` | 各种用**平台无关 Verilog** 实现的数字信号处理算法及其测试台,包括 DDS、下变频、上变频、CIC 滤波器、低通/高通滤波器、混频器等 |
| `cordic` | 一个自包含的 [CORDIC](https://en.wikipedia.org/wiki/CORDIC) Verilog 实现,支持多种可在**编译时或运行时**选择的工作模式 |
| `rtsim` | 对 RF 系统(谐振腔及其电学/力学模式、ADC、电缆、压电片等)的**实时仿真** |
| `cmoc` | 一个 RF 控制器的 Verilog 实现,可以接真实 ADC,也可以接 `rtsim` 里的仿真部件 |
| `badger` | 一个在硬件(fabric)里实时响应 **Ethernet/IP/UDP** 数据包的核 |
| `fpga_family` | 各 FPGA 厂家相关的约束文件和厂家专用特性的钩子(hooks) |
| `localbus` | Bedrock 内部广泛使用的**片上 localbus**(本地总线)的文档与特性 |
| `board_support` | 各块板卡的引脚映射等相关文件 |
| `projects` | 具体实例化工程:它们会综合出比特流,烧到各种板卡上的 FPGA 里 |

> 注意:`README` 的这个清单没有把所有顶层目录都列全。例如 `serial_io`(串行链路)、`peripheral_drivers`(外设驱动)、`soc/picorv32`(RISC-V 软核 SoC)在仓库里真实存在,但没进 README 的这个列表——它们出现在 `dir_list.mk` 的目录常量里(见 4.2)。这也说明:**README 是入口,但不是全貌;要看完整的顶层划分,得结合 `dir_list.mk`。**

**③ 工具链与持续集成(怎么构建)**

[README.md:31-42](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L31-L42) —— 五条关于代码库的说明,信息量很大:

1. 全部软件都设计成在 `*nix` 上轻松运行。
2. 一切都用 **GNU Make** 构建(指向 `build-tools/makefile.md`)。
3. 用 **iverilog** 做仿真,正在慢慢开始用 **Verilator**(见 `badger`)。
4. 用 **Xilinx** 工具做综合,并开始支持 **YoSys**(同样见 `badger`)。
5. 仓库接入了 **GitLab CI**:每次提交都会在 CI 服务器上自动跑所有仿真测试;其中一个有用子集可以通过 `selftest.sh` 在本地复现。

这一段告诉我们 Bedrock 的"地基工具链"是 `Make + iverilog + Python`,这也是为什么本手册单元 2 的第一篇就讲"基于 Make 的 HDL 仿真测试方法"。

**④ 图形 vs 批处理的设计哲学**

[README.md:45-78](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L45-L78) —— 这段是理解 Bedrock 整体设计取向的关键。它讲了两件事:

- Bedrock 的结构**优先利用 `make`、`grep`、shell、python 等传统 `*nix` 命令行工具**,从而方便自动化、定制和挂代码生成钩子。
- 但**图形交互界面也有其不可替代的场景**(看综合后的原理图、调整布局约束、定制厂家 IP 核等)。所以 Bedrock 的做法是:把厂家生成的全部文件放进一个 `_<VENDOR_NAME>` 目录,例如综合 Xilinx 设计时会创建 `_xilinx/`。这样命令行流程和图形工具可以并存而不互相干扰。

  例如,工程建好后可以直接手动打开 Vivado:

  ```bash
  vivado <PROJECT_DIRECTORY>/_xilinx/<TOP_LEVEL_DESIGN_NAME>/<TOP_LEVEL_DESIGN_NAME>.xpr
  ```

  见 [README.md:69-74](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L69-L74)。

**⑤ 依赖清单**

[README.md:80-96](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L80-L96) —— 必需依赖是 `GNU Make / iverilog / Python`;推荐依赖是 `GTKWave / Xilinx Vivado / Verilator / YoSys`。完整清单见 `dependencies.txt`。下一篇讲义(u1-l2)会专门讲怎么装、怎么跑。

**⑥ 版权与许可证**

[README.md:104-107](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md#L104-L107) 指向 [LICENSE.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/LICENSE.md)。[LICENSE.md:3](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/LICENSE.md#L3) 写明:版权归 "The Regents of the University of California, through Lawrence Berkeley National Laboratory"(2019 年),是一份 BSD 风格的开源许可证,允许自由再分发和修改。这意味着你可以放心地学习、使用和二次开发 Bedrock。

#### 4.1.4 代码实践

**实践目标:** 亲手从 `README.md` 提取信息,建立对 Bedrock 顶层结构的"心智地图",而不是只听我转述。

**操作步骤:**

1. 在浏览器打开 [README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md),或用编辑器打开仓库根目录的 `README.md`。
2. 滚动到 `It is currently a conglomerate of code broken into the following subdirectories:` 这一行(约第 10 行)。
3. 针对下面 8 个子系统,逐条阅读其后的说明,用自己的话填一张表:`dsp / cordic / badger / localbus / rtsim / cmoc / soc / projects`。
4. 注意:`soc` 没有出现在 README 的清单里。请到 `dir_list.mk`(见 4.2)里找到它的踪迹,把来源也记下来。

**需要观察的现象:**

- README 用一个**无序列表**罗列子系统,每个条目是"`目录名`:一句话职责"。
- 列表里有的目录名带了超链接(如 `dsp`、`cordic`、`badger`、`localbus`),有的没有(如 `rtsim`、`cmoc`)。想想这暗示了什么(提示:带链接的可能说明该子目录里有更详细的说明文档,或被 README 作者认为更重要)。

**预期结果:** 你应该得到一张类似这样的表(以 `dsp` 为例,其余自己补全):

| 子系统 | 职责(自己的话) | 来源 |
|--------|------------------|------|
| `dsp` | 用可移植 Verilog 写的数字信号处理算法和测试台(DDS、混频、上下变频、CIC、滤波器等) | README 第 12~15 行 |
| `cordic` | …(自填) | README 第 16~18 行 |
| `soc` | …(自填,提示:RISC-V 软核 SoC) | `dir_list.mk` 第 18 行(README 未列) |

> 如果你拿不准某个子系统的职责,就如实写"待确认",不要编造。本讲只要求建立大致地图,细节在后续讲义里展开。

#### 4.1.5 小练习与答案

**练习 1:** README 说 Bedrock 是 "platform-independent" 的,但 `fpga_family`、`board_support` 这些目录又是"和厂家/板卡强相关"的。这两件事矛盾吗?

> **参考答案:** 不矛盾。Bedrock 的**核心算法**尽量用平台无关的 Verilog 实现,这样可以跨厂家复用;而 `fpga_family` / `board_support` 则是"为了让这些平台无关代码落到具体 FPGA 和具体板卡上"而附带的、隔离好的厂家/板卡相关层。把"可移植核心"和"厂家适配层"分开,正是 platform-independent 设计的体现。

**练习 2:** README 第 39~42 行提到了 `selftest.sh`。用一句话说它的作用,并猜猜它和 GitLab CI 是什么关系。

> **参考答案:** `selftest.sh` 让你能在本地工作站上跑 CI 测试的一个有用子集。关系是:CI 在每次提交时自动跑全部仿真测试,`selftest.sh` 则是把其中一部分测试搬到本地,方便你在推送之前先自测。

---

### 4.2 dir_list.mk 目录常量定义

#### 4.2.1 概念说明

Bedrock 是一个**用 GNU Make 串起来的大型工程**。每个子目录(`dsp/`、`cordic/`、`badger/`...)里都有自己的 `Makefile` 或 `rules.mk`,它们经常需要引用**别的子目录**里的文件(比如 `dsp/` 的测试要 include `localbus/` 的总线任务,`cordic/` 要用到 `build-tools/` 里的通用规则)。

如果每个 Makefile 都自己写死路径(`../../dsp/...`),一旦目录改名或整个 Bedrock 被挪到别的位置,就会全面崩溃。解决办法是:**在一个地方集中定义所有子目录的绝对路径变量**,其它 Makefile 只引用变量名。Bedrock 里承担这个角色的就是根目录的 `dir_list.mk`——一个只有 18 行的"目录常量表"。

#### 4.2.2 核心流程

`dir_list.mk` 最巧妙的地方在于它能**自己找到 Bedrock 的根目录**,不依赖任何环境变量。流程是:

```
1. $(lastword $(MAKEFILE_LIST))   ──►  得到"正在被 include 的这个 mk 文件"的路径
2. $(abspath ...)                  ──►  转成绝对路径
3. 赋给 MAKEF_PATH
4. $(dir $(MAKEF_PATH))            ──►  取所在目录(自带末尾的 /)
5. 赋给 MAKEF_DIR / BEDROCK_DIR
6. 其余每个子目录 = $(BEDROCK_DIR) + "子目录名"
```

因为 `dir_list.mk` 永远在 Bedrock 根目录下,所以 `MAKEF_DIR` 就等于 Bedrock 根目录,于是 `BEDROCK_DIR` 被正确点亮,其余所有 `*_DIR` 都由它派生。这样无论 Bedrock 被 clone 到哪里、被谁 include,路径都不会错。

> 名词解释:`$(MAKEFILE_LIST)` 是 GNU Make 的一个内置变量,记录当前 make 运行过程中"已经读入的所有 Makefile"列表;`lastword` 取其中最后一个,也就是"刚刚被 include 进来的那个文件"。`abspath` 把它转绝对路径。`dir` 取目录部分(含末尾斜杠)。

#### 4.2.3 源码精读

**① 自定位:找到 Bedrock 根目录**

[dir_list.mk:1-5](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk#L1-L5) —— 整个机制的"地基":

```makefile
MAKEF_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEF_DIR := $(dir $(MAKEF_PATH))
# $(MAKEF_DIR) as constructed above includes a trailing slash (/)
BEDROCK_DIR        = $(MAKEF_DIR)
```

这 5 行就是 4.2.2 里那个流程图的实现。注意第 4 行的注释特意提醒:`MAKEF_DIR` 末尾**带斜杠**,所以后面拼路径时不用再手动加 `/`(你看 `BUILD_DIR = $(BEDROCK_DIR)build-tools` 中间就没有斜杠)。

**② 子目录常量表**

[dir_list.mk:6-18](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk#L6-L18) —— 13 个目录变量,每个都是"Bedrock 根 + 子目录":

```makefile
BUILD_DIR          = $(BEDROCK_DIR)build-tools
CORDIC_DIR         = $(BEDROCK_DIR)cordic
DSP_DIR            = $(BEDROCK_DIR)dsp
CMOC_DIR           = $(BEDROCK_DIR)cmoc
RTSIM_DIR          = $(BEDROCK_DIR)rtsim
BADGER_DIR         = $(BEDROCK_DIR)badger
...
PICORV_DIR         = $(BEDROCK_DIR)soc/picorv32
```

把这张表和 README 的子系统清单对照着看,非常有意思:

- 它**覆盖**了 README 提到的大部分目录(`cordic`/`dsp`/`cmoc`/`rtsim`/`badger`/`board_support`/`fpga_family`/`projects`)。
- 它**额外**点名了 README 没列的目录:`build-tools`(`BUILD_DIR`)、`peripheral_drivers`(`PERIPH_DRIVERS_DIR`)、`serial_io`(`SERIAL_IO_DIR`)、`soc/picorv32`(`PICORV_DIR`)、`homeless`(`HOMELESS_DIR`)。

> `homeless`(无家可归)是个有趣的目录名:它通常用来临时安放还没找到合适归属的模块。`BUILD_DIR`(即 `build-tools/`)则是整个构建方法学的"工具箱",里面有 `top_rules.mk`(通用 Make 规则)、`newad.py`(寄存器映射生成)、`cdc_snitch.py`(跨域检查)等。这些都会在单元 2 详细讲。

所以,**把 README 和 dir_list.mk 拼在一起,你才得到 Bedrock 顶层结构的完整地图。**

#### 4.2.4 代码实践

**实践目标:** 亲手验证 `dir_list.mk` 的"自定位"机制真的不依赖环境变量,并学会在子目录的 Makefile 里追踪这些变量。

**操作步骤(纯 Make 实验,不需要任何 FPGA 工具):**

1. 在仓库根目录执行下面这条命令,让 Make 把 `dir_list.mk` 里某个变量的值打印出来:

   ```bash
   make -f dir_list.mk -p 2>/dev/null | grep '^DSP_DIR'
   ```

   (这里 `-f dir_list.mk` 指定只读这个文件,`-p` 打印数据库,`grep` 筛出 `DSP_DIR` 这一行。)

2. 再换一个变量试试,例如 `grep '^PICORV_DIR'`,确认它确实指向 `.../soc/picorv32`。

3. 任选一个子目录,例如 `cordic/`,打开它的 `Makefile`(本讲只要扫一眼即可),搜索 `dir_list.mk`,看它是如何 `include` 这个文件、从而拿到 `CORDIC_DIR` 等变量的。

**需要观察的现象:**

- `DSP_DIR` 的值是一个**绝对路径**,末尾是 `/dsp`(没有多余的斜杠,因为 `BEDROCK_DIR` 已经带了一个)。
- 即便你把整个 Bedrock 目录改名或移动,只要重新执行,`DSP_DIR` 会自动指向新的正确位置——这就是自定位的好处。

**预期结果:** 你能在不安装任何工具的前提下,仅凭 GNU Make 自身,看到 `dir_list.mk` 定义的绝对路径变量。如果 `make` 命令本身不可用,这一步标注「待本地验证」,但你仍然可以**纯靠阅读** `dir_list.mk:1-5` 理解它的自定位逻辑。

> 安全提示:本实践只是用 `make -p` **打印**变量,不编译、不仿真、不写任何文件,不会改动源码。

#### 4.2.5 小练习与答案

**练习 1:** 为什么 `dir_list.mk` 要用 `:=`(立即赋值)给 `MAKEF_PATH` 赋值,而用 `=`(延迟赋值)给 `BEDROCK_DIR` 等变量?如果反过来会怎样?

> **参考答案:** `MAKEF_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))` 必须在 `dir_list.mk` **被 include 的那一刻**就求值,否则后续别的 Makefile 再 include 别的文件,`$(MAKEFILE_LIST)` 的"最后一个"就变了,会算错。所以用 `:=` 立即求值锁死。而 `BEDROCK_DIR = $(MAKEF_DIR)` 等用 `=` 延迟展开也没有副作用,因为 `MAKEF_DIR` 一旦定下就不会变。反过来若 `MAKEF_PATH` 用 `=` 延迟展开,就有可能在错误时刻求值导致路径错误。

**练习 2:** 用 `dir_list.mk` 找出 README 子系统清单里**没有提到**的 4 个顶层目录。

> **参考答案:** `build-tools`(`BUILD_DIR`)、`peripheral_drivers`(`PERIPH_DRIVERS_DIR`)、`serial_io`(`SERIAL_IO_DIR`)、`soc/picorv32`(`PICORV_DIR`)。另外还有 `homeless`(`HOMELESS_DIR`)作为第 5 个。这正说明 dir_list.mk 比 README 的清单更全。

**练习 3:** 假设你想在 `dsp/` 的 Makefile 里引用 `cordic/` 目录下的某个文件,你应该写 `../cordic/xxx.v` 还是 `$(CORDIC_DIR)xxx.v`?为什么?

> **参考答案:** 应该写 `$(CORDIC_DIR)xxx.v`。因为 `CORDIC_DIR` 是由 `dir_list.mk` 计算出的绝对路径,不受"当前 Makefile 在哪个目录"影响,也不受目录改名影响;而 `../cordic/...` 是相对路径,一旦目录层级调整就会失效。这正是集中定义目录常量的意义。

---

## 5. 综合实践

**任务:** 把本讲学到的"README 阅读法"和"dir_list.mk 目录常量"结合起来,产出一份属于你自己的 **Bedrock 顶层地图**。

**具体做法:**

1. 打开 [README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/README.md) 的子系统清单(第 10~28 行)和 [dir_list.mk](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dir_list.mk) 的目录常量表(第 6~18 行)。
2. 整理出**一张合并表格**,列出 `dsp`、`cordic`、`badger`、`localbus`、`rtsim`、`cmoc`、`soc`、`projects` 各自负责什么。表格至少包含三列:子系统、职责(你自己的话)、出处(README 第几行 / dir_list.mk 第几行)。
3. 在表格下方,用**一句话**写出你最感兴趣的子系统,以及为什么(例如:"我最感兴趣 `badger`,因为它用纯硬件解析以太网包,我想知道它是怎么做到线速响应的")。
4. 顺手记下两个"反常"发现:(a) 哪些子系统在 README 里有但 dir_list.mk 没单列(答案应该是没有,可自行核对);(b) 哪些子系统在 dir_list.mk 里有但 README 没列(见练习 2)。

**这份地图有什么用:** 它是你后续阅读所有讲义的"导航页"。每篇讲义开头都会说"本讲深入哪个子系统",你随时可以回到这张表,知道它在整体里的位置。

> 如果你目前还没有安装任何 FPGA 工具,完全没关系——本实践是**纯文档阅读 + Make 变量打印**,不依赖 iverilog/Vivado 等任何工具。能否实际执行 `make -p` 标注为「待本地验证」即可。

---

## 6. 本讲小结

- **Bedrock 是 LBNL 多年积累的平台无关 Verilog 代码库**,核心用于 DSP / RF 控制,并附带把它落到 FPGA(如 Xilinx)所需的全部适配层。
- 顶层是一个**多子系统聚合体**:`dsp`(信号处理)、`cordic`(坐标旋转计算)、`rtsim`(RF 系统实时仿真)、`cmoc`(RF 控制器)、`badger`(以太网/IP/UDP 响应核)、`localbus`(片上总线)、`fpga_family`/`board_support`(厂家与板卡适配)、`projects`(具体上板工程)等。
- 工具链地基是 **GNU Make + iverilog + Python**;仿真用 iverilog(逐步引入 Verilator),综合用 Xilinx(逐步引入 YoSys);CI 跑全部测试,`selftest.sh` 可在本地复现一个子集。
- 设计哲学是 **"命令行批处理优先 + 厂家图形工具并存"**:厂家生成物统一放进 `_<VENDOR_NAME>` 目录(如 `_xilinx/`),两者互不干扰。
- `dir_list.mk` 是一个 18 行的"目录常量表",通过 `$(MAKEFILE_LIST)` **自定位** Bedrock 根目录,并集中定义所有子目录的绝对路径变量,让整个 Make 工程稳健可移植。
- 想看完整顶层地图,要把 **README 的子系统清单**和 **dir_list.mk 的目录常量表**拼在一起读;`soc/picorv32`、`serial_io`、`peripheral_drivers`、`build-tools` 等只在 dir_list.mk 里出现。

---

## 7. 下一步学习建议

本讲你只建立了"地图",还没真正运行任何东西。建议按这个顺序继续:

1. **u1-l2 构建与运行:工具链与 selftest** —— 先把 `make`/`iverilog`/`python` 装好,跑通 `selftest.sh` 或至少 `make -C cordic clean all`,亲手看到一个 PASS。这是建立信心的关键一步。
2. **u1-l3 目录结构与代码导航** —— 学会 `*_tb.v`、`*.gtkw`、`rules.mk` 等命名约定,以及用 grep/glob 在大型 Verilog 库里快速定位模块。
3. **u1-l4 RTL 编码规范** —— 在动手读/写代码前,先掌握 Bedrock 的命名约定(接口前缀、`_r/_d` 后缀、参数命名等),后面读源码会顺很多。
4. 在等待装环境的同时,可以**纯阅读**地浏览你最感兴趣的那个子系统的 README(例如 [badger/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/badger/README.md) 或 [dsp/README.md](https://github.com/BerkeleyLab/Bedrock/blob/235f3e3b5602790927caf62a405fce81213bb3de/dsp/README.md)),先有个直觉印象,具体细节留给后续讲义。

> 学习节奏建议:Bedrock 体量大,不要试图一次读懂所有子系统。**先把工具链跑通(单元 1)、再掌握方法学(单元 2 的 Make 测试 / localbus / 寄存器映射),之后每篇讲义只啃一个子系统**,这是本手册设计的学习路径。
