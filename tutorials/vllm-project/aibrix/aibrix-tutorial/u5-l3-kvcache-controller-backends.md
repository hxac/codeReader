# KVCache 控制器与分布式缓存后端编排

## 1. 本讲目标

本讲聚焦 AIBrix 控制平面中 **KVCache 控制器**——它把一个 `KVCache` 自定义资源翻译成一组真实运行的 K8s 对象（缓存服务、元数据存储、成员注册 watcher）。学完后你应当能够：

- 说清 `KVCache` CR 的 Spec 模型，特别是 `Metadata.Redis.ExternalConnection` 这条「外部托管元数据存储」的配置路径。
- 画出「主控制器按注解分发 → 后端 Reconciler 落地」的两层调用结构，并区分 `vineyard` 与 `distributed`（hpkv/infinistore）两类后端的产物差异。
- 解释元数据存储如何在「内置 Redis」与「外部托管 Valkey/Redis 端点」之间切换，以及切换时如何校验地址与密码 Secret、如何清理旧的内置 Redis 残留资源。
- 看懂 `validateExternalConnection` / `cleanupInClusterRedis` / `buildRedisWatcherEnvVars` 这三个关键函数的协作与时序。

本讲是 u5-l1（分布式推理与 KubeRay）、u5-l2（StormService 拓扑）之后，控制平面编排能力的延伸：前两讲编排的是「推理 Pod/进程拓扑」，本讲编排的是「跨节点的 KV 缓存与它的元数据存储」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**什么是分布式 KV Cache，为什么需要元数据存储。** 大模型推理时，每一层 Attention 都会生成 Key/Value 张量（简称 KV）。把 KV 缓存起来，遇到相同前缀的请求就能复用，省去重复计算。当推理分布到多个 Pod/节点时，KV 也需要跨节点共享——这就是 AIBrix 的分布式 KV Cache（数据平面实现在 `python/aibrix_kvcache/`，本讲不讲）。多个缓存节点要协同，就必须知道「集群里有哪些成员、每个键大致落在哪个节点」，这类「成员关系/路由表」信息体量小、读写频繁、要求低延迟，正适合用 Redis（或其协议兼容的开源实现 Valkey）作为**元数据存储（metadata store）**。所以一个 KVCache 部署通常由三类组件构成：

```
kvcache-server (缓存数据平面, 走 RDMA)   +   watcher (把成员注册进元数据存储)   +   元数据存储 (Redis/Valkey/etcd)
```

**控制平面只负责「摆棋局」。** KVCache 控制器像 u5-l1 的 RayClusterFleet 一样，不直接运行缓存逻辑，而是声明式地把上述三类组件以 Pod/Service/StatefulSet 的形式部署到集群里，并通过 OwnerReference 把它们挂在 `KVCache` CR 下，删 CR 即级联回收。控制平面与数据平面的边界：本讲的 Go 代码只到「把容器拉起来、把环境变量注入进去」为止；容器内部 `hpkv-server` / `infinistore` / `vineyardd` 如何工作，是数据平面的事。

**内置 vs 外部托管，是本讲的核心张力。** AIBrix 默认会在集群内部署一个单副本 Redis Pod 当元数据存储（简单、开箱即用）。但生产环境往往已有运维团队托管的 Valkey/Redis 集群（高可用、有备份、统一治理）。#2434 这个改动就是让用户可以声明 `ExternalConnection`，告诉控制器「元数据存储我自己管，你别再部署内置 Redis 了，顺便把我之前部署的内置 Redis 清理掉」。这条「外部托管」路径贯穿本讲后半部分。

**前置讲义承接。** 本讲默认你已经读过：u2-l3（CRD 数据模型、Spec/Status、kubebuilder 标记）、u2-l4（Admission Webhook 的 Defaulter/Validator）、u5-l1（控制器注册、OwnerReference 级联 GC、可选依赖的优雅降级）。这些概念下面直接使用，不再展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [api/orchestration/v1alpha1/kvcache_types.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/api/orchestration/v1alpha1/kvcache_types.go) | `KVCache` CR 的 Spec/Status 数据模型，含 `ExternalConnectionConfig` |
| [pkg/controller/kvcache/kvcache_controller.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/kvcache_controller.go) | 主控制器：取 CR、按注解选后端、委托执行 |
| [pkg/controller/kvcache/backends/reconciler.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/reconciler.go) | `BackendReconciler` / `KVCacheBackend` 两个抽象接口 + `BaseReconciler` 通用增删改 |
| [pkg/controller/kvcache/backends/distributed.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go) | `DistributedReconciler`：hpkv/infinistore 共用的 reconcile 主循环 + Redis 元数据（含外部连接、校验、清理） |
| [pkg/controller/kvcache/backends/common.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go) | 公共构造器：内置 Redis Pod/Service、watcher 的 SA/Role/RoleBinding、`buildRedisWatcherEnvVars` |
| [pkg/controller/kvcache/backends/hpkv.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go) | `HpKVBackend`：实现 `KVCacheBackend`，构造 hpkv StatefulSet/headless Service/watcher |
| [pkg/controller/kvcache/backends/infinistore.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go) | `InfiniStoreBackend`：与 hpkv 同构，默认值与命令不同 |
| [pkg/controller/kvcache/backends/vineyard.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/vineyard.go) | `VineyardReconciler`：独立的另一条 reconcile 路径，用 etcd 做元数据、产物是 Deployment |
| [pkg/webhook/kvcache_webhook.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/webhook/kvcache_webhook.go) | KVCache 的 Defaulter（补默认 backend 注解）与 Validator |
| [pkg/utils/kvcache.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/utils/kvcache.go) | `ValidateKVCacheBackend`：合法 backend 名单校验 |

