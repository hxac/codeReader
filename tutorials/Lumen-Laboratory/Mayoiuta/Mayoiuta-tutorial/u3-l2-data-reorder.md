# 数据格式重排 Data Reorder

## 1. 本讲目标

上一讲（u3-l1）我们看完了 `Memory_Controller`：它把权重和激活「存」进了多 bank 的存储里，`npu_rd_data` 一个周期吐出一大块数据。但这里有个问题——**主机写进存储的数据，排布方式未必是计算单元想要的**。深度学习框架里的张量常以 NCHW 或 NHWC 排列，而本项目的 PE 阵列（u2-l1）、卷积引擎（u2-l2）对数据进入的顺序有自己的偏好。`Data_Reorder` 就是夹在「存储」和「计算」之间的这道**数据整形**工序。

学完本讲，你应当能够：

- 说出 **NCHW、NHWC、Blocked** 三种数据排布的区别，并能用「通道优先」「空间优先」概括前两者。
- 解释 `reorder_buffer` 这个**二维缓冲数组**是如何先把数据重排好、再由 `read_ptr` 流式输出的。
- 对照源码看懂 NCHW 与 NHWC 两条分支里**位拼接（bit concatenation）**抽的是哪些位段、为什么抽的位置不同。
- 识别本模块里几个明显的**待确认/骨架**问题：`read_ptr` 从未声明、`pe_data` 被两个 `always` 块同时驱动、`reorder_buffer` 只用单个下标读取导致维度不匹配、以及它和 `mem_ctl.v` 的位宽根本对不上。

> 本讲会反复出现「待确认」标注。原因和前几讲一样：仓库是源码阅读型项目，很多模块是骨架，并不保证可综合或已联调。我们的原则是**如实指出**，而不是把半成品讲成可用功能。

---

## 2. 前置知识

本讲承接 u3-l1（存储控制器），并回扣 u2-l1（PE 阵列）。下面把本讲要用的几个术语用大白话过一遍。

### 2.1 张量的排布：N、C、H、W

一个四维张量有四个轴：

- **N**（batch）：一批里有几张图。
- **C**（channel）：通道数，比如 RGB 图是 3 通道，卷积层中间可能是 64/128 通道。
- **H**（height）、**W**（width）：一张特征图的空间高和宽。

「排布」回答的是：**在一段连续的内存里，这四个轴谁在外、谁在内？** 同样的数据，排布不同，内存里相邻两个字之间的关系就完全不同。这正是 `Data_Reorder` 要处理的事。

### 2.2 NCHW 与 NHWC

- **NCHW**：轴的顺序是 batch → channel → height → width。通道（C）靠外，空间（H、W）靠内。**同一通道的所有像素连续存放**，想取「下一通道」得跨过一大段空间数据。俗称**通道优先**。
- **NHWC**：轴的顺序是 batch → height → width → channel。空间靠外，通道靠内。**同一空间位置（同一像素）的各通道连续存放**。俗称**空间优先**。

直觉记忆：把 4 个字母里**最先出现 H/W 的那个**记成「空间优先」即可——NHWC 在 C 之前就出现了 H、W，所以空间优先；NCHW 把 C 摆在 H、W 前面，所以通道优先。

### 2.3 位拼接与位段抽取（gather）

Verilog 里 `{a, b, c}` 把若干段位**首尾相接拼成一个更宽的向量**。反过来，从一根宽线里「挑出某些位段再重新拼起来」的操作，常被称为 **gather（聚集）**。本讲的 NCHW/NHWC 分支干的就是这件事：从 128 位的 `mem_data` 里抽出若干不连续的小段，重新拼成缓冲里的一个字。

### 2.4 二维数组与 tile（块）

把一大块数据切成固定大小的小块叫 **tile**（瓦片）。本模块用 `TILE_SIZE` 表示每块的边长，并把重排结果存在一个**二维数组** `reorder_buffer` 里——第一维像「第几个块」，第二维像「块内第几个字」。这种二维缓冲在硬件里很常见，作用是先把一块数据凑齐、整好形，再连续不断地喂给下游计算单元。

> 名词速查：NPU、RTL、Verilog module、非阻塞赋值 `<=`、PE 阵列在前几讲已建立，直接使用。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它牵出好几处「骨架/待确认」的观察点，是本讲的重头戏。

| 文件 | 作用 | 本讲如何使用 |
|---|---|---|
| [data_reorder.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v) | `Data_Reorder` 模块：按 `data_format` 把 `mem_data` 重排到 `reorder_buffer`，再流式输出到 `pe_data` | 逐行精读 |
| [mem_ctl.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/mem_ctl.v) | 上一讲的存储控制器，输出 `npu_rd_data` | 仅用于对比位宽，确认两者未直接对接 |

