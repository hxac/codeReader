# u3-l3 split 数量启发式与中间缓冲分配

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 decoding 场景的「并行度饥饿」问题：为什么 batch×heads 很小、KV 很长时，不切分 KV 会让大部分 SM 空转。
2. 写出占用率效率模型 \( \mathrm{eff}(s) = n_{\text{waves}} / \lceil n_{\text{waves}} \rceil \)，并用它手算一个具体配置下被选中的 `num_splits`。
3. 逐行读懂 `num_splits_heuristic` 的四个组成部分：提前返回、split 资格判定（`is_split_eligible`）、效率枚举、85% 规则。
4. 解释为什么 `set_params_splitkv` 在启发式结果上额外 `+1`（答案是：预留一个槽位给 residual kernel 的部分输出），并在 kernel 启动代码里找到证据。
5. 说出 `softmax_lse_accum` 与 `out_accum` 两块 float 中间缓冲的形状、分配时机，以及 `num_splits ≤ 128` 约束与 combine kernel 模板阶梯的对应关系。

本讲是第三单元（绑定层）的收尾：u3-l1 讲了「怎么进来」，u3-l2 讲了「参数装在哪」，本讲讲「在启动 kernel 之前，CPU 侧做的最后一项动态决策」。

## 2. 前置知识

### 2.1 SM、CTA 与「波次（wave）」

- GPU 由若干 **SM**（Streaming Multiprocessor）组成，例如 A100 有 108 个 SM。
- kernel 以 **CTA**（CUDA thread block，下文称 block）为单位调度到 SM 上。一个 SM 可以同时驻留多个 block（取决于线程数、寄存器、共享内存）。
- 如果待启动的 block 数不是 SM 容量的整数倍，GPU 就要分批执行，每一批称为一个**波次（wave）**。最后一个不满的波次里，空闲的 SM 在「陪跑」——这就是利用率损失的定义。

### 2.2 decode 为什么并行度饥饿（承接 u1-l1、u3-l1）

注意力 kernel 的自然并行维度是 `(batch, heads, seqlen_q 的 M 块)`。decode 阶段每步只处理 1 个新 token：

- `seqlen_q = 1`；经过 u3-l1 讲过的 GQA 重排 `seqlenq_ngroups_swapped` 后，`seqlen_q` 变为 ngroups（如 Llama-3.1-8B 是 32/8 = 4），`num_heads` 变为 `num_heads_k = 8`。M 块数仍只有 1。
- 于是 block 总数约等于 `batch × num_heads_k`。batch=1 时只有 8 个 block，而 A100 有 108 个 SM——**不到 8% 的 SM 在干活**，而 KV cache 却可能有几万个 token 等着被读。

矛盾就在这里：**计算粒度（block 数）与数据量（KV 长度）严重失衡**。split-KV 的思路是把 KV 序列维切成 `num_splits` 段，每段一个 block 去算「部分注意力」，最后再合并——block 数直接乘以 `num_splits`。

### 2.3 部分结果怎么合并：LSE 一句话预告

每个 split 产出的是「未归一化的加权和 + 一个 log-sum-exp 标量（LSE）」。合并时按 \( e^{lse_i - lse_{\max}} \) 权重把各 split 的输出加权求和。本讲只需要知道：**合并要求每个 split 把 float 部分输出和 LSE 写到全局内存的独立槽位里**——这正是中间缓冲存在的原因。完整推导在 u5-l5。

### 2.4 记号

- 上取整除法：`ceildiv(a, b) = (a + b - 1) / b`（整数除法）。
- 本讲用 \( B \) 表示 `batch_nheads_mblocks`（block 基数），\( s \) 表示 split 数，\( S \) 表示「可同时驻留的 block 槽位数」。注意代码里的 `num_SMs` 实际传的是 `SM 数 × 2`，见 4.3 节。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
|---|---|
| `csrc/bit_decode/decode_api.cpp` | 主角：`num_splits_heuristic`（L246-L280）与 `set_params_splitkv`（L282-L313），以及 `mha_fwd_kvcache` 中的调用点（L512-L519） |
| `csrc/bit_decode/src/include/flash.h` | `Flash_fwd_params` 中承载结果的字段：`oaccum_ptr`（L106）、`softmax_lseaccum_ptr`（L118）、`num_splits`（L191） |
| `csrc/bit_decode/src/flash_fwd_launch_template.h` | 消费端：grid 划分（L82-L104）与 combine kernel 的 `Log_max_splits` 阶梯（L106-L127） |
| `csrc/bit_decode/src/flash_fwd_kernel.h` | 证据：residual kernel 固定写第 `num_splits-1` 号槽位（L1678），splitkv kernel 用 `blockIdx.y` 作 split 索引（L1692-L1694），KV 段划分公式（L614-L616） |
| `bit_decode/bit_decode_interface.py` | Python 侧恒传 `num_splits=0`（L76-L81、L96-L101 的最后一个位置参数），即永远走启发式 |

## 4. 核心概念与源码讲解

### 4.1 并行度饥饿与波次效率模型

#### 4.1.1 概念说明

「split 多少份」是一个典型的两头不讨好的决策：

- **split 太少**：block 数不足以填满 SM，波次利用率低，长 KV 的读取带宽吃不满。
- **split 太多**：每个 split 都要重读一遍 Q、把 float 部分输出写回全局内存，combine kernel 又要把它们全部读回来——**HBM 读写次数随 split 数线性增长**；此外 block 太多还会排队。

