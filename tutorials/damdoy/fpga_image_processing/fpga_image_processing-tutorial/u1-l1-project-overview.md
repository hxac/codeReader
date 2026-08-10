# 项目总览：用 Verilog 在 FPGA 上做图像处理

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是带你从「零」认识这个项目。读完本讲你应该能够：

- 说清楚这个项目到底要解决什么问题：**在资源受限、低功耗、低成本的 FPGA 芯片上完成图像处理**。
- 理解项目的整体架构思想：**一个核心 HDL 模块 + 两套可替换的后端（Verilator 仿真 / iCE40 真实硬件）**。
- 看懂项目支持哪些图像操作，并能按「逐像素运算 / 双图运算 / 卷积」三大类对它们归类。
- 理解把图像存进硬件所依赖的**双缓冲（input / storage）模型**。

本篇不要求你懂 Verilog 语法细节，也不要求你跑通编译——这些会在后面的讲义里逐步展开。本篇只要求你建立「项目全貌」的认知。

---

## 2. 前置知识

在开始之前，先用最通俗的话理解几个关键词。

- **FPGA（现场可编程门阵列）**：一种「可以重新连线」的芯片。普通 CPU 是写好的电路，你只能给它写软件指令；FPGA 则允许你**用硬件描述语言（如 Verilog）直接画出电路**，让算法变成真正跑在电路上的硬件逻辑。
- **iCE40 UltraPlus**：本项目使用的目标芯片系列。它属于「低端」FPGA——便宜、功耗低，但资源有限（本项目用它片上 1Mbit 内存）。这种限制正是本项目设计取舍的来源。
- **Verilog / HDL**：硬件描述语言，用来写 FPGA 的电路。本项目核心算法用 Verilog 写在 `image_processing.v` 里。
- **Verilator**：一个把 Verilog 代码「翻译」成 C++ 程序的工具。这样你不用插上真实 FPGA，也能在电脑上用软件模拟硬件的行为，方便调试。
- **灰度图像 / 像素**：本项目处理的图像是**单通道灰度图**，每个像素是一个 0~255 的数值（0 黑，255 白）。彩色图（RGB）会被预先降成单通道（见后续讲义）。

> 一句话直觉：这个项目想做的事，相当于「把一段在 CPU 上跑的图像处理程序，改写成跑在 FPGA 芯片上的硬件电路」。

---

## 3. 本讲源码地图

本讲主要从「俯瞰」视角理解项目，涉及的关键文件不多：

| 文件 | 作用 | 本讲用它来理解什么 |
|------|------|--------------------|
| `README.md` | 项目说明书 | 项目定位、操作清单、架构图、命令表、工具链 |
| `software/main.cpp` | 主机端 C++ 主程序 | 一段图像处理从头到尾的调用流程、两种后端如何被选择 |
| `software/image_processing.hpp` | 抽象接口（纯虚基类） | 「一套接口、两套实现」的契约 |
| `hdl/image_processing.v` | 核心 Verilog 模块 | 双缓冲存储参数与端口（本讲只看存储模型概览） |

> 后面几节会反复引用这几个文件，并给出带行号的永久链接，方便你点开对照。

---

## 4. 核心概念与源码讲解

### 4.1 项目定位与目标平台

#### 4.1.1 概念说明

理解一个项目，先问三个问题：**它是什么？给谁用？为什么这么做？**

- **是什么**：一个用 Verilog 实现的简单图像处理系统。
- **目标平台**：iCE40 UltraPlus 这类低端 FPGA。
- **为什么**：这类芯片便宜、省电，但资源非常有限。项目作者故意选择「在受限硬件上做出能用的图像处理」，因此所有的设计（存储方式、运算方式）都围绕「省资源」展开。

这是理解后续所有讲义的总纲：当你看到项目里出现「为什么用 16 位存两个像素」「为什么用定点数而不是浮点」「为什么单口 RAM 要分两拍」这些奇怪做法时，答案通常都指向一个词——**资源受限**。

#### 4.1.2 核心流程

从「项目想做什么」到「最终交付什么」，可以这样概括：

