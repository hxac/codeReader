# u3-l8 split-KV 解码与投影融合

## 1. 本讲目标

学完本讲，你应该能够：

1. 手推 `SplitKvConfig::actual_chunk_size` 的 chunk 公式，并解释「chunk 下限 + 按上限凑整」这两个方向各是为了什么。
2. 把 `split_kv_{chunk}x{max}` 标签与 `kernel_manifests/qwen3.toml` 里的变体一一对应，说清生产 Tuned 配置就是 `split_kv_64x64`。
3. 解释 `DecodeAttentionPath` 的 non_partition / split_kv 分叉：Tuned 按批宽（CTA 数 vs SM 数）选路，Pin/PerToken 无条件钉死 SplitKv。
4. 读懂 `projection_fusion.rs` 的失败关闭（fail-closed）资格门：为什么 QKV 融合只在「TP=1 + Tuned + 无重叠 + 无草稿器 + SM 与几何逐行认证」全部成立时才启用，以及 `QUALIFIED_QKV` 表为何按 `(SM, hidden, q_dim, kv_dim, layers)` 逐行认证。
5. 说出本次提交（785d0487 → af9e40e1）带来的变化：投机解码强制 Split 路径的参数从 `dflash_enabled` 推广为 `draft_enabled`，理由标签从 `"dflash"` 改为 `"speculative"`——DFlash 与 EAGLE-3 两种草稿器现在走同一道门。

## 2. 前置知识

- **CTA 与 SM**：CUDA 里一个线程块（CTA）是 GPU 的一次调度单位，SM（流式多处理器）是物理执行单元。GPU 的并行度上限 ≈ SM 数；如果内核只派出很少的 CTA，大部分 SM 在空转，这叫**欠填充（underfill）**。
- **非分区解码注意力内核的欠填充问题**：Qwen3-4B 有 8 个 KV 头，非分区（non-partitioned）解码注意力内核每个「请求 × KV 头」派一个 CTA。批内只有 1 个请求时全 GPU 只有 8 个 CTA——无论上下文多长，SM 都吃不饱。
- **split-KV（Flash-Decoding 式切分）**：把每个请求的 KV 序列切成若干 **chunk**，每个 chunk 由不同 CTA 并行算出局部最大值/求和与部分输出，最后按 online-softmax 顺序归并。切分让「1 个长请求」也能铺满 SM。代价是归并开销，所以 chunk 不能太碎（源码注释：64 token 的 chunk 在 RTX 5090 上最快，32 token 越过归并开销的拐点反而变慢）。
- **NumericPolicy（承接 u3-l6）**：`Tuned` 是默认策略，按活批规模自适应；`Pin` / `PerToken` 是 `--batch-invariant` 的逐位一致策略，任何影响数值归约顺序的量都必须与「同批还排了谁」无关（详见 u8-l1）。
- **CUDA Graph 桶（承接 u3-l4/u3-l5）**：解码按 `BATCH_BUCKETS` 凑整捕获图；本讲会看到每个桶其实捕获了**两条**注意力路径的图。
- **草稿器（承接 u1-l4）**：crate 现在有两种投机草稿器——DFlash 与 EAGLE-3，`launch` 把它们折叠成互斥的单槽 `Option<DraftModel>`。本讲的资格门只问「装没装草稿器」，不关心是哪一种。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/split_kv.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs) | 纯数据模块：`SplitKvConfig` 的 chunk 公式、`split_kv_{chunk}x{max}` 标签与解析的往返 |
| [src/batch_decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs) | 解码缓冲：`SPLIT_KV_*` 常量、每策略的 split-KV 配置、`DecodeAttentionPath` 选择、split 元数据 CSR 的同步与工作区预留 |
| [src/projection_fusion.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs) | QKV 投影融合的资格判定：`DecodeProjectionPath`、`QUALIFIED_QKV` 认证表、五道失败关闭门 |
| [kernel_manifests/qwen3.toml](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/kernel_manifests/qwen3.toml) | kernel-report 清单：`paged_decode_attention` 的 split_kv 变体扫描（本讲用作公式的对照物，工具链详见 u8-l3） |
| src/weights/load.rs | 资格判定的调用点：装载模型时用 `ModelRuntimeConfig.draft_enabled` 等输入解析投影路径 |
| src/executor.rs | `draft_enabled` 的源头：`draft.is_some()`（DFlash 或 EAGLE-3 皆置位） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**SplitKvConfig**（公式与标签）、**DecodeAttentionPath / SPLIT_KV_\* 常量**（运行时选路与缓冲）、**DecodeProjectionPath**（QKV 融合资格）。前两个关于「注意力怎么算」，第三个关于「投影 GEMM 怎么算」，合起来就是解码步里除 MLP 外的全部路径决策。

