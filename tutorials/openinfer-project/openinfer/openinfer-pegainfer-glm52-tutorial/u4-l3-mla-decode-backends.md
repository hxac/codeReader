# u4-l3 MLA 解码后半：absorb、稀疏后端与 o_proj

## 1. 本讲目标

学完本讲，你应该能够：

1. **解释吸收式解码（weight absorption）**：为什么 GLM5.2 解码时可以用「低秩 latent 注意力」替代逐头 KV——把 \(W_{UK}\) 折进查询、把 \(W_{UV}\) 留在输出侧，注意力核直接在 576 维 latent 空间里算。
2. **复述双后端的选择条件与回退关系**：`heads == 16` 且 cubin 可用走 FlashInfer fp8（576 B/token 静态 e4m3 cache），否则一律回退 FlashMLA fp8-ds（656 B/token 逐组 scale cache）。
3. **读懂 `Glm52MlaSchedMetadata`**：为什么 tile-scheduler 计划可以构建期算一次、为什么 `num_blocks` 是后绑定的占位值、`folds_kv_pack` 如何把 front 的 norm/split 折进 attend 侧。
4. **讲出 UE8M0 约束的教训来源**：FlashMLA sm100 kernel 把 f32 scale 截断成 2 的幂，GB300 上一次「输出整体缩小到 0.67×」的线上精度事故是怎么被定位、又怎么用「写入方向上取整到 2 的幂」修掉的。

本讲是 u4-l1（MLA 前半：q_a/kv_a/q_b 投影）的直接续篇：前半场把 `hidden[T, 6144]` 变成 `q_full[T, heads, 256]` 与 `ckv[T, 576]`，本讲的 `glm52_mla_attend_into` 接过这两样东西，输出注意力结果 `o[T, 6144]`。

## 2. 前置知识

- **低秩分解与结合律**：矩阵乘法满足结合律 \((AB)c = A(Bc)\)。MLA 的 K/V 不是直接算出来的，而是「压缩向量 × 上投影矩阵」：\(k_{nope,h} = W_{UK,h}\,c_{kv}\)。于是点积可以先在查询侧「吸收」权重：\(q^\top k = (W_{UK,h}^\top q)^\top c_{kv}\)。这一步换掉了「为每个缓存 token 展开逐头 K/V」的需求。
- **e4m3 与逐组 scale（回顾 u3-l1）**：fp8 e4m3 只有 8 位；GLM5.2 的投影权重按 \(128\times128\) 块携带 f32 逆 scale。KV cache 里 latent 512 维按 4 组 × 128 量化，每组一个 f32 scale。
- **UE8M0**：一种只有指数位、尾数位全零的 8 位浮点格式，即「2 的幂」。\(2^e\) 形式的数被 e8m0 截断（round-toward-zero）是无损的；非 2 的幂会被截到不大于它的 2 的幂——这正是本讲 4.3 节事故的数学根源。
- **KV 页布局（u4-l2）**：KV 活在 rank 级页池，一页 64 token；MLA 每层每 token 占一个 656 B 切片（fp8_ds_mla 格式），`kv_layer_offset_bytes` 是某层切片在页内的字节偏移，`kv_block_stride_bytes`（= 页距 3,503,040 B）是相邻页的距离。本讲的 attend launch 就靠这两个数在页池里寻址。
- **decode bucket（u1-l3/u6 预告）**：整步前向按离散行数（`[1,2,4,8,16,32,48,64,96]`）分别捕获 CUDA Graph，每个 bucket 拥有自己的 sched/scratch/graph。本讲的 sched metadata 是 per-bucket 的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/mla_decode.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs) | 本讲主角：后端枚举与选择、attend scratch、sched metadata、`glm52_mla_attend_into` 整条链 |
| [src/mla_front.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs) | u4-l1 的前半场；本讲消费它的 `q_full`/`kv_c`/`k_pe`/`ckv`，并引用 `dequant_kv_b`（吸收因子的出生地）与 `glm52_mla_front_rest_into` 的 `fold_kv_pack` 分支 |
| [src/layer.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs) | 调用方：单层注意力半场的编排（front_q → indexer → front_rest → attend） |
| [src/model/mod.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs) | `build_fixed` 里选后端、建 per-bucket 契约；`finish_kv` 重绑 `num_blocks` |
| [../pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs) | FlashMLA 契约结构体、形状校验、长度推导、metadata launch 的 Rust 包装 |
| [../pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs) | FlashInfer 后端的契约、支持性探测、贪心分批 launch |
| [../pegainfer-kernels/src/ops/glm52/moe_quant.rs](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs) | UE8M0 逐组量化 kernel 的 Rust 包装与契约注释 |
| [../docs/lessons/flashmla-sm100-ue8m0-kv-scales.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/lessons/flashmla-sm100-ue8m0-kv-scales.md) | UE8M0 事故的完整复盘（症状、定位路径、规则） |

## 4. 核心概念与源码讲解

### 4.1 吸收因子与 QUERY_DIM=576

#### 4.1.1 概念说明

先回忆 u4-l1 的前半场产出：每 token 每层得到

