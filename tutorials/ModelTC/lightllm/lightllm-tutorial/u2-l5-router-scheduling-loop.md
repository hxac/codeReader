# Router 调度循环

## 1. 本讲目标

本讲聚焦 LightLLM 多进程架构中 **Router 进程的大脑**——调度主循环。学完后你应该能够：

- 理解 Router 以固定时间间隔（默认 30ms）驱动的 `loop_for_fwd` 事件循环，知道它「一轮做什么、为什么用 sleep 节拍」。
- 掌握 `_step` 一轮调度里依次完成的五件事：接收新请求、写入 profiler 命令、把新 batch 送进推理、过滤已完成请求、处理中止 / 命中停止串的请求。
- 看懂 `schedule_new_batch`、`running_batch`、`req_queue` 三个核心数据结构在新请求进入推理时的协同关系。
- 认识**激进调度**与**保守调度**的区别，以及 `router_token_ratio`、`is_busy` 如何在两者之间切换。

承接前面讲义：u2-l1 给出了请求在进程间流转的整体地图，u2-l3 讲清了 Req 对象与共享内存，u2-l4 讲清了 ModelBackend 自驱的 `infer_loop`。本讲把这些串起来——**Router 决定「这一轮该让谁跑」，ModelBackend 负责「真去跑」**。

## 2. 前置知识

在读懂本讲前，先建立以下直觉：

- **事件循环（event loop）**：Python 的 `asyncio` 把「需要等待」的操作（如网络收发）注册到底层，主线程不必阻塞死等，而是循环地「看看谁就绪了就处理谁」。LightLLM 用 `uvloop`（一个高性能 asyncio 实现）驱动 Router 循环。本讲里的 `await asyncio.sleep(...)` 不是浪费时间，而是「让出这一拍」。
- **节拍器式调度（tick scheduling）**：Router 不是「有请求就立刻跑」，而是每隔一个固定时间间隔（`schedule_time_interval`，默认约 30ms）醒来一次，把这段时间内到达的请求攒起来统一决策。这像音乐里的节拍器——以拍为单位组织工作。
- **三类对象**（u2-l3、u2-l4 已建立）：
  - `req_queue`：等待区，存放「还没被允许进入 GPU 推理」的请求。
  - `running_batch`：在跑区，存放「已经被送进 ModelBackend、正在参与 decode」的请求。
  - `schedule_new_batch`：中转区，存放「已经被调度选中、但还没写进推理后端共享内存」的新 batch。
- **共享内存命令管道**：Router 与 ModelBackend 之间，控制命令（要 prefill 哪些 req、要 abort 哪些 req）通过共享内存里的 `shm_reqs_io_buffer` 传递，由 backend 的 `infer_loop` 自行轮询读取（u2-l4）。
- **激进 vs 保守**：当显存吃紧时，调度要尽量不暂停已有请求（保守）；当显存宽裕时，可以更积极地塞入新请求甚至打断 decode（激进）。这个切换由 `router_token_ratio` 控制。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py) | Router 进程的核心。`RouterManager` 持有 `req_queue`、`running_batch`、`schedule_new_batch`，定义 `loop_for_fwd` 与 `_step`。 |
| [lightllm/server/router/batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py) | `Batch` 数据结构：一批请求的容器，提供 `merge`、`filter_out_finished_req`、`is_clear`、`merge_two_batch` 等批管理操作。 |
| [lightllm/server/router/req_queue/base_queue.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py) | 请求队列基类。定义 `is_busy`（忙闲判断）、`generate_new_batch`（生成新 batch 的抽象接口）、`update_token_load`。 |
| [lightllm/server/router/req_queue/chunked_prefill/impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py) | 默认队列实现 `ChunkedPrefillQueue`。给出 `generate_new_batch` 的真实算法，是「激进 / 保守调度」落地的关键。 |
| [lightllm/server/core/objs/req.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py) | `Req` 请求对象。其中的 `get_tuple_tokens(is_busy, ...)` 体现了 is_busy 如何改变对单个请求 token 占用的估计。 |

---

## 4. 核心概念与源码讲解

### 4.1 调度循环

#### 4.1.1 概念说明