先看模块全貌。`Data_Reorder` 的参数、端口、缓冲、两个 `always` 块加起来不到 45 行，非常紧凑：

[data_reorder.v:1-12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L1-L12) — 声明模块名、两个参数（`CHANNELS=64`、`TILE_SIZE=8`）、五个端口（`clk/rst_n/mem_data/pe_data/data_format`），以及二维重排缓冲 `reorder_buffer`。

下面按机制拆成四个最小模块来讲。

---

## 4. 核心概念与源码讲解

### 4.1 数据排布格式：NCHW / NHWC / Blocked

#### 4.1.1 概念说明

为什么要重排？因为**计算单元的「胃口」和存储里的「摆法」常常对不上**。以 u2-l1 的 PE 阵列为例：它要求权重自西向东、激活自北向南一行一行地喂进来；而主机放进存储的张量很可能是 NCHW 或 NHWC，通道和空间的优先级跟阵列的喂法不一致。直接把存储数据灌进 PE 阵列，会让相邻 PE 拿到的数据在逻辑上八竿子打不着，白白浪费了「相邻数据复用」的机会。

`Data_Reorder` 的解决办法是：用一个 2 位的 `data_format` 选择信号，在三种排布之间切换：

- `2'b00`：**NCHW**（通道优先）。
- `2'b01`：**NHWC**（空间优先）。
- `2'b10`：**Blocked**（已经分块，直接透传）。

`data_format` 共 2 位，能表示 4 个值（0~3），但源码只用了前三个，`2'b11` 未被处理——这一点我们在第 5 节的综合实践里会用到（正好可以塞进一个新格式 CHWN）。

#### 4.1.2 核心流程

格式选择的执行流程：

1. 每个时钟沿，`case(data_format)` 根据 `data_format` 的值选一条分支。
2. 命中 NCHW 或 NHWC 时，把 `mem_data` 的若干位段重排后写入 `reorder_buffer`。
3. 命中 Blocked 时，把 `mem_data` **原样**送到 `pe_data`（因为已经分好块，无需重排）。
4. 另一个 `always` 块负责把 `reorder_buffer` 里的数据按 `read_ptr` 顺序搬到 `pe_data`。

写成伪代码：

```text
每个时钟沿:
    若 data_format == NCHW:    把 mem_data 的位段按「通道优先」拼进 reorder_buffer
    若 data_format == NHWC:    把 mem_data 的位段按「空间优先」拼进 reorder_buffer
    若 data_format == Blocked: pe_data ← mem_data  // 直通
    // 下列「输出调度」无条件执行（见 4.4）
    pe_data   ← reorder_buffer[read_ptr]
    read_ptr  ← 回绕自增
```

#### 4.1.3 源码精读

[data_reorder.v:5-9](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L5-L9) — 端口与格式选择信号：

```verilog
input wire [127:0] mem_data,
output reg [127:0] pe_data,
input wire [1:0] data_format  // 0:NCHW 1:NHWC 2:Blocked
```

- `mem_data`：来自上游（设计上应是存储控制器）的输入数据，**128 位**。
- `pe_data`：重排后送给 PE 阵列的输出，也是 128 位。
- `data_format`：2 位选择信号，注释写明三个值的含义。

> **一个跨模块的待确认点**：上一讲 `Memory_Controller` 的 `npu_rd_data` 是 **256 位**（`DATA_WIDTH=256`），而这里的 `mem_data` 只有 **128 位**。两者位宽对不上，说明 `Data_Reorder` 当前**并没有真正连在 `mem_ctl.v` 后面**——它更像一个独立写好的骨架模块，等着将来接线。这一点和整个第 3 单元「模块各自成形、尚未互连」的整体印象一致。

#### 4.1.4 代码实践

**目标**：建立「同一份数据、不同排布」的直觉，不碰源码。

1. 设想一张 2×2 像素、3 通道的小特征图，把它的 12 个数（3 通道 × 2 × 2）按 NCHW 和 NHWC 两种顺序各写成一维数组。
2. NCHW 顺序应为：先通道 0 的 4 个像素，再通道 1 的 4 个，再通道 2 的 4 个。
3. NHWC 顺序应为：先像素 (0,0) 的 3 个通道，再像素 (0,1)，依此类推。
4. **需要观察的现象**：同一组数，两种排布下「数组里相邻的两个数」关系完全不同——NCHW 里相邻的是同通道的相邻像素，NHWC 里相邻的是同像素的相邻通道。
5. **预期结果**：理解为什么 PE 阵列按行/列喂数据时，需要先把 NCHW/NHWC「打散重排」。这是纯纸面练习，运行结果无需本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`data_format` 是 2 位信号，能编码几个值？源码用了几个？剩哪个没用？

