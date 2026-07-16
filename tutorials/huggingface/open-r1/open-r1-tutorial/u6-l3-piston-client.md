# Piston 执行客户端与负载均衡

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `PistonClient` 为什么用「令牌桶（token bucket）」同时完成**负载均衡**与**并发限流**这两件事，以及它和普通的轮询/随机负载均衡有何不同。
- 逐行解释 `send_execute` 的重试循环：它重试哪些异常、指数退避（exponential backoff）如何计算、抖动（jitter）为什么是 ±10%、什么时候会**丢弃**一个端点的令牌而不是放回。
- 描述当某个 Piston worker 连接失败时，客户端如何通过 `_check_failed_endpoint` 把它标记为 `unhealthy`，以及「所有端点都不健康」时如何抛出 `PistonError`。
- 解释 `get_piston_client_from_env` 如何按 `LOCAL_RANK`/`WORLD_SIZE` 对端点列表做**跨步分片（strided sharding）**，让多 GPU 训练时各卡不打架、不挤同一个 worker。
- 对比同目录下 `MorphCloudExecutionClient.execute` 的退避策略，理解两种执行后端在「失败兜底」上的取舍。

## 2. 前置知识

本讲是竞赛编程评分单元的第三篇，承接 **u6-l1（IOI 评分系统）**。在继续前，请确认你已了解：

- **Piston 是什么**：一个开源的代码执行引擎（[engineer-man/piston](https://github.com/engineer-man/piston)），对外暴露 REST API，能在隔离沙箱里编译并运行用户提交的代码。open-r1 在它上面装了一个自定义的 `cms_ioi` 包来跑 IOI 题目的 grader。
- **判分调用链**：IOI 的 `run_submission → execute_ioi → client.send_execute(data)`（见 u6-l1）和 Codeforces 的 `score_single_test_case → client.send_execute(...)`（见 u6-l2），最终都会落到本讲要讲的 `PistonClient.send_execute`。也就是说，`PistonClient` 是**所有竞赛编程奖励的执行底座**。
- **为什么需要多个端点**：GRPO 训练时每个 prompt 要采多条回答、每条回答要跑很多测试点，编译+运行 C++ 是 CPU 密集型操作，单个 Piston worker 根本扛不住，必须开一批 worker 组成「机群」，再由客户端把请求分散过去。
- **异步与 `asyncio`**：open-r1 的判分是 `async` 的（`asyncio.gather` 并发跑一批测试点）。本讲的令牌桶、锁、信号量都是 `asyncio` 原语。如果你对 `asyncio.Queue`、`asyncio.Lock`、`asyncio.gather` 不熟，可以先把它们理解成「线程安全的队列/锁/并行任务」。
- **分布式训练的环境变量**：`LOCAL_RANK`（当前 GPU 在本机内的编号）、`WORLD_SIZE`（参与训练的 GPU 总数）由 `accelerate`/`torchrun` 注入。本讲的端点分片就靠这两个变量。

一个核心直觉先建立起来：**令牌桶 =「打着端点名字的通行证」**。后面所有细节都是围绕这张通行证展开的。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/open_r1/utils/competitive_programming/piston_client.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py) | **本讲主角**。定义 `PistonClient`（多端点负载均衡 + 重试 + 健康检查）、`get_piston_client_from_env`（环境变量构造 + 多 GPU 分片）、`get_slurm_piston_endpoints`（从 squeue 自动发现 worker）。 |
| [src/open_r1/utils/competitive_programming/morph_client.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/morph_client.py) | 另一种执行后端 `MorphCloudExecutionClient`，用 MorphCloud 虚拟机跑 IOI 题目。本讲把它作为**对照**，比较它的退避与兜底策略。 |
| [slurm/piston/README.md](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/README.md) | 如何在 Slurm 集群或本地 Docker 上拉起一批 Piston worker，以及 `PISTON_ENDPOINTS` / `PISTON_MAX_REQUESTS_PER_ENDPOINT` 环境变量的含义。 |
| [slurm/piston/launch_piston_workers.sh](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/launch_piston_workers.sh) | 批量提交 worker 的脚本，约定作业名必须是 `piston-worker-<port>`，这是自动发现的关键。 |

调用关系速览（向下越来越底层）：

```
GRPO 奖励 (ioi_code_reward / cf_code_reward)
        │
        ▼
评分逻辑 (ioi_scoring.execute_ioi / cf_scoring.score_single_test_case)
        │  都调用 client.send_execute(data)
        ▼
PistonClient.send_execute   ←── 本讲 4.2 重点（重试/退避/健康检查）
        │  从 endpoint_tokens 取令牌 → POST /execute
        ▼
PistonClient.__init__       ←── 本讲 4.1 重点（令牌桶 = 负载均衡 + 限流）
        ▲
        │  由工厂构造，并做端点分片
get_piston_client_from_env  ←── 本讲 4.3 重点（多 GPU 分片）
```

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**4.1 令牌桶式的负载均衡**、**4.2 send_execute 的重试/退避/健康检查**、**4.3 工厂函数与多 GPU 分片**。

### 4.1 PistonClient 多端点负载均衡与令牌桶

#### 4.1.1 概念说明

假设你有一组 Piston worker（比如 `http://w1:2000/api/v2`、`http://w2:2000/api/v2`、`http://w3:2000/api/v2`），同时有几十个 `asyncio` 协程在并发判分。你需要解决两个问题：

1. **负载均衡**：把请求尽量均匀地分散到各个 worker，别让一个 worker 排长队、其他闲着。
2. **并发限流**：每个 worker 同时只扛得了有限个编译任务（默认 1 个），超过就会报 `Resource temporarily unavailable`（Piston 自身过载）。

