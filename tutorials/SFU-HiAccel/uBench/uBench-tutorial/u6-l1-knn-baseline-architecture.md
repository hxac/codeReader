# KNN 案例研究 I：baseline 的三级流水与全局归并架构

## 1. 本讲目标

本讲是案例研究单元的第一讲。前面单元里我们测的是「内存系统本身」（带宽、延迟、流），从本讲开始，我们看**一个真实的加速器是如何把这些微基准洞察用起来的**。

学完本讲，你应该能够：

1. 读懂 `krnl_partialKnn` 内核中 load / compute / sort 三级**软件流水**：为什么用 `flag` 相位错开、为什么用 `i % 2` 双缓冲。
2. 解读**奇偶交替插入排序**（odd-even sort）如何用一份完全展开的 12 元素寄存器数组，在线维护 TOP=10 最近邻。
3. 说明 14 个 `krnl_partialKnn` 实例如何通过 `hls::stream`（AXIS 流）把部分结果送进 `krnl_globalSort`，完成 14 路 × 10 个候选的**多路归并**。
4. 对照 `knn.ini` 与 `host.cpp`，理解 **nk 多实例 + stream_connect + sp 端口映射** 三类连接指令如何共同拼出一张 15 个计算单元的数据流网。

## 2. 前置知识

本讲默认你已读过 u2-l1（HLS 内核基础）、u2-l2（主机端编程模型）与 u4-l1（流带宽微基准）。用到的前置概念快速复习：

- **KNN（K-近邻）**：给定一个查询点 q 和一个含 N 个点的搜索空间，找出与 q 欧氏距离最小的 K 个点。本仓库中查询点维度 `INPUT_DIM=2`，`TOP=10` 即 K=10。这是典型的**带宽受限**问题——计算只是减法和乘加，瓶颈全在把搜索空间从片外内存搬进片上。
- **m_axi 端口**（u2-l1）：内核经 AXI 主端口读片外 DDR。本例每个 `krnl_partialKnn` 的两个指针参数共用一个 `bundle=gmem1` 端口，32bit 位宽（`DWIDTH=32`）。
- **hls::stream 与 stream_connect**（u4-l1）：`hls::stream<pkt>` 综合成 AXIS 流端口；`knn.ini` 里的 `stream_connect=CU名.端口名:CU名.端口名` 在**链接期**把两个内核的流端口焊死，流参数对主机透明、不能 setArg。
- **nk 指令**（u3-l3）：`nk=内核名:实例数` 让链接器例化多个计算单元（CU），CU 自动命名为 `内核名_1`、`内核名_2`……主机按 `内核名:{内核名_N}` 逐个创建 Kernel 对象。
- **数据局部性与双缓冲（乒乓缓冲）**：片上 RAM 容量有限，只能装下一小块数据（tile）；要流水就必须准备两份缓冲，一份在写（装载新 tile）、一份在读（处理旧 tile），按步数奇偶交替角色。

一句话预览全局：**14 个「搬运 + 算距离 + 局部排序」的工人内核并行分摊搜索空间，每人交出自己的 TOP=10，最后一个归并内核收齐 14 份榜单，选出全局 TOP=10 写回 DDR。**

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [case_study/KNN/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md) | 案例背景：源自 Rodinia KNN，是论文 Table 2 / Section 5 的四个设计之一 |
| [case_study/KNN/baseline_14PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_config.h) | 契约头：`DWIDTH=32`、`NUM_KERNEL=14`、`TOP=10`、`NUM_ITERATIONS=5000`、流类型 `pkt` |
| [case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp) | 工人内核：load/compute/sort 三级软件流水 + 奇偶插入排序 + 流输出 |
| [case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp) | 归并内核：收 28 条流，做 14 路选择归并，写回 TOP=10 |
| [case_study/KNN/baseline_14PE/knn.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini) | 连接表：slr 放置、sp 端口→DDR[0]、stream_connect 流焊接、nk 实例数 |
| [case_study/KNN/baseline_14PE/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp) | 主机：切分搜索空间、按 CU 创建 15 个 Kernel、软件验证数据生成 |

## 4. 核心概念与源码讲解

### 4.1 三级软件流水：load / compute / sort 的相位错开与双缓冲

#### 4.1.1 概念说明

一个工人内核要处理属于自己的 2341 个 tile（每 tile 256 个 32bit 字，即 128 个二维点）。对每个 tile 依次做三件事：

1. **load**：把 tile 从 DDR 搬进片上 URAM（`local_SP_0/1`）。
2. **compute**：对 tile 中每个点算与查询点的欧氏距离平方（`INPUT_DIM=2`，就是两次减法、两次乘法、一次加法），128 个结果写进 `local_distance_0/1`。
3. **sort**：把 128 个距离逐个喂进插入排序网络，维护全局 TOP=10 候选。

如果老老实实「装载完 → 算完 → 排完 → 再装载下一个」，DDR 端口在算和排的两段时间里全程闲置——这正是 u3 系列微基准告诉我们的最大浪费。**软件流水（software pipelining）** 的思路是：把三级的执行在时间上错开半个身位，让任一时刻三级各处理一个**不同的** tile：第 i 步在装载 tile i 的同时，计算 tile i-1、排序 tile i-2。这样 DDR 读、距离计算、排序比较三种互不相同的资源同时满负荷。

#### 4.1.2 核心流程

