# GPU 线程模型：RankWorker 与 LocalQwen3Lane

## 1. 本讲目标

u3-l1 里我们看清了 executor.rs 的**相位边界**：调度器与执行器之间只交换纯数据的 plan→result。但当时有一个问题被刻意按下了：`Qwen3Executor` 自己并不直接碰 GPU——它把每一步前向**发到一条独立的 worker 线程**上去执行。本讲拆开这条线程缝。学完本讲，你应该能够：

1. 画出一次 decode step 的**完整消息时序**：调度器线程 → `Qwen3Executor::run_step` → crossbeam 通道 → `RankWorker` 线程 → `execute_step_on_lane` → 响应通道回传 `WorkerStepOutcome`。
2. 解释 `StepCommand` / `WorkerCommand` / `WorkerStepOutcome` **三层通道协议**：谁是"算什么"、谁是"控制面"、谁承载结果；`collect_result` 标志为什么让主 rank 回载荷、其余 rank 只回 `Ack`。
3. 说出 `LocalQwen3Lane` 为什么是"**一条 GPU 车道**"：模型、KV 缓冲、解码缓冲、采样 scratch、草稿器车道为何必须整体住在同一条线程里，`bufs` 字段为什么必须排在 `model` 前面。
4. 解释启动期 **PrecapturePhase 预捕获扫描**为什么把 Capture 与 Launch 拆成两个阶段、为什么配一个 600 秒看门狗。
5. 说明 `bind_model_thread` 与 `CublasThreadGuard` **各自防什么**：CUDA 上下文绑定防什么，cuBLAS 句柄的线程亲和防什么。
6. 理解 `WorkerLost` 错误如何从一条死掉的 worker 线程传播到调度器：单卡为何报错返回、TP 多卡为何直接 `abort()`。

> **版本说明（对应 HEAD af9e40e1）**：EAGLE-3 草稿器落地（commit af9e40e1）把一批 DFlash 专属命名**推广为通用草稿层**。如果你读的是旧文档，对照表如下：
>
> | 旧名（785d0487 及以前） | 现名（af9e40e1） |
> | --- | --- |
> | `WorkerCommand::DropDflash` | `WorkerCommand::DropDraft` |
> | `RankWorker::drop_dflash_request` | `RankWorker::drop_draft_request` |
> | `WorkerCommand::LoadEagle3` | （新增） |
> | `execute_dflash_verify` | `execute_draft_verify`（内部按已装载草稿器分派） |
> | `DraftMeta`（无 `max_draft_batch`） | `DraftMeta`（新增 `max_draft_batch: Option<usize>`） |
>
> `StepCommand::SpeculativeDraft` 的处理逻辑也随之变化：worker 现在按 `lane.eagle3` 是否已装载，把草稿步骤分派到 dflash 或 eagle3 车道。本讲全部按现名讲解。

## 2. 前置知识

- **线程亲和（thread affinity）**：CUDA 的 context 与 cuBLAS 的句柄（handle）都对"创建它的线程"有亲和性。一个 `CUcontext` 要在某条线程上使用，通常需要先绑定到该线程；cuBLAS/cublasLt 的句柄在创建线程之外使用，轻则性能塌陷、重则未定义行为。这就是为什么本讲的全部 GPU 状态都要"搬家进"一条专职线程。
- **请求-响应通道（request-response channel）**：生产者-消费者队列大家都熟；这里用的是 crossbeam 的**有界通道**（`channel::bounded(1)`）拼出一个 RPC 调用——发送方把命令连同"回信信箱"（一个容量为 1 的响应通道 `resp`）一起发过去，然后等回信。命令通道本身是 `unbounded` 的，保证发送永不阻塞。
- **车道（lane）**：本讲对 `LocalQwen3Lane` 的翻译。一台 GPU 上执行 Qwen3 前向所需的**全部**可变状态（模型、KV 缓冲、解码缓冲、采样 scratch、草稿器）打包成一个结构体，整体移交给一条线程独占。单卡 = 1 条车道；TP=N 卡 = N 条车道，每卡一条。
- **TP 最小背景**（u9-l1 展开）：张量并行把每层投影按行/列切到 N 张卡，前向中每个 rank 都要跑 all-reduce（NCCL 集合通信）。集合通信要求**所有 rank 配对出现**——一个 rank 掉队或退出，其余 rank 会永远阻塞在 NCCL 调用里，因为 NCCL 没有设备侧超时。这个事实解释了本讲一半的"怪设计"。
- **投机解码最小背景**（u6 展开）：草稿器（DFlash / EAGLE-3）先便宜地猜若干 token，目标模型一次前向验证。草稿器要读目标模型的嵌入、lm_head 和它在 prefill/verify 时捕获的隐藏态——所以草稿器车道必须与目标模型**同线程共存**（u1-l4 的 `max_draft_batch` 能力位在 u3-l1 已见）。
- **为什么 `Qwen3Executor` 不是一条线程**：它是一个普通结构体，被调度器线程（frontend_adapter 驱动循环）调用。真正的 GPU 工作在它名下的 `RankWorker` 线程里。所以"executor 线程"在本讲指**调用 `Qwen3Executor` 方法的调度器线程**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | 本讲主战场：`LocalQwen3Lane`、`RankWorker`、`StepCommand`/`WorkerCommand`/`WorkerStepOutcome`、`PrecapturePhase`、`CublasThreadGuard`、`WorkerLost`、预捕获扫描与看门狗 |
| [src/executor/eagle3_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs) | EAGLE-3 的 worker 侧车道 `Eagle3LaneState` 与 `LocalQwen3Lane::load_eagle3`（构造 `DraftMeta { max_draft_batch: Some(1) }`） |
| [src/executor/dflash_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs) | DFlash 的 worker 侧车道：`should_capture_dflash_prefill_context`、`execute_dflash_draft`、`drop_dflash_request` 等 lane 方法 |
| [src/executor/dflash_prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs) | `prefill_capture_supported`：DFlash 与 EAGLE-3 共用的"prefill 捕获资格"谓词 |
| [src/speculative.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs) | `DraftModel` 枚举（L35-L38）：按 checkpoint 路径在 DFlash / EAGLE-3 间二选一的纯标签 |
| [src/frontend_adapter.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs) | `WorkerLost` 的消费终点：`absorb_step_failure` 把它当作引擎级致命错误上抛 |

