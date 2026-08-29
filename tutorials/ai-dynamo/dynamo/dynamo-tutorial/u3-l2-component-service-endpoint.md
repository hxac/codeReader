# 服务注册模型：Component / Service / Endpoint

## 1. 本讲目标

上一讲（u3-l1）我们看清了 `Runtime` 与 `DistributedRuntime` 各自管什么：前者管本机的线程池与取消令牌，后者在其上管"集群视角"。但 `DistributedRuntime` 只是**持有**了一套服务目录，本讲要回答的是这套目录**长什么样**：

1. 说得出 `Namespace` / `Component` / `Endpoint` / `Instance` / `StartedEndpoint` 各自代表什么，以及它们之间的包含关系。
2. 能追踪一次 `endpoint_builder().start()` 从"本地对象"变成"可被别人发现的服务"的完整注册链路，包括它最终在 etcd（或 file/mem）里写下的 key 长什么样。
3. 能解释 `EndpointConfig` 里的标签、元数据（`device_type`、`request_plane_codec`、健康检查负载）各自的用途。
4. 能说清客户端如何订阅 endpoint 的实例变化，以及请求是怎样在多个实例间分摊的。

### 一个重要的术语校正（先读这一段）

本讲规划时的提法是"客户端如何按 tag 查找 endpoint"。**在当前 HEAD（`2c4ab6cf`）的代码里已经没有 tag 这个概念了**。旧版 Dynamo 用 etcd tag 做松散的服务分组，现在改成了严格的**层级路径 + 实例 ID** 两级寻址：

- **第一级（静态）**：`namespace / component / endpoint` 三段路径，等价于旧时代的"tag"，但它是一个精确的树形坐标，不是可叠加的标签。
- **第二级（动态）**：同一条路径下可以挂任意多个 `Instance`，每个用 `instance_id`（进程级的连接 ID）区分。

所以"两个同 tag 的 endpoint"在今天等价于"**两个进程注册了同一条 `namespace/component/endpoint` 路径**"，负载均衡就发生在这两个 `Instance` 之间。本讲的实践任务会按这个现代语义来做。

## 2. 前置知识

- **服务目录 vs 服务实现**。Dynamo 把"哪里有谁"和"谁在干活"彻底分开：目录信息（谁注册了、地址是什么）走**发现面**（discovery plane），实际请求走**请求面**（request plane，TCP 或 NATS）。本讲几乎全部在讲发现面。
- **etcd 是什么**。一个分布式键值存储，支持前缀查询、watch（监听 key 变化）和 lease（租约）。Dynamo 用它存服务目录：worker 启动时写入自己的地址，并挂在一个租约上；进程死亡后租约到期，key 自动消失，别人就"看不见"它了。
- **KV 存储抽象**。Dynamo 并不绑定 etcd。`lib/runtime/src/storage/kv.rs` 定义了 `Store`/`Bucket` trait，etcd / file / memory 三种实现可互换，由 `DYN_DISCOVERY_BACKEND` 选择。本讲的实践用 `file` 后端，零外部依赖。
- **`DashMap` / `watch` channel**。前者是并发哈希表（分片加锁，读几乎无锁）；后者是 tokio 的"只保留最新值"的广播通道，非常适合"当前有哪些实例"这种状态。
- **builder 模式**。Dynamo 大量用 `derive_builder` 生成 `XxxBuilder` 类型，字段可选、链式设置、最后 `build()` 校验。看到 `ComponentBuilder`、`EndpointConfigBuilder` 不要慌，它们都是宏生成的。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `lib/runtime/src/component.rs` | **核心**。定义 `Namespace`、`Component`、`Endpoint`、`Instance`、`TransportType`、`DeviceType`，以及模块的 `pub use` 出口 |
| `lib/runtime/src/component/endpoint.rs` | `EndpointConfig` / `EndpointConfigBuilder` / `StartedEndpoint`：一个 endpoint 从"配置"到"已注册"的全部逻辑 |
| `lib/runtime/src/component/client.rs` | 客户端侧：`Client` 如何 `list_and_watch` 发现面、维护可用实例集合 |
| `lib/runtime/src/component/registry.rs` | 现在只剩 `Registry::new()`，是历史遗留的薄壳 |
| `lib/runtime/src/component/service.rs` | NATS 遗留请求面的服务构建辅助，同样已高度收缩 |
| `lib/runtime/src/discovery/mod.rs` | **发现面抽象**：`Discovery` trait、`DiscoverySpec`、`DiscoveryInstance`、`DiscoveryQuery`、`EndpointInstanceId` |
| `lib/runtime/src/discovery/kv_store.rs` | `KVStoreDiscovery`：把发现面落到 etcd/file/mem KV 存储上的通用实现，定义 bucket 与 key 布局 |
| `lib/runtime/src/storage/kv.rs` | `Selector` 枚举与 `DYN_DISCOVERY_BACKEND` 的字符串解析 |
| `lib/runtime/src/transports/etcd/lease.rs` | etcd 租约的创建与保活，决定 worker 死后多久"消失" |
| `lib/runtime/examples/hello_world/src/bin/{server,client}.rs` | 本讲实践的基础示例 |

> 注意：规划时列出的 `lib/runtime/src/component/component.rs` 在当前 HEAD 已经退化成一段 8 行的注释，只告诉你 `EventPublisher`/`EventSubscriber` 挪到了哪里。真正的类型都在上一级的 `lib/runtime/src/component.rs`。同样，`service.rs` 与 `registry.rs` 也已收缩——"服务目录"这件事整体移交给了 `discovery/` 模块。这是阅读快速演进代码时常见的现象：**文件名还在，职责已经搬走了**。

## 4. 核心概念与源码讲解

### 4.1 三层名字空间：Namespace → Component → Endpoint

#### 4.1.1 概念说明

一个分布式 Dynamo 应用被组织成一棵目录树：

