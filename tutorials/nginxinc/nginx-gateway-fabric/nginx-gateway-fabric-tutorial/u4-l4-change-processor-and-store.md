# ChangeProcessor 与状态存储

> 本讲属于第四单元「事件管线：从控制器到处理器」，承接 u4-l3（EventHandler 总编排）。在上一讲里，`eventHandlerImpl` 把一个事件批次交给 `processor`：先逐条 `Capture*` 登记变更，再一次性 `Process` 得到 `graph.Graph`，然后才进入「生成 NGINX 配置 → 下发」的阶段。本讲就下钻这个 `processor`——它是事件管线与图构建之间的「中转站」，回答三个问题：变更怎么被捕获、捕获后的对象存在哪里、以及如何判断「这次变更到底有没有意义」。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ChangeProcessor` 接口的五个方法各干什么，特别是 `Process` 在「无变更」时返回什么、为什么这样设计。
- 画出「一个 client.Object 进来 → 按 GVK 找到对应 store → 写入/删除 → 决定是否翻转 changed 标志」的完整路径。
- 区分三类资源在 changed predicate 上的不同待遇（`nil` predicate / `funcPredicate` / `annotationChangedPredicate`），并能解释这种分类如何避免无意义的图重建与 NGINX reload。
- 看懂 `forceRebuild` 这条「不改集群状态、只把 changed 置真」的特殊通道（WAF bundle 场景）。

## 2. 前置知识

本讲默认你已经掌握以下概念（前序讲义已建立）：

- **事件批次与双缓冲**（u4-l2）：控制器把资源变更翻译成 `UpsertEvent`/`DeleteEvent`，由 `EventLoop` 攒批后整批派发。同一批里可能含多次同对象变更，下游必须**幂等**。
- **EventHandler 编排**（u4-l3）：`eventHandlerImpl` 对每条事件调 `Capture*`，再调一次 `Process`，得到 graph 后才下发。
- **Generation / 幂等**：Kubernetes 资源的 `.metadata.generation` 在 spec 变化时自增；只有真正影响 spec 的变更才值得重建配置。
- **cluster state**：控制面在内存里维护的一份「当前集群里我们关心的资源」快照，区别于 controller-runtime 的 informer 缓存。

如果你还不熟悉「为什么一次事件批次只该触发一次 reload」，建议先回看 u4-l2 的双缓冲机制，本讲的 `changed` 标志正是那条链路上的「最后一道闸门」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [internal/controller/state/change_processor.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go) | 定义 `ChangeProcessor` 接口与 `ChangeProcessorImpl` 实现：捕获变更、维护 `clusterState`、在 `Process` 时调用 `graph.BuildGraph`。本讲的「主文件」。 |
| [internal/controller/state/store.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go) | 定义 `Updater` 接口、各种 `objectStore` 实现（按 GVK 分发、泛型 map 适配器、策略专用 store、ReferenceGrant 转换 store）、以及真正的 `Updater` 实现 `changeTrackingUpdater`（带 changed 跟踪）。 |
| [internal/controller/state/changed_predicate.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/changed_predicate.go) | 定义 `stateChangedPredicate` 接口及其两种实现：`funcPredicate`（委托给一个函数）和 `annotationChangedPredicate`（按注解变化判定）。 |
| [internal/controller/state/graph/graph.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go) | `ClusterState`（存储原始资源）与 `Graph`（构建后的内部模型）的类型定义，以及 `IsReferenced`/`IsNGFPolicyRelevant` 两个判定方法——它们是 predicate 判定的依据。 |
| [internal/controller/handler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go) | 调用方：`eventHandlerImpl` 在何处调 `CaptureUpsertChange`/`CaptureDeleteChange`/`Process`/`ForceRebuild`。 |
| [internal/controller/state/change_processor_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor_test.go) | 测试：含「捕获不支持的类型会 panic」「合并 WAF bundle」等用例，是理解边界行为的依据。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 变更捕获**、**4.2 对象存储**、**4.3 changed predicate**。三者关系可以用一句话概括：

> 捕获（Capture）= 把对象写进存储（store）+ 用谓词（predicate）决定要不要把 `changed` 置真；`Process` 只在 `changed` 为真时才重建图。

### 4.1 变更捕获

#### 4.1.1 概念说明

`ChangeProcessor` 是 EventHandler 看到的「变更处理器」抽象。它的职责很克制：**只负责登记变更和决定要不要重建图，不负责生成 NGINX 配置、不负责下发**。生成配置是 `graph` 包的事，下发是 `NginxUpdater` 的事（u7 主题）。

之所以要把「登记」和「处理」拆成两个步骤（先一批 `Capture*`，再一次 `Process`），而不是「来一条处理一条」，是为了配合 u4-l2 的批处理：一批事件可以包含几十条变更，我们希望把它们「攒」在一起，最后用完整集群状态重建一次图，而不是每条都重建。

关键术语：

- **Capture（捕获）**：把一条增/删事件登记到内部状态，但**不立刻**重建图。
- **Process（处理）**：检查「这批捕获有没有产生有效变更」，有才重建图，没有就返回 `nil`。
- **changed 标志**：一个布尔值，任何一条 `Capture*` 都可能把它从 `false` 翻成 `true`（只置真、不清零）；`Process` 读取并清零它。

#### 4.1.2 核心流程

一次事件批次在 `ChangeProcessor` 内部的流转：

```
事件批次（来自 EventLoop，见 u4-l2）
   │
   ├─ 对每条事件:
   │     UpsertEvent ──► CaptureUpsertChange(obj)
   │                          └─ 加锁 ► updater.Upsert(obj)
   │                               ├─ 把 obj 写进对应 store
   │                               └─ 用 predicate 决定是否 changed=true
   │     DeleteEvent ──► CaptureDeleteChange(type, nsname)
   │                          └─ 加锁 ► updater.Delete(type, nsname)
   │                               ├─ 从对应 store 删除
   │                               └─ 用 predicate 决定是否 changed=true
   │     WAFBundleReconcileEvent ──► ForceRebuild()
   │                                    └─ 加锁 ► changed=true（不动 store）
   │
   └─ Process(ctx):
         ├─ getAndResetClusterStateChanged() == false ?
         │       └─ 是 ► 返回 nil（不重建图，不 reload）
         └─ 否 ► graph.BuildGraph(clusterState, ...) ► 存为 latestGraph ► 返回
