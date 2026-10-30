# 线性注意力层：Gated DeltaNet 的状态与更新

## 1. 本讲目标

学完本讲，你应该能够：

1. 写出 `per_layer_dims` 的两条元素数公式，并对任一配置手算一层线性注意力的 state 与 conv_state 元素数、字节数。
2. 解释 `RecurrentState` / `LayerRecurrentState` 的 `[H,K,V]` f32 状态与 bf16 conv_state 布局，以及两种精度各自的选择理由。
3. 说明 causal conv1d 在 prefill 与 decode 两条路径上如何读写状态。
4. 解释 `LinearStatePointerTables` 为什么能让「批量内核 + CUDA Graph」同时成立。
5. 推导 TP 下 recurrent/conv 状态按 value head 分片、从不 all-reduce 的不变量，并证明分片后各 rank 状态之和恰好等于 TP1 总量。

## 2. 前置知识

**线性注意力与全注意力的记忆方式对比。** 全注意力层把历史上每个 token 的 K/V 都存进分页 KV 池，注意力计算时 \(Q K^\top V\) 要回看全部历史，显存随上下文长度线性增长。Gated DeltaNet（GDN，Qwen3.5 线性注意力层的底层模型）换了一种「记忆」：每个头维护一个**固定大小**的递推状态矩阵 \(S \in \mathbb{R}^{d_k \times d_v}\)，每来一个 token 就对状态做一次**门控衰减 + delta（误差校正）写入**，输出由查询直接读状态得到。历史信息被「压缩」进这个矩阵里，因此显存不随上下文增长——这正是 u1-l1 说的「4B 为 8 full + 24 linear」中 24 个 linear 层的记忆载体。

**两个短术语：**

- **FLA `[H,K,V]` 约定**：状态张量按「头 → key 维 → value 维」嵌套存放，value 维连续。本 crate 的状态是「每头一个 \([d_k, d_v]\) 矩阵，头之间依次拼接」的一维 f32 数组。
- **depthwise conv1d（因果卷积）**：每个通道独立做一维卷积，只看当前和过去 \(k_{conv}-1\) 个输入。GDN 用它给 qkv 做一层短程平滑，需要保存最近的卷积抽头（taps）。

**承接 u2-l4 的关键结论**：层结构体（`LinearAttentionLayer`）只装**不可变权重**；递推状态**外置**在调度器侧的 `RecurrentState` 里。本讲就放大这个「外置的状态」：它的布局公式、它如何被内核原地更新、以及批量执行时如何被指针表索引。权重侧的装载期融合（`in_proj_qkvz`、`in_proj_ba`）与 z 门控 RMSNorm 已在 u2-l4 讲过，本讲只在使用点引用。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|------|------|------|
| `src/recurrent_state.rs` | 252 | 本讲主角：状态布局公式、`RecurrentState` 分配/拷贝、`LinearStatePointerTables` 指针表、每请求字节数 |
| `src/config/model.rs` | 433 | `Config35` 中线性注意力几何字段与内核维度门（GDN 128/128、卷积宽 1..=4） |
| `src/config/tp.rs` | 360 | `LocalGeometry` 的 `local_linear_*` 派生与 TP 整除门 |
| `src/recurrent.rs` | 1360 | 内核封装：conv1d prefill、GDR 单槽 decode（测试用）、GDR chunkwise prefill 七段流水线 |
| `src/batch_decode.rs` | ~950 | 批解码主循环：`batch_decode_linear_attention_slots` 一次线性层解码的完整算子序列 |
| `src/batch_decode_graph.rs` | 169 | `slot_states` 槽位与指针表的建表、`copy_state_to_slot` / `move_slot_within` |
| `src/prefill.rs` | ~560 | prefill 侧线性层前向：状态在 chunk 间的推进 |
| `src/weights.rs` | ~700 | `STATES_PER_DECODE_SLOT=2` 预留、显存预算顺序、`all_reduce_hidden` |
| `src/weights/layers.rs` | 300+ | `LinearAttentionLayer` 权重定义（本讲只引用维度注释） |
| `../pegainfer-kernels/src/ops/qwen35.rs` | 650+ | 批量 decode 内核的 Rust 封装与文档契约（`state_ptrs: &CudaSlice<u64>`） |

## 4. 核心概念与源码讲解

### 4.1 状态布局：per_layer_dims 与 [H,K,V] f32 状态 + bf16 conv_state

#### 4.1.1 概念说明

`RecurrentState` 是**一个请求**在某一时刻的全部线性注意力状态：24 个线性层（以 4B 为例）各持一份 `LayerRecurrentState`，外加一个 host 侧的 `seq_len` 计数器。每个 `LayerRecurrentState` 有两块设备内存：

- **state**：GDN 递推状态，\([H_v^{local} \times d_k \times d_v]\) 个 **f32**。这是「被压缩的历史」，随每个 token 做门控衰减与 delta 写入，是长期累积量。
- **conv_state**：因果卷积抽头，\([d_{qkv}^{local} \times (k_{conv}-1)]\) 个 **bf16**。它只是「最近 \(k_{conv}-1\) 个输入的原样字节」，不参与任何算术累积。

「状态外置于层结构体」是三个后续机制的前提：CUDA Graph 指针稳定（u4-l3）、前缀缓存快照（u6）、prefill/decode 状态交接（u4-l1）。本讲先把布局本身讲透。

