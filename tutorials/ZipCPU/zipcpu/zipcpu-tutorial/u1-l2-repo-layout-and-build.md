# 仓库目录结构与顶层构建系统

## 1. 本讲目标

上一讲（u1-l1）我们建立了 ZipCPU 的整体认知：它是什么、设计目标是什么、仓库里大致有哪几块。本讲我们要把这张「地图」画得更精确——

学完本讲，你应当能够：

- 说出仓库顶层 `rtl/`、`sw/`、`sim/`、`bench/`、`doc/` 五个目录各自的职责。
- 看懂顶层 `Makefile` 中 `all`、`rtl`、`sw`、`sim`、`bench`、`test`、`formal`、`doc`、`clean` 这些目标的依赖关系与作用。
- 知道构建 RTL、工具链、模拟器分别会进入哪个子目录、产出什么文件。
- 按照 `INSTALL.md` 列出构建所需的系统依赖，并理解「一次 `make` 跑不完、需要把本地安装目录加入 PATH 后再跑一次」这个特殊流程。
- 独立运行 `make rtl`，并能在产物目录里找到 Verilator 生成的模型。

本讲只讲「怎么把项目搭起来、各部分放在哪」，不深入任何一段 Verilog 逻辑。具体的 CPU 核心实现、外设、总线封装留到后续单元。

## 2. 前置知识

在开始前，请确认你理解以下几个概念（不熟悉的可以先记住结论）：

- **Make / Makefile**：一个用「目标—依赖—命令」描述如何构建工程的工具。例如 `rtl:` 是一个目标名，下面缩进的行是该目标的构建命令；`target: dep1 dep2` 表示 `target` 依赖 `dep1`、`dep2`。本仓库大量使用「顶层 Makefile 转发到子目录 Makefile」的组织方式。
- **Verilator**：一个把 Verilog RTL 翻译成 C++ 的开源工具，常用于在 CPU 软核项目中做高速仿真。本仓库的 `rtl/` 与 `sim/verilator/` 都依赖它。
- **binutils / GCC / newlib**：构成一套交叉编译工具链的三件套。binutils 提供汇编器 `zip-as`、链接器 `zip-ld` 等；GCC 提供 C 编译器 `zip-gcc`；newlib 提供 C 标准库。它们都被加上「ZipCPU 后端」后用于把 C/汇编编译成 ZipCPU 能跑的机器码。
- **ELF**：可执行与可链接格式，是 Unix 系可执行文件的常见格式。本仓库的测试程序最终都被链接成 ELF。
- **形式化验证（formal）/ SymbiYosys**：一种用数学证明（而非跑测试用例）来验证硬件模块正确性的方法。本讲只需知道 `bench/formal/` 里放的是这类证明的配置（`.sby` 文件）。
- **load/store 架构、软核 CPU、ISA**：上一讲已介绍，本讲不再重复。

> 提示：本讲会频繁引用真实文件的行号。你可以对照永久链接在浏览器里打开，逐行核对。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。注意：每个子目录里都有自己的 `Makefile`，构成一套「顶层转发 + 子目录执行」的层级构建系统。

| 文件 | 作用 |
|------|------|
| [Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile) | 顶层 Makefile，定义 `all/rtl/sw/sim/bench/test/formal/doc/clean` 等目标，并把工作转发到各子目录。 |
| [INSTALL.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md) | 安装说明：列出系统依赖、依赖安装命令，以及构建流程的注意事项。 |
| [sw/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile) | 工具链构建：binutils、GCC、newlib 的解包、打补丁、配置、编译、安装。 |
| [sim/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/Makefile) | 仿真入口：构建 Verilator 测试台并运行全部仿真测试。 |
| [rtl/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile) | RTL 的 Verilator 构建：用多组 `OPT_*` 配置参数编译四种顶层封装。 |
| [bench/asm/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/Makefile) | 汇编测试程序构建（`hellosim`、`simuart`、`simtest`、`cmptest`）。 |
| [bench/formal/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile) | 形式化验证：定义 `TESTS` 列表，逐个组件跑 SymbiYosys 证明。 |
| [bench/cpp/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/cpp/Makefile) | 遗留 C++ 测试程序 `helloworld`（仿真已迁移至 `sim/verilator`）。 |
| [sim/verilator/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile) | 基于 RTL 的 Verilator 模拟器，产出 `zipsys_tb`、`zipbones_tb` 等可执行测试台。 |

## 4. 核心概念与源码讲解

### 4.1 仓库目录整体组织

#### 4.1.1 概念说明

ZipCPU 是一个「大而全的单仓库（monorepo）」项目：CPU 的硬件实现、配套工具链、模拟器、测试与验证、规范文档全部放在同一个 git 仓库里。理解这种组织方式是后续阅读任何代码的前提——你必须先知道「我要找的东西在哪个目录」。

顶层一共五个核心目录（外加一个语法高亮文件 `zip.vim`）：

