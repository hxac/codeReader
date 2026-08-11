# 动态规划法寻找最佳缝合线 DynamicSeam

## 1. 本讲目标

本讲进入专家层，精读 [动态规划法寻找最佳缝合线/DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v)——它是整套拼接流水线在 FPGA 上的最后一块算法拼图：**缝合线查找（seam finding）**。

在 [u2-l1](u2-l1-opencv-stitching-pipeline.md) 里我们看到 OpenCV 用 `GraphCutSeamFinder` 找缝合线；本讲对应的是作者把它移植到硬件的尝试。这个模块把 [u2-l4](u2-l4-cylindrical-projection-hardware.md) 的投影结果（两幅投影图的重叠区）和 [u3-l1](u3-l1-ddr3-mem-burst.md) 的 DDR3 读突发结合在一起：先从 DDR3 把两幅图重叠区的「行」读进来，再用动态规划在重叠区里找一条「代价最小的切分线」，决定每个像素到底取左图还是右图。

读完本讲，你应该能够：

1. 说清楚**缝合线（seam）**要解决什么问题：为什么不能简单地把两幅图「一刀切」拼接，而要找一条「随行列弯曲、走差异最小处」的切分线。
2. 读懂 `DynamicSeam` 的三状态机 `IDLE → READ → SeamFind`：先从 DDR3 读重叠区数据填进 `row1/row2`，再扫描 `cost/coordinate` 两个数组做动态规划。
3. 理解 `cost/coordinate` 数组对应的「最小累计代价路径」更新逻辑，以及它和标准 DP 缝合线的关系。
4. **识别本模块中至少三处会导致综合失败的写法**：`parameter` 当变量用、`localparam` 写进 `always` 块、模块例化（`mig_7series_0`）写进 `always` 块等，并解释为什么文件头注释会写「外部代码不能综合」。
5. 用一段 RTL 思路重新描述 `cost` 更新的正确时序设计。

> ⚠️ **本讲会反复强调一件事**：`DynamicSeam.v` 是一份「**算法思路草稿**」，**不是可直接综合的代码**。作者在文件头注释里就声明了这一点（原文因编码乱码无法逐字辨认，但字面与上下文指向「外部代码不能综合」）。本讲一半篇幅在讲「它想做什么」，另一半在讲「它为什么不能直接落地」——这种「读半成品 RTL」的能力，正是专家层要训练的。

## 2. 前置知识

本讲承接 [u2-l4 圆柱面投影硬件实现](u2-l4-cylindrical-projection-hardware.md)（你要记得投影后图像落在 `dst_tl/dst_br` 矩形里）和 [u3-l1 mem_burst 突发控制器](u3-l1-ddr3-mem-burst.md)（你要记得 MIG 的 `app_*` 应用接口长什么样、`mem_burst` 是怎么「在模块级」包装它的）。下面补三个本讲要用到的新概念。

- **缝合线（seam / 最佳缝合线）**：两幅投影图在拼接时有一块**重叠区**（overlap）。如果在重叠区里画一条竖直的直线、左半取左图、右半取右图，那么只要两图在这条线上有一丁点配准误差，接缝处就会出现明显的「裂缝/重影」。缝合线的做法是：不画直线，而画一条**可以逐行左右挪动的折线**，让它在「两图差异最小」的列上穿过——这样左右图在切分处几乎一样，接缝就看不见了。找这条折线，就是「缝合线查找」。
- **动态规划（Dynamic Programming, DP）找缝合线**：这是缝合线查找的经典做法之一（OpenCV 的 `DpSeamFinder` 即如此）。把「找全程最优折线」拆成「逐列（或逐行）累加代价」：每个位置的**累计代价 = 本位置代价 + 上一行三个相邻位置里最小的那个累计代价**；同时记录「最小代价是从哪个邻居来的」作为回溯指针。扫完整幅后，从最后一行代价最小的位置往回走，就还原出整条折线。
- **综合（synthesis）**：把 Verilog 代码翻译成 FPGA 网表（真实逻辑门/触发器）的过程。**不是所有能仿真的 Verilog 都能综合**——有些写法只在仿真器（行为级）里跑得动，综合器会直接报错拒绝。本模块集中了多种「能仿真思路、不能综合」的写法。

一个关键直觉：**DP 是「逐点累加 + 取 min」的串行递推**，它天然有数据依赖（第 *i* 行要用第 *i-1* 行的结果）。这和 FPGA 喜欢的「无依赖、全并行」相抵触，所以把 DP 搬上硬件，难点不在算法本身，而在**怎么把这种行间依赖排成时序干净的流水线**——而本模块恰恰没有把这件事做对。

> 术语速查：缝合线（seam）、重叠区（overlap）、累计代价（cumulative cost）、回溯指针（backpointer）、`cost/coordinate` 数组、综合（synthesis）、模块例化（instantiation）、`parameter`/`localparam`/`reg` 的区别。

## 3. 本讲源码地图

本讲只读一个文件，但它的不同区段承担完全不同的职责，按下表分区阅读会清晰很多。

| 文件区段 | 行号 | 作用 |
|---|---|---|
| DynamicSeam.v | L21-L23 | 文件头注释：**声明本代码不能综合**（编码乱码，据上下文为「外部代码不能综合」） |
| DynamicSeam.v | L24-L34 | 模块端口、`WIDTH/HEIGHT/OVERLAPWIDTH/OVERLAPHEIGHT` 参数、`row1/row2/cost/coordinate` 四个存储数组 |
| DynamicSeam.v | L38-L47 | `clk_wiz_0` 时钟 IP 例化（模块级，姿态正确，但有端口拼写错误） |
| DynamicSeam.v | L52-L59 | 状态机寄存器、`IDLE/READ/SeamFind` 状态码、**被误声明为 `parameter` 的 `row/col/read_col`** |
| DynamicSeam.v | L62-L89 | MIG 相关 `localparam`（端口宽度推导）与 `app_*` 线网声明 |
| DynamicSeam.v | L91-L118 | 状态寄存器 + 次态组合逻辑（三状态转移） |
| DynamicSeam.v | L121-L181 | 五个计数器风格的 `always` 块：`cnt / row / col / read_col` 的自增（**全部在驱动 `parameter`**） |
| DynamicSeam.v | L184-L265 | 「主控」`always` 块：**内嵌 `mig_7series_0` 例化 + DP 主体**（综合重灾区） |
| DDR3控制/mem_burst.v | L3-L40, L78-L233 | 对照参考：`mem_burst` 如何「在模块级」正确地包装 MIG 的 `app_*` 接口 |