```
Namespace（命名空间，逻辑隔离边界）
└── Component（组件，一个有明确角色的逻辑单元，如 frontend / prefill worker）
    └── Endpoint（端点，组件对外暴露的一个可调用服务，如 generate）
        └── Instance × N（实例，真正干活的进程，用 instance_id 区分）
```

为什么需要三层而不是一层？因为这三个概念**变化频率完全不同**：

- `Namespace` 在部署时定死，用于隔离（比如多租户、多套环境共用一个 etcd）。
- `Component` 对应 DGD（DynamoGraphDeployment）里的一个 component，决定"这一类 worker 是什么角色"。
- `Endpoint` 是组件上的一个具名服务点，客户端只认它。
- `Instance` 随时增减——扩容、缩容、进程崩溃重启，都只动这一层。

客户端**只写死 endpoint 路径**，对 instance 一无所知；实例的上下线由 watch 机制自动同步进来。这就是 Dynamo 能做"worker 挂了请求不挂"的结构基础（u12-l4 会展开）。

#### 4.1.2 核心流程

创建路径的三步，全部是纯内存操作，不碰任何存储：

```text
DistributedRuntime::namespace("dynamo")        → Namespace
Namespace::component("backend")                 → Component（有 DashMap 缓存）
Component::endpoint("generate")                 → Endpoint（只是个轻量句柄）
```

要点：

1. `Namespace::component()` 带**组件缓存**。同一个名字重复调用返回同一个 `Component` 克隆，目的是让同一组件的所有 endpoint 共享一个 `MetricsRegistry`，避免指标重复注册。
2. `Component::endpoint()` 每次都新建 `Endpoint`，并把它的 metrics registry 挂为组件 registry 的子节点，形成一条可遍历的指标层级。
3. 名字有字符集校验：只允许 `a-z 0-9 - _`（小写）。这是因为这些名字会直接拼进 etcd key 路径和 NATS subject。

#### 4.1.3 源码精读

`Component` 结构体本身相当薄——它持有一个 `Arc<DistributedRuntime>`、名字、所属 namespace 和自己的 metrics registry，没有任何网络字段：

[lib/runtime/src/component.rs:173-198](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L173-L198)

这段定义了 `Component` 的字段。注意 `#[builder(private)]` 的 `drt` 字段：使用者不能自己塞一个运行时进来，必须走 `ComponentBuilder::from_runtime()`，保证组件永远挂在某个 `DistributedRuntime` 下。

`Namespace` 上的 `component()` 是带缓存的工厂方法，这是理解"组件缓存"的关键：

[lib/runtime/src/component.rs:540-565](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L540-L565)

快路径直接查 `DashMap` 命中就返回克隆；慢路径才构造新的 `Component` 并写回缓存。注释里明确说了动机：防止重复的指标注册，并让所有 endpoint 共享同一个 `Component::metrics_registry`。

`Component::endpoint()` 创建端点句柄并挂接指标层级：

[lib/runtime/src/component.rs:277-288](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L277-L288)

`Component::list_instances()` 是**本讲最有用的调试入口**——给定组件，直接问发现面"你现在有哪些活着的服务实例"：

[lib/runtime/src/component.rs:290-311](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L290-L311)

它构造 `DiscoveryQuery::ComponentEndpoints` 查询、调用 `discovery.list()` 拿一次性快照、过滤出 `DiscoveryInstance::Endpoint` 变体并排序返回。实践环节我们会用它来核对注册结果。

`Endpoint` 同样是个句柄，甚至比 `Component` 更薄：

[lib/runtime/src/component.rs:358-371](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L358-L371)

`Endpoint::id()` 把三段路径拼成一个 `EndpointId`，这是后面所有 etcd key、TCP 路由地址的原料：

[lib/runtime/src/component.rs:427-433](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L427-L433)

名字校验规则在 `validate_allowed_chars`，用正则 `^[a-z0-9-_]+$` 限制字符集：

[lib/runtime/src/component.rs:589-598](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L589-L598)

真正承载"一个可连接的服务实例"的是 `Instance`——它才是写进 etcd 的那条记录：

[lib/runtime/src/component.rs:106-119](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L106-L119)

五个字段值得逐个理解：`instance_id` 是进程级连接 ID（etcd 后端下就是 lease id，见 4.3.3）；`transport` 是 `Tcp("host:port/instance_id_hex/endpoint_name")` 或 `Nats(subject)`，即**请求面的实际地址**；`device_type` 标记这个 worker 在 CPU 还是 GPU 上（被异构路由用来区分）；`request_plane_codec` 声明这个 worker 接受的负载编码格式，缺省代表旧的 JSON-only worker。

`Instance` 的 `Display` 实现给出了它的规范字符串形式，日志里到处都是它：

[lib/runtime/src/component.rs:144-152](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L144-L152)

输出形如 `dynamo/backend/generate/1a2b3c`——前四段就是 etcd 里的 key 路径。

#### 4.1.4 代码实践

**实践目标**：不用任何网络，纯内存地感受三层路径的构造与字符集校验。

**操作步骤**：

在 `lib/runtime` 下新建一个临时测试（或者直接读 `component.rs` 文件末尾已有的测试），确认以下行为。以下为**示例代码**：

```rust
// 任意一个 lib/runtime 的集成测试里
use dynamo_runtime::DistributedRuntime;

#[tokio::test]
async fn build_path() -> anyhow::Result<()> {
    let rt = dynamo_runtime::Runtime::from_settings()?;
    let drt = DistributedRuntime::from_settings(rt.clone()).await?;

    let ns = drt.namespace("dynamo")?;
    // 同名两次拿到的 Component 是同一个（DashMap 缓存）
    let c1 = ns.component("backend")?;
    let c2 = ns.component("backend")?;
    assert_eq!(c1, c2);

    let ep = c1.endpoint("generate");
    assert_eq!(ep.id().to_string(), "dynamo/backend/generate");
    Ok(())
}
```

