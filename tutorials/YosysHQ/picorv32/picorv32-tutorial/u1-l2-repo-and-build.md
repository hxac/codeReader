# 仓库结构与构建系统

## 1. 本讲目标

上一讲我们建立了对 PicoRV32 的整体印象：它是一个尺寸优先的 RISC-V CPU，全部逻辑集中在一个 `picorv32.v` 文件里。本讲要回答的是一个非常实际的问题：

> 拿到这个仓库之后，**目录里都有些什么？我该敲哪条命令把它跑起来？**

学完本讲你应该能够：

1. 看懂 `picorv32` 仓库的目录组织，能说出每个顶层目录的职责。
2. 读懂根目录的 `Makefile`，理解 `test` / `test_ez` / `test_wb` / `test_axi` 这一组 `test_*` 目标分别对应哪个测试台（testbench）、生成哪个 `.vvp` 文件、内部实例化了哪个 CPU 模块。
3. 理解 `picorv32.core` 这个 FuseSoC 打包文件是如何用一份声明式描述把同一份 RTL 和参数暴露给不同 EDA 工具的。

本讲是「读源码之前的热身」：不涉及 CPU 内部实现，只解决「东西在哪、怎么编译、怎么跑」。

## 2. 前置知识

在开始前，请确保你了解以下几个概念（不熟悉也没关系，下面会结合源码再讲）：

- **Makefile / make**：一个用「目标: 依赖」规则描述如何编译工程文件的工具。例如 `testbench.vvp: testbench.v picorv32.v` 表示「要生成 `testbench.vvp`，需要 `testbench.v` 和 `picorv32.v`」。
- **Verilog 仿真**：硬件描述语言 Verilog 写的代码不能直接运行，需要先用仿真器（如 Icarus Verilog）编译成可执行的 `.vvp` 文件，再用 `vvp` 命令执行。
- **测试台（testbench）**：一段不对应真实硬件、只为仿真而写的 Verilog 代码。它负责给被测模块（这里是 CPU）提供时钟、复位和内存模型，并检查输出。
- **IP 核（IP core）**：可复用的硬件模块。PicoRV32 就是一个 IP 核，可以「拷贝即用」地嵌入到更大的设计里。
- **FuseSoC**：一个开源的 HDL 包管理器/构建工具。它用 `.core` 文件描述一个 IP 核包含哪些源文件、支持哪些参数、可以在哪些工具下跑，然后帮你生成对应工具的命令。

如果你已经会 `make` 和基本 Verilog 仿真，可以直接跳到第 3 节。

## 3. 本讲源码地图

本讲涉及的关键文件不多，但每一个都决定了「怎么跑」：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md) | 项目的「说明书」。其中 *Files in this Repository* 一节逐目录解释了仓库结构。 |
| [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile) | 根目录唯一的构建入口。定义了 `test_*` 测试目标族、固件编译链、工具链构建、形式化验证和清理规则。 |
| [picorv32.core](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core) | FuseSoC（CAPI=2）打包文件，把 RTL、各测试台和参数以工具无关的方式声明出来。 |
| `picorv32.v` | 全部 CPU 逻辑所在的文件（本讲只看它「有哪些模块」，不读实现）。 |
| `testbench*.v` / `testbench.cc` | 一组测试台，分别驱动不同总线变体的 CPU。 |

> 说明：本仓库所有永久链接都以提交 `87c89acc18994c8cf9a2311e871818e87d304568` 为基准，行号据此版本标注。

## 4. 核心概念与源码讲解

### 4.1 目录结构总览

#### 4.1.1 概念说明

一个硬件 IP 项目的仓库通常不止有 RTL（寄存器传输级代码），还会有：测试固件（运行在 CPU 上的 C/汇编程序）、测试台（驱动仿真的 Verilog）、基准测试、示例 SoC、以及针对各种综合工具的脚本。PicoRV32 就是这种「麻雀虽小五脏俱全」的结构——核心 RTL 只有一个文件，但围绕它的工程文件分布在不同目录里。

README 的 *Files in this Repository* 一节就是官方的目录导读，先看它最高效。

