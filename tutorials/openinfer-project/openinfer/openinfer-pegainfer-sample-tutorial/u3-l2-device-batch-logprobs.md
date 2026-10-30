# token_logprobs_batch：设备端批量孪生与它的防护栏

## 1. 本讲目标

上一讲（u3-l1）我们精读了主机端单行参考实现 `token_logprob_from_row`：一次 O(V) 遍历算出 log-softmax、rank 和 top-k。它是「正确性锚点」，但如果每个解码步都对 16 万宽的词表逐行做主机计算，代价是把整行 logits 从 GPU 搬下来再在 CPU 上扫一遍——批越大越不可承受。

本讲精读它的设备端批量孪生 `token_logprobs_batch`：一次内核 launch 服务整批请求，只回读一份紧凑的 \( O(\text{rows} \times (k_{\max}+1)) \) 结果。学完本讲你应该能够：

1. 算出一次调用中每块设备缓冲的元素个数（三组 i32 输入、四组输出，其中 top-k 两组的长度是 `max(1, rows × k_max)`）。
2. 解释为什么这个函数每次调用现分配临时缓冲，而不像 `select_batch` 那样用常驻 `SampleScratch`。
3. 读懂结果重组时的 `i*k_max + j` 布局，以及 `ctx.sync()` 在数据流中的位置。
4. 复述它的三道防护栏：空请求短路、逐条越界拒绝（以及下层内核对「说谎的 `seq_len`」的兜底校验）、stream override 激活时直接报错。
5. 复述分片词表警告：分片局部的 logsumexp 不是全局的，必须先跨 rank 归并。

## 2. 前置知识

- **logits arena**：模型执行完最后 projections 后的二维 bf16 矩阵 `HiddenStates { data, hidden_dim, seq_len }`——`hidden_dim` 是词表宽度（每行一个 token 的 logit），`seq_len` 是行数（每行一个序列位置/请求）。本讲的「行号」都是指 arena 的行下标。
- **log-softmax 与 LSE**：被选 token 的 logprob = 被选 logit − LSE（log-sum-exp），其中 \[ \mathrm{LSE}(x) = \log \sum_v e^{x_v - \max(x)} + \max(x). \] u3-l1 已推导过 max 平移防溢出与 f64 累加的必要性，本讲直接沿用。
- **H2D / D2H**：host-to-device / device-to-host 的内存拷贝。`clone_htod` 把主机 `Vec` 上传成一块新的 `CudaSlice`；`clone_dtoh` 把设备缓冲拷回一个新的主机 `Vec`；`alloc_zeros` 在设备上分配清零缓冲。
- **stream override（流覆盖）**：`pegainfer-kernels` 里一个线程本地的开关（`StreamOverrideGuard`），激活后 `active_cu_stream(ctx)` 返回覆盖流而不是 `DeviceContext` 自带的主流——用于 Green Context SM 分区等并发车道。内核 launch 走 `active_cu_stream`，而 `ctx.stream.alloc_zeros` / `clone_htod` / `clone_dtoh` 永远走主流。
- **CUDA 图与指针稳定**：解码热路径的缓冲必须一次分配、地址固定（u2-l2 讲过 `SampleScratch` 的动机），否则图捕获会失效。
- **分片词表（sharded vocab）**：张量并行/专家并行下，词表按 rank 切片，每个 rank 只看到一部分 logit 列。Kimi-K2 就属于这类（见 u1-l1 的例外说明）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs) | 本讲主体：`LogprobRequest`（L522-L529）、`token_logprobs_batch`（L533-L649）；顶部模块文档交代分层与 Kimi-K2 例外（L1-L25） |
| [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs) | 7 个需要 GPU 的集成测试：GPU-主机 A/B 互证 + 越界/流覆盖拒绝 + 批组合无关性 |
| [../pegainfer-kernels/src/ops/sampling.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs) | 底层 `pub unsafe fn logprob_topk_batch_bf16_into`（L527-L606）：形状校验 + FFI launch，是本函数的唯一内核入口 |
| [../pegainfer-kernels/src/tensor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs) | `HiddenStates`（L551-L555）、`StreamOverrideGuard`/`has_stream_override`（L18-L66）、`ctx.sync()`（L400-L404） |
| [../pegainfer-gemma4/src/engine.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs) | 一个真实调用方：按请求是否要求 logprobs 过滤出 `LogprobRequest`（L987-L1005），印证「按需运行」 |

## 4. 核心概念与源码讲解

### 4.1 LogprobRequest：请求布局与入口校验

#### 4.1.1 概念说明

`token_logprobs_batch` 的调用方不传「整行都要」，而是传一个精简的请求列表：每条请求说清「我要 arena 的第几行、被选中的是哪个 token、顺便给我这行的 top-k」。`LogprobRequest` 就是这个三字段 POD：

