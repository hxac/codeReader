# 目录结构与文件分工

## 1. 本讲目标

上一讲（u1-l1）我们从概念上认识了项目：一个核心 HDL 模块 + 两套可替换后端（仿真 / 硬件）。本讲我们要把这套架构「落到磁盘上」——看清楚仓库里每个目录、每个文件分别扮演什么角色。

学完本讲，你应该能够：

- 准确说出仓库的「三段式」目录划分：核心 HDL（`hdl/`）、仿真后端（`simulation/`）、硬件后端（`ice40/`），外加主机软件（`software/`）。
- 把任意一个源码文件归类到正确的子系统，并指出它会被哪个构建脚本编译/综合。
- 解释「为什么仿真代码和硬件代码要被物理隔离在两个不同的目录里」——这背后是真实存在的工程原因，而不是随手放的。

## 2. 前置知识

本讲只需要你已经读过 u1-l1，理解以下两个概念即可：

- **核心模块与后端分离**：`image_processing.v` 是「大脑」，它只认「命令 + 数据」，不关心数据是怎么传进来的（仿真用 C++ 队列，硬件用 SPI 线）。
- **两套后端**：仿真后端用 Verilator 把 Verilog 编译成 C++ 模型；硬件后端把 Verilog 综合成比特流烧进 iCE40 芯片，再通过 SPI 与主机通信。

如果你还不熟悉这两个概念，请先回到 u1-l1。本讲不会再重复它们的原理，而是专注在「这些代码在文件系统里长什么样」。

另外补充两个本讲会用到的术语：

- **综合（synthesis）**：把 Verilog 源码翻译成 FPGA 能识别的硬件网表/比特流的过程，工具是 yosys。这是「硬件后端」独有的步骤，仿真后端不需要。
- **编译（compile）**：这里特指用 gcc/g++ 把 C++ 主机程序编成可执行文件（仿真模式下还包括 Verilator 生成的 C++ 模型）。

## 3. 本讲源码地图

本讲涉及的文件不多，但它们构成了整个仓库的骨架。理解了这一张地图，后面所有讲义你都能快速定位。

| 文件 | 所属子系统 | 作用 |
|------|-----------|------|
| [README.md](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md) | 文档 | 项目说明、命令表、架构图、构建运行指引 |
| [hdl/image_processing.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v) | 核心 HDL | 唯一的核心处理模块，两套后端共用 |
| [simulation/image_processing_simulation.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.cpp) | 仿真后端 | 用 FIFO 队列驱动 Verilator 模型 |
| [ice40/hdl/top.v](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v) | 硬件后端 HDL | 顶层模块，把核心模块、RAM、SPI 三者连起来 |
| [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp) | 主机软件 | 业务逻辑入口，两套后端共用一份 |
| [software/image_processing.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp) | 主机软件 | 纯虚基类 + 命令枚举，是两套后端的统一契约 |
| [build_simulation.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh) | 脚本 | 编译仿真模式 |
| [build_ice40.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh) | 脚本 | 编译硬件模式的主机软件 |
| [ice40/hdl/Makefile](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile) | 硬件后端脚本 | 综合并烧录 iCE40 比特流 |

## 4. 核心概念与源码讲解

### 4.1 hdl 核心模块目录

#### 4.1.1 概念说明

`hdl/` 目录里只有一个文件：`image_processing.v`。它是整个项目的「心脏」——所有的图像处理逻辑（加法、阈值、卷积、双缓冲管理……）都在这一个 Verilog 模块里。

它的关键特征是**与运行环境完全解耦**：这个模块既不知道自己在被仿真，也不知道自己在真实芯片上跑。它只对外暴露两类接口：

- **存储器接口**：读写图像数据的地址/数据线（由谁提供真实 RAM，它不管）。
- **通信接口**：接收命令和数据的握手线（命令从哪来，它也不管）。

正因为如此，仿真后端可以用 C++ 数组「假装」是 RAM、用队列「假装」是通信线；硬件后端可以用真实 SPRAM 芯片和 SPI 接口。两种情况下，`image_processing.v` 一行都不用改。