先用伪代码看懂循环骨架（变量名与源码一致）：

```text
for it_idx in 0..NUM_ITERATIONS-1:            # 5000 次，时间放大
    for i in 0..NUM_OF_TILES+1:               # 每轮 NUM_OF_TILES+2 步
        load_flag    = (0 <= i < NUM_OF_TILES)          # 装载 tile i
        compute_flag = (1 <= i < NUM_OF_TILES+1)        # 计算 tile i-1
        sort_flag    = (2 <= i < NUM_OF_TILES+2)        # 排序 tile i-2

        if i 为偶数:
            load(→SP_0);  compute(读SP_1→dist_1);  sort(读dist_0)
        else:
            load(→SP_1);  compute(读SP_0→dist_0);  sort(读dist_1)
```

三个 flag 是三条错开一格的窗口：装载窗口从 i=0 打开，计算窗口从 i=1 打开，排序窗口从 i=2 打开；三者同时关闭也依次错开。于是这个长度为 `NUM_OF_TILES+2` 的循环里，**开头两步只有装载在干活（流水线充填），结尾两步只有排序在收尾（流水线排空），中间 2341 步三级满载**。+2 正是两级流水的充填/排空开销。

`i % 2` 双缓冲的角色分配：

| 步数 i（偶） | 写 SP_0（tile i） | 读 SP_1（tile i-1 装的） | 写 dist_0 | 读 dist_1 |
| --- | --- | --- | --- | --- |
| 步数 i+1（奇） | 写 SP_1（tile i+1） | 读 SP_0（tile i 装的） | 写 dist_1 | 读 dist_0 |

每个缓冲在奇数步被写、偶数步被读（或反之），写与读永远隔一步，硬件上两份 URAM 即可支撑「装载新 tile 与处理旧 tile 并行」。这与 u3-l4 写带宽微基准里「内核实例乒乓」、u4-l3 URAM 上的双缓冲是同一思想：**用两倍缓冲面积换取访问级并行**。

一个必须诚实指出的细节：本仓库这份源码**没有** `#pragma HLS DATAFLOW`（我用 Grep 在整个 `case_study/KNN/` 下确认过，一处都没有）。三个子函数靠 `#pragma HLS INLINE OFF` 保持为独立硬件模块，跨步骤的重叠由「flag 守卫 + 双缓冲使相邻步骤无数据依赖」这一结构性质支撑，实际重叠程度交给 HLS 调度器。这是从上游例子摘抄时丢掉了 DATAFLOW 标注、还是刻意为之，属于「待确认」——但它恰好是很好的阅读练习：判断流水能否重叠，看的是**依赖关系**，不是有没有那行 pragma。

#### 4.1.3 源码精读

**契约头**先定三个全局数：[case_study/KNN/baseline_14PE/src/krnl_config.h:8-15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_config.h#L8-L15) 定义 `DWIDTH=32`（32bit 端口）、流拍类型 `pkt`（32bit 数据、无 side-band）、`NUM_KERNEL=14`、`TOP=10`、`NUM_ITERATIONS=5000`。注意 `NUM_KERNEL` 同时被内核侧（算 tile 数）和主机侧（切分数据、建 CU）include，是本工程份量最重的一个常量。

**tile 尺寸与总数**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:5-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L5-L9) 定义 `INPUT_DIM=2`、`SP_LEN=256`（每 tile 256 个 32bit 字 = 128 个二维点 = 1KB）、`DIS_LEN=128`（每 tile 128 个距离），以及本讲标题里那个数：

```cpp
const int NUM_OF_TILES = 32774/NUM_KERNEL;
```

32774 是**整个搜索空间的 tile 总数**：主机设 `num_of_points = 4195072`（见 4.4.3），每 tile 128 点，\( 4\,195\,072 / 128 = 32\,774 \)；再被 14 个内核均分，\( 32\,774 / 14 = 2\,341 \)，恰好整除。所以每个工人内核处理 2341 个 tile、覆盖 \( 2341 \times 128 = 299\,648 \) 个点，14 个内核合计正好 4195072 个点。

**load 级**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:11-20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L11-L20)。`INLINE OFF` 让它独立成模块；`flag` 为假时整个循环被跳过（空转一步）；为真时以 `PIPELINE II=1` 每拍读一个 32bit 字，共 256 拍——地址连续（`tile_idx*SP_LEN+i`），会由 AXI 自动合并成突发（baseline 未显式设 `max_read_burst_length`，这正是 u6-l2 中 suboptimal 与 optimal 的分野）。

**compute 级**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:22-39](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L22-L39)。外层 `ii += 2` 是因为每个点占 2 个字（INPUT_DIM=2）：`local_SP[ii]`、`local_SP[ii+1]` 分别是 x、y。`range(31,0)` 从 32bit 字里取出低 32 位再按 float 重解释（位宽加大后这个套路会变成 u6-l2 aggressive 版的 `FACTOR_W` 路展开）。`kk` 循环 `UNROLL` 后，两点维度的减乘加在单拍内完成：

\[ \text{dist} = \sum_{k=0}^{1} (p_k - q_k)^2 \]