```

两个要点先记住：

1. `Process` 在「没有有效变更」时返回 `nil`，EventHandler 据此跳过整个「构建配置 → 下发」环节——这是把多次无关变更压缩成零次 reload 的核心。
2. `ForceRebuild` 是一条「旁路」：它不碰任何 store，只把 `changed` 置真，专门给「集群状态没变、但外部条件变了（如 WAF bundle 就绪）」的场景用。

#### 4.1.3 源码精读

先看接口定义，它有五个方法：

[internal/controller/state/change_processor.go:40-58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L40-L58) — 定义 `CaptureUpsertChange`、`CaptureDeleteChange`、`Process`、`GetLatestGraph`、`ForceRebuild`。注意 `Process` 的注释明确说「If no changes were captured, the graph will be empty」（实际实现是返回 `nil`）。

实现结构体 `ChangeProcessorImpl` 只持有少量字段：

[internal/controller/state/change_processor.go:97-112](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L97-L112) — `clusterState`（集群状态快照）、`updater`（真正干活的人）、`getAndResetClusterStateChanged` / `forceClusterStateRebuild`（两个函数字段，在构造时绑定到 `changeTrackingUpdater` 的方法）、`latestGraph`（最近一次构建的图）、一把 `sync.Mutex`。

捕获方法极其简短——加锁后委托给 `updater`：

[internal/controller/state/change_processor.go:338-358](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L338-L358) — `CaptureUpsertChange` 调 `c.updater.Upsert(obj)`；`CaptureDeleteChange` 调 `c.updater.Delete(...)`；`ForceRebuild` 调 `c.forceClusterStateRebuild()`。三者都在同一把锁下进行，保证线程安全（捕获可能来自不同 goroutine，例如 WAF poller 与控制器并行投递事件）。

`Process` 是本模块的「闸门」：

[internal/controller/state/change_processor.go:360-386](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L360-L386) — 第 364 行 `if !c.getAndResetClusterStateChanged() { return nil }`。**这就是「无变更则不重建」的源头**：只要这一批捕获没有产生有效变更，`Process` 直接返回 `nil`，调用方（EventHandler）拿到 `nil` 就跳过整条下发链路。只有 changed 为真，才调用 `graph.BuildGraph`（u5-l1 主题），把当前 `clusterState` 翻译成内部模型并存为 `latestGraph`。

> 这里的 `return nil` 与 u4-l2 讲到的「同 Generation 重复 upsert 不触发重配」是同一机制的两面：双缓冲把多次事件攒成一批，而 `changed` 标志 + predicate 把「这一批里有没有真正影响配置的变更」判定出来。两层叠加，N 次资源变更才可能被压缩成远少于 N 次 reload。

`GetLatestGraph` 在 EventHandler 中有两个用途（u4-l3）：首次图构建就绪后立即取图下发、以及 leader 切换后重发最新图。它只是加锁返回 `latestGraph`：

[internal/controller/state/change_processor.go:418-423](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L418-L423)。

最后看调用方在 EventHandler 里怎么用这组方法，确认接口契约：

[internal/controller/handler.go:842-872](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L842-L872) — `parseAndCaptureEvent` 对 `UpsertEvent` 调 `CaptureUpsertChange`，对 `DeleteEvent` 调 `CaptureDeleteChange`，对 `WAFBundleReconcileEvent` 调 `ForceRebuild`。注意 WAF 分支的注释特别强调了「不调 `CaptureUpsertChange` 是因为那会用一个 metadata-only stub 覆盖真实策略对象」——这正是 `ForceRebuild` 设计成「不动 store」的原因。

#### 4.1.4 代码实践

**实践目标**：亲手验证「不支持的资源类型会让 `CaptureUpsertChange` panic」，从而理解 NGF 为何用 panic 这种「 fail-fast 」方式守门。

**操作步骤**（源码阅读型实践）：

1. 打开 [internal/controller/state/change_processor_test.go:4322-4368](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor_test.go#L4322-L4368)，阅读 `CaptureUpsertChange must panic` 与 `CaptureDeleteChange must panic` 两张 `DescribeTable`。
2. 注意两个 `Entry`：「an unsupported resource」（传一个 `&apiv1.Pod{}`）和「nil resource」（传 `nil`）都期望 `Should(Panic())`。
3. 回到实现 [internal/controller/state/store.go:303-309](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L303-L309)，看 `Upsert` 第一行 `s.assertSupportedGVK(s.extractGVK(obj))`，再跳到 [internal/controller/state/store.go:278-282](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L278-L282) 的 `assertSupportedGVK`，确认它对不在注册表里的 GVK 调 `panic`。
4. 思考：为什么用 panic 而不是返回 error？因为「收到一个未注册类型的资源」是**编程错误**（注册表漏配），不是运行期可恢复的故障，应当让进程尽早暴露而非吞掉。

**需要观察的现象 / 预期结果**：注册表（`trackingUpdaterCfg`）里列出的 GVK 是「允许捕获」的全集；任何不在其中的类型一旦被 predicate/控制器放行到这里，进程会直接 panic。**待本地验证**：你可以在测试里追加一个 `Entry`，传入一个合法但故意未注册的类型（如自定义 CRD 的 `Unstructured`），预期同样 panic。

#### 4.1.5 小练习与答案

**练习 1**：假如同一批事件里对同一个 Gateway 先 `CaptureUpsertChange` 又 `CaptureDeleteChange`，`Process` 会重建图吗？

**参考答案**：会。两步都不停地把 `changed` 累加（`changed = changed || changingUpsert/deleting`），只要其中任一步把 changed 置真，`Process` 第 364 行的判断就为真，触发重建。最终图反映的是「该 Gateway 已删除」的状态。

**练习 2**：`Process` 返回 `nil` 时，`latestGraph` 会被清空吗？

**参考答案**：不会。`Process` 在 changed 为假时直接 `return nil`，根本没有触碰 `latestGraph`。所以 `GetLatestGraph()` 仍能返回上一轮构建的图——这正是 EventHandler 在 leader 切换后用 `GetLatestGraph()` 重发配置的基础。

---

### 4.2 对象存储

#### 4.2.1 概念说明

「捕获」要落到一个实在的地方——这就是**对象存储（object store）**。NGF 把集群里它关心的资源按类型分别存进一组 map，这组 map 加在一起就是 `clusterState`。`Process` 时，`graph.BuildGraph` 拿这份 `clusterState` 作为输入重建图。

这里有个关键设计：存储并不只是一个扁平大 map，而是「**按 GVK 分发**」的多层结构。原因有二：

1. **类型安全**：Gateway、HTTPRoute、Service 是不同的 Go 类型，存进同一个 map 需要类型断言，易错；按类型分 map（`map[NamespacedName]*v1.Gateway` 等）既类型安全又省去断言。
2. **可扩展**：有的资源需要特殊处理（如 NGF Policy 用 `PolicyKey` 而非 `NamespacedName` 作键；ReferenceGrant 要把 v1beta1 转成 v1 再存），每种特化都封装成一个实现同一 `objectStore` 接口的适配器，互不干扰。

关键术语：

- **GVK（GroupVersionKind）**：资源的「类型身份证」，如 `gateway.networking.k8s.io/v1/Gateway`。store 用它做分发键。
- **persisted（持久化）**：一个类型的对象是否真正存进 `clusterState`。绝大多数类型 persisted=true；但 **EndpointSlice 例外**——它只用来判定「是否触发变更」，并不进 `clusterState`（端点解析走另一条路，见 u5-l3）。

#### 4.2.2 核心流程

对象存储的分层结构：

```
changeTrackingUpdater（Updater 实现）
   ├─ store: *multiObjectStore            ← 按 GVK 分发
   │     └─ stores: map[GVK]objectStore
   │           ├─ objectStoreMapAdapter[*v1.Gateway]    ← 泛型适配器（大多数类型）
   │           ├─ objectStoreMapAdapter[*v1.HTTPRoute]
   │           ├─ ngfPolicyObjectStore                   ← 策略专用，按 PolicyKey 存储
   │           ├─ convertingReferenceGrantStore          ← v1beta1→v1 转换
   │           └─ ... (每类资源一个)
   │
   ├─ stateChangedPredicates: map[GVK]stateChangedPredicate   ← 决定是否 changed（见 4.3）
   │
   └─ changed: bool   ← 单一全局脏标志
