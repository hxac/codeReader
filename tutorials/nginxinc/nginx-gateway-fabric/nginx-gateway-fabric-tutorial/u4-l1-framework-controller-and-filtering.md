# 自研框架：控制器抽象与过滤机制

## 1. 本讲目标

本讲我们下钻到 NGF「自研框架层」`internal/framework/controller`，把上一讲（[u3-l3](u3-l3-controller-registration-and-crd-discovery.md)）里那个被反复调用的 `controller.Register` 拆开来看：**当一条控制器注册项被提交后，框架内部到底做了什么？资源变更又是如何一步步变成下游能消费的事件？**

学完本讲你应该能够：

- 说清 `Register` 函数用「函数式选项（Option）」把一个资源类型装配成一个控制器的完整过程。
- 解释 `Reconciler.Reconcile` 如何用一次 `Get` 区分「新增/更新」与「删除」，并据此发出 `UpsertEvent` 或 `DeleteEvent`。
- 画出事件从 API server 到事件 channel 的**多层过滤流水线**：cache transform → predicate → namespacedName filter，并说明 field index 是「加速查询」而非「过滤」。
- 读懂 NGF 自带的 `GatewayClassPredicate`、`AnnotationPredicate` 等谓词，并能照着它们为某类资源写一个「只在某注解变化时才触发」的 predicate。
- 区分 `WithOnlyMetadata`、`WithK8sPredicate`、`WithFieldIndices`、`WithNamespacedNameFilter` 四个选项各自的作用层与适用场景。

