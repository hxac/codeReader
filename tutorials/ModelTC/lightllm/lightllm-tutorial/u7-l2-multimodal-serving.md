# 多模态推理服务

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 LightLLM 中「多模态请求」从 HTTP 进入、到被拆成「文本交给 LLM、图像/音频交给独立的视觉/音频进程」的完整流转路径。
- 理解 `visualserver` / `audioserver` 作为**独立进程**的职责：它们各自持有 ViT / 音频编码器权重，负责把原始图像/音频算成嵌入（embedding），并通过 zmq 链式转交给下一个模块。
- 掌握 `embed_cache` 如何用 **MD5 + 引用计数 + LRU** 把同一张图的嵌入只算一次、反复复用，以及它如何用「data_ready / embed_ready 两个就绪位」协调「原图字节」与「算好的嵌入」两种共享内存资源。
- 能够跟踪一次「图片 + 文本」请求的源码调用链，并解释缓存命中与未命中时分别会发生什么。

## 2. 前置知识

本讲是 **advanced** 级别，假设你已经读过：

- **u2-l1 多进程架构总览**：LightLLM 把推理拆成 HttpServer / Router / ModelBackend / Detokenization 等进程，用 zmq（通知）、rpyc（远程调用）、共享内存（大数据）三类 IPC 协作。
- **u2-l2 HTTP API 服务与请求分发**：HttpServer 用 `transfer_to_next_module` 按优先级选路，请求对象放共享内存、线上只传索引（`GroupReqIndexes`）。

在此之上补充三个本讲要用到的概念：

- **多模态（multimodal）**：模型同时接受多种输入。视觉模型（如 Qwen-VL、Llama 4）接受「图片 + 文本」，音频模型（如 Whisper）接受「音频」。模型内部通常先用一个**编码器**（ViT / 音频编码器）把图片/音频变成一串连续向量（**嵌入 embedding**），再把这串向量当作「特殊的输入 token」喂给 LLM 主体。
- **嵌入（embedding）**：一段固定维度的浮点张量。一张图经过 ViT 后会产出 `token_num × hidden_size` 的张量，这部分要塞进 LLM 的 embedding 层输出里参与后续推理。
- **MD5 摘要**：对任意长度字节算出的固定长度指纹，相同字节必然得到相同 MD5。本讲用它做「这张图我之前算过没有」的判定键。

一句话定位：多模态服务 = 在已有的「文本 → Router → LLM」主链路上，**插入一段「先把图片/音频算成嵌入」的预处理进程链**，并用一个独立的缓存进程避免重复计算。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| `lightllm/server/multimodal_params.py` | 定义 `MultimodalParams` / `ImageItem` / `AudioItem` 三种数据结构，承载一路请求里所有图片与音频及其预处理。 |
| `lightllm/server/httpserver/manager.py` | HttpServer 干活层：负责多模态资源分配（`_alloc_multimodal_resources`）与分发选路（`transfer_to_next_module`）。 |
| `lightllm/server/core/objs/io_objs/group_req.py` | 定义 `GroupReqIndexes` / `GroupReqObjs`，即线上传输的「索引 + 多模态参数」轻量对象。 |
| `lightllm/server/visualserver/manager.py` | **视觉进程**入口 `VisualManager`：收图、查缓存、按 DP 轮询分发推理、转交下一模块。 |
| `lightllm/server/audioserver/manager.py` | **音频进程**入口 `AudioManager`：结构与视觉进程对称，处理音频。 |
| `lightllm/server/visualserver/model_infer/model_rpc.py` | ViT 推理后端：真正算嵌入并把结果拷回 CPU 缓存。 |
| `lightllm/server/embed_cache/manager.py` | **缓存进程**入口 `CacheServer`：以 rpyc 暴露 `alloc/release/get_items_embed` 等接口。 |
| `lightllm/server/embed_cache/impl/naive_memory_cache.py` | 缓存实现 `InMemoryCache`：MD5 去重、引用计数、LRU 淘汰。 |
| `lightllm/server/embed_cache/embed_cache_client.py` | `CpuEmbedCacheClient`：管理承载嵌入的大块 CPU 共享内存张量。 |
| `lightllm/server/embed_cache/copy_to_cache.py` | Triton kernel：把 GPU 上的嵌入张量拷进 CPU 缓存。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**多模态服务的整体架构与请求流转**、**视觉/音频推理进程**、**嵌入缓存与 MD5 去重**。

### 4.1 多模态服务的整体架构与请求流转

#### 4.1.1 概念说明