| 目录 | 职责 |
|------|------|
| `rtl/` | Verilog 硬件源码：CPU 核心（`core/`）、总线辅助模块（`ex/`）、外设（`peripherals/`）、AXI DMA（`zipdma/`），以及四种顶层封装 `zipsystem.v` / `zipbones.v` / `zipaxil.v` / `zipaxi.v`。 |
| `sw/` | 工具链：binutils、GCC、newlib 的源码包与补丁（`*-zippatch.patch`）和构建脚本（`gas-script.sh` / `gcc-script.sh` / `nlib-script.sh`），以及遗留汇编器 `zasm/` 和调试器 `zipdbg/`。 |
| `sim/` | 两套模拟器：`sim/cpp`（独立于 RTL 的 C++ 指令级模拟器）与 `sim/verilator`（基于 RTL 的 Verilator 模拟器）；还有示例程序目录 `sim/zipsw` 与仿真用 RTL 设备 `sim/rtl`。 |
| `bench/` | 测试与验证：`bench/asm`（汇编测试程序）、`bench/cpp`（遗留 C++ 测试）、`bench/formal`（SymbiYosys 形式化验证）、`bench/rtl`（仿真用内存设备与 MMU 测试台）、`bench/mcy`（变异覆盖）。 |
| `doc/` | 规范文档：`spec.tex`/`spec.pdf`（ISA 规范）、GPL 许可、历年的会议论文 PDF 等。 |

一句话记忆：**`rtl/` 造硬件、`sw/` 造工具链、`sim/` 做模拟、`bench/` 做验证、`doc/` 写规范。**

#### 4.1.2 核心流程

项目的总体构建流程可以用下面这张「依赖箭头图」表示：

```
            ┌──── sw/ ──── 产出: 工具链 (zip-gcc / zip-as / zip-ld ...)
            │
 make all ──┼──── rtl/ ──── 产出: Verilator 模型 (rtl/obj_dir/V*.a)
            │
            └──── sim/ ──── 产出: 模拟器 (sim/cpp, sim/verilator/zipsys_tb)
                   │
                   ├─ cppsim  → sim/cpp
                   └─ vsim    → 依赖 rtl，再构建 sim/verilator
```

关键依赖关系（自下而上理解）：

1. **工具链 `sw/`** 是基础——没有 `zip-gcc` 就没法把任何 C/汇编编译成 ZipCPU 程序。
2. **RTL `rtl/`** 只依赖 Verilator，与工具链相互独立。
3. **模拟器 `sim/verilator`** 既要 RTL 模型（`vsim` 依赖 `rtl`），也常需要工具链来编译被测程序。
4. **测试 `bench`** 依赖 `rtl` 与 `sw`；**完整测试 `test`** 还要 `sim` 和 `formal`。

#### 4.1.3 源码精读

顶层目录布局可以直接通过 `ls` 看到：核心子目录就是 `rtl/`、`sw/`、`sim/`、`bench/`、`doc/`，加上两个 markdown 文档 `README.md`、`INSTALL.md` 和顶层 `Makefile`。

`INSTALL.md` 对每个目录的角色有一段直接说明，值得逐条对照：

- 关于工具链：仓库包含被所有实现共用的 [binutils 与带 ZipCPU 后端的 GCC](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L6)（「*This is very necessary.*」）。
- 关于模拟器：仓库包含两套模拟器——[一套独立于 RTL（`sim/cpp`），另一套基于 RTL（`sim/verilator`）](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L14)。
- 关于 RTL：[Verilog RTL 描述了 ZipCPU](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L12)，如果你只是想用某个现成的 SoC 实现，可以忽略这个目录。

这些说明与我们上面的目录表完全一致，说明我们对仓库结构的理解是正确的。

#### 4.1.4 代码实践

**实践目标**：亲手确认仓库的目录结构，并把每个目录与它的职责对应起来。

**操作步骤**：

1. 在仓库根目录执行 `ls -F`，列出顶层条目。
2. 进入 `rtl/`，执行 `ls`，观察它内部还有 `core/`、`ex/`、`peripherals/`、`zipdma/` 等子目录与四个顶层 `.v` 文件。
3. 进入 `sw/`，执行 `ls`，观察其中的 `.tar.bz2` 源码包、`*-zippatch.patch` 补丁文件、`*-script.sh` 构建脚本。
4. 进入 `bench/`，执行 `ls -F`，观察 `asm/`、`cpp/`、`formal/`、`rtl/`、`mcy/` 五个子目录。

**需要观察的现象**：顶层恰好有 `rtl/ sw/ sim/ bench/ doc/` 五个核心目录；`sw/` 里能看到 `binutils-2.27.tar.bz2`、`gcc-zippatch.patch` 这类工具链相关文件；`bench/` 里能看到 `formal/` 下大量 `.sby` 文件（形式化证明配置）。

**预期结果**：与「4.1.1」的目录表一一对应。若 `sw/` 中看不到编译后的 `install/` 目录，那是正常的——它只有在运行过 `make sw` 之后才会生成（详见 4.3.1）。

