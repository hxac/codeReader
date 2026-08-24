# u6-l2 调度器与 APC 缓存感知源码

## 1. 本讲目标

上一讲（u6-l1）我们把 omni-proxy 当作一个黑盒来使用：知道它有两个 Nginx 动态模块、请求有十个阶段生命周期、`omni_proxy.sh` 能渲染出 nginx.conf。本讲打开这个黑盒，进入 C 源码层面。学完本讲，你应该能够：

1. 说出 omni_proxy 五个 C 源码文件各自的职责，以及它们被主模块 `ngx_http_omni_proxy_module.c` 调用的位置（C 模块结构）。
2. 解释 `omni_global_state_t` 这块共享内存里放了什么、多 worker 如何选举主调度器并保持视图一致（共享内存状态）。
3. 在 `omni_scheduler.c` 中准确定位「请求排序权重公式」与「过载保护判断」两段核心代码，讲清 prefill 调度中 radix tree 命中与未命中两条选择路径（负载感知调度）。
4. 理解 radix tree 如何用「块哈希链」维护每个推理节点的全局 KV 缓存视图，以及无锁读的实现（radix tree APC）。
5. 解释 ZMQ 如何把推理引擎广播的 KV 事件（BlockStored / BlockRemoved / AllBlocksCleared）变成 radix tree 的增、删、清空操作（ZMQ 事件订阅）。

## 2. 前置知识

本讲会接触到一批 Nginx 与系统编程概念，先用通俗语言过一遍：

- **Nginx 多进程模型**：Nginx 由一个 master 进程和多个 worker 进程组成，每个 worker 用 epoll 事件循环处理海量连接。omni_proxy 的调度逻辑就寄生在 worker 进程的事件循环里。
- **共享内存（shared memory）**：多个 worker 进程地址空间互相隔离，要让所有 worker 看到同一份「请求池 + 上游负载表」，就必须把它放进共享内存。Nginx 提供 `ngx_slab_pool_t`（slab 分配器，从共享内存段里切小块内存）和 `ngx_shmtx_t`（跨进程互斥锁）。
- **红黑树（rbtree）**：一种自平衡二叉搜索树，查找复杂度 \( O(\log n) \)。Nginx 内置 `ngx_rbtree_t`。omni_proxy 用它给 radix tree 的节点做「按哈希值索引」。
- **msgpack**：一种二进制 JSON，vLLM 侧把 KV 事件序列化成 msgpack 广播，C 侧用 `msgpack_unpack_next` 解析。
- **ZMQ PUB/SUB**：ZeroMQ 的发布/订阅模式。发布端 bind，订阅端 connect 并设置 topic 前缀；空 topic 表示订阅一切消息。ZMQ 自己管理收发缓冲队列，只暴露一个文件描述符（`ZMQ_FD`）给外部事件循环。
- **block hash（块哈希）**：把 token 序列按 block_size（本项目为 128）切块，每块算一个 64 位哈希；后一块的哈希把前一块的哈希链进来（链式哈希），因此「块哈希序列」天然等价于「token 前缀链」。这正是前缀缓存匹配的钥匙。
- **APC**：Aggregated Prefix Cache / 前缀缓存。同一个对话多轮请求时，前缀相同部分的 KV Cache 可以复用，prefill 只需算新增部分。调度器若能把请求派到「已经缓存了它的前缀」的节点，就能省掉大量计算。
- **承接 u6-l1**：请求十阶段生命周期中，`PHASE_TOKENIZING → PHASE_APC_MATCHING → PHASE_PREFILL_WAITING_SCHEDULE → PHASE_PREFILL_SCHEDULED` 这一段正是本讲的主战场。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| `components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c` | 4511 | 主模块：HTTP 处理、定时器、tokenizer 管道、KV 事件回调、上游初始化，是所有卫星模块的唯一调用方 |
| `components/omni-proxy/omni_proxy/modules/omni_scheduler.c` | 990 | 调度器：请求排序权重、prefill/decode 上游选择、过载保护、earliest-batch 预测算法 |
| `components/omni-proxy/omni_proxy/modules/omni_radix_tree.c` | 641 | 前缀树：块哈希链的插入、最长前缀匹配、删除，以及内置单元测试 |
| `components/omni-proxy/omni_proxy/modules/omni_apc.c` | 327 | KV 事件解析：把 msgpack 二进制解成 `KVCacheEvent` 结构体 |
| `components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c` | 274 | ZMQ 订阅器：SUB socket 生命周期管理，把 ZMQ fd 挂进 Nginx epoll |
| `components/omni-proxy/omni_proxy/modules/omni_tokenizer.c` | 275 | C↔Python 桥：嵌入 CPython 解释器，调用分词与块哈希生成 |
| `components/omni-proxy/omni_proxy/modules/omni_tokenizer_worker.c` | 254 | 分词工作线程：命令/响应双管道 + epoll 批量分词 |
| `components/omni-proxy/omni_proxy/modules/omni_shared_state.h` | 281 | 共享内存布局：全局状态、请求池、阶段分组、上游状态表 |
| `components/omni-proxy/omni_proxy/modules/omni_utils.h` | 208 | 工具内联函数：阶段位图、分组排序、主调度器选举 |
| `components/omni-proxy/omni_proxy/modules/omni_tokenizer.py` | 46814 字节 | Python 侧分词器：chat 模板展开、tokenize、`hash_block_tokens` 块哈希链 |

阅读建议：先读 `omni_shared_state.h`（数据结构是骨架），再按「定时器 → 调度器 → radix tree → ZMQ」的顺序精读。

## 4. 核心概念与源码讲解

### 4.1 C 模块结构：一个主模块和它的卫星模块

#### 4.1.1 概念说明

omni_proxy 的 C 代码是「主模块 + 卫星模块」的结构：`ngx_http_omni_proxy_module.c` 是 Nginx 动态模块的标准入口，负责 HTTP 请求处理与生命周期；其余 `.c` 文件都是被动提供函数的库，不注册任何 Nginx 指令。理解本讲的关键是：**调度不是在某个请求的处理函数里同步完成的，而是由三条独立的事件源驱动的异步状态机**：

1. **1 毫秒定时器**——周期性把「等待调度」分组里的请求派给上游节点（调度主循环）。
2. **ZMQ 文件描述符可读事件**——推理引擎广播 KV 事件到达，更新 radix tree。
3. **分词线程响应管道可读事件**——分词完成，触发 APC 匹配。

#### 4.1.2 核心流程

```text
worker 进程启动 (omni_proxy_init_process)
 ├─ 1. 注册定时器 handler，ngx_add_timer(1ms)
 ├─ 2. 初始化 tokenizer worker（pthread + 双管道）
 ├─ 3. omni_register_worker：抢占/选举主调度器
 └─ 4. omni_proxy_init_kv_listener：为每个上游建 ZMQ 订阅

事件循环中三个入口：
 A. omni_proxy_timer_handler（每 1ms）
     └─ omni_proxy_schedule → schedule_prefill / schedule_decode   ← 4.3
 B. omni_zmq_event_handler（ZMQ fd 可读）
     └─ message_callback = omni_proxy_kv_event_handler             ← 4.5
 C. ngx_omni_tokenizer_pipe_handler（resp_pipe 可读）
     └─ omni_proxy_post_tokenized → radix tree 匹配                ← 4.4
```

