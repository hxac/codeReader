# 推理平面与 SGLang 捕获

## 1. 本讲目标

本讲聚焦 SpecForge 在线分离式训练四平面中的「**推理面（inference plane）**」——它在哪儿跑、跑什么、以及它和另外三个平面（控制面 / 数据面 / 训练面）的分界线画在哪里。

学完后你应当能够：

- 说出 `RolloutWorker` 的「**lease → produce_refs / generate_features → commit**」三段式职责，并理解它为何刻意「算法无关」。
- 看懂 `SGLangServerCaptureAdapter` 如何把一次 `/generate` 请求变成若干「**已提交就绪的 `SampleRef`**」，以及为什么这套传输是「**零拷贝**」的。
- 解释 `CaptureConfig` + `verify_capture_specs` 如何把「一次捕获的形状对不对」从「几小时后训练崩」提前到「**捕获边界立刻炸**」。
- 用一句话讲清 SpecForge 运行时最重要的不变量之一：**目标模型的张量永远不会流经 controller 进程或 producer 进程**。

## 2. 前置知识

在进入源码前，先用一句话建立全局直觉。回忆 u7-l1 已经建立的四平面分工：

| 平面 | 职责 | 是否携带张量 |
|---|---|---|
| 控制面（control） | 调度、去重、租约、记账 | 否，只传元数据 |
| 数据面（data） | 搬运张量（`FeatureStore`） | 是，张量的唯一拥有者 |
| **推理面（inference）** | **捕获目标模型特征，提交元数据** | **捕获时算，提交时不带张量** |
| 训练面（training） | 算梯度，对部署形态无感知 | 是，但只在自己的进程里 |

本讲要补的关键认知是「推理面」的精确定义。在**离线**训练里，特征是提前用 `scripts/prepare_hidden_states.py` 算好落盘的（见 u5-l3），训练时根本没有「推理」这一步。**推理面只在「在线 disaggregated」训练里才真正存在**：目标模型不能被 producer / trainer 进程加载（那样就把目标模型和草稿训练耦合死了），它必须作为一个**外部服务**在独立进程甚至独立节点上运行，SpecForge 只能通过网络向它发请求。

于是产生了一个看似简单实则深刻的问题：

> 既然目标模型跑在「另一个进程的服务」里，那它算出来的隐藏状态张量，**怎么搬回 SpecForge 的训练流**？

最朴素的答案是「服务算完 → HTTP 返回张量 → producer 收到 → 再发给 consumer」。SpecForge **明确拒绝了这条路**。理由有三：

1. 隐藏状态张量极大（一个长序列的多层拼接特征动辄上百 MB），HTTP JSON / 二进制往返既慢又吃内存。
2. producer 进程一旦触碰张量，就违反了「控制面只传元数据」的铁律（u5-l4、u7-l2），跨节点协同的轻量性会崩。
3. 目标模型服务通常已经直连高速 RDMA 网络（Mooncake），让张量「**就近落盘**」比绕一圈 producer 更快。

SpecForge 的方案因此是「**零拷贝（zero-copy）服务端写入**」：

- 目标模型服务（一个打过补丁的 SGLang）执行 prefill 后，**自己**把捕获到的张量直接写进 Mooncake 共享存储；
- 它只在 HTTP 响应里返回「**样本 id + 特征的 key / shape / dtype**」这种**纯元数据**；
- producer 进程拿到这些元数据，拼成 `SampleRef`（一个纯指针），交给控制面去调度。

这就是本讲的灵魂句：**模型执行在服务端，张量写入在服务端，元数据返回给 producer**。producer 全程不持有任何目标模型张量。

> 术语速查：`prefill`（对 prompt 做一次前向，得到各层隐藏状态）、`Mooncake`（RDMA 共享内存传输层，u7-l3 的数据面后端之一）、`SampleRef`（指向一个样本全部特征的纯元数据指针，u5-l4）、`quantum`（producer/consumer 窗口握手单位 = dp×batch×accum，u7-l4）。

## 3. 本讲源码地图

本讲涉及的关键文件全部位于 `specforge/inference/`，外加把它们接线的两处装配代码：

| 文件 | 作用 |
|---|---|
| `specforge/inference/DESIGN.md` | 推理面的设计总纲，含 mermaid 流程图与端点表 |
| `specforge/inference/rollout_worker.py` | `RolloutWorker`：算法无关的「租约→特征→提交」编排器 |
| `specforge/inference/adapters/server_capture.py` | `SGLangServerCaptureAdapter`：在线零拷贝捕获的 `RefSource` 实现，以及 `ServerCaptureSchema` |
| `specforge/inference/capture.py` | `CaptureConfig` 类型化契约 + `verify_capture` / `verify_capture_specs` 校验 |
| `specforge/inference/batch_partition.py` | `TargetBatchPartition`：colocated 目标张量并行时的批次分片（在线用不到） |
| `specforge/launch.py`（节选） | `build_rollout_workers`：从算法契约构造 `CaptureConfig` 与 worker |
| `specforge/training/disaggregated.py`（节选） | producer 装配：从 layout 构造 `ServerCaptureSchema` 与 adapter |

> 一个常被混淆的点：`specforge/offline_capture/` 是**另一个**包，只服务 `scripts/prepare_hidden_states.py`（离线预计算），**与在线应用、训练装配互不 import**。本讲讲的 `specforge/inference/` 才是在线推理面。这点在 DESIGN.md 里也明确写了。

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：先讲 `RolloutWorker`（编排器），再讲 `SGLangServerCaptureAdapter`（真正发请求的引擎），最后讲贯穿两者的 `CaptureConfig` 契约与校验。

