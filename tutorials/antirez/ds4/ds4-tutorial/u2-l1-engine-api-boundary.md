# ds4.h：引擎边界与生命周期

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `ds4_engine`（已加载的模型）与 `ds4_session`（一条可变的推理时间线）这两条公共边界的职责划分，以及为什么 ds4 故意把它们拆开。
- 读懂 `ds4_engine_options` 这个“打开引擎用的配置包”，并能按 **路径 / 后端 / 推理控制 / SSD 流式 / 分布式 / 功耗 / 检查** 给它的字段分类。
- 理清 `ds4_engine_open` → `ds4_engine_summary` → `ds4_engine_close` 这条生命周期主线，知道每个函数在哪一步、消费了哪些字段。

本讲是 u2 单元（引擎公共 API 与 CLI）的第一篇，承接 u1-l3 建立的“源码地图”。它只盯住一个文件：`ds4.h`（公共头），再配合 `ds4.c` 里三个生命周期函数的实现。**本讲不展开推理内核、采样、KV 缓存的算法**，那是后续讲义的内容。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：推理引擎里有两类完全不同的“重物”。**

- 一类是**模型权重**：动辄几十到几百 GB，一旦加载进来基本不变。它是“只读”的。
- 一类是**推理状态**：当前这段对话已经处理到了第几个 token、KV 缓存里存了哪些层的结果、上一步算出的 logits（词表概率分布）。它是“可变”的，每生成一个 token 都会变。

把这两类东西塞进同一个结构体，是新手常犯的错误。ds4 用两个类型把它们隔开：`ds4_engine` 装第一类（模型），`ds4_session` 装第二类（一条对话的推理时间线）。

**直觉二：“打开模型”和“跑一次推理”是两个独立的动作。**

打开模型（`ds4_engine_open`）很贵：要 mmap 几十 GB 的 GGUF 文件、加载词表、把张量绑定到层、初始化 GPU。这件事你只愿意在每个进程里做一次。而跑一次推理（创建 session、喂 prompt、生成 token）是高频操作。把它们拆成 engine / session 两层后，同一个 engine 可以被多个 session 共享，权重不需要重复加载。

**直觉三：公共头要“窄”。**

`ds4.h` 是 CLI（`ds4_cli.c`）、服务器（`ds4_server.c`）、agent（`ds4_agent.c`）等所有前端都要 include 的头文件。如果它把张量内部结构、GPU 命令缓冲等细节暴露出去，前端代码就会和引擎实现死死耦合，后续重构一动全动。所以 ds4 故意把 `ds4_engine` 和 `ds4_session` 写成**不透明指针**（opaque pointer）：头里只有 `typedef struct ds4_engine ds4_engine;`，结构体的字段全部藏在 `ds4.c` 里。

> 术语速查：
> - **GGUF**：ds4 使用的模型文件格式（ds4 在 MIT 协议下源码级继承了 GGML 的 GGUF 量化布局）。
> - **mmap**：把文件“映射”进内存，按需从磁盘读页，避免一次性把几十 GB 全读进内存。ds4 用它加载权重。
> - **opaque pointer（不透明指针）**：头里只声明 `struct ds4_engine;`，不暴露字段；调用方只能拿到指针、调用函数，不能直接读字段。
> - **logits**：模型对词表里每个 token 输出的原始分数，采样时把它变成下一个 token。
> - **prefill / decode**：prefill 是一次性把 prompt 填进 KV 缓存（决定首 token 延迟）；decode 是逐 token 自回归生成（决定生成速度）。详见 u1-l5。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 角色 | 本讲关注的内容 |
| --- | --- | --- |
| `ds4.h` | 公共头，所有前端 include 它 | `ds4_engine`/`ds4_session` 不透明声明、`ds4_engine_options` 结构体、生命周期函数声明、各种枚举 |
| `ds4.c` | 引擎核心实现（约 2.7 万行） | `ds4_engine_open`（L25546）、`ds4_engine_summary`（L25977）、`ds4_engine_close`（L26021）、`ds4_session_create`（L26039） |

辅助参考 `ds4_cli.c`：它是 `ds4_engine_options` 最典型的“填表人”，能让你看到这些字段在真实程序里是怎么被赋值的（默认值 L1392、`parse_backend` L165、`default_backend` L182、open/close 调用点 L1652）。