```

`multiObjectStore.upsert(obj)` 的逻辑：先用 `extractGVK(obj)` 拿到类型身份证，再到 `stores` map 里找到对应 store，委托它写入。如果找不到对应 store，`mustFindStoreForObj` 会 panic——这与 4.1 讲的「未注册类型 panic」是同一道防线。

`clusterState` 本身只是一个结构体，里面塞了一堆按类型分的 map：

[internal/controller/state/graph/graph.go:33-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L33-L57) — `ClusterState` 结构，注意它的字段与 4.2.4 里 `NewChangeProcessorImpl` 初始化的那组 map 一一对应。

#### 4.2.3 源码精读

先看两个核心接口：

[internal/controller/state/store.go:20-30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L20-L30) — `Updater`（对外的增删接口）与 `objectStore`（内部的 get/upsert/delete 接口）。`Updater` 是 `ChangeProcessorImpl.updater` 字段的类型，`objectStore` 是每个分类型 store 要实现的接口。

最常用的是泛型适配器，它把一个 `map[NamespacedName]T` 包装成 `objectStore`：

[internal/controller/state/store.go:84-113](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L84-L113) — `objectStoreMapAdapter[T]`。`upsert` 里有一行类型断言 `obj.(T)`，类型不匹配就 panic，保证「写进 Gateway map 的只能是 `*v1.Gateway`」。这是用 Go 泛型实现「类型安全的分类型存储」的典型写法。

策略资源用专门的 store，因为它要按 `(NamespacedName, GVK)` 组合的 `PolicyKey` 存（不同策略 CRD 可能同名，必须靠 GVK 区分）：

[internal/controller/state/store.go:34-80](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L34-L80) — `ngfPolicyObjectStore`。所有 NGF 策略（ClientSettingsPolicy、ObservabilityPolicy、UpstreamSettingsPolicy、ProxySettingsPolicy、WAFPolicy、RateLimitPolicy、SnippetsPolicy）共用同一个 store（即 `clusterStore.NGFPolicies` 这个 map），靠 GVK 区分。

ReferenceGrant 有个历史包袱：v1 与 v1beta1 两个版本并存。NGF 内部一律按 v1 处理，所以遇到 v1beta1 的对象要先转换：

[internal/controller/state/store.go:117-168](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L117-L168) — `convertingReferenceGrantStore` 在 `upsert` 时把 `*v1beta1.ReferenceGrant` 逐字段拷成 `*v1.ReferenceGrant` 再存进底层 v1 store。这与 u3-l3 讲的「ReferenceGrant v1→v1beta1 回退」是配套的：能探测到 v1 CRD 就用 v1 store，探测不到就回退到这个转换 store。

把这些 store 串起来的是 `multiObjectStore`：

[internal/controller/state/store.go:176-219](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L176-L219) — 持有 `stores map[GVK]objectStore` 与 `persistedGVKs`（标记哪些类型真正持久化）。`mustFindStoreForObj` 是分发核心：找不到就 panic。`persists` 方法供 `changeTrackingUpdater` 判断「这个类型要不要读写 store」（EndpointSlice 的 store 是 nil，故 `persists` 返回 false，永远不会去 get/upsert/delete 真实存储）。

真正的 `Updater` 实现是 `changeTrackingUpdater`，它把「store 写入」和「changed 跟踪」绑在一起：

[internal/controller/state/store.go:235-276](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L235-L276) — 构造函数遍历 `objectTypeCfgs`，把每个配置里的 store（非 nil 才记）和 predicate 分别收进两个 map，并维护 `supportedGVKs`（合法类型全集）。

upsert 的内部实现是 4.3 模块的入口，但存储部分在这里：

[internal/controller/state/store.go:284-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L284-L301) — 注意顺序：**先取出 oldObj、再写入新 obj**（第 289-293 行），这样 predicate 才能拿到「改之前」和「改之后」两个对象做对比（详见 4.3 的 `annotationChangedPredicate`）。对未持久化的类型（`persists` 为假），跳过 get/upsert，只走 predicate。

最后看 `clusterState` 在哪初始化、各 store 如何与字段绑定：

[internal/controller/state/change_processor.go:116-162](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L116-L162) — `NewChangeProcessorImpl` 创建空 `ClusterState`，并为每类资源建一个 `objectStoreMapAdapter` 指向对应 map。注意第 162 行所有 NGF 策略共享同一个 `commonPolicyObjectStore`。

#### 4.2.4 代码实践

**实践目标**：跟踪「一个 HTTPRoute 被捕获后，最终落在 `clusterState` 的哪个字段」，建立从事件到存储的肌肉记忆。

**操作步骤**（源码阅读型实践）：

1. 假设 EventHandler 收到一条 HTTPRoute 的 `UpsertEvent`，调 `processor.CaptureUpsertChange(httpRoute)`。
2. 跟进到 [change_processor.go:338-343](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L338-L343)，确认它调 `c.updater.Upsert(obj)`。
3. `updater` 是 `changeTrackingUpdater`，跟进 [store.go:303-309](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L303-L309)（`Upsert`），它调内部 `upsert`（[store.go:284-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L284-L301)）。
4. `extractGVK` 得到 HTTPRoute 的 GVK，`persists` 为真，于是 `s.store.get(...)` 取 oldObj，再 `s.store.upsert(obj)` 写入。
5. `s.store` 是 `multiObjectStore`，跟进 [store.go:209-211](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L209-L211)，它把请求转发给 HTTPRoute 对应的 `objectStoreMapAdapter`。
6. 该适配器在 [change_processor.go:176-179](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L176-L179) 创建，绑定的 map 正是 `clusterStore.HTTPRoutes`。

**预期结果**：这个 HTTPRoute 最终落在 `clusterState.HTTPRoutes[NamespacedName]`。下次 `Process` 重建图时，`graph.BuildGraph` 会从 `state.HTTPRoutes` 读取它。

#### 4.2.5 小练习与答案

**练习 1**：为什么 NGF 策略不能像 HTTPRoute 那样用一个 `objectStoreMapAdapter`？

**参考答案**：因为不同策略 CRD（如 `ClientSettingsPolicy` 和 `UpstreamSettingsPolicy`）可能同名同命名空间，仅靠 `NamespacedName` 无法区分。`ngfPolicyObjectStore` 用 `(NamespacedName, GVK)` 组成的 `PolicyKey` 作键，才能无歧义地存放所有策略类型。

**练习 2**：EndpointSlice 的 `changeTrackingUpdaterObjectTypeCfg` 里 `store` 是 `nil`（见 [change_processor.go:207-210](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L207-L210)），这意味着什么？

**参考答案**：意味着 EndpointSlice **不进 `clusterState`**，捕获它时不会调用 get/upsert/delete 任何真实存储。它存在的唯一目的是配合 predicate 判断「这个 EndpointSlice 所属的 Service 是否被路由引用」——若是则 `changed=true`，若否则静默丢弃。Endpoint 的实际解析在图构建阶段通过 resolver 另走（u5-l3）。

---

### 4.3 changed predicate

#### 4.3.1 概念说明

如果每来一条事件都重建图、reload NGINX，集群稍微抖动一下（比如某个根本没被任何路由引用的 Service 更新了 status）就会引发一次无谓的 reload。`changed predicate`（变更谓词）就是用来回答这个问题的：

> 这条 upsert/delete，**到底有没有改变会影响配置的状态？**

`changeTrackingUpdater` 为每类资源配置一个 predicate。捕获时，**写存储是一回事，predicate 判定是另一回事**——只有 predicate 返回 `true`，才把全局 `changed` 置真；predicate 返回 `false`，对象照常写进存储，但 `changed` 保持不变，于是这一批 `Process` 会返回 `nil`，跳过重建。

NGF 把资源分成三类，对应三种 predicate 策略：

| 类别 | predicate | 含义 | 典型资源 |
|------|-----------|------|----------|
| 配置主体 | `nil` | **任何**变更都算数，始终 `changed=true` | GatewayClass、Gateway、HTTPRoute、GRPCRoute、TLS/TCP/UDPRoute、BackendTLSPolicy、ListenerSet、APPolicy、APLogConf、SnippetsFilter、AuthenticationFilter |
| 被引用才相关 | `funcPredicate{isReferenced}` | 仅当对象被当前图引用时才算变更 | Service、Secret、Namespace、EndpointSlice、NginxProxy、InferencePool |
| 策略相关性 | `funcPredicate{isNGFPolicyRelevant}` | 仅当策略在图中或其 targetRef 命中图中资源时才算变更 | 各类 NGF Policy |
| 按注解变化 | `annotationChangedPredicate` | 仅当指定注解的值变化才算变更 | CRD（`bundle-version` 注解） |

> **为什么 `nil` predicate 等于「始终变更」**：看 [store.go:295-298](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L295-L298)，`if !ok { return true }`——没注册 predicate 的类型，upsert 直接返回 `true`。这些是「配置主体」资源：它们本身的任何变化都可能影响路由/监听器结构，宁可不漏。

#### 4.3.2 核心流程

一次 upsert 在 `changeTrackingUpdater` 里的判定流程：

```
upsert(obj):
   1. 取 objTypeGVK
   2. if persists(gvk):
         oldObj = store.get(obj)     ← 拿旧值
         store.upsert(obj)            ← 写新值
   3. stateChanged, ok = predicates[gvk]
   4. if !ok: return true             ← nil predicate：始终变更
   5. return stateChanged.upsert(oldObj, obj)   ← 交给谓词用 old/new 对比判定
