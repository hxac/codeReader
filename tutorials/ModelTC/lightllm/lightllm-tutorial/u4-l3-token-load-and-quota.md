# Token 负载估算与调度配额

## 1. 本讲目标

本讲聚焦 Router 中的「负载账本」。学完本讲，你应当能够：

- 说清 `TokenLoad` 在共享内存里到底存了哪几个负载指标、它们各自的含义与量纲（是占用**比例**还是 token **绝对数**）。
- 解释 `RouterStatics` 如何用 EMA（指数移动平均）在线估计请求的输出长度，以及它为什么会影响调度是「激进」还是「保守」。
- 读懂底层 `SharedArray`/`SharedInt` 是如何把一个 numpy 数组直接「铺」在共享内存上、从而让两个进程零拷贝读写同一份负载值的。
- 厘清一个容易被误解的因果关系：到底是「共享负载值决定能否加入新请求」，还是「加入新请求的决策被发布为共享负载值」。

本讲是 [u2-l6](u2-l6-reqqueue-chunked-prefill.md)（chunked prefill 调度）与 [u4-l2](u4-l2-radix-prefix-cache.md)（RadixCache）的延续：前两讲分别讲了「挑请求成批」的算法和「复用 KV」的基数树，本讲则回答一个贯穿两者的前提问题——**Router 凭什么数值来判断「还能不能再塞一个请求」？这些数值又是怎么跨进程告诉别人的？**

## 2. 前置知识

阅读本讲前，建议你已经建立以下认知（来自前置讲义）：

- **token 级 KV 管理**（[u4-l1](u4-l1-kv-cache-memory-manager.md)）：每张 GPU 上有一个 `MemoryManager`，持有固定容量的 KV buffer，容量上限是 `max_total_token_num`。调度必须保证所有在跑请求的 KV 总和不超过这个上限。
- **共享内存 IPC**（[u2-l3](u2-l3-req-and-shm-ipc.md)）：LightLLM 多进程之间用 zmq/rpyc 传「轻量通知」，用共享内存承载「大块状态」。本讲要讲的负载账本，正是放在共享内存里、被多进程直接读取的那一类状态。
- **chunked prefill 调度**（[u2-l6](u2-l6-reqqueue-chunked-prefill.md)）：每拍调度会评估「把等待区的新请求塞进 batch 后，KV 峰值会不会爆显存」。你已经知道有三道闸门（`ok_token_num`/`ok_req_num`/`ok_prefill`），本讲把这三道闸门背后用到的「峰值估算公式」和「统计量」拆开讲透。
- **激进/保守调度**（[u2-l5](u2-l5-router-scheduling-loop.md)）：`router_token_ratio` 触发的 `is_busy` 状态，会让 Router 在「估长」时偏向保守（按请求声明的 `max_new_tokens` 估）或激进（按经验值估）。本讲的 `RouterStatics.ema_req_out_len` 就是这个「经验值」的来源。

两个本讲会用到的术语先约定清楚：

- **负载（load）**：在本讲语境下，特指「KV Cache 显存占用」的某种度量，不是 CPU/GPU 利用率。
- **峰值（peak）**：连续批处理中，一批请求在各自生命周期内对 KV 显存的**最大瞬时需求**。调度的核心难点就是预估这个峰值，而不是只看「此刻」占了多少。

## 3. 本讲源码地图

本讲涉及的核心源码文件只有三个，外加几个把它们「串起来」的调用点：

| 文件 | 作用 | 角色 |
| --- | --- | --- |
| `lightllm/server/router/dynamic_prompt/shared_arr.py` | 把 numpy 数组铺到共享内存上的基础工具 | **地基**：所有跨进程数值的载体 |
| `lightllm/server/router/token_load.py` | 跨进程负载账本 `TokenLoad` | **账本**：定义存什么、怎么读写 |
| `lightllm/server/router/stats.py` | 输出长度的 EMA 估计 `RouterStatics` | **统计**：为峰值估算提供「请求还能跑多长」的经验值 |

调用点（用于把账本接进主循环、暴露给外部）：

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `lightllm/server/router/req_queue/base_queue.py` | `is_busy` / `update_token_load` | 每拍计算并发布负载 |
| `lightllm/server/router/req_queue/chunked_prefill/impl.py` | `_can_add_new_req` | 峰值估算公式 + 三道闸门 |
| `lightllm/server/router/manager.py` | `loop_for_fwd` / `get_used_tokens` | 驱动发布、上报指标 |
| `lightllm/server/api_http.py` | `/token_load` 端点 | 把负载以 JSON 暴露给运维 |
| `lightllm/server/httpserver/pd_loop.py` | 节点负载上报 | PD 分离模式下供 master 路由 |

## 4. 核心概念与源码讲解

按「自底向上」的顺序拆成三个最小模块：先讲共享数组（地基），再讲 `TokenLoad`（账本），再讲 `RouterStatics`（统计），最后用一个综合小节把它们接回调度主循环。

### 4.1 共享数组 SharedArray / SharedInt

#### 4.1.1 概念说明

回顾 [u2-l3](u2-l3-req-and-shm-ipc.md)：LightLLM 的多进程之间，大块数据走共享内存。但共享内存（`multiprocessing.shared_memory.SharedMemory`）给的只是一段**裸字节**（一个 `bytes`-like buffer）。两个进程各自 `mmap` 同一段命名内存后，怎么把它当成「一个有结构的小数组」来读写？这就是 `SharedArray` 要解决的问题。

