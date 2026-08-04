# ModelClaim 模型激活与池化策略

## 1. 本讲目标

上一篇（u4-l1）我们学了 `ModelAdapter`：它把一个 **LoRA 适配器**挂到一个已经常驻运行的基座模型上。本讲换一个维度——`ModelClaim`。

学完本讲，你应当能够：

1. 说清 `ModelClaim` 与 `ModelAdapter` 的本质区别：前者不是挂适配器，而是让运行时边车为一个模型**拉起一整个独立的推理引擎进程**，多个引擎还能共享同一张 GPU（通过 kvcached）。
2. 画出 `ModelClaim` 控制器的 reconcile 主循环：选 Pod → 激活引擎 → 「端口为 0 不引流」直到就绪 → 就绪后翻成真实端口让网关引流 → 删除时优雅停机。
3. 描述控制平面与运行时边车之间的 HTTP 激活协议（`activate` / `deactivate` / `snapshot` 等）。
4. 理解「放置算法」如何在多个候选 warm Pod 里挑一个负载最低、显存最空闲、权重已缓存的 Pod。
5. 理解「池化策略」如何用 warm-pool Deployment 上的一个 JSON 注解，在多引擎共享一张 GPU 时弹性分配 KV Cache 预算、并把空闲引擎休眠。

## 2. 前置知识

在进入源码前，先用几个通俗概念铺底。

- **warm GPU pool（热身 GPU 池）**：一组**预先就绪**的 GPU Pod。它们由一个普通 Deployment 创建，已经把 GPU/CUDA 上下文、kvcached 的 KV 池都准备好，只等「被挂模型」。你可以把它想象成一排「开着机、空着显卡、随时能装模型」的机器。`ModelClaim` **不创建 Pod**，它只在这些 warm Pod 上「认领」资源。
- **引擎进程（engine process）**：一个模型对应一个真正在跑的推理服务进程（如 vLLM）。`ModelClaim` 激活模型 = 让运行时边车 fork 一个引擎进程。这与 `ModelAdapter`（在一个已有引擎里挂 LoRA）完全不同。
- **高密度（high-density）**：一张物理 GPU 上同时跑多个模型的引擎进程，靠 kvcached 共享 KV Cache 内存，从而压低成本。
- **kvcached**：AIBrix 的分布式 KV Cache 组件（详见单元 10）。这里你只需知道：每个引擎进程有一个唯一的「IPC 名」（共享内存段名），kvcached 用它区分同 GPU 上的多个引擎。
- **网关引流**：网关（单元 7）靠读取 warm Pod 上的注解来决定「这个模型现在能不能被路由到」。注解里的 `port` 决定了真实端口；`port: 0` 表示「这个引擎还活着但还不能接客」。
- **reconcile（调谐）**：Kubernetes 控制器的标准模式——不断把「期望状态」推向「实际状态」。本讲的「期望状态」就是 `ModelClaim` 里写的「我要 1 个该模型的活跃实例」。

> 与 u4-l1 对照记忆：`ModelAdapter` 解决「一个基座模型挂多个 LoRA」；`ModelClaim` 解决「一张 GPU 跑多个完整模型」。两者都靠运行时边车干活，但一个是「热插拔小模块」，一个是「整机开新进程」。

## 3. 本讲源码地图

本讲聚焦 `pkg/controller/modelclaim/` 包，核心文件如下：

| 文件 | 作用 |
| --- | --- |
| [modelclaim_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go) | reconcile 主循环：列候选 Pod、激活/缩容、按快照做就绪收敛、删 finalizer。控制器的「大脑」。 |
| [runtime_client.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go) | 控制平面 ↔ 运行时边车的 HTTP 协议：请求/响应结构体、`RuntimeClient` 接口、各端点路径。 |
| [placement.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go) | 放置算法：在候选 warm Pod 里挑一个「最该挂载」的 Pod。 |
| [pool_policy.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go) | 池化策略的解析与核心预算算法：把一个物理 KV 预算在多引擎间分配。 |
| [pool_policy_controller.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go) | 池化策略的 reconcile：找 warm Deployment、读注解、按 tick 执行 KV 限流与空闲休眠。 |
| [snapshot.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go) | 运行时快照的进程内缓存，以及 `PodPlacementState`（放置算法的输入摘要）。 |
| [parallelism.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/parallelism.go) | 解析 vLLM 的 TP/PP 并行度，校验 warm Pod 的 GPU 数是否匹配。 |

辅助数据模型见 [api/model/v1alpha1/modelclaim_types.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/api/model/v1alpha1/modelclaim_types.go)（u2-l3 已讲过 Spec/Status 字段，本讲只引用其中关键字段）。

---

## 4. 核心概念与源码讲解

### 4.1 ModelClaim reconcile 与激活协议

#### 4.1.1 概念说明

`ModelClaim` 这个 CR 表达的意图是：**「请把名为 X 的模型，在 warm GPU 池里激活成一个独立引擎进程」**。它的核心字段（来自 `ModelClaimSpec`）：

