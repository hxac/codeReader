# u5-l1 · KV 前缀解析与请求控制细节

## 1. 本讲目标

本讲钻进引擎契约的「边缘地带」——那些不处在 `submit → step → terminal` 主干道上、却决定请求生死与正确性的三块硬骨头：

1. **KvPrefix**：KV 前缀解析结果。三个成员各有讲究——`hit_tokens` 让退化解析只是一个更小的数、`rank` 把请求钉死在解析时的分区上、`hold` 是一个全契约都只能 drop 的不透明 RAII 把手。
2. **RequestEnvelope / RequestControl / DeferredFinish**：账本管不到的三段窗口里的「应答到底」机制——信封的 drop 炸弹、每请求的原子 abort 旗、以及迟到投递的完结消息。
3. **StopPolicy / StopCause**：token 级停止的判定词汇。它随终态上行，并且在 `DeferredFinish` 的迟到终态场景里被原样保真。

学完本讲，你应该能：

- 解释 `KvPrefix` 三个成员的语义，以及「已解析前缀与调用方绑定 rank 不一致」为什么被判为调用方 bug 而非引擎故障；
- 走查一条 abort 从前端到调度器 `retire` 的完整路径，并说出调度器线程消失后新提交为何仍能拿到 `Failed` 终态；
- 读懂 `StopPolicy::classify` 的优先级规则与 `StopCause` 在终态里的传递位置。

## 2. 前置知识

本讲假设你已读完 u3-l4（RequestLedger 账本）与 u3-l6（接线与路由）。在此之上，补四个 Rust 语言层面的概念：

- **RAII 与 `Drop` trait**：Rust 没有手动 `close`/`free`，资源生命周期绑在值的生命周期上。值离开作用域（或被 drop）时 `Drop::drop` 自动运行。所谓 **drop 炸弹**，就是故意在 `drop` 里做「善后应答」——保证哪怕持有者提前死亡，副作用也发生。
- **`Drop` 不能移出字段**：Rust 规定 `drop(&mut self)` 只拿可变引用，不能把字段 move 出来。所以「消费式转移 + 析构兜底」的类型都长一个样：`inner: Option<T>`，消费方法 `take()` 走内值（拆掉引信），`Drop` 在 `Some` 上引爆。本讲的 `RequestEnvelope` 与 `DeferredFinish` 都是这个模式。
- **`Arc<AtomicBool>` 与 Release/Acquire 内存序**：跨线程的每请求取消旗。写方用 `Release`（保证旗子置位前对自己状态的写入对读方可见），读方用 `Acquire`（保证读到 true 后看到的都是写方旗前的世界）。这是「无通道取消」的基石。
- **`debug_assert!` vs 运行期错误**：`debug_assert!` 只在 debug 构建生效，表达「这是调用方违反契约，生产环境不值得为它付检查成本」。对照 u2-l3 的分工原则——构造期 panic 管代码错、运行期 `Err` 管命令错——`debug_assert` 管的是**调用方代码错**。

另外回顾一个账本事实（u3-l4）：账户关闭即从 HashMap 移走，触碰已关账户在出错调用点 panic；终态计数只出自账本流水。本讲的三个类型全是「账本之外」的补充机制。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `../pegainfer-frontend/src/engine/kv.rs` | KV 前缀解析类型与池容量算术 | `KvPrefix` 三成员、`SubmittedRequest`、`KvCapacity` |
| `../pegainfer-frontend/src/engine/request_lifecycle.rs` | 信封与迟到投递把手 | `RequestEnvelope` drop 炸弹、`DeferredFinish`、`RequestControl` |
| `../pegainfer-frontend/src/engine/stop.rs` | token 停止策略 | `EosPolicy`、`StopPolicy::classify`、`StopCause` |
| `../pegainfer-frontend/src/engine/handle.rs` | legacy 引擎把手与多分区路由 | `submit_resolved` 的 rank 裁决、`reject_unroutable` |
| `../pegainfer-frontend/src/engine/ledger.rs`（辅读） | 活请求账本 | `is_aborted`、`retire`、`defer_finish_with_cause`、`register` |
| `../pegainfer-frontend/src/engine/wiring.rs`（辅读） | step 契约双端接线 | `SchedulerHandle::submit` 铸造信封 |
| `../pegainfer-frontend/src/engine/sink.rs`（辅读） | legacy 打标事件通道 | `TokenSink` 的 abort reason（legacy 侧取消） |
| `../pegainfer-frontend/src/vllm/bridge/stepped.rs`（辅读） | step 引擎桥 | Abort 帧如何变成 `control.abort()` |
| `../pegainfer-frontend/examples/echo-server.rs`（辅读） | 无 GPU 参考模型线 | `EchoScheduler::step` 里的 retire 响应 |
| `../pegainfer-qwen3/src/frontend_adapter.rs`（辅读） | Qwen3 调度器到账本的适配 | `defer_finish_with_cause` 的生产调用点 |

> 提醒：frontend crate 是 CUDA-free 的，本讲全部源码与实践都不需要 GPU。

## 4. 核心概念与源码讲解

### 4.1 KvPrefix：不透明 RAII hold 与 rank 绑定

#### 4.1.1 概念说明

前缀缓存的核心直觉：同一个会话的第二轮请求，其 prompt 的前一大段（第一轮的 prompt + 第一轮的回答）的 KV 状态在 GPU 上已经算过一遍，可以复用。**前缀解析**就是在请求到达调度器*之前*，由 KV store 回答一个问题：「这条 prompt 的前多少个 token 已经物化在某个分区的 GPU 前缀缓存里了？」

