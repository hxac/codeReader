# Leader 选举与 Status 回写

## 1. 本讲目标

本讲承接 u3-l1 的控制器生命周期，回答两个紧随其后的工程问题：

1. 当 Ingress Controller 部署成多个副本（replicas）时，**谁有资格把状态写回 Kubernetes**？如果每个副本都写，会发生什么？
2. 写回状态这一动作本身是一个对 API Server 的写请求，它**何时做、怎么做、失败了如何重试**，才不会拖慢控制器启动、又不丢状态？

学完本讲，你应当能够：

- 说清 client-go `LeaseLock` 选举的三段时间参数（LeaseDuration / RenewDeadline / RetryPeriod）的含义。
- 解释 `OnStartedLeading` 回调为什么是「新 leader 初始化状态」的天然时机。
- 掌握 `statusUpdater` 写回 Ingress / VS / VSR / TS / Policy 状态的统一三段式模式。
- 理解启动期为何用 `pendingStatus` 切片延后状态写回，以及 `flushPendingStatusesAsync` 如何在 pod 标记就绪后再并行刷写。
- 在源码中找到「判断当前 pod 是否为 leader」的所有位置。

## 2. 前置知识

### 什么是资源的 status

Kubernetes 里很多资源都有 `spec`（用户期望）和 `status`（控制器报告的实际状态）两个段落。对于 NIC 管理的路由资源，status 是控制器**写回**给用户看的反馈：

- **Ingress** 的 `.status.loadBalancer.ingress`：对外可达的 IP / 主机名，告诉用户「这个 Ingress 已经在哪个地址上对外服务了」。
- **VirtualServer / VirtualServerRoute / TransportServer / Policy** 的 `.status`：包含 `state`（`Valid` / `Invalid` / `Warning`）、`reason`、`message`，以及 `externalEndpoints`（CR 专属，含 IP / Hostname / Ports）。

state 三态常量定义在：

[types.go:L9-L14](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L9-L14) —— 三个状态常量，决定写回哪一档。

VirtualServerStatus 的结构（VSR / TS 的 status 同构）：

[types.go:L465-L478](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L465-L478) —— state / reason / message / externalEndpoints 四个字段。

### 什么是 leader 选举

多个副本同时运行时，状态写回天然存在竞争：如果所有副本都把自己的「我看到的对外 IP」写进同一个 Ingress，会产生「写放大」和「竞态抖动」（A 写了 IP，B 觉得不对又清空）。解决思路是 **leader election**：副本之间抢一把「锁」，只有抢到锁（成为 leader）的那一个副本才负责写状态，其余副本（follower）只负责转发流量、生成 NGINX 配置，但不写 status。

client-go 提供了 `leaderelection` 包，用 K8s 的 `coordination.k8s.io/Lease` 资源做这把锁（早期实现用 ConfigMap，NIC 的 flag 说明文字里仍保留了「ConfigMap」的字样，但代码里用的是 `LeaseLock`）。

### status 写回为什么是个「性能隐患」