辅助：[pkg/constants/kvcache.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/constants/kvcache.go) 集中定义标签、注解、后端名常量。

## 4. 核心概念与源码讲解

### 4.1 KVCache CR 模型与按注解分发的 reconcile 主循环

#### 4.1.1 概念说明

KVCache 控制器面对的核心问题是：**一个 CR 要能同时描述「好几种完全不同的缓存后端」**。hpkv 和 infinistore 是基于 RDMA 的分布式内存缓存，vineyard 则是另一种以 etcd 为元数据的对象引擎；它们的容器、端口、命令、元数据存储都不一样。

AIBrix 的解法是两步走：

1. **用注解（annotation）选择后端**——在 CR 上写 `kvcache.orchestration.aibrix.ai/backend: hpkv`，控制器读到后据此分发。
2. **用接口隔离差异**——主控制器只负责「取 CR → 读注解 → 查表委托」，具体「造哪些 Pod/Service」交给后端实现。

这样主循环非常薄，新增一个后端只要写一个新的实现并往表里注册一项，主控制器一行不用改。

#### 4.1.2 核心流程

主控制器的 `Reconcile` 流程（伪代码）：

```
Reconcile(req):
    kv = Get(KVCache, req)          # 取 CR，不存在则直接返回（级联 GC 自动清理）
    backend = 注解 kv.annotations[backend]   # 缺省 → "vineyard"
    handler = Backends[backend]      # 查表；查不到 → 报错 "unsupported backend"
    return handler.Reconcile(kv)     # 委托给具体后端
```

注意三个关键点：

- CR 不存在时**直接返回 nil**，不报错——因为控制器 Owns 了 Service/Deployment，KVCache 被删后这些子对象由 Kubernetes 的 OwnerReference 垃圾回收自动清理（与 u5-l1 的级联 GC 同源）。
- 「按注解分发」是字符串匹配，所以 backend 名必须落在白名单里。白名单校验发生在 **Webhook 的 ValidateCreate**（拦截非法值），控制器侧只做兜底。
- 后端表在 `newReconciler` 时一次性构造好（构造期就决定了支持哪些后端）。

#### 4.1.3 源码精读

先看数据模型。`KVCacheSpec` 把缓存数据平面（`Cache`）、成员注册（`Watcher`）、对外服务（`Service`）和元数据（`Metadata`）分开声明：

