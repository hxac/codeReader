# u4-l1 测试数据加载 data2sram 与三批矩阵拼接

## 1. 本讲目标

本讲聚焦 testbench 里最"烧脑"的一段预处理代码：`data2sram` 任务。它发生在 `tpu_start` 拉高**之前**，作用是把磁盘上的三批 8×8 矩阵"摆好姿势"塞进四块读 SRAM（a0/a1/b0/b1），让脉动阵列一启动就能算出三个正确的矩阵乘。

学完本讲你应该能够：

1. 说清 `mat1` / `mat2`（24 行）如何对应三批 8×8 矩阵，以及它们被水平拼接进 `tmp_c_mat` 的位移过程。
2. 看懂 `i % 4` 字节级错位（0 / 8 / 16 / 24 bit 偏移）的代码，并解释它**为什么等于在时间轴上给每路输入制造 0/1/2/3 拍延迟**。
3. 理解 `char2sram` 如何把 4 个 8bit 字节打包成一个 32bit SRAM 字，以及"绕过时钟"的预载后门含义。
4. 能跟踪一个具体的字节，从 `mat1` 一路追到某块 SRAM 的某个地址，画出完整的位变换路径。

## 2. 前置知识

- **testbench 的角色**：本项目的 testbench（`Pre-Synthesis_Simulation/test_tpu.v`）既是激励发生器，又是"数据搬运工"。它要在启动 TPU 前把输入矩阵预填进 SRAM，算完后再把输出 SRAM 与 golden 逐地址比对（详见 u1-l5）。
- **脉动阵列的输入歪斜（skew）**：阵列要算对矩阵乘，各路输入不能同时到达，而要按反对角线**错拍**进入。u2-l1 讲过，移位队列本身会制造歪斜；本讲要讲的是，**testbench 在 SRAM 布局阶段就预先烤好了另一层歪斜**，二者叠加才能对齐。
- **SRAM 顺序读 = 时间轴**：addr_sel 每个时钟把读地址 `addr_serial_num` 加 1（详见 u3-l2），所以 SRAM 的"地址序号"天然就是"时钟节拍"。**把某路数据整体往后挪 1 个地址，就等于让它晚 1 拍到达阵列。** 这是理解 `i % 4` 的钥匙。
- **位宽约定**（与 u1-l4 一致）：`DATA_WIDTH=8`、`ARRAY_SIZE=8`、`SRAM_DATA_WIDTH=32`、`OUT_DATA_WIDTH=16`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [Pre-Synthesis_Simulation/test_tpu.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v) | 仿真顶层 | `data2sram` 任务、数据结构声明、`$readmemb` 加载 |
| [Pre-Synthesis_Simulation/sram_256x32b.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/sram_256x32b.v) | 32bit 宽 SRAM 行为模型 | 内含 `char2sram` 任务（注意：文件名是 `sram_256x32b.v`，但里面 `module` 名是 `sram_128x32b`） |
| [Pre-Synthesis_Simulation/systolic.v](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/systolic.v) | 8×8 脉动阵列本体 | 仅引用 2 行，确认 SRAM 字节→阵列列的映射（详见 u2-l1） |
| data/mat1.txt、mat2.txt；golden/golden1~3.txt | 测试数据 | 24 行×64bit 输入；8 行×128bit 期望输出 |

> 提示：testbench 里实际例化的模块名是 `sram_128x32b`，但仓库里**没有** `sram_128x32b.v` 这个文件——它的定义藏在 `sram_256x32b.v` 里（文件第 2 行 `module sram_128x32b`）。仿真时要把 `sram_256x32b.v` 一起编译进去，否则会报找不到模块。这是本目录一个有名的"坑"。

## 4. 核心概念与源码讲解

本讲按 `data2sram` 的三个最小模块拆分：**任务全貌与数据结构** → **i%4 字节级错位** → **char2sram 打包写入**。

### 4.1 data2sram 任务全貌与三批矩阵的数据结构

#### 4.1.1 概念说明

