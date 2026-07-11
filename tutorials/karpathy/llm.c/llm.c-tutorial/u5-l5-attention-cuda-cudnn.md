# Attention CUDA：手写版与 cuDNN Flash Attention

## 1. 本讲目标

本讲是「CUDA 主线 train_gpt2.cu 与 llmc 头文件库」单元里最难、也最值得精读的一篇：注意力（attention）在 GPU 上的两种实现。

学完后你应当能够：

1. 说清楚 `llmc/attention.cuh` 里「手写 attention」是如何用 **cuBLASLt 批量矩阵乘 + 一个 online softmax kernel** 拼出来的，以及它为什么仍然会物化完整的 `(B, NH, T, T)` 注意力矩阵。
2. 说清楚 `llmc/cudnn_att.cpp` 是如何用 **cuDNN frontend 的 graph API** 调用 Flash Attention 的，它为什么只需要 `(B, NH, T)` 的统计量而不再需要那个大方阵。
3. 在 `train_gpt2.cu` 里精确指出决定走哪条路径的是 **编译期宏 `ENABLE_CUDNN`（而非任何运行时变量）**，并能解释为什么默认关闭 cuDNN。
4. 理解两种实现的显存/算力/编译时间取舍，能根据场景选择。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义，不会重复展开）：

- **因果自注意力的数学**（u2-l4）：给定 `ln1` 输出，先做一次 `OC=3C` 的 matmul 得到 `qkv(B,T,3C)`，切成 Q/K/V；打分 `scale·Q·Kᵀ`、因果 mask、softmax、加权 value。术语 `preatt`（原始打分）、`att`（归一化权重）来自那里。
- **CUDA 三层执行模型 + 公共地基**（u5-l1、u5-l4）：grid/block/warp、`warpReduceSum`/`warpReduceMax`/`blockReduce`、`Packed128` 向量化、`__ldcs`/`__stcs` cache hint、`NVTX_RANGE_FN()`。
- **cuBLASLt 的封装 `matmul_cublaslt`**（u5-l3）：它的参数里 `transA/transB`、`alpha/beta`、`bias`、batched 维度如何编码，以及 `beta=0` 表示覆盖写、`beta=1` 表示累加的约定。本讲的手写 attention 会**复用**这个封装来做两次矩阵乘，而不是自己写 GEMM。

再补充两个本讲会用到的术语：

- **online softmax（在线 softmax）**：一种「边流式读取、边维护全局最大值与累加和」的数值稳定 softmax，不必先扫一遍求 max 再扫一遍求 exp。它是 Flash Attention 能省显存的核心算法。
- **Flash Attention**：一种不把 `(T, T)` 注意力矩阵完整写回显存的注意力算法，把 softmax 拆成分块的 online softmax，在 SRAM/寄存器里完成。本讲里它由 NVIDIA cuDNN 库提供，llm.c 不自己实现，只负责正确调用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [llmc/attention.cuh](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh) | **手写 attention 路径**（不启用 cuDNN 时的默认实现）。含 permute/unpermute kernel、online softmax kernel、softmax 反向 in-place kernel，以及前向/反向的 launcher。 |
| [llmc/cudnn_att.h](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.h) | cuDNN attention 的**函数声明**：`create_cudnn`/`destroy_cudnn`/`attention_forward_cudnn`/`attention_backward_cudnn`。 |
| [llmc/cudnn_att.cpp](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp) | cuDNN attention 的**实现**：用 cuDNN frontend graph API 构建 SDPA（Scaled Dot Product Attention）计算图并缓存，前向输出 `out + stats`，反向用 `stats` 复算。 |
| [train_gpt2.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu) | CUDA 主线。本讲关注它的三处：顶部 `#ifdef ENABLE_CUDNN` 的 include 切换、`ActivationTensors.att` 字段的两种形状、`gpt2_forward`/`gpt2_backward` 里的前向/反向分支。 |
| [Makefile](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile) | 把 make 变量 `USE_CUDNN=1` 翻译成 `-DENABLE_CUDNN` 编译宏，并探测 cuDNN / cuDNN frontend 头文件路径。 |

## 4. 核心概念与源码讲解

### 4.1 全景：为什么 attention 有两条路径

#### 4.1.1 概念说明

注意力是整个 Transformer 里**显存压力最大**的算子。朴素实现的瓶颈不在算力，而在那个 `(B, NH, T, T)` 的注意力矩阵——对 GPT-2 124M（`B=4, NH=12, T=1024`），单个样本一层就有 \(12 \times 1024 \times 1024 \approx 1.26\times10^7\) 个元素，12 层累计非常大，而且它在反向传播里还要被反复读写。

llm.c 在 CUDA 主线里给了两条路：

- **手写路径（`attention.cuh`，默认）**：物化完整的 `att` 方阵，但用 cuBLASLt 做两次批量矩阵乘、用一个精心优化的 online softmax kernel 做归一化。代码全在仓库里，可读、可改、可教学。
- **Flash Attention 路径（`cudnn_att.cpp`，需 `USE_CUDNN=1`）**：调用 cuDNN 提供的 Flash Attention，**不物化** `att` 方阵，只保存一份用于反向的 `(B, NH, T)` 统计量。显存省很多，但实现是 cuDNN 的黑盒，且编译时间显著增加。

