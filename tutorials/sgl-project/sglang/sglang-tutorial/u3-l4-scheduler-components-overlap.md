# 调度组件与 CPU-GPU 重叠

## 1. 本讲目标

在 u3-l1 里我们打开了 Scheduler 这个黑盒，看到它的事件循环把请求编排成批、交给 GPU 前向；在 u3-l2 里我们看清了 `Req` 与 `ScheduleBatch` 这些数据结构。但当时有一个细节被刻意略过：`run_event_loop` 里提到的 **「overlap 模式 / 零开销调度器」** 究竟是怎么做到的？Scheduler 又是怎么把几十个职责拆成一个个「组件」的？

本讲学完后你应该能够：

- 说出 `scheduler_components/` 目录下每个组件的职责，并理解 SGLang 用「组合而非继承」拆分 Scheduler 的设计取向。
- 画出 `event_loop_normal`（串行）与 `event_loop_overlap`（重叠）两种事件循环在一次迭代里的 CPU/GPU 时间线，讲清重叠为何能把 CPU 调度开销「藏」到 GPU 计算背后。
- 看懂 `overlap_utils.py` 的 `FutureMap` 如何用「池索引的 GPU 缓冲 + 流水线中转」跨迭代传递数据，避免每轮 D2H 同步。
- 理解 `output_streamer` 与 `metrics_reporter` 这两个横切组件如何把前向结果流式发回 Detokenizer、并周期性上报指标。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么需要多进程和重叠。** 推理服务的每一次「生成一个 token」其实分成两类工作：CPU 上的调度、组批、张量准备、结果解码（**调度开销**），以及 GPU 上的注意力与前向（**计算开销**）。如果老老实实串行执行，GPU 在 CPU 忙的时候就空转，CPU 在 GPU 忙的时候就干等。SGLang 的目标之一是让 GPU 尽量不停——也就是 README 里反复强调的「零开销调度器（zero-overhead scheduler）」。

**CUDA Stream 是重叠的基础。** PyTorch（CUDA）里每个 stream 是一条「异步命令队列」：你往 stream A 里塞一堆算子、往 stream B 里塞另一堆，它们可以真正并发地跑在 GPU 上（或 GPU 与 CPU 之间）。SGLang 的 overlap 模式就是开两条 stream：一条叫 `schedule_stream`，专门做 CPU 侧的张量准备和结果处理；一条叫 `forward_stream`，专门做 GPU 前向。把上一批的结果处理放到 `schedule_stream`、和「当前正在 GPU 上跑的前向」错开，就实现了重叠。

**结果「延后一轮」是代价。** 想让 N+1 批的 CPU 准备和 N 批的 GPU 前向重叠，最简单的办法是：本轮先启动 N 批的前向，再去处理 N-1 批的结果。于是结果的处理被整体「延后了一个迭代」——这正是 overlap 模式要维护一个 `result_queue`、并在输出侧处理「多发一个延迟 token」的根本原因。

> 名词速查：**WAR 屏障（Write-After-Read barrier）** —— 当两条 stream 都要访问同一块显存（比如统一的 KV 内存池）时，必须保证「先读完再写」。SGLang 用一个事件屏障把「下一轮的写入」挡在「上一轮前向的读取」之后，否则会数据竞争。

## 3. 本讲源码地图

本讲涉及的关键文件与作用：

| 文件 | 作用 |
| --- | --- |
| `python/sglang/srt/managers/scheduler_components/` | 整个目录：Scheduler 的模块化组件集合，每个文件一个职责清晰的组件。 |
| `python/sglang/srt/managers/scheduler_components/ipc_channels.py` | `SchedulerIpcChannels`：把 Scheduler 用到的 ZMQ 套接字打包成一个不可变（frozen）结构。 |
| `python/sglang/srt/managers/scheduler_components/output_streamer.py` | `SchedulerOutputStreamer`：把前向产出的 token 批打包成 `BatchTokenIDOutput`，经 ZMQ 流式发回 Detokenizer。 |
| `python/sglang/srt/managers/scheduler_components/metrics_reporter.py` | `SchedulerMetricsReporter`：采集每批的 prefill/decode 指标，周期性日志打印并更新 Prometheus 指标。 |
| `python/sglang/srt/managers/scheduler_components/idle_sleeper.py` | `IdleSleeper`：服务空闲时用 `zmq.Poller` 休眠以省电、降温。 |
| `python/sglang/srt/managers/overlap_utils.py` | overlap 模式的核心：`FutureMap` 及一组 resolve/publish/stash 辅助函数，跨迭代中转 GPU 张量。 |
| `python/sglang/srt/managers/scheduler.py` | `Scheduler` 类本身。本讲重点读其中的 `run_event_loop`、`event_loop_normal`、`event_loop_overlap`、`is_disable_overlap_for_batch`、`run_batch` 与组件初始化段落。 |

