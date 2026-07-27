# FlashAttention：在线 softmax 与 macro 结构

> 本讲对应讲义 id：`u5-l16`，依赖 `u3-l9`（块级 GEMM 内核解剖）。
> 唯一关键源码：`hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py`。

## 1. 本讲目标

本讲是「Attention 系列」单元的第一篇。读完本讲，你应当能够：

1. 说清标准注意力 \(\mathrm{softmax}(QK^\top/\sqrt{d})V\) 为什么不能直接在 GPU 上「一次性」算，以及 FlashAttention 用「分块 + 在线 softmax」解决的是什么问题。
2. 读懂 TileLang 内核里 `@T.macro` 拆出的四段——`MMA0 / Softmax / Rescale / MMA1`——各自做什么、为什么这样切，以及它们在 K 维流水循环里的调用顺序。
3. 写出「在线 softmax」的递推公式：`scores_max_prev / scores_max / scores_scale / scores_sum / logsum` 这五个 `[block_M]` 小向量各自的角色，以及为什么最后一步 `acc_o /= logsum` 能得到精确的 softmax 结果。
4. 解释代码里把 `exp` 改写成 `exp2`、并把 \(\log_2 e\) 预先折进 `scale` 的指令优化原理。
5. 区分 `T.SharedBuffer` 与 `T.FragmentBuffer`，并理解 `acc_o`（fragment，float）必须先回写到 `O_shared`（shared，fp16）再写回全局 `Output` 的两步搬运。

本讲**不**展开 causal 掩码、`loop_range` 循环裁剪与 `Check_inf` 的 `-inf` 处理——这些细节代码里同样存在，但属于下一讲 `u5-l17` 的主题。本讲只把它们当作「掩码把某些分数置成 \(-\infty\)」一笔带过。

## 2. 前置知识

### 2.1 注意力是什么（一句话复习）

给定 query 矩阵 \(Q\)、key 矩阵 \(K\)、value 矩阵 \(V\)（形状都是 `[batch, heads, seq, dim]`），标准注意力输出是：

\[
\mathrm{Attn}(Q,K,V) = \mathrm{softmax}\!\left(\frac{QK^\top}{\sqrt{d}}\right) V
\]

它由两次矩阵乘法夹一次 softmax 构成：

1. \(S = QK^\top/\sqrt{d}\)（分数矩阵，`[seq_q, seq_kv]`）；
2. \(P = \mathrm{softmax}(S)\)（按行做 softmax，每行和为 1）；
3. \(O = PV\)（加权求和，`[seq_q, dim]`）。

本讲内核对应这两次乘法就是 `MMA0`（算 \(S\)）与 `MMA1`（算 \(O\)），夹在中间的 `Softmax / Rescale` 把「softmax」这一步拆成了分块可计算的在线形式。

### 2.2 为什么需要 FlashAttention（要解决什么）

朴素实现要把整张 \(S\)（`[seq_q, seq_kv]`，例如 `8192×8192`）物化到显存（HBM），再读回来做 softmax，再读回来乘 \(V\)。显存带宽是瓶颈，且 \(S\) 占用巨大。FlashAttention 的核心思路是：

- 把 \(K, V\) 沿序列维切成块（block），每次只把一小块 \(K_k, V_k\) 读进共享内存；
- 在寄存器/共享内存里**增量**地累加 softmax 所需的统计量（行最大值、行和），**绝不**把整张 \(S\) 写回显存；
- 这样把 \(O(n^2)\) 的中间矩阵 \(S\) 从 HBM 流量里消掉，用片上存储换取带宽。

代价是：softmax 的分母 \(\sum_j e^{s_j}\) 依赖**整行**的最大值，分块计算时这个全局最大值是未知的，于是需要「在线 softmax」——一边处理新块、一边用新信息**修正**已经累加的结果。这正是本讲要讲清的数学。

### 2.3 承接 u3-l9：块级 GEMM 五要素

u3-l9 讲过 TileLang 块级 GEMM 的「五要素」：`T.Kernel` 网格映射、`alloc_shared/alloc_fragment` 分配片上存储、`T.copy` 在全局↔shared↔fragment 间搬运、`T.gemm` 做 TensorCore 乘加、`T.Pipelined` 做软件流水。本讲里这些要素**原封不动**地出现，只是从「一次 GEMM」变成了「夹着在线 softmax 的两次 GEMM」。如果你对 `T.copy` 的「全局→shared→fragment→shared→全局」闭环、以及 `acc` 为何要先清零还不够熟，建议先回看 u3-l9。

## 3. 本讲源码地图

本讲只引用**一个**文件，但会反复引用它的不同区段。

| 文件 | 作用 |
|---|---|
| [`benchmark_tilelang_mha.py`](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py) | hopper 架构下 FlashAttention 的 TileLang 内核。`get_configs`（L14-L27）只放了一个手调配置；`flashattn`（L30-L173）把内核拆成 4 个 `@T.macro` + 1 个 `@T.prim_func main`；`ref_program`（L176-L188）是 torch 实现的参考答案。本讲聚焦 4 个 macro 与 `main` 里的 K 维循环。 |

> ⚠️ **读源码以实际文件为准（承接前几讲的反复提醒）**：本目录下的 `benchmark_torch.sh`（[见此](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_torch.sh)）调用的是 `benchmark_torch_mha.py`，但那个文件实际在**同级目录** `0.torch_benchmark/` 里，不在本目录。本目录真正的 TileLang 内核入口是 `benchmark_tilelang_mha.py`。运行脚本和内核文件张冠李戴是本项目常见历史遗留，定位文件时以 `ls` 为准，不要盲信脚本字面量。

---

## 4. 核心概念与源码讲解

本讲按 5 个最小模块展开：

- **4.1** `T.macro`：把内核拆成可复用的四段
- **4.2** MMA0 与 MMA1：\(QK^\top\) 与 \(PV\) 两次 `T.gemm`
- **4.3** online softmax 与 `reduce_max / reduce_sum`
- **4.4** `exp2` / `logsum`：用 \(2^x\) 替代 \(e^x\) 的指令优化
- **4.5** 片上缓冲（Fragment / Shared buffer）与回写顺序

