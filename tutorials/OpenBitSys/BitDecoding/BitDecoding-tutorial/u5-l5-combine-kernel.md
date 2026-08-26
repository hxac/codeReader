# combine kernel：用 LSE 加权合并多个 split 的部分结果

## 1. 本讲目标

学完本讲，你应该能够：

1. **手推**基于 log-sum-exp（LSE）的两 split 合并公式：给定 `(O1, lse1)` 与 `(O2, lse2)`，写出合并后的 `O` 与 `lse`，并证明它与「不切分直接算」严格等价。
2. 讲清 `flash_fwd_splitkv_combine_kernel` 的启动逻辑：`Log_max_splits` 为什么取 2 的幂阶梯、`grid_combine` 怎么算、combine 用的 `kBlockM` 为什么与解码主 kernel 不同。
3. 逐段读懂 `combine_attn_seqk_parallel`：LSE 从 `softmax_lse_accum` 缓冲装载进共享内存、转置到寄存器、用 warp 蝴蝶 shuffle 归约出 `lse_logsum`，再以 `exp(lse_i - lse_logsum)` 为权重把 `out_accum` 中各 split 的 float 部分输出累加成最终结果。
4. 说清楚代码里三处「等价条件判断」（`-INFINITY` 填充、`lse_max == -inf` 特判、`lse_sum == 0` 特判）分别防住了哪类数值事故。

本讲是第五单元的收官：u5-l2 讲了 splitkv 主 kernel 怎么按 split 写出部分结果，u5-l4 讲了 residual kernel 怎么占用最后一个累积槽位，本讲讲这些部分结果**最后如何被无损地拼回一个正确的注意力输出**。

## 2. 前置知识

### 2.1 什么是 LSE（log-sum-exp）

对一个数集 \(\{a_1,\dots,a_n\}\)，定义：

\[
\mathrm{LSE}(a_1,\dots,a_n) = \log\sum_{i=1}^{n} e^{a_i}
\]

它就是「先指数、再求和、后取对数」。softmax 的分母正是 \(\sum_i e^{a_i}\)，所以 **LSE 就是 softmax 归一化因子的对数**。attention 里每个 query 行都会产生一个 LSE（本讲记作 `lse`），它把「这一行所有分数的指数和」压缩成一个数，是跨 kernel 传递 softmax 状态的最紧凑形式——combine kernel 的全部数学都围绕它展开。

LSE 的关键性质是**数值稳定**：\(\mathrm{LSE} = m + \log\sum_i e^{a_i - m}\)（\(m=\max a_i\)），减去最大值后再指数，避免 \(e^{a_i}\) 直接溢出。你在下面会看到这条性质在代码里反复出现。

### 2.2 承接前几讲的三个事实

| 来自 | 事实 | 本讲怎么用 |
|---|---|---|
| u3-l3 | `set_params_splitkv` 把启发式结果 **+1** 再分配 `softmax_lse_accum`（形状 `{num_splits, b, h, seqlen_q}`）与 `out_accum`（形状 `{num_splits, b, h, seqlen_q, d_rounded}`）两个 float 缓冲 | combine 的循环遍历**全部 `num_splits` 个槽位**，多出来的那个槽位属于 residual kernel |
| u5-l2 | splitkv 主 kernel 在 epilogue 用 `normalize_softmax_lse` 把 `acc_o` **除以本 split 内的 row_sum** 后写入 `gOaccum`，同时把 `lse` 写入 `gLSEaccum`；空 split 早退时写 `0` 与 `-INFINITY` | 这正是 combine 读到的 `(O_i, lse_i)` 二元组的来源 |
| u5-l4 | residual kernel 以 `n_split_idx = params.num_splits - 1` 写入**最后一个**累积槽位 | combine 无需区分槽位来自谁：统一按 LSE 权重求和 |

### 2.3 符号约定

设解码阶段某个 query 行的缩放后分数为 \(s_j = \mathrm{scale}\cdot(q\cdot k_j)\)，KV 序列被切成若干子集 \(S_i\)（第 \(i\) 个 split 负责的 token）。对第 \(i\) 个 split：

- \(m_i = \max_{j\in S_i} s_j\)（split 内最大分数）
- \(l_i = \sum_{j\in S_i} e^{s_j - m_i}\)（split 内指数和，已减最大值）
- \(O_i\)：该 split 写入 `out_accum` 的输出，**已经除以 \(l_i\)**（归一化过）
- \(\mathrm{lse}_i = m_i + \log l_i\)：该 split 写入 `softmax_lse_accum` 的 LSE

> 代码主循环内部用 exp2 加速（把 \(\mathrm{scale}\cdot\log_2 e\) 折进缩放、用硬件 `exp2` 指令，见 u5-l2），但写入缓冲的 LSE 统一是**自然底数**：`softmax.h` 的 epilogue 用 `__logf(sum)`，本讲 kernel 全程用 `expf/logf`，两边底数匹配。本文推导一律用自然底数。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `csrc/bit_decode/src/flash_fwd_kernel.h` L1699–L1882 | `combine_attn_seqk_parallel`：本讲主角，LSE 归并与加权累加的全部实现 |
| `csrc/bit_decode/src/flash_fwd_launch_template.h` L66–L69、L106–L127 | `flash_fwd_splitkv_combine_kernel` 定义与 `Log_max_splits` 启动阶梯 |
| `csrc/bit_decode/src/flash_fwd_kernel.h` L478–L554、L618–L656、L1193–L1251 | 生产者侧：residual/splitkv kernel 如何写 `gOaccum`/`gLSEaccum`，空 split 早退 |
| `csrc/bit_decode/src/include/softmax.h` L292–L309 | `normalize_softmax_lse`：`lse_i` 的产生地，含 `Split → -INFINITY` 特判 |
| `csrc/bit_decode/src/include/utils.h` L103–L153 | `MaxOp`/`SumOp`/`Allreduce<W>`：warp 蝴蝶 shuffle 归约原语 |
| `csrc/bit_decode/decode_api.cpp` L282–L313 | `set_params_splitkv`：`num_splits` 的 +1 协议与两个 float 缓冲的分配 |

---

## 4. 核心概念与源码讲解

### 4.1 合并的数学：两 split 的 LSE 加权公式

#### 4.1.1 概念说明

u5-l2 里，KV 序列被切成 `num_splits - 1` 份由 splitkv kernel 并行处理，residual kernel 单独处理 FP16 残余区（第 `num_splits - 1` 个槽位）。每个 kernel 只看到**自己那份 KV**，各自算出一个「局部 softmax 输出」和一个「局部 LSE」，写进 `out_accum` / `softmax_lse_accum`。

问题来了：softmax 的分母是**全序列**的指数和，各 split 各自除以自己的局部和 \(l_i\)，直接把输出相加显然不对。combine kernel 要做的就是找一个权重 \(w_i\)，使得

\[
O = \sum_i w_i O_i
\]

恰好等于不切分时的全局输出。这个权重必须同时「抵消」每个 split 各自做过的归一化，并把全局归一化补回来。

#### 4.1.2 核心流程：推导

**第一步：全局量。** 全局最大值与全局指数和为