关键设计目标：**读写的两端都不想做拷贝、不想做序列化**。负载值每 30ms（一个调度拍）就要更新一次、被另一个进程读一次，如果每次都 `pickle.dumps` 再 `send`，开销不可接受。`SharedArray` 的做法是把一个 numpy 数组**直接建在共享内存的 buffer 上**，于是「写」就是往数组元素赋值，「读」就是取数组元素，全程零拷贝。

#### 4.1.2 核心流程

`SharedArray` 的构造只有两步：

1. 按数据类型和形状算出需要的字节数，用 `create_or_link_shm(name, size)` 申请/连接一段命名共享内存。
2. 用 `np.ndarray(shape, dtype, buffer=self.shm.buf)` 把这段字节**解释**成一个 numpy 数组视图。

之后所有读写都通过 `self.arr` 这个视图完成。

#### 4.1.3 源码精读

[lightllm/server/router/dynamic_prompt/shared_arr.py:12-17](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/shared_arr.py#L12-L17) 是 `SharedArray` 的全部实现，极其精简：

```python
class SharedArray:
    def __init__(self, name, shape, dtype):
        dtype_byte_num = np.array([1], dtype=dtype).dtype.itemsize
        dest_size = np.prod(shape) * dtype_byte_num
        self.shm = create_or_link_shm(name, dest_size)
        self.arr = np.ndarray(shape, dtype=dtype, buffer=self.shm.buf)
```

几个要点：

- `name` 是这段共享内存的**全局唯一名字**。同一台机器上，任何进程只要用同样的 `name` 调 `create_or_link_shm`，就会拿到同一段字节——这正是「跨进程共享」的纽带。命名空间隔离由 `set_unique_server_name()`（见 [u1-l5](u1-l5-process-orchestration.md)）保证，多个 lightllm 实例同机运行不会串段。
- `np.ndarray(..., buffer=self.shm.buf)` 是「零拷贝」的关键：numpy 数组**不分配自己的内存**，而是把共享内存的 buffer 当作自己的存储后端。于是「写 `arr[i] = x`」直接落到共享内存的字节里。
- `create_or_link_shm`（在 `lightllm/utils/shm_utils.py`）负责「没有就创建、有就连接」的语义，使得 Router（写方）和 HttpServer（读方）无论谁先启动都能对上。

`SharedInt` 是 `SharedArray` 的特化，专门表达「一个跨进程整数」：

[lightllm/server/router/dynamic_prompt/shared_arr.py:20-28](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/dynamic_prompt/shared_arr.py#L20-L28) 把形状固定为 `(1,)`、类型固定为 `int64`，再包一对 `set_value`/`get_value`。KV 内存管理器里那个 `can_use_mem_size`（[u4-l1](u4-l1-kv-cache-memory-manager.md)）就是用 `SharedInt` 跨进程发布的。

> ⚠️ 一个重要的工程细节：`SharedArray` **没有加锁**。这之所以可行，是因为这些负载值都是** advisory（参考性）的估算值**——读方偶尔读到「写了一半」的旧值，最多导致一次略微保守或略微激进的调度决策，不会破坏数据结构。这种「最终一致即可」的取舍换来的是极低延迟。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `SharedArray` 在两个独立 Python 进程间确实共享同一份字节。

**操作步骤**（任意目录，确保已 `pip install numpy`，无需启动 lightllm）：

1. 在进程 A 写入：

   ```python
   # proc_a.py —— 示例代码，非项目源码
   import numpy as np
   from multiprocessing import shared_memory

   shm = shared_memory.SharedMemory(create=True, size=24, name="demo_load")
   arr = np.ndarray((3,), dtype=np.float64, buffer=shm.buf)
   arr[0], arr[1], arr[2] = 0.1, 0.2, 0.3
   input("进程 A 写完，回车退出（先别退出，去跑进程 B）")
   ```

2. 在进程 B（另一个终端）读取同一段：

   ```python
   # proc_b.py —— 示例代码，非项目源码
   import numpy as np
   from multiprocessing import shared_memory

   shm = shared_memory.SharedMemory(name="demo_load")   # 注意：不 create
   arr = np.ndarray((3,), dtype=np.float64, buffer=shm.buf)
   print("读到：", arr[:])   # 期望 [0.1 0.2 0.3]
   ```

**需要观察的现象**：进程 B 不做任何 `send`/`recv`，却能直接读到进程 A 写入的三个浮点数；进程 A 再改 `arr[1] = 0.9`，进程 B 立刻能读到新值。

**预期结果**：两个进程的 `arr` 指向同一段物理内存，验证了 `SharedArray` 的零拷贝共享语义。这正是 `TokenLoad` 能在 Router 与 HttpServer 之间「实时同步」的物理基础。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SharedArray` 不需要像 `Req` 那样（见 [u2-l3](u2-l3-req-and-shm-ipc.md)）维护引用计数和空闲链表？

**参考答案**：因为每个 `SharedArray` 的形状、用途在创建时就固定了（比如 `TokenLoad` 的两个数组在 Router 启动时一次性建好、生命周期与进程同寿），不存在「按需分配/回收槽位」的需求；而 `Req` 要随请求的到来与结束动态申请/归还槽位，才需要引用计数与链表。

**练习 2**：如果把 `dtype` 从 `np.float64`（8 字节）改成 `np.float32`（4 字节），`SharedArray` 申请的共享内存字节数会怎么变？

**参考答案**：`dest_size = np.prod(shape) * dtype.itemsize`，元素个数不变，`itemsize` 减半，所以字节数减半。

### 4.2 TokenLoad：跨进程的负载账本

#### 4.2.1 概念说明

有了 `SharedArray` 这个地基，`TokenLoad` 就是在其之上定义「账本格式」的薄封装。它回答两个问题：

1. **存哪些负载指标？** 调度方（Router）和读方（HttpServer、PD master）需要看到哪些数值？
2. **这些指标量纲分别是什么？** 这是最容易踩坑的地方——有的指标是「占用比例」（0~1 的浮点），有的是「token 绝对数」（整数）。

`TokenLoad` 维护**两个** `SharedArray`：一个存「比例类」负载，一个存「绝对数类」负载。它们的第二维分别对应不同的指标列；第一维是 `dp_size_in_node`，为数据并行（DP）预留——每个 DP 组有自己的负载列（DP 的概念见 [u7-l3](u7-l3-dp-and-load-balance.md)，本讲只需知道「按 DP 组分别记账」即可）。

#### 4.2.2 核心流程

`TokenLoad` 在共享内存里铺设的两张表：

| SharedArray | 形状 | dtype | 各列含义 |
| --- | --- | --- | --- |
| `shared_token_load` | `(dp, 3)` | float64 | `[0]` current_load、`[1]` logical_max_load、`[2]` dynamic_max_load |
| `shared_token_infos` | `(dp, 1)` | int64 | `[0]` estimated_peak_token_count |

四个核心指标的对照（**请特别留意量纲差异**）：

| 指标 | 量纲 | 含义 |
| --- | --- | --- |
| `current_load` | 占用**比例**（0~1） | 此刻实际 KV 占用 / `max_total_token_num` |
| `estimated_peak_token_count` | token **绝对数** | 估算的一批请求在其生命周期内的峰值 KV 需求 |
| `dynamic_max_load` | 占用**比例**（0~1） | `estimated_peak_token_count / max_total_token_num`，考虑请求中途结束 |
| `logical_max_load` | 占用**比例**（0~1） | **历史遗留字段**：原本是「输入+输出长度朴素相加」的估计；现已废弃，值与 `dynamic_max_load` 相同 |

`TokenLoad` 同时记录 `last_dynamic_max_load_update_time`，用于节流「重算并发布动态负载」的频率，避免每拍都做一次 numpy 峰值估算。

#### 4.2.3 源码精读

[lightllm/server/router/token_load.py:6-27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L6-L27) 是构造函数，把两张表铺到共享内存上：

```python
class TokenLoad:
    def __init__(self, name, dp_size_in_node) -> None:
        self.dp_size_in_node = dp_size_in_node
        self.shared_token_load = SharedArray(
            name, shape=(self.dp_size_in_node, 3,), dtype=np.float64)
        self.shared_token_infos = SharedArray(
            f"{name}_ext_infos", shape=(self.dp_size_in_node, 1,), dtype=np.int64)
        self.last_dynamic_max_load_update_time = time.time()
```

注意第二个数组的名字是 `f"{name}_ext_infos"`——它复用主名字加后缀，保证两张表在命名空间里既关联又独立。

四个指标的读写都是「直接索引赋值/取值」，没有任何计算逻辑——计算在调用方（队列）做，`TokenLoad` 只负责存。比如：

- [lightllm/server/router/token_load.py:30-33](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L30-L33)：`set_estimated_peak_token_count` 写的是 `shared_token_infos.arr[index, 0]`（**绝对数**列，int64）。
- [lightllm/server/router/token_load.py:44-49](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L44-L49)：`current_load` 读写 `shared_token_load.arr[index, 0]`（**比例**列，float64）。

最值得精读的是 `dynamic_max_load` 与 `logical_max_load` 的关系：

[lightllm/server/router/token_load.py:51-64](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L51-L64)：

```python
# logical_max_load 朴素估计的负载...目前已未使用，其值与dynamic_max_load一样
def set_logical_max_load(self, value, index: int):
    self.shared_token_load.arr[index, 1] = value

# dynamic_max_load 动态估计的最大负载，考虑请求中途退出的情况
def set_dynamic_max_load(self, value, index: int):
    self.shared_token_load.arr[index, 2] = value
    self.set_logical_max_load(value, index=index)   # ← 关键：顺手把 logical 也写成同一个值
    self.last_dynamic_max_load_update_time = time.time()
```

这是回答本讲实践任务的**关键代码点**：调用 `set_dynamic_max_load` 时，会**同时**把 `logical_max_load` 写成同一个值。所以两者在运行时**永远相等**——`logical_max_load` 退化成了一个「兼容字段」，仅仅为了 `/token_load` 接口（见 4.4.3）的字段不删而保留。源码注释也明确写了「目前已未使用，其值与 dynamic_max_load 一样」。

节流逻辑：

[lightllm/server/router/token_load.py:69-74](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L69-L74)：

```python
def need_update_dynamic_max_load(self, index: int = 0):
    # 3s 需要进行一次更新
    if time.time() - self.last_dynamic_max_load_update_time >= 6.0:
        return True
    else:
        return False
```

> ⚠️ 注意源码注释与实现的不一致：注释写「3s」，但实际阈值是 `6.0` 秒。本讲以代码实际行为为准——动态负载每 **6 秒**才允许重算一次（除非调用方传 `force_update=True` 强制重算）。这是「读方读到稍微旧的估算」的另一个来源，与 `SharedArray` 无锁设计一脉相承：负载估算无需实时精确。

#### 4.2.4 代码实践

**实践目标**：搞清 `current_load` / `estimated_peak_token_count` / `dynamic_max_load` 三者的量纲与换算关系。

**操作步骤**（源码阅读型实践）：

1. 打开 [lightllm/server/router/req_queue/base_queue.py:78-85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L78-L85) 的 `update_token_load`：
   - `set_current_load(token_ratio1, ...)` —— 传入的是 `get_used_tokens / max_total_token_num`，即**比例**。
   - `set_estimated_peak_token_count(estimated_peak_token_count, ...)` —— 传入的是 `calcu_batch_token_load` 返回的**绝对数**。
   - `set_dynamic_max_load(dynamic_max_load, ...)` —— 传入的是 `calcu_batch_token_load` 返回的**比例**。
2. 再看 [lightllm/server/router/manager.py:253-259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L253-L259) 上报指标 `lightllm_batch_current_max_tokens` 的代码：它把 `dynamic_max_load`（比例）**乘回** `max_total_token_num` 还原成绝对数。

**需要观察的现象**：`dynamic_max_load` 与 `estimated_peak_token_count` 表达的是**同一个峰值**，只是一个归一化成比例、一个是绝对数；而 `current_load` 是「当下实际占用」，不涉及未来估算。

**预期结果**：你能用一句话说清三者的换算关系：

\[ \texttt{dynamic\_max\_load} = \frac{\texttt{estimated\_peak\_token\_count}}{\texttt{max\_total\_token\_num}}, \qquad \texttt{current\_load} = \frac{\text{当前实际 KV 占用}}{\texttt{max\_total\_token\_num}} \]

#### 4.2.5 小练习与答案

**练习 1**：为什么 `estimated_peak_token_count` 用 `int64` 存，而 `current_load`/`dynamic_max_load` 用 `float64` 存？

**参考答案**：`estimated_peak_token_count` 是 token 的个数（整数计数，可能很大），用 `int64` 精确表达；后两者是 0~1 之间的归一化比例，需要小数，用 `float64`。

**练习 2**：如果调用 `set_logical_max_load(0.5, index=0)` 后，紧接着调用 `set_dynamic_max_load(0.8, index=0)`，最终 `get_logical_max_load(0)` 返回多少？

**参考答案**：返回 `0.8`。因为 `set_dynamic_max_load` 内部会调用 `set_logical_max_load` 把 logical 也覆盖成 0.8。这印证了「两者运行时恒等」。

### 4.3 RouterStatics：用 EMA 估算输出长度

#### 4.3.1 概念说明

要估「一批请求的峰值 KV 需求」，必须先知道「每个请求还要生成多少 token」。问题在于：请求刚进来时，只声明了上限 `max_new_tokens`（往往很大，比如 2048），实际很可能几十 token 就 eos 结束了。如果一律按 `max_new_tokens` 估，系统会极度保守、吞吐崩塌；如果一律按某个固定小值估，又容易估爆显存。

`RouterStatics` 的解法是**在线学习**：维护一个对「真实输出长度」的指数移动平均（EMA）估计 `ema_req_out_len`，随着已完成请求的真实输出长度不断修正，作为「典型请求还会跑多长」的经验默认值。它还顺带持有 `busy_token_used_ratio`（即启动参数 `router_token_ratio`），也就是 `is_busy` 的阈值（见 [u2-l5](u2-l5-router-scheduling-loop.md)）。

#### 4.3.2 核心流程

EMA 的递推公式（标准定义）：

\[ \text{ema}_t = \text{ema}_{t-1}\,(1-\alpha) + x_t\,\alpha \]

其中 \(x_t\) 是第 \(t\) 个已完成请求的真实输出长度，\(\alpha\) 是平滑系数。\(\alpha\) 越大越「听新的」、跟踪越快但越抖动。

`RouterStatics` 的精巧之处在于 \(\alpha\) **本身随时间衰减**：

- 初始 \(\alpha_0 = 0.5\)（强跟踪，快速从默认值 2048 拉到真实分布）。
- 每次更新后 \(\alpha \leftarrow \max(0.04,\; \alpha \times 0.8)\)，逐步收敛到下限 0.04（稳态平滑）。

这样「早期快速校准、后期稳定」：

\[ \alpha_t = \max\!\left(0.04,\; 0.5 \times 0.8^{\,t}\right) \]

另外两个保护措施：观测值 \(x_t\) 先与 64 取 `max`（过滤掉「只输出几个 token 就结束」的极短请求，避免把 EMA 拉得过低、导致调度频繁暂停）；EMA 结果也与 64 取 `max` 兜底。

#### 4.3.3 源码精读

[lightllm/server/router/stats.py:7-12](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/stats.py#L7-L12) 是构造函数，给出三个超参的初值：

```python
class RouterStatics:
    def __init__(self, args: StartArgs):
        self.busy_token_used_ratio = args.router_token_ratio
        self.ema_req_out_len = 2048          # 默认输出长度初值
        self.cur_ema_params = 0.5            # α 初值
        self.min_ema_params = 0.04           # α 下限
```

`ema_req_out_len` 初值 2048 是一个相对保守的默认，等真实请求到来后会很快被修正。

[lightllm/server/router/stats.py:14-21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/stats.py#L14-L21) 是 EMA 更新本体：

```python
def update(self, req_out_len: int):
    # 过滤掉输出特别短的情况，防止计算得过于短，导致调度频繁引发暂停，导致系统吞吐下降。
    req_out_len = max(req_out_len, 64)
    self.ema_req_out_len = int(self.ema_req_out_len * (1 - self.cur_ema_params) + req_out_len * self.cur_ema_params)
    self.ema_req_out_len = max(64, self.ema_req_out_len)
    # 不断的调整ema 的计算参数...早期快速、后期稳定
    self.cur_ema_params = max(self.min_ema_params, self.cur_ema_params * 0.8)
```

这与 4.3.2 的公式逐行对应。`ema_req_out_len` 随后被 `Req.get_tuple_tokens` 用作「请求还没怎么输出时的默认期望输出长度」——详见 [lightllm/server/core/objs/req.py:364-393](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L364-L393)，其中关键分支：

```python
if self.sample_params.ignore_eos:
    cur_max_new_token_len = self.sample_params.max_new_tokens   # ignore_eos: 必须按上限估
elif is_busy:
    cur_max_new_token_len = self.sample_params.max_new_tokens   # 保守: 按上限估
else:
    cur_max_new_token_len = min(self.sample_params.max_new_tokens,
                                max(int(1.1 * has_out_len), ema_req_out_len))  # 激进: 用 EMA 估
```

`get_tuple_tokens` 最终返回一个元组 `(a_len, b_len)`：

- `a_len`：请求「已经固定占用」的 token 数（prompt + 已生成，或当前 KV 长度，取大）。
- `b_len`：请求「还要占用」的 token 数估计（受 chunked prefill 生命周期延长因子 `router_max_wait_tokens` 影响，外加一个 `ADDED_OUTPUT_LEN` 安全余量）。

这个 `(a_len, b_len)` 正是下一节峰值公式的输入。

[lightllm/server/router/stats.py:23-27](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/stats.py#L23-L27) 的 `log_str` 把当前统计量格式化成日志串，供 Router 每隔若干拍打印一次（见 4.4.3 的 `logger.debug(self.router_statics.log_str())`）。

#### 4.3.4 代码实践

**实践目标**：在纸上推演一次 EMA 更新，体会「α 衰减」的效果。

**操作步骤**：

1. 设 `ema_req_out_len` 初值 2048，`cur_ema_params = 0.5`。
2. 假设连续到来三个已完成请求，真实输出长度依次为 200、220、180。
3. 手算每次 `update` 后的 `ema_req_out_len` 与 `cur_ema_params`。

**需要观察的现象**：第一次更新后 EMA 就从 2048 大幅下跳到接近 200 附近；之后随着 α 衰减，EMA 对新观测的反应越来越弱。

**预期结果**（「待本地验证」——你可写个小脚本核对）：
- update(200)：α=0.5，ema = 2048·0.5 + 200·0.5 = 1124；α→0.4
- update(220)：α=0.4，ema = 1124·0.6 + 220·0.4 = 760.4 → 760；α→0.32
- update(180)：α=0.32，ema = 760·0.68 + 180·0.32 = 516.8 + 57.6 = 574；α→0.256

可以看到 α 从 0.5 经 0.4、0.32 一路衰减向 0.04，EMA 收敛速度逐渐放缓。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `update` 要先对 `req_out_len` 做 `max(req_out_len, 64)`？

**参考答案**：过滤掉「只输出极短就结束」的请求（比如被 stop string 截断、或天生只回答一两个字）。若不过滤，EMA 会被这些短样本拉得过低，导致调度器对每个新请求都估得很短、过早把请求塞进 batch，进而频繁触发 KV 不够→暂停，反而拉低吞吐。

**练习 2**：`is_busy=True` 时，`get_tuple_tokens` 为什么不再用 `ema_req_out_len`，而是直接用 `max_new_tokens`？

**参考答案**：`is_busy` 表示当前 KV 占用率已经超过 `router_token_ratio` 阈值，系统处于高压。此时应**保守**——按每个请求声明的最坏情况（`max_new_tokens`）估长，宁可少塞也不要估爆；只有非 busy 时才用 EMA 这个「乐观经验值」激进塞批以提升吞吐。

### 4.4 负载如何驱动调度与准入（综合）

#### 4.4.1 概念说明

前面三节分别讲了「存（`SharedArray`）」「存什么（`TokenLoad`）」「怎么估（`RouterStatics`）」。这一节把它们接回调度主循环，并厘清一个**容易误解**的因果关系。

直觉上很多人会以为：「Router 读取共享内存里的 `dynamic_max_load`，发现它太高，于是拒绝加入新请求」。**这是错的**。真实因果是反过来的：

- 在 Router **进程内部**，能否加入新请求的判断由 `_can_add_new_req` **当场重算**峰值完成（三道闸门），**不读**共享内存里已发布的旧值。
- 算完之后，作为**副作用**，把刚刚算出的峰值写进 `TokenLoad`（`set_estimated_peak_token_count` / `set_dynamic_max_load`），供**别的进程**（HttpServer、PD master）读取。
- 所以共享负载值是 Router 决策的**输出/广播**，不是 Router 决策的**输入**。它服务的对象是「跨进程背压与可观测性」，而不是 Router 自身的闸门。

记住这个区分，本节的代码就很好读了。

#### 4.4.2 核心流程

一条完整的「负载发布」闭环：

1. **每拍重算**：Router 的 `loop_for_fwd` 每拍调用 `req_queue.update_token_load(running_batch)`，它（在 6 秒节流或 `force_update` 下）重算峰值并发布 `current_load`/`estimated_peak_token_count`/`dynamic_max_load` 到共享内存。
2. **准入时也发布**：`_can_add_new_req` 在三道闸门全过、决定收下一个新请求时，立刻把「加上这个请求后的新峰值」写进共享内存，让读方尽快看到趋势变化。
3. **读方消费**：
   - HttpServer 的 `/token_load` 端点把账本以 JSON 暴露给运维监控。
   - 指标 `lightllm_batch_current_max_tokens` = `Σ dynamic_max_load × max_total_token_num`，经 MetricClient 上报（见 [u7-l7](u7-l7-metrics-health-monitor.md)）。
   - PD 分离模式下，每个节点把 `dynamic_max_load` 作为 `total_token_usage_rate` 上报给 PD master，master 据此把新请求路由到负载较低的节点。

峰值估算公式（连续批处理的标准上界，在 [u2-l6](u2-l6-reqqueue-chunked-prefill.md) 已提及，这里给出向量化实现）：设一批请求按剩余输出长度 \(b\) **降序**排列，\(a_i\) 为第 \(i\) 个请求已固定占用的 token 数，则峰值上界为：

\[ \text{peak} = \max_{i=0}^{n-1}\left[\, b_i \cdot (i+1) \;+\; \sum_{j=0}^{i} a_j \,\right] \]

直觉：剩余输出越多的请求存活越久、持有 KV 越久；排序后，「在第 \(i\) 个完成事件附近、仍有 \(i+1\) 个长请求存活」的瞬间往往是峰值点。\(b_i\cdot(i+1)\) 近似这些存活请求的新增占用，\(\sum a_j\) 是它们已占用的基数。这是一个保守上界，用于安全准入。

#### 4.4.3 源码精读

**峰值公式的向量化实现**——[lightllm/server/router/req_queue/chunked_prefill/impl.py:27-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54)（`_can_add_new_req`）：

```python
self.cache_len_list.append(req.get_tuple_tokens(is_busy, ema_req_out_len))  # 得到 (a,b)
self.cache_len_list.sort(key=lambda x: -x[1])          # 按 b 降序

left_out_len_array = np.array([e[1] for e in self.cache_len_list])  # b
has_run_len_array  = np.array([e[0] for e in self.cache_len_list])  # a
cum_run_len_array  = np.cumsum(has_run_len_array)                   # Σa 的前缀和
size_array         = np.arange(1, len(self.cache_len_list) + 1, 1)  # 1,2,3,... = (i+1)

need_max_token_num = (left_out_len_array * size_array + cum_run_len_array).max()  # 峰值公式
```

这段 numpy 正是 4.4.2 公式的直译：`left_out_len_array * size_array` 是 \(b_i\cdot(i+1)\)，`cum_run_len_array` 是 \(\sum_{j\le i} a_j\)，逐项相加取 max 即峰值。

**三道闸门**——紧接着的 [impl.py:39-44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L39-L44)：

```python
ok_token_num = need_max_token_num < self.max_total_tokens     # 闸门1: KV 峰值预算
ok_req_num   = len(self.cache_len_list) <= self.running_max_req_size  # 闸门2: 请求数上限
new_batch_first_router_need_tokens += req.get_first_router_need_tokens()
ok_prefill   = new_batch_first_router_need_tokens <= self.batch_max_tokens  # 闸门3: 单拍 prefill 预算
```

三道闸门全过才收下请求；此时**作为副作用**把新峰值发布出去——[impl.py:46-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L46-L51)：

```python
if ok_token_num and ok_req_num and ok_prefill:
    self.router.shared_token_load.set_estimated_peak_token_count(need_max_token_num, self.dp_index)
    self.router.shared_token_load.set_dynamic_max_load(
        need_max_token_num / self.max_total_tokens, self.dp_index)
    return True, new_batch_first_router_need_tokens
```

注意此处印证了 4.4.1 的论断：**闸门用的是当场算的 `need_max_token_num` 与 `self.max_total_tokens` 比较**，而 `set_dynamic_max_load` 只是把同一结果**广播**给共享内存。闸门本身不读共享账本。

> 旁注：`self.max_total_tokens` 不是裸的 `max_total_token_num`，而是 [base_queue.py:21](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L21) 里 `args.max_total_token_num - get_fixed_kv_len()`——某些特殊推理模式会预占一部分 KV，需扣除。非特殊模式下 `get_fixed_kv_len()` 返回 0。

**`current_load` 用到的实际占用**——[base_queue.py:44-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L44-L50) 的 `is_busy` 和 [base_queue.py:78-85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/base_queue.py#L78-L85) 的 `update_token_load` 都依赖 `router.get_used_tokens(dp_index)`，它的定义在 [manager.py:396-404](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L396-L404)：

```python
def get_used_tokens(self, dp_index):
    if not self.args.disable_dynamic_prompt_cache:
        return (self.max_total_token_num
                - self.read_only_statics_mem_manager.get_unrefed_token_num(dp_index)
                - self.radix_cache_client.get_unrefed_tokens_num(dp_index))
    else:
        return self.max_total_token_num - self.read_only_statics_mem_manager.get_unrefed_token_num(dp_index)
```

即「实际占用 = 总容量 − 未被引用的空闲量」，与 [u4-l2](u4-l2-radix-prefix-cache.md) RadixCache 的引用计数直接挂钩（开启动态 prompt cache 时还要扣除 RadixCache 树里未被引用的部分）。

**主循环驱动发布**——[manager.py:221-274](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L221-L274) 的 `loop_for_fwd` 每拍末尾调用 `self.req_queue.update_token_load(self.running_batch, force_update=self.is_pd_decode_mode)`，并把 `dynamic_max_load` 汇总上报为 `lightllm_batch_current_max_tokens` 指标（[manager.py:253-259](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/manager.py#L253-L259)）。

**读方一：可观测端点**——[api_http.py:208-228](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_http.py#L208-L228) 的 `/token_load` 把账本三个比例指标按 DP 组展平成 JSON 返回。注意它的注释也再次写明 `logical_max_load`「目前已未使用，其值与 dynamic_max_load 一样」。

**读方二：PD 节点上报**——[httpserver/pd_loop.py:253-261](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/pd_loop.py#L253-L261) 把每个 DP 组的 `dynamic_max_load` 求平均，作为 `total_token_usage_rate` 连同节点地址上报，PD master 据此路由——这是共享负载值真正影响「新请求去哪个节点」的地方。

#### 4.4.4 代码实践

**实践目标**：在运行中的服务上观察 `dynamic_max_load` 随并发请求的变化，验证它是 Router 决策的「输出」而非「输入」。

**操作步骤**（需可启动一个本地小模型服务，否则改为源码阅读型实践，见下）：

1. 按 [u1-l2](u1-l2-install-and-quickstart.md) 启动服务，例如：

   ```bash
   python -m lightllm.server.api_server \
     --model_dir <模型目录> --tp 1 --max_total_token_num 4096 --port 8000
   ```

2. 用 `curl` 查询负载基线：

   ```bash
   curl -s http://127.0.0.1:8000/token_load
   ```

   预期 `current_load`、`dynamic_max_load` 都接近 0（空载）。

3. 用一个循环**并发**发起多个长输出请求（让 batch 堆起来）：

   ```bash
   for i in $(seq 1 8); do
     curl -s http://127.0.0.1:8000/generate \
       -H 'Content-Type: application/json' \
       -d '{"inputs":"讲一个很长的故事","parameters":{"max_new_tokens":1024}}' &
   done
   ```

4. 在请求进行中，**另开终端**反复 `curl /token_load`。

**需要观察的现象**：`dynamic_max_load` 与 `estimated_peak_token_count`（后者需开 debug 日志看，或读 metric `lightllm_batch_current_max_tokens`）会随 batch 堆积而上升；`current_load` 随实际生成逐步上升。请求结束后三者回落。

**预期结果**：你将看到负载值是「Router 已经做了调度决策之后」的反映——并发越多、声明的 `max_new_tokens` 越大，发布出来的 `dynamic_max_load` 越高。当它逼近 1 时（即 `need_max_token_num` 逼近 `max_total_tokens`），`_can_add_new_req` 的 `ok_token_num` 闸门会开始 fail，新请求被挡在 `waiting_req_list` 里等待。

> **源码阅读型替代实践**（无法跑服务时）：在 [impl.py:27-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54) 中标注出「决策（三道 if）」与「发布（两个 set_*）」的边界，写一句话证明「闸门读的是局部变量 `need_max_token_num`，而不是 `get_dynamic_max_load()`」。这正是 4.4.1 因果论断的代码证据。

#### 4.4.5 小练习与答案

**练习 1**：本讲实践任务——说明 `logical_max_load` 与 `dynamic_max_load` 的区别，并解释这些共享值如何影响 chunked prefill 能否加入新请求。

**参考答案**：
- **区别**：历史上 `logical_max_load` 是「输入+输出长度朴素相加」的粗估，`dynamic_max_load` 是「考虑请求中途结束、用峰值公式算」的精估；但当前版本里 `set_dynamic_max_load` 会把 `logical_max_load` 一起写成同值（[token_load.py:60-64](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/token_load.py#L60-L64)），所以**两者运行时恒等**，`logical_max_load` 已退化为兼容字段。
- **如何影响 chunked prefill 加入新请求**：需要纠正一个直觉——真正决定「能否加入」的是 `_can_add_new_req` 里**当场重算**的 `need_max_token_num < max_total_tokens`（`ok_token_num` 闸门），**不读**共享账本。共享的 `dynamic_max_load`/`estimated_peak_token_count` 是这个决策的**广播输出**，服务于跨进程背压：PD master 读它来做节点路由、HttpServer 读它做 `/token_load` 监控、指标系统读它上报 `lightllm_batch_current_max_tokens`。换句话说，共享负载值**间接**影响「整个集群是否还往这个节点派请求」，但在单个 Router 内部，闸门是局部重算的。

**练习 2**：峰值公式里为什么要把请求按 \(b\)（剩余输出）**降序**排序后才累加？如果不排序会怎样？

**参考答案**：降序排列保证「存活最久（剩余最多）的请求排在前面」，使前缀和 \(\sum_{j\le i} a_j\) 与「第 \(i\) 个完成事件时仍存活的请求集合」对齐，从而取到的 max 才是真正的峰值上界。若不排序，`size_array` 与 \(b\) 不再单调对应，算出的量会偏离真实峰值上界，可能偏低（估爆显存）或失真。

**练习 3**：`need_update_dynamic_max_load()` 的注释写「3s」、实现是 `6.0`，这种不一致会带来什么实际影响？

**参考答案**：实际重算频率是「每 6 秒一次或 `force_update` 时立即重算」。读方（HttpServer/PD master）读到的 `dynamic_max_load` 最多滞后约 6 秒。由于该值是 advisory 的调度参考、且 `_can_add_new_req` 在准入时仍会即时 set，这种滞后是可接受的安全取舍；但读方不应假设它实时精确。

## 5. 综合实践

把本讲三个模块串起来，完成一次「从请求到账本」的端到端追踪。

**任务**：给定一个并发场景，画出「输入 → 统计 → 峰值公式 → 闸门 → 共享账本 → 读方」的完整数据流，并标注每一步用了本讲哪个组件。

**操作步骤**：

1. **构造场景**（纸上推演）：假设当前 batch 已有 2 个请求，其 `get_tuple_tokens` 返回值分别为 `(a=512, b=800)`、`(a=300, b=400)`；等待区新来一个请求返回 `(a=200, b=600)`。`max_total_tokens=4096`，`running_max_req_size=8`，`batch_max_tokens` 足够大。

2. **套峰值公式**：把三个请求的 \((a,b)\) 按 \(b\) 降序排列为 `[(512,800),(200,600),(300,400)]`。
   - \(i=0\)：\(800\times1 + 512 = 1312\)
   - \(i=1\)：\(600\times2 + (512+200) = 1912\)
   - \(i=2\)：\(400\times3 + (512+200+300) = 2212\)
   - \(\text{peak} = \max = 2212\)

3. **过三道闸门**：
   - `ok_token_num`：\(2212 < 4096\) ✅
   - `ok_req_num`：\(3 \le 8\) ✅
   - `ok_prefill`：假设预算充足 ✅
   - 结论：新请求可加入。

4. **发布账本**：成功后调用 `set_estimated_peak_token_count(2212)`、`set_dynamic_max_load(2212/4096≈0.54)`，并自动把 `logical_max_load` 也写成 0.54。

5. **读方消费**：PD master 若收到该节点的 `total_token_usage_rate≈0.54`，会与其他节点比较决定是否继续派请求；`/token_load` 端点会返回这三个比例值。

6. **代码对照**：用 [impl.py:27-54](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/req_queue/chunked_prefill/impl.py#L27-L54) 逐行核对你的手算与 numpy 实现是否一致。

**预期结果**：你能用一张图（请求 → `get_tuple_tokens` → 排序 → 峰值公式 → 三闸门 → `TokenLoad` → 读方）把本讲三个最小模块（共享数组、负载账本、统计）全部串起来，并指出闸门用的是局部变量、账本是副作用广播这一关键区分。

> 「待本地验证」：若你手头有可启动的模型，可用第 4.4.4 节的 curl 并发实验，配合服务端 debug 日志（`estimated_peak_token_count` 每隔若干拍打印一次）观察真实数值与你推演的数量级是否吻合。

## 6. 本讲小结

- `SharedArray` 把 numpy 数组**直接铺在共享内存 buffer 上**，读方写方零拷贝、无锁，适合高频更新的 advisory 负载值；`SharedInt` 是其单整数特化。
- `TokenLoad` 在共享内存里维护两张表：比例类（`current_load`/`logical_max_load`/`dynamic_max_load`，float64）与绝对数类（`estimated_peak_token_count`，int64），按 DP 组分行记账。
- `logical_max_load` 已**废弃**：`set_dynamic_max_load` 会顺手把它写成同值，两者运行时恒等；保留仅为 `/token_load` 接口兼容。
- `RouterStatics` 用**自适应衰减的 EMA** 在线估计请求输出长度（`ema_req_out_len`），早期 α=0.5 快速校准、后期衰减到 0.04 稳定，并带 64 的下限过滤防抖。
- 峰值上界公式 \(\text{peak}=\max_i[b_i(i+1)+\sum_{j\le i}a_j]\) 由 numpy 向量化实现，是三道闸门（`ok_token_num`/`ok_req_num`/`ok_prefill`）中 KV 预算闸门的基础。
- **关键因果**：共享负载值是 Router 调度决策的**广播输出**（供 PD master 路由、HttpServer 监控、指标上报），而**不是** Router 内部闸门的输入；闸门用的是当场局部重算的 `need_max_token_num`。

## 7. 下一步学习建议

- 想看「读方」如何把负载用于**多节点路由**与背压，继续阅读 [u7-l1](u7-l1-pd-disaggregation-kv-transfer.md)（PD 分离部署）与 [u7-l3](u7-l3-dp-and-load-balance.md)（数据并行与负载均衡），其中 `roundrobin`/`bs` 均衡器正是 `dynamic_max_load` 的直接消费者。
- 想深入「实际占用」的来源，回顾 [u4-l2](u4-l2-radix-prefix-cache.md) RadixCache 的引用计数——`get_used_tokens` 扣除的正是 RadixCache 树里未被引用的 token。
- 想了解负载如何变成监控指标，阅读 [u7-l7](u7-l7-metrics-health-monitor.md)（指标监控与健康检查），其中 `lightllm_batch_current_max_tokens` 就是本讲 `dynamic_max_load` 的产物。
- 源码层面，建议顺藤摸瓜读 `lightllm/server/router/req_queue/chunked_prefill/impl.py` 全文（含 `generate_new_batch`），把「挑批 → 估峰值 → 过闸门 → 发布账本」这条链路在脑中闭环。
