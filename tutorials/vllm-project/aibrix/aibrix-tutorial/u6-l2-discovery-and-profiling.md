# Pod 发现、模型画像与输出预测

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 AIBrix 网关如何「发现」推理 Pod：`discovery` 子包用 `Provider` 接口把 K8s informer 与 standalone 静态文件统一成同一套 `WatchEvent`，最终落地为 `pkg/cache` 里的两张映射表（`metaPods` ↔ `metaModels`）。
- 描述每个被发现的 Pod 携带了哪些「状态」：模型归属、实时指标、在途请求数等，这些都封装在 `pkg/cache/pod.go` 的 `Pod` 包装结构与 `model.go` 的 `Model` 结构里。
- 理解 GPU 画像（`ModelGPUProfile`）的数据结构与查询方式：它如何用二维 `log2` 网格索引把「输入/输出 token 数」映射到吞吐与延迟，以及它是从 Redis 加载、按 deployment 缓存的。
- 掌握输出预测器（`SimpleOutputPredictor`）：它如何用「按输入 token 分桶的滑动窗口直方图 + 加权随机」预估一个请求的输出 token 数，以及它是如何被「请求完成」事件喂料、被路由/排队消费的。
- 准确说出预测结果被谁消费：经 `RoutingContext.Features()` / `TokenLength()` 被 **SLO 感知路由与排队**（`slo_queue`、`pending_load_provider`）消费；并纠正一个容易混淆的点——**VTC 路由用的是它自己的 `SimpleTokenEstimator`，并不直接调用这里的输出预测器**。

## 2. 前置知识

本讲默认你已经学完：

- **u1-l2 / u1-l5**：知道 `pkg/` 是 Go 逻辑目录，standalone 模式靠静态 `endpoints.yaml` 发现后端，K8s 模式靠动态发现。
- **u6-l1**：`pkg/cache` 是网关进程内的中央缓存（进程级单例），`discovery.Provider` 是其订阅机制抽象，`informers.go` 把事件写入 `metaPods`/`metaModels`。本讲正是把 u6-l1 里的「订阅机制」与「查询 API」拆开深入。
- **u7 系列（可后置）**：路由算法（`Router` 接口、slo、vtc）。本讲会在「输出预测器的消费方」一节引用它们，但只点到为止。

需要先建立的几个直觉概念：

| 术语 | 含义 |
| --- | --- |
| 服务发现（Service Discovery） | 运行时「谁能为我服务」的答案：在 K8s 里通常等于「哪些 Pod 在跑我请求的模型」。 |
| Informer | client-go 的「本地缓存 + 事件流」机制：先 List 全量，再持续 Watch 增量，回调 Add/Update/Delete。 |
| 画像（Profile） | 离线/在线测得的「模型 × GPU × 负载」性能表（吞吐、TTFT、TPOT、E2E 延迟等），用于在运行时查表估算成本。 |
| 输出预测（Output Prediction） | 在请求开始前，根据 prompt 长度**预估**它会生成多少 token，用于在路由阶段就估算「这个请求有多重」。 |

一句话定位：本讲讲的是 `pkg/cache` 的「感知」层——**先把后端 Pod 发现进缓存（4.1），再给每个模型挂上「性能画像（4.2）」和「输出预测器（4.3）」两件工具**，让后续的路由算法不仅知道「有哪些 Pod」，还能估算「这个请求在某个 Pod 上会消耗多少资源、能否满足 SLO」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/cache/discovery/discovery.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/discovery.go) | `Provider` 接口、`WatchEvent`、`EventType` 与 `EventHandler` 定义——服务发现抽象的总入口。 |
| [pkg/cache/discovery/kubernetes.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go) | `KubernetesProvider`：用 Pod + ModelAdapter 两个 informer 实现 `Provider`，初始同步后做一次「补发 ModelAdapter」的 reconcile。 |
| [pkg/cache/discovery/static.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go) | `StaticProvider`：读 `endpoints.yaml`，把 `host:port` 列表（含 P/D roleset）翻译成「合成的 `*v1.Pod`」。 |
| [pkg/cache/cache_init.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go) | `initDiscoveryProvider` 把 `Provider.Watch` 的回调接到 `handleDiscoveryObject`，完成「发现事件 → 缓存增删」的接线；并定义预测器的输入/输出上限常量。 |
| [pkg/cache/informers.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go) | `addPod`/`updatePod`/`deletePod` 等事件处理器：从 Pod 提取模型名、过滤 worker、维护 `metaPods`↔`metaModels` 双向映射，并在新建模型时挂上 `OutputPredictor`。 |
| [pkg/cache/pod.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pod.go) / [pkg/cache/model.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model.go) | `Pod`（`*v1.Pod` + 模型注册表 + 实时指标/计数）与 `Model`（Pod 集合 + `OutputPredictor` + `QueueRouter`）两个核心数据结构。 |
| [pkg/cache/model_gpu_profile.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go) | `ModelGPUProfile` / `ModelSLOs`：画像数据模型，`log2` 索引、签名查找与吞吐/延迟查表。 |
| [pkg/cache/cache_profile.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_profile.go) | `updateDeploymentProfiles`：周期性从 Redis 扫描 `aibrix:profile_*` 键，反序列化为画像并写缓存。 |
| [pkg/cache/cache_impl.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go) | 画像的 `UpdateModelProfile`/`GetModelProfileByPod`、预测器的 `GetOutputPredictor`，以及请求完成时给预测器「喂料」的 `DoneRequestTrace`。 |
| [pkg/cache/output_predictor.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go) | `SimpleOutputPredictor`：滑动窗口直方图 + 加权随机输出预测器，核心算法所在。 |
| [pkg/cache/pending_load_provider.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go) | `GetConsumption`：把「画像 + 预测特征」换算成 Pod 的归一化负载（Little's Law），是预测/画像的典型消费方。 |
| [pkg/types/output_predictor.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/output_predictor.go) | `OutputPredictor` / `OutputPredictorProvider` 接口，解耦 cache 实现与路由层。 |

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 Pod 发现与状态**、**4.2 GPU 画像**、**4.3 输出预测器**。三者共享同一个「按模型组织」的缓存：发现负责「填表」，画像与预测器是挂在每个 `Model` 上的两件「估算工具」。