- `row: usize`——arena 行号（一个 arena 行可以被多条请求引用，同一行请求不同 picked/top_k 也行）；
- `picked: u32`——已经采样定的 token id（先采样后打分，见 4.2.1 调用方时序）；
- `top_k: usize`——要的 top-k 个数，`0` 表示只要 logprob 和 rank，不要 top 列表。

函数入口的职责是把这份「意图列表」变成可执行且可信任的输入：空列表短路、逐条越界拒绝、k 截断到词表宽度、`usize → i32` 收窄。

#### 4.1.2 核心流程

```text
requests 为空？ ──是──▶ 返回 Ok(vec![])
     │否
stream override 激活？ ──是──▶ 报错（见 4.4）
     │否
vocab == 0？ ──是──▶ 报错
     │否
对每条 r ∈ requests：
    r.row    ≥ seq_len ？ ──是──▶ 报错（行越界）
    r.picked ≥ vocab   ？ ──是──▶ 报错（token 越界）
    k = min(r.top_k, vocab)          ← 截断
    k_max = max(k_max, k)            ← 累积批内最大 k
    rows.push(row as i32)、picked.push、ks.push(k)
    （usize→i32 转换失败也直接以错误冒泡）
```

#### 4.1.3 源码精读

请求结构体本身就三个 pub 字段，语义全部由文档注释钉死——[src/lib.rs:522-529](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L522-L529) 定义 `LogprobRequest`：一条 `token_logprobs_batch` 请求 = 被选 token 的 logprob + 该 arena 行的 top-k 条目。

入口的三段校验在 [src/lib.rs:538-547](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L538-L547)：`requests.is_empty()` 直接返回空 `Vec`（调用方不必特判）；`has_stream_override()` 时 `ensure!` 报错，错误信息原文是 "cannot run under a stream override — buffer traffic is ordered on the primary stream"（为什么必须报错而不是排队，见 4.4）；`vocab > 0` 拒绝空词表。

逐条校验循环在 [src/lib.rs:548-569](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L548-L569)：`r.row < logits.seq_len` 与 `(r.picked as usize) < vocab` 两条 `ensure!` 各自给出带具体数值的错误消息；随后 `let k = r.top_k.min(vocab)` 把 k 截断到词表宽度（所以 `k_max ≤ vocab` 恒成立，top-k 缓冲不会因调用方传了一个荒谬的 `top_k = 10^9` 而爆掉）；最后三个 `i32::try_from(..)?` 把 `usize/u32` 收窄成内核需要的 i32——收窄失败（数值超过 21 亿）会作为 `anyhow` 错误直接冒泡，而不是截断成错误答案。

注意一个细节：主机侧的 `ks` 向量（截断后的 k）在函数尾部的组装阶段还会被再次读用（4.3.3），它兼任「组装元数据」。

#### 4.1.4 代码实践

**实践目标**：不运行代码，仅凭源码推演校验结果，训练「读 ensure! 判结果」的能力。

**操作步骤**：

1. 假设 arena 是 `seq_len = 2, hidden_dim = 64`（即 2 行 × 64 列），逐条预测以下请求会通过、报错，还是被静默修改：
   - `LogprobRequest { row: 0, picked: 63, top_k: 1 }`
   - `LogprobRequest { row: 2, picked: 0, top_k: 1 }`
   - `LogprobRequest { row: 0, picked: 64, top_k: 1 }`
   - `LogprobRequest { row: 1, picked: 0, top_k: 100 }`
   - `[]`（空切片）
2. 到 [src/lib.rs:538-569](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L538-L569) 逐条核对。
3. 对照 [tests/gpu_logprobs.rs:210-261](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L210-L261) 的 `rejects_out_of_range_requests`，看测试作者选的越界值（`picked: 64`、`row: 2`）与你预测的报错边界是否一致。

**需要观察的现象 / 预期结果**：前两条边界值恰好是「最大合法值」与「最小非法值」；`top_k: 100` 不报错而是被截成 64；空切片返回空结果。本实践是纸面推演，无需 GPU，也无需运行（如想真正运行对应测试需要 GPU，待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `picked` 用 `u32` 而 `row`、`top_k` 用 `usize`？

**答案**：`picked` 是 token id，与采样路径的返回类型 `Vec<u32>`、`TokenLogprob.top_logprobs` 里的 `(u32, f32)` 对齐，跨crate 传输时定宽；`row`/`top_k` 是内存下标和元素个数，Rust 惯例用 `usize`，进内核前统一收窄成 i32。

**练习 2**：同一行被两条请求引用（`row: 0` 出现两次、picked 不同）合法吗？

**答案**：合法。校验只看单条请求是否越界，不要求行号唯一；[tests/gpu_logprobs.rs:57-92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L57-L92) 的测试里 `row: 0` 就出现了两次（picked 分别为 7 和 0、top_k 分别为 0 和 20）。