> 说明：讲义大纲里把 overlap 源码标为 `python/sglang/srt/overlap_utils.py`，实际路径在 `managers` 子目录下，即 `python/sglang/srt/managers/overlap_utils.py`，本讲以真实路径为准。

---

## 4. 核心概念与源码讲解

### 4.1 scheduler_components 目录概览与组合模式

#### 4.1.1 概念说明

随着项目演进，`Scheduler` 类要做的事越来越多：收请求、组批、算预算、跑前向、收结果、发回 Detokenizer、采指标、刷缓存、更新权重……如果把所有逻辑都塞进 `scheduler.py`，这个文件会变成几千行的「上帝类」。SGLang 的做法是：**把可独立的职责拆成一个个小组件类，Scheduler 以「持有并调用」的方式组合它们**（explicit composition）。

这正是项目代码规范 `general-code-style.md` 里强调的取向——「避免 mixin，优先显式组合（hold a collaborator and call it）」。所以你会看到 `scheduler_components/` 目录下的每个文件几乎都是一个小类，构造时接收它需要的那几个值（很多以 `lambda: self.last_batch` 这种回调形式注入，避免把整个 Scheduler 当作「上帝对象」传进去）。

#### 4.1.2 核心流程

Scheduler 在 `__init__` 与一系列 `init_*` 方法里逐个创建组件，并保存为 `self.<name>`：

```
Scheduler.__init__
  ├── init_ipc_channels()        → self.ipc_channels        (SchedulerIpcChannels)
  ├── init_metrics_reporter()    → self.metrics_reporter    (SchedulerMetricsReporter)
  ├── init_request_receiver()    → self.request_receiver    (SchedulerRequestReceiver)
  ├── init_output_streamer()     → self.output_streamer     (SchedulerOutputStreamer)
  ├── init_batch_result_processor() → self.batch_result_processor
  ├── init_pool_stats_observer() → self.pool_stats_observer
  ├── init_invariant_checker()   → self.invariant_checker
  ├── init_weight_updater()      → self.weight_updater
  ├── init_profiler()            → self.profiler_manager
  └── ... (kv_events_publisher / load_inquirer / flush_wrapper / idle_sleeper ...)
```

每个组件的生命周期都挂在 Scheduler 上，事件循环在恰当的时机调用它们的方法（比如收请求时调 `request_receiver.recv_requests()`，处理结果时调 `batch_result_processor` 内部进而调 `output_streamer.stream_output()` 和 `metrics_reporter.report_*`）。

#### 4.1.3 源码精读

先看目录里都有哪些组件（每个文件一个职责）：

```
batch_result_processor.py   消化前向结果：释放 KV、流式输出、上报指标
dp_attn.py                  数据并行注意力的调度器侧适配
flush_wrapper.py            缓存刷新的薄封装
idle_sleeper.py             空闲时休眠省电
invariant_checker.py        内存/状态一致性自检
ipc_channels.py             ZMQ 套接字集合（frozen dataclass）
kv_events_publisher.py      分离部署下的 KV 事件发布
load_inquirer.py            负载快照（供 DP 路由）
logprob_result_processor.py logprob 结果处理
metrics_reporter.py         指标采集与周期上报
new_token_ratio_tracker.py  跟踪「新增 token 比例」
output_sender.py            ZMQ 发送薄封装（SenderWrapper）
output_streamer.py          把结果流式发回 Detokenizer
pool_stats_observer.py      KV 内存池使用统计
profiler_manager.py         前向 profiler 管理
recv_skipper.py             跳过收请求（批量轮询优化）
request_receiver.py         从 ZMQ 收请求并广播到各 TP rank
weight_updater.py           权重热更新（RL 常用）
```

举一个最能体现「组合 + 回调注入」的例子——`SchedulerRequestReceiver`。它只持有自己需要的字段，并接受一个 `stream_output` 回调，而不是整个 Scheduler：