#### 4.1.2 核心流程

一层线性注意力的两个状态尺寸由同一个小函数决定：

\[
S_{state} = H_v^{local} \times d_k \times d_v \quad (\text{f32 元素})
\]

\[
S_{conv} = d_{qkv}^{local} \times (k_{conv} - 1) \quad (\text{bf16 元素})
\]

其中融合 qkv 投影的本地宽度（q 段与 k 段等宽，v 段按 value head 计）：

\[
d_{qkv}^{local} = 2 \times (H_k^{local} \times d_k) + H_v^{local} \times d_v
\]

一个请求的总状态字节：

\[
B_{req} = L_{linear} \times (S_{state} \times 4 + S_{conv} \times 2)
\]

伪代码（分配路径）：

```text
RecurrentState::new(ctx, config, geometry):
    (L, S_state, S_conv) = per_layer_dims(config, geometry)
    for _ in 0..L:                      # 每个线性层一份
        state      = alloc_zeros::<f32>(S_state)     # 零初始化：空历史
        conv_state = DeviceVec::zeros(S_conv)        # 零抽头
    seq_len = 0
```

#### 4.1.3 源码精读

模块文档一句话给出全部布局与 TP 结论，值得先读三遍：

- [src/recurrent_state.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L1-L9) — 声明每层状态为 `[local_value_heads, key_head_dim, value_head_dim]` f32、conv 状态为 `local_qkv_dim × (conv_kernel_dim - 1)` bf16，并预告 TP 下「每个 rank 拥有自己的状态、从不 all-reduce」。

两个结构体：

- [src/recurrent_state.rs:L20-L28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L20-L28) — `LayerRecurrentState`：`state: CudaSlice<f32>`（类型化 f32 切片）与 `conv_state: DeviceVec`（bf16 元素向量）。注释 `Stored as f32 per mamba_ssm_dtype="float32" in config` 引用的是上游 HF 配置的键名约定；注意本 crate 的 `RawConfig` 并**不**解析这个键（`TextConfig` 里没有它），f32 是无条件的选择，引用的是上游语义。
- [src/recurrent_state.rs:L30-L35](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L30-L35) — `RecurrentState`：`layers: Vec<LayerRecurrentState>` 按**模型层序**存所有线性层（第 ℓ 个线性层对应 `layers[ℓ]`，与 `linear_idx` 计数器配套，见 4.3.3），`seq_len` 是 host 侧 token 计数。

布局公式的唯一权威来源：

- [src/recurrent_state.rs:L53-L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L53-L62) — `per_layer_dims` 返回 `(线性层数, f32 state 元素数, bf16 conv 元素数)`。线性层数 = `num_hidden_layers - num_full_attention_layers()`；state 尺寸取 `geometry.local_linear_num_value_heads() * linear_key_head_dim * linear_value_head_dim`；conv 尺寸取 `geometry.local_linear_qkv_dim() * (linear_conv_kernel_dim - 1)`。分配与预留（4.1.4 的实践、weights.rs 的显存预算、prefix_cache 的槽尺寸）共享这一个函数，杜绝「两处公式漂移」。

分配与拷贝：

- [src/recurrent_state.rs:L64-L86](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L64-L86) — `RecurrentState::new` 对每层零分配两块缓冲。零初始化即「空历史」：GDN 的初始状态矩阵与卷积抽头都从 0 出发。
- [src/recurrent_state.rs:L88-L106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L88-L106) — `copy_from` 逐层 D2D 拷贝 state 与 conv_state，并同步 `seq_len`。关键细节：它是**拷入目标已有的分配**，不换缓冲——目标槽的设备地址不变，CUDA Graph 不会失效（这是 prefill→decode 交接 `copy_state_to_slot` 的实现基础，见 4.3.3）。
- [src/recurrent_state.rs:L229-L235](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L229-L235) — `bytes_per_request`：\(B_{req} = L \times (S_{state}\times 4 + S_{conv}\times 2)\)。它被三处消费：weights.rs 的 recurrent 预留、prefix snapshot 槽数换算、`allocation_bytes`。

几何字段来自 config（TP1 时 local 即 global）：

- [src/config/model.rs:L92-L97](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L92-L97) — `Config35` 的五个线性注意力字段：`linear_num_key_heads`、`linear_key_head_dim`、`linear_num_value_heads`、`linear_value_head_dim`、`linear_conv_kernel_dim`。
- [src/config/model.rs:L232-L253](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L232-L253) — 内核维度门：GDN key/value head dim 必须等于 AOT 烘焙值（128/128，重导出自 kernels），卷积核宽必须在 `1..=LINEAR_CONV_MAX_KERNEL_DIM`（=4）。这两条在加载期对拍，config 与内核不符根本走不到分配。
- [src/config/tp.rs:L220-L228](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L220-L228) — `local_linear_qkv_dim()` 与 `local_linear_z_dim()`（z 段宽 = 本地 v 宽）。

容量预留侧的乘数：

- [src/weights.rs:L96-L97](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L96-L97) — `STATES_PER_DECODE_SLOT: usize = 2`：每个 decode 槽配「1 份图槽状态 + 1 份在途 prefill 瞬态」。
- [src/weights.rs:L287-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L287-L299) — `recurrent_reserve = 2 × max_batch × bytes_per_request`，随后前缀快照预算按同一 `bytes_per_request` 换算槽数。