**练习 3**：`k = r.top_k.min(vocab)` 之后才累积 `k_max`，这个顺序换掉会怎样？

**答案**：先累积会把未截断的原始 `top_k` 记进 `k_max`，导致 top-k 输出缓冲按一个可能远超词表的长度分配（浪费显存甚至 OOM），而实际每行只写得出最多 `vocab` 条。

### 4.2 缓冲区组装与一次 launch：长度公式与「不入 scratch」的取舍

#### 4.2.1 概念说明

校验通过后，函数把主机侧的三个 `Vec<i32>` 上传成三块设备输入缓冲，再分配四块设备输出缓冲，然后**一次** `logprob_topk_batch_bf16_into` launch 服务整批。一次调用共触及 8 块设备缓冲（3 输入 + 4 输出 + 1 块只读复用的 logits arena）。

为什么不像 `select_batch` 那样把这些缓冲放进常驻 `SampleScratch`？因为 logprob 是**按需路径**：只有在请求显式要求 logprobs 时才会运行（调用方 gemma4 的 engine 里，`spec.logprobs` 为 `None` 的行根本不产生 `LogprobRequest`，请求列表为空时整个调用被跳过——见 [../pegainfer-gemma4/src/engine.rs:987-1005](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L987-L1005)，`filter_map` 只为 `spec.logprobs = Some(top_k)` 的行造请求，`if !requests.is_empty()` 才调用）。为一条多数请求不走的路径常驻一块按最大形状预留的显存，既浪费又要面对「形状每次都不同」的现实：行数是**本次**要 logprob 的行数，`k_max` 是**本次**截断后 k 的最大值。所以这里宁可每次现分配——几微秒级的分配开销，换来零常驻 footprint，也不把临时分配的语义混进解码路径的指针稳定纪律。

#### 4.2.2 核心流程

记 \( n = \text{requests.len()} \)，\( k_{\max} \) 为截断后批内最大 k，\( V = \text{hidden\_dim} \)。各缓冲长度：

| 缓冲 | 方向 | 元素类型 | 长度（元素个数） | 用途 |
|------|------|----------|------------------|------|
| `rows_gpu` | H2D | i32 | \( n \) | 每条请求的 arena 行号 |
| `picked_gpu` | H2D | i32 | \( n \) | 每条请求的被选 token |
| `ks_gpu` | H2D | i32 | \( n \) | 每条请求截断后的 k |
| `picked_lp_gpu` | 设备输出 | f32 | \( n \) | 每条请求被选 token 的 logprob |
| `ranks_gpu` | 设备输出 | i32 | \( n \) | 每条请求被选 token 的 rank |
| `vals_gpu` | 设备输出 | f32 | \( \max(1,\; n \cdot k_{\max}) \) | top-k 的 logprob 值，`i*k_max+j` 布局 |
| `ids_gpu` | 设备输出 | i32 | \( \max(1,\; n \cdot k_{\max}) \) | top-k 的 token id，同上布局 |
| logits arena | 只读输入 | bf16 | \( \text{seq\_len} \times V \)（调用方已持有） | 打分的原始行 |

其中 \[ \text{topk\_len} = \max\bigl(1,\; n \times k_{\max}\bigr), \qquad k_{\max} \le V. \]

`.max(1)` 的用意：当批内所有请求都 `top_k = 0`（只要 logprob/rank）时 \( n \cdot k_{\max} = 0 \)，而 0 个元素既不是一块能交给内核解引用的合法设备缓冲，也让后续 D2H 无处落——垫到 1 个元素当占位符，内核侧 `k_max = 0` 时本来就一条 top-k 都不写。

乘法用 `checked_mul` 而不是裸 `*`：\( n \) 与 \( k_{\max} \) 都来自调用方输入，溢出时报 "rows * k_max overflows" 而不是回绕出一个过小的长度。

#### 4.2.3 源码精读

三组 i32 输入的上传在 [src/lib.rs:571-578](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L571-L578)：一个 `htod` 闭包把 `ctx.stream.clone_htod` 包上统一错误消息，`rows`、`picked`、`ks` 三个主机向量各成一块 `CudaSlice<i32>`。

四组输出缓冲的分配在 [src/lib.rs:579-599](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L579-L599)：`picked_lp_gpu`/`ranks_gpu` 各 `alloc_zeros(requests.len())`；`topk_len` 的计算是 [src/lib.rs:587-591](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L587-L591) 的 `checked_mul(...).ok_or_else(...)?.max(1)`；`vals_gpu`/`ids_gpu` 按 `topk_len` 分配。

