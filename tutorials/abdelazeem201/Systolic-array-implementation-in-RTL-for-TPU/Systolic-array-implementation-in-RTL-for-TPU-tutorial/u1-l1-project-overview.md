# 项目总览：TPU、脉动阵列与本项目定位

## 1. 本讲目标

本讲是整本学习手册的第一篇，面向「完全没接触过这个项目」的读者。读完后你应当能够：

- 说清楚什么是 **TPU（Tensor Processing Unit，张量处理单元）**、什么是**脉动阵列（systolic array）**，以及它们为什么能加速神经网络里的矩阵乘法。
- 用一句话讲明白本项目到底在造什么：一个用 Verilog 实现的、8×8（可参数化到 32×32）定点矩阵乘法加速器。
- 画出本项目的核心数据流方向：**weight（权重）自上而下、data（数据）自左而右**，并解释每个 cell（处理单元）内部三类寄存器各自的作用。
- 认识项目的三大组成部分：核心 RTL、ASIC 后端流程、扩展架构。

本讲**不要求你立刻看懂每一行 Verilog**，重点是建立「这个项目在做什么、为什么这么做」的全局画面。具体的代码精读会在后续讲义逐层展开。

## 2. 前置知识

阅读本讲前，你最好对以下概念有最基本的印象。如果没有也没关系，本讲会用通俗语言补上。

- **矩阵乘法**：两个二维数表按「行乘列再求和」的规则相乘。神经网络里最常见、计算量也最大的运算就是矩阵乘法（全连接层、卷积层都可以转化成矩阵乘）。
- **有符号定点数（signed fixed-point）**：用固定位数的小数表示实数。比如「8 位有符号、4 位整数 + 4 位小数」表示一个范围有限、小数精度固定的小数。本讲后面会详细解释。
- **Verilog / RTL**：一种硬件描述语言；用 RTL（Register Transfer Level，寄存器传输级）代码描述的电路可以在仿真器里跑，也可以被综合成真实的芯片或烧到 FPGA 里。
- **时钟周期（clock cycle）**：数字电路按节拍工作，每个上升沿所有寄存器同步更新一次。本项目的性能目标就是「每个时钟周期 3 纳秒」。

> 名词小贴士：下文反复出现的 **cell** 指 systolic array 里的一个最小处理单元；**MAC** 指 Multiply-Accumulate（乘加）操作，是这些 cell 干的核心活。

## 3. 本讲源码地图

本讲主要阅读两个说明性文件，并辅以少量代码来印证说明。后续讲义会深入这些代码。

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md) | 项目主说明：介绍 8×8 脉动阵列设计、每个 cell 的三类寄存器、定点格式、以及综合/PnR/FPGA 结果。 |
| [rtl/systolic array/Readme.md](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic%20array/Readme.md) | RTL 子目录说明：列出 5 个核心模块和 4 个关键参数。 |
| [rtl/systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v) | 脉动阵列本体代码。本讲只用它来「印证」README 里描述的数据流和三类寄存器确实在代码里存在。 |
| [rtl/tpu_top.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/tpu_top.v) | 顶层模块，把 5 个子模块连成一个完整 TPU。本讲只看它的端口规模。 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 项目说明**：TPU 是什么、脉动阵列是什么、本项目在造什么、由哪几部分组成。
- **4.2 8×8 脉动阵列架构图**：阵列里数据怎么流动、每个 cell 里有什么、定点数怎么表示。

### 4.1 项目说明

#### 4.1.1 概念说明

**什么是 TPU？**
TPU 是 Google 提出的专用加速芯片，专门为神经网络里的大规模矩阵乘法设计。通用 CPU 为了「什么都能干」牺牲了效率；GPU 并行度高但仍然偏通用；而 TPU 走的是「**把一件事做到极致**」的路线——把矩阵乘法这种单一但极高频的运算，直接用硬件电路铺开来做。