`KvPrefix` 就是这个答案的载体。它的设计张力在于：`pegainfer-frontend` 这个 crate 是 CUDA-free 的，它**不知道也不该知道** KV 内部长什么样。于是答案被拆成三个成员：

- `hit_tokens: usize` —— 命中了多少个前导 token。**退化解析（超时、池压力）不是独立状态，只是一个更小的数**——消费方统统「从 `hit_tokens` 处开始 prefill」，数字本身承载全部下游语义。
- `hold: Option<Box<dyn Any + Send>>` —— 不透明 RAII 把手。由 `pegainfer-kv-store` 铸造，作用是**钉住**（anti-eviction pin）已命中的块不被驱逐。除铸造它的 store（可 downcast 回来）外，所有 crate 只能 drop 它——drop 即释放钉住。
- `rank: Option<usize>` —— 解析跑在哪个调度器分区上。hold 把块钉在**这个** rank 上，路由去别的分区会白白丢命中、浪费钉住。

#### 4.1.2 核心流程

一条 `KvPrefix` 的一生：

```text
pegainfer-kv-store 解析请求前缀
        │ 铸造 KvPrefix::resolved(hit_tokens, rank, hold)
        ▼
SubmittedRequest = (GenerateRequest, KvPrefix)   ← tuple 而非包装结构：
        │                                          「store 的产物是前缀解析，
        ▼                                           不是一种新请求」
EngineHandle::submit_resolved 路由裁决（rank 优先）
        │
        ▼
调度器在 match_and_add_prefix 中消费命中
        │
        ├── 正常：hold 随值 drop → 释放钉住
        └── 请求先死：hold 随请求 drop → 同样释放钉住
```

路由裁决是一棵三岔决策树（详见 4.1.3 的 `submit_resolved`）：已解析 rank 压倒一切 → 其次调用方绑定 rank → 都没有则最低负载分区。无解析路径（`KvPrefix::none()`）走后两岔。

附带一提同文件的 `KvCapacity`：KV 池按**整块**分配，\( L \) 个 token 占 \( \lceil L / \text{block\_size} \rceil \) 块，所以批量适配检查必须**逐请求向上取整再求和**——直接加总 token 数会少算，放进一个调度器随后只能推迟的批。

#### 4.1.3 源码精读

**类型本体与三成员**——注意结构体文档对「退化即更小的数」和「不透明 hold」的原文约定：

- [kv.rs:L6-L30](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L6-L30)：`KvPrefix` 的文档注释与结构体。`hit_tokens` 是已物化的前导 token 数；`hold` 由 `pegainfer-kv-store` 铸造、全契约只能 drop；`rank` 字段的注释直说「hold 把块钉在 THIS rank 上，路由去别处会静默丢命中并浪费钉住，`submit_resolved` 按它路由」。

```rust
pub struct KvPrefix {
    hit_tokens: usize,
    hold: Option<Box<dyn Any + Send>>,
    rank: Option<usize>,
}
```

**两个构造器**——「没有解析」与「有解析」是仅有的两种出身：

- [kv.rs:L33-L42](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L33-L42)：`KvPrefix::none()`——没跑解析（或退化到一无所有），从头 prefill；调度器自己的 GPU 前缀匹配照常生效。
- [kv.rs:L44-L53](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L44-L53)：`KvPrefix::resolved(hit_tokens, rank, hold)`——三个成员一次配齐，rank 与 hold 同时从 `None` 变 `Some`，二者永远同生同灭。

**访问面刻意收窄**——`rank()` 是 `pub(crate)`，外界拿不到：

- [kv.rs:L55-L73](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L55-L73)：`hit_tokens()` 公开；`rank()` 只有 crate 内（即 `EngineHandle::submit_resolved`）能读；`has_hold()` 只答「还在钉吗」；`hold_any()` 供铸造方 downcast，其余 crate 视之为 drop-only。

**Debug 实现泄露的是「不泄露」**：

- [kv.rs:L76-L84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L76-L84)：`hold` 字段打印为字符串 `<opaque>`——Debug 输出里也只有「有没有」，没有「是什么」。

**SubmittedRequest 是个 tuple 而非包装结构**：

- [kv.rs:L86-L90](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L86-L90)：文档点明设计意图——store 的产物是「前缀解析」，不是一种新型请求。未解析路径提交 `KvPrefix::none()`。

**rank 的裁决现场**在 legacy 把手的 `submit_resolved`：

- [handle.rs:L191-L200](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L191-L200)：公开的 `submit` 就是「无解析」的语法糖——`submit_resolved(req, KvPrefix::none())`。
- [handle.rs:L200-L236](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L200-L236)：路由主体。关键一段：

```rust
let partition = match (kv_prefix.rank(), req.data_parallel_rank) {
    (Some(resolved), bound) => {
        debug_assert!(
            bound.is_none_or(|b| b == resolved),
            "request bound to rank {bound:?} but its prefix resolved on rank {resolved}"
        );
        resolved
    }
    (None, Some(bound)) => bound,
    (None, None) => self.least_loaded_partition(),
};
```

已解析 rank **无条件取胜**（生产构建里即使与调用方绑定不一致也路由到 resolved）；不一致只触发 `debug_assert`。`(None, None)` 时走 [handle.rs:L238-L260](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L238-L260) 的最低负载：\( \text{score} = \text{running} + 4 \times \text{waiting} \)（vLLM DP 策略的权重），并列取小下标，无 feed 的分区记 0 分。

