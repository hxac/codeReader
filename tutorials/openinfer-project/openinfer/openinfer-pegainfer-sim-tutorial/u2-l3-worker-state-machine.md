# WorkerState 状态机（上）：请求阶段、准入与拒绝

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出（或默写）一个请求在 `Waiting` / `Prefill` / `Decode` 三个阶段之间的迁移条件，以及它如何离开状态机（完成或取消）。
2. 逐字段解释 `preflight` 中两类拒绝 —— `ModelLengthExceeded` 与 `WholePrefillExceedsStepBudget` —— 的判定表达式，并说明「同时违反两条时返回哪一个」。
3. 区分 `SubmissionResult` 的三种结果（`Queued` / `Finished` / `Rejected`）与 `submit` 返回 `Err`（重复 id）的本质差别，并解释 `output_tokens == 0` 短路的设计动机。
4. 会用 `request()` 快照和 `waiting_len` / `running_len` / `is_idle` 这组观察接口从外部检查状态机的内部状态。

本讲只讲「请求怎么进来、怎么被拒绝、停在哪个阶段」；「每一步具体装哪些活、怎么推进」属于下一讲（u2-l4）的 `plan_step` / `complete_step`。

## 2. 前置知识

### 2.1 承接前面的讲义

- u1-l1 / u1-l3 已建立整体图景：`SimScheduler` 有两条互斥车道，legacy 定时车道（u2-l1）不用 `worker.rs`；只有配置了 `--profile` 档案的车道才会构造 `WorkerState`，由 `ProfiledRuntime` 驱动。
- u2-l1 已讲清 prefill（算 prompt 的 KV，决定 TTFT）与 decode（逐 token 生成，决定 TPOT）这两个物理阶段。
- u2-l2 已讲清 `planned_completion`：模拟引擎的输出内容在准入时一次性定死，`planned_completion_len` 供本讲的 preflight 报预算。

### 2.2 本讲的新术语

| 术语 | 含义 |
|---|---|
| 确定性状态机 | 同样的输入序列永远得到同样的状态序列：没有时钟、没有随机数、没有线程竞争。`worker.rs` 里连一个 `Instant` 都没有——「这步做什么」归 worker，「这步睡多久」归 profile 的 `estimate_step`。 |
| `max_num_seqs` | 并发序列数上限：`running` 队列里最多同时住多少个请求。 |
| `max_num_batched_tokens` | 单个 step 的 token 预算：每个 decode 请求占 1 个槽位，prefill 按块占若干个。可以把一步想象成一条传送带，带上只有这么多 token 槽位。 |
| `max_model_len` | 单个请求的总长度上限（prompt + output），对应模型上下文窗口。 |
| `PrefillPolicy::Whole` | prefill 不许拆块：一个请求的整个 prompt 必须在同一个 step 里算完。 |
| `PrefillPolicy::Chunked { max_chunk_tokens }` | prefill 允许拆块：每步最多为一个请求算 `max_chunk_tokens` 个 prompt token，长 prompt 分多步消化。 |
| 泛型参数 `I` | 请求 id 的类型。worker 只数 token 个数、不看 token 内容，所以 id 用泛型就能满足一切需要（在 sim 里实参是 `RequestId`）。 |

### 2.3 vLLM V1 是什么背景

真实的 vLLM V1 调度器每一步遵循一条纪律：**先保证所有正在 decode 的请求每步拿到 1 个 token 槽位，再把剩余预算花在 prefill 上**。`worker.rs` 就是这条纪律（外加准入、拒绝、取消）的确定性再实现，目的是在没有 GPU 的环境里复现、预测调度行为。代码里的注释也直接点明了这一点（见 4.1 节引用）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| `src/worker.rs` | 确定性泛型调度状态机，本讲主角 | `RequestPhase`、`WorkerRequest`、`RequestRejection`、`SubmissionResult`、`WorkerState::submit/preflight/request` |
| `src/profile.rs` | 档案数据结构 | `SchedulerProfile`（约束的来源）、`PrefillPolicy`、`StepShape` |
| `src/lib.rs` | `ProfiledRuntime` 把 worker 接进前端 | `drain_submissions` 如何消费 `SubmissionResult` 三分支、`reject_reason` 如何翻译拒绝 |

## 4. 核心概念与源码讲解

### 4.1 RequestPhase：请求的三阶段生命周期

#### 4.1.1 概念说明

一个请求在 worker 眼里只有三个「阶段」：

- **Waiting**：已提交、已通过 preflight，躺在等待队列里，还没被任何一步接纳。
- **Prefill**：已被某一步接纳，正在分块计算 prompt 的 KV。
- **Decode**：prompt 的 KV 算完了，正在逐 token 产出。

注意枚举里**没有** `Finished` / `Cancelled` 这样的终态。终态不是「阶段」，而是「离开状态机」：完成或被取消的请求直接从 `running` 里移除，`request(id)` 从此返回 `None`。这是刻意设计——如果终态也是阶段，那么每个还在跑的请求在每个分支点上都要多判一个「不可能的变体」。