**需要观察的现象**：`assert_eq!(c1, c2)` 通过，说明缓存生效（`Component` 的 `PartialEq` 只比 namespace 和名字，见 [component.rs:207-211](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L207-L211)）。

**预期结果**：再试试 `ns.component("Backend")`（大写 B），`build()` 会因为 `validate_allowed_chars` 返回校验错误。**待本地验证**（具体错误文案取决于 derive_builder 的包装方式）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Namespace::component()` 要做缓存，而 `Component::endpoint()` 不做？

**答案**：`Component` 拥有自己的 `MetricsRegistry`，重复创建会导致同名指标被注册两次（Prometheus 会报 duplicate collector）；而 `Endpoint` 的 metrics registry 是每次新建并作为子节点挂上去的，指标名靠层级路径区分，天然不冲突。此外 `Component` 还是 NATS 请求面下服务注册的载体，重复注册同样有害。

**练习 2**：`Endpoint` 和 `Instance` 都带 `instance_id` 相关信息，它们的本质区别是什么？

**答案**：`Endpoint` 是**静态坐标**（namespace/component/endpoint 三段路径），在部署时确定，不含任何网络地址；`Instance` 是**动态事实**（坐标 + instance_id + transport 地址 + 设备类型 + 编码能力），由一个具体进程在运行时注册，随进程生死增减。一个 `Endpoint` 对应 0 到 N 个 `Instance`。

**练习 3**：`Namespace` 支持嵌套（`namespace()` 方法）。嵌套 namespace 的完整名字是怎么算出来的？

**答案**：递归拼接。见 [component.rs:580-585](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L580-L585)：有父 namespace 时返回 `format!("{}.{}", parent.name(), self.name)`，因此 `a.b.c` 这样的名字会被逐层展开。

### 4.2 注册与启动：EndpointConfig → StartedEndpoint

#### 4.2.1 概念说明

`Endpoint` 只是个地址句柄，要让它真正能服务请求，需要把一个**处理器**（handler）绑上去并"启动"。这一步由 `EndpointConfig` 描述、`EndpointConfigBuilder` 构建、`start_with_registration()` 执行，产物是 `StartedEndpoint`。

这是本讲最长也最关键的一段源码，它把四件事按顺序串起来：

1. **本地注册**：把 handler 挂到本进程的请求面服务器上（这样别人发来的请求才知道交给谁）。
2. **发现面注册**：把 `Instance` 记录写进 etcd/file/mem，让别人能"看见"我。
3. **优雅关停登记**：把自己计入 graceful shutdown tracker。
4. **清理任务**：spawn 一个后台任务，等关停令牌触发时把前两步撤销。

`StartedEndpoint` 则是这一切的持有者：你可以 `wait()`（传统方式，跟着运行时活到关停）或 `shutdown()`（精确控制这个 endpoint 的生命周期）。

#### 4.2.2 核心流程

```text
endpoint_builder()
  .handler(ingress)            // 必填：PushWorkHandler
  .metrics_labels(...)         // 可选：附加指标标签
  .graceful_shutdown(true)     // 可选：默认 true
  .health_check_payload(...)   // 可选：金丝雀健康检查负载
  .start_with_registration()
      │
      ├─ 1. handler.add_metrics(endpoint, labels)      ← 给 handler 注入指标
      ├─ 2. drt.child_token()                          ← endpoint 级关停令牌
      ├─ 3. build_transport_type(...)                  ← 算出 TCP/NATS 地址
      ├─ 4. [若有 health_check_payload]                 ← 校验并注册金丝雀目标
      ├─ 5. server.register_endpoint(name, handler)    ← 本地请求面注册
      ├─ 6. tracker.register_endpoint()                ← 优雅关停计数
      ├─ 7. discovery.register(DiscoverySpec::Endpoint) ← 写入 etcd/file/mem
      │      失败则回滚第 5、6 步
      └─ 8. spawn 清理任务（等待令牌取消后执行 unregister ×3）
      ▼
  StartedEndpoint { instance, shutdown_token, task }
