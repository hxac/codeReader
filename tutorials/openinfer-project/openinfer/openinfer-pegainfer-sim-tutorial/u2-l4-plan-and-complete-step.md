# WorkerState（下）：plan_step 的预算分配与 complete_step 的推进

## 1. 本讲目标

上一讲（u2-l3）我们讲完了 `WorkerState` 的"静态"部分：请求如何提交、`preflight` 如何拒绝、`RequestPhase` 三阶段如何迁移。本讲进入它的"动态"核心——调度器的每一次心跳：

1. **`plan_step` 的预算分配**：一步之内先装 decode、再装 prefill、最后准入新请求，`max_num_seqs` 与 `max_num_batched_tokens` 双上限如何共同决定"这一步装什么"。
2. **`complete_step` 的推进语义**：prefill 收尾时为什么直接产出第一个输出 token、decode 如何逐 token 推进、完成的请求如何出队。
3. **`StepPlan` / `StepOutcome` / `StepShape` 三个契约类型**：尤其是 `StepShape`——它把一步的负载压缩成三个数字，正是后续（u3 单元）计时预测的查表键。

学完本讲，你应该能拿一份 `SchedulerProfile` 和一组请求，**手推**出完整的步序列：每一步的 shape、每一步谁被准入、谁产出了第几个 token、总共多少步。

## 2. 前置知识

本讲建立在 u2-l3 的概念之上，先花一分钟回顾：

- **`WorkerRequest<I>`**：`{ id, prompt_tokens, output_tokens }`，请求的全部静态属性。
- **`RequestPhase`**：`Waiting`（在排队队列里）→ `Prefill`（正在算 prompt 的 KV）→ `Decode`（正在逐 token 生成）。终态不是枚举变体，而是从 `running` 里物理移除。
- **`SchedulerProfile`**（定义在 [src/profile.rs:213-221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L213-L221)）：调度器的四个旋钮——`max_num_seqs`（最多同时跑几个请求）、`max_num_batched_tokens`（一步最多算多少 token）、`max_model_len`（单请求总长度上限）、`prefill`（`Whole` 或 `Chunked { max_chunk_tokens }`）。
- **step contract**（u1-l3 建立）：前端驱动 `Scheduler::step`，引擎一步最多给每个请求吐一个 token。本讲的 `plan_step`/`complete_step` 就是这个契约在 worker 内部的"一步"的两半。

再补两个本讲要用的直觉：

- **token 预算（budget）**：真实引擎一步要发动一次 GPU kernel，这次 kernel 能装的 token 数有上限。`max_num_batched_tokens` 就是这个上限的抽象。decode 一个请求一步恰好消耗 1 个 token（算一个新 token 的 KV 与 logits），prefill 则按块消耗。
- **"先 decode 后 prefill"**：这是 vLLM V1 调度器的真实策略。已经进入 decode 的请求对延迟最敏感（用户正盯着逐字输出），所以它们的 1 token/步必须先预留；剩下的预算才分给 prefill 和新请求。源码里专门留有注释说明这一点。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/worker.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs) | 本讲主角：`plan_step`、`complete_step`、`scheduled_prefill_tokens`，以及 `StepPlan`/`StepOutcome` 等契约类型和单元测试 |
| [src/profile.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs) | `StepShape`、`PrefillPolicy`、`SchedulerProfile` 的定义与校验（`validate`/`validate_shape`）、`estimate_step` 的入口 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs) | `ProfiledRuntime::step` 消费 `plan.shape()` 做步定价的位置——说明 shape 为什么是"键" |
| [tests/frontend_e2e.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs) | `replay_profiled_worker` 回放器：把 worker 步序列和 profile 计时串起来的集成测试样板 |

## 4. 核心概念与源码讲解

### 4.1 plan_step 预算分配：先 decode、后 prefill、再准入

#### 4.1.1 概念说明

`plan_step` 回答一个问题：**"下一步算什么？"**——注意它只做决定，不做时间，也不真正"算"任何 token。它的产出是一份不可变的施工图 `StepPlan`，真正推进状态要等 `complete_step`（见 4.3）。

这个"规划/完成"两段式是刻意的架构选择：把"装什么"（纯逻辑、确定性）与"这一步要花多久"（时间模型）解耦。在 profile 车道里，`lib.rs` 正是插在 plan 与 complete 之间，拿 `plan.shape()` 去查计时网格、睡到预计时刻才调 `complete_step`（见 4.4.3）。

预算分配遵循三条铁律：

1. **decode 优先**：所有已在 `Decode` 阶段的 running 请求，每个预留 1 个 token，绝不因为 prefill 挤占而掉拍。
2. **running prefill 其次**：已在 `Prefill` 阶段的请求按进入顺序分走剩余预算。
3. **新请求准入最后**：在剩余预算和 `max_num_seqs` 双重余量内，按 FCFS（先来先服务）从 `waiting` 队首准入。

#### 4.1.2 核心流程

```
plan_step():
  若已有 in_flight 步        → Err（一次只能有一步在飞）
  若 running、waiting 全空   → 返回 None（引擎空闲）

  budget = max_num_batched_tokens
  ① decode 循环：遍历 running，每个 Decode 阶段且未被取消的请求
       budget -= 1；累计 context_tokens 之和
  ② prefill 循环：遍历 running，每个 Prefill 阶段的请求
       tokens = scheduled_prefill_tokens(policy, 剩余, budget)
       tokens == 0 → 标记 prefill_blocked，停止（后续 running prefill 也不再分）
       否则 budget -= tokens
  ③ 准入循环：当 !prefill_blocked 且 budget > 0
       且 running.len() < max_num_seqs 且 waiting 非空：
       取队首请求；空 prompt 直接进 Decode（占 1 预算），
       否则按 scheduled_prefill_tokens 进 Prefill；
       分不到 token 就 break（不跳过队首去看后面的请求）

  shape = (decode 请求数, decode 上下文之和, 本步 prefill token 数)
  自检：shape 非空；decode ≤ max_num_seqs；总量 ≤ max_num_batched_tokens
  存为 in_flight，返回 &StepPlan
```