1. 用 Verilog 写出一个核心图像处理模块（`image_processing.v`）。
2. 这个模块不依赖具体怎么通信，它只认「命令 + 数据」。
3. 提供两种使用方式：电脑上用 Verilator 仿真验证、真实 iCE40 板子上运行。
4. 主机端用 C++ 程序（`main.cpp`）发命令、送图像、收结果。

#### 4.1.3 源码精读

README 的开头一句话就点明了项目定位与目标平台：

> [README.md:L3-L5](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L3-L5) —— 说明项目围绕一个核心模块 `image_processing.v`，目标是低端 FPGA，且明确点出「both in price and power consumption」（兼顾价格和功耗）。

README 的「Needed tools」一节列出了构建所需的工具链，从这里能看出项目「双形态」的影子：

> [README.md:L177-L182](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L177-L182) —— 列出了 Verilator（仿真用）、yosys（综合 FPGA 比特流用）、gnuplot（看结果用）、FTDI 库（硬件模式下主机和 FPGA 通信用）。**这几样工具正好对应后面要讲的两套后端。**

#### 4.1.4 代码实践

这是一个「源码阅读型实践」，不需要运行任何命令。

1. **实践目标**：把工具链和「项目要实现的目标」对应起来。
2. **操作步骤**：打开 [README.md:L177-L182](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L177-L182)，针对每个工具写一句话说明它在本项目里扮演什么角色。
3. **需要观察的现象**：你会注意到 Verilator 与 yosys 是「互斥」的——前者把 Verilog 变成 C++ 仿真程序，后者把 Verilog 变成 FPGA 比特流。
4. **预期结果**：你应当得到类似「Verilator=仿真后端的核心工具；yosys=硬件后端的核心工具」的结论。
5. **若无法确定**：可标注「待本地验证」并在后续讲义（u1-l3、u1-l4）中确认。

#### 4.1.5 小练习与答案

**练习 1**：本项目为什么强调目标是「low end fpga devices」？这对设计有什么影响？
**参考答案**：因为这类芯片便宜省电，但内存和逻辑单元有限。影响是：项目必须用很省资源的做法，比如用定点数代替浮点、用单口 RAM 配合两拍流水、把两个像素打包进一个 16 位字等。

**练习 2**：Verilator 和 yosys 在本项目里是「二选一」的关系，还是「都需要」的关系？
**参考答案**：取决于用哪种模式——仿真模式只需要 Verilator，硬件模式只需要 yosys（加 arachne-pnr 等）。对单次构建而言是二选一；但作为学习项目，两者都会用到。

---

### 4.2 双后端架构（仿真 / 硬件）

#### 4.2.1 概念说明

本项目的灵魂设计是：**同一份核心 Verilog 代码，既能跑在电脑仿真里，也能跑在真实 FPGA 上**。这就是所谓的「双后端」。

- **仿真后端（simulation）**：用 Verilator 把 Verilog 编译成 C++ 类，主机程序直接调用这个类，不需要任何硬件。优点是快速、好调试。
- **硬件后端（ice40）**：把 Verilog 综合成比特流烧进 iCE40 FPGA，主机程序通过 SPI 接口和真实芯片通信。优点是真实、能验证最终硬件行为。

为了让主机程序（`main.cpp`）**对这两种后端完全无感**，项目在中间放了一层抽象接口 `Image_processing`（在 `image_processing.hpp` 里定义）。`main.cpp` 只跟这个抽象接口对话，具体走仿真还是走硬件，由编译时的宏决定。

#### 4.2.2 核心流程

数据流可以这样画（这也是本讲综合实践里你要自己整理的框图）：

```
                         +-------------------------+
                         |  software/main.cpp      |
                         |  （主机程序，只认抽象接口）|
                         +-----------+-------------+
                                     |
                                     v
                    +----------------+------------------+
                    |  Image_processing（抽象基类 hpp）  |
                    +--------+---------------------+----+
                             |                     |
              仿真模式(SIMULATION)            硬件模式(ICE40)
                             |                     |
                  +----------v--------+   +--------v-----------+
                  | IP_simulation.cpp |   | IP_ice40.cpp       |
                  | （Verilator C++） |   | （走 FTDI/SPI）    |
                  +----------+--------+   +--------+-----------+
                             |                     |
                  +----------v--------+   +--------v-----------+
                  | Verilator 仿真模型|   | iCE40 FPGA 真实硬件|
                  |  obj_dir/         |   |  image_processing.v|
                  +-------------------+   +--------------------+
```

