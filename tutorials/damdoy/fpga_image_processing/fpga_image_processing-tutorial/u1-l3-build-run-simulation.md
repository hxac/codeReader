# 构建并运行：Verilator 仿真模式

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「Verilator 仿真模式」从一条 `verilator` 命令到屏幕上出现一张灰度图的**完整流程**。
- 解释 `build_simulation.sh` 里那两行命令各自在做什么，以及为什么需要它们配合。
- 理解 `-DSIMULATION` 这个宏是如何在「同一份 `main.cpp`」里切换出仿真后端的。
- 看懂 `obj_dir/` 目录与 `Vimage_processing` 这个名字从哪里来、代表什么。
- 能用 `run_gnuplot.sh` 把数值矩阵 `output.dat` 可视化成图像，并能解释其中几条关键 gnuplot 指令的作用。

本讲不深入仿真后端的内部实现（那是后面的专题），只关注「**怎么把它跑起来、中间发生了什么**」。

## 2. 前置知识

在进入正文前，先建立两个直觉。上一讲（u1-l2）已经讲过项目的三段式目录划分和「一个核心 HDL 模块 + 两套可替换后端」的架构，本讲在这些基础上继续。

**直觉一：硬件描述语言（HDL）本来不是用来「运行」的。**
`hdl/image_processing.v` 是 Verilog 代码，它描述的是电路，最终会被综合成真实的逻辑门烧进 FPGA。但在开发阶段，我们不可能每次改一行代码就烧一次芯片——太慢也太麻烦。于是需要一个办法，**把 Verilog 当成普通程序在电脑上「跑」起来**，观察它的输入输出对不对。这个办法就是仿真（simulation）。

**直觉二：Verilator 是「Verilog 到 C++ 的翻译器」。**
Verilator 不是在电脑上模拟一套电路网表（那是事件驱动的传统仿真器如 ModelSim 的做法），而是直接**把 Verilog 翻译成等价的 C++ 代码**，再用普通的 C++ 编译器（g++）编译成可执行文件。这样仿真速度极快，而且能和我们的 C++ 主机程序 `main.cpp` 天然地链接在一起。这就是本项目选择 Verilator 的原因。

下面几个术语会在文中反复出现：

| 术语 | 含义 |
|------|------|
| Verilator | 把 Verilog 翻译成 C++ 的工具，本项目用它做仿真 |
| 综合工具（yosys） | 把 Verilog 变成 FPGA 比特流的工具（硬件模式才用，本讲不涉及） |
| `obj_dir/` | Verilator 生成的所有 C++ 文件所在的目录 |
| `Vimage_processing` | Verilator 生成的 C++ 类名/可执行名（`V` + 顶层模块名） |
| `output.dat` | 主机程序把结果像素写成的纯文本矩阵文件 |
| gnuplot | 一个绘图工具，这里用它把矩阵渲染成灰度图 |

## 3. 本讲源码地图

本讲只涉及 3 个关键文件，它们正好串成一条「构建 → 运行 → 可视化」的链路：

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [build_simulation.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh) | 仿真模式的构建脚本 | 两行核心命令：`verilator` 翻译 + `make` 编译 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机程序入口（两套后端共用） | `#ifdef SIMULATION` 切换后端、写 `output.dat` |
| [run_gnuplot.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/run_gnuplot.sh) | 可视化脚本 | 一条 gnuplot 指令把矩阵渲染成灰度图 |

理解了这 3 个文件，你也就理解了「仿真模式」的全部操作面。

## 4. 核心概念与源码讲解

### 4.1 Verilator 编译命令：把 Verilog 翻译成 C++

#### 4.1.1 概念说明

仿真模式的第一步，是用 Verilator 把核心模块 `hdl/image_processing.v` 翻译成 C++ 源码。但光有「翻译产物」还不够——Verilator 翻译出来的是一个**类（class）**，它本身不会自己跑起来，必须有一个 C++ 程序去实例化它、给它喂输入、读它的输出。

这个「驱动程序」在本项目里就是仿真后端 `simulation/image_processing_simulation.cpp`，再加上业务逻辑入口 `software/main.cpp`。所以构建过程必须把这三样东西——**一个 `.v`（硬件）+ 两个 `.cpp`（驱动 + 业务）**——连到一起。

