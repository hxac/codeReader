# SSD 流式推理路径与延迟隐藏

## 1. 本讲目标

u9-l1 讲清了 SSD 流式模式的**缓存规划层**：怎样把 NGB 预算换算成「能驻留多少个完整 routed 专家」。本讲往下走一层，回答规划好之后**推理到底怎么跑**：当一个被路由选中的专家不在内存缓存里（cache miss），ds4 从哪里读它、什么时候读、读它的那段磁盘延迟怎么藏起来。读完本讲，你应该能够：

1. 描述 routed 专家 **cache miss 时的按需读取路径**——为什么是显式分配缓冲、从 mmap 的 GGUF 里 `pread`（或触发内核预读），而不是像普通权重那样整体常驻。
2. 讲清两套**延迟隐藏**机制：生成（decode）时把缺失专家的加载**重叠在 shared expert 推理背后**；prefill 时把**下一层的加载重叠在当前层计算背后**。
3. 解释**预取（prefetch）与 hotlist**：启动时基于受欢迎度（popularity）预热哪些专家、为什么自动预热要封顶 4096、`--ssd-streaming-cold` / `--ssd-streaming-preload-experts` 各自绕过哪一步。
4. 回答本讲的核心实践问题：**为什么生成（generation）比 prefill 对 cache miss 更敏感**，并能把这条结论与 prefill「一批 token 覆盖几乎所有专家」、decode「一个 token 只点 6 个专家」的事实对上号。

本讲只讲 SSD 流式的**运行时推理路径**（读、重叠、预热）。它不重复 u9-l1 的预算数学，也不展开 expert 缓存的 slab 分配器内部结构（那是后端 `ds4_metal.m`/`ds4_cuda.cu` 的实现细节，留待结合 u5-l2/u5-l3）。

## 2. 前置知识

进入运行时路径前，先用几句话回收几个前置概念（细节见对应讲义）：

- **routed vs shared 专家**：DeepSeek V4 每层有 256 个 routed 专家，router 用 Top-k 选 6 个（`DS4_N_EXPERT_USED=6`）激活；另有 1 个 shared 专家**永远激活**（u4-l1）。SSD 流式只把 routed 专家放进按需缓存，shared 专家和所有投影权重始终常驻——它们体积小且每次必用。
- **非对称量化只压 routed 专家**：routed 专家是模型体积的大头（gate/up=IQ2_XXS、down=Q2_K），所以「只把它们做按需」收益最大；其余保持高精度常驻（u3-l4）。
- **expert 缓存以「完整专家个数」为单位**：缓存预算不是字节，而是「能放下几个完整 expert」；每个 expert 是 gate+up+down 三段张量（u9-l1）。
- **tensor-resident 执行模型**：激活、KV、scratch 一旦在 GPU 分配就全程留在设备，算子间用设备指针交接；命令缓冲有 `begin_commands`/`flush_commands`/`end_commands`/`synchronize` 生命周期（u5-l1、u5-l2）。`flush`「提交一批、再开一批」正是本讲重叠机制的基础。
- **prefill vs decode**：prefill 一次性把整段 prompt 填进 KV 缓存（一批 token），decode 每次只生成一个 token（u6-l1、u4-l3）。

一句话定位本讲：**SSD 流式把 routed 专家从「常驻内存」降级为「按需调入的缓存行」，于是推理速度从「纯算力上限」变成「算力与磁盘延迟中取大」**。ds4 的全部工程努力都在于把磁盘延迟藏到算力背后，让取大者尽量仍是算力。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [AGENT.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md) | 第 11 行写明 SSD 流式的**设计约束**：routed 专家显式缓冲、快速磁盘读、把缺失专家的加载藏在 shared/已缓存专家的推理时间里；prefill 层级也要把「下一层」的加载藏在「当前层」的推理时间里。本讲的总纲。 |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 推理图调度主战场：按需读取的 `metal_graph_stream_pread_range`（~12128）、decode 重叠分支（~15780）、prefill 层预取循环（~20440 / ~20746）、hotlist 预热（~13949 / ~19755）、自动预热 4096 封顶（~13991）。 |
| [ds4_gpu.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h) | 公共 GPU 抽象里与流式相关的接口：`ds4_gpu_stream_expert_table`（描述一层专家在 GGUF 里的偏移）、`..._begin_selected_load` / `..._prepare_selected_batch` / `..._seed_experts`（按需加载与预热的统一入口）。 |
| [ds4_streaming_hotlist.inc](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_streaming_hotlist.inc) | 编译期内置的**默认受欢迎度表**（PRO / Flash 两套），按 hits/weight 排序，启动预热的数据来源。 |
| [ds4_help.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c) | `--ssd-streaming` / `--ssd-streaming-cold` / `--ssd-streaming-cache-experts` / `--ssd-streaming-preload-experts` 四个 CLI 选项的官方一句话说明。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | `Running models larger than RAM` 一节（~180–216 行）给出 SSD 流式的动机、默认命令、以及「generation 比 prefill 更怕 cache miss」的官方表述。 |

> 范围提示：真正把 expert 字节搬进设备缓冲的 slab 分配器、mlock 锁定、Metal/CUDA 内核绑定都在后端文件（`ds4_metal.m`/`ds4_cuda.cu`）里。本讲从 `ds4.c` 的**调度层**看这些函数「被谁、在什么时机、和什么重叠地调用」，不展开后端内部。

## 4. 核心概念与源码讲解

### 4.1 按需读取：routed 专家的 cache miss 路径

#### 4.1.1 概念说明

普通（非流式）模式下，整个模型在 `ds4_engine_open` 时就 mmap 进进程地址空间并交给 GPU 当作常驻权重，推理代码用 `model->map + tensor->abs_offset` 直接寻址（u3-l1、u5-l1）。routed 专家也包含在内，但它的体积占了模型绝大部分——这正是「模型放不下内存」的根因。

SSD 流式模式的取舍是：**不让 routed 专家占用常驻内存，而是给它们开一块小得多的「设备内 expert 缓存」**，只在 router 真的选中某个专家、而它又不在缓存里时，才从 GGUF 把它的字节读进来。这就是「按需读取（on-demand / cache-miss read）」。`README.md` 对此有一句精确的定位：