## 4. 核心概念与源码讲解

### 4.1 LocalQwen3Lane：一条 GPU 车道

#### 4.1.1 概念说明

`LocalQwen3Lane` 是"在一台 GPU 上跑 Qwen3 前向"所需的全部状态：目标模型、KV 缓冲、批量解码缓冲、采样 scratch、草稿器车道。它解决的问题是**所有权与线程归属**：CUDA 上下文、cuBLAS 句柄、CUDA Graph 捕获的指针稳定性都要求这些状态被一条线程独占，从不跨线程共享。于是设计上干脆把"这台 GPU 的一切"打包成一个结构体，用 `move` 闭包整体移交给 worker 线程——编译器从此保证没有第二个人能碰它。

注意车道与 executor 的分工（承接 u3-l1）：`Qwen3Executor` 持有的是**请求簿记**（`request_kvs`、prefetch 停车场、`draft_ready_requests` 集合……），车道持有的是**纯 GPU 状态**。executor 在调度器线程上跑，车道在 worker 线程上跑，两者只通过通道说话。

#### 4.1.2 核心流程

车道的生命周期：

1. **构造**（调度器线程）：`LocalQwen3Lane::new` 拿到已装载的 `Qwen3Model` 与 `KvBuffer`，按最大 decode bucket 一次性分配 `BatchDecodeBuffers` 与采样 scratch——此后前向**零临时分配**（这是 u3-l5 CUDA Graph 指针稳定性的前提）。
2. **移交**：lane 被 `move` 进 `RankWorker::spawn` 的闭包，从此只存在于 worker 线程栈上。
3. **绑定**（worker 线程上第一件事）：`lane.bind()` 做 CUDA 设备/上下文绑定、cuBLAS 初始化、decode GEMM 算法预热（4.4 详述）。
4. **服役**：worker 主循环反复对它调用 `execute_step_on_lane`。
5. **预捕获**（仅 TP + CUDA Graph）：启动期对它调用若干次 `precapture_phase`。
6. **随线程终结**：命令通道关闭或收到 `Shutdown` 时，闭包返回，lane 随栈帧 drop。

#### 4.1.3 源码精读

**车道的全部字段**——[src/executor.rs:L3390-L3427](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3390-L3427)。第一眼就该注意 `bufs` 字段上方的注释：**字段顺序是有意的**——`bufs` 必须排在 `model` 之前 drop，因为 NCCL 通信器析构时会轮询直到所有录制过集合通信的 CUDA Graph 都销毁；Rust 结构体按声明顺序逆序 drop，把 `bufs`（持有 decode 图）放前面就保证了这个顺序。这是一个"用字段顺序表达析构约束"的经典手法。

草稿器车道是本 HEAD 的新形态——[src/executor.rs:L3410-L3414](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3410-L3414)：`dflash: Option<DFlashLaneState>` 与 `eagle3: Option<Eagle3LaneState>` 两个字段并存，注释明确"至多一个被装载"。它们住在 lane 里而不是 executor 里，原因见 [src/executor/eagle3_lane.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L1-L6) 的模块文档：草稿器要读目标的嵌入与捕获的隐藏态，**这些隐藏态从不出车道**——草稿/验证边界是纯 token 跨界（u6-l1 的事务核心）。

**构造函数按最大桶预分配**——[src/executor.rs:L3464-L3519](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3464-L3519)：`BatchDecodeBuffers::new` 按 `BATCH_BUCKETS.last()`（最大批）分配（u3-l4 展开），采样 scratch 同样按最大桶宽度建好，unified 注意力计划也在这里预建。注意构造期 lane 尚未绑定线程——它此刻还在调度器线程手里，真正的 GPU 上下文绑定发生在 worker 线程的 `bind()` 里。

**谁构造了车道**：单卡路径在 [src/executor.rs:L1200-L1210](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1200-L1210) 把 rank 0 车道交给 `RankWorker::spawn`，`workers` 留空；TP 路径在 [src/executor.rs:L1524-L1549](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1524-L1549) 为主 rank 与每个额外设备各 spawn 一条车道。**即便单卡，前向也跑在专职线程上**——`Qwen3Executor` 的 `primary` 字段就是 rank 0 的 `RankWorker`（[src/executor.rs:L957-L958](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L957-L958)：`primary: RankWorker` + `workers: Vec<RankWorker>`）。

**verify 缓冲也在车道里懒建**——[src/executor.rs:L3415-L3419](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3415-L3419)：`verify_bufs` 在第一次 verify 步时按已装载草稿器的形状构建（u6-l6 展开）。

#### 4.1.4 代码实践