\[
m = \max_i m_i, \qquad l = \sum_i e^{m_i - m}\, l_i
\]

**第二步：拆解全局输出。** 全局（未归一化）分子可以按 split 拆开：

\[
\sum_{j} e^{s_j - m}\, v_j
= \sum_i e^{m_i - m} \underbrace{\sum_{j\in S_i} e^{s_j - m_i}\, v_j}_{=\,l_i O_i}
= \sum_i e^{m_i - m}\, l_i\, O_i
\]

最后一步用了 \(O_i\) 的定义（\(O_i\) 是「除以 \(l_i\)」之后的结果，所以乘回 \(l_i\) 就还原成未归一化分子）。

**第三步：除以全局分母。**

\[
O = \frac{1}{l}\sum_i e^{m_i - m} l_i O_i = \sum_i \frac{e^{m_i - m} l_i}{\sum_k e^{m_k - m} l_k}\, O_i
\]

**第四步：写成 LSE 形式。** 注意权重只依赖比值，分子分母同除 \(e^m\)：

\[
w_i = \frac{e^{m_i} l_i}{\sum_k e^{m_k} l_k} = \frac{e^{\mathrm{lse}_i}}{\sum_k e^{\mathrm{lse}_k}}
\]

再对分母取对数，定义全局 LSE：

\[
L = \mathrm{LSE}(\mathrm{lse}_1, \mathrm{lse}_2) = \log\big(e^{\mathrm{lse}_1} + e^{\mathrm{lse}_2}\big)
\]

于是得到本讲的核心公式（两 split；多 split 是自然推广）：

\[
\boxed{\;w_i = e^{\mathrm{lse}_i - L}, \qquad O = e^{\mathrm{lse}_1 - L}\,O_1 + e^{\mathrm{lse}_2 - L}\,O_2, \qquad L = m + \log\!\big(e^{\mathrm{lse}_1-m} + e^{\mathrm{lse}_2-m}\big),\; m = \max(\mathrm{lse}_1,\mathrm{lse}_2)\;}
\]

三个直接推论，后面源码会一一对应：

1. **空 split 自动出局**：若 split 2 没有任何 token，其 `lse₂ = -INFINITY`，则 \(e^{-\infty - L} = 0\)，权重为零——不需要任何 `if`。
2. **\(L\) 的计算就是 LSE 减最大值**：\(L = m + \log\sum_i e^{\mathrm{lse}_i - m}\)，与 2.1 的稳定形式相同。
3. **合并满足结合律**：所以任意多个 split 可以两两合并，也可以一次全并——combine kernel 选择「一次全并」。

#### 4.1.3 源码精读：生产者写了什么（被合并数据的三处来源）

在看合并者之前，先确认 `(O_i, lse_i)` 到底长什么样。三个写入者共享同一套偏移公式。

**（a）residual kernel 的 epilogue（槽位 `num_splits-1`）。** `compute_attn_residualkv` 把自己的 split 索引硬编码为最后一个槽位：

[flash_fwd_kernel.h:1672-1680](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1672-L1680)——`bidb/bidh` 来自 `blockIdx.y/z`，`n_split_idx = params.num_splits - 1`。这就是 u3-l3 里 `num_splits += 1` 那一行的另一半协议：**+1 分配出来的槽位由 residual kernel 独占**。

epilogue 中先算 `lse` 再写缓冲：

[flash_fwd_kernel.h:478-481](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L478-L481)——调用 `normalize_softmax_lse</*Split=*/true>`，随后构造 `gOaccum`/`gLSEaccum` 张量视图。

[flash_fwd_kernel.h:505-515](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L505-L515)——累积缓冲的偏移公式：

```cpp
const index_t row_offset_oaccum = (((n_split_idx * params.b + bidb) * params.h + bidh) * params.seqlen_q
                                     + m_block * kBlockM) * params.d_rounded;
const index_t row_offset_lseaccum = ... ((n_split_idx * params.b + bidb) * params.h + bidh) * params.seqlen_q ...;
Tensor gOaccum = make_tensor(..., Split ? params.oaccum_ptr : params.o_ptr ..., ...);
Tensor gLSEaccum = make_tensor(..., Split ? params.softmax_lseaccum_ptr : params.softmax_lse_ptr ...);
```

注意第一维（split 维）的步长恰好是 `b * h * seqlen_q`——combine 侧会以同样的 `stride` 读回来。

**（b）`lse_i` 与归一化 `O_i` 的诞生地。**

[softmax.h:292-309](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L292-L309)——`normalize_softmax_lse` 做了两件对本讲至关重要的事：

```cpp
float inv_sum = (sum == 0.f || sum != sum) ? 1.f : 1.f / sum;
lse(mi) = (sum == 0.f || sum != sum) ? (Split ? -INFINITY : INFINITY) : row_max(mi) * softmax_scale + __logf(sum);
float scale = !Is_dropout ? inv_sum : inv_sum * rp_dropout;
... acc_o_rowcol(mi, ni) *= scale;
```

- `acc_o *= 1/sum`：**输出已按本 split 的局部指数和归一化**——这正是公式里 \(O_i\) 的定义；
- `lse = row_max * softmax_scale + log(sum)`：即 \(\mathrm{lse}_i = m_i + \log l_i\)（自然底数）；
- 本 split 一个有效 token 都没有（`sum == 0`，例如被掩码全屏蔽）时，Split 路径写 `-INFINITY`——推论 1 的数据来源。

**（c）splitkv kernel：正常写出与空 split 早退。** splitkv kernel 的 epilogue（[flash_fwd_kernel.h:1193](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1193) 起同一套 `normalize_softmax_lse` + 视图构造 + [L1251](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1251) 的 `gLSEaccum(row) = lse(mi)` 写出）与 residual 完全同构。而当某 split 分到的块区间为空时：

[flash_fwd_kernel.h:618-656](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L618-L656)——`n_block_min >= n_block_max` 时早退，先写 `0` 到 `gOaccum`，再在 [L654](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L654) 写 `Split ? -INFINITY : INFINITY` 到 `gLSEaccum`。注释写得很清楚：不这样做 combine 阶段「会从 gK/gV 读到越界元素，或合并出错误结果」。零输出 + \(-\infty\) 的 LSE，代入公式权重恰为 0，该 split 对合并无害。

**（d）缓冲从哪来。** 最后确认缓冲的分配（u3-l3 已讲，这里只引用行号）：

[decode_api.cpp:299-311](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L299-L311)——启发式结果 `+= 1`，然后 `torch::empty` 分配 `{num_splits, b, h, seqlen_q}` 的 `softmax_lse_accum` 与 `{num_splits, b, h, seqlen_q, head_size_rounded}` 的 `out_accum`（均 float），并把指针塞进 `params`；`TORCH_CHECK(params.num_splits <= 128)` 是 combine kernel 的硬上限（下节解释）。

#### 4.1.4 代码实践：用 numpy 验证合并公式

**实践目标**：证明「先分两半各自做 softmax，再用 LSE 权重合并」与「不分半直接做 softmax」数值上一致，并观察空 split 的权重自动归零。

**操作步骤**（纯 CPU，无需 GPU 与项目编译）：

