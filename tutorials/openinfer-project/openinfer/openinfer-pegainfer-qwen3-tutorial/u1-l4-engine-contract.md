# u1-l4 引擎契约与启动装配：frontend_adapter 全景

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 **Engine**、**Scheduler**、**RequestLedger** 三个契约角色各自的职责边界——谁拥有请求状态、谁驱动步进、谁记录裁定。
2. 完整追踪一次引擎启动的调用链：`Qwen3Line::launch` → `crate::launch` → `start_engine_with_offload_inner` / `start_engine_with_lora_control` → `frontend_adapter::start_qwen3` → `start_with_executor` → `spawn_scheduler`。
3. 解释 executor 构建之后 `set_no_prefix_cache` / `enable_decode_overlap` / `load_dflash_draft_model` 三步配置的**顺序为什么不能乱**。
4. 读懂 `Qwen3Scheduler::step` 的八段式结构，知道每一段在做什么（为 u4 的调度器三讲铺路）。

本讲是 u1（起步单元）的收官：u1-l3 讲了 Qwen3 如何通过 ModelLine 挂进 CLI，本讲接着回答「`crate::launch` 被调用之后发生了什么」，把服务器进程与 GPU 执行器之间的最后一层接缝讲透。

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

- 已读 u1-l3（`Qwen3Line::launch` 如何组装 `Qwen3LaunchOptions`）。
- 知道 prefill（整段提示词前向）与 decode（每请求单 token 批量前向）两阶段的区别（u1-l1）。
- 知道 `--lib` 单测不需要 GPU（u1-l2）——本讲实践全部是源码阅读型，无需 GPU。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `src/frontend_adapter.rs` | 契约适配层：Qwen3 与引擎契约的全部接缝，一个文件写完 | 模块文档、`start_qwen3`、`start_with_executor`、`Qwen3Scheduler` 的 `Scheduler` 实现 |
| `src/lib.rs` | crate 根：`Qwen3LaunchOptions`、`launch` 与各 `start_engine*` 入口、`runtime` 再导出 | `launch` 的策略分叉、`start_engine_with_offload_inner` |
| `src/model_line.rs` | ModelLine 实现（u1-l3 已讲） | 仅引用 `launch` 调用点，标出链条最上游 |
| `src/executor.rs` | GPU 执行器（u3 深入） | `ModelExecutor` trait、三个构建后配置方法、DFlash 显存预留 |
| `src/scheduler.rs` / `src/scheduler/plan.rs` | 纯调度层（u4 深入） | `servable_len`、`runtime_plan`、`execute_plan` 的衔接点 |

> **注意**：`pegainfer-frontend` 是工作区里的独立 crate，其源码**不在本仓库快照内**（本仓库只含 `pegainfer-qwen3`）。因此契约类型的定义处无法给出永久链接；本讲全部从 Qwen3 这一**消费侧**的 import、调用点与构造点来还原契约面。凡是涉及 frontend crate 内部实现细节的地方，都以「消费侧可见」为准。

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

**① 模块文档：这个文件自己声明了两项契约职责**。[src/frontend_adapter.rs:1-L22](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L1-L22) 说：`Qwen3Scheduler` 实现 `pegainfer_frontend::engine::Scheduler`；驱动轮询 `submit`/`step`/`metrics`；此文件只拥有两个面向契约的职责——**ledger 写入**（把每步 effects 翻译成按 id 记录的裁定与 token）和**引擎装配**（构建 executor 并从 `launch` 返回 `Engine`）。取消是响应式的：前端翻转请求的 abort 标志，适配器在下步顶部退休它，调度机制永远看不到 abort。

**② import 清单即「契约面清单」**。[src/frontend_adapter.rs:33-L48](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L33-L48) 从 `pegainfer_frontend::engine` 引入的全部类型就是 Qwen3 实际用到的契约面：`Engine`、`EngineInfo`、`KvCapacity`、`QueuedRequest`、`RequestLedger`、`Scheduler`、`SchedulerMetrics`、`RejectReason`、`FinishReason`、`StopCause`、`DeferredFinish`、`PromptEcho`、`LoraClient`/`LoraControl`/`LoraControlReceiver`、`spawn_scheduler`。

**③ `Scheduler` 的实现签名**。[src/frontend_adapter.rs:618-L630](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L618-L630) 是 `impl<E: ModelExecutor> Scheduler for Qwen3Scheduler<E>` 的开头——`submit` 把 `QueuedRequest` 拆成契约给的 `id` 与请求体，记入队列追踪（`tracker.enter_queue`），转成内部 `PendingRequest` 后推进 `deferred` 队列；若此刻有待处理的 LoRA 控制命令，则先押进 `post_control_deferred`（控制命令必须先于后来者生效）。

**④ 账本写入长什么样**。[src/frontend_adapter.rs:399-L414](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L399-L414) 的 `apply_effects` 是唯一的「翻译器」：先把本步的前缀缓存计数累进单调总量，再依次处理 `cached`（写 `ledger.set_cached_tokens`）、`prompt_echoes`（写 `ledger.echo_prompt`）、`decode`/`pending` 两族 effect（写 `ledger.push_tokens`、收集 finish），最后统一结算。Qwen3 侧可见的账本方法全集（消费侧证据）：`admit` / `reject` / `retire` / `fail` / `is_active` / `is_aborted` / `push_tokens` / `set_cached_tokens` / `echo_prompt` / `finish_with_cause` / `defer_finish_with_cause`。

