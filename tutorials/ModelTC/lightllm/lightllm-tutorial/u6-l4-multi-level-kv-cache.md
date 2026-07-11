# 多级 KV Cache（CPU/磁盘）

## 1. 本讲目标

本讲讲解 LightLLM 的「三级 KV Cache」机制：把原本只能放在 GPU 显存里的 KV Cache，扩展成 **GPU（L1）→ CPU 内存（L2）→ 磁盘（L3）** 的层级结构，从而在长上下文、高并发、Prompt Cache 等场景下用更便宜的存储换取更大的缓存容量。

学完后你应该能够：

- 说清 `multi_level_kv_cache` 这个独立进程的职责、触发条件，以及它在请求链路里「插队」的位置。
- 理解 CPU 缓存是如何按「页（page）」组织、用引用计数 + LRU 链表管理的，以及它如何被 GPU 进程零拷贝地共享。
- 理解磁盘缓存工作线程如何借助 LightMem 把 CPU 页落盘、又如何在需要时按最长前缀回填。
- 掌握「卸载（offload，GPU→CPU→磁盘）」与「回填（load，磁盘→CPU→GPU）」两条数据流的真实代码路径。

本讲依赖前置讲义 **u2-l1（多进程架构总览）** 与 **u4-l1（KV Cache 内存管理）**：前者建立「对象放共享内存、线上只传索引」的进程协作观，后者建立「token 级 KV buffer + 索引分配器」的显存地基。本讲就是把同样的「索引 + 大 buffer」思想从 GPU 搬到 CPU、再搬到磁盘。

## 2. 前置知识

### 2.1 为什么需要多级缓存

在 u4-l1 中我们见过：GPU 显存里的 `kv_buffer` 形状为 `(layer_num, size+1, 2*head_num, head_dim)`，能容纳的 token 数（`max_total_token_num`）受显存大小硬性限制。对于：

- 百万 token 级超长上下文；
- 同时缓存大量历史会话的高并发场景；
- 多个用户共享同一段系统 prompt 的 Prompt Cache 场景，

单卡显存远远不够。但 GPU 显存最贵、CPU 内存便宜一个量级、磁盘（尤其 NVMe SSD）又便宜一个量级。于是自然产生一个想法：**把「冷」的 KV Cache 从 GPU 搬到 CPU，再搬到磁盘；下次命中时再搬回来。**

### 2.2 页（page）与 token 哈希

多级缓存不是按单个 token 搬运，而是按「页」搬运。一页 = `cpu_cache_token_page_size` 个连续 token（默认 256，可调 64~512）。每一页用一个 **128 位哈希** 作为唯一身份证：

- 哈希用 `xxhash` 的 `xxh3_128`，采用**滚动累加**——第 i 页的哈希包含了第 0..i 页所有 token 的信息。
- 因此「页哈希序列」天然满足前缀性质：两条 prompt 只要有公共前缀，它们对应的前若干页哈希就完全相同，可以直接按哈希查表复用。

> 为什么「少算最后一个 token」？见 [kv_cache_utils.py:37-58](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/kv_cache_utils.py#L37-L58)：`calcu_num = (len(tokens) - 1) // cpu_cache_token_page_size`。注释解释：如果算满全长，请求会「整条命中」CPU 缓存，导致 prefill 阶段没有任何输入 token 去导出第一个输出，推理链断掉。所以最后一页故意不算，留给 prefill 自己跑。

### 2.3 共享内存与零拷贝回顾

CPU 缓存的大张量并不属于某一个进程，而是放在 **POSIX 共享内存** 里，由一个「主进程」创建（`init_shm_data=True`），其它进程「attach」（`init_shm_data=False`）。所有进程对同一块物理内存直接读写，无需序列化搬运。这与 u2-l3 传递 `Req` 的思路一致。

### 2.4 LRU 与引用计数

CPU/磁盘两级都用 **LRU（最近最少使用）** 决定淘汰谁：一个页只要还被任何请求引用（`ref_count > 0`）就不能被回收；`ref_count` 归零的页回到 LRU 链表尾部，下次分配时从链表头取走复用。

## 3. 本讲源码地图

本讲涉及的核心文件分两类。第一类是**本进程（multi_level_kv_cache）内部**的三个文件，也是讲义规格指定的关键源码：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/multi_level_kv_cache/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py) | 进程主体：收请求、做 CPU/磁盘前缀匹配、回填页面索引、转发给 router |
| [lightllm/server/multi_level_kv_cache/cpu_cache_client.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py) | CPU 缓存「客户端」：页面分配/回收、状态机、引用计数、LRU、落盘候选登记 |
| [lightllm/server/multi_level_kv_cache/disk_cache_worker.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py) | 磁盘缓存工作线程：基于 LightMem 把 CPU 页落盘 / 从磁盘读回 |

第二类是**与本进程协作的外部代码**，用来把数据流串成闭环：