```python
# 示例代码：独立验证两 split 的 LSE 合并公式（对应 4.1.2 的推导）
import numpy as np
np.random.seed(0)
d, n1, n2 = 8, 6, 5
q  = np.random.randn(d)
K1, V1 = np.random.randn(n1, d), np.random.randn(n1, d)
K2, V2 = np.random.randn(n2, d), np.random.randn(n2, d)

def split_result(K, V):
    """模拟一个 split kernel 的写出：归一化 O_i 与 lse_i（softmax.h 的 normalize_softmax_lse）"""
    s = q @ K.T
    m = s.max()
    l = np.exp(s - m).sum()
    O = np.exp(s - m) @ V / l          # -> out_accum（已除以局部指数和）
    lse = m + np.log(l)                # -> softmax_lse_accum
    return O, lse

O1, lse1 = split_result(K1, V1)
O2, lse2 = split_result(K2, V2)

# --- combine kernel 做的事（对应 L1772-L1786 与 L1803/L1847）---
m = max(lse1, lse2)                    # L1777: 跨 split 的 max
L = m + np.log(np.exp(lse1 - m) + np.exp(lse2 - m))   # L1786: lse_logsum
O = np.exp(lse1 - L) * O1 + np.exp(lse2 - L) * O2     # L1803+L1847: 加权累加

# --- 参考答案：不切分直接算 ---
O_ref, lse_ref = split_result(np.concatenate([K1, K2]), np.concatenate([V1, V2]))
print("O   max abs err:", np.abs(O - O_ref).max())
print("lse abs err    :", abs(L - lse_ref))

# --- 空 split 实验：把 n2 置 0（对应 splitkv 的空 split 早退，lse=-inf）---
lse2_empty = -np.inf
m2 = max(lse1, lse2_empty)
L2 = m2 + np.log(np.exp(lse1 - m2) + np.exp(lse2_empty - m2))
print("empty-split weight of O1:", np.exp(lse1 - L2))   # 应为 1.0
```

**需要观察的现象**：`O` 与 `O_ref` 的最大误差在 \(10^{-16}\) 量级（双精度下即机器精度）；`L` 与 `lse_ref` 相等；空 split 时 `O1` 的权重恰好是 1.0。

**预期结果**：合并公式严格等价（误差仅来自浮点舍入）；`np.exp(-inf - m)` 为 0，不需要任何 `if` 就把空 split 排除。若你把 `lse2` 改成 `-np.inf` 后忘了在 `m` 里保护，numpy 会报 runtime warning 但结果仍对——CUDA 里 `-inf - -inf` 会产生 NaN，这正是源码要做特判的原因（见 4.4.3）。本实践可本地直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：三个 split 的 LSE 分别为 `lse = [10.0, 9.0, -inf]`，求各权重。
**答案**：\(m=10\)，\(L = 10 + \log(e^0 + e^{-1} + e^{-\infty}) = 10 + \log(1 + e^{-1}) \approx 10.3133\)；\(w_1 = e^{10-10.3133} \approx 0.7311\)，\(w_2 \approx 0.2689\)，\(w_3 = 0\)。权重和为 1。

**练习 2**：为什么 splitkv kernel 写 `gOaccum` 前必须先除以局部 `row_sum`？如果不除，combine 的权重公式要怎么改？
**答案**：除以 \(l_i\) 使 \(O_i\) 成为「局部 softmax 输出」，权重 \(e^{\mathrm{lse}_i - L}\) 恰好同时完成「乘回 \(e^{m_i-m} l_i\)、再除以全局 \(l\)」两步。若不除，写入的其实是未归一化分子 \(\tilde O_i = l_i O_i\)，权重需改为 \(e^{\mathrm{lse}_i - m}/\sum_k e^{\mathrm{lse}_k - L}\) 形式之外还需再除 \(e^{m_i}\) 一类常数因子——更关键的是 float 累积缓冲会存放大数量级的未归一化值，精度更差；且「每 split 输出本身就是一次合法的局部注意力」这一不变量会被破坏。源码选择了先归一化（softmax.h L306 的 `acc_o *= scale`）。

**练习 3**：combine 是在 fp32 上做累加的（`ElementAccum`），为什么最终输出转 fp16 前这一步很重要？
**答案**：splitkv/residual kernel 的 `acc_o` 本身是 fp32 累积器，写入 `out_accum` 也是 fp32；跨 split 的加权求和再除一次全局和，两步归一化都涉及数量级悬殊的指数值相加，fp16 的 11 位有效数字与 \(6\times10^{-8}\) 的 normal 下界不够用。combine 全程 fp32，只在最后一步 `convert_type<Element>` 转回 fp16 写 `params.o_ptr`（见 4.4.3），这是与不切分路径等价性的精度保障。

---

### 4.2 `flash_fwd_splitkv_combine_kernel`：启动、grid 与 `Log_max_splits` 阶梯

#### 4.2.1 概念说明

combine kernel 是一个**独立启动的第三个 kernel**（一个 decode 步依次跑 residual → splitkv → combine）。它的输入是两个 float 缓冲，输出是最终的 fp16 注意力结果。它面临一个设计选择：共享内存里要摆下「所有 split 的 LSE」这张小表，表的最大行数是 `num_splits` 的上限 128；但如果 `num_splits` 只有 2，却按 128 行分配、按 128 路 reduce，纯属浪费。`Log_max_splits` 模板参数就是解法：**把 1~128 的 split 数按 2 的幂分成 7 档，编译期特化 7 个 kernel 实例，运行时按实际 `num_splits` 选一档**——共享内存和归约步数都只付实际需要的代价。

#### 4.2.2 核心流程

```text
run_flash_splitkv_fwd(params, stream):
    1. 启动 residual kernel  (grid = (num_m_block, b, h))      # 写槽位 num_splits-1
    2. 启动 splitkv kernel   (grid = (num_m_block, num_splits-1, b*h))  # 写槽位 0..num_splits-2
    3. if num_splits > 1:                                     # decode 下恒成立（+1 协议）
         kBlockM := 4 (headdim=128 时)
         grid_combine := ceil(b*h*seqlen_q / kBlockM)
         选档: num_splits≤2→Log=1, ≤4→2, ≤8→3, ..., ≤128→7
         flash_fwd_splitkv_combine_kernel<Kernel_traits, kBlockM, Log><<<grid_combine, 128, 0>>>
```

#### 4.2.3 源码精读

**kernel 定义**——一层薄包装，直接转发到本讲主角：

[flash_fwd_launch_template.h:66-69](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L66-L69)——`DEFINE_FLASH_FORWARD_KERNEL(flash_fwd_splitkv_combine_kernel, int kBlockM, int Log_max_splits, bool Is_even_K)`，`static_assert(Log_max_splits >= 1)`，调用 `flash::combine_attn_seqk_parallel<Kernel_traits, kBlockM, Log_max_splits, Is_even_K>(params)`。

**三 kernel 顺序与 grid**：

