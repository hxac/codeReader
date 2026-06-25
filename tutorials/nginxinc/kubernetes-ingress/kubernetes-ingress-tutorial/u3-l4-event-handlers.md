# 事件处理器：从资源变更到入队

## 1. 本讲目标

上一讲（u3-l3）我们看清了「调度层」`taskQueue`：它把事件解耦成可消费的 `task{Kind, Key}`，单 worker 串行消费，**没有指数退避、没有最大重试次数**，只靠「单 worker + 去重」天然节流。

这就引出一个尖锐的问题：既然队列本身不做节流，那么「**哪些事件该入队、哪些该被丢弃**」这个决定权在谁手里？

答案就是**本讲的主角——事件处理器（event handler）**。它是感知层（Informer）和调度层（taskQueue）之间的「过滤阀」。

学完本讲，你应该能够：

1. 说清 `createXHandlers` 这套统一三件套（AddFunc / DeleteFunc / UpdateFunc）的固定骨架，并能照葫芦画瓢写出新资源的 handler。
2. 解释为什么不同资源的 UpdateFunc 用了**截然不同**的变更检测策略（有的比整个对象、有的只比 Spec、有的只比个别字段），以及每 种选择背后的性能与正确性考量。
3. 理解 `cache.DeletedFinalStateUnknown`（tombstone，墓碑对象）的成因与防御性处理模式。
4. 读懂 Service、Policy 这两个「专用 handler」为何要脱离通用模板，自行定制逻辑。

---

## 2. 前置知识

本讲承接 u3-l1～u3-l3，假设你已经理解：

- **控制器循环骨架**（u3-l1）：`LoadBalancerController.Run()` 先 `WaitForCacheSync`，再启动 `syncQueue`。
- **Informer 与 list-watch**（u3-l2）：Informer 在构造期注册 handler、运行期统一 `Start`；`addXHandler` 是「取 Informer → 挂 `AddEventHandler` → 取 Lister 并登记 `HasSynced`」的三步模式；所有事件最终汇聚到 `AddSyncQueue` 入队。
- **taskQueue 语义**（u3-l3）：入队分 `Enqueue / Requeue / RequeueAfter`；`task` 只存 `{Kind, Key}`，**不存对象本身**；队列去重、单 worker。

本讲需要补充两个 client-go 的基础概念：

| 概念 | 含义 |
| --- | --- |
| `cache.ResourceEventHandlerFuncs` | client-go 提供的一个结构体，含 `AddFunc / UpdateFunc / DeleteFunc` 三个函数字段。你只需把三个闭包填进去，就得到了一个 `ResourceEventHandler`。 |
| `cache.DeletedFinalStateUnknown` | watch 长连接断开重连后，client-go 用本地缓存兜底补发「遗漏的删除事件」时包装出的墓碑对象，字段 `.Obj` 才是真正的被删对象。详见 4.3。 |

一句话回顾数据流方向：

```
apiserver  ──watch──▶  Informer  ──回调──▶  event handler(createXHandlers)
                                                      │
                                              AddSyncQueue(item)   ← 本讲研究的「过滤阀」
                                                      │
                                                      ▼
                                               taskQueue(u3-l3)  ──▶  sync(u3-l5)
```

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/k8s/handlers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go) | **主战场**。Ingress / Secret / VirtualServer / VirtualServerRoute 的 handler 工厂函数，以及通用的 `areResourcesDifferent`、权重清零辅助。 |
| [internal/k8s/service.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go) | Service 专用 handler `createServiceHandlers`，及其定制的变更检测 `hasServicedChanged / hasServicePortChanges / hasServiceExternalNameChanges`。 |
| [internal/k8s/policy.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go) | Policy 专用 handler `createPolicyHandlers`。 |
| [internal/k8s/transport_server.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go) | TransportServer handler，是「最朴素」模板的代表，便于对照。 |
| [internal/k8s/endpoint_slice.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/endpoint_slice.go) | EndpointSlice handler，同样是朴素模板。 |
| [internal/k8s/controller.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go) | 在 `newNamespacedInformer` / `addCustomResourceHandlers` 中把 handler 工厂装配到 Informer 上；`AddSyncQueue` 的定义也在此。 |
| [internal/k8s/utils.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/utils.go) | Ingress 专用的变更检测函数 `hasChanges`。 |

---

## 4. 核心概念与源码讲解

本讲的四个最小模块构成一条递进线索：先掌握通用模板（4.1），再看模板里最需要动脑子的「变更检测」（4.2），接着补上删除路径的防御性细节（4.3），最后看两个主动「打破」模板的专用 handler（4.4）。

### 4.1 handler 统一模式：createXHandlers 三件套与装配

#### 4.1.1 概念说明

NGINX Ingress Controller（以下简称 NIC）要监听的资源种类很多：Ingress、Secret、Service、EndpointSlice、VirtualServer、VirtualServerRoute、TransportServer、Policy，以及可选的 App Protect 资源。如果每种资源各写一套完全不同的 handler，代码会迅速膨胀且难以维护。