**片上缓冲与资源绑定**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:109-122](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L109-L122)。两份 `local_SP`（各 256×32bit）与两份 `local_distance`（各 128 float）都用 `HLS RESOURCE core=XPM_MEMORY uram` 绑到 URAM；而排序数组 `local_kNearstDist/Id` 用 `ARRAY_PARTITION complete` 全展开成寄存器（它只有 12 个元素，且排序网络要随机访问）。每内核 URAM 开销约 \( 2 \times 1\,\text{KB} + 2 \times 512\,\text{B} = 3\,\text{KB} \)，14 份毫无压力。

**流水主循环**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:128-145](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L128-L145)。三个 flag 的窗口错开一格（L130-132），`i%2` 分支选择缓冲组（L134-143），`sort` 的第三实参 `(i-2)*DIS_LEN` 把「当前在排序的 tile」换算成全局点 ID 偏移。外层 `NUM_ITERATIONS=5000` 把整轮扫描重复 5000 遍——功能上结果不变（每轮 i=0,1 时 sort 的 else 分支会把 TOP 数组重置，见 4.2.3），时间上把执行时间放大 5000 倍以便主机稳定计时。这正是 u3-l1 里读带宽微基准 `NUM_ITERATIONS=10000` 的同款手法：**案例研究与微基准共享同一套测量纪律**。

#### 4.1.4 代码实践

1. **实践目标**：用一张「相位表」亲眼验证三级流水错开与双缓冲角色互换。
2. **操作步骤**：拿纸笔，对照 [krnl_partialKnn.cpp:128-145](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L128-L145)，为 i = 0,1,2,3,4 五步各写一行，填四列：`load 写哪个 SP、装哪个 tile`；`compute 读哪个 SP、算哪个 tile`；`sort 读哪个 dist、排哪个 tile`；`sort_flag=false 时做什么`。
3. **需要观察的现象**：i=0 时 compute 读的 `local_SP_1` 还没被写过——为什么不出错？（提示：`compute_flag=0` 使函数空转，即「守卫比缓冲初始化更早生效」）。
4. **预期结果**：表格应呈现出对角线错位——第 i 行装载 tile i、计算 tile i-1、排序 tile i-2；SP/dist 缓冲的下标随 i 奇偶每步翻转。若你的表出现同一缓冲同一步既被读又被写，说明推错了行。
5. 本实践为源码阅读型，无需运行，结论可直接从代码推出。

#### 4.1.5 小练习与答案

**练习 1**：把 `NUM_KERNEL` 从 14 改成 7（假设内核和 ini 同步改），`NUM_OF_TILES` 变成多少？总 tile 数变吗？
答案：`32774/7 = 4696`，总 tile 数不变（由搜索空间大小和 SP_LEN 决定），只是每个工人分到的 tile 翻倍、并行度减半。

**练习 2**：循环为什么是 `NUM_OF_TILES+2` 步而不是 `NUM_OF_TILES` 步？
答案：三级流水需要 2 步充填（前 2 步只有 load 有效）与 2 步排空（后 2 步只有 sort 有效），充填与排空在首尾交叠，总步数 = 有效载荷 2341 步 + 2 步相位差。

**练习 3**：为什么 `local_SP` 需要两份而 `local_Query` 一份就够？
答案：`local_Query` 在流水启动前写一次、之后只读（无写读冲突）；`local_SP` 每步都要写新 tile、同时被上一 tile 的 compute 读，写读相隔一步靠奇偶两份解冲突。

### 4.2 奇偶插入排序：用 12 个寄存器维护 TOP=10

#### 4.2.1 概念说明

sort 级要在流经的 \( 2341 \times 128 \approx 30 \) 万个距离里在线选出最小的 10 个。经典做法是维护一个长度 K 的有序数组，每来一个候选就插入排序——但串行插入的比较链无法流水。**奇偶转置排序（odd-even transposition sort）** 的技巧是：把「一次插入」拆成一轮**奇偶两趟不相邻比较交换**：

- **奇趟**：同时比较所有奇数位置对 (1,2)、(3,4)、(5,6)……这些数对互不相交，可以**全部并行**。
- **偶趟**：同时比较 (0,1)、(2,3)、(4,5)……同样全并行。

两趟交替执行，新元素就像气泡一样从入口位置 0 逐步沉到自己的名次，整个数组始终维持**降序**（位置越大距离越小，位置 10 是当前已知最近点）。每个新候选只需 O(1) 趟（这里每候选固定 2 趟、每趟 5 个并行比较器）即可归位，与 K 无关的常数流水深度。

#### 4.2.2 核心流程

```text
维护数组 d[0..11]（降序），d[0] 是入口槽：
每来一个候选 dist：
    d[0] = dist;  id[0] = start_id + 本候选在 tile 内的序号
    奇趟: 并行 for ii in {1,3,5,7,9}:  若 d[ii] < d[ii+1] 则交换 (d[ii],d[ii+1]) 与对应 id
    偶趟: 并行 for ii in {1,3,5,7,9}:  若 d[ii] > d[ii-1] 则交换
结束后输出 d[1..10]   # 共 10 个，弃掉 d[0]（它是当前最大者，本就是待淘汰槽）
```

为什么弃 d[0]？数组降序时 d[0] 恰是维护集合中的最大距离；它同时是所有新候选的入口——新候选无论多差，最坏也只占据 d[0] 附近并在下一轮被更新覆盖，永远不会污染 d[1..10] 里真正的 TOP=10。**入口槽 = 牺牲槽**，这是这套网络最优雅的一处设计。

#### 4.2.3 源码精读