#### 4.1.2 核心流程

`build_simulation.sh` 整体只有三步动作：

```text
1. 删除旧的 obj_dir/            （保证干净重建）
2. verilator 翻译 + make 编译   （核心，下面拆开讲）
3. 把可执行文件复制成 ./simu    （方便运行）
```

其中第 2 步实际是**两条命令**串起来：

- 命令 A（`verilator ...`）：**翻译 + 生成一个 Makefile**。它读入 `image_processing.v`，生成一堆 C++ 文件和一个 `Vimage_processing.mk`，但**还不会编译**。
- 命令 B（`make ...`）：用 A 生成的 Makefile 真正编译、链接，产出可执行文件 `Vimage_processing`。

这种「先 verilator 生成 mk，再 make 编译」的两段式是 Verilator 的标准用法。

#### 4.1.3 源码精读

构建脚本非常短，先看全貌：

[build_simulation.sh:1-7](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L1-L7) —— 脚本头与清理旧目录。注意第 3 行的注释 `#compile simple_cpu` 是历史遗留（从别的项目复制过来的），与本项目无关，属于源码里的小瑕疵。

关键的第一条命令：

[build_simulation.sh:10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L10) —— Verilator 翻译命令。逐个参数解释：

- `-Wall`：打开 Verilator 的全部警告。
- `--cc`：生成 C++（而不是 SystemC）输出，即产生 `.h`/`.cpp` 和一个 Makefile。
- `hdl/image_processing.v`：要翻译的 Verilog 源文件。它的**顶层模块名是 `image_processing`**——这一点直接决定了后面生成物的名字。
- `--exe`：表示「最终要产出一个可执行文件」，因此后面紧跟的 `.cpp` 会被当作这个可执行文件的源码一起纳入工程。
- `simulation/image_processing_simulation.cpp` 和 `software/main.cpp`：这两个 C++ 文件就是 `--exe` 要链接进去的「驱动 + 业务」。**Verilator 会把 `.v` 翻译成的 C++ 与这两个 `.cpp` 一起写进同一个 Makefile**，等 `make` 时统一编译链接。

> 一句话：这条命令把「1 个 Verilog 模块 + 2 个 C++ 文件」登记进了同一个工程，并生成编译所需的全部文件和 Makefile。

第二条命令：

[build_simulation.sh:13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L13) —— 用 Verilator 生成的 Makefile 真正编译。逐个参数解释：

- `CXXFLAGS="-g -DSIMULATION"`：**这是本讲最关键的一个开关**——向 C++ 编译器传入 `-DSIMULATION` 宏定义（`-g` 只是带调试符号）。它如何起作用，见 4.3 节。
- `-j`：并行编译，加快速度。
- `-C obj_dir`：等价于先 `cd obj_dir` 再 make。
- `-f Vimage_processing.mk`：用 Verilator 刚刚生成的这个 Makefile。
- `Vimage_processing`：要构建的目标（即可执行文件名）。

最后一步：

[build_simulation.sh:15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L15) —— 把编译产物复制到仓库根目录并改名为 `simu`。这样无论你在哪个目录，直接 `./simu` 就能运行。

#### 4.1.4 代码实践

**实践目标**：亲手走查第一条 `verilator` 命令，弄清到底有哪些文件被连在了一起。

**操作步骤**：

1. 打开 [build_simulation.sh:10](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L10)。
2. 数清楚这条命令里出现了几个 `.v`、几个 `.cpp`，分别写出它们的全名。
3. 如果本地已安装 Verilator（README 要求 3.874 版本），运行 `./build_simulation.sh`，然后执行 `ls obj_dir/`，看看生成了哪些文件。

**需要观察的现象**：

- 第一条命令登记的文件是：`hdl/image_processing.v`、`simulation/image_processing_simulation.cpp`、`software/main.cpp`（**1 个 Verilog + 2 个 C++**）。
- `obj_dir/` 里应出现诸如 `Vimage_processing.h`、`Vimage_processing.cpp`、`Vimage_processing.mk`、以及最终的可执行文件 `Vimage_processing` 等。