[flash_fwd_launch_template.h:82-104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L82-L104)——`num_splits_ = params.num_splits - 1`；residual kernel 以 `grid_res(num_m_block, b, h)` 启动，splitkv kernel 以 `grid(num_m_block, num_splits_, b*h)` 启动，两者均带各自的动态共享内存（超过 48KB 时先 `cudaFuncSetAttribute` 抬限，见 u5-l1/u5-l4）。

**combine 的启动**：

[flash_fwd_launch_template.h:106-127](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L127)——三个要点：

```cpp
if (params.num_splits > 1) {
    constexpr static int kBlockM = Kernel_traits::kHeadDim % 128 == 0 ? 4 : (Kernel_traits::kHeadDim % 64 == 0 ? 8 : 16);
    dim3 grid_combine((params.b * params.h * seqlen_q + kBlockM - 1) / kBlockM);
    EVENK_SWITCH(is_even_K, IsEvenKConst, [&] {
        if (params.num_splits <= 2)      { ...combine_kernel<..., 1, ...><<<grid_combine, kNThreads, 0, stream>>>(params); }
        else if (params.num_splits <= 4)  { ...<..., 2, ...>... }
        ... // ≤8→3, ≤16→4, ≤32→5, ≤64→6
        else if (params.num_splits <= 128){ ...<..., 7, ...>... }
    });
}
```

- **`kBlockM = 4`（headdim=128 时）**：注意这是 combine 自己的 `kBlockM`，与解码主 kernel 的 `kBlockM=16` 无关！combine 的「行」是 `(batch, head, q)` 展平后的全局行，每行只需做一次「跨 split 的标量加权求和」，没有 MMA tile 的约束，所以敢取小 tile。
- **`grid_combine = ceil(b·h·seqlen_q / 4)`**：decode 时 `seqlen_q = 1`；若发生 GQA 的 `seqlenq_ngroups_swapped` 重排（u3-l1），`seqlen_q` 变为 `ngroups`、`h` 变为 KV 头数。一个 block（128 线程）负责 4 个全局行的合并。
- **`<<<grid_combine, kNThreads, 0, stream>>>`**：第三个参数 0——combine **不用动态共享内存**，它的 `sLSE` 是静态声明的（见 4.3.3），大小由模板参数编译期决定。
- **阶梯的 7 档**：`num_splits ≤ 2^k → Log_max_splits = k`，`kMaxSplits = 1 << k ∈ {2,4,8,...,128}`。上限 128 正是 `decode_api.cpp:311` 那条 `TORCH_CHECK` 的来源。
- 一个边界：若 `params.num_splits > 128`，阶梯全部落空、**combine 不会被启动**——但 host 侧的 `TORCH_CHECK` 已经挡住这种情况，所以这是「防御性死代码」而非 bug。

**为什么 decode 下 combine 恒启动**：u3-l3 讲过 `params.num_splits = 启发式结果 + 1 ≥ 2`，因此 `num_splits > 1` 恒为真——每个 decode 步固定是「residual + splitkv + combine」三连发。

#### 4.2.4 代码实践：手算一次启动参数

**实践目标**：给定一个具体 decode 配置，推出 combine kernel 的全部启动参数与共享内存用量，把 `Log_max_splits` 阶梯用活。

**配置**：`headdim=128`，`b=2`，`h=16`（KV 头数），`seqlen_q=8`（GQA 重排后 q 序列维），`num_splits=5`，`d=128`。

**操作步骤**（纸笔推导，可用一段 Python 复核）：

1. `kBlockM = 4`（headdim%128==0）。
2. `grid_combine = ceil(2×16×8 / 4) = 64` 个 block，每 block 128 线程。
3. `num_splits=5 ≤ 8` → `Log_max_splits=3` → `kMaxSplits = 8`。
4. 共享内存 `sLSE[8][4+1]` = 40 个 float = **160 字节**（静态分配，无需 `cudaFuncSetAttribute`）。
5. 每线程 LSE 持有量 `kNLsePerThread = ceil(8×4/128) = 1`；归约宽度 `kRowsPerLoadTranspose = min(128/4, 8) = 8` → 用 `Allreduce<8>`，蝴蝶 shuffle 共 3 步（xor 4、2、1）。

**需要观察的现象**：把 `num_splits` 从 2 扫到 128，`kMaxSplits` 只在 2/4/8/…/128 七个点上跳变；`kRowsPerLoadTranspose` 在 `kMaxSplits ≤ 32` 时始终等于 `kMaxSplits`，直到 128 才被 `128/kBlockM = 32` 截断。

**预期结果**：阶梯把「最多 128 split」的最坏情况压缩到实际需要的那一档；`kRowsPerLoadTranspose ≤ 32` 保证归约永远不跨 warp（下节的 `static_assert`）。复核用示例代码：

```python
# 示例代码：复核 4.2.4 的推导
import math
kBlockM, kNThreads = 4, 128
for num_splits in [2, 3, 5, 9, 33, 100, 128]:
    log_max_splits = max(1, (num_splits - 1).bit_length())   # ≤2^k → k
    kMaxSplits = 1 << log_max_splits
    kNLsePerThread = math.ceil(kMaxSplits * kBlockM / kNThreads)
    kRPL = min(kNThreads // kBlockM, kMaxSplits)
    print(f"num_splits={num_splits:>3}  Log={log_max_splits}  kMaxSplits={kMaxSplits:>3}  "
          f"sLSE={kMaxSplits*(kBlockM+1)*4}B  NLse/thread={kNLsePerThread}  Allreduce<{kRPL}>")
```

（`(num_splits-1).bit_length()` 与源码「`≤ 2^k` 取 `k`」的阶梯逐点一致，可本地运行核对。）

#### 4.2.5 小练习与答案

**练习 1**：为什么 `kBlockM` 在 headdim=128 时取 4，而不是与主 kernel 一致的 16？
**答案**：主 kernel 的 `kBlockM=16` 由 MMA tile（16×8×16）与共享内存 Q tile 决定；combine 没有 MMA，行数只影响「一个 block 覆盖多少全局行」与 `sLSE` 的第二维。取 4 让 `grid_combine = b·h·seqlen_q/4` 足够大（decode 时 b·h 通常几百，除以 4 仍有上百个 block），能铺满 SM；同时 `128/4 = 32` 恰好是一个 warp，后面跨 split 归约可以在 warp 内完成。

**练习 2**：如果去掉 `Log_max_splits` 阶梯、只编译 `Log=7`（kMaxSplits=128）一个实例，功能还正确吗？代价是什么？
**答案**：功能正确——谓词 `row < params.num_splits` 会把多余槽位填 `-INFINITY`，权重为 0。代价：(a) `sLSE` 恒为 128×(kBlockM+1)×4B ≈ 2.5KB 静态共享内存；(b) `kNLsePerThread` 变 4，装载/转置/归约循环全部按 4 倍展开，寄存器与指令数浪费；(c) 阶梯版本对常见小 split 数（decode 短上下文时 num_splits 常是个位数）用 1~2 步 shuffle 就完成归约。这是「编译期换运行时」的标准 CUDA 手法。

---

### 4.3 `combine_attn_seqk_parallel`（上）：LSE 装载、转置与 warp 归约

#### 4.3.1 概念说明

