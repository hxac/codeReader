# 事件循环与批处理（双缓冲）

## 1. 本讲目标

本讲承接 [u4-l1 自研框架：控制器抽象与过滤机制](u4-l1-framework-controller-and-filtering.md)。在 u4-l1 中我们看清了：控制器（`Reconciler`）把一次资源变更翻译成一个事件（`UpsertEvent` / `DeleteEvent`），再把它塞进一个 `eventCh`。但 `eventCh` 的另一头是谁？这些事件又是如何被消费的？本讲就回答这个问题。

学完本讲，你应当能够：

1. 说清 `internal/framework/events` 包里 **`Event` / `EventBatch` / `EventHandler`** 三个抽象各自的职责，以及事件有哪些具体类型。
2. 画出 **`EventLoop` 的双缓冲（double buffering）** 工作流程，并解释它为什么能把「短时间内多次资源变更」合并成「一次 NGINX 配置更新」，从而大幅减少 reload 次数。
3. 理解为什么控制面启动时要先准备一个 **首批事件（first event batch）**，以及 `FirstEventBatchPreparer` 如何把集群当前状态快照成一批 `UpsertEvent`。

本讲只讲「事件如何被收集、批处理、派发」，**不讲**这批事件被 `EventHandler` 拿到之后具体怎么生成 NGINX 配置——那是 [u4-l3 EventHandler：事件批次的总编排](u4-l3-event-handler-orchestration.md) 的主题。

## 2. 前置知识

- **NGINX reload 的代价**：处理一批事件通常会触发数据面 NGINX 重新加载配置（reload）。reload 不是免费的——它至少要花 200ms，具体取决于配置大小、TLS 证书数量和可用 CPU；同时 reload 还可能对数据面流量产生副作用。所以「减少 reload 次数」是 NGF 控制面的一条核心优化目标。
- **事件（Event）**：在本讲的语境里，「事件」=「一次资源变更的通知」，比如「某个 HTTPRoute 被创建了」「某个 Service 被删除了」。
- **生产者 / 消费者**：`Reconciler`（u4-l1）是事件的生产者，把事件写入 `eventCh`；`EventLoop`（本讲）是消费者，从 `eventCh` 读事件并交给 `EventHandler` 处理。两者之间用一个 Go channel 解耦。
- **快照（snapshot）**：处理一批事件时，希望处理过程看到的是一个「不再变化」的稳定视图，而不是一边处理、一边有新事件插进来。

> 阅读建议：本讲的灵魂在第 4.2 节的双缓冲。如果时间有限，先读 4.1 建立词汇，再精读 4.2，最后用 4.3 收尾。

## 3. 本讲源码地图

本讲聚焦在自研框架包 `internal/framework/events`，全部源码都很短：

| 文件 | 作用 | 本讲用到的关键符号 |
| --- | --- | --- |
| `internal/framework/events/event.go` | 定义事件与批次类型（词汇表） | `EventBatch`、`UpsertEvent`、`DeleteEvent`、`WAFBundleReconcileEvent` |
| `internal/framework/events/handler.go` | 定义 `EventHandler` 接口（消费侧抽象） | `EventHandler.HandleEventBatch` |
| `internal/framework/events/loop.go` | 本讲主角：`EventLoop`，含双缓冲逻辑 | `EventLoop`、`Start`、`swapBatches` |
| `internal/framework/events/first_eventbatch_preparer.go` | 准备首批事件（集群初始状态快照） | `FirstEventBatchPreparer`、`Prepare` |
| `internal/framework/controller/reconciler.go` | u4-l1 的生产者，作为本讲的输入侧上下文 | `Reconciler` 往 `EventCh` 发事件 |
| `internal/controller/manager.go` | 把 `EventLoop` 装配进控制面的总装现场 | `eventCh` 创建、`NewEventLoop`、`prepareFirstEventBatchPreparerArgs` |

一句话定位：`events` 包是「事件管线的中段」——左边接控制器产出的事件，右边接 `EventHandler`，中间用双缓冲做批处理。

## 4. 核心概念与源码讲解

### 4.1 Event / EventBatch / EventHandler：事件管线的三个抽象

#### 4.1.1 概念说明

在讲双缓冲之前，先把「事件」这个词的精确定义固定下来。`events` 包定义了三个抽象，构成一个极简的契约：

