# SDPRAM_SINGLECLK：简单双口 RAM 原语

## 1. 本讲目标

学完本讲，你应该能够：

- 读懂底层存储原语 `SDPRAM_SINGLECLK` 的端口、参数与内部寄存器结构。
- 说清楚「简单双口 RAM（Simple Dual-Port RAM）」的写口与读口是如何在同一时钟下并行工作的。
- 理解 `$readmemh` 如何把一个 `.hex` 文件灌进 RAM，以及参数 `RAM_INIT_FILE` 的作用。
- 看懂 `OUTPUT_REG` 这个字符串参数如何用 `generate` 在「1 级读寄存器」和「2 级读寄存器」之间二选一，并由此决定读延迟是 1 拍还是 2 拍。
- 能够自己在仿真中切换 `OUTPUT_REG` 并用波形验证两条读路径。

本讲是 [u3-l1](u3-l1-data-buffer-wrapper.md) 的下钻：u3-l1 把 `DATA_BUFFER` 当作「黑盒拼接」，本讲打开黑盒里那个真正负责存取的原语。

## 2. 前置知识

### 2.1 什么叫「存储原语」

在 FPGA 设计里，「原语（primitive）」指的是最底层、只做一件具体小事的模块。`SDPRAM_SINGLECLK` 只做一件事：**把数据按地址存进去、再按地址读出来**，别的一概不管（不管地址怎么生成、不管什么时候该读写）。

为什么要单独抽出这么一层？因为不同厂商（Intel/Altera、AMD/Xilinx、Gowin、Efinix）的片上 Block RAM/ROM 用法各不相同。把存取逻辑隔离在最底层一个文件里，移植到别的厂商时只需要替换这一个文件（换成厂商官方 BRAM IP），上层控制器 `DPRAM_CONT` 和封装 `DATA_BUFFER` 完全不用动。这正是 u1-l2 提到的「原语被隔离在最底层以便按厂商替换」的设计意图。

### 2.2 单时钟域与同步读

回顾 [u2-l2](u2-l2-audio-clock-model.md)：整个 FIR_x2 只有 `MCLK_I` 一个时钟，`BCK_I`、`LRCK_I` 都被当作「数据信号」而非时钟。因此这个 RAM 也是**单时钟（single clock）**——写口和读口都用 `MCLK_I` 的上升沿驱动，这也正是模块名 `_SINGLECLK` 的含义。

这个 RAM 的读是**同步读（registered read）**：你给出读地址后，数据不会立刻出现，而是要等时钟沿打一拍（甚至两拍）才输出。这一点和组合读的异步 RAM 不同，是后面讲「读延迟」的关键。

### 2.3 非阻塞赋值的「读旧值」特性

在 Verilog 里，用 `<=`（非阻塞赋值）对同一个数组在同一时钟沿「一边写一边读」时，读到的是**本周期开始时的旧值**（read-first / 读优先）。本讲的写口与读口正好分成两个 `always` 块、都挂在 `posedge CLK_I` 上，因此仿真里表现为读优先。不过 FIR_x2 的控制器在读写地址相撞时会主动把读使能拉低（见 [u3-l2](u3-l2-dpram-ring-buffer-controller.md) 的 `REN_O`），所以正常工作时这种同址冲突根本不会发生——读优先还是写优先在这里其实不会触发。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [02_DATA_BUFFER/SDPRAM_SINGLECLK.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v) | **本讲主角**。简单双口 RAM 原语：写口、读口、`$readmemh` 初始化、1/2 级输出寄存器选择。 |
| [02_DATA_BUFFER/DATA_BUFFER.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v) | 上一层封装。把控制器与原语拼起来，决定 `OUTPUT_REG`、`RAM_INIT_FILE` 等参数取值（u3-l1）。 |
| [02_DATA_BUFFER/Questa/BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex) | RAM 初始化文件，被 `$readmemh` 读入，决定上电时缓冲里装的是什么。 |
| [02_DATA_BUFFER/DATA_BUFFER_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER_TB.v) | DATA_BUFFER 的测试激励，本讲用来在仿真中验证原语的两条读路径。 |
| [02_DATA_BUFFER/Questa/run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/run.do) | Questa 波形脚本，告诉我们要在波形里观察哪些信号。 |

---

## 4. 核心概念与源码讲解

### 4.1 双口 RAM 读写时序

#### 4.1.1 概念说明