### 4.1 `T.macro`：把内核拆成可复用的四段

#### 4.1.1 概念说明

一个 FlashAttention block 的计算其实有清晰的「四步循环」：取一块 \(K\)、算分数（`MMA0`）；对分数做 softmax（`Softmax`）；把旧的输出累加值按新尺度缩放（`Rescale`）；取一块 \(V\)、加权累加（`MMA1`）。如果把这四步全挤在 `main` 的 K 循环里写，代码会变成几百行难读的面条。

`@T.macro` 是 TileLang 提供的「内核子程序」：用 `@T.macro` 装饰一个函数，在 `main` 里像普通函数一样调用它（`MMA0(...)`），编译器会把宏体**内联（inline）**展开到调用处。它和普通函数的关键区别在于：宏的形参可以带**带类型的缓冲标注**（`T.SharedBuffer` / `T.FragmentBuffer`），用来声明「这一段计算需要哪一块片上存储、在哪个存储空间」。

> 直觉：`@T.macro` ≈ 带类型签名的「可内联代码块」，作用是**让内核像拼积木一样可读**，运行时没有函数调用开销。

#### 4.1.2 核心流程

四个 macro 在 K 维循环里的调用顺序（[L148-L152](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L148-L152)）：

```
for k in T.Pipelined(loop_range, num_stages=2):
    MMA0(...)      # 算当前块的分数 S_k = Q · K_k^T，写入 acc_s
    Softmax(...)   # 对 acc_s 做在线 softmax，更新 scores_max/logsum，产出 acc_s_cast
    Rescale(...)   # 把旧输出 acc_o 缩放到新尺度
    MMA1(...)      # 累加当前块贡献 acc_o += acc_s_cast · V_k
```

注意顺序不是任意的：`Rescale` 必须在 `MMA1` **之前**（4.5 节会解释为什么）。

四个 macro 的分工一览：

| macro | 输入 | 输出 | 数学含义 |
|---|---|---|---|
| `MMA0` | `Q_shared, K_shared` | `acc_s` | \(S_k = Q \cdot K_k^\top\)（非 causal 时先 `clear`，causal 时按掩码置 \(-\infty\)） |
| `Softmax` | `acc_s` + 5 个统计向量 | `acc_s_cast, scores_max, logsum, ...` | 在线 softmax：更新行最大值、行和，把分数压成概率 |
| `Rescale` | `acc_o, scores_scale` | `acc_o` | 把旧累加输出按 \(\exp(M_{k-1}-M_k)\) 缩放 |
| `MMA1` | `acc_s_cast, V_shared` | `acc_o` | \(O \mathrel{+}= P_k \cdot V_k\) |

#### 4.1.3 源码精读

四个宏的定义紧挨在一起，先给整体位置：

- `MMA0`：[benchmark_tilelang_mha.py:39-59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L39-L59)
- `MMA1`：[benchmark_tilelang_mha.py:61-72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L61-L72)
- `Softmax`：[benchmark_tilelang_mha.py:74-103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L74-L103)
- `Rescale`：[benchmark_tilelang_mha.py:105-111](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L105-L111)

注意每个宏的形参都带类型标注。以 `MMA0` 为例（[L39-L49](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L39-L49)）：

```python
@T.macro
def MMA0(
    K: T.Tensor(kv_shape, dtype),                       # 全局张量，按引用传入
    Q_shared: T.SharedBuffer([block_M, dim], dtype),    # 要求一块 shared 内存
    K_shared: T.SharedBuffer([block_N, dim], dtype),    # 要求一块 shared 内存
    acc_s: T.FragmentBuffer([block_M, block_N], accum_dtype),  # 要求一块 fragment(寄存器)
    k: T.int32, bx: T.int32, by: T.int32, bz: T.int32, # 普通标量索引
):
```

这些带类型标注的形参，在 `main` 里调用时（[L148](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L148)）会绑定到 `main` 用 `T.alloc_shared / T.alloc_fragment` 申请的缓冲（见 4.5 节）。`SharedBuffer` 形参只能接 `alloc_shared` 的实参，`FragmentBuffer` 形参只能接 `alloc_fragment` 的实参——这就是宏签名声明的「存储空间契约」。

> 「以代码为准」小提示：宏体里出现了 causal 分支（`if is_causal`）和被注释掉的 `Check_inf`，本讲先忽略，它们属于 u5-l17。

#### 4.1.4 代码实践

**实践目标**：确认「宏是内联展开、无独立生命」这一性质。

**操作步骤**（源码阅读型）：

1. 打开 [benchmark_tilelang_mha.py:113-158](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L113-L158)（`main` 的 `@T.prim_func`）。
2. 在 `main` 里数一下：`acc_s`、`acc_o`、`scores_max` 等缓冲是在哪里 `alloc` 的？它们是**全局变量**还是**每个 block 私有**的？
3. 观察四个宏的调用（L148-L152）传给宏的实参，是不是正好就是 `main` 里 `alloc` 出来的那些缓冲？

**需要观察的现象 / 预期结果**：四个宏形参里声明的所有缓冲，在 `main` 里都恰好 `alloc` 了一次（L121-L132），并在循环里反复复用——宏本身不 `alloc`，只是「借用」`main` 的缓冲。这正是「内联」的含义：宏没有自己的栈帧，它的存储全部来自调用方。

#### 4.1.5 小练习与答案

**Q1**：如果把 `Softmax` 宏里对 `logsum` 的更新（L102）删掉，最终结果会错在哪一步？

**答**：`logsum` 是 softmax 的分母（行和）。删掉更新会让 `logsum` 恒为初值 0，循环结束后 `acc_o /= logsum` 就是除以 0，结果变成 NaN/inf。即使不报错，数值也完全错误。

**Q2**：`MMA0` 的形参 `K` 标注为 `T.Tensor(kv_shape, dtype)`，而 `K_shared` 标注为 `T.SharedBuffer`。两者在「存储位置」上的区别是什么？