- **`EventBatch`**：一批事件，就是 `[]any`。一次「批处理」的最小单元。
- **具体的 Event 类型**：塞进 `EventBatch` 里的元素，目前有三种：
  - `UpsertEvent`：表示「插入或更新」一个资源（created/updated）。
  - `DeleteEvent`：表示「删除」一个资源。
  - `WAFBundleReconcileEvent`：WAF bundle 首次可用时由 WAF poller 注入的特殊事件（见 u10）。
- **`EventHandler`**：消费侧接口，只有一个方法 `HandleEventBatch`，负责「拿到一批事件后做什么」。

这套设计的关键是 **解耦**：`EventLoop` 只负责「收事件、攒批次、派发」，它完全不知道某个 `UpsertEvent` 里的 `HTTPRoute` 该翻译成什么 NGINX 配置——那是 `EventHandler` 实现的事（u4-l3 的 `eventHandlerImpl`）。正因如此，同一个 `EventLoop` 既能服务主控制面 handler，也能服务 provisioner 的 handler（见 4.1.3）。

#### 4.1.2 核心流程

数据流非常直白：

```
Reconciler(u4-l1) ──► eventCh ──► EventLoop(本讲)
                                       │ 攒成 EventBatch
                                       ▼
                                  EventHandler.HandleEventBatch(batch)
```

注意一个约定：**`EventBatch` 里允许出现重复事件**（接口注释明确写了 `EventBatch can include duplicated events`）。这意味着下游 `EventHandler` 必须是幂等的——同一个资源被 upsert 多次，只要 `Generation` 没变，就不该触发重配。这条约定是理解「首批事件」与「控制器缓存同步重复」为何无害的钥匙（见 4.3）。

#### 4.1.3 源码精读

先看「词汇表」`event.go`：

```go
// EventBatch is a batch of events to be handled at once.
type EventBatch []any

// UpsertEvent represents upserting a resource.
type UpsertEvent struct {
    Resource client.Object
}

// DeleteEvent representing deleting a resource.
type DeleteEvent struct {
    Type           ngftypes.ObjectType
    NamespacedName types.NamespacedName
}
```