#### 4.1.5 小练习与答案

**练习 1**：如果你只想看 CPU 的硬件实现，应该进入哪个目录？想编译一段 C 代码为 ZipCPU 程序，又该用哪个目录产出的工具？

> **参考答案**：看硬件实现进入 `rtl/`（核心逻辑在 `rtl/core/`）；编译 C 代码用 `sw/` 产出的 `zip-gcc`，它被安装到 `sw/install/cross-tools/bin/`。

**练习 2**：`bench/formal/` 与 `bench/asm/` 的产物形态有什么本质区别？

> **参考答案**：`bench/asm/` 产出的是**可执行程序**（ELF，由 `zip-gcc`/`zip-as`/`zip-ld` 编译链接），用来在模拟器上跑；`bench/formal/` 产出的不是程序，而是形式化证明的**通过/失败结论**（由 SymbiYosys 对每个 `.sby` 配置求解得到）。

### 4.2 顶层 Makefile 的构建目标

#### 4.2.1 概念说明

顶层 `Makefile` 本身几乎不含真正的构建命令，它的核心职责是**调度**——把不同性质的构建工作转发到对应的子目录。理解它等于掌握了一张「我想做 X，该去哪」的索引表。

这里要先理解一个本仓库反复出现的写法。顶层 Makefile 定义了一个转发用的变量：

```make
SUBMAKE := $(MAKE) --no-print-directory -C
```

随后大量目标写成 `$(SUBMAKE) 子目录/` 的形式，含义是「切到该子目录并在那里执行 `make`」。这正是层级构建系统的典型模式。

#### 4.2.2 核心流程

顶层目标及其依赖关系汇总如下表（按 `Makefile` 中出现的顺序）：

| 目标 | 依赖 | 进入的子目录 | 作用 |
|------|------|--------------|------|
| `all`（默认） | `rtl sw sim` | — | 构建三大块：RTL、工具链、模拟器 |
| `doc` | — | `doc/` | 重建规范 PDF 等 |
| `formal` | — | `bench/formal/` | 跑形式化证明，再生成 report |
| `rtl` | — | `rtl/` | 对 RTL 跑 Verilator |
| `sw` | — | `sw/` | 构建工具链（binutils/GCC/newlib） |
| `sim` | `cppsim vsim` | `sim/cpp` 与 `sim/verilator` | 构建两套模拟器 |
| `cppsim` | — | `sim/cpp` | 构建独立 C++ 指令级模拟器 |
| `vsim` | `rtl` | （先）`rtl/`，再 `sim/verilator` | 构建基于 RTL 的 Verilator 模拟器 |
| `clean` | — | 各子目录 | 清理全部产物 |
| `bench` | `rtl sw` | `bench/asm` | 构建汇编测试程序 |
| `test` | `bench sim formal` | `sim/` | 跑完整测试套件 |

一个关键细节：`vsim` 依赖 `rtl`，意味着构建 Verilator 模拟器时会**先**自动构建 RTL 模型——这就是为什么「跑模拟」往往隐含了「先建 RTL」。

#### 4.2.3 源码精读

默认目标 `all` 一目了然地列出三大块：

```make
all: rtl sw sim
```
这是顶层 [Makefile 第 57–58 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L57-L58) 所定义的内容：`make` 不带参数等价于 `make all`，会依次构建 RTL、工具链、模拟器。

`rtl` 目标的工作就是把控制权交给 `rtl/` 子目录：

```make
rtl:
	@echo "Building rtl for Verilator";
	+@$(SUBMAKE) rtl/
```
见 [Makefile 第 78–83 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L78-L83)。注意命令前的 `+` 号：它告诉 make「这条命令可以安全地传递 jobserver（并行构建）信息给子 make」。`sw`（[L85–L90](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L85-L90)）和 `doc`（[L63–L68](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L63-L68)）、`formal`（[L70–L76](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L70-L76)）的结构完全一致，只是目标子目录不同。

`sim` 目标最值得细看，因为它展示了「复合依赖 + 子目标」的写法：

```make
sim:	cppsim vsim

cppsim:
	@echo "Building in C++ simulator";
	+@$(SUBMAKE) sim/cpp

vsim: rtl
	@echo "Building Verilator simulator";
	+@$(SUBMAKE) sim/verilator
```
见 [Makefile 第 92–103 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L92-L103)。`vsim: rtl` 这一行就是「构建 Verilator 模拟器前先确保 RTL 已构建」的来源。

完整测试目标 `test` 把前面几乎所有东西串起来：

```make
test: bench sim formal
	@echo "Running simulation test suite"; $(SUBMAKE) sim/ test
```
见 [Makefile 第 123–127 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L123-L127)。它依赖 `bench`（本身又依赖 `rtl sw`）、`sim`、`formal`，最后在 `sim/` 里跑 `test`。所以 `make test` 是一个「全链路」入口。