`data2sram` 是一个 Verilog `task`（任务），在主流程 `initial` 块里、`tpu_start` 拉高之前被调用（[test_tpu.v:L253](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L253)）。它要解决的问题是：

- 磁盘上的 `mat1.txt` / `mat2.txt` 各有 **24 行**，每行 64bit（即 8 个字节），代表**三批 8×8 矩阵**叠在一起（24 = 3 批 × 8 行）。
- 但 TPU 一次仿真要**连算三批**矩阵乘（对应输出 c0/c1/c2 三组 SRAM），所以 testbench 必须把三批输入"排成一列长流水"，让阵列像吃流水线一样依次吃掉。
- 最终写入的是 4 块 32bit 宽的读 SRAM（a0/a1 装 weight，b0/b1 装 data），每块 128 个深度。

为此 testbench 用了三层临时数组做"渐进式整形"：`mat` → `tmp_c_mat` → `tmp_mat` → SRAM。

#### 4.1.2 核心流程

`data2sram` 内部分四步：

1. **清零**：把 `tmp_c_mat1/2`、`tmp_mat1/2` 清零。
2. **水平拼接**：把三批矩阵按"批次"维度水平拼到 `tmp_c_mat`（每路 192bit = 3 批 × 64bit）。
3. **字节级错位**：按 `i % 4` 把每路数据在 216bit 字段里左移 0/1/2/3 字节，得到 `tmp_mat`（见 4.2）。
4. **打包写入**：按地址遍历，用 `char2sram` 把 4 路字节打包成 32bit 写进 a0/a1/b0/b1（见 4.3）。

数据结构尺寸一览（`ARRAY_SIZE=8, DATA_WIDTH=8`）：

| 数组 | 元素位宽 | 元素个数 | 含义 |
|------|---------|---------|------|
| `mat1` / `mat2` | `ARRAY_SIZE*DATA_WIDTH` = 64bit | `ARRAY_SIZE*3` = 24 | 三批矩阵，每元素=1 行(8 字节) |
| `tmp_c_mat1/2` | `ARRAY_SIZE*3*DATA_WIDTH` = 192bit | `ARRAY_SIZE` = 8 | 水平拼好，每元素=1 路×3 批 |
| `tmp_mat1/2` | `(ARRAY_SIZE*3+3)*DATA_WIDTH` = 216bit | `ARRAY_SIZE` = 8 | 错位后，多出 3 字节(24bit)用于歪斜填充 |

#### 4.1.3 源码精读

数据结构与文件加载——`$readmemb` 把二进制文本文件按行灌进数组（[test_tpu.v:L220-L225](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L220-L225)、[L245-L249](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L245-L249)）：

```verilog
reg [ARRAY_SIZE*DATA_WIDTH-1:0]       mat1[0:ARRAY_SIZE*3-1];   // 24×64bit
reg [ARRAY_SIZE*3*DATA_WIDTH-1:0]     tmp_c_mat1[0:ARRAY_SIZE-1]; // 8×192bit
reg [(ARRAY_SIZE*3+3)*DATA_WIDTH-1:0] tmp_mat1[0:ARRAY_SIZE-1];  // 8×216bit
...
$readmemb("data/mat1.txt", mat1);   // 24 行二进制 → mat1[0..23]
```

`data2sram` 的清零与"三批水平拼接"（[test_tpu.v:L323-L336](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L323-L336)）：

```verilog
// ① 清零
for(i = 0; i< ARRAY_SIZE ; i = i + 1) begin
    tmp_c_mat1[i] = 0; tmp_mat1[i] = 0; ...
end
// ② 三批水平拼接：外层 i 遍历 3 批，内层 j 遍历 8 路
for(i = 0; i< 3 ; i = i + 1) begin
    for(j = 0; j< ARRAY_SIZE; j = j+1) begin
        tmp_c_mat1[j] = {mat1[ARRAY_SIZE*i+j], tmp_c_mat1[j][(ARRAY_SIZE*3*DATA_WIDTH-1) -: 2*DATA_WIDTH*ARRAY_SIZE]};
    end
end
```