纯文本模型的服务链路是 `HttpServer → Router → ModelBackend`（见 u2-l1）。多模态模型多了一道工序：**在进入 Router 之前，先把图片/音频变成嵌入**。LightLLM 的做法是把这道工序做成**独立进程**，并把它**插在 HttpServer 与 Router 之间，形成一条链**：

```
HttpServer ──zmq PUSH──> visualserver ──zmq PUSH──> audioserver ──zmq PUSH──> (multi_level_kv_cache?) ──> Router
```

这条链有三个关键设计：

1. **链式转发**：每个进程只知道自己「下一棒」是谁，处理完就把请求索引继续 PUSH 出去。HttpServer 并不需要知道整条链的全貌，它只挑「第一棒」。
2. **只传索引，不传大数据**：线上 zmq 投递的是 `GroupReqIndexes`（请求在共享内存中的下标 + `MultimodalParams`），图片字节、嵌入张量都走共享内存，绝不进 zmq 报文。
3. **按模型能力自动启用**：启动时根据模型目录里有没有视觉/音频模块自动决定是否拉起对应进程（详见 4.1.3）。

#### 4.1.2 核心流程

一次「图片 + 文本」请求的整体流转（仅 P / NORMAL 节点；D 节点不复算多模态）：

```text
1. HttpServer 收到 HTTP 请求，解析出 images/audios
2. MultimodalParams.verify_and_preload()  并发下载/解码/缩放图片
3. HttpServer._encode()                   把文本分词 + 占位 image token
4. _alloc_multimodal_resources()          为每张图算 MD5、向 cache 申请槽位
   └─ rpyc → CacheServer.alloc(md5sums, token_nums)
   └─ 命中：直接拿已有 uuid/token_id/start_index；未命中：分配新槽位 + 写原图字节到 shm
5. transfer_to_next_module()              按 visual→audio→multi_level→router 选第一棒
   └─ zmq PUSH GroupReqIndexes → visualserver
6. visualserver.get_need_infer_images()   查 embed_ready，只算还没算过的图
7. visualserver.infer_images()            ViT 前向 → 嵌入拷回 CPU 缓存 → set_items_embed
8. visualserver PUSH GroupReqIndexes → 下一棒（audio / router）
9. Router / LLM 推理时按 start_index_in_embed_cache 从 CPU 缓存读嵌入拼进序列
```

注意第 4 步与第 6 步都有「查缓存」，但查的是**两个不同的就绪位**：HttpServer 查 `data_ready`（原图字节是否已落 shm，避免重复传字节），visualserver 查 `embed_ready`（嵌入是否已算好，避免重复跑 ViT）。详见 4.3。

#### 4.1.3 源码精读

**（a）HttpServer 的分发选路**。`transfer_to_next_module` 是整条链的起点，按 `if/elif` 优先级挑第一棒：

[lightllm/server/httpserver/manager.py:626-662](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L626-L662) —— 仅当 `pd_mode.is_P_or_NORMAL()` 时才走多模态链路；优先 `send_to_visual`，其次 `send_to_audio`，再次 `send_to_multi_level_kv_cache`，最后兜底 `send_to_router`。D 节点直接发 router（因为 P 节点已经把多模态处理好）。

注意投递的是 `group_req_objs.to_group_req_index()`，即把「带 Req 对象」的重组件压缩成「只带共享内存下标」的轻量件：

