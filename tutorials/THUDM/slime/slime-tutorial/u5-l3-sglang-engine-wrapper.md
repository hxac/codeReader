# SGLang 引擎封装与生命周期

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `SGLangEngine` 这个 Ray actor 是什么、它和「真正的 SGLang 服务进程」是什么关系。
- 复述 `SGLangEngine.init` 的启动链路：拼参数 → 拉起子进程 → 等待健康 → 注册到 router。
- 区分 `update_weights_from_tensor / from_disk / from_distributed` 三条权重同步路径在引擎侧分别对应哪个 HTTP 端点。
- 解释一次权重更新期间「pause → flush → update → continue」这套仪式的调用顺序，并回答「为什么必须先 flush_cache」。
- 说明 `abort_server_until_idle` 如何把还在排队的采样请求排空。

## 2. 前置知识

本讲承接 [u5-l1 权重同步全景](u5-l1-weight-sync-overview.md) 和 [u5-l2 三种权重传输](u5-l2-weight-transport-modes.md)。前面两讲聚焦「Megatron 侧的搬运工」（`UpdateWeightFromTensor` / `UpdateWeightFromDiskDelta` 等类负责把分片权重聚合并送出），本讲视角切换到**接收端**——推理引擎这一侧。

需要先建立三个直觉：

1. **Ray actor 是「遥控器」，不是「引擎本体」。** slime 用 Ray 把每个 SGLang 推理服务包成一个 `SGLangEngine` 远程 actor；但这个 actor 本身只是个轻量的 Python 对象，真正干推理的是它用 `multiprocessing` 拉起的一个**独立 SGLang HTTP 服务子进程**。actor 对子进程的控制，靠的是 HTTP 调用，就像你在终端里 `curl` 这个服务一样。

2. **一次权重更新是一套「仪式（ceremony）」。** 它不是单次函数调用，而是由 Megatron 侧的 `weight_updater` 按固定顺序按下引擎的若干「按钮」：暂停生成 → 清缓存 → 装新权重 → 恢复生成。引擎侧只提供按钮，顺序由调用方编排。

3. **KV cache 会让权重更新变得危险。** 推理引擎为提速会缓存已算过的注意力中间状态（KV cache）。这些缓存是用**旧权重**算出来的；一旦换了权重还继续复用旧缓存，就会出现「旧权重的上下文 + 新权重的后续 token」的混搭，破坏 on-policy 正确性。所以换权重前必须把缓存倒掉。

> 术语提示：
> - **on-policy**：训练数据由当前策略（当前权重）生成，故每轮训练后必须把新权重同步给生成数据的引擎。
> - **KV cache**：Transformer 推理时为避免重复计算而缓存的历史 token 的 Key/Value 张量。
> - **router**：slime 在多个 SGLang 服务前端放的一个轻量 HTTP 路由器，按负载把请求分发到后端 engine。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [slime/backends/sglang_utils/sglang_engine.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py) | 本讲主角。定义 `SGLangEngine` Ray actor，封装 SGLang 服务进程的启动、注册、权重更新、暂停/恢复、缓存清理等全部 HTTP 控制方法。 |
| [slime/backends/sglang_utils/server_control.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py) | 引擎的「排空」工具：`abort_server_until_idle` 向引擎发中断、轮询直到没有在途请求。 |
| [slime/ray/rollout.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py) | `RolloutManager` 在这里创建 `SGLangEngine` actor 并远程触发 `init`。 |
| [slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py) | Megatron 侧搬运工之一，编排 colocate 路径下 pause/flush/update/continue 的顺序。 |

---

## 4. 核心概念与源码讲解

### 4.1 SGLangEngine 的身份与初始化（init）

#### 4.1.1 概念说明

slime 的 rollout 层由若干个 SGLang 推理服务支撑。每张（或每组）GPU 上跑一个 SGLang 服务进程。slime 不直接持有这些进程，而是为每个服务包一个 `SGLangEngine` Ray actor，把它当作「远程遥控器」：

