# Gateway API 推理扩展（Inference）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 NGF 为什么要为 AI/LLM 工作负载单独引入一套推理扩展机制，它与普通 HTTPRoute 后端有什么本质区别。
- 读懂 `endpoint-picker` 子命令：它是一个跑在数据面 Pod 里的「HTTP↔gRPC ext_proc 翻译器」，并解释为什么必须用它而不能让 NGINX 直接调用 EPP。
- 读懂 `InferencePool` 这条 CRD 在图（`graph.Graph`）里是如何被发现、引用、校验并回写状态的。
- 把整条请求链路串起来：NGINX 外部 location → NJS 模块 → Go shim → gRPC ext_proc → EPP → 选定 endpoint → 内部 location `proxy_pass`，并指出每一步用到的 HTTP 头。

本讲是 **advanced** 阶段的内容，依赖你已经学过 u5-l2（路由与监听器）和 u4（事件管线/图构建）的核心结论。本讲不会重复「Source+Valid+Conditions 节点三元组」「批处理双缓冲」等前置概念，需要时请回看对应讲义。

## 2. 前置知识

在进入源码之前，先用大白话建立三个直觉。

### 2.1 普通 Service 后端 vs 推理后端

在 u5-l3 里你已经见过：一条 HTTPRoute 的 `backendRefs` 指向一个 Kubernetes `Service`，NGF 解析出 `EndpointSlice`，生成一个 NGINX `upstream`，NGINX 用默认的 `random two least_conn` 负载均衡挑一个 endpoint 转发。这套机制对普通 Web 服务足够好。

但 AI/LLM 推理工作负载不一样：

- 它们往往跑在 GPU 上，**单条请求的代价极高**，简单轮询会浪费昂贵的算力。
- 选哪个副本（endpoint）应该综合考虑**模型名、队列长度、KV-cache 命中、显存余量**等因素，而不是只看连接数。
- 这些决策需要**专门的组件**来做。