> In this mode the non-routed model weights stay resident, while routed MoE experts are kept in an in-memory cache and loaded from the GGUF file on cache misses.（[README.md:184-187](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L184-L187)）

`AGENT.md` 把它写成一条工程约束——**「显式分配缓冲、快速磁盘读」**，不要偷偷依赖内核的延迟页入：

> Keep the model loading for SSD streaming of routed experts explicit: allocated buffers, fast reads from disk ...（[AGENT.md:11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L11)）

「显式」的关键在于可预测：ds4 知道自己要读哪几个专家、各多大，于是可以**用 `pread` 精确地把字节读进自己分配的缓冲**，而不是等 GPU 访问到某页才触发一次不可控的、可能很深的缺页。

#### 4.1.2 核心流程

一次 decode 的某一层，按需读取的发生过程：

1. router 在 GPU 上算出本 token 的 Top-k 专家 id（6 个），写进 `g->router_selected`。
2. 主机端把 `router_selected` 读回（`ds4_gpu_tensor_read`）。
3. 构造一张 `ds4_gpu_stream_expert_table`——它**不是数据**，而是「这层的 gate/up/down 三个张量在 GGUF 里的基地址、每个专家的字节数、专家总数」这张寻址表。
4. 调用 `ds4_gpu_stream_expert_cache_begin_selected_load(&table, selected_ids, 6)`：后端遍历这 6 个 id，对**不在缓存里的**那几个，按 `table` 给出的偏移用 `pread`（或等价的 host→device 拷贝）把字节搬进 expert 缓存的空槽；已在缓存里的直接命中、不读盘。
5. 之后的 routed MoE 矩阵乘就在这些已就位的设备缓冲上做。

prefill 的按需读取形状类似，但一次喂一整批 token：用 `ds4_gpu_stream_expert_cache_prepare_selected_batch`，把这一 chunk 里**所有 token 在本层选中的专家并集**一次性加载（见 4.4 的讨论）。

#### 4.1.3 源码精读

`metal_graph_stream_pread_range` 是「显式磁盘读」的最底层：以 1 MiB 为单位、`EINTR` 可重试地从 `model->fd` 在指定偏移读字节。它不假设内核会替你预读——它自己读。

```c
/* ds4.c:12128  从 GGUF 的给定偏移显式 pread 一段专家字节 */
static bool metal_graph_stream_pread_range(
        const ds4_model *model, uint64_t offset, uint64_t size,
        uint64_t *read_bytes, uint8_t *sink) {
    ...
    const size_t chunk = 1024u * 1024u;          // 固定 1MiB 粒度
    uint8_t *buf = xmalloc(chunk);
    uint64_t pos = offset, rem = size;
    while (rem != 0) {
        const size_t want = rem > (uint64_t)chunk ? chunk : (size_t)rem;
        ssize_t nread;
        do {
            nread = pread(model->fd, buf, want, (off_t)pos);   // EINTR 可重试
        } while (nread < 0 && errno == EINTR);
        ...
    }
}
```

([ds4.c:12128-12171](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L12128-L12171)) —— 这段代码做了「按 1MiB 分块、`pread` 显式读」两件事，正是 AGENT.md「fast reads from disk」的落地。

真正把「选中专家」翻译成「读哪些字节」的是 decode 路径里的 `metal_graph_decode_selected_readahead_override`（以及 CUDA 对应的 `metal_graph_decode_cuda_selected_load`）。它先读回 router 选中的 id，再对每个 id 算出 gate/up/down 三段的绝对偏移，构造 `ds4_gpu_stream_expert_table` 后交给后端：

```c
/* ds4.c:14231  把 6 个选中专家交给 expert 缓存按需加载 */
if (ds4_gpu_routed_moe_set_selected_override(selected_ids,
                                             DS4_N_EXPERT_USED) == 0) return false;
const ds4_gpu_stream_expert_table table =
    graph_stream_expert_table_make(model, layer, il,
                                   gate_expert_bytes, down_expert_bytes);
if (ds4_gpu_stream_expert_cache_begin_selected_load(
            &table, selected_ids, DS4_N_EXPERT_USED) == 0) return false;
```

([ds4.c:14231-14246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14231-L14246))

`ds4_gpu_stream_expert_table` 这张寻址表定义在公共头里——它把「一层专家在 GGUF 里的几何」打包成可传给后端的纯数据：

```c
/* ds4_gpu.h:80  一层 routed 专家在 GGUF 里的寻址表（纯描述，不含字节） */
typedef struct ds4_gpu_stream_expert_table {
    const void *model_map;      // mmap 基址
    uint64_t    model_size;
    uint32_t    layer;
    uint32_t    n_total_expert;
    uint64_t    gate_offset, up_offset, down_offset;   // 三段张量基地址
    uint64_t    gate_expert_bytes, down_expert_bytes;  // 单个专家字节
} ds4_gpu_stream_expert_table;
```

([ds4_gpu.h:80-90](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L80-L90))

后端暴露的几个加载入口都吃这张表（[ds4_gpu.h:98-128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L98-L128)）：`..._begin_selected_load`（decode 选 6 个）、`..._prepare_selected_batch`（prefill 一批）、`..._seed_experts`（预热一组热门专家）。**按需、批量、预热三种语义共用同一套寻址与缓冲机制**——区别只在「选哪些 id、何时调用」。

#### 4.1.4 代码实践

**实践目标**：不跑模型，纯静态追踪「一次 decode 的某层，缺失专家的字节从哪里来」。

**操作步骤**：

1. 打开 [ds4.c:14231-14246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14231-L14246)，确认 `metal_graph_decode_selected_readahead_override` 先 `ds4_gpu_tensor_read(g->router_selected, ...)` 把 6 个 id 读回主机。
2. 跟进 `graph_stream_expert_table_make`（同文件内 grep 可得），看它如何用 `layer->ffn_gate_exps->abs_offset` 等填出 `ds4_gpu_stream_expert_table`。
3. 在 `ds4_metal.m` 里 grep `ds4_gpu_stream_expert_cache_begin_selected_load` 的实现，确认后端是按 `table->gate_offset + expert_id * gate_expert_bytes` 这种偏移去读/拷贝字节的。

