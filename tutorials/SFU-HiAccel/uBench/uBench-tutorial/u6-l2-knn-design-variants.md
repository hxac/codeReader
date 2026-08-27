# KNN 案例研究 II：四个设计变体与带宽导向的取舍

## 1. 本讲目标

上一讲（u6-l1）我们解剖了 `baseline_14PE` 的三级流水与全局归并架构。本讲把视野扩展到 KNN 案例研究的全部四个设计变体，学完后你应当能够：

1. 用一张配置矩阵表整理四个变体（baseline_14PE / suboptimal_14PE / optimal_14PE / aggressive_11PE）在 PE 数量、端口位宽、突发长度、tile 粒度上的差异，并理解每个差异都对应微基准套件（u3 系列）扫过的一个带宽影响因素。
2. 解释「宽端口数据抽取」技巧：512-bit 端口一次读回 16 个 float，如何用 `range()` 位切片配合 `FACTOR_W` 循环展开把 32-bit 浮点逐个取出来。
3. 论述两条扩展路线——「加宽端口、减少 PE」与「增多 PE、用窄端口」——在理论带宽、片上存储资源、部署复杂度上的代价与收益。
4. 亲手执行一次「只改一个突发长度参数」的对照实验（suboptimal vs optimal），体会微基准洞察如何直接转化为加速器设计决策。

## 2. 前置知识

本讲默认你已读过 u6-l1（KNN baseline 架构）和 u3 系列微基准。用三段话唤醒关键记忆：

- **带宽五因素与理论峰值公式**（u1-l1、u3-l2）：内核频率、并发端口数、端口位宽决定理论上限，公式为 \( BW_{theory} = f \times N_{port} \times W / 8 \)（字节/秒）。本讲所有变体统一按 300 MHz 讨论。突发长度与连续访问数据量决定你能在多大程度上逼近这个上限。
- **m_axi 端口与突发**（u2-l1、u3-l2）：`#pragma HLS INTERFACE m_axi` 把指针参数变成 AXI 主端口；`max_read_burst_length` 是单次读突发的拍（beat）数上限，单突发最大字节数 = 拍数 × 位宽 / 8。AXI4 协议规定一个突发最多 256 拍。连续地址的多次请求会被 HLS 自动合并成突发。
- **单通道争用**（u3-l3）：KNN 的 `knn.ini` 把所有内核端口都用 `sp=` 连到同一条 `DDR[0]` 通道（这一点与微基准示例相同）。一条 300 MHz × 512-bit 的 DDR 通道自身峰值约 19.2 GB/s——这是所有变体共同撞上的「天花板」，也是理解 suboptimal/optimal/aggressive 差异的参照系。

另外两个本讲反复使用的术语：

- **PE（Processing Element，处理单元）**：这里就是 `krnl_partialKnn` 内核的一个实例（CU）。`knn.ini` 的 `nk=krnl_partialKnn:14` 例化 14 个 PE。
- **tile（瓦片）**：一次装进片上双缓冲的连续数据块。`load` 级每步搬一个 tile，`compute`/`sort` 每步消化一个 tile。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [case_study/KNN/README.md](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md) | 案例说明：四个设计对应论文 Table 2 与 Section 5 |
| [case_study/KNN/baseline_14PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_config.h) | baseline 契约头：DWIDTH=32、NUM_KERNEL=14 |
| [case_study/KNN/suboptimal_14PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/suboptimal_14PE/src/krnl_config.h) | suboptimal 契约头：DWIDTH=64、NUM_KERNEL=14 |
| [case_study/KNN/optimal_14PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_config.h) | optimal 契约头：DWIDTH=64、NUM_KERNEL=14 |
| [case_study/KNN/aggressive_11PE/src/krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_config.h) | aggressive 契约头：DWIDTH=512、NUM_KERNEL=11 |
| [case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp) | 32-bit 窄口版工人内核（u6-l1 已精读，本讲作对照） |
| [case_study/KNN/suboptimal_14PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/suboptimal_14PE/src/krnl_partialKnn.cpp) | 64-bit 口 + 短突发（16 拍）版 |
| [case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp) | 64-bit 口 + 长突发（256 拍）版，本讲精读主对象 |
| [case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp) | 512-bit 宽口 + FACTOR_W 展开版 |
| [case_study/KNN/optimal_14PE/knn.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/knn.ini) 与 [case_study/KNN/aggressive_11PE/knn.ini](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/knn.ini) | 两版部署：nk=14 vs nk=11 的流连接与 sp 连线 |
| [case_study/KNN/optimal_14PE/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp) 与 [case_study/KNN/aggressive_11PE/src/host.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/host.cpp) | 两版主机：数据规模、setArg 编号与 NUM_KERNEL 的耦合 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 变体配置矩阵**、**4.2 宽端口数据抽取**、**4.3 PE 与位宽权衡**。

### 4.1 变体配置矩阵

#### 4.1.1 概念说明