另外值得体会的是 `worker.rs` 的**关注点分离**：它是泛型的、纯逻辑的、确定性的，只回答「第 N 步装哪些活」；时间维度完全外包给 `profile.rs` 的 `estimate_step`。正因为它没有时间、没有随机，测试才能像 `generated_plans_never_exceed_sequence_or_token_capacity` 那样穷举几百种参数组合做性质测试。

#### 4.1.2 核心流程

请求的生命周期（迁移条件表）：

| 从 | 到 | 触发条件 | 代码位置 |
|---|---|---|---|
| （新请求） | `Waiting` | `submit` 通过 preflight 且 `output_tokens > 0` | `src/worker.rs:L266` |
| `Waiting` | `Prefill` | `plan_step` 准入时 `remaining_prefill_tokens > 0`，且按策略本步预算能装下一块（Whole 要求装下全部，Chunked 至少装 1 个） | `src/worker.rs:L388-L400` |
| `Waiting` | `Decode` | `plan_step` 准入时 `remaining_prefill_tokens == 0`（空 prompt 直通，`context_tokens = 0`） | `src/worker.rs:L388-L390` |
| `Prefill` | `Decode` | `complete_step` 消化完最后一块 prefill（`remaining_prefill_tokens` 减到 0），**同时产出第 1 个 output token** | `src/worker.rs:L502-L513` |
| `Decode` | （出队） | `complete_step` 中 `generated_tokens == output_tokens`，进入 `finished` | `src/worker.rs:L547-L549` |
| `Prefill`/`Decode` | （出队） | `output_tokens == 1` 时，prefill 收尾产出的那 1 个 token 就足以完成 | `src/worker.rs:L514-L516` |
| 任意 | （移除） | `cancel`（完整语义留给 u2-l5） | `src/worker.rs:L299-L317` |

文字版流程图：

```
submit（preflight 通过 且 output_tokens > 0）
        │
        ▼
    Waiting ──准入：prompt 为空──▶ Decode（context_tokens = 0）
        │
        │ 准入：prompt 有剩，本步装下一块
        ▼
     Prefill ──最后一块消化完（remaining = 0），产出第 1 个 token──▶ Decode
                                                                      │ 每步再产 1 个
                                                                      ▼
                                                     generated_tokens == output_tokens
                                                                      │
                                                                      ▼
                                                          出队（finished，request(id) 变 None）
```

两个容易忽略的细节：

- **`Prefill → Decode` 的迁移伴随首个 token**：真实引擎里最后一段 prefill 的 logits 本来就会给出第一个输出 token，如果迁移后再等一个 decode 步，TTFT 会凭空多出一步。代码注释原文写得很清楚。
- **空 prompt 直通 `Decode`**：没有 prompt 就没有 prefill 工作，准入它的那一步直接给它排 decode 活（`context_tokens = 0`）。回想 u2-l2：空 prompt 的输出内容走 `fallback_token_id`，两件事互不干扰。

#### 4.1.3 源码精读

阶段枚举本身只有三个变体：[src/worker.rs:L12-L17](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L12-L17) 定义了 `RequestPhase`，`Clone + Copy` 说明阶段是可随意复制的廉价标签。

请求进入状态机时的初始状态由 `RequestState::new` 决定：[src/worker.rs:L177-L187](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L177-L187) 把新请求置为 `Waiting`、`remaining_prefill_tokens = prompt_tokens`、`generated_tokens = 0`、`context_tokens = 0`。这个结构体是私有的——外部看不到 `RequestState`，只能通过下一节的快照。

状态机主体的存储布局：[src/worker.rs:L201-L207](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L201-L207)。`WorkerState` 持有调度画像 `scheduler`、FIFO 的 `waiting: VecDeque`、`running: Vec`，以及「当前已规划未完成的一步」`in_flight: Option<StepPlan>`。`waiting` 用双端队列是因为准入只从队首摘、新请求只从队尾进。

`Waiting → Prefill/Decode` 的分岔在准入循环里：[src/worker.rs:L388-L400](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L388-L400) 先看 `remaining_prefill_tokens == 0` 决定直通 Decode 还是切一块 prefill；如果预算连一块都装不下（`tokens == 0`）就 `break`，请求继续留在 Waiting。

`Prefill → Decode` 的迁移与首 token 产出：[src/worker.rs:L491-L517](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L491-L517)。每消化一块就把 `remaining_prefill_tokens` 减掉、把 `context_tokens` 加上（KV 已算出的 token 数）；减到 0 时改阶段、`generated_tokens + 1` 并产出 `GeneratedToken`，若恰好达到 `output_tokens` 甚至当场 finish。

Decode 的终点：[src/worker.rs:L547-L549](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L547-L549) 在 `generated_tokens == output_tokens` 时把 id 推入 `outcome.finished`；随后 [src/worker.rs:L558-L559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L558-L559) 的 `retain` 把 finished（和 cancelled）的请求从 `running` 里物理移除——这就是「终态即离场」。