#### 4.1.2 核心流程

理解目录结构的推荐顺序：

1. 先读 README 的目录导读，建立「目录 → 职责」的初步映射。
2. 再用 `git ls-files`（或文件管理器）看每个目录下实际有哪些文件。
3. 区分**受版本管理的源文件**（`git ls-files` 能列出）和**构建产物**（由 `make` 生成，会被 `make clean` 删除）。

仓库顶层可以归纳成下表（结合 README 与 `git ls-files` 整理）：

| 路径 | 职责（README 原文/转述） |
| --- | --- |
| `picorv32.v` | 全部 CPU 模块，含 `picorv32` / `picorv32_axi` / `picorv32_wb` / 适配器与 PCPI 乘除法核。**拷贝即用**。 |
| `testbench.v` | 主测试台：包装 `picorv32_axi`，带一个 AXI 内存模型。 |
| `testbench_ez.v` | 最小测试台：直接实例化原生接口的 `picorv32`，内存指令内置，**不需要工具链/firmware**。 |
| `testbench_wb.v` | Wishbone 版测试台：实例化 `picorv32_wb`。 |
| `testbench.cc` | Verilator（C++）版测试台。 |
| `showtrace.py` | 把仿真产生的 trace 文件解码回汇编指令。 |
| `firmware/` | 测试固件源码（C/汇编）、`makehex.py`、链接脚本 `sections.lds`。 |
| `tests/` | 来自 `riscv-tests` 的指令级测试（`rv32ui`，每条指令一个 `.S`）。 |
| `dhrystone/` | 跑 Dhrystone 基准测试的固件。 |
| `picosoc/` | 一个完整示例 SoC，可从 SPI flash 直接执行代码。 |
| `scripts/` | 面向不同工具/架构的脚本与示例（vivado、quartus、icestorm、smtbmc、yosys、torture 等）。 |
| `picorv32.core` | FuseSoC 打包文件。 |
| `Makefile` | 构建/测试入口。 |
| `shell.nix` | Nix 可复现环境的声明文件。 |
| `COPYING` | ISC 许可证。 |

#### 4.1.3 源码精读

README 把 `picorv32.v` 里包含的模块列成了一张表，这是理解「同一个文件提供多个变体」的关键：

[README.md:89-103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L89-L103) —— 这段用表格说明 `picorv32.v` 内含七个模块：CPU 主体 `picorv32`、AXI 变体 `picorv32_axi`、从原生接口桥接到 AXI4-Lite 的 `picorv32_axi_adapter`、Wishbone 变体 `picorv32_wb`，以及三个 PCPI 乘除法协处理器 `picorv32_pcpi_mul` / `picorv32_pcpi_fast_mul` / `picorv32_pcpi_div`。README 紧接着的一句 "Simply copy this file into your project."（[README.md:103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L103)）点明了它的分发哲学：不需要安装、没有子依赖，复制单个文件就能用。

README 对测试台和各目录的导读在这里：

[README.md:105-118](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L105-L118) —— 介绍 `make test` 跑标准测试台 `testbench.v`，并提到 `make test_ez` 跑不需要固件的极简测试台 `testbench_ez.v`，还提醒需要较新版本的 Icarus Verilog。

[README.md:120-142](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L120-L142) —— 逐目录介绍 `firmware/`（测试固件）、`tests/`（riscv-tests 指令测试）、`dhrystone/`（基准）、`picosoc/`（示例 SoC）、`scripts/`（各类工具脚本）。

> 小贴士：连 README 的目录本身都可以由 Makefile 重新生成——`make toc`（见 [Makefile:172-173](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L172-L173)）用一段 `gawk` 脚本扫描 README 里的标题行并打印成 Markdown 目录条目。这从侧面说明 Makefile 在这个项目里承担了相当多「杂活」。

#### 4.1.4 代码实践

**实践目标**：把「目录」和「职责」对应起来，并学会区分源文件与构建产物。

**操作步骤**：

