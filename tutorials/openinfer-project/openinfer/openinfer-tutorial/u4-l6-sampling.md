# 采样层：pegainfer-sample

## 1. 本讲目标

模型前向的最后一站是 logits（每个候选 token 的未归一化分数），但真正决定「下一个 token 是谁」的是采样层。本讲解剖 `pegainfer-sample` 这个所有模型 crate 共用的采样 crate，学完后你应该能：

- 说清 `SampleScratch` 的「一次分配、跨步复用」缓冲策略，以及它为什么服务于 CUDA Graph 的指针稳定性要求。
- 区分三条采样路径：批 argmax 贪心、FlashInfer 批量 temperature/top-k/top-p（min_p 行独立分区）、seeded 行的单行确定性重放。
- 理解 `effectively_greedy` 的数学依据：为什么 `top_p <= 1/V` 时核内只剩 argmax 一个 token。
- 掌握 #1046 引入的 **selection_width 与 arena 宽度（vocab）的分离**：模型把 logits arena 对齐到 GEMM tile 倍数后，「argmax 还是走采样器」的路由判定仍必须按**可解码词表宽度**度量，而不是按 arena 宽度。
- 会读（有 GPU 时会跑）`select_batch` 的测试与 bench，并能解释新门禁测试 `padded_arena_width_does_not_suppress_the_argmax_routing` 守护的不变量。

## 2. 前置知识

- **logits 与 softmax**：模型对词表里每个 token 输出一个 logit \( s_i \)。softmax 把它变成概率分布 \( p_i = \dfrac{e^{s_i}}{\sum_j e^{s_j}} \)。「采样」就是从这个分布里抽一个 token。
- **贪心解码（greedy）**：永远取 \(\arg\max_i s_i\)，输出确定。「采样解码」则按概率随机抽，同一段话每次可能不同。
- **temperature / top-k / top-p / min_p**：四种收缩采样分布的旋钮。temperature < 1 让分布更尖；top-k 只保留概率最高的 k 个；top-p（核采样）保留按概率降序累积到 p 的最小前缀；min_p 砍掉概率低于 \( \text{min\_p} \times p_{\max} \) 的 token。
- **bf16 与平局（tie）**：logits 以 bf16（8 位指数 + 7 位尾数）存放，精度有限，两个不同 token 的 logit 可能完全相等。argmax 内核按「值相等时取更小下标」破平局，是确定性的；而 rejection sampler 遇到并列最大值时选哪个取决于随机数——这就是本讲反复出现的「bf16-tied maxima」问题。
- **philox**：GPU 上常用的确定性计数器模式随机数生成器（counter-based RNG）。给同样的 seed 就产出同样的随机数流。FlashInfer 的采样内核用它。
- **前置讲义**：`DeviceContext`、单流设计与 `HiddenStates`（seq_len × hidden_dim 的 arena，见 u4-l1）；pinned 主机内存与 DMA（见 u4-l4）。`HiddenStates` 在本讲扮演「logits arena」：行是 batch 里的请求，`hidden_dim` 此时是词表宽度。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `pegainfer-frontend/src/sampler.rs` | CUDA 无关的采样参数契约 `SamplingParams`（temperature/top_k/top_p/min_p/seed）与 `is_greedy()` 判定 |
| `pegainfer-sample/src/lib.rs` | 本讲主角：`SampleScratch` 复用缓冲、`select_batch` 三路分发、`mix_seed`、`effectively_greedy`、logprobs 家族 |
| `pegainfer-sample/tests/select_batch.rs` | 无模型权重的合成 arena 集成测试：路由、落位、scratch 复用、容量不变量，全部需要 GPU |
| `pegainfer-sample/benches/select_batch.rs` | criterion 微基准：greedy / sampling / mixed 三种混合在 Qwen3 词表宽度下的耗时 |
| `pegainfer-kernels/src/ops/sampling.rs` | 下层：`BatchSamplingRow`/`BatchSamplingScratch` 与 `gpu_sample_batch_into`（FlashInfer 批量采样、min_p 分区） |
| `pegainfer-qwen35/src/decode_buffers.rs` | 消费侧样本：qwen35 如何用 `with_selection_width` 构造 scratch（#1046） |
| `docs/models/qwen35/decode-kernel-attribution.md` | #1046 的归因文档：奇数宽度 GEMM 落到 align-1 内核的性能证据与两条不变量 |

分层关系（承接 u4-l3 的三层结构）：`.cu` 内核与 FFI 属于 `pegainfer-kernels`；`pegainfer-sample` 拥有**策略**（贪心/非贪心路由、logprob 数学）和可复用 scratch；`SamplingParams`/`TokenLogprob` 定义在无 CUDA 的 `pegainfer-frontend` 契约 crate 里。

## 4. 核心概念与源码讲解

