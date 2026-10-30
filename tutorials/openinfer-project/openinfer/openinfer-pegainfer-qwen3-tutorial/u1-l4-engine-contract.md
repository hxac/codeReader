# u1-l4 引擎契约与启动装配：frontend_adapter 全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **Engine**、**Scheduler**、**RequestLedger** 三个契约角色各自的职责边界——谁拥有请求状态、谁驱动步进、谁记录裁定。
2. 完整追踪一次引擎启动的调用链：`Qwen3Line::launch` → `crate::launch` → `start_engine_with_offload_inner` / `start_engine_with_lora_control` → `frontend_adapter::start_qwen3` → `start_with_executor` → `spawn_scheduler`。
3. 解释 executor 构建之后 `set_no_prefix_cache` / `enable_decode_overlap` / `load_draft_model(DraftModel)` 三步配置的**顺序为什么不能乱**。
4. 说出 `launch` 如何把 DFlash 与 EAGLE-3 两条草稿路径折叠成一个 `Option<DraftModel>`，以及**为什么两种草稿器不能同时装载**。
5. 读懂 `Qwen3Scheduler::step` 的 0-8 编号九段式结构，知道每一段在做什么（为 u4 的调度器三讲铺路）。

本讲是 u1（起步单元）的收官：u1-l3 讲了 Qwen3 如何通过 ModelLine 挂进 CLI（包括本周期新增的 `--eagle3-draft-model-path`），本讲接着回答「`crate::launch` 被调用之后发生了什么」，把服务器进程与 GPU 执行器之间的最后一层接缝讲透。

## 2. 前置知识

### 2.1 什么是「引擎契约」

PegaInfer 是多模型线架构：服务器二进制（通过 `ModelLine` 派发，见 u1-l3）不认识任何具体模型的类型。为了让「服务器」和「Qwen3 执行器」能对接，`pegainfer-frontend` crate 定义了一组**双方都只依赖它**的类型与 trait，这就是**引擎契约**（engine contract）。模型 crate 实现契约，服务器消费契约——于是服务器做纯派发，模型可以自由演化内部实现。

三个核心角色（先给直觉版，源码证据在 4.1）：

| 角色 | 直觉 | 一句话职责 |
|---|---|---|
| `Engine` | 交给服务器的「整车」 | 一组调度器线程句柄 + 对外元数据（KV 容量、可服务长度）+ 可选的 LoRA 控制客户端 |
| `Scheduler` | 车上的「发动机循环」 | 实现 `submit` / `step` / `metrics` 三个方法，由契约的驱动线程（driver）反复轮询 |
| `RequestLedger` | 发动机旁的「记账本」 | 按 `RequestId` 记录每个请求的裁定（admit/reject/retire/fail/finish）与产出的 token，前端据此外露事件 |

> **术语**：ledger 直译「账本」。每个请求从进入队列到结束，账本上有一格账户；调度器每步把结果「写进账本」，前端读账本生成响应。这个比喻贯穿本讲。

### 2.2 「步进式」引擎与 plan/result 边界

回顾 u1-l1 引入的概念：本 crate 的公共执行边界是 `runtime` 再导出层——`Qwen3Executor` 与三对计划/结果类型（`PrefillPlan`/`PrefillResult`、`DecodePlan`/`DecodeResult`、`UnifiedPlan`/`UnifiedResult`）。它们是**纯数据**：计划里只有要做的事，结果里只有做完的事，谁都不持有引擎句柄。调度器因此可以在纯函数层面决定「这一步做什么」（计划），再把计划交给持有 GPU 的 executor 执行。

### 2.3 本讲需要的前置

- 已读 u1-l3（`Qwen3Line::launch` 如何组装 `Qwen3LaunchOptions`；DFlash 与 EAGLE-3 两个草稿 flag 的互斥表）。
- 知道 prefill（整段提示词前向）与 decode（每请求单 token 批量前向）两阶段的区别（u1-l1）。
- 知道 `--lib` 单测不需要 GPU（u1-l2）——本讲实践全部是源码阅读型，无需 GPU。
- 知道「投机解码 = 用便宜草稿器提案、目标模型一次前向验证」的粗概念即可——本讲只关心它的**装载**，事务机制留给 u6。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `src/frontend_adapter.rs` | 契约适配层：Qwen3 与引擎契约的全部接缝，一个文件写完 | 模块文档、`start_qwen3`、`start_with_executor`、`Qwen3Scheduler` 的 `Scheduler` 实现 |
| `src/lib.rs` | crate 根：`Qwen3LaunchOptions`、`launch` 与各 `start_engine*` 入口、`runtime` 再导出 | `launch` 的策略分叉与草稿器折叠、`start_engine_with_offload_inner` |
| `src/speculative.rs` | 投机解码事务核心（u6-l1 深入） | 本讲只取 `DraftModel` 枚举——装载层「二选一」的类型化表达 |
| `src/model_line.rs` | ModelLine 实现（u1-l3 已讲） | 仅引用 `launch` 调用点，标出链条最上游 |
| `src/executor.rs` | GPU 执行器（u3 深入） | `ModelExecutor` trait、三个构建后配置方法、草稿器显存预留 |
| `src/scheduler.rs` / `src/scheduler/plan.rs` | 纯调度层（u4 深入） | `servable_len`、`runtime_plan`、`execute_plan` 的衔接点 |

> **注意**：`pegainfer-frontend` 是工作区里的独立 crate（源码在仓库的 `pegainfer-frontend/src/engine/` 下），而本讲义的永久链接 base 只覆盖 `pegainfer-qwen3/`，因此契约类型的定义处**不给永久链接**；本讲全部从 Qwen3 这一**消费侧**的 import、调用点与构造点来还原契约面。持有完整仓库检出的读者可以自行到 `pegainfer-frontend/src/engine/` 对照；凡是只涉及 frontend crate 内部实现细节的论断，都以「消费侧可见」为准。

## 4. 核心概念与源码讲解

本讲的三个最小模块——`launch`、`start_qwen3`、`start_with_executor`——正好是启动链条上的三站。在此之前先用 4.1 把契约本身立起来。

### 4.1 引擎契约三件套：Engine / Scheduler / RequestLedger

#### 4.1.1 概念说明

为什么需要契约层？因为服务器要同时面对多条模型线。若无契约，服务器就得 `match` 每个模型的具体类型——每加一条模型线都要改服务器。有了契约，服务器只问三件事：

- 「给我一个 `Engine`」（启动时，一次性）；
- 「请求来了，收下」（`submit`）；
- 「现在什么状态了？」（`step` 推进一步并写账本；`metrics` 报数）。

Qwen3 侧则把内部世界（KV 池、CUDA 图、投机草稿器……）全部藏在 `Qwen3Executor` 后面，对外只讲契约的语言：**纯 effect 数据 + 账本写入**。

#### 4.1.2 核心流程

驱动线程（由 `spawn_scheduler` 创建）的稳态循环，从 Qwen3 实现侧还原如下：