## 4. 核心概念与源码讲解

### 4.1 engine vs session：两条公共边界

#### 4.1.1 概念说明

`ds4.h` 顶部有一段注释，是理解整个公共 API 的钥匙：

> The CLI and server should treat `ds4_engine` as the loaded model and `ds4_session` as one mutable inference timeline. A session owns the live KV cache and logits; callers provide full token prefixes and let `ds4_session_sync()` reuse, extend, or rebuild the graph state. Keep this header narrow so HTTP/CLI code does not depend on tensor internals.

把这段话翻译成职责表：

| | `ds4_engine` | `ds4_session` |
| --- | --- | --- |
| 含义 | 已加载的模型 | 一条可变的推理时间线（一段对话） |
| 生命周期 | 进程级，通常只 open 一次 | 对话级，可以反复 create/free |
| 持有什么 | 权重、词表、GPU 设备、模型形状常量 | 活的 KV 缓存、当前 logits、当前位置指针 |
| 可变性 | 基本只读（功耗档位等少数旋钮可调） | 每生成一个 token 都在变 |
| 数量关系 | 1 个 engine | 可挂多个 session |

关键设计点：**调用方永远提供“完整的 token 前缀”**，由 `ds4_session_sync()` 内部决定是“复用现有 KV、只增量评估后缀”，还是“整个重建”。这样前端代码不需要手动维护“当前缓存到了第几个 token”这种脆弱状态——它只要把“我希望这段对话最终是这串 token”交给引擎即可。

为什么不直接暴露结构体字段？因为 `ds4.h` 要被 HTTP 服务器、CLI、agent 同时依赖，一旦字段暴露，任何引擎内部重构都会波及所有前端。用不透明指针 + 函数访问器，引擎实现可以自由演进。

#### 4.1.2 核心流程

一个典型 ds4 进程的公共 API 调用顺序（伪代码）：

```
ds4_engine_options opt = { .model_path = "ds4flash.gguf", .backend = ..., ... };
ds4_engine *e;
ds4_engine_open(&e, &opt);          # 1. 装载模型（贵，只做一次）

ds4_session *s;
ds4_session_create(&s, e, ctx_size); # 2. 开一条对话时间线
ds4_session_sync(s, &prompt_tokens); # 3. 把 prompt 同步进 KV（复用/增量/重建）
while (生成中) {
    int tok = ds4_session_sample(s, ...);  # 4. 采样下一个 token
    ds4_session_eval(s, tok, ...);         # 5. 把它喂回去，前进一步
}
ds4_session_free(s);                 # 6. 丢掉这条时间线（权重还在 engine 里）

ds4_engine_close(e);                 # 7. 进程退出前卸载模型
```

注意第 6 步：`ds4_session_free` 只释放 KV 缓存和 logits，**不动权重**。所以你可以 create 一个 session、free 掉、再 create 一个，权重始终只加载一次。

#### 4.1.3 源码精读

公共边界的不透明声明——两个 `typedef`，没有字段，所以前端只能拿指针：

```c
typedef struct ds4_engine ds4_engine;
typedef struct ds4_session ds4_session;
```
来源：[ds4.h:L59-L60](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L59-L60) —— 这两行就是“窄边界”的物理体现。

顶部那段定义边界的注释：[ds4.h:L11-L17](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L11-L17) —— 中文说明：明确 engine = 模型、session = 推理时间线，并要求保持头文件“窄”。

`ds4_session_create` 的实现最能体现“session 只持有 KV + logits，权重来自 engine”。它的 GPU 分支：

```c
ds4_session *s = xcalloc(1, sizeof(*s));
s->engine = e;                       # session 反向引用 engine（共享权重）
s->ctx_size = ctx_size;
...
metal_graph_alloc_raw_cap(&s->graph, &e->weights, ...);  # 分配 KV 缓存
s->logits = xmalloc(DS4_N_VOCAB * sizeof(s->logits[0])); # 分配 logits
```
来源：[ds4.c:L26062-L26093](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26062-L26093) —— 中文说明：session 通过 `s->engine = e` 共享 engine 的权重（`&e->weights`），自己只 `xmalloc` 出 KV 图和 logits。