- [handle.rs:L231-L235](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L231-L235)：rank 越界（不在 `submit_txs` 范围内）不算引擎故障——[handle.rs:L271-L287](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L271-L287) 的 `reject_unroutable` 以标准 `Scheduled → Rejected` 事件对作答并返回 `Ok(())`。

**池容量算术**（顺带）：

- [kv.rs:L92-L104](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L92-L104) 与 [kv.rs:L114-L118](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L114-L118)：`blocks_for` 用 `div_ceil` 表达整块分配的向上取整。

#### 4.1.4 代码实践

**实践目标**：把 `submit_resolved` 的三岔路由决策树手工推导成表，再用 crate 自带的单元测试逐叶验证。

1. **操作步骤**：
   - 精读 [handle.rs:L200-L236](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L200-L236)，画出 `(kv_prefix.rank(), req.data_parallel_rank)` 四种输入组合（`(Some,_)`、`(None,Some)`、`(None,None)`、越界）到输出分区的表。
   - 在工作区根目录运行：
     ```bash
     cargo test --release -p pegainfer-frontend --lib engine::handle
     ```
   - 把每个测试名对应到决策树的一片叶子：`multi_partition_submit_routes_by_bound_rank` 对应 `(None, Some)`；`multi_partition_submit_places_unbound_on_the_least_loaded` 对应 `(None, None)` 且验证 4×waiting 权重；`multi_partition_out_of_range_rank_is_rejected_not_dropped` 对应越界叶（断言事件序列是 `Scheduled` 后跟 `Rejected`，错误消息含 `outside 0..1`）。
2. **需要观察的现象**：测试全部通过；`multi_partition_submit_places_unbound_on_the_least_loaded` 里第二段把 rank 0 的 running 抬到 6 后，请求翻到 rank 1（\( 4 < 6 \)）。
3. **预期结果**：三个路由测试各钉住决策树一叶；`kv_prefix.hit_tokens()` 在测试里被断言为 0（`submit` 走的是 `none()`）。frontend 是 CUDA-free crate，纯 CPU 可跑；若工作区 feature 组合导致编译问题，以 `cargo test -p pegainfer-frontend --lib multi_partition` 收窄过滤再试。
4. 上述测试名与断言内容均来自 [handle.rs:L357-L420](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L357-L420) 的测试模块原文；实际运行输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么「退化解析（超时、池压力）」不设计成一个独立的 `Degraded` 状态，而只是「一个更小的 `hit_tokens`」？

**答案**：因为下游语义完全由数字承载。除 disaggregated-decode 的接纳检查会把 `hit_tokens` 与交接承诺长度对账外，所有消费方都只是「从 `hit_tokens` 开始 prefill」——命中 700 与命中 0 之间是连续谱，没有需要分支处理的质变。加一个状态只会让每个消费方多一个必须正确忽略的分支（见 [kv.rs:L17-L21](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L17-L21) 的文档注释）。

**练习 2**：`KvPrefix` 的 `Debug` 实现为什么把 `hold` 打成 `<opaque>` 而不是直接 derive？

**答案**：`Box<dyn Any + Send>` 的 derive Debug 输出没有任何信息量（`Any` 没有 `Debug` 约束）；手写实现 ([kv.rs:L76-L84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L76-L84)) 把它降为「有/无」一个布尔事实，与「除铸造者外人人只能 drop」的边界一致——连调试输出都不承诺 hold 的内部结构。

**练习 3**：`hold_any()` 存在的目的是什么？普通模型 crate 应该调用它吗？

**答案**：供铸造 hold 的 `pegainfer-frontend` 之外的 store（`pegainfer-kv-store`）把 `dyn Any` downcast 回具体类型做读侧操作；其余所有 crate 的唯一合法操作是让 `KvPrefix` 被 drop（[kv.rs:L69-L73](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L69-L73) 注释原文「Every other crate treats the hold as drop-only」）。普通模型 crate 不应调用。

### 4.2 RequestEnvelope / RequestControl / DeferredFinish：应答到底的三件套

#### 4.2.1 概念说明

账本为「已开户」的请求兜底，但一个请求的生命周期里有三段**账本管不到的窗口**，各需一件专用机制：

| 窗口 | 问题 | 机制 |
|---|---|---|
| `SchedulerHandle::submit` → `ledger.register` 之间 | 请求躺在提交通道里，或被发往一个**已退出的调度器线程**——没有账户替它应答 | `RequestEnvelope`（信封）的 drop 炸弹 |
| 请求被接纳后的任意时刻 | 前端要取消**单个**请求，又不能关闭其他请求还在用的通道 | `RequestControl`（原子 abort 旗） |
| 调度器想完结请求，但终态必须晚点、从别的线程发 | P/D 分离的 prefill 角色要把 `Finished` 扣到本步 KV 保存对 peer 可见之后 | `DeferredFinish`（迟到投递的消息） |

三件套共享同一条铁律（[request_lifecycle.rs:L19-L22](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L19-L22) 原文）：

> An unanswered request must surface as a `Failed` terminal, never a client hang.

（未获应答的请求必须以 `Failed` 终态浮出水面，绝不能让客户端挂着。）

而「drop 炸弹」能工作的语言机制只有一个：`Drop` 拿不到所有权，所以消费式转移用 `Option<inner>` + `take()` 实现——消费成功则拆掉引信，忘了消费则炸弹在 drop 时引爆。