**答**：`T.Tensor` 是**全局显存**张量（在 HBM），宏只能从它读、向它写（经 `T.copy` 搬运）；`T.SharedBuffer` 是**片上共享内存**（shared memory），位于 SM 内、带宽高、延迟低。`MMA0` 的第一件事就是把 `K`（全局）`T.copy` 进 `K_shared`（共享），这正是 u3-l9 讲过的「全局→shared」搬运。

---

### 4.2 MMA0 与 MMA1：两次 `T.gemm`

#### 4.2.1 概念说明

FlashAttention 内核里只有两次真正的密集矩阵乘，分别对应注意力公式的两步：

- **MMA0**：\(S_k = Q \cdot K_k^\top\)（query 与 key 的相似度，`[block_M, block_N]`）；
- **MMA1**：\(O \mathrel{+}= P_k \cdot V_k\)（用概率加权 value，`[block_M, dim]`）。

两次都用 `T.gemm`，背后都走 TensorCore 的 MMA 指令。但它们的转置与累加语义不同，需要看仔细。

#### 4.2.2 核心流程

```
MMA0:  acc_s  = Q_shared @ K_shared^T      # transpose_B=True，acc_s 先清零/置 -inf
MMA1:  acc_o += acc_s_cast @ V_shared       # 默认累加(acc_o += )，不转置
```

两个关键差异：

1. **转置**：\(K\) 在共享内存里按 `[block_N, dim]` 存放，但 \(QK^\top\) 需要的是 \(K\) 的转置，所以 `MMA0` 用 `transpose_B=True`。`MMA1` 里 \(P V\) 不需要转置，故不写。
2. **累加 vs 覆盖**：`MMA0` 每个块都**重新算** \(S_k\)（先用 `T.clear` 或掩码清零，再 `T.gemm`），所以是「覆盖」语义；`MMA1` 的 `acc_o` 要在 K 维上**累加**所有块的贡献，所以 `T.gemm(..., acc_o)` 默认是 `acc_o += ...`。

#### 4.2.3 源码精读

`MMA0` 的乘加（[L51-L59](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L51-L59)）：

```python
T.copy(K[bz, by, k * block_N:(k + 1) * block_N, :], K_shared)   # 取第 k 块 K 进共享
if is_causal:
    for i, j in T.Parallel(block_M, block_N):
        q_idx = bx * block_M + i + past_len
        k_idx = k * block_N + j
        acc_s[i, j] = T.if_then_else(q_idx >= k_idx, 0, -T.infinity(acc_s.dtype))
else:
    T.clear(acc_s)                                               # 非因果：清零
T.gemm(Q_shared, K_shared, acc_s, transpose_B=True,
       policy=T.GemmWarpPolicy.FullRow)                          # acc_s = Q @ K^T
```

- `T.copy(K[bz, by, k*block_N:(k+1)*block_N, :], K_shared)`：沿序列维切出第 `k` 块 key（`bz` 绑 batch、`by` 绑 head、`bx` 绑 query 块）。
- causal 分支用 `T.if_then_else` 把「query 在 key 之后」的位置置 \(-\infty\)（本讲只点到这里，详见 u5-l17）。
- `T.gemm(Q_shared, K_shared, acc_s, transpose_B=True, policy=T.GemmWarpPolicy.FullRow)`：算 \(S_k = Q K_k^\top\)。`transpose_B=True` 让第二操作数按转置参与；`policy=T.GemmWarpPolicy.FullRow` 是 warp 切分策略（u3-l11 讲过 `Square / from_warp_partition`，这里是 `FullRow`——按整行切给 warp）。

`MMA1` 的乘加（[L71-L72](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L71-L72)）：

```python
T.copy(V[bz, by, k * block_N:(k + 1) * block_N, :], V_shared)   # 取第 k 块 V 进共享
T.gemm(acc_s_cast, V_shared, acc_o, policy=T.GemmWarpPolicy.FullRow)  # acc_o += P @ V
```

注意 `MMA1` 的第一操作数是 `acc_s_cast`，不是 `acc_s`：

- `acc_s` 是 `accum_dtype = float`（fp32）的分数；
- `acc_s_cast` 是 `dtype = float16` 的概率（softmax 之后、参与第二次 MMA 前被降精度）。

TensorCore MMA 对输入精度有要求，第二次 `T.gemm` 要吃 fp16，所以中间多了一次 `T.copy(acc_s, acc_s_cast)`（在 `Softmax` 宏末尾，L103），它同时承担「float→fp16 的精度转换」职责（与 u3-l9 里 `C_local→C_shared` 的回写转换同源）。

#### 4.2.4 代码实践

**实践目标**：亲手验证两次 `T.gemm` 的形状与转置语义。

**操作步骤**（源码阅读 + 手算）：

1. 设 `block_M = block_N = 128, dim = 128`（取默认配置，[L14-L19](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L14-L19)）。
2. 写出 `Q_shared`、`K_shared`、`acc_s`、`V_shared`、`acc_o` 的形状。
3. 用矩阵乘法规则验证：`MMA0` 的 `transpose_B=True` 让 `[128,128] @ [128,128]^T = [128,128]` 落到 `acc_s`；`MMA1` 的 `[128,128] @ [128,128] = [128,128]` 落到 `acc_o`（注意 `acc_o` 形状是 `[block_M, dim] = [128,128]`）。

**预期结果**：

| 张量 | 形状 | 含义 |
|---|---|---|
| `Q_shared` | `[128, 128]` | 一块 query |
| `K_shared` | `[128, 128]` | 一块 key |
| `acc_s` | `[128, 128]` | 分数 \(S_k\)（float） |
| `V_shared` | `[128, 128]` | 一块 value |
| `acc_o` | `[128, 128]` | 输出累加（float） |

#### 4.2.5 小练习与答案

**Q1**：`MMA0` 里 `acc_s` 在 `T.gemm` 之前必须先 `T.clear` 或置掩码。为什么？如果不清零直接 `T.gemm`，结果会怎样？

**答**：`T.gemm` 在这里写成 `T.gemm(Q_shared, K_shared, acc_s, ...)`，输出 `acc_s` 是覆盖写（首块）但若不清零则 `acc_s` 里残留上一块 `k-1` 的分数会被当成本块的初值叠加，导致 \(S_k\) 错误。所以每个块都要先把 `acc_s` 清成 0（非 causal）或掩码值（causal）。

