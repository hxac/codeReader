# 全注意力层：paged KV、partial RoPE 与 hd256 内核族

## 1. 本讲目标

学完本讲，你应该能够：

1. 追踪 hd256 全注意力 **prefill / decode 两条内核调用链**的每一个封装函数与参数，说出每个指针参数对应哪块显存、每个整数参数来自 config 还是 geometry。
2. 理解 **partial RoPE** 的 cos/sin 缓存组织：为什么只旋转每头前 `rotary_dim = 64` 维、表为什么是 half-split 重复布局、以及运行期的「覆盖守卫」`ensure_rope_cache_covers` 到底检查什么（并澄清：它**不**扩容）。
3. 画出 **分页 KV 池**的物理布局（页 → 层 → K/V 块 → token → head → dim），用 `KvLayout` 的三个步长公式手算任意 (层, 页, token, head) 的元素偏移。
4. 解释 **qk_norm 与 GQA 分组如何在内核参数里表达**：`[head_dim]` 共享权重指针 + `num_attention_heads`/`num_key_value_heads` 这对整数，以及 GQA 组比率如何在 decode 侧决定三条内核路线（split-KV / FlashInfer 批量 / via-prefill 回退）。

## 2. 前置知识

本讲直接承接 u2-l4：你已经知道 `FullAttentionLayer` 只装权重、前向五步是「投影 → 融合预备 → paged attention → gate → o_proj」、`q_proj` 行含 per-head `[q | gate]` 交错块。本讲把其中的「融合预备」「paged attention」两步放大到参数级。再补三段直觉：

**为什么 KV 要分页**。如果把每个请求的 KV cache 放在一整块连续显存里，请求一长一短、来了又走，显存很快碎片化。分页方案把池切成固定大小的**页**（page，这里 16 个 token 一页），每个请求持有一张「页号表」，逻辑上连续、物理上散落。注意力内核不再假设 K/V 连续，而是拿 `page_indices` 数组间接寻址。描述「一个请求的 KV 长什么样」的类型就是 `KvView`（页号表 + 序列长 + 页大小），池本身是 `KvBuffer`——都来自 workspace 的 `pegainfer-kv-cache` crate（`layout.rs` / `view.rs` / `buffer.rs`）。

**什么是 partial RoPE**。RoPE（旋转位置编码）把每个 token 的 Q/K 向量按其位置旋转一个角度，让内积自然携带相对位置信息。**partial** 指只旋转每头前 `rotary_dim` 维（Qwen3.5 是 `head_dim × partial_rotary_factor = 256 × 0.25 = 64` 维），其余 192 维原样通过。旋转角只依赖 (位置, 维度对)，与 token 内容无关，所以整张 cos/sin 表可以在**模型加载时一次性预计算**，前向只查表——这就是「RoPE 缓存」。

**什么是 GQA**。Grouped-Query Attention：16 个 query head 共享 4 组 KV head（组比率 4），K/V 的存储与计算量直接除以 4。内核拿到 `num_attention_heads` 与 `num_key_value_heads` 两个数自己算分组；这个比率还决定了哪些 decode 内核可用（见 4.3）。

本讲所有具体数字以 **Qwen3.5-4B** 为例（出处 `docs/models/qwen35/decode-kernel-attribution.md`：hidden 2560，32 层 = 24 linear + 8 full attention，16 q heads / 4 kv heads，head_dim 256，GQA 4，vocab 248320；`docs/models/qwen35/optimization.md`：rotary_dim=64，theta=1e7）：