Router 进程的本质是一个「心跳循环」。它不直接做 GPU 计算（那是 ModelBackend 的事），它的职责是**周期性地做调度决策**：这一拍要不要把新请求送进推理？

为什么不做成「请求一到就立刻调度」？因为：

1. **攒批（batching）需要时间窗口**：连续到达的请求如果各自立刻 prefill，会形成很多极小 batch，GPU 利用率低。每隔 30ms 攒一批，能在延迟几乎无感的前提下显著提升吞吐。
2. **避免忙等空耗 CPU**：循环用 `await asyncio.sleep(...)` 把空闲节拍让出去，而不是 `while True: pass`。
3. **多 DP 组配平**：在数据并行模式下，更长的间隔能收到更多请求，便于在多个 DP 组之间做负载均衡（见 `_get_schedule_time_interval` 的注释）。

#### 4.1.2 核心流程

`loop_for_fwd` 是一个无限循环，每一轮的伪代码如下：

```
loop_for_fwd():
    计数器 = 0
    while True:
        await _step()                      # 干一拍调度活
        计数器 += 1
        if running_batch 非空:
            每 100 拍打印一次调试日志、上报 gauge 指标
            更新 token_load（pd decode 模式强制每拍更新）
        else:
            强制更新 token_load（空载时也要刷新）
            每 300 拍把 batch/queue 指标清零上报
        await asyncio.sleep(调度间隔)      # 让出这一拍
```

关键点：**`_step()` 是「决策」，`sleep` 是「节拍」**。两者交替，构成稳定的心跳。

#### 4.1.3 源码精读

先看循环本体 [manager.py:L221-L226](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L221-L226)——`loop_for_fwd` 的入口与死循环：

```python
async def loop_for_fwd(self):
    counter_count = 0
    while True:
        await self._step()
        counter_count += 1
```

每一拍先调用 `_step()`（下一节细讲），随后用计数器分两种情况上报指标。

**有在跑请求时**（[manager.py:L228-L259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L228-L259)）：每 100 拍打一次 debug 日志，记录每个 DP 组的 batch size、暂停请求数、预估峰值 token 数、token 使用率；并始终通过 `req_queue.update_token_load` 刷新共享内存里的负载信息（pd decode 模式下 `force_update=True` 强制每拍刷新），再上报若干 gauge：

```python
self.req_queue.update_token_load(self.running_batch, force_update=self.is_pd_decode_mode)
self.metric_client.gauge_set("lightllm_batch_current_size", len(self.running_batch.reqs))
self.metric_client.gauge_set("lightllm_num_running_reqs", len(self.running_batch.reqs))
self.metric_client.gauge_set("lightllm_queue_size", self.req_queue.get_wait_req_num())
```

**空载时**（[manager.py:L260-L267](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L260-L267)）：每 300 拍把 batch / queue 相关指标清零上报，避免监控面板上残留旧值。

最后是节拍器本身 [manager.py:L274](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L274)：

```python
await asyncio.sleep(self._get_schedule_time_interval())
```

`_get_schedule_time_interval` 返回的就是构造时记下的 `self.schedule_time_interval`，它来自启动参数 `--schedule_time_interval`，注释明确写着「默认 30ms 的调度周期」（见 [manager.py:L52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L52)）。

> 小贴士：默认 30ms 是一个权衡值。太短则 CPU 唤醒开销大、且攒不到批；太长则首 token 延迟（TTFT）变差。在数据并行场景下，注释建议用更长间隔以便收到更多请求做配平（[manager.py:L217-L219](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L217-L219)）。

#### 4.1.4 代码实践

**实践目标**：把「节拍器」可视化，直观感受 30ms 心跳。

**操作步骤**：

1. 定位到 [manager.py:L52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L52)，确认 `schedule_time_interval` 的来源。
2. 定位到 [manager.py:L274](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L274) 的 `await asyncio.sleep(...)`，理解它是节拍的来源。
3. 阅读启动参数定义中 `--schedule_time_interval` 的默认值（在 `api_cli.py` 中）。

**需要观察的现象**：循环里**只有一处** `await asyncio.sleep`，且它在 `_step()` 之后；指标上报被 `counter_count % 100` 与 `% 300` 限制频率，说明监控本身不会拖慢调度。

