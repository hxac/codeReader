# RequestLedger：活请求的账本与 terminal 恰一次

## 1. 本讲目标

[u3-l3](u3-l3-step-wire.md) 讲清了 step 契约的**线类型**：一条扁平 `RequestUpdate` 如何把「步内顺序」变成结构。但那句结论的另一半当时被推迟了——「**跨步保证由账本承接：账户在 terminal 时关闭，对已关账户的写入会 panic**」。本讲就把这个「账本」（[ledger.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs)）完整拆开。读完本讲你应当能够：

1. **走查账户生命周期**：`register` 开户（`Queued`）→ `admit` 转 `Active` → `push_tokens` 计数 → `finish`/`fail`/`reject`/`retire`/`defer_finish` 五种关户，外加账本 `Drop` 时的销账扫尾。
2. **解释 terminal-exactly-once 的强制方式**：为什么「触碰已关闭或未知账户」不是返回 `Err` 而是**直接 panic 在出错的调度器调用点**，以及这层保证为什么在 release 构建下依然生效。
3. **区分 `pub` 与 `pub(crate)` 的职责划分**：`register`、`commit_step`、`fail_all` 为什么只有 driver 能碰——**节奏（cadence）由 driver 拥有**，模型 crate 只能在自己的 `step` 里记账。

本讲承接 [u3-l3](u3-l3-step-wire.md)（线类型）与 [u3-l1](u3-l1-contract-overview.md)（两代契约地图），是 [u3-l5](u3-l5-scheduler-driver.md)（Scheduler 与 drive 循环）的直接前置。

## 2. 前置知识

### 2.1 为什么有了线类型还需要一本「账」

u3-l3 的结论是：`RequestUpdate` 的扁平结构让「terminal 之后再出 token」在**一步之内**不可表达。但契约还有一条跨步规则：「terminal 是请求生命周期的终点，**之后的步不再提及此 id**」。类型管不了跨步——要判断「这一步还能不能提这个请求」，必须**记得**哪些请求还活着。记忆就是状态，状态就是账本。

`RequestLedger` 的记账对象是「**未答复的请求**」（unanswered requests）：每个这样的请求在账本里有一个**账户**（`Account`），账户里存的不是请求负载，而是「引擎还欠客户端什么」——abort 标志的引用、prompt 长度、排队时间、已发 token 的流水。请求一旦拿到终态（`Finished`/`Rejected`/`Failed`），账户**关户**；账本里剩下的账户集合，恰好就是「还欠着的请求」全集。

### 2.2 「应答到底」：三层 drop 保护

PegaInfer 前端契约的一条硬哲学：**每个请求必须以一个 terminal 收场，绝不允许客户端挂死**。一个请求从 HTTP 进入到拿到终态，会经历三个「无人看管」的窗口，每个窗口各有一层保护：

| 窗口 | 危险 | 保护机制 | 所在 |
|---|---|---|---|
| 提交后、开户前（躺在提交通道里，或调度器线程已死） | 没人知道这个请求 | `RequestEnvelope` 的 **drop 炸弹**：未消费就落盘成 `Failed` | [request_lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs) |
| 开户后、终态前（引擎出错/panic/拆除） | 调度器可能跟丢请求 | 账本账户 + `RequestLedger` 的 **`Drop` 销账扫尾** | ledger.rs |
| 关户后、迟投递前（P/D 场景 `Finished` 被扣住） | 扣着终态的句柄被弄丢 | `DeferredFinish` 的 **drop 炸弹**：改写成 `Failed` 再发 | request_lifecycle.rs |

本讲主线是中间一层；首尾两层在 4.2.3 的「握手」小节点到，完整拆解留给 [u5-l1](u5-l1-kv-and-lifecycle.md)。

### 2.3 panic 作为契约执行手段

Rust 里表达「调用方做错了」有两档：`Result::Err`（可恢复，调用方分支处理）与 panic（不可恢复，线程展开）。账本对**调度器（模型 crate）的记账错误**一律选 panic，理由有三：

- 这类错误不是运行时偶发状况，而是**生产者违反契约的 bug**——对已关账户 `finish`、未 `admit` 就 `push_tokens`，任何一条都意味着调度器逻辑错了，继续跑只会错得更远；
- panic 带**调用点栈回溯**，「panic at the offending scheduler call site」（模块文档原话）——bug 直接定位在模型 crate 的那一行，而不是被一层 `Err` 吞掉后变成含糊的日志；
- 账本用的是 `panic!`/`assert!`/`unwrap`，**不是** `debug_assert!`——后者在 release 构建里是空操作，前者不是。所以这层保证在生产二进制里同样生效。

作为对照，`Scheduler::step` 返回的 `anyhow::Result` 留给**引擎自身的致命错误**（GPU 坏了、CUDA 出错）——那是另一档语义，u3-l5 详讲。

### 2.4 需要的 Rust 背景

- **`HashMap::remove(&k) -> Option<V>`**：删除并**移出**值。「账户已关」在账本里不是一个布尔标志或一个 `Closed` 状态，而是**值从 map 里消失了**——此后任何触碰都走同一条「键不存在」的 panic 路径。
- **`Arc<AtomicBool>` + `Acquire`/`Release`**：abort 标志由前端 `RequestControl::abort` 以 `Release` 序写入，账本 `is_aborted` 以 `Acquire` 序读取——写方的清理先于标志可见，读方看到标志后能信任「前端状态已拆完」。
- **`pub(crate)`**：对 crate 外不可见。外部（包括模型 crate 与你的草稿工程）能调 `ledger.finish(...)`，但**够不着** `ledger.register(...)`——这直接决定了本讲实践必须写进 pegainfer-frontend crate 内部。
- **`Vec<Option<T>>` 的「洞」**：把元素 `take` 走后留下 `None` 槽位而不移动其他元素——`StepStatement` 用它做合并缓冲（4.3）。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `pegainfer-frontend/src/engine/ledger.rs` | **主角**：账本本体 | `Account`/`AccountState`、`StepStatement`、`RequestLedger` 全部记账方法、`Drop` 销账、8 个单元测试 |
| `pegainfer-frontend/src/engine/request_lifecycle.rs` | **主角**：账本触不到的三件套 | `RequestEnvelope`（开户前的 drop 炸弹）、`DeferredFinish`（关户后的迟投递）、`RequestControl`（abort 标志） |
| `pegainfer-frontend/src/engine/driver.rs` | **主角**：节奏所有者 | `drive` 循环里 `register`/`commit_step`/`fail_all` 的唯一合法调用点；`Scheduler` trait 对账本的使用纪律 |
| `pegainfer-frontend/src/engine/step.rs` | 辅：线类型 | `RequestUpdate` 的区段语义、`empty`/`is_vacant` 两个 `pub(crate)` 帮手（u3-l3 已精读） |
| `pegainfer-frontend/src/engine/wiring.rs` | 辅：接线 | `scheduler_pair` 铸出账本与步流；`SchedulerBackend` 字段均为 `pub(crate)` |