1. 在仓库根目录执行 `git ls-files | cut -d/ -f1 | sort -u`，列出所有受版本管理的顶层条目。
2. 对照上面那张表，给每个条目写一句中文说明。
3. 打开 [Makefile:175-182](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L175-L182) 的 `clean` 目标，看 `make clean` 会删除哪些文件。

**需要观察的现象**：

- `git ls-files` 列出的都是**源文件**，例如 `picorv32.v`、`firmware/start.S`、`testbench.v` 等。
- `make clean` 里出现的 `firmware/firmware.elf`、`firmware/firmware.hex`、`*.vvp`、`testbench_verilator` 等都**不在** `git ls-files` 里——它们是构建产物。

**预期结果**：你会清楚地看到，仓库里只有一份 RTL（`picorv32.v`）和若干测试台/固件源码；其余 `.elf`/`.bin`/`.hex`/`.vvp` 都是 `make` 临时生成的，`make clean` 后会消失。如果你当前没装工具链，`make clean` 删除的这些文件可能本来就不存在，属正常。

#### 4.1.5 小练习与答案

**练习 1**：`tests/` 目录下的 `add.S`、`lw.S` 这类文件，最终是怎么被「用上」的？
**答案**：它们被汇编成 `.o` 后链接进 `firmware/firmware.elf`。Makefile 用 `TEST_OBJS = $(addsuffix .o,$(basename $(wildcard tests/*.S)))`（[Makefile:14](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L14)）自动收集所有 `tests/*.S`，并在链接行（[Makefile:109-113](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L109-L113)）里把 `$(TEST_OBJS)` 和固件主程序一起链入。

**练习 2**：为什么 README 反复强调 `picorv32.v` 可以「Simply copy」？
**答案**：因为这个 IP 核的全部模块（CPU 三种总线变体 + AXI 适配器 + 乘除法协处理器）都集中在单一文件里，没有跨文件依赖，复制一个文件就拿到了完整功能。

---

### 4.2 Makefile 测试目标族

#### 4.2.1 概念说明

PicoRV32 的 `Makefile` 用一组 `test_*` 目标把「在哪种配置下跑哪个测试台」这件事固化了下来。理解这一组目标是「把项目跑起来」的核心——你不需要记住每条 `iverilog`/`vvp` 命令，只要记住 `make test_ez`、`make test`、`make test_wb`、`make test_axi` 这几个名字即可。

一个关键且容易踩坑的点：**`make test` 默认跑的不是「裸 `picorv32`」，而是 AXI 变体 `picorv32_axi`**。裸的、原生内存接口的 `picorv32` 只在 `testbench_ez.v` 里被直接实例化。这一点会直接影响你调试时观察到的总线信号。

#### 4.2.2 核心流程

`make test_xxx` 背后是两层规则：

1. **运行规则**（伪目标）：例如 `test_ez: testbench_ez.vvp` 声明「先确保 `.vvp` 存在，再用 `vvp` 执行它」。
2. **编译规则**：例如 `testbench_ez.vvp: testbench_ez.v picorv32.v` 声明「用 `iverilog` 把测试台和 CPU 源码编译成 `.vvp`」。

整体调用链可以用下面这张「目标 → 测试台 → .vvp → CPU 模块」图概括（本讲综合实践会让你亲手把它画完整）：

```
make test_ez   ─▶ testbench_ez.v  ─▶ testbench_ez.vvp ─▶ picorv32        （原生接口）
make test      ─▶ testbench.v     ─▶ testbench.vvp    ─▶ picorv32_axi    （经 picorv32_wrapper 包装）
make test_axi  ─▶ (复用 testbench.vvp) + plusarg +axi_test              （同上，开启随机延迟）
make test_wb   ─▶ testbench_wb.v  ─▶ testbench_wb.vvp ─▶ picorv32_wb     （Wishbone）
```

此外还有 `test_vcd` / `test_ez_vcd` / `test_wb_vcd`（额外加 `+vcd +trace` 生成波形/追踪）、`test_sp`（单端口内存）、`test_synth`（综合后门级网表仿真）、`test_verilator`（Verilator C++ 仿真）等扩展目标，它们都遵循同样的「运行规则 + 编译规则」两层结构。

#### 4.2.3 源码精读