内核调用在 [src/lib.rs:601-616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L601-L616)：被 `unsafe` 包住，旁边一行注释 "Indices validated and stream override rejected above." 说明 unsafe 契约（下标在界内、流环境正确）正是由上面那些 `ensure!` 兑现的。被调函数是 kernels 里的 [../pegainfer-kernels/src/ops/sampling.rs:527-539](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L527-L539) `pub unsafe fn logprob_topk_batch_bf16_into`——签名就是上面那 8 块缓冲加 `rows`、`k_max` 两个标量。

内核侧自己还有第二层形状校验，[../pegainfer-kernels/src/ops/sampling.rs:540-574](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L540-L574)：`rows > 0`、`hidden_dim > 0`、`backing = hidden_dim × seq_len` 用 `checked_mul` 算出后要求 `logits.data.len() >= backing`（arena 背衬必须真装得下它声称的形状——这道兜底校验在 4.4.1 会再登场）、三组输入各 ≥ `rows`、`picked_lp`/`ranks` ≥ `rows`、`vals`/`ids` ≥ `rows × k_max`。真正的 launch 在 [../pegainfer-kernels/src/ops/sampling.rs:588-603](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L588-L603)：取各缓冲的设备指针，调 FFI 的 `logprob_topk_batch_bf16_cuda`，落在 `active_cu_stream(ctx)` 上——没有 stream override 时它就是 `ctx.stream` 主流，与前面所有 H2D/分配同流、天然按序。

`token_logprob_from_row` 的文档注释（[src/lib.rs:463-472](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L463-L472)）里那句 "runs only when a request asked for logprobs" 正是「按需路径」的出处；同样的运行条件对设备孪生成立。

#### 4.2.4 代码实践

**实践目标**：给具体数字算出全部缓冲长度，把 4.2.2 的公式落地。

**操作步骤**：

1. 取 [tests/gpu_logprobs.rs:57-92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L57-L92) 的测试参数：`vocab = 151_936`，4 条请求的 `top_k` 分别为 `0, 1, 5, 20`。
2. 算出：\( n \)、\( k_{\max} \)、`topk_len`、三组输入缓冲各多少元素、`vals_gpu`/`ids_gpu` 各多少元素、其中第 0 条请求（top_k=0）会占用多少个有效槽位。
3. 再算一个极端：4 条请求全部 `top_k: 0`，`topk_len` 是多少？为什么不是 0？
4. 换成 [tests/gpu_logprobs.rs:191-208](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L191-L208) 的 `k_larger_than_vocab_is_clamped`：vocab=8、`top_k: 32`，算截断后的 `ks[0]`、\( k_{\max} \)、`topk_len`。

**需要观察的现象 / 预期结果**：(2) \( n=4, k_{\max}=20 \)，`topk_len = 80`；第 0 条请求占 0 个有效槽位（但它的 `base = 0×20 = 0`，槽位 0..20 属于它却全不被读）。(3) `topk_len = 1`（`.max(1)` 垫底）。(4) `ks[0] = 8`，`k_max = 8`，`topk_len = 8`。纸面计算即可，无需 GPU。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `token_logprobs_batch` 的临时缓冲挪进 `SampleScratch`，至少要解决哪两个新问题？

**答案**：一是形状可变——行数与 `k_max` 每次调用都不同，scratch 只能按「史上最大」预留，为一条按需路径常驻显存；二是解码路径的 scratch 纪律是「一次分配、指针稳定、绝不每步重分配」（CUDA 图捕获要求），把一块形状随请求变化的缓冲混进去会破坏这条纪律，且本函数里的 `clone_htod`/`alloc_zeros` 本身就是临时分配语义。

**练习 2**：为什么 `unsafe` 块只包住内核调用一行，而校验都在块外？

**答案**：`unsafe` 标记的是「调用方必须维护的契约」（下标在界内、缓冲够长、流环境正确）无法由类型系统表达；把校验写在块外的普通代码里，用 `ensure!`/`try_from` 兑现契约后再进入 `unsafe`，正是让 unsafe 面积最小、契约出处可追溯的写法（注释 "Indices validated and stream override rejected above." 即契约清单）。

**练习 3**：内核 launch 用 `active_cu_stream(ctx)` 而临时缓冲全在 `ctx.stream` 上——两者什么时候是同一条流？

**答案**：没有 stream override 时，`active_cu_stream` 返回 `DeviceContext` 自己的流，与 `ctx.stream` 是同一条，H2D → 分配 → 内核 → D2H 全部同流按序；一旦线程激活了 `StreamOverrideGuard`，两者分叉，这正是 4.4 里函数直接拒绝执行的原因。

### 4.3 D2H 回读与结果重组：ctx.sync() 与 i*k_max+j 布局

#### 4.3.1 概念说明

内核写完七块缓冲后，函数把四个输出缓冲各 `clone_dtoh` 回主机，`ctx.sync()` 等整条流完成，然后在主机上把扁平缓冲重组回契约类型 `TokenLogprob { logprob, rank, top_logprobs }`。

