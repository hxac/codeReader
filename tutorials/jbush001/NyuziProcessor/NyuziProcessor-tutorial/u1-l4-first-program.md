# 运行第一个程序

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚一个 Nyuzi 程序从「C 源码」到「在模拟器里跑起来」的完整链路。
- 解释 `hello_world` 这个程序是如何被编译成 ELF、再被 `elf2hex` 转成 hex 内存镜像、最后被模拟器加载到地址 0 并执行的。
- 理解 `run_emulator` / `run_verilator` 这些脚本是从哪里来的、背后调用了什么命令。
- 说明程序通过哪条路径把文本「打印」到宿主屏幕，以及它在结束时如何通过写一个控制寄存器让整个模拟器停机。

本讲是 u1 单元「项目概览与上手」的最后一篇。它把前面 u1-l2（构建与运行）和 u1-l3（目录地图）学到的知识串成一条可运行的主线：**构建 → 生成镜像 → 加载 → 执行 → 输出 → 停机**。

## 2. 前置知识

在进入源码之前，先用通俗语言建立三个直觉。这些概念在 u1-l1/u1-l2 已经部分提过，这里从「跑程序」的视角再讲一次。

- **指令集模拟器（emulator）**：Nyuzi 用 C 写了一个 `nyuzi_emulator`，它不模拟流水线和缓存（非周期精确），但能一条一条地解释执行 Nyuzi 指令，并且仿真出 FPGA 上的外设（串口、SD 卡、帧缓冲等）。它的最大用处是**软件开发**：编译快、能直接看到输出、能接调试器。它和真实的 SystemVerilog 硬件实现的是同一套指令集（ISA），所以同一份程序既能在模拟器跑，也能在硬件/Verilator 仿真里跑。

- **hex 内存镜像**：模拟器和 Verilog 仿真器都不会直接读 ELF。它们读的是一种纯文本的「内存初值」文件——每个 32 位字写成一个十六进制数，可以用 `@地址` 跳到指定位置。这正是 Verilog 里 `$readmemh` 任务使用的格式。工具链里的 `elf2hex` 负责把 ELF 转成这种格式。

- **内存映射 I/O（MMIO）**：Nyuzi 没有专门的「输出指令」。程序要向宿主打印字符，就是往一段特殊的内存地址（`0xffff0000` 开始的外设区）写值。模拟器拦截到对这段地址的写操作，就把字符送到宿主的 `stdout`。这和真实 FPGA 上「往 UART 寄存器写字节就会从串口发出去」是一回事。

一个关键心智模型：**程序不是「返回就结束」**。`main` 返回后，启动代码会接管控制权，它要做两件收尾的事——向串口发送一个结束符、然后写一个控制寄存器把所有硬件线程挂起。挂起之后模拟器发现「没有可运行的线程了」，就自然退出。这就是 Nyuzi 程序的「停机」机制。

## 3. 本讲源码地图

本讲涉及的关键文件，按它们在「跑程序」这条主线里出现的顺序排列：

| 文件 | 作用 |
|------|------|
| `software/apps/hello_world/hello_world.c` | 最简单的示例程序，只有一行 `printf`。本讲的「主角」。 |
| `software/apps/hello_world/CMakeLists.txt` | 声明如何构建 hello_world，调用 `add_nyuzi_executable` 宏。 |
| `cmake/nyuzi.cmake` | 定义 `add_nyuzi_executable` 宏：编译 ELF、转 hex、生成各种 `run_*` 脚本。 |
| `software/libs/libos/bare-metal/crt0.S` | C 运行时启动代码：设置栈、调用 `main`、`main` 返回后停机。 |
| `tools/emulator/main.c` | 模拟器入口：解析命令行、加载 hex、驱动执行主循环。 |
| `tools/emulator/util.c` | `read_hex_file`：把 hex 文本解析进模拟器的内存数组。 |
| `tools/emulator/processor.c` | 模拟器核心：线程调度、指令执行、控制寄存器处理、停机判定。 |
| `tools/emulator/device.c` / `device.h` | 外设仿真：对 `0xffff0000` 区的访问在这里分发，串口输出落到 `stdout`。 |
| `software/libs/libc/src/stdio.c` | `printf` 的实现入口。 |
| `software/libs/libos/bare-metal/uart.c` | `_write_uart`：把单个字符写到 UART 寄存器。 |
| `hardware/core/defines.svh` | 控制寄存器编号定义（`CR_SUSPEND_THREAD` 等）。 |
| `hardware/core/control_registers.sv` | 硬件侧控制寄存器：把 `setcr` 的值变成 `cr_suspend_thread` 信号。 |