```text
loop {
    从请求通道取出新请求  → scheduler.submit(QueuedRequest)   // 入队，不碰 GPU
    scheduler.step(&mut ledger)?                                // 一次完整调度步
    //   step 内部：队列维护 → 选计划 → executor 执行 → resolve 成 StepEffects
    //             → apply_effects 把 effects 翻译成 ledger 写入
    metrics = scheduler.metrics()                               // KV 占用/等待数等
    // 前端从 ledger 读取每个请求的新 token / 裁定，生成对外事件
}
```

要点：

- `submit` 只是把请求放进 Qwen3 自己的队列（`deferred`），**不做任何 GPU 工作**；真正的工作都发生在 `step`。
- `step` 拿到的是 `&mut RequestLedger`——账本由**驱动侧**拥有并借给调度器写；Qwen3 调度器自己不保存对外事件队列，这是「契约归契约、机制归机制」的关键设计。
- `metrics` 是纯读，返回 `SchedulerMetrics` 快照。

#### 4.1.3 源码精读

**① 模块文档：这个文件自己声明了两项契约职责**。[src/frontend_adapter.rs:1-L22](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L1-L22) 说：`Qwen3Scheduler` 实现 `pegainfer_frontend::engine::Scheduler`；驱动轮询 `submit`/`step`/`metrics`；此文件只拥有两个面向契约的职责——**ledger 写入**（把每步 effects 翻译成按 id 记录的裁定与 token）和**引擎装配**（构建 executor 并从 `launch` 返回 `Engine`）。取消是响应式的：前端翻转请求的 abort 标志，适配器在下步顶部退休它，调度机制永远看不到 abort。

**② import 清单即「契约面清单」**。[src/frontend_adapter.rs:33-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L33-L48) 从 `pegainfer_frontend::engine` 引入的全部类型就是 Qwen3 实际用到的契约面：`Engine`、`EngineInfo`、`KvCapacity`、`QueuedRequest`、`RequestLedger`、`Scheduler`、`SchedulerMetrics`、`RejectReason`、`FinishReason`、`StopCause`、`DeferredFinish`、`PromptEcho`、`LoraClient`/`LoraControl`/`LoraControlReceiver`、`spawn_scheduler`。（紧随其后的 [src/frontend_adapter.rs:83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L83) 引入的 `crate::speculative::DraftModel` 则是 crate 内部类型——见 4.2。）

**③ `Scheduler` 的实现签名**。[src/frontend_adapter.rs:619-L631](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L619-L631) 是 `impl<E: ModelExecutor> Scheduler for Qwen3Scheduler<E>` 的开头——`submit` 把 `QueuedRequest` 拆成契约给的 `id` 与请求体，记入队列追踪（`tracker.enter_queue`），转成内部 `PendingRequest` 后推进 `deferred` 队列；若此刻有待处理的 LoRA 控制命令，则先押进 `post_control_deferred`（控制命令必须先于后来者生效）。

**④ 账本写入长什么样**。[src/frontend_adapter.rs:400-L421](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L400-L421) 的 `apply_effects` 是唯一的「翻译器」：先把本步的前缀缓存计数累进单调总量（L412-L413），再依次处理 `cached`（写 `ledger.set_cached_tokens`）、`prompt_echoes`（写 `ledger.echo_prompt`）、`decode`/`pending` 两族 effect（写 `ledger.push_tokens`、收集 finish），最后统一结算。Qwen3 侧可见的账本方法全集（消费侧证据）：`admit` / `reject` / `retire` / `fail` / `is_active` / `is_aborted` / `push_tokens` / `set_cached_tokens` / `echo_prompt` / `finish_with_cause` / `defer_finish_with_cause`——共 11 个。

**⑤ 完成事件的两种出口**。[src/frontend_adapter.rs:595-L609](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L595-L609)：普通执行器直接 `ledger.finish_with_cause` 让终止事件随本步提交；**扣留型**执行器（`executor.withholds_finishes()` 为真，即 P/D prefill 角色）则用 `ledger.defer_finish_with_cause` 铸出 `DeferredFinish` 令牌交给 `executor.release_finished_events`，等 KV 保存对同伴可见后再投递（投递函数见 [src/executor.rs:941-L945](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L941-L945) 的 `send_finished_events`）。每个 `DeferredFinish` 携带该请求的完整最终记录，所以晚投递不会乱序。

**⑥ `metrics`：路由器想看的稳态负载**。[src/frontend_adapter.rs:832-L866](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L832-L866) 汇报：`kv_used_blocks`（总数减空闲，步间稳态而非步内峰值）、`num_running_reqs`（active + prefilling + 在途异步 prefill）、`num_waiting_reqs`（deferred + loading + post_control 三个未运行队列之和）、投机解码计数器，以及前缀缓存计数（本调度器的累计查询/命中 + 执行器侧 connector 查询的合并，见注释里 local/external 的归并规则）。

#### 4.1.4 代码实践

**实践目标**：不用读 frontend crate 源码，仅凭消费侧调用点画出「契约面使用图」。

1. 运行：
   ```bash
   rg -n "ledger\." src/frontend_adapter.rs
   ```
2. 把输出整理成一张表：`账本方法 | 调用行号 | 所在 effect 分支 | 语义`（例如 `push_tokens` 出现在 `DecodeEffect::Continue` / `Finish` / `FinishMany` 与 `PendingEffect::Promote` / `EmitAndFinish`）。
3. 再运行 `rg -n "is_aborted" src/frontend_adapter.rs`，数一数 abort 探测点出现在哪些阶段——体会「取消是响应式的」这句话落在多少个检查点上。

**需要观察的现象**：所有账本写入都发生在 `apply_effects`（L400）、`admit_or_retire`（L302）、`reject_pending`（L312）、`retire_aborted`/`retire_aborted_waiting`（L324/L336）、`fail_touched_requests`（L358）这几个函数里，`step` 的调度逻辑（选计划、装箱）本身零账本写入——读写分离。