| 文件 | 作用 |
| --- | --- |
| [lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py) | GPU 推理后端里的同名模块：负责把 KV 从 GPU 卸载到 CPU（offload）、把 CPU 缓存回填到 GPU（load） |
| [lightllm/common/kv_cache_mem_manager/operator/normal.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py) | 真正执行页级 GPU↔CPU 拷贝的 Triton kernel 封装 |
| [lightllm/utils/kv_cache_utils.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/kv_cache_utils.py) | 计算 token 页哈希、推算 CPU 缓存页数与张量形状 |
| [lightllm/server/httpserver/manager.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py) | 请求分发：决定请求先发给本进程而不是直接发 router |

> 注意有两个名字相近的类：进程内的 `MultiLevelKVCacheManager`（本讲主角，做匹配）和 GPU 后端内的 `MultiLevelKvCacheModule`（做实际数据搬运）。前者算「查哪些页命中」，后者算「把命中的页搬进/搬出 GPU」，两者通过共享内存里的 `Req.cpu_cache_match_page_indexes` 字段通信。

## 4. 核心概念与源码讲解

### 4.1 多级缓存总体架构

#### 4.1.1 概念说明

LightLLM 的三级缓存遵循一个明确的设计取舍：

- **L1 = GPU 显存**（u4-l1 的 `kv_buffer`）：最快、最贵、容量最小，存「热」请求的 KV。
- **L2 = CPU 内存**（本讲的 `cpu_kv_cache_tensor`）：中速、较便宜，存「温」KV。官方文档强调：CPU 缓存放的是 **GPU 缓存的一份完整备份**，而不是「放不下的溢出部分」——这样命中后可以直接整段复用。
- **L3 = 磁盘**（LightMem 管理）：最慢、最便宜、容量最大，存「冷」KV。

查询时按 **最长前缀** 串联三级：先在 GPU 的 RadixCache（u4-l2）查前缀，没命中的部分去 CPU 查，再没命中的去磁盘查，最后才真正 prefill 剩余 token。三级各自独立做 LRU 淘汰。

这套机制由一个独立进程 `multi_level_kv_cache` 承担「匹配」职责，它在请求链路里**插在 HttpServer 与 Router 之间**：

```
HttpServer ──PUSH──> multi_level_kv_cache(匹配CPU/磁盘前缀) ──PUSH──> Router ──rpyc──> ModelBackend(prefill时回填)
```

为什么要把匹配独立成进程？因为磁盘 IO 很慢，若放在 Router 的主循环里会拖垮整个调度（u2-l5 的 30ms 心拍容不下磁盘读）。独立进程 + 大线程池 + 超时兜底，把慢 IO 隔离在调度循环之外。

#### 4.1.2 核心流程

一次请求在多级缓存里的完整生命周期：

```text
【写入/卸载方向 offload：GPU → CPU → 磁盘】
请求在 GPU 上跑完(finished)
  └─ backend: offload_finished_reqs_to_cpu_cache
       └─ allocate CPU 页 + Triton kernel: GPU kv_buffer → CPU 页(独立 cuda stream)
       └─ 页标记 READY，加入 offload_page_indexes 落盘候选(若 prompt 足够长)
            └─ DiskCacheWorker.run: 取候选 → LightMem 落盘 → deref 页

【读取/回填方向 load：磁盘 → CPU → GPU】
新请求到达 HttpServer，预计算 token_hash_list
  └─ HttpServer PUSH 给 multi_level_kv_cache 进程
       └─ _cpu_cache_match: 按页哈希查 CPU 命中前缀
       └─ _disk_cache_match: 命中前缀之后，查磁盘可加载长度，落盘页读回 CPU
       └─ 把命中页索引写进 Req.cpu_cache_match_page_indexes(共享内存)
       └─ PUSH 转发给 Router
            └─ backend prefill: load_cpu_cache_to_reqs
                 └─ Triton kernel: CPU 页 → GPU kv_buffer，跳过这段 prefill
```

#### 4.1.3 源码精读

进程入口在 [manager.py:248-265](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L248-L265)：它注册优雅退出、设置进程名、向父进程 `Pipe` 回送 `"init ok"`（这是 u1-l5 提到的启动握手），最后进入 `recv_loop()` 阻塞。进程名形如 `lightllm::<server_name>::multi_level_kv_cache`。

这个进程何时被拉起？在 [api_start.py:468-476](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L468-L476)，仅当 `--enable_cpu_cache` 时才启动它；且它必须在 metric/router/detokenization 之前就绪，因为 router 一旦开始收请求就可能需要读它写好的命中信息。CPU 缓存的共享内存 id 在 [api_start.py:113-115](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L113-L115) 由一个 UUID 派生，保证同机多实例不冲突。

HttpServer 的分发逻辑在 [httpserver/manager.py:640-645](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L640-L645)：纯文本（非多模态）模型 + 开启 cpu cache 时，请求优先 PUSH 给本进程，而非直接给 router。注意第 95 行的开关是 `enable_cpu_cache and not enable_multimodal`——多模态请求要先走 visualserver 算图像嵌入，所以本进程只接纯文本。