| 量 | 值 | 来源 |
|---|---|---|
| `hidden_size` | 2560 | config.json |
| 全注意力层（也即 KV 池层数） | 8 | `layer_types` 计数 |
| `num_attention_heads` / `num_key_value_heads` | 16 / 4 | config.json |
| `head_dim` | 256 | config.json（内核编译期约定） |
| `rotary_dim` | 256 × 0.25 = 64 | [src/config/model.rs:219-222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L219-L222) |
| `q_dim` / gated q 维 / `kv_dim` | 4096 / 8192 / 1024 | heads × head_dim（gate 翻倍） |
| `page_size` | 16 | [src/weights.rs:262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L262) |
| `max_position_embeddings` | 262144 | config.json |

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L305-L446) | prefill 侧全注意力主战场：chunk 的 `KvView`/`PrefillPagedPlan` 构建 + 三个 hd256 FFI 调用 |
| [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L112-L286) | decode 侧：`batch_decode_full_attention` 与 via-prefill 回退、GQA 路由 |
| [src/decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L1-L268) | decode 预分配缓冲的形状清单 + split-KV 阈值常量 + `sync_paged_views` 页表上传 |
| [src/ops.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L1-L29) | 29 行算子重导出面：本讲「函数名字典」，hd256 内核全部来自 `pegainfer_kernels::ops` |
| [src/weights.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L236-L275) | RoPE 缓存预计算、`ensure_rope_cache_covers`、KV 池布局与容量 |
| [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L60-L68) | `rotary_dim` 推导、head_dim=256 编译期门、`decode_group_is_compiled` |
| [src/config/tp.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L140-L165) | `local_*` 维度派生（decode 内核吃的是 rank 本地头数） |
| [src/weights/layers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights/layers.rs#L15-L28) | `FullAttentionLayer` 权重形状（u2-l4 已精读，本讲当字典查） |

内核本体在本 crate 之外，供对照：封装在 `pegainfer-kernels/src/ops/qwen35.rs`，CUDA 实现在 `pegainfer-kernels/csrc/qwen35/prefill_attention_hd256.cu`；RoPE 表生成在 `pegainfer-core/src/rope.rs`；分页池在 `pegainfer-kv-cache/src/{layout,view,buffer}.rs`。

## 4. 核心概念与源码讲解

### 4.1 RoPE 缓存：partial 旋转的查表与覆盖守卫

#### 4.1.1 概念说明

RoPE 对每对维度 \((i,\, i+h)\)（\(h = \text{rotary\_dim}/2\)）施加随位置 \(p\) 旋转的二维旋转：

\[
\begin{pmatrix} x'_i \\ x'_{i+h} \end{pmatrix}
=
\begin{pmatrix}
\cos(p\theta_i) & -\sin(p\theta_i) \\
\sin(p\theta_i) & \cos(p\theta_i)
\end{pmatrix}
\begin{pmatrix} x_i \\ x_{i+h} \end{pmatrix},
\qquad
\theta_i = \text{theta}^{-2i/\text{rotary\_dim}}
\]

要点有三个：

1. **旋转角与内容无关**，只依赖 \((p, i)\)——所以 `\cos(p\theta_i)`、`\sin(p\theta_i)` 可以整表预计算，内核里零三角函数。
2. **partial**：只对每头前 64 维做上述变换（\(i \in [0, 32)\)，搭档是 \(i+32\)），第 64 维起原样拷贝。CUDA 预备内核里就是这样写的：`d >= rotary_dim` 的分量直接 `q_batch_out[dst + d] = smem[d]`。
3. **half-split（对半拆）布局**：Q/K 向量被当作「前半 32 维 + 后半 32 维」两段，旋转在两段**同下标**之间配对进行。为了让内核按下标线性读取，cos/sin 表每位置存成 `[cos(0)..cos(31), cos(0)..cos(31)]` ——**前后两半重复**。这是 NeoX 系 RoPE 的标准 trick，`pegainfer-core/src/rope.rs` 的 `tables()` 注释原话是 "Half-split layout"。

缓存属于**模型**而不是层：所有 8 个全注意力层共享同一张表（`Qwen35Model` 的字段，[src/weights.rs:65-67](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L65-L67)，注释即 `[max_seq_len * rotary_dim]`）。

> **勘误（相对本讲规划稿）**：规划稿写「按需扩容」，但当前代码**没有扩容路径**——表在加载期按 `max_position_embeddings` 一次建满（262144 个位置），运行期只有 `ensure_rope_cache_covers` 这个**纯校验守卫**：超覆盖即报错，绝不重算。学习以代码为准。

#### 4.1.2 核心流程

```
加载期（一次）:
  rotary_dim = head_dim × partial_rotary_factor          # config 校验，0 即拒绝
  cos/sin ← precompute_rope(RopeTableSpec{
      rotary_dim, frequency_dim: rotary_dim,             # 两者相等 → 频率基只用 rotary_dim
      max_seq_len: max_position_embeddings, theta })
  # 每张表 262144 × 64 个 bf16 = 32 MiB，两张共 64 MiB

前向期（每步，零计算只查表）:
  prefill: 整段先校验 ensure_rope_cache_covers(end_pos)，再进预备内核
  decode:  逐请求校验 ensure_rope_cache_covers(pos + 1)，内核按 positions_d 查表
```

#### 4.1.3 源码精读

**rotary_dim 的出生地**。[src/config/model.rs:219-222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L219-L222)：

```rust
let rotary_dim = (t.head_dim as f64 * t.rope_parameters.partial_rotary_factor) as usize;
if rotary_dim == 0 {
    return Err(ConfigError::ZeroRotaryDim);
}
```

`partial_rotary_factor` 与 `rope_theta` 只从 `rope_parameters` 块读这两个键（[src/config/model.rs:22-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L22-L27)）。4B：256 × 0.25 = 64。

**预计算调用**。[src/weights.rs:236-248](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L236-L248)：

```rust
let (cos_cache, sin_cache) = precompute_rope(
    &ctx,
    &RopeTableSpec {
        rotary_dim: config.rotary_dim,
        frequency_dim: config.rotary_dim,   // 频率分母也用 64，不是 256
        max_seq_len: config.max_position_embeddings,
        theta: config.rope_theta,
    },
)?;
```

注意 `frequency_dim = rotary_dim`：频率 \(\theta_i = \text{theta}^{-2i/64}\) 在被旋转的 64 维里走完整个频率范围，而不是摊到 256 维上——这是 partial RoPE 与「全旋转只取前 64 维」的关键区别。表布局是 `[位置 × rotary_dim]`，位置 `pos` 落在偏移 `pos * rotary_dim` 处，内核索引正是 `cos_cache[pos * rotary_dim + d]`。

**覆盖守卫**。[src/weights.rs:407-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L407-L415)：

```rust
pub(crate) fn ensure_rope_cache_covers(&self, positions: usize) -> Result<()> {
    let cache_positions = self.cos_cache.len / self.config.rotary_dim;
    anyhow::ensure!(
        positions <= cache_positions,
        "Qwen3.5 RoPE cache covers {cache_positions} positions, requested {positions}; ...",
    );
    Ok(())
}
```

细节有二：它从**实际缓冲长度**反推覆盖数（`len / rotary_dim`），不信任 config 字段——即使未来有人改小缓冲，守卫依然正确；其次调用时机分层——prefill 在动任何状态**之前**对整段终点校验（`end_pos`，[src/prefill.rs:69-87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L69-L87)，与位置溢出、KV 视图长度一起构成「先整段校验后变更」），decode 则在每个请求自己的位置上校验（`pos + 1`，[src/batch_decode.rs:329](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L329)、[src/batch_decode.rs:465](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L465)）。由于 `end_pos ≤ max_position_embeddings` 已在 prefill 入口挡了一道，这道守卫当前实际是纵深防御。

**消费点**。prefill 把两张表的指针直接塞进融合预备内核（[src/prefill.rs:344-345](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L344-L345)）；decode 侧 `qk_norm_partial_rope_batched_decode_hd256_into` 的参数表里同样有 `cos_cache`/`sin_cache`/`positions_d` 三件套（[src/batch_decode.rs:130-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L130-L144)）。两条路径查的是**同一张表**，这也是「表属于模型」的直接收益。

#### 4.1.4 代码实践

**实践目标**：纸上验证缓存尺寸与守卫边界，确认「一次建满、绝不扩容」。

**操作步骤**：

1. 用 4B 数字手算：`rotary_dim`、一张表的元素数（`max_position_embeddings × rotary_dim`）、bf16 字节数、两张表合计。
2. 读 [src/weights.rs:407-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L407-L415)，回答：`ensure_rope_cache_covers(262144)` 通过吗？`262145` 呢？提示：`positions <= cache_positions`，边界取等。
3. 用 `Grep` 在 `src/` 搜 `precompute_rope`，确认全 crate 只有一处调用（加载期）；再搜 `ensure_rope_cache_covers` 数一数调用点，按 prefill / eager decode / graph decode / hybrid decode 归类。

**需要观察的现象**：`precompute_rope` 仅出现在 weights.rs 加载路径；四个守卫调用点全部在「位置即将被使用」的现场，且都在内核发射**之前**。

**预期结果**：表元素数 = 262144 × 64 = 16,777,216，单表 32 MiB（bf16），cos+sin 共 64 MiB；`262144` 通过（取等），`262145` 报错。守卫调用点：prefill.rs:87 一个 + batch_decode.rs 三个（eager 329、graph 465、hybrid 549）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `frequency_dim` 传 `rotary_dim` 而不是 `head_dim`？传错了会发生什么？
**答案**：`frequency_dim` 是频率公式的分母 \(\theta^{-2i/f}\)。传 64 时，64 个被旋转维度在 theta=1e7 的完整频率范围内取值（\(\theta_0=1\)、\(\theta_{31}=10^{-7\times 62/64}\)）；若传 256，所有频率会挤进 \([10^{-7\times 126/256}, 1]\) 的高频段，长文本的位置分辨率劣化——数学上仍是合法 RoPE，但与 HF 权重的预期编码不一致，logits 会对不上。`pegainfer-core/src/rope.rs` 的校验只要求 `rotary_dim <= frequency_dim`，两种都放行，所以这里的取值是与 checkpoint 的对齐约定。

**练习 2**：cos 表为什么前后两半重复？不重复（每位置只存 32 个 cos）行不行？
**答案**：行，但内核要多一次地址判断。half-split 布局下 Q/K 的第 \(i\) 维与第 \(i+32\) 维配对旋转，两行都要读 \(\cos(p\theta_i)\)；表若按 64 项存（重复两半），两行的读取地址就是线性的 `base + i` 与 `base + i + 32`，无需分支。这是用 2 倍显存（每表 32 MiB 而非 16 MiB）换内核寻址的简化——在 64 MiB 量级上，这笔交易显然划算。

**练习 3**：decode 为什么是 `ensure_rope_cache_covers(pos + 1)` 而不是 `pos`？
**答案**：`pos = view.seq_len() - 1` 是**本步要写入的 token 位置**（KvView 描述的是步后长度，[src/batch_decode.rs:319-323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L319-L323) 的注释）。查表需要覆盖**闭区间** `[0, pos]`，即「前 pos+1 个位置都有表项」；只查 `pos` 会漏掉边界：若表恰好只建到 `pos` 项，位置 `pos` 本身就越界了。prefill 侧传 `end_pos`（开区间终点，位置上限 `end_pos - 1`）与此等价。

### 4.2 paged attention 内核：分页布局、PrefillPagedPlan 与两条调用链

#### 4.2.1 概念说明

分页 KV 池是一维的 `bf16` 大数组，物理结构由 `KvLayout` 的三个步长完全描述（字段定义在 `pegainfer-kv-cache/src/layout.rs`）：

\[
\text{kv\_block\_len} = \text{page\_size} \times H_{kv} \times d,
\quad
\text{layer\_stride} = 2 \times \text{kv\_block\_len},
\quad
\text{page\_stride} = L_{full} \times \text{layer\_stride}
\]

即**页 → 层 → K 块/V 块 → token → head → dim** 的逐级嵌套：一页里先按层铺，每层内 K 块在前、V 块紧随，块内按 (token, head, dim) 行主序。4B 代入（页 16 token、4 KV 头、d=256、8 层）：`kv_block_len = 16384` 元素，`layer_stride = 32768`，`page_stride = 262144` 元素 = **512 KiB/页**；摊到每 token 是 \(2 \times 8 \times 4 \times 256 \times 2\text{B} = 32\text{KiB}\)。

于是任意 (层 `l`, 页 `p`, token `t`, head `h`, dim `d`) 的 K 地址：

\[
\text{offset}_K = l \cdot \text{layer\_stride} + p \cdot \text{page\_stride} + (t \cdot H_{kv} + h) \cdot d + \text{dim}
\]

V 地址只是把第一项换成 `l·layer_stride + kv_block_len`——这正是调用点手算 `layer_k_off`/`layer_v_off` 的公式（[src/prefill.rs:332-335](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L332-L335)）。

`KvView` 是请求侧的轻量句柄：`(page_indices: Vec<i32>, seq_len, page_size)`，构造时断言**页表恰好覆盖序列**（多一页读到垃圾、少一页越界，注释点名 #291）；`last_page_len()` 在整页时返回 `page_size` 而非 0，使内核能用 `(num_pages-1)·page_size + last_page_len` 还原长度。池容量在加载期按「扣掉 scratch/递推/快照预留后剩余的 85%」折算成页数（[src/weights.rs:316-334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L316-L334)）。

#### 4.2.2 核心流程

**prefill 调用链**（一个 chunk，[src/prefill.rs:305-446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L305-L446)）：

```
1. q_full = gemm(q_proj[8192×2560], normed)      # Q 与 gate 交错
   k     = gemm(k_proj[1024×2560], normed)
   v     = gemm(v_proj[1024×2560], normed)
2. chunk_view = KvView(全量页表[..page_count], chunk_end)   # 本 chunk 可见的 KV 前缀
   plan = PrefillPagedPlan::from_raw_batch_with_cta_tile_q(
       页表, [last_page_len], [base_pos], [chunk_len], 16头, 4头, 256, cta_tile_q=0)
3. ffi::prefill_attention_hd256_prep_paged_cuda   # 一个内核做三件事:
      a. Q/K 各过 [256] 共享 RMSNorm (eps)
      b. Q/K 前 64 维查 cos/sin 表做 partial RoPE → q_prepped[4096×seq]
      c. K/V 按 start_pos..start_pos+seq 逐 token 写入池(页表寻址)
4. ffi::batch_prefill_paged_cuda_hd256            # FA2 式分块注意力, 读池, sm_scale=1/√256
5. ffi::attention_gate_batch_hd256_cuda           # attn_out ⊙ sigmoid(gate), 原地
6. gemm(o_proj[2560×4096]) → all_reduce_hidden
```

步骤 2 的 `PrefillPagedPlan`（重导出自 `pegainfer_core::ops`，[src/ops.rs:5](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L5)）把页表加工成 FA2 内核要的设备侧元数据：`page_indices_d`/`page_indptr_d`（页表 + 前缀和）、`last_page_len_d`、`q_indptr_d`/`request_indices_d`（Q 行归属）、`qo_tile_indices_d`/`kv_tile_indices_d`/`kv_chunk_size_d`/`total_num_rows_d`（Q/KV 分块调度）。`cta_tile_q = 0` 表示让内核按自己的 FA2 推导取 tile（[src/batch_decode.rs:593-596](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L593-L596) 注释：hd256 FFI 不接受 override）。

**decode 调用链**（[src/batch_decode.rs:112-214](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L112-L214)）结构同构，两处关键差异：

- **谁写池**：decode 的预备内核 `qk_norm_partial_rope_batched_decode_hd256_into` 只做 QK-norm + partial RoPE（Q 从交错的 `q_full` 抽取到 `q_attn`、K 原地处理），**不碰池**；本步 K/V 的落页藏在注意力包装函数开头——`paged_attention_batch_decode_*_hd256_into` 内部先调 `scatter_decode_kv_into_paged` 按 `positions_d` 写池再发射注意力内核。u2-l4 已指出这个不对称，本讲看参数就知道为什么：decode 预备内核签名里根本没有页表与池指针（[src/batch_decode.rs:130-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L130-L144)）。
- **位置语义**：prefill 每个 chunk 一个标量 `start_pos`（单个 `i32` 上传，[src/prefill.rs:327-331](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L327-L331)）；decode 是**每请求一个位置**的 `positions_d` 数组——N 个请求各 1 token，各查各的表、各写各的页。

#### 4.2.3 源码精读

**chunk 视图与 plan 构建**。[src/prefill.rs:94-120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L94-L120)：`page_count = chunk_end.div_ceil(page_size)`，chunk 视图截取全量页表的前缀——前一个 chunk 写过的页对本 chunk 可见（因果性由 `start_pos` 起点保证）。plan 参数按请求逐项传单元素切片（prefill 的 plan 只装这一个 chunk 的这一个请求）。

**偏移手算现场**。分两处：[src/prefill.rs:121-135](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L121-L135) 把池裸指针、`KvLayout`、plan 与 `base_pos` 打包成 `PrefillKvAccess` 一路传下去；随后 [src/prefill.rs:332-335](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L332-L335) 算出 `layer_k_off = full_idx * layer_stride`、`layer_v_off = layer_k_off + kv_block_len`、`stride_page = page_stride` 三个 `i64`——它们就是地址公式的三项系数。

**prep 内核的参数表**。[src/prefill.rs:337-373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L337-L373) 一次 FFI 调用吃进 20 个参数，按用途分四组：输入投影 `q_full/k/v`；norm 与位置 `q_norm/k_norm/cos/sin/start_pos`；输出 `q_prepped` 与池 `buf + layer_k_off/layer_v_off/page_indices`；几何 `num_attention_heads/num_key_value_heads/seq_len/rotary_dim/eps/page_size/stride_page`。**qk_norm 与 GQA 的表达就在这里**：norm 是两个 `[256]` 指针（head 共享，u2-l4 讲过权重形状），GQA 是 `16/4` 这对整数——内核按 `kv_head = q_head / (H_q/H_kv)` 把 query head 映射到 KV head。

**prefill 注意力内核**。[src/prefill.rs:375-423](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L375-L423)：输入 `q_prepped` 与九个 plan 设备数组 + `layer_k_off/layer_v_off` + 几何 + `sm_scale`，返回码非 0 时用 `ffi_exception_message` 展开错误（这是 prefill 侧唯一检查返回码的 hd256 调用）。

**decode 的页表上传**。[src/decode_buffers.rs:216-267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L216-L267)：`sync_paged_views` 把 N 个 `KvView` 拼成扁平页表 + `indptr` 前缀和 + `last_page_len` + `kv_chunk_size`，一次 H2D 上传。**padding 槽指向保留页**（`padding_page_id`，seq_len=1）——CUDA Graph 重放时 batch 桶固定，凑不满的行也要让内核读到合法地址，这是 u4-l3 的伏笔，此处先记住「padding 行的页表项是真的」。

**decode 的两个缓冲族**。[src/decode_buffers.rs:60-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L60-L74)：全注意力五件套 `q_full/q_attn/k_attn/v_attn/attn_out_full` 的宽度取自 geometry（[src/decode_buffers.rs:119-124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L119-L124)：`q_proj_dim = local_full_attn_gated_q_dim()` = 8192，`q_dim` = 4096，`kv_dim` = 1024）；split-KV 的三块累加器 `split_partial_o [splits, bs, heads, 256]` f32 与 `split_partial_m/l [splits, bs, heads]` 按 4.3 的阈值定容（[src/decode_buffers.rs:144-159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L144-L159)，注意 `bs.min(SPLIT_DECODE_MAX_BATCH)`——只为 split 服务的小桶定容）。

#### 4.2.4 代码实践

**实践目标**：给定一个请求（seq_len=128，页表 `[P0..P7]`），手算 decode 一步的全部页表元数据与第 3 个全注意力层的池偏移。

**操作步骤**：

1. 算页数与尾页长：`128.div_ceil(16)`、`128 % 16`（注意 `last_page_len` 整页时的特殊返回值，去看 `KvView::last_page_len` 的实现或本讲 4.2.1）。
2. 写出 `sync_paged_views` 上传后的五个数组内容（batch=1、无 padding）：`page_indices_d`、`page_indptr_d`、`last_page_len_d`、`kv_chunk_size_d`、`request_indices_d`，以及 `positions_d`（decode 位 = `seq_len - 1`）。
3. 用 4B 布局常量算 `full_idx = 3` 这一层的 `layer_k_off`、`layer_v_off`、`stride_page`（元素数），再算 token 127 的 K 在页 `P7` 内的块内偏移（`token_in_page × H_kv × d + ...`，`token_in_page = 127 % 16`）。

**需要观察的现象**：第 3 步的三个池级系数与请求无关（只由布局决定），块内偏移与请求无关（只由 token 在页内的位置决定）——**请求的个体性全部收敛在页表里**，这正是分页设计的意图。

**预期结果**：8 页、`last_page_len = 16`（整页返回 page_size 而非 0）；`page_indices_d = [P0..P7]`，`page_indptr_d = [0, 8]`，`last_page_len_d = [16]`，`kv_chunk_size_d = [128]`，`request_indices_d = [0]`，`positions_d = [127]`；`layer_k_off = 3 × 32768 = 98304`，`layer_v_off = 98304 + 16384 = 114688`，`stride_page = 262144`；token 127 在页内偏移 15，K 块内偏移起点 `15 × 1024 = 15360`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `KvView::new` 要断言「页数恰好等于 `seq_len.div_ceil(page_size)`」？给一个具体后果。
**答案**：内核用 `(num_pages - 1) × page_size + last_page_len` 还原序列长度并按页表访存。若页表多一页（如 128 token 挂 9 页），attention 会把第 9 页的陈旧/未初始化 K/V 当作合法历史算进 softmax——输出悄悄错，不越界不报错；少一页则直接越界读（注释引用的 #291 就是这类）。所以「恰好覆盖」在构造点用 `assert_eq!` 拦截。

**练习 2**：prefill 的 plan 参数里 `[base_pos + offset]` 与 `[chunk.len()]` 分别控制什么？
**答案**：前者是本 chunk 的起始绝对位置——既决定 RoPE 查表起点（预备内核用它推每个 token 的位置），也决定 K/V 落页的写入位置；后者是本 chunk 的 token 数，决定 plan 生成的 Q 行数与调度 tile 数。二者配合实现「多 chunk 串起来等价于一次整段 prefill」：位置连续、页表前缀叠加、recurrent 状态逐 chunk 推进。

**练习 3**：decode 的 `kv_chunk_size_d` 上传的是 `view.seq_len()`（如 128），为什么不是 1？这个 chunk 是给谁看的？
**答案**：它描述的是**KV 侧**的长度（该请求的池内历史共 128 个 token），不是本步 Q 的长度（Q 恒为 1 行）。split-KV 内核按 `kv_chunk_size` 把 KV 区间切成 `splits` 份并行归约；Q 侧的「每请求 1 行」由 `q_indptr`/`request_indices` 表达。一个数组管 KV 维度、一组索引管 Q 归属，两套元数据各司其职。

### 4.3 head_dim=256 约定：编译期门、sm_scale 与 GQA 路由

#### 4.3.1 概念说明

函数名后缀 `hd256` 不只是命名习惯，而是**编译期事实**：这些内核按 head_dim=256 特化（CTA 内的寄存器/共享内存布局按 256 定宽），所以 config 校验层直接把「head_dim ≠ 256」挡在加载期。约定在三个层面落地：

1. **config 门**：`FULL_ATTN_HEAD_DIM = 256` 常量 + `FullAttnHeadDimMismatch` 错误（[src/config/model.rs:60-68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L60-L68) 的注释说得直白："a config that passes here is one the kernels can run"；具体比对在 [src/config/model.rs:242-247](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L242-L247)）。
2. **调用点常量**：prefill 用本地 `const HEAD_DIM: usize = 256`（[src/prefill.rs:21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L21)）传给内核，并据此取 `sm_scale = 1/√256 = 0.0625`（[src/prefill.rs:376](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L376)）——注意分母是**全头维 256**，不是被旋转的 64：softmax 缩放补偿的是 QK 内积的方差，内积在全部 256 维上累加。
3. **decode 路由**：GQA 组比率 \(H_q/H_{kv}\) 决定用哪条 decode 内核路线，见下。

**GQA 路由的完整决策树**（4B 组比率为 4）：

```
decode_group_is_compiled()?            # 组比率 ∈ SUPPORTED_GQA_GROUP_SIZES = [1,2,3,4,6,8]
├─ 否（清单外比率，如 32/4=8 之外的奇数组）
│   → batched hybrid 回退: 全注意力走 via_prefill
│     (paged_attention_batch_decode_via_prefill_hd256_into, 1-token PrefillPagedPlan)
│     且只允许 Serve 模式(不能进 CUDA Graph 的 Capture/Replay)
└─ 是
    ├─ bs ≤ 16 且 组比率 == 4 → split-KV 内核 (paged_attention_batch_decode_split_hd256_into)
    └─ 否则                    → FlashInfer 批量内核 (paged_attention_batch_decode_hd256_into)
```

split-KV 路线的动机写在常量注释里（[src/decode_buffers.rs:12-29](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L12-L29)）：FlashInfer 批量内核每 (请求, KV 头) 只开一个 CTA、grid 不随 KV 长度增长——bs=1 时全卡只有 4 个 CTA 在跑；把 KV 区间切成 `splits` 份并行、两阶段归约，小 batch 全面占优（注释附实测：bs1 9.41→8.57 ms）。`split_decode_splits` 每桶固定（bs≤2 用 32、否则 16，[src/decode_buffers.rs:31-41](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L31-L41)）——切分数必须**不随 KV 长度变**，否则 CUDA Graph 桶的 grid 形状不稳定（注释原话 "has to be constant across a captured CUDA Graph bucket"）。

#### 4.3.2 核心流程

decode 侧全注意力一步的完整路由（对照 [src/batch_decode.rs:126-213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L126-L213)）：

```
输入 normed [2560, bs]（bs 已按桶取整）
1. q_full[8192,bs] / k_attn[1024,bs] / v_attn[1024,bs] ← 三个 gemm_into
2. qk_norm_partial_rope_batched_decode_hd256_into:
      q_attn[4096,bs] ← Q 带(norm+RoPE), k_attn 原地(norm+RoPE), gate 带留在 q_full
3. 路由:
   bs≤16 且 16/4==SPLIT_DECODE_GQA_GROUP(4):
      split 内核: 先 scatter 本步 K/V 入池, 再按 splits 份切 KV,
        partial_o/m/l 归并 → attn_out_full[4096,bs]
   否则: hd256 批量内核(同样先 scatter)
4. attention_gate_batch_hd256_cuda: attn_out_full ⊙ sigmoid(gate) 原地
5. gemm_into(o_proj) → attn_results[2560,bs] → all_reduce_hidden
```

#### 4.3.3 源码精读

**编译清单的查询**。[src/config/model.rs:148-152](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L148-L152)：

```rust
pub(crate) fn decode_group_is_compiled(&self) -> bool {
    // Uncompiled GQA groups use the batched hybrid eager fallback.
    pegainfer_core::ops::SUPPORTED_GQA_GROUP_SIZES
        .contains(&(self.num_attention_heads / self.num_key_value_heads))
}
```

`SUPPORTED_GQA_GROUP_SIZES = &[1, 2, 3, 4, 6, 8]`（定义在 `pegainfer-kernels/src/ops/attention.rs:21`，经 core 重导出）。u2-l2 讲过这是 P2c 门：TP 下头切分不改变组比率，所以这个判定在各 rank 一致，不引入额外集合通信（[src/batch_decode.rs:344-349](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L344-L349) 注释）。graph 路径遇到不编译组时直接改道 hybrid 并只允许 Serve 模式（[src/batch_decode.rs:433-447](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L433-L447)），eager 路径则现算一个 1-token plan（[src/batch_decode.rs:349-360](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L349-L360)）。

**split 条件与调用**。[src/batch_decode.rs:146-171](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L146-L171)：条件是 `bs <= SPLIT_DECODE_MAX_BATCH(16)` **且** `num_attention_heads == num_key_value_heads * SPLIT_DECODE_GQA_GROUP(4)`（注意是本地头数比——单卡 16/4、TP2 下 8/2，比率不变，判定结果也不变）。split 调用比 hd256 调用**多**三块 partial 累加器与 `split_decode_splits(bs)`，**少** `kv_tile_indices_d`（split 内核用 chunk size 切 KV，不用 tile 表）。

**via_prefill 回退**。[src/batch_decode.rs:251-264](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L251-L264)：把每请求 1 行的 Q 塞进 prefill FA2 内核跑，plan 由 `one_token_paged_plan` 现场构建（每请求 `seq_lens = [1]`，[src/batch_decode.rs:597-630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L597-L630)）。代价是每步重建 plan（eager 元数据组装），收益是清单外几何也有正确路径——**正确性优先于峰值性能**的典型取舍。

**sm_scale 只在 prefill 显式出现**：[src/prefill.rs:376](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L376) 的 `1.0f32 / f32::sqrt(HEAD_DIM as f32)`；decode 两个包装的调用点都不传 sm_scale（尺度由内核内部按 head_dim 固定），这也侧面说明 256 是写死在内核里的。

**qk_norm 与 GQA 的参数级表达（decode 侧）**。[src/batch_decode.rs:130-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L130-L144)：

```rust
ops::qk_norm_partial_rope_batched_decode_hd256_into(
    &self.ctx,
    &bufs.q_full,          // 输入: 交错的 [q|gate] 投影
    &mut bufs.q_attn,      // 输出: norm+RoPE 后的 Q（gate 带被丢弃）
    &mut bufs.k_attn,      // 原地: norm+RoPE
    &attn.q_norm,          // [256]，所有 head 共享
    &attn.k_norm,          // [256]，所有 head 共享
    &self.cos_cache, &self.sin_cache,
    &bufs.positions_d,     // 每请求一个位置
    num_attention_heads,   // 16（TP2 下 8）—— 本地头数
    num_key_value_heads,   // 4（TP2 下 2）
    self.config.rotary_dim,// 64
    eps,                   // rms_norm_eps
);
```

norm 不需要 head 数参数（权重共享、逐 head 广播）；GQA 只传两个头数、比率由内核自算；TP 下传的是 `geometry.local_*`（[src/batch_decode.rs:123-124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L123-L124)），本地维度派生见 [src/config/tp.rs:140-165](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/tp.rs#L140-L165)（其中 `local_full_attn_gated_q_dim = local_full_attn_q_dim * 2`，正是 gate 带让 `q_full` 宽度翻倍的出处）。

#### 4.3.4 代码实践

**实践目标**：对 5 个场景判定 decode 走哪条全注意力路线，并写明依据。

**操作步骤**：

1. 先读三个判定源：[src/config/model.rs:148-152](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L148-L152)（编译清单）、[src/batch_decode.rs:146-149](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L146-L149)（split 条件）、[src/batch_decode.rs:433-447](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L433-L447)（不编译组的 graph 限制）。
2. 对下表每行给出路线（split / hd256 / via_prefill-hybrid）与 `splits` 值（若走 split）：

| 场景 | 模型几何（H_q/H_kv） | TP | 桶 bs |
|---|---|---|---|
| A | 4B：16/4 | 1 | 1 |
| B | 4B：16/4 | 1 | 32 |
| C | 4B：16/4 | 2 | 8 |
| D | 某模型：8/4（比率 2） | 1 | 4 |
| E | 某模型：16/5（比率 3.2） | 1 | 4 |

**需要观察的现象**：判定只依赖两个量——组比率的整数值与（split 分支内的）桶大小；TP 改变本地头数但不改变比率，因此不改变路线。

**预期结果**：A → split（bs=1≤16，比率 4，splits=32）；B → hd256（bs=32>16）；C → split（本地 8/2 比率仍 4，splits=16，因 bs=8>2）；D → hd256（比率 2 在编译清单内但不等于 4，split 条件第二项不满足）；E → **根本到不了路由**：16 不被 5 整除，在 config 校验层就被 `AttentionHeadDivisibility` 拒载（[src/config/model.rs:264-271](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs#L264-L271)）。**E 的教训**：三层漏斗——先过 config 整除门，再看编译清单，最后看 split 双条件，缺一不可；清单外**整数**比率（如某模型 40/8=5）才落 via_prefill hybrid。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `split_decode_splits` 按桶固定，而不是按当前 KV 长度动态取最优？
**答案**：CUDA Graph 重放要求网格形状在捕获时定死。decode 的图按桶捕获（u4-l3 详述），同一条重放要在任意 KV 长度下正确；若 splits 随长度变，重放的 grid 与捕获时不同，轻则性能劣化重则读越界。注释给出的策略是「每桶取曲线拐点值，过长请求让后面的 CTA 空转」——空 CTA 是正确性安全的。同时这也解释了 `SPLIT_DECODE_MAX_SPLITS = 32` 的存在：partial 缓冲要按上界一次性定容。

**练习 2**：`qk_norm_partial_rope_batched_decode_hd256_into` 为什么把「Q 从 q_full 抽取」也做进内核，而不是先用一个独立的 gather 内核？
**答案**：因为 `q_full` 的行是 per-head `[q | gate]` 交错块，Q 带与 gate 带不在连续行段里（u2-l4 的 gated-q 布局）。任何「先抽取再 norm+RoPE」的方案都要多一次全局读写 4096 宽的中间缓冲。融成一个内核后，Q 带在读进寄存器/共享内存的同一步完成 norm、RoPE 与重排写出，省一次往返；gate 带则留给第 4 步的 `attention_gate_batch_hd256_cuda` 原地消费。两个内核各自只读一次 `q_full`，是「一次 GEMM 两种用途」的最终落点。

**练习 3**：Qwen3.8-27B 的全注意力是 24 Q / 4 KV 头（比率 6）。它在本讲的决策树上怎么走？`SPLIT_DECODE_GQA_GROUP` 为什么不顺便支持 6？
**答案**：比率 6 在 `SUPPORTED_GQA_GROUP_SIZES` 清单内（FlashInfer 有编译实例），所以不走 hybrid；但 split 条件要求本地比率恰等于 4（`SPLIT_DECODE_GQA_GROUP`，注释说该内核 "one warp each" 按组 4 的 warp 布局手写），因此每个桶都走 hd256 批量内核——大 batch 下这正是注释实测里更优的路线。不为 6 再写一版 split 内核是收益问题：split 的优势区间（bs≤16）在 27B 的负载里占比小，多一版特化内核的维护成本大于收益。这与 4.3.1「正确性优先，性能按实测逐路线开闸」的思路一致。

## 5. 综合实践

**任务：batch=1、seq=128 的一次 decode——从 hidden 到输出的全形状表 + 与 ops.rs 参数逐一对齐（本讲规格指定的实践）。**

设定：Qwen3.5-4B、单 GPU（TP1）、一个请求已 prefill 128 个 token，现在做它的下一步 decode（位置 127）。桶取整为 bs=1。请独立完成下表，再对照源码核对：

| # | 操作 | 函数（ops.rs / ffi） | 输出张量与形状 |
|---|------|----------------------|----------------|
| 0 | 本层输入（已过 input_layernorm） | `rms_norm_batch_offset_into` | `normed [2560, 1]` |
| 1 | Q 投影（含 gate） | `gemm_into` × `q_proj[8192×2560]` | `q_full [8192, 1]` |
| 2 | K / V 投影 | `gemm_into` × `k_proj/v_proj[1024×2560]` | `k_attn`, `v_attn [1024, 1]` |
| 3 | QK-norm + partial RoPE | `qk_norm_partial_rope_batched_decode_hd256_into` | `q_attn [4096, 1]`；`k_attn` 原地 |
| 4 | 本步 K/V 落页 + split-KV 注意力 | `paged_attention_batch_decode_split_hd256_into` | `attn_out_full [4096, 1]`；partial_o `[32,1,16,256]` |
| 5 | gate 调制 | `attention_gate_batch_hd256_cuda` | `attn_out_full` 原地 ⊙ sigmoid(gate) |
| 6 | 输出投影 | `gemm_into` × `o_proj[2560×4096]` | `attn_results [2560, 1]`（TP1 下 all-reduce 为 no-op） |

**对齐要求**（参数逐一对上）：

1. 打开 [src/ops.rs:20-26](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L20-L26)，确认第 3、4 步函数的重导出来源（`pegainfer_kernels::ops`）。
2. 对照 [src/batch_decode.rs:130-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L130-L144)，为第 3 步的 12 个参数各写一行「实参 → 值/形状」：如 `q_full → &bufs.q_full [8192,1]`、`positions_d → [127]`、`num_attention_heads → 16`、`rotary_dim → 64`。
3. 对照 [src/batch_decode.rs:150-171](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L150-L171)，为第 4 步写出：走 split 的两个条件为何成立（bs=1≤16、16==4×4）、`split_decode_splits(1) = 32`、三块 partial 的形状（用 [src/decode_buffers.rs:144-159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L144-L159) 的分配公式核对 `32 × min(1,16) × 16 × 256`）。
4. 补上 KV 元数据行：`page_indices_d = [P0..P7]`、`page_indptr_d = [0, 8]`、`last_page_len_d = [16]`、`kv_chunk_size_d = [128]`、`request_indices_d = [0]`、`positions_d = [127]`（来源 [src/decode_buffers.rs:216-267](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/decode_buffers.rs#L216-L267) 与 [src/batch_decode.rs:319-332](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode.rs#L319-L332)）。

**验收标准**：不看讲义，能回答三个追问——(a) `q_full` 为什么是 8192 宽而 `q_attn` 只有 4096？谁消费了差的那 4096？(b) 第 4 步有没有额外的「写池」动作，藏在哪？(c) 这一步读到的 KV 有多少 token、来自哪些页、第 3 层（`full_idx=3`）的 K 起始元素偏移是多少？（答案：(a) gate 带，第 5 步的 gate 内核；(b) 有，包装函数开头的 `scatter_decode_kv_into_paged`；(c) 128 个 token、P0..P7 共 8 页、`3 × 32768 = 98304`。）

**可选 GPU 验证（待本地验证）**：在有 GPU + Qwen3.5-4B 权重的环境，`PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release -p pegainfer-qwen35 --features qwen35 --test e2e_scheduler` 跑通后，把 `bs=1` 换成多请求并发，观察日志中 decode 桶切换（1→2→4…）时 split 路线是否如决策树预测保持/切换。

## 6. 本讲小结

- **RoPE 缓存**：加载期按 `max_position_embeddings × rotary_dim` 一次建满（4B 每表 32 MiB），half-split 重复布局让内核线性寻址；`frequency_dim = rotary_dim` 是与 checkpoint 的对齐约定；运行期只有 `ensure_rope_cache_covers` 纯校验（prefill 整段预检、decode 逐请求），**没有扩容路径**。
- **分页布局**：池 = 页 → 层 → K/V 块 → (token, head, dim)，三个步长 `kv_block_len/layer_stride/page_stride`（4B：16384/32768/262144 元素，512 KiB/页，32 KiB/token）；请求个体性全部收敛在 `KvView` 页表里，`KvView::new` 断言页表恰好覆盖序列。
- **两条调用链同构**：prefill 三内核（融合 prep 含落页 → FA2 分块注意力 → gate）带 `PrefillPagedPlan` 九件设备元数据；decode 的 prep 不碰池，落页藏在注意力包装开头的 scatter，位置从单标量 `start_pos` 变为每请求 `positions_d`。
- **hd256 是编译期约定**：config 层 `head_dim == 256` 直接拒绝其他值；`sm_scale = 1/√256` 用全头维而非 rotary_dim；qk_norm 以两个 `[256]` 共享指针表达、GQA 以 `H_q/H_kv` 一对整数表达。
- **decode 三路由**：组比率不在 `[1,2,3,4,6,8]` 清单 → via_prefill hybrid（仅 Serve）；否则 bs≤16 且比率恰为 4 → split-KV（splits 按桶固定：≤2 用 32，3..=16 用 16）；否则 FlashInfer 批量内核。TP 不改变组比率，路由判定各 rank 一致。

## 7. 下一步学习建议

- **u3-l2（线性注意力层）**：走向另一半——`RecurrentState` 的 `[H,K,V]` f32 状态、`per_layer_dims` 元素数公式、`LinearStatePointerTables` 指针表。读完你就凑齐了混合层的两套状态系统，可以对比「分页 KV vs 固定递推状态」的显存曲线。
- **u3-l3（FFI 边界与内核封装）**：本讲大量 `device_ptr` guard 与 `unsafe` 调用的模式将在那里系统化——guard 生命周期、流归属、三来源算子面。
- **u4-l1（prefill 分块主链路）**：本讲的 `PREFILL_CHUNK_LEN` 分块与 chunk 视图前缀拼接，将在整条 `prefill_last_hidden` 链路上走一遍，包括 GDR scratch 的线性伸缩。
- 若想先看这些内核**在图里怎么被捕获**，可跳读 u4-l3（CUDA Graph 与批次桶）再回头；`sync_paged_views` 的 padding 页伏笔在那里兑现。