**预期结果**：得到一张 11 个账本方法的清单表；能指出 `finish_with_cause` 与 `defer_finish_with_cause` 只在 [src/frontend_adapter.rs:595-L609](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L595-L609) 一处按 `withholds_finishes()` 二选一。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Qwen3Scheduler` 自己不维护「要发给前端的 token 队列」，而要借 `RequestLedger` 写入？
**答案**：对外事件的记账是契约的职责（前端按 id 读账本生成响应）；调度器若自持队列，就必须同时实现一套与契约对齐的事件语义，且 decode-overlap 等路径下队列克隆/重放容易出错。账本由驱动侧拥有、借给 `step` 写入，保证每请求单一事实源，调度机制保持「纯 effect 输出」（这正是 u4-l3 要展开的 sink-free 设计）。

**练习 2**：`submit` 里 `PendingRequest::from_request(id, request)` 的 `id` 从哪来？
**答案**：来自契约的 `QueuedRequest`——`let QueuedRequest { id, request } = request;`（[src/frontend_adapter.rs:623](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L623)）。`id` 就是贯穿全链路的 `RequestId`，账本、effect、executor 结果全部按它索引。

**练习 3**：`DeferredFinish` 解决什么问题？
**答案**：P/D prefill 角色要求「HTTP 响应即 KV-ready 信号」——`Finished` 事件必须等到 KV 保存 + MetaServer 注册对同伴可见后才能发出。扣留型执行器把 finish 铸成携带完整最终记录的 `DeferredFinish` 令牌，在步外投递，调度线程从不等待 flush 屏障（默认 trait 方法与文档见 [src/executor.rs:903-L923](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L903-L923)）。

### 4.2 launch：启动策略的唯一拥有者（lib.rs）

#### 4.2.1 概念说明

`launch` 是 crate 对外的**策略入口**：吃进服务器翻译好的 `Qwen3LaunchOptions`（[src/lib.rs:235-L262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L235-L262)，字段含 `device_ordinal`、`tp_size`、`cuda_graph`、`dump_graph_png`、`offload`、`no_prefix_cache`、`max_prefill_tokens`、`memory`、`lora`、`decode_overlap`、`batch_invariant`、`dflash_draft_model_path` 与**新增的 `eagle3_draft_model_path`**），吐出一个 `Engine`。它的文档（[src/lib.rs:224-L229](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L224-L229)）点明设计意图：**启动策略跟着模型走，不漏进服务器**——TP→设备的映射、LoRA↔CUDA-Graph 互斥这些「为什么」只有 Qwen3 自己知道。

本周期的一个关键变化：两条草稿路径不再各自为政，而是在 `launch` 里被**折叠**成一个 `Option<DraftModel>`。`DraftModel` 是个两变体枚举（[src/speculative.rs:33-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L33-L38)）：

```rust
/// The drafter a speculative engine loads, by checkpoint path.
pub(crate) enum DraftModel<'a> {
    DFlash(&'a str),
    Eagle3(&'a str),
}
```

它按 checkpoint 路径指认草稿器。下游（构建函数、装配函数、executor）从此只见「至多一个草稿器」，不再关心它来自哪个 CLI flag。

#### 4.2.2 核心流程

```text
launch(model_path, options)
├── 1. 推导 device_ordinals        // tp=1 → [device_ordinal]；否则 0..tp
├── 2. LoRA 强制关 CUDA Graph       // 图会烘焙适配器指针（u7 详述）
├── 3. dump_graph_png 三重校验      // 需图开启 + 无 LoRA + 路径可写
├── 4. 组装 EngineLoadOptions       // 契约侧的加载选项（seed=42 等）
├── 5. overlap×TP 提前拒绝
├── 6. 草稿器折叠：两条路径 → Option<DraftModel>
│       // (Some, Some) → bail；恰好一条 → DraftModel::DFlash / ::Eagle3
├── 7. 两条投机提前拒绝             // draft×LoRA、draft×decode-overlap
└── 8. match options.lora
    ├── Some → start_engine_with_lora_control   // LoRA 分支
    └── None → start_engine_with_offload_inner  // 主分支（收 draft + dump_graph_png）
```

「失败越早越便宜」是 u1-l3 建立的主题在 lib.rs 的延续：第 5 步的检查在 executor.rs 里有**承重副本**（如 `enable_decode_overlap` 内部的 TP 拒绝），这里提前再查一遍是为了省掉整次模型装载 + 图预捕获的开销后才报错——见 [src/lib.rs:310-L311](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L310-L311) 的注释。

#### 4.2.3 源码精读

**① 入口与设备映射**。[src/lib.rs:270-L274](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L270-L274)：单卡用 `options.device_ordinal`；TP>1 固定占用 `0..tp_size`。

**② LoRA 关图**。[src/lib.rs:275-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L275-L284)：LoRA 服务会在步与步之间重指适配器权重，而捕获好的 decode 图把旧指针烘焙在内，所以强制 `enable_cuda_graph=false` 并 `warn!` 提示（用户显式要图时才有日志）。

**③ dump_graph_png 校验**。[src/lib.rs:285-L295](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L285-L295)：`ensure!`（图必须开启）、`ensure!`（不能与 LoRA 同用）、`pegainfer_core::cuda_graph::validate_graph_dump_request(path)?`（路径可写性等，实现在 core crate）。注意 `dump_graph_png` **只走主分支**——LoRA 分支的调用（L343-L353）根本不传这个参数。

**④ 契约侧加载选项**。[src/lib.rs:296-L302](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L296-L302) 构造 `EngineLoadOptions { enable_cuda_graph, device_ordinals, parallel_config: None, ep_backend: Nccl, seed: 42 }`——这是契约定义的通用加载参数包，Qwen3 模型线当前固定 seed 为 42。

**⑤ 草稿器折叠（本讲新看点）**。[src/lib.rs:316-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L316-L326) 用一个四臂 `match` 把 `(dflash_draft_model_path, eagle3_draft_model_path)` 折成 `Option<DraftModel>`：

- `(Some, Some)` → `bail!("DFlash and EAGLE-3 drafters cannot be loaded together")`——**两种草稿器互斥的第一道闸**；
- `(Some(path), None)` → `Some(DraftModel::DFlash(draft_path(path)?))`；
- `(None, Some(path))` → `Some(DraftModel::Eagle3(draft_path(path)?))`；
- `(None, None)` → `None`。

`draft_path`（[src/lib.rs:370-L373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L370-L373)）是新抽出的小助手：把 `&Path` 校验成 `&str`（非 UTF-8 即报错）。这段校验原来长在 `start_engine_with_offload_inner` 里、只服务 DFlash；现在上移到折叠点，两种草稿器共用。

**为什么 DFlash 与 EAGLE-3 不能同时装载？** 三层理由，一层比一层靠近机制：

1. **类型层**：折叠结果 `Option<DraftModel>` 只有一个槽——下游所有签名（`start_qwen3` 的 `draft` 参数、`load_draft_model`）都按「至多一个草稿器」设计。
2. **状态层**：executor 的投机状态是单个 `Option<DraftMeta>` 槽位——`load_draft_model` 末尾 `self.speculative = Some(meta)` 是覆盖式写入（[src/executor.rs:1824-L1825](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1824-L1825)），投机计数器 `spec_decode_counters` 也只有一份；装第二个会静默顶掉第一个的元数据。
3. **机制层**：两种草稿器**共享同一条目标隐藏态捕获管线**——[src/speculative.rs:13-L17](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L13-L17) 的模块文档明说「两个 proposer 都从 prefill/verify 捕获的目标隐藏态出发，共享捕获、ready 集与事务，只有把捕获变成草稿的 worker 车道不同」。共享的前提是当前恰有一个 proposer；显存预算（见 4.3）也只按一个草稿器预留。此外 CLI 层早有第三道闸：`--eagle3-draft-model-path` 与 `--dflash-draft-model-path` 组合在 [src/model_line.rs:260-L278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L260-L278) 直接拒绝。

**⑥ 两条投机提前拒绝**。[src/lib.rs:327-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L327-L336)：折好的 `draft.is_some()` 统一替代了原先的 DFlash 判空，错误信息也从「DFlash speculative decoding cannot…」推广为「**speculative decoding** cannot be combined with LoRA serving / decode overlap」——规则本身没变，覆盖面从「DFlash」扩成「任何草稿器」。decode-overlap 拒绝的理由文本原样保留：投机路径永不走统一 overlap 路线，两条流只会白占草稿器需要的显存。

**⑦ 分叉点**。[src/lib.rs:337-L367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L337-L367)：`match options.lora` 是启动期最大的分叉——`Some(lora)` 走 LoRA 控制（热加载通道模式，见 4.3），`None` 走主分支并把折好的 `draft`（L364）与 `dump_graph_png`（L365）一并传下。

**⑧ 主分支内部**（`start_engine_with_offload_inner`，[src/lib.rs:431-L476](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L431-L476)）：
- L443 先 `crate::spec_env::validate()`——投机解码环境旋钮非法就在任何 GPU 工作前失败（u6-l7 详述）；
- L444-L449 从 `EngineLoadOptions` 解构出 `enable_cuda_graph` / `device_ordinals` / `seed`；
- L453-L461 `batch_invariant` 时调 `ensure_batch_invariant_supported`——注意第 4 个参数现在是 `draft.is_some()`，在函数内对应改名为 `speculative`（[src/lib.rs:536-L566](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L536-L566)），即 batch-invariant 对**任何**草稿器（DFlash 与 EAGLE-3 同遭拒绝）都说不行，u8-l1 逐条分析五条拒绝；
- L462 `apply_batch_invariant_policy` 设全局 `NumericPolicy`（[src/lib.rs:524-L534](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L524-L534)）——注意它必须在 executor 构建之前执行，因为构建期的 GEMM 调参/图捕获行为依赖这个全局策略；
- L463-L475 调 `frontend_adapter::start_qwen3`——链条进入下一站。原先夹在这两步之间的草稿路径 UTF-8 校验已上移到 ⑤ 的 `draft_path`。

LoRA 分支（[src/lib.rs:478-L522](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L478-L522)）结构相同但更严：`batch_invariant` 直接 `bail!`（rank-K 适配器形状未经验证，L499-L505）、decode-overlap 必须 `Off`（L506-L509），然后调 `frontend_adapter::start_qwen3_with_lora_control`。

**⑨ 更低层的公共入口**。`start_engine`（[src/lib.rs:375-L387](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L375-L387)）与 `start_engine_with_offload`（[src/lib.rs:402-L428](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L402-L428)）是不经 `Qwen3LaunchOptions` 的便捷入口（后者是前者的 offload 版，默认参数 + `dump_graph_png=None`），主要服务测试与基准；服务器路径始终走 `launch`。注意它俩的签名**仍只收 `dflash_draft_model_path`**，内部经 `draft_path` 映射成 `DraftModel::DFlash`（L422-L425）——EAGLE-3 目前只能经 `launch`/`Qwen3LaunchOptions` 装载，这是「新枚举已立、便捷入口未跟」的现状，读代码时别误以为便捷入口能装 EAGLE-3。

#### 4.2.4 代码实践

**实践目标**：把 `launch` 的全部守卫整理成一张「检查 × 条件 × 错误信息 × 承重副本位置」表。

1. 运行：
   ```bash
   rg -n "ensure!|bail!|warn!" src/lib.rs | sed -n '1,60p'
   ```
2. 只挑 `launch` / `start_engine_with_offload_inner` / `start_engine_with_lora_control` / `ensure_batch_invariant_supported` 四个函数里的命中项制表。
3. 对每条检查，用 `rg -n "关键词" src/executor.rs` 找它的承重副本（例：「decode-overlap is unsupported under tensor parallelism」在 [src/executor.rs:1754-L1757](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1754-L1757) 还有一份；「speculative decoding requires the single-GPU path」在 [src/executor.rs:1423-L1427](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1423-L1427) 与 [src/executor.rs:1794-L1798](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1794-L1798) 各有一份）。

**需要观察的现象**：同一条规则往往出现两处——外层早失败（省装载时间）、内层承重（防绕过）。两条信息文本几乎一致，方便搜索配对；DFlash/EAGLE-3 互斥是个例外：它的闸门在 CLI validate 与 launch 折叠两处，executor 侧不再重复（`Option<DraftModel>` 类型已保证）。

**预期结果**：至少 10 行的守卫表（含新加入的「DFlash×EAGLE-3 互斥」）；能回答「如果绕过 CLI 直接用 `start_engine_with_offload` 传非法组合，哪条检查还会兜底」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LoRA 分支收不到 `dump_graph_png`？
**答案**：因为分叉发生在 `match options.lora`（[src/lib.rs:337-L353](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L337-L353)），LoRA 臂的调用参数表里没有这一项；且 `launch` 早在 L290-L293 就 `ensure!` 过「图导出不能与 LoRA 服务同用」，所以传了也会在更早处被拒。

**练习 2**：`apply_batch_invariant_policy` 为什么放在 `start_qwen3` 之前而不是之后？
**答案**：它设置进程级全局 `NumericPolicy`（Pin/Tuned）。executor 构建过程（GEMM 算法预热、图捕获、Pin 包络自检）的行为由这个全局策略决定；放在构建后就晚了——图已经按 Tuned 捕获完了。见 [src/lib.rs:453-L462](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L453-L462) 的顺序。

**练习 3**：`launch` 的种子固定为 42，这影响什么？
**答案**：`EngineLoadOptions.seed` 最终传给 `start_with_executor`，用于 `StdRng::seed_from_u64`（[src/frontend_adapter.rs:272](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L272)），即调度器采样 RNG 的根种子；请求级采样种子由每步计划从该 RNG 派生（u4-l2 详述）。

**练习 4**：为什么把「草稿路径必须是合法 UTF-8」的检查从 `start_engine_with_offload_inner` 上移到 `launch` 的折叠点？
**答案**：上移之后它对两种草稿器（DFlash/EAGLE-3）都生效，而且失败更早——坏路径在任何 spec_env 校验、batch-invariant 门之前就被拒绝；同时 `start_engine_with_offload_inner` 的签名从 `Option<&Path>` 变成 `Option<DraftModel>`，装载意图在类型上就已经定型，内层不再需要重复解释路径语义（[src/lib.rs:316-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L316-L326) 与 [src/lib.rs:370-L373](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L370-L373)）。

### 4.3 start_qwen3：构建 executor 与构建后配置的顺序（frontend_adapter.rs）

#### 4.3.1 概念说明

`start_qwen3` 是契约适配层的**主装配函数**：把十来个扁平参数（其中草稿器参数现在是 `Option<DraftModel<'_>>`）变成一个配置完毕的 `Qwen3Executor`，再交给 `start_with_executor` 包装成 `Engine`。它表面上只是「new 一个 executor 再拧三个旋钮」，真正的学问在**旋钮的顺序**：三个构建后配置——`set_no_prefix_cache`、`enable_decode_overlap`、`load_draft_model`——两两之间有依赖，乱序会静默破坏语义。

#### 4.3.2 核心流程

```text
start_qwen3(...)
├── 1. Qwen3Executor::from_runtime_with_decode_environment(...)  // 构建目标模型
│      ├── 校验 memory/lora 选项、设备数、offload 单卡约束、PerToken 约束
│      ├── （DFlash 时）先 ensure_servable 再装载目标 —— 坏草稿配置自己报错
│      ├── 装载权重 + 显存画像（profile）→ 推导 KV 块预算
│      └── （有草稿时）按 DraftModel 两臂预留显存：固定字节进 margin，按 token 字节进块预算
├── 2. dump_graph_png（可选）：导出 rank-0 batch-1 split-KV decode 图
├── 3. set_no_prefix_cache(no_prefix_cache)                       // 旋钮①
├── 4. enable_decode_overlap(decode_overlap)?                     // 旋钮②（建第二条流）
├── 5. load_draft_model(draft)?（可选）                            // 旋钮③（最后；按 DraftModel 分派）
└── 6. start_with_executor(executor, seed, max_prefill_tokens, false)
```

顺序的三条理由（对应源码注释 [src/frontend_adapter.rs:126-L129](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L126-L129)）：

1. **草稿模型建立在目标之上**——DFlash 与 EAGLE-3 都要用目标的 embedding/lm_head 与隐藏态捕获（EAGLE-3 的构造函数就叫 `from_safetensors_for_target`），只能等目标 executor 起来之后装载；
2. **装载草稿器会强制关闭前缀缓存**（`load_draft_model` 末尾显式 `set_prefix_cache_enabled(false)`），所以它必须排在 `set_no_prefix_cache` **之后**——若顺序颠倒，后执行的 `set_no_prefix_cache(false)` 会把草稿器刚关掉的缓存又打开（最后写入者胜）；
3. **草稿的显存足迹在构建期已预留**——`draft` 在第 1 步就传进了构建函数，画像阶段据此扩 margin、缩块预算，所以第 5 步装载时不会溢出。

#### 4.3.3 源码精读

**① 构建调用**。[src/frontend_adapter.rs:102-L112](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L102-L112) 调 `Qwen3Executor::from_runtime_with_decode_environment`，把模型路径、图开关、设备表、LoRA 选项（此处传 default，热加载模式才传真配置）、offload、`max_prefill_tokens`、**折好的 `draft`**、内存选项、overlap 模式全部交出。构建函数开头（[src/executor.rs:1286-L1317](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1286-L1317)）能看到「坏草稿配置先于目标装载被拒」——但注意它**只对 DFlash 做**：`if let Some(DraftModel::DFlash(path)) = draft { DFlashConfig::from_file(path)?.ensure_servable()? }`（L1315-L1317），注释明言这是为了让 DSpine 配置以自己的理由失败，而不是在 KV 尺寸推导时炸出费解错误。EAGLE-3 没有等价预检，坏配置要到下一站的 `Eagle3MemoryReservation::from_path` 才报错。

**② 草稿显存预留（两臂）**。[src/executor.rs:1354-L1404](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1354-L1404)：草稿模型在画像之后装载、却住在分页 KV 池之外，所以构建期就从草稿配置**预先**算显存预留，`match draft` 分两臂：
- `DFlash` 臂（L1356-L1390）：`DFlashMemoryReservation::from_path` 算出固定字节（权重 + 块 scratch）加进 `kv_cache_memory_margin_bytes`；另按 `PEGAINFER_SPEC_HEDGE` 算 hedge 链 scratch 页数——仅 Markov 头配置下非零；
- `Eagle3` 臂（L1391-L1402，新增）：`Eagle3MemoryReservation::from_path`（吃 `max_prefill_tokens` 与 `EAGLE3_MAX_BATCH`）同样把 `fixed_bytes` 折进 margin，`kv_bytes_per_token` 折进块预算，无 hedge 页。

这解释了 4.3.2 的理由 3。TP 路径则干脆拒绝草稿器（[src/executor.rs:1423-L1427](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1423-L1427)：speculative decoding requires the single-GPU path）。

**③ 图导出**。[src/frontend_adapter.rs:113-L123](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L113-L123)：若有 `dump_graph_png`，立即导出活动图的摘要（节点数/内核数/边数 + `.dot`/`.png` 路径）并 `info!`。导出用的图是构建期捕获好的，所以放在构建后立刻做；u3-l5 会沿 `dump_decode_graph_png`（[src/executor.rs:1120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1120)）展开整条链。

**④ 旋钮①：`set_no_prefix_cache`**。执行器侧实现在 [src/executor.rs:1776-L1784](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1776-L1784)，语义分两支：无 offload 时经典地整体关闭前缀匹配；有 offload 时变成「纯 L2 模式」——匹配保持开（host 层恢复依赖它拾起块），但停止在 HBM 保留已完成块，逼每次复用走主机层。

**⑤ 旋钮②：`enable_decode_overlap`**。[src/executor.rs:1729-L1761](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1729-L1761)：`Off` 是无操作；否则先过数道防御（Pin×overlap 拒绝、PerToken×overlap 拒绝、GQA 分组无编译内核拒绝、TP 拒绝——注释解释了每道防的是哪条绕行路径），最后 `OverlapStreams::create` 建两条 CUDA 流（u3-l7 专讲）。

**⑥ 旋钮③：`load_draft_model`**。[src/executor.rs:1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827)：先校验单卡拓扑、与 KV offload 互斥，然后 `match draft` 分派——`DFlash` 走 `primary.load_dflash`（日志「Qwen3 DFlash speculative decoding enabled」），`Eagle3` 走 `primary.load_eagle3`（日志「Qwen3 EAGLE-3 speculative decoding enabled: N drafts per round, greedy batch-1 only」）。两条臂都返回一份 `DraftMeta`（结构见 [src/executor.rs:3373-L3389](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3373-L3389)），其中新增的 `max_draft_batch` 字段是**调度器可见的草稿器能力位**：DFlash 报 `None`（任意 decode 批都能服务，[src/executor.rs:3607](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3607)），EAGLE-3 报 `Some(EAGLE3_MAX_BATCH)` 即 `Some(1)`（[src/executor/eagle3_lane.rs:66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L66)，常量见 [src/eagle3.rs:24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L24)）——它经 `ModelExecutor::max_draft_batch`（[src/executor.rs:834-L838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838)）上浮到调度门：批宽超过它的请求整批退回纯解码（u4-l2 / u6-l8 展开）。最后在 L1823 **强制 `set_prefix_cache_enabled(false)`**——投机捕获需要每个提示 token 的干净未缓存目标隐藏态，而前缀命中会跳过产生它们的那个前向。这一行就是「必须排在 set_no_prefix_cache 之后」的物证；L1825 `self.speculative = Some(meta)` 则是 4.2.3 ⑤ 说的单槽位写入。

**⑦ LoRA 版装配**。[src/frontend_adapter.rs:142-L170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L142-L170) 的 `start_qwen3_with_lora_control` 是平行入口：构建函数换成 `from_runtime_with_lora_options`（[src/executor.rs:1258-L1279](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1258-L1279)，签名仍收 `dflash_draft_path`，内部以 `DecodeOverlap::Off` 委托回主构建函数；适配层调用时传 `None`），之后只拧旋钮①——LoRA 模式没有 overlap 也没有草稿器（上游 launch 已拒）。

#### 4.3.4 代码实践

**实践目标**：用「反证法」验证三个旋钮的顺序约束，产出一页顺序论证。

1. 读 [src/frontend_adapter.rs:124-L132](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L124-L132)，抄下三行调用的顺序。
2. 读 [src/executor.rs:1776-L1784](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1776-L1784) 与 [src/executor.rs:1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827) 两个方法体。
3. 在纸上执行两次「思想实验」：
   - a) 把 `load_draft_model` 挪到 `set_no_prefix_cache` 之前、且用户传了 `no_prefix_cache=false`：写出两次 `set_prefix_cache_enabled` 的调用序列与最终状态；
   - b) 保持现顺序、用户传 `no_prefix_cache=true`：写出最终状态。
