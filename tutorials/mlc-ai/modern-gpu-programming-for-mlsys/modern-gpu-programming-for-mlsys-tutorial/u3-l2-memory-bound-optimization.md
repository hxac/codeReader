# 内存受限内核的优化（u3-l2）

## 1. 本讲目标

学完本讲，你应该能够：

1. 对一条组合算子链（如 `x@W → GeLU → reduction`）**逐阶段记账 HBM 字节**，量化算子融合（fusion）前后的流量节省与理论加速比。
2. 说清 memory-bound 内核的两条出路——**减少 HBM 流量抬高算术强度**，以及流量减不动时**把实际搬运率做到接近带宽屋顶**——并理解数据复用（reuse）与算子融合（fusion）各自扮演的角色。
3. 用「字节 / FLOP」估算解释实测加速：明白**字节比不等于时间比**，省下的字节只有落在关键路径上才能兑换成时间。

本讲是单元三的第二讲。u3-l1 回答了「瓶颈在哪一侧」（AI 与拐点 250 的比较）；本讲回答「判到左侧（memory-bound）之后怎么办」。本讲所有实践只需要纯 Python（可选 matplotlib），**不需要 GPU**。

## 2. 前置知识

本讲直接站在 u3-l1 的结论上，只做最小回顾、不再重新推导：

- **roofline 与拐点**：内核性能上限 \( \le \min(\text{算力峰值},\ \text{带宽} \times \text{AI}) \)；B200 取整值 2 PFLOP/s 与 8 TB/s 给出拐点 ≈ 250 FLOP/byte。AI 低于拐点 → memory-bound，**性能 = 带宽 × AI**，被斜线封顶。
- **算术强度（AI）记账**：分子是数学运算量（乘/加各 1 FLOP、FMA 计 2），分母绑定内存层级（本讲默认 HBM）。reduction ≈ 0.25、elementwise ≈ 1、GEMM 方阵 \(N/3\)、tile 级 \(\approx B/s\)——这四个数 u3-l1 都已算过，本讲直接引用。
- **HBM 往返（round trip）**：一个中间张量若落盘，「写一次 + 读一次」= 每元素 \(2s\) 字节的流量（\(s\) 为每元素字节数）。这是本讲最重要的记账单位。
- **eager 执行的背景知识**（外部框架常识，非本书内容）：PyTorch 逐算子（eager）执行时，每个算子对应一次独立的 kernel launch，算子输出会完整物化到显存（HBM）。这正是「中间张量往返」在 everyday 代码里的来源；融合则是编译器（如 torch.compile、TVM/TIRx）或手写内核的主要优化手段之一。
- **片上存储回顾**（u2-l2）：寄存器、SMEM、TMEM 是「中间结果可以留下来」的地方——融合的本质就是让中间值停留在这些层级，不下去 HBM。

## 3. 本讲源码地图

本讲的主战场是性能章正文的「Optimizing Memory-Bound Kernels」一节（u3-l1 只借用了它的 tile 公式，本讲正面精读）：

| 文件 | 作用 |
| --- | --- |
| [chapter_performance/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md) | 本章正文；[L150-L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L150-L246) 是本讲全部三个模块的原文出处 |
| [zh/chapter_performance/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md) | 中文镜像，对应段落为 [L121-L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L121-L190)（中英同构，见 u1-l2） |
| [chapter_gemm_basics/index.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md) | GEMM Step 1 内核；其 epilogue 写回段 [L132-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L132-L143) 是书中「elementwise 操作融合进 epilogue」的真实现场 |
| [img/scripts/gen_roofline.py](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/img/scripts/gen_roofline.py) | u3-l1 已精读的屋顶参数（2000 TFLOP/s、8 TB/s），本讲实践沿用同一组取整值 |

## 4. 核心概念与源码讲解

### 4.1 访存削减：memory-bound 内核的两条出路

#### 4.1.1 概念说明

u3-l1 的结论是：AI 低于拐点的内核被**带宽斜线**封顶，性能 ≈ 带宽 × AI。看这个乘积就知道出路只有两个因子可动：

1. **减少 HBM 字节（抬高 AI）**——同一个分子除以更小的分母。手段包括算子融合（4.2）、数据复用（4.3）、以及更小的 dtype。
2. **把实际搬运率做到接近带宽上限**——字节已经减不动时，让每一字节搬得尽可能快（合并访存、TMA、保持足够的在途请求）。

正文的章节纲要一句话就是这两条路：低 AI 内核的「优化重点是减少 HBM 流量、提高复用、融合操作，并尽量接近内存带宽上限」（[chapter_performance/index.md:L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L8)，中文镜像 [zh/chapter_performance/index.md:L8](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L8)）。

还有一个重要的推论放在本节末尾：**一旦内核已经贴到带宽屋顶，再优化计算部分就不会有任何收益，唯一的提速方法是改变算法、搬更少的字节**。这句话是整个 memory-bound 优化世界的「宪法」。

