# 反向预处理与后处理

## 1. 本讲目标

在上一讲（u9-l1）里，我们看清了反向主循环 `FlashAttentionBackwardSm80` 的五条梯度公式与重计算 `S/P` 的主循环。但主循环并不是孤立运行的——它在启动**之前**需要有人帮它把「行修正项 D」「base-2 的 LSE」和「清零的 dQ 累加器」准备好，在结束**之后**又需要有人把「跨 thread block 的 fp32 dQ 累加值」收敛成最终 fp16 的 `dq`。这两件事分别由两个独立的轻量 kernel 完成：`FlashAttentionBackwardPreprocess`（预处理）与 `FlashAttentionBackwardPostprocess`（后处理）。

学完本讲，你应当能够：

- 说清反向「预处理 → 主循环 → 后处理」三阶段之间，`O`、`dO`、`LSE`、`D`、`dQaccum`、`dQ` 这些张量是如何流动的；
- 理解为什么 `dQ` 需要一个 fp32 的累加缓冲 `dQaccum`、为什么它必须在主循环前被清零；
- 掌握 `D = (O⊙dO).rowsum` 这一行和的来源、以及 `dLSE` 如何把它修正成 `D' = D − dLSE`；
- 看懂后处理如何把「延迟到最后的 softmax 缩放」与「fp32→fp16 类型转换」**融合在一步**完成；
- 能够独立画出三阶段的数据依赖图。

本讲聚焦 [flash_bwd_preprocess.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py) 与 [flash_bwd_postprocess.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py)，不重复上一讲的主循环推导，只在必要处承接。

## 2. 前置知识

阅读本讲前，你需要熟悉以下概念（大多来自 u9-l1）：

- **反向五公式**：\(dV=P^{\top}dO\)、\(dP=dO\cdot V^{\top}\)、\(dS=P\odot(dP-D)\)、\(dQ=\text{scale}\cdot dS\cdot K^{\top}\)、\(dK=\text{scale}\cdot dS^{\top}\cdot Q\)。其中 **D 是逐行标量** \(D_i=\sum_j (O_{ij}\odot dO_{ij})\)，它在 `dS` 公式里充当「行修正项」。
- **dQ 的特殊性**：`dK/dV` 在主循环里按 n_block 切工作，单个 thread block 内跨 m_block 用寄存器累加后只写一次；而 `dQ` 对每个 Q 行要汇总所有 n_block 的贡献，**这些 n_block 来自不同的 thread block**，因此 dQ 必须用**全局 fp32 缓冲** `dQaccum` + `atomic_add` 来汇总。
- **LSE 与 exp2**：前向产出 `lse = ln(Σ exp(S))`，反向要重建 `P`。为复用硬件 `exp2`，需要 base-2 的 LSE，即 `lse_log2 = lse · log₂e`。
- **gmem / smem / rmem 三级存储**与 cp.async / TMA 拷贝原子（u5-l2）。
- **`const_expr` / `cutlass.Constexpr`** 编译期常量驱动 kernel 特化（u11-l2 会深入）。

几个对初学者可能陌生的术语：

- **PDL（Programmatic Dependent Launch）**：Hopper 起支持的硬件特性，允许一个 kernel 在「上一个 stream kernel 还没结束」时就以 prologue 模式提前启动，靠 `griddepcontrol_wait/launch_dependents` 显式握手来保证数据依赖。预处理 kernel 用它来与前向 kernel 的尾部重叠。
- **延迟缩放（deferred scale）**：把本该乘在每个梯度上的 `softmax_scale` 推迟到类型转换时一次性乘上，省掉一次遍历。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [flash_bwd_preprocess.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py) | `FlashAttentionBackwardPreprocess`：主循环前运行。读 `O/dO/LSE`，写出行和 `D`（`dpsum`）、base-2 LSE（`lse_log2`），并把 `dQaccum` 清零。 |
| [flash_bwd_postprocess.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py) | `FlashAttentionBackwardPostprocess`：主循环后运行。把 fp32 的 `dQaccum` 乘上延迟的 `softmax_scale`、转成 fp16/bf16 写回 `dq`；GQA 时同样处理 `dKaccum/dVaccum`。 |
| [interface.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py) | `_flash_attn_bwd` 里的「三阶段编排」：分配 `dq_accum/dpsum/lse_log2`（[:1539-L1569](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1539-L1569)）、调用预处理（[:1626-L1632](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1626-L1632)）、调用主循环、调用后处理（[:1964-L1999](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1964-L1999)）。 |
| [flash_bwd.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py) | 主循环消费方：用 `dpsum` 当 D、用 `lse_log2` 重建 P、向 `dQaccum` 原子累加。本讲只在「接口契约」层面引用它。 |