所以需要一个「够好就行」的折中：**在效率波形上取一个接近峰值的较小值**。这就是 `num_splits_heuristic` 的设计哲学——不追求最优占用率，而是「达到最优效率 85% 的最小 split 数」。

#### 4.1.2 核心流程

设 block 基数为 \( B \)（= batch × heads × m 块数），split 数为 \( s \)，可驻留 block 槽位数为 \( S \)：

\[
n_{\text{waves}}(s) = \frac{B \cdot s}{S}, \qquad
\mathrm{eff}(s) = \frac{n_{\text{waves}}(s)}{\lceil n_{\text{waves}}(s) \rceil}
\]

直觉：\( \lceil n_{\text{waves}} \rceil \) 是实际要跑的波次数，\( n_{\text{waves}} \) 是「满波次的等效数」，两者之比就是 SM 时间利用率。\( \mathrm{eff} = 1 \) 表示恰好整波，最理想；\( \mathrm{eff} = 0.5 \) 表示第二波只干了一半活。

源码注释里的经典例子（\( B = 48 \)，\( S = 108 \)）：

| \( s \) | \( n_{\text{waves}} \) | 波次数 | \( \mathrm{eff} \) |
|---|---|---|---|
| 2 | 96/108 = 0.89 | 1 | **0.89** |
| 3 | 144/108 = 1.33 | 2 | **0.67** |

2 份 split 反而优于 3 份——多切一刀让第二波只剩 1/3 的活，利用率骤降。

选择规则（两轮扫描）：

```
第一轮：枚举 s = 1..max_splits，记录每个合法 s 的 eff(s)，并记 max_eff
第二轮：从小到大找第一个 eff(s) >= 0.85 * max_eff 的合法 s，返回
```

#### 4.1.3 源码精读

启发式的调用入口在 `mha_fwd_kvcache` 尾部，紧跟 `set_params_fprop`（u3-l2）之后、`run_mha_fwd` 之前：

[decode_api.cpp:512-519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L512-L519)

```cpp
    set_params_splitkv(params, batch_size, num_heads,
                       head_size, seqlen_k, seqlen_q,
                       head_size_rounded, /*dropout*/0.f, num_splits, dprops, opts);

    auto stream = at::cuda::getCurrentCUDAStream().stream();
    run_mha_fwd<num_bits>(params, stream, /*force_split_kernel=*/true);
```

注意两点：传给 `set_params_splitkv` 的 `num_heads` 是 GQA 重排**之后**的头数（`num_heads_k`）；`force_split_kernel=true`（u3-l1 讲过）意味着 decode 永远走 split 路径。

`num_splits` 的值来自 Python 侧。`fwd_kvcache_int` 调用绑定函数时，最后一个位置参数硬编码为 `0`（对应 C++ 签名的默认 `num_splits=0`）：

[bit_decode_interface.py:76-81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L76-L81)

```python
            False,          # Added  (is_causal)
            -1,             # Added  (window_size_left)
            -1,             # Added  (window_size_right)
            0.0,            # Added  (softcap)
            True,           # Added  (is_rotary_interleaved)
            0               # Added  (num_splits —— 0 表示"自动"，触发启发式)
```

即：**当前仓库永远使用自动启发式，Python 层没有暴露覆盖入口**（int2 分支 [bit_decode_interface.py:96-101](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L96-L101) 同样）。

决策结果如何在 GPU 侧被消费？看启动代码：

[flash_fwd_launch_template.h:82-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L82-L104)

```cpp
    const int num_m_block = (params.seqlen_q + Kernel_traits::kBlockM - 1) / Kernel_traits::kBlockM; 
    const int num_splits_ = params.num_splits - 1;
    
    dim3 grid_res(num_m_block, params.b, params.h);
    dim3 grid(num_m_block, num_splits_, params.b * params.h);
    ...
    auto kernel_res = &flash_fwd_residual_kernel<...>;
    kernel_res<<<grid_res, Kernel_traits::kNThreads, smem_size_res, stream>>>(params);
    ...
    auto kernel = &flash_fwd_splitkv_kernel<...>;
    kernel<<<grid, Kernel_traits::kNThreads, smem_size, stream>>>(params);
```

三个关键事实：

1. `params.num_splits` 包含那个 `+1`；**splitkv kernel 的 grid.y 只有 `num_splits - 1`**，即启发式算出来的真实 KV split 数。
2. **residual kernel 是单独一个 grid**（`grid.y = b`），且先于 splitkv kernel 启动（同一条流上串行）。
3. splitkv kernel 内部用 `blockIdx.y` 作为 split 索引，并按下面的公式切分 KV 块区间：

[flash_fwd_kernel.h:614-616](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L614-L616)

```cpp
    const int n_blocks_per_split = ((binfo.seqlen_k_cache + kBlockN - 1) / kBlockN + num_n_splits - 1) / num_n_splits;
    const int n_block_min = n_split_idx * n_blocks_per_split;
    int n_block_max = std::min(cute::ceil_div(binfo.actual_seqlen_k, kBlockN), (n_split_idx + 1) * n_blocks_per_split);
```

每个 split 负责连续的 `[n_split_idx * n_blocks_per_split, ...)` 一段 KV 块——**KV 的每个 token 恰好被一个 split 读取一次**，split 数再多也不会重复读 KV。会随 split 数增长的是 Q 的重复读取和部分输出的写出/读回。

#### 4.1.4 代码实践（源码阅读型）