**答案**：2 位能编码 4 个值（`2'b00`~`2'b11`）。源码用了前 3 个（NCHW、NHWC、Blocked），`2'b11`（即 3）未被 `case` 处理。这恰好给「新增第 4 种格式」留了位置，第 5 节会用到。

**练习 2**：为什么 Blocked 分支可以直接把 `mem_data` 透传给 `pe_data`，而 NCHW/NHWC 不行？

**答案**：Blocked 表示数据**已经按 PE 阵列想要的分块方式排好了**，存储里的摆法和下游计算需求一致，所以无需搬运重排，直接传即可。NCHW/NHWC 的摆法和下游不一致，必须先重排。

---

### 4.2 reorder_buffer：二维重排缓冲

#### 4.2.1 概念说明

重排不是「来一个、改一个、立刻送走」那么简单。计算单元常常需要**一整块**数据凑齐后才能开算（比如 PE 阵列要一整行权重、卷积引擎要一个 3×3 窗口）。所以 `Data_Reorder` 用了一个**缓冲（buffer）**：先把重排结果攒在 `reorder_buffer` 里，攒够了再连续不断地往外送。这和 u2-l2 卷积引擎里的 `window_buffer`、u3-l1 存储控制器里的 `memory_bank` 是同一类思路——**用一块小存储换取数据供给的连续性**。

这个缓冲被组织成**二维数组**：

- 第一维大小：`CHANNELS/TILE_SIZE`（通道数除以块大小，默认 `64/8 = 8`）。
- 第二维大小：`TILE_SIZE`（默认 `8`）。
- 每个元素：128 位。

于是 `reorder_buffer[i][j]` 就是「第 i 块、块内第 j 个字」，整张缓冲是 8×8 = 64 个 128 位字。

#### 4.2.2 核心流程

缓冲的工作分两段：

1. **写入（重排填充）**：第一个 `always` 块按 `data_format` 把 `mem_data` 的位段写到 `reorder_buffer` 的某些位置。
2. **读出（流式输出）**：第二个 `always` 块用 `read_ptr` 每个周期读出一个字送到 `pe_data`，读完后 `read_ptr` 自增并在到达末尾时回绕到 0，形成循环输出。

整块缓冲的总容量可由参数算出：

\[
\text{容量} = \frac{\text{CHANNELS}}{\text{TILE\_SIZE}} \times \text{TILE\_SIZE} \times 128 \text{ bit}
\]

代入默认参数：

\[
8 \times 8 \times 128\text{ bit} = 8192\text{ bit} = 1024\text{ B} = 1\text{ KiB}
\]

#### 4.2.3 源码精读

[data_reorder.v:12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L12) — 声明二维重排缓冲：

```verilog
reg [127:0] reorder_buffer [0:CHANNELS/TILE_SIZE-1][0:TILE_SIZE-1];
```

逐段拆：

- `reg [127:0]`：每个字 128 位。
- `[0:CHANNELS/TILE_SIZE-1]`：第一维，默认 `0..7`（8 块）。
- `[0:TILE_SIZE-1]`：第二维，默认 `0..7`（每块 8 字）。

> **维护隐患（与 u3-l1 同款）**：第一维大小用 `CHANNELS/TILE_SIZE` 做整数除法。只有当 `CHANNELS` 是 `TILE_SIZE` 的整数倍时，缓冲才正好装下全部通道；否则尾部的零头通道会被**静默丢弃**。例如 `CHANNELS=60, TILE_SIZE=8` 时，`60/8=7`，缓冲只覆盖 56 个通道，多出的 4 个通道无处安放。真实设计里要么断言整除，要么补齐，本模块没有处理。

#### 4.2.4 代码实践

**目标**：手算不同参数下缓冲的形状，体会参数化。