关键布局：top-k 的 `vals`/`ids` 不是按「实际 k」紧凑排布，而是**按 `k_max` 定长分行**——第 \( i \) 条请求的条目住在下标区间 \( [i \cdot k_{\max},\; i \cdot k_{\max} + k_i) \)，即第 \( j \) 个条目在 \( i \cdot k_{\max} + j \)。因为内核按批内统一的最大 k 写出（GPU 上每行变长输出要引入复杂的紧凑化），`k_i < k_max` 的行在区间尾部留下一段不被读取的填充槽。重组循环只读每行的前 \( k_i \) 个槽，天然跳过填充。

#### 4.3.2 核心流程

```text
（主 stream 上已按序排好：H2D×3 → alloc×4 → 内核 → ）
clone_dtoh(picked_lp_gpu) → 主机 Vec<f32>，长 n
clone_dtoh(ranks_gpu)     → 主机 Vec<i32>，长 n
clone_dtoh(vals_gpu)      → 主机 Vec<f32>，长 topk_len
clone_dtoh(ids_gpu)       → 主机 Vec<i32>，长 topk_len
ctx.sync()                ← 同步点：此后内核与全部拷贝确已落地
对 i = 0..n：
    k   = ks[i]                     ← 复用上传前截断好的主机侧 k
    base = i * k_max
    TokenLogprob {
        logprob: picked_lp[i],
        rank:    ranks[i] as u32,
        top_logprobs: (0..k).map(|j| (ids[base+j] as u32, vals[base+j])),
    }
返回 Vec<TokenLogprob>（与 requests 等长、同序）
```

#### 4.3.3 源码精读

四次回读与同步点在 [src/lib.rs:618-634](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L618-L634)：四个 `clone_dtoh` 依次排队并各自返回主机向量，随后一次 `ctx.sync()`（实现即 [../pegainfer-kernels/src/tensor.rs:400-404](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L400-L404) 的 `stream.synchronize()`）。sync 位于「全部拷贝已排队」与「主机开始读向量组装」之间——它把完成点显式钉死：流上此前排队的内核与全部 D2H 都已结束，主机读到的四个向量必然完整。

组装循环在 [src/lib.rs:636-648](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L636-L648)：`let k = ks[i] as usize` 直接复用 4.1 阶段截断后的主机侧 `ks`（这就是它兼任组装元数据的含义）；`let base = i * k_max` 落实 \( i \cdot k_{\max} \) 的行基址；`(0..k)` 只读该行前 k 个槽，`ids[base+j] as u32` 与 `vals[base+j]` 配成 `(token_id, logprob)` 对。`rank: ranks[i] as u32` 的 u32 化与主机参考实现返回的 `rank: u32` 对齐——rank 语义（原始 logit 不小于被选值的个数、含并列含自己）在 u3-l1 已推导，设备内核实现的正是同一约定，由 A/B 测试钉死。

值得注意：这里读回的是**整个** `vals`/`ids`（含每行尾部的填充槽），而非每行只拷 k 个元素——一次整块 D2H 比多次短拷贝便宜得多，填充槽只是几十字节的浪费，换来拷贝次数从 \( 2n \) 降到 2。

#### 4.3.4 代码实践

**实践目标**：在纸上走出 `i*k_max+j` 布局，确认「谁读哪里、哪些槽是填充」。

**操作步骤**：

1. 构造一个 3 条请求的批：`top_k` 分别为 `3, 0, 5`（vocab ≥ 5）。算出 \( k_{\max} \)、`topk_len`。
2. 画一条长为 `topk_len` 的格子，标出每条请求的 `[base, base+k)` 有效区间和填充区间。
3. 对每条请求写出组装时读到的 `top_logprobs` 长度；再写出第 2 条请求（top_k=0）的 `base` 和它读取的槽位数。
4. 核对 [src/lib.rs:636-648](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L636-L648)：`k = ks[2]` 是多少、`(0..k)` 循环体执行几次。

