# DSA 索引器：2048 稀疏 top-k 与跨层共享

> 学习路线：单元 4「注意力子系统」第 4 讲。前置：u4-l1（MLA 前半）、u4-l2（KV 页布局）、u4-l3（MLA 解码后半）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 逐步复述 DSA indexer 一步 decode 的完整数据流：q/k 投影 → 带偏置 LayerNorm → RoPE → fp8 逐组量化 → 权重折叠 → DeepGEMM paged MQA logits → 确定性 top-2048 → 全局槽位换算，并说出每一步的源码位置。
2. 说出 full / shared 两类索引层的判别谓词 `glm52_layer_has_full_indexer`、21 层的分布规律，并解释 top-k 为什么能跨层复用——**索引是全局 KV 槽位，所有层共用同一张 block table**。
3. 解释 index-K 缓存的页内布局（每 token 132 B = 128 B fp8 + 4 B f32 scale）、写入 kernel 与 DeepGEMM 读取侧如何共用同一缓冲。

## 2. 前置知识

- **DSA / 稀疏注意力**：u4-l3 讲过，GLM5.2 的 MLA 解码不对全部上下文做注意力，而是先挑出「最重要的少量 token」再算。挑哪些 token，就是本讲 indexer 的职责：它为每个查询 token 选出至多 2048 个 KV 槽位，FlashMLA 稀疏解码只在这些槽位上做注意力。
- **lightning indexer（闪电索引器）**：DeepSeek-V3.2 引入的轻量打分器——32 个查询头、head_dim=128 的小型 MQA，用 fp8 低精度算出查询与每个已缓存 token 的相关性分数。GLM5.2（config 里 `model_type=glm_moe_dsa`）沿用这套设计，本 crate 的实现逐 kernel 对齐 vLLM 的 `DeepseekV32Indexer`（生产参考实现），对照过程记录在团队活文档 `docs/models/glm52/indexer-forward.md`（本会话工具权限读不到该文件，读者请自行打开对照，本文不转述其内容）。
- **MQA（Multi-Query Attention）**：多个查询头共享一个 KV 头。indexer 的 K 每层只有一个 128 维头，这就是它便宜的原因——打分比注意力本身轻一两个数量级。
- **逐组（per-token-group）fp8 动态量化**：u3-l1 讲过块量化；indexer 用「每 token 每组一个 f32 scale」的动态量化，一个组恰好是一个索引头的 128 维（源码注释：`quant per 128-group (one group per head)`），量化 kernel 与 MoE dispatch 同源（形状描述 `Glm52MoeQuantShape`）。
- **全局槽位（global slot）**：u4-l2 讲过 KV 页池——rank 级一块 slab，按 64 token 一页寻址。「槽位」就是 token 在页池中的全局线性位置；稀疏解码拿到槽位号才知道去哪读 KV。
- 术语提醒：本讲的 top-2048 是**注意力稀疏度**，与 u5 将讲的 MoE top-8 专家路由无关，两者恰好都叫 top-k。

## 3. 本讲源码地图

| 文件 | 本讲视角下的角色 |
| --- | --- |
| `src/indexer.rs`（1027 行） | indexer 本体：权重结构、持久 scratch、decode 前向 `glm52_indexer_forward_into`（L408-L616）、prefill 分块 scratch（L663 起）、cfg(test) 的 oracle 便捷封装 |
| `src/layer.rs`（422 行） | decoder 层组装：`Glm52LayerIndexer` 枚举、top-k 跨层 carry（`s.idx.global_slots`）与 AUX 流并行 |
| `src/config.rs`（305 行） | 索引几何常量、`glm52_layer_has_full_indexer` 谓词、probe 对 indexer 字段的逐项钉死 |
| `src/model/build.rs` | 权重装载：按谓词构造 `Full(...)` 或 `Shared` |
| `src/model/mod.rs` | KV 页布局 `glm52_page_layout`（index-K 切片偏移）、页字节数常量与 `cache_layout_tests` 单测 |
| `src/weights.rs` | checkpoint 侧：只有拥有 indexer 的层才被枚举 indexer 张量名 |

## 4. 核心概念与源码讲解

### 4.1 indexer 前向 kernel 链（decode 路径）

#### 4.1.1 概念说明

稀疏注意力有个「先有鸡还是先有蛋」的问题：要知道哪些 token 值得注意，得先给所有 token 打分；而打分如果和注意力一样贵，稀疏就失去了意义。indexer 的答案是**用一个便宜一两个数量级的打分器**：查询侧把 MLA 的 2048 维低秩中间量 `q_resid`（u4-l1）投到 32×128 的小空间，键侧每层只缓存一个 128 维 fp8 向量，再用 DeepGEMM 的 paged MQA kernel 一次对全上下文打分、取 top-2048。「打分足够便宜，才能先全量打分、再挑重点」，这就是 DSA（DeepSeek Sparse Attention）的工作方式。