**需要观察的现象**：读回的只是 6 个 int32 的 id；真正的大块字节搬运发生在后端、按表里算出的偏移进行。

**预期结果**：你能画出「router 选 id → 主机读回 id → 按表算偏移 → `pread`/拷贝 gate+up+down 三段 → routed MoE 在设备缓冲上计算」这条链。

**待本地验证**：在没有 Metal/CUDA 的机器上无法跑后端实现，最后一步只能靠阅读 `ds4_metal.m`/`ds4_cuda.cu` 确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么按需读取用 `pread`（带偏移）而不是 `read`（顺序）？
**答案**：routed 专家在 GGUF 里按层、按专家 id 交错排布，cache miss 时要读的是**某个专家的某段**，偏移由 `table` 算出且互不连续。`pread` 允许每次指定偏移、且不移动文件游标，天然适合多个线程/多个范围并发读；`read` 必须顺序、会互相干扰。

**练习 2**：`ds4_gpu_stream_expert_table` 里为什么没有「每个专家的绝对地址数组」，只有基地址 + 单专家字节数？
**答案**：同一层内每个 expert 等长且连续排列，地址可由 `base + expert_id * per_expert_bytes` 现算，省去存一张大表；这也让缓存里的 slot 与 GGUF 里的专家一一对应、换入换出只需改 slot 指向。

---

### 4.2 重叠隐藏延迟（一）：decode 把专家加载藏在 shared expert 背后

#### 4.2.1 概念说明

按需读取有一个致命弱点：**读盘慢**。即便 Mac SSD 很快，一次专家 miss 仍可能要几百微秒到几毫秒；decode 每个 token、每层都可能 miss 6 个专家里的若干个，如果「先读完专家、再算 routed MoE」串行执行，生成速度就会被磁盘延迟直接卡死。

ds4 的解法来自 AGENT.md 的第二条约束：

> always try to hide loading of missing routed experts by loading them while performing the inference of the shared expert and routed experts already in RAM.（[AGENT.md:11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L11)）

直觉是：每一层除了 6 个 routed 专家，还有**1 个 shared 专家永远激活、且常驻内存**。shared 专家的计算（gate/up 矩阵乘 → SwiGLU → down 矩阵乘）是有相当算力开销的，这段 GPU 计算时间里，**CPU/磁盘是空闲的**。于是 ds4 把「读缺失的 routed 专家」塞进 shared 专家计算的时间窗里——两者并行，磁盘延迟就被算力「吸收」了。这就是本节标题里的**重叠（overlap）**。

#### 4.2.2 核心流程

decode 某层的前向，**重叠分支**（`overlap_selected_shared`）的时序：

1. router 算完后，主机**发一个信号**（`ds4_gpu_signal_selected_readback_ready`），表示「等 GPU 把 router_selected 写完，我就要读回这 6 个 id」。
2. **启动一个异步加载线程**（`metal_graph_selected_async_load`），它会在 router_selected 就绪后读回 id、构造 table、调 `begin_selected_load` 去读缺失专家——**这一切在后台线程跑**。
3. 与此同时，**主线程在 GPU 上跑 shared 专家**（gate/up SwiGLU 融合 + down 投影）。
4. shared 专家算完时，调 `flush_commands` 把已录好的命令提交，并 `finish` 等异步加载线程把专家字节就位。
5. 此时 routed 专家已全在设备缓冲里，再跑 routed MoE。

关键点：步骤 2（读盘）和步骤 3（shared 算力）**时间上重叠**。只要「读盘耗时 ≤ shared 算力耗时」，miss 就被完全隐藏；否则 miss 只暴露两者之差。`README.md` 的「modern Mac SSDs are fast enough to make cache misses tolerable」正是这个不等式成立的前提（[README.md:189-194](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L189-L194)）。

异步加载在一个专用 pthread 里执行，避免阻塞主线程的命令录制：

```
主线程:  router → signal → [启动 async load] → 跑 shared expert → flush → 等 async finish → routed MoE
async 线程:                        等事件 → 读 id → 构造 table → begin_selected_load(读盘) → ok
                       ↑────────────── 这一段与 shared expert 计算并行 ──────────────↑
```

#### 4.2.3 源码精读

是否走重叠分支，由一组开关在 decode 层入口算出。核心是 `overlap_selected_shared`：它要求是流式、非 profile、非 hash 路由层，且后端支持 selected 重叠（Metal Q4、IQ2，或 CUDA）：

```c
/* ds4.c:15606  decode 层入口：决定是否把专家加载重叠在 shared expert 背后 */
const bool q4_selected_shared_overlap       = metal_graph_use_q4_selected_shared_overlap() && ...;
const bool iq2_selected_shared_overlap      = metal_graph_use_iq2_selected_shared_overlap(g) && ...;
const bool cuda_selected_shared_overlap     = metal_graph_use_cuda_selected_shared_overlap(g) && ...;
const bool overlap_selected_shared =
        ok && !decode_stage_profile &&
        !metal_graph_decode_cpu_router_applicable(g, layer) &&
        layer->ffn_gate_tid2eid == NULL &&
        getenv("DS4_MOE_REPLAY_SELECTED_IDS") == NULL &&
        (q4_selected_shared_overlap || iq2_selected_shared_overlap || cuda_selected_shared_overlap);
const bool async_selected_load =
        overlap_selected_shared &&
        ((iq2_selected_shared_overlap && metal_graph_use_iq2_selected_async_load(g)) ||
         cuda_selected_shared_overlap);
```

([ds4.c:15606-15631](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15606-L15631))

`async_selected_load` 为真时，下面就是真正的重叠编排——「signal 事件 → 起后台线程加载 → 主线程算 shared expert → flush + 等加载完成 → routed MoE」：

