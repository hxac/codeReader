# u8-l1 batch-invariant 服务：Pin 数值策略与请求本地网格

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 `NumericPolicy::Pin` 与 `NumericPolicy::Tuned` 的本质差异：一个是「每个 (M,K) 只钉一个 cuBLASLt 算法、对所有 N 复用」，一个是「按 N 逐形状挑选最快算法」；并说出 `set_numeric_policy` 为何是进程全局、且必须在执行器构造/图捕获之前生效。
2. 走读 `tune_decode_gemm_algos` 的两条分支（Pin 预热 vs Tuned 扫参）与它在两个 worker（画像 worker / 服务 worker）上的不同行为。
3. 推导 `verify_pin_envelope` 的自检范围：6 组 `(M,K)` 投影形状 × 每个 N 逐一点检，以及 \( \text{ceiling} = \text{max\_prefill\_tokens} + (B_{\max} - 1) \) 的来历。
4. 理解 `take_prefill_chunks` 的 request-local 网格如何让「同一请求的 chunk 序列只由自身 prompt 长度决定」，以及前缀缓存命中为什么会把边界撞出网格。
5. 为 `ensure_batch_invariant_supported` 的五条拒绝（decode-overlap、KV offload、TP、投机解码、前缀缓存）各写出一条理由，并注意草稿器拒绝已从 DFlash 专属推广为覆盖 EAGLE-3 的通用 speculative 拒绝（`af9e40e1` 提交）。

## 2. 前置知识

**为什么 batch-mates 会改变我的输出？** bf16 GEMM \( Y = W \cdot X \) 里每个输出元素 \( y_{m,n} = \sum_k W_{m,k} X_{k,n} \) 是一个浮点求和，求和顺序不同则舍入不同。cuBLASLt 的启发式会按 GEMM 的 N（本 crate 中 N = 一步前向的 token 行数，即 batch 组合）选择不同算法（不同的 tile 切分、不同的 split-K 归约树），于是「和我同批的请求有多少 token」会改变我这一步的归约顺序，进而改变低位比特。生产模式（Tuned）追求最快，天然接受这种漂移。

**batch-invariant 的承诺**：同一请求无论单独跑还是与谁同批，输出 token 与 logprob 逐位一致。它靠「两条腿」站立：

- 腿一（GEMM 轴）：Pin——每个 `(M,K)` 只在固定代表点 N=32 上解析出一个算法，之后对所有 N 复用，且绝不静默回退。
- 腿二（chunk 轴）：request-local 网格——一个请求经历哪些 prefill 步、每步多少 token，只由它自己的 prompt 长度决定。

**承接前讲**：u3-l5 讲过 CUDA Graph 在捕获时烘焙算法选择（所以策略必须先于捕获设置）；u4-l1 讲过 `take_prefill_chunks` 的共享预算装箱——本讲看它的 `request_local` 分支；u3-l8 讲过 split-KV chunk 公式与 Pin/PerToken 的差别，本讲只引用不再重推。

**分层设防词汇**：CLI 层（`model_line.rs` 的 `validate`，用户敲错命令时拒绝）→ 构建器层（`lib.rs` 的 `ensure_batch_invariant_supported`，覆盖所有嵌入方）→ 执行器层（`enable_decode_overlap` 的预捕获 backstop、`launch_gemm_pin` 的运行期 bail）。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs) | `apply_batch_invariant_policy`（设全局策略）与 `ensure_batch_invariant_supported`（五条拒绝），LoRA 拒绝在 `start_engine_with_lora_control` |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | `tune_decode_gemm_algos`（Pin 预热/Tuned 扫参）与 `verify_pin_envelope`（启动期全 N 自检），及其两个调用点 |
| [src/batch_decode_buffers.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs) | `decode_projection_pin_shapes`（6 组被钉的 `(M,K)`）、`warmup_decode_projection_pins`、`pin_chunk_size` |
| [src/scheduler.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs) | `take_prefill_chunks` 的 `request_local` 分支（本讲的腿二） |
| [src/frontend_adapter.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs) | 装配层按当前数值策略决定是否启用 request-local |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs) | CLI 层的 batch-invariant 互斥块（DFlash 文案 + EAGLE-3 冲突表） |
| [pegainfer-kernels/src/ops/linear.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs) | `NumericPolicy` 定义、`launch_gemm_pin`（无回退语义）、`gemm_lt_pin_check`、`pin_served` 计数 |
| [tests/batch_invariance_reject.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs) | 构建器层拒绝测试（无 GPU 可跑） |
| [tests/batch_invariance_envelope.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_envelope.rs) | 数值包络测试（GPU + 模型权重） |
| [tests/batch_invariance_endtoend.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_endtoend.rs) | 端到端逐位一致测试（GPU + 模型权重） |

