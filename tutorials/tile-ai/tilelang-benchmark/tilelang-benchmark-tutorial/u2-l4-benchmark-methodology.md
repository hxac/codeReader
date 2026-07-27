# 性能度量方法论

## 1. 本讲目标

本讲是「基准测试方法论」单元的第一篇，回答一个看似简单实则关键的问题：**当我们说某个算子「跑了 800 TFlops」时，这个数字是怎么算出来的、可不可信？**

学完本讲，你应该能够：

1. 独立手算一个矩阵乘（GEMM）或 FlashAttention 的浮点运算量（FLOPS）。
2. 把测得的延迟（latency）正确换算成吞吐（TFlops），并能看懂本仓库代码里的换算公式。
3. 说清楚 `warmup`（预热）和 `rep`（重复）对测量稳定性的作用，并理解 cuBLAS 基线为什么用「自适应重复次数」。
4. 读懂 README 里的 V / M / FA / CC / CT 五组 shape 配置表，并知道它与 `data/*.py` 里实际跑的 shape 列表的关系。

本讲**不**讲具体内核怎么写，也不讲怎么调优——那是后面几个单元的事。本讲只建立「如何公平、可信地度量一个算子」这把标尺。没有这把标尺，后面所有「TileLang 比 Triton 快 X%」的结论都无从谈起。

## 2. 前置知识

在进入源码前，先用通俗语言把几个基础概念讲透。

### 2.1 什么是 FLOPS 与 TFlops

- **FLOP**（Floating-point OPeration，浮点运算）：一次浮点加法算 1 次，一次浮点乘法算 1 次。
- **FLOPS**（注意全大写，指 Floating-point Operations Per Second，每秒浮点运算次数）：衡量硬件或内核的**吞吐**（throughput），单位是「次/秒」。
- **TFlops**：1 TFlops = \(10^{12}\) FLOPS，即每秒一万亿次浮点运算。H100 在 fp16 下的理论峰值约 989 TFlops。

> 容易混淆点：`FLOP`（运算次数，是个数）和 `FLOPS`（每秒运算次数，是速率）。本仓库代码里 `total_flops` 这个变量名其实是「总运算次数」(FLOP count)，而打印出的 `TFlops` 才是速率。读代码时不要被命名绕晕。

### 2.2 为什么 GEMM 的运算量是 \(2MNK\)

矩阵乘 \(C = A \times B\)，其中 \(A\) 是 \(M \times K\)，\(B\) 是 \(K \times N\)，\(C\) 是 \(M \times N\)。结果矩阵 \(C\) 共有 \(M \times N\) 个元素，每个元素是 \(A\) 的一行（\(K\) 个数）和 \(B\) 的一列（\(K\) 个数）做**点积**——也就是 \(K\) 次乘法和 \(K\) 次加法，共 \(2K\) 次浮点运算。

所以总运算量：

\[
\text{FLOP} = M \times N \times 2K = 2MNK
\]

这就是后面代码里 `2 * M * N * K` 的来历。公式前的那个 `2`，就是「一次乘 + 一次加 = 2 次运算」。

### 2.3 latency 与 TFlops 的换算直觉

内核跑一次花了多少时间叫 **latency**（延迟，单位通常是毫秒 ms）。把「总运算量」除以「时间」就得到吞吐：

\[
\text{TFlops} = \frac{\text{total\_flops}}{\text{latency}}
\]

但要注意单位：只有当分子分母单位配套时，结果才对。本仓库用的换算系数 `1e-9` 里藏着单位转换的玄机，我们在 4.4 节专门拆解。

### 2.4 为什么要 warmup

GPU 第一次执行一个内核时，会做很多「一次性」工作：分配显存、加载指令到指令缓存、把数据搬进 L2/共享内存、GPU 时钟频率可能还没爬到最高。如果第一次的耗时也被算进去，测出来的数会偏大且不稳定。**warmup**（预热）就是先空跑若干次，让这些都 settle 下来，然后再开始正式计时。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py) | GEMM 基准：`total_flops = 2*M*N*K`、`@autotune(warmup=3, rep=20)`、TFlops 换算的样板 |
| [hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py) | Attention 基准：`flops_per_matmul` 公式、causal `×0.5`、`do_bench(warmup=500)`，并且**明确把延迟标注为 ms** |
| [hopper_benchmark/dense_matmul/data/data_float16_gemm.py](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py) | 实际 benchmark 的 shape 列表（13 条，M0–M12）与从日志解析 latency 的逻辑 |
| [hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu) | 对照组：cuBLAS C++ 基线用「1 次 warmup + 自适应重复次数」保证测量稳定 |
| [README.md](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md) | V/M/FA/CC/CT 五组 shape 配置表，定义测试集的分类与命名 |