函数签名的四件套（[flash_fwd_kernel.h:1699-1700](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1699-L1700)）：`Kernel_traits` 提供类型与 `kHeadDim/kNThreads`，`kBlockM` 是每 block 行数，`Log_max_splits` 决定 `kMaxSplits`，`Is_even_K` 决定列方向（head dim）是否需要谓词。

上半场只处理一张小表：**`num_splits × kBlockM` 个 LSE 值**。要做的事是「按行求 logsumexp」，但数据在 global memory 里按 `(split, 行)` 排布、而 128 个线程天然按一维编号——于是代码用了「gmem → smem（行主序）→ 寄存器（列主序）」的**两段转置**，让同一输出行的所有 split 落到同一段连续 lane 上，warp shuffle 归约就能直接用。这是本模块最值得学会的技巧。

#### 4.3.2 核心流程

```text
阶段 A  装载+写 smem:  128 线程协作把 gLSEaccum[kMaxSplits][kBlockM] 拷进 sLSE（越界填 -INF）
阶段 B  __syncthreads()
阶段 C  转置读回:      lse_accum(l) = sLSE[tidx%kRPL 所属 split][tidx/kRPL 所属行]
                      → 同一行的 kRPL 个 split 恰好落在 kRPL 个连续线程上
阶段 D  行内归约:      lse_max  = Allreduce<kRPL>(max, lse_accum)
                      lse_sum  = Allreduce<kRPL>(sum, expf(lse - lse_max))
                      lse_logsum = logf(lse_sum) + lse_max        # 即公式里的 L
阶段 E  写回:          gLSE[row] = lse_logsum（最终 LSE，可选输出）
                      sLSE[split][row] = expf(lse - lse_logsum)   # 权重 w_i，供下半场用
```

#### 4.3.3 源码精读

**静态共享内存与编译期防线**：

[flash_fwd_kernel.h:1704-1714](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1704-L1714)——`kMaxSplits = 1 << Log_max_splits`；三条 `static_assert`（≤128、`kBlockM∈{4,8,16,32}`、`kNThreads==128`）把上一节的阶梯协议写死在编译期；`__shared__ ElementAccum sLSE[kMaxSplits][kBlockM + 1]`，注释说明 **+1 是为了减少 bank conflict**（每行多 1 个 float 错位，相邻行不落在同一 bank）。

**gLSEaccum 视图**：

[flash_fwd_kernel.h:1720-1725](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1720-L1725)——`lse_size = b*h*seqlen_q`；本 block 负责的行起点 `row_offset_lse = bidx * kBlockM`；

```cpp
Tensor gLSEaccum = make_tensor(... params.softmax_lseaccum_ptr + row_offset_lse ...,
                               Shape<Int<kMaxSplits>, Int<kBlockM>>{},
                               make_stride(lse_size, _1{}));
```

split 维 stride = `lse_size`，与生产者侧 `row_offset_lseaccum` 的公式（4.1.3 (a)）严格互逆——**写和读是同一张内存布局的两面**。

**阶段 A：装载并填 `-INFINITY`**：

[flash_fwd_kernel.h:1741-1754](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1741-L1754)——`kNLsePerThread = ceil(kMaxSplits·kBlockM/128)`，`kRowsPerLoadLSE = 128/kBlockM`；每个线程算出自己的 `(row=split, col=输出行)`：

```cpp
ElementAccum lse = (row < params.num_splits && col < lse_size - bidx * kBlockM) ? gLSEaccum(row, col) : -INFINITY;
if (row < kMaxSplits) { sLSE[row][col] = lse; }
```

两个谓词分别挡住「阶梯取大了」（`kMaxSplits > num_splits` 的空档）与「最后一个 block 的行越界」（`b·h·seqlen_q` 不被 4 整除时）。**越界即 `-INFINITY`**——等价条件判断第一处：无效 split 权重自动为 0。

**阶段 C：转置读回**：

[flash_fwd_kernel.h:1755-1770](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1755-L1770)——`kRowsPerLoadTranspose = min(kRowsPerLoadLSE, kMaxSplits)`；

```cpp
const int row = l * kRowsPerLoadTranspose + tidx % kRowsPerLoadTranspose;  // split 维
const int col = tidx / kRowsPerLoadTranspose;                             // 输出行
lse_accum(l) = (row < kMaxSplits && col < kBlockM) ? sLSE[row][col] : -INFINITY;
```

对照阶段 A 的 `row = ... + tidx/kBlockM; col = tidx%kBlockM`——**两次除/取模的方向恰好互换**，这就是转置。效果：线程 `col·kRPL + r` 持有「第 `col` 行、第 `r` 个 split」的 LSE。两条 `static_assert`（[L1762-1763](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1762-L1763)）：`kRowsPerLoadTranspose ≤ 32` 保证**一行的所有 split 落在同一个 warp**；`kNLsePerThread · kRowsPerLoadTranspose ≤ kMaxSplits` 保证循环覆盖不重不漏。源码注释（L1757-1759）举例：kMaxSplits=16、kBlockM=4 时 kRPL=16、每线程 1 个元素，16 个连续线程正好摊平一行。

**阶段 D：warp 蝴蝶归约求 `lse_logsum`（公式里的 \(L\)）**：

[flash_fwd_kernel.h:1772-1786](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1772-L1786)——逐行对照 4.1.2 的公式：

```cpp
ElementAccum lse_max = lse_accum(0);
for (int l = 1; ...) { lse_max = max(lse_max, lse_accum(l)); }   // 线程内局部 max
lse_max = Allreduce<kRowsPerLoadTranspose>::run(lse_max, max_op);// → m = max_i lse_i
lse_max = lse_max == -INFINITY ? 0.0f : lse_max;                 // 特判①：全 -inf 时给 0
float lse_sum = expf(lse_accum(0) - lse_max);
for (int l = 1; ...) { lse_sum += expf(lse_accum(l) - lse_max); }// 线程内局部 Σ e^{lse_i - m}
lse_sum = Allreduce<kRowsPerLoadTranspose>::run(lse_sum, sum_op);// → Σ_i e^{lse_i - m}
ElementAccum lse_logsum = (lse_sum == 0.f || lse_sum != lse_sum) ? INFINITY : logf(lse_sum) + lse_max;  // 特判②
```

- `Allreduce<W>`（[utils.h:133-153](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L133-L153)）是递归蝴蝶：`x = op(x, __shfl_xor_sync(-1, x, W/2))` 逐层折半，`Allreduce<2>` 兜底；只与「编号异或后仍在组内」的 lane 交换，**不需要共享内存、不需要锁步屏障**。这正是阶段 C 转置的意义：让归约组恰好对齐 shuffle 的异或分组。（对比 u4-l2 的 `allreduce_` 要跨 4 个 warp、必须借共享内存——两者名字像，机制不同。）
- `logf(lse_sum) + lse_max` 就是 \(\log\sum_i e^{\mathrm{lse}_i - m} + m = L\)。