最朴素的做法是「随机选一个 worker 发请求」，但这样**无法限流**——可能 10 个协程同时选中 w1，把 w1 打爆。

`PistonClient` 的巧妙之处在于：它用**一个 `asyncio.Queue` 同时解决两件事**。队列里放的不是抽象的「许可」，而是**打着端点 URL 的通行证**。每个端点在队列里有 `max_requests_per_endpoint` 张通行证（默认 1 张）。协程要发请求，必须先从队列里 `get()` 一张通行证——这张通行证既「告诉你发给谁」，又「占用一个并发名额」；请求结束后 `put()` 放回。

这样：
- 因为每个端点的通行证数量 = 它的并发上限，所以**没有协程能让某个端点超载**。
- 因为通行证总量 = 总并发上限，队列的 `get()` 天然在名额耗尽时阻塞，起到了**全局背压**作用。

这就是所谓的**令牌桶（token bucket）**，只不过桶里的令牌是「带名字的」。

> 术语提示：这里「令牌」指队列里的端点字符串，「桶」指 `asyncio.Queue`。它和经典限流算法里的「令牌桶限流（token bucket rate limiting）」思想相通，但本讲里令牌同时承担路由职责。

#### 4.1.2 核心流程

令牌桶的初始化与使用流程：

```
__init__ 阶段：
  for 每个端点 endpoint:
      把 endpoint 的 URL 放进队列 max_requests_per_endpoint 次
  → 队列里现在有  N_endpoints × max_requests_per_endpoint  张通行证

每次请求：
  1. endpoint = await endpoint_tokens.get()   # 取一张通行证（满了就阻塞）
  2. POST {endpoint}/execute                   # 用这张通行证对应的端点发请求
  3. await endpoint_tokens.put(endpoint)       # 用完放回（finally 里保证）
```

关键不变量：**队列里某个端点 URL 的出现次数 = 该端点允许的最大并发请求数**。只要你不放回，它就少一张；放回，它就恢复一张。

#### 4.1.3 源码精读

先看构造函数如何「填桶」：

[src/open_r1/utils/competitive_programming/piston_client.py:L59-L79](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L59-L79) —— 把端点列表规整、建索引、构造令牌桶并预填通行证：

```python
self.max_requests_per_endpoint = max_requests_per_endpoint
self.base_endpoints = [base_endpoint] if isinstance(base_endpoint, str) else base_endpoint
if len(self.base_endpoints) == 0:
    raise ValueError("No Piston endpoints provided. ...")
self.endpoint_ids = {endpoint: i for i, endpoint in enumerate(self.base_endpoints)}

self._session = session
self.endpoint_tokens = asyncio.Queue(maxsize=max_requests_per_endpoint * len(self.base_endpoints))

for _ in range(max_requests_per_endpoint):
    for base_endpoint in self.base_endpoints:
        self.endpoint_tokens.put_nowait(base_endpoint)
```

逐句解读：

- `base_endpoints` 统一成列表；空列表直接报错（说明 `PISTON_ENDPOINTS` 没配好）。
- `endpoint_ids` 给每个端点编个号，**仅用于日志**（重试时打印 `[{编号}]` 方便定位是哪个 worker）。
- `endpoint_tokens = asyncio.Queue(maxsize=...)`：队列容量正好等于「端点数 × 每端点并发数」，这是桶的物理上限。
- 双层 `for` 循环：外层 `max_requests_per_endpoint` 次、内层每个端点一次，`put_nowait` 把每个端点的 URL 放进桶里指定的次数。默认 `max_requests_per_endpoint=1` 时，每个端点 URL 在桶里恰好出现一次。

再看「取令牌」和「还令牌」两个最薄的方法：

[src/open_r1/utils/competitive_programming/piston_client.py:L94-L99](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L94-L99) —— `get()` 阻塞式获取、`put()` 归还：

```python
async def _wait_for_endpoint(self):
    endpoint = await self.endpoint_tokens.get()
    return endpoint

async def _release_endpoint(self, endpoint):
    await self.endpoint_tokens.put(endpoint)
```

注意 `_wait_for_endpoint` 之所以 `await self.endpoint_tokens.get()` 会阻塞，是因为当所有通行证都被借走时，`asyncio.Queue.get()` 会挂起当前协程，直到有人归还。这就是「背压」的来源——桶空了，新请求自动排队等待，而不是无脑涌向 worker。

`session` 属性负责懒加载一个共享的 `aiohttp.ClientSession`，并设置连接池上限与超时：

[src/open_r1/utils/competitive_programming/piston_client.py:L81-L92](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L81-L92) —— 连接池上限与令牌桶容量对齐：

```python
self._session = aiohttp.ClientSession(
    timeout=aiohttp.ClientTimeout(sock_read=30),
    connector=aiohttp.TCPConnector(
        limit=self.max_requests_per_endpoint * len(self.base_endpoints),
        ttl_dns_cache=300,
        keepalive_timeout=5 * 60,
    ),
)
```

`TCPConnector(limit=...)` 把 HTTP 连接池上限设成和令牌桶一样的容量，避免「拿到了通行证却开不出连接」的浪费；`sock_read=30` 给每次读取设了 30 秒超时（这会触发 `asyncio.TimeoutError`，在 4.2 里被重试捕获）。

#### 4.1.4 代码实践

**实践目标**：亲手验证「令牌桶 = 负载均衡 + 并发限流」，理解默认 `max_requests_per_endpoint=1` 时桶里每个端点只有一张通行证。