**swap 与 sort 本体**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:41-90](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L41-L90)。外层 `i` 循环逐个消费 `local_distance[]` 的 128 个候选，`PIPELINE II=1`（L59-61 写入口槽）；奇趟在 [L64-71](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L64-L71)，偶趟在 [L73-80](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L73-L80)，两趟的 `ii` 循环均 `UNROLL` 并配 `DEPENDENCE variable inter false`——这句 pragma 是在向调度器保证「同一趟内被展开的各比较交换彼此无依赖，可同拍执行」，没有它奇偶并行就不成立。

**else 分支的双重身份**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:83-89](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L83-L89)。`flag` 为假时把 12 个元素全置 `MAX_FLT`/`-1`。它在流水里出现于每轮 i=0、1 两步（sort_flag 尚未打开），作用有二：一是首轮初始化；二是 `NUM_ITERATIONS` 外层循环每重复一轮时**重置状态**——否则上一轮的 TOP 结果会泄露进下一轮。flag 机制一石二鸟，把「初始化」也编进了流水相位。

**流输出**：[case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp:147-155](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L147-L155)。全部 tile 处理完后，把 d[1..10] 逐个包成 `pkt` 写进 `kNearstDist` 流；同时把 `local_kNearstId[1..10]` 写进 `kNearstId` 流。注意输出顺序是**降序**（先 d[1] 最大者、后 d[10] 最小者），这个顺序约定马上会在归并内核里被利用。

**一个可以观察到的冗余**：数组声明为 `TOP+2 = 12` 个元素（L119-122），但奇偶趟的循环上界 `ii < TOP+1` 使所有比较只触及下标 0..10，**下标 11 从未被比较**，仅被初始化和闲置。它不影响正确性，是防御式留位或上游代码的残留——阅读源码时能指出「哪个元素从不参与运算」是很好的读懂标志。

#### 4.2.4 代码实践

1. **实践目标**：手工推演奇偶插入排序，验证「降序不变式 + 入口槽淘汰」。
2. **操作步骤**：设 TOP=3（数组长 5），初始 `d = [MAX, MAX, MAX, MAX, MAX]`，候选序列依次为 `0.9, 0.1, 0.5, 0.7, 0.3`。对每个候选按源码 L59-80 的顺序执行：写 d[0]、奇趟（比较 (1,2)、(3,4)）、偶趟（比较 (1,0)、(3,2)），逐步抄下数组状态。
3. **需要观察的现象**：任何一步之后数组是否始终降序？0.9（最大候选）最终停在哪里？
4. **预期结果**：五步之后 `d[1..3] = {0.7, 0.5, 0.3}`，0.9 被后续候选挤到 d[0] 并被覆盖或停留在入口槽——即正确维护了最小的 3 个；每一步数组都保持降序。若出现乱序说明某趟的比较方向抄反（奇趟是 `<` 交换、偶趟是 `>` 交换，方向相反）。
5. 本实践纯纸面推演，无需硬件。

#### 4.2.5 小练习与答案

**练习 1**：如果删掉 `DEPENDENCE inter false` 两条 pragma，预计会发生什么？
答案：调度器无法证明同趟内被 UNROLL 的各比较交换互不依赖，会保守地串行化它们（或大幅降低 II），排序吞吐骤降、sort 级成为流水瓶颈。功能不变，性能受损。

**练习 2**：把 TOP 改成 20，硬件代价大约怎么变？
答案：数组 22 个元素仍全展开成寄存器；每趟的比较器从 5 个变 10 个，资源近似线性增长，且仍是每候选固定两趟——这就是这种网络对 K 的良好伸缩性（也是 `ARRAY_PARTITION complete` 的前提）。

**练习 3**：sort 级每步消费 128 个候选，为什么它不会被设计成每次比较 128 路全展开？
答案：128 路两两比较的并行归约网络深度和面积都远大于「逐个入场 + 固定 2 趟奇偶」的在线方案；后者与 `PIPELINE II=1` 兼容，让 sort 级与 load/compute 级节拍对齐，这正是三级流水能咬合的前提。

### 4.3 流归并内核 krnl_globalSort：28 条流收拢成全局 TOP=10

#### 4.3.1 概念说明

14 个工人各交一份降序 TOP=10 榜单，归并内核要从中选出全局 TOP=10。这是经典的 **K 路归并**：每路已有序，用「每路一个游标，反复取当前 14 个头部中的最小者」的 选择法，共输出 10 次即止（不必归并完 140 个——只要最小的 10 个出来就可以收工）。

为什么用流而不是让工人直接写 DDR？回顾 u4-l1：流是片上直连、零 DDR 占用、天然带反压（读端没来，写端阻塞）。14 份榜单每份只有 10 个 32bit 字，共 560 字节，走 DDR 既浪费带宽又要主机中转；走 AXIS 流则归并内核的输入端口在链接期就被 `stream_connect` 焊到 14 个工人身上，**部分结果的聚合完全发生在片上**。

#### 4.3.2 核心流程

```text
krnl_globalSort:
1. 收流：循环 TOP=10 轮，每轮从 14 条距离流各读 1 个 + 14 条 ID 流各读 1 个
        → local_kNearstDist_partial[14][10]（全展开寄存器阵列）
2. 选择归并（从最小端取）：
        idx[j] = 9 对每路 j            # 每路游标从"最小者"所在端起步
        for 输出槽 i = 9 down to 0:
            在 14 路的当前游标值中找最小 → dist[i], id[i]
            该路游标 idx[j] 减 1
3. 把 local[0..9] 写回 DDR 的 kNearstDist/kNearstId
```