4. 结论应能回答：哪种顺序会让「投机解码 + 前缀缓存同时开」这一非法状态出现？

**需要观察的现象**：`load_draft_model` 的文档注释（L1786-L1792）明说「Disables the prefix cache」，而 `set_no_prefix_cache(false)` 在无 offload 时会调用 `set_prefix_cache_enabled(true)`——两个开关写同一个内部状态位；且无论 `DraftModel::DFlash` 还是 `::Eagle3`，强制关闭都发生在同一条 L1823。

**预期结果**：思想实验 a 出现 `set_prefix_cache_enabled(false)`（草稿装载）→ `set_prefix_cache_enabled(true)`（旋钮①）的序列，最终缓存被重新打开 → 投机路径踩到脏前缀，这正是源码注释防止的事故。b 的最终状态是关。全程源码阅读即可完成，无需运行；如需运行时佐证（日志行「Qwen3 DFlash/EAGLE-3 speculative decoding enabled」），待本地验证（需要 GPU + 模型权重）。

#### 4.3.5 小练习与答案

**练习 1**：草稿的显存既然在构建期预留，为什么装载要推迟到构建后？
**答案**：预留只需要草稿**配置**（尺寸可从 checkpoint 元数据算出，两臂各自调 `*MemoryReservation::from_path`）；而装载需要**目标模型已在 GPU 上**——草稿器与目标共享 embedding/lm_head 并要从目标捕获隐藏态，且要挂进主 lane 的执行结构（[src/frontend_adapter.rs:126-L129](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L126-L129) 注释原话：「the draft is built against the target's embeddings/head」）。