#### 4.1.4 代码实践

1. **实践目标**：在源码里确认「请求是否绕过本进程直送 router」的两条分支。
2. **操作步骤**：打开 [httpserver/manager.py:626-662](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L626-L662) 的 `transfer_to_next_module`，依次列出 visual → audio → multi_level_kv_cache → router 四个 `if` 分支的条件。
3. **需要观察的现象**：四个分支是互斥的 `return`，请求只会走其中一条。
4. **预期结果**：当你不传 `--enable_cpu_cache` 时，纯文本请求走最后一条 `send_to_router`；传了之后走第三条 `send_to_multi_level_kv_cache`。多模态请求（无论是否开 cpu cache）走第一条 visual 分支，本进程不参与。

#### 4.1.5 小练习与答案

**练习 1**：为什么 CPU 缓存要做成「GPU 缓存的完整备份」，而不是「放不下的溢出区」？

> **参考答案**：因为命中复用要求「页的 KV 内容与请求当前需要的 KV 完全一致」。如果 CPU 只存溢出部分，命中逻辑就要在 GPU 与 CPU 之间做复杂的分段拼接；做成完整备份后，一段公共前缀要么整段在 CPU 可直接复用、要么不在，匹配退化为简单的「最长公共前缀查表」，实现简单且便于按页哈希去重。

**练习 2**：本进程在请求链路里「插队」，会不会拖慢没有命中任何缓存的冷请求？

> **参考答案**：会有一点延迟，但有兜底。本进程用 `cpu_cache_time_out = 0.5`（[manager.py:44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L44)）作为匹配的软超时：若任务排队/匹配已超过 0.5s，直接放弃匹配转发给 router（见 4.4.3）。CPU 匹配本身只是几次字典查询，通常远低于该阈值；磁盘匹配才会逼近它。

### 4.2 CPU 缓存客户端与页面状态管理（CPU 卸载）

#### 4.2.1 概念说明

`CpuKvCacheClient` 是 CPU 缓存的「元数据管家」。注意它叫 *Client* 却不是网络客户端——它管理的是**本地共享内存**里的页面元数据（哪些页空闲、哪些页存了哪个哈希、被引用几次）。它的「数据体」是一块巨大的 CPU 张量 `cpu_kv_cache_tensor`，形状是：

```
(page_num, layer_num, token_page_size, num_heads, head_dim)
```

即「第几个页 × 第几层 × 页内第几个 token × 几个头 × 头维度」。一页正好容纳 `token_page_size` 个 token 在**所有层**上的完整 KV。

每个页槽位对应一个 `_CpuPageStatus` 元数据记录，构成一个三态状态机：

| 状态 | 含义 |
| --- | --- |
| `EMPTY` (0) | 空闲，可分配 |
| `LOADING` (1) | 正在被写入（GPU→CPU 或 磁盘→CPU） |
| `READY` (2) | 数据就绪，可被读取/复用 |

#### 4.2.2 核心流程

CPU 页面的分配与回收遵循「引用计数 + LRU 链表」：

```text
分配 allocate_pages(hash_keys):
  对每个 hash_key:
    若该哈希已在 page_hash_dict 中(命中已有页):
      ref_count += 1, 从 LRU 摘出; 返回 (page_index, ready=是否READY)
    否则(新页):
      get_one_empty_page: 从 LRU 头取一个 can_realloc 的页(EMPTY或READY且ref==0)
      标记 LOADING, ref_count=1, 登记到 hash_dict; 返回 (page_index, ready=False)

回收 deref_pages(page_list):
  对每个页: ref_count -= 1
  若 ref_count==0: 放回 LRU 尾部(等下次被复用或淘汰)
```

关键点：**分配返回的是页索引（整数）**，真正的 KV 数据靠这个索引去 `cpu_kv_cache_tensor[page_index]` 切片得到——这正是 u4-l1「索引 + 大 buffer」思想在 CPU 上的复刻。

#### 4.2.3 源码精读

CPU 张量由谁创建？见 [cpu_cache_client.py:18-44](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L18-L44)：构造时 `CpuCacheCreator.create_or_attach(init_shm_data=init_shm_data, pin=not init_shm_data)`。本进程（`MultiLevelKVCacheManager`）用 `init_shm_data=True` 创建并清零；GPU 后端进程用 `init_shm_data=False` 仅 attach，且 `pin=True` 把内存注册为 pinned（锁页内存），加速 GPU↔CPU 拷贝。两者靠同一个 `args.cpu_kv_cache_shm_id` 找到同一块共享内存。