### 4.1 RolloutWorker：算法无关的编排器

#### 4.1.1 概念说明

`RolloutWorker` 是推理面的「**总调度**」，但它本身极其轻量——文档原话是「**deliberately small and algorithm-agnostic**（刻意小、且与算法无关）」。它解决的问题是：

> 无论用 EAGLE3 还是 DFlash，无论特征是本地算出来还是远端服务算出来，「**把一批 prompt 变成一批已提交的 `SampleRef`**」这件事的**流程骨架**是一样的。

所以 `RolloutWorker` 把**流程**留给自己，把**算法细节**（要捕获哪些张量、目标怎么投影）甩给两样东西：

- `CaptureConfig`：一个类型化的「这次要捕什么」的契约（4.3 节）。
- `feature_source`：一个**可插拔的来源对象**，可能是本地算（`FeatureSource`）也可能是远端服务（`RefSource`）。

它对外呈现的就是一个极简的生命周期：`start()` → 反复 `run_once()` → `drain()` / `stop()`，并向 controller 报告 `health()`。

关键设计点：`RolloutWorker` **从不把张量交给 controller**。它要么自己把特征 `put` 进 `FeatureStore` 后只提交 `SampleRef`，要么（在线路径）连 put 都不做——特征是服务端写好的，它只提交一个指针。

#### 4.1.2 核心流程

`RolloutWorker` 支持两条数据来源路径，由「`feature_source` 是否有 `produce_refs` 方法」在运行时分发：

```
run_once(max_tasks)
  │
  ├─ controller.lease_prompt_tasks(worker_id, max_tasks)   # 1. 租一批 prompt（控制面，纯元数据）
  │     └─ 没拿到 → return []
  │
  ├─【target-TP drop-last】若配置了批次分片且租到的任务数 != max_tasks
  │     └─ complete 掉这批，return []（保证各 rank 批次大小一致）
  │
  ├─ produce_refs = getattr(feature_source, "produce_refs", None)
  │
  ├─ if callable(produce_refs):      # —— 在线路径：服务端已写好张量 ——
  │     └─ _run_ref_source(tasks, produce_refs)
  │           ├─ results = produce_refs(tasks, capture=...)   # 服务端 /generate，返回 SampleRef 或 Failure
  │           ├─ 逐个甄别：是 SampleRef 就收，是 ServerCaptureFailure 就 fail 单条
  │           └─ controller.commit_samples(refs)              # 只提交元数据
  │
  └─ else:                           # —— 本地路径：自己算特征再 put ——
        ├─ feats_list = feature_source.generate_features(tasks, capture=...)
        ├─ 逐 task: verify_capture → feature_store.put → 收集 ref
        └─ controller.commit_samples(refs)
```

两条路径的**共同收尾**都是 `controller.commit_samples(refs)`——注意，提交的 `SampleRef` 是纯元数据指针，**不含张量**。这正是「tensor 不经过 controller」的落地。

两条路径的**根本差异**在于：本地路径（`generate_features`）里 worker 要 `verify_capture` + `feature_store.put`（张量真真切切经过了 worker 进程，但只存在于数据面 `FeatureStore`，从不进控制面）；在线路径（`produce_refs`）里 worker **连张量都不碰**，张量是服务端直接写进 Mooncake 的。

#### 4.1.3 源码精读

先看两个来源协议的定义。`FeatureSource` 与 `RefSource` 是两个 `typing.Protocol`，区分了「我返回特征字典」与「我返回已就绪的 `SampleRef`」：

[specforge/inference/rollout_worker.py:L37-L46](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L37-L46) —— 两个协议：`generate_features` 返回特征字典列表（本地路径），`produce_refs` 返回任意对象列表（在线路径，期望是 `SampleRef`）。

`RolloutWorker.__init__` 把 controller、store、feature_source、capture 这四样东西接好线，并向 controller 注册自己（拿到 `worker_id`）。注意它**不持有任何模型**：

[specforge/inference/rollout_worker.py:L49-L86](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L49-L86) —— 构造里调用 `controller.register_rollout_worker(...)` 注册一个 rollout 角色，并初始化健康状态机 `_state`。

分发点是 `run_once` 中这一段，用鸭子类型 `getattr(..., "produce_refs", None)` 选路：

[specforge/inference/rollout_worker.py:L118-L125](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L118-L125) —— 有 `produce_refs` 就走 `_run_ref_source`；注意 target-TP 批次分片（在线用不到）会在这里被显式拒绝（`NotImplementedError`）。

在线路径 `_run_ref_source` 是本模块的核心。它把 `produce_refs` 的结果分拣：成功的 `SampleRef` 收集起来统一提交，失败对象逐条报告给 controller 但**不抛异常**，保证「一批里某个 prompt 捕获失败，不会拖死同批其它成功的 prompt」：

[specforge/inference/rollout_worker.py:L227-L289](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L227-L289) —— `_run_ref_source`：`isinstance(result, SampleRef)` 收下，否则当失败对象处理，最后 `controller.commit_samples(refs)`。

最后看本地路径里「张量进 store 但元数据进 controller」的分界点：

[specforge/inference/rollout_worker.py:L194-L216](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L194-L216) —— `feature_store.put(feats, ...)` 返回一个 `ref`（张量进了数据面 store），随后 `controller.commit_samples(self.worker_id, refs)`（只把 ref 元数据提交给控制面）。两步之间，张量从未进入 controller。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「`RolloutWorker` 把流程留给自己、把算法细节甩给 `feature_source` / `capture`」这一断言。