## 4. 核心概念与源码讲解

### 4.1 Account 与 AccountState：一个请求在引擎里的户口

#### 4.1.1 概念说明

「账户」是账本的最小单位。设计上最反直觉的一点是：**账户里没有请求负载**。`prompt_tokens`、采样参数、停止策略这些在 `submit` 时已经**所有权转移**给了调度器（`Scheduler::submit` 只做转移，见 [driver.rs:L20-L27](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L20-L27)）；账户保存的是引擎**单方面欠客户端的东西**：

- **abort 标志的引用**（`Arc<AtomicBool>`）——与前端手里的 `RequestControl` 共享同一个原子位，前端 abort、账本可读；
- **prompt 长度**——终态（`Rejected`/`Failed`）要上报给前端，负载却不在手上，所以开户时抄一份长度；
- **排队时间戳** `queued_at`——`admit` 时要盖进 `ScheduledInfo`（排队多久才被接纳）；
- **状态与流水**——`Queued`（已提交未接纳）或 `Active { completion_tokens }`（已接纳，token 逐次累计）。

一个账户从开户到关户，状态只走一条单向路：`Queued → Active → 关户`（或 `Queued → 关户`）。没有回退、没有重开。

#### 4.1.2 核心流程

```
        register(envelope)                    ← driver 专属（pub(crate)）
              │  开户：Account { abort, prompt_len, queued_at, state: Queued }
              │  （同时解除信封的 drop 炸弹：应答责任从信封转移到账户）
              ▼
        ┌── Queued ─────────────────────────────────────┐
        │      │                │                       │
        │ admit│（转活跃）       │ reject（拒绝关户）      │ fail（两态皆可）
        ▼      │                ▼                       ▼
      Active … │          Terminal::Rejected       Terminal::Failed
   { completion_
     tokens }  │ push_tokens / set_cached_tokens /
               │ echo_prompt / kv_transfer        ← 不关户，只记本步账
               │
               ├── finish(_with_cause)  ──► Terminal::Finished（计数取自流水）
               ├── fail                 ──► Terminal::Failed
               ├── defer_finish         ──► 关户 + 记录折进 DeferredFinish（迟投递）
               └── retire（abort 之后）  ──► 静默关户，丢弃本步缓冲

        账本 Drop 时仍开着的账户（无论 Queued/Active）
              └──► Terminal::Failed（销账扫尾，按状态选文案）
```

**关户的五扇门**各有前置状态检查，违规即 panic。一张速查表（全部可在 [ledger.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs) 里对上号）：

| 方法 | 要求的前置状态 | 违规 panic 消息 |
|---|---|---|
| `admit` | `Queued`（账户存在） | `admit on already-admitted request {id}` |
| `reject` | `Queued` | `reject after admission for {id}: an admitted request finishes or fails` |
| `push_tokens` / `set_cached_tokens` / `echo_prompt` / `kv_transfer` | `Active` | `... on {id} before admission` |
| `finish` / `finish_with_cause` / `defer_finish` | `Active` | `finish on {id} before admission` |
| `fail` | **任意**（两态皆可） | —— |
| 任何方法 | 账户**存在且未关** | `no open account for {id}: already answered, or never registered` |

#### 4.1.3 源码精读

**`Account`：欠条，不是负载**

[pegainfer-frontend/src/engine/ledger.rs:L43-L51](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L43-L51) 定义账户本体，四个字段即上文四样「欠款」。结构体文档一句话点题：「**负载不在这里——它在 `submit` 时去了调度器；账户是引擎还欠客户端的东西**」。

[pegainfer-frontend/src/engine/ledger.rs:L53-L58](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L53-L58) 是两态的 `AccountState`。注意 `Active` 变体**内联携带流水** `completion_tokens: usize`——账本的计数不在别处，就在状态里，随状态的每一次合法转移而更新。[L60-L67](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L60-L67) 的 `completion_tokens()` 查询把 `Queued` 折算成 0，让 `fail` 一类两态通用的方法不必分支。

**「已关」= 不存在**

[pegainfer-frontend/src/engine/ledger.rs:L119-L129](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L119-L129) 是两个私有底座：`account(id)` 读账户、`close(id)` 关账户（`HashMap::remove` 移出）。两者共用同一条 panic：

```rust
panic!("no open account for {id}: already answered, or never registered")
```

这是 terminal-exactly-once 的**机制核心**。所有终态方法都从 `close(id)` 起步（[finish_with_cause](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L246-L263) 第一行就是 `let account = self.close(id);`）。第一次 `finish` 把账户从 map 里**移走**；第二次 `finish` 在 `close` 里找不到键，panic 当场引爆。「已关」不是账户上的一个标志位，而是**值的不存在**——不存在就无法被误碰，也不需要在每个方法里写「如果是 Closed 就拒绝」的分支。

这个保证由测试逐字锁死：[ledger.rs:L596-L606](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L596-L606) 的 `touching_a_closed_account_panics` 用 `#[should_panic(expected = "no open account")]` 断言了「finish 之后再 push_tokens 必然引爆」。

**为什么 `fail` 独享两态通行权**

[ledger.rs:L265-L274](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L265-L274) 的文档写明理由：「**排队中的请求也可能在接纳前死于引擎错误**」。想象 prefill 步整批失败——排队队列里还没轮到的请求同样没救了，必须能直接 `fail` 关户。与之相对，`reject` 是**接纳时刻的裁决**（拒绝一个已接纳的请求语义不通），`finish` 是**已接纳请求的完结**（没接纳过就无所谓完结），所以各自锁死单一前置状态。