### 4.1 SplitKvConfig：chunk 公式与标签

#### 4.1.1 概念说明

split-KV 需要回答两个问题：每个 chunk 多少 token（**chunk 尺寸**），以及一个请求最多允许切成几块（**chunk 上限**）。chunk 尺寸太小 → 归并开销占上风；chunk 上限太小 → 长上下文请求即使切成上限块数，每块还是太长，并行度上不去。

`SplitKvConfig` 把这两个量打包成一个可复制的小配置，并让「chunk 尺寸」随请求长度自适应：短请求用固定的 chunk 下限，长请求把 chunk 放大，保证块数不超过上限。它还提供一个与 kernel-report 清单互逆的字符串标签，使「生产用的配置」和「扫参用的变体名」是同一套语言。

#### 4.1.2 核心流程

设 chunk 下限为 \( c \)（`chunk_tokens`），块数上限为 \( m \)（`max_chunks_per_request`），请求的 KV 长度为 \( L \)，则：

\[
\text{chunk}(L) = \max\left(c,\ \left\lceil \frac{L}{m} \right\rceil\right),\qquad
\text{chunks}(L) = \max\left(1,\ \left\lceil \frac{L}{\text{chunk}(L)} \right\rceil\right)
\]

- 第一项 \( c \)：**不要切太碎**——块数没到上限时永远用下限尺寸，避开归并开销拐点。
- 第二项 \( \lceil L/m \rceil \)：**不要超上限**——把 chunk 放大到恰好 \( m \) 块装下。
- 两项取 max 后，实际块数必然 \( \le m \)，工作区可以按 \( m \) 预留。

标签是 `split_kv_{chunk}x{max}`，例如 `split_kv_64x64` 即 \( c=64, m=64 \)。`parse` 是 `label` 的逆，遇到畸形输入（缺 `x`、非数字、任一字段为 0）返回 `None`；构造函数 `new` 对零值直接 `assert!`，把除零错误拦在构造期而不是解码中途。

#### 4.1.3 源码精读

结构体就两个字段——一个 chunk 下限，一个块数上限：

[src/split_kv.rs:8-12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L8-L12) 定义 `SplitKvConfig { chunk_tokens, max_chunks_per_request }`。

[src/split_kv.rs:15-27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L15-L27) 构造函数拒绝零值：注释点明零 `max_chunks_per_request` 会在 `actual_chunk_size` 里除零、零 `chunk_tokens` 会在 `kv_len==0` 的 `active_chunks` 里除零，所以在 `new`（const 场下是编译期）就大声失败。

[src/split_kv.rs:29-38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L29-L38) 就是上面的公式本体：`actual_chunk_size` 取「下限」与「按上限凑整」的较大者；`active_chunks` 再算实际块数并对 0 兜底为 1（kv_len=0 的空视图只出现在 padding 场景）。

[src/split_kv.rs:40-58](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L40-L58) `label`/`parse` 往返：`parse` 先剥 `split_kv_` 前缀、按 `x` 切两半、各自解析成 `usize`，零值拒绝后重新走 `new` 的校验。