> 承接 u1-l1：上一讲说的「双后端架构」之所以可行，根本原因就是这个模块被放在了独立的 `hdl/` 目录里、且不依赖任何后端。本讲后续会看到，两套后端分别以「包含」或「例化」的方式把它接进来。

#### 4.1.2 核心流程

`image_processing.v` 的「被使用」流程在两套后端里不同：

- **仿真后端**：Verilator 直接把 `hdl/image_processing.v` 编译成 C++ 类 `Vimage_processing`，仿真代码 new 出这个类的对象来驱动它（详见 4.2）。
- **硬件后端**：顶层模块 `ice40/hdl/top.v` 用 Verilog 的 `` `include `` 把它 textual 地包含进来，再例化为一个子模块（详见 4.3）。

无论哪种方式，`image_processing.v` 这份源码本身是**唯一且共享**的——这正是它单独放在 `hdl/` 的意义。

#### 4.1.3 源码精读

构建脚本里可以直接看到 `hdl/image_processing.v` 是如何被「作为核心」引入仿真后端的：

[build_simulation.sh:10-13](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_simulation.sh#L10-L13) —— Verilator 命令把 `hdl/image_processing.v` 与仿真 C++ 文件、主机 `main.cpp` 连在一起编译，并用 `-DSIMULATION` 宏标记这是仿真模式。

```bash
verilator -Wall --cc hdl/image_processing.v --exe simulation/image_processing_simulation.cpp software/main.cpp
make CXXFLAGS="-g -DSIMULATION" -j -C obj_dir -f Vimage_processing.mk Vimage_processing
```

可以看到 `hdl/image_processing.v` 出现在命令行最前面，是被编译的核心硬件描述。

而在硬件后端一侧，它是被 `top.v` 包含进来的（`top.v` 第 1 行），这部分我们在 4.3 节细看。

#### 4.1.4 代码实践

**实践目标**：确认 `hdl/` 目录的「纯粹性」——它真的只放核心模块、不掺杂任何后端代码。

**操作步骤**：

1. 在仓库根目录运行 `ls hdl/`，观察里面有哪些文件。
2. 再运行 `grep -rn "Verilator\|Vimage_processing\|SPI\|SB_SPRAM\|ftdi" hdl/`，看核心模块里是否出现任何后端/平台相关的关键字。

**需要观察的现象**：

- `hdl/` 下应当只有 `image_processing.v` 一个文件。
- 上述 grep 在 `hdl/image_processing.v` 中应当**没有任何匹配**——核心模块对仿真器、SPI、SPRAM、FTDI 全都一无所知。

**预期结果**：这验证了核心模块与后端彻底解耦。如果你看到了平台关键字，那说明核心模块被「污染」了（本项目里不会发生）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `image_processing.v` 不直接放在 `ice40/hdl/` 或 `simulation/` 里，而要单独建一个 `hdl/`？

**参考答案**：因为它被两套后端共享。如果放进某一个后端目录，就会产生「谁拥有它」的歧义，也容易让人误以为它属于那个后端。独立放在中立的 `hdl/` 目录，从物理位置上就表明它是「与平台无关的核心」。

**练习 2**：`hdl/image_processing.v` 对外暴露哪两类接口？（提示：回忆 u1-l1 与 4.1.1）

**参考答案**：存储器接口（地址/读写使能/数据线）和通信接口（命令/数据输入输出 + 握手信号）。它不暴露任何「如何到达这些接口」的信息。

---

### 4.2 simulation 仿真后端目录

#### 4.2.1 概念说明

`simulation/` 目录里有一对文件：

- `image_processing_simulation.hpp`：仿真后端的类声明。
- `image_processing_simulation.cpp`：仿真后端的实现。

它们共同定义了一个类 `Image_processing_simulation`，它**实现了**主机侧的统一接口（`Image_processing`，定义在 `software/image_processing.hpp`），但内部不是和真实硬件通信，而是：

- 把主机发来的每个高层调用（如 `send_add`）**排进一个命令队列**。
- 然后用一个 `main_loop_clk()` 函数**手动翻转时钟**，驱动 Verilator 生成的 `Vimage_processing` 模型消化这些命令。
- 用一个 C++ 数组 `memory[]` **假装是 RAM**，响应核心模块的读写请求。

也就是说，仿真后端用纯软件的方式，把核心模块运行所需的「存储器」和「通信线」都用 C++ 模拟了出来。

#### 4.2.2 核心流程

仿真后端是如何被「选中」并接入主程序的？靠的是编译宏 `SIMULATION` 和条件编译：

1. `build_simulation.sh` 用 `-DSIMULATION` 编译 `main.cpp`。
2. `main.cpp` 顶部的 `#ifdef` 据此包含仿真后端的头文件。
3. `main()` 里同样用 `#ifdef` new 出 `Image_processing_simulation` 对象。