预算的消耗可以写成一行不等式，plan 结束时必须成立：

\[ n_{decode} + n_{prefill\_tokens} \le \text{max\_num\_batched\_tokens}, \qquad n_{decode} \le \text{max\_num\_seqs} \]

还有一个隐含的不变量：decode 循环开头的 `ensure!(remaining_budget > 0)` 理论上永远不会触发，因为 `SchedulerProfile::validate` 强制了 `max_num_seqs <= max_num_batched_tokens`（每个 decode 请求每步恰耗 1 token，所以 decode 永远付得起）——这条 profile 级约束正是"decode 优先"策略成立的前提。

#### 4.1.3 源码精读

入口守卫与空闲短路：[src/worker.rs:319-332](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L319-L332)——`plan_step` 拒绝在还有一步在飞时再规划；两边队列全空时返回 `Ok(None)`，调用方以此感知"没有活干"。`next_step_id` 用 `checked_add` 防溢出。

decode 优先循环：[src/worker.rs:337-352](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L337-L352)——注释直说 vLLM V1 的策略：所有 running decode 先调度，剩余 token 预算才花在 prefill 上。每个请求扣 1 预算、把它的 `context_tokens` 累进 `sum_decode_ctx_tokens`（这个和数将进入 `StepShape`，见 4.4）。

running prefill 循环：[src/worker.rs:354-376](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L354-L376)——按 `running` 向量顺序（即准入顺序）给每个 Prefill 阶段请求分 `scheduled_prefill_tokens` 算出的量；分到 0 就置 `prefill_blocked` 并 break。注意这是**队首阻塞**：第一个分不到的 prefill 会挡住后面所有 running prefill，保证先来的请求先走完。

准入循环：[src/worker.rs:378-423](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L378-L423)——四个条件共同控制循环：没有 prefill 被阻塞、还有预算、`running.len() < max_num_seqs`、还有排队请求。队首请求若 `remaining_prefill_tokens == 0`（空 prompt）直接进 `Decode`、只占 1 预算（[src/worker.rs:408-413](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L408-L413)）；否则按策略分 prefill token，分不到就 `break`——**不会跳过队首去搜刮后面更小的请求**，FCFS 语义严格到不越过一个无法准入的队首。准入的请求立刻 `push` 进 `running`，所以同一步内准入的请求与老请求在 `StepPlan` 里平起平坐。

shape 组装与自检：[src/worker.rs:425-453](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L425-L453)——用 `u32::try_from` 把 decode 数收窄后构造 `StepShape`，随后三条 `ensure!` 复核预算双上限与"非空步"（有请求在却规划出空步是逻辑 bug，直接报错而不是静默空转），最后存入 `in_flight` 并返回引用。

#### 4.1.4 代码实践

**实践目标**：验证你真的会手推预算分配，而不是只会读文字。

1. 阅读 `WorkerState` 的字段定义 [src/worker.rs:201-207](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L201-L207)，确认 `waiting` 是 `VecDeque`（队首弹出 O(1)）而 `running` 是 `Vec`（保序）。
2. 手推：`scheduler(max_num_seqs=2, max_num_batched_tokens=6, max_model_len=16, Chunked { max_chunk_tokens: 3 })`，同步提交请求 A`(prompt=1, output=2)` 与 B`(prompt=3, output=2)`，写出第一步的 `admitted`、`prefill` 明细与 `shape`。
3. 运行现有测试确认环境可用：

```bash
cargo test --release -p pegainfer-sim --lib context_and_generated_progress_end_in_one_terminal_outcome -- --nocapture
```

4. 在你自己的工作副本里（例如 `src/worker.rs` 的 `#[cfg(test)] mod tests` 中）仿照 [src/worker.rs:639-669](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L639-L669) 写一个小测试，把第 2 步的手推值写成断言（下述代码为**示例代码**）：

```rust
#[test]
fn first_step_budget_is_prefill_only_for_two_prompts() {
    let mut worker = WorkerState::new(scheduler(2, 6, 16, PrefillPolicy::Chunked { max_chunk_tokens: 3 })).unwrap();
    worker.submit(request(1, 1, 2)).unwrap();
    worker.submit(request(2, 3, 2)).unwrap();

    let plan = worker.plan_step().unwrap().unwrap();
    assert_eq!(plan.admitted(), &[1, 2]);
    assert_eq!(plan.prefill(), &[PrefillWork { request_id: 1, tokens: 1 }, PrefillWork { request_id: 2, tokens: 3 }]);
    assert_eq!(plan.shape(), StepShape { decode_reqs: 0, sum_decode_ctx_tokens: 0, prefill_tokens_in_step: 4 });
}
```

**需要观察的现象**：第一步没有任何 decode（两个请求都是新准入的非空 prompt），prefill 共消耗 \(1+3=4\le 6\) 个预算，两个请求同一步被准入。

**预期结果**：断言全部通过（预算 6 足够装下 1+3，且 `max_num_seqs=2` 恰好容纳两个请求）。测试运行结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`max_num_seqs=4`、`max_num_batched_tokens=8`，四个 running 请求全在 Decode。某步还能准入几个 token 的 prefill？如果这时 `waiting` 里有一个 prompt=6 的请求、策略是 `Chunked { max_chunk_tokens: 4 }`，它会被准入吗？