---

## 4. 核心概念与源码讲解

### 4.1 反向三阶段总览与预处理职责

（对应最小模块：**预处理：dQ/D/缩放准备**）

#### 4.1.1 概念说明

把反向看作一条流水线，主循环 kernel 不是自给自足的——它依赖三个「预先备好的料」：

1. **D（`dpsum`）**：`dS = P⊙(dP−D)` 里的逐行标量。它本质是 \((O\odot dO)\) 沿 head_dim 的行和。如果对整行做一次乘加，开销远低于在每个 n_block 的主循环里重复算。
2. **base-2 LSE（`lse_log2`）**：主循环要用 `exp2` 重建 P，而前向给出的是自然底 LSE，必须预先乘上 `log₂e` 换底。
3. **清零的 `dQaccum`**：dQ 的 fp32 全局累加缓冲。主循环里多个 thread block 会向它 `atomic_add`，**若不预先清零，结果会被上一次反向的残留污染**。

预处理 kernel `FlashAttentionBackwardPreprocess` 就是这三件事的「备料工」。它是一个**逐 m_block（Q 行块）并行**的轻量 kernel：每个 thread block 负责一个 `(m_block, head, batch)` 工作块，把对应那一块 Q 行的 D、lse_log2 算出来写回 gmem，并顺手把对应的 dQaccum 区域清零。

为什么要把这些事拆成单独 kernel，而不是塞进主循环？因为它们读的是 `O/dO/LSE`（前向产物），而主循环读的是 `Q/K/V/dO` 并按 n_block 切分。把「行级统计」和「按 K 切分的重计算」放在同一个 kernel 里会让访存模式互相干扰；独立成一个 kernel 后，每个 kernel 的访存都是规整、可并行的。

#### 4.1.2 核心流程

预处理 kernel 单 thread block 的工作流程（伪代码）：

```
领一个工作块 work_tile = (m_block, head_idx, batch_idx)
若 use_pdl: griddepcontrol_wait()        # 等前向 kernel 写完 O/dO/LSE
seqlen = 由 cu_seqlens/seqused 解出该 batch 的 seqlen_q
把本 m_block 的 gO、gdO 经 cp.async 搬进寄存器 tOrO、tOrdO     # 带 OOB 谓词
若 use_pdl: griddepcontrol_launch_dependents()   # 通知下一个 kernel 可以读我的输出
# —— D 行和 ——
pdpsum = (tOrO * tOrdO).reduce(沿 head_dim_v 求和)
pdpsum = warp_reduce(pdpsum)               # 跨线程归约成每行一个值
把 pdpsum 写回 gPdPsum（必要时减去 dLSE）  # 即 D
# —— 清零 dQaccum ——
若 mdQaccum 不为 None: 把本 m_block 对应的 dQaccum 区域写 0
# —— base-2 LSE ——
若 mLSElog2 不为 None: 写出 lse_log2 = lse * log2(e)（空行置 0）
```

值得注意的两点设计：

- **PDL 重叠**：预处理在 SM90+ 上 `use_pdl=True`。它在读 `O/dO` 之前 `griddepcontrol_wait()`（等前向写完），在读完后立即 `griddepcontrol_launch_dependents()`（放行后续 kernel）。这让预处理能挤进前向 kernel 的尾部空隙。
- **写入是按列 0 线程**：`pdpsum` 归约后只有「对应第 0 列」的线程把结果写回 gmem，避免多线程重复写同一地址。

#### 4.1.3 源码精读

预处理类的构造与备料参数见 [flash_bwd_preprocess.py:39-77](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L39-L77)：`tile_m` 是 Q 行块大小，`head_dim/head_dim_v` 决定 D 归约的 K 维长度，`pack_gqa/qhead_per_kvhead/nheads_kv` 用于 GQA 折叠。`use_pdl` 由架构决定（SM90+ 才开）：

```python
self.use_pdl = BaseDSL._get_dsl().get_arch_enum() >= Arch.sm_90a
```

D′ 推导的数学在文件开头的模块文档串里有完整说明 [flash_bwd_preprocess.py:5-13](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L5-L13)。其核心是：当 LSE 也参与求导（即外部给了 `dLSE`）时，`dS` 多出一项 `dLSE_i·P_ij`，可吸收为 `D' = D − dLSE`，主循环公式形式不变：