- **actor 自身很轻**：只是个普通 Python 对象（继承自极简的 `RayActor` 基类），不占大量显存、不做推理计算。
- **真正的引擎是子进程**：actor 在初始化时用 `multiprocessing.Process` 拉起一个跑着 `sglang.srt.entrypoints.http_server.launch_server` 的独立进程。
- **控制走 HTTP**：actor 持有子进程的 host:port，后续所有控制（暂停、清缓存、换权重）都是对这个地址发 HTTP 请求。

为什么要这样拆？因为 SGLang 是一个完整的、不断演进的上游项目，slime 选择「SGLang-native」——不 fork、不重写它的推理主循环，而是把它当一个外部服务「拉起来 + 远程操控」，这样上游升级几乎零成本（这一点在 [u1-l1](u1-l1-project-overview.md) 已建立认知）。

#### 4.1.2 核心流程

`SGLangEngine` 的创建分两步，这是一个典型的「构造轻、初始化重」模式：

```
第一步：__init__(args, rank, worker_type, ...)
   └─ 只存配置（rank、worker_type、base_gpu_id、sglang_overrides），什么都不启动
        目的：让 Ray 能尽快把 actor 调度到目标 GPU 所在的节点上

第二步：init(dist_init_addr, port, nccl_port, host=None, router_ip, router_port)
   ├─ 1. 解析 router 地址、本机 host（含 IPv6 方括号格式化）
   ├─ 2. _compute_server_args(...) → 拼出 SGLang ServerArgs 字典
   │     （tp/dp/pp/ep、base_gpu_id、dist_init_addr、各种 enable_* 开关）
   ├─ 3. 分叉：
   │     ├─ rollout_external=True → _init_external：发现外部引擎 + 校验 + 注册
   │     └─ 否则                → _init_normal：拉起子进程 + 等健康 + 注册
   └─ 4. _register_to_router：POST 到 router 的 /workers，把自己登记进去
```

为什么要拆两步？因为 actor 被 Ray 调度到具体节点、拿到正确的 GPU 可见性之后，才能确定 base_gpu_id、host 这些「和物理位置相关」的信息。所以构造时不启动，等 `RolloutManager.start_engines` 在目标机器上调 `engine.init.remote(...)` 时才真正拉服务。这一点在 [rollout.py 的 start_engines](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L252-L259) 里体现：它先创建 actor、分配端口，再统一 fire 一批 `engine.init.remote(...)`，由调用方 `ray.get()` 阻塞等待全部健康。

#### 4.1.3 源码精读

先看构造函数，确认它确实「只存配置」：

[sglang_engine.py:L106-L120](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L106-L120) —— `__init__` 只把参数存到 `self`，不启动任何服务。

真正的初始化在 `init`：它先解析 router 地址、把 host 格式化（IPv6 要加方括号），然后调 `_compute_server_args` 拼出完整的 SGLang 启动参数字典，最后根据是否外部托管分叉：

[sglang_engine.py:L122-L172](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L122-L172) —— `init` 拼参数并在 `_init_external` / `_init_normal` 之间分叉。

`_compute_server_args` 是「把 slime 参数翻译成 SGLang ServerArgs」的中枢，值得留意几个关键映射：

[sglang_engine.py:L544-L571](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L544-L571) —— 这段构建了并行的 tp/dp/pp/ep、`enable_memory_saver=args.offload_rollout`（colocate 时让引擎能释放显存让位给训练）、`skip_server_warmup=True`（防止 warmup 超时）等核心开关。

注意它的参数优先级（高 → 低）：**per-group YAML 覆盖（`sglang_overrides`）> `--sglang-` 前缀透传 > 上述硬编码默认**。这段逻辑在 [sglang_engine.py:L604-L620](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L604-L620)，正是 slime「直接读 SGLang 参数、无损透传」哲学的落地（详见 [u8-l3 参数体系](u8-l3-argument-system.md)）。

`_init_normal` 才是真正「拉服务」的地方：

[sglang_engine.py:L189-L192](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L189-L192) —— 用 `ServerArgs(**server_args_dict)` 构造参数对象，调 `launch_server_process` 拉起子进程，再注册。