第二步的拼接是个**位移合并**。把 `tmp_c_mat1[j]`（192bit）代入 `ARRAY_SIZE=8`：

\[ \text{tmp\_c\_mat1}[j] \;=\; \{\,\text{mat1}[8i+j]\,[63{:}0],\;\; \text{tmp\_c\_mat1}[j][191{:}64]\,\} \]

也就是"把当前 192bit 的**高 128bit** 挪到低 128bit，把新的 64bit `mat1` 放到最高 64bit"。循环 3 次（i=0,1,2）后，`tmp_c_mat1[j]` 从高到低依次是：**batch2 → batch1 → batch0**：

| tmp_c_mat1[j] 位段 | 字节号 (LSB=0) | 内容 |
|--------------------|---------------|------|
| [63:0]   | 字节 0~7  | batch0 第 j 路的 8 字节（= mat1[j]） |
| [127:64] | 字节 8~15 | batch1 第 j 路（= mat1[8+j]） |
| [191:128]| 字节 16~23| batch2 第 j 路（= mat1[16+j]） |

> 注意：这里"第 j 路"指的是阵列的**第 j 列输入**，对应后面 `weight_queue[0][j]`（见 4.2.3 的 systolic.v 引用）。也就是说 `mat1[k]` 的下标 k 同时编码了"批次"和"列号"：`k = 批次*8 + 列号`。

#### 4.1.4 代码实践

**实践目标**：用眼睛确认 `mat1.txt` 的 24 行确实划分成三批，并定位某一批某一路。

**操作步骤**：

1. 打开 `Pre-Synthesis_Simulation/mat1.txt`，数行数（应为 24）。
2. 第 1~8 行 = batch0 的第 0~7 路；第 9~16 行 = batch1；第 17~24 行 = batch2。
3. 任取第 1 行（batch0、第 0 路），它有 64 个二进制字符 = 8 字节；最低字节 = 该路在时间上最先喂入阵列的那个权重。

**预期结果**：24 行 × 64bit；行号 `r` 对应 `批次 = r / 8`（整除）、`路号 = r % 8`。

**待本地验证**：若手头有 VCS/Verdi，可在 `$readmemb` 后加一句 `$display("mat1[0]=%b", mat1[0]);`，与 txt 第 1 行对照确认加载顺序。

#### 4.1.5 小练习与答案

**Q1**：`mat1` 有 24 个元素，每个 64bit，如何对应"三批 8×8 矩阵"？
**A1**：`mat1[8*批次 + 路]`，每批 8 路、每路 8 字节（64bit）。即 `mat1[0..7]`=batch0、`[8..15]`=batch1、`[16..23]`=batch2。

**Q2**：为什么 `tmp_c_mat1` 每个元素是 192bit？
**A2**：\(3\text{ 批} \times 8\text{ 字节} \times 8\text{ bit} = 192\)。三批沿"时间/字节"方向水平拼到了同一路里。

---

### 4.2 i%4 字节级错位：把"时间歪斜"烧进 SRAM 布局

#### 4.2.1 概念说明

这是 `data2sram` 最关键、也最容易看错的一步。`tmp_c_mat` 已经把三批拼好了，为什么还要再做一次 `i % 4` 的字节移位？

答案是：**脉动阵列要求 8 路输入在时间上错拍到达**。而 SRAM 是"一个地址一拍"地顺序读出的（见 u3-l2 的 `addr_serial_num`）。于是 testbench 用了一个巧妙的等价变换——**把"时间上的延迟"翻译成"空间上的地址偏移"**：

- 第 0 路数据从地址 0 开始放 → 第 0 拍到达；
- 第 1 路数据整体往后挪 1 个字节（地址） → 第 1 拍到达；
- 第 2 路挪 2 字节 → 第 2 拍到达；
- 第 3 路挪 3 字节 → 第 3 拍到达。

这样，当阵列顺序读 SRAM 时，8 路输入就自动形成了 0/1/2/3 拍的歪斜波前。