```

其中第 7 步是"被发现"的分水岭：在这之前你只是个本地对象，之后你才出现在别人的 `list_and_watch` 结果里。

#### 4.2.3 源码精读

`StartedEndpoint` 的定义很好地体现了"作用域化生命周期"的设计意图：

[lib/runtime/src/component/endpoint.rs:52-78](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L52-L78)

两个关键语义：`Drop` 这个句柄**不会**停止 endpoint（这是文档明确警告的），要停就得显式调 `shutdown()`（取消令牌 + 等任务收尾）或 `wait()`（等运行时关停）。

`EndpointConfig` 的字段就是"标签与元数据"问题的答案所在：

[lib/runtime/src/component/endpoint.rs:80-105](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L80-L105)

三类可配置项：`metrics_labels` 只影响指标打点（给 Prometheus 序列附加维度，比如把 endpoint 按模型名分组）；`graceful_shutdown` 决定关停时是否等在途请求排空；`health_check_payload` 是发给自己的探针负载，用于金丝雀健康检查（配合 `register_local_engine` 使用，u12-l4 展开）。

`start_with_registration()` 的主干（节选关键部分）：

[lib/runtime/src/component/endpoint.rs:133-160](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L133-L160)

先是拆解配置、算出 `connection_id` 与 `endpoint_id`，给 handler 挂指标，然后创建**子令牌**——注释特别指出它是运行时 `endpoint_shutdown_token` 的 child，会在优雅关停中被**最先**取消，这正好对应 u3-l1 讲过的三阶段关停顺序。

接着构造发现面注册规格。这一段就是"一个 endpoint 如何变成一条可被发现记录"的直接证据：

[lib/runtime/src/component/endpoint.rs:231-265](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L231-L265)

`DiscoverySpec::Endpoint` 携带五元组：三段路径、`transport`（请求面地址）、`device_type`、`request_plane_codec`。注意 `discovery.register()` 失败时会**回滚**——先从请求面服务器注销、再从关停 tracker 注销，然后才报错。这是很典型的"多步注册必须考虑部分失败"的写法。

最后是清理任务，它把"注册"变成"可撤销的租借"：

[lib/runtime/src/component/endpoint.rs:267-302](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L267-L302)

任务体只做一件事：`cancel_token_for_cleanup.cancelled().await`，醒来后依次从发现面、请求面服务器、关停 tracker 三处注销。三步都只 `warn` 不 `bail`，尽力清理。

传输地址的构造逻辑值得单独看，它解释了 TCP 地址为什么长成那个样子：

[lib/runtime/src/component/endpoint.rs:321-353](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L321-L353)

TCP 模式下地址格式是 `host:port/{:x}/{endpoint_name}`——十六进制的 `instance_id` 加 endpoint 名。注释说明了原因：当 `--num-workers > 1` 时多个 worker 共享同一个 TCP 服务器，必须靠 instance_id 才能区分路由。NATS 模式则用 `nats::instance_subject()` 生成按实例唯一的 subject。

除了"启停"，`Endpoint` 还提供了一对精细的"暂时下线/重新上线"方法，专门服务于 worker 睡眠/唤醒场景：

[lib/runtime/src/component/endpoint.rs:392-429](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L392-L429)

`unregister_endpoint_instance()` 只从**发现面**移除实例（本地 handler 还在，进程没死），日志里明确写着 "worker removed from routing pool"。对应的 [register_endpoint_instance()](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L436-L472) 则把它加回来。这是 Planner 做缩容/休眠时的底层原语。

#### 4.2.4 代码实践

**实践目标**：通过日志亲眼看到注册链路的执行顺序。

**操作步骤**：

1. 设置环境变量提高日志级别：
   ```bash
   export RUST_LOG=dynamo_runtime=debug,hello_world=info
   export DYN_DISCOVERY_BACKEND=file
   ```
2. 运行 hello_world 的 server：
   ```bash
   cargo run -p hello_world --bin server
   ```
3. 观察启动日志。

**需要观察的现象**：日志里应依次出现类似下面这些行（顺序很重要）：
- `Starting endpoint: dynamo/backend/generate/...`
- `Registering endpoint with request plane server`（transport 名）
- `KVStoreDiscovery::register: Registering endpoint instance_id=..., key=dynamo/backend/generate/<hex>`

**预期结果**：日志顺序与 4.2.2 流程图的第 3、5、7 步一一对应。如果 `key=` 那一行没出现，说明 `RUST_LOG` 没覆盖到 `dynamo_runtime` 这个 target。**待本地验证**（日志是否全部可见取决于 tracing subscriber 的过滤配置）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `StartedEndpoint` 的 `Drop` 不停止 endpoint，而要显式 `shutdown()`？

**答案**：因为 endpoint 的生命周期通常应该**长于**任何单个持有句柄的代码作用域——你可能在函数里拿到 `StartedEndpoint` 做完别的事就返回了，但服务还得继续跑。如果 Drop 就停，句柄的移动会意外杀掉服务。显式 `shutdown()` 把意图交给调用者，`wait()` 则把控制权交还给运行时的统一关停流程。

**练习 2**：`unregister_endpoint_instance()` 和直接杀进程，对路由器来说有什么区别？

**答案**：直接杀进程要等租约到期（etcd 默认 10 秒，见 4.3.3）实例才会从发现面消失，期间路由器仍可能把请求发给一个已经不存在的地址；`unregister_endpoint_instance()` 是**主动、即时**的移除，watch 推送立刻到达路由器，但它保留了本地 handler 和进程本身，随时可以 `register_endpoint_instance()` 回来。前者是"意外死亡"，后者是"主动让位"。

**练习 3**：如果 `discovery.register()` 失败（比如 etcd 连不上），endpoint 会处于什么状态？源码怎么处理的？

**答案**：处于"半注册"状态——本地请求面和关停 tracker 已登记，但发现面没有记录。源码在 [endpoint.rs:245-261](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L245-L261) 处理了这种情况：先从 server 注销 endpoint、从 tracker 注销计数，然后 `bail!` 返回错误，让调用者知道启动失败而不是留下一个"看似在跑其实没人找得到"的僵尸服务。

### 4.3 Registry 与服务目录：从遗留薄壳到 Discovery 抽象

#### 4.3.1 概念说明

规划里说的 "Registry" 模块，字面上对应 `lib/runtime/src/component/registry.rs`——但它如今只剩 27 行：

[lib/runtime/src/component/registry.rs:14-27](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/registry.rs#L14-L27)

`Registry` 内部只是 `Arc<Mutex<RegistryInner>>`，而 `RegistryInner` 是 `HashMap<String, Service>`（NATS 的 service 类型）。也就是说，**它现在只服务于 NATS 遗留请求面**，与 etcd 服务发现无关。`service.rs` 同理，只剩一个 `build_nats_service()` 辅助：

[lib/runtime/src/component/service.rs:10-34](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/service.rs#L10-L34)

注释说得很直白："Minimal NATS service builder to support legacy NATS request plane. This will be removed once all components migrate to TCP request plane."

真正承担"服务目录"职责的，是 `lib/runtime/src/discovery/` 模块。它用三个类型把"注册"抽象出来：

- **`DiscoverySpec`** —— 我想注册什么（写入的意图）。
- **`DiscoveryInstance`** —— 注册成功后拿到的记录（含分配好的 instance_id）。
- **`DiscoveryQuery`** —— 我想查询什么（含前缀层级）。

再加上一个 `Discovery` trait 作为后端接口。这种"规格 / 实例 / 查询"三分法让同一套代码可以跑在 etcd、file、mem、kubernetes 四种后端上。

#### 4.3.2 核心流程

**注册**（写入方向）：

```text
DiscoverySpec::Endpoint { namespace, component, endpoint, transport, device_type, codec }
    │ spec.into_instance(self.instance_id())     ← 填入进程级 instance_id
    ▼