**预期结果**：你会清楚地看到「决策（_step）→ 上报 → sleep」的固定节拍结构，这就是 Router 心跳的全部骨架。

> 说明：本实践为源码阅读型实践，无需启动服务即可完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `schedule_time_interval` 调到非常大（比如 1 秒），系统会有什么外在表现？

**参考答案**：请求从进入到开始 prefill 的延迟会明显增大（TTFT 变差），但每一拍能攒到更多请求、batch 更大，对**吞吐**有利；同时 `_step` 里对 abort / stop str 的响应也会变慢，用户点「停止生成」后要等更久才真正停。

**练习 2**：为什么空载分支（`running_batch is None`）也要调用 `update_token_load(force_update=True)`？

**参考答案**：当最后一个请求结束时，负载信息必须及时刷新归零，否则共享内存里残留的旧 `estimated_peak_token_count` / `dynamic_max_load` 会误导 HttpServer 端的并发接纳判断（u2-l2 会根据 token load 决定能否收新请求）。

---

### 4.2 事件处理

#### 4.2.1 概念说明

每一拍 `_step()` 是 Router 真正「干活」的地方。它把一拍内要做的事按固定顺序排好，像流水线工位一样依次处理。理解这个顺序非常关键，因为它决定了「新请求、已完成请求、被中止请求」之间的优先级。

#### 4.2.2 核心流程

`_step` 的五步流水线（顺序不能乱）：

```
_step():
    1. _recv_new_reqs_and_schedule()   # 收新请求 + 尝试生成新 batch
    2. _write_profiler_cmds()          # 写入性能分析命令（若有）
    3. 若 schedule_new_batch 非空 且 命令管道空闲:
         把 schedule_new_batch 合并进 running_batch
         _add_batch() 把控制命令写进共享内存唤醒 backend
    4. _filter_reqs_from_running_batch()  # 剔除已完成的请求
    5. 处理被中止 / 命中停止串的请求，把对应命令写给 backend
```

设计要点：
- **先收后发**：第 1 步先把外部进来的新请求接纳进 `req_queue` 并尝试调度；第 3 步才把决策结果发给 backend。这样一拍内「来一个、调度一个、发一个」链路完整。
- **空管道才发**：第 3 步的 `shm_reqs_io_buffer.is_empty()` 检查保证上一拍的命令已被 backend 消费完，才写新命令，避免命令堆积（u2-l3 讲过 `ShmObjsIOBuffer` 是单生产者多消费者的就绪计数协议）。
- **先清理再中止**：第 4 步先剔除已完成请求，第 5 步再处理中止 / 停止串，保证状态一致。

#### 4.2.3 源码精读

`_step` 主体 [manager.py:L276-L299](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L276-L299)：

```python
async def _step(self):
    # 1. 接受新请求，并尝试调度
    await self._recv_new_reqs_and_schedule()
    # 2. 写入 profiler 命令
    await self._write_profiler_cmds()
    # 3. 判断是否有新请求加入推理（激进调度 / 延迟 step 满足）
    if (self.schedule_new_batch is not None) and self.shm_reqs_io_buffer.is_empty():
        new_batch = self.schedule_new_batch
        self.schedule_new_batch = None
        self._add_new_batch_to_running_batch(new_batch=new_batch)
        await self._add_batch(new_batch)
    # 4. 过滤已完成
    self._filter_reqs_from_running_batch()
    # 5. 处理中止 / 停止串
    aborted_reqs = self._get_aborted_reqs_from_running_batch()
    if aborted_reqs:
        await self._aborted_reqs(aborted_reqs=aborted_reqs)
    stop_str_matched_reqs = self._get_stop_str_reqs_from_running_batch()
    if stop_str_matched_reqs:
        await self._stop_str_matched_reqs(stop_str_matched_reqs=stop_str_matched_reqs)
```

注释「激进调度满足，有新的推理 batch 就需要加入」点明了 `schedule_new_batch` 的语义：**只要上一拍调度出了新 batch 且管道空闲，就立刻发**，这正是激进调度的体现（不等 `router_max_wait_tokens` 个 decode 步）。