**操作步骤**：

1. 打开 `specforge/inference/rollout_worker.py`，全文搜索是否出现任何算法名字符串（如 `eagle3`、`dflash`）或对草稿模型类的引用。
2. 在 `run_once` 与 `_run_ref_source` 中标出三处与 controller 交互的调用：`lease_prompt_tasks`、`commit_samples`、`fail_prompt_tasks`。
3. 确认 `_run_ref_source` 全程没有出现 `feature_store.put`。

**需要观察的现象**：

- `RolloutWorker` 类体里找不到任何具体算法名或模型类，唯一的「算法痕迹」是构造参数里的 `strategy: str = "eagle3"`（仅作为 provenance 写进 metadata，见 `_put_metadata`）。
- 在线路径 `_run_ref_source` 确实没有任何 `put`/`get` 张量的调用——它只是收 `SampleRef` 再提交。

**预期结果**：你会确信 `RolloutWorker` 是一个纯粹的「**流程编排器**」，算法差异全部被隔离在 `feature_source` 与 `capture` 两个注入对象里。这正是它能同时服务本地 colocated 与在线 disaggregated 两种部署的关键。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_run_ref_source` 在遇到一个非 `SampleRef` 的失败结果时，选择 `fail_prompt_tasks` 单条报告并 `continue`，而不是直接 `raise`？

**参考答案**：因为一次 `/generate` 是**批量**请求，服务端可能对其中部分 prompt 捕获成功、部分失败。如果一条失败就整体 `raise`，会让 controller 把「同批已成功的 prompt 租约」也一并标失败或滞留，造成租约泄漏（rollout_worker.py 文档原话是「keep this batch's other prompt leases moving so no lease is stranded」）。逐条失败报告让成功的样本照常提交，失败的可由 controller 决定是否重试。

**练习 2**：`HEALTH_STATES` 里定义了哪几种健康状态？谁负责「报告」，谁负责「决策调度」？

**参考答案**：`starting / ready / paused / draining / unhealthy / stopped`。文件头注释明确：worker 只负责**报告**健康（`health()` 方法），**调度决策**由 controller 做。这是一种关注点分离——worker 不自作主张退出，只把状态如实上报。

---

### 4.2 SGLangServerCaptureAdapter：零拷贝服务端捕获

#### 4.2.1 概念说明

如果 `RolloutWorker` 是「**通用骨架**」，那么 `SGLangServerCaptureAdapter` 就是骨架在在线模式下插上的「**那块具体肌肉**」。它实现了 `RefSource` 协议（即提供 `produce_refs`），是 `RolloutWorker` 在在线 disaggregated 训练里默认的 `feature_source`。

它解决的核心问题是「**如何让目标模型服务把特征张量直接写进 Mooncake，而 SpecForge 只拿回元数据**」。这依赖一个外部约定：目标模型必须是一个**打过补丁的 SGLang 服务**（补丁文件 `patches/sglang/v0.5.14/spec-capture.patch`，见 server_capture.py 文件头注释）。这个 patched SGLang 在收到带 `spec_capture` 字段的 `/generate` 请求时，会：

1. 对输入做一次完整 prefill；
2. 把请求的 `aux`（捕获层拼接）、`last_hidden`（最终隐藏状态）以及透传张量，按 `mooncake://{store_id}/{sample_id}` 的 key 布局**直接写进 Mooncake**；
3. 在响应的 `meta_info["spec_capture"]` 里**只返回** `{sample_id, store_id, gen, features:{name:{shape,dtype}}}` 这种纯元数据。

由此引出本模块最重要的认知——**传输边界**：

- **模型执行**：发生在 patched SGLang 服务端；
- **张量写入**：发生在 patched SGLang 服务端（直写 Mooncake）；
- **元数据返回**：HTTP 响应只带 key/shape/dtype，回到 producer 进程；
- **producer 进程**：只依据元数据拼 `SampleRef`，**全程零张量**。

还有一个被刻意强调的解耦点：**adapter 自己不解析算法名**。文件头注释原话是「`this transport never resolves an algorithm name`」。adapter 只认识两类**通用产物**——`aux`（捕获层拼接）和 `last_hidden`（post-norm 最终隐藏态），外加若干透传张量。算法该叫什么、这些张量在训练侧对应哪个特征名，是由组合根在装配时通过一个 `ServerCaptureSchema` **注入**进来的。这样「目标执行」「算法策略」「部署接线」三件事就被彻底分开（DESIGN.md 的 Responsibility 段原话）。

#### 4.2.2 核心流程

`produce_refs` 把「一批 prompt」变成「一批已就绪 `SampleRef`」的全过程：