记忆口诀：**L24-L34 是数据结构（四个数组），L52-L59 是状态机骨架，L184-L265 是「又例化 MIG、又写 DP」的混合主块——本讲所有麻烦都集中在这最后一段。**

## 4. 核心概念与源码讲解

### 4.1 DynamicSeam 模块定位、端口与四个存储数组

#### 4.1.1 概念说明

`DynamicSeam` 想做的事情可以一句话概括：**给定两幅投影图在重叠区的一行像素，找出这一行里「左图切到右图」的最佳切换列，并把逐行的选择记录下来，连成一条缝合线。**

它的输入很简洁：复位 `rst_n`、差分时钟 `clk_p/clk_n`（200MHz，给 `clk_wiz`）、一个启动信号 `read_request`。它不直接暴露「缝合线结果」端口，而是把整条切分线编码进 `coordinate` 数组（每个列位置存一个回溯指针）。注意它**自己例化了 MIG**（DDR3 控制器 IP）直接去读 DDR3——这和 [u3-l1](u3-l1-ddr3-mem-burst.md) 里 `mem_burst`「只做用户侧包装、MIG 留给顶层例化」的干净做法完全不同（见 4.2）。

#### 4.1.2 核心流程

```
read_request=1
   │
   ▼
READ 阶段：逐拍从 DDR3 读重叠区像素
   │  前 OVERLAPWIDTH+1 拍 → row1[0..OVERLAPWIDTH]   （第一幅图，当前行的各列）
   │  后 OVERLAPWIDTH+1 拍 → row2[0..OVERLAPWIDTH]   （第二幅图，当前行的各列）
   ▼
SeamFind 阶段：扫描 col=0..OVERLAPWIDTH
   │  对每个 col：比较 row2 在 index 及其左右邻居的值
   │  更新 coordinate[col]（回溯指针）与 cost[col]（累计代价）
   ▼
（cnt==OVERLAPWIDTH）→ 回 IDLE，等待下一次 read_request
```

注意：`row1/row2` 虽然名字带「row」，但它们是一维数组、下标范围是 `0..OVERLAPWIDTH`（列方向）。也就是说，这里一个「row」其实是一整条**扫描线在重叠区各列上的像素缓冲**，不是「一行里的某一个像素」。这个命名在 4.3 会和另一个 `row` 计数器撞车。

#### 4.1.3 源码精读

[DynamicSeam.v:L24-L34](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L24-L34) 是模块头与存储结构：

```verilog
module DynamicSeam
	#(parameter[31:0] WIDTH = 1100, parameter[31:0] HEIGHT = 1100,
	  parameter[31:0] OVERLAPWIDTH = 300, parameter[31:0] OVERLAPHEIGHT = 1100)
	(input rst_n, input clk_p, input clk_n, input read_request);

reg [31:0] row1 [OVERLAPWIDTH : 0];     // 第一幅图：当前行各列像素
reg [31:0] row2 [OVERLAPWIDTH : 0];     // 第二幅图：当前行各列像素
reg [31:0] cost [OVERLAPWIDTH : 0];     // 每列的累计代价
reg [31:0] coordinate [OVERLAPWIDTH : 0]; // 每列的回溯指针（缝合线的列轨迹）
reg [31:0] cnt = 0;
```

四个数组都用 `OVERLAPWIDTH`（默认 300）做下标上界，故每个数组有 301 个表项。从算法角度，它们的角色是：

| 数组 | 维度方向 | 算法角色 |
|---|---|---|
| `row1[col]` | 列 | 第一幅图在当前行、第 `col` 列的像素值（被减数） |
| `row2[col]` | 列 | 第二幅图在当前行、第 `col` 列的像素值（减数/比较基准） |
| `cost[col]` | 列 | 到第 `col` 列为止的**累计代价**（DP 的状态量） |
| `coordinate[col]` | 列 | 「最佳路径走到本列时，上一行来自哪一列」的**回溯指针** |

理想情况下，单点代价应当反映「两图在此列的差异」，例如 \(e(col) = |row1[col] - row2[col]|\)。两图差异越小的列，越适合做切分点。我们会在 4.4 看到，代码并没有真正算这个差，而是直接拿 `row2` 当代价——这是逻辑缺陷之一。

最后请先记住文件头那句注释 [DynamicSeam.v:L21-L23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L21-L23)，它是一句乱码（GBK 源码被当 UTF-8 读取的典型「锟斤拷」式 mojibake），但结合本模块随处可见的综合障碍，其字面含义指向 **「外部代码不能综合」**——作者本人已经标注这份代码不能直接进综合流程。本讲后面会逐条兑现这句话。

#### 4.1.4 代码实践

**实践目标**：把「缝合线为何必要」从直觉落实到本模块的数据结构。

**操作步骤**：

1. 阅读 [DynamicSeam.v:L30-L33](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L30-L33)，确认四个数组都按列（`OVERLAPWIDTH`）展开。
2. 假设重叠区宽 `OVERLAPWIDTH=4`（5 个列位置 0..4），在纸上画两幅图在这一行的像素：`row1 = {10, 12, 200, 205, 11}`、`row2 = {11, 13, 198, 204, 12}`。
3. 观察哪几个列两图最接近——这些就是缝合线**应该**优先经过的列。

**需要观察的现象**：列 0、1、4 两图几乎相等（差 1），列 2、3 差异也小（差 2~3）；中间没有「差异巨大」的列，说明这一行接缝不明显。若把 `row1[2]` 改成 50，则列 2 处两图差异骤增，缝合线应**绕开**列 2。

**预期结果**：理解 `cost[col]` 越小代表「累计到此列的差异越小」，`coordinate` 记录的是「最小代价路径怎么走」。（本实践为纸面推演，无需运行。）

#### 4.1.5 小练习与答案

- **Q**：`row1/row2` 的下标是 `OVERLAPWIDTH`（列方向），可它们叫「row」。这种命名会带来什么隐患？
  **A**：会和后面那个作为「行计数器」的 `parameter row`（[L57](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L57)）撞名，让人误以为 `row1/row2` 是「某一行」而 `row` 是「哪一行」——实际上 `row1/row2` 是「一条扫描线的各列缓冲」。命名混乱会直接放大 4.3 里「行号/计数器复用」的迷惑。