**操作步骤**（纯本地，不需要真 Piston）：

1. 新建一个 `fake_bucket.py`，复刻令牌桶的最小骨架，把 `aiohttp` 换成假的发请求函数：

```python
# 示例代码：模拟令牌桶，不依赖真实 Piston
import asyncio
from datetime import datetime

class FakeBucket:
    def __init__(self, endpoints, max_per=1):
        self.tokens = asyncio.Queue(maxsize=max_per * len(endpoints))
        for _ in range(max_per):
            for e in endpoints:
                self.tokens.put_nowait(e)

    async def acquire(self):
        return await self.tokens.get()

    async def release(self, e):
        await self.tokens.put(e)

async def job(bucket, i):
    e = await bucket.acquire()              # 取通行证（可能阻塞）
    print(f"{datetime.now().strftime('%H:%M:%S.%f')} job#{i} -> {e}")
    await asyncio.sleep(0.5)                # 假装在跑测试点
    await bucket.release(e)

async def main():
    bucket = FakeBucket(["w1", "w2", "w3"], max_per=1)  # 3 个端点，各 1 张通行证
    await asyncio.gather(*[job(bucket, i) for i in range(9)])

asyncio.run(main())
```

2. 运行 `python fake_bucket.py`。

**需要观察的现象**：

- 每个 `w1/w2/w3` 在同一时刻**最多出现一次**（因为 `max_per=1`，桶里每个端点只有 1 张通行证）。
- 9 个任务被分成约 3 批、每批 3 个并行执行，总共约 `ceil(9/3) × 0.5 ≈ 1.5` 秒完成。
- 把 `FakeBucket(..., max_per=1)` 改成 `max_per=2`，再跑一次。

**预期结果**：`max_per=2` 时每个端点同时可有 2 个任务，9 个任务分约 2 批跑完，总耗时约 `ceil(9/6) × 0.5 ≈ 1.0` 秒。这正好对应 `PISTON_MAX_REQUESTS_PER_ENDPOINT` 调大的效果——并发更高、更快，但单个 worker 压力更大。

> 说明：本实践是「骨架模拟」，真实 `PistonClient` 用 `aiohttp` 发 HTTP、还多出重试与健康检查。这里只验证令牌桶本身的调度行为。

#### 4.1.5 小练习与答案

**练习 1**：桶里有 4 个端点、`max_requests_per_endpoint=2`，桶的总容量是多少？最多能有多少个请求**同时**在执行？

**答案**：总容量 = `4 × 2 = 8`，即队列 `maxsize=8`；同时执行的请求数上限也是 8（被通行证数量卡住），且其中对任意单个端点的并发不超过 2。

**练习 2**：README 里说 `PISTON_MAX_REQUESTS_PER_ENDPOINT` 是「**local limit**」，分布式下没有全局限制，worker 仍可能被打爆。结合本节的令牌桶，解释这句话。

**答案**：令牌桶是**单个 Python 进程内**的并发控制。当多 GPU、多进程训练时，每个进程各自维护自己的桶，彼此不知道对方。若两个进程恰好都把请求指向同一个 worker，对该 worker 而言并发就是两倍。桶只能保证「我这个进程不会超过 N」，却管不了别的进程——这正是 4.3 要用「端点分片」来缓解的痛点。

---

### 4.2 send_execute 重试退避与端点健康检查

#### 4.2.1 概念说明

Piston worker 在大规模训练里是脆弱的：网络抖动会让连接失败、worker 会被打爆返回 `Resource temporarily unavailable`、单次请求会超时。如果判分函数动不动就抛异常，整个 GRPO 训练步就会崩。因此 `send_execute` 必须足够「抗造」：

- **重试（retry）**：对**可恢复**的错误（超时、连接错误、Piston 过载、Piston 返回的非 200）重试若干次。
- **指数退避（exponential backoff）**：每次重试等待时间翻倍，避免在 worker 还没缓过来时又打上去；并设上限防止等太久。
- **抖动（jitter）**：在退避时间上加一点随机扰动，避免多个协程「步调一致」地同时重试、同时打爆（即避免「惊群效应」）。
- **健康检查（health check）**：当**连接根本建立不起来**（worker 可能死了），不只是重试，还要把这个端点**踢出轮转**并标记 `unhealthy`；若所有端点都不健康，则明确抛 `PistonError` 让上层知道「沙箱全挂了」，而不是无限重试。

这里有一个关键区分：**普通可恢复错误**会把通行证**放回桶里**（端点可能只是忙，下次还能用）；而**worker 死亡**会把通行证**丢弃**（端点可能真坏了，别再用它）。这是 4.1 令牌桶的一个微妙用法。

#### 4.2.2 核心流程

`send_execute` 的重试主循环（`max_retries=5`，共 6 次尝试）：

```
for attempt in 0..max_retries:                        # 6 次尝试
    endpoint = await _wait_for_endpoint()             # 取通行证
    if attempt > 0: sleep(1)                          # 非首次尝试前先等 1s
    POST {endpoint}/execute
    if status != 200 或 响应为空 或 Piston 过载:
        raise PistonError(...)                        # 触发下面的重试分支
    else: return res_json                             # 成功

    # —— 捕获可恢复错误 ——
    except (PistonError, TimeoutError, ClientConnectionError, RuntimeError):
        if attempt < max_retries:
            delay = min(1 * 2**attempt, 10)           # 指数退避，封顶 10s
            jitter = delay * 0.2 * (((2t) % 1) - 0.5) # ±10% 抖动
            if 是「连接失败」: _check_failed_endpoint(endpoint)  # 丢弃令牌 + 健康检查
            else:            _release_endpoint(endpoint)        # 放回令牌
            await sleep(delay + jitter)
        else:
            _check_failed_endpoint(endpoint)          # 用尽次数也做一次健康检查
    finally:
        if endpoint is not None: _release_endpoint(endpoint)   # 兜底归还
```