#### 4.1.2 核心流程

对已判定 memory-bound 的内核（u3-l1 三步流程的第 2 步产出），决策树是：

```text
问: HBM 字节能不能减？
├── 能 → 融合（4.2）/ 复用（4.3）/ 更小 dtype
│        → AI 上升 → 斜线屋顶整体上移
└── 不能（纯 copy、简单 elementwise、single-pass reduction）
         → 目标转为有效带宽:
            ① 每 byte 只搬一次   ② coalesced/vectorized 访存
            ③ 规则大块用 TMA     ④ 保持足够多的在途 memory request
已贴住带宽屋顶还想更快 → 只能换算法（减字节），别再碰计算侧
```

#### 4.1.3 源码精读

**（1）两条出路的原文。** [chapter_performance/index.md:L150-L154](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L150-L154) 开宗明义：确定 memory-bound 后，优化有两条路——**减少 HBM 搬运量以提高算术强度**；或搬运量无法再减时，**让实际数据传输速度尽可能接近带宽上限**。中文镜像对应 [zh/chapter_performance/index.md:L121-L123](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L121-L123)。

**（2）更小的 dtype。** [chapter_performance/index.md:L230-L235](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L230-L235)：fp32 → fp16/fp8/fp4 直接减少搬运量、提高每 byte 的有效计算量；但若低精度格式需要 scale factor 等元数据（block-scaled fp8/fp4 就需要），实际收益会低于按 dtype 大小的估算。u3-l1 练习 2 已算过 \(B/s\) 口径下 fp8 让 AI 翻倍——本节只需记住：**dtype 减半 → 字节减半 → AI 翻倍**，且这是三个「减字节」手段中改动最小的一个。中文镜像 [zh/chapter_performance/index.md:L181](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L181)。

**（3）「宪法」条款。** [chapter_performance/index.md:L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L246)：memory-bound 内核一旦达到内存屋顶，进一步优化计算没有帮助；**唯一能让它更快的方法是改变算法、让它搬更少的字节**。中文镜像 [zh/chapter_performance/index.md:L190](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L190)。

#### 4.1.4 代码实践：三个 standalone 算子的字节账本

1. **实践目标**：对最简单的 memory-bound 算子手工记账字节与 AI，验证「少搬字节 = 抬 AI = 抬上限」，并把 u3-l1 的 reduction AI ≈ 0.25 复核一遍。

2. **操作步骤**：运行以下**示例代码**（纯 Python）：

   ```python
   # 示例代码：bytes_check.py —— standalone memory-bound 算子的字节与 AI
   BW_TB_S, PEAK_TFLOPS = 8.0, 2000.0

   def report(name, flops, bytes_):
       ai = flops / bytes_
       roof = min(PEAK_TFLOPS, BW_TB_S * ai)      # TB/s × FLOP/byte = TFLOP/s
       print(f"{name:22s} bytes={bytes_/2**20:7.1f} MiB  AI={ai:.3f}  "
             f"roof={roof:5.2f} TFLOP/s  mem-time={bytes_/(BW_TB_S*1e12)*1e6:.2f} us")

   n = 4096 * 4096                                 # 元素个数
   report("residual add fp16", n,       3*2*n)     # 读 x + 读 w + 写 y，1 FLOP/元素
   report("residual add fp32", n,       3*4*n)     # 同一算子，dtype 加倍
   report("sum reduction fp16", n - 1,  2*2*n)     # 沿用 u3-l1 的读写各一遍口径(2sn)
   ```

3. **需要观察的现象**：同一数学运算换 dtype 后字节、AI、屋顶三列如何联动；reduction 的 AI 是否与 u3-l1 的表格一致。

4. **预期结果**（手算可复核）：

   | 算子 | 字节 | AI (FLOP/byte) | 屋顶 | 纯搬运时间 @8TB/s |
   | --- | --- | --- | --- | --- |
   | residual add fp16 | 96.0 MiB | 0.167 | 1.33 TFLOP/s | 12.6 µs |
   | residual add fp32 | 192.0 MiB | 0.083 | 0.67 TFLOP/s | 25.2 µs |
   | sum reduction fp16 | 64.0 MiB | 0.250 | 2.00 TFLOP/s | 8.4 µs |

   三个 AI 都远低于拐点 250——它们被斜线死死封顶，dtype 减半让字节减半、AI 与屋顶翻倍。reduction 的 0.25 与 u3-l1 4.3.2(b) 一致（读写各一遍的保守口径 \(2sn\)）；若按严格「只读大张量」口径（\(sn\) 字节）则 AI = 0.5——两种口径都离拐点差两三个数量级，**分类对口径稳健**（u3-l1 已强调过这一点）。实际打印格式因环境略有差异——**待本地验证**。