- **Q**：为什么缝合线要逐行左右挪动，而不是固定一条直线？
  **A**：因为两图配准误差在空间上不均匀：某些行在列 100 处差异最小，另一些行可能在列 130 处最小。固定直线只能迁就某一处；逐行取局部最优的折线，能把每个行的接缝都放在「差异最小处」，整条接缝才看不见。

---

### 4.2 clk_wiz_0 与 mig_7series_0：两种 IP 例化姿态

#### 4.2.1 概念说明

本模块用了两个 Xilinx IP 核：

- **`clk_wiz_0`（时钟向导）**：把差分输入的 200MHz 时钟（`clk_p/clk_n`）转成单端系统时钟 `sys_clk`，给本模块所有 `always` 块当工作时钟。
- **`mig_7series_0`（MIG，Memory Interface Generator）**：7 系列 DDR3 的控制器 IP。它对用户暴露一组 `app_*` 应用接口（[u3-l1](u3-l1-ddr3-mem-burst.md) 详讲过），用户通过 `app_cmd/app_addr/app_en` 发命令、用 `app_rd_data/app_rd_data_valid` 收读数据。

**关键点**：IP 例化（`模块名 例化名 (.端口(信号), ...)`）属于 Verilog 的**并发语句**，**只能写在模块级**（也就是 `module ... endmodule` 之间、任何 `always`/`initial` 之外）。这一点本模块做对了一半：`clk_wiz_0` 写在模块级（姿态对），`mig_7series_0` 却写进了 `always` 块里（姿态错得离谱）。

#### 4.2.2 核心流程

```
差分时钟 clk_p/clk_n ──► clk_wiz_0 ──► sys_clk (单端, 200MHz) ──► 所有 always@(posedge sys_clk)
                                                    │
                                                    └──► 同时作为 mig_7series_0 的 sys_clk_i

DDR3 物理引脚 ◄──── mig_7series_0 ────► app_* 应用接口（app_addr/app_en/app_rd_data...）
                                        ▲
                        本模块在 always 块里驱动 app_*（❌ 错误位置）
```

#### 4.2.3 源码精读

先看姿态**正确**的 `clk_wiz_0`：[DynamicSeam.v:L38-L47](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L38-L47)

```verilog
clk_wiz_0 instance_name
   (
    .clk_out1(sys_clk),     // 输出：单端系统时钟
    .reset(rst_n),          // 复位（注意极性：clk_wiz 的 reset 通常高有效，rst_n 是低有效，可能反相）
    .locked(),              // 时钟稳定锁定指示（悬空未用）
    .clk_in1_p(cl_p),       // ❌ 拼写错误：模块端口是 clk_p，这里写成 cl_p
    .clk_in1_n(clk_n));
```

这段例化写在 `always` 之外（模块级），**位置是对的**。但有两个细节问题：

1. **`.clk_in1_p(cl_p)` 拼写错误**（[L46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L46)）：模块声明里的输入端口叫 `clk_p`（[L26](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L26)），这里却接成 `cl_p`。在默认 `default_nettype` 下，`cl_p` 会被当成一根**隐式声明的线网**（哪都不驱动，浮空），导致差分时钟的正端实际没接上。
2. **`.reset(rst_n)` 极性可疑**：`clk_wiz` 的 `reset` 一般是**高有效**，而本模块的 `rst_n` 是**低有效**（从命名和 [L93](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L93) 的 `if(!rst_n)` 可证），直接相连会让复位逻辑反相。（具体极性取决于 IP 生成时的配置，标注待确认。）

再看姿态**错误**的 `mig_7series_0`：它被写在主控 `always` 块的 `READ` 分支里——[DynamicSeam.v:L192-L232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L192-L232)。这段近 40 行的例化，整体嵌在 `always@(posedge sys_clk) case(cstate) READ: begin ... end` 里。这是本模块**最致命**的写法，原因见 4.5。

**对照参考 `mem_burst`**：[mem_burst.v:L26-L39](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L26-L39) 把 `app_*` 全部声明为**模块端口**，然后用 [mem_burst.v:L63-L76](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L63-L76) 的 `assign` 和一个独立的 `always@(posedge mem_clk)`（[L78-L233](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L78-L233)）去驱动它们。`mem_burst` **根本不例化 MIG**——它只负责「按 `app_*` 协议发命令」，MIG 由更顶层的模块去例化、把 `app_*` 连到 `mem_burst`。这才是「包装 MIG 应用接口」的干净分层。`DynamicSeam` 把 MIG 例化塞进 `always`，是把两层职责糊在了一起。

> 📌 **承接 u5-l2**：本讲只看 IP 的「例化姿态对不对」。[u5-l2 CORDIC 与 MIG/时钟 IP 集成](u5-l2-cordic-mig-ip-integration.md) 会专门讲 `cordic_0/clk_wiz_0/mig_7series_0` 三个 IP 的端口含义与时钟来源（`sys_clk/ui_clk/mem_clk` 的频率关系），具体 IP 配置细节此处标注待确认。

#### 4.2.4 代码实践

**实践目标**：体会「IP 例化必须写在模块级」这条硬规则。

**操作步骤**：

1. 在 [DynamicSeam.v:L38-L47](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L38-L47) 确认 `clk_wiz_0` 写在所有 `always` 之外。
2. 在 [DynamicSeam.v:L184-L192](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L184-L192) 数清楚 `mig_7series_0` 的例化嵌套层级：它在第几层 `begin...end` 里？
3. 对比 [mem_burst.v:L78](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L78) 的 `always`——里面有没有任何模块例化？

**需要观察的现象**：`clk_wiz_0` 在模块级（0 层嵌套）；`mig_7series_0` 在 `always → case → READ → begin` 之内（3 层嵌套，过程块内部）；`mem_burst` 的 `always` 里**没有任何例化**，只有对寄存器的赋值。

**预期结果**：能一眼判断「这段例化写在了非法位置」。（源码阅读型实践，无需运行。）

#### 4.2.5 小练习与答案

- **Q**：为什么 `clk_wiz_0` 的例化位置合法、`mig_7series_0` 的不合法？
  **A**：模块例化是并发语句，必须出现在模块级（`module` 与 `endmodule` 之间、过程块之外）。`clk_wiz_0` 写在所有 `always` 之外，符合；`mig_7series_0` 写在 `always` 的 `case` 分支里，属于过程块内部，综合器无法把它映射成「一直存在」的硬件实例。