```
produce_refs(tasks, capture)
  │
  ├─ body = _request_inputs(tasks)            # 只组装模型输入字段（input_ids 等）
  ├─ body["extra_key"] = 每任务一个 uuid       # 强制每次都全量 prefill（新鲜缓存命名空间）
  ├─ body["sampling_params"] = {temp:0, max_new_tokens:1}  # 不真生成，只 prefill
  ├─ capture_payloads = [_spec_capture_payload(t) for t in tasks]  # 每任务的 spec_capture 描述
  │     └─ 告诉服务端：写到哪个 store_id / sample_id / 捕哪些 feature / 透传哪些张量
  ├─ 对每个 payload：store.track_external_attempt(...)   # 在本地 store 预登记这次外部写
  │
  ├─ rows = post_fn(base_url + "/generate", body)        # 一次批量 HTTP 请求
  ├─ rows = _flatten_list_wrappers(rows)                  # 拍平响应包装
  ├─ assert len(rows) == len(tasks)                       # 行数必须对齐
  │
  └─ 逐 (task, row)：
        ├─ 取 meta_info["spec_capture"]，按 sample_id 选中本任务结果
        ├─ 若无结果 / 有 error → 记 ServerCaptureFailure（单条失败，可重试/不可重试）
        ├─ 校验身份 {sample_id, store_id, gen} == 期望值   # 写错对象了直接 RuntimeError
        ├─ ref = _ref_from_result(task, result, capture)  # 纯元数据拼 SampleRef
        ├─ 校验序列长度：shape[1] == prompt 长度           # 捕短了 = prefill 不完整 → 不可重试失败
        ├─ verify_capture_specs(ref.feature_specs, capture, ...)  # 形状契约校验（见 4.3）
        │     └─ 失败：adopt + abort 释放服务端写的 key，记不可重试失败
        └─ 成功：先攒进 successful_refs（暂不 adopt）
  最后：所有行都通过结构校验后，统一 store.adopt(ref) 把服务端写的对象纳入 producer 所有权
```

这里有几个极其精妙的设计，逐一解释：

- **`extra_key` 用 uuid**：每个请求一个全新缓存命名空间，**强制服务端做完整 prefill**。在线捕获要求每个样本的特征必须来自一次完整的前向，不能命中旧缓存。重试时也用新 key，避免「上次请求在响应丢失前已经写过了，这次又写一份」造成混乱。
- **`gen: 1` + `replace`**：`gen` 是 Mooncake 的代数（generation），用于区分同一 `sample_id` 的多次写入。这里刻意固定为 1，并配合 `replace = (attempt > 0)`，让重试时服务端**原地替换**而非新增代数——文档原话是「a response lost after the server write cannot strand gN when the retry writes gN+1」（响应丢了也不能让重试写成 gN+1 把 gN 孤立掉）。
- **延迟 adopt**：成功的 ref 先攒进 `successful_refs`，**等整批所有行都通过校验后才统一 `adopt`**。注释解释得很清楚：如果先 adopt 了前几行，后面某行结构错会让整个 lease batch 抛异常，那这几行就变成了「已被 adopt 但不会返回」的孤儿，后续清理看不见它们。
- **身份校验与长度校验是硬错误**：服务端返回了错误的 `sample_id/store_id/gen`（`RuntimeError`，整批失败），或捕出来的序列长度 != prompt 长度（说明 prefill 没跑完整，`retryable=False`），都不是「这个样本坏了」，而是「这次请求本身有问题」。

#### 4.2.3 源码精读

先看 `ServerCaptureSchema`——它是「算法语义」与「服务端通用产物」之间的翻译表，由组合根注入，adapter 本身不解析算法名：

[specforge/inference/adapters/server_capture.py:L42-L56](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L42-L56) —— `aux_feature`/`last_hidden_feature` 命名两类引擎产物（`None` 表示不要），`passthrough` 描述透传张量，`attention_mask_feature` 合成全 1 mask。

`__init__` 对 `store` 做强校验：必须长得像 `MooncakeFeatureStore`（暴露 `adopt`/`discard_external_attempts`/`store_id`/`track_external_attempt` 这套外部写入 API），否则直接 `TypeError`：

[specforge/inference/adapters/server_capture.py:L141-L162](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L141-L162) —— 校验 store API 与 schema 类型，确保 adapter 绑定的是一个能「领养外部写入」的 Mooncake 存储。

`_spec_capture_payload` 负责把每个任务翻译成服务端能理解的「写到哪里、捕什么、透传什么」描述。注意 `gen: 1` 与 `replace` 的取值：

[specforge/inference/adapters/server_capture.py:L203-L256](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L203-L256) —— 构造 `spec_capture` 载荷：`store_id`/`sample_id` 定位、`features` 声明要 aux/last_hidden、`passthrough` 携带 input_ids 等透传张量并校验长度、`gen:1` + `replace` 保证重试时原地替换。

`_ref_from_result` 是「元数据 → `SampleRef`」的纯函数。它从响应里读 `shape`/`dtype`，为每个特征构造一个 `FeatureSpec`（注意：**没有任何张量被读取**，全是元数据），并算出一个 `estimated_bytes`：

[specforge/inference/adapters/server_capture.py:L259-L307](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L259-L307) —— `_ref_from_result`：`feature_store_uri = mooncake://{store_id}/{sample_id}`，`feature_keys = {name: sample_id/name}`，全部是定位指针，不含张量本体。

`produce_refs` 的请求构造与响应处理是本模块重头戏。先看请求侧（强制全量 prefill + 预登记外部写）：

[specforge/inference/adapters/server_capture.py:L320-L341](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L320-L341) —— `extra_key` 用 uuid 强制新鲜命名空间、`max_new_tokens:1` 不真生成、`spec_capture` 载荷挂进 body、发请求前 `track_external_attempt` 在本地 store 预登记。

再看响应侧的「身份校验 + 长度校验 + 契约校验」三道关卡：

[specforge/inference/adapters/server_capture.py:L386-L426](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L386-L426) —— 身份三元组校验（写错对象整批炸）、序列长度校验（捕短了说明 prefill 不完整、`retryable=False` 并 `adopt`+`abort` 释放服务端 key）。