**练习 2**：`enable_decode_overlap` 排在 `load_draft_model` 之前还是之后有讲究吗？
**答案**：本讲范围内两者无直接状态冲突（overlap 建流、draft 装模型），但上游 `launch` 已确保任何草稿器与 overlap 互斥（[src/lib.rs:331-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L331-L336)），所以这对顺序在合法输入下不会同时出现。真正不可换的是 draft 与 `set_no_prefix_cache` 这一对。

**练习 3**：LoRA 平行入口 `start_qwen3_with_lora_control`（[src/frontend_adapter.rs:142-L170](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L142-L170)）里为什么没有 `enable_decode_overlap` 调用？
**答案**：因为 LoRA 服务不支持 decode-overlap——上游 [src/lib.rs:506-L509](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L506-L509) 已 `ensure!` 拒绝，且其构建函数 `from_runtime_with_lora_options` 内部以 `DecodeOverlap::Off` 委托主构建函数（[src/executor.rs:1268-L1278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1268-L1278)）。

**练习 4**：`DraftMeta::max_draft_batch` 为什么 DFlash 报 `None`、EAGLE-3 报 `Some(1)`？
**答案**：DFlash 的草稿器按批工作——任意 decode 批宽都能一轮出草稿，`None` 表示「不设限」；EAGLE-3 v1 只实现了单请求贪心草稿（`EAGLE3_MAX_BATCH = 1`，车道注释写明 greedy batch-1 only），批宽 >1 时它服务不了，所以报 `Some(1)` 让调度器的投机门把这种情况退回纯解码（u6-l8 展开）。能力位放 meta 而不是写死在调度器里，正是为了让调度器对草稿器种类无感。