答案：decode 消耗 4，剩余预算 \(8-4=4\)。准入还受 `running.len()=4 = max_num_seqs` 限制，所以一个也进不来——`max_num_seqs` 是**并发请求数**上限，与 token 预算是两个独立的闸门。即使没有 seq 限制，`min(6, 4, 4) = 4` 会准入并装 4 个 prefill token；但本例中 seq 上限先挡住了它。

**练习 2**：为什么 running prefill 循环里 `tokens == 0` 要设置 `prefill_blocked` 并因此连**新请求准入**也一并停掉，而不是让更小的 waiting 请求先挤进来？

答案：两个原因。其一是 FCFS 公平性：waiting 准入严格从队首开始，如果被阻塞的 running prefill 之后放行更小的 waiting 请求，等于让后来者插队；其二是预算语义：`tokens == 0` 而 `remaining_budget > 0` 只会发生在 `Whole` 策略下"剩余 prefill 大于剩余预算"，此时继续搜刮预算只会让被阻塞者等更久。宁可本步收尾，下一步预算重置后再推进。

**练习 3**：`plan_step` 为什么返回 `Result<Option<&StepPlan>>` 而不是直接 `Option<StepPlan>`？

答案：`Err` 留给调用方契约违规与内部不变量被打破（重复规划、预算超限、溢出）——这些是 bug，应当带上下文上抛（u2-l1 讲过错误上抛而非 panic 的风格）；`Ok(None)` 则是正常业务信号"引擎空闲，没有下一步"，`lib.rs` 的 ProfiledRuntime 据此直接返回继续下一轮。返回引用是因为计划已存进 `self.in_flight`，调用方拿着引用读取即可，`complete_step` 再按 `StepId` 结算。

### 4.2 scheduled_prefill_tokens 与 PrefillPolicy：一次调度给多少

#### 4.2.1 概念说明

`scheduled_prefill_tokens` 是一个不到 20 行的纯函数，却是两条 prefill 策略的分水岭。它回答："这个请求这一步能装多少 prefill token？"

- **`Whole`（整块）**：要么全装，要么不装。`remaining <= budget` 时装 `remaining`，否则 0。
- **`Chunked { max_chunk_tokens }`（分块）**：装 \(\min(\text{remaining},\ \text{max\_chunk\_tokens},\ \text{budget})\)，永远尽量推进。

策略定义在 [src/profile.rs:293-298](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L293-L298)，是 `SchedulerProfile` 的一部分，随 JSON 档案一起加载（serde tag 为 `mode`，即 `{"mode": "whole"}` 或 `{"mode": "chunked", "max_chunk_tokens": N}`）。

#### 4.2.2 核心流程

```
scheduled_prefill_tokens(policy, remaining, budget):
  Whole                     → remaining <= budget ? remaining : 0
  Chunked { max_chunk }     → min(remaining, max_chunk, budget)
```

两条策略的后果对比：

| 维度 | Whole | Chunked |
|------|-------|---------|
| 长 prompt 的命运 | prompt > `max_num_batched_tokens` 且要输出 → 提交即被 preflight 拒绝（u2-l3 的 `WholePrefillExceedsStepBudget`） | 逐块推进，永不饿死 |
| 步数 | 少（一次装完） | 多（prompt ÷ max_chunk_tokens 向上取整的 prefill 段数） |
| 单步 shape | 大起大落（整段 prompt 一次入步） | 平滑（每步 prefill ≤ max_chunk_tokens） |
| 对 running decode 的延迟 | 一次大 prefill 步可能把步时拉长 | 步型受控，decode 掉拍风险小 |
| 输出集合 | 与 Chunked **完全相同**（见 4.2.4 与综合实践） | 同左 |

最后一行是本讲最重要的不变量：**chunk 大小只改变"步怎么切"，不改变"最终算了什么"**。这解释了为什么 `chunk_cap_changes_same_step_admission_without_changing_outputs` 这个测试名里敢写 "without changing outputs"。

#### 4.2.3 源码精读

函数本体：[src/worker.rs:564-581](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L564-L581)——`Whole` 分支是全有或全无；`Chunked` 分支一行三量取最小。注意它对 `remaining == 0` 的输入在 `Whole` 下返回 0（`0 <= budget` 恒真，返回 `remaining = 0`）——不过实践中不会发生：空 prompt 在准入时就直接进了 Decode，不进入 prefill 循环。

策略的合法性由 profile 校验兜底：[src/profile.rs:241-250](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L241-L250)——`max_chunk_tokens` 必须为正且不超过 `max_num_batched_tokens`（超过没有意义：一步装不下更多）。

与 preflight 的呼应：[src/worker.rs:241-249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L241-L249)——`Whole` 策略下 `output_tokens > 0` 且 `prompt_tokens > max_num_batched_tokens` 直接拒绝。为什么 `output_tokens == 0` 豁免？因为零输出请求在 `submit` 里走的是另一条短路（直接 `Finished`，u2-l3），根本不会进 prefill 循环——被拒绝的分支不可达，所以不设卡。这就是 `Whole` 与 `scheduled_prefill_tokens` 返回 0 的"饿死"路径形成互补：**要么进不来，要么一定装得下**，`Whole` 下永远不会出现"进了 running 却永远分不到预算"的请求。

#### 4.2.4 代码实践

**实践目标**：用一个穷举式属性测试，验证 chunk 上限在**任意**参数组合下都不破坏预算不变量。

