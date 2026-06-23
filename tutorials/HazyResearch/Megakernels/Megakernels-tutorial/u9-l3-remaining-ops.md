# 其余 op：MatVecAddOp 与 attention_reduction

> 阶段：advanced · 依赖：u8-l3（通用 matvec 流水线 `matvec_pipeline` / `rms_matvec_pipeline`）、u9-l1（`attention_partial`）、u9-l2（跨 op 全局屏障 `Bar`）

## 1. 本讲目标

本讲收尾「低延迟 Llama」这条 op 流水线里**还没讲过的最后几个 op**。它们没有引入全新的执行模型，而是**复用前面已经搭好的两套骨架**：

- `MatVecAddOp` 复用 `matvec_pipeline`，用一份模板服务 `o_proj` 与 `down_proj` 两个残差累加 op；
- `attention_reduction` 是 attention 阶段的「归并器」，把多个 `attention_partial` 的局部结果用 **log-sum-exp（LSE）** 合并成最终注意力输出；
- `upgate`（`rms_upgate_silu`）与 `rms_lm_head` 复用带 RMSNorm 的 `rms_matvec_pipeline`，遵循和 `rms_qkv_rope_append` 一模一样的流水线模式。

学完本讲，你应当能够：

1. 说清楚 `MatVecAddOp` 这一个模板如何通过**模板参数**同时表达 `o_proj` 与 `down_proj`，并指出两者在「权重张量、输入/输出激活指针、opcode、`EXPECTED_ARRIVAL_COUNT`」上的具体差异。
2. 解释 `tma::store_add_async`（TMA 原子加法存储）为什么是实现「残差累加 `hidden_states += 投影(...)`」的正确原语，以及它和普通 `store_async` 的语义差别。
3. 推导 `attention_reduction` 里 **LSE 合并**的数学公式，并说清楚它为什么全程使用**以 2 为底**的 `exp2f / log2f`。
4. 对比 `attention_reduction` 与 `attention_partial` 对 LSE 的**不同处理粒度**（partial 级合并 vs. KV block 级在线 softmax）。
5. 一眼看出 `upgate` 与 `rms_lm_head` 只是换了 `load_iter` / `store` 两个回调的「流水线填空题」。

---

## 2. 前置知识

先用大白话对齐几个概念（细节都在依赖讲义里，这里只做最小回顾）。

**通用 matvec 流水线做什么？**

`matvec_pipeline`（见 [matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh)）把一次「矩阵-向量乘」拆成流水线：

- **loader**：用 TMA 把权重块（`st_bf<16, 512>`）一块块搬进共享内存；
- **consumer**：每个 warp 取自己负责的那段**归约维度（reduction dim）**切片，算出一个 16 元素的部分和 `out_smem`，再用 `matvec_reduce` 跨 warp 求和得到完整的 16 元素输出；
- **storer**：把输出写回全局内存。

`rms_matvec_pipeline` 在它前面**多加一步 RMSNorm**：先 TMA 读入 RMS 缩放因子与激活，做 `rms_norm`，归一化后的向量再喂给 `consumer_loop`。本讲的 `upgate` 与 `rms_lm_head` 都继承自 `rms_matvec_pipeline`。关键设备函数 `matvec` / `matvec_reduce` / `rms_norm` 的定义在 [utils.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh)。

> 关键回顾：`matvec` 是 **split-K** 的——归约维度被 `NUM_CONSUMER_WARPS` 个 warp 切片并行，每个 warp 只算一段；`matvec_reduce` 把这些部分和跨 warp 加起来。`MatVecAddOp` 完全复用这套机制，本讲不重复推导，只看它「填了哪些回调」。

**什么是「残差累加」？**

Transformer 里有两处「加上残差」：

- 注意力之后：`hidden_states += o_proj(attn_out)`；
- MLP 之后：`hidden_states += down_proj(silu(upgate(hidden_states)))`。

这两步在数值上都是「读旧值、加新值、写回同一地址」。在 GPU 语义里，这就是一个**原子加法（atomic add）**。Megakernels 用 TMA 的 `store_add_async`（加法归约存储）在硬件层面一次完成，既实现了残差，又顺便实现了跨指令的 split-K 归约。

**什么是 LSE（log-sum-exp）？**

注意力 softmax 的分母是 \(Z=\sum_i e^{s_i}\)，直接存 \(Z\) 会溢出，所以存它的对数 \(l=\log Z\)，即 LSE。把「两段已经各自算好 \((O, l)\) 的部分注意力结果」合并成一个全局结果，需要一套**数值稳定的 LSE 合并公式**——这正是 `attention_reduction` 的核心数学，细节在 4.2 推导。

**opcode 与屏障的约定（回顾 u9-l2）**

每个 op 有一个 opcode（见 [llama.cuh:7-13](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L7-L13)）。op 完成后用 `atomicAdd` 往 `g.Bar[layer, opcode-1, head]` 投票；下游 op 在 `gmem_wait` 里自旋等这个槽位涨到阈值 `EXPECTED_ARRIVAL_COUNT`。本讲会反复用到这条链。