**预期结果**：你能用一句话回答「`verilator --exe` 把哪几个 `.cpp` 与哪个 `.v` 连在了一起」。如果本地没有 Verilator，可只完成第 1、2 步（源码阅读型实践），并标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `--exe` 去掉，构建会缺什么？
**答案**：缺「最终可执行文件」。没有 `--exe`，Verilator 只会生成库和类，不会把 `main.cpp` 链接成可运行的 `Vimage_processing`，也就无法直接 `./simu`。

**练习 2**：为什么顶层模块名 `image_processing` 很重要？
**答案**：Verilator 生成的 C++ 类名 = `V` + 顶层模块名 = `Vimage_processing`，可执行文件和 Makefile 也都叫这个名字。改名会导致后续 `#include "../obj_dir/Vimage_processing.h"` 和 `cp obj_dir/Vimage_processing simu` 全部对不上。

---

### 4.2 obj_dir 与 Vimage_processing 生成物

#### 4.2.1 概念说明

上一节说 Verilator 会生成一堆文件放在 `obj_dir/` 里，并产出一个叫 `Vimage_processing` 的东西。这里要分清「`Vimage_processing` 这一个名字其实指代了三个不同的对象」：

1. **一个 C++ 类** `Vimage_processing`（在 `obj_dir/Vimage_processing.h` 中声明）——它是 Verilog 模块 `image_processing` 在 C++ 世界里的化身。你在 C++ 里操作这个类的对象，就相当于在操作那个硬件模块的引脚。
2. **一个可执行文件** `obj_dir/Vimage_processing`——`make` 编译链接出来的程序，也就是被复制成 `./simu` 的那个。
3. **一份 Makefile** `obj_dir/Vimage_processing.mk`——`make` 用的构建脚本。

这三个同名对象之间的关系是：`.mk` 编译 `.h/.cpp`（类）+ 你的 `main.cpp`，链接出可执行文件。

#### 4.2.2 核心流程

```text
hdl/image_processing.v
        │  verilator --cc --exe ...
        ▼
obj_dir/Vimage_processing.h   ← C++ 类（模块化身）
obj_dir/Vimage_processing.cpp ← 类的实现
obj_dir/Vimage_processing.mk  ← Makefile
        │  make -f Vimage_processing.mk
        ▼
obj_dir/Vimage_processing     ← 可执行文件
        │  cp ... simu
        ▼
./simu                         ← 你实际运行的程序
```

#### 4.2.3 源码精读

`Vimage_processing` 这个类被仿真后端直接 include 并实例化：