1. 默认参数 `CHANNELS=64, TILE_SIZE=8`：缓冲 8×8，共 64 字、1024 B（已算过）。
2. 在脑中（不改源码）把 `TILE_SIZE` 改成 16、`CHANNELS` 保持 64：第一维变为 `64/16=4`，缓冲变成 4×16，总字数仍是 64，容量仍是 1024 B。
3. 再把 `CHANNELS` 改成 128、`TILE_SIZE=8`：第一维 `128/8=16`，缓冲 16×8 = 128 字 = 2048 B。
4. **需要观察的现象**：总字数始终等于 `CHANNELS`（因为两维相乘 = `CHANNELS/TILE_SIZE × TILE_SIZE = CHANNELS`，前提是整除），改 `TILE_SIZE` 只改变「长宽比」不改总容量。
5. **预期结果**：理解 `CHANNELS/TILE_SIZE × TILE_SIZE` 这对维度的乘积恒等于通道数。整除前提下的这一守恒关系，运行结果**待本地验证**（仓库无仿真脚手架）。

#### 4.2.5 小练习与答案

**练习 1**：`CHANNELS=64, TILE_SIZE=8` 时，`reorder_buffer` 总共能存多少比特？合多少字节？

**答案**：`8 × 8 × 128 = 8192` 比特 = 1024 字节 = 1 KiB。

**练习 2**：如果 `CHANNELS=66, TILE_SIZE=8`，缓冲第一维是几？会有什么问题？

**答案**：`66/8 = 8`（整数除法舍去小数），第一维仍是 8，缓冲只覆盖 `8×8 = 64` 个通道。剩下 2 个通道（第 64、65 号）没有位置存放，被静默丢弃。这说明该参数化要求通道数能被块大小整除，否则丢数据。

---

### 4.3 NCHW 与 NHWC 的位重排：通道优先 vs 空间优先

#### 4.3.1 概念说明

这是本讲最核心的一段。两种排布的差异，最终落在**从 `mem_data` 的哪些位段取数**上。我们可以把 128 位的 `mem_data` 想象成一长条数据，里面按某种粒度排着一串「小格子」：

- NCHW 分支按 **16 位（半字）** 粒度取数，对应注释里的「通道优先」。直觉是：通道数据在 NCHW 里相隔较远，要**跨越空间**把同属一组通道的半字「聚」到一起。
- NHWC 分支按 **8 位（字节）** 粒度取数，对应注释里的「空间优先」。直觉是：NHWC 里通道在同一像素内是连续的，但跨像素的空间采样要按字节步长抽取。

两条分支的共同点是：它们都**不是把 `mem_data` 整条搬过去**，而是只挑出若干小段重新拼接。这是一种典型的 **bit-level gather（位级聚集）**。

#### 4.3.2 核心流程

**NCHW 分支**（通道优先）：

1. 外层 `for c`：以 `TILE_SIZE` 为步长遍历通道，共 `CHANNELS/TILE_SIZE = 8` 个块。
2. 内层 `for t`：在每块内遍历 `TILE_SIZE = 8` 个槽位。
3. 每次写入：从 `mem_data` 抽 4 段 16 位（共 64 位），拼成一个新字，写到 `reorder_buffer[c/TILE_SIZE][t]`。

抽取的 4 段是：

\[
\text{mem\_data}[127{:}112],\ \text{mem\_data}[95{:}80],\ \text{mem\_data}[63{:}48],\ \text{mem\_data}[31{:}16]
\]

注意这 4 段在 `mem_data` 里**并不连续**——它们每隔一个 16 位取一段（落在第 7、5、3、1 号半字上，从最低位 0 开始数）。

**NHWC 分支**（空间优先）：

1. 单层 `for h`：遍历 `TILE_SIZE = 8` 次。
2. 每次写入：从 `mem_data` 抽 4 段 8 位（共 32 位），拼成新字，写到 `reorder_buffer[h%4][h/4]`。

抽取的 4 段是：

\[
\text{mem\_data}[127{:}120],\ \text{mem\_data}[95{:}88],\ \text{mem\_data}[63{:}56],\ \text{mem\_data}[31{:}24]
\]

这 4 段是每隔 4 个字节取一个字节（落在第 15、11、7、3 号字节上）。

#### 4.3.3 源码精读

[data_reorder.v:17-24](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L17-L24) — NCHW 分支：

```verilog
2'b00: // NCHW -> PE阵列格式
    for (int c = 0; c < CHANNELS; c += TILE_SIZE) begin
        for (int t = 0; t < TILE_SIZE; t++) begin
            reorder_buffer[c/TILE_SIZE][t] <=
                {mem_data[127:112], mem_data[95:80],  // 通道优先
                 mem_data[63:48],  mem_data[31:16]};
        end
    end
```

[data_reorder.v:26-31](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L26-L31) — NHWC 分支：

```verilog
2'b01: // NHWC -> PE阵列格式
    for (int h = 0; h < TILE_SIZE; h++) begin
        reorder_buffer[h%4][h/4] <=
            {mem_data[127:120], mem_data[95:88],  // 空间优先
             mem_data[63:56],  mem_data[31:24]};
    end
```

