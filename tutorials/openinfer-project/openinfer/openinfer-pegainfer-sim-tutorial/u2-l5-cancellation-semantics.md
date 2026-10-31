# 取消语义：CancelResult 四分支与中止清理

## 1. 本讲目标

学完本讲，你应该能够：

1. 枚举 `CancelResult` 的四种返回（`Cancelled` / `Deferred` / `AlreadyRequested` / `NotFound`），并说出每一种对应请求在 `WorkerState` 中所处的位置（waiting 队列、running 列表、已被移除）。
2. 解释「in_flight 期间只能标记、complete_step 之后统一收割」这条设计约束的根源：`StepPlan` 是一份按 id 引用 running 请求的不可变施工图，施工期间抽走任何一根梁都会让结算失败。
3. 跟踪前端 `abort()` 的原子标记如何沿 `ProfiledRuntime::cancel_aborted` → `worker.cancel` → `apply_outcome` → `cancel_worker_requests` 这条链，最终传导为 `ledger.retire`。
4. 区分 `ledger.retire` 与 `ledger.finish` 对前端可见性的差别：一个静默销账、什么都不发；一个写入 `Terminal::Finished` 并随 step 流交付。

本讲只讲「请求是怎么被中途撤掉的」，不引入新的调度规则——所有预算分配与推进语义都来自上一讲（u2-l4）。

## 2. 前置知识

### 2.1 前端的 abort 是一个原子布尔

客户端断开连接或超时后，前端不会给调度器发一条「取消消息」，而是在请求的控制句柄上置一个标志位：