```c
/* ds4.c:15780  overlap 分支：把缺失专家的加载藏在 shared expert 计算背后 */
if (overlap_selected_shared) {
    uint64_t selected_event = 0;
    if (ok) ok = ds4_gpu_signal_selected_readback_ready(&selected_event) != 0;
    metal_graph_selected_async_load async_load = {0};
    bool async_load_started = false;
    ...
    if (ok && async_selected_load) {
        ok = metal_graph_selected_async_load_start(&async_load, g, model, layer, il,
                                                   selected_event, gate_expert_bytes, down_expert_bytes);
        async_load_started = ok;
    }
    if (ok && async_early_commit) ok = ds4_gpu_flush_commands() != 0;
    /* === 主线程在这里跑 shared expert（gate/up SwiGLU + down），与后台加载并行 === */
    if (ok && fuse_shared_gate_up) {
        ok = ds4_gpu_shared_gate_up_swiglu_q8_0_tensor(g->shared_gate, g->shared_up, g->shared_mid, ...);
    } else if (ok) { ... ds4_gpu_matmul_q8_0_tensor(g->shared_gate, ...); ... }
    ...
    if (async_load_started) {
        const bool flush_ok = ds4_gpu_flush_commands() != 0;
        const bool finish_ok = metal_graph_selected_async_load_finish(&async_load);  // 等加载完
        ok = ok && flush_ok && finish_ok;
    }
    /* === 专家已就位，现在跑 routed MoE === */
    if (ok) ok = ds4_gpu_routed_moe_one_tensor(g->routed_out, ...);
}
```

([ds4.c:15780-15883](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15780-L15883))

后台线程干的事就是 4.1 的按需读取，只是它跑在另一条线程上、且会先等 `selected_event` 表示「GPU 已把 router_selected 写好、可以读了」：

```c
/* ds4.c:14446  后台加载线程的主体：等事件 → 读 id → begin_selected_load */
static void metal_graph_selected_async_load_run(metal_graph_selected_async_load *job) {
    ...
    if (job->event_value != 0) {
        if (ds4_gpu_wait_selected_readback_ready(job->event_value,
                "selected-id async expert load") == 0) return;
        if (ds4_gpu_tensor_read(job->g->router_selected, 0, job->selected_ids, ...) == 0) return;
    }
    ...
    const ds4_gpu_stream_expert_table table = graph_stream_expert_table_make(...);
    if (ds4_gpu_stream_expert_cache_begin_selected_load(&table, job->selected_ids,
                                                       DS4_N_EXPERT_USED) == 0) return;
    job->ok = true;
}
```

([ds4.c:14446-14506](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14446-L14506))

把这三段串起来就看到完整的设计：**主线程的 shared expert 算力 = 一扇时间窗；后台线程的 expert 读盘 = 要塞进窗里的活**。`flush_commands` 保证 shared 的命令先提交去 GPU 执行（而不是憋在主机端录制缓冲里），这样「GPU 算 shared」与「CPU/磁盘读 expert」才真正并行。

#### 4.2.4 代码实践

**实践目标**：用环境变量打开 decode 重叠的 profile 输出，量化「读盘耗时 vs shared 算力耗时」。

**操作步骤**：

1. 阅读 [ds4.c:15780-15883](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15780-L15883)，确认 shared expert 的 gate/up/down 三个 `DS4_METAL_PROFILE_DECODE_STAGE` 标记点（`shared_gate_up`、`shared_down`）落在 async load `start` 与 `finish` 之间。
2. 阅读异步加载的 profile 路径（grep `selected-id async expert load` 与 `DS4_CUDA_STREAMING_EXPERT_CACHE_PROFILE`），看它打印 `load=...ms`。
3. （待本地验证）在有 Metal/CUDA 的机器上跑 `./ds4 -m ds4flash.gguf --ssd-streaming`，按后端文档打开对应 profile 环境变量，观察 shared expert 阶段耗时与 expert load 耗时。

**需要观察的现象**：当 cache 命中率高时，`load` 时间很短（甚至为 0，因为都命中），重叠几乎免费；当 miss 多时，`load` 变长，若超过 shared 算力，差额会体现在该 token 的总耗时里。

**预期结果**：你能用一句话解释「为什么把 expert 读盘塞在 shared expert 背后，而不是塞在 routed MoE 背后」——因为 routed MoE **正要用**刚加载的专家，它必须在加载之后；而 shared 专家与 routed 专家相互独立、且 shared 必跑，是唯一可用的并行窗。

**待本地验证**：profile 输出需要在真实后端机器上才能观察到。

#### 4.2.5 小练习与答案

**练习 1**：为什么重叠窗选 shared expert，而不是 attention？
**答案**：routed/shared MoE 是 FFN 子层的内容，与 attention 子层在 HC 连接里也是分开的两个子层（u4-l1）。选 shared expert 是因为它（a）每次必激活、算力稳定可作时间窗，（b）与 routed 专家的数据依赖最直接相邻——shared 算完紧接着就是 routed MoE，把加载塞在 shared 背后刚好赶得上 routed 用。attention 子层离 routed MoE 还隔了一层调度，且其算力随上下文长度波动大，做稳定时间窗不如 shared。

**练习 2**：`async_selected_load` 为假（即非异步）时，重叠还成立吗？
**答案**：仍部分成立。非异步路径用 `ds4_gpu_commit_and_wait_selected_readback` 同步读回 id 后再加载，然后再跑 shared——这时读回 id 的开销暴露，但「加载缺失专家」仍可与 shared 计算重叠（取决于后端是否把 load 做成异步命令）。异步路径（`metal_graph_selected_async_load`）把「读回 id + 加载」整段挪到后台线程，是重叠最彻底的形式。

---

### 4.3 重叠隐藏延迟（二）：prefill 把「下一层」的加载藏在「当前层」的计算背后

#### 4.3.1 概念说明

decode 的重叠是「同一层之内、shared 与 expert 之间」。prefill 还有一层正交的重叠：**层与层之间**。AGENT.md 的第三条约束：

> Always try to hide loading of layers for prefill in SSD streaming mode using the inference time of the current layer as the next one is loaded.（[AGENT.md:11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L11)）

prefill 的 layer-major 图调度（u5-l2、u6-l1）是**一层一层算**的：算第 `il` 层时，第 `il+1` 层的权重还没用上。于是可以把「让第 `il+1` 层的专家/权重字节准备好（page-in / readahead / pread / madvise）」这件磁盘活，塞进「算第 `il` 层」的时间窗里——两者并行。

这里有个工程取舍：prefill 一层涉及的不止 6 个专家（一批 token 的并集可能覆盖几十上百个专家），所以「准备下一层」不能像 decode 那样只读 6 个，而是要把**整层的专家范围**做一些「预热式」的磁盘提示。ds4 提供了四种由轻到重的提示手段，按优先级互斥选用：