### 4.4 start_with_executor：装配 Engine、spawn_scheduler 与调度器骨架

#### 4.4.1 概念说明

`start_with_executor` 是契约适配层的**最后一站**：拿一个配置完毕的 executor，造出 `Qwen3Scheduler`，把它交给契约的 `spawn_scheduler` 装进 `Engine` 返回。它是泛型的（`E: ModelExecutor + 'static`）——这一泛型边界让 u1-l2 讲过的 hf_golden_gate 测试与模型本地 benchmark 可以塞入**替身 executor**（实现同一 trait 的测试执行器），复用整套调度与契约逻辑。`ModelExecutor` trait（[src/executor.rs:788-L937](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L788-L937)）因此是「调度器看到的执行器」抽象：容量查询（`block_size`/`available_blocks`/`max_request_blocks`/`max_context_tokens`/`max_decode_batch_size`）、三组前向（`execute_prefill`/`execute_decode`/`execute_unified`）、投机族（draft/verify 两步 + `speculative_enabled`/`speculative_request_ready`/`max_draft_batch` 三个能力查询）、LoRA 三命令、KV-offload 预取钩子等。

#### 4.4.2 核心流程

```text
start_with_executor(executor, seed, max_prefill_tokens, lora_control)
├── 1. assert max_prefill_tokens > 0          // 零预算永远调不出 prefill chunk
├── 2. servable = servable_len(max_context, max_request_blocks, block_size)
├── 3. kv_total = executor.available_blocks() // 刚建好：唯一已占用块是泄漏的图 padding 槽
├── 4. （lora_control 时）铸 LoRA 私有通道 LoraClient::channel()
├── 5. scheduler = Qwen3Scheduler::new(executor, seed, max_prefill_tokens, kv_total, lora_rx)
└── 6. Engine {
         schedulers: vec![ spawn_scheduler("qwen3-scheduler", scheduler) ],
         info:       EngineInfo { kv_capacity, servable_len },
         lora,       // Option<LoraClient>，随 Engine 出境；rx 留在调度器里
     }
```

可服务长度公式（[src/scheduler.rs:135-L140](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L135-L140)）：

\[ \text{servable\_len} = \min\left(\text{max\_context\_tokens},\ \text{max\_request\_blocks} \times \text{block\_size}\right) \]

即「checkpoint 声明的上限」与「单请求最多能占到的 KV 块数」二者取小——u2-l4 会从显存画像方向重新推导这两个输入。

启动之后，`Qwen3Scheduler::step` 每次被驱动调用就跑一遍九段流水（[src/frontend_adapter.rs:633-L826](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L633-L826)，源码自带 0-8 编号注释）：

| 段 | 位置 | 做什么 |
|---|---|---|
| 0 | L634-L640 | 收 LoRA 控制命令入 `pending_control`（仅在调度器排空时应用） |
| 1 | L642-L661 | 轮询 decode-overlap 异步 prefill；退休已 abort 的等待请求 |
| 2 | L663-L686 | 回收已落定的预取、给新请求发起预取（`reserve_floor` 保护已承诺块） |
| 3 | L688-L695 | 若空闲：应用控制命令，放行 `post_control_deferred` |
| 4 | L697-L716 | 仍无事可做：阻塞等一个在途预取 DMA，否则直接返回 |
| 5 | L718-L752 | LoRA 校验 + 准入：每个裁定关闭请求的排队账目 |
| 6 | L754-L774 | chunk 装箱选块（`take_prefill_chunks`），开 prefill 相位 span |
| 7 | L776-L813 | `runtime_plan` 选计划；overlap 激活且 Unified 时走异步 prefill 分支 |
| 8 | L815-L825 | `execute_plan` → `resolve_step` 纯函数解析 → `apply_effects` 写账本 |