写 status = 对 API Server 发一次 `UpdateStatus` 请求。在控制器启动时，如果把内存模型里成百上千个资源逐个串行写 status，会阻塞启动数分钟。因此 NIC 把「写 status」做成了**可延后、可批处理、可并行**的操作。这是本讲后半部分（启动期延后刷新）的核心动机。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/k8s/leader.go` | 构造 `LeaseLock`、定义 `OnStartedLeading` / `OnStoppedLeading` 回调，是选举的全部代码。 |
| `internal/k8s/status.go` | `statusUpdater` 结构体与所有「写回某类资源 status」的方法，含重试逻辑。 |
| `internal/k8s/controller.go` | 控制器主体：保存选举结果、启动选举 goroutine、判断是否 leader、启动期延后刷新的全部编排。 |
| `cmd/nginx-ingress/flags.go` | `-enable-leader-election`、`-leader-election-lock-name`、`-report-ingress-status` 等 flag 定义。 |
| `pkg/apis/configuration/v1/types.go` | status 结构与 state 常量的真相源。 |

## 4. 核心概念与源码讲解

### 4.1 LeaseLock 选举

#### 4.1.1 概念说明

选举要解决的问题是「N 个副本里选且仅选 1 个 leader」。NIC 直接复用 client-go 的 `leaderelection` 机制，核心是一把 `LeaseLock`（基于 Lease 资源）：

- 每个 controller pod 有一个唯一身份（`POD_NAME` 环境变量）。
- 想当 leader 的 pod 周期性地「续约」这把 Lease；只要在租期内不断续，它就一直是 leader。
- 如果 leader 崩溃或网络分区导致续约超时，租约过期，其他 pod 抢占成为新 leader。

#### 4.1.2 核心流程

选举依赖三个时间参数，理解它们的相对大小是关键。设 TTL（租期）为 \(T\)，NIC 取：

\[
\text{LeaseDuration} = T, \quad \text{RenewDeadline} = \frac{T}{2}, \quad \text{RetryPeriod} = \frac{T}{4}
\]

其中 NIC 把 \(T\) 设为 30 秒，因此：

| 参数 | 值 | 含义 |
| --- | --- | --- |
| LeaseDuration | 30s | 租期上限。leader 超过这么久没续约，租约作废，他人可抢。 |
| RenewDeadline | 15s | leader 续约的截止线。在此之内必须续约成功一次，否则主动放弃 leader 身份。 |
| RetryPeriod | 7.5s | 续约 / 抢占的重试间隔。 |

它们必须满足 `RenewDeadline < LeaseDuration`，否则 leader 永远续不上约；`RetryPeriod` 要远小于 `RenewDeadline`，保证续约有足够重试机会。这是 client-go 强制的不变式。

选举循环的伪代码（client-go 内部行为）：

```text
loop:
    try acquire / renew lease   # 每 RetryPeriod 一次
    if 成功且 not leading:
        leading = true
        OnStartedLeading(ctx)   # 回调：新 leader 上任
    if 失败且 leading:
        leading = false
        OnStoppedLeading()      # 回调：失去 leader
    sleep RetryPeriod