之后，`main.cpp` 里所有通过 `img_proc->` 调用的接口，都走进仿真实现，而仿真实现再去驱动 Verilator 模型。

#### 4.2.3 源码精读

主机程序用条件编译在「仿真头文件」和「硬件头文件」之间二选一：

[software/main.cpp:14-18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) —— 仿真模式下包含 `../simulation/image_processing_simulation.hpp`，硬件模式下包含 `../ice40/software/image_processing_ice40.hpp`。

```cpp
#ifdef SIMULATION
#include "../simulation/image_processing_simulation.hpp"
#elif ICE40
#include "../ice40/software/image_processing_ice40.hpp"
#endif
```

注意这里的相对路径 `../simulation/...`：从 `software/main.cpp` 往上一层到仓库根，再进 `simulation/`。这说明仿真后端与主机软件是**平级的两个目录**，互不嵌套。

随后在 `main()` 里，同一个宏决定 new 出哪个后端对象：

[software/main.cpp:226-230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230) —— 仿真模式 new 的是 `Image_processing_simulation`，硬件模式 new 的是 `Image_processing_ice40`，但两者都赋值给基类指针 `Image_processing *img_proc`，这就是多态切换后端的实现。

```cpp
#ifdef SIMULATION
img_proc = new Image_processing_simulation();
#elif ICE40
img_proc = new Image_processing_ice40();
#endif
```

#### 4.2.4 代码实践

**实践目标**：看清仿真后端与主机软件之间的「平级目录 + 相对路径包含」关系。

**操作步骤**：

1. 打开 [simulation/image_processing_simulation.hpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/simulation/image_processing_simulation.hpp)，找到类名和它继承的基类。
2. 确认它继承自 `Image_processing`（定义在 `software/image_processing.hpp`），并且注意它是怎么引用那个头文件的（同样是 `../software/...` 相对路径）。

**需要观察的现象**：仿真后端的头文件里，类的声明形如 `class Image_processing_simulation : public Image_processing`，并且文件顶部有形如 `#include "../software/image_processing.hpp"` 的引用。

**预期结果**：两个目录互相用 `../对方目录/文件` 的方式引用，呈对称结构。这种「平级 + 相对引用」是本项目的组织惯例。

#### 4.2.5 小练习与答案

**练习 1**：仿真后端为什么不需要 `top.v`、`ram_interface.v`、`spi_interface.v` 这些硬件后端文件？

**参考答案**：因为仿真里没有真实 RAM 芯片，也没有 SPI 线。RAM 用 C++ 的 `memory[]` 数组模拟，通信用 FIFO 队列模拟。核心模块 `image_processing.v` 需要的存储器和通信接口，都被仿真后端用纯软件实现填补了，所以不需要那些硬件外设模块。

**练习 2**：如果把 `SIMULATION` 宏改成同时定义 `SIMULATION` 和 `ICE40`，`main.cpp` 会发生什么？

**参考答案**：`#ifdef SIMULATION ... #elif ICE40 ... #endif` 是互斥分支（`#elif`），`SIMULATION` 先命中后就不再看 `ICE40`，所以会按仿真模式编译。这也说明两个宏是「二选一」的设计。

---

### 4.3 ice40 硬件后端目录

#### 4.3.1 概念说明

硬件后端比仿真后端复杂得多，因为它要面对真实芯片。所以 `ice40/` 目录**自己又分了两层**：

