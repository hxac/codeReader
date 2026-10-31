# Prefill 主链路：分块、位置校验与 last-hidden 提取

## 1. 本讲目标

prefill（预填充）是模型「读完提示词」的阶段：把整条 prompt 一次（或分几次）喂进网络，为第一个生成 token 做准备。本讲沿 `prefill_last_hidden` 这一个函数，把 Qwen3.5 prefill 主链路的三件事讲透：

1. 解释 `PREFILL_CHUNK_LEN` 与 `SCRATCH_ESTIMATE_SEQ` 为什么相等——「启动期预留恰好覆盖一个最大 chunk」的显存契约。
2. 追踪一次多 chunk 预填充中，分页 KV（全注意力层）与 recurrent 状态（线性注意力层）是如何逐块原地推进的。
3. 理解「先验证后变更」的错误处理策略：所有可预判的失败（位置溢出、RoPE 覆盖不足）必须在第一个 chunk 改写状态之前发生。

学完本讲，你应该能独立推导一条任意长度 prompt 在 prefill 主链路上的每一步状态变化。

## 2. 前置知识

### 2.1 prefill 要推进的两套状态

回顾 [u1-l1](u1-l1-crate-positioning.md) 与 [u3-l2](u3-l2-linear-attention-gdn.md)：Qwen3.5 是混合架构（4B 为 8 层全注意力 + 24 层线性注意力），因此 prefill 必须同时推进两套状态：

| 状态 | 所属层 | 存放位置 | prefill 时怎么变 |
|------|--------|----------|------------------|
| 分页 KV | 全注意力层 | 模型级共享分页池 `KvBuffer` | 本 chunk 的 K/V 由融合预备内核直接写进该请求的新页 |
| recurrent 状态 | 线性注意力层（Gated DeltaNet） | 调度器侧每请求一份 `RecurrentState` | GDR chunkwise 内核原地推进 `[H,K,V]` f32 状态；conv1d 更新末尾抽头 |

`RecurrentState` 里有一个纯主机侧字段 `seq_len`——「该请求已处理过多少个 token」，它是 prefill 位置编号的锚点（见 [src/recurrent_state.rs:L30-L35](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/recurrent_state.rs#L30-L35)，注释明确写着 "for prefill/decode tracking"）。

### 2.2 GDR chunkwise 内核需要 scratch

回顾 [u3-l3](u3-l3-ffi-kernel-wrappers.md)：线性注意力层的 prefill 走 GDR 分块流水线（prepare→cumsum→A→solve→recompute→state→output 七个 Triton AOT 内核），这条流水线不是一次 opaque launch，而是需要一组**中间缓冲**（`GdrChunkwiseScratch35`），且这些缓冲的大小随**单次前向的序列长度线性增长**。这是本讲一切设计的出发点。

### 2.3 「先验证后变更」

GPU 上的状态推进是原地的：chunk 0 一旦把 K/V 写进池、把 recurrent 状态推进了，就没有「回滚到 20 分钟前」这回事。所以凡是**不看数据就能预判的失败**（整数溢出、超出 `max_position_embeddings`、RoPE 缓存覆盖不够），都必须在动第一个字节之前检查完。这与 [u2-l1](u2-l1-config-validation.md) 讲过的 config 校验是同一种纪律在执行层的延伸。

### 2.4 术语约定

- **pass（次）**：一次 `prefill_chunk_forward` 前向，处理一段连续 token。
- **chunk（块）**：一次 pass 处理的 token 段，长度 ≤ `PREFILL_CHUNK_LEN`。
- **base_pos**：请求在本轮 prefill 开始前已有的 token 数（`recurrent.seq_len`）；前缀缓存命中或多轮对话时大于 0。
- **页（page）**：分页 KV 池的最小分配单位，本 crate 固定 `page_size = 16` token。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs) | prefill 主链路 | 两个 chunk 常量、`checked_prefill_end_pos`、`prefill_last_hidden` 分块循环、逐层前向、last-hidden 提取 |
| [src/prefill_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs) | GDR chunkwise scratch | 字段清单、分配公式、`estimate_bytes` 启动期预算 |
| [src/unified_forward.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs) | prefill 的三个调用入口 | `batch_prefill_logits` 请求级串行、`unified_step` 组合步、流切换入口 |
| src/weights.rs（节选） | 模型组装与显存预算 | scratch 在 KV 池之前预留；`ensure_rope_cache_covers` |
| src/scheduler/backend.rs、src/scheduler/steps.rs（节选） | 调度侧调用点 | KV 页提前分配、失败时 revert 与 `fail_chunk` |
| src/tp_executor/worker.rs（节选） | TP 路径 | 同一常量钳制 TP 的单步 prefill 长度 |

## 4. 核心概念与源码讲解

### 4.1 chunk 常量：`PREFILL_CHUNK_LEN` 与 `SCRATCH_ESTIMATE_SEQ` 为何相等

#### 4.1.1 概念说明

直觉很朴素：GDR scratch 随 pass 长度 \(L\) 线性增长。如果一条 20 万 token 的 prompt 一次前向吃完，scratch 就要按 20 万 token 预留——显存放不下，也浪费。解决方案分两半：

- **启动期**：按一个固定上界 \(L_{max}\)（20 000 token）估算 scratch 峰值，在分配 KV 池**之前**把这笔显存扣掉。
- **运行期**：把 prefill 切成至多 \(L_{max}\) token 的 chunk 逐块执行，任何一条 prompt 的任何一次 pass 都不超过这个上界。

两个常量相等，就是这两半的对齐：**预留恰好覆盖一个最大 chunk**。这是「跨模块显存契约」——一个写在 `prefill.rs`，一个用在 `weights.rs`，靠命名与注释维系。

#### 4.1.2 核心流程