对比两条分支，可以读出三个关键差异：

- **取数粒度不同**：NCHW 取 16 位段（4 段 = 64 位），NHWC 取 8 位段（4 段 = 32 位）。
- **段的位置不同**：NCHW 落在半字 7/5/3/1，NHWC 落在字节 15/11/7/3——步长不同，体现两种排布下「跨多远才能取到下一个相关数据」的差异。
- **写址计算不同**：NCHW 用规整的 `[c/TILE_SIZE][t]`（外层除、内层顺次）；NHWC 用 `[h%4][h/4]`，把一个一维循环变量 `h` 用「模 4」和「除 4」拆成二维下标，等价于按列优先的顺序填写 `4×2` 的一组位置。

读到这里必须如实标注几处**待确认/可疑**：

1. **拼接结果窄于目标字宽**：`reorder_buffer` 每个字是 128 位，但 NCHW 拼出的是 64 位、NHWC 是 32 位。赋值时高位会被**零扩展**（NCHW 高 64 位为 0、NHWC 高 96 位为 0），等于每个字有一半甚至四分之三的位永远是 0，浪费了缓冲位宽。这要么是简化骨架，要么意在「只用低位」，**待确认**。
2. **NCHW 内层循环写了相同的值**：内层 `for t` 的循环体里，右值 `{mem_data[...]}` **完全不依赖 `t`**，于是同一个时钟沿里把**同一个值**写到 `reorder_buffer[c/TILE_SIZE][0..7]` 共 8 个位置。由于非阻塞赋值且值相同，结果只是「8 个位置都是同一个字」，循环本身是冗余的。
3. **NHWC 循环同样写了相同的值**：右值也不依赖 `h`，把同一个 32 位的值广播写到 `reorder_buffer[0][0]..[3][0]..[3][1]` 共 8 个位置。
4. **没有 `default` 分支**：`data_format == 2'b11` 时，两条分支都不命中，`reorder_buffer` 保持原值不更新。

> 综合判断：这两条分支的**结构（取数粒度、步长、写址方式）传达了清晰的设计意图**——NCHW 通道优先聚半字、NHWC 空间优先取字节——但**循环右值不随循环变量变化、位宽不匹配**这两点说明它尚未达到「逐周期搬一份新数据」的可用状态，更像一份表达思路的骨架。教学上我们学它的**思路**，工程上要补全它的**逐拍取址**。

#### 4.3.4 代码实践

**目标**：把「位段抽取」拆成一张对照表，看清 NCHW 与 NHWC 取的是哪些位。

1. 准备一张 128 位的 `mem_data`，标出每 16 位的半字编号（半字 0 = `[15:0]`，半字 1 = `[31:16]`，…，半字 7 = `[127:112]`）。
2. NCHW 抽取 `[127:112]`、`[95:80]`、`[63:48]`、`[31:16]` → 半字 **7、5、3、1**。
3. 再按每 8 位标出字节编号（字节 0 = `[7:0]`，…，字节 15 = `[127:120]`）。NHWC 抽取 `[127:120]`、`[95:88]`、`[63:56]`、`[31:24]` → 字节 **15、11、7、3**。
4. **需要观察的现象**：NCHW 取的是「奇数号半字」，NHWC 取的是「每隔 4 个字节取一个」——两种抽取的步长（16 位 vs 32 位）不同，正对应通道优先与空间优先下数据间距不同。
5. **预期结果**：得到一张「格式 → 取数粒度 → 抽取位置」对照表，直观理解两条分支的差异。这是纸面分析，**待本地验证**指实际重排结果（需自建 testbench 给定 `mem_data` 观察 `reorder_buffer`）。

#### 4.3.5 小练习与答案

**练习 1**：NCHW 分支拼出的字是多少位？赋给 128 位的 `reorder_buffer[c/TILE_SIZE][t]` 后，高几位是什么？

**答案**：4 段 16 位 = 64 位。赋给 128 位目标时高 64 位被零扩展为 0。即每个缓冲字实际只有低 64 位有效。

**练习 2**：NHWC 分支里 `reorder_buffer[h%4][h/4]`，当 `h` 从 0 到 7 时，依次写到哪 8 个位置？

**答案**：
- `h=0`：`[0][0]`
- `h=1`：`[1][0]`
- `h=2`：`[2][0]`
- `h=3`：`[3][0]`
- `h=4`：`[0][1]`
- `h=5`：`[1][1]`
- `h=6`：`[2][1]`
- `h=7`：`[3][1]`