```

delete 类似，但 predicate 拿不到「新对象」，只能基于 `(类型, 名称)` 判定（通常是「被引用就删、不被引用本来也没存就返回 false」）。

关键直觉：**predicate 让「是否影响配置」的判断从「对象变了」下沉到「对象变了且这次变化有意义」**。一个被 100 个 Service 共享的集群里，只有那几个被路由引用的 Service 的变更才会触发 reload，其余都被 predicate 静默吸收。

#### 4.3.3 源码精读

先看谓词接口：

[internal/controller/state/changed_predicate.go:10-16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/changed_predicate.go#L10-L16) — `stateChangedPredicate` 只有两个方法：`upsert(old, new)` 和 `delete(type, nsname)`。注意 `upsert` 同时拿到旧对象和新对象——这是为「对比变化」准备的。

第一种实现 `funcPredicate`，把判定委托给一个外部函数（依赖注入）：

[internal/controller/state/changed_predicate.go:20-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/changed_predicate.go#L20-L34) — `upsert` 忽略 `oldObject`，只把 `newObject` 连同它的 `NamespacedName` 交给 `stateChanged` 函数。这个函数在 `NewChangeProcessorImpl` 里被定义为 `isReferenced` 或 `isNGFPolicyRelevant`（见 [change_processor.go:146-159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L146-L159)）。`isReferenced` 的核心是「去问当前图 `latestGraph.IsReferenced(...)`」：

[internal/controller/state/graph/graph.go:133-195](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L133-L195) — `IsReferenced` 用一个大 switch 按类型查图里各种 `Referenced*` map。例如 Service（第 163-165 行）查 `ReferencedServices`，Secret（第 135-142 行）查多个引用集合（Gateway 引用、Plus 报告、WAF 鉴权、PLM 存储四类都要查）。

> 注意这里有一个**首图悖论**：`isReferenced` 依赖 `latestGraph`，但 `latestGraph` 在首次 `Process` 前是 `nil`。看 [change_processor.go:146-148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L146-L148)：`latestGraph != nil && ...`。首图时 `latestGraph` 为 nil，`isReferenced` 直接返回 false。这不会导致首批 Service/Secret 被漏掉，因为首批事件里有 Gateway/HTTPRoute 这类 `nil` predicate 的「主体」资源，它们会先把 `changed` 置真，触发首次完整建图；之后 Service/Secret 的判定才有 `latestGraph` 可依。这也呼应了 u4-l2 讲的「首批事件由 `FirstEventBatchPreparer` 主动 list 全量资源」。

第二种实现 `annotationChangedPredicate`，专门用于 CRD 的 `bundle-version` 注解：

[internal/controller/state/changed_predicate.go:39-60](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/changed_predicate.go#L39-L60) — `upsert` 里：oldObj 为 nil（首次）直接返回 true；否则比较新旧对象上指定注解的值，只有值变了才返回 true。`delete` 永远返回 true（删了肯定要重建）。

为什么 CRD 要这么特殊？因为 NGF 只关心 CRD 的「支持的版本」这个注解（`bundle-version`，来自 Gateway API 的 consts），CRD 对象本身的 `.metadata` 频繁变化（如 `managedFields`）对配置毫无影响——这正是 u3-l2 cache transform 剥掉 `managedFields` 的同一类噪声，predicate 在这里做第二道过滤。

现在把三类 predicate 放回注册表里看全貌：

[internal/controller/state/change_processor.go:164-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L164-L301) — 这是 `trackingUpdaterCfg` 全表。仔细对照三个字段：`gvk`（类型）、`store`（存储，nil=不持久化）、`predicate`（nil=始终变更）。例如：
- GatewayClass/Gateway/HTTPRoute：`predicate: nil`（主体，任何变更都算）。
- Service/Secret/Namespace/EndpointSlice/NginxProxy/InferencePool：`predicate: funcPredicate{stateChanged: isReferenced}`（被引用才算）。
- 各 NGF Policy：`predicate: funcPredicate{stateChanged: isNGFPolicyRelevant}`（策略相关性才算）。
- CRD：`predicate: annotationChangedPredicate{annotation: consts.BundleVersionAnnotation}`（注解变化才算）。
- SnippetsFilter/AuthenticationFilter：`predicate: nil`，代码注释 [change_processor.go:282-290](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L282-L290) 说明原因——「我们总是想给它们写 status，所以不过滤」。

最后，`changeTrackingUpdater.Upsert/Delete` 把 predicate 结果累加进全局 `changed`：

[internal/controller/state/store.go:303-336](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L303-L336) — 第 308 行 `s.changed = s.changed || changingUpsert`（只置真不清零）。而 `Process` 通过 [store.go:340-344](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L340-L344) 的 `getAndResetChangedStatus` 读取并清零。`forceRebuild`（[store.go:349-351](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L349-L351)）是旁路：直接把 `changed=true`，给 WAF bundle 场景用。

#### 4.3.4 代码实践

**实践目标**：用真实源码讲清「changed predicate 如何避免无意义的状态重建」，并验证一个具体场景——一个**未被任何路由引用的 Service** 更新时，不应触发图重建。

**操作步骤**（源码阅读 + 推理型实践）：

1. **构造场景**：集群里有一个 Service `ns-a/svc-x`，但没有任何 HTTPRoute/GRPCRoute 的 `backendRefs` 指向它。
2. **追踪捕获路径**：Service 的 controller 发来 `UpsertEvent`，`CaptureUpsertChange(svc)` → `changeTrackingUpdater.upsert`。
3. **看存储**：[store.go:289-293](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L289-L293)，Service 是 persisted 的，`oldObj` 取出（假设是首次则为 nil）、新 svc 写进 `clusterState.Services`。**注意：对象照常被存储了。**
4. **看判定**：[store.go:295-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L295-L301)，Service 注册了 `funcPredicate{isReferenced}`，于是调 `isReferenced(svc, nsname)`。
5. **看 isReferenced**：[change_processor.go:146-148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L146-L148) 转到 [graph.go:163-165](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L163-L165)，查 `g.ReferencedServices[nsname]`。因为没人引用 `svc-x`，返回 false。
6. **结论**：predicate 返回 false → `changingUpsert=false` → 全局 `changed` 保持不变。若这一批只有这一个事件，`Process` 第 364 行判定为 false，返回 `nil`，**不会重建图、不会 reload NGINX**。

**需要观察的现象 / 预期结果**：
- **未被引用的 Service 频繁更新**：从控制面日志/指标看，不会出现 NGINX reload（待本地验证：可观察 NGINX Pod 的 reload 计数或 NGF 的配置下发日志）。
- **一旦有 HTTPRoute 把 `svc-x` 设为 backendRef**：下一次该 HTTPRoute 的 upsert 会（它是 nil predicate）把 `changed` 置真、重建图；重建后 `ReferencedServices` 里就有了 `svc-x`，此后 `svc-x` 的任何变更都会因为 `isReferenced` 返回 true 而触发重建。

**关键洞察**：predicate 把「影响 reload 的变更」从「任意资源变更」收窄到「与当前配置真正相关的资源变更」。在一个有成百上千 Service 的大集群里，这个收窄是 NGF 控制面吞吐与 NGINX 稳定性的关键。

#### 4.3.5 小练习与答案

**练习 1**：假如一个 NGF Policy（如 ClientSettingsPolicy）附着在一个根本不存在的 HTTPRoute 上，它的更新会触发图重建吗？

**参考答案**：取决于它是否已在图中或 targetRef 是否命中。`IsNGFPolicyRelevant`（[graph.go:198-233](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L198-L233)）先查 `g.NGFPolicies[key]` 是否存在，再遍历 `GetTargetRefs()` 看是否命中图中的 Gateway/HTTPRoute/Service。若该 HTTPRoute 不在图中且 Policy 也未进图，则返回 false，policy 的更新不会触发重建。这避免了「大量配错或悬空的策略」拖垮控制面。

**练习 2**：`forceRebuild` 与一次普通的「Gateway upsert」最终都把 `changed` 置真，它们的区别是什么？

**参考答案**：区别在于**是否修改 clusterState**。普通 Gateway upsert 会先把对象写进 `clusterState.Gateways` 再置真；`forceRebuild`（[store.go:349-351](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/store.go#L349-L351)）只置真、不动任何 store。`forceRebuild` 专门用于「集群状态未变、但外部条件变了」的场景——典型是 WAF bundle 刚拉取成功（见 [handler.go:867-872](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L867-L872) 的注释），此时需要重建图以应用新 bundle，但绝不能拿一个 metadata-only 的 stub 去覆盖真实策略对象。

## 5. 综合实践

把三个模块串起来，完成一次「**给一类新资源配置捕获行为**」的源码设计练习（不写代码，只做设计跟踪）。

**场景**：假设 NGF 新增了一种策略 CRD `HeaderRewritePolicy`，它附着在 HTTPRoute 上做请求头改写。请回答：

1. **存储**：它该用哪种 store？为什么？
   - 提示：它是一种 NGF 策略，需按 GVK 区分，应复用 `commonPolicyObjectStore`（`ngfPolicyObjectStore`），并存进 `clusterStore.NGFPolicies`。
2. **predicate**：它该用哪种 predicate？为什么？
   - 提示：它是「附着型」策略，只有附着到图中资源才有意义，应用 `funcPredicate{stateChanged: isNGFPolicyRelevant}`，这样悬空或附错对象的策略不会无谓触发 reload。
3. **注册**：在 `trackingUpdaterCfg`（[change_processor.go:164-301](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L164-L301)）里追加一项 `changeTrackingUpdaterObjectTypeCfg`，三元组（gvk / store / predicate）怎么填？参考已有的 `ClientSettingsPolicy` 配置（[change_processor.go:232-235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L232-L235)）。
4. **联动**：除了这里，还要改哪些地方才能让它真正工作？
   - 提示：参考 u3-l3——控制器注册表（决定 watch 它）、首批事件 list（`FirstEventBatchPreparer`，决定首图包含它）、以及 u5 的图构建（`processPolicies` 要识别新类型）。`change_processor.go` 的注册表只是「捕获后怎么存、要不要算变更」这一环。

**预期收获**：通过这个设计练习，你会理解「新增一种被 NGF 处理的资源」需要跨越注册→捕获→存储→判定→建图多个环节，本讲覆盖的是其中的「捕获 + 存储 + 判定」三环。

## 6. 本讲小结

- `ChangeProcessor` 只做三件事：捕获变更、维护 `clusterState`、在 `Process` 时按需重建图；它不碰 NGINX 配置生成与下发。
- `Process` 的闸门是 `changed` 标志：`getAndResetClusterStateChanged()` 为假时直接返回 `nil`，整条「生成配置→下发」链路被跳过——这是把多次无关变更压缩成零次 reload 的核心。
- 对象存储按 GVK 分发：`multiObjectStore` 下挂一组适配器（泛型 `objectStoreMapAdapter`、策略专用 `ngfPolicyObjectStore`、`convertingReferenceGrantStore`），既类型安全又支持特化；EndpointSlice 是唯一不持久化（store=nil）的类型。
- `changed predicate` 把「资源变了」收窄为「与配置相关的资源变了」：`nil`（主体资源，始终算）、`funcPredicate{isReferenced}`（被引用才算）、`funcPredicate{isNGFPolicyRelevant}`（策略相关才算）、`annotationChangedPredicate`（注解变化才算）。
- `ForceRebuild` 是「不动 store、只置真 changed」的旁路，专供 WAF bundle 等外部条件变化场景，避免用 stub 覆盖真实对象。
- predicate 判定依赖 `latestGraph`，首图时为 nil——这不会丢事件，因为首批里总有 nil-predicate 的主体资源先把 changed 置真、触发完整建图。

## 7. 下一步学习建议

本讲止于「`Process` 输出一个 `graph.Graph`」。这个 graph 是怎么从 `clusterState` 构建出来的、内部长什么样？请进入：

- **u5-l1 Graph：把 Gateway API 资源变成内部模型**：精读 `graph.BuildGraph`，理解 GatewayClass 被认领/拒绝、Gateway 到图的映射、校验与 conditions 收集。
- **u5-l3 后端解析**：理解本讲提到「EndpointSlice 不进 clusterState」之后，端点到底是怎么被 resolver 解析进图的。
- **u8-l1 Conditions 体系与状态更新**：本讲的 predicate 只决定「要不要重建图」，而重建出的图里携带的 conditions 如何异步写回 K8s 资源，是 u8-l1 的主题。

另外，若你想验证本讲的行为，强烈建议阅读 [internal/controller/state/change_processor_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor_test.go) 中的 `Ordered` 测试组——它们用「先建图、再捕获一个不相关 Service、再 Process 应返回 nil」的断言，把本讲的机制钉死在测试里。