一个容易困惑的点：游标为什么从 `idx[j] = TOP-1 = 9` 起步而不是 0？因为每路的流输出是**降序**（4.2.3 的输出顺序），该路第 9 个（最后一个写的）才是它的最小距离。归并从各路最小端开始逐轮选取，输出槽从 i=9 往 i=0 填——最终 `dist[0]` 装的是第 10 小、`dist[9]` 装的是全局最小。这个「降序进、降序出」的约定是工人与归并者之间的一份隐式契约，两边源码里都没有注释，只能靠对读确认。

#### 4.3.3 源码精读

**选择归并函数**：[case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp:5-33](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L5-L33)。`idx` 数组 `ARRAY_PARTITION complete dim=0` 全展开（L10-11）使 14 个游标可并行递减；外层循环从 `i=TOP-1` 递减到 0（L18），内层 j 循环 `PIPELINE II=1` 扫 14 路找最小（L21-27），选出后写输出槽并递减胜者游标（L28-31）。总节拍约 \( TOP \times NUM\_KERNEL = 10 \times 14 = 140 \) 拍量级，相对工人的百万拍计算可以忽略。

**28 个流参数与接口声明**：[case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp:35-100](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L35-L100)。签名前 28 个参数全是 `hls::stream<pkt>&`（14 条距离 + 14 条 ID），各自配 `INTERFACE axis`；最后两个 DDR 输出指针共用 `bundle=gmem0` 并配 `s_axilite` 控制端口。流参数在主机侧**不可见也不可设**，但它们仍占据内核参数表的前 28 个位置——所以主机给 `kNearstDist`/`kNearstId` setArg 时索引必须是 28 和 29（见 4.4.3），这一处编号契约是新手最常踩的坑。

**收流循环**：[case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp:112-199](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L112-L199)。循环 TOP 轮、`PIPELINE II=1`，每轮把 28 次 `read()` 的 32bit 数据经 `uint32_t` 中转按 float/int 重解释，填进 `local_*_partial[14][10]`（该阵列在 [L102-105](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L102-L105) `ARRAY_PARTITION complete dim=0` 全展开成寄存器）。`read()` 是阻塞的——工人没写完榜单，归并内核就在此等待，这代替了任何显式的内核间同步。

**归并与写回**：[case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp:201-207](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L201-L207)。调 `seq_global_merge` 后，把本地 TOP 数组以 `II=1` 写回 DDR 输出缓冲。

**与主机验证口径的对齐**：主机在 [host.cpp:80-84](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L80-L84) 用 `std::sort` 升序排距离后取 `kNearstNeighbors[i] = distance[TOP-1-i]`，得到的是**降序**的软件基准——与硬件输出的降序 `dist[0..9]` 恰好同序，`verify` 才能逐元素相等通过。另注意 [host.cpp:314-315](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L314-L315) 只验证了距离 `kNN_hw`，**ID 数组 `kNN_hw_id` 未被验证**（仅搬运回主机）。这对教学工程无伤大雅，但读代码时应当看出来。

#### 4.3.4 代码实践

1. **实践目标**：用玩具规模手工走通选择归并，并核对 setArg 编号。
2. **操作步骤**：
   a. 设 NUM_KERNEL=2、TOP=3。两路降序榜单：A = {0.5, 0.2, 0.1}，B = {0.4, 0.3, 0.6}（注意每路必须降序）。按 [seq_global_merge](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L5-L33) 的逻辑，游标 `idx = {2, 2}`，从输出槽 i=2 递减到 i=0，逐步记录每轮的最小值与被递减的游标。
   b. 数一数 [krnl_globalSort 签名](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L35-L66) 的参数，回答：为什么主机是 `setArg(28, ...)` 与 `setArg(29, ...)`？
3. **需要观察的现象**：归并是否在取满 3 个后自然停止（游标远未耗尽）？
4. **预期结果**：a 的输出应为 `dist[2]=0.1, dist[1]=0.2, dist[0]=0.3`（升序次序填槽，最终数组降序）；b：28 个流参数占据实参 0..27，两个 DDR 指针是第 28、29 个。若 a 中出现 0.6，说明某路游标越界（0.6 是 B 路最大者，只有归并满 6 个才会轮到它——这正是「取满即停」的价值）。
5. 本实践纸面可完成；有 Vitis 环境者可进一步用 `make check TARGET=sw_emu DEVICE=<平台>` 跑仿真看 `TEST PASSED`（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么收流循环只需 TOP 轮就能保证归并正确？
答案：全局 TOP=10 的任何成员，在其所属路的榜单中必然排名前 10（该路只上交了自己的前 10），所以每路只需提供 10 个候选、归并只需在这些候选中选 10 个——140 选 10，答案与全量排序一致。

**练习 2**：如果某个工人内核意外崩溃、流里少写一个元素，系统表现是什么？
答案：没有任何超时或报错机制——归并内核的 `read()` 会**永久阻塞**，`q.finish()` 不返回，程序挂死。流通道提供了同步与反压，但不提供活性监测，这是 u4-l1 里「生产者—消费者必须配对」结论在真实设计中的再现。