## 4. 核心概念与源码讲解

### 4.1 NumericPolicy 与 apply_batch_invariant_policy：进程全局数值策略

#### 4.1.1 概念说明

`NumericPolicy` 是 pegainfer-kernels 里的进程全局三态开关，控制所有投影 GEMM 的算法选择方式：

- `Tuned`（默认）：生产路径，每个 decode 形状（bucket）实测挑最快算法——不同 N 可能选不同算法，归约顺序随批组合漂移。
- `Pin`：batch-invariant 模式，每个 `(M,K)` 只钉一个算法，对所有 N 复用；算法是在固定代表点 N=32 上解析的（不是「首次调用的 N」，否则钉谁取决于调用顺序）。
- `PerToken`：N=1 的诊断 oracle（逐 token 每列独立计算，按构造不变），用于在测试里给 Pin 提供参照。

`--batch-invariant` CLI 旗标最终就是把它拨到 Pin。

#### 4.1.2 核心流程

```text
start_engine_with_offload_inner(batch_invariant = true)
  ├─ 1. ensure_batch_invariant_supported(...)   // 先拒绝：五条互斥（见 4.5）
  ├─ 2. apply_batch_invariant_policy(true)      // 再设策略：NumericPolicy::Pin 写入进程全局 AtomicU8
  └─ 3. frontend_adapter::start_qwen3(...)      // 之后才构造执行器 / 捕获图
```

顺序不可颠倒：若先设策略后被拒，进程全局策略已被污染成 Pin，同一进程里后续非 invariant 启动会带着错误的策略跑。顺序同时保证「捕获到的算法就是生效的算法」——u3-l5 讲过 CUDA Graph 会烘焙捕获时刻的算法。

#### 4.1.3 源码精读

[pegainfer-kernels/src/ops/linear.rs:734-746](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L734-L746) 定义三态并注明：原子量而非线程局部——「主线程在 worker 构造前写入，worker 上读取」；`Pin` 的语义是「一个 (M,K) 一个算法，`launch_gemm_pin` 在无法服务某 N 或遭遇流覆写时直接 bail，不做逐 token 回退」。

[pegainfer-kernels/src/ops/linear.rs:748-764](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L748-L764) 是 `NUMERIC_POLICY: AtomicU8` 全局量与 `set_numeric_policy` / `numeric_policy` 读写对。

[src/lib.rs:524-534](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L524-L534) 是 `apply_batch_invariant_policy`：把布尔旗标翻译成 `Pin`/`Tuned`，打一行 `Qwen3 numeric policy: ...` 日志后调用 `set_numeric_policy`。它是一个极薄的「旗标 → 全局策略」翻译器。

[src/lib.rs:453-462](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L453-L462) 是调用点，可清楚看到「先 `ensure_batch_invariant_supported(...)?` 后 `apply_batch_invariant_policy(batch_invariant)`」的顺序；注意传给守卫的第四个参数是 `draft.is_some()`——`draft` 是 `Option<DraftModel>`，DFlash 与 EAGLE-3 都折进这个单槽（u1-l4），所以这条拒绝天然覆盖两种草稿器。

[pegainfer-kernels/src/ops/linear.rs:776-782](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L776-L782) 的 `pin_served()` 计数器是「Pin 真的跑了」的证据：Pin 下一个 GEMM 要么被计数、要么 bail，不存在静默回退；decode 只计图捕获不计重放。

#### 4.1.4 代码实践

