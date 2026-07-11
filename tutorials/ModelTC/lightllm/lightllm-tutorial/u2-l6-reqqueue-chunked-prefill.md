# 请求队列与 chunked prefill 调度

## 1. 本讲目标

上一讲（u2-l5）我们看清了 Router 的心跳主循环 `_step`：它每拍把「新请求调度进 batch」「下发 backend」「过滤已完成请求」按固定顺序做一遍。但那一讲有意留下了一个黑盒——`_step` 里那句 `_generate_new_batch()` 到底是怎么从一堆等待请求里挑出一批、又怎么决定「本轮 prefill 多少 token」的？

本讲就打开这个黑盒。读完本讲你应当能够：

- 说清 `BaseQueue` / `ChunkedPrefillQueue` 的抽象职责，以及 `build_req_queue` 如何根据启动参数选出具体队列类。
- 理解 **chunked prefill（分块预填充）** 的动机：长 prompt 不再一次性算完，而是按块切分，与正在进行的 decode 共享每拍的 token 预算。
- 逐行读懂 `generate_new_batch` 与 `_can_add_new_req`，并解释「当前 batch 为空 / 不为空」时 token 负载计算策略的差异。
- 认识 token load 估算公式如何为「能否再加入新请求」与「对外暴露给 HttpServer 的准入判断」提供依据。

## 2. 前置知识

- **prefill 与 decode 两阶段**：LLM 推理分两段。prefill 阶段处理整段输入 prompt，计算量大、显存增长快；decode 阶段每步只生成一个新 token，计算量小但要持续占用已积累的 KV Cache。
- **continuous batching（连续批处理）**：传统做法是等一批 prefill 全部算完才能服务新请求；连续批处理允许新请求在老请求 decode 时插队进来，提升吞吐。
- **chunked prefill（分块预填充）**：是 continuous batching 的进一步细化——当一个 prompt 很长（比如上万 token），不再要求一拍算完，而是每拍只算 `chunked_prefill_size` 个 token，把剩下的留到后续拍。这样长 prompt 不会长时间霸占 GPU、阻塞正在 decode 的短请求。
- **token 预算 / token load**：Router 并不直接看显存字节数，而是把「KV Cache 能容纳多少 token」当作一个总量（`max_total_token_num`），用「当前 + 预计峰值会用掉多少 token」来判断还能不能塞新请求。这就是本讲反复出现的「token 负载」。
- **`is_busy` 与激进/保守调度**：上一讲讲过，当 token 占用率超过 `router_token_ratio` 阈值时 Router 进入「保守」状态（`is_busy=True`），否则「激进」。本讲会看到这个布尔值如何渗透进 token 估算。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/req_queue/\_\_init\_\_.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py) | 队列工厂：`_get_req_queue_class` 按启动参数选具体队列类，`build_req_queue` 按 `dp_size_in_node` 决定是否套一层 `DpQueue`。 |
| [lightllm/server/router/req_queue/base_queue.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py) | `BaseQueue` 抽象基类：定义 `extend`/`is_busy`/`generate_new_batch`/`calcu_batch_token_load`/`update_token_load` 等公共接口与默认实现。 |
| [lightllm/server/router/req_queue/chunked_prefill/impl.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py) | `ChunkedPrefillQueue`：本讲主角，实现 chunked prefill 调度算法。 |
| [lightllm/server/router/batch.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py) | `Batch` 类，提供 `get_batch_decode_need_tokens` 等批量统计方法。 |
| [lightllm/server/core/objs/req.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py) | `ChunkedPrefillReq`：提供 `get_tuple_tokens`、`get_decode_need_tokens`、`get_first_router_need_tokens` 三个负载估算原子。 |
| [lightllm/server/router/req_queue/dp_base_queue.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py) | `DpQueue`：数据并行（DP）场景下包裹一组内层队列的容器，展示队列抽象的可组合性。 |

## 4. 核心概念与源码讲解

### 4.1 请求队列的抽象设计

#### 4.1.1 概念说明

Router 每拍要回答一个问题：「等待区里有哪些请求可以被推上去推理？」这个问题看似简单，但回答方式有好几种：

- **普通连续批处理**：只要显存够就把整条 prompt 一次性 prefill。
- **chunked prefill**：把长 prompt 切块，每拍只 prefill 一块。
- **beam search（diverse_mode）**：一个请求带多条候选序列，调度时要考虑 beam 宽度。
- **PD 分离（prefill/decode）**：prefill 节点和 decode 节点各自有独立队列。