KNN 目录下的四个子目录不是四个不同算法，而是**同一个算法在「内存端口配置」这个维度上的四个采样点**。[README.md:L2](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md#L2) 明确说明这四个设计对应论文的 Table 2，目的是演示「内存系统洞察（memory system insights）在设计 HLS 加速器时的实际用法」。

把 u3 微基准扫过的参数维度与四个变体对上号：

| 变体 | 端口位宽 DWIDTH | PE 数 NUM_KERNEL | 最大读突发 | 300 MHz 理论聚合带宽 |
|---|---|---|---|---|
| baseline_14PE | 32 bit | 14 | 未显式设置 | 1.2 GB/s × 14 = **16.8 GB/s** |
| suboptimal_14PE | 64 bit | 14 | 16 拍（128 B/突发） | 2.4 GB/s × 14 = **33.6 GB/s** |
| optimal_14PE | 64 bit | 14 | 256 拍（2 KB/突发） | 2.4 GB/s × 14 = **33.6 GB/s** |
| aggressive_11PE | 512 bit | 11 | 32 拍（2 KB/突发） | 19.2 GB/s × 11 = **211.2 GB/s** |

单 PE 口带宽 = 300 MHz × DWIDTH / 8。四行恰好构成一组受控实验：

- **suboptimal vs optimal**：位宽、PE 数完全相同，只差突发长度——论文用这一对隔离「突发长度」单个因素的作用。
- **baseline vs optimal**：PE 数相同、聚合位宽乘积相同吗？不同（32×14 vs 64×14），baseline 是「窄口多 PE」的起点。
- **optimal vs aggressive**：「适度位宽 + 多 PE」vs「极宽位宽 + 少 PE」两条扩展路线的对决。

#### 4.1.2 核心流程

四个变体的内核源码结构完全同构（都是 u6-l1 讲过的 load/compute/sort 三级乒乓流水），差异全部落在**常量参数**上。分析任一变体的套路：

```text
读 krnl_config.h     → DWIDTH（位宽）、NUM_KERNEL（PE 数）
读 krnl_partialKnn.cpp 头部宏 → SP_LEN（tile 字数）、DIS_LEN（tile 点数）、
                              FACTOR_W（每字点数）、NUM_OF_TILES（每 PE tile 数）
读 m_axi pragma      → max_read_burst_length（突发拍数）
读 knn.ini           → nk（例化数）、stream_connect（流条数 = 2×NUM_KERNEL）
读 host.cpp          → num_of_points（总点数，与位宽无关！）
```

由此可推出每个变体的「tile 几何」（下列数字均由源码常量直接算出）：

| 变体 | SP_LEN（字） | 单 tile 字节 = SP_LEN×W/8 | 点/tile = DIS_LEN | 每字点数 = FACTOR_W | NUM_OF_TILES | 每 PE 片上双缓冲（local_SP×2 + distance×2） |
|---|---|---|---|---|---|---|
| baseline | 256 | 1 KB | 128 | —（无此变量） | 32774/14 = 2341 | 2 KB + 1 KB = 3 KB |
| suboptimal / optimal | 256 | 2 KB | 256 | 1 | 16394/14 = 1171 | 4 KB + 2 KB = 6 KB |
| aggressive | 2048 | 128 KB | 16384 | 8 | 264/11 = 24 | 256 KB + 128 KB = 384 KB |

三点值得注意的不变量与变量：

- **每点字节数恒为 8 B**（INPUT_DIM=2 个 float32），与端口位宽无关。位宽改变的是「一个字装几个点」：`FACTOR_W = DWIDTH / (32 × INPUT_DIM)`。
- **总工作量规模一致**：baseline 主机生成 4,195,072 个点（≈32 MB），suboptimal/optimal 为 265216×8×2 = 4,243,456 个点，aggressive 为 16384×264 = 4,325,376 个点——四个变体跑的是同一量级的搜索空间，保证横向可比。
- **tile 越大，流水充排空开销占比越高**：三级流水的 tile 循环步数为 `NUM_OF_TILES+2`（多出的 2 步是充填/排空，见 u6-l1），baseline 的开销占比 2/2341 < 0.1%，aggressive 则为 2/24 ≈ 8%。

#### 4.1.3 源码精读

**四份契约头只差两个数字。** 以 aggressive 为例：

[case_study/KNN/aggressive_11PE/src/krnl_config.h:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_config.h#L8-L12) 定义宽口版的核心参数：DWIDTH=512、NUM_KERNEL=11。与之对照，[baseline_14PE/src/krnl_config.h:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_config.h#L8-L12) 是 DWIDTH=32、NUM_KERNEL=14；[suboptimal_14PE/src/krnl_config.h:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/suboptimal_14PE/src/krnl_config.h#L8-L12) 与 [optimal_14PE/src/krnl_config.h:8-12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_config.h#L8-L12) 完全相同（DWIDTH=64、NUM_KERNEL=14）。四份文件的其余行逐字相同。

**内核头部的 tile 几何宏。** [optimal_14PE/src/krnl_partialKnn.cpp:5-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L5-L9)：SP_LEN=256 个 64-bit 字（2 KB/tile）、DIS_LEN=256 点、FACTOR_W=1（每字 1 点）、NUM_OF_TILES=16394/14。对照 [baseline_14PE/src/krnl_partialKnn.cpp:5-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L5-L9)（SP_LEN=256 字但每字 4 B，DIS_LEN=128，无 FACTOR_W，NUM_OF_TILES=32774/14）与 [aggressive_11PE/src/krnl_partialKnn.cpp:5-9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L5-L9)（SP_LEN=2048、DIS_LEN=16384、FACTOR_W=8、NUM_OF_TILES=264/11）：**同样的总数据量，位宽越宽，每 PE 的 tile 数越少、单 tile 越大**——分子上总字数被位宽除掉了。

**suboptimal 与 optimal 的全部差异只有两行 pragma。** 对两个文件执行 `diff`，输出只有一处（本讲义环境已实测）：

```diff
--- suboptimal_14PE/src/krnl_partialKnn.cpp
+++ optimal_14PE/src/krnl_partialKnn.cpp
- 	#pragma HLS INTERFACE m_axi port=inputQuery offset=slave bundle=gmem1 max_read_burst_length=16
- 	#pragma HLS INTERFACE m_axi port=searchSpace offset=slave bundle=gmem1 max_read_burst_length=16
+ 	#pragma HLS INTERFACE m_axi port=inputQuery offset=slave bundle=gmem1 max_read_burst_length=256
+ 	#pragma HLS INTERFACE m_axi port=searchSpace offset=slave bundle=gmem1 max_read_burst_length=256
```

即 [suboptimal_14PE/src/krnl_partialKnn.cpp:100-101](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/suboptimal_14PE/src/krnl_partialKnn.cpp#L100-L101) 与 [optimal_14PE/src/krnl_partialKnn.cpp:100-101](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L100-L101) 之差。这是论文方法学的精髓：**控制变量到只剩一个微基准因素**。baseline 版的 m_axi（[baseline_14PE/src/krnl_partialKnn.cpp:98-99](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L98-L99)）未写突发选项，采用工具默认值；aggressive 版（[aggressive_11PE/src/krnl_partialKnn.cpp:101-102](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L101-L102)）设 32 拍 × 64 B = 2 KB/突发。

**部署侧：nk 与流条数随 PE 数缩放。** [optimal_14PE/knn.ini:91-92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/knn.ini#L91-L92) 例化 `nk=krnl_partialKnn:14` + `nk=krnl_globalSort:1`，配 [L59-L87](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/knn.ini#L59-L87) 的 28 条 `stream_connect`；[aggressive_11PE/knn.ini:72-73](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/knn.ini#L72-L73) 则是 `nk=krnl_partialKnn:11`，配 [L47-L68](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/knn.ini#L47-L68) 的 22 条流。两版所有 `sp=` 行都指向 `DDR[0]`（如 [optimal_14PE/knn.ini:3-4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/knn.ini#L3-L4)）——全部读口共享一条 DDR 通道，这一点对 4.3 的权衡分析至关重要。

**主机侧：setArg 编号与 NUM_KERNEL 的耦合。** [optimal_14PE/src/host.cpp:300-301](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp#L300-L301) 把输出缓冲硬编码为参数 28/29（前 28 个参数被 14×2 条流占据，见 u6-l1）；而 [aggressive_11PE/src/host.cpp:299-301](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/host.cpp#L299-L301) 改用 `krnl_idx = NUM_KERNEL*2` 泛化计算——改 PE 数时这里是容易踩的坑。数据规模上，[optimal_14PE/src/host.cpp:160-165](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp#L160-L165) 的 `num_of_points = 265216*8*2` 与 [aggressive_11PE/src/host.cpp:160-163](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/host.cpp#L160-L163) 的 `num_of_points = 16384*264` 单位都是**点**，主机全程不做位宽换算（这一点与微基准主机要除以 WIDTH_FACTOR 不同，见 4.3.3 的讨论）。

#### 4.1.4 代码实践

**实践一：脚本化提取配置矩阵并计算理论带宽。**

1. 实践目标：写一个 Python 脚本，自动从四个 `krnl_config.h` 提取 `DWIDTH` 与 `NUM_KERNEL`，按 \( BW = 300\times10^6 \times W \times N / 8 / 10^9 \) 计算理论聚合带宽（GB/s），验证 4.1.1 的表格。
2. 操作步骤：

   ```python
   #!/usr/bin/env python3
   # extract_knn_bw.py —— 示例代码（非项目原有文件）
   import re, pathlib

   FREQ = 300e6  # 论文案例统一按 300 MHz 讨论
   root = pathlib.Path("case_study/KNN")
   for d in sorted(root.iterdir()):
       cfg = (d / "src" / "krnl_config.h").read_text()
       width = int(re.search(r"DWIDTH = (\d+)", cfg).group(1))
       npe   = int(re.search(r"NUM_KERNEL=(\d+)", cfg).group(1))
       bw    = FREQ * width * npe / 8 / 1e9
       print(f"{d.name:<18} DWIDTH={width:<4} NUM_KERNEL={npe:<3} "
             f"每PE={FREQ*width/8/1e9:>5.1f} GB/s  聚合={bw:>6.1f} GB/s")
   ```

   在仓库根目录运行 `python3 extract_knn_bw.py`。
3. 需要观察的现象：四行输出的 DWIDTH/NUM_KERNEL 与目录名后缀一致（14PE↔14、11PE↔11）；带宽列是位宽与 PE 数的乘积。
4. 预期结果（数值为手工推导，本讲义编写环境未安装运行该脚本，待本地验证）：

   ```text
   aggressive_11PE    DWIDTH=512  NUM_KERNEL=11  每PE= 19.2 GB/s  聚合= 211.2 GB/s
   baseline_14PE      DWIDTH=32   NUM_KERNEL=14  每PE=  1.2 GB/s  聚合=  16.8 GB/s
   optimal_14PE       DWIDTH=64   NUM_KERNEL=14  每PE=  2.4 GB/s  聚合=  33.6 GB/s
   suboptimal_14PE    DWIDTH=64   NUM_KERNEL=14  每PE=  2.4 GB/s  聚合=  33.6 GB/s
   ```

   核对要点：9.6/19.2/2.4 等每 PE 数值可由 300 MHz × 位宽/8 口算得出。
5. 若脚本报 `FileNotFoundError`：确认工作目录是仓库根，且四个变体目录名与 `case_study/KNN/` 下一致。

**实践二：复现「只差突发长度」的对照实验。**

1. 实践目标：用 `diff` 亲眼确认 suboptimal 与 optimal 的内核源码只有 `max_read_burst_length` 一处差异。
2. 操作步骤：在仓库根执行 `diff case_study/KNN/suboptimal_14PE/src/krnl_partialKnn.cpp case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp`。
3. 需要观察的现象：输出仅一处 `100,101c100,101` 块，内容是 16→256。
4. 预期结果：与 4.1.3 中引用的 diff 输出完全一致（本讲义环境已实测验证）。再对 `krnl_config.h`、`host.cpp`、`knn.ini`、`krnl_globalSort.cpp` 分别 diff，确认全部逐字节相同。
5. 待本地验证项：无。

#### 4.1.5 小练习与答案

**练习 1**：optimal 的突发为什么选 256 拍而不是更大？选 256 有什么「恰好」？

答案：AXI4 协议规定单次突发最多 256 拍，256 已是协议上限；且 256 拍 × 8 B/拍 = 2048 B 恰好等于一个 tile（SP_LEN=256 字 × 64 bit），即 `load` 循环的整段连续读可以被合并为**每个 tile 恰好一次突发**，地址/路由开销摊到最薄。再大也无意义——协议不允许，数据也不连续超过一个 tile。

**练习 2**：aggressive 的 `NUM_OF_TILES = 264/NUM_KERNEL` 中 264 从哪来？host.cpp 里哪个数字与之呼应？

答案：264 = 总 tile 数 = 搜索空间总字数 / SP_LEN。host 侧 [aggressive_11PE/src/host.cpp:160](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/host.cpp#L160) 的 `num_of_points = 16384 * 264`：每个 tile 16384 个点，共 264 个 tile；264 = 11 PE × 24 tile/PE，恰好被 NUM_KERNEL 整除。验证：16384 点 × 8 B/点 = 128 KB = SP_LEN(2048) × 64 B。✓

**练习 3**：四个变体主机程序的搜索空间总量为何刻意保持同量级？

答案：KNN 的执行时间近似正比于读入的数据量；只有总工作量一致，四个变体的实测耗时才可直接比较，带宽利用率的差异才能归因于端口配置（位宽/突发/PE 数）而非数据规模——这沿用了微基准「一次只动一个因素」的控制变量法（u2-l3、u3-l2）。

### 4.2 宽端口数据抽取

#### 4.2.1 概念说明

端口加宽后出现一个微基准里没有的问题：**AXI 端口按「字」传输，而算法按「32-bit float」计算**。32-bit 口时一个字就是一个 float，直接用；512-bit 口时一次读回 16 个 float 挤在一个字里，必须把它们逐个「拆」出来——这就是宽端口数据抽取（unpacking）。

KNN 的拆包工具是 `ap_uint` 的 `range(hi, lo)` 位切片方法：从宽字中取出比特区间 [lo, hi]（两端包含，bit 0 是最低位）组成窄值。配套的循环维度 `FACTOR_W`（每字点数）决定一个宽字要拆出多少个计算对象。

#### 4.2.2 核心流程

设字宽 \( W = DWIDTH \)，每个点有 \( D = INPUT\_DIM = 2 \) 个 float，则每个字装 \( F = W/(32D) \) 个点（即 FACTOR_W）。第 \( j \) 个点（\( 0 \le j < F \)）的第 \( k \) 维位于宽字的比特区间：

\[ [\,32\,(jD + k),\ \ 32\,(jD + k) + 31\,] \]

拆包循环的通用形状（对任意位宽成立）：

```text
for ii in 0..SP_LEN-1:            # 遍历宽字
    for jj in 0..FACTOR_W-1:      # 遍历字内各点
        for kk in 0..INPUT_DIM-1: # 遍历点的各维（UNROLL 完全展开）
            bits = (jj*D + kk) * 32
            float = word[ii].range(bits+31, bits)
        distance[ii*F + jj] = 欧氏距离
```

三个变体的实例化：

- **baseline（W=32）**：退化情形，一个字 = 一个维度值，`range(31,0)` 取整个字，两点跨两个字（`local_SP[ii+kk]`）。
- **optimal（W=64）**：一个字 = 1 个点（2 float），FACTOR_W=1，`range(31,0)` 与 `range(63,32)`。
- **aggressive（W=512）**：一个字 = 8 个点（16 float），FACTOR_W=8，`range()` 的起点随 `jj` 步进 64 bit。

#### 4.2.3 源码精读

**baseline：窄口直取。** [baseline_14PE/src/krnl_partialKnn.cpp:26-37](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L26-L37)：`compute` 的外层循环 `ii += 2` 每步跨两个字（一个点），第 [31](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L31) 行 `local_SP[ii+kk].range(31, 0)` 直接把 32-bit 字转成 float——无拆包，但每拍只消费 4 B。

**optimal：FACTOR_W 骨架已就位但取值为 1。** [optimal_14PE/src/krnl_partialKnn.cpp:26-41](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L26-L41)：外层 `ii` 循环带 `PIPELINE II=1`（每拍一个字），内层 `jj` 循环（FACTOR_W=1，[UNROLL](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L28-L29) 完全展开）是单次迭代。关键的泛化拆包在 [L34-L36](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L34-L36)：`range_idx = (start_idx + kk) * 32`，其中 `start_idx = jj * INPUT_DIM`——这正是通用公式的实现；FACTOR_W=1 时 `range_idx` 退化为 0 或 32。结果写入 `local_distance[ii*FACTOR_W+jj]`（[L41](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L41)）。

**aggressive：FACTOR_W=8 的完整形态。** [aggressive_11PE/src/krnl_partialKnn.cpp:26-44](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L26-L44) 与 optimal 的差别有三处，全部围绕「一个字 8 个点」：

1. **流水线从外层挪到内层**：外层 `ii` 循环的 `// #pragma HLS PIPELINE II=1` 被注释掉（[L27](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L27)），`PIPELINE II=1` 改挂在 `jj` 循环上（[L29](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L29)）。效果是**每拍完成一个点**的距离计算——与 optimal 的每拍一个点持平，拆包本身没有降低计算吞吐。
2. **位切片随 jj 步进**：[L32-L36](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L32-L36)：`start_idx = jj * INPUT_DIM` 后 `range_idx = (start_idx + kk) * 32`，第 jj 个点占比特 [64jj, 64jj+63]，kk 维在其内偏移 32kk。512-bit 字被切成 8 段 64-bit 点，再各切两段 float。
3. **距离数组按点索引并做循环分区**：`local_distance[ii*FACTOR_W+jj]`（[L42](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L42)）长度 SP_LEN×8 = 16384 = DIS_LEN；对应的 [L117-L120](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L117-L120) 用 `ARRAY_PARTITION cyclic factor=FACTOR_W` 把距离数组分成 8 个存储体，使一个字产生的 8 个写入按拍分发不冲突。注意对照 optimal 的 [L116-L121](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L116-L121)：aggressive 把距离数组的 `RESOURCE ... uram` 约束删掉了（回落默认 BRAM 实现），只有 `local_SP` 双缓冲仍锁 URAM（[L112-L115](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L112-L115)）——128 KB × 2 的距离缓冲若再占 URAM 会与 256 KB × 2 的 SP 缓冲争抢同一资源列。

**sort 级不需要拆包。** 三个变体的 `sort` 函数逐字节同构（差异只是 DIS_LEN 常量）：距离数组已按点存放，奇偶插入排序每次消费一个点（如 [aggressive_11PE/src/krnl_partialKnn.cpp:48-93](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L48-L93)）。宽口的影响止步于 compute 级——这是三级流水各司其职的体现。

#### 4.2.4 代码实践

**实践：手工推演一个 512-bit 字的拆包位图，再用 `range()` 公式核对。**

1. 实践目标：不依赖开发板，在纸上验证 4.2.2 的比特区间公式与 aggressive 源码一致。
2. 操作步骤：
   - 取一个 512-bit 字，画出 16 个 float 槽位（bit 0 在最右）；
   - 对 jj=0..7、kk=0..1 逐一标出 `range(range_idx+31, range_idx)` 的区间；
   - 标注每个点 jj 的两个 float 分别落在哪些比特；
   - 最后核对最高位点：jj=7 时 `start_idx = 7*2 = 14`，dim 0 的 `range_idx = 14*32 = 448` → 区间 [479, 448]，dim 1 的 `range_idx = 15*32 = 480` → 区间 [511, 480]——即字的**最高 32 bit 是 point 7 的 dim 1**，最低 32 bit 是 point 0 的 dim 0。
3. 需要观察的现象：点的序号增大，比特区间向高位步进 64；同一点的两个 dim 相邻 32 bit。
4. 预期结果：16 个槽位恰好被 8 个点 × 2 维填满，无空洞、无重叠；这与 `local_distance[ii*8+jj]` 共 16384 项匹配。
5. 进阶（可选）：把 `INPUT_DIM` 换成 3（需同时改 host.cpp 的数据生成），检查公式 \( 32(jD+k) \) 是否仍然无空洞——会发现 512/96 不是整数，需要补零填充，这正是为什么仓库固定 INPUT_DIM=2。

#### 4.2.5 小练习与答案

**练习 1**：为什么 optimal 把 `PIPELINE II=1` 放外层 `ii` 循环，aggressive 却放内层 `jj` 循环？

答案：流水线挂在哪层，就决定「每拍产出什么」。optimal 每字 1 点，外层每拍一个字 = 每拍一个点，吞吐 8 B/拍；aggressive 每字 8 点，若仍流水外层则一个字要 8 拍内层顺序执行（或强行展开使单拍逻辑过深），挂到内层后每拍一个点，吞吐同样 8 B/拍，而每拍需要的数据只是宽字中已取好的一段。两版殊途同归：**compute 级吞吐都是每拍一个点**。

**练习 2**：`ARRAY_PARTITION cyclic factor=FACTOR_W` 若删掉，aggressive 会发生什么？

答案：一个 `jj` 循环迭代写入 `local_distance[ii*8+jj]`，相邻迭代地址间隔 1，落在同一存储体时产生写端口冲突，内层循环无法达到 II=1，拆包循环吞吐下降，进而拖慢整条三级流水（compute 成为更深的瓶颈）。

**练习 3**：baseline 的 `compute` 里 `local_SP[ii+kk]` 与 optimal 的 `local_SP[ii].range(...)` 在「地址计算」上有什么本质区别？

答案：baseline 用**两个字**拼一个点，地址随 kk 变化（`ii+kk`），每点要发起两次 32-bit 访问；optimal 用**一个字**装一个点，地址只随 ii 变化，kk 变成了字内比特偏移。宽口的本质就是把「跨字的地址维」压缩成「字内的比特维」。

### 4.3 PE 与位宽权衡

#### 4.3.1 概念说明

要提升聚合带宽，直觉上有两个旋钮：**加宽每个端口**，或**增多端口（PE）**。四个 KNN 变体恰好把两条路线都走了一遍：

- **窄口多 PE**（baseline：32 bit × 14）：每 PE 便宜（片上仅 3 KB 缓冲），但每 PE 口带宽只有 1.2 GB/s，聚合 16.8 GB/s 连一条 DDR 通道的 ~19.2 GB/s 都吃不满。
- **宽口少 PE**（aggressive：512 bit × 11）：单 PE 口带宽 19.2 GB/s 恰好等于一条通道峰值，聚合需求高达 211.2 GB/s，但每 PE 片上缓冲暴涨到 384 KB，PE 数反而从 14 降到 11。
- **中庸平衡**（optimal：64 bit × 14 + 长突发）：聚合 33.6 GB/s 约为单通道峰值的 1.75 倍，配合 2 KB 长突发把通道效率榨满。

关键约束（u3-l3 已建立）：**所有变体的 `sp=` 都连到同一条 `DDR[0]`**，因此实际吞吐被 min(理论聚合, 通道峰值 ~19.2 GB/s) 锁顶。位宽 × PE 数的乘积一旦超过通道峰值，继续加码只是白费资源——除非把 `sp=` 分散到多条通道（U280 的 32 个 HBM 伪通道正是为此存在）。

#### 4.3.2 核心流程

权衡分析的可量化清单（数字均来自 4.1 的配置矩阵）：

```text
方向 A：增多 PE（位宽不变）
  聚合带宽 ↑ 线性        : N × (f·W/8)
  片上存储 ↑ 线性        : N × 每PE缓冲
  控制开销 ↑             : nk 行数、stream_connect 2N 条、
                           globalSort 流参数 2N 个、host 分区 N 份
  上限                   : 受单通道峰值与 CU/布线资源限制

方向 B：加宽端口（PE 数不变或减少）
  单 PE 带宽 ↑ 线性      : f·W/8
  每字点数 FACTOR_W ↑    : 拆包循环、ARRAY_PARTITION 随之加宽
  tile 粒度 ↑            : SP_LEN 字数下保持 tile 字节则点数↑，
                           双缓冲字节 ↑（或缩 SP_LEN 保点数）
  上限                   : AXI4 单突发 256 拍；通道峰值；
                           URAM/BRAM 列资源
```

每 PE 片上双缓冲对照（数据来源：`krnl_partialKnn.cpp` 数组声明 × 4.1.2 表格）：

| 变体 | local_SP ×2 | local_distance ×2 | 合计/PE | 相对 optimal |
|---|---|---|---|---|
| baseline | 2 KB | 1 KB | 3 KB | 0.5× |
| suboptimal/optimal | 4 KB | 2 KB | 6 KB | 1× |
| aggressive | 256 KB | 128 KB | 384 KB | **64×** |

aggressive 每 PE 缓冲是 optimal 的 64 倍，正好等于 tile 字节放大倍数（128 KB / 2 KB）——**宽口路线的代价不是端口本身，而是支撑宽口的深双缓冲**。这就解释了 PE 数为何从 14 回落到 11：资源预算被单个 PE 吃掉了。

#### 4.3.3 源码精读

**load 级速率由位宽直接决定。** 三个变体的 `load` 函数逐字同构（如 [aggressive_11PE/src/krnl_partialKnn.cpp:11-20](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L11-L20)）：`PIPELINE II=1` 的循环每拍搬一个 W-bit 字。于是每 PE 的读入速率上限是：

\[ r_{load} = f \times W/8 \quad (\text{B/s}) \]

baseline 4 B/拍、optimal 8 B/拍、aggressive 64 B/拍。而 compute 级三个变体都是每拍一个点 = 8 B/拍（4.2.5 练习 1）。因此：

- **optimal 是「口算平衡」设计**：load 8 B/拍 = compute 8 B/拍，读口速率与消费速率严丝合缝，一位宽不多不少。
- **aggressive 的读口有 8 倍速率冗余**：load 64 B/拍 ≫ compute 8 B/拍。单个 PE 内部这 8 倍余量用不满——它是为多通道部署准备的带宽余量（或从另一面看：当所有 PE 挤一条 DDR 通道时，先到先得的通道份额会补偿到争用的 PE 上）。

**乒乓缓冲与 tile 粒度。** [aggressive_11PE/src/krnl_partialKnn.cpp:112-115](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L112-L115)：`INTERFACE_WIDTH local_SP_0/1[SP_LEN]` 各 2048 × 64 B = 128 KB 且锁定 URAM。对照 optimal 的 [L111-L114](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L111-L114)（各 2 KB）。三级乒乓循环本身三个变体完全一致（[aggressive_11PE/src/krnl_partialKnn.cpp:131-148](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L131-L148)，u6-l1 已精读）——位宽权衡完全没有触碰流水线结构，只动了缓冲深度与字宽。

**主机与位宽解耦：案例研究对微基准的改进。** 回忆 u2-l2/u3-l2：微基准主机必须把 int 数除以 `WIDTH_FACTOR` 换算成宽字个数才能 `setArg`。KNN 主机没有这个耦合——[optimal_14PE/src/host.cpp:170-192](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp#L170-L192) 的 `num_of_points`、`partition_size`、buffer 大小（[L253-L260](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp#L253-L260)）全部以 float/点为单位，位宽换算（tile 内的字数、每字点数）收进内核常量。**改位宽只需要动 `src/krnl_config.h` 和 `src/krnl_partialKnn.cpp` 两个文件；改 PE 数却要联动 `knn.ini`（nk/stream_connect/slr/sp 四类行）+ `host.cpp`（NUM_KERNEL、分区循环、setArg 编号）+ `krnl_globalSort.cpp`（流参数个数）**——这是两条扩展路线在工程成本上的隐形差异。

**setArg 编号：PE 数耦合的活标本。** [optimal_14PE/src/host.cpp:300-301](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/host.cpp#L300-L301) 硬编码 `setArg(28, ...)`/`setArg(29, ...)`——28 = 14 PE × 2 条流；aggressive 版本 [aggressive_11PE/src/host.cpp:299-301](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/host.cpp#L299-L301) 改成 `krnl_idx = NUM_KERNEL*2` 后再 setArg(krnl_idx, ...)，把这条脆弱的隐式契约显式化了。这正说明「增多 PE」路线的连锁改动之多。

**为什么 suboptimal 的 16 拍突发「次优」。** 单突发 16 拍 × 8 B = 128 B。每发出一次突发，AXI 通道要经历地址相位、路由、响应，这些固定开销由 128 B 摊薄；optimal 用 2 KB/突发把同一开销摊薄 16 倍。当 14 个 PE 共享一条通道时，短突发还意味着更频繁的通道仲裁切换，实测有效带宽显著低于长突发版（论文 Table 2 的量化结论；本仓库不含实测数据，具体数值待查阅论文——见 README 引用）。

#### 4.3.4 代码实践

**实践：纸面设计第五个变体 `optimal_14PE` 的 128-bit 版（`128bit_14PE`），列出全部改动清单。**

1. 实践目标：把 4.1–4.3 的公式串成一个完整设计流程，验证你掌握了「位宽改动的最小侵入性」。
2. 操作步骤：
   - **契约头**：`krnl_config.h` 的 DWIDTH 改 128（NUM_KERNEL 保持 14）。
   - **tile 几何**：保持每 tile 256 点（DIS_LEN=256）不变 → 每字点数 \( F = 128/64 = 2 \)，故 FACTOR_W=2；SP_LEN = 256 点 / 2 点每字 = 128 字（单 tile 仍 2 KB）。
   - **突发**：`max_read_burst_length` 维持 256 拍 → 单突发 256 × 16 B = 4 KB，一个 tile（2 KB）单突发读完。
   - **NUM_OF_TILES**：tile 数只取决于「每 PE 字节数 / 每 tile 字节」。每 PE 数据量不变（约 2.4 MB）、每 tile 仍 2 KB，故 NUM_OF_TILES 保持 16394/14 = 1171 不变——tile 的「字数」变了（256→128），但「字节数与点数」没变。
   - **无需改动**：`knn.ini`（nk/sp/stream_connect 与位宽无关）、`host.cpp`（以点为单位）、`krnl_globalSort.cpp`。
3. 需要观察的现象：改动收敛在两个源文件内的三四处常量；理论聚合带宽变为 300 MHz × 128 bit × 14 / 8 = **67.2 GB/s**（每 PE 4.8 GB/s，load 16 B/拍 vs compute 8 B/拍，读口 2 倍冗余）。
4. 预期结果：一份不超过 6 行的 diff 计划。若你发现自己在改 `knn.ini` 或 `host.cpp`，说明把「字内比特维」误当成了「地址维」——回头检查 4.2.5 练习 3。
5. 待本地验证：有 Vitis 环境时，复制 optimal_14PE 目录应用上述改动，`make build TARGET=sw_emu DEVICE=<平台>` 至少通过编译与仿真跑通。

#### 4.3.5 小练习与答案

**练习 1**：把 aggressive 的 11 个 PE 改连到 U280 的 11 个不同 HBM 伪通道（改 `knn.ini` 的 `sp=` 与 host 的 bank flags），聚合带宽上限变为多少？

答案：每伪通道峰值约 300 MHz × 512 bit / 8 = 19.2 GB/s，11 条独立通道的聚合上限 ≈ 211 GB/s，恰好等于 aggressive 的理论聚合需求——aggressive 的「211.2 GB/s」本来就是按多通道兑现设计的；连单通道 DDR[0] 时它被锁在 ~19.2 GB/s，与 optimal 同顶。这说明**端口位宽 × PE 数的乘积必须与「可用独立通道数 × 通道峰值」匹配**，过与不及都是浪费。

**练习 2**：为什么 aggressive 宁可把 PE 从 14 降到 11，也不把 SP_LEN 缩小以省 URAM？

答案：SP_LEN 缩小则 tile 字节数下降，单 tile 的连续访问量随之变短（128 KB → 更小），突发条数不变但每 tile 更碎，通道效率与流水线充排空开销（2/(NUM_OF_TILES+2)）同时恶化——tile 粒度正是微基准五因素中的「连续访问数据量」。降 PE 数是牺牲并行度换访存效率的两难折中，aggressive 选择了保 tile 粒度。

**练习 3**：给定约束「每 PE 片上缓冲 ≤ 8 KB、DDR 通道峰值 19.2 GB/s、目标聚合 ≥ 30 GB/s、单通道部署」，应该选哪个变体的配置？

答案：无解——单通道部署下任何配置的实测都被 19.2 GB/s 锁顶，30 GB/s 目标不可能达成。若改为双通道：optimal（64 bit × 14 + 256 拍突发、6 KB/PE）拆成 7+7 两组 PE 各连一条通道即可逼近 2 × 19.2 = 38.4 GB/s 的目标区间。这道题的要点是先核对**通道数**这个硬约束，再谈位宽与 PE 数。

## 5. 综合实践

**任务：产出一份《KNN 第五变体设计书》并做同行评审。**

把 4.3.4 的纸面设计扩展成完整的设计文档，走一遍「微基准洞察 → 设计决策」的闭环：

1. **选定设计点**：自选一组参数（例如 4.3.4 的 128 bit × 14，或 256 bit × 12、64 bit × 20 等），用本讲公式算出：每 PE 口带宽、理论聚合带宽、每 PE 片上缓冲、tile 字节、FACTOR_W、SP_LEN、DIS_LEN、NUM_OF_TILES。
2. **核对通道预算**：按「全部 sp 连 DDR[0] 单通道」与「sp 分散到 N 条通道」两种部署分别给出实测带宽预期上限（min(聚合, N × 19.2 GB/s)），指出你的设计点在哪种部署下才合理。
3. **写出最小 diff 清单**：仿照 4.3.4，列出每个要改的文件与行；特别核对 setArg 编号公式（流参数 2 × NUM_KERNEL 之后）、`knn.ini` 需要增删的行数、`ARRAY_PARTITION` 的 factor。
4. **自评**：用 4.3.2 的两个清单（方向 A/B）给你的设计归类，说明你为什么选这条路线，以及哪个微基准因素（位宽/突发/连续量/端口数）是你的第一瓶颈。
5. **评审练习**：交换设计书与同伴互相挑错，重点检查三类常见错误——位维与地址维混淆（4.2.5 练习 3）、tile 点数与字数换算错（4.1.5 练习 2）、忘记 setArg 随 NUM_KERNEL 平移（4.1.3）。

## 6. 本讲小结

- 四个 KNN 变体是同一条三级流水在「端口配置」维度上的受控实验：baseline（32 bit × 14，16.8 GB/s）、suboptimal/optimal（64 bit × 14，33.6 GB/s，仅突发 16↔256 拍之差）、aggressive（512 bit × 11，211.2 GB/s 理论值）。
- `diff` 实测确认 suboptimal 与 optimal 的内核源码只差两行 `max_read_burst_length`——微基准「突发长度」因素的结论（长突发摊薄 AXI 固定开销）被直接移植成设计决策：256 拍 × 8 B = 2 KB 恰好一个 tile 一次突发读完。
- 宽端口拆包的通用公式：第 j 点第 k 维位于宽字比特区间 [32(jD+k), 32(jD+k)+31]；`FACTOR_W = DWIDTH/(32·INPUT_DIM)`，配 `ARRAY_PARTITION cyclic` 保证拆包循环 II=1。
- 两条扩展路线的账本：增多 PE 是线性买带宽、线性买存储，还要连带 ini/host/globalSort 的编号耦合；加宽端口每 PE 缓冲按 tile 字节暴涨（aggressive 是 optimal 的 64 倍），故 PE 数反而回落。
- 所有变体 `sp=` 均连单条 DDR[0]，实测吞吐被 min(理论聚合, 通道峰值) 锁顶——位宽 × PE 数的乘积必须与独立通道数匹配，这是下一讲（u6-l4）「从洞察到决策」的核心判据。
- KNN 主机以「点」为单位与位宽解耦，改位宽只动 `src/` 两个文件——相比微基准主机手动除以 WIDTH_FACTOR，这是案例研究在工程健壮性上的实质改进。

## 7. 下一步学习建议

- **下一讲 u6-l3（SpMV 案例研究）**：把本讲的「tile 乒乓 + 宽口拆包」模式换成 ELLPACK 稀疏矩阵场景，观察 UNROLL_FACTOR 与 ROWS_PER_TILE 如何扮演与 FACTOR_W/SP_LEN 对偶的角色，以及 30 PE 如何铺满 4 个 DDR bank（`spmv.ini` 终于不用挤单通道了）。
- **u6-l4（从微基准洞察到设计决策）**：本讲的权衡清单将在那里与 SpMV 四变体合流，形成完整的设计方法学；建议先把 4.3.5 三道练习做完再进入。
- **源码延伸阅读**：对比 [case_study/KNN/aggressive_11PE/src/krnl_globalSort.cpp](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_globalSort.cpp)（11 路输入）与 optimal 版（14 路输入）的 `seq_global_merge`，体会 PE 数变化如何波及归并内核的参数表；[README.md:L4](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/README.md#L4) 指向的 CHIP-KNN 项目则是这套设计方法论的产品化后续，可作为课程后的进阶阅读。