先看四个核心运行目标。`make test` 跑标准测试台，依赖 `testbench.vvp` 和 `firmware/firmware.hex`，然后用 `vvp` 执行：

[Makefile:24-25](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L24-L25) —— `test` 目标：`$(VVP) -N $<` 中的 `$<` 是第一条依赖 `testbench.vvp`。

`make test_ez` 是对初学者最友好的入口，因为它**不依赖固件**，只要 `testbench_ez.vvp`：

[Makefile:39-40](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L39-L40) —— 注意它的依赖里没有 `firmware/firmware.hex`，这就是「无需工具链也能跑」的来源。

`make test_wb` 跑 Wishbone 版测试台：

[Makefile:33-34](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L33-L34)。

`make test_axi` **复用** `testbench.vvp`，只是多传一个 `+axi_test` plusarg 给仿真器（plusarg 是运行时传给 Verilog `$test$plusargs` 的开关）：

[Makefile:48-49](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L48-L49) —— 同一个 `.vvp`，靠 plusarg 切换行为。

再看三个编译规则，它们揭示了每个 `.vvp` 到底由哪些源文件编译而来：

[Makefile:57-59](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L57-L59) —— `testbench.vvp` 由 `testbench.v` 和 `picorv32.v` 编译；而 `testbench.v` 内部（`picorv32_wrapper` 模块）实例化的是 `picorv32_axi`，所以 `make test` 实际驱动的是 AXI 变体。命令里的 `$(subst C,-DCOMPRESSED_ISA,$(COMPRESSED_ISA))` 会把变量 `COMPRESSED_ISA = C`（[Makefile:19](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L19)）替换成 Verilog 宏定义 `-DCOMPRESSED_ISA`，即默认开启压缩指令集支持。

[Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71) —— `testbench_ez.vvp` 由 `testbench_ez.v` 和 `picorv32.v` 编译；`testbench_ez.v` 直接实例化原生接口的 `picorv32`。

[Makefile:65-67](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L65-L67) —— `testbench_wb.vvp` 由 `testbench_wb.v` 和 `picorv32.v` 编译；`testbench_wb.v` 实例化 `picorv32_wb`。

最后，所有这些目标都被声明为伪目标，避免和同名文件冲突：

[Makefile:184](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L184) —— `.PHONY` 列出所有不是真实文件的目标。

> 补充：固件相关的编译链（`.elf` → `.bin` → `.hex`）属于下一讲（u2-l1）的主题，这里只指出它的入口：`firmware/firmware.hex` 由 `firmware/firmware.bin` 经 `makehex.py` 生成（[Makefile:102-103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L102-L103)），而 `firmware.elf` 由工具链链接（[Makefile:109-113](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L109-L113)）。
>
> 关于 `test_rvf`：Makefile 的规则（[Makefile:61-63](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L61-L63)）引用了一个 `rvfimon.v`（RISC-V 形式化接口监控器），但该文件**不在本仓库**（`git ls-files` 查无），需用户自行准备，故初学者可暂时忽略此目标。

#### 4.2.4 代码实践

**实践目标**：亲手跑通唯一一个不依赖工具链的目标，并追踪它的编译规则。

**操作步骤**：

1. 确认已安装 Icarus Verilog（命令 `iverilog -V` 有输出）。若没有，先安装：Ubuntu 上 `sudo apt install iverilog`。
2. 在仓库根目录执行 `make test_ez`。
3. 观察命令行：`make` 会先执行编译规则 `testbench_ez.vvp: testbench_ez.v picorv32.v`（调用 `iverilog`），再执行运行规则（调用 `vvp`）。
4. 若想看波形，执行 `make test_ez_vcd`，会额外传 `+vcd` 并生成 `testbench.vcd`。

**需要观察的现象**：

- 第一行是 `iverilog -o testbench_ez.vvp -DCOMPRESSED_ISA testbench_ez.v picorv32.v`（由 [Makefile:69-71](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L69-L71) 展开而来），随后 `chmod -x` 去掉可执行位。
- 第二行是 `vvp -N testbench_ez.vvp`，之后是测试台打印的输出（`testbench_ez.v` 会把取指/访存地址和数据打印出来）。