[simulation/image_processing_simulation.hpp:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp#L5) —— `#include "../obj_dir/Vimage_processing.h"`，把 Verilator 生成的类引入仿真后端。

[simulation/image_processing_simulation.hpp:50](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp#L50) —— 成员 `Vimage_processing *simulator;`，即仿真后端内部持有一个指向该类对象的指针。这个 `simulator` 对象的每个成员变量就对应 Verilog 模块的一个端口（如 `clk`、`comm_cmd` 等），改这些成员再调用 `eval()` 就是在「驱动硬件」。

可执行文件本身则通过 `main.cpp` 的 `main()` 函数获得入口：

[software/main.cpp:221](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L221) —— `int main()`，这是链接进 `Vimage_processing` 可执行文件的真正入口。Verilator 的 `--exe` 模式要求你提供的某个 `.cpp` 里有 `main()`，这里由 `software/main.cpp` 提供。

#### 4.2.4 代码实践

**实践目标**：确认「类、可执行文件、Makefile」三者真实存在，并理解它们的依赖。

**操作步骤**：

1. 阅读 [simulation/image_processing_simulation.hpp:5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp#L5)，确认 `Vimage_processing.h` 是被 include 进来的（而非手写的）。
2. 若本地已构建：执行 `ls obj_dir/`，找出 `Vimage_processing.h`、`Vimage_processing.mk`、可执行文件 `Vimage_processing` 三者。
3. 执行 `./simu`，观察它是否在当前目录生成 `output.dat`（见 4.4 节）。

**需要观察的现象**：运行 `./simu` 后，屏幕会打印若干 `status out ... : 0x..` 之类的日志（来自 `main.cpp` 里 `test_*` 函数的 `printf`），并在当前目录下多出一个 `output.dat` 文件。

**预期结果**：`output.dat` 是一个纯文本文件，里面是一堆 0~255 的数字，每行若干个、用空格分隔。这就是结果图像的像素矩阵。若无法运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Vimage_processing`、`Vimage_processing.h`、`Vimage_processing.mk` 三者谁是「因」谁是「果」？
**答案**：`.h`/`.cpp`（类）和 `.mk`（Makefile）是 Verilator 生成的「因」；可执行文件 `Vimage_processing` 是 `make` 用 `.mk` 编译这些产物 + `main.cpp` 后得到的「果」。

**练习 2**：为什么 `simulation/image_processing_simulation.hpp` 里写的是 `Vimage_processing *simulator` 而不是 `image_processing *simulator`？
**答案**：因为在 C++ 仿真世界里，Verilog 模块 `image_processing` 已经被翻译成了名为 `Vimage_processing` 的 C++ 类（带 `V` 前缀），C++ 代码里只认这个类名。

---

### 4.3 SIMULATION 宏开关：同一份 main.cpp，切换两个后端

#### 4.3.1 概念说明

上一讲（u1-l2）已经指出：`main.cpp` 是两套后端**共用**的同一份文件，靠 `SIMULATION` / `ICE40` 两个宏在两处切换后端。本节我们要看清楚这个宏**到底是从哪里来的、在哪两处起作用**。

关键在于：源码里并没有人 `#define SIMULATION`——这个宏完全来自构建脚本的命令行参数 `-DSIMULATION`，由编译器在编译 `main.cpp` 时注入。换一个构建脚本（硬件模式用 `-DICE40`），同一份 `main.cpp` 就编译出完全不同的后端。

#### 4.3.2 核心流程

```text
build_simulation.sh 第13行
   make CXXFLAGS="-g -DSIMULATION" ...     ← 宏从命令行注入
                  │
                  ▼  g++ 编译 main.cpp 时，SIMULATION 已被定义
main.cpp:
   #ifdef SIMULATION                        ← 作用点 1：选 include 哪个后端头文件
   #elif ICE40
   ...
   #ifdef SIMULATION                        ← 作用点 2：选 new 哪个后端对象
   #elif ICE40
```

因此「后端切换」的本质是：**构建脚本决定定义哪个宏 → 宏决定 `main.cpp` 编译进哪个后端的头文件和对象**。

#### 4.3.3 源码精读

宏的注入点（回顾 4.1.3）：

[build_simulation.sh:13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L13) —— `CXXFLAGS="-g -DSIMULATION"`，`-D` 就是 g++ 的「定义宏」开关，效果等价于在所有 `.cpp` 文件最开头写了 `#define SIMULATION`。

宏的两个作用点之一——**选择头文件**：

[software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) —— 仿真模式时 include 仿真后端 `image_processing_simulation.hpp`；硬件模式时 include `image_processing_ice40.hpp`。两个后端都继承自同一个纯虚基类 `Image_processing`（见 [software/image_processing.hpp:8](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8)），所以下面用基类指针调用时接口完全一致。

宏的两个作用点之二——**选择实例化的对象**：

[software/main.cpp:226-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230) —— `#ifdef SIMULATION` 时 `new Image_processing_simulation()`，否则 `new Image_processing_ice40()`。注意赋值号左边是基类指针 `Image_processing *img_proc`（[software/main.cpp:224](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L224)）——这正是多态：后续 `test_*` 函数通过基类指针调用虚函数（如 `send_image`、`wait_end_busy`），实际执行的是哪个后端的实现，完全由这里 `new` 的是谁决定。

> 关键认知：仿真模式下，`-DSIMULATION` 让 `main.cpp` 编译进**仿真后端**，而仿真后端内部又实例化了 Verilator 生成的 `Vimage_processing` 类（见 4.2.3）。整条链是：宏 → 后端 → Verilator 模型。

#### 4.3.4 代码实践

**实践目标**：从命令行到源码，完整追踪 `-DSIMULATION` 的来龙去脉。

**操作步骤**：

1. 在 [build_simulation.sh:13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L13) 找到 `-DSIMULATION`。
2. 在 [software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) 和 [software/main.cpp:226-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230) 找到它的两个作用点。
3. **对比思考**：如果改用 `build_ice40.sh`（用 `-DICE40` 而非 `-DSIMULATION`），`main.cpp` 的这两处 `#ifdef` 会走哪个分支？会 `new` 出哪个对象？（注意：硬件后端依赖 `-lftdi`，直接用 `build_simulation.sh` 是无法编出硬件后端的——这恰恰说明宏切换是「二选一」的。）

**需要观察的现象**：理解「同一份 `main.cpp`，因编译期宏不同而编译出两个不同的可执行程序」这一机制。

**预期结果**：能说出 `-DSIMULATION` 在 `main.cpp` 里同时决定了「include 哪个后端头文件」和「new 哪个后端对象」两件事。本步骤为源码阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：如果在 `build_simulation.sh` 里既不加 `-DSIMULATION` 也不加 `-DICE40`，编译 `main.cpp` 会怎样？
**答案**：两个 `#ifdef` 分支都不进入，`img_proc` 既没有 include 对应头文件、也没有 `new` 出任何对象，编译会报错（未定义类型/未声明标识符）。这说明两个宏必须二选一。

**练习 2**：为什么 `test_add_threshold` 等函数的参数类型是 `Image_processing *`（基类指针），而不是具体的后端类型？
**答案**：因为要靠多态让同一份 `test_*` 代码同时适用于仿真和硬件两个后端。基类定义了统一契约（[software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp)），具体实现由宏选出的子类提供。

---

### 4.4 output.dat 与 gnuplot 可视化

#### 4.4.1 概念说明

仿真的最终目的是「看结果」。但仿真后端算出来的结果是一堆数字（像素值 0~255），不是图像。我们需要两步把它变成「看得见的图」：

1. `main.cpp` 把结果像素按一定格式写成纯文本文件 `output.dat`。
2. `run_gnuplot.sh` 调用 gnuplot，把这个文本矩阵渲染成灰度图窗口。

这里的关键是：**`output.dat` 的格式必须和 gnuplot 的期望对得上**，否则图就错了。本项目刻意把 `output.dat` 写成了 gnuplot 的「matrix」格式，于是一条 `plot ... matrix w image` 就能直接渲染。

#### 4.4.2 核心流程

```text
image_output[] 像素数组（0~255）
        │  main.cpp 的写文件循环
        ▼
output.dat：每行 = 图像一行，行内像素用空格分隔
        │  run_gnuplot.sh
        ▼
gnuplot 窗口：矩阵渲染成灰度图（0=黑，255=白）
```

gnuplot 的 `matrix` 数据格式约定是：**文件里每个数字之间用空白分隔，每一行就是矩阵的一行**。`main.cpp` 的写法正好满足。

#### 4.4.3 源码精读

先看 `main.cpp` 如何写 `output.dat`：

[software/main.cpp:222](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L222) —— `fopen("output.dat", "w")` 打开输出文件。

[software/main.cpp:264-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L264-L269) —— 写像素的核心循环：

```c
for (size_t i = 0; i < image_height*image_width; i++) {
   fprintf(output_file, "%d ", image_output[i]);   // 每个像素后跟一个空格
   if( ((i+1) % (image_width)) == 0){
      fprintf(output_file, "\n");                    // 凑满一行宽度就换行
   }
}
```

这段代码的逻辑是：逐个像素写成十进制数 + 空格，每当写满 `image_width` 个像素（即一整行）就换行。最终 `output.dat` 长这样（示意）：

```text
12 45 78 ... （共 image_width 个数）
34 56 90 ...
...
```

这正是 gnuplot `matrix` 格式。

再看可视化脚本：

[run_gnuplot.sh:3](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/run_gnuplot.sh#L3) —— 整条 gnuplot 指令（`--persist` 让窗口保持打开；`-e` 直接执行引号内的命令）。逐段拆解：

| 片段 | 作用 |
|------|------|
| `set palette gray` | 设为灰度调色板（随后被下一句覆盖，效果相同） |
| `set yrange [] reverse` | **翻转 y 轴**：gnuplot 默认把矩阵第 0 行画在**底部**，但图像约定第 0 行在**顶部**，`reverse` 把它正过来（`[]` 表示范围仍自动） |
| `set cbrange [0:255]` | 把调色板的输入范围固定为 0~255，正好匹配 8 位像素取值 |
| `set palette defined (0 "black", 255 "white")` | 定义 0→黑、255→白的渐变，与上一句配合实现真正的灰度映射（这句会覆盖最前面的 `gray`） |
| `plot "output.dat" matrix w image noti` | 把 `output.dat` 当作矩阵、用 `image` 画法渲染成图；`noti` 表示不显示图例（no title/key） |

其中最需要理解的是**两条**指令：

1. **`set yrange [] reverse`**：解决「上下颠倒」问题。gnuplot 的坐标系 y 轴向上为正，所以矩阵第一行（y=0）落在画面底部，导致图像看起来是倒置的；`reverse` 把 y 轴反向，第一行就回到了画面顶部，图像就「立」起来了。
2. **`set cbrange [0:255]`**：解决「灰度映射」问题。像素值本身就是 0~255，把调色板（color box）的范围也设成 0~255，gnuplot 才会把 0 映射成黑、255 映射成白、中间值映射成对应灰度，形成正确的灰度图。

#### 4.4.4 代码实践

**实践目标**：亲手验证 `output.dat` 格式与两条关键 gnuplot 指令的作用。

**操作步骤**：

1. 阅读 [software/main.cpp:264-269](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L264-L269)，确认它写出的文件就是「每行 = 图像一行、空格分隔」的矩阵。
2. 若本地已运行过 `./simu`：用文本编辑器打开 `output.dat`，数一数每行有几个数字，是否等于图像宽度。
3. **对比实验**：复制 `run_gnuplot.sh`，做一个去掉 `reverse` 的版本（即删掉 `set yrange [] reverse;`），分别运行两个脚本，观察图像是否上下翻转。
4. **对比实验**：再做一个把 `set cbrange [0:255]` 改成 `set cbrange [0:128]` 的版本，观察原本偏亮的区域（>128）是否全部「过曝」成白色（因为超过 128 的值被钳到调色板顶端）。

**需要观察的现象**：

- 去掉 `reverse`：图像上下颠倒（原本在顶部的内容跑到底部）。
- 改窄 `cbrange`：超过范围上限的像素全部变成同一种最亮色，丢失灰度层次。

**预期结果**：你能用自己的话解释「`reverse` 修正图像方向、`cbrange` 决定灰度映射范围」。若本地无 gnuplot（README 要求 5.0 版本），可只做第 1 步源码阅读，并标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `main.cpp` 写文件时的 `"%d "`（带空格）改成 `"%d"`（不带空格），gnuplot 还能正确渲染吗？
**答案**：不能。相邻数字会黏在一起（如 `122 45` 变成 `12245`），gnuplot 会把它当成一个数，矩阵列数就全错了，图会严重变形。空格（或换行）是 gnuplot 区分数字的分隔符。

**练习 2**：为什么 `cbrange` 必须设成 `[0:255]` 而不能用自动范围？
**答案**：像素值固定是 0~255（8 位灰度）。若让 gnuplot 自动选范围，它可能按数据实际最小/最大值缩放，导致不同图像的「同一灰度值」显示成不同亮度，无法横向比较；固定 `[0:255]` 才保证「数值↔亮度」的映射恒定。

---

## 5. 综合实践

把本讲的四条线（构建 → 生成物 → 宏切换 → 可视化）串起来，完成一次**端到端的「改调用 → 重建 → 可视化」循环**。

**任务**：体验「主机改一行测试选择 → 重新仿真 → 看到新结果图」的完整开发闭环。

**操作步骤**：

1. 打开 [software/main.cpp:252-260](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L252-L260)，这是「测试选择区」。可以看到当前只有 `test_simple_edge_detection` 没被注释（[software/main.cpp:256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L256)），其余 `test_*` 都被注释掉了。
2. 选一个**不同的**测试函数取消注释（例如 `test_add_threshold`，[software/main.cpp:253](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L253)），同时把 `test_simple_edge_detection` 重新注释掉。**注意：本讲只是「阅读并说明」这一改动会带来什么，不要真的去修改源码（本讲义禁止改源码）；若要实际验证，请在自己的副本里操作。**
3. 说明改动后，重新执行 `./build_simulation.sh && ./simu && ./run_gnuplot.sh`，`output.dat` 的内容会变成新运算（如「加法+阈值」）的结果，gnuplot 窗口里也会显示一张完全不同的灰度图。

**预期结果**：

- 你能说清楚：改 `main.cpp` 里取消注释哪个 `test_*`，等价于让主机对硬件模块发送**不同的命令序列**；重新仿真后，`output.dat` 是该命令序列的运算结果，gnuplot 则把它可视化出来。
- 这条闭环（`build_simulation.sh` → `./simu` → `run_gnuplot.sh`，对应 README 第 155–161 行的说明）就是本讲所讲的「仿真模式」的全部日常用法。

若本地环境不全，请把第 3 步标注为「待本地验证」，并改为源码阅读型实践：对比 `test_add_threshold`（[software/main.cpp:38-73](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L38-L73)）和 `test_simple_edge_detection`（[software/main.cpp:116-151](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L116-L151)）调用了哪些不同的接口，说明它们会命令硬件做不同的运算。

## 6. 本讲小结

- 仿真模式的核心是「**Verilator 把 Verilog 翻译成 C++**」，再用普通 `make` 编译成可执行文件。
- `build_simulation.sh` 用两条命令完成构建：`verilator ... --exe` 把 `image_processing.v` 与 `image_processing_simulation.cpp`、`main.cpp` 登记进一个工程并生成 Makefile；`make` 再编译出可执行文件。
- 生成物集中在 `obj_dir/`，其中 `Vimage_processing` 一个名字指代了「C++ 类 / 可执行文件 / Makefile」三样东西；顶层模块名 `image_processing` 决定了这个前缀。
- `-DSIMULATION` 宏从 `make` 命令行注入，在 `main.cpp` 的**两处**（include 头文件、`new` 后端对象）起作用，使同一份 `main.cpp` 编译出仿真后端而非硬件后端。
- 结果像素被 `main.cpp` 写成 gnuplot `matrix` 格式的 `output.dat`，`run_gnuplot.sh` 用 `plot ... matrix w image` 渲染成灰度图。
- `set yrange [] reverse` 修正图像上下方向，`set cbrange [0:255]` 固定灰度映射范围，二者是正确显示灰度图的关键。

## 7. 下一步学习建议

本讲只覆盖了「**怎么把仿真跑起来**」，但还没有回答：

- 仿真后端 `Image_processing_simulation` 内部是怎么驱动那个 `Vimage_processing` 模型的？（比如它怎么模拟时钟、怎么模拟存储器读写、怎么处理反压？）
- `main.cpp` 里那些 `test_*` 函数发出的接口调用，最终在硬件侧对应哪些命令？

建议的后续阅读顺序：

1. **先读 [u1-l5 主机程序入口与测试流程](u1-l5-host-main-flow.md)**：把 `main.cpp` 的端到端调用顺序（`send_params → send_image → 运算 → read_image`）和 `test_*` 函数看明白，理解一次完整运算的接口序列。
2. 再进入第 2 单元，学习 `Image_processing` 抽象基类与 `Commands` 枚举这一「贯穿全项目的契约」。
3. 等到第 6 单元（仿真后端专题）再回头深挖 `Image_processing_simulation` 内部的 `main_loop_clk` 时钟驱动与 `memory[]` 读写模拟。

如果想立刻动手验证本讲内容，请按 README「Needed tools」一节准备好 Verilator 3.874 与 gnuplot 5.0，然后在仓库根目录依次执行 `./build_simulation.sh`、`./simu`、`./run_gnuplot.sh`。
