# 构建与运行环境搭建

## 1. 本讲目标

本讲解决一个非常实际的问题：**拿到 Nyuzi 源码后，怎样把它从「一堆源文件」变成「能跑的程序」**。读完本讲，你应该能够：

- 说出搭建 Nyuzi 开发环境需要安装哪些依赖、为什么要装它们；
- 解释 `scripts/setup_tools.sh` 这一个脚本替你做了哪两件大事（装 Verilator、装编译器工具链）；
- 看懂根目录的 `CMakeLists.txt` 如何把 `tools / software / hardware / tests` 四个子项目串成一条构建流水线；
- 独立执行 `cmake . && make` 完成构建，并指出最终生成的两个可执行文件 `nyuzi_vsim` 与 `nyuzi_emulator` 分别在哪里、有什么区别。

本讲承接上一讲（u1-l1）建立的全局认知：Nyuzi 由硬件、模拟器、工具链、软件栈、测试五部分组成。这一讲就是把这五部分「装起来、连起来、跑起来」。

## 2. 前置知识

本讲是纯「上手」内容，不需要你懂 Verilog 或编译原理，但有几个概念最好先有个印象：

- **构建系统（build system）**：大型项目的源文件成百上千，手敲 `gcc` 不现实。构建系统（如 CMake + Make）让你用一份配置描述「谁依赖谁、怎么编译」，然后一键生成所有产物。CMake 本身不编译，它根据你写的 `CMakeLists.txt` 生成 Makefile，再由 `make` 真正去编译。
- **工具链（toolchain）**：指编译器、汇编器、链接器这一整套工具。Nyuzi 有自己专属的指令集，普通的 `gcc` 不认识它，所以必须用专门为 Nyuzi 定制的、基于 LLVM 的编译器（`clang`、`elf2hex`、`lldb` 等）。
- **子模块（git submodule）**：Git 仓库里「套着」的另一个仓库。Nyuzi 把 Verilator 和工具链作为子模块引入，需要先 `git submodule update` 把它们的代码拉下来才能编译。
- **仿真器 vs. 模拟器（本讲中两个不同概念）**：
  - **周期精确仿真器（`nyuzi_vsim`）**：用 Verilator 把真实的硬件描述语言（SystemVerilog）编译成可执行程序，逐个时钟周期地模拟，连流水线和缓存都和真实硬件一致——它几乎「就是」硬件。
  - **指令集模拟器（`nyuzi_emulator`）**：用 C 写的解释器，只关心「这条指令做了什么」，不关心硬件内部周期、流水线、缓存。它跑得快、方便调试软件，但不能用来验证硬件细节。
  - 这两者的区别是本讲实践的落点，后面会反复出现。

## 3. 本讲源码地图

本讲涉及的文件都偏「项目骨架」，不涉及具体硬件逻辑或软件算法：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目主页说明，包含依赖安装与构建的全部命令，是「上手第一手资料」 |
| `CMakeLists.txt` | 根目录构建入口，把四个子项目挂到构建树上 |
| `scripts/setup_tools.sh` | 一键脚本：拉子模块、编译安装 Verilator 与 Nyuzi 工具链 |
| `hardware/README.md` | 硬件目录说明，解释 `nyuzi_vsim` 的命令行参数与仿真机制 |
| `tools/emulator/CMakeLists.txt` | 模拟器的构建定义，产出 `nyuzi_emulator` |
| `cmake/cline_tool.cmake` | 「宿主端命令行工具」的通用宏，决定模拟器二进制输出到哪里 |
| `cmake/nyuzi.cmake` | 「Nyuzi 目标程序」的通用宏，构建时自动生成 `run_emulator` / `run_verilator` 等运行脚本 |
| `hardware/CMakeLists.txt` | 硬件目录构建定义，调用 Verilator 产出 `nyuzi_vsim` |

> 小提示：本讲提到的 `run_emulator`、`run_verilator` 等脚本在仓库里**搜不到**，因为它们不是提交进仓库的源码，而是构建时由 `cmake/nyuzi.cmake` 自动生成的（见 4.3 节）。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**依赖安装 → 工具链脚本 → CMake 构建**。三者正好对应「装环境 → 装编译器 → 编译项目」三步。

### 4.1 依赖安装

#### 4.1.1 概念说明