**需要观察的现象 / 预期结果**：\( k_{\max}=5 \)，`topk_len = 15`；请求 0 读槽 0..3，槽 3..5 填充；请求 1 `base=5`、读 0 个槽（槽 5..10 全是填充）；请求 2 读槽 10..15。第 2 条请求的 `top_logprobs` 是空 `Vec`，但 `logprob`/`rank` 仍从 `picked_lp[2]`/`ranks[2]` 正常取出。纸面推演，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ctx.sync()` 放在四次 `clone_dtoh` 之后、组装循环之前，而不是每次拷贝后各 sync 一次？

**答案**：四次拷贝连同内核都在同一条流上按序排队，一次全流同步即可保证全部落地；逐次 sync 会引入四次主机-设备的等待往返，白付延迟而没有换来任何额外的正确性。

**练习 2**：如果调用方把 `top_k` 全传 0，返回的 `TokenLogprob` 里 `top_logprobs` 是什么？`logprob` 和 `rank` 呢？

**答案**：`top_logprobs` 为空 `Vec`（`(0..0)` 不产生条目），`logprob` 与 `rank` 照常由内核写出并回读——这正是「只要 logprob/rank、不要 top 列表」的用法，[tests/gpu_logprobs.rs:64-69](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L64-L69) 的第一条请求就是 `top_k: 0`。

**练习 3**：`vals`/`ids` 为什么要按 `k_max` 定长分行，而不是每行只写 k 个、紧凑排列？

**答案**：GPU 内核按统一步长写定长行无需行间依赖或第二个紧凑化 pass，写侧最简单；代价是行尾填充槽与稍大的 D2H。由于 \( k_{\max} \le V \) 且 n 通常只有几行，这点冗余远小于紧凑化所需的额外内核工作或多次短拷贝。

### 4.4 防护栏专题：stream override 必须拒绝，以及分片词表警告

#### 4.4.1 概念说明

本函数有三道防护栏，前两道已见（空请求短路、逐条越界拒绝），这一模块讲后两道深水区：

**（a）「说谎的 `seq_len`」与纵深防御。** 包装层的 `r.row < logits.seq_len` 信任 arena 自报的形状；但 `HiddenStates` 的 `seq_len` 是个 pub 字段，调用方 bug 可能把它改得比背衬数据大。此时包装层校验会放行，真正拦住它的是内核层的兜底校验 `logits.data.len() >= hidden_dim × seq_len`——两层校验各拦一类错：包装层拦「下标超界」，内核层拦「结构体自身不一致」。

**（b）stream override 为什么必须报错而不是排队。** 错误信息已经给出答案的骨架："buffer traffic is ordered on the primary stream"。这个函数的全部显存流量（H2D、`alloc_zeros`、D2H）都排在 `ctx.stream` 主流上，而内核 launch 落在 `active_cu_stream(ctx)`——覆盖激活时这是另一条流。两条流之间没有任何排序：D2H 可能跑在内核之前（读回全零）、更糟的是函数返回时各临时 `CudaSlice` 被 drop，而 drop 的释放语义排在主流上——覆盖流上的内核可能还在读写一块已被释放的内存。「排队等着执行」并不能修复这种跨流乱序，唯一正确的做法是拒绝：这是一条按需、非重叠的路径，稳态下拒绝零成本，还把误用变成显式错误而不是隐性数据竞争。测试 [tests/gpu_logprobs.rs:263-281](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L263-L281) 用 `StreamOverrideGuard::activate(ctx.stream.cu_stream())` 激活覆盖后断言调用 `is_err()`，再 drop guard。

**（c）分片词表警告。** 内核对 arena 行的 `hidden_dim` 列做 LSE。若 arena 只装着本 rank 的词表分片，算出的是**分片局部** LSE：\[ \log\!\Bigl(\sum_{v \in \text{shard}_r} e^{x_v}\Bigr) \;\ne\; \log\!\Bigl(\sum_{v \in \text{global}} e^{x_v}\Bigr), \] 概率根本不归一。所以文档要求行必须是未分片的全词表，分片调用方必须先跨 rank 归并（Kimi-K2 的做法，#236）。这条警告写在主机参考实现的文档里（[src/lib.rs:470-472](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L470-L472)），对设备孪生同样成立——两者在同一 arena 行上做同一数学。

#### 4.4.2 核心流程

```text
调用 token_logprobs_batch
  ├─ requests 空 ──────────────▶ Ok(vec![])                （护栏 1：短路）
  ├─ has_stream_override() ────▶ Err("…ordered on the primary stream")（护栏 2：拒绝）
  ├─ 逐条 row/picked 越界 ─────▶ Err（护栏 3：包装层下标校验）
  └─ 全部通过
       └─ 内核层再校验 backing/长度 ──▶ Err（护栏 4：兜底结构校验，
                                          拦「seq_len 说谎」这类调用方 bug）
