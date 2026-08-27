# SpMV 案例研究：ELLPACK 分块、UNROLL 与 30 PE 部署

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 SpMV（稀疏矩阵-向量乘法）为什么是「内存系统受限」的内核，以及 ELLPACK 稀疏存储格式如何组织数据。
2. 逐行解释 `ellpack()` 计算函数中 `out[i] += nzval[j + i*L] * vec[cols[j + i*L]]` 的计算顺序，以及为什么要做**循环交换**（j 外层流水、i 内层展开）。
3. 说明 `buffer_load` / `buffer_compute` 如何用 x / y 两组片上数组加两条 flag 窗口，实现 tile 级乒乓双缓冲。
4. 解读 `load_nzval` / `load_cols` 从 256-bit 宽端口读入后，如何用 `range(31,0)` 逐段拆包成 float / int，并分发到 UNROLL_FACTOR 个计算通道。
5. 读懂 `spmv.ini` 如何用 `nk=30` 例化 30 个内核实例、铺满 4 条 DDR 通道，以及主机端 `cl_mem_ext_ptr_t` 与之一一对应的 bank 绑定。
6. 对照 baseline_30PE（30 PE × 32bit 窄端口）与 optimal_4PE（4 PE × 256bit 宽端口 × UNROLL 4），理解「PE 数 × 端口位宽 × 片上并行」三者之间的置换关系。

本讲是 u6 案例研究单元的第二篇。KNN（u6-l1、u6-l2）展示了「流归并 + 位宽/突发取舍」，本讲的 SpMV 则展示另一个极端：**用海量窄端口铺满内存通道**，以及如何用片上乒乓双缓冲把「装载」和「计算」解耦。

## 2. 前置知识

### 2.1 SpMV 与 ELLPACK 格式