### 4.1 采样契约与贪心判定：SamplingParams 与 effectively_greedy

#### 4.1.1 概念说明

采样请求由 `SamplingParams` 描述。它定义在前端契约 crate 里（不含任何 CUDA 类型），因为 HTTP 层要直接填充它。核心问题是：**一行请求什么时候可以走廉价的确定性 argmax，什么时候必须走真正的随机采样？**

答案是两级判定：

1. `is_greedy()`：显式贪心——temperature 低于采样下限（1e-5，vLLM 画同一条线）或 `top_k == 1`。
2. `effectively_greedy()`：隐式贪心——除显式贪心外，`top_p` 紧到 \( \le 1/V \) 时，核内只剩 argmax 一个 token，采样在数学上退化为 argmax。

为什么要为「隐式贪心」专门建一条路由？因为如果把它送进 rejection sampler，遇到 bf16 平局的最大值时 sampler 会随机挑一个并列者——一个语义上确定性的请求变成了随机的。路由到 argmax 既保住确定性，又跳过了一次不必要的 softmax。

#### 4.1.2 核心流程

先看数学。设词表大小为 \( V \)，softmax 后的概率满足 \( \sum_{i=1}^{V} p_i = 1 \)，因此最大概率有下界：

\[ p_{\max} = \max_i p_i \;\ge\; \frac{1}{V} \]

top-p 核是按概率降序累积到 `top_p` 的最小前缀。若

\[ \text{top\_p} \le \frac{1}{V} \le p_{\max} \]

则累积一个 token 就已达到阈值——核恰好是 \(\{\arg\max\}\) 单点集。判定流程：

```text
对 arena 的每行 params[i]:
  if params.is_greedy()                    # temperature < 1e-5 或 top_k == 1
      -> argmax 路径
  else if 0 < top_p <= 1/V                  # V = 可解码词表宽度（见 4.2）
      -> argmax 路径（隐式贪心，确定性 + 免 softmax）
  else
      -> 采样路径
```

注意 \( 1/V \) 对 **V 的取值敏感**：V 越大，\( 1/V \) 越小，「隐式贪心」区间越窄。这正是 #1046 修复的坑（见 4.2）。

#### 4.1.3 源码精读

`SamplingParams` 与 `is_greedy`（无 CUDA 的契约侧）：