## 4. 核心概念与源码讲解

### 4.1 FLOPS 计算

#### 4.1.1 概念说明

「FLOPS 计算」回答的是：**这个算子到底做了多少次浮点运算？** 它是吞吐公式的分子。分子算错（多算或少算），后面除出来的 TFlops 就全是错的。

要点有两个：

1. 对 GEMM，运算量是 \(2MNK\)，与数据精度（fp32/fp16/int8）**无关**——公式只数「乘+加」的逻辑次数。但注意：**峰值 TFlops 随精度变化剧烈**（int8 TensorCore 峰值 ≫ fp16 ≫ fp32），所以同样 2MNK 的运算量，不同精度下的「占峰值比例」完全不同。
2. 对 Attention，运算量是两次矩阵乘（\(QK^T\) 和 \(PV\)）之和，并且在 causal（因果掩码）情况下要打半折 `×0.5`。

#### 4.1.2 核心流程

**GEMM 的 FLOPS：**

\[
\text{total\_flops}_{\text{GEMM}} = 2 \cdot M \cdot N \cdot K
\]

**FlashAttention 的 FLOPS：**

设 `batch=B, heads=H, seq_q=Sq, seq_kv=Skv, head_dim=D`。

- 一次矩阵乘 \(QK^T\)：每个 `(batch, head)` 切片是 \([Sq, D] \times [D, Skv]\)，运算量 \(2 \cdot Sq \cdot D \cdot Skv\)，乘以 `B*H` 个切片：

\[
\text{flops\_per\_matmul} = 2 \cdot B \cdot H \cdot Sq \cdot Skv \cdot D
\]

- 两次矩阵乘（\(QK^T\) 和 \(PV\) 各一次）：

\[
\text{total\_flops} = 2 \cdot \text{flops\_per\_matmul}
\]

- **causal 修正**：因果掩码把注意力分数矩阵的下三角置零（详见 u5-l17），有效计算量约为非 causal 的一半：

\[
\text{total\_flops}_{\text{causal}} = \text{total\_flops} \times 0.5
\]

#### 4.1.3 源码精读

GEMM 的运算量计算在主程序里，紧跟在解析完命令行参数之后：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:267-268](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L267-L268) —— 计算 `total_flops = 2 * M * N * K`，即本节公式 \(\eqref{}\) 的直接翻译。

Attention 的运算量计算在 `flashattn` 函数返回前，注意它先算「单次矩阵乘」，再乘 2，再按 causal 打折：

[hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py:202-205](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L202-L205) —— `flops_per_matmul` 是一次 \(QK^T\) 的运算量，`total_flops = 2 * flops_per_matmul` 计入两次矩阵乘，`if is_causal: total_flops *= 0.5` 对 causal 情况折半。

**为什么 causal 要乘 0.5？** 直观理解：causal 掩码让注意力分数矩阵 \(S = QK^T\) 只保留下三角部分（query 位置 \(i\) 只能 attend 到 key 位置 \(j \le i\)）。整个 \(Sq \times Skv\) 矩阵里大约只有一半的元素是「有效的」。FlashAttention 内核在实现上会**按 query 行裁剪 KV 循环**（跳过上三角的整个 block，见 u5-l17），真正执行的 MMA 指令数约为非 causal 的一半。因此，为了让报告出的 TFlops 反映「内核实际做的有用功」，业界（FlashAttention 官方、TileLang 等）统一约定：causal 的 FLOPS 按 0.5 折算。这样 causal 与非 causal 的吞吐数字才有可比性，否则 causal 会因为「分母没变、分子没打折」而显得吞吐异常偏低。