约束的来源在 profile 侧：[src/profile.rs:L213-L221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L213-L221) 定义 `SchedulerProfile` 四个字段；[src/profile.rs:L293-L298](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L293-L298) 定义 `PrefillPolicy` 两个变体。`WorkerState::new` 在构造期就先 `scheduler.validate()`（[src/worker.rs:L213-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L213-L222)），保证后面所有判定面对的都是合法画像。

#### 4.1.4 代码实践（源码阅读 + 快照观察）

1. **实践目标**：用快照接口亲眼看到「新请求 = Waiting + 三个零计数」。
2. **操作步骤**：在 `src/worker.rs` 底部的 `#[cfg(test)] mod tests` 里临时追加一个测试（练习完删掉即可；本讲的实践都会改动测试代码，请只在练习副本上做）：

   ```rust
   #[test]
   fn snapshot_exposes_fresh_waiting_state() {
       let mut worker = WorkerState::new(scheduler(2, 4, 8, PrefillPolicy::Whole)).unwrap();
       assert_eq!(
           worker.submit(request(1, 3, 2)).unwrap(),
           SubmissionResult::Queued
       );
       let snap = worker.request(1).unwrap();
       assert_eq!(snap.phase, RequestPhase::Waiting);
       assert_eq!(snap.remaining_prefill_tokens, 3);
       assert_eq!(snap.generated_tokens, 0);
       assert_eq!(snap.context_tokens, 0);
       assert_eq!(snap.output_tokens, 2);
       assert!(!snap.cancel_requested);
       assert_eq!((worker.waiting_len(), worker.running_len()), (1, 0));
       assert!(!worker.is_idle());
   }
   ```

   运行：`cargo test --release -p pegainfer-sim --lib snapshot_exposes`（注意 workspace 的 default-members 不含本 crate，`-p` 不能省）。
3. **需要观察的现象**：所有断言的字段值与 `RequestState::new` 的初始化一一对应。
4. **预期结果**：测试通过（依据 `src/worker.rs:L177-L187` 的字段初始化推导；待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Waiting → Decode` 的直通迁移只在空 prompt 时发生？

**答案**：prefill 的工作就是把 prompt 的 KV 算出来。`remaining_prefill_tokens == 0` 意味着没有任何 prefill 工作可排，请求被接纳的那一步直接给它排 decode 活（`DecodeWork { context_tokens: 0 }`，见 `src/worker.rs:L408-L413`）。有 prompt 的请求必须先经过 Prefill 阶段消化掉这些 token。

**练习 2**：`RequestPhase` 为什么不设 `Finished` / `Cancelled` 变体？

**答案**：终态不是阶段而是离场——finished/cancelled 的请求被 `retain` 从 `running` 中物理移除（`src/worker.rs:L558-L559`），`request(id)` 返回 `None`。若终态也是枚举变体，所有还在跑的请求在每个匹配点上都要处理一个逻辑上不可能的分支，状态机反而更难保持穷尽和正确。

**练习 3**：`RequestSnapshot` 为什么做成 `Copy` 的值类型，而不是返回内部状态的引用？

**答案**：快照是「某一时刻的拷贝」，调用方拿到的数字不会被后续 step 改写，观测有一致性；同时底层 `RequestState` 是私有类型，值拷贝把内部表示和对外契约隔离开。

### 4.2 preflight：两类拒绝的判定表达式

#### 4.2.1 概念说明

`preflight` 是「准入前检查」：在请求占用任何队列位置之前，先判断它是否**根本不可能被服务**。它回答两个独立的问题：

1. **长度上不可能**：prompt + output 超过了模型上下文窗口 `max_model_len`。这与调度策略无关，换什么策略都救不了，所以最先判。
2. **预算上不可能（仅 Whole 策略）**：Whole 模式要求一个请求的整个 prompt 挤进同一个 step 的 token 预算，若 `prompt_tokens > max_num_batched_tokens`，这个请求永远等不到能装下它的那一步——与其无限排队，不如立刻拒绝。

用符号表示（\(P\) = prompt_tokens，\(O\) = output_tokens，\(L\) = max_model_len，\(B\) = max_num_batched_tokens）：

- 拒绝一（ModelLengthExceeded）：\( P + O > L \)
- 拒绝二（WholePrefillExceedsStepBudget）：\( O > 0 \land \text{policy} = \text{Whole} \land P > B \)

两条都满足时返回**拒绝一**——判定顺序体现了「更根本的能力上限优先」。

为什么拒绝二要加 \( O > 0 \) 这个条件？因为 `output_tokens == 0` 的请求会在 `submit` 里短路成 `Finished`（见 4.3），它永远不占用任何 step 预算，prompt 再长也不该被拒。

为什么 Chunked 策略没有对应的预算拒绝？因为 Chunked 会把 prompt 切成不超过 `max_chunk_tokens` 的块、分多步消化（`scheduled_prefill_tokens`，`src/worker.rs:L564-L581`），任何合法长度的 prompt 终究能被排进预算，preflight 阶段不存在「不可能」的可判定条件。

#### 4.2.2 核心流程

```text
preflight(prompt_tokens, output_tokens):
    total = prompt_tokens + output_tokens        # u64 饱和加
    若 total > max_model_len:
        返回 ModelLengthExceeded { total, max_model_len }
    若 output_tokens > 0 且策略为 Whole 且 prompt_tokens > max_num_batched_tokens:
        返回 WholePrefillExceedsStepBudget { prompt_tokens, max_num_batched_tokens }
    返回 None                                    # 放行
