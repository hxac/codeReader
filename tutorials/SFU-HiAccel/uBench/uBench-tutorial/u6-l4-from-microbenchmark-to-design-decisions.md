# 从微基准洞察到加速器设计决策

## 1. 本讲目标

学完本讲，你应该能够：

1. 把微基准揭示的五个带宽因素（频率、并发端口数、端口位宽、最大突发长度、连续访问数据量）逐项映射到 KNN/SpMV 案例中的具体源码参数，说清「每条洞察落在哪个文件的哪一行」。
2. 用屋顶线式的估算方法 \( BW_{ceil} = \min(\text{端口聚合峰值},\ \text{通道容量},\ \text{突发效率}) \) 推演一个加速器配置的带宽上限，并用「带宽利用率 = 实际吞吐 / 微基准峰值」评估设计效率。
3. 解释一个反直觉结论：更宽的端口（aggressive 设计）为什么可能在实测中反而次优于「刚刚好」的 optimal 设计——端口过度配置没有收益、却带来布线压力、时序收敛困难和片上缓冲膨胀。
4. 独立完成一份设计决策报告：给定平台微基准结论，为一个新内核（如 STREAM triad）论证性地选出端口数 / 位宽 / 突发长度组合。

本讲是案例研究单元的收官，不再引入新的源码机制，而是把 u3（微基准）、u6-l1～u6-l3（KNN/SpMV 架构）已经讲过的材料重新组织成一套**可复用的设计方法论**。

## 2. 前置知识

本讲假设你已理解以下概念（前序讲义已建立，这里只做一句话回顾）：

- **五因素模型**（u1-l1）：理论峰值 = 频率 × 端口数 × 位宽 / 8；突发长度与连续访问数据量决定你能吃到峰值的几成。
- **突发效率曲线**（u3-l1/u3-l2）：AXI 突发有固定的地址/响应开销，单突发搬运的字节数 = burst 拍数 × 位宽 / 8；突发太短（如 64 B）时带宽利用率显著下降，加长到 KB 级才逼近峰值。
- **通道容量锁顶**（u3-l3/u6-l2）：无论端口侧配置多强，吞吐都被 \( \min(\text{端口聚合},\ \text{所连内存通道容量之和}) \) 锁顶；多条 sp= 连到同一通道就是共享争用。
- **跨工具契约**（u6-l3）：内核 pragma（端口位宽、burst）、krnl_config.h（PE 数、tile 几何）、ini（sp 连线、nk 实例数）、主机（bank 绑定）四端联动，改一处须同步其余。
- **乒乓双缓冲**（u6-l1/u6-l3）：load 与 compute 两级用 x/y 两组片上数组交替工作，缓冲大小由 tile 几何（ROWS_PER_TILE、SP_LEN、DIS_LEN）决定。

还有一个本讲反复用到的基本换算：300 MHz 下，一个 \( W \) 位端口的单向峰值带宽为

\[ BW_{port} = f \times W/8 = 3\times 10^{8}\ \text{Hz} \times W/8\ \text{B} \]

即 32 bit → 1.2 GB/s，64 bit → 2.4 GB/s，256 bit → 9.6 GB/s，512 bit → 19.2 GB/s。本教程沿用的工程口径是：U200 ini 中可见的每条 DDR 通道峰值约 19.2 GB/s（DDR4-2400 × 64 bit），四条通道合计 76.8 GB/s。

## 3. 本讲源码地图

本讲的「源码」主要是**配置与连接**，而非新的计算逻辑：

| 文件 | 作用 |
|---|---|
| `case_study/SpMV/baseline_30PE/src/krnl_config.h` | SpMV baseline：30 PE、32 bit 端口、ROWS_PER_TILE=8、UNROLL=1 |
| `case_study/SpMV/suboptimal_4PE/src/krnl_config.h` | SpMV suboptimal：4 PE、256 bit、与 optimal 仅 burst 不同 |
| `case_study/SpMV/optimal_4PE/src/krnl_config.h` | SpMV optimal：4 PE、256 bit、UNROLL=4 |
| `case_study/SpMV/aggressive_4PE/src/krnl_config.h` | SpMV aggressive：4 PE、512 bit 宽口 |
| `case_study/SpMV/*/src/krnl_partialspmv.cpp` | 各变体内核，L89-L92 的 pragma 是四变体差异的集中地 |
| `case_study/SpMV/*/spmv.ini` | nk 实例数与 sp 通道分配（baseline 铺 4 条 DDR，4PE 变体一一对应） |
| `case_study/SpMV/*/Makefile` | L66 的 `--kernel_frequency 300` 锁定频率 |
| `case_study/KNN/aggressive_11PE/src/krnl_config.h` | KNN aggressive：512 bit、11 PE |
| `case_study/KNN/*/src/krnl_partialKnn.cpp` | KNN 各变体的 SP_LEN/DIS_LEN/FACTOR_W 与 burst pragma |
| `case_study/SpMV/README.md`、`case_study/KNN/README.md` | 指向论文 Table 4 / Table 2 的四个设计出处 |