[pegainfer-frontend/src/sampler.rs:L2-L15](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-frontend/src/sampler.rs#L2-L15) 定义七个字段：temperature、top_k、top_p、min_p、`seed: Option<u64>`（`Some` 使请求 token 成为 (seed, step, 分布) 的纯函数）、ignore_eos。

[pegainfer-frontend/src/sampler.rs:36-L39](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-frontend/src/sampler.rs#L36-L39) 是 `is_greedy`：

```rust
pub fn is_greedy(&self) -> bool {
    self.temperature < 1e-5 || self.top_k == 1
}
```

`effectively_greedy`（策略侧，公开给自持贪心路径的模型用）：

[pegainfer-sample/src/lib.rs:448-L461](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L448-L461)

```rust
pub fn effectively_greedy(params: &SamplingParams, vocab_size: usize) -> bool {
    params.is_greedy()
        || (vocab_size > 0
            && params.top_p.is_finite()
            && params.top_p > 0.0
            && params.top_p <= 1.0 / vocab_size as f32)
}
```

除 `is_greedy` 外还要求 top_p 有限且严格为正——NaN/0/负数不构成合法核参数，不视为隐式贪心。

一个重要的旁注（Kimi-K2 例外）：crate 模块文档 [pegainfer-sample/src/lib.rs:21-L25](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L21-L25) 说明 Kimi-K2 词表分片在各 rank 上，`select_batch` 的「整词表 argmax」假设表达不了它，于是它自己跑分片局部 argmax + 跨 rank 归约，但非贪心行仍走本 crate 再导出的 `gpu_sample_batch_into` 单一入口——所以它必须复用这里的 `effectively_greedy`/`mix_seed` 语义，否则同一请求换模型就换行为。

#### 4.1.4 代码实践

1. **实践目标**：不运行任何代码，纸面掌握路由判定。
2. **操作步骤**：对下表五组参数（词表宽度 \( V = 151{,}936 \)，即 \( 1/V \approx 6.58 \times 10^{-6} \)）逐行预测 `effectively_greedy` 的结果。

   | temperature | top_k | top_p | 预测 |
   | --- | --- | --- | --- |
   | 0.0 | -1 | 1.0 | ？ |
   | 1.0 | 1 | 1.0 | ？ |
   | 1.0 | -1 | 0.9 | ？ |
   | 1.0 | -1 | 1e-6 | ？ |
   | 1.0 | -1 | 1e-5 | ？ |

3. **需要观察的现象**：自己写出的预测与答案的偏差。
4. **预期结果**：依次为 贪心（显式）、贪心（top_k==1）、采样、贪心（隐式，1e-6 < 6.58e-6）、采样（1e-5 > 6.58e-6，核内不止一个 token）。第五行是关键：`1e-5` 看起来很小，但换算成 \( 1/V \) 尺度还不够小。
5. 有 GPU 时可用测试 [tiny_top_p_routes_to_argmax_even_under_bf16_ties](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L142-L178) 验证第四行场景：它在 151,936 宽词表上构造 bf16 平局双峰（logit 8.0 的两个 token），断言 `top_p = 1e-6` 时 64 个 seed 全部命中较小下标的 argmax。**待本地验证**（该测试需要 GPU）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `is_greedy` 用 `temperature < 1e-5` 而不是 `temperature == 0.0`？
**答案**：浮点比较不可靠，且语义上 temperature → 0 的极限就是 argmax（1/temperature 在真正下溢之前早就溢出了）。vLLM 也在 1e-5 画同一条线——两引擎对「同一请求是否贪心」的判定保持一致，便于对照测试。

**练习 2**：`top_p = 0` 的请求按当前实现走哪条路径？合理吗？
**答案**：走采样路径——`effectively_greedy` 要求 `top_p > 0.0`。`top_p = 0` 在语义上是非法/未定义参数（核采样累积阈值不能为零），把它交给 FlashInfer 报错比在路由处静默改成贪心更诚实。

### 4.2 SampleScratch：一次分配的复用缓冲与 selection_width（#1046）

#### 4.2.1 概念说明

`SampleScratch` 是 `select_batch` 的全部设备缓冲，按 `max_rows × vocab` 一次分配、跨解码步复用。解码路径需要指针稳定的缓冲（承接 u4-l5 的 CUDA Graph 主题），所以**绝不每步重分配**。贪心行与非贪心行使用不相交的缓冲、顺序执行，因此一个 scratch 就覆盖完整的混合批。

#1046 之后，scratch 携带**两个宽度**：

- `vocab`：arena 的几何宽度。所有缓冲按它分配；`select_batch` 拒绝 `hidden_dim` 不同的 arena。
- `selection_width`：路由判定（`top_p <= 1/V`）所用的宽度——**可发射（emittable）的 token 数**。当模型为了对齐 GEMM tile 把 arena 加宽后，`selection_width < vocab`。

背景：qwen35 把输出投影 GEMM 的宽度从可解码词表 248,077（奇数）对齐到 128 的倍数 248,192，否则 cublasLt 会把这条每步一次的 GEMM 派给 align-1 的 sm_75 时代内核，单次 1.67 ms、约占 c16 TPOT 的 12%；对齐后 c16 TPOT −5.3%（14.26 → 13.51 ms）。加宽产生 115 列 pad 列，由此需要两条不变量（详见 [docs/models/qwen35/decode-kernel-attribution.md:46-L62](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md#L46-L62)）：

1. **pad 列不可被选中**——模型侧 `output_logits_into` 在 GEMM 后立刻把它们写成 \(-\infty\)（u8-l7 专题讲解）。
2. **pad 列不得移动路由阈值**——本 crate 的职责。若路由按 arena 宽度 \( V_{\text{arena}} = 248{,}192 \) 度量，则 \( \text{top\_p} \in \left(\frac{1}{248{,}192},\; \frac{1}{248{,}077}\right] \) 的请求会满足 \( \text{top\_p} \le 1/V_{\text{decodable}} \)（本该走 argmax）却不满足 \( \text{top\_p} \le 1/V_{\text{arena}} \)，被推进 rejection sampler，在 bf16 平局上随机挑 token——一个事实上的确定性请求失去了确定性。注意分母越小 \( 1/V \) 越大，所以用**较小的**可解码宽度判定是更宽的贪心门。

#### 4.2.2 核心流程

```text
SampleScratch::new(ctx, vocab, max_rows)                 # selection_width = vocab（默认）
SampleScratch::with_selection_width(ctx, vocab,
                                    selection_width,     # 1 <= selection_width <= vocab
                                    max_rows)
  ├─ 校验 vocab > 0、max_rows > 0、1 <= selection_width <= vocab
  ├─ 分配 argmax 缓冲（行索引、split 部分和、top1 值、输出）
  ├─ 分配两块 pinned 主机回读缓冲（双槽轮换）
  ├─ 上传 identity 行映射 (0..max_rows)
  └─ 构造 BatchSamplingScratch（按 arena 宽度 vocab 分配——
     FlashInfer 采样扫整个 arena，pad 列此时已是 -inf，天然不参与）

select_batch 中：
  ensure!(logits.hidden_dim == scratch.vocab)      # 几何校验：按 arena 宽度
  is_argmax = |p| effectively_greedy(p, scratch.selection_width)   # 语义路由：按可解码宽度
```

#### 4.2.3 源码精读

结构体与两个宽度字段：

[pegainfer-sample/src/lib.rs:61-L90](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L61-L90) 中 `vocab` 的注释说明它是缓冲尺寸的依据；[pegainfer-sample/src/lib.rs:86-L88](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L86-L88) 新增的 `selection_width` 字段注释一针见血：「argmax-vs-sample 路由判定所度量的宽度：可发射的 token，而不是模型可能为对齐 tile 而加宽的 arena」。

构造器族与 #1046 的关键文档：

[pegainfer-sample/src/lib.rs:93-L115](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L93-L115)

```rust
pub fn new(ctx: &DeviceContext, vocab: usize, max_rows: usize) -> Result<Self> {
    Self::with_selection_width(ctx, vocab, vocab, max_rows)
}

pub fn with_selection_width(
    ctx: &DeviceContext,
    vocab: usize,
    selection_width: usize,
    max_rows: usize,
) -> Result<Self> {
    ensure!(vocab > 0 && max_rows > 0, ...);
    ensure!(
        selection_width > 0 && selection_width <= vocab,
        "SampleScratch selection width {selection_width} must be in 1..={vocab}"
    );
```

`new` 现在只是 `with_selection_width(vocab, vocab)` 的别名——未加宽的模型（qwen3 等）行为不变。

路由判定处（几何与语义在此分家）：

[pegainfer-sample/src/lib.rs:217-L225](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L217-L225)

```rust
let vocab = logits.hidden_dim;
ensure!(
    vocab == scratch.vocab,
    "select_batch: logits vocab {vocab} != scratch vocab {}",
    scratch.vocab
);
// Pad columns a model aligned its GEMM to are not emittable tokens, so they
// must not move the `top_p <= 1/vocab` nucleus.
let is_argmax = |p: &&SamplingParams| effectively_greedy(p, scratch.selection_width);
```

消费侧样本（qwen35 的解码缓冲）：

[pegainfer-qwen35/src/decode_buffers.rs:119-L124](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-qwen35/src/decode_buffers.rs#L119-L124) 用 `(config.selection_vocab, config.decodable_vocab)` 构造 scratch——arena 248,192 列，路由按 248,077 判定。`unified_forward.rs:156` 与 `tp_executor.rs:1398` 是同一模式的两处复刻。

另外两个值得读的缓冲细节：

- [pegainfer-sample/src/lib.rs:71-L79](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L71-L79)：argmax 结果的回读落在 **pinned** 主机缓冲。注释记录了 #704 事故：pageable 回读具有同步拷贝语义，一旦 P/D KV 恢复的批量拷贝洪峰挤占通道，这个亚毫秒级调用被拖成固定 23.6 ms，冻住了所有活跃流的 token 交付。pinned 让 D2H 保持异步；还配了两块缓冲（`argmax_host`/`argmax_host_alt`）轮换。
- [pegainfer-sample/src/lib.rs:344-L434](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L344-L434)：**staged 贪心路径**（`greedy_argmax_ids` → `greedy_stage_readback` → `greedy_collect_resident`）。argmax 与 device 拷贝可被 CUDA Graph 捕获，pinned 回读排在图外、事后收集；两个槽让「上一步的回读」与「这一步的排队」重叠。全程限定 base stream（`ensure!(!has_stream_override())`，承接 u4-l1 的流覆盖机制）。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到「pad 列宽度参与路由」与「不参与路由」的行为差异。
2. **操作步骤**：
   - 有 GPU：运行新门禁测试
     ```bash
     cargo test --release -p pegainfer-sample --test select_batch \
       padded_arena_width_does_not_suppress_the_argmax_routing -- --nocapture
     ```
   - 无 GPU：精读 [pegainfer-sample/tests/select_batch.rs:180-L219](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L180-L219)，再执行 `git show 72cbbe8a -- pegainfer-sample/src/lib.rs` 对照 diff。
3. **需要观察的现象**：测试分对照/实验两组——`decodable = 256`，`vocab = 512`，行内两个 logit 8.0 的 bf16 平局 token（下标 128 与 200），`top_p = 1/256`：
   - 对照组 `SampleScratch::new(ctx, 512, 1)`（路由按 512）：`1/256 > 1/512`，不满足隐式贪心 → 走采样器，64 个 seed 里必然出现选中并列者（下标 200）的情况，断言 `sampled_the_peer` 为真；
   - 实验组 `with_selection_width(ctx, 512, 256, 1)`（路由按 256）：`1/256 <= 1/256` 恰好满足隐式贪心 → 走 argmax，64 个 seed 全部返回下标 128。
4. **预期结果**：测试通过。对照组断言的存在意义是防止实验组「假绿」——若 `with_selection_width` 偷懒没生效（仍按 arena 判定），两组行为相同，对照组的 `sampled_the_peer` 断言会失败。**待本地验证**（需要 GPU）。
5. 回答绑定问题：为什么 pad 列计入核判定会破坏确定性？因为该请求语义上是贪心的（核内只有 argmax 一个 token），被误路由进 rejection sampler 后，sampler 面对两个 bf16 并列最大值会按随机数挑一个——输出从「永远 token 128」变成「随 seed 在 128/200 间摇摆」。

#### 4.2.5 小练习与答案

**练习 1**：`with_selection_width(ctx, 248192, 248077, 16)` 与 `SampleScratch::new(ctx, 248077, 16)` 各自接受什么宽度的 arena？
**答案**：前者只接受 `hidden_dim == 248192` 的 arena（几何校验按 `vocab`），后者只接受 248,077。两者路由判定都用 248,077，所以对「哪些行走 argmax」给出完全一致的答案——这正是「padded arena 路由得和 unpadded 一样」的含义。

**练习 2**：若把 `selection_width` 误设为比真实可解码宽度小（例如把 qwen35 设成 248,064），会出什么错？
**答案**：路由阈值 \( 1/V \) 变大，贪心门变宽——`top_p` 在 \((1/248077, 1/248064]\) 区间的请求本应走采样器却被送去 argmax。这不破坏确定性，但改变了请求的语义（悄悄把采样请求变成贪心）。反过来设太大（如 248,192）才是 #1046 修掉的「确定性丢失」。两个方向都不对，所以构造器 `ensure!` 只能防越界，正确取值靠模型 config 保证。

**练习 3**：为什么 `BatchSamplingScratch`（采样侧缓冲）仍按 arena 宽度分配，而不是按 selection_width 缩小？
**答案**：FlashInfer 采样在完整 arena 上工作，pad 列靠模型侧的 \(-\infty\) 掩码天然出局（softmax 后概率为 0），不需要采样器知道边界的存在。路由阈值是唯一 pad 列可能「作恶」的地方，所以只有它改用 selection_width——修改面最小化。

### 4.3 select_batch 三路分发：批 argmax、FlashInfer 批量与 seeded 单行

#### 4.3.1 概念说明

`select_batch` 是整个 crate 的正门：把一个 logits arena 加一组每行参数变成每行一个 token id。它把行分成三组分别处理，**没有任何逐行逃生门**——调用者无法退化回 `for i { sample(i) }` 的低效写法（这是模块文档 [pegainfer-sample/src/lib.rs:6-L11](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L6-L11) 明说的设计目标）：

1. **argmax 行**（`effectively_greedy`）：一次批量**索引式** argmax——只算被选中的行，不是全 arena。
2. **无 seed 的采样行**：一次 `gpu_sample_batch_into` 批量调用（FlashInfer 的 temperature/top-k/top-p），min_p 行在其中被自动分区成独立的一趟。
3. **有 seed 的采样行**：每行一次单行调用，philox seed 由 `mix_seed(request_seed, step)` 混出——重放与批组合无关。

#### 4.3.2 核心流程

```text
select_batch(ctx, logits, params, steps, seed, scratch):
  校验: steps.len()==n, n<=max_rows, logits.seq_len>=n, hidden_dim==scratch.vocab
  路由: is_argmax(i) = effectively_greedy(params[i], scratch.selection_width)

  [第一路] greedy = { i | is_argmax(i) }
      H2D 行索引 -> argmax_batch_bf16_split_indexed_into（split 归约的批 argmax）
      -> D2H 到 pinned -> stream_spin_wait（自旋等本拷贝事件，顺带覆盖前面的内核）
      -> 按 greedy 顺序写回 tokens[row]

  [第二路] sampling_rows = { i | !is_argmax(i) && seed.is_none() }
      一次 gpu_sample_batch_into(全批, seed=引擎本步 seed)
      FlashInfer 内部: min_p 行分区为独立 pass；min_p-free 行走融合快速路径
      -> tokens[r.row] = sampled[r]

  [第三路] 对每个 { i | !is_argmax(i) && params[i].seed == Some(s) }
      单行 gpu_sample_batch_into(mix_seed(s, steps[i]))
      -> tokens[i] = sampled[0]
```

seed 语义分两层：**引擎 seed** 每步新鲜（启动时一个，逐步推进），无 seed 的行靠 philox 子序列（批内位置）去相关；**请求 seed** 则承诺 token 是 (seed, step, 分布) 的纯函数——FlashInfer 把批位置折进 philox 子序列，seeded 行若搭批量调用，它的随机流会随批组合漂移，所以必须单行调用（此时 blockIdx 恒为 0）。

`mix_seed` 用 SplitMix64 混 (seed, step)：

\[ z_0 = \text{seed} \oplus (\text{step} \cdot C_1),\quad z_{k+1} = (z_k \oplus (z_k \gg b_k)) \cdot C_{k+1} \]

其中 \( C_1 = \texttt{0x9E3779B97F4A7C15} \)（黄金比例常数）等三个奇常数，最后再异或一次移位输出。良好的雪崩特性保证相邻 (seed, step) 产生充分不同的 philox 种子。

#### 4.3.3 源码精读

第一路（批量索引 argmax）：

[pegainfer-sample/src/lib.rs:229-L263](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L229-L263)。行索引列表 H2D 上传后，`argmax_batch_bf16_split_indexed_into` 只对选中的行做 split 归约 argmax（partial 值/下标缓冲正是 scratch 里按 `argmax_batch_bf16_split_partials_len(max_rows, vocab)` 预分配的）。回读后按 `greedy[k]` 的行号散写回 `tokens`。等待用的是 `stream_spin_wait`——注释解释：自旋等本拷贝自身的事件，传递性地覆盖同流上先排的 argmax 内核，效果等同旧的整流同步；自旋还把调度器唤醒从步循环里拿掉。

第二路（批量 FlashInfer）：

[pegainfer-sample/src/lib.rs:268-L291](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L268-L291)。每行打包成 `BatchSamplingRow`，一次调用带回全部 token。`BatchSamplingRow` 的定义与 min_p 混批许可见 [pegainfer-kernels/src/ops/sampling.rs:14-L29](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/sampling.rs#L14-L29)；min_p 分区的动机见 [pegainfer-kernels/src/ops/sampling.rs:87-L93](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/sampling.rs#L87-L93)：min_p 内核的 u 缩放（`u * q`）与幸存者谓词和融合快速路径不同，混在一起会让 `min_p == 0` 的行采出与独行时不同的 token——分区放在这一层（而非调用者）才能对所有调用者统一保证「min_p==0 的行走原路径」。

第三路（seeded 单行）：

[pegainfer-sample/src/lib.rs:296-L318](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L296-L318)。`mix_seed(request_seed, steps[i])` 作 philox 种子，逐行单发。

`mix_seed` 本体：

[pegainfer-sample/src/lib.rs:441-L446](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L441-L446) 就是上文公式的逐行翻译。它公开导出，供自持贪心/采样路径的模型（Kimi-K2）复现同语义。

行为对照最好读测试：[mixed_batch_routes_and_places_each_row](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L94-L115) 验证一次调用里两路并行、token 各落各位；[seeded_rows_replay_independent_of_batch_position](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L328-L371) 验证 seeded 行换批位、换邻居、换引擎 seed 都重放出同一 token；[mixed_batch_keeps_plain_rows_on_the_fast_path](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/tests/select_batch.rs#L277-L323) 验证 min_p 邻居不扰动普通行的 token。

#### 4.3.4 代码实践

1. **实践目标**：量化 greedy 路径与 sampling 路径的耗时差，并验证混合批的路由正确性。
2. **操作步骤**（需要 GPU）：
   ```bash
   # 集成测试：路由、落位、scratch 复用、容量不变量
   cargo test --release -p pegainfer-sample --test select_batch -- --nocapture

   # 微基准：greedy / sampling / mixed × batch {1,8,32,64}，Qwen3 词表 151,936
   cargo bench -p pegainfer-sample --bench select_batch

   # 被忽略的延迟探针（#512）：span-17 行逐参数组打印 us/call
   cargo test --release -p pegainfer-sample --test select_batch \
     select_batch_span17_latency_probe -- --ignored --nocapture
   ```
   bench 的三种混合构造在 [pegainfer-sample/benches/select_batch.rs:45-L60](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/benches/select_batch.rs#L45-L60)：greedy 全 `p(0.0,-1,1.0)`（走第一路）、sampling 全 `p(1.0,50,0.9)`（走第二路）、mixed 一半一半——正好是任务要求的「temperature=0 与 top_p=0.9 混合批次」。
3. **需要观察的现象**：greedy 与 sampling 两条曲线的垂直距离随 batch 的变化；mixed 是否约等于两路之和（两次 pass 串行）还是低于之和（argmax 行被摘出后 sampling pass 变小）。
4. **预期结果**：greedy 恒为批量 argmax 的地板；sampling 显著高于 greedy（FlashInfer 三次内核 + softmax workspace，见 [pegainfer-kernels/src/ops/sampling.rs:79-L85](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/sampling.rs#L79-L85)）；mixed 介于两者之间。具体数值**待本地验证**。无 GPU 替代：精读 bench 源码，写出三种 mix 各触发的内核序列。
5. 用 `effectively_greedy` 复核 bench 参数：`p(0.0,-1,1.0)` 经 `is_greedy`（temperature < 1e-5）判真；`p(1.0,50,0.9)` 中 `0.9 > 1/151936` 判假——路由与你观察到的曲线分组一致。

#### 4.3.5 小练习与答案

**练习 1**：为什么 seeded 行不能搭第二路的批量调用？
**答案**：FlashInfer 把行在批内的位置折进 philox 子序列，同一请求 seed 在不同批组合下会产出不同随机流，重放承诺就破了。单行调用时 blockIdx 恒为 0，token 退化为 (seed, step, 分布) 的纯函数。

**练习 2**：`steps[i]` 参数对哪些行有意义？
**答案**：只对 `params[i].seed == Some` 的行有意义——它是请求本地解码步，参与 `mix_seed`。无 seed 的行根本不看它（第二路整批共用引擎本步 seed）。

**练习 3**：假设一个混合批里 8 行贪心、8 行采样，`select_batch` 内核层面发起了几次 argmax 调用、几次采样调用？
**答案**：各一次。argmax 行不论多少都汇成一次批量索引 argmax；无 seed 采样行汇成一次批量 FlashInfer（若含 min_p 行则再多一趟独立 pass）；seeded 行才逐行单发（本例为零）。

### 4.4 logprobs 双通道：主机参考与设备批量

#### 4.4.1 概念说明

采样之外，crate 还负责「解释」采样结果：客户端请求 logprobs 时，要算出被选 token 的对数概率、它的排名、以及 top-k 候选。两个实现互为镜像：

- `token_logprob_from_row`：主机单行参考实现，O(V) 一遍，只在请求要 logprobs 时运行。
- `token_logprobs_batch`：设备批量孪生，一次内核启动 + 紧凑回读。

#### 4.4.2 核心流程

单行 log-softmax：先取行最大值 \( m \)，再算

\[ \text{LSE} = m + \ln \sum_i e^{s_i - m} \]

被选 token 的 logprob 即 \( s_{\text{picked}} - \text{LSE} \)；排名按**原始 logit**比较（先算排名再减 LSE，f32 舍入就不会改变名次）；top-k 用插入排序维护降序前 k 个。指数和在 **f64** 里累加——16 万宽的词表用 f32 求和会丢精度。

#### 4.4.3 源码精读

[pegainfer-sample/src/lib.rs:473-L520](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L473-L520)：泛型 `T: Into<f32>` 让 Qwen 喂 f32、Kimi 直接喂 bf16 而无需加宽拷贝；空行或 picked 越界返回 None；sharded 词表的调用者必须先跨 rank 归并（注释援引 #236）。

[pegainfer-sample/src/lib.rs:533-L649](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L533-L649)：设备孪生。校验行号/picked 越界后，调 kernels 层 `logprob_topk_batch_bf16_into`，回读四组小数组（picked logprob、rank、top-k 值与 id，O(rows×(top_k+1))），一次 `ctx.sync` 收尾。注意它拒绝在流覆盖下运行（缓冲流量按主流排序）。

两个实现的对齐由纯主机单测钉住：[pegainfer-sample/src/lib.rs:657-L703](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-sample/src/lib.rs#L657-L703) 用 bf16 精确可表达的输入做解析断言（LSE、平局按升序 token id、k 超过词表截断、f32 输入与守卫）。

#### 4.4.4 代码实践

1. **实践目标**：验证主机 logprob 数学的正确性（不需要 GPU 运行时，但构建链需要 CUDA 工具链）。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-sample --lib -- --nocapture
   ```
3. **需要观察的现象**：三个单测（解析 log-softmax、k 超宽、f32 输入与守卫）全部通过。
4. **预期结果**：通过；其中 `token_logprob_matches_exact_log_softmax` 断言 top-3 顺序 `[1, 4, 2]`——两个 logit 3.0 的并列 token 按更小 id 在前。**待本地验证**（无 CUDA 工具链的机器连构建都过不去，此时退化为精读测试断言）。

#### 4.4.5 小练习与答案

**练习 1**：为什么指数和用 f64 而最大值修正用 f32 的 \( e^{s_i - m} \)？
**答案**：减去最大值把每项压进 \( (0, 1] \) 防溢出，这一步 f32 足够；但求和是 16 万项的累加，f32 的有效位只有约 7 位十进制，误差会直接进 LSE 进而进 logprob，所以累加器用 f64。

**练习 2**：`token_logprobs_batch` 为什么以 `ctx.sync()` 结尾，而 `select_batch` 的 argmax 路径用 `stream_spin_wait`？
**答案**：argmax 回读量小且在解码热路径上，自旋等待省掉调度器唤醒（#704 之后的取舍）；logprobs 是按需的低频路径，一次全量同步最简单，不值得优化。

## 5. 综合实践

**任务：完整跑一遍「宽度语义」的证据链，并量化两条采样路径。**

在有 GPU 的机器上（无 GPU 则每步换成对应精读并标注「待本地验证」）：

1. **推导**：以 248,077 为例手工推导 #1046 的对齐——248,077 不是 128 的倍数（248,064 = 1938×128 < 248,077 < 1939×128 = 248,192），故 selection_vocab 取 248,192，仍小于 checkpoint 权重的 248,320 行。画出三个宽度 `decodable_vocab / selection_vocab / vocab_size` 在 config、权重、decode_buffers、sampler 四层的传播图（本层只涉及最后两层：`with_selection_width(248192, 248077)`）。
2. **跑门禁**：
   ```bash
   cargo test --release -p pegainfer-sample --test select_batch -- --nocapture
   ```
   记录 `padded_arena_width_does_not_suppress_the_argmax_routing` 与 `tiny_top_p_routes_to_argmax_even_under_bf16_ties` 的行为差异：前者测「宽度语义」，后者测「平局下的确定性」。
3. **跑基准**：
   ```bash
   cargo bench -p pegainfer-sample --bench select_batch
   ```
   摘录 batch=32 时 greedy 与 sampling 的每调用耗时，算出比值；再跑 `select_batch_span17_latency_probe`（`--ignored --nocapture`）记录 greedy 与 `t0.8_topp0.95` 的 µs/call 差。
4. **复核路由**：对照 bench 的 `p(1.0, 50, 0.9)`，用 \( 1/151{,}936 \approx 6.58 \times 10^{-6} \) 手算确认它落在采样区间；再解释若 arena 被加宽到 303,872（翻倍），同样的参数是否改变路由（答案：否，0.9 远大于任何 \( 1/V \)——路由只对贴着 \( 1/V \) 边界的 top_p 敏感，而那正是门禁测试要把 top_p 钉在边界上的原因）。
5. **交付物**：一页笔记，含三宽度传播图、两条路径的耗时表、以及用自己的话解释「为什么 pad 列计入核判定会把 effectively-greedy 请求推进 rejection sampler 并破坏确定性」。

## 6. 本讲小结

- `pegainfer-sample` 是所有模型共用的采样策略层：内核与 FFI 在 `pegainfer-kernels`，参数契约 `SamplingParams` 在无 CUDA 的 `pegainfer-frontend`，本 crate 拥有路由策略、seed 语义、logprob 数学和复用 scratch。
- `SampleScratch` 按 `max_rows × vocab` 一次分配、跨步复用，贪心/非贪心缓冲不相交；pinned 双槽回读来自 #704 的 23.6 ms 事故教训；staged 贪心路径把可捕获的 argmax+拷贝与图外回读分离。
- `select_batch` 三路分发且无逐行逃生门：argmax 行一次批量索引 argmax（`stream_spin_wait` 收尾）、无 seed 采样行一次 FlashInfer 批量（min_p 行自动分区独立 pass）、seeded 行逐行单发 `mix_seed(seed, step)`（SplitMix64），重放与批组合无关。
- `effectively_greedy` 的数学：softmax 最大概率 \( \ge 1/V \)，故 `top_p <= 1/V` 时核内只剩 argmax——把这种隐式贪心行路由到 argmax 同时买到确定性与性能。
- #1046 的分离：arena 宽度（几何，缓冲与校验）与 selection_width（语义，路由阈值）分家；`with_selection_width` 保证加宽后的 arena 路由出与未加宽完全相同的行集合，否则贴边的 top_p 会把确定性请求送进在 bf16 平局上随机挑选的 rejection sampler。
- logprobs 一套两实现：主机 O(V) 参考（f64 累加、先排名后减 LSE）与设备批量孪生，由纯主机单测对齐。

## 7. 下一步学习建议

- **u8-l7（Qwen3.5 logits 宽度对齐）**是本讲 #1046 主题的模型侧下半场：`Config35::bound_selection_vocab` 如何把宽度向上对齐并 clamp 回 vocab_size、`output_logits_into` 为何把 GEMM 与 \(-\infty\) 掩码绑成唯一 logits 出口、以及为什么 HF golden 门禁对 pad 掩码天然失明。
- **u4-l5（CUDA Graph 基础设施）**：本讲的 staged 贪心路径、指针稳定 scratch 都是为图捕获解码服务的，读完那一讲再回头看 `greedy_stage_resident`/`greedy_collect_resident` 的双槽设计会更有体感。
- **u10-l1（测试体系）**：本讲的 `select_batch` 测试是「合成 arena、无模型权重」的门禁风格样本，测试哲学的完整图谱在那一讲。
- 想继续读源码的话，顺 [pegainfer-kernels/src/ops/sampling.rs](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/pegainfer-kernels/src/ops/sampling.rs#L79-L94) 下钻 FlashInfer 批量采样的 gather→softmax→sample 三内核结构，与 [docs/models/qwen35/decode-kernel-attribution.md](https://github.com/openinfer-project/openinfer/blob/72cbbe8a72e06329b2b4d6fa1e8e906acf2acc85/docs/models/qwen35/decode-kernel-attribution.md#L46-L62) 的归因方法。