注意：**左右两条路最终都驱动同一个核心模块 `image_processing.v`**。这正是「一套核心、两套后端」的本质。

#### 4.2.3 源码精读

README 的 Architecture 一节画出了这个架构，并明确点出「两个实现」对应「两种通信」：

> [README.md:L104-L133](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L104-L133) —— 架构图与说明：`image_processing.hpp` 的两个实现，分别「与 verilator 通信」或「通过 SPI 与 ice40 通信」。

抽象接口本身定义在 `image_processing.hpp`，它是一个纯虚基类：

> [software/image_processing.hpp:L8-L39](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8-L39) —— `class Image_processing` 里全部是 `= 0` 的纯虚函数（如 `send_image`、`send_add`、`read_image`、`switch_buffers` 等）。这就是「契约」：任何后端都必须实现这些函数，`main.cpp` 只调用它们。

`main.cpp` 用编译宏来选择具体后端，这是「双后端切换」的开关所在：

> [software/main.cpp:L14-L18](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L14-L18) —— 根据 `SIMULATION` / `ICE40` 宏包含不同的后端头文件。

> [software/main.cpp:L226-L230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230) —— 在 `main()` 里 `new` 出对应的后端对象，但都赋值给抽象基类指针 `Image_processing *img_proc`，之后一律用基类指针操作（多态）。

#### 4.2.4 代码实践

1. **实践目标**：理解「抽象接口 + 多态后端」如何让 `main.cpp` 与底层解耦。
2. **操作步骤**：
   - 打开 [software/image_processing.hpp:L8-L39](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L8-L39)，数一数有多少个纯虚函数。
   - 再看 [software/main.cpp:L226-L230](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L226-L230)，确认 `img_proc` 的类型是基类指针。
3. **需要观察的现象**：`main.cpp` 里到处调用 `img_proc->send_image(...)`，但**没有任何一处**写死了「这是仿真还是硬件」。
4. **预期结果**：体会到「换后端只需换一个宏 + 换一个 `new`，业务逻辑（test_* 函数）一行都不用改」。
5. **若无法确定**：待本地编译验证（见 u1-l3、u1-l4）。

#### 4.2.5 小练习与答案

**练习 1**：如果把抽象基类去掉，让 `main.cpp` 直接调用仿真类，会带来什么坏处？
**参考答案**：那 `main.cpp` 就必须为仿真和硬件各写一份几乎相同的流程，每加一个后端都要复制一遍；而且换模式要改业务代码。抽象基类把「接口」和「实现」分开，业务代码只依赖接口，符合依赖倒置原则。

**练习 2**：抽象基类里为什么要放 `image_width` / `image_height` 两个 `protected` 成员？
**参考答案**：因为两个后端都需要记住图像尺寸（发命令、读写图像都用到），把它放到基类可以复用，子类通过 `protected` 访问即可，不必各自再声明一份。

---

### 4.3 支持的图像操作清单

#### 4.3.1 概念说明

知道「是什么、怎么搭」之后，还要知道「能做什么」。本项目支持的图像操作可以分成三大类：

1. **逐像素运算（per pixel / unary）**：对 storage 缓冲里每个像素单独做运算，如加一个常数、求反、阈值化、乘一个系数。
2. **双图运算（binary）**：把 input 缓冲和 storage 缓冲里的两幅图做运算（加、减、乘），结果写回 storage。比如「两幅图相减取绝对差」。
3. **3x3 卷积（convolution）**：用一个小矩阵（核）对图像做卷积，可实现高斯模糊、边缘检测等。这是项目里最复杂的部分。

另外还有几个「管理类」操作：切换两个缓冲、把图像载入 input、把图像从 input 读出、查询状态（忙/闲）。

#### 4.3.2 核心流程

这些操作在硬件侧都是通过「命令」来触发的。一条命令 = 1 字节操作码 + 变长参数。简化流程是：

```
主机发命令(操作码+参数)  -->  硬件解析命令  -->  执行对应运算  -->  写回 storage 缓冲
                                                       |
                                         主机查询状态/读回结果
```