```

#### 4.1.3 源码精读

选举对象在 [leader.go:L22-L58](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L22-L58) 中构造，关键部分：

```go
podName := os.Getenv("POD_NAME")
// ...
lc := resourcelock.ResourceLockConfig{
    Identity:      podName,        // 身份 = pod 名
    EventRecorder: recorder,
}
lock := &resourcelock.LeaseLock{
    LeaseMeta:  leaseMeta,         // namespace + lockName
    Client:     client.CoordinationV1(),
    LockConfig: lc,
}
ttl := 30 * time.Second
return leaderelection.NewLeaderElector(
    leaderelection.LeaderElectionConfig{
        Lock:          lock,
        LeaseDuration: ttl,
        RenewDeadline: ttl / 2,
        RetryPeriod:   ttl / 4,
        Callbacks:     callbacks,
    },
)
```

读法要点：

- `Identity` 用 pod 名（来自 `POD_NAME` 环境变量），它会被写进 Lease 的 `holderIdentity` 字段，谁的名字在里面谁就是当前 leader。
- `LeaseLock` 用 `client.CoordinationV1()`（即 `coordination.k8s.io/v1` 的 Lease），锁对象的名字由 flag `-leader-election-lock-name` 决定，默认 `nginx-ingress-leader-election`，所在命名空间是 controller 自己的命名空间。
- 三个时间参数严格按上面的公式取值。

flag 定义见 [flags.go:L130-L134](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/flags.go#L130-L134) —— 默认开启选举（`-enable-leader-election` 默认 `true`），锁名默认 `nginx-ingress-leader-election`。

`addLeaderHandler` 把选举器挂到控制器上（[leader.go:L110-L117](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L110-L117)），但它**只是构造对象、并不启动**。真正的启动发生在 `Run()` 里（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：理解锁对象的物理形态，找到 leader 的「名字」存在集群里的哪里。

**操作步骤**：

1. 阅读 [leader.go:L22-L58](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L22-L58)，确认锁类型是 `LeaseLock`。
2. 如果你有一个运行中的多副本 NIC 集群，执行（替换命名空间）：

   ```bash
   kubectl get lease nginx-ingress-leader-election -n <nic-namespace> -o yaml
   ```

**需要观察的现象**：输出里的 `.spec.holderIdentity` 字段值就是当前 leader 的 `POD_NAME`；`.spec.renewTime` / `.spec.leaseDurationSeconds` 反映续约状态。

**预期结果**：`holderIdentity` 恰好等于某一个 NIC pod 的名字。手动删除该 leader pod 后，短时间内 `holderIdentity` 会切换到另一个 pod。

> 待本地验证：在没有运行中集群时，此步无法执行，可改为阅读 Lease 的 API 定义理解字段。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `LeaseDuration` 改得比 `RenewDeadline` 还小，会发生什么？

**答案**：这违反了 client-go 的不变式（构造时会 panic 报错 `RenewDeadline must be less than LeaseDuration`）。即使不报错，逻辑上 leader 永远来不及在租期过期前续约，会不断「丢 leader 又抢回来」，导致状态写回抖动。

**练习 2**：为什么 `Identity` 用 pod 名而不是随机 UUID？

**答案**：pod 名可读、可观测，排障时能直接从 Lease 的 `holderIdentity` 看出当前 leader 是哪个 pod；同时 pod 名在同一集群内足够唯一。

---

### 4.2 选举回调与状态同步

#### 4.2.1 概念说明

`LeaderCallbacks` 有两个回调：

- `OnStartedLeading(ctx)`：pod 刚成为 leader 时触发一次。这是「新 leader 初始化对外状态」的天然时机——因为上一个 leader 可能刚崩溃，集群里很多资源的 status 可能是旧的，新 leader 上任应当尽快把所有受管资源的状态刷新一遍。
- `OnStoppedLeading()`：pod 失去 leader 身份时触发。NIC 在此只打了一条日志（不再写任何状态），因为失去 leader 后本副本本来就不该再写状态了。

注意一个微妙点：`OnStartedLeading` 收到的 `ctx` 在 leader 期间一直有效，一旦失去 leader 会被取消。NIC 用 `close(lbc.telemetryChan)` 这个动作来通知遥测模块「现在是 leader 了，可以开始上报」（见 u7-l3 遥测讲义）。

#### 4.2.2 核心流程

`OnStartedLeading` 的工作流（[leader.go:L63-L103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L63-L103)）：

```text
OnStartedLeading(ctx):
    1. 关闭 telemetryChan  →  通知遥测模块开始上报
    2. if reportIngressStatus:
           取出全部 Ingress，批量写回外部端点（LB IP）
    3. if areCustomResourcesEnabled:
           从 Event 重建 VS 状态
           从 Event 重建 VSR 状态
           重新校验并写回所有 Policy 状态
           从 Event 重建 TS 状态