- `modelName`：客户端在请求里填的模型名（OpenAI 风格请求的 `model` 字段），省略时用对象名。
- `podSelector`：标签选择器，圈定一组 warm Pod 作为候选池，通常匹配 `pool.aibrix.ai/name=<池名>`。
- `artifactURL`：模型权重地址，如 `huggingface://Qwen/Qwen3-0.6B`、`s3://...`。
- `engine`：推理引擎，枚举 `vllm` / `sglang`，默认 `vllm`。
- `replicas`：期望的活跃实例数。**当前高密度路径只支持且默认为 1**（kubebuilder 标记里 `Minimum=1; Maximum=1`）。
- `engineConfig.args`：透传给引擎的 CLI 参数，如 `--max-model-len: "2048"`。

它解决的问题是 **GPU 利用率与冷启动**：与其为每个模型各起一个常驻 Deployment（GPU 大量空闲），不如让 warm 池的机器开着，按需把模型「激活」上去，多个小模型还能挤一张卡。

控制器与运行时边车之间是一套 **HTTP 协议**（不是 gRPC）。控制平面是客户端，运行时边车是服务端，固定监听 8080 端口。这套协议在 [runtime_client.go:31-35](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L31-L35) 的注释里明确：它「镜像」了 `ModelAdapter` 那套 runtime-sidecar 协议，区别只是这里激活的是完整引擎进程。

#### 4.1.2 核心流程

一次 reconcile 的主干（伪代码）：

```text
Reconcile(ModelClaim):
    取出 ModelClaim 对象
    if 正在删除:
        停掉所有引擎实例 → 清理指标 → 移除 finalizer → 结束
    if 还没有 finalizer:
        加上 finalizer → 立即 requeue（保证后续能继续走）

    校验 engineConfig 并行度合法性
    candidates = listCandidateWarmPods()      # 按 podSelector + warm 标签过滤
    pruneDeadInstances()                       # 剔除已不存在 Pod 的旧实例记录
    setStatusFields()                          # 刷新 candidates/desired 计数

    if desired > 当前实例数:
        ensureActivated()                      # 选 Pod + 调 Runtime.Activate
    if desired < 当前实例数:
        scaleDown()                            # 调 Runtime.Deactivate

    reconcileInstanceHealth()                  # 拉快照，按就绪态翻注解端口
    recomputeReadiness()                       # 算 ReadyReplicas / Phase
    Status().Update()
    reconcilePoolPolicies()                    # 可选的池化策略 tick

    return RequeueAfter 10s                    # 周期性兜底
```

其中有三个**关键设计**值得先记住：

1. **finalizer 保证清理**：删除 `ModelClaim` 前，控制器必须先调 `deactivate` 把引擎进程停掉、把引流注解删掉，否则会留下「僵尸引擎」继续吃 GPU 显存。
2. **「端口 0」门槛**：刚 `Activate` 完的引擎还在 boot/compile，**不能接客**。控制器先把 Pod 注解写成 `port: 0`，直到运行时快照确认引擎 `Ready` 才翻成真实端口。这保证网关永远不会把请求路由到一个还在启动的引擎。
3. **快照权威**：引擎到底活没活、端口是多少，**以运行时 `/snapshot` 返回的为准**，不靠控制器自己记的状态猜。

#### 4.1.3 源码精读

**(a) 注册与启动：受 Feature Gate 控制**