SpMV 计算向量 \( y = A \cdot x \)，其中 \( A \) 是稀疏矩阵。它几乎不做什么「计算」——每个非零元素只参与一次乘加——却要搬运全部非零数据，是典型的**内存受限**内核，因此非常适合用来验收内存系统洞察（这正是 uBench 把它选为案例研究的原因，见 [case_study/SpMV/README.md:1-3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/README.md#L1-L3)，该 README 也说明算法源自 MachSuite 基准套件）。

ELLPACK 是一种「按行定长」的稀疏存储：**每行都固定存 L 个非零元素**（不足则补零），用两个平铺数组描述整个矩阵：

- `nzval[row * L + j]`：第 row 行第 j 个非零元素的值；
- `cols[row * L + j]`：该元素所在的列号。

于是每行的计算是：

\[ out[i] = \sum_{j=0}^{L-1} nzval[i \cdot L + j] \cdot vec[cols[i \cdot L + j]] \]

ELLPACK 的代价是「补零浪费」（本仓库的测试矩阵每行都填满 L 个随机列，见 4.4 节），收益是**访存地址完全规则**——连续行、连续列在内存里是连续存放的，天然适合宽端口 + 突发读取。对比 u4-l2 延迟微基准里「随机下标 + 依赖链」的最坏访问模式，SpMV 案例选了最好的一种。

注意一个例外：`vec[cols[...]]` 的下标来自数据，是**随机收集（gather）**。本设计把整个 vec（N 个 float）预取到片上 URAM，让这个随机访存发生在片内。

### 2.2 你需要带回的记忆

以下概念在前面讲义已建立，本讲直接使用：

- **m_axi 端口、bundle、max_read_burst_length**（u2-l1）：bundle 异名 = 独立 AXI 端口；突发长度是链接期参数。
- **理论峰值 = 频率 × 端口数 × 位宽 / 8**（u3-l2）：本讲四个变体都是 300 MHz（见各 Makefile 的 `--kernel_frequency 300`）。
- **sp / slr / nk 与主机 bank flag 的跨工具契约**（u3-l3、u6-l1）：ini 的 `sp=` 行必须与主机 `cl_mem_ext_ptr_t` 的 flags 逐端口对齐，CU 名由 `nk` 生成、编号从 1 开始。
- **NUM_ITERATIONS 放大执行时间**（u1-l4、u2-l3）：这里 `NUM_ITERATIONS = 5000`，让内核跑够久以稀释启动开销。
- **flag 窗口 + i%2 乒乓**（u6-l1）：KNN 用三级 flag 窗口做软件流水，本讲 SpMV 是同一手法的两级版。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [case_study/SpMV/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/README.md#L1-L3) | 案例背景：源自 MachSuite 的 SpMV，论文 Table 4 / Section 6 的四个设计 |
| [case_study/SpMV/baseline_30PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L7-L25) | 契约头：端口位宽、问题规模、tile 几何、UNROLL 因子，内核与主机共享 |
| [case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L5-L169) | HLS 内核：ellpack 计算、宽口装载、乒乓调度、顶层接口 |
| [case_study/SpMV/baseline_30PE/spmv.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L1-L195) | 连接配置：30 个 CU 的 SLR 放置与 4 bank 端口映射 |
| [case_study/SpMV/baseline_30PE/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L92-L354) | OpenCL 主机：数据生成、按 PE 切分、bank 绑定、启动与校验 |
| [case_study/SpMV/optimal_4PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_config.h#L7-L25) | 对照组：256bit 宽口 + 4 PE + UNROLL 4 的配置 |
| [case_study/SpMV/baseline_30PE/Makefile](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/Makefile#L66-L75) | `--kernel_frequency 300`、`--config ./spmv.ini`、可执行文件 `spmv` |

先记住 baseline_30PE 的几何参数（后面所有推导都基于它），一行行看 [krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L14-L25)：

| 宏 | 值 | 含义 |
|---|---|---|
| `NUM_KERNEL` | 30 | 并行内核（PE）实例数 |
| `N` | 8400 | 矩阵行数 = 向量长度 |
| `L` | 1024 | 每行非零元素数（ELLPACK 定长） |
| `N_OUT` | N/NUM_KERNEL = 280 | 每个 PE 负责的行数 |
| `ROWS_PER_TILE` | 8 | 每个 tile 的行数 |
| `NUM_TILES` | N_OUT/ROWS_PER_TILE = 35 | 每个 PE 要处理的 tile 数 |
| `UNROLL_FACTOR` | 1 | 每 PE 内部并行计算通道数 |
| `NUM_ITERATIONS` | 5000 | 整个 tile 扫描的重复次数（时间放大） |

位宽部分（[krnl_config.h:7-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L7-L12)）有一个**命名陷阱**：`INTERFACE_WIDTH_256` 与 `INTERFACE_WIDTH_512` 的名字是历史遗留——baseline 中 `DWIDTH_256 = 32`、`DWIDTH_512 = 32`，**两者实际都是 32-bit**。名字里的数字不代表当前宽度，宽窄完全由 `DWIDTH_*` 常量决定。这套宏在四个变体里的取值正是本讲的主角之一（见 4.4 节的对照表）。

## 4. 核心概念与源码讲解

### 4.1 ELLPACK 内核

#### 4.1.1 概念说明

`ellpack()` 是纯粹的**片上计算函数**：它的四个参数全都不是 DRAM 指针，而是已经装载到片上的局部数组。它解决的问题只有一个——**按什么顺序把 L×rows 个乘加做完，才能既保持浮点累加的正确性，又让硬件流水线打得满**。

朴素写法是「逐行」：外层遍历行 i，内层遍历列 j 累加。但这样内层循环对 `out[i]` 形成**循环携带依赖**（下一次加法必须等上一次加法完成），浮点加法的延迟有多个时钟周期，流水线会被这条串行链卡死。

#### 4.1.2 核心流程

`ellpack()` 采用**循环交换**：把列循环 `ellpack_2`（j）放到外层并加 `PIPELINE`，把行循环 `ellpack_1`（i）放到内层并完全 `UNROLL`：

```text
先清零:  for i in 0..rows-1:            out[i] = 0        （rows = ROWS_PER_TILE/UNROLL_FACTOR）
计算:    for j in 0..L-1:              ← PIPELINE（流水）
           for i in 0..rows-1:          ← UNROLL（完全展开）
             out[i] += nzval[j + i*L] * vec[cols[j + i*L]]
```

交换后的效果：流水线的**每一拍**同时更新 rows 个**互相独立**的累加器 `out[0..rows-1]`。单个累加器的递推仍然跨 j 拍存在，但因为同时有 rows 条并行的累加链，流水线可以在等待某条链的浮点结果期间去推进其他链——串行依赖被「宽度」摊平了。

每通道的数据量：

\[ \text{lane\_elements} = \frac{L \times ROWS\_PER\_TILE}{UNROLL\_FACTOR} \]

baseline 中 UNROLL_FACTOR=1，单通道即整块 tile：1024 × 8 = 8192 个 float。

#### 4.1.3 源码精读

清零循环初始化累加器（本 tile 的 out 在每次 `ellpack()` 调用时从头累加）：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:8-11](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L8-L11)——按 `ROWS_PER_TILE/UNROLL_FACTOR` 个元素把 `out[i]` 置 0。

核心双重循环与乘加语句：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:12-18](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L12-L18)。注意第 16 行就是学习目标里的那条语句：

```cpp
out[i] = out[i] + nzval[j + i*L] * vec[cols[j + i*L]];
```

- `#pragma HLS PIPELINE`（第 13 行）标在 j 循环上，未显式写 II，由工具求解；
- `#pragma HLS UNROLL`（第 15 行）完全展开 i 循环；
- `j + i*L` 正是 ELLPACK 平铺数组里元素 (行 i, 列 j) 的下标——i 是**通道内的相对行号**，L 是每行步长；
- `vec[cols[...]]` 是随机 gather，`vec` 指向该通道私有的 `local_vec` 副本（见 4.2.3），片上 URAM 随机读写。

调用方 `buffer_compute` 以 UNROLL 方式同时驱动 UNROLL_FACTOR 个通道，每个通道一份独立的 nzval/cols/vec/out 局部数组：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:24-28](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L24-L28)。UNROLL_FACTOR 份通道并行执行，这就是「单 PE 内部并行」的落点。

计算完成后把各通道的 `local_out` 写回片上汇总数组 `all_out`：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:29-34](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L29-L34)，第 q 通道的第 r 行写到 `out[q*ROWS_PER_TILE/UNROLL_FACTOR + r]`——通道按行块切分整块 tile。

#### 4.1.4 代码实践

**实践目标**：亲手验证 NUM_TILES 的推导链和 ELLPACK 下标的正确性。

**操作步骤**：

1. 手算三层数值链：`N_OUT = N/NUM_KERNEL = 8400/30 = 280`；`NUM_TILES = N_OUT/ROWS_PER_TILE = 280/8 = 35`；每通道元素数 `L*ROWS_PER_TILE/UNROLL_FACTOR = 1024*8/1 = 8192`。
2. 写一个 20 行的 Python 脚本（示例代码，非仓库原有）：用 `N=12, L=4, NUM_KERNEL=2, ROWS_PER_TILE=3, UNROLL_FACTOR=1` 缩小问题，按 host 的切分方式（行块连续切分）分数据，按 `ellpack()` 的 j-外 i-内顺序累加，与「逐行朴素顺序」的结果对比。
3. 回答：为什么 `N` 必须能被 `NUM_KERNEL × ROWS_PER_TILE` 整除？如果 `8400` 换成 `8401` 会发生什么？