#### 4.1.4 代码实践

**实践目标**：用 `per_layer_dims` 公式手算 Qwen3.5-4B（TP1）一层线性注意力的 state/conv 元素数与字节数，再核算 64 个 decode 槽（含 prefill 瞬态，×2）的总显存预留，并与源码内嵌单测对拍。

**操作步骤**（纸面计算，4B 配置：`num_hidden_layers=32`、8 full + 24 linear，`linear_num_key_heads=16`、`linear_num_value_heads=32`、`linear_key_head_dim=linear_value_head_dim=128`、`linear_conv_kernel_dim=4`，TP1 故 local 即 global）：

1. state 元素数：\(32 \times 128 \times 128 = 524{,}288\) 个 f32 → 每层 2 MiB。
2. \(d_{qkv} = 2\times(16\times128) + 32\times128 = 8192\)；conv 元素数：\(8192 \times (4-1) = 24{,}576\) 个 bf16 → 每层 48 KiB。
3. 每层字节 \(= 524288\times4 + 24576\times2 = 2{,}146{,}304\)；每请求 \( \times 24\) 层 \(= 51{,}511{,}296\) B \(= 49.125\) MiB（即 49 MiB + 128 KiB）。
4. 64 槽预留：\(2 \times 64 \times 49.125\text{ MiB} = 6288\text{ MiB} \approx 6.14\text{ GiB}\)。
5. 与 [src/recurrent_state.rs:L243-L252](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L243-L252) 的内嵌单测 `qwen35_4b_recurrent_allocation_is_49_125_mib` 对拍——它用同样的常数断言 `bytes == 49*1024*1024 + 128*1024`。这是纯 host 算术测试，不需要 GPU 设备，但编译需要 CUDA + Triton AOT 工具链（见 u1-l2）。