**第 1 步：接收新请求** `_recv_new_reqs_and_schedule` [manager.py:L506-L531](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L506-L531)。它用 `zmq.NOBLOCK` 非阻塞地从 zmq PULL 套接字拉请求，单拍最多拉 `recv_max_count` 个（默认 64，队列繁忙时自动放大到 256，清空时回落），防止主循环被海量请求卡住：

```python
for _ in range(self.recv_max_count):
    recv_req = self.zmq_recv_socket.recv_pyobj(zmq.NOBLOCK)
    if isinstance(recv_req, GroupReqIndexes):
        self._add_req(recv_req)
```

每收到一组请求就调 `_add_req`（[manager.py:L406-L422](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L406-L422)）：从共享内存按索引取出 Req 对象、打上 `_router_aborted=False` 等私有标记、`extend` 进 `req_queue`，并把请求索引 `send_pyobj` 转发给 detokenization 进程（让它提前知道有这么个请求）。

收完请求后，若当前没有「暂停中的请求」（`_get_paused_req_num()==0`），就调 `_generate_new_batch()` 尝试调度出一个新 batch（多机 TP 模式走 `_multinode_tp_generate_new_batch`，通过 gloo barrier + broadcast 协调各节点选同一批请求）。

**第 3 步：发送新 batch** `_add_batch` [manager.py:L301-L309](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L301-L309)：

```python
async def _add_batch(self, batch: Batch):
    reqs = [r.to_router_rpc_obj() for r in batch.reqs]
    while not self.shm_reqs_io_buffer.is_empty():
        await asyncio.sleep(0.001)        # 等管道被消费干净
    self.shm_reqs_io_buffer.write_obj(reqs)
    self.shm_reqs_io_buffer.set_ready()    # 通知 backend 可以读了
```

`write_obj` 只 pickle 含 `index_in_shm_mem` 的小对象（u2-l3 讲过的「对象放共享内存、线上只传索引」），`set_ready` 唤醒 backend 的 `infer_loop` 去做 prefill。

**第 4 步：过滤已完成** `_filter_reqs_from_running_batch` [manager.py:L345-L350](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L345-L350)，委托给 `Batch.filter_out_finished_req`，详见 4.3 节。

**第 5 步：中止与停止串**。注意 `_get_aborted_reqs_from_running_batch`（[manager.py:L352-L360](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L352-L360)）用 `_router_aborted` 私有标记**保证每个请求的 abort 命令只发给 backend 一次**，避免反复发送。`_get_stop_str_reqs_from_running_batch`（[manager.py:L362-L374](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L362-L374)）同理处理「命中停止字符串」的请求，但**多机 TP 模式暂不支持 stop str 退出**（注释 L363 说明）。

#### 4.2.4 代码实践

**实践目标**：亲手标注 `_step` 的处理顺序，验证「先收后发、先清后停」的设计。

**操作步骤**：

1. 打开 [manager.py:L276-L299](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L276-L299)。
2. 在每一行前面按 1~5 编号注释（如「① 收新请求」「② profiler」「③ 发新 batch」「④ 过滤已完成」「⑤ 中止/停止串」）。
3. 追问自己三个问题：
   - 为什么 ① 必须在 ③ 之前？（答：要先有 `schedule_new_batch` 才能发。）
   - 为什么 ④ 在 ⑤ 之前？（答：先把真正结束的请求移出 batch，避免对已结束请求再发无谓的 abort 命令。）
   - 为什么 ③ 要判断 `is_empty()`？（答：单生产者命令管道需等上一条被消费，参考 u2-l3。）

**需要观察的现象**：五步顺序严格固定，且 ③ 与 ⑤ 都依赖共享内存命令管道的 `is_empty` / `set_ready` 协议。

**预期结果**：你会得到一张清晰的「一拍五步」流程表，这就是 Router 调度的最小完整单元。

> 说明：本实践为源码阅读型实践，无需启动服务。

#### 4.2.5 小练习与答案

**练习 1**：`_recv_new_reqs_and_schedule` 为什么要限制单拍最多拉 `recv_max_count` 个请求，而不是一口气拉完？