```text
启动期 (weights.rs)
  estimate_bytes(config, geometry, SCRATCH_ESTIMATE_SEQ=20_000)
      → 从空闲显存中扣除 scratch_reserve → 余量的 85% 给 KV 池

运行期 (prefill.rs, 每条请求)
  token_ids.chunks(PREFILL_CHUNK_LEN=20_000)
      → 每块一次 pass，scratch 按 chunk.len() ≤ 20_000 分配
      → 单 pass scratch 永不超过启动期预留
```

scratch 随 pass 长度的伸缩可以写成（\(H_v\) 为 value 头数，\(C=64\) 为 GDR chunk 宽度，\(d_k=d_v=128\)）：

\[ \text{GDR scratch 字节数} \;\approx\; 4L H_v (2 + C) \;+\; 4\lceil L/C \rceil H_v d_k d_v \;+\; 2L H_v C \;+\; 12 L H_v d \]

每一项都是 \(L\) 的线性函数（详见 4.3）。

#### 4.1.3 源码精读

常量定义带了两段高质量注释。[src/prefill.rs:L6-L12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L6-L12) 明确说 `SCRATCH_ESTIMATE_SEQ` **不是准入上限**——真正的准入由分页 KV 池、RoPE 覆盖和分配成功决定，超长 prompt 靠分块处理而非拒绝：

```rust
/// Sequence length used for conservative prefill scratch reservation.
///
/// This is not an admission cap. Actual prompt admission is governed by the
/// paged KV pool, RoPE cache coverage, and allocation success. Prompts longer
/// than this are handled by chunking prefill at `PREFILL_CHUNK_LEN` rather than
/// being rejected (see `prefill_last_hidden`).
pub(crate) const SCRATCH_ESTIMATE_SEQ: usize = 20_000;
```

[src/prefill.rs:L14-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L14-L20) 给出相等性的理由——「预留恰好覆盖一个 chunk」：

```rust
/// Maximum number of tokens processed in a single prefill forward pass.
///
/// Prefill is chunked at this granularity so the per-pass GDR scratch
/// (`GdrChunkwiseScratch35`, which scales linearly with the pass length) never
/// exceeds the memory reserved at startup. Kept equal to `SCRATCH_ESTIMATE_SEQ`
/// so the reservation in `weights.rs` covers exactly one chunk.
pub(crate) const PREFILL_CHUNK_LEN: usize = SCRATCH_ESTIMATE_SEQ;
```

启动期的消费方在模型组装处。[src/weights.rs:L279-L286](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L279-L286) 用 `estimate_bytes` 算出预留额，注释点明「在 KV 池之前预留，prefill 才不会 OOM」；随后 [src/weights.rs:L315-L317](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L315-L317) 扣除各项预留后把余量的 85% 给 KV 池：

```rust
let max_prefill_len = super::prefill::SCRATCH_ESTIMATE_SEQ;
let scratch_reserve = super::prefill_buffers::GdrChunkwiseScratch35::estimate_bytes(
    &config, geometry, max_prefill_len,
);
// ...
let available = free_bytes - scratch_reserve - recurrent_reserve - snapshot_reserve;
let kv_budget = (available as f64 * 0.85) as usize;
```