[specforge/inference/adapters/server_capture.py:L427-L469](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L427-L469) —— `verify_capture_specs` 契约校验；失败则 `adopt`+`abort` 释放并记不可重试失败；成功先攒进 `successful_refs`，等整批通过后统一 `store.adopt(ref)`。

最后看这些 adapter 是怎么在 producer 装配时被构造出来的——`ServerCaptureSchema` 的字段直接来自算法 provider 提供的 `ServerCaptureLayout`（u4-l3 提到的 layout）：

[specforge/training/disaggregated.py:L574-L591](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L574-L591) —— producer 装配：从 `streaming.layout` 取 `aux_feature`/`last_hidden_feature`/`passthrough`/`attention_mask_feature` 构造 `ServerCaptureSchema`，再为每个服务 URL 构造一个 `SGLangServerCaptureAdapter`。

#### 4.2.4 代码实践

**实践目标**：印证「一次 capture 请求中，模型执行、张量写入、元数据返回分别发生在哪」，并理解 `verify_capture_specs` 的作用。

**操作步骤**：

1. 打开 [specforge/inference/DESIGN.md](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/DESIGN.md)，找到其中的 mermaid 流程图（`flowchart LR`）与 Endpoints 表。
2. 对照 `server_capture.py` 的 `produce_refs`，把流程图的每个节点映射到代码行：
   - `P[PromptTask lease]` → `produce_refs` 的调用方 `RolloutWorker._run_ref_source`（先由 `lease_prompt_tasks` 拿到 tasks）。
   - `A[SGLangServerCaptureAdapter]` → `produce_refs` 本身。
   - `S[patched SGLang /generate]` → `post_fn(f"{base_url}/generate", ...)`。
   - `M[Mooncake tensor writes]` → **服务端行为**（代码里看不到，由 patch 实现，但请求侧用 `_spec_capture_payload` 携带了 `store_id`/`sample_id`/`features` 指示它）。
   - `V[feature metadata response]` → `meta_info["spec_capture"]`。
   - `C[verify_capture_specs]` → `verify_capture_specs(ref.feature_specs, capture, ...)`。
   - `R[adopt SampleRef]` → `store.adopt(ref)`。
   - `W[RolloutWorker commit]` → 回到 `controller.commit_samples`。
3. 在 `produce_refs` 里数一数：有没有任何一处真正 `.get()` 或反序列化了一个张量？

**需要观察的现象**：

- 「模型执行」与「张量写入」**在 SpecForge 的源码里是看不到的**——它们发生在 patched SGLang 进程内。SpecForge 这边能看到的唯一痕迹是「请求里带了 `store_id`/`sample_id`/`features`/`passthrough` 这套指示」（`_spec_capture_payload`）。
- 「元数据返回」体现在 `result["features"][name]["shape" / "dtype"]`，`_ref_from_result` 只读这些字段。
- 整个 `produce_refs` 里**没有任何一处把张量搬进 producer 进程**。

**预期结果**：你会在流程图与代码之间建立起精确的对应，并亲手确认「tensor 不经过 producer 进程」不是一句口号，而是代码结构上成立的——producer 全程只在搬运 `shape`/`dtype` 字符串与整数。

> 关于 `verify_capture_specs` 的作用（实践任务的第二问）：它是「**捕获边界的守门员**」。服务端只产通用产物（aux / last_hidden），它不知道某个算法要求 aux 宽度必须等于 `len(aux_layer_ids) × target_hidden_size`，也不知道 `pruned_logits` 目标的最后一维必须等于 `draft_vocab_size`、且必须带 `vocab_map_version`。这些算法语义由 `CaptureConfig` 携带（4.3 节），`verify_capture_specs` 拿着这份契约去比对服务端返回的 `FeatureSpec`，**一旦不符就当场 `adopt`+`abort` 释放服务端已写的 key，并把这条记为不可重试失败**。它把「形状/语义不对」从「trainer 训到一半崩」提前到「捕获当场炸」，且绝不留下一个「坏样本」让下游消费。

#### 4.2.5 小练习与答案

**练习 1**：`produce_refs` 里 `_request_inputs` 为什么要禁止 `request_input_adapter` 设置 `extra_key`/`sampling_params`/`spec_capture` 这三个字段（见 `_request_inputs` 里的 `reserved` 检查）？

**参考答案**：因为这三个字段是「**传输拥有的运行时键**」（transport-owned keys）。`extra_key` 由 adapter 用 uuid 生成以强制全量 prefill；`sampling_params` 由 adapter 固定为 `{temperature:0, max_new_tokens:1}`（只 prefill 不真生成）；`spec_capture` 由 adapter 用 `_spec_capture_payload` 构造。如果允许算法侧的 `ServerInputAdapter` 也写这些字段，就会和 adapter 的传输逻辑打架（比如算法侧不小心设了 `max_new_tokens:100`，那服务端就真去生成 100 个 token 了）。所以 `reserved` 集合把这三个字段划为「传输专属」，算法侧只能提供模型输入（input_ids 等），职责边界用校验焊死。

**练习 2**：为什么成功的 ref 要先攒进 `successful_refs`，等整批校验通过后才统一 `adopt`，而不是每个一成功就立刻 `adopt`？

**参考答案**：因为「**adopt 之后、返回之前**」存在一个窗口。如果第 1 个 ref adopt 了，第 3 个 ref 的身份校验却失败、整个 `produce_refs` 抛 `RuntimeError`，那么第 1 个 ref 已经被 producer 接管所有权，但 `produce_refs` 没有返回它——它对后续的终止清理（terminal provisional cleanup）就变成了不可见的孤儿，服务端写的张量永远不会被释放。延迟到「整批所有行都通过结构校验」再统一 adopt，保证了「要么全部纳入并返回、要么一个都不纳入」的原子语义（server_capture.py L461-L469 的注释详细解释了这一点）。