`ModelClaim` 控制器在 [pkg/controller/controller.go:64-65](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/controller.go#L64-L65) 注册，用 `ModelClaimController` 这个 Feature Gate 开关包裹（u2-l2 讲过该机制）。只有显式启用时，`modelclaim.Add` 才进入 `controllerAddFuncs`。

`Add` 函数除了 `For(&ModelClaim{})`，还 `Watches(&corev1.Pod{})`，目的是当 warm Pod 增减时，把同命名空间的 `ModelClaim` 全部重新入队——这样「一个待放置的模型」能在「新 Pod 上线」时立刻被挂上：

[pkg/controller/modelclaim/modelclaim_controller.go:115-117](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L115-L117) —— 监听 warm 池 Pod 的增删，触发候选集变化时重新 reconcile。

注意这里的 `Reconciler` 结构体持有一个 `Runtime RuntimeClient` 字段（[modelclaim_controller.go:78](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L78)）。它被故意设计成**可注入**，注释说「Injected so the reconcile loop is testable with an in-process fake」——即测试时用一个假的 runtime client 替换真实 HTTP 调用。这是本讲实践任务的关键入口。

**(b) Reconcile 主循环与 finalizer**

[pkg/controller/modelclaim/modelclaim_controller.go:136-230](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L136-L230) 是整个 reconcile。删除分支在 [143-153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L143-L153) 行：先 `deactivateInstances`、清指标、移除 finalizer。

finalizer 的添加有个**精妙的坑**值得一看：

[pkg/controller/modelclaim/modelclaim_controller.go:156-167](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L156-L167) —— 加 finalizer 后**显式 `Requeue: true`。

为什么？因为控制器的 watch predicate 只在 generation/label/annotation 变化时触发（见 [108-112](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L108-L112) 行）。而「只加一个 finalizer」既不改 generation 也不改 label/annotation，会被 predicate 过滤掉，**不会自动再入队**。不显式 requeue，模型就会卡在「加了 finalizer 但没激活」直到某个无关事件来触发。这正是上一讲的 predicate 过滤与「副作用更新」之间的典型冲突。

**(c) 列候选 warm Pod**

[pkg/controller/modelclaim/modelclaim_controller.go:245-287](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L245-L287) 的 `listCandidateWarmPods` 用 `podSelector` 选 Pod，再叠加四道**硬过滤**：

- [269](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L269)：必须有 `pool.aibrix.ai/enabled=true`，即必须是「开放认领」的 warm 池成员。
- [272](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L272)：`PodRunning`。
- [275](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L275)：有 `PodIP`（否则没法调 HTTP）。
- [281](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L281)：vLLM 模型的 TP×PP 并行度要与 Pod 的 GPU 数匹配。

这就是 **`pool_policy`（广义的池选择器）如何影响候选 Pod 集合**的第一层：`podSelector` 圈定大池，warm 标签 + 运行态 + GPU 拓扑进一步收窄成真正可用的 `candidates`。

**(d) 激活：调 Runtime.Activate + 端口 0 门槛**

[pkg/controller/modelclaim/modelclaim_controller.go:389-449](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L389-L449) 的 `ensureActivated` 是核心。循环里：

[413-424](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L413-L424) —— 构造 `ActivateRequest` 调 `r.Runtime.Activate(ctx, podIP, DefaultRuntimePort, req)`。注意请求里带了：
- `ModelName`（served 名）、`ArtifactURL`、`Engine`；
- `IPCName`：由 `ipcNameFor(pm)` 算出的 kvcached 共享内存段名，必须全 GPU 唯一（见 [placement.go:49-51](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L49-L51)，前缀 `kvc_` + 净化后的对象名）；
- **`ClaimRef`**：带上 ModelClaim 的 namespace/name/UID。这是后续用快照「认领」自己引擎的关键（下面会讲）。

调用成功后：

[435](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L435) 行 `r.annotateWarmPod(ctx, pm, pod, 0)` —— **刻意传 0**。引擎刚拉起、还在 boot/compile，此刻不可引流。

[439-443](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L439-L443) —— 把实例记为 `Phase: Activating`，**端口记的是运行时返回的真实端口** `resp.Port`（留待就绪后用），但注解里写的是 0。两处端口的分离是「端口 0 门槛」的实现要点。

**(e) 注解格式：网关引流的契约**

[pkg/controller/modelclaim/modelclaim_controller.go:601-619](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L601-L619) 的 `annotateWarmPodWithState` 写入注解：

[pkg/constants/model.go:73](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/model.go#L73) 定义 key 前缀 `modelclaim.aibrix.ai/`，拼上 ModelClaim 名；value 是 JSON：

```json
{"model":"qwen3-0.6b","port":9001,"state":"active"}
```

四个 state 见 [pkg/constants/model.go:78-81](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/model.go#L78-L81)：`active` / `activating` / `sleeping` / `failed`。网关缓存读这些注解决定「能不能路由」（单元 6/7 会讲）。`port:0` 意味着已知但不可路由（正在激活/不健康/休眠/失败）。

注释里提到一个细节：**「每个 ModelClaim 一个 key」**（而不是所有模型挤一个注解），是为了避免多写入方对同一注解的竞争。

**(f) 就绪收敛：快照权威**

[pkg/controller/modelclaim/modelclaim_controller.go:481-563](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L481-L563) 的 `reconcileInstanceHealth` 是「端口 0 门槛」的翻牌器。对每个实例拉一次 `r.Runtime.Snapshot(...)`，然后按快照里的真实状态决定 `desiredPhase` 和 `routingPort`：

[pkg/controller/modelclaim/modelclaim_controller.go:515-517](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L515-L517) —— 只有当 `observed.Ready && observedPort > 0` 时，才把 `routingPort` 设成真实端口、`desiredPhase` 设成 `Active`；否则一律保持 `port:0`。这就是「快照权威」：控制器**绝不**因为自己状态里写过 `Active` 就擅自引流，必须运行时点头。

还有一个**「认领」逻辑** `snapshotModelForClaim`（[569-588](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L569-L588)）：快照里可能同时有多个引擎，怎么知道哪个是「我的」？优先用 `ClaimRef.UID` 精确匹配 ModelClaim 的 UID；只有旧版运行时镜像不提供 ClaimRef 时，才退化用 served 名匹配。这避免了「同名模型在别处」的误配。

**(g) 协议结构体一览**

`RuntimeClient` 接口定义了 7 个方法（[runtime_client.go:194-202](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L194-L202)），对应端点路径见 [runtime_client.go:42-48](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L42-L48)：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `Activate` | `/v1/runtime/models/activate` | 拉起引擎进程 |
| `Deactivate` | `/v1/runtime/models/deactivate` | 停掉引擎进程 |
| `ListModels` | `/v1/runtime/models` | 列出本 Pod 上所有引擎 |
| `Snapshot` | `/v1/runtime/snapshot` | 拉 GPU/引擎状态快照（放置与健康判断的依据）|
| `SetKVLimit` | `/v1/runtime/models/kv-limit` | 设置某引擎的 KV 上限（池化策略用）|
| `Sleep` | `/v1/runtime/models/sleep` | 让 vLLM 引擎休眠 |
| `Wake` | `/v1/runtime/models/wake` | 唤醒休眠引擎 |

`ActivateRequest`（[runtime_client.go:63-79](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L63-L79)）和 `ActivateResponse`（[runtime_client.go:90-96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L90-L96)）是激活的请求/响应契约。响应里 `Status` 为 `success`/`error`，`Port` 是运行时**为引擎挑选的端口**（请求里传 0 表示让运行时自选空闲端口）。HTTP 实现在 [runtime_client.go:220-229](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L220-L229)，统一走 `postJSON`（[299-326](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L299-L326)），非 2xx 一律转成 error。

#### 4.1.4 代码实践

> **本讲主任务：追踪「控制平面通过 runtime_client 激活模型」的完整请求/响应流程。**

1. **实践目标**：在不真实部署的情况下，用现成的单测跑通真实 HTTP 路径，看清 `Activate` 到底发了什么、收了什么。
2. **操作步骤**：
   - 打开 [pkg/controller/modelclaim/runtime_client_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client_test.go) 的 `TestHTTPRuntimeActivate`（[43-72](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client_test.go#L43-L72) 行）。它用 `httptest.NewServer` 起一个假运行时，断言收到的路径正是 `/v1/runtime/models/activate`、请求体里的 `ModelName/IPCName/ClaimRef.UID/EngineConfig.Args` 都正确，然后回一个 `success` 响应。
   - 运行该测试：
     ```bash
     go test ./pkg/controller/modelclaim/ -run TestHTTPRuntimeActivate -v
     ```
   - 在脑中（或纸面）补全控制器的调用链：`Reconcile` → `ensureActivated`（[413-424](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L413-L424) 构造 `ActivateRequest`）→ `httpRuntimeClient.Activate`（[220-229](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L220-L229)）→ `postJSON`（[299-326](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/runtime_client.go#L299-L326)）→ 运行时 `/v1/runtime/models/activate`。
3. **需要观察的现象**：测试里断言响应端口是 `9123`、IPC 名回显一致；这说明「端口是运行时挑的、控制器只是接收」。把 `ActivateResponse.Status` 改成 `"error"`（仅在你本地实验里），你会看到 `Activate` 返回 error——对应控制器 [425-428](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L425-L428) 行的 `aerr != nil` 分支。
4. **预期结果**：测试通过，并理解「激活成功 ≠ 可引流」——成功后控制器仍把注解写成 `port:0`（[435](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L435) 行），要等 `reconcileInstanceHealth` 拉快照确认 Ready 才翻牌。
5. 若无法本地运行 Go 测试，明确标注「待本地验证」，改为纯源码阅读：对照 `ActivateRequest` 字段表与控制器构造代码，逐字段说明来源。

#### 4.1.5 小练习与答案

**练习 1**：为什么删除 `ModelClaim` 时要先 `deactivateInstances` 再移除 finalizer？如果顺序反了会怎样？

> **答案**：finalizer 是「删除的最后一道闸」。移除 finalizer 后 Kubernetes 会立即物理删除对象，控制器再也看不到它。若先移除 finalizer，引擎进程就成了没人管的「僵尸」，继续吃 GPU 显存，引流注解也残留在 Pod 上。所以必须趁对象还在时调 `deactivate` 停掉引擎、删注解，再移除 finalizer 放行删除。见 [modelclaim_controller.go:143-153](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L143-L153)。

**练习 2**：`Activate` 成功后，控制器把 Pod 注解端口写成 0、却在 `Status.Instances` 里记了真实端口 `resp.Port`。为什么要分开记两个值？

> **答案**：注解端口是给**网关**看的「能不能引流」开关，0 = 不可路由（引擎还在 boot）。而 status 里的真实端口是控制器自己留底「引擎在哪个端口」，等 `reconcileInstanceHealth` 通过快照确认 Ready 后，直接用这个真实端口去翻注解（见 [515-517](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L515-L517) 与 [530](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L530) 行 `inst.Port = observedPort`）。分开记避免了「刚激活就引流到没就绪的引擎」。

---

### 4.2 放置算法（Placement）

#### 4.2.1 概念说明

当 `ensureActivated` 需要挑一个 warm Pod 挂模型时，由 `placement.go` 的 `selectPodForActivationWithState` 决定选谁。它解决的问题是：**在一堆候选 Pod 里，挑一个「挂上去最划算」的**。

「划算」由两层信号决定：

- **运行时实时信号**（来自快照）：权重是否已在本 Pod 缓存、显存还剩多少、KV 用了多少、已经挂了几个模型。
- **历史负载信号**：该 Pod 上当前挂了多少个模型实例（跨所有 `ModelClaim`）。

放置算法故意做成**无副作用、可回退**：如果拿不到运行时快照（比如旧版边车不支持），就退化成纯「负载 + 名字」的确定性排序，照样能工作。

#### 4.2.2 核心流程

选择逻辑（伪代码）：

```text
selectPodForActivationWithState(candidates, alreadyOn, load, model, locality, states):
    best = nil
    for pod in candidates:
        if pod 已经挂着本模型:        # alreadyOn 防重复激活
            continue
        if 运行时状态能让 pod 排更前 (placementStateLess):
            更新 best
        else if 传统排序能让 pod 排更前 (rankLess):
            更新 best
    return best (or error "no available candidate")
```

排序的优先级是**字典序多关键字比较**：

1. **运行时状态层**（`placementStateLess`，[placement.go:137-157](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L137-L157)）：
   - 优先「权重已本地缓存」（`ArtifactCached`，省下载）；
   - 再优先「有快照数据」（`SnapshotKnown`）；
   - 再优先「有显存数据」（`MemoryKnown`）；
   - 显存数据相同时，**空闲 HBM 越大越优先**；
   - 再看 **KV 已用字节越少越优先**；
   - 再看 **已挂模型数越少越优先**。
2. **传统层**（`rankLess`，[placement.go:161-169](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L161-L169)）：`locality cost` → `load`（实例数）→ `name`（字典序兜底，保证确定性）。

「loc cost」来自 `LocalityProvider` 接口（[placement.go:74-76](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L74-L76)），当前默认实现 `uniformLocality` 恒返回 0（[80-82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L80-L82)），即「所有节点一样便宜」，为未来「节点级权重缓存」留接口。注释明确这是 Phase 1 的 load-only 回退。

#### 4.2.3 源码精读

**(a) 主选择函数**

[placement.go:100-132](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L100-L132) 遍历候选，[117-119](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L117-L119) 行用 `alreadyOn` 跳过已挂本模型的 Pod（避免一个模型在同一 Pod 上激活两次）。更新条件 [123-124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L123-L124) 是「运行时状态胜出」或「运行时打平后传统排序胜出」。

**(b) PodPlacementState：从快照摘要出排序输入**

[snapshot.go:96-103](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L96-L103) 定义 `PodPlacementState`，它**不存进 CRD**，因为运行时边车才是权威源。`placementStateFromSnapshot`（[105-133](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L105-L133)）把快照转成这个摘要：

- [107-112](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L107-L112)：检查 `artifactURL` 是否在快照的 `CachedArtifacts` 里。
- [119-128](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L119-L128)：算可用 HBM。**注意单 GPU 与多 GPU 的不同**——单 GPU 引擎要最大空闲槽位（取 `max`），而固定 TP/PP 组要用全部 GPU、安全余量取「最不空的 rank」（取 `min`）。注释解释得很清楚：取聚合值或最大值会误导。

**(c) 快照缓存：减少边车压力**

控制器的 `collectPlacementStates`（[modelclaim_controller.go:451-474](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L451-L474)）对每个候选 Pod 取快照。为避免每个 tick 都打 HTTP，`runtimeSnapshotCache`（[snapshot.go:36-92](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L36-L92)）做了 5 秒 TTL 缓存，且**刷新失败时绝不返回过期快照**（[73-82](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L73-L82) 行）——避免用过期显存数据做错误放置。

**(d) 跨声明周期负载统计**

`computePodLoad`（[modelclaim_controller.go:690-703](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L690-L703)）列出**同命名空间所有 `ModelClaim`**，统计每个 Pod 上挂了几个实例。这就是「least-loaded bin-packing」的负载来源。

#### 4.2.4 代码实践

1. **实践目标**：手算一次放置决策，理解排序键。
2. **操作步骤**：假设有 3 个候选 warm Pod，`ModelClaim A` 要挂上去，且都不在 `alreadyOn` 里。已知：

   | Pod | ArtifactCached | HBMFreeBytes | KVUsedBytes | ModelCount | load（实例数）|
   | --- | --- | --- | --- | --- | --- |
   | p1 | true  | 10 GB | 2 GB | 1 | 1 |
   | p2 | false | 20 GB | 1 GB | 0 | 0 |
   | p3 | false | 20 GB | 1 GB | 0 | 0 |

3. **需要观察的现象**：用 `placementStateLess` 逐对比较。
4. **预期结果**：**p1 胜出**。因为 `ArtifactCached` 是最高优先级键，p1 命中权重缓存（省一次下载），即便 p2/p3 显存更空、负载更低。这正是 [placement.go:138-140](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L138-L140) 的 `if a.ArtifactCached != b.ArtifactCached { return a.ArtifactCached }`。若把 p1 的 `ArtifactCached` 也设为 false，则 p2 与 p3 在运行时层全部打平，交给 `rankLess`：loc 相同（uniform）、load 相同（都是 0）、最后比 name → p2 < p3，p2 胜。
5. **待本地验证**：可写一个表格驱动的单测断言上述结果，模板见 [placement_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement_test.go)。

#### 4.2.5 小练习与答案

**练习 1**：如果运行时边车是旧版本、不支持 `/snapshot`（`states` 为空 map），放置算法还能工作吗？会怎么选？

> **答案**：能。`collectPlacementStates` 拿不到快照时返回空 map（[modelclaim_controller.go:458-460](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L458-L460)），所有 Pod 的 `PodPlacementState` 都是零值，`placementStateLess` 两两返回 false（[156](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/placement.go#L156) 行），完全交给 `rankLess` 的 load + name 排序。这就是注释说的「deterministic fallback」。

**练习 2**：为什么多 GPU（TP/PP > 1）时可用 HBM 取所有 rank 的**最小值**，而不是平均值或最大值？

> **答案**：固定 TP/PP 组会用满每个可见 GPU，整体能否放下取决于「最不空的那个 rank」（木桶效应）。取平均或最大都会高估可用空间，导致放置后某张卡 OOM。见 [snapshot.go:122-125](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/snapshot.go#L122-L125) 的 `parallelism > 1 && accelerator.HBMFreeBytes < state.HBMFreeBytes` 分支。

---

### 4.3 池化策略（Pool Policy）

#### 4.3.1 概念说明

「池化策略」解决的是**多引擎共享一张 GPU 时的资源治理**，分两件事：

- **reclaim（回收）**：给一张 GPU 设一个总 KV 预算，按各引擎的实时活跃度把预算**弹性**分给它们（忙的多分、闲的少分）。这是 kvcached「弹性 KV 分配」的体现——你不能再用 `--gpu-memory-utilization` 硬切（[parallelism.go:39-41](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/parallelism.go#L39-L41) 会直接报错）。
- **lifecycle（生命周期）**：引擎空闲超过 `sleepAfterSeconds` 秒就调 vLLM sleep 把它「停薪留职」，腾出显存；来请求时再异步唤醒。

**关键设计抉择：策略不做成 CRD，而是 warm-pool Deployment 上的一个 JSON 注解**（`pool.aibrix.ai/policy`）。理由见 [pkg/constants/model.go:58-62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/model.go#L58-L62) 的注释：配置天然属于「拥有这批 GPU Pod 的那个池」，没必要再造一个资源对象。

样例注解（来自 [samples/modelclaim/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/modelclaim/README.md) 第 61 行）：

```yaml
metadata:
  annotations:
    pool.aibrix.ai/policy: '{"reclaim":{"mode":"kv-first","capacityBytes":17179869184}}'
```

#### 4.3.2 核心流程

池化策略的执行是一个**可选的、独立于激活的**控制循环（[modelclaim_controller.go:225-228](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L225-L228) 注释强调：它在 status 持久化**之后**跑，这样策略失败**不会阻塞**激活或路由收敛）：

```text
reconcilePoolPolicies(candidates):
    for 每个候选 Pod:
        poolDeployment = 沿 ReplicaSet 找到 owning Deployment
        若该 Deployment 已处理过: 跳过            # 一个池只跑一次/tick
        policy = parsePoolPolicy(Deployment 注解)  # 失败即 fail-closed
        if 限频器允许 (begin):
            reconcilePoolPolicy():
                for 每个 warm Pod:
                    snapshot = Runtime.Snapshot()
                    if 非单 GPU: 跳过              # 当前只验证单 GPU
                    observe 活动计数 deltas
                    if reclaim: computePoolKVTargets → 对每个引擎 SetKVLimit
                    if lifecycle: 对空闲引擎 Sleep
```

**KV 预算分配算法**（`computePoolKVTargets`，[pool_policy.go:169-247](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L169-L247)）的数学：

设总预算 \( C \)（capacityBytes）、保底百分比 \( g \)（guaranteedFloorPercent，0~100）、各引擎当前已用 \( u_i \)、活动权重 \( w_i \)。

1. **每个引擎的保底** \( b_i = \max(\text{floor}, u_i) \)，其中 \(\text{floor} = C \cdot g / 100\)。当前用量是**硬下限**——不能为了省内存强行缩一个正在用的引擎。
2. 若 \( \sum b_i > C \)，说明「已用量 + 保底」已超预算，**返回空计划**（拒绝缩，[200-202](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L200-L201)），安全优先。
3. 若**没有任何活跃引擎**（无压力信号），也返回空计划——保留现有上限，避免「控制器刚重启/恰好静默」就误缩。
4. 剩余预算 \( R = C - \sum b_i \) 按权重 \( w_i \) 分给活跃引擎：

\[
\text{grant}_i = R \cdot \frac{w_i}{\sum w_j}, \qquad
w_i = 1 + \text{bounded}(\text{inFlight}_i) + \text{bounded}(\text{completionDelta}_i)
\]

其中 `bounded` 把活动计数夹在 \([0,4]\)（[249-257](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L249-L257) 行），防止单个引擎的突发计数淹没其他引擎。

5. **取整余数**确定性分配（[242-245](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L242-L245) 行）：按名字排序把剩下的几个字节依次加给前面的引擎，避免连续 tick 因一字节之差来回抖动。

#### 4.3.3 源码精读

**(a) 注解解析：fail-closed**

`parsePoolPolicy`（[pool_policy.go:98-148](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L98-L148)）用 `decoder.DisallowUnknownFields()`（[100](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L100) 行）——**拼错字段名会被拒绝**，而不是静默忽略。注释点明动机：「A typo must disable policy safely rather than silently changing GPU memory behavior」。校验分多个错误类（`poolPolicyError*` 常量，[35-42](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L35-L42) 行），低基数以便 Event 去重。

**(b) 找 owning Deployment**

`poolDeploymentForPod`（[pool_policy_controller.go:196-218](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L196-L218)）：Pod → ReplicaSet（owner）→ Deployment（owner）。注解挂在 Deployment 而非 Pod 上，所以要把 Pod 反向追溯到它的 Deployment 才能读到策略。`reconcilePoolPolicies`（[165-194](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L165-L194)）用 `seen` map 保证一个 Deployment 每个 tick 只处理一次。

**(c) 执行：限频 + 单 GPU 约束 + 幂等**

`reconcilePoolPolicy`（[258-357](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L258-L357)）：

- [289-295](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L289-L295)：**只处理单 GPU**（`len(snapshot.Accelerators) != 1` 跳过）。注释说多 GPU 的 kvcached 记账还没验证，保守起见跳过。
- [325-327](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L325-L327)：调 `SetKVLimit`，每个操作带一个**确定性 `OperationID`**（[321-324](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L321-L324) 行，由 pool/pod/model/target/time 组成），让运行时侧能识别「这是同一个操作的重试」从而幂等。

**(d) 活动计数器：进程内、非持久**

`poolPolicyManager`（[pool_policy_controller.go:39-45](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L39-L45)）只存进程内存。注释（[35-38](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L35-L38) 行）解释：控制器重启会清空计数基线，从而**延迟**基于活动的决策直到下一个快照到来——这比从陈旧注解/status 重建活动状态**更安全**。`observe`（[110-143](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L110-L143)）用两次快照的 `RequestSuccessTotal` 差值算 `CompletionDelta`，用 `RequestsRunning + RequestsWaiting` 算在途。

**(e) 空闲休眠**

`reconcilePoolIdleSleep`（[402-472](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L402-L472)）：对空闲超阈的引擎，先 `annotateWarmPodWithState(..., Sleeping)` **去路由**（避免 sleep 期间还来请求），再调 `Runtime.Sleep`（[451](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L451) 行），成功后把实例标记 `Sleeping`。若 sleep 失败会**回滚注解**恢复路由（[454-458](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L454-L458)）。来请求时由网关触发异步 wake（README 第 43-46 行：返回 HTTP 503 + `Retry-After`，不持有原请求）。

#### 4.3.4 代码实践

1. **实践目标**：手算一次 KV 预算分配，理解 `computePoolKVTargets` 的「保底 + 按权重分剩余」。
2. **操作步骤**：设总预算 \( C = 16\,\text{GB} = 17179869184 \) 字节，保底百分比 \( g = 10\% \)。一张 GPU 上有两个**活跃**引擎 A、B：

   | 引擎 | KVUsedBytes \(u\) | inFlight | completionDelta |
   | --- | --- | --- | --- |
   | A | 2 GB | 3 | 2 |
   | B | 5 GB | 0 | 0 |

   - 保底 floor \( = 16 \times 10\% = 1.6\,\text{GB} \)。
   - \( b_A = \max(1.6, 2) = 2\,\text{GB} \)，\( b_B = \max(1.6, 5) = 5\,\text{GB} \)。合计 \( \sum b = 7\,\text{GB} < 16\,\text{GB} \)，未超预算。
   - 剩余 \( R = 16 - 7 = 9\,\text{GB} \)。
   - 权重：\( w_A = 1 + \min(3,4) + \min(2,4) = 6 \)，\( w_B = 1 + 0 + 0 = 1 \)，\( \sum w = 7 \)。
   - 分配：\( \text{grant}_A = 9 \times 6/7 \approx 7.71\,\text{GB} \)，\( \text{grant}_B = 9 \times 1/7 \approx 1.29\,\text{GB} \)。
   - 最终目标：\( \text{target}_A \approx 9.71\,\text{GB} \)，\( \text{target}_B \approx 6.29\,\text{GB} \)。
3. **需要观察的现象**：A 更忙（在途 + 完成增量都高），所以拿到大部分剩余预算；B 几乎闲着，但仍保留 5 GB 的「已用硬下限」不被强行缩。
4. **预期结果**：验证了「保底优先 + 剩余按活跃度加权」的设计。把 B 改成 `RequestMetricsObserved=false`（无活动观测），则 `computePoolKVTargets` 因 `hasActiveModel` 仍为 true（A 活跃）会继续；但若**两个都没有活动观测**，则返回空计划（保留现有上限）。可对照 [pool_policy_test.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_test.go) 写表驱动断言。
5. **待本地验证**：上述数值用字节精确计算时会有取整余数分配（[242-245](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy.go#L242-L245) 行），本地用 Go 跑 `computePoolKVTargets` 才能得到字节精确结果。

#### 4.3.5 小练习与答案

**练习 1**：为什么池化策略做成 Deployment 注解，而不是像 `ModelClaim` 一样做成独立 CRD？

> **答案**：因为策略天然属于「拥有这批 GPU Pod 的那个 warm 池（Deployment）」，配置和它治理的对象强绑定。用注解避免了多造一个资源对象，也避免了「策略 CR」与「池 Deployment」之间的一致性问题。见 [pkg/constants/model.go:58-62](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/constants/model.go#L58-L62)。

**练习 2**：池化策略的执行为什么放在 reconcile 的**最后**（status 持久化之后）？

> **答案**：策略是「锦上添花」的弹性治理，不是激活的必要条件。把它放在最后、且任何错误都只记日志/Event + 下个 tick 重试（[190-193](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L190-L193)），保证「策略解析失败/限流失败」**不会**阻塞模型激活和路由就绪收敛。见 [modelclaim_controller.go:225-228](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/modelclaim_controller.go#L225-L228) 的注释。

**练习 3**：控制器重启后，池化策略的活动计数基线会怎样？

> **答案**：清空。`poolPolicyManager` 是进程内的（[pool_policy_controller.go:39-45](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L39-L45)），重启后没有上一个 `RequestSuccessTotal` 基线，`observe` 不会立即产生 `CompletionDelta`，从而**延迟**空闲休眠等决策，直到下一个快照建立基线。这是刻意的「安全优先」，避免用陈旧数据误判引擎空闲。

---

## 5. 综合实践

**任务：用一条 YAML 串起「warm 池 + 两个 ModelClaim + 一个池化策略」，并预测控制器的每一步行为。**

1. 阅读 [samples/modelclaim/warm-runtime-pool.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/modelclaim/warm-runtime-pool.yaml) 与 [samples/modelclaim/modelclaims.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/modelclaim/modelclaims.yaml)。
2. 假设你在 warm-pool Deployment 上再加一条注解：
   ```yaml
   metadata:
     annotations:
       pool.aibrix.ai/policy: '{"reclaim":{"mode":"kv-first","capacityBytes":17179869184,"guaranteedFloorPercent":10},"lifecycle":{"sleepAfterSeconds":300}}'
   ```
3. 在一张纸上画出以下时间线（**纯源码阅读型实践，不要求真实部署**）：
   - **T0**：apply 两个 `ModelClaim`（qwen3-0.6b、qwen2.5-0.5b），都 `replicas:1`，`podSelector` 都指向同一个单 Pod 池。
   - **T1**：控制器为 qwen3 选 Pod（只有一个候选）→ 调 `Activate` → 注解写 `port:0`、`state:activating`。
   - **T2**：快照报告 qwen3 的引擎 `Ready` → 注解翻成真实端口、`state:active`；`Status.Phase=Active`。
   - **T3**：qwen2.5 走同样流程，**但放置算法会发现同一 Pod 上已有 1 个实例（load=1）**——因为只有一个 Pod，仍选它，两个引擎共享一张 GPU。
   - **T4**：池化策略 tick：拉快照，两个引擎都活跃 → `computePoolKVTargets` 按 16 GB 预算分配 → 各调一次 `SetKVLimit`。
   - **T5**：qwen3 持续 5 分钟无请求 → `reconcilePoolIdleSleep` 把它注解改为 `state:sleeping`、`port:0` → 调 `Sleep`。
   - **T6**：删除 qwen3 的 `ModelClaim` → `deactivateInstances` 停掉 qwen3 引擎、删注解 → 移除 finalizer。
4. **自检问题**：
   - T2 中，网关凭什么知道 qwen3 现在能引流了？（答：Pod 注解的 `port` 从 0 变成真实端口、`state` 变 `active`。）
   - T3 中，放置算法的 `alreadyOn` 会阻止 qwen2.5 激活吗？（答：不会，`alreadyOn` 只阻止「同一模型在同一 Pod 重复激活」，不同模型互不影响。）
   - T5 中，若 `Sleep` 调用失败会怎样？（答：回滚注解恢复 `active` 路由，见 [pool_policy_controller.go:454-458](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/pkg/controller/modelclaim/pool_policy_controller.go#L454-L458)。）
5. 若有真实集群（kvcached-enabled vLLM 镜像），可按 [samples/modelclaim/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/samples/modelclaim/README.md) 的命令实际 apply 并用 `kubectl get modelclaims -w` 与 `kubectl get pods ... -o jsonpath=...{.metadata.annotations}` 观察上述时间线；否则标注「待本地验证」。

## 6. 本讲小结

- `ModelClaim` 让运行时边车为模型**拉起一整个独立引擎进程**，与 `ModelAdapter`（挂 LoRA）正交；多个 `ModelClaim` 可借 kvcached 共享一张 GPU，实现高密度低成本推理。
- reconcile 主循环：列候选 warm Pod → 激活/缩容 → 按运行时**快照**做就绪收敛 → 刷新 status → 跑池化策略；finalizer 保证删除时优雅停机。
- **「端口 0 门槛」**：刚激活的引擎注解端口写 0（不可路由），直到快照确认 `Ready` 才翻成真实端口，杜绝网关把请求打到还在 boot 的引擎。
- 控制平面 ↔ 运行时边车是 **7 个 HTTP 端点**的协议（`activate`/`deactivate`/`snapshot`/`list`/`kv-limit`/`sleep`/`wake`），`RuntimeClient` 接口可注入假实现便于测试。
- **放置算法**用「运行时状态（权重缓存 > 显存 > KV > 模型数）+ 负载 + 名字」的字典序排序选 Pod，拿不到快照时安全回退。
- **池化策略**是 warm-pool Deployment 上的一个 JSON 注解（非 CRD），fail-closed 解析；`computePoolKVTargets` 用「保底下限 + 剩余按活跃度加权」分配 KV 预算，进程内活动计数器重启即清空，安全优先。

## 7. 下一步学习建议

- **横向对照**：回到 u4-l1 的 `ModelAdapter`，对比 `lora_client` 与本讲的 `runtime_client`，体会「挂适配器」与「起引擎进程」两套协议的异同。
- **往下深入运行时**：本讲的 HTTP 服务端在 Python 运行时边车里。建议进入单元 9 的 **u9-l1（AI Runtime 边车与引擎生命周期）**，看 `model_runtime.py` / `engine_registry.py` 如何实现 `/v1/runtime/models/activate` 等端点的真正逻辑（下载权重、fork 引擎进程、kvcached 接入）。
- **网关侧的呼应**：本讲反复提到「网关读注解决定引流」。到单元 6（u6-l1 中央缓存）和单元 7（u7-l1 网关 ExtProc 入口）看 `pkg/cache` 如何把 warm Pod 上的 `modelclaim.aibrix.ai/*` 注解翻译成可路由的目标。
- **KV Cache 深水区**：`SetKVLimit` / kvcached / IPC 名到底如何影响显存？进入单元 10 的 **u10-l1（分布式 KV Cache 架构）** 看 `aibrix_kvcache` 的 cache_manager 与 L1/L2 两级缓存。
- **CRD 与校验**：若想给 `ModelClaim` 加字段或改校验，复习 u2-l3 的 kubebuilder 标记机制，以及 `parallelism.go` 里 `--gpu-memory-utilization` 与 kvcached 冲突的校验范式。