「双口 RAM（Dual-Port RAM）」是指**可以同时进行两套访问**的存储器。FIR_x2 用的是其中的 **Simple Dual-Port RAM（SDP，简单双口）**：一个口只负责写（写口），另一个口只负责读（读口），两个口各有自己的地址线、使能线和数据线。

为什么输入缓冲需要双口？因为在 FIR 滤波里，**控制器要不断地把新的 PCM 样点写进缓冲，同时又要不停地从历史地址里读出旧样点去做卷积**。如果只有一个口，写和读就得排队轮流用；有了双口，写和读可以同时进行，吞吐翻倍。这正是环形缓冲（[u3-l2](u3-l2-dpram-ring-buffer-controller.md)）能边写边读的物理基础。

关于「写优先 / 读优先」：当写口和读口在**同一周期访问同一地址**时，读口拿到的是新写入的值还是旧值？仿真上本模块表现为**读优先（读旧值）**（原因见 2.3）；而真实 FPGA 的 BRAM IP 通常可以配置成 read-first / write-first / no-change 三种模式之一。但正如 2.3 所说，控制器已经在上游用 `REN` 避开了同址冲突，所以这个语义差异在实际运行中不会被触发。

#### 4.1.2 核心流程

模块对外暴露的端口可以分为「写侧」和「读侧」两组：

```
        ┌─────────────── SDPRAM_SINGLECLK ───────────────┐
 写侧 → │ CLK_I  WADDR_I  WENABLE_I  WDATA_I             │
        │                                                │
        │   ┌──────────── RAM[MEMORY_DEPTH] ──────────┐  │
        │   │  写：posedge 时若 WENABLE_I=1，           │  │
        │   │      RAM[WADDR_I] <= WDATA_I             │  │
        │   └──────────────────────────────────────────┘  │
        │                                                │
 读侧 → │ CLK_I  RADDR_I  RENABLE_I           RDATA_O ←── │
        └────────────────────────────────────────────────┘
```

- **写流程**：每个 `CLK_I` 上升沿，若 `WENABLE_I == 1`，就把 `WDATA_I` 写到 `RAM[WADDR_I]`。
- **读流程**：每个 `CLK_I` 上升沿，若 `RENABLE_I == 1`，就把 `RAM[RADDR_I]` 的内容锁进读寄存器（1 级或 2 级，见 4.3）。
- 写与读是**两个独立的 `always` 块**，互不阻塞，所以「双口」并行。

存储深度由地址位宽决定：

\[
\text{MEMORY\_DEPTH} = 2^{\text{ADDR\_WIDTH}}
\]

也就是说，地址线有 `ADDR_WIDTH` 位，就能寻址 \(2^{\text{ADDR\_WIDTH}}\) 个存储单元。例如默认配置 `ADDR_WIDTH = 8` 时，深度为 \(2^8 = 256\)。

#### 4.1.3 源码精读

先看模块的端口与参数定义。注意有 4 个参数：数据位宽 `DATA_WIDTH`、地址位宽 `ADDR_WIDTH`、输出寄存器开关 `OUTPUT_REG`、初始化文件 `RAM_INIT_FILE`。

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:45-62](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L45-L62) —— 模块声明：写口（`WADDR_I/WENABLE_I/WDATA_I`）与读口（`RADDR_I/RENABLE_I/RDATA_O`）共用同一个时钟 `CLK_I`，这正是「单时钟」双口 RAM。

存储深度由 `localparam` 派生，避免每次手算 \(2^N\)：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:64-65](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L64-L65) —— `MEMORY_DEPTH = 2**ADDR_WIDTH`，存储器数组 `RAM` 的大小就由它定。

接着是写口。这是整个文件里唯一会改变 `RAM` 内容的地方：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:80-85](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L80-L85) —— 写口：`posedge CLK_I` 时若 `WENABLE_I == 1`，则 `RAM[WADDR_I] <= WDATA_I`。注意用的是非阻塞赋值 `<=`。

读口在另一个 `always` 块里，它把 `RAM[RADDR_I]` 读出来打到寄存器上（这部分细节在 4.3 展开）：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:88-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L88-L93) —— 读口：`posedge CLK_I` 时若 `RENABLE_I == 1`，把 `RAM[RADDR_I]` 锁进 `RDATA_REG_1P`，并把上一拍的 `RDATA_REG_1P` 推进到 `RDATA_REG_2P`。