**等价条件判断②的含义**（源码注释 L1784-1785 原文）：若某行所有 split 的 LSE 都是 `-INFINITY`（整行没有任何有效 KV token），`lse_sum` 会是 0，直接 `logf(0) = -inf`，下一行代码 `lse_accum(l) - lse_logsum` 出现 \(-\infty - (-\infty) = \mathrm{NaN}\)。所以把 `lse_logsum` 特判为 `+INFINITY`，随后 `expf(-inf - inf) = expf(-inf) = 0`，该行权重全 0、输出全 0，干净落地。`lse_sum != lse_sum` 是 NaN 检测（`NaN != NaN` 为真）。特判①同理：全 `-inf` 时 `lse_max` 会是 `-inf`，`lse - lse_max` 变 NaN，先把它抬成 0。

**阶段 E：写回**：

[flash_fwd_kernel.h:1788-1805](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1788-L1805)——每个输出行由 `tidx % kRowsPerLoadTranspose == 0` 的「组长」线程把 `lse_logsum` 写进最终 LSE 张量（`unpadded_lse` 两个分支对应 varlen 与定长两种 LSE 排布，LSE 张量由 [decode_api.cpp:413](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L413) 分配）；随后所有线程把**权重**写回共享内存：

```cpp
if (row < params.num_splits && col < kBlockM) { sLSE[row][col] = expf(lse_accum(l) - lse_logsum); }
```

注意 `sLSE` 被**原地复用**：上半场存 LSE 原值，下半场（`__syncthreads()` 之后）存权重 \(w_i\)——160 字节的共享内存干了两份活。

#### 4.3.4 代码实践：把 128 个线程「摆」进 tile，推一遍索引

**实践目标**：亲手验证阶段 A/C 的两个索引公式确实互为转置，并确认归约组不跨 warp。

**操作步骤**（纸笔 + 示例代码复核）：

1. 取 `kBlockM=4`、`kMaxSplits=8`（即 4.2.4 的档位）。
2. 阶段 A：`kRowsPerLoadLSE = 32`，`kNLsePerThread = ceil(32/128) = 1`；对 `tidx = 0..127` 算 `row = tidx/4`（0..31，截到 8）、`col = tidx%4`。
3. 阶段 C：`kRowsPerLoadTranspose = min(32,8) = 8`；`row = tidx%8`（split 0..7）、`col = tidx/8`（输出行 0..15，截到 4）。
4. 回答：输出行 2 的 8 个 split 分别由哪些线程持有？它们在同一个 warp 吗？

**需要观察的现象**：输出行 2 的 split 0..7 由线程 16..23 持有（`col=2` ⇒ `tidx∈[16,24)`），全在 warp 0；`Allreduce<8>` 的三次异或是 4、2、1，都在这 8 个 lane 内部完成。

**预期结果**：两阶段索引合起来是精确转置；每组的 lane 连续且组大小 ≤32。复核用示例代码：

```python
# 示例代码：复核两阶段索引的转置关系
kBlockM, kMaxSplits, kNThreads = 4, 8, 128
kRPL_load = kNThreads // kBlockM
kRPL_trans = min(kRPL_load, kMaxSplits)
stageA = {(t // kBlockM, t % kBlockM) for t in range(kNThreads)}          # (split, row)
stageC = {(t % kRPL_trans, t // kRPL_trans) for t in range(kNThreads)}    # (split, row)
assert stageA == stageC == {(s, r) for s in range(kRPL_trans) for r in range(kRPL_load)}
row2_owners = [t for t in range(kNThreads) if t // kRPL_trans == 2]
print("row 2 的 split 由线程持有:", row2_owners, "| 同 warp:", len(set(t // 32 for t in row2_owners)) == 1)
```

#### 4.3.5 小练习与答案

**练习 1**：`sLSE` 的第二维为什么 +1？不加会怎样？
**答案**：防止 bank conflict。`kBlockM+1=5` 个 float 一行，相邻行的同一列错开 1 个 float（4 字节），`sLSE[row][col]` 按列访问时不同 row 命中不同 bank；若不加，`kBlockM=4` 行（16 字节）恰好对齐 bank 周期，列方向访问会串行化。这是 CUDA 共享内存的经典 padding 技巧（32 bank × 4B）。

**练习 2**：如果把阶段 C 的转置去掉、直接让每个线程沿 split 维循环读 gmem 求 logsumexp，会有什么问题？
**答案**：(a) gmem 访问不再合并——每线程沿 split 维跨 `lse_size` 步长跳读，一个 warp 一次发出的 32 个地址彼此相距 `lse_size×4` 字节，无法合并成少量 128B 事务；(b) 无法用 warp shuffle 归约，只能回到共享内存两遍扫；(c) gmem→smem 一次协作拷贝 + 转置读 smem，把「不规则的 gmem 访问」换成了「规则的 smem 访问」，正是两段式的动机。

---

### 4.4 `combine_attn_seqk_parallel`（下）：加权累加与最终写出

#### 4.4.1 概念说明

下半场执行公式的最后一行：\(O = \sum_i e^{\mathrm{lse}_i - L} O_i\)。数据是 `out_accum` 里 `num_splits` 份 `(kBlockM 行 × 128 维)` 的 float 块。难点不在数学（权重已经躺在 `sLSE` 里），而在**访存组织**：128 个线程要把每份块按 `(4 行 × 128 列)` 的 tile 摆开、逐 split 读入寄存器、乘权重累加，最后按 `(batch, head, row)` 的真实 stride 写回 fp16 输出。

还要注意一个容易漏看的点：**循环上界是 `params.num_splits`，不是 `num_splits - 1`**——最后一个槽位属于 residual kernel（u5-l4 的 piggyback 结果也一并在这里被合并），combine 对两类生产者一视同仁。

#### 4.4.2 核心流程

```text
for split in [0, num_splits):                     # 含 residual 占用的最后一槽
    从 gOaccum[split] 协作拷贝 kBlockM×kHeadDim 的 float tile 进寄存器 tOrOaccum
    对每行 row: lse_scale = sLSE[split][row]      # 权重 w_i
    tOrO(i,m,k) += lse_scale * tOrOaccum(i,m,k)   # 加权累加
    指针步进 b*h*seqlen_q*d_rounded 到下一 split
rO = fp16(tOrO)
按 (batch_idx, head_idx, row) 分解 → o_batch/head/row_stride 定位 → 向量化写出
```

#### 4.4.3 源码精读

**gOaccum 视图与线程摆放**：

[flash_fwd_kernel.h:1807-1823](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1807-L1823)——行偏移 `bidx*kBlockM*d_rounded`，视图形状 `(kBlockM, kHeadDim)`、stride `(kHeadDim, 1)`（即每 split 一块连续的 `kBlockM×128` float）；`GmemTiledCopyOaccum` 把 128 线程摆成 `(kBlockM=4 行, kBlockN=128/4=32 列)` 的 tile、每线程 4 个连续 float（val layout `Shape<_1,_4>`，一次 16B 向量化访问）；累加器 `tOrO` 清零。

**列谓词**：

[flash_fwd_kernel.h:1826-1833](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1826-L1833)——`!Is_even_K` 时按 `get<1>(坐标) < params.d` 生成 `tOpOaccum`，处理 `d < kHeadDim` 的非整 head dim（本仓库 hdim=128 且 d=128，`Is_even_K` 恒真，此为继承自 FA 的通用路径）。