| opcode 宏 | 值 | op | 含义 |
| --- | --- | --- | --- |
| `OPCODE_RMS_QKV_MatVecRopeAppend` | 1 | `rms_qkv_rope_append` | RMSNorm + QKV 投影 + RoPE + 追加 KV |
| `OPCODE_PartialAttention` | 2 | `attention_partial` | 单段 KV 上的 flash attention |
| `OPCODE_AttentionReduction` | 3 | `attention_reduction` | 合并多段 partial |
| `OPCODE_O_ProjResidual` | 4 | `o_proj` | 输出投影 + 残差 |
| `OPCODE_RMS_DoubleMatVecSiLU` | 5 | `rms_upgate_silu` | RMSNorm + up/gate 双投影 + SiLU |
| `OPCODE_DownProjResidual` | 6 | `down_proj` | down 投影 + 残差 |
| `OPCODE_RMS_LM_Head` | 7 | `rms_lm_head` | 最终 RMSNorm + lm_head → logits |

一条单层前向的数据流是：

```
rms_qkv_rope_append ──► attention_partial ──► attention_reduction ──► o_proj(+残差)
        │                                                                  │
        └──────────────► (KV cache)                                        ▼
                                                          rms_upgate_silu ──► down_proj(+残差) ──► 下一层 / lm_head
```

第 4、6 步的「+ 残差」就是本讲模块 4.1 的 `store_add_async`；第 3 步是模块 4.2 的 LSE 合并。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demos/low-latency-llama/matvec_adds.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu) | **模块 4.1 主角**。定义模板 `MatVecAddOp`，并在底部用两行 `struct ... : MatVecAddOp<...>` 特化出 `downproj` 与 `o_proj`。 |
| [demos/low-latency-llama/attention_reduction.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu) | **模块 4.2 主角**。读取多个 partial 的 \((O, l)\)，用 LSE 公式合并，写出最终 `attn_out`。 |
| [demos/low-latency-llama/attention_partial.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu) | 模块 4.2 的对照对象。生产每个 partial 的 \((O_{\text{partial}}, l_{\text{partial}})\) 并写入中间张量。 |
| [demos/low-latency-llama/upgate.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu) | **模块 4.3**。`rms_upgate_silu`：up/gate 交替加载，SiLU 门控，写出 `silu_out`。 |
| [demos/low-latency-llama/rms_lm_head.cu](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu) | **模块 4.3**。`rms_lm_head`：最后一层 RMSNorm + lm_head，写出 `logits`。 |
| [demos/low-latency-llama/matvec_pipeline.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh) | 通用流水线 `matvec_pipeline` / `rms_matvec_pipeline`。本讲的 op 都把脏活交给它。 |
| [demos/low-latency-llama/utils.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh) | `matvec` / `matvec_reduce` / `rms_norm` 设备函数。 |
| [demos/low-latency-llama/llama.cuh](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh) | `globals_t`：权重/激活张量成员、`Bar` 屏障、维度常量、opcode 宏。 |

---

## 4. 核心概念与源码讲解

### 4.1 MatVecAddOp 模板：一份源码服务 o_proj / down_proj 两个残差累加

#### 4.1.1 概念说明

`o_proj` 和 `down_proj` 在数学上几乎是同一件事：

\[
\text{out}[\text{block}] \;\mathrel{+}= \; W \cdot \text{in}
\]

即「把输入激活做一个线性投影，再把结果**累加**到输出残差流上」。两者的区别只在「投影的输入维度、用哪块权重、等谁的数据」这些**参数**上，而**执行结构完全相同**（都是 split-K 的 matvec 流水线 + 原子加法存储）。

这正是模板的用武之地：`MatVecAddOp` 把「结构」写死，把「参数」抽成模板参数，于是 `o_proj` 与 `down_proj` 各自只需要一行特化。先看模板的参数列表（[matvec_adds.cu:10-16](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L10-L16)）：

| 模板参数 | 作用 | o_proj 取值 | down_proj 取值 |
| --- | --- | --- | --- |
| `_EXPECTED_ARRIVAL_COUNT` | consumer 自旋等待上游屏障到达多少票 | `num_attention_heads` = 32 | `hidden_dim / matvec_block_size` = 2048/16 = 128 |
| `WeightsPtr` | 指向 `globals` 里权重成员的**指针-到-成员** | `&Globals::o_weights` | `&Globals::down_weights` |
| `InputActivationsPtr` | 输入激活的成员指针 | `&Globals::attn_out` | `&Globals::silu_out` |
| `OutputActivationsPtr` | 输出（残差流）的成员指针 | `&Globals::hidden_states` | `&Globals::hidden_states` |
| `_opcode` | 本 op 的 opcode（决定往 `Bar` 哪个槽投票） | `OPCODE_O_ProjResidual` (4) | `OPCODE_DownProjResidual` (6) |
| `_prev_opcode` | 上游 op 的 opcode（决定等哪个槽） | `OPCODE_AttentionReduction` (3) | `OPCODE_RMS_DoubleMatVecSiLU` (5) |

> 注意 `g.*WeightsPtr` 这种写法：`WeightsPtr` 是「指向 `globals` 成员的指针」，`g.*WeightsPtr` 在运行时解引用成具体的张量引用（如 `g.o_weights`）。这是 C++「成员指针」机制，让模板能在**编译期**绑定到一个具名成员，而运行时只花一次解引用。

两个特化只有这一行（[matvec_adds.cu:181-192](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L181-L192)）：

```cpp
template <typename Config, typename Globals>
struct downproj : MatVecAddOp<llama_1b_globals::hidden_dim / llama_1b_globals::matvec_block_size,
                              &Globals::down_weights, &Globals::silu_out,
                              &Globals::hidden_states, OPCODE_DownProjResidual,
                              OPCODE_DownProjResidual - 1, Config, Globals> {};

template <typename Config, typename Globals>
struct o_proj : MatVecAddOp<llama_1b_globals::num_attention_heads,
                            &Globals::o_weights, &Globals::attn_out,
                            &Globals::hidden_states, OPCODE_O_ProjResidual,
                            OPCODE_O_ProjResidual - 1, Config, Globals> {};
```