```

要点：

- 检查顺序固定：先长度、后预算；命中即返回，不收集多个错误。
- `total_tokens` 用 `saturating_add`：即便加法饱和到 `u64::MAX`，也必然大于任何 `max_model_len`，照样落入拒绝一——防御式写法让「溢出」不可能伪装成「放行」。
- `preflight` 是纯函数式的只读方法（`&self`），不改变任何状态；被拒绝的请求在 worker 里不留任何痕迹。

#### 4.2.3 源码精读

完整的判定逻辑：[src/worker.rs:L229-L251](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L229-L251)。第一段 `if` 判 \( P + O > L \) 并携带两个数字回来；第二段 `if` 是三条件合取，注意 `output_tokens > 0` 排在最前——最便宜的短路条件先验。

两类拒绝的定义与人类可读文案：[src/worker.rs:L26-L57](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L26-L57)。`RequestRejection` 是 `Clone + Copy` 的枚举，每个变体都携带判定时用到的数字，这让上游可以把 `max_model_len` 之类的限制原样报给前端，而不是只丢一句「不行」。`Display` 实现给出了两句报错文案（`"request total tokens ... exceed max_model_len ..."` / `"whole prefill has ... tokens but max_num_batched_tokens is ..."`）。

注意可见性：`preflight` 是 `pub(crate)` 而非 `pub`——它不打算成为公共 API，但 `lib.rs` 的 `ProfiledRuntime::drain_submissions` 需要在 `submit` **之前**单独调用它：[src/lib.rs:L389-L392](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L389-L392) 先 preflight，被拒的请求直接 `ledger.reject` 并 `continue`，不会触发前缀缓存计数（`prefix.note` 在 [src/lib.rs:L395](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L395)）也不会计算输出计划。顺带一提：对通过检查的请求，`submit` 内部还会再跑一次 preflight——纯函数、确定性，重复调用无害。

两类拒绝最终被翻译成前端可下发的 `RejectReason`：[src/lib.rs:L571-L583](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L571-L583) 把 `ModelLengthExceeded` 映射为 `ContextLength`（带 prompt/max_tokens/limit），把 `WholePrefillExceedsStepBudget` 映射为另一类原因——worker 的判定数字一路穿透到 HTTP 错误响应。

约束本身的合法性由画像校验兜底：[src/profile.rs:L223-L252](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L223-L252) 保证 `max_num_seqs`/`max_num_batched_tokens`/`max_model_len` 为正、`max_num_seqs <= max_num_batched_tokens`（每个 decode 请求每步吃 1 个 token 槽位，序列数超过预算必然有请求永远排不上）、Chunked 的块上限不超过预算。`WorkerState::new` 先调它，所以 preflight 面对的画像永远合法。

#### 4.2.4 代码实践（边界探测）

1. **实践目标**：用一组贴边输入验证两条判定的边界与顺序。
2. **操作步骤**：在 `src/worker.rs` 的 tests 模块里追加（`preflight` 是 `pub(crate)`，同 crate 测试可直接调用）：

   ```rust
   #[test]
   fn preflight_boundary_probes() {
       let whole = WorkerState::new(scheduler(2, 4, 8, PrefillPolicy::Whole)).unwrap();
       assert_eq!(whole.preflight(4, 4), None); // 4+4=8 == max_model_len，贴线放行
       assert_eq!(whole.preflight(5, 4), Some(RequestRejection::ModelLengthExceeded {
           total_tokens: 9, max_model_len: 8,
       })); // 9 > 8，且 5 > 4 两条都违反：长度拒绝优先
       assert_eq!(whole.preflight(4, 0), None); // prompt 贴预算线 + output=0：不触发预算拒绝
       assert_eq!(whole.preflight(5, 1), Some(RequestRejection::WholePrefillExceedsStepBudget {
           prompt_tokens: 5, max_num_batched_tokens: 4,
       })); // 长度合法（5+1=6 ≤ 8），仅违反 Whole 预算
       let chunked = WorkerState::new(scheduler(
           2, 4, 8, PrefillPolicy::Chunked { max_chunk_tokens: 2 })).unwrap();
       assert_eq!(chunked.preflight(7, 1), None); // Chunked 下超预算 prompt 不在准入前拒绝
   }
   ```

   运行：`cargo test --release -p pegainfer-sim --lib preflight_boundary`。
3. **需要观察的现象**：`preflight(5, 1)`（总长 6 合法、prompt 5 超预算 4）返回的是预算拒绝而非长度拒绝；`preflight(5, 4)`（两条都违反）返回长度拒绝。
4. **预期结果**：全部断言通过（依据 `src/worker.rs:L229-L251` 逐条件推导；待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：一个请求同时违反两条约束时，返回哪个拒绝？为什么？

**答案**：`ModelLengthExceeded`。第一段 `if` 先判总长（`src/worker.rs:L234-L240`）：上下文窗口是模型能力上限，与调度策略无关、更根本；预算问题只在长度合法时才有意义。

**练习 2**：为什么第二条判定要求 `output_tokens > 0`？

**答案**：`output_tokens == 0` 的请求随后会在 `submit` 里短路成 `Finished`，从不进入 waiting/running、从不占用任何 step 的 token 预算，因此「prompt 装不进一个 step」对它不构成障碍，拒绝反而是错的。

**练习 3**：`preflight` 里为什么用 `saturating_add` 而不是普通 `+`？

**答案**：`prompt_tokens: u32` 与 `output_tokens: u64` 相加若用窄类型会溢出 panic；提升到 u64 后饱和加保证即便饱和到 `u64::MAX` 也必然大于 `max_model_len`，溢出输入只会落入拒绝一，不可能伪装成放行。

### 4.3 submit：提交路径与 SubmissionResult 三分支

#### 4.3.1 概念说明

`submit` 是请求进入状态机的唯一入口。它按固定顺序做四件事：

1. **重复 id 防御**：`request(id)` 已存在则返回 `Err`（注意是 `Result` 的 Err，不是 `SubmissionResult` 的变体）。
2. **preflight**：不可能是请求 → `Rejected(rejection)`。
3. **零输出短路**：`output_tokens == 0` → `Finished`。
4. **入队**：其余情况推进 `waiting`，返回 `Queued`。

`SubmissionResult` 三分支各自的语义（对照 `lib.rs` 的消费方式理解最直观）：

| 变体 | worker 内部动作 | `ProfiledRuntime::drain_submissions` 的反应（lib.rs:L401-L433） |
|---|---|---|
| `Queued` | 请求进入 `waiting` | 记录输出计划等元数据，等后续 step 产 token |
| `Finished` | 什么都不存，请求从未入场 | `ledger.admit` → 可选 `echo_prompt` → 直接 `ledger.finish`，一次 step 都不发生 |
| `Rejected(rejection)` | 什么都不存 | `ledger.reject(reject_reason(...))`，把拒绝翻译成前端错误 |

三个设计判断值得咀嚼：

- **重复 id 是 `Err` 而不是第四个变体**：重复 id 不是「这个请求的属性」，而是调用方违反契约（`lib.rs` 用 `HashMap` 的 key 唯一性保证不会发生）。用 `Err` 让它走 `?` 传播成引擎错误，也永远不会被翻译成面向用户的拒绝原因。
- **`output_tokens == 0` 短路**：一个 `max_tokens = 0` 的请求没有任何工作可排。若把它塞进队列，`plan_step` 面对「有请求但无活可干」会触发 `"worker with queued or running requests produced an empty step"` 的 ensure 失败（`src/worker.rs:L431-L434`）；为它发明一个「零工作步」又会污染步序列。短路到 `Finished` 是最诚实的表达：它已经完成了，完成得毫无内容。
- **顺序敏感**：preflight 在短路**之前**，所以 `output_tokens == 0` 但总长超限的请求得到的是 `Rejected` 而不是 `Finished`——长度违规是能力问题，优先于「没事可做」。

#### 4.3.2 核心流程

```text
submit(request):
    若 request(id) 已存在            → Err("worker request id is already active")
    若 preflight(prompt, output) 命中 → Ok(Rejected(rejection))
    若 output_tokens == 0            → Ok(Finished)
    否则                              → waiting.push_back(新 RequestState)
                                      → Ok(Queued)