> **为什么是 `% 4` 而不是 `% 8`？** 因为 8 路输入被分成了两组：**a0 服务第 0~3 路、a1 服务第 4~7 路**，两块 SRAM 在同一地址**同时**读出（各出 4 字节）。所以组内 4 路各自歪斜 0/1/2/3 即可，第 4 路的歪斜回到 0、与第 0 路对齐。这正是 `i % 4` 的由来。

#### 4.2.2 核心流程

对每一路 `i`（0~7），按 `i % 4` 把 192bit 的 `tmp_c_mat1[i]` 塞进 216bit 的 `tmp_mat1[i]` 里，左移 0/1/2/3 字节，空出来的位置补零：

| 路 i | i%4 | 拼接形式（左=高位） | 有效数据起始字节 | 含义 |
|------|-----|--------------------|----------------|------|
| 0, 4 | 0 | `{24'b0, c}`        | 字节 0  | 不偏移，第 0 拍到达 |
| 1, 5 | 1 | `{16'b0, c, 8'b0}`  | 字节 1  | 左移 1 字节，晚 1 拍 |
| 2, 6 | 2 | `{8'b0, c, 16'b0}`  | 字节 2  | 左移 2 字节，晚 2 拍 |
| 3, 7 | 3 | `{c, 24'b0}`        | 字节 3  | 左移 3 字节，晚 3 拍 |

（表中 `c` 代表 `tmp_c_mat1[i]`，192bit。）

注意 216bit = 192bit 数据 + 24bit（3 字节）填充，正好容纳最大 3 字节的偏移而不会把有效数据挤出字段。

#### 4.2.3 源码精读

错位 `case` 语句（[test_tpu.v:L337-L360](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L337-L360)）：

```verilog
for(i = 0; i< ARRAY_SIZE ; i = i + 1) begin
    case (i % 4)
        0 : tmp_mat1[i] = {24'b0,        tmp_c_mat1[i]          }; // 偏移 0
        1 : tmp_mat1[i] = {16'b0, tmp_c_mat1[i],  8'b0          }; // 偏移 1 字节
        2 : tmp_mat1[i] = { 8'b0, tmp_c_mat1[i], 16'b0          }; // 偏移 2 字节
        3 : tmp_mat1[i] = {      tmp_c_mat1[i], 24'b0           }; // 偏移 3 字节
        default: tmp_mat1[i] = 0;
    endcase
end
```