- **page-in**：直接读字节进缓冲（最重、最确定）。
- **readahead**：用内核 `RADVISE`/等价物提示「这些范围马上要用」。
- **pread**：显式 `pread` 触发读取（4.1 的底层）。
- **madvise（MADV_WILLNEED）**：最轻，只告诉内核「将会需要」，由内核自行预读。

#### 4.3.2 核心流程

prefill 主循环（`ds4_gpu_graph` 的 layer-major prefill）里，流式层的准备逻辑：

1. 循环开始前，先把**第 0 层**准备好（`metal_graph_stream_prepare_start_if_needed(... layer=0 ...)`）。
2. 进入层循环，对第 `il` 层：先 `join`（确认第 `il` 层已准备好），再录命令、跑该层的 attention + FFN。
3. 该层算完后，若**未开 overlap**：启动第 `il+1` 层的准备（`start_if_needed(... layer=il+1 ...)`）——它会在后台跑，与「下一轮循环里算第 il+1 层之前的主线程工作」并行。
4. 若**开了 overlap**：用一组 `prepare_slots`（`layer_prepare_ahead` 个）提前准备后面好几层，slot 里的 job 各自在后台线程跑，循环到某层时只需 `join` 对应 slot。

核心不变量：**到要算第 `il` 层时，它的字节一定已 ready**（要么命中、要么已被后台线程读好）。准备工作的耗时被前面层的计算吸收。

```
层循环:  准备L0 ┐
                ├ 算L0 ┐
                │      ├ 准备L1 ┐
                │      │        ├ 算L1 ┐
                │      │        │      ├ 准备L2 ...
后台线程:      │      └读L0/1    │      └读L1/2
              (准备某层与算前一层并行)
```

#### 4.3.3 源码精读

循环前的开关选择——四种提示按优先级互斥，并决定是否启用 overlap 与「提前几层」：

```c
/* ds4.c:20447  prefill 主循环：选层准备手段、是否重叠、提前几层 */
const bool layer_pagein   = metal_graph_stream_prefill_layer_pagein_enabled(g);
const bool layer_readahead = !layer_pagein && metal_graph_stream_prefill_layer_readahead_enabled(g);
const bool layer_pread    = !layer_pagein && !layer_readahead && metal_graph_stream_prefill_layer_pread_enabled(g);
const bool layer_madvise  = !layer_pagein && !layer_pread && !layer_readahead && metal_graph_stream_prefill_layer_madvise_enabled(g);
const bool layer_prepare  = layer_pagein || layer_pread || layer_readahead || layer_madvise;
const bool layer_prepare_overlap =
        layer_prepare && metal_graph_stream_prefill_layer_pagein_overlap_enabled();
const uint32_t layer_prepare_ahead =
        layer_prepare && layer_prepare_overlap ?
        metal_graph_stream_prefill_layer_prepare_ahead() : 1u;
```

([ds4.c:20447-20464](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20447-L20464))

`overlap_enabled` 默认为真——除非显式设了 `DS4_METAL_STREAMING_PREFILL_LAYER_PAGEIN_NO_OVERLAP` 等否定环境变量：

```c
/* ds4.c:12294  overlap 默认开 */
static bool metal_graph_stream_prefill_layer_pagein_overlap_enabled(void) {
    return getenv("DS4_METAL_STREAMING_PREFILL_LAYER_PREPARE_NO_OVERLAP") == NULL &&
           getenv("DS4_METAL_STREAMING_PREFILL_LAYER_PAGEIN_NO_OVERLAP") == NULL &&
           getenv("DS4_METAL_DISABLE_STREAMING_PREFILL_LAYER_PAGEIN_OVERLAP") == NULL;
}
```

([ds4.c:12294-12298](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L12294-L12298))

准备某层 = 在一个空闲 slot 里启动后台 job（`metal_graph_stream_prefill_layer_pagein_start`），job 内部按 `pread_only`/`readahead_only`/`madvise_only`/page-in 分派到 4.1 的底层函数：

```c
/* ds4.c:12768  为某层开一个后台准备 slot（若该层尚未在准备中） */
static bool metal_graph_stream_prepare_start_if_needed(
        const ds4_gpu_graph *g, const ds4_model *model, const ds4_weights *weights,
        uint32_t layer, uint32_t n_tokens,
        bool madvise_only, bool pread_only, bool readahead_only, bool decode_only,
        metal_graph_stream_prepare_slot *slots, uint32_t n_slots) {
    if (layer >= DS4_N_LAYER) return true;
    if (metal_graph_stream_prepare_slot_find(slots, n_slots, layer)) return true;   // 已在准备
    metal_graph_stream_prepare_slot *slot = metal_graph_stream_prepare_slot_free(slots, n_slots);
    ...
    slot->layer = layer;
    if (!metal_graph_stream_prefill_layer_pagein_start(g, model, weights, layer, n_tokens,
                                                       madvise_only, pread_only, readahead_only,
                                                       decode_only, &slot->job)) { ... }
    slot->active = slot->job.started;
    return true;
}
```

([ds4.c:12768-12809](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L12768-L12809))

循环体里，算完第 `il` 层后（未开 overlap 时）启动第 `il+1` 层的准备——这就是「用当前层的推理时间加载下一层」：

```c
/* ds4.c:20746  算完第 il 层后，在后台开始准备第 il+1 层（最后一层则预热输出头） */
if (ok && g->ssd_streaming && layer_prepare && !layer_prepare_overlap) {
    if (il + 1 < DS4_N_LAYER) {
        if (!metal_graph_stream_prepare_start_if_needed(g, model, weights,
                                                      il + 1, n_tokens,
                                                      layer_madvise, layer_pread, layer_readahead,
                                                      batch_selected_addr,
                                                      layer_prepare_slots, layer_prepare_ahead)) {
            ok = false;
        }
    } else if (logits) {
        metal_graph_stream_readahead_output(model, weights);   // 最后一层：预热输出头
    }
}
```

([ds4.c:20746-20767](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20746-L20767))