```

配套事实：

- `Rejected` / `Finished` 的请求从未进入 `waiting`/`running`，因此**同一个 id 可以立刻再次 submit**；只有 `Queued` 过的 id 在离场（finish/cancel）前是「活性」的。
- `submit` 不做任何预算分配——预算是 `plan_step`（下一讲）的事。准入期只判「永远不可能」和「无需调度」。

#### 4.3.3 源码精读

提交主路径：[src/worker.rs:L253-L268](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L253-L268)。`ensure!` 守住重复 id；随后依次 preflight、零输出短路、`waiting.push_back(RequestState::new(request))`。注意第一行的 `ensure!` 用的是 `self.request(request.id).is_none()`——复用了 4.4 节的查询方法，waiting 与 running 一并查过。

三种结果的定义：[src/worker.rs:L59-L64](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L59-L64)，`SubmissionResult` 只有 `Queued` / `Finished` / `Rejected(RequestRejection)` 三个变体。

前端侧的消费现场：[src/lib.rs:L401-L433](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L401-L433)。`drain_submissions` 把 `QueuedRequest` 逐个转成 `WorkerRequest` 提交，三个 match 分支正好对应上表：`Queued` 存 `ProfiledRequest` 元数据并断言无重复；`Finished` 走 admit/echo/finish 一条龙；`Rejected` 走 `ledger.reject`。这段代码也解释了为什么 worker 的返回值要设计成数据（枚举）而不是回调——调用方需要按分支做完全不同的记账。

空步防御（短路的动机之一）：[src/worker.rs:L431-L434](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L431-L434) 的 `ensure!` 表明：只要有请求在场，规划出的步必须含活。零输出请求若不短路，就会成为「在场但永远无活」的请求。

#### 4.3.4 代码实践（顺序实验）

1. **实践目标**：亲手验证「preflight 先于零输出短路」以及「非 Queued 的 id 不占用」。
2. **操作步骤**：在 `src/worker.rs` 的 tests 模块里追加：

   ```rust
   #[test]
   fn zero_output_short_circuits_only_after_preflight() {
       let mut worker = WorkerState::new(scheduler(2, 4, 8, PrefillPolicy::Whole)).unwrap();
       // 长度违规 + 零输出：Rejected 优先于 Finished
       assert_eq!(
           worker.submit(request(1, 9, 0)).unwrap(),
           SubmissionResult::Rejected(RequestRejection::ModelLengthExceeded {
               total_tokens: 9, max_model_len: 8,
           })
       );
       // prompt(8) 超 step 预算(4) 但零输出：不触发预算拒绝，直接 Finished
       assert_eq!(worker.submit(request(2, 8, 0)).unwrap(), SubmissionResult::Finished);
       // Finished/Rejected 的 id 不占用，可立即复用
       assert_eq!(worker.submit(request(2, 1, 1)).unwrap(), SubmissionResult::Queued);
       // Queued 的 id 是活性的：重复提交报 Err
       assert!(worker.submit(request(2, 1, 1)).is_err());
   }
   ```

   运行：`cargo test --release -p pegainfer-sim --lib zero_output_short_circuits`。
3. **需要观察的现象**：四个断言分别命中 Rejected、Finished、Queued、Err——与 `submit` 四步顺序一一对应。
4. **预期结果**：测试通过（依据 `src/worker.rs:L253-L268` 的判定顺序推导；待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`submit` 的返回类型是 `Result<SubmissionResult>`——外层 `Err` 和内层 `Rejected` 各代表什么？

**答案**：`Err` 是调用方违反契约（重复 id 这类「不可能发生在正确调用下」的程序员错误），会沿 `?` 传播为引擎错误；`Rejected` 是请求自身的属性（太长/装不下），是面向用户的正当拒绝，会被翻译成前端的 `RejectReason`。

**练习 2**：`output_tokens == 0` 的请求为什么必须短路，而不能排队等调度？

**答案**：它没有任何 prefill/decode 工作可排。若入场，`plan_step` 会遇到「有请求在场却规划不出任何活」的局面，触发 `src/worker.rs:L431-L434` 的空步 ensure；为它造一个「零工作步」又会污染步序列与步计时。短路成 `Finished`（前端侧 admit + finish 一步到位）是最小且诚实的语义。

**练习 3**：一个 id 先后两次 `submit` 都返回 `Finished`，中间需要做什么吗？

**答案**：什么都不需要。`Finished`（和 `Rejected`）的请求从未进入 `waiting`/`running`，`request(id)` 一直是 `None`，重复 id 防御不会命中。只有 `Queued` 过的 id 才需要等它 finish/cancel 离场后才能复用。

### 4.4 请求快照与容量观察：request() 与三个计数器

#### 4.4.1 概念说明

状态机的内部（`RequestState`、两个队列）是私有的，对外只开放一组**只读观察接口**：

- `request(id) -> Option<RequestSnapshot>`：按 id 找请求，返回七个字段的值拷贝快照。
- `waiting_len()` / `running_len()`：两个队列的长度。
- `is_idle()`：等待空、运行空、无在途步——三者同时成立才算闲。
- `in_flight() -> Option<&StepPlan>`：当前已规划未完成的步（本讲只知其存在，内容下一讲拆）。
- `scheduler() -> &SchedulerProfile`：回看构造时的画像。

这组接口的意义在于**可测试性**：单元测试不需要侵入私有字段，就能断言「submit 之后请求在 Waiting」「prefill 收尾后 context_tokens 涨到了 prompt 长度」。`worker.rs` 自带的性质测试 `generated_plans_never_exceed_sequence_or_token_capacity`（[src/worker.rs:L694-L748](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L694-L748)）全程只用这些观察器做断言。

#### 4.4.2 核心流程

```text
request(id):
    在 running 里线性找 id → 找到则返回快照
    在 waiting 里线性找 id → 找到则返回快照
    都没有 → None