两条路在数学上等价，区别纯粹是**显存 vs 工程复杂度**的取舍。理解这一点是本讲的总纲。

#### 4.1.2 核心流程

不论哪条路径，注意力的前向都可以拆成 4 步：

```
1. QKV 投影:   ln1(B,T,C) --matmul(3C)--> qkv(B,T,3C)     # 在 gpt2_forward 里, 不在这两个文件内
2. permute:    qkv(B,T,3,NH,HS) --> Q,K,V 各(B,NH,T,HS)    # 让"头"成为独立批次
3. 打分+softmax: score = scale·Q·Kᵀ  (B,NH,T,T), 因果mask, softmax --> att
4. 加权求和:   out = att @ V  (B,NH,T,HS), 再 unpermute 回 (B,T,C)
```

其中 `HS = C/NH` 是每个头的通道数，`scale = 1/√HS`。

两条路径的差别全在**第 2~4 步如何执行**：

- 手写路径：第 2 步用一个 permute kernel；第 3 步先 cuBLASLt 算 `scale·K·Qᵀ` 得 `preatt`，再用 `softmax_forward_kernel5` 算 `att`；第 4 步再 cuBLASLt 算 `att @ V`，最后 unpermute。`preatt`/`att` 都物化成 `(B,NH,T,T)`。
- Flash 路径：把第 2~4 步整体交给 cuDNN 的 SDPA 算子，cuDNN 直接吃 `(B,T,3,NH,HS)` 的交错布局（连 permute 都省了），内部用分块 online softmax，只回吐 `out` 和一份 `stats(B,NH,T)`。

#### 4.1.3 源码精读

决定走哪条路径的「总开关」在 `train_gpt2.cu` 顶部的 include 段，是一个纯编译期分支：

[train_gpt2.cu:52-58](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L52-L58) — 启用 cuDNN 时包含 `cudnn_att.h`，否则包含 `attention.cuh`。**这是编译期二选一，不是运行时 `if`**：两套实现的函数名都不同（`attention_forward_cudnn` vs `attention_forward`），所以同一份二进制里只存在其中一套。

注意 `attention.cuh` 第一行的自我定位注释——它是「fallback」：

[llmc/attention.cuh:1-3](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L1-L3) 说明这个文件是「不使用 cuDNN Flash Attention 时的回退实现」。

而 `cudnn_att.h` 的函数签名透露了 Flash 路径最大的不同：它的前向多了一个 `stats` 输出：