**需要观察的现象**：两种累加顺序的浮点结果按位一致（每行的 j 累加顺序都是 0→L-1，与循环交换无关）；`NUM_TILES = N_OUT/ROWS_PER_TILE` 是整数除法，8401 会被截断成 34，**尾部 8 行被静默丢弃、结果错误且无任何报错**——这正是 N 取 8400（= 30×8×35）、变体取 8192（= 4×64×32）这种「可整除」数字的原因。

**预期结果**：手算值与 [krnl_config.h:22-25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L22-L25) 的宏展开完全一致；optimal_4PE 一侧 `8192/4/64 = 32`。

#### 4.1.5 小练习与答案

**练习 1**：`ellpack_1` 内层循环如果从 UNROLL 改成 PIPELINE，直觉上会发生什么？

**参考答案**：i 循环每拍只处理一行，同一拍内不再有 rows 个独立乘加并行；同时 j 循环的 PIPELINE 与内层 PIPELINE 会嵌套成两级流水，综合工具通常会把内层完全展开后再流水外层，或退化为 II 受浮点累加链延迟限制。总之丢失的正是「多个独立累加器摊平串行依赖」这一设计的核心收益。

**练习 2**：`ellpack()` 里为什么必须有第 8-11 行的清零循环？把它删掉、只在外层初始化一次行不行？

**参考答案**：`ellpack()` 每处理一个 tile 被调用一次，`local_out` 是复用的寄存器组；若只初始化一次，第二个 tile 的累加会叠在第一个 tile 的结果上，`all_out` 从第 2 块起全部错。清零循环保证每个 tile 的累加器从 0 出发。

**练习 3**：`vec[cols[j+i*L]]` 中的 `cols` 是 int 型列号。为什么这个随机索引不会拖垮 DRAM 带宽？

**参考答案**：因为 `vec` 不是 DRAM 数组——内核启动时整个向量已被预取进片上（4.2.3 的装载循环），随机 gather 发生在 URAM 里。DRAM 上读的 `cols` 本身是连续流。

### 4.2 乒乓双缓冲

#### 4.2.1 概念说明

SpMV 的数据流分两级：**装载**（从 DRAM 把 tile 的 nzval/cols 搬进片上）和**计算**（`ellpack()` 消费片上数据）。如果串行执行——装一块、算一块、再装下一块——内存端口在计算期间空闲、计算单元在装载期间空闲，双方利用率都减半。

乒乓（ping-pong）双缓冲用 **x / y 两组片上数组**解决：某一步在向 x 组装载 tile i+1 的同时，用上一步已装好的 y 组计算 tile i。两组交替担任「正在装载」和「正在计算」的角色，互不冲突。这与 u6-l1 KNN 的 `i%2` 双缓冲是同一模式的两级版（KNN 是 load/compute/sort 三级）。

#### 4.2.2 核心流程

顶层 tile 循环跑 `NUM_TILES + 1` 步（多出的 1 步用于排空），每步先算两条 flag：

```text
load_flag    = (0 <= i < NUM_TILES)   # i 从 0 起：第 0 步只装不算
compute_flag = (0 < i <= NUM_TILES)   # i 到 NUM_TILES 止：最后一步只算不装

若 i 为偶数:  装载 → x 组 (tile i)；计算 ← y 组 (tile i-1)
若 i 为奇数:  装载 → y 组 (tile i)；计算 ← x 组 (tile i-1)
```

baseline（NUM_TILES=35，共 36 步）的时序：

```text
步 i:       0      1      2      3    ...    34     35
装载:      T0→x  T1→y  T2→x  T3→y   ...  T34→x    —
计算:        —   C(x)  C(y)  C(x)   ...  C(y)   C(x)
写回区间:    —   [0,8) [8,16) [16,24) ... [264,272) [272,280)
```

1 步充填（prologue）+ 34 步稳态 + 1 步排空（epilogue）。flag 窗口的精妙在于**边界正确性**：第 0 步 compute_flag=0，`buffer_compute` 整个被跳过——虽然此时指针参数已算出 `all_out + (0-1)*8`（负偏移），但函数体不执行，永远不会真正解引用；最后一步 load_flag=0，`buffer_load` 空转，避免读出 tile 范围之外的 DRAM。

#### 4.2.3 源码精读

x / y 两组乒乓缓冲的声明与综合指示：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:99-109](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L99-L109)。`local_nzval_x/y` 与 `local_cols_x/y` 各是 `[UNROLL_FACTOR][L*ROWS_PER_TILE/UNROLL_FACTOR]` 的二维数组：第一维是计算通道（`ARRAY_PARTITION dim=1 complete` 把各通道拆成独立存储），`RESOURCE core=XPM_MEMORY uram` 把大数组映射到 URAM。

`buffer_load` 内部的 DATAFLOW——nzval 与 cols 两路装载并行：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:78-84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L78-L84)。两条装载函数分别走 gmem0 / gmem1 两个独立 bundle，`#pragma HLS DATAFLOW`（第 81 行）让它们重叠执行——这是内核里**显式标注的唯一一处数据流并行**。