一个先声明的事实核对结论（本讲以代码为准）：SpMV 四个变体的 `Makefile` 都显式指定了 `--kernel_frequency 300`，而 **KNN 的 Makefile 没有指定频率**（grep 全 `case_study/` 仅 SpMV 命中），因此下文对 KNN 的带宽推演按 300 MHz 假设进行，标注「待本地验证」。

## 4. 核心概念与源码讲解

### 4.1 模块一：洞察到参数映射

#### 4.1.1 概念说明

微基准的价值不在于「测出了数字」，而在于把数字变成设计约束。uBench 的论文思路是：先用 `ubench/` 里的微基准扫出平台在各位宽 × 突发 × 端口数组合下的可达带宽，再据此为真实内核（KNN、SpMV）选择端口配置。这个「翻译」过程就是**洞察到参数映射**：

- 微基准扫出的「位宽-带宽」曲线 → 决定内核 `DWIDTH`；
- 微基准扫出的「突发长度-带宽」曲线 → 决定 `max_read_burst_length`；
- 微基准扫出的「并发端口数-带宽」曲线（以及通道争用行为）→ 决定 PE 数（`NUM_KERNEL`）与 ini 的 sp 分配；
- 剩下的连续访问数据量维度 → 决定 tile 大小（SpMV 的 `ROWS_PER_TILE`、KNN 的 `SP_LEN`），保证每条流足够长以维持突发。

反过来看：案例中四个「变体」其实就是**在同一架构骨架上只拧这几个旋钮**的受控实验，这正是微基准方法学的实验化呈现。

#### 4.1.2 核心流程

设计时的映射流程可以写成一个查表过程：

```
输入：平台微基准结论（各 DWIDTH × burst × 端口数的可达带宽表）
      目标：某内存受限内核的吞吐需求 T

1. 选位宽 W：满足 单口峰值 f·W/8 ≥ 单 PE 需求 的最小档位
2. 选突发 B：微基准曲线上 使该位宽达到 ≥90% 峰值 的最小 burst
3. 选端口数/PE 数 n：n × 单口峰值 ≈ 但不超过 Σ通道容量
   （端口聚合 < 通道容量 → 端口受限；反之 → 通道受限，浪费端口）
4. 选 tile 大小：使每条访问流的连续字节数 ≥ B × W/8 的若干倍
5. 用 ini 的 sp 把各 PE 的端口摊到通道上，主机端 bank flag 对齐
```

判断设计处于哪个受限状态是核心技能：

\[ BW_{ceil} = \min\underbrace{\left(\sum_i f \cdot W_i/8\right)}_{\text{端口聚合}} ,\ \underbrace{C_{ch}}_{\text{通道容量}} ,\ \underbrace{e(B_b)\cdot C_{ch}}_{\text{突发效率折损}} \]

其中 \( e(B_b) \in (0,1] \) 是微基准给出的突发效率因子，随单突发字节数 \( B_b = B \times W/8 \) 递增。

#### 4.1.3 源码精读