`src/indexer.rs` 的文件头注释就是官方数据流图，建议先读它再看代码：

- [src/indexer.rs:L1-L30](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L1-L30)——文件头：声明本实现对齐 vLLM `DeepseekV32Indexer`，并画出从 `q_resid`/`hidden` 两条输入到 `topk_indices[T, topk]` 的完整链路（4.1.2 复述）。

几何常量（全部来自 config，编译期钉死）：

- [src/config.rs:L52-L57](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L52-L57)——`GLM52_INDEX_TOPK = 2048`（稀疏度）、`GLM52_INDEX_TOPK_FREQ = 4`（full 层频率）、`GLM52_INDEX_HEAD_DIM = 128`、`GLM52_INDEX_HEADS = 32`、`GLM52_INDEX_SKIP_TOPK_OFFSET = 3`（前几层跳过共享的偏置）。

两个派生 scale（vLLM 同款）：

- [src/indexer.rs:L77-L85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L77-L85)——`SOFTMAX_SCALE = 128^{-0.5} ≈ 0.08838835`、`N_HEADS_SCALE = 32^{-0.5} ≈ 0.1767767`、`K_NORM_EPS = 1e-6`。

#### 4.1.2 核心流程

一步 decode（批内 T 行，每行一个查询 token）的执行序列：

```text
q_resid[T,2048]（MLA 前半已产出，见 u4-l1）
  ├─ wq_b（fp8 线性）──────────► q[T,32,128]
normed[T,6144]（本层 input_layernorm 输出）
  ├─ wk（fp8 线性）→ k_raw[T,128] → LayerNorm(eps=1e-6，带 bias)
  │      → RoPE（q[:64]/k[:64]，cos/sin 宽 [32]）
  │      → k 量化 + 写 index-K 缓存（按 slot_mapping 落位）
  ├─ weights_proj（bf16 GEMV）→ weights[T,32]
q 链：q → RoPE → 逐组 fp8 量化 → q_fp8[T,32*128], q_scale[T,32]
  ▼
DeepGEMM paged MQA logits（融合逐头 ReLU×权重，输出 bf16，按 block_table 分页读缓存）
  ▼ bf16→f32
FlashInfer 确定性 top-k（K = topk ≤ 2048；短上下文时等价于全选）
  ▼
局部上下文偏移 → 全局 KV 槽位（查 block_table）→ s.global_slots[T, topk]（i32，-1 补齐）
```

打分的数学（查询 token \(t\)、缓存 token \(v\)，对 32 个索引头求和）：

\[ \mathrm{logit}(t,v) = \sum_{h=0}^{31} w^{\mathrm{fold}}_{t,h}\cdot \max\big(\langle q_{t,h},\, k_v\rangle,\, 0\big), \qquad w^{\mathrm{fold}}_{t,h} = w_{t,h}\cdot s^{q}_{t,h}\cdot \frac{1}{\sqrt{128}}\cdot\frac{1}{\sqrt{32}} \]

其中 \(s^{q}_{t,h}\) 是 q 的逐组量化 scale，\(w_{t,h}\) 是 `weights_proj` 算出的每头重要性权重；逐头 ReLU 由 logits kernel 融合完成。把全部标度折叠进 \(w^{\mathrm{fold}}\)，logits kernel 内就只剩一次乘加——GPU 上常见的「把标度折进权重」手法，也是保证与 vLLM 逐位对齐的乘法顺序（注释原话：bit-identical multiply order）。

#### 4.1.3 源码精读

**权重结构**（每 full 层一份；全套 21 层约 0.18 GB）：

- [src/indexer.rs:L87-L94](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L87-L94)——`Glm52IndexerLayerWeights` 五个字段：`wq_b` fp8 `[32*128, 2048]`、`wk` fp8 `[128, 6144]`、`weights_proj` bf16 `[32, 6144]`、`k_norm_w`/`k_norm_b` `[128]`（f32——FlashInfer LayerNorm 要求 f32 gamma/beta）。
- [src/indexer.rs:L161-L197](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L161-L197)——`from_device`：生产装载路径，fp8 投影与 bf16 weights_proj 以已驻留张量移入；两个 k_norm 以宿主 bf16 字节进、经 [L200-L217](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L200-L217) 的 `upcast_bf16_to_f32` 上传成 f32。

**持久 scratch**（CUDA Graph 指针稳定性的前提，u6-l3 展开）：