### 4.1 Pod 发现与状态

#### 4.1.1 概念说明

网关要把一个请求转发出去，首先要回答：**「这个模型现在由哪些 Pod 提供服务？」** 这就是服务发现。AIBrix 原本只支持 K8s informer，但为了让网关也能在 bare-metal / Docker Compose / VM 上跑，作者把「发现」抽成了一个 `Provider` 接口（见 [discovery/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/README.md) 的设计动机）。

关键设计取舍是：**所有 Provider 最终都吐出 `*v1.Pod` 对象**。也就是说，路由算法与缓存只认 K8s 的 `Pod` 类型，完全不知道后端是 K8s 还是静态文件——静态文件里的 `host:port` 会被「伪装」成一个看起来正在 Running、已 Ready 的 Pod。这是用「适配器模式」换取路由层零改动。

被发现的 Pod 不只是「存在」这么简单。`pkg/cache/pod.go` 里的 `Pod` 是 `*v1.Pod` 的包装，额外挂了模型注册表与实时计数器；`model.go` 的 `Model` 则反向聚合了「跑这个模型的所有 Pod」。两者构成双向映射，是后续所有查询的基础。

#### 4.1.2 核心流程

发现数据进入缓存的主链路（自上而下）：

```text
┌─────────────────────────────┐
│ StaticProvider / K8sProvider │   ← 发现后端（YAML 文件 或 informer）
└──────────────┬──────────────┘
               │ WatchEvent{Add/Update/Delete, Object:*v1.Pod 或 *ModelAdapter}
               ▼
   initDiscoveryProvider 注册的回调  (cache_init.go)
               │
               ▼
      handleDiscoveryObject        按 *v1.Pod / *ModelAdapter 分流
               │
               ▼
   store.addPod / updatePod / deletePod   (informers.go)
               │
               ▼
   addPodLocked + addPodAndModelMappingLocked
               │
               ▼
   metaPods (PodKey → *Pod)   ⟷   metaModels (modelName → *Model)
```

- **静态模式**：`StaticProvider.Watch` 一次性把 YAML 里所有端点读出，全作为 `EventAdd` 推给回调，然后返回，不再有后续事件。
- **K8s 模式**：`KubernetesProvider.Watch` 把回调直接接到 informer 的 `AddFunc/UpdateFunc/DeleteFunc` 上，先 `Start` 再 `WaitForCacheSync`，同步完成后**补发一次所有 ModelAdapter** 以修正并发 list 带来的乱序，然后返回；此后 informer 在后台持续推增量事件。
- **健康/状态语义**：静态 Pod 直接被构造成 `Phase=Running`、`PodReady=True`（见 `addressToPod`）；K8s Pod 的就绪状态则随 informer 的 Update 事件自然进入缓存。`informers.go` 只对「带模型名（label 或 annotation）」且「不是 worker」的 Pod 建映射，其余忽略。

#### 4.1.3 源码精读

**① `Provider` 接口与事件类型** —— 发现抽象的总入口：