LightLLM 没有为每种场景各写一套 Router，而是把「挑选请求成批」这件事抽象成一个**队列对象** `req_queue`，Router 主循环只调它的两个方法：

- `generate_new_batch(current_batch)`：从等待区挑出一批新请求；
- `update_token_load(current_batch)`：把当前负载估算发布到共享内存。

不同启动模式下，`build_req_queue` 装配不同的队列实现类，Router 的主循环代码完全不变。这就是抽象的价值——**调度策略可替换，调用方稳定**。

#### 4.1.2 核心流程

队列的装配流程：

```text
build_req_queue(args, router, dp_size_in_node)
   │
   ├─ _get_req_queue_class(args)  →  选出 base_queue_class
   │      diverse_mode        → ChunkedBeamContinuesBatchQueue
   │      token_healing_mode  → ChunkedPrefillQueue
   │      output_constraint   → ChunkedPrefillQueue
   │      first_token_constraint → ChunkedPrefillQueue
   │      run_mode in [prefill,decode] → PDQueue
   │      其余（含 disable_chunked_prefill、normal）→ ChunkedPrefillQueue
   │
   └─ dp_size_in_node == 1 ? base_queue_class(args, router, 0, 1)
                           : DpQueue(args, router, base_queue_class, dp_size_in_node)
```

注意一个反直觉但重要的设计：即便用户加了 `--disable_chunked_prefill`，选出的依然是 `ChunkedPrefillQueue`，而不是另一个类。原因在于 [api_start.py:L275-L276](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L275-L276) 把 `chunked_prefill_size` 直接设成了 `max_req_total_len`（整条 prompt 长度），于是「每拍 prefill 一整块」就退化成了「一次性 prefill」，行为等价于传统连续批处理。源码注释明确说明这是为了**统一实现、减少代码重复**。

`BaseQueue` 在构造时确定三个关键容量参数：

```text
max_total_tokens     = args.max_total_token_num - get_fixed_kv_len()   # KV 池总量（token 数）
batch_max_tokens     = args.batch_max_tokens                            # 每拍 prefill 预算
running_max_req_size = args.running_max_req_size                        # 同时在跑的请求上限
```

其中 `get_fixed_kv_len()` 在特定推理模式（如某些 MLA/MTP 模式）下会预先占用一部分 KV 资源，普通模式下返回 0，不影响计算（见 [base_queue.py:L17-L21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L17-L21) 的注释）。

#### 4.1.3 源码精读