5. 注意记账口径：residual add 读两个张量写一个（\(3sn\) 字节），比「读一写一」的 elementwise 多一个读操作，AI 更低。

#### 4.1.5 小练习与答案

**练习 1**：把 residual add 从 fp16 改成 fp8（\(s=1\)，暂不考虑 scale factor），三行表格怎么变？

**答案**：字节 96 → 48 MiB，AI \(1/(3s)\) 从 0.167 → 0.333，屋顶 1.33 → 2.67 TFLOP/s。FLOP 数完全不变，减字节直接线性抬 AI（依据 [chapter_performance/index.md:L230-L235](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L230-L235)）；若 fp8 需要 scale factor 元数据，实际收益会低于这个翻倍。

**练习 2**：为什么把 reduction 的求和循环「写得更 clever」（比如重新结合加法）几乎不会让内核变快？

**答案**：AI ≈ 0.25，远低于拐点 250，性能 = 带宽 × AI，瓶颈在分母（字节）而不在分子（FLOP）。依据「宪法」条款（[chapter_performance/index.md:L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L246)）：已受内存限制时优化计算侧无帮助，要快只能搬更少字节。

**练习 3**：一个 elementwise 内核实测只达到 70% 峰值带宽（u3-l1 图中红点的 0.7 因子），接下来该做什么？

**答案**：先走 4.1.2 决策树的右支——检查四条搬运效率清单（每 byte 只搬一次、coalesced/vectorized、规则大块用 TMA、保持在途请求，见 4.3.3 的原文）把剩下的 30% 拿回来；贴到带宽屋顶后，若还想更快就回到左支：融合进相邻算子（4.2）抬 AI。

### 4.2 算子融合：消灭中间张量的 HBM 往返

#### 4.2.1 概念说明

低 AI 最常见的来源不是单个算子本身，而是**中间张量的落盘往返**：一个内核把中间结果写进 HBM，下一个操作又立刻把它读回来。这一写一读各值 \(s|I|\) 字节（\(|I|\) 为中间张量元素数），却常常不产生任何新的数学价值。

**算子融合（fusion）** 就是把产生中间结果的操作（producer）和消费它的操作（consumer）放进同一个内核，让中间值**留在寄存器或片上存储（SMEM、TMEM）里直接交接**，整笔 \(2s|I|\) 的 HBM 往返随之消失。这是减字节三个手段中最直接的一个——它不改变任何数学，只是把数据的「卸货点」从 HBM 挪到片上。

正文给出三个典型融合点：GEMM 拼 elementwise epilogue、normalization 融进相邻算子、attention 不生成完整 score matrix。第三个是算法级的最大案例——Flash Attention 的核心动机。

#### 4.2.2 核心流程

融合的收益可以用一本「字节账本」精确记账：

```text
设中间张量 I 有 |I| 个元素、每元素 s 字节，链上共 k 个中间张量

未融合（eager, 每算子一个内核）:
    每个中间张量: producer 写 s|I| + consumer 读 s|I| = 2s|I| 字节往返
    总流量 = 输入读入 + 输出写出 + Σ 2s|I_j|

融合后（单内核）:
    中间值经寄存器/SMEM/TMEM 交接 → 每个 |I_j| 贡献 0 字节
    总流量 = 输入读入 + 输出写出

性能模型（两版本各自算时间下界）:
    t = max( FLOP / 算力屋顶, bytes / 带宽 )
    注意: 字节比 ≠ 时间比——删掉的流量只有落在关键路径上才兑换成时间
```

「时间 = 两条屋顶的较低者」这一步就是 u3-l1 的 roofline 不等式反过来用：给定一个版本的 FLOP 与字节，\(t \ge \max(\text{FLOP}/\text{PEAK},\ \text{bytes}/\text{BW})\)。融合改的是第二项，但最终时间由两项的**较大者**决定——这正是「字节比 ≠ 时间比」的数学根源，4.2.4 的实践会给出两个鲜明对比的例子。

#### 4.2.3 源码精读

**（1）融合的定义与机理。** [chapter_performance/index.md:L156](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L156)：融合是最直接的方法；低 AI 的常见来源是一个内核把中间张量写入 HBM、下一个操作立刻读回；把 producer 与 consumer 融合后，中间值可保留在**寄存器或片上存储（如 SMEM、TMEM）**，避开 HBM 往返。中文镜像 [zh/chapter_performance/index.md:L125](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L125)。

**（2）三个融合点。** [chapter_performance/index.md:L158-L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L158-L160)：GEMM + elementwise epilogue；normalization 融进相邻算子；attention 不物化完整 score matrix。中文镜像 [zh/chapter_performance/index.md:L127-L129](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L127-L129)。