**目标**：亲手验证「+1 之后，两个 kernel 的 block 总数确实接近填满 SM 槽位」。

**步骤**：

1. 取 A100（108 SM）、batch=1、Llama-3.1-8B（`num_heads_k=8`，GQA 重排后 `seqlen_q=4`，`num_m_block=1`）、`seqlen_k ≈ 16384`（`num_n_blocks = 16384/256 = 64`）。
2. 在 4.2.4 的实践中你会得到该配置下启发式选择 `s = 22`。
3. 计算：splitkv grid = `1 × 22 × (1×8)` = **176 个 block**；residual grid = `1 × 1 × 8` = **8 个 block**；合计 184。
4. 对比槽位数 `S = 108 × 2 = 216`：176/216 ≈ 0.815（正是启发式优化的 eff），加上 residual 后 184/216 ≈ 0.852。

**需要观察的现象**：启发式建模的对象是 `B × s`（即 splitkv 的 176），**并不把 residual kernel 的 8 个 block 计入**——所以真实 SM 占用略高于模型值。这是一个可以接受的近似。

**预期结果**：两者都远大于不切分时的 8 个 block（占用 3.7%）。若无 GPU，以上数字为手推结果，可先用 4.2.4 的程序验证 `s=22` 这一步（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 decode 阶段 `num_m_blocks` 恒等于 1？

**答案**：M 维对应 `seqlen_q`。decode 每步只有 1 个 query token；即使发生 GQA 重排，`seqlen_q` 也只是变成 ngroups（如 4），仍远小于块高 64，`ceildiv(seqlen_q, 64) = 1`。

**练习 2**：residual kernel 的 grid 为什么是 `(num_m_block, b, h)` 而不需要 split 维？

**答案**：FP16 残余区的长度被 `residual_block_size`（u2-l2）限制在一块以内，且单块数据量小，一个 block 就能处理完一个 `(b, h)` 的全部残余 token，无需切分；它的「split 身份」体现在写入累积缓冲时固定占用最后一个槽位（见 4.3.3）。

**练习 3**：split 数从 22 增加到 44，KV cache 的总读取量会翻倍吗？

**答案**：不会。KV 按 `[n_split_idx * n_blocks_per_split, ...)` 划分区间，每个 token 恰好属于一个 split，KV 读取总量与 split 数无关。翻倍的是：Q 的重复读取次数、`out_accum` 的写出量、combine 的读回量。

### 4.2 num_splits_heuristic：枚举、资格与 85% 规则

#### 4.2.1 概念说明

`num_splits_heuristic` 是一个**纯 CPU 函数**：输入 block 基数、SM 槽位数、KV 块数和上限，输出一个整数。它继承自 FlashAttention v2，BitDecoding 原样保留。它要解决的问题是 4.1 描述的占用率与流量的折中，策略概括为一句话：

> 先枚举所有候选 split 数算出效率波形；找到波形峰值；然后从小的 split 数开始，返回第一个效率达到峰值 85% 的候选。

「取最小达标值」而不是「取峰值」正是为了省 HBM 流量——峰值点往往在很深的 split 处，而 85% 的效率用一个浅得多的 split 就能达到。

#### 4.2.2 核心流程

```
num_splits_heuristic(B, S, num_n_blocks, max_splits):
  1. 若 B >= 0.8 * S：直接返回 1          # 已经够满，不切
  2. max_splits = min(max_splits, S, num_n_blocks)
  3. 对 s = 1..max_splits：
       a. 资格判定：ceildiv(n, s) != ceildiv(n, s-1) 才合法
          （否则多切一份并不能减少每份的块数，属于"伪 split"）
       b. 合法者计算 eff(s) = (B*s/S) / ceil(B*s/S)，记录并更新 max_eff
       c. 非法者效率记 0（保持数组索引对齐）
  4. 对 s = 1..max_splits 从小到大：
       返回第一个 eff(s) >= 0.85 * max_eff 的合法 s
  5. 兜底返回 1
```

为什么需要资格判定？看注释里的例子：`num_n_blocks = 64` 时，`ceildiv(64, 11) = 6` 而 `ceildiv(64, 12)` 也等于 6——选 12 份和选 11 份每份块数完全一样，第 12 份是空头支票，只会白白多写一份部分输出。

#### 4.2.3 源码精读

函数头与提前返回。注意注释直接给出了设计动机：

[decode_api.cpp:240-249](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L240-L249)

```cpp
// Find the number of splits that maximizes the occupancy. For example, if we have
// batch * n_heads = 48 and we have 108 SMs, having 2 splits (efficiency = 0.89) is
// better than having 3 splits (efficiency = 0.67). However, we also don't want too many
// splits as that would incur more HBM reads/writes.
// So we find the best efficiency, then find the smallest number of splits that gets 85%
// of the best efficiency.
inline int num_splits_heuristic(int batch_nheads_mblocks, int num_SMs, int num_n_blocks, int max_splits) {
    // If we have enough to almost fill the SMs, then just use 1 split
    if (batch_nheads_mblocks >= 0.8f * num_SMs) { return 1; }
    max_splits = std::min({max_splits, num_SMs, num_n_blocks});
```

资格判定用两个 lambda 实现，`ceildiv` 是整数上取整除法：

[decode_api.cpp:253-260](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L253-L260)