#### 4.1.4 代码实践

**实践目标**：用两个新测试把「状态机的前置检查」从表格变成亲手验证；顺带跑通账本既有测试套件，为 4.2 的实践铺路。

**操作步骤**：

1. 先跑既有套件（在**仓库根目录**，纯 CPU，无 GPU 依赖）：

```bash
cargo test -p pegainfer-frontend --lib engine::ledger
```

应看到 8 个测试通过：`admission_tokens_and_finish_fold_into_one_entry`、`reject_carries_prompt_len_and_no_tokens`、`retire_discards_buffered_output`、`defer_finish_folds_step_output_and_delivers_late`、`dropped_ledger_writes_off_open_accounts`、`abort_flag_is_visible_through_both_states`、`submit_to_a_dead_scheduler_fails_the_request`、`touching_a_closed_account_panics`。把它们逐个对到 4.1.2 的状态机图上。（**待本地验证**：命令形式标准，当前环境是否可用 cargo 请自行确认。）

2. 在**你自己的工作副本**里（不要提交），往 [ledger.rs 的 `#[cfg(test)] mod tests`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L393-L611) 末尾追加两个测试（**示例代码**，非项目原有代码；直接复用模块里已有的 `request()` 与 `scheduler_pair()` 帮手，无需新 import）：

```rust
#[test]
fn fail_from_the_queued_state_closes_the_account() {
    let (handle, mut backend) = scheduler_pair();
    let _control = handle.submit(request(vec![1, 2]));
    let envelope = backend.submissions.try_recv().expect("envelope");
    let id = backend.ledger.register(envelope).id;
    // 未 admit，直接 fail：Queued 态的合法关户
    backend.ledger.fail(id, "prefill batch failed");
    backend.ledger.commit_step(false);

    let step = handle_steps(handle).try_recv().expect("step");
    assert!(matches!(
        step.updates[0].terminal,
        Some(Terminal::Failed { ref message, completion_tokens: 0, prompt_tokens: 2, .. })
            if message == "prefill batch failed"
    ));
}

#[test]
#[should_panic(expected = "finish on")]
fn finish_before_admission_panics() {
    let (handle, mut backend) = scheduler_pair();
    let _control = handle.submit(request(vec![1]));
    let envelope = backend.submissions.try_recv().expect("envelope");
    let id = backend.ledger.register(envelope).id;
    backend.ledger.finish(id, FinishReason::Stop); // Queued 态直接 finish
}
```

3. `cargo test -p pegainfer-frontend --lib engine::ledger` 重跑。

**需要观察的现象**：第一个测试绿灯——`fail` 不经 `admit` 也能关户，且 `completion_tokens` 为 0；第二个测试「绿着失败」——`#[should_panic]]` 捕获的 panic 消息以 `finish on` 开头（完整消息是 `finish on req-0 before admission`，出自 [finish_with_cause 的 let-else](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L253-L256)）。

**预期结果**：10 个测试全部通过。注意这两个测试**必须**写进 crate 内部——`register`/`commit_step` 与 `backend.ledger`/`backend.submissions` 都是 `pub(crate)`，外部草稿 crate 编译不过（这正是 4.2 要讲的节奏所有权）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Account` 里不保存 `Request` 负载（比如存个 `Arc<Request>` 备用）？

**答案**：负载在 `submit` 时已**所有权转移**给调度器，这是 `Scheduler::submit`「只做转移、不写账」纪律的另一面。账本若留一份副本，就有两个持有者、两个可能的真相源。账户只存引擎单方面欠客户端的事实（abort 引用、prompt 长度、排队时间、流水）——需要负载的是调度器，需要「欠条」的是账本，二者不重叠。

**练习 2**：「账户已关」为什么用从 `HashMap` 移除来表示，而不是给 `AccountState` 加一个 `Closed` 变体？

**答案**：三个理由。① **不可误碰**：值不存在后，任何方法（包括未来新增的方法）都走同一条「键缺失」panic 路径；`Closed` 变体则要求每个方法都记得匹配它，漏一处就是静默错账。② **map 即欠款全集**：`fail_all` 与 `Drop` 扫尾直接迭代 map 就是「全部未答复请求」，无需过滤。③ **省一条不可能的状态**：`Closed` 账户再无任何合法操作，让它作为类型存在没有收益。

**练习 3**：`admit` 之后再 `admit` 同一个 id 会怎样？错误消息是什么？

**答案**：panic，消息 `admit on already-admitted request {id}`（[ledger.rs:L165-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L165-L168) 的 `assert!`）。注意它不是 `no open account`——账户还开着，只是状态转移非法：`admit` 是 `Queued → Active` 的单向边，走第二次就是调度器 bug。

---

### 4.2 RequestLedger 记账方法：查询、流式记账与五种关户

#### 4.2.1 概念说明

`RequestLedger` 把账户集合、本步账单缓冲（`StepStatement`，4.3 详讲）和步流发送端装在一个结构里。它向**两个方向**各暴露一副面孔：

- **对调度器（模型 crate）**：`pub` 的记账方法——查询（`is_aborted`/`is_active`/`completion_tokens`）、接纳裁决（`admit`/`reject`）、流式记账（`push_tokens`/`set_cached_tokens`/`echo_prompt`/`kv_transfer`）、关户（`finish`/`fail`/`retire`/`defer_finish`）。调度器在 `step` 里**只能**通过这些方法对外发声——「单一发射点」是契约的明确措辞。
- **对 driver**：`pub(crate)` 的节奏方法——`register`（何时开户）、`commit_step`（何时把账单发上线）、`fail_all`（致命错误时统一销账）。模型 crate 在类型上够不着它们。

这套划分回答的问题只有一个：**谁拥有节奏**。账单何时上线不是调度器的自由——driver 保证「每迭代恰好一次 commit」，这个节拍是 `StepOutputs` 消息粒度的全部来源。

#### 4.2.2 核心流程

一个请求穿过账本的完整记账链（对照 [driver.rs 的 `drive` 循环](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L59-L105)读）：

```
前端 submit ──► crossbeam 通道 ──► drive 排空：
    ledger.register(envelope)          开户 + 解除信封炸弹 + 交出 QueuedRequest
    scheduler.submit(queued)           负载所有权转移给调度器（不记账）
    scheduler.step(&mut ledger):       调度器在此写全部账：
        is_aborted?  ── 是 ─► retire（静默关户）
        admit / reject                  接纳裁决
        push_tokens ×n                  流水 += n，token 进本步账单
        set_cached_tokens / echo_prompt / kv_transfer
        finish / fail / defer_finish    关户（终态进本步账单）
    metrics.publish(snapshot)           先发布 metrics，记住是否变化
    ledger.commit_step(metrics_changed) 一迭代一账单：发 StepOutputs
    step 返回 Err？ ── 是 ─► fail_all(真实错误) + commit_step + 退出