\[ dS_{ij}=P_{ij}(dP_{ij}-D_i)+dLSE_i\cdot P_{ij}=P_{ij}\bigl(dP_{ij}-(D_i-dLSE_i)\bigr) \]

预处理 kernel 的入口与 PDL 握手 [flash_bwd_preprocess.py:311-321](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L311-L321)：先领工作块，再 `griddepcontrol_wait()` 等上游写完。`O/dO` 加载完成后调用 `griddepcontrol_launch_dependents()` 放行 [flash_bwd_preprocess.py:385-386](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L385-L386)。

接口层 `_bwd_preprocess` 把这些料包装成一次调用 [interface.py:1203-1242](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1203-L1242)，它的 `compile_key` 包含了 `dq_accum is not None`、`dlse is not None`、`row_max is not None` 等布尔——这意味着**是否需要 dQaccum 清零、是否需要 dLSE 修正会编译出不同的预处理 kernel**。真正的调度发生在主反向函数里 [interface.py:1626-1632](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1626-L1632)。

`dq_accum/dpsum/lse_log2` 这三个缓冲的分配 [interface.py:1539-1569](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1539-L1569)，注意三者都是 **fp32**，且尺寸按 `seqlen_q_rounded`（向上取整到 `m_block_size` 的倍数）和 `head_dim_rounded`（向上取整到 32 的倍数）来分配，这是为了让每个 m_block 的写回都落在整齐对齐的地址上。

#### 4.1.4 代码实践

**实践目标**：确认「预处理写出三个料」这件事在接口层是可观测的，并理解 `dq_accum` 的清零依赖。

**操作步骤**：

1. 打开 [interface.py:1539-1569](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1539-L1569)，记录 `dq_accum`、`dpsum`、`lse_log2` 三者的 dtype 与形状公式。
2. 想象一个假想实验：如果预处理里**删掉**清零 `dQaccum` 的代码（即下文 4.2 会精读的 [:416-L431](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L416-L431)），但**复用同一个 `dq_accum` 张量连续做两次反向**，第二次的 `dq` 会受什么影响？写下你的推理。

**需要观察的现象**：`dq_accum` 必须在主循环前为全 0；否则主循环的 `atomic_add` 会叠加上一次反向的残留，导致 `dq` 错误。

**预期结果**：连续两次反向若复用未清零的 `dq_accum`，第二次的 `dq` ≈ 正确值 + 上一次的 `dq_accum` 残留。这正是预处理必须清零的根因。（本步为源码推理型实践，不需要 GPU；如要实测，可在本地用 `flash_attn.cute.flash_attn_func` 做两次 `.backward()` 并观察，**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：预处理 kernel 为什么要用 `griddepcontrol_wait()` 而不是依赖 CUDA stream 的默认顺序？

> **答**：预处理开了 PDL（`use_pdl=True`），PDL 允许 kernel 在前一个 stream kernel 还在跑时就提前启动 prologue。如果不显式 `wait`，预处理可能读到前向 kernel 还没写完的 `O/dO/LSE`，导致 `dpsum = Σ(O·dO)` 被半成品污染，并顺着 `dS=P·(dP−dpsum)` 传染到 `dQ/dK/dV`。源码注释 [:315-L321](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L315-L321) 明确说明了这一风险。

**练习 2**：`dq_accum` 为什么用 fp32 而不是 fp16？

> **答**：dQ 由来自不同 thread block 的许多 `dS·Kᵀ` 片段经 `atomic_add` 累加而成，累加次数多、动态范围大。fp16 的精度与原子加实现都不足以安全承担这种归约；fp32 提供足够的累加精度，最终再由后处理一次性转回 fp16。

---

### 4.2 D 行和计算：softmax 反向的「行修正项」

（对应最小模块：**D 行和计算**）

#### 4.2.1 概念说明

反向公式里最容易被忽略、却最关键的一个量是行标量 D：

\[ D_i=\sum_j O_{ij}\odot dO_{ij} \]

它出现在 `dS = P⊙(dP−D)` 中，作用是补偿 softmax 归一化带来的梯度依赖——因为 `P` 的每一行依赖该行所有元素（经 softmax 分母），所以 `dP` 不能直接当 `dS`，必须减去一个与该行整体能量 `D` 有关的修正项。上一讲 u9-l1 已给出推导，本讲只关注**这个 D 是怎么被算出来并写到 gmem 的**。

D 的计算有一个非常好的性质：它只依赖 `O` 和 `dO`，与 `Q/K/V` 的分块无关。因此它可以在一个**只按 Q 行块（m_block）并行**的轻量 kernel 里完成，主循环只需读取它即可。这正是预处理存在最重要的理由之一。