is_idle():
    waiting.is_empty() && running.is_empty() && in_flight.is_none()
```

`RequestSnapshot` 七个字段速查：

| 字段 | 含义 | 何时变化 |
|---|---|---|
| `phase` | 当前阶段 | 准入时、prefill 收尾时 |
| `remaining_prefill_tokens` | 还没算 KV 的 prompt token 数 | 每消化一块 prefill 减少 |
| `generated_tokens` | 已产出 token 数 | prefill 收尾 +1、每个 decode 步 +1 |
| `context_tokens` | KV 已算出的 token 数（下一次 decode 查询前的上下文长度） | prefill 按块累加、decode 每步 +1 |
| `output_tokens` | 目标产出数（构造时定死） | 不变 |
| `cancel_requested` | 是否已被请求取消 | `cancel` 在步在途时置位（u2-l5） |

#### 4.4.3 源码精读

快照结构与那个关键注释：[src/worker.rs:L74-L83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L74-L83)。`context_tokens` 的文档注释写的是「Tokens whose KV has been computed before the next decode query」——它就是下一步 decode 要面对的上下文长度，也是 `StepShape.sum_decode_ctx_tokens` 的累加来源（下一讲会看到它参与步计时定价）。

查找实现：[src/worker.rs:L291-L297](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L291-L297)。先 `running` 后 `waiting` 链式查找。由于 `submit` 保证了 id 全局唯一，两个集合不会有同一个 id，查找顺序没有语义差别，只是实现选择；`map(RequestState::snapshot)` 把私有状态拷贝成公共快照。

观察器与闲判定：[src/worker.rs:L270-L288](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L270-L288)。`is_idle` 的三个条件缺一不可：`waiting` 空（无排队）、`running` 空（无在场请求）、`in_flight` 空（无已规划待完成的步）——最后一个条件保证「规划了但还没 complete」的瞬间不会被误判为闲。这些方法都标了 `#[must_use]`，提醒调用方它们是纯查询。