**什么是脉动阵列（systolic array）？**
「Systolic」原意是心脏的收缩。脉动阵列的灵感正是来自心脏：数据像血液一样，按固定节拍（时钟）在一张由大量相同处理单元（cell）组成的网格里**流动**，每经过一个 cell 就做一次乘加，最后从阵列边缘流出结果。它的核心特点是：

1. **数据复用**：一个数据进入阵列后不会被立刻丢掉，而是沿着行或列一路传给后续 cell，每个 cell 都能用它算一次。
2. **本地化通信**：每个 cell 只和它上下左右的邻居通信，不需要全局的长连线，非常适合芯片实现。
3. **规整、好流水**：所有 cell 做同样的动作（乘、加、把数据传给邻居），结构高度规整，天然适合流水线化，能跑到很高频率。

**本项目在造什么？**
本项目用 Verilog 实现了一个 TPU 的核心计算部件——一个 **8×8 的脉动阵列**（代码里用参数 `ARRAY_SIZE` 控制，可放大到 32×32）。它完成的任务是矩阵乘法：矩阵 A（项目里叫 **weight 矩阵**）乘矩阵 B（项目里叫 **data 矩阵**），两个都是 8×8。README 里这样描述它：

> 项目设计了一个 8×8 脉动阵列；当需要对两个矩阵做乘法时，矩阵 A（weight）与矩阵 B（data）会被先重排成特定喂入顺序，再送进各自的队列，最终每个 cell 根据收到的 weight 和 data 做乘加，并在下一个周期把 weight 和 data 传给下一个 cell。

参见 [README.md:1-2](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L1-L2)（这段同时给出了关键的数据流方向，我们下一节细讲）。

#### 4.1.2 核心流程

从「项目层面」看，本仓库其实包含三块互相独立但互补的工作：

```
                ┌─────────────────────────────────────────────┐
   核心RTL      │  rtl/ : tpu_top + systolic + controller      │
   (算得对)     │  + quantize + addr_sel + write_out           │
                └─────────────────────────────────────────────┘
                ┌─────────────────────────────────────────────┐
   后端流程     │  syn/ : Design Compiler 综合                 │
   (造得出)     │  pnr/ : ICC2 布局布线                        │
                └─────────────────────────────────────────────┘
                ┌─────────────────────────────────────────────┐
   扩展架构     │  rtl/RTL_modified/ : 更完整的 TPU SoC         │
   (用得起)     │  + Avalon 总线 + master_control + FIFO ...   │
                └─────────────────────────────────────────────┘
```