#### 4.2.2 核心流程

**abort 的一条完整旅程（step 契约侧）**：

```text
客户端断开 / 上游发 Abort 帧
        │  stepped 桥：先 drop 本请求的 stream 状态，再置旗
        ▼
RequestControl::abort()          ← Release 写 Arc<AtomicBool>
        │  （不关任何通道；其他请求不受影响）
        ▼
调度器下一个 step 触碰该请求：
        ledger.is_aborted(id)    ← Acquire 读同一 AtomicBool
        │ true
        ▼
ledger.retire(id)                ← 静默关户：丢弃本步为它缓冲的一切，
        │                          从 statement 中 extract 掉
        ▼
（步流上不出现该请求的更新——前端早已删掉它的状态，无人可寻址）
```

**信封兜底的旅程（调度器已死）**：

```text
SchedulerHandle::submit 铸信封（id + abort 旗 + step_tx + 请求 + 时间戳）
        │ crossbeam send 失败（调度器线程没了，通道关闭）
        ▼
返回的信封被显式 drop → 引爆：
        构造 RequestUpdate { terminal: Failed { "request dropped by the engine
        before it was answered", prompt_tokens, 0 } }
        │
        ▼
        经 handle 保管的 step_tx 副本发进步流 → 客户端观察到终态
```

注意一个精妙处：`SchedulerHandle` 特意保管一份 `step_tx` 的 clone（[wiring.rs:L73-L75](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L73-L75)），注释明说「让调度器线程退出后铸出的请求仍能送出 drop 炸弹终态（信封需要一个活的 sender）」。

**legacy 侧对照**：legacy 契约没有 `RequestControl`，取消走 `TokenSink` 的共享 abort reason——一个 `Arc<AtomicU8>`，取值 `None / Cancelled / Disconnected`。`send` 在 reason 非 `None` 时直接返回 `Err`、`is_closed` 为 true，调度器在下一次 emit 时读到「消费者没了，retire」——与旧的「drop 每请求接收端」等价的**反应式**退役，只是信号从通道关闭换成了 reason 位。

#### 4.2.3 源码精读

**模块文档：三件套的分工总纲**——

- [request_lifecycle.rs:L1-L22](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L1-L22)：逐一列出三件套覆盖的窗口，末尾给出 `Option<inner>` 的唯一理由与「绝不客户端挂起」的铁律。

**信封：结构与引信**——

- [request_lifecycle.rs:L40-L79](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L40-L79)：`RequestEnvelope` 持 `Option<EnvelopeInner>`；`EnvelopeInner` 五字段——`id`、`abort`（与 `RequestControl` 共享的同一面旗）、`tx`（步流 sender）、`request`（负载本体）、`queued_at`。`consume()` 是 `take()` + 断言「submission consumed twice」。

- [request_lifecycle.rs:L81-L99](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L81-L99)：**drop 炸弹本体**。未被消费的信封在 drop 时构造一条 `Terminal::Failed`（消息固定为 `"request dropped by the engine before it was answered"`，prompt_tokens 取自请求，completion_tokens 为 0）并发进步流。`let _ = inner.tx.send(...)` 的 `let _ =` 是刻意的：接收端可能也没了，此时无人可寻址，发送失败无所谓。

**消费现场在账本的 `register`**——

- [ledger.rs:L323-L340](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L323-L340)：`register` 调 `envelope.consume()` 拆引信，随后开账户——**应答责任从信封移交账户**。注意账户存的第一字段就是 `abort: inner.abort`：那面旗从铸造起就与账户共享，`RequestControl` 翻旗即账户可见。

**铸造现场在 `SchedulerHandle::submit`**——

- [wiring.rs:L79-L95](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L79-L95)：铸造 `RequestId`、时间戳与 `Arc<AtomicBool>`，造出 `RequestControl`（拿旗的 clone）与信封（拿旗本体），`submit` **永不失败**——send 失败时 `drop(returned.into_inner())` 显式引爆信封。

**每请求取消旗**——

- [request_lifecycle.rs:L173-L198](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L173-L198)：`RequestControl` 只有 `id()` 与 `abort()` 两个公开方法。`abort()` 的文档说清内存序：`Release` 排在调用方自己拆除每请求状态**之后**，镜像调度器侧的 `Acquire` 读。

```rust
pub fn abort(&self) {
    self.abort.store(true, Ordering::Release);
}
```

**调度器侧的观察与响应**——

- [ledger.rs:L133-L138](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L133-L138)：`is_aborted` 就是 `Acquire` 读旗。文档强调「abort 随时可能落地，所以探针要出现在**每条** finish 路径上」。
- [ledger.rs:L276-L286](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L276-L286)：`retire`——关户、打一条 debug 日志（`request retired: frontend aborted: {id} tokens_shipped=...`）、`statement.extract(id)` 把本步已缓冲的更新**整条抽走**。静默是刻意的：前端已删掉该 id 的状态，没人可寻址。
- [echo-server.rs:L121-L143](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L121-L143)：`EchoScheduler::step` 在两处探针——接纳循环入口（排队期间被 abort）与每个 running 请求每步开头（生成途中被 abort），两处都是 `is_aborted → retire → continue`。

**前端置旗的两个现场（stepped 桥）**——