差异就藏在 `_EXPECTED_ARRIVAL_COUNT`、三个成员指针、两个 opcode 里——这正是本讲第一个实践任务要你讲清楚的事。

#### 4.1.2 核心流程

`MatVecAddOp` 自己不实现流水线主体，而是把回调交给 `matvec_pipeline`：

```
struct MatVecAddOp {
    using pipeline = matvec_pipeline<Config, Globals, parsed_instruction, pipeline_specifics>;
    ── controller : 转发 pipeline::release_lid / init_semaphores
    ── loader     : 调 pipeline::loader_loop        （搬权重）
    ── launcher   : Blackwell tensor 就绪栅栏
    ── consumer   : 等上游屏障 → 读输入激活 → pipeline::consumer_loop （做 matvec）
    ── storer     : pipeline::storer_loop           （每个输出块回调 store）→ 一次性 += iters 投票
    └─ pipeline_specifics : 提供 load_iter / store 两个回调
}
```

对每个 16 元素输出块，数据流是：

```
权重 W[layer, block*16, reduction_col + 512*col]   ──load_iter──► 共享内存
                                                                    │
输入激活 in[start_reduction_col + warp*REDUCTION_DIM_PER_WARP] ──► consumer
                                                                    ▼
                                              matvec（split-K，每 warp 一段归约维度）
                                                                    │
                                              matvec_reduce：跨 warp 把部分和加成完整 16 元素输出
                                                                    │
                                              store_add_async：把 16 元素「原子加」进 hidden_states[block_idx]
                                                                    ▼
                                                       hidden_states[block_idx] += 投影(in)
```

这里有一个关键设计点：**输入的归约维度被切成若干 `reduction_block`**。每个 `reduction_block` 覆盖 `hidden_dim = 2048` 列（对应 4 个 `st_bf<16,512>` 权重块，共 4×512=2048 列）。

- `o_proj` 的输入 `attn_out` 宽度 = `hidden_dim = 2048` → 只有 1 个 reduction block（`reduction_block_idx = 0`）；
- `down_proj` 的输入 `silu_out` 宽度 = `intermediate_dim = 8192` → 有 4 个 reduction block（`reduction_block_idx = 0,1,2,3`，即列起点 0, 2048, 4096, 6144，注释见 [matvec_adds.cu:31-33](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L31-L33)）。

每个 reduction block 由一条指令处理，算出**完整输出维度**的部分投影，再用 `store_add_async` 原子累加进同一个 `hidden_states[block_idx]`。于是 4 条 down_proj 指令（不同 reduction block，可能跑在不同 SM 上）的部分和，被 `store_add_async` 自动加总——这就是「跨指令 / 跨 SM 的 split-K 归约」，也是 `store_add_async` 必须用**原子加**而不是普通写的根本原因。

#### 4.1.3 源码精读

**(a) 指令解析 `parsed_instruction`**（[matvec_adds.cu:21-38](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L21-L38)）——把裸 `int` 数组翻译成语义字段：

| 槽位 | 字段 | 含义 |
| --- | --- | --- |
| `instruction[1]` | `layer` | 层号 |
| `instruction[2]` | `start_block_idx` | 起始输出块号（单位 1 个 16 元素块） |
| `instruction[3]` | `end_block_idx` | 结束输出块号 |
| `instruction[4]` | `reduction_block_idx` | 归约维度分块号，单位 `hidden_dim=2048`（0, 2048, 4096, 6144） |
| 派生 | `start_reduction_col = reduction_block_idx * hidden_dim` | 输入激活的列起点 |
| 派生 | `iters = end_block_idx - start_block_idx` | 本指令处理多少个输出块 |

**(b) `load_iter`：搬一块权重**（[matvec_adds.cu:42-53](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L42-L53)）。用 `tma::load_async<ROW, EVICT_FIRST>` 从 `g.*WeightsPtr` 搬一块 `st_bf<16,512>`，三维坐标是 `{layer, (start_block_idx+iter)*matvec_block_size, start_reduction_col + 512*col_idx}`——行维选输出块、列维选归约分块里的 512 列段。

**(c) `store`：归约 + 残差原子加**（[matvec_adds.cu:55-84](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L55-L84)），这是本模块最核心的几行：

```cpp
kittens::rv_fl<16> output_rv;
matvec_reduce<Config, kittens::sv_fl<16>, kittens::rv_fl<16>,
              pipeline::SCRATCH_BYTES_PER_WARP>(output_scratch_start, output_rv); // 跨 warp 归约
kittens::warp::sync();
kittens::warp::store(output_smem_bf, output_rv);                                  // float→bf16 回写 smem
kittens::warp::sync();
if (kittens::warp::laneid() == 0) {
    auto &OutputActivations = g.*OutputActivationsPtr;
    kittens::tma::store_add_async<cache_policy::EVICT_LAST>(                      // ★ 原子加法存储
        OutputActivations, output_smem_bf, {block_idx});
    kittens::tma::store_async_read_wait();
}
```