`launch_server_process` 做了一件容易被忽略但很重要的事——**删掉 `PYTORCH_CUDA_ALLOC_CONF`**：

[sglang_engine.py:L48-L67](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L48-L67) —— 注释解释了矛盾：expandable segments（可扩展段）能让 colocate 的训练工人容忍反复释放显存，但 SGLang 的内存分配器/sleep 路径不支持它。由于 rollout actor 会继承整个 job 的环境变量，所以每次拉服务前都把这俩变量 pop 掉。

随后它用 `multiprocessing.Process(target=launch_server, ...)` 起子进程，并只在 `node_rank == 0` 时阻塞等待健康：

[sglang_engine.py:L84-L102](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L84-L102) —— `_wait_server_healthy` 每 2 秒轮询 `/health_generate`，返回 200 才认为服务就绪；若子进程已死则直接抛异常，避免静默挂死。

> **node_rank 的含义**：当一个引擎横跨多机（`nnodes > 1`）时，只有 `node_rank == 0` 的那张机器上跑着对外 HTTP 服务，其余机器的进程只是参与张量并行的 worker。因此 `SGLangEngine` 的绝大多数控制方法都用 `if self.node_rank != 0: return` 做早退——只有「主节点」的 actor 真正发 HTTP，其余 actor 的同名 `.remote()` 调用是空操作（但仍是集合通信里必须全员参与的占位）。

最后一步是注册到 router：

[sglang_engine.py:L194-L216](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L194-L216) —— `_register_to_router` 在 `node_rank == 0` 且配置了 router 时，POST `{"url": worker_url, "worker_type": ...}` 到 `router_ip:router_port/workers`。`encoder` 类型直接跳过（不接推理流量）；`prefill` 类型还要额外带上 `bootstrap_port`（PD 分离时 router 靠它路由）。

外部托管模式（`_init_external`）走另一条路：不拉子进程，而是用 `get_server_info` 探测已存在的外部引擎，做参数一致性校验后注册。这对应 [u8-l2 外部推理引擎](u8-l2-pd-disagg-external-engines.md) 的部署形态：

[sglang_engine.py:L174-L187](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L174-L187) —— 探测外部引擎的真实参数，与期望值逐字段比对（跳过位置/随机种子等本地字段），校验通过后才注册。

#### 4.1.4 代码实践

**实践目标**：理解「构造 vs init」的两段式，以及 node_rank 的早退约定。

**操作步骤（源码阅读型实践）**：

1. 打开 [sglang_engine.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py)，用搜索定位 `if self.node_rank != 0`，统计有多少个方法以这行开头（提示：`health_generate`、`flush_cache`、`get_url`、`update_weights_from_tensor` 经由 `_make_request`、`pause_generation`、`continue_generation` 等）。
2. 对照 [rollout.py 的 start_engines](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L182-L230)，确认 `SGLangEngine` 是用 `ray.remote(SGLangEngine).options(...).remote(...)` 创建的普通 Ray actor，创建时只传了 `args/rank/worker_type/base_gpu_id/sglang_overrides/num_gpus_per_engine` 这几个参数。
3. 思考：为什么创建 actor 时（第 2 步）不直接拉服务，而要在 [rollout.py:L252-L259](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L252-L259) 单独 fire 一批 `engine.init.remote(...)`？

**需要观察的现象 / 预期结果**：

- 多个方法共享同一个 `node_rank != 0 → 早退` 约定，说明「多机引擎里只有 node_rank==0 是对外控制面」。
- 创建与 init 分离，是因为 actor 必须先被 Ray 调度到目标节点、拿到 GPU 可见性和端口，才能确定 init 所需的 `host / base_gpu_id / dist_init_addr`。
- 第 3 步的预期结论：**init 返回 ObjectRef 但不阻塞**，`start_engines` 把一批 init 句柄收集起来交给调用方 `ray.get`，实现「并行启动所有引擎、统一等待就绪」。