先看 SpMV 四个变体的配置头如何只靠几个常量就区分出四种设计。[baseline_30PE 的 krnl_config.h:L7-L25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_config.h#L7-L25) 定义了 `DWIDTH_256 = 32`（nzval/cols 端口实际是 32 bit）、`DWIDTH_512 = 32`（vec/out 也是 32 bit）、`NUM_KERNEL = 30`、`ROWS_PER_TILE = 8`、`UNROLL_FACTOR = 1`——30 个 PE、全窄口、无片上并行，是最朴素的基线。

[optimal_4PE 的 krnl_config.h:L7-L25](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_config.h#L7-L25) 把 `DWIDTH_256` 提到 256、`NUM_KERNEL` 降到 4、`ROWS_PER_TILE` 扩到 64、`UNROLL_FACTOR` 提到 4：宽口 + 大 tile + 片上 4 路并行。[suboptimal_4PE 的 krnl_config.h:L7-L17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/suboptimal_4PE/src/krnl_config.h#L7-L17) 与 optimal **逐行相同**（256 bit、4 PE）——两者的全部差异在内核 pragma 里。[aggressive_4PE 的 krnl_config.h:L7-L17](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_config.h#L7-L17) 只再把 `DWIDTH_256` 翻倍到 512。

突发长度的落点在内核 pragma。[baseline_30PE 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L89-L92) 全部端口 `max_read_burst_length=16`，且 cols/vec/out 共享 `bundle=gmem1`（每核 2 个物理口）；[suboptimal_4PE 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/suboptimal_4PE/src/krnl_partialspmv.cpp#L89-L92) 主数据口 burst=16；[optimal 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_partialspmv.cpp#L89-L92) 提到 64；[aggressive 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_partialspmv.cpp#L89-L92) 是 512 bit × burst 32。注意 vec/out 在四个变体里都保持 32 bit——因为 [krnl_partialspmv.cpp:L111-L142](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L111-L142) 显示 vec 只在内核启动时装载一次（随后复用片上 `temp_vec`/`local_vec`），真正的持续大流量只有 nzval/cols。**只加宽吞吐主导端口**，本身就是一条微基准洞察的落地。

通道分配在 ini 里。[baseline_30PE 的 spmv.ini:L3-L25 与 L183-L195](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/spmv.ini#L183-L195) 用 `nk=krnl_partialspmv:30` 例化 30 个 CU 并摊到 DDR[0..3]；[optimal_4PE 的 spmv.ini:L3-L27](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/spmv.ini#L3-L27) 则是 4 个 CU 一一对应 4 条通道。频率锚点在 [baseline_30PE 的 Makefile:L66](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/Makefile#L66)：`--kernel_frequency 300`。

KNN 侧对应物是 [aggressive_11PE 的 krnl_config.h:L8-L15](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_config.h#L8-L15)（`DWIDTH=512`、`NUM_KERNEL=11`）与 [krnl_partialKnn.cpp:L5-L9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L5-L9)（`SP_LEN=2048`、`DIS_LEN=16384`、`FACTOR_W=8`）；optimal 的对应段在 [krnl_partialKnn.cpp:L6-L9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp#L6-L9)（`SP_LEN=256`、`DIS_LEN=256`、`FACTOR_W=1`）。另据 [baseline_14PE 的 krnl_partialKnn.cpp:L92-L104](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/baseline_14PE/src/krnl_partialKnn.cpp#L92-L104)：四个 KNN 变体的 `inputQuery` 与 `searchSpace` **共用 `bundle=gmem1`**，即每 PE 物理上只有一个读端口，且 baseline 未写 burst pragma（采用工具默认值，具体拍数待确认）——u6-l1 摘要中「28 个读端口」是把两个指针参数都计入了，物理口数以 bundle 为准，此处按代码修正为 14 个。

#### 4.1.4 代码实践

**实践目标**：亲手建立「微基准五因素 → 案例源码行」的映射表，验证你真的知道每个旋钮在哪里。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "max_read_burst_length\|DWIDTH" case_study/SpMV/*/src/ | grep -v host`，列出所有位宽与突发取值。
2. 执行 `grep -c "sp=krnl_partialspmv" case_study/SpMV/baseline_30PE/spmv.ini` 与 `grep -rn "DDR\[" case_study/SpMV/optimal_4PE/spmv.ini`，统计两变体的通道分配方式。
3. 把结果整理成五列映射表：因素 | 微基准中的形式 | SpMV 中的落点（文件:行） | KNN 中的落点 | 谁消费它（v++/g++/链接器）。

**需要观察的现象**：SpMV 四变体在 `krnl_config.h` 上的差异不超过 3 行；`N` 也随 PE 数变化（baseline 用 8400 = 30 的倍数，4PE 变体用 8192 = 4 的倍数）——矩阵规模被刻意选为能被 PE 数整除。

**预期结果**：得到一张类似下表的映射表（答案见 4.1.5 练习 1）。本实践为纯源码阅读型，无需硬件；grep 命令可在任何环境执行。

#### 4.1.5 小练习与答案

**练习 1**：写出「五因素 → SpMV 源码落点」映射表。

**参考答案**：

| 因素 | SpMV 落点 |
|---|---|
| 内核频率 | 各变体 Makefile L66 `--kernel_frequency 300`（链接期） |
| 端口位宽 | `krnl_config.h` 的 `DWIDTH_256`（L7），被 `INTERFACE_WIDTH_256` 派生为 `ap_uint` |
| 突发长度 | `krnl_partialspmv.cpp` L89-L92 的 `max_read_burst_length` |
| 并发端口数 | `NUM_KERNEL`（config L17）× 每核 bundle 数 × `spmv.ini` 的 `nk`/`sp=` |
| 连续访问量 | `ROWS_PER_TILE × L`（config L20/L23）决定每条装载流的长度 |

**练习 2**：为什么 SpMV 的 vec/out 端口在四个变体里都保持 32 bit？

**参考答案**：vec 只在内核启动时装载一次（krnl_partialspmv.cpp L111-L135，位于 `NUM_ITERATIONS` 循环之外），out 每 PE 只写 `N_OUT` 个数；持续大流量只有 nzval/cols。加宽低流量端口只会增加布线与 AXI 机器面积，不能提高吞吐——「按流量份额分配端口宽度」。

**练习 3**：baseline 与 suboptimal 的 burst 都是 16，为什么 suboptimal 的单突发字节数是 baseline 的 8 倍？

**参考答案**：单突发字节数 = burst 拍数 × 位宽 / 8。baseline 为 16 × 32/8 = 64 B；suboptimal 为 16 × 256/8 = 512 B。突发效率由**字节数**而非拍数决定，所以「位宽 × burst」要作为整体来选。

### 4.2 模块二：带宽利用率评估

#### 4.2.1 概念说明

有了映射，就能对任意配置算出一个**解析带宽上限**，再与实测吞吐相除得到**带宽利用率**：

\[ \eta = \frac{BW_{actual}}{BW_{ubench\ peak}} \]

其中分母「微基准峰值」取该配置（位宽、burst、端口数、通道分配）下微基准实测可达的最大带宽。\( \eta \) 低说明设计没有吃满内存系统——可能是 tile 太短、乒乓断流、片上并行度不足（UNROLL 太小、浮点累加链太长）；\( \eta \) 接近 1 且吞吐仍不够，则说明必须换更宽的口或更多通道。这就是「用数字代替感觉」做设计迭代。

#### 4.2.2 核心流程

评估一个变体的步骤：

1. 数物理端口：每核不同 bundle 名的 m_axi 口数 × `NUM_KERNEL`（以 ini 的 nk 为准）。
2. 算端口聚合峰值 \( \sum_i f W_i/8 \)。
3. 从 ini 的 sp 行数出独立通道数，算通道容量 \( C_{ch} \)。
4. 取 \( \min \) 得上限，再乘突发效率 \( e(B_b) \) 修正（\( e \) 来自微基准曲线，定性使用）。
5. 与实测吞吐相除得 \( \eta \)（实测值见论文表格，仓库未附带，待本地验证）。

#### 4.2.3 源码精读

先把 SpMV 四变体的关键量列成表（全部取自上面 4.1.3 引用的配置与 pragma，频率 300 MHz，通道容量按 4 × 19.2 = 76.8 GB/s）：

| 变体 | 主口位宽 | PE | burst(主口) | 单突发字节 | 读口数 | 端口聚合峰值 | 通道容量 | 受限层 |
|---|---|---|---|---|---|---|---|---|
| baseline_30PE | 32 bit | 30 | 16 | 64 B | 60 | 72 GB/s | 76.8 GB/s | 端口 + 突发 |
| suboptimal_4PE | 256 bit | 4 | 16 | 512 B | 8(+4 窄) | 76.8 GB/s | 76.8 GB/s | 突发 |
| optimal_4PE | 256 bit | 4 | 64 | 2 KB | 8(+4 窄) | 76.8 GB/s | 76.8 GB/s | 通道（恰好匹配） |
| aggressive_4PE | 512 bit | 4 | 32 | 2 KB | 8(+4 窄) | 153.6 GB/s | 76.8 GB/s | 通道（2× 过配置） |

逐行解读：

- **baseline**：30 × 2 bundle = 60 个 32 bit 口，聚合 60 × 1.2 = 72 GB/s，已经低于通道容量；再叠加 64 B 的超短突发（\( e \) 很小），双重折损。它演示的是「窄口多 PE」这条路在端口侧就先撞墙。
- **suboptimal**：端口聚合 8 × 9.6 = 76.8 GB/s 恰好等于通道容量——但 burst 只有 16 拍（512 B），微基准告诉我们这个突发长度吃不到峰值，所以叫 suboptimal。它与 optimal 的源码差异只有 [krnl_partialspmv.cpp:L89-L90](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_partialspmv.cpp#L89-L90) 两行 pragma 里的 64 对 16。
- **optimal**：位宽 × 端口数的乘积刚好匹配 4 通道容量（76.8 = 76.8），突发 64 拍（2 KB）足以摊薄 AXI 开销。这是「用微基准结论反推配置」的正面教材。
- **aggressive**：主口翻倍到 512 bit，端口聚合 153.6 GB/s 是通道容量的整整 2 倍——多出来的 76.8 GB/s **在物理上不可能兑现**，因为瓶颈已在通道侧。更妙的是它的 burst=32 × 64 B = 2 KB，单突发字节数与 optimal 完全相同：加宽端口换来的「每拍更多字节」被减半的 burst 拍数抵消，突发效率没有任何提升。

KNN 一侧（频率未在 Makefile 指定，按 300 MHz 推演，待本地验证；所有变体 sp 均指向单条 DDR[0]，通道容量 ≈ 19.2 GB/s）：

| 变体 | 位宽 | PE | burst | 端口聚合峰值 | 单通道容量 | 受限层 |
|---|---|---|---|---|---|---|
| baseline_14PE | 32 bit | 14 | 未指定(默认，待确认) | 16.8 GB/s | 19.2 GB/s | 端口 |
| suboptimal_14PE | 64 bit | 14 | 16 | 33.6 GB/s | 19.2 GB/s | 通道 + 突发 |
| optimal_14PE | 64 bit | 14 | 256 | 33.6 GB/s | 19.2 GB/s | 通道（突发充足） |
| aggressive_11PE | 512 bit | 11 | 32 | 211.2 GB/s | 19.2 GB/s | 通道（11× 过配置） |

KNN 的对照更尖锐：optimal 只用 64 bit 口就把端口聚合推到通道容量的 1.75 倍，而 aggressive 的 11 × 19.2 = 211.2 GB/s 是单通道容量的 11 倍——共享单通道时，端口侧的天文数字毫无意义。

#### 4.2.4 代码实践

**实践目标**：用脚本自动完成上面的推演，得到四变体聚合带宽上限的排序。

**操作步骤**（示例代码，非项目原有）：

```python
# spmv_ceiling.py —— 示例代码：从 config 推演 SpMV 各变体带宽上限
F = 300e6  # SpMV Makefile 锁定 300MHz
variants = [  # (名字, 主口位宽, 主口数, 通道数)
    ("baseline_30PE",    32,  60, 4),
    ("suboptimal_4PE",  256,   8, 4),
    ("optimal_4PE",     256,   8, 4),
    ("aggressive_4PE",  512,   8, 4),
]
CH = 19.2  # GB/s，每条 DDR 通道
for name, w, n, ch_n in variants:
    agg = n * F * (w / 8) / 1e9      # 端口聚合峰值
    cap = CH * ch_n                  # 通道容量之和
    print(f"{name:16s} 端口聚合 {agg:6.1f} GB/s | 通道容量 {cap:5.1f} | "
          f"上限 {min(agg, cap):5.1f} | {'端口受限' if agg < cap else '通道受限'}")
```

运行 `python3 spmv_ceiling.py`，再手工把突发修正（64 B → 明显折损；512 B → 中等；2 KB → 接近峰值）作为第二道乘法叠加上去。

**需要观察的现象**：端口聚合上限的排序是 aggressive(153.6) > optimal = suboptimal(76.8) > baseline(72)；叠加突发修正后，解析排序变为 **optimal ≥ aggressive > suboptimal > baseline**。

**预期结果**：与论文 Table 4（[SpMV/README.md:L3](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/README.md#L3) 指出四个设计出自该表）报告的实测相对次序对照：optimal 最优，aggressive 因下述 4.3 的原因实测可能低于解析上限、次优于 optimal。仓库内不含论文数值，**具体数字待本地验证**（有 U200 真机时可跑 `make check TARGET=hw` 取吞吐）。

#### 4.2.5 小练习与答案

**练习 1**：aggressive_4PE 的端口聚合是 optimal 的 2 倍，为什么其带宽上限不可能超过 optimal？

**参考答案**：两者 sp 都连到同样 4 条 DDR 通道，通道容量同为 76.8 GB/s；\( BW_{ceil} = \min(153.6, 76.8) = 76.8 = \min(76.8, 76.8) \)。端口聚合超过通道容量的部分是纯过剩配置，不产生任何吞吐收益。

**练习 2**：KNN baseline 的端口聚合（16.8 GB/s）低于通道容量（19.2 GB/s），这说明什么？如果要修复，动哪个参数最划算？

**参考答案**：说明 baseline 是**端口受限**——即使通道空闲，14 个 32 bit 口也供不满。最划算的旋钮是位宽（DWIDTH 32→64 后聚合变 33.6 GB/s，越过通道容量），这正是 suboptimal/optimal 的做法；或增加 PE 数，但那会牵连 ini 的 nk 与流归并结构，改动成本高得多。

**练习 3**：定义带宽利用率 \( \eta = BW_{actual}/BW_{ubench\ peak} \) 时，为什么分母用「该配置下的微基准峰值」而不是「通道理论容量」？

**参考答案**：通道理论容量是物理上限，任何设计都达不到；微基准峰值是「与被测内核相同的位宽/burst/端口配置」下实测可达到的带宽，包含突发效率、流水爬坡等真实折损。用它做分母，\( \eta \) 衡量的才是**内核自身**（tile 组织、乒乓、片上并行）离「内存系统能给的就差多少」，而不是把 AXI 开销也算作内核的罪。

### 4.3 模块三：过优化风险

#### 4.3.1 概念说明

「过优化」（over-engineering）指把某一维参数推到远超瓶颈所在的位置：它不能提高吞吐，却按别的维度收费。本案例里有两笔账：

1. **资源与布线账**：512 bit 口意味着更宽的 AXI 互连、更多的寄存器与布线通道。SpMV aggressive 与 optimal 的片上缓冲大小完全相同（ROWS_PER_TILE=64、UNROLL=4 都一样），多出来的面积全花在「永远跑不满的端口」上；布线压力上升会侵蚀时序裕量，300 MHz 的频率目标更难收敛。
2. **片上缓冲账（KNN）**：加宽端口后，为了喂饱一个 512 bit 口，tile 必须加大——KNN aggressive 的 `SP_LEN` 从 256 涨到 2048、`DIS_LEN` 从 256 涨到 16384（见 [krnl_partialKnn.cpp:L6-L9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L6-L9) 与 optimal 的对照），双缓冲随之膨胀，URAM/BRAM 容量逼着 PE 数从 14 降到 11。宽口「吃掉」了并行度。

#### 4.3.2 核心流程

过优化的因果链：

```
端口位宽 ↑↑ （远超通道容量所需）
  → 端口聚合 ≫ 通道容量，吞吐增益 = 0
  → 互连/布线面积 ↑ → 时序裕量 ↓ （300MHz 收敛风险 ↑）
  → （若靠大 tile 喂口）片上双缓冲 ↑↑ → PE 数 ↓ → 并行度损失
  → 实测 ≤ "刚好匹配"的配置，甚至更差
```

判断一个配置是否「刚刚好」的准则：**端口聚合峰值略高于（而非数倍于）通道容量，且单突发字节数落在微基准效率曲线的饱和区**。

#### 4.3.3 源码精读

SpMV 侧的直接证据是「一字之差，白翻一倍」：对比 [aggressive_4PE 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_partialspmv.cpp#L89-L92) 与 [optimal_4PE 的 krnl_partialspmv.cpp:L89-L92](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/optimal_4PE/src/krnl_partialspmv.cpp#L89-L92)：位宽 512 对 256，burst 32 对 64——**乘积相同（都是 2 KB/突发）**，也就是说 aggressive 在突发效率维度上没有任何改善，却把互连宽度翻倍。而两变体 [krnl_config.h](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/aggressive_4PE/src/krnl_config.h#L17-L25) 的 `NUM_KERNEL=4`、`ROWS_PER_TILE=64`、`UNROLL_FACTOR=4` 完全一致，片上缓冲（[krnl_partialspmv.cpp:L99-L109](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/SpMV/baseline_30PE/src/krnl_partialspmv.cpp#L99-L109) 的 x/y 双组数组）一字不差。结论：aggressive 相对 optimal 的全部变化就是「用 2 倍的端口宽度和布线，换取 0 的带宽上限提升」。

KNN 侧的证据链在配置比例上：[aggressive_11PE 的 krnl_config.h:L8-L12](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_config.h#L8-L12) 是 `DWIDTH=512`、`NUM_KERNEL=11`；每个 PE 只有一个读口（query 与 searchSpace 共用 `bundle=gmem1`，burst=32，见 [krnl_partialKnn.cpp:L101-L102](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L101-L102)），所有口仍共享单条 DDR[0]（≈19.2 GB/s）。端口聚合 211.2 GB/s 是通道容量的 11 倍；同时 `SP_LEN×DIS_LEN` 相对 optimal（256×256）放大到 2048×16384，双缓冲膨胀迫使 PE 数从 14 缩到 11、`NUM_OF_TILES` 从 16394/14 缩到 264/11=24（[krnl_partialKnn.cpp:L9](https://github.com/SFU-HiAccel/uBench/blob/57fc9b5be6c902af56dbf6c87f152fd0bbcad1a3/case_study/KNN/aggressive_11PE/src/krnl_partialKnn.cpp#L9)）。这正是「宽口吃掉 PE」的完整标本。

需要强调的边界：以上是**机理层面的论证**；aggressive 在实测中究竟比 optimal 差多少，取决于布线 congestion 对时序的实际影响，仓库内未附带测量数据，具体数字以论文 Table 2/Table 4 与真机复测为准（待本地验证）。

#### 4.3.4 代码实践

**实践目标**：用 diff 亲手确认「四变体之间的源码差异小得惊人」，体会受控实验的设计。

**操作步骤**：

1. `diff case_study/SpMV/optimal_4PE/src/krnl_partialspmv.cpp case_study/SpMV/aggressive_4PE/src/krnl_partialspmv.cpp`——预期只有 L89-L90 两行的 burst 数字与位宽相关行不同。
2. `diff case_study/SpMV/suboptimal_4PE/src/krnl_config.h case_study/SpMV/optimal_4PE/src/krnl_config.h`——预期完全无差异。
3. `diff case_study/KNN/suboptimal_14PE/src/krnl_partialKnn.cpp case_study/KNN/optimal_14PE/src/krnl_partialKnn.cpp`——预期只有 `max_read_burst_length` 的 16 对 256。
4. 对每处 diff 标注：它改变的是五因素中的哪一个、影响端口聚合还是突发效率还是片上缓冲。

**需要观察的现象**：SpMV 的 suboptimal→optimal 差异比 KNN 的还小（两行 pragma 里的两个数字）；KNN aggressive 的 diff 则铺满 `SP_LEN/DIS_LEN/FACTOR_W` 与整段拆包逻辑。

**预期结果**：四组 diff 全部可在本仓库工作区完成，无需硬件；差异行数与 4.2.3 两张表逐项对得上。若你的 diff 出现额外行，说明仓库与本讲义所述 HEAD（57fc9b5）之后有更新，应以下列命令核对：`git log --oneline -3`。

#### 4.3.5 小练习与答案

**练习 1**：SpMV aggressive 把位宽翻倍、burst 减半（256×64 → 512×32），单突发字节数不变。为什么说这个组合「在突发维度上零收益」？

**参考答案**：AXI 突发的开销（地址阶段、响应间隔）按「突发次数」计，收益按「每次搬运的字节数」计。两配置每突发都是 2 KB，搬运同样数据量的突发次数相同、开销相同，突发效率相同；而位宽翻倍使互连与位切片逻辑（`range()` 拆包从 8 路变 16 路）变贵。支出增加、收益为零。

**练习 2**：KNN aggressive 的 PE 数为什么是 11 而不是 14？

**参考答案**：`SP_LEN=2048、DIS_LEN=16384` 使每 PE 的双缓冲（load 级 URAM 数组）远大于 14 PE 版本（256×256 量级），器件 URAM/BRAM 容量装不下 14 份，只能降到 11 份。这演示了「端口宽度—tile 大小—PE 数」三者被片上存储耦合：想喂宽口就要大 tile，大 tile 就少 PE。

**练习 3**：给出一条通用的「反过优化」检查清单。

**参考答案**：① 端口聚合峰值 / 通道容量 ∈ [1, 1.5]（留少量裕量防抖动，勿数倍）；② 单突发字节数落在微基准效率曲线饱和区（本案例 ≥2 KB）；③ 加宽任何端口前先问该端口承载的流量份额（vec/out 教训）；④ 改完先算 \( \min \) 再综合——如果上限没变，就不要动这根旋钮。

## 5. 综合实践

撰写一份**设计决策报告**，把本讲方法走完整遍（纯文档 + 推演型实践，无需硬件）：

1. **STREAM triad 设计**。给定平台微基准结论：512 bit 端口在 burst=256 时达到单口峰值 19.2 GB/s；窄口（32/64 bit）多 PE 因共享 bank 争用，聚合上限约为独立通道数的 19.2 倍；突发 <512 B 时效率明显下降。为一个假想的 triad 内核（`a[i] = b[i] + scalar*c[i]`，两读一写、流式连续访问）选出端口数 / 位宽 / 突发长度 / tile 大小，要求：(a) 列出每个选择的依据（引用上述哪条微基准结论）；(b) 算出端口聚合、通道容量与 \( BW_{ceil} \)；(c) 说明读写混在一个 bundle 还是分 bundle（提示：回忆 u3-l4 的写带宽与 u4-l1 的流式访问特性）；(d) 给出对应的 `krnl_config.h` 片段、内核 pragma 片段与 ini 的 sp/nk 行（示例代码，标注非项目原有）。
2. **SpMV 排序推演**。用 4.2.4 的脚本（或手算）完成四个 SpMV 变体的聚合带宽上限排序，再叠加突发修正得出预测的实测排序，与论文 Table 4 的结论对照；明确标注哪些是解析可得、哪些待本地验证。
3. **自查**。用 4.3.5 练习 3 的清单逐条检查你的 triad 设计，特别是端口聚合/通道容量比。

## 6. 本讲小结

- 微基准五因素的每一项都在案例源码里有确切落点：频率在 Makefile 的 `--kernel_frequency`，位宽在 `krnl_config.h` 的 DWIDTH，突发在内核 pragma，端口数在 `NUM_KERNEL` × bundle × ini 的 nk/sp，连续访问量在 tile 几何（ROWS_PER_TILE、SP_LEN）。
- 带宽上限由三层取最小决定：\( \min(\text{端口聚合},\ \text{通道容量},\ \text{突发效率}\times\text{通道容量}) \)；带宽利用率 \( \eta = \text{实测}/\text{同配置微基准峰值} \) 衡量内核离内存系统上限的距离。
- SpMV 四变体是一组受控实验：baseline 端口受限（72 < 76.8）叠加 64 B 短突发；suboptimal 端口匹配但突发 512 B 不足；optimal 端口恰好匹配且 2 KB 突发；aggressive 端口 2× 过配置、单突发字节数与 optimal 相同、收益为零。
- 过优化有两笔账：SpMV aggressive 是「互连翻倍、上限不变」的布线/时序账；KNN aggressive 是「宽口逼大 tile、大 tile 吃掉 PE（14→11）」的片上缓冲账。
- 好配置的准则是「刚刚好」：端口聚合略高于通道容量、单突发字节数在效率曲线饱和区、按流量份额分配端口宽度。
- 解析推演给排序，真机实测给数字：仓库不含论文测量数据，结论的最后一公里必须 `make check TARGET=hw` 落地（待本地验证）。

## 7. 下一步学习建议

本讲完成了案例研究单元，推荐两个方向收尾整套手册：

1. 学 u7-l1（仓库公共设施），理解 `common/includes` 与 `common/utility` 如何支撑这些工程的构建与文档，为自建工程打地基。
2. 学 u7-l2（创建你自己的微基准）与 u7-l3（测量方法学批判）：前者把第 5 节的 triad 设计真正变成一个五件套工程并接入 auto_collect；后者补上本讲刻意略过的测量误差问题（主机计时含启动开销、仿真数值无物理意义），让「实测吞吐」这个分母本身也经得起推敲。