**Q2**：为什么 `MMA1` 用 `acc_s_cast`（fp16）而不是 `acc_s`（float）？

**答**：第二次 `T.gemm` 把概率 \(P_k\) 与 \(V_k\) 相乘，输入需要和 \(V\) 同精度（fp16）以匹配 TensorCore 指令。`acc_s_cast` 就是 softmax 后的、降到 fp16 的概率。fp32 的 `acc_s` 是为 softmax 数值稳定保留的高精度中间量。

---

### 4.3 online softmax 与 `reduce_max / reduce_sum`

> 这是本讲最难也最重要的模块。请放慢读。

#### 4.3.1 概念说明：标准 softmax 与「全局最大值」难题

对分数矩阵 \(S\) 的某一行，标准 softmax 是：

\[
p_j = \frac{e^{s_j / \sqrt{d}}}{\sum_j e^{s_j / \sqrt{d}}}, \qquad O = \sum_j p_j v_j
\]

为了数值稳定，要先减去这一行的最大值 \(m = \max_j s_j\)：

\[
p_j = \frac{e^{(s_j - m)/\sqrt{d}}}{\sum_j e^{(s_j - m)/\sqrt{d}}}
\]

问题来了：**分块计算时，\(m\) 是整行的最大值，必须看完所有 key 块才知道**。FlashAttention 的「在线 softmax」做法是：维护一个**随处理进度更新的统计量**，每来一个新块 \(k\)，就用新块里的信息去**修正**已经累加的输出 \(O\) 与分母 \(Z\)，让它们始终等价于「用某个最大值归一化的结果」。

#### 4.3.2 核心流程：在线 softmax 递推

设第 \(k\) 块的行最大值为 \(M_k = \max_{j\in\text{block }k} s_{ij}/\sqrt{d}\)（注意本讲里 scale 已折进，下文为简洁把 \(s/\sqrt{d}\) 直接记作 \(s\)）。本内核维护两个累加量（对每一行 \(i\)）：

- `acc_o`：加权和的分子 \(\sum_k e^{(s - M_k)} v\) 的「滚动的、按最新 \(M_k\) 归一」版本；
- `logsum`：分母 \(\sum_k \mathrm{rowsum}(e^{(s-M_k)})\) 的同样版本。

每来一个新块 \(k\)，做三件事（对应三个 macro）：

1. **`Softmax` 宏**：
   - 记下旧最大值 `scores_max_prev = M_{k-1}`，算出本块最大值 `scores_max = M_k`；
   - 算**缩放因子** `scores_scale = e^{M_{k-1} - M_k}`——把「按 \(M_{k-1}\) 归一」的旧统计量改写成「按 \(M_k\) 归一」；
   - 把本块分数压成概率 `acc_s = e^{s - M_k}`，求行和 `scores_sum = rowsum(e^{s-M_k})`；
   - **更新分母**：`logsum = logsum * scores_scale + scores_sum`。
2. **`Rescale` 宏**：把旧分子按同样因子缩放 `acc_o *= scores_scale`。
3. **`MMA1` 宏**：累加本块贡献 `acc_o += e^{s-M_k} @ V_k`。

#### 4.3.3 源码精读

**`Softmax` 宏全貌**（[L84-L103](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L84-L103)）：

```python
T.copy(scores_max, scores_max_prev)                 # 旧最大值存档
T.fill(scores_max, -T.infinity(accum_dtype))        # 清成 -inf
T.reduce_max(acc_s, scores_max, dim=1, clear=False) # scores_max = 本块行最大值 M_k
...
for i in T.Parallel(block_M):
    scores_scale[i] = T.exp2(scores_max_prev[i] * scale - scores_max[i] * scale)  # e^{M_{k-1}-M_k}

for i, j in T.Parallel(block_M, block_N):
    acc_s[i, j] = T.exp2(acc_s[i, j] * scale - scores_max[i] * scale)  # 概率 e^{s-M_k}
T.reduce_sum(acc_s, scores_sum, dim=1)             # 行和
for i in T.Parallel(block_M):
    logsum[i] = logsum[i] * scores_scale[i] + scores_sum[i]           # 更新分母
T.copy(acc_s, acc_s_cast)                          # float→fp16，供 MMA1
```

**两个归约原语**：

- `T.reduce_max(acc_s, scores_max, dim=1, clear=False)`（[L86](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L86)）：沿 `dim=1`（即 `block_N` 轴）对 `acc_s`（`[block_M, block_N]`）求每行最大值，输出 `scores_max`（`[block_M]`）。`clear=False` 表示「不清空目标、与现有值取 max」——但这里上一行刚 `fill(-inf)`，所以等价于「只取本块最大值」。
- `T.reduce_sum(acc_s, scores_sum, dim=1)`（[L100](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L100)）：沿 `dim=1` 求每行和，输出 `scores_sum`（`[block_M]`）。默认 `clear=True`（先清零再求和）。

两者都把 `[block_M, block_N]` 的二维分数压成 `[block_M]` 的一维行向量——每行（每个 query）一个标量统计量。

**`Rescale` 宏**（[L106-L111](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L106-L111)）：

```python
@T.macro
def Rescale(acc_o, scores_scale):
    for i, j in T.Parallel(block_M, dim):
        acc_o[i, j] *= scores_scale[i]      # 把旧 acc_o 缩放到新最大值 M_k 的参考
```

**循环后的归一化**（[L153-L154](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L153-L154)）：

```python
for i, j in T.Parallel(block_M, dim):
    acc_o[i, j] /= logsum[i]                # 最终 O = 分子 / 分母
```

#### 4.3.4 为什么最后除一下就「精确等价」softmax？（关键证明）

这是理解本内核的灵魂。你会注意到：本内核**并不维护「全局行最大值」**——`scores_max` 每个块都重置成**本块的局部最大值** \(M_k\)，`scores_scale` 用的是 \(M_{k-1}\)（上一个块的局部最大值），而不是「到目前的全局最大值」。这与教科书里「维护 running global max」的在线 softmax 不同。它为什么仍然正确？

