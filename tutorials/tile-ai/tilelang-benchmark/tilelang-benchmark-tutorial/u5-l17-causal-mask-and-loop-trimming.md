# causal 掩码、循环裁剪与 -inf 处理

## 1. 本讲目标

本讲承接 [u5-l16 FlashAttention：在线 softmax 与 macro 结构](./u5-l16-flashattention-online-softmax.md)，专门回答一个问题：当注意力是**因果**（causal / 自回归）时，TileLang 内核如何把「上三角不许看」这条规则落地成数值，并且不浪费算力。

学完后你应能：

- 说清 `is_causal` 在数值上做了什么——用 `T.if_then_else` 配合 `-T.infinity(...)` 把不该看的位置置成 \(-\infty\)。
- 解释 `loop_range` 如何按 query block 裁剪 KV 循环，跳过整块被掩码的 KV 块。
- 理解为什么 `acc_s` 里的 \(-\infty\) 经过 `exp2` 后自动变成 0，以及它在什么情况下会「失灵」退化成 NaN。
- 认识注释里提到的 FA3 `Check_inf` 技巧：把全 \(-\infty\) 行的最大值归零，避免 NaN 污染整个 softmax。

## 2. 前置知识

- **u5-l16 的成果**：FlashAttention 四段 macro（`MMA0 → Softmax → Rescale → MMA1`）、在线 softmax 的 `scores_max / scores_scale / logsum` 更新、以及 `exp2(x·scale)` 用 `scale = (1/\mathrm{dim})^{0.5}\cdot \log_2 e` 把 `exp` 折成 `exp2` 的指令优化。本讲只动其中的 `MMA0` 与 `Softmax` 两段，不再重复在线 softmax 的完整推导。
- **因果注意力（causal / masked / autoregressive attention）**：query 在位置 \(q\) 只能看 key 位置 \(k \le q\)。GPT 这类自回归模型必须如此，否则就「偷看未来」。数学上就是把分数矩阵 \(S = QK^{\top}\) 中 \(k>q\) 的上三角置成 \(-\infty\)，softmax 后这些位置权重为 0。
- **\(-\infty\) 在 softmax 里的作用**：\(\exp(-\infty)=0\)，被置 \(-\infty\) 的位置不参与加权平均。但前提是「同一行里至少有一个有限值」，否则会出现 \(\exp(-\infty-(-\infty))\) 之类的 NaN——这正是第 4.4 节 `Check_inf` 要处理的隐患。
- **TileLang 三件套原语**：`T.if_then_else(cond, true_val, false_val)` 是三目运算的张量/标量版本；`T.infinity(dtype)` 返回该 dtype 的正无穷，取负即 \(-\infty\)；`T.Parallel(...)` 标注可并行循环。

## 3. 本讲源码地图

本讲只读一个文件，但聚焦其中四处：

| 位置 | 作用 |
|---|---|
| `MMA0` 宏里的 `if is_causal:` 分支 | 把因果掩码写进分数块 `acc_s` |
| `past_len = seq_kv - seq_q` | 处理 `seq_kv > seq_q`（带历史 KV）时 query 的绝对位置偏移 |
| `main` 里的 `loop_range` | 按 query block 裁剪 KV 循环上界 |
| `Softmax` 宏里被注释掉的 `Check_inf` 段 | 防御「整行 \(-\infty\)」导致 NaN |

参考实现（用来对答案）在 `ref_program` 里用 `torch.tril + masked_fill(-inf)` 实现同一掩码，方便对照「声明式 DSL」与「PyTorch 朴素写法」。

全程文件：`hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py`

永久链接基址：`https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/`

## 4. 核心概念与源码讲解

### 4.1 is_causal：因果注意力与掩码语义

#### 4.1.1 概念说明

`is_causal` 是 `flashattn(...)` 的一个布尔参数，从命令行 `--is_causal` 传入（不传则为 `False`）。它决定内核是否屏蔽分数矩阵的上三角。它会在三个地方生效：