[llmc/cudnn_att.h:12-19](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.h#L12-L19) — `attention_forward_cudnn` 输出 `out(B,T,NH,HS)` 与 `stats(B,NH,T)`；反向 `attention_backward_cudnn` 用这份 `stats` 复算，不再需要那个大方阵。

#### 4.1.4 代码实践

> **实践目标**：在不编译的前提下，建立「两条路径、两种函数名、两种显存布局」的直觉。

操作步骤：

1. 打开 `train_gpt2.cu` 第 52–58 行，确认这是 `#ifdef … #else … #endif` 的编译期分支。
2. 在仓库里用搜索（`attention_forward_cudnn`、`attention_forward(`）确认：这两个名字**不会**同时出现在编译产物里。
3. 观察 `ActivationTensors.att` 的注释：

[train_gpt2.cu:177-182](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L177-L182) — 同一个字段 `att`，cuDNN 时是 `float* (L,B,NH,T)`，手写时是 `floatX* (L,B,NH,T,T)`。

**需要观察的现象**：同一个结构体字段的类型和形状随宏变化——这是「编译期切换」的典型副作用，C++ 模板/宏里很常见，但要求两套代码对「att 到底是什么」达成默契。

**预期结果**：你能用一句话讲清——「att 在手写路径里是物化的注意力方阵，在 cuDNN 路径里退化成一份统计量」。无需运行即可确认（纯静态阅读）。

#### 4.1.5 小练习与答案

**练习 1**：既然两条路径在 `train_gpt2.cu` 里是 `#ifdef` 二选一，那 `gpt2_forward` 函数体里的 `attention_forward(...)` 调用，在启用 cuDNN 编译时会发生什么？

**答案**：启用 cuDNN 时，`#else` 分支（含 `attention_forward(...)` 调用）整段被预处理器删掉，编译器根本看不到它；同理关闭 cuDNN 时 `attention_forward_cudnn(...)` 调用被删掉。所以不会有「函数找不到」的链接错误。

**练习 2**：Flash 路径的前向输出里为什么有一个 `stats`？手写路径有没有等价物？

**答案**：`stats(B,NH,T)` 存的是每个 query 位置做 online softmax 时的「log-sum-exp」类统计量（全局最大值与归一化因子），反向重算 attention 时要用它来稳定地复现前向的 softmax 归一化。手写路径因为物化了完整 `att` 方阵，反向直接读 `att` 即可，不需要单独的 `stats`。

---

### 4.2 手写 attention：cuBLASLt 组装 + online softmax kernel

#### 4.2.1 概念说明

`attention.cuh` 里的「手写」其实**不是**从头手写每一行——两次矩阵乘（`Q·Kᵀ` 和 `att·V`）它直接复用了 u5-l3 讲过的 `matmul_cublaslt`（用 batched 模式把 `B*NH` 当批次）。真正「手写」的只有三件事：

1. **permute / unpermute**：把交错布局 `(B,T,3,NH,HS)` 重排成 batched 友好的 `(B,NH,T,HS)`。
2. **online softmax kernel**（`softmax_forward_kernel5`）：把 `scale·preatt` 行做因果 softmax，写出 `att`。
3. **softmax 反向 in-place kernel**：把 `datt` 原地变成 `dpreatt`。

这套设计的精髓在于：**把注意力里「两个大 GEMM」交给高度优化的 cuBLASLt，只把「softmax 这个 cuBLAS 做不了的部分」留给自己手写**。这是工程上的明智分工。

online softmax 的核心思想（本模块重点）：

标准 softmax 为了数值稳定要先减最大值 \(m\)：

\[
p_i = \frac{e^{x_i - m}}{\sum_j e^{x_j - m}},\quad m=\max_j x_j
\]

但这要求**两遍扫描**：一遍求 \(m\)，一遍求 exp。online softmax 把它压成一遍，维护「运行最大值」\(m\) 和「运行和」\(s\)：每读到一个新值 \(x\)，如果它让最大值从 \(m_{\text{old}}\) 涨到 \(m_{\text{new}}\)，就把之前累加的和整体缩放：

\[
s_{\text{new}} = s_{\text{old}}\cdot e^{m_{\text{old}}-m_{\text{new}}} + e^{x - m_{\text{new}}}
\]

这样一边流式读取、一边就能得到稳定的分母，最后一遍写出 \(p_i = e^{x_i - m}/s\)。Flash Attention 之所以能省显存，本质就是把这种「分块 online softmax」搬进了片上存储。

#### 4.2.2 核心流程

前向 `attention_forward`（[llmc/attention.cuh:195-235](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L195-L235)）的执行顺序：

```
1. permute_kernel:        inp(B,T,3,NH,HS)  --> q,k,v 各 (B,NH,T,HS)
2. matmul_cublaslt:       preatt = scale·(k @ qᵀ)          # batched, B*NH 批次
                          注: preatt 复用 inp 的显存做 scratch!
3. softmax_forward_kernel5: att = causal_softmax(preatt)    # online softmax, 一个 warp 管一行
4. matmul_cublaslt:       vaccum = v @ att                  # batched
5. unpermute_kernel:      vaccum(B,NH,T,HS) --> out(B,T,C)
```

注意两处「省显存」的工程技巧：

- `preatt` 直接复用 `inp` 缓冲（前向之后 `inp` 不再需要，注释明说「re-use it as a scratch buffer」）。
- `vaccum`（第 4 步结果）也复用 `inp`。

反向 `attention_backward`（[llmc/attention.cuh:239-276](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L239-L276)）严格按前向逆序：unpermute 反向 → 算 `datt` → 算 `dv` → softmax 反向 in-place（`datt`→`dpreatt`）→ 算 `dq` → 算 `dk` → permute 反向。两次反向 GEMM 同样复用 `matmul_cublaslt`。

#### 4.2.3 源码精读

**permute kernel**——元素级重排，一个线程搬一个元素：

[llmc/attention.cuh:14-33](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L14-L33) 把交错布局 `(B,T,3,NH,HS)` 拆成 Q/K/V 三块连续输出，用 `__ldcs`（cache-streaming load）读。关键是下标换算 `inp_idx` 与 `idx` 的对应关系。

**online softmax kernel** 是本模块的灵魂：

[llmc/attention.cuh:85-150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L85-L150) — `softmax_forward_kernel5`。

逐段读它：

- 第 90 行 `assert(T % 4 == 0)`：每次处理 4 个元素（向量化）。
- 第 91–93 行：在 warp 内分 lane/warp——**一个 warp 协作处理 attention 矩阵的一行**（`idx` 范围 `N*T`，`N=B*NH`）。
- 第 100 行 `idx = (gridDim.x - blockIdx.x - 1) * num_warps + warp_id`：**倒序遍历 block**。注释解释这是缓存微优化——softmax 反向跑完后，方阵左上角尽量留在 cache 里，惠及紧随其后的 matmul。
- 第 112–136 行就是上面讲的 online softmax 主体：维护 `maxval` 和 `sumval`，遇到更大值就 `sumval *= expf(inv_temperature * (old_maxval - maxval))` 缩放（第 125、134 行）。
- 第 138–139 行：跨 lane 用 `warpReduceMax` 得到全局 max，再缩放 `sumval`。
- 第 141–148 行：`warpReduceSum` 得到分母 `sum`，最后**重算**（第 146 行注释「recalculation is faster than doing the round-trip through memory」）写出 `att`，且只写到 `own_pos`（因果下三角）。

**反向 in-place kernel** 把 `datt` 变成 `dpreatt`：

[llmc/attention.cuh:152-190](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L152-L190) — 它先对因果行求 `local_sum = Σ att·datt`（softmax 反向的标准项），再写 `dpreatt = scale·att·(datt - local_sum)`，并把非因果位置显式置零。`in-place` 指它把结果写回 `datt` 同一块内存（第 169 行 `dpreatt_bth = datt + t*T`）。

**前向 launcher** 把上述步骤串起来：

[llmc/attention.cuh:217-228](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L217-L228) — 注意两次 `matmul_cublaslt` 的 batched 参数：第 218 行 `k @ q` 得 `preatt`（`B*NH` 批次），第 228 行 `v @ att` 得 `vaccum`。`preatt`/`vaccum` 都指向 `inp`（scratch 复用）。

#### 4.2.4 代码实践

> **实践目标**：读懂 online softmax 的「缩放因子」从哪来，并能在纸上演算一个 4 元素的小例子。

操作步骤：

1. 打开 [llmc/attention.cuh:116-129](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L116-L129)，找到 `sumval *= expf(inv_temperature * (old_maxval - maxval));` 这一行。
2. 设 `inv_temperature = 1`，4 个原始分数为 `x = [1, 3, 2, 2]`，按 online 流程手算：
   - 读 1：`maxval=1, sumval=e^(1-1)=1`
   - 读 3：`old=1, new=3`，`sumval = 1·e^(1-3) + e^(3-3) = e^-2 + 1 ≈ 1.135`
   - 读 2：`old=3, new=3`（不变），`sumval = 1.135 + e^(2-3) ≈ 1.135 + 0.368 = 1.503`
   - 读 2：`sumval = 1.503 + 0.368 = 1.871`
3. 用标准两遍 softmax 验证：`m=3`，分母 `= e^-2 + e^0 + e^-1 + e^-1 ≈ 0.135+1+0.368+0.368 = 1.871`。

**需要观察的现象**：两种算法得到的分母完全相同——证明 online 缩放因子 `e^(old-new)` 正确补偿了「最大值变化导致旧 exp 全部需要重缩放」这件事。

**预期结果**：手算分母与两遍法一致（≈1.871）。若不一致，检查你是否漏了缩放那一步。此为纯算术验证，「待本地验证」仅指你需要在纸或计算器上实际算一遍。

#### 4.2.5 小练习与答案

**练习 1**：`softmax_forward_kernel5` 为什么用「一个 warp 管一行」而不是「一个 thread 管一行」？

**答案**：一行长度是 `T`（如 1024），单个 thread 串行算太慢；一个 warp（32 个 lane）可以让 32 个 lane 并行累加，再用 `warpReduceMax`/`warpReduceSum` 在 warp 内二叉归约，刚好对应「一行的 reduce」语义，且 warp 归约用 shuffle 指令几乎零开销。

**练习 2**：前向 launcher 里 `preatt` 为什么可以直接复用 `inp` 缓冲？这样不会影响反向吗？

**答案**：注释（[attention.cuh:199-200](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/attention.cuh#L199-L200)）说明 `inp` 在前向之后不再被需要（QKV 已 permute 进 `qkvr`），所以可当 scratch。反向的输入是 `qkvr` 和 `att`，不依赖 `inp` 的原始内容，因此复用安全。

**练习 3**：两次 `matmul_cublaslt` 调用的 batched 维度都是 `B*NH`，这说明什么？

**答案**：说明「多头」在本实现里被当成矩阵乘的**批次维**——每个头独立做一次 `(T,T)@(T,HS)` 的 GEMM，互不混合。这正是「多头 = 多个并行的小注意力」在工程上的落地方式。

---

### 4.3 cuDNN frontend 封装：Flash Attention 的 graph API

#### 4.3.1 概念说明

当 `ENABLE_CUDNN` 定义时，attention 不再走 `attention.cuh`，而是走 `cudnn_att.cpp`。这里用的是 **cuDNN frontend**——NVIDIA 提供的一层 C++ 封装，让你用「构建计算图」的方式调用 cuDNN，而不是写 C 风格的描述符。

关键概念：

- **SDPA（Scaled Dot Product Attention）算子**：cuDNN 把「打分 + 因果 mask + softmax + 加权 value」打包成一个高级算子，内部就是 Flash Attention。你只要声明 Q/K/V、attn_scale、是否因果，cuDNN 自动选最高效的 kernel。
- **graph 的构建很慢**：注释里反复出现「this is the VERY SLOW PART」。所以代码用一个 `std::map` 按 `(B,H,T,HS,...)` 做**缓存**，同一个形状只在第一次构建，之后直接复用。
- **stats 张量**：Flash Attention 不物化 `att`，但反向需要前向 softmax 的归一化信息，于是 cuDNN 把每个 query 位置的 log-sum-exp 统计量存进 `stats(B,NH,T)`（fp32）。

还有一个**精度限制**值得注意：cuDNN 路径**不支持 FP32**（注释里直接 `static_assert(false)`），只支持 FP16/BF16。这与 u6-l1 讲的混合精度主线一致。

#### 4.3.2 核心流程

前向 `attention_forward_cudnn`（[llmc/cudnn_att.cpp:222-254](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L222-L254)）：

```
1. is_inference_only = (stats == nullptr)        # 生成时传 stats, 推理时传 NULL
2. graph = lookup_cache_or_build_graph_fwd(...)  # 命中缓存或构建 SDPA 图
3. 准备 variant_pack: 把 Q/K/V/O/Stats 的 UID 映射到真实设备指针
4. graph->execute(cudnn_handle, variant_pack, cudnn_workspace)
```

构建图的过程（`lookup_cache_or_build_graph_fwd`，[cudnn_att.cpp:60-135](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L60-L135)）：

```
1. new Graph, 设 io/intermediate/compute 数据类型 (BF16 进出, FP32 中间与计算)
2. 声明 Q/K/V 张量, 关键: 用 stride 描述交错布局, 不做物理 permute!
   stride={3*H*HS*T, HS, 3*H*HS, 1}  → 直接告诉 cuDNN "Q 在内存里是隔 3 项取一次"
3. sdpa_options: set_causal_mask(true), set_attn_scale(...)
4. graph->sdpa(Q,K,V, options) → 得到 (O, stats)
5. validate → build_operation_graph(慢) → create_execution_plans → check_support → build_plans
6. 按需扩容 cudnn_workspace
7. 存进 cache
```

反向 `attention_backward_cudnn`（[cudnn_att.cpp:256-288](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L256-L288)）结构对称：用 `sdpa_backward` 算子，输入 Q/K/V/O/dO/stats，输出 dQ/dK/dV。

#### 4.3.3 源码精读

**精度守卫**——FP32 直接拒绝：

[llmc/cudnn_att.cpp:13-20](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L13-L20) — `#if defined(ENABLE_FP32)` 里 `static_assert(false)`，FP16 用 `HALF`，默认（BF16）用 `BFLOAT16`。

**用 stride 描述交错布局，省掉 permute**——这是 Flash 路径相对手写路径的一个额外优势：

[llmc/cudnn_att.cpp:77-88](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L77-L88) — Q/K/V 的 `set_dim({B,H,T,HS})` 配 `set_stride({3*H*HS*T, HS, 3*H*HS, 1})`。注释（第 76 行）明说：QKV 是 `(B,T,3,NH,HS)`，cuDNN 能直接处理，**无需外部 permute**。对比手写路径专门的 `permute_kernel`，这里省了一个 kernel 启动和一份中间显存。

**SDPA 选项**——因果 mask 与 attn_scale 在这里设：

[llmc/cudnn_att.cpp:96-99](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L96-L99) — `set_is_inference`、`set_attn_scale`、`set_causal_mask(true)`。注意 `scale = 1/√HS` 与手写路径完全一致（第 239 行）。

**stats 只在训练时输出**：

[llmc/cudnn_att.cpp:107-113](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L107-L113) — `assert(stats == nullptr || is_inference_only == false)`：推理模式不产出 stats，训练模式才产出（反向要用）。这呼应了 u3-l3 讲过的「推理/生成 vs 训练」两条路径之分。

**构建很慢 → 缓存**：

[llmc/cudnn_att.cpp:56-57](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L56-L57) 定义两个 `std::map` 缓存（前向键含 `is_inference_only`，反向键不含）；[cudnn_att.cpp:62-69](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L62-L69) 是典型的「find 命中则返回，否则构建后 insert」。

**workspace 动态扩容**：

[llmc/cudnn_att.cpp:124-130](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L124-L130) — 注释提到 H100 上可能需要约 16B（注释笔误，应为 16MiB 量级），默认 cuDNN 最多用 256MiB，所以按需 `cudaMalloc` 而不是一次性开满。这与 u5-l2 讲的 cuBLASLt workspace 思路一致。

**生命周期**：`create_cudnn`/`destroy_cudnn` 管理 handle 与 workspace（[cudnn_att.cpp:290-297](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L290-L297)）。

#### 4.3.4 代码实践

> **实践目标**：理解 cuDNN frontend 的「UID → 设备指针」绑定机制，看清前向如何把 `inp` 同时当 Q/K/V。

操作步骤：

1. 打开 [llmc/cudnn_att.cpp:42-53](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L42-L53)，看 `enum UIDs` 定义了一组整数 ID（Q_UID、K_UID…）。
2. 打开 [llmc/cudnn_att.cpp:236-244](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L236-L244)，看 `variant_pack` 怎么把 UID 映射到指针：

```cpp
void* devPtrQ = inp;
void* devPtrK = (inp + C);
void* devPtrV = (inp + 2 * C);
```

3. 解释：`inp` 是 `(B,T,3,NH,HS)`，沿「3」那一维每隔 `C=NH*HS` 一个元素；于是 Q/K/V 共享同一块显存、靠不同的起始偏移区分，再配合第 77–88 行的 stride 描述，cuDNN 就能正确读出三个张量。

**需要观察的现象**：Q/K/V 的指针全在同一块 `inp` 内存里，只是偏移不同——这与手写路径「permute 出三块独立连续内存」形成鲜明对比。

**预期结果**：你能说清「cuDNN 靠 stride 描述交错布局，省掉物理 permute；手写路径靠 permute kernel 把 Q/K/V 物理分开，方便喂给 cuBLASLt 的 batched GEMM」。这是两条路径工程风格的根本差异。纯静态阅读，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么前向缓存的键是 `(B,H,T,HS,is_inference_only)` 五元组，而反向只有 `(B,NH,T,HS)` 四元组？

**答案**：前向区分推理与训练（推理不输出 stats，图的输出张量集合不同，是两张不同的图），所以键要含 `is_inference_only`。反向永远需要 stats、永远训练态，没有这种二分，所以键少一维。

**练习 2**：cuDNN 路径为什么「不需要」手写路径里的 `softmax_autoregressive_backward_inplace_kernel`？

**答案**：Flash Attention 的反向由 cuDNN 的 `sdpa_backward` 算子整体完成，它内部用自己的方式（基于 `stats`）重算 softmax 的雅可比，不需要物化 `att` 也不需要外部的 softmax 反向 kernel。llm.c 只是调用，不参与实现。

**练习 3**：如果有人想在 FP32 精度（`PRECISION=FP32`）下启用 cuDNN attention，会发生什么？

**答案**：编译期 `static_assert(false, "cuDNN is not supported in FP32 mode.")`（[cudnn_att.cpp:13-14](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L13-L14)）直接让编译失败。要跑 cuDNN Flash Attention，必须用 FP16 或 BF16。

---

### 4.4 编译期 ENABLE_CUDNN 切换：Makefile、分支与激活差异

#### 4.4.1 概念说明

本模块回答一个关键问题：**到底「由谁」决定走哪条 attention 路径？**——答案是 **Makefile 变量 `USE_CUDNN` 翻译成的编译宏 `ENABLE_CUDNN`**，全程在编译期完成，没有任何运行时开关。这一点务必记牢，因为很多人会误以为有个命令行参数 `-cudnn` 之类在运行时切换。

链路是这样的：

```
make USE_CUDNN=1
   └─(Makefile ifeq)─> NVCC_FLAGS += -DENABLE_CUDNN
        └─(预处理器)─> train_gpt2.cu 里所有 #ifdef ENABLE_CUDNN 选中 cudnn 分支
             └─(编译)─> 产物里只有 attention_*_cudnn, 没有 attention_forward
```

而默认 `USE_CUDNN ?= 0`（[Makefile:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L26)），所以**开箱即用的二进制走的是手写 attention 路径**。

为什么默认关闭 cuDNN？README 给了两个理由（[README.md:120](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L120)）：

1. **编译时间膨胀**：cuDNN frontend 是大量 C++ 头文件，把编译从「几秒」拖到「约一分钟」，且 `cudnn_att.cpp` 需要单独预编译成 `.o`（Makefile 里 `NVCC_CUDNN = $(BUILD_DIR)/cudnn_att.o`）。
2. **代码路径很新**（README 写于 2024 年 5 月）：作者希望先让手写路径稳定。

#### 4.4.2 核心流程

`ENABLE_CUDNN` 在 `train_gpt2.cu` 里一共点亮 6 处（见 u5-l5 源码地图的 grep 结果），可分三类：

| 位置 | 行号 | 作用 |
| --- | --- | --- |
| include 切换 | 52–58 | 选 `cudnn_att.h` 还是 `attention.cuh` |
| 激活字段类型 | 178–182 | `att` 是 `float*(L,B,NH,T)` 还是 `floatX*(L,B,NH,T,T)` |
| 激活尺寸 | 230–235 | `att` 分配 `L*B*NH*T` 还是 `L*B*NH*T*T` |
| 前向分支 | 718–731 | 调 `attention_forward_cudnn` 还是 `attention_forward` |
| 反向分支 | 910–919 | 调 `attention_backward_cudnn` 还是 `attention_backward` |
| 生命周期 | 1194–1204 | `create_cudnn`/`destroy_cudnn` 的调用 |

#### 4.4.3 源码精读

**Makefile 的翻译**——`USE_CUDNN=1` → `-DENABLE_CUDNN`，并探测头文件：

[Makefile:113-127](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L113-L127) — `ifeq ($(USE_CUDNN), 1)` 里：先在 `$(HOME)/cudnn-frontend/include` 或 `./cudnn-frontend/include` 找 cuDNN frontend 头文件（第 115–123 行），找不到就 `$(error)`；找到才 `-I` 包含、`-lcudnn` 链接、`-DENABLE_CUDNN` 定义宏、把 `cudnn_att.o` 加入依赖（第 124–127 行）。这是 u1-l2 讲过的「环境自动探测」模式在 cuDNN 上的又一次应用。

**默认关闭的提示**：

[Makefile:149-151](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L149-L151) — `USE_CUDNN` 不为 1 时打印「cuDNN is manually disabled by default, run make with USE_CUDNN=1 to try to enable」。

**前向分支**——本讲 practice_task 的核心目标：

[train_gpt2.cu:718-731](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L718-L731) — cuDNN 分支：先 `matmul_forward_cublaslt(l_qkvr, …)`（QKV 投影结果直接进 `l_qkvr`），再 `attention_forward_cudnn(l_atty, (float*)l_att, l_qkvr, …)`，其中 `l_att` 指向 `acts.att + l*B*NH*T`（小尺寸 stats）。手写分支：`scratch` 做 QKV 投影，`l_att` 指向 `acts.att + l*B*NH*T*T`（大方阵），且当 `T != seq_len` 时要先 `cudaMemset` 清零未用部分（第 724–726 行）。

**反向分支**结构对称：

[train_gpt2.cu:910-919](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L910-L919) — cuDNN 分支用 `l_att`（stats）+ `l_atty`（前向 out）调用 `attention_backward_cudnn`；手写分支用 `l_att`（大方阵）+ 两个 scratch 缓冲（复用 `l_atty`、`l_fch_pre_gelu` 的内存，第 916–917 行）。

**激活尺寸差异**——显存账本的直接体现：

[train_gpt2.cu:230-235](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L230-L235) — cuDNN：`tensors[5] = L*B*NH*T`；手写：`tensors[5] = L*B*NH*T*T`。对 124M（`L=12,B=4,NH=12,T=1024`），前者 ≈ 6×10⁵ 个 fp32 ≈ 2.4MB，后者 ≈ 1.5×10⁸ 个 floatX ≈ 300MB（BF16）——差了约两个数量级。这就是 Flash Attention 的显存收益。

**生命周期**：

[train_gpt2.cu:1194-1195](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1194-L1195) 与 [train_gpt2.cu:1203-1204](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L1203-L1204) — `create_cudnn()` 在初始化时创建 handle，`destroy_cudnn()` 在退出时释放。手写路径不需要这两个调用（它们也被 `#ifdef` 包起来）。

#### 4.4.4 代码实践（对应规格里的 practice_task）

> **实践目标**：在 `train_gpt2.cu` 的 `gpt2_forward` 中找到 `attention_forward_cudnn` 与 `attention_forward` 的分支，说明由哪个宏/变量决定走哪条路径，以及为何默认关闭 cuDNN。

操作步骤：

1. 在 [train_gpt2.cu:718-731](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/train_gpt2.cu#L718-L731) 定位前向分支：第 718 行 `#ifdef ENABLE_CUDNN`、第 721 行 `attention_forward_cudnn(...)`、第 722 行 `#else`、第 730 行 `attention_forward(...)`、第 731 行 `#endif`。
2. 确认决定路径的是**编译宏 `ENABLE_CUDNN`**（不是运行时变量）。该宏由 [Makefile:113-127](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L113-L127) 在 `make USE_CUDNN=1` 时经 `-DENABLE_CUDNN` 定义，默认 [Makefile:26](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L26) `USE_CUDNN ?= 0` 故默认未定义。
3. 读 [README.md:120](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/README.md#L120)，记录默认关闭的两条理由：编译时间从几秒膨胀到约一分钟；该代码路径当时还很新。
4. （可选，需要 GPU 与 cuDNN 环境）在有 cuDNN 的机器上：

```bash
make train_gpt2cu USE_CUDNN=1        # 走 Flash Attention 路径
./train_gpt2cu ...                    # 观察训练 loss
# 另开一个终端, 不带 USE_CUDNN 重新编译对比
```

**需要观察的现象**：

- 编译时 Makefile 会打印「✓ cuDNN found, will run with flash-attention」（[Makefile:116](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L116)），或未启用时打印「cuDNN is manually disabled by default」（[Makefile:150](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/Makefile#L150)）。
- 两种二进制的训练 loss 曲线应当一致（数学等价）。

**预期结果**：能口述完整链路「`make USE_CUDNN=1` → `-DENABLE_CUDNN` → `#ifdef` 选 cuDNN 分支 → 调 `attention_forward_cudnn`」。步骤 1–3 为纯静态阅读可完成；步骤 4「待本地验证」（依赖 GPU + cuDNN 安装，参考 README 的 `apt-get install libcudnn9-dev-cuda-12` 与 cuDNN frontend 克隆说明）。

#### 4.4.5 小练习与答案

**练习 1**：如果用户运行 `./train_gpt2cu --use_cudnn 1`（假设有这个命令行参数），会发生什么？

**答案**：什么都不会发生——根本不存在这个运行时参数。路径在编译期就由 `ENABLE_CUDNN` 定死了，运行时无法切换。要换路径必须重新 `make`。这是本讲最容易踩的误解。

**练习 2**：同一个 `acts.att` 字段，cuDNN 时是 `float*`、手写时是 `floatX*`。为什么类型也变了？

**答案**：cuDNN 的 `stats` 必须是 fp32（[cudnn_att.cpp:109](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/llmc/cudnn_att.cpp#L109) `set_data_type(FLOAT)`），它是归一化统计量，对精度敏感；手写路径的 `att` 是注意力权重，跟主激活精度 `floatX`（BF16/FP16）即可。所以类型随语义不同而不同。

**练习 3**：从显存账本看，启用 cuDNN 对训练「能开多大 batch」有什么影响？

**答案**：手写路径 `att` 占 `L*B*NH*T*T`，cuDNN 路径只占 `L*B*NH*T`，前者大一个 `T` 因子（如 1024×）。所以启用 cuDNN 能显著省显存（约两个数量级），从而允许更大的 batch 或更长的序列——这正是 Flash Attention 的核心卖点。

---

## 5. 综合实践

**任务**：写一份「两种 attention 路径对照表」，并据此解释一个真实的显存问题。

1. **填表**（全部基于本讲引用的真实源码，不要编造）：

| 维度 | 手写路径（`attention.cuh`） | Flash 路径（`cudnn_att.cpp`） |
| --- | --- | --- |
| 启用方式 | 默认（`USE_CUDNN=0`） | `make USE_CUDNN=1` |
| 决定开关的宏 | （未定义 `ENABLE_CUDNN`） | `ENABLE_CUDNN` |
| 支持的精度 | FP32/FP16/BF16 | 仅 FP16/BF16（FP32 编译断言失败） |
| 是否物化 `att` 方阵 | 是 `(B,NH,T,T)` | 否，只存 `stats(B,NH,T)` |
| 两次 GEMM 由谁做 | cuBLASLt（batched） | cuDNN SDPA 内部 |
| softmax 由谁做 | 自写 `softmax_forward_kernel5`（online softmax） | cuDNN SDPA 内部 |
| 是否需要 permute kernel | 是 | 否（用 stride 描述交错布局） |
| 是否需要 `create_cudnn` | 否 | 是 |
| 反向输入 | `att` 方阵 | `stats` + 前向 `out` |

2. **算一笔账**：GPT-2 124M（`L=12, B=4, NH=12, T=1024, C=768`），手写路径的 `att` 占多少字节？（BF16，2 字节/元素）答案：`12*4*12*1024*1024*2 ≈ 302MB`。cuDNN 路径的 `stats` 呢？（fp32，4 字节/元素，`L*B*NH*T`）答案：`12*4*12*1024*4 ≈ 2.4MB`。

3. **回答**：在一块 24GB 显存的消费级 GPU 上训练 124M，单从 attention 的 `att` 缓冲看，哪条路径更友好？如果换成 `T=2048` 呢？

   **参考答案**：手写路径 `att` 在 `T=1024` 已占约 302MB，`T=2048` 会涨到约 1.2GB（`T²` 增长）；cuDNN 路径 `stats` 仅线性增长（`T=2048` 约 4.8MB）。长序列下 cuDNN 优势放大，这正是 Flash Attention 对大模型训练的关键意义。

## 6. 本讲小结

- llm.c 的 CUDA attention 有**两条编译期互斥的路径**：默认的手写 `attention.cuh` 与可选的 cuDNN Flash `cudnn_att.cpp`，由编译宏 `ENABLE_CUDNN` 决定，没有运行时开关。
- 手写路径并非纯手写：两次矩阵乘复用 cuBLASLt（batched，`B*NH` 批次），自己只写 permute/unpermute、online softmax 与 softmax 反向 in-place kernel。
- `softmax_forward_kernel5` 用 **online softmax**——边流式读取边维护运行 max 与运行 sum，遇更大值时用 `sum *= exp(old_max - new_max)` 缩放——一遍扫描完成数值稳定 softmax，一个 warp 管一行、倒序遍历 block 做缓存优化。
- cuDNN 路径用 **frontend graph API** 调用 SDPA 算子，靠 stride 描述 `(B,T,3,NH,HS)` 交错布局省掉 permute，用 `std::map` 缓存计算图（构建极慢），只输出 `stats(B,NH,T)` 而非大方阵；不支持 FP32。
- 两条路径在 `train_gpt2.cu` 里点亮 6 处 `#ifdef`：include 切换、`att` 字段类型/尺寸、前向分支、反向分支、`create/destroy_cudnn` 生命周期。
- 默认关闭 cuDNN 的原因是**编译时间膨胀**（几秒→约一分钟）与代码路径较新；启用方式是 `make train_gpt2cu USE_CUDNN=1`（需安装 cuDNN + cuDNN frontend 头文件）。

## 7. 下一步学习建议

- **接 u6-l1（混合精度与 master weights）**：本讲反复出现的「FP32 统计量 vs BF16 主激活」「FP32 不支持 cuDNN」正是混合精度主题的引子。建议接着读 `train_gpt2.cu` 里 `use_master_weights` 与 `grad_scale` 如何与 attention 的 BF16 计算配合。
- **接 u6-l2（recompute 与融合算子）**：本讲的 `att` 大方阵显存压力，与 u5-l1 讲的 `recompute`（丢弃前向激活、反向重算）是同一类「显存 vs 算力」权衡的两种解法。可对比「用 Flash Attention 省掉 att」与「用 recompute 省掉 ln/fch_gelu」两种思路的异同。
- **接 u7-l1（dev/cuda 内核库）**：若你想看 attention 的**更多手写版本**（从朴素到 cooperative groups 的逐步优化），去读 [dev/cuda/attention_forward.cu](https://github.com/karpathy/llm.c/blob/f1e2ace651495b74ae22d45d1723443fd00ecd3a/dev/cuda/attention_forward.cu)，那里有同一算子的多版本对照与 benchmark，是理解手写 attention kernel 优化的最佳续读材料。
- **想深入 Flash Attention 算法本身**：可阅读原始论文（Dao, 2022）对照本讲「online softmax + 分块」的描述，再回看 `cudnn_att.cpp` 的 SDPA 调用，理解 cuDNN 在黑盒里为你省下了多少显存搬运。