- [L67-L69](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L67-L69)：`matvec_reduce`（见 [utils.cuh:103-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/utils.cuh#L103-L120)）把 `NUM_CONSUMER_WARPS` 个 warp 各自写在 scratch 区的 16 元素部分和跨 warp 加成一个完整 16 元素输出 `output_rv`。
- [L78-L80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L78-L80)：`tma::store_add_async` 是 TMA 的**加法归约存储**原语（Hopper/Blackwell 上对应 `cp.reduce.async.bulk.tensor` 一类的 reduce-store 指令）。语义是 `dst[block_idx] += src`，而不是覆盖。`EVICT_LAST` 让这条残差流尽量留在 L2，方便下游立刻读。

> **对比普通 `store_async`**：`rms_lm_head` 的 `store`（见 4.3）用的是 `tma::store_async`，因为 logits 是「一次性写出」，没有残差也没有跨指令归约，覆盖写即可。`MatVecAddOp` 必须用 `store_add_async`，否则第二条 down_proj 指令会冲掉第一条的部分和。

**(d) `consumer`：等上游 + 读输入激活**（[matvec_adds.cu:116-158](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L116-L158)）。关键两段：

- [L129-L133](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L129-L133)：自旋等 `g.Bar[{layer, prev_opcode-1, reduction_block_idx}] >= EXPECTED_ARRIVAL_COUNT`。这正是 u9-l2 讲的 `volatile` 读 + `__nanosleep` 等待模式；`prev_opcode-1` 指向上游 op 的屏障槽，`reduction_block_idx` 把等待范围缩小到「本归约分块对应的那段输入」。
- [L145-L147](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L145-L147)：每个 warp 用 `warp::load` 读自己负责的输入切片 `in[start_reduction_col + warpid()*REDUCTION_DIM_PER_WARP]`，其中 `REDUCTION_DIM_PER_WARP = hidden_dim / NUM_CONSUMER_WARPS`（见 [matvec_pipeline.cuh:14-15](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L14-L15)）。这就是 split-K 的「每个 warp 一段归约维度」。

**(e) `storer`：一次性投票**（[matvec_adds.cu:159-178](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L159-L178)）。先 `tma::store_async_wait()`（注意是**完整 wait**，确保加法存储已对全局可见，见 [L169-L170](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L169-L170) 的注释），再：

```cpp
atomicAdd(&g.Bar[{inst.layer, opcode - 1, 0}], inst.iters);   // 一次性 += iters
```

这里和 `rms_qkv_rope_append` 的「逐块 `+= 1`」形成对照（详见 u8-l4）：因为本 op 对**同一个 `block_idx` 的所有 iters 都已写完**才通知下游，且下游关心的是「这条输出流整体就绪」，所以直接把 `iters` 一次性加进去，省下 `iters` 次 `atomicAdd`。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`o_proj` 与 `down_proj` 共用 `MatVecAddOp`，差异只在模板参数」，并理解每个参数的语义。

**操作步骤（源码阅读型）**：

1. 打开 [matvec_adds.cu:181-192](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L181-L192)，把 `downproj` 与 `o_proj` 两行特化并排对照，逐个模板参数填出 4.1.1 那张对照表。
2. 追「成员指针」：在 [llama.cuh:113-137](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L113-L137) 找到 `o_weights`（类型 `weights_t`，归约维 = `hidden_dim`=2048）与 `down_weights`（类型 `weights_big_indim_t`，归约维 = `intermediate_dim`=8192），确认两者**输入维度不同**、但**输出维度都是 `hidden_dim`**，所以都能写进 `hidden_states`。
3. 追「等待阈值」：
   - `o_proj` 等 `attention_reduction`：看 [attention_reduction.cu:334-335](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L334-L335) 的 `atomicAdd(..., Q_HEADS_PER_INSTRUCTION)`（=4），共 `num_attention_heads/4 = 8` 条 reduction 指令 × 4 = 32，与 `EXPECTED_ARRIVAL_COUNT=32` 对上。
   - `down_proj` 等 `rms_upgate_silu`：看 [upgate.cu:117-120](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L117-L120) 的 `atomicAdd(..., 1)`，每个 reduction block（2048/16=128 块）累加到 128，与 `EXPECTED_ARRIVAL_COUNT=128` 对上。

**需要观察的现象**：你会看到两个 op 的**执行代码完全相同**（都是同一个 `MatVecAddOp<...>::pipeline::*_loop`），所有差异都被编译期模板参数吸收。

**预期结果**：能在不打开 `matvec_adds.cu` 主体的前提下，仅凭模板参数列表预测「这个 op 等谁、读什么、写到哪、投哪个槽」。

> 本地是否运行：无需运行，纯源码阅读即可完成（无 GPU 也能做）。

#### 4.1.5 小练习与答案

**Q1**：如果把 `o_proj` 的 `InputActivationsPtr` 错填成 `&Globals::hidden_states`（而不是 `attn_out`），运行期会发生什么类型错误？为什么模板**编译期**发现不了这种「填错成员」？

**答**：编译期不会报错——`hidden_states` 与 `attn_out` 都是 `activations_t` 类型（见 [llama.cuh:131-133](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L131-L133)），成员指针类型完全兼容。后果是 o_proj 会拿残差流自己当输入去投影，结果在数值上完全错误但能「正常」跑完。这是「成员指针 + 模板」的代价：类型系统只保证「这是个合法成员」，不保证「填的是语义上正确的成员」。

**Q2**：为什么 `store_add_async` 配 `EVICT_LAST`，而 `load_iter` 里的权重加载配 `EVICT_FIRST`？

**答**：`hidden_states` 是残差流，刚写完马上会被下游 op（下一层的 RMS_QKV，或 lm_head）读，所以希望它留在 L2 → `EVICT_LAST`。权重块只用一次、用完即可丢弃，避免污染 L2 → `EVICT_FIRST`。

**Q3**：`down_proj` 的输入是 8192 维，但 `REDUCTION_DIM_PER_WARP = hidden_dim/NUM_CONSUMER_WARPS` 用的是 `hidden_dim=2048`，这矛盾吗？

**答**：不矛盾。8192 维被 `reduction_block_idx` 切成 4 段每段 2048，**每条指令只处理一段**；段内的 2048 列再由 `NUM_CONSUMER_WARPS` 个 warp 各分 `REDUCTION_DIM_PER_WARP` 列。跨段的累加交给 `store_add_async`（见 4.1.2）。

---

### 4.2 attention_reduction：用 log-sum-exp 合并多个 partial

#### 4.2.1 概念说明

`attention_partial`（u9-l1）只算了**一段 KV** 上的注意力：它把序列切成若干 partial，每个 partial 独立产出

- \(O_{\text{partial}}\)：这段 KV 上 softmax 归一化后的输出向量（`head_dim` 维）；
- \(l_{\text{partial}}\)：这段 KV 上未归一化注意力分数的 **LSE**（标量）。

这两者被分别存进全局张量 `attn_out_intermediates` 与 `attn_lse_intermediates`（见 [attention_partial.cu:583-639](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L583-L639)）。

`attention_reduction` 的工作是：把属于同一个 query head 的所有 partial 的 \((O_{\text{partial}}, l_{\text{partial}})\) **合并成最终的全序列注意力输出**。难点在于——每个 partial 的 \(O\) 已经被自己的局部分母归一化了，不能直接相加；必须用 LSE 把它们的分母「对齐」后再加权合并。

#### 4.2.2 核心流程：LSE 合并的数学

设两个 partial 分别是 \((O_a, l_a)\) 与 \((O_c, l_c)\)，其中（以下指数均以 2 为底，因为整个 attention 流水线用 `exp2/log2`）

\[
l = \log_2 Z, \qquad Z = \sum_i 2^{s_i}, \qquad O = \frac{\sum_i 2^{s_i} V_i}{Z}.
\]

于是 \(\sum_i 2^{s_i} V_i = Z \cdot O = 2^{l} \cdot O\)。合并两段：

\[
Z_{\text{tot}} = Z_a + Z_c = 2^{l_a} + 2^{l_c},
\]

\[
O_{\text{tot}} = \frac{2^{l_a} O_a + 2^{l_c} O_c}{2^{l_a} + 2^{l_c}},
\qquad
l_{\text{tot}} = \log_2(2^{l_a} + 2^{l_c}).
\]

直接算 \(2^{l_a}+2^{l_c}\) 会溢出。提取 \(m=\max(l_a,l_c)\)：

\[
2^{l_a} + 2^{l_c} = 2^{m}\bigl(2^{l_a-m} + 2^{l_c-m}\bigr).
\]

记 \(d_a = 2^{l_a-m},\; d_c = 2^{l_c-m},\; D = d_a + d_c\)，则

\[
\boxed{\;O_{\text{tot}} = \frac{d_a}{D} O_a + \frac{d_c}{D} O_c,\qquad l_{\text{tot}} = m + \log_2 D.\;}
\]

这正是代码里那段看似绕的浮点运算（[attention_reduction.cu:270-286](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L270-L286)）：

```cpp
float max_lse = max(accumulated_lse, current_lse);
float accumulated_exp = exp2f(accumulated_lse - max_lse);   // d_a
float current_exp     = exp2f(current_lse     - max_lse);   // d_c
float new_denom       = accumulated_exp + current_exp;      // D
float accumulated_scale = accumulated_exp / new_denom;      // d_a/D
float current_scale     = current_exp     / new_denom;      // d_c/D
kittens::warp::mul(accumulated_out, accumulated_out, accumulated_scale);
kittens::warp::mul(current_out,     current_out,     current_scale);
kittens::warp::add(accumulated_out, accumulated_out, current_out);
accumulated_lse = max_lse + log2f(new_denom);               // l_tot
```

**初始化**：`accumulated_lse = -INFINITY`、`accumulated_out = 0`（[L240, L245](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L245)）。代入第一个 partial（\(l_a=-\infty\)）：\(d_a=2^{-\infty}=0\)、\(d_c=1\)、\(D=1\)，于是 \(O_{\text{tot}}=O_c\)、\(l_{\text{tot}}=l_c\)——即「合并起点 = 第一个 partial」，符合直觉。

> **为什么全程 base-2？** 因为上游 `attention_partial` 的 LSE 就是用 `log2` 算的（[attention_partial.cu:528-531](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L528-L531)：`L_reg = log2(norm) + scaled_max`）。reduction 必须用同一套底（`exp2f/log2f`）去还原 \(Z=2^l\)，否则合并结果错。整套 attention 之所以选 base-2，是因为分数预先乘了 `1/ln(2)`（见 [attention_partial.cu:421-422](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L421-L422)），让 `exp2` 直接充当 softmax 的指数。

#### 4.2.3 源码精读

**指令解析**（[attention_reduction.cu:25-47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L25-L47)）：一条 reduction 指令负责 `Q_HEADS_PER_INSTRUCTION = 4` 个 query head（见 [attention_reduction.cu:9](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L9)）。字段含 `layer_idx`、`q_head_start_idx`、`num_partials`（要合并几个 partial）、`reduction_list[]`（这 `num_partials` 个 partial 各自的编号）。`reduction_list` 之所以是显式列表而非 `0..num_partials-1`，是为了支持「跳过空 partial」等灵活调度。

**launcher：TMA 预取所有 partial**（[attention_reduction.cu:173-228](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L173-L228)）：
- [L184-L197](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L184-L197)：先自旋等 4 个 head 的 partial 都到齐（`Bar[layer, PartialAttention-1, head] >= num_partials`）。
- [L200-L207](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L200-L207)：TMA 一次性把 4 个 head 的 **LSE 向量**（`attn_lse_intermediates`）搬进共享内存。注意 LSE 张量类型是 `sv_fl<((sm_count+15)/16)*16>`（见 [llama.cuh:100-101](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/llama.cuh#L100-L101)）——长度被向上取整到 16 的倍数（`ROUNDED_MAX_ATTN_PARTIALS`，[attention_reduction.cu:11](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L11)），因为最大 partial 数 = SM 数，需对齐到 TMA 块大小。
- [L209-L226](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L209-L226)：对每个 partial，用 `NUM_STAGES=2` 级流水把 **O partial 向量**（`attn_out_intermediates`）逐个搬进来，靠 `O_partial_arrived/finished` 信号量与 consumer 握手（double-buffer）。

**consumer：每个 warp 合并一个 head**（[attention_reduction.cu:231-301](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L231-L301)）。`warpid() < 4` 的 4 个 warp 各领一个 head，独立跑上面的 LSE 合并循环：
- [L262-L266](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L262-L266)：从共享内存的 LSE 向量里用 `lds`（单 float）取出本 partial 的 `current_lse`；`current_out` 是 `head_dim` 维的寄存器向量。
- [L253-L290](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L253-L290)：核心合并循环，数学已在 4.2.2 推导。每个 partial 处理完 `arrive(O_partial_finished)`，让 launcher 释放该 double-buffer 槽。
- [L293-L295](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L293-L295)：把合并好的 `accumulated_out`（float）转 bf16 存进 `O_final_smem`。

**storer：写出最终 attn_out**（[attention_reduction.cu:305-339](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L305-L339)）：用**普通** `tma::store_async`（不是 add！）把最终结果写进 `g.attn_out`（[L318-L321](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L318-L321)），然后 `atomicAdd(&g.Bar[layer, AttentionReduction-1, 0], 4)`（[L334-L335](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L334-L335)）通知下游 `o_proj`。

#### 4.2.4 代码实践

**实践目标**：对比 `attention_reduction` 与 `attention_partial` 对 LSE 的不同处理。

**操作步骤（源码阅读 + 推导型）**：

1. 在 [attention_partial.cu:463-531](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L463-L531) 找到 partial 内部的**在线 softmax**：每来一个 KV block，它用 `diff_scaled_max_vec = exp2(old_max - new_max)`（[L497-L500](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L497-L500)）把已累积的 \(O\) 和分母 rescale，再累加新块。这本质上是「把 4.2.2 的两路合并公式，退化成『已有累积 vs. 单个新 block』的特例」。
2. 对照 [attention_reduction.cu:270-286](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L270-L286)：注意 partial 维护的是「\(O\) 尚未除以分母的**非归一化**累加 + 一个单独的分母 norm」，最后才 `div_row(O, O, norm)`（[attention_partial.cu:527](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L527)）；而 reduction 维护的是「**已归一化**的 \(O_{\text{tot}}\) + 标量 LSE」，每步都保持归一化。
3. 自己手算一遍：两个 partial，\(l_a = \log_2 4 = 2\)、\(O_a = (1,0,\dots)\)；\(l_c = \log_2 2 = 1\)、\(O_c = (0,1,\dots)\)。用 4.2.2 公式验证 \(O_{\text{tot}} \approx (0.8, 0.2,\dots)\)、\(l_{\text{tot}} = \log_2 6\)。

**需要观察的现象**：两个 op 用的是**同一套** LSE 合并思想（提取 max、用差值的 exp 做权重、更新 LSE = max + log(denom)），但 partial 是「逐 KV block、跨多步、保留未归一化 O」，reduction 是「逐 partial、少步骤、O 始终归一化」。

**预期结果**：能口头复述「为什么 reduction 必须读 partial 的 LSE，而不能直接把两个 partial 的 \(O\) 相加」——因为两者分母 \(Z\) 不同，相加会偏袒分母大的那段。

> 本地是否运行：手算可完成；若想跑代码，需要 Hopper/Blackwell 环境与完整 demo（见仓库 README）。

#### 4.2.5 小练习与答案

**Q1**：合并循环里 `accumulated_lse` 初始化成 `-INFINITY` 而不是 `0`。如果误初始化成 `0`，第一个 partial 合并后结果会怎样？

**答**：若初值 \(l_a=0\)，则第一个 partial 合并时 \(d_a=2^{0-m}=2^{-l_c}\neq 0\)，相当于凭空多了一个「权重为 \(2^{0}\)、输出为 0 的空 partial」，把 \(O_{\text{tot}}\) 往 0 方向拉偏、把 \(l_{\text{tot}}\) 抬高。`-INFINITY` 让 \(d_a=0\)，空起点不参与加权，才正确。

**Q2**：`attention_reduction` 的 storer 用 `store_async`，而 `MatVecAddOp` 用 `store_add_async`。为什么 reduction 不需要原子加？

**答**：reduction 写的是 `attn_out`，每个 head 的最终输出由**唯一一条** reduction 指令、**唯一一个 warp** 算出并写一次，没有并发写同一地址，覆盖写即可。`hidden_states` 则会被多条 down_proj 指令（不同 reduction block）并发累加，必须原子加。

**Q3**：为什么 LSE 张量 `attn_lse_intermediates` 的最后一维要 `((sm_count+15)/16)*16` 向上取整，而 `attn_out_intermediates` 不需要？

**答**：LSE 是用 TMA 整块搬运的 `sv_fl<N>` 向量，TMA 要求块大小对齐（这里对齐到 16 个 float）；实际 partial 数 ≤ sm_count，但取整到 16 的倍数才能用固定大小的 TMA descriptor。O partial 是按 `[head, partial, head_dim]` 多维取的，`head_dim=64` 已是对齐块，不需额外取整。

---

### 4.3 upgate / rms_lm_head：同一套 RMSNorm 流水线的两种填法

#### 4.3.1 概念说明

`rms_upgate_silu`（文件里叫 `upgate.cu`）和 `rms_lm_head` 都继承自 `rms_matvec_pipeline`——也就是「先 RMSNorm，再 split-K matvec」。它们和 u8-l4 讲过的 `rms_qkv_rope_append` 是**同一个模子**，区别只在 `pipeline_specifics` 里填的 `load_iter` / `store` / `gmem_wait` 三个回调。本节只点出它们「填了什么」。

#### 4.3.2 核心流程

| op | 流水线基类 | `load_iter` 干什么 | `store` 干什么 | 写到哪 |
| --- | --- | --- | --- | --- |
| `rms_upgate_silu` | `rms_matvec_pipeline` | 偶数 iter 拉 `up_weights`，奇数 iter 拉 `gate_weights`（[upgate.cu:46-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L46-L55)） | 偶数 output_idx 跳过；奇数时把上一阶段的 up 与本阶段的 gate 归约出来，算 \(\text{SiLU}(x)=x/(1+e^{-x})\)，再 `up * SiLU(gate)`（[upgate.cu:58-126](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L58-L126)） | `silu_out`（普通 `store_async`） |
| `rms_lm_head` | `rms_matvec_pipeline` | 拉 `lm_head_weights`（[rms_lm_head.cu:43-45](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L43-L45)） | `matvec_reduce` → logits（[rms_lm_head.cu:48-77](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L48-L77)） | `logits`（普通 `store_async`） |

`rms_upgate_silu` 有两个值得注意的小机关：

1. **双投影交替加载**：`iters = 2 * instruction[2]`（[upgate.cu:24](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L24)），up 与 gate 共享同一条 matvec 流水线、交替占用每个 iter；store 端用 `storer_loop<2>`（`iter_scale=2`，[upgate.cu:169](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L169)）每两个 iter 才回调一次 store——正好攒齐一对 (up, gate)。
2. **SiLU 门控在 store 回调里做**：[upgate.cu:91-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L91-L98) 里用 `mul(-1)` → `exp` → `add 1` → `div` 实现 \( \text{SiLU}(x) = x / (1 + e^{-x}) \)，再乘 up。

`rms_lm_head` 则是「最朴素的填法」：等最后一层 down_proj 全部就绪（`EXPECTED_ARRIVAL_COUNT = 512`，见 [rms_lm_head.cu:14, 32-34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L32-L34)），做 RMSNorm + lm_head 投影，写出 logits。

#### 4.3.3 源码精读

- up/gate 交替：[upgate.cu:46-55](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L46-L55)（`iter % 2 == 0` 选 `up_weights`，否则 `gate_weights`）。
- SiLU 门控：[upgate.cu:91-98](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L91-L98)（`mul(-1)` → `exp` → `add 1` → `div`，等价于 \(x/(1+e^{-x})\)）。
- 每 2 次一存：[upgate.cu:169](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L169)（`storer_loop<2>`）。
- lm_head 等待：[rms_lm_head.cu:32-34](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L32-L34)（等 `Bar[num_layers-1, DownProj-1, 0] >= 512`）。
- lm_head 写 logits：[rms_lm_head.cu:71-73](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L71-L73)（普通 `store_async`）。

#### 4.3.4 代码实践

**实践目标**：确认 `upgate` 与 `rms_lm_head` 只是在 `rms_matvec_pipeline` 上填回调，结构无新东西。

**操作步骤（源码阅读型）**：

1. 打开 [matvec_pipeline.cuh:246-348](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_pipeline.cuh#L246-L348)，看 `rms_matvec_pipeline::consumer_loop` 如何先 `rms_norm`、再调基类 `consumer_loop`。
2. 对照 [upgate.cu:129-171](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L129-L171) 与 [rms_lm_head.cu:80-116](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L80-L116)：两者的 controller/loader/launcher/consumer/storer 几乎都是一行转发 `pipeline::*`，差异全在 `pipeline_specifics`。
3. 把 `rms_qkv_rope_append`（u8-l4）的回调签名和这两个 op 并排，体会「填空题」的一致性。

**需要观察的现象**：三个 rms 系 op 的「骨架代码」逐行相同，只有 `load_iter`/`store`/`gmem_wait` 三处不同。

**预期结果**：能说出「给 `rms_matvec_pipeline` 写一个新 op，最少只需实现这三个回调」。

> 本地是否运行：纯阅读，无需运行。

#### 4.3.5 小练习与答案

**Q1**：`rms_upgate_silu` 为什么用 `storer_loop<2>`（`iter_scale=2`）？

**答**：因为 up 和 gate 交替占用相邻两个 iter，必须攒齐一对 (up 块, gate 块) 才能算 `up * SiLU(gate)`。`iter_scale=2` 让 storer 每两个 iter 才回调一次 `store`，此时上一阶段（up）与本阶段（gate）的部分和都在 scratch 里，可同时取出归约（见 [upgate.cu:84-89](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/upgate.cu#L84-L89)）。

**Q2**：`rms_lm_head` 的 `gmem_wait` 等 `Bar[num_layers-1, OPCODE_DownProjResidual-1, 0] >= 512`。这个 512 是怎么来的？

**答**：`EXPECTED_ARRIVAL_COUNT = 512`（[rms_lm_head.cu:14](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/rms_lm_head.cu#L14)）。它等的是「最后一层 DownProj 全部完成」这一全局事件——512 对应 lm_head 输出（logits）方向上 16 元素块的总数，down_proj 的 storer 把残差流按块逐个投票，攒满 512 即表示最后一层残差流就绪，可以开始算 logits。

---

## 5. 综合实践：沿数据流追一条残差的「加法」与一次注意力的「合并」

把本讲三个模块串起来，做一次端到端的「读 + 推」。每问都给出**文件:行号**证据。

1. **残差加法点有几处？分别用哪个原语？**
   - 提示：`o_proj` 与 `down_proj` 都把结果加进 `hidden_states`。两处 `store_add_async` 共用同一份模板代码（[matvec_adds.cu:78-80](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/matvec_adds.cu#L78-L80)），只是成员指针不同；说明它们为何必须用「加」而非「覆盖」。
2. **一次 attention 的 LSE 经历了几次「合并」？分别在哪个 op？**
   - 提示：第一次在 `attention_partial` 内部逐 KV block 合并（在线 softmax，用 `exp2(old_max-new_max)` rescale，[attention_partial.cu:497-503](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_partial.cu#L497-L503)）；第二次在 `attention_reduction` 逐 partial 合并（4.2.2 公式，[attention_reduction.cu:270-286](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/demos/low-latency-llama/attention_reduction.cu#L270-L286)）。两次都用 base-2，写出各自的「max → exp 差 → 加权 → 更新 LSE」四步。
3. **同一份 `hidden_states`，谁是生产者、谁是消费者？**
   - 画一条链：`o_proj`/`down_proj` 用 `store_add_async` 写 `hidden_states`（生产者）；下一层的 `rms_qkv_rope_append` / `rms_upgate_silu` / 末层的 `rms_lm_head` 通过 `gmem_wait` 读它（消费者）。指出每个消费者等的 `Bar` 槽与阈值（注意 `rms_lm_head` 的 `EXPECTED_ARRIVAL_COUNT = 512`）。

**验收**：如果你能不看答案把上面三问的文件行号和数值（32、128、512）填对，说明你已经把「残差累加 + LSE 合并 + 跨 op 屏障」这三件事在源码层面打通了。

> 本地是否运行：纯阅读与推导；若要数值验证 LSE 合并，可把 4.2.4 第 3 步的手算例子写成一个 5 行的 Python/numpy 脚本独立验证（不依赖 GPU）。

---

## 6. 本讲小结

- `MatVecAddOp` 是一个**模板**，`o_proj` 与 `down_proj` 各用一行特化复用它；两者差异仅在 `_EXPECTED_ARRIVAL_COUNT`（32 vs 128）、三个成员指针（权重/输入/输出）与两个 opcode。
- 残差累加由 `tma::store_add_async`（TMA 原子加法存储）实现，语义是 `dst += src`；它同时承担了**跨指令 split-K 归约**（down_proj 的 4 个 reduction block 并发写同一 `hidden_states`）。
- `attention_reduction` 用 **log-sum-exp** 合并多个 partial：\(O_{\text{tot}} = (d_a O_a + d_c O_c)/D\)、\(l_{\text{tot}} = m + \log_2 D\)，其中 \(d=2^{l-m}\)、\(D=d_a+d_c\)、\(m=\max(l_a,l_c)\)。
- 全套 attention 的 LSE **统一用 base-2**（`exp2f/log2f`），因为分数预乘了 `1/ln(2)`；reduction 与 partial 必须用同一套底。
- `attention_partial` 是「逐 KV block、保留未归一化 O」的细粒度在线 softmax；`attention_reduction` 是「逐 partial、O 始终归一化」的粗粒度合并——同一思想、两种粒度。
- `upgate` 与 `rms_lm_head` 只是 `rms_matvec_pipeline` 的「填空题」，骨架与 `rms_qkv_rope_append` 完全一致，差异只在 `load_iter`/`store`/`gmem_wait` 三个回调。

---

## 7. 下一步学习建议

到这里，低延迟 Llama demo 的 **7 个 op 全部讲完**，整条 `RMS_QKV → PartialAttention → AttentionReduction → O_Proj → UpGate → DownProj → LM_Head` 流水线在源码层面已经闭合。建议接下来：

1. **读宿主侧调度**：`megakernels/generators.py` 与 `megakernels/demos/latency/python_vm.py`——看每个 token 的指令是如何生成、`Bar` 如何清零、`reduction_list` 如何编排，把「指令发射」和本讲看到的「指令执行」对上。
2. **读框架核心**：`include/util.cuh` 的 `state`、`dispatch_op`、page/scratch 管理，理解 controller/loader/launcher/consumer/storer 这 5 类 warp 是如何被框架调度到一条指令上的（这是所有 op 共用的「虚拟机」）。
3. **做一次端到端的时间线分析**：用 demo 自带的 timing 机制（代码里大量的 `s.record(TEVENT_...)`）画出单层的 megakernel 时间线，观察 `store_add_async` 的原子加与 LSE 合并分别落在哪段，验证它们是不是预期的延迟热点。