- **核心 RTL（`rtl/`）**：解决「**算得对**」。这是我们这本手册前几个单元的主角。它由 5 个模块拼成，顶层叫 `tpu_top`。RTL 子目录的说明文件把它描述为一个「32×32 脉动阵列」，并点明了模块分工，见 [rtl/systolic array/Readme.md:1-3](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic%20array/Readme.md#L1-L3)。
- **后端流程（`syn/`、`pnr/`）**：解决「**造得出**」。RTL 代码只是描述电路，要变成真实芯片还需要「综合（把 RTL 翻译成工艺库里的标准单元）」和「布局布线（把单元摆到芯片上并连好线）」。README 报告了综合后的周期与面积，见 [README.md:26-29](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L26-L29)（3ns 周期、面积 116493.18）。
- **扩展架构（`rtl/RTL_modified/`）**：解决「**用得起**」。在一个完整系统里，TPU 不能孤立存在，它要挂在总线上、由主机软件指挥、还要能跑真实网络。这部分加上了 Avalon 总线接口、主控状态机、权重 FIFO、累加表、ReLU 激活等，面向的是 SoC 级集成。

> 注意一个「文档 vs 代码」的小出入：`rtl/systolic array/Readme.md` 把阵列说成「32×32」（见其参数段），但主 README 和实际仿真、综合都围绕 **8×8** 展开，`rtl/systolic.v` 里参数默认值也是 `ARRAY_SIZE = 8`。**本手册以实际代码和主 README 为准，按 8×8 讲解，并把 32×32 视作参数化后的可选目标。** 这是一个初学者容易困惑的点，先记住即可。

#### 4.1.3 源码精读

先看主 README 对项目的整体定位（矩阵 A 是 weight、矩阵 B 是 data，二者要做矩阵乘）：

[README.md:1-2](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L1-L2) —— 说明这是一个 8×8 脉动阵列，做 weight 矩阵乘 data 矩阵；并明确了数据流动方向。

再看 RTL 子目录说明列出的 5 个核心模块与职责分工：

[rtl/systolic array/Readme.md:1-3](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic%20array/Readme.md#L1-L3) —— 用一句话概括每个模块：`systolic`（阵列计算）、`systolic_controll`（控制状态机）、`quantize`（量化）、`addr_sel`（地址选择）、`tpu_top`（顶层集成）。

最后看代码层面的参数定义，确认阵列规模和位宽都是「参数化」的：

[rtl/systolic.v:4-8](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L4-L8) —— 模块 `systolic` 的三个参数：`ARRAY_SIZE = 8`（阵列边长）、`SRAM_DATA_WIDTH = 32`（SRAM 数据总线宽）、`DATA_WIDTH = 8`（单个输入元素位宽）。

#### 4.1.4 代码实践

**实践目标**：把「项目由三部分组成」这件事从抽象概念变成你自己的笔记。

**操作步骤**：

1. 打开仓库根目录，用 `git ls-files` 或直接浏览，列出顶层目录（`rtl/`、`Pre-Synthesis_Simulation/`、`syn/`、`pnr/`、`rtl/RTL_modified/`）。
2. 把每个顶层目录归类到「核心 RTL / 后端流程 / 扩展架构」三块之一（`Pre-Synthesis_Simulation/` 属于核心 RTL 的仿真验证部分）。
3. 对「核心 RTL」这一块，到 `rtl/systolic array/Readme.md` 里抄下 5 个模块的名字和各自的一句话职责。

**需要观察的现象**：你会发现 `rtl/` 下同时存在一份「带空格目录名」的 `systolic array/`（含一组同名 `.v` 和一个 Readme）和根级的一组 `.v`（如 `rtl/systolic.v`）。两份内容几乎一致，根级那组是综合/仿真实际使用的。

**预期结果**：得到一张「目录 → 归属 → 关键文件」的三列表格。这就是后续整本手册的导航地图。

#### 4.1.5 小练习与答案

**练习 1**：本项目要加速的核心运算是哪一种？为什么这种运算值得用专用硬件？
> **答案**：矩阵乘法。因为它是神经网络里计算量最大、调用最频繁的运算，且结构高度规整、易于并行展开，用专用脉动阵列硬件比通用 CPU/GPU 更省功耗、吞吐更高。

**练习 2**：`rtl/systolic array/Readme.md` 说阵列是 32×32，但主 README 和仿真都按 8×8 来讲。这两者矛盾吗？该信哪个？
> **答案**：不矛盾——`ARRAY_SIZE` 是参数，理论上可设成 32。但**实际代码默认值、主 README 的设计说明、以及综合/仿真结果都基于 8×8**。学习时应以 8×8 为主线，把 32×32 当作「参数可放大」的能力说明。

---

### 4.2 8×8 脉动阵列架构图

#### 4.2.1 概念说明

这一节我们钻进阵列内部，回答三个问题：**数据怎么流、每个 cell 里有什么、数怎么表示。**

**数据流方向**（这是本节最重要的一句话）：

- **weight（矩阵 A）自上而下流动**：weight 从阵列的**顶行**喂入，每个时钟周期向下传一行。
- **data（矩阵 B）自左而右流动**：data 从阵列的**左列**喂入，每个时钟周期向右传一列。

每个 cell 在每个周期做三件事：① 把收到的 weight 和 data **相乘并累加**进自己的 ALU；② 把 weight **向下**传给正下方的 cell；③ 把 data **向右**传给右侧的 cell。这就是「输出固定（output-stationary）」式脉动阵列：**结果留在 cell 里不动，让两个操作数流过来相会**。

**每个 cell 里的三类寄存器**（主 README 明确写了这点）：

1. **ALU 累加寄存器**：保存该 cell 的累加结果（部分和），每个周期更新一次。
2. **weight 寄存器**：保存当前周期收到的 weight，并在下个周期把它向下传。
3. **data 寄存器**：保存当前周期收到的 data，并在下个周期把它向右传。

参见 [README.md:13-15](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L13-L15) —— 这一段同时给出了三类寄存器、定点格式和三批测试矩阵的设计意图，是理解整个项目的「总纲」。

**定点数怎么表示**（也来自上面那段 README）：

- 输入元素是 **8 位有符号数，4 位整数 + 4 位小数**。
- 最终矩阵乘结果是 **16 位有符号数，8 位整数 + 8 位小数**。

#### 4.2.2 核心流程

**阵列拓扑与数据流示意**（8×8，箭头表示每个周期的传递方向）：

```
            weight 流向（自上而下 ↓，每列独立）
              ↓     ↓     ↓     ↓   ...   ↓
         ┌──────┬──────┬──────┬──────┬───┬──────┐
  data → │cell  │cell  │cell  │cell  │...│cell  │  → data 继续向右流出
  流向   │(0,0) │(0,1) │(0,2) │(0,3) │   │(0,7) │
（自左   ├──────┼──────┼──────┼──────┼───┼──────┤
 而右 →）│cell  │cell  │cell  │cell  │...│cell  │
         │(1,0) │(1,1) │(1,2) │(1,3) │   │(1,7) │
         ├──────┼──────┼──────┼──────┼───┼──────┤
         │ ...           ...            │   │ ...  │
         ├──────┼──────┼──────┼──────┼───┼──────┤
         │cell  │cell  │cell  │cell  │...│cell  │
         │(7,0) │(7,1) │(7,2) │(7,3) │   │(7,7) │
         └──────┴──────┴──────┴──────┴───┴──────┘
              ↓     ↓     ↓     ↓   ...   ↓     weight 继续向下流出
```

每个 cell `(i,j)` 内部：

```
            weight_in (来自上方 cell(i-1,j) 或顶行 SRAM)
                 │
                 ▼
          ┌──────────────┐
 data_in →│  weight_reg  │→ weight_out（下传）
 (左侧)   │  data_reg    │→ data_out（右传）
          │  ALU(累加器) │→ 结果最终留在此 cell
          └──────────────┘
                 ▲
            data_in (来自左方 cell(i,j-1) 或左列 SRAM)
```

**单 cell 的伪代码**（帮助理解节奏，非项目原代码）：

```
// 示例代码：单个 cell 每个时钟上升沿的动作（伪代码，仅示意）
always @(posedge clk) begin
    alu      <= alu + weight_reg * data_reg;  // 乘加累加
    weight_reg <= weight_in;                   // 接住上方来的 weight
    data_reg   <= data_in;                     // 接住左方来的 data
end
// weight_out = weight_reg;  data_out = data_reg;  // 传给下游
```

> 说明：项目真实代码用一整张二维数组 `weight_queue[i][j]`、`data_queue[i][j]` 统一描述全部 64 个 cell 的寄存器，并用 `for` 循环完成「向下/向右移位」，而不是逐个 cell 写。下一段会看到。

**为什么 8×8 乘 8×8 最终结果是 16 位？** 简单算一下位宽：

两个 \(n\) 位有符号数相乘，积最多 \(2n\) 位。本项目 \(n=8\)，所以单次乘积为 16 位。乘 8 次累加后，为了不溢出还需要额外的保护位（guard bits），代码里用 21 位中间结果承载，最后再量化回 16 位输出：

\[ \text{输入位宽} = 8,\quad \text{单次乘积} = 8+8 = 16,\quad \text{累加中间结果} = 8+8+5 = 21,\quad \text{输出} = 16 \]

其中 21 这个数字就来自代码里的 `OUTCOME_WIDTH = DATA_WIDTH+DATA_WIDTH+5`（[rtl/systolic.v:28](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L28)），那 5 位就是累加保护位；16 位输出对应顶层参数 `OUTPUT_DATA_WIDTH = 16`。

#### 4.2.3 源码精读

主 README 对 cell 内部结构与定点格式的权威描述（本节最核心的一段引用）：

[README.md:13-15](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L13-L15) —— 明确：每个 cell 有 3 个寄存器（1 个 ALU 累加、1 个 weight 寄存器、1 个 data 寄存器）；共 8×8=64 个 cell；输入 8 位有符号（4 整 4 小）；输出 16 位有符号（8 整 8 小）；testbench 用三批矩阵分别模拟「进入 / 满载 / 离开」三种工况。

下面用真实代码印证 README 的描述。先看三类寄存器在代码里对应的二维数组声明：

[rtl/systolic.v:30-33](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L30-L33) —— 三个二维数组正好对应三类寄存器：`matrix_mul_2D`（ALU 累加结果，21 位）、`weight_queue`（weight 寄存器，8 位）、`data_queue`（data 寄存器，8 位）。

再看「weight 向下移位、data 向右移位」的时序逻辑，它就是上面示意图里箭头的代码实现：

[rtl/systolic.v:54-74](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L54-L74) —— `weight_queue[i][j] <= weight_queue[i-1][j]`（weight 向下一行）、`data_queue[i][j] <= data_queue[i][j-1]`（data 向右一列），顶行与左列则从 SRAM 读数据 `sram_rdata_w0/w1`、`sram_rdata_d0/d1` 切成 8 位喂入。

最后看「乘加」本身——每个 cell 把自己当前的 weight 和 data 相乘：

[rtl/systolic.v:97-99](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/rtl/systolic.v#L97-L99) —— `mul_result = weight_queue[i][j] * data_queue[i][j]`，并做符号扩展 `{ {5{mul_result[15]}}, mul_result }`（把 16 位有符号积扩到 21 位后再累加，这正是上节说的 5 位保护位）。

> 这一小节只要求你「认得出」代码里哪些地方对应 README 说的概念。时序细节（什么时候开始累加、结果怎么收集）属于第二、三单元的内容，这里不必深究。

#### 4.2.4 代码实践

**实践目标**：亲手把数据流方向和 cell 内部结构画出来，建立空间直觉。

**操作步骤**：

1. 画一个 8×8 的方格（代表 64 个 cell）。
2. 在方格**上方**画 8 个向下箭头，标注「weight（矩阵 A）自上而下」；在方格**左侧**画 8 个向右箭头，标注「data（矩阵 B）自左而右」。
3. 任选一个方格（比如第 3 行第 4 列的 cell），在它内部画出三个小框，分别写：`ALU 累加`、`weight_reg`、`data_reg`，并各加一句中文说明：
   - `ALU 累加`：保存本 cell 的部分和，每周期做 `+= weight*data`。
   - `weight_reg`：暂存当前 weight，下周期向下传。
   - `data_reg`：暂存当前 data，下周期向右传。
4. 在图旁标注位宽：输入 8 位（4 整 4 小）、单次乘积 16 位、累加中间结果 21 位、输出 16 位（8 整 8 小）。

**需要观察的现象**：画完后你应该能直观看到——weight 只在「列」方向流动、data 只在「行」方向流动，二者在每个 cell 交汇一次，结果不流动（output-stationary）。

**预期结果**：一张包含数据流方向、三类寄存器、位宽标注的 8×8 阵列示意图。这就是后续所有讲义的「底图」，建议保存。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `ARRAY_SIZE` 从 8 改成 16，cell 总数会变成多少？每个 cell 内部寄存器数量会变吗？
> **答案**：cell 总数 \(= 16\times16 = 256\)（原来是 64）。每个 cell 内部仍是 3 个寄存器（ALU/weight/data），结构不变，变的是阵列规模。

**练习 2**：两个 8 位有符号数相乘，为什么结果要扩到 21 位再累加，而不是直接用 16 位累加？
> **答案**：单次乘积最多 16 位，但 8×8 矩阵乘要对 8 个积求和，连续累加会向高位增长。若只用 16 位累加会很快溢出。多出的 5 位保护位（21 = 16 + 5）给累加留出余量，最后再由 `quantize` 饱和截断回 16 位输出。

**练习 3**：为什么说本阵列是「output-stationary（输出固定）」？哪类东西是「不流动」的？
> **答案**：因为最终结果（累加和）固定保存在每个 cell 的 ALU 寄存器里，不随数据流动；流动的是两个输入操作数 weight（向下）和 data（向右）。

## 5. 综合实践

把本讲两个模块串起来，完成一份「**一页项目速览**」。

任务：

1. **定位**：用一句话写出本项目是什么（提示：8×8、参数化、定点、矩阵乘、脉动阵列、TPU）。
2. **三块组成**：画一张三栏图，分别写「核心 RTL / 后端流程 / 扩展架构」，并各列 1～2 个对应目录或文件名（如 `rtl/tpu_top.v`、`syn/scripts/`、`rtl/RTL_modified/top.v`）。
3. **底图**：附上 4.2.4 里你画的 8×8 数据流示意图（含三类寄存器与位宽）。
4. **一句话数据流**：在图下写「weight 自上而下，data 自左而右，每个 cell 做 MAC，结果就地累加」。
5. **自检**：对照 [README.md:13-15](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L13-L15) 检查你的位宽和三类寄存器描述是否和原文一致。

完成后，这一页就是你阅读后续所有讲义时的「随身地图」。

## 6. 本讲小结

- TPU 是为神经网络里的大规模矩阵乘法设计的专用加速器；脉动阵列是它最核心的计算结构，靠「数据在 cell 网格里按节拍流动 + 本地化通信」获得高吞吐与高频率。
- 本项目用 Verilog 实现了一个 **8×8（参数 `ARRAY_SIZE` 可放大到 32×32）定点矩阵乘加速器**：矩阵 A 是 weight、矩阵 B 是 data。
- 数据流方向是本项目的「宪法」：**weight 自上而下、data 自左而右**，每个 cell 做一次乘加并把结果就地累加（output-stationary）。
- 每个 cell 有 **三类寄存器**：ALU 累加、weight 寄存器、data 寄存器，共 64 个 cell。
- 定点格式：输入 8 位（4 整 4 小）、单次乘积 16 位、累加中间结果 21 位（含 5 位保护位）、输出 16 位（8 整 8 小）。
- 项目由三块组成：**核心 RTL**（算得对）、**后端流程 syn/pnr**（造得出）、**扩展架构 RTL_modified**（用得起）。

## 7. 下一步学习建议

接下来建议按顺序阅读：

- **u1-l2 仓库目录结构与源码组织**：把本讲提到的目录逐一打开，建立完整的文件导航。
- **u1-l3 顶层模块 tpu_top 与系统级数据流**：进入 `rtl/tpu_top.v`，看 5 个子模块如何连成一个完整 TPU。
- 之后第二、三单元将分别深入「脉动阵列数据通路」与「控制器/地址/写回/量化」，把本讲画在图里的箭头，一条条对应到真实代码。

如果想在动代码前再巩固直觉，建议重读一遍主 README 的 [README.md:1-2](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L1-L2) 与 [README.md:13-15](https://github.com/abdelazeem201-Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/README.md#L13-L15)，确保「数据流方向 + 三类寄存器 + 定点位宽」这三件事你已经能脱稿讲清楚。