不用一次记住全部，后面每个模块都会精确引用其中的几行。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，恰好对应主线上的四个环节：**程序入口 → 镜像加载 → 运行脚本 → 输出与停机**。

### 4.1 程序入口：从 `_start` 到 `main`

#### 4.1.1 概念说明

在 PC 上写 C 程序时，你写的 `main` 并不是 CPU 真正执行的第一条指令。在 `main` 之前，有一段「C 运行时启动代码」（通常叫 `crt0`）负责把 CPU 拉到一个 C 程序能运行的最低状态：设好栈指针、初始化全局变量、调用全局对象的构造函数，然后才 `call main`。

Nyuzi 也是这样。程序被加载后从地址 0 开始执行的第一条指令就在 `crt0.S` 的 `_start` 标号处。理解这段代码很重要，因为：

1. 它说明了「为什么程序从地址 0 启动」——因为模拟器把镜像加载到地址 0，而链接器把 `_start` 放在了地址 0。
2. 它说明了「每个线程的栈在哪里」。
3. `main` 返回之后的事情（停机）也发生在这里，详见 4.4 节。

#### 4.1.2 核心流程

`crt0.S` 在文件头部的注释里画了一张内存布局图，我们把它转写成文字版：

```
高地址
  +---------------+  0x00200000  栈底（每线程 16KiB，向下生长）
  |    stacks     |
  +---------------+
  |   code/data   |  0x00000000  代码与数据（镜像加载处）
低地址
```

启动流程（线程 0 视角）：

1. 用 `getcr s0, 0` 读出「我是第几号线程」（控制寄存器 0 = 线程号）。
2. 根据线程号计算自己的栈地址：栈底在 `0x200000`，每线程 16KiB，地址向下偏移。
3. 加载全局指针 `gp`（GOT 基址）。
4. 只有线程 0 执行全局构造函数循环；其它线程（被线程 0 唤醒后才到这里）直接跳到 `main`。
5. `call main`，把 `main` 的返回值忽略（`main` 在 Nyuzi 裸机程序里返回值意义不大）。

栈地址的计算可以用一个简单公式表达：

\[
\text{sp} = 0x200000 - \text{threadId} \times 0x4000
\]

其中 \(0x4000 = 16384\) 即 16KiB。

#### 4.1.3 源码精读

启动代码的入口与栈设置 [`software/libs/libos/bare-metal/crt0.S:42-47`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L42-L47)：读线程号、左移 14 位（×16KiB）、从栈底减去得到本线程的栈顶。这段注释里写明「上电时只有硬件线程 0 在跑」。

只有线程 0 做初始化、然后跳转 [`software/libs/libos/bare-metal/crt0.S:56-69`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L56-L69)：`bnz s0, do_main` 让非 0 线程跳过构造函数循环；线程 0 走完 `init_loop` 调用完所有构造函数后也落到 `do_main`，设 `argc=0` 并 `call main`。