**工厂选择逻辑**：[\_\_init\_\_.py:L7-L24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py#L7-L24) 是一组 `if` 优先级链，最先命中的决定队列类。最后两条说明绝大多数情况都会落到 `ChunkedPrefillQueue`。

[\_\_init\_\_.py:L27-L33](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py#L27-L33) 的 `build_req_queue` 负责 DP 包装：单 DP 组直接返回内层队列；多 DP 组用 `DpQueue` 把若干内层队列收拢成一个对外接口（DP 负载均衡是 u7-l3 的主题）。

**BaseQueue 的公共接口**（[base_queue.py:L9-L86](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L9-L86)）：

- `extend(req_group)`（[L35-L39](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L35-L39)）：把新请求追加到 `waiting_req_list`，并打上 `suggested_dp_index`。Router 收到新请求后正是经 [manager.py:L420](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L420) 的 `self.req_queue.extend(req_group)` 入队。
- `is_busy()`（[L44-L50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L44-L50)）：用 `当前已用 token / max_total_tokens` 与 `router_token_ratio` 比较，超过即「忙」。这个布尔量会传给负载估算，影响输出长度估计（见 4.2）。
- `generate_new_batch` / `_calcu_batch_token_load_batch_not_none`（[L60-L76](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L60-L76)）：抽象方法，由子类实现，本讲主角。
- `update_token_load`（[L78-L85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L78-L85)）：把负载估算写进 `shared_token_load`（共享内存），供 HttpServer 准入判断使用，是 u4-l3 的接口。

#### 4.1.4 代码实践

**实践目标**：确认本机启动会装配出哪种队列类，并理解默认容量参数。

**操作步骤**：

1. 阅读 [\_\_init\_\_.py:L7-L24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py#L7-L24)，推断：在「不指定任何特殊模式、单 DP 组、normal 模式」下，会选中哪个类？
2. 运行 `python -m lightllm.server.api_server --help`，找到 `--chunked_prefill_size`、`--batch_max_tokens`、`--disable_chunked_prefill`、`--running_max_req_size` 四个参数的默认值与说明。
3. 对照 [api_start.py:L285-L294](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L285-L294)，说明 `chunked_prefill_size` 与 `batch_max_tokens` 都为 `None` 时，系统如何自动推导默认值（`dp=1` 时分别得到多少）。

**需要观察的现象**：`--help` 输出里这几个参数的 `default`；以及 `chunked_prefill_size` 默认是 `None`（由启动期推导），不是某个固定数。

**预期结果**：在默认 chunked 模式、`dp=1` 下，`batch_max_tokens` 被推导为 `16384 // 1 = 16384`，`chunked_prefill_size` 被推导为 `16384 // 2 = 8192`；选中的队列类是 `ChunkedPrefillQueue`。若 `dp>1`，外层会再套一个 `DpQueue`。

> 若本地无 GPU 或未安装依赖，无法真正启动服务，上述「`--help` 输出」与「默认值推导」可仅通过阅读源码得出结论，标注「待本地验证」即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `--disable_chunked_prefill` 不用一个独立的队列类？

**参考答案**：因为禁用 chunked prefill 后，只需把 `chunked_prefill_size` 设为整条 prompt 长度（`max_req_total_len`），`ChunkedPrefillQueue` 的「每拍 prefill 一块」就退化成「一拍 prefill 整条」。复用同一份实现可以避免维护两套高度相似的调度代码（见 [\_\_init\_\_.py:L19-L22](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/__init__.py#L19-L22) 注释）。

**练习 2**：`BaseQueue` 里 `max_total_tokens` 为什么不是直接等于 `args.max_total_token_num`？

**参考答案**：要减去 `get_fixed_kv_len()`。某些特定推理模式会预先占用一部分 KV 资源，这部分必须从可用池里扣除，否则调度会把已占用的资源重复计入，导致 OOM。普通模式下 `get_fixed_kv_len()` 返回 0，等价于不减（见 [base_queue.py:L17-L21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L17-L21)）。

---

### 4.2 chunked prefill 调度算法（generate_new_batch）

#### 4.2.1 概念说明

chunked prefill 要解决的核心矛盾是：**一个超长 prompt 的 prefill 如果独占一拍，会让所有正在 decode 的请求卡住几百毫秒**。解法是把 prefill 也变成「每拍做一点」，与 decode 共享同一拍的 token 预算 `batch_max_tokens`：

- 正在 decode 的老请求，每拍消耗少量 token（每请求约 1 个 decode token）；
- 等待区的新请求，每拍最多 prefill `chunked_prefill_size` 个 token；
- 两者加起来不能超过 `batch_max_tokens`，于是长 prompt 的 prefill 被自然分摊到多拍，decode 延迟不会被严重拖累。

一个长 prompt 因此可能要经历好几拍才 prefill 完，期间它在 batch 里处于「还没轮到 decode」的半成品状态——这正是 `get_tuple_tokens` 里那段「延长生命周期」估计要处理的情况（见 4.2.3）。

#### 4.2.2 核心流程

[impl.py:L57-L103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L57-L103) 的 `generate_new_batch` 可拆成 7 步：

```text
输入：current_batch（当前正在跑的 batch，可能为 None）
1. waiting_req_list 为空                  → 直接返回 None
2. 当前 batch 请求数已达 running_max_req_size → 返回 None（满了）
3. 计算 is_busy（激进/保守）
4. 计算 new_batch_first_router_need_tokens：
       current_batch 为 None        → 0
       current_batch 不为 None      → 老请求本拍需要的 decode/prefill token 数
5. _init_cache_list：把老请求的 (a_len, b_len) 填进 cache_len_list
6. 遍历 waiting_req_list，逐个调 _can_add_new_req：
       通过 → 加入 can_run_list；不通过 → break（停止再加）
       被中止（is_aborted）的请求单独收集、释放
7. 用 can_run_list 构造 Batch 返回，并从 waiting_req_list 移除已调度/已中止的请求
```

其中第 4 步是本讲的关键分叉点，也是代码实践要观察的对象：**当 `current_batch` 为空时，prefill 预算从 0 开始计；不为空时，预算先被老请求的 decode 需求占去一部分**。这正体现了「prefill 与 decode 共享预算」。

#### 4.2.3 源码精读

**构造期悄悄翻倍的预算**：[impl.py:L11-L13](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L11-L13) 里 `self.batch_max_tokens = self.batch_max_tokens * 2`。chunked 模式下默认把 prefill 预算再放大一倍，给分块 prefill 更多空间（DP 模式下 [dp_base_queue.py:L23-L24](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py#L23-L24) 也做了同样的放大）。

**第 4 步预算初值的分叉**（[impl.py:L69-L71](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L69-L71)）：

```python
new_batch_first_router_need_tokens = (
    0 if current_batch is None else current_batch.get_batch_decode_need_tokens()[self.dp_index]
)
```

`get_batch_decode_need_tokens`（[batch.py:L25-L32](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/batch.py#L25-L32)）按 DP 维度统计老请求本拍需要的 token 数；而单个请求需要多少由 `get_decode_need_tokens` 决定（[req.py:L395-L408](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L395-L408)）：已 prefill 完的请求返回 1（一步 decode），尚未 prefill 完的返回剩余 prefill 块大小（上限 `chunked_prefill_size`）。

**单个请求能否加入：`_can_add_new_req`**（[impl.py:L27-L54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54)），三道闸门：

```python
need_max_token_num = (left_out_len_array * size_array + cum_run_len_array).max()
ok_token_num  = need_max_token_num < self.max_total_tokens      # 闸门1：峰值 KV 不超池
ok_req_num    = len(self.cache_len_list) <= self.running_max_req_size  # 闸门2：请求数不超上限
new_batch_first_router_need_tokens += req.get_first_router_need_tokens()
ok_prefill    = new_batch_first_router_need_tokens <= self.batch_max_tokens  # 闸门3：本拍 prefill 不超预算
```

- 闸门 3 里的 `get_first_router_need_tokens`（[req.py:L410-L412](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L410-L412)）= `min(input_len + 已输出, chunked_prefill_size)`，即新请求本拍最多贡献一块 prefill。
- 三闸门全过才返回 `True`，并顺手把 `need_max_token_num` 写进 `shared_token_load`（[impl.py:L46-L52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L46-L52)），供外部准入参考。任一不过则返回 `False`，外层 `for` 循环 `break`，不再尝试后面的请求。

**峰值估算公式**：闸门 1 的 `need_max_token_num` 来自一次向量化的「峰值占用」估算。先把所有候选请求按剩余输出 `b_len` 降序排序，对前 i 个请求计算：

\[
\text{peak}(i) = b_{[i]} \cdot i + \sum_{j \le i} a_{[j]}
\]

其中 \(a_{[j]}\) 是第 j 个请求「已运行长度」（已积累的 KV），\(b_{[i]}\) 是排序后第 i 个请求的剩余输出长度。直观含义：当这 i 个长寿命请求同时驻留时，再走 \(b_{[i]}\) 步，每步会给所有 i 个请求各加 1 个 token 的 KV，于是额外占用 \(b_{[i]} \cdot i\)，加上他们已有 prefix 之和 \(\sum a\)。取所有 i 中的最大值，就是「最坏情况下这批请求会占掉多少 token」——一个刻意偏保守的上界，用于防 OOM。`.max()` 选出最重的那个 Admission 点。

**`b_len` 为何被刻意加长**：[req.py:L364-L393](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L364-L393) 的 `get_tuple_tokens` 在算 `b_len` 时额外加了一项 \(\lceil \text{剩余 prefill} / \text{chunked\_prefill\_size} \rceil \cdot (\text{max\_waiting\_token}+1)\)。注释（[req.py:L366-L372](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L366-L372)）解释：chunked prefill 会把请求的 decode 推迟若干拍，等于延长了它在显存里的「生命周期」，于是用模拟加长输出长度的方式把这个延迟补进估算。最后还 `+ ADDED_OUTPUT_LEN`（16，见 [req.py:L354-L358](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L354-L358)）做整体保守余量。`is_busy=True` 时直接用完整的 `max_new_tokens`，否则用一个较短的估计值（[req.py:L375-L380](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L375-L380)）——这就是 4.1 里 `is_busy` 渗透进估算的入口。

#### 4.2.4 代码实践

**实践目标**：说清 `generate_new_batch` 在 `current_batch` 为空与不为空时，token 负载计算策略的区别（本讲义指定的实践任务）。

**操作步骤**：

1. 打开 [impl.py:L57-L103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L57-L103)，定位第 69–71 行给 `new_batch_first_router_need_tokens` 赋初值的三元表达式。
2. 跟踪 `current_batch is None` 分支：初值 = 0，于是 [impl.py:L43-L44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L43-L44) 的 `ok_prefill` 只受新请求自身的 first-chunk token 约束——整份 `batch_max_tokens` 预算都留给新请求 prefill。
3. 跟踪 `current_batch is not None` 分支：初值 = `get_batch_decode_need_tokens()[dp_index]`，即老请求本拍的 decode/prefill 需求。新请求的 first-chunk token 会叠加在这个基数上，于是「老请求 decode 优先吃预算，新 prefill 只能用剩下的」。
4. 阅读注释 [impl.py:L82-L85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L82-L85)，确认只有「从未被调度过」的中止请求才能直接从队列丢弃，暂停请求要由 Router manager 另行过滤，以免「token 泄漏」。

**需要观察的现象**：两种分支下 `new_batch_first_router_need_tokens` 的初值差异，以及它如何决定 `ok_prefill` 闸门的松紧。

**预期结果**：

- **batch 为空**：预算从 0 起算，新请求可独占本拍 prefill 预算，调度更激进、能一次性拉起较多新请求（只要不撞 KV 峰值与请求数上限）。这通常对应「系统刚启动 / 上一批刚跑完」的场景。
- **batch 不为空**：预算先扣除老请求的 decode 需求，新 prefill 只能用剩余预算，调度更克制，从而**保护正在 decode 的请求不被长 prompt 抢占**。这正是 chunked prefill 调度的精髓：prefill 与 decode 共享同一拍的 token 预算。

> 这是一道「源码阅读型实践」，不需要真正跑服务。若想本地验证，可在 [impl.py:L44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L44) 的 `ok_prefill` 前加一行日志，打印 `current_batch is None`、`new_batch_first_router_need_tokens`、`self.batch_max_tokens`，观察两种分支下的取值——属「待本地验证」项。

#### 4.2.5 小练习与答案

**练习 1**：`_can_add_new_req` 里三道闸门 `ok_token_num` / `ok_req_num` / `ok_prefill` 分别防的是哪种资源被耗尽？

**参考答案**：`ok_token_num` 防 **KV Cache 池**（峰值 token 数不超过 `max_total_tokens`）；`ok_req_num` 防 **同时运行请求数**（不超过 `running_max_req_size`，避免 batch 过大撑爆显存/计算）；`ok_prefill` 防 **单拍 prefill 计算量**（不超过 `batch_max_tokens`，避免一拍 prefill 拖垮 decode 延迟）。

**练习 2**：为什么 `get_tuple_tokens` 在保守（`is_busy=True`）和激进两种状态下，对 `cur_max_new_token_len` 的估计不同？

**参考答案**：保守状态下系统显存吃紧，用完整的 `max_new_tokens` 做最坏估计，宁可少塞请求也别 OOM；激进状态下显存宽裕，用一个更短的经验值（`max(1.1×已输出, ema)`）估计，倾向于多塞请求提吞吐。这是一种「显存压力 → 估计长短 → 调度松紧」的负反馈（见 [req.py:L375-L380](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L375-L380)）。

---

### 4.3 token load 估算与对外发布

#### 4.3.1 概念说明

`generate_new_batch` 解决「本轮塞哪些请求」；但 Router 还要回答另一个问题：**「我现在整体有多忙？」** 这个答案不能只留在 Router 进程里——HttpServer 在接新请求前（u2-l2/u4-l3）也要读它来决定「还能不能再接活」。于是队列提供了第二个口径的估算 `calcu_batch_token_load`，由 `update_token_load` 每拍写进共享内存 `shared_token_load`。

注意它和 `generate_new_batch` 里的峰值估算**口径不同**：

- `generate_new_batch` 的峰值估算包含**等待区里即将加入的新请求**（边遍历边加）；
- `calcu_batch_token_load` 只看**当前已在 batch 里的请求**，不碰等待区——它衡量的是「现有负载的峰值」，用于对外广播。

#### 4.3.2 核心流程

```text
每拍（manager.py 的 loop_for_fwd 内）：
  req_queue.update_token_load(running_batch, force_update=...)
     │
     ├─ need_update_dynamic_max_load() 或 force_update 为真？
     │     否 → 直接返回（节流，避免每拍都重算）
     │     是 → calcu_batch_token_load(current_batch)
     │           current_batch 为 None → (0, 0.0)
     │           否则 → _calcu_batch_token_load_batch_not_none
     │                   重新算一遍 cache_len_list（仅当前 batch）
     │                   跑同一个峰值公式，得 (need_max_token_num, 比例)
     │
     ├─ set_current_load(已用/总量)            → 当前占用率
     ├─ set_estimated_peak_token_count(...)    → 预计峰值 token 数
     └─ set_dynamic_max_load(峰值/总量)        → 预计峰值占用率
```

这套值被 HttpServer 的准入逻辑读取（u4-l3 详解），是跨进程「背压」的信号源。

#### 4.3.3 源码精读

**基类的节流与发布**：[base_queue.py:L78-L85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L78-L85) 的 `update_token_load` 用 `need_update_dynamic_max_load()` 做更新节流——不是每拍都重算，避免无谓开销。`calcu_batch_token_load`（[L69-L73](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L69-L73)）在 `current_batch is None` 时直接返回 `(0, 0.0)`，否则转交子类。

**chunked 实现**：[impl.py:L105-L121](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L105-L121) 的 `_calcu_batch_token_load_batch_not_none` 与 4.2 里的峰值公式**完全同构**：同样按 `b_len` 降序排序、同样计算 `(left_out_len * size + cum_run_len).max()`，区别只是输入集合是「当前 batch 的请求」而非「当前 batch + 等待区新请求」。当 `cache_len_list` 为空时返回 0。返回 `(峰值 token 数, 峰值/总量)`。

**Router 的调用点**：[manager.py:L249](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L249) 与 [manager.py:L261](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L261) 分别在「有 running batch」和「无 running batch」两个分支里调用 `update_token_load`，PD decode 模式下还会 `force_update=True` 提高更新频率（注释 [manager.py:L248](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L248)）。

**DP 场景的组合**：[dp_base_queue.py:L65-L75](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py#L65-L75) 的 `DpQueue.update_token_load` 遍历每个内层队列，按 DP 维度分别发布 `current_load` / `estimated_peak_token_count` / `dynamic_max_load`。它复用了内层队列的 `calcu_batch_token_load`，这就是 4.1 抽象带来的复用红利——DpQueue 自己不重写估算算法。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 token load 从「队列估算」到「写进共享内存」的完整路径。

**操作步骤**：

1. 从 [base_queue.py:L78-L85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L78-L85) 的 `update_token_load` 出发，确认它写了 `shared_token_load` 的三个字段：`current_load`、`estimated_peak_token_count`、`dynamic_max_load`。
2. 回到 [impl.py:L105-L121](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L105-L121)，对比它与 [impl.py:L27-L54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54) 的 `_can_add_new_req`：两段代码都出现「排序 + `left_out_len * size + cum_run_len` 的 max」，确认它们共用同一套峰值估算思想，只是输入集合不同。

**需要观察的现象**：同一个峰值公式在「内部准入（`_can_add_new_req`）」和「对外发布（`_calcu_batch_token_load_batch_not_none`）」两处复用。

**预期结果**：你能指出 `calcu_batch_token_load` 不引入等待区新请求，因此它反映的是「现有 batch 的预计峰值」，而 `_can_add_new_req` 反映的是「现有 batch + 候选新请求一并算入的峰值」。前者偏「现状广播」，后者偏「准入决策」。

> 真正读取这些共享值的是 HttpServer 的请求准入逻辑，属于 u4-l3 的内容；本讲只到「写入 shared_token_load」为止。

#### 4.3.5 小练习与答案

**练习 1**：`update_token_load` 为什么要用 `need_update_dynamic_max_load()` 做节流，而不是每拍都算？

**参考答案**：峰值估算含 numpy 排序与累积求和，每拍（默认约 30ms）都跑会有开销；而负载变化通常不需要那么高的刷新频率。节流能在「对外广播足够新鲜」和「CPU 开销可控」之间取平衡。`force_update=True`（如 PD decode 模式）可绕过节流强制刷新（见 [manager.py:L248-L249](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L248-L249)）。

**练习 2**：`DpQueue` 为什么几乎不写自己的估算算法？

**参考答案**：因为估算逻辑与单 DP 组完全一致，只是要按 DP 维度各算一次再分别发布。`DpQueue.update_token_load` 直接复用每个内层队列（`ChunkedPrefillQueue` 等）的 `calcu_batch_token_load`（见 [dp_base_queue.py:L67-L74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/dp_base_queue.py#L67-L74)）。这正是把 `BaseQueue` 抽象出来后获得的复用红利。

## 5. 综合实践

把本讲三块知识串起来，完成一次「纸面调度演练」。

**场景**：单 DP 组（`dp_size_in_node=1`），`max_total_tokens=10000`，`batch_max_tokens`（翻倍后）`=` 大于若干块、`running_max_req_size=4`，`chunked_prefill_size=8192`，`router_token_ratio=1.0`（恒激进，简化判断）。当前 `running_batch` 里有 2 个请求，都已 prefill 完进入 decode，各还需输出 50 与 30 个 token，prefix 长度分别为 2000、1000。等待区里有 2 个新请求，prompt 长度分别为 7000、5000，各还需输出 100、80。

**任务**：

1. 走一遍 [generate_new_batch](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L57-L103) 的 7 步，判断 `new_batch_first_router_need_tokens` 的初值（提示：batch 非空，用 `get_batch_decode_need_tokens`，两个 decode 请求各贡献 1）。
2. 用 4.2.3 的峰值公式，估算把第一个新请求加入后 `need_max_token_num` 大致量级，判断 `ok_token_num`（是否 `< 10000`）会不会拦住它。
3. 写出本轮 `generate_new_batch` 最终返回的 `can_run_list` 可能包含哪些请求，并说明哪道闸门最可能先触发 `break`。

**预期结论**（参考）：初值 `new_batch_first_router_need_tokens=2`（两个 decode 各 1）；由于新请求 prompt 很长（7000）且 `max_total_tokens=10000`，KV 峰值闸门 `ok_token_num` 很可能最先触发，导致 `break`，只放入 0 或 1 个新请求。这个演练帮你体会「三道闸门」如何共同把 OOM 风险挡在调度阶段。

> 上述数字是手工设定的演练场景，非真实运行输出，目的是理解算法行为；如需精确复现请按 4.2.4 的提示加日志本地验证（待本地验证）。

## 6. 本讲小结

- LightLLM 把「挑请求成批」抽象成 `req_queue`，Router 主循环只调 `generate_new_batch` 与 `update_token_load`，调度策略可替换而调用方稳定。
- `build_req_queue` 通过优先级链选出队列类；绝大多数模式（含 `disable_chunked_prefill`）都落到 `ChunkedPrefillQueue`，靠调大 `chunked_prefill_size` 来统一实现、减少重复。
- chunked prefill 让长 prompt 分块 prefill，与 decode 共享每拍的 `batch_max_tokens` 预算，避免长 prompt 阻塞 decode。
- `generate_new_batch` 的预算初值在「batch 为空」时是 0、「batch 不为空」时是老请求的 decode 需求——这正是「prefill 与 decode 共享预算」的体现，也是本讲代码实践的观察点。
- 单个请求能否加入由三道闸门把关：KV 峰值（`ok_token_num`）、请求数（`ok_req_num`）、单拍 prefill 预算（`ok_prefill`），任一不过即 `break`。
- 同一套峰值公式 \(\text{peak}(i)=b_{[i]}\cdot i+\sum_{j\le i}a_{[j]}\) 在「准入（`_can_add_new_req`）」与「对外发布（`calcu_batch_token_load`）」两处复用，前者含等待区新请求、后者只看现有 batch。

## 7. 下一步学习建议

- **横向看 KV 容量**：本讲的 `max_total_tokens` 是 KV Cache 的「总盘子」，它如何被 MemoryManager 分配回收、`mem_fraction` 如何决定它的大小，是 u4-l1（KV Cache 内存管理）的主题。
- **纵向看请求对象**：`get_tuple_tokens` / `get_decode_need_tokens` / `get_first_router_need_tokens` 这些估算原子都来自 `Req`，其字段（`shm_cur_kv_len`、`chunked_prefill_size`、`shm_cur_output_len`）的生命周期在 u2-l3（请求对象与共享内存通信）已建立。
- **看负载的消费者**：本讲只到「写进 `shared_token_load`」为止；这些值如何被 HttpServer 用于请求准入、`logical_max_load` 与 `dynamic_max_load` 的区别，见 u4-l3（Token 负载估算与调度配额）。
- **看 DP 与均衡**：`DpQueue` 把多个内层队列组合起来，其上还有 `dp_balancer` 决定请求分到哪个 DP 组，见 u7-l3（数据并行与负载均衡）。