- `q_full[T, heads, 256]`：每头查询，前 192 维是 nope 部分（要和 \(W_{UK}\) 作用），后 64 维是 rope 部分；
- `kv_c[T, 512]`：latent 压缩 KV（已做 kv_a RMSNorm），**与头数无关**；
- `k_pe[T, 64]`：rope 键分量。

标准多头注意力里，每个缓存 token 每层要存「64 头 × (K 192 + V 256)」的展开量。MLA 的洞见是：K/V 展开是线性的，注意力分数和输出可以改写到 latent 空间——

\[
\text{score}(q_h, k_h) = q_{nope,h}^\top \underbrace{(W_{UK,h}\,c_{kv})}_{k_{nope,h}} + q_{rope,h}^\top k_{pe}
= \underbrace{(W_{UK,h}^\top q_{nope,h})^\top}_{\text{吸收进查询}} c_{kv} + q_{rope,h}^\top k_{pe}
\]

于是注意力核的「查询」变成 576 维：\(512\)（latent，吸收了 \(W_{UK}\)）+ \(64\)（rope）。同理 value 侧：

\[
o_h = \sum_i \alpha_i\, \underbrace{(W_{UV,h}\,c_i)}_{v_{i,h}} = W_{UV,h}\underbrace{\Big(\sum_i \alpha_i\,c_i\Big)}_{\text{latent 输出 } u_h}
\]

注意力核先输出 latent 结果 \(u_h \in \mathbb{R}^{512}\)，再由一次小 GEMM 乘 \(W_{UV,h}\) 还原成 256 维的 \(v_h\)。**代价**是查询侧两个 batched GEMM；**收益**是 cache 里每 token 只存 576 个 latent 元素而非 64×448 个展开元素。

这就是 `mla_decode.rs` 顶部注释说的 "kv_b is pre-dequantized into the bf16 absorb factors W_UK / W_UV at construction"：checkpoint 的 `kv_b_proj` 是 fp8 的 \([\,heads\times448,\,512\,]\)（每头 192 行给 UK、256 行给 UV），加载期在 host 一次性反量化成 bf16 的 `w_uk [heads,192,512]` 与 `w_uv [heads,256,512]`，运行期不再有 kv_b 的 fp8 形式。

#### 4.1.2 核心流程

attend 半场对一层一批 T 行的执行序列（FlashMLA 后端视角）：

```text
q_full[T,heads,256], kv_c[T,512], k_pe[T,64]
        │
        ▼ ① 吸收 GEMM（batched over heads）
ql_nope[T,heads,512] = q_full 的 nope 192 维 @ W_UK[heads,192,512]
        │
        ▼ ② query assemble：拼 ql_nope + rope(q_rope) → query[T,64,576]
        ▼ ③ kv_c 逐组 UE8M0 量化 → ckv_fp8[T,512] + scales[T,4]
        ▼ ④ cache pack：ckv_fp8|scales|rope(k_pe) 写进 slot_mapping[row] 的页槽
        ▼ ⑤ FlashMLA 稀疏解码：按 topk 全局槽位读 cache → latent[T,64,512]
        │
        ▼ ⑥ 还原 GEMM：v[T,heads,256] = latent @ W_UV[heads,256,512]
        ▼ ⑦ o_proj：v[T, heads*256] @ o_proj → o[T,6144]
```

缓存收益的量化（每 token 每层）：

- 逐头 MHA（bf16）：\(64 \times (192+64+256) \times 2\,\text{B} = 65{,}536\,\text{B}\)；
- MLA fp8_ds cache：\(656\,\text{B}\)（构成见 4.3 节）——约 **100×** 压缩；
- 即便按 bf16 存 latent（\(576\times2=1152\,\text{B}\)）也有约 57×。

这就是 GLM5.2 能在单卡 96 行并发下把长上下文 KV 塞进显存的根本原因。

#### 4.1.3 源码精读

**吸收因子的出生地**——[src/mla_front.rs:251-298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L251-L298)：`dequant_kv_b` 在 host 上按 128×128 块 scale 反量化 kv_b 的每个元素，行号 `r < 192` 进 `w_uk`，否则进 `w_uv`，两个 bf16 向量各上传一次设备。关键片段：

```rust
let s = scale[(row / FP8_BLOCK) * scale_cols + j / FP8_BLOCK];
let val = bf16::from_f32(e4m3_to_f32(kv_b.weight[row * KV_LORA + j]) * s);
if r < QK_NOPE {
    w_uk[(h * QK_NOPE + r) * KV_LORA + j] = val;   // 192 行 → UK
} else {
    w_uv[(h * V_HEAD + (r - QK_NOPE)) * KV_LORA + j] = val; // 256 行 → UV
}
```

**生产加载路径**（不走 `from_host` 而走 `from_device`）——[src/mla_front.rs:170-181](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L170-L181)：已驻留的 kv_b fp8 字节先 `clone_dtoh` 拉回 host 做块 scale 反量化，随后原始 fp8 字节即被丢弃（注释明确 "it is not stored"）。启动期一次性成本，换来运行期 bf16 吸收 GEMM 不用查 scale。

**局部常量与 QUERY_DIM**——[src/mla_decode.rs:58-65](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L58-L65)：模块把 config 常量起短名，最后一行就是本模块标题里的那个数：

