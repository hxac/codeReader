# SSD 流式专家缓存

## 1. 本讲目标

本讲进入「SSD 流式与分布式推理」单元的第一篇。当 DeepSeek V4 的完整模型塞不进一台机器的内存时，ds4 提供一条 **Metal 专属的 SSD 流式容量模式**：让非 routed 权重常驻内存，把体积最大的 routed MoE 专家放进一个「内存里的专家缓存」，缓存未命中时再从 GGUF 文件按需读取。

本讲只盯「缓存规划（cache planning）」这一层，不展开按需读取与延迟隐藏（那是 u9-l2 的事）。学完后你应当能够：

- 读懂 `ds4_ssd_auto_cache_plan` 的预算算法：如何从一个「推荐工作集」字节数，减去非 routed 权重，换算出能装下多少个**完整的 routed 专家**。
- 区分两种 `mlock`：ds4_ssd.c 里的诊断用 `mlock`（`--simulate-used-memory`）与 ds4_metal.m 里真正的「专家缓存 mlock + 可锁定上限裁剪」。
- 理解启动时的 hot expert 预加载（popularity-based 预热、默认 4096 上限、`--ssd-streaming-cold` 跳过）。

## 2. 前置知识

阅读本讲前，请确认你已掌握以下概念（它们来自前置讲义）：

- **routed MoE 专家**（u4-l1）：DeepSeek V4 每层有 256 个（Flash）/ 384 个（PRO）routed 专家，router 每个 token 只激活其中 6 个。专家权重是模型体积的大头。
- **权重绑定与张量布局**（u3-l2）：routed 专家在 GGUF 里是三维张量（含 N 个专家），按名字绑定进 `ds4_layer_weights` 的 `ffn_gate_exps / ffn_up_exps / ffn_down_exps`。一个「完整专家」= 该层的 gate + up + down 三块权重中各取一份。
- **非对称量化**（u1-l2 / u3-l4）：只有 routed 专家被压到 2bit（IQ2_XXS / Q2_K），shared 专家、投影、路由保持高精度。这直接影响「每个专家多少字节」。
- **mmap 权重加载**（u3-l1）：GGUF 整个文件被 mmap 进进程地址空间，权重字节始终留在映射区，推理代码用 `map + abs_offset` 直接寻址。

两个本讲要用到的术语：