关键在于 `acc_o` 和 `logsum` 用的是**同一个参考最大值**，而最后一步是**相除**。

设一共处理了 \(K\) 个块。把递推展开（为简洁省略 scale/\(\sqrt d\)，并记 \(p_k = e^{S_k - M_k}\)）：

- `scores_scale_k = e^{M_{k-1} - M_k}`（块 0 时 \(M_{-1}=-\infty\)，故 `scores_scale_0 = 0`）
- 分子递推：\(o_k = o_{k-1}\cdot e^{M_{k-1}-M_k} + p_k V_k\)
- 分母递推：\(z_k = z_{k-1}\cdot e^{M_{k-1}-M_k} + \mathrm{rowsum}(p_k)\)

逐步代入（\(o_0 = p_0 V_0\)，\(o_1 = e^{M_0-M_1}p_0V_0 + p_1V_1 = e^{S_0-M_1}V_0 + e^{S_1-M_1}V_1\)，依此类推），最终：

\[
o_K = \sum_{k=0}^{K-1} e^{S_k - M_K} V_k, \qquad z_K = \sum_{k=0}^{K-1} \mathrm{rowsum}\!\bigl(e^{S_k - M_K}\bigr)
\]

即「所有块都按**最后一个块**的局部最大值 \(M_K\) 归一」。而真实 softmax 是按全局最大值 \(m=\max_k M_k\) 归一。两者差一个**对所有 \(j\) 相同的常数因子**：

\[
e^{S_k - M_K} = e^{S_k - m}\cdot e^{m - M_K}
\]

这个 \(e^{m - M_K}\) 在分子分母里**同时出现**，相除时**约掉**：

\[
O_i = \frac{o_K}{z_K} = \frac{\sum_k e^{S_k-M_K}V_k}{\sum_k \mathrm{rowsum}(e^{S_k-M_K})} = \frac{\sum_k e^{S_k-m}V_k}{\sum_k \mathrm{rowsum}(e^{S_k-m})} = \mathrm{softmax}_i
\]

**结论**：本内核无需追踪全局最大值——只要 `acc_o` 与 `logsum` 始终被**同一个 `scores_scale`** 同步缩放到**同一个参考最大值**，最后一步 `acc_o /= logsum` 就能把那个任意的参考最大值约掉，得到精确的 softmax。`scores_scale` 的作用，正是把旧统计量从 \(M_{k-1}\) 参考搬迁到 \(M_k\) 参考，让两者始终保持一致。

> 这也解释了 4.1 节遗留的问题：为什么 `Rescale` 必须在 `MMA1` **之前**？因为 `MMA1` 加进去的新项 \(p_k V_k = e^{S_k-M_k}V_k\) 本身已经是按 \(M_k\) 归一好的，**不能**再乘 `scores_scale`；而旧的 `acc_o` 还是按 \(M_{k-1}\) 归一的，必须先乘 `scores_scale` 迁到 \(M_k\)，再加新项。顺序反了就把新项也错误地缩放了。

#### 4.3.5 代码实践

**实践目标**：把 MMA0→Softmax→Rescale→MMA1 在 K 循环里的执行顺序画成流程图，并写出 `logsum` 的更新公式（即本讲义指定的实践任务）。

**操作步骤**（源码阅读 + 手画）：

1. 读 [L143-L154](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L143-L154)。
2. 画出循环内每块的流程图（参考下方）。
3. 写出 `logsum[i]` 的更新式，标清每一项含义。

**参考流程图**（每个 K 块重复一次）：

```
            ┌─────────────── 进入第 k 块 (k = 0..K-1) ───────────────┐
            │                                                          │
            ▼                                                          │
   MMA0:  acc_s  = Q @ K_k^T   (本块分数)                              │
            │                                                          │
            ▼                                                          │
   Softmax:                                                          │
     scores_max_prev ← scores_max   (存档旧参考 M_{k-1})               │
     scores_max ← max_j(acc_s)      (本块局部最大值 M_k)                │
     scores_scale ← exp2(M_{k-1}·s - M_k·s)   (搬迁因子)               │
     acc_s ← exp2(acc_s·s - M_k·s)         (概率 p_k)                  │
     scores_sum ← rowsum_j(acc_s)                                     │
     logsum ← logsum * scores_scale + scores_sum   (更新分母)          │
     acc_s_cast ← acc_s   (float→fp16)                                │
            │                                                          │
            ▼                                                          │
   Rescale: acc_o *= scores_scale    (把旧分子迁到 M_k 参考)           │
            │                                                          │
            ▼                                                          │
   MMA1:   acc_o += acc_s_cast @ V_k  (累加本块贡献)                   │
            │                                                          │
            └──────────────── 下一块 k+1 ─────────────────────────────┘

   循环结束后:
     acc_o /= logsum      (最终归一化, 约掉参考最大值)
     acc_o → O_shared → Output
```

**`logsum` 更新公式**（精确对应 [L102](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L102)）：

\[
\texttt{logsum}_i \;\leftarrow\; \texttt{logsum}_i \cdot \texttt{scores\_scale}_i + \texttt{scores\_sum}_i
\]

其中：

\[
\texttt{scores\_scale}_i = \exp\!\bigl((\texttt{scores\_max\_prev}_i - \texttt{scores\_max}_i)\cdot \texttt{scale}\bigr), \qquad
\texttt{scores\_sum}_i = \sum_{j} \exp\!\bigl((\texttt{acc\_s}_{i,j} - \texttt{scores\_max}_i)\cdot \texttt{scale}\bigr)
\]

> 提醒：`scale` 里已经折进了 \(1/\sqrt{d}\) 和 \(\log_2 e\)，所以这里的 \(\exp\) 在代码里都写成 `T.exp2`（见 4.4 节）。

**需要观察的现象**：第一块 \(k=0\) 时 \(M_{-1}=-\infty\)，`scores_scale = exp2(-∞) = 0`，于是 `logsum_0 = 0*0 + scores_sum_0 = scores_sum_0`，`Rescale` 把初值为 0 的 `acc_o` 乘 0 仍为 0——自洽。