```rust
const QUERY_DIM: usize = KV_LORA + ROPE_DIM; // 576
```

注意一个命名陷阱：**kernel 侧的同名常量语义不同**。[flashmla_sparse.rs:14-15](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L14-L15) 里 `QK_HEAD_DIM = 576`、`V_HEAD_DIM = 512`——都在 **absorbed/latent 空间**；而 config 侧 `GLM52_QK_HEAD_DIM = 256`、`GLM52_V_HEAD_DIM = 256` 在**模型空间**。读源码时先问自己「这个维度在哪个空间」。

**吸收 GEMM**——[src/mla_decode.rs:388-411](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L388-L411)：`gemm_strided_batched_bf16` 以 `heads` 为批次数，每批算 \(\text{ql\_nope}[t,h] = W_{UK,h}^{512\times192}\) 乘 `q_full` 的 nope 列块。注释里讲清了 cuBLAS 的转置技巧：T 行 ride 在 GEMM 的 n 维上，列 t 读 `q_full[t, h, 0..192]`（ldb = 紧凑的 `[T,heads,256]` token 步长）。

**还原 GEMM 与 o_proj**——[src/mla_decode.rs:537-569](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L537-L569)：第二个 batched GEMM 把 latent（FlashMLA 时全宽 `[T,64,512]`，token 步长 `HEADS*KV_LORA`）乘 \(W_{UV,h}\) 得 `v[T,heads,256]`；随后 o_proj 走 u3-l1 的 fp8 投影：行数 ≤ `FP8_GEMV_MAX_ROWS`(=8) 用 weight-only GEMV，超过则切换 `fp8_linear_large_m_into` 的 wide GEMM 路径（宽 bucket，#812 引入）：

```rust
if let Some(wide) = s.wide.as_mut() {
    return crate::fp8::fp8_linear_large_m_into(ctx, &w.o_proj, t, &s.v, wide, out.data_mut());
}
fp8_linear_into(ctx, &w.o_proj, t, &s.v, Some(&mut s.gemv_partial), out.data_mut())
```

#### 4.1.4 代码实践（源码阅读 + 手算）

**实践目标**：亲手验证两个 cache 格式的字节构成，确认吸收换来的压缩比。

**操作步骤**：

1. 打开 [flashmla_sparse.rs:16-18](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L16-L18) 与 [flashinfer_sparse.rs:15-16](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L15-L16)，抄下两个 `BYTES_PER_TOKEN`。
2. 纸笔推导 656 B：512 个 e4m3 latent 字节 + \(512/128 = 4\) 组 × 4 B f32 scale + 64 个 rope 元素 × 2 B（bf16）。
3. 推导 576 B：512 个 e4m3 latent 字节 + 64 个 rope 元素 × 1 B（e4m3，静态 scale，无逐组 f32——对应 bringup 文档的 "576-byte standard E4M3"）。
4. 对照 [mla_decode.rs:166-167](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L166-L167) 的 scratch 尺寸（`ckv_fp8: t * KV_LORA` 个 u8、`ckv_scales: t * (KV_LORA / FP8_BLOCK)` 个 f32）核对你的第 2 步。
5. 计算压缩比：逐头 MHA \(64\times(192+64+256)\times2 = 65{,}536\) B 对 656 B。

**需要观察的现象**：`ckv_scales` 的长度恰是每 token 4 个 f32——这 16 B 正是 656 与 640（512+128×bf16 rope 的朴素差）之间的差值来源，也是 4.3 节 UE8M0 事故的主角。

**预期结果**：两个格式字节账都能闭合（656 / 576），压缩比 ≈ 99.9×。本实践纯纸笔 + 源码，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `w_uk`/`w_uv` 用 bf16 而不是留在 fp8、在 kernel 里现查 scale？

**答案**：吸收 GEMM 是每步每层都跑的常规 batched GEMM（cuBLAS bf16）；若权重留 fp8 就需要块 scale 感知的自定义 GEMM。反量化在加载期一次完成（[mla_front.rs:255-258](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L255-L258)），把每步的解scale 成本变成零；且 kv_b 只在注意力里用这一种方式消费，驻留 bf16 即可。

**练习 2**：FlashMLA 输出的 `latent` 是 `[T, 64, 512]` 全宽，哪怕权重只带 16 头的 TP shard。为什么？

**答案**：FlashMLA kernel 按 64 头编译（[flashmla_sparse.rs:13](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L13) `SPARSE_HEADS=64`），scratch 也按全宽分配（[mla_decode.rs:159](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L159)）；后续 W_UV 的 batched GEMM 只取本 shard 的头槽位（[mla_decode.rs:538-540](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L538-L540) 注释 "the batch count reads only the shard's head slots"）。

**练习 3**：把 `QUERY_DIM` 里的两个加数说清楚——各来自哪条链、谁做了 norm？

**答案**：512 = `kv_c`（kv_a 输出的压缩 KV，**经过** kv_a RMSNorm）；64 = rope 维（k_pe，**不做** norm，u4-l1 讲过的分工）。查询侧的 512 是 \(W_{UK,h}^\top q_{nope,h}\)（q 侧 norm 发生在更早的 q_a RMSNorm），rope 64 直接拼在后面。