**（3）算法级最大案例：attention。** [chapter_performance/index.md:L141-L144](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L141-L144)：标准 attention 的主要性能成本是 `QK^T` 产生的 score 矩阵——写回 HBM 再读回造成大量流量；Flash Attention（含 FA4）把相关 tile 留在片上、避开往返，从而抬高 AI。用 u3-l1 4.3.2(d) 的定量结果读：naive ≈ 62、flash ≈ 2048 FLOP/byte，**仅「S/P 是否落盘」一项就跨过拐点**——这是融合威力的一次完整展示（工程实现见单元十四）。

**（4）书中的融合现场：GEMM epilogue。** 「GEMM + elementwise epilogue」在本书 GEMM 章节有真实代码锚点。[chapter_gemm_basics/index.md:L43](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L43) 定义了 epilogue：writeback 阶段把结果从 TMEM 读进寄存器、再存回 GMEM；而 [chapter_gemm_basics/index.md:L132-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L132-L143) 的 Step 1 代码里，fp32→fp16 的类型转换 `Tx.cast(Dreg_f16[:], Dreg[:])` 就是在寄存器里完成的 elementwise 操作：

   ```python
   Dreg = T.alloc_local((BLK_N,), acc_type)        # 每线程 fp32 寄存器行
   ...
   Tx.cast(Dreg_f16[:], Dreg[:])                   # ← elementwise 转换，发生在寄存器里
   T.meta_var(m_st + warp_id * 32 + lane_id)
   Tx.copy(D[m_thr, n_st : n_st + BLK_N], Dreg_f16[:])
   ```

   这个 cast 没有为「类型转换」多搬一个字节的 HBM——因为它住在 epilogue 里、住在寄存器中。若要在 GEMM 后接 GeLU，插入点就在 `Tx.cast` 旁边：数据已经在寄存器里，GeLU 是零额外 HBM 流量的。这就是第一条融合 bullet 的现场形态。

**（5）估算之后要实测。** [chapter_performance/index.md:L342-L344](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L342-L344)：roofline 解读要从可信的测量出发，计时、用 Proton 定位昂贵 launch、用 Nsight Compute 检验硬件假设的完整工作流收在基准测试附录。本讲的字节账本给出**预测**，附录（u15-l4/l5）给出**实测**——两者对上，优化才算闭环。

#### 4.2.4 代码实践：`x@W → GeLU → reduction` 融合前后的流量与加速（本讲主实践）

1. **实践目标**：对规格中给定的组合算子建立完整的字节账本，量化融合节省的 HBM 流量与理论加速比，并与简单逐元素内核的屋顶上限对比，亲眼看「字节比 ≠ 时间比」。

2. **操作步骤**：运行以下**示例代码**（纯 Python，无需 GPU）。设定：`x: M×K`，`W: K×N`，fp16（\(s=2\)），GeLU 每元素约 10 FLOP（tanh 近似的量级，取值只影响小数点、不影响结论），reduction 为全量求和、写出标量可忽略。

   ```python
   # 示例代码：fusion_traffic.py —— 融合前后的 HBM 流量与理论加速
   PEAK_TFLOPS, BW_TB_S, S = 2000.0, 8.0, 2      # B200 取整值（同 gen_roofline.py）
   GELU_FLOPS = 10

   def account(M, N, K):
       b_gemm = S*(M*K + K*N) + S*M*N            # 读 x、读 W、写 Y
       b_gelu = 2 * S*M*N                        # 读 Y、写 G
       b_red  = S*M*N                            # 读 G
       flops  = 2*M*N*K + (GELU_FLOPS + 1)*M*N   # GEMM + GeLU + 求和
       t_gemm = max(2*M*N*K/(PEAK_TFLOPS*1e12), b_gemm/(BW_TB_S*1e12))
       t_gelu = b_gelu/(BW_TB_S*1e12)            # elementwise: 斜线封顶
       t_red  = b_red /(BW_TB_S*1e12)
       t_unfused = t_gemm + t_gelu + t_red       # 三个内核串行
       b_fused = S*(M*K + K*N)                   # 中间值留片上: 只剩读入
       t_fused = max(flops/(PEAK_TFLOPS*1e12), b_fused/(BW_TB_S*1e12))
       print(f"shape {M}x{K} @ {K}x{N}")
       print(f"  unfused: {b_gemm+b_gelu+b_red:>14,} B   t >= {t_unfused*1e6:7.2f} us")
       print(f"  fused  : {b_fused:>14,} B   t >= {t_fused*1e6:7.2f} us")
       print(f"  byte-ratio {((b_gemm+b_gelu+b_red)/b_fused):5.2f}x   "
             f"time-ratio {t_unfused/t_fused:5.2f}x   "
             f"AI {flops/(b_gemm+b_gelu+b_red):.1f} -> {flops/b_fused:.1f} FLOP/byte")

   account(4096, 4096, 4096)      # 大方阵: GEMM 主导
   account(1024, 4096, 128)       # 瘦矩阵: elementwise/reduction 主导
   ```