NIC 的做法是：**为每种资源写一个 `createXHandlers(lbc)` 工厂函数**，返回一个填好三个闭包的 `cache.ResourceEventHandlerFuncs`。这三个闭包就是 client-go Informer 回调的三件套：

- `AddFunc(obj)`：资源新增时触发。
- `UpdateFunc(old, cur)`：资源更新时触发，同时拿到新旧两个对象。
- `DeleteFunc(obj)`：资源删除时触发。

这套三件套的**唯一职责**就是把「值得处理」的资源对象转交给 `lbc.AddSyncQueue(item)`。**handler 不做配置生成、不写 status、不 reload**——那是后续 `sync` 的事。handler 只当「过滤阀 + 入队器」。

#### 4.1.2 核心流程

通用模板的骨架（以 TransportServer 为最干净的样本）：

```
AddFunc(obj):
    x = obj.(*T)              # 类型断言
    log "Adding T"
    lbc.AddSyncQueue(x)       # 无条件入队

DeleteFunc(obj):
    x, ok = obj.(*T)          # 先尝试直接断言
    if !ok:                   # 断言失败 → 处理 tombstone（见 4.3）
        ...从 DeletedFinalStateUnknown 里取出真实对象...
    log "Removing T"
    lbc.AddSyncQueue(x)

UpdateFunc(old, cur):
    if <发生了值得关心的变化>:   # 变更检测（见 4.2）
        log "T changed, syncing"
        lbc.AddSyncQueue(cur)
```

装配发生在 `newNamespacedInformer` 里（core 资源）和 `addCustomResourceHandlers` 里（CRD 资源）：把工厂函数的产物传给对应的 `addXHandler`，由后者挂到 Informer 上（这一步在 u3-l2 已讲）。

#### 4.1.3 源码精读

TransportServer handler 是最朴素的样本——三件套里没有任何花活，最适合当模板记：