1. **掩码生成**（`MMA0`）：决定 `acc_s` 每个格子是 0（保留）还是 \(-\infty\)（屏蔽）。
2. **循环裁剪**（`main` 的 `loop_range`）：决定每个 query block 要迭代多少个 KV 块。
3. **算量统计**（`__main__`）：causal 时总 FLOPS 乘 0.5，因为大约一半的分数被屏蔽、无需真正算。

一个关键概念是 **绝对位置**。当 `seq_kv > seq_q`（带历史 KV 的场景）时，query 不是从位置 0 开始，而是排在一段「过去」token 之后。代码用 `past_len = seq_kv - seq_q` 表示这段过去的长度，query 的绝对位置要加上它。本基准套件测试的 prefill shape（`1024×1024`、`8192×8192`）满足 `seq_q = seq_kv`，故 `past_len = 0`；解码 shape（`seq_q = 1`）见 `benchmark_torch.sh`，但该脚本并不传 `--is_causal`。

#### 4.1.2 核心流程

`is_causal` 标志在内核里的传播路径：

```
命令行 --is_causal
   └─> flashattn(..., is_causal, ...)          # 闭包捕获
         ├─> MMA0: if is_causal: 给 acc_s 上三角写 -inf   else: T.clear(acc_s)
         └─> main:  loop_range = min(全 KV 块数, 对角线块数)  仅当 is_causal
```

参考实现 `ref_program` 用 PyTorch 朴素写法做同一件事，便于核对数值：

```python
mask = torch.tril(torch.ones(seq_q, seq_kv))   # 下三角为 1
scores = scores.masked_fill(mask == 0, float('-inf'))
```

二者等价：`torch.tril` 保留下三角（\(k \le q\)），把上三角填 \(-\infty\)；DSL 版则是在 kernel 内逐格判断 `q_idx >= k_idx`。

#### 4.1.3 源码精读

`is_causal` 作为参数贯穿整个 `flashattn` 闭包，并参与算量折半：

- [benchmark_tilelang_mha.py:30-31](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L30-L31)：`flashattn` 的签名含 `is_causal`，并把 `scale` 预折 \(\log_2 e\)（u5-l16 已讲）。`is_causal` 被闭包捕获，下面的 macro 与 `loop_range` 都直接引用它。
- [benchmark_tilelang_mha.py:198-198](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L198)：命令行 `--is_causal` 用 `action='store_true'`，默认 `False`。
- [benchmark_tilelang_mha.py:204-205](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L204-L205)：causal 时 `total_flops *= 0.5`，因为上三角约一半运算被屏蔽（精确说是近似一半，对长序列趋于 0.5）。
- [benchmark_tilelang_mha.py:180-185](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L180-L185)：参考实现用 `torch.tril` + `masked_fill(mask==0, -inf)` 构造同样的因果掩码，作为正确性对照。

#### 4.1.4 代码实践

**实践目标**：把「命令行开关」与「内核里三处生效点」串起来。

**操作步骤**：

1. 在 [benchmark_tilelang_mha.py:198](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L198) 找到 `--is_causal` 的定义，确认默认值。
2. 全文搜索 `is_causal`，列出它在 `flashattn` 内部出现的所有位置（应有 `MMA0` 分支、`loop_range` 两处）。
3. 对照 [benchmark_tilelang_mha.py:180-185](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L180-L185) 的 `ref_program`，确认 DSL 的「逐格判断」与 PyTorch 的「整张掩码」在 `seq_q = seq_kv` 时等价。

**需要观察的现象**：`is_causal` 同时改变了「算什么」（掩码）、「算多少」（循环）、「怎么报」（FLOPS 折半）三件事。

**预期结果**：三处引用一一对应；`ref_program` 在 `seq_q = seq_kv` 时与 kernel 数值一致。

> 若手边有 GPU，可运行 `python benchmark_tilelang_mha.py --seq_q 8192 --seq_kv 8192 --is_causal` 与去掉 `--is_causal` 两次，对比 latency。预期 causal 的 latency 显著低于非 causal（约为一半量级，因迭代块数减半）。无 GPU 时标记**待本地验证**。

#### 4.1.5 小练习与答案

**Q1**：`--is_causal` 不传时，`is_causal` 的值是什么？内核会走哪个分支？
**答**：`action='store_true'` 使默认为 `False`；`MMA0` 走 `else: T.clear(acc_s)`，`loop_range` 走「全 KV 块数」分支，即标准（非因果）全注意力。