> 顺带一提：`benchmark_tilelang_matmul.py` 里的内核 `dtype` 实际是 `"int8"`（见后续 u4-l12），而本文件名却叫 `float16` 的 data 脚本——这正说明 **FLOPS 公式与精度无关**，2MNK 对 int8 和 fp16 都成立，区别只在峰值上限。

#### 4.1.4 代码实践

**实践目标**：亲手验证 GEMM 与 Attention 的运算量公式。

**操作步骤**：

1. 打开 [benchmark_tilelang_matmul.py:267-268](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L267-L268)，确认 `total_flops = 2 * M * N * K`。
2. 用计算器或 Python 算：取 README 里 M2（m=4096, n=28672, k=8192），`total_flops = 2 * 4096 * 28672 * 8192`。
3. 打开 [benchmark_tilelang_mha.py:202-205](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L202-L205)，对照公式理解 `flops_per_matmul`。
4. 取 README 里 FA0（batch=1, heads=32, seq=512, dim=128），分别算 causal=True 和 causal=False 的 total_flops，观察它们正好是 2 倍关系。

**需要观察的现象**：

- M2 的 `total_flops` = \(2 \times 4096 \times 28672 \times 8192 = 1{,}932{,}735{,}283{,}200\)（约 \(1.93 \times 10^{12}\)）。
- FA0：`flops_per_matmul = 2 * 1 * 32 * 512 * 512 * 128 = 4{,}294{,}967{,}296`；`total_flops`(非 causal) = \(2 \times\) 该值 = \(8{,}589{,}934{,}592\)；causal 情况 = 其一半 = \(4{,}294{,}967{,}296\)。

**预期结果**：causal 与非 causal 的 total_flops 恰好成 2 倍关系，验证 `×0.5` 的正确性。（上述数值待本地用计算器复核。）

#### 4.1.5 小练习与答案

**练习 1**：GEMM 形状 \(M=8192, N=8192, K=8192\)，total_flops 是多少？写成 TFlops 量级。
**答**：\(2 \times 8192^3 = 1{,}099{,}511{,}627{,}776 \approx 1.10 \times 10^{12}\) 次，约 1.1 T FLOP（注意这里是运算「次数」，不是每秒速率）。

**练习 2**：为什么 int8 GEMM 和 fp16 GEMM 用**同一个** `2*M*N*K` 算 FLOPS，但报告的「占峰值百分比」差别很大？
**答**：FLOPS 公式只数逻辑乘加次数，与精度无关；但 GPU 的 int8 TensorCore 峰值远高于 fp16，所以同样运算量下，int8 的绝对 TFlops 上限更高，占峰值比的计算基准不同。

**练习 3**：FlashAttention 的 `total_flops = 2 * flops_per_matmul` 里的 `2`，对应物理上的哪两次矩阵乘？
**答**：第一次 \(Q \cdot K^T\) 得到注意力分数，第二次 \(\text{softmax}(QK^T) \cdot V\) 得到输出；各贡献一份 `flops_per_matmul`，合计乘 2。

---

### 4.2 warmup / rep 与测量稳定性

#### 4.2.1 概念说明

测延迟时，单次计时是不可靠的——内核启动有固定开销、GPU 时钟会波动、缓存状态在变化。本仓库的 TileLang 基线用两个参数控制计时质量：

- **`warmup`**：正式计时前的预热次数，这些迭代**不**计入结果，只用来稳定时钟、填满缓存。
- **`rep`**：正式计时的重复次数，最终延迟是这 `rep` 次测量的统计值。

它们决定了「测出来的是一个稳定均值/最小值，还是抖动很大的偶然数」。`rep` 太小 → 抖动大、不可复现；`warmup` 太小 → 首次开销拉高均值。

#### 4.2.2 核心流程

TileLang 的 `@autotune` 装饰器对**每个候选配置**都跑一遍 `do_bench`，流程是：

```
对每个 config:
    先空跑 warmup 次          # 不计时，热身
    连续跑 rep 次，逐次计时    # 收集 rep 个延迟样本
    取这 rep 次的统计量作为该 config 的 latency
在所有 config 里选 latency 最小的作为 best_result
```

`do_bench` 本身（不走 autotune 的离线评估路径也用它）同样接受一个 `warmup` 参数，含义一致。