> 待本地验证：在没有 GPU 的机器上无法真正启动 SGLang，故以上为源码阅读型实践，不要求实跑。

#### 4.1.5 小练习与答案

**练习 1**：`launch_server_process` 为什么要先 `os.environ.pop("PYTORCH_CUDA_ALLOC_CONF")`？不删会怎样？

**参考答案**：slime job 环境里常设置 expandable segments 以便 colocate 的训练工人反复释放显存，但 SGLang 的显存分配器与 sleep/唤醒路径不支持这种分配策略，混用会导致 SGLang 内存管理异常。由于 rollout actor 会继承 job 级环境变量，故在每次拉子进程前主动 pop 掉，保证 SGLang 用默认分配器。

**练习 2**：`_wait_server_healthy` 里有两个退出条件，分别是什么？

**参考答案**：一是轮询 `/health_generate` 返回 HTTP 200（服务就绪，正常退出循环）；二是 `is_process_alive()` 返回 False（子进程意外死亡，抛 `"Server process terminated unexpectedly."`）。后者防止服务启动失败时无限轮询。

---

### 4.2 update_weights_*：三条权重同步路径

#### 4.2.1 概念说明

权重从 Megatron 训练工人搬到推理引擎，在引擎侧落地为三种「装新权重」的方式。它们对应 `SGLangEngine` 上三个方法，本质都是对引擎 HTTP 服务发请求，区别在于「权重数据怎么到达引擎、引擎用什么 load_format 装载」：

| 引擎侧方法 | HTTP 端点 | 数据怎么到引擎 | 典型场景 |
|-----------|-----------|---------------|---------|
| `update_weights_from_tensor` | `/update_weights_from_tensor` | HTTP 只传**元数据（序列化的张量描述）**，真实权重靠 CUDA IPC 或 NCCL 直接拷显存 | colocate（共卡，IPC）/ 远端引擎（NCCL），slime 默认 |
| `update_weights_from_disk` | `/update_weights_from_disk` | 权重先写成磁盘 checkpoint，引擎从路径 reload | 跨机共享文件系统、delta 模式的最终落地 |
| `update_weights_from_distributed` | `/update_weights_from_distributed` | 先建 NCCL 通信组（`init_weights_update_group`），再按张量名 broadcast | 非 colocate 的张量直传 |

> 关键认知：`SGLangEngine` 只提供「按钮」（这三个方法 + pause/flush/continue），**按什么顺序按按钮是 Megatron 侧 `weight_updater` 类编排的**（见 [u5-l2](u5-l2-weight-transport-modes.md)）。本讲关注按钮本身和最常见的那套仪式。

#### 4.2.2 核心流程：一次权重更新的标准仪式

三条路径在 Megatron 侧的编排顺序高度一致，都是这套「四步仪式」：

```
weight_version += 1                          # 版本号自增（用于校验/cache 失效）
① pause_generation()  （node_rank==0）       # 暂停接受新请求
② flush_cache()       （node_rank==0）       # 排空在途请求并清空 KV cache
   （可选）post_process_weights               # 量化权重的 restore/quantize 钩子
   barrier()
③ 发送权重：                                 # 三选一
   ├─ IPC/NCCL → update_weights_from_tensor
   ├─ NCCL 组  → init_weights_update_group + update_weights_from_distributed
   └─ 磁盘     → pull_weights + update_weights_from_disk
   barrier()
④ continue_generation()（node_rank==0）      # 恢复接受新请求
   barrier()
```

注意一个细节：这套顺序在 Megatron 侧的 `update_weights()` 方法里（不是在 `SGLangEngine` 内部），引擎只是被远程调用。以 colocate 的 tensor 路径为例：

[update_weight_from_tensor.py:L277-L331](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L277-L331) —— `update_weights()` 里能清楚看到 `pause_generation`（284 行）→ `flush_cache`（285 行）→ 发送权重（299-313 行）→ `continue_generation`（330 行）的顺序，每步配 `dist.barrier` 同步。

disk delta 路径的 `_reload_engines` 也是同样的四步，只是「发送权重」换成了「`pull_weights`（引擎把 delta 拉到本地）+ `update_weights_from_disk`（从本地 checkpoint reload）」：