页数与张量形状由 [kv_cache_utils.py:61-136](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/kv_cache_utils.py#L61-L136) 的 `calcu_cpu_cache_meta()` 推算：根据模型类型（普通 MHA / Deepseek2 MLA / INT8 量化 / linear att）算出一页的字节数，再用 `cpu_cache_storage_size(GB) / 一页字节数` 得到 `page_num`（[kv_cache_utils.py:129-132](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/kv_cache_utils.py#L129-L132)）。文档的经验值是「每 2GB ≈ 10K token」。

页状态机的定义在 [cpu_cache_client.py:289-339](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L289-L339)。`hash_key` 用两个 `uint64` 拼成 128 位（[cpu_cache_client.py:312-321](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L312-L321)）。`can_realloc` 只允许复用 `EMPTY` 或 `READY` 且 `ref_count==0` 的页（[cpu_cache_client.py:338-339](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L338-L339)）——这保证不会踢掉正在被读写的页。

`update_pages_status_to_ready`（[cpu_cache_client.py:113-167](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L113-L167)）承担三件事：把 LOADING 页升为 READY、按需 `deref` 减引用、以及登记「落盘候选」。注意第 152-166 行：只有当 `disk_offload_enable` 且 prompt 长度 ≥ `LIGHTLLM_DISK_CACHE_PROMPT_LIMIT_LENGTH`（默认 2048，[envs_utils.py:225-226](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/utils/envs_utils.py#L225-L226)）时才加入 `offload_page_indexes`，并且**先给候选页加一次引用计数**（第 156-161 行），等落盘成功后由 `DiskCacheWorker` 再 `deref`。这个「落盘期间加引用」是关键：防止页还在往磁盘写，就被 LRU 淘汰给别人用了。候选索引用「`group_size` + 后续若干 `page_index`」的分组编码写入，便于磁盘工作线程按请求分组落盘（第 165-166 行注释明确说明了这点）。

#### 4.2.4 代码实践

1. **实践目标**：理解「页命中复用」与「新分配」两条路径如何用 `ready_list` 区分。
2. **操作步骤**：阅读 [cpu_cache_client.py:69-111](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/cpu_cache_client.py#L69-L111) 的 `allocate_pages` → `allocate_one_page`。
3. **需要观察的现象**：返回值是 `(page_list, ready_list)` 二元组。对命中的页，`ready` 取决于该页是否 `is_data_ready()`；对新分配的页，`ready` 恒为 `False`。
4. **预期结果**：GPU 后端拿到 `ready_list` 后，只需对 `ready=False` 的页执行真正的 GPU→CPU 拷贝，`ready=True` 的页因为别人已经写过、直接共享即可——这正是「写时复制」式的去重。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update_pages_status_to_ready` 在登记落盘候选时要「先加引用、落盘后再减」？

> **参考答案**：落盘是异步慢操作。如果不加引用，候选页可能在落盘完成前 `ref_count` 归零、被 LRU 回收并分配给新请求覆写，导致磁盘上写入了错误/混合的数据。加一次引用相当于给「正在落盘的页」上一把锁，落盘完成后由 `DiskCacheWorker._persist_pages_to_disk` 调 `deref_pages` 解锁。

**练习 2**：`_CpuPageStatus` 里 `status` 字段用的是 `>=` 比较（`is_data_ready` 返回 `status >= READY`），而不是 `==`。这暗示了什么？

> **参考答案**：暗示未来可能引入比 `READY` 更高的状态（例如某种「已落盘且内存可回收」的状态），届时 `>= READY` 的页都算「数据可用」。这是一种向前兼容的状态机设计。

### 4.3 磁盘缓存工作线程（磁盘卸载）

#### 4.3.1 概念说明

磁盘层由 `DiskCacheWorker` 负责，但它**并不自己实现磁盘 IO**——真正的读写交给外部库 **LightMem**（`PyLocalCacheService`）。LightMem 是 ModelTC 另一个高性能 KV Cache 磁盘管理库，专门为大模型推理设计，内部用多 shard、多 worker 线程榨干 NVMe SSD 的并发带宽。本讲只把它当黑盒：给它「页索引 + 哈希」，它负责把 CPU 页内容落盘或读回。

磁盘工作线程是一个 daemon 线程，在 [manager.py:51-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L51-L60) 仅当 `--enable_disk_cache` 时启动。**只用 L1+L2 两级时不启动它，也不需要装 LightMem**。

#### 4.3.2 核心流程

落盘与回填是两条独立的循环/调用：

```text
【落盘 run() 主循环】
每 0.1s:
  _gather_offload_payloads: 加锁 pop offload_page_indexes, 解析成 [[page_index...], ...] 分组
  对每组: _persist_pages_to_disk
    query(哈希): 哪些页磁盘上已有?
      已有的: 立即 deref(不必重写)
      缺失的: create(mode="w") 落盘任务
        限流: 写线程≥16 且有读线程时 sleep 让资源
        等 task.data_safe()(数据已拷到内部缓冲即可, 不必等真正 flush 完)
        deref 剩余页

【回填 load_pages() 由匹配逻辑调用】
create(mode="r", start_pos): 异步读任务
  等 task.ready()
  返回是否全部 Finished
```

一个关键设计：**写任务在「数据安全」即可返回，无需等磁盘真正写完**（[disk_cache_worker.py:136-138](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L136-L138)）。这是因为 LightMem 内部把数据拷进自己的缓冲后，CPU 页就可以被覆写/回收了，真正的落盘由它后台慢慢做。

#### 4.3.3 源码精读

构造见 [disk_cache_worker.py:34-76](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L34-L76)。几个硬编码参数值得注意：

- `num_shard=32`、`num_worker=48`：注释（[disk_cache_worker.py:45-47](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L45-L47)）说在 `KVCACHE_MAX_BLOCK_SIZE=64MB` 前提下，32 shard 能让磁盘容量利用率达 90%，再大反而下降。
- `max_concurrent_write_tasks=16`：读写并发时，写最多占 16 线程，把更多资源留给读（[disk_cache_worker.py:49-50](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L49-L50)）。因为读（回填）在请求关键路径上，写（落盘）不在。
- 磁盘目录默认用临时目录 + server 名隔离，推荐用 NVMe SSD（[disk_cache_worker.py:52-56](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L52-L56)）。

`_prepare_tensor`（[disk_cache_worker.py:78-79](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L78-L79)）把 CPU 缓存张量 `flatten(1).view(uint8)` 摊平成「页 × 字节」的连续视图交给 LightMem——LightMem 按页偏移直接定位字节，无需理解 KV 的语义结构。

回填时的「最长可加载前缀」逻辑在 [disk_cache_worker.py:151-168](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L151-L168) 的 `query_loadable_pages`：它对每页哈希 `query` 得到一个布尔数组，找到从 `start_pos` 起第一个「磁盘上不存在」的页，返回它之前的连续命中长度。这保证回填只读「连续命中」的前缀，不会读半截。

#### 4.3.4 代码实践

1. **实践目标**：理解落盘任务的「去重」行为。
2. **操作步骤**：阅读 [disk_cache_worker.py:112-149](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L112-L149) 的 `_persist_pages_to_disk`。
3. **需要观察的现象**：第 121 行先 `query(hashs)` 检查磁盘是否已有这些页；第 122 行 `if not all(query_result)` 分支处理「部分缺失」；第 131-134 行对 `task.page_already_list`（磁盘上已有的页）**立即 deref 而不重写**。
4. **预期结果**：若两条 prompt 共享某段前缀，第二段请求落盘时该前缀页已被第一段写过了，第二段不会重复写磁盘，只是把引用减掉。这就是磁盘层的去重复用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `load_pages`（回填）的注释里有一段被注释掉的「写线程忙就跳过」逻辑（[disk_cache_worker.py:177-180](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L177-L180)）？

> **参考答案**：作者曾考虑「磁盘正在大量写入时，跳过本次回填请求」以保护读延迟，但最终注释掉不用。结合 4.3.2 的 `max_concurrent_write_tasks=16` 限流，已经从「写线程数量」上保证了读有足够带宽，不需要再在请求层硬跳过。保留注释是为了说明这个权衡曾被考虑过。

**练习 2**：磁盘缓存页大小（`service._n`）与 CPU 缓存页大小（`cpu_cache_token_page_size`）是什么关系？

> **参考答案**：它们是两层各自的「块粒度」。回填时必须按磁盘 block 边界对齐——见 [manager.py:117-120](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L117-L120)，`load_start_pos` 被向下对齐到 `block_size` 的整数倍，否则跨 block 的部分页无法从磁盘完整恢复。

### 4.4 回填机制：从 CPU/磁盘加载回 GPU

#### 4.4.1 概念说明

「回填（load）」是命中缓存后真正产生收益的动作：把命中的页内容从 CPU（必要时先从磁盘读进 CPU）拷回 GPU 的 `kv_buffer`，让 prefill 跳过这段 token 的计算。注意分工：

- **`MultiLevelKVCacheManager`（本进程）**：算出「命中了哪些页」，把页索引写进 `Req.cpu_cache_match_page_indexes`。
- **`MultiLevelKvCacheModule`（GPU 后端进程）**：在 prefill 前，读出这些页索引，执行真正的 CPU→GPU 拷贝。

#### 4.4.2 核心流程

GPU 后端在 prefill 一个请求前的回填流程（[multi_level_kv_cache.py:61-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py)）：

```text
对每个 req:
  读 page_list = req.cpu_cache_match_page_indexes
  match_tokens = 该请求命中的总 token 数(按页长度累加)
  从 match_tokens 中扣除: 已被 GPU RadixCache 命中的部分(cur_kv_len) + 磁盘命中部分
       => 得到真正需要从 CPU 拷回 GPU 的 cpu_prompt_cache_len
  need_token_num = match_tokens - cur_kv_len
  若 need_token_num >= 128 且 input_len >= 256(太短不值得拷):
    alloc GPU 显存槽位 mem_indexes
    load_cpu_cache_to_gpu(mem_indexes, page_indexes, ...)  # Triton kernel
    更新 req_to_token_indexs 映射, req.cur_kv_len += need_token_num
  deref 这些页(本次匹配用完了)
```

阈值 `need_token_num >= 128 and input_len >= 256`（[multi_level_kv_cache.py:84](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py#L84)）是个收益判断：拷贝本身有开销，命中太短时不如直接重算。

#### 4.4.3 源码精读

回填的匹配编排是本进程的核心，在 [manager.py:138-210](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L138-L210) 的 `_handle_group_req_multi_cache_match`。流程：

1. **超时兜底**（第 143-150 行）：若距任务提交已超 0.5s，直接转发不匹配。
2. **CPU 前缀匹配**（第 173 行）`_cpu_cache_match`：逐页哈希查 `query_one_page`，断在第一个未命中页，得到最长 CPU 命中前缀。
3. **磁盘匹配**（第 176-178 行）：仅当 CPU 没全命中且磁盘开启时，`_disk_cache_match` 把磁盘上连续命中的页读回 CPU（见 4.3），并 append 到命中列表。
4. **磁盘命中长度核算**（第 180-199 行）：用 `token_hash_page_len_list` 把「页数」换算回「token 数」，分离出 `disk_prompt_cache_len`（最终给 HttpServer 做统计用）。
5. **等数据就绪**（第 201 行）：`check_allpages_ready` 轮询，因为磁盘回填是异步的，要等页都变 READY。
6. **写回共享内存**（第 204 行）：`req.cpu_cache_match_page_indexes.fill(finded_page_indexes)`——GPU 后端会读这个字段。
7. **转发**（第 209 行）：PUSH 给 router，请求正式进入调度。

> 这里有一个精妙的「三段命中长度」记账：一个请求的 prompt 命中可能同时来自 GPU RadixCache（`cur_kv_len`）、CPU（`cpu_prompt_cache_len`）、磁盘（`disk_prompt_cache_len`）。HttpServer 最后会把三者相加作为 `prompt_cache_len` 上报给用户（见 [httpserver/manager.py:710-712](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L710-L712)），并在日志里分别打印 cpu/disk 是否命中。

GPU 侧的实际拷贝在 [multi_level_kv_cache.py:130-135](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py#L130-L135)，调用 `mem_manager.operator.load_cpu_cache_to_gpu`；其 Triton kernel 封装在 [normal.py:27-53](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py#L27-L53)，底层 `load_cpu_kv_to_gpu` 按 `page_indexes` 把 CPU 页拷进 GPU 的 `kv_buffer`，并按 TP rank 切分（每个 rank 只拷自己那一片）。

#### 4.4.4 代码实践

1. **实践目标**：追踪一次「命中磁盘」请求的完整回填调用链。
2. **操作步骤**：从 [manager.py:88-136](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L88-L136) `_disk_cache_match` 出发：`query_loadable_pages` → `allocate_pages`（在 CPU 侧先占好页槽）→ `disk_cache_worker.load_pages`（磁盘→CPU 页）→ `update_pages_status_to_ready`（页升 READY）。再跳到 GPU 侧 [multi_level_kv_cache.py:61-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py#L61-L150) 看 `load_cpu_cache_to_reqs` 如何把这些页拷进 GPU。
3. **需要观察的现象**：磁盘回填先在 CPU 侧「占位」（LOADING），再异步读磁盘填进去，最后才升 READY；GPU 侧必须 `check_allpages_ready` 通过后才拷。
4. **预期结果**：磁盘命中不会阻塞 CPU 命中部分——CPU 已 READY 的页可以和「磁盘正在读」的页在同一次匹配里并存，只是 GPU 拷贝要等全部就绪。

#### 4.4.5 小练习与答案

**练习 1**：`disk_prompt_cache_len` 是怎么从「页数」换算成「token 数」的？

> **参考答案**：通过 `token_hash_page_len_list`（[req.py:125](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/req.py#L125)）。它存的是「截至第 i 页的累计 token 数」前缀和。在 [manager.py:189-194](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L189-L194)，用总命中页数对应的累计长度，减去 CPU 命中页数对应的累计长度，差值即磁盘命中 token 数。之所以用前缀和而非「页数×页大小」，是因为最后一个页可能是没填满的「碎页」。

**练习 2**：为什么 GPU 侧的回填有 `need_token_num >= 128` 的阈值？低于它会怎样？

> **参考答案**：低于阈值就不做 CPU→GPU 拷贝，直接 prefill 这段 token。因为页级拷贝要走 PCI-e、要 alloc 显存、要更新映射表，固定开销不低；命中太短（如几十 token）时直接重算反而更快。这是一种「拷贝 vs 重算」的收益切换。

### 4.5 multi_level_kv_cache 进程的匹配编排主循环

#### 4.5.1 概念说明

把前面三个模块串起来的是本进程的主循环设计。它采用经典的 **「接收线程 → 有界队列 → 工作线程池」** 模型，目的是把慢且可并发的匹配/磁盘 IO 与 zmq 接收解耦，并控制对 Router 的反压。

#### 4.5.2 核心流程

```text
zmq PULL socket (bind 多级缓存端口)
   │ recv_loop(): 批量取(最多128~256个) → recv_queue(容量1024)
   ▼
recv_queue
   │ cpu_cache_hanle_loop()(daemon 线程): 取一个 → executor.submit(...)
   ▼
ThreadPoolExecutor(6 或 500 workers)
   │ _handle_group_req_multi_cache_match(group_req, start_time):
   │    超时? → 转发不匹配
   │    否则 CPU 匹配 (+磁盘匹配) → 写 cpu_cache_match_page_indexes → PUSH 给 router
   ▼
zmq PUSH socket (connect router 端口)
```

线程池大小是关键差异（[manager.py:42](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L42)）：只用 CPU 缓存时 6 个线程够（内存查询极快）；开了磁盘缓存时 500 个线程——因为 NVMe SSD 需要**大量并发**才能打满带宽，注释明确写了「磁盘io在NVMe SSD上需要大量并发才能发挥性能」。

#### 4.5.3 源码精读

`recv_loop`（[manager.py:212-245](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L212-L245)）做了**自适应批量接收**：队列积压时把一次接收量上调到 256（×1.3），队列快空时回落到 128（第 231-235 行）。这避免 zmq 队列积压时单次取太少导致主循环空转。

`cpu_cache_hanle_loop`（[manager.py:63-70](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L63-L70)）是 daemon 线程，每次从 `recv_queue` 取一个 `GroupReqIndexes` 提交到线程池，并把提交时刻 `time.time()` 作为 `start_time` 传入——这个时刻是 4.4.3 超时判断的基准。

`diverse_mode` 的特殊处理在 [manager.py:157-159](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L157-L159)：`diverse_mode` 下一个 group 里只有主请求（`request_id == group_req_id`）做缓存匹配，其余采样分支不重复做——因为它们的 prompt 相同，匹配结果可以共享。

#### 4.5.4 代码实践

1. **实践目标**：用真实的启动脚本验证三级缓存的参数组合。
2. **操作步骤**：阅读官方提供的两级缓存启动脚本 [test/start_scripts/single_node_tp_cpu_cache_enable.sh](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/test/start_scripts/single_node_tp_cpu_cache_enable.sh)，它对 Qwen3-8B 用了 `--enable_cpu_cache --cpu_cache_storage_size 66 --cpu_cache_token_page_size 128`。再对照部署文档 [docs/EN/source/tutorial/multi_level_cache_deployment.rst](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/docs/EN/source/tutorial/multi_level_cache_deployment.rst) 的三级缓存命令（多加 `--enable_disk_cache --disk_cache_storage_size 1000 --disk_cache_dir /mnt/ssd/...`）。
3. **需要观察的现象**：两级命令没有 `--enable_disk_cache`，因此本进程的 `DiskCacheWorker` 不会被创建（[manager.py:51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L51) 的 `if` 不进），线程池只有 6 个 worker，也无需安装 LightMem。三级命令才会触发磁盘工作线程和 500 worker。
4. **预期结果**：如果你本地有 GPU 并能拉到 Qwen3-8B，可用两级脚本启动一次（待本地验证），向 `/generate` 发两次**相同前缀**的长 prompt，第二次请求的响应日志里应出现 `cpu cache hit: True` 与非零的 `cpu_prompt_cache_len`（见 [httpserver/manager.py:757-761](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L757-L761) 的日志格式）。若无法本地运行，则止步于源码阅读与参数对照，明确标注「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`recv_queue` 的容量是 1024（[manager.py:45](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L45)）。当队列满时会发生什么？这对上游 HttpServer 意味着什么？

> **参考答案**：`recv_queue.put` 会阻塞 `recv_loop`，进而导致 zmq PULL socket 不再消费，最终 HttpServer 端的 PUSH socket 因高水位（SNDHWM）阻塞 `send_to_multi_level_kv_cache.send_pyobj`。这是一种自然的**反压（backpressure）**：当多级缓存处理不过来时，压力沿着 zmq 链路回传到 HttpServer，让它放慢接收新请求的节奏。

**练习 2**：为什么 `cpu_cache_hanle_loop` 用 daemon 线程 + `ThreadPoolExecutor`，而不是直接在 `recv_loop` 里同步处理？

> **参考答案**：同步处理会让一个慢请求（尤其是磁盘回填）阻塞后续所有请求的接收，丢失并发。拆成「接收 → 队列 → 线程池」后，接收始终顺畅，多个请求的匹配/磁盘 IO 可以在线程池里并行（磁盘缓存时最多 500 路并发），整体吞吐由线程池并发度而非单请求延迟决定。

## 5. 综合实践

**任务**：在源码里画出「一个长 prompt 第二次到达时，KV 是如何穿越三级缓存被复用的」全链路时序图，并解释每一级用到的关键数据结构与阈值。

具体步骤：

1. **写入侧**：假设请求 A（长 prompt）刚跑完。从 [multi_level_kv_cache.py:152-206](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py#L152-L206) `offload_finished_reqs_to_cpu_cache` 出发，标注：
   - 谁分配 CPU 页、谁触发 Triton kernel `offload_gpu_kv_to_cpu`（[normal.py:55-85](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/kv_cache_mem_manager/operator/normal.py#L55-L85)）；
   - 页何时从 LOADING→READY；何时被登记为落盘候选（需要 prompt ≥ 多少 token？）。
2. **落盘侧**：从 [disk_cache_worker.py:81-90](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/disk_cache_worker.py#L81-L90) `run` 出发，标注 `_gather_offload_payloads` 如何按分组取出候选、`_persist_pages_to_disk` 如何去重落盘、何时 `deref`。
3. **读取侧**：假设请求 B 与 A 共享前缀到达。从 [manager.py:138-210](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multi_level_kv_cache/manager.py#L138-L210) 出发，标注 CPU 匹配命中哪些页、若开了磁盘还读回哪些页、`cpu_cache_match_page_indexes` 如何被 GPU 侧 [multi_level_kv_cache.py:61-150](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/router/model_infer/mode_backend/multi_level_kv_cache.py#L61-L150) 读出并 `load_cpu_cache_to_gpu`。
4. **产物**：一张包含「进程名 → 动作 → 关键阈值」的时序图。例如标注「GPU→CPU 拷贝在独立 cuda stream」「回填阈值 `need_token_num≥128`」「落盘阈值 `prompt≥2048`」「超时 0.5s 兜底」等。

如果你本地具备多卡 GPU 与一块 NVMe SSD，可进一步用三级缓存命令启动 Qwen3-8B（参考部署文档），发两次共享前缀的请求，从 HttpServer 日志验证 `cpu cache hit` / `disk cache hit` 是否如预期翻转。**实际运行结果待本地验证**。

## 6. 本讲小结

- LightLLM 的 KV Cache 是三级层次：GPU（L1，u4-l1 的 `kv_buffer`）→ CPU 内存（L2，共享内存大张量）→ 磁盘（L3，LightMem 管理），三级各自 LRU，查询按最长前缀串联。
- 一个独立进程 `multi_level_kv_cache` 插在 HttpServer 与 Router 之间，专职做 CPU/磁盘前缀匹配；它把慢磁盘 IO 用「接收线程 + 队列 + 大线程池（磁盘模式 500 workers）+ 0.5s 超时兜底」隔离在调度循环之外。
- CPU 缓存按「页」管理：一页 = `cpu_cache_token_page_size` 个 token，用滚动 xxh3_128 哈希做身份证；`_CpuPageStatus` 三态机（EMPTY/LOADING/READY）+ 引用计数 + LRU 链表决定分配与淘汰，返回的是页索引而非数据。
- 磁盘层由 `DiskCacheWorker` daemon 线程承担，真正的 IO 交给 LightMem；落盘在「数据安全」即可返回并 `deref`，写入有限流（最多 16 线程）把带宽留给读；回填按磁盘 block 边界对齐取最长连续命中前缀。
- 卸载（offload）与回填（load）是两条对称数据流：offload 由 GPU 后端在请求结束时触发，经独立 cuda stream 把 GPU 页拷到 CPU、再异步落盘；load 在 prefill 前把命中页（必要时先磁盘→CPU）拷回 GPU，设有 `need_token_num≥128` 的拷贝/重算收益阈值。
- 三段命中长度（GPU `cur_kv_len` / CPU `cpu_prompt_cache_len` / 磁盘 `disk_prompt_cache_len`）分别记账，最终在 HttpServer 汇总上报。

## 7. 下一步学习建议

- **接续 PD 分离**：本讲的 CPU/磁盘缓存是「单机内」的层级；当 prefill 与 decode 分到不同节点时，KV 需要跨节点迁移，那是 u7-l1（PD 分离部署与 KV 迁移）的主题，与本讲共享「KV 是可搬运的索引化数据」这一前提。
- **回顾量化缓存**：本讲的 CPU 张量 `data_type` 会随模型 `--llm_kv_type` 变化（INT8/FP8 时带 scale 维，见 `calcu_cpu_cache_meta` 的分支），可与 u6-l3（FP8 KV Cache 量化）对照阅读，理解量化与多级缓存如何叠加。
- **动手扩展阅读**：若想理解「页级 GPU↔CPU 拷贝」的 Triton kernel 细节，可读 [lightllm/common/basemodel/triton_kernel/kv_cache_offload.py](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/common/basemodel/triton_kernel/kv_cache_offload.py) 中的 `offload_gpu_kv_to_cpu` / `load_cpu_kv_to_gpu`，理解它如何按 `page_indexes` 和 TP rank 切分拷贝。
- **linear att 特例**：Qwen3.5 等 linear attention 混合模型的 CPU 缓存形状与碎页处理与普通 MHA 不同（`_handle_linear_att_last_page`），若你关注这类模型，建议结合 u5-l5 与 `calcu_cpu_cache_meta` 中 `Qwen3NextMemManager` 分支深入。
```