> 上游连线提示：在 [DATA_BUFFER.v:89-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L89-L102) 中，原语的 `CLK_I` 被接到 `MCLK_I`、`WENABLE_I` 接控制器来的 `WEN`、`RENABLE_I` 接 `REN`，而 PCM 数据 `WDATA_I` 与输出 `RDATA_O` 都是直连、不经过控制器——这正是 u3-l1 强调的「数据不碰控制器」。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立端口到物理意义的映射，并确认「读写分属两个 always 块」。

**操作步骤**：

1. 打开 [SDPRAM_SINGLECLK.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v)。
2. 在文件顶部注释的 `Port` 段，为每个端口补一句「谁驱动它」的中文说明。例如：
   - `CLK_I` ← 来自 `DATA_BUFFER` 的 `MCLK_I`（写读共用）。
   - `WENABLE_I` ← 来自控制器 `DPRAM_CONT` 的 `WEN_O`。
   - `RENABLE_I` ← 来自控制器 `DPRAM_CONT` 的 `REN_O`（读写撞址时被拉低）。
3. 在写口 `always` 块旁标注：「写：唯一修改 RAM 的地方」；在读口 `always` 块旁标注：「读：把 RAM 内容锁进寄存器」。

**需要观察的现象**：标注完成后，你能清楚看到写和读在**源码上是完全分离的两个块**。

**预期结果**：你会得到一张「端口 → 驱动来源」对照表，并理解为什么这一层只管存取、不管时序编排。

> 本实践只阅读和加注释，不改变任何逻辑行为，无需运行仿真即可完成；若要真正跑起来验证，配合下面的 4.3 实践一起做。

#### 4.1.5 小练习与答案

**Q1**：模块名里的 `SINGLECLK` 是什么意思？为什么 FIR_x2 的 RAM 可以用单时钟？

**参考答案**：表示写口和读口共用同一个时钟。因为整个 FIR_x2 是单时钟域设计（见 [u2-l2](u2-l2-audio-clock-model.md)），`BCK/LRCK` 都不是时钟而是数据信号，所以 RAM 的读写都用 `MCLK_I` 驱动。

**Q2**：若把 `ADDR_WIDTH` 从 8 改成 9，`MEMORY_DEPTH` 变成多少？存储容量（位）怎么变？

**参考答案**：`MEMORY_DEPTH = 2**9 = 512` 个单元；容量 = \(512 \times \text{DATA\_WIDTH}\)。默认 `DATA_WIDTH=32` 时为 \(512 \times 32 = 16384\) 位。

**Q3**：写口和读口为什么分成两个 `always` 块而不是合在一个？

**参考答案**：分开后写逻辑和读逻辑互不干扰、可同时触发，这正是「双口并行」的体现；合并反而会引入人为的先后依赖。另外分开写也方便厂商替换（真实 BRAM 的读写往往是两套独立端口）。

---

### 4.2 `$readmemh` 初始化

#### 4.2.1 概念说明

RAM 上电时，每个单元的值是不确定的（仿真里是 `x`）。但 FIR_x2 的输入缓冲需要在**复位阶段就被预填满有效数据**（见 [u3-l2](u3-l2-dpram-ring-buffer-controller.md) 的「复位预填充」），否则滤波器一开始工作就会读到一堆 `x`，污染输出。

`$readmemh` 是 Verilog 的系统任务，作用是：**把一个文本文件里的十六进制数，按顺序塞进一个 memory 数组**。文件每行一个十六进制值，从地址 0 开始依次填充。这样设计者就可以用一个 `.hex` 文件预先定义 RAM 的初始内容。

参数 `RAM_INIT_FILE` 让这个文件路径可配置：默认是 `"BUFFER_INIT.hex"`，如果传空字符串 `""` 就跳过初始化（保持 `x`）。

#### 4.2.2 核心流程

```
            RAM_INIT_FILE = "BUFFER_INIT.hex"
                          │
   文件内容（每行一个 hex）  │   $readmemh(文件, RAM)
   00000000  ← 地址 0      │ ───────────────────────►  RAM[0] = 0
   00000000  ← 地址 1      │                          RAM[1] = 0
   00000000  ← 地址 2      │                          RAM[2] = 0
        ...                │                            ...
```