`ds4_session_sync` 的声明注释进一步点明了“复用/增量/重建”三选一的语义：[ds4.h:L246-L249](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L246-L249)（这一步的具体算法在 u2-l3 详讲，本讲只点出它的存在）。

#### 4.1.4 代码实践

**实践目标**：用源码验证“engine 持有模型形状常量、session 不持有权重”。

**操作步骤**：

1. 打开 `ds4.c`，看这几个 engine 访问器：
   - `ds4_engine_layer_count`（L26000）直接 `return DS4_N_LAYER;`
   - `ds4_engine_model_name`（L25995）直接 `return DS4_MODEL_SHAPE_NAME;`
   - `ds4_engine_vocab_size`（L25981）返回 `e->vocab.n_vocab`。
2. 打开 `ds4_session_create`（L26039），确认它把 `s->engine = e`，并且只分配 `s->graph`（KV）和 `s->logits`，没有任何 `weights_*` 分配。

**需要观察的现象**：engine 访问器返回的是**编译期常量**或 engine 自身字段；session 结构里没有自己的权重副本，只有指向 engine 的指针。

**预期结果**：你会清楚看到“权重在 engine、KV/logits 在 session”这条物理分界线。

> 本实践为“源码阅读型实践”，不需要运行命令，也不需要 GPU。

#### 4.1.5 小练习与答案

**练习 1**：如果同一个 engine 上同时挂两个 session，两个 session 会各自加载一份权重吗？

> **答案**：不会。`ds4_session_create` 里 `s->engine = e`，KV 和 logits 在 session 上各自分配，但权重（`e->weights`）只有 engine 那一份，被两个 session 共享只读。

**练习 2**：为什么 `ds4.h` 里 `ds4_engine` 用 `typedef struct ds4_engine ds4_engine;` 而不是直接写出结构体字段？

> **答案**：为了让结构体“不透明”。前端只能拿到指针、调用 `ds4_engine_*` 函数，看不到字段，从而引擎内部重构字段时不会 forcing 前端重编译或改代码。这就是注释里“keep this header narrow”的意思。

---

### 4.2 ds4_engine_options：打开引擎用的配置包

#### 4.2.1 概念说明

`ds4_engine_options` 是调用 `ds4_engine_open` 时传进去的“配置包”。它是一个**纯值结构体**（POD），里面是一堆字段，描述“我想用哪个模型文件、哪个后端、开不开 SSD 流式、是不是分布式……”等所有“开机选项”。

它的设计有两个特点：

1. **集中**：所有打开引擎需要的选项都堆在这一个结构体里，而不是搞成二十个函数参数。这样加新选项时只改结构体，调用方的 `ds4_engine_open(&e, &opt)` 签名不变。
2. **嵌套**：分布式相关的选项单独抽成一个子结构体 `ds4_distributed_options`，再以 `ds4_distributed_options distributed;` 嵌进来。这让“分布式”这一整族选项可以被独立创建/释放（CLI 里用 `ds4_dist_options_create()` 分配）。

#### 4.2.2 核心流程

配置包的流动是这样的：

```
前端（CLI/server/agent）
   │  按需填字段：model_path、backend、power_percent、ssd_streaming ...
   ▼
ds4_engine_options opt
   │  作为单一参数传入
   ▼
ds4_engine_open(&e, &opt)
   │  逐字段消费：拷贝进 engine 结构体 / 校验 / 触发副作用
   ▼
ds4_engine e（内部结构，字段藏在 ds4.c）
```

`ds4_engine_open` 对 `opt` 的处理可以分成几类（这也是本讲实践任务要你画的分类表）：

- **直接拷贝**进 engine：`backend`、`quality`、`ssd_streaming`、`power_percent`、`prefill_chunk`……
- **带钳位（clamp）地拷贝**：`power_percent` 钳到 (0, 100]、`mtp_draft_tokens` 钳到 [1, 16]、`mtp_margin` 缺省 3.0。
- **触发副作用**：`model_path` 触发 `model_open`（mmap 加载）；`n_threads` 写全局变量 `g_requested_threads`；`simulate_used_memory_bytes` 触发一次内存锁占用。
- **分支跳过**：`inspect_only` 为真时，跳过词表加载、跳过 GPU 初始化，提前 return。