- [src/indexer.rs:L272-L300](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L272-L300)——`Glm52IndexerScratch`：全部中间量常驻，尺寸只随 `shape.batch_size`（行容量）变化；`global_slots` 的注释点明它**兼任跨层 top-k carry**（4.2 展开）。`gemv_partial` 被 indexer 独占持有（[L295-L298](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L295-L298)）——indexer 链跑在 AUX 流上与 MLA 前半并行，共享设备级 mma 缓冲会竞态。
- [src/indexer.rs:L303-L307](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L303-L307)——`new`：行数超过 `FP8_GEMV_MAX_ROWS`（#812 的宽 bucket）时改走大矩阵 fp8 GEMM。
- [src/indexer.rs:L350-L356](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L350-L356)——`set_num_kv_blocks`：池块数在实测 KV 填充（u2-l4）之后才定，scratch 建早了在此重绑；注释解释为何只需重绑——所有缓冲按 batch/stride 计尺寸、从不按块数。
- [src/indexer.rs:L358-L381](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L358-L381)——`paged_mqa_shape`：DeepGEMM 形状的单一事实来源；`logits_stride = max_model_len` 向上取 256 的倍数。

**前向主体 `glm52_indexer_forward_into`**（L408-L616）：

- [src/indexer.rs:L384-L406](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L384-L406)——文档：输出是 `topk_indices[T, topk]`（i32，短上下文以 -1 补齐）；`topk` 取自 attend 计划、范围 1..=2048 在编译期与注意力侧钉死一致；短上下文档位选 top-`topk` 与 top-2048 是同一结果（都是「全选」），正是调用方分层 graph 保证的区间。
- [src/indexer.rs:L423-L437](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L423-L437)——scratch 被全部层共享：本次调用的 `index_k_offset`（该层 index-K 切片在页内的字节偏移）随调用传入、重绑进 shape 与 cache 布局——「形状/布局失配不可表示」。
- [src/indexer.rs:L439-L460](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L439-L460)——投影段：行少走 GEMV（`fp8_linear_into`，带独占 mma partial），行多走大 GEMM（`fp8_linear_large_m_into`）。注意输入分工：`wq_b` 吃 MLA 的 `q_resid`，`wk` 吃本层 `input_layernorm` 的输出 `normed`。
- [src/indexer.rs:L461-L493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L461-L493)——`weights_proj`：bf16 输入输出（transformers 侧经 `_keep_in_fp32_modules` 视为 fp32 语义、checkpoint 存 bf16），大/小行数两条 kernel 路线，输出 `[T,32]` 行主序——正是折叠 kernel 要吃的布局。
- [src/indexer.rs:L494-L507](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L494-L507)——k 侧 LayerNorm（eps=1e-6、带 bias、每行一个 CTA）与 RoPE（每头前 64 维，cos/sin 宽 [32]）。注意：config 把 `indexer_rope_interleave` 钉为 true（HF 字段名），而 crate 注释以 half-split（非交织）描述 kernel 实现，并强调 MLA 表与 indexer 表约定不同（[src/layer.rs:L113-L116](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L113-L116)）；两种表述的换算发生在 kernel 与表格构造侧，本讲不展开。
- [src/indexer.rs:L509-L534](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L509-L534)——q 逐组 fp8 量化（`rows = t*32, width = 128, group = FP8_BLOCK`）；权重折叠在设备上完成——注释记录迁移原因：旧宿主折叠需要两次 D2H + 一次 H2D，是步内仅有的流同步，**被捕获的 CUDA Graph 不能包含它们**。

- [src/indexer.rs:L548-L584](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L548-L584)——logits 段：先把 `seq_lens` 拷成 `context_lens`、跑 DeepGEMM 调度元数据 kernel，再跑 paged MQA logits；bf16 logits 经 `bf16_bytes_to_f32_into` 转 f32。注释（L582-L583）说明 sm90 kernel 已融合逐头 ReLU（`fmaxf(score,0)*weight`），与 transformers 的 `F.relu(scores)` 对齐，无需额外 kernel。
- [src/indexer.rs:L586-L613](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L586-L613)——收尾两步：FlashInfer 确定性 top-k（`top_k` 取 attend 计划值、`max_len` 取 logits_stride）；`glm52_indexer_local_topk_to_slots_launch` 把每行 top-k 的**局部上下文偏移**换算成全局槽位——语义上等价于「查该行 block_table 得物理页号，槽位 = 页号 × 64 + 页内偏移」（kernel 参数即 `block_size` 与 `block_table_cols`），供 FlashMLA 稀疏解码直接寻址（u4-l3 的 `Glm52MlaSchedMetadata::topk()` 就是这里的 `topk` 对端）。

prefill 与测试入口（本讲了解即可）：

- [src/indexer.rs:L663-L680](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L663-L680)——prefill 分块 scratch：每层把分页 index-K 缓存 gather 成紧凑非分页布局，逐请求段跑 DeepGEMM SM100 **非分页** MQA logits kernel（vLLM 的 DSv3.2 indexer prefill kernel），取代了每 16K chunk 约 1.76 s 的 32 行分页子平铺（u5-l4 展开）。
- [src/indexer.rs:L618-L661](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L618-L661)——cfg(test) 的分配式封装 `glm52_indexer_forward`，供 oracle 精度门复放（u10-l1）；另有 [L219-L270](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L219-L270) 的逐 token `glm52_indexer_cache_fill`（同样仅测试路径，生产 prefill 走上面的分块 scratch）。