**Q2**：为什么 causal 时 `total_flops *= 0.5` 是「近似」而非精确？
**答**：因果掩码屏蔽的是严格上三角，元素数恰为 \(N(N-1)/2\)，占比 \((N-1)/(2N)\)，对长序列趋近 0.5；项目用 0.5 作工程近似。

---

### 4.2 T.if_then_else 与 T.infinity：把掩码写进 acc_s

#### 4.2.1 概念说明

u5-l16 讲过，`MMA0` 先把 K 块搬进 `K_shared`，再 `T.gemm(Q_shared, K_shared, acc_s, transpose_B=True)` 算 \(S_k = QK_k^{\top}\)。注意 `T.gemm` 做的是**累加** `acc_s += QK^{\top}`。因果掩码就利用了这一点：**在 gemm 之前**先把 `acc_s` 的每个格子预设成 0（保留）或 \(-\infty\)（屏蔽），gemm 累加后：

- 保留格：\(0 + (QK^{\top})_{ij} = (QK^{\top})_{ij}\)，正常分数。
- 屏蔽格：\(-\infty + (QK^{\top})_{ij} = -\infty\)，仍为 \(-\infty\)。

这样无需在 gemm 之后再扫一遍写掩码，把「掩码」与「矩阵乘」天然合一。判断条件就是因果关系：query 绝对位置 \(q\) 能看到 key 绝对位置 \(k\) 当且仅当 \(q \ge k\)。

#### 4.2.2 核心流程

`MMA0` 内 causal 分支的执行步骤：

```
past_len = seq_kv - seq_q
T.copy(K[第 k 个 KV 块], K_shared)              # 取 K 块
if is_causal:
    并行遍历 (i, j) ∈ block_M × block_N:
        q_idx = bx*block_M + i + past_len      # query 绝对位置
        k_idx = k*block_N + j                   # key 绝对位置
        acc_s[i,j] = (q_idx >= k_idx) ? 0 : -infinity
else:
    T.clear(acc_s)                              # 全 0
T.gemm(Q_shared, K_shared, acc_s, transpose_B=True)   # acc_s += Q K^T
```

两个绝对位置的取法是关键：

- \(q\_idx = bx\cdot block\_M + i + past\_len\)：第 `bx` 个 query block、块内第 `i` 行，加上历史偏移 `past_len`。
- \(k\_idx = k\cdot block\_N + j\)：第 `k` 个 KV block、块内第 `j` 列（key 无偏移，从 0 起）。

#### 4.2.3 源码精读

- [benchmark_tilelang_mha.py:50-51](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L50-L51)：`past_len = seq_kv - seq_kv`，并 `T.copy` 取第 k 个 K 块进 `K_shared`。
- [benchmark_tilelang_mha.py:52-56](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L52-L56)：causal 分支主体。`T.Parallel(block_M, block_N)` 并行遍历块内每个格子，算 `q_idx`、`k_idx`，用 `T.if_then_else(q_idx >= k_idx, 0, -T.infinity(acc_s.dtype))` 写掩码。注意取的是 `acc_s.dtype`（累加精度 `float`）的无穷。
- [benchmark_tilelang_mha.py:57-58](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L57-L58)：非 causal 分支直接 `T.clear(acc_s)`（全 0，即不屏蔽）。
- [benchmark_tilelang_mha.py:59-59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L59-L59)：`T.gemm(..., transpose_B=True)` 累加 \(QK^{\top}\)。因 `transpose_B=True`，B 共享内存按 `[block_N, dim]` 存、gemm 时转置，省一次显式转置拷贝。

#### 4.2.4 代码实践

**实践目标**：验证「先写掩码、再 gemm 累加」等价于「先 gemm、再用掩码覆盖」。

**操作步骤**：

1. 读 [benchmark_tilelang_mha.py:52-59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L52-L59)，把 `acc_s[i,j]` 的初值与 gemm 的累加语义写成等式。
2. 在草稿纸上对一个 \(2\times 2\) 的小块手算：设 \(QK^{\top} = \begin{bmatrix}1&2\\3&4\end{bmatrix}\)，causal 掩码使上三角为 \(-\infty\)，写出 gemm 后的 `acc_s`。