1. **实践目标**：确认「策略先于执行器构造生效」这条时序契约在源码里成立。
2. **操作步骤**：从 `src/lib.rs:453` 出发，用 `rg "set_numeric_policy|apply_batch_invariant_policy" src/ tests/` 列出所有写入点；再打开 [tests/batch_invariance_reject.rs:19-20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs#L19-L20) 思考：为什么这些拒绝测试要用一个 `static POLICY_LOCK: Mutex` 串行？答案：策略是进程全局的，并发测试会互相污染。
3. **需要观察的现象**：日志行 `Qwen3 numeric policy: Pin (--batch-invariant=true)`（启动时打印）。
4. **预期结果**：拒绝测试里每个用例都先 `set_numeric_policy(NumericPolicy::Tuned)` 复位再断言「拒绝发生时全局策略仍为 Tuned」，证明守卫先于 apply（见 [tests/batch_invariance_reject.rs:43-47](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs#L43-L47) 的断言文案）。
5. 无 GPU 也能运行：`cargo test --release -p pegainfer-qwen3 --test batch_invariance_reject`——本环境未验证，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NumericPolicy` 用 `AtomicU8` 而不是 thread-local？
**答案**：策略在主线程（引擎启动）写入，在 GPU worker 线程读取；thread-local 会让 worker 读到默认值 Tuned，等于没设。注释原话：「set on the main thread before worker construction, read on the worker」。

**练习 2**：如果把 Pin 的算法解析点从固定 N=32 改成「首次调用时的 N」，会破坏什么？
**答案**：钉哪个算法会取决于「哪个形状先来」——即调用顺序，而调用顺序由批组合决定，恰恰是被不变性排除掉的自由度。固定代表点 `PIN_REP_N = 32` 消除这一依赖（[pegainfer-kernels/src/ops/linear.rs:796-803](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L796-L803)）。

### 4.2 tune_decode_gemm_algos：Pin 预热 vs Tuned 扫参

#### 4.2.1 概念说明

图捕获之前必须把 GEMM 算法准备好——捕获后再懒解析会「在图捕获中途分配工作区」，非法。`tune_decode_gemm_algos` 按当前策略做这件事，两个模式干完全不同的活：

- **Pin 模式**：对 6 组 `(M,K)` 投影形状逐一 `gemm_lt_pin_warmup`（在 N=32 上钉算法），日志确认 split-KV chunk 已按上下文上限钉死，然后（仅服务 worker）跑 `verify_pin_envelope` 自检。
- **Tuned 模式**：对每个 bucket（N ≤ `GEMM_LT_MAX_N = 32`，即 bucket 1/2/4/8/16/20/24/32）、每个投影族做 `gemm_lt_tune` 计时挑选——采样时把每一层的真实权重卷进轮转，保证 L2 冷态计时公平。

#### 4.2.2 核心流程

```text
tune_decode_gemm_algos(model, max_prefill_tokens, run_envelope_check)
  ├─ policy == Pin?
  │    ├─ warmup_decode_projection_pins(q, kv, o, gate_up, down, lm_head 六组 (M,K))
  │    ├─ log: split-KV decode chunk 已按 max_context 钉死（chunk 与批无关）
  │    └─ run_envelope_check? → verify_pin_envelope(model, max_prefill_tokens)  // 见 4.3
  │         └─ return
  └─ Tuned: for bucket in BATCH_BUCKETS[..=32]:
        gemm_lt_tune(q/kv/o/gate_up/down/lm_head 各族, N=bucket)   // 逐形状计时
```

两个调用点行为不同：

- **画像 worker**（`profile_kv_budget_on_worker`，量 KV 预算的短命线程）：`run_envelope_check = false`——它只预热自己要用的线程本地计划。
- **服务 worker**（`LocalQwen3Lane::bind`，长寿命）：`run_envelope_check = true`——它验证「本线程（线程本地）预热过的计划」能覆盖完整生产包络，保证 pre-serving 前包络成立。

#### 4.2.3 源码精读

[src/executor.rs:577-581](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L577-L581) 的文档注释给出两种模式的完整语义与启动成本说明。

[src/executor.rs:598-618](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L598-L618) 是 Pin 分支：先 `warmup_decode_projection_pins`（注释点明「eager pin before capture: the lazy pin-workspace alloc is illegal mid-capture」），再打 split-KV chunk 日志，最后按 `run_envelope_check` 决定是否自检。

[src/executor.rs:620-657](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L620-L657) 是 Tuned 分支：把每层权重组成 `q_samples`/`kv_samples`/`o_samples`/`gate_up_samples`/`down_samples`/`lm_head_samples` 六族采样，对 `BATCH_BUCKETS` 中 ≤ `GEMM_LT_MAX_N` 的每个 bucket 调 `ops::gemm_lt_tune`。

[src/batch_decode_buffers.rs:65-81](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L65-L81) 的 `decode_projection_pin_shapes` 列出被钉的 6 组 `(M,K)`：q、kv、o、gate_up、down、lm_head（lm_head 放最后，4.3 会用到这个顺序）。

[src/batch_decode_buffers.rs:83-101](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L83-L101) 的 `warmup_decode_projection_pins` 逐组调用 `gemm_lt_pin_warmup` 并打印每组的 split-K 与归约方案。

两个调用点：[src/executor.rs:2330-2341](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2330-L2341)（画像 worker，`false`）与 [src/executor.rs:3627-3632](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3627-L3632)（服务 worker `bind`，`true`）。

运行期防线在 [pegainfer-kernels/src/ops/linear.rs:805-854](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L805-L854) 的 `launch_gemm_pin`：遇到流覆写直接 bail（decode-overlap 是唯一流覆写源，且已在启动期拒绝，这里出现即误用）；`(M,K)` 未预热或算法服务不了 N 时 bail，不做懒解析、不做逐 token 回退——任何回退都会重新引入 N 依赖，破坏不变性。bail 文案里点名的三个典型成因：N 超出包络、(M,K) 从未预热（「e.g. a DFlash drafter or LoRA prefill-delta shape」——这正是 4.5 两条拒绝的机理）、策略在执行器构造之后才设置。

#### 4.2.4 代码实践

1. **实践目标**：弄清「为什么 envelope 自检只在服务 worker 上跑」。
2. **操作步骤**：读 [src/executor.rs:612-616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L612-L616) 的注释（「The profile worker skips the full sweep; the long-lived serving worker verifies its own (thread-local) warmed plans before capture」），再对照两个调用点的 `run_envelope_check` 实参。
3. **需要观察的现象**：Pin 启动日志里先出现每组的 `Qwen3 GEMM pin: m=..., k=..., splitk=...`，再出现 `Qwen3 split-KV decode chunk pinned: ... tokens`。
4. **预期结果**：画像线程短命且只为估算显存，全 N 扫描是纯启动开销，没必要重复；服务线程的验证保证「包络成立」发生在第一个请求进来之前。
5. 启动日志需 GPU 环境，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：Tuned 扫参为什么只扫到 bucket 32？
**答案**：`GEMM_LT_MAX_N = 32`（[pegainfer-kernels/src/ops/linear.rs:224](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L224)）：≤32 的 bucket 用 cublasLt 精调算法，更大的 bucket 交给别的路径/启发式——这也是 bucket 8/16 存在的原因（[src/batch_decode_buffers.rs:23-26](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L23-L26) 的注释）。

**练习 2**：Pin 分支为什么顺带打印 split-KV chunk？
**答案**：chunk 被钉死是「请求本地」性质的另一环——Pin 的 chunk 只依赖上下文上限（u3-l8 的 \( \text{chunk}(L) = \max(64, \lceil L/256 \rceil) \)，[src/batch_decode_buffers.rs:60-63](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L60-L63) 与 [:46-53](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L46-L53)），块数请求本地、批内逐位一致；日志把这件事显式钉在启动记录里。

### 4.3 verify_pin_envelope：启动期全 N 自检

#### 4.3.1 概念说明

Pin 承诺「一个算法服务所有生产可达的 N」。但 cuBLASLt 不保证任一算法能服务任意 N——所以这个承诺必须被验证而不是被假设。`verify_pin_envelope` 在启动期（预热后、图捕获前、GEMM 线程上）对 6 组 `(M,K)` 的每一组，把生产可达的**每一个** N（1 到各自上限，不是抽样）送进宿主侧 `gemm_lt_pin_check`——后者只做 `cublasLtMatmulAlgoCheck`，不发起任何 GEMM、不分配缓冲，所以密集全扫只是启动开销。任何点检失败就 `bail!` 并点名第一个服务不了的 `{M,N,K}`。

#### 4.3.2 核心流程

N 上限来自生产形状推导：

- **统一步 N 峰值**：\( \text{ceiling} = \text{max\_prefill\_tokens} + (B_{\max} - 1) \)——一个 prefill chunk（≤ `max_prefill_tokens`）加上并发 decode 行数（依赖 `max_decode_batch_size == BATCH_BUCKETS.last()`）。
- **lm_head 特殊**：它跑在「被采样位置数」上而非 token 数——decode-only 按桶填充（≤ 最大桶），全位置评分到 `max_prefill_tokens`，故其上限取 \( \max(\text{max\_prefill\_tokens}, B_{\max}) \)。

以默认值 `DEFAULT_MAX_PREFILL_TOKENS = 1024`（[src/scheduler.rs:416](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L416)）与 `BATCH_BUCKETS.last() = 256`（[src/batch_decode_buffers.rs:27-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode_buffers.rs#L27-L30)）计算：

\[ \text{ceiling} = 1024 + 255 = 1279, \qquad N_{\text{lm\_head}} = \max(1024, 256) = 1024 \]

点检总数 \( = 5 \times 1279 + 1024 = 7419 \)。

#### 4.3.3 源码精读

[src/executor.rs:660-664](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L660-L664) 的文档写明四个要点：完整生产 N 包络（every reachable N, not a sample）、宿主侧检查零启动、时机为 post-warmup pre-capture、失败 bail 点名 `{M,N,K}`。

[src/executor.rs:672-678](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L672-L678) 推导两个上限：`ceiling`（统一 N 峰值）与 `lm_head_max_n`（被采样位置数），注释解释了 lm_head 为何与别的投影不同源。

[src/executor.rs:679-688](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L679-L688) 取 6 组形状并配 `max_ns = [ceiling × 5, lm_head_max_n]`——顺序与 `decode_projection_pin_shapes` 的 lm_head-last 约定对齐。

[src/executor.rs:690-702](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L690-L702) 是双层循环：每个 `(M,K)` 对每个 `n in 1..=max_n` 调 `gemm_lt_pin_check`，`Ok(false)` 即 bail；成功后日志汇总 `batch-invariant pin envelope verified: every N up to {ceiling} (lm_head to {lm_head_max_n}), {checks} checks, 0 fallback`。

[pegainfer-kernels/src/ops/linear.rs:396-413](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L396-L413) 的 `gemm_lt_pin_check` 只调 `ffi::gemm_lt_pin_check_cuda`（纯 `cublasLtMatmulAlgoCheck`），`Ok(false)` 含「工作区超预算」这类生产同样会拒绝的情形。

对照面：[tests/batch_invariance_envelope.rs:1-3](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_envelope.rs#L1-L3) 的包络测试不是全扫，而是**恰好这些生产点**：Unified 在 N=101/201/513/1024/1279、纯 decode 在 bs=256——启动自检保证「算法能服务」，包络测试证明「真的跑通且零回退」（[tests/batch_invariance_envelope.rs:49-58](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_envelope.rs#L49-L58) 用 `pin_served() > 0` 防止空洞通过）。

#### 4.3.4 代码实践

1. **实践目标**：手工复算自检规模，与启动日志互相印证。
2. **操作步骤**：按 4.3.2 的公式，用你启动时的实际 `max_prefill_tokens`（CLI 可改）算出 `ceiling`、`lm_head_max_n` 与点检总数；再在 GPU 环境以 `--batch-invariant` 启动引擎，抓取启动日志里的 `batch-invariant pin envelope verified: ...` 行。
3. **需要观察的现象**：日志中的三个数字（ceiling、lm_head 上限、checks 计数）应与你手算一致。
4. **预期结果**：默认参数下应看到 `every N up to 1279 (lm_head to 1024), 7419 checks, 0 fallback`；若 `max_prefill_tokens` 改为 2048，ceiling 变 2303、lm_head 上限变 2048。
5. 本环境无 GPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么自检用宿主侧 `MatmulAlgoCheck` 而不是真跑一遍 GEMM？
**答案**：要检查的 N 有数千个，真跑是显存与时间双重浪费；`AlgoCheck` 不启动 kernel、不碰缓冲，语义与生产拒绝一致（工作区超预算同样返回不支持）。

**练习 2**：`max_ns` 为什么是 `[ceiling; 5] + [lm_head_max_n]` 而不是六个 `ceiling`？
**答案**：前五组投影的行数 = token 数，统一步峰值是 ceiling；lm_head 的行数 = 被采样/评分位置数，decode-only 按桶填充（≤256）、全位置评分到 max_prefill_tokens，上限是两者取大——默认下 1024 < 1279，所以它反而比其他形状的上限小。

### 4.4 请求本地 chunk 网格：take_prefill_chunks 的 request_local 分支

#### 4.4.1 概念说明

Pin 解决了「算法随 N 漂移」，但同一请求的 chunk 边界仍可能随批组合漂移：共享预算装箱下，队首请求吃掉多少预算，后面的请求第一步能吃多大 chunk 就受影响——它经历的前向序列（每步 token 数、形状、布局）成了批组合的函数。request-local 网格堵住这条轴：**一个请求要么按 `min(剩余, max_prefill_tokens)` 整段拿走，要么这一步整段跳过**——它的 chunk 序列只由自身 prompt 长度决定（每步都是满 1024，最后一步是余数），与谁在同批、排第几无关。

#### 4.4.2 核心流程

```text
take_prefill_chunks(prefilling, max_prefill_tokens, request_local)
  budget = max_prefill_tokens
  对队列中每个请求:
    ├─ prompt_logprobs 请求: 剩余必须整段装下, 否则跳过 (老规则, 两种模式共有)
    ├─ request_local 模式:   desired = min(剩余, max_prefill_tokens)
    │                        desired > budget → 跳过 (整段-or-跳过)
    └─ 共享预算模式:          chunk = min(剩余, budget)   ← 边界依赖前人
  taken 按 request_id 排序 (与结果序对齐)
```

调用方按数值策略选择模式：`NumericPolicy::Pin | PerToken` 时 `request_local = true`。

#### 4.4.3 源码精读

[src/scheduler.rs:91-98](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L91-L98) 的文档注释直接给出契约：「Under request-local chunking, a request takes `min(remaining, max_prefill_tokens)` whole or skips the step, so its chunk boundaries depend only on its own length and are batch-invariant.」

[src/scheduler.rs:99-133](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L99-L133) 是函数本体；[src/scheduler.rs:115-121](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L115-L121) 就是整段-or-跳过分支：`desired > budget` 时 `continue`（留在队列里等下一步），而非截短。

[src/frontend_adapter.rs:756-767](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L756-L767) 是装配层的选择点：`request_local = matches!(numeric_policy(), NumericPolicy::Pin | NumericPolicy::PerToken)`——策略是全局的，装配层每次选 chunk 时现读现用。

单测 [src/scheduler/tests.rs:293-333](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L293-L333) `request_local_chunks_are_independent_of_earlier_requests` 是这条腿的 CPU 可跑证明：请求 3（prompt 80）单独跑与排在两个短请求后面跑，request-local 模式下 chunk 序列都是 `[32, 32, 16]`；共享预算模式下则会不同——测试还反向断言了这一点（否则前两个断言是空洞的）。

为什么前缀缓存与网格互斥：命中长度 h 会把 `prefill_pos` 直接推到 h，此后所有 chunk 边界平移 h——不再落在「只由自身长度决定」的网格上。这正是 4.5 第 5 条拒绝与第 2 条（offload）拒绝的机理。

#### 4.4.4 代码实践

1. **实践目标**：用 CPU 单测亲手复现「网格不随邻居漂移」。
2. **操作步骤**：运行 `cargo test --release -p pegainfer-qwen3 --lib request_local -- --nocapture`，然后读 [src/scheduler/tests.rs:319-332](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L319-L332)：`alone()` 与 `behind_others()` 两个场景、`request_local` 真假两个模式共四组断言。
3. **需要观察的现象**：测试通过；`simulate(..., true)` 两种场景同为 `[32, 32, 16]`，`simulate(behind_others(), false)` 偏离 `[32, 32, 16]`。
4. **预期结果**：四组断言全部成立（该测试不碰 GPU，`--lib` 层即可运行）。本环境未运行，**待本地验证**。
5. 延伸：把 `mk(3, 80)` 改成 70，手推 request-local 序列应为 `[32, 32, 6]`——可自行改断言验证（改完记得还原，不要提交对源码的修改）。

#### 4.4.5 小练习与答案

**练习 1**：request-local 模式下，prompt 长度 L 的请求（`max_prefill_tokens = T`）的 chunk 序列是什么？
**答案**：\( \lceil L/T \rceil \) 步，每步 \( \min(\text{剩余}, T) \)——即 `(T, T, ..., T, L mod T)`（余数为 0 时最后一步恰为 T），只依赖 L 与 T。

**练习 2**：整段-or-跳过会不会让队列头后面的长请求永远饿死？
**答案**：不会永远——budget 每步重置为 `max_prefill_tokens`，长请求的 `desired = min(剩余, max_prefill_tokens)` 恰好等于满预算，只要它排在能被满足的位置（或前面的请求这步被跳过后轮到它）就能整段拿走；代价是吞吐上的排布损失，这是 batch-invariant 明码标价的取舍。

**练习 3**：为什么 PerToken（oracle 策略）也启用 request-local？
**答案**：PerToken 在 GEMM 轴上按构造不变，但 chunk 轴的漂移它同样不免疫；作为参照策略，它必须与 Pin 共享除 GEMM 算法之外的一切数值路径，否则对照实验测的就不只是算法差异。

### 4.5 ensure_batch_invariant_supported：五条拒绝与分层设防

#### 4.5.1 概念说明

batch-invariant 不是免费开关——它把「逐位一致」作为硬承诺，任何会引入未验证数值自由度的组合都必须在启动时拒绝。构建器层的守卫是 `ensure_batch_invariant_supported`，五条拒绝各对应一个具体机理（LoRA 拒绝因入口不同单独住在 `start_engine_with_lora_control`）。CLI 层（model_line.rs）把这些拒绝提前到敲命令时，文案更用户友好；执行器层还有运行期 backstop 兜底。`af9e40e1`（EAGLE-3 落地）把第四条从 `dflash: bool` 推广为 `speculative: bool`——DFlash 与 EAGLE-3 都折进 `Option<DraftModel>`，`draft.is_some()` 同时挡住两者，报错文案也从「DFlash speculative decoding」改为「speculative decoding」。

#### 4.5.2 核心流程

五条拒绝（按源码判定顺序）：

| # | 组合 | 机理 |
| --- | --- | --- |
| 1 | `--decode-overlap` | 流覆写下 `launch_gemm_pin` 直接 bail（拒绝把热路径失败提前到启动点） |
| 2 | KV offload 开启 | offload 下前缀匹配仍然开着（`--no-prefix-cache` 只关 HBM 留存），主机层命中会把 chunk 边界撞出请求本地网格 |
| 3 | TP `world_size > 1` | 跨 rank 归约顺序未验证 |
| 4 | 投机解码（DFlash **或** EAGLE-3） | Pin 只预热并自检六个基础投影 `(M,K)`；草稿器的 fc/MLP 形状从未预热，Pin 下会在 GEMM 边界 bail |
| 5 | 未加 `--no-prefix-cache` | 本地前缀缓存命中同样把 chunk 边界撞出网格 |

第六条 LoRA：入口是 `start_engine_with_lora_control`，机理与第 4 条同源——逐适配器的 rank-K 形状未被 Pin 覆盖。

#### 4.5.3 源码精读

[src/lib.rs:536-574](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L536-L574) 是完整守卫。逐条：[:543-547](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L543-L547) decode-overlap；[:548-554](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L548-L554) offload；[:555-559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L555-L559) TP；[:560-566](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L560-L566) speculative（注意参数名与文案均已推广）；[:567-572](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L567-L572) 前缀缓存。

LoRA 拒绝在 [src/lib.rs:499-505](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L499-L505)：LoRA 入口根本没有 batch_invariant 可开组合，直接 bail。

CLI 层在 [src/model_line.rs:208-242](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L208-L242)：同一批互斥以 `CliError::rule` 呈现；其中 DFlash 文案仍是旧词（[:226-229](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L226-L229)），EAGLE-3 则经冲突表覆盖（[:260-284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L260-L284)，[:272](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L272) 把 `--batch-invariant` 列入 `--eagle3-draft-model-path` 的冲突数组）——CLI 层已挡双草稿器，只是文案不对称。

执行器层 backstop 在 [src/executor.rs:1729-1737](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1729-L1737)：运行期调用者可能先设 Pin 再 `enable_decode_overlap` 绕过入口守卫——`launch_gemm_pin` 最终也会在流覆写处 bail，但那是图捕获/重放中的热路径失败；此处把它挪到安全点。

测试层：[tests/batch_invariance_reject.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs) 头注释自述定位——「Builder-boundary tests … call the public `start_engine_*` entry points, so they also cover guard wiring」。五个用例分别锚定 overlap/dflash/prefix/offload/lora，每个都断言两件事：错误发生 + 全局策略未被污染（如 [:50-76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs#L50-L76) 的 dflash 用例）。

值得注意的**过期断言**：`batch_invariant_rejects_dflash` 仍断言错误文案 `contains("DFlash")`（[:68](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs#L68)），而 `af9e40e1` 之后该分支文案已是「…not supported with speculative decoding…」——纯文本上已不含 "DFlash" 子串。按源码走读（默认 `EngineLoadOptions` 的 `device_ordinals = vec![0]`，前序分支不触发），该用例应在此 HEAD 上失败于文案断言。本环境无法编译 CUDA 工作区，**待本地验证**；若属实，修复方向是把断言子串改成与推广后文案一致的词（如 "speculative"）。

#### 4.5.4 代码实践

1. **实践目标**：产出「为什么每个组合都不行」的一页论证（本讲综合实践的前半部分在此预演）。
2. **操作步骤**：对五条拒绝各写一句机理（提示：三条指向「网格被破坏」——offload/前缀缓存/（间接）TP，两条指向「Pin 未覆盖的形状/流」——overlap/speculative；LoRA 同属形状未覆盖）。然后在无 GPU 环境运行 `cargo test --release -p pegainfer-qwen3 --test batch_invariance_reject -- --nocapture`。
3. **需要观察的现象**：四至五个用例通过；观察 `batch_invariant_rejects_dflash` 是否如上所述失败于 `contains("DFlash")` 断言（错误确实发生了，只是文案断言过期）。
4. **预期结果**：拒绝全部在 `start_engine_*` 入口发生，且每个用例末尾的「global policy 未被污染」断言都成立。本环境未运行，**待本地验证**。
5. 若确认断言过期，把发现记录下来（修复本身属于源码改动，超出本讲义范围）。

#### 4.5.5 小练习与答案

**练习 1**：五条拒绝里哪几条是「网格轴」、哪几条是「算法轴」？
**答案**：网格轴——KV offload（第 2）与前缀缓存（第 5）：命中把 chunk 边界平移出请求本地网格；算法轴——decode-overlap（第 1，流覆写使 pinned GEMM bail）与投机解码（第 4，草稿器形状未预热）；TP（第 3）是独立的第三轴：跨 rank 归约顺序未验证，两条腿都救不了它。

**练习 2**：为什么 LoRA 拒绝不放进 `ensure_batch_invariant_supported`？
**答案**：LoRA 服务走独立入口 `start_engine_with_lora_control`，该入口根本没有「合法组合」可言——batch_invariant=true 直接 bail（[src/lib.rs:499-505](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L499-L505)），不需要枚举式守卫。

**练习 3**：CLI 层已有同样的拒绝，构建器层为何还要再来一遍？
**答案**：`start_engine_*` 是公共 API，嵌入方可以不经 CLI 直接调用；且测试头注释明说 builder 层测试「also cover guard wiring」——同一不变量多层设防，失败越早越便宜（与 u1-l3 的结论同构）。

## 5. 综合实践

**任务：写一页《为什么每个组合都不行》并给测试分层建索引。**

1. **五条拒绝论证页**：按下表把 4.5 的源码走读整理成一页文档，每条拒绝必须包含：判定顺序、错误文案关键词、机理归轴（网格轴/算法轴/TP 轴）、对应的深层防线（如 overlap 的 `launch_gemm_pin` bail 与 executor backstop）。特别标注：草稿器拒绝自 `af9e40e1` 起是通用 speculative 拒绝，DFlash 与 EAGLE-3（`Option<DraftModel>` 单槽，u1-l4）同遭拒绝，只是 CLI 文案尚未对齐（model_line.rs 仍写 DFlash，EAGLE-3 走冲突表反方向报错）。

2. **测试分层索引**：对照三个测试文件列出各自验证的层级与所需环境：

   | 测试 | 验证层级 | 需要什么 |
   | --- | --- | --- |
   | [tests/batch_invariance_reject.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_reject.rs) | 构建器层拒绝（CLI 层拒绝在 model_line.rs 的 tests 模块，见 u1-l3） | 无 GPU、无权重 |
   | [tests/batch_invariance_envelope.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_envelope.rs) | 数值包络：钉住的算法在恰好这些生产点（N=101/201/513/1024/1279、bs=256）零回退跑通 | 1 GPU + `PEGAINFER_TEST_MODEL_PATH` |
   | [tests/batch_invariance_endtoend.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_endtoend.rs) | 端到端逐位一致：prompt_a 单跑 vs 同批，Tuned 漂移（复现 bug）/ Pin 逐位同（修复）/ PerToken oracle | 1 GPU + `PEGAINFER_TEST_MODEL_PATH` |

3. **可跑部分**：无 GPU 环境先跑两条——`cargo test --release -p pegainfer-qwen3 --test batch_invariance_reject`（构建器层）与 `cargo test --release -p pegainfer-qwen3 --lib request_local`（网格轴单测）；有 GPU 再补 envelope 与 endtoend（endtoend 的运行命令在文件头注释里）。
4. **观察与预期**：reject 用例全部在入口拒绝且不污染全局策略；envelope 日志 `[envelope] Unified N=... served=... ok`；endtoend 在 Tuned 轮记录到 baseline 漂移（否则 Pin 通过是空洞的，测试自己会 fail）。本环境均未运行，**待本地验证**。
5. **发现记录**：若 `batch_invariance_rejects_dflash` 如 4.5.3 所析失败于过期文案断言，把它写进你的索引页备注——这是「报错文案推广后测试断言未跟上」的真实样本，也是阅读者能为仓库贡献的第一个修复点。

## 6. 本讲小结

- batch-invariant 靠两条腿：Pin（每 `(M,K)` 一个算法服务所有 N，零静默回退）+ request-local chunk 网格（chunk 序列只由请求自身长度决定）；`NumericPolicy` 是进程全局 AtomicU8，必须在执行器构造/图捕获前设置。
- `tune_decode_gemm_algos` 在图捕获前分流：Pin 走 6 组形状预热（`PIN_REP_N=32` 定点解析）+ 可选自检，Tuned 走逐 bucket 逐投影族计时扫参；envelope 自检只在长寿命服务 worker 上跑。
- `verify_pin_envelope` 用宿主侧 `AlgoCheck` 对每个生产可达 N 全量点检（默认参数下 5×1279 + 1024 = 7419 次），失败即拒绝启动——「承诺被验证，而非被假设」。
- request-local 整段-or-跳过让 chunk 边界与批组合解耦；前缀缓存命中（本地或主机层）会平移边界出网格，这就是缓存与 offload 双双被拒的机理。
- 五条构建器层拒绝 + LoRA 拒绝 + CLI 层 + executor backstop 构成四层设防；草稿器拒绝已推广为通用 speculative（DFlash 与 EAGLE-3 同拒），CLI 文案与新测试断言的滞后是当前 HEAD 上可发现的收尾工作。

## 7. 下一步学习建议

- 下一讲 u8-l2（HF golden 精度门）换一个视角看数值：batch-invariant 追求「与批组合无关的逐位一致」，golden gate 追求「与 HF 参考的距离有界」——两者容差哲学（位快照 vs 噪声地板）正好互补。
- 补充阅读同族测试：[tests/batch_invariance_attention_path.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_attention_path.rs)（注意力路径轴）、[tests/batch_invariance_decode_gemm_graph.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_decode_gemm_graph.rs)（图内 decode GEMM 轴）、[tests/batch_invariance_unified.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_unified.rs)（decode vs unified 路径轴）、[tests/batch_invariance_output.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/batch_invariance_output.rs)（引擎级 token/logprob 位一致）——每个文件头注释都标了自己负责的数值轴。
- 想深入 Pin 与注意力路径的联动，回读 u3-l8 的 `DecodeAttentionPath`：Pin/PerToken 无条件钉死 SplitKv 且 chunk 只依赖上下文上限，正是本讲「请求本地」性质在解码侧的镜像。