#### 4.1.4 代码实践

1. **实践目标**：把 T=1（单行 decode）的前向张量形状手推一遍，与 scratch 的尺寸公式互相印证。
2. **操作步骤**：对照 [L408-L422](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L408-L422) 的签名与 [L317-L347](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L317-L347) 的分配代码，填写下表（示例答案已给出一半）：

| 中间量 | 形状（T=1） | dtype |
| --- | --- | --- |
| q / k_raw / k | [1, 32*128] / [1,128] / [1,128] | bf16 |
| weights_bf16 | ? | bf16 |
| q_fp8 / q_scale | ? / [1,32] | u8 / f32 |
| weights_folded | [1,32] | f32 |
| logits / logits_f32 | [1, next_n*logits_stride] | bf16 / f32 |
| topk_offsets / topk_values | [1, topk] | i32 / f32 |
| global_slots | [1, topk] | i32 |

3. **需要观察的现象**：所有缓冲的元素数只含 `t`、`topk`、`logits_stride` 的线性因子，不含 KV 块数——这解释了 `set_num_kv_blocks` 为什么可以事后重绑。
4. **预期结果**：三个填空分别是 weights_bf16 `[1,32]`、q_fp8 `[1, 32*128]`、topk 取 attend 计划值（≤2048）。本实践纯纸面即可完成；有 GPU + checkpoint 时可再跑 oracle 门验证数值（u10-l1，**待本地验证**）。

#### 4.1.5 小练习与答案

1. **`weights_proj` 为什么不像 wq_b/wk 一样走 fp8？** 答：`[32, 6144]` 的小矩阵、每 token 只输出 32 个数；transformers 侧把它当 fp32 敏感模块（`_keep_in_fp32_modules`），这里用 bf16 min-latency GEMV 保住低延迟，量化收益盖不过开销（[L461-L493](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L461-L493) 注释）。
2. **logits_stride 为什么要向上取 256 的倍数？** 答：它既是 logits 的行距又是 top-k kernel 的 `max_len` 参数（[L377](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L377)、[L591](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L591)），对齐让 kernel 访存与循环计数规整。
3. **权重折叠从宿主搬到设备上是为了什么？** 答：旧路径每步两次 D2H + 一次 H2D，是步内唯一的流同步点；CUDA Graph 无法捕获含同步的段，搬到设备后是纯 elementwise，graph 安全（[L523-L534](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L523-L534) 注释）。

### 4.2 topk_carry 跨层共享

#### 4.2.1 概念说明

78 层如果每层都跑一遍完整 indexer，权重与算力都要 ×78。GLM5.2 的做法（layer.rs 注释称之为相对 DSv3.2 的分歧点）是**只让 21 层持有 indexer 权重并计算新 top-k（full 层），其余 57 层逐字复用最近一个 full 层的 `topk_indices`（shared 层）**——每 4 层里 1 个 full 带 3 个 shared（前三层例外，见下）。

复用为什么成立？关键在于**top-k 的产物不是「分数」而是「全局 KV 槽位」**：所有层共用同一张 block table、同一套 slot 映射（KV 页池里每个 token 一个槽位，每层在同一槽位存自己的切片，见 u4-l2）。full 层 L 选出的槽位集合 S，对 L+1、L+2、L+3 同样是有意义的候选集合——它们只需在这些槽位上读**自己层**的 MLA 切片做注意力。换句话说，跨层复用的是「去哪读」这个决定，而不是「读到的值」。

- [src/layer.rs:L1-L23](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L1-L23)——文件头：逐层数学（双 norm 残差布局）+ 跨层共享的设计说明，原话：reuse is sound because the indices are global KV slots and every layer shares one block table / slot mapping。

#### 4.2.2 核心流程

一步 decode 中，第 L 层注意力半场围绕 carry 的时序：

```text
读 s.layer.normed（上一层融合收尾已写好；首层由调用方单独 norm）
  ├─ MLA front_q（主流 ctx）──► q_resid
  ├─ 若 L 是 full：
  │    fork：ctx 记 q_ready 事件 → AUX 流等待
  │    indexer 全链（AUX 流，吃 normed + q_resid）──► 覆写 s.idx.global_slots
  │    AUX 记 topk_ready 事件；carry_ready = true
  ├─ 若 L 是 shared：不跑 indexer，直接要求 carry_ready
  ├─ MLA front_rest（主流 ctx，与 AUX 上的 indexer 并行）
  ├─ join：ctx 等 topk_ready（attend 要读 global_slots 了）
  └─ MLA attend：global_slots 作稀疏索引 + 本层 mla_offset 切片 ──► attn_out
```

要点三条：