#### 4.3.6 小练习与答案

**Q1**：本内核用「本块局部最大值 \(M_k\)」而非「全局行最大值」，最后却能得到精确 softmax。这个奇迹发生在哪一步？

**答**：发生在循环后的 `acc_o /= logsum`（[L153-L154](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L153-L154)）。因为分子 `acc_o` 和分母 `logsum` 都按**同一个**参考最大值 \(M_K\) 归一，相除时公因子 \(e^{m-M_K}\) 约掉。

**Q2**：`T.reduce_max(acc_s, scores_max, dim=1, clear=False)` 里 `clear=False` 的作用是什么？为什么前面要先 `T.fill(scores_max, -inf)`？

**答**：`clear=False` 表示「不清空目标缓冲、把归约结果与目标现有值取 max」。前面 `fill(-inf)` 把目标清成 \(-\infty\)，于是「与 \(-\infty\) 取 max」就等于「直接取本块最大值」——既复用了 `clear=False` 的接口，又保证只反映本块分数。

---

### 4.4 `exp2` / `logsum`：用 \(2^x\) 替代 \(e^x\) 的指令优化

#### 4.4.1 概念说明

代码里所有「指数」都写成 `T.exp2`（以 2 为底），而不是 `T.exp`（以 e 为底），并把常数 \(\log_2 e \approx 1.4427\) 提前折进了 `scale`。这是一处典型的 GPU 指令优化：

\[
\exp(x/\sqrt d) = 2^{\,x \cdot \log_2 e / \sqrt d}
\]

令 \(\texttt{scale} = \frac{\log_2 e}{\sqrt d}\)，则 \(\exp(x/\sqrt d) = \texttt{exp2}(x\cdot\texttt{scale})\)。在 GPU 上 `exp2` 直接对应一条硬件指令（`ex2.approx`），比 `exp` 快；而 `x*scale - max*scale` 可以用一条 fused multiply-add（`ffma`）完成，比 `x - max` 后再 `*scale` 的两条指令更省。

#### 4.4.2 核心流程

1. 在内核最外层把 `scale` 预算好，折进 \(\log_2 e\)。
2. softmax 中所有指数都写成 `T.exp2(数值 * scale - 最大值 * scale)`，让减法与乘法合并成 `ffma`。

#### 4.4.3 源码精读

`scale` 的定义（[L31](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L31)）：

```python
scale = (1.0 / dim)**0.5 * 1.44269504  # log2(e)
```

- `(1.0/dim)**0.5` 是 \(1/\sqrt d\)；
- `1.44269504` 就是 \(\log_2 e = 1/\ln 2\)；
- 所以 `scale = (1/√d) · log2(e)`。

softmax 里的两次指数（[L93](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L93) 与 [L99](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L99)）：

```python
scores_scale[i] = T.exp2(scores_max_prev[i] * scale - scores_max[i] * scale)
...
acc_s[i, j]      = T.exp2(acc_s[i, j] * scale - scores_max[i] * scale)
```

代码注释（[L95-L98](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L95-L98)）原话：

> Instead of computing exp(x - max), we compute exp2(x * log_2(e) - max * log_2(e)). This allows the compiler to use the ffma instruction instead of fadd and fmul separately.

> ⚠️ 注意「`logsum`」这个名字有误导性：它**不是** \(\log\) 形式的对数和，只是一个普通的累加分母（行和）。命名借用了 FlashAttention 论文/实现里 `row_sum / logsumexp` 的习惯用语，但本内核里它存的就是「减去参考最大值后指数的和」，没有取对数。读代码时不要被名字带偏。

#### 4.4.4 代码实践

**实践目标**：确认「`exp2` + 折进 scale」与「`exp` + 不折进」数值等价。

**操作步骤**（手算）：

1. 取 `dim = 128`，算出 `scale = (1/128)**0.5 * 1.44269504 ≈ 0.12745`。
2. 任取一个分数 `x = 3.0`（未除 \(\sqrt d\) 的原始分数），分别用两种方式算：
   - 原始：\(\exp(x/\sqrt d) = \exp(3/11.314) = \exp(0.2652) \approx 1.3037\)
   - 代码：`exp2(x * scale) = exp2(3 * 0.12745) = exp2(0.38235) = 2^0.38235 ≈ 1.3037`
3. 比较两者。

**预期结果**：两者在小数点后多位一致（约 1.3037），证明 `exp(x/√d) ≡ exp2(x · scale)`。

> 若你手算时与上述略有出入，属于四舍五入；核心是验证两种写法**数学恒等**。

#### 4.4.5 小练习与答案

**Q1**：如果不折进 \(\log_2 e\)，把 `scale` 写成 `(1.0/dim)**0.5`，并把所有 `T.exp2` 改成 `T.exp`、去掉 `* scale` 里那部分，结果是否还正确？

**答**：数学上完全等价、结果正确。代价是：`exp` 在 GPU 上比 `exp2` 慢，且 `x - max` 之后还要单独 `* 1/√d`，多用指令、少用 `ffma`。所以本内核选择「折进 scale + `exp2`」是纯性能优化，不改语义。

**Q2**：为什么 `scores_scale = exp2(M_{k-1}*scale - M_k*scale)` 而不是 `exp2((M_{k-1}-M_k)*scale)`？两者等价吗？

**答**：两者数学等价：\(a\cdot s - b\cdot s = (a-b)\cdot s\)。代码故意写成前者，正是为了让编译器把 `scores_max_prev*scale - scores_max*scale` 编成一条 `ffma`（注释 L95-L98 已说明），而不是先做减法再乘 scale 的两条指令。

---

### 4.5 片上缓冲（Fragment / Shared buffer）与回写顺序

#### 4.5.1 概念说明

本内核用了两类片上存储（与 u3-l9 一致）：

- **shared memory**（`T.alloc_shared` / `T.SharedBuffer`）：SM 内共享内存，块内所有线程可见，带宽高，是「全局↔寄存器」的中转站。
- **fragment / register**（`T.alloc_fragment` / `T.FragmentBuffer`）：寄存器，线程私有，最快但容量最小，放累加器。