- [`internal/framework/events/event.go:10-25`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/event.go#L10-L25)：`EventBatch` 就是一个任意类型切片；`UpsertEvent` 带完整资源对象，`DeleteEvent` 因为对象已不存在，只带类型和命名空间/名字。
- [`internal/framework/events/event.go:27-33`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/event.go#L27-L33)：第三种事件 `WAFBundleReconcileEvent`，由 WAF poller 在某个 WAF bundle 首次拉取成功时注入，提醒 handler 重新 reconcile 对应的 WAFPolicy。这说明 `eventCh` 的生产者**不止控制器**，WAF 子系统也会往里投递事件。

再看消费侧抽象 `handler.go`：

```go
type EventHandler interface {
    HandleEventBatch(ctx context.Context, logger logr.Logger, batch EventBatch)
}
```

- [`internal/framework/events/handler.go:13-17`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/handler.go#L13-L17)：接口只有一个方法，签名里就带了「批次」语义——`EventHandler` 天然是按批被调用的，而不是一个事件调一次。

最后看一眼输入侧，确认 u4-l1 的 `Reconciler` 确实是事件生产者：

```go
if obj == nil {
    e = &events.DeleteEvent{Type: r.cfg.ObjectType, NamespacedName: req.NamespacedName}
    op = "Deleted"
} else {
    e = &events.UpsertEvent{Resource: obj}
    op = "Upserted"
}
select {
case <-ctx.Done():
    return reconcile.Result{}, nil
case r.cfg.EventCh <- e:
}
```

- [`internal/framework/controller/reconciler.go:112-130`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L112-L130)：`Reconciler` 用一次 `Get` 区分增删，构造对应事件，再用 `select` 把事件送进 `EventCh`。注意它是**阻塞发送**（无缓冲或满时让出），并监听 `ctx.Done()` 防止关闭时死锁。这个 `EventCh` 正是 `EventLoop` 读取的 `eventCh`。

> 旁证：`EventLoop` 被设计成通用组件，provisioner 也复用了它——见 [`internal/controller/provisioner/eventloop.go:236-241`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/eventloop.go#L236-L241)，同样的 `NewEventLoop(...)` 调用。这就是把它放在 `framework` 层（而非 `controller` 产品层）的意义。

#### 4.1.4 代码实践

**实践目标**：确认「谁能往 `eventCh` 里写事件」。

**操作步骤（源码阅读型）**：

1. 打开 `internal/controller/manager.go`，找到 `eventCh := make(chan any)` 这一行，看它被传给了哪几个函数（提示：`registerControllers`、`createWAFPollerManager`、`NewEventLoop`）。
2. 分别确认：控制器经 `Reconciler.EventCh` 写、WAF poller manager 也写、`EventLoop` 负责读。

**需要观察的现象 / 预期结果**：同一个 `chan any` 有多个生产者、单一消费者（`EventLoop`）。这说明 `eventCh` 是一条**多源汇聚**的管线，而不是控制器独占。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DeleteEvent` 不像 `UpsertEvent` 那样携带完整资源对象，而只带 `Type` 和 `NamespacedName`？

> **参考答案**：删除事件发生时，资源已经从集群中不存在，`Reconciler` 的 `Get` 返回 `NotFound`，根本拿不到对象。下游处理删除只需要知道「是哪种类型、叫什么名字」即可，所以只带类型 + 命名空间/名字。

**练习 2**：`EventBatch can include duplicated events` 这条契约对下游 `EventHandler` 提出了什么要求？

> **参考答案**：要求幂等——同一资源被多次 upsert 进同一批次（或跨批次重复）时，handler 不能每次都触发 NGINX 重配。实际靠比较资源的 `Generation` 来判定「是否真的变了」（与 u4-l4 的 `changed_predicate` 呼应）。

### 4.2 EventLoop 双缓冲：本讲的核心

#### 4.2.1 概念说明

`EventLoop` 是事件管线的中段，它的核心职责是：**从 `eventCh` 读事件，攒成批次，交给 `EventHandler`，并保证任意时刻最多只有一批事件在被处理。**

为什么不能「来一个事件处理一个」？因为每处理一批事件通常就要 reload 一次 NGINX，而 reload 很贵（≥200ms，且有副作用）。设想集群里一次性来了 100 个资源变更——如果逐个处理，就是 100 次 reload；如果能攒在一起处理，就只有 1 次 reload。`EventLoop` 用的手段就是 **双缓冲（double buffering）+ 批处理（batching）**。

「双缓冲」这个名字来自图形学里的页面翻转（page flipping）思想：准备两块缓冲区，一块正在被「显示」（处理），另一块在后台被「绘制」（攒新事件），两者角色在合适时机翻转。它的好处是：处理一批事件时，handler 拿到的是一块稳定的、不再变化的快照；与此同时新来的事件可以无冲突地写进另一块缓冲区，**不需要加锁**。

> 先建立直觉，再看代码：处理一批 = handler 在一块缓冲上工作（可能花几百毫秒）；这期间到达的所有新事件都堆进另一块缓冲；handler 干完后，两块缓冲交换，把堆起来的事件一次性处理掉。

#### 4.2.2 核心流程

`EventLoop` 的内部状态（见 `EventLoop` 结构体）只有两块缓冲加上一个批次计数器：

```go
type EventLoop struct {
    handler  EventHandler
    preparer FirstEventBatchPreparer
    eventCh  <-chan any
    logger   logr.Logger

    currentBatch EventBatch   // 正在被处理的那一批（handler 读它）
    nextBatch    EventBatch   // 正在攒新事件的那一批（主循环写它）

    currentBatchID int
}
```

完整运行流程伪代码（省略首批事件，先看稳态）：

```
handling := false                          # 当前是否有批次在被处理

loop:
  select:
    case ctx.Done():
        if handling: 等待 handlingDone
        return
    case e := <-eventCh:                   # 来了一个事件
        nextBatch.append(e)                # 永远写进 nextBatch
        if not handling:                   # 没人在处理 → 立刻处理
            swapBatches()                  #   交换 current/next
            go handle(currentBatch)        #   新 goroutine 处理快照
            handling = true
        # 否则：事件留在 nextBatch，等当前批次处理完再一起处理
    case <-handlingDone:                   # 当前批次处理完了
        handling = false
        if len(nextBatch) > 0:             # 攒到了新事件
            swapBatches()
            go handle(currentBatch)
            handling = true
```

关键不变量（invariants）：

1. **任意时刻最多只有一批在处理**（`handling` 为 true 时，新事件只进 `nextBatch`，不会立即处理）。
2. **handler 拿到的是快照**：`handleBatch` 把 `currentBatch` **按值**传给处理 goroutine，所以 handler 迭代的是一块稳定的切片，主循环对 `nextBatch` 的写入不会影响它。
3. **无锁**：主循环 goroutine 只写 `nextBatch`、只在「无 handler 在飞」时翻转；handler goroutine 只读自己的快照。两者操作的切片底层永远不是同一块内存（翻转后，被写入的 `nextBatch` 用的是「两批之前的」旧数组，handler 用的是翻转前 `nextBatch` 对应的数组），所以不需要互斥锁。

**为什么能减少 reload**：设处理一批耗时 `T`（含一次 reload，约 200ms+），事件到达率为 `r`。则在处理一批的 `T` 时间内，会积累约 `r·T` 个新事件；这批处理完时，这 `r·T` 个事件被合并成**下一批**，只触发**一次** reload。没有批处理时，每个事件各触发一次 reload；批处理后，reload 次数约等于「处理耗时窗口」的数量级，而非事件数量级。源码注释里举的例子是：100 个积压事件，一次处理远好于逐个处理（见下方源码注释链接）。

#### 4.2.3 源码精读

先读 `EventLoop` 顶部那段极其重要的文档注释，它把「为什么批处理」讲得很清楚：

```go
// Batching is needed because handling an event (or multiple events at once) will typically result in
// reloading NGINX, which is an operation we want to minimize for the following reasons:
// (1) A reload takes time - at least 200ms. ...
// (2) A reload can have side-effects for data plane traffic.
// So when the EventLoop have 100 saved events, it is better to process them at once rather than one by one.
```

- [`internal/framework/events/loop.go:10-24`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L10-L24)：官方对「为什么要批处理」的论证——reload 贵且有副作用，100 个事件攒一起处理远好于逐个处理。

再看结构体里两块缓冲的字段与注释：

- [`internal/framework/events/loop.go:31-39`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L31-L39)：注释直接说明了双缓冲策略——「处理批次的 goroutine 总是读 `currentBatch`；处理期间新事件加进 `nextBatch`；启动 handler goroutine 之前交换两块缓冲」。

`Start` 方法是主循环，分三段看。

**第一段：两个闭包**。`handleBatch` 启动一个 goroutine 处理 `currentBatch`，注意它**按值**把 `el.currentBatch` 作为参数传进去（这就是「快照」的来源）：

```go
handleBatch := func() {
    go func(batch EventBatch) {
        el.currentBatchID++
        batchLogger := el.logger.WithName("eventHandler").WithValues("batchID", el.currentBatchID)
        batchLogger.V(1).Info("Handling events from the batch", "total", len(batch))
        el.handler.HandleEventBatch(ctx, batchLogger, batch)
        batchLogger.V(1).Info("Finished handling the batch")
        handlingDone <- struct{}{}
    }(el.currentBatch)   // ← 按值传入，形成快照
}

swapAndHandleBatch := func() {
    el.swapBatches()
    handleBatch()
    handling = true
}
```

- [`internal/framework/events/loop.go:67-85`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L67-L85)：`handleBatch` 里 `go func(batch EventBatch){...}(el.currentBatch)` 这一行是双缓冲的灵魂——`batch` 是一份切片头拷贝，handler 永远在这份快照上工作。`handlingDone` 是个信号 channel，handler 跑完就往里发一个空结构体，唤醒主循环。`swapAndHandleBatch` 把「交换缓冲」和「启动处理」打包成一个动作。

**第二段：交换逻辑**。

```go
func (el *EventLoop) swapBatches() {
    el.currentBatch, el.nextBatch = el.nextBatch, el.currentBatch
    el.nextBatch = el.nextBatch[:0]
}
```

- [`internal/framework/events/loop.go:144-148`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L144-L148)：先交换两个切片头（O(1)，不拷贝元素），再把（交换后的）`nextBatch` 截断到长度 0 复用底层数组——避免反复分配。配合「快照按值传递」，翻转后被写入的 `nextBatch` 与在飞 handler 的快照指向不同底层数组，于是无需加锁。

**第三段：主 select 循环**。

```go
for {
    select {
    case <-ctx.Done():
        if handling {
            <-handlingDone   // 等当前批次处理完再退出，避免半途而废
        }
        return nil
    case e := <-el.eventCh:
        el.nextBatch = append(el.nextBatch, e)   // 新事件一律进 nextBatch
        // ...
        if !handling {
            swapAndHandleBatch()                 // 没人在处理 → 立刻处理
        }
    case <-handlingDone:
        handling = false
        if len(el.nextBatch) > 0 {               // 攒到了 → 合并成一批处理
            swapAndHandleBatch()
        }
    }
}
```

- [`internal/framework/events/loop.go:111-141`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L111-L141)：三个 case 的语义。
  - `eventCh` 到达：**总是** append 到 `nextBatch`；若当前没有批次在处理，立刻交换并处理（此时 `nextBatch` 通常只有这一个事件，相当于立即处理）；若有批次在处理，则事件留在 `nextBatch` 里「等车」。
  - `handlingDone` 到达：当前批次处理完。此时检查 `nextBatch`——只要在处理期间攒到了 ≥1 个事件，就把它们合并成**一批**处理。**这就是「多次变更合并成一次更新」的发生地**。
  - `ctx.Done`：优雅退出，会等在飞批次处理完。

#### 4.2.4 代码实践

**实践目标**：用现成的单元测试亲眼看到「双缓冲把多个事件合并成一批」。

**操作步骤**：

1. 打开 `internal/framework/events/loop_test.go`，找到名为 `should batch multiple events` 的测试用例（位于 `Describe("Normal processing")` 下）。
2. 阅读它的思路：测试用一个会「阻塞」的 fake handler 处理 `e1`，在 `e1` 还没处理完时往 `eventCh` 发 `e2`、`e3`，然后放开阻塞，断言第二次 `HandleEventBatch` 收到的是 `[e2, e3]` 一批。
3. 运行该测试：

   ```bash
   go test ./internal/framework/events/ -run TestEventLoop -v
   ```

   （该包用 Ginkgo，`TestEventLoop` 是 Ginkgo 的入口。）

**需要观察的现象 / 预期结果**：测试通过，证明 `e2`、`e3` 被 `EventLoop` 合并进了同一批，而不是各触发一次 `HandleEventBatch`。把它翻译成生产语义：`e1` 对应的那次 NGINX reload 还在进行时到达的 `e2`、`e3`，会等到 `e1` 处理完后一起处理，最终只多产生一次 reload，而不是两次。

> 这是「源码阅读 + 测试验证型」实践。如果你想在本地进一步观察，可在 `swapAndHandleBatch` 里加一行示例日志（标注为**示例代码**，非项目原有代码）：`el.logger.Info("swapping", "nextBatchLen", len(el.nextBatch))`，重跑测试，会看到翻转时 `nextBatch` 的长度如何随积压事件增长。**待本地验证**具体日志输出。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `handleBatch` 改成 `go func() { ... el.handler.HandleEventBatch(ctx, ..., el.currentBatch) ... }()`（即闭包直接捕获 `el.currentBatch` 而不按值传参），会出什么问题？

> **参考答案**：闭包捕获的是 `el.currentBatch` 这个字段本身（按引用）。handler 还在迭代时，主循环可能在 `swapBatches` 里把 `el.currentBatch` 换成 `nextBatch`，导致 handler 中途看到的切片被「换底」，产生数据竞争或读到半新半旧的批次。按值传参（`(el.currentBatch)`）才形成稳定快照。

**练习 2**：为什么 `swapBatches` 用 `el.nextBatch[:0]` 截断，而不是 `make` 一个新切片？

> **参考答案**：复用底层数组，避免每个批次都分配新内存，减少 GC 压力。安全性由「翻转后的 `nextBatch` 与在飞 handler 快照指向不同底层数组」这一不变量保证，所以复用不会踩到正在被读的内存。

**练习 3**：`ctx.Done` 分支里为什么要 `if handling { <-handlingDone }`？

> **参考答案**：优雅关闭。收到关闭信号时，若仍有一批事件在处理，主循环会阻塞等它处理完再退出，避免在 handler 中途被打断、留下不一致的集群状态。

### 4.3 首批事件准备：FirstEventBatchPreparer

#### 4.3.1 概念说明

`EventLoop.Start` 进入主循环之前，会先做一件特殊的事：**准备并处理「首批事件」**。首批事件不是从 `eventCh` 来的，而是由 `FirstEventBatchPreparer` 主动从集群**全量 list/get** 一次，把所有相关资源的当前状态打包成一批 `UpsertEvent`。

为什么需要这个「特例」？因为控制面启动后第一次生成 NGINX 配置，必须基于**集群的完整视图**。如果只依赖控制器随后通过 `eventCh` 一个个送来的事件，那么在所有控制器缓存同步完成之前，handler 看到的都是「残缺」的集群状态，会生成不完整的配置——客户端可能看到瞬态 404，资源状态也会被错误地更新。首批事件就是用来保证「第一张图是完整的」。

#### 4.3.2 核心流程

```
启动 Start()
   │
   ▼
preparer.Prepare(ctx)              # 主动 list/get 集群当前状态
   │  对每个相关资源类型：
   │    reader.List(list)          # 全量列出
   │    每个 item → &UpsertEvent{Resource: item}
   │  对每个「单对象」（如 GatewayClass）：
   │    reader.Get(key)            # 按名字取
   │    存在 → &UpsertEvent{Resource: obj}
   ▼
得到首批 EventBatch（= 集群初始快照）
   │
   ▼
handleBatch() 直接处理这批        # 第一张图就基于完整视图生成
   │
   ▼
进入主 select 循环（开始接收 eventCh 事件）
```

一个值得注意的细节：**首批事件处理完之后，控制器随后通过缓存同步送来的初始 `UpsertEvent` 会和首批里的的事件重复**。这没问题——因为 `EventHandler` 对「同 `Generation` 的重复 upsert」不会触发重配（见 4.1.2 的幂等约定）。源码注释明确解释了这一点。

#### 4.3.3 源码精读

先看接口定义：

```go
type FirstEventBatchPreparer interface {
    Prepare(ctx context.Context) (EventBatch, error)
}
```

- [`internal/framework/events/first_eventbatch_preparer.go:20-23`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/first_eventbatch_preparer.go#L20-L23)：接口只有一个方法，返回首批批次。它是 `EventLoop` 的一个依赖（构造时注入），便于用 fake 替换做单测（文件顶部有 `//counterfeiter:generate . FirstEventBatchPreparer`）。

`Prepare` 的实现分两步：先全量 list 所有列表类型并统计总数，再逐个 get 单对象：

```go
for _, list := range p.objectLists {
    if err := p.reader.List(ctx, list); err != nil {
        return nil, err
    }
    total += meta.LenList(list)
}
batch := make([]any, 0, total+len(p.objects))

for _, obj := range p.objects {
    key := types.NamespacedName{Namespace: obj.GetNamespace(), Name: obj.GetName()}
    if err := p.reader.Get(ctx, key, obj); err != nil {
        if !apierrors.IsNotFound(err) {
            return nil, err
        }
    } else {
        batch = append(batch, &UpsertEvent{Resource: obj})
    }
}

for _, list := range p.objectLists {
    err := p.eachListItem(list, func(object runtime.Object) error {
        clientObj, ok := object.(client.Object)
        if !ok {
            return fmt.Errorf("cannot cast %T to client.Object", object)
        }
        batch = append(batch, &UpsertEvent{Resource: clientObj})
        return nil
    })
    // ...
}
```

- [`internal/framework/events/first_eventbatch_preparer.go:62-106`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/first_eventbatch_preparer.go#L62-L106)：
  - 用 `reader.List` 拉取每种资源列表（`reader` 实际是 controller-runtime 的 cache，见 u3-l2），用 `meta.LenList` 累加元素数，**预分配** `batch` 容量，避免反复扩容。
  - 单对象（如 `GatewayClass`，集群内通常只有目标那一个）用 `reader.Get` 按名字取；`NotFound` 被静默忽略（资源可能还没创建），其他错误才返回。
  - 列表里的每个 item 经 `eachListItem`（默认是 `meta.EachListItem`）遍历，转型为 `client.Object` 后包成 `UpsertEvent`。
  - 注释强调「事件顺序无关紧要」——下游的 `ChangeProcessor`（u4-l4）会自己按类型组织 store，不依赖到达顺序。

那么 `objects` 和 `objectLists` 具体是哪些资源？这是「首批要快照哪些集群状态」的总账，由控制面在总装时填好：

```go
firstBatchPreparer := events.NewFirstEventBatchPreparerImpl(mgr.GetCache(), objects, objectLists)
eventLoop := events.NewEventLoop(eventCh, cfg.Logger.WithName("eventLoop"), eventHandler, firstBatchPreparer)
```

- [`internal/controller/manager.go:254-266`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L254-L266)：`prepareFirstEventBatchPreparerArgs(cfg, discoveredCRDs)` 产出要快照的资源清单，用 cache 作 reader 构造 preparer，再把 preparer 注入 `EventLoop`。注意 `EventLoop` 被包成 `LeaderOrNonLeader` runnable 注册进 manager（u3-l4：所有副本都跑事件循环做热备）。

清单本身在 `prepareFirstEventBatchPreparerArgs` 里，是一长串「按资源类型 + 按 CRD 是否存在 / 特性开关是否开启」条件拼接的列表：

- [`internal/controller/manager.go:1276-1365`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1276-L1365)：
  - 固定要快照的：`GatewayClass`（单对象）、`ServiceList`、`SecretList`、`NamespaceList`、`EndpointSliceList`、`HTTPRouteList`、`ConfigMapList`、`NginxProxyList`、`GRPCRouteList` 以及各类 NGF 策略（`ClientSettingsPolicy`、`ObservabilityPolicy`、`ProxySettingsPolicy`、`UpstreamSettingsPolicy`、`AuthenticationFilter`、`RateLimitPolicy`、`WAFPolicy`）和一份 `PartialObjectMetadataList`（CRD 元信息）。
  - 条件追加：`APPolicy`/`APLogConf`（WAF PLM，按 CRD 发现）、`ReferenceGrant`（v1 优先，回退 v1beta1）、`BackendTLSPolicy`、`ListenerSet`、实验性的 `TLSRoute`/`TCPRoute`/`UDPRoute`（按 `ExperimentalFeatures`）、`InferencePool`（按 `InferenceExtension`）、`SnippetsFilter`/`SnippetsPolicy`（按 snippets 开关）。
  - 这与 u3-l3 的控制器注册表是**同一套资源清单逻辑的另一面**：注册表决定「watch 谁」，首批准备决定「快照谁」，两者必须保持一致（源码注释 `make sure to also update prepareFirstEventBatchPreparerArgs()` 提醒同步）。

最后，回到 `loop.go` 看首批事件如何被消费，以及重复无害的论证：

```go
// Prepare the fist event batch, which includes the UpsertEvents for all relevant cluster resources.
// This is necessary so that the first time the EventHandler generates NGINX configuration, it derives it from
// a complete view of the cluster. ...
// After the handler goroutine handles the first batch, the loop will start receiving events from
// the controllers, which at the beginning will be UpsertEvents with the relevant cluster resources - i.e. they
// will be duplicates of the events in the first batch. This is OK, because it is expected that the EventHandler will
// not trigger any reconfiguration after receiving an upsert for an existing resource with the same Generation.

el.currentBatch, err = el.preparer.Prepare(ctx)
if err != nil {
    return fmt.Errorf("failed to prepare the first batch: %w", err)
}
handleBatch()
handling = true
```

- [`internal/framework/events/loop.go:87-108`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L87-L108)：`Prepare` 出错则 `Start` 直接返回错误（启动失败）；成功则直接 `handleBatch()` 处理这批，置 `handling=true`，随后才进入主循环。注释把「首批 = 完整视图」和「随后的重复 upsert 无害（同 Generation 不重配）」两件事都讲透了。

#### 4.3.4 代码实践

**实践目标**：理解「首批事件 = 集群初始快照」，并验证重复无害的设计前提。

**操作步骤（源码阅读型）**：

1. 读 [`internal/controller/manager.go:1276-1310`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L1276-L1310)，数一下首批固定快照了多少种资源类型。
2. 对照 `internal/framework/events/first_eventbatch_preparer_test.go`，看 `Prepare` 的单测如何用 fake reader 构造列表/单对象返回值、断言生成的批次内容。
3. 思考：控制器随后缓存同步送来的「重复 upsert」为什么不会触发第二次 NGINX reload？把答案与 [`internal/framework/events/loop.go:92-96`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L92-L96) 的注释对照。

**需要观察的现象 / 预期结果**：能说清「首批负责把集群现状灌进 handler，使其第一张图完整；之后控制器的同步事件即便重复，也因 Generation 未变而被下游 ChangeProcessor 判定为无变化、不重配」。若想本地验证 Generation 去重，可阅读 `internal/controller/state/changed_predicate.go`（u4-l4）。

#### 4.3.5 小练习与答案

**练习 1**：为什么首批事件里 `GatewayClass` 用 `Get`（单对象），而 `HTTPRoute` 用 `List`（列表）？

> **参考答案**：NGF 只认领**一个**指定的 `GatewayClass`（由 `--gatewayclass` 指定），所以按名字 `Get` 那一个即可；而 `HTTPRoute` 等是用户资源，集群里可能有很多个、分布在各命名空间，必须 `List` 全量。

**练习 2**：如果 `preparer.Prepare` 因为某个资源类型 `List` 失败而返回错误，`EventLoop.Start` 会怎样？

> **参考答案**：`Start` 直接 `return fmt.Errorf("failed to prepare the first batch: %w", err)`，`EventLoop` 不会进入主循环、控制面启动失败（见 [`loop.go:99-102`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L99-L102)）。这保证控制面绝不会在「集群视图不完整」的情况下开始工作。

## 5. 综合实践

**任务**：用一句话+一张时序图，解释「为什么双缓冲能把多次资源变更合并成一次 NGINX 配置更新」，并指出合并动作发生在源码的哪一行。

**建议步骤**：

1. **复述机制**：参照 4.2.2 的伪代码，写出「事件 e1 正在被处理、期间 e2/e3 到达、e1 处理完后 e2/e3 被合并处理」的完整时序，标注每一步操作的是 `currentBatch` 还是 `nextBatch`。
2. **定位代码**：指出「合并」发生在 [`internal/framework/events/loop.go:133-139`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L133-L139) 的 `case <-handlingDone` 分支——只要 `nextBatch` 非空，就把积压的全部事件一次性 `swapAndHandleBatch`。
3. **量化收益**：结合 [`loop.go:17-23`](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/loop.go#L17-L23) 的 reload 代价论证（≥200ms/次），估算「一次性 apply 100 个 YAML」在有/无批处理两种情况下的 reload 次数差异（批处理 ≈ 1~2 次；逐个处理可达数十~上百次）。
4. **回归测试**：运行 `go test ./internal/framework/events/ -run TestEventLoop -v`，确认 `should batch multiple events` 通过，作为机制证据。

**预期产出**：一段说明 + 一张时序图 + 一个 reload 次数对比结论。结论应落在：「双缓冲让『处理一批』与『攒下一批』并发进行，处理期间到达的所有事件自动并入下一批，于是 N 次变更被压缩成约 ⌈总处理时间 / 单批处理耗时⌉ 次 reload。」

## 6. 本讲小结

- `events` 包用三个极简抽象串起事件管线中段：`EventBatch`（`[]any`，批次）、具体 `Event` 类型（`UpsertEvent` / `DeleteEvent` / `WAFBundleReconcileEvent`）、`EventHandler` 接口（按批消费）。`eventCh` 是一条多源（控制器 + WAF poller）汇聚、单消费者（`EventLoop`）的管线。
- `EventLoop` 用**双缓冲**：`currentBatch`（handler 读快照）与 `nextBatch`（主循环攒新事件），翻转时机在「无批次在处理」或「一批处理完且 `nextBatch` 非空」。handler 按值拿到稳定快照，主循环只写 `nextBatch`，二者底层数组不重叠，所以**无需加锁**。
- **批处理减少 reload**：处理一批期间到达的所有事件都进 `nextBatch`，处理完后合并成**一批**处理，从而把「N 次变更」压缩成远少于 N 次 reload（reload ≥200ms 且有副作用）。
- **首批事件**由 `FirstEventBatchPreparer` 主动 list/get 集群当前状态生成，保证 handler 第一次生成 NGINX 配置时基于完整视图，避免瞬态 404 与错误状态；`Prepare` 失败则控制面启动失败。
- 首批之后控制器缓存同步送来的重复 upsert 无害，因为 `EventHandler` 对同 `Generation` 的重复 upsert 不触发重配（幂等约定，呼应 u4-l4）。
- `EventLoop` 是 `framework` 层通用组件，主控制面 handler 与 provisioner 都复用它；以 `LeaderOrNonLeader` runnable 注册，所有副本都跑事件循环做热备。

## 7. 下一步学习建议

本讲止步于「事件被攒成批次、派发给 `EventHandler`」。下一讲 [u4-l3 EventHandler：事件批次的总编排](u4-l3-event-handler-orchestration.md) 将打开 `EventHandler.HandleEventBatch` 的实现（`internal/controller/handler.go` 的 `eventHandlerImpl`），看清一批事件到达后如何依次调用 `ChangeProcessor`（捕获变更）→ 构建图 → 生成配置 → 下发 NGINX → 更新状态。

建议同步阅读的源码：

- `internal/framework/events/loop_test.go`：`should batch multiple events` 是双缓冲最好的「活文档」。
- `internal/controller/state/change_processor.go` 与 `changed_predicate.go`（u4-l4）：解释「重复 upsert 为何不重配」的真正落点。
- `internal/controller/manager.go` 的 `prepareFirstEventBatchPreparerArgs`：理解首批快照的资源清单如何与控制器注册表（u3-l3）一一对应。