```cpp
    auto ceildiv = [](int a, int b) { return (a + b - 1) / b; };
    // Some splits are not eligible. For example, if we have 64 blocks and choose 11 splits,
    // we'll have 6 * 10 + 4 blocks. If we choose 12 splits, we'll have 6 * 11 + (-2) blocks
    // (i.e. it's 11 splits anyway).
    // So we check if the number of blocks per split is the same as the previous num_splits.
    auto is_split_eligible = [&ceildiv, &num_n_blocks](int num_splits) {
        return num_splits == 1 || ceildiv(num_n_blocks, num_splits) != ceildiv(num_n_blocks, num_splits - 1);
    };
```

第一轮扫描：枚举效率。核心就是 4.1.2 的公式，逐行对应：

[decode_api.cpp:261-271](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L261-L271)

```cpp
    for (int num_splits = 1; num_splits <= max_splits; num_splits++) {
        if (!is_split_eligible(num_splits)) {
            efficiency.push_back(0.f);
        } else {
            float n_waves = float(batch_nheads_mblocks * num_splits) / num_SMs;
            float eff = n_waves / ceil(n_waves);
            // printf("num_splits = %d, eff = %f\n", num_splits, eff);
            if (eff > max_efficiency) { max_efficiency = eff; }
            efficiency.push_back(eff);
        }
    }
```

（被注释掉的 `printf` 是现成的调试开关，见 4.2.4。）

第二轮扫描：85% 规则，取**最小**达标值：

[decode_api.cpp:272-280](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L272-L280)

```cpp
    for (int num_splits = 1; num_splits <= max_splits; num_splits++) {
        if (!is_split_eligible(num_splits)) { continue; }
        if (efficiency[num_splits - 1] >= 0.85 * max_efficiency) {
            return num_splits;
        }
    }
    return 1;
```

**手推一个真实配置**（A100，batch=1，`num_heads_k=8`，`seqlen_k=16384`）：
\( B = 8 \)，\( S = 108 \times 2 = 216 \)，\( n = 64 \)，故 \( n_{\text{waves}}(s) = 8s/216 = s/27 \)。

- 合法 split 点（`ceildiv(64, s)` 发生变化的 s）：{1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 13, 16, 22, 32, 64}。
- 各点效率（\( s \le 27 \) 时 \( \mathrm{eff} = s/27 \)）：s=16 → 0.593；**s=22 → 0.815**；s=32 → (32/27)/2 = 0.593；s=64 → (64/27)/3 = 0.790。
- `max_eff = 0.815`（在 s=22 取得）；阈值 `0.85 × 0.815 = 0.693`。
- 从小到大找第一个 ≥ 0.693 的合法点：s=22。**返回 22，`set_params_splitkv` 再 +1 得 23**。

注意 s=27（eff 本可达 1.0）是**非法**的：`ceildiv(64,27) = 3 = ceildiv(64,26)`，被资格判定过滤掉了——波形峰值只能落在合法点上。

#### 4.2.4 代码实践（手推 + 调试开关）

**目标**：验证上面手推的 `s=22`，并观察效率波形的锯齿形态。

**步骤**（需要能编译本项目的环境；无 GPU 时改用第 5 节的独立程序）：

1. 打开 [decode_api.cpp:267](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L267) 和 [decode_api.cpp:275](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L275)，把两行 `printf` 取消注释（这是仓库自带的调试开关，不算改逻辑）。
2. 重新编译安装（回顾 u1-l2 的 `bash install.sh`）。
3. 运行 `evaluation/test.py`（回顾 u1-l4），在 stderr 中找 `num_splits = ...` 输出；对照 [decode_api.cpp:300](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L300)（启发式返回值，打印在 +1 之前）与 [decode_api.cpp:304](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L304)（+1 之后）两处打印。
4. 把测试张量的 `seqlen_k` 从 1000 逐步调大（如 4096、16384、65536），观察被选中的 split 数如何随 `num_n_blocks` 变化。

**需要观察的现象**：第一轮扫描打印出的 eff 值整体呈锯齿上升——每跨过一个波次边界（\( Bs/S \) 越过整数）效率掉回去再爬升；非法 split 点被打印为 0。

**预期结果**：A100、batch=1、`num_heads_k=8`、`seqlen_k≈16384` 时应看到 `num_splits chosen = 22`、最终 `num_splits = 23`（待本地验证；本环境无 GPU，以上为手推值，可用第 5 节独立程序先行核验）。

#### 4.2.5 小练习与答案

**练习 1**：为什么非法 split 的效率要 `push_back(0.f)`，而不是直接跳过不放入数组？

**答案**：第二轮扫描用 `efficiency[num_splits - 1]` 按下标访问，数组下标必须与 `num_splits` 严格对齐。跳过会导致错位，把 s 的效率当成 s-1 的来比。放 0 再配合第二轮的 `continue`，既保住对齐又排除非法点。

**练习 2**：提前返回用的系数是 0.8，即 `B >= 0.8*S` 就不切分。此时单波利用率最多只有 80%，为什么不继续切到更满？

**答案**：切分的收益是利用率，代价是 Q 重复读取 + 部分输出写出/combine 读回的额外 HBM 流量，还有多一次 kernel 调度。80% 单波已经「够本」，再切只会让流量代价压过占用收益。这和 85% 规则一样，都是「够好就行」的工程折中。

**练习 3**：把 `max_splits` 上限从 128 改成 8，对长上下文（如 `num_n_blocks=64`）会有什么影响？

**答案**：枚举范围缩小到 s ≤ 8，波形峰值只能落在 {1,...,8} 的合法点上（本例 max_eff ≈ 0.296），最终选出的 split 数变小、占用率下降。这正说明 `set_params_splitkv` 传入的 128 是「最深允许的 split」上限。