#### 4.2.3 源码精读

`ds4_engine_options` 结构体定义（这是本讲最核心的一段代码，逐字段都真实存在）：

```c
typedef struct {
    const char *model_path;                 /* 路径：主模型 GGUF */
    const char *mtp_path;                   /* 路径：MTP 投机解码小模型 */
    ds4_backend backend;                    /* 后端：Metal/CUDA/CPU */
    int n_threads;                          /* 后端：CPU 线程数 */
    uint32_t prefill_chunk;                 /* 推理控制：prefill 分块 */
    int mtp_draft_tokens;                   /* 推理控制：MTP draft token 数 */
    float mtp_margin;                       /* 推理控制：MTP 置信度门控 */
    const char *directional_steering_file;  /* 路径：方向性引导向量 */
    const char *expert_profile_path;        /* 路径：expert profile */
    float directional_steering_attn;        /* 推理控制：attention 引导强度 */
    float directional_steering_ffn;         /* 推理控制：FFN 引导强度 */
    int power_percent;                      /* 功耗：节流百分比 */
    uint32_t ssd_streaming_cache_experts;   /* SSD：缓存的 expert 数 */
    uint64_t ssd_streaming_cache_bytes;     /* SSD：缓存字节数预算 */
    uint32_t ssd_streaming_preload_experts; /* SSD：预加载 hot expert 数 */
    uint64_t simulate_used_memory_bytes;    /* 内存模拟：假装已占用 */
    bool warm_weights;                      /* 后端：预热权重页 */
    bool quality;                           /* 推理控制：高质量档 */
    bool ssd_streaming;                     /* SSD：是否开启流式 */
    bool ssd_streaming_cold;                /* SSD：冷缓存模式 */
    bool inspect_only;                      /* 检查：只加载不推理 */
    bool load_slice;                        /* 分布式：只加载层片 */
    uint32_t load_layer_start;              /* 分布式：层片起点 */
    uint32_t load_layer_end;                /* 分布式：层片终点 */
    bool load_output;                       /* 分布式：是否含 output head */
    ds4_distributed_options distributed;    /* 分布式：嵌套子配置 */
} ds4_engine_options;
```
来源：[ds4.h:L94-L121](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L94-L121) —— 中文说明：ds4 打开引擎的全部选项都在这里；行内中文注释是本讲为方便分类加的（项目原代码无此注释，属于“示例代码”式的标注）。

嵌套的分布式子配置（注意它有自己的 `role`、`layers`、连接地址等）：

```c
typedef struct {
    ds4_distributed_role role;       /* NONE / COORDINATOR / WORKER */
    ds4_distributed_layers layers;   /* start/end/has_output 层区间 */
    const char *listen_host; int listen_port;
    const char *coordinator_host; int coordinator_port;
    uint32_t prefill_chunk; uint32_t prefill_window;
    uint32_t activation_bits;
    bool replay_check; bool debug;
} ds4_distributed_options;
```
来源：[ds4.h:L80-L92](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L80-L92) —— 中文说明：分布式选项单独成块，便于 CLI 用 `ds4_dist_options_create/free` 独立管理（详见 u9 单元）。

后端枚举只有三个值（ROCm 复用了 `DS4_BACKEND_CUDA` 这一位，靠编译期宏区分，见 u1-l4）：

```c
typedef enum { DS4_BACKEND_METAL, DS4_BACKEND_CUDA, DS4_BACKEND_CPU } ds4_backend;
```
来源：[ds4.h:L19-L23](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L19-L23)。

CLI 里这套选项的默认值（最典型的“填表人”），可以看到只填了三个字段，其余全靠结构体零值：

```c
.engine = {
    .model_path = "ds4flash.gguf",
    .backend = default_backend(),
    .mtp_draft_tokens = 1,
    .mtp_margin = 3.0f,
},
```
来源：[ds4_cli.c:L1394-L1399](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1394-L1399) —— 中文说明：CLI 默认模型路径是 `ds4flash.gguf`（这也是 `download_model.sh` 下完主模型后软链的名字，见 u1-l5），后端由 `default_backend()` 按平台决定。

`default_backend()` 把“编译期宏”和“运行期默认后端”串起来：