### 4.2 双后端与 sched metadata

#### 4.2.1 概念说明

「稀疏 MLA 解码」这一步有两套可用的 kernel 栈，对应两种 cache 线格式：

| | FlashMLA fp8-ds | FlashInfer fp8 |
| --- | --- | --- |
| 枚举值 | `FlashMlaFp8Ds` | `FlashInferFp8` |
| 头数要求 | 1..=64（kernel 按 64 头编译） | **恰好 16**（cubin 只编译了 16 头） |
| cache 格式 | 656 B/token，逐 128 组 f32 scale（**必须 UE8M0**） | 576 B/token，静态 e4m3 无逐组 scale |
| 缓存寻址 | 支持页池 strided 布局（层偏移 + 页距） | 只认 dense cache（层偏移必须为 0） |
| 硬件依赖 | `sm_100f` 组装（Blackwell） | 7 个 SM100 家族 cubin（selector 闭包） |

选择逻辑极简——**16 头且 cubin 可用才走 FlashInfer，否则一律 FlashMLA 兜底**。16 头的场景恰是 TP4 拓扑：64 头被 4 rank 切成每 rank 16 头（attention-TP shard）；EP 拓扑每 rank 持全 64 头，永远走 FlashMLA。这就是「双后端」的分工：FlashInfer 是 TP4 带来的**专用加速路径**，FlashMLA 是**通用回退**。

`sched metadata` 要解决的问题：FlashMLA 是 split-KV 风格 kernel，每次 launch 前需要一份 tile-scheduler 计划（哪些 SM 处理哪些 tile、每个 batch 切成几份）。这份计划**只依赖 `batch_size`、`topk`、`num_sm_parts` 三个数**——不依赖位置、序列长度、层号。所以它在构建期对每个 decode bucket 算一次（78 层共享），而不是每层每步现算：注释给出的量级是 bs=1 时省掉 78 × ~25 µs/步。

#### 4.2.2 核心流程

```text
build_fixed (model/mod.rs)
  ├─ mla_heads = 全 64（EP）或 64/设备数（TP shard）
  ├─ mla_backend = glm52_select_mla_backend(mla_heads)
  ├─ 契约模板：num_blocks=1 占位（池容量此刻还没定，u2-l4）
  └─ 对 GLM52_DECODE_BUCKETS 每个 rows：
       Glm52MlaSchedMetadata::new_for_backend(契约{batch_size=rows}, heads, backend)
         ├─ FlashMLA: alloc tile_scheduler_metadata / num_splits
         │            └─ metadata_launch（构建期一次，之后整步图重放）
         └─ FlashInfer: 无计划需要
finish_kv (池容量已实测)
  └─ sched.set_num_blocks(pool_blocks)   ← 只重绑缓存几何，计划仍然有效
每层每步
  └─ attend 用 (sched, kv_layer_offset=本层切片偏移) 组装最终 launch 契约
```

「计划/契约不可能失配」是这里的类型设计要点：计划与契约住在同一个 `Glm52MlaSchedMetadata` 对象里，每个消费者都从同一个对象读两者。

#### 4.2.3 源码精读

**后端枚举与选择**——[src/mla_decode.rs:67-88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L67-L88)：`cache_bytes_per_token` 把枚举映射到线格式常量；选择函数一行条件：

```rust
pub(crate) fn glm52_select_mla_backend(heads: usize) -> Result<Glm52MlaBackend> {
    if heads == 16 && glm52_flashinfer_sparse_mla_supported(heads)? {
        Ok(Glm52MlaBackend::FlashInferFp8)
    } else {
        Ok(Glm52MlaBackend::FlashMlaFp8Ds)
    }
}
```