vec 的预取与按通道复制：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:112-135](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L112-L135)。先把 DRAM 上的 vec 读进 `temp_vec[N]`，再复制成 `local_vec[UNROLL_FACTOR][N]`——每个计算通道一份完整副本。原因：各通道并行执行随机 gather，共享一份数组会造成读口争用；`dim=1 complete` 分区后每通道独占一个 URAM 读口。第 113 行的 `cyclic factor=W_FACTOR_512` 在 baseline 中因子为 1（`DWIDTH_512=32`），是宽口时代的遗留写法。

顶层 tile 循环——flag 窗口与 i%2 交替：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:141-155](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L141-L155)。第 142 行是 `NUM_ITERATIONS` 放大外环；第 144-145 行定义两条 flag；第 147/151 行计算装载指针 `nzval + i*ROWS_PER_TILE*L/W_FACTOR_256`——tile i 在 DRAM 上从宽字偏移 `i*ROWS_PER_TILE*L/W_FACTOR_256` 处开始（行块连续切分，见 4.4.3 的主机侧对应代码）；第 148/152 行计算消费上一组缓冲、把结果写到 `all_out + (i-1)*ROWS_PER_TILE`。

一个值得记录的**仓库观察**（与 u6-l1 对 KNN baseline 的观察相同）：tile 循环本身**没有** `DATAFLOW` 标注，装载与计算在代码里是先后两个函数调用；x/y 分组 + flag 窗口的作用是**消除装载与计算之间的写读数据冲突**，使两者具备被调度重叠的前提（正确性条件），而实际重叠程度由综合调度器决定。代码中显式保证的并行只有 `buffer_load` 内部的两路装载。

最终把 `all_out` 打包写回 DRAM：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:157-166](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L157-L166)——`NUM_ITERATIONS` 全部跑完后才执行一次，用 `range()` 位段写入（写方向的宽口拆包），流量极小（每 PE 仅 N_OUT 个 float）。

#### 4.2.4 代码实践

**实践目标**：把 4.2.2 的文字时序图落到纸面，并核对你的推导与代码逐行吻合。

**操作步骤**：

1. 画出 baseline_30PE 的 tile 级时序甘特图：横轴为步 i（0..35），两行分别记「装载目标组 + tile 号」与「计算源组 + 写回区间」。
2. 对 optimal_4PE 重画一张（NUM_TILES=32，共 33 步）。
3. 在 [krnl_partialspmv.cpp:146-153](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L146-L153) 上逐行核对：偶数步装载 x、计算 y；奇数步反过来；写回区间 `(i-1)*ROWS_PER_TILE` 起共 ROWS_PER_TILE 行。

**需要观察的现象**：最后一步（i=35）只有计算没有装载，第一步（i=0）只有装载没有计算；35 个 tile 恰好被 35 次计算消费，`all_out` 的 280 行被无缝填满。

**预期结果**：与 4.2.2 的表格一致；两条 flag 表达式在 i=0 与 i=NUM_TILES 处的边界行为（跳过计算 / 跳过装载）在图上一目了然。

#### 4.2.5 小练习与答案

**练习 1**：如果去掉 x/y 两组、只用一组缓冲（装载和计算都用它），会发生什么？

**参考答案**：同一步内「装载写缓冲」与「计算读缓冲」变成对同一数组的写后读冲突，计算必须等装载完全结束，退化为完全串行——装载与计算在数据上互斥。乒乓的意义就是让「写这块」与「读那块」永不指向同一组。

**练习 2**：`NUM_TILES+1` 步里，稳态（装载与计算都有效）占多少步？这个比例随什么变化？

**参考答案**：NUM_TILES+1 步中充填 1 步、排空 1 步，稳态 NUM_TILES-1 步（baseline 为 34/36 ≈ 94%）。tile 数越多（即 ROWS_PER_TILE 相对于 N_OUT 越小），充填/排空的占比越低；但 tile 越小，每 tile 的计算量也越小，调度开销占比上升——tile 大小是这两者之间的权衡。

**练习 3**：为什么 `local_vec` 不需要 x/y 乒乓？

**参考答案**：vec 在内核启动时装载一次、整个生命周期只读不变（SpMV 中右端向量被所有行共享）。会变的数据（nzval/cols 每 tile 换一批）才需要乒乓；恒定数据一份即可，但为了并行通道不争读口，要按 `UNROLL_FACTOR` 复制。

### 4.3 宽口拆包

#### 4.3.1 概念说明

微基准（u3-l2）告诉我们：端口位宽从 32bit 提到 256bit，理论带宽×8，且每个宽字一次传输摊薄的 AXI 协议开销更少。但 ELLPACK 的语义单位是 32-bit 的 float 和 int——**端口变宽后，必须把宽字拆回语义单位**。

`load_nzval` / `load_cols` 完成三件事：

1. **顺序读宽字**：以 `INTERFACE_WIDTH_256`（`ap_uint<DWIDTH_256>`）为单位连续读 tile 数据——连续地址会被 AXI 自动合并成突发；
2. **位段拆包**：用 `ap_uint::range(hi, lo)` 从宽字里切出每 32-bit；
3. **通道分发**：把拆出的元素按到达顺序路由到 UNROLL_FACTOR 个计算通道的局部数组。

#### 4.3.2 核心流程

每个 tile 在宽口视角下的总量：

\[ \text{tile\_length} = \frac{L \times ROWS\_PER\_TILE}{W\_FACTOR\_256}, \qquad W\_FACTOR\_256 = \frac{DWIDTH\_256}{32} \]

每个宽字携带 `W_FACTOR_256` 个元素，通道 j 分得其中的第 `tile_length/UNROLL_FACTOR` 个宽字段：