**加权累加主循环**：

[flash_fwd_kernel.h:1835-1853](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1835-L1853)——

```cpp
for (int split = 0; split < params.num_splits; ++split) {
    flash::copy</*Is_even_MN=*/false, Is_even_K>(..., params.b * params.h * params.seqlen_q - bidx * kBlockM);
    for (int m = 0; m < size<1>(tOrOaccum); ++m) {
        int row = get<0>(tOcOaccum(0, m, 0));
        ElementAccum lse_scale = sLSE[split][row];            // ← 权重 w_i = exp(lse_i - L)
        for (...) for (...) tOrO(i, m, k) += lse_scale * tOrOaccum(i, m, k);   // ← O += w_i * O_i
    }
    tOgOaccum.data() = tOgOaccum.data() + params.b * params.h * params.seqlen_q * params.d_rounded;
}
```

四个要点：

1. **`row` 是 tile 内行号**，`sLSE[split][row]` 取的正是 4.3.3 阶段 E 写入的权重——上一模块与这一模块在 `sLSE` 上交接。
2. **行方向谓词**（`flash::copy` 的最后一个参数）挡住最后一个 block 的越界行；`Is_even_MN=false` 也解释了为什么越界行允许读到垃圾：反正权重路径上这些行最终不会被写出。
3. **指针步进** `b·h·seqlen_q·d_rounded` 与视图构造互逆，逐 split 顺序扫过 `out_accum` 的第一维。
4. `params.num_splits` 上限 128 ⇒ 循环最多 128 次迭代，每次读 `4×128×4B = 2KB` float——这就是 combine 的全部 HBM 读入，与 KV 总长无关（KV 在前两个 kernel 已经被消费）。

**最终写出**：

[flash_fwd_kernel.h:1856-1881](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1856-L1881)——`rO = flash::convert_type<Element>(tOrO)`（fp32→fp16，只转一次）；全局行号 `idx = bidx*kBlockM + row` 越界检查后**分解回三维**：

```cpp
const int batch_idx = idx / (params.h * params.seqlen_q);
const int head_idx  = (idx - batch_idx * (params.h * params.seqlen_q)) / params.seqlen_q;
const int row       = idx - batch_idx * (params.h * params.seqlen_q) - head_idx * params.seqlen_q;
auto o_ptr = ... params.o_ptr + batch_idx * params.o_batch_stride + head_idx * params.o_head_stride + row * params.o_row_stride;
copy(rO(_, m, k), gO);   // 每线程 4 个 float→fp16 向量化写出
```

回忆 u3-l1：GQA 的 `seqlenq_ngroups_swapped` 重排把 q 头折进了序列维，所以 combine 的输出必须按「合并后的 (b,h,seqlen_q) 逻辑形状 × o_*_stride」写出，再由 **host 侧的逆重排**（`mha_fwd_kvcache` 返回前的 transpose）还原成 `(b, seqlen_q, h, d)`——combine 是重排发生侧的最后一个 kernel。

**三处等价条件判断汇总**（本讲学习目标之三）：

| 位置 | 判断 | 防住的事故 |
|---|---|---|
| [L1749](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1749) | 越界/空档槽位填 `-INFINITY` | 阶梯取大或行越界时读到未初始化值；`-inf` 使权重为 0，空 split（splitkv 早退写的 `-inf`）自动出局 |
| [L1778](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1778) | `lse_max == -INFINITY ? 0.0f` | 全 `-inf` 时 `lse - lse_max = NaN`，污染 `lse_sum` |
| [L1786](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1786) | `lse_sum == 0 \|\| lse_sum != lse_sum ? INFINITY` | `logf(0) = -inf` 导致下一步 `-inf - -inf = NaN`；特判后 `expf(-inf - inf) = 0`，整行输出干净的 0 |

这三处加上生产者侧的 `Split ? -INFINITY : INFINITY`（softmax.h L303、flash_fwd_kernel.h L654），构成一条完整的「**用 ±infinity 作哨兵、让指数函数自动清零**」的边界协议——没有一处需要 `if (empty)` 分支跳出主公式，公式在所有输入下保持同一个形状。

#### 4.4.4 代码实践：跟踪一行数据的完整旅程（源码阅读型）

**实践目标**：把 `b=1, h=2, seqlen_q=1, num_splits=3, d=128` 时「batch 0、KV 头 1、query 行 0」这一行的输出，从产生到合并全程串起来，验证你对两个缓冲读写对称性的理解。

**操作步骤**：

1. 生产者侧：代入 4.1.3 (a) 的 `row_offset_lseaccum = ((n_split_idx·1 + 0)·2 + 1)·1 + 0 = 2·n_split_idx + 1`。写出 split 0/1（splitkv）与 split 2（residual）各自写的 `softmax_lse_accum` 下标。
2. 消费者侧：本例 `lse_size = 2`，combine 的 `grid_combine = ceil(2/4) = 1`，`bidx=0`；该行在 `gLSEaccum` 视图中的 `(row=split, col=1)`，即地址 `softmax_lseaccum_ptr + split·2 + 1`——与步骤 1 的下标逐点对照。
3. 同理对 `out_accum`：生产者 `row_offset_oaccum = ((split·1+0)·2+1)·1·128 = (2·split+1)·128`，消费者 `row_offset_oaccum = bidx·4·128 = 0` 加上 tile 内行 1、split 步进 `2·1·128 = 256`——再次逐点对照。
4. 输出侧：`idx = 0·4 + 1 = 1`，分解 `batch_idx = 1/(2·1) = 0`、`head_idx = 1/1 = 1`、`row = 0`，按 `o_batch_stride/o_head_stride` 定位写出地址。

**需要观察的现象**：三个生产者写的地址集合与 combine 读的地址集合**完全相同**——同一公式 `(split·b + bidb)·h + bidh)·seqlen_q + m_block·kBlockM`（LSE 侧）与它的 `×d_rounded` 版本（O 侧）两侧共享。

**预期结果**：你会确认「缓冲布局是生产者与消费者之间唯一的契约」——任何一侧改 stride，另一侧必须同步。这也是 u7-l3 新增配置时容易踩的坑。本实践为纯源码推导，结论可直接对照源码行号验证，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：combine 的 HBM 读流量大约是多少？它随 KV 长度增长吗？
**答案**：读 `num_splits × kBlockM × 128 × 4B`（LSE 另有 `num_splits × kBlockM × 4B`），写 `kBlockM × 128 × 2B`；对固定的 `num_splits` 上限 128，最多约 128×2KB ≈ 256KB/行组，**与 KV 序列长度无关**——KV 的读取全部发生在 splitkv/residual kernel，且 u3-l3 说过「每个 token 恰属一个 split」，split 数增加不增加 KV 总读量，只增加这里的中间缓冲流量（这正是启发式用 85% 规则选最小 split 数的原因）。