3. **需要观察的现象**：两种 shape 下字节比与时间比的巨大差异；融合前后链条整体 AI 相对拐点 250 的位置变化；单独 GeLU 阶段的时间被什么封顶。

4. **预期结果**（手算可复核，fp16）：

   **shape A：\(M=N=K=4096\)**

   | 版本 | HBM 字节 | 理想时间下界 | 构成 |
   | --- | --- | --- | --- |
   | 未融合（3 内核） | 192 MiB | ≈ 81.3 µs | gemm 68.7（**计算界**）+ gelu 8.4 + red 4.2 |
   | 融合（1 内核） | 64 MiB | ≈ 68.8 µs | 计算界（137.6 GFLOP ÷ 2000 TFLOP/s） |

   字节比 **3.0×**，时间比只有 **≈1.18×**——被删掉的 128 MiB 流量大部分不在关键路径上，因为大方阵 GEMM 本身是 compute-bound（u3-l1 的 \(N/3 = 1365\)），融合删掉的是它后面的两个 memory-bound 小尾巴。

   **shape B：\(M=1024,\ N=4096,\ K=128\)（瘦矩阵）**

   | 版本 | HBM 字节 | 理想时间下界 | AI（链条整体） |
   | --- | --- | --- | --- |
   | 未融合 | 33.3 MiB | ≈ 4.36 µs（1.21 + 2.10 + 1.05） | 32.1 → memory-bound |
   | 融合 | 1.25 MiB | ≈ 0.56 µs（计算界） | 854.5 → **跨过拐点 250** |

   字节比 **26.6×**，时间比 **≈7.8×**。三个阶段全部 memory-bound，流量就是时间，删掉的字节几乎全额兑现；融合还把整条链的 AI 从 32 抬到 855，**从斜线区搬进了水平线区**。

   **与逐元素内核屋顶的对比**：单独的 GeLU 内核 AI \(= 10/(2\times2) = 2.5\) FLOP/byte，上限 = \(8 \times 2.5 = 20\) TFLOP/s（等效），换算成时间即 \(2sMN/8\,\text{TB/s}\)（shape A 为 8.4 µs、shape B 为 2.1 µs）——**这是它无论怎么优化都跳不出的屋顶**（依据 4.1.3 条款 3：贴顶后只能减字节）。融合的策略不是「把这个 elementwise 内核调快」，而是让它**整个消失**：那 2·s·|I| 字节在融合版里根本不存在。

   模型忽略 launch 开销、L2 命中与非理想带宽利用率，实测数值会偏离——**待本地验证**（有 GPU 时可用 `torch` eager 链 vs `torch.compile` 对照方向性结论）。

5. 把两个 shape 的结论写成一句话：**融合省的是字节，兑换多少时间取决于被删流量是否在关键路径上**。

#### 4.2.5 小练习与答案

**练习 1**：把链改成 `x@W → bias add → GeLU`（两个中间张量），相对纯 GEMM（读 x、W，写 D）多出的 HBM 流量是多少？融合能省回多少？

**答案**：bias add 产生中间张量 \(Y\)（读 Y 写 Y'：\(2sMN\)），GeLU 消费 \(Y'\)（读 Y' 写 G：\(2sMN\)），共多出 \(4sMN\) 字节；融合后两个中间值都留片上，全额省回 \(4sMN\)（账本法见 4.2.2）。

**练习 2**：为什么同一个融合在 4096³ 只买回 1.18×，在 \(1024\times4096\times128\) 却买回约 7.8×？

**答案**：时间下界是 \( \max(\text{计算},\ \text{字节}) \)。4096³ 时 GEMM 段的计算项（68.7 µs）远大于各段字节项，删掉 gelu/red 的流量不动最大项；瘦矩阵时三个段的字节项都在关键路径上（各段都 memory-bound），删掉的 32 MiB 几乎全部变成时间。**字节比是收益的上限，时间比由关键路径决定**。

**练习 3**：「attention 不生成完整 score matrix」对应 [chapter_performance/index.md:L158-L160](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L158-L160) 三条中的哪一条？被消灭的 \(2s|I|\) 里 \(|I|\) 是什么？

**答案**：第三条（Compute attention without materializing the full score matrix）。\(|I|\) 是 \(L \times L\) 的 score 矩阵 \(S\) 与 \(P=\text{softmax}(S)\)（各一次写+读往返）；u3-l1 的定量结果（naive ≈ 62 → flash ≈ 2048 FLOP/byte）显示这笔往返正是 naive attention 被 lock 在拐点左侧的原因。

### 4.3 数据复用与搬运效率：tiling 与抬不动 AI 之后的清单

#### 4.3.1 概念说明

减字节的第二个手段是**数据复用（reuse）**，正文也称 tiling/blocking：把大问题切成小 tile，使加载到片上的数据被**多次使用**。它与融合是互补的两种复用：