[update_weight_from_disk_delta.py:L178-L189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L178-L189) —— `pause_generation → flush_cache → update_weights_from_disk → continue_generation`，与 tensor 路径仪式一致。

#### 4.2.3 源码精读

三个引擎侧方法都很薄，都经 `_make_request` 把请求转成 HTTP POST：

`_make_request` 是统一的 HTTP 出口，注意它的 `node_rank != 0` 早退与错误处理：

[sglang_engine.py:L218-L238](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L218-L238) —— 非 node_rank==0 直接返回；HTTP 错误时用 `e.add_note` 把响应体附到异常上，方便排查。

`update_weights_from_tensor` 只把**序列化的张量描述**通过 HTTP 传过去，真实权重走 IPC/NCCL：

[sglang_engine.py:L262-L285](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L262-L285) —— payload 含 `serialized_named_tensors`、`load_format`、`flush_cache`、可选 `weight_version`。注释点明「HTTP 只传 meta，真实权重直接从 GPU 拷贝」。

`update_weights_from_disk` 最简单——给一个 checkpoint 路径，引擎自己 reload：

[sglang_engine.py:L375-L387](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L375-L387) —— payload 就是 `model_path`，可选 `load_format` 与 `weight_version`。

`update_weights_from_distributed` 是 NCCL 路径，必须先建通信组：

[sglang_engine.py:L389-L438](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L389-L438) —— 先 `init_weights_update_group`（传 master_address/port/world_size/group_name/backend），引擎据此加入 NCCL 组；随后 `update_weights_from_distributed` 传张量名/dtype/shape，引擎按名从组里接收。用完调 `destroy_weights_update_group` 拆组（它对「组还不存在」做了容错 [L402-L412](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L402-L412)）。

pause/continue 则是对引擎两个轻量端点的封装：

[sglang_engine.py:L440-L452](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L440-L452) —— `pause_generation` 与 `continue_generation` 各 POST 一个空 JSON。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：把「一次 update_weights 期间 SGLangEngine 的方法调用顺序」梳理成序列，并解释为何要先 `flush_cache`。

**操作步骤（源码追踪型实践）**：