[pkg/cache/discovery/discovery.go:49-70](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/discovery.go#L49-L70) 定义了 `Provider` 接口，只有两个方法：`Watch(handler, stopCh)` 注册回调并开始监听，`Type()` 返回标识串。关键约定在注释里：`Watch` 应在「达到一致就绪状态」（初始同步完成/配置加载完成）后返回，从而保证网关开始接流量前缓存是热的。事件载荷 `WatchEvent` 见 [discovery.go:36-44](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/discovery.go#L36-L44)，含 `Type`（Add/Update/Delete）、当前 `Object` 与（仅 Update 的）`OldObject`。

**② `KubernetesProvider.Watch`** —— informer 直连回调：

[pkg/cache/discovery/kubernetes.go:52-126](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L52-L126) 是 K8s 发现的核心。其中 [kubernetes.go:76-94](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L76-L94) 的 `registerHandlers` 把 informer 的 `AddFunc/UpdateFunc/DeleteFunc` 直接翻译成 `handler(WatchEvent{...})`——**没有中间 channel、没有缓冲**，事件在 informer 自己的 goroutine 上直接回调。删除事件还会解包 `DeletedFinalStateUnknown` 墓碑对象（[kubernetes.go:87-89](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L87-L89)）。同步完成后 [kubernetes.go:112-123](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/kubernetes.go#L112-L123) 做了「补发所有 ModelAdapter」的 reconcile，注释解释了原因：Pod 与 ModelAdapter 两个 informer 并发 list，可能出现「适配器先到、Pod 还没到」导致映射丢失，补发后 `addModelAdapter` 的幂等性会补全映射。

**③ `StaticProvider`** —— 把 `host:port` 伪装成 Pod：

[pkg/cache/discovery/static.go:86-95](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go#L86-L95) 的 `Watch` 调 `load()` 读 YAML，把每个端点作为 `EventAdd` 推送后立即返回。配置模型 `StaticModelConfig` 见 [static.go:47-58](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go#L47-L58)，支持普通 `endpoints` 与 P/D 解耦的 `rolesets`（二者互斥）。真正的「伪装」在 [static.go:163-221](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static.go#L163-L221) 的 `addressToPod`：解析 `host:port`，给它装上 `model.aibrix.ai/name`、`model.aibrix.ai/port`、（可选）`model.aibrix.ai/engine` 与 roleset 相关标签，并把 `Phase` 设为 `Running`、`PodReady` 设为 `True`。这样下游根本看不出它是「假 Pod」。

**④ 接线：发现事件 → 缓存增删**：

[cache_init.go:425-432](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L425-L432) 的 `initDiscoveryProvider` 把一个把 `WatchEvent` 转交 `handleDiscoveryObject` 的闭包注册为回调。[cache_init.go:434-467](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L434-L467) 的 `handleDiscoveryObject` 用类型 switch 区分 `*v1.Pod` 与 `*ModelAdapter`，再按事件类型分派到 `store.addPod/updatePod/deletePod`。选用哪个 Provider 在 [cache_init.go:355-363](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L355-L363)：`opts.DiscoveryProvider` 为 `nil` 时默认用 `NewKubernetesProvider`（standalone 模式会注入 `StaticProvider`）。

**⑤ `addPod` 维护双向映射**：

[informers.go:104-108](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L104-L108) 的 `getModelNameFromPod` 先查 label 再查 annotation（兼容模型名含 `/` 等非法 label 字符的情况）。[informers.go:110-148](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L110-L148) 的 `addPod`：没有模型名且无 ModelClaim 绑定的 Pod 直接忽略（[L116-119](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L116-L119)）；worker Pod（Ray 的 worker）忽略（[L121-124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L121-L124)）；其余在加锁后 `addPodLocked` 建/取 `*Pod`，再 `addPodAndModelMappingLocked` 建映射。ModelClaim 绑定还会按「端口 > 0 才可路由」的规则选择性建映射（[L134-139](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L134-L139)，对应 u4-l3 讲过的「端口 0 门槛」）。

**⑥ `Pod` / `Model` 数据结构**：

[pkg/cache/pod.go:29-42](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pod.go#L29-L42) 的 `Pod` 在 `*v1.Pod` 之上加了：`Models`（该 Pod 跑哪些模型/适配器）、`Metrics` 与 `ModelMetrics`（按指标名 / 按「模型/指标」存的实时指标）、以及 `runningRequests`/`completedRequests`/`pendingLoadUtilization` 等实时计数器。[pkg/cache/model.go:27-41](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model.go#L27-L41) 的 `Model` 反向持有 `Pods` 集合、本讲的两位主角 `OutputPredictor` 与（排队用的）`QueueRouter`，外加 `pendingRequests` 计数。注意 `addPodAndModelMappingLocked` 在 [informers.go:358-362](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L358-L362) 新建 `Model` 时就顺手挂上了 `OutputPredictor`——这是 4.3 节的伏笔。

#### 4.1.4 代码实践

**实践目标**：亲手看清「静态 YAML → 合成 Pod → 双向映射」这条链路，理解为什么路由层对静态/K8s 后端无感。

**操作步骤**：

1. 打开 [pkg/cache/discovery/static_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/discovery/static_test.go)，找到构造一个最小 `StaticConfig`（含一个模型、两个 `host:port` 端点）的测试用例。
2. 在该测试里手动调用 `provider.Watch(handler, nil)`，让 `handler` 把每个 `WatchEvent` 的 `Type` 与 `Object.(*v1.Pod).Name`、`.Status.Phase`、`.Labels` 打印或断言出来。
3. 再写一段：把 handler 改成把收到的 Pod 喂给一个真实的 `Store`（参考 `cache_init.go` 的 `handleDiscoveryObject`），然后调用 `store.ListPodsByModel(modelName)`。

**需要观察的现象**：

- `Watch` 返回前，handler 被每个端点回调一次，`Type` 全是 `EventAdd`。
- 每个合成 Pod 的 `Phase` 恒为 `Running`、`PodReady` 恒为 `True`，`Labels` 里含 `model.aibrix.ai/name` 与 `model.aibrix.ai/port`。
- `ListPodsByModel` 返回的 Pod 数等于 YAML 里的端点数——证明静态 Pod 与 K8s Pod 在缓存里走的是同一套映射逻辑。

**预期结果**：你会直观看到「静态后端被伪装成 Ready 的 K8s Pod」，从而理解路由算法为何无需感知后端类型。

> 说明：本实践属于「源码阅读 + 测试改写型」。若不便于运行，可只读 `static_test.go` 的断言（它已覆盖端点→Pod 的字段映射）来推断结果；运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `KubernetesProvider.Watch` 在 `WaitForCacheSync` 之后还要「补发一次所有 ModelAdapter」？不补发会怎样？

> **答案**：Pod 与 ModelAdapter 两个 informer 并发执行初始 list，可能出现「ModelAdapter 的 AddFunc 先触发，而它关联的 Pod 还没进缓存」，导致 `addModelAdapter` 找不到 Pod、Pod↔模型映射丢失。补发后依赖 `addModelAdapter` 的幂等性重新建立映射。不补发会导致某些模型在缓存里「看不见 Pod」，路由时无候选。

**练习 2**：`StaticProvider` 把所有 Pod 都标成 `Ready=True`，这在生产环境意味着什么风险？

> **答案**：静态后端没有真实健康检查，缓存会「无条件信任」YAML 里的地址都是健康的。如果某个后端实际宕机，缓存仍认为它 Ready，路由算法仍可能把请求打过去。因此 standalone 模式更适合演示/测试，生产应走 K8s 模式让就绪状态由真实探针驱动。

---

### 4.2 GPU 画像

#### 4.2.1 概念说明

输出预测器只能告诉你「这个请求大概会生成多少 token」。但要回答「**这个请求在某个 Pod 上会跑多久、占多少资源、能否满足 SLO**」，就需要该「模型 × GPU」组合在不同负载下的性能数据——这就是**画像（Profile）**。

AIBrix 的画像是预先测好、存在 Redis 里的一张性能表，按 `(输出 token, 输入 token)` 二维网格索引，每个格子存该负载点下的吞吐（RPS）、TTFT、TPOT、E2E 延迟等。运行时，路由算法拿「预测出的输出 token + 实际输入 token」去这张表里查最近的格子，得到该请求的性能预期。

一个关键技巧：token 数跨度极大（几十到几十万），若线性分桶会导致小请求区域被严重欠采样。AIBrix 因此用 **`log2` 分桶**——相邻桶代表「翻倍」的 token 数，使每个数量级获得相近的分辨率。

#### 4.2.2 核心流程

画像从加载到消费的链路：

```text
Redis: aibrix:profile_{model}_{deployment}  ──(SCAN)──►  updateDeploymentProfiles
                                                              │ Unmarshal (Indexes 取 log2)
                                                              ▼
                                                  Store.UpdateModelProfile  (按 Created 时间版本择优)
                                                              │
                                                              ▼
                                             deploymentProfiles map  (key = profile_{model}_{deployment})
                                                              │
                                              路由请求时 GetModelProfileByPod
                                                              │ 由 Pod 推断 deployment 名
                                                              ▼
                                             ModelGPUProfile.GetSignature(输出, 输入)
                                                              │ 二分查找 log2 网格 → (i, j)
                                                              ▼
                                  ThroughputRPS(i,j) / LatencySeconds(i,j) / TTFT / TPOT
```

签名（Signature）查找的数学：把 token 数 \(t\) 映射到桶号 \(b\) 用 \(b = \mathrm{round}(\log_2 t)\)；若 \(t\) 落在两个已知网格点之间，则用容差 \(\tau = 0.5\) 决定偏向哪个：

\[
b_{\text{chosen}} = \begin{cases} \text{left} & \text{if } t < t_{\text{left}} + (t_{\text{right}} - t_{\text{left}})\cdot \tau \\ \text{right} & \text{otherwise} \end{cases} \]
其中 \(\tau = 0.5\) 表示中性偏好（见 `SignatureTolerance`）。

#### 4.2.3 源码精读

**① 画像数据模型**：

[pkg/cache/model_gpu_profile.go:59-69](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L59-L69) 的 `ModelGPUProfile`：`Indexes` 是 `[output tokens, input tokens]` 的二维网格刻度（注意第一维是 output）；`Tputs`/`E2E`/`TTFT`/`TPOT` 是与刻度同形状的性能值矩阵；`Cost` 是单位时间美元成本；`SLOs` 见 [model_gpu_profile.go:71-79](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L71-L79)。错误哨兵与缓存开关在 [model_gpu_profile.go:29-57](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L29-L57)。

**② 反序列化即取 `log2`**：

[pkg/cache/model_gpu_profile.go:85-97](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L85-L97) 的 `Unmarshal` 用 sonic 解 JSON 后，把 `Indexes` 的每个值原地替换为 \(\log_2\)。这意味着后续所有比较都在「对数域」进行，运行时输入也要先取 `log2` 才能对齐网格。

**③ 签名查找 `GetSignature`**：

[pkg/cache/model_gpu_profile.go:99-154](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L99-L154) 是画像查询的核心：先对每个特征取 `log2`（[L106-111](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L106-L111)），再对该维度的刻度做二分（[L129-143](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L129-L143)），未精确命中时按 `SignatureTolerance` 决定左右归属（[L144-150](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L144-L150)），越界则钳到首/尾桶。返回的 `[]int` 就是二维下标。

**④ 查表取值与访问器**：

[pkg/cache/model_gpu_profile.go:156-168](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L156-L168) 的 `getValue(ref, signature)` 做越界检查后返回 `ref[i][j]`；[model_gpu_profile.go:170-196](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L170-L196) 的 `ThroughputRPS`/`LatencySeconds`/`TTFTSeconds`/`TPOTSeconds` 是对它的薄封装，各自在对应矩阵为 `nil` 时返回专用错误（如 `ErrProfileNoThroughput`）。

**⑤ 画像加载（Redis → 缓存）**：

[pkg/cache/cache_profile.go:24-70](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_profile.go#L24-L70) 的 `updateDeploymentProfiles` 用 `SCAN` 模式扫描 `aibrix:profile_*` 键（[L36](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_profile.go#L36)），逐个 `Get` 后 `Unmarshal` 成 `ModelGPUProfile`，再 `UpdateModelProfile`。它在 ctx 取消时优雅退出、解析失败时跳过单条而非整体失败（[L58-61](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_profile.go#L58-L61)）。

**⑥ 缓存与查询**：

[cache_impl.go:301-308](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L301-L308) 的 `UpdateModelProfile` 用 `Created` 时间戳做版本择优——只有 `force` 或新画像的 `Created` 更晚才覆盖，避免旧数据刷掉新数据。[cache_impl.go:310-327](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L310-L327) 的 `GetModelProfileByPod` 先用 `utils.DeploymentNameFromPod(pod)` 推断 deployment 名（因为同模型可能在不同 GPU 部署上有不同画像），拼出键 `aibrix:profile_{model}_{deployment}`（模板见 [model_gpu_profile.go:81-83](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile.go#L81-L83)），命中则返回画像，未命中返回 `MissingProfileError`。

#### 4.2.4 代码实践

**实践目标**：理解 `log2` 网格如何把 token 数对齐到桶，并验证越界/夹中的行为。

**操作步骤**：

1. 打开 [pkg/cache/model_gpu_profile_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/model_gpu_profile_test.go)，找到构造一个 `ModelGPUProfile`（给定 `Indexes` 网格）的测试用例。
2. 在脑中/纸上构造一个一维网格 `Indexes[0] = [1, 8, 64, 512]`（取 `log2` 后即 `[0, 3, 6, 9]`），调用 `GetSignature(20)`、`GetSignature(1)`、`GetSignature(10000)`，断言返回的下标。
3. 把 `SignatureTolerance` 暂时理解成 `0.5`，解释 `GetSignature(4)`（\(\log_2 4 = 2\)，落在刻度 0 和 3 之间）会返回哪个下标。

**需要观察的现象**：

- `GetSignature(1)`（\(\log_2 1 = 0\)）命中首桶 → 下标 0。
- `GetSignature(10000)` 超过最大刻度 → 钳到末桶。
- `GetSignature(20)`（\(\log_2 \approx 4.32\)，落在 3 和 6 之间）按容差判定归属。

**预期结果**：你会确认「画像查询本质是对数域的二分 + 容差夹中」，理解为什么 token 数翻倍才移动一格。

> 说明：本实践以读测试、手算为主；`SignatureTolerance` 为常量不可在运行时改，故「改容差」属于思考题而非可执行步骤。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么画像的 `Indexes` 在反序列化时统一取 `log2`，而不在查询时临时取？

> **答案**：取一次 `log2` 后，所有后续比较（二分、越界判断）都在对数域进行，省去每次查询重复取对数；同时让刻度数组本身成为「对数均匀」的，便于二分。代价是：一旦反序列化完成，原始线性刻度就丢失了，调试时看到的 `Indexes` 不再是直观的 token 数。

**练习 2**：`UpdateModelProfile` 为什么要用 `Created` 时间戳比较，而不是直接覆盖？

> **答案**：画像可能由多个来源/时刻写入 Redis（如离线 benchmark 重新跑了一次）。按 `Created` 择优可保证缓存里始终是「最新一次测量」的画像，避免扫描顺序导致旧数据覆盖新数据。`force=true` 用于显式强制刷新的场景。

---

### 4.3 输出预测器

#### 4.3.1 概念说明

LLM 的输出长度高度不确定——同一个 prompt 可能生成 10 个 token 也可能生成 2000 个。但很多路由决策（这个请求有多重、会不会拖垮某个 Pod、能不能满足 SLO）必须在**请求开始前**就做。于是需要一个**输出预测器**：给定 prompt 长度，预估输出 token 数。

AIBrix 的 `SimpleOutputPredictor` 用一个朴素但有效的思路：**统计历史上「输入 token 桶 → 输出 token 桶」的出现频次，按频次做加权随机采样**。直觉是：如果过去「输入约 2^6 个 token」的请求大多输出了 2^8 个 token，那新来一个 2^6 输入的请求，就大概率预测为 2^8。

它有两个关键性质：

- **在线学习**：每个请求完成时，把真实的 (输入, 输出) 喂回预测器，直方图不断刷新。
- **滑动窗口**：只保留最近一段时间（默认 240s）的历史，使预测跟随流量分布的漂移。

它通过 `pkg/types/output_predictor.go` 的接口暴露给路由层，从而与具体实现解耦。

#### 4.3.2 核心流程

预测器的「写」（喂料）与「读」（预测）两条路径：

```text
请求完成 (cache_impl.go DoneRequestTrace, 拿到真实 inputTokens/outputTokens)
        │
        ▼
  OutputPredictor.AddTrace(inputTokens, outputTokens, 1)
        │ 1. tryRotate：必要时启动一次窗口轮转（过期旧桶）
        │ 2. token2bucket：input→inputBucket, output→outputBucket (都是 round(log2))
        │ 3. 原子累加：summary(inputs, inputsSums) + 当前历史槽
        ▼
  [汇总直方图 inputs：inputBucket × outputBucket 的频次；inputsSums：每个 inputBucket 的总频次]


路由/排队请求预测 (RoutingContext.TokenLength / Features)
        │
        ▼
  OutputPredictor.Predict(promptLen)
        │ 1. token2bucket(promptLen) → inputBucket
        │ 2. 该桶总频次 inputsSums[inputBucket]；若为 0 → coldPredict（冷启动）
        │ 3. 否则在 [0, sum) 随机取 cursor，沿 output 桶累加，命中即返回 2^outputBucket
        ▼
  预测的输出 token 数
```

分桶的数学：桶数由 token 上限决定，

\[
\text{inputBuckets} = \lceil \log_2(\text{maxInputTokens}+1) \rceil,\quad
\text{outputBuckets} = \lceil \log_2(\text{maxOutputTokens}+1) \rceil
\]

默认上限都是 \(2^{20}\)（约 1M），故桶数约 20。

#### 4.3.3 源码精读

**① 接口抽象**：

[pkg/types/output_predictor.go:18-30](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/output_predictor.go#L18-L30) 定义 `OutputPredictor`（`AddTrace` + `Predict`）与 `OutputPredictorProvider`（`GetOutputPredictor(modelName)`）。cache 的 `Cache` 接口通过组合 `types.OutputPredictorProvider` 把它暴露出去（见 [cache_api.go:33](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_api.go#L33)）。

**② 构造与分桶**：

[pkg/cache/output_predictor.go:146-169](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L146-L169) 的 `NewSimpleOutputPredictor`：输入/输出桶数按上式计算（[L152-153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L152-L153)）；`history.window` 是一个环形缓冲，槽数为 `window/MovingInterval + extraSlot`（`MovingInterval=10s`，[output_predictor.go:30-34](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L30-L34)）；多分配 1~2 个「余量槽」是为了让轮转期间的汇总更新可以无锁进行（注释 L147-148）。该构造在 [informers.go:358-362](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L358-L362) 新建 `Model` 时被调用，常量见 [cache_init.go:66-68](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_init.go#L66-L68)（`maxInputTokens=maxOutputTokens=1M`，`movingWindow=240s`）。

**③ 写入 `AddTraceWithTimestamp`**：

[pkg/cache/output_predictor.go:171-190](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L171-L190)：先 `tryRotate`（必要时后台轮转），用 `token2bucket` 把输入/输出各自映射到桶（[L174-175](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L174-L175)），再在读锁保护下原子累加三处：汇总直方图 `inputs[idx]`、输入桶总数 `inputsSums[inputBucket]`、当前历史槽（[L187-189](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L187-L189)）。注释 L186 说明「先写汇总、再写历史」是为了避免轮转时汇总出现负数。

**④ 预测 `Predict`**：

[pkg/cache/output_predictor.go:196-213](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L196-L213) 是加权随机采样的核心：取输入桶总频次 `randRange`（[L198](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L198)）；为 0 则走 `coldPredict` 冷启动（[L199-200](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L199-L200)）；否则在 `[0, randRange)` 取一个随机 `cursor`，沿该输入桶对应的输出桶区间累加频次，命中（`cursor < accumulation`）时返回 \(2^{\text{outputBucket}}\)（[L203-211](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L203-L211)）。换言之，频次越高的输出桶，被采中的区间越大——这就是「加权随机」。`token2bucket` 见 [output_predictor.go:233-242](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L233-L242)（`round(log2(tokens))`，并钳到桶数上限）。

**⑤ 冷启动策略**：

[pkg/cache/output_predictor.go:215-227](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L215-L227) 的 `coldPredict` 在无历史时按 `DefaultColdPrediction`（默认 `OptimisticColdPrediction`，返回 1）处理，另有随机/等于输入/悲观（取 `MaxOutputLen=4096`）等策略（枚举见 [output_predictor.go:36-45](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L36-L45)）。选「乐观=1」是因为多数画像在「输出最小」时给出最优性能预期。

**⑥ 窗口轮转 `rotatingHistory`**：

[pkg/cache/output_predictor.go:98-144](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L98-L144) 的环形缓冲：`forwardLocked` 推进 head 并记录「跳过的空槽数」（[L120-138](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L120-L138)），`resetTail` 在淘汰最旧槽时从汇总里扣回该槽的频次（[L140-144](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L140-L144)）。轮转在 `tryRotate` 里用 `go p.rotate(ts)` 异步触发（[L244-253](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor.go#L244-L253)），与写入用 `RWMutex` 配合——写持读锁、轮转持写锁。

**⑦ 喂料入口（请求完成）**：

[cache_impl.go:286-289](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L286-L289) 的 `DoneRequestTrace` 在请求结束时，若模型存在且有预测器，就调用 `meta.OutputPredictor.AddTrace(int(inputTokens), int(outputTokens), 1)`。这就是预测器「在线学习」的来源——真实的输入/输出 token 数（来自响应的 usage 统计）。

**⑧ 消费方 1：路由上下文**：

`Predict` 并不直接被路由算法调用，而是经 `RoutingContext` 间接消费。[pkg/types/router_context.go:191-203](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context.go#L191-L203) 的 `TokenLength()` 返回 `predictor.Predict(promptLen)`；[router_context.go:205-219](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context.go#L205-L219) 的 `Features()` 把它组合成 `{outputLen, promptLen}`——这正是画像签名查找所需的输入。预测器由 [router_context.go:152-156](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context.go#L152-L156) 的 `SetOutputPreditor` 注入。

**⑨ 消费方 2：SLO 排队**：

[pkg/plugins/gateway/queue/slo_queue.go:111-135](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/queue/slo_queue.go#L111-L135) 的 `Enqueue` 先 `GetOutputPredictor(model)` 并 `ctx.SetOutputPreditor(predictor)`（[L113-117](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/queue/slo_queue.go#L113-L117)），再用 `ctx.Features()` 把请求按特征分到不同子队列（[L120-125](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/queue/slo_queue.go#L120-L125)）。`slo.go` 路由器还通过 `cache.NewPendingLoadProvider()` 间接用到了画像（见下）。

**⑩ 消费方 3：pending load（画像 + 预测的合流）**：

[pkg/cache/pending_load_provider.go:61-86](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L61-L86) 的 `GetConsumption` 是预测与画像交汇的典型场景：先按 Pod 取画像（[L62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L62)），再 `ctx.Features()` 拿到（预测的）输出/输入 token（[L67](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L67)），查签名得吞吐 \(\lambda\) 与平均延迟（[L72-78](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L72-L78)），按 Little's Law 返回归一化负载 \(1/(\lambda \cdot \text{latency})\)（[L84](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L84)）。这里**没有预测就没有特征，没有画像就没有性能表**——二者缺一不可。

> ⚠️ **关于 VTC 的澄清**：你可能以为 token 感知的 VTC 路由也会用这个预测器。实际上 [pkg/plugins/gateway/algorithms/vtc/token_estimator.go:23-47](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/algorithms/vtc/token_estimator.go#L23-L47) 的 VTC 用的是它**自己**的 `SimpleTokenEstimator`（按「4 字符 ≈ 1 token、输出≈1.5×输入」粗估），并不调用 `cache.OutputPredictor`。本讲的预测器主要服务于 **SLO 感知**链路（`slo_queue` 与 `pending_load_provider`/`slo` 路由），不要把两者混为一谈。

#### 4.3.4 代码实践

**实践目标**：追踪 `output_predictor` 如何预估输出 token 数，并说清该预测被谁消费。

**操作步骤**：

1. 打开 [pkg/cache/output_predictor_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/output_predictor_test.go)，找到「喂若干 `AddTrace(inputTokens, outputTokens, cnt)` 后再 `Predict(inputTokens)`」的用例。
2. 在测试里手动构造一个分布：对同一个输入桶，连续喂「输出桶 A」5 次、「输出桶 B」1 次，然后调用 `Predict(同输入桶)` 100 次，统计返回值落在 A、B 的比例。
3. 在 [pkg/types/router_context_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context_test.go) 中确认 `SetOutputPreditor` → `Features()` 的调用链（[router_context_test.go:63](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context_test.go#L63)）。
4. 在 `slo_queue.go` 的 `Enqueue` 处与 `pending_load_provider.go` 的 `GetConsumption` 处各加一行 `klog`（仅阅读定位，不修改源码逻辑），说清预测结果在两处的角色。

**需要观察的现象**：

- 步骤 2 中，`Predict` 的返回值大致按 5:1 的比例落在桶 A 与桶 B（加权随机），而非固定值。
- 当某个输入桶从未被喂过，`Predict` 返回冷启动值（默认 1）。
- `Features()` 返回 `{outputLen, promptLen}`，其 `outputLen` 正是 `Predict` 的产物。

**预测结果的消费方（回答实践任务的核心问题）**：

| 消费方 | 调用点 | 用途 |
| --- | --- | --- |
| `RoutingContext.Features()` / `TokenLength()` | [router_context.go:205-219](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/types/router_context.go#L205-L219) | 把预测的输出长度封装为「请求特征」，供下游统一读取 |
| SLO 排队 `slo_queue` | [slo_queue.go:113-125](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/plugins/gateway/queue/slo_queue.go#L113-L125) | 用特征把请求分桶到不同子队列，做 SLO 感知调度 |
| pending load / `slo` 路由 | [pending_load_provider.go:67-84](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/pending_load_provider.go#L67-L84) | 特征 → 画像签名 → 吞吐/延迟 → 归一化负载（Little's Law） |

**预期结果**：你会得出结论——预测结果经 `Features()` 统一出口，主要被 **SLO 感知的排队与路由**消费；VTC 走的是自己的字符级估算器，不在此列。

> 说明：步骤 2 的「100 次采样比例」依赖随机数，是大数定律下的近似；具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：预测器把 token 数映射到桶用的是 `round(log2(tokens))`。若某请求输入 50 个 token，`maxInputTokens=1M`，它会落到哪个输入桶？

> **答案**：\(\log_2 50 \approx 5.64\)，`round` 后为 6，故落到输入桶 6（桶号从 0 起，代表约 \(2^6=64\) 个 token 这一数量级）。`maxInputTokens=1M` 对应约 20 个桶，6 在范围内，无需钳位。

**练习 2**：为什么 `AddTrace` 在写汇总与写历史槽时要「先写汇总、再写历史」？

> **答案**：轮转（`resetTail`）会从汇总里扣回即将淘汰的旧槽频次。如果先写历史槽、再写汇总，存在一个窗口：旧槽已被计入淘汰但汇总尚未加上新值，可能出现「汇总为负」的瞬时状态。先写汇总可保证汇总始终 ≥ 任何已计入的历史，避免负数。

**练习 3**：冷启动时默认预测输出为 1（乐观策略）。这个选择对 SLO 路由有什么影响？

> **答案**：乐观预测会让画像签名落在「输出最小」的点，而注释指出多数画像在该点给出最优（最低延迟/最高吞吐）性能预期。于是冷启动请求被估算成「很轻」，更容易被放到任何 Pod 上、更不容易触发保守的 SLO 拒绝。代价是冷启动期可能低估真实负载，导致短暂超载；随着 `AddTrace` 积累，预测会自我修正。

---

## 5. 综合实践

把三个模块串起来：**一次请求从「找到 Pod」到「被估算成本」的完整感知链路**。

设想一个 SLO 路由的请求，请按下列顺序跟踪并画出数据流，标注每一步涉及的文件与方法：

1. **发现**：该模型对应的 Pod 是如何进入 `metaModels` 的？分别描述 K8s 模式（`KubernetesProvider.Watch` → `addPod` → `addPodAndModelMappingLocked`）与 standalone 模式（`StaticProvider` 合成 Pod）两条路径，指出它们在哪一行汇合到同一个 `addPod`。
2. **挂载工具**：模型第一次被创建时（[informers.go:358-362](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/informers.go#L358-L362)），`OutputPredictor` 是如何被挂上的？此时它的直方图是空的，预测会走哪条分支？
3. **画像就绪**：画像从 Redis 加载（`updateDeploymentProfiles`）到 `deploymentProfiles` 后，`GetModelProfileByPod` 如何凭 Pod 的 deployment 名找到它？若该模型从未有画像写入 Redis，`GetModelProfileByPod` 返回什么错误？这个错误在 `pending_load_provider.GetConsumption` 里被如何处理？
4. **预测 + 画像 = 负载**：在 `pending_load_provider.GetConsumption` 中，预测器提供的「输出 token」与画像提供的「吞吐/延迟」如何合成归一化负载？请写出 Little's Law 的公式与每个变量的来源。
5. **闭环学习**：请求结束后，真实的输入/输出 token 如何经 `DoneRequestTrace` 回喂预测器（[cache_impl.go:286-289](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/cache/cache_impl.go#L286-L289)），使下一次预测更准？

**交付物**：一张标注了文件名、方法名、行号与数据流向的图，以及一段说明「为什么没有发现就没有路由、没有画像与预测就没有 SLO 感知」。

> 说明：本实践为「源码阅读 + 调用链跟踪型」，无需运行集群；若要实跑，可在 `slo_queue` 与 `pending_load_provider` 关键点加 `klog` 观察日志（仅阅读定位，不改动逻辑）。具体运行现象待本地验证。

## 6. 本讲小结

- **Pod 发现**：`discovery.Provider` 接口把 K8s informer 与静态 YAML 统一成 `WatchEvent`，经 `initDiscoveryProvider`/`handleDiscoveryObject` 接到 `informers.go` 的 `addPod` 等，落地为 `metaPods` ↔ `metaModels` 双向映射；静态后端靠 `addressToPod` 把 `host:port` 伪装成 Ready 的 `*v1.Pod`，路由层对后端类型无感。
- **`Pod`/`Model` 结构**：被发现的 Pod 携带模型归属、实时指标与计数器（`pod.go`）；`Model` 反向聚合 Pod 集合并挂载 `OutputPredictor` 与 `QueueRouter`（`model.go`）。
- **GPU 画像**：`ModelGPUProfile` 是按 `(output, input)` token 的 `log2` 网格索引的性能表，从 Redis 扫描加载、按 `Created` 版本择优缓存、按 Pod 的 deployment 名查询；`GetSignature` 用对数域二分 + 容差夹中定位网格点。
- **输出预测器**：`SimpleOutputPredictor` 用「按输入 token 分桶的滑动窗口直方图 + 加权随机」预估输出长度，请求完成时由 `DoneRequestTrace` 在线喂料，默认 240s 窗口、`log2` 分桶、乐观冷启动。
- **消费关系**：预测结果经 `RoutingContext.Features()`/`TokenLength()` 统一出口，被 **SLO 感知排队（`slo_queue`）与 pending-load/SLO 路由（`pending_load_provider`）** 消费——后者正是「预测（特征）+ 画像（性能表）」合流计算归一化负载之处。
- **一个易错点**：VTC 路由用的是自己的 `SimpleTokenEstimator`（字符级估算），**不**直接消费本讲的 `OutputPredictor`；不要把两者混淆。

## 7. 下一步学习建议

- **进入网关核心（u7-1）**：本讲的「发现 + 预测 + 画像」是网关做路由决策的「输入侧」。下一步应学习 `pkg/plugins/gateway` 如何在 Envoy ExtProc 的请求阶段调用这些数据，完成一次真实的路由。
- **深入路由算法（u7-3 / u7-4 / u8-3）**：重点读 `slo.go`（消费 `pending_load_provider` 与画像）、`slo_queue.go`（消费预测器分桶），以及 `vtc`（对比它自己的 token 估算器与本讲的预测器，理解两种 token 估计思路的取舍）。
- **回到控制平面（u4-l3 / u9-1）**：本讲提到的「ModelClaim 端口 0 门槛」与运行时边车的激活协议，可在 ModelClaim 控制器与 Python runtime 讲义中找到上游来源，理解「Pod 何时才被认为可路由」的全链路。
- **可观测性（u11-3）**：若想观察预测器/画像在线上的实际表现，可结合 `pkg/cache/cache_trace.go` 与 Grafana 面板，追踪 `DoneRequestTrace` 记录的输入/输出 token 分布与预测命中情况。