**练习 3**：归并输出的 `dist[0]` 是最近点还是第 10 近点？主机的软件基准又是哪种次序？
答案：`dist[0]` 是第 10 近（最大者），`dist[9]` 是最近点；主机 `kNearstNeighbors[i] = distance[TOP-1-i]` 同样把降序基准存进 `kNN_sw[0..9]`，两边同序，故逐元素 `verify` 能通过。

### 4.4 多实例连接配置：nk × stream_connect × sp 拼出 15 个 CU 的数据流网

#### 4.4.1 概念说明

u3-l3 讲过 ini 三指令，本讲是把它们用到极致的样本：`nk` 一行把 `krnl_partialKnn` 例化 14 份；`stream_connect` 28 行把每份的两条输出流焊到归并内核的对应输入；`sp` 30 行把 15 个内核的全部 30 个 m_axi 端口指到内存通道。三者的**命名必须逐字符对齐**：CU 名由 `nk` 生成规则决定（`内核名_序号`），stream_connect 两端写「CU 名.端口名」，sp 写「CU 名.指针参数名」——任何一端拼错，链接器直接报错或落空。这是 u3-l3 所说「跨工具契约」的 15 倍放大版。

#### 4.4.2 核心流程

```text
knn.ini 的三段式结构：
[connectivity]
  ① slr=<CU>:SLR0                     × 15     # 全部塞进 SLR0
  ② sp=<CU>.<port>:DDR[0]             × 30     # 14×2 输入端口 + 归并 2 输出端口
  ③ stream_connect=<工人CU>.<流>:<归并CU>.<流> × 28   # 14 条距离流 + 14 条 ID 流
  nk=krnl_partialKnn:14
  nk=krnl_globalSort:1
```

数据流全景（也是综合实践要画的图）：

```text
                    DDR[0]（唯一被使用的内存通道）
        ┌────────────┬────────────┬──────────────┬─────────────┐
     query[0]   searchSpace[0]  searchSpace[1]      ...     searchSpace[13]
        │            │             │                          │
        ▼            ▼             ▼                          ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐                 ┌─────────┐
   │partialKnn│ │partialKnn│ │partialKnn│     ×14        │partialKnn│
   │   _1     │  │   _2    │  │   _3     │                │   _14   │
   └──┬──┬───┘  └──┬──┬───┘  └──┬──┬───┘                └──┬──┬──┘
   Dist Id      Dist Id       Dist Id                     Dist Id     ← AXIS 流
      │  └────┐   │  └────┐    │  └────┐                   │  └────┐
      ▼      ▼   ▼      ▼    ▼       ▼                    ▼       ▼
   in1   in1_id in2  in2_id  in3  in3_id        …      in14  in14_id
   ┌────────────────────────────────────────────────────────────────┐
   │                     krnl_globalSort_1                           │
   └──────────────────────────┬──────────────────┬──────────────────┘
                       kNearstDist         kNearstId        → 写回 DDR[0]
```

#### 4.4.3 源码精读

**nk 与 slr/sp**：[case_study/KNN/baseline_14PE/knn.ini:91-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L91-L92) 声明 14+1 个实例；[knn.ini:2-56](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L2-L56) 把 14 个工人全放 SLR0、把每个工人的 `inputQuery` 与 `searchSpace` 两个端口都指到 `DDR[0]`（例如 [L2-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L2-L4) 是第一个 CU 的三行）。**28 个读端口全部挤在一条 DDR 通道上**——每个端口 32bit，若 300MHz 满发，聚合需求 \( 14 \times 4\,\text{B} \times 300\,\text{MHz} = 16.8\,\text{GB/s} \)，直逼单通道上限。这是 baseline 的关键设计取舍：简单（不用操心跨 bank 的数据划分），但带宽天花板被单通道锁死——u6-l2 的 64bit 端口版与 u6-l4 的方法论分析都由此发端。

**stream_connect 焊接**：[knn.ini:59-72](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L59-L72) 逐一把 `krnl_partialKnn_N.kNearstDist` 连到 `krnl_globalSort_1.inN`；[knn.ini:74-87](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L74-L87) 把 `kNearstId` 连到 `inN_id`；[knn.ini:88-89](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L88-L89) 再把归并内核的两个输出端口指到 DDR[0]。对照 4.3.3 的签名即可核对：`in1..in14`、`in1_id..in14_id` 正是归并内核 28 个流参数的名字，两端名字各出现一次、一一配对。

**主机侧的三件配套工作**：

1. **按 CU 建 15 个 Kernel 对象**：[host.cpp:135-148](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L135-L148)。用 `"krnl_partialKnn:{krnl_partialKnn_N}"` 的「内核名:{CU名}」语法逐个绑定，再用 `"krnl_globalSort:{krnl_globalSort_1}"` 建归并内核——CU 名与 `nk` 的自动命名严格耦合。
2. **把搜索空间 14 等分**：[host.cpp:181-190](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L181-L190)。`partition_size = num_of_points*INPUT_DIM/NUM_KERNEL`，第 i 份取原数组的 `[i*partition_size, ...)` 段——切分粒度与内核的 `NUM_OF_TILES = 32774/14` 恰好一致（`num_of_points` 在 [L159](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L159) 定为 4195072）。**主机切法与内核 tile 数是两处独立常量、必须人肉保持一致**——又一份无编译期检查的契约。
3. **缓冲绑 bank、设参、启动**：[host.cpp:199-232](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L199-L232) 用 `cl_mem_ext_ptr_t` 把 30 个缓冲全绑到与 `sp` 行对应的 bank（真机分支 `XCL_MEM_DDR_BANK0` 对应 `DDR[0]`；仿真分支却写 `DDR_BANK1`，与 ini 不一致——仿真不严格执行拓扑所以能跑通，这是 u2-l2 见过的「模板遗留空骨架」现象的又一例）。[host.cpp:292-301](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L292-L301) 逐个 `setArg(0/1, ...)` 启动 14 个工人（流参数无需也无法设置），最后 `setArg(28, 29)` 启动归并、`finish()` 等全队收工。