```c
static ds4_backend default_backend(void) {
#ifdef DS4_NO_GPU
    return DS4_BACKEND_CPU;
#elif defined(__APPLE__)
    return DS4_BACKEND_METAL;
#else
    return DS4_BACKEND_CUDA;
#endif
}
```
来源：[ds4_cli.c:L182-L190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L182-L190) —— 中文说明：CPU 构建默认 CPU、Mac 默认 Metal、Linux 默认 CUDA。这呼应了 u1-l4 讲的“后端可替换、引擎核心稳定”。

#### 4.2.4 代码实践

**实践目标**：把 `ds4_engine_options` 的字段按类别分类，并指出哪些会被 `ds4_engine_open` 直接消费（答案：**几乎全部**）。

**操作步骤**：

1. 打开 [ds4.h:L94-L121](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L94-L121)，按下表把每个字段归类。
2. 打开 `ds4_engine_open`（[ds4.c:L25546](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25546)）逐字段核对，确认它确实读了该字段。

**参考分类表**（实践产出应类似这样）：

| 类别 | 字段 |
| --- | --- |
| 路径 | `model_path`、`mtp_path`、`directional_steering_file`、`expert_profile_path` |
| 后端 | `backend`、`n_threads`、`warm_weights` |
| 推理控制 | `prefill_chunk`、`mtp_draft_tokens`、`mtp_margin`、`directional_steering_attn`、`directional_steering_ffn`、`quality` |
| SSD 流式 | `ssd_streaming`、`ssd_streaming_cold`、`ssd_streaming_cache_experts`、`ssd_streaming_cache_bytes`、`ssd_streaming_preload_experts` |
| 分布式 | `load_slice`、`load_layer_start`、`load_layer_end`、`load_output`、`distributed` |
| 功耗 | `power_percent` |
| 内存模拟 / 检查 | `simulate_used_memory_bytes`、`inspect_only` |

**需要观察的现象**：`ds4_engine_open` 开头那几十行（L25550-L25577）几乎逐行拷贝/钳位了上面每一个字段。

**预期结果**：你会得出结论——**`ds4_engine_options` 的每一个字段都在 `ds4_engine_open` 里被消费**，没有“填了却没人读”的死字段。例如 `model_path` 在 [ds4.c:L25606](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25606) 传给 `model_open`，`inspect_only` 在 [ds4.c:L25673-L25676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25673-L25676) 触发提前 return。

> 本实践为源码阅读型；如需运行验证，可执行 `./ds4 --inspect -m <模型>`（`--inspect` 即把 `inspect_only` 置真），观察它只打印模型摘要、不分配 KV、不推理。

#### 4.2.5 小练习与答案

**练习 1**：为什么分布式选项要单独抽成 `ds4_distributed_options` 嵌进来，而不是平铺？

> **答案**：因为分布式是一整族相关选项（角色、层区间、监听/连接地址、prefill 窗口……），而且它需要独立的生命周期管理——CLI 里用 `ds4_dist_options_create()` 分配、`ds4_dist_options_free()` 释放。嵌套成子结构体让这族选项可以成组传递和回收，也避免 `ds4_engine_options` 顶层被几十个 `dist_*` 字段撑爆。

**练习 2**：`power_percent` 字段填 0 会发生什么？填 200 呢？

> **答案**：看 [ds4.c:L25555-L25560](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25555-L25560)：`opt->power_percent > 0 ? opt->power_percent : 100`，所以填 0 会被当成 100（即默认满功耗）；随后 `if (e->power_percent > 100) e->power_percent = 100;` 把 200 钳回 100。这就是“带钳位地拷贝”。

---

### 4.3 生命周期函数：open / summary / close

#### 4.3.1 概念说明

engine 的生命周期由三个公共函数控制：

| 函数 | 作用 | 贵不贵 |
| --- | --- | --- |
| `ds4_engine_open(&out, &opt)` | 装载模型：mmap、词表、绑定权重、初始化 GPU、拿进程锁 | **非常贵**，几十 GB 级别的 I/O |
| `ds4_engine_summary(e)` | 打印模型摘要（形状、层数、量化等） | 便宜，只是 fprintf |
| `ds4_engine_close(e)` | 释放一切：权重、词表、GPU、模型 fd、进程锁 | 中等 |