`clean` 目标把所有会产出文件的子目录都清理一遍，见 [Makefile 第 105–115 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L105-L115)：

```make
clean:
	+@$(SUBMAKE) rtl           clean
	+@$(SUBMAKE) sw            clean
	+@$(SUBMAKE) sim/cpp       clean
	+@$(SUBMAKE) sim/verilator clean
	+@$(SUBMAKE) bench/asm     clean
	+@$(SUBMAKE) bench/cpp     clean
	+@$(SUBMAKE) bench/formal  clean
```

#### 4.2.4 代码实践

**实践目标**：用「源码阅读型实践」验证你对顶层目标依赖关系的理解，不实际编译。

**操作步骤**：

1. 打开顶层 [Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile)。
2. 找到 `test:` 目标（[L123–L127](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L123-L127)），列出它的三个依赖：`bench`、`sim`、`formal`。
3. 对每个依赖继续追溯：`bench`（[L117–L121](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L117-L121)）依赖 `rtl sw`；`sim`（[L92–L103](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L92-L103)）依赖 `cppsim vsim`，而 `vsim` 又依赖 `rtl`。
4. 画出最终的依赖树。

**需要观察的现象**：`make test` 会触发 `rtl`、`sw`、`sim/cpp`、`sim/verilator`、`bench/asm`、`bench/formal` 几乎所有子目录的构建，最后才在 `sim/` 里真正「跑」测试。

**预期结果**：你应当得出结论——**`make test` 是最重的入口**，它隐含了整个工具链、RTL、模拟器的构建。如果你只想快速验证某一部分，应该用更小的目标（如 `make rtl`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vsim` 要显式依赖 `rtl`，而 `cppsim` 不需要？

> **参考答案**：`vsim` 对应的 `sim/verilator` 模拟器是把 RTL 翻译成 C++ 来跑的，必须先有 Verilator 处理后的 RTL 模型（`rtl/obj_dir/`）；而 `cppsim`（`sim/cpp`）是一个用 C++ 独立实现的指令级模拟器，不依赖任何 RTL，所以不需要先构建 `rtl`。

**练习 2**：`make bench` 与 `make test` 的区别是什么？

> **参考答案**：`make bench`（[L117–L121](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L117-L121)）只**构建**汇编测试程序（依赖 `rtl sw`）；`make test` 不仅构建（依赖 `bench sim formal`），还在 `sim/` 里**运行**整套仿真测试。前者是「编译」，后者是「编译 + 运行」。

**练习 3**：如果你不小心构建了一堆产物想全部清掉，该用什么命令？它会清理哪些目录？

> **参考答案**：用 `make clean`（[L105–L115](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L105-L115)），它会进入 `rtl/`、`sw/`、`sim/cpp`、`sim/verilator`、`bench/asm`、`bench/cpp`、`bench/formal` 七个子目录分别执行 `clean`。

### 4.3 子目录 Makefile 与构建产物

顶层只负责「去哪个子目录」，真正的构建逻辑都在子目录里。这一节我们看三个最重要的子目录 Makefile，搞清楚它们各自产出什么。

#### 4.3.1 工具链构建：sw/Makefile

**概念**：`sw/Makefile` 把 binutils、GCC、newlib 三套上游源码「打补丁 + 配置 + 编译 + 安装」到本地目录 `sw/install/`。这是整个项目里最耗时、也最特殊的一步。

**核心流程**：

1. 解压 `binutils-2.27.tar.bz2`，打上 `gas-zippatch.patch`，配置（`gas-script.sh`）后编译安装。
2. 解压 GCC 源码，打上 `gcc-zippatch.patch`，分「host」与「依赖已建编译器的库」两阶段构建。
3. 解压 newlib，打上 `nlib-zippatch.patch` 后构建安装。
4. 全部安装到 `$(INSTALLD)/cross-tools/bin/`，即 `sw/install/cross-tools/bin/`。

**源码精读**：版本号与安装目录定义在 [sw/Makefile 第 80–83 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L80-L83)：

```make
BINUTILSD:=binutils-2.27
GCCD:=gcc-10.3.0
NLIBD:=newlib-4.1.0
export INSTALLD:=$(shell pwd)/install
```

注意这里有几个值得留意的版本号：binutils 是 2.27，GCC 是 **10.3.0**（与 `INSTALL.md` 第 6 行的 `gcc-6.2.0-zip` 说法不同——以源码里的 `GCCD:=gcc-10.3.0` 为准，文档里的旧版本号已过时），newlib 是 4.1.0。`INSTALLD` 用 `$(shell pwd)` 取得 `sw/` 的绝对路径，所以安装目录是 `sw/install/`。

默认目标 `install`（[sw/Makefile 第 73–76 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L73-L76)）拆解为：

```make
install: basic-install
all: basic-install binutils-pdf-install gcc-pdf-install
basic-install: gas-install gcc-install newlib-install
build: gas gcc-all nlib
```

也就是说 `make sw` 实际跑的是 `gas-install → gcc-install → newlib-install` 三连，最终产出 `zip-gcc`、`zip-as`、`zip-ld` 等命令，位于 [sw/install/cross-tools/bin/](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L32)。

`binutils`（[L114–L118](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L114-L118)）、`gcc`（[L233–L243](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L233-L243)）、`newlib`（[L276–L294](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sw/Makefile#L276-L294)）三个目标分别对应三件套，结构高度相似。

**特殊流程提示**：`INSTALL.md` 明确警告，[一次 `make` 会中途失败](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L32)，需要把本地安装目录 `sw/install/cross-tools/bin` 加入 `PATH` 后**重新 `make`**，它会从断点继续。这是因为 GCC 的某些阶段需要用到前一步刚安装好的 `zip-*` 工具。

#### 4.3.2 RTL 构建：rtl/Makefile 与产物路径

**概念**：`rtl/Makefile` 用 Verilator 把 RTL 编译成 C++ 仿真模型，且会用**多组不同的 `OPT_*` 参数**重复编译同一段 RTL，以验证不同配置（如是否流水线、是否带缓存）都能通过 Verilator。

**核心流程**：

1. 定义若干「预设配置」（`ASMCFG`、`TRAPCFG`、`MINCFG`、`PIPECFG`、`CACHECFG`、`LWPWR`），每组是一串 `-GOPT_XXX=...` 参数。
2. 对每种顶层封装（`zipsystem`、`zipbones`、`zipaxil`、`zipaxi`），用每组配置分别调用 Verilator，生成带不同前缀的模型。
3. 产物统一放在 `rtl/obj_dir/`，命名形如 `V<前缀>.cpp`、`V<前缀>.h`、`V<前缀>__ALL.a`。

**源码精读**：默认目标列出八个最终产物（[rtl/Makefile 第 40–42 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L40-L42)）：

```make
all: zipbones zipsystem zipaxil zipaxi div zipmmu cpuops pfcache
```

Verilator 的关键设置在 [rtl/Makefile 第 127–139 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L127-L139)：

```make
VOBJ := obj_dir
...
VFLAGS := -Wall -MMD --trace $(VCOVERAGE) -cc -y $(CORED) -y $(PRPHD) -y $(EXD) -y $(DMAD)
```

`VOBJ := obj_dir` 正是产物所在目录——也就是「4.2」中 `make rtl` 的产物路径 **`rtl/obj_dir/`**。`-y core -y peripherals -y ex -y zipdma` 告诉 Verilator 去这些子目录查找 Verilog 模块。

以 `zipsystem` 为例，它内部又派生出六个配置变体（[rtl/Makefile 第 188–197 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L188-L197)）：

```make
zipsystem: sysasm systrap sysmin syspipe syscache syslp zipsys