**参考答案**：zmq 队列里可能积压大量请求，若一拍全拉完并逐个 `_add_req`，会阻塞主循环，导致这一拍超过 30ms，破坏节拍稳定性、拖慢 decode 响应。用 `NOBLOCK` + 上限 + 动态伸缩（繁忙放大、空闲回落）把工作量限制在一拍能消化的范围内。

**练习 2**：`_get_aborted_reqs_from_running_batch` 里 `req._router_aborted` 标记解决什么问题？

**参考答案**：一个请求被用户中止后，其 `is_aborted` 标志会持续为 True 直到被真正清理。若不加 `_router_aborted` 私有标记，后续每一拍都会重复向 backend 发送 abort 命令，浪费且可能引发竞争。该标记确保「abort 命令只发一次」。

---

### 4.3 批管理

#### 4.3.1 概念说明

这是本讲最核心的模块，也是代码实践任务直接对应的内容：**`schedule_new_batch`、`running_batch`、`req_queue` 三者如何协同**，把一个新请求从「到达」推进到「在 GPU 上 decode」。

先给三者的角色定位：

| 结构 | 角色 | 生命周期位置 | 定义处 |
| --- | --- | --- | --- |
| `req_queue.waiting_req_list` | **等待区**：尚未被选中跑的请求 | 刚到达 / 被退回 | `BaseQueue` [base_queue.py:L25](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L25) |
| `schedule_new_batch` | **中转区**：已被调度选中、尚未写进 backend 的新 batch | 调度后、下发前 | `RouterManager.wait_to_model_ready` [manager.py:L113](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L113) |
| `running_batch` | **在跑区**：已下发 backend、正在 decode 的请求 | 推理中 | `RouterManager.__init__` [manager.py:L80](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L80) |

`Batch` 是贯穿三者的统一容器（[batch.py:L11-L17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L11-L17)），它持有 `reqs` 列表与 `id_to_reqs` 字典，并提供 `merge`、`merge_two_batch`、`filter_out_finished_req`、`is_clear` 等操作。

#### 4.3.2 核心流程

新请求从到达到推理的完整流转（结合激进 / 保守调度）：

```
[新请求到达 zmq]
        │  _recv_new_reqs_and_schedule
        ▼
  req_queue.waiting_req_list         ← 等待区
        │  generate_new_batch（按 is_busy 决定激进/保守）
        ▼
  schedule_new_batch                 ← 中转区（可能跨多拍累积）
        │  _step 检查管道空闲后
        │   ├─ _add_new_batch_to_running_batch (merge 进 running_batch)
        │   └─ _add_batch (写共享内存 set_ready → backend prefill)
        ▼
  running_batch                      ← 在跑区
        │  backend 每拍 decode；完成的请求被 filter_out_finished_req 移出
        ▼
  (完成的请求归还共享内存槽位，结束)
```

**激进 vs 保守**的切换点在 `generate_new_batch` 里调用 `is_busy()`：

- `is_busy() == True`（显存占用率 ≥ `router_token_ratio`）：**保守**。调度倾向于少塞新请求、避免暂停在跑请求，保证吞吐稳定。
- `is_busy() == False`（显存宽裕）：**激进**。可以塞更多新请求，甚至打断 decode 做 prefill，追求低延迟。

#### 4.3.3 源码精读

**(a) Batch 容器与合并操作**

`Batch.merge`（[batch.py:L78-L85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L78-L85)）把另一个 batch 的请求追加进来并重建索引；静态方法 `merge_two_batch`（[batch.py:L87-L97](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L87-L97)）合并两个 batch（任一为 None 也安全）。这个静态方法在 `_generate_new_batch` 里反复出现，用于把「在跑的 + 已调度未发的」当作整体交给队列评估。

**(b) 生成新 batch：`_generate_new_batch`**

[manager.py:L424-L434](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L424-L434)：

```python
def _generate_new_batch(self):
    # 调度时需考虑当前运行 batch 和「已调度但未推理」的部分请求
    new_batch = self.req_queue.generate_new_batch(
        Batch.merge_two_batch(self.running_batch, self.schedule_new_batch)
    )
    ...
    self.schedule_new_batch = Batch.merge_two_batch(self.schedule_new_batch, new_batch)
```