> 注意：循环开头对第 `il` 层有 `metal_graph_stream_prepare_join_layer(...)`（[ds4.c:20542-20558](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20542-L20558)）——它等该层的后台准备 job 完成才继续算。于是「准备」与「算前一层」重叠，「算本层」发生在「本层已 ready」之后，正确性不会被并发破坏。

#### 4.3.4 代码实践

**实践目标**：理解 prefill 层级重叠的「提前量（prepare-ahead）」与「join 时机」如何保证既重叠又正确。

**操作步骤**：

1. 阅读 [ds4.c:20472-20494](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20472-L20494)，确认循环前先对第 0 层 `start_if_needed`。
2. 在循环体内定位两处调用：算前 `join_layer(il)`（[ds4.c:20544-20555](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20544-L20555)）、算后 `start_if_needed(il+1)`（[ds4.c:20750-20763](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20750-L20763)）。
3. 设想 `layer_prepare_ahead=1`（不开 overlap）时的时序：准备 L(il+1) 在算 L(il) 之后才启动——它只能与「算 L(il) 之后到算 L(il+1) 之前」的主线程间隙重叠。再设想 `layer_prepare_ahead>1`：L(il+2) 在算 L(il) 时就已启动，重叠窗口更长。

**需要观察的现象**：`join` 总是在「算这层」之前；`start` 总是在「算完上一层」之后（或更早）。这正是「先 ready 再用」+「尽早开始准备」两个不变量。

**预期结果**：你能画出 `prepare_ahead=1` 与 `prepare_ahead=2` 两种时序图，说明后者隐藏延迟的能力更强、代价是占更多 slot 内存。

**待本地验证**：`prepare_ahead` 的实际默认值由 `metal_graph_stream_prefill_layer_prepare_ahead`（[ds4.c:12303](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L12303)）决定，可设 `DS4_METAL_STREAMING_PREFILL_LAYER_PREPARE_AHEAD` 覆盖；具体最优值依赖机器，需在真机测。

#### 4.3.5 小练习与答案

**练习 1**：四种层准备手段（page-in / readahead / pread / madvise）为什么要互斥选用，而不是同时全开？
**答案**：它们对同一批字节做的是**同一件事的不同力度版本**（让字节尽快在内存里 ready）。全开只会重复读、浪费带宽。ds4 按从重到轻的优先级选一种：若 page-in 开了就不读 readahead，依此类推。这样环境变量只要「开一个」即可表达「用这种力度」。

**练习 2**：为什么 prefill 的层重叠放在「算完 il 层后启动 il+1」，而不是「算 il 层之前启动 il+1」？
**答案**：算 il 层之前，主线程正忙着录 il 层的命令、且 il 层的字节必须已 ready（要 `join` il）；只有算完 il 层、主线程进入「准备下一轮」的间隙，启动 il+1 的后台准备才不会与 il 层的正确性竞争。同时 il+1 的准备要赶在「循环到算 il+1 之前」完成，窗口正好是「算 il 层」这段时间。

---

### 4.4 预取与 hotlist：启动时预热哪些专家

#### 4.4.1 按需读取的两种模式回顾与 prefill 的批量预取

在进入 hotlist 之前，先澄清 4.1 留的一个问题：prefill 的按需读取为什么形状不同于 decode？

- **decode**（1 个 token）：每层 router 选 6 个专家，用 `begin_selected_load` 加载这 6 个。
- **prefill**（一批 N 个 token）：每层有 N×6 个「选中」记录，它们的**并集**可能覆盖几十到几百个专家。用 `prepare_selected_batch` 把这整个并集一次性加载。

```c
/* ds4.c:14385  prefill：把一整批 token 在本层选中的专家并集喂给缓存 */
ok = ds4_gpu_stream_expert_cache_prepare_selected_batch(
            &table, selected_ids, n_tokens, DS4_N_EXPERT_USED);
```

([ds4.c:14385-14389](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14385-L14389))

这正解释了本讲的核心命题（详见 4.5）：prefill 一次覆盖大量专家，命中率高、且大批量算力足以吸收加载；decode 一次只 6 个，命中率低、算力又小——所以 generation 更怕 miss。

#### 4.4.2 概念说明：hot expert 预热

即便有重叠，冷启动时 expert 缓存是空的，头几个 token 几乎必然全 miss。ds4 在 `ds4_engine_open` 末尾、第一次推理之前，做一次**基于受欢迎度（popularity）的预热**：把「历史上/统计上最常被路由到的那些专家」提前读进缓存，让首批 token 就有较高命中率。

受欢迎度有两个来源：

1. **内置默认表**（`ds4_streaming_hotlist.inc`）：编译期固化的、按 hits/weight 排序的 `(layer, expert)` 表，PRO 和 Flash 各一套，由作者从 profile 数据离线生成。
2. **用户提供的 profile 文件**：运行时用 `--profile-experts`（agent locality profiler，见 [ds4.c:853](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L853) 起的 `ds4_expert_profile_init`）在你自己的 workload 上采集后导出，更贴合你的真实输入分布。