---

### 4.3 capture contract：CaptureConfig 与 verify_capture_specs

#### 4.3.1 概念说明

第三个最小模块是贯穿前两者的「**契约与校验**」。它的核心问题是：

> 在线捕获的特征不是 SpecForge 自己算的，而是外部服务写的。怎么保证服务写回来的东西，正是某个算法训练所需的？

答案分两层：

1. **契约层 `CaptureConfig`**：一个 `frozen=True` 的 dataclass，把「这次捕获应当满足什么」用类型化字段固定下来——要哪些特征名、aux 用了哪些层、目标表示是哪种、宽度该多少。它由算法的流式特征契约（`FeatureContract(STREAMING, modality)`，见 u4-l1）+ 草稿捕获层在装配期推导出来，**只依赖标准库、可单测、无需 GPU**。
2. **校验层 `verify_capture` / `verify_capture_specs`**：在「特征写入特征库之前」（本地路径）或「`SampleRef` 被提交之前」（在线路径）做四道形状检查，任一不符就抛 `CaptureMismatchError`。

这里有一个非常关键的区分，初学者容易混：

- `verify_capture(tensors, ...)`：入参是**真的张量**，从 `tensor.shape` 取形状。用于本地路径 `RolloutWorker.run_once` 里、`feature_store.put` **之前**。
- `verify_capture_specs(specs, ...)`：入参是 `FeatureSpec`（纯元数据），从 `spec.shape` 取形状。用于在线路径 `SGLangServerCaptureAdapter.produce_refs` 里、`store.adopt` **之前**。

两者复用同一份 `_verify_capture_shapes` 逻辑，只是形状来源不同。后者尤其重要——它让在线路径**在不读取任何张量的前提下**就能完成契约校验，完美贴合「零拷贝」理念。

`CaptureConfig` 还定义了一个对训练极重要的概念 `target_repr`（`TargetRepr`），它描述「目标侧给草稿的监督信号是什么形态」：

| `target_repr` | 期望的目标最后一维 | 含义 |
|---|---|---|
| `"hidden_state"` | `target_hidden_size` | 目标给的是隐藏态，草稿自己算 logits |
| `"logits"` | `target_vocab_size` | 目标给全词表 logits |
| `"pruned_logits"` | `draft_vocab_size` | 目标给裁剪到草稿词表的 logits（需 vocab mapping） |

注意 `pruned_logits` 是个特例——它**强制要求** `vocab_map_version` 非空，否则校验直接失败（capture.py L136-L140）。因为裁剪 logits 必须和一个确定的词表映射绑定，否则 trainer 侧的映射无法对齐。

#### 4.3.2 核心流程

`_verify_capture_shapes` 做的四道检查（按代码顺序）：

```
_verify_capture_shapes(shapes, capture, sample_id, recorded_aux_layer_ids)
  │
  ├─ (1) 缺特征检查
  │     missing = capture.feature_names 中不在 shapes 里的
  │     └─ 非空 → CaptureMismatchError（列出 requested vs got）
  │
  ├─ (2) aux 层 id 一致性检查
  │     若服务端记录了 recorded_aux_layer_ids：
  │       recorded != capture.aux_hidden_state_layer_ids → 报错
  │     （即「我让你捕第 [1,3,12] 层，你捕的不是这三层」）
  │
  ├─ (3) aux 宽度检查
  │     width(shape[-1]) == len(aux_layer_ids) * target_hidden_size ？
  │     └─ 不等 → 报错（拼接层数 × 每层宽度）
  │
  └─ (4) 目标最后一维检查
        expected = expected_target_dim()   # 依 target_repr 取 draft_vocab/target_vocab/hidden_size
        shape[-1] == expected ？
        └─ 不等 → 报错
        且若 target_repr == "pruned_logits"：vocab_map_version 必须非空，否则报错
```

其中两个派生量值得记住（它们是检查 (3)(4) 的算子）：

- aux 宽度：`expected_aux_width = len(aux_hidden_state_layer_ids) × target_hidden_size`（capture.py L66-L68）。EAGLE3 默认捕 3 层（u1-l4），所以 aux 宽度 = `3 × hidden_size`。
- 目标维度：`expected_target_dim` 按 `target_repr` 三分支返回 `draft_vocab_size / target_vocab_size / target_hidden_size`（capture.py L70-L77）。

这两条公式背后的物理含义：EAGLE3 把多层隐藏态**横向拼接**成一个宽向量（capture.py 用 `aux` = 捕获层拼接），所以宽度必须是「层数 × 单层宽度」；而目标侧的最后一维必须严格匹配它承诺的表示形态。

#### 4.3.3 源码精读

先看 `CaptureConfig` 的定义与三个派生方法。注意它是 `frozen=True` 且文件头强调「stdlib only」：