两个关键设计：
1. 传给队列的 `current_batch` 是 `running_batch` 与 `schedule_new_batch` 的**合并体**——因为评估「还能不能塞新请求」时，必须把「已经在跑的」和「已选中马上要发的」都算进去，否则会重复占用显存。
2. 产出的 `new_batch` 被 `merge_two_batch` 累加进 `schedule_new_batch`，而不是直接覆盖。这让 `schedule_new_batch` 能**跨多拍累积**：即使某拍管道忙没发出去，下一拍仍可继续累积新选中的请求一起发。

**(c) 队列的 `generate_new_batch` 算法（默认实现）**

[impl.py:L57-L103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L57-L103) 是默认 `ChunkedPrefillQueue.generate_new_batch` 的核心。流程：

1. 等待区空 → 返回 None。
2. 当前已调度请求数 ≥ `running_max_req_size` → 返回 None（满了）。
3. 调 `is_busy()` 判断忙闲，决定这一拍的调度基调。
4. 遍历 `waiting_req_list`，对每个请求调 `_can_add_new_req`（[impl.py:L27-L54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54)）做三重检查：
   - **ok_token**：预估峰值 token 数 `< max_total_tokens`（显存够）。
   - **ok_req_num**：请求数 `≤ running_max_req_size`（并发数够）。
   - **ok_prefill**：本批首批需 prefill 的 token `≤ batch_max_tokens`（单批 prefill 预算够）。
   一旦某个请求不满足，直接 `break`（按等待顺序贪心塞，塞不下就停）。
5. 满足的请求组成新 `Batch`，从等待区移除；被 abort 的请求归还共享内存槽位。

**(d) 忙闲判断：`is_busy` 与 `router_token_ratio`**

[base_queue.py:L44-L50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L44-L50)：

```python
def is_busy(self):
    cur_all_used_tokens = self.router.get_used_tokens(self.dp_index)
    cur_token_ratio = cur_all_used_tokens / self.max_total_tokens
    is_busy = cur_token_ratio >= self.router_token_ratio
    return is_busy
```

`router_token_ratio`（[base_queue.py:L26](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L26)）来自启动参数 `--router_token_ratio`（[api_cli.py:L284-L295](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L284-L295)），范围 `[0.0, 1.0]`，默认 None 由系统自动选。`is_busy` 还会传给 `Req.get_tuple_tokens(is_busy, ...)`，影响对单个请求剩余输出长度的估计——见 [req.py:L377-L380](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L377-L380)：

```python
if self.sample_params.ignore_eos:
    cur_max_new_token_len = self.sample_params.max_new_tokens
elif is_busy:                                   # 保守：按最大可能长度估
    cur_max_new_token_len = self.sample_params.max_new_tokens
else:                                           # 激进：用较短估计
    cur_max_new_token_len = min(self.sample_params.max_new_tokens,
                                max(int(1.1 * has_out_len), ema_req_out_len))
```

也就是说：**忙时把每个请求的预期输出估得更长（占用更保守），闲时估得更短（敢于塞更多请求）**。这是 `router_token_ratio` 同时影响「塞不塞」和「怎么估」两层的精妙之处。

特别地，`router_token_ratio == 0.0` 时 `is_busy` 恒为 True，对应**纯保守调度**（`is_safe_schedule`，见 [manager.py:L61](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L61)），不会发生暂停请求的情况，代价是可能影响吞吐。

**(e) 合并进在跑区与清理**

`_add_new_batch_to_running_batch`（[manager.py:L338-L343](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L338-L343)）：若 `running_batch` 为 None 就直接赋值，否则 `merge`。完成后 `running_batch` 既包含老请求也包含新请求，backend 在下一次 prefill 时会一起处理。

`Batch.filter_out_finished_req`（[batch.py:L53-L68](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L53-L68)）遍历请求，把 `shm_infer_released` 为 True（backend 已释放其推理资源）的请求归还共享内存槽位（`put_back_req_obj`），并更新统计；剩下的重建为新的 `running_batch`。若全部清理完则由 `is_clear` 触发 Router 把 `running_batch` 置 None。