#### 4.1.3 源码精读

worker 进程初始化入口，按顺序装好定时器、分词线程与 KV 监听：

- [ngx_http_omni_proxy_module.c:3873-3910](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3873-L3910)：`omni_proxy_init_process` 把 `omni_proxy_timer_event.handler` 指向 `omni_proxy_timer_handler`（L3889），随后 `ngx_add_timer(..., TIMER_INTERVAL)` 启动周期定时器（L3905），最后调用 `omni_proxy_init_kv_listener` 建立 ZMQ 订阅（L3907）。
- [ngx_http_omni_proxy_module.c:23](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L23)：`#define TIMER_INTERVAL 1`——`ngx_add_timer` 的单位是毫秒，所以调度器 **每 1 毫秒跑一轮**。

定时器处理函数是三个入口的汇合点：

- [ngx_http_omni_proxy_module.c:2513-2529](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2513-L2529)：`omni_proxy_timer_handler` 先调度（L2515），再把全局调度结果同步到本地分组（L2518-2519），然后唤醒已被调度、等待发起上游连接的请求（L2521-2522），最后给自己续 1ms 定时器（L2528）——这是 Nginx 定时器的标准「自续期」写法。

分词完成的回程通路（入口 C）：

- [ngx_http_omni_proxy_module.c:3528-3580](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3528-L3580)：`ngx_omni_tokenizer_pipe_handler` 从响应管道读出完成分词的 slot_id，更新 `prompt_num_tokens`（L3552），打点「Enter state APC matching」，然后调用 `omni_proxy_post_tokenized` 进入 APC 匹配（L3564）。

#### 4.1.4 代码实践

**实践目标**：用静态检索验证「主模块是卫星模块唯一调用方」这一结构判断。

**操作步骤**：

1. 在 `components/omni-proxy/omni_proxy/modules/` 目录执行：

   ```bash
   grep -n "omni_zmq_handler_init\|omni_radix_tree_match\|omni_proxy_schedule_prefill\|omni_batch_chat_encode" \
        ngx_http_omni_proxy_module.c omni_tokenizer_worker.c
   ```

2. 对每个命中行，确认它所在的函数是主模块的哪个初始化/回调函数。
3. 反向检查：`grep -n "ngx_http_omni_proxy_module" omni_scheduler.c omni_apc.c omni_zmq_handler.c`，观察卫星模块是否反向依赖主模块。

**需要观察的现象**：卫星模块的对外函数只被 `ngx_http_omni_proxy_module.c`（以及 `omni_tokenizer_worker.c` 调用 `omni_batch_chat_encode` 这一处库间调用）引用；反向 grep 在卫星模块中几乎无命中（它们只 include 自己的头文件与 `omni_utils.h`）。

**预期结果**：得到一张「主模块 → 卫星模块」的单向依赖图；`omni_scheduler.c` 不 include 任何 Nginx HTTP 头（只含 `omni_proxy.h/omni_utils.h`），说明调度器纯粹操作共享状态，不碰 HTTP——这就是它能被任何事件源复用的原因。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `TIMER_INTERVAL` 是 1 而不是 100？
**答案**：单位是毫秒。调度周期决定「请求从进入等待分组到被派发」的最小延迟 granularity；1ms 让调度对 TTFT 几乎无贡献，代价是每秒 1000 次空轮询（等待分组为空时循环很快返回）。若改成 100ms，短请求的平均派发延迟会直接增加几十毫秒。

**练习 2**：ZMQ 事件和定时器事件会不会在同一个 worker 进程里并发执行？
**答案**：不会真正并行。两者都表现为 worker 进程 epoll 循环里的就绪事件，Nginx 在单线程事件循环中依次处理；因此 `omni_proxy_kv_event_handler` 与 `omni_proxy_schedule_prefill` 之间不需要互斥，只有跨进程的共享状态才需要 `ngx_shmtx` 锁。

### 4.2 共享内存状态：所有 worker 共用一张全局账本

#### 4.2.1 概念说明

Nginx 默认多 worker 进程各自为政，但调度需要**全局视野**：任意 worker 收到的 HTTP 请求都要进入同一个请求池、被同一个调度器看到；任意 worker 收到的 KV 事件都要更新同一份 radix tree。解法是把整个 `omni_global_state_t` 放进共享内存段，再辅以两条纪律：

- **主从调度**：多个 worker 中只有一个「主调度器」执行选节点逻辑，避免重复派发；主挂掉后其他 worker 可接管（选举）。
- **原子记账**：负载计数（运行数、token 数）用 `ngx_atomic_fetch_add` 无锁更新，只有涉及分组结构的修改才上 `ngx_shmtx` 锁。

#### 4.2.2 核心流程

```text
worker 启动 → omni_register_worker：
    if (master_worker_pid == 0 或 原主已死(kill(pid,0)!=0))
        抢占 master_worker_pid，标记本 worker 为主

每 1ms（仅主 worker）：
    lock(shmtx)
      schedule_prefill(gs) / schedule_decode(gs)   // 修改全局 groups 与上游计数
    unlock
所有 worker：
    omni_update_local_waiting()  // 把全局结果搬运回本地分组视图
```

#### 4.2.3 源码精读

共享内存的总账本（先看后半段的上游状态表）：

- [omni_shared_state.h:236-279](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L236-L279)：`omni_global_state_t` 包含请求池（L249）、按阶段分组的 `groups[PHASE_MAX]`（L250）、三类上游状态数组 `encode_states/prefill_states/decode_states`（L260-262）、主调度器 PID（L259）、TTFT/TPOT/E2E 指标桶（L265-275）与 prefill 批执行时间统计（L278）。规模上限在文件头定义：最多 1024 个 prefill 上游、4096 个 decode 上游、16384 个请求槽位（[omni_shared_state.h:16-19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L16-L19)）。

每个 prefill 上游自带一棵 radix tree，这就是「每节点一份 KV 缓存视图」的物理形态：

- [omni_shared_state.h:186-201](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L186-L201)：`omni_upstream_prefill_t` 的 `radix_tree` 指针（L198）以及负载字段 `num_running/num_queue/num_batch_exec/num_tokens`（L190-194）。decode 上游结构（[L203-218](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L203-L218)）还额外持有 `kv_handler`（ZMQ 订阅器，L213）与自己的 `radix_tree`（L215）。
- [omni_shared_state.h:77-102](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L77-L102)：请求槽 `omni_req_t` 里存着 APC 匹配结果 `prefill_match_depths[MAX_PREFILL_UPSTREAMS]` 与 `decode_match_depths[MAX_DECODE_UPSTREAMS]`（L96-99）——4.4 的匹配结果写进来，4.3 的调度器读出去，两个模块靠这个数组解耦。