**一段死代码**：[host.cpp:18-28](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L18-L28) 定义了 32 项 HBM `XCL_MEM_TOPOLOGY` bank 表，但两个分支都只用 `XCL_MEM_DDR_BANK*`，这张表在 baseline 里从未被读取——它是从某个 HBM 工程拷模板带过来的残留，也顺带说明这套代码的 U280/HBM 血统（u3-l3 的 `bank[n]|XCL_MEM_TOPOLOGY` 写法）。

**构建入口**：[case_study/KNN/baseline_14PE/Makefile:73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/Makefile#L73) 通过 `LDCLFLAGS += --config ./knn.ini` 把连接表交给 `v++ -l`；[Makefile:96-104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/Makefile#L96-L104) 先把两个内核各编成 `.xo` 再链接成 `knn.xclbin`——与 u1-l3 的微基准构建完全同构，只是内核变成两个。

#### 4.4.4 代码实践

1. **实践目标**：核对「nk 生成 CU 名 → stream_connect/sp 引用 CU 名 → 主机绑定 CU 名」这条命名链，并量化连接表规模。
2. **操作步骤**：
   a. 在仓库根目录执行 `grep -c "^stream_connect" case_study/KNN/baseline_14PE/knn.ini` 与 `grep -c "^sp=" case_study/KNN/baseline_14PE/knn.ini`、`grep -c "^slr=" case_study/KNN/baseline_14PE/knn.ini`。
   b. 对照 [krnl_globalSort.cpp:35-66](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_globalSort.cpp#L35-L66) 的参数名，逐行核对 `knn.ini` 里 28 条 `stream_connect` 右端的端口名是否与之一致。
   c. 找出 `knn.ini` 中没有任何 `stream_connect`/`sp` 行引用的内核端口（如果存在）。
3. **需要观察的现象**：三类行数的总和是否等于「15 个 CU × 各自端口数」？有没有落单的端口？
4. **预期结果**：`stream_connect` 28 行、`sp` 30 行、`slr` 15 行；归并内核 28 个流端口与 2 个 DDR 端口全部被 wiring 覆盖；工人内核的 `inputQuery`/`searchSpace` 各 14 份被 sp 覆盖。唯一的「落单」观感来自 `inputQuery`——14 份缓冲装的是同一份查询点数据（[host.cpp:200-203](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L200-L203) 全指向 `query_data.data()`），即同一数据被复制绑定了 14 次。
5. 本实践仅需 grep 与阅读，无硬件依赖。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `nk=krnl_partialKnn:14` 改成 `:7` 而不改其他任何行，会在哪个环节报什么错？
答案：链接期报错。`nk=7` 只生成 `_1.._7` 七个 CU，而 ini 里 `slr=`/`sp=`/`stream_connect=` 仍引用 `_8.._14`，`v++ -l --config knn.ini` 找不到这些 CU 名直接失败——这正是「命名链逐字符对齐」的机械后果。

**练习 2**：28 个读端口全接 `DDR[0]`，与把它们分摊到 U200 的两条 DDR 通道相比，得失是什么？
答案：得——主机无需考虑跨通道的数据划分与 bank 标志，buffer/代码最简单；失——聚合带宽被单通道锁死（约 16.8 GB/s 需求挤一条通道），多 PE 扩展到带宽上限后收益归零。把 7 个 PE 分到 `DDR[1]` 可近乎翻倍上限，代价是主机切分与绑 bank 的复杂度。这是 u6-l4「微基准洞察 → 设计决策」的直接素材。

**练习 3**：主机为什么要建 14 个 `cl::Kernel` 对象而不是一个？
答案：`enqueueTask` 的一次调用只在一个 CU 上启动一份内核。要让 14 个 CU 同时跑，需要 14 个各自绑定到具体 CU 的 Kernel 对象（「内核名:{CU名}」语法）分别 enqueue；配合 `CL_QUEUE_OUT_OF_ORDER_EXEC_MODE_ENABLE`（[host.cpp:118-123](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L118-L123)）它们才能并发执行。

## 5. 综合实践

把本讲四个模块串成一个任务——**为 baseline_14PE 建立一张「从数字到连线」的完整档案**：

**任务 A：画数据流图。** 仿照 4.4.2 的示意图，自己从零画一张更大的：左边是 DDR[0] 与它上面的 30 个缓冲（标注哪些是同一份数据的复制），中间 14 个 `krnl_partialKnn_N`（每个内部再画 load→compute→sort 三级与两组乒乓缓冲的小示意），右边 `krnl_globalSort_1` 收 28 条流、出 2 个 DDR 端口。每条 AXIS 流标注两端的名字（如 `krnl_partialKnn_3.kNearstDist → krnl_globalSort_1.in3`），逐条对照 [knn.ini:59-87](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/knn.ini#L59-L87) 验证。

**任务 B：手算整条数字链。** 从 `num_of_points = 4195072` 出发，推出下面每一个数并写明算式与出处：

| 量 | 值 | 算式 | 出处 |
| --- | --- | --- | --- |
| 搜索空间总 float 数 | 8390144 | 4195072 × INPUT_DIM(2) | [host.cpp:159](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L159) |
| 总 tile 数 | 32774 | 4195072 ÷ 128 点/tile | [krnl_partialKnn.cpp:9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L9) |
| 每 PE tile 数 NUM_OF_TILES | 2341 | 32774 ÷ 14 | 同上 |
| 每 PE 点数 | 299648 | 2341 × 128 | 核对 [host.cpp:183-190](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/host.cpp#L183-L190) 的切分 |
| 每 tile 字数 SP_LEN | 256 | 128 点 × 2 维 | [krnl_partialKnn.cpp:6](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L6) |
| 流水总步数（每轮） | 2343 | NUM_OF_TILES + 2 | [krnl_partialKnn.cpp:129](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L129) |

验算标准：2341 × 14 × 128 必须恰好等于 4195072；`partition_size`（主机）× 14 必须覆盖 8390144 个 float。

**任务 C：解释相位错开。** 用你在 4.1.4 画出的相位表，写 3-5 句话回答：为什么三条 flag 窗口各错开一格就能让三级同时满载？为什么 `+2` 步里前两步只能充填、后两步只能排空？如果把三条 flag 全改成同一个窗口（`0 <= i < NUM_OF_TILES`），会发生什么？（提示：三级将在每一步处理**同一个** tile，退化为串行，双缓冲也随之失去意义。）

完成三个任务后，你手里这张图＋表＋解释，就是读 u6-l2 四个变体时的对照基线——下一讲所有「改动」都发生在你刚标好的这些位置上。

## 6. 本讲小结

- **三级软件流水**：`krnl_partialKnn` 用三条错开一格的 flag 窗口 + `i%2` 乒乓缓冲（两组 URAM：`local_SP_0/1`、`local_distance_0/1`），让 load/compute/sort 在任一步各处理一个不同 tile；循环长 `NUM_OF_TILES+2`，+2 是充填/排空；仓库这版无 DATAFLOW 标注，重叠由「flag 守卫 + 相邻步骤无依赖」的结构性质支撑。
- **奇偶插入排序**：12 元素全展开寄存器数组，入口槽 `d[0]` 兼任牺牲槽，奇趟/偶趟各 5 个不相交换并行执行（靠 `DEPENDENCE inter false` 许可），数组恒降序，最终输出 `d[1..10]`；flag 的 else 分支兼任初始化与 `NUM_ITERATIONS=5000` 轮次间的状态重置。
- **流归并内核**：`krnl_globalSort` 以 28 条阻塞式 `read()` 收齐 14 路降序榜单，`seq_global_merge` 从各路最小端（`idx=TOP-1`）做选择归并、取满 10 个即停，输出同为降序——与主机软件基准的降序口径逐元素对齐；流参数占据内核参数表前 28 位，故 DDR 输出的 setArg 索引是 28/29。
- **多实例连接**：`nk=krnl_partialKnn:14` + 28 条 `stream_connect` + 30 条 `sp`（全部指向 `DDR[0]`、全部置于 SLR0）拼出 15 CU 数据流网；CU 名、端口名、主机「内核名:{CU名}」三方逐字符耦合，主机还需以一致的粒度切分搜索空间。
- **设计取舍伏笔**：28 个 32bit 读端口共享单条 DDR 通道，聚合需求约 16.8 GB/s 逼近单通道上限——baseline 选了「最简连接、单通道锁顶」的一端，u6-l2 的位宽/突发变体与 u6-l4 的方法论将给出另一端。
- **遗留观察**：host.cpp 的 32 项 HBM bank 表是死代码；仿真分支 bank flag 与 ini 不一致；`kNN_hw_id` 未参与验证；`TOP+2` 数组的下标 11 从不被比较——读案例源码也要带着微基准系列练出的「找契约、找死代码」的眼光。

## 7. 下一步学习建议

下一讲 **u6-l2《KNN 案例研究 II：四个设计变体与带宽导向的取舍》** 将在本次建立的基线上做差分：`suboptimal_14PE` 与 `optimal_14PE` 只差一个 `max_read_burst_length`（16 对 256，对应 u3-l2 的突发长度因素），`aggressive_11PE` 换 512bit 宽端口 + `FACTOR_W` 路拆包 + `SP_LEN=2048`（对应位宽与片上缓冲因素）。建议先自己 `diff` 这三个目录的 `krnl_config.h` 与 `krnl_partialKnn.cpp`，带着「每个差异对应微基准五因素中的哪一个」的问题去读。

若想先巩固本讲的流水与排序骨架，可延伸阅读：`case_study/KNN/*/src/krnl_partialKnn.cpp` 四个版本互相对照；上游 Xilinx Vitis_Accel_Examples 的 KNN 例程（本案例的血缘来源，README 所引 CHIP-KNN 项目是其泛化）；以及 u4-l1 流微基准中 `ap_axiu` 与 `stream_connect` 的最小化版本，回头再看本讲 28 条流的焊接会更有体感。