[request_receiver.py:L45-L65](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/request_receiver.py#L45-L65) —— `SchedulerRequestReceiver` 用 `slots=True, frozen=True` 锁定字段，其中 `stream_output: Callable` 和 `get_last_batch: Callable` 都是「回调句柄」，让组件能在不持有 Scheduler 的前提下读到它的动态状态。

对应地，Scheduler 在创建它时把回调绑进去：

[scheduler.py:L1771-L1772](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1771-L1772) —— `stream_output=lambda *a, **kw: self.output_streamer.stream_output(...)`，把请求接收器和输出流通过回调接上。

再看 IPC 通道组件——它把 Scheduler 要用的几条 ZMQ 套接字打包成一个不可变结构（承接 u2-l1 讲的三进程环拓扑）：

[ipc_channels.py:L16-L23](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/ipc_channels.py#L16-L23) —— `SchedulerIpcChannels` 持有「收请求 / 收 RPC / 发回 Tokenizer / 发回 Detokenizer / 发指标」五条 socket，rank 0 才真正建 socket，非 rank 0 拿到的是 `None` 的空壳。

而 `IdleSleeper` 是一个很小的横切组件，体现「职责单一」：

[idle_sleeper.py:L28-L35](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/idle_sleeper.py#L28-L35) —— `maybe_sleep()` 用 `zmq.Poller` 阻塞最多 1 秒等新请求，空闲时让出 CPU，避免每个 GPU 各占满一个核 100%。

#### 4.1.4 代码实践

这是一个「源码阅读型实践」，目标是熟悉组件如何被组装。

1. 实践目标：在源码里画出「组件 ↔ Scheduler 字段 ↔ 事件循环调用点」的对应关系。
2. 操作步骤：
   - 在 `scheduler.py` 中搜索 `self.request_receiver`、`self.output_streamer`、`self.metrics_reporter`、`self.batch_result_processor` 四个字段，分别记录它们在 `__init__`/`init_*` 里被创建的位置，以及被事件循环调用的位置。
   - 特别留意 `request_receiver` 构造时传入的 `stream_output=lambda ...` 回调（[scheduler.py:L1771](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1771)）。
3. 需要观察的现象：你会看到组件方法几乎都不直接返回大对象给事件循环，而是「自己持有上下文 + 被回调驱动」。
4. 预期结果：列出一张三列表（组件类名 / Scheduler 字段 / 在事件循环哪一步被调用）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 SGLang 用「组合 + 回调」而不是「把 Scheduler 传给每个组件」？
**参考答案**：组合 + 回调让每个组件只依赖自己真正用到的几个值，降低耦合、方便单测，也符合「Pass what you need, not the god object」的规范；把整个 Scheduler 传进去会让组件能偷偷访问任意内部状态，破坏封装，也容易造成循环依赖。

**练习 2**：`SchedulerIpcChannels` 被声明为 `frozen=True`（不可变）。这样设计有什么好处？
**参考答案**：socket 在进程生命周期内创建一次即可，frozen 防止后续代码误改字段、也提示这是一个稳定的「资源集合」；不可变结构还更利于推理——持有它的代码不用担心别处偷偷换掉 socket。

---

### 4.2 overlap_utils：FutureMap 与 CPU-GPU 重叠

这是本讲最核心的一节。`overlap_utils.py` 是 overlap 模式的「中转站」，它解决一个关键问题：**当结果处理被延后一轮时，下一轮前向需要的输入（比如上一轮刚采样的 token）怎么在没有 CPU 同步的情况下跨迭代传递？**

#### 4.2.1 概念说明

先对比两种事件循环的本质差异。

**串行模式（event_loop_normal）**：每一轮严格按顺序执行——收请求 → 决定下一批 → 启动前向 → **等前向跑完** → 处理结果 → 进入下一轮。CPU 的「调度/结果处理」和 GPU 的「前向」完全串行，GPU 在 CPU 忙时空闲。

**重叠模式（event_loop_overlap）**：开两条 CUDA stream，把「处理上一批的结果」放到 `schedule_stream`，和「当前批在 `forward_stream` 上的前向」并发。于是 GPU 几乎不用等 CPU——CPU 调度开销被藏到了 GPU 计算背后，这就是「零开销」的字面含义。

但重叠带来两个工程难题，正是 `overlap_utils.py` 要解决的：

1. **跨迭代数据传递**：第 N 轮 decode 采样的 token，就是第 N+1 轮 decode 的输入。串行模式下可以直接在 CPU 上读上一轮结果；overlap 模式下结果被延后处理，怎么让下一轮前向拿到这些 token 而不必等同步？
2. **共享显存的读写顺序**：`schedule_stream` 在写下一批的输入、`forward_stream` 在读统一的 KV 内存池，两者碰同一块显存，需要正确的顺序保证（WAR 屏障）。

`FutureMap` 就是难题 1 的答案：一个「按请求池索引（`req_pool_idx`）寻址的 GPU 缓冲」。前向结束后把要中转的张量 `publish`/`stash` 进去，下一轮前向入口处 `resolve_forward_inputs` 再 `gather` 出来——全程在 GPU 上，不需要 D2H 同步。

#### 4.2.2 核心流程

先看事件循环本身的流水线（伪代码，省略非关键分支）：

```
event_loop_overlap:
  result_queue = deque()                 # 保存「待处理的结果」

  while True:
    recv_reqs = request_receiver.recv_requests()
    plan = get_next_batch_to_run(...)    # ① CPU：在 schedule_stream 上决定下一批
    batch = plan.batch_to_run

    if is_disable_overlap_for_batch(batch, last_batch):
        pop_and_process()                # 同步边界：立刻处理完队列（见 4.2.3 末尾）

    if batch:
        batch_result = run_batch(batch)  # ② GPU：在 forward_stream 上启动前向（异步返回）
        _apply_war_barrier()             # ③ 设置 WAR 屏障：后续写入挡在前向读取之后
        result_queue.append((batch.copy(), batch_result))   # ④ 快照入队

    if last_batch:
        pop_and_process()                # ⑤ CPU：处理「上一批」结果——此时当前批正在 GPU 上跑！

    last_batch = batch
```

关键在第 ④ 步的 `batch.copy()`：把批做一份快照入队，这样处理上一批时修改张量不会破坏「正在 GPU 上跑的当前批」。这也正是项目规范 `schedule-batch-out-of-place-mutation.md` 里强调「ScheduleBatch 字段只能整体重绑、不能就地改」的根本原因——overlap 依赖「旧批被冻结」这一前提。

`FutureMap` 在前向入口与出口处发挥作用，串起两条 stream：

```
第 N 轮 run_batch（forward_stream）：
  resolve_forward_inputs(batch, future_map)   # 从 future_map 取出第 N-1 轮采样的 token
  前向 + 采样 ...
  future_map.publish(indices, new_seq_lens)    # 把第 N 轮结果写回 future_map 的 GPU 缓冲
  future_map.stash(indices, payload)

第 N+1 轮 run_batch：
  resolve_forward_inputs(...)                  # gather 出第 N 轮的 token 作为输入 —— 无需 CPU 同步
```

CPU/GPU 时间线对比（甘特图）：

```
event_loop_normal（串行）：
时间→
CPU: [recv+组批N][..........空等..........][处理结果N][recv+组批N+1][空等][处理结果N+1]
GPU:                  [前向N][前向N+1]
                       ↑GPU在CPU忙时空闲

event_loop_overlap（重叠）：
时间→
CPU: [组批N][启动N] [处理结果N-1 + 组批N+1][启动N+1] [处理结果N + 组批N+2]...
                       (藏在前向N背后)                 (藏在前向N+1背后)
GPU:        [========前向N========][======前向N+1======][====前向N+2====]
            ↑GPU几乎无空隙，CPU工作被重叠掉
```

> 结果「延后一轮」的代价：图中 N 批的结果要等到 N+1 批那一轮才被 `pop_and_process` 处理，所以输出整体晚一个迭代。这正是 4.3 节会看到的「overlap 下请求会被多输出一个延迟 token」。

#### 4.2.3 源码精读

**入口分发：选择哪种事件循环。** `run_event_loop` 先建 `schedule_stream`，并确保它不会和 `forward_stream` 是同一条（否则重叠失效），然后启用 WAR 屏障，最后交 `dispatch_event_loop` 按配置分流：

[scheduler.py:L1481-L1500](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1481-L1500) —— 创建 `schedule_stream`，并在 CUDA/HIP 上检查其底层句柄不等于 `forward_stream`（最多重抽 64 次），最后在 `schedule_stream` 上下文里进入事件循环。

[scheduler.py:L4497-L4525](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4497-L4525) —— `dispatch_event_loop` 按 disaggregation 模式与 `enable_overlap`/`pp_size` 等开关，分发到 `event_loop_overlap` 或 `event_loop_normal`（以及分离部署的若干变体）。

**串行版。** 简单直接——跑完一批立刻处理：

[scheduler.py:L1520-L1549](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1520-L1549) —— `event_loop_normal`：`run_batch(batch)` 之后**紧接着** `process_batch_result(batch, result)`，CPU 必须等 GPU 前向完成才能继续。

**重叠版。** 注意第 ④ 步的快照入队、第 ⑤ 步的「处理上一批」：

[scheduler.py:L1554-L1622](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1554-L1622) —— `event_loop_overlap`：`run_batch` 后 `result_queue.append((batch.copy(), batch_result))`，随后在 `if self.last_batch` 分支里 `pop_and_process()` 处理上一批。`batch.copy()` 是 overlap 的正确性前提。

**什么时候关闭重叠。** 并非所有批都能重叠。`is_disable_overlap_for_batch` 决定同步边界：

[scheduler.py:L1627-L1665](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1627-L1665) —— 两类情况强制退回串行：① 连续两个 prefill 批（由 `SGLANG_DISABLE_CONSECUTIVE_PREFILL_OVERLAP` 控制，为了首请求 TTFT）；② 某些投机解码算法与文法（grammar）配合时，FSM 必须先于下一批的 bitmask 推进，需要同步。

**WAR 屏障。** `_apply_war_barrier` 在每次启动后插入，把后续写入挡在前向读取之后，优先用「读完成事件」做细粒度等待，必要时退回整条 `wait_stream`：

[scheduler.py:L1502-L1517](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1502-L1517) —— `_apply_war_barrier`：fast path 等前向发布的 `war_fastpath_read_done_event`，coarse path 才 `wait_stream(self.forward_stream)`（可被 `SGLANG_FORCE_COARSE_WAR_BARRIER` 强制）。

**FutureMap：跨迭代中转站。** 这是 `overlap_utils.py` 的主角：

[overlap_utils.py:L232-L295](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/overlap_utils.py#L232-L295) —— `FutureMap.__init__`：按请求池大小 `req_pool_size` 分配 GPU 缓冲 `output_tokens_buf`、`new_seq_lens_buf`，并按需分配 pinned host 缓冲与专用 D2H stream（注释说明这用来「恢复被 WAR 屏障损失的并发度」）。注意 slot 0 是 KV padding 行，让 CUDA graph 的 padded 批（`req_pool_idx == 0`）无害。

前向入口处物化输入：prefill 走 pinned CPU staging 的 H2D 拷贝，decode/spec 则从 `FutureMap` gather 出上一轮采样的 token：

[overlap_utils.py:L84-L116](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/overlap_utils.py#L84-L116) —— `resolve_forward_inputs`：两条来源——`batch.prefill_input_ids_cpu` 的 H2D 拷贝，或从 `future_map.output_tokens_buf[indices]` gather。

前向结束后把结果写回：

[overlap_utils.py:L470-L533](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/overlap_utils.py#L470-L533) —— `publish`（写 `new_seq_lens_buf`、必要时记录事件）与 `stash`（写 `output_tokens_buf` 及 spec 相关的 topk/hidden_states 等）。事件 `publish_ready` 用来给后续 D2H 拷贝做栅栏。

这些 `publish`/`stash`/`resolve_*` 在 `run_batch` 里被编排起来（前向入口 resolve、前向出口 publish）：

[scheduler.py:L3334-L3374](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3334-L3374) —— `run_batch` 的 overlap 分支：先 `future_map.resolve_seq_lens_cpu(batch)`，进入 `forward_stream_ctx` 后 `resolve_forward_inputs`，跑 `forward_batch_generation`，再 `future_map.publish(...)`。

#### 4.2.4 代码实践

本节的实践任务与讲义规格一致：对比两种事件循环的时间线，画出甘特图。

1. 实践目标：亲手把 normal 与 overlap 的 CPU/GPU 时间线画出来，并指出 overlap 的吞吐提升点。
2. 操作步骤：
   - 对照 [scheduler.py:L1520-L1549](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1520-L1549)（normal）和 [scheduler.py:L1554-L1622](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1554-L1622)（overlap），把每一轮迭代拆成「CPU 工作」和「GPU 工作」两行。
   - 重点标注：normal 里 `process_batch_result` 紧跟 `run_batch`（CPU 等待）；overlap 里 `run_batch(N)` 之后紧跟 `pop_and_process()` 处理的是 `last_batch`（即 N-1）。
   - 在 overlap 图里标出 `result_queue` 的入队与出队时序。
3. 需要观察的现象：overlap 模式下 GPU 两次前向之间的空隙被压缩；代价是结果晚一轮、且需要 `batch.copy()` 快照与 WAR 屏障。
4. 预期结果：产出两张 ASCII 甘特图（可参考 4.2.2 节的范本，但要按你自己对源码的理解重画），并写一句话总结：「overlap 把 CPU 的调度与结果处理重叠到 GPU 前向背后，吞吐提升点在于消除 GPU 空等；但引入了结果延后一轮、需要快照与 WAR 屏障的正确性成本」。若无法在本地跑 GPU 验证时序，可标注「时序图为源码阅读推导，待本地用 profiler 实测」。

> 进阶（可选）：如果本地有 GPU，可用 `--disable-overlap` 启动服务（或读 `server_args.enable_overlap` 的来源），再用讲义 u7-l3 介绍的 profiler 抓一次 decode 阶段的 trace，对比 GPU stream 的占用率。这一步若无环境可跳过，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：overlap 模式里，为什么处理上一批结果前要先 `result_queue.append((batch.copy(), batch_result))` 而不是直接 `append((batch, batch_result))`？
**参考答案**：因为当前批 `batch` 已经启动到 GPU 上前向，而我们要在 CPU 侧处理「上一批」时会修改/重建当前批的张量字段（如 `input_ids`、`seq_lens`）。如果不做快照，处理上一批时的写入会破坏正在 GPU 上被读取的当前批数据。`batch.copy()` 冻结一份旧批供延后处理。

**练习 2**：`FutureMap` 为什么要「按请求池索引（`req_pool_idx`）」而不是「按批内下标」寻址？
**参考答案**：跨迭代时批的组成、顺序都会变，批内下标在下一轮没有意义；而 `req_pool_idx` 是请求在全局池中的稳定槽位号。前向按 `req_pool_indices` 把结果 `publish` 到对应槽，下一轮无论这些请求被分到哪个批、什么位置，都能按同样的槽号 `gather` 出来。

**练习 3**：什么情况下 overlap 会自动退回串行？
**参考答案**：见 `is_disable_overlap_for_batch`：连续两个 prefill 批（受 `SGLANG_DISABLE_CONSECUTIVE_PREFILL_OVERLAP` 控制，为保 TTFT）或不支持文法重叠的投机解码算法遇到带文法的 decode 批时，会强制同步。此时 `pop_and_process()` 立刻排空队列。

---

### 4.3 output_streamer：把前向结果流式回送

#### 4.3.1 概念说明

Scheduler 跑完一批前向后，要把产出的 token 送回 DetokenizerManager（承接 u2-l3）。但「送回」不是简单地把 token 列表扔过 ZMQ——它要处理：流式输出的频率（`stream_interval`，每隔多少 token 发一次）、增量解码所需的偏移量、logprob、多模态 token 计数、缓存命中明细、投机解码的接受统计，等等。`SchedulerOutputStreamer` 就是专门把「一批 `Req` 的输出」整理成一条 `BatchTokenIDOutput` 消息并发出 ZMQ 的组件。

它还必须 **overlap 感知**：由于结果延后一轮，一个已完成的请求可能被访问两次（多发了一个延迟 token），需要去重。

#### 4.3.2 核心流程

```
stream_output(reqs, return_logprob, skip_req)
  ├── is_generation? → _stream_output_generation(reqs, ...)
  │     ├── 构造 _GenerationStreamAccumulator
  │     ├── 对每个 req：若已完成且已输出 → continue（去重）
  │     │            否则 acc.accept(req)（按 stream_interval 决定是否真发）
  │     ├── payload = acc.to_payload(dp_rank, is_idle_batch)
  │     └── send_to_detokenizer.send_output(payload)
  └── else → _stream_output_embedding(reqs)（embedding/奖励模型路径）
```

`accept` 里最关键的逻辑是 `should_output` 判定：流式请求按 `stream_interval` 决定本轮要不要发 token（只有到了边界才真正累加进 payload），非流式请求按 `force_stream_interval` 强制周期上报。

#### 4.3.3 源码精读

`SchedulerOutputStreamer` 是一个 `slots=True, kw_only=True` 的 dataclass，持有发往 Detokenizer 的 socket 与若干上下文：

[output_streamer.py:L38-L48](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L38-L48) —— 持有 `send_to_detokenizer: zmq.Socket`、`tree_cache`、`ps`、`server_args` 等。

入口按生成/embedding 分流，并带一个「测试用崩溃」钩子（体现 SGLang 用真实流式路径做混沌测试）：

[output_streamer.py:L93-L108](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L93-L108) —— `stream_output` 分流；`SGLANG_TEST_CRASH_AFTER_STREAM_OUTPUTS` 可在第 N 次输出后主动崩溃。

overlap 感知的去重逻辑——已完成且已输出的请求直接跳过：

[output_streamer.py:L151-L159](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L151-L159) —— 注释直说：「With the overlap schedule, a request will try to output twice and hit this line twice because of the one additional delayed token」。这正是 4.2 节「结果延后一轮」在输出侧的具体表现。

`should_output` 与累加——按 `stream_interval` 决定是否真发：

[output_streamer.py:L344-L362](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L344-L362) —— 流式请求 `len(output_ids) % stream_interval == 1`（或 0），非流式按 `force_stream_interval`。

最终把累加器转成 `BatchTokenIDOutput` 并发 ZMQ：

[output_streamer.py:L163-L168](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L163-L168) —— `to_payload` 后 `send_to_detokenizer.send_output(payload)`。

它不是被事件循环直接调用，而是被 `SchedulerBatchResultProcessor` 在处理结果时调用：

[batch_result_processor.py:L824-L825](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L824-L825) —— decode 结果处理末尾调用 `self.output_streamer.stream_output(batch.reqs, batch.return_logprob)`。

#### 4.3.4 代码实践

1. 实践目标：理解 `stream_interval` 如何影响流式输出的「发 token 频率」。
2. 操作步骤：
   - 在 `output_streamer.py` 的 `accept` 中定位 `stream_interval` 取值（[L345-L347](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/output_streamer.py#L345-L347)）：优先用请求自带的 `sampling_params.stream_interval`，否则回退到 `server_args.stream_interval`。
   - 手算：假设 `stream_interval=8`，一个请求依次产出 token，写出第 1、9、17… 个 token 时才会触发发送（对照 `len(output_ids) % stream_interval == 1`）。
3. 需要观察的现象：`stream_interval` 越大，Detokenizer 收到的消息越少、单条越大；越小则流式越「丝滑」但 ZMQ 往返越多。
4. 预期结果：用一句话写出 `stream_interval` 对「流式延迟」与「IPC 开销」的权衡。若要在服务里实测，可用不同 `--stream-interval` 启动并观察首 token 到达节奏（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 overlap 模式下，一个 `finished()` 的请求会在 `stream_output` 里被访问到两次？
**参考答案**：overlap 把结果处理延后一轮，且 decode 会多算一个延迟 token，于是请求可能在「真正结束的那一轮」和「延后处理的那一轮」都被遍历到。靠 `finished_output` 标志位去重，避免发出空输出。

**练习 2**：`SchedulerOutputStreamer` 与 DetokenizerManager 之间用哪条消息结构通信？
**参考答案**：生成路径用 `BatchTokenIDOutput`（见 `to_payload`），embedding 路径用 `BatchEmbeddingOutput`（见 `_stream_output_embedding`）。它们都来自 u2-l1 讲的 `io_struct.py`，是真正跨进程上线（ZMQ）的结构体。

---

### 4.4 metrics_reporter：批量指标与周期上报

#### 4.4.1 概念说明

服务跑起来后，运维要回答这些问题：「现在 decode 多快？」「KV 内存池用了多少？」「投机解码的接受率如何？」`SchedulerMetricsReporter` 负责把每一批的原始数据聚合成指标：一部分实时更新（供 Prometheus 端点拉取），另一部分按 `decode_log_interval`/`prefill_log_interval` 周期性打印到日志。它和 `output_streamer` 一样，是「横切」在整个调度流程上的组件，由 `batch_result_processor` 在处理 prefill/decode 结果时调用。

#### 4.4.2 核心流程

```
处理 prefill 结果 → batch_result_processor 调 metrics_reporter.report_prefill_stats(...)
处理 decode 结果  → batch_result_processor 调 metrics_reporter.report_decode_stats(...)
                    ├── 每轮：实时 token 计数 → metrics_collector（Prometheus）
                    └── 每 decode_log_interval 轮：算吞吐、读 pool_stats、打印日志、推重指标
```

`report_decode_stats` 内部分两层：「每轮都要做的实时计数」和「到了日志间隔才做的重活（算吞吐、读内存池统计、写 step time）」。用数学表达吞吐计算：

每 decode 日志间隔的生成吞吐（token/s）：

\[
\text{throughput} = \frac{\text{num\_generated\_tokens}}{t_{\text{now}} - t_{\text{last}}}
\]

其中 \(t_{\text{last}}\) 是上次打印 decode 统计的时刻。打印后 `num_generated_tokens` 清零，开始下一个窗口。

#### 4.4.3 源码精读

`SchedulerMetricsReporter` 同样是组合组件，构造时持有 `metrics_collector`（真正写 Prometheus 的对象）与一批 rank 信息：

[metrics_reporter.py:L89-L112](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L89-L112) —— 持有 `metrics_collector`、各 rank、以及从 `metrics_collector_context` 解析的一组开关；`__post_init__` 里 `_init_metrics` 初始化计数器并 `_install_device_timer_on_runners`（把 GPU 计时器装到 runner 上）。

`report_decode_stats` 的双层结构：

[metrics_reporter.py:L691-L733](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L691-L733) —— 先做每轮实时计数（`increment_realtime_tokens`），再判断 `forward_ct_decode % decode_log_interval != 0` 就 return（节流），否则算 `gap_latency` 与 `last_gen_throughput`。

调用点在 `batch_result_processor` 里：

[batch_result_processor.py:L827-L834](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L827-L834) —— decode 处理末尾自增 `forward_ct_decode`（对 \(2^{30}\) 取模防爆）并调 `report_decode_stats(...)`。

prefill 侧对称地有 `report_prefill_stats`：

[metrics_reporter.py:L526-L526](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L526-L526) —— `report_prefill_stats`，由 [batch_result_processor.py:L345](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L345) 调用。

`PrefillStats` 数据类把 prefill 一批的关键数字（输入/命中 token 数、新增比例、运行中请求数）打包，便于日志和上报：

[metrics_reporter.py:L54-L86](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L54-L86) —— `PrefillStats.from_adder` 直接从 u3-l3 讲的 `PrefillAdder` 取 `log_input_tokens`、`log_hit_tokens`、`new_token_ratio` 等。

#### 4.4.4 代码实践

1. 实践目标：找到「decode 吞吐」与「KV 使用率」这两个最常用指标的来源。
2. 操作步骤：
   - 在 `report_decode_stats` 里定位吞吐公式 `last_gen_throughput = num_generated_tokens / gap_latency`（[L734](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L734)）。
   - 顺着 `pool_stats = self.scheduler.pool_stats_observer.get_pool_stats()`（[L739](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L739)）看到 KV 使用率来自另一个组件 `SchedulerPoolStatsObserver`。
3. 需要观察的现象：decode 日志里 `Decode batch. #running-req: K, ...` 这行就是 `report_decode_stats` 拼出来的（[L753](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/metrics_reporter.py#L753)）。
4. 预期结果：写出一条 decode 日志里每个字段分别对应源码哪一行。若本地启动服务，可观察该日志随负载变化的节奏（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `report_decode_stats` 要分「每轮实时」和「按间隔重活」两层？
**参考答案**：实时层只做廉价的自增计数，保证 Prometheus 指标始终新鲜且不拖慢每轮前向；重活（读内存池、算吞吐、格式化日志）有 IO 与计算开销，按 `decode_log_interval` 节流，避免每轮都做。

**练习 2**：`forward_ct_decode` 为什么用 `(x + 1) % (1 << 30)` 而不是直接 `+ 1`？
**参考答案**：decode 可能持续极多轮，朴素自增最终会溢出或变成超大整数；对 \(2^{30}\) 取模既保留「周期性判断」（`% decode_log_interval`）所需的单调循环计数，又避免数值无限增长。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「overlap 模式全链路阅读」。

任务：**画出一条 decode 请求在 overlap 模式下，从「第 N 轮前向结束」到「第 N+1 轮前向启动」之间，所有相关组件与缓冲的协作时序。**

要求包含：

1. **数据中转**：`run_batch` 出口 `future_map.publish(...)` 写入 `output_tokens_buf`/`new_seq_lens_buf`（[scheduler.py:L3374](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3374)）→ 下一轮入口 `resolve_forward_inputs` 从中 gather（[overlap_utils.py:L84](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/overlap_utils.py#L84)）。
2. **结果延后**：当前批 `result_queue.append((batch.copy(), ...))`（[scheduler.py:L1604](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1604)）→ 下一轮 `pop_and_process()` 处理（[scheduler.py:L1560](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1560)）。
3. **正确性**：`_apply_war_barrier` 何时插入（[scheduler.py:L1603](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1603)）。
4. **横切组件**：结果处理时 `batch_result_processor` → `output_streamer.stream_output`（[batch_result_processor.py:L824](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L824)）→ `metrics_reporter.report_decode_stats`（[batch_result_processor.py:L830](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L830)）。

产出：一张纵向时序图（横轴为时间，纵轴为 `forward_stream` / `schedule_stream` / `FutureMap 缓冲` / `result_queue` 四条泳道），并标注同步点（WAR 屏障、`batch.copy()`、`is_disable_overlap_for_batch` 退回串行的位置）。标注哪些步骤在 GPU 上、哪些在 CPU 上。

---

## 6. 本讲小结

- `scheduler_components/` 目录用「组合 + 回调注入」把 Scheduler 的职责拆成近 20 个小组件（receiver/streamer/metrics_reporter/result_processor/…），避免上帝类，符合项目「hold a collaborator and call it」规范。
- overlap 模式靠两条 CUDA stream（`schedule_stream` 与 `forward_stream`）实现 CPU/GPU 重叠：本轮启动前向后，立刻处理上一批结果，从而把调度与结果处理藏到 GPU 计算背后，即「零开销调度器」。
- 结果被整体延后一轮：用 `result_queue` 缓冲、用 `batch.copy()` 冻结旧批、用 `output_streamer` 的 `finished_output` 标志去重「多发的一个延迟 token」。
- `overlap_utils.FutureMap` 是跨迭代中转站：按请求池索引寻址的 GPU 缓冲，前向出口 `publish`/`stash`、下一轮入口 `resolve_forward_inputs` gather，全程不必 D2H 同步。
- 正确性靠 WAR 屏障（`_apply_war_barrier`）保证「先读后写」；`is_disable_overlap_for_batch` 在连续 prefill 或文法/投机解码特定场景下退回串行。
- `output_streamer` 把一批输出按 `stream_interval` 整理成 `BatchTokenIDOutput` 流式发回 Detokenizer；`metrics_reporter` 双层（实时计数 + 周期重活）采集 prefill/decode 指标并上报 Prometheus。

## 7. 下一步学习建议

- 本讲把 overlap 的调度侧讲清了，但「前向内部如何与 schedule_stream 协作」要看 **u7-l1（CUDA Graph 捕获与回放）**：graph 回放正是和 overlap、`FutureMap` 的静态缓冲紧密耦合的。
- 想看指标最终如何暴露给运维，进入 **u7-l3（可观测性、指标与性能分析）**：那里会讲 `metrics_collector`、Prometheus 端点与 profiler，与本讲的 `metrics_reporter` 上下衔接。
- 想理解「内存池」这个反复出现的统一 KV 缓冲，进入 **u4-l2（KV 内存池：ReqToTokenPool 与 TokenToKVPool）**——本讲里 WAR 屏障保护的「共享显存」正是它。
- 若对投机解码如何与 overlap 交织（`publish`/`stash` 里的 topk/hidden_states）感兴趣，可先读 **u10-l1（投机解码概览与算法注册）**，再回看 `FutureMap` 的 spec 分支。