- [stepped.rs:L292-L308](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L292-L308)：收到 vLLM `Abort` 帧时，**先** `streams.remove`（drop 本请求的桥侧状态）**再** `control.abort()`——注释明说 Release 写要排在移除之后，且「update already in flight finds no stream entry」（在途更新找不到流表项，被 [stepped.rs:L236-L241](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L236-L241) 的 `let Some(state) = ... else { continue }` 丢弃）。
- [stepped.rs:L187-L192](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L187-L192)：桥关闭路径——对**所有**在途请求翻旗，随后 drop 分区把手断开提交通道，让 driver 排空退出。

**迟到投递的完结**——

- [request_lifecycle.rs:L101-L141](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L101-L141)：`DeferredFinish` 同样是 `Option<inner>`；`send()` 消费并单发一条 `StepOutputs`。注释说明它由 `defer_finish` 铸造——账本关户时把该请求本步已缓冲的记录（**含 token**）折进这一条消息，所以从任何线程、任何晚些时候发送都不可能与步流乱序：整份 per-request 记录作为一个条目走。
- [ledger.rs:L288-L319](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L288-L319)：`defer_finish` / `defer_finish_with_cause`——`close` 关户、`statement.extract` 抽走缓冲、装配 `Terminal::Finished`（token 计数出自账本流水）、连同 `tx.clone()` 打包成 `DeferredFinish`。
- [request_lifecycle.rs:L144-L171](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L144-L171)：**DeferredFinish 的 drop 炸弹**——未送出的迟到完结是持有者 bug。炸弹把终态**改写**为 `Failed`（消息 `"deferred finish dropped by the engine before delivery"`）但保留缓冲 token 照发。注释给出不能假装成功的理由：被扣住的 `Finished` 同时是 **P/D KV-ready 的屏障信号**，丢弃的把手不许伪造成功。
- [frontend_adapter.rs:L595-L608](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/frontend_adapter.rs#L595-L608)：Qwen3 的生产调用点——`executor.withholds_finishes()` 为真时把整批 finish 折成 `DeferredFinish` 交给 `release_finished_events`（P/D prefill 角色扣到 KV 保存对 peer 可见再放行），否则直接 `finish_with_cause`。

**legacy 侧对照（sink.rs）**——

- [sink.rs:L20-L40](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/sink.rs#L20-L40)：`TokenSink` 的文档把取消从「drop 每请求接收端」迁到「共享 abort reason」的设计讲透：`tx.is_closed()` 是引擎级信号（整个 demux 没了），abort reason 才是每请求信号。
- [sink.rs:L51-L69](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/sink.rs#L51-L69)：`send` 在 reason 非 `None` 时把事件原样退还（`Err`）；`is_closed` = reason 非 None **或** 共享通道关闭。
- [sink.rs:L108-L128](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/sink.rs#L108-L128)：`RequestAbortReason` 的三值 u8 编码与 Release 写 / Acquire 读。

#### 4.2.4 代码实践

**实践目标**：用账本自带的单元测试逐条验证「三件套」的关键不变式——abort 旗两种状态可见、死调度器上提交必得 Failed、retire 丢弃缓冲、deferred finish 晚到不乱序。

1. **操作步骤**：在工作区根目录运行（frontend 为 CUDA-free crate，纯 CPU）：
   ```bash
   cargo test --release -p pegainfer-frontend --lib engine::ledger
   ```
   然后对照源码阅读四个测试（均在 [ledger.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L486-L600) 的 `tests` 模块）：
   - `abort_flag_is_visible_through_both_states`（约 L567）：`control.abort()` 之后，`Queued` 与 admit 后的 `Active` 两种账户状态下 `is_aborted` 都为 true——旗挂在账户上而非状态上。
   - `submit_to_a_dead_scheduler_fails_the_request`（约 L582）：drop 掉 backend 后再 `handle.submit`，步流上收到的终态是 `Failed`——这就是信封炸弹。
   - `retire_discards_buffered_output`（约 L487）：push 过 token 再 retire，本步步流上**什么都没有**——`statement.extract` 把缓冲连同之前的 Scheduled 一起抽走。
   - `defer_finish_folds_step_output_and_delivers_late`（约 L503）：defer 后本步步流为空（记录折进了消息），随后 `deferred.send()` 才一步到位。
2. **需要观察的现象**：全部通过；四个测试各自展示一个「不证自明就会变成线上 bug」的不变式。
3. **预期结果**：`engine::ledger` 过滤下的测试全绿。测试名已从源码核对；具体运行输出**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`RequestEnvelope` 与 `DeferredFinish` 为什么都必须是 `Option<inner>` 而不是裸结构体？

**答案**：`Drop::drop(&mut self)` 只有可变引用、不能把字段 move 出来。消费路径（`consume` / `send`）需要**拿走**内值（信封要把它交给账户、迟到完结要把它发上通道），所以先 `take()` 清空 `Option` 拆掉引信；忘了消费则 drop 时发现 `Some`，就地引爆兜底。模块文档 ([request_lifecycle.rs:L19-L21](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L19-L21)) 明说这是 `Option` 存在的唯一理由。

**练习 2**：stepped 桥处理 Abort 帧时为什么「先删 stream 状态、再翻 abort 旗」，顺序不能反？

**答案**：两件事有内存序上的因果：删状态是对该请求「我不再看你的更新」的声明，`abort()` 的 Release 写必须排在这个拆除之后，调度器读到旗时（Acquire）才能确信桥侧不会再消费该请求的任何输出；反过来先翻旗，一个竞态窗口里在途 update 仍可能命中一个已半拆的状态。同时先删还有协议效果：已经上路 的 update 因找不到流表项而被直接丢弃 ([stepped.rs:L296-L305](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L296-L305) 的注释原文)。

**练习 3**：`DeferredFinish` 的 drop 炸弹为什么把终态**改写**为 `Failed`，而不是原样把缓冲的 `Finished` 发出去？

**答案**：因为被扣住的 `Finished` 身兼两职——对客户端是完结，对 P/D 流水线是「本步 KV 保存已对 peer 可见」的**屏障信号**。持有者把它弄丢了，就意味着屏障语义没人能担保；此时补发一个 `Finished` 等于伪造屏障成功，下游可能带着没就绪的 KV 继续跑。改写为 `Failed` 保留 token 流水（客户端仍拿到已生成的文本）但诚实地宣告引擎侧出了错 ([request_lifecycle.rs:L144-L148](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L144-L148))。

### 4.3 StopPolicy 与 StopCause：token 停止词汇与迟到终态的保真

#### 4.3.1 概念说明

一个生成请求除了「长度到顶」（`FinishReason::Length`）还能以「撞见停止 token」收场：模型 EOS、协议主 EOS、或请求显式列出的 stop token id。**StopPolicy** 就是这条请求的停止判定器，**StopCause** 是判定结果的类型化词汇（`Eos(u32)` / `Token(u32)`）。

它解决三件小事：

1. **归一化只做一次**：线上的 stop id 列表是调用方随手给的 Vec——可能乱序、可能重复。`StopPolicy::new` 在请求边界一次排序去重，之后所有克隆（建 plan、发 worker rank）共享 `Arc<[u32]>`，判定用二分搜索，一个大停止集不会拖慢常见的一 ID 场景。
2. **优先级即协议**：vLLM 的规则是 EOS 优先于显式停止集——同一个 token 既是 EOS 又在停止集里时，报 `Eos` 不报 `Token`。而且 vLLM 主 EOS 没有 `stop_reason`，其余匹配（包括降级进显式集的次级 EOS）要报出 token ID。
3. **迟到终态的保真**：`StopCause` 随 `Terminal::Finished.stop_cause` 上行；在 `defer_finish_with_cause` 的迟到路径里它被原样保留——完结晚到，原因不丢。

`EosPolicy` 三档决定「什么算主 EOS」：`Ignore`（不按模型 EOS 停）、`ModelDefault`（用执行器配置的 EOS 集，默认档）、`Token(u32)`（用协议给的指定 ID，次级 EOS 降级进显式集）。

#### 4.3.2 核心流程

`classify` 的判定伪代码：

```text
classify(token_id, is_model_eos):
    is_primary_eos = 按 EosPolicy 分档：
        Ignore        → false
        ModelDefault  → is_model_eos(token_id)
        Token(e)      → token_id == e
    if is_primary_eos:        return StopCause::Eos(token_id)
    elif token_id ∈ 集合:      return StopCause::Token(token_id)   # 二分查找
    else:                     return None                          # 继续
```

StopCause 从判定到上行的完整路径：

```text
调度器 step 中对新生成 token 调 policy.classify
        │ Some(cause)
        ▼
ledger.finish_with_cause(id, reason, Some(cause))          ← 即时终态
ledger.defer_finish_with_cause(id, reason, Some(cause))    ← 迟到终态（P/D）
        │ 二者都把 cause 装进 Terminal::Finished.stop_cause
        ▼
RequestUpdate（触发 token 保留在 tokens 里，附请求的 logprob）
        ▼
桥翻译成线上输出（vLLM 侧自行决定是否剔除停止 token——见 u4-l3 的 EOS 哨兵）
```

「触发 token 保留」是 step 契约的显式语义：[step.rs:L262-L264](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L262-L264) 注释原文「The triggering token remains in `RequestUpdate.tokens`, with its matching logprob when requested」——账本流水里它算数，剔除是协议栈的下游决定。

#### 4.3.3 源码精读

**EosPolicy 三档**——

- [stop.rs:L3-L14](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L3-L14)：`Ignore` / `ModelDefault`（`#[default]`）/ `Token(u32)`。

**StopPolicy 本体：归一化集合 + 共享**——

- [stop.rs:L16-L30](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L16-L30)：字段注释说明请求克隆 plan、发 worker rank 的场景，以及「大停止集二分、单 ID 常见场景不退化」的取舍。文档同时写明协议差异：vLLM 主 EOS 无 `stop_reason`，其余匹配报 token ID。

- [stop.rs:L32-L46](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L32-L46)：`new` —— `sort_unstable` + `dedup` 后装进 `Arc`。注释强调归一化只在请求边界发生一次。

**classify：优先级的实现**——

- [stop.rs:L48-L70](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L48-L70)：先按 `EosPolicy` 算主 EOS，命中即 `StopCause::Eos`；否则 `binary_search` 显式集，命中 `StopCause::Token`；都否 `None`。闭包参数 `is_model_eos: impl FnOnce(u32) -> bool` 把「模型 EOS 集」的知识留在调用方——stop 模块不需要知道任何模型细节。

**StopCause 词汇**——

- [stop.rs:L72-L79](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L72-L79)：两个变体各自带 token ID，供上游还原 `stop_reason`。

**终态上的挂点**——

- [step.rs:L256-L281](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L256-L281)：`Terminal::Finished { reason, stop_cause, prompt_tokens, completion_tokens }`。`stop_cause: Option<StopCause>` 只在 token 驱动的完结里出现；token 计数出自账本流水（结构体文档明示）。

**两条终态通道的对称性**——

- [ledger.rs:L246-L263](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L246-L263)：`finish_with_cause` 把 cause 装进即时的 `Terminal::Finished`。
- [ledger.rs:L297-L319](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L297-L319)：`defer_finish_with_cause` 同样装 cause，只是载体换成 `DeferredFinish`——**迟到不损保真**。
- [frontend_adapter.rs:L595-L608](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/frontend_adapter.rs#L595-L608)：Qwen3 适配层按 `withholds_finishes()` 在两条通道间二选一，`(request_id, reason, stop_cause)` 三元组原样传递。

**测试即规范**——

- [stop.rs:L81-L133](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L81-L133)：五个测试逐条钉住语义：默认档按模型 EOS 判 `Eos`；`Ignore` 不废除显式停止；乱序重复 id 被归一化；模型 EOS 优先于显式集；主 EOS 之外，次级模型 EOS 降级为 `Token` 停止。

#### 4.3.4 代码实践

**实践目标**：跑通 stop 模块的五个规范测试，并在纸上复算一遍优先级。

1. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-frontend --lib engine::stop
   ```
   然后不看源码，手工预测以下三例的 `classify` 结果并与 [stop.rs:L81-L133](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L81-L133) 的断言对照：
   - `StopPolicy::new(EosPolicy::Ignore, vec![7, 3, 7, 1])`，`classify(3, |_| false)` → ?
   - `StopPolicy::new(EosPolicy::ModelDefault, vec![99])`，`classify(99, |t| t == 99)` → ?
   - `StopPolicy::new(EosPolicy::Token(1), vec![1, 2])`，`classify(2, |_| true)` → ?
2. **需要观察的现象**：五个测试全绿；三例手算分别为 `Some(StopCause::Token(3))`（归一化后 `Ignore` 档下 3 仍在集合）、`Some(StopCause::Eos(99))`（模型 EOS 优先）、`Some(StopCause::Token(2))`（主 EOS 是 1，token 2 虽是模型 EOS 也降级为显式停止）。
3. **预期结果**：与源码测试断言逐一吻合。运行输出**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`StopPolicy` 为什么把 token_ids 装进 `Arc<[u32]>` 而不是 `Vec<u32>`？

**答案**：请求在构建 plan 与下发 worker rank 时会多次克隆策略；`Arc` 让这些克隆只是一次引用计数，且归一化（排序去重）只在请求边界做一次，内部副本共享同一不可变切片 ([stop.rs:L23-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L23-L29) 字段注释)。

**练习 2**：`EosPolicy::Token(1)` 且显式集含 `{1, 2}` 时，`classify(2, |_| true)` 返回 `Token(2)`——为什么 2 明明也是模型 EOS 却不算 `Eos`？

**答案**：`EosPolicy::Token(e)` 声明「本请求的主 EOS 是 e」，其余一切（包括模型默认 EOS 集的成员）都不再享有主 EOS 地位；次级 EOS 在请求整形阶段被降级进显式停止集，命中按 `Token` 上报并携带 token ID——这正是「vLLM 主 EOS 无 stop_reason、其余报 ID」协议规则的落地 ([stop.rs:L10-L13](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L10-L13) 与 [stop.rs:L125-L132](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/stop.rs#L125-L132))。

**练习 3**：`stop_cause` 与 `FinishReason` 是什么关系？

**答案**：`FinishReason` 是粗粒度完结类别（Length/Stop/Error，legacy 与新契约共用，见 u3-l2）；`stop_cause` 是 `Finished` 里对「Stop」的 token 级细化——由哪个 token、以哪种身份（主 EOS 还是显式集成员）触发。二者一起出现在 `Terminal::Finished { reason, stop_cause, .. }` ([step.rs:L260-L267](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L260-L267))。

## 5. 综合实践

把本讲三块内容串成一条可观察的链：**对 echo-server 发一个长流式请求并中途取消，对照源码指认 abort 在每一层的落点；再论证 rank 不一致为何是调用方 bug。**

### 5.1 准备与操作

1. **启动 echo-server**（无 GPU、无权重，见 [echo-server.rs:L11-L22](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L11-L22) 的文件头说明）：
   ```bash
   cargo run -p pegainfer-frontend --example echo-server
   ```
2. **发一个足够长的流式请求**（echo 调度器每个 step 只弹一个 token——[echo-server.rs:L144-L146](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L144-L146)——所以长 prompt + 大 max_tokens 会持续吐流）：
   ```bash
   curl -N http://127.0.0.1:8000/v1/chat/completions \
     -H 'content-type: application/json' \
     -d '{"model":"echo","messages":[{"role":"user","content":"<粘一段足够长的文本，比如几百字>"}],"max_tokens":2048,"stream":true}'
   ```
   （非流式用 `/v1/completions` 也行，但流式能让你看到 token 逐个到达、中断点清晰。）
3. **在流到一半时 Ctrl-C 杀掉 curl**（客户端断开）。

### 5.2 对照代码回答三问

**问一：abort 状态在哪一层被置位？**

- **step 契约侧**：客户端断开 → 上游 vLLM HTTP 层向引擎发 `Abort` 帧 → stepped 桥**先删自己的流表项、再** `state.control.abort()`（[stepped.rs:L292-L308](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L292-L308)），即 [request_lifecycle.rs:L195-L197](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/request_lifecycle.rs#L195-L197) 的 `Release` 写。echo 走的是 step 契约（`LaunchedEngine::Stepped`），所以本例**不经过** `TokenSink`。
- **legacy 契约侧**（若换成 deepseek-v2-lite / glm52 / kimi-k2）：同一断开会被前端写进 `TokenSink` 的 abort reason（`Cancelled` 或 `Disconnected`，[sink.rs:L108-L128](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/sink.rs#L108-L128)），调度器在下一次 `send` 返回 `Err` 或 `is_closed()` 为真时退役（[sink.rs:L51-L69](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/sink.rs#L51-L69)）。step 侧的等价物是账本的 `is_aborted`（[ledger.rs:L133-L138](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L133-L138)）。

**问二：调度器在哪个方法里 retire？**

- `EchoScheduler::step` 的两处探针：接纳循环入口 [echo-server.rs:L122-L126](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L122-L126) 与每个 running 请求每步开头 [echo-server.rs:L139-L143](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L139-L143)；真正关户的是 [ledger.rs:L279-L286](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/ledger.rs#L279-L286) 的 `RequestLedger::retire`——静默关户并 `statement.extract` 丢弃本步缓冲。可观察现象：流**静默地停止**（不再有 chunk，也没有错误 chunk）；若想看 `request retired: frontend aborted: ...` 的 debug 日志需调高日志级别，echo-server 是否接通该日志**待本地验证**。

**问三：论证「已解析前缀与调用方绑定 rank 不一致」为什么是调用方 bug 而非引擎故障。**

论据链（全部可在源码中指认）：

1. **hold 把块钉在解析时的 rank 上**：[kv.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/kv.rs#L25-L29)——解析一旦发生，命中的 KV 块被 pin 在那个分区；引擎没有「把钉住的块搬到别的 rank」的机制。
2. **所以 resolved rank 是路由的硬事实**：[handle.rs:L213-L225](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L213-L225) 里 `(Some(resolved), _)` 分支无条件取 `resolved`。
3. **不一致只可能是调用方制造的**：`data_parallel_rank`（调用方绑定）与 `KvPrefix::resolved(..., rank, ...)`（store 解析）是**同一个调用方**先后写进请求的两份信息；引擎只做拼接。二者打架，说明调用方在 A 分区上做了解析、又把请求绑到 B 分区——这是调用方自己流程里的自相矛盾，引擎既无法修复（不能搬块）也不该替它选边（选哪边都丢另一半事实）。
4. **所以它是契约违规而非运行时故障**：用 `debug_assert!`（只在 debug 构建检查）而非 `Err`——生产路径按 resolved 路由（保住 KV 命中这个更贵的事实），违规只在开发期炸出来提醒修调用方。这与 u2-l3 的分工一致：运行期 `Err` 管「用户命令错」，`debug_assert` 管「调用方代码错」。

### 5.3 预期结果

- Ctrl-C 后流静默终止；服务端不崩、可继续服务新请求（retire 是每请求的，不触碰其他账户）。
- 三问的每个落点都能用本讲给出的行号指认到具体代码。
- echo-server 的完整交互（含流式行为）**待本地验证**；代码路径本身已全部核对到 HEAD `a474b264`。

## 6. 本讲小结

- **KvPrefix** 三成员各司其职：`hit_tokens` 让退化解析退化为「一个更小的数」（数字承载全部下游语义）；`rank` 是路由的硬事实（hold 把块钉在那个分区上）；`hold` 是全契约只能 drop 的不透明 RAII 把手，连 `Debug` 都只显示 `<opaque>`。
- **`submit_resolved` 的路由决策树**：已解析 rank 无条件取胜（不一致触发 `debug_assert`，判调用方 bug）→ 调用方绑定 rank → 最低负载（\( \text{running} + 4 \times \text{waiting} \)）；越界 rank 以 `Scheduled → Rejected` 应答而非报错。
- **应答到底三件套**覆盖账本外的三段窗口：信封 drop 炸弹（调度器死后新提交仍得 `Failed`）、`RequestControl` 原子 abort 旗（Release 写 / Acquire 读，不关通道）、`DeferredFinish`（账本关户折档、任意线程晚发不乱序，drop 时改写为 `Failed`——被扣的 `Finished` 是 P/D 屏障信号，不许伪造成功）。
- **abort 是反应式的**：前端只翻旗，调度器在下一个 step 触碰时 `is_aborted → retire`，本步缓冲整条抽走、静默无人寻址；stepped 桥先删流表项再置旗，在途 update 自然落空。
- **StopPolicy/StopCause** 是 token 停止的归一化词汇：请求边界一次排序去重进 `Arc<[u32]>`，classify 按 vLLM 优先级（主 EOS > 显式集）判定；`StopCause` 随 `Terminal::Finished` 上行，在 `defer_finish_with_cause` 的迟到终态里原样保真。

## 7. 下一步学习建议

- **u5-l2（从零接入一条新模型线）**：本讲的 `EchoScheduler` 已是你见过的第二个完整调度器；u5-l2 把它扩成接入清单——probe、注册、feature、检测门测试。
- **u5-l3（设计取舍复盘）**：本讲两处「谁该对错误负责」的裁决（rank 不一致归调用方、deferred 丢失改写为 Failed）是契约作者价值观的直接体现，u5-l3 系统复盘这类取舍。
- 继续阅读：`pegainfer-qwen3/src/frontend_adapter.rs` 全文（本讲只取了 `defer_finish_with_cause` 一处，它是「调度器义务如何映射到账本方法」的最完整范本）；以及 `pegainfer-kv-offload`（hold 的铸造方在真实 store 里长什么样，可对照 `hold_any` 的 downcast 用法）。