```

**计数的单向流**是本模块最重要的一条不变量：token 数只在 `push_tokens` 里累计、只在终态里上报——

\[ \text{completion\_tokens}(r) \;=\; \sum_{\text{step } s} \bigl|\,\text{tokens}_s(r)\,\bigr| \]

模型侧算错 token 数骗不了账本：终态里的计数是账本自己的流水（模块文档原话：「Token counts on terminals derive from the ledger's own tally, never from model-side arithmetic」）。

#### 4.2.3 源码精读

**账本本体与三个查询**

[ledger.rs:L104-L117](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L104-L117)：`RequestLedger { accounts, statement, tx }`——账户表、本步账单缓冲、步流发送端（`StepSender`，即 [request_lifecycle.rs:L35-L38](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L35-L38) 定义的 tokio 无界 mpsc sender）。

三个查询在 [L131-L156](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L131-L156)：

- `is_aborted`（[L133-L138](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L133-L138)）：`Acquire` 序加载共享原子位。文档给出使用纪律：「abort 随时可能落下，所以**每条 finish 路径都要探测**」——前端已经拆掉这个 id 的状态，迟到的 `Finished` 无人认领；正确动作是 `retire`。[driver.rs 测试里的 EchoScheduler](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L137-L142) 就是范本：每个 running 请求先问 `is_aborted`，是则 `retire` 并跳过。
- `is_active`（[L140-L151](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L140-L151)）：账户存在**且**已接纳。给「同一步里可能已被答复」的防御路径用（批失败、重复效果）——注意它对已关账户返回 `false` 而**不 panic**，是查询里唯一宽容的一个。
- `completion_tokens`（[L153-L156](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L153-L156)）：读流水。EchoScheduler 甚至拿它当「下一个 token 该吐什么」的依据（[driver.rs:L143-L145](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L143-L145)）——调度器可以不自己计数，完全信任账本。

**接纳裁决：`admit` 与 `reject`**

[ledger.rs:L160-L178](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L160-L178) 的 `admit` 是**唯一**盖 `scheduled_at` 戳的地方（[step.rs:L158-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L158-L168) 的 `ScheduledInfo` 文档：「admission facts stamped by the contract layer, never by model code」）：断言 `Queued` 态，转入 `Active { completion_tokens: 0 }`，把 `queued_at`（开户时抄下的）与 `Instant::now()`、prompt 长度一起写进本步账单的 `scheduled` 槽。

[ledger.rs:L180-L191](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L180-L191) 的 `reject` 关户并写入 `Terminal::Rejected { reason, prompt_tokens }`——**没有 token、没有 scheduled**：被拒的请求从未持有槽位（对照 u3-l3 的 `Terminal::Rejected` 文档）。

**流式记账：`push_tokens` 与三个至多一次的事实**

[ledger.rs:L195-L216](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L195-L216) 的 `push_tokens` 干四件事：① 断言 logprobs 要么空要么与 ids 等长（「absent or parallel」）；② 用 let-else 解构 `Active`——`Queued` 态直接 panic `push_tokens on {id} before admission`；③ **流水累加** `*completion_tokens += ids.len()`（4.2.2 不等式的唯一写入点）；④ 把 ids 与对齐后的 logprobs（未请求时补 `None`）追加进本步账单。

[L218-L237](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L218-L237) 的 `set_cached_tokens`/`echo_prompt`/`kv_transfer` 都断言 `is_active`、都只写账单的对应槽。为什么这三个不放进 `ScheduledInfo`？u3-l3 已答过：**时机自由**——缓存命中数要到第一个 prefill 分块才知道，而 `scheduled` 受「每生命周期恰一次」约束不能补写。

**关户五法**

- `finish`/`finish_with_cause`（[L241-L263](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L241-L263)）：`close` 取出账户、解构 `Active` 拿到流水、写 `Terminal::Finished { reason, stop_cause, prompt_tokens, completion_tokens }`。`completion_tokens` 来自账户字段——模型侧从未被询问。
- `fail`（[L265-L274](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L265-L274)）：两态通用，消息 `impl Into<String>`。
- `retire`（[L276-L286](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L276-L286)）：**静默**关户——不写终态、发一条 `log::debug!`，并把该 id 本步已缓冲的账单**整条抽走**（`statement.extract(id)`）。理由写在文档里：「前端已经为这个 id 拆掉了状态，**无人可寻址**」。既有测试 [retire_discards_buffered_output](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L486-L500) 锁住了这一点：先 `admit`+`push_tokens` 再 `retire`，本步账单空空如也。
- `defer_finish`/`defer_finish_with_cause`（[L288-L319](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L288-L319)）：P/D 分离的 prefill 角色要把 `Finished` 扣到「本步的 KV 保存对 peer 可见」才能发。它关户、把本步已缓冲的记录**连 token 一起抽出**、折上终态、连同 `tx.clone()` 装进 `DeferredFinish` 交还调度器——之后**从任何线程、任何时刻**调 `send()` 都不会与步流乱序，因为这个请求的整条记录作为一个条目走同一条通道。

**driver 面：`register` / `commit_step` / `fail_all`**

[ledger.rs:L321-L340](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L321-L340) 的 `register` 是开户唯一入口：消费信封（`envelope.consume()` **解除 drop 炸弹**——「从这里起，账户扛着应答到底的责任」）、抄下 prompt 长度与 `queued_at`、以 `Queued` 态插入、`assert!(previous.is_none())` 拒绝重复 id（id 来自 per-scheduler 计数器，撞号只可能是接线 bug），最后交出 `QueuedRequest`。

[ledger.rs:L342-L351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L342-L351) 的 `commit_step`：取走账单（4.3），「无更新且 metrics 未变」则**什么都不发**；否则 `tx.send(StepOutputs { updates })`——发送失败（接收端已关）被 `let _ =` 吞掉，注释说明 driver 会从提交通道察觉前端消失并收尾。

[ledger.rs:L353-L361](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L353-L361) 的 `fail_all`：对每个仍开着的账户 `fail` 同一条消息。唯一调用点在 drive 的致命路径（[driver.rs:L85-L93](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L85-L93)）：`step` 返回 `Err` → 记日志 → `fail_all(&format!("engine fatal: {error:#}"))` → `commit_step` → 线程退出。文档强调「**带着真实错误**而非 drop 扫尾的通用文案」——客户端看到的是 `engine fatal: <具体原因>`。测试 [fatal_step_fails_in_flight_requests_with_the_error](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L307-L340) 断言消息里含 `injected fatal`。

**`Drop` 销账扫尾：最后一道网**

[ledger.rs:L364-L391](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L364-L391)：账本倒下时（driver 线程正常返回、panic 展开、或任何路径把 `SchedulerBackend` drop 掉），每个仍开着的账户被写成 `Terminal::Failed`，文案按状态二选一——`Queued`：「request dropped by the engine before it was answered」；`Active`：「request dropped by the engine mid-stream」。注意 panic 也走这张网：调度器在 `step` 里记账违契约 panic → 展开 → `drive` 的局部（含账本）drop → 存活账户全部拿到 `Failed`。测试 [dropped_ledger_writes_off_open_accounts](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L538-L564) 一开一活两个账户同时销账；因为 `HashMap::drain` 顺序不定，测试先排序再断言。

**与 request_lifecycle 的握手**

- **开户前**：[`RequestEnvelope`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L40-L55) 的 [`Drop`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L81-L99) 覆盖「请求已存在但账户未开」的窗口：未被 `register` 消费就 drop（调度器已死、通道随 driver 倒掉），直接向步流发 `Failed`（"request dropped by the engine before it was answered"）。[`SchedulerHandle` 特意保留 `step_tx` 的克隆](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L73-L75)，让调度器死后再 submit 的请求也能把炸弹消息送达（测试 [`submit_to_a_dead_scheduler_fails_the_request`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L581-L594)）。
- **关户后**：[`DeferredFinish`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L101-L142) 的 [`Drop`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L144-L171) 把没送出的 `Finished` **改写成** `Failed` 再发——被扣住的 `Finished` 在 P/D 协议里兼任「KV 已就绪」的屏障信号，弄丢的句柄绝不能伪造成功。
- **全程**：[`RequestControl::abort`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L173-L198) 以 `Release` 序置位共享标志，账本 `is_aborted` 以 `Acquire` 序读——不关任何通道（对照 legacy 契约的 abort reason 设计，u3-l2）。

#### 4.2.4 代码实践

**实践目标**：写出本讲规格指定的「两步调度测试」——第一步 `admit` + `push_tokens`，第二步再 `push_tokens` + `finish`，断言步流上**恰好两条** `RequestUpdate`，且第二条的 token 计数**来自账本流水而非模型侧**。

**操作步骤**：

1. 继续写在你工作副本的 [ledger.rs 测试模块](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L393-L611)里（**示例代码**；沿用既有帮手，零新 import）：

```rust
#[test]
fn two_steps_ship_exactly_two_updates_with_ledger_tallied_counts() {
    let (handle, mut backend) = scheduler_pair();
    let mut steps = handle_steps(handle);
    let _control = handle.submit(request(vec![1, 2, 3]));
    let envelope = backend.submissions.try_recv().expect("envelope");
    let id = backend.ledger.register(envelope).id;

    // ── 第一步：接纳 + 一个 token ──
    backend.ledger.admit(id);
    backend.ledger.push_tokens(id, &[10], &[]);
    backend.ledger.commit_step(false); // 手动扮演 driver 的每迭代一次 commit

    // ── 第二步：再一个 token + 完结 ──
    // 注意：测试从未自己数过 token——finish 的计数只能出自账本流水。
    backend.ledger.push_tokens(id, &[11], &[]);
    backend.ledger.finish(id, FinishReason::Length);
    backend.ledger.commit_step(false);

    // ── 断言一：恰好两条更新，各在一帧里 ──
    let step1 = steps.try_recv().expect("first step message");
    assert_eq!(step1.updates.len(), 1);
    let u1 = &step1.updates[0];
    assert!(u1.scheduled.is_some());     // scheduled 恰一次：在接纳的那个步
    assert_eq!(u1.tokens, vec![10]);
    assert!(u1.terminal.is_none());

    let step2 = steps.try_recv().expect("second step message");
    assert_eq!(step2.updates.len(), 1);
    let u2 = &step2.updates[0];
    assert!(u2.scheduled.is_none());     // 第二步不再有 admission 事实
    assert_eq!(u2.tokens, vec![11]);
    assert!(matches!(
        u2.terminal,
        Some(Terminal::Finished { completion_tokens: 2, .. })
    ));                                   // 2 = 账本流水 1 + 1，不是测试算的

    // ── 断言二：之后再无消息（terminal 之后无人再提这个 id）──
    assert!(steps.try_recv().is_err());
}
```

2. `cargo test -p pegainfer-frontend --lib engine::ledger` 运行。

**需要观察的现象**：两帧消息、各一条 `RequestUpdate`；`scheduled` 只出现在第一帧；`completion_tokens = 2` 出现在终态里——而这个 2 在测试代码里**从未被手工计算或传入**，它只能来自 [push_tokens 的流水累加](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L205-L208)与 [finish_with_cause 的取数](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L253-L262)。最后的 `steps.try_recv()` 返回 `Err(TryRecvError::Empty)`——backend 仍持有发送端，只是 terminal 之后账本对这个 id 再无账可发。

**预期结果**：测试通过。若把 `commit_step` 的调用点挪一挪（比如两步之间多发一次），你会在最后的 `try_recv` 上看到额外帧——这就是「commit 的节奏属于 driver」的直接体感。

**实践要求复述：「对已关账户再 finish 会 panic」由哪类代码保证？**

由**基于所有权移除的运行时强制**保证，具体是三层普通（非 `debug_`）断言代码：① 每条终态路径第一行都调 [`close(id)`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L125-L129)，它是 `HashMap::remove(...).unwrap_or_else(|| panic!("no open account for {id}: ..."))`——第一次 `finish` 已把账户**移出** map，第二次的 `remove` 拿到 `None`，panic 在**这第二次调用的调用点**（即模型 crate 的那行代码）引爆；② 状态前置由 [`assert!`/let-else panic](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L253-L256) 把守（如 `finish on {id} before admission`）；③ 两条都被 `#[should_panic(expected = ...)]` 测试（[L596-L606](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L596-L606)）逐字锁进规格。它们全是 `panic!` 家族，release 构建照常生效；即便调度器无视这一切硬 panic，展开也会触发账本 `Drop` 把其余账户销账成 `Failed`——错误暴露，但客户端不挂死。