#### 4.2.2 核心流程

预处理 kernel 内部，D 的计算是一个经典的「分块 + 两级归约」：

1. 把本 m_block 的 `gO`、`gdO`（形状 `(tile_m, head_dim_v)`）搬进寄存器 `tOrO`、`tOrdO`。
2. 逐元素相乘后沿 head_dim_v（K 维）求和，得到每个线程负责的若干行的「部分行和」`pdpsum`。
3. **warp 内归约**：因为 gmem 拷贝布局故意让一行落在同一个 warp 内，`utils.warp_reduce(pdpsum, width=threads_per_row)` 把同一行的部分和归约成「每行一个值」。
4. 写回 gmem：只有对应「第 0 列」的线程执行写回，地址由 identity tensor 的坐标决定，并做 `row < seqlen_limit` 的越界保护；若提供了 `dLSE`，则写入前减去 `gdLSE[row]`，得到 D′。

数学上最终写入的是：

\[ \texttt{dpsum}_i = D_i - dLSE_i = \sum_j O_{ij}dO_{ij} - dLSE_i \]

主循环随后把它当作 `dS` 公式里的 D 使用。

#### 4.2.3 源码精读

D 归约的核心几行 [flash_bwd_preprocess.py:388-395](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L388-L395)：

```python
pdpsum = (tOrO.load().to(Float32) * tOrdO.load().to(Float32)).reduce(
    cute.ReductionOp.ADD, init_val=0.0, reduction_profile=(0, None, 1)
)
threads_per_row = gmem_tiled_copy_O.layout_src_tv_tiled[0].shape[0]
pdpsum = utils.warp_reduce(pdpsum, operator.add, width=threads_per_row)
```

这里 `(tOrO * tOrdO)` 在 fp32 上做（先 `.to(Float32)` 提精度），沿 K 维 `reduction_profile=(0, None, 1)` 求和，再 `warp_reduce` 收敛到每行一个值。

写回 D（含 dLSE 修正）[flash_bwd_preprocess.py:404-414](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L404-L414)：

```python
gPdPsum = cute.local_tile(mPdPsum_cur, (self.tile_m,), (m_block,))
if tOcO[0, 0, 0][1] == 0:          # 只有第 0 列线程写
    for m in cutlass.range(cute.size(PdP_sum), unroll_full=True):
        row = tOcO[0, m, 0][0]
        PdPsum_val = 0.0
        if row < seqlen_limit:
            PdPsum_val = PdP_sum[m]
            if const_expr(mdLSE is not None):
                PdPsum_val -= gdLSE[row]   # D' = D - dLSE
        gPdPsum[row] = PdPsum_val
```

注意 `if row < seqlen_limit` 把越界行写成 0，这与 varlen 下「不足一个 tile 的尾部」对齐。

清零 `dQaccum` 的代码紧随其后 [flash_bwd_preprocess.py:416-431](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L416-L431)：构造一个全 0 的寄存器张量，用专门的 1D 拷贝原子 `gmem_tiled_copy_dQaccum`（在 `_setup_attributes` 里建 [:126-L133](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L126-L133)）写回对应 m_block 的 `tile_m × head_dim_padded` 区域。