u4 会把 5-8 段拆开精讲；本讲只需记住「step = 队列维护 + 选计划 + 执行 + 解析 + 记账」。

#### 4.4.3 源码精读

**① 装配函数本体**。[src/frontend_adapter.rs:172-L217](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L172-L217)。注意三处注释的「为什么」：
- L183-L186 的 `assert!`：零 prefill 预算的调度器是结构性死锁（永远凑不出一个 chunk），所以用断言而非运行时错误；
- L192-L195：刚建好的 executor 唯一已提交的块是「泄漏的 CUDA-graph padding 槽」，所以 `available_blocks()` 恰好是「总数减一」——保守一个块，恰是容量上限该偏的一侧；
- L200-L201：LoRA 通道是 Qwen3 的**私有布线**，在调度器被线程吞掉之前铸好；契约只携带 client 端（`LoraClient`），接收端 `rx` 留在调度器里逐步排空。

**② `Engine` 的构造点即字段证据**。[src/frontend_adapter.rs:209-L216](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L209-L216)：`schedulers` 是 `spawn_scheduler("qwen3-scheduler", scheduler)` 的单元素 vec（驱动线程句柄，名字用于诊断）；`info` 携带 `KvCapacity { total_blocks, block_size }` 与 `servable_len`；`lora` 是 `Option<LoraClient>`。Qwen3 是**单调度器**引擎——所有请求进同一个步进循环，批内并行而非多调度器并行。

**③ 调度器的状态字段**。[src/frontend_adapter.rs:221-L260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L221-L260) 的结构体就是它的「队列全家福」：`deferred`（KV 预算压力下未能准入）、`loading`（异步 CPU 层 KV 预取中）、`prefilling`（已准入未填完，按 id FIFO）、`active`（解码中）、`inflight_prefill_pending`（overlap 异步 prefill 在途）五个队列 + LoRA 控制两队列 + 前缀缓存两个单调计数器。构造函数 [src/frontend_adapter.rs:263-L287](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L263-L287) 全部从空开始，RNG 由 seed 播种。

**④ 错误吸收边界**。[src/frontend_adapter.rs:383-L395](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L383-L395) 的 `absorb_step_failure`：普通步执行错误只杀死被触碰的批次（`fail_touched_requests`，L358 起），引擎继续活；唯独 `WorkerLost`（rank 工作线程丢了，[src/executor.rs:4347](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4347)）向上返回 `Err`——没有可服务的东西了，驱动把整个引擎收摊。

**⑤ 裁定翻译**。[src/frontend_adapter.rs:872-L888](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L872-L888) 的 `contract_reject_reason`：机制层（scheduler.rs 的 `AdmissionReject`）只带决策所需的最小字段，此处再从请求本体补齐 prompt 长度等，扩成契约的带类型 `RejectReason`（`ContextLength` / `EchoPrefillTokens` / `KvBudget`）——又一次「机制与契约分层」。

**⑥ 链条终点**。`spawn_scheduler` 是契约提供的函数（import 于 [src/frontend_adapter.rs:48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L48)，调用于 L210）：起一个驱动线程、以 `"qwen3-scheduler"` 命名、反复调用 scheduler 的 `submit`/`step`/`metrics`。它的实现位于 frontend crate（`pegainfer-frontend/src/engine/`，本讲义的链接 base 之外）；从 Qwen3 侧能断言的是：`start_with_executor` 返回后，调度器对象的所有权已转移进该线程，服务器拿到的 `Engine` 只握句柄与元数据。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 `rg` 从 `pegainfer_qwen3::launch` 一路追到 `spawn_scheduler`，写出带行号的完整启动调用链，并标注分叉点、审计 `dump_graph_png` 校验。

1. **锚点搜索**（本讲已验证可复现）：
   ```bash
   rg -n "fn launch" src/lib.rs src/model_line.rs
   rg -n "start_engine_with_offload_inner|start_engine_with_lora_control|fn start_qwen3|fn start_with_executor|spawn_scheduler|DraftModel::" src/lib.rs src/model_line.rs src/frontend_adapter.rs
   ```
2. **画出调用链**（Markdown 代码块或 mermaid 均可），每个函数标注 `文件:起止行`。参考骨架（行号请以你的搜索输出为准核对）：
   ```text
   Qwen3Line::launch                      src/model_line.rs:296-367
     └─ crate::launch                     src/lib.rs:269-368
        ├─ [LoRA 分支] start_engine_with_lora_control        src/lib.rs:478-522
        │    └─ start_qwen3_with_lora_control                src/frontend_adapter.rs:142-170
        │         └─ start_with_executor                     src/frontend_adapter.rs:174-217
        └─ [主分支] start_engine_with_offload_inner          src/lib.rs:431-476
             └─ start_qwen3                                 src/frontend_adapter.rs:89-140
                  └─ start_with_executor                    src/frontend_adapter.rs:174-217
                       └─ spawn_scheduler("qwen3-scheduler", …)   src/frontend_adapter.rs:210
   ```
   两条支线最终汇入同一个 `start_with_executor`——这就是 4.4.1 说的泛型收口。
3. **标注分叉点**：LoRA 分叉在 `match options.lora`（src/lib.rs:337）；草稿器分叉有两层——`launch` 里的 `DraftModel` 折叠（src/lib.rs:316-326，DFlash/EAGLE-3 在此二选一）与 `start_qwen3` 内 `if let Some(draft) = draft`（src/frontend_adapter.rs:130-132）。注意 DFlash/EAGLE-3 不是两条独立启动分支：折叠之后它们共用主分支的同一个可选步骤，只在 `load_draft_model` 的 `match` 内部才分开（src/executor.rs:1803-1821）。
4. **审计 dump_graph_png**：`rg -n "dump_graph_png" src/` 应命中五处语义点——CLI 认领共享参数（model_line.rs:152 附近）、validate 拒绝与 LoRA 同用（model_line.rs:198-L202）、launch 的三重校验（lib.rs:285-295）、传给主分支（lib.rs:365）、装配期导出（frontend_adapter.rs:113-123）。回答：LoRA 分支是否遗漏了该校验？（提示：不是遗漏——LoRA 臂根本不传此参数，且 lib.rs L290-L293 已把「图导出 + LoRA」组合拒绝在更早处。）

**需要观察的现象**：`rg` 输出里 LoRA 支线的函数都不接收 `draft` / `dump_graph_png` 参数；而 `DraftModel::DFlash` / `DraftModel::Eagle3` 的构造只出现在 `launch` 的折叠 match 与 `start_engine_with_offload` 的映射里——参数表与类型构造点本身就是分叉语义的可读证据。

**预期结果**：一页带行号的调用链 + 分叉标注 + dump 审计结论。全程无需 GPU、无需编译；若想用调试器在 `start_with_executor` 打断点实证调用顺序，待本地验证（需 GPU 环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `start_with_executor` 要做成泛型 `E: ModelExecutor + 'static`，而不是直接收 `Qwen3Executor`？
**答案**：让「调度 + 契约适配」整层可以被非 GPU 替身执行器复用——hf_golden_gate 的 teacher-forcing 回放、模型本地 benchmark、以及 scheduler 单测的 test_support 执行器（[src/scheduler/test_support.rs:473](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L473) 就有替身对 `has_decode_overlap` 的实现，L194-L195 还提供 `with_max_draft_batch` 供测试模拟 EAGLE-3 的批宽限制）都从这一泛型边界受益。`'static` 是因为调度器对象要被 move 进 `spawn_scheduler` 的驱动线程。

