# Qwen3.5 logits 宽度对齐：tile 对齐 GEMM 与 pad 行抑制

## 1. 本讲目标

本讲解剖提交 #1046（`perf(qwen35): align the logits GEMM width so the output projection stops landing on an align-1 sm_75 kernel`）引入的一条**跨层机制**。学完后你应该能够：

1. 说出 `vocab_size` / `decodable_vocab` / `selection_vocab` 三个宽度的语义，以及 Qwen3.5-4B checkpoint 下它们的实际取值（248320 / 248077 / 248192）。
2. 解释奇数选择宽度如何让 cublasLt 落到 align-1 的 sm_75 时代内核（1.67 ms/step，约占 c16 TPOT 的 12%），以及 tile 对齐后的实测收益（c16 TPOT −5.3%）。
3. 理解 `Qwen35Model::output_logits_into` 为什么把输出投影 GEMM 与 `SuppressIds` 的 \(-\infty\) 抑制绑定为模型**唯一** logits 出口：pad 行是训练过的 embedding，漏掉抑制不会崩溃，而会在 wire 上吐出不可解码 id 并回灌后续解码步。
4. 理解 `SampleScratch::with_selection_width` 如何保证 `effectively_greedy` 路由按**可解码宽度**而非 arena 宽度判定。
5. 说清两道新门禁各自守护的不变量——`pad_columns_are_suppressed_on_the_model_logits_paths`（模型侧掩码）与 `padded_arena_width_does_not_suppress_the_argmax_routing`（采样路由宽度）——以及 `hf_golden_gate` 为何对这两者天然失明。

## 2. 前置知识

本讲是 advanced 级别，默认你已读过 u8-l1（Qwen3.5 混合架构）、u4-l3（core::ops 三层结构）、u4-l6（采样层）。以下几个术语再用一段话补齐：

- **输出投影（output projection / LM head）**：解码最后一步把最后一层的 hidden state（经 RMSNorm）乘以词表矩阵，得到每个候选 token 的 logit。Qwen3.5-4B 词表很大（约 24.8 万行），这一步是一次大开销的 GEMM。
- **cublasLt 与「对齐」**：cuBLASLt 是 NVIDIA 的 GEMM 算法库，它按矩阵各维的**对齐程度**挑选内核。宽度是 128 的倍数（且指针按 16 字节对齐）时，可以选用向量化访存的 Ampere 专用内核；宽度是奇数时，每一行 logits 的起始地址都无法对齐，库只能退回到兼容老 GPU（sm_75/Turing 时代）、逐元素访问的 `align1` 内核——慢一个数量级并不奇怪。
- **leading dimension（首维度/行步长）**：列主序存储下，logits arena 每行起点之间的字节距离由词表宽度决定。宽度奇数 ⇒ 每一行都错位。
- **softmax 与 nucleus（top-p）采样**：logits 经 softmax 变概率；top-p 采样取「概率从大到小累积到 `top_p` 为止」的最小 token 集合（核）里随机抽一个。
- **rejection sampler 与 bf16 平局**：FlashInfer 的采样内核用拒绝采样实现 top-p/top-k。当两个 token 的最大 logit 在 bf16 精度下**完全相等**（平局）时，拒绝采样在二者之间的挑选是任意的——对「本质上是贪心」的请求来说，这是确定性的破坏。
- **teacher forcing 与 golden 门禁**：`hf_golden_gate` 用 HuggingFace 预先算好的「固定输入序列 → 每个位置的 top-K logprob」做基线，回放同样的序列并比较有界漂移。它**从不自己选 token**——这一点是第 4.4 节论证「门禁失明」的关键。
- **nsys**：NVIDIA Nsight Systems，GPU 时间线剖析工具；本讲的性能证据来自 [docs/models/qwen35/decode-kernel-attribution.md](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md) 的 nsys 内核级归因。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pegainfer-qwen35/src/config/model.rs` | `Config35` 定义 `selection_vocab` / `decodable_vocab` 字段；`bound_selection_vocab` 在验证边界完成「fail-closed 检查 + tile 对齐」 |
| `pegainfer-qwen35/src/config/tokenizer.rs` | `tokenizer_effective_vocab`：镜像前端分词器的合并规则，算出可解码的稠密词表宽度 |
| `pegainfer-qwen35/src/weights.rs` | `Qwen35Model`：加载期调用 bound、构造 `SuppressIds`、提供唯一 logits 出口 `output_logits_into` |
| `pegainfer-kernels/src/ops/elementwise.rs` | `SuppressIds` 类型与 `suppress_logits_bf16_in_place` 内核包装（共享层，gemma4 也在用） |
| `pegainfer-qwen35/src/prefill.rs` / `batch_decode.rs` | `output_logits_into` 的 prefill / decode 调用点 |
| `pegainfer-qwen35/src/decode_buffers.rs` / `tp_executor.rs` | logits arena 与采样 scratch 按「arena 用 selection_vocab、路由用 decodable_vocab」双宽度构造 |
| `pegainfer-sample/src/lib.rs` | `SampleScratch::with_selection_width`、`select_batch` 的路由闭包、`effectively_greedy` |
| `pegainfer-qwen35/src/unified_forward.rs` | 门禁一：`pad_columns_are_suppressed_on_the_model_logits_paths` |
| `pegainfer-sample/tests/select_batch.rs` | 门禁二：`padded_arena_width_does_not_suppress_the_argmax_routing` |
| `pegainfer-qwen35/tests/hf_golden_gate.rs` | HF golden 门禁（用来解释它为何看不见本讲的两种失效） |
| `docs/models/qwen35/decode-kernel-attribution.md` | 性能归因与两不变量的原始文档 |

## 4. 核心概念与源码讲解

### 4.1 三个宽度：从 tokenizer 到 tile 对齐的 `selection_vocab`

#### 4.1.1 概念说明

Qwen3.5-4B 的 checkpoint 里，`embed_tokens` / `lm_head` 有 248320 行（`vocab_size`），但前端分词器真正能解码的 token id 只构成一个 **248077 宽的稠密前缀**（`decodable_vocab`）。多出来的 243 行是训练时为对齐而留的 padding 行——注意它们是**训练过的 embedding**，不是垃圾数据。

问题出在性能：输出投影 GEMM 的宽度取 248077 这个**奇数**时，GEMM 的 M 维和 logits 矩阵的 leading dimension 都无法对齐，cublasLt 只能选 align-1 的老内核。#1046 的解法是把 GEMM 宽度**向上**取整到 128 的倍数（得 248192，仍小于 248320），记为 `selection_vocab`——一个纯粹的 GEMM 几何决策：

\[ \text{selection\_vocab} = \min\big(\lceil \text{decodable\_vocab} / 128 \rceil \times 128,\ \text{vocab\_size}\big) \]

代价是 logits arena 里多出 `248077..248192` 这 115 列「可计算、不可发射」的 pad 列——于是引出本讲后两个模块（掩码 + 路由宽度）。

#### 4.1.2 核心流程

```text
tokenizer.json (model.vocab + added_tokens)  ─┐
                                              ├─► tokenizer_effective_vocab()
