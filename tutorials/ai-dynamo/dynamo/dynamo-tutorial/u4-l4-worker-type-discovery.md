# Worker 类型与服务发现：discovery 模块

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `WorkerType` 四种角色（prefill / decode / encode / aggregated）的语义，以及一个 worker 进程在注册时如何声明自己的角色与依赖（`needs`）。
2. 解释 `WorkerTypeReadiness` / `NamespaceReadiness` / `ModelReadiness` 如何用「DNF 依赖 + 存活计数」表达一个部署的 P/D 就绪状态，并能手工推算一次就绪判定。
3. 追踪 worker 上下线的完整链路：发现面事件 → `ModelDiscoveryController` 分组状态机 → `ModelManager` 提交/替换/移除 WorkerSet → `worker_count` 与就绪状态翻转。
4. 说出 `Model` 抽象在多后端共存时的作用：frontend 只按「模型名 + 引擎种类」取引擎，不关心背后是 vLLM、SGLang 还是 TensorRT-LLM。
5. 解释 #13861 重构后负载状态如何进入发现层：每个 WorkerSet 在物化时创建自己的 `RoutingLoadContext`，`LoadThresholdHandle` 成为共享配置入口，`KvWorkerMonitor` 从 WorkerSet 字段退居为上下文内部组件。

本讲是 u4-l1（引擎装配）与 u3-l2（服务注册模型）的汇合点：u3-l2 讲了「一个 endpoint 实例如何注册与被发现」，本讲讲「lib/llm 在发现之上如何把 worker 组织成可服务的拓扑」。

## 2. 前置知识

- **WorkerType（角色）**：一个 worker 进程在拓扑中的分工。聚合模式（aggregated）下一个进程包办 prefill 与 decode；分离模式下 prefill、decode（以及多模态的 encode）是不同进程池。
- **DNF（析取范式）**：布尔表达式的一种标准形式——「若干 AND 组的 OR」。本讲中 worker 的依赖声明 `needs: Vec<Vec<WorkerType>>` 就是 DNF：外层 Vec 是 OR，内层 Vec 是 AND。例如 `[[Prefill, Decode], [Aggregated]]` 表示「(有 prefill 且有 decode) 或 (有一个聚合 worker)」。
- **watch channel**：tokio 的单值广播通道（`tokio::sync::watch`）。发送端每次覆盖一个值，接收端总能 `borrow()` 到最新值。本讲中「当前存活的 worker 实例 id 列表」就是一个 `watch::Receiver<Vec<u64>>`。
- **DashMap**：并发分片哈希表，读写不需要全局锁；**ArcSwap**：原子指针交换容器，用来一次性发布一份只读快照（本讲的「目录发布」）。
- **GroupKey / cohort / fingerprint**：discovery 控制器里的分组概念——同一模型、同一 `(endpoint, model_type, worker_type)` 的 worker 实例归为一组；组内成员卡片内容（指纹）必须一致，否则进入 Conflict。
- **路由上下文（routing context）**：u4-l1 引入的术语，由 `RoutingLoadContext` 落地——一个类型化端点自持的负载生命周期：唯一的共享 `Client`、负载来源、调度负载通道、阈值配置与取消子树。本讲 4.5 讲它如何挂在发现层上。
- 需要回顾的旧讲结论：u3-l2 的「`v1/instances/{ns}/{comp}/{ep}/{instance_id}` 发现 key 与实例动态性」；u4-l1 的「网络组件要等 watcher 发现 worker 后才生成」；u3-l4 的「`dispatch_kv_admitted` 以 `OverloadCheck::AlreadyAdmitted` 免重复过载检查」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `lib/llm/src/worker_type.rs` | 角色类型的兼容再导出（仅 9 行），规范定义在 dynamo-kv-router |
| `lib/kv-router/src/worker_type.rs` | `WorkerType` 枚举的规范定义：序列化、解析、内置选择器标签 |
| `lib/llm/src/discovery/worker_set.rs` | `WorkerSet`：一组同类 worker + 它们专属的管线、负载上下文与阈值句柄 |
| `lib/llm/src/discovery/model.rs` | `Model` 与就绪类型（`WorkerTypeReadiness` 等）、引擎选择逻辑 |
| `lib/llm/src/discovery/readiness.rs` | 就绪判定的纯函数：`ReadinessUnit` → `ReadinessEval` |
| `lib/llm/src/discovery/model_manager.rs` | `ModelManager`：模型目录、发现组提交、拓扑对账、KV 路由器构造 |
| `lib/llm/src/discovery/watcher.rs` | `ModelWatcher`：把发现到的卡片物化成 WorkerSet（管线与负载上下文的构建处） |
| `lib/llm/src/discovery/controller.rs` | `ModelDiscoveryController`：分组状态机，驱动 commit/replace/remove |
| `lib/llm/src/discovery/worker_monitor.rs` | `LoadThresholdConfig` / `LoadThresholdHandle` / `KvWorkerMonitor`：负载阈值与过载监测 |
| `lib/llm/src/kv_router/routing_load.rs` | `RoutingLoadContext` / `RouterLoadSource` / `SchedulerLoadSender`：每个路由端点自持的负载生命周期（本次重构新增） |
| `lib/backend-common/src/worker.rs` | Rust `Worker`：按 `disaggregation_mode` 推导 `worker_type` 与 `needs` 并注册 |
| `lib/llm/src/model_card.rs` | `ModelDeploymentCard` 上的 `worker_type` / `needs` 字段与旧卡片兼容解析 |