- **Q**：`mem_burst` 自己不例化 MIG，那 DDR3 物理引脚（`ddr3_dq` 等）由谁驱动？
  **A**：由**顶层模块**例化 MIG 来驱动——顶层把 MIG 的 `app_*` 端口连到 `mem_burst` 的 `app_*` 端口，`mem_burst` 只管「按协议发命令」。`DynamicSeam` 试图在一个模块里既发命令又直接例化 MIG，混淆了分层。

---

### 4.3 三状态机：IDLE → READ → SeamFind（与计数器复用）

#### 4.3.1 概念说明

本模块用「三段式状态机」的骨架（[u1-l3](u1-l3-uart-fsm.md) 讲过这个术语）：一个 `always` 存当前状态、一个 `always @(*)` 算次态、若干 `always` 算输出（这里输出就是各种计数器和对 `app_*` 的驱动）。三个状态：

- **`IDLE`**：空闲，等 `read_request`。
- **`READ`**：从 DDR3 读重叠区像素，填 `row1/row2`。计数器 `cnt` 跑 `0..2*OVERLAPWIDTH`（前一半填 `row1`、后一半填 `row2`）。
- **`SeamFind`**：扫描 `col`，做 DP 更新 `cost/coordinate`。计数器 `cnt` 复用为 `0..OVERLAPWIDTH`。

这里有一个本模块特有的毛病——**计数器复用/行号复用**：同一个 `cnt` 在 `READ` 阶段跑 `2*OVERLAPWIDTH`、在 `SeamFind` 阶段又跑 `OVERLAPWIDTH`；同时 `SeamFind` 阶段还有另一个 `col` 也在跑 `0..OVERLAPWIDTH`。`cnt` 和 `col` 在 `SeamFind` 阶段几乎做同一件事，代码却两个都用，让人无法判断「到底哪个才是当前列游标」。

#### 4.3.2 核心流程

```
       read_request=1             cnt==2*OVERLAPWIDTH         cnt==OVERLAPWIDTH
IDLE ──────────────────► READ ───────────────────────► SeamFind ─────────────────► IDLE
                          │                                  │
                          │ cnt: 0..2*OVERLAPWIDTH           │ cnt:  0..OVERLAPWIDTH  (复用)
                          │ read_col: 0..OVERLAPWIDTH        │ col:  0..OVERLAPWIDTH  (与 cnt 重复)
                          │ row1/row2[read_col] ← DDR3        │ 更新 cost[col]/coordinate[col]
```

#### 4.3.3 源码精读

状态码与状态寄存器：[DynamicSeam.v:L52-L59](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L52-L59)

```verilog
reg [1:0] cstate;
reg [1:0] nstate;
parameter IDLE = 0;
parameter READ = 1;
parameter SeamFind = 2;
parameter row = 0;       // ❌ 想当「行计数器」，却声明成 parameter（常量）
parameter col = 0;       // ❌ 想当「列游标」，却声明成 parameter
parameter read_col = 0;  // ❌ 想当「读列游标」，却声明成 parameter
```

注意这 6 个 `parameter` 里，前 3 个（`IDLE/READ/SeamFind`）当**状态码常量**用，是 `parameter` 的正确用法；后 3 个（`row/col/read_col`）当**会变化的循环计数器**用，是**错误用法**——`parameter` 是编译期常量，综合后就是「一根硬接的常数线」，根本不能被 `<=` 改写。可偏偏 [L139-L181](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L139-L181) 的三个 `always` 块都在用 `<=` 给它们赋值（如 `row <= row + 1'b1`）。这是 4.5 要列出的综合错误之一。

次态逻辑：[DynamicSeam.v:L99-L118](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L99-L118)

```verilog
case(cstate)
    IDLE:     nstate <= read_request ? READ : IDLE;
    READ:     nstate <= (cnt == OVERLAPWIDTH * 2) ? SeamFind : READ;
    SeamFind: nstate <= (cnt == OVERLAPWIDTH)     ? IDLE     : SeamFind;
endcase
```

`cnt` 的复用就在转移条件里露馅：`READ` 阶段判 `cnt == OVERLAPWIDTH*2`（读两幅图各 `OVERLAPWIDTH+1` 个像素），`SeamFind` 阶段判 `cnt == OVERLAPWIDTH`。同一个 `cnt`，两段含义。

再看 `SeamFind` 阶段两个并行自增的游标——`cnt`（[L131-L136](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L131-L136)）和 `col`（[L159-L166](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L159-L166)），两者在 `SeamFind` 里都是「从 0 数到 `OVERLAPWIDTH`」：

```verilog
SeamFind:                                   // cnt 块 (L131)
    cnt <= (cnt == OVERLAPWIDTH) ? 0 : cnt + 1;
...
SeamFind :                                  // col 块 (L159)
    col <= (col < OVERLAPWIDTH) ? col + 1 : 0;
```

两个游标同步自增、范围相同，于是后面 DP 主体里 `if(cnt < OVERLAPWIDTH)`（[L246](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L246)）和用 `col` 当下标（[L248](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L248)）其实指的是同一个列位置——这就是「行号/计数器复用」带来的可读性灾难：读者永远搞不清 `cnt` 和 `col` 谁是真正的列游标。

> 📌 **对照 u1-l3 的三段式状态机**：UART 收发器用「状态寄存器 + 次态组合逻辑 + 输出逻辑」的干净三段式。本模块骨架相同，但输出逻辑被拆成了 5 个 `always`（`cnt/row/col/read_col` + 主控），且其中三个驱动的是 `parameter`——骨架对、填料错。

#### 4.3.4 代码实践

**实践目标**：把「`cnt` 与 `col` 在 SeamFind 阶段重复」这件事看清楚。

**操作步骤**：

1. 在 [L121-L137](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L121-L137)（`cnt` 块）和 [L154-L167](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L154-L167)（`col` 块）分别找到 `SeamFind` 分支。
2. 列表对比两者在 `SeamFind` 下的「判零条件 / 自增 / 上界」。
3. 在 [L246](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L246) 看 DP 的「闸门」用的是 `cnt`，在 [L248](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L248) 看下标用的是 `col`。

**需要观察的现象**：`cnt` 和 `col` 在 `SeamFind` 阶段的轨迹完全一致（同步从 0 涨到 `OVERLAPWIDTH` 再回 0），代码却一个当闸门、一个当下标。

**预期结果**：得出结论「这两个计数器功能重复，应合并成一个 `col`，并把 `cnt` 的职责限定在 `READ` 阶段」。（源码阅读型实践。）

