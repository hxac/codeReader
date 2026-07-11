# 数据并行与负载均衡

## 1. 本讲目标

本讲讲解 LightLLM 在**数据并行（Data Parallel，DP）**模式下的请求分发与负载均衡机制。读完本讲，你应当能够：

1. 说清 LightLLM 里「数据并行（DP）」与「张量并行（TP）」的区别，以及 DP 模式为何对 DeepSeek-V2/V3 这类模型特别有用。
2. 理解 `DpQueue` 如何把多个单 DP 组的内部队列（`BaseQueue`）包装成一个对外的统一队列，并在每拍调度里把请求「分流 + 合批」。
3. 掌握两种负载均衡策略 `round_robin` 与 `bs_balancer` 的算法差异，知道为什么默认是 `bs_balancer`。
4. 分清两个容易混淆的概念——**Router 层的 `--dp_balancer`（决定新请求去哪个 DP 组）** 与 **Backend 层的 `--enable_dp_prefill_balance`（在 prefill 时跨 DP 组重分 token）**，并说出后者启用时的前置条件。

本讲只聚焦「请求往哪个 DP 组送」这一调度层问题，不展开 DP 组内部的 prefill/decode 推理细节（那属于第三单元），也不展开 token 负载如何估算（那是 u4-l3 的内容，本讲直接复用其结论）。

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（对应依赖讲义 u2-l6、u4-l3）：

- **chunked prefill 调度（u2-l6）**：Router 每拍调用 `req_queue.generate_new_batch()` 挑出一批请求送去推理，调用 `update_token_load()` 把负载写进共享内存。本讲要回答的是：当存在多个 DP 组时，这个 `req_queue` 内部是如何把请求分到不同 DP 组的。
- **Token 负载估算（u4-l3）**：Router 维护一张「每个 DP 组一行」的负载表（`shared_token_load`，含 `current_load`、`estimated_peak_token_count`、`dynamic_max_load`），按 DP 组分别记账。本讲的均衡器正是要让各 DP 组的负载尽量接近。
- **张量并行（TP）**：同一个请求的同一层算子被切到多张 GPU 上协同计算（见 u3-l1/u7-l4）。本讲的 DP 是与 TP 正交的另一维度——多个 TP 组各自独立跑不同的请求。
- **`suggested_dp_index`**：每个请求的采样参数里有一个 `suggested_dp_index` 字段，标记「这个请求希望交给哪个 DP 组」。它在共享内存里的 ctypes 版本默认是 `-1`（见 [lightllm/server/core/objs/sampling_params.py:335](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py#L335)），`-1` 即「还没决定，交给均衡器分配」。

> 名词速查：**DP 组（dp rank / dp group）** = 一个完整的、能独立跑一次推理的 TP 组。一台机器上有 `dp_size_in_node` 个 DP 组，它们共享同一份模型权重、各自维护独立的 KV Cache 与请求队列。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/req_queue/dp_base_queue.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py) | `DpQueue`：把多个内部队列包装成统一队列，做分流与合批，是 DP 模式的「外壳」。 |
| [lightllm/server/router/req_queue/dp_balancer/base.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/base.py) | `DpBalancer`：负载均衡器的抽象基类，只定义一个接口 `assign_reqs_to_dp`。 |
| [lightllm/server/router/req_queue/dp_balancer/__init__.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/__init__.py) | `get_dp_balancer`：按 `--dp_balancer` 参数选择具体均衡器。 |
| [lightllm/server/router/req_queue/dp_balancer/roundrobin.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/roundrobin.py) | `RoundRobinDpBalancer`：在「等待队列最短」的 DP 组之间轮询。 |
| [lightllm/server/router/req_queue/dp_balancer/bs.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/bs.py) | `DpBsBalancer`：按「在跑 + 等待」的总请求数最小来分配，默认策略。 |
| [lightllm/server/router/req_queue/__init__.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py) | `build_req_queue`：决定 dp=1 时直接用单队列，dp>1 时套上 `DpQueue`。 |
| [lightllm/server/router/req_queue/base_queue.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py) | `BaseQueue`：每个 DP 组内部队列的基类，提供 `is_busy`、`get_batch_dp_req_size` 等共享方法。 |
| [lightllm/server/router/batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py) | `Batch.get_all_dp_req_num` / `Batch.merge`：按 DP 组统计请求数、合并多个子 batch。 |
| [lightllm/server/api_cli.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py) | `--dp`、`--dp_balancer`、`--enable_dp_prefill_balance` 等命令行参数定义。 |

辅助理解（点到为止）：`manager.py` 里 `dp_size_in_node` 的计算与 `req_queue` 的构建；`basemodel.py`/`infer_struct.py` 里 `enable_dp_prefill_balance` 在推理后端的真实用法（第 4.3 节区分概念时引用）。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**数据并行（4.1）→ DP 队列（4.2）→ 负载均衡（4.3）**。三者是层层包裹的关系——先有多个 DP 组（4.1），才需要 `DpQueue` 把它们包起来（4.2），才需要在 `DpQueue` 里塞一个均衡器决定新请求去哪（4.3）。

### 4.1 数据并行（DP）

#### 4.1.1 概念说明