1. **carry 就是 `s.idx.global_slots` 这块缓冲**：full 层把它当输出写，后续 shared 层把它当输入读，直到下一个 full 层覆写——零拷贝、零额外分配。
2. **`carry_ready` 守卫陈旧读**：每步必须从 `false` 开始；层 0 恒为 full，所以按序走完整 78 层时 carry 总会先被刷新。守卫防的是「从 shared 层起跑的残缺 walk」静默吃上一步的旧 top-k。
3. **AUX 流并行的等价性**：indexer 只依赖 `normed` 与 `q_resid`，与 front_rest（q_b/kv_a）互不依赖；注释强调两条路线 kernel 完全相同、逐字节一致，fork/join 事件在 graph 捕获时变成图边。

#### 4.2.3 源码精读

**判别谓词与类型**：

- [src/config.rs:L61-L68](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L61-L68)——`glm52_layer_has_full_indexer(layer)`：`layer.saturating_sub(SKIP-1).is_multiple_of(FREQ)`，即 `max(layer-2,0) % 4 == 0`；文档注释直接给出结果集 **{0,1,2} ∪ {6,10,…,74}，共 21 / 78 层**。注意三个 dense MLP 层（0-2）也是 full——dense 指的是 MLP 半场，与注意力半场是否跑 indexer 无关。
- [src/layer.rs:L70-L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L70-L76)——`Glm52LayerIndexer` 两态枚举：`Full(Box<Glm52IndexerLayerWeights>)` 持权重，`Shared` 是**无字段单元变体**——shared 层在类型层面就没有可引用的 indexer 权重。
- [src/layer.rs:L78-L85](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L78-L85)——`Glm52DecoderLayerWeights`：`indexer` 字段就嵌在这个每层结构里，与 `mla`、`mlp` 并列。

**注意力半场中的 fork/carry/join**（`glm52_layer_attention_half`）：

- [src/layer.rs:L162-L187](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L162-L187)——函数文档与签名：carry 活在 `s.idx.global_slots`；`carry_ready` 参数要求调用方每步传全新的 `false`；`aux` 是可选的第二 CUDA 上下文（AUX 流）。
- [src/layer.rs:L199-L212](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L199-L212)——`normed` 的来源：上一层的融合「加+norm」收尾顺带产出，首层由调用方对 embedding 单独 norm；随后跑 `glm52_mla_front_q_into` 产出 `q_resid`（AUX 并行的前提之一）。
- [src/layer.rs:L203-L207](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L203-L207)——注释：full 层 + AUX 时 indexer 链与 MLA front 其余部分并行，kernel 相同、逐字节一致，fork/join 事件捕获后成为 graph 边。
- [src/layer.rs:L214-L218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L214-L218)——`Full` 分支先取本层 `index_k_offset`，没有切片直接报错退出（fail-fast，不静默错算）。

- [src/layer.rs:L219-L240](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L219-L240)——fork 与调用：ctx 记 `q_ready` → AUX 等待 → indexer 前向在 AUX 上跑，实参清楚地显示两路输入：`&s.layer.normed`（wk/weights_proj 吃）与 `&s.mla_front.q_resid`（wq_b 吃），外加 `idx_cos/idx_sin`、slab、`index_k_offset`、`slot_mapping/block_table/seq_lens`、`topk()` 与 scratch。
- [src/layer.rs:L241-L251](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L241-L251)——AUX 记 `topk_ready`、置 `carry_ready = true`；`Shared` 分支则只做反向断言：shared 层**不得**持有 index-K 切片。
- [src/layer.rs:L253-L256](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L253-L256)——carry 守卫：任何 shared 层读到 `carry_ready == false` 即报错退出——拒绝陈旧 top-k 而不是用它。
- [src/layer.rs:L257-L267](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L257-L267)——`glm52_mla_front_rest_into` 在主流继续跑（与 AUX 上的 indexer 并行）；随后主流 `wait(topk_ready)`——注释点明 join 时机：attend 马上要消费 `s.idx.global_slots`。
- [src/layer.rs:L268-L288](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L268-L288)——`glm52_mla_attend_into`：`&s.idx.global_slots` 作为稀疏索引传入，配合本层 `caches.mla_offset` 在同一 slab 上读本层切片——「索引跨层共享、数据各读各层」在调用参数上一目了然。

**装载与不分片**：

- [src/model/build.rs:L111-L133](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L111-L133)——构造点：`if glm52_layer_has_full_indexer(layer)` 才 `take_tensor` 逐个取 k_norm/weights_proj/wq_b/wk 并建 `Full(...)`，否则直接 `Glm52LayerIndexer::Shared`。
- [src/model/build.rs:L73-L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L73-L76)——注意力 TP 下 indexer **不切分**：logits 要在 top-2048 之前对 32 个索引头求和，切分就得跨 rank 归约；21 层全套权重约 0.18 GB，复制是更划算的取舍。
- [src/weights.rs:L424-L433](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L424-L433)——checkpoint 侧入口：只有当 checkpoint 里存在该层的 `…indexer.k_norm.weight` 时才把五个 indexer 张量名推进装载清单——shared 层从头到尾不会发起 indexer 张量的请求。