sysasm: $(VOBJ)/Vsysasm__ALL.a
$(VOBJ)/Vsysasm.cpp: $(SYSSRC)
	$(VERILATE) $(ASMCFG) --prefix Vsysasm -cc zipsystem.v
$(VOBJ)/Vsysasm__ALL.a: $(VOBJ)/Vsysasm.cpp $(VOBJ)/Vsysasm.h
	$(SUBMAKE) Vsysasm.mk
```

`sysasm` 用 `ASMCFG`（最简配置，关掉流水线/缓存/乘除/用户模式等）编译 `zipsystem.v`，产物前缀是 `Vsysasm`，最终归档为 `rtl/obj_dir/Vsysasm__ALL.a`。其余变体（`systrap`、`sysmin`、`syspipe`、`syscache`、`syslp`、`zipsys`）只是换一组 `OPT_*` 参数和前缀。

> 关键结论：**`make rtl` 的产物在 `rtl/obj_dir/` 目录下**，是 Verilator 生成的 C++ 模型源码（`.cpp`/`.h`）与编译归档（`V*__ALL.a`）。它们本身还不是可运行的模拟器——可运行的模拟器由 `sim/verilator` 在此基础上链接得到（见下一节）。

#### 4.3.3 模拟器入口：sim/Makefile 与 sim/verilator

**概念**：`sim/Makefile` 是仿真的总入口，它先构建 Verilator 测试台可执行文件，再驱动一组测试用例运行。

**核心流程**（[sim/Makefile 第 43–57 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/Makefile#L43-L57)）：

```make
all: test

host:
	make -C verilator
	make -C verilator test

zipsw: host
	make -C zipsw

test: zipsw
	perl sim_run.pl verilator all