[lightllm/server/core/objs/io_objs/group_req.py:7-28](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/core/objs/io_objs/group_req.py#L7-L28) —— `GroupReqIndexes` 只含 `group_req_id`、`multimodal_params`、`shm_req_indexes`（Req 在共享内存里的下标列表）、`time_mark`。`to_group_req_index` 把每个 Req 替换成它的 `index_in_shm_mem`，这就是「对象放共享内存、线上只传索引」的具体落地。

**（b）多模态参数对象**。`MultimodalParams` 只是一个装图片与音频的容器：

[lightllm/server/multimodal_params.py:221-237](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multimodal_params.py#L221-L237) —— 构造时把原始 dict 列表包装成 `ImageItem` / `AudioItem`；`verify_and_preload` 用 `asyncio.gather` **并发**预加载所有图片与音频（下载/解码/缩放），这是吞吐关键——多张图的解码可以并行。

**（c）图片预加载与缩放**。`ImageItem.preload` 处理三种来源（`url` / `base64` / `image_size`），并把超大图按 `max_image_pixels` 等比缩放：

[lightllm/server/multimodal_params.py:134-191](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/multimodal_params.py#L134-L191) —— 关键点是「先校验、再缩放」都在线程池 `_IMAGE_VERIFY_POOL` 里跑（解码是 C 库 libjpeg/libpng，释放 GIL，多线程真并行）；缩放算法保持长宽比、用 LANCZOS 重采样再压成 JPEG quality=96。`image_size` 类型只取宽高、不读像素，专用于「只做 token 计数」的场景。

**（d）进程拉起与自动检测**。`api_start.py` 在 `normal_or_p_d_start` 里按 `disable_vision` / `disable_audio` 决定是否拉起对应进程，这两个开关在未显式指定时会按模型目录自动判定：

[lightllm/server/api_start.py:426-461](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L426-L461) —— 先拉 `start_cache_manager`（缓存进程是多模态链路的地基，必须先于 visual/audio），再按需拉 `start_visual_process` / `start_audio_process`。进程间端口（`visual_port` / `audio_port` / `cache_port`）在更早的端口分配阶段已解包进 `args`，与 u1-l5 描述的端口分配机制一致。

> 自动检测细节：[lightllm/server/api_start.py:92-108](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/api_start.py#L92-L108) 中，当用户未显式指定 `--disable_vision` / `--disable_audio` 时，用 `has_audio_module(model_dir)` 等判定模型是否带对应模块，从而决定开关取值。

#### 4.1.4 代码实践

**实践目标**：在不动源码的前提下，跟踪一次多模态请求从 HTTP 进入到进入 visualserver 的代码路径。

**操作步骤**：

1. 打开 `lightllm/server/httpserver/manager.py`，定位 `transfer_to_next_module`（L626）。
2. 向上回溯：找到它的调用者 `transfer_to_next_module_or_node`（L608），再找到真正分配资源、构造 `group_req_objs` 的 `generate` 方法（约 L313 起），重点看其中 `_alloc_multimodal_resources`（L198）与 `_encode` 的先后顺序。
3. 向下跟踪：`send_to_visual` 这个 zmq socket 在哪里 `connect` 到 `visual_port`（在 `HttpServerManager.__init__` 里，参考 u2-l2）。
4. 在 `lightllm/server/visualserver/manager.py` 的 `loop_for_netio_req`（L174）确认 visualserver 是用 PULL 在 `visual_port` 上接收的。

**需要观察的现象**：投递对象类型是 `GroupReqIndexes`，它**不含**任何 Req 对象、也不含图片字节，只有 `shm_req_indexes` 与 `multimodal_params`（后者只含 `ImageItem` 的元信息如 `uuid`/`md5`，不含原始像素）。

**预期结果**：你能画出「HTTP handler → generate → _alloc_multimodal_resources → transfer_to_next_module → send_to_visual → visualserver.loop_for_netio_req」这条调用链，并解释为什么大对象不进 zmq。

**待本地验证**：上述调用链中 `send_to_visual` socket 的 connect 行号在不同版本可能有细微偏移，建议本地用 `grep -n "send_to_visual" lightllm/server/httpserver/manager.py` 确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 HttpServer 的 `transfer_to_next_module` 用 `if/elif`（只发一棒），而不是把请求同时发给 visual、audio、router？

**答案**：因为多模态链路是**串行依赖**的——必须先算完图片嵌入，音频/LLM 才能拿到完整的输入序列。若并行发，router 会在嵌入还没算好时就开跑。所以采用「链式转发」：HttpServer 发给第一棒，第一棒处理完自己再发给下一棒，保证顺序。

**练习 2**：D（decode）节点收到多模态请求时，会走 visual/audio 吗？

**答案**：不会。见 `transfer_to_next_module` 的 `if self.pd_mode.is_D()` 分支（L653-659），D 节点直接 `send_to_router`，因为多模态资源已在 P 节点算好并迁移过来（PD 分离见 u7-l1）。

---

### 4.2 视觉/音频推理进程

#### 4.2.1 概念说明

`visualserver` 与 `audioserver` 是两个**结构对称**的独立进程，本节以视觉进程为主讲解。它的职责很纯粹：

- 从 zmq PULL 接收 `GroupReqIndexes`；
- 查嵌入缓存，**只对未命中（embed 没算好）的图跑 ViT**；
- 把算好的嵌入拷回 CPU 共享内存缓存；
- 把请求索引 PUSH 给下一棒。

它有自己独立的并行度配置：`--visual_dp`（数据并行，把不同图分到不同 GPU）与 `--visual_tp`（张量并行，把一张图切给多 GPU）。注意这与 LLM 主模型的 `--tp`/`--dp` **完全独立**——你可以让 LLM 用 4 卡 TP、ViT 用 1 卡，反之亦可。

#### 4.2.2 核心流程

视觉进程对一批请求的处理流程（以一次 `handle_group_indexes` 为单位）：

```text
loop_for_netio_req (常驻协程, PULL 收请求)
   └─ create_task → handle_group_indexes(group_req_indexes)
        ├─ get_need_infer_images()
        │    ├─ 读 shm_req 判断 is_aborted / disable_prompt_cache
        │    ├─ cache_client.get_items_embed(uuids)   # 查 embed_ready
        │    └─ 返回 embed 未就绪的 ImageItem 列表
        ├─ if 全部已就绪 → 直接 send_to_next_module 转发
        ├─ else handle_images(need_infer)
        │    ├─ 按 cur_dp_index % visual_dp 把图轮询分到各 DP 组
        │    ├─ 每组调 infer_images(dp_index) → 各 tp rank 的 model_rpc.run_task
        │    └─ await threading.Event 等待 ViT 推理 + 拷缓存完成
        └─ send_to_next_module.send_pyobj(group_req_indexes)  # 转交下一棒
```

「下一棒」是谁由构造期决定：若启用音频则连 `audio_port`，否则若启用 CPU 缓存则连 `multi_level_kv_cache_port`，否则连 `router_port`——这正是 4.1 说的链式转发在进程内的体现。

#### 4.2.3 源码精读

**（a）VisualManager 的「下一棒」选路**。构造函数里按 `enable_audio` / `enable_cpu_cache` 决定 PUSH 连到哪个端口：

[lightllm/server/visualserver/manager.py:30-60](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/manager.py#L30-L60) —— 注意它与 HttpServer 的选路是**镜像**的：HttpServer 用 `disable_vision` 选第一棒，visualserver 用 `enable_audio` 选下一棒，两者合起来才能拼出完整链路。同一段还建了 PULL 接收 socket（绑 `visual_port`）、rpyc 连缓存进程（`cache_port`）、本地 `ShmReqManager`（用来读 Req 元信息）。

**（b）多 DP/TP 模型的异步初始化**。`wait_to_model_ready` 按 `visual_dp × visual_tp` 网格起模型进程：

[lightllm/server/visualserver/manager.py:62-92](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/manager.py#L62-L92) —— 先 `start_model_process()` 把所有 rpyc 子进程拉起，再用 `asyncio.gather` 并行 `init_model`，每个子进程拿到自己的 `device_id`、`tp_rank_id`、`dp_rank_id` 与一条独立的 NCCL 端口（`visual_nccl_ports`）。这与 u2-l4 描述的 ModelBackend「每 GPU 一个 rpyc 服务」模式一致，只是这里跑的是 ViT 而非 LLM。

**（c）查缓存决定要不要算**。`get_need_infer_images` 是「避免重复计算」的核心：

[lightllm/server/visualserver/manager.py:94-122](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/manager.py#L94-L122) —— 三类返回：① `is_aborted` 时返回空（但请求仍要转发，保证共享内存引用计数流程一致）；② `disable_prompt_cache`（测试用）时返回全部图，强制每张都算；③ 正常情况用 `cache_client.root.get_items_embed(img_uuids)` 查每张图的 embed 就绪位，只收集 `ready=False` 的图。注意这里查的是 `embed`，与 HttpServer 查 `data` 不同。

**（d）DP 轮询 + Event 同步**。`handle_images` 把待算图片按 `cur_dp_index % vit_dp` 轮询分配，保证各 DP 组负载均衡：

[lightllm/server/visualserver/manager.py:135-165](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/manager.py#L135-L165) —— 每张图配一个 `threading.Event`；`asyncio.gather` 并发发起各 DP 组的 `infer_images` 后，再用 `await asyncio.to_thread(event.wait)` 阻塞等最后一张图算完，确保转发前所有嵌入已落盘。`self.lock` 保证同一时刻只有一批图在算（避免 batch 间穿插）。

**（e）ViT 推理与嵌入落盘**。真正算嵌入在 model_rpc 子进程里，算完拷回 CPU 缓存并标记就绪：

[lightllm/server/visualserver/model_infer/model_rpc.py:299-310](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/model_infer/model_rpc.py#L299-L310) —— `_store_to_cpu_cache` 里 `tp_rank_id==0` 的 rank 调 `cpu_embed_cache_client.copy_vision_to_cache`，把这张图对应的嵌入段（`all_img_embeds[start:end]`）按 `start_index_in_embed_cache` 写进 CPU 共享张量，然后 record 一个 `cuda_event` 用于异步同步。

[lightllm/server/visualserver/model_infer/model_rpc.py:350-365](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/model_infer/model_rpc.py#L350-L365) —— `_commit_to_cpu_cache` 先 `cuda_event.synchronize()` 等拷贝完成，再 `cache_client.root.set_items_embed(uuids)` 把这些图的 embed 就绪位翻成 True，最后 `image.event.set()` 唤醒 4.2 图(d) 里等待的 `handle_images`。

**（f）音频进程的对称结构**。`AudioManager` 与 `VisualManager` 几乎一一对应，只把「image」换成「audio」、注意力后端换成音频编码器：

[lightllm/server/audioserver/manager.py:80-103](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/audioserver/manager.py#L80-L103) —— `get_need_infer_audios` 同样查 `get_items_embed` 决定要不要算。注意音频目前**不支持 TP**（启动期断言 `audio_tp == 1`，多卡只能走 `audio_dp`，见 api_start.py L250）。

#### 4.2.4 代码实践

**实践目标**：理解 ViT 嵌入是如何从 GPU 搬到 CPU 缓存的，以及为什么用 Triton kernel 而非 `tensor.cpu()`。

**操作步骤**：

1. 阅读 [lightllm/server/embed_cache/copy_to_cache.py:42-70](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/copy_to_cache.py#L42-L70) 的 `offload_embed_tensor_to_cache`，关注它的 grid 是 `(token_num,)`、每个 program 负责一个 token 的全部 layer × hidden 拷贝。
2. 对照 [lightllm/server/embed_cache/embed_cache_client.py:59-69](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/embed_cache_client.py#L59-L69) 的 `copy_vision_to_cache`，确认它就是把 GPU 张量 + `start_index_in_cache` 传给上面那个 kernel。

**需要观察的现象**：kernel 直接从 GPU 显存 `tl.load`、写入 CPU 共享内存张量 `tl.store`（注意 `cache_tensor_ptr` 指向的是 pinned CPU 内存），跳过了 PyTorch `tensor.cpu()` 会引入的临时张量与同步开销。

**预期结果**：你能解释「嵌入张量在 GPU 上算好后，由一个 Triton kernel 直接 DMA 写入 CPU pinned 共享内存的指定偏移，LLM 进程后续按 `start_index_in_embed_cache` 直接读同一块共享内存」这一零拷贝路径。

**待本地验证**：`cpu_embed_cache_tensor` 是否为 pinned 内存取决于 `CpuCacheCreator` 的 `pin` 参数（见 embed_cache_client.py L38-41 的 `pin=pin_shm`），可本地断点确认。

#### 4.2.5 小练习与答案

**练习 1**：`get_need_infer_images` 里为什么 `is_aborted` 的请求要返回空列表，但 `handle_group_indexes` 仍然把它转发给下一棒？

**答案**：因为采用了共享内存映射所有 Req 对象后，引用计数管理很复杂，需要一条**一致的流程**保证不出现异步问题（见源码 L100-104 注释）。若 aborted 请求不转发，下一棒（router）就不会按正常流程释放该 Req 的共享内存槽位，导致内存泄漏。所以「不再算图，但仍走完转发与释放流程」。

**练习 2**：`visual_dp=2` 时，3 张图到达，会被怎样分配？

**答案**：`cur_dp_index` 从 0 开始递增，`select_dp = cur_dp_index % 2`，所以 3 张图依次落到 dp0、dp1、dp0，两组各算一批（dp0 算 2 张、dp1 算 1 张），`asyncio.gather` 并发执行，最后等较慢的那组完成才转发。

---

### 4.3 嵌入缓存与 MD5 去重

#### 4.3.1 概念说明

`embed_cache` 是多模态服务的**地基**，解决两个问题：

1. **去重计算**：同一张图（或同一段音频）在多个请求里反复出现时，ViT 只该算一次。
2. **跨进程共享**：嵌入要在 visualserver 进程（生产）与 LLM 进程（消费）之间共享，且两者地址空间不同。

它用一个**独立的 rpyc 缓存进程** `CacheServer` 做中央账本，用 **MD5** 做去重键，用**引用计数 + LRU** 做生命周期管理，用一块**CPU 共享内存大张量**做实际存储。每个被缓存的条目（`Record`）有两个就绪位：

- `data`：原始图像/音频字节是否已写入共享内存（由 HttpServer 写）。
- `embed`：嵌入是否已算好（由 visualserver/audioserver 写）。

#### 4.3.2 核心流程

**分配（alloc）流程**——HttpServer 对每张图算 MD5 后向缓存进程申请：

```text
对每个 md5sum:
  if md5 已在 _md5_to_record:   → 命中，仅 _add_ref（不分配新槽位）
  else:                          → 新条目，需分配 token_num 连续槽位
        └─ 若容量不足：_free_to_alloc 按 (ref, visittime) LRU 淘汰 ref<=0 的旧条目腾位
        └─ 分配 MemoryBlock [start, start+token_num)
        └─ 分配全局唯一 token_id（从 token_id range 取一段）
返回每个 md5 的 {id(uuid), token_id, start_index_in_embed_cache, token_num, data_ready, embed_ready}
```

**写数据 / 写嵌入**是两个独立的 rpyc 调用：

- HttpServer 拿到 record 后，若 `data_ready=False`，把原图字节写进名为 `"<uuid>-data"` 的共享内存段，再 `set_items_data`。
- visualserver 算完嵌入、拷进 CPU 张量后，调 `set_items_embed`。

**命中判定**：

- HttpServer 查 `data_ready`：命中则跳过「写原图字节」（字节已在 shm 里）。
- visualserver 查 `embed_ready`：命中则跳过「跑 ViT」（嵌入已算好）。
- MD5 相同 ⇒ 同一个 record ⇒ 两个就绪位都已是 True ⇒ 整条多模态链路对该图几乎零开销。

**释放（release）**：请求结束时 HttpServer 调 `release(uuids)` 给每个 record 减一引用；只有 `ref<=0` 的条目才可能被 LRU 淘汰（同一张图正在被多个请求用时绝不会被删）。

#### 4.3.3 源码精读

**（a）MD5 键的构造**。MD5 不仅对原始字节，还拼上 `extra_params` 的哈希，因为同一张图在不同采样参数下可能产出不同 token 数：

[lightllm/server/httpserver/manager.py:198-226](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L198-L226) —— 对每张图先 `init_imageitem_extral_params`（让 tokenizer 按 extra_params 算 token 数），再 `md5sum = hashlib.md5(data).hexdigest() + "_" + str(hash(frozendict(img.extra_params)))`。`frozendict` 是为了可哈希。这就把「图内容 + 处理参数」共同作为缓存键，避免参数变化时拿到错的嵌入。

**（b）alloc 的资源申请与重试**。`_alloc_resource` 用两层重试 + 一把 `_resource_lock` 防止多请求竞争死锁：

[lightllm/server/httpserver/manager.py:144-185](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/httpserver/manager.py#L144-L185) —— 先轻量重试 2000 次（每次睡 5ms），仍失败则进入阻塞式重试（睡 100ms）直到拿到资源；拿到后把 record 的 `id/token_id/token_num/start_index` 回填进 `ImageItem`，并对 `data_ready=False` 的条目 `create_shm("<uuid>-data", data)` 写原图字节。注释里给的死锁例子很关键：cache_capacity=10 而两个请求各 6 张图会互相占位死锁，故需全局串行化申请。

**（c）Record 与三套索引**。`InMemoryCache` 用三个容器维护同一批 record 的不同视角：

[lightllm/server/embed_cache/impl/naive_memory_cache.py:21-51](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L21-L51) —— `_id_to_records`（uuid → record）、`_md5_to_record`（md5 → record，去重用）、`_sorted_records`（按 `(ref, visittime, id)` 排序的 `SortedSet`，淘汰用）。`capacity` 默认 200（`--cache_capacity`），`expired_secs=3600s`。`CpuEmbedCacheClient(create_meta_data=True, init_shm_data=True)` 在缓存进程内创建元数据索引器并初始化那块承载嵌入的 CPU 共享张量。

**（d）alloc 核心逻辑**。`alloc` 是去重与淘汰的交汇点：

[lightllm/server/embed_cache/impl/naive_memory_cache.py:157-223](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L157-L223) —— 关键几步：① `_judge_enough_token_cache` 先看总 token 需求是否超过缓存容量 1/3，超了直接报错（提示调大 `--embed_cache_storage_size`）；② 遍历 md5，已在 `_md5_to_record` 的只 `_add_ref`（命中），其余进 `new_md5_dict`；③ `_free_to_alloc` 按需淘汰；④ 给每个新条目分一段 `token_id_range`（全局唯一，多节点时向 config_server 申请）与一段 `MemoryBlock`；⑤ 最后统一 `_add_ref` 并返回 record 字典。注意第 ② 步先临时加 ref 又在第 ④ 步前 `_del_ref` 解锁，是为了防止分配过程中被并发淘汰。

**（e）LRU 淘汰**。`_try_free_one` 永远从排序集最小的（即 `ref` 最小、其次最久未访问）开始淘汰：

[lightllm/server/embed_cache/impl/naive_memory_cache.py:81-93](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L81-L93) —— 条件是 `len>1` 且队首 `ref<=0`（正在被引用的绝不会删）；删时同时释放 CPU 张量那段索引（`release_indexes`）、`free_shm` 原图字节段、清三个容器。淘汰的度量可写作：被淘汰者 \(r\) 满足

\[
r.\text{ref} \le 0 \;\wedge\; \forall r'.\; (r.\text{ref},\ r.\text{visittime},\ r.\text{id}) \le (r'.\text{ref},\ r'.\text{visittime},\ r'.\text{id})
\]

即引用数优先（先回收无人使用的），相同时按最久未访问（LRU），再相同时按 id（稳定排序去歧义）。

**（f）就绪位读写**。data 与 embed 两套独立的 get/set：

[lightllm/server/embed_cache/impl/naive_memory_cache.py:230-242](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L230-L242) —— `get_items_data` / `get_items_embed` 返回布尔列表，缓存未命中（id 不在表里）时对应位置返回 False，这正是 visualserver `get_need_infer_images` 据以决定要不要算的依据。

**（g）rpyc 服务暴露**。`CacheServer` 把上述能力以 `exposed_*` 方法暴露：

[lightllm/server/embed_cache/manager.py:28-52](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/manager.py#L28-L52) —— 注意每个方法首行 `obtain(...)`，这是因为 rpyc 默认返回的是 netref（远程引用）而非本地拷贝；对列表这种会被反复访问的数据，`obtain` 强制一次性把值取回本地，避免每次下标访问都跨进程往返。`allow_pickle=True` 是为了能 pickle `MultimodalParams` 等自定义对象。

#### 4.3.4 代码实践

**实践目标**：构造一个「同一张图发两次」的场景，验证 embed_cache 的命中行为（命中时第二次不再跑 ViT）。

**操作步骤**：

1. 选一个支持视觉的模型（如 qwen-vl），用两份**完全相同**的 base64 图片构造两次 `/generate` 请求（同一个进程实例内连续发）。
2. 在 [lightllm/server/embed_cache/impl/naive_memory_cache.py:157](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L157) 的 `alloc` 入口与 [lightllm/server/embed_cache/impl/naive_memory_cache.py:241](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/embed_cache/impl/naive_memory_cache.py#L241) 的 `get_items_embed` 各加一行日志，打印 md5 与是否命中。
3. 在 [lightllm/server/visualserver/manager.py:127](https://github.com/ModelTC/lightllm/blob/5d59e4907dc9b5338426b56723bdee42d8af9309/lightllm/server/visualserver/manager.py#L127) 的 `handle_group_indexes` 里打印 `len(images_need_infer)`。

**需要观察的现象**：
- 第一次请求：`alloc` 时该 md5 不在 `_md5_to_record`，走分配分支；`get_items_embed` 返回 `[False]`；`images_need_infer` 长度为 1（要算）。
- 第二次请求（同一张图、同一参数）：`alloc` 时 md5 命中、仅 `_add_ref`；`get_items_embed` 返回 `[True]`；`images_need_infer` 长度为 0（跳过 ViT，直接转发）。

**预期结果**：第二次请求的端到端延迟显著低于第一次（省掉了 ViT 前向），证明缓存命中。

**待本地验证**：上述现象依赖同一进程实例与未触发淘汰；若两次请求间隔过久导致 record 被 LRU 回收（`ref<=0` 且有新图挤占），第二次仍会重算——可临时把 `--cache_capacity` 调大排除干扰。

> 备注：本实践需要修改源码加日志来观察，改完记得还原；若环境无 GPU/模型，可退化为「源码阅读型实践」——仅凭 `alloc` 与 `get_items_embed` 的逻辑推演两次请求的命中差异。

#### 4.3.5 小练习与答案

**练习 1**：为什么 MD5 键要拼上 `hash(frozendict(extra_params))`，而不只用图片字节的 MD5？

**答案**：因为同一张图在不同的 `extra_params`（如不同分辨率/网格设置）下，tokenizer 会展开成不同数量的 image token，对应的嵌入形状与内容都不同。若只用字节 MD5，第二次以不同参数请求时会拿到旧参数下的嵌入，导致结果错误。拼上参数哈希保证「图内容相同且处理参数相同」才算同一缓存项。

**练习 2**：`alloc` 里为什么在遍历 md5 时对命中项先 `_add_ref`，到 `_free_to_alloc` 之前又 `_del_ref`？

**答案**：为了防止「分配新条目触发的淘汰」误删正在被本次 alloc 命中的旧条目。先加 ref 让它的 `ref>0` 从而被 `_try_free_one` 跳过；分配完新条目后再把临时加的 ref 减掉，最后在第 ⑤ 步统一对所有 md5 正式 `_add_ref`。这是一段「临时保护 → 分配 → 正式引用」的精细并发控制。

**练习 3**：`cache_capacity`（默认 200）与 `embed_cache_storage_size`（默认 4G）分别限制什么？

**答案**：`cache_capacity` 限制的是 **record 条目数**（即最多缓存多少个不同的图/音频元数据项，见 naive_memory_cache.py L44 `self.capacity`）；`embed_cache_storage_size` 限制的是承载嵌入的 **CPU 共享张量总字节**（由 `calcu_embed_cache_meta` 折算成 `token_num`，见 embed_cache_client.py L21-22）。前者是「项数」维度，后者是「字节数」维度，两者共同决定缓存能装多少。

## 5. 综合实践

把三个模块串起来，完成一次完整的「图片 + 文本」请求**全链路追踪**任务：

1. **画出进程拓扑**：在一张图上标出 HttpServer、cache_manager、visualserver、router 四个进程，标注它们之间的通信方式（HttpServer↔cache 用 rpyc、HttpServer→visual 用 zmq PUSH、visual→router 用 zmq PUSH、各进程↔共享内存）。
2. **填一张资源生命周期表**：对一张图，按时间顺序列出 `ImageItem` 的 `md5 / uuid / token_id / start_index_in_embed_cache / data_ready / embed_ready` 六个字段分别在哪一步被赋值或翻转（提示：md5 在 HttpServer `_alloc_multimodal_resources`；uuid/token_id/start_index 在 `_alloc_resource` 回填；data_ready 在 `set_items_data`；embed_ready 在 `set_items_embed`）。
3. **回答三个判断题**（自验）：
   - 同一张图第二次请求时，HttpServer 还会 `create_shm` 写字节吗？（不会，因 `data_ready=True`。）
   - 同一张图第二次请求时，visualserver 还会跑 ViT 吗？（不会，因 `embed_ready=True`。）
   - 一张图正被两个请求共用时，会被 LRU 淘汰吗？（不会，因 `ref>=2>0`。）

完成本实践后，你应当能用一句话向别人讲清：**LightLLM 的多模态服务 = 在主链路前插一条 visual/audio 进程链 + 一个 MD5 去重的中央缓存，所有大对象走共享内存，线上只传索引。**

## 6. 本讲小结

- 多模态请求在进入 Router 前，要先经一条**链式进程链**（visual → audio → multi_level_kv_cache → router），HttpServer 用 `transfer_to_next_module` 按优先级挑第一棒，每棒处理完自行 PUSH 给下一棒。
- `visualserver` / `audioserver` 是对称的独立进程，自带 `visual_dp`/`visual_tp`（与 LLM 的 tp/dp 独立），只对 `embed_ready=False` 的图片/音频跑编码器，算完用 Triton kernel 把嵌入直接 DMA 写入 CPU pinned 共享内存。
- 线上 zmq 只传 `GroupReqIndexes`（索引 + `MultimodalParams` 元信息），图片字节、嵌入张量全部走共享内存，这是「对象放共享内存、线上只传索引」原则在多模态场景的延伸。
- `embed_cache` 用独立 rpyc 进程做中央账本，以 `MD5(字节) + hash(extra_params)` 为去重键，命中时仅 `_add_ref` 不重算；用 `(ref, visittime, id)` 排序的 `SortedSet` 做引用计数 + LRU 淘汰。
- 每个 `Record` 有 `data`（原图字节是否落 shm，由 HttpServer 写）与 `embed`（嵌入是否算好，由 visual/audio 写）**两个独立就绪位**，分别被 HttpServer 与 visualserver 查询，决定是否跳过「写字节」与「跑 ViT」。
- 缓存容量有两个维度：`--cache_capacity`（项数，默认 200）与 `--embed_cache_storage_size`（字节数，默认 4G）；alloc 用全局锁 + 重试避免多请求竞争死锁。

## 7. 下一步学习建议

- **u7-l1 PD 分离部署与 KV 迁移**：本讲多次提到「D 节点不重算多模态、由 P 节点处理好」，下一讲正式讲清 P/D 分离时多模态嵌入与 KV 如何跨节点迁移。
- **u7-l3 数据并行与负载均衡**：visualserver 的 `cur_dp_index % vit_dp` 轮询是 DP 负载均衡的雏形，下一讲看 LLM 主模型的 DP 组如何做更精细的负载均衡。
- **继续阅读源码**：若想深入「嵌入如何被 LLM 消费」，可读 `lightllm/common/basemodel/basemodel.py` 中 prefill 阶段对 `start_index_in_embed_cache` 的读取，以及具体视觉模型（如 `lightllm/models/qwen_vl/`）的 `infer_struct.py`，看嵌入如何被拼进输入序列。