#### 4.2.5 小练习与答案

**练习 1**：为什么「Nothing is lost by deferring」（`Scheduler::submit` 的文档说，把裁决推迟到 `step` 里写不丢任何东西）？

**答案**：因为账单的上线节奏由 driver 拥有——每迭代**恰好一次** `commit_step`，且它发生在 `scheduler.step(...)` **之后**（[driver.rs:L81-L84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L81-L84)）。同一次迭代里，无论裁决是在排空提交的瞬间写还是在 `step` 里写，都汇进**同一张**账单、同一帧 `StepOutputs`。这让 `Scheduler::submit` 可以只做所有权转移，契约不需要「提交即裁决」的通道。

**练习 2**：`retire` 为什么是静默的，而 `reject`/`fail`/`finish` 都要写终态？

**答案**：终态是说给**客户端**听的。`retire` 的前提是 `is_aborted`——前端已经主动拆掉了这个 id 的全部状态（HTTP 流已断或已应答），此时再发 `Finished` 没有接收者，发了反而是协议噪声（消费侧会看到一个未知 id 的记录）。所以 `retire` 只做簿记：关户、debug 日志、把本步已缓冲的记录抽走——账本侧干净，线上零输出。

**练习 3**：drive 的致命路径为什么是 `fail_all` + `commit_step` 而不是直接 `return`（让 `Drop` 扫尾去发）？