#### 4.3.5 小练习与答案

- **Q**：`READ` 阶段为什么 `cnt` 要数到 `2*OVERLAPWIDTH`，而不是 `OVERLAPWIDTH`？
  **A**：因为要读**两幅图**：前 `OVERLAPWIDTH+1` 拍填 `row1[0..OVERLAPWIDTH]`，后 `OVERLAPWIDTH+1` 拍填 `row2[0..OVERLAPWIDTH]`（见 [L234-L241](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L234-L241) 的 `if(cnt <= OVERLAPWIDTH)` 分流）。两段加起来约 `2*(OVERLAPWIDTH+1)`，故判 `2*OVERLAPWIDTH`。
- **Q**：把 `row/col/read_col` 从 `parameter` 改成 `reg`，状态机就能综合了吗？
  **A**：只解决了「计数器类型」这一处错误。本模块还有「`always` 内例化模块」「`always` 内声明 `localparam`」「`wire` 被过程赋值」等多处问题（见 4.5），都要一并改才行。

---

### 4.4 缝合线动态规划：cost / coordinate 数组的最小代价路径更新

#### 4.4.1 概念说明

这是本模块的算法内核。先用一句话讲清标准 DP 缝合线的递推，再看代码「想实现但没实现对」的版本。

**标准 DP 缝合线**（按行推进、在列方向找路径）：设重叠区有 `H` 行、`W+1` 列。对第 `i` 行第 `j` 列，定义单点代价 \(e_i(j)\)（通常取两图差异 \(|row1_i(j)-row2_i(j)|\)），累计代价 \(M\) 与回溯指针 \(B\) 的递推为：

\[
M_i(j) = e_i(j) + \min\bigl(M_{i-1}(j-1),\; M_{i-1}(j),\; M_{i-1}(j+1)\bigr)
\]

\[
B_i(j) = \arg\min_{k\in\{j-1,j,j+1\}} M_{i-1}(k)
\]

即「本格累计代价 = 本格代价 + 上一行三个邻居里最小的累计代价」，并记下「最小那个邻居是哪一列」。扫完所有行后，最后一行里 \(M\) 最小的列就是缝合线终点，沿 \(B\) 反向走回第一行，就得到整条折线。

代码里的 `cost[col]` 对应 \(M\)，`coordinate[col]` 对应 \(B\)——但因为本模块只存「一维」数组（没有按行展开成二维），它实际只能处理「单行」的局部选择，并不是完整的二维 DP。

#### 4.4.2 核心流程

代码（试图）实现的单行扫描逻辑：

```
对 col = 0 .. OVERLAPWIDTH:
    index = coordinate[col]              // 取「上一次选中的列」
    min_val = row2[index]                // 候选代价初值（注意：用的是 row2，不是 |row1-row2|）
    若 col>0 且 row2[index-1] < min_val:  min_val = row2[index-1]; 记 index-1
    若 col<OVERLAPWIDTH 且 row2[index+1] < min_val: min_val = row2[index+1]; 记 index+1
    coordinate[col] = 选中的列
    cost[col] = cost[col] + min_val      // 累计代价
```

理想（正确）的单点代价应是 \(e=|row1-row2|\)，而代码直接拿 `row2[index]` 当代价——这在语义上是错的（`row2` 是像素灰度，不是两图差异），但**思路**是想做「取三个邻居的最小、累加进 cost、记录回溯」。4.5 会看到，即使是这个「思路」也因多处语法错误无法综合。

#### 4.4.3 源码精读

DP 主体在主控 `always` 的 `SeamFind` 分支：[DynamicSeam.v:L243-L263](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L243-L263)

```verilog
SeamFind:
begin
    if(cnt < OVERLAPWIDTH)
    begin
        localparam index = coordinate[col];   // ❌ localparam 不能在 always 内声明，也不能用非常量初始化
        localparam min   <= row2[index];      // ❌ localparam 不能用 <= 赋值；min 还是常数
        coordinate[col] <= index;
        if(col > 0 && min < row2[index - 1])
        begin
            min <= row2[index - 1];            // ❌ 给 localparam 赋值
            coordinate[index] <= index - 1;    // ❌ 下标一会儿 [col] 一会儿 [index]，且 coordinate 被多处驱动
        end
        if(col < OVERLAPWIDTH && min < row2[index + 1])
        begin
            min <= row2[index + 1];
            coordinate[index] <= index + 1;
        end
        cost[col] = cost[col] + min;           // ❌ 阻塞 '=' 与上面 '<=' 混用；读到的 min 是初值
    end
end
```

逐条点出算法层的问题（语法/综合错误留到 4.5 集中讲）：

- **代价取错对象**：`min <= row2[index]` 把第二幅图的像素灰度直接当「代价」。正确的单点代价应是 \(e=|row1[col]-row2[col]|\)——两图差异越小越适合切。用裸 `row2` 会让 DP 朝着「`row2` 灰度小的列」走，而非「两图一致的列」走，语义错误。
- **下标混乱 `[col]` vs `[index]`**：`coordinate[col] <= index`（写当前列）、`coordinate[index] <= index-1`（写下标为 `index` 的表项）。到底在更新「当前列的回溯指针」还是「邻居列的回溯指针」？同一拍里 `coordinate` 的两个不同表项被同时改写，且 `index` 本身又是 `coordinate[col]` 读出的值——读写交织，语义自相矛盾。
- **阻塞/非阻塞混用**：`cost[col] = cost[col] + min` 用阻塞 `=`，而 `min <= row2[...]` 用非阻塞 `<=`。在同一 `always` 里，`<=` 的赋值在块结束时才生效，所以这里的 `min` 永远是**初值**（第一行 `localparam min <= row2[index]` 那次的值），后面两个 `if` 对 `min` 的更新**根本读不到**。即便修掉 `localparam`，这个时序也读不出正确的 `min`。
- **`coordinate` 多处驱动**：`coordinate[col] <= index` 与 `coordinate[index] <= index±1` 可能在同一拍写不同表项，但当 `index == col` 时就变成**同一表项两个驱动源**，综合上属于多驱动冲突。

把这四点和 4.4.1 的标准递推对照看：作者的**意图**是 \(M_j \mathrel{+}= \min(\text{邻居})\) 并记录 \(\arg\min\)，方向没错；但「代价取 `row2`」「下标 `col/index` 混用」「`min` 时序读不到」让这份代码即便能综合，结果也是错的。

#### 4.4.4 代码实践