- **融合**：跨算子的复用——中间值不落盘，直接喂给下一个操作（4.2）。
- **tiling**：单个算子内部的复用——同一片输入数据在片上服务多次计算。

GEMM 是 tiling 的天然宿主：A 的一个元素参与**同一行**多个 C 元素的计算，B 的一个元素参与**同一列**多个 C 元素的计算。若每次使用都回 HBM 重读，流量会大到荒谬；把 A/B tile 留在片上，同样的 \(2MNK\) 次计算只需要少得多的 HBM 字节。tile 级 AI 公式（\(\text{AI} \approx 2 B_M B_N / (s(B_M + B_N))\)，方阵时 \(\approx B/s\)）u3-l1 4.3.2(c) 已完整推导，本讲不重复，只引用其结论。

本模块还有收尾的一半：**当 AI 确实抬不动了**——纯 copy、简单 elementwise、单遍 reduction 这类算子，既没有值得融合的中间张量，也没有足够的数据复用——优化目标就从「抬 AI」切换为「逼近有效带宽」：每个 byte 只搬一次、coalesced/vectorized 访存、规则大块用 TMA、保持足够的在途请求。

#### 4.3.2 核心流程

把复用水平做成一把梯子，同一个 \(4096^3\) fp16 GEMM（FLOP 固定为 137.4 GFLOP）在不同梯级上的 HBM 流量为（**示例推导**：no-reuse 档按「每次使用都从 HBM 现读」的口径数出，记账规则同 u3-l1；其余两档来自正文）：

```text
梯级 0  完全不复用: 每条乘加现取 A、B 元素
        A 字节 = s·M·K·N（每个 A 元素被读 N 次），B 字节 = s·K·N·M
        → 2sMNK ≈ 274.9 GB，AI ≈ 1/s = 0.5 FLOP/byte
梯级 1  CTA tile 复用（正文公式，AI ≈ B/s）:
        B=16 → AI 8；B=64 → AI 32（正文的数字）
梯级 2  整矩阵完美复用: A、B 各读一次 + C 写一次
        → s(MK+KN+MN) ≈ 100.7 MB，AI = N/3 ≈ 1365 FLOP/byte
```

从梯级 0 到梯级 2，**同一个数学问题，字节差约 2730 倍**——这就是「Reloading those values from HBM for every use would create substantial traffic」这句话的定量版本。梯级 1 与 2 之间的差距（AI 从 \(B/s\) 到 \(N/3\)）靠多级 tile（CTA tile × L2 × 循环顺序）逐步逼近，是 GEMM 单元（u11–u13）的实战内容。

对抬不动 AI 的算子，流程切换为「搬运效率核查单」：

```text
① 每 byte 只搬一次，消灭冗余读
② coalesced / vectorized 访存（一次事务搬一段连续字节）
③ 规则的大块 tile 交给 TMA 硬件引擎
④ 保持足够多的在途 memory request，不让内存管线空闲
```

#### 4.3.3 源码精读

**（1）tiling 复用的动机。** [chapter_performance/index.md:L162-L166](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L162-L166)：通过 tiling（亦称 blocking）提高复用——把大问题切成小 tile、让片上数据被多次使用；GEMM 中一个 A 元素参与同一行多个 C 元素、一个 B 元素参与同一列多个 C 元素，**若每次使用都从 HBM 重读，会产生大量搬运**。中文镜像 [zh/chapter_performance/index.md:L131](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L131)。

**（2）片上复用抬 AI。** [chapter_performance/index.md:L168-L169](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L168-L169)：把 A/B tile 留在片上，同样的 \(2MNK\) 次计算只需更少的 HBM 字节、AI 更高；该思路适用于一切反复复用同一 tile 的 workload。随后的简化模型与 16×16/64×64 数例（[L171-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L171-L228)，AI 8 → 32）u3-l1 已精读；机理总结在 [L225-L228](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L225-L228)：从全局内存读入的一个 A/B 元素，能在 tile 内服务更多次乘加。中文镜像 [zh/chapter_performance/index.md:L133](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L133)。