1. **实践目标**：把 `LocalQwen3Lane` 的字段整理成一张"所有权清单"，体会"一台 GPU 的全部状态"到底包括什么。
2. **操作步骤**：打开 [src/executor.rs:L3390-L3427](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3390-L3427)，逐字段抄录并归类：目标模型类 / KV 数据面类 / decode 缓冲类 / 采样类 / 投机类 / 簿记类（`precapture_complete` 这样的 bool）。再对照 [src/executor.rs:L953-L1029](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L953-L1029) 的 `Qwen3Executor` 字段，标注哪些状态**绝不可能**出现在 lane 里（提示：凡按 `RequestId` 索引的 HashMap 都在 executor 侧，如 `request_kvs`、`draft_ready_requests`）。
3. **需要观察的现象**：两个结构体没有任何交叉字段——lane 不认识 RequestId 级别的簿记，executor 不直接持有任何 `cudarc` 对象（`async_prefill` 的 `CudaEvent` 是唯一例外，它是跨线程传递的纯句柄）。
4. **预期结果**：一张两列对照表，能回答"如果我新增一个每请求的 GPU 缓冲，它应该放哪一侧？如果新增一个全 GPU 的常量表呢？"

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dflash` 与 `eagle3` 是两个字段而不是一个 `Option<DraftLane>` 枚举？

**答案**：两者的车道状态类型完全不同——`DFlashLaneState` 持有块草稿缓冲与 hedge 链几何，`Eagle3LaneState` 持有 `HashMap<RequestId, Eagle3RequestState>` 与单 token scratch（[src/executor/eagle3_lane.rs:L30-L36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L30-L36)）。「至多一个装载」的不变量由装载入口保证（`load_draft_model` 按枚举二选一，见 4.3.3），用两个 `Option` 表达"互斥但类型不同"比包一层枚举更直白；分派点只需 `lane.eagle3.is_some()` 一个判断。

**练习 2**：如果某个未来特性要求"草稿器的隐藏态需要传回 executor 线程"，会遇到什么设计阻力？

**答案**：模块文档（[src/executor/eagle3_lane.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L1-L6)）明确"隐藏态留在车道内、草稿/验证边界是纯 token"。打破它意味着跨线程共享 GPU 指针（通道里传设备缓冲句柄，像 `SplitConcurrent` 传 `SendStream` 那样），要重新论证线程安全、显存所有权与生命周期——现有设计刻意用 token 边界把这一切挡在车道里。

### 4.2 通道协议：StepCommand / WorkerCommand / WorkerStepOutcome

#### 4.2.1 概念说明

调度器线程与 worker 线程之间是一套**消息传递协议**，分三层：

| 层 | 类型 | 方向 | 回答的问题 |
| --- | --- | --- | --- |
| 计算载荷 | `StepCommand` | executor → worker（藏在 `RunStep` 里） | 这一步**算什么** |
| 控制信封 | `WorkerCommand` | executor → worker | 对车道**做什么**（跑一步 / 装卸草稿器 / LoRA / 预捕获 / 关停） |
| 结果载荷 | `WorkerStepOutcome` | worker → executor | 一步**算出了什么** |

`WorkerCommand` 的每个变体都带一个 `resp: channel::Sender<Result<...>>`——**回信信箱**随命令一起寄过去，worker 处理完把结果投回信箱。这就是用两个有界通道拼出的同步 RPC：命令通道 `unbounded` 保证发送不阻塞，响应通道 `bounded(1)` 恰好装一个回信。

#### 4.2.2 核心流程

一次 decode 步的消息流（单卡）：

```
调度器线程                                worker 线程 (qwen3-tp-rank-0)
    |                                            |
    | execute_decode(plan)                        |
    |  构造 StepCommand::Decode{requests,         |
    |    kv_views, sample_seed}                   |
    |  run_step:                                   |
    |  [tx] RunStep{step, collect_result=true,    |
    |        resp=resp_tx} ----------------------> | rx.recv()
    |                                            | execute_step_on_lane(lane, step, true)
    |                                            |   前向 + 采样
    |  resp_rx.recv() <-------------------------- | [resp] Ok(WorkerStepOutcome::Decode(..))
    |  apply_decode(result)                        |