**练习 2**：假设某个 split 的 LSE 因为 bug 写成了 `+INFINITY`，combine 会输出什么？能被 `TORCH_CHECK` 之类挡住吗？
**答案**：`lse_max = +inf`，`expf(lse_i - inf)` 对其余 split 为 0、对自身为 `expf(inf - inf) = NaN`，`lse_sum = NaN`，触发 `lse_sum != lse_sum` 特判 → `lse_logsum = +INFINITY`，所有权重 `expf(±inf - inf) = NaN 或 0`，该行输出 NaN/0。host 侧没有对应的 `TORCH_CHECK`——这是 kernel 间的隐式契约，只能靠 `evaluation/test.py` 这类端到端误差检查兜底（u7-l1）。

**练习 3**：为什么加权循环里 `lse_scale` 在 `m` 循环内取、而不是提到循环外？
**答案**：`lse_scale` 依赖**tile 内行号** `row = get<0>(tOcOaccum(0, m, 0))`——一个线程在 `m` 维上可能负责多行（本配置 `kBlockM=4`、128 线程摆 4×32 tile 时每线程只管 1 行，但代码必须对通用摆放成立），不同行权重不同，所以必须在 `m` 循环内查 `sLSE[split][row]`。

---

## 5. 综合实践

**任务：手推两 split 合并公式，并对照 `combine_attn_seqk_parallel` 完成「公式 ↔ 代码行」标注。**（本讲规格指定的主实践）

**第一步（推导）**：给定 `(O1, lse1)` 与 `(O2, lse2)`，其中 `O_i` 是各 split **已归一化**的输出、`lse_i = m_i + log l_i`。写出合并输出与合并 LSE。参考答案：

\[
L = m + \log\!\left(e^{\mathrm{lse}_1 - m} + e^{\mathrm{lse}_2 - m}\right),\quad m = \max(\mathrm{lse}_1, \mathrm{lse}_2)
\]
\[
O = e^{\mathrm{lse}_1 - L}\,O_1 + e^{\mathrm{lse}_2 - L}\,O_2
\]

（推导过程见 4.1.2；可先用 4.1.4 的 numpy 脚本数值验证再往下走。）

**第二步（标注）**：把公式逐项对到源码。参考答案表：

| 公式量 | 代码位置 | 代码 |
|---|---|---|
| 载入 \(\mathrm{lse}_i\) | [flash_fwd_kernel.h:1749](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1749) | `lse = (...) ? gLSEaccum(row, col) : -INFINITY` |
| 转置到归约布局 | [L1766-1768](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1766-L1768) | `lse_accum(l) = sLSE[row][col]` |
| \(m = \max_i \mathrm{lse}_i\) | [L1773-1777](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1773-L1777) | 局部 max + `Allreduce<...>::run(lse_max, max_op)` |
| \(\sum_i e^{\mathrm{lse}_i - m}\) | [L1779-1783](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1779-L1783) | `expf(lse_accum(l) - lse_max)` 求和 + `Allreduce` |
| \(L\) | [L1786](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1786) | `logf(lse_sum) + lse_max` |
| \(w_i = e^{\mathrm{lse}_i - L}\) | [L1803](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1803) | `sLSE[row][col] = expf(lse_accum(l) - lse_logsum)` |
| \(O = \sum_i w_i O_i\) | [L1842-1847](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1842-L1847) | `tOrO(i,m,k) += lse_scale * tOrOaccum(i,m,k)` |
| 逐 split 迭代 | [L1835](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1835) | `for (int split = 0; split < params.num_splits; ++split)`（含 residual 槽位） |

**第三步（可选，需 GPU）**：跑一遍 `evaluation/test.py`（用法见 u1-l4）。combine 在每个 decode 步都会执行（`num_splits ≥ 2` 恒成立），因此你看到的每一行 MAE 输出都已经是「三个 kernel + LSE 合并」的端到端误差。改 `num_bits=2` 再跑一遍，对比误差量级变化。运行结果**待本地验证**（本讲义未在 GPU 环境执行）。

**验收标准**：不看讲义，能对着 4.1.2 的公式在源码里指出每一项的实现行，并解释三处 infinity 哨兵各自的位置与作用。

## 6. 本讲小结

- **合并公式**：\(O = \sum_i e^{\mathrm{lse}_i - L} O_i\)，\(L = \mathrm{LSE}(\mathrm{lse}_1,\dots,\mathrm{lse}_n)\)；权重同时完成「乘回局部指数和、除以全局指数和」，与「先归一化再合并」的生产者约定（softmax.h 的 `acc_o *= 1/sum`）严格配套，合并结果与不切分数学等价。
- **`softmax_lse_accum` / `out_accum` 是生产者与消费者之间唯一的契约**：布局 `{num_splits, b, h, seqlen_q(, d_rounded)}`、float、由 `set_params_splitkv` 分配；splitkv 写槽位 `0..num_splits-2`，residual 独占 `num_splits-1`，combine 遍历全部 `num_splits` 个槽位，空槽以 `-INFINITY` 兜底。
- **`Log_max_splits` 阶梯**：把 1~128 的 split 数分 7 档编译期特化，`kMaxSplits = 1<<Log` 决定静态共享内存 `sLSE[kMaxSplits][kBlockM+1]` 的大小与归约宽度；combine 的 `kBlockM=4`（hdim128）与主 kernel 的 16 无关，`grid_combine = ceil(b·h·seqlen_q/4)`。
- **两段转置 + warp 蝴蝶归约**：gmem→smem 按行主序协作装载，再转置读回使「一行的所有 split」落在连续 lane 上，`Allreduce<W>` 用 `__shfl_xor_sync` 无共享内存地完成 max/sum 归约。
- **等价条件判断**：三处哨兵（装载填 `-inf`、`lse_max==-inf→0`、`lse_sum==0/NaN→+inf`）让同一公式覆盖空 split、越界行、整行无 token 等全部边界，指数函数自动把无效项清零，无需分支。
- **代价视角**：combine 的流量与 KV 长度无关（每行组最多 ~128×2KB），split 数只影响这部分中间流量——这是 u3-l3 启发式「85% 规则取最小 split 数」的另一半理由。

## 7. 下一步学习建议

至此第五单元（解码 kernel 全家福）完结：traits（u5-l1）→ splitkv 主循环（u5-l2）→ LOP3 反量化（u5-l3）→ residual kernel（u5-l4）→ combine（本讲）。两条后续路线：

1. **回到模型层（u6）**：看 `evaluation/llama.py` 如何消费 `fwd_kvcache_int` 返回的 `out`（正是 combine 写出的 `params.o_ptr` 内容），以及 `DynamicCache` 与 `*_new` 四件套的拼接——你会看到本讲的输出直接变成 LlamaBitDecoding 的注意力返回值。
2. **进入测试与扩展（u7）**：`evaluation/test.py` 的逐轮 MAE 是验证「三 kernel + 合并」端到端正确性的第一道闸门（u7-l1）；若做 u7-l3 的新增 `group_size` 配置实践，本讲 4.4.4 的「缓冲布局契约」是最容易踩的坑——combine 不需要改模板参数，但任何 stride/形状的改动都会在这里炸出 NaN。

建议再通读一遍 `combine_attn_seqk_parallel`（约 180 行）作为本单元的收官自测：能不查资料说出每 20 行在做什么，就可以进入第六单元了。