**练习 2**：`kv_total` 为什么在装配时快照一次，而不是每次 `metrics` 现场向 executor 要总数？
**答案**：`metrics` 里 `kv_used_blocks = kv_total - available_blocks()`（[src/frontend_adapter.rs:837-L839](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L837-L839)）。总块数在 executor 生命周期内固定（池在构建期一次划定），装配时快照（L195，保守取「总数减一」）既便宜又保证 `used ≤ total` 的不变量；空闲数才是每步变化的量。

**练习 3**：`Qwen3Scheduler` 的五个请求队列里，哪一个不参与 `metrics` 的 `num_running_reqs`？为什么？
**答案**：`loading`（以及 `deferred`、`post_control_deferred`）——它们计入 `num_waiting_reqs`（[src/frontend_adapter.rs:845-L847](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L845-L847)）。running 的口径是「占用引擎执行资源」（active 解码 + prefilling 填装 + 在途异步 prefill）；`loading` 只是在等 CPU 层 DMA，没占 GPU 步进位。

## 5. 综合实践

**场景推演：四种启动配置各走哪条路？**

给定四个（虚构但参数真实的）启动命令，只靠源码预测行为，再核对：

| 场景 | 关键 flag | 预测要点 |
|---|---|---|
| A | `--cuda-graph`（无其他） | 走主分支；`enable_cuda_graph=true`；无图导出；三个旋钮只执行①②（均为默认态）；`start_with_executor` 返回单调度器 Engine |
| B | `--lora-modules name=path` + `--cuda-graph` | 走 LoRA 分支；`launch` L275-L284 打 warn 并强制关图；`LoraClient::channel()` 被铸出；`Engine.lora = Some(client)` |
| C | `--dflash-draft-model-path models/draft` + `--dump-graph-png g.png` | 走主分支；折叠成 `DraftModel::DFlash`；构建期先 `ensure_servable` 校验草稿配置并走 DFlash 臂预留显存；装配期先导图再装草稿；`load_draft_model` 强制关前缀缓存；`max_draft_batch = None` |
| D | `--eagle3-draft-model-path models/eagle3` | 走主分支；折叠成 `DraftModel::Eagle3`；构建期走 Eagle3 臂预留（无 ensure_servable 预检）；装载后日志「greedy batch-1 only」；`max_draft_batch = Some(1)` |

操作步骤：

1. 对每个场景，沿 4.4.4 的调用链逐函数标注「会执行 / 被跳过 / 提前失败」，并指出最终 `Engine` 的 `info`（`kv_capacity`、`servable_len`）与 `lora` 字段取值来源。
2. 为场景 C 额外回答：若用户再叠加 `--no-prefix-cache`，`set_no_prefix_cache(true)` 与草稿装载内部的强制关闭是否冲突？（用 4.3.4 思想实验 b 的结论回答：不冲突，两次都指向「关」。）
3. 为场景 C+D（两个草稿 flag 同时给）回答：会在哪几层被拒？（CLI validate 的 conflicts 表 model_line.rs:260-L278 先拒；即使绕过 CLI，`launch` 折叠的 `(Some, Some)` 臂 lib.rs:320-L322 再拒。）
4. 有 GPU 与权重时可选实测：场景 A 启动后观察日志里的图捕获信息与 `/metrics`（前缀缓存计数器）；场景 D 观察装载日志与投机计数器是否出现；无 GPU 则全部结论标注「源码推理，待本地验证」。

预期结果：一张四列对照表，每列从 `Qwen3Line::launch` 一路写到 `spawn_scheduler`，能向别人五分钟讲清「服务器进程从收到 CLI 参数到驱动线程开转」的全过程。

## 6. 本讲小结

- **契约三件套**：`Engine` 是交给服务器的整车（调度器句柄 + 元数据 + 可选 LoRA 客户端）；`Scheduler` 是被驱动轮询的 `submit`/`step`/`metrics`；`RequestLedger` 是驱动侧拥有、调度器按 `RequestId` 写入的裁定与 token 账本。frontend crate 在链接 base 之外，但契约面可从 `frontend_adapter.rs` 的 import 与调用点完整还原。
- **frontend_adapter.rs 只有两项契约职责**：把每步 effects 翻译成 ledger 写入（`apply_effects`），以及构建 executor 并装配 `Engine` 返回——其余全是 Qwen3 私有机制。
- **启动链条**：`Qwen3Line::launch` → `crate::launch`（策略：设备映射、LoRA 关图、dump 校验、overlap×TP 拒绝、**DraftModel 折叠**、两条投机拒绝、lora 分叉）→ `start_engine_with_offload_inner`（spec_env 校验、batch-invariant 门、全局 NumericPolicy）→ `start_qwen3`（构建 + 三旋钮）→ `start_with_executor`（`servable_len`、LoRA 通道、`Qwen3Scheduler::new`）→ `spawn_scheduler`。
- **草稿器二选一**：`launch` 把 DFlash/EAGLE-3 两条路径折叠成 `Option<DraftModel>`，同时给两个直接 bail；executor 的 `speculative` 是单槽位、捕获管线与显存预留也只服务一个草稿器，所以互斥在 CLI、launch 两层设防。
- **三旋钮顺序不可乱**：`set_no_prefix_cache` → `enable_decode_overlap` → `load_draft_model(DraftModel)`；草稿装载（无论 DFlash 还是 EAGLE-3）都会强制关前缀缓存，若排在旋钮①之前会被「最后写入者胜」重新打开；草稿显存之所以敢后装，是因为构建期已按草稿配置（两臂各自的 reservation）预留。
- **取消是响应式的、完成有两种出口**：abort 靠下步顶部的探测点退休；普通 finish 随本步提交，P/D prefill 角色则铸 `DeferredFinish` 延迟到 KV 对同伴可见。
- **`ModelExecutor` 是调度器视角的执行器抽象**，`start_with_executor` 的泛型边界让测试替身与 benchmark 复用整层调度逻辑；新增的 `max_draft_batch` 能力位让调度器无需知道草稿器种类就能做批宽门控。

## 7. 下一步学习建议

本讲收官 u1 单元：你已经掌握 crate 定位（u1-l1）、测试地图（u1-l2）、CLI 接缝（u1-l3）与契约装配（本讲）。接下来进入 u2「配置与权重：模型本体」：

- **u2-l1 配置解析**：`start_qwen3` 第 1 步里被我们一笔带过的 `config.json` 校验，正是下一讲的起点；
- **u2-l2/u2-l4**：本讲出现的 `Qwen3MemoryOptions`、`KvCapacity`、`servable_len` 的上游——权重布局与显存画像；
- 若更想先看动态一侧，可直接跳 u3-l1（executor 与 plan/result 契约总览，`DraftMeta` 与投机两步在那里展开），再回头补 u2；
- 阅读源码时建议带着本讲的综合实践产出（调用链笔记）对照：u2-u4 的每一站都能挂到这条链的某个节点上。