注意一个关键约定（README 反复强调）：**大部分运算都在 storage 缓冲上进行，结果也写回 storage**；只有「载入/读出图像」用到 input 缓冲。两幅图运算时，结果同样写进 storage。

#### 4.3.3 源码精读

README 的 Operations 一节把支持的操作列得很清楚，并按三大类分组：

> [README.md:L11-L26](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L11-L26) —— 三大类操作（per pixel / convolution / binary）+ 缓冲管理命令的清单。

这些操作在抽象接口里一一对应成纯虚函数，命令编号则定义在 `Commands` 枚举里：

> [software/image_processing.hpp:L4-L6](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L4-L6) —— `enum Commands` 列出了 `COMMAND_APPLY_ADD`、`COMMAND_CONVOLUTION`、`COMMAND_BINARY_SUB` 等所有命令编号。

`main.cpp` 里为每种操作都写了一个测试函数，是理解「一个操作怎么用」的最佳入口。例如 `test_multiplication` 演示了「乘 0.5」的完整调用：

> [software/main.cpp:L153-L165](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L153-L165) —— `send_params` 设尺寸 → `send_image` 载入 → `switch_buffers` → `send_mult(0.5f, true)` → `wait_end_busy` → `switch_buffers` → `read_image`。这一串就是一次完整运算的标准范式。

> 小提示：乘法、卷积等运算需要「小数」，但 FPGA 上做浮点太贵，所以项目用**定点数**表示。8 位定点 = 1 位符号 + 3 位整数 + 4 位小数，范围约 -7.0~7.0，精度 1/16。详见 [README.md:L95-L102](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L95-L102)。定点数的细节会在讲义 u4-l2 深入。

#### 4.3.4 代码实践