- **`ice40/hdl/`**：FPGA 侧的 Verilog（要被综合成比特流烧进芯片）。
  - `top.v`：顶层模块，把核心模块、RAM 接口、SPI 接口三者连线。
  - `ram_interface.v`：用 4 片 iCE40 内置 SPRAM 拼出 128KB 存储。
  - `spi_interface.v`：用 iCE40 内置的 `SB_SPI` 硬件块做 SPI 从机。
  - `Makefile`：调用 yosys / arachne-pnr / icepack 综合，调用 iceprog 烧录。
  - `io.pcf`：引脚约束文件，告诉工具每根信号连到芯片的哪个物理引脚。
- **`ice40/software/`**：PC 主机侧的 C++ 软件（要通过 FTDI 芯片和 FPGA 通信）。
  - `image_processing_ice40.cpp` / `.hpp`：实现 `Image_processing` 接口，把高层调用翻译成 SPI 事务。
  - `spi_lib/spi_lib.c` / `spi_lib.h`：底层 FTDI/MPSSE 驱动（源自 iceprog），负责真正收发 SPI 字节。

注意这个划分的对称美：硬件后端的「FPGA 侧」和「主机侧」都被收拢在同一个 `ice40/` 目录下，因为它们**必须成对配合**才能跑通硬件模式。

#### 4.3.2 核心流程

硬件模式的构建分两条独立的线，由两个不同的脚本负责（这是本讲最容易混淆的一点，请留意）：

1. **综合比特流**（把 Verilog 变成芯片能跑的东西）：在 `ice40/hdl/` 里执行 `make`，它用 yosys 综合顶层 `top.v`、用 arachne-pnr 布局布线、用 icepack 打包出 `top.bin`，再用 `make prog` 烧录。
2. **编译主机软件**（让 PC 能和芯片说话）：在仓库根目录执行 `build_ice40.sh`，它用 g++ 编译主机 C++ 程序。

关键点：`top.v` 通过 `` `include `` 把核心模块 `hdl/image_processing.v` 以及 `ram_interface.v`、`spi_interface.v` 一起拉进综合流程，所以 yosys 虽然只看到 `top.v` 一个输入，却能把四个 `.v` 文件都综合进去。

#### 4.3.3 源码精读

`top.v` 最开头三行就是它的「组装清单」：

[ice40/hdl/top.v:1-3](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L1-L3) —— 顶层用 `` `include `` 包含核心模块（路径回到上两级的 `../../hdl/image_processing.v`）以及两个硬件外设模块。