本讲是 [u3-l3](u3-l3-controller-registration-and-crd-discovery.md) 的直接续篇：上一讲回答了「**注册哪些**控制器」（注册表、CRD 发现、特性开关），本讲回答「**控制器被注册时框架内部如何运作**」。本讲也是整个 [u4 单元](#)（事件管线）的起点——它产出的 `UpsertEvent`/`DeleteEvent` 正是下一讲 [u4-l2](u4-l2-event-loop-and-batching.md) 事件循环的输入。

## 2. 前置知识

### 2.1 controller-runtime 的 Controller / Reconciler / Manager

在 Kubernetes 控制器范式里，**Controller** 是一个后台 goroutine：它 watch（持续监视）某一种资源，一旦该资源增删改，就把一个 `reconcile.Request`（内含资源的 `NamespacedName`）放进工作队列，随后取出并调用 **Reconciler** 的 `Reconcile(ctx, req)` 方法做调谐。controller-runtime 提供了这套基础设施（Manager 聚合了 Cache、Client、工作队列、Leader 选举等共享组件）。在 [u3-l2](u3-l2-controller-runtime-manager-and-cache.md) 里我们已经建好了这个 Manager 容器。

关键点：controller-runtime 的 Reconciler 只拿到 `req.NamespacedName`（一个坐标），**不直接拿到对象本身**——要拿对象必须自己去 `Get`。NGF 正是利用这一点，在 `Get` 的成功/失败（NotFound）上区分「增改」与「删」。

### 2.2 Predicate（谓词）：事件进门前的门卫

controller-runtime 在「资源变更」变成「入队 reconcile.Request」之间，插了一道可选的过滤器，叫 **predicate**。它有四个方法 `Create / Update / Delete / Generic`，分别对四种事件返回 `bool`：返回 `false`，该事件被直接丢弃，Reconciler 根本不会被调用。NGF 海量使用 controller-runtime 自带的 `GenerationChangedPredicate{}`（只有 `metadata.generation` 变化，即 spec 被改了，才放行；纯 status 更新会被忽略），从而把大量「无关紧要的变化」挡在门外，避免无谓的 reconcile 与 NGINX reload。

### 2.3 Field Index（字段索引）：给缓存建目录

`Field Index` 不是过滤器，而是缓存上的「二级索引」，类似数据库索引。注册时你提供一个 `IndexerFunc`，告诉框架「从这个对象里抽出某个字段的值」；框架据此为缓存里的每个对象建立「字段值 → 对象列表」的反向映射。之后查询方可以用 `client.MatchingFields{字段名: 值}` 在 O(命中数) 内拿到结果，而不必把整类对象 list 出来再逐个过滤。典型例子：给 `EndpointSlice` 按「所属 Service 名」建索引，查某 Service 的 endpoints 时一查即得（见 4.3.3）。

### 2.4 函数式选项（Functional Options）模式

Go 里常见的配置写法：定义一个 `Option func(*config)` 类型，再用一组 `WithXxx(...)` 工厂函数返回各种 `Option`；被配置方持有一个 `config` 结构，循环 `for _, opt := range options { opt(&cfg) }` 即可把选项「刷」进配置。它的好处是：可选参数、可组合、易扩展（加一个选项只需加一个 `WithXxx`，不必改函数签名）。NGF 的 `Register` 正是这种写法。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [internal/framework/controller/register.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go) | 框架层 `Register` 函数与全部 `Option`（predicate / field index / namespacedName filter / only-metadata）。每条注册项最终在此落地 |
| [internal/framework/controller/reconciler.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go) | 框架层 `Reconciler`：`Get` 资源 → 区分增删 → 发出 `UpsertEvent`/`DeleteEvent` 到事件 channel |
| [internal/framework/controller/predicate/gatewayclass.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/gatewayclass.go) | `GatewayClassPredicate`：只放行 `controllerName` 匹配本控制器的 GatewayClass，是 predicate 写法的范本 |
| [internal/framework/controller/predicate/annotation.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation.go) | `AnnotationPredicate`：只在指定注解存在（Create）或变化（Update）时放行，**本讲主实践要模仿的对象** |
| [internal/framework/controller/index/index.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/index.go) | `FieldIndices` 类型定义（`map[string]client.IndexerFunc`），field index 的注册载体 |
| [internal/framework/controller/index/endpointslice.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/endpointslice.go) | EndpointSlice 的字段索引实现，是理解「建索引 + 查索引」闭环的最佳例子 |
| [internal/framework/controller/filter/filter.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/filter/filter.go) | `CreateSingleResourceFilter`：把 `NamespacedNameFilterFunc` 具体化为「只放行某一个固定资源」 |
| [internal/framework/events/event.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/event.go) | `UpsertEvent` / `DeleteEvent` 的类型定义，Reconciler 的输出 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 产品层注册表，本讲用来观察各 `Option` 在真实场景下如何组合使用 |
| [internal/controller/state/resolver/resolver.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go) | 消费 field index 的查询方，证明「建索引」确有其用 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：

1. **Register/Reconciler 抽象**——框架如何用「函数式选项」把一个资源类型装配成控制器。
2. **事件转换**——`Reconcile` 如何把一次 reconcile 变成 `UpsertEvent`/`DeleteEvent`。
3. **多层过滤与索引**——predicate / namespacedName filter / field index / cache transform 各自的作用层与协作。

### 4.1 Register/Reconciler 抽象：框架如何把一个资源类型变成控制器

#### 4.1.1 概念说明

回忆 [u3-l3](u3-l3-controller-registration-and-crd-discovery.md)：产品层 `registerControllers` 遍历注册表 `[]ctlrCfg`，对每一条调用 `controller.Register(...)`。`Register` 就是框架层暴露给产品层的**唯一装配入口**。它的职责可以概括为一句话：

> 给我一个资源类型（`objectType`）、一个控制器名、一个事件 channel，再加上一组选项，我就给你装配出一个「watch 这个类型、把变化翻译成事件、丢进 channel」的控制器，并挂到 Manager 上。

这里有三件事值得先在脑子里建立直觉：

- **框架不关心业务**。`Register` 不知道 Gateway/HTTPRoute 是什么，它只认 `client.Object`。所有 Gateway API 语义都在下游的 event handler（[u4-l3](u4-l3-event-handler-orchestration.md)）和 graph（u5）里。框架只负责「把变化变成事件」这个通用机制。这正是 [u1-l2](u1-l2-repo-structure.md) 讲的 `framework` 与 `controller` 边界：framework 写「怎么搭控制器」，controller 写「NGF 这个产品」。
- **配置用函数式选项传递**。`Register` 的可变参数 `options ...Option` 让调用方按需「点菜」：要不要加 predicate？要不要建 field index？要不要只缓存 metadata？全靠传不传对应的 `WithXxx`。
- **Reconciler 是可替换的**（为了测试）。`WithNewReconciler` 允许注入一个 mock reconciler，这样单测里可以不依赖真实 Kubernetes。默认情况下用的是 `NewReconciler`。

#### 4.1.2 核心流程

`Register` 的装配流程（伪代码）：

```text
Register(ctx, objectType, name, mgr, eventCh, options...):
    cfg = 默认配置()                  # newReconciler = NewReconciler
    for opt in options: opt(&cfg)     # 把各个 WithXxx 刷进 cfg

    # 1. 注册 field index（若有）：往 Manager 的 FieldIndexer 里加索引
    for field, fn in cfg.fieldIndices:
        AddIndex(ctx, mgr.GetFieldIndexer(), objectType, field, fn)

    # 2. 处理 OnlyMetadata：构造 ForOption
    forOpts = []
    if cfg.onlyMetadata:
        断言 objectType 已设置 GVK
        forOpts.append(builder.OnlyMetadata)

    # 3. 用 controller-runtime 的 builder 建控制器
    builder = NewControllerManagedBy(mgr).Named(name).For(objectType, forOpts...)
    if cfg.k8sPredicate != nil:
        builder = builder.WithEventFilter(cfg.k8sPredicate)   # 挂谓词

    # 4. 构造 Reconciler 配置并 Complete（启动控制器）
    recCfg = {Getter, ObjectType, EventCh, NamespacedNameFilter, OnlyMetadata}
    builder.Complete(cfg.newReconciler(recCfg))
```

注意几个关键设计：

- **field index 注册在最前**。索引必须在控制器开始 watch **之前**就建好，否则早期进缓存的对象不会被索引。所以 `AddIndex` 排在 `Complete` 之前。
- **predicate 挂在 builder 上**（`WithEventFilter`），由 controller-runtime 在事件入队前调用，比 Reconciler 更靠前（见 4.3）。
- **`namespacedNameFilter` 不挂 builder，而是塞进 `ReconcilerConfig`**。这意味着它是在 Reconciler 内部、拿到 `req.NamespacedName` 之后才判断的——比 predicate 晚一道（见 4.3.1）。

#### 4.1.3 源码精读

先看承载配置的结构体与选项类型。[register.go:23-29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L23-L29) 定义了私有 `config`，五个字段一一对应五个选项：

```go
type config struct {
    namespacedNameFilter NamespacedNameFilterFunc
    k8sPredicate         predicate.Predicate
    fieldIndices         index.FieldIndices
    newReconciler        NewReconcilerFunc
    onlyMetadata         bool
}
```

[register.go:34-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L34-L35) 定义 `Option` 类型，这是函数式选项的核心：

```go
// Option defines configuration options for registering a controller.
type Option func(*config)
```

五个 `WithXxx` 工厂函数都遵循同一个套路——返回一个修改 `config` 的闭包：

- [WithNamespacedNameFilter](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L38-L42)：注入「按 NamespacedName 过滤」的函数。
- [WithK8sPredicate](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L45-L49)：注入 controller-runtime 谓词。
- [WithFieldIndices](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L52-L56)：注入一组字段索引。
- [WithNewReconciler](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L59-L63)：注入自定义 reconciler 构造器（**仅测试用**，注释明说）。
- [WithOnlyMetadata](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L69-L73)：标记「只缓存 metadata」。

默认配置把 `newReconciler` 设成真实的 `NewReconciler`：[register.go:75-79](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L75-L79)。

接着是装配主体 [Register](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L84-L137)。几处要点：

[register.go:92-96](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L92-L96) 把所有选项刷进 `cfg`——这就是函数式选项的「应用」步骤：

```go
cfg := defaultConfig()
for _, opt := range options {
    opt(&cfg)
}
```

[register.go:98-108](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L98-L108) 注册所有 field index（在 watch 启动前完成）：

```go
for field, indexerFunc := range cfg.fieldIndices {
    if err := AddIndex(ctx, mgr.GetFieldIndexer(), objectType, field, indexerFunc); err != nil {
        return err
    }
}
```

[register.go:110-122](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L110-L122) 处理 `OnlyMetadata`（要求 GVK 已设置，否则 panic），并用 controller-runtime builder 建控制器、挂 predicate：

```go
builder := ctlr.NewControllerManagedBy(mgr).Named(name).For(objectType, forOpts...)
if cfg.k8sPredicate != nil {
    builder = builder.WithEventFilter(cfg.k8sPredicate)
}
```

[register.go:124-134](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L124-L134) 构造 `ReconcilerConfig` 并 `Complete`——后者真正把控制器注册进 Manager 并启动它的 watch goroutine。注意 `namespacedNameFilter` 与 `onlyMetadata` 是在这里通过 `ReconcilerConfig` 传进 Reconciler 的，**没有走 builder**。

最后看 `Reconciler` 类型本身：[reconciler.go:44-55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L44-L55)。它只是持有一个 `ReconcilerConfig`，并通过编译期断言 `var _ reconcile.Reconciler = &Reconciler{}` 保证自己实现了 controller-runtime 要求的接口：

```go
type Reconciler struct {
    cfg ReconcilerConfig
}
var _ reconcile.Reconciler = &Reconciler{}
```

[reconciler.go:25-36](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L25-L36) 的 `ReconcilerConfig` 把框架与外部世界（K8s client、事件 channel）连起来，是上一节 `Register` 末尾 `recCfg` 的接收方。

#### 4.1.4 代码实践

**实践目标**：在源码里验证「函数式选项」如何改变一个控制器的行为，理解选项的可组合性。

**操作步骤（源码阅读型实践）**：

1. 打开 [internal/controller/manager.go:805-816](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L805-L816)，这是 `GatewayClass` 的注册项。注意它的 `options` 用了 `controller.WithK8sPredicate(k8spredicate.And(GenerationChangedPredicate{}, GatewayClassPredicate{...}))`——两个谓词被 `And` 组合成一个。
2. 对比 [internal/controller/manager.go:848-854](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L848-L854)（`EndpointSlice`）：它同时用了 `WithK8sPredicate` **和** `WithFieldIndices`，演示了「多个选项叠加」。
3. 再看 [internal/controller/manager.go:873-881](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L873-L881)（CRD 资源）：它同时用了 `WithOnlyMetadata()` **和** `WithK8sPredicate(AnnotationPredicate{...})`，三个选项叠加。

**需要观察的现象**：

- 同一个资源类型，通过不同 `Option` 的组合，可以表现出完全不同的过滤与缓存行为，而 `Register` 函数本身的签名从不需要改变——这就是函数式选项的可扩展性。
- 选项的「组合」是 product 层的责任（决定 `And` 哪些谓词），框架层只提供积木。

**预期结果**：你能用自己的话回答「为什么 NGF 加一个新的过滤维度时，几乎不用动 `register.go`，只在 product 层注册表里加一个 `WithXxx`？」

#### 4.1.5 小练习与答案

**练习 1**：假如你想新增一个选项「限制控制器只在指定 namespace 下 watch」，应该改 `Register` 的签名吗？

**参考答案**：不需要。按现有模式新增一个 `WithNamespace(string) Option`，并在 `config` 加一个 `namespace` 字段即可；`Register` 用 `for _, opt := range options` 自然就能接收它。这正是函数式选项的设计意图——加选项不必改函数签名。

**练习 2**：为什么 `WithNewReconciler` 的注释强调「Used for unit-testing」？

**参考答案**：因为它允许单测注入一个假的 Reconciler，从而在没有真实 Kubernetes 的前提下测试 `Register` 的装配逻辑（比如验证 predicate/field index 是否被正确注册），而不是真的去 reconcile 资源。生产代码永远用默认的 `NewReconciler`。

### 4.2 事件转换：Reconciler 如何把资源变更变成 UpsertEvent/DeleteEvent

#### 4.2.1 概念说明

controller-runtime 的 Reconciler 拿到的只是一个坐标 `reconcile.Request{NamespacedName}`，并没有告诉你「这次是新增、更新还是删除」。NGF 的框架层用一个极其简洁的策略解决了这个问题：**再 `Get` 一次**。

- `Get` 成功 → 对象还在 → 这是**新增或更新** → 发 `UpsertEvent{Resource: obj}`。
- `Get` 返回 `NotFound` → 对象已不在 → 这是**删除** → 发 `DeleteEvent{Type, NamespacedName}`。
- `Get` 返回其它错误 → 真的出错了 → 把 error 返回给 controller-runtime，它稍后会重试。

这套设计的精妙之处在于：**它把「增、改、删」三类事件统一成「Get 一次 + 两个分支」**，下游处理器（[u4-l3](u4-l3-event-handler-orchestration.md) 的 `ChangeProcessor`）只需分别处理 `UpsertEvent`/`DeleteEvent` 两种事件即可，不必关心 controller-runtime 内部到底触发的是 Create/Update 还是 Delete watch 事件。

> 一个容易混淆的点：为什么 watch 已经告诉你是 Delete 了，还要靠 Get 来判断？因为 controller-runtime 把所有变更都规约成「带 NamespacedName 的 reconcile.Request」，Reconciler 拿到的输入里**不携带是哪种事件的信息**。所以框架用 Get 的结果来「重新推断」事件类型。这种「最终一致 / 重新查询」的风格是 Kubernetes 控制器的标准范式。

#### 4.2.2 核心流程

`Reconcile(ctx, req)` 的流程：

```text
Reconcile(ctx, req):
    logger = 从 ctx 取日志（controller-runtime 已注入 group/kind/ns/name）

    # 第一道：NamespacedName 过滤（可选）
    if namespacedNameFilter != nil:
        if !filter(req.NamespacedName):
            logger.Info(msg)        # 记日志说明为什么跳过
            return (Result{}, nil)  # 不发任何事件

    # 准备一个空的目标对象（按 ObjectType 实例化）
    obj = mustCreateNewObject(ObjectType)

    # 第二道：Get 资源，区分增删
    err = Getter.Get(ctx, req.NamespacedName, obj)
    if err != nil:
        if !IsNotFound(err):
            return (Result{}, err)   # 真错误，交还 controller-runtime 重试
        obj = nil                    # NotFound → 视为删除

    # 第三道：根据 obj 是否为 nil 组装事件
    if obj == nil:
        e = DeleteEvent{Type: ObjectType, NamespacedName: req.NamespacedName}
        op = "Deleted"
    else:
        e = UpsertEvent{Resource: obj}
        op = "Upserted"

    # 第四道：把事件送进 channel（支持 ctx 取消）
    select:
    case <-ctx.Done(): return       # 退出，丢弃这次
    case EventCh <- e:

    logger.Info(op + " the resource")
    return (Result{}, nil)
```

注意 `DeleteEvent` 只携带「类型 + 坐标」而**不携带对象本身**（因为对象已经没了）；`UpsertEvent` 则携带完整的资源对象。这决定了下游处理器对两类事件的处理方式不同（见 [u4-l4](u4-l4-change-processor-and-store.md) 的 `CaptureDeleteChange`）。

#### 4.2.3 源码精读

事件类型的定义在 [event.go:13-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/events/event.go#L13-L25)：

```go
type UpsertEvent struct {
    Resource client.Object
}
type DeleteEvent struct {
    Type           ngftypes.ObjectType
    NamespacedName types.NamespacedName
}
```

`Reconcile` 主体在 [reconciler.go:84-135](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L84-L135)。逐段看：

[reconciler.go:91-96](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L91-L96) 是 namespacedName 过滤——注意它发生在 `Get` **之前**，被过滤掉的资源连一次 Get 都不会发，省一次 API 调用：

```go
if r.cfg.NamespacedNameFilter != nil {
    if shouldProcess, msg := r.cfg.NamespacedNameFilter(req.NamespacedName); !shouldProcess {
        logger.Info(msg)
        return reconcile.Result{}, nil
    }
}
```

[reconciler.go:98-107](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L98-L107) 实例化空对象并 `Get`，用 `apierrors.IsNotFound` 区分删除：

```go
obj := r.mustCreateNewObject(r.cfg.ObjectType)
if err := r.cfg.Getter.Get(ctx, req.NamespacedName, obj); err != nil {
    if !apierrors.IsNotFound(err) {
        logger.Error(err, "Failed to get the resource")
        return reconcile.Result{}, err
    }
    obj = nil
}
```

`mustCreateNewObject` 在 [reconciler.go:57-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L57-L81) 有两个分支值得注意：

- 当 `OnlyMetadata=true` 时，创建的是 `metav1.PartialObjectMetadata`（只有元数据，体积小）。
- 否则用 `reflect.New` 反射创建真实类型实例。注释指出之所以不用 `DeepCopyObject` 是因为**反射更快**（有 benchmark 支撑）；对 `unstructured` 还要手动补回 GVK（因为反射出的零值会丢失运行时 GVK）。

[reconciler.go:109-130](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler.go#L109-L130) 组装事件并通过带 `select` 的发送把事件投入 channel：

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
    logger.Info("Did not process the resource because the context was canceled")
    return reconcile.Result{}, nil
case r.cfg.EventCh <- e:
}
```

这个 `select` 同时监听 `ctx.Done()`，保证控制面关闭时 Reconciler 不会卡在 channel 发送上。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，确认「NotFound → DeleteEvent、存在 → UpsertEvent」的转换规则，并理解 namespacedName filter 的短路行为。

**操作步骤（源码阅读型实践）**：

1. 打开 [internal/framework/controller/reconciler_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/reconciler_test.go)。找到「Get 返回 NotFound」的用例，断言 Reconciler 发出的是 `DeleteEvent`；找到「Get 成功」的用例，断言发出 `UpsertEvent`。
2. 找到「namespacedNameFilter 返回 false」的用例，断言**没有任何事件**被发送（channel 里读不到东西），且函数直接返回 nil。
3. 找到「Get 返回非 NotFound 错误」的用例，断言该 error 被原样返回。

**需要观察的现象**：

- 测试里通常用一个「假 client / fake getter」让 `Get` 返回想要的结果，再用一个 buffered channel 接收 Reconciler 发出的事件并断言其类型——这就是 `Getter` 接口（[getter.go:13-16](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/getter.go#L13-L16)）被设计成可 mock 的原因。
- namespacedName filter 命中时，连「创建对象 + Get」都被跳过，验证了它是最靠后但成本最低的「提前返回」。

**预期结果**：你能复述出 Reconciler 的四个出口：① filter 拦截（静默返回）；② Get 真错误（返回 error 重试）；③ ctx 取消（丢弃事件）；④ 正常发出 Upsert/Delete 事件。运行测试：`go test ./internal/framework/controller/...`（**待本地验证**具体输出）。

#### 4.2.5 小练习与答案

**练习 1**：如果 `Get` 返回 `NotFound`，但 controller-runtime 触发的其实是「更新」事件（对象在 watch 与 Get 之间被删除了），会发生什么？

**参考答案**：Reconciler 仍会发出 `DeleteEvent`。这正是「最终一致」语义的体现：控制器不关心中间到底发生了什么，只以「Get 当下的真实状态」为准——对象不在了，就当删除处理。下游的 ChangeProcessor 收到 DeleteEvent 后会从内部存储里移除它。

**练习 2**：为什么发送事件用 `select` 监听 `ctx.Done()`，而不是直接 `EventCh <- e`？

**参考答案**：channel 是无缓冲或下游暂时不消费时会阻塞发送。如果不监听 `ctx.Done()`，当控制面正在关闭（ctx 取消）而下游事件循环已停止读取 channel 时，Reconciler 会永久卡住，导致 controller 无法优雅退出。`select` 让它在 ctx 取消时及时放手。

### 4.3 多层过滤与索引：predicate / filter / field index / cache transform

#### 4.3.1 概念说明

NGF 的控制器在「资源变化」到「进入事件 channel」之间，设置了**一道立体的过滤与加速体系**。理解它们的**作用顺序与所处层级**是本模块的核心。下面这张流水线图把四层全部标出：

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Kubernetes API Server                                                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ watch（事件流）
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 第 0 层：cache transform（u3-l2 详解）                                │
│ 对象「进缓存之前」剥掉无用字段（managedFields、Secret 的 Data 等）。   │
│ 作用层：Cache。对业务透明，降低内存、也限制了 Reconciler 能看到的字段。│
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 缓存里的对象发生变更 → 产生事件
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 第 1 层：predicate（WithEventFilter / WithK8sPredicate）              │
│ controller-runtime 在「事件入队前」调用 Create/Update/Delete。         │
│ 返回 false → 事件直接丢弃，Reconciler 根本不会被调用。                  │
│ 例子：GenerationChangedPredicate、GatewayClassPredicate、             │
│       AnnotationPredicate。                                          │
│ 作用层：controller-runtime builder（最靠前，最省成本）。                │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 通过 → 入队 reconcile.Request
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│ 第 2 层：namespacedName filter（WithNamespacedNameFilter）            │
│ 在 Reconciler 内部、Get 之前，按 NamespacedName 判断要不要处理。       │
│ 返回 false → 静默 return，不发事件（也不 Get）。                       │
│ 典型用途：「这一类资源我只关心其中某一个固定的实例」。                   │
│ 作用层：Reconciler 内（比 predicate 晚一道）。                         │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 通过 → Get → 组装 UpsertEvent/DeleteEvent
                               ▼
                       事件 channel ───► 事件循环（u4-l2）
```

另外两个机制不在这条「过滤主轴」上，但同属控制器配置：

- **field index（WithFieldIndices）**：不是过滤器，而是建在第 0 层缓存之上的**加速索引**。它不影响「哪些事件会进来」，只在**下游查询**时让 `client.MatchingFields` 能 O(命中数) 取数。把它和过滤分开理解非常重要。
- **WithOnlyMetadata**：一种**缓存优化**，让控制器只 watch/缓存对象的元数据（`metav1.PartialObjectMetadata`），不缓存 spec/status。当业务只关心对象「存在与否 / 带哪些注解」而不关心正文时，能显著省内存。代价是：Reconciler 拿到的 `obj` 只有 metadata，**不能读取 spec 字段**；且必须给 `objectType` 显式设置 GVK（见 [register.go:110-116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L110-L116) 的 panic 兜底）。

> 何时用 predicate、何时用 namespacedName filter？经验法则：**能用 predicate 就用 predicate**——它更靠前，连 reconcile 入队都省了。namespacedName filter 适合「这类资源我只关心其中一个固定实例」这种「按身份筛选」的场景（因为 predicate 拿到的是整个对象，filter 拿到的只是坐标，后者更轻；而且有些场景 predicate 不好表达「只认这一个名字」）。实际上 NGF 也常把它们组合使用（如 [manager.go:984-987](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L984-L987) 同时用了两者）。

#### 4.3.2 核心流程

各层判定逻辑的要点：

```text
# predicate（以 GatewayClassPredicate 为例）
Create(e): gc.controllerName == 我 ? true : false
Update(e): old 或 new 的 controllerName == 我 ? true : false   # 任一端属于我就放行
Delete(e): gc.controllerName == 我 ? true : false

# predicate（以 AnnotationPredicate 为例）
Create(e): 对象带该 annotation ? true : false
Update(e): old.annotationVal != new.annotationVal ? true : false   # 值变了才放行

# namespacedName filter（CreateSingleResourceFilter）
filter(nsname): nsname == 目标资源 ? (true,"") : (false,"说明被忽略的原因")

# field index（以 EndpointSlice 为例）
建索引时：对每个 EndpointSlice，抽出 labels["kubernetes.io/service-name"] 作为索引键
查询时：  client.MatchingFields{"k8sServiceName": <svc-name>} → 命中的所有 EndpointSlice
```

注意 predicate 的 `Update` 往往采用「**old 或 new 任一满足即放行**」的策略——这保证「资源从『属于我』变成『不属于我』」这种迁移事件不会被漏掉（否则控制器会错过「它不再归我管」这个关键变化）。`GatewayClassPredicate.Update` 正是这么写的。

#### 4.3.3 源码精读

**predicate 范本之一：`GatewayClassPredicate`**（[predicate/gatewayclass.go:9-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/gatewayclass.go#L9-L61)）。它通过匿名嵌入 `predicate.Funcs` 获得 controller-runtime 谓词的默认实现，再覆盖需要的几个方法。`Create` 的核心是一行类型断言 + 比较：[gatewayclass.go:17-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/gatewayclass.go#L17-L28)

```go
func (gcp GatewayClassPredicate) Create(e event.CreateEvent) bool {
    if e.Object == nil {
        return false
    }
    gc, ok := e.Object.(*v1.GatewayClass)
    if !ok {
        return false
    }
    return string(gc.Spec.ControllerName) == gcp.ControllerName
}
```

`Update` 用「old/new 任一命中即放行」：[gatewayclass.go:31-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/gatewayclass.go#L31-L47)。这个谓词的真实用法在 [manager.go:809-813](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L809-L813)，与 `GenerationChangedPredicate` 用 `k8spredicate.And` 组合——意思是「spec 变了 **且** 这个 GatewayClass 归我管」才放行。

**predicate 范本之二：`AnnotationPredicate`**（[predicate/annotation.go:11-42](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation.go#L11-L42)）——这就是本讲主实践要模仿的对象。`Create` 检查注解是否存在，`Update` 比较新旧值是否不同：

```go
func (ap AnnotationPredicate) Create(e event.CreateEvent) bool {
    if e.Object == nil {
        return false
    }
    _, ok := e.Object.GetAnnotations()[ap.Annotation]
    return ok
}

func (ap AnnotationPredicate) Update(e event.UpdateEvent) bool {
    if e.ObjectOld == nil || e.ObjectNew == nil {
        return false
    }
    oldAnnotationVal := e.ObjectOld.GetAnnotations()[ap.Annotation]
    newAnnotationVal := e.ObjectNew.GetAnnotations()[ap.Annotation]
    return oldAnnotationVal != newAnnotationVal
}
```

它的真实用法见 [manager.go:873-881](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L873-L881)：CRD 资源用 `WithOnlyMetadata()` + `AnnotationPredicate{Annotation: consts.BundleVersionAnnotation}`——只缓存元数据，且只有当 `BundleVersion` 注解变化时才触发 reconcile（WAF bundle 版本号变化时才需要重新处理）。这是一个非常典型的「省心过滤」组合。

同文件里还有一个更具针对性的 [RestartDeploymentAnnotationPredicate](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation.go#L44-L80)：它在 `Update` 里特意**跳过**（返回 false）由滚动重启注解 `kubectl.kubernetes.io/restartedAt` 引起的事件，目的是让 provisioner 允许用户对 nginx Deployment 做滚动重启，而不把它当成「需要纠正的偏移」去回滚。这展示了 predicate 不仅能「收紧」，也能「故意放行某类噪声」。

**namespacedName filter** 的具体实现在 [filter/filter.go:11-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/filter/filter.go#L11-L25)：`CreateSingleResourceFilter` 返回一个闭包，只有当资源的 `NamespacedName` 等于目标时才返回 `true`，否则返回一段说明文字（会被 Reconciler 记进日志）。真实用法在 [manager.go:983-987](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L983-L987)：`NginxGateway` 这种「全局只有一份配置」的资源，用它确保控制器只认那一个实例。

**field index** 的载体类型很简单：[index/index.go:5-6](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/index.go#L5-L6) `type FieldIndices map[string]client.IndexerFunc`——字段名到「抽取函数」的映射。一个完整范本是 EndpointSlice 索引 [index/endpointslice.go:10-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/index/endpointslice.go#L10-L40)：

```go
const (
    KubernetesServiceNameIndexField = "k8sServiceName"
    KubernetesServiceNameLabel      = "kubernetes.io/service-name"
)

func CreateEndpointSliceFieldIndices() FieldIndices {
    return FieldIndices{
        KubernetesServiceNameIndexField: ServiceNameIndexFunc,
    }
}

func ServiceNameIndexFunc(obj client.Object) []string {
    slice, ok := obj.(*discoveryV1.EndpointSlice)
    if !ok {
        panic(fmt.Sprintf("expected an EndpointSlice; got %T", obj))
    }
    name := GetServiceNameFromEndpointSlice(slice)
    if name == "" {
        return nil
    }
    return []string{name}
}
```

「建索引」发生在 `Register` 里（[register.go:98-108](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L98-L108) 调 [AddIndex](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L139-L154)），「查索引」发生在下游 resolver。看 [resolver/resolver.go:75-83](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go#L75-L83)：`ServiceResolver.Resolve` 用 `client.MatchingFields{KubernetesServiceNameIndexField: svcNsName.Name}` 一次性拿到某 Service 的全部 EndpointSlice——这就是 field index 的价值：

```go
// We list EndpointSlices using the Service Name Index Field we added as an index to the EndpointSlice cache.
// This allows us to perform a quick lookup of all EndpointSlices for a Service.
var endpointSliceList discoveryV1.EndpointSliceList
err := e.reader.List(
    ctx,
    &endpointSliceList,
    client.MatchingFields{index.KubernetesServiceNameIndexField: svcNsName.Name},
    client.InNamespace(svcNsName.Namespace),
)
```

注意一个**易踩的坑**（来自 [WithOnlyMetadata](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/register.go#L65-L73) 注释）：用 `OnlyMetadata` 时，后续的 `Get`/`List` **不能用原始类型**（如 `v1.Pod`），必须用 `metav1.PartialObjectMetadata`，否则 controller-runtime 会报错。这是「只缓存元数据」模式的硬性约束。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：仿照现有的 `AnnotationPredicate`，为某类资源编写一个「只在指定注解变化时才触发事件」的谓词，并为它写表驱动单元测试，最后说明如何把它接到一个控制器注册项上。这是规格里要求的实践任务。

> 说明：`AnnotationPredicate` 本身已存在并能直接复用。本实践的重点是让你**亲手走一遍「写谓词 → 写测试 → 接线」的完整流程**，因此下面给出一个带名称语义的新谓词骨架（标注为「示例代码」，非项目原有代码）。

**操作步骤**：

**第 1 步：阅读范本**。先通读 [predicate/annotation.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation.go) 与 [predicate/annotation_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation_test.go)，建立对「匿名嵌入 `predicate.Funcs` + 覆盖 Create/Update」写法与表驱动测试的直观认识。

**第 2 步：编写新谓词**（示例代码）。在 `internal/framework/controller/predicate/` 下新建一个文件，例如 `myannotation.go`：

```go
// 示例代码：仿照 AnnotationPredicate，演示如何写一个谓词
package predicate

import (
    "sigs.k8s.io/controller-runtime/pkg/event"
    "sigs.k8s.io/controller-runtime/pkg/predicate"
)

// MyAnnotationPredicate 只在指定 annotation 的值发生变化（Create 时要求存在）时放行事件。
type MyAnnotationPredicate struct {
    predicate.Funcs          // 复用默认实现，只覆盖需要的 Create/Update
    Annotation       string
}

func (p MyAnnotationPredicate) Create(e event.CreateEvent) bool {
    if e.Object == nil {
        return false
    }
    _, ok := e.Object.GetAnnotations()[p.Annotation]
    return ok
}

func (p MyAnnotationPredicate) Update(e event.UpdateEvent) bool {
    if e.ObjectOld == nil || e.ObjectNew == nil {
        return false
    }
    return e.ObjectOld.GetAnnotations()[p.Annotation] != e.ObjectNew.GetAnnotations()[p.Annotation]
}
```

**第 3 步：编写表驱动测试**（示例代码）。在同级目录新建 `myannotation_test.go`，**照抄** [annotation_test.go:16-228](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/controller/predicate/annotation_test.go#L16-L228) 的结构，覆盖以下用例：注解存在/不存在（Create）、注解值改变/未变/被删除/被新增/换了别的注解（Update）、对象为 nil（两者）。用 `gomega` 的 `g.Expect(p.Create(test.event)).To(Equal(test.expUpdate))` 风格断言。

**第 4 步：接线**（设计说明，不实际改源码）。要把它用起来，在产品层注册表（参照 [manager.go:808-814](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L808-L814)）里给目标资源加上：

```go
// 示例代码：接线方式
controller.WithK8sPredicate(
    k8spredicate.And(
        k8spredicate.GenerationChangedPredicate{},          // 既有：spec 变了才看
        predicate.MyAnnotationPredicate{Annotation: "example.com/my-trigger"}, // 新增：指定注解变了才看
    ),
),
```

**需要观察的现象**：

- 只有当 `example.com/my-trigger` 这个注解的值真正发生变化时，控制器才会触发 reconcile 并最终发出 `UpsertEvent`；只改 status、或改其它注解、或改无关字段，都不会触发。
- 若同时挂了 `GenerationChangedPredicate`，则两个条件取**交集**（`And`）：必须 spec generation 变 **且** 该注解变。

**预期结果**：

- 单元测试全绿，覆盖了 Create/Update 的正反例。
- 你能用一句话说明：predicate 的 `Update` 返回 `false` 时，**连 Reconciler 都不会被调用**，更不会产生任何 `UpsertEvent`/`DeleteEvent`——这是它比 namespacedName filter 更省成本的原因。
- 运行测试命令：`go test ./internal/framework/controller/predicate/...`（**待本地验证**具体输出；本仓库要求 `-race -shuffle=on`，可参考 [u1-l3](u1-l3-build-and-run.md) 的 `make unit-test`）。

#### 4.3.5 小练习与答案

**练习 1**：如果你想让控制器「只在资源被**删除**时触发，忽略新增和更新」，该怎么写 predicate？

**参考答案**：嵌入 `predicate.Funcs`，把 `Create` 和 `Update` 都返回 `false`，只保留（或覆盖）`Delete` 返回 `true`。`predicate.Funcs` 的零值默认全是「放行」，所以需要显式把 Create/Update 关掉。注意：仅靠 predicate 过滤 Create/Update 后，Reconciler 仍只会在 Delete 事件入队时被调用，此时 `Get` 会 NotFound，发出 `DeleteEvent`。

**练习 2**：field index 和 predicate 都「配置在 Register 里」，它们的本质区别是什么？

**参考答案**：predicate 是**过滤器**，决定「哪些事件会到达 Reconciler」；field index 是**加速结构**，建在缓存上，决定「下游查询某类对象有多快」，完全不影响事件流。前者影响「正确性/工作量」，后者影响「查询性能」。两者经常被一起配置（如 EndpointSlice 既有 `GenerationChangedPredicate` 又有 field index），但作用维度不同。

**练习 3**：`GatewayClassPredicate.Update` 为什么用「old 或 new 任一命中即放行」，而不是「new 命中才放行」？

**参考答案**：若一个 GatewayClass 的 `controllerName` 从「我」改成「别人」，`new` 不命中、但 `old` 命中。如果用「new 命中才放行」，这个「它不再归我管」的事件会被丢弃，控制器就无法感知到「需要释放这个 GatewayClass」，导致状态残留。「任一命中即放行」保证了归属迁移的两端都不会被漏掉。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「跟踪一个事件的一生」的小任务：

**场景**：用户在集群里修改了一个 `HTTPRoute`，给它加了一个注解 `example.com/trigger: "v2"`。

**任务**：

1. **定位配置**：在 [manager.go:826-831](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L826-L831) 找到 `HTTPRoute` 的注册项，确认它挂了 `GenerationChangedPredicate`。
2. **判断是否触发**：仅加注解、不改 spec 时，`metadata.generation` **不会**变化（generation 只随 spec 变）。据此推断：这次注解修改会被 `GenerationChangedPredicate` 拦在第 1 层（predicate），**Reconciler 根本不会被调用**，也就不会有 `UpsertEvent` 进入事件 channel。
3. **对比反例**：再看 [manager.go:873-881](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L873-L881) 的 CRD 资源，它用的是 `AnnotationPredicate`——同样是「加注解」，这个控制器却**会**被触发。说明「同样的操作，不同的 predicate 组合，行为完全不同」。
4. **画出流水线**：用本讲 4.3.1 的流水线图，把上述两个资源各自的事件路径标注出来（一个在 predicate 层被丢弃，一个穿过 predicate 进入 Reconciler 发出 UpsertEvent）。
5. **延伸思考**：如果业务上确实需要「注解变化也触发 HTTPRoute 重新处理」，参照 4.3.4 的主实践，设计一个 `WithK8sPredicate(k8spredicate.Or(GenerationChangedPredicate{}, AnnotationPredicate{Annotation: "example.com/trigger"}))` 的方案，并说明它会让哪些以前被忽略的事件开始触发。

**预期产出**：一张标注了「第 0/1/2 层」的事件路径图，以及一段 3～5 句的说明，解释为什么「同样是改 YAML，有的改动 NGF 会响应、有的不会」——这背后正是本讲讲的多层过滤在起作用。

## 6. 本讲小结

- `Register` 是框架层把「一个资源类型」装配成「一个控制器」的**唯一入口**，用**函数式选项**（`WithK8sPredicate`/`WithFieldIndices`/`WithNamespacedNameFilter`/`WithOnlyMetadata`/`WithNewReconciler`）按需配置，加新维度不必改签名。
- `Reconciler` 是一个「笨翻译器」：用一次 `Get` 区分增删——存在则发 `UpsertEvent{Resource}`，NotFound 则发 `DeleteEvent{Type, NamespacedName}`，真错误则交还 controller-runtime 重试；它**不决定** NGINX 怎么改，那是下游 event handler 的事。
- 事件从 API server 到 channel 要穿过**多层过滤**：cache transform（进缓存前剥字段，[u3-l2](u3-l2-controller-runtime-manager-and-cache.md)）→ predicate（入队前最省成本）→ namespacedName filter（Reconciler 内、Get 前）；越靠前的层省下的工作量越多。
- **predicate** 用匿名嵌入 `predicate.Funcs` + 覆盖 Create/Update/Delete 实现；`Update` 常用「old/new 任一命中即放行」来保证归属迁移不丢事件；`AnnotationPredicate` 是「只在某注解变化时触发」的现成范本。
- **field index 不是过滤器**，而是缓存上的加速结构：`Register` 时用 `IndexerFunc` 建索引，下游用 `client.MatchingFields` 查询（如 `ServiceResolver` 查 EndpointSlice）。
- **WithOnlyMetadata** 是缓存优化（只 watch 元数据），但要求 `objectType` 设置 GVK，且后续 Get/List 必须用 `metav1.PartialObjectMetadata`。

## 7. 下一步学习建议

本讲产出的 `UpsertEvent`/`DeleteEvent` 会进入事件 channel，接下来要回答「这些事件如何被批处理、如何合并成一次 NGINX 配置更新」：

- **下一讲 [u4-l2 事件循环与批处理（双缓冲）](u4-l2-event-loop-and-batching.md)**：深入 `internal/framework/events/loop.go`，看双缓冲如何把高频资源变更合并成少量配置下发，理解为什么本讲的「过滤」如此重要——过滤掉的越多，事件循环的压力越小。
- **[u4-l3 EventHandler 编排](u4-l3-event-handler-orchestration.md)**：看 `eventHandlerImpl.HandleEventBatch` 如何消费本讲发出的事件批次。
- **[u4-l4 ChangeProcessor 与状态存储](u4-l4-change-processor-and-store.md)**：看 `CaptureUpsertChange`/`CaptureDeleteChange` 如何处理本讲定义的两种事件类型。
- 若想加深对 field index 消费侧的理解，可继续读 [resolver/resolver.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/resolver/resolver.go)，以及 [u5-l3 后端解析](u5-l3-backend-resolution.md)。