| TileLang 声明 | 存储空间 | 用途 |
|---|---|---|
| `T.alloc_shared` / `T.SharedBuffer` | shared memory | 输入块中转（`Q/K/V_shared`）、回写中转（`O_shared`）、精度转换（`acc_s_cast` 在 fragment 里其实是……见下） |
| `T.alloc_fragment` / `T.FragmentBuffer` | 寄存器 | 累加器（`acc_s / acc_o`）、softmax 一维统计量（`scores_*` / `logsum`） |

> 「以代码为准」提醒：宏签名里 `acc_s_cast` 标注为 `T.FragmentBuffer`（[L65](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L65)），而 `main` 里 `acc_s_cast` 也是 `alloc_fragment`（[L126](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L126)）。所以精度转换 `float→fp16` 是在**寄存器**之间做的，`T.copy(acc_s, acc_s_cast)` 同时改 dtype。

#### 4.5.2 核心流程

数据搬运闭环（每个 query 块）：

```
全局 Q ──T.copy──▶ Q_shared (shared, fp16)          # 循环外取一次, 全程复用
全局 K_k ──T.copy──▶ K_shared (shared, fp16) ──T.gemm──▶ acc_s (fragment, float)
                                                            │  (softmax 在 fragment 内做)
                                                            ▼
                                                      acc_s_cast (fragment, fp16)
                                                            │
全局 V_k ──T.copy──▶ V_shared (shared, fp16) ◀──T.gemm── acc_o (fragment, float)
                                                            │ (循环结束)
                                                            ▼ /logsum
                                                      O_shared (shared, fp16) ──T.copy──▶ 全局 Output
```

回写两步（`acc_o → O_shared → Output`）与 u3-l9 的 `C_local → C_shared → C_global` 同源：`acc_o` 在寄存器里以「分片布局」存放，必须先经共享内存 `O_shared` 重排成可合并写的规整布局，并顺便把 float 降成 fp16，再写回全局。

#### 4.5.3 源码精读

**缓冲分配**（[L121-L132](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L121-L132)）：

```python
Q_shared   = T.alloc_shared([block_M, dim], dtype)       # shared
K_shared   = T.alloc_shared([block_N, dim], dtype)       # shared
V_shared   = T.alloc_shared([block_N, dim], dtype)       # shared
O_shared   = T.alloc_shared([block_M, dim], dtype)       # shared
acc_s      = T.alloc_fragment([block_M, block_N], accum_dtype)   # fragment, float
acc_s_cast = T.alloc_fragment([block_M, block_N], dtype)         # fragment, fp16
acc_o      = T.alloc_fragment([block_M, dim], accum_dtype)       # fragment, float
scores_max = T.alloc_fragment([block_M], accum_dtype)            # fragment, float
scores_max_prev = T.alloc_fragment([block_M], accum_dtype)
scores_scale    = T.alloc_fragment([block_M], accum_dtype)
scores_sum      = T.alloc_fragment([block_M], accum_dtype)
logsum          = T.alloc_fragment([block_M], accum_dtype)
```

**初始化**（[L134-L137](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L134-L137)）：

```python
T.copy(Q[...], Q_shared)        # Q 只取一次, 循环里反复用
T.fill(acc_o, 0)
T.fill(logsum, 0)
T.fill(scores_max, -T.infinity(accum_dtype))   # 初始最大值 -inf
```

**循环与回写**（[L143-L156](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L143-L156)）：四宏循环 → `acc_o /= logsum` → `T.copy(acc_o, O_shared)` → `T.copy(O_shared, Output[...])`。

> 注意 `Q` 的取法：`Q_shared` 在循环**外**只 `T.copy` 一次（[L134](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L134)）。因为一个 query 块要对**所有** key 块做点积，Q 复用 K 次，只取一次是必要的带宽优化。而 `K_shared / V_shared` 在循环内每个块都重新 `T.copy`（在 `MMA0 / MMA1` 宏内）。

#### 4.5.4 代码实践

**实践目标**：把 A/B/C（这里是 Q/K/V/O）的 shared/fragment 分配与每次 `T.copy` 的「源→目的」整理成表格，验证搬运闭环。

**操作步骤**（源码阅读型）：

1. 在 [L121-L156](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L121-L156) 里找出所有 `T.copy` 调用（含宏内的）。
2. 列成「源 → 目的」表，标注每一步的 dtype 变化。

**预期结果**：

| `T.copy` 调用位置 | 源 | 目的 | dtype 变化 |
|---|---|---|---|
| `main` L134 | 全局 `Q` (fp16) | `Q_shared` (shared, fp16) | 无 |
| `MMA0` L51 | 全局 `K_k` (fp16) | `K_shared` (shared, fp16) | 无 |
| `Softmax` L103 | `acc_s` (fragment, float) | `acc_s_cast` (fragment, fp16) | float→fp16 |
| `MMA1` L71 | 全局 `V_k` (fp16) | `V_shared` (shared, fp16) | 无 |
| `main` L155 | `acc_o` (fragment, float) | `O_shared` (shared, fp16) | float→fp16 |
| `main` L156 | `O_shared` (shared, fp16) | 全局 `Output` (fp16) | 无 |

可观察到：精度转换（float→fp16）发生在「fragment→fragment」与「fragment→shared」两类拷贝上；而 shared↔全局的拷贝不改 dtype。

#### 4.5.5 小练习与答案

**Q1**：为什么 `acc_o` 不直接写回全局 `Output`，而要先过 `O_shared`？

**答**：`acc_o` 在寄存器里以 MMA 分片（fragment）布局存放，直接写回全局会是非合并访存、且布局不对。先 `T.copy` 到 `O_shared`（共享内存）让硬件把分片重排成规整的行主序布局，同时把 float 降为 fp16，再合并写回全局。这与 u3-l9 的 `C_local→C_shared→C_global` 完全同源。

**Q2**：`Q_shared` 为什么在循环外只取一次，而 `K_shared / V_shared` 在循环内每个块都重新取？