不同场景选不同 warmup/rep，是「调优」与「单点评估」的取舍：

| 路径 | warmup | rep | 出处 | 用途 |
|------|--------|-----|------|------|
| GEMM autotune | 3 | 20 | matmul `@autotune` | 每个配置快速估时，配置多，故 rep 适中 |
| Attention autotune | 10 | 10 | mha `@autotune` | attention 配置少，可提高 warmup |
| Attention 单点 `do_bench` | 500 | (默认) | mha `do_bench(warmup=500)` | 只评估一个配置，故 warmup 拉满求稳 |

#### 4.2.3 源码精读

GEMM 的 autotune 设置（注意 `warmup=3, rep=20` 是装饰器参数，对每个候选 config 生效）：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:142-146](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L146) —— `@autotune(configs=..., warmup=3, rep=20)`：每个候选配置先预热 3 次，再计时 20 次取统计量。

Attention 的两套设置——autotune 路径和离线 `do_bench` 路径：

[hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py:162](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L162) —— `@autotune(configs=get_configs(), warmup=10, rep=10)`：attention 配置少，预热次数比 GEMM 多。

[hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py:216](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L216) —— `latency = profiler.do_bench(warmup=500)`：不走调优、只评估单个配置时，直接把 warmup 拉到 500 以求最稳的延迟。

**对照组：cuBLAS C++ 基线的「自适应重复」策略。** cuBLAS 不用固定的 rep，而是先用 1 次 warmup 估出「单次迭代耗时」，再倒推出「需要重复多少次才能让总测量窗口达到约 100ms」，并设下限 5 次：

[hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu:108-127](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L108-L127) —— 先跑 1 次 `cublasGemmEx` 作为 warmup 并计时得到 `periter_duration`，再用 `numRepeats = std::max(5, int(minimal_repeat_ms / periter_duration))` 自适应决定正式重复次数（`minimal_repeat_ms=100`）。

> 这种「固定 rep」与「自适应 rep」的差别正是两种框架的风格：TileLang/Triton 的 `do_bench` 用固定 warmup/rep，简单一致；cuBLAS 用 100ms 目标窗口，保证无论内核快慢，总测量时间都足够长。两种都能得到稳定延迟，cuBLAS 的深入剖析见 u2-l5。

#### 4.2.4 代码实践

**实践目标**：定位三处计时参数，并预测改动的影响。

**操作步骤**：

1. 在 matmul 文件第 [142-146 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L142-L146) 找到 `warmup=3, rep=20`。
2. 在 mha 文件第 [162 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L162) 和第 [216 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L216) 分别找到 `warmup=10, rep=10` 与 `do_bench(warmup=500)`。
3. 在 cublas 文件第 [127 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L127) 找到 `numRepeats = std::max(5, ...)`。

**需要观察的现象**（思考题，无需运行）：

- 若把 matmul 的 `rep` 从 20 改成 2，测出的 latency 会有什么变化？
- 若把 `warmup` 从 3 改成 0，第一次计时会包含哪些额外开销？

**预期结果**（待本地验证）：`rep` 过小 → 结果抖动大、两次运行可能差很多；`warmup=0` → 首次冷启动的指令加载、显存分配开销被算进延迟，使测出的 TFlops 偏低。

#### 4.2.5 小练习与答案

**练习 1**：GEMM autotune 有成百上千个候选配置，为什么 `rep` 只设 20 而不是 200？
**答**：每个配置都要跑 warmup+rep 次，配置数 × rep 决定总调优时间。rep=200 会让整个 autotune 慢 10 倍；rep=20 在「足够稳定」和「调优可承受」之间取折中。

**练习 2**：cuBLAS 的 `numRepeats = max(5, int(100 / periter_duration))` 中，为什么要有下限 5？
**答**：对一个很慢的内核，`100/periter` 可能算出 1 或 0，单次或零次测量毫无统计意义；下限 5 保证至少有几个样本求平均。

**练习 3**：`do_bench(warmup=500)` 这种「warmup 远大于 rep」的用法，适合什么场景？
**答**：适合只评估**单一**配置、追求延迟最稳的场景（如 attention 的非调优路径）。因为不需要在多个配置间节省时间，可以把预算全花在预热上，排除冷启动干扰。