[manifest 清单](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/kernel_manifests/qwen3.toml#L3-L8) 里 `paged_decode_attention` 的变体是 `["non_partition", "split_kv_64x64", "split_kv_256x64", "split_kv_512x64"]`——扫参工具用 `SplitKvConfig::parse` 把这三个标签还原成配置去实测（见 [src/bin/report_support/decode_attention.rs:34-38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/bin/report_support/decode_attention.rs#L34-L38)：报告的默认 split 宽度直接取运行时常量 `SPLIT_KV_CHUNK_TOKENS × SPLIT_KV_TUNED_MAX_CHUNKS`，刻意不重抄一份，也不故意用 Pin 宽度）。

模块自带两个纯 CPU 单测：[label/parse 往返](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L65-L75) 与 [畸形标签拒绝](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/split_kv.rs#L77-L84)。

#### 4.1.4 代码实践

1. **实践目标**：把公式吃透——对清单扫的全部 kv_len 手推生产 Tuned 配置下的 chunk 尺寸与块数。
2. **操作步骤**：
   - 生产 Tuned 配置是 \( c=64, m=64 \)（依据见 4.2.3 的常量）。对清单 kv_len ∈ {128, 512, 1024, 2048, 4096, 8192, 10000} 逐个计算 \(\text{chunk}(L)\) 与 \(\text{chunks}(L)\)。
   - 运行模块自带单测：`cargo test --release -p pegainfer-qwen3 --lib split_kv`。
3. **需要观察的现象**：测试输出里 `split_kv::tests::label_parse_round_trips` 与 `split_kv::tests::parse_rejects_malformed` 通过。
4. **预期结果**（可先自推再对照）：

| \( L \) | \(\lceil L/64\rceil\) | chunk | chunks |
|---|---|---|---|
| 128 | 2 | 64 | 2 |
| 512 | 8 | 64 | 8 |
| 1024 | 16 | 64 | 16 |
| 2048 | 32 | 64 | 32 |
| 4096 | 64 | 64 | 64 |
| 8192 | 128 | **128** | 64 |
| 10000 | 157 | **157** | 64 |

   关键现象：\( L \le 4096 \) 时 chunk 恒为下限 64；超过 4096 后开始凑整放大，块数被钉死在 64。清单变体 `split_kv_256x64` / `split_kv_512x64` 则把下限抬到 256/512——扫参问的正是「下限 64 是不是各 (batch, kv_len) 组合下的最优解」。
5. 本实践为纯手推 + 已有单测，**无需 GPU**；测试运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`SplitKvConfig::new(256, 64)` 在 kv_len=8192 时的 chunk 与块数是多少？它与 `split_kv_64x64` 谁的块更少？
答案：\(\text{chunk}=\max(256, \lceil 8192/64\rceil=128)=256\)，块数 \(=\lceil 8192/256\rceil=32\)。它块数更少（32 < 64），但每块更长、并行 CTA 更少——这正是清单要实测权衡的两个方向。

**练习 2**：为什么 `parse("split_kv_64x0")` 必须返回 `None`，而不是悄悄纠正成 1？
答案：零 `max_chunks_per_request` 会让 `actual_chunk_size` 在 `kv_len.div_ceil(0)` 处除零。静默纠正等于把用户（或清单）写错的变体解释成另一个没测过的配置；返回 `None` 让 malformed 标签在入口即失败，与 `new` 的 `assert!` 构成同一不变量的两道防线。

**练习 3**：`active_chunks` 末尾的 `.max(1)` 是给谁兜底的？
答案：kv_len=0 的请求（如 padding 槽或极端空视图）：\(\lceil 0/\text{chunk}\rceil=0\)，但下游 CSR 的 indptr 结构要求每个请求至少占一个槽位，`.max(1)` 保证块数下界为 1。

### 4.2 DecodeAttentionPath 与 SPLIT_KV_* 常量

#### 4.2.1 概念说明

公式有了，接下来是两个运行时问题：**这一步走哪条注意力路径**（`DecodeAttentionPath`），以及**缓冲区按什么上限预留**（`SPLIT_KV_*` 常量族）。核心事实是：路径由**批宽**决定而非上下文长度——split-KV 解决的是「批太小时 CTA 不够」，批一旦够大，非分区内核每个 (请求 × KV 头) 都有 CTA，反而是 split 的归并开销纯亏。

数值策略在这里分岔：Tuned 允许路径随批宽切换、chunk 尺寸随活批的最大 kv_len 浮动；Pin/PerToken 要求同请求在任何批里算出逐位一致的结果，于是把 chunk 尺寸钉死在「只依赖请求自身长度」的函数上，并放弃非分区路径——两条内核的归约顺序不同，切换即破坏不变性。

#### 4.2.2 核心流程

```
每步解码（sync_paged_meta / sync_split_kv_meta）：
  1. 收集各请求 KvView → 更新 max_seq_len = 批内最大 kv_len
  2. 选择路径 attention_path(padded_bs, policy)：
       Pin/PerToken → SplitKv（无条件）
       Tuned        → padded_bs <= 32 ? SplitKv : NonPartition
  3. 计算 chunk 尺寸 split_chunk_size()：
       Tuned  → split_chunk_size_for(max_seq_len)   # 64x64 配置，随活批浮动
       Pin    → pin_chunk_size(max_context_tokens)  # 64x256 配置，只看模型上下文上限
  4. build_split_kv_csr(chunk, cap, kv_lens, padded_bs) → 上传 5 个 CSR 数组
     （Tuned 且 padded_bs > 32 时跳过：反正走 NonPartition）
```

构造期一次性预留工作区：`max_split_slots = split_batch_cap × m`，其中 Tuned 的 `split_batch_cap = min(bs, 32)`（非分区路径不用 split 工作区），Pin/PerToken 为 `bs`（每个桶都可能走 SplitKv）。

#### 4.2.3 源码精读

**常量族**。[src/batch_decode_buffers.rs:33-44](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L33-L44)：上方注释是本模块的经验依据——非分区内核每 (请求 × kv头) 一个 CTA，bs=1 时 8 个 CTA 在任何 seq_len 下都欠填充，所以 split-KV 在**整个上下文区间**都赢；批宽大了后（bs≤8 大胜、~bs16 持平、bs32 差距 ~1%）由 `SPLIT_KV_MAX_BATCH_SIZE = 32` 封顶切回非分区；64-token chunk 是实测最快（测量记录在 `docs/models/qwen3/decode-attention.md`）。四个常量：

- `SPLIT_KV_CHUNK_TOKENS = 64`：chunk 下限（两策略共用）；
- `SPLIT_KV_TUNED_MAX_CHUNKS = 64`：Tuned 的块数上限；
- `SPLIT_KV_MAX_CHUNKS_PER_REQUEST = 256`：Pin/PerToken 的块数上限，也是工作区/越界守卫的上界；
- `SPLIT_KV_MAX_BATCH_SIZE = 32`：Tuned 走 SplitKv 的批宽上限。

**策略 → 配置**。[src/batch_decode_buffers.rs:46-63](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L46-L63)：`split_kv_config` 把策略折成 `SplitKvConfig`——Tuned 得 `new(64, 64)`（正是清单里的 `split_kv_64x64`），Pin/PerToken 得 `new(64, 256)`。`split_chunk_size_for(basis)` 是 Tuned 的公开助手（经 [lib.rs 的 runtime 再导出](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L204-L207) 供模型本地基准使用）；`pin_chunk_size(max_context_tokens)` 以模型上下文上限为基准求 Pin 的固定 chunk。

**路径枚举与选路**。[src/batch_decode_buffers.rs:112-125](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L112-L125) 定义两值枚举 `DecodeAttentionPath` 及其图槽位；[src/batch_decode_buffers.rs:454-466](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L454-L466) 的 `attention_path` 实现分叉：Pin/PerToken 直接返回 SplitKv（「钉死每个桶」，注释明说 chunk 数已经是请求本地的 `pin_chunk_size`，永不随同批负载漂移）；Tuned 按批宽比较 `SPLIT_KV_MAX_BATCH_SIZE`。每个 `(桶, 路径)` 对各持一张独立图：[graph_index](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L468-L470) 用 `bucket_idx * 2 + path.graph_slot()` 编址，所以 u3-l5 里每桶一张图的叙事在本模块细化为**每桶两张**。

**chunk 尺寸的两种基准**。[src/batch_decode_buffers.rs:394-401](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L394-L401)：Tuned 用 `self.max_seq_len`（活批最大 kv_len，在 [sync_paged_meta 的循环](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L346-L354) 里每步刷新）；Pin/PerToken 用构造期记下的 `max_context_tokens`（字段注释见 [src/batch_decode_buffers.rs:191-194](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L191-L194)，即 `max_position_embeddings`）。这就是「Tuned 按活批定尺寸、块数随同批漂移；Pin 定死尺寸、块数请求本地」的代码落点。

**元数据同步与工作区**。[src/batch_decode_buffers.rs:410-452](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L410-L452) 的 `sync_split_kv_meta`：先清 `synced_split_kv`（kernel-call-trace 下防复用陈旧元数据），Tuned 超过批宽上限直接早退（[L424-428](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L424-L428)，注释强调构造期快照与活策略由 `batch_decode` 入口断言保持一致，故 CSR 必然装得下）；随后 `build_split_kv_csr` 生成 CSR 并上传 `split_request_indices_d` / `split_kv_tile_indices_d` / `split_kv_chunk_size_d` / `split_o_indptr_d` / `split_block_valid_mask_d` 五个数组。工作区在构造期按 [split_batch_cap × 每请求块上限](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L236-L240) 预留（`split_tmp_v`、`split_tmp_s` 等见 [L283-289](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L283-L289)）。

**策略快照陷阱**。字段 [`policy_at_construction`](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L196-L199) 的注释值得整段读：工作区按构造期策略定尺寸、`attention_path` 按策略选图，事后换策略要么溢出工作区、要么重放过期的 `(桶, 路径)` 图——这是 u8-l1 批不变专题会再遇到的 policy-key 陷阱。

#### 4.2.4 代码实践

1. **实践目标**：给定 max_context，推导 Pin 与 Tuned 的 chunk 尺寸，并验证 CSR 构建的纯函数行为。
2. **操作步骤**：
   - 取 Qwen3-4B 的 `max_position_embeddings = 32768`。Pin 配置为 `new(64, 256)`：\(\text{chunk} = \max(64, \lceil 32768/256 \rceil = 128) = 128\)，且**每步固定 128**，与活批无关。Tuned 场景取活批最大 kv_len=8192：\(\text{chunk} = \max(64, 128) = 128\)——注意数值恰好相同，但语义完全不同：Tuned 的 128 随活批浮动（批里最长请求变了它就变），Pin 的 128 只由模型上下文上限决定。再验证守卫：\( 128 \times 256 = 32768 \)，即 Pin 公式保证块数不超上限时恰好覆盖整个上下文窗口。
   - 运行三个纯主机 CSR 单测：`cargo test --release -p pegainfer-qwen3 --lib uniform_batch_csr padding_requests_contribute_zero_chunks errors_when_a_request_exceeds_the_cap`。
3. **需要观察的现象**：三个 CSR 测试通过；注意同模块的 `shared_prefix_views_are_counted_by_reference` **需要 CUDA context**，无 GPU 环境不要选它。
4. **预期结果**：
   - `uniform_batch_csr`：kv_len=100、chunk=64 → 每请求 2 块（\(\lceil 100/64\rceil=2\)），`o_indptr = [0, 2, 4]`，`kv_tile_indices = [0,1,0,1]`（[断言](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L526-L534)）。
   - `errors_when_a_request_exceeds_the_cap`：chunk=64、cap=4 时 kv_len=1000 需 \(\lceil 1000/64\rceil=16\) 块 > 4，返回 Err（[L546-549](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L546-L549)）——这是「凑整保证不超上限」不变量被违反时的响亮失败。
5. 测试运行结果待本地验证（编译需 CUDA 工具链，但运行这三个用例不需要 GPU 设备）。

#### 4.2.5 小练习与答案

**练习 1**：Tuned 下 padded_bs=40 走哪条路径？它还需要上传 split CSR 元数据吗？
答案：`attention_path` 中 40 > `SPLIT_KV_MAX_BATCH_SIZE`(32) → NonPartition；`sync_split_kv_meta` 对「Tuned 且 padded_bs > 32」早退，不再上传 split 元数据（u3-l4 的 bucket_for 会先把 40 凑整到桶 40）。

**练习 2**：为什么 Pin/PerToken 的工作区要按**全部** max_batch 预留，而 Tuned 只按 min(bs, 32)？
答案：Pin/PerToken 对**每个**桶都钉死 SplitKv（`attention_path` 无条件返回 SplitKv），任何批宽都可能用 split 工作区；Tuned 超过 32 必走 NonPartition，split 工作区最多服务到 32 宽的批（[L236-240](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L236-L240) 的 `split_batch_cap`）。

**练习 3**：Tuned 的 chunk 尺寸依赖「同批最长请求的 kv_len」。举一个它破坏逐位一致性的具体场景。
答案：请求 A 单独成批（kv_len=2048）时 chunk=64、块数=32；当 kv_len=8192 的请求 B 同批时 max_seq_len=8192、chunk=128，A 的块数变成 \(\lceil 2048/128\rceil=16\)。online-softmax 的归约（rescale）顺序由块数决定——同一请求换个邻居结果就可能在最后一位漂移。Pin 把 chunk 钉死成只依赖 `max_context_tokens` 的常量，正是为了切断这条耦合（批内不变性的完整图景见 u8-l1）。

### 4.3 DecodeProjectionPath：QKV 融合的资格门

#### 4.3.1 概念说明

u2-l2 讲过权重布局：Q/K/V 在显存里本来就合存成单个 `qkv_proj` 矩阵。但「权重合存」不等于「计算融合」——解码时可以把它当**一个** GEMM（M = q_dim + 2·kv_dim）算，也可以按行偏移切成**三个** GEMM（q、k、v 各一个）。`DecodeProjectionPath` 就是这个二选一：`FusedQkv`（一发 GEMM）或 `Split`（三发）。

直觉上融合应该更快（少两次 kernel launch、单次遍历权重）。但这个直觉只在**特定设备架构 × 特定模型几何**上被证据支持：融合后的 M 维改变了 GEMM 的 tile 量化与 wave 数，可能更优也可能更差。于是本模块采用**失败关闭**：默认 Split，只有当运行环境与认证表完全匹配时才开融合；任何一项不符（TP、非 Tuned 策略、重叠流、装了草稿器、未认证的 SM/几何）都退回 Split 并给出理由标签。

本次提交（af9e40e1）落在此处：第四道门的参数从 `dflash_enabled` 改名 `draft_enabled`，理由标签从 `"dflash"` 改为 `"speculative"`——随着 EAGLE-3 草稿器落地，这道门从「装了 DFlash 就退」推广为「装了任一草稿器（DFlash 或 EAGLE-3）就退」。

#### 4.3.2 核心流程

```
select_decode_projection_path(tp, policy, overlap, draft_enabled, sm, hidden, q_dim, kv_dim, layers)
  ├─ tp.world_size != 1            → Split, "tensor_parallel"
  ├─ policy != Tuned               → Split, "numeric_policy"
  ├─ overlap != Off                → Split, "decode_overlap"
  ├─ draft_enabled                 → Split, "speculative"     ← 本次改名推广
  ├─ (sm,…) 不在 QUALIFIED_QKV     → Split, "unqualified_sm_or_geometry"
  └─ 全部通过                       → FusedQkv, "qualified_sm_and_geometry"
```

判定发生在**模型装载期**（一次性，不是每步）：`executor.rs` 把草稿器折成 `draft.is_some()` 塞进 `ModelRuntimeConfig.draft_enabled`，`weights/load.rs` 装载时调用选择函数，结果作为 `decode_projection_path` 存进 `Qwen3Model`，并决定 `BatchDecodeBuffers` 是否分配 `qkv_out` 融合缓冲。

#### 4.3.3 源码精读

**枚举与唯一消费者**。[src/projection_fusion.rs:6-20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L6-L20)：模块文档明说只有 QKV 融合上了生产资格，gate/up 仍走既有 split-GEMM 路径，「直到它自己的组件级与端到端证据各自达标」。`fuses_qkv()` 是唯一的布尔出口，最终被 [weights.rs:414-416](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/weights.rs#L414-L416) 的 `fused_decode_qkv()` 转述，再到 [batch_decode.rs:327-341](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode.rs#L327-L341) 决定走融合 GEMM 还是三发。

**认证表及其哲学**。[src/projection_fusion.rs:22-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L22-L30)：`QUALIFIED_QKV` 是五元组 `(SM, hidden_size, q_dim, kv_dim, num_hidden_layers)` 的白名单，当前仅两行：

| 行 | SM | hidden | q_dim | kv_dim | layers | 对应 |
|---|---|---|---|---|---|---|
| 1 | 89 | 2560 | 4096 | 1024 | 36 | SM 8.9（Ada，如 RTX 4090）× Qwen3-4B 几何 |
| 2 | 89 | 4096 | 4096 | 1024 | 36 | SM 8.9 × Qwen3-8B 几何 |

上方注释是本模块的灵魂：**cuBLASLt 只在单一 GEMM 形状内部选算法，它不裁决「打包拓扑是否胜过三个分离 GEMM」**——所以每加一行都需要该设备架构与模型几何的组件级 + 端到端 A/B 证据。这就是「按 SM + 几何逐行认证」而非「按架构族放行」的原因。

**五道门与理由标签**。[src/projection_fusion.rs:32-59](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L32-L59)：依次是 TP（世界尺寸 >1 → Split，融合形状没有按 TP 行切认证过）、数值策略（非 Tuned → Split，资格证据只在 Tuned 工况取得）、decode 重叠（开重叠 → Split，u3-l7 的双流/GreenCtx 改变了执行时间线）、草稿器（[L52-54](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L52-L54)：`draft_enabled` → Split，标签 `"speculative"`）、认证表（不匹配 → Split）。全过才返回 `FusedQkv`。顺序即报错优先级，与 u1-l3 的互斥块序同一风格。

**改名的两端**。上游：[executor.rs:1337-1349](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1337-L1349) 构造 `ModelRuntimeConfig` 时写 `draft_enabled: draft.is_some()`——`draft` 是 `Option<DraftModel>`，DFlash 与 EAGLE-3 都会置位；字段定义在 [weights/load.rs:31-40](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/weights/load.rs#L31-L40)（u2-l3 已讲过这第七旋钮的改名）。下游：[weights/load.rs:107-125](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/weights/load.rs#L107-L125) 装载期调用选择函数并把 `resolved=… reason=… sm=… hidden=…` 打进日志——排查「为什么没融合」时看这行日志即可。

**两个单测钉住行为**。[enables_qualified_qwen3_tp1_decode](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L65-L81) 证明唯一合格组合返回 `FusedQkv`；[fails_closed_outside_qualified_runtime_environment](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L83-L184) 枚举八种非法组合（TP=2、Pin、PerToken、SharedSm、GreenCtx、**draft_enabled=true**、SM 86、hidden 3072）逐一断言退回 Split——第六个用例正是本次改名的直接受益者：现在它语义上是「装了任一草稿器」，不再仅指 DFlash。

#### 4.3.4 代码实践

1. **实践目标**：写一段对 `QUALIFIED_QKV` 的分析——为何 cuBLASLt 会选算法不等于融合更优；并说明 `dflash_enabled` → `draft_enabled` 后理由标签如何变化。
2. **操作步骤**：
   - 读 [src/projection_fusion.rs:22-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/projection_fusion.rs#L22-L30) 的注释与两行表，写一段分析，要点需覆盖：(a) cuBLASLt 的启发式在**给定 GEMM 形状**内挑算法，输入是这一个形状的 (M, N, K)；(b) 融合改变的是形状本身（M 从 q_dim 或 kv_dim 变成 q_dim+2·kv_dim），tile 量化、wave 数、L2 命中随 M 改变，三个小 GEMM 与一个大 GEMM 的胜负是**拓扑问题**，启发式无法回答；(c) 因此证据必须在目标 SM + 目标几何上组件级（GEMM 计时）与端到端（服务吞吐/时延）各测一次，表只能逐行加，不能按架构族泛化。
   - 用 `git log -L` 或 `git diff 785d0487..af9e40e1 -- src/projection_fusion.rs` 查看本次改动，确认标签从 `"dflash"` 到 `"speculative"`、参数从 `dflash_enabled` 到 `draft_enabled`；再沿 `executor.rs` 的 `draft.is_some()` → `ModelRuntimeConfig.draft_enabled` → `select_decode_projection_path` 追一遍数据流。
   - 运行：`cargo test --release -p pegainfer-qwen3 --lib projection_fusion`。
3. **需要观察的现象**：两个测试通过；`fails_closed…` 的第六个用例（`draft_enabled=true`）断言路径为 Split——在旧代码里它测的是「装了 DFlash」，在当前 HEAD 它同时覆盖「装了 EAGLE-3」。
4. **预期结果**：融合只在「TP=1 + Tuned + Off 重叠 + 无草稿器 + SM89 + 表内几何」成立；装载日志会出现 `reason=qualified_sm_and_geometry` 或五类拒绝理由之一。理由标签变化的实际效果：此前装 DFlash 的服务日志会打 `reason=dflash`，现在任何草稿器（DFlash 或 EAGLE-3）都打 `reason=speculative`——日志不再绑定具体草稿器型号。测试运行结果待本地验证。
5. 本实践为源码阅读 + 纯 CPU 单测，**无需 GPU**。

#### 4.3.5 小练习与答案

**练习 1**：在 SM 120（Blackwell）上跑 Qwen3-4B（hidden 2560），即使其他条件全满足，路径是什么？理由标签？
答案：Split，`"unqualified_sm_or_geometry"`。表里只有 SM 89 两行；SM 120 未取得组件级 + 端到端证据，失败关闭。

**练习 2**：为什么「装了草稿器」要单独设一道门，而不是指望认证表的几何不匹配兜底？
答案：草稿器不改变模型几何（表里的五元组照旧匹配），它改变的是**运行时工况**：投机服务里解码步的批构成与时间线（verify 行混入、草稿车道在同一线程上交错）落在资格证据覆盖的范围之外。几何表只能守住「设备 × 模型」，工况必须另设门。改名 `draft_enabled` 正是把这道门从「DFlash 这一种工况」推广为「任一草稿器的工况」。

**练习 3**：这个判定为什么放在装载期（一次性）而不是每个解码步？
答案：输入全部是启动期常量（TP、策略、重叠模式、是否装草稿器、SM、几何），没有逐步变化的量；结果只需要决定一次 `qkv_out` 融合缓冲是否分配（`BatchDecodeBuffers::new` 的 `fused_decode_qkv` 参数）与解码前向走哪条 GEMM。放每步只会白付一次表查询，还引入同请求内路径漂移的可能——这恰是 CUDA Graph 指针稳定性（u3-l5）不允许的。

## 5. 综合实践

把三个模块串成一张「Qwen3-4B 解码步路径决策表」。设定：Qwen3-4B（hidden 2560、q_dim 4096、kv_dim 1024、36 层、`max_position_embeddings` 32768），SM 89 显卡，CUDA Graph 开启。

对下面四种工况，推导 (a) 注意力路径与 chunk 尺寸、(b) 投影路径与理由标签：

| 工况 | 批宽（凑整后） / 活批最大 kv_len | 策略 / 草稿器 |
|---|---|---|
| A | 20 / 4096 | Tuned / 无 |
| B | 40 / 4096 | Tuned / 无 |
| C | 1 / 8192 | Pin（--batch-invariant）/ 无 |
| D | 8 / 4096 | Tuned / EAGLE-3 |

完成后对照参考答案，再用两条命令验证纯函数层（`cargo test --release -p pegainfer-qwen3 --lib split_kv projection_fusion`；CSR 三测见 4.2.4），有 GPU 的机器可再跑一次服务启动，在装载日志里核对 `decode projection path: resolved=… reason=…` 与你的推导一致。

**参考答案**：

- **A**：批 20 ≤ 32 → SplitKv；Tuned chunk = max(64, ⌈4096/64⌉=64) = **64**，块数 64；TP=1 + Tuned + 无重叠 + 无草稿器 + 表行 1 命中 → **FusedQkv**，`qualified_sm_and_geometry`。
- **B**：批 40 > 32 → **NonPartition**（Tuned 早退，不上传 split CSR）；投影同 A → **FusedQkv**。
- **C**：Pin 无条件 → **SplitKv**；chunk = max(64, ⌈32768/256⌉=128) = **128**，每步固定（该请求块数 ⌈8192/128⌉=64，与邻居无关）；策略非 Tuned → **Split**，`numeric_policy`。
- **D**：批 8 ≤ 32 → SplitKv，chunk 64；装了 EAGLE-3 → `draft.is_some()` 置位 → **Split**，`speculative`（旧代码此处会打 `dflash` 且仅由 DFlash 触发——这就是本次提交在本讲范围的全部行为差异）。

## 6. 本讲小结

- `SplitKvConfig` 用 \(\text{chunk}(L)=\max(c,\lceil L/m\rceil)\) 同时钳住两个方向：下限 \(c\) 防切得太碎（归并开销），上限 \(m\) 防块数失控（工作区与守卫）；`split_kv_{chunk}x{max}` 标签与 kernel-report 清单变体互逆，生产 Tuned 配置就是 `split_kv_64x64`。
- `DecodeAttentionPath` 的分叉依据是**批宽**（CTA 数 vs SM 数），不是上下文长度：Tuned 在 `padded_bs ≤ 32` 走 SplitKv、之上走 NonPartition；Pin/PerToken 对每个桶钉死 SplitKv，chunk 尺寸只依赖 `max_position_embeddings`，从而块数请求本地、批内不变。
- 每个 `(桶, 路径)` 对各持一张独立 CUDA Graph（每桶两张）；split-KV 工作区按「批宽帽 × 块数上限」在构造期一次预留，`policy_at_construction` 快照防住事后换策略的键陷阱。
- QKV 融合是失败关闭的：TP、非 Tuned、重叠流、任一草稿器、SM/几何不在 `QUALIFIED_QKV` 表内——任一命中即退 Split 并报理由标签；表逐行认证，因为 cuBLASLt 只在单一形状内选算法，不裁决「一个融合 GEMM vs 三个分离 GEMM」的拓扑胜负。
- 本次提交（af9e40e1）把第四道门从 `dflash_enabled`/`"dflash"` 推广为 `draft_enabled`/`"speculative"`：数据流是 `executor.rs` 的 `draft.is_some()`（DFlash 或 EAGLE-3 皆置位）→ `ModelRuntimeConfig.draft_enabled` → 装载期一次判定。

## 7. 下一步学习建议

- **u6-l1（投机解码事务核心）**：本讲只到「装了草稿器就退 Split」这道门为止；草稿器在解码循环里如何提出/验证/接受，去 u6 系列看事务模型，再到 u6-l8 看 EAGLE-3 车道如何触发本讲的 `"speculative"` 标签。
- **u8-l1（batch-invariant）**：本讲反复出现的 Pin 策略、`policy_at_construction` 键陷阱、请求本地网格，在 u8-l1 有完整闭环（含 `warmup_decode_projection_pins` 的 pin 预热）。
- **u8-l3（kernel-report 工具链）**：想知道 `split_kv_64x64/256x64/512x64` 这组变体是怎么被逐 (batch × kv_len) 实测并 CUPTI 计时的，读清单驱动的扫参工具链。
- 源码延伸：`docs/models/qwen3/decode-attention.md`（`SPLIT_KV_*` 常量注释引用的测量记录）与 `pegainfer_core::ops::build_split_kv_csr`（CSR 的具体构造与越界守卫）。