### 4.3 set_params_splitkv：+1 槽位与累积缓冲分配

#### 4.3.1 概念说明

`set_params_splitkv` 是启发式与缓冲分配的「装配间」。它做四件事：

1. 用硬编码的 `block_n = 256` 把 KV 长度换算成 KV 块数 `num_n_blocks`（启发式的输入之一）；
2. 在 `num_splits < 1`（即 Python 传 0）时调用启发式；
3. 把结果 **+1**，预留 residual kernel 的槽位；
4. 当 `num_splits > 1` 时分配两块 **float32** 中间缓冲并把指针写进 `params`。

#### 4.3.2 核心流程

```
set_params_splitkv(params, b, h, d, max_seqlen_k, max_seqlen_q, ...):
  block_n     = 256                              # 硬编码，TODO 注释表明原本想按 head_dim 自适应
  num_n_blocks = ceildiv(max_seqlen_k, 256)
  num_m_blocks = ceildiv(max_seqlen_q, 64)
  params.num_splits = num_splits                 # 来自 Python，恒为 0
  if num_splits < 1:
      params.num_splits = num_splits_heuristic(
          b * h * num_m_blocks,                   # block 基数 B
          multiProcessorCount * 2,                # 槽位数 S（128 线程/block → 每 SM 2 个 CTA）
          num_n_blocks, 128)                      # KV 块数与最深 split 上限
  params.num_splits += 1                          # 给 residual kernel 留一个槽位
  if params.num_splits > 1:
      softmax_lse_accum = empty({num_splits, b, h, max_seqlen_q}, float32)
      out_accum          = empty({num_splits, b, h, max_seqlen_q, d_rounded}, float32)
      params.softmax_lseaccum_ptr / params.oaccum_ptr = ...
  TORCH_CHECK(params.num_splits <= 128)
```

关于「为什么 +1」——证据链在 kernel 侧：

[flash_fwd_kernel.h:1678](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1678)

```cpp
    const int n_split_idx = params.num_splits - 1;   // residual kernel：固定写最后一个槽位
```

[flash_fwd_kernel.h:1692-1694](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1692-L1694)

```cpp
    const int n_split_idx  = Split ? blockIdx.y : 0; // splitkv kernel：0 .. num_splits-2
```

也就是说：**累积缓冲共有 `num_splits` 个槽位，前 `num_splits-1` 个（grid.y）由 splitkv kernel 写，最后 1 个由 residual kernel 写**。两块缓冲的寻址公式（以 splitkv kernel 为例）：

[flash_fwd_kernel.h:624-630](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L624-L630)

```cpp
        const index_t row_offset_oaccum = (((n_split_idx * params.b + bidb) * params.h + bidh) * params.seqlen_q
        ...
        Tensor gOaccum = make_tensor(make_gmem_ptr(reinterpret_cast<ElementO *>(Split ? params.oaccum_ptr : params.o_ptr) + ...
```

即布局完全对应 `{num_splits, b, h, seqlen_q, ...}` 的分配形状，`n_split_idx` 是最外维。

#### 4.3.3 源码精读

函数头与块数计算。注意 `block_n` 的自适应版本被注释成 TODO，**当前硬编码 256**，与 kernel 侧 `run_mha_fwd_splitkv_dispatch` 的 `kBlockN = 256`（[flash_fwd_launch_template.h:130-137](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L130-L137)，`kBlockM=16` 同样固定）保持一致：

[decode_api.cpp:282-295](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L282-L295)

```cpp
void set_params_splitkv(Flash_fwd_params &params, const int batch_size,
    const int num_heads, const int head_size, const int max_seqlen_k, const int max_seqlen_q,
    const int head_size_rounded, const float p_dropout,
    const int num_splits, cudaDeviceProp *dprops, struct c10::TensorOptions opts) {

    // This needs to match with run_mha_fwd_splitkv_dispatch
    // TODO
    // const int block_n = head_size <= 64 ? 256 : (head_size <= 128 ? 128 : 64);
    const int block_n = 256;
    const int num_n_blocks = (max_seqlen_k + block_n - 1) / block_n;
    // Technically kBlockM = 64 only for the splitKV kernels, not the standard kernel.
    // In any case we don't expect seqlen_q to be larger than 64 for inference.
    const int num_m_blocks = (max_seqlen_q + 64 - 1) / 64;
    params.num_splits = num_splits;
```

（批判性阅读：注释说 splitKV 的 `kBlockM = 64`，但 dispatch 实际固定 `kBlockM = 16`；host 侧按 64 估算 `num_m_blocks` 只是偏保守。decode 时 `seqlen_q ≤ 64`，两种算法结果都是 1，所以无实际影响——这是典型的「注释与代码漂移」现场。）

启发式调用与 +1。注意 `num_SMs` 实参传的是 `multiProcessorCount * 2`，注释解释了 ×2 的原因（128 线程/block，每个 SM 驻留 2 个 CTA），所以启发式里的「num_SMs」其实是**block 槽位数**：

[decode_api.cpp:296-304](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L296-L304)

```cpp
    if (p_dropout == 0.0f) {  // SplitKV is not implemented for dropout
        if (num_splits < 1) {
            // We multiply number of SMs by 2 to hard-code the fact that we're using 128 threads per block.
            params.num_splits = num_splits_heuristic(batch_size * num_heads * num_m_blocks, dprops->multiProcessorCount * 2, num_n_blocks, 128);
        }
        params.num_splits += 1;  // We need to add 1 for residual kernel.
```