1. 阅读并运行 [src/worker.rs:693-748](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L693-L748) 的 `generated_plans_never_exceed_sequence_or_token_capacity`：

```bash
cargo test --release -p pegainfer-sim --lib generated_plans_never_exceed_sequence_or_token_capacity -- --nocapture
```

2. 观察它三重循环枚举 `max_num_seqs ∈ 1..=4`、`max_num_batched_tokens ∈ max_num_seqs..=8` 以内、`max_chunk_tokens ∈ 1..=max_num_batched_tokens`，每组提交 12 个形状各异的请求，循环 `plan_step`/`complete_step` 直至 `is_idle()`，逐步断言四条性质：
   - `shape.decode_reqs <= max_num_seqs`；
   - `shape.decode_reqs + shape.prefill_tokens_in_step <= max_num_batched_tokens`；
   - 每条 prefill work 的 `tokens <= max_chunk_tokens`；
   - `plan.decode()` 的 `context_tokens` 之和恰等于 `shape.sum_decode_ctx_tokens`。
3. 思考题自测：把策略换成 `Whole`、`max_num_batched_tokens=4`，提交 `(prompt=5, output=1)` 会怎样？（答案见 4.2.5 练习 2。）

**需要观察的现象**：测试通过；无论 chunk 上限多小，worker 最终都 `is_idle()` 且 12 个请求全部终结——分块永不饿死。

**预期结果**：通过（该测试是仓库既有测试）。运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`Chunked { max_chunk_tokens: 2 }`、剩余 prefill 5、剩余预算 4，本步装几个？下步开始时剩多少 prefill？

答案：\(\min(5, 2, 4) = 2\)。本步后剩 3，下步再装 \(\min(3,2,\cdot)=2\)，第三步装 1——三段切完。

**练习 2**：`Whole`、`max_num_batched_tokens=4`，提交 `(prompt=5, output=1)` 与 `(prompt=5, output=0)` 各返回什么？

答案：前者 `SubmissionResult::Rejected(WholePrefillExceedsStepBudget { prompt_tokens: 5, max_num_batched_tokens: 4 })`（u2-l3 讲过的第二类拒绝，判定条件正是"策略为 Whole"）；后者 `SubmissionResult::Finished`——零输出短路发生在 preflight 之后、入队之前，从源头消除了"Whole 下 prompt 装不进预算"的死锁分支。

**练习 3**：把 `max_chunk_tokens` 调大（比如等于 `max_num_batched_tokens`），`Chunked` 与 `Whole` 还有区别吗？

答案：几乎等价但仍有语义差：`Chunked` 允许 prompt 超过 `max_num_batched_tokens` 的请求进入（preflight 的第二类拒绝只在 `Whole` 下触发），此时首段装满预算、后续段在下几步装完；`Whole` 直接拒绝。即 chunk 策略同时改变了"谁能进来"与"怎么切步"。

### 4.3 complete_step 推进语义：prefill 收尾产首 token、decode 逐 token、完成即出队

#### 4.3.1 概念说明

`complete_step` 是"施工"：按 `StepPlan` 把状态推进一格，并把发生的事情以 `StepOutcome` 报告出来。它必须与 `plan_step` 严格交替——调 complete 必须传入当前在飞步骤的 `StepId`，传错即 `Err`。

三个推进规则：

1. **prefill 推进**：`remaining_prefill_tokens -= tokens`、`context_tokens += tokens`。当 remaining 归零，请求迁移到 `Decode`，并且**立刻产出第 1 个输出 token**——这就是源码注释里说的"最后一段 prefill 的 logits 直接产出首个输出 token，再单独走一步 decode 会凭空多出一个 TTFT 步"。
2. **decode 推进**：`context_tokens += 1`（刚生成的 token 的 KV 并入上下文）、`generated_tokens += 1`，产出 `token_index`（1-based）。
3. **收割**：`generated_tokens == output_tokens` 的请求进 `finished`；被标记取消的请求全部进 `cancelled`；两者随后从 `running` 里物理移除。

#### 4.3.2 核心流程

```
complete_step(step_id):
  校验 in_flight 存在且 id 匹配 → 取出 plan
  ① 按 plan.prefill 逐条：
       找到 running 中的请求；被取消 → 跳过
       校验 phase==Prefill、0 < tokens <= remaining（防陈旧计划）
       remaining -= tokens；ctx += tokens；记录 PrefillProgress
       若 remaining == 0：
         phase → Decode；generated_tokens += 1；产出 token_index
         若 generated == output → finished
  ② 按 plan.decode 逐条：
       找到请求；被取消 → 跳过
       校验 phase==Decode、ctx == work.context_tokens、generated < output
       ctx += 1；generated += 1；产出 token_index
       若 generated == output → finished
  ③ cancelled = 所有 cancel_requested 的 running 请求
     running.retain(非取消 且 未 finished)
```

TTFT 的算术：对一个 prompt 长 \(p\)、输出 \(k\) 的请求，从准入到首 token 需要 \(\lceil p / c \rceil\) 步（\(c\) 为每步实际装进的 prefill 量），而**首 token 出现在最后一段 prefill 所在的那一步**，不需要额外一步 decode。若没有这条规则，TTFT 会多一个整步。

#### 4.3.3 源码精读

id 匹配守卫：[src/worker.rs:456-468](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L456-L468)——`complete_step` 要求传入的 id 与在飞步骤一致，`in_flight.take()` 把计划取走，保证一步恰好被结算一次。