**预期结果**：`make test_ez` 成功结束并打印一段地址/数据日志（具体含义在 u1-l3 详讲）。若提示找不到 `iverilog`，则需先安装 Icarus Verilog（README 在 [README.md:115-118](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L115-L118) 提醒需要较新版本）。运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`make test` 和 `make test_axi` 用的是同一个 `.vvp` 吗？它们的区别在哪？
**答案**：是同一个 `testbench.vvp`（都来自 `testbench.v` + `picorv32.v`）。区别仅在运行时：`test_axi` 多传了 `+axi_test` plusarg（[Makefile:48-49](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L48-L49)），该 plusarg 会让测试台在 AXI 事务里插入随机延迟，以更严格地检验总线接口。

**练习 2**：为什么 `make test_ez` 不需要安装 RISC-V 工具链，而 `make test` 需要？
**答案**：`make test_ez` 只依赖 `testbench_ez.vvp`（[Makefile:39-40](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L39-L40)），测试台把测试指令直接写死在 `memory[]` 数组里，不需要外部固件；而 `make test` 依赖 `firmware/firmware.hex`，后者要靠 RISC-V 工具链编译固件源码才能生成。

**练习 3**：`make test` 实际驱动的是哪一个 CPU 模块？为什么容易让人误以为是 `picorv32`？
**答案**：驱动的是 `picorv32_axi`。因为 `testbench.v` 的顶层是 `picorv32_wrapper`，它在内部实例化了 `picorv32_axi`（外加一个 AXI 内存模型）。容易误会是因为目标名叫 `test`、文件名叫 `testbench.v`，看起来像「默认/最基本」的配置，但它其实已经套上了 AXI 接口。

---

### 4.3 FuseSoC core 打包

#### 4.3.1 概念说明

`Makefile` 解决了「在本仓库里、用 Icarus/Verilator 怎么跑」的问题。但 PicoRV32 作为一个 IP 核，还会被别人集成到更大的工程里，那里可能用 Vivado、Quartus、Yosys 等不同工具，而且可能由 FuseSoC 统一管理依赖。为这种场景，项目提供了 `picorv32.core`——一份用 **CAPI=2**（FuseSoC 的 Core API 第 2 版）格式写成的声明文件。

`picorv32.core` 不含任何 RTL，它只是「元数据」：声明这个 IP 叫什么、包含哪些源文件、支持哪些可配置参数、有哪些预设目标（target）。这样别人只需一句 `fusesoc run ...` 就能在不同工具下跑起来，而不必去读 `Makefile`。

#### 4.3.2 核心流程

一份 CAPI=2 的 `.core` 文件主要由四部分构成：

1. **元信息**：`name`（IP 名 + 版本）。
2. **filesets（文件集）**：把源文件按用途分组，例如 `rtl`（综合用）、`tb`（测试用）。
3. **targets（目标）**：预设的运行配置，指定用哪个工具、包含哪些 fileset、传哪些参数、顶层模块是谁。
4. **parameters（参数）**：声明可配置项，区分它们是 Verilog 宏（`vlogdefine`）、运行时 plusarg、还是普通参数。

`picorv32.core` 里定义的目标与 Makefile 的 `test_*` 目标几乎一一对应，可以理解为「FuseSoC 版的 Makefile」。

#### 4.3.3 源码精读

文件开头声明了 CAPI 版本和 IP 名：