模块根文件是 `lib/llm/src/discovery.rs`（[lib/llm/src/lib.rs:L12](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/lib.rs#L12) 的 `pub mod discovery;`），与 `discovery/` 目录并存的旧式布局。

## 4. 核心概念与源码讲解

### 4.1 WorkerType：角色的规范定义与声明链

#### 4.1.1 概念说明

一个 worker「是什么角色」是分离式服务的基础问题：frontend 的 PrefillRouter 只应把 prefill 工作发给 prefill worker；就绪判定需要知道「decode 池是否也活着」。Dynamo 把这个概念规范化为 `WorkerType` 枚举。注意 `lib/llm/src/worker_type.rs` 只是一个再导出——注释说明了原因：规范类型放在 `dynamo-kv-router` crate 里，路由宿主与策略提供者共用它又不会造成依赖环。

每个 worker 恰好一个角色；角色与模型的公开 API 面（chat/completions/embeddings…）是正交的两个维度。

#### 4.1.2 核心流程

角色不是手写的字符串，而是从 `WorkerConfig.disaggregation_mode` 推导而来。Python 侧 worker 只声明「我在分离拓扑里是哪一段」，Rust `Worker` 在注册时推导出 `(worker_type, needs)`：

```text
disaggregation_mode     →  (worker_type,   needs)
prefill                 →  (Prefill,       [[Decode]])            # 有 route_to_encoder 时 [[Decode, Encode]]
decode                  →  (Decode,        [[Prefill]])
aggregated              →  (Aggregated,    [])                    # 有 route_to_encoder 时 [[Encode]]
encode                  →  (Encode,        [[Prefill, Decode], [Aggregated]])
```

两个字段最终写进 `ModelDeploymentCard`，随卡片存入发现面，成为 frontend 侧一切拓扑判断的输入。

#### 4.1.3 源码精读

规范枚举定义——四种角色、线格式统一小写、内置选择器只区分 prefill 与「其余」两池：

[lib/kv-router/src/worker_type.rs:L15-L46](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-router/src/worker_type.rs#L15-L46) 定义 `WorkerType` 枚举、`as_str()`（wire/log/metric 的规范小写形式）与 `default_selector_label()`（内置选择器历史只分 prefill/decode 两池，保持该打分契约）。

[lib/kv-router/src/worker_type.rs:L68-L82](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv-router/src/worker_type.rs#L68-L82) 实现 `FromStr`：大小写不敏感、去空白解析，未知字符串（含空串和 `prefill|decode`）报 `ParseWorkerTypeError`。

[lib/llm/src/worker_type.rs:L4-L9](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/worker_type.rs#L4-L9) 是 lib/llm 侧的再导出，注释解释了为什么规范类型放在 kv-router（避免依赖环）。

角色推导的权威表——注册时由 Rust `Worker` 执行：

[lib/backend-common/src/worker.rs:L1871-L1897](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/backend-common/src/worker.rs#L1871-L1897) `resolve_worker_type_and_needs`：prefill 声明依赖 decode，decode 声明依赖 prefill，aggregated 无依赖，encode 的依赖是 DNF 两选一（P+D 或 Agg）。

卡片上的字段定义（注意 `needs` 的文档注释就是 DNF 语义）：

[lib/llm/src/model_card.rs:L916-L927](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/model_card.rs#L916-L927) `pub worker_type: Option<WorkerType>` 与 `pub needs: Vec<Vec<WorkerType>>`——外层 Vec 是 OR，内层 Vec 是 AND 集，空外层表示「无伙伴依赖」。

Python 侧的单一事实来源：

[components/src/dynamo/common/backend/worker.py:L124-L141](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/components/src/dynamo/common/backend/worker.py#L124-L141) `WorkerConfig.disaggregation_mode`（默认 AGGREGATED 保持旧调用方不变）与 `route_to_encoder`（让 agg/prefill 在 `needs` 里声明一个上游 Encode 伙伴；对 decode/encode 角色设置会在启动时被拒绝）。

跨版本兼容：旧 worker 的卡片没有 `worker_type`，frontend 用旧信号（`ModelType::Prefill` 标记位）重建角色——旧的 decode 与 aggregated 在线上无法区分，统一解析为 Aggregated，就绪路径因此对含旧卡片的命名空间放开严格门控（见 4.3）。解析逻辑现在收敛在卡片类型上：

[lib/llm/src/model_card.rs:L1011-L1022](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/model_card.rs#L1011-L1022) `ModelDeploymentCard::resolve_worker_type`：有 `worker_type` 原样返回；无则按「`model_type` 带 Prefill 标记位 → Prefill，否则 → Aggregated」重建。

[lib/llm/src/discovery/watcher.rs:L168-L179](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L168-L179) `effective_worker_type`：发现层的薄委托，直接调用上面的 `resolve_worker_type`——解析规则只有一份，watcher 与卡片共用。

#### 4.1.4 代码实践

**实践目标**：验证角色→依赖映射，理解它是「声明式」的。

1. 打开 [lib/backend-common/src/worker.rs:L1871-L1897](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/backend-common/src/worker.rs#L1871-L1897)，逐行抄下四种模式的 `(worker_type, needs)`。
2. 运行该 crate 的单测确认解析行为：`cargo test -p dynamo-kv-router worker_type`（覆盖 display 小写、大小写不敏感解析、未知串拒绝等断言，见文件尾部 tests 模块）。
3. **观察现象**：测试全部通过；`WorkerType` 的 JSON 线格式恒为小写字符串。
4. **预期结果**：你得到一张与 4.1.2 表一致的映射。命令运行结果**待本地验证**（本环境未执行）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `WorkerType` 的规范定义放在 `dynamo-kv-router` 而不是 `lib/llm`？
**答案**：`lib/llm/src/worker_type.rs` L4-L7 的注释写明：策略提供者（policy providers）与所有路由宿主（router hosts）都要共享这一类型；放在 kv-router 让双方依赖它而不引入 lib/llm ↔ kv-router 的依赖环。

**练习 2**：一个多模态部署由「1 个 aggregated worker（带 `--route-to-encoder`）+ 1 个 encode worker」组成。写出两者的 `needs`。
**答案**：aggregated → `needs = [[Encode]]`；encode → `needs = [[Prefill, Decode], [Aggregated]]`。两者互相声明对方，就绪判定要求两侧都被满足。

**练习 3**：旧版 decode worker（无 `worker_type` 字段）注册到新 frontend，会被解析成什么角色？会有什么后果？
**答案**：解析为 `Aggregated`（`resolve_worker_type`）。后果是该命名空间含「无 worker_type 的旧卡片」，就绪判定退回兼容路径「有活 worker 即 ready」，不做严格拓扑门控——因为旧 decode 与旧 aggregated 在线上不可区分，严格门控会误伤。

### 4.2 WorkerSet：动态 worker 集合

#### 4.2.1 概念说明

`WorkerSet` 是「同一命名空间/同一配置的一组 worker」加上「这组 worker 专属的完整管线」。它是 frontend 内存里的部署单元：引擎槽位（chat/completions/embeddings/…十一类）、负载上下文、PrefillRouter、encoder router 都挂在 WorkerSet 上，而不是挂在全局。这样两个部署（两个 namespace）即使服务同名模型，也各自拥有独立管线互不干扰。

它的「动态」体现在两处：一是 `worker_count()` 实时反映存活实例数（watch channel）；二是它的生命周期与发现联动（Drop 时取消后台任务）。

本次 #13861 重构改变了它的负载字段：旧的 `worker_monitor: Option<KvWorkerMonitor>` 被换成一对新字段——`load_context: Option<Arc<RoutingLoadContext>>`（拥有负载监测生命周期）与 `load_thresholds: Option<LoadThresholdHandle>`（可动态更新的共享阈值配置）。语义变化见 4.5。

#### 4.2.2 核心流程

一个 WorkerSet 的关键动态是**实例计数**：

```text
发现面实例列表变化
  → Client 的 instance_avail_watcher（watch::Receiver<Vec<u64>>）
  → WorkerSet::worker_count() = rx.borrow().len()   # 每次 borrow 实时读
  → 就绪判定 / 引擎选择 / 加权随机权重 全部随之变化
```

没有 watcher 的进程内模型（http/grpc 直接内嵌引擎）恒为 1。角色分类规则：

- `is_encode_set()`：按 `card.worker_type == Encode` 判定（角色即契约，Encode 没有引擎槽位）。
- `is_prefill_set()`：**非 encode 且无任何 serving 引擎**。即「没有引擎 ⇒ 视作 prefill 生命周期」这条旧规则，但把 Encode 排除出去。

#### 4.2.3 源码精读

结构体全貌——引擎槽位、负载上下文、路由器、实例 watcher、优雅拆除：

[lib/llm/src/discovery/worker_set.rs:L131-L183](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L131-L183) `pub struct WorkerSet`：`namespace`、`endpoint_id`、`mdcsum`（卡片校验和）、`card`（构建管线的依据）、十一个引擎 `Option` 槽位、`load_context`（L163，注释点明它「为不经过 RoutingHost 的路由面持有负载监测」）与 `load_thresholds`（L166）、`prefill_router` / `encoder_router`（存在这里正是为了 worker 全死时由 watcher 停用、重聚时重启，见字段注释 L168-L169）、`instance_count_rx`。

负载上下文的存取（`load_context()` 仅测试可见，供断言物化结果）：

[lib/llm/src/discovery/worker_set.rs:L231-L238](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L231-L238) `set_load_context` 与 `#[cfg(test)] load_context`。

实例计数的实时性：

[lib/llm/src/discovery/worker_set.rs:L370-L380](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L370-L380) `worker_count()`：`Some(rx) => rx.borrow().len()`，`None => 1`（进程内模型）。`set_instance_watcher`（L381）必须在包进 `Arc` 之前调用。

角色分类的三个谓词：

[lib/llm/src/discovery/worker_set.rs:L312-L360](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L312-L360) `has_any_serving_engine()`（注释强调它是「有没有能出推理结果的东西」的单一事实来源，新增模态只需改这里）与 `is_encode_set()` / `is_prefill_set()`——注释解释了为什么 Encode 必须从 prefill 分类中挖出：否则 `/v1/models` 的可见性会错误地门控在一个不存在的 PrefillRouter 上。

watcher 侧的接线点（实例 watcher 从哪来）：

[lib/llm/src/discovery/watcher.rs:L467-L481](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L467-L481) `prepare_worker_set` 中：`endpoint.client()` 拿客户端 → `with_admitted_instances_and_cancellation` 绑定准入列表 → `instance_avail_watcher()` 取实例 watch → `worker_set.set_instance_watcher(instance_watcher)`。同一函数还设置拓扑端点与生命周期取消令牌。

#### 4.2.4 代码实践

**实践目标**：用单元测试验证 worker_count 的动态语义（无需任何运行环境）。

1. 阅读 [lib/llm/src/discovery/worker_set.rs:L718-L770](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L718-L770) 的测试：`test_worker_count_without_watcher`（恒 1）、`test_worker_count_with_watcher`（3→1→0）、`test_worker_count_with_empty_watcher`（空列表）、`test_worker_count_updates_on_join`（0→1→2→3）。
2. 运行：`cargo test -p dynamo-llm worker_count`。
3. **观察现象**：`watch::channel` 的发送端 `tx.send(vec![...])` 每次覆盖值后，下一次 `worker_count()` 立即变化，没有任何回调或钩子。
4. **预期结果**：测试通过；你能在 30 秒内向同伴解释「为什么缩容到 0 不需要清理钩子也能让就绪翻转」——因为就绪是每次查询时活算的（见 4.3）。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `prefill_router` 存在 WorkerSet 上而不是全局单例？
**答案**：worker_set.rs L168-L169 注释：为了让 watcher 在所有 prefill worker 死亡时停用它、重新加入时再激活；且每个 WorkerSet 有自己的管线与目标，全局单例无法区分多个部署。

**练习 2**：一个 WorkerSet 引擎槽位全空、`worker_type` 为 None，它会被分类成什么？哪条测试守卫了这一点？
**答案**：`is_prefill_set() == true`（非 encode 且无引擎）。守卫测试在 model.rs：[lib/llm/src/discovery/model.rs:L999-L1012](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L999-L1012) `test_no_engines_means_prefill` 断言无引擎集合使 `model.has_prefill()` 为真且没有任何 serving 引擎；worker_set.rs 的 [StubEngine（L507-L530）](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_set.rs#L507-L530) 是专门为逐字段检验该分类而生的占位引擎。

**练习 3**：进程内模型（`Input::Http` 直连引擎）的 `worker_count()` 是多少？为什么？
**答案**：1。它没有发现客户端、没有 `instance_count_rx`，本地引擎恒有一个「worker」（它自己）。

### 4.3 Model 与 WorkerTypeReadiness：P/D 就绪状态的数学

#### 4.3.1 概念说明

`Model` 是「一个具名模型」（如 `llama-3-70b`）在 frontend 内存中的条目，它聚合一个或多个 WorkerSet（每个对应一个 namespace，即一次部署）。它回答三个问题：

1. **这个模型现在能服务吗？**（就绪：`is_workers_ready` / `is_ready_to_serve`）
2. **给我一个能跑 chat/generate 的引擎。**（引擎选择：`get_chat_engine` 等）
3. **这个模型该出现在 `/v1/models` 里吗？**（可见性：`is_displayable`）

就绪的核心洞察是**依赖是双向声明的**：prefill 声明 `needs=[[Decode]]`，decode 声明 `needs=[[Prefill]]`。所以「只有 prefill 活着」时，prefill 自己的 needs 不满足 → 不就绪。这天然表达了 P/D 必须成对出现的要求，而无需任何中心化的拓扑配置。

`WorkerTypeReadiness` 是就绪的**展示层**结构（`GET /v1/models/{model}/ready` 的响应体），与门控共享同一份计算，保证「报告的就绪」与「路由接受的现实」永不矛盾。

#### 4.3.2 核心流程

就绪判定是纯函数，输入是一组 `ReadinessUnit`（每个 WorkerSet 投影成一个单元：角色 + 存活数 + needs）：

先定义集合（对一个 namespace \(N\)）：

\[ P(N) = \{\,\tau : \exists\, u \in N,\ \mathrm{type}(u)=\tau \,\wedge\, \mathrm{live}(u) > 0\,\} \quad\text{（present：有活 worker 的类型）} \]

单元 \(u\) 的依赖满足条件（DNF 语义，至少一个 AND 组被 present 覆盖）：

\[ \mathrm{satisfied}(u) \iff \exists\, a \in \mathrm{needs}(u) : a \subseteq P(N) \]

缺失集合与歧义集合：

\[ M(N) = \underbrace{\{\tau : \tau \text{ 注册过但 } \tau \notin P(N)\}}_{\text{整池死亡}} \cup \underbrace{\{\,\tau \in a : a \in \mathrm{needs}(u),\ u \text{ 活},\ \mathrm{satisfied}(u) = \bot\,\}}_{\text{活单元缺的伙伴}} \]

\[ A(N) = \{\,\tau \ne \mathrm{Aggregated} : \text{类型 } \tau \text{ 的活单元数} > 1\,\} \quad\text{（ambiguous）} \]

严格路径的就绪判定：

\[ \mathrm{ready}(N) \iff \big(\exists\, u \in N: \mathrm{live}(u)>0\big) \;\wedge\; M(N)=\varnothing \;\wedge\; A(N)=\varnothing \]

兼容路径（\(N\) 含无 `worker_type` 的旧卡片）：\(\mathrm{ready}(N) \iff \exists\, u: \mathrm{live}(u)>0\)。

手工推演一遍「先 prefill 后 decode」：

| 阶段 | present | 活单元 needs | missing | ready |
|------|---------|--------------|---------|-------|
| 只有 prefill（1 活） | {prefill} | prefill: [[decode]] 未满足 | {decode} | ✗ |
| prefill + decode 各 1 活 | {prefill, decode} | 双方均满足 | ∅ | ✓ |
| decode 全部死亡 | {prefill} | prefill 未满足 | {decode} | ✗ |

注意「就绪是查询时活算的」：没有任何钩子在 worker 死亡时去改一个 `ready` 标志——`worker_count` 变了，下次判定自然翻转（model.rs 的测试 `readiness_scale_down_flips_to_not_ready` 固定了这一行为）。

#### 4.3.3 源码精读

就绪的三层展示结构：

[lib/llm/src/discovery/model.rs:L49-L79](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L49-L79) `WorkerTypeReadiness`（每类型存活数 + 它声明的 needs，注释点明 needs 是 DNF）、`NamespaceReadiness`（一个部署的就绪 + 缺失列表）、`ModelReadiness`（跨所有 namespace 的汇总，即 `/v1/models/{model}/ready` 的响应体）。

Model 本体与 WorkerSet 目录：

[lib/llm/src/discovery/model.rs:L101-L127](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L101-L127) `pub struct Model { name, worker_sets: DashMap<String, Arc<WorkerSet>> }` 与 `add_worker_set`（日志 "Adding worker set to model" 是实践时要盯的日志行）。模块头注释（L6-L7）说明请求按 worker 数加权随机路由到 WorkerSet。

门控入口与共享求值：

[lib/llm/src/discovery/model.rs:L331-L356](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L331-L356) `is_workers_ready(namespace)`：过滤出该 namespace 的 WorkerSet 后取 `evaluate_namespace(..).ready`。其文档注释完整陈述了 DNF 语义与跨版本兼容分支（含 `TODO(v1.5)` 的 shim 移除计划）。

[lib/llm/src/discovery/model.rs:L367-L395](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L367-L395) `evaluate_namespace`：把每个 WorkerSet 投影成 `ReadinessUnit`（`ws_type_and_needs` 在 L321-L329：无 `worker_type` 的卡片返回 `None`，这就是旧卡片信号），委托纯函数 `evaluate_readiness`，并对旧卡片打一次进程级告警（`warn_legacy_readiness_once`，L38-L47 用 `std::sync::Once` 保证只打一次）。

就绪数学的权威实现：

[lib/llm/src/discovery/readiness.rs:L54-L93](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/readiness.rs#L54-L93) 第一遍扫出 present 集与旧卡片标记；含旧卡片即短路返回兼容判定（`ready = has_live_worker`），注释解释了为什么必须如此。

[lib/llm/src/discovery/readiness.rs:L95-L138](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/readiness.rs#L95-L138) 严格路径：先算 ambiguous（同类型多个活单元且非 Aggregated = 拓扑歧义，比如两个不同 prefill 池注册进了同一路径）；再算 missing——「注册过但无活 worker」与「活单元 needs 中未被覆盖的伙伴」都进 missing；末尾汇总出 \(\mathrm{ready} = \text{has\_live} \wedge \text{missing 空} \wedge \text{ambiguous 空}\) 三条件合取。

展示层如何复用同一份事实：

[lib/llm/src/discovery/model.rs:L411-L495](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L411-L495) `namespace_readiness()`：逐 namespace 调 `evaluate_namespace`（注释「Authoritative readiness facts (shared with the serving gate)」），再叠加每类型计数等展示数据；reason 字符串的四种情形也在其中——就绪且含旧卡片（注明 bypass）、旧卡片全死、ambiguous、`missing worker types: prefill`（实践时你会亲眼看到这条）。

就绪与路由的强一致：

[lib/llm/src/discovery/model.rs:L733-L770](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L733-L770) `select_worker_set_with`：先做一次快照（注释解释为什么——避免迭代中重入 DashMap 死锁，并保证就绪过滤与候选资格用同一份视图），算出 ready 的 namespace 集合，单个 WorkerSet 时快路径同样要过 `worker_count() == 0 || !ready` 两道闸。

[lib/llm/src/discovery/model.rs:L795-L806](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L795-L806) 多候选时按 worker 数加权随机选择——这就是「请求按存活 worker 数比例分摊」的落点。

[lib/llm/src/discovery/model.rs:L522-L530](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L522-L530) `is_ready_to_serve`：KServe `model_ready` 用；文档注释点明它委托 `select_worker_set_with`，因此「就绪报告 = 路由会接受什么」，含 namespace 完整性闸（活的 decode-only 集合不会误报 ready）。

本次重构后负载阈值配置的读写入口（原来走 `worker_monitor`，现在走共享句柄）：

[lib/llm/src/discovery/model.rs:L683-L700](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L683-L700) `load_threshold_config`：从第一个带阈值句柄的 WorkerSet 读配置；传入 `Some(config)` 时更新**每一个**句柄——注释说明多个 WorkerSet 可能共享同一个句柄。它被 HTTP 服务用于动态改阈值（详见 4.5）。

#### 4.3.4 代码实践

**实践目标**：用现成单测验证 4.3.2 的三行推演表。

1. 阅读 [lib/llm/src/discovery/model.rs:L1333-L1356](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L1333-L1356)：`readiness_pd_pair_ready`（P+D 各 1 活 → ready）与紧随其后的 `readiness_pd_missing_prefill_not_ready`（L1357 起，只有 decode → not ready）。注意测试辅助函数 `ws_with_type`（L1308-L1322）如何用 `watch::channel` 直接控制存活数。
2. 再读 [lib/llm/src/discovery/model.rs:L1637-L1660](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model.rs#L1637-L1660) `readiness_detail_decode_only_reports_missing_prefill`：断言 `missing_worker_types == ["prefill"]` 且 reason 正是 `"missing worker types: prefill"`。
3. 运行：`cargo test -p dynamo-llm readiness`。
4. **观察现象**：一批 readiness 测试通过，覆盖 P/D、E-P-D、跨 namespace 隔离、缩容翻转、旧卡片兼容等。
5. **预期结果**：全绿。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：namespace `ns-old` 里只有 prefill，namespace `ns-new` 里只有 decode（同名模型）。整个模型 ready 吗？
**答案**：不 ready。`readiness_cross_namespace_isolation`（model.rs L1457-L1480）固定了该行为：就绪按 namespace 评估，跨 namespace 的伙伴不算数——每个 namespace 是一次完整部署。

**练习 2**：两个不同指纹的 prefill 组注册到了同一路径下，就绪结果是什么？为什么要有这个规则？
**答案**：ambiguous={prefill}，不 ready（readiness.rs 严格路径：非 Aggregated 类型出现多个活单元即歧义）。规则动机：同一路径多个异构 prefill 池意味着部署配置冲突（controller 侧同样会以 Conflict 拒绝提交，见 4.4），与其猜测路由给谁，不如显式不可用。而 Aggregated 豁免（多副本加权随机是合法的扩容）。

**练习 3**：`is_displayable()` 与 `is_ready_to_serve()` 有何区别？为什么 prefill-only 的模型可以 displayable 但不能 serve？
**答案**：`is_displayable`（model.rs L532-L546）回答「该不该出现在 `/v1/models`」，对无引擎集合有 prefill 兜底；`is_ready_to_serve`（L522-L530）回答「现在能不能真的跑一条推理」，要求有 serving 引擎的完整集合。prefill-only 可见是因为部署正在成形（KServe 竞态注释），但它没有面向用户的引擎，不能服务。

### 4.4 ModelManager 与 discovery 控制器：worker 上下线的完整链路

#### 4.4.1 概念说明

`ModelManager` 是 frontend 进程的模型目录中枢：`ModelManager → Model → WorkerSet` 三层存储，外加按 `EndpointId` 索引的运行时配置 watch、LoRA 域、HiCache 等旁路状态。它的关键设计是**双视图**：可变的 `DashMap`（注册路径写）+ 不可变的 `ArcSwap` 目录快照（请求路径读）。任何发现变化都在可变地图里组装完毕后，一次指针交换发布——请求侧永远看到一份一致的目录，从不需要锁。

`ModelDiscoveryController` 是驱动方：消费发现面事件（u3-l2 的实例上下线），把实例归入 `GroupKey` 分组，用一个小状态机（`GroupStatus`）决定何时构建、提交、替换或撤除一个 WorkerSet。`ModelWatcher` 实现 `ControllerHost` 回调，是管线真正的构建处。

**Model 在多后端共存时的作用**在这里看得最清楚：frontend 只持有 `Model` 与引擎种类，`prepare_worker_set` 按**卡片声明**（model_input × model_type × worker_type）装配管线——卡片来自 vLLM、SGLang、TRT-LLM 还是 sample 后端，对这一层完全透明。

#### 4.4.2 核心流程

worker 上线（一条 Added 事件的生命周期）：

```text
etcd/file 发现面: DiscoveryEvent::Added(instance)          # u3-l2 的实例注册
  → controller.apply_event → host.normalize (watcher.rs)
      反序列化卡片 → 校验 → GroupKey{model_name,
        worker_set_key = (namespace, component, endpoint, model_type, worker_type)}
  → apply_added: groups[GroupKey].insert(instance)
  → reconcile_group:
      组为空？        → remove_group（撤除已提交的 WorkerSet）
      组内多指纹？    → Conflict（拒绝提交，防异构混搭）
      单指纹:
        首次          → 状态机 Idle→Queued→Building → prepare() 物化 WorkerSet
                       → commit_group → ModelManager::commit_discovery_group
                       （Model 下挂 primary + 别名 + LoRA adapter view，发布目录）
        已 Ready 且指纹未变、仅成员增减
                      → replace_group（原位换成员表，不重建管线）
  → publish_catalog_locked：ArcSwap 一次交换，请求侧即刻可见
```

worker 下线：

```text
DiscoveryEvent::Removed → apply_removed（从 desired/groups 摘除）
  → 组空   → remove_group → remove_discovery_group
              （Model::remove_worker_set、清空 prefill/encoder router 目标、
               对账拓扑、发布目录；Model 空了连 Model 一起删）
  → 组仍在 → replace_group（成员表收缩）或维持
```

同组扩缩容（不重建管线的快路径）：controller 的 `admission_tx`（watch 通道）维护「已准入实例 id 列表」；组就绪且指纹不变时，`reconcile_group` 只更新这个列表，它一路流到 `Client` 的实例 watcher，最终体现为 `worker_count()` 变化与加权随机权重变化——这正是 4.2 与 4.3 观察到的动态性的源头。

`GroupStatus` 状态机（controller.rs L105-L134）：`Idle → Queued → Building → Ready`，异常分支 `Retrying`（构建失败退避重试）、`Conflict`（组内指纹分裂）、`Blocked/BlockedReady`（替换失败时保住最后一次安全提交）。

#### 4.4.3 源码精读

三层目录与双视图：

[lib/llm/src/discovery/model_manager.rs:L151-L205](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L151-L205) `pub struct ModelManager`：文档注释（L151-L156）写明层级「ModelManager → Model → WorkerSet，每个 WorkerSet 拥有按其配置构建的完整管线」并告诫「不要实现 Clone，放进 Arc」；`models: DashMap` + `catalog: ArcSwap<CommittedCatalog>`。

[lib/llm/src/discovery/model_manager.rs:L234-L254](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L234-L254) `publish_catalog_locked`：对每个 Model 做 `snapshot()`（成员表拷贝、WorkerSet 共享），装进新 `CommittedCatalog` 一次 `store`。

分组与状态机：

[lib/llm/src/discovery/controller.rs:L29-L39](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L29-L39) `GroupKey { model_name, worker_set_key }`——分组粒度即 4.1 里 `worker_set_key(endpoint, model_type, worker_type)` 的输出。

[lib/llm/src/discovery/controller.rs:L105-L134](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L105-L134) `enum GroupStatus` 八个状态，含 `BlockedReady`（带着旧提交阻塞等待重试）。

事件入口与增删：

[lib/llm/src/discovery/controller.rs:L277-L307](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L277-L307) `apply_event`：Added → normalize → `apply_added`；解析失败打 error 并**保留上一次合法的期望状态**（不因一张坏卡片摧毁服务）。

[lib/llm/src/discovery/controller.rs:L309-L352](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L309-L352) `apply_added`：同 key 且指纹一致 → 幂等返回；试图原位改变物化（换组或换指纹）→ 拒绝并报「实例路径标识不可变的化身」；否则入组并 `reconcile_group`。

[lib/llm/src/discovery/controller.rs:L354-L374](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L354-L374) `apply_removed`：摘除后对受影响组重 reconcile。

对账的三条出口：

[lib/llm/src/discovery/controller.rs:L383-L391](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L383-L391) 组空 → 清空准入列表、取消构建、若曾提交过则 `remove_group`（worker 下线撤除 WorkerSet 的出口）。

[lib/llm/src/discovery/controller.rs:L393-L407](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L393-L407) 多指纹 cohort → Conflict（增量代数、不提交）。

[lib/llm/src/discovery/controller.rs:L416-L462](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L416-L462) 已 Ready 且指纹相同 → `replace_group` 原位换成员（同组扩缩容不重建管线的关键路径）；失败且成员未变时保住旧提交进入 `BlockedReady`。

提交与撤除在 ModelManager 侧的落地：

[lib/llm/src/discovery/model_manager.rs:L642-L668](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L642-L668) `commit_discovery_group` 头部：取代表卡片、主名与别名、namespace；持保留锁后做名字占用检查（主名不得已是别名的别名等）。

[lib/llm/src/discovery/model_manager.rs:L704-L745](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L704-L745) 提交主体：WorkerSet 挂到主名 Model（L705-L706），同一 WorkerSet 以共享 `Arc` 挂到每个别名（L707-L711），LoRA adapter 卡片经 `adapter_view()` 包装成带 LoRA 身份注入的视图再挂载（L712-L716，`adapter_view` 见 worker_set.rs L419-L460，它同样克隆 `load_context` / `load_thresholds`，L451-L452）——「多后端/多名字共存」的机制细节；随后登记卡片、写组记录、对账拓扑、发布目录。

[lib/llm/src/discovery/model_manager.rs:L881-L927](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L881-L927) `remove_discovery_group`：摘卡片、从主名/别名/adapter 名下移除 WorkerSet、`clear_worker_set_targets` 清空 prefill/encoder router 目标、空 Model 连根删除、发布目录。

[lib/llm/src/discovery/model_manager.rs:L2313-L2338](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L2313-L2338) `reconcile_discovery_topology`：提交/撤除后按 `card().worker_type` 找出 prefill 提供者与 decode 消费者——**当且仅当恰好一提供者一消费者**时把 PrefillRouter 的目标设成该 prefill 端点。这是「frontend 如何知道 decode 该把 prefill 工作发给谁」的答案，也是 u7-l2 PrefillRouter 的接线预告。

物化现场（管线与负载上下文的构建处）：

[lib/llm/src/discovery/watcher.rs:L1039-L1073](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L1039-L1073) `normalize`（ControllerHost 回调）：反序列化卡片 → `normalize_legacy_prefill_topology`（旧 prefill 卡片投影，readiness.rs L42-L51）→ 计算 `GroupKey`（用 `worker_set_key`）→ 产出 `DesiredInstance`。

[lib/llm/src/discovery/watcher.rs:L483-L533](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L483-L533) `prepare_worker_set` 的两个角色短路：Encode（L483-L497，无公开 OpenAI 面，仅注册供 EncoderRouter 发现）与 Prefill（L501-L533，不建任何 OpenAI 管线，日志 "Prefill worker detected, registering and activating prefill router" 在 L528-L532，是实践时另一条要盯的日志）。

[lib/llm/src/discovery/watcher.rs:L1085-L1121](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L1085-L1121) `commit_group`（ControllerHost 回调）：取出物化好的 WorkerSet，替换代表成员的卡片为物化版（含 download_config 副作用），委托 `manager.commit_discovery_group`。

#### 4.4.4 代码实践

**实践目标**：静态追踪一条上线链路，画出调用图。

1. 从 [lib/llm/src/discovery/controller.rs:L277](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/controller.rs#L277) 的 `apply_event` 出发，沿 `apply_added → reconcile_group → prepare → commit_group → commit_discovery_group → publish_catalog_locked` 逐函数阅读（行号见 4.4.3）。
2. 在每个函数旁标注：它读/写了 `ModelManager` 的哪个字段？是否持 `reservation_lock`？是否跨 `.await`？
3. **观察现象**：你会发现注册路径全部同步（锁内、不 await），唯一的异步点在 `prepare`（下载配置、建管线、启动负载上下文）；目录发布是纯指针交换。
4. **预期结果**：得到一张标注了锁与异步点的调用图，能回答「为什么请求路径完全无锁」。无需运行任何进程。

#### 4.4.5 小练习与答案

**练习 1**：同组内第 2 个 decode worker 上线（与第 1 个同卡片），会重建 WorkerSet 吗？
**答案**：不会。指纹相同、组已 Ready，`reconcile_group` 走 `replace_group` 分支（controller.rs L416-L462）只换成员表；存活数的变化经 `admission_tx` 流到实例 watcher，`worker_count()` 从 1 变 2，加权随机权重随之翻倍。

**练习 2**：为什么 `publish_catalog_locked` 要对成员表做拷贝（`Model::snapshot`），却共享 WorkerSet 本身？
**答案**：model.rs `snapshot`（L184 起）的注释——WorkerSet 的引擎与路由生命周期是长久的，可共享；成员表若不拷贝，旧目录会被后续发现变更穿透，破坏「发布即不变」的快照语义。

**练习 3**：`commit_discovery_group` 里 LoRA adapter 为什么不能直接复用 WorkerSet，而要 `adapter_view()`？
**答案**：`adapter_view`（worker_set.rs L419-L460）把引擎包进 `LoraContextEngine`/`LoraGenerateEngine`，在请求里注入 adapter 身份（如 `routing.lora_name`），保证打向该「模型名」的请求携带正确 LoRA 标识；realtime 因双向流无法注入而显式置 `None`（fail closed，宁可不服务也不用基座权重冒充）。

### 4.5 负载状态进入发现层：RoutingLoadContext 与 LoadThresholdHandle（本次重构）

#### 4.5.1 概念说明

#13861（「每个路由上下文自持负载状态」）重构了发现层的负载接线。**重构前**：`prepare_worker_set` 为每个 WorkerSet 直接造一个 `KvWorkerMonitor` 存在字段上，KV 路由器、PrefillRouter、PushRouter 各自拿 Client，靠一段源码注释提醒作者「monitor 必须用 KvRouter 的 Client，否则 PushRouter 永远看不到过载状态」——正确性靠约定。**重构后**：新建 470 行的 `lib/llm/src/kv_router/routing_load.rs`，引入 `RoutingLoadContext`——一个类型化端点（decode / aggregated / prefill / encode）自持的负载生命周期容器：

- **唯一共享 `Client`**：该上下文的所有选点面与分发面拿到的都是这一个 Client 的克隆，过载状态（`set_overloaded_instances`）天然互通——原来的约定变成了结构保证；
- **`RouterLoadSource`**：负载的来源角色，`Encode` 显式豁免序列负载监测；
- **`SchedulerLoadSender`**：非阻塞的调度负载发布通道（容量 256，满了合并最新快照而不是阻塞路由热路径）；
- **`LoadThresholdHandle`**：可动态更新的共享阈值配置；
- **取消子树**：`Drop` 即取消，WorkerSet 撤除时负载任务随之收尾。

`KvWorkerMonitor` 并没有消失，而是从「WorkerSet 的公开字段」退居为「上下文内部组件」：只有监测序列负载的来源（Decode/Aggregated/Prefill）才会创建它。与 u3-l4 呼应：正因为负载状态按上下文自持，KV 选点做完过载准入后的直发走 `dispatch_kv_admitted`（`OverloadCheck::AlreadyAdmitted`），不会被自己刚发布的负载自误拒；选点与分发细节归 u6-l2，本讲只讲「它如何被发现层创建和持有」。

#### 4.5.2 核心流程

一个 WorkerSet 物化时（`prepare_worker_set` 的 Backend 分支）的负载接线：

```text
卡片就绪、需要预处理路由（needs_preprocessed_routing = 需要 chat/generate 管线或分词器）
  → LoadThresholdHandle::new(router_config.load_threshold_config)     # 阈值来自 RouterConfig
  → source = RouterLoadSource::from_worker_type(effective_worker_type(card))
  → RoutingLoadContext::start(client, source, thresholds, &cancellation, allocator_trim)
       ├─ source 监测序列负载（Decode/Aggregated/Prefill）？
       │    是 → 建 scheduler_load_channel + KvWorkerMonitor::new(...)
       │         → monitor.start_monitoring()   # 后台任务，发布过载实例到 Client
       │    否（Encode）→ SchedulerLoadSender::disabled(...)，无 monitor
       └─ 返回 Arc<RoutingLoadContext>（Drop 时取消整个子树）
  → KV 模式：kv_chooser 用 load_context.client() + scheduler_load_sender + cancellation 构造
  → PrefillRouter 改拿 (load_thresholds, cancellation) 而不是 monitor
  → worker_set.load_thresholds = Some(handle)
  → build_preprocessed_routing(load_context, kv_chooser, ...)          # 路由宿主保活上下文
```

非预处理的「直出面」（embeddings 等按卡片声明的 surface）在另一分支各自建独立的 `RoutingLoadContext` 并 `worker_set.set_load_context(...)` 存活。运行期改阈值走 `Model::load_threshold_config(Some(cfg))`——更新该模型所有 WorkerSet 的句柄（句柄可共享，见 4.3.3）。

#### 4.5.3 源码精读

负载来源的四值枚举与两个关键谓词：

[lib/llm/src/kv_router/routing_load.rs:L23-L66](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L23-L66) `RouterLoadSource`（Decode/Aggregated/Prefill/Encode）：`from_worker_type` 从角色映射；`from_worker_role_or_metric` 兼容只有 metric 标签的旧调用；`monitors_sequence_load()` 对 Encode 返回 false——编码端点不参与序列负载。

上下文本体——一个类型化端点的全部负载生命周期：

[lib/llm/src/kv_router/routing_load.rs:L253-L331](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L253-L331) `RoutingLoadContext` 与 `start()`：按来源决定是否创建 `KvWorkerMonitor` 并启动监测；文档注释点明「每个选点与分发面收到的都是本上下文唯一 endpoint `Client` 的克隆，decode、aggregated、prefill 上下文彼此独立」。访问器 `client()` / `scheduler_load_sender()` / `load_thresholds()` / `cancellation_token()` / `monitor()` 都在这里。

[lib/llm/src/kv_router/routing_load.rs:L333-L337](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L333-L337) `Drop for RoutingLoadContext`：取消取消令牌——上下文 dropping 即负载任务收尾，这就是 WorkerSet 撤除时不需要手工清理 monitor 的原因。

非阻塞的调度负载发布（防止路由热路径被反压）：

[lib/llm/src/kv_router/routing_load.rs:L141-L196](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L141-L196) `SchedulerLoadSender::publish/publish_batch`：`try_send` 永不 await；通道满时把快照并入 overflow map（同 worker 只留最新）并 `notify_one` 唤醒接收端，2 的幂次计数打一条 warn；通道意外关闭且未取消时记 error。

阈值配置与共享句柄：

[lib/llm/src/discovery/worker_monitor.rs:L92-L143](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_monitor.rs#L92-L143) `LoadThresholdConfig`：三个全 opt-in 的阈值（decode 块利用率 0-1、prefill 绝对 token 数、占 max_num_batched_tokens 的比例）；全 None 时过载拒绝整体停用；`validate()` 供启动与动态配置共用。

[lib/llm/src/discovery/worker_monitor.rs:L145-L174](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_monitor.rs#L145-L174) `LoadThresholdHandle`：`Arc<RwLock<LoadThresholdConfig>>`；`update` 只覆盖传入的 `Some` 字段（部分更新语义），`get`/`is_configured` 供监测任务每次判定时读——这就是「多个路由上下文共享一份可动态修改的配置」的机制。

监测器的新形态：

[lib/llm/src/discovery/worker_monitor.rs:L537-L591](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/worker_monitor.rs#L537-L591) `KvWorkerMonitor` 与新构造器 `new(client, source, scheduler_load_rx, thresholds, cancellation_token, task_guard)`：现在显式接收来源、调度负载接收端、阈值句柄与取消令牌；克隆共享内部 Arc 状态；`MonitorLifecycle` 的 Drop 取消令牌（L552-L560）。过载判定本体 `is_overloaded`（L393 起）按 `LoadThresholdConfig` 逐字段检查；`WorkerLoadMonitor::start_monitoring` 在 L650-L655。

watcher 的接线现场（本模块的「发现层入口」）：

[lib/llm/src/discovery/watcher.rs:L567-L592](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L567-L592) `prepare_worker_set` 里创建 `LoadThresholdHandle` 并按需 `RoutingLoadContext::start`——来源用 `effective_worker_type` 解析，父取消令牌是 WorkerSet 的生命周期令牌，task_guard 传 allocator_trim。

[lib/llm/src/discovery/watcher.rs:L595-L625](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L595-L625) KV 模式下 `kv_chooser_for_with_selector_and_client` 的调用改传 `load_context.client()`、`scheduler_load_sender()`、`cancellation_token()`——KV 路由器与负载上下文共用同一个 Client。

[lib/llm/src/discovery/watcher.rs:L639-L663](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L639-L663) PrefillRouter 的构造参数从 `worker_monitor` 换成 `(load_thresholds, cancellation.child_token())`——prefill 侧只需要阈值与生命周期，不再持有 monitor。

[lib/llm/src/discovery/watcher.rs:L678-L695](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L678-L695) `worker_set.load_thresholds` 赋值，`build_preprocessed_routing` 改收 `load_context`（u4-l1 讲过的装配函数，其 `KvWorkerMonitor` 参数被上下文取代）。

[lib/llm/src/discovery/watcher.rs:L826-L845](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L826-L845) 与 [L951-L953](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L951-L953)：直出面分支各自 `RoutingLoadContext::start` 后 `worker_set.set_load_context(load_context)`——不经过 RoutingHost 的面由 WorkerSet 保活上下文（对应 worker_set.rs L163 字段注释）。

standalone 场景的封装（独立路由进程用）：

[lib/llm/src/discovery/model_manager.rs:L1941-L1991](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L1941-L1991) `managed_kv_router_for` / `managed_kv_router_for_with_worker_role`：便捷构造。

[lib/llm/src/discovery/model_manager.rs:L1993-L2038](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L1993-L2038) `managed_kv_router_for_with_selector`：自己 `RoutingLoadContext::start`，再把上下文的 sender/令牌喂给 KV 路由器，包成 [ManagedKvRouter](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L339-L390)（L339-L390：选点面 + 拥有其负载任务的上下文，Deref 到 `KvRouter`）——「选点能力」与「负载所有权」打包交付。

对照：旧的 `kv_chooser_for`（[model_manager.rs:L1849-L1870](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/model_manager.rs#L1849-L1870)）现在内部造一个 `SchedulerLoadSender::disabled`——不接负载监测的调用方拿到的是显式关闭的 sender，而不是隐式的「没有 monitor」。

#### 4.5.4 代码实践

**实践目标**：用重构配套的测试验证三条语义：饱和合并、Encode 豁免、阈值经发现层注入。

1. 读 [lib/llm/src/kv_router/routing_load.rs:L405-L431](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L405-L431) `saturated_channel_coalesces_batch_and_later_absolute_state_converges`：灌满小容量通道，断言接收端先收到合并快照、后续绝对状态收敛。
2. 读 [lib/llm/src/kv_router/routing_load.rs:L432-L470](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/kv_router/routing_load.rs#L432-L470) `encode_context_retains_client_with_sequence_load_monitoring_disabled`：Encode 来源的上下文保留 Client、无 monitor、sender 关闭、父令牌未取消。
3. 读 [lib/llm/src/discovery/watcher.rs:L1441-L1520](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L1441-L1520) `text_routes_retain_graph_and_preserve_declared_surfaces`：物化一个 aggregated WorkerSet 后断言 `load_context.source() == Aggregated`、`monitor().is_some()`、Client 端点正确，且 `RouterConfig` 里配置的 `active_decode_blocks_threshold: Some(0.8)` 能从 `Model::load_threshold_config` 读回——「阈值经发现层注入」的端到端验证。
4. 运行：`cargo test -p dynamo-llm routing_load` 与 `cargo test -p dynamo-llm text_routes_retain surface_encode_worker`（后者是 [watcher.rs:L1530](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L1530) 的 Encode 端到端用例）。
5. **观察现象**：测试通过；注意 watcher 测试不需要 GPU，但需要能构建 dynamo-llm（Linux 环境）。
6. **预期结果**：全绿。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `RouterLoadSource::Encode` 不创建 `KvWorkerMonitor`？
**答案**：Encode 端点只做媒体编码，不持有 KV 序列负载，`monitors_sequence_load()` 对 Encode 返回 false（routing_load.rs L63-L65）；`start` 走 else 分支装一个 `disabled` sender。强行监测只会产出无意义的过载信号。

**练习 2**：重构前「monitor 必须用 KvRouter 的 Client」是一条源码注释纪律；重构后靠什么保证？
**答案**：靠结构：`RoutingLoadContext` 是 Client 的唯一所有者，KV 路由器经 `load_context.client()` 拿克隆，分发面同样从上下文取 Client——同一个 ArcSwap 状态，过载发布必然互通，不再依赖作者记得注释。

**练习 3**：`SchedulerLoadSender` 通道满了会发生什么？为什么不阻塞？
**答案**：`try_publish` 把快照并入 `SchedulerLoadShared::overflow`（同 worker 只留最新值）并 `notify_one` 唤醒接收端在 `recv` 的 select 分支里 drain（routing_load.rs L182-L221）。不阻塞是因为该 sender 在请求选点的热路径上调用——宁可暂时合并快照（负载信号本就是近似的）也不能让路由等待消费者。

**练习 4**：运行期通过 HTTP 把某个模型的 `active_decode_blocks_threshold` 从 0.8 改成 0.6，会更新几个对象？
**答案**：`Model::load_threshold_config(Some(cfg))` 遍历该模型所有 WorkerSet 的 `load_thresholds` 句柄逐个 `update`（model.rs L683-L700）；句柄背后是共享的 `RwLock<LoadThresholdConfig>`，监测任务下一次 `is_overloaded` 判定时读到新值。多个 WorkerSet 可能共享同一句柄（同一物化分支创建），update 是幂等的部分覆盖。

## 5. 综合实践

**任务**：复现「worker_set 从未就绪到就绪」的翻转，并用源码解释触发条件。这是 manifest 指定的本讲实践。

**环境**：沿用 u1-l2 的 sample 后端（CPU-only）。零依赖路径用 `DYN_DISCOVERY_BACKEND=file` 与 `DYN_EVENT_PLANE=zmq`（u2-l1 结论：本地无需 etcd/NATS；若你有现成 etcd，去掉这两个变量即可）。

**步骤**：

1. **只启动 frontend + prefill worker**（两个终端，或写进一个脚本）：

   ```bash
   # 终端 1：frontend
   export DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq
   python3 -m dynamo.frontend

   # 终端 2：只起 prefill 角色（取自 disagg.sh 的前半段）
   export DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq
   DYN_SYSTEM_PORT=8081 python3 -m dynamo.common.backend.sample_main \
     --model-name sample-model \
     --component sample-prefill \
     --disaggregation-mode prefill
   ```

   对照 [examples/backends/sample/launch/disagg.sh:L53-L74](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/disagg.sh#L53-L74)：正式脚本一次起齐 frontend + prefill + decode 三个进程，本实践故意把 decode 摘出来延迟启动。脚本头注释（[L7-L11](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/examples/backends/sample/launch/disagg.sh#L7-L11)）说明 prefill worker 会注册为 `WorkerType::Prefill`，且 frontend 的 PrefillRouter 负责交接 `disaggregated_params`。

2. **观察「未就绪」**。等 prefill 注册完成后：

   ```bash
   curl -s localhost:8000/v1/models/sample-model/ready | python3 -m json.tool
   ```

   该就绪子资源经 `/v1/models/{model_id}` 的 catch-all 路由提供（路由文档注册在 [lib/llm/src/http/service/openai.rs:L4155-L4162](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L4155-L4162)，通配符必须是终段所以不能单独成路由），数据由 `Model::namespace_readiness()` 提供。此时发一条 chat 请求应被拒（503，提示语见 [lib/llm/src/http/service/openai.rs:L3862-L3874](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/http/service/openai.rs#L3862-L3874) 的 `model_not_ready_message`——刻意不含 worker 类型等内部术语，细节留给 `/ready`）。

3. **延迟启动 decode worker**（第三个终端）：

   ```bash
   export DYN_DISCOVERY_BACKEND=file DYN_EVENT_PLANE=zmq
   DYN_SYSTEM_PORT=8082 python3 -m dynamo.common.backend.sample_main \
     --model-name sample-model \
     --component sample-decode \
     --disaggregation-mode decode
   ```

4. **再次查询就绪端点**，然后发一条流式 chat 请求验证可服务。

5. **杀掉 prefill 进程**，再查一次就绪端点，观察翻转回未就绪。

6. （可选，etcd 部署时）用 `etcdctl get --prefix v1/instances/` 观察实例 key 随进程生灭出现/消失（u3-l2 的 key 布局）；file 后端则观察 `~/.local/state/dynamo/`（按 u3-l5 结论，file 后端写文件注册、mtime 模拟租约，具体目录以本地运行为准）。

**需要观察的现象与预期结果**（对照 4.3.2 的推演表）：

| 时刻 | `/ready` 的关键字段 | frontend 日志 |
|------|--------------------|---------------|
| 仅 prefill | `ready=false`，`missing_worker_types=["decode"]`，`reason="missing worker types: decode"`，`worker_types.prefill.workers=1`、其 `needs=[["decode"]]` | "Adding worker set to model"、"Prefill worker detected, registering and activating prefill router" |
| prefill + decode | `ready=true`，`present=["decode","prefill"]`，missing 为空 | 第二条 "Adding worker set to model" |
| 杀 prefill 后 | `ready=false`，missing 回到 `["prefill"]`（decode 活着但它的 needs 未满足） | "Removed worker set from model"（model.rs L138-L148） |

**用源码解释触发条件**（这是本实践的交付物，写成一小段笔记）：

- prefill worker 注册时由 [lib/backend-common/src/worker.rs:L1871-L1880](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/backend-common/src/worker.rs#L1871-L1880) 声明 `needs=[[Decode]]`，decode 声明 `needs=[[Prefill]]`（L1881）。
- 阶段 1 只有 prefill：`present={prefill}`，prefill 的 DNF 无一组被覆盖，decode 进入 missing（[lib/llm/src/discovery/readiness.rs:L95-L138](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/readiness.rs#L95-L138)），`ready` 三条件合取失败。
- decode 上线 → Added 事件 → controller `apply_added` → 该 decode 落入独立的 `GroupKey`（`worker_set_key` 含 worker_type，[lib/llm/src/discovery/watcher.rs:L87-L103](https://github.com/ai-dynamo/dynamo/blob/407e8cb75bb98ca51551ce217c9603e446750e39/lib/llm/src/discovery/watcher.rs#L87-L103)）→ 物化并 `commit_discovery_group` 挂进同名 Model → 下一次 `/ready` 查询时 `present={prefill,decode}`，双方 needs 满足，`ready=true`。注意**没有任何代码「设置」ready 标志**——它每次查询时按当前 `worker_count` 活算。
- 杀 prefill → 发现面 Removed（file 后端 mtime 过期 / etcd 租约到期）→ `apply_removed` → 组空 → `remove_discovery_group` → Model 下剩 decode 一个集合，其 `needs=[[Prefill]]` 失败。decode WorkerSet 撤除时其 `RoutingLoadContext`（若建过）随 Arc 释放而 Drop 取消，无残留任务。

以上运行结果**待本地验证**（本环境未启动服务）；若 `/ready` 在仅有 prefill 时已返回 ready，优先检查 sample worker 的注册卡片是否真的带上了 `worker_type`（可用 frontend 的 debug 日志或 6 中 etcd 里的卡片内容核对）。

## 6. 本讲小结

- `WorkerType`（prefill/decode/encode/aggregated）是规范定义在 dynamo-kv-router 的角色枚举；worker 侧由 `disaggregation_mode` 推导出 `(worker_type, needs)` 写入 `ModelDeploymentCard`，`needs` 是 DNF：外层 OR、内层 AND；旧卡片的角色重建收敛在 `ModelDeploymentCard::resolve_worker_type`。
- `WorkerSet` = 一组同类 worker + 专属管线（引擎槽位、负载上下文、Prefill/Encoder 路由器）；`worker_count()` 实时读实例 watch channel，无 watcher 的进程内模型恒为 1。
- 就绪是**查询时活算的纯函数**：`ready ⇔ 有活 worker ∧ missing=∅ ∧ ambiguous=∅`；missing 同时涵盖「注册过但整池死亡」与「活单元 DNF 未覆盖的伙伴」；含无 `worker_type` 的旧卡片时退回「有活即 ready」的兼容路径。
- 引擎选择（`select_worker_set_with`）与就绪共享同一份快照与判定——报告的就绪就是路由会接受的现实；多候选按存活 worker 数加权随机。
- 上下线链路：发现面事件 → `ModelDiscoveryController` 的 `GroupKey` 分组与 `GroupStatus` 状态机 → 物化（`prepare_worker_set`，含负载上下文创建）→ `commit/replace/remove_discovery_group` → ArcSwap 一次发布目录；同组扩缩容走 `replace_group` 快路径，不重建管线。
- #13861 后负载状态按路由上下文自持：`prepare_worker_set` 为每个 WorkerSet 建 `RoutingLoadContext`（唯一共享 Client + `RouterLoadSource` + `SchedulerLoadSender` + 取消子树），`LoadThresholdHandle` 是可动态更新的共享阈值，`KvWorkerMonitor` 只在监测序列负载的来源上创建、由上下文持有——「用对 Client」从注释纪律变成结构保证。

## 7. 下一步学习建议

- **u7-l1（P/D 分离：概念与最小 disagg 拓扑）**：本讲的 `needs` 数学在真实分离部署里的完整形态，含 `disaggregated_params` 交接。
- **u7-l2（Prefill 路由与条件分离）**：本讲 `reconcile_discovery_topology` 设置的 PrefillRouter 目标如何在请求路径上被使用；其激活判断现在基于每个路由上下文自持的负载状态。
- **u6-l2（Rust 路由核心）**：`RoutingLoadContext` 喂养的选点与分发细节（RoutingHost、`dispatch_kv_admitted` 免重复过载检查）在那一讲展开；本讲只讲了它的出生地。
- 想加深就绪数学可通读 `lib/llm/src/discovery/readiness.rs` 与 `model.rs` 的测试区（L1296-L1995）——E-P-D、跨 namespace、缩容翻转、旧卡片兼容每个语义都有对应测试固定；想加深负载语义可通读 `routing_load.rs` 与 `worker_monitor.rs` 的测试区（含 `is_overloaded_*` 系列对三个阈值字段的逐项行为断言）。