**需要观察的现象**：屏蔽格无论 \(QK^{\top}\) 多大，结果都是 \(-\infty\)；保留格等于原分数。

**预期结果**：\(\mathrm{acc\_s} = \begin{bmatrix}1&-\infty\\3&4\end{bmatrix}\)（第 0 行 query 只能看到第 0 个 key）。

#### 4.2.5 小练习与答案

**Q1**：为什么把掩码写在 `T.gemm` **之前**，而不是之后？
**答**：gemm 是累加。之前写 \(-\infty\)，累加有限值后仍为 \(-\infty\)（\(-\infty + x = -\infty\)）；若写在之后则要额外扫一遍覆盖，多一次访存。

**Q2**：`-T.infinity(acc_s.dtype)` 为什么取 `acc_s.dtype` 而不是 `dtype`？
**答**：`acc_s` 是累加缓冲，精度为 `accum_dtype`（`float`）；\(-\infty\) 必须与它同类型，避免类型转换把无穷变成有限值。

---

### 4.3 loop_range 循环裁剪：跳过全掩码块

#### 4.3.1 概念说明

光给上三角写 \(-\infty\) 还不够省——如果 KV 循环仍跑满全部块，那些「整块都是 \(-\infty\)」的 KV 块会白白做一次 `T.gemm` 和 softmax。`loop_range` 就是用来**按 query block 裁剪 KV 循环上界**：第 `bx` 个 query block 只需迭代到「对角线」所在的 KV 块，再往后的块对它全是屏蔽的，直接不进循环。

直觉：query block `bx` 里最高的 query 行在绝对位置约 \((bx+1)\cdot block\_M\)（方阵情形 `past_len=0`），它最多只能看到这个位置之前的 key。所以需要的 KV 块数是 \(\lceil (bx+1)\cdot block\_M / block\_N \rceil\)，与「全部 KV 块数」\(\lceil \mathrm{seq\_kv}/block\_N \rceil\) 取最小。

#### 4.3.2 核心流程

`loop_range` 的取法（方阵 `seq_q = seq_kv`，即 `past_len = 0`）：

\[
\mathrm{loop\_range} = \min\!\left(\left\lceil \frac{\mathrm{seq\_kv}}{block\_N}\right\rceil,\ \left\lceil \frac{(bx+1)\cdot block\_M}{block\_N}\right\rceil\right)
\]

- 左项 \(\lceil \mathrm{seq\_kv}/block\_N\rceil\)：KV 序列总共分成多少块（非 causal 时就只取这一项）。
- 右项 \(\lceil (bx+1)\cdot block\_M/block\_N\rceil\)：query block `bx` 的对角线落在第几个 KV 块。
- 取 `min`：query block 越靠后（`bx` 越大），需要迭代的 KV 块越多；最后一个 query block 几乎要跑满，第一个 query block 只需 1 块。

当 `block_M = block_N`（本基准的配置，二者均为 128）时，右项化简：

\[
\left\lceil \frac{(bx+1)\cdot block\_M}{block\_N}\right\rceil = bx+1
\]

即第 `bx` 个 query block 只迭代 `k = 0..bx`，恰好跳过所有「对该 query block 全屏蔽」的上三角块。整个序列的 KV 块访问总数约为非 causal 的一半，这是 causal 注意力加速的物理来源之一（另一半来自 FLOPS 折半）。

#### 4.3.3 源码精读

- [benchmark_tilelang_mha.py:139-141](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L139-L141)：`loop_range` 定义。causal 取 `min(全 KV 块数, 对角线块数)`，非 causal 取「全 KV 块数」。注意右项用的是 `(bx+1)*block_M`，**不含 `past_len`**。
- [benchmark_tilelang_mha.py:143-147](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L143-L147)：`for k in T.Pipelined(loop_range, num_stages=...)` 用裁剪后的上界驱动 KV 循环，并配软件流水（u5-l16 / u3-l9 已讲）。