- [pegainfer-frontend/src/engine/request_lifecycle.rs:L192-L198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/request_lifecycle.rs#L192-L198) — `RequestControl::abort` 用 `Release` 序把一个 `Arc<AtomicBool>` 写成 `true`。注意它是「标记」，不是「通知」：没有任何唤醒机制，调度器只能在下一次碰这个请求时自己去读。

对应的读取端是 `RequestLedger::is_aborted`（`Acquire` 序）。也就是说，**abort 可以在任意时刻落地，而发现它的时间点完全由调度器的循环结构决定**。本讲的核心问题就是：在「plan → （模拟计算）→ complete」的两相步骤里，哪些时间点能发现 abort、发现了之后各自能做什么。

### 2.2 复习：两相步骤与 in_flight

上一讲讲过 `WorkerState` 的动态核心是两个函数：

- `plan_step()`：产出一份 `StepPlan`（哪些请求做 prefill、哪些做 decode），存进 `self.in_flight`，只决策不推进；
- `complete_step(step_id)`：按同一份 plan 结算，产出 `StepOutcome`（进度、生成的 token、完成与取消清单）。

`in_flight: Option<StepPlan<I>>` 这个字段就是「引擎正在执行这一步」的表示。本讲的关键约束全部围绕它展开：**plan 还没结算时，running 列表里的请求一个都不能动**。

### 2.3 复习：ledger 是调度器与前端之间的账本

`RequestLedger` 给每个请求开一个「账户」：`admit` 开户激活、`push_tokens` 记流水、`finish`/`reject`/`fail` 销户并写终态、`retire` 静默销户。调度器每次 `step` 都拿着 `&mut RequestLedger`，一边推进一边记账。本讲第 4.4 节会精读 `retire` 与 `finish` 的本质差别。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `pegainfer-sim/src/worker.rs` | 确定性调度状态机 | `CancelResult`、`WorkerState::cancel`、`cancel_requested` 标记、`complete_step` 的 cancelled 汇总 |
| `pegainfer-sim/src/lib.rs` | 模拟引擎装配与两条车道 | `ProfiledRuntime::cancel_aborted`、`apply_outcome`、`cancel_worker_requests`、legacy 车道的两处 `retire` |
| `pegainfer-frontend/src/engine/ledger.rs` | 请求账本（`pegainfer-sim` 之外，链接用同一 commit 的完整路径） | `is_aborted` / `is_active` / `finish` / `retire` |
| `pegainfer-frontend/src/engine/request_lifecycle.rs` | 请求控制句柄 | `RequestControl::abort` 的原子写 |

本讲涉及的两个关键测试（也是代码实践的参考样板）：

- [src/worker.rs:L671-L691](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L671-L691) — `cancellation_after_nonterminal_step_completion_removes_running_request`：非终态步完成后取消，请求被立即移除。
- [src/lib.rs:L975-L1012](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L975-L1012) — `late_abort_cleanup_removes_nonterminal_worker_request`：迟到的 abort 在 `cancel_worker_requests` 里被清理，幸存请求不受影响。

## 4. 核心概念与源码讲解

### 4.1 WorkerState::cancel 与 CancelResult 四分支

#### 4.1.1 概念说明

`WorkerState` 里一个请求同一时刻只可能出现在三个地方：

1. **waiting 队列**（`VecDeque<RequestState<I>>`）——已提交、尚未被任何 plan 收编；
2. **running 列表**（`Vec<RequestState<I>>`）——已被某次 plan 准入，正在 prefill 或 decode；
3. **哪儿都不在**——已 finished / 已 cancelled，被物理移除（上一讲讲过：终态不是枚举变体，而是「从 running 里删掉」）。

`cancel(id)` 要把请求从这三个位置之一处理掉，返回值 `CancelResult` 就是「我是在哪个位置、以什么方式处理掉的」：

- `Cancelled`：请求已在本调用中被物理移除（来自 waiting，或来自无 in_flight 时的 running）；
- `Deferred`：请求在 running 且当前有一步正在执行，只能先打标记，等 `complete_step` 收割；
- `AlreadyRequested`：请求已被标记过（上一次 cancel 已返回 `Deferred`），本次是重复取消，幂等返回；
- `NotFound`：waiting 和 running 都没有这个 id——要么从未提交，要么早已终结。

#### 4.1.2 核心流程

`cancel` 是一棵按位置查询的决策树：

```text
cancel(id)
  ├─ id 在 waiting 中？ ── 是 ── 从 waiting 移除 ────────────── 返回 Cancelled
  │                        （请求从未进入任何 plan，直接删是安全的）
  ├─ id 在 running 中？ ── 否 ──────────────────────────────── 返回 NotFound
  ├─ 该请求已带 cancel_requested 标记？ ── 是 ──────────────── 返回 AlreadyRequested
  ├─ in_flight 存在（有一步正在执行）？
  │     ├─ 是 ── 只置 cancel_requested = true ──────────────── 返回 Deferred
  │     └─ 否 ── 从 running 直接移除 ───────────────────────── 返回 Cancelled
```

为什么 waiting 可以直接删、running 要看 in_flight 脸色？因为 `StepPlan` 里存的是 id（`admitted` / `prefill` / `decode` 三个 Vec），`complete_step` 结算时要拿着这些 id 回 running 列表里逐个找回请求——找不到就报错（见 4.2.3 的两处 `.context("planned ... request is no longer running")`）。waiting 里的请求**绝不会出现在任何 plan 中**（准入的那一刻它就从 waiting 挪进了 running），所以删了没有后遗症；running 里的请求可能正被 in_flight 的 plan 引用着，施工期间抽走它会让整步结算失败。

#### 4.1.3 源码精读

枚举定义——四个变体就是四种「请求此刻在哪」的答案：

- [src/worker.rs:L66-L72](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L66-L72) — `CancelResult` 是 `Clone + Copy + Eq` 的简单枚举，不带任何附加数据：调用方只需要知道结果类别，位置信息由调用方自己掌握。

`cancel` 的完整实现，与上面决策树逐行对应：

- [src/worker.rs:L299-L317](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L299-L317) — 先查 waiting（L300-L303，找到即移除并返回 `Cancelled`）；再查 running（L304-L306，找不到返回 `NotFound`）；已标记则幂等返回 `AlreadyRequested`（L307-L309）；最后按 `in_flight.is_some()` 分岔——有步在飞就只置 `cancel_requested = true` 返回 `Deferred`（L310-L312），否则直接从 running 移除返回 `Cancelled`（L313-L316）。

标记本身存在 `RequestState` 上，并且通过快照对外可见：

- [src/worker.rs:L166-L174](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L166-L174) — `RequestState` 的最后一个字段就是 `cancel_requested: bool`，`new` 时初始化为 `false`（L185）。
- [src/worker.rs:L74-L83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L74-L83) — `RequestSnapshot` 把 `cancel_requested` 暴露出来（L82），所以调用方在收到 `Deferred` 之后可以用 `worker.request(id)` 观察到「已标记、尚未移除」的中间态。

`in_flight` 字段本身与两处入口守卫：

- [src/worker.rs:L201-L207](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L201-L207) — `WorkerState` 的五个字段：调度画像、waiting、running、`in_flight: Option<StepPlan<I>>`、步 id 计数器。
- [src/worker.rs:L319-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L319-L326) — `plan_step` 开头 `ensure!(self.in_flight.is_none(), ...)`：同一时刻最多一步在飞；running 与 waiting 全空则返回 `Ok(None)`（这也是后面实践中「取消干净之后 plan_step 返回 None」的来源）。

#### 4.1.4 代码实践

**实践目标**：亲手触发 `CancelResult` 的全部四个分支，并观察 `Deferred` 之后请求的中间态。

**操作步骤**（在本地练习分支上，向 [src/worker.rs:L583-L610](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L583-L610) 的 `mod tests` 里追加一个测试；`scheduler` 与 `request` 两个辅助函数该模块已有，直接复用）：

```rust
#[test]
fn cancel_covers_all_four_branches() {
    let mut worker = WorkerState::new(scheduler(2, 8, 16, PrefillPolicy::Whole)).unwrap();

    // 分支四：从未提交过
    assert_eq!(worker.cancel(99), CancelResult::NotFound);

    // 分支一：还在 waiting，直接移除
    worker.submit(request(1, 2, 2)).unwrap();
    assert_eq!(worker.cancel(1), CancelResult::Cancelled);
    assert!(worker.request(1).is_none());

    // 分支二 + 三：in_flight 期间先 Deferred，再重复取消得 AlreadyRequested
    worker.submit(request(2, 0, 2)).unwrap();
    let step_id = worker.plan_step().unwrap().unwrap().id();
    assert_eq!(worker.cancel(2), CancelResult::Deferred);
    assert!(worker.request(2).unwrap().cancel_requested); // 已标记、未移除
    assert_eq!(worker.cancel(2), CancelResult::AlreadyRequested);

    let outcome = worker.complete_step(step_id).unwrap();
    assert_eq!(outcome.cancelled, vec![2]);
}
```

运行：

```bash
cargo test --release -p pegainfer-sim --lib cancel_covers_all_four_branches -- --nocapture
```

**需要观察的现象**：

1. `cancel(1)` 发生在任何 `plan_step` 之前，请求 1 还躺在 waiting 里，于是立即消失；
2. `cancel(2)` 发生在 `plan_step` 之后（空 prompt 的请求 2 被准入为 Decode 并进入 running），此时 in_flight 非空，只能标记；
3. 快照里 `cancel_requested == true`，但 `request(2)` 仍返回 `Some`——「标记 ≠ 移除」。

**预期结果**：测试通过，四个分支各被命中一次。若把 `worker.cancel(1)` 挪到 `plan_step` 之后，它会改走 `Deferred` 分支——这就是位置决定分支的直接证据。

（本实践为教程设计的本地练习，测试代码不是仓库原有内容；命令输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：一个请求先后收到两次 `cancel`，第一次返回 `Deferred`，第二次返回什么？如果第一次返回的是 `Cancelled`，第二次又返回什么？

**答案**：`Deferred` 之后请求仍带着标记留在 running，第二次命中 `AlreadyRequested`（幂等，调用方无需自己记「我取消过没有」）。`Cancelled` 之后请求已从状态机中物理移除，第二次会命中 `NotFound`。

**练习 2**：为什么 `cancel` 对 waiting 里的请求可以无条件立即移除，对 running 里的却要先看 `in_flight`？

**答案**：`StepPlan` 按 id 引用 running 中的请求，`complete_step` 结算时要求每个被规划的请求仍在 running（否则报错「planned ... request is no longer running」）。waiting 里的请求从未被任何 plan 引用（准入即迁移到 running），删除无副作用；running 里的请求可能正被 in_flight 的 plan 引用，必须等这一步结算完才能安全移除。

**练习 3**：`AlreadyRequested` 这个变体的存在为调用方省掉了什么负担？

**答案**：取消的幂等性。没有它，调用方（例如 `ProfiledRuntime::cancel_aborted` 每步都要扫一遍所有请求）就必须自己维护一张「已取消过的 id」表来避免重复处理的歧义；有了它，重复取消是一个明确的、可忽略的返回值。

### 4.2 cancel_requested 标记与 complete_step 的 cancelled 收割

#### 4.2.1 概念说明

`Deferred` 只是把请求「判了刑」，行刑发生在 `complete_step`。这套「标记 → 跳过 → 汇总 → 移除」的流水线有三个环节：

1. **跳过计算**：结算时，plan 里属于已标记请求的 prefill/decode 工作直接 `continue`——这个请求在本步内不再产生任何进度、任何 token、任何 finish；
2. **汇总**：结算末尾扫一遍 running，把所有带标记的 id 收进 `outcome.cancelled`——这是给上层（`ProfiledRuntime`）的正式通知：「这些人被取消了」；
3. **移除**：`retain` 一次性把「已标记」与「已 finished」的请求都从 running 里删掉。

为什么不在 `cancel` 里就删、非要等一步？除了 4.1 说的 plan 一致性，还有一个语义原因：`StepOutcome` 是「这一步实际发生了什么」的完整报告。取消如果发生在一执行步中间，那么「该请求的取消在本步生效」本身就是这条 outcome 的一部分——上层需要在同一份报告里看到「谁被取消了」，才能同步清理自己的元数据与账目（见 4.3）。

#### 4.2.2 核心流程

一次带取消的完整两相步骤：

```text
plan_step()        产出 StepPlan；in_flight = Some(plan)
   │
cancel(id)         请求在 running 且被 plan 引用 → 置 cancel_requested，返回 Deferred
   │                （此期间新计划也不会再给该请求派活，见 4.2.3 第一条）
   ▼
complete_step(sid) for prefill work: 若请求已标记 → continue（不记 PrefillProgress）
                   for decode  work: 若请求已标记 → continue（不生成 token）
                   汇总：running 中所有带标记者 → outcome.cancelled
                   retain：移除「带标记者」与「finished 者」
   │
   ▼
worker.request(id) → None；plan_step() → Ok(None)（若无其他请求）
```

#### 4.2.3 源码精读

**第一条：标记者不会再被派新活。** `plan_step` 的两个分配循环都把已标记请求排除在外：

- [src/worker.rs:L339-L342](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L339-L342) — decode 分配循环的条件 `request.phase != RequestPhase::Decode || request.cancel_requested` 直接跳过已标记请求；
- [src/worker.rs:L357-L360](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L357-L360) — prefill 分配循环同理。

（值得思考：按 4.1 的分析，`cancel` 只在 in_flight 非空时标记，而带标记者总会在随后那次 `complete_step` 里被 `retain` 移除；in_flight 为空时 `cancel` 直接移除。所以沿着 API 契约的正常顺序推演，`plan_step` 入口其实遇不到带标记的请求——这两处判断是防御性守卫，练习 1 会让你自己推一遍这条链。）

**第二条：结算时跳过已标记请求的工作，且什么都不记录。**

- [src/worker.rs:L476-L484](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L476-L484) — prefill 结算：先按 id 在 running 里找回请求（找不到即报错，这是 plan 一致性的守卫），`if request.cancel_requested { continue; }` 在任何进度写入之前就跳过——连 `PrefillProgress` 都不记；
- [src/worker.rs:L520-L528](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L520-L528) — decode 结算同样：找回、检查标记、`continue`，不产 `GeneratedToken`。

**第三条：汇总与移除——`complete_step` 的收尾两拍。**

- [src/worker.rs:L552-L557](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L552-L557) — `outcome.cancelled.extend(...)`：过滤 running 中所有 `cancel_requested` 的请求，把 id 收进本步报告；
- [src/worker.rs:L558-L559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L558-L559) — `retain(|request| !request.cancel_requested && !outcome.finished.contains(&request.id))`：取消者与完成者在这一行一起物理出队。终态的实现方式与上一讲 finished 完全对称——都不是枚举变体，而是「从 running 里消失」。

`StepOutcome.cancelled` 字段的定义：

- [src/worker.rs:L158-L164](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L158-L164) — 与 `finished: Vec<I>` 平级的一个 `cancelled: Vec<I>`，二者都是「本步出队清单」，只是出队原因不同。

参考样板——仓库自己的取消测试（非终态步完成后取消，此时 in_flight 已空，走的是「立即移除」分支）：

- [src/worker.rs:L671-L691](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/worker.rs#L671-L691) — `cancellation_after_nonterminal_step_completion_removes_running_request`：请求 1（prompt 0、output 3）在第一步生成了 token_index 1 但没完成（L677-L686），随后 `cancel(1)` 返回 `Cancelled`（L688）、请求消失（L689）、`plan_step` 返回 `None`（L690）。

#### 4.2.4 代码实践

**实践目标**：完整走一遍「in_flight 期间的 Deferred 取消 → complete_step 收割 → 状态机归于空闲」的时序，验证三件事：`outcome.cancelled` 收到该请求、`worker.request(id)` 变 `None`、再次 `plan_step` 返回 `None`。

**操作步骤**（同样加在 `src/worker.rs` 的 `mod tests` 里）：

```rust
#[test]
fn deferred_cancel_is_harvested_by_the_next_complete_step() {
    let mut worker = WorkerState::new(scheduler(1, 4, 16, PrefillPolicy::Whole)).unwrap();
    worker.submit(request(1, 0, 3)).unwrap(); // 空 prompt：准入即 Decode

    // 一步在飞，请求已进入 running 且被 plan 引用
    let step_id = worker.plan_step().unwrap().unwrap().id();

    // in_flight 非空：只能标记，不能移除
    assert_eq!(worker.cancel(1), CancelResult::Deferred);
    assert!(worker.request(1).is_some()); // 仍在 running，带标记

    // 结算：decode 工作被跳过，请求被汇总进 cancelled 并 retain 出队
    let outcome = worker.complete_step(step_id).unwrap();
    assert_eq!(outcome.cancelled, vec![1]);
    assert!(outcome.generated.is_empty()); // 它这一步没产出任何 token
    assert!(outcome.finished.is_empty());

    // 状态机归于空闲
    assert!(worker.request(1).is_none());
    assert!(worker.plan_step().unwrap().is_none());
    assert!(worker.is_idle());
}
```

运行：

```bash
cargo test --release -p pegainfer-sim --lib deferred_cancel -- --nocapture
```

**需要观察的现象**：

1. `cancel` 返回 `Deferred` 后请求快照仍在，但 `cancel_requested == true`；
2. `complete_step` 返回的 outcome 里 `generated` 为空——plan 虽然给请求 1 派了一个 decode 工作，但结算时被标记跳过；
3. `outcome.cancelled == vec![1]` 且之后 `plan_step` 返回 `Ok(None)`。

**预期结果**：测试通过。如果把 `worker.cancel(1)` 挪到 `complete_step` 之后，它将返回 `Cancelled`（in_flight 已空）且 `outcome.cancelled` 为空——「收割窗口」只存在于那一步的结算里。

（本实践为教程设计的本地练习；命令输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：沿着 API 契约推演：`plan_step` 里那两处 `|| request.cancel_requested` 判断，正常使用顺序下能被触发吗？

**答案**：推不出触达路径。`cancel` 只在 `in_flight.is_some()` 时置标记；而带标记的请求一定会在随后那次 `complete_step` 的 `retain` 中被移除；`in_flight.is_none()` 时 `cancel` 走立即移除分支，根本不标记。因此进入 `plan_step`（它要求 in_flight 为空）时 running 里不可能有带标记的请求。这两处判断是防御性守卫：万一未来出现多步流水或新的标记入口，计划器也不会给「死刑犯」派活。

**练习 2**：被跳过的 prefill 工作为什么连 `PrefillProgress` 都不写进 outcome？

**答案**：`StepOutcome` 的语义是「这一步实际发生的计算与状态迁移」。被取消的请求对前端已经不可见（客户端早已离开），任何进度报告都没有消费者；写进去反而会诱导上层（如 `ProfiledRuntime::apply_outcome`）去为它查元数据、记账目，制造无意义的分支。

**练习 3**：如果 `cancel` 发生在 `complete_step` 之后、下一次 `plan_step` 之前，返回什么？请求还会出现在任何 `outcome.cancelled` 里吗？

**答案**：返回 `Cancelled`（in_flight 已空，直接从 running 移除）；不会出现在任何 outcome 里——它不是在某个执行步「中间」被取消的，没有一步需要为它报告取消。这正是 worker 层取消的两种干净形态：要么在某步的 outcome 里被收割，要么在两步之间静默消失。

### 4.3 ProfiledRuntime：cancel_aborted 与 cancel_worker_requests

#### 4.3.1 概念说明

`WorkerState` 是纯逻辑，它不知道「前端想不想取消」，只知道 `cancel(id)` 这个动作。把前端的 abort 标志翻译成 `cancel` 调用的，是 profiled 车道的 `ProfiledRuntime`。它在自己的 step 循环里设置了**三个观测点**：

| 观测点 | 时机 | 请求此刻可能的位置 | 动作 |
| --- | --- | --- | --- |
| `drain_submissions` 开头 | 提交队列刚被倒进来 | worker 还不认识它 | `ledger.retire`，根本不提交 |
| `cancel_aborted` | 每次 step 入口 | waiting / running（可能 in_flight） | `worker.cancel`，按四分支处理 |
| `apply_outcome` | `complete_step` 之后 | 已被 outcome 出队或仍在 running | `cancel_worker_requests` 强制清场 |

三个观测点合起来覆盖了 abort 落地的任意时刻。特别地，`cancel_aborted` 在 in_flight 非空时拿到 `Deferred` 什么都不做——这**不是**偷懒：那个请求会在本步结算时被 `complete_step` 收割、出现在 `outcome.cancelled` 里，随后由 `apply_outcome` 接手清理。也就是说，「标记 → 收割」在 worker 内部闭环，runtime 只在收割之后做元数据与账目的同步清理。

`cancel_worker_requests` 是收尾的「强制清场」：它对每个待取消 id 再调一次 `worker.cancel`，此时 in_flight 必为 None（`complete_step` 已把它 take 走），所以期望的返回只有两种——`Cancelled`（迟到 abort：请求还在 running，现在才被移除）和 `NotFound`（请求刚被这一步的 retain 或 finish 移除）。出现 `Deferred` / `AlreadyRequested` 意味着状态机被破坏，直接 `bail!` 停机。

#### 4.3.2 核心流程

profiled 车道一次 `step` 里的取消相关控制流（承接 u3-l4 将要细讲的定价与睡眠）：

```text
ProfiledRuntime::step
  ├─ drain_submissions     每个排队请求先问 ledger.is_aborted
  │                          是 → ledger.retire(id)，不提交给 worker
  ├─ cancel_aborted        对 runtime 记账的每个 id 问 ledger.is_aborted
  │                          是 → worker.cancel(id)
  │                              Cancelled            → ledger.retire + 删元数据
  │                              Deferred/AlreadyReq  → 等 complete_step 收割（本步或后续步）
  │                              NotFound             → bail!（记账与 worker 失步，是 bug）
  ├─ 若 in_flight 到期 → complete_step → apply_outcome
  │     ├─ cancelled = outcome.cancelled ∪ {本步产 token 且 is_aborted 者}
  │     ├─ cancel_worker_requests(cancelled)：逐个 worker.cancel
  │     │     Cancelled | NotFound → ok（迟到 abort / 已出队）
  │     │     Deferred | AlreadyRequested → bail!（in_flight 已空却删不掉 = 不变量破坏）
  │     ├─ 对 cancelled：ledger.is_active 则 ledger.retire；删 runtime 元数据
  │     └─ 后续 prefill/generated/finished 处理一律跳过 cancelled 者
  └─ 否则 plan_step 新一步（标记者不会被派活）
```

#### 4.3.3 源码精读

step 入口的两拍顺序——先排水、再清 abort：

- [src/lib.rs:L320-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L320-L321) — `step` 的前两行就是 `drain_submissions(...)` 与 `cancel_aborted(ledger)`，保证任何后续规划都建立在「已知 abort 已处理」的基础上。

观测点一：提交前发现 abort，直接销账、不进 worker：

- [src/lib.rs:L379-L383](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L379-L383) — `drain_submissions` 遍历排队请求，`is_aborted` 为真则 `ledger.retire(id); continue;`——这个请求从头到尾不会存在于 worker 状态机里。

观测点二：step 入口对已记账请求的统一 abort 扫描：

- [src/lib.rs:L438-L456](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L438-L456) — `cancel_aborted` 以 `self.requests`（runtime 自己的元数据表）的键为迭代源，对每个 abort 者调 `worker.cancel`：`Cancelled` 分支当场 `ledger.retire` 并删除元数据（L445-L448）；`Deferred | AlreadyRequested` 显式空处理（L449）——交给收割；`NotFound` 直接 `bail!`（L450-L452），因为「runtime 认为自己在服务这个请求、worker 却没有它」只能是记账失步。

观测点三：结算后把取消清单落账：

- [src/lib.rs:L466-L483](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L466-L483) — `apply_outcome` 先取 `outcome.cancelled`（worker 已收割者），再把「本步产出了 token 但 ledger 显示已 abort」的 id 补进去（L472-L476，这就是迟到 abort 的捕获）；然后 `cancel_worker_requests` 强制清场（L477）；最后对每个取消者 `ledger.is_active` 才 `retire`、并删元数据（L478-L483）。

强制清场对四个分支的裁决：

- [src/lib.rs:L545-L559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L545-L559) — `cancel_worker_requests`：注释（L548-L551）点名 `Cancelled` 是迟到 abort 的情形——`complete_step` 已清空 in_flight，请求必须在这里从 running 移除，下面的账目与元数据退休才有意义；`Cancelled | NotFound` 都放行（L552），`Deferred | AlreadyRequested` 一律 `bail!`（L553-L555），因为此处 in_flight 必为 None，删不掉就是不变量被破坏。

结算后的三段处理全部跳过 cancelled：

- [src/lib.rs:L485-L496](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L485-L496) — prefill 收尾的 `echo_prompt` 跳过取消者；
- [src/lib.rs:L497-L526](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L497-L526) — 生成的 token 不给取消者 `push_tokens`（注意 L509-L517 还会把 worker 的 1-based `token_index` 换回 0-based 去翻 `completion_tokens` 反转存储）；
- [src/lib.rs:L527-L536](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L527-L536) — 完成者里若混着取消者，不给它 `ledger.finish`——取消的请求绝不以 `Terminal::Finished` 收场。

参考样板——迟到 abort 的清理与幸存者：

- [src/lib.rs:L975-L1012](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L975-L1012) — `late_abort_cleanup_removes_nonterminal_worker_request`：两个请求各生成 1 个 token 后（L998-L1003），对 `request_id` 调 `cancel_worker_requests`（L1005）——此时 in_flight 已空，走 `Cancelled` 分支立即移除（L1006）；幸存的 `survivor_id` 在后续两步里正常走完并 `finished`（L1008-L1011）。

顺带对照：legacy 车道没有 worker，abort 直接在两个循环里翻译成 `retire`：

- [src/lib.rs:L673-L677](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L673-L677) — 准入循环：排队期发现 abort，`retire` 后不建 `RunningRequest`；
- [src/lib.rs:L703-L707](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L703-L707) — 推进循环：running 请求发现 abort，`retire` 后直接丢弃（不进入 `still_running`）。legacy 车道没有「in_flight 引用」问题——它的请求状态就是 `SimScheduler.running` 本身，没有不可变 plan 挂在上面，所以可以即刻移除。

#### 4.3.4 代码实践

**实践目标**：验证 `cancel_worker_requests` 的 `bail!` 守卫真实有效——在 in_flight 非空时调用它应当报错。

**操作步骤**：

1. 先跑通现有样板：

   ```bash
   cargo test --release -p pegainfer-sim --lib late_abort_cleanup -- --nocapture
   ```

2. 在本地练习分支上复制 [src/lib.rs:L975-L1012](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L975-L1012) 的测试为新名字（如 `cancel_worker_requests_bails_while_in_flight`），把这一行：

   ```rust
   let step_id = runtime.worker.plan_step().unwrap().unwrap().id();
   ```

   之后的第一次 `complete_step`（L999）**删掉**，让 `cancel_worker_requests(&[request_id])`（原 L1005）紧跟着 `plan_step` 执行，并把 `.unwrap()` 换成：

   ```rust
   let err = runtime.cancel_worker_requests(&[request_id]).unwrap_err();
   assert!(err.to_string().contains("remained in flight"));
   ```

3. 运行：

   ```bash
   cargo test --release -p pegainfer-sim --lib cancel_worker_requests_bails -- --nocapture
   ```

**需要观察的现象**：此时 in_flight 非空、请求在 running 未标记，`worker.cancel` 返回 `Deferred`，命中 `bail!` 分支；错误信息正是 [src/lib.rs:L554](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L554) 的文案。

**预期结果**：新测试通过，证明该守卫不是摆设；恢复原测试后 `late_abort_cleanup...` 依旧通过。

（本实践为教程设计的本地练习；命令输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`cancel_aborted` 为什么以 `self.requests`（runtime 的元数据表）为迭代源，而不是让 worker 报告「我这里有哪些请求」？

**答案**：`self.requests` 的键集与 worker 的 waiting+running 应当一一对应（提交 Queued 时同时写入两边，finished/cancelled 时同时删两边）。以它为源，`NotFound` 分支就成了天然的失步探测器：runtime 认为在服务、worker 却没有，只能是清理逻辑漏了某条路径，`bail!` 把 bug 暴露在第一时间而不是让它静默积累。

**练习 2**：`cancel_worker_requests` 里 `Cancelled` 与 `NotFound` 分别对应什么时序？

**答案**：`Cancelled` 对应迟到 abort——abort 标志在 `complete_step` 之后才被 `apply_outcome` 观测到（L472-L476 的补录路径），请求还在 running，此刻 in_flight 已空，于是被立即移除；`NotFound` 对应「已经不在了」——要么刚被这一步 `complete_step` 的 `retain` 作为 cancelled 出队（id 来自 `outcome.cancelled`），要么作为 finished 出队（abort 与完成同一步发生）。

**练习 3**：同样是拿到 `Deferred`，`cancel_aborted` 选择忽略，`cancel_worker_requests` 却要 `bail!`——为什么？

**答案**：时机不同，不变量不同。`cancel_aborted` 运行在 step 入口，此刻可能正当某一步在飞（上一步 plan 了、还没到 `ready_at`），`Deferred` 是完全预期的结果，收割会自然发生。`cancel_worker_requests` 运行在 `complete_step` 之后，in_flight 必为 None，此时 `cancel` 只可能返回 `Cancelled`/`NotFound`；出现 `Deferred` 说明有请求在无 in_flight 的情况下带标记滞留 running——状态机被破坏，继续运行只会污染后续所有步骤，所以宁可停机。

### 4.4 ledger.retire 与 finish：对前端可见性的差别

#### 4.4.1 概念说明

worker 层的取消解决「调度状态机里怎么删」，最后一公里是「账本上怎么销」。`RequestLedger` 有两种关闭账户的方式，可见性截然不同：

- **`finish(id, reason)`**：正常终态。把 `Terminal::Finished { reason, prompt_tokens, completion_tokens, ... }` 写进该请求的 statement，随后随 step 流交付给前端，前端再发给客户端；
- **`retire(id)`**：abort 专用。**静默**关闭账户，并把 statement 整个抽走丢弃——不发终态、不发残留 token、什么都不发。

为什么 `retire` 可以什么都不发？因为它的前提是 `is_aborted` 为真：**前端已经为这个请求拆掉了全部状态（客户端早走了），账面上的任何输出都已经没有收件人**。此时再写一个 `Terminal::Cancelled` 之类的终态反而是 bug 的温床——没人消费它，还占着 step 流的带宽。

于是本讲的完整链条是：前端 `abort()`（原子标记）→ runtime 在观测点发现 → `worker.cancel`（四分支）→ 取消者绝不 `finish` → `ledger.retire`（静默销账）。

#### 4.4.2 核心流程

```text
客户端离开 ──► RequestControl::abort()          AtomicBool: false ──Release──► true
                                │
              （调度器下一次 step 才能观测到）
                                ▼
     ledger.is_aborted(id) ──Acquire──► true
                                │
              ┌─────────────────┴──────────────────┐
              ▼                                    ▼
     尚无 worker 身份                      已在 worker 中
     drain_submissions / step_legacy        cancel_aborted / apply_outcome
              │                                    │
              │                         worker.cancel → Cancelled/Deferred
              │                                    │ complete_step 收割
              ▼                                    ▼
        ledger.retire(id)  ◄──────────────── ledger.retire(id)
        （close 账户 + statement.extract，        （绝不调用 finish：
          无 Terminal、无 token、静默）             cancelled ≠ Finished）
```

#### 4.4.3 源码精读

前端侧的标记与账本的读取：

- [pegainfer-frontend/src/engine/request_lifecycle.rs:L192-L198](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/request_lifecycle.rs#L192-L198) — `abort` 以 `Release` 序写 `AtomicBool`，注释点明 Release 序是为了让自己的写排在前端拆除各请求状态之后，与调度器侧的 `Acquire` 读配对；
- [pegainfer-frontend/src/engine/ledger.rs:L133-L138](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L133-L138) — `is_aborted` 以 `Acquire` 序读同一标志；文档注释直说了契约：「前端不再想要这个请求，调度器应在下一次触碰时以 `retire` 回应；abort 可能落在任何时刻，所以每个 finish 路径上都要探一遍」；
- [pegainfer-frontend/src/engine/ledger.rs:L142-L151](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L142-L151) — `is_active`：账户存在且处于 `Active` 状态。它用 `get` 而非 `unwrap`，对「账户可能已在本步早些时候被回答」的防御路径是安全的——这正是 `apply_outcome` 在 `retire` 前先用 `is_active` 探一遍的原因（账户可能已被 `cancel_aborted` 在同一步更早处关闭）。

两条销账路径的对照：

- [pegainfer-frontend/src/engine/ledger.rs:L241-L263](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L241-L263) — `finish` 关闭账户并把 `Terminal::Finished`（含 `reason`、双侧 token 计数）写入 statement——这是一条**会送达客户端**的终态；
- [pegainfer-frontend/src/engine/ledger.rs:L276-L286](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L276-L286) — `retire` 同样关闭账户，但接下来是 `statement.extract(id)`：把该请求缓冲的所有内容（已产出的 token、元数据）整体抽出丢弃，只留一条 `log::debug!`。文档注释写得很直白：「静默，丢弃本步为它缓冲的一切：前端已经为这个 id 删掉了自己的状态，没有人可以投递」。

回看 sim 侧的两处配合，体会「retire 前先探」的模式：

- [src/lib.rs:L478-L483](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L478-L483) — `apply_outcome` 对每个取消者：`if ledger.is_active(request_id) { ledger.retire(request_id); }`。取消清单里可能混着已被同一步早些时候（`cancel_aborted` 的 `Cancelled` 分支）retire 过的 id，而 `retire` 内部的 `close` 在账户不存在时会 panic——先 `is_active` 再 `retire` 是必需的防重入守卫；
- [src/lib.rs:L527-L536](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L527-L536) — finished 循环开头的 `if cancelled.contains(&request_id) { continue; }` 是同一枚硬币的另一面：**取消的请求绝不 finish**。若没有这行，一个「本步既生成完最后一个 token、又被 abort」的请求会以 `Terminal::Finished` 收场，把一条没人消费的终态塞进 step 流。

#### 4.4.4 代码实践

**实践目标**：用前端的账本单测直接观察 `retire` 与 `finish` 对 statement 的不同处置。

**操作步骤**：

1. 阅读并运行前端已有的账本测试：

   ```bash
   cargo test --release -p pegainfer-frontend --lib retire_discards_buffered_output -- --nocapture
   ```

   该测试位于 [pegainfer-frontend/src/engine/ledger.rs:L487](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L487)，锁定的行为是：`retire` 会把缓冲中的输出整体丢弃。

2. 做一次源码对照阅读：把 [ledger.rs:L241-L263](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L241-L263)（`finish` 写 `Terminal::Finished`）与 [ledger.rs:L276-L286](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L276-L286)（`retire` 用 `statement.extract` 丢弃）并排打开，回答下方练习 1。

3. 回到 sim 侧做一次全链路追踪：从 [src/lib.rs:L320-L321](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L320-L321) 出发，沿本讲 4.3.2 的流程图标出一次 abort 分别在「提交前 / 步入口 / 结算后」落地时，`retire` 各自由哪一行调用。

**需要观察的现象**：

1. `retire_discards_buffered_output` 通过——被 retire 的请求即使账上已有 token，statement 也被抽空；
2. 三条路径最终都汇聚到同一个 `ledger.retire`，且没有任何一条路径对取消者调用 `finish`。

**预期结果**：能够不看讲义说出：「finish 写终态并交付，retire 抽走 statement 并静默」，以及「为什么 retire 前要 is_active 探一遍」。（测试命令输出待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`finish` 与 `retire` 对 statement 分别做什么操作？这个差别决定了什么？

**答案**：`finish` 把 `Terminal::Finished { reason, prompt_tokens, completion_tokens, .. }` 写进 statement（随 step 流交付客户端）；`retire` 用 `statement.extract(id)` 把该请求的 statement 连同缓冲 token 整体抽走丢弃。差别决定的是**前端可见性**：finish 的请求客户端会收到最终响应，retire 的请求客户端什么都收不到——因为它早已离开，retire 的职责只是不让账目悬挂。

**练习 2**：`apply_outcome` 里 `retire` 之前为什么要先 `is_active`？直接 `retire` 会怎样？

**答案**：取消清单可能包含在同一步更早处已被 `cancel_aborted` retire 掉的 id；`retire` 内部 `close(id)` 在账户不存在时会 panic（「no open account」）。`is_active` 用 `accounts.get` 探测、不存在返回 false，是这条路径上唯一不会 panic 的探测方式。

**练习 3**：前端是怎么「告诉」调度器要取消的？为什么说这种机制决定了取消的发现时机由调度器决定？

**答案**：`RequestControl::abort()` 以 `Release` 序把共享 `AtomicBool` 置真，`ledger.is_aborted` 以 `Acquire` 序读。它是纯标记、无唤醒：abort 可以在任意两条指令之间落地，但只有调度器循环运行到观测点（drain_submissions / cancel_aborted / apply_outcome，legacy 车道则是准入与推进两个循环）时才会被发现——这正是本讲把观测点逐一列出的原因。

## 5. 综合实践

**任务**：写一个「切块 prefill 中途取消 + 幸存者照常完成」的序列测试，把本讲四个模块串起来：四分支的位置判断（4.1）、标记与收割（4.2）、以及「取消不影响他人」的调度不变量（为 4.3 的 runtime 层行为做铺垫）。

在 `src/worker.rs` 的 `mod tests` 中添加：

```rust
#[test]
fn mid_prefill_deferred_cancel_harvests_victim_and_spares_survivor() {
    let mut worker = WorkerState::new(scheduler(
        2,
        8,
        64,
        PrefillPolicy::Chunked { max_chunk_tokens: 2 },
    ))
    .unwrap();
    worker.submit(request(1, 4, 1)).unwrap(); // 长 prompt：prefill 会被切成两块
    worker.submit(request(2, 0, 2)).unwrap(); // 空 prompt：直通 Decode

    // 第 0 步：请求 1 准入并完成第一块 prefill（2/4），请求 2 准入为 Decode
    let id0 = worker.plan_step().unwrap().unwrap().id();
    let out0 = worker.complete_step(id0).unwrap();
    assert_eq!(out0.prefill.len(), 1);
    assert_eq!(out0.generated, vec![GeneratedToken { request_id: 2, token_index: 1 }]);
    assert!(worker.request(1).unwrap().phase == RequestPhase::Prefill);

    // 第 1 步在飞：计划给请求 1 派第二块 prefill、给请求 2 派第二个 decode
    let id1 = worker.plan_step().unwrap().unwrap().id();
    // 此刻取消请求 1：它在 running 且被 in_flight 的 plan 引用 → 只能标记
    assert_eq!(worker.cancel(1), CancelResult::Deferred);
    assert!(worker.request(1).unwrap().cancel_requested);

    // 结算：请求 1 的 prefill 工作被跳过（连进度都不记），请求 2 照常出 token 并完成
    let out1 = worker.complete_step(id1).unwrap();
    assert!(out1.prefill.is_empty());           // 受害者的工作消失了
    assert_eq!(out1.generated, vec![GeneratedToken { request_id: 2, token_index: 2 }]);
    assert_eq!(out1.finished, vec![2]);          // 幸存者完成
    assert_eq!(out1.cancelled, vec![1]);         // 受害者被收割
    assert!(worker.request(1).is_none());
    assert!(worker.request(2).is_none());
    assert!(worker.plan_step().unwrap().is_none()); // 状态机干净地归于空闲
    assert!(worker.is_idle());
}
```

运行：

```bash
cargo test --release -p pegainfer-sim --lib mid_prefill_deferred_cancel -- --nocapture
```

**验证要点**：

1. 第 0 步后请求 1 处于 `Prefill` 阶段、还剩 2 个 token（`remaining_prefill_tokens == 2`，可用快照断言）；
2. 第 1 步的 plan 确实给请求 1 派了 `PrefillWork { request_id: 1, tokens: 2 }`（可在 `plan_step` 之后、`cancel` 之前用 `worker.in_flight().unwrap().prefill()` 观察），但结算后这些工作凭空消失——被跳过的工作不留痕迹；
3. 幸存者请求 2 的两个 token 与 `finished` 完全不受受害者影响：取消是**请求粒度**的，不会拖累同一步里的其他请求；
4. 结束后 `is_idle()` 为真——没有泄漏的 running 请求，也没有滞留的 waiting 请求。

做完后，对照 [src/lib.rs:L975-L1012](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/src/lib.rs#L975-L1012) 的 `late_abort_cleanup_removes_nonterminal_worker_request`：你刚在 worker 层手推的时序，正是那个测试里 `cancel_worker_requests` 在 runtime 层自动完成的事情。

（本实践为教程设计的本地练习，测试代码非仓库原有；命令输出待本地验证。）

## 6. 本讲小结

- `CancelResult` 四分支就是「请求此刻在哪」的四态回答：waiting 立删得 `Cancelled`、无处可寻得 `NotFound`、running 且有步在飞只能标记得 `Deferred`、重复取消得 `AlreadyRequested`（幂等）。
- `Deferred` 的根源是 plan 一致性：`StepPlan` 按 id 引用 running 中的请求，`complete_step` 要求每个被规划者仍在 running；施工期间抽走请求会让整步结算报错，所以只能「标记 → 等结算」。
- 收割在 `complete_step` 尾部一次完成：已标记者的 prefill/decode 工作被静默跳过（不留进度、不产 token），随后统一收进 `outcome.cancelled`，与 `finished` 一起被 `retain` 物理出队——终态的实现方式依旧是「从 running 里消失」。
- `ProfiledRuntime` 在三个观测点把前端的原子 abort 翻译成 `worker.cancel`：提交前（不进 worker）、步入口（`cancel_aborted`，`NotFound` 即 bail）、结算后（`apply_outcome` 补录迟到 abort 并 `cancel_worker_requests` 强制清场，此时出现 `Deferred` 即不变量破坏）。
- 可见性的最后一环在账本：`finish` 写 `Terminal::Finished` 并交付客户端；`retire` 静默关户并 `statement.extract` 丢弃一切——取消的请求绝不 finish，abort 的请求没有收件人。
- 两条车道殊途同归：legacy 车道没有 worker 与不可变 plan，abort 在准入/推进两个循环里即刻 `retire`；profiled 车道多绕「标记 → 收割」一圈，但落点同样是 `retire`。

## 7. 下一步学习建议

本讲讲完了「请求怎么被撤掉」，u2 单元的调度侧到此收口。接下来：

1. **u3-l1（EngineProfile：校准档案的加载、清单与摘要校验）**——profiled 车道的定时真相来自 JSON 档案，从 `load_from_path` 的 sha256 清单校验读起，为单元三铺路；
2. **u3-l4（ProfiledRuntime：档案驱动的在线引擎车道）**——把本讲的 `step` 循环补完整：`estimate_step` 如何用 `StepShape` 给在飞的步骤定价、`ready_at` 与 `WAIT_SLICE` 切片睡眠如何决定 `cancel_aborted` 的实际观测时机；
3. **延伸阅读（前端侧）**：[pegainfer-frontend/src/engine/ledger.rs:L287-L310](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/ledger.rs#L287-L310) 的 `defer_finish`——另一种「终态不立即发出」的机制（P/D 解耦），与本讲的 `retire` 对照着读，能更清楚「谁还有资格收到终态」这条契约的边界；
4. 若想看取消在 HTTP 层的表现，可预习 u4-l2：`aborted_request_still_ships_its_prefix_lookup`（[tests/frontend_e2e.rs:L724-L757](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-sim/tests/frontend_e2e.rs#L724-L757)）展示了一个被放弃的请求如何仍然把它的前缀缓存查询贡献进指标。