```

TP 时 `run_step` 会把**同一个 step 克隆**发给每个 rank：主 rank `collect_result=true`，其余 rank `false`——非主 rank 只需推进 NCCL 集合通信配对，不需要（也不能）各自产出一份结果，所以它们回 `Ack`。

#### 4.2.3 源码精读

**`StepCommand`：六种计算载荷**——[src/executor.rs:L4177-L4222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4177-L4222)：`Prefill` / `Decode` / `Unified`（对应 u3-l1 的三组 plan）、`SplitConcurrent`（decode-overlap 双流步，注意它把两条 `SendStream` **随命令寄进** worker——流是调度器线程创建、worker 线程使用的跨线程句柄）、`SpeculativeVerify`（目标一次前向验证 K+1 草稿 span）与 `SpeculativeDraft`。`SpeculativeDraft` 的文档（L4219-L4221）在本 HEAD 已推广：'roll the loaded draft model forward one round per request'，且强调"用草稿器自己的 KV——不带目标 KV 视图"。

**`WorkerCommand`：控制面全目录**——[src/executor.rs:L4237-L4290](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4237-L4290)。除 `RunStep` 外还有：LoRA 三命令（Load/Unload/Discard，u7-l2）、`ResolvePrefill`（取回上一步在重叠流上尚未同步的 prefill 结果）、**三条草稿器命令**——`LoadDflash`（L4262-L4267，注释说明"在 worker 线程上构建，因为要读同车道的目标模型"）、新增的 `LoadEagle3`（L4268-L4272，同样理由）、推广后的 `DropDraft`（L4273-L4278：请求退役或退回纯解码时丢弃其草稿态）、`Precapture`（L4279-L4284）、`DumpDecodeGraph` 与 `Shutdown`。

**`WorkerStepOutcome`：七种结果**——[src/executor.rs:L4312-L4342](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4312-L4342)：`Ack`（非主 rank / 无需收集结果的步）、三组 plan 对应的载荷、`SplitDecodeReady{decode, prefill_event}`（decode 已好、prefill 还在另一条流上飞，附一个 `CudaEvent` 供查询）、`SpeculativeVerify` 与 `SpeculativeDraft`。

**worker 侧的分派函数 `execute_step_on_lane`**——[src/executor.rs:L270-L274](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L270-L274)。它的 Prefill 分支最能体现"车道内分派"：[src/executor.rs:L286-L314](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L286-L314) 先算 `capture_requested = should_capture_dflash_prefill_context(...) || should_capture_eagle3_prefill_context(...)`——**两种草稿器的捕获资格取或**；捕获层号由 `lane.draft_capture_layer_ids()`（[src/executor.rs:L3618-L3625](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3618-L3625)，先问 dflash、否则用 eagle3 的 `aux_layers`）统一供给目标前向；捕获结果的记账再按 `lane.eagle3.is_some()` 分派到 `record_prefill_eagle3_context` 或 `record_prefill_dflash_context`，产出统一的 `draft_context_captured_requests`（u3-l1 见过的 `PrefillResult` 字段）。

`SpeculativeDraft` 的分派在同一函数尾部——[src/executor.rs:L534-L541](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L534-L541)：`if lane.eagle3.is_some() { execute_eagle3_draft } else { execute_dflash_draft }`。而 `SpeculativeVerify` 已统一为 [src/executor.rs:L530-L532](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L530-L532) 的 `execute_draft_verify`，其在 [src/executor.rs:L3992-L4006](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3992-L4006) 内部按已装载草稿器选捕获层、verify span 与最大批（DFlash 用 `effective_drafts()+1` 与全桶批；EAGLE-3 用 `EAGLE3_CHAIN_LENGTH+1` 与 `EAGLE3_MAX_BATCH`）。

**crossbeam 别名**——[src/executor.rs:L10](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L10)：`use crossbeam_channel as channel;`，全文件的 `channel::bounded` / `channel::unbounded` / `channel::Select` 都出自这里。

#### 4.2.4 代码实践

1. **实践目标**：用纯 CPU 单测验证通道协议的"死 rank 检测"，感受响应通道关闭如何变成错误。
2. **操作步骤**：运行 `cargo test --release -p pegainfer-qwen3 --lib a_dead_peer -- --nocapture`。这两个测试（[src/executor.rs:L3315-L3329](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3315-L3329)）不需要 GPU：它们手工造出"主 rank 阻塞 + 对端已死"与"主 rank 已死"两组通道，断言 `await_step_responses` 在 5 秒内报告正确的 rank 编号而不是挂死。
3. **需要观察的现象**：测试通过时间应在毫秒级——`Select` 在通道关闭的瞬间就能醒，无需轮询。
4. **预期结果**：两个测试通过；如果 5 秒超时会 panic 并提示 "a closed rank channel must end the wait"。（无 GPU 环境可正常运行，属 `--lib` 单测。）

#### 4.2.5 小练习与答案

**练习 1**：为什么命令通道用 `unbounded`，而每个响应通道用 `bounded(1)`？

**答案**：命令通道（[src/executor.rs:L4374](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4374)）必须永不阻塞发送方——TP 下 `run_step` 要先给所有 rank 发完命令才开始等响应（保证集合通信配对），若发送会阻塞，一个慢 rank 会让发送方在错误的位置卡住。响应通道每次请求新建一个（如 [L4466](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4466)），且一条命令恰好对应一个回信，容量 1 足够；用完即弃，不需要缓冲积压。

**练习 2**：`StepCommand::SplitConcurrent` 为什么要随命令传两条 `SendStream`，而 `Decode` 不用传流？

**答案**：`Decode` 的前向在车道的**默认计算流**上跑（车道创建时就持有）；`SplitConcurrent` 是 decode-overlap 特性（u3-l7），prefill 与 decode 要在**两条不同的流**上并发——流由 executor 侧的 `OverlapStreams`（green_ctx）按 SM 分区创建，与默认流不同源，所以必须作为跨线程句柄寄进 worker（[src/executor.rs:L4197-L4205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4197-L4205)）。`SendStream` 由此得名——它就是把不可 `Send` 的原生流包成可过通道的封装。

**练习 3**：`DropDraft` 的处理为什么同时调用 `drop_dflash_request` 和 `drop_eagle3_request`（[src/executor.rs:L4424-L4428](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4424-L4428)），而不是按已装载的那个调用？

**答案**：两个方法内部都是按 `request_id` 从各自的 HashMap/状态表里移除，未装载时是空操作。统一调用让 `DropDraft` 成为"无论装的是哪个草稿器都正确"的通用清理命令——这正是把 `DropDflash` 推广为 `DropDraft` 的实现基础：调用方（executor 的 retire/回退路径）不需要知道车道里装的是什么。

### 4.3 RankWorker：spawn 主循环、启动预捕获与请求-响应 API

#### 4.3.1 概念说明

`RankWorker` 是"一条车道 + 一条线程"的**句柄**：对 executor 来说它是一个 RPC 客户端（`tx` 发命令、各方法等回信），对线程来说它是所有者（`JoinHandle`）。它解决的问题是：**把一条线程包装成一组类型安全的阻塞方法**——`run_step`、`load_dflash`、`load_eagle3`、`drop_draft_request`、`precapture`……每个方法内部都是同一个模式：建回信通道 → 发命令 → `recv` 等待。

worker 线程的主循环是一个彻底的**单线程事件循环**：`while let Ok(cmd) = rx.recv()`——没有锁、没有共享可变状态、没有唤醒机制。所有并发问题都被通道序列化了：同一条车道上的命令天然按序执行。

#### 4.3.2 核心流程

**spawn 握手与主循环**：

```
executor 线程                        worker 线程 (qwen3-tp-rank-N)
    |                                      |
    | RankWorker::spawn(rank, lane)        |
    |  [tx, rx] = unbounded()              |
    |  [startup_tx, startup_rx] = bounded(1)
    |  thread::spawn(move || { ----------> |  lane.bind()
    |                                      |   ├─ Ok(guard) → startup_tx.send(Ok(()))
    |  startup_rx.recv()                   |   │    then while let Ok(cmd) = rx.recv() { … }
    |   ├─ Ok(()) → 服役                   |   └─ Err(e)  → startup_tx.send(Err(e))，线程退出
    |   └─ Err  → join 取 panic 信息并 bail |
```

**TP 启动预捕获扫描**（仅 `enable_cuda_graph` 且 decode GQA 组已编译时）：

```
for 每个 phase in [Warmup,
                   (Capture{b}, Launch{b}) for b in 0..BATCH_BUCKETS.len(),
                   Finalize]:
    把 phase 先发给【所有】rank（fan-out，拿到各自的 ack 接收器）
    按 rank 顺序逐个收 ack；任何一个失败 → 报错并点名尚未收齐的 rank
```

看门狗线程并行倒数 600 秒，扫不完就 `abort()`。

#### 4.3.3 源码精读

**`RankWorker` 结构**——[src/executor.rs:L4367-L4370](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4367-L4370)：就两个字段——命令通道发送端与可取出的 `JoinHandle`。

**spawn 的启动握手**——[src/executor.rs:L4373-L4459](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4373-L4459)：线程命名 `qwen3-tp-rank-{rank}`（L4376-L4377，方便 perf/top/pprof 归因）；闭包第一件事 `lane.bind()`（L4379）——**绑定失败绝不进入主循环**，错误经 startup 通道传回；executor 侧（L4447-L4453）在 startup 收到 `Err`（通道关闭意味着线程已死）时 `join` 拿回 panic 载荷拼进报错。主循环（L4383-L4439）逐命令分派，`Shutdown` break、通道关闭（发送端全灭）自然退出。三条草稿器命令的臂：`LoadDflash`（L4416-L4419）、`LoadEagle3`（L4420-L4423）、`DropDraft`（L4424-L4428，见练习）。

**请求-响应方法群**：`run_step`（[L4461-L4475](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4461-L4475)）**返回接收器而不阻塞**——TP 下要先给所有 rank 发完再一起等；`load_dflash`（[L4512-L4525](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4512-L4525)）与 `load_eagle3`（[L4527-L4540](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4527-L4540)）则**阻塞到装载完成**并回传 `DraftMeta`；`drop_draft_request`（[L4542-L4554](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4542-L4554)）阻塞到清理确认。每个方法的发送失败都映射成各自的可读错误（"worker channel closed on load_eagle3" 等）。

**executor 侧的扇出与聚合**——[src/executor.rs:L2243-L2257](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2243-L2257)：`Qwen3Executor::run_step` 先 `primary.run_step(step.clone(), true)`，再循环 `worker.run_step(step.clone(), false)`，最后 `await_step_responses` 同时盯住主 rank 与所有对端（[L2264-L2317](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2264-L2317)，crossbeam `Select` 多路等待）。对端只允许回 `Ack`，回了载荷或错误都记为 `peer_error`；任何通道关闭立刻返回该 rank 编号——**一个 rank 死在半步中，其余 rank 可能卡在未配对的集合通信里，所以必须马上发现**（L2260-L2263 的文档注释）。

**预捕获扫描**——[src/executor.rs:L1551-L1623](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1551-L1623)：先起看门狗（L1560-L1579，`qwen3-tp-precapture-watchdog`，600 秒 `recv_deadline`，超时打印后 `std::process::abort()`——注释解释为何不用 exit：cudart 的 atexit 析构会抢同一把已楔死的驱动锁）；`run_phase`（L1581-L1610）把一个 phase **先发给所有 rank 再按序收 ack**——"fan-out 后收集"正是为了让集合通信配对；phase 序列（L1611-L1616）为 Warmup → 每桶 {Capture, Launch} → Finalize。

**为什么 Capture 与 Launch 必须分相**——[src/executor.rs:L4292-L4310](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4292-L4310) 的 `PrecapturePhase` 文档写得很清楚：已捕获图里的集合通信**首次启动会阻塞等它的对端**；若某 rank 还在捕获/实例化/上传（这些操作争抢驱动锁且分配显存），与另一个 rank 的启动重叠就会**把驱动锁死**。所以"所有 rank 先捕完一个桶，再一起启动它"。车道侧实现 `precapture_phase` 在 [L3523-L3548](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3523-L3548)：Warmup 做一次真实 all-reduce（让 NCCL 按该消息尺寸选好算法并建连，**在图录制它之前**）；Capture/Launch 用合成行（token 0、位置 0、共享 padding 页）跑 `precapture_decode`（[L3550-L3570](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3550-L3570)），Capture 的 ack 等到异步 `cuGraphUpload` 落地、Launch 的 ack 等到集合通信排空；Finalize 核验每个桶的图确实已捕获并置 `precapture_complete = true`。

**草稿器装载的上层入口**——[src/executor.rs:L1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827)：`load_draft_model` 先断言单卡（`workers.is_empty()`）且无 KV offload，再按 `DraftModel` 枚举（[src/speculative.rs:L35-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L35-L38)）调 `primary.load_dflash` 或 `primary.load_eagle3`——注意这是**对 rank-0 worker 的阻塞 RPC**，真正的模型装载发生在 worker 线程上（EAGLE-3 侧在 [src/executor/eagle3_lane.rs:L57-L75](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L57-L75)，构造的 `DraftMeta` 带 `max_draft_batch: Some(EAGLE3_MAX_BATCH)`；DFlash 侧在 [src/executor.rs:L3593-L3616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3593-L3616)，报 `max_draft_batch: None`）。回传的 meta 存进 executor 的 `speculative`，随后强制关前缀缓存、装计数器（u1-l4 讲过的顺序）。

**关停**——[src/executor.rs:L4598-L4607](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4598-L4607)：发 `Shutdown`、join；worker 线程 panic 也只警告不传染。`Qwen3Executor::drop`（[L3363-L3370](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3363-L3370)）先关 primary 再逐个关 workers——顺序与 spawn 相反。

#### 4.3.4 代码实践

1. **实践目标**：写出 TP=2、`BATCH_BUCKETS.len()=3` 时预捕获扫描的完整消息时序，验证"fan-out 后收集"与"Capture/Launch 分相"。
2. **操作步骤**：阅读 [src/executor.rs:L1581-L1616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1581-L1616) 与 [L4479-L4488](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4479-L4488)，按时间轴列出：每个 phase 两行（发给 rank0/rank1 的 `Precapture` 命令）+ 两行（rank0/rank1 的 ack）。统计总命令数（应为 \(1 + 2 \times 3 + 1 = 8\) 个 phase × 2 rank = 16 条命令）。再标注：若 rank1 在 `Capture{1}` 的 ack 处失败，[L1600-L1607](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1600-L1607) 会点名哪些 rank"可能楔死在 NCCL 集合通信里"。
3. **需要观察的现象**：`BATCH_BUCKETS` 的真实长度可在 [src/batch_decode.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/batch_decode.rs) 中查到（`BATCH_BUCKETS` 常量，u3-l4 展开）——用它替换假设值重算命令数。
4. **预期结果**：一张双泳道时序图；能口头回答"为什么 Warmup 必须先于任何 Capture"（NCCL 先按消息尺寸选算法建连，图里录到的才是定型的集合通信）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `run_step`（RankWorker 方法）返回 `Receiver` 而其他命令的方法直接阻塞返回结果？

**答案**：TP 下所有 rank 的命令必须**全部发出后**才能开始等待（[src/executor.rs:L2243-L2257](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2243-L2257)）——集合通信的启动在 worker 收到命令后立即发生，若发完 rank0 就阻塞等它，rank1 的命令可能还没寄到，两边在 NCCL 里的等待窗口错开，白白拉高尾延迟。LoRA/草稿器装载没有跨 rank 配对需求，阻塞式最简单。

**练习 2**：启动握手为什么用一个专门的 `startup_tx/startup_rx`，而不是往命令通道里发一条"绑定完成"命令？

**答案**：绑定失败时 worker 线程**根本不会进入主循环**（[L4379-L4383](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4379-L4383)）——若复用命令通道报告，失败路径就得"先进入循环再退出"，而且 executor 无法区分"还在绑定"与"绑定失败线程已死"。专门的 startup 通道让"线程活着且绑定成功"成为进入主循环的前置事实；startup 收到 `Err`（发送端 drop）时 executor 还能 `join` 提取 panic 消息（L4447-L4453）。

**练习 3**：看门狗为什么 `abort()` 而不是 `exit()`？

**答案**：[L1556-L1559](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1556-L1559) 的注释：楔死的 NCCL 握着驱动锁，`exit()` 触发的 cudart atexit 析构要抢同一把锁，进程反而挂死在退出路上；`abort()` 立即终结。同款取舍也出现在 `rank_lost` 与 `DecodeStreamGuard`（4.4）。

### 4.4 CublasThreadGuard、bind_model_thread 与 WorkerLost 传播

#### 4.4.1 概念说明

这一节回答两个"守卫"问题和一个"后事"问题：

- **`bind_model_thread`**：worker 线程开工前的**入籍手续**——把 CUDA 设备设为当前、把模型所在的 `CUcontext` 绑定到本线程、在本线程初始化 cuBLAS。它防的是：线程用错设备、用了一个未绑定的上下文（驱动会另建默认上下文，与模型上下文分裂）、以及 cuBLAS 句柄被"别的线程的句柄"顶替。
- **`CublasThreadGuard`**：一个零尺寸标记类型，唯一意义是它的 `Drop`——worker 线程退出时（无论正常返回还是 panic 展开栈）**由创建 cuBLAS 句柄的同一条线程销毁它**。它防的是：句柄泄漏，以及进程退出时驱动析构撞上未释放句柄导致的挂死/告警。
- **`WorkerLost`**：worker 线程死亡（panic 或退出）后的"不可服役"信号。它防的是：调度器对着一条死线程继续发命令、每一步都超时或挂死。

#### 4.4.2 核心流程

线程绑定与守卫的配对：

```
worker 线程启动
  └─ lane.bind()
       ├─ bind_model_thread(&model)      # cuda_set_device → ctx.bind_to_thread → cublas_init
       ├─ let guard = CublasThreadGuard; # 标记：本线程拥有 cuBLAS 句柄
       └─ tune_decode_gemm_algos(...)    # 预热/调优 decode GEMM 算法
  ……服役（主循环）……
  线程退出（Shutdown / panic / 通道关闭）
  └─ guard.drop() → cublas_destroy()     # 同线程销毁句柄
```

WorkerLost 的传播链（单卡）：

```
worker 线程 panic → 主循环消失 → 命令通道的 rx 消失
  → executor 侧 tx.send 失败 / resp 通道关闭
  → worker_lost("rank N …") 错误
  → 调度器 step 返回 Err
  → frontend_adapter::absorb_step_failure 见 WorkerLost::caused → 不吸收、整错上抛
  → 驱动线程把引擎关停
```

TP 多卡时这条链在第一步就改道 `process::abort()`。

#### 4.4.3 源码精读

**`CublasThreadGuard` 的全部实现**——[src/executor.rs:L545-L553](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L545-L553)：三行。零字段结构体 + `Drop` 里调 `cublas_destroy`。它作为 `bind()` 的返回值被 worker 闭包持有（[L4379-L4381](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4379-L4381)）——**生命周期 = 线程生命周期**，这是 RAII 教科书式用法：把清理动作锚定在栈对象的析构上，panic 展开也会执行。

**`bind_model_thread` 的三步入籍**——[src/executor.rs:L555-L575](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L555-L575)：

1. `cuda_set_device(ordinal)`——本线程的当前设备设为模型所在卡（防"线程默认设备是 0 号卡"的静默错误）；
2. `ctx.bind_to_thread()`——把模型创建时所在的 `CUcontext` 绑到本线程（防驱动为该线程另立默认上下文，导致后续分配/内核落在错误上下文）；
3. `cublas_init()`——在本线程初始化 cuBLAS 句柄（cublasLt 的句柄与工作区有线程亲和；这也是后面 `tune_decode_gemm_algos` 能在同线程选算法的前提）。

**车道的 `bind` 组合两者**——[src/executor.rs:L3627-L3632](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3627-L3632)：`bind` = `bind_model_thread` + 生成守卫 + `tune_decode_gemm_algos`（Pin 策略下要**在图捕获前**把 pin 工作区预分配好——注释与 u8-l1 呼应：懒分配在图捕获中是非法的）。

**临时画像线程也走同一套手续**——[src/executor.rs:L2319-L2346](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2319-L2346)：`profile_kv_budget_on_worker` 为启动期显存画像临时 spawn 一条线程（`qwen3-memory-profile-dev{N}`），同样 `bind_model_thread` + 守卫 + 调优，画像完 `join` 把模型还回来。这证明绑定手续不是 worker 专属，而是**任何要碰这个模型的线程**的入场券——TP 路径正是用它逐 rank 画像再取最小块数（[L1450-L1454](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1450-L1454) 注释）。

**`WorkerLost` 与 `rank_lost` 的分岔**——[src/executor.rs:L4344-L4365](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4344-L4365) 定义错误类型与 `caused` 探测（沿 `anyhow` 错误链找本类型）；[L2233-L2241](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2233-L2241) 的 `rank_lost` 是分岔点：**有对端（TP）→ `log::error!` + `std::process::abort()`**——注释说明同伴可能已楔死在 NCCL，且 join 它们、释放显存、cudart 析构全都阻塞，abort 是唯一出路；**单卡 → 包装成 `WorkerLost` 错误返回**。消费终点在 [src/frontend_adapter.rs:L380-L395](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L380-L395)：`absorb_step_failure` 对普通执行错误只失败受影响请求（引擎继续活），但 `WorkerLost::caused(&error)` 为真时**直接上抛**——没有可服役的执行器，引擎整体关停。4.2.4 跑过的两个单测正是在测这条链的"发现"环节。

#### 4.4.4 代码实践

1. **实践目标**：写一段分析，说明 `bind_model_thread` 与 `CublasThreadGuard` **各自防什么**（这是本讲综合实践的第二半，这里先做精读版）。
2. **操作步骤**：对照 [src/executor.rs:L545-L575](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L545-L575) 与 [L3627-L3632](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3627-L3632)，列出三步绑定各自对应的故障模式（用错设备 / 上下文分裂 / 句柄线程亲和），再列出守卫的 Drop 覆盖的三种退出路径（正常 Shutdown、panic 展开、通道关闭后闭包返回）。检查 [L2319-L2346](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2319-L2346) 的画像线程作为第二个调用点。
3. **需要观察的现象**：`bind_model_thread` 全文件只有两个直接调用点——车道的 `bind`（worker 启动时经 [L4379](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4379) 调用）与画像线程闭包；`CublasThreadGuard` 的构造也只出现在这两处——**每个碰模型的线程都恰好一次绑定 + 恰好一个守卫**。
4. **预期结果**：一段引用行号的分析文字（综合实践中给出参考版本）。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `ctx.bind_to_thread()`，最先出问题的会是什么？

**答案**：worker 线程首次发起 CUDA 调用时，驱动会发现该线程没有绑定上下文而**隐式创建/选取一个当前上下文**——它未必是模型装载时所用的那个（多卡时几乎必然不是），此后内核与分配落在另一个上下文里，表现为"找不到已分配的缓冲"之类的诡异错误，而不是明确的绑定失败。`bind_model_thread` 把这件事变成显式的、可报错的手续。

**练习 2**：`WorkerLost` 为什么不实现成 `std::process::exit()` 直接了断，而是单卡走错误返回？

**答案**：单卡场景死的是唯一一条 GPU 线程，调度器、HTTP 前端、其他请求的账本（ledger）都还健康——正确行为是**把致命错误交给驱动线程做有序关停**（给在途请求回错误、释放资源），而不是替全进程做决定。`abort()` 只保留给 TP——那里"有序"已经不可能（同伴楔死在 NCCL，任何 join/析构都会挂）。这正是 [L2233-L2241](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2233-L2241) 按 topology 分岔的原因。

**练习 3**：`Drop for Qwen3Executor`（[L3363-L3370](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3363-L3370) 之前还有 `primary.shutdown()` 逐个 join）与 `CublasThreadGuard` 是什么关系？

**答案**：两层退出机制各管一段：`shutdown` 发 `Shutdown` 命令并 join 线程——负责**让主循环体面结束**（当前步执行完、命令排空）；守卫的 Drop 在线程栈展开时执行——负责**线程无论怎么结束（包括 panic）都销毁 cuBLAS 句柄**。前者是协议层关停，后者是资源层兜底，缺一不可。

## 5. 综合实践

把本讲的三条线拧成一个任务：**画一张覆盖"启动 + 草稿器装载 + 一次 decode step"的全景时序图，并附一段线程守卫分析**。

**任务 A：全景时序图**。三个参与者泳道：`调度器线程（Qwen3Executor）`、`rank-0 worker 线程`、（可选）`rank-1 worker 线程`。按序画出：

1. **启动段**：`RankWorker::spawn` 握手（startup 通道）→ （TP 时）PrecapturePhase 扫描 `Warmup → (Capture{b}, Launch{b})* → Finalize`，每相"两发两收"，看门狗并行倒数（引用 [L1551-L1623](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1551-L1623)）。
2. **草稿器装载段**：`load_draft_model(DraftModel::Eagle3(path))` → `WorkerCommand::LoadEagle3` → worker 侧 `lane.load_eagle3` → `DraftMeta{max_draft_batch: Some(1)}` 回传 → executor 关前缀缓存、置 `speculative`（[L1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827)）；同一段再画 DFlash 版与请求退役时的 `DropDraft`（[L4542-L4554](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4542-L4554)）。
3. **decode step 段**：`execute_decode` 构造 `StepCommand::Decode`（[L2840](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2840) 附近）→ `run_step` 扇出（primary `collect_result=true`，对端 `false`）→ `await_step_responses` 聚合 → `WorkerStepOutcome::Decode` → `apply_decode`。在 step 段边上标一条"worker panic"的虚线分支：通道关闭 → `rank_lost` → 单卡 `WorkerLost` / TP `abort()` → `absorb_step_failure` 上抛。

**任务 B：守卫分析**（约 300 字，引用行号）。参考论点：`bind_model_thread`（[L555-L575](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L555-L575)）防的是**使用期**的三种错位——线程当前设备错、上下文未绑定被驱动隐式另立、cuBLAS 句柄缺线程亲和；`CublasThreadGuard`（[L545-L553](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L545-L553)）防的是**退出期**的句柄悬空——以 RAII 保证"谁创建谁销毁"，覆盖正常关停、panic 展开与通道关闭三条退出路径；两者经 `bind`（[L3627-L3632](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3627-L3632)）打包成"线程入籍"手续，临时画像线程（[L2319-L2346](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2319-L2346)）同样遵守。

**验证方式**：图中每条消息应能在源码中找到对应的 `send`/`recv` 行号；若某条找不到，说明画错了（或源码变了，重查 HEAD af9e40e1）。绘图工具随意（mermaid、纸笔照片均可）；本任务不需要 GPU——`--lib` 单测（4.2.4）可单独运行验证协议行为，其余为源码阅读型实践。

## 6. 本讲小结

- **车道即线程**：`LocalQwen3Lane` 把一台 GPU 的全部可变状态（模型、KV/解码缓冲、采样 scratch、dflash/eagle3 草稿车道）打包进一条独占线程；`bufs` 排在 `model` 前是用字段顺序表达 NCCL 析构约束。
- **三层通道协议**：`StepCommand` 是算什么（六种载荷，`SplitConcurrent` 连流一起寄）、`WorkerCommand` 是控制面（含 `LoadDflash`/`LoadEagle3`/`DropDraft` 三条草稿器命令）、`WorkerStepOutcome` 是结果；命令通道 `unbounded`、回信通道 `bounded(1)` 拼出 RPC。
- **一个 step 的扇出-聚合**：主 rank `collect_result=true` 回载荷、对端回 `Ack`；`await_step_responses` 用 `Select` 同时盯所有 rank，通道一关立刻点名——因为死 rank 会让同伴楔死在 NCCL。
- **启动预捕获**：`PrecapturePhase` 把"捕一个桶"与"启动一个桶"拆成全局分相（Warmup → 每桶 Capture+Launch → Finalize），配 600 秒看门狗 `abort()`；Capture/Launch 重叠会锁死驱动。
- **线程入籍手续**：`bind_model_thread`（设设备 → 绑上下文 → cuBLAS init）+ `CublasThreadGuard`（Drop 时同线程销毁句柄）+ GEMM 预热，任何碰模型的线程都要走一遍（worker 与临时画像线程）。
- **WorkerLost 的两种后事**：单卡包装成错误逐层上抛、`absorb_step_failure` 判定致命并关停引擎；TP 直接 `abort()`——有序退出在同伴楔死 NCCL 时已不可能。

## 7. 下一步学习建议

线程骨架立起来之后，下一讲沿着 `execute_step_on_lane` 的分派往下钻：

- **u3-l3（prefill 前向）**：`StepCommand::Prefill` 在车道内展开成完整的算子序列与 KV 写入点，`PrefillBuffers`/`PrefillTempBin` 的分配回收就发生在本讲看到的函数体里。
- **u3-l4（批量解码前向）与 u3-l5（CUDA Graph）**：decode 步的桶凑整、图捕获为何要求 `BatchDecodeBuffers` 一次性预分配——本讲 4.1 埋的"零临时分配"伏笔在此兑现。
- **u3-l7（decode-overlap）**：`SplitConcurrent` 与 `SplitDecodeReady{prefill_event}` 的双流细节。
- **u6-l1 / u6-l8**：`SpeculativeDraft`/`SpeculativeVerify` 的车道内实现——`execute_eagle3_draft` 与 `execute_draft_verify` 的 span 选择（本讲 4.2.3 已见其分派点）。
- 若想先看错误传播的下游，直接读 [src/frontend_adapter.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs) 的 `absorb_step_failure` 与 u4-l4 的生命周期管理。