一个处理器项目横跨好几种技术：Verilog 硬件描述、C/C++ 软件、Python 测试、LLVM 编译器。每一种都需要对应的宿主工具。所以在编译 Nyuzi 之前，要先把这些「造工具的工具」装齐。这一步没有任何 Nyuzi 特色，纯粹是给宿主操作系统装软件包，但漏装任何一个都会在后面的某一步报错。

#### 4.1.2 核心流程

以 Ubuntu Linux 为例，依赖安装分为两类：

1. **系统包（apt-get）**：编译器、构建工具、Verilog 自动化（Emacs verilog-mode）、波形查看器、SDL2 图形库等。
2. **Python 包（pip3）**：`pillow`，用于测试和渲染相关的图像处理。

整体流程：

```text
apt-get 安装系统依赖 ──► pip3 安装 pillow ──► 依赖就绪，可进入 4.2
```

#### 4.1.3 源码精读

README 的「Install Prerequisites」一节给出了 Ubuntu 下的完整命令：

[README.md:27-30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L27-L30) —— 安装全部系统依赖。这里同时装了 `cmake make ninja gcc g++`（构建工具）、`bison flex`（语法生成器，编译器要用）、`emacs`（运行 verilog-mode AUTO 宏自动生成连线和复位）、`libsdl2-dev`（模拟器窗口图形）、`gtkwave`（看波形）等。注意它同时装了 `python`（即 Python2）和 `python3`，因为部分老脚本仍依赖 Python2。

README 还特别提示了一个**已知坑**：新版 cmake 会破坏 LLVM 工具链的构建，需要回退到旧版 cmake，对应 issue #204：

[README.md:32-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L32-L34) —— 提醒：若构建工具链时报错，多半是 cmake 版本太新，需回退。

macOS 用户走 Homebrew，命令不同但等价（README 第 56 行 `brew install cmake bison swig sdl2 emacs ninja`）。Windows 官方未测试，建议用 Linux 虚拟机。

#### 4.1.4 代码实践

1. **实践目标**：确认本机依赖是否齐全，理解每个包大致用途。
2. **操作步骤**：
   - 在 Ubuntu 上执行 README:27-30 的那条 `apt-get` 命令与 `pip3 install pillow`。
   - 逐项核对：`cmake --version`、`make --version`、`gcc --version`、`emacs --version`、`verilator --version`（此刻多半还没装，4.2 会装）。
3. **观察现象**：每个命令都应打印出版本号。
4. **预期结果**：除 `verilator` 外都应已存在；`verilator` 留给 4.2 的 `setup_tools.sh` 安装。若 `apt-get` 报「找不到包」，说明发行版太旧或包名不同，需对照修改。
5. **待本地验证**：具体版本号取决于你的系统，本讲无法替你确定。

#### 4.1.5 小练习与答案