> **小心：适用范围**。`loop_range` 右项不含 `past_len`，这在 `seq_q = seq_kv`（prefill 方阵，`past_len = 0`）下精确。本基准的 prefill shape（`1024×1024`、`8192×8192`，见 `benchmark_torch.sh`）正是此情形。但对解码 shape（`seq_q = 1, seq_kv = 1024`，`past_len = 1023`），此式会严重少迭代（单个 query 应看全部 key，却被裁成 1 块），故解码 shape 在 `benchmark_torch.sh` 中**不传** `--is_causal`。若要用 causal 解码，需自行调整 `loop_range`（待本地验证其取舍）。

#### 4.3.4 代码实践

**实践目标**：写出 causal 时第 `bx` 个 query block 的 `loop_range`，并感受裁剪带来的省功。

**操作步骤**：

1. 取 `block_M = block_N = 128`、`seq_q = seq_kv = 8192`。计算 query block 总数 \(N_q = \lceil 8192/128\rceil = 64\)，KV block 总数 \(N_{kv} = 64\)。
2. 对 `bx = 0, 1, 2, 63`，用 \(\min(64,\ bx+1)\) 算出各自的 `loop_range`。
3. 把 64 个 query block 的 `loop_range` 求和，与非 causal 的 \(64\times 64 = 4096\) 块次对比。

**需要观察的现象**：`bx` 越小，迭代越少；总和约为非 causal 的一半。

**预期结果**：`bx=0→1`，`bx=1→2`，`bx=2→3`，`bx=63→64`；总和 \(\sum_{bx=0}^{63}(bx+1) = 64\times 65/2 = 2080\) 块次，约为 4096 的一半。

#### 4.3.5 小练习与答案

**Q1**：`loop_range` 为什么要对左项 \(\lceil \mathrm{seq\_kv}/block\_N\rceil\) 取 `min`？
**答**：防止右项超过 KV 实际块数（例如最后一个 query block 的对角线可能越过 KV 末端），`min` 保证不越界访问。

**Q2**：把 `block_M` 改成 256、`block_N` 保持 128，第 `bx=0` 个 query block 的 `loop_range` 是多少？
**答**：\(\min(\lceil \mathrm{seq\_kv}/128\rceil,\ \lceil 1\cdot 256/128\rceil) = \min(N_{kv}, 2) = 2\)。此时一个 query block 横跨 2 个 KV 块的对角线区域——这正是 4.4 节 `Check_inf` 可能变得必要的情形。

---

### 4.4 Check_inf：当整行都是 -∞

#### 4.4.1 概念说明

4.2 节说 \(-\infty\) 经 `exp2` 会变成 0。这有一个隐含前提：**该行至少有一个有限值**，使得 `scores_max`（行最大值）是有限数。`exp2` 的减法以 `scores_max` 为基准：

\[
\mathrm{acc\_s}[i,j] \leftarrow \exp_2\!\big(\mathrm{acc\_s}[i,j]\cdot scale - \mathrm{scores\_max}[i]\cdot scale\big)
\]

- 屏蔽格 \(\mathrm{acc\_s}=-\infty\)、`scores_max` 有限：\(\exp_2(-\infty - \mathrm{有限}) = \exp_2(-\infty) = 0\)。✅
- 但若**整行都是 \(-\infty\)**，则 `scores_max[i] = -\infty`，于是 \(\exp_2(-\infty - (-\infty)) = \exp_2(\mathrm{NaN}) = \mathrm{NaN}\)，NaN 随后污染 `scores_sum`、`logsum`、`acc_o`，整个 softmax 报废。

`Check_inf` 就是这个隐患的防御：若 `scores_max` 是 \(-\infty\)，就把它**归零**，让该行退化成「全 0 权重」的空操作，避免 NaN。代码注释指出这招来自 FlashAttention-3。

#### 4.4.2 核心流程

`Softmax` 宏里 `scores_max` 的更新与（被注释掉的）Check_inf：