`glm52_flashinfer_sparse_mla_supported` 是一次 FFI 探测（[flashinfer_sparse.rs:80-87](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L80-L87)），cubin 不在就安静回退。调用点有两处：主模型 [model/mod.rs:680-687](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L680-L687)（`mla_heads` 由 `attn_shard` 决定）与 MTP 通道 [model/mtp.rs:388](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mtp.rs#L388)，同一规则。

**契约结构体**——[flashmla_sparse.rs:93-106](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L93-L106)：`Glm52FlashMlaSparseDecode` 是纯数据契约，7 个字段里 `kv_layer_offset_bytes`（层切片在页内偏移）与 `kv_block_stride_bytes`（页距，TMA tensormap 的每页步长，必须保持 token 行粒度——[validate L120-127](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L120-L127)）就是 u4-l2 页几何在这层的投影。

**sched metadata**——[src/mla_decode.rs:255-341](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L255-L341)：

- 构造（L284-312）：FlashMLA 分支分配 `tile_scheduler_metadata`（`num_sm_parts × 8` 个 i32，长度推导见 [flashmla_sparse.rs:170-196](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L170-L196)）与 `num_splits`（`batch+1`），并立即跑一次 metadata launch；FlashInfer 分支什么都不带。
- `set_num_blocks`（L314-320）：池容量实测后重绑——注释解释了为什么计划不用重算（计划不依赖缓存几何）。
- 三个访问器 `topk()`（L322-327）/`batch()`（L329-333）/`folds_kv_pack()`（L335-341）：`topk()` 让「indexer 产生的 top-k 与 attend 期待的 k 不一致」**无法表示**；`batch()` 是一步批形状的唯一事实源；`folds_kv_pack()` 告诉 front 半场要不要跳过 split/norm（见 4.3）。

**bucket 化构建**——[src/model/mod.rs:821-855](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L821-L855)：每个 bucket 行数各建一份 sched + scratch；`finish_kv` 在 [src/model/mod.rs:996-999](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L996-L999) 统一重绑 `pool_blocks`。启动日志（[src/model/mod.rs:700-705](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L700-L705)）会打出 `backend=... heads/rank ... bytes/cache token`——这是验证后端选择最直接的观测点。

**FlashInfer 的贪心分批**——[flashinfer_sparse.rs:27-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L27-L31) 与 [L135-169](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L135-L169)：cubin 只实例化了 batch ∈ {8,4,2,1}，大 batch 被贪心拆成连续小 launch，共享 16 MiB workspace（多 CTA 计数器自复位，同流串行安全）。

#### 4.2.4 代码实践

**实践目标**：不启动模型，推导四种部署形态的后端决策表，并找到日志验证点。

**操作步骤**：

1. 读 [model/mod.rs:680-687](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L680-L687)：`attn_shard.is_some()` 时 `mla_heads = 64 / device_count`，否则 64。
2. 对照 [mla_decode.rs:82-88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L82-L88) 与 [flashinfer_sparse.rs:34-59](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L34-L59) 的 `validate`（heads 必须 == 16），填写下表：

   | 形态 | heads/rank | 后端 | cache B/token |
   | --- | --- | --- | --- |
   | EP8（H200） | 64 | ？ | ？ |
   | EP4（GB300） | ？ | ？ | ？ |
   | TP4（GB300） | ？ | ？ | ？ |
   | MTP 通道（EP 主拓扑） | ？ | ？ | ？ |

3. 再读 [model/mod.rs:264-275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L264-L275) 的 `glm52_persistent_mla_bytes_per_token`：注意 `prefill_only` 分支恒返回 656（P/D 的线/存储格式与本地执行后端无关），解释这与表格的关系。
4. （可选，需 GPU + 权重，**待本地验证**）：以 TP4 启动并观察日志行 `GLM5.2 MLA execution: backend=...`；再以 `--moe-topo=ep8` 启动对比。

**需要观察的现象**：第 3 步会发现「执行后端」与「线格式」是两个概念——TP4 prefill-only 的持久 KV 恒为 656 B 线格式，即便本地 decode 用 576 B 的 FlashInfer cache（两者在 P/D 交接时由专门的转换衔接，见 u8/u9）。

**预期结果**：表格依次为 (64, FlashMlaFp8Ds, 656)、(64, FlashMlaFp8Ds, 656)、(16, FlashInferFp8 或回退 FlashMlaFp8Ds, 576/656)、(64, FlashMlaFp8Ds, 656)。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `set_num_blocks` 只改一个字段就够，而不用重建 tile 计划？

**答案**：计划只依赖 `batch_size/topk/num_sm_parts`（[mla_decode.rs:314-320](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L314-L320) 注释）；`num_blocks` 只是被 attend launch 当作缓存几何读。这也是 build_fixed 敢用 `num_blocks=1` 占位的原因（[model/mod.rs:711-726](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L711-L726)）——nothing in a bucket's allocations scales with it。

**练习 2**：`num_sm_parts` 在什么情况下取 1？

**答案**：attention-TP shard 存在时（[model/mod.rs:706-710](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L706-L710)）；否则查询设备的 SM 数。shard 时 split-KV 没意义（每 rank 只算自己 16 头的一份）。

**练习 3**：FlashInfer 后端为什么 `ensure!(kv_layer_offset_bytes == 0)`？

**答案**：见 [mla_decode.rs:487-495](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L487-L495)——它的 pack/attend launch 从 cache 基址 dense 寻址，不认 strided 页池布局；所以 FlashInfer 的 cache 永远是独享 dense arena（TP4 的 MTP draft cache 亦然）。

### 4.3 cache-pack / query-assemble 编排（含 UE8M0 教训）

#### 4.3.1 概念说明

`glm52_mla_attend_into` 的中段是两条互斥编排，把「查询拼装」「新 token 写入 cache」「稀疏注意力」串起来：

- **FlashMLA 分支（三段式）**：① query assemble 独立 kernel 把 `ql_nope` 与 rope 后的 `q_rope` 拼成 `[T,64,576]`；② kv_c 先做 **UE8M0 逐组量化**（不是普通 amax/448 量化！）；③ cache pack kernel 把 `fp8 latent + 16 B scale + bf16 rope k_pe` 写进 `slot_mapping[row]` 指定的页槽，然后 FlashMLA 按 DSA top-k 的**全局槽位**读回。
- **FlashInfer 分支（融合式）**：一个 `glm52_mla_front_pack_fp8_launch` 同时完成 query assemble + kv_a RMSNorm + cache pack 三件事——为此 front 半场必须**跳过** ckv split 与 norm（`folds_kv_pack()`），把 raw `ckv` 留给 attend 侧消费。

两个贯穿性设计：

1. **`slot_mapping` 是设备端数据**，写槽地址在 kernel 里读——所以整条链可以在 CUDA graph 里重放（u6-l3 的前提），注释称之为 graph 安全的输入重写。
2. **scale 契约按 cache 格式分流**：656 B cache 的逐组 f32 scale 必须是 2 的幂（UE8M0），而 MoE/dense GEMM 的激活量化继续用 amax/448——**这个契约只针对 FlashMLA KV cache**。

**UE8M0 教训的来源**（2026-07，GLM5.2 EP4 GB300 bring-up）：

- 症状：注意力输出在每个位置都 ≈ HF oracle 的 **0.67×**（相关系数 0.996——近乎纯标量缩小），MoE 门 23/64 探针失败，但故障其实与 MoE 无关。
- 根因：FlashMLA V3.2 sm100 kernel 用 `__nv_cvt_float2_to_e8m0x2(..., cudaRoundZero)` 把 f32 scale 截断成 e8m0 参与 tcgen05 块 scale MMA——非 2 的幂被静默读到最多小 2×。
- 为什么长期没暴露：EP8 跑 H200（sm90 kernel 读 f32 精确）；TP4 跑 GB300 但用 FlashInfer（576 B 格式根本没有逐组 scale）。**只有「64 头 FlashMLA + Blackwell」的组合才踩雷**——恰好是双后端矩阵里的空白格。
- 修复：写入方（`glm52_fp8_per_token_group_quant_bf16_ue8m0_launch`）把组 scale **向上**取整到下一个 2 的幂（位技巧 `(bits + 0x007FFFFF) & 0x7F800000`，无 `log2f` 舍入隐患）——向上取整后截断无损。数学上：

\[
\text{scale}_{\text{stored}} = 2^{\lceil \log_2(\text{amax}/448) \rceil} \quad\Rightarrow\quad \text{trunc}_{e8m0}(\text{scale}_{\text{stored}}) = \text{scale}_{\text{stored}}
\]

#### 4.3.2 核心流程

```text
glm52_mla_attend_into 中段（伪代码）
match (sched.backend, scratch.backend):
  (FlashMla, Fp8Ds):
      query_assemble(ql_nope, q_full, cos, sin)         → query[T,64,576]
      ue8m0_quant(kv_c)                                  → ckv_fp8 + pow2 scales
      cache_pack(ckv_fp8, scales, rope(k_pe), cos, sin)  → cache[slot_mapping[row]]
      flashmla_decode(query, cache, topk, tile_plan)     → latent[T,64,512]
      latent_stride = HEADS * 512          # 全宽
  (FlashInfer, FlashInfer):
      ensure(layer_offset == 0)                           # dense cache
      front_pack_fp8(ql_nope, q_full, RAW ckv, kv_a_ln)   # 融合 assemble+norm+pack
      flashinfer_decode(query, cache, topk, seq_lens)     → latent[T,16,512]
      latent_stride = heads * 512           # shard 宽
back: v = latent @ W_UV ; o = o_proj(v)
```

FlashInfer 侧一个容易忽略的细节：它还需要 `seq_lens`（每行序列长度），FlashMLA 分支则完全由 `topk` 列表（-1 填充）驱动。

#### 4.3.3 源码精读

**整体编排**——[src/mla_decode.rs:413-535](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L413-L535)：一个 `match` 同时配对「计划里的后端」与「scratch 里的后端」，任何错配直接 `bail!`——schedule/scratch 在类型上无法交叉验证，就在运行时用穷举 match 兜住。

**FlashMLA 三段**——

- query assemble：[src/mla_decode.rs:418-429](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L418-L429)，读 `ql_nope` 与 `q_full`（rope 列块），用 `cos/sin` 施加 RoPE 后拼 576 维。
- UE8M0 量化：[src/mla_decode.rs:431-444](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L431-L444)。行内注释就是教训的浓缩版（"amax/448 scales silently lose up to 1 bit of V/K magnitude per group on GB300"），量化形状是 `rows=t, width=512, group=128`。
- cache pack + 解码 launch：[src/mla_decode.rs:445-484](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L445-L484)，pack 携带层偏移/页距/页数/slot_mapping；decode 消费构建期的 `tile_scheduler_metadata` 与 `num_splits`，split-KV 部分和落在 `o_accum`/`lse_accum`（长度推导 `batch + num_sm_parts` 份，见 [flashmla_sparse.rs:186-196](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashmla_sparse.rs#L186-L196)）。

**UE8M0 kernel 契约**——[../pegainfer-kernels/src/ops/glm52/moe_quant.rs:72-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs#L72-L78)：doc 注释写死了使用边界——"Use this for every write into the 656-byte fp8_ds_mla cache and **nowhere else**"。

**事故复盘文档**——[../docs/lessons/flashmla-sm100-ue8m0-kv-scales.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/lessons/flashmla-sm100-ue8m0-kv-scales.md)：完整记录了「层门 → 逐行 α 拟合 → MLA-only 门（position 0 时 softmax≡1，锁定 V 路径）→ 无权重 kernel 门（合成数据上单键偏差 34%）」的定位路径，以及四条规则（含「Kimi-K2 用同一 kernel，迁 Blackwell 前要先审计」）。

**FlashInfer 融合段**——[src/mla_decode.rs:487-533](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L487-L533)：dense 断言之后，`glm52_mla_front_pack_fp8_launch` 直接吃 **raw** `front.ckv`（未 split、未 norm）与 `w.kv_a_ln.data`（这是 `kv_a_ln` 字段 pub 的原因），把 norm 折进 pack；`glm52_flashinfer_sparse_mla_fp8_launch` 以 `[T,heads,576]` u8 query + dense cache + `seq_lens` 出 latent。

**front 侧的配合**——[src/mla_front.rs:544-610](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L544-L610)：`glm52_mla_front_rest_into` 尾部的 `if fold_kv_pack { return Ok(()); }`（L597-599）跳过 `ckv_split` + `rms_norm` 两个 launch——参数由调用方 [src/layer.rs:257-263](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L257-L263) 传入 `step.mla_sched.folds_kv_pack()`。

**调用现场**——[src/layer.rs:274-288](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L274-L288)：layer 把 `caches.mla_offset`（本层切片在页内的偏移）作为 `kv_layer_offset` 传给 attend——一份 sched 服务 78 层，靠每次调用注入层偏移。

#### 4.3.4 代码实践

**实践目标**：清点两种后端一个 MoE+full-indexer 层的 attend kernel 链，理解 FlashInfer 融合省掉了什么、UE8M0 换掉了什么。

**操作步骤**：

1. 从 [mla_decode.rs:388-569](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L388-L569) 逐行列出每个 launch 调用，标出它属于哪个阶段。参考答案格式：

   | # | FlashMLA 分支 | FlashInfer 分支 |
   | --- | --- | --- |
   | 1 | absorb GEMM（W_UK） | absorb GEMM（W_UK） |
   | 2 | query_assemble | front_pack_fp8（融合 assemble+norm+pack） |
   | 3 | UE8M0 quant | —（静态 e4m3，pack 内自带） |
   | 4 | cache_pack | —（并入 #2） |
   | 5 | flashmla_decode | flashinfer_decode（可拆多个 8/4/2/1 launch） |
   | 6 | 还原 GEMM（W_UV） | 还原 GEMM（W_UV） |
   | 7 | o_proj（GEMV 或 wide GEMM） | o_proj（GEMV 或 wide GEMM） |

2. 再算上 front 侧的差别：FlashInfer 时 `glm52_mla_front_rest_into` 少了 `ckv_split` 与 `rms_norm` 两个 launch（[mla_front.rs:597-609](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_front.rs#L597-L609)）。
3. 假设一个回归把 #3 换回普通 `amax/448` 量化（即 [moe_quant.rs:72-78](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/moe_quant.rs#L72-L78) 注释禁止的那种用法），写出故障形态。
4. （需 GPU + GLM-5.2-FP8 权重，**待本地验证**）运行精度门：`cargo test --release -p pegainfer-glm52 --features glm52 --lib -- --ignored mla_oracle_gate`（定义在 [src/oracle/mla.rs:221-225](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mla.rs#L221-L225)，RMS 容差探针见 [L296-L309](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/oracle/mla.rs#L296-L309)）。

**需要观察的现象**：第 3 步应得出「输出整体缩小（最多 2×/组的 K/V 幅度损失），相关系数仍高、逐元素比例 ≈ 0.67 量级」——与教训文档的症状一一对应；这正是 oracle 探针用 RMS 缩放容差（而非逐元素相对误差）能抓住的故障类型。

**预期结果**：FlashMLA 分支 7 个 launch/层，FlashInfer 分支 5 个（#5 可能展开为多个子 launch）；front 侧 FlashInfer 再省 2 个。78 层累积后，这是 TP4 每步数百个 graph 节点的差距来源之一。

#### 4.3.5 小练习与答案

**练习 1**：为什么 UE8M0 要**向上**取整而不是四舍五入或向下取整？

**答案**：kernel 读侧是 round-toward-zero 截断。向上取整到 2 的幂后，截断是无损的（\(2^e\) 的 e8m0 表示精确）；且向上取整只会让量化步长略大（最多损失 1 bit 分辨率），不会让数值读小。向下取整/四舍五入在非 2 的幂处仍会再被截断，等于没修。

**练习 2**：`folds_kv_pack` 的信息为什么从 sched（attend 侧拥有）读，而不是 front 自己探测后端？

**答案**：fold 的决策方是后端（FlashInfer 的融合 pack 需要 raw ckv），而后端选择唯一来源是 `glm52_select_mla_backend` 经 sched/scratch 持有。让 front 从 `sched.folds_kv_pack()` 读（[layer.rs:257-263](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L257-L263)），两边就不可能各说各话——与 `topk()`/`batch()` 同一设计哲学：让失配 unrepresentable。

**练习 3**：FlashInfer 分支为什么不需要 tile-scheduler 计划？

**答案**：它是 TRTLLM-generation 的 persistent kernel 风格（固定 16 MiB workspace、多 CTA 计数器自复位，见 [flashinfer_sparse.rs:27-31](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-kernels/src/ops/glm52/flashinfer_sparse.rs#L27-L31) 注释），工作划分在 kernel 内部完成；FlashMLA 则由 host 侧 `glm52_flashmla_sparse_decode_metadata_launch` 预计算 split 计划（[mla_decode.rs:296-303](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L296-L303)）。

## 5. 综合实践

**任务：为一次新部署写出「MLA 解码决策与契约审计单」。**

设想你要把 GLM5.2 部署到一台 4×GB300 机器，备选 `--moe-topo=ep4` 与 `tp4`，且后续可能开 native MTP。请产出一页审计单，包含：

1. **后端决策表**：两种拓扑（及 MTP 通道）各自的后端、heads/rank、cache B/token、`folds_kv_pack` 取值——依据 [mla_decode.rs:67-88](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L67-L88)、[model/mod.rs:264-275](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L264-L275)。
2. **kernel 链清单**：两种后端各一层 attention 半场的 launch 序列（含 front 侧差异），标注哪些 launch 的输入是设备端数据（graph 安全）、哪些契约字段来自 sched。
3. **UE8M0 检查项**：把 [flashmla-sm100-ue8m0-kv-scales.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/lessons/flashmla-sm100-ue8m0-kv-scales.md) 的规则逐条映射到本讲源码位置——规则 1（656 B cache 写入必须 pow2 向上取整）→ [mla_decode.rs:431-444](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L431-L444)；规则 2（GEMM 激活量化保持 amax/448）→ 该调用**不得**换成 ue8m0 变体；规则 5（新架构先跑无权重 kernel 门）→ 指出本仓库对应的门是哪个测试。
4. **验收命令**（需 GPU，**待本地验证**）：TP4 启动后 grep 日志 `GLM5.2 MLA execution: backend=`，确认与表格一致；`cargo test --release -p pegainfer-glm52 --features glm52 --lib` 全绿。

完成标志：拿审计单给一个没读过 `mla_decode.rs` 的同事，对方能在 10 分钟内回答「这台机器上每 token 每层 KV 占多少字节、由哪段代码保证 scale 不丢精度」。

## 6. 本讲小结

- **吸收式解码**用结合律把 \(W_{UK}\) 折进查询（\(512\) 维 latent 拼上 \(64\) 维 rope 得 `QUERY_DIM=576`）、把 \(W_{UV}\) 留在输出侧，注意力核在 latent 空间工作，KV cache 从逐头 65,536 B/token/层 压到 656/576 B——代价只是每层两个 batched GEMM（`W_UK`/`W_UV`，来自加载期 host 反量化 kv_b）。
- **双后端**：`heads==16` 且 cubin 可用 → FlashInfer fp8（576 B 静态 e4m3、dense cache、融合 pack、贪心 {8,4,2,1} 分批）；否则 FlashMLA fp8-ds（656 B 逐组 scale、支持 strided 页池、split-KV 需 tile 计划）。EP 拓扑永远走 FlashMLA，FlashInfer 是 TP4 的专用路径。
- **sched metadata** 把契约与计划绑进同一对象：tile 计划只依赖 batch/topk/sm_parts 所以构建期一次成型、`num_blocks` 由 `finish_kv` 后绑占位值、`topk()`/`batch()`/`folds_kv_pack()` 让跨模块失配 unrepresentable。
- **cache-pack 编排**按后端分流：FlashMLA 三段（assemble → UE8M0 量化 → pack），FlashInfer 一段融合（顺带把 front 的 split/norm 也吞掉）；`slot_mapping` 是设备端数据，整条链 graph 安全。
- **UE8M0 教训**：sm100 FlashMLA 把 f32 scale 截断成 2 的幂，写入方必须向上取整——一次「0.67× 缩小」的 GB300 事故换来的契约，被写死在量化 kernel 的使用边界注释里，且只约束 656 B cache，不波及 GEMM 激活量化。

## 7. 下一步学习建议

- **u4-l4（DSA 索引器）**：本讲 attend 消费的 `topk`（全局槽位列表）从哪来、`Glm52MlaSchedMetadata::topk()` 如何保证 indexer 与 attend 的 k 一致、21 个 full-indexer 层与 shared 层的跨层复用。
- **u6-l2 / u6-l3（整步前向与 CUDA Graph）**：本讲的整条 attend 链如何被装进 `run_step_body` 的 78 层循环并按 bucket 预捕获；scratch 的指针稳定性（本讲 [mla_decode.rs:90-94](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/mla_decode.rs#L90-L94) 已埋下伏笔）在那里成为硬约束。
- **u5-l4（TP4 prefill-only）**：prefill 侧的稀疏注意力走 bf16 FlashMLA prefill kernel（本讲的 decode 后端不参与），以及 656 B 线格式在 P/D 交接中的角色。
- **延伸阅读**：[../docs/lessons/flashmla-sm100-ue8m0-kv-scales.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/lessons/flashmla-sm100-ue8m0-kv-scales.md)（定位方法论：层门 → α 拟合 → 无权重 kernel 门的分层排查）与 [../docs/models/glm52/tp4-gb300-bringup.md](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/docs/models/glm52/tp4-gb300-bringup.md)（FlashInfer cubin 的来源与 selector 闭包）。