DiscoveryInstance::Endpoint(Instance { ... })
    │ 决定 bucket 与 key
    ▼
bucket = "v1/instances"
key    = "{namespace}/{component}/{endpoint}/{instance_id:x}"
    │ bucket.insert(key, serde_json::to_vec(instance))
    ▼
etcd / 文件 / 内存 里出现一条 JSON 记录
```

**查询**（读取方向），`DiscoveryQuery` 的四个层级正好对应四个前缀长度：

| 查询 | etcd/file 前缀 |
|---|---|
| `AllEndpoints` | `v1/instances` |
| `NamespacedEndpoints` | `v1/instances/{ns}` |
| `ComponentEndpoints` | `v1/instances/{ns}/{comp}` |
| `Endpoint` | `v1/instances/{ns}/{comp}/{ep}` |

前缀越短看得越宽。客户端 `Client` 用最长的 `Endpoint` 查询（只看自己关心的那个端点），运维工具和 `Component::list_instances()` 用短前缀做全局盘点。

#### 4.3.3 源码精读

`DiscoveryQuery` 枚举定义了全部查询粒度（节选 endpoint 部分）：

[lib/runtime/src/discovery/mod.rs:204-242](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L204-L242)

注意它不只查 endpoint，还能查 `Model`（模型卡，带 `*Models` 系列变体）和 `EventChannels`/`EventSources`（事件面发布源）。模型卡挂在独立的 `v1/mdc` bucket 下，供 KV 感知路由发现"哪个 worker 加载了哪个模型"。

`DiscoverySpec::Endpoint` 是注册时提交的五元组：

[lib/runtime/src/discovery/mod.rs:553-567](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L553-L567)

`into_instance()` 把 spec 转成带 instance_id 的记录，这是注册的第一步：

[lib/runtime/src/discovery/mod.rs:647-664](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L647-L664)

`Discovery` trait 是所有后端必须实现的接口（头部）：

[lib/runtime/src/discovery/mod.rs:1439-1446](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L1439-L1446)

`instance_id()` 的文档注释解释了它的来源：etcd 后端下就是 lease id。这一点直接决定了 worker 死亡的检测方式。

而 `list` / `list_and_watch` 是客户端发现的两个入口：

[lib/runtime/src/discovery/mod.rs:1537-1550](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L1537-L1550)

`list` 是一次性快照，`list_and_watch` 返回 `DiscoveryStream`（`Added`/`Removed` 事件流）。

具体的 KV 落地由 `KVStoreDiscovery` 完成，四个 bucket 常量定义了整个存储布局：

[lib/runtime/src/discovery/kv_store.rs:22-25](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/kv_store.rs#L22-L25)

endpoint key 的生成就是 `Instance::endpoint_instance_id().to_path()`：

[lib/runtime/src/discovery/kv_store.rs:79-87](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/kv_store.rs#L79-L87)

`EndpointInstanceId::to_path()` 给出了最终格式：

[lib/runtime/src/discovery/mod.rs:859-866](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/mod.rs#L859-L866)

即 `dynamo/backend/generate/1a2b3c4d`——四段斜杠分隔，最后一段是十六进制 instance_id。`from_path()` 则做反向解析（要求恰好 4 段、最后一段是合法十六进制）。

注册落库的主体，序列化成 JSON 后写入 bucket：

[lib/runtime/src/discovery/kv_store.rs:487-503](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/discovery/kv_store.rs#L487-L503)

注释里有一句很重要的话："Store in the KV store with no TTL (instances persist until explicitly removed)"——**KV 层不用 TTL**。那 worker 死了怎么被发现？答案在 etcd 后端的实现：写入时把 key 挂在 lease 上。

etcd 的 `update` 路径明确带了 `with_lease`：

[lib/runtime/src/storage/kv/etcd.rs:260-262](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/etcd.rs#L260-L262)

lease 的创建与保活在 `lease.rs`，TTL 默认 10 秒（可用 `ETCD_LEASE_TTL` 覆盖，非法值回退 10）：

[lib/runtime/src/transports/etcd/lease.rs:17-61](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/transports/etcd/lease.rs#L17-L61)

最关键的语义在末尾：保活任务一旦失败（etcd 不可达），会调用 `runtime.shutdown()` 主动关停整个 worker——注释说这兑现了"丢租约就关 worker"的契约。也就是说** etcd 断连不是"等服务恢复"，而是自杀**，由 K8s 重新拉起。这是有意的失败模式选择。

`file` 后端用文件 mtime 模拟租约，TTL 同样是 10 秒：

[lib/runtime/src/storage/kv/file.rs:28-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv/file.rs#L28-L33)

注释直接说明"10s is the same as our etcd lease expiry"——两个后端刻意对齐了死亡检测延迟。`MIN_KEEP_ALIVE` 限制保活频率至少 1 秒一次，避免磁盘写放大。

最后，`DYN_DISCOVERY_BACKEND` 的字符串解析在 `Selector::from_str`：

[lib/runtime/src/storage/kv.rs:154-170](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/storage/kv.rs#L154-L170)

`file` 后端的根目录由 `DYN_FILE_KV` 指定，默认 `/tmp/dynamo_store_kv`。这正是 u3-l1 结论"file + tcp + zmq 可零外部依赖本地运行"的落点。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 file 后端写下的 key 与记录内容，把"抽象的注册"落到"具体的文件"。

**操作步骤**：

1. 启动 server（file 后端）：
   ```bash
   export DYN_DISCOVERY_BACKEND=file
   export DYN_FILE_KV=/tmp/dynamo_kv_demo
   cargo run -p hello_world --bin server &
   ```
2. 等约 2 秒后查看目录：
   ```bash
   find /tmp/dynamo_kv_demo -type f | sort
   ```
3. 看其中一条记录的内容：
   ```bash
   cat "$(find /tmp/dynamo_kv_demo/v1/instances -type f | head -1)"
   ```

**需要观察的现象**：
- 路径形如 `/tmp/dynamo_kv_demo/v1/instances/dynamo/backend/generate/<hex>`，与 4.3.2 的 key 布局完全一致。
- 文件内容是一段 JSON，含 `component`、`endpoint`、`namespace`、`instance_id`、`transport`（`{"Tcp":"host:port/<hex>/generate"}`）、`device_type`、`request_plane_codec` 字段——就是 [component.rs:106-119](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component.rs#L106-L119) 的 `Instance` 序列化结果。
- 反复 `stat` 同一文件，mtime 每秒左右刷新一次（keep-alive 在"摸"这个文件）。

**预期结果**：杀掉 server 进程后再等约 10 秒，文件被过期线程删除——这就是 file 后端模拟的"租约到期"。**待本地验证**（`device_type` 字段是否出现取决于 `CUDA_VISIBLE_DEVICES` 的设置，见 [endpoint.rs:23-50](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/endpoint.rs#L23-L50) 的探测逻辑）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `KVStoreDiscovery` 写入时不设 TTL，而 etcd 后端却仍然能在 worker 死后清掉 key？

**答案**：因为 etcd 的 lease 机制在**存储层**实现了 TTL——key 通过 `with_lease()` 关联到一个租约，租约由 worker 的保活任务续期；worker 死了没人续期，租约到期时 etcd 服务端**自动删除**所有挂在它上面的 key。抽象层不需要再自己管过期，file 后端才需要在应用层用 mtime + 过期线程模拟同样的行为。

**练习 2**：`DYN_DISCOVERY_BACKEND=mem` 时，两个不同进程各自启动的 server 能互相发现吗？

**答案**：不能。`Memory` 后端的存储是**进程内**的（`MemoryStore` 活在 `KeyValueStoreEnum` 里，没有跨进程共享），每个进程有自己独立的一套目录，互相看不见。注释也提到 `MemoryStore doesn't respect TTL yet`。`mem` 只适合单进程内的单元测试。跨进程本地实验要用 `file`，生产环境用 `etcd` 或 `kubernetes`。