注意签名细节：

- `open` 用**二级指针** `ds4_engine **out` 而不是返回值，因为返回值被用来表示成功/失败（0 成功，非 0 失败）。这是 C 里常见的“用 int 返回状态、用出参回传对象”的写法。
- `open` 失败时，函数内部已经会自己调用 `ds4_engine_close(e)` 清理半成品，并把 `*out = NULL`。所以**调用方在 open 失败时不要再去 close**。

`ds4_engine_open` 内部还有一个重要副作用：`ds4_acquire_instance_lock()`。ds4 在一个进程里只允许打开一个 engine 实例（GPU 资源独占），这个锁在 open 时获取、在 close 时释放。

#### 4.3.2 核心流程

```
ds4_engine_open(&e, &opt)
├─ xcalloc engine 结构体
├─ 把 opt 字段拷贝/钳位进 e
├─ ds4_acquire_instance_lock()           # 进程级单例锁
├─ [可选] simulate_used_memory_bytes → 占内存锁
├─ model_open(&e->model, model_path)     # mmap 主模型
├─ model_warm_weights() / vocab_load() / config_validate_model()
├─ weights_bind(&e->weights, ...)        # 张量绑定到层
├─ [inspect_only] → 提前 return 0
├─ [mtp_path] → model_open MTP + mtp_weights_bind
├─ [graph_backend] → ds4_gpu_init() + 配置 SSD expert 缓存预算
└─ return 0；*out = e

  … 中间用 e 跑各种 session …

ds4_engine_close(e)
├─ weights_free / vocab_free
├─ model_close(mtp) / model_close(model)
├─ ds4_gpu_cleanup()
├─ ds4_ssd_memory_lock_release / ds4_release_instance_lock
└─ free(e)
```

`ds4_engine_summary` 极其简单——它只是把活儿全转给内部的 `model_summary`：

```c
void ds4_engine_summary(ds4_engine *e) { model_summary(&e->model); }
```

#### 4.3.3 源码精读

`ds4_engine_open` 的开头——拷贝/钳位字段、拿单例锁（这段是 4.2 节分类表的“消费证据”）：

```c
int ds4_engine_open(ds4_engine **out, const ds4_engine_options *opt) {
    ds4_engine *e = xcalloc(1, sizeof(*e));
    e->model.fd = -1;
    e->mtp_model.fd = -1;
    e->backend = opt->backend;
    e->quality = opt->quality;
    e->ssd_streaming = opt->ssd_streaming;
    ...
    e->power_percent = opt->power_percent > 0 ? opt->power_percent : 100;
    ...
    if (opt->n_threads > 0) g_requested_threads = (uint32_t)opt->n_threads;
    ds4_acquire_instance_lock();
```
来源：[ds4.c:L25546-L25578](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25546-L25578) —— 中文说明：open 一上来就把 `opt` 的字段逐个搬进 engine，并拿进程级单例锁（`ds4_acquire_instance_lock`）。

真正“装载模型”的三连——mmap、词表、校验：

```c
model_open(&e->model, opt->model_path, graph_backend, !opt->inspect_only);
if (opt->warm_weights) model_warm_weights(&e->model);
if (!opt->inspect_only) vocab_load(&e->vocab, &e->model);
config_validate_model(&e->model);
```
来源：[ds4.c:L25606-L25609](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25606-L25609) —— 中文说明：`model_open` 用 mmap 把 GGUF 映射进来；`inspect_only` 为真时跳过词表加载（`!opt->inspect_only` 为假）；`config_validate_model` 校验模型结构与 ds4 期望一致。

inspect 分支的提前返回（4.2 实践里 `inspect_only` 字段的落点）：

```c
if (opt->inspect_only) {
    *out = e;
    return 0;
}
```
来源：[ds4.c:L25673-L25676](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25673-L25676) —— 中文说明：只检查模型时，到这里就把 engine 交出去，不再初始化 GPU、不分配 KV。

`ds4_engine_summary` 全文（一行委托）：

```c
void ds4_engine_summary(ds4_engine *e) {
    model_summary(&e->model);
}
```
来源：[ds4.c:L25977-L25979](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25977-L25979)。

`ds4_engine_close` 全文——按“后申请先释放”的逆序释放资源：