base-2 LSE 的写出 [flash_bwd_preprocess.py:433-442](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_preprocess.py#L433-L442)：

```python
LOG2_E = math.log2(math.e)
lse_log2 = lse * LOG2_E if lse != -Float32.inf else 0.0
...
gLSElog2[tidx] = lse_log2
```

`lse != -Float32.inf` 的分支是对「全掩码空行」（`lse = −∞`）的安全化：把 `lse_log2` 置 0 而非 `−∞`，避免主循环里 `exp2(S·scale_log2 − lse_log2)` 因减去 `−∞` 变成 `+∞` 而产生 NaN。对空行而言主循环本就不会处理任何 n_block，故置 0 不影响结果。

> 衔接主循环：主反向函数把 **`lse_log2`（而非原始 `lse`）** 作为 LSE 参数传给主循环 kernel [interface.py:1935](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1935)；主循环据此用 `exp2(S·softmax_scale_log2 − tLSErLSE)` 重建 P [flash_bwd.py:953](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L953)。这条「换底」链路是预处理与主循环之间最隐蔽也最关键的契约之一。

#### 4.2.4 代码实践

**实践目标**：用 PyTorch 写出 D 的参考实现，验证 `(O⊙dO).rowsum` 这条公式，并体会它与 kernel 里 `warp_reduce` 的对应关系。

**操作步骤**：

```python
import torch
# 示例代码：仅用于对照理解，非项目源码
torch.manual_seed(0)
batch, seqlen, nheads, hdim_v = 1, 128, 2, 64
O   = torch.randn(batch, seqlen, nheads, hdim_v, dtype=torch.float32)
dO  = torch.randn_like(O)
# D_i = sum_j (O_ij * dO_ij)，沿 head_dim_v（最后一维）求和
D = (O * dO).sum(dim=-1)          # (batch, seqlen, nheads)
print(D.shape, D.float().mean().item())
```

**需要观察的现象**：`D` 的形状是 `(batch, seqlen, nheads)`——每一行（每个 query）一个标量，这与 kernel 写回的 `dpsum` 形状 `(batch, num_head, seqlen_q_rounded)` 在语义上一一对应（只是头维与序列维的排布不同）。

**预期结果**：参考实现给出的 D 与「kernel 里 `pdpsum` 经 `warp_reduce` 后、由第 0 列线程写回的值」在 fp32 下逐元素相等（容差内）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 D 的归约要分成「`.reduce` 沿 K 维」和「`warp_reduce`」两步，而不是一步到位？

> **答**：gmem→寄存器的拷贝布局让一个线程只持有一行里的若干元素，先 `.reduce` 把这些元素加成「该线程负责的部分行和」；但一行可能由同一 warp 内的多个线程共同覆盖，所以再用 `warp_reduce` 把这些线程的部分和归约成「每行一个值」。两步分工对应「线程内 K 维」与「warp 内同行线程」两个归约层次。

**练习 2**：若 `lse == -inf`（全掩码空行），预处理把 `lse_log2` 置 0。请说明为什么这不会让主循环算出错误的 P。

> **答**：`lse = −∞` 意味着该 query 行没有任何合法 key。主循环里 BlockInfo 会为这样的行返回空的 n_block 区间，主循环根本不执行任何 MMA，故 P 始终为 0，`dS = P·(dP−D) = 0`。`lse_log2` 置 0 仅为避免 `exp2(… − (−∞)) = +∞` 产生的 NaN，其具体数值不会被用到。

---

### 4.3 后处理：dQaccum 合并、延迟缩放与类型转换

（对应最小模块：**后处理：dQ 合并与类型转换**）

#### 4.3.1 概念说明

主循环结束时，`dQaccum` 里已经累加好了**未带 softmax 缩放**的 dQ：

\[ \texttt{dQaccum} = \sum_{n} dS_n\cdot K_n^{\top}\quad(\text{未乘 scale}) \]

回想反向公式 \(dQ=\text{scale}\cdot dS\cdot K^{\top}\)，scale 还没乘。后处理 kernel `FlashAttentionBackwardPostprocess` 要做三件事：

1. **合并**：`dQaccum` 是 fp32，跨 thread block 已经原子累加完毕，后处理只需逐 m_block 读出。
2. **延迟缩放**：乘上 `softmax_scale`。
3. **类型转换**：从 fp32 转成 fp16/bf16，写回最终的 `dq`。

第 2、3 步是**融合**在一起的——这是后处理最精妙的设计：既然无论如何都要为类型转换做一次「读 fp32 → 改 → 写 fp16」的遍历，那顺手把 scale 也乘上，就省掉了一次单独的缩放 pass。源码里这一行就是铁证：

```python
rdQ.store((acc.load() * scale).to(self.dtype))
```

即「读出 fp32 累加值 → 乘 scale → 转 fp16」一条龙。

**为什么 dQ 的 scale 能延迟？** 因为 \(dQ=\text{scale}\cdot\sum_n(dS_n K_n^\top)=\text{scale}\cdot\texttt{dQaccum}\)，scale 对求和可分配，完全可以提到求和之外、留到最后一次乘。

**与 dK/dV 的对比**：dK/dV 在主循环里是「寄存器内跨 m_block 累加、写一次」。当 `qhead_per_kvhead==1`（MHA）时，主循环 epilogue 直接乘 scale 写到 `dk/dv`，**不走后处理**；当 GQA（多 Q 头共享 KV）时，多个 Q 头要把梯度累加进同一个 `dk_accum/dv_accum`，此时主循环**故意不乘 scale**（注释见 [flash_bwd.py:867-869](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L867-L869)），把 scale 同样延迟到后处理。注意 `dv_accum` 用 scale=1.0，因为 `dV = Pᵀ·dO` 里 P 已经在前向/重计算时含了 scale，无需再乘。

#### 4.3.2 核心流程

后处理 kernel 同样按 `(m_block, head, batch)` 并行。非 2CTA 路径是一个教科书式的「gmem→smem→rmem→smem→gmem」五步搬运：

```
领工作块 (m_block, head_idx, batch_idx)
Step 1: gdQaccum (fp32)  --cp.async-->  sdQaccum (smem, fp32)
Step 2: sdQaccum          --autovec-->   acc (rmem, fp32)
        rdQ = (acc * scale).to(dtype)        # 延迟缩放 + 类型转换，融合在此
Step 3: rdQ (rmem, fp16)  --r2s atom-->  sdQ (smem, fp16)   # 复用同一块 smem
Step 4: sdQ (smem, fp16)  --autovec-->   tdQrdQ (rmem)      # 为合并写重排
Step 5: tdQrdQ            --coalesced--> gdQ (gmem, fp16)    # 带 head_dim OOB 谓词
```

关键在于 **smem 复用**：`sdQaccum`（fp32）和 `sdQ`（fp16）共用同一块 shared memory，只是 recast 指针类型（[:310-L321](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L310-L321)）。因为 fp16 比 fp32 省一半空间，读完后改写成 fp16 完全放得下，省了一半 smem。

Blackwell 上的 2CTA 路径（`use_2cta_instrs`，仅 SM100/SM110 且 hdim≠64）走另一套基于 tmem 的 reduce，原理相同（读 dQaccum → 乘 scale 转 dtype → 写回），但用 `tcgen05.copy` 把累加值映射进 tmem 视图再reduce，详见 [:376-L490](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L376-L490)。

#### 4.3.3 源码精读

构造函数按架构选择 MMA 与拷贝原子 [flash_bwd_postprocess.py:34-67](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L34-L67)，并断言支持 Ampere(8.x)/Hopper(9.x)/Blackwell(10.x,11.x,12.x)。`_get_tiled_mma` 为每个架构选合适的 tiled MMA [:91-L130](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L91-L130)，`_setup_attributes` 建立 fp32 dQaccum 的 g2s/s2r 拷贝与 fp16 dQ 的 smem 布局 [:132-L208](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L132-L208)。

**Step 1：gmem→smem（cp.async）** [flash_bwd_postprocess.py:492-499](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L492-L499)：

```python
cute.copy(g2s_tiled_copy_dQaccum, tdQgdQaccum, tdQsdQaccumg2s)
cute.arch.cp_async_commit_group()
cute.arch.cp_async_wait_group(0)
cute.arch.barrier()
```

**Step 2：smem→rmem + 延迟缩放 + 类型转换（融合点）** [flash_bwd_postprocess.py:528-532](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L528-L532)：

```python
tdQrdQaccum = cute.make_tensor(acc.iterator, cute.make_layout(tdQsdQaccum.shape))
cute.autovec_copy(tdQsdQaccum, tdQrdQaccum)
rdQ = cute.make_fragment_like(acc, self.dtype)
rdQ.store((acc.load() * scale).to(self.dtype))   # ★ scale 与 cast 融合
```

**Step 3：rmem→smem**（用 smem store atom，可能含转置 `dQ_swapAB`）[flash_bwd_postprocess.py:534-566](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L534-L566)。

**Step 4 & 5：smem→rmem→gmem**（为合并写重排后，按 head_dim 谓词写出）[flash_bwd_postprocess.py:568-587](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L568-L587)：

```python
for rest_m in cutlass.range(cute.size(tdQrdQ.shape[1]), unroll_full=True):
    if tdQcdQ[0, rest_m, 0][0] < seqlen_q - m_block * self.tile_m:
        cute.copy(gmem_tiled_copy_dQ, tdQrdQ[None, rest_m, None],
                  tdQgdQ[None, rest_m, None], pred=tdQpdQ[None, rest_m, None])
```

接口层 `_bwd_postprocess_convert` 把它包装成通用「fp32 累加器 → 目标 dtype」转换器 [interface.py:1270-1289](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1270-L1289)。主反向函数里对 dQ/dK/dV 的三次调用 [interface.py:1975-1999](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1975-L1999) 体现了「延迟缩放」的差异——注意三者传入的 `scale` 实参：

```python
_bwd_postprocess_convert(dq_accum, dq, softmax_scale, ...)   # dQ: scale
...
_bwd_postprocess_convert(dk_accum, dk, softmax_scale, ...)   # dK: scale (仅 GQA)
_bwd_postprocess_convert(dv_accum, dv, 1.0,             ...) # dV: 1.0   (仅 GQA)
```

这与本节 4.3.1 的数学推导完全吻合：dQ、dK 需要补 scale，dV 不需要。

> hd=256 专用 2CTA 反向 kernel 有自己**内置**的 dK/dV 后处理，因此主反向函数对它跳过这里的外部后处理（`if not use_dedicated_hd256_kernel:`，[interface.py:1966](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1966)）。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试，确认后处理产出与 `torch.autograd` 的 dQ 一致，并理解「延迟缩放」在数值上等价于「先缩放再累加」。

**操作步骤**：

1. 打开 [tests/cute/test_flash_attn.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py)，找到反向相关的用例（例如 `test_flash_attn_bwd_preallocated_outputs`，[tests/cute/test_flash_attn.py:1637](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/tests/cute/test_flash_attn.py#L1637)），记录它对 dQ 使用的容差（atol/rtol）。
2. 写一段「延迟缩放等价性」的对照（示例代码）：

```python
import torch
# 示例代码：演示 scale 可分配到求和外
scale = 0.125
dS = torch.randn(4, 8)      # 假装是各 n_block 的 dS 片段（已含 P、D）
K  = torch.randn(8, 64)
# (a) 先缩放再求和
dQ_a = sum((scale * dS) @ K for _ in range(1))   # 单块演示
# (b) 先求和(=dQaccum)再缩放
dQaccum = dS @ K
dQ_b = dQaccum * scale
print(torch.allclose(dQ_a, dQ_b))   # True —— 延迟缩放数学等价
```

**需要观察的现象**：`(a)` 与 `(b)` 在 fp32 下完全相等，验证「scale 可提到累加之外」，这正是后处理能延迟缩放的数学依据。

**预期结果**：`torch.allclose` 返回 `True`。在 GPU 上实测真实 dQ 数值一致性，**待本地验证**（需运行 `pytest tests/cute/test_flash_attn.py -k bwd -x`）。

#### 4.3.5 小练习与答案

**练习 1**：后处理为什么要做 gmem→smem→rmem→smem→gmem 这么多跳，而不是直接 gmem→rmem→gmem？

> **答**：直接 gmem→rmem→gmem 无法保证合并（coalesced）的全局写。中间过 smem 是为了把数据按「合并写友好的线程排布」重排：先用 smem store atom（与主循环 MMA 的 C 片段布局对齐）把 fp16 数据落入 smem，再用专门的 `gmem_tiled_copy_dQ` 按 128-bit 合并的方式读出再写 gmem。这样才能让全局 store 指令合并成少量大事务，撑满带宽。

**练习 2**：dV 的后处理 `scale` 为什么是 `1.0` 而 dK 是 `softmax_scale`？

> **答**：`dK = scale·dSᵀ·Q`，dS 不含 scale，故 dK 需要补 scale（且 GQA 时主循环故意没乘，延迟到这里）。而 `dV = Pᵀ·dO`，其中 P 在重计算时已经用 `exp2(S·softmax_scale_log2 − lse_log2)` 算出，**scale 已经进了 P**，所以 dV 不必再乘，`scale=1.0`。

---

## 5. 综合实践

**任务**：绘制反向三阶段（preprocess → 主循环 → postprocess）的输入输出张量数据依赖图，标明 `O`、`dO`、`LSE`、`D`(dpsum)、`lse_log2`、`dQaccum`、`dKaccum`、`dVaccum`、`dQ`、`dK`、`dV` 在三阶段之间的流动。建议用文字版流程图（或 mermaid）。

**参考答案（文字流程图）**：

```
                         ┌──────── 前向产物 ────────┐
                         │  O   dO   LSE  (q,k,v)    │
                         └─────────────┬─────────────┘
                                       │
                ┌──────────────────────▼──────────────────────┐
   预处理        │  FlashAttentionBackwardPreprocess            │
  preprocess     │  读 O,dO,LSE[,dLSE]                          │
                 │  产出: dpsum = (O⊙dO).rowsum [− dLSE]  (=D)  │
                 │        lse_log2 = LSE·log2e                  │
                 │        dQaccum := 0   (清零)                 │
                └──────┬───────────────┬───────────────┬────────┘
                       │               │               │
                    dpsum(D)      lse_log2        dQaccum(=0)
                       │               │               │
                ┌──────▼───────────────▼───────────────▼────────┐
   主循环        │  FlashAttentionBackwardSm80/90/100 (主循环)    │
   main loop     │  读 Q,K,V,dO + dpsum(=D) + lse_log2           │
                 │  重算 P = exp2(S·scale_log2 − lse_log2)       │
                 │  dS = P·(dP − D)                              │
                 │  dQaccum += dS·Kᵀ          (atomic, 无 scale)│
                 │  MHA: dK/dV = scale·acc 直接写出              │
                 │  GQA: dKaccum += dSᵀ·Q, dVaccum += Pᵀ·dO(无scale)│
                └──────┬──────────┬───────────┬───────────┬──────┘
                       │          │           │           │
                  dQaccum    dKaccum      dVaccum    (MHA: dk,dv 直接完成)
                       │          │           │
                ┌──────▼──────────▼───────────▼────────────────┐
   后处理        │  FlashAttentionBackwardPostprocess            │
  postprocess    │  dQ = dQaccum · softmax_scale   → fp16        │
                 │  dK = dKaccum · softmax_scale   → fp16 (GQA)  │
                 │  dV = dVaccum · 1.0             → fp16 (GQA)  │
                └──────┬──────────┬───────────┬──────────────────┘
                       │          │           │
                     dQ(fp16)  dK(fp16)    dV(fp16)
```

**核查要点**（请逐条对照源码确认）：

1. 预处理写出的 `dpsum` 在主循环里被当作 `D` 使用 [flash_bwd.py:600-601](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L600-L601)、参与 `dS = P·(dP−D)` [:977](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L977)。
2. 主循环收到的 LSE 参数其实是 `lse_log2`（base-2）[interface.py:1935](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/interface.py#L1935)。
3. dQ 的累加不带 scale [flash_bwd.py:1029-1041](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L1029-L1041)，scale 在后处理 [:532](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L532) 补上。
4. GQA 时 dK 的 scale 同样被延迟 [flash_bwd.py:867-869](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd.py#L867-L869)。

完成图后，请用一句话总结：**预处理是「备料」（D、lse_log2、清零 dQaccum），主循环是「重算+累加」（产出未缩放的 dQaccum/dKaccum/dVaccum），后处理是「收尾」（延迟缩放 + 类型转换）。**

## 6. 本讲小结

- 反向被拆成 **预处理 → 主循环 → 后处理** 三阶段；预处理与后处理都是按 `(m_block, head, batch)` 并行的轻量 kernel，主循环负责最重的重计算。
- 预处理产出三件「料」：行修正项 `D = (O⊙dO).rowsum`（可选减 `dLSE` 得 D′）、base-2 的 `lse_log2 = LSE·log₂e`、以及**清零的 `dQaccum`**——清零是主循环 `atomic_add` 正确性的前提。
- D 的计算是「fp32 元素积 → 沿 head_dim 求和 → warp 内归约 → 第 0 列线程写回」的两级归约；空行（`lse=−∞`）被安全化处理以避免 NaN。
- `dQaccum` 是跨 thread block 的 fp32 全局累加器，累加的是**未带 scale** 的 `dS·Kᵀ`；后处理把延迟的 `softmax_scale` 与 fp32→fp16 类型转换**融合在一步**完成（`rdQ.store((acc.load()*scale).to(dtype))`）。
- dK/dV 在 MHA 下由主循环 epilogue 直接写出（带 scale）；GQA 下改走 `dKaccum/dVaccum` + 后处理，其中 dK 补 `softmax_scale`、dV 用 `1.0`（因 P 已含 scale）。
- PDL 让预处理能与前向 kernel 尾部重叠，靠 `griddepcontrol_wait/launch_dependents` 保证 `O/dO/LSE` 已写完。

## 7. 下一步学习建议

- **横向对比三代反向**：本讲以 Ampere(SM80) 为主线。建议接着读 [flash_bwd_sm90.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm90.py) 与 [flash_bwd_sm100.py](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_sm100.py)，看 Hopper/Blackwell 反向是否复用同一套预处理/后处理 kernel（答案是复用，仅主循环与 dQ reduce 策略不同），这正是下一讲 u9-l3 的主题。
- **深入 2CTA 后处理**：本讲只点了 Blackwell 2CTA 后处理 [postprocess:376-490](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/flash_attn/cute/flash_bwd_postprocess.py#L376-L490) 的存在，其 tmem reduce 细节与 u8-l4 的 2CTA 死锁陷阱强相关，可结合 [AI/DEBUG_2CTA.md](https://github.com/Dao-AILab/flash-attention/blob/5835c733e7e9c07606b045255768e8a7e9e851bd/AI/DEBUG_2CTA.md) 一起读。
- **测试侧**：跑 `pytest tests/cute/test_flash_attn.py -k bwd -x` 观察反向用例的容差与参数化维度，并对照 `flash_attn/cute/testing.py` 的 `attention_ref` 参考实现，理解 D 与 dQ 的数值是如何被校验的（呼应 u11-l3）。