```

读法：默认目标 `all` 就是 `test`；`test` 依赖 `zipsw`；`zipsw` 依赖 `host`；`host` 进入 `verilator/` 构建测试台并跑它的 `test`。最终用 Perl 脚本 `sim_run.pl` 驱动 `verilator` 跑「all」测试集。

真正生成可执行模拟器的是 `sim/verilator/Makefile`。它的默认目标在文件开头注释里写得很清楚（[sim/verilator/Makefile 第 13–28 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L13-L28)）：构建两个测试台程序 `zipsys_tb`（基于 ZipSystem）和 `zipbones_tb`（基于 ZipBones）。注释还说明了测试判据——[HALT 表示成功，BUSY 表示失败](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L38-L40)，以及三种运行模式 `stest`/`itest`/`test`（[L42–L50](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/sim/verilator/Makefile#L42-L50)）。

所以 `make sim` 的最终产物是 `sim/verilator/` 下的可执行测试台（如 `zipsys_tb`），它们链接了 `rtl/obj_dir/` 里的模型与 `zipcpu_tb.cpp` 等 C++ 驱动代码。这也是 `vsim` 必须依赖 `rtl` 的根本原因。

#### 4.3.4 代码实践：运行 `make rtl` 并定位产物

这是本讲的核心代码实践任务。

**实践目标**：单独构建 RTL，确认 `make rtl` 的产物路径，并核对三种 bench 子目录各自的产物形态。

**操作步骤**：

1. 先确认依赖已安装（见 4.4，至少要有 Verilator）。在仓库根目录执行：

   ```bash
   make rtl
   ```

   按顶层 [Makefile 第 78–83 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L78-L83)，它会进入 `rtl/` 执行该目录的默认 `all` 目标（[rtl/Makefile 第 40–42 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L40-L42)）。

2. 构建完成后，列出产物目录：

   ```bash
   ls rtl/obj_dir/
   ```

3. 记录其中以 `V` 开头的文件名，例如 `Vsysasm__ALL.a`、`Vzipbones__ALL.a`、`Vdiv.h` 等。

4. 查看三个 bench 子目录的 Makefile 头部说明，回答它们各自产出什么：
   - `bench/asm/Makefile`（[第 67–69 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/asm/Makefile#L67-L69)）：`PROGRAMS := hellosim simuart simtest cmptest`。
   - `bench/formal/Makefile`（[第 67–72 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/formal/Makefile#L67-L72)）：`TESTS := prefetch dblfetch ...` 一长串组件名。
   - `bench/cpp/Makefile`（[第 60 行附近](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/cpp/Makefile#L60-L61)）：默认目标 `helloworld`。

**需要观察的现象**：

- `rtl/obj_dir/` 里出现大量 `V*.cpp`、`V*.h`、`V*__ALL.a`、`V*.mk` 文件，前缀对应不同封装与配置（`Vsysasm`、`Vsystrap`、`Vbonespipe`、`Vaxilmin`、`Vdiv`、`Vcpuops`、`Vpfcache` 等）。
- 终端会打印 `Building rtl for Verilator`，随后是 Verilator 与子 make 的输出。

**预期结果（产物路径与三个 bench 子目录的产物）**：

- **`make rtl` 产物路径**：`rtl/obj_dir/`，内容是 Verilator 生成的 C++ 仿真模型与归档（如 `Vzipsystem__ALL.a`）。具体出现哪些前缀取决于你本机已安装的依赖是否齐全；若 Verilator 未装则构建会失败。
- **`bench/asm` 产出**：汇编测试**可执行程序** `hellosim`、`simuart`、`simtest`、`cmptest`（ELF，用 `zip-gcc`/`zip-as`/`zip-ld` 编译），用于在模拟器上运行。
- **`bench/formal` 产出**：对 `TESTS` 列表（prefetch、dblfetch、pfcache、memops、div、cpuops、zipcore、zipbones、zipaxil…）中每个组件的**形式化证明结论**（PASS/FAIL），由各自的 `.sby` 配置驱动 SymbiYosys 求解，而非可执行程序。
- **`bench/cpp` 产出**：遗留的 `helloworld` 测试程序。注意 [bench/cpp/README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/bench/cpp/README.md) 说明该仿真已迁移到 `sim/verilator`，此目录将来可能被移除。

> **待本地验证**：以上产物路径与文件名是基于 Makefile 规则推断的预期结果。由于本讲义编写环境未实际安装 Verilator 与完整工具链，`make rtl` 是否能在你的机器上一次跑通、`rtl/obj_dir/` 中确切出现哪些前缀文件，需在你本地验证。若 `make rtl` 报缺 Verilator，请先按 4.4 安装依赖。

#### 4.3.5 小练习与答案

**练习 1**：`make rtl` 之后，`rtl/obj_dir/Vsysasm__ALL.a` 这个文件里的 `sysasm` 代表什么？

> **参考答案**：`sysasm` 是 `zipsystem` 封装在 `ASMCFG`（最简配置：关闭流水线、缓存、乘除、用户模式等）下编译出的变体，前缀 `Vsysasm` 由 `--prefix Vsysasm` 指定（[rtl/Makefile 第 194–195 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L194-L195)）。同一份 `zipsystem.v` 会因不同 `OPT_*` 参数生成多个不同的模型变体。

**练习 2**：为什么说 `make rtl` 产出的是「模型」而不是「可运行的模拟器」？

> **参考答案**：`rtl/obj_dir/` 里是 Verilator 翻译出的 C++ 类与归档库，它们描述了硬件行为，但还需要一个 C++ `main()`（即 `sim/verilator/zipcpu_tb.cpp` 等测试台）来驱动时钟、加载 ELF、观察输出并链接这些库，才能得到可执行的模拟器（如 `zipsys_tb`）。`rtl/` 只完成了前半部分。

### 4.4 安装依赖与构建流程（INSTALL.md）

#### 4.4.1 概念说明

在动手 `make` 之前，必须先准备好系统依赖。`INSTALL.md` 的「Dependencies」与「Installation instructions」两节给出了权威清单和流程。本节把这两节拆开讲清楚，避免你构建到一半才报错。

#### 4.4.2 核心流程

依赖与构建流程如下：

1. **安装系统依赖**：用 `apt` 安装一批编译期工具与库。
2. **从源码安装 Verilator**：因为对版本有要求，需要 clone 官方仓库自行编译。
3. **在仓库根目录执行 `make`**：构建工具链、RTL、模拟器。
4. **第一次 `make` 会中途失败**：把本地安装目录加入 `PATH` 后再次 `make`，从断点继续。

#### 4.4.3 源码精读

`INSTALL.md` 第 22 行列出全部系统依赖：texinfo、gcc-dev、g++、flex、bison、libbison-dev、verilator、libgmp10、libgmp-dev、libmpfr-dev、libmpc-dev、libelf-dev、bc、exuberant-ctags、libncurses-dev。

紧接着第 26–29 行给出安装命令（[INSTALL.md 第 26–29 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L26-L29)）：

```bash
sudo apt install texinfo gcc-dev g++ flex bison libbison-dev verilator libgmp10 libgmp-dev libmpfr-dev libmpc-dev libelf-dev bc exuberant-ctags libncurses-dev python3 autoconf help2man
git clone https://github.com/verilator/verilator
cd verilator
autoconf ; ./configure; make ; sudo make install
```

注意两点：一是 `apt` 行里额外加了 `python3 autoconf help2man`，这些是后面从源码编译 Verilator 所需；二是虽然 `apt` 装了一个 `verilator` 包，但作者仍要求从源码安装——这是因为后续测试对 Verilator 版本有要求（仓库顶层注释提到需 v5.007 或更高）。

构建流程说明在第 32–34 行（[INSTALL.md 第 32–34 行](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L32-L34)）：在主目录敲 `make` 会把工具构建并放进本地安装目录 `sw/install/cross-tools/bin`；但**这个过程会中途失败**，解决办法是把该安装目录加入 `PATH` 后重新 `make`，它会接着上次继续。同时强调 ZipCPU 构建过程**不会向系统全局安装任何东西**，所有产物都留在本地 `sw/install`。

#### 4.4.4 代码实践

**实践目标**：把 `INSTALL.md` 的依赖清单与构建注意事项整理成一份可照做的清单。

**操作步骤**：

1. 打开 [INSTALL.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md)，定位第 22 行的依赖列表与第 26–29 行的安装命令。
2. 在你本机执行第 26 行的 `apt install`（需要 sudo 与 Ubuntu/Debian 系发行版；其他发行版请替换为对应的包管理器与包名）。
3. 按第 27–29 行从源码编译安装 Verilator。
4. 准备好把 `sw/install/cross-tools/bin` 加入 `PATH` 的命令，例如：

   ```bash
   export PATH=$PATH:$(pwd)/sw/install/cross-tools/bin
   ```

**需要观察的现象**：`apt install` 与 Verilator 的 `make install` 顺利完成；`sw/install/cross-tools/bin` 目录在第一次 `make` 部分成功后出现。

**预期结果**：依赖就绪后，`make rtl`（不依赖工具链）通常能直接跑通；而 `make sw` 在第一次会中途停下，加入 `PATH` 后再次执行可继续到完成。

> **待本地验证**：依赖包名在不同 Linux 发行版上可能不同；Verilator 版本是否满足要求、`make sw` 中途失败的确切位置，需在你本地验证。

#### 4.4.5 小练习与答案

**练习 1**：为什么作者要求「从源码安装 Verilator」而不是直接用发行版的包？

> **参考答案**：因为后续仿真/形式化对 Verilator 版本有要求（顶层注释提到需 v5.007 或更高）。发行版仓库里的版本可能过旧，所以从官方源码编译安装以保证版本满足。

**练习 2**：`make sw` 第一次会失败，第二次（加入 PATH 后）能成功，根因是什么？

> **参考答案**：GCC 的某些构建阶段需要用到前一步刚刚安装到 `sw/install/cross-tools/bin` 的 `zip-*` 工具（如 `zip-as`）。第一次 `make` 时该目录还不在 `PATH` 里，故中途失败；把它加入 `PATH` 后重新 `make`，make 会跳过已完成的步骤、从断点继续，从而完成剩余构建。

## 5. 综合实践

**任务**：画出 ZipCPU 的「目录 → 构建目标 → 产物」三栏对照图，并用最小命令验证其中一条链路。

请完成以下步骤：

1. **画图**：在一张表里填入五行（`rtl/`、`sw/`、`sim/`、`bench/`、`doc/`），每行写出：对应的顶层 Makefile 目标、进入的子目录、最终产物路径与形态。参考答案见下。

2. **追踪一条链路**：从顶层 [Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile) 出发，追踪 `make vsim` 的完整路径。它依次经过：顶层 `vsim`（[L100–L103](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/Makefile#L100-L103)）→ 依赖 `rtl` → 进入 `rtl/` 跑 `all`（[rtl/Makefile L40–L42](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L40-L42)）产出 `rtl/obj_dir/` → 回到 `vsim` 进入 `sim/verilator` 构建测试台。请把这条链路上每一步的产物写出来。

3. **动手验证（可选）**：若本机依赖齐全，运行 `make rtl`，确认 `rtl/obj_dir/` 下确实出现 `V*__ALL.a` 文件；若依赖不全，则改为「源码阅读型验证」——在 `rtl/Makefile` 中找到 `VOBJ := obj_dir`（[L127](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L127)）与某条 `$(VOBJ)/V...__ALL.a` 规则（如 [L196–L197](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile#L196-L197)），说明产物文件名是如何由 `--prefix` 决定的。

**参考答案（三栏对照图）**：

| 目录 | 顶层目标（依赖链） | 最终产物（路径 / 形态） |
|------|------|------|
| `rtl/` | `rtl`（→ `rtl/` 的 `all`） | `rtl/obj_dir/V*__ALL.a` 等 Verilator C++ 模型 |
| `sw/` | `sw`（→ `sw/` 的 `install`） | `sw/install/cross-tools/bin/zip-gcc` 等工具链 |
| `sim/` | `sim` = `cppsim` + `vsim`（`vsim` 依赖 `rtl`） | `sim/cpp` 的 ISS 与 `sim/verilator/zipsys_tb` 等可执行测试台 |
| `bench/` | `bench`（依赖 `rtl sw`）→ `bench/asm` | 汇编测试 ELF（`hellosim` 等）；`bench/formal` 另产出证明结论 |
| `doc/` | `doc`（→ `doc/`） | `doc/spec.pdf` 等规范文档 |

## 6. 本讲小结

- ZipCPU 是 monorepo：`rtl/` 造硬件、`sw/` 造工具链、`sim/` 做模拟、`bench/` 做验证、`doc/` 写规范。
- 顶层 `Makefile` 是调度器，用 `SUBMAKE := $(MAKE) --no-print-directory -C` 把工作转发到各子目录；默认 `all: rtl sw sim`。
- 各顶层目标：`rtl`（Verilator 编译 RTL）、`sw`（binutils/GCC/newlib 工具链）、`sim`（`cppsim` + `vsim`，其中 `vsim` 依赖 `rtl`）、`bench`（依赖 `rtl sw`）、`test`（依赖 `bench sim formal`，全链路）、`formal`（形式化证明）、`doc`、`clean`。
- `make rtl` 的产物在 `rtl/obj_dir/`，是 Verilator 生成的 C++ 模型（`V*__ALL.a`），同一份 RTL 会因不同 `OPT_*` 配置生成多个变体；可运行的模拟器由 `sim/verilator` 在此基础上链接得到。
- 三个 bench 子目录产物形态不同：`bench/asm` 产出可执行 ELF 测试程序，`bench/formal` 产出形式化证明结论（非程序），`bench/cpp` 是已迁移遗留物。
- 构建前需按 `INSTALL.md` 安装依赖（含从源码装 Verilator）；`make sw` 第一次会中途失败，需把 `sw/install/cross-tools/bin` 加入 `PATH` 后再次 `make` 继续；构建不污染系统全局。

## 7. 下一步学习建议

本讲之后，你已经知道「东西在哪、怎么搭」。建议接下来：

- **紧接 u1-l3《RTL 顶层与四种封装》**：进入 `rtl/`，看 `zipsystem.v`、`zipbones.v`、`zipaxil.v`、`zipaxi.v` 四个顶层封装的端口，理解它们如何包裹同一个 `zipcore`。这是从「目录」走进「硬件」的第一步。
- **若想先跑通一个程序**：跳到 u1-l4《跑起来：模拟器与第一个程序》，用 `sim/zipsw/hello.c` 在模拟器上跑出第一行 `Hello`，获得正反馈后再回头读 u1-l3。
- **后续深入工具链细节**：u5-l5《软件工具链与 ABI》会展开 `sw/Makefile` 里 binutils/GCC/newlib 的补丁机制与调用约定，本讲对其的介绍可作为预习。
- **建议同步阅读的源码**：本讲引用过的 [rtl/Makefile](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/Makefile) 里的 `ASMCFG`/`PIPECFG`/`CACHECFG` 等预设配置，正是后续 u5-l6《构建参数与集成选项》要详讲的 `OPT_*` 参数，提前浏览能建立直觉。