```c
void ds4_engine_close(ds4_engine *e) {
    if (!e) return;
    ds4_expert_profile_close();
    weights_free(&e->weights);
    vocab_free(&e->vocab);
    ds4_threads_shutdown();
    if (e->mtp_ready) model_close(&e->mtp_model);
    model_close(&e->model);
#ifndef DS4_NO_GPU
    ds4_gpu_cleanup();
#endif
    ds4_ssd_memory_lock_release(&e->simulated_memory);
    ds4_release_instance_lock();
    free(e->directional_steering_dirs);
    free(e->directional_steering_file);
    free(e);
}
```
来源：[ds4.c:L26021-L26037](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26021-L26037) —— 中文说明：close 释放权重/词表/模型 fd/GPU，最后释放单例锁（`ds4_release_instance_lock`）。注意它对 `e == NULL` 是安全的（`if (!e) return;`），所以 open 失败设的 `*out = NULL` 即便误传进来也不会崩。

CLI 里的完整生命周期调用点（最能说明这条主线怎么被真实使用）：

```c
cfg.engine.inspect_only = cfg.inspect;
ds4_engine *engine = NULL;
if (ds4_engine_open(&engine, &cfg.engine) != 0) { ... return 1; }
...
if (cfg.inspect) {
    ds4_engine_summary(engine);          # --inspect 模式：只打印摘要
} else if (...) {
    ...                                  # 其它模式：REPL / 一次性生成 / imatrix ...
}
ds4_engine_close(engine);
```
来源：[ds4_cli.c:L1652-L1703](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1652-L1703) —— 中文说明：CLI 的标准三段式——填 `inspect_only` → `open`（失败直接退出）→ 按模式分流（`--inspect` 走 `summary`）→ `close`。

`ds4.c` 里这段的 section 标题也点明了 open 的本质——拿锁、mmap、暴露 tokenization：

```
/* Engine API and Process Lock.
 * The public entry points acquire the single instance lock, open the GGUF with
 * the backend-appropriate mmap policy, and expose tokenized prompt operations
 * to the CLI and server. */
```
来源：[ds4.c:L23112-L23119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23112-L23119)。

#### 4.3.4 代码实践

**实践目标**：亲手走一遍“open → summary → close”的最短路径，验证生命周期函数的行为。

**操作步骤**：

1. 确认你有一个 GGUF 模型文件（或先用 `--inspect` 跑一个真实文件）。
2. 执行（**待本地验证**——取决于你是否有模型文件）：

   ```bash
   ./ds4 --inspect -m ds4flash.gguf
   ```

   `--inspect` 把 `cfg.engine.inspect_only` 置真（[ds4_cli.c:L1652](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1652)），于是 open 在 [ds4.c:L25673](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25673) 提前返回，main 走 `ds4_engine_summary(engine)` 分支。
3. 如果暂时没有模型：退化为源码阅读型实践——在 `ds4_cli.c` 的 `parse_options`（[L1392](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1392)）里追踪 `cfg.inspect` 是怎么被 `--inspect` 置真的，并确认它在 L1687 让程序走 summary 分支而非推理分支。

**需要观察的现象**：

- `--inspect` 模式下程序应**只打印模型摘要**（层数、形状、量化等），不分配 KV、不进入 REPL、不生成 token。
- 程序正常退出，没有“GPU unavailable”之类的报错（因为 inspect 分支在 GPU init 之前就 return 了）。

**预期结果**：你会直观看到“open 可以走轻量 inspect 路径”——这正是 `inspect_only` 字段和 4.3.3 里那条提前 return 的价值：**不初始化 GPU 就能查看模型信息**。

#### 4.3.5 小练习与答案

**练习 1**：`ds4_engine_open` 返回非 0 表示失败。失败后，调用方需要 `ds4_engine_close(*out)` 吗？

> **答案**：不需要。open 内部失败时已经自己调用 `ds4_engine_close(e)` 清理半成品，并把 `*out = NULL`（例如 [ds4.c:L25583-L25585](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25583-L25585)）。而 close 对 `e == NULL` 是安全的，所以即便误调也不会崩，但属于多余动作。

**练习 2**：为什么 `ds4_engine_open` 用 `ds4_engine **out`（二级指针）而不是 `ds4_engine *ds4_engine_open(...)` 直接返回 engine？