**实践目标**：把「正确的 DP 单点代价」和「代码实际用的代价」对照清楚。

**操作步骤**：

1. 假设 `OVERLAPWIDTH=4`，给定一行数据：`row1 = {10, 12, 200, 205, 11}`、`row2 = {11, 13, 198, 204, 12}`。
2. 按标准 DP 计算**正确的**单点代价 \(e(j) = |row1[j] - row2[j]|\)：得 \(\{1,1,2,1,1\}\)。
3. 按代码的写法，候选代价取自 `row2`：得 \(\{11,13,198,204,12\}\)。
4. 想象 DP 会在两种代价下分别「偏好」哪一列：正确代价下各列差不多、偏好差异最小的列 0/1/4；`row2` 代价下会强烈偏好 `row2` 最小的列 0（灰度 11）。

**需要观察的现象**：用 `row2` 当代价时，DP 的选择被「第二幅图本身亮不亮」主导，与「两图接缝明不明显」无关。

**预期结果**：理解为什么 4.4.3 说「代价取错对象」是语义错误——即便语法修对，缝合线也会找错地方。（纸面推演，待本地用完整 RTL 验证。）

#### 4.4.5 小练习与答案

- **Q**：标准缝合线 DP 的单点代价为什么用 \(|row1-row2|\) 而不是 `row2` 本身？
  **A**：缝合线要走在「两图差异最小」处，这样切过去接缝才看不见；\(|row1-row2|\) 正是「两图在此列差多少」的度量。`row2` 只是其中一幅图的亮度，和「差异」无关。
- **Q**：代码里 `cost[col] = cost[col] + min` 用了阻塞赋值，而 `min` 用非阻塞更新。实际生效的 `min` 是哪个？
  **A**：非阻塞 `<=` 在 `always` 块结束时才更新，阻塞 `=` 立即生效但因 `min` 此刻还没被更新，读到的是「本拍进入块时的旧值」——也就是 `localparam min <= row2[index]` 那行试图设的初值。两个 `if` 对 `min` 的更新本拍读不到，下一拍又被重新声明覆盖。所以累加进 `cost` 的永远是初值。

---

### 4.5 综合性缺陷剖析：为什么这段代码不能直接综合（本讲核心）

#### 4.5.1 概念说明

仿真器（如 Vivado 的 xelsim、ModelSim）是「行为级解释器」——它按代码字面意思逐句执行，对很多不合规的写法**宽容**（只要能跑出波形就行）。综合器（如 Vivado Synthesis）则要把代码**翻译成真实的触发器/组合门网表**，对写法有一系列硬性要求。很多「仿真能跑」的代码综合器会直接拒绝。

本模块集中了多种「仿真宽容、综合拒绝」的写法，所以文件头注释才声明「外部代码不能综合」。这一节是本讲的指定实践任务的核心：识别**至少三处**导致综合失败的写法。

先补三条综合器的基本铁律：

1. **`parameter` / `localparam` 是常量**：它们在编译期就被替换成固定值，综合后是「电源/地」或「常数驱动」。**不能被过程赋值（`=` / `<=`）改写**。
2. **模块例化是并发语句**：`模块名 例化名(.端口(信号));` 只能出现在模块级（`module...endmodule` 之间、过程块之外），**不能写在 `always` / `initial` 里**。
3. **被过程赋值驱动的信号必须是 `reg`/变量类型**：`wire` 只能被 `assign` 或例化端口驱动，**不能在 `always` 里用 `<=` 赋值**。

#### 4.5.2 核心流程

```
综合器读 DynamicSeam.v
   │
   ├─ ① 看到 parameter row/col/read_col 在 always 里被 <= 赋值  → 报错：常量不能被驱动
   ├─ ② 看到 mig_7series_0 例化写在 always 的 case 分支里      → 报错：例化不能出现在过程块内
   ├─ ③ 看到 localparam index/min 在 always 内声明并被 <= 赋值  → 报错：localparam 位置/赋值非法
   ├─ ④ 看到 wire app_addr 在 always 里被 <= 赋值               → 报错：wire 不能被过程赋值驱动
   ├─ ⑤ 看到 .clk_in1_p(cl_p) 接到未声明的 cl_p                 → 报错或告警：悬空/隐式线网
   └─ ⑥ 看到 cost[col] = ... (阻塞) 与 min <= ... (非阻塞) 混用  → 告警/时序错乱
   ▼
 综合失败 / 即便勉强通过也行为错误 → 印证「外部代码不能综合」
```

#### 4.5.3 源码精读（三处致命写法 + 补充）

**① `parameter` 当变量用** — [DynamicSeam.v:L57-L59](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L57-L59) 声明、[L139-L181](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L139-L181) 赋值：

```verilog
parameter row = 0;       // L57  常量！
parameter col = 0;       // L58
parameter read_col = 0;  // L59
...
always@(posedge sys_clk)                 // L139 块
    case(cstate)
      SeamFind: row <= row + 1'b1;        // ❌ 给常量赋值
    endcase
```

`row/col/read_col` 都声明成 `parameter`（编译期常量），却在三个 `always` 里被 `<=` 自增。综合器无法把它们映射成「会变的触发器」——常量在硅片上就是固定电平，不可能 `+1`。**修复**：改成 `reg [31:0] row = 0;` 等。

**② 模块例化写进 `always` 块** — [DynamicSeam.v:L184-L265](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L184-L265)，关键在 [L192](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L192)：

```verilog
always@(posedge sys_clk) begin
    case(cstate)
      READ: begin
          mig_7series_0 u_mig_7series_0 ( ... );   // ❌ 例化出现在 always → case → READ → begin 内
          ...
      end
    endcase
end
```

模块例化是**并发语句**，描述的是「一个一直存在的硬件实例」，没有「时序」概念，不能放在「当时钟上升沿到来才执行」的过程块里。这就像在 C 语言里把一个全局变量的定义塞进 `if` 分支——语法层面就不成立。**这是最致命的错误**：任何综合器都会在 elaboration 阶段直接报错，整个模块都过不了。**修复**：把 `mig_7series_0` 例化提到模块级（`always` 之外），参照 `mem_burst` 的分层。

**③ `always` 块内声明 `localparam` 并用 `<=` 赋值** — [DynamicSeam.v:L248-L249](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L248-L249)：

```verilog
localparam index = coordinate[col];   // ❌ localparam 须为常量表达式，coordinate[col] 不是；且过程块内不能这么声明
localparam min   <= row2[index];      // ❌ localparam 不能用 <= 赋值
```