**需要观察的现象**：若本地有构建工具链，运行 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib recurrent` 应通过；启动真实服务时，[src/weights.rs:L322-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L322-L326) 的 info 日志会打出 `recurrent-state reserve: ... MB (2 x 64 slots)`，应与你手算的 6288 MiB 一致。

**预期结果**：每请求 49.125 MiB；64 槽 × 2 ≈ 6.14 GiB。若在无工具链环境，纸面结果即最终产出，运行验证标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：对 [src/config/model.rs:L303-L324](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L303-L324) 测试夹具 `VALID_RAW`（`linear_num_key_heads=16`、`linear_num_value_heads=48`、head dim 均 128、卷积宽 4、1 个线性层）计算 TP1 下的 state/conv 元素数。
**答案**：state \(= 48\times128\times128 = 786{,}432\) 个 f32（3 MiB）；\(d_{qkv} = 2\times(16\times128)+48\times128 = 10240\)，conv \(= 10240\times3 = 30{,}720\) 个 bf16（60 KiB）。

**练习 2**：为什么 state 用 f32 而 conv_state 用 bf16？
**答案**：state 是跨整个序列持续累积的递推量，每步都在旧值上做衰减与更新，量化误差会随序列长度复利放大，所以按上游 `mamba_ssm_dtype="float32"` 约定用 f32，且两个 GDN 内核封装的指针也确实按 `*mut f32` 传递。conv_state 只是把最近 \(k_{conv}-1\) 个 **bf16 输入原样**留存，不参与累积，用 bf16 存是无损的，还省一半显存。

**练习 3**：把 `linear_conv_kernel_dim` 改成 5 会发生什么？
**答案**：加载即失败，返回 `ConfigError::LinearConvKernelDim { max: 4, actual: 5 }`——因为 in-tree 的 `conv1d.cu` 只为 `kernel_size - 1 <= 3` 个抽头携带状态（`LINEAR_CONV_MAX_KERNEL_DIM`，见 [src/config/model.rs:L248-L253](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L248-L253)）。

### 4.2 causal conv1d：短记忆的入口与 prefill/decode 两条内核

#### 4.2.1 概念说明

在进入 GDN 递推之前，线性层先把融合投影出的 qkv 段过一遍**因果 depthwise conv1d**（再接 SiLU）。它是「短记忆」：只看最近 \(k_{conv}\) 个输入（当前 + 状态里的 \(k_{conv}-1\) 个抽头），输出写回给 GDN。卷积状态就是那 \(k_{conv}-1\) 个抽头，oldest-first 排列。

同一个语义有两套内核：

- **prefill**：一次处理整段（一个 chunk 的）token 序列，内核内部能直接看到历史 token，只需在结束时把**最后 \(k_{conv}-1\) 个输入**写进 conv_state，供下一段接续。
- **decode**：每个请求只有 1 个新 token，历史只能来自 conv_state；内核读抽头、算输出、**原地滚动**抽头，并与 SiLU 融合（输出是 `silu(bf16(conv))`，不是 `silu(sum)`， rounding 与参考实现逐位一致）。

#### 4.2.2 核心流程

decode 一步（对槽 \(i\)、通道 \(c\)，卷积宽 \(k\)，抽头宽 \(w = k-1\)）：

\[
out_c = \mathrm{silu}\big(\mathrm{bf16}(\textstyle\sum_{j=0}^{k-1} w_{c,j} \cdot tap_{c,j})\big), \quad tap = [\,\underbrace{state_c[0..w]}_{\text{旧抽头}},\, x_c\,]
\]

随后状态滚动（oldest-first 移位，新输入顶到末位）：

\[
state_c[j] \leftarrow state_c[j+1]\ (j<w-1), \qquad state_c[w-1] \leftarrow x_c
\]

这正是 [src/recurrent.rs:L616-L647](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L616-L647) 里参考实现 `reference_conv1d_decode_step` 逐行写成的内容——测试用它给 GPU 内核对拍。

#### 4.2.3 源码精读

- [src/recurrent.rs:L54-L87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L54-L87) — `conv1d_prefill_batch_into`：对整段 `x_seq` 做因果卷积并更新 `conv_state`。开头的四个断言就是状态契约：`conv_weight.len == num_channels * kernel_size`、`conv_state.len == num_channels * (kernel_size - 1)`，且输入输出同形。
- [../pegainfer-kernels/src/ops/qwen35.rs:L599-L650](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L599-L650) — `conv1d_decode_batch_into`：批量 decode 版。签名里没有「每个槽的状态」数组本体，只有 `conv_state_ptrs: &CudaSlice<u64>`——设备侧指针数组，第 \(i\) 行指向槽 \(i\) 的 conv_state，内核**原地更新**。文档还约定：输入 `x` 可以是融合 qkvz 投影的 qkv 波段（槽按张量自身行宽跨步），`kernel_size` 不得超过 4。
- [src/recurrent.rs:L479-L614](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L479-L614) — 测试 `conv1d_prefill_handoff_matches_single_prefill`：先整段跑一次，再「前缀 + 逐 token」分开跑，断言输出与最终 conv_state 双双一致（容差 0.02）。这是「chunk 间接力等于整段接力」的数值证据——prefill 分块（u4-l1）依赖这个性质。
- [src/prefill.rs:L504-L514](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L504-L514) — prefill 调用点：`prefill_linear_attention` 先用 `gemm_rows_into` 从融合权重里读出 qkv 段，再把它与 `layer_state.conv_state`（注意取的是 `recurrent.layers[linear_idx]`）一起交给 conv 内核；同一层随后把 `layer_state.state` 交给 GDR chunkwise（[src/prefill.rs:L517-L531](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L517-L531)）。**一个 chunk 内 conv 与 GDR 两块状态都被推进**，跨 chunk 由下一 chunk 接续。
- [src/recurrent.rs:L336-L456](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L336-L456) — `gated_delta_rule_prefill_chunkwise_into`：GDR 分块预填充的七段流水线（prepare → cumsum → A → solve → recompute → state → o），显式多内核 + 预分配 scratch，而不是一个黑盒内核。其中 state 段把 `state` 指针**同时**作为只读与可变参数传入（读入旧状态、写出新状态，原地推进，见 [src/recurrent.rs:L247-L290](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L247-L290) 中 `s_ptr` 的两次出现）；o 段的注意力缩放取 \(1/\sqrt{d_k} = 1/\sqrt{128}\)。逐段语义属于 u3-l3 的 FFI 主题，这里只需建立「chunk 会原地推进 state」的认知。

#### 4.2.4 代码实践

**实践目标**：读懂 conv 状态滚动语义，并能从测试反推内核行为。

**操作步骤**：

1. 精读 [src/recurrent.rs:L616-L647](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L616-L647) 的参考实现，自己用 3 个通道、`kernel_size=4`、手工造 2 个 token 的输入，在纸上执行两步，写出每步的输出与状态。
2. 再读 [src/recurrent.rs:L663-L700](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent.rs#L663-L700) 的批量 decode 测试如何刻意给 3 个槽**不同**的初始状态、相同的输入——注释点明动机：如果内核错误地共享或错位索引状态指针，槽 1、2 会偏离各自的参考轨迹。
3. 运行（需 GPU + 构建工具链）：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib conv1d`。

**需要观察的现象**：批量内核输出与逐槽参考逐步对拍通过；`bf16_ulp_distance` 辅助函数按 ULP 度量舍入差异。

**预期结果**：测试通过。无 GPU 环境下第 1、2 步的纸面推演即为产出，运行验证标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：若 `linear_conv_kernel_dim = 1`，conv_state 多大？conv 还剩什么作用？
**答案**：\(S_{conv} = d_{qkv} \times 0 = 0\)，没有抽头；卷积退化为逐通道缩放（权重只剩当前输入那一拍），内核仍可运行（宽度 1 在 `1..=4` 门内）。

**练习 2**：为什么 decode 内核要把 SiLU 融合进来，还特意说明输出是 `silu(bf16(conv))` 而非 `silu(sum)`？
**答案**：GDN 块的输入约定就是「conv 后接 SiLU」，融合省一次内核发射与一次中间缓冲往返；而显式注明 rounding 顺序是为了与参考实现（以及上游 FLA）**逐位**一致——精度门（u8-l1）对这种舍入差是敏感的。