**⑤ 完成事件的两种出口**。[src/frontend_adapter.rs:594-L608](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L594-L608)：普通执行器直接 `ledger.finish_with_cause` 让终止事件随本步提交；**扣留型**执行器（`executor.withholds_finishes()` 为真，即 P/D prefill 角色）则用 `ledger.defer_finish_with_cause` 铸出 `DeferredFinish` 令牌交给 `executor.release_finished_events`，等 KV 保存对同伴可见后再投递（投递函数见 [src/executor.rs:920-L926](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L920-L926) 的 `send_finished_events`）。每个 `DeferredFinish` 携带该请求的完整最终记录，所以晚投递不会乱序。

**⑥ `metrics`：路由器想看的稳态负载**。[src/frontend_adapter.rs:831-L865](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L831-L865) 汇报：`kv_used_blocks`（总数减空闲，步间稳态而非步内峰值）、`num_running_reqs`（active + prefilling + 在途异步 prefill）、`num_waiting_reqs`（deferred + loading + post_control 三个未运行队列之和）、投机解码计数器，以及前缀缓存计数（本调度器的累计查询/命中 + 执行器侧 connector 查询的合并，见注释里 local/external 的归并规则）。

#### 4.1.4 代码实践

**实践目标**：不用读 frontend crate 源码，仅凭消费侧调用点画出「契约面使用图」。

1. 运行：
   ```bash
   rg -n "ledger\." src/frontend_adapter.rs
   ```
2. 把输出整理成一张表：`账本方法 | 调用行号 | 所在 effect 分支 | 语义`（例如 `push_tokens` 出现在 `DecodeEffect::Continue` / `Finish` / `FinishMany` 与 `PendingEffect::Promote` / `EmitAndFinish`）。
3. 再运行 `rg -n "is_aborted" src/frontend_adapter.rs`，数一数 abort 探测点出现在哪些阶段——体会「取消是响应式的」这句话落在多少个检查点上。

**需要观察的现象**：所有账本写入都发生在 `apply_effects`、`admit_or_retire`、`reject_pending`、`retire_aborted*`、`fail_touched_requests` 这几个函数里，`step` 的调度逻辑（选计划、装箱）本身零账本写入——读写分离。

**预期结果**：得到一张 11 个账本方法的清单表；能指出 `finish_with_cause` 与 `defer_finish_with_cause` 只在 [src/frontend_adapter.rs:594-L608](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L594-L608) 一处按 `withholds_finishes()` 二选一。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Qwen3Scheduler` 自己不维护「要发给前端的 token 队列」，而要借 `RequestLedger` 写入？
**答案**：对外事件的记账是契约的职责（前端按 id 读账本生成响应）；调度器若自持队列，就必须同时实现一套与契约对齐的事件语义，且 decode-overlap 等路径下队列克隆/重放容易出错。账本由驱动侧拥有、借给 `step` 写入，保证每请求单一事实源，调度机制保持「纯 effect 输出」（这正是 u4-l3 要展开的 sink-free 设计）。

**练习 2**：`submit` 里 `PendingRequest::from_request(id, request)` 的 `id` 从哪来？
**答案**：来自契约的 `QueuedRequest`——`let QueuedRequest { id, request } = request;`（[src/frontend_adapter.rs:622](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L622)）。`id` 就是贯穿全链路的 `RequestId`，账本、effect、executor 结果全部按它索引。

**练习 3**：`DeferredFinish` 解决什么问题？
**答案**：P/D prefill 角色要求「HTTP 响应即 KV-ready 信号」——`Finished` 事件必须等到 KV 保存 + MetaServer 注册对同伴可见后才能发出。扣留型执行器把 finish 铸成携带完整最终记录的 `DeferredFinish` 令牌，在步外投递，调度线程从不等待 flush 屏障（默认 trait 方法与文档见 [src/executor.rs:884-L904](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L884-L904)）。

### 4.2 launch：启动策略的唯一拥有者（lib.rs）

#### 4.2.1 概念说明

`launch` 是 crate 对外的**策略入口**：吃进服务器翻译好的 `Qwen3LaunchOptions`（[src/lib.rs:228-L256](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L228-L256)，字段含 `device_ordinal`、`tp_size`、`cuda_graph`、`dump_graph_png`、`offload`、`no_prefix_cache`、`max_prefill_tokens`、`memory`、`lora`、`decode_overlap`、`batch_invariant`、`dflash_draft_model_path`），吐出一个 `Engine`。它的文档（[src/lib.rs:222-L227](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L222-L227)）点明设计意图：**启动策略跟着模型走，不漏进服务器**——TP→设备的映射、LoRA↔CUDA-Graph 互斥这些「为什么」只有 Qwen3 自己知道。

#### 4.2.2 核心流程

```text
launch(model_path, options)
├── 1. 推导 device_ordinals        // tp=1 → [device_ordinal]；否则 0..tp
├── 2. LoRA 强制关 CUDA Graph       // 图会烘焙适配器指针（u7 详述）
├── 3. dump_graph_png 三重校验      // 需图开启 + 无 LoRA + 路径可写
├── 4. 组装 EngineLoadOptions       // 契约侧的加载选项（seed=42 等）
├── 5. 三条提前拒绝                 // overlap×TP、DFlash×LoRA、DFlash×overlap
└── 6. match options.lora
    ├── Some → start_engine_with_lora_control   // LoRA 分支
    └── None → start_engine_with_offload_inner  // 主分支（含 DFlash 分叉点）