`localparam` 是「局部常量」，必须能在编译期求值（如 `localparam CYCLE = CLK_FREQ/BAUD;`，见 [u1-l3](u1-l3-uart-fsm.md) 的 UART 分频）。这里用 `coordinate[col]`（一个运行时寄存器数组的读出值）去初始化它，已经不是常量；再用 `<=` 赋值更是双重违规。**修复**：改成 `reg [31:0] index; reg [31:0] min;`，并在组合 `always @(*)` 或时序 `always` 里正确赋值。

**补充缺陷**（不要求全部，列出以备实践任务选用）：

- **`wire app_addr` 被过程赋值驱动**：[L72](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L72) 声明 `wire [ADDR_WIDTH-1:0] app_addr;`，但 [L189](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L189) 用 `app_addr <= 29'b0;`。`wire` 不能被过程赋值驱动，应改 `reg`（或改用 `assign`）。
- **差分时钟正端拼写错误**：[L46](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L46) `.clk_in1_p(cl_p)`，应为 `clk_p`。
- **`app_*` 时钟域错配**：所有 `always` 用 `posedge sys_clk`（200MHz），但 MIG 的 `app_*` 接口按协议应当工作在 MIG 输出的 `ui_clk` 域（见 [u3-l1](u3-l1-ddr3-mem-burst.md) 的 `mem_clk` 讨论）。即便例化位置改对，时钟域也得对齐。

**为什么文件头要写「外部代码不能综合」？** 把 ①②③ 任意一条单拎出来，都足以让综合器拒绝整个模块；本模块三条全占，外加补充缺陷若干。所以作者坦白标注：这份代码只表达算法思路，不能进真实综合流程。

#### 4.5.4 代码实践（本讲指定实践任务）

**实践目标**：独立指出 `DynamicSeam.v` 中三处会导致综合失败的写法，并用 RTL 思路描述正确的 `cost` 更新时序。

**操作步骤**：

1. 通读 [DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v)，列出你找到的「不能综合」写法及对应行号。
2. 对每条，写一句话原因（违反了 4.5.1 的哪条铁律）。
3. 重点针对 DP 主体 [L243-L263](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L243-L263)，描述正确的 `cost` 更新时序该怎么排。

**参考答案（三处致命写法）**：

| # | 写法 | 行号 | 原因 |
|---|---|---|---|
| ① | `parameter row/col/read_col` 在 `always` 里被 `<=` 自增 | L57-L59 声明；L139-L181 赋值 | `parameter` 是编译期常量，综合后是固定电平，不能被过程赋值驱动 |
| ② | `mig_7series_0` 例化写在 `always → case → READ → begin` 内 | L192-L232 | 模块例化是并发语句，只能出现在模块级，不能在过程块内 |
| ③ | `always` 内 `localparam index/min` 声明且用 `<=` 赋值 | L248-L249 | `localparam` 必须为常量表达式且声明位置受限，不能用运行时值初始化、不能用 `<=` 赋值 |

（若你列出的是「`wire app_addr` 被过程赋值」「`cl_p` 拼写错误」等补充缺陷，同样算对——它们也都会让综合失败或行为错误。）

**正确的 `cost` 更新时序（RTL 重设计思路）**：

1. **把所有计数器改成 `reg`**：`reg [31:0] row=0, col=0, read_col=0, cnt=0;`，`row/col/read_col` 不再是 `parameter`。
2. **MIG 例化提到模块级**：`mig_7series_0` 写在 `always` 之外，`app_*` 改为 `reg` 型，在 `ui_clk` 域的独立 `always` 里按 `mem_burst` 的协议驱动（发命令、数 `app_rdy`、收 `app_rd_data_valid`），不要内嵌例化。
3. **DP 改成「按行流水、两块 RAM 交替」**：用两块双口 RAM 分别存「上一行累计代价 `cost_prev`」和「当前行累计代价 `cost_curr`」。每个时钟处理一列 `col`：
   - 读入 `row1[col]`、`row2[col]`，算单点代价 `e = (row1[col] >= row2[col]) ? row1[col]-row2[col] : row2[col]-row1[col];`
   - 同时从 `cost_prev` 读出 `cost_prev[col-1]`、`cost_prev[col]`、`cost_prev[col+1]`（用前一拍预取的地址读）；
   - 在组合逻辑里取三者最小 `min_c` 与对应来源 `arg`；
   - 时序写：`cost_curr[col] <= e + min_c;`、`coordinate[col] <= arg;`（都用非阻塞 `<=`）。
   - 一行扫完（`col` 走到 `OVERLAPWIDTH`）后，把 `cost_curr` 整体搬进 `cost_prev`（或交换两块 RAM 的读/写指针），`row++`，处理下一行。
4. **组合逻辑与寄存器分离**：取最小值 `min` 这种「无记忆」的运算放进组合 `always @(*)` 或 `assign`；只把需要「跨拍保持」的 `cost_curr/coordinate` 放进时序 `always @(posedge clk)`，且一个信号只在**一个** `always` 里赋值，杜绝多驱动。

这样每个时钟固定处理一列、行间依赖通过「`cost_prev`/`cost_curr` 双 RAM + 行末交换」排成干净流水，OVERLAPWIDTH 个时钟处理一行、OVERLAPHEIGHT 行处理完整幅，时序可收敛、综合可通过。

**需要观察的现象 / 预期结果**：重写后，① `row/col` 是真正的寄存器，能在波形里看到逐拍自增；② MIG 例化在模块级、`app_*` 波形与 `mem_burst` 风格一致；③ `cost` 更新每拍只读旧值、写新值，`min` 在组合层算好、不再有时序错位。（本实践为源码阅读 + 设计型，**待本地用 Vivado 仿真与综合验证**；本仓库未收录重写后的代码。）

#### 4.5.5 小练习与答案

- **Q**：把 `parameter row` 改成 `reg row`，`row <= row + 1` 就能综合了吗？
  **A**：这一条改对了。但模块还有「`always` 内例化 MIG」「`always` 内 `localparam`」等其他致命错误，要全部修才行——单改一处不足以让整模块通过综合。
- **Q**：为什么「`always` 内例化模块」在仿真器里有时却「不报错」？
  **A**：因为本模块连基本的 elaboration（例化展开）都过不了，仿真器通常在编译阶段就会失败；个别仿真器对某些写法宽容也只是「没崩」，不代表波形有意义。综合器的检查更严格、更贴近真实硬件语义，所以这类写法在综合阶段必然暴露。