预热的入口是 `ds4_gpu_stream_expert_cache_seed_experts`（[ds4_gpu.h:124-128](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_gpu.h#L124-L128)），与按需加载共用同一套寻址/缓冲机制——只是 id 来自 hotlist 而非 router。

#### 4.4.3 核心流程与「4096 封顶」

预热不是「把整个缓存填满」，而是「热种（hot seed）」：默认把内置表里最靠前的一批专家读进来，**封顶 4096 个**。封顶的原因写在代码注释里——大缓存若同步填满，会在启动阶段做几万次 `pread` 进 Metal 共享缓冲，可能触发系统看门狗：

> Auto mode is a hot seed, not a request to synchronously fill the whole cache. Large Flash caches can otherwise spend startup doing thousands of preads into shared Metal buffers and trip the system watchdog before decode begins.（[ds4.c:13998-14002](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13998-L14002)）

旋钮：

- 默认：自动热种，封顶 4096（可被 `DS4_METAL_STREAMING_EXPERT_AUTO_PRELOAD_CAP` 覆盖）。
- `--ssd-streaming-preload-experts N`：显式指定预热个数，**绕过 4096 封顶**。
- `--ssd-streaming-cold`：完全跳过预热（包括内置表），冷启动——只用于测量。

CLI 一句话说明（[ds4_help.c:166-168](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L166-L168)）。

#### 4.4.4 源码精读

内置表的样子——一个编译期常量数组，元素是 `{layer, expert}`，已按受欢迎度排序：

```c
/* ds4_streaming_hotlist.inc:1  编译期内置的默认受欢迎度表（PRO/Flash 各一套） */
/* Generated from ds4 expert hotlist profiles; sorted by hits/weight. */
/* DeepSeek V4 Pro default streaming expert hotlist. */
static const uint16_t ds4_default_streaming_hotlist_pro[][2] = {
    {44, 213}, {25, 315}, {56, 253}, {19, 161}, {41, 262}, ...
};
```

([ds4_streaming_hotlist.inc:1-6](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_streaming_hotlist.inc#L1-L6))

加载时按模型变体（PRO/Flash）选对应表，逐条 `(layer, expert)` 加进 per-layer 的选中集合（[ds4.c:13949-13989](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13949-L13989)）。封顶逻辑：

```c
/* ds4.c:13991  自动热种个数：默认封顶 4096，显式 CLI 个数绕过封顶 */
static uint32_t metal_graph_streaming_expert_preload_count(
        const ds4_gpu_graph *g, uint32_t cache_budget) {
    if (!g || cache_budget == 0) return 0;
    uint32_t preload = g->streaming_preload_experts;   // 来自 --ssd-streaming-preload-experts
    if (preload == 0) {                                 // 自动模式
        preload = cache_budget;
        const char *env = getenv("DS4_METAL_STREAMING_EXPERT_AUTO_PRELOAD_CAP");
        uint32_t cap = 4096;                            // ← 默认封顶
        ...
        if (cap != 0 && preload > cap) preload = cap;   // 超出则砍到 4096
    }
    if (preload > cache_budget) preload = cache_budget;
    ...
    return preload;
}
```

([ds4.c:13991-14018](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13991-L14018))

真正把 hotlist 灌进缓存的循环——对每个有热门专家的层，调 `seed_experts`（与 decode 的 `begin_selected_load` 同族）：

```c
/* ds4.c:19834  按 hotlist 给每层预热热门专家 */
for (uint32_t il = 0; il < DS4_N_LAYER; il++) {
    const uint32_t n = counts[il];
    if (n == 0) continue;
    ...
    const ds4_gpu_stream_expert_table table = graph_stream_expert_table_make(...);
    if (ds4_gpu_stream_expert_cache_seed_experts(&table, experts[il], priorities[il], n) == 0)
        return false;
    seeded_layers++; seeded_experts += n;
}
```

([ds4.c:19834-19864](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L19834-L19864))

整个预热是否启用，由 `metal_graph_streaming_expert_hotlist_enabled` 把关——`--ssd-streaming-cold` 会关闭它：

```c
/* ds4.c:13834  hotlist 预热仅在非 cold 启动时启用 */
static bool metal_graph_streaming_expert_hotlist_enabled(const ds4_gpu_graph *g) {
    return g && g->ssd_streaming &&
           !g->ssd_streaming_cold && ...;
}
```

（[ds4.c:13834-13839](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13834-L13839)）

#### 4.4.5 代码实践

**实践目标**：把 hotlist 预热的「数据来源、个数封顶、可绕过」三件事对上号。

**操作步骤**：

1. 打开 [ds4_streaming_hotlist.inc:1-6](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_streaming_hotlist.inc#L1-L6)，确认内置表按受欢迎度排序、PRO/Flash 各一套。
2. 读 [ds4.c:13991-14018](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13991-L14018)，找到 `cap = 4096` 与「显式 CLI 个数绕过封顶」的分支。
3. 对照 [ds4_help.c:166-168](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L166-L168)，把 `--ssd-streaming`、`--ssd-streaming-cold`、`--ssd-streaming-preload-experts N` 三个开关与代码里的 `ssd_streaming`、`ssd_streaming_cold`、`streaming_preload_experts` 字段一一对应。

**需要观察的现象**：自动模式（不传 `--ssd-streaming-preload-experts`）下预热个数 ≤ 4096；传 `N` 后 `preload = N`（不再受 4096 约束，但仍受 `cache_budget` 与 `N_LAYER*N_EXPERT` 上限钳制）。

**预期结果**：你能解释「为什么默认是热种而非填满」——填满会让启动阶段做几万次 `pread`、触发 macOS 看门狗，得不偿失；热种用很少的读换来首批 token 的高命中。

**待本地验证**：`ds4_engine_open` 末尾的预热日志（grep `streaming expert hotlist seed`）在真机会打印 `source=built-in-pro/flash preload=... experts=...`，可直接核对预热个数。

#### 4.4.6 小练习与答案

**练习 1**：`--ssd-streaming-preload-experts 0` 与 `--ssd-streaming-cold` 有何区别？
**答案**：`--ssd-streaming-cold` 直接令 `metal_graph_streaming_expert_hotlist_enabled` 返回假，**完全跳过** hotlist 路径（连选表、算个数都不做）；`--ssd-streaming-preload-experts 0` 会让 `preload_count` 在 `preload==0` 分支走自动模式（仍可能预热到 4096）。所以「想完全不预热」要用 `--ssd-streaming-cold`，「想精确控制个数」用 `--ssd-streaming-preload-experts N`（N>0）。

**练习 2**：内置 hotlist 是 `(layer, expert)` 对，为什么不存「每层一个排序后的 expert 数组」？
**答案**：不同层的热门 expert 数量差异大，定长数组浪费；变长表又麻烦。`(layer, expert)` 平铺表 + 按 hits 排序后，加载时只需「取前 preload 个、按 layer 分桶」，既紧凑又让「全局最热门」天然排在前、封顶时砍掉的就是最不热的——与热种语义一致。

---

## 5. 综合实践：解释「为什么生成比 prefill 更怕 cache miss」

这是本讲的核心实践任务，要求把四个最小模块串起来回答一个问题。`README.md` 给出了官方结论但没给推理：

> Long prefills can still be fast; generation is more sensitive to cache misses because every new token routes through experts again.（[README.md:192-194](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L192-L194)）

**任务**：结合本讲源码，用三到五条理由解释这条结论，并给出一条可验证的推论。

**操作步骤（源码阅读型）**：

1. **命中面不同**。读 [ds4.c:14385-14389](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14385-L14389)（prefill 的 `prepare_selected_batch`）与 [ds4.c:14231-14246](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L14231-L14246)（decode 的 `begin_selected_load`）。prefill 一次喂 N×6 个选择，并集可能覆盖一层里上百个专家（接近「把这层专家都读一遍」）；decode 一次只有 6 个。因此 prefill 的 miss 比例更低、且 miss 的专家更可能在同 chunk 内被后续 token 复用而「读一次用多次」。

2. **重叠窗大小不同**。读 [ds4.c:15780-15883](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L15780-L15883)（decode 重叠）。decode 用来藏 expert 加载的窗是**单个 shared expert 的算力**（1 个 token 的 gate/up/down），非常小；prefill 的窗是**一整层批量 attention+FFN 的算力**（N 个 token）+ **层间预取窗**（[ds4.c:20746-20767](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L20746-L20767)），大得多。窗越小，能藏住的磁盘延迟越少。

3. **每 token 的 miss 次数不同**。prefill 的 miss 被「一批 token」摊销——一个 chunk 只把每层专家各读一遍；decode 是「每生成一个 token、每层都可能 miss 若干个专家」，miss 次数随生成长度**线性累加**，直接体现在 token/s 上。

4. **串行性不同**。decode 必须按 token 严格自回归（u4-l3）：下一个 token 的 router 依赖上一个 token 的输出，所以「读专家」处在生成下一个 token 的关键路径上，无法跨 token 批量掩盖。prefill 的整段 prompt 之间无依赖，层间预取天然并行。

**需要给出的推论**：放大 expert 缓存（`--ssd-streaming-cache-experts`，u9-l1）对 **decode 速度**的提升应明显大于对 **prefill 速度**的提升——因为 decode 的瓶颈正是 miss 率，而 prefill 的 miss 本就被批量和层间重叠吸收。

**预期结果**：你能写出类似下面的一段话——

> prefill 一次处理一大批 token，每层几乎要把所有专家读一遍，命中率天然高、且读盘能被批量算力和下一层预取这两扇大窗吸收；decode 一次只有一个 token、每层只点 6 个专家，命中率低，而能藏读盘的 shared expert 窗又极小，加上严格自回归使 miss 处在关键路径上——所以同样的 cache miss，prefill 几乎无感，decode 却直接拖慢 token/s。

**待本地验证**：在真机上可用 `ds4-bench`（u10-l5）分别测 prefill t/s 与生成 t/s 随 `--ssd-streaming-cache-experts` 的变化曲线，预期生成曲线斜率更陡。

## 6. 本讲小结

- **按需读取**是 SSD 流式的底层：routed 专家不常驻，cache miss 时用 `metal_graph_stream_pread_range` 这类显式 `pread` 从 GGUF 读字节进自己分配的缓冲，由 `ds4_gpu_stream_expert_table` 这张寻址表把「选中 id」翻译成「磁盘偏移」（4.1）。
- **decode 延迟隐藏**靠「把缺失专家的加载藏在 shared expert 计算背后」：主线程跑 shared gate/up/down 的同时，后台线程（`metal_graph_selected_async_load`）读回 router id 并 `begin_selected_load` 读盘，`flush_commands` + `finish` 在 routed MoE 之前汇合（4.2）。
- **prefill 延迟隐藏**靠两层：层内用 `prepare_selected_batch` 一次覆盖一批 token 的专家并集；层间在算完第 `il` 层后启动第 `il+1` 层的后台准备（page-in/readahead/pread/madvise 四选一，`prepare_ahead` 个 slot 提前），`join` 保证「先 ready 再算」（4.3）。
- **预取与 hotlist**：启动时按受欢迎度热种最常被路由的专家，默认封顶 4096 避免几万次 `pread` 触发看门狗；内置 PRO/Flash 两套表，`--ssd-streaming-preload-experts N` 绕过封顶，`--ssd-streaming-cold` 完全跳过（4.4）。
- 一条贯穿全讲的工程哲学（[AGENT.md:11](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/AGENT.md#L11)）：**让磁盘延迟与算力并行，使推理速度尽量由算力而非磁盘决定**。
- 核心结论（[README.md:192-194](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L192-L194)）：**prefill 比 decode 更耐 cache miss**，因为 prefill 命中面大、重叠窗大、miss 被批量摊销；decode 命中面小、重叠窗小、miss 处在自回归关键路径上。

## 7. 下一步学习建议

- **横向扩展**：本讲的「按需读 + 重叠」是单机 SSD 流式。把模型拆到多机、用网络而非磁盘搬运激活值，是下一讲 [u9-l3 分布式推理架构与层切分](u9-l3-distributed-architecture.md) 与 [u9-l4 分布式协议、路由与流水线](u9-l4-distributed-protocol.md) 的主题——其中的「prefill 流水线（chunk N+1/N 重叠）」与本讲的层间预取是同一种延迟隐藏思想在跨机场景的复用。
- **纵向深挖后端**：本讲停在 `ds4.c` 的调度层。expert slab 分配器、mlock 锁定、Metal/CUDA 内核如何消费这些设备缓冲，需结合 [u5-l2 Metal 后端](u5-l2-metal-backend.md) 与 [u5-l3 CUDA/ROCm 后端](u5-l3-cuda-rocm-backends.md) 直接读 `ds4_metal.m`/`ds4_cuda.cu` 里 `ds4_gpu_stream_expert_cache_*` 的实现。
- **测量**：想量化「重叠藏住了多少延迟」，下一阶段可读 [u10-l5 速度基准 ds4-bench](u10-l5-speed-benchmark.md)，用 `ds4-bench` 在不同 `--ssd-streaming-cache-experts` 下测 prefill/生成曲线，验证本讲 4.5 的推论。
- **采集自己的 hotlist**：若要在自己的 workload 上替换内置表，阅读 [ds4.c:853](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L853) 起的 `ds4_expert_profile_*` 一族函数（agent locality profiler），它能把运行时观测到的 `(layer, expert)` 命中分布导出成可喂回的 hotlist 文件。