缓冲分配。两块都是 `torch::empty` 的**函数局部张量**，只把裸指针存进 `params`：

[decode_api.cpp:305-311](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L305-L311)

```cpp
        if (params.num_splits > 1) {
            at::Tensor softmax_lse_accum = torch::empty({params.num_splits, batch_size, num_heads, max_seqlen_q}, opts.dtype(at::kFloat));
            at::Tensor out_accum = torch::empty({params.num_splits, batch_size, num_heads, max_seqlen_q, head_size_rounded}, opts.dtype(at::kFloat));
            params.softmax_lseaccum_ptr = softmax_lse_accum.data_ptr();
            params.oaccum_ptr = out_accum.data_ptr();
        }
        TORCH_CHECK(params.num_splits <= 128, "num_splits > 128 not supported");
```

这两个指针在参数结构体里的位置（u3-l2 的延续）：

[flash.h:104-118](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L104-L118)

```cpp
    // The O matrix (output).
    void * __restrict__ o_ptr;
    void * __restrict__ oaccum_ptr;      // split 模式下的 float32 部分输出
    ...
    // The pointer to the softmax sum.
    void * __restrict__ softmax_lse_ptr;
    void * __restrict__ softmax_lseaccum_ptr;   // split 模式下每个 split 的 LSE
```