```
T.copy(scores_max, scores_max_prev)        # 保存上一轮最大值
T.fill(scores_max, -infinity)              # 初始化为 -inf
T.reduce_max(acc_s, scores_max, dim=1, clear=False)   # 求行最大（累加到初值上）
# === 下面这段 Check_inf 被注释掉 ===
# for i in T.Parallel(block_M):
#     scores_max[i] = if scores_max[i] == -inf then 0 else scores_max[i]
scores_scale[i] = exp2(scores_max_prev*scale - scores_max*scale)
acc_s[i,j]      = exp2(acc_s[i,j]*scale - scores_max[i]*scale)
```

为什么这段在本内核里**被注释掉**？因为 4.3 节的 `loop_range` 裁剪配合 `block_M = block_N = 128`，已经保证了「每个被迭代的 KV 块里，没有任何一行是全 \(-\infty\)」。

证明（方阵 `past_len = 0`，`block_M = block_N = B`）：query block `bx` 迭代 `k = 0..bx`（因 `loop_range = bx+1`）。对块 `k ≤ bx`，其首个 key 在 \(k\cdot B\)，query 行 \(i\) 的绝对位置 \(bx\cdot B + i\)。整行屏蔽需要 \(bx\cdot B + i < k\cdot B\)，即 \(i < (k-bx)\cdot B \le 0\)，对 \(i \ge 0\) 不可能。故无整行 \(-\infty\)，`scores_max` 恒有限，`Check_inf` 不需要。

那什么时候会需要？当 `block_M > block_N` 时，一个 query block 的高度横跨多个 KV 块的宽度，对角线区域的最后一个 KV 块里会出现「上半部分整行屏蔽」的行——此时 `scores_max` 可能为 \(-\infty\)，必须 `Check_inf`。这与注释里 FA3 的说法一致：只需在最初的 \(\lceil block\_M / block\_N\rceil\) 个 KV 块做检查（即对角线交叠区）。

#### 4.4.3 源码精读

- [benchmark_tilelang_mha.py:85-86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L85-L86)：先把 `scores_max` 填 \(-\infty\)，再 `T.reduce_max(acc_s, scores_max, dim=1, clear=False)` 求行最大（`clear=False` 表示在初值上累加，故初值必须已是 \(-\infty\) 才不影响结果）。
- [benchmark_tilelang_mha.py:87-91](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L87-L91)：`Check_inf` 注释与被注释掉的代码。注释说明：做 causal softmax 时，若 `scores_max` 为 \(-\infty\) 要置 0；FA3 称之为 `Check_inf`，且只需在前 \(\lceil kBlockM/kBlockN\rceil\) 步做。
- [benchmark_tilelang_mha.py:92-99](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L92-L99)：`scores_scale` 与 `acc_s` 的 `exp2` 计算。若 `scores_max` 是 \(-\infty\)，这两步都会产生 NaN——这就是 `Check_inf` 要堵的漏洞。

#### 4.4.4 代码实践

**实践目标**：理解 `Check_inf` 在什么配置下「必需」、在本配置下「可省」。

**操作步骤**：

1. 读 [benchmark_tilelang_mha.py:87-99](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L87-L99)，把 `scores_max = -inf` 时 `acc_s[i,j]` 的计算式写出来，确认会得到 NaN。
2. 思考实验（不改源码）：若把 [benchmark_tilelang_mha.py:15-16](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L15-L16) 的 `block_M` 改成 256、`block_N` 保持 128 并启用 `--is_causal`，预测 `bx=0` 的对角线块里哪些行会出现 `scores_max = -inf`。

**需要观察的现象**：`block_M = block_N` 时无整行 \(-\infty\)；`block_M > block_N` 时对角线块的上半部分行会全 \(-\infty\)。

**预期结果**：`block_M=256, block_N=128, bx=0` 时，块 `k=1`（key 在 \([128,256)\)）里 query 行 \(i = 0..127\)（绝对位置 \(0..127\)）全部 \(< 128\)，故整行屏蔽，`scores_max` 为 \(-\infty\)；若不启用 `Check_inf` 会出 NaN。本配置（128/128）下则安全。

#### 4.4.5 小练习与答案

**Q1**：`T.reduce_max(..., clear=False)` 为什么要求初值是 \(-\infty\)？
**答**：`clear=False` 表示在缓冲现有值上取 max 而非先清零；若初值是 0，则全是负分数的行最大值会被错误地抬到 0。初值 \(-\infty\) 才是 max 运算的单位元。