**（3）抬不动 AI 之后的核查单。** [chapter_performance/index.md:L237-L244](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L237-L244)：AI 无法再提高时，优化目标转向**有效带宽**；纯 copy、简单 elementwise、大 tensor 上的单遍 reduction 通常**既无可融合的中间结果、也无足够复用**，此时应做到——每个 byte 只搬一次、coalesced/vectorized 访存、规则大块 tile 用 TMA、保持足够多在途 memory request 以防空闲内存管线。中文镜像 [zh/chapter_performance/index.md:L183-L188](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/zh/chapter_performance/index.md#L183-L188)。

**（4）核查单第 ③ 条与 GEMM 主线的衔接。** [chapter_performance/index.md:L255](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L255)：GEMM 优化阶梯中第一个大的实测跃变，就是从 thread-copy 路径切到 TMA-backed 路径——把规则的 tile 搬运交给 TMA 硬件引擎。也就是说，核查单第 ③ 条不是边缘技巧，而是 GEMM 九步优化（单元十一~十三）的第一级台阶；TMA 机制本身在单元六展开。

#### 4.3.4 代码实践：复用梯子上的字节对比

1. **实践目标**：把 4.3.2 的三档复用水平代码化，亲眼核对「同一个 GEMM 差 2730 倍字节」，并用 tile 公式补上中间档。

2. **操作步骤**：运行以下**示例代码**（纯 Python）：

   ```python
   # 示例代码：reuse_ladder.py —— 同一 GEMM、三档复用水平的 HBM 流量
   M = N = K = 4096; s = 2; BW = 8e12

   flops = 2*M*N*K
   regimes = [
       ("no reuse",      s*M*K*N + s*K*N*M),          # 每次使用都从 HBM 现读
       ("perfect reuse", s*(M*K + K*N) + s*M*N),      # A、B 各读一次 + C 写一次
   ]
   for name, b in regimes:
       print(f"{name:15s} {b/2**30:9.3f} GiB  AI={flops/b:8.2f}  "
             f"mem-time={b/B*1e3:9.3f} ms")

   for B in (16, 64):   # 中间档: CTA tile 复用（u3-l1 公式，忽略 C 读写）
       print(f"CTA tile {B}x{B}   stage AI ≈ {2*B*B/(s*2*B):.0f} FLOP/byte")
   ```

3. **需要观察的现象**：no-reuse 档的 AI 是否 ≈ \(1/s = 0.5\)；两档字节相差多少倍；纯搬运时间从几十毫秒缩到十几微秒的跨度。

4. **预期结果**（手算可复核）：

   | 复用水平 | HBM 字节 | AI (FLOP/byte) | 纯搬运时间 @8TB/s |
   | --- | --- | --- | --- |
   | 完全不复用 | 256.0 GiB（274.9 GB） | 0.50 | 34.4 ms |
   | CTA tile 16×16（\(B_K=64\)） | — | 8 | — |
   | CTA tile 64×64（\(B_K=64\)） | — | 32 | — |
   | 完美复用 | 0.094 GiB（100.7 MB） | 1365.3 | 12.6 µs |

   字节比 \(274.9\,\text{GB} / 100.7\,\text{MB} \approx 2730\times\)；tile 档的 8 与 32 与正文数例一致。注意方向：**tile 越大 AI 越高，但 SMEM/寄存器/TMEM 消耗也越大**——这个代价维度是 u3-l3（occupancy 与资源压力）的主题，本讲先按下。实际打印格式——**待本地验证**。

5. 反向自检：no-reuse 的 34.4 ms 与 u3-l1 图中 naive GEMM 点（2.9 TFLOP/s，同规模约 47 ms）同量级——naive 实现虽不至于每次现读，但复用水平同样很低，屋顶差距要靠整条优化阶梯爬。

#### 4.3.5 小练习与答案

**练习 1**：为什么「完全不复用」档的 AI 恰好 ≈ \(1/s\)（fp16 时 0.5）？

**答案**：每条乘加需要现取 A、B 各 \(s\) 字节（共 \(2s\) 字节）才产出 2 FLOP，AI \(= 2/(2s) = 1/s\)。这个 0.5 与完美复用的 1365 相差约 2730 倍，全部来自「数据在片上被用了多少次」——正是 [chapter_performance/index.md:L164-L166](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L164-L166) 所说「每次使用都重读会造成大量搬运」的定量形态。

**练习 2**：哪些算子连「融合」和「复用」两扇门都进不去？对它们正确的优化目标是什么？

**答案**：纯 copy、简单 elementwise、大 tensor 上的单遍 reduction（[chapter_performance/index.md:L237-L239](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L237-L239)）——既无可融合的中间张量，也无足够复用。目标是有效带宽的四条核查单（[L241-L244](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L241-L244)）。

**练习 3**：四条核查单中的「对规则的大块 tile 使用 TMA」，在本书后续哪条主线里兑现？

**答案**：GEMM 优化的第一个实测大跳变——thread-copy 路径换成 TMA-backed 路径（[chapter_performance/index.md:L255](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L255)），即 GEMM Step 4（u12-l1）；TMA 的机制细节在单元六。

## 5. 综合实践：一条组合算子的完整优化分析

把本讲三个模块串成一份对 `x@W → GeLU → reduction` 的完整分析报告（纯 Python 可完成，第 5 步可选）：

1. **字节账本**：扩展 4.2.4 的 `fusion_traffic.py`，对 shape A（4096³）与 shape B（1024×4096×128）打印「每阶段字节 / 每阶段时间下界 / 总字节 / 总时间 / 字节比 / 时间比」六列表格。
2. **AI 迁移图**：计算两种 shape 下未融合与融合链条的整体 AI，标出它们相对拐点 250 的位置；用一句话解释为什么 shape B 的融合「跨过了拐点」而 shape A 没有（参考答案：B 的 GEMM 段本身 memory-bound、AI 低，融合后 GEMM 的复用把链条 AI 抬过 250；A 的两版都被大 GEMM 的 1365 主导。）
3. **屋顶对比**：用 u3-l1 4.3.4 的画法重绘 roofline，把四个点画上去——单独 GeLU（AI 2.5）、未融合链条 B（32.1）、融合链条 B（854.5）、融合链条 A（≈2050），并给 GeLU 点标注「它的屋顶只能靠融合删除，不能靠调优跨越」。
4. **源码阅读**：打开 [chapter_gemm_basics/index.md:L132-L143](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_gemm_basics/index.md#L132-L143) 的 Step 1 epilogue，回答：若要把 GeLU 融合进这个内核，应该插在哪一行旁边？为什么这一步不增加任何 HBM 流量？（参考答案：`Tx.cast(Dreg_f16[:], Dreg[:])` 旁——数据此刻在每线程的寄存器行 `Dreg` 里，GeLU 就地计算后再 `Tx.copy` 写出；寄存器中的 elementwise 操作不产生 HBM 流量，见 4.2.3 第 (4) 点。）
5. **（可选，需 Blackwell GPU）实测闭环**：按 u1-l3 的环境用 PyTorch 实测 eager 链与 `torch.compile` 后的耗时，与账本预测的时间比对照；方向一致即算验证成功，数值偏差用「非理想带宽利用率 + launch 开销」解释。无 GPU 时记录环境限制并标注**待本地验证**。

完成后你应当能脱口而出本讲的三个数字：**3.0× 字节 / 1.18× 时间（4096³）**，**26.6× 字节 / 7.8× 时间（瘦矩阵）**，**2730×（复用梯子两极）**。

## 6. 本讲小结

- memory-bound 内核只有两条出路：**减少 HBM 字节抬高 AI**（融合、复用、更小 dtype），或流量减不动时**把搬运率逼近带宽屋顶**；已贴屋顶后，优化计算侧零收益，唯一出路是换算法搬更少字节（[chapter_performance/index.md:L246](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/chapter_performance/index.md#L246)）。
- **融合的账本**：每个落盘的中间张量花费 \(2s|I|\) 字节往返，融合让它在寄存器/SMEM/TMEM 中交接、这笔流量归零；`x@W → GeLU → reduction` 在 4096³ 省 3.0× 字节却只兑 1.18× 时间，在瘦矩阵省 26.6× 字节兑约 7.8× 时间——**字节比是上限，时间比由关键路径决定**。
- **复用的梯子**：同一个 \(4096^3\) GEMM，从「每次使用都重读」（≈274.9 GB，AI≈0.5）到「片上完美复用」（≈100.7 MB，AI≈1365），字节差约 2730 倍；CTA tile 档 \(AI \approx B/s\) 是两极之间的台阶。
- **抬不动 AI 的算子**（纯 copy、简单 elementwise、单遍 reduction）走四条搬运核查单：每 byte 只搬一次、coalesced/vectorized、规则大块用 TMA、保持在途请求；其中 TMA 一条正是 GEMM 九步优化的第一级台阶。
- 书中的两个融合现场：GEMM epilogue 里的 `Tx.cast`（elementwise 转换住在寄存器里，零 HBM 流量）与 Flash Attention 的 score matrix 不落盘（算法级融合，AI 从 ≈62 抬到 ≈2048）。

## 7. 下一步学习建议

- **下一讲（u3-l3）重叠、Occupancy 与优化阶梯**：本讲处理斜线区（memory-bound），下一讲转向水平线区——compute-bound 内核如何通过 overlap 减少 Tensor Core 空闲、优化阶梯各台阶改了什么，以及「低 occupancy 换显式重叠」的资源取舍（正好接住 4.3.4 实践第 5 点埋下的问题）。
- **单元六（TMA）**：核查单第 ③ 条的机制细节——tensor map、单线程发起、mbarrier 字节追踪，是 u12 GEMM Step 4 的直接前置。
- **单元十一~十三（GEMM 九步）**：看复用梯子的中间档如何落成 SMEM pool、双缓冲与持久内核，把 \(B/s\) 公式逐级推向 \(N/3\)。
- **单元十四（FA4）**：算法级融合的完整工程实现——score tile 留在 TMEM/SMEM 的那份设计，正是本讲 4.2.3 第 (3) 点的展开。
- **测量闭环**：本讲的账本只给预测；读 [appendix/benchmarking_gpu_kernels.md](https://github.com/mlc-ai/modern-gpu-programming-for-mlsys/blob/0fdad075417d347e2171affadf9c1b07cd54f87f/appendix/benchmarking_gpu_kernels.md)（u15-l4/l5）学会用 CUDA events 与 Proton/Nsight 把「离屋顶多远」测出来。