可见它是按「列优先」填写一个 4 行 2 列的区域，第一维（行）= `h%4`，第二维（列）= `h/4`。

---

### 4.4 Blocked 透传与输出调度：read_ptr（待确认）

#### 4.4.1 概念说明

前面三条分支负责「把数据整形成什么样子」，而**输出调度**负责「整形好的数据按什么顺序送出去」。`Data_Reorder` 的第二个 `always` 块干的就是这件事：用一个读指针 `read_ptr`，每个周期从 `reorder_buffer` 里取一个字送到 `pe_data`，取完一轮就回绕，形成**循环流式输出**。这种「先攒一块、再连续吐」的模式，正好能匹配 PE 阵列持续不断吃数据的需求。

Blocked 分支则走另一条捷径：既然数据已经分好块、无需重排，那就**跳过缓冲，直接把 `mem_data` 送到 `pe_data`**。这条「直通」路径的设计意图是省掉重排开销。

但正是在这第二个 `always` 块里，集中出现了本讲最关键的几处**待确认**问题，下面逐一指出。

#### 4.4.2 核心流程

理想的输出调度流程是：

1. 维护一个读指针 `read_ptr`，范围 `0` 到 `CHANNELS/TILE_SIZE-1`（默认 `0..7`）。
2. 每个时钟沿：`pe_data ← reorder_buffer[read_ptr]`。
3. `read_ptr` 自增；到达末尾（`== CHANNELS/TILE_SIZE-1`）时回绕到 0。

回绕逻辑是一个典型的**模计数器**：

\[
\text{read\_ptr}_{\text{next}} =
\begin{cases}
0, & \text{read\_ptr} = \frac{\text{CHANNELS}}{\text{TILE\_SIZE}} - 1 \\
\text{read\_ptr} + 1, & \text{否则}
\end{cases}
\]

#### 4.4.3 源码精读

[data_reorder.v:33-34](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L33-L34) — Blocked 分支直通：

```verilog
2'b10: // Blocked格式
    pe_data <= mem_data;  // 直接传递
```

[data_reorder.v:39-42](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L39-L42) — 输出调度（第二个 `always`）：

```verilog
always @(posedge clk) begin
    pe_data <= reorder_buffer[read_ptr];
    read_ptr <= (read_ptr == (CHANNELS/TILE_SIZE-1)) ? 0 : read_ptr + 1;
end
```

这两段是本讲**问题最集中**的地方，必须如实标注四个待确认点：

1. **`read_ptr` 从未声明（核心待确认）**：在第二个 `always` 里，`read_ptr` 既被读（`reorder_buffer[read_ptr]`、比较 `read_ptr == ...`）又被写（`read_ptr <= ...`），但整个文件从 `module` 到 `endmodule`，**没有任何一行 `reg ... read_ptr;` 的声明**。严格 Verilog 下，未声明的标识符会被推断为 1 位 `wire`（旧版）或直接报错（SystemVerilog），无论哪种都和「范围 0..7 的读指针」对不上。这是本讲规格明确点出的语法问题：`read_ptr` 未声明，**待确认**。
2. **`pe_data` 被两个 `always` 块同时驱动（多驱动）**：[data_reorder.v:34](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L34) 的 Blocked 分支写 `pe_data <= mem_data`，而 [data_reorder.v:40](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L40) 的第二个 `always` 又写 `pe_data <= reorder_buffer[read_ptr]`。`pe_data` 被两个 `always` 块赋值，构成**多驱动冲突（multiple drivers）**——综合工具会报错，仿真里结果不确定。尤其在 Blocked 模式下，两个驱动同时生效，输出无法预测。
3. **`reorder_buffer[read_ptr]` 维度不匹配**：`reorder_buffer` 是**二维**数组 `[0:7][0:7]`，要取一个 128 位的字必须给**两个**下标（如 `reorder_buffer[i][j]`）。这里只给了一个下标 `read_ptr`，相当于取了「一整行（8 个字）」，无法直接赋给单个 128 位的 `pe_data`。这要么是漏写了第二维下标，要么是把二维当一维用了——**待确认**。
4. **Blocked 模式下缓冲不更新、却仍被读**：Blocked 分支只写 `pe_data`、不写 `reorder_buffer`，但第二个 `always` 无条件地读 `reorder_buffer` 到 `pe_data`。于是在 Blocked 模式下，`pe_data` 同时被「`mem_data`」和「未被更新的旧缓冲」两路驱动，且缓冲内容停留在上一次 NCHW/NHWC 写入的残值（或上电的 `X`）。