prefill 推进与首 token：[src/worker.rs:476-518](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L476-L518)——先做防御校验（阶段正确、token 量在剩余范围内），再扣减 remaining、累加 ctx，压入一条 `PrefillProgress`。关键在 [src/worker.rs:502-517](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L502-L517)：remaining 归零时阶段迁移到 `Decode`，`generated_tokens` 从 0 加到 1 并立刻 push 一条 `GeneratedToken { token_index: 1 }`；若 `output_tokens == 1`（比如脚本化补全只要一个 token 的场景），同一步就进 `finished`。注释（[src/worker.rs:504-505](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L504-L505)）写明了动机：单独再走一步 decode 会给 TTFT 凭空加一步。

decode 推进：[src/worker.rs:520-550](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L520-L550)——校验三元组里最值得注意的是 `request.context_tokens == work.context_tokens`：计划里记录的 ctx 是"这次 decode query 之前已有的 KV 长度"，若结算时对不上，说明计划与状态已经脱节（调用方用错了顺序），立即报错。随后 `ctx += 1`（新 token 的 KV 入池）、`generated += 1`，压入 `GeneratedToken`，同样在追平 `output_tokens` 时进 `finished`。`generated_tokens < output_tokens` 的校验则保证不会超发。

取消收割与出队：[src/worker.rs:552-559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L552-L559)——所有 `cancel_requested` 的 running 请求（无论本步是否被 plan 跳过）统一进 `cancelled`；`retain` 一次性移除取消与完成的请求。终态 = 离开 `running`，这与 u2-l3"终态不是枚举变体"的设计闭环。

`outcome.generated` 的顺序值得记住：**prefill 产出的 token 在前（按 `plan.prefill` 顺序），decode 产出的在后（按 `plan.decode` 顺序）**——见 [src/worker.rs:469-474](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L469-L474) 的容量预估就已经暗示了这个分组。

#### 4.3.4 代码实践

**实践目标**：用现有测试亲眼确认"prefill 收尾即首 token"与 token_index 的推进。

1. 阅读 [src/worker.rs:639-669](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L639-L669) 的 `context_and_generated_progress_end_in_one_terminal_outcome`：`scheduler(1, 4, 16, Whole)`，请求 `(prompt=3, output=3)`。
2. 运行：

```bash
cargo test --release -p pegainfer-sim --lib context_and_generated_progress_end_in_one_terminal_outcome -- --nocapture
```

3. 对照断言梳理这条四步时间线（` Whole` 一次装完 prompt 3）：
   - 步 0（prefill）：remaining 3→0，ctx=3，产出 `token_index 1`；
   - 步 1（decode）：shape 的 `sum_decode_ctx_tokens = 3`，ctx=4，`token_index 2`；
   - 步 2（decode）：`sum_decode_ctx_tokens = 4`，ctx=5，`token_index 3`，`generated == output` → `finished = [1]`；
   - 此后 `request(1)` 返回 `None`，`plan_step()` 返回 `None`。
4. 思考并手推：若把 `output_tokens` 改成 1，这个请求在第几步 finished？（答案：步 0，prefill 收尾产首 token 的同一步。）

**需要观察的现象**：首 token 的 `token_index` 出现在 prefill 那一步的 outcome 里；decode 步的 `sum_decode_ctx_tokens` 逐步 +1（3 → 4）。

**预期结果**：测试通过（仓库既有测试）；第 4 步的手推结论可直接改参数后加断言验证（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 decode 校验要求 `request.context_tokens == work.context_tokens`，而 prefill 校验没有对应条款？

答案：decode work 里携带的 `context_tokens` 是规划时刻"这次 query 之前"的 KV 长度快照；从 plan 到 complete 之间请求状态不应变化（两阶段之间没有其他修改通道），对不上就意味着调用方违反了"plan→complete 交替、id 匹配"的契约。prefill work 只携带 token 数量，其等效守卫是 `work.tokens <= request.remaining_prefill_tokens`——同样是在防"陈旧计划"。

**练习 2**：一个 `(prompt=2, output=2)` 的请求在 `Chunked { max_chunk_tokens: 1 }` 下，从准入到 finished 需要几步？每步各产出什么？

答案：4 步。步 1：prefill 1 token（remaining 2→1）；步 2：prefill 1 token（remaining→0）并产出 `token_index 1`；步 3：decode 产出 `token_index 2`，`generated == output` → finished。若 budget 允许同一步装更多（cap=2），则 2 步完成：步 1 prefill 全装完并产首 token，步 2 decode 产第二 token 即终结。

**练习 3**：`StepOutcome.finished` 收割后，同一个请求 id 还能再次出现在后续任何 StepPlan 里吗？

答案：不能。`retain` 已把它从 `running` 移除，而 `plan_step` 只从 `running` 与 `waiting` 取材；除非调用方对**同一 id** 再次 `submit`（允许——u2-l3 只禁止"仍然活跃"的 id 重复），它才会作为全新请求从头走 Waiting→Prefill→Decode。

### 4.4 StepPlan / StepOutcome / StepShape：一步的契约与计时预测的键

#### 4.4.1 概念说明

三个类型构成 worker 与外界（以及 worker 与计时模型）之间的契约：

- **`StepPlan<I>`**：施工图。字段全部私有，只能经 `id()` / `shape()` / `admitted()` / `prefill()` / `decode()` 读取——`worker.rs` 不希望任何调用方在计划生成后篡改它。
- **`StepOutcome<I>`**：施工报告。四份清单：`prefill`（每请求的进度与剩余）、`generated`（每请求产出的 token 序号）、`finished`、`cancelled`。
- **`StepShape`**：把一步的**负载**压缩成三个数——`decode_reqs`（decode 请求数）、`sum_decode_ctx_tokens`（这些请求的上下文长度之和）、`prefill_tokens_in_step`（本步 prefill token 总数）。它**刻意丢弃请求身份**：两个完全不同的请求组合，只要负载三元组相同，对计时模型而言就是同一种步。