> **答案**：因为返回值 `int` 已经被用来表示成功/失败（0/非 0）。用出参回传对象、用返回值回传状态，是 C 里在没有异常/多返回值时的常见约定，让错误处理统一且明确。

**练习 3**：`ds4_engine_summary` 为什么只有一行？它把活儿交给了谁？

> **答案**：它把活儿全委托给内部的 `model_summary(&e->model)`（[ds4.c:L25978](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L25978)）。这体现了公共 API 是“薄包装”：`ds4_engine_summary` 只是把“对 engine 的请求”转成“对内部 model 的调用”，避免前端直接碰内部函数。

---

## 5. 综合实践

把本讲三个模块串起来的小任务：**画一张 ds4 引擎的“打开与使用”时序图**。

要求：

1. 列出一个 CLI 进程从启动到退出，对公共 API 的全部调用顺序（参考 4.1.2 的伪代码）。
2. 在每个调用旁边标注：它属于 engine 还是 session？它消费了 `ds4_engine_options` 的哪类字段（用 4.2.4 的分类表）？
3. 标出“贵的”操作（`ds4_engine_open` 里的 `model_open`/`weights_bind`/`ds4_gpu_init`）和“便宜的”操作（`ds4_engine_summary`）分别在时序的哪一步。
4. 用一句话回答：为什么 ds4 把 `--inspect` 实现成“open 的一个分支”而不是一个独立命令？

**参考答案要点**：

- 时序：填 `opt` → `ds4_engine_open`（贵：mmap + 绑定 + 可能的 GPU init）→ `ds4_session_create` × N → `sync`/`eval`/`sample` → `ds4_session_free` × N → `ds4_engine_close`。
- engine 消费 model_path/backend/power_percent 等几乎所有字段；session 不消费 `ds4_engine_options`，它只接收 `ctx_size`。
- `--inspect` 复用 open 的 mmap/校验逻辑，只是通过 `inspect_only` 在 GPU init 之前提前 return，从而“不重复造一个只读模型加载器”。这是“配置包 + 分支”设计带来的复用红利。

## 6. 本讲小结

- ds4 的公共边界是两条不透明类型：**`ds4_engine` = 已加载模型（进程级、基本只读、持权重），`ds4_session` = 一条可变推理时间线（对话级、持 KV 缓存和 logits）**。权重在 engine、状态在 session。
- `ds4_engine_options` 是“打开引擎用的配置包”，集中了路径/后端/推理控制/SSD 流式/分布式/功耗/检查七大类选项；分布式选项嵌套成 `ds4_distributed_options` 子结构体。
- `ds4_engine_open` **几乎逐字段消费** `ds4_engine_options`，处理方式分四种：直接拷贝、带钳位拷贝、触发副作用（mmap/GPU init/拿锁）、分支跳过（`inspect_only`）。
- 生命周期主线 `open → summary → close`：open 拿进程单例锁并装载模型（贵），summary 只是一行委托给 `model_summary`（便宜），close 按逆序释放权重/词表/GPU/锁并对 NULL 安全。
- open 用二级指针出参 + int 状态返回；**open 失败时内部已自清理，调用方不要再 close**。
- 公共头刻意保持“窄”（不透明指针 + 函数访问器），让 CLI/服务器/agent 三个前端不依赖张量内部结构。

## 7. 下一步学习建议

- **u2-l2 CLI 主流程**：本讲只看了 open/close 的调用点，下一篇会完整追踪 `ds4_cli.c` 的 `main`——参数怎么解析、`-p` 一次性推理和交互 REPL 两条分支怎么走、信号如何协作式中断生成。
- **u2-l3 Session 同步与前缀复用**：本讲多次提到 `ds4_session_sync` 的“复用/增量/重建”三选一，下一篇正是拆开它的前缀匹配算法（`ds4_session_common_prefix`、`ds4_session_rewrite_requires_rebuild`）。
- **延伸阅读**：想提前了解 engine 内部“装载模型”的两个关键步骤，可以读 `ds4.c` 里的 `model_open`（mmap，u3-l1 详讲）和 `weights_bind`（张量绑定，u3-l2 详讲）。本讲刻意没展开它们，是为了让边界先立住。