**答案**：两个原因。① **文案**：`Drop` 扫尾只能发通用文案（"dropped by the engine ..."），`fail_all` 带的是真实错误 `engine fatal: {error:#}`，客户端排障需要它（测试 `fatal_step_fails_in_flight_requests_with_the_error` 断言了 `injected fatal` 在消息里）。② **确定性**：显式 `commit_step` 保证这批终态在 `fail_all` 后立即上线，不依赖 drop 顺序；随后 map 已空，`Drop` 的早退分支（`accounts.is_empty()`）不会再发第二条——不会重复应答，恰好呼应 terminal-exactly-once。

**练习 4**：`defer_finish` 为什么必须把本步已缓冲的记录（连 token）整条抽进 `DeferredFinish`，而不是只装一个终态？

**答案**：为了**迟投递不乱序**。若 token 留在本步账单先行上线、终态改由别的通道迟发，消费侧要么看到「同一步两条记录」（违反一步一条的线约定），要么要处理两条通道的交错。把整条记录（tokens + scheduled + terminal）折成一个条目、走**同一条**步流通道，`send()` 无论何时调用都等价于「这一步本来就有这么一条记录」——顺序问题在构造上消失。测试 [defer_finish_folds_step_output_and_delivers_late](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L502-L536) 验证了「当步空账单 + 迟到的完整记录」。

---

### 4.3 StepStatement 合并：一步一账单

#### 4.3.1 概念说明

调度器在一个步里对同一请求可能写好几次账：`admit` 之后 `push_tokens` 两次、`set_cached_tokens` 一次、最后 `finish`。但线类型约定「每请求每步**至多一条** `RequestUpdate`」。把多次记账**合并**成一条记录，就是 `StepStatement`（本步账单）的职责——它是 `RequestLedger` 的第二个字段，也是 u3-l3 反复引用的「账本的生产侧」的实体。

它同时回答两个问题：

- **合并**：同一 id 的所有写入落到同一条记录的不同槽位（`scheduled` 一个槽、`tokens` 追加、`terminal` 一个槽）——「一步一条」由数据结构保证，不靠调度器自觉；
- **收回**：`retire` 与 `defer_finish` 需要把某 id 的记录**整条拿走**（前者丢弃、后者折进迟投递消息），缓冲要支持「挖洞不搬家」。

#### 4.3.2 核心流程

```
step 开始（账单为空）
  ledger.admit(id)          ┐
  ledger.push_tokens(id,…)  │  每次调用：
  ledger.set_cached_tokens  │    statement.entry(id) → 找到或新建该 id 的记录
  ledger.finish(id)         ┘    写进对应字段/追加 tokens
step 结束，driver 调 commit_step：
  statement.take_updates()
    ├─ 清空 id → 槽位 索引
    ├─ drain 整个 buffer，flatten 跳过被挖走的洞
    └─ filter(!is_vacant)：丢弃无任何可观察事实的空记录
  updates 为空 且 metrics 未变 → 不发送
  否则 tx.send(StepOutputs { updates })
```

三个值得记住的性质：**至多一条**（`entry` 按 id 合并）、**首触有序**（buffer 按第一次触碰的顺序排列，账单内记录顺序确定）、**空账不发货**（`is_vacant` 过滤 + `commit_step` 的短路）。

#### 4.3.3 源码精读

**结构：buffer 加索引**

[ledger.rs:L69-L75](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L69-L75)：`buffer: Vec<Option<RequestUpdate>>` 加 `index: HashMap<RequestId, usize>`。为什么不是 `HashMap<RequestId, RequestUpdate>` 一把梭？两个原因：① **顺序**——HashMap 迭代顺序不定，而账单里记录的先后（谁先被触碰）应当确定，`Vec` 保序；② **挖洞**——`extract` 用 `take` 把值取走留下 `None` 槽（[L88-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L88-L92)），不搬动其他元素、不失效其他 id 的索引；同一步内若该 id 再被触碰，`entry` 会开一条**新**记录（索引已删，`or_insert_with` 重新分配槽位）。

