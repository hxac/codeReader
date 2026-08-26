# split-KV 主循环：compute_attn_1rowblock_splitkv 的在线注意力

## 1. 本讲目标

本讲进入 BitDecoding 解码阶段真正吃掉绝大部分带宽与算力的 kernel：`flash_fwd_splitkv_kernel` 的核心函数 `compute_attn_1rowblock_splitkv`。学完本讲你应该能够：

1. 说清一个 CUDA block 拿到 `n_split_idx` 之后负责哪些 KV 块（`n_block_min` / `n_block_max` 的推导），以及"这个 split 没活干"时为什么还要写 `-inf`。
2. 定位每个 KV 块从 gmem → smem → 寄存器的完整拷贝代码，说出每一段对应的寄存器张量名（`mQ`、`sK_pack`、`tSrK_pack`、`tSrK_dequant`、`acc_s`、`acc_o`……）。
3. 画出一次循环迭代中"加载 → 反量化 → 第一次 GEMM（Q·Kᵀ）→ 在线 softmax → 第二次 GEMM（P·V）→ rescale"的数据流图。
4. 手推在线 softmax 的 rescale 公式与多 split 的 LSE 合并公式，并与 `softmax_rescale_o` / `combine_attn_seqk_parallel` 的代码逐行对应。

本讲只读"主 cache（已量化打包部分）"的解码路径；FP16 残余区由 `compute_attn_1rowblock_residualkv` 处理，是下一讲（u5-l4）的内容。LOP3 反量化的位操作细节在 u5-l3，本讲只把它当作黑盒调用点。

## 2. 前置知识

### 2.1 在线 softmax（streaming softmax）

注意力输出 \(O = \mathrm{softmax}(S)\,V\)，其中 \(S = QK^\top / \sqrt{d}\)。朴素做法要先把整个 \(S\) 算完才能做 softmax 的归一化，长上下文下 \(S\) 放不进显存更放不进寄存器。在线 softmax 的思路是**分块流式处理**，任意时刻只维护每行的三个量：

- \(m\)：目前见过的分数最大值（数值稳定项）；
- \(\ell = \sum_j e^{(s_j - m)\cdot\mathrm{scale}}\)：以 \(m\) 为基准的部分指数和；
- \(O = \sum_j e^{(s_j - m)\cdot\mathrm{scale}}\, V_j\)：以 \(m\) 为基准的部分加权和。

来了一个新块后，若最大值从 \(m\) 更新为 \(m'\)，旧的 \(\ell\) 与 \(O\) 都差一个因子 \(e^{(m-m')\cdot\mathrm{scale}}\)，乘上去再累加新块即可。这就是代码里 `softmax_rescale_o` 的 `rescale` 一词的含义。

### 2.2 cp.async 与软件流水

`cp.async` 是 SM80 引入的异步全局内存→共享内存拷贝指令。`cute::cp_async_fence()` 提交一批异步拷贝，`flash::cp_async_wait<0>()` 等待最近一批完成。本 kernel 用它实现两级流水：**在算当前块的 Q·Kᵀ 时，下一块的 K 已经在往 smem 搬；在算 P·V 时，K 的搬运与 softmax 重叠**。理解主循环的关键不是算术，而是"哪份数据此刻已经就位"。

### 2.3 CuTe 张量与 fragment 记号

本 kernel 的张量全是 CuTe 的 `make_tensor` 视图，命名约定（继承自 FlashAttention）：

| 前缀 | 位置 | 例 |
|---|---|---|
| `m` | 整个 gmem 矩阵 | `mQ` |
| `g` | gmem 上的当前 tile | `gK_pack` |
| `s` | shared memory tile | `sK_pack` |
| `tXgY` / `tXsY` | 拷贝的 gmem/smem 侧分区 | `tKgK_pack` |
| `tSrX` / `tOrX` | MMA 的 A / B 操作数寄存器 fragment | `tSrK_dequant` |
| `acc_x` | fp32 累积器 | `acc_s`、`acc_o` |

MMA 累积器布局记作 `(MMA=4, MMA_M, MMA_N)`：SM80 的 m16n8k16 MMA 每线程持有 4 个 fp32，`convert_layout_acc_rowcol` 会把它重排成 `(nrow=(2,MMA_M), ncol=(2,MMA_N))`，softmax 的行归约就在这个视图上做。

### 2.4 块尺寸常量（承接 u5-l1）