**练习 1**：为什么构建一个「处理器项目」居然要装 Emacs？
**答案**：Nyuzi 的 SystemVerilog 源码里用了 [verilog-mode](http://www.veripool.org/wiki/verilog-mode) 的 AUTO 宏来自动生成模块端口的连线声明和复位逻辑（见 `hardware/README.md` 第 17-19 行），构建时会用 Emacs 以批处理方式展开这些宏。Emacs 在这里不是编辑器，而是「代码生成器」。

**练习 2**：如果构建 LLVM 工具链时突然报一堆奇怪错误，README 建议先怀疑什么？
**答案**：先怀疑 cmake 版本太新。README:32-34 与 issue #204 指出近期 cmake 会破坏工具链构建，可回退旧版 cmake 绕过。

### 4.2 工具链脚本 setup_tools.sh

#### 4.2.1 概念说明

系统依赖装好后，还差两样「重家伙」，它们体量大、不在系统包管理器里，所以 Nyuzi 提供了一个专门脚本 `scripts/setup_tools.sh` 来下载并编译它们：

- **Verilator**：把 SystemVerilog 翻译成 C++/可执行程序的「Verilog 仿真器」。没有它，就无法把硬件 RTL 跑起来（也就没有 `nyuzi_vsim`）。
- **NyuziToolchain**：基于 LLVM 的 Nyuzi 专属编译器（`clang`、`elf2hex`、`llvm-objdump`、`lldb` 等）。没有它，就无法把 C/C++ 代码编译成 Nyuzi 能执行的机器码。

这两者都是作为 **git 子模块** 引入的，所以脚本开头要先拉子模块。脚本会多次请求 `sudo`（root 密码），因为最终要把它们 `make install` 到系统目录（默认 `/usr/local/`）。

#### 4.2.2 核心流程

```text
git submodule init / update        # 拉取 verilator 与 NyuziToolchain 源码
        │
        ├─► 构建 Verilator：
        │       cd tools/verilator
        │       autoconf → ./configure → make → sudo make install
        │
        └─► 构建 NyuziToolchain：
                mkdir tools/NyuziToolchain/build
                cd build && cmake -DCMAKE_BUILD_TYPE=Release ..
                make → sudo make install   （安装到 /usr/local/llvm-nyuzi）
```

任何一步失败，脚本会通过 `fail` 函数打印信息并 `exit 1` 中止，避免带着错误继续往下走。

#### 4.2.3 源码精读

脚本开头的注释点明了它的定位——「仓库首次克隆后运行，用来下载并构建 verilator 和工具链」：

[scripts/setup_tools.sh:18-22](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/scripts/setup_tools.sh#L18-L22) —— 说明本脚本的用途。

紧接着拉取两个子模块：

[scripts/setup_tools.sh:28-29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/scripts/setup_tools.sh#L28-L29) —— `git submodule init/update` 把 Verilator 和工具链的源码取到 `tools/` 下。`|| fail "..."` 保证失败即停。

然后用一个子 shell `( cd ... )` 构建 Verilator：

[scripts/setup_tools.sh:34-49](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/scripts/setup_tools.sh#L34-L49) —— 进入 `tools/verilator`，必要时 `autoconf` 生成 configure 脚本，再 `./configure`、`make`、`sudo make install`。注意第 39 行的判断 `if [ ! -f Makefile ]`：只有首次（还没 configure 过）才重新 configure，之后重跑脚本不会重复配置。

随后构建工具链：

[scripts/setup_tools.sh:54-67](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/scripts/setup_tools.sh#L54-L67) —— 在 `tools/NyuziToolchain/build` 里用 `cmake -DCMAKE_BUILD_TYPE=Release ..` 配置，再 `make`、`sudo make install`。最终安装到 `/usr/local/llvm-nyuzi/`（这正是根 `CMakeLists.txt` 里 `NYUZI_COMPILER_ROOT` 的默认值，见 4.3 节）。

> 旁支知识：README 第 94-99 行说明了「偶尔工具链需要更新版本时如何重建」——`git submodule update` 后进 `tools/NyuziToolchain/build` 重新 `make && sudo make install`，即手动重跑脚本的后半段。

#### 4.2.4 代码实践

1. **实践目标**：跑通 `setup_tools.sh`，确认 Verilator 与工具链都装好。
2. **操作步骤**：
   - 在仓库根目录执行 `./scripts/setup_tools.sh`。
   - 完成后分别验证：`verilator --version`（应 ≥ 4.12，见 4.3）、`ls /usr/local/llvm-nyuzi/bin/`（应能看到 `clang`、`elf2hex`、`llvm-objdump`、`lldb` 等）。
3. **观察现象**：脚本会多次提示输入 sudo 密码；末尾无 `fail` 报错即成功。
4. **预期结果**：`/usr/local/llvm-nyuzi/bin/clang --version` 能打印出包含 `nyuzi` 目标的版本信息。
5. **待本地验证**：首次编译工具链耗时较长（通常数十分钟），且依赖网络拉取子模块，本讲无法替你跑完，结果需本地确认。

#### 4.2.5 小练习与答案

**练习 1**：脚本里 `cd tools/verilator` 用 `( ... )` 包起来，这是什么技巧？为什么后面 `cd tools/NyuziToolchain/build` 时不用再 `cd` 回根目录？
**答案**：`( ... )` 是子 shell，其内部的 `cd` 只影响子 shell，退出括号后当前目录自动恢复。所以两段构建互不干扰，不必手动切回根目录。

**练习 2**：Verilator 的系统包（如 `apt install verilator`）明明能装，Nyuzi 为什么坚持自己编译？
**答案**：README 第 73-74 行指出，发行源自带的 Verilator 版本普遍太旧，而 Nyuzi 要求至少 4.12（见 `hardware/CMakeLists.txt:55-65` 的版本检查），所以必须自行编译新版。

### 4.3 CMake 构建

#### 4.3.1 概念说明

依赖和工具链都就绪后，剩下的「项目本体」（模拟器、软件、硬件模型、测试）统一由 CMake 构建。Nyuzi 的构建有清晰的层次：

- **根 `CMakeLists.txt`** 是总入口，它通过 `add_subdirectory` 把四个子目录挂到构建树上：
  - `tools/` —— 宿主端工具，最关键的就是模拟器 `nyuzi_emulator`；
  - `software/` —— 用 Nyuzi 工具链编译、跑在 Nyuzi 上的程序（libc、apps、kernel 等）；
  - `hardware/` —— 用 Verilator 把 RTL 编译成仿真器 `nyuzi_vsim`；
  - `tests/` —— 定义 `make tests` 等测试目标。
- 根目录还设定了一个关键变量 `NYUZI_COMPILER_ROOT`，指向 4.2 装好的工具链位置，供编译 Nyuzi 目标程序时使用。

构建结果集中在 `${CMAKE_BINARY_DIR}/bin/`（即构建目录下的 `bin/`），其中最重要的两个可执行文件就是本讲的主角：`nyuzi_emulator` 与 `nyuzi_vsim`。

#### 4.3.2 核心流程

```text
cmake .                      # 读取 CMakeLists.txt，生成 Makefile（仅首次或改动后需要）
   │
   └─► make                  # 真正编译：模拟器(C) + 软件(Nyuzi) + 硬件(Verilator)
            │
            └─► 产物在 bin/：
                    bin/nyuzi_emulator    ← tools/emulator/*.c 编译而来（C 模拟器）
                    bin/nyuzi_vsim        ← Verilator 编译 SystemVerilog RTL 而来（周期精确）
                    各程序的 *.hex / run_emulator / run_verilator 等运行脚本
   │
   └─► make tests            # 跑测试套件（可选）
```

`cmake .` 用「原地构建」（in-tree），即直接在仓库根目录生成构建文件，所以 `${CMAKE_BINARY_DIR}` 就是仓库根，产物落在 `bin/`。

#### 4.3.3 源码精读

先看根 `CMakeLists.txt`：

[CMakeLists.txt:17-19](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/CMakeLists.txt#L17-L19) —— 要求 cmake ≥ 3.4，并把 `cmake/` 目录加入模块搜索路径（这样后面才能 `include(cline_tool)`、`include(nyuzi)`）。

[CMakeLists.txt:21-22](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/CMakeLists.txt#L21-L22) —— 定义工具链路径：`NYUZI_COMPILER_ROOT` 默认 `/usr/local/llvm-nyuzi/`（正是 4.2 的安装位置），`NYUZI_COMPILER_BIN` 是其 `bin` 子目录。所有 Nyuzi 目标程序的编译器都从这里取（见 `cmake/nyuzi.cmake:20-24`）。

[CMakeLists.txt:24-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/CMakeLists.txt#L24-L27) —— 四个 `add_subdirectory` 把 tools/software/hardware/tests 挂上构建树。这就是「根 CMakeLists 如何组织四个子项目」的答案。

再看 `nyuzi_emulator` 怎么来。模拟器的构建定义在 `tools/emulator/CMakeLists.txt`：

[tools/emulator/CMakeLists.txt:17-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/CMakeLists.txt#L17-L28) —— 用 `add_command_line_tool(nyuzi_emulator ...)` 把 8 个 C 源文件（`main.c`、`processor.c`、`device.c`、`cosimulation.c`、`remote-gdb.c` 等）编成可执行程序，并链接 SDL2 做图形窗口。

这个 `add_command_line_tool` 宏来自 `cmake/cline_tool.cmake`，它决定了二进制输出位置：

[cmake/cline_tool.cmake:19-23](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/cline_tool.cmake#L19-L23) —— 关键一行：`set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)`，把所有「宿主端命令行工具」的可执行文件统一放到 `${CMAKE_BINARY_DIR}/bin/`。所以 `nyuzi_emulator` 落在 `bin/nyuzi_emulator`。

接着看 `nyuzi_vsim` 怎么来。硬件构建定义在 `hardware/CMakeLists.txt`，先看 Verilator 的选项：

[hardware/CMakeLists.txt:35-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/CMakeLists.txt#L35-L47) —— `VERILATOR_OPTIONS` 里关键的是 `-DSIMULATION=1`（启用仿真专用代码路径，见 `hardware/README.md:21-24`）、`--assert`（开断言）、`-Wall`，以及搜索路径 `-I core`、`-y testbench`、`-y fpga/common`。

[hardware/CMakeLists.txt:55-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/CMakeLists.txt#L55-L65) —— 运行时检查已安装的 Verilator 版本，要求至少 4.12，否则 `FATAL_ERROR`。这解释了 4.2 为什么坚持自编译新版 Verilator。

[hardware/CMakeLists.txt:67-75](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/CMakeLists.txt#L67-L75) —— 自定义目标 `nyuzi_vsim`（且为 `ALL`，即默认构建）：调用 Verilator 编译 `testbench/soc_tb.sv`（仿真顶层）+ `verilator_main.cpp` + `jtag_socket.cpp`，生成 `Vsoc_tb`，最后 `cp` 到 `${CMAKE_BINARY_DIR}/bin/nyuzi_vsim`。所以 `nyuzi_vsim` 同样落在 `bin/` 下。

最后揭示一个容易让人困惑的点：`run_emulator` / `run_verilator` 这些「运行脚本」是构建时自动生成的，不在仓库里。逻辑在 `cmake/nyuzi.cmake` 的 `add_nyuzi_executable` 宏中：

[cmake/nyuzi.cmake:91-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L91-L92) —— 用 `file(GENERATE ...)` 在每个程序的构建目录里生成 `run_emulator`，内容是「调用 `nyuzi_emulator` 并带上该程序的 `.hex` 与显示参数」。

[cmake/nyuzi.cmake:103-104](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L103-L104) —— 同理生成 `run_verilator`，内容是「调用 `bin/nyuzi_vsim` 并 `+bin=xxx.hex`」。

[cmake/nyuzi.cmake:119-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L119-L121) —— 因为 `file(GENERATE)` 没法设可执行权限，所以在 POST_BUILD 里 `chmod +x run_*` 把这些脚本加上执行位。这也解释了为什么你能在程序构建目录直接 `./run_emulator`。

#### 4.3.4 代码实践（本讲核心实践）

1. **实践目标**：完成构建，定位并区分 `nyuzi_vsim` 与 `nyuzi_emulator`。
2. **操作步骤**：
   - 在仓库根执行 `cmake .`（生成 Makefile）。
   - 执行 `make`（编译全部子项目，首次耗时较长）。
   - 定位两个二进制：`ls -l bin/nyuzi_emulator bin/nyuzi_vsim`。
3. **观察现象 / 预期结果**：
   - 两个文件都应存在于 `bin/`（因为根 `CMakeLists.txt` 用了原地构建，`${CMAKE_BINARY_DIR}` 即仓库根）。
   - `bin/nyuzi_emulator`：C 源码编译产物，体积小、启动快。它是**指令集模拟器**，不模拟流水线/缓存（见 `tools/emulator/README.md:1-3`）。
   - `bin/nyuzi_vsim`：Verilator 把整套 SystemVerilog RTL 编译成的可执行程序，**周期精确**，模拟真实流水线、缓存与硬件时序（见 `hardware/README.md:40-43`）。
4. **两者的核心区别（务必讲清）**：

   | 维度 | `nyuzi_emulator` | `nyuzi_vsim` |
   |------|------------------|--------------|
   | 来源 | `tools/emulator/*.c`（C 解释器） | Verilator 编译 SystemVerilog RTL |
   | 精度 | 指令级，**非**周期精确 | **周期精确**，含流水线/缓存 |
   | 速度 | 快 | 慢（要逐周期模拟） |
   | 典型用途 | 软件开发、调试、性能建模、协同仿真参考 | 验证硬件设计本身、跑硬件测试 |
   | 生成目标 | `add_command_line_tool` 宏 | 自定义目标 `nyuzi_vsim`，`cp` 到 `bin/` |

5. **待本地验证**：构建是否一次通过取决于依赖与工具链是否完整装好；若 `make` 报「找不到 verilator」或「找不到 clang」，请回到 4.1/4.2 检查。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `nyuzi_emulator` 和 `nyuzi_vsim` 最后都出现在 `bin/` 目录，而不是各自的源码目录？
**答案**：模拟器走 `cmake/cline_tool.cmake` 的宏，显式设 `CMAKE_RUNTIME_OUTPUT_DIRECTORY=${CMAKE_BINARY_DIR}/bin`；仿真器走 `hardware/CMakeLists.txt:73` 的 `cp ... ${CMAKE_BINARY_DIR}/bin/nyuzi_vsim`。两者都被人为统一放到 `${CMAKE_BINARY_DIR}/bin/`，方便所有运行脚本和测试用同一个固定路径找到它们。

**练习 2**：你在仓库里 `find -name run_emulator` 一无所获，但构建后却能在程序目录里 `./run_emulator`，这是怎么回事？
**答案**：`run_emulator` 不是源码，而是构建时由 `cmake/nyuzi.cmake:91-92` 的 `file(GENERATE ...)` 自动生成的脚本，内容是把 `nyuzi_emulator` 指向该程序的 `.hex`；生成后还由 `cmake/nyuzi.cmake:119-121` 的 `chmod +x` 加了执行位。

**练习 3**：根 `CMakeLists.txt` 里 `NYUZI_COMPILER_ROOT` 默认是 `/usr/local/llvm-nyuzi/`，如果我把工具链装到了别的地方，构建会受影响吗？怎么改？
**答案**：会。`software/` 下的程序用 Nyuzi 工具链编译，编译器路径就取自这个变量（见 `cmake/nyuzi.cmake:20-24`）。可改成自定义路径，或在 `cmake .` 时用 `-DNYUZI_COMPILER_ROOT=/你的/路径` 覆盖。

## 5. 综合实践

把三个模块串起来，完成一次「从零到能跑」的完整搭建，并验证产物正确：

1. **装依赖**（对应 4.1）：按 `README.md:27-30` 安装系统包与 `pillow`。
2. **装工具链**（对应 4.2）：执行 `./scripts/setup_tools.sh`，验证 `verilator --version` 与 `/usr/local/llvm-nyuzi/bin/clang` 都已就位。
3. **构建项目**（对应 4.3）：`cmake . && make`。
4. **产物核对**：确认 `bin/nyuzi_emulator`、`bin/nyuzi_vsim` 均已生成；用 `file bin/nyuzi_vsim` 确认它是宿主机可执行文件。
5. **跑测试**：执行 `make tests`（对应根 `CMakeLists.txt` 之外、定义在 `tests/CMakeLists.txt:61-79` 的 `tests` 目标），观察哪些测试通过、哪些被限制在特定 target（例如 `whole-program` 只在 verilator 跑，`kernel` 只在 emulator 跑，见 `tests/CMakeLists.txt:68-79`）。
6. **写一句话总结**：用自己的话回答「`nyuzi_emulator` 和 `nyuzi_vsim` 各是什么、分别用来干什么」。

> 若受限于环境（无 sudo、无网络、CI 容器）无法真正执行，请把 1-5 步作为「源码阅读型实践」：对照本讲给出的永久链接，逐文件核对你对每一步的理解，并把第 6 步的总结写出来。

## 6. 本讲小结

- Nyuzi 环境搭建分三步：**装系统依赖 → 跑 `setup_tools.sh` 装工具链 → `cmake . && make` 构建项目**。
- `README.md` 是依赖与构建命令的第一手资料；其中有一条重要提示：新版 cmake 会破坏 LLVM 工具链构建（issue #204）。
- `scripts/setup_tools.sh` 一个脚本干两件事：拉子模块后分别编译安装 **Verilator** 和 **NyuziToolchain**，最终装到 `/usr/local/`。
- 根 `CMakeLists.txt` 通过四个 `add_subdirectory(tools/software/hardware/tests)` 把项目组织成统一构建树，并用 `NYUZI_COMPILER_ROOT` 指向工具链。
- 两个核心产物都在 `${CMAKE_BINARY_DIR}/bin/`：`nyuzi_emulator`（C 指令集模拟器，非周期精确）与 `nyuzi_vsim`（Verilator 编译 RTL 的周期精确仿真器）。
- `run_emulator` / `run_verilator` 等运行脚本不是源码，而是构建时由 `cmake/nyuzi.cmake` 的 `file(GENERATE)` 自动生成并 `chmod +x` 的。

## 7. 下一步学习建议

- 环境搭好、产物就绪后，下一步自然是**跑一个真实程序**。建议接着学习 **u1-l4 运行第一个程序**，它会演示如何用 `run_emulator` 跑通 `hello_world`，并解释 ELF→hex 镜像、从地址 0 启动、写控制寄存器停机的机制。
- 若想先建立「目录到功能」的映射，可先读 **u1-l3 目录结构与源码地图**。
- 想深入理解两个仿真器差异的读者，后续可对照 **u8-l1 模拟器架构与指令执行**（讲 `nyuzi_emulator` 内部）与硬件相关讲义（讲 `nyuzi_vsim` 背后的 RTL）。
- 建议随手翻阅的源码：`README.md` 的「Build」「Next Steps」两节、`hardware/README.md` 的命令行参数表（`nyuzi_vsim` 的 `+bin`、`+trace`、`+profile` 等会在后续讲义反复出现）。