**Q2**：为什么 `Check_inf` 把 \(-\infty\) 归成 0，而不是别的值？
**答**：归 0 后，屏蔽格 \(\exp_2(-\infty - 0) = 0\)，该行所有权重为 0、对 `acc_o` 贡献为 0，且 `scores_scale = exp2(prev - 0)` 有限，整条计算保持有限且语义正确（空行不作贡献）。

---

## 5. 综合实践

把本讲三件事（掩码、裁剪、防 NaN）串起来，做一次「配置敏感性」分析：

1. **基线**：默认配置 `block_M = block_N = 128`、`seq_q = seq_kv = 8192`、`--is_causal`。在 [benchmark_tilelang_mha.py:139-141](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L139-L141) 旁注出 `bx=0..63` 各自的 `loop_range`，求和确认约为非 causal 的一半。
2. **掩码追踪**：在 [benchmark_tilelang_mha.py:52-59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L52-L59) 标出 `q_idx / k_idx / if_then_else / gemm` 四步，解释为何 gemm 后屏蔽格仍是 \(-\infty\)。
3. **防 NaN 论证**：在 [benchmark_tilelang_mha.py:87-91](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L87-L91) 写一段话，说明本配置下 `loop_range` 已消除整行 \(-\infty\)，故 `Check_inf` 可注释。
4. **挑战**：若要让本内核支持 `seq_q = 1` 的 causal 解码（`past_len = seq_kv - 1`），指出 [benchmark_tilelang_mha.py:139-141](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L139-L141) 的 `loop_range` 该如何修改（提示：右项应反映 query 的真实绝对位置 \((bx+1)\cdot block\_M + past\_len\)），并讨论此时是否仍需 `Check_inf`。

> 步骤 4 涉及修改建议，仅作设计层面讨论，**不修改源码**。其运行效果待本地验证。

## 6. 本讲小结

- **`is_causal`** 是贯穿三处的开关：`MMA0` 的掩码、`main` 的 `loop_range`、`__main__` 的 FLOPS 折半。
- **掩码机制**：在 `T.gemm` **之前**用 `T.if_then_else(q_idx >= k_idx, 0, -T.infinity(...))` 给 `acc_s` 预置 0 或 \(-\infty\)，靠 gemm 的累加语义让屏蔽格保持 \(-\infty\)。
- **绝对位置**：query 用 `bx*block_M + i + past_len`，key 用 `k*block_N + j`；`past_len = seq_kv - seq_q` 处理带历史 KV 的情形。
- **循环裁剪**：`loop_range = min(全 KV 块数, ⌈(bx+1)·block_M/block_N⌉)`，方阵 + `block_M=block_N` 时退化为 `bx+1`，跳过所有全屏蔽块，省约一半功。
- **\(-\infty \to 0\)**：`exp2(-∞ - 有限) = 0`，前提是行内有有限值使 `scores_max` 有限。
- **Check_inf**：整行 \(-\infty\) 会使 `scores_max = -∞`，导致 `exp2(NaN)` 污染；本配置靠 `loop_range` 已消除该情形，故注释掉；`block_M > block_N` 时则需启用。

## 7. 下一步学习建议

- **本单元收尾**：本讲完成了 FlashAttention 因果分支的细节。建议接着读 [u5-l18 tilelang.compile 评估与正确性校验](./u5-l18-compile-profiler-and-correctness.md)，看 `ref_program`（本讲用作对照的 `torch.tril` 参考实现）如何配合 `assert_close` 校验因果掩码的数值正确性。
- **进阶**：若对「分段 + 合并」感兴趣，可预习 [u6-l20 MLA decode：split-KV 并行与 combine](./u6-l20-mla-decode-split-combine.md)，那里的 `combine` 用 `log2/exp2` 合并多 split 的 logsum，与本章的 `logsum` 重缩放同源。
- **源码延伸**：回看 [u5-l16](./u5-l16-flashattention-online-softmax.md) 的四段 macro，把本讲的 `MMA0` 因果分支与 `Softmax` 的 `Check_inf` 注释嵌回完整流程，形成对 causal FlashAttention 的整体印象。