- `initial` 块在仿真开始时（`t=0`）执行一次。
- 若 `RAM_INIT_FILE != ""`，调用 `$readmemh(RAM_INIT_FILE, RAM)`，按行把十六进制数写进 `RAM`，从下标 0 开始递增。
- 若文件行数少于 `MEMORY_DEPTH`，剩余单元保持 `x`；若多于 `MEMORY_DEPTH`，多出的被忽略（仿真器通常给一个 warning）。

> 注意区分两类文件（这是 [u1-l3](u1-l3-simulation-flow.md) 强调过的）：`.hex` 文件被 `$readmemh` 读、用来初始化**存储**；`.txt` 文件被 `$fscanf` 读、用来在仿真过程中喂**输入信号**。两者不可混淆。

#### 4.2.3 源码精读

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:73-78](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L73-L78) —— 初始化：`initial` 块里判断 `RAM_INIT_FILE != ""` 后调用 `$readmemh(RAM_INIT_FILE, RAM)`。`RAM` 就是上一节声明的存储数组。

存储数组本身长这样（每个单元 `DATA_WIDTH` 位宽，共 `MEMORY_DEPTH` 个单元）：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:68-71](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L68-L71) —— 声明 RAM 数组与两个读寄存器 `RDATA_REG_1P`、`RDATA_REG_2P`（后者在 4.3 讲）。

来看实际被加载的文件内容（截选）：