**张量并行（TP）** 解决的是「一张卡装不下一个模型」：把每一层的权重按维度切到多张卡上，一次推理需要多张卡协作。**数据并行（DP）** 解决的是另一个问题：「一张机器上有好多卡，能不能同时处理多个请求，提高吞吐」。

具体做法是：把机器上的 GPU 分成若干个 **DP 组**，每个 DP 组内部自己做 TP，组与组之间跑**不同的请求**、互不干扰。例如一台 8 卡机器，可以配成 `--tp 2 --dp 4`（4 个 DP 组，每组 2 卡做 TP），也可以配成 `--tp 8 --dp 1`（只有 1 个 DP 组，8 卡全做 TP）。

LightLLM 的 `--dp` 参数帮助文档明确写了它当前的主要用途——**DeepSeek-V2/V3**：

```python
# lightllm/server/api_cli.py:224-231
parser.add_argument(
    "--dp",
    type=int,
    default=1,
    help="""This is just a useful parameter for deepseekv2. When
                    using the deepseekv2 model, set dp to be equal to the tp parameter. In other cases, please
                    do not set it and keep the default value as 1.""",
)
```

> 为什么 DeepSeek-V2 适合 `dp == tp`？因为 DeepSeek-V2 用了 MLA 注意力（见 u5-l5），它的 KV Cache 极小，单卡往往塞不满；与其让 8 张卡都做 TP 把一个请求算完，不如切成多个 DP 组同时处理多个请求，吞吐更高。这是 LightLLM 针对 DeepSeek 系列的典型部署形态。

#### 4.1.2 核心流程

Router 在启动时按机器数（`nnodes`）把 `--dp` 折算成「本机 DP 组数」`dp_size_in_node`：

- 单机部署（`nnodes == 1`）：`dp_size_in_node = dp`。
- 多机部署（`nnodes > 1`）：`dp_size_in_node = dp // nnodes`，即每台机器分到几个 DP 组；并且用 `max(1, ...)` 兼容「多机纯 TP（dp=1）」时 `1 // nnodes == 0` 的退化情况。

每个 DP 组各自拥有独立的 KV Cache、独立的等待队列、独立的负载账本（u4-l3 讲过的 `shared_token_load` 就是按 DP 组分行记账的）。Router 只负责把请求分发到正确的 DP 组，之后每个 DP 组就像一个「迷你 Router」一样独立调度。

#### 4.1.3 源码精读

`dp_size_in_node` 的折算在 RouterManager 初始化里：

```python
# lightllm/server/router/manager.py:51-55
self.dp_size = args.dp
self.schedule_time_interval = args.schedule_time_interval  # 默认30ms 的调度周期
# 兼容多机纯tp的运行模式，这时候 1 // 2 == 0, 需要兼容
self.dp_size_in_node = max(1, args.dp // self.nnodes)
self.dp_world_size = self.world_size // self.dp_size
```

`dp_world_size` 是「单个 DP 组内的总卡数」（含跨机），它会被传给 KV 内存管理器等组件，决定一个 DP 组的并行范围。

随后 Router 用 `dp_size_in_node` 构建请求队列（注意 `dp_size_in_node` 才是本机真正需要建的队列数）：

```python
# lightllm/server/router/manager.py:198
self.req_queue = build_req_queue(self.args, self, self.dp_size_in_node)
```

进入 `build_req_queue` 后，会看到 DP 模式的分叉点——**dp=1 时根本没有 `DpQueue`**：

```python
# lightllm/server/router/req_queue/__init__.py:27-33
def build_req_queue(args, router, dp_size_in_node: int):
    queue_class = _get_req_queue_class(args, router, dp_size_in_node)

    if dp_size_in_node == 1:
        return queue_class(args, router, 0, dp_size_in_node)
    else:
        return DpQueue(args, router, queue_class, dp_size_in_node)
```

也就是说，`DpQueue` 只在 `dp_size_in_node >= 2` 时才被创建。这是后面所有逻辑的前提——**没有多 DP 组，就没有本讲后面的分流与均衡**。

#### 4.1.4 代码实践

1. **实践目标**：从命令行参数和 Router 初始化两个入口，确认 DP 组数是如何决定的。
2. **操作步骤**：
   - 打开 [lightllm/server/api_cli.py:224](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L224)，阅读 `--dp` 的 help。
   - 打开 [lightllm/server/router/manager.py:54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L54)，记录 `dp_size_in_node` 的公式。
3. **需要观察的现象**：在脑中或纸上推演几种部署形态下 `dp_size_in_node` 的取值。
4. **预期结果**（待本地结合真实模型验证部署可行性）：

   | 启动参数 | nnodes | dp_size_in_node | 说明 |
   | --- | --- | --- | --- |
   | `--dp 1`（默认） | 1 | 1 | 单 DP 组，`DpQueue` 不创建 |
   | `--tp 2 --dp 4`（8 卡单机） | 1 | 4 | 4 个 DP 组，每组 2 卡 TP |
   | `--dp 8`（8 卡单机，deepseekv2） | 1 | 8 | 8 个 DP 组，每组 1 卡 |
   | `--dp 4`，2 机 | 2 | 2 | 每机 2 个 DP 组 |