主调度器选举（用 `kill(pid, 0)` 探活原主）：

- [omni_utils.h:134-149](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_utils.h#L134-L149)：`omni_register_worker` 在共享锁内判断 `master_worker_pid == 0`（还没人选过）或 `kill(...) != 0`（原主进程已不存在）则自封为主；`omni_is_master_worker` 只是读本地标志。

调度入口只让主 worker 进临界区：

- [ngx_http_omni_proxy_module.c:2500-2511](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2500-L2511)：`omni_proxy_schedule` 先 `omni_is_master_worker` 过滤，再 `ngx_shmtx_lock` 包住 `omni_proxy_schedule_prefill/decode`。
- [ngx_http_omni_proxy_module.c:2459-2484](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L2459-L2484)：非主 worker 用 `omni_update_local_waiting` 把全局分组的变化搬运到本地分组（依据 `req->has_prefill_sched` 标志），保持双份视图一致。

#### 4.2.4 代码实践

**实践目标**：体察「原子计数 vs 互斥锁」两种并发手段的分工。

**操作步骤**：

1. 执行 `grep -n "ngx_atomic_fetch_add" components/omni-proxy/omni_proxy/modules/*.c`，统计所有原子记账点。
2. 执行 `grep -n "ngx_shmtx_lock" components/omni-proxy/omni_proxy/modules/*.c`，列出所有临界区。
3. 对照两类操作的客体：改的是「标量计数」还是「链表/红黑树结构」。

**需要观察的现象**：`ngx_atomic_fetch_add` 集中出现在调度器记账（如 [omni_scheduler.c:842-844](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L842-L844) 的 `num_running/num_tokens/comm.ref` 三连加）；`ngx_shmtx_lock` 出现在修改分组归属、遍历上游表、操作 radix tree 的场合。

**预期结果**：得出规律——**单行标量更新走原子，多步结构修改走锁**。`comm.ref` 引用计数配合 `STATUS_ABANDON` 状态（[omni_shared_state.h:38-44](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L38-L44)）用于配置 reload 时安全复用/废弃上游槽位。

#### 4.2.5 小练习与答案

**练习 1**：如果两个 worker 同时通过 `omni_register_worker` 抢主，会发生什么？
**答案**：不会出现双主。函数整体在 `ngx_shmtx_lock(&gs->shmtx)` 临界区内（[omni_utils.h:136-143](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_utils.h#L136-L143)），第二个进入的 worker 会看到刚被写入的 `master_worker_pid` 且 `kill` 探活成功，从而放弃抢占。

**练习 2**：为什么请求要同时挂在「全局分组」和「本地分组」两份结构里？
**答案**：全局分组是主调度器的输入（决定给谁派节点），但 HTTP 连接与回调只能由最初接收该请求的 worker 处理；本地分组让每个 worker 知道「我手上的哪些请求已被调度、该发起上游连接了」。两份视图靠 1ms 定时器里的 `omni_update_local_waiting` 对齐。

### 4.3 负载感知调度：omni_scheduler.c 的排序与选择

#### 4.3.1 概念说明

prefill 调度是两级决策：

1. **排序（先为谁服务）**：每个调度周期对等待分组里的请求算一个权重，按权重降序排列。权重鼓励「prompt 短、等待久」的请求先走，同时等待时间项保证大 prompt 不会永远饿死。
2. **上游选择（派给谁）**：对排好序的每个请求，在候选上游中选一个节点。默认算法是「APC 命中优先，其次最轻负载」；可选 `earliest_batch` 算法改为「预测最早完成时间」。两级都带**过载保护**：节点负载超过阈值就跳过它，请求留在队列等下个周期。

decode 调度结构类似，但多一条硬约束：**只能选与 prefill 同 `group_id` 的 decode 节点**（分组调度的运行时体现，配置面在 u6-l3 讲）。

#### 4.3.2 核心流程

```text
omni_proxy_schedule_prefill(gs, olcf)          [每 1ms，仅主 worker]
 ├─ update_prefill_weights(group)              ① 排序：算权重 + 降序排列
 ├─ for 每个请求（按权重序）:
 │   ├─ if schedule_algo == EARLIEST_BATCH:
 │   │     select_earliest_prefill()           ② 预测型选择（可选）
 │   ├─ else（default）:
 │   │     for j in 所有 ENABLE 的 prefill 上游:
 │   │         if 负载超阈值: continue          ③ 过载保护
 │   │         m = req->prefill_match_depths[j] ④ APC 命中深度（4.4 写入）
 │   │         按 (m 大 > token 少 > running 少) 三级比较取 best
 │   ├─ if best_match > 0:  选命中节点          ⑤ 命中路径
 │   ├─ else: 从上次选中处轮询找 least_load     ⑥ 未命中路径
 │   └─ 记账：num_queue/num_running/num_tokens
 │       相变 WAITING_SCHEDULE → SCHEDULED
```

排序权重公式（prefill 侧）：

\[ w_i = 0.8 \cdot \frac{T_{\max} - T_i}{T_{\max}} + 0.2 \cdot \frac{W_i}{W_{\max}} \]

其中 \(T_i\) 是该请求的 prompt token 数，\(T_{\max}\) 是当前队列中的最大值，\(W_i\) 是已等待时长（下限 50ms），\(W_{\max}\) 是队列中的最大等待。prompt 越短、等得越久，\(w_i\) 越大，排序越靠前。

decode 侧权重则是「总 token 规模」归一化：

\[ w_i = \frac{T_i + \alpha \cdot M_i}{\max_j\left(T_j + \alpha \cdot M_j\right)} \]

其中 \(M_i\) 是请求的 `max_tokens`（生成长度上限），\(\alpha\) 是配置项 `omni_proxy_max_tokens_weight`。

`earliest_batch` 算法给候选节点估计「若把新请求加进去，它多久能完成」：

\[ \text{wait}_j = \Delta e \cdot Q_j^2 + e(Q_j + 1) + e_{\text{current},j}, \qquad \text{finish}_j = \text{now} + \text{wait}_j \]

其中 \(Q_j\) 是该节点排队数，\(e(\cdot)\) 是「批大小 → 执行时长」的经验函数（按批大小分桶的滑动窗口平均，查不到就线性外推，每多一个请求加 50ms，封顶 30s），\(\Delta e = e(Q_j+1) - e(Q_j)\)。选 finish 最小者，并在 25ms 阈值内随机打散避免集中。

#### 4.3.3 源码精读

**① 排序权重**——本讲实践任务要找的第一段核心代码：

- [omni_scheduler.c:583-646](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L583-L646)：`update_prefill_weights` 先扫描一遍求 \(T_{\max}\) 与 \(W_{\max}\)（L585-606，等待下限钳到 50ms 见 L608-611），再对每个请求按上式算 `info->weight`（L623-626），最后 `omni_sort_compact_group` 排序并打印 `[Prefill-Sort]` 日志。排序比较器在 [omni_utils.h:83-107](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_utils.h#L83-L107)：`cmp_req_info_desc` 按权重**降序**（in_use 的排前面），排序后把空洞压实到 `num_requests` 以内。
- [omni_scheduler.c:648-686](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L648-L686)：`update_decode_weights` 用总 token 规模做权重（公式在 L665），日志前缀 `[Decode-Sort]`。

**②③④⑤⑥ 上游选择主循环**——实践任务要找的第二段核心代码：

- [omni_scheduler.c:688-859](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L688-L859)：`omni_proxy_schedule_prefill`。先排序（L699），再按权重序遍历请求（L701）。
  - **过载保护**在 [L773-776](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L773-L776)：`load_tokens > olcf->max_batch_num_token || running > olcf->prefill_max_num_seqs` 即 `continue`——该节点本周期不再接新请求。两个阈值分别来自 nginx 指令 `omni_proxy_max_batch_num_token` 与 `omni_proxy_prefill_max_num_seqs`（指令表见 [ngx_http_omni_proxy_module.c:4357-4385](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L4357-L4385)）。
  - **APC 优先的三级比较**在 [L763-786](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L763-L786)：`m = req->prefill_match_depths[j]`（L769，即 4.4 匹配写入的前缀命中深度），比较规则为「命中深度大者优先 → 同深度比 token 负载小 → 再比 running 小」。
  - **命中路径**在 [L788-792](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L788-L792)：`best_match > 0` 时选中该节点并打印 `Prefix cache hit on: ...`。
  - **未命中路径**在 [L793-823](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L793-L823)：从 `gs->last_selected_prefill` 起环形轮询，选 `num_tokens` 最小的节点（负载归零可提前 break），日志 `No Prefix cache hit, choose least workload Prefill`。
  - **记账与相变**在 [L826-855](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L826-L855)：记 `prefill_upstream_endpoint_idx` 与 `prefill_group_id`，原子累加三个负载计数，`PHASE_PREFILL_WAITING_SCHEDULE → PHASE_PREFILL_SCHEDULED`。

**decode 侧的分组约束与保护**：

- [omni_scheduler.c:861-988](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L861-L988)：`omni_proxy_schedule_decode`。第一遍循环先按 `decode_match_depths` 找最优（L895-923），其中 `comm.group_id != req->prefill_group_id` 直接跳过（L901-904）——**decode 必须与 prefill 同组**；`running > olcf->decode_max_num_seqs` 的过载保护在 [L910-913](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L910-L913)。第二遍循环（L924-952）在组内找最轻负载；若组内全满，则从「组内可达节点」中随机挑一个兜底（L954-963），避免请求卡死。

**earliest_batch 预测算法**（可选调度器）：

- [omni_scheduler.c:362-580](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L362-L580)：`omni_scheduler_select_earliest_prefill` 对每个候选估计 finish 时间（核心公式在 [L403-444](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L403-L444)，其中排队项 \(\Delta e \cdot Q^2\) 在 L430），随后过滤出与最优差距 ≤ 25ms 的候选（L497-505）、排序取 top_k、在阈值内随机打散（L543-559）。
- [omni_scheduler.c:182-343](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L182-L343)：`omni_scheduler_estimate_batch_exec_time` 是经验函数 \(e(\cdot)\)：先查「批大小分桶」的近期平均（桶结构见下），查不到就向小/大桶搜索并按每请求 50ms 线性外推（增量常量定义在 [L152-154](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L152-L154)），全都没有时退回静态基线表 `{0, 220, 280, 330, 380}`（L187）。
- [omni_scheduler.c:73-149](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L73-L149)：`omni_scheduler_record_prefill_batch_stat` 把每批真实执行时长写进 128 深度的环形窗口（按批大小最多 64 个桶，容量定义在 [omni_shared_state.h:14-15](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_shared_state.h#L14-L15)），供上面的估计函数查询；调用点在主模块统计批次指标处（[ngx_http_omni_proxy_module.c:1467](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L1467)）。
- 算法选择开关：nginx 指令 `omni_proxy_schedule_algo`，取值枚举 `default / earliest_batch`（[ngx_http_omni_proxy_module.c:77-80](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L77-L80)，指令注册在 [L4434-4439](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L4434-L4439)）。只有配置为 `earliest_batch` 时，主循环才在 [omni_scheduler.c:722-759](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L722-L759) 走预测算法；若 `select_earliest_prefill` 返回 `NGX_ERROR`（无可用候选），`used_earliest_algo` 保持 0，回落到 L761 起的 default 路径。

官方文档对这套设计的描述可对照 [README_CN.md:89-96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L89-L96)（请求排序、上游节点选择、过载保护三段）。

#### 4.3.4 代码实践

**实践目标**：不看讲义，独立定位「排序权重」与「过载保护」两段代码（本讲综合实践的前半部分）。

**操作步骤**：

1. `grep -n "0.8\|0.2" components/omni-proxy/omni_proxy/modules/omni_scheduler.c`——定位 prefill 权重的加权系数。
2. `grep -n "max_batch_num_token\|prefill_max_num_seqs\|decode_max_num_seqs" components/omni-proxy/omni_proxy/modules/omni_scheduler.c`——定位三处过载阈值判断。
3. 阅读命中行所在的完整函数，确认它们各自在哪个循环里、`continue` 掉的是什么。

**需要观察的现象**：权重公式在 `update_prefill_weights` 内（约 L623-626）；过载判断出现两处——prefill 主循环的 L773-776 与 decode 循环的 L910-913，且都通过 `continue` 把超载节点从候选集中剔除，而不是报错。

**预期结果**：能够回答「权重 0.8/0.2 分别乘的是什么」「过载时请求去了哪里」——被跳过的只是该节点，请求若还有其他候选节点则正常派发；若所有节点都超载，`best_idx == UINT32_MAX` 且轮询也选不中，请求保留在等待分组，等下一个 1ms 周期重试。

#### 4.3.5 小练习与答案

**练习 1**：权重公式里 token 项用 \(T_{\max} - T_i\)（越短越大）而不是直接用 \(T_i\)，为什么？
**答案**：归一化后方向取反，使「短 prompt 权重大、排前面」。这近似最短作业优先（SJF）：prefill 耗时近似与 prompt 长度成正比，先做短的能降低平均等待。20% 的等待时间项则防止长请求无限饿死——等得越久权重越高，最终会浮到队首。

**练习 2**：decode 选择为什么必须过滤 `group_id`？
**答案**：PD 分离下 decode 节点要承接 prefill 产出的 KV。分组调度（u6-l3 的 `omni_proxy_prefill_groups/decode_groups`）把「一组 P」与「一组 D」绑定成Pair，KV 只在组内传输。若把请求派去别的组的 D 节点，那边既拿不到 KV 也无法接力，所以代码在两遍循环里都做了 `comm.group_id != req->prefill_group_id → continue` 的硬过滤。

**练习 3**：`least_load == 0` 时为什么可以直接 `break` 出轮询循环？
**答案**：负载是 `uint32_t` 的 token 计数，0 已是最小可能值，不可能有更优候选，提前退出省掉剩余节点的扫描（[omni_scheduler.c:806-814](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_scheduler.c#L806-L814)）。

### 4.4 radix tree APC：块哈希链驱动的全局 KV 缓存视图

#### 4.4.1 概念说明

APC 调度的前提是 proxy 知道**每个推理节点缓存了哪些前缀**。omni_proxy 的方案分三步：

1. **分词时算块哈希链**：请求 tokenize 完成后，把 input_ids 按 128 token 一块切块，算链式哈希——第 k 块的哈希把第 k-1 块的哈希当作输入。于是「块哈希序列」与「token 前缀」一一对应。
2. **radix tree 存前缀**：每个上游节点一棵树，节点即块哈希，父即前缀块。KV 事件到达时插入/删除（4.5）；请求到来时拿它的哈希链去树上走，能走多深就命中多少块。
3. **命中深度指导调度**：匹配结果写进 `req->prefill_match_depths[]`，供 4.3 的调度器做「命中优先」选择。

这里有一个精妙的工程细节：树的「父子关系」用链表表达，但查找用一棵**按节点哈希为 key 的红黑树**做全局索引——把「找某条边」降为一次 \( O(\log n) \) 的按 key 查找加父指针校验。

#### 4.4.2 核心流程

块哈希链的数学定义（与 vLLM 侧算法一致，这是能对上号的前提）：

\[ h_0 = \mathrm{sha256}\big(\mathrm{pickle}(\texttt{PYTHONHASHSEED})\big), \qquad h_k = \mathrm{trunc}_{64}\Big(\mathrm{sha256}\big(\mathrm{pickle}(h_{k-1}, \mathrm{tokens}_k, \mathrm{None})\big)\Big) \]

- 每 128 个 token 一块；**不足 128 的尾块直接丢弃**（不完整块不会被缓存，自然也不该参与匹配）。
- \( \mathrm{trunc}_{64} \) 指取 sha256 摘要的前 8 字节按大端转成的 64 位整数。
- 链式结构保证：两个请求只要前 j 块完全相同，它们的 \(h_1..h_j\) 必然相同——这正是 radix tree 能按前缀共享节点的原因。

匹配流程（每请求一次）：

```text
tokenize 完成 (ngx_omni_tokenizer_pipe_handler)
 → omni_proxy_post_tokenized(req)
    for 每个 ENABLE 的 prefill 上游 i:
        match_depth[i] = radix_tree_match_optimistic(tree_i, req.block_hashes)
    for 每个 ENABLE 的 decode 上游 i: 同上
    写回 req->prefill_match_depths / decode_match_depths
    相变 → PHASE_PREFILL_WAITING_SCHEDULE（交给 4.3 的调度器）
```

#### 4.4.3 源码精读

**块哈希链的生成（Python 侧）**：

- [omni_tokenizer.py:1030-1046](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_tokenizer.py#L1030-L1046)：`hash_block_tokens` 按上式实现——链首取 `pickled_sha256(PYTHONHASHSEED)`（L1032-1033），每块 `pickled_sha256((parent_hash, tuple(block_token_ids), None))` 后截断为 64 位（L1042-1043），并把本块哈希滚动为下一块的 parent（L1044）；不足 `block_size` 的尾块 `break`（L1039-1040）。`pickled_sha256` 的定义在 [L1025-1027](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_tokenizer.py#L1025-L1027)：对输入做 `pickle.dumps` 再 sha256。分词入口对每条请求调用它（[L1076](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_tokenizer.py#L1076)）。
- [omni_tokenizer.py:24-27](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_tokenizer.py#L24-L27)：模块导入时强制要求设置 `PYTHONHASHSEED` 环境变量，否则抛错——这就是 u6-l1 提到「PYTHONHASHSEED=123 是 APC 匹配前提」的源头：它参与链首哈希，proxy 与推理引擎两侧必须取相同值才能算出相同链。

**radix tree 的数据结构**：

- [omni_radix_tree.h:12-31](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.h#L12-L31)：节点 `omni_radix_node_s` 含红黑树节点（内嵌）、`hash_key`（本块哈希）、`first_child/next_sibling`（孩子链表）、`parent` 指针；树 `omni_radix_tree_t` 含 slab 池、root、全局索引红黑树 `lookup_tree`、互斥锁与 `version` 计数。
- [omni_radix_tree.c:21-62](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L21-L62)：`omni_radix_tree_init` 从共享内存 slab 池分配树与根节点（`ngx_slab_alloc`），初始化互斥锁并把根（hash 0）插入索引红黑树。**节点全部住在共享内存里**，所以多个 worker 都能读到同一棵树。文件头注释（[L8-19](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L8-L19)）说明了设计取舍：红黑树 key 从「边id」改为「节点id（child_hash）」，靠 parent 指针支撑 O(1) 摘链。

**插入（写路径）**：

- [omni_radix_tree.c:101-156](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L101-L156)：`omni_radix_tree_add_chain` 持锁沿哈希链逐块推进：`omni_find_node(tree, current->hash_key, current_hash)` 找「当前节点之下哈希为 current_hash 的孩子」，找到则复用（前缀共享！），找不到则分配新节点、挂进父的孩子链表与全局红黑树，最后 `version++`。查找函数 `omni_find_node` 在 [L64-99](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L64-L99)：按 `child_hash` 在红黑树里二分，命中后再校验 `orn->parent->hash_key == parent_hash`。

**匹配（读路径，有无锁两个版本）**：

- [omni_radix_tree.c:158-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L158-L197)：`omni_radix_tree_match` 持锁版本，逐块走树，走不动即返回已走的深度。
- [omni_radix_tree.c:199-262](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L199-L262)：`omni_radix_tree_match_optimistic` **无锁**版本：读前记录 `tree->version`，直接在红黑树上裸走，读后再比对 version；若期间有写者改过树（version 变化）则整趟重试（do-while）。这是乐观读策略——匹配远多于修改，绝大多数时候一次成功，避免每个请求的匹配都去抢全局锁。

**删除**：

- [omni_radix_tree.c:338-362](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L338-L362)：`omni_radix_tree_remove` 按哈希找到节点，从父链表摘除后递归删除整个子树（一个块被逐出缓存，它的所有延长前缀也必然失效）。
- [omni_radix_tree.c:264-305](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L264-L305)：`omni_recursive_remove` 的实现很有味道：先用指针反转把子树**展平成后序链表**（O(1) 辅助空间），再线性地逐个从红黑树摘除并释放。

**每请求的匹配入口**：

- [ngx_http_omni_proxy_module.c:227-275](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L227-L275)：`omni_proxy_post_tokenized` 的 prefill 部分——对每个 ENABLE 的 prefill 上游调用 `omni_radix_tree_match_optimistic(tree, req->tokenizer_req.block_hashes, block_hashes_len)`（L246-248），打印 `[APC_MATCHING-...]` 日志；decode 部分同构（[L277-321](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L277-L321)）；两组结果最终 `ngx_memcpy` 进 `req->prefill_match_depths/decode_match_depths`（L318-321），随后相变进入等待调度（L328-339；`PD_AGGREGATION` 模式则直接跳去 decode 等待）。

**树的初始化时机与内置单测**：

- [ngx_http_omni_proxy_module.c:3065-3083](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3065-L3083)：配置解析阶段注册上游时，若设置了 `omni_proxy_vllm_kv_port_offset`，即为每个 prefill/decode 上游 `omni_radix_tree_init` 一棵空树。
- [omni_radix_tree.c:383-642](https://github.com/gitcode.com/ascend-tribe-openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L383-L642)：`omni_radix_tree_test_internal` 是 15 个用例的内置单测（加链/部分匹配/公共前缀/最长前缀/删除/不存在删除/再插入等），对 `match` 与 `match_optimistic` 各跑一遍（L624-642）。它在每次建立 KV 订阅前被执行（[ngx_http_omni_proxy_module.c:3799-3801](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3799-L3801)）——这份测试代码是理解树语义的最佳教材。

#### 4.4.4 代码实践

**实践目标**：在纯 CPU 环境（无需 NPU、无需部署）验证链式块哈希的三个性质：前缀确定性、链式依赖、尾块截断。

**操作步骤**：`hash_block_tokens` 只依赖标准库（hashlib/pickle/os），把它抄出来做成独立脚本即可（直接 import `omni_tokenizer` 会连带要求 transformers 等依赖，不必）。以下为**示例代码**（非项目原文件，函数体逐行取自 `omni_tokenizer.py`）：

```python
# 示例代码：block_hash_demo.py —— 从 omni_tokenizer.py 抄出的两个函数
import hashlib, os, pickle
from typing import List

def pickled_sha256(value) -> bytes:
    input_bytes = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    return hashlib.sha256(input_bytes).digest()

def hash_block_tokens(token_ids: List[int], block_size: int = 128) -> List[int]:
    block_hashes = []
    NONE_HASH = pickled_sha256(os.environ.get("PYTHONHASHSEED"))
    parent_block_hash_value = NONE_HASH
    for start in range(0, len(token_ids), block_size):
        end = start + block_size
        block_token_ids = token_ids[start:end]
        if len(block_token_ids) < block_size:
            break
        block_hash = pickled_sha256((parent_block_hash_value, tuple(block_token_ids), None))
        block_hashes.append((int.from_bytes(block_hash, byteorder="big")) & ((1 << 64) - 1))
        parent_block_hash_value = block_hash
    return block_hashes

if __name__ == "__main__":
    os.environ["PYTHONHASHSEED"] = "123"
    a = list(range(128 * 3))            # 3 个整块
    b = list(range(128 * 3)) + [7, 8]   # 同前缀 + 2 个零散 token（尾块不足 128）
    c = list(range(128 * 3)); c[200] = -1  # 中途改一个 token
    print("a:", hash_block_tokens(a))
    print("b:", hash_block_tokens(b))   # 观察：与 a 完全一致（尾块被丢弃）
    print("c:", hash_block_tokens(c))   # 观察：第 1 块相同，第 2 块起全变
```

运行：`python3 block_hash_demo.py`。

**需要观察的现象**：`b` 的哈希链与 `a` 完全相同（证明尾块截断 + 前缀确定性——调度到同前缀节点即可复用 3 块 KV）；`c` 只有第 1 块与 `a` 相同，第 2 块起全部改变（证明链式依赖——改一个 token 会污染其后所有块）。

**预期结果**：三条打印中 `len == 3`；`hash(a) == hash(b)`；`hash(c)[0] == hash(a)[0]` 且 `hash(c)[1:] != hash(a)[1:]`。若把 `PYTHONHASHSEED` 改成别的值再跑，所有哈希全变——这就是 proxy 与 vLLM 两侧必须约定同一 seed 的原因。

#### 4.4.5 小练习与答案

**练习 1**：为什么 radix tree 不直接存 token，而存「块哈希」？
**答案**：三个理由：①压缩——一个节点代表 128 个 token，树高缩小 128 倍；②对齐——vLLM 的 KV Cache 本来就按 block 管理，事件里给的身份证就是块哈希；③安全——64 位哈希做 key 可直接进红黑树，比较是 O(1) 整数比较。

**练习 2**：`omni_radix_tree_match_optimistic` 重试的触发条件是什么？会不会死循环？
**答案**：读前读后各一次 `ngx_memory_barrier` + version 比对（[omni_radix_tree.c:211-259](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L211-L259)），version 变了就整趟重试。写频率远低于读（每秒 KV 事件数 << 每秒请求数 × 上游数），连续撞上写者的概率极小；理论上高竞争时会多转几圈但每次都是完整一致快照，不会读到撕裂数据。

**练习 3**：阅读单测 Test 6（[omni_radix_tree.c:463-472](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_radix_tree.c#L463-L472)），为什么查询 `{128803, 1182, 5567, 2342, 34242}` 返回 3？
**答案**：树里此前只插入过链 `{128803, 1182, 5567}`（Test 5）。查询链的前 3 块能沿树走到底，第 4 块 `2342` 找不到孩子即停，`match_depth` 停在 3——「最长前缀匹配」语义：能复用的 KV 恰好是前 3 块。

### 4.5 ZMQ 事件订阅：KV 事件如何变成 radix tree 的增删

#### 4.5.1 概念说明

radix tree 的数据从哪来？推理引擎（vLLM）在 KV Cache 变化时会对外广播事件消息：**BlockStored**（一批块被缓存）、**BlockRemoved**（块被逐出）、**AllBlocksCleared**（缓存清空）。omni_proxy 为每个上游节点建一个 ZMQ SUB 订阅，把这些事件实时翻译成 radix tree 的 `add_chain / remove / destroy+init`。部署上对应 u6-l1 提过的开关链：ansible 模板 `USE_OMNI_PROXY=1` + pd_run.sh `ENABLE_APC_EVENT=1`（见 [README_CN.md:139-140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L139-L140)）。

整条链路四个角色：**omni_zmq_handler.c**（订阅与事件桥接）→ **omni_apc.c**（msgpack 解析）→ 主模块的 **omni_proxy_kv_event_handler**（语义分派）→ **omni_radix_tree.c**（落树）。

#### 4.5.2 核心流程

```text
初始化（omni_proxy_init_kv_listener，仅当配置了 omni_proxy_vllm_kv_port_offset）
  for 每个上游（按 cnt % worker_processes == ngx_worker 分片给本 worker）:
      订阅地址 = 上游 ip : (上游端口 + kv_port_offset)
      omni_zmq_handler_init(addr, topic="", callback=omni_proxy_kv_event_handler)
      把 ZMQ_FD 包装成 nginx connection 挂进 epoll

运行时（ZMQ fd 可读）
  omni_zmq_event_handler:
      while 有消息: 依次收 topic 帧、seq 帧、payload 帧（三段式）
          callback(handler, topic, payload, len)
  omni_proxy_kv_event_handler:
      batch = parse_kv_event_batch(payload)          # msgpack → [ts, [events]]
      按 handler->index 定位上游的 radix_tree        # <1024 是 prefill，否则 decode
      for event in batch:
          BlockStored      → add_chain(block_hashes)
          BlockRemoved     → for h: remove(h)
          AllBlocksCleared → destroy + 重新 init 一棵空树
```

#### 4.5.3 源码精读

**订阅器**：

- [omni_zmq_handler.c:142-250](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L142-L250)：`omni_zmq_handler_reinit` 创建 context 与 `ZMQ_SUB` socket（L151-159），设最大消息 64MB（L169-170，容量宏在 [L8](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L8)）与 100ms 接收超时（L172-173），`tcp://ip:port` connect 发布端（L175-185），订阅空 topic 即**订阅全部消息**（L187-197）。关键的桥接在 [L199-243](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L199-L243)：用 `ZMQ_FD` 拿到 ZMQ 的信号 fd，`ngx_get_connection` 包装成 Nginx 连接、设非阻塞、read handler 指向 `omni_zmq_event_handler`，最后 `ngx_add_conn` 挂进 epoll——从此 ZMQ 消息到达表现为 Nginx 事件循环里的一次普通读事件。失败清理统一走 `omni_zmq_cleanup_resources`（[L117-140](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L117-L140)）。
- [omni_zmq_handler.c:14-116](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L14-L116)：`omni_zmq_event_handler` 先 `ZMQ_EVENTS` 查询就绪状态（POLLERR 即降级失活，L28-34），然后循环 `ZMQ_DONTWAIT` 收**三段式消息**：topic 帧 → seq 帧 → payload 帧（L36-102；中段缺 more 的残包直接丢弃），凑齐后调用 `message_callback`（L104-110），即初始化时传入的 `omni_proxy_kv_event_handler`。

**解析器（msgpack → 结构体）**：

- [omni_apc.c:229-299](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L229-L299)：`parse_kv_event_batch` 期望外层是 `[时间戳, [事件数组]]` 的二元数组（L241-245），逐事件调 `parse_kv_event`。事件是「标签 + 字段」的数组，三种合法标签常量定义在 [L13-15](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L13-L15)。
- [omni_apc.c:190-227](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L190-L227)：`parse_kv_event` 按首元素字符串分发到三个解析器，不认识的标 `KV_EVENT_UNKNOWN`。
- [omni_apc.c:92-153](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L92-L153)：`parse_block_stored` 解析 9 字段的 BlockStored（字段下标表在 [L16-25](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L16-L25)）：`block_hashes`（int64 数组，即要插入 radix tree 的链）、`parent_block_hash`、`token_ids`、`block_size`（缺省 128）、`lora_id/medium/lora_name/group_idx` 等。BlockRemoved 是 4 字段版本（[L155-181](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L155-L181)）。内存释放对应 `free_kv_event_batch`（[L301-327](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_apc.c#L301-L327)）。

**语义分派与落树**：

- [ngx_http_omni_proxy_module.c:3643-3771](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3643-L3771)：`omni_proxy_kv_event_handler`。先解析 batch（L3648），持全局锁，用 `handler->index` 定位目标树——**index < 1024（MAX_PREFILL_UPSTREAMS）是 prefill 上游，减去 1024 后是 decode 上游**（L3662-3668），这是「一个 handler 编号空间编码两类上游」的小技巧。随后三分支：
  - `KV_EVENT_BLOCK_STORED` → `omni_radix_tree_add_chain`（[L3697-3708](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3697-L3708)）；
  - `KV_EVENT_BLOCK_REMOVED` → 逐哈希 `omni_radix_tree_remove`（[L3710-3745](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3710-L3745)，删不到只记 debug 日志——事件与本地视图可能滞后，属正常）；
  - `KV_EVENT_ALL_BLOCKS_CLEARED` → `omni_radix_tree_destroy` 后重新 `omni_radix_tree_init`（[L3747-3762](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3747-L3762)），对应推理引擎重启/清缓存的场景。

**监听器初始化与 worker 分片**：

- [ngx_http_omni_proxy_module.c:3773-3871](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3773-L3871)：`omni_proxy_init_kv_listener`。未配置 `omni_proxy_vllm_kv_port_offset` 则直接返回（L3775-3778，即 APC 功能默认关闭）。对每个 ENABLE 的 prefill 上游：仅当 `prefill_cnt % worker_processes == ngx_worker` 时由本 worker 负责订阅（L3790）——**上游按 worker 取模分片，每个上游只被一个 worker 订阅**，既分摊解析开销又避免重复插树；订阅地址为 `ip:(port + vllm_kv_port_offset)`（L3803-3806），随后跑一遍 radix tree 单测再 `omni_zmq_handler_init`（L3818）。decode 上游同理（L3827-3870，handler index 记为 `MAX_PREFILL_UPSTREAMS + i`）。

#### 4.5.4 代码实践

**实践目标**：在一套已启用 APC 的部署上（u1-l4 的环境 + 模板里开启 APC 相关开关），从日志与端口两个侧面确认订阅链路活着。若无环境，则完成源码侧推演。

**操作步骤**：

1. 源码推演（任何环境可做）：对照 inventory 里某台 P 节点的 `node_port`（u1-l3 讲过端口规划），加上模板中 `omni_proxy_vllm_kv_port_offset` 的取值，写出 proxy 将订阅的 `ip:port`。
2. 部署侧观察（需本地环境，**待本地验证**）：在 C 节点容器内 `grep -E "ZMQ handler initialized|Received KV event batch|Removed block hash" <LOG_PATH>/nginx_*.log`，应能看到每个上游一条 `ZMQ handler initialized for <ip:port>, topic:`，以及请求压测期间持续的 `Received KV event batch with N events`。
3. 连发两轮相同前缀的请求，再 grep `APC_MATCHING.*match_depth`，第二轮应出现非零 match_depth，随后调度日志出现 `Prefix cache hit on:`。

**需要观察的现象**：KV 事件日志的到达先于请求的 match_depth 非零——树是事件驱动的，先有 BlockStored 才可能命中。

**预期结果**：能画出「vLLM 广播 → ZMQ SUB（分片到某 worker）→ msgpack 解析 → radix tree 增删 → 下一个请求 match_depth 非零 → 调度器命中路径」的完整时序。

#### 4.5.5 小练习与答案

**练习 1**：为什么 handler 的 `index` 要用「小于 1024 是 prefill、否则减 1024 是 decode」这种编码？
**答案**：让两类上游共用一个回调函数与一条 handler 链表（`local_state.kv_handler_list`），回调内用一次数值比较完成分派（[ngx_http_omni_proxy_module.c:3662-3668](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L3662-L3668)），无需在 handler 结构里加类型字段。1024 正是 `MAX_PREFILL_UPSTREAMS`。

**练习 2**：`BLOCK_REMOVED` 事件的哈希在树里找不到时为什么只记 debug 就放过，而不是报错？
**答案**：事件流与本地视图不保证严格一致——树可能因 `ALL_BLOCKS_CLEARED` 重建过、事件可能乱序或丢失。radix tree 只是调度的**启发式视图**：找不到说明本来就没这条前缀，删操作幂等，无需纠错；真正的正确性由 vLLM 侧按块哈希精确校验兜底。

**练习 3**：三段式消息（topic/seq/payload）里，topic 为空字符串意味着什么？
**答案**：ZMQ SUB 的 topic 是前缀过滤器，空串是任何消息的前缀，因此等价于「订阅全部」。本项目只有一类广播源，无需细分 topic（[omni_zmq_handler.c:187-197](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/omni_zmq_handler.c#L187-L197) 中 topic 取 `ngx_string("")`）。

## 5. 综合实践

**任务**：绘制「新请求进入 → 分配 upstream」的全链路流程图，并标注 radix tree 命中与否的两条分派路径。这是本讲实践任务的完整版。

**要求与步骤**：

1. 以本讲 4.1—4.5 的源码为唯一依据，画出从 worker 收到 HTTP 请求体开始的完整流程，至少包含这些节点与标注：
   - `omni_proxy_begin_tokenization`（[ngx_http_omni_proxy_module.c:342-371](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/modules/ngx_http_omni_proxy_module.c#L342-L371)）→ tokenizer 线程（管道批处理）→ `omni_proxy_post_tokenized`；
   - APC 匹配：对每个上游调 `omni_radix_tree_match_optimistic`，结果写入 `req->prefill_match_depths[]`（标注行号 L246-248、L318-321）；
   - 等待分组 + 1ms 定时器 → 主 worker 加锁调 `omni_proxy_schedule_prefill`（标注 L2500-2511）；
   - 排序：`update_prefill_weights` 的 0.8/0.2 公式（标注 L623-626）；
   - **命中路径**：`best_match > 0` → 选中缓存最深的节点（标注 L788-792）；
   - **未命中路径**：轮询选 `num_tokens` 最小节点（标注 L793-823）；
   - 过载保护分支：两个阈值判断（标注 L773-776）；
   - 记账与相变 → `PHASE_PREFILL_SCHEDULED` → 定时器唤醒发起上游连接（L2521）。
2. 在流程图旁另画一条**数据面支线**：vLLM KV 事件 → ZMQ fd（worker 分片）→ msgpack 解析 → radix tree 增/删/清（标注 4.5 的关键行号），说明它如何持续修正主流程读到的 `match_depths`。
3. 用一个真实数字走一遍图：假设 3 个 prefill 上游，请求块哈希链长 5，三棵树的命中深度分别为 5、2、0，三节点 `num_tokens` 为 9000、100、100，阈值 `max_batch_num_token=8192`——手工推导选中哪个节点。

**参考答案（第 3 步推导）**：上游 0 虽然命中最深（5），但 `load_tokens=9000 > 8192` 触发过载保护被 `continue` 跳过；上游 1 与 2 都通过保护检查，命中深度同为 2（上游 2 为 0），按三级比较「m 大者优先」选中**上游 1**。这正体现了「APC 优先、但过载保护有一票否决权」的设计意图。

**交付物**：一张流程图（任何画图工具或 ASCII 均可）+ 行号标注清单。完成后建议把它与 [README_CN.md:84-96](https://github.com/gitcode.com/ascend-tribe/openPangu-2.0-Infer/blob/2c16b67fb408ddd1ddfd8da855e2bedd5fc23e15/components/omni-proxy/omni_proxy/README_CN.md#L84-L96) 的官方描述对照，检验你对「排序—选择—保护—预测」四层机制的理解是否完整。

## 6. 本讲小结

- **模块结构**：omni_proxy 是「主模块 + 卫星模块」的单向依赖结构；调度由 1ms 定时器、ZMQ 读事件、分词响应管道三条事件源驱动，`ngx_http_omni_proxy_module.c` 是唯一汇合点。
- **共享内存**：`omni_global_state_t` 承载请求池、阶段分组与三类上游状态表；多 worker 通过 `kill(pid,0)` 探活选举唯一主调度器，标量走原子计数、结构修改走 `ngx_shmtx` 锁。
- **请求排序**：prefill 权重 = 0.8×(归一化短 prompt 项) + 0.2×(归一化等待项)，按权重降序派发（`omni_scheduler.c` L623-626）；decode 以「prompt + α·max_tokens」归一化为权重。
- **上游选择**：默认算法「APC 命中深度优先 → token 负载次之 → running 数再次」，未命中则轮询最轻负载；`max_batch_num_token / prefill_max_num_seqs / decode_max_num_seqs` 三道过载保护让超载节点本周期静默出局。
- **radix tree APC**：分词侧按 128 token 链式 sha256 生成块哈希链（PYTHONHASHSEED 作链首），每个上游一棵树（红黑树按节点哈希索引 + 父指针校验边），读路径用 version 号做乐观无锁匹配，命中深度写进 `req->prefill_match_depths` 供调度器消费。
- **ZMQ 订阅**：每个上游一个 SUB socket（按 worker 取模分片），三段式消息经 msgpack 解析成 BlockStored/Removed/Cleared 三类事件，分别落为 radix tree 的 add_chain/remove/重建，形成持续自我修正的全局 KV 缓存视图。

## 7. 下一步学习建议

下一讲 **u6-l3「在 ansible 体系里部署 proxy 与分组调度」** 将回到部署面：本讲读到的 `prefill_group_id` 分组过滤（`omni_scheduler.c:901-904`）、`omni_proxy_vllm_kv_port_offset`、`PYTHONHASHSEED` 等机制，都对应 ansible 模板与 `omni_proxy.sh` 里的具体配置项，届时可把源码认知落到可操作的部署参数上。若想继续深挖源码，建议接着阅读 `ngx_http_omni_proxy_module.c` 中本讲未展开的两块：`omni_pd_body_rewrite.c`（tokenize 结果如何附加进转发请求体，实现「Tokenizer 结果复用」）与 `omni_metrics.c`（十阶段埋点如何变成 access log 字段）；对 KV 事件的生产端感兴趣的话，可回看 u4-l2 讲的 omni-npu connector 与 vLLM 原生 KV 事件广播的关系。