tokenizer_config.json (added_tokens_decoder) ─┘        │ 稠密性检查: max_id + 1 == width
                                                       ▼
                                             effective_vocab = 248077
config.json 的 text_config.vocab_size ──────────────► bound_selection_vocab(effective_vocab)
                                                       │
                                                       ├─ effective > vocab_size ? ──► 报错（fail-closed）
                                                       ├─ decodable_vocab = effective
                                                       └─ selection_vocab = min(next_multiple_of_128, vocab_size)
```

三宽度在 4B checkpoint 下的取值与关系：

| 宽度 | 取值 | 语义 | 谁消费 |
| --- | --- | --- | --- |
| `vocab_size` | 248320 | checkpoint 权重的物理行数 | 权重映射、lm_head 形状校验 |
| `decodable_vocab` | 248077 | 前端可解码的稠密前缀宽度 | pad 区间下界、采样路由阈值 |
| `selection_vocab` | 248192 | tile 对齐后的 GEMM/arena 宽度 | 输出投影 GEMM、logits arena、采样 arena |

#### 4.1.3 源码精读

`Config35` 用两个带文档的字段把宽度差异显式化——[pegainfer-qwen35/src/config/model.rs:L106-L112](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L106-L112)：`selection_vocab` 是「词表宽度向上取整到 logits GEMM 的 tile 倍数，缓冲与采样 arena 都按它铺开」；`decodable_vocab` 是「超出它的行会被抑制为 -inf，也是 argmax-vs-sample 路由的度量宽度」。

核心逻辑全在 [bound_selection_vocab](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L161-L181)：

```rust
pub(crate) fn bound_selection_vocab(
    &mut self,
    effective_vocab: usize,
) -> Result<(), ConfigError> {
    if effective_vocab > self.vocab_size {
        return Err(ConfigError::EffectiveVocabExceedsCheckpoint { ... });   // fail-closed
    }
    // ……注释：奇数宽度迫使 cublasLt 落到 align-1 的 sm_75 时代内核，
    // 在 sm_80 上每个解码步约 1.7 ms……
    let aligned = effective_vocab.next_multiple_of(128);
    self.decodable_vocab = effective_vocab;
    self.selection_vocab = aligned.min(self.vocab_size);
    Ok(())
}
```

三个要点：

1. **fail-closed 在验证边界**：分词器比 checkpoint 还宽就直接拒绝加载（[L165-L170](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L165-L170)），而不是把检查散落在 loader 各处。
2. **三行核心算术**（[L177-L179](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L177-L179)）：`next_multiple_of(128)` 向上取整；`.min(self.vocab_size)` 保证对齐结果不越过 checkpoint 行数——对齐出来的 pad 行必须是**真实存在的权重行**（它们的 logit 才是「合理的」，这正是后面必须掩码的原因）。
3. **默认无 pad**：`TryFrom<RawConfig>` 初始化时两个宽度都等于 `vocab_size`（[L294-L295](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L294-L295)），只有显式调用 bound 才可能产生差异——不需要对齐的模型行为零变化。

`effective_vocab` 的来源是 [tokenizer_effective_vocab](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/tokenizer.rs#L69-L104)：它镜像钉住的前端 vLLM crate 的合并规则——`tokenizer.json` 的 `model.vocab` 加 `added_tokens`（解析失败即致命），再加 `tokenizer_config.json` 的 `added_tokens_decoder`（整文件 typed parse，失败则像前端一样丢弃全部 decoder tokens 并告警）。最后做**稠密性检查**（[L93-L103](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/tokenizer.rs#L93-L103)）：`max_id + 1 == width`，否则报错——因为「按行区间抑制 pad」这个设计**无法掩盖 id 空间中间的洞**，稀疏 id 空间必须在加载期暴露。

配置层有现成单测钉住算术语义（[pegainfer-qwen35/src/config/model.rs:L343-L359](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L343-L359)）：977 在 vocab 1000 下对齐到 1024 再被 clamp 回 1000（pad 被截断，无抑制）；769 对齐到 896（pad 区间 769..896，有抑制）。

#### 4.1.4 代码实践

1. **实践目标**：手工验证 4B checkpoint 的对齐算术，并跑通（或精读）配置单测。
2. **操作步骤**：
   - 推导：\( 248077 = 128 \times 1938 + 13 \)，故 `next_multiple_of(128)` 得 \( 128 \times 1939 = 248192 \)；又 \( 248192 < 248320 \)，clamp 不生效，`selection_vocab = 248192`，pad 区间为 `248077..248192`（115 行）。
   - 阅读单测 `selection_vocab_bound_aligns_to_tile_multiple` 与 `selection_vocab_bound_still_rejects_wider_tokenizers`（[L343-L371](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/config/model.rs#L343-L371)），自己先写下 977/769/1001 三个输入的预期输出再对照断言。
   - 有环境时运行：`cargo test --release -p pegainfer-qwen35 --features qwen35 --lib config::model`（注意：qwen35 feature 的构建需要构建期 Python + Triton，见 CLAUDE.md）。
3. **需要观察的现象**：单测绿；977 与 769 走出不同分支（一个被 clamp、一个产生真 pad 区间）。
4. **预期结果**：三组输入分别得到 `(selection, decodable) = (1000, 977)`、`(896, 769)`、报错。运行部分**待本地验证**（依赖构建环境）。

#### 4.1.5 小练习与答案

**练习 1**：若某 checkpoint 的 `vocab_size = 248200`、decodable = 248077，`selection_vocab` 是多少？pad 区间多大？
**答案**：对齐仍得 248192，`min(248192, 248200) = 248192`；pad 区间 `248077..248192`（115 行）。checkpoint 剩下的 `248192..248200` 这 8 行根本不进 GEMM，无需处理。

**练习 2**：为什么对齐方向必须「向上」而不是「向下」取整？
**答案**：向下取整会砍掉可解码 token——decodable 宽度内的每一列都必须能被 GEMM 计算并参与选择；而向上取整只添列，添出来的列交给 4.2 的掩码处理。

**练习 3**：为什么 `tokenizer_effective_vocab` 要求 id 空间稠密？
**答案**：本设计用「行区间 `decodable..selection`」描述 pad；若 id 空间中间有洞（比如缺 id 5），区间边界既定位不了洞也掩不了它，静默截断输出空间比加载失败更危险，所以稠密性是硬前提。

### 4.2 唯一的 logits 出口：`output_logits_into` 与 `SuppressIds`

#### 4.2.1 概念说明

arena 加宽后，pad 列 `248077..248192` 会带着**合理的 logits** 从 GEMM 里出来（它们是训练过的 embedding 行）。若不处理，一个宽松采样（permissive sample）可能抽中 pad 列——不崩溃、不报错，而是把一个**前端不可解码的 token id 发到 wire 上，并回灌进后续解码步**。这类「静默语义损坏」比崩溃难抓得多。

#1046 的结构性答案：把「输出投影 GEMM」和「pad 列 \(-\infty\) 掩码」合并成**一个方法** `output_logits_into`，让它成为模型产出 logits 的唯一途径；同时把裸的 `output_projection()` 访问器设为私有。一个只跑 GEMM、忘掉掩码的调用点从此**写不出来**——这正是文档注释里「二者属于一体」（the two belong together）的含义。

掩码的载体 `SuppressIds` 不是 qwen35 私有的：它定义在共享层 `pegainfer-kernels` 的 elementwise 模块，gemma4 的 `suppress_ids` 策略用的也是它。

#### 4.2.2 核心流程

```text
加载期（from_safetensors，一次性）：
  tokenizer_effective_vocab(model_path) ─► bound_selection_vocab(effective)
  if selection_vocab > decodable_vocab:
      ids = [decodable_vocab .. selection_vocab) 逐个收集
      SuppressIds::upload(ctx, ids, selection_vocab)     # 头宽度 = arena 宽度，越界 id 拒绝
  else: None                                             # 无 pad → 零开销