```

「失败越早越便宜」是 u1-l3 建立的主题在 lib.rs 的延续：第 5 步的三条检查在 executor.rs 里有**承重副本**（如 `enable_decode_overlap` 内部的 TP 拒绝），这里提前再查一遍是为了省掉整次模型装载 + 图预捕获的开销后才报错——见 [src/lib.rs:304-L305](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L304-L305) 的注释。

#### 4.2.3 源码精读

**① 入口与设备映射**。[src/lib.rs:263-L268](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L263-L268)：单卡用 `options.device_ordinal`；TP>1 固定占用 `0..tp_size`。

**② LoRA 关图**。[src/lib.rs:270-L278](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L270-L278)：LoRA 服务会在步与步之间重指适配器权重，而捕获好的 decode 图把旧指针烘焙在内，所以强制 `enable_cuda_graph=false` 并 `warn!` 提示（用户显式要图时才有日志）。

**③ dump_graph_png 校验**。[src/lib.rs:279-L289](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L279-L289)：`ensure!`（图必须开启）、`ensure!`（不能与 LoRA 同用）、`pegainfer_core::cuda_graph::validate_graph_dump_request(path)?`（路径可写性等，实现在 core crate）。注意 `dump_graph_png` **只走主分支**——LoRA 分支的调用（L327-L338）根本不传这个参数。

**④ 契约侧加载选项**。[src/lib.rs:290-L296](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L290-L296) 构造 `EngineLoadOptions { enable_cuda_graph, device_ordinals, parallel_config: None, ep_backend: Nccl, seed: 42 }`——这是契约定义的通用加载参数包，Qwen3 模型线当前固定 seed 为 42。

**⑤ 三条提前拒绝**。[src/lib.rs:306-L320](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L306-L320)：decode-overlap 不支持 TP（overlap 流建在 device 0 上、TP 图只在计算流预捕获）；DFlash 不能配 LoRA；DFlash 不能配 decode-overlap（投机路径永不走统一 overlap 路线，两条流只会白占草稿器需要的显存——错误信息本身就在讲「为什么」）。

**⑥ 分叉点**。[src/lib.rs:321-L351](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L321-L351)：`match options.lora` 是启动期最大的分叉——`Some(lora)` 走 LoRA 控制（热加载通道模式，见 4.3），`None` 走主分支并把 `dflash_draft_model_path` 与 `dump_graph_png` 一并传下。

**⑦ 主分支内部**（`start_engine_with_offload_inner`，[src/lib.rs:406-L458](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L406-L458)）：
- L419 先 `crate::spec_env::validate()`——投机解码环境旋钮非法就在任何 GPU 工作前失败（u6-l7 详述）；
- L420-L425 从 `EngineLoadOptions` 解构出 `enable_cuda_graph` / `device_ordinals` / `seed`；
- L429-L437 `batch_invariant` 时调 `ensure_batch_invariant_supported`（五条拒绝，u8-l1 逐条分析）；
- L438 `apply_batch_invariant_policy` 设全局 `NumericPolicy`（[src/lib.rs:506-L516](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L506-L516)）——注意它必须在 executor 构建之前执行，因为构建期的 GEMM 调参/图捕获行为依赖这个全局策略；
- L439-L444 把草稿模型路径校验成 UTF-8；
- L445-L457 调 `frontend_adapter::start_qwen3`——链条进入下一站。

LoRA 分支（[src/lib.rs:460-L504](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L460-L504)）结构相同但更严：`batch_invariant` 直接 `bail!`（rank-K 适配器形状未经验证）、decode-overlap 必须 `Off`，然后调 `frontend_adapter::start_qwen3_with_lora_control`。

**⑧ 更低层的公共入口**。`start_engine`（[src/lib.rs:354-L366](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L354-L366)）与 `start_engine_with_offload`（[src/lib.rs:381-L404](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L381-L404)）是不经 `Qwen3LaunchOptions` 的便捷入口（后者是前者的 offload 版，默认参数 + `dump_graph_png=None`），主要服务测试与基准；服务器路径始终走 `launch`。

#### 4.2.4 代码实践

**实践目标**：把 `launch` 的全部守卫整理成一张「检查 × 条件 × 错误信息 × 承重副本位置」表。

1. 运行：
   ```bash
   rg -n "ensure!|bail!|warn!" src/lib.rs | sed -n '1,60p'
   ```
2. 只挑 `launch` / `start_engine_with_offload_inner` / `start_engine_with_lora_control` / `ensure_batch_invariant_supported` 四个函数里的命中项制表。
3. 对每条检查，用 `rg -n "关键词" src/executor.rs` 找它的承重副本（例：「decode-overlap is unsupported under tensor parallelism」在 [src/executor.rs:1723-L1726](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1723-L1726) 还有一份）。

**需要观察的现象**：同一条规则往往出现两处——外层早失败（省装载时间）、内层承重（防绕过）。两条信息文本几乎一致，方便搜索配对。

**预期结果**：至少 9 行的守卫表；能回答「如果绕过 CLI 直接用 `start_engine_with_offload` 传非法组合，哪条检查还会兜底」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LoRA 分支收不到 `dump_graph_png`？
**答案**：因为分叉发生在 `match options.lora`（[src/lib.rs:321-L338](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L321-L338)），LoRA 臂的调用参数表里没有这一项；且 `launch` 早在 L284-L287 就 `ensure!` 过「图导出不能配 LoRA 服务」，所以传了也会在更早处被拒。

**练习 2**：`apply_batch_invariant_policy` 为什么放在 `start_qwen3` 之前而不是之后？
**答案**：它设置进程级全局 `NumericPolicy`（Pin/Tuned）。executor 构建过程（GEMM 算法预热、图捕获、Pin 包络自检）的行为由这个全局策略决定；放在构建后就晚了——图已经按 Tuned 捕获完了。见 [src/lib.rs:429-L438](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L429-L438) 的顺序。

**练习 3**：`launch` 的种子固定为 42，这影响什么？
**答案**：`EngineLoadOptions.seed` 最终传给 `start_with_executor`，用于 `StdRng::seed_from_u64`（[src/frontend_adapter.rs:271](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L271)），即调度器采样 RNG 的根种子；请求级采样种子由每步计划从该 RNG 派生（u4-l2 详述）。

### 4.3 start_qwen3：构建 executor 与构建后配置的顺序（frontend_adapter.rs）

#### 4.3.1 概念说明

`start_qwen3` 是契约适配层的**主装配函数**：把十来个扁平参数变成一个配置完毕的 `Qwen3Executor`，再交给 `start_with_executor` 包装成 `Engine`。它表面上只是「new 一个 executor 再拧三个旋钮」，真正的学问在**旋钮的顺序**：三个构建后配置——`set_no_prefix_cache`、`enable_decode_overlap`、`load_dflash_draft_model`——两两之间有依赖，乱序会静默破坏语义。

#### 4.3.2 核心流程

```text
start_qwen3(...)
├── 1. Qwen3Executor::from_runtime_with_decode_environment(...)   // 构建目标模型
│      ├── 校验 memory/lora 选项、设备数、offload 单卡约束、PerToken 约束
│      ├── （有草稿路径时）先 ensure_servable 再装载目标 —— 坏草稿配置自己报错
│      ├── 装载权重 + 显存画像（profile）→ 推导 KV 块预算
│      └── （有草稿路径时）把草稿的固定字节与池内按比例字节折进 margin/块预算
├── 2. dump_graph_png（可选）：导出 rank-0 batch-1 split-KV decode 图
├── 3. set_no_prefix_cache(no_prefix_cache)                       // 旋钮①
├── 4. enable_decode_overlap(decode_overlap)?                     // 旋钮②（建第二条流）
├── 5. load_dflash_draft_model(draft_path)?（可选）               // 旋钮③（最后）
└── 6. start_with_executor(executor, seed, max_prefill_tokens, false)
```

顺序的三条理由（对应源码注释 [src/frontend_adapter.rs:125-L128](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L125-L128)）：

1. **草稿模型建立在目标之上**——DFlash 草稿器要用目标的 embedding/lm_head，只能等目标 executor 起来之后装载；
2. **DFlash 会强制关闭前缀缓存**（`load_dflash_draft_model` 末尾显式 `set_prefix_cache_enabled(false)`），所以它必须排在 `set_no_prefix_cache` **之后**——若顺序颠倒，后执行的 `set_no_prefix_cache(false)` 会把草稿器刚关掉的缓存又打开（最后写入者胜）；
3. **草稿的显存足迹在构建期已预留**——草稿路径在第 1 步就传进了构建函数，画像阶段据此扩 margin、缩块预算，所以第 5 步装载时不会溢出。

#### 4.3.3 源码精读

**① 构建调用**。[src/frontend_adapter.rs:101-L111](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L101-L111) 调 `Qwen3Executor::from_runtime_with_decode_environment`，把模型路径、图开关、设备表、LoRA 选项（此处传 default，热加载模式才传真配置）、offload、`max_prefill_tokens`、**草稿路径**、内存选项、overlap 模式全部交出。构建函数开头（[src/executor.rs:1267-L1298](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1267-L1298)）就能看到「坏草稿配置先于目标装载被拒」：`DFlashConfig::from_file(path)?.ensure_servable()?`，注释明言这是为了让 DSpine 配置以自己的理由失败，而不是在 KV 尺寸推导时炸出费解错误。

**② 草稿显存预留**。[src/executor.rs:1331-L1342](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1331-L1342)：草稿模型在画像之后装载、却住在分页 KV 池之外，所以构建期就从草稿配置**预先**算出 `DFlashMemoryReservation`——固定字节（权重 + 块 scratch）加进 `kv_cache_memory_margin_bytes`，按 token 比例的字节折进块预算。这解释了 4.3.2 的理由 3。

**③ 图导出**。[src/frontend_adapter.rs:112-L122](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L112-L122)：若有 `dump_graph_png`，立即导出活动图的摘要（节点数/内核数/边数 + `.dot`/`.png` 路径）并 `info!`。导出用的图是构建期捕获好的，所以放在构建后立刻做；u3-l5 会沿 `dump_decode_graph_png`（[src/executor.rs:1101](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1101)）展开整条链。

**④ 旋钮①：`set_no_prefix_cache`**。执行器侧实现在 [src/executor.rs:1732-L1753](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1732-L1753)，语义分两支：无 offload 时经典地整体关闭前缀匹配；有 offload 时变成「纯 L2 模式」——匹配保持开（host 层恢复依赖它拾起块），但停止在 HBM 保留已完成块，逼每次复用走主机层。

**⑤ 旋钮②：`enable_decode_overlap`**。[src/executor.rs:1695-L1730](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1695-L1730)：`Off` 是无操作；否则先过三道防御（Pin×overlap 拒绝、PerToken×overlap 拒绝、GQA 分组无编译内核拒绝、TP 拒绝——注释解释了每道防的是哪条绕行路径），最后 `OverlapStreams::create` 建两条 CUDA 流（u3-l7 专讲）。

**⑥ 旋钮③：`load_dflash_draft_model`**。[src/executor.rs:1755-L1782](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1755-L1782)：校验单卡拓扑、与 KV offload 互斥，装载草稿到主 lane，然后在 L1778 **强制 `set_prefix_cache_enabled(false)`**——投机捕获需要每个提示 token 的干净未缓存目标隐藏态，而前缀命中会跳过产生它们的那个前向。这一行就是「必须排在 set_no_prefix_cache 之后」的物证。

**⑦ LoRA 版装配**。[src/frontend_adapter.rs:141-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L141-L169) 的 `start_qwen3_with_lora_control` 是平行入口：构建函数换成 `from_runtime_with_lora_options`（[src/executor.rs:1239-L1260](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1239-L1260)，内部只是以 `DecodeOverlap::Off` 委托回主构建函数），之后只拧旋钮①——LoRA 模式没有 overlap 也没有草稿器（上游 launch 已拒）。

#### 4.3.4 代码实践

**实践目标**：用「反证法」验证三个旋钮的顺序约束，产出一页顺序论证。

1. 读 [src/frontend_adapter.rs:123-L131](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L123-L131)，抄下三行调用的顺序。
2. 读 [src/executor.rs:1745-L1753](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1745-L1753) 与 [src/executor.rs:1762-L1782](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1762-L1782) 两个方法体。
3. 在纸上执行两次「思想实验」：
   - a) 把 `load_dflash_draft_model` 挪到 `set_no_prefix_cache` 之前、且用户传了 `no_prefix_cache=false`：写出两次 `set_prefix_cache_enabled` 的调用序列与最终状态；
   - b) 保持现顺序、用户传 `no_prefix_cache=true`：写出最终状态。
4. 结论应能回答：哪种顺序会让「投机解码 + 前缀缓存同时开」这一非法状态出现？

**需要观察的现象**：`load_dflash_draft_model` 的文档注释（L1755-L1761）明说「Disables the prefix cache」，而 `set_no_prefix_cache(false)` 在无 offload 时会调用 `set_prefix_cache_enabled(true)`——两个开关写同一个内部状态位。

**预期结果**：思想实验 a 出现 `set_prefix_cache_enabled(false)`（草稿装载）→ `set_prefix_cache_enabled(true)`（旋钮①）的序列，最终缓存被重新打开 → 投机路径踩到脏前缀，这正是源码注释防止的事故。b 的最终状态是关。全程源码阅读即可完成，无需运行；如需运行时佐证（日志行「Qwen3 DFlash speculative decoding enabled」），待本地验证（需要 GPU + 模型权重）。

#### 4.3.5 小练习与答案

**练习 1**：DFlash 草稿的显存既然在构建期预留，为什么装载要推迟到构建后？
**答案**：预留只需要草稿**配置**（尺寸可从 checkpoint 元数据算出）；而装载需要**目标模型已在 GPU 上**——草稿器与目标共享 embedding/lm_head，且要挂进主 lane 的执行结构（[src/frontend_adapter.rs:125-L128](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L125-L128) 注释原话：「the draft is built against the target's embeddings/head」）。

**练习 2**：`enable_decode_overlap` 排在 `load_dflash_draft_model` 之前还是之后有讲究吗？
**答案**：本讲范围内两者无直接状态冲突（overlap 建流、draft 装模型），但上游 `launch` 已确保 DFlash 与 overlap 互斥（[src/lib.rs:314-L320](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L314-L320)），所以这对顺序在合法输入下不会同时出现。真正不可换的是 draft 与 `set_no_prefix_cache` 这一对。

**练习 3**：LoRA 平行入口 `start_qwen3_with_lora_control`（[src/frontend_adapter.rs:141-L169](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L141-L169)）里为什么没有 `enable_decode_overlap` 调用？
**答案**：因为 LoRA 服务不支持 decode-overlap——上游 [src/lib.rs:488-L491](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L488-L491) 已 `ensure!` 拒绝，且其构建函数 `from_runtime_with_lora_options` 内部以 `DecodeOverlap::Off` 委托主构建函数（[src/executor.rs:1249-L1259](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L1249-L1259)）。

### 4.4 start_with_executor：装配 Engine、spawn_scheduler 与调度器骨架

#### 4.4.1 概念说明

`start_with_executor` 是契约适配层的**最后一站**：拿一个配置完毕的 executor，造出 `Qwen3Scheduler`，把它交给契约的 `spawn_scheduler` 装进 `Engine` 返回。它是泛型的（`E: ModelExecutor + 'static`）——这一泛型边界让 u1-l2 讲过的 hf_golden_gate 测试与模型本地 benchmark 可以塞入**替身 executor**（实现同一 trait 的测试执行器），复用整套调度与契约逻辑。`ModelExecutor` trait（[src/executor.rs:775-L918](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L775-L918)）因此是「调度器看到的执行器」抽象：容量查询（`block_size`/`available_blocks`/`max_request_blocks`/`max_context_tokens`/`max_decode_batch_size`）、三组前向（`execute_prefill`/`execute_decode`/`execute_unified`）、投机两步（draft/verify）、LoRA 三命令、KV-offload 预取钩子等。

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