[specforge/inference/capture.py:L33-L77](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/capture.py#L33-L77) —— `CaptureConfig` 数据类；`from_strategy` 工厂把算法的 `required_features` 与捕获层 id 装进来；`expected_aux_width` 与 `expected_target_dim` 是校验算子。

四道检查的实现，是整个契约的「裁判」：

[specforge/inference/capture.py:L80-L140](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/capture.py#L80-L140) —— `_verify_capture_shapes`：(1) 缺特征 (2) aux 层 id 一致 (3) aux 宽度 (4) 目标最后一维 + `pruned_logits` 必须带 vocab_map_version。

两个对外入口：`verify_capture` 吃真张量、`verify_capture_specs` 吃 `FeatureSpec`，殊途同归地复用 `_verify_capture_shapes`：

[specforge/inference/capture.py:L143-L184](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/capture.py#L143-L184) —— 两个校验入口，唯一差异是形状来源（`tensor.shape` vs `spec.shape`）。

最后看 `CaptureConfig` 是怎么被构造出来的——在 `launch.py` 的 `build_rollout_workers` 里，从算法的流式特征契约直接推导：

[specforge/launch.py:L470-L494](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L470-L494) —— `CaptureConfig.from_strategy`：`required_features` 取自 `algorithm.spec.feature_contract("streaming", modality).required_tensors`，捕获层 id、目标表示、词表大小等作为参数注入，最后为每个 `feature_source` 构造一个 `RolloutWorker`。

这条链很重要，因为它把 u4-l1 的算法契约和本讲的推理面接起来了：**算法在契约里声明「我流式训练需要哪些张量」（`FeatureContract(STREAMING, text)` 的 `required_tensors`），这个声明经 `CaptureConfig` 一路传到 `verify_capture_specs`，成为校验在线捕获产物的裁判标准。**

#### 4.3.4 代码实践

**实践目标**：亲手构造一个 `CaptureConfig` 并用 `verify_capture_specs` 校验，直观感受「契约校验如何把坏样本挡在门外」。

**操作步骤**（这是一个源码阅读 + 心智实验型实践，无需 GPU）：

1. 阅读 [specforge/inference/capture.py:L80-L140](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/capture.py#L80-L140) 的四道检查。
2. 在脑中（或在一份「示例代码」里，不要写进仓库）构造一个 EAGLE3 风格的 `CaptureConfig`：
   ```python
   # 示例代码（非项目原有，仅供理解，勿写入仓库）
   from specforge.inference.capture import CaptureConfig, verify_capture_specs
   from specforge.runtime.contracts import FeatureSpec

   cfg = CaptureConfig.from_strategy(
       required_features={"hidden_state", "target"},
       aux_hidden_state_layer_ids=(1, 15, 28),   # 3 层
       target_repr="hidden_state",
       target_hidden_size=4096,
   )
   # 预期 aux 宽度 = 3 * 4096 = 12288；target 最后一维 = 4096
   ```
3. 构造两组 `FeatureSpec` 做对比：
   - **正确组**：`hidden_state` 形状 `(1, L, 12288)`，`target` 形状 `(1, L, 4096)`。
   - **错误组**：`hidden_state` 形状 `(1, L, 8192)`（少捕了一层）。
4. 对两组分别调用 `verify_capture_specs(specs, cfg, sample_id="run:s1")`。

**需要观察的现象**：

- 正确组不抛异常，静默通过。
- 错误组抛 `CaptureMismatchError`，且错误消息精确指出「aux width 8192 != len(aux_layer_ids)*target_hidden_size=3*4096=12288」，并带上 `[run:s1]` 的 sample_id 面包屑。

**预期结果**：你会看到 `verify_capture_specs` 是如何用一个「纯元数据」的 `FeatureSpec`，在不读取任何张量的前提下，精确地拦下一个「少捕了一层」的坏样本。这正是它在 `SGLangServerCaptureAdapter.produce_refs` 里被调用的意义——把坏样本挡在 `adopt` 之前。

> 若你想真实运行，可在装好 SpecForge 的 venv 里 `python -c` 执行上述示例代码。capture.py 文件头明确它是「stdlib only」，所以这条命令**不需要 GPU**。若不确定环境，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`verify_capture` 和 `verify_capture_specs` 为什么是两个函数而不是一个？它们的调用时机有何不同？

**参考答案**：因为形状来源不同。`verify_capture` 入参是真张量（`tensor.shape`），用于本地路径——特征是 worker 自己 `generate_features` 算出来的，手上有张量，在 `feature_store.put` 之前校验。`verify_capture_specs` 入参是 `FeatureSpec`（`spec.shape`），用于在线路径——特征是服务端写的、worker 手上**只有元数据没有张量**，在 `store.adopt` 之前校验。两者复用同一份 `_verify_capture_shapes`，既保证了「同样的四道检查」，又让在线路径维持零拷贝。

**练习 2**：当 `target_repr="pruned_logits"` 但 `vocab_map_version=None` 时，`verify_capture_specs` 会怎么做？为什么必须这样？

**参考答案**：它会抛 `CaptureMismatchError`，提示「`pruned_logits` requires a vocab_map_version so the trainer-side mapping is gated」（capture.py L136-L140）。因为裁剪 logits 是按草稿词表索引的，必须和一个确定的词表映射绑定；trainer 侧要用这个 `vocab_map_version` 来 gate（门控）映射的正确性。没有它，trainer 可能拿一个错位的词表映射去解读这批裁剪 logits，训练目标就错了。所以契约层在捕获边界就把它拦下。

---

## 5. 综合实践

把本讲三个模块串起来，做一次「**端到端追踪一次在线捕获**」的源码阅读任务。

**任务**：假设你正在调试一个在线 disaggregated EAGLE3 训练，发现 consumer 侧收到的 `SampleRef` 数量比 producer 发出的 prompt 少。请按下面的顺序，沿着推理面的真实调用链，定位「特征从哪来、张量在哪写、元数据怎么回、谁在把关」。

**步骤**：

1. **入口**：从 [specforge/training/disaggregated.py:L574-L591](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/disaggregated.py#L574-L591) 出发，确认 producer 为每个服务 URL 构造了一个 `SGLangServerCaptureAdapter`，并把它们作为 `feature_source` 传给了 `RolloutWorker`。
2. **驱动**：阅读 `launch.py` 的 `drive_producer`（[specforge/launch.py:L935-L998](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L935-L998)），确认它先做 quantum 握手（等 consumer 发布 `consumer_quantum`、校验高低水位 ≥ quantum），再 `w.start()` 启动每个 worker、循环驱动 `run_once`。
3. **租约**：进入 [specforge/inference/rollout_worker.py:L97-L125](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L97-L125)，确认 worker 先 `lease_prompt_tasks`（控制面元数据），再因 `feature_source` 有 `produce_refs` 而走 `_run_ref_source`。
4. **捕获**：进入 [specforge/inference/adapters/server_capture.py:L310-L341](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L310-L341)，确认一次批量 `/generate` 请求被发出，`spec_capture` 载荷告诉服务端「写到哪个 store/sample_id、捕 aux+last_hidden、透传 input_ids」。
5. **回填与把关**：进入 [specforge/inference/adapters/server_capture.py:L386-L469](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/adapters/server_capture.py#L386-L469)，确认身份校验、长度校验、`verify_capture_specs` 契约校验三道关卡，以及「失败则 adopt+abort 释放、成功则攒到 successful_refs 统一 adopt」的策略。
6. **提交**：回到 [specforge/inference/rollout_worker.py:L256-L289](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/inference/rollout_worker.py#L256-L289)，确认只有通过校验的 `SampleRef` 会被 `controller.commit_samples` 提交（纯元数据），失败对象被逐条 `fail_prompt_tasks`。

**需要观察的现象 / 排查方向**：

- 如果少的那些样本在 producer 日志里能看到 `recent_failures`（见 `RolloutWorker.health`），大概率是某道校验把它们挡下了——去看是「序列长度不符」（prefill 不完整）还是「契约不符」（形状/层 id/vocab_map）。
- 如果 producer 这边一条 failure 都没有、但 consumer 收到的少，问题可能在 producer→consumer 的引用分发（u7-l4 的 RefDistributor / quantum 窗口 / EOF 零头），而非推理面。
- 全程确认：**没有一张目标模型张量经过了 producer 进程**——producer 日志里只会出现 `sample_id`、`shape`、`dtype`、字节数，绝不应该出现张量值。

**预期结果**：你能在脑子里画出一张完整的「prompt → 服务端 prefill → 服务端写 Mooncake → 元数据回 producer → 校验 → adopt → commit」的时序图，并能据此判断「样本丢失」到底卡在哪一段。

## 6. 本讲小结

- **推理面是外部服务的传输边界**：在线训练里目标模型跑在独立的 patched SGLang 服务中，SpecForge 不在 producer / trainer 进程里加载或执行它（DESIGN.md 原话「an external-server transport boundary」）。
- **`RolloutWorker` 算法无关**：它只负责「lease → produce_refs/generate_features → commit」的流程骨架，算法细节全部甩给注入的 `CaptureConfig` 与 `feature_source`，且永远不把张量交给 controller。
- **`SGLangServerCaptureAdapter` 实现零拷贝**：服务端执行 prefill 并直写 Mooncake，HTTP 响应只回 key/shape/dtype 元数据；adapter 用这些元数据拼 `SampleRef`，producer 全程不碰张量。
- **三道关卡挡住坏样本**：身份三元组校验、序列长度校验、`verify_capture_specs` 契约校验；任一失败都「adopt + abort」释放服务端已写的 key，绝不留坏样本给下游。
- **`CaptureConfig` 是算法契约在推理面的化身**：它由算法的流式 `FeatureContract` 推导而来，把「缺特征 / 层 id 不符 / aux 宽度错 / 目标维度错 / pruned_logits 缺 vocab_map」五类错误从「trainer 训崩」提前到「捕获当场炸」。
- **解耦三件事**：目标执行（服务端）、算法策略（`CaptureConfig` + `ServerCaptureSchema` 注入）、部署接线（producer 装配）三者分开，adapter 自己从不解析算法名。

## 7. 下一步学习建议

本讲把推理面讲完了，它产出的 `SampleRef` 接下来要进入「引用分发与流式队列」。建议：

- **紧接着读 u7-l4（在线引用分发与流式队列）**：看 producer 提交的 `SampleRef` 如何经 `RefDistributor` 按 quantum 窗口分发到各 consumer rank 的 inbox，以及 `DPAckController` 如何在 optimizer 边界做 durable ack。本讲的「`commit_samples`」正是那条链的起点。
- **回看 u7-l3（数据平面 feature store 与传输）**：本讲反复提到的 `MooncakeFeatureStore.adopt` / `abort` / `track_external_attempt` 是 Mooncake 后端的「领养外部写入」API，u7-l3 给出了 FeatureStore 契约与三种后端的全貌。
- **若对算法侧的捕获层选取好奇**：读 u4-l3（算法 providers 与扩展端口）的 `resolve_capture_layers` 与 `ServerCaptureLayout`，看 `ServerCaptureSchema` 的字段是从哪儿来的。
- **若要做离线预计算**：本讲的 `inference/` 面与 `offline_capture/` 包是两套东西；离线请读 u5-l3（离线特征生成 prepare_hidden_states）与 `scripts/prepare_hidden_states.py`。
