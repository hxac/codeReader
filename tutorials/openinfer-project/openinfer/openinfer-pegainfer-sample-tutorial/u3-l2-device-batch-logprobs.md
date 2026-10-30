# token_logprobs_batch：设备端批量孪生与它的防护栏

## 1. 本讲目标

上一讲（u3-l1）我们精读了主机端的单行参考实现 `token_logprob_from_row`：一次 O(V) 遍历算出 log-softmax、rank 和 top-k。本讲进入它的**设备端批量孪生** `token_logprobs_batch`。学完本讲，你应该能够：

1. 说出 `LogprobRequest` 三个字段（`row` / `picked` / `top_k`）各自的语义，以及入口处每一条校验守护的不变量。
2. 对一次调用**算出全部 8 个设备缓冲的元素个数**——三组 H2D 输入（`rows` / `picked` / `ks`，各 `requests.len()` 个 i32）、五组输出（`picked_lp`、`ranks` 各 `requests.len()` 个，`vals` / `ids` 各 `topk_len = requests.len() × k_max` 与 `.max(1)` 兜底）。
3. 解释为什么这个函数**每次调用临时分配**缓冲，而不像 `select_batch` 那样进 `SampleScratch` 长期持有。
4. 理解 `has_stream_override` 防护栏：为什么流覆盖激活时它必须**报错拒绝**而不是把工作排队到别的流上。
5. 复述分片词表警告：分片局部的 logsumexp 不是全局的，必须先跨 rank 归并（#236）。

## 2. 前置知识

- **log-softmax 与 LSE**（上一讲已建立）：一行 logits 的 logprob = 被选 logit − LSE，其中 LSE 需先做 max 平移防溢出。回顾公式：

  \[ \mathrm{logprob}(x_{\text{picked}}) = x_{\text{picked}} - \mathrm{LSE}(x), \qquad \mathrm{LSE}(x) = \max_i x_i + \ln\sum_i e^{x_i - \max_j x_j} \]

- **logits arena**：模型 crate 里未分片的全词表 logits 住在一个 `HiddenStates` 里——`seq_len` 行 × `hidden_dim` 列的 bf16 行主序设备缓冲，`hidden_dim` 就是词表宽度 V。
- **`TokenLogprob` 契约类型**：来自 `pegainfer_frontend::engine`，三个字段 `logprob: f32`、`rank: u32`、`top_logprobs: Vec<(u32, f32)>`（token id → logprob，按 logit 降序、并列按 id 升序）。
- **CUDA stream**：可以把流理解为 GPU 上的「按序执行队列」。同一个流里，先入队的拷贝/核函数先完成，天然有顺序保证；**不同流之间没有任何顺序保证**，需要事件显式同步。本讲的防护栏全部围绕这一点。
- **流覆盖（stream override）**：本仓库里的一些执行器会把当前线程的「生效流」临时切到别的流上（比如 qwen3 的 prefill/decode 双流切分），这是一个线程局部开关，详见 4.1.3。
- 不需要：CUDA 图（那是 u4-l1 的事）、pinned 内存细节（u4-l2 的事）。本讲的 D2H 用的是普通回读加整流同步。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs) | 本 crate 唯一源文件。`LogprobRequest`（L522–L529）与 `token_logprobs_batch`（L531–L649）是本讲主角；L463–L520 的 `token_logprob_from_row` 是它的正确性锚点 |
| [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs) | 7 个 GPU 集成测试：GPU-主机 A/B 互证 + 越界/流覆盖拒绝 + 批组合无关性 |
| [pegainfer-kernels/src/ops/sampling.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs) | 底层 `unsafe fn logprob_topk_batch_bf16_into`（L527–L606）：内核签名、第二层形状校验、真实 launch |
| [pegainfer-kernels/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs) | `has_stream_override`（L63–L66）、`active_cu_stream`（L73–L79）、`DeviceContext::sync`（L400–L404） |
| [pegainfer-qwen3/src/executor/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/logprobs.rs) | 真实调用方：Qwen3 如何按需组装 `LogprobRequest`（只有要求 logprob 的请求才进批） |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1** `LogprobRequest` 布局与入口校验（含流覆盖防护栏与分片警告）；**4.2** 一次 `logprob_topk_batch_bf16_into` 调用的 8 个缓冲与按需分配取舍；**4.3** D2H 回读与 `i*k_max+j` 布局的结果重组。

### 4.1 LogprobRequest 布局与入口校验

#### 4.1.1 概念说明

设备孪生与主机参考实现输入形状不同：主机版吃「一行裸切片」，设备版吃「整个 arena + 一份行级请求清单」。`LogprobRequest` 就是清单里的一行——它描述的是**一个问题**，不是数据：

> 「对 arena 的第 `row` 行，告诉我 `picked` 这个 token 的 logprob 和排名，顺便把这行 logits 的前 `top_k` 名也带回来。」

三个字段各有来头：

- `row: usize`——arena 行号。解码时一行就是一个活跃请求；prompt logprobs 时一行对应 prompt 里的一个位置。同一行可以出现在多个请求里（比如同一行问两个不同 `picked`）。
- `picked: u32`——被选 token。它通常不是这个函数自己选的，而是调用方刚用 `select_batch`（或贪心路径）采出来的 token：logprob 通道与采样通道是**两条平行通道**，采样决定「发什么」，logprob 事后解释「这个 token 有多可能」。
- `top_k: usize`——除了 `picked` 本身，还要这一行的前 k 名。对应 OpenAI 风格 API 里的 `top_logprobs` 参数；`0` 表示不要 top 名单，只要 logprob 和 rank。