---

### 4.3 shape 配置表

#### 4.3.1 概念说明

「shape 配置表」定义了**用哪些矩阵尺寸/张量尺寸来测试**。要让 cuBLAS、Triton、TileLang 之间的对比「公平」，最基本的前提是：大家跑**完全相同的一组 shape**。README 用 V/M/FA/CC/CT 五组列族来分类定义这套公共测试集。

- **V 族（V0–V7）**：矩阵向量乘 GEMV，特征是 `m=1`（一条 query 向量乘一个大矩阵）。
- **M 族（M0–M7）**：矩阵矩阵乘 GEMM，`m` 较大（4096/8192）。
- **FA 族（FA0–FA4）**：FlashAttention，含 batch/heads/seq_len/head_dim/causal 五个维度。
- **CC / CT 族**：Linear Attention 的两种变体，多了 `d_state` 维度。

#### 4.3.2 核心流程

README 把测试集组织成「族 → 编号 → 各维度值」的三层结构。读表的方法：

```
选定一个族（如 M），选定一个编号（如 M2）
    → 查该列的 m / n / k 三行
    → 得到一个具体的 (m, n, k)，例如 M2 = (m=4096, n=28672, k=8192)
用这个 shape 去喂所有框架的基准脚本，得到各框架的 latency
```

五组族分别覆盖三类算子：V/M 覆盖 GEMV/GEMM，FA 覆盖 Attention，CC/CT 覆盖 Linear Attention。

#### 4.3.3 源码精读

README 的三张 shape 表——Table 1（V/M，矩阵）、Table 2（FA，Attention）、Table 3（CC/CT，Linear Attention）：

[README.md:34-45](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L34-L45) —— Table 1：V0–V7（GEMV，`m=1`，`n/k` 在 9216–57344 之间）与 M0–M7（GEMM，`m` 为 4096/8192）。

[README.md:51-57](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L51-L57) —— Table 2：FA0–FA4，含 `causal` 列（true/false 交替），是 attention 测试集。

[README.md:64-79](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L64-L79) —— Table 3：CC0–CC5 与 CT0–CT5，Linear Attention 两种变体，多出 `d_state=128` 维度。

**重要诚实提示：README 的表 ≠ 实际跑的 shape 列表。** hopper dense_matmul 的可视化数据脚本 `data_float16_gemm.py` 用的是一份**13 条**的 inline 列表（编号 M0–M12），它和 README 的 M0–M7 **并不一致**：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:25-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L25-L39) —— 实际 benchmark 用的 13 条 shape，第一条就是 `[16384, 16384, 16384]`（远大于 README 的 M0=(4096,1024,8192)），并扩展出 README 没有的 M8–M12（如 `(8192, 22016, 8192)`）。

所以正确的理解是：**README 的表是「测试集的分类与命名约定」（有哪些族、族怎么命名），而每个算子真正跑、真正画图的 shape 列表写在该算子 `data/*.py` 的 inline 列表里**。两者都叫 M0/M1…，但具体数值可能不同——这是本仓库的一个历史遗留坑（延续了 u1-l2、u1-l3 提醒的「读代码别盲信字面量」原则）。要复现某张图，必须以 `data/*.py` 的列表为准。

顺便看一眼数据组织：`matmul_providers` 和 `matmul_times_data` 用「(provider 名, [13 个延迟])」的结构存放各框架结果：

[hopper_benchmark/dense_matmul/data/data_float16_gemm.py:5-11](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L5-L11) —— `matmul_providers` 是 13 个 shape 编号 M0–M12；`matmul_times_data` 是三条 `(框架名, [13 个延迟])`，初始全填 `-1` 表示「尚未从日志解析」的占位符，后面脚本再用正则从各框架日志回填真实 latency。

#### 4.3.4 代码实践

**实践目标**：把 README 的 shape 族与实际数据脚本的 shape 列表对上。

**操作步骤**：

1. 打开 [README.md Table 1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L34-L45)，记下 V0–V7 和 M0–M7 的 (m,n,k)。
2. 打开 [data_float16_gemm.py:25-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L25-L39)，列出实际跑的 13 条 shape。
3. 对比两者，找出：(a) 哪些 shape 两边都有；(b) README 有但 data 脚本没有的；(c) data 脚本有但 README 没有的。