> 综合判断：这两段代码传达的**设计意图很清楚**——Blocked 直通、其余格式经缓冲流式输出、读指针回绕计数。但**实现层面有四个未闭合的问题**（`read_ptr` 未声明、`pe_data` 多驱动、缓冲单下标读取、Blocked 下缓冲残值）。把它当成「表达思路的骨架」来读，并清楚知道它**当前不可综合、未联调**，是正确的阅读姿态。若要修复，最小动作至少包括：声明 `reg [...] read_ptr;`、给 `reorder_buffer` 读取补第二维下标、让 Blocked 与缓冲输出二选一（例如把第二个 `always` 的读出也放进 `case`，或加一个 `valid` 选通）。

#### 4.4.4 代码实践

**目标**：仅从源码出发，定位并解释本模块的待确认问题，不实际改源码。

1. 在 [data_reorder.v](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v) 里搜索 `read_ptr`，确认它**只在第 40、41 行出现**，没有任何 `reg`/`wire` 声明。
2. 数一数 `pe_data` 被几个 `always` 块赋值：第 34 行（Blocked 分支）和第 40 行（输出调度），共两处。
3. 检查 `reorder_buffer[read_ptr]` 的下标个数：缓冲是二维，这里只给了一个下标。
4. **需要观察的现象**：以上三处分别对应「未声明信号」「多驱动」「维度不匹配」三类问题。
5. **预期结果**：能口头说清每个问题的现象和最小修复方向（见上文「综合判断」）。由于仓库无仿真脚手架，实际仿真波形**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：要让输出调度的 `read_ptr` 合法，至少应该在哪一行补什么声明？

**答案**：在 [data_reorder.v:12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L12) `reorder_buffer` 声明附近，补一行声明，例如 `reg [$clog2(CHANNELS/TILE_SIZE)-1:0] read_ptr;`（默认范围 0..7，需 3 位）。同时还应给 `read_ptr` 加复位初值（如 `if (!rst_n) read_ptr <= 0;`），否则上电指针未知。

**练习 2**：为什么 Blocked 模式下 `pe_data` 的输出不可预测？

**答案**：Blocked 分支写 `pe_data <= mem_data`，而第二个 `always` 无条件写 `pe_data <= reorder_buffer[read_ptr]`，两个 `always` 块在同一时钟沿驱动同一个 `reg`，构成多驱动冲突。综合会报错，仿真结果不确定，故输出不可预测。修复思路是让两者互斥（如把读出也纳入 `case`，仅在非 Blocked 时读缓冲）。

**练习 3**：`reorder_buffer[read_ptr]` 想取出一个 128 位的字，正确写法应该是什么？

**答案**：`reorder_buffer` 是二维数组，取单个字需要两个下标，应写成 `reorder_buffer[read_ptr][某个第二维下标]`。当前只给一个下标取到的是「一整行」，无法赋给 128 位的 `pe_data`。具体第二维如何递增，需结合输出调度设计**待确认**。

---

## 5. 综合实践

本实践是本讲规格要求的核心任务：**为 `data_format` 增加第 4 种排布 CHWN，写出对应的位重排赋值，并说明它适合什么计算模式**。这个任务正好用上前文发现的「`2'b11` 未被 `case` 处理」的空位，把一个待确认的漏洞变成一个可动手的练习。

### 5.1 CHWN 是什么，适合哪种计算

**CHWN** 把四个轴排成 channel → height → width → batch 的顺序：通道在最外，batch 在最内。它的特点是「**先把一个通道在所有空间位置上的数据连续放完，再放下一个通道**」。这种排布适合**逐通道流式处理**的计算模式——例如权重驻留型脉动阵列（u2-l1）在处理某一组输出通道时，会连续不断地吃入同一通道的整片空间激活，CHWN 让「同一通道的数据」在内存里相邻，能最大化顺序读取带宽。一些早期框架（Darknet 等）和按通道并行的推理引擎里能见到它的身影。

### 5.2 在 case 中新增 CHWN 分支（示例代码）

下面是**示例代码**（不是仓库原有代码，仅作练习参考，不要写入源码），展示如何补一个 `2'b11` 分支：

```verilog
// 示例代码：CHWN -> PE阵列格式（逐通道流式）
2'b11: // CHWN
    for (int c = 0; c < CHANNELS; c += TILE_SIZE) begin
        // 按通道聚集：把同一通道在相邻空间位置的位段聚到一起
        reorder_buffer[c/TILE_SIZE][0] <=
            {mem_data[119:112], mem_data[87:80],   // 取同通道的相邻空间采样
             mem_data[55:48],  mem_data[23:16]};
    end
```