`StepShape` 是本讲的桥：u3 单元将讲 `EngineProfile` 如何用它查 `TimingGrid` 做三线性插值得到 `duration_us`。丢身份正是为了让查表空间有限——请求 id 无限，负载组合有限。

#### 4.4.2 核心流程

```
StepPlan（不可变）
  ├─ id: StepId            —— 结算凭证，complete_step 必须对上
  ├─ shape: StepShape      —— 计时键：负载三元组
  ├─ admitted: Vec<I>      —— 本步新准入的请求（前端据此发"已受理"）
  ├─ prefill: Vec<PrefillWork>   —— (请求, 本步 prefill token 数)
  └─ decode: Vec<DecodeWork>     —— (请求, 该请求解码前 ctx 长度)

StepOutcome（一次性报告）
  ├─ prefill: Vec<PrefillProgress>  —— (processed, remaining, ctx)
  ├─ generated: Vec<GeneratedToken> —— (请求, 1-based token_index)
  ├─ finished: Vec<I>
  └─ cancelled: Vec<I>

StepShape（纯值，Copy）
  ├─ decode_reqs: u32
  ├─ sum_decode_ctx_tokens: u64
  └─ prefill_tokens_in_step: u32
```

`StepShape` 的合法性约束（`SchedulerProfile::validate_shape`）：非空（decode 与 prefill 至少其一大于 0）、`decode_reqs <= max_num_seqs`、`decode_reqs + prefill_tokens_in_step <= max_num_batched_tokens`、`decode_reqs == 0` 时 `sum_decode_ctx_tokens` 必须为 0、`sum_decode_ctx_tokens <= decode_reqs × max_model_len`。`plan_step` 末尾的自检（4.1.3）与这套约束一一对应——worker 产出的每一步天然能通过计时模型的形状校验。

#### 4.4.3 源码精读

`StepPlan` 及其只读访问器：[src/worker.rs:108-142](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L108-L142)——字段私有 + `#[must_use]` 访问器，构造只发生在 `plan_step` 内部。`StepId` 是 `u64` 的新类型包装（[src/worker.rs:85-93](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L85-L93)），防止调用方拿裸整数伪造步骤凭证。

`PrefillWork` / `DecodeWork`：[src/worker.rs:95-106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L95-L106)——注意 `DecodeWork.context_tokens` 的文档注释："计算这次 decode query 之前已有的 KV 长度"，即 attention 的 query 长度恒为 1、KV 长度为该值。

`StepOutcome` 三兄弟：[src/worker.rs:144-164](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L144-L164)——`PrefillProgress` 报告 processed/remaining/context 三元组，`GeneratedToken` 的 `token_index` 从 1 计起（对应 u2-l2 讲过的 profiled 车道"pending 反转存储按 1-based 取值"）。

`StepShape` 定义：[src/profile.rs:300-305](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L300-L305)——`Copy` 纯值类型，放在 `profile.rs` 而非 `worker.rs`，因为它同时是计时预测的输入。形状校验在 [src/profile.rs:254-290](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L254-L290)，其中 `max_num_seqs <= max_num_batched_tokens` 的总约束在 [src/profile.rs:237-240](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L237-L240)（理由写得很直白：每个 decode 请求每步吃 1 token）。

shape 作为计时键的用法：[src/lib.rs:335-368](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L335-L368)——`ProfiledRuntime::step` 在 `plan_step` 之后立刻 `estimate_step(plan.shape())`（[src/lib.rs:338-340](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L338-L340)），把估出的微秒换算成 `ready_at` 时刻存进 `in_flight`；下一轮循环发现未到点就按 `WAIT_SLICE` 切片睡（u2-l1 讲过），到点才 `complete_step`。`estimate_step` 本体在 [src/profile.rs:101-119](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L101-L119)：先 `validate_shape`，再查 coverage，最后网格插值——细节留给 u3。

集成侧的回放器：[tests/frontend_e2e.rs:299-367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L299-L367) 的 `replay_profiled_worker` 把三个固定请求喂给 worker，循环 plan→estimate→complete，把每步的 shape、估时、admitted/prefill/decode 记进 `PlanTrace`（[tests/frontend_e2e.rs:269-276](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L269-L276)）；[tests/frontend_e2e.rs:405-440](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L405-L440) 断言两次回放逐位相同、且必须出现过 prefill 步、decode 步与**互不相同的估时**——最后一条正是在验证"shape 不同 → 计价不同"。

#### 4.4.4 代码实践

**实践目标**：亲眼看到"同一负载 → 同一步时"的键语义，以及 worker 回放的确定性。

1. 阅读 `replay_profiled_worker`（链接同上），注意它复用 `profile.scheduler` 构造 `WorkerState`——调度参数与计时数据同源，这是 profile 车道"档案是唯一真相"（u1-l2）的体现。
2. 运行（纯 CPU，无需 GPU，但需要编译整个 crate）：

```bash
cargo test --release -p pegainfer-sim --test frontend_e2e profiled_worker_replay_is_deterministic_and_bounded -- --nocapture
```

3. 在你自己的工作副本里给 `PlanTrace` 加一行打印（或写个新测试收集 trace），找出 shape 不同的两步，核对它们的 `duration_us` 不同；再找出（若存在）shape 相同的两步，核对 `duration_us` 逐位相同。
4. 对照 `online-step-gate.json` fixture（[tests/fixtures/online-step-gate.json](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/fixtures/online-step-gate.json)）里的 `scheduler` 段与 `grid` 段，确认回放中出现的每个 shape 都落在 grid 的覆盖域内（u3-l2 会展开"覆盖域"概念）。