**需要观察的现象**：

- data 脚本的 M0 = (16384,16384,16384)，README 的 M0 = (4096,1024,8192)——完全不同。
- data 脚本多出 M8–M12 五条；README 的 M 族只到 M7。

**预期结果**：确认「README 表 = 命名约定，data/*.py = 实际测试集」，两者数值不一致。要复现图，以 `data/*.py` 为准。

#### 4.3.5 小练习与答案

**练习 1**：README 的 V 族和 M 族，最本质的区别是什么？
**答**：V 族 `m=1`（GEMV，矩阵乘向量），M 族 `m` 较大（GEMM，矩阵乘矩阵）。两者瓶颈不同：GEMV 是访存瓶颈，GEMM 是计算瓶颈。

**练习 2**：为什么 `data_float16_gemm.py` 里延迟初始值是 `-1` 而不是 `0`？
**答**：`-1` 是「尚未解析」的显式占位符；若用 0，则「没跑到」和「延迟为 0」会混淆，画图时也会把 0 当成真实数据点。

**练习 3**：README 的 FA 表里 `causal` 列 true/false 交替出现，目的是什么？
**答**：让 causal 和非 causal 两种 attention 都被测试，覆盖 attention 内核的两条主要代码路径（掩码与不掩码），避免只测一种而得到片面的性能结论。

---

### 4.4 latency 到 TFlops

#### 4.4.1 概念说明

有了分子（total_flops）和分母（latency），最后一步是把延迟换算成 TFlops。本仓库所有基准脚本都用同一句公式：

```python
total_flops / latency * 1e-9
```

这个 `1e-9` 看似神秘，其实是两个单位换算因子的合并。理解它，就能避免「单位用错导致 TFlops 差 1000 倍」的经典错误。

#### 4.4.2 核心流程

设 `total_flops` 单位是「次」（运算次数），`latency` 单位是「毫秒 ms」。目标结果是 TFlops（\(10^{12}\) 次/秒）。分两步换算：

1. **ms → s**：`latency_s = latency_ms × 1e-3`
2. **求 FLOPS 并换算到 TFlops**：

\[
\text{TFlops} = \frac{\text{total\_flops}}{\text{latency\_s}} \times 10^{-12}
= \frac{\text{total\_flops}}{\text{latency\_ms} \times 10^{-3}} \times 10^{-12}
= \frac{\text{total\_flops}}{\text{latency\_ms}} \times 10^{-9}
\]

所以代码里的 `* 1e-9` 就等于 `×(1e-3 / 1e-12)`：`1e-3` 抵消 ms→s，`1e-12` 把 FLOPS 压成 TFlops。**前提是 latency 必须以 ms 为单位输入**——这是整条公式的隐含约定。

#### 4.4.3 源码精读

GEMM 的换算与打印（注意打印标签写的是 `(s)`，但根据公式，latency 实际是 ms——见下方提醒）：

[hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py:278-282](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L278-L282) —— `Best TFlops: {total_flops / best_latency * 1e-9:.3f}`，把 best_latency 与 ref_latency 都用同一公式换算成 TFlops，便于直接对比加速比。

Attention 的换算——**这一份明确把 latency 标注为 ms**，是单位约定的关键证据：

[hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py:216-218](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L216-L218) —— `print("Tile-lang: {:.2f} ms".format(latency))` 紧接着 `print("... {:.2f} TFlops".format(total_flops / latency * 1e-9))`：明确表明 `do_bench` 返回的 `latency` 单位是 **ms**，与 `* 1e-9` 公式配套。

> **单位陷阱（重要）**：`benchmark_tilelang_matmul.py` 第 [278 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/3.tilelang-benchmark/benchmark_tilelang_matmul.py#L278) 打印 `Best latency (s): {best_latency}`，标签写的是秒（s），但 `best_latency` 来自与 mha 同源的 tilelang profiler（`do_bench`/autotune 内部计时），单位其实是 **ms**。证据有三：(1) mha 文件把它标成 ms；(2) `do_bench` 沿用 `triton.testing.do_bench` 返回 ms 的惯例；(3) 只有 latency 按 ms 代入，`* 1e-9` 才算得出物理上合理的 TFlops（详见 4.4.4 的数值校验）。这又是 u1-l3 反复强调的「别盲信标签，信公式与数值」的一个实例——matmul 文件的 `(s)` 标签是误导，实际是 ms。

#### 4.4.4 代码实践

**实践目标**：手算一次 TFlops，并验证公式的单位约定。

**操作步骤**：

1. 给定 GEMM shape \(M=N=K=16384\)，假设测得 `latency = 10 ms`。
2. 算 `total_flops = 2 * 16384^3`。
3. 代入 `TFlops = total_flops / latency_ms * 1e-9`。
4. 反向校验：若误把 latency 当成秒（即代入 0.01），算出的 TFlops 是多少？是否合理？

**计算过程**：

\[
\text{total\_flops} = 2 \times 16384^3 = 8{,}796{,}093{,}022{,}208 \approx 8.796 \times 10^{12}
\]

\[
\text{TFlops} = \frac{8.796 \times 10^{12}}{10} \times 10^{-9} = 879.6 \text{ TFlops}
\]

**需要观察的现象 / 预期结果**：

- 按 **ms** 代入（latency=10）：得 **879.6 TFlops**。H100 的 fp16 峰值约 989 TFlops，879.6 约占峰值 89%——物理上合理，说明单位正确。
- 若误按 **秒** 代入（latency=0.01）：得 \(8.796\times10^{12}/0.01\times10^{-9} = 879{,}600\) TFlops——远超硬件峰值 4 个量级，明显荒谬。这反过来证明 latency 必须是 ms，也印证了 matmul 文件 `(s)` 标签是错的。

> 结论：读这些基准脚本时，牢记 **`do_bench` 返回 ms、公式 `*1e-9` 按 ms 设计**，忽略个别误导性的 `(s)` 标签。

#### 4.4.5 小练习与答案

**练习 1**：把 `* 1e-9` 拆成两个因子，分别说明各自抵消/转换了什么单位。
**答**：`1e-9 = 1e-3 × 1e-12`。`1e-3` 把分母的 ms 换算成 s（即 `÷(ms×1e-3)` 中的 `1e-3`），`1e-12` 把 FLOPS（次/秒）换算成 TFlops（÷1e12）。

**练习 2**：同一个内核，若某人报告 latency 时单位用错了（把 ms 当 µs），算出的 TFlops 会偏大还是偏小多少？
**答**：ms 当 µs 等于把 latency 缩小 1000 倍，TFlops 会**偏大 1000 倍**。这正是为什么必须严格对齐单位。

**练习 3**：为什么 matmul 和 mha 用**完全相同**的 `total_flops / latency * 1e-9` 公式，即便它们 total_flops 的算法不同？
**答**：分子 total_flops 因算子而异（GEMM 用 2MNK，attention 用 2×flops_per_matmul 再按 causal 折半），但「延迟→吞吐」的换算与算子无关，只取决于单位约定（ms 与 1e-9）。所以分母侧的公式可以全局复用。

---

## 5. 综合实践

**任务**：模拟一次完整的「日志 → TFlops」度量流程，把本讲四个模块串起来。

假设你在 hopper dense_matmul 目录下得到一条 cuBLAS 日志行：

```
16384,16384,16384, 12.50
```

（格式为 `m,n,k,latency`，latency 单位待你判断。）

请完成：

1. **识别 shape**：这条日志对应 `data_float16_gemm.py` 列表里的哪个编号（M0–M12）？它是否与 README 的同编号 shape 一致？（提示：查 [data_float16_gemm.py:25-39](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/data/data_float16_gemm.py#L25-L39) 与 [README Table 1](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/README.md#L34-L45)。）
2. **算 total_flops**：用 GEMM 公式算这个 shape 的运算量。
3. **判断 latency 单位**：结合 cuBLAS harness 第 [149-151 行](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/dense_matmul/0.cublas-benchmark/cublas_benchmark.cu#L149-L151) 返回的是 µs，以及 4.4 节的单位约定，说明 12.50 这个数若直接代入 `*1e-9` 公式会有什么问题，应如何换算。
4. **算 TFlops**：给出最终 cuBLAS 在该 shape 的 TFlops，并估算它占 H100 fp16 峰值（约 989 TFlops）的比例。

**参考答案要点**：

1. `(16384,16384,16384)` 对应 data 脚本的 **M0**（第一条）；但 README 的 M0 是 `(4096,1024,8192)`，两者**不一致**——再次印证以 data 脚本为准。
2. `total_flops = 2 × 16384³ ≈ 8.796 × 10¹²`。
3. cuBLAS 的 `time_gemm` 返回值单位是 **µs**（第 150 行用 `std::micro`），而 `*1e-9` 公式要求 **ms**。直接代入会偏大 1000 倍。应先把 12.50 µs 换算成 `0.0125 ms` 再代入（或等价地，把公式改成 `×1e-6` 来吃 µs）。这也解释了 u1-l3 提到的「CSV 表头 `(usec)` 实为 ms」之类的单位混乱——跨框架对比时务必统一单位。
4. `TFlops = 8.796e12 / 0.0125 × 1e-9 ≈ 703.7 TFlops`，约占 H100 fp16 峰值的 71%。（数值待本地复核。）

> 这个综合实践揭示了本仓库一个真实存在的工程难点：**不同基线（cuBLAS 用 µs、TileLang/Triton 用 ms）报告延迟的单位不同**，做跨框架 TFlops 对比时，必须先把所有 latency 统一到同一单位，否则对比完全失真。`data/*.py` 里的正则解析与占位符回填，正是这套统一管线的一部分（详见 u2-l7）。

## 6. 本讲小结

- **FLOPS 公式**：GEMM 用 \(2MNK\)；FlashAttention 用 \(2 \times (2BH S_q S_{kv} D)\)，causal 情况再 `×0.5`。公式与精度无关，但峰值随精度变化。
- **warmup/rep**：TileLang 用固定 warmup+rep（如 matmul `warmup=3, rep=20`），cuBLAS 用「1 次 warmup + 自适应重复到约 100ms」保证测量稳定。两者目的相同：排除冷启动与抖动。
- **shape 配置表**：README 的 V/M/FA/CC/CT 五组族是测试集的**命名约定**；每个算子实际跑的 shape 列表在 `data/*.py` 的 inline 列表里，两者数值可能不一致，复现要以 `data/*.py` 为准。
- **latency → TFlops**：统一公式 `total_flops / latency * 1e-9`，其中 `1e-9 = 1e-3(ms→s) × 1e-12(FLOPS→TFlops)`，隐含约定 **latency 以 ms 为单位**。`do_bench` 返回 ms；matmul 文件 `(s)` 标签是误导。
- **单位是最大的坑**：cuBLAS 返回 µs、TileLang/Triton 返回 ms，跨框架对比前必须统一单位，否则 TFlops 差 1000 倍。
- **causal ×0.5 的理由**：因果掩码使有效计算量约为非 causal 的一半，折半后报告的 TFlops 才反映「实际有用功」，并与非 causal 数字可比。

## 7. 下一步学习建议

本讲建立的是「度量方法论」这把标尺的**数学与约定**部分。接下来两个方向：

1. **继续本单元（u2）**：
   - **u2-l5 cuBLAS 参考基准**：深入 `cublas_benchmark.cu` 的 `time_gemm` 自适应计时与 `cublasGemmEx` 多精度路径，把本讲提到的 cuBLAS 测量策略讲透。
   - **u2-l6 Triton 基线与 `do_bench`**：理解 Triton 侧的 `do_bench` 计时约定，作为与 TileLang 对比的标尺。
   - **u2-l7 数据提取与可视化**：看 `data/*.py` 如何用正则从各框架日志（含不同单位）解析 latency、用 `-1` 占位、回填并绘图，把本讲的「单位统一」问题落实到代码。

2. **进入下一单元（u3）TileLang 语言核心**：有了可信的度量方法后，u3-l8 起开始解剖 TileLang 内核本身（`@autotune`/`@jit`/`T.prim_func`），那时你会反复用到本讲的 TFlops 公式来评估每个内核。

建议先把 u2 剩余三讲读完，把「度量管线」完整建立起来，再进入内核解剖——否则写出的内核「快不快」将无从评判。