```verilog
`include "../../hdl/image_processing.v"
`include "ram_interface.v"
`include "spi_interface.v"
```

第一行的 `../../hdl/image_processing.v` 特别值得注意：它从 `ice40/hdl/` 往上两级回到仓库根，再进 `hdl/`，把那个**与平台无关的核心模块**抓了过来。这就是核心模块在硬件后端里的接入方式。

随后 `top.v` 把三者例化并连线（这里只看模块名与实例名）：

[ice40/hdl/top.v:32-42](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/top.v#L32-L42) —— 例化 `image_processing`、`ram_interface`、`spi_interface` 三个实例，并用一堆 `wire` 把它们的端口连起来。

```verilog
image_processing image_processing(.clk(clk), .reset(reset_ip),
...
ram_interface ram_interface(.clk(clk), .addr(ip_mem_addr), .wr_en(ip_mem_wr_en), .rd_en(ip_mem_rd_en),
...
spi_interface spi_interface(.clk(clk), .spi_sck(SPI_SCK), ...
```

而综合这些 Verilog 的脚本，是 `ice40/hdl/Makefile`：

[ice40/hdl/Makefile:4-15](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile#L4-L15) —— `build` 目标做 yosys→arachne-pnr→icepack 三步综合；`prog` 用 `iceprog -S` 烧进 SRAM（断电丢失），`prog_flash` 烧进 FLASH（持久）。

```makefile
build:
	yosys -p "synth_ice40 -blif $(filename).blif" $(filename).v
	arachne-pnr -d 5k -P sg48 -p $(pcf_file) $(filename).blif -o $(filename).asc
	icepack $(filename).asc $(filename).bin
prog:
	iceprog -S $(filename).bin
prog_flash:
	iceprog $(filename).bin
```

注意：综合只把 `top.v`（即 `$(filename).v`）作为输入传给 yosys，其余 `.v` 靠 `top.v` 里的 `` `include `` 传递进来。

至于主机侧软件，编译它的是另一个脚本：

[build_ice40.sh:1-4](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh#L1-L4) —— 用 `g++ -DICE40` 编译主机软件，链接 FTDI 库（`-lftdi`）。注意第一行 `echo "TODO"` 表示这个脚本本身还没完全成型，但第 3 行的编译命令是可用的。

```bash
echo "TODO"
g++ -DICE40 ice40/software/spi_lib/spi_lib.c ice40/software/image_processing_ice40.cpp software/main.cpp -o soft_ice40 -lftdi
```

这一行清楚地展示了硬件后端「主机侧」的全部 C++ 源文件：`spi_lib.c`（FTDI 底层）+ `image_processing_ice40.cpp`（接口翻译）+ `software/main.cpp`（业务逻辑，两套后端共用）。它**不编译任何 `.v` 文件**——Verilog 的综合是 `ice40/hdl/Makefile` 的职责。

#### 4.3.4 代码实践

**实践目标**：理清硬件后端「FPGA 侧用 Makefile 综合」与「主机侧用 build_ice40.sh 编译」这条容易混淆的分工线。

**操作步骤**：

1. 在 [ice40/hdl/Makefile](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/ice40/hdl/Makefile) 中确认：它处理的全是 `.v / .pcf / .blif / .asc / .bin` 这些硬件文件，完全没有 `.cpp`。
2. 在 [build_ice40.sh](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/build_ice40.sh) 中确认：它处理的全是 `.c / .cpp` 主机文件，完全没有 `.v`。

**需要观察的现象**：两个脚本各管一摊，文件类型完全不重叠——Makefile 只碰硬件描述，build_ice40.sh 只碰主机程序。

**预期结果**：你会得出结论——硬件模式要跑通，必须先 `make`（综合烧录 FPGA），再 `build_ice40.sh`（编译主机软件），两步缺一不可，且顺序上通常先烧 FPGA、再跑主机软件去连它。

#### 4.3.5 小练习与答案

**练习 1**：`ice40/` 目录下为什么还要再分 `hdl/` 和 `software/` 两个子目录？

**参考答案**：因为硬件模式同时涉及「跑在 FPGA 上的代码」（Verilog，要综合）和「跑在 PC 上的代码」（C++，要编译）。它们用的是完全不同的工具链（yosys vs g++），产出也完全不同（比特流 vs 可执行文件），物理隔离能避免混淆。

**练习 2**：`top.v` 第 1 行 `` `include "../../hdl/image_processing.v" `` 里的 `../../` 是什么意思？

**参考答案**：从 `ice40/hdl/` 目录出发，`..` 回到 `ice40/`，再 `..` 回到仓库根目录，然后进入 `hdl/`。这正好印证了「核心模块独立放在仓库根的 `hdl/` 下，被各后端以相对路径引用」的组织方式。

**练习 3**：`build_ice40.sh` 能不能替代 `ice40/hdl/Makefile` 的工作？

**参考答案**：不能。前者只编译主机 C++ 程序，后者才负责把 Verilog 综合成比特流。两者是硬件模式的两条独立构建线，互不替代。

---

### 4.4 software 主机程序与图像资源目录

#### 4.4.1 概念说明

`software/` 目录放的是**与后端无关、被两套后端共用**的主机侧资产：

- `main.cpp`：业务逻辑入口。它写了一堆 `test_*` 函数（加法、阈值、卷积……），并负责读取输入图像、写 `output.dat`。**这一份代码在仿真模式和硬件模式下完全相同**，靠 `#ifdef` 切换后端。
- `image_processing.hpp`：纯虚基类 `Image_processing` 和 `Commands` 枚举。它是两套后端必须遵守的「契约」——无论仿真还是硬件，都要实现这套接口。
- `images/`：测试图像，是用 GIMP 导出的 C 头文件（如 `image_fruits_64.h`），把图像像素编码成可打印字符直接编进程序。

> 承接 u1-l1：上一讲说两套后端通过「纯虚基类」形成统一契约，主机业务逻辑与底层解耦。这个契约文件就住在 `software/image_processing.hpp`，它是整个项目最关键的「接口边界」。

#### 4.4.2 核心流程

主机程序的组织逻辑可以这样概括：

1. `image_processing.hpp` 定义抽象接口 `Image_processing`（能做什么）。
2. `simulation/` 和 `ice40/software/` 各写一个子类实现这套接口（怎么做）。
3. `main.cpp` 只面向抽象基类编程（`Image_processing *img_proc`），用 `#ifdef` 决定 new 哪个子类。
4. `main.cpp` 用 `images/` 里的头文件作为输入图像，把处理结果写到 `output.dat`。

这样一来，`main.cpp` 的所有 `test_*` 函数都写成「对 `img_proc->` 发命令」的形式，完全感知不到底层是仿真还是硬件。

#### 4.4.3 源码精读

抽象契约本身只占两个文件、几十行，却定义了整个项目的接口边界：

[software/image_processing.hpp:4-6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6) —— `Commands` 枚举列出了所有硬件命令的操作码，两套后端都要用同一套编码打包字节。

```cpp
enum Commands {COMMAND_PARAM, COMMAND_SEND_IMG, COMMAND_READ_IMG, COMMAND_GET_STATUS, COMMAND_APPLY_ADD, COMMAND_APPLY_THRESHOLD,
               COMMAND_SWITCH_BUFFERS, COMMAND_BINARY_ADD, COMMAND_APPLY_INVERT,
               COMMAND_CONVOLUTION, COMMAND_BINARY_SUB, COMMAND_BINARY_MULT, COMMAND_APPLY_MULT, COMMAND_NONE=255};
```

[software/image_processing.hpp:8-39](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8-L39) —— 纯虚基类 `Image_processing`，声明了 `send_params / send_image / send_add / send_convolution / read_image / switch_buffers / wait_end_busy` 等一系列「`= 0`」的纯虚函数。仿真后端和硬件后端都要把它们全部实现。

而 `main.cpp` 顶部则把所需的图像资源 include 进来：

[software/main.cpp:1-12](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L1-L12) —— include 了 `images/image_fruits_64.h`（当前启用）和抽象接口头文件；其余图像头文件被注释掉，说明切换测试图像只需改这里的 include。

```cpp
// #include "images/peppers128.h"
#include "images/image_fruits_64.h"
// #include "images/image_fruits_8.h"
// #include "images/image_sequential.h"

#include "image_processing.hpp"
```

可以看到，`main.cpp` 只引用 `software/` 自己目录下的东西（`images/` 子目录和 `image_processing.hpp`），后端头文件则由 4.2 节那段的 `#ifdef` 引入。

#### 4.4.4 代码实践

**实践目标**：直观感受「同一份 main.cpp，靠 #ifdef 适配两套后端」的设计。

**操作步骤**：

1. 打开 [software/main.cpp](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp)，找到所有 `#ifdef SIMULATION` / `#elif ICE40` 出现的位置（至少两处：顶部 include、`main()` 里 new 对象）。
2. 数一数：除了这两处条件编译，`main.cpp` 的其余代码（所有 `test_*` 函数、图像加载、写 `output.dat`）里还有没有出现 `simulation` 或 `ice40` 字样？

**需要观察的现象**：除了那两处 `#ifdef`，业务逻辑里完全不区分后端——所有 `test_*` 函数都只调用 `img_proc->` 的抽象接口。

**预期结果**：这正是「主机业务逻辑与底层解耦」的直接证据。如果把后端切换看作一个开关，那么这个开关只存在于那两处 `#ifdef`，其余上百行业务代码毫无感知。

#### 4.4.5 小练习与答案

**练习 1**：`software/image_processing.hpp` 里的 `Image_processing` 为什么全是纯虚函数（`= 0`）？

**参考答案**：因为它是一个「契约」/接口，本身不包含任何实现，只规定「能做什么」。具体「怎么做」交给仿真子类和硬件子类各自实现。这种纯虚基类是多态切换后端的基础。

**练习 2**：为什么测试图像是 `.h`（C 头文件）而不是 `.png` / `.jpg`？

**参考答案**：因为主机程序是 C++，把图像直接编成头文件就能 `#include` 进来，无需任何图像解码库或文件 I/O。这是嵌入式/硬件项目里常见的「把资源固化进程序」的做法（这些 `.h` 是用 GIMP 的「导出为 C 源码」功能生成的）。具体的编码格式会在 u2-l3 讲义里详细拆解。

**练习 3**：`main.cpp` 是「两套后端共用一份」的，那么它在仿真模式和硬件模式下分别被哪个脚本编译？

**参考答案**：仿真模式下被 `build_simulation.sh` 编译（带 `-DSIMULATION`，并与 Verilator 模型链接）；硬件模式下被 `build_ice40.sh` 编译（带 `-DICE40`，并与 FTDI 库 `-lftdi` 链接）。同一份 `main.cpp` 源码，两种宏定义产出两个不同的可执行文件。

---

## 5. 综合实践

本讲的综合实践就是规格里要求的核心任务：**把整个仓库的每个源码文件归类，并标注它会被哪个构建脚本处理**。这是一次「亲手绘制项目地图」的练习，做完后你对目录结构的理解会非常牢固。

### 实践目标

用 `git ls-files` 列出全部文件，制作一张分类表，把每个文件归到下面五类之一，并标出它被哪个脚本编译/综合：

| 分类 | 含义 |
|------|------|
| 核心 HDL | 与平台无关的核心硬件描述 |
| 仿真后端 | 只在仿真模式下使用的代码 |
| 硬件后端 | 只在硬件模式下使用的代码（含 FPGA 侧 Verilog 与主机侧 C++） |
| 主机软件 | 两套后端共用的主机代码与资源 |
| 资源与脚本 | 文档、构建脚本、示例图等 |

### 操作步骤

1. 在仓库根目录运行 `git ls-files`，拿到完整文件清单。
2. 对每个文件，根据本讲学到的目录归属进行分类。
3. 对每个**源码文件**（`.v / .c / .cpp / .hpp`），判断它会被哪个脚本处理：
   - `build_simulation.sh`（仿真编译）
   - `build_ice40.sh`（硬件主机编译）
   - `ice40/hdl/Makefile`（硬件综合）
   - 注意：有的文件会被多个脚本处理（如 `main.cpp` 被两个脚本共用；`hdl/image_processing.v` 既被仿真编译，又被硬件综合），要全部标出。

### 参考答案表

下面是按本讲内容整理的完整归类（你可以对照自己的答案）。脚本列中「仿真」「主机」「综合」分别指上面三个脚本。

| 文件 | 分类 | 被哪个脚本处理 |
|------|------|----------------|
| `README.md` | 资源与脚本 | —（文档） |
| `.gitignore` | 资源与脚本 | —（配置） |
| `build_simulation.sh` | 资源与脚本 | —（脚本本身） |
| `build_ice40.sh` | 资源与脚本 | —（脚本本身） |
| `run_gnuplot.sh` | 资源与脚本 | —（脚本本身） |
| `hdl/image_processing.v` | 核心 HDL | 仿真编译 + 硬件综合（两套都用） |
| `simulation/image_processing_simulation.cpp` | 仿真后端 | 仿真编译 |
| `simulation/image_processing_simulation.hpp` | 仿真后端 | 仿真编译（被 include） |
| `ice40/hdl/top.v` | 硬件后端（FPGA 侧） | 硬件综合 |
| `ice40/hdl/ram_interface.v` | 硬件后端（FPGA 侧） | 硬件综合（被 top.v include） |
| `ice40/hdl/spi_interface.v` | 硬件后端（FPGA 侧） | 硬件综合（被 top.v include） |
| `ice40/hdl/Makefile` | 硬件后端（脚本） | —（综合脚本本身） |
| `ice40/hdl/io.pcf` | 硬件后端（约束） | 硬件综合（引脚约束） |
| `ice40/software/image_processing_ice40.cpp` | 硬件后端（主机侧） | 硬件主机编译 |
| `ice40/software/image_processing_ice40.hpp` | 硬件后端（主机侧） | 硬件主机编译（被 include） |
| `ice40/software/spi_lib/spi_lib.c` | 硬件后端（主机侧） | 硬件主机编译 |
| `ice40/software/spi_lib/spi_lib.h` | 硬件后端（主机侧） | 硬件主机编译（被 include） |
| `software/main.cpp` | 主机软件 | 仿真编译 + 硬件主机编译（两套共用） |
| `software/image_processing.hpp` | 主机软件 | 两个脚本都 include（共用契约） |
| `software/images/image_fruits_64.h` | 主机软件（资源） | 被 main.cpp include |
| `software/images/image_fruits_8.h` | 主机软件（资源） | 被 main.cpp include（当前注释掉） |
| `software/images/image_sequential.h` | 主机软件（资源） | 被 main.cpp include（当前注释掉） |
| `examples/*.png`（15 张） | 资源与脚本 | —（README 示例图） |

> 待本地验证：上表中「被哪个脚本处理」一列是基于源码 include 关系与脚本命令行推断的。建议你在本地实际跑一次 `build_simulation.sh` 与（如有硬件环境）`build_ice40.sh`，观察编译器实际打开了哪些文件，以最终确认。

### 进阶思考

完成表格后，回答这个问题：**为什么 `main.cpp` 和 `hdl/image_processing.v` 是唯二「被两套后端共享」的文件？**

提示：一个是业务逻辑入口（主机侧），一个是处理逻辑核心（硬件侧）。它们分别代表了「软件复用」和「硬件描述复用」两个层面，正是这套「一个核心 + 两套后端」架构得以成立的两个支点。

## 6. 本讲小结

- 仓库采用**三段式 + 主机软件**的目录划分：`hdl/`（核心 HDL）、`simulation/`（仿真后端）、`ice40/`（硬件后端）、`software/`（共用主机软件）。
- `hdl/image_processing.v` 是唯一的核心模块，被两套后端以不同方式接入（仿真用 Verilator 编译，硬件用 `top.v` 的 `` `include ``），这是它能独立成目录的根本原因。
- 仿真后端（`simulation/`）与硬件后端（`ice40/`）**被物理隔离**，因为它们面对的运行环境完全不同：仿真用 C++ 数组/队列模拟 RAM 与通信，硬件用真实 SPRAM 与 SPI。
- 硬件后端 `ice40/` 内部又分 `hdl/`（FPGA 侧，由 `Makefile` 综合）和 `software/`（主机侧，由 `build_ice40.sh` 编译）两条独立构建线，工具链完全不同。
- 主机程序 `software/main.cpp` 是**一份代码两套后端共用**，仅靠 `#ifdef SIMULATION/ICE40` 在 include 和 `new` 对象两处切换；统一契约定义在 `software/image_processing.hpp`。
- 构建脚本各司其职：`build_simulation.sh` 编译仿真、`build_ice40.sh` 编译硬件主机软件、`ice40/hdl/Makefile` 综合硬件比特流，彼此文件类型不重叠。

## 7. 下一步学习建议

现在你已经能在文件系统里准确定位每个模块了，接下来的讲义会沿着「怎么把这些文件跑起来」推进：

- **u1-l3 构建并运行：Verilator 仿真模式**：动手执行 `build_simulation.sh`，看 Verilator 如何把 `hdl/image_processing.v` 编成 C++ 模型，并用 `run_gnuplot.sh` 把 `output.dat` 可视化。
- **u1-l4 构建并运行：iCE40 硬件模式**：走一遍 yosys + arachne-pnr + icepack + iceprog 的硬件工具链，理解 `ice40/hdl/Makefile` 与 `io.pcf` 的作用。
- **u1-l5 主机程序入口与测试流程**：逐行拆解 `software/main.cpp` 的 `main()` 与各 `test_*` 函数，看清一次完整图像处理的调用链。

如果你更想先理解「两套后端为什么能共用一份 main.cpp」的契约设计，也可以跳到 **u2-l1 抽象基类与命令枚举**，那里会深入 `software/image_processing.hpp` 的每一个纯虚函数。