#### 4.2.4 代码实践（本讲核心实践）

1. **实践目标**：打印 0..78 层的 full/shared 序列，标出 dense 层（0-2）与第一个 MoE full 层；并解释 shared 层没有 indexer 权重时，装载器为什么不可能引用到空指针。
2. **操作步骤**：
   - 手推谓词：对 layer ∈ 0..78 计算 `max(layer-2, 0) % 4 == 0`。也可用一行脚本辅助（**示例代码**，等价复现谓词，非项目代码）：
     ```bash
     python3 -c "print(' '.join(f'{l}:{\"F\" if max(l-2,0)%4==0 else \"s\"}' for l in range(78)))"
     ```
   - 对照源码注释自检：[src/config.rs:L61-L63](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/config.rs#L61-L63) 的文档写明结果集 {0,1,2} ∪ {6,10,…,74}、共 21 层。
   - 跑既有单测佐证（CPU 即可，CLAUDE.md：工作区 lib 单测约 9 秒）：
     ```bash
     cargo test --release -p pegainfer-glm52 --features glm52 --lib cache_layout_tests
     ```
     （**待本地验证**：本讲义写作环境无法执行 cargo。）
3. **需要观察的现象**：序列开头是 `0:F 1:F 2:F 3:s 4:s 5:s 6:F …`；层 3-5 是第一批 MoE 层但都是 shared；第 6 层是**首个 MoE + full indexer 层**（u6-l1 的层组合实践也以它为样本）。
4. **预期结果与第二问答案**：F 共 21 个。装载器不会引用空指针的原因是**类型层面的**：[src/model/build.rs:L131-L132](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L131-L132) 给 shared 层赋的是无字段的 `Glm52LayerIndexer::Shared`——既不是 `Option<…>` 的 `None`，也不是空指针，编译器让「在 shared 层上取 indexer 权重」这个操作根本写不出来（match 两个分支，`Shared` 分支没有权重可取）。配套的三道运行时防线：weights.rs 只为 full 层枚举 indexer 张量名（[L424-L427](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/weights.rs#L424-L427) 按 checkpoint 是否含该层 indexer 张量决定）；full 层缺 index-K 切片会报错（[layer.rs:L216-L218](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L216-L218)）；shared 层意外持有切片也会报错（[L246-L251](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L246-L251)）。

#### 4.2.5 小练习与答案

1. **如果某个残缺的层遍历从第 4 层（shared）开始执行，会发生什么？** 答：第 4 层走到 [L253-L256](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L253-L256) 的 `ensure!` 失败、整步报错退出——宁可失败也不用上一步的陈旧 top-k。
2. **注意力 TP4 下为什么不把 indexer 也按 rank 切 8 个头？** 答：top-2048 的打分要对全部 32 头求和后才能排序，切分就得引入跨 rank 归约（破坏整步固定链）；全套 21 层权重才约 0.18 GB，直接复制（[build.rs:L73-L76](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/build.rs#L73-L76)）。
3. **carry 为什么复用 `global_slots` 缓冲而不是每层各存一份？** 答：源码设计即「同一缓冲既当本层输出又当后续 shared 层的输入」，full 覆写、shared 直读到下一个 full（[indexer.rs:L275-L277](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L275-L277)）——`[T, topk]` 的 i32 缓冲不小，78 份既费显存也无必要。

### 4.3 index-K 缓存写入

#### 4.3.1 概念说明

indexer 打分要对**全上下文**的每个 token 算一次相似度，所以每个 token 的索引键（index-K）必须像 MLA KV 一样缓存下来：token 写入 KV 页池时，除了该层 64×656 B 的 MLA 切片（u4-l2），full 层还要在**同一页**里写一个 128 维 fp8 的索引键切片。也就是说页池里一页存两类切片，寻址方式完全一致（slab 基址 + 页号 × 页距 + 层内偏移），indexer 的 DeepGEMM kernel 与 FlashMLA 读的是同一块 slab 的不同区段。

每 token 每层 132 B = 128 B（fp8 键）+ 4 B（f32 逐 token scale）。为什么 MLA 切片是逐页一组 scale 而 index-K 是逐 token 一个 scale？因为 indexer 的量化组就是「一个 token 的 128 维」（组内求 max 定 scale），64 个 token 一页自然带 64 个 f32 scale。

#### 4.3.2 核心流程

写入侧（decode 每步、prefill 每块）：

```text
k（LayerNorm+RoPE 后的 bf16 [T,128]）
  └─ glm52_indexer_k_quant_and_cache_launch
       ├─ 每 token 128 维求 amax → f32 scale → fp8 e4m3
       └─ 按 slot_mapping 把 (fp8[128], scale f32) 写进
          slab + index_k_offset + 槽位寻址
```

读取侧（同一前向内、写入之后）：

```text
DeepGEMM paged MQA logits
  ├─ 布局：每页 [64×128 B fp8][64×4 B f32 scale] 交错排布
  ├─ TMA 描述符用 kv_cache_stride_bytes（页距）跨过 scale 区
  ├─ scales 指针 = kv_cache + block_kv × head_dim（wrapper 内部计算）
  └─ 输出 bf16 logits → f32 → top-k
```

写入与读取共用一个缓冲、零拷贝：写入 kernel 落位的布局正是 logits kernel 的 TMA 期待——这是「布局即协议」的又一例（u4-l2 的页距同理）。

#### 4.3.3 源码精读

**页内布局**：

- [src/layer.rs:L87-L96](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L87-L96)——`Glm52LayerCaches { mla_offset, index_k_offset: Option<usize> }`：每层在页内的两个字节偏移；注释写明 MLA 切片 64×656 B、full 层另有 DeepGEMM 布局的 index-K 切片 64×132 B；`Option` 正好编码「shared 层没有切片」。
- [src/model/mod.rs:L146](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L146)——`GLM52_KV_PAGE_IDXK_BYTES = INDEX_CACHE_BLOCK * (GLM52_INDEX_HEAD_DIM + 4)` = 64 × 132 = **8 448 B/页/full 层**（`+4` 就是每 token 的 f32 scale）。
- [src/model/mod.rs:L177-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L177-L200)——`glm52_page_layout`：单点构造 78 层偏移表——每层先 MLA 切片、full 层再追加 index-K 切片；末尾是第 78 层 MTP 镜像（[L193-L197](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L193-L197)，镜像同样带一个 index-K 切片——config 的 `index_share_for_mtp_iteration=true` 对应的物理载体，u9-l3 展开）；收尾 `assert_eq!(offset, GLM52_KV_PAGE_CONTENT_BYTES)` 把布局钉死在线格式常量上，防止漂移。
- [src/model/mod.rs:L688-L698](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L688-L698)——启动日志用同一个谓词数切片数：`index_k_slices=21 x 8448 (fp8[64,128]+f32[64]) + L78 mirrors`——日志本身就是布局文档。

**写入 kernel**：

- [src/indexer.rs:L536-L546](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L536-L546)——`glm52_indexer_k_quant_and_cache_launch`：入参 `Glm52IndexerCacheInsert { tokens: t, layout }` + bf16 的 k + 整块 slab + `slot_mapping`——量化与按槽位落位在一个 kernel 里完成，落在 4.1 说的 LayerNorm/RoPE 之后。

**读取侧（DeepGEMM）**：

- [src/indexer.rs:L548-L554](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L548-L554)——布局注释：每页 `[block_size×128 fp8][block_size×4 f32 scale]` 交错；TMA 描述符以 `kv_cache_stride_bytes` 跨过 scale 区，scales 指针按 `kv_cache + block_kv×head_dim` 推出——「与 vLLM decode 路径 API 一致，无需独立 scales 缓冲」。
- [src/indexer.rs:L555-L579](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L555-L579)——`glm52_deepgemm_paged_mqa_metadata_launch` + `glm52_deepgemm_paged_mqa_logits_launch`：前者为不规则上下文长度产调度元数据，后者按 block_table 分页读缓存打分；[L565-L567](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L565-L567) 注释再次确认 scales 内嵌、无独立分配。

#### 4.3.4 代码实践

1. **实践目标**：用纯算术复算页布局常量，并跑既有单测验证。
2. **操作步骤**：
   - 手算：78 × 41 984（MLA 切片）+ 21 × 8 448（index-K 切片）+ 41 984 + 8 448（L78 镜像）= ?，应等于 `GLM52_KV_PAGE_CONTENT_BYTES`。
   - 打开 [src/model/mod.rs:L1656-L1711](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L1656-L1711) 的 `cache_layout_tests`，核对 `page_layout_matches_the_wire_constants` 的断言：恰好 21 层 `index_k_offset.is_some()`、切片 16 字节对齐且不越界、`layers[0].index_k_offset == Some(41_984)`、MTP 镜像的 index-K 是页内最后一个切片。
   - 运行（CPU 即可）：
     ```bash
     cargo test --release -p pegainfer-glm52 --features glm52 --lib cache_layout_tests -- --nocapture
     ```
3. **需要观察的现象**：两个测试通过；若未来有人改动切片尺寸或谓词，`assert_eq!(offset, GLM52_KV_PAGE_CONTENT_BYTES)` 会立刻变红——布局漂移被编译期/测试期拦下而不是上线后出错。
4. **预期结果**：3 274 752 + 177 408 + 50 432 = 3 502 592 B（与 [L151](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L151) 常量一致）；页距 3 503 040（内容向上取 656 的倍数，u4-l2）。cargo 命令**待本地验证**（写作环境无法执行）。

#### 4.3.5 小练习与答案

1. **132 B/token 怎么分解？为什么是 4 的整数倍很重要？** 答：128 B fp8 键 + 4 B f32 scale（[mod.rs:L146](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L146) 的 `HEAD_DIM + 4`）；页内切片起始需 16 字节对齐（单测断言），8 448 = 64×132 恰好满足。
2. **DeepGEMM 为什么不需要单独的 scales 张量？** 答：scale 交错进同一页缓冲、紧跟 fp8 键区，scales 指针由 wrapper 按 `kv_cache + block_kv×head_dim` 内部推出（[indexer.rs:L548-L567](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/indexer.rs#L548-L567)），少一次分配也少一次同步。
3. **为什么 shared 层的页内没有 index-K 切片也不会浪费打分机会？** 答：shared 层根本不打分——它复用 full 层的槽位集合；它的注意力只读本层 MLA 切片（[layer.rs:L246-L251](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/layer.rs#L246-L251) 甚至断言它不得拥有切片）。

## 5. 综合实践

**任务：跟踪一步 decode 在第 6 层（首个 MoE + full indexer 层）注意力半场的完整时序。**

1. 画出第 6 层的 fork/join 时序图：主流跑 `front_q → （fork）front_rest → （join）attend`，AUX 流跑 indexer 全链；标出 `q_ready` 与 `topk_ready` 两个事件的方向（谁记录、谁等待）。
2. 列出 indexer 在 AUX 流上的 kernel 序列（对照 4.1.2 的流程图：投影 → LayerNorm → RoPE → 量化 → 缓存写入 → 元数据 → logits → 转型 → top-k → 槽位换算），并数一数：**第 7、8、9 层（shared）各自少了哪几步？**（答案：少了 indexer 全链，只保留 `ensure` 检查；carry 直接复用 `global_slots`。）
3. 回答一个问题：第 6-9 层这四层共享的 top-k，对 KV 页池意味着什么？（提示：这四层的 token 在同一页里只有第 6 层的 index-K 切片——用 [src/model/mod.rs:L177-L200](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L177-L200) 的循环验证：层 7、8、9 的 `index_k_offset` 都是 `None`。）
4. 有 GPU 的读者：启动 `--features glm52` 服务，在日志里找 [src/model/mod.rs:L691-L699](https://github.com/openinfer-project/openinfer/blob/09a1a38f8b4938b06ee4e4f63f64fd4feed59434/pegainfer-glm52/src/model/mod.rs#L691-L699) 的 `GLM5.2 KV cache: ... index_k_slices=21 x 8448 (fp8[64,128]+f32[64]) + L78 mirrors` 行，与本讲的数字对照（**待本地验证**）。

## 6. 本讲小结

- DSA indexer 是「先全量打分、再挑重点」的轻量 MQA（32 头 × 128 维，fp8），一步链路：投影 → 带偏置 LayerNorm → RoPE → 逐组量化 → 权重折叠 → DeepGEMM paged logits（融合逐头 ReLU）→ 确定性 top-k → 全局槽位，对齐 vLLM `DeepseekV32Indexer`。
- `topk` 的产物是**全局 KV 槽位**而非分数；所有层共用一张 block table，所以 full 层算出的槽位集合可以被后续 shared 层逐字复用——这是跨层共享成立的全部理由。
- full/shared 由纯谓词 `max(layer-2,0)%4==0` 决定：{0,1,2}∪{6,10,…,74} 共 21 层；dense 层 0-2 也是 full（dense 说的是 MLP 半场）。carry 就是 `s.idx.global_slots` 缓冲，`carry_ready` 守卫陈旧读。
- shared 层的安全性是类型层面的：`Glm52LayerIndexer::Shared` 无字段，空引用不可表示；装载与运行时另有三道 fail-fast 防线。
- index-K 每层每 token 132 B（128 fp8 + 4 B scale），按 `slot_mapping` 落进与 MLA 同一页的专属切片（8 448 B/页/full 层），DeepGEMM 的 TMA 直接按交错布局读取，写入读取零拷贝。
- 权重折叠等步骤「设备化」的共同动机：CUDA Graph 不能捕获步内流同步；scratch 常驻 + 偏移随调用传递保证指针稳定。

## 7. 下一步学习建议

- 下一讲 **u5-l1（路由器 noaux_tc 与共享专家）**：离开注意力子系统，进入 MoE——那里的 top-8 与本讲的 top-2048 形成有趣对照。
- 若想先把注意力收尾，可读 **u6-l1（decoder 层组合）**：本讲的注意力半场如何与 MLP 半场拼成完整一层（第 6 层是它的样本层）。
- 延伸源码：`pegainfer-kernels` 中 `glm52_indexer_*` 系列 kernel 的实现（本讲只到 launch 边界）；团队活文档 `docs/models/glm52/indexer-forward.md` 记录与 vLLM 的逐 kernel 对照，建议对照阅读。
- 精度验证线索：`src/oracle/` 的 indexer 精度门（u10-l1）会复放本讲的 cfg(test) 封装 `glm52_indexer_forward`，适合学完 u10 后回头跑一遍。