```text
for i in 0..tile_length-1:                # PIPELINE II=1
    row = i / (tile_length/UNROLL_FACTOR)  # 目标通道号（整除）
    col = i % (tile_length/UNROLL_FACTOR)  # 通道内宽字位置
    temp = 宽口读一个字
    for k in 0..W_FACTOR_256-1:            # UNROLL
        元素 = temp.range(32k+31, 32k)      # 位段切出 32bit
        local[row][col*W_FACTOR_256 + k] = 位重解释后的值
```

关键点：`row = i / (tile_length/UNROLL_FACTOR)` 是**整除映射**——前 `tile_length/UNROLL_FACTOR` 个宽字全给通道 0，接着一段给通道 1……而主机按行块连续切分数据（4.4.3），通道 q 恰好对应 tile 的第 q 个行块，与 `buffer_compute` 写回时 `out[q*rows + r]` 的行块顺序严格一致。装载、计算、写回三方对「通道 = 连续行块」达成构造性对齐——没有任何运行期检查，全靠三处代码用同一套整除/取余公式。

#### 4.3.3 源码精读

`load_nzval` 全函数：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:38-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L38-L56)。逐段看：

- 第 41 行：`tile_length = L*ROWS_PER_TILE/W_FACTOR_256`，宽字总数；
- 第 42-45 行：循环体 + `row`/`col` 的整除与取余（通道分发公式）；
- 第 46 行：`INTERFACE_WIDTH_256 temp_data = nzval[i];` 一次宽口读（baseline 中是 32-bit 读，optimal 中是 256-bit 读——**源码一个字不改**，改的只是头文件里的 `DWIDTH_256`）；
- 第 47-53 行：k 循环 `UNROLL`，`temp_data.range(range_idx+31, range_idx)` 切出第 k 个 32-bit 位段，`*((float*)(&tmp_int))` 做**位重解释**（bit-cast，把同一组位当作 float 读，不是数值转换），存入 `local_nzval[row][col*W_FACTOR_256+k]`。

`load_cols` 是同一结构的 int 版：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:58-76](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L58-L76)，第 70-72 行拆包后位重解释为 int。

与 u2-l1 读带宽微基准的对照：那边 `INTERFACE_WIDTH* temp = in0[j];` 里 temp 只被 volatile 消费（防优化），拆包不存在；这边 temp 的每个位段都进入后续计算，**拆包循环本身就是计算的一部分**。k 循环必须 UNROLL，否则每拍只能拆一个元素，宽口读入的 8 个（或 16 个）float 要 8（16）拍才能消化，装载流水线反被拆包卡住。

#### 4.3.4 代码实践

**实践目标**：对 optimal_4PE 手推通道分发，体会「同一份内核源码 + 不同的 `DWIDTH_256`」如何改变拆包行为。

**操作步骤**：