1. 以 colocate + tensor 路径为主样本，打开 [update_weight_from_tensor.py 的 `update_weights`](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L277-L331)，逐行标注它对 `self.rollout_engines` 里每个 engine 调用了哪些 `.remote()`，顺序是什么。
2. 交叉验证：打开 [update_weight_from_distributed.py:L102-L134](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_distributed.py#L102-L134) 与 [update_weight_from_disk_delta.py:L178-L189](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_disk_delta.py#L178-L189)，确认三条路径的「仪式」骨架一致。
3. 回答下面两个问题（见「预期结果」）。

**需要观察的现象 / 预期结果**：

预期调用顺序（node_rank==0 的引擎）：

```
pause_generation.remote()
flush_cache.remote()
（量化场景）post_process_weights（restore_weights_before_load=True）
   → [实际发送权重：update_weights_from_tensor / _from_distributed / pull_weights+_from_disk]
（量化场景）post_process_weights（post_process_quantization=True）
continue_generation.remote()
```

**为什么必须先 flush_cache？** 两个原因叠加：

1. **正确性**：KV cache 是用旧权重算出的历史中间状态。若不清空，换权重后引擎会复用旧 cache，产生「旧权重上下文 + 新权重续写」的混搭，破坏 on-policy 假设。
2. **可实现性**：SGLang 的 `flush_cache` 在还有在途请求时不会返回 200（见引擎侧 [sglang_engine.py:L287-L306](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L287-L306) 的重试循环与注释「will not return status_code 200 when there are pending requests」）。所以必须先 `pause_generation`（停止接受**新**请求）让在途的跑完，再 `flush_cache` 才能成功排空并清缓存，从而进入一个干净的「无请求、无缓存」稳态，再装新权重。

> 待本地验证：实跑需多卡 GPU 集群，本实践为源码追踪型，不要求实跑。若要半验证，可在单机用两个 Python 进程模拟「先 pause 再 flush」的 HTTP 顺序，观察 flush 在有请求时返回非 200。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `update_weights_from_tensor` 的 HTTP body 里没有真实的权重张量数据，只有 `serialized_named_tensors`？

**参考答案**：因为真实权重通过 CUDA IPC（colocate，传显存句柄）或 NCCL broadcast（远端引擎）直接在 GPU 间拷贝，HTTP 只负责传「张量的名字/形状/dtype/布局」等元数据，让引擎知道该从 IPC handle 或 NCCL 组里接收哪些张量。这样避免了把大张量序列化进 HTTP body 的开销。

**练习 2**：`flush_cache` 的实现里有一个 `for _ in range(60)` 的重试循环，它在等什么？

**参考答案**：等在途请求排空。SGLang 在仍有未完成请求时不会真正 flush（返回非 200），所以引擎侧最多重试 60 次、每次间隔 1 秒；若始终排不空则抛 `TimeoutError("Timeout while flushing cache.")`。这把「pause 让在途跑完」的等待时间上限设为约 60 秒。

---

### 4.3 abort_server_until_idle：中断与排空

#### 4.3.1 概念说明

除了权重更新时的「pause → flush」，引擎还需要另一种排空：**rollout 采样中途的中断**。

回顾 [u3-l2](u3-l2-default-rollout-flow.md)：默认 rollout 用「过采样 + 动态过滤」凑齐目标组数。一旦凑够，多余的、还在途的采样请求必须被叫停——这就是 `abort`。但「叫停」不是粗暴 kill：已发出去的请求可能正卡在某个长尾 prompt 上，需要让引擎把队列里**还没开始算**的请求丢弃、把**已在算的**安全收尾，最终达到「没有在途请求」的空闲态，才能进入下一阶段（权重更新或新一轮采样）。

这个「发中断 + 轮询直到空闲」的逻辑封装在 [server_control.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py)，由 rollout 的 `abort` 调用。

#### 4.3.2 核心流程

```
abort_servers_until_idle(urls)            # 并发对多个引擎
   └─ 对每个 url 调 abort_server_until_idle(url):
        循环:
        ├─ POST {url}/abort_request {"abort_all": True}   # 通知引擎丢弃所有请求
        ├─ GET {url}/v1/loads?include=core                # 查在途请求数
        │   └─ num_requests_from_load 从负载 dict 灵活解析
        ├─ if num_requests <= 0: return                   # 排空成功
        └─ sleep(retry_interval=3s) 后重试                 # 还有在途，等一会再来
```

它的两个设计要点：

- **轮询而非阻塞**：abort 请求是「尽力而为」的信号，引擎不会一次性同步清空。所以发完 abort 后要主动查 `/v1/loads`，靠反复轮询确认真的空闲。
- **解析宽容**：不同版本/配置的 SGLang，`/v1/loads` 返回的字段名略有差异，`num_requests_from_load` 用多种 key 兜底解析。

#### 4.3.3 源码精读

`abort_server_until_idle` 是核心循环：

[server_control.py:L43-L63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L43-L63) —— 死循环里先发一次 `abort_request`，再查在途数；为 0 则返回，否则记日志、睡 `retry_interval`（默认 3 秒）后重试。

发中断与查负载的两个底层函数：

[server_control.py:L32-L40](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L32-L40) —— `_abort_server_once` POST `/abort_request` 且对失败做 warning（不抛错，因为下一次循环会重试）；`_get_server_num_requests` 用 `num_requests_from_load` 解析。

`num_requests_from_load` 的宽容解析值得一看——它体现「对上游 SGLang 多版本字段名的兼容」：

[server_control.py:L12-L29](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L12-L29) —— 先递归处理嵌套 `loads` 列表，再依次尝试 `num_reqs / num_total_reqs / total_reqs`，都没有就把 `running + waiting` 相加。任一字段命中即返回。

并发对多个引擎排空：

[server_control.py:L66-L67](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L66-L67) —— `abort_servers_until_idle` 用 `asyncio.gather` 对所有 engine url 并发排空。

它在 rollout 侧的调用点（凑够样本后中断多余请求）：

[sglang_rollout.py:L337-L347](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L337-L347) —— `abort` 先从 router 拿到所有 worker 的 url，再 `await abort_servers_until_idle(urls)`，随后等所有在途 asyncio 任务收口（partial rollout 时把半成品回收到 buffer）。

> 对比 4.2 的 `flush_cache`：两者都是「排空在途请求」，但触发场景不同。`flush_cache` 服务于**权重更新**（必须连 KV cache 一起清，保证新权重干净），由 Megatron 侧 weight_updater 在 pause 后调用；`abort_server_until_idle` 服务于**采样中途凑够**（只关心请求队列清空，不动 KV cache），由 rollout 侧 abort 调用。

#### 4.3.4 代码实践

**实践目标**：理解 abort 的「发中断 + 轮询」两段式，以及它和 flush_cache 的分工。

**操作步骤（源码追踪型实践）**：

1. 从 [sglang_rollout.py:L337-L347](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L337-L347) 出发，确认 `abort` 是先向 router 查 `/workers` 拿到每个引擎 url，再交给 `abort_servers_until_idle`。
2. 在 [server_control.py:L43-L63](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L43-L63) 里数清楚：一次「attempt」包含哪两个 HTTP 请求？退出循环的条件是什么？
3. 列表对比 `abort_server_until_idle` 与 `flush_cache`（[sglang_engine.py:L287-L306](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L287-L306)）在「目的 / 是否清 KV cache / 触发方 / 调用阶段」上的差异。

**需要观察的现象 / 预期结果**：

- 一次 attempt = 一个 `POST /abort_request` + 一个 `GET /v1/loads`；退出条件是解析出的 `num_requests <= 0`。
- 对比表预期：

| 维度 | `abort_server_until_idle` | `flush_cache` |
|------|--------------------------|---------------|
| 目的 | 排空采样请求队列 | 排空请求 + 清空 KV cache |
| 是否清 KV cache | 否 | 是 |
| 触发方 | rollout 侧 `abort`（凑够样本后） | Megatron 侧 weight_updater（权重更新前） |
| 失败处理 | 查负载失败则 warning 后 return；仍有请求则每 3s 重试 | 60 次内拿不到 200 则 `TimeoutError` |

> 待本地验证：本实践为源码追踪型。

#### 4.3.5 小练习与答案

**练习 1**：`abort_server_until_idle` 里 `_get_server_num_requests` 抛异常时，函数为什么直接 `return` 而不是继续重试？

**参考答案**：见 [server_control.py:L50-L53](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/server_control.py#L50-L53)。查不到负载意味着无法判断是否还有在途请求，继续盲目重试可能无限循环；此时选择 warning 后返回，把「是否真的排空」交给上层（上层随后会用 `asyncio.wait` 等所有在途任务真正结束来兜底，见 [sglang_rollout.py:L350-L359](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L350-L359)）。

**练习 2**：如果一个长尾请求卡住导致某个引擎迟迟排不空，`abort_server_until_idle` 会怎样？

**参考答案**：它会持续每 3 秒发一次 abort 并查负载，循环不退出（没有内置最大次数上限，`attempt` 只是用于日志计数）。这意味着上层需要有自己的超时/熔断机制，否则会一直挂在这个引擎上。这也呼应了 [u7-l4](u7-l4-streaming-async-partial.md) 要讲的 fully_async / partial-rollout 等应对长尾的高级数据流。

---

## 5. 综合实践

把本讲三个模块串起来，画一张「**SGLangEngine 一个完整生命周期**」的时序图，并标注每一步对应的方法与源码位置。

要求在笔记里画出以下三段：

1. **启动段**：`RolloutManager.start_engines` → 创建 actor（[rollout.py:L216-L230](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L216-L230)）→ `engine.init.remote`（[rollout.py:L252-L259](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L252-L259)）→ `init` 内部 `_compute_server_args` → `launch_server_process` → `_wait_server_healthy` → `_register_to_router`。
2. **采样中断段**：rollout 凑够样本 → `abort`（[sglang_rollout.py:L337-L347](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/rollout/sglang_rollout.py#L337-L347)）→ `abort_servers_until_idle`（轮询 `/abort_request` + `/v1/loads`）。
3. **权重更新段**：Megatron 侧 `update_weights` → `pause_generation` → `flush_cache` → 发送权重（tensor/distributed/disk 三选一）→ `continue_generation`（[update_weight_from_tensor.py:L277-L331](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/megatron_utils/update_weight/update_weight_from_tensor.py#L277-L331)）。

并在图旁用一句话回答：**为什么这三段里的「排空」操作（采样中断的 abort、权重更新的 flush）都不可省？**
（参考答案：abort 不可省，否则多余采样请求继续占用引擎、与下一阶段争抢资源；flush 不可省，否则旧权重算出的 KV cache 与新权重混用，破坏 on-policy 正确性。）

最后补一个收尾段：`shutdown`（[sglang_engine.py:L313-L335](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L313-L335)）—— 先从 router `/workers` 注销自己，再 `kill_process_tree` 杀掉子进程。

> 待本地验证：本综合实践为源码阅读 + 画图型，不要求实跑。

## 6. 本讲小结

- `SGLangEngine` 是一个 Ray actor，是 SGLang 服务进程的「远程遥控器」：自身轻量，靠 `multiprocessing` 拉起真正的 SGLang HTTP 子进程，再通过 HTTP 控制它。
- 采用「构造轻、init 重」两段式：`__init__` 只存配置，`init` 才拼 ServerArgs、拉子进程、等健康、注册到 router；多机引擎里只有 `node_rank==0` 是对外控制面，其余 rank 的控制方法早退。
- `update_weights_from_tensor / from_disk / from_distributed` 是引擎侧三条「装新权重」的按钮，分别走 IPC/NCCL 元数据、磁盘 reload、NCCL 通信组；按钮本身很薄，按什么顺序按由 Megatron 侧 weight_updater 编排。
- 一次权重更新是固定的「pause → flush → 发权重 → continue」仪式，三条传输路径骨架一致；先 flush 是因为 KV cache 是旧权重产物，必须清空，且 SGLang 的 flush 在有在途请求时不会成功，需先 pause 让其跑完。
- `abort_server_until_idle` 用「发 abort + 轮询 `/v1/loads`」排空采样请求队列，服务于 rollout 凑够样本后的多余请求中断；它与 `flush_cache` 都排空在途请求，但 abort 不动 KV cache、由 rollout 侧触发，flush 要清 cache、由权重更新触发。
- `launch_server_process` 会主动移除 `PYTORCH_CUDA_ALLOC_CONF`，因为 SGLang 分配器/sleep 路径不支持 expandable segments，而 rollout actor 会继承 job 级环境变量。

## 7. 下一步学习建议

- 下一讲 [u5-l4 Megatron 权重服务端](u5-l4-megatron-weight-server.md) 会从「另一个方向」看同一类问题：slime 如何反向复用 Megatron 暴露 HTTP 端点（`/generate` 提供 logprob 采样），与本讲的「SGLang 作为推理服务被 HTTP 操控」对照阅读，能更完整理解 slime 的双 HTTP 服务架构。
- 想深入 pause/flush/continue 之外的高级显存协作（`release_memory_occupation` / `resume_memory_occupation` 的 tags 机制），可读 [sglang_engine.py:L345-L356](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/backends/sglang_utils/sglang_engine.py#L345-L356) 与 [rollout.py 的 offload/onload](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/ray/rollout.py#L262-L280)，并接续 [u8-l1 sglang-config 拓扑](u8-l1-sglang-config-topology.md)。
- 若对长尾请求导致 abort 排不空的问题感兴趣，可跳读 [u7-l4 流式、全异步与部分回滚](u7-l4-streaming-async-partial.md) 了解 fully_async / partial-rollout 等应对方案。