#### 4.3.4 代码实践

**实践目标**（本讲代码实践任务）：在 manager.py 中标注 `_step` 的处理顺序，并说明 `schedule_new_batch`、`running_batch`、`req_queue` 三者在新请求进入推理时的协同关系。

**操作步骤**：

1. **标注 `_step` 顺序**：在 [manager.py:L276-L299](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L276-L299) 标出 ①~⑤（见 4.2.2）。
2. **画协同关系图**：准备一张表，按时间拍（tick）记录一个新请求 `R` 的位置迁移：
   - tick N：`R` 到达 zmq → `_add_req` → 进入 `req_queue.waiting_req_list`。
   - tick N：`_generate_new_batch` 调 `generate_new_batch`，`is_busy` 判断为闲 → `_can_add_new_req` 通过 → `R` 进入 `schedule_new_batch`，从 `waiting_req_list` 移除。
   - tick N（同拍，第③步）：管道空闲 → `schedule_new_batch` 经 `_add_new_batch_to_running_batch` 合并进 `running_batch`，`_add_batch` 写共享内存 `set_ready`。
   - tick N+1 起：backend 消费命令完成 `R` 的 prefill，之后每拍 decode；某拍 `R` 输出 EOS → backend 释放 → `filter_out_finished_req` 把 `R` 移出 `running_batch`，槽位归还。
3. **验证保守分支**：构造一个想象场景——若 tick N 时 `is_busy()` 为 True，`R` 会停在 `req_queue` 不被选中（或仅被选中更少），直到显存释放。在 `_can_add_new_req` 的三个 `ok_*` 条件中定位是哪一条卡住了 `R`。

**需要观察的现象**：
- `schedule_new_batch` 是**累积式**的：它在 `_generate_new_batch` 里被 `merge_two_batch` 累加，在 `_step` 第③步被清空。
- 交给队列评估的 batch 是 `running_batch + schedule_new_batch` 的合并体，保证不重复计显存。
- `req_queue` 只管「等待 → 被选中」，一旦选中就移交 `schedule_new_batch`，不再持有该请求。

**预期结果**：你能用一张时序图说清「`req_queue`（等待）→ `schedule_new_batch`（中转）→ `running_batch`（在跑）」三段式迁移，并能解释 `router_token_ratio` 在迁移关口的作用。

> 说明：本实践为源码阅读 + 推理型实践，无需启动服务。若想实跑验证，可在本地用小模型启动服务并发起多个并发请求，观察日志中 `generate new batch` 与 `Prefill Batch` 的打印节奏（对应 [manager.py:L431](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L431) 与 [manager.py:L308](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L308)），但具体输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`_generate_new_batch` 为什么要把 `running_batch` 和 `schedule_new_batch` 合并后再交给 `req_queue.generate_new_batch`？

**参考答案**：评估「能否再塞新请求」必须以「当前实际占用 / 即将占用的显存」为基数。`running_batch` 是已经在跑的，`schedule_new_batch` 是已选中马上要发的，二者合并才代表「真实的占用基数」。若漏掉 `schedule_new_batch`，会重复选中请求导致显存超估不准。

**练习 2**：`schedule_new_batch` 为什么用 `merge_two_batch` 累加，而不是每次直接赋值为最新结果？

**参考答案**：因为下发新 batch（`_add_batch`）受「管道必须空闲」制约，可能跨多拍。若直接覆盖，上一拍选中但还没来得及发的请求会被丢弃。累加式设计让 `schedule_new_batch` 成为「待发积压区」，攒到管道空闲时一次性发出。

**练习 3**：`router_token_ratio` 设为 `0.0` 与设为较大值（如 `0.9`）分别对应什么调度行为？

**参考答案**：`0.0` → `is_busy` 恒为 True → `is_safe_schedule` 纯保守调度，不会暂停请求、预估更保守，吞吐可能受损但稳定；`0.9` → 只有显存占用率 ≥ 90% 才进入保守，多数时间激进调度，首 token 延迟更低、吞吐更高，但高峰期可能需要暂停部分请求。

---

## 5. 综合实践

把本讲三块知识串起来，完成一次「**为一拍 `_step` 写执行日志**」的源码阅读任务：