**练习 3**：prefill 内核明明能看到整段输入，为什么还需要 conv_state？
**答案**：prefill 是分块执行的（u4-l1）：第 N 个 chunk 的因果卷积需要第 N-1 个 chunk **末尾**的 \(k_{conv}-1\) 个输入，这正是上一个 chunk 结束时写进 conv_state 的内容。`conv1d_prefill_handoff_matches_single_prefill` 验证了这种接力与整段执行等价。

### 4.3 LinearStatePointerTables：为批量内核准备的每槽指针表

#### 4.3.1 概念说明

批解码一步要同时更新 N 个请求 × 24 个线性层的状态。若每个请求单独持有 `RecurrentState`，内核无法用一次发射索引到它们——CUDA 内核需要**设备侧连续的指针数组**。`LinearStatePointerTables` 就是这张表：每个线性层 ℓ 配一对 `CudaSlice<u64>`（`state_ptrs[ℓ]`、`conv_state_ptrs[ℓ]`），第 \(i\) 行是槽 \(i\) 的该层状态设备地址。

它成立的前提是一个不变量：**槽 \(i\) 的状态分配在请求生命周期内地址永不改变**。于是表可以一次建好、每个 decode token 反复重放——这正是 CUDA Graph 捕获需要的指针稳定性（图里烤进了这些地址，重放时读写同一位置）。

#### 4.3.2 核心流程

一次批解码中线性层的执行序列（`padded_bs` 行，含 padding）：

```text
batch_decode_linear_attention_slots(attn, state_ptrs[ℓ], conv_state_ptrs[ℓ], padded_bs):
1. qkvz = GEMM(in_proj_qkvz, normed)          # [qkv+z, padded_bs]
2. ba   = GEMM(in_proj_ba,   normed)          # [2*Hv, padded_bs]
3. qkv_conv = conv1d_decode_batch(qkvz[:, :qkv_dim], conv_w, conv_state_ptrs[ℓ])
                                            # 内核按指针表原地滚动各槽 conv_state
4. o = gdr_decode_batch(qkv_conv, ba[:, :Hv], ba[:, Hv:], dt_bias, a_log, state_ptrs[ℓ])
                                            # 内核按指针表原地更新各槽 GDN state
5. g = rms_norm_gated(o, norm_w, z=qkvz[:, qkv_dim:])
6. attn_results = GEMM(out_proj, g); all_reduce_hidden(attn_results)   # TP 唯一通信点
```

表的两种构建方式：

- **固定行集**（单 GPU 图解码）：`from_recurrent_refs` 按精确 batch 建表填满，一次成型。
- **行集每步变化**（eager TP 解码）：`with_capacity` 按容量建一次空表，每步 `refill_from_recurrent_refs` 只把**活跃行** H2D 覆写进既有分配——零每步分配。

#### 4.3.3 源码精读

- [src/recurrent_state.rs:L37-L51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L37-L51) — 结构体文档讲清两个使用模式与「底层分配地址固定 ⇒ 表建一次反复用」的理由。
- [src/recurrent_state.rs:L112-L147](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L112-L147) — `with_capacity`（按层 × 容量零分配 u64 表）与 `from_recurrent_refs`（建表 + 立即填满）。
- [src/recurrent_state.rs:L149-L204](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L149-L204) — `refill_from_recurrent_refs`：对每层收集 `batch_size` 个 `device_ptr_mut` 地址（[L177-L191](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L177-L191)），一次 `memcpy_htod` 写入既有表。两个 `ensure!` 先验容量；文档明确 **batch 之后的行保留旧值且必须不被寻址**——内核只拿到 `batch_size`。
- [src/recurrent_state.rs:L206-L226](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L206-L226) — `validate_for`：容量 ≥ batch、表层数 = 线性层数。启动期自检用。
- [src/batch_decode_graph.rs:L32-L103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L32-L103) — `BatchDecodeGraphState::with_capacity`：为 `max_batch` 个槽各建一份 `RecurrentState`（[L77-L80](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L77-L80)），再把全部槽的地址一次性灌入指针表（[L81-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L81-L90)）。结构体文档（[L32-L49](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L32-L49)）定义了槽位契约：**槽 i ⇔ 批位置 i**，请求住进槽 i 后状态终生在 `slot_states[i]`，因此某桶的图永远访问 `slot_states[0..bucket]`，重放地址逐位一致。
- [src/batch_decode_graph.rs:L105-L167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L105-L167) — 槽位管理的两个原语：`copy_state_to_slot`（请求 prefill 完成后把瞬态状态 D2D **拷入**槽 i 的既有分配——地址不变，图不失效）与 `move_slot_within`（TP 压缩：末槽搬进空槽保持行密集；**from 槽留旧字节是良性的**，因为准入会整体覆写、padding 行可能改写它，而它不再被任何活跃行寻址）。
- [src/batch_decode.rs:L840-L906](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L840-L906) — `batch_decode_linear_attention_slots`：4.3.2 伪代码的真实实现。GDR 调用（[L871-L885](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L871-L885)）把 `state_ptrs`、头数、两个 head dim 全部传入；头数用 `geometry.local_*`，head dim 用 `config.linear_*`——这个「头数可变、维度烘焙」的分工在内核侧也有断言。
- [src/batch_decode.rs:L651-L683](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L651-L683) — 层循环分派：`linear_idx` / `full_idx` 两个计数器把模型层序分别映射到线性层表与全注意力层表，线性层取 `&linear_state_ptrs[linear_idx]` 传入。
- [../pegainfer-kernels/src/ops/qwen35.rs:L511-L522](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L511-L522) — GDR 批量 decode 的文档契约：`qkv` 是 **post-conv、post-SiLU** 的投影，`[q|k|v]` 逐 token 排布（q、k 段按 key head 计宽，v 段按 value head 计宽）；`state_ptrs` 每行指向一个请求的 `[H,K,V]` f32 状态，「读取并原地更新」；head dim 必须是烘焙值而**头数自由**——换 value head 数不需要重编内核。
- [src/batch_decode.rs:L461-L466](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L461-L466) — `seq_len` 的守卫用法：decode 前断言 `slot_states[i].seq_len == pos`，decode 后 `+= 1`。host 侧计数与设备侧状态同步推进，错位立刻暴露。