[api/orchestration/v1alpha1/kvcache_types.go:85-107](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/api/orchestration/v1alpha1/kvcache_types.go#L85-L107) — 定义 `KVCacheSpec`，`Metadata *MetadataSpec` 是元数据存储的入口，`Mode` 字段（centralized/distributed）是即将废弃的旧式配置。

元数据存储的嵌套结构是本讲的重点，`MetadataSpec → MetadataConfig → ExternalConnectionConfig` 三层：

[api/orchestration/v1alpha1/kvcache_types.go:35-54](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/api/orchestration/v1alpha1/kvcache_types.go#L35-L54) — `ExternalConnectionConfig` 只有 `Address`（host:port）和 `PasswordSecretRef` 两个字段；`MetadataConfig` 用 `ExternalConnection` 与 `Runtime` 二选一表达「外部托管 vs 内置部署」。

主控制器 `Reconcile`：

[pkg/controller/kvcache/kvcache_controller.go:160-181](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/kvcache_controller.go#L160-L181) — 取 CR → 读注解选 backend → 委托。注意第 174 行 `r.Backends[backend]` 查不到时返回 `"unsupported backend"` 错误，reconcile 会被重试。

后端表是怎么构造的：

[pkg/controller/kvcache/kvcache_controller.go:62-75](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/kvcache_controller.go#L62-L75) — `Backends` map 把三个常量分别映射到 `VineyardReconciler` 与两个 `DistributedReconciler`（hpkv/infinistore 共用同一个 Reconciler 类型，只是传入的 backend 字符串不同）。这里已经透露出后端分两大类：vineyard 独立一类，hpkv/infinistore 共用 distributed 这一类。

按注解选后端时如何兜底：

[pkg/controller/kvcache/kvcache_controller.go:184-193](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/kvcache_controller.go#L184-L193) — 注解为空时返回 `KVCacheBackendDefault`（即 vineyard）。注释解释了为什么需要这个兜底：当用 `--disable-webhook` 关闭准入 webhook 时，Defaulter 没机会注入默认值，控制器自己再兜一次底。

Webhook 侧的默认值注入与校验是「配置单一数据源」的另一面：

[pkg/webhook/kvcache_webhook.go:59-79](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/webhook/kvcache_webhook.go#L59-L79) — `Default()` 在 CR 入库前把 backend 注解补成默认值 vineyard。

[pkg/webhook/kvcache_webhook.go:97-108](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/webhook/kvcache_webhook.go#L97-L108) — `ValidateCreate` 调 `utils.ValidateKVCacheBackend`，拒绝非法 backend 名。

[pkg/utils/kvcache.go:27-55](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/utils/kvcache.go#L27-L55) — 这里有个历史包袱：合法 backend 只有 vineyard/hpkv/infinistore；旧注解 `mode`（distributed→infinistore，centralized→vineyard）仍被兼容，作为 backend 注解缺失时的回退依据。

最后，这个控制器本身受 Feature Gate 控制，且 Watches 带 `KVCacheLabelKeyIdentifier` 标签的 Pod（缓存/watcher Pod 都带这个标签，Pod 变化会反向触发对应 KVCache 的 reconcile）：

[pkg/controller/controller.go:89-91](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/controller.go#L89-L91) — 通过 `features.KVCacheController` 开关注册。

[pkg/controller/kvcache/kvcache_controller.go:100-124](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/kvcache_controller.go#L100-L124) — `For(KVCache)` + `Owns(Service/Deployment)` + `Watches(Pod)`，并用 `podWithLabelFilter` 谓词过滤掉不带标识标签的 Pod，避免无关 Pod 抖动引起全量 reconcile。

#### 4.1.4 代码实践

**实践目标**：亲手验证「注解 → 后端分发」这条链路，并理解 Webhook 关闭时的兜底行为。

**操作步骤**（源码阅读型，无需集群）：

1. 在 [pkg/constants/kvcache.go:39-42](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/constants/kvcache.go#L39-L42) 确认三个 backend 常量字符串。
2. 追踪：假设用户提交一个 `metadata.annotations` 为空、`spec` 也未填 backend 的 KVCache CR。
   - 若 webhook 开启：`Default()` 会把注解写成 `vineyard` → 控制器 `getKVCacheBackendFromAnnotations` 读到 `vineyard` → 走 `VineyardReconciler`。
   - 若 webhook 关闭（`--disable-webhook`）：注解保持为空 → 控制器 `getKVCacheBackendFromAnnotations` 返回默认 `vineyard` → 同样走 `VineyardReconciler`。
3. 再追踪：用户显式写 `annotations: {"kvcache.orchestration.aibrix.ai/backend": "hpkv"}`，但拼错成 `hpkvs`。
   - webhook 开启时会被 `ValidateKVCacheBackend` 拒绝（创建失败）。
   - webhook 关闭时校验不生效，控制器 `r.Backends["hpkvs"]` 查不到，返回 `unsupported backend` 并不断重试。

**需要观察的现象**：第 3 步的两种情形体现了「Webhook 是第一道防线、控制器是第二道兜底」的双层防护。

**预期结果**：你能用一张表说清「backend 注解取值 × webhook 开关」四种组合下，请求分别止步于哪里。

**待本地验证**：第 3 步 webhook 关闭时的重试行为，可在 envtest 中用一条用例确认 events 里出现 `unsupported backend`。

#### 4.1.5 小练习与答案

**练习 1**：为什么控制器在 `r.Backends[backend]` 查不到时不静默跳过，而是返回错误？

**参考答案**：因为「未知的 backend 名」几乎一定是用户配置错误（拼写或使用了未实现的后端）。静默跳过会让 CR 永远停在「无产物」状态且无任何信号；返回错误则触发 reconcile 重试并在事件/日志里暴露问题，便于排查。

**练习 2**：`getKVCacheBackendFromAnnotations` 的兜底默认值是 vineyard，而 `utils.getKVCacheBackendFromMetadata` 在 mode=distributed 时回退到 infinistore。这两处默认值为何不同？

**参考答案**：控制器侧只看 `backend` 注解，缺失即 vineyard（最保守、产物最简单）；utils 侧额外兼容即将废弃的 `mode` 注解，按语义把旧式 `distributed` 翻译成 `infinistore`。前者是「运行时分发」，后者是「创建期校验 + 历史兼容」，职责不同故默认值口径不同。

---

### 4.2 backends 抽象与多后端实现（distributed / hpkv / infinistore / vineyard）

#### 4.2.1 概念说明

后端这一层用了**两个正交的抽象**，初学者容易混淆，务必分清：

- `BackendReconciler` 接口：面向**主控制器**，只有一个方法 `Reconcile(ctx, kv)`。主控制器只认这个接口，不关心后端内部长什么样。
- `KVCacheBackend` 接口：面向 **distributed 类后端内部**，是一组「构造器」方法（`BuildCacheStatefulSet`、`BuildWatcherPod`、`BuildService`…）。hpkv 和 infinistore 的 reconcile 流程几乎一样，只有「构造出来的对象」不同，于是抽出这组接口让两者复用同一个 `DistributedReconciler`。

也就是说，**vineyard 走自己的 `VineyardReconciler`（直接实现 `BackendReconciler`），不使用 `KVCacheBackend` 接口**；而 hpkv/infinistore 共享 `DistributedReconciler`，通过实现 `KVCacheBackend` 接口注入差异。这是一个「模板方法 + 策略」的组合：`DistributedReconciler` 是模板，`KVCacheBackend` 实现是策略。

#### 4.2.2 核心流程

`DistributedReconciler.Reconcile` 的固定步骤（hpkv/infinistore 共用）：

```
Reconcile(kv):
    1. Backend.ValidateObject(kv)            # 校验必须配了 etcd 或 redis
    2. if kv.Spec.Metadata.Redis != nil:
           reconcileRedisService(kv)          # ★ 元数据存储（见 4.3/4.4）
    3. ReconcileStatefulsetObject(BuildCacheStatefulSet(kv))   # 缓存数据平面
    4. ReconcileServiceObject(BuildService(kv))                # headless Service
    5. if kv.Spec.Watcher != nil:
           SA → Role → RoleBinding → BuildWatcherPod          # 成员注册 watcher
```

`VineyardReconciler.Reconcile` 则是另一套（用 etcd、产物是 Deployment、无 watcher）：

```
Reconcile(kv):
    1. if kv.Spec.Metadata != nil: reconcileMetadataService(kv)   # 目前只支持 etcd
    2. ReconcileDeploymentObject(buildVineyardDeployment(kv))
    3. ReconcileServiceObject(buildVineyardRpcService(kv))
```

#### 4.2.3 源码精读

两个抽象接口的定义：

[pkg/controller/kvcache/backends/reconciler.go:35-37](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/reconciler.go#L35-L37) — `BackendReconciler`，主控制器唯一依赖的接口。

[pkg/controller/kvcache/backends/reconciler.go:210-221](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/reconciler.go#L210-L221) — `KVCacheBackend`，一组「构造器」方法，是 distributed 类后端的差异注入点。

`BaseReconciler` 提供与具体后端无关的「创建或更新」通用逻辑，所有后端都嵌入它。以 Pod 为例：

[pkg/controller/kvcache/backends/reconciler.go:43-72](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/reconciler.go#L43-L72) — 不存在则 Create；存在则只比较容器镜像，镜像变了才 Update。**注意：它只 reconcile 镜像，不 reconcile 环境变量等字段**——这一点在 4.3 讲外部连接时会成为一个重要限制。

`DistributedReconciler` 的 reconcile 主循环（hpkv/infinistore 共用）：

[pkg/controller/kvcache/backends/distributed.go:41-55](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L41-L55) — 构造时按 backend 字符串选择 `InfiniStoreBackend` 或 `HpKVBackend`，其它值直接 `panic`（构造期失败比运行期失败更安全）。

[pkg/controller/kvcache/backends/distributed.go:57-98](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L57-L98) — 上面伪代码对应的真实主循环：校验 → Redis 元数据 → StatefulSet → Service → watcher（SA/Role/RoleBinding/Pod）。

后端差异主要体现在「构造出来的 StatefulSet 容器命令与默认参数」上。hpkv 的缓存 StatefulSet：

[pkg/controller/kvcache/backends/hpkv.go:185-371](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L185-L371) — `buildCacheStatefulSet`：容器跑 `hpkv-server`，开 `HostIPC`、挂内存型 `/dev/shm`、加 `IPC_LOCK` capability（RDMA 所需），从 `eth1` 探测 RDMA IP；参数（RDMA 端口、block 大小/数量、一致性哈希槽位数）从注解读取并有默认值。

hpkv 的默认参数集中定义：

[pkg/controller/kvcache/backends/hpkv.go:44-51](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L44-L51) — RDMA 端口 18512、admin 9100、block 4096 字节、block 数 1048576、一致性哈希总槽 4096、虚拟节点 100。

infinistore 与 hpkv 同构，但默认值与命令不同：

[pkg/controller/kvcache/backends/infinistore.go:40-47](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L40-L47) — RDMA 端口 12345、admin 8088、链路类型默认 Ethernet、GID index 7。

[pkg/controller/kvcache/backends/infinistore.go:179-357](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L179-L357) — 容器跑 `infinistore`，额外要 `SYS_RESOURCE` capability；其预分配内存大小由容器内存限额推算。

infinistore 的内存预分配算法值得一看（独立公式）：

[pkg/controller/kvcache/backends/infinistore.go:410-430](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L410-L430) — 取内存限额（缺失则取 requests，再缺失默认 1 GiB），换算成 GiB 后乘 0.9 向下取整，不足 1 则取 1。即：

\[ \text{prealloc}(\text{GiB}) = \max\!\left(1,\; \left\lfloor \text{memGiB} \times 0.9 \right\rfloor\right) \]

vineyard 是完全独立的另一条路径（不走 `KVCacheBackend` 接口）：

[pkg/controller/kvcache/backends/vineyard.go:35-65](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/vineyard.go#L35-L65) — 自己实现 `Reconcile`，元数据走 etcd，缓存产物是 Deployment（不是 StatefulSet），容器跑 `vineyardd`，无 watcher 角色。

下表汇总四类后端的关键差异：

| 维度 | vineyard | hpkv | infinistore |
| --- | --- | --- | --- |
| Reconciler | `VineyardReconciler`（独立） | `DistributedReconciler`（共用） | `DistributedReconciler`（共用） |
| 缓存工作负载 | Deployment | StatefulSet | StatefulSet |
| 容器命令 | `vineyardd` (RPC 9600) | `hpkv-server` (RDMA 18512/admin 9100) | `infinistore` (RDMA 12345/admin 8088) |
| 元数据存储 | etcd（内置多副本） | Redis（内置单副本或外部） | Redis（内置单副本或外部） |
| 是否有 watcher | 否 | 是 | 是 |
| Service 类型 | ClusterIP | headless（ClusterIP=None） | headless（ClusterIP=None） |
| RDMA 相关 | 否 | 是（IPC_LOCK） | 是（IPC_LOCK + SYS_RESOURCE） |

#### 4.2.4 代码实践

**实践目标**：对比 hpkv 与 infinistore 两个 `KVCacheBackend` 实现，体会「同模板、不同策略」。

**操作步骤**（源码阅读型）：

1. 打开 [hpkv.go:64-104](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L64-L104) 与 [infinistore.go:60-99](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L60-L99)，逐方法对比 `BuildMetadataPod/Service`（两者都直接返回 `buildRedisPod/buildRedisService`）、`ValidateObject`（都要求 etcd 或 redis）。
2. 对比两者的 watcher Pod 构造：[hpkv.go:106-183](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L106-L183) 与 [infinistore.go:101-177](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L101-L177)。差异仅在 `--kvcache-backend` 参数值与默认端口。
3. **留意一处不一致**：hpkv 在 [hpkv.go:253](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L253) 用 `if len(...) != 0` 追加用户自定义 Env；而 infinistore 在 [infinistore.go:244](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/infinistore.go#L244) 写成 `if len(...) == 0`——条件恰好相反。这意味着 infinistore 在用户填了 `spec.cache.env` 时反而不会追加，留空时反而追加一个空切片。

**需要观察的现象**：第 3 步的两处条件是否确实相反；若相反，思考它对「给 infinistore 缓存容器注入自定义环境变量」的实际影响。

**预期结果**：你应当得出结论——当前为 infinistore 配置的 `spec.cache.env` 不会被注入缓存 StatefulSet（与 hpkv 行为不一致）。这是一个值得向社区确认的疑似缺陷（**待本地验证**：写一条用例断言 infinistore 缓存 Pod 含某自定义 Env，预期会失败）。

> 说明：以上是对两份真实源码字面条件的对比观察，结论以代码为准；是否为有意为之，可对照 PR 与 issue 进一步确认。

#### 4.2.5 小练习与答案

**练习 1**：如果新增第五种后端「mycache」，且它的 reconcile 流程与 hpkv 几乎一样（只是容器镜像和端口不同），最少要改哪些地方？

**参考答案**：① 在 `constants/kvcache.go` 加常量；② 写一个 `MyCacheBackend` 结构体实现 `KVCacheBackend` 接口；③ 在 `NewDistributedReconciler` 的分支里加 `else if backend == mycache`；④ 在主控制器 `newReconciler` 的 `Backends` map 注册一项 `NewDistributedReconciler(c, mycache)`；⑤ 在 `utils.isValidKVCacheBackend` 白名单加它。主控制器的 `Reconcile` 一行不用改——这正是抽象的价值。

**练习 2**：为什么 vineyard 不像 hpkv/infinistore 那样实现 `KVCacheBackend` 接口？

**参考答案**：vineyard 的产物（Deployment 而非 StatefulSet）、元数据（etcd 而非 Redis）、无 watcher 角色，都与 distributed 模板差异太大；强行套用 `KVCacheBackend` 接口会出现一堆「vineyard 用不到」的方法。因此它直接实现更上层的 `BackendReconciler` 接口，自成一条路径。抽象的边界应贴合真实共性，而非为了复用而复用。

---

### 4.3 元数据存储：内置 Redis vs 外部托管 Valkey/Redis

#### 4.3.1 概念说明

hpkv/infinistore 用 Redis 存成员关系元数据。这里有一条分叉：

- **内置 Redis**：AIBrix 在集群内部署一个名为 `{kvCache.Name}-redis` 的单副本 Pod + 同名 Service，跑 `redis-server`。简单，但单点、无运维治理。
- **外部托管**：用户已有 Valkey/Redis 端点（Valkey 是 Redis 的 BSD 开源分支，协议兼容），在 CR 里填 `metadata.redis.externalConnection.address`，控制器就不再部署内置 Redis，转而把外部地址告诉 watcher。

Valkey 与 Redis 对消费者（watcher）完全等价，因为 watcher 只通过 `REDIS_ADDR`/`REDIS_PASSWORD` 两个环境变量连接——这是 u1-l5 讲过的「协议兼容即可透明切换」在同一处的体现。

一个关键设计是**密码不进控制器内存**：控制器只校验「Secret 及其 key 是否存在」，真正的密码值通过 Kubernetes 的 `SecretKeyRef` 由 kubelet 注入到 watcher 容器，控制器代码全程不读取密码明文。这把「校验权限」和「使用权限」干净地分离了。

#### 4.3.2 核心流程

`reconcileRedisService` 的决策树：

```
reconcileRedisService(kv):
    redisConfig = kv.Spec.Metadata.Redis
    if redisConfig.ExternalConnection != nil 且 Address != "":
        ① validateExternalConnection(kv)     # 先校验（地址格式 + Secret 存在）
        ② 日志：使用外部连接，跳过内置 Redis
        ③ cleanupInClusterRedis(kv)          # 清理可能存在的旧内置 Redis Pod/Service
        return                                # 不部署任何内置 Redis
    else:
        if redisConfig.Runtime == nil:
            return error("requires either externalConnection or runtime")
        # 内置路径：部署 Redis Pod + Service（强制单副本）
        ReconcilePodObject(buildRedisPod)
        ReconcileServiceObject(buildRedisService)
```

注意顺序的严谨性：**先校验、后清理**。只有当外部配置被确认无误后，才去删除旧的内置 Redis——避免「外部配错了，结果内置的也被删了，两头空」。

#### 4.3.3 源码精读

决策树本体：

[pkg/controller/kvcache/backends/distributed.go:112-159](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L112-L159) — `reconcileRedisService`。第 117 行判断外部连接；第 138 行内置路径要求 `Runtime` 非空；第 143-145 行强制把副本数收敛为 1（多副本只是告警，不报错）。

内置 Redis 的 Pod 构造（外部路径下不会调用，但迁移前可能已存在，故清理时要按这个名字找）：

[pkg/controller/kvcache/backends/common.go:35-80](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L35-L80) — Pod 名 `{name}-redis`，端口 6379，命令 `redis-server`，带 `metadata` 角色标签，OwnerReference 指向 KVCache。

[pkg/controller/kvcache/backends/common.go:85-117](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L85-L117) — 同名 ClusterIP Service，selector 匹配 `metadata` 角色标签。

watcher 如何拿到（内置或外部）Redis 地址与密码——本讲的另一个关键函数：

[pkg/controller/kvcache/backends/common.go:212-248](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L212-L248) — `buildRedisWatcherEnvVars`。默认 `REDIS_ADDR=<name>-redis:6379`、`REDIS_PASSWORD=""`；一旦设了外部地址，`REDIS_ADDR` 改用外部地址，密码则经 `SecretKeyRef` 注入（kubelet 在 Pod 重启时重读 Secret 值）。

这个函数被 watcher Pod 构造调用，例如 hpkv：

[pkg/controller/kvcache/backends/hpkv.go:106-109](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/hpkv.go#L106-L109) — watcher 的 Env 以 `buildRedisWatcherEnvVars(kv)` 为起点，再追加 `REDIS_DATABASE=0`、watch 命名空间、集群名等。

`PasswordSecretRef` 的「名字/键」解析：

[pkg/controller/kvcache/backends/common.go:193-199](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L193-L199) — `parseSecretRef` 支持 `secretName/key` 格式，省略 `/` 时键默认为 `password`。

#### 4.3.4 代码实践

**实践目标**：从一份 CR 配置出发，手算 watcher 容器最终拿到的 `REDIS_ADDR` 与 `REDIS_PASSWORD` 两个环境变量。

**操作步骤**：

1. 设想一份 CR（命名空间 `default`，名字 `mycache`，backend `hpkv`），分三种情形填 `spec.metadata.redis`：
   - **情形 A（内置）**：`runtime: { replicas: 1, image: "redis:7" }`，无 `externalConnection`。
   - **情形 B（外部，无密码）**：`externalConnection: { address: "valkey.example.com:6379" }`。
   - **情形 C（外部，带密码）**：`externalConnection: { address: "valkey.prod:6380", passwordSecretRef: "prod-secret/auth-token" }`。
2. 对每种情形，套用 [common.go:212-248](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L212-L248) 的逻辑，写出 watcher 的两个 Env。

**需要观察的现象**：情形 C 的 `REDIS_PASSWORD` 不是字符串值，而是一个 `SecretKeyRef`（指向 `prod-secret` 的 `auth-token` 键）。

**预期结果**：

| 情形 | `REDIS_ADDR` | `REDIS_PASSWORD` |
| --- | --- | --- |
| A 内置 | `mycache-redis:6379` | `""`（字面空串） |
| B 外部无密码 | `valkey.example.com:6379` | `""`（字面空串） |
| C 外部带密码 | `valkey.prod:6380` | `SecretKeyRef{name=prod-secret, key=auth-token}` |

这正好对应 [distributed_test.go:428-531](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed_test.go#L428-L531) 中 `TestBuildRedisWatcherEnvVars` 的四条用例（含「无 `/` 时键默认 password」）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `REDIS_PASSWORD` 用 `SecretKeyRef` 而不是把密码值直接写进 Env 的 `Value`？

**参考答案**：`SecretKeyRef` 是引用而非快照——控制器代码不接触密码明文（安全），kubelet 在 Pod 启动/重启时从 Secret 读取（密码轮换后重启即生效），且 Secret 的访问受独立 RBAC 控制。直接写 `Value` 会把明文写进 etcd 里的 Pod 对象，泄露面更大。

**练习 2**：内置 Redis 被强制单副本（多副本只告警）。这条限制背后的工程权衡是什么？

**参考答案**：Redis 作为成员关系元数据存储，对一致性敏感；多副本 Redis 需要复杂的复制/故障切换（如 Sentinel/Cluster）才能保证一致，而 KVCache 控制器此刻只想提供「能用的元数据存储」。因此它收敛为单副本、把高可用的责任留给「外部托管」路径——用户若要高可用，就接自己运维的 Valkey/Redis 集群。这是「简单默认 + 可选增强」的典型取舍。

---

### 4.4 外部连接校验与残留内置 Redis 资源清理

#### 4.4.1 概念说明

「外部托管」听起来只是「不部署内置 Redis」，但真实场景更微妙：用户可能**先用了内置 Redis，之后才切换到外部端点**。这时集群里残留着旧的 `{name}-redis` Pod 和 Service，它们成了孤儿（仍在跑、仍占资源、却没人用）。控制器必须主动清理。

这就引出本节的核心设计命题：**清理是破坏性操作，必须在校验通过后才执行**。否则一旦外部配置写错（比如地址漏了端口、或密码 Secret 名拼错），控制器却已经把内置 Redis 删了，就会出现「内置没了、外部又连不上」的元数据真空，watcher 无法注册成员、整个缓存集群可能瘫痪。所以代码里的顺序是铁律：**validate → cleanup**，且 cleanup 失败要返回错误让 reconcile 重试，直到清理干净。

校验本身也有讲究：它**不读取 Secret 的值**，只确认 Secret 对象与指定 key 存在。这与「密码经 SecretKeyRef 注入」的设计一脉相承——校验只需要知道「引用能否解析」，不需要知道「值是什么」。

#### 4.4.2 核心流程

外部连接路径的完整时序：

```
reconcileRedisService(kv)  [外部连接分支]
    │
    ├─ validateExternalConnection(kv)
    │      ├─ net.SplitHostPort(address)        # 必须是 host:port
    │      └─ if passwordSecretRef != "":
    │             validateSecretExists(ns, ref)
    │               ├─ parseSecretRef → name, key
    │               ├─ Get(Secret, name)         # Secret 必须存在
    │               └─ secret.Data[key] 必须存在  # key 必须存在
    │
    ├─ （仅当上面全通过）cleanupInClusterRedis(kv)
    │      ├─ Delete(Pod "{name}-redis")         # 忽略 NotFound
    │      └─ Delete(Service "{name}-redis")     # 忽略 NotFound
    │
    └─ return nil  # 不部署任何内置 Redis
```

清理的「部分失败」是安全的：即便只删了 Pod、Service 没删成（返回错误），下一次 reconcile 会重新进入这里把剩下的删完。注释明确说「Partial cleanup ... is safe because the retry will finish the job」。

#### 4.4.3 源码精读

校验入口与地址格式检查：

[pkg/controller/kvcache/backends/distributed.go:163-179](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L163-L179) — `validateExternalConnection`：`net.SplitHostPort` 校验 `host:port` 格式；若填了 `PasswordSecretRef` 则进一步校验 Secret 存在。

Secret 存在性校验（只看存在、不读值）：

[pkg/controller/kvcache/backends/distributed.go:184-200](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L184-L200) — `validateSecretExists`：Get Secret，再检查 `secret.Data[key]` 是否存在。注释强调「without reading the secret value into memory」。

清理旧内置 Redis：

[pkg/controller/kvcache/backends/distributed.go:205-230](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L205-L230) — `cleanupInClusterRedis`：按 `{name}-redis` 名字删 Pod 和 Service，`NotFound` 被忽略（本就没部署过的情况）。

把上面三段拼回决策树，注意 `reconcileRedisService` 第 120-132 行的顺序——先 validate、后 cleanup，cleanup 失败用 `%w` 包装错误返回：

[pkg/controller/kvcache/backends/distributed.go:117-135](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L117-L135) — 校验失败立即 return（此时 cleanup 尚未执行，内置 Redis 安全保留）；只有校验通过才进入 cleanup。

「校验失败不清理」这一点有专门的测试守护：

[pkg/controller/kvcache/backends/distributed_test.go:535-592](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed_test.go#L535-L592) — `TestReconcileRedisService_ValidationBeforeCleanup`：预先创建内置 Redis Pod/Service，给一个非法外部地址（缺端口），断言 reconcile 报错且 Pod/Service **仍然存在**。这是对「先校验后清理」铁律的回归保护。

完整的外部连接端到端用例：

[pkg/controller/kvcache/backends/distributed_test.go:182-249](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed_test.go#L182-L249) — `TestReconcileRedisService_ExternalConnection`：预置 Secret、内置 Redis Pod/Service，校验通过后断言 Pod/Service 被删除。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：完整描述「KVCache 配置了 `Metadata.Redis.ExternalConnection` 时」从 reconcile 到 watcher 拿到环境变量的全链路，覆盖校验、清理、注入三段。

**操作步骤**：

1. 准备一份 CR 心智模型（namespace `default`，name `prod-cache`，backend `infinistore`）：
   ```yaml
   # 示例代码：仅用于说明，非仓库内现成 YAML
   apiVersion: orchestration.aibrix.ai/v1alpha1
   kind: KVCache
   metadata:
     name: prod-cache
     annotations:
       kvcache.orchestration.aibrix.ai/backend: infinistore
   spec:
     watcher: { image: "aibrix/kvcache-watcher:latest" }
     cache:   { image: "...", replicas: 2 }
     metadata:
       redis:
         externalConnection:
           address: "valkey.prod.example.com:6380"
           passwordSecretRef: "valkey-creds/auth-token"
   ```
2. 追踪 reconcile 进入 [distributed.go:57-98](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L57-L98)，`Metadata.Redis != nil` → `reconcileRedisService`。
3. 在 [distributed.go:117-135](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L117-L135) 命中外部分支：先 `validateExternalConnection`：
   - `net.SplitHostPort("valkey.prod.example.com:6380")` → 通过；
   - `validateSecretExists("default", "valkey-creds/auth-token")` → `parseSecretRef` 得 `name=valkey-creds, key=auth-token` → Get Secret 成功且含 `auth-token` 键 → 通过。
4. 校验通过 → `cleanupInClusterRedis` 删除 `prod-cache-redis` 的 Pod 与 Service（若迁移前存在）；返回 nil，**不部署任何内置 Redis**。
5. 回到主循环继续部署缓存 StatefulSet 与 watcher Pod。watcher 的 Env 由 [common.go:212-248](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L212-L248) 生成：`REDIS_ADDR=valkey.prod.example.com:6380`，`REDIS_PASSWORD=SecretKeyRef{valkey-creds/auth-token}`。

**需要观察的现象**：

- 步骤 3 若把地址改成 `valkey.prod.example.com`（缺端口），校验应在 `SplitHostPort` 失败、立即返回错误，步骤 4 的清理**不会执行**（对照 [distributed_test.go:535-592](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed_test.go#L535-L592)）。
- 步骤 4 删除用 `Delete` 且忽略 `NotFound`——首次切外部时本就没有内置 Redis，是安全 no-op（对照 [distributed_test.go:410-424](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed_test.go#L410-L424)）。
- 步骤 5 的一个**重要限制**：`ReconcilePodObject` 只 reconcile 镜像、不 reconcile Env（见 [reconciler.go:43-72](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/reconciler.go#L43-L72)）。[common.go:206-211](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L206-L211) 的注释明确指出：之后若修改外部地址或密码 Secret 的**名字**，运行中的 watcher 仍持有旧 Env，必须删除重建 watcher Pod 才能生效（Secret 的**值**轮换则由 kubelet 在重启时自动重读）。

**预期结果**：你能画出「外部地址校验 → 清理内置 Redis → 注入 Env 到 watcher」的时序图，并标注校验失败时的回退点与 Env 不热更新的边界条件。

**待本地验证**：在 envtest 中模拟「先内置后外部」的迁移——首次创建带 `runtime` 的 CR，等内置 Redis Pod 就绪；再 patch 成 `externalConnection`，断言内置 Pod/Service 被删除且 watcher Pod 的 `REDIS_ADDR` 指向外部地址（注意需重建 watcher 才能看到新 Env）。

#### 4.4.5 小练习与答案

**练习 1**：`cleanupInClusterRedis` 只删 Pod 和 Service，为何不删它们带的其他资源（如 PVC）？

**参考答案**：内置 Redis Pod 由 [common.go:35-80](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L35-L80) 构造时未挂 PVC（Redis 进程内存数据不持久化到卷），所以不存在关联 PVC。且这些资源都带指向 KVCache 的 OwnerReference，即便有遗漏也会在 KVCache 被删除时由 Kubernetes 垃圾回收兜底。控制器清理的目标只是「当前 reconcile 循环里会造成混淆的那两个具名对象」。

**练习 2**：如果用户把 `passwordSecretRef` 指向一个**存在但键名拼错**的 Secret（如键是 `pass` 而非 `password`），reconcile 会怎样？

**参考答案**：`validateSecretExists` 的 [distributed.go:195-197](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L195-L197) 会发现 `secret.Data[secretKey]` 不存在，返回 `key %q not found in secret %q` 错误。reconcile 因此重试，内置 Redis 不会被清理（校验未通过）。用户修正 Secret 键名后，下一次 reconcile 通过校验，才会进入清理与部署。这正是「校验守在清理之前」的价值。

## 5. 综合实践

**任务**：为一个 hpkv 后端的 KVCache 设计「内置 → 外部 Valkey」的迁移剧本，并指出每一步控制器会发生什么。

请按下列顺序写出你的分析与预期（源码阅读型，无需集群）：

1. **初始态（内置）**：CR 的 `metadata.redis.runtime` 配置了一个 `redis:7` 镜像、replicas=2。reconcile 后集群里会出现哪些对象？ replicas=2 会被如何处理？（提示：[distributed.go:142-159](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/distributed.go#L142-L159)）
2. **准备外部端点**：运维方部署了一个 Valkey 实例 `valkey.internal:6379`，并创建 Secret `vlkey/pass`（键 `pass`）。写出用户应如何填写 `metadata.redis.externalConnection`（注意 `passwordSecretRef` 的格式与默认键）。
3. **切换**：用户 patch CR 改用外部连接。请按 4.4.4 的五步追踪：校验如何通过、内置 Redis Pod/Service 如何被清理、watcher 的 `REDIS_ADDR`/`REDIS_PASSWORD` 变成什么。
4. **边界情形**：切换后，运维方又把 Valkey 地址改成 `valkey.internal:6380` 并 patch CR。watcher 会自动用新地址吗？为什么？要让新地址生效需要做什么？（提示：[common.go:206-211](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/controller/kvcache/backends/common.go#L206-L211)）
5. **失败演练**：若步骤 3 里 Secret 名误写成 `vlkey-typo/pass`，reconcile 的行为是什么？内置 Redis 会不会被误删？

**完成标准**：你能给出每一步对应的源码行号，并用一句话解释「为什么这个顺序是安全的」。把这份剧本整理成一页 runbook，就足以指导真实的元数据存储迁移。

## 6. 本讲小结

- KVCache 控制器是**两层结构**：主控制器按 `backend` 注解分发到 `BackendReconciler`，后端再用 `KVCacheBackend` 接口注入具体差异；新增后端几乎不碰主循环。
- 后端分两条路径：**vineyard** 独立一类（Deployment + etcd + 无 watcher），**distributed**（hpkv/infinistore）共用一类（StatefulSet + Redis + watcher，走 RDMA）。
- 元数据存储有**内置 Redis**（单副本 Pod+Service）与**外部托管 Valkey/Redis**（`ExternalConnection`）二选一；外部路径下控制器不部署任何内置 Redis。
- 外部连接路径遵循**先校验后清理**铁律：`validateExternalConnection`（地址 `host:port` + Secret 存在）通过后，`cleanupInClusterRedis` 才删除旧内置 Redis Pod/Service；校验失败则内置 Redis 安全保留。
- 密码全程**不进控制器内存**：校验只确认 Secret/key 存在，使用时由 kubelet 经 `SecretKeyRef` 注入 watcher。
- 一个重要限制：`ReconcilePodObject` 只 reconcile 镜像——改外部地址或密码 Secret **名字**后，需重建 watcher Pod 才生效（Secret **值**轮换由 kubelet 重启自动重读）。

## 7. 下一步学习建议

- **数据平面**：本讲只到「把容器拉起来」。缓存容器内部如何工作，请进入单元 10：先读 [u10-l1 分布式 KV Cache 架构与缓存管理器]（`python/aibrix_kvcache/` 的 L1/L2 两级缓存），再看 u10-l2 的跨引擎传输层与 u10-l3 的 CUDA 内核。
- **控制器横向对比**：把本讲的 KVCache 控制器与 u5-l1 的 RayClusterFleet、u5-l2 的 StormService 并列阅读，体会 AIBrix 控制平面「分层 CRD + OwnerReference 级联 GC + 可选依赖优雅降级」这套统一范式在不同场景下的复用。
- **Webhook 协作**：若对「Defaulter 补默认值、Validator 拦截非法值、控制器兜底」的三段式协作感兴趣，可回看 u2-l4，并对照本讲的 [kvcache_webhook.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/webhook/kvcache_webhook.go) 与 [utils/kvcache.go](https://github.com/vllm-project/aibrix/blob/af135690430bcf85bb5607c1b79c0fda30d5ff69/pkg/utils/kvcache.go) 加深理解。
- **动手扩展**：尝试按 4.2.5 练习 1 的清单，新增一个「mock」后端（只造一个最小 StatefulSet），跑通 envtest，验证主控制器零改动的可扩展性。