- **Q**：标准 DP 缝合线在硬件上为什么不能「一个时钟算完整幅」？
  **A**：因为 DP 有行间数据依赖（第 `i` 行的累计代价必须等第 `i-1` 行全部算完才能开始），且单点要比较三个邻居——展开成纯组合逻辑路径极深、时序无法收敛。所以硬件上普遍排成「逐列流水 + 行末交换双 RAM」的折叠结构，用多个时钟换可综合性。

---

## 5. 综合实践

把本讲五块知识（模块定位、IP 集成姿态、状态机、DP、综合缺陷）串成一个「**给 DynamicSeam 做综合体检 + 重写处方**」的任务。这正是本讲的指定实践任务。

### 任务：综合体检 + cost 更新时序重写

**A. 综合体检（找错）**

通读 [DynamicSeam.v](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v)，独立列出至少**三处**会导致综合失败的写法，每条给出：行号、违规类型（常量被赋值 / 例化在过程块内 / localparam 非法 / wire 被过程驱动 / …）、一句话原因。（参考答案见 4.5.4 的表。）

**B. 算法层批评（语义）**

即便语法全部修对，指出 DP 主体 [L243-L263](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L243-L263) 里两处**语义**错误：① 代价取的是 `row2` 而非 \(|row1-row2|\)；② `cost[col] = cost[col] + min` 用阻塞赋值却想读非阻塞更新的 `min`，结果读到初值。

**C. 重写处方（设计）**

用一段 RTL 思路描述正确的 `cost` 更新时序，要点（参考答案见 4.5.4）：计数器改 `reg`；MIG 例化提到模块级、`app_*` 改 `reg` 并在 `ui_clk` 域驱动；DP 用 `cost_prev/cost_curr` 双 RAM、逐列流水、组合层取 min、时序层写 `cost_curr/coordinate`、行末交换两块 RAM。

**验收标准**：能说出「这段代码为什么不能综合」（A）、能指出「就算能综合结果也是错的」（B）、能画出「正确 cost 更新的时序与数据通路」（C），就达成本讲目标。整个任务在纸面/源码层面完成，无需上板；重写后的代码为本练习产出，仓库未收录。

## 6. 本讲小结

- `DynamicSeam` 的职责是：从 DDR3 读两幅投影图的重叠区行数据（`row1/row2`），用动态规划在 `cost/coordinate` 两个数组里找一条「累计代价最小」的缝合线（[L24-L34](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L24-L34)）。
- 三状态机 `IDLE → READ → SeamFind`：`READ` 用 `cnt` 数 `2*OVERLAPWIDTH` 读两幅图、`SeamFind` 用 `cnt`/`col` 扫列做 DP；`cnt` 与 `col` 在 `SeamFind` 重复，是「计数器/行号复用」的可读性陷阱（[L99-L181](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L99-L181)）。
- DP 标准递推是 \(M_i(j)=e_i(j)+\min(M_{i-1}(j-1),M_{i-1}(j),M_{i-1}(j+1))\)，代码的 `cost[col]`↔\(M\)、`coordinate[col]`↔回溯指针；但代码把代价取成 `row2` 而非 \(|row1-row2|\)，语义错误（[L243-L263](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L243-L263)）。
- 文件头声明「外部代码不能综合」（[L21-L23](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L21-L23) 乱码，据上下文推断），原因是至少三处致命写法：① `parameter` 当变量（[L57-L59](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L57-L59)）、② `mig_7series_0` 例化写进 `always`（[L192-L232](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L192-L232)）、③ `localparam` 在 `always` 内声明并被 `<=` 赋值（[L248-L249](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/%E5%8A%A8%E6%80%81%E8%A7%84%E5%88%92%E6%B3%95%E5%AF%BB%E6%89%BE%E6%9C%80%E4%BD%B3%E7%BC%9D%E5%90%88%E7%BA%BF/DynamicSeam.v#L248-L249)）。
- 正确的 cost 更新应：计数器改 `reg`、MIG 例化提到模块级并按 `mem_burst` 协议在 `ui_clk` 域驱动、DP 用 `cost_prev/cost_curr` 双 RAM 逐列流水、组合层取 min、时序层写 `coordinate`，行末交换两块 RAM。
- **核心方法论**：读「半成品/有问题 RTL」时，要分清「作者想做什么」（算法意图）与「代码实际能做什么」（综合可行性），两者往往不一致——这种鉴别力是专家层的关键。

## 7. 下一步学习建议

- **横向对照**：回看 [u3-l1 mem_burst](u3-l1-ddr3-mem-burst.md) 的 [L78-L233](https://github.com/mhhai/ImageStitchBasedOnFPGA/blob/8728b11c25af93dd1b4f7606aca64433827adfd5/DDR3%E6%8E%A7%E5%88%B6/mem_burst.v#L78-L233)，把「MIG 例化的正确分层」和本讲的「错误内嵌例化」并排比较，巩固「例化只能在模块级」的规则。
- **下一篇 [u5-l1 定点数运算与位宽设计](u5-l1-fixed-point-arithmetic.md)**：回到 `圆柱面投影.v`，深入定点 Q 格式与位宽推导——本讲的 `cost` 累加、`|row1-row2|` 代价计算，在真实实现里同样要面对定点位宽与溢出问题。
- **之后 [u5-l2 CORDIC 与 MIG/时钟 IP 集成](u5-l2-cordic-mig-ip-integration.md)**：系统讲清 `cordic_0/clk_wiz_0/mig_7series_0` 三个 IP 的端口、时钟来源（`sys_clk/ui_clk/mem_clk`）与正确例化方式，补全本讲 4.2 留下的「IP 配置待确认」部分。
- **收官 [u5-l3 系统集成与架构取舍](u5-l3-system-integration-tradeoffs.md)**：把投影、DDR3、缝合线串成完整数据通路，讨论「缝合线该用 DP 还是 graph-cut」「cost 用定点还是查表」等架构取舍，并回到 README 列出的遗留问题给出改进方向。
- **动手延伸**：参照本讲 4.5.4 的重写处方，自己用「双 RAM 逐列流水」结构写一个**可综合的**单行 seam DP 模块（输入 `row1/row2`、输出 `coordinate`），先用 `OVERLAPWIDTH=8` 跑仿真验证递推正确，再尝试综合。注意：这是你自己的练习产出，不是仓库代码。