退避时间的数学表达（`base_delay = 1.0`）：

\[
\text{delay}(n) = \min\bigl(2^{n},\ 10\bigr) \quad \text{(秒)},\qquad n = 0,1,2,\dots
\]

各次失败后的等待序列约为 \(1, 2, 4, 8, 10, 10\) 秒（在第 5 次、即 `2^4=16` 处被 10 秒封顶）。实际等待再叠加 ±10% 抖动：

\[
\text{retry\_delay} = \text{delay} + \text{jitter},\quad
\text{jitter} \in [-0.1\cdot\text{delay},\ +0.1\cdot\text{delay})
\]

所以 jitter 最大让等待时间偏移 ±10%。注意源码里 `delay * 0.2 * ((2 * t % 1) - 0.5)`：由于 Python 中 `%` 与 `*` 同优先级、左结合，等价于 `delay * 0.2 * (((2t) % 1) - 0.5)`，因子 `((2t)%1) - 0.5 ∈ [-0.5, 0.5)`，乘以 `0.2*delay` 恰好得到 `±0.1*delay`。

#### 4.2.3 源码精读

先看主请求体——它定义了哪些情况算「需要重试的错误」：

[src/open_r1/utils/competitive_programming/piston_client.py:L137-L166](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L137-L166) —— 取令牌、发请求、识别三类失败：

```python
data = data | {"language": language, "version": "*"}
base_delay = 1.0
status = None
endpoint = None
for attempt in range(max_retries + 1):
    try:
        endpoint = await self._wait_for_endpoint()
        if attempt > 0:
            await asyncio.sleep(1)
        async with self.session.post(
            f"{endpoint.rstrip('/')}/execute", json=data, headers={"Content-Type": "application/json"}
        ) as response:
            status = response.status
            res_json = await response.json(content_type=None)
            if status != 200:
                raise PistonError(f"Server error. status={status}. {res_json}")
            if res_json is None:
                raise PistonError(f"Empty response. status={status}")
            # piston overloaded
            if "run" in res_json and "Resource temporarily unavailable" in res_json["run"].get("stderr", ""):
                raise PistonError(f"Piston overloaded: {res_json['run']['stderr']}")
            return res_json
```

注意三点：

1. `data | {"language": ..., "version": "*"}` 把判分逻辑（u6-l1/u6-l2）传进来的 `files`/`run_timeout` 等字段，补上 `language`（默认 `cms_ioi`）和 `version: "*"`（任意版本），告诉 Piston 用哪个包跑。
2. 三类被主动 `raise` 的失败：**非 200 状态码**、**空响应**、**Piston 过载**（在 `run.stderr` 里检测 `Resource temporarily unavailable`）。这三个 `PistonError` 会被下面的 `except` 捕获并重试。
3. 成功则立即 `return res_json`，跳出循环。

接着看重试与退避分支——这是「放回令牌」还是「丢弃令牌」的分水岭：

[src/open_r1/utils/competitive_programming/piston_client.py:L168-L198](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L168-L198) —— 指数退避 + jitter，以及 worker 死亡时的特殊处理：

```python
except (PistonError, asyncio.TimeoutError, aiohttp.ClientConnectionError, RuntimeError) as e:
    if attempt < max_retries:
        delay = min(base_delay * (2**attempt), 10)                      # 指数退避，封顶 10s
        jitter = delay * 0.2 * (2 * asyncio.get_event_loop().time() % 1 - 0.5)  # ±10% 抖动
        retry_delay = delay + jitter
        print(f"Retrying in {retry_delay:.2f} seconds [{self.endpoint_ids[endpoint]}] {endpoint} - {e}")
        # special case: worker died
        if isinstance(e, aiohttp.ClientConnectionError) and "Connect call failed" in str(e):
            await self._check_failed_endpoint(endpoint)                 # 丢弃令牌 + 健康检查
        else:
            await self._release_endpoint(endpoint)                      # 放回令牌
        endpoint = None
        await asyncio.sleep(retry_delay)
    else:
        await self._check_failed_endpoint(endpoint)
except Exception as e:
    print(f"Propagating exception {type(e)}: {e}")
    raise e
finally:
    if endpoint is not None:
        try:
            await self._release_endpoint(endpoint)
        except Exception as e:
            print(f"Error releasing endpoint {endpoint}: {e}")
        endpoint = None
```

最关键的两条路径：

- **普通可恢复错误**（超时、过载、非 200、`RuntimeError`）：调 `_release_endpoint(endpoint)` 把通行证**放回桶**，这个端点下次还会被选中（它只是忙/抖动）。再 `endpoint = None`，`finally` 不会重复归还。
- **worker 死亡**（`aiohttp.ClientConnectionError` 且消息含 `Connect call failed`，即 TCP 三次握手都没成功）：调 `_check_failed_endpoint(endpoint)`，**不归还令牌**，然后 `endpoint = None`。于是这张通行证永久离开桶——在默认 `max_per=1` 下，等同于把这个端点从轮转里踢了出去。

`finally` 块是兜底保险：只要 `endpoint` 还没被置 `None`（比如「普通错误」分支正常归还后置 None、或「worker 死亡」分支置 None），它都会再确保归还一次，**保证令牌不会泄漏**。