TP 路径遵守同一上界：worker 的单步 prefill 长度被钳制为 `max_prefill_tokens.min(PREFILL_CHUNK_LEN)`（[src/tp_executor/worker.rs:L435-L437](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor/worker.rs#L435-L437)），而执行器默认构造直接以 `PREFILL_CHUNK_LEN` 为每步上限（[src/tp_executor.rs:L649-L662](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/tp_executor.rs#L649-L662)）。单 GPU 与 TP 共用同一个 scratch 契约。

#### 4.1.4 代码实践

1. **实践目标**：确认「预留 = 一个最大 chunk」这条契约的所有参与方。
2. **操作步骤**：
   - 在仓库内 `grep -rn "SCRATCH_ESTIMATE_SEQ\|PREFILL_CHUNK_LEN" pegainfer-qwen35/src`，列出全部引用点；
   - 逐点分类：谁定义、谁预留（weights.rs）、谁消费（prefill.rs 分块、tp_executor 钳制）；
   - 纸面推导 Rust `slice::chunks` 语义：长度 45 000 与 20 001 的切片按 20 000 分块各得几段、每段多长。
3. **需要观察的现象**：引用点总共只有 7 处左右，且全部能归入上述三类角色——契约面很小、可审计。
4. **预期结果**：45 000 → 段长 `[20000, 20000, 5000]`；20 001 → `[20000, 1]`（`chunks` 保证最后一段非空）。

#### 4.1.5 小练习与答案

**练习 1**：如果有人把 `PREFILL_CHUNK_LEN` 单独调大到 40 000 而不动 `SCRATCH_ESTIMATE_SEQ`，会发生什么？
**答案**：单次 pass 的 scratch 可以达到 40 000 token 的规模，而启动期只按 20 000 预留。预留之外的显存已被 KV 池、recurrent 状态等占走，长 prompt 的 prefill 会在运行期分配失败（OOM 路径），这正是两个常量必须一起改的原因。

**练习 2**：`SCRATCH_ESTIMATE_SEQ` 是不是 prompt 的准入上限？
**答案**：不是。注释明确说 "This is not an admission cap"。超过 20 000 token 的 prompt 不会被拒绝，而是被分块 prefill；准入真正取决于分页 KV 池余量、RoPE 缓存覆盖与分配是否成功。

**练习 3**：为什么预留发生在 KV 池分配**之前**，而不是之后？
**答案**：KV 池是「有多少显存吃多少显存」的大户。如果先建池，长 prompt 的 prefill scratch 就只能用池建成后剩下的零头，可能不够；先扣 scratch 再让池吃余量，保证 prefill 永远有预算（代价是池小一点）。

### 4.2 先验证后变更：整段校验与串行分块循环

#### 4.2.1 概念说明

`prefill_last_hidden` 是 prefill 主链路的枢纽（对单条请求而言）。它做三件事，顺序是本模块的核心：

1. **整段校验**：对**整条** prompt 的目标区间 \([base\_pos,\; base\_pos + len)\) 一次性检查位置溢出、`max_position_embeddings` 上限、KV 视图一致性、RoPE 缓存覆盖——全部通过才进入循环。
2. **串行分块**：按 `PREFILL_CHUNK_LEN` 逐块前向。每块构造一个「截至本块末尾」的 KV 视图与计划，调用 `prefill_chunk_forward`，推进 recurrent 计数。
3. **last-hidden 提取**：只在最后一块的输出上取最后一个位置的 hidden（见 4.4）。

「先验证后变更」的收益：一条注定越界的 prompt（比如位置编号将超出 262 144）在**任何**状态被改写之前就被拒绝，调用方可以干净地失败这条请求；若校验散在各 chunk 里，前几块已经推进了 KV 与 recurrent 状态，失败后就得处理「半截状态」。

#### 4.2.2 核心流程

一次多 chunk 预填充的推进过程（设 `base_pos = 0`、prompt 长 45 000、`page_size = 16`）：

```text
校验段（不动任何状态）
  end_pos = 0 + 45000 = 45000
  ① checked_add 不溢出 ② 45000 ≤ max_position_embeddings
  ③ full_view.seq_len() == 45000  ④ RoPE cache 覆盖 45000

循环段（每块原地推进两套状态）
  chunk 0: len=20000  start=0      chunk_end=20000  视图取前 1250 页
      → 全注意力层把本块 K/V 写入新页；线性层推进 state/conv_state
      → recurrent.seq_len: 0 → 20000
  chunk 1: len=20000  start=20000  chunk_end=40000  视图取前 2500 页（含 chunk 0 的页）
      → recurrent.seq_len: 20000 → 40000
  chunk 2: len=5000   start=40000  chunk_end=45000  视图取前 2813 页
      → recurrent.seq_len: 40000 → 45000

提取段
  在最后一块输出的 5000 行里取第 4999 行 → DeviceVec[hidden_size]
```

注意一个反直觉的点：**chunk 的注意力要读整个前缀**。第 2 块的 20 000 个 query 不只看自己这 20 000 个 token，还要对第 1 块的 20 000 个 token 做注意力——所以每块的 KV 视图 `seq_len` 是 `chunk_end`（累计长度），而不是本块长度；query 侧才由计划里的「本块起点 + 本块长度」描述。

#### 4.2.3 源码精读

**（a）整段校验。** [src/prefill.rs:L69-L87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L69-L87) 是校验段的全部：非空、`checked_prefill_end_pos`、视图长度一致、RoPE 覆盖。注释把动机写得非常清楚：

```rust
// Validate the full target range up front (position overflow + RoPE cache
// coverage) so an out-of-range prompt is rejected before any chunk mutates
// the KV / recurrent state, rather than failing partway through.
let base_pos = recurrent.seq_len;
let end_pos = checked_prefill_end_pos(
    base_pos,
    token_ids.len(),
    self.config.max_position_embeddings,
)?;
anyhow::ensure!(
    full_view.seq_len() == end_pos,
    "Qwen3.5 prefill view ends at {}, expected {end_pos}",
    full_view.seq_len()
);
self.ensure_rope_cache_covers(end_pos)?;
```

`base_pos` 取自 `recurrent.seq_len`（[src/prefill.rs:L76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L76)）——这就是「前缀缓存命中恢复后续写」「多轮对话追加」时位置自动接续的机制：状态里记了多少 token，本块就从多少号开始编号。

**（b）位置边界函数。** [src/prefill.rs:L46-L59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L46-L59) 用 `checked_add` 防整数溢出、用 `ensure!` 钳上限，配套三个单测（[src/prefill.rs:L566-L598](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L566-L598)）分别覆盖「恰好到达上限」「超过上限」「加法溢出」：

```rust
fn checked_prefill_end_pos(
    base_pos: usize, seq_len: usize, max_position_embeddings: usize,
) -> Result<usize> {
    let end_pos = base_pos.checked_add(seq_len).ok_or_else(|| { /* 溢出 */ })?;
    anyhow::ensure!(
        end_pos <= max_position_embeddings,
        "Qwen3.5 prefill requested end_pos={end_pos}, beyond max_position_embeddings={max_position_embeddings}"
    );
    Ok(end_pos)
}
```

RoPE 覆盖检查在 [src/weights.rs:L407-L415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L407-L415)：缓存是加载期一次建满的（见 [u3-l1](u3-l1-full-attention-layer.md)），这里只校验、不扩容——所以覆盖不足必须在 prefill 前发现，而不是跑到一半。

**（c）分块循环。** [src/prefill.rs:L94-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L94-L139) 是主循环。每个 chunk 依次做四件事：

```rust
for chunk in token_ids.chunks(PREFILL_CHUNK_LEN) {
    // ① 释放上一块的输出再分配本块 scratch，峰值不超过一个 chunk 的预算
    drop(hidden_batch.take());
    let (chunk_hidden, chunk_scratch) = self.prepare_prefill_chunk(chunk)?;
    // ② 视图 = full_view 的前 page_count 页，长度为累计 chunk_end
    let chunk_end = base_pos + offset + chunk.len();
    let page_count = chunk_end.div_ceil(page_size);
    let chunk_view = KvView::new(
        full_view.page_indices()[..page_count].to_vec(),
        chunk_end, page_size,
    );
    // ③ 计划：query 从 base_pos+offset 起、长 chunk.len()，读整个前缀
    let plan = PrefillPagedPlan::from_raw_batch_with_cta_tile_q(
        &self.ctx, &page_indices, &[chunk_view.last_page_len()],
        &[base_pos + offset], &[chunk.len()],
        /* heads / head_dim / tile 参数 */ ...)?;
    // ④ 前向（推进 KV 页与 recurrent 状态），offset 前移
    hidden_batch = Some(self.prefill_chunk_forward(chunk_hidden, chunk_scratch, &access, recurrent)?);
    offset += chunk.len();
}
```

（节选自 [src/prefill.rs:L97-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L97-L139)，参数有删节。）

三个细节值得咀嚼：

- **`drop(hidden_batch.take())`（[L98-L100](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L98-L100)）**：上一块的输出 hidden（`hidden_size × 20000` 行 bf16，4B 上约 100 MiB）先释放，再分配本块 scratch——新旧不叠加，峰值显存恒为「一个 chunk 的 scratch + 一份输出」。
- **视图是累计的、query 是增量的**：`KvView::new(..., chunk_end, page_size)`（[L104-L108](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L104-L108)）把本块「可见的池内容」定义为从头到 `chunk_end`；而计划里 `&[base_pos + offset]` 与 `&[chunk.len()]`（[L110-L120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L110-L120)）说明 query 只是本块新增的 token。二者合起来正是「新 query 对全部历史做因果注意力」。
- **recurrent 计数由前向函数推进**：[src/prefill.rs:L229-L231](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L229-L231) 在 `prefill_chunk_forward` 末尾执行 `recurrent.seq_len += seq_len`，注释说明「paged KV 的位置由调用方提交」——KV 侧的推进藏在全注意力层写池的动作里，主机侧只推进 recurrent 计数这一个数字。

**（d）chunk 内的逐层前向。** `prefill_chunk_forward`（[src/prefill.rs:L204-L234](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L204-L234)）遍历所有层，用 `full_idx` / `linear_idx` 双计数器分别索引两类层（呼应 [u2-l4](u2-l4-layer-structures.md) 的「权重与状态分离」）。每层的五步（[src/prefill.rs:L238-L302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L238-L302)）在两种层上的分叉：

| 步骤 | 全注意力层（[L305-L446](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L305-L446)） | 线性注意力层（[L448-L552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L448-L552)） |
|------|----------------------------------------|---------------------------------------|
| 投影 | `q/k/v_proj` 三个 GEMM | 对融合权重 `in_proj_qkvz`/`in_proj_ba` 做行切读取（`gemm_rows_into`） |
| 状态预备 | 融合内核一次完成 qk_norm + partial RoPE + **K/V 直接落池**（[L337-L373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L337-L373)） | `conv1d_prefill_batch_into` 整段卷积并把末尾抽头写回 `conv_state`（[L506-L514](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L506-L514)） |
| 核心计算 | `batch_prefill_paged_cuda_hd256` 批量分页注意力（[L375-L423](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L375-L423)） | `gated_delta_rule_prefill_chunkwise_into` 七步流水线，**原地推进 `layer_state.state`**（[L516-L531](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L516-L531)） |
| 输出整形 | gate 乘法 + `o_proj` + all_reduce | z 门控 RMSNorm + `out_proj` + all_reduce（[L533-L550](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L533-L550)） |

也就是说：**KV 的推进 = 全注意力层的落池内核；recurrent 的推进 = 线性层的 chunkwise 内核**。两者都发生在 chunk 前向内部，且都是原地的。

**（e）失败路径。** 循环中途失败（如内核返回非零码）时：本 chunk 内 `?` 直接上抛；调度器一侧 [src/scheduler/backend.rs:L368-L371](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L368-L371) 会 `revert_scheduled_requests` 收回本步新调度的 KV 页，[src/scheduler/steps.rs:L23-L30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/steps.rs#L23-L30) 把整个 chunk 交给 `fail_chunk`——请求报错结束，但引擎继续服务其他请求。前置校验的价值正在于此：它拦下的是「注定失败」的情况，让运行期失败尽量只剩「真的硬件/内核错误」这一类。

**（f）两级串行。** `prefill_last_hidden` 之上还有一层请求级串行：`batch_prefill_logits` 对同一批的每条请求逐条调用它（[src/unified_forward.rs:L44-L61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L44-L61)）。模块开头的注释（[L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L1-L11)）解释了原因：GDR chunkwise 没有跨请求的批量 prefill 内核，所以「批 prefill」实际是逐请求串行，最后只把各请求的 last-hidden 拼批共享一次 norm + lm_head。

#### 4.2.4 代码实践

1. **实践目标**：手推一条 45 000 token prompt 走分块循环时所有循环变量的取值。
2. **操作步骤**：以 `base_pos = 0`、`page_size = 16`、`PREFILL_CHUNK_LEN = 20 000`，对照 [src/prefill.rs:L97-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L97-L139)，为每个 chunk 计算 `chunk.len()`、`base_pos + offset`、`chunk_end`、`page_count = ceil(chunk_end/16)` 与循环后的 `recurrent.seq_len`，填成表格（参考答案见第 5 节综合实践）。
3. **需要观察的现象**：`page_count` 每块单调递增且包含之前所有块的页；`recurrent.seq_len` 与 `chunk_end` 每块同步。
4. **预期结果**：三块的 `(len, start, end, page_count)` 分别为 `(20000, 0, 20000, 1250)`、`(20000, 20000, 40000, 2500)`、`(5000, 40000, 45000, 2813)`。纯纸面推导，无需 GPU；若想本地验证，可在循环里临时加一行 `tracing::debug!` 打印这些量（改完记得还原，本仓库不允许把调试改动留在源码里）。

#### 4.2.5 小练习与答案

**练习 1**：什么时候 `base_pos > 0`？
**答案**：请求已有部分历史时——典型场景是前缀缓存命中后从快照恢复继续 prefill（[u6-l2](u6-l2-snapshot-store-restore.md)），或多轮对话在已有 KV/recurrent 状态上追加新 prompt。`base_pos = recurrent.seq_len` 把位置编号自动接上。

**练习 2**：为什么 chunk 视图的 `seq_len` 是 `chunk_end` 而不是 `chunk.len()`？
**答案**：本块 query 要对全部历史做因果注意力。视图描述的是「池中可见内容」（从头到 `chunk_end` 的所有页），query 侧信息（起点 `base_pos + offset`、长度 `chunk.len()`）由 `PrefillPagedPlan` 单独携带。若视图只含本块，注意力会看不到之前的 token，结果错误。

**练习 3**：如果第 2 块中途内核失败，第 1 块已写入池里的 K/V 会被回滚吗？请求的命运是什么？
**答案**：不会回滚——写入是原地的。调度器收回的是「本步新调度」的 KV 页（`revert_scheduled_requests`），整个 chunk 走 `fail_chunk`，该请求以错误终止并发射失败事件；同批其他请求与后续新请求不受影响。这正是前置校验要尽量把失败提前的原因：能预判的失败不产生半截状态。

### 4.3 GDR scratch 预留：`GdrChunkwiseScratch35` 的字段与预算

#### 4.3.1 概念说明

`GdrChunkwiseScratch35` 是 GDR chunkwise 七步流水线（[u3-l3](u3-l3-ffi-kernel-wrappers.md)）的中间缓冲集合。它有两个身份：

- **运行期**：每个 chunk 开始时新建一份（`prepare_prefill_chunk` 里 `new`），块结束随 scratch 变量一起释放——生命周期恰好一个 pass。
- **启动期**：它的类方法 `estimate_bytes` 是显存预算公式，决定模型加载时扣留多少显存。

理解它的字段形状，就理解了「为什么 scratch 随 pass 长度线性伸缩」以及「预留为什么是保守的」。

#### 4.3.2 核心流程

设单次 pass 长度为 \(L\)，value 头数 \(H_v\)，key/value 头维 \(d_k = d_v = 128\)，GDR chunk 宽度 \(C = 64\)。分配发生在 [src/prefill_buffers.rs:L69-L114](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L69-L114)，全部缓冲可以按「伸缩因子」分成三类：

\[ \text{每 token 一行}：\; g\_cumsum,\, beta \;=\; L H_v \quad\text{(f32)} \]

\[ \text{每 token 一行、行宽 } C：\; a\_tril \;=\; L H_v C \;\text{(f32)},\; a\_inv \;=\; L H_v C \;\text{(bf16)} \]

\[ \text{每 } C \text{ 个 token 一份快照}：\; chunk\_state \;=\; \lceil L/C \rceil\, H_v d_k d_v \;\text{(f32)} \]

另有 6 个 token-major 的 `HiddenStates`（`q_expanded`、`k_expanded`、`v_raw`、`w`、`u`、`v_new`），各为 \(L \cdot H_v d\)（bf16）。所有项都是 \(L\) 的线性函数——**没有任何一项随 prompt 总长或上下文增长**，这就是分块能够封顶显存的数学根源。

#### 4.3.3 源码精读

字段清单在 [src/prefill_buffers.rs:L22-L49](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L22-L49)，每个字段都标注了形状与 dtype，摘录几个关键项：

```rust
pub struct GdrChunkwiseScratch35 {
    /// Chunk-local cumulative gate, fp32: [seq_len, num_value_heads]
    pub(crate) g_cumsum: CudaSlice<f32>,
    /// Beta values, fp32: [seq_len, num_value_heads]
    pub(crate) beta: CudaSlice<f32>,
    /// Chunk attention matrix storage, fp32: [seq_len, num_value_heads, chunk_size]
    pub(crate) a_tril: CudaSlice<f32>,
    /// Inverse (I + A)^-1 in bf16: [seq_len, num_value_heads, chunk_size]
    pub(crate) a_inv: CudaSlice<bf16>,
    /// Per-chunk recurrent state snapshots, fp32:
    /// [num_chunks, num_value_heads, key_dim, value_dim]
    pub(crate) chunk_state: CudaSlice<f32>,
    // …另有 q_expanded / k_expanded / v_raw / w / u / v_new 六个 HiddenStates
}
```

- `g_cumsum` / `beta`：cumsum 阶段的每 token 门控累积与 β 值。
- `a_tril` / `a_inv`：chunk 内注意力三角矩阵及其逆 \((I+A)^{-1}\)（solve 阶段），每 token 一行、行宽 64。
- `chunk_state`：**每个 64-token chunk 一份递推状态快照**（recompute 阶段用），是单项最重的缓冲。
- 六个 `HiddenStates`：token-major 的展开/预备张量，供后续阶段消费。

结构体头部的文档（[L12-L21](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L12-L21)）点明设计立场：目标刻意收窄（batch=1、固定形状、仅前向、chunk_size=64），缓冲显式列出是因为「chunk-wise 路径天然是多阶段流水线，而不是一次 opaque kernel launch」。

分配逻辑 [src/prefill_buffers.rs:L76-L99](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L76-L99)：

```rust
let kv_hidden_dim = num_value_heads * key_dim;
let vv_hidden_dim = num_value_heads * value_dim;
let num_chunks = seq_len.div_ceil(Self::CHUNK_SIZE);   // CHUNK_SIZE = 64（L52）
let g_cumsum = ctx.stream.alloc_zeros(seq_len * num_value_heads)?;
// …beta、a_tril、a_inv 同理
let chunk_state = ctx.stream
    .alloc_zeros(num_chunks * num_value_heads * value_dim * key_dim)?;
```

启动期预算 `estimate_bytes`（[src/prefill_buffers.rs:L120-L181](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L120-L181)）= GDR scratch + 逐层瞬态峰值，其中 GDR 部分（[L144-L158](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L144-L158)）：

```rust
let f32_elems = seq * num_vh            // g_cumsum
    + seq * num_vh                      // beta
    + seq * num_vh * chunk_sz           // a_tril
    + num_chunks * num_vh * val_dim * key_dim; // chunk_state
let bf16_elems = seq * num_vh * chunk_sz        // a_inv
    + kv_hidden * seq                           // q_expanded
    + kv_hidden * seq                           // k_expanded
    + vv_hidden * seq                           // v_raw
    + kv_hidden * seq                           // w
    + vv_hidden * seq                           // u
    + vv_hidden * seq;                          // v_new
f32_elems * 4 + bf16_elems * 2
```

逐层瞬态部分（[L160-L178](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L160-L178)）取「共享 hidden 缓冲 + max(全注意力瞬态, MLP 瞬态)」——注意力与 MLP 不同时活跃，取 max 而非求和。注释还记录了一个历史：直写分页的 prefill 把 K/V 直接落池后，HND KVCache 暂存缓冲就退出了 scratch 清单。

预算不足时启动直接失败：[src/weights.rs:L300-L314](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L300-L314) 要求空闲显存 ≥ scratch + recurrent + 快照 + 最小 KV 池，否则带着明确建议（降 `--max-batch`、降前缀缓存预算或换小模型）拒载。实际扣留值会打进启动日志（[src/weights.rs:L322-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L322-L326) 的 `prefill scratch reserve: X MB` 行）。

#### 4.3.4 代码实践

1. **实践目标**：验证「scratch 随 pass 长度线性伸缩」并量化一个最大 chunk 的开销。
2. **操作步骤**：以 Qwen3.5-4B 单 GPU（TP1）为例取 \(H_v = 32\)、\(d_k = d_v = 128\)、\(C = 64\)、\(L = 20\,000\)（数字来源见 [docs/models/qwen35/tp-design.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/tp-design.md) 的 4B 几何：32 个 value 头、头维 128）。手工计算 `f32_elems`、`bf16_elems` 与 `gdr_bytes`，再核对：把 \(L\) 翻倍，各项是否恰好翻倍。
3. **需要观察的现象 / 预期结果**：
   - `num_chunks = ⌈20000/64⌉ = 313`；
   - f32 元素 = 640 000 + 640 000 + 40 960 000 + 313×524 288（= 164 102 144）≈ 2.063 亿 → 约 787 MiB；
   - bf16 元素 = 40 960 000 + 6×81 920 000 ≈ 5.325 亿 → 约 1 016 MiB；
   - **GDR 部分合计约 1.80 GiB**（加上逐层瞬态才是完整 `scratch_reserve`），其中 `chunk_state` 单项约 656 MiB、是最重的一项；
   - \(L\) 翻倍 → 每项翻倍，确认线性。
4. 本机若装好 GPU + 4B 权重，可启动引擎对照日志中的 `prefill scratch reserve` 行验证手算（**待本地验证**）；无 GPU 环境做纯纸面推导即可。

#### 4.3.5 小练习与答案

**练习 1**：哪个字段随「chunk 数」而非「token 数」伸缩？这个 chunk 数与 `PREFILL_CHUNK_LEN` 的 chunk 是一回事吗？
**答案**：`chunk_state`，大小为 \(\lceil L/64 \rceil \cdot H_v d_k d_v\)。不是一回事——它是 GDR 内核内部的 64-token 小块（`CHUNK_SIZE = 64`）；`PREFILL_CHUNK_LEN` 是前向 pass 级的 20 000-token 大块。一个 pass 内部还套着 ⌈L/64⌉ 个 GDR 小块。

**练习 2**：为什么 `a_tril` 的形状是 `[seq_len, num_value_heads, chunk_size]`——第三维为什么是 64 而不是 `seq_len`？
**答案**：GDR 分块算法把序列切成 64-token 的块，块内注意力矩阵最多 64×64；存储按「每 token 一行、行宽 64」的下三角布局。这让该项保持 \(L\) 线性而不是 \(L^2\)——若是整段 \(L^2\) 三角阵，20 000 token 根本存不下。

**练习 3**：scratch 是每 chunk 新建的，为什么不做成模型级常驻、一次分配循环复用？
**答案**：常驻按最大 chunk 分配也可以工作，但模型级常驻意味着整个服务生命周期占住这块显存且要防并发误用；当前设计「每块新建、块毕即释」让生命周期与 pass 严格对齐，配合 `drop(hidden_batch.take())` 把峰值钉在一个 chunk 的预算内。启动期仍按上界预留（KV 池前扣除），所以运行期分配不会与他人争抢。

### 4.4 last-hidden 提取：从最后一个 token 到批 logits

#### 4.4.1 概念说明

prefill 的最终产物只需要**最后一个位置**的 hidden state——用它过一遍最终 norm + lm_head 得到首个生成 token 的 logits。但注意因果注意力的性质：最后位置 attends 全部前缀，所以整条 prompt 的所有位置都必须算，只是**别的位置的输出行用完即弃**。

于是链路分两段：

1. `prefill_last_hidden`：每块返回全块 hidden（`hidden_size × chunk_len`），循环结束后只从最后一块取最后一行（`extract_vec`）。
2. `batch_last_hidden_logits`：把批内各请求的 last-hidden 行拼成一个 `[hidden_size, n]` 矩阵，**共享一次** rms_norm 与 lm_head GEMM，输出 `[selection_vocab, n]` 的批 logits。

#### 4.4.2 核心流程

```text
chunk 前向输出 hidden_batch [hidden_size, chunk_len]
        │  （只保留最后一块）
        ▼
extract_vec(hidden_batch, seq_len - 1)  →  DeviceVec [hidden_size]   （每请求一份）
        │
        ▼  n 条请求
batch_last_hidden_logits:
  write_vec_into 逐行写入 → batched [hidden_size, n]
  rms_norm_batch_offset_into → normed [hidden_size, n]
  output_logits_into → logits [selection_vocab, n]   （含对齐 pad 列的 -inf 抑制）
```

#### 4.4.3 源码精读

提取点在主函数末尾（[src/prefill.rs:L140-L145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L140-L145)）：`hidden_batch` 此时是最后一块的输出，`seq_len - 1` 即最后位置：

```rust
// `seq_len > 0` guarantees at least one chunk produced hidden states.
let hidden_batch = hidden_batch.expect("non-empty prefill produced no chunk");
// Last-token logic runs once, on the final chunk's output.
ops::extract_vec(&self.ctx, &hidden_batch, hidden_batch.seq_len - 1)
```

`extract_vec` 与 `write_vec_into` 都是 `pegainfer-core` 通用算子，经 ops.rs 目录页重导出（[src/ops.rs:L10](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L10)、[L19](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/ops.rs#L19)）。

拼批与 logits 在 [src/prefill.rs:L147-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L147-L177)：逐请求校验行宽后 `write_vec_into` 写入预分配的 `HiddenStates`，再共享 norm 与输出投影：

```rust
let mut batched = HiddenStates::zeros(&self.ctx, hidden_dim, n)?;
for (request_idx, last_hidden) in last_hiddens.iter().enumerate() {
    anyhow::ensure!(last_hidden.len == hidden_dim, /* 行宽校验 */);
    ops::write_vec_into(&self.ctx, last_hidden, &mut batched, request_idx)?;
}
let mut normed = HiddenStates::zeros(&self.ctx, hidden_dim, n)?;
ops::rms_norm_batch_offset_into(&self.ctx, &batched, &self.norm,
    self.config.rms_norm_eps, &mut normed)?;
let mut logits = HiddenStates::zeros(&self.ctx, self.config.selection_vocab, n)?;
self.output_logits_into(&normed, &mut logits)?;
```

`output_logits_into` 是 logits 的必经点——`selection_vocab` 与 `decodable_vocab` 之间的对齐 pad 列在这里被压成 -inf（回顾 [u2-l3](u2-l3-weight-loading.md)；`unified_forward.rs` 的单测 `pad_columns_are_suppressed_on_the_model_logits_paths` 专门守护这条性质，[src/unified_forward.rs:L179-L250](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L179-L250)）。

调用入口（谁调 `prefill_last_hidden`）在 [src/unified_forward.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs) 有三个，服务不同执行形态：

| 入口 | 位置 | 用途 |
|------|------|------|
| `batch_prefill_logits` | [L37-L61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L37-L61) | 基础形态：逐请求串行调 `prefill_last_hidden`，再拼批出 logits |
| `batch_prefill_logits_on_stream` | [L66-L89](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L66-L89) | SharedSM 重叠形态：临时把 `ctx.stream` 换成第二 CUDA 流并激活 `StreamOverrideGuard`，让 prefill 与 decode 图重放并行（[u4-l4](u4-l4-unified-step-overlap.md) 展开） |
| `unified_step` | [L101-L146](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/unified_forward.rs#L101-L146) | 组合步：先串行 prefill 新请求，再用 CUDA Graph decode 老请求，一次调用完成 |

调度器侧的对应关系（单 GPU 后端）：串行 prefill 步走 `backend.batch_prefill_logits`（[src/scheduler/backend.rs:L355-L373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L355-L373)，先 `schedule_prefill_views` 分配 KV 页，失败即 revert）、重叠步走 `launch_async_prefill`（[L379-L436](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L379-L436)）、组合步走 `unified_step`（[L438-L474](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/scheduler/backend.rs#L438-L474)）。KV 页的分配/回收都在调度层完成——`prefill_last_hidden` 拿到的 `full_view` 已经覆盖整条本步调度的窗口。

> 概念澄清：本讲的「分块」发生在**模型前向内部**（保护 scratch）；调度器还有一层跨步的 chunked prefill（把长 prompt 拆成多个调度窗口、跨多个 step 喂入，见 [u5-l2](u5-l2-execution-plan-admission.md) 与 `tests/chunked_prefill.rs`）。两层机制独立、可以叠加：一个调度窗口内部再按 `PREFILL_CHUNK_LEN` 分块。

#### 4.4.4 代码实践

1. **实践目标**：把 last-hidden 数据流上每个张量的形状写清楚。
2. **操作步骤**：以 4B（`hidden_size = 2560`）+ 3 条请求为例，沿 [src/prefill.rs:L144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L144) → [L147-L177](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L147-L177) 写出每个中间张量的 `[宽, 行数]`：每请求 `DeviceVec [2560]` → `batched [2560, 3]` → `normed [2560, 3]` → `logits [selection_vocab, 3]`；再回答：为什么最后一步宽度是 `selection_vocab` 而不是 `vocab_size`（提示：回看 [u2-l1](u2-l1-config-validation.md) 的三个词表宽度）。
3. **需要观察的现象**：整条链路只有一次 lm_head GEMM，行数为请求数而不是 prompt 总 token 数。
4. **预期结果**：形状链如上；`selection_vocab` 是 128 对齐后的 GEMM 快路径宽度，pad 列由 `output_logits_into` 压 -inf，保证采样只在 `decodable_vocab` 内选。纸面推导即可，无需 GPU。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `extract_vec` 只在最后一块上做，前面的块连输出都不保留？
**答案**：prefill 只需要最后位置的 hidden 产首 token logits；前面块的所有中间行用完即弃。循环里 `drop(hidden_batch.take())` 在分配下一块 scratch 前先释放上一块输出，正是把「弃」落实成显存峰值控制。

**练习 2**：既然逐请求串行前向，为什么还要 `batch_last_hidden_logits` 拼批？
**答案**：前向受限于「GDR 没有跨请求批量 prefill 内核」只能串行，但 norm 与 lm_head 是普通 GEMM，把 n 条请求的 last-hidden 拼成 `[hidden, n]` 一次算，比逐请求各发一次小 GEMM 高效得多。

**练习 3**：`batch_prefill_logits_on_stream` 为什么要 `std::mem::replace` 换流、结束时再换回来？
**答案**：模型所有算子默认从 `ctx.stream` 取流发射；要在第二条 CUDA 流上跑 prefill，就得让 `ctx.stream` 临时指向它。模型为调度线程独占，临时换流不会与别的主机调用竞争；配合 `StreamOverrideGuard::activate_prefill` 让内核侧 FFI 也记到正确的流上。细节在 [u4-l4](u4-l4-unified-step-overlap.md) 展开。

## 5. 综合实践

**任务：为一条 45 000 token 的提示词写完整的分块推进表，并分析 scratch 的伸缩性。**

设定：新请求（`base_pos = 0`）、`PREFILL_CHUNK_LEN = 20 000`、`page_size = 16`、Qwen3.5-4B 单 GPU。对照 [src/prefill.rs:L97-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill.rs#L97-L139) 的循环变量，写出下表（参考答案）：

| chunk # | token 区间 | `chunk.len()` | 计划 start（`base_pos+offset`） | `chunk_end` | `page_count = ⌈chunk_end/16⌉` | 末页长度 | 循环后 `recurrent.seq_len` |
|---------|-----------|---------------|--------------------------------|-------------|------------------------------|----------|---------------------------|
| 0 | [0, 20000) | 20 000 | 0 | 20 000 | 1 250（整页） | 16 | 20 000 |
| 1 | [20000, 40000) | 20 000 | 20 000 | 40 000 | 2 500（整页） | 16 | 40 000 |
| 2 | [40000, 45000) | 5 000 | 40 000 | 45 000 | 2 813 | 8 | 45 000 |

核对要点：

1. 45 000 = 16×2 812 + 8，所以第 3 块需要 2 813 页、末页只有 8 个 token——这正是计划里 `last_page_len` 的来源。
2. 每块的全注意力层把本块 K/V 写入视图**新增**的页（第 1 块写第 0–1 249 页，第 2 块写第 1 250–2 499 页，第 3 块写第 2 500–2 812 页），线性层原地推进 `state` / `conv_state`，主机侧 `recurrent.seq_len` 与 `chunk_end` 始终同步。
3. 前置校验一次性检查的是 `end_pos = 45 000 ≤ max_position_embeddings` 与 RoPE 覆盖，而不是逐块检查。

scratch 伸缩性分析（对照 [src/prefill_buffers.rs:L22-L49](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/prefill_buffers.rs#L22-L49) 的字段）：**全部 11 个缓冲都只随单次 pass 长度线性伸缩，与 prompt 总长无关**——这是分块有效的根本。按伸缩因子分三组：

- 每 token 一行（`g_cumsum`、`beta`，及六个 token-major `HiddenStates`）：\(\propto L\)；
- 每 token 一行、行宽 64（`a_tril` f32、`a_inv` bf16）：\(\propto L \times 64\)；
- 每 64-token GDR 小块一份（`chunk_state`）：\(\propto \lceil L/64 \rceil\)，4B/TP1、\(L=20\,000\) 时单项约 656 MiB，是最重的一项。

因此 45 000 token 的 prompt 三次 pass 的 scratch 峰值分别为约 1.80 GiB / 1.80 GiB / 0.45 GiB（GDR 部分，逐层瞬态另计）——任一时刻都不超过启动期按 20 000 token 预留的额度。若有 GPU 环境，可启动引擎并用日志行 `prefill scratch reserve: X MB`（[src/weights.rs:L322-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/weights.rs#L322-L326)）对照手算，**待本地验证**。

## 6. 本讲小结

- `PREFILL_CHUNK_LEN = SCRATCH_ESTIMATE_SEQ = 20 000`：启动期按该上界估算并**在 KV 池之前**扣留 scratch，运行期把每次 pass 钳在同一上界——「预留恰好覆盖一个最大 chunk」是跨 `prefill.rs` / `weights.rs` / TP worker 的显存契约；它不是准入上限，超长 prompt 靠分块消化。
- `prefill_last_hidden` 先对整条 prompt 的目标区间做一次性校验（`checked_add` 防溢出 → `max_position_embeddings` 上限 → 视图长度一致 → RoPE 覆盖），全部通过后才让第一个 chunk 动状态——「先验证后变更」。
- 分块循环每步构造**累计** KV 视图（前 `⌈chunk_end/page_size⌉` 页）+ **增量** query 计划（起点 `base_pos+offset`、长度 `chunk.len()`）：全注意力层把本块 K/V 融合落池，线性层原地推进 GDN 状态与 conv 抽头，主机侧 `recurrent.seq_len += chunk.len()`；`base_pos = recurrent.seq_len` 让前缀缓存/多轮场景自动接续位置。
- `GdrChunkwiseScratch35` 的 11 个缓冲全部随 pass 长度线性伸缩（每 token、每 token×64、每 64-token 小块三种因子），4B/TP1 一个满 chunk 的 GDR 部分约 1.8 GiB，`chunk_state` 是最重单项；逐块新建、块毕即释，配合 `drop(hidden_batch.take())` 把峰值钉死。
- prefill 只需最后位置的 hidden：最后一块输出上 `extract_vec` 取一行，逐请求拼批后共享一次 norm + lm_head（`output_logits_into` 同时完成 pad 列 -inf 抑制）。
- 「批 prefill」是两级串行：请求级（GDR 无跨请求批量内核）+ chunk 级；`unified_forward.rs` 提供模型流 / 重叠流 / 组合步三个入口，KV 页的分配与失败回收都在调度层。

## 7. 下一步学习建议

本讲讲完了「一条请求如何被读完」。下一步按顺序：

1. **[u4-l2](u4-l2-batch-decode.md) 批量解码 batch_decode**：prefill 之后的每一步——N 个请求各 1 token 的混合批前向，看 decode 侧全注意力与线性注意力各读写哪些缓冲（与本讲的 prefill 侧形成对照）。
2. **[u4-l3](u4-l3-cuda-graph-buckets.md) CUDA Graph 与批次桶**：`copy_state_to_slot` / `move_slot_within` 的槽位契约正是建立在本讲「recurrent 状态外置、指针稳定」的基础上。
3. **[u4-l4](u4-l4-unified-step-overlap.md) unified_step 与 SharedSM 重叠**：本讲已见 `batch_prefill_logits_on_stream` 的流切换雏形，那一讲展开第二流、事件与安全上限。
4. 想先看调度层如何决定「这一步喂哪个窗口」的读者，可跳到 [u5-l2](u5-l2-execution-plan-admission.md) 的执行计划与准入，再回头看本讲的「调度窗口 vs 前向 chunk」两层分块之辨。