```

「从 Event 重建状态」是个值得注意的设计：VS/VSR/TS 的 `state/reason/message` 是控制器自己通过 `EventRecorder` 记录的 K8s Event。新 leader 上任时，它**去查每个资源最近的 Event**，把最近一条 Event 的 reason/message 翻译成 state 写回（`getStatusFromEventTitle` 做这个翻译，见 [controller.go:L2448-L2459](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2448-L2459)）。这样即使新 leader 内存里没有「历史」，也能从 Event 这一持久化来源恢复出资源的最终状态。

#### 4.2.3 源码精读

回调构造在 [leader.go:L60-L108](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L60-L108)。Ingress 部分的刷新：

```go
if lbc.reportIngressStatus {
    ingresses := lbc.configuration.GetResourcesWithFilter(resourceFilter{Ingresses: true})
    err := lbc.statusUpdater.UpdateExternalEndpointsForResources(ingresses)
    // ...
}
```

CR 部分调用 `updateVirtualServersStatusFromEvents()`，它遍历所有命名空间的 VS，查最近 Event 并写回（[controller.go:L2461-L2503](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2461-L2503)）：

```go
for _, obj := range nsi.virtualServerLister.List() {
    vs := obj.(*conf_v1.VirtualServer)
    // 查该 VS 的全部 Event，取最新一条
    events, _ := lbc.client.CoreV1().Events(vs.Namespace).List(...)
    // 翻译 reason → state 并写回
    lbc.statusUpdater.UpdateVirtualServerStatus(vs, getStatusFromEventTitle(...), ...)
}
```

Policy 的刷新走另一条路——它不是从 Event 恢复，而是**当场重新校验**每个 Policy（`updatePoliciesStatus`，[leader.go:L119-L147](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L119-L147)）：对每个 Policy 调 `validation.ValidatePolicy`，通过则写 `Valid`，否则写 `Invalid`。

选举器的启动时机在 `Run()`（[controller.go:L765-L767](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L765-L767)）：

```go
if lbc.leaderElector != nil {
    go lbc.leaderElector.Run(lbc.ctx)
}
```

它作为一个**独立 goroutine** 与 syncQueue 并行运行，靠 `lbc.ctx` 的取消来随控制器一起退出。

#### 4.2.4 代码实践

**实践目标**：理解 `OnStartedLeading` 是 leader 切换时的「状态恢复点」。

**操作步骤**：

1. 阅读 [leader.go:L63-L103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L63-L103)，列出 `OnStartedLeading` 刷新了哪几类资源。
2. 阅读 [controller.go:L2448-L2459](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2448-L2459) 的 `getStatusFromEventTitle`，理解 Event reason 与 state 的映射。

**需要观察的现象（源码阅读型）**：注意 VS/VSR/TS 用「查 Event」恢复，而 Policy 用「重新校验」恢复——两种恢复策略的区别。

**预期结果**：你能说清「为什么 Policy 不查 Event」——因为 Policy 的 Valid/Invalid 完全由其 spec 是否通过校验决定，重算一次即可，不需要历史 Event。

#### 4.2.5 小练习与答案

**练习 1**：`OnStoppedLeading` 为什么几乎什么都不做？

**答案**：失去 leader 后，本副本通过 `reportStatusEnabled()` / `reportCustomResourceStatusEnabled()` 这两个门禁会被拦下（见 4.4），本来就不会再写状态；所以 `OnStoppedLeading` 只需打日志。真正的「停写」由门禁函数保证，而不是在回调里显式做。

**练习 2**：为什么用 Event 来恢复 VS 状态，而不是直接从内存的 `Configuration` 推断？

**答案**：新 leader 刚上任时，它的内存 `Configuration` 可能刚完成 cache sync、状态齐全；但 Event 是跨 pod 重启仍存在的持久记录，能反映「上一次写入 NGINX 的最终结论（含警告/错误）」。用 Event 保证了 leader 切换后 status 与历史结论一致。

---

### 4.3 statusUpdater 模式

#### 4.3.1 概念说明

`statusUpdater` 是写回状态的统一执行者（[status.go:L28-L45](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L28-L45)）。它持有：

- `client`（标准 K8s client，写 Ingress status）
- `confClient`（NIC 自定义资源 client，写 VS/VSR/TS/Policy status）
- `status`（当前对外端点列表 `[]networking.IngressLoadBalancerIngress`，给 Ingress 用）
- `externalEndpoints`（CR 用的 `[]conf_v1.ExternalEndpoint`，含 IP/Hostname/Ports）
- `statusInitialized`（对外端点是否已就绪的标志）

对外端点（LB IP / 主机名）从哪里来？有三个来源，优先级从高到低：

1. **ConfigMap 的 `external-status-address`**（手工指定，最高优先级）—— [status.go:L290-L313](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L290-L313)
2. **IngressLink**（BIG-IP 提供）—— [status.go:L335-L347](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L335-L347)
3. **外部 LoadBalancer Service 的 IP**（最常见）—— [status.go:L320-L333](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L320-L333)

三个 `SaveStatusFromXxx` 方法都最终落到 `saveStatus`（[status.go:L214-L225](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L214-L225)），它把字符串 IP 解析成 IP 或 Hostname 形态，并置 `statusInitialized = true`。

#### 4.3.2 核心流程

无论哪类资源，写回状态都遵循同一个三段式（以 Ingress 为例，[status.go:L130-L169](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L130-L169)）：

```text
updateXxxStatus(obj, ...):
    1. 从 Store 取最新对象副本（保证 resourceVersion 最新）
       └─ 用 keyFunc 算 key，再用对应 Lister 的 GetByKeySafe / Get 取
    2. 变更检测：若新旧 status 相同（DeepEqual），直接返回，避免无效写
    3. DeepCopy → 改 status → UpdateStatus 提交
       └─ 若失败（通常是 409 冲突），走 retryXxxStatusUpdate：
              Get 拿最新版 → 再比对 → 必要时再 UpdateStatus