最后看健康检查本身：

[src/open_r1/utils/competitive_programming/piston_client.py:L124-L135](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L124-L135) —— 串行化的「等 5 秒 + 探活」：

```python
async def _check_failed_endpoint(self, endpoint):
    async with self._endpoint_failures_lock:
        if endpoint in self._unhealthy_endpoints:
            return
        try:
            await asyncio.sleep(5)
            await self.get_supported_runtimes()
        except Exception as e:
            print(f"Error checking endpoint {endpoint}, dropping it ({e})")
            self._unhealthy_endpoints.add(endpoint)
            if len(self._unhealthy_endpoints) >= len(self.base_endpoints):
                raise PistonError("All endpoints are unhealthy. Please check your Piston workers.")
```

解读：

- **加锁串行化**：整个「等 5 秒 + 探活」都在 `_endpoint_failures_lock` 内，意味着同一时刻**只有一个健康检查在跑**。这避免了大量协程同时探活造成的二次惊群，也让「已经判不健康的端点」直接 `return`（幂等）。
- **探活手段**：`get_supported_runtimes()` 实际是 `_send_to_all("runtimes", method="get")`（[L121-L122](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L121-L122)），即 **GET 所有端点的 `/runtimes`**。这是一个粗粒度的「我还能不能连上 Piston 机群」探针——它其实 ping 了所有端点，但失败时只把当前 `endpoint` 标记为 unhealthy。够用且便宜。
- **三段判罚**：探活成功 → 什么都不做（但注意令牌已在调用方丢弃，见上）；探活失败 → 把 `endpoint` 加入 `_unhealthy_endpoints` 集合；**若不健康端点数 ≥ 总端点数 → `raise PistonError`**，明确告诉上层「沙箱全挂了，别再重试」，让 GRPO 训练步以可识别的方式失败，而不是无限空转。