**需要观察的现象**：两次回放的 `PlanTrace` 列表完全相等；trace 中既有 `prefill_tokens_in_step > 0` 的混合步，也有纯 decode 步；不同 shape 对应不同 `duration_us`。

**预期结果**：测试通过（仓库既有测试）；打印观察**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`decode_reqs = 3, sum_decode_ctx_tokens = 10, prefill_tokens_in_step = 0` 与 `decode_reqs = 1, sum_decode_ctx_tokens = 10, prefill_tokens_in_step = 0` 是同一种步吗？计时模型怎么看？

答案：对 worker 不是（涉及不同请求组合），对计时模型是**两种**不同的步——shape 三元组不同就是不同的键。前者是"3 个短上下文请求各decode 一步"，后者是"1 个长上下文请求 decode 一步"；attention 的计算量分别近似 \(3 \times\)（短 KV）与 \(1 \times 10\)（长 KV），网格插值允许它们定价不同。

**练习 2**：为什么 `sum_decode_ctx_tokens` 用 `u64`，而另两个字段用 `u32`？

答案：它是多个请求上下文之和，可能远超单请求上限——`validate_shape` 里它的天花板是 `decode_reqs × max_model_len`（[src/profile.rs:280-288](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/profile.rs#L280-L288)），两个 `u32` 相乘必须用 `u64` 承接；`plan_step` 累加时也全程 `checked_add`（[src/worker.rs:345-347](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L345-L347)）。

**练习 3**：`StepPlan` 为什么不直接携带"这步该睡多久"？

答案：职责分离。`worker.rs` 是纯逻辑状态机，对时间一无所知；时间属于 `profile.rs` 的计时模型。`lib.rs` 的 ProfiledRuntime 在 plan 与 complete 之间插入了"查 shape → 估时 → 定 ready_at → 睡觉"的环节（[src/lib.rs:338-368](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L338-L368)）。这样同一个 `WorkerState` 可以配任何档案（或未来配真实计时）而不改一行调度代码——4.4.4 实践里 `replay_profiled_worker` 直接复用它就是证明。

## 5. 综合实践

**任务**：写一个"chunk 上限回放对比"测试，亲手验证本讲的核心不变量——**chunk 大小改变步的切法，不改变最终输出集合**。

样板是仓库既有的 [src/worker.rs:750-929](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L750-L929) `chunk_cap_changes_same_step_admission_without_changing_outputs`（它对比 cap=6 与 cap=2）。我们换一组参数与请求，对比 **cap=4 与 cap=1**。

**场景设定**（手推锚点已给出，均由 4.1–4.3 的规则逐步推出）：

- 调度器：`scheduler(max_num_seqs=4, max_num_batched_tokens=8, max_model_len=128, Chunked { max_chunk_tokens })`
- 先提交 req1 `(prompt=0, output=3)`、req2 `(prompt=0, output=3)`，走完第一步；
- 再提交 req3 `(prompt=10, output=2)`（本场景的"长 prompt"）、req4 `(prompt=1, output=1)`；
- 循环 plan→complete 直到 `is_idle()`，逐步记录 shape / admitted / prefill / generated / finished。

**操作步骤**（以下测试代码为**示例代码**，加到你自己的工作副本 `src/worker.rs` 测试模块中）：

```rust
#[test]
fn cap_one_vs_cap_four_replay_changes_steps_not_outputs() {
    struct Trace { shapes: Vec<StepShape>, generated: Vec<(u32, u32)>, finished: Vec<u32>, steps: usize }

    fn replay(max_chunk_tokens: u32) -> Trace {
        let mut worker = WorkerState::new(scheduler(4, 8, 128, PrefillPolicy::Chunked { max_chunk_tokens })).unwrap();
        worker.submit(request(1, 0, 3)).unwrap();
        worker.submit(request(2, 0, 3)).unwrap();
        let mut shapes = Vec::new();
        let mut generated = Vec::new();
        let mut finished = Vec::new();
        let mut steps = 0;
        let mut drain = |worker: &mut WorkerState<u32>, shapes: &mut Vec<StepShape>,
                         generated: &mut Vec<(u32, u32)>, finished: &mut Vec<u32>| -> bool {
            let Some(plan) = worker.plan_step().unwrap() else { return false };
            shapes.push(plan.shape());
            // cap 不改变"每条 prefill work 不超过上限"这一性质
            assert!(plan.prefill().iter().all(|w| w.tokens <= max_chunk_tokens));
            let id = plan.id();
            let outcome = worker.complete_step(id).unwrap();
            generated.extend(outcome.generated.into_iter().map(|t| (t.request_id, t.token_index)));
            finished.extend(outcome.finished);
            true
        };
        // 第 0 步：两个空 prompt 请求直接进 decode
        assert!(drain(&mut worker, &mut shapes, &mut generated, &mut finished));
        worker.submit(request(3, 10, 2)).unwrap();
        worker.submit(request(4, 1, 1)).unwrap();
        while drain(&mut worker, &mut shapes, &mut generated, &mut finished) {}
        assert!(worker.is_idle());
        let mut finished = finished; finished.sort_unstable();
        let mut generated = generated; generated.sort_unstable();
        Trace { shapes, generated, finished, steps }
    }
    // ...对比断言见下
}
```

（注意闭包捕获 `max_chunk_tokens` 需要它按值拷贝进闭包或改成参数传入——示例从简，写测试时把 `drain` 改成接收 cap 的普通内部函数更清晰，可参考原测试的 `record_step` 写法。）

**手推锚点**（用于核对你跑出来的 trace；这些值由本讲规则逐步推出，属"预期结果"，**待本地验证**）：

| 观察点 | cap=4 | cap=1 |
|--------|-------|-------|
| 总步数 | 5（id 0..4） | 12（id 0..11） |
| 第 0 步 shape | `{decode_reqs: 2, sum_ctx: 0, prefill: 0}`，admitted `[1,2]` | 同左 |
| 第 1 步 shape | `{2, 2, 5}`，admitted `[3,4]`，prefill `[(3,4),(4,1)]` | `{2, 2, 2}`，admitted `[3,4]`，prefill `[(3,1),(4,1)]` |
| 第 2 步 shape | `{2, 4, 4}`（req1/req2 本步 finished） | `{2, 4, 1}`（req1/req2 本步 finished） |
| 第 3 步起 | 仅剩 req3：第 3 步 prefill 2 token 收尾并产 `token_index 1`，第 4 步 decode 产 `token_index 2` → finished | 仅剩 req3：第 3..9 步每步 1 个 prefill token（shape `{0,0,1}`），第 10 步收尾产首 token，第 11 步 decode → finished |
| 最终 finished | `[1,2,3,4]` | `[1,2,3,4]` |
| 最终 generated（排序后） | `[(1,1),(1,2),(1,3),(2,1),(2,2),(2,3),(3,1),(3,2),(4,1)]` 共 9 条 | 与左**完全相同** |

**需要观察的现象与断言要点**：

1. `cap_one.finished == cap_four.finished == [1,2,3,4]`；`cap_one.generated == cap_four.generated`（排序后逐位相等）——输出集合不变。
2. `cap_one.steps == 12`、`cap_four.steps == 5`——步数差约等于"长 prompt 被切成多少段"。
3. 每条 prefill work 的 `tokens <= max_chunk_tokens`；每步 `decode_reqs + prefill_tokens_in_step <= 8`、`decode_reqs <= 4`。
4. cap=1 的 trace 中出现一串 `decode_reqs == 0` 的纯 prefill 步（只剩 req3 时的 `{0,0,1}`）——这正好印证 `validate_shape` 的"非空步"约束与 `sum_decode_ctx_tokens == 0 ⟺ decode_reqs == 0` 约束都能被真实序列满足。
5. 思考题：为什么第 1 步两种 cap 都把 req3 和 req4 **同时**准入？（原测试里 cap=6 只准入 `[3]`。）答案：decode 用掉 2 个预算后还剩 6；cap=4 时 req3 取 4、剩 2 ≥ 1，req4 还进得来；cap=1 时各取 1 更是宽裕。原测试 cap=6 下 req3 一次取 6 把预算吃到 0，准入循环因 `remaining_budget > 0` 不成立而停——挡住 req4 的是**预算**，不是 seq 上限。

## 6. 本讲小结

- `plan_step` 只做决定不做时间：**decode 优先**（每请求 1 token，靠 `max_num_seqs <= max_num_batched_tokens` 这条 profile 约束保证永远付得起）→ running prefill 按序分块 → waiting 按 FCFS 准入，`max_num_seqs` 与 `max_num_batched_tokens` 是两道独立的闸门。
- `scheduled_prefill_tokens` 是两条策略的分水岭：`Whole` 全有或全无（装不下的请求在 preflight 就被拒，运行中永不饿死），`Chunked` 恒取 \(\min(\text{剩余}, \text{cap}, \text{预算})\) 永远推进。
- `complete_step` 三段推进：prefill 扣 remaining 加 ctx；**remaining 归零当步迁移到 Decode 并直接产出 `token_index 1`**（否则 TTFT 凭空多一步）；decode 每步 ctx+1、generated+1；追平 output 即 finished，与取消请求一起被 `retain` 出队。
- `StepPlan` 不可变（私有字段 + 只读访问器），`StepId` 是结算凭证；`StepOutcome` 是一次性施工报告，`generated` 里 prefill 产出的 token 排在 decode 产出的前面。
- `StepShape = (decode_reqs, sum_decode_ctx_tokens, prefill_tokens_in_step)` **丢掉请求身份**，把一步负载压成计时预测的查表键——`lib.rs` 的 ProfiledRuntime 正是插在 plan 与 complete 之间用 shape 估时、睡到 `ready_at` 才结算。
- 核心不变量（综合实践验证）：**chunk 上限只改变步的切法与步数，不改变最终 generated/finished 集合**。

## 7. 下一步学习建议

本讲结束后，你已经完整掌握 worker 的"装什么、怎么推进"。接下来两条路：

1. **u2-l5（取消语义）**：`cancel` 的四种返回（`Cancelled`/`Deferred`/`AlreadyRequested`/`NotFound`）如何与 `plan_step` 跳过 `cancel_requested` 请求、`complete_step` 统一收割 `cancelled` 的机制配合——本讲已多次碰到这个标记，下一讲把它讲透。
2. **u3 单元（引擎档案）**：本讲反复出现的 `StepShape` 在 u3-l2（`SchedulerProfile`/`PredictorCoverage` 校验）与 u3-l3（`TimingGrid` 三线性插值）里从"键"变成主角；u3-l4 再把 `ProfiledRuntime` 的完整在线车道（准入 → 定价 → 睡眠 → 结算）合拢。

建议继续精读的源码：`src/worker.rs` 的测试模块（本讲引用的三个测试是手推能力的最好标尺），以及 `tests/frontend_e2e.rs` 的 `replay_profiled_worker`——它示范了"worker + profile"这对组合的标准用法。