[picorv32.core:1-2](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core#L1-L2) —— `CAPI=2` 指定格式版本；`name : ::picorv32:0-r1` 是 FuseSoC 的命名规范（`::` 前缀表库名、`:0-r1` 表版本）。

filesets 把源文件分组，注意 `rtl` 只有 `picorv32.v` 一个文件，而各测试台各自成组：

[picorv32.core:4-20](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core#L4-L20) —— `rtl`（`picorv32.v`）、`tb`（`testbench.v`）、`tb_ez`（`testbench_ez.v`）、`tb_wb`（`testbench_wb.v`）、`tb_verilator`（`testbench.cc`，`cppSource`）。这恰好覆盖了 Makefile 编译规则里出现的所有源文件。

targets 定义了预设运行配置，与 Makefile 目标对应：

[picorv32.core:22-54](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core#L22-L54) —— 例如 `default` 只含 `rtl`（供综合用）；`lint` 用 Verilator 做纯 lint、顶层 `picorv32_axi`；`test` 用 Icarus，包含 `rtl` + `tb`（并条件性地加上 Verilator 的 `tb_verilator`），顶层在 Verilator 下是 `picorv32_wrapper`、否则是 `testbench`；`test_ez` / `test_wb` 分别对应两个简化测试台。可以看到 FuseSoC 用同一份声明同时覆盖了 Icarus 和 Verilator 两种工具。

parameters 声明可配置项，并区分它们的「类型」：

[picorv32.core:56-78](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core#L56-L78) —— `COMPRESSED_ISA` 是 `vlogdefine`（编译期 Verilog 宏，对应 Makefile 里的 `-DCOMPRESSED_ISA`）；`axi_test` / `firmware` / `noerror` / `trace` / `vcd` / `verbose` 都是 `plusarg`（运行时传给仿真器的开关，对应 `+vcd`、`+axi_test` 等）。`firmware` 的类型是 `file`，说明它把固件 hex 文件路径以 plusarg 形式传进去。

#### 4.3.4 代码实践

**实践目标**：建立 `picorv32.core` 目标与 `Makefile` 目标之间的映射，理解它们是「同一件事的两种表达」。

**操作步骤**：

1. 打开 [picorv32.core:22-54](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.core#L22-L54)，把每个 target 的 `filesets`、`default_tool`、`toplevel` 填进一张表。
2. 把这张表与 4.2 节的 Makefile 目标对照：`test_ez` ↔ `make test_ez`、`test_wb` ↔ `make test_wb`、`test` ↔ `make test`。
3. （可选，需安装 FuseSoC）执行 `fusesoc run --target=test_ez picorv32`，与 `make test_ez` 的输出对比。

**需要观察的现象**：

- 两种方式调用的底层仿真器都是 Icarus Verilog，顶层模块和源文件集合也一致，差别只在「谁来组织命令行」。
- `picorv32.core` 的 `lint` target（顶层 `picorv32_axi`、Verilator lint-only）在 Makefile 里**没有直接对应**——这是 FuseSoC 版独有的便捷入口。

**预期结果**：你会得出结论——`picorv32.core` 是把 Makefile 里的 `test_*` 规则用工具无关的方式重新声明了一遍，让这个 IP 能被 FuseSoC 生态直接消费。FuseSoC 的实际运行输出**待本地验证**（取决于是否安装了 FuseSoC 及其版本）。

#### 4.3.5 小练习与答案

**练习 1**：`COMPRESSED_ISA` 在 `picorv32.core` 里是 `vlogdefine`，在 Makefile 里以什么形式出现？为什么类型不同？
**答案**：在 Makefile 里它通过 `$(subst C,-DCOMPRESSED_ISA,$(COMPRESSED_ISA))` 变成 `iverilog` 的 `-DCOMPRESSED_ISA` 命令行宏（[Makefile:57-59](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile#L57-L59)）。`vlogdefine` 正是 FuseSoC 对「编译期 Verilog 宏」的抽象，两者本质相同。

**练习 2**：`picorv32.core` 的 `rtl` fileset 为什么只有 `picorv32.v`，而不包含任何测试台？
**答案**：因为 `rtl` 是供**综合/集成**使用的文件集，只应包含可综合的 IP 源码；测试台属于仿真专用文件，被单独放在 `tb` / `tb_ez` / `tb_wb` / `tb_verilator` 里，综合时不会被带入。

---

## 5. 综合实践

把本讲三部分串起来，完成下面这个**依赖图绘制任务**（即本讲规格中要求的实践）：

**任务**：通读根 [Makefile](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/Makefile)，针对 `make test`、`make test_ez`、`make test_wb`、`make test_axi` 四个目标，画出一张「目标 → 测试台源文件 → 生成的 `.vvp` → 实例化的 CPU 模块」的完整依赖图。

**建议步骤**：

1. 对每个目标，找到它的运行规则（`test:` / `test_ez:` / `test_wb:` / `test_axi:`）和它依赖的 `.vvp`。
2. 对每个 `.vvp`，找到对应的编译规则（`testbench.vvp:` / `testbench_ez.vvp:` / `testbench_wb.vvp:`），列出编译它用的源文件。
3. 打开对应的测试台源文件，用 `grep -n "picorv32" testbench.v testbench_ez.v testbench_wb.v` 确认它实例化的到底是 `picorv32`、`picorv32_axi` 还是 `picorv32_wb`。
4. 把结果填进下表（参考答案已在前文 4.2.2 节的图中，建议先自己填再核对）：

| make 目标 | 依赖的 .vvp | 编译该 .vvp 的源文件 | 实例化的 CPU 模块 | 是否需要 firmware.hex |
| --- | --- | --- | --- | --- |
| `test` | | | | |
| `test_axi` | | | | |
| `test_ez` | | | | |
| `test_wb` | | | | |

**预期结果（自检）**：

- `test` 与 `test_axi` 共享 `testbench.vvp`（编译自 `testbench.v` + `picorv32.v`），实例化 `picorv32_axi`；区别只在 `+axi_test` plusarg。
- `test_ez` 用 `testbench_ez.vvp`（编译自 `testbench_ez.v` + `picorv32.v`），实例化原生 `picorv32`，**不需要** `firmware.hex`。
- `test_wb` 用 `testbench_wb.vvp`（编译自 `testbench_wb.v` + `picorv32.v`），实例化 `picorv32_wb`，需要 `firmware.hex`。

**进阶**：把 `test_sp`、`test_synth`、`test_verilator` 也补进图里，并标注它们各自特殊的地方（单端口内存 / 综合后网表 / Verilator C++）。

## 6. 本讲小结

- 仓库的核心 RTL 只有 `picorv32.v` 一个文件，其余目录（`firmware/`、`tests/`、`dhrystone/`、`picosoc/`、`scripts/`）都是围绕它的固件、测试、基准和工程脚本。
- 构建与测试的入口是根 `Makefile`；受版本管理的是源文件，`.elf`/`.bin`/`.hex`/`.vvp` 等都是 `make` 产物，会被 `make clean` 清除。
- `test_*` 目标族遵循「运行规则 + 编译规则」两层结构；`make test_ez` 是唯一不依赖 RISC-V 工具链的入口，最适合初学者先跑通。
- 一个关键事实：`make test` 驱动的是 **`picorv32_axi`**（经 `picorv32_wrapper` 包装），裸 `picorv32` 只在 `testbench_ez.v` 中被直接使用；`make test_axi` 与 `make test` 共用同一 `.vvp`，仅多一个 `+axi_test` plusarg。
- `picorv32.core` 是 FuseSoC（CAPI=2）打包文件，用 filesets/targets/parameters 把同一份 RTL 和参数以工具无关的方式重新声明，相当于「FuseSoC 版的 Makefile」。
- README 的 *Files in this Repository* 一节是官方目录导读；连 README 的目录都可以由 `make toc` 自动生成。

## 7. 下一步学习建议

本讲解决的是「东西在哪、怎么跑」。建议下一步：

1. **动手跑**：先按 4.2.4 节执行 `make test_ez`，亲眼看到 CPU 在仿真里跑起来；具体如何阅读 `testbench_ez.v` 的时钟、复位、内建内存与打印输出，是 **u1-l3《跑起来：最小测试台 testbench_ez》** 的主题。
2. **补上工具链**：当你想跑 `make test`（需要固件）时，就需要 RISC-V 工具链；如何构建/安装工具链、把 C/汇编编译成 `.elf`/`.hex`，是 **u2-l1《RISC-V 工具链与从源码到 hex》** 的主题。
3. **进入 RTL**：在「会跑」之后，就可以从 **u3-l1《模块参数》** 开始，正式进入 `picorv32.v` 内部，先看它对外暴露的参数与端口，再逐步深入译码器与状态机。