1. 读 [optimal_4PE/src/krnl_config.h:7-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_config.h#L7-L9)：`DWIDTH_256 = 256`，故 `W_FACTOR_256 = 8`。
2. 手算：`tile_length = 1024*64/8 = 8192` 个宽字；每通道 `8192/4 = 2048` 个宽字；每通道元素数 `2048*8 = 16384`，与 `L*ROWS_PER_TILE/UNROLL_FACTOR = 1024*64/4` 核对。
3. 回答：第 `i = 5000` 个宽字去哪个通道、存在通道内哪个位置？装的是矩阵第几行（tile 内相对行号）的数据？

**需要观察的现象**：`row = 5000/2048 = 2`（第 2 通道），`col = 5000%2048 = 904`；该字装的是通道 2 的第 904 个宽字，即 tile 内相对行 `2*(64/4) + 904*8/1024 = 32 + 7 = 39` 起的连续 8 个列元素。

**预期结果**：三个数字（tile_length、每通道宽字数、每通道元素数）与上一步手算一致；baseline 一侧对应值是 8192、8192、8192（UNROLL_FACTOR=1，单通道独吞）。

**待本地验证**：若本机装有 Vitis，可 `make check TARGET=sw_emu DEVICE=<平台>` 验证功能（可执行文件为 `spmv`，见 [Makefile:75](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/Makefile#L75) 与 [Makefile:110-115](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/Makefile#L110-L115)）；注意 sw_emu 要实例化 30 个 CU、主机生成完整 8400×1024 数据（仿真分支未缩小规模，见 [host.cpp:162-165](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L162-L165) 中被注释掉的 1024），运行可能相当慢。

#### 4.3.5 小练习与答案

**练习 1**：`*((float*)(&tmp_int))` 和 `(float)tmp_int` 有什么区别？

**参考答案**：前者是位重解释（bit-cast）——把 uint32_t 的 32 个位原样当作 IEEE 754 浮点读出，不改变任何位；后者是数值转换——把整数当数值算出最接近的浮点表示（如 1065353216 → 1.06353532e9）。AXI 搬来的就是原始浮点位型，必须用前者。

**练习 2**：baseline 的 `max_read_burst_length=16` 配 32-bit 端口，单次突发最多搬多少字节？optimal 的 64 配 256-bit 呢？

**参考答案**：突发 × 位宽/8。baseline：16 × 4 = 64 B；optimal：64 × 32 = 2048 B。前者每次突发要付一次 AXI 协议开销只搬 64 B，效率低——这正是 suboptimal_4PE（burst 16）与 optimal_4PE（burst 64）唯一差异的由来（见 4.4.3）。

**练习 3**：如果把 k 拆包循环上的 `UNROLL` 去掉，装载吞吐会怎样？

**参考答案**：每拍只能拆出一个 32-bit 元素，消费速率降为 1/W_FACTOR_256；宽口每拍供 8 个（optimal）元素而只消化 1 个，装载循环的 II 无法维持 1，等效于宽口带宽被浪费约 7/8。

### 4.4 多实例部署

#### 4.4.1 概念说明

单个 PE 的聚合带宽有限，SpMV 的行之间完全独立，天然适合「按行块切给多个 PE」。baseline_30PE 的选择是微基准结论的一个极端应用：**30 个内核实例 × 32-bit 窄端口**，把 4 条 DDR 通道铺满。这一部署涉及三层协同：

1. **内核接口**：每 CU 有 gmem0（nzval）与 gmem1（cols/vec/out 共享）两个 AXi 端口；
2. **链接配置**：`spmv.ini` 用 `nk=30` 例化 30 个 CU，用 `sp=` 把每个端口指到 DDR[0..3]，用 `slr=` 做物理放置；
3. **主机绑定**：`host.cpp` 为每个 PE 分配独立的缓冲区，flags 与 `sp=` 行逐端口对齐。

#### 4.4.2 核心流程

baseline 的分 bank 方案（`num_PE_per_bank = {7,7,7,7}` + 2 个例外）：

```text
DDR[0]: CU1-7  全部端口          + CU29 的 cols/vec/out
DDR[1]: CU8-14 全部端口          + CU29 的 nzval
DDR[2]: CU15-21 全部端口         + CU30 的 nzval
DDR[3]: CU22-28 全部端口         + CU30 的 cols/vec/out

重流量流（nzval + cols）合计: DDR0=15, DDR1=15, DDR2=15, DDR3=15  ← 完全均衡
```

30 个 CU × 2 条重流量流 = 60 条 32-bit 读流，恰好每 bank 15 条。SLR 放置上 30 个 CU 分布在 SLR0（12 个）/SLR1（6 个）/SLR2（12 个）。

对照 optimal_4PE：4 个 CU 各占一条 DDR 通道，每 CU 三个 bundle（gmem0=nzval、gmem1=cols、gmem2=vec/out），共 8 条重流量流（4×2，均为 256-bit）。「多而窄」与「少而宽」两个极端在同一份内核源码上实现。

#### 4.4.3 源码精读

**内核接口与 bundle 分组**：[case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp:89-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L89-L92)。baseline 中 nzval 独占 gmem0（读突发 16），**cols、vec、out 三个参数共用 gmem1**——同名 bundle 在硬件上是同一个 AXI 主端口，所以 ini 里每个 CU 虽有 4 条 `sp=` 行，物理上每 CU 只有 2 个主端口；30 CU × 2 = 60 个主端口。对照 optimal 版（[optimal_4PE/src/krnl_partialspmv.cpp:89-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_partialspmv.cpp#L89-L92)）：vec/out 挪到独立的 gmem2，且 nzval/cols 的 `max_read_burst_length` 从 16 提到 64——**suboptimal_4PE 与 optimal_4PE 的内核源码只有这两行 burst 数字不同**（已用 diff 实测），是突发长度这一单因素的受控实验。

**ini 的常规块**：[spmv.ini:3-25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L3-L25) 是 CU1-7 的标准块（SLR0 + DDR[0] 全端口）；[spmv.ini:48-52](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L48-L52) 起是 DDR[1] 组；第 84 行起 CU14 被放回 SLR0 但仍连 DDR[1]（slr 与 bank 是两个独立自由度）。

**两个例外 CU**：[spmv.ini:183-193](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L183-L193)——CU29 的 nzval→DDR[1]、其余→DDR[0]；CU30 的 nzval→DDR[2]、其余→DDR[3]。这正是 4.4.2 里「每 bank 15 条流」补齐的最后两块拼图。实例总数声明：[spmv.ini:195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L195) `nk=krnl_partialspmv:30`。

**主机侧 CU 创建**：[host.cpp:138-149](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L138-L149) 按 `krnl_partialspmv:{krnl_partialspmv_N}` 名字为 30 个 CU 各建一个 `cl::Kernel` 对象——CU 编号**从 1 开始**。

**按 PE 切分数据**：[host.cpp:181-201](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L181-L201)。`part_size = dataSize/NUM_KERNEL`，第 i 个 PE 拿走连续的第 i 段（nzval 与 cols 同步切，vec 整份复制，输出预留 N/NUM_KERNEL）——**连续行块切分**，这正是内核侧 `nzval + i*ROWS_PER_TILE*L/W_FACTOR_256` 指针算术成立的前提。

**主机 bank 绑定与 ini 对齐**：[host.cpp:229-264](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L229-L264)。真机分支里第 230 行 `num_PE_per_bank[] = {7,7,7,7}`，第 245-254 行按序把 PE 0..27 的四组缓冲绑到对应 `ddr_bank[i]`，第 255-263 行处理两个例外。注意**偏移一位的对应关系**：主机的 `PE_idx` 从 0 计数，`nzvalBufExt[28].flags = ddr_bank[1]` 对应 ini 里**编号从 1 起**的 `krnl_partialspmv_29`。仿真分支（[host.cpp:212-228](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L212-L228)）全部绑 `ddr_bank[1]`——sw_emu 不模拟 bank 拓扑，绑定无物理意义。

**启动与计时**：[host.cpp:311-330](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L311-L330)。30 个内核在乱序队列上 `setArg` + `enqueueTask` 后 `finish`；计时窗口是 u2-l3 分析过的主机 chrono 口径（含启动开销，但 `NUM_ITERATIONS=5000` 已把执行时间放大到足以稀释它）。与微基准不同，第 330 行**只打印 Execution time，不算带宽**——案例研究把推算留给读者：每次全扫描的读流量为 \( B_{pass} = 2 \times N \times L \times 4 \) 字节（nzval+cols，两变体分别约 68.8 MB 与 67.1 MB），有效带宽 = \( B_{pass} \times 5000 / t \)。

**结果校验**：[host.cpp:58-90](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L58-L90) 在 CPU 上以同序累加生成金标准，[host.cpp:341-349](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L341-L349) 合并 30 段输出后逐元素比对——这是案例研究与微基准的重要差异：微基准只测不验，案例研究必须保证算得对。

**四变体配置矩阵**（配置均出自各自 `krnl_config.h` 第 7-25 行，突发出自内核第 89-92 行）：

| 变体 | DWIDTH_256（重流量端口） | W_FACTOR_256 | NUM_KERNEL | N | ROWS_PER_TILE | UNROLL_FACTOR | NUM_TILES | nzval/cols 读突发 | 每 CU 端口组 |
|---|---|---|---|---|---|---|---|---|---|
| baseline_30PE | 32 | 1 | 30 | 8400 | 8 | 1 | 35 | 16 | 2（gmem0 + gmem1 共享） |
| suboptimal_4PE | 256 | 8 | 4 | 8192 | 64 | 4 | 32 | 16 | 3（gmem0/1/2） |
| optimal_4PE | 256 | 8 | 4 | 8192 | 64 | 4 | 32 | 64 | 3（gmem0/1/2） |
| aggressive_4PE | 512 | 16 | 4 | 8192 | 64 | 4 | 32 | 32 | 3（gmem0/1/2） |

（四变体频率均为 300 MHz：[baseline_30PE/Makefile:66](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/Makefile#L66) 等四份 Makefile 同款 `--kernel_frequency 300`；aggressive 的突发见 [aggressive_4PE/src/krnl_partialspmv.cpp:89-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_partialspmv.cpp#L89-L92)。所有变体的 `DWIDTH_512` 恒为 32——vec/out 端口始终是窄口，因为它们的流量只占零头。）

#### 4.4.4 代码实践

**实践目标**：验证 ini 与主机 bank 绑定的跨工具契约，特别是两个例外 CU 的「差一」对齐。

**操作步骤**：

1. 逐行对照 [spmv.ini:183-193](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L183-L193) 与 [host.cpp:255-263](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/host.cpp#L255-L263)，写出「ini CU 名 ↔ 主机数组下标」的映射式。
2. 统计每条 DDR 通道上的重流量流（nzval + cols）数量，验证 4.4.2 声称的 {15,15,15,15}。
3. 换 optimal_4PE（[optimal_4PE/spmv.ini:3-27](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/spmv.ini#L3-L27)）：每 CU 一条通道、`nk=4`，重流量流是「4 × 2 条 256-bit」。
4. 用频率 × 位宽 / 8 算两类部署的**端口侧峰值和**：baseline 60 × 1.2 GB/s；optimal 8 × 9.6 GB/s。

**需要观察的现象**：CU29 ↔ `PE_idx=28`、CU30 ↔ `PE_idx=29`（ini 编号从 1 起、主机从 0 起）；两类部署的端口侧峰值和分别约 72 GB/s 与 76.8 GB/s，而 4 条 DDR 通道的通道侧上限约 4 × 19.2 = 76.8 GB/s（单通道峰值，u6-l2 已建立）。

**预期结果**：baseline 的端口和（72）**低于**通道和——端口先锁顶；optimal 的端口和恰好贴着通道和——两边匹配。这正是 u6-l4 要展开的「min(端口和， 通道和)」分析入口。

#### 4.4.5 小练习与答案

**练习 1**：为什么主机要给每个 PE 单独一份 `vec_data_part[i]`，而不是 30 个 PE 共享一个只读 vec 缓冲？

**参考答案**：ini 把 30 个 CU 的 vec 端口分散到 4 条 bank（且 CU29/30 的 vec 与其他端口不同 bank）。若共享一个缓冲，它只能放在某一条 bank 上，所有 CU 的 vec 端口都得连到那条 bank，与各自的 nzval/cols 不同 bank 的均衡布局冲突；每 PE 一份让四个端口可以绑进同一 bank，与 `sp=` 行一致。代价是 30 份冗余拷贝（每份 33.6 KB，流量上可忽略，内核启动时只读一次）。

**练习 2**：baseline 的 ini 给 CU14 写 `slr=krnl_partialspmv_14:SLR0` 但端口连 `DDR[1]`。SLR 放置和 bank 连接是什么关系？

**参考答案**：两者是独立的链接期自由度：`sp=` 决定数据走到哪条内存通道（流量均衡），`slr=` 决定 CU 的电路放在哪个 Super Logic Region（时序/走线）。CU14 的数据归 DDR[1] 组、电路放 SLR0——大概率是为了把 SLR1 的资源占用摊给资源更空的 SLR0。这也呼应 u3-l3 的结论：slr 对主机透明，只影响时序。

**练习 3**：把 baseline 的 `NUM_KERNEL` 从 30 改成 32，最少要动哪些地方？

**参考答案**：`krnl_config.h` 的 `NUM_KERNEL`（若 N 不变，N_OUT=8400/32=262.5 非整数——还必须改 N 或 ROWS_PER_TILE 保证整除，例如 N=8192、ROWS_PER_TILE=8 → NUM_TILES=32）；`spmv.ini` 要为 CU31/CU32 增加 slr/sp 行并改 `nk=krnl_partialspmv:32`；`host.cpp` 真机分支的 `num_PE_per_bank` 要从 {7,7,7,7} 改成 {8,8,8,8}（或保留例外逻辑并重算）。内核源码与装载/计算逻辑零改动——规模参数全部收敛在契约头与连接配置里。

## 5. 综合实践

完成一份「SpMV 变体推演报告」，把本讲四个模块串起来：

1. **NUM_TILES 推导链**（承接 4.1.4）：对四个变体各写一行 `NUM_TILES = N/NUM_KERNEL/ROWS_PER_TILE`，验算 8400/30/8=35 与 8192/4/64=32，并解释 N 的取值为何必须是 `NUM_KERNEL × ROWS_PER_TILE` 的倍数。
2. **乒乓时序图**（承接 4.2.4）：画 baseline（36 步）与 optimal（33 步）两张甘特图，标出充填步、稳态步、排空步，以及每步的写回行区间。
3. **置换关系分析**（本讲规格中的核心任务）：以「单 PE 内部并行 × PE 数」为坐标，对比 baseline（UNROLL 1 × 30 PE，每通道 8192 元素）与 optimal（UNROLL 4 × 4 PE，每通道 16384 元素）：
   - 每通道片上 nzval 缓冲：两档 ×(L×ROWS_PER_TILE/UNROLL_FACTOR×4 B)（x/y 双份）；
   - `local_vec` 复制成本：`UNROLL_FACTOR × N × 4 B`（baseline 33.6 KB vs optimal 131 KB，每 PE）；
   - 说明「加宽端口 → 每 tile 携带元素暴增 → 片上缓冲随之暴涨 → 只能减少 PE」的因果链，以及「UNROLL 是片上并行的第三个旋钮，与 PE 数近似可互换但代价结构不同（UNROLL 吃每 PE 的存储与向量通道，PE 数吃连线与实例数）」。
4. **带宽上限排序**（为 u6-l4 铺垫）：用 4.4.4 的端口侧峰值和（72 / 76.8 / 76.8 / 153.6 GB/s）对照通道侧上限 76.8 GB/s，给出四变体理论聚合带宽的排序与锁顶原因，并标注「suboptimal 与 optimal 配置全同、仅突发 16↔64 之差」——预测它俩的实测差距来自突发效率而非峰值。

报告不依赖真机即可完成；若在真机（或拿到论文数据）上验证，用第 3 项的推演对照 `B_pass × 5000 / t` 的实测带宽。

## 6. 本讲小结

- **ELLPACK 内核**：`ellpack()` 用循环交换（j 外层 PIPELINE、i 内层 UNROLL）把逐行串行浮点累加链变换为多个独立累加器的并行更新，`out[i] += nzval[j+i*L] * vec[cols[j+i*L]]` 中的随机 gather 由按通道复制的片上 `local_vec` 承接。
- **乒乓双缓冲**：x/y 两组片上数组 + `load_flag`/`compute_flag` 两条窗口，`NUM_TILES+1` 步完成充填/稳态/排空，消除装载与计算之间的写读冲突；vec 只读不需乒乓，但需按 UNROLL_FACTOR 复制防读口争用。
- **宽口拆包**：`load_nzval`/`load_cols` 以 `range(32k+31, 32k)` 位段切出 32-bit 并 bit-cast 回 float/int，用整除/取余把连续宽字流分发到 UNROLL_FACTOR 个通道，与主机行块切分、计算写回三方构造性对齐；改位宽只动 `krnl_config.h` 的 `DWIDTH_256` 一行。
- **多实例部署**：`nk=30` + 每 CU 两条 `sp=`（gmem0/gmem1 两组 bundle，cols/vec/out 共享 gmem1）把 60 条 32-bit 重流量流均衡到 4 条 DDR 通道（每 bank 恰 15 条，含 CU29/CU30 两个跨 bank 例外），主机 `num_PE_per_bank={7,7,7,7}` 加例外与 ini 逐行对齐（注意 CU 编号从 1 起、主机下标从 0 起的差一）。
- **变体置换**：四变体在「PE 数 × 端口位宽 × UNROLL」三维上做受控实验——suboptimal 与 optimal 仅突发 16↔64 两行不同；baseline 端口侧峰值和 72 GB/s 先于通道上限锁顶，optimal 的 76.8 GB/s 恰好匹配，aggressive 的 153.6 GB/s 远超通道上限。
- **遗留观察**：`INTERFACE_WIDTH_256/512` 命名与 baseline 实际 32-bit 不符；tile 循环无顶层 DATAFLOW 标注（与 KNN baseline 同款）；主机只打印时间不算带宽。

## 7. 下一步学习建议

- 下一讲 **u6-l4「从微基准洞察到加速器设计决策」**：把本讲的端口侧/通道侧峰值推演正式化，用「带宽利用率 = 实测吞吐 / 微基准峰值」评估四个 SpMV 变体与 KNN 变体，并解释 aggressive（512bit 宽口）为何可能实测反而次优。
- 回读对照：[optimal_4PE/spmv.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/spmv.ini#L1-L27)（每 CU 独占一条 bank 的极简部署）与 [aggressive_4PE/src/krnl_partialspmv.cpp:89-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_partialspmv.cpp#L89-L92)（512-bit 宽口的突发配置）。
- 若想巩固乒乓与三级流水的对照，重读 [case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel-uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L1-L1)，比较 load/compute/sort 三级 flag 窗口与本章两级窗口的异同。
- 若对宽口拆包想再进一步，回到微基准主线 [ubench/offchip_bandwidth/datacenter/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/ubench/offchip_bandwidth/datacenter/README.md#L1-L1)，用 auto_collect 生成一个 256bit 读带宽工程，实测该位宽在 burst=16 与 64 下的效率差，验证 suboptimal/optimal 分手的依据。