而 `main` 本身极其简单 [`software/apps/hello_world/hello_world.c:21-25`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/hello_world/hello_world.c#L21-L25)：只调一次 `printf("Hello World\n")` 就返回。文件头注释也明确写着「Simple program to demonstrate build system and simulation environment」——它的存在就是为了让你验证整条工具链和运行环境是通的。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：搞清楚 `main` 被调用时寄存器 `s0`（`argc`）的值，以及为什么裸机程序里 `argc` 恒为 0。
2. **步骤**：打开 `crt0.S`，找到 `do_main:` 标号，观察 `call main` 前一行 `move s0, 0`。
3. **观察**：对比 PC 上 `crt0` 会从操作系统拿到 `argv`，这里完全没有——因为裸机没有操作系统来传参。
4. **预期结果**：你应当能解释「Nyuzi 裸机程序的 `main` 不接收命令行参数」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `crt0.S` 里要用 `getcr s0, 0` 读线程号，而不是写死线程 0？
  - **答案**：因为同一段 `_start` 代码会被所有硬件线程执行。线程 0 上电时跑，其它线程被软件唤醒后也从 `_start` 跑。只有靠读控制寄存器 0（线程号）才能区分「我现在是谁」，从而算出各自的栈地址、并决定是否跳过全局初始化。

- **练习 2**：如果把每线程栈大小从 16KiB 改成 8KiB，需要改 `crt0.S` 里哪一行？
  - **答案**：改 `shl s0, s0, 14`（左移 14 位 = ×16KiB）为左移 13 位（×8KiB）。注意 `0x200000` 栈底也要相应评估是否还够容纳全部线程的栈（与 `THREADS_PER_CORE` 有关）。

### 4.2 镜像加载：ELF 到 hex 与内存映像

#### 4.2.1 概念说明

`clang` 编译链接后得到的是 **ELF** 文件——里面带段头、符号表、调试信息，结构很丰富。但模拟器和 Verilog 仿真器并不想解析这么复杂的格式，它们只想要一个东西：**「内存地址 → 初始内容」的对照表**。

`elf2hex`（工具链自带）就干这件事：它读 ELF，把所有「需要加载到内存」的段抽出来，按地址写成一个纯文本的 **hex 文件**。这个 hex 文件的格式是 Verilog `$readmemh` 任务的标准格式（IEEE 1364-2001），所以同一份镜像既能给 C 模拟器用，也能给 Verilog 仿真用。

关键约定：**内存从地址 0 开始，程序也从地址 0 开始执行**。所以 `_start` 必须被链接到地址 0，模拟器加载完后把 PC 设成 0 即可。

#### 4.2.2 核心流程

hex 文件的文法非常简单：

- 一串十六进制数字 → 一个 32 位字的值，按顺序填入从「当前地址」开始的内存。
- `@` 开头 → 后面跟着一个地址，把「当前地址」跳到那里。
- `//` 或 `/* ... */` → 注释，解析时跳过。

模拟器这边的加载流程：

1. `main.c` 解析完命令行后，调用 `load_hex_file(proc, argv[optind])`，其中 `argv[optind]` 就是命令行里那个 `.hex` 文件名。
2. `load_hex_file` 转调 `read_hex_file`，它是一个状态机，逐字符扫描 hex 文本。
3. 扫到的每个字经过 `endian_swap32` 字节序转换后写入 `proc->memory[address++]`。
4. 全部读完后，`proc->memory[]` 就是「开机瞬间的内存快照」。所有线程的 PC 都从 0 开始。

#### 4.2.3 源码精读

模拟器入口里加载镜像的那一行 [`tools/emulator/main.c:347-351`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L347-L351)：`load_hex_file(proc, argv[optind])` 读命令行末尾的 hex 文件，失败则报错退出。注意这之前 `init_processor` 已经把内存大小、核数、线程数都设好了。

`load_hex_file` 只是一层薄包装 [`tools/emulator/processor.c:293-295`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L293-L295)：把模拟器的内存数组 `proc->memory` 和大小传给真正的解析器。

真正的解析逻辑 [`tools/emulator/util.c:107-268`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.c#L107-L268)：开头注释点明「Format is defined in IEEE 1364-2001, section 17.2.8」。其中 `SCAN_ADDRESS` 状态处理 `@`（util.c 第 150-154 行把 `@` 触发地址扫描），`SCAN_NUMBER` 状态把一串十六进制数字拼成一个 32 位字。最关键的一行是写入内存 [`tools/emulator/util.c:238`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.c#L238)：`memory[address++] = endian_swap32(number_value);`——每个字都做了大小端转换，因为 Nyuzi 是小端，而 `$readmemh` 文本按大端可读顺序书写。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：亲手核对 hex 文件的「字」与模拟器内存里「字」的字节序关系。
2. **步骤**：构建 hello_world 后，用文本编辑器打开构建目录下的 `hello_world.hex`（它很小）。挑一个字，比如 `00000004`。再读 util.c 第 238 行的 `endian_swap32`。
3. **观察**：hex 文件里写的 `00000004` 进入模拟器内存后，对应的 4 个字节在内存里的排列顺序。
4. **预期结果**：你能解释为什么需要 `endian_swap32`——文本里 `00000004` 的高位字节 `00` 在最前面，但小端机内存里最低地址应放低位字节 `04`。
5. **待本地验证**：实际 hex 文件内容请以本地构建产物为准。

#### 4.2.5 小练习与答案

- **练习 1**：如果 hex 文件里没有任何 `@地址`，模拟器会把它加载到哪里？
  - **答案**：从地址 0 开始顺序填入。`read_hex_file` 里 `address` 初值为 0（util.c 第 114 行），没有 `@` 就一直 `address++` 往上写。

- **练习 2**：为什么模拟器和 Verilog 仿真器要共用同一种 hex 格式？
  - **答案**：因为两者实现的是同一套 ISA、同一种内存模型，用同一种镜像格式可以让一份程序在两种环境间无缝切换；而且 hex 格式恰好是 Verilog `$readmemh` 原生支持的，Verilog 仿真器读它最省事。

### 4.3 运行脚本：`run_emulator` / `run_verilator` 的生成

#### 4.3.1 概念说明

你也许注意到了：每个程序目录（比如 `software/apps/hello_world/`）构建后会在它的构建目录里冒出一堆可执行脚本——`run_emulator`、`run_verilator`、`run_vcs`、`run_fpga`、`run_debug`。这些脚本不是手写的，也不是项目自带的，而是 **CMake 在配置/构建阶段自动生成** 的。

它们存在的意义是：让你不必记住每种运行环境的命令行参数。想跑模拟器就 `./run_emulator`，想跑 Verilator 就 `./run_verilator`，想调试就 `./run_debug`。脚本里已经把「用哪个可执行文件、带什么参数、加载哪个 hex」都拼好了。

#### 4.3.2 核心流程

`add_nyuzi_executable` 宏（在 `cmake/nyuzi.cmake`）在定义一个 Nyuzi 可执行目标时，会做这几件事：

1. 设置交叉编译器为 Nyuzi 版的 `clang`/`clang++`。
2. `add_executable(...)` 正常编译出 ELF。
3. 两个 `add_custom_command`（POST_BUILD）：
   - 调 `elf2hex` 把 ELF 转成 `<name>.hex`（4.2 节讲过）。
   - 调 `llvm-objdump` 生成反汇编列表 `<name>.lst`（给 `-v` 跟踪对照用）。
4. 用 `file(GENERATE ...)` 生成 5 个 `run_*` 脚本，内容就是把「模拟器/仿真器可执行文件 + 参数 + hex 路径」拼成一行命令。
5. POST_BUILD 里 `chmod +x run_*` 给脚本加可执行权限。

hello_world 的 `CMakeLists.txt` 极其简短，正是因为这些繁琐逻辑都封装在宏里了。

#### 4.3.3 源码精读

hello_world 的构建声明 [`software/apps/hello_world/CMakeLists.txt:20-25`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/hello_world/CMakeLists.txt#L20-L25)：`add_nyuzi_executable(hello_world SOURCES hello_world.c)` 声明目标，`target_link_libraries(hello_world c os-bare)` 链接 libc（`c`）和裸机版 libos（`os-bare`，含 `crt0.S`、`_write_uart` 等）。

宏定义的入口 [`cmake/nyuzi.cmake:33-49`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L33-L49)：解析参数、设置编译器、`add_executable` 产出 ELF。

把 ELF 转成 hex 的 POST_BUILD 命令 [`cmake/nyuzi.cmake:52-55`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L52-L55)：调用 `${NYUZI_COMPILER_BIN}/elf2hex`，输出 `${name}.hex`。这正是 4.2 节模拟器要加载的那个文件。

生成 `run_emulator` 脚本 [`cmake/nyuzi.cmake:90-92`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L90-L92)：脚本内容就是 `nyuzi_emulator ${EMULATOR_ARGS} ${name}.hex`。对 hello_world 来说 `EMULATOR_ARGS` 为空，所以等价于 `bin/nyuzi_emulator hello_world.hex`。

生成 `run_verilator` 脚本 [`cmake/nyuzi.cmake:103-104`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L103-L104)：内容是 `bin/nyuzi_vsim +bin=${name}.hex`。注意 Verilator 用的是 `+bin=` plusarg 风格的参数（Verilog testbench 读 plusarg 再 `$readmemh`），而模拟器用的是位置参数。

给脚本加可执行权限 [`cmake/nyuzi.cmake:118-121`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L118-L121)：注释说明 `file(GENERATE)` 不支持直接设权限，所以用 POST_BUILD 的 `chmod +x run_*` 兜底。

模拟器命令行选项的权威说明在 [`tools/emulator/README.md:21-40`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/README.md#L21-L40)：比如 `-v` 打开指令跟踪、`-m gdb` 进入调试模式、`-b` 挂虚拟块设备等。README 还明确说明了三条关键约定：内存从地址 0 开始、用 `$readmemh` 格式加载、**所有线程都停机时模拟器退出**（见 4.4 节）。

#### 4.3.4 代码实践（源码阅读型）

1. **目标**：搞清楚 `run_emulator` 和 `run_verilator` 在「传 hex 的方式」上的区别。
2. **步骤**：阅读 nyuzi.cmake 第 90-92 行和第 103-104 行，对比两段 `file(GENERATE)` 的 `CONTENT`。
3. **观察**：模拟器把 hex 作为位置参数直接传；Verilator 把 hex 作为 `+bin=` plusarg 传。
4. **预期结果**：你能解释为什么二者不同——模拟器是 C 程序读 `argv`，而 Verilator 仿真器是 Verilog testbench 通过 `$value$plusargs` 读 plusarg。

#### 4.3.5 小练习与答案

- **练习 1**：如果你想给 hello_world 加一个 `-v` 让模拟器打印指令跟踪，应该改哪里？
  - **答案**：最简单的方式是直接运行 `bin/nyuzi_emulator -v hello_world.hex` 而不用脚本；如果想固化进脚本，可以在 `add_nyuzi_executable` 调用里没有现成开关，需要手动改 `run_emulator`（它是构建产物，会被重新生成覆盖），或仿照 `DISPLAY_WIDTH` 的模式在 nyuzi.cmake 里加一个新参数拼进 `EMULATOR_ARGS`。

- **练习 2**：`run_debug` 脚本会启动哪两件事？
  - **答案**：见 nyuzi.cmake 第 94-96 行——它一行启动 `nyuzi_emulator -m gdb`（监听 8000 端口），另一行启动 `lldb` 并 `gdb-remote 8000` 附加上去。两件事在同一个脚本里用 `&` 串起来。

### 4.4 文本输出与停机机制

#### 4.4.1 概念说明

这是本讲最关键的一节，它回答两个问题：**程序怎么把字打到屏幕上？程序怎么让模拟器停下来？**

**输出**：Nyuzi 程序调用标准 C 的 `printf`，这条调用最终会走到一个把字符写到「UART 寄存器」的函数。UART 寄存器位于内存映射外设区（`0xffff0000` 起）。在模拟器里，对这个地址的写会被外设仿真代码拦截，转而调用宿主的 `putc(stdout)`——于是字符就出现在你启动模拟器的终端里。在真实 FPGA 上，同样的写操作会让硬件 UART 把字节从串口发出去。**同一份代码，两种环境，语义一致**，这正是内存映射 I/O 的威力。

**停机**：`main` 返回并不意味着模拟器退出——`crt0` 会接着执行收尾代码，最后一件事是往控制寄存器 `CR_SUSPEND_THREAD`（编号 20）写 `-1`（全 1）。这个写操作会把「线程使能掩码」里所有位清零，即挂起所有线程。模拟器的主循环发现「没有任何线程可运行」了，就退出。这就是 Nyuzi 的停机机制。

#### 4.4.2 核心流程

**输出路径**（自顶向下）：

```
printf("Hello World\n")            // 用户代码
  → vfprintf(stdout, ...)          // 格式化
  → fputc(ch, stdout)              // 逐字符
  → write_console(&_ch, 1)         // libc → libos
  → _write_uart(ch)                // 查询 UART 状态、写 UART_TX 寄存器
  → 写 0xffff0048 (REG_SERIAL_OUTPUT)
  → 模拟器 write_device_register 拦截
  → putc(value & 0xff, stdout)     // 宿主屏幕
```

**停机路径**（`main` 返回后，在 `crt0` 里）：

```
main 返回
  → 只让一个线程跑 atexit 析构（用 load_sync/store_sync 自旋锁）
  → call_atexit_functions
  → 发送 ^D (ASCII 4) 给串口（FPGA 上用于通知串口监控程序结束）
  → setcr s0=-1, CR_SUSPEND_THREAD(=20)   // 清空 thread_enable_mask
  → 死循环 b 1b（永远不会真的执行到这里，因为本线程已被挂起）
```

模拟器侧的退出判定：

```
setcr 写 CR_SUSPEND_THREAD  →  thread_enable_mask &= ~value
                                   (value=-1 ⇒ mask=0)
execute_instructions 主循环  →  if (thread_enable_mask == 0) return false
main.c 的 while(execute_instructions(...))  →  循环结束，main 返回 0
```

位运算上：`value = -1` 即 32 位全 1，`~value` 为全 0，故 `mask &= 0` 必为 0。

#### 4.4.3 源码精读

`printf` 的实现入口 [`software/libs/libc/src/stdio.c:25-34`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L25-L34)：转调 `vfprintf(stdout, ...)`。`stdout` 是一个内部 `__stdout`（stdio.c 第 84-98 行），其 `write_buf` 为 `NULL`。

逐字符落到外设的关键 [`software/libs/libc/src/stdio.c:116-132`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L116-L132)：`fputc` 发现目标是 `stdout` 时，调 `write_console(&_ch, 1)`，这就把控制权从 libc 交到了 libos。

`write_console` 与 `_write_uart` [`software/libs/libos/bare-metal/uart.c:21-37`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/uart.c#L21-L37)：`_write_uart` 先轮询 `REG_UART_STATUS & UART_TX_READY` 等发送就绪，再把字符写入 `REG_UART_TX`。`write_console` 就是对每个字符调一次 `_write_uart`。

UART 寄存器地址 [`software/libs/libos/bare-metal/registers.h:21-33`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L21-L33)：`REGISTERS` 基址 `0xffff0000`，`REG_UART_TX = 0x0048/4`，即字节地址 `0xffff0048`。这与模拟器侧定义一致。

模拟器侧的设备寄存器地址 [`tools/emulator/device.h:22-28`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/device.h#L22-L28)：`DEVICE_BASE_ADDRESS = 0xffff0000`，`REG_SERIAL_OUTPUT = 0xffff0048`——和上面 `REG_UART_TX` 完全对应。

模拟器拦截串口写 [`tools/emulator/device.c:42-49`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/device.c#L42-L49)：`write_device_register` 收到对 `REG_SERIAL_OUTPUT` 的写，就 `putc(value & 0xff, stdout); fflush(stdout);`。这就是「程序往 `0xffff0048` 写一个字节，宿主终端就多出一个字符」的最终落点。

`main` 返回后的停机收尾 [`software/libs/libos/bare-metal/crt0.S:71-90`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L71-L90)：先用 `load_sync/store_sync` 自旋锁保证只有一个线程跑析构；调用 `call_atexit_functions`；发送 `^D`（`move s0, 4; call _write_uart`）；最后 `move s0, -1; setcr s0, CR_SUSPEND_THREAD` 挂起所有线程，再 `1: b 1b` 死循环兜底。`CR_SUSPEND_THREAD` 的编号定义在文件第 36 行 `#define CR_SUSPEND_THREAD 20`。

控制寄存器编号的「权威定义」 [`hardware/core/defines.svh:169-194`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L169-L194)：`CR_SUSPEND_THREAD = 5'd20`、`CR_RESUME_THREAD = 5'd21`。这组 `control_register_t` 枚举贯穿硬件、模拟器、软件，是三方共用的「控制寄存器编址表」。

模拟器侧处理 `setcr` [`tools/emulator/processor.c:1770-1777`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1770-L1777)：`CR_SUSPEND_THREAD` 分支执行 `thread_enable_mask &= ~value;`。`value = -1` 时 `~value = 0`，掩码被清零。对应地 `CR_RESUME_THREAD` 是「或」上某些位（带线程总数掩码保护）。

停机判定 [`tools/emulator/processor.c:390-393`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L390-L393)：`is_proc_halted` 当 `thread_enable_mask == 0 || crashed` 时返回真。

执行主循环因此退出 [`tools/emulator/processor.c:431-438`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L431-L438)：轮询调度里每轮先检查 `thread_enable_mask == 0`，是则打印 `thread enable mask is now zero` 并 `return false`。

模拟器 `main` 的驱动循环 [`tools/emulator/main.c:414-418`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L414-L418)：`while (execute_instructions(proc, 1000000)) poll_inputs(proc);`——`execute_instructions` 返回假时循环结束，`main` 最终 `return 0`（第 447 行）。

硬件侧（仅供对照，本讲不深入） [`hardware/core/control_registers.sv:208-209`](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/control_registers.sv#L208-L209)：写 `CR_SUSPEND_THREAD` 时把值的低 `TOTAL_THREADS` 位送到 `cr_suspend_thread` 输出，由顶层聚合成 `thread_en`。可以看到硬件和模拟器对同一个控制寄存器的语义是一致的。

#### 4.4.4 代码实践（动手型：用 `-v` 看停机瞬间）

1. **目标**：亲眼看到「程序输出文本 → 写 `CR_SUSPEND_THREAD` → 模拟器退出」的完整过程。
2. **步骤**：
   - 在 hello_world 构建目录里直接运行带跟踪的模拟器：
     ```
     bin/nyuzi_emulator -v hello_world.hex
     ```
   - `-v` 会把每条指令的寄存器写回、内存写都打到 stdout（见 emulator README「Tracing」一节）。
3. **需要观察的现象**：
   - 跟踪输出里应当能看到一连串 `writeMemWord 0xffff0048 ...`（向串口寄存器写字符）。
   - 末尾会出现一行 `thread enable mask is now zero`（processor.c 第 436 行打印的），紧接着模拟器退出。
4. **预期结果**：终端先打印 `Hello World`，最后打印 `thread enable mask is now zero`。这就是「输出」与「停机」两件事的可见证据。
5. **待本地验证**：`-v` 跟踪输出量很大且与工具链版本相关，请以本地实际输出为准；若觉得太刷屏，可改用 `bin/nyuzi_emulator hello_world.hex`（不带 `-v`）只看最终的两行输出。

#### 4.4.5 小练习与答案

- **练习 1**：如果 `main` 里写了个死循环（永远不返回），模拟器会发生什么？
  - **答案**：永远不会走到 `crt0` 的停机代码，`thread_enable_mask` 永远不为 0，`execute_instructions` 永远返回真，`main.c` 的 `while` 循环不会结束——模拟器会一直跑下去，直到你手动 Ctrl-C。

- **练习 2**：为什么 `crt0` 停机前要发一个 `^D`（ASCII 4）？
  - **答案**：见 crt0.S 第 83 行注释「Send ^D to terminate serial console program on FPGA」。在 FPGA 上板时，串口监控程序（`serial_boot` 之类）把 `^D` 当作「程序输出结束」的信号，从而收回串口控制权。模拟器里这个字节只是被 `putc` 到 stdout，没有特殊作用。

- **练习 3**：用 `setcr` 写 `CR_SUSPEND_THREAD` 时传 `-1` 和传 `1` 有什么区别？
  - **答案**：`thread_enable_mask &= ~value`。传 `-1`（全 1）会清掉所有线程的位，挂起全部线程，导致停机；传 `1` 只清最低位（线程 0），只挂起线程 0，其它线程仍可运行，模拟器不会停。这就是「挂起自己」与「停机」的差别。

## 5. 综合实践

把本讲四个模块串起来，完成下面这个贯穿性任务：

> **从零跑通 hello_world，并解释你看到的每一行输出。**

操作步骤：

1. **构建**（参考 u1-l2）：在仓库根目录执行 `cmake . && make hello_world`（或直接 `make`）。构建成功后，进入 `software/apps/hello_world/` 对应的构建目录。

2. **观察产物**：列出该目录，确认存在 `hello_world.elf`、`hello_world.hex`、`hello_world.lst`、以及五个 `run_*` 脚本。用文本工具打开 `hello_world.hex`，看一眼 4.2 节讲的 hex 格式长什么样。

3. **运行**：
   - 方式 A（用脚本）：`./run_emulator`
   - 方式 B（直接调模拟器）：`bin/nyuzi_emulator hello_world.hex`

4. **记录输出**：把终端输出贴下来。预期会看到 `Hello World` 和 `thread enable mask is now zero`（**待本地验证**）。

5. **用本讲学到的知识解释输出**，回答以下问题（写成本讲的「实验报告」）：
   - `Hello World` 这几个字，是经过哪一条函数调用链（`printf → vfprintf → fputc → write_console → _write_uart → 写 0xffff0048 → putc(stdout)`）打到屏幕的？分别指出每一步对应的源码文件。
   - `main` 返回后，`crt0` 在停机前做了哪几件事？为什么写 `CR_SUSPEND_THREAD = -1` 就能让模拟器退出？请引用 `processor.c` 的 `thread_enable_mask &= ~value` 和 `is_proc_halted` 来说明。
   - 如果把 `hello_world.c` 改成 `while (1) ;`，重新构建运行，会发生什么？为什么？（印证 4.4.5 练习 1）

6. **进阶（可选）**：换用 Verilator 跑一遍——`./run_verilator`。对比它与模拟器在「传 hex 的方式」（`+bin=` vs 位置参数，见 4.3.3）和「执行速度」上的差异。注意 Verilator 不支持帧缓冲窗口，但 hello_world 没有图形输出，所以不受影响。

完成这个实践后，你就真正打通了 Nyuzi 的「构建 → 镜像 → 加载 → 执行 → 输出 → 停机」全链路，这也是后续所有讲义依赖的最底层运行模型。

## 6. 本讲小结

- Nyuzi 裸机程序的入口不是 `main`，而是 `crt0.S` 的 `_start`：它设置每线程栈、跑全局构造，然后才 `call main`。
- 模拟器/仿真器不读 ELF，而是读 `$readmemh` 格式的 hex 镜像；`elf2hex` 负责 ELF → hex，`read_hex_file` 负责把 hex 解析进内存数组，程序从地址 0 启动。
- 每个程序目录下的 `run_emulator` / `run_verilator` 等脚本是 CMake 用 `file(GENERATE)` 自动生成的，背后分别调用 `nyuzi_emulator` 和 `nyuzi_vsim`。
- 文本输出走的是内存映射 I/O：`printf` 最终把字符写到 `0xffff0048`（UART/串口寄存器），模拟器在此处用 `putc(stdout)` 转发到宿主终端。
- 程序的停机靠写控制寄存器 `CR_SUSPEND_THREAD`（编号 20）：传 `-1` 会清空 `thread_enable_mask`，模拟器主循环发现「无线程可运行」即退出。
- 硬件、模拟器、软件三方共用同一套控制寄存器编址（`defines.svh` 的 `control_register_t`），这是「同一份程序多环境运行」的基础。

## 7. 下一步学习建议

到这里，u1「项目概览与上手」单元就完整了：你已经知道 Nyuzi 是什么（u1-l1）、怎么构建（u1-l2）、目录怎么导航（u1-l3）、怎么跑第一个程序（本讲）。接下来建议：

- **进入 u2 指令集架构入门**：本讲你看到 `getcr`、`setcr`、`call`、`bz`/`bnz`、`load_32`/`store_*` 等 Nyuzi 指令，但只是当汇编助记符用了。u2-l1 会系统讲解 32 位定长指令格式、标量/向量寄存器组与 16 通道 SIMD。
- **想更懂停机/调度**：直接读 `tools/emulator/processor.c` 的 `execute_instructions` 和控制寄存器处理（u8 模拟器单元会深入）。
- **想更懂启动/库**：阅读 `software/libs/libos/bare-metal/` 下的 `crt0.S`、`uart.c`、`sbrk.c`（u9 软件栈单元会系统讲）。
- **想验证学习效果**：先做本讲第 5 节的综合实践，确认能独立跑通并解释 hello_world，再进入下一单元。