可服务长度公式（[src/scheduler.rs:135-L140](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler.rs#L135-L140)）：

\[ \text{servable\_len} = \min\left(\text{max\_context\_tokens},\ \text{max\_request\_blocks} \times \text{block\_size}\right) \]

即「checkpoint 声明的上限」与「单请求最多能占到的 KV 块数」二者取小——u2-l4 会从显存画像方向重新推导这两个输入。

启动之后，`Qwen3Scheduler::step` 每次被驱动调用就跑一遍八段流水（[src/frontend_adapter.rs:632-L825](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L632-L825)，源码自带 0-8 编号注释）：

| 段 | 位置 | 做什么 |
|---|---|---|
| 0 | L633-L639 | 收 LoRA 控制命令入 `pending_control`（仅在调度器排空时应用） |
| 1 | L641-L660 | 轮询 decode-overlap 异步 prefill；退休已 abort 的等待请求 |
| 2 | L662-L685 | 回收已落定的预取、给新请求发起预取（`reserve_floor` 保护已承诺块） |
| 3 | L687-L694 | 若空闲：应用控制命令，放行 `post_control_deferred` |
| 4 | L696-L715 | 仍无事可做：阻塞等一个在途预取 DMA，否则直接返回 |
| 5 | L717-L751 | LoRA 校验 + 准入：每个裁定关闭请求的排队账目 |
| 6 | L753-L773 | chunk 装箱选块（`take_prefill_chunks`），开 prefill 相位 span |
| 7 | L775-L812 | `runtime_plan` 选计划；overlap 激活且 Unified 时走异步 prefill 分支 |
| 8 | L814-L824 | `execute_plan` → `resolve_step` 纯函数解析 → `apply_effects` 写账本 |

u4 会把 5-8 段拆开精讲；本讲只需记住「step = 队列维护 + 选计划 + 执行 + 解析 + 记账」。

#### 4.4.3 源码精读

**① 装配函数本体**。[src/frontend_adapter.rs:171-L216](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L171-L216)。注意三处注释的「为什么」：
- L182-L185 的 `assert!`：零 prefill 预算的调度器是结构性死锁（永远凑不出一个 chunk），所以用断言而非运行时错误；
- L191-L193：刚建好的 executor 唯一已提交的块是「泄漏的 CUDA-graph padding 槽」，所以 `available_blocks()` 恰好是「总数减一」——保守一个块，恰是容量上限该偏的一侧；
- L199-L200：LoRA 通道是 Qwen3 的**私有布线**，在调度器被线程吞掉之前铸好；契约只携带 client 端（`LoraClient`），接收端 `rx` 留在调度器里逐步排空。

**② `Engine` 的构造点即字段证据**。[src/frontend_adapter.rs:208-L215](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L208-L215)：`schedulers` 是 `spawn_scheduler("qwen3-scheduler", scheduler)` 的单元素 vec（驱动线程句柄，名字用于诊断）；`info` 携带 `KvCapacity { total_blocks, block_size }` 与 `servable_len`；`lora` 是 `Option<LoraClient>`。Qwen3 是**单调度器**引擎——所有请求进同一个步进循环，批内并行而非多调度器并行。

**③ 调度器的状态字段**。[src/frontend_adapter.rs:220-L259](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L220-L259) 的结构体就是它的「队列全家福」：`deferred`（KV 预算压力下未能准入）、`loading`（异步 CPU 层 KV 预取中）、`prefilling`（已准入未填完，按 id FIFO）、`active`（解码中）、`inflight_prefill_pending`（overlap 异步 prefill 在途）五个队列 + LoRA 控制两队列 + 前缀缓存两个单调计数器。构造函数 [src/frontend_adapter.rs:261-L286](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L261-L286) 全部从空开始，RNG 由 seed 播种。

**④ 错误吸收边界**。[src/frontend_adapter.rs:382-L394](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L382-L394) 的 `absorb_step_failure`：普通步执行错误只杀死被触碰的批次（`fail_touched_requests`，L357-L377），引擎继续活；唯独 `WorkerLost`（rank 工作线程丢了，[src/executor.rs:4266](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/executor.rs#L4266)）向上返回 `Err`——没有可服务的东西了，驱动把整个引擎收摊。

**⑤ 裁定翻译**。[src/frontend_adapter.rs:868-L887](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L868-L887) 的 `contract_reject_reason`：机制层（scheduler.rs 的 `AdmissionReject`）只带决策所需的最小字段，此处再从请求本体补齐 prompt 长度等，扩成契约的带类型 `RejectReason`（`ContextLength` / `EchoPrefillTokens` / `KvBudget`）——又一次「机制与契约分层」。

**⑥ 链条终点**。`spawn_scheduler` 是契约提供的函数（import 于 [src/frontend_adapter.rs:48](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L48)，调用于 L209）：起一个驱动线程、以 `"qwen3-scheduler"` 命名、反复调用 scheduler 的 `submit`/`step`/`metrics`。它的实现位于 frontend crate（本仓库快照之外）；从 Qwen3 侧能断言的是：`start_with_executor` 返回后，调度器对象的所有权已转移进该线程，服务器拿到的 `Engine` 只握句柄与元数据。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：用 `rg` 从 `pegainfer_qwen3::launch` 一路追到 `spawn_scheduler`，写出带行号的完整启动调用链，并标注两个分叉点、审计 `dump_graph_png` 校验。

1. **锚点搜索**（本讲已验证可复现）：
   ```bash
   rg -n "fn launch" src/lib.rs src/model_line.rs
   rg -n "start_engine_with_offload_inner|start_engine_with_lora_control|fn start_qwen3|fn start_with_executor|spawn_scheduler" src/lib.rs src/model_line.rs src/frontend_adapter.rs
   ```
2. **画出调用链**（Markdown 代码块或 mermaid 均可），每个函数标注 `文件:起止行`。参考骨架（行号请以你的搜索输出为准核对）：
   ```text
   Qwen3Line::launch                      src/model_line.rs:263-333
     └─ crate::launch                     src/lib.rs:263-352
        ├─ [LoRA 分支] start_engine_with_lora_control        src/lib.rs:460-504
        │    └─ start_qwen3_with_lora_control                src/frontend_adapter.rs:141-169
        │         └─ start_with_executor                     src/frontend_adapter.rs:173-216
        └─ [主分支] start_engine_with_offload_inner          src/lib.rs:407-458
             └─ start_qwen3                                 src/frontend_adapter.rs:88-139
                  └─ start_with_executor                    src/frontend_adapter.rs:173-216
                       └─ spawn_scheduler("qwen3-scheduler", …)   src/frontend_adapter.rs:209
   ```
   两条支线最终汇入同一个 `start_with_executor`——这就是 4.4.1 说的泛型收口。
3. **标注分叉点**：LoRA 分叉在 `match options.lora`（src/lib.rs:321）；DFlash 分叉在 `start_qwen3` 内 `if let Some(draft_path)`（src/frontend_adapter.rs:129）——注意 DFlash 不是独立分支，而是主分支上的可选附加步。
4. **审计 dump_graph_png**：`rg -n "dump_graph_png" src/` 应命中五处语义点——CLI 装载（model_line.rs:314）、launch 的三重校验（lib.rs:279-289）、传递（lib.rs:349）、装配期导出（frontend_adapter.rs:112-122）。回答：LoRA 分支是否遗漏了该校验？（提示：不是遗漏——LoRA 臂根本不传此参数，且 L284-L287 已把「图导出 + LoRA」组合拒绝在更早处。）

**需要观察的现象**：`rg` 输出里 LoRA 支线的函数都不接收 `dflash_draft_model_path` / `dump_graph_png` 参数——参数表本身就是分叉语义的可读证据。

**预期结果**：一页带行号的调用链 + 分叉标注 + dump 审计结论。全程无需 GPU、无需编译；若想用调试器在 `start_with_executor` 打断点实证调用顺序，待本地验证（需 GPU 环境）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `start_with_executor` 要做成泛型 `E: ModelExecutor + 'static`，而不是直接收 `Qwen3Executor`？
**答案**：让「调度 + 契约适配」整层可以被非 GPU 替身执行器复用——hf_golden_gate 的 teacher-forcing 回放、模型本地 benchmark、以及 scheduler 单测的 test_support 执行器（[src/scheduler/test_support.rs:462](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/scheduler/test_support.rs#L462) 就有替身对 `has_decode_overlap` 的实现）都从这一泛型边界受益。`'static` 是因为调度器对象要被 move 进 `spawn_scheduler` 的驱动线程。

**练习 2**：`kv_total` 为什么在装配时快照一次，而不是每次 `metrics` 现场向 executor 要总数？
**答案**：`metrics` 里 `kv_used_blocks = kv_total - available_blocks()`（[src/frontend_adapter.rs:836-L839](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L836-L839)）。总块数在 executor 生命周期内固定（池在构建期一次划定），装配时快照（L194，保守取「总数减一」）既便宜又保证 `used ≤ total` 的不变量；空闲数才是每步变化的量。

**练习 3**：`Qwen3Scheduler` 的五个请求队列里，哪一个不参与 `metrics` 的 `num_running_reqs`？为什么？
**答案**：`loading`（以及 `deferred`、`post_control_deferred`）——它们计入 `num_waiting_reqs`（[src/frontend_adapter.rs:840-L846](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/frontend_adapter.rs#L840-L846)）。running 的口径是「占用引擎执行资源」（active 解码 + prefilling 填装 + 在途异步 prefill）；`loading` 只是在等 CPU 层 DMA，没占 GPU 步进位。

## 5. 综合实践

**场景推演：三种启动配置各走哪条路？**

给定三个（虚构但参数真实的）启动命令，只靠源码预测行为，再核对：

| 场景 | 关键 flag | 预测要点 |
|---|---|---|
| A | `--cuda-graph`（无其他） | 走主分支；`enable_cuda_graph=true`；无图导出；三个旋钮只执行①②（均为默认态）；`start_with_executor` 返回单调度器 Engine |
| B | `--lora-modules name=path` + `--cuda-graph` | 走 LoRA 分支；`launch` L270-L278 打 warn 并强制关图；`LoraClient::channel()` 被铸出；`Engine.lora = Some(client)` |
| C | `--dflash-draft-model-path models/draft` + `--dump-graph-png g.png` | 走主分支；构建期先 `ensure_servable` 校验草稿配置并预留显存；装配期先导图再装草稿；`load_dflash_draft_model` 强制关前缀缓存 |

操作步骤：

1. 对每个场景，沿 4.4.4 的调用链逐函数标注「会执行 / 被跳过 / 提前失败」，并指出最终 `Engine` 的 `info`（`kv_capacity`、`servable_len`）与 `lora` 字段取值来源。
2. 为场景 C 额外回答：若用户再叠加 `--no-prefix-cache`，`set_no_prefix_cache(true)` 与草稿装载内部的强制关闭是否冲突？（用 4.3.4 思想实验 b 的结论回答：不冲突，两次都指向「关」。）
3. 有 GPU 与权重时可选实测：场景 A 启动后观察日志里的图捕获信息与 `/metrics`（前缀缓存计数器）；无 GPU 则全部结论标注「源码推理，待本地验证」。

预期结果：一张三列对照表，每列从 `Qwen3Line::launch` 一路写到 `spawn_scheduler`，能向别人五分钟讲清「服务器进程从收到 CLI 参数到驱动线程开转」的全过程。

## 6. 本讲小结

- **契约三件套**：`Engine` 是交给服务器的整车（调度器句柄 + 元数据 + 可选 LoRA 客户端）；`Scheduler` 是被驱动轮询的 `submit`/`step`/`metrics`；`RequestLedger` 是驱动侧拥有、调度器按 `RequestId` 写入的裁定与 token 账本。frontend crate 源码不在本仓库，但契约面可从 `frontend_adapter.rs` 的 import 与调用点完整还原。
- **frontend_adapter.rs 只有两项契约职责**：把每步 effects 翻译成 ledger 写入（`apply_effects`），以及构建 executor 并装配 `Engine` 返回——其余全是 Qwen3 私有机制。
- **启动链条**：`Qwen3Line::launch` → `crate::launch`（策略：设备映射、LoRA 关图、dump 校验、三条提前拒绝、lora 分叉）→ `start_engine_with_offload_inner`（spec_env 校验、batch-invariant 门、全局 NumericPolicy）→ `start_qwen3`（构建 + 三旋钮）→ `start_with_executor`（`servable_len`、LoRA 通道、`Qwen3Scheduler::new`）→ `spawn_scheduler`。
- **三旋钮顺序不可乱**：`set_no_prefix_cache` → `enable_decode_overlap` → `load_dflash_draft_model`；草稿装载会强制关前缀缓存，若排在旋钮①之前会被「最后写入者胜」重新打开；草稿显存之所以敢后装，是因为构建期已按草稿配置预留。
- **取消是响应式的、完成有两种出口**：abort 靠下步顶部的探测点退休；普通 finish 随本步提交，P/D prefill 角色则铸 `DeferredFinish` 延迟到 KV 对同伴可见。
- **`ModelExecutor` 是调度器视角的执行器抽象**，`start_with_executor` 的泛型边界让测试替身与 benchmark 复用整层调度逻辑。

## 7. 下一步学习建议

本讲收官 u1 单元：你已经掌握 crate 定位（u1-l1）、测试地图（u1-l2）、CLI 接缝（u1-l3）与契约装配（本讲）。接下来进入 u2「配置与权重：模型本体」：

- **u2-l1 配置解析**：`start_qwen3` 第 1 步里被我们一笔带过的 `config.json` 校验，正是下一讲的起点；
- **u2-l2/u2-l4**：本讲出现的 `Qwen3MemoryOptions`、`KvCapacity`、`servable_len` 的上游——权重布局与显存画像；
- 若更想先看动态一侧，可直接跳 u3-l1（executor 与 plan/result 契约总览），再回头补 u2；
- 阅读源码时建议带着本讲的综合实践产出（调用链笔记）对照：u2-u4 的每一站都能挂到这条链的某个节点上。