5. 注意：实际能否以 `dp>1` 启动还依赖模型与 GPU 数量，本步只验证「参数→组数」的折算逻辑，不必真的拉起服务。

#### 4.1.5 小练习与答案

**练习 1**：为什么帮助文档强调「使用 deepseekv2 模型时建议 `dp == tp`」，而一般模型建议保持 `dp=1`？

> **参考答案**：DeepSeek-V2 采用 MLA 注意力，KV Cache 被高度压缩（见 u5-l5），单卡的 KV 显存往往用不满，多卡全做 TP 的边际收益低；切成多个 DP 组能并行处理更多请求、提高吞吐。而一般模型 KV 较大，多卡做 TP 才能装下一个长上下文请求的 KV，强行 `dp>1` 反而让每个 DP 组能服务的上下文变短。

**练习 2**：`dp // nnodes` 为什么要外面再套一层 `max(1, ...)`？

> **参考答案**：多机纯 TP 模式下 `dp=1`、`nnodes>1`，`1 // nnodes == 0`，会让本机 DP 组数变成 0 而崩溃；`max(1, ...)` 把这种退化情况兜底成 1 个 DP 组。

### 4.2 DP 队列（DpQueue）

#### 4.2.1 概念说明

`DpQueue` 是 DP 模式的「外壳」。它对 Router 主循环呈现的接口（`extend`、`generate_new_batch`、`update_token_load`、`is_busy`）和单个 `BaseQueue` 完全一致——Router 根本不需要知道自己面对的是 1 个队列还是 N 个队列。区别在于：`DpQueue` 内部其实持有 `dp_size_in_node` 个 `inner_queues`（每个是一个完整的 `ChunkedPrefillQueue`），外加一个 **负载均衡器** `dp_balancer`，负责决定每个新请求归哪个内部队列。

用一个比喻：`DpQueue` 像一个前台接待员，手里有 N 个窗口（内部队列）；来了一位客人（请求），他要么按客人自己的要求直接送到指定窗口，要么交给一个叫号系统（均衡器）挑一个窗口。

#### 4.2.2 核心流程

`DpQueue` 的生命周期里有两个关键流程：

**A. 请求进入（`extend`）**：

1. 取请求组的 `suggested_dp_index`。
2. 如果它是一个合法的本机 DP 组号（`0 <= idx < dp_size_in_node`）——说明上游（如 PD master）已经指定了——直接塞进对应的内部队列。
3. 否则（默认 `-1`，或越界）——**先放进暂存区 `reqs_waiting_for_dp_index`，不立刻分配**，留给均衡器在下一拍调度时统一决定。

**B. 每拍调度（`generate_new_batch`）**：

1. 调 `dp_balancer.assign_reqs_to_dp(...)`：把暂存区里的请求组分发到各内部队列（这一步才真正写 `suggested_dp_index` 并入队）。
2. 对每个内部队列各跑一次 `generate_new_batch(current_batch)`，得到每个 DP 组各自挑出的新 batch。
3. 用 `_merge_batch` 把这些子 batch 合并成一个大 batch 返回给 Router 主循环。

注意第 3 步——合并后的是**一个** batch，里面混着多个 DP 组的请求，靠每个请求自带的 `suggested_dp_index` 区分归属。下游（`Batch` 的统计方法、backend 推理）正是靠这个字段按 DP 组分别处理的。

#### 4.2.3 源码精读

先看 `DpQueue` 的构造，它一次性建好 N 个内部队列、把每个队列的 prefill 预算翻倍、并选定均衡器：

```python
# lightllm/server/router/req_queue/dp_base_queue.py:11-27
class DpQueue:
    def __init__(self, args, router, base_queue_class, dp_size_in_node) -> None:
        self.dp_size_in_node = dp_size_in_node
        self.base_queue_class = base_queue_class
        from lightllm.server.router.manager import RouterManager

        self.router: RouterManager = router
        self.inner_queues: List[BaseQueue] = [
            base_queue_class(args, router, dp_index, dp_size_in_node) for dp_index in range(self.dp_size_in_node)
        ]
        # 在调度这放松，在推理时约束。
        # 避免prefill 模式下的情况下，推理完成了，调度没及时获取信息，导致调度bs 过小
        for queue in self.inner_queues:
            queue.batch_max_tokens = int(args.batch_max_tokens * 2)
        self.dp_balancer = get_dp_balancer(args, dp_size_in_node, self.inner_queues)
        self.reqs_waiting_for_dp_index: List[List[Req]] = []
        return
```

要点逐条说明：

- 第 18-20 行：每个内部队列被传入自己的 `dp_index`，从 0 到 `dp_size_in_node-1`，内部队列据此知道自己代表第几组。
- 第 23-24 行：把每条内部队列的 `batch_max_tokens` **翻倍**。注释解释了意图——「在调度这放松，在推理时约束」。这是因为多 DP 组时调度信息有滞后（某组推理刚完成，调度还没及时收到），放宽调度期预算能避免挑出的 batch 太小；真正的约束在后端推理时再做。
- 第 25 行：`get_dp_balancer` 根据 `--dp_balancer` 选具体均衡器（见 4.3）。
- 第 26 行：暂存区，存「还没决定去哪个 DP 组」的请求组。

再看请求进入时的分流（本讲最核心的方法之一）：