说明这版示例的设计取舍：

- **填满 `2'b11` 这个空缺**：让 `case` 覆盖全部 4 个值，消除「未处理」分支。
- **沿用「位级聚集」的风格**：和 NCHW/NHWC 一样从 `mem_data` 抽 4 段拼成新字，但抽的位置按「同一通道的相邻空间采样」来取（此处取 8 位段，模拟逐字节取同通道相邻像素）。
- **只写第二维下标 `[0]`**：避免 4.3 节里「内层循环写相同值」的冗余，明确表达「每个块先填第一个字」。

### 5.3 你需要做的

1. 阅读现有 [data_reorder.v:16-35](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L16-L35) 的三条分支，确认 `2'b11` 未被处理。
2. 决定 CHWN 分支取数的粒度（8 位？16 位？）和抽取位置，并写出对应的拼接表达式（可参考 5.2 的示例）。
3. 口头说明：CHWN 把通道放最外，所以它适合「**一次喂完一个通道**」的逐通道流式计算，能和权重驻留型 PE 阵列配合。
4. 反思：补完 CHWN 后，`data_format` 仍是 2 位、4 个值刚好用满；若将来还要加第 5 种格式，就得扩宽 `data_format` 的位宽——这是一个值得记的扩展点。
5. 本仓库无仿真脚手架，运行波形**待本地验证**（需自建 testbench，给定 `mem_data` 与 `data_format=2'b11`，观察 `reorder_buffer` 的写入）。

---

## 6. 本讲小结

- `Data_Reorder` 用一个 2 位 `data_format` 在三种排布间切换：`2'b00` NCHW（通道优先）、`2'b01` NHWC（空间优先）、`2'b10` Blocked（透传），`2'b11` 未处理（[data_reorder.v:9](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L9)）。
- 重排结果先存进二维缓冲 `reorder_buffer[CHANNELS/TILE_SIZE][TILE_SIZE]`，默认 8×8 共 1 KiB，先攒齐再流式输出（[data_reorder.v:12](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L12)）。
- NCHW 分支按 16 位半字粒度、取第 7/5/3/1 号半字（通道优先）；NHWC 按 8 位字节粒度、取第 15/11/7/3 号字节（空间优先）——两条分支传达了清晰的设计意图（[data_reorder.v:17-31](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L17-L31)）。
- Blocked 分支把 `mem_data` 直接透传给 `pe_data`，跳过缓冲，省去重排开销（[data_reorder.v:33-34](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L33-L34)）。
- 四处**待确认**必须记牢：① `read_ptr` 从未声明（[data_reorder.v:40-41](https://github.com/Lumen-Laboratory/Mayoiuta/blob/100706ee7dbee8ff5b05d0ca1fcecc887b38187a/hardware/rtl/memory/data_reorder.v#L40-L41)）；② `pe_data` 被两个 `always` 块多驱动；③ `reorder_buffer[read_ptr]` 只给一个下标、维度不匹配；④ NCHW/NHWC 循环右值不随循环变量变化、且拼出的位宽（64/32 位）窄于 128 位目标字。
- 跨模块看：本模块 128 位 `mem_data` 与 `mem_ctl.v` 的 256 位 `npu_rd_data` 位宽对不上，说明它当前并未真正接在存储控制器之后，仍是独立骨架。

---

## 7. 下一步学习建议

至此，第 3 单元（存储与数据调度）已覆盖存储控制器（u3-l1）和数据重排（u3-l2，本讲）。数据从存储出来、经过重排整形后，下一步要面对的问题是：**如果输入形状不固定（不同卷积核、不同步长、不同 padding），如何动态算出输出该有多大、要不要补零？** 这正是下一讲 **u3-l3 动态形状适配 Shape Adaptor** 的主题——它会讲 `shape_adaptor.v` 如何根据输入尺寸、卷积核、STRIDE/PAD 实时计算输出尺寸并做边界保护。建议接着阅读 `hardware/rtl/control/shape_adaptor.v`，并注意它和 u2-l2（卷积引擎）在「输出尺寸公式」上是同一件事的硬件/参数两面。

如果你更想从「整条数据通路」的视角把这些模块串起来，可以跳到 **u4-l3 全系统数据通路与集成**——那里会把 SoC → 存储 → 重排 → 计算 → 写回画成一张端到端的框图，并标出哪些环节已实现、哪些待确认（包括本讲的 `Data_Reorder` 与存储控制器位宽不匹配这一处断点）。