私有状态到快照的转换：[src/worker.rs:L189-L198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L189-L198) 逐字段拷贝，没有任何计算——快照永远等于当下内部状态。

#### 4.4.4 代码实践（观察者小测试）

1. **实践目标**：只用观察接口，跟踪两个请求从入队到清空的队列长度变化。
2. **操作步骤**：在 `src/worker.rs` 的 tests 模块里追加（`cancel` 的完整语义在 u2-l5，这里只借它「从 waiting 移除」的效果）：

   ```rust
   #[test]
   fn observers_track_waiting_queue_lengths() {
       let mut worker = WorkerState::new(scheduler(2, 4, 8, PrefillPolicy::Whole)).unwrap();
       worker.submit(request(1, 3, 2)).unwrap();
       worker.submit(request(2, 2, 2)).unwrap();
       assert_eq!((worker.waiting_len(), worker.running_len()), (2, 0));
       assert_eq!(worker.request(2).unwrap().phase, RequestPhase::Waiting);
       assert!(!worker.is_idle());
       assert!(worker.in_flight().is_none());

       assert_eq!(worker.cancel(1), CancelResult::Cancelled);
       assert_eq!(worker.waiting_len(), 1);
       assert!(worker.request(1).is_none());

       assert_eq!(worker.cancel(2), CancelResult::Cancelled);
       assert!(worker.is_idle());
   }
   ```

   运行：`cargo test --release -p pegainfer-sim --lib observers_track`。
3. **需要观察的现象**：`running_len` 全程为 0（还没规划过任何步）；两个请求都在 `waiting` 里被 `cancel` 直接移除。
4. **预期结果**：测试通过（依据 `src/worker.rs:L299-L303` 的 waiting 分支：在 waiting 里找到即移除并返回 `Cancelled`；待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`request()` 为什么先查 `running` 再查 `waiting`？顺序有语义差别吗？

**答案**：没有。`submit` 的重复 id 防御保证同一个 id 至多存在于两个集合之一，查找顺序不影响结果；先查 `running` 只是因为在多数场景下运行中的请求被查询得更频繁（线性扫描先扫更可能命中的集合）。

**练习 2**：`is_idle()` 的三个条件各自防住什么误判？

**答案**：只查 `waiting`/`running` 会漏掉「已 `plan_step` 但尚未 `complete_step`」的窗口——此时状态机仍有未了结的义务（必须 complete 才能继续 plan），`in_flight.is_none()` 补上这个洞。三个皆空才是真正的静止。

**练习 3**：一个 `prompt_tokens = 3` 的请求刚完成全部 prefill，此刻快照的 `context_tokens` 是多少？之后每完成一个 decode 步呢？

**答案**：prefill 收尾时 `context_tokens == 3`：`complete_step` 每消化一块就把 `work.tokens` 累加进去（`src/worker.rs:L491-L495`），全部消化完恰好等于 prompt 长度。此后每个 decode 步再 +1（`src/worker.rs:L535-L538`），因为刚产出的 token 的 KV 也算进了上下文。

## 5. 综合实践

把本讲四个模块串成一次「先推导、后验证」的完整练习。

### 5.1 目标

复刻并扩展 `impossible_and_duplicate_requests_are_rejected`（[src/worker.rs:L611-L637](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L611-L637)）：用同一份画像 `SchedulerProfile(max_num_seqs=2, max_num_batched_tokens=4, max_model_len=8, Whole)` 提交四种请求，断言四种结果；再换一组参数做「盲推」验证你的判定表达式。