每个 logits 产出点（prefill / decode 每步）：
  normed = RMSNorm(last hidden)
  output_logits_into(normed, logits):
      gemm_rows_into_checked(output_projection, 0, selection_vocab, normed → logits)
      if let Some(suppress): suppress_logits_bf16_in_place(logits, suppress)
                              # 一次内核启动，所有 (row, id) 槽位写 -inf
```

decode 路径上该序列在 CUDA Graph 内捕获回放，掩码随图一起执行（上传只发生在加载期一次）。

#### 4.2.3 源码精读

先看模型侧字段——[pegainfer-qwen35/src/weights.rs:L74-L77](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L74-L77)：`Qwen35Model` 持有 `pad_logit_suppress: Option<crate::ops::SuppressIds>`，文档注明「由 `output_logits_into` 施加；选择宽度无需 padding 时不存在」。`Option` 语义即性能语义：无 pad 的模型连判空开销都几乎为零。

加载期的接线在 [pegainfer-qwen35/src/weights.rs:L172-L181](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L172-L181)：先算 `effective_vocab`、调用 bound，若发生了加宽就打一行 info 日志（`selection width … = decodable vocab … + tile-alignment pad`），把三个宽度都亮给运维。

`SuppressIds` 的构造在 [pegainfer-qwen35/src/weights.rs:L317-L330](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L317-L330)——注释一句话点破不变量：「对齐 pad 行是真实 checkpoint embedding 但不是可解码 token，所以它们绝不能赢得选择」。收集 `decodable_vocab..selection_vocab` 的全部 id，以 `selection_vocab` 为头宽度上传。

共享层的类型定义（[pegainfer-kernels/src/ops/elementwise.rs:L1091-L1119](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/elementwise.rs#L1091-L1119)）体现了一个精巧的防御设计：`SuppressIds` 把「ids + 上传时的头宽度」**焊死在类型里**，`upload` 拒绝任何超出头宽度的 id，内核按 id 直接索引 logits 而不再回读主机做检查——「边界检查是结构性的：这是通向它的唯一路径，不做检查就构造不出来」。

```rust
pub fn suppress_logits_bf16_in_place(
    ctx: &DeviceContext, logits: &mut HiddenStates, ids: &SuppressIds,
) -> Result<()> {
    ...
    if logits.hidden_dim != ids.vocab { bail!(...) }   # arena 宽度必须等于上传时的头宽度
    ...
    unsafe { ffi::suppress_logits_bf16_in_place_cuda(logits_ptr, ids_ptr, vocab, rows, id_count, stream) }
}
```

[pegainfer-kernels/src/ops/elementwise.rs:L1121-L1157](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/elementwise.rs#L1121-L1157)：一次内核启动把所有 `(row, id)` 槽位写成 \(-\infty\)；运行时再校验 `logits.hidden_dim == ids.vocab`，把掩码与 arena 宽度互相钉住。

最后是本模块的主角 [output_logits_into](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L360-L385)（文档注释在 L360-L366）：

```rust
pub(crate) fn output_logits_into(
    &self, normed: &HiddenStates, logits: &mut HiddenStates,
) -> Result<()> {
    let vocab = self.config.selection_vocab;
    gemm_rows_into_checked(&self.ctx, self.output_projection(), 0, vocab, normed, logits)?;
    if let Some(suppress) = &self.pad_logit_suppress {
        suppress_logits_bf16_in_place(&self.ctx, logits, suppress)?;
    }
    Ok(())
}
```

GEMM 经 core 的 traced 包装（[pegainfer-core/src/ops/traced.rs:L69-L89](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-core/src/ops/traced.rs#L69-L89)）落到 kernels；`output_projection()` 返回 `lm_head` 或（tie_word_embeddings 时）`embed_tokens`。配套的访问控制：裸权重矩阵的 [output_projection 被设为私有](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L354-L358)，注释直言「只有 GEMM tuning helper 直接采样它；logits 一律走 `output_logits_into`，pad 行掩码才无法被绕过」。

调用点恰好覆盖两条 logits 路径：

- **prefill 路径**：[pegainfer-qwen35/src/prefill.rs:L93-L123](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/prefill.rs#L93-L123) 的 `batch_last_hidden_logits`——按 `selection_vocab` 分配 logits（L119），末 token hidden 经 RMSNorm 后调用（L120）。
- **decode 路径**：[pegainfer-qwen35/src/batch_decode.rs:L690-L698](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/batch_decode.rs#L690-L698)（L697 调用）与 [L768](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/batch_decode.rs#L768)——解码侧两个实现各一处；服务路径经 `batch_decode_graph` 在 CUDA Graph 内执行，掩码随之进图。

消费端按 `selection_vocab` 铺 arena：decode 缓冲的 logits 是 `HiddenStates::zeros(ctx, config.selection_vocab, bs)`（[pegainfer-qwen35/src/decode_buffers.rs:L93](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/decode_buffers.rs#L93)）——它是 CUDA Graph 的预分配缓冲，宽度必须在加载期定死。

#### 4.2.4 代码实践

1. **实践目标**：验证「绕过掩码」在结构上不可能，并（在有声明的前提下）观察破坏掩码后门禁如何失败。
2. **操作步骤**：
   - 精读 `output_logits_into`（[weights.rs:L360-L385](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/weights.rs#L360-L385)），在仓库里全局搜索 `output_logits_into(` 与 `output_projection()`，确认调用点只有 prefill 一处 + decode 两处 + 测试，裸投影无逃逸。
   - 精读门禁 `pad_columns_are_suppressed_on_the_model_logits_paths`（[pegainfer-qwen35/src/unified_forward.rs:L168-L231](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L168-L231)）：它驱动**真实 checkpoint** 走 `batch_prefill_logits` 与 `batch_decode_graph`（DecodeGraphUse::Serve）两条路径，对每条路径断言 pad 列全部为 `-inf` 且 decodable 前缀不全为 `-inf`。
   - （可选、需 GPU + Qwen3.5-4B 权重）在本地分支把 `output_logits_into` 里的 `suppress_logits_bf16_in_place` 调用注释掉，运行该门禁：`PEGAINFER_TEST_MODEL_PATH=models/Qwen3.5-4B cargo test --release -p pegainfer-qwen35 --features qwen35 --lib pad_columns`。**实验后还原，严禁提交**。
3. **需要观察的现象**：两条路径的断言分别以 `pad id N survived selection (want -inf, got …)` 失败——报出的 got 值正是「合理」的有限 logit，印证 pad 行确实带着可信的数值。
4. **预期结果**：门禁在 prefill 与 decode 两处都红。无 GPU 时以精读断言 + 复述失败模式代替。**待本地验证**（GPU 部分）。

#### 4.2.5 小练习与答案

**练习 1**：为什么把 GEMM 和掩码合成一个方法，而不是在两处调用点各写两行？
**答案**：因为失效是静默的——pad 行 logits 合理，漏掩码只表现为 wire 上的不可解码 id。两个语句分放两处，将来新增第三个 logits 调用点时很容易只抄 GEMM 那行；合成一个方法 + 私有化裸投影后，「只跑 GEMM」的代码写不出来，错误从运行时移到编译期。

**练习 2**：`SuppressIds::upload` 为什么要拒绝超出头宽度的 id？内核里再查一遍不行吗？
**答案**：内核按 id 直接索引 logits、不回读主机；若 id 越界就是越界写。把检查放进唯一构造路径（upload），让「未检查的 SuppressIds」在类型层面无法存在，比在每个消费点记着检查更可靠。

**练习 3**：掩码写 \(-\infty\) 而不是一个大负数（比如 -1e30），有什么差别？
**答案**：\(-\infty\) 在数学上严格出局——softmax 后概率恰为 0，任何 `top_p`/`top_k`/`min_p` 组合都不可能选中，且对 logsumexp 的贡献是 \(e^{-\infty}=0\)（见 4.4 的失明论证）。大负数只是「极小概率」，在极端温度参数下理论上仍可被抽中。

### 4.3 采样路由按可解码宽度：`SampleScratch::with_selection_width`

#### 4.3.1 概念说明

掩码保住了「pad 列不可被选中」，但 arena 里多出来的列还剩最后一个作恶角度：**argmax-vs-sample 的路由判定**。

u4-l6 讲过 `effectively_greedy`：除了显式贪心参数，`top_p <= 1/V` 时核内只剩 argmax 一个 token，也按确定性 argmax 路径走——否则 rejection sampler 会在 bf16 平局的最大值上**随机**挑选。这里的 \( V \) 该取哪个宽度？若取 arena 宽度 248192，那么满足

\[ \frac{1}{248192} < \text{top\_p} \le \frac{1}{248077} \]

（一个相对宽度约 0.05% 的窗口）的请求会被误判为「非贪心」，送进随机采样器——而按可解码宽度 248077 度量时它本该走 argmax。也就是说：**加宽后的 arena 必须路由出与未加宽完全相同的行集合**。这就是几何宽度（缓冲、校验用 `vocab`）与语义宽度（路由阈值用 `selection_width`）的分离。

#### 4.3.2 核心流程

```text
SampleScratch::new(ctx, vocab, rows)
    == with_selection_width(ctx, vocab, vocab, rows)        # 未加宽模型：两宽度相等

with_selection_width(ctx, vocab, selection_width, rows)
    ├─ 校验: 1 <= selection_width <= vocab
    ├─ 所有缓冲按 vocab（arena 几何）分配
    └─ selection_width 仅存一个 usize 字段

select_batch(...):
    ensure!(logits.hidden_dim == scratch.vocab)             # 几何校验：arena 必须 == 分配宽度
    let is_argmax = |p| effectively_greedy(p, scratch.selection_width)   # 语义路由
```

softmax 的均值论证给出阈值合法性：\( V \) 个概率和为 1 时 \( p_{\max} \ge 1/V \)；若 \( \text{top\_p} \le 1/V \le p_{\max} \)，累积到 `top_p` 的最小核只含 top-1，采样退化为 argmax。

#### 4.3.3 源码精读

[SampleScratch](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L61-L90) 的两个宽度字段各配一段文档（L83-L88）：`vocab` 是「每个缓冲分配时所依据的词表宽度，`select_batch` 拒绝 `hidden_dim` 不同的 arena」；`selection_width` 是「argmax-vs-sample 路由判定所度量的宽度：**可发射的 token**，而不是模型可能为了对齐 tile 而加宽的 arena」。

[with_selection_width](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L97-L151)（[new](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L93-L95) 只是它的 `vocab, vocab` 别名）在构造期校验 `1 <= selection_width <= vocab`；doc 注释直接说明动机：pad 列「不得撑大 `top_p <= 1/vocab` 的核，否则本属 effectively-greedy 的请求会跌进在 bf16 平局最大值上（随机挑选）的 rejection sampler」。

[select_batch](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L211-L225) 里两个宽度各司其职：

```rust
let vocab = logits.hidden_dim;
ensure!(vocab == scratch.vocab, ...);          // 几何：arena == 分配宽度（248192）
// Pad columns a model aligned its GEMM to are not emittable tokens, so they
// must not move the `top_p <= 1/vocab` nucleus.
let is_argmax = |p: &&SamplingParams| effectively_greedy(p, scratch.selection_width);  // 语义：248077
```

[effectively_greedy](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L448-L461) 的核判定：

```rust
pub fn effectively_greedy(params: &SamplingParams, vocab_size: usize) -> bool {
    params.is_greedy()
        || (vocab_size > 0
            && params.top_p.is_finite()
            && params.top_p > 0.0
            && params.top_p <= 1.0 / vocab_size as f32)
}
```

模型侧三个构造点全部改为双宽度：单卡 decode 缓冲 [pegainfer-qwen35/src/decode_buffers.rs:L119-L124](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/decode_buffers.rs#L119-L124) 与 TP rank [pegainfer-qwen35/src/tp_executor.rs:L1398-L1403](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/tp_executor.rs#L1398-L1403) 都是 `with_selection_width(selection_vocab, decodable_vocab, …)`；测试助手亦然（[pegainfer-qwen35/src/unified_forward.rs:L152-L166](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L152-L166)）。

#### 4.3.4 代码实践

1. **实践目标**：用模型无关的合成 arena 测试，验证路由宽度对行为的实际影响。
2. **操作步骤**：阅读并（有 GPU 时）运行 [padded_arena_width_does_not_suppress_the_argmax_routing](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L181-L219)（`cargo test --release -p pegainfer-sample --test select_batch padded_arena -- --nocapture`，需要 GPU、**不需要模型权重**——测试自己构造 `DeviceContext` 和 logits）：
   - 场景：decodable=256、arena=512、`top_p = 1/256` 恰在边界上；logits 行里 id 128 与 200 同为 8.0（真 bf16 平局）。
   - 对照组 `SampleScratch::new(512)`：按 512 度量时 `1/256 > 1/512`，不满足隐式贪心 → 走采样器；断言 64 个 seed 中**必须至少一次**抽到平局伙伴 200（证明该行确实在采样器里，防止实验组「假绿」）。
   - 实验组 `with_selection_width(512, 256)`：`1/256 <= 1/256` 成立 → 走 argmax；断言 64 个 seed 全部返回 128。
3. **需要观察的现象**：两组行为分岔——同一 arena、同一参数，唯一区别是 scratch 携带的 selection_width。
4. **预期结果**：测试通过；对照组的 `sampled_the_peer` 断言与实验组的全 argmax 断言同时成立。**待本地验证**（需要 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `BatchSamplingScratch`（FlashInfer 采样缓冲）仍按 arena 宽度分配，不按 selection_width 缩小？
**答案**：采样在完整 arena 上工作，pad 列经模型侧 \(-\infty\) 掩码后 softmax 概率为 0，天然出局——采样器不需要知道边界。pad 列唯一能「作恶」的地方是路由阈值，所以只有它改用 selection_width，修改面最小。

**练习 2**：把 `selection_width` 误设成一个偏小的值（比如 248064）会怎样？
**答案**：`top_p` 在 \( (1/248077,\ 1/248064] \) 的请求会被**误判为贪心**走 argmax——本应按分布采样的行被强行确定化，采样多样性受损。构造期校验只能保证 `1 <= selection_width <= vocab`，语义正确性靠调用方传 `config.decodable_vocab`。

**练习 3**：`select_batch` 的 arena 校验为什么用 `scratch.vocab` 而不用 `selection_width`？
**答案**：那是一次**几何**校验——缓冲（argmax partials、BatchSamplingScratch）都按 `vocab` 分配，来的 arena 宽度不同就是越界风险，必须精确相等；路由是另一件事，用语义宽度。两个宽度在同一个函数里同框出现，正是本讲「几何与语义分离」主题的缩影。

### 4.4 两道门禁与一个盲区：测试如何钉住静默不变量

#### 4.4.1 概念说明

本讲的两个不变量（pad 不可选、pad 不挪路由门）有一个共同点：**破坏时不出声**。pad 行的 logits 合理，golden 基线照常对齐，服务照常吐 token——直到某个用户的请求收到不可解码的 id，或某个贴边 `top_p` 的请求输出变得不确定。

而项目现有的精度门禁 `hf_golden_gate` 对此**天然失明**，原因有二：

1. 它回放固定的 teacher-forced 序列、只比较**给定 token** 的 logprob 漂移（[pegainfer-qwen35/tests/hf_golden_gate.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/tests/hf_golden_gate.rs#L1-L8)），**从不自己选 token**——掩码的失效只在「选择」时暴露。
2. 它比对的是每行 logprob，而 logprob 的分母（logsumexp）对掩码不变：pad 列被写成 \(-\infty\) 后对求和的贡献是 \( e^{-\infty} = 0 \)，加不加掩码，真实 token 的归一化常数一字不差：

\[ \log\Big(\sum_{j<V_{\text{dec}}} e^{l_j} + \sum_{j \in \text{pad}} e^{-\infty}\Big) = \log\sum_{j<V_{\text{dec}}} e^{l_j} \]

所以必须另立两道门禁，分别瞄准两个不变量。

#### 4.4.2 核心流程

```text
不变量                            门禁                                       手段
─────────────────────────────  ─────────────────────────────────────────  ──────────────────────────────
pad 列不可被选中（模型侧掩码）   pad_columns_are_suppressed_on_the_...      真 checkpoint 驱动 prefill 与
（weights.rs 的 output_logits_    (qwen35 crate 内 unified_forward.rs)      decode graph 两条 logits 路径，
 _into + SuppressIds）                                                       断言 pad 列全 -inf、前缀不全 -inf

pad 列不得挪动路由阈值（采样     padded_arena_width_does_not_suppress_the_   合成 arena（无需权重），top_p 恰
 scratch 的 selection_width）     _argmax_routing (pegainfer-sample 测试)    在 1/decodable 边界，对照组+
                                                                            实验组双向断言
```

#### 4.4.3 源码精读

门禁一 [pad_columns_are_suppressed_on_the_model_logits_paths](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L168-L231) 的测试文档（L168-L173）先复述了失效模式：「只跑输出投影而不掩码的 logits 调用点会让 pad 列可选，且套件里没有别的测试会注意到——pad 行是带合理 logits 的训练 embedding，失效表现为 wire 上的不可解码 id 而非崩溃」。测试体：

- 加载真实模型（无权重则跳过），若 `decodable == selection`（该 checkpoint 无需对齐）也跳过——门禁只在有 pad 时有意义（[L181-L188](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L181-L188)）。
- 闭包 `pad_tail_is_suppressed`（[L190-L204](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L190-L204)）把 logits 第一行读回主机，逐列断言 `decodable..selection` 区间 `is_sign_negative() && is_infinite()`（即 \(-\infty\)），并断言 decodable 前缀**不全**为 \(-\infty\)（防「全掩掉也绿」的假阳性）。
- 先跑 `batch_prefill_logits` 验证 prefill 路径（[L206-L214](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L206-L214)），再取贪心 next token 走 `batch_decode_graph` 验证 decode 图路径（[L216-L230](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/unified_forward.rs#L216-L230)）——覆盖 `output_logits_into` 的全部两个调用面。

门禁二 `padded_arena_width_does_not_suppress_the_argmax_routing` 已在 4.3.4 精读，此处补一句设计点评：对照组断言「采样器确实会抽到平局伙伴」是整条测试的**有效性证明**——若 `with_selection_width` 偷懒没生效，两组行为相同，对照组先红。

两道门禁与失明论证的原始出处是 [docs/models/qwen35/decode-kernel-attribution.md:L46-L62](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md#L46-L62)（Finding 4），其中也记录了性能证据：align-1 内核 `cutlass_75_tensorop_bf16_s1688gemm_bf16_128x64_tn_align1` 每步 1.67 ms（280 实例 ≈ 270 步），对齐后 c16 TPOT `14.26 → 13.51 ms`（−5.3%）、c8 `11.98 → 11.23`、QPS16 `23.77 → 22.68`（vLLM 0.27 为 23.60）。

#### 4.4.4 代码实践

1. **实践目标**：把「golden 失明」从一句结论变成你能独立复述的论证。
2. **操作步骤**：
   - 读 [hf_golden_gate.rs 的模块文档](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/tests/hf_golden_gate.rs#L1-L8)，圈出「teacher-forced」「比较 logprob 漂移而非精确文本」两处。
   - 写下掩码前后某真实 token 的 logprob 表达式：\( \log \frac{e^{l_t}}{\sum_j e^{l_j}} \)，把 pad 列加入分母，说明 \( e^{-\infty} = 0 \) 使其不变。
   - 自查两问：golden 若要看见掩码缺失，必须做什么？（答：自己采样并允许 pad id 胜出，或直接断言 pad 列值——前者改变门禁性质，后者就是门禁一。）路由失效为什么 golden 更看不见？（答：路由只影响「选谁」，不影响任何 logprob 数值。）
3. **需要观察的现象**：纯阅读型实践，无运行现象。
4. **预期结果**：你能用三句话向同伴解释「为什么这两类失效需要专门门禁」。

#### 4.4.5 小练习与答案

**练习 1**：门禁一为什么要断言「decodable 前缀不全为 \(-\infty\)」？
**答案**：防止假阳性——若实现把整行都掩掉（比如 id 区间算反了），「pad 列全 -inf」依旧成立。前缀断言保证掩码是**选择性**的。

**练习 2**：门禁一无权重时跳过（`model_path_or_skip`），这削弱了它吗？
**答案**：不削弱其定位但限制其覆盖——它守护的是「真实 checkpoint 的 pad 行真的是合理 logits」这一性质，合成数据无法复现；CI 无权重时靠门禁二（模型无关）兜住路由侧，掩码侧则必须在有 GPU + 权重的环境跑。这正是项目把「CPU 单测 / GPU 无权重 / GPU+权重」分层组织测试的原因（见 u10-l1）。

**练习 3**：假如未来另一个模型线（比如 k3）也做 tile 对齐，应该复用哪些件？
**答案**：`bound_selection_vocab` 的算术（各 crate 自持 config，需照搬语义）、共享层的 `SuppressIds` / `suppress_logits_bf16_in_place`（直接复用）、`SampleScratch::with_selection_width`（直接复用），并照两道门禁的模式各立一道测试——共享内核已就位，不变量与门禁是每个模型线自己的责任。

## 5. 综合实践

把本讲三个模块串成一张图加一次破坏性实验（源码阅读为主，实验需 GPU 时已标注）：

**任务 A：三宽度传播图。** 以 Qwen3.5-4B 为例手工推导（\( 248077 \to 248192 \)，clamp 不触发），然后画出三个宽度穿过四层的完整传播图，并在每个箭头旁标注源码位置：

```text
tokenizer.json / tokenizer_config.json
      │ tokenizer_effective_vocab()          [config/tokenizer.rs:L69-L104]
      ▼
effective_vocab = 248077 ──┐  vocab_size = 248320 (config.json)
                           ▼  ▼
              bound_selection_vocab()         [config/model.rs:L161-L181]
                           │
         ┌─────────────────┴──────────────────────┐
         ▼                                        ▼
  decodable_vocab = 248077                 selection_vocab = 248192
         │                                        │
         │   权重层 [weights.rs:L172-L181,L317-L330]
         │   · embed/lm_head 全量 248320 行上映射，GEMM 只读前 248192 行
         │   · SuppressIds = ids[248077..248192)，头宽度 248192
         │                                        │
         │   缓冲层 [decode_buffers.rs:L93,L119-L124]
         │   · logits arena = HiddenStates[248192 × bs]（CUDA Graph 预分配）
         ▼                                        ▼
   路由阈值                                  采样 arena（几何）
   SampleScratch::with_selection_width(248192, 248077, bs)
```

自查：图上每个「248192」都是几何决策、每个「248077」都是语义决策，混用即是 bug。

**任务 B：破坏掩码并解释门禁反应。** 按 4.2.4 的步骤注释掉 `suppress_logits_bf16_in_place` 调用（本地实验、事后还原），运行门禁一，记录 prefill 与 decode 两条路径各自的失败信息；然后写一段话解释为什么 `hf_golden_gate` 仍然全绿（teacher-forced logprob、\( e^{-\infty} = 0 \) 使 logsumexp 不变、从不选择 token）。无 GPU 时改为精读门禁一的断言并推演失败模式。**GPU 部分待本地验证**。

## 6. 本讲小结

- 一个性能问题（奇数宽度 248077 → cublasLt align-1 的 sm_75 时代内核，1.67 ms/step、约 c16 TPOT 的 12%）引出三个宽度：`vocab_size` 248320 / `decodable_vocab` 248077 / `selection_vocab` 248192；对齐后 c16 TPOT −5.3%。
- `Config35::bound_selection_vocab` 在验证边界一次性完成 fail-closed 检查与 `next_multiple_of(128)` 对齐加 clamp；`tokenizer_effective_vocab` 镜像前端合并规则并强制 id 空间稠密。
- `Qwen35Model::output_logits_into` 把输出投影 GEMM 与 `SuppressIds` 的 \(-\infty\) 掩码绑定为唯一 logits 出口，裸投影私有化——pad 行是训练过的 embedding，漏掩码的失效是 wire 上的不可解码 id 回灌，而非崩溃。
- `SampleScratch::with_selection_width` 把几何宽度（arena 分配与校验）与语义宽度（`effectively_greedy` 的 `top_p <= 1/V` 阈值）分离，保证加宽后的 arena 路由出与未加宽完全相同的行集合。
- `hf_golden_gate` 因 teacher-forced 且 \( e^{-\infty}=0 \) 使 logsumexp 不变而对两类失效天然失明；两道新门禁各守一个不变量：模型侧掩码（真 checkpoint 双路径断言）与采样路由宽度（合成 arena 对照+实验双向断言）。
- 方法论收获：静默不变量要靠「结构性防御（类型/私有性/唯一出口）+ 针对性门禁」双保险，而不是指望现有精度基线顺带覆盖。

## 7. 下一步学习建议

- 读 [docs/models/qwen35/decode-kernel-attribution.md](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md) 全文，看 nsys 归因如何定位到「每步一次的 GEMM」这个形状指纹，并了解 improvement queue 里排队的其余优化（GEMM 家族重调参、FlashInfer paged decode 替换）。
- 结合 u10-l3（profiling）复现一次内核级归因：用 `nsys stats --report cuda_gpu_kern_sum` 观察对齐前后输出投影 GEMM 的内核名与耗时变化。
- 对照 gemma4 的 `suppress_ids` 用法（[pegainfer-gemma4/src/engine.rs:L1154](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-gemma4/src/engine.rs#L1154) 附近），体会共享 `SuppressIds` 在不同模型线里的两种动机（gemma4 是 EOS 抑制策略，qwen35 是对齐 pad）。
- 若你计划给某条模型线做类似的 GEMM 对齐，按 u10-l5 的接入清单走：config 层立宽度、weights 层立掩码、sampler 层传双宽度、再补两道门禁——缺一不可。