#### 4.3.4 代码实践

**实践目标**：画出一批 8 个请求的 decode 步中，GDN 状态「从槽到内核」的完整寻址链。

**操作步骤**：

1. 从 [src/batch_decode_graph.rs:L77-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L77-L90) 出发，画出：`slot_states[i].layers[ℓ].state`（分配）→ `refill/from_recurrent_refs` 收集地址 → `linear_pointer_tables.state_ptrs[ℓ][i]`（u64 行）→ `batch_decode_linear_attention_slots` 取 `&linear_state_ptrs[ℓ]` → 内核 `state_ptrs` 参数。标注每个箭头发生在什么时机（建表一次 / 每 token 重放）。
2. 回答：请求 R 从槽 3 换到槽 5（TP 压缩），指针表需要重灌吗？
3. 核对 [src/tp_executor/worker.rs:L399-L404](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L399-L404) 附近 `LinearStatePointerTables::with_capacity` 的调用，确认 TP eager 路径用的是「容量建表 + 每步 refill」模式。

**需要观察的现象**：单 GPU 图路径中，同一批请求连续 decode 多个 token，指针表内容一个字节都不变；只有准入/退出/压缩发生时状态**内容**搬迁，而地址不搬。

**预期结果**：第 2 步答案是**不需要**——`move_slot_within` 把槽 3 的字节 D2D 拷进槽 5 的既有分配，槽 5 的地址早在建表时就写进表里了；表永远指向槽，不指向请求。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `refill_from_recurrent_refs` 之后 batch 之外的行可以留着过期指针？
**答案**：内核拿到的 batch 计数参数就是活跃行数，GPU 线程按 `i < batch_size` 寻址，过期行根本不会被读；表按容量一次分配则是为了避免每步 cudaMalloc（既慢又破坏图/流语义）。

**练习 2**：`copy_state_to_slot` 与 `move_slot_within` 的调用时机分别是什么？
**答案**：`copy_state_to_slot` 在**请求 prefill 完成、加入 decode 批**时调用一次，把 prefill 瞬态状态灌进槽；`move_slot_within` 在 **TP 批内某请求中途退出**时调用，把最后一个占用槽搬进空出的槽，保证 decode 行密集（`0..bs`），这是 TP 图重放的行集前提（详见 u7-l3）。

**练习 3**：假如让每个请求的 `RecurrentState` 在 decode 中途重新分配（比如增长容量），会发生什么？
**答案**：设备地址改变，指针表过期，而已捕获的 CUDA Graph 里烤的还是旧地址——重放会读写已释放/错误的内存。所以本 crate 的所有状态操作（copy_from、move_slot_within）都是**向既有分配写入**，从不换缓冲。

### 4.4 TP 分片不变量：按 value head 分片、从不 all-reduce

#### 4.4.1 概念说明

GDN 状态天然按 value head 可分：状态是「每头一个 \([d_k, d_v]\) 矩阵」，头与头之间在递推中**互不交换数据**。于是 TP 的切法是：把 \(H_v\) 个 value head（连同 qkv 投影的对应行段、beta/alpha/dt_bias/a_log 等每头向量）均匀分给各 rank，每个 rank 只为自己那 \(H_v/W\) 个头维护并更新状态。分片是不相交的——各 rank 状态拼起来恰是完整状态，因此**没有任何理由对状态做 all-reduce**。

跨 rank 聚合只发生在回到 hidden 维的那一刻：`out_proj` 是行并行（各 rank 算出 hidden 维的**部分和**），随后一次 `all_reduce_hidden` 求和。这与 u2-l4 的结论「TP 通信点统一在回到 hidden 维处」一致，且模块文档直接写明状态「never all-reduced」。

#### 4.4.2 核心流程

TP 下每 rank 的状态尺寸：

\[
H_v^{local} = H_v / W, \qquad H_k^{local} = H_k / W
\]

\[
S_{state}^{local} = \frac{H_v}{W} \times d_k \times d_v, \qquad
S_{conv}^{local} = \Big(2 \frac{H_k}{W} d_k + \frac{H_v}{W} d_v\Big)(k_{conv}-1)
\]

分片正确性依赖两条上游约束：

1. **头分组不变量**（Config35 层）：\(H_v\) 是 \(H_k\) 的整数倍（一个 key head 服务一组 value head），`ConfigError::LinearHeadDivisibility` 强制。
2. **整除门**（LocalGeometry 层）：\(H_k\)（连同注意力头、KV 头、intermediate）必须被 \(W\) 整除，`TpIndivisible` fail-closed。