`kBlockM=16`、`kBlockN=256`、`kNWarps=4` 写死于 dispatch 层。打包侧常量由 `pack_num = 16/num_bits` 派生：4-bit 时 `kBlockN_pack=128`（一个打包 tile 的 token 数）、`kBlockP=64`（256/4，一个 K 块的 uint16 行数）；2-bit 时 `kBlockN_pack=256`、`kBlockP=32`。`kBlockK_params = kBlockN/group_size` 是 K 参数 tile 的行数（一个 256-token 块包含多少个量化组）。这些常量都定义在 [kernel_traits.h:L75-L111](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/kernel_traits.h#L75-L111)。

另请回忆 u2-l2 的结论：**主缓存长度 `seqlen_k_cache` 恒为 `residual_block_size`（4-bit 128、2-bit 256）的整数倍**，这决定了尾块的形状，本讲 4.4 节会反复用到。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [csrc/bit_decode/src/flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L561-L1268) | 本讲主体：`compute_attn_1rowblock_splitkv`（L561-L1268）、grid 解码包装 `compute_attn_splitkv`（L1685-L1695）、合并 kernel `combine_attn_seqk_parallel`（L1699-L1882） |
| [csrc/bit_decode/src/include/block_info.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L11-L46) | `BlockInfo`：从 `params` 解出本 batch 的实际序列长度 |
| [csrc/bit_decode/src/include/softmax.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L188-L310) | `scale_apply_exp2`、`Softmax::softmax_rescale_o`、`normalize_softmax_lse` |
| [csrc/bit_decode/src/include/utils.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L186-L287) | `gemm_Kchannel` / `gemm_Vtensor`：反量化 + MMA 的融合循环 |
| [csrc/bit_decode/src/flash_fwd_launch_template.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L76-L124) | `run_flash_splitkv_fwd`：三个 kernel 的启动顺序与 grid 形状 |

调用链回顾（承接 u3-l3）：`fwd_kvcache_int` → `mha_fwd_kvcache` → `set_params_splitkv`（决定 `num_splits`）→ `run_flash_splitkv_fwd` 依次启动 **residual kernel → splitkv kernel →（若 num_splits>1）combine kernel**。本讲的函数就是第二棒。

## 4. 核心概念与源码讲解

### 4.1 BlockInfo：这个 block 看到的 KV 到底有多长

#### 4.1.1 概念说明

decode 是逐 token 的：`seqlen_q = 1`（GQA 重排后变大，见 u3-l1），而 KV 来自两处——已量化打包的主缓存（长度 `opt_seqlens_k`）和 FP16 残余区。`BlockInfo` 是一个小结构体，把 `Flash_fwd_params` 里散落的长度字段换算成 kernel 里最常用的三个量：`actual_seqlen_q`、`seqlen_k_cache`（主缓存 token 数）、`actual_seqlen_k`（主缓存 + 残余）。它存在的原因很简单：主循环的块切分、越界掩码、早退判断全部依赖这几个数，与其在每个 kernel 里重复推导，不如统一封装。

#### 4.1.2 核心流程

```text
BlockInfo(params, bidb):
  若 Varlen 且 cu_seqlens_q 存在:  sum_s_q = cu_seqlens_q[bidb]      否则 -1
  若 Varlen 且 cu_seqlens_k 存在且 is_seqlens_k_cumulative: sum_s_k = cu_seqlens_k[bidb] 否则 -1
  actual_seqlen_q = cu_seqlens_q[bidb+1] - sum_s_q   （或标量 params.seqlen_q）
  seqlen_k_cache  = is_seqlens_k_cumulative ? cu_seqlens_k[bidb+1] - sum_s_k
                                         : cu_seqlens_k[bidb]        ← decode 走这支
  actual_seqlen_k = seqused_k ? seqused_k[bidb]
                              : seqlen_k_cache + (knew_ptr ? seqlen_knew : 0)
```

decode 路径的关键语义（承接 u3-l2）：`mha_fwd_kvcache` 把逐 batch 的已打包长度写进 `cu_seqlens_k` 并置 `is_seqlens_k_cumulative=false`（见 [decode_api.cpp:L473](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L473)、[decode_api.cpp:L502](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L502)），同时残余区 K/V 通过 `knew_ptr`/`seqlen_knew` 传入。于是：

- `seqlen_k_cache = cu_seqlens_k[bidb]`：主缓存里真实打包的 token 数；
- `actual_seqlen_k = seqlen_k_cache + seqlen_knew`：加上补零对齐后的残余块长度。

#### 4.1.3 源码精读

构造函数主体在 [block_info.h:L14-L25](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L14-L25)。第 22 行计算 `seqlen_k_cache`——`is_seqlens_k_cumulative` 为假时直接取 `cu_seqlens_k[bidb]`（注释也写明"we use cu_seqlens_k to store the sequence lengths of K"）；第 23 行计算 `actual_seqlen_k`，`knew_ptr` 非空时加上 `params.seqlen_knew`：

```cpp
, seqlen_k_cache((!Varlen || params.cu_seqlens_k == nullptr ? params.seqlen_k
      : (params.is_seqlens_k_cumulative ? params.cu_seqlens_k[bidb + 1] - sum_s_k
                                        : params.cu_seqlens_k[bidb])) - leftpad_k)
, actual_seqlen_k(params.seqused_k ? params.seqused_k[bidb] - leftpad_k
      : seqlen_k_cache + (params.knew_ptr == nullptr ? 0 : params.seqlen_knew))
```

两个偏移函数 [block_info.h:L27-L37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L27-L37)：`q_offset`/`k_offset` 在非 varlen（`sum_s_* == -1`）时退化为 `bidb * batch_stride`，即按 batch 维直接定位；varlen 时用累积和乘行stride。主 kernel 里所有 gmem 基址都从这两个函数出发。

#### 4.1.4 代码实践

1. **实践目标**：验证你对 `BlockInfo` 三个字段在 decode 语义下的理解。
2. **操作步骤**：打开 [evaluation/test.py](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/evaluation/test.py)，找到构造 `opt_seqlens_k` 与残余区（`k_new`/`v_new`）的代码；设 `seqlen_k=1000`、`num_bits=4`，写出第 1 轮 decode 时 `seqlen_k_cache` 与 `seqlen_knew` 的值。
3. **需要观察的现象**：按 u2-l2 的规则，prefill 后主缓存只收整块，`seqlen_k_cache = 768`（1000 中取 6×128），残余 232 个 token 补零到 256？——注意残余实际长度由 `new_lens` 传递，`seqlen_knew` 是 `k_new.size(1)`（见 [decode_api.cpp:L447](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/decode_api.cpp#L447)），即补零后的块容量。
4. **预期结果**：`seqlen_k_cache=768`、`seqlen_knew=256`（4-bit residual_block_size），`actual_seqlen_k=1024`。具体数值「待本地验证」——可在 test.py 中打印 `opt_seqlens_k` 与 `k_new.shape` 确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `seqlen_k_cache` 和 `actual_seqlen_k` 要分成两个字段，而不是只用一个总长度？
**答案**：splitkv kernel 读的是**打包后的主缓存**，有效列边界由 `seqlen_k_cache` 决定（拷贝谓词与掩码都用它）；而"整个 KV 序列有多长"决定块总数上界（`n_block_max` 的 ceiling 用 `actual_seqlen_k`）。两者混用会把残余区误当成已打包数据去读 uint16 位流。

**练习 2**：非 varlen 路径 `sum_s_k == -1` 时，`k_offset(batch_stride, row_stride, bidb)` 返回什么？
**答案**：`bidb * batch_stride + leftpad_k * row_stride`（[block_info.h:L33-L37](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/block_info.h#L33-L37)），即 batch 基址加左填充，供 gK/gV 基址计算复用。

### 4.2 softmax_rescale_o 与 scale_apply_exp2：主循环的心脏

#### 4.2.1 概念说明

`Softmax` 结构体（[softmax.h:L250-L310](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L250-L310)）封装在线 softmax 的全部状态：每线程 `row_max` 与 `row_sum` 两个长度 `kNRows = 2·MMA_M` 的寄存器数组（16 行 tile、4 warp 全铺在 N 维时 `kNRows=2`，即每个线程负责 2 行）。它对外只有两个方法：

- `softmax_rescale_o(acc_s, acc_o, ...)`：吃进本块的原始分数 `acc_s`，更新 `row_max/row_sum` 并把历史输出 `acc_o` 校正到新基准；
- `normalize_softmax_lse(acc_o, ...)`：循环结束后归一化并产出 LSE。

选用 `exp2` 而非 `exp` 是刻意的：把缩放因子 \(\log_2 e\) 预乘进 `scale_softmax_log2 = softmax_scale · log2(e)`，循环内只需要 `exp2f(x*scale - max*scale)` 一条 FFMA 加一条 SFU 指令，注释里明确写了这一点（[softmax.h:L200-L211](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L200-L211)）。

#### 4.2.2 核心流程

设历史状态为 \((m, \ell, O)\)，新块分数为 \(S\)（未乘 scale 的原始内积），缩放因子 \(\alpha = \mathrm{scale}\cdot\log_2 e\)：

\[
m' = \max(m, \max_j S_j),\qquad
c = 2^{(m - m')\,\alpha}
\]
\[
\ell' = c\,\ell + \sum_j 2^{(S_j - m')\,\alpha},\qquad
O' = c\,O + \sum_j 2^{(S_j - m')\,\alpha}\,V_j
\]

注意代码的实现顺序有个精妙之处：**`acc_o` 的乘 \(c\) 在 `gemm_Vtensor` 之前完成**，因此第二次 GEMM 累加进 `acc_o` 的就是新基准下的部分和，无需事后再校正。归一化与 LSE：

\[
O_{final} = O/\ell,\qquad
\mathrm{lse} = m\cdot\mathrm{scale} + \log \ell
\]

（\(\ell\) 是以 2 为底的指数和，\(\log \ell\) 换底后正好与 \(m\cdot\mathrm{scale}\) 量纲一致。）

多 split 合并：split \(i\) 各自输出归一化的 \(O_i\) 与 \(\mathrm{lse}_i\)。由 \(e^{s_j\cdot\mathrm{scale}} = e^{(s_j-m_i)\mathrm{scale}}\cdot e^{m_i\cdot\mathrm{scale}}\) 可得全局结果

\[
O = \sum_i e^{\mathrm{lse}_i - L}\,O_i,\qquad
L = \log\sum_i e^{\mathrm{lse}_i}
\]

这正是 u5-l5 要精读的 combine kernel 的公式，本讲先在 4.3.3 的 epilogue 处对上号。

#### 4.2.3 源码精读

**行归约原语**。`reduce_max_2`（[softmax.h:L175-L179](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L175-L179)）= 线程内 `thread_reduce_`（[L22-L36](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L22-L36)，沿 fragment 的列维折叠）+ `quad_allreduce_2`（[L69-L118](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L69-L118)）。`quad_allreduce_2` 先用 `warp_reduce_acc`（[L47-L67](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L47-L67)，`__shfl_down_sync` 蝴蝶归约）在**同行的 4 个线程**（m16n8k16 中一行由 4 个线程各持 2 列拼成）内归约，再经共享内存 `reduce_tmp` 跨 4 个 warp 汇总。一行 16 个元素恰好分布在 4 线程 × 4 warp 上，归约路径与 MMA fragment 的线程排布严格对齐。`reduce_sum`（[L181-L185](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L181-L185)）只有线程内归约，跨线程部分推迟到 `normalize_softmax_lse` 里的 `quad_allreduce_2(row_sum, ...)`（[L295](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L295)）——因为循环体内不需要 \(\ell\) 的精确值。

**`scale_apply_exp2`**（[softmax.h:L188-L214](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L188-L214)）把 `tensor(mi,ni)` 原地替换为 `exp2f(tensor*scale - max*scale)`；当 `max == -inf`（整行被掩码）时特判 `max_scaled = 0`，避免 `-inf - (-inf) = NaN`：

```cpp
const float max_scaled = max(mi) == -INFINITY ? 0.f : max(mi) * (Scale_max ? scale : float(M_LOG2E));
tensor(mi, ni) = exp2f(tensor(mi, ni) * scale - max_scaled);
```

**`softmax_rescale_o`**（[softmax.h:L258-L290](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L258-L290)）先做布局重排 `convert_layout_acc_rowcol`（[utils.h:L390-L397](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L390-L397)），然后分两支。首块支（`Is_first`，[L263-L266](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L263-L266)）：

```cpp
flash::template reduce_max_2</*zero_init=*/true>(scores, row_max, reduce_tmp);
flash::scale_apply_exp2(scores, row_max, softmax_scale_log2);
flash::reduce_sum</*zero_init=*/true>(scores, row_sum);
```

非首块支（[L267-L289](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L267-L289)）就是 4.2.2 公式的直译：

```cpp
Tensor scores_max_prev = make_fragment_like(row_max);
cute::copy(row_max, scores_max_prev);                    // 保存 m
flash::template reduce_max_2</*zero_init=*/false>(...);  // m' = max(m, max S)
float scores_scale = exp2f((scores_max_prev(mi) - scores_max_cur) * softmax_scale_log2);  // c
row_sum(mi) *= scores_scale;                             // ℓ ← c·ℓ
for (int ni ...) { acc_o_rowcol(mi, ni) *= scores_scale; }  // O ← c·O（在 PV 之前！）
flash::scale_apply_exp2(scores, row_max, softmax_scale_log2);
flash::reduce_sum</*zero_init=*/false>(scores, row_sum); // ℓ' = c·ℓ + Σ exp2(...)
```

**`normalize_softmax_lse`**（[softmax.h:L292-L309](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L292-L309)）：跨线程补齐 `row_sum`，除法归一化 `acc_o`，并产出 `lse = row_max * softmax_scale + __logf(sum)`；`sum==0`（整序列被掩码）时 `Split` 模式给 `-INFINITY`，让 combine 忽略这个 split。

#### 4.2.4 代码实践

1. **实践目标**：用一个 8 行 Python 脚本验证在线 softmax 与朴素 softmax 数值等价，并复现 rescale 公式。
2. **操作步骤**：随机生成 `S = randn(2, 8)`（2 块、8 列）、`V = randn(8, 4)`；先算朴素 `softmax(S.flatten()) @ V`；再按 4.2.2 公式手工分块累积（第一块 `Is_first`，第二块走 rescale 支），比较两者最大误差。
3. **需要观察的现象**：两结果差应在 `1e-6` 量级（float32）。
4. **预期结果**：等价；这证明 kernel 分块累积与一次性 softmax 数学一致，误差仅来自浮点舍入。（此实践纯 CPU，可直接运行验证。）

#### 4.2.5 小练习与答案

**练习 1**：把 `exp2f(x*scale - max_scaled)` 改写成 `expf` 形式应该是什么？为什么代码选 exp2？
**答案**：等价于 `expf((x - max) * softmax_scale)`，因为 \(2^{(x-m)\alpha} = e^{(x-m)\cdot\mathrm{scale}}\)。选 exp2 是因为乘法 \((x-m)\cdot\mathrm{scale}\) 可与减法融合成一条 FFMA，且 `exp2f` 直接映射 SFU 指令，省一次换底乘法（见 [softmax.h:L200-L205](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/softmax.h#L200-L205) 的注释）。

**练习 2**：`softmax_rescale_o` 里 `acc_o *= scores_scale` 为什么必须在 `gemm_Vtensor` 之前执行？放到循环末尾行不行？
**答案**：行不行取决于是否再次校正。放在 PV 之前，PV 累加进来的新块部分和天然以 \(m'\) 为基准，一次乘法覆盖全部历史；放到后面，则新块贡献也随 `acc_o` 一起被乘了 \(c\)，会错——除非给新块单独补偿 \(c^{-1}\)，徒增指令。

**练习 3**：`reduce_sum` 为什么不做跨线程归约，而 `reduce_max_2` 要做？
**答案**：行最大值必须完整才能安全做指数减法（数值稳定性），所以 max 必须立刻跨线程归约；而 `row_sum` 在循环内只是各线程的部分和，跨线程求和可以推迟到 epilogue 的 `normalize_softmax_lse` 一次完成，省掉每个块的同步开销。

### 4.3 compute_attn_1rowblock_splitkv：主 kernel 全景

#### 4.3.1 概念说明

这是 BitDecoding 的"重量级"kernel：处理**已量化打包的主 KV cache**。它解决的问法是：decode 时 batch×heads 个 block 远少于 SM 数（u3-l3 的"并行度饥饿"），因此把 KV 序列切成 `num_n_splits` 份，每份由一个 CUDA block 用在线 softmax 独立算出部分注意力，最后由 combine kernel 按 LSE 权重合并。与 residual kernel 的分工：splitkv 吃低比特主缓存（本讲），residual 吃 FP16 残余区并顺带再量化（u5-l4）。

grid 映射在包装函数 [compute_attn_splitkv（flash_fwd_kernel.h:L1685-L1695）](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1685-L1695)：`blockIdx.x → m_block`（Q 块），`Split` 时 `blockIdx.y → n_split_idx`、`blockIdx.z → bidb*h + bidh`。启动侧 [flash_fwd_launch_template.h:L82-L104](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L82-L104)：

```cpp
const int num_splits_ = params.num_splits - 1;          // 最后一个槽位留给 residual kernel
dim3 grid(num_m_block, num_splits_, params.b * params.h);
```

即 splitkv 的 `num_n_splits = gridDim.y = params.num_splits - 1`，residual kernel 固定写第 `params.num_splits - 1` 个累积槽（见 [flash_fwd_kernel.h:L1678](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1678)）——这就是 u3-l3 "num_splits 额外 +1" 的落点。

#### 4.3.2 核心流程

**第一步：算出我负责哪些块。** [flash_fwd_kernel.h:L614-L616](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L614-L616)

```cpp
const int n_blocks_per_split = ((seqlen_k_cache + kBlockN - 1) / kBlockN + num_n_splits - 1) / num_n_splits;
const int n_block_min = n_split_idx * n_blocks_per_split;
int n_block_max = std::min(cute::ceil_div(actual_seqlen_k, kBlockN), (n_split_idx + 1) * n_blocks_per_split);
```

本 split 负责半开区间 \([n\_block\_min,\; n\_block\_max)\) 里的 KV 块，**从 `n_block_max-1` 倒序遍历到 `n_block_min`**。倒序不是随意的：第一个处理的块是序列尾部（唯一可能不满、需要掩码的块），后续块全部整块，主循环因此拆成"1 步带掩码 + N 步不带掩码"两段。

**第二步：没活干就早退。** 若 `n_block_min >= n_block_max`（[L618-L657](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L618-L657)），向 `gOaccum` 写 0、`gLSEaccum` 写 `-INFINITY` 后 return——combine 时 \(e^{-\infty - L} = 0\)，该 split 自动被忽略；不写的话累积缓冲里是未初始化内存，合并结果会被污染。

**第三步：三级数据通路的准备**（详见 4.3.3 前半）：为 Q、K_pack、K_params、V_pack、V_params 五类数据各建 gmem/smem 视图与拷贝分区，为两次 GEMM 各建寄存器 fragment。

**第四步：主循环。** 每个块一次迭代，五阶段（软件流水嵌在其中）：

```text
对 n_block 从 n_block_max-1 递减到 n_block_min:
  ① 等待上一迭代预取的 K_pack/K_params 到位（cp_async_wait + syncthreads）
  ② 发出本块 V_pack/V_params 的异步加载（cp_async_fence）
  ③ 第一次 GEMM：gemm_Kchannel —— smem 的 uint16 位流 → 寄存器 → LOP3 反量化
     → half fragment → Q·Kᵀ MMA → acc_s (fp32)
  ④ mask（仅第一段循环）；softmax_rescale_o：更新 row_max/row_sum，rescale acc_o，
     scale_apply_exp2 把 acc_s 变成概率 fragment
  ⑤ 预取下一块 K（若 n_block > n_block_min）
  ⑥ acc_s → fp16 → 经 sAcc 转成 MMA 的 A 操作数
  ⑦ 第二次 GEMM：gemm_Vtensor —— V_pack 反量化 → P·V MMA → 累加进 acc_o
```

**第五步：epilogue。** `normalize_softmax_lse` 归一化 `acc_o` 并产出 `lse`，经 `sOaccum` 中转写回 `gOaccum`（Split 时是 float 累积缓冲）与 `gLSEaccum`，等 combine kernel 收割。

#### 4.3.3 源码精读

**(a) 切分与早退。** 上面已引 L614-L616 与 L618-L657。注意 `n_block_max` 的 ceiling 用的是 `actual_seqlen_k`（含残余），而第三步里所有拷贝谓词与掩码边界用的是 `seqlen_k_cache`（仅主缓存）——两者职责不同：前者决定"块空间总大小"，后者决定"打包数据实际有效到哪一列"。`params.num_splits > 1` 时才启动 combine（[flash_fwd_launch_template.h:L106-L124](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L106-L124)），并按 `num_splits` 落在哪个 2 的幂区间选 `Log_max_splits` 模板阶梯。

**(b) gmem 基址与张量。** 六个 row_offset（[L668-L694](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L668-L694)）全部锚定在 `(n_block_max - 1)` 块——因为遍历从尾块开始，K 指针随后只做减法推进（[L1021-L1022](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1021-L1022)）。三个代表性 gmem 张量（[L705-L717](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L705-L717)）：

```cpp
Tensor gK_pack   = make_tensor(..., Shape<Int<kBlockP>, Int<kHeadDim_k>>{}, ...);          // uint16 打包 K
Tensor gK_params = make_tensor(..., Shape<Int<kBlockK_params>, Int<kHeadDim_k_params>>{}, ...); // __half2 参数
Tensor gV_params = make_tensor(..., Shape<Int<kBlockN>, Int<kHeadDim_v_params>>{},
                               make_stride(_1{}, params.v_params_dim_stride));             // 注意行stride=1
```

`gV_params` 的行 stride 是 `_1{}`：承接 u2-l1/u3-l2 的布局结论——`v_params` 形状 (b, d/g, h, s)，序列维在最后一维，同一量化组沿序列连续，与 `gK_params`（序列在行维）的取法非对称。

**(c) smem 与寄存器。** smem 侧（[L723-L741](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L723-L741)）：`sQ`、`sK_pack`（+转置视图 `sK_pack_transposed`）、`sK_params`、`sV_pack`（+转置视图）、`sV_params`、`sAcc`（P 矩阵中转站）与 `sReduce_tmp`（softmax 归约用的临时区，与 `sAcc` 共享同一块 smem）。注意 `sK_dequant`/`sVtNoSwizzle_dequant` 只是**布局描述**（复用 `smem_Q` 的地址），它们的作用是给 `partition_fragment_B` 提供正确的 fragment 形状——反量化结果并不写回 smem，而是直接放寄存器。MMA 分区（[L804-L824](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L804-L824)）：

```cpp
Tensor tSrQ          = thr_mma.partition_fragment_A(sQ);                    // Q 的 A 片段（half）
Tensor tSrK_dequant  = thr_mma.partition_fragment_B(sK_dequant);            // K 的 B 片段（half，反量化目标）
Tensor tSrK_pack_tmp = thr_mma_KV_i4.partition_fragment_B(sK_pack_transposed);
Tensor tSrK_pack     = make_fragment_like<ElementKVPack>(tSrK_pack_tmp);    // 同形状的 uint16 片段
Tensor acc_o         = partition_fragment_C(tiled_mma, Shape<Int<kBlockM>, Int<kHeadDim>>{});
```

两套 MMA（u5-l1）：`TiledMma`（16×128×16）服务反量化后的 half 操作数；`TiledMmaKV_i4`（16×32×16）只用来给打包 uint16 数据切出 fragment 形状。smem→寄存器的拷贝分区在 [L844-L864](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L844-L864)：`tSsQ`（Q）、`tSsK_pack`+`tSrK_pack_view`（K 打包数据）、`tOsVt_pack`+`tOrVt_pack_view`（V 打包数据）。量化参数则直接从 smem 标量装载到 `tScales_*`/`tZeros_*` 寄存器数组（[L776-L802](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L776-L802)，`__half2` 视图）。

**(d) 循环前准备。** `clear(acc_o)`、Q 装载（[L893-L901](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L893-L901)）；构造 softmax 状态与掩码器（[L903-L905](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L903-L905)）——`Softmax<2 * size<1>(acc_o)>` 即 `kNRows = 2·MMA_M`；`mask_main` 以 `seqlen_k_cache` 为列边界。然后**预取尾块的 K**（[L911-L927](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L911-L927)）：

```cpp
int n_block = n_block_max - 1;
clear(tVsV_pack); clear(tVsV_params); clear(tKsK_pack); clear(tKsK_params);  // OOB 行补 0
const bool is_short_block = num_bits == 4 && (actual_seqlen_k - n_block*kBlockN) % kBlockN < residual_block_size;
const int k_params_div = is_short_block && Kernel_traits::quant_mode == 1 ? group_size : 1;
flash::copy<...>(gmem_tiled_copy_k_pack,   tKgK_pack,   tKsK_pack,   ..., (seqlen_k_cache - n_block*kBlockN) / k_pack_div);
flash::copy<...>(gmem_tiled_copy_k_params, tKgK_params, tKsK_params, ..., (seqlen_k_cache - n_block*kBlockN) / k_params_div);
```

边界参数的单位是"tile 行"：K_pack 的 tile 行是一个 uint16（含 `pack_num` 个 token），所以剩余 token 数除以 `k_pack_div = pack_num`；K_params 的 tile 行是一个量化组，所以除以 `group_size`。`is_short_block` 只在 4-bit 出现（`kBlockN=256 > kBlockN_pack=128`，尾块可能只有 128 个 token）：此时 params 边界必须换算成组数，否则会把"128 token = 1 组"误当成 128 行。2-bit 时 `kBlockN = kBlockN_pack = 256` 且主缓存长度恒为 256 的倍数，尾块必然是满块，无需特判。`clear(...)` 先把 smem tile 清零，越界列读到的补零值在 softmax 里会被掩码干掉。

**(e) 第一段：带掩码的迭代**（`n_masking_steps = 1`，[L929-L1070](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L929-L1070)）。每迭代：`cp_async_wait<0>` + `__syncthreads()` 等待 K 到位（[L935-L936](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L935-L936)）；发出本块 V 的异步加载（[L960-L968](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L960-L968)，用 `flash::copy` 带谓词，边界 `seqlen_k_cache - n_block*kBlockN` 个 token）；第一次 GEMM（[L974-L986](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L974-L986)）：

```cpp
flash::gemm_Kchannel<num_bits>(
    acc_s, tSrQ, tSrK_pack, tSrK_dequant,
    tScales_k_h2_c, tZeros_k_h2_c, sK_params,
    tSsQ, tSsK_pack, tiled_mma, smem_tiled_copy_Q, smem_tiled_copy_K_pack,
    smem_thr_copy_Q, smem_thr_copy_K_pack, num_params);
```

`gemm_Kchannel`（[utils.h:L237-L287](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L237-L287)）内部循环逐 K-fragment 做"拷 uint16 → 反量化 → MMA"三连：先一次性把整块的 scale/zero 从 `sK_params` 装进寄存器（[L265-L268](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L265-L268)），再在第 0 个 fragment 上示范了流水（[L270-L274](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L270-L274)）：

```cpp
cute::copy(smem_tiled_copy_B_i4, tCsB_i4(_, _, _0{}), tCrB_i4_copy_view(_, _, _0{}));   // uint16 → 寄存器
quant::dequant_Kchannel_Vtensor<num_bits>(tCrB_i4(_,_,_0{}), tCrB_dequant(_,_,_0{}),
                                          tCrB_scales(_,_,_0{}), tCrB_zeros(_,_,_0{}), num_params);  // LOP3
cute::gemm(tiled_mma, tCrA(_, _, i), tCrB_dequant(_, _, i), acc);                        // MMA
```

之后掩码（[L1010-L1012](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1010-L1012)）：launch 固定 `Is_even_MN=false`（[flash_fwd_launch_template.h:L98](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_launch_template.h#L98)），`Mask::apply_mask`（[mask.h:L128-L165](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/mask.h#L128-L165)）把 `col_idx >= seqlen_k_cache` 的列置 `-INFINITY`。接着等 V 就位、预取下一块 K（[L1014-L1040](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1014-L1040)，注释强调 fence 必须留在 if 块内否则出竞态）；softmax（[L1043-L1045](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1043-L1045)，首迭代 `Is_first=true`）；`acc_s` 转 fp16 经 `sAcc` 中转（[L1047-L1055](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1047-L1055)）；第二次 GEMM（[L1057-L1069](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1057-L1069)）——`gemm_Vtensor`（[utils.h:L186-L233](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/include/utils.h#L186-L233)）与 K 版结构相同，差别仅在于参数按 `load_params_Vtensor` 逐 fragment 装载（V 的组沿通道维，每个 K-fragment 属于不同组）。

**(f) 第二段：无掩码迭代**（[L1072-L1189](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1072-L1189)）。骨架与第一段完全同构，省略三处：不再调 `apply_mask`（块都是满的）；V/K 的推进用无谓词的 `cute::copy`（[L1093-L1094](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1093-L1094)）；softmax 恒走 `Is_first=false` 支（[L1164](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1164)）。k-tensor 分支（`quant_mode != 1`）整段被注释（[L1113-L1129](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1113-L1129)），印证 u3-l1"仅 k-channel 启用"。

**(g) epilogue。** [L1191-L1267](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1191-L1267)。`normalize_softmax_lse<false, Split>` 归一化并得 `lse`；`acc_o`（fp32）→ `sOaccum` → `gOaccum`。Split 时 `ElementO = ElementAccum`（float）、目标指针换成 `params.oaccum_ptr`，行偏移带上了 split 维（[L1218-L1222](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1218-L1222)）：

```cpp
const index_t row_offset_oaccum = (((n_split_idx * params.b + bidb) * params.h + bidh)
                                   * params.seqlen_q + m_block * kBlockM) * params.d_rounded;
```

LSE 写入 `gLSEaccum`，同样按 split 维寻址（[L1247-L1253](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1247-L1253)）。combine kernel 随后按 4.2.2 的合并公式收割：算 `lse_logsum`（[L1786](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1786)），把权重 `expf(lse - lse_logsum)` 存进 `sLSE`（[L1798-L1804](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1798-L1804)），逐 split 读 `gOaccum` 加权累加（[L1835-L1853](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L1835-L1853)），细节留给 u5-l5。

#### 4.3.4 代码实践

**实践目标**：给 `compute_attn_1rowblock_splitkv` 的主循环五个阶段做"行号标注 + 寄存器张量点名"，产出一张数据流图。这是本讲规格指定的核心实践。

**操作步骤**：

1. 打开 [flash_fwd_kernel.h](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L929-L1189)，用编辑器书签标注以下行（以第一段循环为准）：
   - 阶段① 等待 K：L935-L936；
   - 阶段② 加载 V（pack+params）：L960-L968；
   - 阶段③ 反量化 + 第一次 GEMM：L974-L986（真正体感在 `gemm_Kchannel`，utils.h L265-L286）；
   - 阶段④ 掩码 + 在线 softmax：L1010-L1012、L1043-L1045；
   - 阶段⑤ 预取下一块 K：L1017-L1040；
   - 阶段⑥ P 矩阵中转：L1047-L1055；
   - 阶段⑦ 第二次 GEMM：L1057-L1069。
2. 为每个阶段点名寄存器/smem 张量，填出下面这张表（答案已给出，建议先自己填再对照）：

   | 阶段 | 读 | 写 | 关键张量 |
   |---|---|---|---|
   | ① | `tKsK_pack`/`tKsK_params`（smem 目的侧） | — | `cp_async_wait<0>` |
   | ② | `tVgV_pack`/`tVgV_params`（gmem） | `tVsV_pack`/`tVsV_params`（smem） | 边界 = 剩余 token 数 |
   | ③ | `tSsQ`→`tSrQ`；`tSsK_pack`→`tSrK_pack`；`sK_params`→`tScales_k_h2_c`/`tZeros_k_h2_c` | `tSrK_dequant`、`acc_s` | uint16→LOP3→half→MMA |
   | ④ | `acc_s`、`row_max`/`row_sum`、`acc_o`、`sReduce_tmp` | `acc_s`（原地变概率）、`acc_o`（×c）、`row_max`/`row_sum` | `softmax_rescale_o` |
   | ⑤ | `tKgK_pack`/`tKgK_params`（指针已前移） | `tKsK_pack`/`tKsK_params` | 与 ③④ 重叠执行 |
   | ⑥ | `acc_s`（fp32） | `acc_s_fp16` → `tCsAcc_r2s` → `sAcc` | `R2SCopyAtomAcc` |
   | ⑦ | `tSsAcc_view`→`tSrAcc`；`tOsVt_pack`→`tOrVt_pack`；`sV_params`→`tScales_v_h2`/`tZeros_v_h2` | `tOrVt_dequant`、`acc_o` | `gemm_Vtensor` |

3. 把表格转画成数据流图（参考答案见下方"预期结果"）。
4. （可选，需重编译）kernel 内有现成的调试打印 [L659-L666](https://github.com/OpenBitSys/BitDecoding/blob/ae0d83630d6292453355ced498db2ac87f56ec62/csrc/bit_decode/src/flash_fwd_kernel.h#L659-L666)，编译时定义 `DEBUG` 宏即可在 blockIdx=(0,0,0) 的 block 打印 `n_blocks_per_split / n_block_min / n_block_max`。重编译方式见 u1-l2。「待本地验证」。

**需要观察的现象**：五阶段中 ③ 与 ②⑤ 的访存是重叠的（软件流水）；`acc_s` 是唯一被"消费即转写"的张量（④ 用完、⑥ 立刻转 fp16 进 smem）；`acc_o` 跨迭代存活，是唯一的持久累积器。

**预期结果**——数据流图参考答案：

```text
gmem                      smem                         寄存器
────                      ────                         ──────
gQ ──tQgQ→tQsQ────────▶ sQ ──tSsQ────────────▶ tSrQ (half, A片段)
gK_pack ──tKgK_pack──▶ sK_pack ──tSsK_pack───▶ tSrK_pack (uint16)
                                             │ quant::dequant_Kchannel_Vtensor (LOP3)
gK_params ──────────▶ sK_params ─load_params▶ tScales_k_h2_c/tZeros_k_h2_c
                                             ▼
                          tSrQ × tSrK_dequant ──TiledMma(16×128×16)──▶ acc_s (fp32)
                                             │ mask_main.apply_mask（仅第1段）
                                             │ softmax_rescale_o: row_max/row_sum,
                                             │   acc_o×c, scale_apply_exp2
                                             ▼
                          acc_s_fp16 ──tCrAcc_r2s──▶ sAcc (P矩阵)
                                             │ tSrAcc (A片段)
gV_pack ──tVgV_pack──▶ sV_pack ──tOsVt_pack─▶ tOrVt_pack (uint16)
                                             │ dequant (LOP3)
gV_params ──────────▶ sV_params ─load_params▶ tScales_v_h2/tZeros_v_h2
                                             ▼
                          tSrAcc × tOrVt_dequant ──MMA──▶ acc_o += (fp32)
epilogue: normalize_softmax_lse → lse; acc_o → sOaccum → gOaccum/gLSEaccum
        （Split=true 时写 float 累积缓冲，等待 combine kernel 按 LSE 加权合并）
```

#### 4.3.5 小练习与答案

**练习 1**：设 4-bit、`kBlockN=256`、`seqlen_k_cache=768`、`actual_seqlen_k=1024`（残余 256）、`num_n_splits=3`。求每个 split 的 `[n_block_min, n_block_max)`。
**答案**：`ceil(768/256)=3` 个块，`n_blocks_per_split = ceil(3/3)=1`。split0：`[0, min(ceil(1024/256)=4, 1))=[0,1)`；split1：`[1,2)`；split2：`[2, min(4,3))=[2,3)`。三个 split 各拿 1 块；`actual_seqlen_k` 的 ceiling（4）比块总数（3）大也没关系，`min` 起保护作用。若把 `num_n_splits` 改成 2：`n_blocks_per_split=ceil(3/2)=2`，split0=`[0,2)`、split1=`[2,3)`——总块数 3 < 供给 4，最后一个名义槽天然空缺。

**练习 2**：接上题，若 `num_n_splits=4`（即 `params.num_splits=5`，residual 占第 4 槽），split3 的区间是什么？它会走哪段代码？
**答案**：`n_blocks_per_split = ceil(3/4) = 1`；split3：`n_block_min=3, n_block_max=min(ceil(1024/256)=4, 4)=4`，区间 `[3,4)` 非空、仍会执行——但该块边界 `seqlen_k_cache - 3*256 = 0`，全部列被掩成 `-inf`。真正触发早退分支（L618）需要 `n_block_min >= n_block_max`，例如 `seqlen_k_cache=512`、残余 256、`num_n_splits=4` 时（块数 2，`n_blocks_per_split=1`，ceiling `ceil(768/256)=3`）：split0=`[0,1)`、split1=`[1,2)`、split2=`[2,3)`（全掩码空块）、split3=`[3,3)`（早退，向 LSE 写 `-INFINITY`）。两种"无贡献"路径（全掩码 vs 早退）殊途同归：combine 权重为 0。

**练习 3**：`is_short_block`（L918）为什么带 `num_bits == 4` 前置条件？
**答案**：只有 4-bit 满足 `kBlockN(256) > kBlockN_pack(128)`，尾块 token 数可能是 128（256 的非零余数且小于 128 不可能，因为主缓存长度是 128 的倍数——余数只能是 128）；此时 params 边界必须从 token 数换成组数（除以 `group_size`）才不至于把 128 当成 128 行。2-bit 时 `kBlockN = kBlockN_pack = 256`，主缓存长度是 256 的倍数，尾块恒满，`actual_seqlen_k` 多出的残余长度只会让 ceiling 多出一块"全掩码空块"，不影响 params 边界。

**练习 4**：为什么 V 的拷贝放在迭代开头（②），K 的预取放在中间（⑤），而不是都放在迭代开头？
**答案**：这是三级流水的错峰安排：迭代开始时 K 已在 smem（上一迭代 ⑤ 发出），腾出异步通道先拉 V（本迭代 ⑦ 要用）；③④ 消费 K 的同时，V 在路上；③④ 完成后 V 已到位，此时再发下一块的 K，让 ⑥⑦ 执行期间 K 在路上。每时刻异步拷贝、smem→寄存器、MMA 三类资源都不空转。

## 5. 综合实践

**任务：一次完整 decode 步的全流程推演手册。** 配置：`b=1, h=32, d=128, num_bits=4, group_size=128, kBlockN=256`，prefill 后 `seqlen_k_cache=768`，当前残余区补零到 256（`seqlen_knew=256`），假设启发式给出 `params.num_splits=4`（splitkv 占 grid.y 前 3 层，residual 写第 3 槽）。请完成：

1. **块分配表**：按 4.3.5 练习 1 的方法列出 split0/1/2 各自的 `[n_block_min, n_block_max)`、处理的 token 范围、每个 split 内两段循环（掩码段/无掩码段）各执行几次。
2. **单块五阶段清单**：任选一个中间块（如 split1 的块 1），按 4.3.4 的表格写出五阶段的输入输出张量名，并标注 K_pack 该块的形状（`(kBlockP=64, kHeadDim_k=128)` 的 uint16 tile）与 K_params 形状（`(kBlockK_params=2, kHeadDim_k_params=128)` 的 half2 tile）。
3. **softmax 推演**：设 split1 处理的两块行最大值先后为 \(m_1=3.2\)、\(m_2=2.1\)，写出第二次 `softmax_rescale_o` 调用时 `scores_scale` 的表达式（答：\(2^{(3.2-2.1)\alpha}\)，\(\alpha=\mathrm{scale}\cdot\log_2 e\)，即历史 `row_sum`/`acc_o` 被放大而非缩小——因为新块最大值更小）。
4. **合并权重**：若三个 splitkv 与 residual 共 4 份的 LSE 为 `(2.0, 2.5, 1.8, 2.3)`，写出 combine 中每份的权重（答：\(e^{\mathrm{lse}_i - L}\)，\(L=\log(e^{2.0}+e^{2.5}+e^{1.8}+e^{2.3})\approx 3.572\)，四份权重约 0.208/0.342/0.170/0.280，和为 1——即 softmax(lse)，可手算验证）。
5. （可选，有 GPU 时）用 `evaluation/test.py` 把 `seqlen_k` 改成 768+256 的组合，观察 MAE 输出；对照你的推演表解释每个 decode 轮次各 split 的工作量变化。「待本地验证」。

完成后你应当能脱稿回答：**"一个 KV 块从 HBM 里的 uint16 位流变成 acc_o 里的一列加权和，中间经过了哪几站、每一站在哪个文件哪一行？"**

## 6. 本讲小结

- `BlockInfo` 把 decode 语义（`cu_seqlens_k` 存逐 batch 已打包长度、`knew_ptr` 存 FP16 残余）换算成 `seqlen_k_cache`（主缓存 token 数，拷贝谓词与掩码的边界）与 `actual_seqlen_k`（含残余的总长，块数上界的边界）——两个长度各司其职，不可混用。
- `compute_attn_1rowblock_splitkv` 按 `n_blocks_per_split = ceil(块数/num_n_splits)` 把 KV 块区间 `[n_block_min, n_block_max)` 分给每个 `n_split_idx`，从尾块**倒序**遍历：第一段（1 步）带谓词拷贝与掩码，第二段全部满块直通车；空 split 早退写 0/-inf，让 combine 自动忽略。
- 主循环五阶段的数据通路：`gK_pack/gV_pack`(uint16) → smem → 寄存器 `tSrK_pack/tOrVt_pack` → LOP3 反量化 → `tSrK_dequant/tOrVt_dequant`(half) → 两次 MMA 分别累积进 `acc_s` 与 `acc_o`；量化参数从 `sK_params/sV_params` 装进 `tScales_*/tZeros_*`。反量化与 GEMM 在 `gemm_Kchannel/gemm_Vtensor` 内逐 fragment 融合，不回写 smem。
- 在线 softmax 状态只有 `row_max/row_sum/acc_o` 三个寄存器量：`softmax_rescale_o` 在 PV **之前**把 `acc_o` 乘上 \(c = 2^{(m-m')\alpha}\)，使新块贡献天然对齐新基准；`scale_apply_exp2` 用 exp2+FFMA 并特判全 `-inf` 行。
- epilogue 按 `Split` 分流：直接写 `gO`，或写 float 的 `gOaccum/gLSEaccum`（行偏移带 split 维），交给 `combine_attn_seqk_parallel` 以 \(e^{\mathrm{lse}_i - L}\) 权重合并——每个 split 输出的已是归一化局部 softmax，合并公式因此格外简洁。

## 7. 下一步学习建议

本讲把 `gemm_Kchannel`/`gemm_Vtensor` 里的 `quant::dequant_Kchannel_Vtensor` 当黑盒。下一讲 **u5-l3（LOP3 快速反量化）** 打开这个黑盒：`0x6400` 指数魔数如何把 nibble 变成合法 half、SUB/MUL/ADD 常量如何融合 scale/zero。之后 **u5-l4（残余 kernel）** 讲同一文件里的姊妹函数 `compute_attn_1rowblock_residualkv`——它处理 FP16 残余区并在攒满一个块时**在 kernel 内**调用第四单元的 qpack 原语完成再量化，与本讲构成完整解码闭环；**u5-l5** 再精读 `combine_attn_seqk_parallel` 的 LSE 合并实现。建议按此顺序阅读，并回头用本讲 4.3.4 的数据流图对照每一讲的新细节挂载点。