为什么这些"路"对应阵列的列？看 systolic.v 如何切 SRAM 字节（[systolic.v:L57-L58](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/systolic.v#L57-L58)）：

```verilog
weight_queue[0][i]   <= sram_rdata_w0[31-8*i-:8];  // i=0..3 → 列 0..3 取自 a0
weight_queue[0][i+4] <= sram_rdata_w1[31-8*i-:8];  // i=0..3 → 列 4..7 取自 a1
```

也就是说：`a0` 的最高字节 `[31:24]` 喂给**列 0**，`[23:16]` 喂给列 1，…，`[7:0]` 喂给列 3；`a1` 同理喂列 4~7。结合 4.3 的打包方式可知：**`tmp_mat1[k]` 的字节流就是第 k 列的输入流**。所以"`i % 4` 错位"= 给第 k 列的输入流整体延后 `k % 4` 拍——这就是它"模拟脉动阵列输入时序"的精确含义（阵列内部的移位歪斜见 u2-l1，二者叠加）。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：跟踪两个具体字节的位变换路径，亲眼看见"字节偏移 = 时钟延迟"。

**跟踪 A：第 0 路的第一个字节**（应第 0 拍到达列 0）

1. 起点：`mat1[0][7:0]`（batch0、第 0 路、最低字节）。
2. 水平拼接：`tmp_c_mat1[0][63:0] = mat1[0]`，所以该字节落在 `tmp_c_mat1[0][7:0]`。
3. 错位：`i=0 → i%4=0`，`tmp_mat1[0] = {24'b0, tmp_c_mat1[0]}`，故 `tmp_c_mat1[0][7:0]` 原位保留为 `tmp_mat1[0][7:0]`（字节 0）。
4. 打包：在 `char2sram` 地址 0 处，`a0[0][31:24] = tmp_mat1[0] 字节0`（见 4.3）。
5. 运行时：列 0 在第 0 拍读到 `a0[0][31:24]` = 该字节。✓ **0 偏移 → 0 拍延迟**。

**跟踪 B：第 3 路的第一个字节**（应第 3 拍到达列 3）

1. 起点：`mat1[3][7:0]`（batch0、第 3 路、最低字节）。
2. 水平拼接：落在 `tmp_c_mat1[3][7:0]`。
3. 错位：`i=3 → i%4=3`，`tmp_mat1[3] = {tmp_c_mat1[3], 24'b0}`，于是 `tmp_c_mat1[3][7:0]` 被顶到 `tmp_mat1[3][31:24]`（字节 3）。
4. 打包：在 `char2sram` **地址 3** 处，`a0[3][7:0] = tmp_mat1[3] 字节3` = 该字节。
5. 运行时：列 3 在**第 3 拍**才读到地址 3 的内容 = 该字节。✓ **3 字节偏移 → 3 拍延迟**。

**需要观察的现象**：在地址 0 处，列 3（`a0[0][7:0] = tmp_mat1[3] 字节0`）因为偏移 3 而读到 `24'b0` 里的 0，即一个**气泡（bubble）**；列 3 的真实数据要到地址 3 才出现。这种"先进来的路先有数据、后进来的路先吃气泡"正是脉动阵列波前的样子。

**预期结果**：路 0 的首字节在地址 0、路 3 的首字节在地址 3，二者相差 3 个地址 = 3 拍。

#### 4.2.5 小练习与答案

**Q1**：如果把 `i % 4` 改成 `i % 8`，会发生什么？
**A1**：第 4~7 路的偏移会变成 4/5/6/7 字节。但 a1 与 a0 是同地址并行读出的，第 4 路本应与第 0 路对齐（都在地址 0 开始），改成 `% 8` 后第 4 路被推迟 4 拍，破坏了 a0/a1 的并行对齐，阵列会算错。

**Q2**：填充位（`24'b0` 等）除了"占位"还有别的用处吗？
**A2**：有。它直接变成了阵列输入流头部的**零气泡**，正好对应三批矩阵"进入/稳态/离开"三种工况里的"进入"阶段——边界的空拍，和 u3-l2 里 addr_sel 用地址 127 填零是同一思想。

---

### 4.3 char2sram 调用：4 个 8bit 打包写入 32bit SRAM 字

#### 4.3.1 概念说明

`tmp_mat` 摆好后，要把这 8 路×27 字节的数据写进 32bit 宽的 SRAM。一块 32bit SRAM 一个地址只能存 4 字节，而我们有 8 路——所以**8 路被拆给两块 SRAM**：a0 存第 0~3 路，a1 存第 4~7 路。每个 32bit 字 = 同一时间步（同地址）下 4 路各取 1 字节拼成。

写入靠的是 `char2sram`——一个**仿真后门任务**：它直接 `mem[index] = char_in`，不走时钟、不走写使能，纯粹用来在仿真开始前预载 SRAM 内容。综合时不会有它（它住在 SRAM 行为模型里，不是设计 RTL）。

#### 4.3.2 核心流程

`data2sram` 第三步遍历 128 个地址：

- 地址 `0 ~ 26`（`i < ARRAY_SIZE*3+3 = 27`）：从 `tmp_mat1/2` 的 4 路里各取第 `i` 字节，拼成 32bit 写入。
- 地址 `27 ~ 127`：写 `32'b0`，用零填满 SRAM 尾部（对应输入流结束后的空拍）。

每个 32bit 字的摆位：**第 0 路在最高字节 `[31:24]`，第 3 路在最低字节 `[7:0]`**。这与 systolic.v 的切片 `w0[31-8*i-:8]` 严格对应——最高字节喂列 0。

#### 4.3.3 源码精读

打包写入主循环（[test_tpu.v:L362-L378](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L362-L378)）：

```verilog
for(i = 0; i < 128; i=i+1) begin
    if(i < (ARRAY_SIZE*3+3)) begin   // i < 27：有效数据
        // a0 装第 0~3 路：每路取第 i 字节，路0放最高字节
        sram_128x32b_a0.char2sram(i, {tmp_mat1[0][(DATA_WIDTH*(i+1)-1) -: DATA_WIDTH],
                                       tmp_mat1[1][(DATA_WIDTH*(i+1)-1) -: DATA_WIDTH],
                                       tmp_mat1[2][(DATA_WIDTH*(i+1)-1) -: DATA_WIDTH],
                                       tmp_mat1[3][(DATA_WIDTH*(i+1)-1) -: DATA_WIDTH]});
        // a1 装第 4~7 路（结构相同）
        sram_128x32b_a1.char2sram(i, {tmp_mat1[4][...], tmp_mat1[5][...],
                                       tmp_mat1[6][...], tmp_mat1[7][...]});
        // b0/b1 对 tmp_mat2 做完全一样的事（data 矩阵）
        ...
    end else begin                    // i >= 27：尾部填零
        sram_128x32b_a0.char2sram(i, 32'b0); ...
    end
end
```

其中 `tmp_mat1[k][(DATA_WIDTH*(i+1)-1) -: DATA_WIDTH]` = `tmp_mat1[k]` 的**第 i 字节**（变基部分选择，LSB 为字节 0）。

`char2sram` 任务本身（[sram_256x32b.v:L45-L52](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/sram_256x32b.v#L45-L52)）：

```verilog
task char2sram(input [31:0] index, input [31:0] char_in);
    mem[index] = char_in;   // 直接写存储阵列，绕过时钟/写使能
endtask
```

它和验证阶段直接读 `sram_16x128b_c0.mem[i]`（[test_tpu.v:L287](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L287)）是配套的——都依赖 SRAM 模型把 `mem` 暴露成可被 testbench 直接读写的数组。

#### 4.3.4 代码实践

**实践目标**：手算 `a0` 地址 0 的 32bit 内容，验证"路号→字节位"的摆位。

**操作步骤**：

1. 从 4.2.4 已知：`tmp_mat1[0] 字节0` = batch0 第 0 路最低字节（设其值为 `B0`）。
2. 同理 `tmp_mat1[1/2/3] 字节0`：注意路 1/2/3 因 `i%4` 偏移，它们的"字节 0"落在补零区，**值都是 0**（真实数据要到字节 1/2/3 才出现）。
3. 套拼装公式：`a0[0] = {B0, 0, 0, 0}`，即 `B0` 在 `[31:24]`，低 24bit 为 0。

**预期结果**：`a0[0] = {tmp_mat1[0]字节0, 0, 0, 0}`。对照 [test_tpu.v:L380-L382](https://github.com/abdelazeem201/Systolic-array-implementation-in-RTL-for-TPU/blob/6a93418faefac866eeab6349c8adfc91576099fb/Pre-Synthesis_Simulation/test_tpu.v#L380-L382) 的打印循环，地址 0 应只有最高字节非零、其余三字节为 0——这与"列 1/2/3 第 0 拍吃气泡"一致。

**待本地验证**：跑完 `data2sram` 后看打印的 `SRAM a0!!!!` 段，确认地址 0 形如 `B0 0 0 0`。

#### 4.3.5 小练习与答案

**Q1**：`char2sram` 为什么能"立即"写进去，不用等时钟沿？
**A1**：它是 `task`，体内直接 `mem[index] = char_in`，属于过程性赋值，是仿真专用的预载后门；SRAM 模型在运行时仍按 `posedge clk` 正常读写。

**Q2**：为什么地址 27~127 要写零？
**A2**：`tmp_mat` 有效数据只有 27 字节（3 批×8 字节 + 3 字节歪斜余量），超出部分用零填充，模拟输入流结束后的"空拍/离开"工况，避免 SRAM 里的随机初值污染阵列尾部计算。

---

## 5. 综合实践

**任务**：画出一条完整的"字节旅行"路线，把本讲三个模块串起来。

选取元素 **`mat1[0]` 的最低字节**（batch0、第 0 列、时间上最先喂入的那个权重），完成下表：

| 阶段 | 所在变量与位段 | 值/说明 |
|------|--------------|---------|
| 磁盘加载 | `mat1[0][7:0]` | 来自 mat1.txt 第 1 行最低 8 bit |
| ① 水平拼接 | `tmp_c_mat1[?][?:?]` | （自己填） |
| ② i%4 错位 | `tmp_mat1[?][?:?]` | （自己填，注意 `i%4=0` 时位置不变） |
| ③ char2sram 打包 | `a0` 地址 `?` 的 `[?:?]` | （自己填） |
| ④ 运行时读取 | 第 `?` 列、第 `?` 拍 | 由 systolic.v 切片决定 |

**进阶**：再选 **`mat1[7]` 的最低字节**（第 7 列、属于 a1）重做一遍，对比它与第 0 列字节"落在哪块 SRAM、第几拍到达"，用自己的话解释 `i % 4` 错位为什么能模拟脉动阵列的输入时序。

**参考答案要点**（第 0 列）：① `tmp_c_mat1[0][7:0]`；② `tmp_mat1[0][7:0]`（`i%4=0` 不偏移）；③ `a0` 地址 0 的 `[31:24]`；④ 第 0 列、第 0 拍。第 7 列：① `tmp_c_mat1[7][7:0]`；② `tmp_mat1[7][31:24]`（`i%4=3`，左移到字节 3）；③ `a1` 地址 3 的 `[7:0]`；④ 第 7 列、**第 3 拍**。两者相差 3 拍，正是 `i%4` 制造的时间歪斜。

## 6. 本讲小结

- `data2sram` 是 testbench 在 `tpu_start` 前的"数据整形"任务，把磁盘上的三批 8×8 矩阵摆进四块读 SRAM。
- 数据流经三层临时数组：`mat`(24×64bit) → 水平拼接 `tmp_c_mat`(8×192bit) → 字节错位 `tmp_mat`(8×216bit) → SRAM。
- **水平拼接**用"高 128bit 下移、新 64bit 置顶"的位移合并，把 batch2/batch1/batch0 沿字节方向铺到同一路。
- **`i % 4` 字节级错位**是核心：把每路数据左移 0/1/2/3 字节，等于在时间轴上延后 0/1/2/3 拍；用 `%4` 是因为 8 路分成 a0/a1 两组并行 SRAM。
- **`char2sram`** 把 4 路各 1 字节打包成 32bit 字（路 0 在最高字节），是绕过时钟的仿真预载后门；地址 27~127 用零填充尾部空拍。
- 三种工况（进入/稳态/离开）由"头部气泡 + 三批有效数据 + 尾部填零"共同模拟。

## 7. 下一步学习建议

- 下一讲 **u4-l2（golden 参考比对与结果验证流程）**接着读 testbench 的 `golden_transform` 任务与主流程的逐地址比对，看输出侧如何"反着"把反对角线结果重排回 golden 形式——与本讲"正着"把输入歪斜烧进 SRAM 形成镜像。
- 想深入"歪斜如何被阵列消费"，回看 **u2-l1（移位队列）** 与 **u3-l2（addr_sel 地址歪斜）**：本讲是"SRAM 布局级歪斜"，u2/u3 是"移位与地址级歪斜"，三者叠加才完整。
- 想验证自己的理解，可在本地用 VCS 编译 `Pre-Synthesis_Simulation/` 下全部 `.v`（务必包含 `sram_256x32b.v` 与 `sram_16x128b.v`），在 `data2sram` 末尾打断点观察 `sram_128x32b_a0.mem[0..3]` 的实际内容。