[flash.h:191](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/flash.h#L191)

```cpp
    int num_splits;  // For split-KV version
```

**分配时机的三个要点**：

1. **每步都分配**。`fwd_kvcache_int` 每轮 decode、每层都会走到这里，`torch::empty` 两次。开销由 PyTorch caching allocator 摊销（同形状的分配会命中缓存池，不真正调 cudaMalloc）。
2. **局部张量的生命周期陷阱**。两个 `at::Tensor` 在函数返回时析构，`params` 里只剩裸指针。之所以安全，是因为 caching allocator 的流序复用语义 + `set_params_splitkv` 返回到 kernel 启动（[decode_api.cpp:519](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L519)）之间没有其他同流分配会抢走这块内存。这是从 FlashAttention 继承来的写法，二次开发时若在这段区间插入新的张量分配要格外小心。
3. **`num_splits ≤ 128` 的真正原因**在 combine kernel：它按 `Log_max_splits` 模板参数选实现，阶梯正好到 128：

[flash_fwd_launch_template.h:106-127](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L127)

```cpp
    if (params.num_splits > 1) {
        constexpr static int kBlockM = ...;
        dim3 grid_combine((params.b * params.h * params.seqlen_q + kBlockM - 1) / kBlockM);
        EVENK_SWITCH(is_even_K, IsEvenKConst, [&] {
            if (params.num_splits <= 2) {
                flash_fwd_splitkv_combine_kernel<Kernel_traits, kBlockM, 1, IsEvenKConst><<<...>>>(params);
            } else if (params.num_splits <= 4) {
                flash_fwd_splitkv_combine_kernel<Kernel_traits, kBlockM, 2, IsEvenKConst><<<...>>>(params);
            } ...
            } else if (params.num_splits <= 128) {
                flash_fwd_splitkv_combine_kernel<Kernel_traits, kBlockM, 7, IsEvenKConst><<<...>>>(params);
            }
```

`Log_max_splits` 决定 combine kernel 用多大的共享内存数组缓存各 split 的 LSE（\( 2^{\log} \) 个），`num_splits ≤ 2^k` 选中对应的 k。还有一个值得注意的推论：**BitDecoding 里 `num_splits` 至少是 2**（启发式最小返回 1，再 +1），所以 `params.num_splits > 1` 恒成立——**累积缓冲每次 decode 都会分配，combine kernel 每次都会运行**。

#### 4.3.4 代码实践（缓冲体积估算 + 观察分配时机）

**目标**：算出一次 decode 调用分配的中间缓冲字节数，理解「为什么用 float32」与「为什么用 `head_size_rounded`」。

**步骤**：

1. 取 4.1.4 的配置：A100、b=1、`h=8`、`seqlen_q=4`（GQA 重排后）、`d=128`、`num_splits=23`（22+1）。
2. 套公式计算：
   - `out_accum`：\( 23 \times 1 \times 8 \times 4 \times 128 \) 个 float = 94,208 × 4 B ≈ **368 KB**
   - `softmax_lse_accum`：\( 23 \times 1 \times 8 \times 4 \) 个 float = 736 × 4 B ≈ **3 KB**
3. 思考并回答：如果 32 层 × 每层调用一次，caching allocator 池里最多常驻多大？（同形状复用，约 32 × 371 KB ≈ 11.6 MB 量级；由于各层 `num_splits` 相同、形状一致，实际可复用同一块。）
4. （可选，需 GPU）在 `test.py` 外面套一层 `torch.cuda.memory_allocated()` 前后差值打印，或用 `nsys profile python evaluation/test.py` 观察每轮 decode 中 `cudaLaunchKernel` 前后的 `cudaMalloc`/缓存命中情况。

**需要观察的现象**：首轮 decode 之后显存不再阶梯式增长——caching allocator 命中池内同形状块。

**预期结果**：与手算的 371 KB/层 量级吻合；`num_splits` 随 `seqlen_k` 越过 256 的倍数而变化时（KV 逐渐变长、`num_n_blocks` 增大），分配形状也会随之改变。无 GPU 时第 1-3 步为纸面推导（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `out_accum` 用 float32 而不是和输出一样的 FP16？

**答案**：各 split 的部分输出是「未归一化的加权和」，量级差异大（依赖各自段内的 max）。合并时要按 LSE 权重精确缩放求和，FP16 的 11 位尾数会在深 split、长上下文时累积舍入误差。float32 是数值安全的折中——这也是 split 带来的额外流量代价的一部分（每个元素 4 字节）。

**练习 2**：`out_accum` 最后一维为什么是 `head_size_rounded`（128 对齐到 32 的倍数）而不是 `head_size`？

**答案**：kernel 写出时按对齐后的 tile 形状（MMA 累加器布局）拷贝到全局内存，d=128 时两者恰好相等；但对 d 不整除 32 的头维（本项目当前不支持，见 u7-l4 的硬编码讨论），写出按 `d_rounded` 走，缓冲必须与之匹配。

**练习 3**：如果 Python 侧把 `num_splits` 传成 4（而非 0），会发生什么？

**答案**：`num_splits < 1` 不成立，启发式被跳过，`params.num_splits = 4 + 1 = 5`，同样分配 5 槽位缓冲并走 combine。这提供了一条「手动固定 split 数」的实验通道——但当前 `bit_decode_interface.py` 把该参数硬编码为 0（[bit_decode_interface.py:81](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/bit_decode/bit_decode_interface.py#L81)），要利用它需要改这一行并在 C++ 侧确认参数位次。

## 5. 综合实践：独立复刻 num_splits_heuristic 并做阈值扫描

这是本讲的主实践：**不依赖 PyTorch/CUDA**，用一个纯 C++ 程序复刻启发式，扫描「batch×heads」对被选中 split 数的影响，再改阈值观察策略变化。（本实践为示例代码，非仓库原有文件。）

**实践目标**：

1. 验证 4.2.3 手推的 `(B=8, S=216, n=64) → 22`。
2. 得到 `num_splits(B)` 曲线，理解它为何是「锯齿下降、到 0.8×S 处跳变为 1」。
3. 分析阈值 0.5 / 0.85 / 0.95 如何在「占用率」与「HBM 流量」之间移动平衡点。

**操作步骤**：

第 1 步，把下面的程序保存为 `split_heuristic_demo.cpp`（示例代码，逻辑逐行对照 [decode_api.cpp:246-280](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L246-L280)，唯一改动是把硬编码的 0.85 提升为命令行参数）：

```cpp
// split_heuristic_demo.cpp （示例代码）
// 编译：g++ -O2 -std=c++17 split_heuristic_demo.cpp -o split_heuristic_demo
#include <cstdio>
#include <cstdlib>
#include <vector>
#include <algorithm>

int num_splits_heuristic_replica(int batch_nheads_mblocks, int num_SMs,
                                 int num_n_blocks, int max_splits,
                                 float threshold) {   // 原代码此处硬编码 0.85
    if (batch_nheads_mblocks >= 0.8f * num_SMs) { return 1; }
    max_splits = std::min({max_splits, num_SMs, num_n_blocks});
    float max_efficiency = 0.f;
    std::vector<float> efficiency;
    efficiency.reserve(max_splits);
    auto ceildiv = [](int a, int b) { return (a + b - 1) / b; };
    auto is_split_eligible = [&ceildiv, &num_n_blocks](int num_splits) {
        return num_splits == 1 || ceildiv(num_n_blocks, num_splits) != ceildiv(num_n_blocks, num_splits - 1);
    };
    for (int num_splits = 1; num_splits <= max_splits; num_splits++) {
        if (!is_split_eligible(num_splits)) {
            efficiency.push_back(0.f);
        } else {
            float n_waves = float(batch_nheads_mblocks * num_splits) / num_SMs;
            float eff = n_waves / ceil(n_waves);
            if (eff > max_efficiency) { max_efficiency = eff; }
            efficiency.push_back(eff);
        }
    }
    for (int num_splits = 1; num_splits <= max_splits; num_splits++) {
        if (!is_split_eligible(num_splits)) { continue; }
        if (efficiency[num_splits - 1] >= threshold * max_efficiency) {
            return num_splits;
        }
    }
    return 1;
}

int main(int argc, char **argv) {
    // A100：108 个 SM × 2（128 线程/block，两个 CTA/SM），num_n_blocks = 64（seqlen_k ≈ 16K）
    const int num_SMs = 108 * 2;
    const int num_n_blocks = 64;
    float threshold = (argc > 1) ? std::atof(argv[1]) : 0.85f;
    std::printf("batch_nheads_mblocks,num_splits,num_splits_with_residual\n");
    for (int b = 8; b <= 512; b += 8) {
        int s = num_splits_heuristic_replica(b, num_SMs, num_n_blocks, 128, threshold);
        std::printf("%d,%d,%d\n", b, s, s + 1);
    }
    return 0;
}
```

第 2 步，编译并生成三组数据：

```bash
g++ -O2 -std=c++17 split_heuristic_demo.cpp -o split_heuristic_demo
./split_heuristic_demo 0.50 > s50.csv
./split_heuristic_demo 0.85 > s85.csv
./split_heuristic_demo 0.95 > s95.csv
head -5 s85.csv
```

第 3 步，画图（示例代码）：

```python
import matplotlib.pyplot as plt
import csv

fig, ax = plt.subplots(figsize=(7, 4))
for t, color in [(0.50, "tab:blue"), (0.85, "tab:orange"), (0.95, "tab:green")]:
    with open(f"s{int(t*100)}.csv") as f:
        rows = list(csv.reader(f))[1:]
    xs = [int(r[0]) for r in rows]
    ys = [int(r[1]) for r in rows]
    ax.plot(xs, ys, marker=".", color=color, label=f"threshold={t}")
ax.axvline(0.8 * 216, ls="--", c="gray", label="0.8 * S = 172.8 (early-exit)")
ax.set_xlabel("batch * nheads * mblocks")
ax.set_ylabel("chosen num_splits (before +1)")
ax.legend(); ax.grid(alpha=0.3)
fig.savefig("num_splits_curve.png", dpi=150)
```

**需要观察的现象**：

1. `s85.csv` 第一行（B=8）应为 `22`——验证 4.2.3 的手推。
2. 曲线随 B 增大整体下降（B 越大越不需要切），且因资格判定呈台阶/锯齿状；B ≥ 176（第一个 ≥ 172.8 的 8 倍数）后跳变为 1（提前返回生效）。
3. 阈值 0.5 的曲线整体**更低**（更早满足「凑合」标准，选更浅的 split）；0.95 整体**更高**、更贴近各段波形峰值点。

**预期结果与 HBM 分析**：

- split 数每 +1，每个 `(b, h, q)` 多付出：一份 float 部分输出写出（\( d_{\text{rounded}} \times 4 \) B）+ combine 读回（同量）+ 一份 LSE（8 B）+ 一遍 Q 重读（\( d \times 2 \) B）。粗略地，**额外 HBM 字节 ≈ s × b × h × seqlen_q × (8·d_rounded + 12)**。
- 以 4.1.4 配置（b=1,h=8,seqlen_q=4,d=128）估算：阈值 0.85 选 s=22 → 每层约 22×8×4×(1024+12) ≈ 728 KB 的额外流量；若阈值 0.95 把 s 推到更深（如 64 附近），流量接近 ×3。对照收益：占用率从 0.815 只再涨零点几个百分点——**这就是 0.85 而不是 0.95 的道理，也是注释"don't want too many splits as that would incur more HBM reads/writes"的定量含义**。
- 以上数值为公式推算，具体曲线以本地运行为准（待本地验证）。

**思考题（选做）**：把 `num_n_blocks` 从 64 改成 256（seqlen_k ≈ 64K）重新扫描，B=8 时选中的 split 数会显著变大吗？先预测再运行，解释原因（提示：合法点变密、波形峰值位置移动）。

## 6. 本讲小结

- decoding 的并行度饥饿：block 数 ≈ batch × heads_k，远小于 SM 数；split-KV 用 KV 序列维切分换取占用率，`num_splits_heuristic` 用波次效率模型 \( \mathrm{eff} = n_{\text{waves}}/\lceil n_{\text{waves}} \rceil \) 在枚举的合法 split 点里选「达到峰值效率 85% 的最小值」。
- 资格判定 `is_split_eligible` 过滤掉不能真正减少每份块数的「伪 split」（`ceildiv(n,s)` 未变化的 s），非法点效率记 0 以保持数组下标对齐。
- `set_params_splitkv` 先按硬编码 `block_n = 256` 算 `num_n_blocks`，把 `multiProcessorCount × 2` 当作 block 槽位数传入启发式（128 线程/block、每 SM 2 CTA），随后 **`+1` 给 residual kernel**——kernel 侧证据是 residual kernel 固定写第 `num_splits-1` 号累积槽位，splitkv kernel 的 grid.y 为 `num_splits-1`。
- 中间缓冲 `softmax_lse_accum {num_splits,b,h,seqlen_q}` 与 `out_accum {num_splits,b,h,seqlen_q,d_rounded}` 均为 float32 局部张量，每次 decode、每层都会 `torch::empty`（靠 caching allocator 摊销）；`num_splits ≤ 128` 的约束来自 combine kernel 的 `Log_max_splits` 模板阶梯。
- Python 侧恒传 `num_splits=0` 走自动启发式；由于 +1，`num_splits ≥ 2` 恒成立，累积缓冲与 combine kernel 在每次 decode 中必然参与。
- split 数只放大 Q 重读与部分输出流量，KV 读取总量不变（每个 token 恰属一个 split）——评估 split 代价时不要算错这笔账。

## 7. 下一步学习建议

按大纲顺序，下一讲进入第四单元：**u4-l1（QPack kernel 启动路径）**，看 prefill 侧的量化打包 kernel 如何以 `grid = (num_n_block, b, h)` 启动——与本讲 grid.y 放 split 数的设计对照着读，会看到「同一个维度、两种用途」的调度差异。

如果你想趁热打铁继续追 `num_splits` 的消费端，可以先跳读：

- **u5-l2（split-KV 主循环）**：`compute_attn_1rowblock_splitkv` 如何用 `n_block_min/n_block_max` 划分 KV 段、把部分输出写进本讲的累积缓冲。
- **u5-l5（combine kernel）**：`combine_attn_seqk_parallel` 如何用 LSE 加权合并这 `num_splits` 份部分结果，以及 `Log_max_splits` 阶梯背后的共享内存归约细节。

回到本讲的两个遗留点也值得记下：`block_n` 与 `kBlockM` 的 host/device 不一致注释（u7-l4 架构评审的素材），以及 `num_splits` 手动覆盖入口（`bit_decode_interface.py:81` 硬编码 0，做消融实验时可以启用）。