Gateway API 社区给出的方案是 **[Gateway API Inference Extension](https://gateway-api-inference-extension.sigs.k8s.io/)**：引入一个叫 **Endpoint Picker（EPP）** 的组件，由它根据请求内容和实时指标决定「这条请求该发往哪个推理副本」，再把选中的 endpoint 通过**响应头**告诉数据面网关（NGINX）。

### 2.2 EPP 用的是 gRPC ext_proc 协议，而 NGINX 说不了

EPP 和网关之间用的是 Envoy 的 **[ext_proc（External Processing）gRPC 协议](https://github.com/kubernetes-sigs/gateway-api-inference-extension/tree/main/docs/proposals/004-endpoint-picker-protocol)**：网关把请求头和请求体通过一条 gRPC 双向流发给 EPP，EPP 处理后用「设响应头（set header）」的方式把选中的 endpoint 回传。

问题来了：**NGINX（含 NJS）原生不会发起 gRPC ext_proc 调用**。所以 NGF 的折中方案是——在数据面 Pod 里多塞一个用 Go 写的「垫片（shim）」进程，它对外暴露一个**普通 HTTP 接口**（NGINX 的 NJS 用 `ngx.fetch` 就能调），对内把这个 HTTP 请求**翻译成一条 gRPC ext_proc 流**去打 EPP。这个 shim 就是 `endpoint-picker` 子命令。

### 2.3 InferencePool：一个「伪装成 Service 的后端」

EPP 知道一个推理池（InferencePool）里有哪些副本，但 NGF 的视角下，`InferencePool` 是一种**特殊的 backendRef 目标**——它在图里被当作「一个后端」处理，最终让 NGINX 不自己挑 endpoint、而是问 EPP 要一个 endpoint。

官方设计文档（[docs/proposals/gateway-inference-extension.md:62-66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/proposals/gateway-inference-extension.md#L62-L66)）还提到一个细节：NGF 仍会**为 InferencePool 的副本建一个上游（upstream）**，目的是支持 `FailOpen`（EPP 挂了也能兜底）以及**校验 EPP 返回的 endpoint 是否可信**。这也是为什么后面你会看到 `$inference_backend_*` 这样的变量和一张 `map`。

> 关键术语速查：**EPP**（Endpoint Picker，选副本的组件）、**ext_proc**（Envoy 外部处理 gRPC 协议）、**shim/垫片**（`endpoint-picker` Go 进程，HTTP↔gRPC 翻译）、**InferencePool**（推理池 CRD，当后端用）、**headless shadow Service**（NGF 为池副本建的影子服务，复用既有 EndpointSlice 逻辑）。

## 3. 本讲源码地图

本讲涉及的关键文件，按职责分组：

| 文件 | 职责 |
| --- | --- |
| [cmd/gateway/endpoint_picker.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go) | shim 主体：HTTP server + gRPC ext_proc 客户端 |
| [internal/framework/types/types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/types/types.go) | shim 用到的头常量与端口常量 |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | `endpoint-picker` 子命令的 cobra 装配 |
| [internal/controller/nginx/modules/src/epp.js](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/modules/src/epp.js) | NGINX 侧 NJS 模块：调用 shim、回填 endpoint |
| [internal/controller/state/graph/inferencepools.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go) | InferencePool 在图中的引用收集与校验 |
| [internal/controller/state/graph/graph.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go) | 图构建总入口，调用上述模块 |
| [internal/controller/state/graph/backend_refs.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go) | 把 InferencePool 引用「加肥」成带端口和 EPP 配置的后端引用 |
| [internal/controller/nginx/config/servers.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go) | 生成调用 EPP 的内部 location |
| [internal/controller/nginx/config/maps.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go) | `$inference_workload_endpoint` → `$inference_backend_*` 的兜底映射 |
| [internal/controller/state/conditions/conditions.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go) | InferencePool 的 Accepted/ResolvedRefs 条件工厂 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | InferencePool 控制器的特性开关注册 |
| [internal/controller/provisioner/objects.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go) | 把 shim 作为 sidecar 注入数据面 Deployment |
| [docs/proposals/gateway-inference-extension.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/proposals/gateway-inference-extension.md) | 官方设计提案（EP-3716） |

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：

1. **endpoint-picker shim**（4.2）：HTTP↔gRPC ext_proc 翻译器。
2. **InferencePool 图处理**（4.3）：CRD 如何进图、如何校验、如何回写状态。
3. **NGINX↔EPP 头交互**（4.4）：NJS + Go shim + ext_proc 的全链路头协议。

开讲前，先用 4.1 把「整条请求链路」的全景图钉在墙上，后面三个模块都是在填这张图里的某一段。

### 4.1 全景：一次推理请求经过哪些环节

这是本讲的「地图」，建议先记住再读源码：

```
Client ──HTTP──▶ NGINX (外部 location: js_content epp.getEndpoint)
                       │
                       │ ① NJS 用 ngx.fetch 打 http://127.0.0.1:54800
                       ▼
                 Go shim (endpoint-picker 子命令, 同一个 Pod)
                       │
                       │ ② 读 X-EPP-Host / X-EPP-Port, 拨号 gRPC
                       ▼
                 EPP (Endpoint Picker Pod, ext_proc gRPC 服务)
                       │
                       │ ③ 在响应的 set-header 里回填 X-Gateway-Destination-Endpoint
                       ▼
                 Go shim ──把该头写回 HTTP 响应──▶ NJS
                       │
                       │ ④ NJS 把 endpoint 存进 $inference_workload_endpoint
                       ▼
                 NJS internalRedirect 到内部 location
                       │
                       │ ⑤ 内部 location: proxy_pass http://$inference_backend_<pool>
                       ▼
                 AI 工作负载副本（EPP 选中的那个 endpoint）
```

四个关键事实先记下：

- shim 监听在 **`127.0.0.1:54800`**，只在 Pod 内可见——NGINX 和 shim 同处一个 Pod（数据面 Deployment 的两个容器）。
- NGINX 和 shim 之间用 **HTTP** 通信；shim 和 EPP 之间用 **gRPC ext_proc** 通信。
- 选中的 endpoint 通过一个固定响应头 **`X-Gateway-Destination-Endpoint`** 在链路上回传。
- 整条链路对 NGF 控制面来说是「配置生成」问题：它只需生成正确的 location/map/upstream，真正的选路发生在**数据面运行时**，控制面不参与每次选副本。

接下来逐段拆开。

### 4.2 endpoint-picker shim：HTTP↔gRPC ext_proc 翻译器

#### 4.2.1 概念说明

shim 的存在意义只有一个：**NGINX 不会说 gRPC ext_proc，但会说 HTTP**。所以 shim 充当一个协议网关：

- **对外（面向 NGINX/NJS）**：是一个极简的 HTTP server，只在本机回环地址监听。
- **对内（面向 EPP）**：是一个 gRPC 客户端，用 ext_proc 协议的 `Process` 双向流和 EPP 交换数据。

每来一个请求，shim 就**临时拨一条 gRPC 连接**到 EPP（不池化连接），把 HTTP 请求头和请求体翻译成 ext_proc 的 `ProcessingRequest` 发过去，再把 EPP 回填的「选中 endpoint」头翻译回 HTTP 响应头返回给 NJS。

#### 4.2.2 核心流程

shim 处理单个请求的流程（伪代码）：

```text
收到 HTTP 请求 r:
  1. 从 r.Header 读 X-EPP-Host、X-EPP-Port；缺一即 400 退出
  2. target = host:port
  3. 用 factory 拨一条 gRPC 连接到 target，得到 ext_proc client（+ close 函数）
  4. client.Process(ctx) 打开 ext_proc 双向流 stream
  5. sendRequest(stream, r):
       a. buildHeaderRequest: 把 r.Header 全部转成 ext_proc RequestHeaders（键统一转小写）
       b. buildBodyRequest:   把 r.Body 包成 RequestBody（空 body 视为错误）
       c. CloseSend
  6. 循环 stream.Recv():
       - 若 ImmediateResponse：把 EPP 的错误码/正文当 HTTP 错误返回
       - 否则从 RequestHeaders 的 SetHeaders 里找键 == DestinationEndpointKey 的头，
         把它的值写进 HTTP 响应头（同名回传）
  7. 写 200 OK，返回
```

注意第 5.a 步：HTTP 头键被**统一转成小写**，因为 gRPC 跑在 HTTP/2 之上，而 HTTP/2 规定头名必须小写（RFC 7540 §8.1.2），同时 EPP 也按小写匹配。

#### 4.2.3 源码精读

先看监听端口和头常量，这是整条链路的「地址簿」：

[internal/framework/types/types.go:9-18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/types/types.go#L9-L18) —— 定义了 NGINX↔shim 通信用的两个请求头（`X-EPP-Host`/`X-EPP-Port`）和 shim 的监听端口 `54800`。注释里还顺手解释了 54800 的来历（把 "nginx" 的 ASCII 码求和再乘 100）。

shim 的 HTTP server 只绑回环：

[cmd/gateway/endpoint_picker.go:28-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L28-L35) —— `endpointPickerServer` 把 `Addr` 写死成 `127.0.0.1:<GoShimPort>`，因此 shim **只对本 Pod 内的进程可见**，集群外任何东西都打不到它。`ReadHeaderTimeout: 10s` 防慢速攻击。

读头、拨号、开流是 handler 的主干：

[cmd/gateway/endpoint_picker.go:61-89](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L61-L89) —— handler 第一步就从请求头取 `X-EPP-Host`/`X-EPP-Port`（决定这次要打哪个 EPP），拼成 `host:port` 后调 `factory(target)` 拨 gRPC 连接。`defer closeConn()` 保证每次请求结束都关连接——印证了「每请求一连接」的设计。

> 这里有一个值得品的设计：**为什么要把 host/port 放在请求头里、而不是固定配置？** 因为同一个数据面 Pod 可能为多个 InferencePool 服务，而**每个 InferencePool 有自己的 EPP**。NGINX 根据匹配到的后端，在 location 里用 `set $epp_host ...` 决定这次该问哪个 EPP（见 4.4）。

接着是 ext_proc 流的收发：

[cmd/gateway/endpoint_picker.go:91-131](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L91-L131) —— 打开 `Process` 流后，先 `sendRequest` 把头和体发过去，再循环 `Recv`。这段处理了 ext_proc 协议的两种响应：一种是 `ImmediateResponse`（EPP 直接拒掉请求，如校验失败），shim 把它的状态码和正文透传成 HTTP 错误；另一种是正常流，shim 在 `RequestHeaders.Response.HeaderMutation.SetHeaders` 里**翻找**键等于 `eppMetadata.DestinationEndpointKey`（即 `X-Gateway-Destination-Endpoint`）的头，把它**原样写回 HTTP 响应头**。

请求构造里最值得注意的是头键小写化：

[cmd/gateway/endpoint_picker.go:157-187](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L157-L187) —— `buildHeaderRequest` 遍历 `r.Header`，用 `strings.ToLower(key)` 把每个头键规范化，再装进 ext_proc 的 `HeaderValue`。注释明确说了：这解决的是 Go 默认 Title-Case 与 EPP 期望小写的不一致，并满足 HTTP/2 头名必须小写的硬性要求。

最后看 gRPC 客户端工厂的 TLS 选项：

[cmd/gateway/endpoint_picker.go:37-58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L37-L58) —— `realExtProcClientFactory` 按 `disableTLS` 二选一：要么明文（`insecure.NewCredentials()`），要么 TLS（可配 `InsecureSkipVerify`）。这两个开关通过命令行 flag 暴露。

命令装配与 flag 在 commands.go：

[cmd/gateway/commands.go:950-987](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L950-L987) —— `createEndpointPickerCommand` 装出 `endpoint-picker` 子命令，`RunE` 里依次构造 logger、handler、server。`addEPPConnectionFlags` 注册 `--endpoint-picker-disable-tls` 和 `--endpoint-picker-tls-skip-verify`（默认 **true**）。注意注释里那行 `REQUIRED: Must be true until ... EndpointPicker supports mounting certificates.` —— 这说明当前 EPP 还不能方便地挂证书，所以 shim 默认跳过证书校验，是个**待改进的安全妥协**。

#### 4.2.4 代码实践

**实践目标**：把 shim 的「翻译」职责与它的输入/输出协议对应起来，能在源码里指出「哪个头从哪来、到哪去」。

**操作步骤**：

1. 打开 `cmd/gateway/endpoint_picker.go`，在 `createEndpointPickerHandler`（L61 起）里用笔标注三件事：
   - 输入头：`X-EPP-Host`、`X-EPP-Port`（来自 NJS）。
   - 输出头：`eppMetadata.DestinationEndpointKey`（回写给 NJS）。
   - 拨号目标：`net.JoinHostPort(host, port)`（即 EPP 的地址）。
2. 打开测试 `cmd/gateway/endpoint_picker_test.go`，找到设置 `X-EPP-Host: test-host`、`X-EPP-Port: 1234` 并断言响应头 `DestinationEndpointKey == "test-value"` 的用例（约 L127-L136）。

**需要观察的现象**：测试里**注入**了一个假的 ext_proc client（替换 `realExtProcClientFactory`），让它在响应头里返回 `test-value`，于是断言 shim 能把这个值**透传**到 HTTP 响应头。这正好验证了「shim 不做选路决策，只搬运头」。

**预期结果**：你会确认 shim 是一个**无状态协议翻译器**——它对推理一无所知，只负责把 HTTP 头/体塞进 ext_proc 流、把 EPP 的 set-header 捞出来回传。

**待本地验证**：若你想真跑一次，需要先 `make build` 得到 `gateway` 二进制，再手动 `./gateway endpoint-picker --help` 查看两个 flag；但要让它真正工作，还需要一个 EPP 进程在某个 `host:port` 监听 ext_proc，这通常需要一个完整的推理环境（参考 [Gateway API Inference Extension Getting Started](https://gateway-api-inference-extension.sigs.k8s.io/guides/)），多数学习者无需实际部署。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `buildHeaderRequest` 里的 `strings.ToLower(key)` 去掉，会发生什么？

**参考答案**：Go 的 `net/http` 默认把头键规范成 Title-Case（如 `X-Foo`），而 EPP 期望小写、且 gRPC/HTTP/2 强制头名小写。去掉后，EPP 按小写匹配的某些关键头可能对不上，导致选路依据丢失；同时严格的服务端可能因违反 HTTP/2 头名规范而拒绝连接。

**练习 2**：为什么 shim 用「每请求一连接」而不是复用一个长连接到 EPP？

**参考答案**：因为同一个 Pod 可能服务多个 InferencePool、对应**多个不同的 EPP**（地址由每条请求的头动态决定），单一长连接无法表达「这次打 A、下次打 B」。每请求拨号最简单、也避免维护按目标池化的连接池的复杂度。代价是每请求一次 TLS 握手，但因为是 Pod 内/集群内通信，可接受。

### 4.3 InferencePool 图处理：CRD 如何进图

#### 4.3.1 概念说明

shim 解决了「数据面运行时怎么问 EPP」，但还有一连串控制面问题：NGF 怎么知道一个 HTTPRoute 的后端是个 InferencePool？它该 watch 哪些 CRD？池子配置错了怎么报状态？

这些都发生在**图构建阶段**。回顾 u5-l1：`BuildGraph` 把集群资源快照加工成内部 `Graph`。InferencePool 作为「一类后端」，在图里有自己的处理路径，核心产物是 `Graph.ReferencedInferencePools`——一张「被至少一条 Route 引用的 InferencePool」表，每项带上它所属的 Gateway、引用它的 HTTPRoute、校验得出的 Conditions、以及 `Valid` 标志。

`InferencePool` 来自一个**非标准 API 组**（`inference.networking.x-k8s.io`），由 `sigs.k8s.io/gateway-api-inference-extension/api/v1` 提供。这有一个重要后果，下面会看到：它不能用标准的 CRD 发现机制探测，只能靠特性开关 `--gateway-api-inference-extension` 硬开关。

#### 4.3.2 核心流程

InferencePool 进图的流程：

```text
BuildGraph(...):
  ... 先建 GatewayClass、Gateway、Routes（routes 里已标记哪些 backendRef 是 InferencePool）
  referencedInferencePools = buildReferencedInferencePools(routes, gws, state.InferencePools, services, listenerSets):
    for 每个 Gateway gw:
      processInferencePoolsForGateway(routes, gw, ...):
        for 每条属于 gw 的 route:
          for 每条 rule 的每个 backendRef:
            if 是 InferencePool（IsInferencePool 或 Kind==InferencePool）:
              按 (name, namespace) 收集到 referencedInferencePools，记下所属 gw 与 route
    for 每个 referencedPool: 校验 → 累积 Conditions → Valid = 无条件
  addBackendRefsToRouteRules(routes, ..., referencedInferencePools, ...):
    对每个 inferencePool 引用调 resolveInferencePoolRef：回填端口 + EndpointPickerConfig
  ...
  Graph.ReferencedInferencePools = referencedInferencePools
```

校验有两个独立的检查（对应 InferencePool 的两个标准 Condition）：

- **Accepted**：引用该池的 HTTPRoute 是否被 Gateway 接受（`route.Valid`）。若任一引用它的 route 不被接受 → `Accepted=False, Reason=HTTPRouteNotAccepted`。
- **ResolvedRefs**：池的 `EndpointPickerRef` 是否合法。kind 默认且必须是 `Service`，且该 Service 必须存在于集群；否则 → `ResolvedRefs=False, Reason=InvalidExtensionRef`。

#### 4.3.3 源码精读

先看图的承载字段和「是否被引用」判定：

[internal/controller/state/graph/graph.go:82-84](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L82-L84) —— `Graph.ReferencedInferencePools` 字段，即本模块的产物。

[internal/controller/state/graph/graph.go:166-169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/graph.go#L166-L169) —— `IsReferenced` 对 `*inference.InferencePool` 的分支：只有「被至少一条 Route 引用」的池子才算图的一部分。这呼应 u4-l4 的 changed predicate——**未被引用的 InferencePool 变更不会触发重建**。

接着是核心数据结构与收集函数：

[internal/controller/state/graph/inferencepools.go:18-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L18-L37) —— `ReferencedInferencePool` 把一个池子连同它的「社会关系」打包：`Source`（原始 CRD）、`Gateways`、`HTTPRoutes`、`Conditions`、`Valid`。`EndpointPickerConfig` 则是后续要灌进后端引用的 EPP 引用 + 命名空间。

[internal/controller/state/graph/inferencepools.go:41-76](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L41-L76) —— `buildReferencedInferencePools`：先逐 Gateway 收集引用关系，再对每个被引用的池子跑两项校验，`Valid = len(Conditions) == 0`。注意它**只处理被 Route 引用的池子**——没被引用的池不会出现在结果里（即便它存在于集群）。

[internal/controller/state/graph/inferencepools.go:79-136](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L79-L136) —— `processInferencePoolsForGateway` 是「发现」逻辑：遍历属于该 Gateway 的每条 Route 的每条 rule 的每个 `RouteBackendRef`，凡 `IsInferencePool` 为真或 `Kind == InferencePool` 的，按 `(name, namespace)` 记账。注意第 95 行的判断——一个引用要么显式声明 kind 是 InferencePool，要么是「伪装成 Service 的 InferencePool 后端」（`IsInferencePool`，对应 4.3.1 提到的 headless shadow Service）。

两个校验函数：

[internal/controller/state/graph/inferencepools.go:138-172](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L138-L172) —— `validateInferencePoolExtensionRef`：kind 为空时**默认按 Service 处理**；kind 不是 Service 直接判无效；是 Service 则要求它在集群里存在。失败时返回 `NewInferencePoolInvalidExtensionref`（对应 `ResolvedRefs=False`）。

[internal/controller/state/graph/inferencepools.go:174-196](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L174-L196) —— `validateInferencePoolRoutesAcceptance`：只要有一条引用该池的 Route 不被 Gateway 接受（`!route.Valid`），就返回 `NewInferencePoolInvalidHTTPRouteNotAccepted`（对应 `Accepted=False`）。

这两个 Condition 的工厂定义在条件词典里：

[internal/controller/state/conditions/conditions.go:1443-1491](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1443-L1491) —— `NewInferencePoolAccepted` / `NewInferencePoolResolvedRefs` 造「正常」条件，`NewDefaultInferenceConditions` 给每个 InferencePool 的 status 提供默认的两个 True 条件；两个 `...Invalid...` 工厂造失败条件。这些 Condition 最终由 status updater（u8-l1）异步写回 InferencePool 的 `.status`。

收集完引用关系后，`resolveInferencePoolRef` 把池子的端口和 EPP 配置「加肥」到后端引用上：

[internal/controller/state/graph/backend_refs.go:173-215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L173-L215) —— `resolveInferencePoolRef` 做三件事：① 若池子在图里但 `!Valid`，给 Route 打 `NewRouteBackendRefInvalidInferencePool` 条件并返回 `false`（跳过该引用）；② 把池子的 `TargetPorts[0]` 灌进 `ref.Port`（决定 NGINX upstream 的端口）；③ 把 `EndpointPickerRef` 和命名空间灌进 `ref.EndpointPickerConfig`（决定生成 location 时 `set $epp_host/$epp_port` 的值）。注意第 95 行那种「IsInferencePool 但池子不在图里」的情况会原样返回——留给后续 Service/Endpoint 解析走兜底。

`RouteBackendRef` 结构体本身为推理场景加了三个字段：

[internal/controller/state/graph/route_common.go:193-205](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/route_common.go#L193-L205) —— `EndpointPickerConfig`（EPP 引用 + 命名空间）、`InferencePoolName`（池名）、`IsInferencePool`（是否「伪装成 Service 的推理后端」）。

最后是控制器注册侧——决定 NGF 到底 watch 不 watch InferencePool：

[internal/controller/manager.go:749-762](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L749-L762) —— 只有 `--gateway-api-inference-extension` 打开时才注册 InferencePool 控制器。注释点出一个关键点：它 `requireCRDCheck: false`——因为 InferencePool 用的是 `x-k8s.io` 这种非标准 API 组，**标准 API discovery 探测不可靠**，所以干脆不用 CRD 存在性发现（u3-l3 讲的那套机制），而由 flag 单独控制。这是 u3-l3 提到的「InferencePool 为靠 flag 单独控制的例外」。

对象存储侧也对应注册了 InferencePool 的 store：

[internal/controller/state/change_processor.go:135](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L135) 与 [internal/controller/state/change_processor.go:202-203](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L202-L203) —— `ChangeProcessor` 的 `multiObjectStore` 里为 InferencePool 建了一张 `objectStoreMapAdapter`，使 InferencePool 的增删改能被捕获进 `ClusterState.InferencePools`（呼应 u4-l4 的对象存储按 GVK 分发）。

#### 4.3.4 代码实践

**实践目标**：追踪「一个 HTTPRoute 引用了 InferencePool」时，校验结果如何沉淀到 InferencePool 的 Conditions 上。

**操作步骤**：

1. 假设有一条 HTTPRoute，其 `backendRefs` 指向一个不存在的 InferencePool（或池子存在但 `EndpointPickerRef` 指向一个不存在的 Service）。
2. 在源码里画出这条因果链：
   - `buildReferencedInferencePools`（inferencepools.go:41）收集到该池；
   - `validateInferencePoolExtensionRef`（inferencepools.go:138）发现 Service 不存在 → 产生一个 Condition；
   - 该 Condition 被 `refPool.Conditions` 收下 → `Valid = false`（inferencepools.go:72）；
   - `resolveInferencePoolRef`（backend_refs.go:199）看到 `!pool.Valid`，给 HTTPRoute 打 `NewRouteBackendRefInvalidInferencePool`（conditions.go:562）。
3. 打开 `internal/controller/state/graph/inferencepools_test.go`，找到断言 `ResolvedRefs=False / InvalidExtensionRef` 的用例，确认你的因果链与测试一致。

**需要观察的现象**：错误会**同时**体现在两个对象上——InferencePool 自己的 status（ResolvedRefs=False），以及引用它的 HTTPRoute 的 backendRef（InvalidInferencePool）。这是 Gateway API「双向状态回写」的典型体现。

**预期结果**：你能说清「Service 不存在」这一种错误，分别经由哪两个 Condition 工厂、落到哪两个资源的 status 上。

#### 4.3.5 小练习与答案

**练习 1**：为什么 InferencePool 控制器要 `requireCRDCheck: false`，而不是像其他 CRD 那样先探测再注册？

**参考答案**：InferencePool 属于 `inference.networking.x-k8s.io` 这个非标准 API 组，标准 Kubernetes API discovery 对这类 `x-k8s.io` 组的探测不可靠（u3-l3 的 CRD 发现机制按 GroupVersion 分组发 discovery 请求）。因此 NGF 不依赖发现，而是直接由 `--gateway-api-inference-extension` flag 表达意图：flag 开了就注册并 watch，没装 CRD 时 watch 会失败但那属于用户配置问题。

**练习 2**：如果一个 InferencePool 存在于集群，但没有任何 Route 引用它，它会进 `Graph.ReferencedInferencePools` 吗？

**参考答案**：不会。`buildReferencedInferencePools` 只收集「被至少一条 Route 引用」的池子（通过遍历 route 的 backendRef 反向发现）。未被引用的池不进图，其变更也因 `IsReferenced` 返回 false 而不触发重建（呼应 changed predicate）。

### 4.4 NGINX↔EPP 头交互：NJS + Go shim 全链路

#### 4.4.1 概念说明

4.2 讲了 shim 的内部，4.3 讲了图怎么准备数据，本节把它们**缝起来**：控制面生成的 NGINX 配置如何驱动 NJS 调 shim、shim 又如何驱动 EPP，以及那个被选中的 endpoint 如何最终变成 `proxy_pass` 的目标。

这条链路横跨四种「语言」：Go（控制面生成配置 + 运行时 shim）、NGINX 配置指令、JavaScript（NJS 模块）、gRPC（ext_proc）。把它们粘合在一起的，是一组**约定好的头名和变量名**。

#### 4.4.2 核心流程

数据面运行时的完整链路（对应 4.1 的全景图）：

```text
① 控制面为推理后端生成 location，写入三条 set：
     set $epp_internal_path <内部 proxy_pass location 的路径>;
     set $epp_host <EPP Service 名>.<命名空间>;     # 由 extractEPPConfig 拼出
     set $epp_port <EPP 端口>;
     js_content epp.getEndpoint;

② NJS getEndpoint(r) 执行：
     把 $epp_host/$epp_port 塞进请求头 X-EPP-Host / X-EPP-Port
     resp = ngx.fetch("http://127.0.0.1:54800", {method, headers, body})  # 打 shim
     endpoint = resp.headers["X-Gateway-Destination-Endpoint"]            # shim 回传的选中 endpoint
     r.variables.inference_workload_endpoint = endpoint                   # 存进变量
     r.internalRedirect($epp_internal_path)                               # 跳到内部 proxy_pass location

③ shim（4.2 已详述）：把 X-EPP-Host/Port 当目标，打 gRPC ext_proc 给 EPP，回传 X-Gateway-Destination-Endpoint

④ 内部 location 执行：
     proxy_pass http://$inference_backend_<pool>     # 注意这是个变量

⑤ 该变量由一张 map 解析（maps.go）：
     map $inference_workload_endpoint $inference_backend_<pool> {
       ""        <池 upstream>;          # EPP 没选出 endpoint
       "~.+"     $inference_workload_endpoint;   # 用 EPP 选的 endpoint
       default   <failOpen? 池 upstream : invalidBackendRef>;  # 兜底
     }
   —— 这张 map 既实现了「用 EPP 的选择」，又实现了 FailOpen/FailClose 兜底。
```

这张 map 是理解整条链路的「机关」：NGINX 的 `proxy_pass` 永远写一个固定变量名 `$inference_backend_<pool>`，而这个变量的**取值**由 `$inference_workload_endpoint`（NJS 写入的 EPP 选择结果）经 map 决定。EPP 选了就用选的；没选就按失败模式兜底。

#### 4.4.3 源码精读

先看 NJS 模块——它是 NGINX 侧的「发起方」：

[internal/controller/nginx/modules/src/epp.js:1-10](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/modules/src/epp.js#L1-L10) —— 头常量与 shim 地址。注意 `SHIM_URI = 'http://127.0.0.1:54800'`（与 Go 侧 `GoShimPort` 同源）、`ENDPOINT_HEADER = 'X-Gateway-Destination-Endpoint'`（EPP 回传的选中 endpoint 头名）。

[internal/controller/nginx/modules/src/epp.js:12-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/modules/src/epp.js#L12-L57) —— `getEndpoint` 函数全貌：① 校验 `$epp_host/$epp_port/$epp_internal_path` 三个变量都在；② 把后两者塞进请求头 `X-EPP-Host/X-EPP-Port`；③ `ngx.fetch` 打 shim；④ 成功则把响应头 `X-Gateway-Destination-Endpoint` 存进变量 `inference_workload_endpoint`，失败只记错误日志（**不中断请求**，让 map 的 default 分支兜底）；⑤ `internalRedirect` 到内部 location。第 49-54 行处理了「rewrite 场景下 `$request_uri` 不再生效」的细节，用 `qs.stringify` 手动保留 query 参数。

> 注意第 33 行：只有 `status === 200 && endpointHeader` 同时成立才写入变量。这意味着如果 shim 返回非 200（比如 EPP 不可达、ext_proc 报错），NJS **不会**写入 `inference_workload_endpoint`，变量保持空串，于是 map 走 default 分支——这就是 FailOpen/FailClose 在数据面的真正落点。

控制面怎么把 `$epp_host/$epp_port` 算出来：

[internal/controller/nginx/config/servers.go:945-961](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L945-L961) —— `extractEPPConfig` 从后端的 `EndpointPickerConfig` 取值：端口取自 `EndpointPickerRef.Port.Number`；host 在命名空间非空时拼成 `<name>.<namespace>`（即集群内 Service DNS 名），否则只用 name。这个 host 就是 shim 要去打的 EPP 地址。

[internal/controller/nginx/config/servers.go:938-943](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L938-L943) —— `setLocationEPPConfig` 把 `EPPInternalPath/EPPHost/EPPPort` 三件套写进 location 结构体，供模板渲染。

模板把它们渲染成 NGINX 指令：

[internal/controller/nginx/config/servers_template.go:270-274](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L270-L274) —— 模板片段：三条 `set` 指令 + `js_content epp.getEndpoint`。第 270 行的 `js_var $inference_workload_endpoint;` 声明了那个被 NJS 写入、被 map 读取的可写变量。

内部 location 与内部路径的生成：

[internal/controller/nginx/config/servers.go:1125-1144](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1125-L1144) —— `initializeInternalInferenceEPPLocation` 造一个 `internal` location，其 `Path` 由 `generateInternalInferenceEPPLocationPath` 按 `upstream名-namespace-name-routeRule-pathRule` 规则拼出，保证每条推理后端有唯一、可被 `internalRedirect` 命中的内部 location。`Type` 标为 `InferenceInternalLocationType`。

> servers.go 顶部那段大注释（L416-L525）用伪配置把「无匹配条件」「多后端」「带 HTTP 匹配条件」「多后端+匹配条件」四种形态的 location 布局画得非常清楚，**强烈建议读一遍**——它比任何文字描述都能说明 NJS external location、EPP internal location、proxy_pass internal location、split_clients 四者怎么编排。

最后是那个「机关」map：

[internal/controller/nginx/config/maps.go:415-447](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L415-L447) —— 这段构造了 `map $inference_workload_endpoint $inference_backend_<pool>`：空串 → 池 upstream；匹配 `~.+`（非空）→ `$inference_workload_endpoint`（EPP 选的 endpoint）；default 按 EPP 的失败模式决定——`FailOpen` 走池 upstream、否则走 `invalidBackendRef`（返回错误）。注释「no endpoint picked by EPP go to inference pool directly」点明了兜底语义。这就是 4.1 提到的「NGF 仍为池副本建 upstream」的用途：FailOpen 时由 NGINX 自己从池 upstream 里挑一个。

shim 作为 sidecar 被注入数据面，发生在 provisioner：

[internal/controller/provisioner/objects.go:1529-1565](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L1529-L1565) —— `configureInferenceExtension` 往数据面 Deployment 的 Pod 模板里 append 一个名为 `endpoint-picker-shim` 的容器，命令是 `/usr/bin/gateway endpoint-picker`，并按配置追加 `--endpoint-picker-disable-tls` / `--endpoint-picker-tls-skip-verify`。安全上下文很严（drop ALL capabilities、只读根文件系统、非 root）。这印证了「shim 与 NGINX 同 Pod」，所以 shim 的 `127.0.0.1:54800` 才对 NGINX 可达。

> 这一步把 4.2（shim 进程）和 4.4（NGINX 调 shim）在部署层面闭环：provisioner（u9-l1）创建数据面 Deployment 时，如果该 Gateway 涉及推理后端，就调 `configureInferenceExtension` 把 shim 容器塞进去。没有推理后端的普通 Gateway 不会有这个 sidecar。

#### 4.4.4 代码实践

**实践目标**：把「NGINX 的一次请求如何被转发到 EPP 选中的副本」整条链路在源码里逐跳指认出来，并说清每个头/变量在哪一层产生、被谁消费。

**操作步骤**（源码阅读型实践，推荐两人结对互相讲解）：

1. 从 **NJS** 出发：读 `epp.js` 的 `getEndpoint`，标出它**读**哪些变量（`epp_host/epp_port/epp_internal_path`）、**写**哪个变量（`inference_workload_endpoint`）、**发**哪些头（`X-EPP-Host/X-EPP-Port`）、**收**哪个头（`X-Gateway-Destination-Endpoint`）。
2. 跳到 **shim**：读 `endpoint_picker.go` 的 handler，确认它收的头（`X-EPP-Host/X-EPP-Port`）正是 NJS 发的，回的头（`DestinationEndpointKey`）正是 NJS 收的——链路在头层面**严丝合缝**。
3. 跳到 **map**：读 `maps.go:415-447`，解释 `proxy_pass http://$inference_backend_<pool>` 里的变量如何由 `$inference_workload_endpoint` 经 map 解析成「EPP 选的 endpoint」或「兜底 upstream」。
4. 跳到 **模板**：读 `servers_template.go:270-274`，确认控制面确实生成了 NJS 依赖的那三条 `set` 与 `js_var`。
5. 用 NJS 单测自检：读 `internal/controller/nginx/modules/test/epp.test.go`，找到断言 `r.variables.inference_workload_endpoint === endpoint` 的用例（约 L59），它用一个 mock 的 fetch 返回带 `X-Gateway-Destination-Endpoint` 的响应，验证 NJS 正确地把头值写进了变量。

**需要观察的现象**：你会看到「同一个头名 `X-Gateway-Destination-Endpoint`」在三处出现——EPP 用 ext_proc set-header 写、shim 用 HTTP 响应头转发、NJS 用 `response.headers.get` 读取。**这正是这条异构链路的协议契约**：头名是三方共同认可的「选路结果载体」。

**预期结果**：你能不查源码地复述整条链路，并能回答「如果 EPP 不可达，请求会怎样」——NJS 的 `ngx.fetch` 失败、不写 `inference_workload_endpoint`、变量为空、map 走 default、按 FailOpen/FailClose 决定兜底还是报错。

**待本地验证**：若你有完整推理环境，可部署一个 InferencePool + EPP，对数据面 Pod 抓包（shim↔EPP 的 gRPC、NGINX↔shim 的 HTTP），核对 `X-EPP-Host/X-EPP-Port/X-Gateway-Destination-Endpoint` 三个头的实际取值是否与源码一致。

#### 4.4.5 小练习与答案

**练习 1**：`proxy_pass http://$inference_backend_<pool>` 里的变量为什么不能直接写成 `$inference_workload_endpoint`？

**参考答案**：因为还需要表达「EPP 没选出 endpoint」和「失败模式兜底」两种情况。直接用 `$inference_workload_endpoint` 会在它为空时让 `proxy_pass` 指向空地址而失败。引入 map 后，空串和非空串、以及 default，分别可指向「池 upstream（兜底）」「EPP 选的 endpoint」「invalidBackendRef（FailClose）」，从而把 FailOpen/FailClose 语义编码进配置。

**练习 2**：`X-EPP-Host` 在多 InferencePool 场景下为什么必须是「按请求动态决定」而不是 shim 启动时固定？

**参考答案**：一个数据面 Pod 可能同时服务多个 InferencePool，每个池有自己的 EPP（不同 Service/端口）。shim 启动时无法预知本次请求属于哪个池，所以由 NGINX 在 location 里按匹配到的后端 `set $epp_host ...`，再通过请求头告诉 shim「这次该问哪个 EPP」。这等价于把「请求→EPP」的路由决策下沉到 NGINX 配置层，shim 只做无状态翻译。

## 5. 综合实践

把本讲三个模块串成一个端到端的「故障排查」小任务。

**场景**：你部署了一个引用 InferencePool 的 HTTPRoute，但请求一直返回 500，且你怀疑是推理扩展链路某环出了问题。请按以下顺序排查，并在每一步指出**该看哪个源码文件、哪段逻辑**：

1. **控制面是否认得这个池？**
   - 用 `kubectl get inferencepool <name> -o yaml` 看它的 `.status.conditions`。
   - 对照 [inferencepools.go:138-196](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/inferencepools.go#L138-L196)：如果 `ResolvedRefs=False / InvalidExtensionRef`，说明 `EndpointPickerRef` 指的 Service 不存在或 kind 非 Service；如果 `Accepted=False / HTTPRouteNotAccepted`，说明引用它的 HTTPRoute 没被 Gateway 接受。

2. **Route 侧是否报了 InvalidInferencePool？**
   - 看 HTTPRoute 的 `.status.parents[].conditions`，对照 [backend_refs.go:199-206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/backend_refs.go#L199-L206)（`resolveInferencePoolRef` 在池 `!Valid` 时打的条件）。

3. **数据面有没有 shim sidecar？**
   - `kubectl get pod <nginx-pod> -o jsonpath='{.spec.containers[*].name}'`，确认有 `endpoint-picker-shim`。对照 [objects.go:1529-1565](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/provisioner/objects.go#L1529-L1565)。

4. **shim 是否能连上 EPP？**
   - 看 `endpoint-picker-shim` 容器日志里的 `error opening ext_proc stream` 或 `error creating gRPC client`，对照 [endpoint_picker.go:79-96](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/endpoint_picker.go#L79-L96)。常见原因：`X-EPP-Host/Port` 拼出的 Service DNS 解析不到、或 EPP 没起。

5. **EPP 选不出 endpoint 时是否兜底？**
   - 看 NGINX error 日志里 NJS 的 `could not get specific inference endpoint`（[epp.js:40-43](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/modules/src/epp.js#L40-L43)），再对照 [maps.go:415-447](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L415-L447) 确认失败模式是 FailOpen（兜底到池 upstream）还是 FailClose（invalidBackendRef → 500）。

**交付物**：一张表，把「症状 → 对应源码位置 → 可能的根因 → 修复动作」四列填满。这张表本身就是本讲三个最小模块（shim / 图处理 / 头交互）的「故障版地图」。

## 6. 本讲小结

- 推理扩展要解决的核心问题是「AI/LLM 副本太贵，不能简单轮询」，方案是引入 **EPP** 专门选副本，并通过响应头 `X-Gateway-Destination-Endpoint` 把选择结果回传给 NGINX。
- NGINX 不会说 gRPC ext_proc，所以 NGF 在数据面 Pod 里塞一个 **Go shim**（`endpoint-picker` 子命令），它对外是 `127.0.0.1:54800` 的 HTTP server，对内把请求翻译成 ext_proc gRPC 流打给 EPP——本质是一个**无状态协议翻译器**。
- `InferencePool` 在图里被当作「一类后端」：`buildReferencedInferencePools` 反向收集被 Route 引用的池，跑 Accepted/ResolvedRefs 两项校验，结果沉淀到 `Graph.ReferencedInferencePools` 并双向回写状态（池自身的 Conditions + Route 的 InvalidInferencePool）。
- InferencePool 控制器**不走标准 CRD 发现**，由 `--gateway-api-inference-extension` flag 单独开关（`requireCRDCheck: false`），因为它是 `x-k8s.io` 非标准 API 组。
- 数据面全链路靠一组**约定头名/变量名**缝合：NJS 用 `X-EPP-Host/Port` 打 shim、收 `X-Gateway-Destination-Endpoint` 写进 `$inference_workload_endpoint`，再由 `map` 把它解析成 `$inference_backend_<pool>` 供 `proxy_pass` 使用，同时编码 FailOpen/FailClose 兜底。
- shim 由 provisioner 的 `configureInferenceExtension` 作为 sidecar 注入数据面 Deployment，**只有涉及推理后端的 Gateway 才会有这个容器**。

## 7. 下一步学习建议

- **横向对照 FailOpen 机制**：本讲的 `map` 兜底（maps.go）与 u6-l3 的 `split_clients` 加权分流是 NGINX「用变量做动态选路」的两种范式，建议对照阅读，理解 NGF 为何统一用「map/split_clients + 变量」表达运行时决策。
- **深入 provisioner**：shim sidecar 的注入（objects.go:1529）属于 u9-l1 的数据面置备链路。建议接着学 u9-l1，理解「创建一个引用 InferencePool 的 Gateway 时，provisioner 会在数据面 Deployment 上额外做哪些事」。
- **状态回写闭环**：本讲产生的 InferencePool Conditions 由 u8-l1 的 status updater 异步写回。若想看清「Conditions 怎样最终落到 `.status`」，请读 u8-l1 的 status.Queue 与 GroupUpdater。
- **跟进上游演进**：Inference Extension 的 API（含 `InferenceObjective`）仍处于 alpha，设计提案（EP-3716）的 Alternatives 章节提到了「未来可能用 NGINX 原生能力替代 NJS+shim」的改进方向，值得持续关注上游 [gateway-api-inference-extension](https://github.com/kubernetes-sigs/gateway-api-inference-extension) 仓库。