[internal/k8s/transport_server.go:18-50](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/transport_server.go#L18-L50) —— `createTransportServerHandlers`：AddFunc 直接断言并入队；DeleteFunc 带 tombstone 兜底；UpdateFunc 用 `reflect.DeepEqual(old, cur)` 比较整个对象。

装配点：core 资源（Ingress/Service）在构造每个命名空间 informer 时挂上：

[internal/k8s/controller.go:558-567](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L558-L567) —— `nsi.addIngressHandler(createIngressHandlers(lbc))`、`addServiceHandler(createServiceHandlers(lbc))`、`addEndpointSliceHandler(...)`，Pod 例外只建 Lister 不挂 handler。

CRD 资源（VS/VSR/TS/Policy）在 `areCustomResourcesEnabled` 时才挂：

[internal/k8s/controller.go:614-625](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L614-L625) —— 四个 CRD handler 依次挂到 `confSharedInformerFactory`。

所有 handler 的终点都是这一个一行函数：

[internal/k8s/controller.go:663-666](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L663-L666) —— `AddSyncQueue` 只是 `lbc.syncQueue.Enqueue(item)` 的薄封装。这是「感知层 → 调度层」的唯一入口。

> 小贴士：注意 `createXHandlers` 接收的是 `lbc *LoadBalancerController` 指针，闭包通过它调用 `lbc.AddSyncQueue`、`lbc.Logger`、以及一些运行期标志（如 `weightChangesDynamicReload`）。handler 不是纯函数，它能访问控制器的一切状态——但模板里它只用了「入队」这一个动作。

#### 4.1.4 代码实践

**实践目标**：建立「handler = 三件套工厂 + 装配」的空间感。

**操作步骤**：

1. 打开 `internal/k8s/handlers.go`、`service.go`、`policy.go`、`transport_server.go`、`endpoint_slice.go`，对比五个 `createXHandlers` 工厂函数的骨架。
2. 在 `controller.go` 中搜索 `createIngressHandlers(` 等调用点，确认每个工厂都被一个 `addXHandler` 包裹。

**需要观察的现象**：五个工厂函数的 AddFunc 几乎一模一样（断言 → log → 入队）；DeleteFunc 都有一段长得几乎相同的 tombstone 兜底代码；真正不同的是 UpdateFunc。

**预期结果**：你能口述出「新增 core 资源 handler 需要两步——写一个 `createXHandlers` 工厂，再在 `newNamespacedInformer` 里加一行 `nsi.addXHandler(createXHandlers(lbc))`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么不把三个闭包直接写在 `addXHandler` 调用处，而要单独抽成 `createXHandlers` 工厂函数？

**参考答案**：分离关注点。`addXHandler`（在 u3-l2 讲过）负责「取 Informer → 挂 handler → 取 Lister → 登记 HasSynced」这套和 client-go Informer 打交道的机械动作；`createXHandlers` 只负责「定义三个回调的业务逻辑」。两者解耦后，新增资源只需各写一块，互不干扰；也方便单独测试 handler 逻辑。

**练习 2**：`AddSyncQueue` 内部只有一行 `lbc.syncQueue.Enqueue(item)`。既然如此，为何 handler 不直接调 `lbc.syncQueue.Enqueue`？

**参考答案**：为了**封装与可读性**。`syncQueue` 是控制器的内部调度细节，handler 层用语义化的 `AddSyncQueue` 表达「我要把这个资源加入同步队列」的意图；同时这层封装也为将来在入队前后插埋点、指标（如 workqueue 深度指标）留出统一的钩子位置。

---

### 4.2 变更检测：UpdateFunc 如何避免无效重载

#### 4.2.1 概念说明

这是本讲**最重要**的一节。请记住 u3-l3 的结论：**taskQueue 没有指数退避、没有最大重试**。这意味着如果 handler 把每一次 Informer 回调都原封不动入队，那么一次无意义的 status 更新就会触发一次完整的 NGINX reload。

NGINX reload 是**昂贵**的（要 fork 新 worker、重新加载配置、做版本验证，见 u5 单元）。在高频变更的集群里，「无效 reload」会拖垮控制器。因此**变更检测（change detection）是 handler 层对性能最重要的贡献**——它在事件进入队列之前就丢弃掉「看起来变了、但实质没变」的更新。

不同的资源需要不同的「实质变更」定义，于是 NIC 给每种资源量身定制了 UpdateFunc。规律是：

- **哪些字段会影响 NGINX 配置，就只比哪些字段。**
- 会因为「无关原因」频繁变动的字段（如 `ResourceVersion`、`Status`），必须在比较前**归一化（normalize）掉**。

#### 4.2.2 核心流程

NIC 的 UpdateFunc 变更检测分四档，从「最挑剔」到「最宽松」：

```
┌─────────────────────────────────────────────────────────────────┐
│ 档位 1：定制字段比较（最挑剔）—— Service                          │
│   只看 ports / externalName / headless，其余一概忽略               │
│   hasServicedChanged(oldSvc, curSvc)                             │
├─────────────────────────────────────────────────────────────────┤
│ 档位 2：归一化后比较整个对象 —— Ingress                           │
│   先把 old.Status 与 old.ResourceVersion 抹平成 current，再比较     │
│   hasChanges(old, current)                                       │
├─────────────────────────────────────────────────────────────────┤
│ 档位 3：只比较 .Spec —— Policy / VirtualServer / VSR              │
│   reflect.DeepEqual(old.Spec, cur.Spec)                          │
├─────────────────────────────────────────────────────────────────┤
│ 档位 4：直接比较整个对象（最宽松）—— TransportServer/EndpointSlice  │
│   reflect.DeepEqual(old, cur)                                    │
└─────────────────────────────────────────────────────────────────┘
```

为什么有这四档差异？关键在于**「无关变动」的来源不同**：

- Ingress 每次被控制器回写 status（外部 LB 地址）时，`Status.LoadBalancer.Ingress` 和 `ResourceVersion` 都会变，但这跟路由配置无关——必须归一化掉，否则会形成「写 status → 触发 UpdateFunc → reload → 又写 status」的自激振荡。
- Policy/VS 的 status 也在变，但它们直接 `reflect.DeepEqual(old.Spec, cur.Spec)`，只比 Spec，天然就把 status 隔离了。
- Service 最特殊，见 4.4。

#### 4.2.3 源码精读

**Ingress 的归一化比较**——这是理解「为什么不能直接 DeepEqual」的钥匙：

[internal/k8s/utils.go:145-150](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/utils.go#L145-L150) —— `hasChanges` 把 `old.Status.LoadBalancer.Ingress` 和 `old.ResourceVersion` 强行改成 current 的值，**再**做 `reflect.DeepEqual`。这样只有「除 status 和版本号以外的字段」变了才会返回 true。注意这是直接改 `old` 指针的字段——因为 old 是 Informer 缓存里的副本，改了也无所谓。

handler 侧的调用：

[internal/k8s/handlers.go:45-52](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L45-L52) —— Ingress 的 UpdateFunc：`if hasChanges(o, c) { AddSyncQueue(c) }`。

**只比 Spec 的策略**（Policy / VirtualServer）：

[internal/k8s/policy.go:39-46](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L39-L46) —— Policy UpdateFunc：`reflect.DeepEqual(oldPol.Spec, curPol.Spec)`，只比 spec。

[internal/k8s/handlers.go:158-161](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L158-L161) —— VirtualServer UpdateFunc：同样只比 `oldVs.Spec` vs `curVs.Spec`。

**VirtualServerRoute 的额外维度**——VSR 除了 Spec，还比 Labels：

[internal/k8s/handlers.go:218-221](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L218-L221) —— VSR UpdateFunc 比较 `Spec` **和** `Labels`。这是因为宿主 VirtualServer 可以「按标签选择器」引用 VSR（见 u2-l1），所以 VSR 的标签变化会影响它是否被装配，必须入队。

**整个对象直接比较**（最宽松档）：

[internal/k8s/handlers.go:98-101](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L98-L101) —— Secret UpdateFunc：`reflect.DeepEqual(old, cur)` 比较整个 Secret。Secret 没有「频繁变动的无关字段」问题（证书内容变了就是要 reload），所以可以整对象比。

> 关于 VirtualServer 的 `weightChangesDynamicReload` 快路径：handlers.go:134-156 里有一段「把新旧 VS 深拷贝后、把 splits 权重清零、再比 Spec」的逻辑——如果**只有流量切分权重变了**（其余 Spec 没变），就走 `processVSWeightChangesDynamicReload` 的免 reload 动态更新路径（仅 NGINX Plus 可用），而不入队触发完整 reload。这是变更检测「更精细」的进阶玩法，理解主线时可先跳过。

#### 4.2.4 代码实践

**实践目标**：亲手验证「归一化」如何避免无效入队。

**操作步骤**：

1. 阅读 [internal/k8s/utils.go:145-150](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/utils.go#L145-L150) 的 `hasChanges`。
2. 做一个**思想实验**（无需运行）：假设控制器刚把外部 LB 地址写进了某 Ingress 的 `Status.LoadBalancer.Ingress`，于是 Informer 触发 `UpdateFunc(old, cur)`。此时 `old.Status` 是旧值、`cur.Status` 是新值，两者不同。
3. 追踪 `hasChanges` 的执行：第 147 行把 `old.Status.LoadBalancer.Ingress` 改成 current 的值，第 148 行把 `old.ResourceVersion` 也改成 current。此时 old 与 cur 在这两个字段上已经相等。
4. 第 149 行 `reflect.DeepEqual(old, current)`：由于除被抹平的字段外没有别的差异，返回 `true` 的否定即 `false`——**不入队**。

**需要观察的现象**：如果没有第 147-148 行的归一化，`DeepEqual` 会因为 status 不同而返回 false，`hasChanges` 返回 true，于是每次 status 回写都会触发一次入队 → reload。

**预期结果**：你能解释「删掉 hasChanges 里的两行归一化会导致 status 回写引发 reload 风暴」。如果想在本地坐实，可仿照 `internal/k8s/handlers_test.go` 的表驱动写法，构造 old/cur 两个 Ingress（仅 Status 不同）断言 `hasChanges` 返回 false——但当前仓库未直接测 `hasChanges`，此为「**待本地验证**」的扩展练习。

#### 4.2.5 小练习与答案

**练习 1**：Policy 的 UpdateFunc 只比 `Spec`，不比 `Status` 和 `Metadata`。如果有人给 Policy 加了一个会改变行为的 annotation（比如未来某个开关），这套检测会不会漏掉？

**参考答案**：会。`reflect.DeepEqual(oldPol.Spec, curPol.Spec)` 只看 Spec，annotation 属于 `Metadata.Annotations`，不在比较范围。这正是设计取舍：当前 Policy 的所有行为都编码在 Spec 里，所以只比 Spec 足够且高效；若将来引入「注解控制行为」，就必须像 VSR 那样把 Labels（或相应 Annotations）也加入比较，否则会出现改了注解却不 reload 的 bug。

**练习 2**：Secret 的 UpdateFunc 直接 `reflect.DeepEqual(old, cur)` 比较整个对象，会不会因为 `ResourceVersion` 变化而误触发？

**参考答案**：会触发入队，但**不算误触发**。Secret 的内容（证书/密钥/JWK）只要真的变了就应当 reload；而 `ResourceVersion` 只在对象被写入 etcd 时才递增——它变了必然意味着 Secret 被改写过。对 Secret 而言「被改写」本身就是有意义的信号（即使最终内容碰巧相同，多一次 reload 也是可接受的安全冗余）。这与 Ingress 不同：Ingress 的 status 回写是控制器自己制造的、高频的、与路由无关的变动，才必须归一化。

---

### 4.3 删除与 tombstone：DeletedFinalStateUnknown 防御

#### 4.3.1 概念说明

AddFunc 和 UpdateFunc 拿到的 `obj` 类型是确定的（就是你注册 Informer 时指定的那种资源）。但 **DeleteFunc 不一样**。

Informer 靠 watch 长连接接收删除事件。如果某次 watch 连接断开（网络抖动、apiserver 重启、超时），client-go 会重连并**用本地缓存兜底**：它把断连期间可能遗漏的删除事件，从本地缓存里补发出去。这种「补发的删除」无法保证能拿到完整的原始对象，于是 client-go 把它包成一个 `cache.DeletedFinalStateUnknown` 结构（俗称 **tombstone，墓碑**）：

```go
type DeletedFinalStateUnknown struct {
    Key string
    Obj interface{}   // 真正被删的对象，藏在里面
}
```

所以 DeleteFunc 收到的 `obj` **可能是两种形态**：

1. 正常形态：`obj.(*T)` 直接断言成功。
2. 墓碑形态：`obj` 其实是 `*cache.DeletedFinalStateUnknown`，要先取出 `.Obj` 再断言成 `*T`。

如果不处理墓碑形态，一旦发生 watch 重连补发，DeleteFunc 会类型断言失败、直接 panic 或漏处理删除——资源在缓存里删了，但 NGINX 配置里还留着它的路由。因此**墓碑处理是 DeleteFunc 的强制性防御代码**。

#### 4.3.2 核心流程

NIC 所有 DeleteFunc 共用同一个三段式防御骨架：

```
DeleteFunc(obj):
    x, ok = obj.(*T)              # 第 1 段：尝试正常断言
    if !ok:                       # 断言失败 → 可能是墓碑
        deletedState, ok = obj.(cache.DeletedFinalStateUnknown)   # 第 2 段：尝试当墓碑解包
        if !ok:                   # 连墓碑都不是 → 真的是脏数据
            log "received unexpected object"
            return                # 丢弃，不能 panic
        x, ok = deletedState.Obj.(*T)   # 从墓碑里取真实对象
        if !ok:                   # 墓碑里装的也不是 T
            log "DeletedFinalStateUnknown contained non-T object"
            return
    log "Removing T"
    lbc.AddSyncQueue(x)           # 第 3 段：入队
```

核心思想：**两道类型断言 + 两道静默丢弃**。任何一道失败都只记 debug 日志然后 `return`，绝不 panic、绝不把脏数据塞进队列。

#### 4.3.3 源码精读

以 Ingress 的 DeleteFunc 为标准样本：

[internal/k8s/handlers.go:28-44](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers.go#L28-L44) —— 第 29 行先 `obj.(*networking.Ingress)`；失败则第 31 行检查 `cache.DeletedFinalStateUnknown`；再失败或墓碑内容非 Ingress，都 debug 日志后 return。只有拿到合法 Ingress 才 `AddSyncQueue(obj)`。

> 注意一个细节：这里入队的是原始的 `obj`（可能是墓碑），而不是解包后的 `ingress`。这没问题——因为下游 `AddSyncQueue → Enqueue → newTask` 会用 Key（namespace/name）来标识 task，而墓碑对象也携带了 Key 信息（client-go 保证）。对其它资源（如 service.go:33-50）则入队解包后的对象，两种写法都可行，关键是 task 能拿到正确的 Key。

这个三段式骨架在**每一个** DeleteFunc 里都重复出现——Service、Secret、VirtualServer、VSR、TransportServer、Policy、EndpointSlice 全部照抄。这是有意为之的「防御性冗余」：宁可代码重复，也要确保每条删除路径都不会因墓碑而崩。

对照 Service 的版本：

[internal/k8s/service.go:33-50](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L33-L50) —— 与 Ingress 版本逐行对应，只是类型换成 `*v1.Service`、日志级别用 `Infof` 而非 `Debugf`（Service 删除被认为更重要）。

#### 4.3.4 代码实践

**实践目标**：通过阅读测试，理解「脏数据」如何被安全丢弃。

**操作步骤**：

1. 打开 [internal/k8s/handlers_test.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/handlers_test.go)，阅读 `TestAreResourcesDifferent`（这是 handlers.go 里另一个变更检测函数 `areResourcesDifferent` 的表驱动测试，用于 dynamic 资源）。
2. 注意测试用例覆盖了「spec 字段类型错误」「spec 缺失」「spec 相等」「spec 不等」等边界——体会 handler 层对「异常输入」的防御心态与 DeleteFunc 的墓碑防御一脉相承。
3. 在五个 DeleteFunc（handlers.go / service.go / policy.go / transport_server.go / endpoint_slice.go）里数一数 `DeletedFinalStateUnknown` 出现的次数，确认每个资源都有这道防线。

**需要观察的现象**：每个 DeleteFunc 都有完全平行结构的「双重断言 + 双重 return」。

**预期结果**：你能说出「tombstone 防御是 NIC handler 的强制不变量；新增任何资源的 DeleteFunc 都必须照抄这套骨架，否则在 watch 重连时可能漏处理删除」。

#### 4.3.5 小练习与答案

**练习 1**：为什么墓碑处理失败时要 `return`（静默丢弃），而不是返回一个错误或触发重试？

**参考答案**：因为 handler 层拿到的已经是「兜底的兜底」——watch 断连、本地缓存补发、还遇到了类型不符。此时没有任何可靠手段能恢复这个对象。丢弃并记日志是最安全的选择：如果真的漏了删除，下一次该资源相关的任何变更（或 resync 兜底重触发）会重新进入处理流程；而强行处理一个类型不明的脏数据，反而可能把错误对象塞进配置生成链路，造成更难排查的损坏。这体现了「宁可少处理一次（后续会补），不可处理错一次」的防御哲学。

**练习 2**：假设你新增了一种 CRD 资源 `Foo`，照抄了 Ingress 的 DeleteFunc 骨架，但忘了写墓碑那一段。会发生什么？

**参考答案**：在正常运行（watch 不断连）时**完全无感**——删除事件以正常形态 `*Foo` 到达，直接断言成功。只有当 watch 断开重连、且恰好有遗漏的 Foo 删除需要从本地缓存补发时，`obj` 才会是 `DeletedFinalStateUnknown`，此时 `obj.(*conf_v1.Foo)` 断言失败，`ok` 为 false，由于没有兜底分支，代码会跳过入队——这个 Foo 的删除被悄悄吞掉，NGINX 里残留它的路由，直到下一次 resync 或相关变更才可能纠正。所以墓碑防御是「平时用不上、出事才救命」的代码。

---

### 4.4 Service 与 Policy 的专用处理

通用模板虽好，但 Service 和 Policy 各有一个「**主动打破模板**」的理由。理解这两个例外，能帮你抓住 handler 设计的本质：**模板服务于通用情况，特殊情况就该特殊处理**。

#### 4.4.1 Service：为什么 UpdateFunc 不能只比 Spec

##### 概念说明

Service 是 NIC 监听里最「反常」的资源。它的 UpdateFunc 既不是「比整个对象」也不是「只比 Spec」，而是**只比几个特定字段**：端口列表、ExternalName、是否 headless。

原因写在 service.go 的注释里：**Kubernetes 在 Service 的 port 字段变化时，不会更新对应的 EndpointSlice**。而 NIC 同时也在监听 EndpointSlice（u3-l2 提到 EndpointSlice handler 是端口/端点变更的主信号源）。这意味着「改了 Service port」这个事件，EndpointSlice handler 收不到，只有 Service handler 自己能感知——所以 Service handler 必须**亲自**检查 port 变化。

此外，Service 还可能承担「外部状态服务」的角色（其 LoadBalancer 外部地址会被写进所有 Ingress/VS 的 status，见 u3-l7），这条路径在 UpdateFunc 里被优先拦截。

##### 核心流程

```
Service.UpdateFunc(old, cur):
    if reflect.DeepEqual(old, cur):        # 先粗筛：完全没变就跳过
        return
    curSvc = cur.(*Service)
    if IsExternalServiceForStatus(curSvc): # 情形 A：这是外部状态服务
        AddSyncQueue(curSvc)               #   无论如何都要入队（要刷 status）
        return
    if hasServicedChanged(oldSvc, curSvc): # 情形 B：port/externalName/headless 变了
        AddSyncQueue(curSvc)
    # 否则：只是些无关字段（如 label）变了，丢弃
```

`hasServicedChanged` 是定制检测的总入口，内部组合三条规则：

```
hasServicedChanged(old, cur) =
      hasServicePortChanges(old.Ports, cur.Ports)        # 端口名/端口号变了
   || hasServiceExternalNameChanges(old, cur)            # ExternalName 型服务的域名变了
   || isHeadless(old) || isHeadless(cur)                 # 任一方是 headless
```

其中 `hasServicePortChanges` 有个细节：**比较前先对两端端口排序**（用 `portSort`），这样即使端口顺序不同但内容相同，也判为「没变」。

##### 源码精读

[internal/k8s/service.go:25-66](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L25-L66) —— `createServiceHandlers`。注意 UpdateFunc 的三层判断：先 `DeepEqual` 粗筛（52 行），再判外部状态服务（54-57 行），最后 `hasServicedChanged`（59 行）。文件顶部 17-24 行的注释明确解释了「为何要自己 catch port 变化」。

[internal/k8s/service.go:73-87](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L73-L87) —— `hasServicedChanged`：组合三条规则，任一为真即返回 true。注意 headless 服务（`clusterIP == "None"`）只要任一方是 headless 就一律认为变了——因为 headless 服务的端点解析方式特殊，保守起见全量同步。

[internal/k8s/service.go:95-110](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L95-L110) —— `hasServicePortChanges`：先比长度，再各自 `sort.Sort(portSort(...))` 排序，最后逐项比 `Port` 和 `Name`。配套的 `portSort`（112-127 行）实现 `sort.Interface`，按 Name 为主键、Port 为次键排序。

Service 入队后会被 `syncService` 处理（service.go:206 起），它既更新外部状态服务带来的所有资源 status，又通过 `FindResourcesForService` 找到引用该 Service 的所有 Ingress/VS 并重新生成配置——但这已属于 sync 层（u3-l5），不在本讲范围。

#### 4.4.2 Policy：handler 朴素，sync 复杂

##### 概念说明

Policy 的 handler 本身**完全遵循通用模板**（Add/Delete/Update 三件套，UpdateFunc 只比 Spec），并没有 Service 那种「打破模板」的需要。本模块把它和 Service 并列，是为了形成一个重要对比：

> **handler 的简单与否，和 sync 的复杂与否，是两回事。**

Policy 的 handler 极简（4.2.3 已展示其 UpdateFunc），但它的 `syncPolicy`（policy.go:61 起）极其丰富：校验 Policy → 回写 status（含启动期延后刷新）→ 维护 ExternalAuth 服务引用 → 找到所有引用该 Policy 的资源 → 检查策略是否被 Ingress 支持 → 重新生成这些资源的配置。

这条对比印证了本讲开篇的核心论点：**handler 的职责是「过滤 + 入队」，越轻越好；真正的业务复杂度全部推迟到 sync 层**。把复杂度留在 sync 而不是 handler，还有一个好处：sync 是单 worker 串行的（u3-l3），复杂逻辑放这里天然避免并发问题；而 handler 是被 Informer 多个 goroutine 回调的，越简单越安全。

##### 源码精读

[internal/k8s/policy.go:15-48](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L15-L48) —— `createPolicyHandlers`：标准三件套，DeleteFunc 带墓碑防御，UpdateFunc 只比 `Spec`。与 TransportServer 模板几乎一致。

对照同一文件的 `syncPolicy`（[internal/k8s/policy.go:61-231](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L61-L231)）：体量大、分支多，做了校验、status、引用追踪、受影响资源重生成。这种「轻 handler + 重 sync」的分工是 NIC 一以贯之的风格。

#### 4.4.3 代码实践（Service + Policy 合并）

**实践目标**：亲手列出 Service UpdateFunc 会调用 `AddSyncQueue` 的全部触发条件——这是本讲的核心练习任务。

**操作步骤**：

1. 打开 [internal/k8s/service.go:51-64](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L51-L64)。
2. 在一张纸上列出 UpdateFunc 调用 `AddSyncQueue(curSvc)` 的**所有**触发分支。
3. 用 `internal/k8s/service_test.go` 里的 `TestHasServicePortChanges`（[service_test.go:10 起](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service_test.go#L10)）验证你对「端口变化」判断的理解：运行 `make test`（或 `go test -run TestHasServicePortChanges ./internal/k8s/`）应全部通过。

**需要观察的现象 / 预期结果**：Service UpdateFunc 入队共有如下触发条件（完整清单）：

| # | 触发条件 | 代码位置 | 为什么入队 |
| --- | --- | --- | --- |
| 1 | `reflect.DeepEqual(old, cur)` 为 false **且** `IsExternalServiceForStatus(curSvc)` 为 true | service.go:52-57 | 这是外部状态服务，任何变动都要刷新所有资源的 status |
| 2 | `hasServicePortChanges` 为 true（端口数量、端口号或端口名变化） | service.go:59, 95-110 | K8s 改 Service port 时不更新 EndpointSlice，只有 Service handler 能感知 |
| 3 | `hasServiceExternalNameChanges` 为 true（ExternalName 型服务的域名变化） | service.go:59, 90-92 | ExternalName 服务的上游地址直接来自此字段 |
| 4 | `isHeadless(old)` 或 `isHeadless(cur)` 为 true（任一方 clusterIP=="None"） | service.go:59, 82-84, 69-71 | headless 服务端点解析特殊，保守起见全量同步 |

若 `DeepEqual` 判定完全相同（条件 0），则直接跳过不入队。

> 注意条件 1 是 `return` 提前返回的，条件 2-4 是 `hasServicedChanged` 内部「或」关系——只要任一为真，整条 UpdateFunc 就会入队一次。

#### 4.4.4 小练习与答案

**练习 1**：Service 的 UpdateFunc 先用 `reflect.DeepEqual(old, cur)` 粗筛，完全相同就 return。既然后面还有 `hasServicedChanged` 的精细判断，这个粗筛是不是多余的？

**参考答案**：不多余，是性能优化。`hasServicedChanged` 内部要对端口排序（`sort.Sort`）并逐项比较，开销大于一次整体 `DeepEqual`。绝大多数 Service 更新事件其实什么关键字段都没变（只是 label/annotation 抖动），粗筛能以最低成本把它们挡在门外，避免无谓的排序。这是「快速路径 + 慢速路径」的经典分层。

**练习 2**：Policy 的 handler 极简，但 `syncPolicy` 极复杂。如果有人把一部分 sync 逻辑（比如「找到引用该 Policy 的资源」）上移到 UpdateFunc 里提前算好，会有什么坏处？

**参考答案**：三方面坏处。(1) **并发安全**：handler 被 Informer 的回调 goroutine 调用，而 `lbc.configuration`（内存模型）是共享状态，在 handler 里读写它需要加锁，复杂且易错；sync 是单 worker 串行，天然无并发问题。(2) **重复计算**：handler 里算了结果也无处可存（task 只存 Key），sync 时还得重算，白白浪费。(3) **违背分层**：handler 的职责被定义为「过滤 + 入队」，混入业务逻辑会让 handler 变重、难测、难复用。NIC 选择「轻 handler + 重 sync」正是为了规避这些问题。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个**源码阅读型综合任务**。

**任务**：假设产品决定新增一种 CRD 资源 `Widget`（`k8s.nginx.org/v1`），需要被 NIC 监听并参与配置生成。请基于本讲学到的模式，**设计**（无需实现）它的 event handler，并说明每一处设计决策的依据。

**要求产出**：

1. **写出 `createWidgetHandlers(lbc)` 的伪代码骨架**，必须包含：
   - AddFunc：断言 `*conf_v1.Widget` → log → 入队。
   - DeleteFunc：完整的墓碑三段式防御（双重断言 + 双重 return）。
   - UpdateFunc：一个变更检测策略，并说明你选「比整个对象 / 只比 Spec / 归一化后比较 / 定制字段」中的哪一档，**为什么**。

2. **回答两个设计问题**：
   - 如果 `Widget` 像 Ingress 一样会被控制器回写 status，你的 UpdateFunc 需要做哪种归一化？参考 `hasChanges`（[utils.go:145-150](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/utils.go#L145-L150)）说明。
   - 如果 `Widget` 像 Service 一样「K8s 改它的某个字段时不会联动更新另一个被监听资源」，你需要什么样的定制检测函数？参考 `hasServicedChanged`（[service.go:73-87](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L73-L87)）说明。

3. **装配清单**：列出要让 `Widget` handler 真正生效，需要在 `controller.go` 的哪个函数里加哪一行（参考 [controller.go:614-625](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L614-L625) 的 CRD 装配模式），以及对应的 `addWidgetHandler`（参考 [policy.go:50-59](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L50-L59) 的写法）应当长什么样。

**参考思路**（先自己想，再对照）：

- UpdateFunc 默认选「只比 Spec」（`reflect.DeepEqual(oldWidget.Spec, curWidget.Spec)`），因为 CRD 的行为通常全部编码在 Spec 里，status 与 metadata 的抖动不应触发 reload——这与 Policy/VS 一致。
- 若 Widget 会被回写 status，则要么继续只比 Spec（天然隔离 status），要么若必须整对象比，就得像 `hasChanges` 那样在比较前抹平 `Status` 与 `ResourceVersion`。
- 若 Widget 存在「K8s 不联动更新」的字段，就照搬 `hasServicedChanged` 的套路：写一个 `hasWidgetChanged(old, cur)` 只检查那些关键字段。
- 装配：在 `addCustomResourceHandlers`（CRD 装配处）加 `nsi.addWidgetHandler(createWidgetHandlers(lbc))`；`addWidgetHandler` 仿照 `addPolicyHandler`，从 `confSharedInformerFactory.K8s().V1().Widgets()` 取 Informer，挂 handler，取 Lister，登记 `HasSynced`。

> 这个练习覆盖了本讲全部四个最小模块：统一模式（4.1）、变更检测（4.2）、墓碑防御（4.3）、专用处理取舍（4.4）。完成它意味着你已经具备为 NIC 新增一种被监听资源的 handler 设计能力。

---

## 6. 本讲小结

- **handler 是过滤阀**：`createXHandlers` 工厂返回 `ResourceEventHandlerFuncs` 三件套，唯一职责是把「值得处理」的对象通过 `lbc.AddSyncQueue` 入队；它不做配置生成、不写 status、不 reload——那是 sync 层的事。
- **变更检测是性能关键**：因为 taskQueue 无指数退避、无最大重试（u3-l3），UpdateFunc 必须在入队前丢弃「无关变动」。NIC 给出了四档策略：定制字段（Service）/ 归一化后比整个对象（Ingress）/ 只比 Spec（Policy、VS）/ 直接比整个对象（TS、EndpointSlice）。
- **Ingress 的 `hasChanges` 必须归一化**：比较前抹平 `Status.LoadBalancer.Ingress` 和 `ResourceVersion`，否则控制器自己回写的 status 会引发「reload 风暴」。
- **墓碑防御是 DeleteFunc 的强制不变量**：每个 DeleteFunc 都有「双重类型断言 + 双重静默 return」的 `DeletedFinalStateUnknown` 处理骨架，防止 watch 重连补发删除事件时漏处理或 panic。
- **Service 主动打破模板**：因为「K8s 改 Service port 时不更新 EndpointSlice」，Service handler 必须自己检查 port/externalName/headless 变化（`hasServicedChanged`），且优先拦截外部状态服务。
- **轻 handler + 重 sync**：Policy 的 handler 极简而 sync 极复杂，印证了 NIC 把业务复杂度全部推迟到单 worker 串行的 sync 层的设计取向，既安全又可维护。

---

## 7. 下一步学习建议

本讲把「事件如何从 Informer 到达队列」讲透了，但入队只是开始——**task 被 worker 取出后，由谁处理、怎么处理**？这就是下一讲 u3-l5（`sync` 调度器与各资源 sync 函数）的主题。

建议下一步：

1. **阅读 u3-l5**：看 `controller.go` 里的 `sync(task)` 如何按 `task.Kind` 用 switch 分发到 `syncIngress / syncVirtualServer / syncService / syncConfigMap` 等函数，以及各 sync 函数「取资源 → 更新内存模型 → 处理变更」的三段式通用模式。本讲提到的 `syncService`、`syncPolicy` 就在那里被详细展开。
2. **顺带预习 u3-l6**：handler 入队后，sync 会读写 `configuration` 这个内存模型（如 `syncPolicy` 里的 `FindResourcesForPolicy`、`UpdatePolicyServiceRef`）。u3-l6 讲清这个内存模型如何索引资源、检测主机冲突、校验引用。
3. **源码阅读建议**：以本讲列出的「Service UpdateFunc 触发条件表」为起点，跟踪一次 Service port 变更的完整旅程：`UpdateFunc → AddSyncQueue → syncQueue worker → sync → syncService → FindResourcesForService → AddOrUpdateResources → Configurator → reload`。这条链路横跨 u3-l3、u3-l4、u3-l5、u4、u5 五个单元，走通它就建立了 NIC 的全局心智模型。