结构体标注了 `#[derive(Clone, Copy, Debug)]`——纯数据小结构体，主机侧随手构造、按值传递。

#### 4.1.2 核心流程

`token_logprobs_batch` 入口的完整校验链，按代码顺序：

```
requests 为空?          ──是──> 直接返回空 Vec（空请求短路，不碰 GPU）
      │否
流覆盖激活?             ──是──> 报错拒绝（4.1.3 详述）
      │否
vocab（hidden_dim）== 0? ──是──> 报错（空词表无意义）
      │否
逐请求循环:
  ├─ r.row    >= seq_len?      ──是──> 报错（行号越界）
  ├─ r.picked >= vocab?        ──是──> 报错（token 越界）
  ├─ k = min(r.top_k, vocab)             （k 钳制，防止 k > V）
  ├─ k_max = max(k_max, k)               （本轮请求里的最大 k）
  └─ row/picked/k 逐一 i32::try_from     （防 usize > i32 上界）
      │全部通过
组装缓冲 → 一次 launch → 回读（4.2 / 4.3）
```

要点：`k` 的钳制发生在**入口**（`min(top_k, vocab)`），后续传给内核和结果重组用的都是钳制后的值——所以 `k_larger_than_vocab_is_clamped` 测试里 `top_k: 32` 对 `vocab: 8` 的行最终只返回 8 项。

#### 4.1.3 源码精读

先是 `LogprobRequest` 本体与函数文档：

[tests/gpu_logprobs.rs 无关，先看 lib.rs L522-L537](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L522-L537)

```rust
/// One row of a [`token_logprobs_batch`] call: the logprob of `picked` plus
/// the arena row's `top_k` entries.
#[derive(Clone, Copy, Debug)]
pub struct LogprobRequest {
    pub row: usize,
    pub picked: u32,
    pub top_k: usize,
}

/// Batched device twin of [`token_logprob_from_row`]: one launch and a
/// compact O(rows x (top_k + 1)) readback per call.
pub fn token_logprobs_batch(
```

文档里的「batched device twin」是本讲的题眼：主机版一次一行、一次 O(V)；设备版一次 launch 处理全部行，回读量只有 \( O(\text{rows} \times (k_{\max}+1)) \) 而不是 \( O(\text{rows} \times V) \)——对 15 万宽词表、多行 prompt logprobs，这就是这个孪生存在的理由。

再看入口三连（空短路、流覆盖拒绝、空词表拒绝）：

[lib.rs L538-L547](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L538-L547)

```rust
    if requests.is_empty() {
        return Ok(Vec::new());
    }
    ensure!(
        !has_stream_override(),
        "token_logprobs_batch: cannot run under a stream override — buffer \
         traffic is ordered on the primary stream"
    );
    let vocab = logits.hidden_dim;
    ensure!(vocab > 0, "token_logprobs_batch: empty vocab");
```

空请求短路排在最前：一个 `Vec::new()` 直接返回，连设备上下文都不碰——`rejects_out_of_range_requests` 测试专门断言了这一点（空清单是无操作，不是错误）。

**为什么流覆盖必须报错而不是排队？** 关键在错误信息那半句「buffer traffic is ordered on the primary stream」。看本函数后续的所有缓冲操作：H2D 上传用的是 `ctx.stream.clone_htod`、输出分配用的是 `ctx.stream.alloc_zeros`、回读用的是 `ctx.stream.clone_dtoh`——全部排在**主 stream**上；而内核 launch 走的是 `active_cu_stream(ctx)`，即「线程局部覆盖流（若激活）否则主 stream」（[tensor.rs L73-L79](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L73-L79)）。如果流覆盖激活时放行：

1. 输入 H2D 排在主 stream，内核却 launch 在覆盖流上——两条流无顺序保证，内核可能在 H2D 落地前就读 `rows/picked/ks`；
2. 结尾的 `ctx.sync()` 同步的只是 `ctx.stream`（[tensor.rs L400-L404](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L400-L404)），根本等不到覆盖流上的内核，回读会读到未写完的输出。

要正确支持覆盖流，得在两条流之间架事件依赖、把每次拷贝都改到覆盖流上——代价远超收益。所以契约干脆定成「仅限 base stream」：在入口用一个布尔检查把整类竞态挡掉。这不是本函数独有的纪律，分阶段贪心路径（u4-l1）的每个入口都有同款 `ensure!(!has_stream_override(), ...)`。真实世界里流覆盖确实会被激活——比如 qwen3 执行器的 prefill/decode 双流切分（[pegainfer-qwen3/src/executor.rs L453](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L453)、[L471](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L471)），所以这道护栏是会被真实触发的，测试也确实钉住了它（见 4.1.4）。

然后是逐请求校验循环：

[lib.rs L548-L569](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L548-L569)