**练习 3**：`Component::list_instances()` 和 `Client::instances()` 都能拿到实例列表，它们有何不同？

**答案**：前者调 `discovery.list(DiscoveryQuery::ComponentEndpoints)`，是**按需的一次性快照**，查的是组件下所有 endpoint 的所有实例；后者来自 `list_and_watch` 建立的**持续订阅**，查询范围是单一 endpoint 路径，且由后台任务维护、随 DiscoveryEvent 增量更新，还叠加了 overload/故障屏蔽等路由状态。前者用于盘点，后者用于路由。

### 4.4 客户端侧：Client 如何订阅实例并分摊负载

#### 4.4.1 概念说明

服务端把 `Instance` 写进了目录，客户端要做的三件事是：**找到、跟踪、分摊**。

`Client` 是 `Endpoint` 的客户端视角。它内部维护两套有细微差别的实例集合：

- `instance_source`：**发现面看到的**全部实例（discovered）。
- `routing_instances`：**可以路由到的**实例（routable / free）——在 discovered 基础上扣掉被标记为 overloaded 或已被故障屏蔽的。

`RoutingInstanceCounts` 把这个漏斗量化成四个数字：`discovered` / `routable` / `overloaded` / `free`。KV 感知路由（u6 系列）就是在这个漏斗之上再加打分排序。

#### 4.4.2 核心流程

```text
Endpoint::client()
  └─ Client::new(endpoint)
       └─ get_or_create_dynamic_discovery_source(endpoint)
            ├─ discovery.list_and_watch(DiscoveryQuery::Endpoint{...})   ← 建立订阅
            ├─ watch::channel(vec![])                                     ← 广播通道
            └─ secondary.spawn(watch_loop)                                ← 后台任务
                 loop {
                     收到 DiscoveryEvent::Added(Endpoint(inst))  → map.insert(inst.instance_id, inst)
                     收到 DiscoveryEvent::Removed(id)            → map.remove(id)
                     watch_tx.send(map.values().collect())        ← 广播最新列表
                 }
  之后每次调用 router.round_robin()/random()/direct() 时：
     select_untracked_worker(picker)  ← 从 instance_ids_free() 里挑一个
```

要点：**watch 是按 endpoint 路径精确订阅的**，不是全量轮询。实例列表更新靠事件推送，不需要定时刷新。

#### 4.4.3 源码精读

`Client` 结构体的字段注释把两套实例集合讲得很清楚：

[lib/runtime/src/component/client.rs:418-433](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L418-L433)

`instance_source` 是"发现面的原始视图"，`routing_instances` 是"不可变路由快照（free 由 discovered 减 overloaded 推导）"。`reconcile_interval` 控制多久把 `instance_avail` 重置回 `instance_source`，让被 `report_instance_down` 临时屏蔽的实例最终恢复——0 表示禁用本地屏蔽。

`wait_for_instances()` 是所有客户端的第一行代码，它的行为值得记住：

[lib/runtime/src/component/client.rs:643-666](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L643-L666)

循环里 `borrow_and_update()` 拿当前快照，为空就 `rx.changed().await` 等下一次变化——**没有超时，永远等**。这印证了 u3-l1 的结论：endpoint 不存在时 client 是无限挂起而不是报错，所以启动顺序上要么 client 后启动，要么接受阻塞。

建立订阅的核心在 `get_or_create_dynamic_discovery_source()`：

[lib/runtime/src/component/client.rs:809-833](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L809-L833)