```

「先取最新对象再改」是应对 **乐观锁（resourceVersion）** 的关键。K8s 的更新基于 `resourceVersion`：如果你手里这份对象是旧的，`UpdateStatus` 会返回 `409 Conflict`。所以重试逻辑（[status.go:L191-L210](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L191-L210)）的策略是：**重新 GET 一份最新的，判断是否仍需要改，再试一次**。

各类资源的分派入口在 [status.go:L64-L96](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L64-L96)，用 Go 的 type switch 按 `Resource` 接口的实现类型分发到 Ingress 或 VS 分支。

#### 4.3.3 源码精读

Ingress 写回的核心（[status.go:L129-L169](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L129-L169)）：

```go
func (su *statusUpdater) updateIngressWithStatus(ing networking.Ingress, status []networking.IngressLoadBalancerIngress) error {
    key, err := su.keyFunc(&ing)
    // ...
    ingCopy, exists, err = su.getNamespacedInformer(ns).ingressLister.GetByKeySafe(key)
    // ...
    if reflect.DeepEqual(ingCopy.Status.LoadBalancer.Ingress, status) {
        return nil            // (2) 变更检测：相同就跳过
    }
    ingCopy.Status.LoadBalancer.Ingress = status
    clientIngress := su.client.NetworkingV1().Ingresses(ingCopy.Namespace)
    _, err = clientIngress.UpdateStatus(context.TODO(), ingCopy, metav1.UpdateOptions{})
    if err != nil {
        err = su.retryStatusUpdate(clientIngress, ingCopy)   // (3) 冲突重试
    }
    return err
}
```

注意 `UpdateIngressStatus` 有一道额外门禁（[status.go:L104-L109](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L104-L109)）：若 `!su.statusInitialized`（还没拿到任何对外端点），直接返回不写——避免把空 status 写进去又很快被覆盖。

VS 的写回（[status.go:L474-L508](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L474-L508)）结构几乎一致，只是改的是 `vsCopy.Status.State/Reason/Message/ExternalEndpoints`，且用 `confClient`。Policy 写回（[status.go:L688-L727](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go#L688-L727)）还多一道 `hasCorrectIngressClass` 过滤——不属于本控制器的 Policy 不写。这种「同构方法 × 多类型」的重复，是本项目 status 子系统的典型形态。

#### 4.3.4 代码实践

**实践目标**：跟踪一次状态写回的完整数据流，验证三段式与重试机制。

**操作步骤**：

1. 打开 [status_test.go:L260-L299](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status_test.go#L260-L299)（测试文件），看它如何用 `fakeClient` 驱动 `SaveStatusFromExternalStatus` + `UpdateIngressStatus`。
2. 阅读该测试的断言：先 `SaveStatusFromExternalStatus("1.1.1.1")` 再 `UpdateIngressStatus`，然后 GET 出来验证 `.status.loadBalancer.ingress` 变成了 `1.1.1.1`。

**需要观察的现象（源码阅读型）**：测试里 `ClearIngressStatus` 之后状态被清空，`SaveStatusFromExternalStatus("1.1.1.1")` + `UpdateIngressStatus` 之后状态变为 `1.1.1.1`，说明 `saveStatus` 只是「准备好要写的值」，真正的 API 写入由 `UpdateIngressStatus` 触发。

**预期结果**：你能解释「为什么 `SaveStatusFromExternalService` 单独调用不会改变集群里 Ingress 的 status」——因为它只更新 `su.status` 内存字段，必须再调 `UpdateIngressStatus` / `BulkUpdateIngressStatus` 才真正写 API。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `updateIngressWithStatus` 要先 `GetByKeySafe` 取最新副本，而不是直接改传入的 `ing`？

**答案**：传入的 `ing` 可能是旧版本（resourceVersion 过期），直接 `UpdateStatus` 会 409 冲突。从 Store 取最新副本能最大概率拿到最新 resourceVersion；万一还是冲突，再由 `retryStatusUpdate` 兜底。

**练习 2**：变更检测（`reflect.DeepEqual`）省掉了什么？

**答案**：省掉了「status 没变也写一次 API」的无谓写入。这在 resync 或 status 回写触发自身 watch 事件时尤其重要，能避免写放大循环。

---

### 4.4 启动期延后刷新（pendingStatus 与 flushPendingStatusesAsync）

#### 4.4.1 概念说明

这是本讲最精巧的部分。问题：控制器启动时，sync 会为内存里的每个资源调一次 status 写回。如果集群里有几千个 VS，就是几千次串行 `UpdateStatus`，pod 会迟迟不 Ready（即使 NGINX 其实已经能转发流量了）。

NIC 的解法（[controller.go:L122-L129](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L122-L129) 的注释说明了动机）：

1. **启动期（`!isNginxReady`）**：把每个资源「该写什么 status」记进 `pendingStatusXxx` 切片，**不真正写 API**。
2. NGINX reload 完成后，先把 `isNginxReady` 置 `true`（pod 标记 Ready，能转发流量），**再**用 `flushPendingStatusesAsync` 在后台并行刷写这些积压状态。

这样「能转发流量」和「status 元数据已写回」被解耦——前者是用户关心的，立即完成；后者是元数据，后台慢慢补。

#### 4.4.2 核心流程

五个 pending 切片（[controller.go:L236-L244](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L236-L244)）分别对应五类资源：

```go
pendingStatusIngresses []networking.Ingress
pendingStatusVSes      []pendingVSStatus
pendingStatusVSRs      []pendingVSRStatus
pendingStatusTSes      []pendingTSStatus
pendingStatusPolicies  []pendingPolicyStatus
```

每个 `pendingXxxStatus` 是一个小结构体，把「写状态需要的全部参数」打包（如 [controller.go:L131-L162](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L131-L162)），这样后台 worker 拿到切片元素就能直接调对应的 `UpdateXxxStatus`。

启动期各 sync 函数里的延后分支（以 Ingress 为例，[controller.go:L1762-L1768](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1762-L1768)）：

```go
if !lbc.isNginxReady {
    // 启动期：只记进切片，不写 API
    lbc.pendingStatusIngresses = append(lbc.pendingStatusIngresses, ings...)
} else {
    err := lbc.statusUpdater.BulkUpdateIngressStatus(ings)   // 稳态：直接写
}
```

触发点在 `Run` 的启动收尾（[controller.go:L1290-L1303](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1290-L1303)）：先 `isNginxReady = true`，紧接着 `flushPendingStatusesAsync()`。

**flushPendingStatusesAsync**（[controller.go:L2044-L2059](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2044-L2059)）做了一件关键的事——**快照并置空**：

```go
ings := lbc.pendingStatusIngresses
lbc.pendingStatusIngresses = nil
// ... 对 vses/vsrs/tses/pols 同样快照并置空
go lbc.runStatusFlush(ings, vses, vsrs, tses, pols)
```

快照后立即置空，意味着后台 goroutine 拿到的是「启动那一刻的」积压，而主 goroutine 在置空后可以继续用这些切片字段记录「启动之后新到的资源」，互不干扰。

**runStatusFlush**（[controller.go:L2067-L2099](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2067-L2099)）用 10 个 worker 的信号量并发刷写：

```go
const statusFlushWorkers = 10
sem := make(chan struct{}, statusFlushWorkers)   // 带缓冲 channel 当计数信号量
// 对每个资源：launchStatusWorker(sem, wg, name, func() error { return su.UpdateXxxStatus(...) })
```

`launchStatusWorker`（[controller.go:L2172-L2182](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2172-L2182)）就是「拿一个信号量槽 → 开 goroutine 执行 → 完了释放槽」的标准并发限流模式。10 这个数字的来由写在注释里（[controller.go:L2006-L2022](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2006-L2022)）：它对齐 K8s API 的 PriorityAndFairness（APF）并发席位预算，即使被限流也会排队而不是被拒。

#### 4.4.3 源码精读：leader 门禁与 waitForLeadership

flush 还要解决一个问题：**多副本下，谁是 leader 谁才该刷**。`runStatusFlush` 开头就做这个判断（[controller.go:L2074-L2076](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2074-L2076)）：

```go
if lbc.isLeaderElectionEnabled && !lbc.waitForLeadership() {
    return
}
```

`waitForLeadership`（[controller.go:L2107-L2128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2107-L2128)）每 500ms 轮询一次 `IsLeader()`，最多等 60 秒。如果 60 秒内没成为 leader（说明自己是 follower），就放弃本次 flush——follower 不写状态，等将来抢到 leader 时由 `OnStartedLeading` 统一恢复。

而稳态下，每次要写状态前都过两道门禁（[controller.go:L1986-L2004](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1986-L2004)）：

```go
func (lbc *LoadBalancerController) reportStatusEnabled() bool {
    if lbc.reportIngressStatus {
        if lbc.isLeaderElectionEnabled {
            return lbc.leaderElector != nil && lbc.leaderElector.IsLeader()
        }
        return true
    }
    return false
}
```

`reportCustomResourceStatusEnabled` 同构（只差 `reportIngressStatus` 那一层，因为 CR 的 status 上报不受 `-report-ingress-status` flag 约束）。`leaderElector.IsLeader()` 是 client-go 提供的线程安全查询，这就是代码里判断「我是不是 leader」的最终落点。

**注意一个例外**：problem 资源（冲突、校验失败）的状态写回**不进 pending 切片**，即使在启动期也立即写。原因写在 [controller.go:L1454-L1462](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1454-L1462)：problem 数量受限于「错误配置」而非「总资源数」，量小；而且 Ingress 的 problem 需要 `ClearIngressStatus`（清掉 LB IP），但 pending 切片只会调 `UpdateIngressStatus`（设置 LB IP），语义对不上。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：解释为什么非 leader 副本不应写回 status，并指出代码中判断 leader 的位置。

**操作步骤**：

1. 打开 [controller.go:L1986-L2004](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1986-L2004)，确认 `reportStatusEnabled` / `reportCustomResourceStatusEnabled` 是稳态写状态的门禁，它们的 leader 判断语句是 `lbc.leaderElector.IsLeader()`。
2. 打开 [controller.go:L2107-L2128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2107-L2128)，确认启动期 flush 也通过 `waitForLeadership()` → `IsLeader()` 守门。
3. 打开 [service.go:L233-L242](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/service.go#L233-L242)（`syncService` 中），看 `if lbc.reportStatusEnabled()` 如何在真正调 `UpdateExternalEndpointsForResources` 之前挡住 follower。

**需要观察的现象（源码阅读型）**：在 `syncService`、`processChanges`、`processProblems`、`runStatusFlush` 这些写状态的调用点，每一处要么直接被 `reportStatusEnabled()` / `reportCustomResourceStatusEnabled()` 包裹，要么被 `waitForLeadership()` 守门。

**预期结果**（请用一段话作答）：非 leader 副本如果也写 status，会和 leader 的写互相覆盖、产生抖动（A 写 IP、B 清空），且浪费 API 配额。代码中判断 leader 的位置有三类——

| 场景 | 判断位置 | 语句 |
| --- | --- | --- |
| 稳态写 Ingress status | `reportStatusEnabled()` [controller.go:L1987-L1995](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1987-L1995) | `lbc.leaderElector.IsLeader()` |
| 稳态写 CR status | `reportCustomResourceStatusEnabled()` [controller.go:L1998-L2004](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1998-L2004) | `lbc.leaderElector.IsLeader()` |
| 启动期 flush | `waitForLeadership()` [controller.go:L2107-L2128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2107-L2128) | 轮询 `IsLeader()` |

#### 4.4.5 小练习与答案

**练习 1**：为什么 `flushPendingStatusesAsync` 要「快照并置空」pending 切片，而不是直接遍历原切片？

**答案**：后台 flush 是异步的，主 goroutine 在 flush 期间会继续处理新事件、继续往 pending 切片 append 新状态。如果直接遍历共享切片，要么需要加锁、要么产生竞态。「快照 + 置空」让后台 goroutine 独占快照，主 goroutine 独占置空后的新切片，无锁且安全。

**练习 2**：如果 `waitForLeadership` 等了 60 秒还没成为 leader，本次 flush 被跳过，这些资源的状态会丢失吗？

**答案**：不会。它们的状态会被「将来抢到 leader 时」的 `OnStartedLeading` 统一恢复（见 4.2：Ingress 走 `UpdateExternalEndpointsForResources`，VS/VSR/TS 走 `updateXxxStatusFromEvents`，Policy 走 `updatePoliciesStatus`）。follower 跳过 flush 不是「丢状态」，而是「让 leader 来写」。

**练习 3**：problem 资源为什么不延后？

**答案**：见 [controller.go:L1454-L1462](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1454-L1462) 的注释——problem 数量小（受错误配置数而非总资源数限制），且 Ingress problem 需要 `ClearIngressStatus` 语义，pending 切片只会 `UpdateIngressStatus`，对不上。

---

## 5. 综合实践

**任务**：画出一次「多副本 NIC 滚动升级」过程中，状态写回职责是如何交接的。

请按下面的线索阅读源码，并画出时序图：

1. **旧 leader 还在**：它持有 Lease，`IsLeader()` 返回 true，稳态写状态过 `reportStatusEnabled()` 门禁（[controller.go:L1986-L1995](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1986-L1995)）。
2. **旧 leader 被删除**：它的 `leaderElector.Run(ctx)` 因 `ctx` 取消而退出，停止续约。Lease 在 30 秒（LeaseDuration）后过期。
3. **新 pod 启动**：它跑 `Run()`，启动自己的 `leaderElector.Run`（[controller.go:L765-L767](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L765-L767)）。启动期它把 status 积压进 pending 切片。
4. **新 pod 抢到 leader**：`OnStartedLeading` 触发（[leader.go:L63-L103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/leader.go#L63-L103)），它从 Event 重建 VS/VSR/TS 状态、重算 Policy 状态、刷新 Ingress 外部端点。
5. **同时**，新 pod 的 `isNginxReady` 置 true 后触发 `flushPendingStatusesAsync`，`runStatusFlush` 经 `waitForLeadership` 确认自己是 leader 后并行刷写 pending 切片（[controller.go:L2067-L2099](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L2067-L2099)）。

**交付物**：一张时序图，标出「Lease 持有者切换」「OnStartedLeading 触发」「flushPendingStatusesAsync 触发」三个关键事件，并注明每个事件后哪些资源的 status 被写回。

> 待本地验证：在真实集群可用 `kubectl get lease -w` 配合 `kubectl get vs -o yaml` 观察 holderIdentity 切换与 status 变化来验证你的时序图。

## 6. 本讲小结

- NIC 用 client-go 的 `LeaseLock` 做选举，三段时间参数取 `LeaseDuration=30s`、`RenewDeadline=15s`、`RetryPeriod=7.5s`，身份是 `POD_NAME`。
- 只有 leader 副本写状态；`OnStartedLeading` 是新 leader 的「状态恢复点」，Ingress 刷外部端点、VS/VSR/TS 从 Event 重建状态、Policy 重新校验写状态。
- `statusUpdater` 对五类资源用同构的三段式写回：取最新对象 → 变更检测（DeepEqual）→ DeepCopy 改 status 并 UpdateStatus，失败则重新 GET 后重试以应对 resourceVersion 乐观锁冲突。
- 对外端点有三个来源（ConfigMap `external-status-address` > IngressLink > LoadBalancer Service），由 `SaveStatusFromXxx` 准备好内存值，再由 `UpdateXxxStatus` 真正写 API。
- 启动期 status 写回被延后进 `pendingStatusXxx` 切片，pod 标记 Ready 后由 `flushPendingStatusesAsync` 快照置空并后台并行（10 worker）刷写，解耦了「能转发流量」与「status 已写回」。
- 判断 leader 的统一入口是 `leaderElector.IsLeader()`，分布在 `reportStatusEnabled` / `reportCustomResourceStatusEnabled`（稳态门禁）与 `waitForLeadership`（启动期 flush 门禁）三处。

## 7. 下一步学习建议

- 继续阅读 `internal/configs/configurator.go`，进入第 4 单元「NGINX 配置生成」，理解 status 之外 controller 的另一条主产出（nginx.conf）。
- 阅读 `internal/telemetry/collector.go`，理解 `OnStartedLeading` 里 `close(telemetryChan)` 如何触发遥测上报（对应 u7-l3）。
- 对照阅读 client-go 的 `leaderelection` 包源码，确认本讲描述的三段时间参数不变式与 `IsLeader()` 的线程安全实现。
- 在真实多副本集群上做一次「删 leader pod」实验，用 `kubectl get lease -w` 与 `kubectl get vs -o yaml` 验证本讲的 leader 切换与 status 恢复时序。