### 5.2 步骤

1. **先跑原测试**：`cargo test --release -p pegainfer-sim --lib impossible_and_duplicate`。
2. **盖住答案，手推四种请求的结果**（画像 2/4/8/Whole）：

   | 请求 | prompt | output | 推导 | 预期结果 |
   |---|---|---|---|---|
   | id=1 | 7 | 2 | \(7+2=9>8\) | `Rejected(ModelLengthExceeded { 9, 8 })` |
   | id=2 | 5 | 1 | \(5+1=6\le8\)，但 \(O>0 \land \text{Whole} \land 5>4\) | `Rejected(WholePrefillExceedsStepBudget { 5, 4 })` |
   | id=3（两次） | 1 | 1 | 首次合法入队；第二次命中重复 id | 首次 `Queued`，第二次 `Err` |
   | id=4 | 4 | 0 | \(4+0=4\le8\)，\(O=0\) 跳过预算判定，短路 | `Finished` |

3. **写进测试**：在 `src/worker.rs` 的 tests 模块里照上表写一个新测试（就是原测试的复刻），跑通。
4. **盲推变体**：换成 `scheduler(3, 6, 12, PrefillPolicy::Whole)`，先在纸上写下以下请求的预期，再写测试对照：

   | 请求 | prompt | output | 预期 |
   |---|---|---|---|
   | id=1 | 7 | 2 | `WholePrefillExceedsStepBudget { 7, 6 }`（\(9\le12\)，\(7>6\)） |
   | id=2 | 13 | 1 | `ModelLengthExceeded { 14, 12 }` |
   | id=3 | 6 | 1 | `Queued`（\(6\le6\) 贴线放行） |
   | id=4 | 12 | 0 | `Finished`（\(12\le12\) 且零输出短路，prompt 超预算也不影响） |
   | id=5 | 13 | 0 | `Rejected(ModelLengthExceeded)`（长度判定先于短路） |

5. **收尾**：练习用的测试函数删除或保留均可（建议删掉，保持源码干净——本仓库的 AI 辅助贡献规范要求每处改动服务于明确的不变量）。

### 5.3 需要观察的现象

- 两种拒绝的判定顺序：同时违规时长度拒绝胜出。
- 零输出短路发生在 preflight 之后：`prompt=13, output=0` 得到 Rejected 而非 Finished。
- 重复 id 只对「活性」请求报错：`Queued` 之外的结果不占用 id。

### 5.4 预期结果

两个测试全部通过（以上预期均由 `src/worker.rs:L229-L268` 的判定式逐步推导；待本地验证）。如果盲推表里任何一格与实测不符，回到 4.2/4.3 的判定表达式找出推导错误——这正是本练习的训练点。

## 6. 本讲小结

- `worker.rs` 是纯逻辑、泛型、确定性的调度状态机：只决定「每步装什么」，不掺任何时间；时间由 profile 的 `estimate_step` 另行定价。
- 请求生命周期是 `Waiting → Prefill → Decode → 离场`：空 prompt 直通 Decode；prefill 收尾迁移到 Decode 的同时产出第 1 个 token（避免 TTFT 凭空多一步）；终态不是枚举变体而是从 `running` 物理移除。
- `preflight` 的两类拒绝：\(P+O>L_{\max}\)（能力上限，永远优先）与 \(O>0 \land \text{Whole} \land P>B\)（Whole 模式下永远装不进单步预算）；Chunked 模式无后者，因为拆块总能装下。
- `submit` 四步顺序固定：重复 id 报 `Err` → preflight 拒绝 → `output_tokens==0` 短路成 `Finished` → 入队返回 `Queued`；`Err` 是契约违规，`Rejected` 是请求属性，二者不可混同。
- 观察接口（`request` 快照、`waiting_len`/`running_len`/`is_idle`/`in_flight`）让测试无需触碰私有字段即可断言内部状态；`is_idle` 的三条件覆盖了「已规划未完成」的窗口。

## 7. 下一步学习建议

下一讲 u2-l4《WorkerState（下）：plan_step 的预算分配与 complete_step 的推进》接着拆同一文件的后半部分：

- `plan_step` 如何先保 decode、再用剩余预算做 prefill 与准入（`max_num_seqs` 与 `max_num_batched_tokens` 双上限的落地）；
- `scheduled_prefill_tokens` 在 Whole 与 Chunked 下的切块规则；
- `complete_step` 如何把 `StepPlan` 翻译成 `StepOutcome`（prefill 进度、产出 token、finished/cancelled 汇总与出队）；
- `StepShape` 三个分量如何成为步计时预测的键。

建议先精读 [src/worker.rs:L319-L454](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L319-L454)（plan_step）再对照本讲的迁移条件表，你会看到 `Waiting → Prefill/Decode` 的每一次迁移具体发生在哪一行。之后 u2-l5 讲 `cancel` 的四分支与中止清理，补完状态机的最后一块。