```python
# lightllm/server/router/req_queue/dp_base_queue.py:53-60
    def extend(self, req_group: List[Req]):
        suggested_dp_index = req_group[0].sample_params.suggested_dp_index
        if suggested_dp_index >= self.dp_size_in_node or suggested_dp_index < 0:
            # 同一个组的，要分配在同一个 dp 上
            self.reqs_waiting_for_dp_index.append(req_group)
        else:
            self.inner_queues[suggested_dp_index].extend(req_group)
        return
```

注意两个细节：① 这里取的是 `req_group[0]`（同组请求共享一个 `suggested_dp_index`，注释「同一个组的，要分配在同一个 dp 上」）；② 当 `suggested_dp_index` 默认为 `-1` 时（[sampling_params.py:335](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/sampling_params.py#L335)），`-1 < 0` 成立，请求被丢进暂存区等待均衡器发落。

然后是每拍调度的「分流 + 各组挑批 + 合批」：

```python
# lightllm/server/router/req_queue/dp_base_queue.py:37-51
    def generate_new_batch(self, current_batch: Batch):
        self.dp_balancer.assign_reqs_to_dp(current_batch, self.reqs_waiting_for_dp_index)
        batches = [
            self.inner_queues[dp_index].generate_new_batch(current_batch) for dp_index in range(self.dp_size_in_node)
        ]
        return self._merge_batch(batches)

    def _merge_batch(self, dp_batches: List[Batch]):
        merged_batch: Batch = None
        for iter_batch in dp_batches:
            if merged_batch is not None:
                merged_batch.merge(iter_batch)
            else:
                merged_batch = iter_batch
        return merged_batch
```

- 第 38 行先做分流（均衡器把暂存区的请求写进各内部队列）；
- 第 39-41 行对每个内部队列并行地挑批（内部队列各自按 u2-l6 的 chunked prefill 逻辑判断三道闸门）；
- 第 42 行合批，`_merge_batch` 调 `Batch.merge` 把子 batch 的 `reqs` 拼到一起。

`Batch.merge` 的实现很简单，就是拼接请求列表并重建索引：

```python
# lightllm/server/router/batch.py:78-85
    def merge(self, mini_batch: "Batch"):
        if mini_batch is None:
            return

        for _req in mini_batch.reqs:
            self.reqs.append(_req)
        self.id_to_reqs = {req.request_id: req for req in self.reqs}
        return
```

最后看两个辅助方法。`is_busy` 被 `DpQueue` 直接覆写为永远返回 `True`——因为「忙不忙」要按 DP 组分别判断（内部队列各自有 `is_busy`），外壳层面没有统一的 busy 概念，索性返回 True 让逻辑走「不省 token 估算」的分支：

```python
# lightllm/server/router/req_queue/dp_base_queue.py:62-63
    def is_busy(self):
        return True
```

`update_token_load` 则按 DP 组逐组更新共享负载表（与 u4-l3 讲的 `shared_token_load` 对接）：

```python
# lightllm/server/router/req_queue/dp_base_queue.py:65-75
    def update_token_load(self, current_batch: Batch, force_update=False):
        if self.router.shared_token_load.need_update_dynamic_max_load() or force_update:
            for dp_index in range(self.dp_size_in_node):
                estimated_peak_token_count, dynamic_max_load = self.inner_queues[dp_index].calcu_batch_token_load(
                    current_batch
                )
                token_ratio1 = self.router.get_used_tokens(dp_index) / self.router.max_total_token_num
                self.router.shared_token_load.set_current_load(token_ratio1, dp_index)
                self.router.shared_token_load.set_estimated_peak_token_count(estimated_peak_token_count, dp_index)
                self.router.shared_token_load.set_dynamic_max_load(dynamic_max_load, dp_index)
        return
```

三个 `set_*` 调用把第 i 个 DP 组的负载写进 `shared_token_load` 的第 i 行（与 u4-l3 讲的 per-DP-组 负载表对接），下游 HttpServer 准入、PD master 路由、指标上报都从这张共享表读。

#### 4.2.4 代码实践

1. **实践目标**：追踪一次「请求进入 → 暂存 → 均衡器分流 → 内部队列挑批 → 合批」的完整代码路径。
2. **操作步骤**：
   - 在 [dp_base_queue.py:53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py#L53) 的 `extend` 处，确认默认请求（`suggested_dp_index == -1`）走第 57 行进暂存区。
   - 在 [dp_base_queue.py:37](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py#L37) 的 `generate_new_batch` 处，依次标出三步：分流（38 行）、各组挑批（39-41 行）、合批（42 行）。
   - 在 [batch.py:44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L44) 看 `get_all_dp_req_num` 如何用 `suggested_dp_index` 把一个混合 batch 按组拆开计数（这正是合批后仍能「按 DP 组分别处理」的依据）。
3. **需要观察的现象**：合批后的 `Batch.reqs` 里同时存在 `suggested_dp_index=0` 和 `suggested_dp_index=1` 的请求；`get_all_dp_req_num` 返回的是一个长度为 `dp_size_in_node` 的列表。
4. **预期结果**：能画出「Router 主循环 → DpQueue.generate_new_batch → N 个 inner_queue.generate_new_batch → _merge_batch → 单个 Batch（含多组请求）」的调用链。
5. 若想本地验证，可在 `generate_new_batch` 入口临时加一行日志打印每个内部队列挑出的请求数（**示例代码**，非项目原有）：
   ```python
   # 示例代码：仅用于观察，验证后请删除
   logger.debug([ (i, len(b.reqs) if b else 0) for i, b in enumerate(batches) ])
   ```

#### 4.2.5 小练习与答案

**练习 1**：为什么 `DpQueue.generate_new_batch` 要先把暂存区请求分流（第 38 行），再让各内部队列挑批（第 39-41 行），而不是反过来？

> **参考答案**：因为内部队列挑批时需要知道「自己组里现在有多少等待请求」才能判断是否还能加新请求（三道闸门里的请求数上限）。如果不先分流，新到的请求就不在任何一个内部队列里，本拍无法被考虑；先分流再挑批，保证本拍到达的请求本拍就能参与调度。

**练习 2**：`DpQueue.is_busy()` 为什么直接返回 `True`？

> **参考答案**：「忙」是一个 per-DP-组 的概念，每个内部队列有自己的 `is_busy`（基于该组 token 使用率）。外壳层面没有统一的 busy 状态，返回 `True` 让 chunked prefill 的 token 估算走不省略的保守分支，避免外壳层给出错误的「不忙」信号。

### 4.3 负载均衡（roundrobin vs bs_balancer）

#### 4.3.1 概念说明

负载均衡器回答一个问题：**当一个新请求没有指定 DP 组（`suggested_dp_index == -1`）时，应该把它送到哪个 DP 组？**

这是个真问题，因为 DP 模式有个「坑」：**如果一个 DP 组这一拍没有任何请求，backend 在推理时会被迫 padding 一个空请求，白白浪费一次 GPU 计算**。`bs_balancer` 的类注释直接点明了这一点：

```python
# lightllm/server/router/req_queue/dp_balancer/bs.py:11-16
class DpBsBalancer(DpBalancer):
    """
    This balancer is main to balance the batch size of each dp rank.
    Because, for dp mode, if it exists a dp rank without any request, it will
    padding a request and cause the waste of GPU compute resource.
    """
```

LightLLM 提供两种均衡器，由 `--dp_balancer` 选择，**默认 `bs_balancer`**：

```python
# lightllm/server/api_cli.py:232-238
parser.add_argument(
    "--dp_balancer",
    type=str,
    default="bs_balancer",
    choices=["round_robin", "bs_balancer"],
    help="the dp balancer type, default is bs_balancer",
)
```

> ⚠️ **重要区分**：本节讲的是 **Router 层的 `--dp_balancer`**，它决定「新请求去哪个 DP 组」，操作对象是**请求**。后面 4.3.5 会专门讲另一个同名概念 **Backend 层的 `--enable_dp_prefill_balance`**，它在 prefill 时跨 DP 组重分**token**，是两套完全不同的机制，不要混淆。这也是本讲实践任务要求你指出的关键点。

#### 4.3.2 核心流程

两种均衡器都实现同一个抽象接口 `assign_reqs_to_dp(current_batch, reqs_waiting_for_dp_index)`：吃进「当前在跑的 batch」和「暂存区里待分配的请求组列表」，副作用是把每个请求组挂到某个内部队列上，并清空暂存区。

抽象基类 `DpBalancer` 只持有 `dp_size_in_node` 和 `inner_queues` 两个引用，外加一个抽象方法：

```python
# lightllm/server/router/req_queue/dp_balancer/base.py:11-23
class DpBalancer(ABC):
    """
    DP负载均衡器基类
    定义了负载均衡策略的接口，子类可以实现不同的负载均衡算法
    """

    def __init__(self, dp_size_in_node: int, inner_queues: List[BaseQueue]):
        self.dp_size_in_node = dp_size_in_node
        self.inner_queues = inner_queues

    @abstractmethod
    def assign_reqs_to_dp(self, current_batch: Batch, reqs_waiting_for_dp_index: List[List[Req]]) -> None:
        pass
```

工厂方法 `get_dp_balancer` 按参数二选一：

```python
# lightllm/server/router/req_queue/dp_balancer/__init__.py:7-13
def get_dp_balancer(args, dp_size_in_node: int, inner_queues: List[BaseQueue]):
    if args.dp_balancer == "round_robin":
        return RoundRobinDpBalancer(dp_size_in_node, inner_queues)
    elif args.dp_balancer == "bs_balancer":
        return DpBsBalancer(dp_size_in_node, inner_queues)
    else:
        raise ValueError(f"Invalid dp balancer: {args.dp_balancer}")
```

两种策略的核心差异可以用一句话概括：

- **`round_robin`（轮询）**：在「等待队列最短」的 DP 组之间**轮换**，目标是让各组**等待请求数**均衡。
- **`bs_balancer`（按 batch size）**：看「在跑 + 等待」的**总请求数**，永远往**总负载最小**的组塞，目标是让各组**实际 batch size** 均衡（避免某组空转 padding）。

#### 4.3.3 源码精读

**策略一：RoundRobinDpBalancer（轮询）**

```python
# lightllm/server/router/req_queue/dp_balancer/roundrobin.py:11-42
class RoundRobinDpBalancer(DpBalancer):
    """
    轮询负载均衡器
    在队列长度最小的DP中进行轮询选择
    """

    def __init__(self, dp_size_in_node: int, inner_queues: List[BaseQueue]):
        super().__init__(dp_size_in_node, inner_queues)
        self.pre_select_dp_index = self.dp_size_in_node - 1

    def get_suggest_dp_index(self) -> int:
        min_length = min(len(queue.waiting_req_list) for queue in self.inner_queues)
        select_dp_indexes = [
            i for i, queue in enumerate(self.inner_queues) if len(queue.waiting_req_list) == min_length
        ]

        # 如果没有可选择的索引，随机选择一个
        if not select_dp_indexes:
            self.pre_select_dp_index = random.randint(0, self.dp_size_in_node - 1)
            return self.pre_select_dp_index

        # 轮询选择
        for i in range(self.dp_size_in_node):
            next_dp_index = (self.pre_select_dp_index + i + 1) % self.dp_size_in_node
            if next_dp_index in select_dp_indexes:
                self.pre_select_dp_index = next_dp_index
                return self.pre_select_dp_index

        self.pre_select_dp_index = random.choice(select_dp_indexes)
        return self.pre_select_dp_index
```

阅读要点：

- `pre_select_dp_index` 记录「上一次选的组」，初值为 `dp_size_in_node - 1`（最后一组），这样第一次轮询会从第 0 组开始。
- 第 24 行先算出所有内部队列里**最短的等待队列长度** `min_length`。
- 第 25-27 行筛出所有「等待队列 == 最短」的候选组 `select_dp_indexes`。
- 第 35-39 行在候选组里做**轮询**：从上一次选的组的下一个开始，按取模顺序找第一个落在候选集合里的组。这样即便总是同一批候选组，也会循环分配，避免反复砸同一组。
- 第 30-32 行是对「候选为空」的兜底（实际 `min` 在非空列表上一定有候选，这里更像是防御性代码）。

它的分配入口把每个请求组都问一遍 `get_suggest_dp_index`，写回 `suggested_dp_index` 并入队：

```python
# lightllm/server/router/req_queue/dp_balancer/roundrobin.py:44-51
    def assign_reqs_to_dp(self, current_batch: Batch, reqs_waiting_for_dp_index: List[List[Req]]) -> None:
        for req_group in reqs_waiting_for_dp_index:
            suggested_dp_index = self.get_suggest_dp_index()
            for req in req_group:
                req.sample_params.suggested_dp_index = suggested_dp_index
            self.inner_queues[suggested_dp_index].extend(req_group)
        reqs_waiting_for_dp_index.clear()
        return
```

注意：它**完全没用 `current_batch` 参数**——轮询策略只看各组的等待队列长度，不看当前在跑的请求。这是一个关键差异点。

**策略二：DpBsBalancer（按 batch size，默认）**

```python
# lightllm/server/router/req_queue/dp_balancer/bs.py:18-45
    def __init__(self, dp_size_in_node: int, inner_queues: List[BaseQueue]):
        super().__init__(dp_size_in_node, inner_queues)

    def assign_reqs_to_dp(self, current_batch: Batch, reqs_waiting_for_dp_index: List[List[Req]]) -> None:
        if len(reqs_waiting_for_dp_index) == 0:
            return
        # calculate the total load of each dp rank
        all_dp_req_num = [0 for _ in range(self.dp_size_in_node)]
        if current_batch is not None:
            all_dp_req_num = current_batch.get_all_dp_req_num()
        total_load_per_dp = [
            all_dp_req_num[i] + len(self.inner_queues[i].waiting_req_list) for i in range(self.dp_size_in_node)
        ]
        for req_group in reqs_waiting_for_dp_index:
            # find the dp rank with minimum load
            min_load = min(total_load_per_dp)
            select_dp_indexes = [i for i in range(self.dp_size_in_node) if total_load_per_dp[i] == min_load]
            suggested_dp_index = random.choice(select_dp_indexes)

            # assign the request to the dp rank and update the load count
            for req in req_group:
                req.sample_params.suggested_dp_index = suggested_dp_index
            self.inner_queues[suggested_dp_index].extend(req_group)
            # update the load count for this dp rank
            total_load_per_dp[suggested_dp_index] += len(req_group)

        reqs_waiting_for_dp_index.clear()
        return
```

阅读要点：

- 第 26-27 行：用 `current_batch.get_all_dp_req_num()` 取「当前在跑的 batch 里每个 DP 组各有多少请求」。这个方法正是 [batch.py:44-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L44) 提供的，按 `suggested_dp_index` 统计：

```python
# lightllm/server/router/batch.py:44-51
    def get_all_dp_req_num(self) -> List[int]:
        if self.dp_size_in_node == 1:
            return [len(self.reqs)]

        all_dp_req_num = [0 for _ in range(self.dp_size_in_node)]
        for req in self.reqs:
            all_dp_req_num[req.sample_params.suggested_dp_index] += 1
        return all_dp_req_num
```

- 第 28-30 行：`total_load_per_dp[i] = 在跑请求数[i] + 等待请求数[i]`。这就是「batch size 视角」的总负载——既算正在推理的，也算排队等着的。
- 第 31-42 行：对每个待分配请求组，挑总负载最小的组（并列则随机选一个），分配后**立刻把该组计数加上本组请求数**（第 42 行）。这一步很关键：它让连续分配多个请求组时，负载会自动「滚雪球」地往还闲的组堆，而不是把一批请求全塞进同一个「当下最小」的组。

**两者对比**：

| 维度 | `round_robin` | `bs_balancer`（默认） |
| --- | --- | --- |
| 衡量指标 | 各组**等待队列**长度 | 各组「**在跑 + 等待**」总请求数 |
| 是否看 `current_batch` | 否 | 是 |
| 选择方式 | 候选组间轮询 | 总负载最小（并列随机） |
| 直接目标 | 等待请求数均衡 | 实际 batch size 均衡，避免某组空转 padding |
| 连续分配多个组的行为 | 轮换着发 | 动态更新计数，自动往闲组堆 |

为什么默认选 `bs_balancer`？因为 DP 推理时，**只要某个 DP 组这一拍 batch 为空，backend 就得 padding 一个假请求陪跑，浪费算力**；`bs_balancer` 直接以「各组实际请求数」为目标，最能避免这种空转。

#### 4.3.4 代码实践

1. **实践目标**：对比两种均衡器在相同输入下的分配结果，并理解 `enable_dp_prefill_balance` 的前置条件。
2. **操作步骤**：
   - 读 [roundrobin.py:21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/roundrobin.py#L21) 与 [bs.py:21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/bs.py#L21)，标注两者的「指标」差异（等待队列 vs 在跑+等待）。
   - 在 [bs.py:27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_balancer/bs.py#L27) 处确认它调用了 `current_batch.get_all_dp_req_num()`，而 roundrobin 完全没用到 `current_batch`。
   - 读 [api_start.py:197-198](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L197) 看 `enable_dp_prefill_balance` 的断言。
3. **需要观察的现象 / 思考推演**：
   - 假设 2 个 DP 组，`current_batch` 里组 0 有 3 个请求、组 1 有 0 个，暂存区来了 2 个新请求组（各 1 个请求）。
     - `round_robin`：只看等待队列（初始都为 0），会在两组间轮询 → 可能分给组 0、组 1 各一个（取决于 `pre_select_dp_index`），**忽略了组 1 当前在跑 0 个请求、更应该优先填的事实**。
     - `bs_balancer`：总负载组 0=3、组 1=0，第一个请求组分给组 1（负载变 1），第二个请求组再看总负载组 0=3、组 1=1，仍分给组 1（负载变 2）。**两个新请求都进了更闲的组 1**，更接近 batch size 均衡。
4. **预期结果**：能口头复述上表对比，并解释「默认 `bs_balancer` 是为了防 DP 组空转 padding」。
5. **`enable_dp_prefill_balance` 的前置条件**（来自 [api_start.py:197-198](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L197)）：
   ```python
   if args.enable_dp_prefill_balance:
       assert args.enable_tpsp_mix_mode and args.dp > 1, "need set --enable_tpsp_mix_mode firstly and --dp > 1"
   ```
   即必须**同时**满足：① 已开 `--enable_tpsp_mix_mode`（TP+SP 混合并行，见 u6-l2）；② `--dp > 1`。CLI help 也写明「need set --enable_tpsp_mix_mode first」（[api_cli.py:370-373](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L370)）。

#### 4.3.5 两种「DP balance」的区别（重要）

这是本讲最容易混淆、也最值得单独点出的点。LightLLM 里有**两个**都叫「dp balance」的机制，分处不同层、解决不同问题：

| 机制 | 层次 | 开关 | 操作对象 | 解决的问题 |
| --- | --- | --- | --- | --- |
| **请求分发均衡** | Router 调度层 | `--dp_balancer round_robin/bs_balancer` | **请求**（决定新请求去哪个 DP 组） | 各 DP 组请求数不均 |
| **prefill 重分布** | Backend 推理层 | `--enable_dp_prefill_balance` | **token**（prefill 时跨组重分数据） | 各 DP 组 prefill 计算量不均 |

`--enable_dp_prefill_balance` 在推理后端的 `_context_forward` 里生效：prefill 前 `prepare_prefill_dp_balance()` 先 all-gather 各组的输入长度，再用 all-to-all 把数据重新均摊到各组，prefill 算完后再 all-to-all 还原（`_all_to_all_unbalance_get`）：

```python
# lightllm/common/basemodel/basemodel.py:659-662
        if self.args.enable_dp_prefill_balance:
            assert not self.args.enable_prefill_cudagraph, "not support now"
            infer_state.prepare_prefill_dp_balance()
            input_embs = infer_state._all_to_all_balance_get(data=input_embs)
```

```python
# lightllm/common/basemodel/basemodel.py:700-701
        if infer_state.need_dp_prefill_balance:
            last_input_embs = infer_state._all_to_all_unbalance_get(data=last_input_embs)
```

之所以需要它，是因为即便请求分发均衡了（请求数差不多），**不同请求的 prompt 长度可能差很多**，导致某组 prefill 的 token 数远多于另一组、整批要等最慢的那组。`enable_dp_prefill_balance` 用一次 all-to-all 把 token 摊平，降低 prefill 尾延迟。它依赖 TPSP 混合并行提供的可拆分通信原语，所以前置条件才要求先开 `--enable_tpsp_mix_mode`。

> 一句话记法：`--dp_balancer` 管的是**请求往哪送**（Router，本讲三个核心文件），`--enable_dp_prefill_balance` 管的是**token 怎么摊**（Backend）。

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，为一次 DeepSeek-V2 双 DP 组部署「画一张调度决策图」。

背景：一台机器上 `--tp 4 --dp 2`（共 8 卡），即 2 个 DP 组，默认 `--dp_balancer bs_balancer`。

请完成：

1. **画队列结构图**：标出 `DpQueue`（外壳）→ 2 个 `inner_queues`（dp_index=0/1）→ 每个内部队列各自的 `waiting_req_list`，以及暂存区 `reqs_waiting_for_dp_index` 和均衡器 `dp_balancer`。
2. **推演一次调度**：假设某一拍 `current_batch` 里组 0 有 2 个请求、组 1 有 0 个请求，暂存区有 3 个新请求组（每组 1 个请求）。写出 `bs_balancer.assign_reqs_to_dp` 执行后：
   - 每一步 `total_load_per_dp` 的变化（初始 `[2, 0]`）。
   - 最终 3 个请求组的 `suggested_dp_index` 各是多少。
3. **改用 `round_robin` 重推**：同样的初始状态（暂存区 3 个请求组），假设 `pre_select_dp_index` 初值是 1，写出 `round_robin` 会如何分配，并说明它是否考虑了「组 1 当前在跑 0 个请求」。
4. **回答**：如果运维想进一步打开 `--enable_dp_prefill_balance`，他还需要补哪两个启动参数？为什么？

参考答案要点：

- 第 2 步：初始 `total_load = [2, 0]`。第 1 个请求组 → 组 1（`[2,1]`）；第 2 个 → 组 1（`[2,2]`）；第 3 个 → 并列最小，随机选 0 或 1。可见 `bs_balancer` 会优先把请求堆到更闲的组 1，直到两组拉平。
- 第 3 步：`round_robin` 只看等待队列（两组等待数初始都为 0），在两组间轮询，会大致均分（如 1→0、2→1、3→0）；它**不参考** `current_batch`，所以无视「组 1 当前在跑 0 个请求」。
- 第 4 步：需补 `--enable_tpsp_mix_mode` 并保证 `--dp > 1`（[api_start.py:197-198](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L197)）。因为 prefill 重分布依赖 TPSP 提供的可拆分 all-to-all 通信原语，且只在多 DP 组时有意义。

## 6. 本讲小结

- **数据并行（DP）**：把机器上的 GPU 切成多个 DP 组，各组独立跑不同请求、各有独立 KV Cache 与队列；`dp_size_in_node = max(1, dp // nnodes)`；DeepSeek-V2 因 MLA 压缩 KV 常用 `dp == tp`。
- **`DpQueue` 是 DP 模式的外壳**：只在 `dp_size_in_node >= 2` 时创建，内部持有 N 个 `inner_queues` 和一个均衡器；对 Router 暴露与单队列一致的接口。
- **请求分流两段式**：`extend` 时 `suggested_dp_index == -1` 的请求先进暂存区；`generate_new_batch` 时先由均衡器分流、各组再各自挑批、最后 `_merge_batch` 合成一个含多组请求的 batch。
- **两种均衡策略**：`round_robin` 看「等待队列最短」并在候选间轮询；`bs_balancer`（默认）看「在跑+等待」总请求数最小，目的是避免某 DP 组空转 padding。
- **两个「dp balance」要分清**：`--dp_balancer`（Router 层，分发请求）vs `--enable_dp_prefill_balance`（Backend 层，prefill 时重分 token）；后者前置条件是 `--enable_tpsp_mix_mode` 且 `--dp > 1`。
- **调度放宽**：`DpQueue` 把各内部队列的 `batch_max_tokens` 翻倍，以应对多 DP 组下调度信息滞后导致的 batch 偏小。

## 7. 下一步学习建议

- **向下深入 prefill 重分布**：读 [basemodel.py:655](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/basemodel.py#L655) 的 `_context_forward` 与 [infer_struct.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/infer_struct.py) 里 `prepare_prefill_dp_balance` 的 all-to-all 实现，理解 `enable_dp_prefill_balance` 的算子细节（衔接 u6-l2 TPSP 混合并行）。
- **横向扩展 DP 高级特性**：阅读 `--enable_dp_prompt_cache_fetch`（[api_cli.py:778](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_cli.py#L778)）与 `base_backend.py` 的 `init_dp_kv_shared`，看跨 DP 组共享 KV 内存管理器做前缀缓存抓取的机制。
- **回到调度闭环**：结合 u2-l5（Router 调度循环）、u2-l6（chunked prefill）、u4-l3（token 负载估算），把本讲的 DP 分流放进完整的「每拍 `_step`」流程中理解。
- **PD 分离视角**：u7-l1 讲的 PD 分离中，PD master 也会给请求预先指定 `suggested_dp_index`（合法值，跳过均衡器直达内部队列），可作为本讲 `extend` 分支的另一条入口继续追踪。