由 1、2 可推出 \(H_v\) 免检：设 \(H_v = m \cdot H_k\) 且 \(H_k = W \cdot k\)，则 \(H_v = W \cdot (m k)\)，自动整除——这就是 tp.rs 文档说「value-head count follows from the Config35 key/value-head invariant」的推理链。

每个 rank 状态字节占总量的比例：

\[
\frac{B_{req}^{local}}{B_{req}} = \frac{1}{W}
\]

（qkv 通道同样按行分片，conv 状态也随之精确减半。）**精确 \(1/W\)、零复制、零通信**。

#### 4.4.3 源码精读

- [src/config/tp.rs:L93-L138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L93-L138) — `LocalGeometry::try_new` 的四条有序整除门。注意线性注意力**只检查 `linear_num_key_heads`**（[L132-L138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L132-L138)），并注明「fail closed 而非降级复制」；value head 数凭上游倍数不变量免检（[L96-L99](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L96-L99) 文档）。对应 config 侧的倍数检查在 [src/config/model.rs:L254-L263](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L254-L263)。
- [src/config/tp.rs:L140-L165](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L140-L165) — 本地维度派生：`local_linear_num_value_heads = linear_num_value_heads / W`（[L146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L146)）、q 段「按 key head 计宽（一个 key head 对一组 value head）」的注释（[L147-L150](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L147-L150)）、`local_linear_qkv_dim = 2·q_dim + v_dim`（[L164](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L164)）。TP1 契约注释（[L209-L210](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L209-L210)）：W=1 时所有 local 维等于 global 维，内核/缓冲/状态保持原形。
- [src/recurrent_state.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L1-L9) — 「value heads（及融合 qkv 通道）跨 rank 分片，各 rank 拥有自己的 recurrent/conv 状态，从不 all-reduce」——不变量的原文。
- [src/weights/layers.rs:L30-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L30-L55) — 权重侧印证：`in_proj_qkvz` 行维是 `local_linear_qkv_dim + local_linear_z_dim`、`in_proj_ba` 是 `2 × local_linear_num_value_heads`、`dt_bias`/`a_log` 按 local value head 数宽、`conv1d_weight` 按 local qkv 通道宽——**每头向量与通道全部随 value head 分片**；而 `norm_weight` 是 head 共享（按 \(d_v\) 宽），各 rank 复制。`out_proj` 注释直接点明「行并行，层在 TP 下 all-reduce 部分和」。
- [src/batch_decode.rs:L898-L905](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L898-L905) — 唯一通信点：`out_proj` GEMM 之后 `all_reduce_hidden`。实现在 [src/weights.rs:L465-L471](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L465-L471)：TP1 时 `tp_comm` 为 None，函数是空操作——单卡路径零开销。
- [../pegainfer-kernels/src/ops/qwen35.rs:L550-L565](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/qwen35.rs#L550-L565) — 内核侧断言：`num_value_heads` 必须是 `num_key_heads` 的整数倍（头分组不变量在内核边界再验一次），head dim 必须等于烘焙值，而头数自由——所以 TP 改变头数**不需要重编内核**，这也解释了为什么状态尺寸公式只依赖运行期头数。

#### 4.4.4 代码实践

**实践目标**：用数字验证「TP 下各 rank 状态精确减半、总和不变」，体会免检推理。

**操作步骤**（4B 配置，TP2）：

1. \(H_v^{local} = 32/2 = 16\)，\(H_k^{local} = 16/2 = 8\)。
2. state：\(16 \times 128 \times 128 = 262{,}144\) f32 = 1 MiB/层。
3. \(d_{qkv}^{local} = 2\times(8\times128) + 16\times128 = 4096\)；conv：\(4096\times3 = 12{,}288\) bf16 = 24 KiB/层。
4. 每请求每 rank：\(24 \times (1\,\text{MiB} + 24\,\text{KiB}) = 25{,}755{,}648\) B = 24.5625 MiB；两 rank 合计 \(= 49.125\) MiB，与 TP1 每请求总量**逐字节相等**。
5. 复核免检链：\(H_v = 32 = 2 \times H_k = 2 \times 16\)，\(H_k = 2 \times 8\) ⟹ \(H_v = 2 \times (2\times 8)\)，被 2 整除。再用 [src/config/tp.rs:L342-L359](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L342-L359) 的测试 `requires_linear_attention_key_head_divisibility` 确认：只有 key head 不整除时才报 `TpIndivisible`。

**需要观察的现象**：第 4 步的两 rank 合计与 4.1.4 手算的 TP1 每请求字节数完全相同（51,511,296 B）——分片零复制、零冗余。

**预期结果**：TP2 每 rank 24.5625 MiB/请求；64 槽 × 2 预留在每 rank 上约 \(128 \times 24.5625 = 3144\) MiB ≈ 3.07 GiB（TP 每卡只需一半状态显存）。纸面可完成，无需设备。

#### 4.4.5 小练习与答案

**练习 1**：为什么 GDN 状态从不 all-reduce，而 MLP 的输出要 all-reduce？
**答案**：状态按 value head 不相交分片，各 rank 更新的切片拼起来就是完整状态，无信息重叠；而 `down_proj`/`out_proj` 是行并行，各 rank 只产出 hidden 维部分和，必须求和才能得到正确残差流。通信只发生在「回到 hidden 维」处。

**练习 2**：如果某配置 \(H_k = 17\)、\(H_v = 34\)、W=2，会触发哪个错误？
**答案**：`ConfigError::TpIndivisible { field: "linear_num_key_heads", value: 17, world_size: 2 }`。注意 \(H_v = 34\) 虽然能被 2 整除也救不了——免检的前提是 \(H_k\) 先过整除门，这正是 tp.rs 测试刻意保持 value = 2×key 不变量再只打破 key 整除性的原因。

**练习 3**：TP 下 f32 状态的精度策略变了吗？
**答案**：没有。状态仍是每 rank 本地的 f32 `[H,K,V]`，公式与内核与 TP1 完全相同（TP1 契约：W=1 时 local 即 global）；TP 改变的只是头数，而头数是内核的运行期参数。

## 5. 综合实践

为 Qwen3.5-4B 建立一份「**GDN 状态账本**」，把本讲全部内容串起来：

1. **布局列**（4.1）：对 TP1 与 TP2 各列一行——每层 state 元素数/字节、conv 元素数/字节、每请求 24 层总字节。来源：`per_layer_dims` 与 `bytes_per_request` 手算。
2. **预留列**（4.1）：`STATES_PER_DECODE_SLOT=2 × 64 槽`的 recurrent 预留（TP1 ≈ 6.14 GiB，TP2 每 rank ≈ 3.07 GiB），并注明它在 weights.rs 显存预算顺序中的位置（scratch → recurrent → snapshot → KV 池余量 85%）。
3. **实例清单**（4.1/4.3）：用 Grep 找出所有 `RecurrentState::new` 调用点并注明角色——图槽 ×64（`batch_decode_graph.rs`）、prefill 瞬态（`scheduler/backend.rs::alloc_recurrent`、`unified_forward.rs`）、teacher-force 执行器（`executor.rs`）、前缀快照槽（`prefix_cache.rs::RecurrentStateStore`）。每份的大小都由同一个 `bytes_per_request` 决定。
4. **寻址链**（4.3）：画「槽 → 指针表 → 内核」的箭头图，标注 `copy_state_to_slot` / `move_slot_within` 两个原语作用在哪条边上，并写一句为什么两者都不改地址。
5. **验证**（有构建工具链时）：跑 `cargo test --release -p pegainfer-qwen35 --features qwen35 --lib recurrent`，确认 `qwen35_4b_recurrent_allocation_is_49_125_mib` 与你的账本一致；无工具链则账本本身就是交付物，运行验证标注「待本地验证」。

完成标准：账本中任何一个数字都能回溯到 `per_layer_dims` / `bytes_per_request` 的一次代入，且 TP1 与 TP×W 的数字满足「每 rank = 总量 / W」。

## 6. 本讲小结

- `RecurrentState` = 24 份 `LayerRecurrentState` + `seq_len`；每层是 `[H_v^local·d_k·d_v]` 个 f32（GDN 递推状态，长期累积故用 f32）+ `[d_qkv^local·(k_conv-1)]` 个 bf16（卷积抽头，原样字节故用 bf16）。
- 所有尺寸出自唯一的 `per_layer_dims`；分配、显存预留、前缀快照槽数共享它，4B 每请求 49.125 MiB。
- causal conv1d 有 prefill（整段 + 末尾抽头落盘）与 decode（读抽头、原地滚动、融合 SiLU）两套内核，`conv1d_prefill_handoff` 证明 chunk 接力与整段等价。
- `LinearStatePointerTables` 用「槽地址终身不变」换来一次建表、token 间重放，是批量内核与 CUDA Graph 同时成立的前提；状态迁移原语一律向既有分配写入。
- TP 按 value head（连同 qkv 通道与每头向量）不相交分片，状态从不 all-reduce；唯一通信点是 `out_proj` 后的 `all_reduce_hidden`，各 rank 状态精确等于总量的 1/W。
- 头数是运行期参数、head dim 是编译期烘焙（128/128、卷积宽 ≤4）——config 加载期的三条门就是这两层事实的守门人。

## 7. 下一步学习建议

- **u3-l3（FFI 边界与内核封装）**：本讲只把 `gated_delta_rule_prefill_chunkwise_into` 当黑盒流水线；下一讲拆 `unsafe` FFI 调用的指针 guard 生命周期、`device_ptr`/`device_ptr_mut` 模式与 chunkwise 七段的算子契约。
- **u4-l1（Prefill 分块）**：本讲看到 conv/GDR 状态在单 chunk 内推进；下一讲看 `PREFILL_CHUNK_LEN` 如何把长提示切成 chunk 串行接力，以及 `GdrChunkwiseScratch35` 的保守预留。
- **u6-l1/l2（前缀缓存）**：`RecurrentStateStore` 的快照槽就是本讲的 `RecurrentState` 按预算复用——建议带着「快照 = 状态字节照搬」的直觉去读 `PrefixBoundaryKey` 与恢复路径。
- 想先摸执行全景的读者也可跳到 **u4-l2（batch_decode）**：本讲 4.3.2 的六步序列就是它核心循环的线性层分支。