```

#### 4.4.3 源码精读

stream override 拒绝在 [src/lib.rs:541-545](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L541-L545)，`ensure!(!has_stream_override(), ...)`；`has_stream_override` 是 [../pegainfer-kernels/src/tensor.rs:63-66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L63-L66) 的线程本地开关读数，覆盖机制（`StreamOverrideGuard`，drop 恢复）在 [../pegainfer-kernels/src/tensor.rs:25-61](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/tensor.rs#L25-L61)，文件头 L18 注明其用途是 Green Context SM 分区。

「seq_len 说谎」测试在 [tests/gpu_logprobs.rs:246-260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L246-L260)：`make_arena` 造了一个真实的 1 行 × 64 列 arena，然后 `inflated.seq_len = 4` 手动把行数改成 4——背衬只有 64 个元素，声称的形状却是 4×64。`row: 3 < seq_len: 4` 骗过了包装层，最终由内核层的 `data.len() >= hidden_dim × seq_len`（[../pegainfer-kernels/src/ops/sampling.rs:544-552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/sampling.rs#L544-L552)）报 "arena backing too small"。分片词表警告见 [src/lib.rs:470-472](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L470-L472)（"a shard-local logsumexp is not the global one, so sharded callers must merge across ranks first (#236)"）；模块文档 [src/lib.rs:21-25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/src/lib.rs#L21-L25) 交代了 Kimi-K2 跨 rank 归并的背景。

批组合无关性（同一行在单独批与混批里结果逐位一致）由 [tests/gpu_logprobs.rs:283-336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L283-L336) 用 `to_bits()` 逐位断言钉死——这与 u2-l4 讲的采样侧可重放契约是同一精神在 logprob 通道的体现。

#### 4.4.4 代码实践

**实践目标**：弄清两件事——`inflated.seq_len = 4` 为什么是有效的测试手法；stream override 激活时为什么必须报错而不是排队。

**操作步骤**：

1. 读 [tests/gpu_logprobs.rs:210-261](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L210-L261) `rejects_out_of_range_requests` 的四段断言，注意前两段（`picked: 64`、`row: 2`）测的是包装层校验，第四段测的是内核层兜底。
2. 问自己：正常构造函数 `HiddenStates::zeros(ctx, vocab, rows)`（见 [tests/gpu_logprobs.rs:15-23](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L15-L23) 的 `make_arena`）能不能造出「seq_len 大于背衬」的结构体？如果不能，测试只能靠手动改字段来模拟这种调用方 bug。
3. 读 [tests/gpu_logprobs.rs:263-281](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs#L263-L281) `rejects_active_stream_override`，注意 guard 是 `drop(guard)` 之前完成断言的。
4. （可选，需 GPU，待本地验证）`cargo test --release -p pegainfer-sample --test gpu_logprobs -- --nocapture`。

**需要观察的现象 / 预期结果**：步骤 1-3 的参考答案——`seq_len` 与背衬不一致的结构体无法经正常构造函数产出（`zeros` 按形状真实分配），所以 `inflated.seq_len = 4` 是制造「结构性谎言」的唯一手段，它验证的是防线纵深：包装层信任形状、内核层验证背衬，两层合起来把「形状说谎」也拦住；stream override 必须报错，因为函数的全部缓冲流量排在主流而内核落在覆盖流，跨流无排序——D2H 可能在内核前执行、临时缓冲可能在内核仍在写时被 drop 释放，「排队」修不了乱序，只有拒绝才安全（且这是按需路径，拒绝无稳态成本）。

#### 4.4.5 小练习与答案

**练习 1**：如果去掉内核层的 backing 校验、只留包装层的 `row < seq_len`，`inflated` 那个用例会发生什么？

**答案**：包装层会放行（3 < 4），内核随后按 `row × hidden_dim = 3 × 64 = 192` 的偏移读一个只有 64 元素的缓冲——越界读设备内存，读到未定义数据甚至触发 CUDA 错误；错误从「清晰的 ensure! 消息」退化成「不可复现的内存事故」。

**练习 2**：为什么 `token_logprobs_batch` 不像 `greedy_stage_readback` 那样用 pinned 缓冲加事件等待（u4 将讲到的手法），而是普通 `clone_dtoh` + `ctx.sync()`？

**答案**：pinned 双槽 + 事件等待是为解码热路径的延迟纪律设计的（每步都要跑、不能整流同步）；本函数是低频按需路径，回读量是 \( O(n(k_{\max}+1)) \) 的几个向量，一次整流同步的实现简单性胜过微秒级的延迟优化。

**练习 3**：Kimi-K2 为什么不能直接把本 rank 的 logits arena 喂给 `token_logprobs_batch`？

**答案**：它的词表按 rank 分片，arena 行只含本 rank 的列；内核算出的 LSE 是分片局部的，logprob 不归一、top-k 也不全局。必须先跨 rank 归并出全词表行（或改走主机参考实现配合跨 rank 归并，#236/#237 的 DP 归并背景见模块文档）。

## 5. 综合实践

把本讲全部内容串成一张数据流图，并回答两个源码问题。

**任务**：画出 `token_logprobs_batch` 一次调用的完整数据流图，要求：

1. 从 `requests: &[LogprobRequest]` 开始，到返回 `Vec<TokenLogprob>` 结束；
2. 标出每块 H2D 缓冲与每块 D2H 缓冲的**长度公式**（不是具体数字），特别是 `topk_len` 的 `checked_mul` 与 `.max(1)` 两步；
3. 用竖线或标记注明 `ctx.sync()` 的位置（四次 D2H 之后、组装循环之前）；
4. 标出内核 launch 落在哪条流上（`active_cu_stream`），以及它与 `ctx.stream` 的关系。

**参考画法**（可以照此核对，也可自创画法）：

```text
requests: &[LogprobRequest]
   │ ① 校验循环：ensure! 逐条拦截 row/picked 越界；k=min(top_k,V)；k_max 累积；i32 收窄
   ▼