**答**：一个 query 块要与**所有** key 块做点积（Q 复用 K 次），故只取一次。而每个 key/value 块只在第 k 次迭代用到，必须在循环内按块流式取入，配合 `T.Pipelined(num_stages=2)` 做软件流水以隐藏访存延迟。

---

## 5. 综合实践

把本讲的「macro 结构 + 在线 softmax + 两次 gemm + exp2/logsum」串起来，完成下面的源码阅读型任务：

**任务**：以 2 个 key 块（\(K=2\)）为例，在纸上完整推演一次「非 causal」FlashAttention 内核的一行（某个 query）。

设某行在两个块里的原始分数（未除 \(\sqrt d\)、未乘 scale，为简洁已令 scale=1）为：
- 块 0：\(s^{(0)} = [1,\ 3]\)，故 \(M_0 = 3\)
- 块 1：\(s^{(1)} = [2,\ 0]\)，故 \(M_1 = 2\)
- 对应 value 块 \(V_0 = [v_1, v_2]\)、\(V_1 = [v_3, v_4]\)（符号即可）

请按内核递推一步步算：

1. **k=0**：`scores_max_prev = M_{-1} = -inf`；`scores_max = M_0 = 3`；`scores_scale_0 = exp2(-inf-3) = 0`；`p_0 = [exp(1-3), exp(3-3)] = [e^{-2}, 1]`；`scores_sum_0 = e^{-2}+1`；`logsum_0 = 0*0 + scores_sum_0 = 1+e^{-2}`；`Rescale`：`acc_o *= 0`（仍为 0）；`MMA1`：`acc_o = 0 + p_0·V_0 = e^{-2}v_1 + 1·v_2`。
2. **k=1**：`scores_max_prev = M_0 = 3`；`scores_max = M_1 = 2`；`scores_scale_1 = exp2(3-2) = e`；`p_1 = [exp(2-2), exp(0-2)] = [1, e^{-2}]`；`scores_sum_1 = 1+e^{-2}`；`logsum_1 = logsum_0·e + scores_sum_1 = (1+e^{-2})e + (1+e^{-2})`；`Rescale`：`acc_o = (e^{-2}v_1+v_2)·e`；`MMA1`：`acc_o += p_1·V_1 = (e^{-2}v_1+v_2)e + 1·v_3 + e^{-2}v_4`。
3. **归一化**：`O = acc_o / logsum_1`。
4. **验证等价性**：把 `acc_o` 与 `logsum` 都按 \(M_1=2\) 归一展开，你会看到分子分母同时含有公因子 \(e^{m-M_1}\)（其中 \(m=\max(3,2)=3\)），相除约掉，最终等于按全局最大值 \(m=3\) 做的标准 softmax。

**完成标志**：你能说清「为什么块 1 的局部最大值 \(M_1=2 < M_0=3\)，但结果依然正确」——因为分子分母用同一个参考 \(M_1\)，归一化时约掉了。这正是本讲 4.3.4 证明的具体实例。

> 待本地验证：若你想看真实数字，可把上述符号替换成具体向量（如 \(V_0=[1,2], V_1=[3,4]\)）手算数值，或编写一个最小 torch 脚本调用 `ref_program`（[L176-L188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L176-L188)）对比。

## 6. 本讲小结

- FlashAttention 用「分块 + 在线 softmax」消掉了 \(O(n^2)\) 中间矩阵 \(S\) 的显存流量；本内核用 4 个 `@T.macro`——`MMA0 / Softmax / Rescale / MMA1`——把每个 K 块的四步计算封装成可读的子程序，编译时内联展开。
- 两次 `T.gemm`：`MMA0` 算 \(S_k = QK_k^\top\)（`transpose_B=True`，先清零/掩码）；`MMA1` 累加 \(O \mathrel{+}= P_k V_k\)。中间经 `acc_s`（float）→`acc_s_cast`（fp16）的精度转换。
- 在线 softmax 不需要追踪全局最大值：`Rescale` 与 `logsum` 更新用**同一个** `scores_scale = e^{M_{k-1}-M_k}` 把分子分母同步迁到最新参考最大值，循环后 `acc_o /= logsum` 把这个参考约掉，得到精确 softmax。
- `reduce_max` / `reduce_sum` 沿 `dim=1` 把 `[block_M, block_N]` 分数压成 `[block_M]` 行向量，分别给出本块行最大值与行和。
- `exp2` + 预折 \(\log_2 e\) 进 `scale` 是指令优化：`exp2(x·scale - max·scale)` 让一条 `ex2` 配一条 `ffma` 取代更慢的 `exp` 路径，数值不变。
- 片上存储分 shared（`alloc_shared`，块内可见、做中转）与 fragment（`alloc_fragment`，寄存器、放累加器）；回写走 `acc_o → O_shared → Output` 两步，兼顾布局重排与 float→fp16 降精度。

## 7. 下一步学习建议

- **紧接的下一讲 `u5-l17`** 会把本讲一笔带过的 causal 分支讲透：`is_causal` 时用 `T.if_then_else` + `-T.infinity` 给上三角置 \(-\infty\)、按 `(bx+1)*block_M` 裁剪 `loop_range` 减少无效迭代，以及被注释掉的 FA3 `Check_inf`（把 \(-\infty\) 最大值归零）技巧。建议读完本讲再去看 `MMA0` 的 `if is_causal` 分支（[L52-L56](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L52-L56)）与 `loop_range`（[L139-L141](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L139-L141)）。
- **`u5-l18`** 会讲本文件里的 `tilelang.compile + get_profiler().do_bench` 离线评估流程，以及 `ref_program`（[L176-L188](https://github.com/tile-ai/tilelang-benchmark/blob/b658f7e9f326156d11a09dff1e9825fa6d9a8767/hopper_benchmark/flashattention/1.tilelang_benchmark/benchmark_tilelang_mha.py#L176-L188)）如何用 torch einsum+softmax 做正确性校验。
- 如果你对在线 softmax 的「全局最大值版本」感兴趣，可对照 FlashAttention 原论文的 Algorithm 1，体会本内核「用局部最大值 + 相除约分」这一更简洁实现之间的取舍。