> 对照阅读：另一种后端 [morph_client.py:execute](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/morph_client.py#L282-L335)（L282-L335）也用指数退避（`min(base_delay * 2**attempt, 30)`，封顶 30 秒、`max_retries=4`），但**没有 jitter**，且**用尽次数后不抛异常，而是返回 `("0", "失败信息")`**——把失败也当成「0 分」吞掉。Piston 则选择「全挂时抛错」。这是两种后端在「严格 vs 宽容」上的取舍。

#### 4.2.4 代码实践

**实践目标**：亲手验证「worker 死亡 → 令牌丢弃 + 标记 unhealthy」，并观察「所有端点不健康 → 抛 PistonError」。

**操作步骤**：

1. 写一个 `probe.py`，直接构造一个 `PistonClient`，端点指向**根本不存在的地址**（必然 `Connect call failed`），并 monkey-patch 让探活也失败：

```python
# 示例代码：观察 worker 死亡时的健康检查行为
import asyncio
from open_r1.utils.competitive_programming.piston_client import PistonClient, PistonError

async def main():
    # 两个都不存在的端点，连接必然失败
    client = PistonClient(
        ["http://127.0.0.1:9/fake1/api/v2", "http://127.0.0.1:9/fake2/api/v2"],
        max_requests_per_endpoint=1,
    )
    try:
        await client.send_execute({"files": []}, language="cms_ioi", max_retries=2)
    except PistonError as e:
        print("最终抛出 PistonError:", e)
    print("unhealthy 集合 =", client._unhealthy_endpoints)

asyncio.run(main())
```

2. 运行 `python probe.py`（设好 `PYTHONPATH=src`）。

**需要观察的现象**：

- 控制台反复打印 `Retrying in ... seconds [0/1] http://... - ClientConnectionError(... Connect call failed ...)`。
- 因为两个端点都连不上，`_check_failed_endpoint` 探活也失败，它们被陆续加入 `_unhealthy_endpoints`。
- 当 `_unhealthy_endpoints` 数量达到端点总数（2）时，抛出 `PistonError: All endpoints are unhealthy. ...`。

**预期结果**：程序以 `PistonError` 结束，`_unhealthy_endpoints` 包含两个端点。这就是「沙箱全挂」的可识别信号——上层奖励函数应据此决定是否中止训练步。

> 待本地验证：具体重试次数与日志行数取决于 `max_retries` 与连接失败的时序；若本机 9 端口可被立即拒绝，整个过程很快；若被防火墙 DROP（而非 REJECT），`Connect call failed` 可能要等更久。

#### 4.2.5 小练习与答案

**练习 1**：为什么「Piston 过载（`Resource temporarily unavailable`）」走的是 `_release_endpoint`（放回令牌），而「连接失败（`Connect call failed`）」走的是 `_check_failed_endpoint`（丢弃令牌）？

**答案**：过载说明 worker 还活着、只是当前并发太多，缓一缓就能继续服务，所以把令牌放回桶、稍后重试即可；连接失败说明 worker 可能已经死了或网络断了，再发也是白发，于是丢弃令牌（从轮转里移出这个端点）并触发健康检查，避免后续协程继续踩雷。

**练习 2**：`finally` 里为什么还要判断 `if endpoint is not None` 再 `_release_endpoint`？去掉这个判断会怎样？

**答案**：两个重试分支在归还/丢弃后都把 `endpoint = None`，`finally` 据此判断可避免**重复归还**（普通错误分支已归还过一次）。若去掉判断直接归还，会把同一个端点 URL 多放一张进桶，破坏「每端点通行证数 = 并发上限」的不变量，导致并发限流失效。

**练习 3**：把退避封顶从 10 秒改成 1 秒，对大规模训练是好是坏？

**答案**：通常更差。封顶太低意味着在高并发把 worker 打爆后，客户端很快就重试，可能再次打爆，形成「重试风暴」；10 秒封顶给 worker 更多恢复时间。但若沙箱本身很健康、失败只是偶发抖动，过长的退避会拖慢判分。这是吞吐与稳健之间的权衡。

---

### 4.3 get_piston_client_from_env 的端点分片

#### 4.3.1 概念说明

4.1 练习 2 已点出痛点：令牌桶是**进程内**限流，多进程训练时各进程的桶互不感知，可能挤同一个 worker。`get_piston_client_from_env` 用一个简单而有效的办法缓解它——**把端点列表切成互不相交的若干份，每个 GPU 进程只拿其中一份**。

这样，只要分片是「跨 GPU 互斥」的，两个 GPU 进程就**根本不会**同时持有同一个 worker 的令牌，从源头消除了跨进程争用。

工厂函数还顺带做了三件事：

1. **端点来源**：从 `PISTON_ENDPOINTS` 读端点列表；值为 `slurm` 时调 `get_slurm_piston_endpoints()` 自动从 `squeue` 发现所有 `piston-worker-*` 作业。
2. **进程内单例**：用 `@lru_cache(maxsize=1)` 保证一个进程只造一个客户端（共享令牌桶与连接池）。
3. **分片 + 随机洗牌**：先 `sorted` 保证跨进程顺序一致，再按 `LOCAL_RANK::WORLD_SIZE` 跨步切片，最后对本 GPU 的子集 `shuffle` 打乱起始顺序。

#### 4.3.2 核心流程

```
get_piston_client_from_env():
  endpoints = 读取 PISTON_ENDPOINTS
              ├── 值为 "slurm" → get_slurm_piston_endpoints()  # 解析 squeue
              └── 否则         → split(",")                     # 逗号分隔
  endpoints = sorted(endpoints)                # 关键：让所有进程顺序一致
  gpu  = LOCAL_RANK (默认 0)
  world = WORLD_SIZE (默认 1)
  if world > 1:
      endpoints = endpoints[gpu::world]        # 跨步分片：GPU i 取 i, i+world, i+2world, ...
  random.shuffle(endpoints)                    # 仅对本 GPU 子集洗牌
  return PistonClient(endpoints, session, max_requests_per_endpoint)
```

跨步切片 `endpoints[gpu::world]` 的含义（假设 `sorted` 后有 6 个端点 `e0..e5`，`world=3`）：

| GPU（LOCAL_RANK） | 分到的端点 | 切片表达式 |
| --- | --- | --- |
| 0 | e0, e3 | `endpoints[0::3]` |
| 1 | e1, e4 | `endpoints[1::3]` |
| 2 | e2, e5 | `endpoints[2::3]` |

三份**互不相交**且**并集为全集**——这就是「分片避免冲突」的本质。

#### 4.3.3 源码精读

[src/open_r1/utils/competitive_programming/piston_client.py:L16-L33](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L16-L33) —— 读环境变量、排序、分片、洗牌、造客户端：

```python
@lru_cache(maxsize=1)
def get_piston_client_from_env(session=None):
    piston_endpoints = os.getenv("PISTON_ENDPOINTS")
    if piston_endpoints is None:
        raise ValueError("For IOI/CF problems Piston endpoints ... are required. ...")
    piston_endpoints = sorted(
        piston_endpoints.split(",") if piston_endpoints != "slurm" else get_slurm_piston_endpoints()
    )
    gpu_nb = int(os.getenv("LOCAL_RANK", 0))   # per‑GPU index
    world = int(os.getenv("WORLD_SIZE", 1))    # total GPUs
    if world > 1:
        print(f"Using a subset of piston endpoints for GPU#{gpu_nb}")
        piston_endpoints = piston_endpoints[gpu_nb::world]
    random.shuffle(piston_endpoints)
    max_requests_per_endpoint = os.getenv("PISTON_MAX_REQUESTS_PER_ENDPOINT", "1")
    return PistonClient(piston_endpoints, session, max_requests_per_endpoint=int(max_requests_per_endpoint))
```

逐句要点：

- `@lru_cache(maxsize=1)`：函数级缓存，同一进程**重复调用返回同一个 `PistonClient`**。这很重要——奖励函数里 `ioi_code_reward`/`cf_code_reward` 每次判分都可能调它，单例避免反复重建令牌桶和连接池。注意 `lru_cache` 对 `session` 参数也敏感，默认 `None` 时只缓存一份。
- `sorted(...)`：**分片正确性的前提**。`endpoints[gpu_nb::world]` 依赖一个「全 GPU 一致」的端点顺序；`sorted` 用 URL 字典序把这个顺序固定下来。若不排序，各进程拿到的端点顺序可能不同，跨步切片就会错位、重叠。
- `piston_endpoints[gpu_nb::world]`：跨步切片，只在 `world > 1`（多 GPU）时启用。
- `random.shuffle(...)`：**分片之后**才洗牌，所以只洗本 GPU 的子集。作用是让「桶里通行证的初始顺序」随机化，避免多个进程（同 GPU 上的数据并行 worker）都以同样的顺序优先抢同一个端点。
- `PISTON_MAX_REQUESTS_PER_ENDPOINT`：透传给 `PistonClient`，控制每个端点的并发上限（4.1 的 `max_per`），默认 1。

再看 Slurm 自动发现：

[src/open_r1/utils/competitive_programming/piston_client.py:L201-L224](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/piston_client.py#L201-L224) —— 从 `squeue` 解析作业名 → 端点 URL：

```python
def get_slurm_piston_endpoints():
    """Get list of active piston worker endpoints from squeue output"""
    result = subprocess.run(
        ["squeue", '--format="%j %N %T"', "--noheader", "--states=RUNNING"], capture_output=True, text=True
    )
    lines = result.stdout.strip().split("\n")
    endpoints = []
    for line in lines:
        fields = line.split()
        job_name = fields[0].strip('"')
        hostname = fields[1]
        match = re.match(r"piston-worker-(\d+)", job_name)
        if match:
            port = match.group(1)
            endpoints.append(f"http://{hostname}:{port}/api/v2")
    return endpoints
```

它和 [launch_piston_workers.sh](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/piston/launch_piston_workers.sh) 是一对约定：启动脚本把每个 worker 的作业名设成 `piston-worker-$PORT`（端口就是 worker 监听端口），这里就用正则 `piston-worker-(\d+)` 把端口抠出来，拼成 `http://{hostname}:{port}/api/v2`。只取 `RUNNING` 状态的作业，避免捞到排队中的。

这就形成了一条闭环：**启动脚本命名作业 → squeue 列出作业 → 正则解析端口 → 拼端点 URL → sorted → 分片 → 令牌桶**。

#### 4.3.4 代码实践

**实践目标**：验证「跨步分片」在不同 `LOCAL_RANK`/`WORLD_SIZE` 下产出互不相交的端点子集。

**操作步骤**（纯本地，无需集群）：

1. 写 `shard_demo.py`，直接调用切片逻辑（不依赖真实 squeue）：

```python
# 示例代码：复刻分片逻辑，观察各 GPU 拿到哪些端点
import os, random

def shard(endpoints, world, gpu_nb):
    endpoints = sorted(endpoints)          # 与源码一致：先排序
    if world > 1:
        endpoints = endpoints[gpu_nb::world]
    # 注意：random.shuffle 不影响「分到哪些」，只影响桶内初始顺序，这里省略
    return endpoints

all_eps = [f"http://w{i}:2000/api/v2" for i in range(8)]  # 8 个 worker

for world in (1, 4):
    print(f"--- WORLD_SIZE={world} ---")
    subsets = []
    for gpu in range(world):
        s = shard(all_eps, world, gpu)
        subsets.append(set(s))
        print(f"  GPU#{gpu} -> {[e.split('//')[1].split(':')[0] for e in s]}")
    # 校验互斥与覆盖
    union = set().union(*subsets)
    pairwise_disjoint = all(subsets[i].isdisjoint(subsets[j]) for i in range(world) for j in range(i+1, world))
    print(f"  互斥={pairwise_disjoint}, 覆盖全集={union == set(all_eps)}")
```

2. 运行 `python shard_demo.py`。

**需要观察的现象**：

- `WORLD_SIZE=1` 时，GPU#0 拿到全部 8 个端点。
- `WORLD_SIZE=4` 时，GPU#0 拿 `{w0,w4}`、GPU#1 拿 `{w1,w5}`、GPU#2 拿 `{w2,w6}`、GPU#3 拿 `{w3,w7}`。

**预期结果**：每个 `world>1` 的场景里，`互斥=True` 且 `覆盖全集=True`。这证明分片既不重叠（避免冲突）又不遗漏（不浪费 worker）。

**回答本讲的核心问题**（来自任务要求）：

- **worker 连接失败如何被检测并标记 unhealthy**：`send_execute` 捕获 `aiohttp.ClientConnectionError`（且消息含 `Connect call failed`）→ 调 `_check_failed_endpoint` → 在锁内 `sleep(5)` 后用 `get_supported_runtimes()` 探活 → 探活失败则 `self._unhealthy_endpoints.add(endpoint)`，同时该端点的令牌已在主循环里丢弃，从而移出轮转；若全部端点都不健康，抛 `PistonError`。
- **多 GPU 训练时端点如何分片避免冲突**：`get_piston_client_from_env` 先 `sorted` 固定全局顺序，再 `piston_endpoints[LOCAL_RANK::WORLD_SIZE]` 跨步取互斥子集，使每个 GPU 进程只持有一组互不重叠的 worker，从源头杜绝跨进程挤同一 worker。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `sorted(...)`，直接 `piston_endpoints[gpu_nb::world]`，多 GPU 分片会出什么问题？

**答案**：`get_slurm_piston_endpoints` 返回的顺序依赖 `squeue` 输出（即作业提交/调度顺序），各进程看到的顺序可能不同；即便顺序相同，逗号分隔的 `PISTON_ENDPOINTS` 在不同进程里虽然一致，但跨步切片本身需要一个「稳定的全局序」。少了 `sorted`，不同 GPU 的跨步起点对齐的就不是同一个序列，分出的子集可能重叠（两卡抢同一 worker）或遗漏（某 worker 无人用）。`sorted` 用字典序把顺序锁死，保证跨步切片全局一致。

**练习 2**：`@lru_cache(maxsize=1)` 在这里有副作用吗？如果训练中途新增了一批 Piston worker（重新跑 `launch_piston_workers.sh`），客户端会发现它们吗？

**答案**：会有副作用——`lru_cache` 让 `get_piston_client_from_env` 在整个进程生命周期内只执行一次，端点列表和令牌桶在首次构造后就固定了。训练中途新加的 worker 不会被已存在的客户端发现，除非重启训练进程。这是「单例」换来的便利与「动态扩容」能力的权衡。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来，做一个「带健康检查的多端点模拟客户端」，观察令牌桶调度、退避重试、端点驱逐的全过程。

**步骤**：

1. 准备 3 个「假 worker」——用 `asyncio.sleep` 模拟，其中一个（`w1`）故意在第 2 次被调用时抛 `Connect call failed` 模拟宕机：

```python
# 示例代码：综合模拟（不含真实 HTTP）
import asyncio, random
from collections import Counter

class SimClient:
    def __init__(self, endpoints, max_per=1):
        self.endpoints = endpoints
        self.calls = Counter()
        self.unhealthy = set()
        self.tokens = asyncio.Queue(maxsize=max_per * len(endpoints))
        for _ in range(max_per):
            for e in endpoints:
                self.tokens.put_nowait(e)

    async def call_endpoint(self, e):
        self.calls[e] += 1
        if e == "w1" and self.calls[e] == 2:
            raise ConnectionError("Connect call failed (simulated worker death)")
        await asyncio.sleep(0.3)        # 假装编译运行
        return {"run": {"stderr": ""}}  # 成功

    async def send_execute(self):
        base = 1.0
        for attempt in range(5 + 1):
            e = await self.tokens.get()
            try:
                await asyncio.sleep(1 if attempt > 0 else 0)
                return await self.call_endpoint(e)
            except ConnectionError as ex:
                if "Connect call failed" in str(ex):
                    self.unhealthy.add(e)            # 丢弃令牌：不 put 回去
                    await asyncio.sleep(min(base * 2**attempt, 10))
                else:
                    await self.tokens.put(e)
            finally:
                pass   # 真实代码这里会判断 endpoint is not None 再归还

async def main():
    c = SimClient(["w1", "w2", "w3"])
    await asyncio.gather(*[c.send_execute() for _ in range(6)])
    print("每个端点被调用次数:", dict(c.calls))
    print("被标记 unhealthy:", c.unhealthy)

asyncio.run(main())
```

2. 多次运行，关注：
   - `w1` 在第 2 次调用后是否进入 `unhealthy`，且后续不再被调用（令牌已丢弃）。
   - 总请求数 6 是如何被 `w1/w2/w3` 分摊的，验证令牌桶的负载均衡。

3. **进阶**：在 `SimClient` 里加上 `sorted` + 跨步分片，模拟 `WORLD_SIZE=2` 时两个「逻辑 GPU」各持一份端点，确认 `w1` 只可能属于其中一个分片。

**预期结果**：你会清楚看到「令牌桶限流 → 失败重试 → worker 死亡驱逐令牌 → 健康标记」四个机制如何在一次 `send_execute` 里协同。这正是真实 `PistonClient` 在大规模 GRPO 判分时的行为缩影。

## 6. 本讲小结

- `PistonClient` 用**一个 `asyncio.Queue` 当令牌桶**，桶里放的是「打着端点 URL 的通行证」，同时完成**负载均衡**（通行证决定发给谁）和**并发限流**（每端点通行证数 = 其并发上限）。
- `send_execute` 对超时、连接错误、Piston 过载、非 200 等**可恢复错误**做指数退避重试：\( \text{delay}=\min(2^n,10) \) 秒，并叠加 ±10% 抖动防惊群。
- **普通可恢复错误**把通行证**放回**桶里；**worker 死亡**（`Connect call failed`）则**丢弃**通行证并触发 `_check_failed_endpoint`，在锁内等 5 秒后探活，失败即把端点加入 `_unhealthy_endpoints`；全部不健康时抛 `PistonError`。
- `get_piston_client_from_env` 用 `@lru_cache` 做进程单例，先 `sorted` 固定全局顺序，再按 `LOCAL_RANK::WORLD_SIZE` **跨步分片**，让多 GPU 各持互不相交的端点子集，从源头避免跨进程挤同一 worker。
- Slurm 部署靠命名约定闭环：`launch_piston_workers.sh` 把作业命名为 `piston-worker-<port>`，`get_slurm_piston_endpoints` 用 `squeue` + 正则把端口解析回端点 URL。
- 对照 `MorphCloudExecutionClient.execute`：同样指数退避（封顶 30 秒、无 jitter），但失败到顶时**返回 `("0", msg)` 而非抛错**——Piston 更「严格」，Morph 更「宽容」。

## 7. 下一步学习建议

- **回顾整条判分链**：现在你已经看完了 `PistonClient`，建议重读 [ioi_scoring.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/ioi_scoring.py) 的 `run_submission`/`execute_ioi` 与 [cf_scoring.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/competitive_programming/cf_scoring.py) 的 `score_single_test_case`，确认 `send_execute` 返回的 `response` 是如何被解析成 `(score, feedback)` 的。
- **了解另一种沙箱**：回到 u5 系列的 [code_providers.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/code_providers.py) 与 [routed_sandbox.py](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/src/open_r1/utils/routed_sandbox.py)，对比 E2B/Morph 的「Provider + Router」抽象与本讲的「Piston 令牌桶」是两种不同的并发治理思路。
- **规模化部署**：结合 u7 单元，阅读 [slurm/train.slurm](https://github.com/huggingface/open-r1/blob/1416fa0cf21595d2083b399a2a0bbddd7f6e9563/slurm/train.slurm)，理解训练节点如何与 Piston worker 机群共存于一个 Slurm 集群，以及 `PISTON_ENDPOINTS=slurm` 如何把两者串起来。
- **动手调参**：若你有集群，尝试改 `PISTON_MAX_REQUESTS_PER_ENDPOINT` 和 worker 数量，观察 `send_execute` 的重试日志频率与训练吞吐的变化，体会「限流-退避-分片」三者的权衡。