- **推荐工作集（recommended working set）**：Metal 设备通过 `recommendedMaxWorkingSetSize` 报告的「建议驻留显存/统一内存上限」。超过它就会落入 macOS 分页换出，速度骤降。
- **可锁定（lockable）**：用 `mlock(2)` 把内存页钉在物理 RAM 里、禁止换出的能力。macOS 对可锁定字节数有上限，超出会失败。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到什么 |
| --- | --- | --- |
| [ds4_ssd.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.h) | SSD 流式的**规划层公共头**：预算结构体、字节↔专家数换算、mlock 锁原语 | `ds4_ssd_cache_plan`、`ds4_ssd_memory_lock`、四个函数原型 |
| [ds4_ssd.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c) | 规划层的全部实现：GiB/NGB 解析、自动预算算法、诊断用 mlock | `ds4_ssd_auto_cache_plan`、`ds4_parse_streaming_cache_experts_arg`、`ds4_ssd_memory_lock_acquire` |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心：自动预算的**调用方**、非 routed 字节与单专家字节的测量、hot 预加载计数 | `ds4_engine_configure_streaming_auto_cache`、`weights_streaming_non_routed_bytes`、`metal_graph_streaming_expert_preload_count` |
| [ds4_metal.m](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m) | Metal 后端：**运行时真正的专家缓存 mlock + 可锁定上限裁剪** | `ds4_gpu_stream_expert_alloc_buffer`、`..._cap_budget_to_locked`、`..._configured_budget` |
| [ds4_help.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c) / [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | 五个 SSD 相关 CLI 选项的文档与解析 | `--ssd-streaming`、`--ssd-streaming-cache-experts N|NGB`、`--ssd-streaming-preload-experts N`、`--ssd-streaming-cold`、`--simulate-used-memory NGB` |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | SSD 流式用户指南 | 「SSD streaming capacity mode」一节 |

一句话定位：**ds4_ssd.{h,c} 是纯规划/数学层，不含任何 GPU 代码、不含任何按需读取逻辑**；真正的运行时（缓存分配、mlock、prefill 预取）在 ds4_metal.m 与 ds4.c 的图代码里。本讲把这层「数学规划」单独抽出来讲清楚。

## 4. 核心概念与源码讲解

### 4.1 自动预算算法：从「推荐工作集」到「能缓存多少个专家」

#### 4.1.1 概念说明

SSD 流式模式的核心问题是：**专家缓存开多大？**

- 开太大：和 KV 缓存、图 scratch、激活争内存，把非 routed 权重挤出物理 RAM，触发换页，速度崩溃。
- 开太小：缓存命中率低，每个 token 都要回 GGUF 读专家，生成速度（每 token 都要路由专家）受拖累。

这个问题对人很难手算，因为「缓存大小」既不是字节数（用户给的 `32GB` 要先除以单专家字节得到专家数），也不是专家数（不同量化档单专家字节不同）。于是 ds4 提供一个**自动预算**：问 Metal「你建议我用多少内存」，留 80% 给模型+缓存，先扣掉必须常驻的非 routed 权重，剩下的全给专家缓存，再换算成完整专家数。

这里有一个贯穿全讲的要点：**专家缓存的计量单位永远是「完整专家个数」，不是字节数**。`ds4_ssd_cache_plan` 的核心输出就是 `cache_experts`。

#### 4.1.2 核心流程

自动预算在引擎打开时执行，调用链是：

```
ds4_engine_open
  └─ ds4_engine_configure_streaming_auto_cache   (仅当未显式指定 cache 时)
       ├─ recommended   = ds4_gpu_recommended_working_set_size()   // Metal 推荐
       ├─ non_routed_bytes = weights_streaming_non_routed_bytes()  // 必须常驻部分
       ├─ per_expert_bytes = ds4_streaming_routed_expert_bytes()   // 单个专家字节
       ├─ max_model_experts = DS4_N_LAYER * DS4_N_EXPERT            // 硬上限
       └─ ds4_ssd_auto_cache_plan(recommended, non_routed, per_expert, max, &plan)
                ├─ model_target_bytes = recommended * 4 / 5          // 取 80%
                ├─ cache_bytes = model_target_bytes - non_routed     // 扣除非 routed
                ├─ cache_experts = cache_bytes / per_expert          // 换算成专家数
                │       (下保底 1，上封顶 max_model_experts)
                └─ effective_cache_bytes = cache_experts * per_expert
```

数学上（独立公式）：

\[
\text{model\_target} = \left\lfloor \frac{4}{5}\,\text{recommended} \right\rfloor
\]

\[
\text{cache\_bytes} = \max\!\left(0,\ \text{model\_target} - \text{non\_routed\_bytes}\right)
\]

\[
\text{cache\_experts} = \min\!\left(\text{max\_model\_experts},\ \left\lfloor \frac{\text{cache\_bytes}}{\text{per\_expert\_bytes}} \right\rfloor\right),\quad \text{下保底为 }1
\]

注意 `effective_cache_bytes` 与 `cache_bytes` 通常**不相等**：前者是「整数个专家」占的字节（向下取整丢掉了零头），后者是预算字节。运行时按 `cache_experts` 分配，零头还回给系统。

#### 4.1.3 源码精读

先看预算结构体，它在 [ds4_ssd.h:12-17](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.h#L12-L17) 定义——四个字段就是上面流程里的四个量：

```c
typedef struct {
    uint64_t model_target_bytes;     // 推荐工作集的 80%
    uint64_t cache_bytes;            // model_target 减去非 routed（可能为 0）
    uint64_t effective_cache_bytes;  // 整数个专家实际占用字节
    uint32_t cache_experts;          // 最终换算出的完整专家数（核心输出）
} ds4_ssd_cache_plan;
```

预算算法本体在 [ds4_ssd.c:80-106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L80-L106)，每个关键步骤都对得上公式：

```c
out->model_target_bytes =
    recommended_bytes > UINT64_MAX / 4ull ?
        UINT64_MAX : (recommended_bytes * 4ull) / 5ull;   // 80%，带溢出保护
if (out->model_target_bytes > non_routed_bytes) {
    out->cache_bytes = out->model_target_bytes - non_routed_bytes;   // 扣除非 routed
}
uint64_t cache_experts = out->cache_bytes / per_expert_bytes;
if (cache_experts == 0) cache_experts = 1;                            // 下保底 1
if (max_model_experts != 0 && cache_experts > max_model_experts) {
    cache_experts = max_model_experts;                                // 上封顶
}
out->cache_experts = (uint32_t)cache_experts;
out->effective_cache_bytes = cache_experts * per_expert_bytes;        // 零头丢回系统
```

它配套一个更小的换算函数 [ds4_ssd.c:72-78](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L72-L78)，只做「字节→专家数」一步除法，供用户显式给 `NGB` 时使用：

```c
uint32_t ds4_ssd_cache_experts_for_byte_budget(uint64_t bytes,
                                               uint64_t per_expert_bytes) {
    if (bytes == 0 || per_expert_bytes == 0) return 0;
    const uint64_t experts = bytes / per_expert_bytes;
    if (experts == 0 || experts > UINT32_MAX) return 0;
    return (uint32_t)experts;
}
```

算法所需的三种「测量值」由引擎核心提供。**非 routed 字节**用 [weights_streaming_non_routed_bytes](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L4403-L4416)（ds4.c:4403-4416）汇总所有「常驻、不走缓存」的张量 span；**单个专家字节**用 [ds4_streaming_routed_expert_bytes](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L3321-L3334)（ds4.c:3321-3334）从**第一个 routed 层**测得：

```c
for (uint32_t il = 0; il < DS4_N_LAYER; il++) {
    if (streaming_layer_routed_expert_bytes(&weights->layer[il], per_expert_bytes_out)) {
        return true;   // 只用第一层，决定整个缓存的 size class
    }
}
```

> 为什么只用第一层？因为专家缓存是一个**单一尺寸类（single size-class）的 slab 分配器**：第一个 routed 层的专家字节决定整个缓存的槽位大小。混合精度 GGUF（少数层升档到 Q4_K）的「大专家」永远进不了缓存，只能走 mmap 直读——这点 ds4.c:3336-3343 的注释讲得很清楚。

把这三项喂给预算函数的就是 [ds4_engine_configure_streaming_auto_cache](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25380-L25456)（ds4.c:25380-25456），它还有一个**重要的进入条件**——只有用户没显式指定缓存时才自动算（ds4.c:25385-25394）：

```c
if (e->ssd_streaming_cache_experts != 0 ||
    e->ssd_streaming_cache_bytes   != 0) {
    return true;   // 用户已经给了 N 或 NGB，不再自动规划
}
```

成功后把 `plan.cache_experts` 写进 `e->ssd_streaming_cache_experts`，并在 stderr 打印那张预算明细表（推荐 GiB、80% 目标、非 routed GiB、单专家 MiB、缓存专家数及 GiB）。

README 对这条算法有权威的用户向描述，见 [README.md:209-216](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L209-L216)：「它取 Metal 推荐工作集的 80%，减去非 routed 权重，剩下的全给 routed 专家。只有自动预算会替你做这个减法。」

#### 4.1.4 代码实践

**实践目标**：亲手推一遍自动预算的减法，确认它「从推荐工作集减去非 routed 权重得到 expert 缓存大小」。

**操作步骤**（纯源码阅读型，无需 GPU）：

1. 打开 [ds4_ssd.c:80-106](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L80-L106)，把 `recommended_bytes`、`non_routed_bytes`、`per_expert_bytes` 三个形参当成未知数，写出 `model_target_bytes`、`cache_bytes`、`cache_experts`、`effective_cache_bytes` 的表达式。
2. 打开调用方 [ds4.c:25396-25430](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25396-L25430)，确认三个实参的来源：
   - `recommended` ← `ds4_gpu_recommended_working_set_size()`（即 Metal 的 `recommendedMaxWorkingSetSize`，见 [ds4_metal.m:2970-2973](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L2970-L2973)）
   - `non_routed_bytes` ← `weights_streaming_non_routed_bytes`
   - `per_expert_bytes` ← `ds4_streaming_routed_expert_bytes`
   - `max_model_experts` = `DS4_N_LAYER * DS4_N_EXPERT`
3. 代入 README 给的真实案例（[README.md:246-247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L246-L247)）：M5 Max 128GB、PRO q2，自动预算选出约 `59GB` routed 专家缓存。反推：如果 `recommendedMaxWorkingSetSize≈100GB`，则 `model_target≈80GB`；扣掉非 routed 后剩约 59GB 给专家。

**需要观察的现象**：`cache_bytes` 是「预算字节数」，而 `effective_cache_bytes ≤ cache_bytes` 是「整数个专家实际占的字节」，二者之间的差就是被 `per_expert_bytes` 取整丢掉的零头。

**预期结果**：你能用一句话复述算法——「`cache_experts = floor( (recommended×0.8 − non_routed) / per_expert )`，再夹在 `[1, N_LAYER×N_EXPERT]` 之间」。若手头有 Mac，运行 `./ds4 -m ./ds4flash.gguf --ssd-streaming` 并观察启动日志的「SSD streaming auto cache budget」明细块与之核对（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果一台机器的推荐工作集很大，但模型非 routed 权重已经超过它的 80%，`cache_experts` 会是多少？

**答案**：`cache_bytes` 被钳为 0，但 [ds4_ssd.c:97](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L97) 有 `if (cache_experts == 0) cache_experts = 1;` 的下保底，所以最终是 **1 个专家**。调用方还会额外打印一条提示（[ds4.c:25450-25453](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25450-L25453)）「non-routed weights already fill the 80% target; keeping a one-expert cache」。

**练习 2**：为什么 `max_model_experts` 要传 `DS4_N_LAYER * DS4_N_EXPERT`，而不是单层的 `DS4_N_EXPERT`？

**答案**：缓存是**全局驻留策略**，不按层划分——任何层都可以用缓存里任意一个专家槽。所以理论上限是「全模型所有专家」= 层数 × 每层专家数。这是软上限，实际会被预算字节先卡住。

---

### 4.2 mlock 与可锁定上限裁剪

#### 4.2.1 概念说明

本模块要严格区分**两种 mlock**，初学者最容易混淆：

| | ds4_ssd.c 的 mlock | ds4_metal.m 的 mlock |
| --- | --- | --- |
| 函数 | `ds4_ssd_memory_lock_acquire` | `ds4_gpu_stream_expert_alloc_buffer` 内联 |
| 用途 | **诊断**：模拟「内存已被占用」 | **运行时**：把每个专家缓冲钉在 RAM |
| 触发 | `--simulate-used-memory NGB` | 启用 SSD 流式后每个专家缓冲分配时 |
| 失败后果 | 引擎打开失败退出 | **裁剪缓存预算**，继续运行 |

为什么要 mlock？因为 SSD 流式模式下，专家缓存的 Metal buffer 是 `StorageModeShared`（统一内存机器上就是普通 RAM）。如果不 mlock，macOS 可能把专家页换出到 SSD——而 SSD 流式本来就是为了「从 SSD 读专家」，被换出的专家再被读回，等于双重 SSD 往返，速度极不稳定。把热专家钉在 RAM 里才能保证缓存命中是真命中。

但 macOS 对单进程可锁定的字节数有上限（受 `ulimit -l` 与系统策略约束）。当用户或自动预算要的缓存超过可锁定上限时，ds4 不会崩，而是**把缓存预算裁剪到实际能锁定的量**——这就是「可锁定上限裁剪（lockable budget cap）」。

#### 4.2.2 核心流程

**诊断用 mlock**（ds4_ssd.c，`--simulate-used-memory`）的流程：

```
ds4_engine_open
  └─ if (simulate_used_memory_bytes != 0)
        ds4_ssd_memory_lock_acquire(bytes)
            ├─ mmap(NULL, bytes, ... MAP_PRIVATE|MAP_ANONYMOUS)   // 申请匿名页
            ├─ 分 256MiB 一块：
            │     ├─ 逐页 touch（写一个字节）强制物理分配
            │     └─ mlock(ptr+len, len)                          // 钉住这一块
            └─ 任一块失败 → munlock 已锁部分、munmap、返回 false
```

**运行时 mlock + 裁剪**（ds4_metal.m）的流程：

```
为每个专家槽分配 Metal buffer（ds4_gpu_stream_expert_alloc_buffer）
  └─ mlock([buffer contents], length)
        ├─ 成功 → g_stream_expert_cache_mlock_bytes += n
        └─ 失败 → g_stream_expert_cache_mlock_failures++
                   ├─ ds4_gpu_stream_expert_cache_cap_budget_to_locked()
                   │     └─ g_stream_expert_cache_mlock_budget_cap =
                   │          min(已锁槽位数, (已锁字节-1GiB)/单专家字节)
                   └─ ds4_gpu_stream_expert_cache_warn_mlock_failure()  // 告警并建议更小的 NGB

之后 ds4_gpu_stream_expert_cache_configured_budget()
  └─ if (请求预算 > mlock_budget_cap) 请求预算 = mlock_budget_cap   // 裁剪生效
```

裁剪的关键直觉：**第一次 mlock 失败的位置，就是这台机器此刻能锁定的真实上限**。ds4 把它换算回「专家数」，存进 `g_stream_expert_cache_mlock_budget_cap`，之后所有问「缓存预算多少」的地方都被这个值夹住。README [249-255 行](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L249-L255) 把这条策略描述为：「过大的 `NGB` 请求会在推理前被裁剪，使专家缓冲保持可锁定而不是落入 macOS 分页；如果系统仍内存吃紧、mlock 仍失败，ds4 拒绝装入可换页的专家缓存项，释放一段锁定余量后，用测得的『可锁定缓存大小』继续。」

#### 4.2.3 源码精读

诊断用 mlock 锁结构在 [ds4_ssd.h:7-10](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.h#L7-L10)，实现 [ds4_ssd.c:108-173](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L108-L173)。关键两段：

```c
const uint64_t chunk_bytes = 256ull * 1024ull * 1024ull;   // 256MiB 一块
for (uint64_t off = 0; off < bytes; off += chunk_bytes) {
    ...
    for (uint64_t pos = off; pos < off + len; pos += page) {
        p[pos] = (unsigned char)(pos / page);   // touch 强制物理分配（volatile 防优化掉）
    }
    if (mlock((void *)(p + off), (size_t)len) != 0) { ... return false; }   // 逐块锁
    locked += len;
}
```

> 注释 [ds4_ssd.c:139-143](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_ssd.c#L139-L143) 解释了为什么分块：单次超大 `mlock` 失败时难诊断，且在 macOS 上会制造长时间不可中断的 VM 工作；分块「镜像独立诊断工具的行为」。这是给 `--simulate-used-memory` 用的——一个让你在 128GB 机器上模拟 64GB 机器、测试预算算法的诊断旋钮，见 [ds4_help.c:169](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L169)。

它在引擎打开最早期消费（[ds4.c:25580-25586](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25580-L25586)），**在 mmap 加载 GGUF 之前**——这样才能真的「挤占」可用内存，影响后续的自动预算。

运行时真正的专家 mlock 在 [ds4_gpu_stream_expert_alloc_buffer](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L8206-L8258)（ds4_metal.m:8206-8258），分配 Metal buffer 后立刻尝试锁：

```c
void *ptr = [buffer contents];
const NSUInteger n = [buffer length];
if (ptr && n != 0 && mlock(ptr, (size_t)n) == 0) {
    g_stream_expert_cache_mlock_bytes += (uint64_t)n;          // 记已锁字节
} else {
    g_stream_expert_cache_mlock_failures++;                    // 记失败次数
    g_stream_expert_cache_mlock_fail_bytes += (uint64_t)n;
    ds4_gpu_stream_expert_cache_cap_budget_to_locked();        // ★ 触发裁剪
    ds4_gpu_stream_expert_cache_warn_mlock_failure((uint64_t)n, err);
}
```

裁剪逻辑 [ds4_gpu_stream_expert_cache_cap_budget_to_locked](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L8183-L8204)（ds4_metal.m:8183-8204）把「已成功锁定的字节数」换算回专家数，再留 1GiB 安全余量：

```c
uint32_t cap = g_stream_expert_cache_entry_count;
const uint32_t locked_slots = ds4_gpu_stream_expert_slab_locked_slot_count();
if (locked_slots != 0 && locked_slots < cap) cap = locked_slots;
uint64_t safe_gib = g_stream_expert_cache_mlock_bytes / gib;
if (safe_gib > 1) safe_gib--;                                 // 留 1GiB 余量
if (safe_gib != 0 && g_stream_expert_cache_expert_bytes != 0) {
    uint64_t safe_cap64 = (safe_gib * gib) / g_stream_expert_cache_expert_bytes;
    if (safe_cap64 < cap) cap = (uint32_t)safe_cap64;
}
if (cap == 0) return;
if (g_stream_expert_cache_mlock_budget_cap == 0 ||
    cap < g_stream_expert_cache_mlock_budget_cap) {
    g_stream_expert_cache_mlock_budget_cap = cap;             // 只取更小值
}
```

这个 cap 真正「夹住」预算的地方是 [ds4_gpu_stream_expert_cache_configured_budget](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L7540-L7548)（ds4_metal.m:7540-7548），任何询问「缓存到底多大」的调用都走它：

```c
uint32_t budget = ds4_gpu_stream_expert_cache_requested_budget();   // 用户/自动给的
if (budget != 0 &&
    g_stream_expert_cache_mlock_budget_cap != 0 &&
    budget > g_stream_expert_cache_mlock_budget_cap) {
    budget = g_stream_expert_cache_mlock_budget_cap;                // 裁剪
}
return budget;
```

告警函数 [ds4_gpu_stream_expert_cache_warn_mlock_failure](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_metal.m#L8108-L8169)（ds4_metal.m:8108-8169）会在 stderr 打印「locked so far / failed buffer / 建议改用更小的 `--ssd-streaming-cache-experts NGB`」，并解释「macOS 可能换出未锁的专家缓冲，导致速度变差或不稳」。

#### 4.2.4 代码实践

**实践目标**：用 `--simulate-used-memory` 这个诊断旋钮，亲手逼出「自动预算缩小」的行为，并理解它与运行时 mlock 裁剪是两件事。

**操作步骤**：

1. 只读路径：打开 [ds4.c:25580-25586](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25580-L25586)，确认 `ds4_ssd_memory_lock_acquire` 在 `ds4_acquire_instance_lock()` 之后、模型加载之前被调用——这正是它能「吃掉」内存、影响后续 `recommendedMaxWorkingSetSize` 之外可用内存的位置。
2. 推演：在一台 128GB Mac 上，`--simulate-used-memory 64GB` 会先 mlock 住 64GB，随后自动预算看到的「可用」事实上更小。结合 4.1 的公式，预测 `cache_experts` 会比不开此开关时**更小**还是更小？（提示：`recommendedMaxWorkingSetSize` 是设备级常量，不会变；但物理上可锁定的余量变少了，运行时 mlock 裁剪会更早触发。）
3. 若有 Mac：分别运行
   `./ds4 -m ./ds4flash.gguf --ssd-streaming`
   与
   `./ds4 -m ./ds4flash.gguf --ssd-streaming --simulate-used-memory 64GB`
   对比启动日志里「cached expert count」与是否有 mlock 失败告警（待本地验证）。

**需要观察的现象**：诊断用 mlock 失败会直接报 `--simulate-used-memory mlock failed after X/Y GiB` 并退出；运行时专家 mlock 失败则只告警、不退出，并把缓存数裁到 `using locked cache cap: ...`。

**预期结果**：你能向别人讲清「ds4_ssd.c 的 mlock 是用来模拟内存占用、测规划算法的；真正决定缓存可锁定上限、并在失败时裁剪预算的是 ds4_metal.m 里每个专家缓冲的 mlock」。

#### 4.2.5 小练习与答案

**练习 1**：`ds4_ssd_memory_lock_acquire` 为什么要先 `touch` 每一页再 `mlock`，而不是直接 `mlock` 整块？

**答案**：`MAP_ANONYMOUS` 的页在第一次访问前只是「保留」、没有物理页。`mlock` 锁的是**已分配的物理页**，不 touch 直接 mlock 在某些系统上行为不可预期（可能不强制写时分配失败）。逐页写一个字节（`p[pos] = pos/page`）强制每页物理驻留，再 mlock 才有意义。`volatile` 指针防止编译器把这次写优化掉。

**练习 2**：运行时专家 mlock 失败时，ds4 为什么选择「裁剪预算继续跑」而不是「直接退出」？

**答案**：因为部分锁定的专家缓存仍然有用——只是上限变小。退出会让用户在边界内存配置下完全无法启动；裁剪则降级使用，并用告警提示用户调小 `NGB`。这符合 README 反复强调的「保守起步，有 headroom 再加大缓存」策略。

---

### 4.3 hot expert 预加载

#### 4.3.1 概念说明

自动预算算出「能缓存 N 个专家」，但**启动时不必立刻把这 N 个专家全读进缓存**。原因有二：

1. 读一个专家要从 GGUF 做 `pread`，N 很大时（Flash 可达数万槽）启动会变成「几万次 pread 进 shared Metal buffer」，可能触发系统看门狗，迟迟进不了 decode。
2. 并非所有专家 equally hot——DeepSeek V4 的 router 对专家的使用频率高度不均，少数「热门专家」承担大部分 token。

于是 ds4 默认走 **popularity-based（基于受欢迎度）的 hot 预加载**：启动时只预热「最热门」的一批专家，剩下的等运行中按需读取（u9-l2 详述）。用户可用 `--ssd-streaming-preload-experts N` 显式指定预热数，或用 `--ssd-streaming-cold` 完全跳过预热（留给测量场景）。

#### 4.3.2 核心流程

```
metal_graph_streaming_expert_preload_count(g, cache_budget):
    preload = g->streaming_preload_experts        // 用户给的就直接用
    if (preload == 0):                            // 自动模式
        preload = cache_budget                     //   默认想预加载全部
        cap = 4096  (或 DS4_METAL_STREAMING_EXPERT_AUTO_PRELOAD_CAP)
        if (preload > cap) preload = cap           //   自动模式封顶 4096
    preload = min(preload, cache_budget)           // 不能超过缓存容量
    preload = min(preload, N_LAYER * N_EXPERT)     // 不能超过全模型专家数
    return preload
```

直觉：**用户显式给的 N 是承诺（无封顶），自动模式是种子（有 4096 封顶）**。注释 [ds4.c:13998-14002](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13998-L14002) 说得很直白：「自动模式是一个 hot 种子，不是『同步填满整个缓存』的请求——大 Flash 缓存否则会在启动时做几千次 pread 进 shared Metal buffer，并在 decode 开始前触发系统看门狗。显式 CLI 预加载数绕过这个上限。」

#### 4.3.3 源码精读

预加载计数函数 [metal_graph_streaming_expert_preload_count](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13991-L14018)（ds4.c:13991-14018）：

```c
uint32_t preload = g->streaming_preload_experts;
if (preload == 0) {
    preload = cache_budget;
    /* Auto mode is a hot seed, not a request to synchronously fill ... */
    const char *env = getenv("DS4_METAL_STREAMING_EXPERT_AUTO_PRELOAD_CAP");
    uint32_t cap = 4096;
    ...   // 解析环境变量覆盖 cap
    if (cap != 0 && preload > cap) preload = cap;
}
if (preload > cache_budget) preload = cache_budget;
const uint64_t max_possible = (uint64_t)DS4_N_LAYER * DS4_N_EXPERT;
if ((uint64_t)preload > max_possible) preload = (uint32_t)max_possible;
return preload;
```

用户旋钮从 CLI 进入引擎选项，见 [ds4_cli.c:1494-1500](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1494-L1500) 与帮助文本 [ds4_help.c:168](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L168)：

```c
} else if (!strcmp(arg, "--ssd-streaming-preload-experts")) {
    int v = parse_int(need_arg(&i, argc, argv, arg), arg);
    if (v <= 0) { ... exit(2); }
    c.engine.ssd_streaming_preload_experts = (uint32_t)v;
}
```

跳过预热的开关 `--ssd-streaming-cold` 在 [ds4_cli.c:1481-1482](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1481-L1482)，帮助 [ds4_help.c:166](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_help.c#L166)：「跳过默认的基于受欢迎度的专家缓存预加载」。README [215 行](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L215) 总结了用法：「正常使用时保留 hot 专家预加载；只在测量时才用 `--ssd-streaming-cold` 和 `--ssd-streaming-preload-experts N`。」

> 注意区分三个旋钮：`--ssd-streaming-cache-experts N|NGB` 决定**缓存容量**（4.1）；`--ssd-streaming-preload-experts N` 决定**启动预热多少**（本节，默认自动封顶 4096）；`--ssd-streaming-cold` 决定**完全不预热**。三者正交，但 preload 数会被夹到 ≤ cache 容量。

#### 4.3.4 代码实践

**实践目标**：读懂「自动预热=hot 种子」与「显式预热=承诺」的区别，并解释为何默认要封顶。

**操作步骤**：

1. 打开 [ds4.c:13991-14018](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L13991-L14018)，回答：自动模式下 `preload` 的初值为何是 `cache_budget`？随后被什么值夹住？
2. 假设 Flash 模型自动预算得到 `cache_experts = 30000`，问：默认启动会预热多少个专家？答：`min(30000, 4096) = 4096`。
3. 若用户加 `--ssd-streaming-preload-experts 30000`，会发生什么？答：**不被 4096 封顶**，直接预热 30000 个（但仍夹到 ≤ cache_budget 与 ≤ N_LAYER×N_EXPERT）。这正是注释里「Explicit CLI preload counts bypass this cap」的含义。
4.（可选，待本地验证）对照运行 `--ssd-streaming-cold` 与默认启动的启动耗时，确认 cold 启动更快、但首批 token 更慢。

**需要观察的现象**：默认启动日志会报告预热了多少专家、耗时多少秒；cold 启动跳过这段日志，但第一个生成 token 的延迟更高（因为缓存全空、全 miss）。

**预期结果**：你能说清「为什么自动预加载要封顶 4096——避免启动阶段几万次 pread 触发看门狗；以及为什么显式 N 不封顶——用户主动要求时尊重其意图」。

#### 4.3.5 小练习与答案

**练习 1**：`--ssd-streaming-cold` 和 `--ssd-streaming-preload-experts 0` 是一回事吗？

**答案**：效果上接近（都不预热），但语义路径不同。`--ssd-streaming-cold` 设置 `ssd_streaming_cold` 标志，是上层「跳过 popularity 预加载」的开关；`preload_experts` 是图层的预热计数。实际使用按 README 建议用 `--ssd-streaming-cold` 表达「我要测量」的意图，更清晰。

**练习 2**：为什么「基于受欢迎度（popularity-based）」的预加载对 DeepSeek V4 特别有效？

**答案**：MoE 的 router 学到的专家使用频率高度长尾——少量热门专家被频繁选中，大量冷门专家极少被选。预热 top-hot 的一批，能在启动后立即拿到大部分命中，把冷启动的 SSD miss 限制在长尾上。这依赖 u4-l1 讲过的 router softmax/top-k 选择机制。

---

## 5. 综合实践

把三个模块串起来，完成一次「为指定机器规划 SSD 流式缓存」的纸面推演：

**场景**：一台 128GB 统一内存的 Mac，运行 PRO q2 GGUF（每层 384 个 routed 专家，单专家约 `per_expert_bytes = B`，43 层），用户直接 `./ds4 -m ... --ssd-streaming`（不指定缓存）。

**任务**：

1. 写出自动预算的完整公式，标出 `recommended`、`non_routed_bytes`、`per_expert_bytes`、`max_model_experts` 各自的来源（对应 4.1.3 的四个函数）。
2. 假设 `recommendedMaxWorkingSetSize ≈ 100 GiB`、`non_routed_bytes ≈ 20 GiB`、单专家 `≈ 1.5 MiB`，手算 `cache_experts` 与 `effective_cache_bytes`（答案：`model_target=80GiB`，`cache_bytes=60GiB`，`cache_experts = 60GiB/1.5MiB ≈ 40960`，但封顶到 `DS4_N_LAYER*DS4_N_EXPERT = 43*384 = 16512`，所以最终 16512 个专家、约占 24.2 GiB）。
3. 接着回答：若运行时 mlock 在锁到 18 GiB 时失败，缓存会被裁到多少专家？（套用 4.2 的 `(已锁字节 − 1GiB)/单专家字节`，约 `17GiB/1.5MiB ≈ 11377` 个，并受已锁槽位数夹取。）
4. 最后说明：默认启动会预热多少专家？（4.3：自动模式封顶 4096。）

完成后，你应当能用一张图把「Metal 推荐工作集 → 80% → 减非 routed → 换算专家数 → mlock 裁剪 → hot 预热」整条规划链画出来。

## 6. 本讲小结

- SSD 流式的**规划层**集中在 `ds4_ssd.{h,c}`，是纯数学、零 GPU 代码；运行时（分配、按需读取、mlock）在 ds4_metal.m 与 ds4.c 图代码里。
- 自动预算 `ds4_ssd_auto_cache_plan` 的核心是 `cache_experts = floor((recommended×0.8 − non_routed)/per_expert)`，下保底 1、上封顶 `N_LAYER×N_EXPERT`；计量单位始终是「完整专家个数」。
- 必须区分两种 mlock：ds4_ssd.c 的 `ds4_ssd_memory_lock_acquire` 是 `--simulate-used-memory` 的**诊断**工具；真正运行时锁专家缓冲、并在失败时把预算**裁剪到可锁定上限**的是 ds4_metal.m 的 `ds4_gpu_stream_expert_alloc_buffer` + `cap_budget_to_locked`。
- hot 专家预加载默认走 popularity-based 种子、自动封顶 4096，避免启动阶段几万次 pread 触发看门狗；`--ssd-streaming-preload-experts N` 显式绕过封顶，`--ssd-streaming-cold` 完全跳过预热。
- 专家缓存是「单一 size-class 的 slab 分配器」，尺寸由第一个 routed 层决定；混合精度的「大专家」永远进不了缓存。
- README 的官方建议是「先用自动预算，保守起步；有 headroom 再加大，且只在不报 mlock 失败时加大」。

## 7. 下一步学习建议

本讲只覆盖**规划**：缓存开多大、锁多少、预热多少。一旦推理开始，专家缓存真正「跑起来」的细节——按需读取、与 shared/已缓存专家推理重叠隐藏延迟、prefill 下一层预取、hotlist 驱逐——是 **u9-l2《SSD 流式推理路径与延迟隐藏》** 的主题，建议直接接着读，重点看 `ds4_streaming_hotlist.inc` 与 ds4.c 的流式前向路径。

若你想暂时离开 SSD 主题，可以先读 **u9-l3《分布式推理架构与层切分》** 看另一种「模型太大一台装不下」的解法（跨机切层而非单机流式），对比两种取舍。读完 u9 全单元后，回头再读 [README.md:182-268](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L182-L268) 的 SSD 实战段落，把规划层的数学和用户视角的调参经验对齐。