1. **实践目标**：把「三大类操作」与「对应的抽象接口函数」一一对应起来。
2. **操作步骤**：
   - 打开 [README.md:L11-L26](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L11-L26) 的操作清单。
   - 打开 [software/image_processing.hpp:L15-L34](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/image_processing.hpp#L15-L34) 的虚函数列表。
   - 画一张三列表格：`操作类别 | README 里的名称 | 对应的接口函数名`。
3. **需要观察的现象**：你会发现 README 里「binary operation (input op buffer)」下的 add/sub/mult，正好对应 `send_binary_add` / `send_binary_sub` / `send_binary_mult`。
4. **预期结果**：得到一张约 10~12 行的对照表，说明每个高层概念在代码里落在了哪个函数上。
5. **若无法确定**：可标注「待确认」，讲义 u2-l1 会逐个核对。

#### 4.3.5 小练习与答案

**练习 1**：`send_binary_sub` 比 `send_binary_add` 多一个参数 `absolute_diff`，猜猜它干什么用？查 README 命令表验证。
**参考答案**：`absolute_diff` 控制是否取「绝对差」（两幅图相减后取绝对值，避免负数）。README 的 `COMMAND_BINARY_SUB` 一行写到「bit1 is absolute difference」——置 1 时做 `abs(input - storage)`。

**练习 2**：README 提到「卷积可作用于 input 缓冲，并把结果加到 storage」。这在接口上对应哪个参数？
**参考答案**：对应 `send_convolution(kernel, clamp, input_source, add_to_output)` 的后两个参数：`input_source` 决定卷积读哪个缓冲，`add_to_output=true` 时结果「叠加」到 storage 而不是覆盖（用于多方向边缘检测累加）。

---

### 4.4 双缓冲（input / storage）模型概览

#### 4.4.1 概念说明

要把图像存进硬件，最朴素的做法是「一块内存放一幅图」。但本项目用了**两块缓冲**：

- **input 缓冲**：图像「进出」的窗口。主机送进来的图先放这里，要读回去的图也从这里读。
- **storage 缓冲**：运算的「工作台」。绝大多数运算在这里做，结果也写回这里。

两块缓冲的大小相等、地址相邻，可以通过一条命令**互换**。这个设计的好处是：你可以先把图送进 input，再「换」到 storage 去做运算，input 这边空出来又能接收下一幅图或返回结果。这正是双缓冲（double buffer）的典型用法。

#### 4.4.2 核心流程

存储总量与划分（来自源码）：

- 总内存：\( \text{MEMORY\_SIZE} = 128\text{KB} = 131072 \) 字节（即 iCE40 的 1Mbit 片上 RAM）。
- 两个缓冲各占一半：\( \text{BUFFER\_SIZE} = \text{MEMORY\_SIZE}/2 = 64\text{KB} \)。
- storage 缓冲的起始地址 \( \text{BUFFER2\_LOCATION} = \text{MEMORY\_SIZE}/2 = 65536 \)。

操作流程示意：

```
初始/复位:   input@0 , storage@0（复位时同址，初始化命令会校正）
COMMAND_PARAM(设尺寸):  input@0 , storage@65536   ← 两个缓冲各就各位
COMMAND_SWITCH_BUFFERS:  input 与 storage 地址互换
```

> 关键约定：每个 16 位存储字（word）打包了 **2 个像素**。这是因为存储字宽是 16 位，而灰度像素是 8 位，所以一个字正好装下相邻的两个像素——这是项目为「省一半存储访问次数」做的取舍。细节在讲义 u3-l2 展开。

#### 4.4.3 源码精读

README 在开头就说明了双缓冲思想：

> [README.md:L5-L7](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L5-L7) —— 使用 1Mbit 内存，分成 input 和 storage 两个缓冲；图像在 input 进出，运算在 storage 进行；两缓冲可互换；双图运算结果写回 storage。

存储参数定义在核心模块顶部，三个 `parameter` 一目了然：

> [hdl/image_processing.v:L78-L83](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L78-L83) —— `MEMORY_SIZE = 1024*128`、`BUFFER_SIZE = MEMORY_SIZE/2`、`BUFFER2_LOCATION = MEMORY_SIZE/2`，并注释「128KB / 2*64KB / 可存 256×256 单字节像素图」。

两个缓冲的起始地址用两个寄存器保存，**互换它们 = 切换缓冲**：

> [hdl/image_processing.v:L142-L143](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L142-L143) —— `buffer_input_address` 和 `buffer_storage_address` 两个地址寄存器。

收到 `COMMAND_PARAM`（设尺寸，同时做初始化）时，两个缓冲地址被放到正确位置：

> [hdl/image_processing.v:L228-L229](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L228-L229) —— `buffer_storage_address <= BUFFER2_LOCATION; buffer_input_address <= 0;`（storage 在后半段，input 在前半段）。

`COMMAND_SWITCH_BUFFERS` 的实现就是「交换两个地址寄存器的值」：

> [hdl/image_processing.v:L253-L256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L256) —— `buffer_input_address <= buffer_storage_address; buffer_storage_address <= buffer_input_address;`，这就是「互换」的本质。

#### 4.4.4 代码实践

1. **实践目标**：验证「切换缓冲 = 交换地址」这一抽象，并在代码里找到证据。
2. **操作步骤**：
   - 打开 [hdl/image_processing.v:L253-L256](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/hdl/image_processing.v#L253-L256)，确认 `COMMAND_SWITCH_BUFFERS` 只交换了两个地址寄存器，并**没有搬运任何像素数据**。
   - 对照 [software/main.cpp:L153-L165](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/software/main.cpp#L153-L165)，理解为什么 `test_multiplication` 里 `send_mult` 后要 `switch_buffers` 再 `read_image`（因为运算结果在 storage，而读图总是从 input 读，所以要交换让结果「回到」input 侧）。
3. **需要观察的现象**：你会看到「交换缓冲」是一个极廉价的操作——几乎零成本，因为只是改了两个地址寄存器。
4. **预期结果**：能用一句话解释「为什么 storage 上的运算结果，要 switch_buffers 之后才能被 read_image 读出来」。
5. **若无法确定**：可标注「待确认」，讲义 u3-l2 会从存储地址角度完整推导。

#### 4.4.5 小练习与答案

**练习 1**：256×256 的灰度图需要多少字节存储？为什么项目说 128KB 正好够放两幅？
**参考答案**：256×256×1B = 65536B = 64KB。两个缓冲各 64KB，所以 128KB 正好放两幅 256×256 的图。

**练习 2**：`COMMAND_SWITCH_BUFFERS` 既然不搬数据，为什么能「切换」缓冲？
**参考答案**：因为 input/storage 只是「地址寄存器里的两个数值」。交换这两个值，就等于交换了「谁指向前半段、谁指向后半段」。所有后续按 `buffer_input_address` / `buffer_storage_address` 访问的逻辑，会自动作用到「换过来」的那段内存上。

---

## 5. 综合实践

本讲的综合实践把四个模块的知识串成一张图。

**任务**：阅读 README 的 Architecture 章节（[README.md:L104-L133](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L104-L133)）和命令表（[README.md:L72-L93](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L72-L93)），完成下面两件事：

1. **画一张端到端数据流框图**，从「主机软件」出发，经过：
   - `main.cpp`（业务逻辑）
   - `image_processing.hpp`（抽象接口层）
   - 分叉到「仿真后端」或「硬件后端」（两条路）
   - 最终汇到核心 HDL 模块 `image_processing.v`
   
   在框图上标出：哪一段是「抽象」、哪一段是「两套实现」、哪一段是「真正干活的硬件」。可以参考本讲 4.2.2 节的图，但要求你自己再结合 README 的命令表，标注出一次「发送图像 + 做加法 + 读回结果」会在这张图上经过哪些节点。

2. **写一段话（100~200 字）**，用自己的话回答：**这个项目为什么选择在 FPGA 上而不是 CPU 上做图像处理？** 至少提到两点理由，并结合本讲提到的「资源受限」这一前提。

   *提示*：可以从「功耗/价格」「专用硬件并行性」「嵌入式/边缘场景」等角度切入；也可以反过来思考「CPU 做这些不是更简单吗，为什么还要折腾 FPGA」。

**预期产出**：一张手画或软件画的框图 + 一段说明文字。完成后你就具备了进入第二单元（命令接口与主机软件抽象）的全局视野。

> 说明：本综合实践为「源码阅读 + 文档梳理」型，不要求运行命令。若你想顺便确认编译流程，可留到 u1-l3（仿真模式）和 u1-l4（硬件模式）再动手。

---

## 6. 本讲小结

- 项目是一个用 **Verilog 在低端 iCE40 UltraPlus FPGA 上做图像处理**的系统，所有设计取舍都围绕「资源受限」展开。
- 架构核心是**「一个核心 HDL 模块 + 两套可替换后端」**：仿真用 Verilator、硬件走 SPI，二者由编译宏在 `main.cpp` 里切换。
- 抽象基类 `Image_processing` 是贯穿全项目的**契约**：`main.cpp` 只跟它对话，业务逻辑与底层彻底解耦。
- 支持的操作分三大类：**逐像素运算 / 双图运算 / 3x3 卷积**，外加缓冲管理与状态查询。
- 图像存储采用**双缓冲（input / storage）模型**：图像从 input 进出、在 storage 运算，两缓冲可零成本互换（仅交换地址寄存器）。
- 涉及「小数」的运算（乘法、卷积）一律用 **8 位定点数**（1+3+4）替代浮点，以省硬件资源。

---

## 7. 下一步学习建议

本讲建立的是「全局俯瞰」，接下来建议按这个顺序继续：

1. **u1-l2（目录结构与文件分工）**：先把仓库里每个文件归位，知道「核心 HDL / 仿真后端 / 硬件后端 / 主机软件」分别放在哪。
2. **u1-l3 / u1-l4（两种运行方式）**：动手把仿真模式（Verilator）和硬件模式（yosys + iceprog）跑通或至少走查脚本。
3. **u1-l5（主机程序入口与测试流程）**：跟着 `main.cpp` 走一遍一次完整运算的调用链。
4. 进入第二单元后，再系统学习抽象接口、命令协议与图像数据格式。

> 如果你想立刻「看到」本项目能产生什么效果，可以先翻一翻 README 的 Examples 一节（[README.md:L28-L70](https://github.com/damdoy/fpga_image_processing/blob/b1d7480cd804e53a53af48e95850bbf61088f40f/README.md#L28-L70)）里的示例图——加法+阈值、乘法、高斯模糊、边缘检测、两图平均/差分，它们都是本讲提到的三类操作的真实产出。