[02_DATA_BUFFER/Questa/BUFFER_INIT.hex:1-8](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex#L1-L8) —— 每行一个 8 位十六进制数（`00000000`），对应一个 32 位全 0 的存储单元。整个文件由全 0 构成。

这个文件全 0 是有意的：它表示**缓冲上电时所有历史样点被视为「静音（幅值 0）」**。这样滤波器在头几个样点还没真正进来时，读到的都是 0，输出是干净的 0 而不是 `x`。默认配置 `ADDR_WIDTH=8`、`DATA_WIDTH=32`，所以文件提供 \(2^8 = 256\) 个 32 位条目，正好填满整个 RAM。

参数是怎么从上层传下来的？看封装层：

[02_DATA_BUFFER/DATA_BUFFER.v:50-56](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER.v#L50-L56) —— `DATA_BUFFER` 的默认 `RAM_INIT_FILE = "BUFFER_INIT.hex"`，并把这个值原样透传给原语（见上文的 `DATA_BUFFER.v:89-102` 实例化）。

#### 4.2.4 代码实践

**实践目标**：体会 `.hex` 文件 → RAM 内容 的对应关系。

**操作步骤**：

1. 打开 [BUFFER_INIT.hex](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/BUFFER_INIT.hex)，确认它由全 `00000000` 构成。
2. 想象把第 5 行改成 `0000FFFF`（一个非零值），它会被 `$readmemh` 写到 `RAM[4]`（地址从 0 起，第 5 行对应下标 4）。
3. 在 [DATA_BUFFER.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/DATA_BUFFER.bat) 指定的 Questa 工程目录里，确认 `.hex` 与 `.bat`、`run.do` 在同一目录（`$readmemh` 按相对路径找文件）。

**需要观察的现象（待本地验证）**：若真的改了某行为非零值并跑仿真，应在波形里看到该地址被读出时 `RDATA_O` 出现这个非零数；保持全 0 时，复位预填充阶段读到的都是 0。

**预期结果**：建立「`.hex` 第 N 行 ↔ `RAM[N-1]`」的一一对应直觉，并理解全 0 文件 = 静音初始化。

#### 4.2.5 小练习与答案

**Q1**：为什么 `RAM_INIT_FILE` 要设计成参数而不是写死？

**参考答案**：不同应用、不同板卡可能想用不同的初始值（比如预置一段测试信号或固定偏置）；做成参数后，上层 `DATA_BUFFER`、乃至顶层 `FIR_x2` 都能通过实例化时传不同的文件名来定制，原语代码本身不用改。

**Q2**：如果 `RAM_INIT_FILE` 传 `""`（空串），RAM 上电后是什么状态？对滤波器意味着什么？

**参考答案**：跳过 `$readmemh`，RAM 全是 `x`。滤波器在最初若干样点会读到 `x`，输出也会是 `x`（直到有效数据把 `x` 全部「挤」出缓冲）。

**Q3**：`$readmemh` 和 `$readmemb` 有什么区别？

**参考答案**：`$readmemh` 读**十六进制**文件（每行是 hex），`$readmemb` 读**二进制**文件（每行是 0/1）。本模块用 `h`，所以 `BUFFER_INIT.hex` 每行写的是 `00000000` 这样的十六进制。

---

### 4.3 输出寄存器 generate（1 级 vs 2 级读路径）

#### 4.3.1 概念说明

这是本讲最关键、也最巧妙的部分。同步读 RAM 的输出数据要经过**寄存器**才能稳定，而寄存器可以放 1 级，也可以放 2 级。级数越多：

- **优点**：时序裕量更好（数据多打一拍，更容易满足高频下的建立/保持时间），便于布线。
- **代价**：读延迟更大（数据要等更多拍才出来）。

FIR_x2 需要在这两者间权衡，而且不同应用场景可能想要不同的延迟。于是模块用了一个参数 `OUTPUT_REG`（取字符串 `"TRUE"` 或 `"FALSE"`）和一段 `generate`，在编译期就决定走 1 级还是 2 级读路径——**两条路径共用同一份 RAM 和读逻辑，只是最终从哪个寄存器引出 `RDATA_O` 不同**。

这个延迟还直接影响下游：乘法流水线要和「数据何时到达」严格对齐（见 [u5-l1](u5-l1-mult-pipeline.md)），所以选 1 级还是 2 级不是随便定的，要和整条流水线的拍数配合。

#### 4.3.2 核心流程

读逻辑其实始终在跑**两级流水线**：

```
  RADDR_I ──┐
            ▼ posedge (第1拍)
        RDATA_REG_1P ──┐  ← RAM[RADDR_I] 在这里
                       ▼ posedge (第2拍)
                   RDATA_REG_2P ──┐  ← 上一拍的 1P 在这里
                                  ▼
            OUTPUT_REG="FALSE" → 取 RDATA_REG_1P（读延迟 1 拍）
            OUTPUT_REG="TRUE"  → 取 RDATA_REG_2P（读延迟 2 拍）
```

注意：**两个寄存器始终都在更新**（见 `RDATA_REG_1P` 和 `RDATA_REG_2P` 那两行赋值），`OUTPUT_REG` 只是决定 `RDATA_O` 最终从哪一个**接出来**。这是用 `generate` + `assign` 实现的「二选一多路」，在综合时只会保留被选中那条路径相关的寄存器。

读延迟（从 `RADDR_I` 稳定到 `RDATA_O` 有效）：

\[
\text{读延迟} =
\begin{cases}
1 \text{ 拍} & \text{当 } \text{OUTPUT\_REG} = \text{"FALSE"} \\
2 \text{ 拍} & \text{当 } \text{OUTPUT\_REG} = \text{"TRUE"}
\end{cases}
\]

对应的时序表（以读地址 `A` 为例，`A` 在 `t` 拍的上升沿前已稳定）：

| 时刻（posedge） | `RDATA_REG_1P` | `RDATA_REG_2P` | `OUTPUT_REG="FALSE"` 时 `RDATA_O` | `OUTPUT_REG="TRUE"` 时 `RDATA_O` |
| --- | --- | --- | --- | --- |
| `t`（读到 A） | `RAM[A]` | （旧值） | `RAM[A]` ✅ | （旧值） |
| `t+1` | （新地址） | `RAM[A]` | （新） | `RAM[A]` ✅ |

可见 `FALSE` 时数据早一拍出现，`TRUE` 时晚一拍出现。

#### 4.3.3 源码精读

两级读寄存器在同一个 `always` 块里依次推进：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:87-93](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L87-L93) —— 读寄存器：`RDATA_REG_1P <= RAM[RADDR_I]`（第 1 级，从 RAM 读出），`RDATA_REG_2P <= RDATA_REG_1P`（第 2 级，把 1 级推进一格）。两条都用 `<=`，构成标准流水线打拍。

最终的「二选一」由 `generate` 完成：

[02_DATA_BUFFER/SDPRAM_SINGLECLK.v:95-102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L95-L102) —— `generate` 块：`OUTPUT_REG == "TRUE"` 时 `assign RDATA_O = RDATA_REG_2P`（2 拍延迟，走 `gen_reg2p` 分支）；否则 `assign RDATA_O = RDATA_REG_1P`（1 拍延迟，走 `gen_reg1p` 分支）。

> 关于「字符串参数比较」：Verilog 里 `OUTPUT_REG == "TRUE"` 是把参数和字符串字面量比较。参数必须在实例化时**精确**写成 `"TRUE"`（注意是字符串而非标识符），否则会落入 `else` 分支走 1 级路径。这也是为什么 [DATA_BUFFER_TB.v:53-57](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER_TB.v#L53-L57) 里写的是 `.OUTPUT_REG("TRUE")`，引号不能漏。

FIR_x2 顶层固定用 2 级路径（`OUTPUT_REG="TRUE"`），目的是把读延迟做到 2 拍，与下游乘法/累加流水线对齐。这一点 [u3-l1](u3-l1-data-buffer-wrapper.md) 已经点明：「`OUTPUT_REG="TRUE"`（顶层固定取值）时读数据相对读地址滞后 2 个 MCLK，`FALSE` 则滞后 1 拍」。本讲终于给出了它对应的源码出处。

#### 4.3.4 代码实践

**实践目标**：亲手切换 `OUTPUT_REG`，用波形看到 `RDATA_O` 在两条路径间的延迟差异。

**操作步骤**：

1. 复制一份 [DATA_BUFFER_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER_TB.v) 作为实验副本（不要改动仓库原文件）。
2. 把实例化里的 `.OUTPUT_REG("TRUE")` 改成 `.OUTPUT_REG("FALSE")`。
3. 按 [DATA_BUFFER.bat](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/DATA_BUFFER.bat) 的命令（`vlib` → `vlog` → `vsim`）跑仿真，并在 [run.do](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/Questa/run.do) 里补一行，把内部信号加进波形：
   ```
   add wave -position insertpoint sim:/DATA_BUFFER_TB/u_DATA_BUFFER/u_SDPRAM_SINGLECLK/RDATA_REG_1P
   add wave -position insertpoint sim:/DATA_BUFFER_TB/u_DATA_BUFFER/u_SDPRAM_SINGLECLK/RDATA_REG_2P
   ```
4. 给原语补一段注释（练习要求的产出），例如在读寄存器 `always` 块上方写：
   > // 当 OUTPUT_REG=="FALSE" 时，RDATA_O = RDATA_REG_1P，读延迟为 1 拍；
   > // 当 OUTPUT_REG=="TRUE"  时，RDATA_O = RDATA_REG_2P，读延迟为 2 拍（FIR_x2 顶层固定采用此值）。

**需要观察的现象（待本地验证）**：
- `OUTPUT_REG="TRUE"` 时，`RDATA_O` 与 `RDATA_REG_2P` 完全重合，比 `RDATA_REG_1P` 晚 1 个 `MCLK`。
- `OUTPUT_REG="FALSE"` 时，`RDATA_O` 与 `RDATA_REG_1P` 完全重合，读延迟减少到 1 拍。

**预期结果**：你会直观看到 `generate` 如何在两条路径间切换，并理解为何顶层选 2 拍——给下游流水线留出对齐余量。

#### 4.3.5 小练习与答案

**Q1**：为什么两个读寄存器 `RDATA_REG_1P`、`RDATA_REG_2P` 始终都在更新，而不是只更新被选中的那一个？

**参考答案**：读 `always` 块是固定的硬件描述，不知道 `OUTPUT_REG` 选了哪条；它无条件地把 RAM 推进到 1P、把 1P 推进到 2P。到底从哪个寄存器接出 `RDATA_O`，由 `generate` 在编译期决定。综合器会优化掉用不到的寄存器（若选 1 级，2P 可能被裁掉）。

**Q2**：如果把 `OUTPUT_REG` 写成 `"true"`（小写），会发生什么？

**参考答案**：字符串比较 `"true" == "TRUE"` 为假，落入 `else` 分支，`RDATA_O` 走 1 级路径（`RDATA_REG_1P`），读延迟变成 1 拍。这会破坏与下游乘法流水线的拍数对齐，属于隐蔽错误。

**Q3**：从「读延迟」角度，为什么 FIR_x2 顶层要固定选 2 级路径而不是 1 级？

**参考答案**：FIR_x2 的乘法、累加是逐级打拍的流水线，数据缓冲的输出必须按固定的拍数到达乘法器入口才能对齐系数。2 级路径提供 2 拍延迟，正好与系数 ROM 的 2 级读延迟（见 [u4-l1](u4-l1-fir-coef-wrapper.md)）及后续乘法寄存对齐；若改成 1 级，整条流水线的对齐关系都要重新调整。

---

## 5. 综合实践

**任务**：给 `SDPRAM_SINGLECLK` 画一张「参数 → 行为」的影响图，并在仿真里逐一验证。

要求：

1. 在一张图（或表格）里列出三个参数 `DATA_WIDTH`、`ADDR_WIDTH`、`OUTPUT_REG`、`RAM_INIT_FILE` 各自影响什么（容量？深度？延迟？初值？），并标注对应的源码行号（例如 `MEMORY_DEPTH` 在 [L65](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L65)，`generate` 在 [L96-L102](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/SDPRAM_SINGLECLK.v#L96-L102)）。
2. 用 [DATA_BUFFER_TB.v](https://github.com/AUDIY/FIR_x2/blob/a06376b3aa726029e95f94f88236372533e650be/02_DATA_BUFFER/DATA_BUFFER_TB.v) 跑两次仿真：一次 `OUTPUT_REG="TRUE"`，一次 `"FALSE"`，把 `RDATA_REG_1P`、`RDATA_REG_2P`、`RDATA_O` 都加进波形，截图或记录「`RDATA_O` 跟随 1P 还是 2P」。
3. 解释：如果把 `OUTPUT_REG` 从 `"TRUE"` 改成 `"FALSE"`，下游乘法流水线的时序对齐会发生什么问题？应该同步调整哪里？（提示：联系 [u4-l1](u4-l1-fir-coef-wrapper.md) 系数 ROM 的读延迟与 [u5-l1](u5-l1-mult-pipeline.md) 的乘法寄存。）

**验收标准**：
- 能准确说出四个参数各自的作用域。
- 能用波形证明 `OUTPUT_REG` 切换确实改变了 `RDATA_O` 的来源寄存器。
- 能讲清「读延迟变化会破坏流水线对齐」这一后果。

> 本实践涉及改 TB 副本与重跑仿真，具体波形数值「待本地验证」；但源码层面的结论（参数影响、generate 分支、延迟公式）是确定的。

## 6. 本讲小结

- `SDPRAM_SINGLECLK` 是 FIR_x2 最底层的存储原语，**只管按地址存取**，写口与读口是两个独立 `always` 块、共用 `MCLK_I`，构成单时钟简单双口 RAM。
- 存储深度由 `MEMORY_DEPTH = 2**ADDR_WIDTH` 派生；默认 `ADDR_WIDTH=8` 对应 256 个 32 位单元。
- 上电初值由 `$readmemh(RAM_INIT_FILE, RAM)` 从 `.hex` 文件加载；`BUFFER_INIT.hex` 全 0 表示「静音初始化」，配合控制器复位预填充让缓冲一上电就装满有效数据。
- 读路径始终维护两级寄存器 `RDATA_REG_1P`/`RDATA_REG_2P`；`generate` 依据字符串参数 `OUTPUT_REG` 决定 `RDATA_O` 从哪一级接出，从而读延迟为 1 拍（`"FALSE"`）或 2 拍（`"TRUE"`）。
- FIR_x2 顶层固定 `OUTPUT_REG="TRUE"`（2 拍延迟），目的是与系数 ROM 及下游乘法流水线对齐——这是「时钟随数据打拍」理念在存储原语层的体现。
- 这一层被刻意隔离出来，是为了跨厂商替换 Block RAM/ROM 时只动这一个文件（呼应 u1-l2 / u6-l4 的移植主题）。

## 7. 下一步学习建议

- 现在你已经看完了输入通路三层（封装 `DATA_BUFFER` → 控制器 `DPRAM_CONT` → 原语 `SDPRAM`）。建议回头重读 [u3-l2](u3-l2-dpram-ring-buffer-controller.md)，确认控制器产生的 `WEN/WADDR/REN/RADDR` 是如何精确驱动本讲的写口与读口的，特别注意 `REN` 在读写撞址时的保护作用。
- 接下来进入 [第 4 单元](u4-l1-fir-coef-wrapper.md)：系数通路。那里的 `SPROM`（单口 ROM 原语）与本讲的 `SDPRAM` 结构非常像（都用 `$readmemh`、都有 1/2 级输出寄存器 generate），学完本讲后读 `SPROM` 会非常轻松，可以重点对比「只读 ROM」与「可读写 RAM」在端口上的差异。
- 如果对厂商移植感兴趣，可以提前跳到 [u6-l4](u6-l4-fpga-porting-examples.md)，看本讲的 `SDPRAM` 在 Vivado/Quartus 下会被替换成什么样的官方 BRAM IP。