**`entry`：合并的执行者**

[ledger.rs:L77-L86](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L77-L86)：`index.entry(id).or_insert_with(|| { buffer.push(empty); buffer.len()-1 })`——第一次见到这个 id 就开新记录，之后每次都返回**同一条**记录的可变引用。末尾的 `.expect("indexed step entry was extracted without index cleanup")` 是 buffer 与索引之间的一致性断言：正常流程里「索引还在、槽位却是洞」不可达（`extract` 总是先删索引再挖洞）。

**`take_updates`：出账并滤空**

[ledger.rs:L94-L102](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L94-L102)：清索引、`drain(..).flatten()`（保序 + 跳洞）、再 `filter(|u| !u.is_vacant())`。`is_vacant`（[step.rs:L148-L155](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L148-L155)）判「所有区段都空」——只做过簿记、没产生任何可观察事实的记录**不上线**。文档注释举例的场景是「一个请求本步唯一的活动是簿记」。

**commit 的节奏与 metrics 搭车**

`commit_step`（[ledger.rs:L342-L351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L342-L351)）的签名带 `metrics_changed: bool`，唯一调用点是 drive 里 `metrics.publish` 之后的 [driver.rs:L81-L84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L81-L84)。于是有了 u3-l3 见过的那条规则的具体实现：**账单空但 metrics 变了 → 发一帧空 `updates` 的消息**（纯 metrics 通知，典型场景是取消请求后 running/waiting 计数变化）；**两者皆无 → 什么都不发**。发送端 `MetricsPublisher` 的「`Mutex` 整体换快照、比较前后是否变化」在 [wiring.rs:L51-L65](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L65)。

**既有测试即规格**

账本测试套件里一半在验证合并语义：[`admission_tokens_and_finish_fold_into_one_entry`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L420-L455) 在**一次** commit 前 `admit` + `push_tokens` + `set_cached_tokens` + `finish_with_cause`，断言产出**一条**记录，且 `scheduled`、`tokens == vec![10, 11]`、`logprobs.len() == 2`、`cached_tokens == Some(2)`、`Terminal::Finished { completion_tokens: 2, .. }` 各就各位——这就是 4.3.1 说的「多次记账、一条记录」。

#### 4.3.4 代码实践

**实践目标**：亲手验证「同一步内多次 `push_tokens` 合并成一条记录、logprobs 自动对齐」，以及账单的确定性顺序。

**操作步骤**：

1. 在工作副本的 ledger.rs 测试模块追加（**示例代码**）：

```rust
#[test]
fn same_step_pushes_merge_into_one_aligned_record() {
    let (handle, mut backend) = scheduler_pair();
    let mut steps = handle_steps(handle);
    let _control = handle.submit(request(vec![1]));
    let envelope = backend.submissions.try_recv().expect("envelope");
    let id = backend.ledger.register(envelope).id;

    backend.ledger.admit(id);
    backend.ledger.push_tokens(id, &[10], &[]);        // 1 个 token，无打分
    backend.ledger.push_tokens(id, &[11, 12], &[]);    // 2 个 token
    backend.ledger.push_tokens(id, &[13], &[]);        // 1 个 token
    backend.ledger.commit_step(false);                 // 一次 commit，一条记录

    let step = steps.try_recv().expect("one merged message");
    assert_eq!(step.updates.len(), 1);
    let update = &step.updates[0];
    assert_eq!(update.tokens, vec![10, 11, 12, 13]);   // 三次追加合一
    assert_eq!(update.logprobs.len(), 4);              // logprobs 平行对齐
    assert!(update.logprobs.iter().all(|lp| lp.is_none())); // 未请求打分全 None
    assert!(update.terminal.is_none());                // 本步没关户
    assert!(steps.try_recv().is_err());                // 恰好一帧
}
```

2. 运行 `cargo test -p pegainfer-frontend --lib engine::ledger same_step`。

**需要观察的现象**：三次 `push_tokens`、一次 `commit_step` → 步流上**一帧**、一条记录、四个 token 按调用顺序拼接；`logprobs` 长度自动补齐到 4 且全为 `None`（[push_tokens 里「未请求打分则补 None」的分支](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L211-L215)）。

**预期结果**：测试通过。作为对照，把三次 `push_tokens` 拆进两次 `commit_step`（如 4.2.4 那样），你会得到两帧各一条——「几帧」完全由 commit 次数决定，「一条记录里有什么」由同一步内的全部记账决定。

#### 4.3.5 小练习与答案

**练习 1**：`StepStatement::extract` 为什么必须「先删索引、再挖洞」？反过来会怎样？

**答案**：先挖洞再删索引的话，两者之间存在一个「索引指向 `None` 槽」的中间态；虽然单线程下最终一致，但 `entry` 的 `or_insert_with` 若在此间被调（同一 id 再次被记账），会拿到洞并触发末尾的 `.expect("indexed step entry was extracted without index cleanup")`。先删索引，后续 `entry` 走「新建」分支开新记录，语义变成「retire 之后同一步内重新开始记账」——这正是 `retire` 后账户已关、任何再触碰会先在 `close` 处 panic 之外的又一层有序性。（实际上 retire 后再记账根本到不了 `entry`：账户已移除，前置的取账户就 panic 了。）

**练习 2**：一帧「空 `updates`」的 `StepOutputs` 什么时候会出现？谁需要它？

**答案**：`commit_step(metrics_changed = true)` 且本步无请求更新时。需要它的是**前端的路由与监控逻辑**：metrics 变化（比如取消一个请求让 running 数下降）通过步流广播，协议栈在收步的循环里顺带刷新对分区的负载认知。这是 u3-l2/u3-l6 讲过的「metrics 变化经步流信号化」在 step 契约里的延续，u4-l6 会追到 `/metrics`。

**练习 3**：为什么 `take_updates` 用 `drain(..).flatten()` 而不是 `retain` 或重建 `Vec`？