两个细节：一是入口处先查 `drt.endpoint_discovery_sources()` 的弱引用缓存——同一个 endpoint 的多个 `Client` 共享**一个**订阅任务，不会重复 watch；二是查询构造用的是最长的 `DiscoveryQuery::Endpoint`（三段路径全给），只订阅自己关心的那个端点。

后台 watch 循环维护 `HashMap<u64, Instance>` 并广播：

[lib/runtime/src/component/client.rs:840-888](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L840-L888)

`Added(Endpoint(inst))` 就 insert、`Removed` 且 id 是 Endpoint 就 remove，然后把 map 的值发进 watch channel。注意这个任务跑在 **secondary 线程池**上（`drt.runtime().secondary()`），不占用主池——发现面的维护不该抢请求处理的算力。

最后是分摊策略。`PushRouter` 在 `Client` 之上提供三种选点方式，`round_robin` 是其中最可预测的：

[lib/runtime/src/pipeline/network/egress/push_router.rs:890-916](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/network/egress/push_router.rs#L890-L916)

选完后打一条 `router_mode = "round-robin", worker_id = ...` 的 info 日志——**这就是实践环节观察"请求发给了谁"的官方窗口**。`random` 与之同构（[push_router.rs:918-943](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/network/egress/push_router.rs#L918-L943)），只是 picker 换成随机。

第三种 `direct` 用于精确指定实例，不允许传输层回退：

[lib/runtime/src/pipeline/network/egress/push_router.rs:991-1004](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/pipeline/network/egress/push_router.rs#L991-L1004)

`direct` 是 P/D 分离场景的关键：prefill 路由必须把请求送到**当初选中并已传输 KV 的那个** decode 实例，绝不能换人。

#### 4.4.4 代码实践

**实践目标**：写一个客户端，打印每次请求实际路由到的实例 ID，并统计两个实例的分摊情况。

**操作步骤**（以下为**示例代码**，基于 `hello_world` 的 client.rs 改写）：

```rust
// lib/runtime/examples/hello_world/src/bin/client.rs 的改写版
use std::collections::BTreeMap;

async fn app(runtime: Runtime) -> anyhow::Result<()> {
    let distributed = DistributedRuntime::from_settings(runtime.clone()).await?;

    let client = distributed
        .namespace(DEFAULT_NAMESPACE)?
        .component("backend")?
        .endpoint("generate")
        .client()
        .await?;

    let instances = client.wait_for_instances().await?;
    // 打印发现到的实例及其请求面地址
    for inst in &instances {
        println!("discovered {} -> {}", inst, inst.transport.address());
    }

    let router =
        PushRouter::<String, Annotated<String>>::from_client(client, Default::default()).await?;

    let mut tally: BTreeMap<u64, usize> = BTreeMap::new();
    for i in 0..20 {
        let mut stream = router.round_robin(format!("req-{i}").into()).await?;
        // 取第一帧，由 server 端把 instance_id 编进输出（见综合实践）
        if let Some(Ok(first)) = stream.next().await {
            let text = first.data;
            let id: u64 = text
                .split(':').next().unwrap_or("?")
                .parse().unwrap_or(u64::MAX);
            *tally.entry(id).or_insert(0) += 1;
        }
    }
    println!("round_robin 分布: {tally:?}");
    runtime.shutdown();
    Ok(())
}
```

**需要观察的现象**：`discovered` 打印出两行（前提是先启动了两个 server 进程），`instance_id` 不同、TCP 地址的端口段也不同；`round_robin 分布` 里两个 id 各约 10 次。

**预期结果**：如果两个 server 进程只启动了一个，分布会集中在一个 id 上；补启第二个后**不需要重启 client**，后续请求会自动开始分摊（watch 生效）。**待本地验证**（`Annotated` 的字段访问方式以 [hello_world client.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/examples/hello_world/src/bin/client.rs#L29-L33) 现有写法为准，它用的是 `println!("{:?}", resp)` 直接打印）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 watch 循环跑在 secondary 线程池而不是 primary？

**答案**：primary 池承担请求处理，是延迟敏感路径。发现面的 watch 是低频后台工作（只在实例上下线时触发），放 secondary 可以避免它和请求处理抢线程。这对应 u3-l1 讲过的 primary/secondary 双池分工。

**练习 2**：`Client` 被克隆了很多份，后台 watch 任务会不会跟着变多？

**答案**：不会。`get_or_create_dynamic_discovery_source()` 用 `drt.endpoint_discovery_sources()` 这个**按 Endpoint 键控的弱引用表**做去重：同一路径的 `Client` 拿到的是同一个 `Arc<EndpointDiscoverySource>`，任务只有一个；最后一个引用消失后弱引用升级失败，表项被清理。但要注意 [client.rs:441-476](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L441-L476) 的警告——默认构造的任务绑定**进程级**令牌，drop 光所有 `Client` 句柄并不会停它，会一直跑到进程退出；作用域更窄的调用方应改用 `client_with_cancellation()`。

**练习 3**：`instance_ids()`、`instance_ids_avail()`、`instance_ids_free()` 三个方法分别返回什么？

**答案**：`instance_ids()` 是发现面看到的全部实例（[client.rs:517-519](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L517-L519)）；`instance_ids_avail()` 是可路由集合（排除被 `report_instance_down` 临时屏蔽的）；`instance_ids_free()` 是 `instance_ids_avail()` 再排除被标记 overloaded 的，也是 `random`/`round_robin` 实际挑人的集合（[client.rs:525-529](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L525-L529)）。三层是逐级收紧的漏斗。

## 5. 综合实践

**任务：两个同路径实例的注册、发现、分摊与下线——一次跑通本讲全部内容。**

### 5.1 准备

零外部依赖组合（承接 u3-l1 的结论）：

```bash
export DYN_DISCOVERY_BACKEND=file      # 服务发现走文件系统，不需要 etcd
export DYN_FILE_KV=/tmp/dynamo_kv_lab  # 固定根目录，三个进程必须一致
export RUST_LOG=hello_world=info,dynamo_runtime=info
```

请求面默认 TCP、事件面默认 ZMQ，都不用额外起服务。

### 5.2 改造 server：把 instance_id 编进响应

要让客户端知道"是谁服务了我"，最简单的办法是让 server 自报家门。修改 `backend()`，把 `DistributedRuntime::connection_id()` 传进引擎（**示例代码**）：

```rust
async fn backend(runtime: DistributedRuntime) -> anyhow::Result<()> {
    let my_id = runtime.connection_id();
    println!("[server] my instance_id = {my_id}");

    let ingress = Ingress::for_engine(RequestHandler { instance_id: my_id })?;
    let component = runtime.namespace(DEFAULT_NAMESPACE)?.component("backend")?;
    component
        .endpoint("generate")          // 两个进程注册同一条路径
        .endpoint_builder()
        .handler(ingress)
        .start()
        .await
}
```

`RequestHandler` 增加字段并把每帧输出改成 `format!("{instance_id}:{ch}")`。注意 `runtime.connection_id()` 的文档说明（[distributed.rs:359-365](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/distributed.rs#L359-L365)）：它标识的是 **DRT 实例**而非操作系统进程，同一进程里多个 DRT 会拿到不同 ID。

### 5.3 启动两个 server 并验证注册

```bash
cargo run -p hello_world --bin server &     # 进程 A
cargo run -p hello_world --bin server &     # 进程 B
sleep 2
find /tmp/dynamo_kv_lab/v1/instances -type f | sort
```

此时应看到**两条** `.../dynamo/backend/generate/<hexA>` 与 `<hexB>` 记录——这就是"两个同路径实例"在存储层的形态，也是现代代码里"同 tag"的等价物。

再用 `Component::list_instances()` 从程序内交叉验证（**示例代码**）：

```rust
let comp = distributed.namespace("dynamo")?.component("backend")?;
for inst in comp.list_instances().await? {
    println!("list_instances -> {inst}");
}
```

### 5.4 跑分摊统计

按 4.4.4 的客户端跑 20 次 `round_robin`，记录分布；改成 `random` 再跑一轮，对比两份分布的方差差异。

### 5.5 观察下线

1. 保持 client 循环发请求（把 `for i in 0..20` 改成 `loop` 加 `tokio::time::sleep`）。
2. `kill` 掉进程 B。
3. 观察：client 的日志里出现 `Removed`，后续请求全部落到 A，**进程不报错**。
4. 等 10 秒后确认 `/tmp/dynamo_kv_lab/v1/instances` 下 B 的文件被过期线程删除。
5. 重新拉起一个 server，确认新实例自动进入轮换。

### 5.6 记录要求

把观察结果整理成一张表：注册时写的 key、两个实例的 `instance_id` 与 TCP 地址、`round_robin` 分布、`random` 分布、kill 后恢复耗时。这张表就是本讲四个模块的实验证据。

## 6. 本讲小结

- Dynamo 的服务目录是**四层结构**：`Namespace → Component → Endpoint → Instance`。前三层是静态坐标（部署时确定），`Instance` 是动态事实（随进程生死增减），客户端只认 endpoint 路径。
- 规划中提到的 "tag" 在当前 HEAD 已不存在，被"三段路径 + instance_id"的两级寻址取代；"两个同 tag 的 endpoint" 等价于"两个进程注册同一条路径"。
- `endpoint_builder().start_with_registration()` 依次完成：挂指标 → 建子关停令牌 → 算传输地址 → 本地请求面注册 → 优雅关停登记 → 发现面注册（失败会回滚）→ spawn 清理任务。
- `EndpointConfig` 的元数据各有去处：`metrics_labels` 进指标、`device_type` 供异构路由、`request_plane_codec` 声明负载编码、`health_check_payload` 支撑金丝雀探活。
- 存储布局固定为 `v1/instances/{ns}/{comp}/{ep}/{instance_id:x}`，值是 `Instance` 的 JSON。etcd 后端把 key 挂在 lease 上实现死亡检测（TTL 10 秒，保活失败会主动 `runtime.shutdown()`），file 后端用 mtime + 过期线程模拟同样的语义。
- 客户端按 endpoint 路径**精确订阅**（`list_and_watch`），watch 循环跑在 secondary 线程池、按 Endpoint 去重共享；实际选点发生在 `instance_ids_free()` 这个"discovered − 屏蔽 − 过载"的漏斗上，`round_robin`/`random`/`direct` 三种策略各有适用场景。
- `component/service.rs` 与 `component/registry.rs` 已收缩为 NATS 遗留请求面的薄壳，服务目录的真实实现在 `discovery/` 模块——读演进中的代码要学会识别"文件还在、职责已搬"的情况。

## 7. 下一步学习建议

本讲结束在"endpoint 已经能互相找到"这一步，但 `endpoint_builder().handler(ingress)` 里那个 `ingress` 我们当作黑盒用了。下一讲 **u3-l3 引擎抽象：AsyncEngine 与类型擦除** 正好拆它：`AsyncEngine`/`AsyncEngineUnary`/`AsyncEngineStream` 这组 trait 怎样定义"一个能处理请求的东西"，`AnyAsyncEngine` 如何做类型擦除让任意引擎挂到同一个 endpoint 上，以及 `Data` 类型如何承载异构负载。

若你想先横向补一块，建议读 `lib/runtime/src/component/client.rs` 的 `RoutingInstancesState`（[client.rs:275-415](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/runtime/src/component/client.rs#L275-L415)），那里有本讲提到的 overload 漏斗的完整实现，是 u6 KV 感知路由的直接前置。另外 `lib/llm/src/discovery/model_manager.rs:1969` 用到了 `drt.register_endpoint_lease()`——那是比本讲 `start_with_registration()` 更新式的**引用计数租约**注册方式，值得对照着看两种生命周期的取舍。