rows / picked / ks  三个 Vec<i32>（长 n）
   │ ── clone_htod ×3（ctx.stream 主流） ──▶  rows_gpu / picked_gpu / ks_gpu     [H2D，各 n 个 i32]
   │ ── alloc_zeros ×4（ctx.stream 主流） ──▶  picked_lp_gpu (f32×n)  ranks_gpu (i32×n)
   │                                          vals_gpu (f32×max(1,n·k_max))
   │                                          ids_gpu  (i32×max(1,n·k_max))
   │ ② unsafe logprob_topk_batch_bf16_into ── 一次 launch，落在 active_cu_stream(ctx)
   │    （无 override 时 = ctx.stream 主流，与上面全部同流按序）
   │ ── clone_dtoh ×4（ctx.stream 主流） ──▶  picked_lp / ranks / vals / ids 四个主机 Vec
   ▼
═══ ctx.sync() ═══  ← 全流同步点：内核 + 全部拷贝确已落地
   │ ③ 组装循环：k=ks[i]，base=i·k_max，
   │    TokenLogprob { picked_lp[i], ranks[i] as u32, (ids|vals)[base..base+k] }
   ▼
Vec<TokenLogprob>（与 requests 等长同序）
```

**源码问题**（参考答案已分别在 4.4.4 给出，这里要求你先独立作答再核对）：

- 为什么 `rejects_out_of_range_requests` 里「把 `seq_len` 手动改成 4」是一个有效的测试手法？（提示：正常构造函数造不出这种结构体；它测的是哪一层防线？）
- stream override 激活时这个函数为何必须报错而不是排队？（提示：数一数这个函数有几块缓冲排在主流、内核落在哪条流、函数返回时临时缓冲发生了什么。）

**验收标准**：图上 7 块新建设备缓冲（3 H2D + 4 输出）各有长度公式；`ctx.sync()` 位置正确；两个问题能脱离讲义复述。全程纸面即可完成；若想在 GPU 上跑对应测试验证（`cargo test --release -p pegainfer-sample --test gpu_logprobs`），待本地验证。

## 6. 本讲小结

- `token_logprobs_batch` 是 `token_logprob_from_row` 的设备端批量孪生：一次 `logprob_topk_batch_bf16_into` launch 服务整批，只回读 \( O(n(k_{\max}+1)) \) 的紧凑结果。
- 一次调用新建 7 块设备缓冲：三组 i32 输入（各 \( n \) 个）+ 四组输出，其中 `vals`/`ids` 长度 \( \max(1,\, n \cdot k_{\max}) \)——`checked_mul` 防溢出、`.max(1)` 给全零 k 的批垫一个合法占位缓冲。
- 结果按 `i*k_max + j` 定长分行布局重组，`k_i < k_max` 的行尾填充槽不被读取；主机侧 `ks` 向量兼任组装元数据；`ctx.sync()` 钉在四次 D2H 之后、组装之前。
- 它每次现分配而不入 scratch：logprob 只在请求方要求时运行，形状每次不同，不值得为按需路径常驻显存，也不能把临时分配混进解码路径的指针稳定纪律。
- 防护栏成纵深：空请求短路 → stream override 拒绝（缓冲流量排在主流、内核落在覆盖流，跨流无序，排队救不了）→ 包装层下标校验 → 内核层背衬兜底（拦「seq_len 说谎」）。
- 分片词表警告：分片局部的 logsumexp 不是全局的，必须先跨 rank 归并（Kimi-K2，#236）。

## 7. 下一步学习建议

- 下一讲（u4-l1）进入 CUDA 图友好的分阶段贪心路径：`greedy_argmax_ids` / `greedy_stage_readback` / `greedy_stage_resident` / `greedy_collect_resident` 四件套，你会看到「图内指针稳定 + 图外 pinned 回读」的完整纪律——本讲的 `ctx.sync()` 与「为何不入 scratch」正好是它的铺垫。
- 若想巩固 logprob 通道，重读 [tests/gpu_logprobs.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sample/tests/gpu_logprobs.rs) 的 `tie_ordering_matches_host`（L94-L159）：它用两个人造并列梯队和一组「f32 减 LSE 后塌缩成同值」的相邻 bf16 位模式，把并列排序契约钉到逐位。
- 想看真实调用方的完整时序（先采样后打分、logprob 请求按需过滤），读 [../pegainfer-gemma4/src/engine.rs:975-1007](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-gemma4/src/engine.rs#L975-L1007)。