**答案**：`buffer` 的元素是 `Option<RequestUpdate>`；`drain(..)` 把整个 Vec 取空（留下空 buffer 供下步复用），`flatten()` 在迭代时跳过 `None`（被 extract 挖走的洞）——一次遍历同时完成「取走全部」「跳洞」「保序」三件事，且不需要原地修补结构。配合先行的 `index.clear()`，账单回到初始状态，两个集合的一致性由操作顺序保证。

## 5. 综合实践

**任务：账本全生命周期巡检——一次测试走完四种关户与一次销账。**

在 ledger.rs 测试模块写一个测试 `ledger_lifecycle_tour`，用**五个请求**把本讲全部内容串起来。骨架（**示例代码**，请补全断言）：

```rust
#[test]
fn ledger_lifecycle_tour() {
    let (handle, mut backend) = scheduler_pair();
    let mut steps = handle_steps(handle);

    // 提交五个请求，全部 register（复用 request() 帮手，prompt 长度互不相同）
    // A: 正常完结  B: 接纳期拒绝  C: abort 后 retire  D: defer_finish
    // E: 什么都不做，留给 Drop 扫尾
    // ... 依次 submit + try_recv + register，收集 id ...

    // 第一步：A admit+push+finish；B reject；C admit+push；D admit+push+defer_finish
    // commit_step(false) → 断言本帧恰好 3 条记录（四个请求、四笔账，为何只有三条？
    // 提示：D 的整条记录被 defer_finish 抽进了 DeferredFinish，没有留在账单上）
    // 第二步：C is_aborted 探测（先 control.abort()）→ retire → commit
    //        → 断言本帧为空（retire 静默）或恰在 metrics 语义下无输出
    // 测试末尾 drop(backend) → 断言 E 的 Failed（"before it was answered"）
    //                      以及 D 若未 send，其 DeferredFinish 的 drop 炸弹改写为 Failed
}
```

**验收清单**（每条都对应本讲一个论断）：

1. A 的终态 `Finished { completion_tokens }` 与你 push 的 token 总数一致——账本流水；
2. B 的记录**没有** `scheduled`、**没有** token——拒绝即从未接纳；
3. C 在 abort 置位后 `is_aborted` 为真、`retire` 后当帧**无输出**——静默关户（对照既有测试 `retire_discards_buffered_output`）；
4. D 的 `defer_finish` 返回值在被 drop 前不产生任何线上输出，drop 后收到改写成 `Failed`（"deferred finish dropped ..."）的完整记录——第三层 drop 保护；
5. E 只在 `drop(backend)` 后收到 `Failed`（"dropped by the engine before it was answered"）——Drop 销账扫尾；
6. 全程每个 id 的线上事实序列满足：`scheduled` 恰一次（或零次，若被拒/被 retire）、terminal 至多一次、terminal 之后无任何记录——u3-l3 的消费侧公理在生产侧的兑现。

**完成后回答**：如果把 D 的 `defer_finish` 换成普通 `finish`，第 1 帧的记录数会怎么变？（答：D 的记录不再被抽走，`scheduled+tokens+terminal` 合并在第 1 帧里——`defer` 与 `finish` 的区别只在于**终态何时、经谁的手上线**，记账本身完全同构。）

## 6. 本讲小结

- **账本 = 未答复请求的户口簿**：一个账户一条欠款（abort 引用、prompt 长度、queued_at、状态与 token 流水）；负载在 `submit` 时已归调度器，账户只存「引擎还欠客户端什么」。
- **生命周期一条单向路**：`register` 开户（`Queued`）→ `admit` 转 `Active { completion_tokens }` → `push_tokens` 等流式记账 → 五种关户（`finish`/`fail`/`reject`/`retire`/`defer_finish`），`fail` 两态通用、其余各有前置状态；账本 `Drop` 再兜底一层销账。
- **terminal 恰一次 = 所有权移除**：关户是把账户从 HashMap **移走**，「已关」不是标志而是不存在；任何对已关/未知账户的触碰在**出错调用点** panic（`no open account for {id}`），普通 `panic!`/`assert!` 在 release 依然生效，`#[should_panic]` 测试逐字锁死规格。
- **计数单向流**：`completion_tokens` 只在 `push_tokens` 累计、只在终态上报——模型侧算术永远插不进这条链，「never from model-side arithmetic」是结构保证。
- **节奏归 driver**：`register`/`commit_step`/`fail_all` 是 `pub(crate)`，唯一调用点在 `drive`——每迭代先排空提交、再 `step`、再 publish metrics、最后 commit 一次；「submit 只转移、step 单一发射点」的纪律由此免费成立。
- **StepStatement 一步一账单**：同 id 的多次记账经 `entry` 合并成一条 `RequestUpdate`，`extract` 支持整条收回（retire 丢弃 / defer 折进迟投递），`take_updates` 滤掉空记录；空账单 + metrics 变化才发空帧，皆无则静默。

## 7. 下一步学习建议

1. **下一讲 [u3-l5]（Scheduler 与 drive 循环）**：本讲把 driver 当「节奏所有者」引用了多次，下一讲把它完整展开——`Scheduler` 三方法的分工契约、`drive` 的排空顺序、致命错误路径、空闲自旋，以及 `spawn_scheduler` 怎样把本讲的账本装进线程。你会发现本讲 4.2.2 的流程图就是下一讲的目录。
2. **[u5-l1]（KV 与请求控制细节）**：本讲 4.2.3 末尾的「三层 drop 保护」在那里得到完整拆解——`RequestEnvelope` 的炸弹为什么用 `Option<inner>`、`DeferredFinish` 的屏障语义、`RequestControl` 与 KV 前缀 hold 的交互。
3. **[u4-l3]（两代引擎桥）**：账本产出的 `StepOutputs` 谁在消费？stepped 桥把每帧 1:1 翻译成 EngineCoreOutputs——带上本讲「一步一账单」的模型去读，你会看到「burst 折叠」为何在 stepped 桥里消失。
4. **动手方向**：给 4.2.4 的两步测试加第三个请求走 `reject`，验证「一帧账单可同时含多个请求的异构记录」；再读一遍 [ledger.rs 的模块文档](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L1-L20)——学完本讲后，那 20 行应该已经一句都不多余。