1. **设定场景**：假设某拍开始时 `running_batch` 有 2 个在跑请求（其中一个刚被用户中止），`req_queue` 有 3 个等待请求，当前显存占用率 50%（`router_token_ratio` 默认假设为 0.5，处于激进与保守临界）。
2. **逐步推演**，按 `_step` 的五步写出每一步后三个结构（`req_queue` / `schedule_new_batch` / `running_batch`）的状态变化，以及哪些命令被写进 `shm_reqs_io_buffer`：
   - ① 收新请求：3 个等待请求是否会被选中？参考 `_can_add_new_req` 的三重检查与 `is_busy` 估计。
   - ③ 发新 batch：`schedule_new_batch` 是否非空？管道是否空闲？是否合并进 `running_batch`？
   - ④ 过滤：被中止的请求在 `filter_out_finished_req` 中是否会被移出？（提示：看 `shm_infer_released` 与 `is_aborted` 的区别——中止需先发 abort 命令，由 backend 释放后才会在过滤阶段被移出。）
   - ⑤ 中止：`_get_aborted_reqs_from_running_batch` 是否会发出 abort 命令？发几次？
3. **画出最终的命令序列**：这一拍一共往 `shm_reqs_io_buffer` 写了哪几条命令（new batch / abort / stop str / profiler），顺序如何。
4. **反思**：若把 `router_token_ratio` 改为 `0.0`，第①步的选中结果会如何变化？

这个任务把「节拍循环」「事件顺序」「三结构协同」「激进/保守调度」全部贯通，做完后你应能独立向他人讲清 Router 的一拍到底发生了什么。

## 6. 本讲小结

- Router 是一个 **uvloop 驱动的心跳循环** `loop_for_fwd`：每一拍 `_step()` 做决策，随后 `await asyncio.sleep(schedule_time_interval)`（默认约 30ms）让出节拍。
- 一拍 `_step` 严格按 **五步**执行：收新请求并调度 → 写 profiler 命令 → 把新 batch 合并进 running_batch 并下发 backend → 过滤已完成请求 → 处理中止 / 停止串。
- 三个核心结构构成 **三段式迁移**：`req_queue.waiting_req_list`（等待）→ `schedule_new_batch`（中转，跨拍累积）→ `running_batch`（在跑）。下发受「共享内存命令管道必须空闲」制约。
- 评估能否塞新请求时，传入队列的是 `running_batch + schedule_new_batch` 的合并体，避免重复计显存；选中策略是贪心 + 三重检查（显存、并发数、单批 prefill 预算）。
- **激进 vs 保守**由 `router_token_ratio` + `is_busy` 控制：显存占用率 ≥ 该阈值则保守（少塞、估计更长），否则激进（多塞、估计更短）。`router_token_ratio == 0.0` 对应纯保守的 `is_safe_schedule`。
- Router 只做「决策」，真正的 GPU prefill/decode 由 ModelBackend 的自驱 `infer_loop` 完成，二者通过共享内存命令管道的 `write_obj` / `set_ready` / `is_empty` 协议解耦。

## 7. 下一步学习建议

- **u2-l6 请求队列与 chunked prefill 调度**：本讲只用到 `generate_new_batch` 的接口层，下一讲会深入 `ChunkedPrefillQueue` 内部的 token 负载估算与 chunked prefill 切分算法，把 `_can_add_new_req` 里的 `need_max_token_num` 公式讲透。
- **u4-l3 Token 负载估算与调度配额**：本讲反复出现的 `shared_token_load`、`estimated_peak_token_count`、`dynamic_max_load` 在第四单元有专讲，理解它们如何被 HttpServer 反过来用于接纳控制。
- **u2-l4 复习**：本讲的 `_add_batch` / `set_ready` 是「写」的一侧，建议回看 u2-l4 中 backend `infer_loop`「读」的一侧，闭环理解命令管道协议。
- **延伸阅读**：`lightllm/server/router/req_queue/chunked_prefill/impl.py` 的 `_can_add_new_req` 与 `Req.get_tuple_tokens`（[req.py:L364-L393](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L364-L393)）是激进/保守调度的数学核心，值得逐行精读。