```rust
    for r in requests {
        ensure!(
            r.row < logits.seq_len,
            "token_logprobs_batch: row {} out of bounds for arena of {} rows",
            ...
        );
        ensure!(
            (r.picked as usize) < vocab,
            "token_logprobs_batch: picked token {} out of bounds for vocab {vocab}",
            ...
        );
        let k = r.top_k.min(vocab);
        k_max = k_max.max(k);
        rows.push(i32::try_from(r.row)?);
        picked.push(i32::try_from(r.picked)?);
        ks.push(i32::try_from(k)?);
    }
```

注意循环在做校验的同时**顺手完成了主机侧的数据重组**：把 `usize`/`u32` 压成 i32 三个平行数组（`rows` / `picked` / `ks`），它们就是稍后三组 H2D 的载荷。`i32::try_from` 是最后一道保险——内核 API 用 i32 传形状，arena 真有大到 2^31 行的问题会在这里以错误而不是截断收场。

最后是**分片词表警告**。它写在主机参考实现的文档里，对设备孪生同样成立：

[lib.rs L471-L472](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L471-L472)

> The row must be the unsharded global vocab: a shard-local logsumexp is not
> the global one, so sharded callers must merge across ranks first (#236).

直觉：LSE 要对**整个词表**求和。若词表被 EP 分片，每个 rank 只握着一截，分片局部算出的 \(\ln\sum e^x\) 少了其他分片的贡献，logprob 会系统性偏高。数学上，设词表被切成 \( S_1, S_2 \)，全局 LSE 与任意分片局部 LSE 的关系是：

\[ \mathrm{LSE}(S_1 \cup S_2) = \max(m_1, m_2) + \ln\!\left(e^{m_1 - \max(m_1,m_2)} \cdot \Sigma_1 + e^{m_2 - \max(m_1,m_2)} \cdot \Sigma_2\right), \quad m_i = \max S_i,\ \Sigma_i = \sum_{x \in S_i} e^{x - m_i} \]

也就是说跨 rank 归并只需要交换每片的 `(max, 局部和)` 两个标量——但必须先归并。这正是模块文档里 Kimi-K2 的处境（[lib.rs L21-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L21-L25)）：它的贪心路径是分片局部 argmax 加跨 rank 归约，没法喂给「假设全词表」的 arena 接口，所以它的 logprob 走主机参考实现（配合归并后的行）。设备孪生的前提始终是：`logits.hidden_dim` 就是完整词表宽度。

#### 4.1.4 代码实践

**实践目标**：确认调用方真的按「按需、逐请求」的方式组装 `LogprobRequest`，并亲眼看一遍两条防护栏的拒绝行为。

**操作步骤**：

1. 打开 [pegainfer-qwen3/src/executor/logprobs.rs L19-L36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/logprobs.rs#L19-L36)，读 `gather_decode_logprobs`：注意 `req.logprobs.map(|top_k| LogprobRequest { row: ..., picked: rows.tokens[...], top_k })`——只有请求带 `logprobs` 参数才会产出一条请求，`picked` 直接取刚采出的 token。再读 [L91-L103](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/logprobs.rs#L91-L103) 的 `score_rows`：`Vec<Option<LogprobRequest>>` 先 `flatten` 成紧凑子集再一次性调用，返回结果再按 `Option` 摊回原位。
2. 打开 [tests/gpu_logprobs.rs L210-L261](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L210-L261) 的 `rejects_out_of_range_requests` 与 [L263-L281](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L263-L281) 的 `rejects_active_stream_override`，对照 4.1.3 的校验链逐条标注每个 `assert!(...is_err())` 命中的是哪一条 `ensure!`。
3. （需要 GPU，待本地验证）有 GPU 时可单跑：

   ```bash
   cargo test --release -p pegainfer-sample --test gpu_logprobs -- --nocapture
   ```

**需要观察的现象**：步骤 1 中「想 要 logprob 的请求」与「批里实际发出的请求」是两个不同的集合（后者是前者的过滤压缩）；步骤 2 中四个拒绝断言与入口校验一一对应，而空清单断言的是 `unwrap().is_empty()`（成功路径）。

**预期结果**：能画出「解码步 → 每请求 logprobs 参数 → LogprobRequest 子集 → token_logprobs_batch」的过滤链；能说出 `rejects_active_stream_override` 里 `StreamOverrideGuard::activate(ctx.stream.cu_stream())` 激活的覆盖流**就是主 stream 本身**——测试证明拒绝逻辑只看「覆盖是否激活」这个布尔，与覆盖流具体是哪条无关。

#### 4.1.5 小练习与答案

**练习 1**：同一个 `LogprobRequest { row: 5, picked: 100, top_k: 0 }`，`top_k: 0` 时函数还做有意义的事吗？

**答案**：做。`top_k = 0` 只表示不要 top 名单；`picked` 的 logprob 和 rank 照算（内核仍会写 `picked_lp[5]` 和 `ranks[5]`，只是该行在 `vals`/`ids` 里的槽位不被读回）。A/B 测试 `matches_host_reference_across_k_at_model_vocab` 的第一条请求正是 `top_k: 0`。

**练习 2**：`ensure!(r.row < logits.seq_len)` 校验的是 `seq_len` 这个**元数据字段**。如果调用方伪造了 `seq_len`（声称 4 行实际只有 1 行的底层存储），还有第二道防线吗？

**答案**：有。底层 `logprob_topk_batch_bf16_into` 会再算一遍 `backing = hidden_dim * seq_len` 并要求 `logits.data.len() >= backing`（[sampling.rs L544-L552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L544-L552)）。策略层校验语义（行号在声称范围内），内核层校验物理（底层数组真装得下），双层防御。`rejects_out_of_range_requests` 末段的 `inflated.seq_len = 4` 正是专门打第二道防线的（见综合实践）。

**练习 3**：为什么 `LogprobRequest` 里的 `picked` 由调用方传入，而不是函数内部自己 argmax？

**答案**：因为 logprob 通道的职责是**解释**而不是**选择**：被评估的 token 是采样通道（`select_batch` 或贪心路径）按请求的采样参数选出来的，可能是低概率 token。函数内部 argmax 只能回答「最可能的 token 有多可能」，无法回答「实际发出的这个 token 有多可能」——后者才是 API 语义（`logprobs` 指的是已生成 token 的对数概率）。

### 4.2 一次 logprob_topk_batch_bf16_into 调用：8 个缓冲与按需分配取舍

#### 4.2.1 概念说明

校验通过后，函数把一次调用需要的**全部**设备内存现场组装出来：三组 H2D 输入、五组输出缓冲，然后一次 `logprob_topk_batch_bf16_into` launch 搞定所有行。这一节解决两个问题：

1. 每个缓冲多长？（这是本讲的学习目标之一，下面直接给公式表。）
2. 为什么**每次调用现分配**，而不是像 `select_batch` 那样把缓冲搬进 `SampleScratch` 一次分配、长期持有？

第 2 个问题的答案有三层：

- **频率**：`select_batch` 每个解码步、每一行都要跑，是热路径，且解码路径被 CUDA 图捕获，图内烘焙了缓冲地址——指针必须稳定，所以必须 allocate-once。而 logprob 通道「只在请求方要求时运行」（主机参考实现的文档原话，[lib.rs L464](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L464)）：多数请求根本不带 `logprobs` 参数，这个函数常常整个请求生命周期都不被调用。为一条冷路径常驻显存不划算。
- **形状**：scratch 的两个维度（`max_rows`、`vocab`）在启动时就已知且固定；而 logprob 批的形状每次都不同——`requests.len()` 与 `k_max` 都随请求组合浮动。要入 scratch 就得按两个维度的最大值预留，进一步放大冷路径的常驻成本。
- **同步模型**：本函数以 `ctx.sync()` 收尾、在主机侧拼装结果，本来就是同步工具函数，不在 CUDA 图捕获范围内；走 CUDA 缓存分配器的临时分配对它足够便宜。

一句话：**scratch 的纪律是「热路径 + 图捕获 + 指针稳定」三件套催生的；logprob 通道一条都不沾，按需分配反而最简**。

#### 4.2.2 核心流程

设 `R = requests.len()`（行数），`k_max = max(min(r.top_k, vocab))`（本批最大钳制后 k）。一次调用的缓冲清单：

| 缓冲 | 方向 | 元素类型 | 长度公式 | 用途 |
|------|------|---------|---------|------|
| `rows_gpu` | H2D | i32 | `R` | 每条请求的 arena 行号 |
| `picked_gpu` | H2D | i32 | `R` | 每条请求的被评 token |
| `ks_gpu` | H2D | i32 | `R` | 每条请求钳制后的 k |
| `picked_lp_gpu` | 设备分配→D2H | f32 | `R` | 每条请求 picked 的 logprob |
| `ranks_gpu` | 设备分配→D2H | i32 | `R` | 每条请求 picked 的 rank |
| `vals_gpu` | 设备分配→D2H | f32 | `topk_len = R × k_max`（`.max(1)` 兜底） | top-k 的 logprob 值，`i*k_max + j` 布局 |
| `ids_gpu` | 设备分配→D2H | i32 | `topk_len`（同上） | top-k 的 token id，同布局 |

数据流（一次调用内）：

```
主机 rows/picked/ks (各 R 个 i32)
   │ clone_htod ×3（主 stream）
   ▼
rows_gpu / picked_gpu / ks_gpu ──┐
                                 ├─→ logprob_topk_batch_bf16_into（一次 launch）
logits arena（已在设备上，不搬运）─┘         │
                                    写入 picked_lp/ranks (各 R)
                                    写入 vals/ids    (各 R×k_max)
   │ clone_dtoh ×4（主 stream）
   ▼
主机 picked_lp / ranks / vals / ids ──ctx.sync()──→ 按 i*k_max+j 重组（4.3）
```

两个值得注意的细节：

- **logits 不上传**。arena 本来就在设备上，H2D 只有 3 个 `R` 元素的小 i32 数组——这也是「设备孪生」省钱的一部分：主机版要拿到行数据必须先 D2H 整行（V 个元素），设备版只回传答案。
- **`topk_len` 的 `.max(1)`**：若本批所有请求 `top_k` 都是 0（或都被钳成 0 不可能——钳制下限是 0），`k_max = 0`，`R × k_max = 0`。但内核签名**无条件**接收 `vals`/`ids` 指针（见 4.2.3），零长度的 `CudaSlice` 没有有效设备内存可指。`.max(1)` 保证至少分配 1 个元素让指针合法，读回时那一个元素也不会被任何人消费（重组循环只读 `0..ks[i]` 步）。

#### 4.2.3 源码精读

缓冲组装的完整代码：

[lib.rs L571-L599](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L571-L599)

```rust
    let htod = |data: &[i32]| -> Result<CudaSlice<i32>> {
        ctx.stream
            .clone_htod(data)
            .map_err(|e| anyhow!("token_logprobs_batch H2D failed: {e}"))
    };
    let rows_gpu = htod(&rows)?;
    let picked_gpu = htod(&picked)?;
    let ks_gpu = htod(&ks)?;
    let mut picked_lp_gpu: CudaSlice<f32> = ... alloc_zeros(requests.len()) ...;
    let mut ranks_gpu: CudaSlice<i32> = ... alloc_zeros(requests.len()) ...;
    let topk_len = requests
        .len()
        .checked_mul(k_max)
        .ok_or_else(|| anyhow!("token_logprobs_batch: rows * k_max overflows"))?
        .max(1);
    let mut vals_gpu: CudaSlice<f32> = ... alloc_zeros(topk_len) ...;
    let mut ids_gpu: CudaSlice<i32> = ... alloc_zeros(topk_len) ...;
```

（省略号处都是同构的 `ctx.stream.alloc_zeros(...).map_err(...)`，见链接原文。）三组 H2D 与五组分配全部排在 `ctx.stream` 主 stream 上——记住这一点，4.1.3 的防护栏才有落点。`checked_mul` 防的是 `R × k_max` 本身溢出 usize，与内核侧同款检查（[sampling.rs L566-L568](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L566-L568)）。

然后是 launch 本体：

[lib.rs L601-L616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L601-L616)

```rust
    // Indices validated and stream override rejected above.
    unsafe {
        logprob_topk_batch_bf16_into(
            ctx,
            logits.as_ref(),
            &rows_gpu,
            &picked_gpu,
            &ks_gpu,
            requests.len(),
            k_max,
            &mut picked_lp_gpu,
            &mut ranks_gpu,
            &mut vals_gpu,
            &mut ids_gpu,
        )?;
    }
```

那行注释就是 `unsafe` 的合同：底层函数是 `unsafe fn`，它信任调用方已保证行号/token 越界与流纪律——而这三样恰好在上面几十行里被 `ensure!` 过了。跨到 kernels crate 看被调用方的形状与第二层校验：

[sampling.rs L527-L539 与 L540-L574](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L527-L574)

```rust
pub unsafe fn logprob_topk_batch_bf16_into(
    ctx: &DeviceContext,
    logits: HiddenStatesRef<'_>,
    row_indices: &CudaSlice<i32>,
    picked: &CudaSlice<i32>,
    top_k: &CudaSlice<i32>,
    rows: usize,
    k_max: usize,
    out_picked_lp: &mut CudaSlice<f32>,
    out_picked_rank: &mut CudaSlice<i32>,
    out_topk_vals: &mut CudaSlice<f32>,
    out_topk_ids: &mut CudaSlice<i32>,
) -> Result<()> {
```

函数体里的 `ensure!` 群（`rows > 0`、arena 底层容量、三输入各 ≥ `rows`、两个 picked 输出各 ≥ `rows`、两个 top-k 输出各 ≥ `rows × k_max`）复核了**纯形状**不变量——它看不到「行号是否越界语义」，但能拦住「元数据与底层存储不一致」这类调用方 bug。launch 时内核拿到的是 `active_cu_stream(ctx)`（[sampling.rs L588-L603](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L588-L603)），这就是 4.1.3 里「覆盖流一旦激活就会产生跨流竞态」的机制根源。

#### 4.2.4 代码实践

**实践目标**：对两组具体参数手算全部 8 个缓冲的长度，检验公式表。

**操作步骤**（纸笔即可，无需 GPU）：

1. **情形 A**：`R = 4` 条请求，钳制后 k 分别为 `0, 1, 5, 20`（即 A/B 测试 `matches_host_reference_across_k_at_model_vocab` 的真实请求），vocab = 151_936。逐项写出 `rows_gpu` / `picked_gpu` / `ks_gpu` / `picked_lp_gpu` / `ranks_gpu` / `vals_gpu` / `ids_gpu` 的元素个数。
2. **情形 B**：`R = 3`，三条请求全是 `top_k: 0`。再算一遍，重点看 `topk_len`。
3. 对照 [lib.rs L587-L591](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L587-L591) 核对 `.checked_mul(k_max).max(1)` 的求值顺序。

**需要观察的现象**：情形 A 中 `vals_gpu` 与 `ids_gpu` 按 `k_max = 20`（批内最大值）统一加宽——k 只有 0 的那行也在 `vals`/`ids` 里占 20 个槽位（浪费但无害）；情形 B 中 `k_max = 0`，`topk_len` 落到 `.max(1)` 的兜底值 1。

**预期结果**：
- 情形 A：三组 H2D 各 4；`picked_lp_gpu`/`ranks_gpu` 各 4；`topk_len = 4 × 20 = 80`。
- 情形 B：三组 H2D 各 3；`picked_lp_gpu`/`ranks_gpu` 各 3；`topk_len = 3 × 0 = 0 → max(1) → 1`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `vals`/`ids` 按 `R × k_max` 统一分配，而不是按 `sum(ks[i])` 紧凑分配？

**答案**：统一步长让内核与主机重组都能用 `base = i * k_max` 一个乘法定位每行槽位，无需前缀和；代价是 k 较小的行浪费 `(k_max − k)` 个槽。logprob 是冷路径，用一点尾部空洞换索引简单是正确取舍。紧凑布局还得把每行偏移额外上传给内核。

**练习 2**：如果把 `token_logprobs_batch` 的缓冲搬进某个长期持有的 scratch，最先撞上什么问题？

**答案**：形状不可预知——`requests.len()` 与 `k_max` 每次调用都随请求组合浮动，scratch 只能按两个维度的最大值预留。而这条路径本来就「只在请求方要求时运行」，绝大多数调用根本不发生，常驻显存纯属浪费；它也不在 CUDA 图捕获范围内，享受不到指针稳定的必要性（那是 `select_batch`/scratch 的动机）。

**练习 3**：`unsafe` 块上方那行注释「Indices validated and stream override rejected above」起到了什么作用？

**答案**：它是 Rust `unsafe` 合同的书面形式——`logprob_topk_batch_bf16_into` 是 `unsafe fn`，安全性由调用方保证；注释把「保证了什么、在哪保证」钉在调用点，让这两个函数之间的责任边界可审计。被调用方仍保留纯形状的第二层 `ensure!`，双层防御但职责不同。

### 4.3 D2H 回读与结果重组：`i*k_max + j` 布局

#### 4.3.1 概念说明

launch 之后设备上躺着的全是答案，但请求方要的是主机上的 `Vec<TokenLogprob>`。这一步做三件事：**回读**（四组 D2H）、**同步**（一次 `ctx.sync()`）、**重组**（把平行数组拼回契约类型）。核心是理解 `vals`/`ids` 的行主序布局：

```
vals / ids（各 R×k_max 个元素，行主序，步长 k_max）:

行 0:  [0*k_max + 0] [0*k_max + 1] ... [0*k_max + k_max-1]
行 1:  [1*k_max + 0] [1*k_max + 1] ...
...
行 i:  base = i*k_max, 有效前 ks[i] 个, 尾部是未写/未读的空洞
```

对第 i 条请求，重组读的是 `picked_lp[i]`、`ranks[i]`、以及 `vals[base + j]` / `ids[base + j]`（`j ∈ 0..ks[i]`）。注意用的是**主机侧留存的 `ks[i]`**（钳制后的 k），不是原始 `top_k`。

#### 4.3.2 核心流程

```
四组 clone_dtoh（主 stream，按序排队）:
  picked_lp_gpu(R) → picked_lp: Vec<f32>
  ranks_gpu(R)    → ranks:    Vec<i32>
  vals_gpu(L)     → vals:     Vec<f32>     ┐ L = topk_len
  ids_gpu(L)      → ids:      Vec<i32>     ┘
ctx.sync()   ←── 全函数唯一同步点：等主 stream 上 launch + 四份拷贝全部完成
重组: for i in 0..R:
  k = ks[i] as usize; base = i * k_max
  TokenLogprob {
    logprob: picked_lp[i],
    rank:    ranks[i] as u32,
    top_logprobs: (0..k).map(|j| (ids[base+j] as u32, vals[base+j])).collect(),
  }
```

回读量核算（这正对应函数文档里的 \( O(\text{rows} \times (k_{\max}+1)) \)）：每行 1 个 f32 logprob + 1 个 i32 rank + \( 2k_{\max} \) 个 top-k 元素，共 \( R(2 + 2k_{\max}) \) 个元素 = \( 8R(k_{\max}+1) \) 字节。对比把整行 D2H 回主机再算（\( R \times V \times 2 \) 字节，V = 15 万时约 300 KB/行），差距三个数量级——这就是「compact readback」的含义。

#### 4.3.3 源码精读

回读与同步：

[lib.rs L618-L634](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L618-L634)

```rust
    let picked_lp = ctx
        .stream
        .clone_dtoh(&picked_lp_gpu)
        .map_err(|e| anyhow!("token_logprobs_batch D2H failed: {e}"))?;
    let ranks = ... clone_dtoh(&ranks_gpu) ...;
    let vals = ... clone_dtoh(&vals_gpu) ...;
    let ids = ... clone_dtoh(&ids_gpu) ...;
    ctx.sync()?;
```

四份拷贝都在主 stream 上，彼此按序；`ctx.sync()` 是整函数唯一的同步点——它内部就是 `self.stream.synchronize()`（[tensor.rs L400-L404](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L400-L404)），一个等待同时覆盖 launch 与四份拷贝。与 `select_batch` 贪心分支的 `stream_spin_wait` 精细化等待（u4-l2 的主题）相比，这里选择了最朴素的整流同步——再次印证「冷路径从简」。

重组循环：

[lib.rs L636-L648](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L636-L648)

```rust
    Ok((0..requests.len())
        .map(|i| {
            let k = ks[i] as usize;
            let base = i * k_max;
            TokenLogprob {
                logprob: picked_lp[i],
                rank: ranks[i] as u32,
                top_logprobs: (0..k)
                    .map(|j| (ids[base + j] as u32, vals[base + j]))
                    .collect(),
            }
        })
        .collect())
```

四行核心：`k = ks[i]`（每行自己的 k，不是 k_max）、`base = i * k_max`（统一步长定位）、三元组拼装、`k = 0` 时 `top_logprobs` 自然为空。

重组的契约由 A/B 测试钉死。测试文件头两句就是方法宣言：

[gpu_logprobs.rs L1-L2](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L1-L2)

> GPU-vs-host A/B: `token_logprobs_batch` must match `token_logprob_from_row`
> on the same bf16 rows — top-k ids exactly, values within `TOL`. Needs a GPU.

也就是：**top-k 的 id 序列必须逐位相等，数值容差 `TOL = 5e-5`**（[L13](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L13)）。`assert_matches_host`（[L37-L55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L37-L55)）把这条规则落到 rank、logprob、id 序列、每个 top logprob 四个维度。两个最值得读的钉子：

- **乱序行与批组合无关性**：`indexes_arena_rows_out_of_order`（[L161-L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L161-L189)）让请求引用行 3、1、3；`outputs_are_independent_of_batch_composition`（[L283-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L283-L336)）让同一行「单独成批」与「夹在 3 行批中间」产出**按位相同**（`to_bits()` 比较）的结果——logprob 是确定性的纯函数，不像采样那样有种子问题，这里连比特都不许变。
- **并列与 k 边界**：`tie_ordering_matches_host`（[L94-L159](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L94-L159)）造了两个精确并列的峰值层（三个 8.0、四个 6.5），在 k = 2/3/5/7 边界上断言 id 序列恰为 `[77, 901, 2048, 15, 512]`（降序值、并列按 id 升序）、rank 把三个并列峰全算进去（rank = 3）。设备核与主机参考实现必须在**破并列规则**上也逐位一致——这是把 u3-l1 讲过的稳定序契约搬到了设备侧验证。

#### 4.3.4 代码实践

**实践目标**：手工跟踪一次真实调用的 `i*k_max + j` 寻址，确认自己真的理解布局。

**操作步骤**（纸笔）：

1. 取 `indexes_arena_rows_out_of_order` 的请求（[L168-L184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L168-L184)）：三条请求 `(row:3, k:4)`、`(row:1, k:2)`、`(row:3, k:1)`，vocab = 8192。
2. 写出 `R`、钳制后各 `ks[i]`、`k_max`、`topk_len`。
3. 对每条请求 i 算 `base = i * k_max`，标出它在 `vals`/`ids` 里占用的下标区间与有效长度。
4. （需要 GPU，待本地验证）单跑该测试对照：

   ```bash
   cargo test --release -p pegainfer-sample --test gpu_logprobs indexes_arena_rows_out_of_order -- --nocapture
   ```

**需要观察的现象**：第 0 条与第 2 条请求引用**同一个 arena 行 3** 但 k 不同（4 vs 1）；它们在输出数组里各占 `k_max` 宽的独立槽位，互不干扰。

**预期结果**：`R = 3`，`ks = [4, 2, 1]`，`k_max = 4`，`topk_len = 12`；槽位区间：请求 0 → `[0, 4)` 有效 4，请求 1 → `[4, 8)` 有效 2（`[6, 8)` 空洞），请求 2 → `[8, 12)` 有效 1（`[9, 12)` 空洞）。

#### 4.3.5 小练习与答案

**练习 1**：`ctx.sync()` 若被删掉，最可能的故障形态是什么？

**答案**：四份 `clone_dtoh` 的结果在主机侧被读时，主 stream 上的 launch/拷贝可能尚未完成，读到未写满的输出——典型的静默数据错乱而非崩溃。因为拷贝在主 stream 上按序排队，删掉 sync 只丢「等待」，不丢「顺序」，所以表现为偶发、与负载时序相关的错误值（这类 bug 最难复现）。

**练习 2**：`outputs_are_independent_of_batch_composition` 为什么用 `to_bits()` 比较 f32 而不是 `abs(a-b) < eps`？

**答案**：它要钉的是**更强**的命题：同一行的输出不仅近似相等，而且按位相同——logprob 通道没有随机源，同一行进不进批、批里第几位，都不该改变哪怕一个舍入位。`to_bits()` 把「一个比特都不许动」写成断言；`abs(a-b) < eps` 会放过最后一位的漂移，钉不住这个不变量。

**练习 3**：为什么重组时用 `ks[i]` 而不是原始 `requests[i].top_k` 截断 `top_logprobs`？

**答案**：两个原因。其一，`ks[i] = min(top_k, vocab)` 是钳制后的值，内核也只写了 `ks[i]` 个有效项，用它截断才与设备产出一致（k > vocab 的行没有更多可读的）。其二，`ks` 是入口循环已经在主机侧留存的数组，零额外成本；若用原始 `top_k`，`k > vocab` 时会读到槽位空洞甚至越界。

## 5. 综合实践

把本讲全部内容串成一张图和两个追问。全程纸笔即可完成，GPU 部分标注为待本地验证。

### 任务：绘制 `token_logprobs_batch` 一次调用的完整数据流图

**步骤 1 —— 画图**。以 `R` 条请求、批内最大钳制 k 为 `k_max` 作参数，画出从 `requests: &[LogprobRequest]` 到 `Vec<TokenLogprob>` 的完整数据流，必须标注：

- 入口校验链的四个拒绝点 + 空短路（内容写进图旁的图例）；
- 三组 H2D（`rows` / `picked` / `ks`，各 `R` 个 i32，`ctx.stream.clone_htod`）；
- 五组设备缓冲（`picked_lp`、`ranks` 各 `R`；`vals`、`ids` 各 `topk_len = R × k_max` 经 `.checked_mul` 与 `.max(1)`）；
- 唯一一次 launch（`logprob_topk_batch_bf16_into`，行内标注它读 arena、写五组输出）；
- 四组 D2H（`ctx.stream.clone_dtoh`）与 **`ctx.sync()` 的位置**——画在四份拷贝之后、重组循环之前；
- 重组的 `base = i * k_max` 与 `j ∈ 0..ks[i]` 寻址。

**步骤 2 —— 回答两个追问**（答案依据都在源码里）：

**追问 A**：`rejects_out_of_range_requests`（[L246-L260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L246-L260)）先造一个 1 行的 arena，再手动 `inflated.seq_len = 4`、请求 `row: 3`。为什么这是一个有效的测试手法？

参考答案：`seq_len` 是 `HiddenStates` 上的普通元数据字段，这一改伪造出的正是「元数据声称 4 行、底层存储只有 1 行」的不一致状态。它有效的三层理由：① 策略层的 `ensure!(r.row < logits.seq_len)` 只看元数据，`row: 3 < 4` 会被放过——单测第一段（row: 2 对 2 行 arena）打的是这一层，测不到更深；② 把行号合法化之后，命中内核层的 `logits.data.len() >= hidden_dim × seq_len` 容量检查（[sampling.rs L544-L552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L544-L552)），从而证明第二道防线真实存在且可达；③ 这类「arena 别名/元数据与底层脱节」的 bug 在真实调用方是可能发生的，直接改字段是构造它的最小手段——不需要真的分配一个大 arena。

**追问 B**：流覆盖激活时，为什么 `token_logprobs_batch` 必须**报错**而不是把工作排队到覆盖流上？

参考答案：它的全部缓冲流量（H2D、分配、D2H）都排在主 stream `ctx.stream` 上，而内核 launch 走 `active_cu_stream(ctx)`——覆盖激活时这是另一条流。放行会产生两处竞态：内核可能在 H2D 落地前读输入；结尾 `ctx.sync()` 只同步主 stream（`stream.synchronize()`），等不到覆盖流上的内核，回读取到未写完的输出。要正确支持就得把所有拷贝改道 + 架跨流事件依赖，成本远超一条冷路径的收益，所以契约定为 base-stream-only、入口拒绝。`rejects_active_stream_override`（[L263-L281](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L263-L281)）里激活用的覆盖流甚至就是主 stream 本身——证明拒绝只认「覆盖激活」这个布尔状态，不看你覆盖到哪条流。

**步骤 3 ——（可选，需要 GPU，待本地验证）**：跑全量 GPU A/B 并试着让其中一条失败（例如把 `TOL` 收紧到 1e-7 观察 GPU-主机浮点差异在什么量级；或给某条请求加一个越界 `picked` 看错误信息落点是策略层还是内核层）：

```bash
cargo test --release -p pegainfer-sample --test gpu_logprobs -- --nocapture
```

## 6. 本讲小结

- `LogprobRequest { row, picked, top_k }` 是一行「问题」：`picked` 由调用方传入（通常是刚采出的 token），logprob 通道解释选择，不做选择。
- 入口校验链：空请求短路 → 流覆盖拒绝 → 空词表拒绝 → 逐请求行号/token 越界与 k 钳制；内核层还有一道纯形状的第二防线（元数据 vs 底层容量）。
- 8 个缓冲的长度公式：三组 H2D 各 `R` 个 i32；`picked_lp`/`ranks` 各 `R`；`vals`/`ids` 各 `topk_len = R × k_max`（`.max(1)` 保证零 k 批也有合法指针）。
- 按需分配而非 scratch：logprob 只在请求方要求时运行、批形状（R 与 k_max）逐次浮动、且不在 CUDA 图捕获内——三条都推翻了 allocate-once 的动机。
- 回读是「compact」的 \( O(R(k_{\max}+1)) \)，logits 全程留在设备上；`i*k_max + j` 统一步长布局让重组只需一个乘法定位。
- 契约由 GPU-主机 A/B 钉死：top-k id 序列逐位相等、数值容差 5e-5、并列按 id 升序、输出按位独立于批组合；分片词表必须先跨 rank 归并 LSE（#236），Kimi-K2 因此走主机参考实现。

## 7. 下一步学习建议

- **u4-l1（CUDA 图友好的分阶段贪心路径）**：本讲看到了「logprob 冷路径从简、整流同步」的一面；下一讲看热路径的另一面——图内指针稳定、pinned 双槽排队回读、`stream_spin_wait` 事件级等待，两讲对照着读最能体会取舍。
- **u4-l2（固定内存、流同步与 #704 教训）**：本讲的 `ctx.sync()` 是最粗粒度的等待；#704 讲的正是回读同步语义如何把亚毫秒调用拖成 23.6ms 的故事，是本讲同步话题的深化。
- 继续读源码的话，推荐两条线：① [pegainfer-qwen3/src/executor/logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/logprobs.rs) 的 `score_prompts`（L105-L123）——prompt logprobs 如何把多行多位置压成一批 `LogprobRequest`，是本讲 API 的最大规模用例；② [pegainfer-kernels/src/ops/sampling.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs) 里 `logprob_topk_batch_bf16_into` 旁边的 `markov_step_argmax_into`（L608 起），看同一个 crate 里批式内核的另一种「请求主序」布局长什么样。
