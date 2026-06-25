# Ingress → 配置（version1）

## 1. 本讲目标

本讲聚焦 NGINX Ingress Controller（以下简称 NIC）「配置生成」层中最经典的一条链路：**把一个标准 Kubernetes `Ingress` 资源翻译成 NGINX 能识别的 `.conf` 文件**。

读完本讲，你应当能够：

- 说清 `IngressEx` 扩展模型为什么存在，它和裸 `Ingress` 的差别是什么。
- 跟踪 `generateNginxCfg` 如何遍历 `Ingress.Spec.Rules`，为每条 host 生成一个 `Server`、为每个 path 生成一个 `Location` 和一个 `Upstream`。
- 解释 `createUpstream`、`createLocation`、`createHealthCheck` 各自的职责，以及 upstream 命名规则。
- 理解 TLS（`addSSLConfig`）和主动健康检查（`createHealthCheck`）是如何挂到生成结果上的。
- 区分普通 Ingress 与 mergeable Ingress（master/minion）在配置生成上的不同，理解 `generateNginxCfgForMergeableIngresses` 如何把多个 Ingress 合并成单个 `Server`。

本讲承接 [u4-l1 Configurator](u4-l1-configurator-overview.md)（已经知道 Configurator 是「只执行不裁决」的中枢）与 [u2-l1 路由资源全景](u2-l1-routing-resource-concepts.md)（已经知道 Ingress 是标准 host/path 路由资源）。本讲只讲 **Ingress（version1）** 的翻译；VirtualServer（version2）的翻译留待 [u4-l5](u4-l5-virtualserver-config-generation.md)。

## 2. 前置知识

阅读本讲前，请确保你已经理解以下概念（若不熟悉，先看前置讲义）：

- **Ingress 资源的结构**：`spec.rules[].host` + `spec.rules[].http.paths[].path` + `backend.service`（name + port）。一条 Ingress 描述「某个 host 的某个 path 转发到哪个 Service」。这是 [u2-l1](u2-l1-routing-resource-concepts.md) 的内容。
- **NGINX 的三大配置块**：`http {}` 里有 `upstream {}`（后端服务器组）、`server {}`（虚拟主机）、`server {}` 里有 `location {}`（URL 匹配规则）。`proxy_pass` 把请求从 location 转发到 upstream。
- **Configurator 的三段式**（[u4-l1](u4-l1-configurator-overview.md)）：生成器产出模板输入结构体 → 执行器渲染成文本 → `CreateConfig` 写盘。本讲对应其中的「生成器」。
- **注解（annotations）与 ConfigMap**（[u4-l2](u4-l2-configmap-parsing.md)、[u4-l3](u4-l3-annotations-parsing.md)）：`nginx.org/*` 注解和全局 ConfigMap 会解析成 `ConfigParams`，本讲会大量读取 `ConfigParams` 的字段（如 `ProxyConnectTimeout`、`HealthCheckEnabled`）。

一个直觉性的类比：标准 `Ingress` 只是一张「路由表」，但 NGINX 配置是一份「完整的服务器程序」。`ingress.go` 的工作就是补齐路由表里缺的所有信息——后端的真实 IP 地址（Endpoints）、超时时间、TLS 证书、健康检查 URI 等——把一张扁平的路由表「膨胀」成一份可运行的 NGINX 配置。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/configs/ingress.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go) | **本讲核心**。定义 `IngressEx` 模型，并实现 `generateNginxCfg`（普通 Ingress）与 `generateNginxCfgForMergeableIngresses`（mergeable Ingress），以及 `createUpstream`、`createLocation`、`createHealthCheck`、`addSSLConfig` 等构造函数。 |
| [internal/configs/version1/config.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/config.go) | 定义「模板输入结构体」：`IngressNginxConfig`、`Server`、`Location`、`Upstream`、`HealthCheck`、`LimitReqZone` 等。这是生成函数的返回类型，也是模板渲染的输入。 |
| [internal/configs/configurator.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go) | `Configurator` 的 `AddOrUpdateIngress` / `addOrUpdateIngress` 把生成结果接到「渲染 + 写盘 + reload」三段式上，是本讲生成函数的上游调用方。 |
| [internal/configs/version1/template_executor.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/template_executor.go) | `ExecuteIngressConfigTemplate` 把 `IngressNginxConfig` 喂给 `.tmpl` 模板渲染成文本。 |
| [internal/configs/version1/nginx.ingress.tmpl](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl) | OSS 版 Ingress 模板，把 `IngressNginxConfig` 渲染成真实的 `upstream {} / server {} / location {}` 指令。 |

记忆口诀：`IngressEx`（输入扩展模型）→ `generateNginxCfg`（翻译）→ `IngressNginxConfig`（模板输入结构体）→ `.tmpl`（渲染）→ `nginx.conf`。

## 4. 核心概念与源码讲解

### 4.1 IngressEx 模型与生成入口

#### 4.1.1 概念说明

Kubernetes 原生的 `networking.Ingress` 只描述「意图」：把 `host/path` 转给某个 `Service`。但 NGINX 配置需要的是「事实」：

- 这个 Service 后面到底有哪些 Pod IP（Endpoints）？
- 这个 Service 是不是 ExternalName 类型？需要 resolver 吗？
- Pod 上配的 `readiness/liveness Probe` 是什么（用来做主动健康检查）？
- 引用的 TLS Secret、JWT Secret、BasicAuth Secret 校验过了吗、落盘到哪个路径？
- 这个 Ingress 引用了哪些 Policy CRD？AppProtect/DoS 资源？

控制器层（[u3-l6](u3-l6-configuration-model.md)）在调谐时已经把这些「周边资源」全部解析好，打包成一个叫 **`IngressEx`**（Ingress Extended）的结构体，再交给配置层。所以配置层拿到的是一份「信息完备」的 Ingress，不需要再去查 API Server。

#### 4.1.2 核心流程

`IngressEx` → `generateNginxCfg` 的整体流程：

1. **组装入参**：Configurator 把 `IngressEx`、`ConfigParams`（注解/ConfigMap 解析结果）、`StaticConfigParams`（启动 flags）、若干运行期开关（`isPlus`、`isResolverConfigured` 等）打包成 `NginxCfgParams`。
2. **入口函数** `generateNginxCfg(ncp NginxCfgParams)`：
   - 调 `parseAnnotations` 把注解解析成 `cfgParams`（详见 [u4-l3](u4-l3-annotations-parsing.md)）。
   - 收集每条 path 的周边信息：websocket 服务、session persistence、rewrite、ssl、grpc。
   - 为 `DefaultBackend`（如果有）生成 upstream。
   - 遍历 `Ingress.Spec.Rules`，为每条 host 建一个 `Server`。
   - 在每个 `Server` 内遍历 `paths`，为每个 path 建 `Location` + `Upstream`。
   - 返回 `version1.IngressNginxConfig` 与一堆 `Warnings`（不致命的告警）。

伪代码：

```text
func generateNginxCfg(ncp):
    cfgParams = parseAnnotations(ingEx)            # 注解 → ConfigParams
    wsServices, grpcServices, ... = 收集周边信息
    upstreams = {}                                  # 按 upstream 名去重
    for rule in ingEx.Ingress.Spec.Rules:
        if host 非法: continue
        server = 构造 Server{...大量来自 cfgParams 的字段...}
        for path in rule.HTTP.Paths:
            upsName = getNameForUpstream(...)       # 唯一命名
            if upsName not in upstreams:
                upstreams[upsName] = createUpstream(...)
            loc = createLocation(path, upstreams[upsName], cfgParams, ...)
            locations.append(loc)
        server.Locations = locations
        servers.append(server)
    return IngressNginxConfig{Upstreams, Servers, ...}
```

#### 4.1.3 源码精读

`IngressEx` 结构体——注意它「持有」了所有周边资源，而不只是 Ingress 本身：

[internal/configs/ingress.go:44-62](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L44-L62) —— `IngressEx` 把 `Ingress` 和它的「全部依赖」打包：`Endpoints`（Pod IP 列表）、`HealthChecks`（来自 Pod Probe）、`Policies`（CRD）、`SecretRefs`（已校验的 Secret 引用）、`ExternalNameSvcs`、`ValidHosts` 等。

`NginxCfgParams` 是生成函数的统一入参包：

[internal/configs/ingress.go:91-105](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L91-L105) —— 把三类来源捏在一起：`staticParams`（启动 flags）、`ingEx`/`mergeableIngs`（资源）、`BaseCfgParams`（ConfigMap + 注解基线），加上 `isPlus`/`isResolverConfigured` 等开关。

`generateNginxCfg` 的开头——先把注解解析掉，再收集周边信息：

[internal/configs/ingress.go:244-257](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L244-L257) —— `parseAnnotations` 产出 `cfgParams`；随后 `getWebsocketServices`、`getSSLServices`、`getGrpcServices` 等把「按 Service 名索引」的周边信息抽出来，后面在 path 循环里按 `backend.Service.Name` 查回。

`Configurator` 如何调用它——这是生成函数接入三段式的位置：

[internal/configs/configurator.go:504-531](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L504-L531) —— `addOrUpdateIngress` 调 `generateNginxCfg` 拿到 `nginxCfg`，再 `ExecuteIngressConfigTemplate(&nginxCfg)` 渲染、`nginxManager.CreateConfig(configName, content)` 写盘。注意这里有个细节：空 host 的 Ingress 会写到共享的 `DefaultServerConfigName` 文件（[configurator.go:519-522](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L519-L522)）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「配置层拿到的是信息完备的 `IngressEx`，而不是裸 Ingress」。

**操作步骤**：

1. 打开 [internal/configs/ingress.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L44-L62)，定位 `IngressEx` 结构体。
2. 数一数：除了 `Ingress *networking.Ingress` 这一个原生字段外，它还有多少个字段是用来「携带周边资源」的？
3. 在仓库里搜索这些字段在何处被填充，例如：

```bash
git grep -n "ingEx.Endpoints" internal/k8s
git grep -n "ValidHosts" internal/k8s
```

**需要观察的现象**：`IngressEx` 的字段几乎都对应「需要额外查 API Server 才能拿到的信息」，而填充它们的代码都在控制器层 `internal/k8s/`，不在配置层。这印证了「配置层只执行不裁决、不查 API」的分层。

**预期结果**：你能列出至少 6 个周边资源字段（Endpoints、HealthChecks、Policies、ApPolRefs、SecretRefs、ExternalNameSvcs 等），且填充点都在 `internal/k8s/`。

#### 4.1.5 小练习与答案

**练习 1**：`IngressEx` 里的 `ValidHosts map[string]bool` 是干嘛的？为什么不在配置层直接信任 `Spec.Rules` 里的所有 host？

**参考答案**：`ValidHosts` 标记哪些 host 是「赢家」（详见 [u3-l6](u3-l6-configuration-model.md) 的主机冲突裁决）。当多个 Ingress 抢同一个 host 时，只有 `CreationTimestamp`/`UID` 最小的那个赢家才会被置为 `ValidHosts[host]=true`。配置层在 `generateNginxCfg` 里用 `if !ncp.ingEx.ValidHosts[rule.Host] { continue }`（[ingress.go:363-366](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L363-L366)）跳过输家，从而避免为同一 host 生成两份冲突的 `server_name`。配置层本身不裁决冲突，裁决由控制器层完成，配置层只消费结果。

**练习 2**：`NginxCfgParams` 为什么要同时区分 `staticParams` 和 `BaseCfgParams`？

**参考答案**：`staticParams`（`StaticConfigParams`）来自命令行 flags，在进程生命周期内不变（如 `DefaultHTTPListenerPort`、`TLSPassthrough`、`AppProtectBundlePath`）；`BaseCfgParams`（`ConfigParams`）来自 ConfigMap 和注解，运行期可改、会触发 reload。把两者分开，既能让生成函数读到「不变的全局约束」，又能读到「可变的配置参数」。

---

### 4.2 Upstream 生成

#### 4.2.1 概念说明

NGINX 的 `upstream {}` 块定义「一组后端服务器」。对 Ingress 而言，一个 upstream 对应「某个 host 的某个 path 转向的 Service 在某个端口上的全部 Endpoints」。

关键问题是 **命名**：NGINX 的 `proxy_pass` 用名字引用 upstream，这个名字必须在整个 NGINX 进程内全局唯一。NIC 用一个确定性公式生成名字，使得「同一个 Ingress 的同一个 backend」永远映射到同一个 upstream 名，重复处理时不会重复创建。

#### 4.2.2 核心流程

- **命名公式**：`getNameForUpstream` 用 `namespace-ingressName-host-serviceName-port` 拼出唯一名。空 host 用占位符 `_`。
- **去重**：生成过程把所有 upstream 放进一个 `map[string]Upstream`（[ingress.go:258](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L258)），同名的 backend 只建一次。
- **端点填充**：`createUpstream` 从 `ingEx.Endpoints[service+port]` 取 Pod IP 列表，逐个转成 `UpstreamServer`，排序后填进 upstream。
- **兜底**：没有 Endpoints 时用 `NewUpstreamWithDefaultServer` 塞一个 `127.0.0.1:8181`，让 NGINX 返回 502 而不是配置加载失败。
- **转切片**：最后 `upstreamMapToSlice` 把 map 按 key 排序转成切片，保证生成文件顺序稳定。

#### 4.2.3 源码精读

upstream 命名规则——这是整条链路的「命名锚点」：

[internal/configs/ingress.go:1143-1148](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1143-L1148) —— `getNameForUpstream` 用五元组拼接：`namespace-ingressName-host-serviceName-port`，空 host 替换为 `_`。`GetBackendPortAsString`（[ingress.go:1407-1412](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1407-L1412)）处理「port 可以是名字也可以是数字」。

`createUpstream` 的核心——从 Endpoints 填充服务器，带 Plus 与 OSS 的分支：

[internal/configs/ingress.go:1054-1112](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1054-L1112) —— 注意几个要点：
- Plus 模式下 upstream 不预填端点（端点走 Plus API 动态更新，见 [u5-l4](u5-l4-plus-api-dynamic-config.md)），但会设置 queue（来自强制健康检查，`upstreamRequiresQueue`）。
- OSS 模式下用 `NewUpstreamWithDefaultServer` 初始化（先放占位 8181），再用真实 Endpoints 覆盖。
- ExternalName Service 若未配 resolver 会清空端点并告警（[ingress.go:1084-1088](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1084-L1088)）。
- 每条端点带上 `MaxFails`、`MaxConns`、`FailTimeout`、`SlowStart`（都来自 `cfgParams`，即注解）。
- 最后按 `Address` 排序，保证生成顺序稳定。

没有 Endpoints 时的兜底——保证「Service 没有可用 Pod」不会让 NGINX 配置加载失败：

[internal/configs/version1/config.go:363-379](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/config.go#L363-L379) —— `NewUpstreamWithDefaultServer` 塞入 `127.0.0.1:8181`，proxy 过去会返回 502。这是「优雅降级」而非「配置错误」。

map → 排序切片，保证生成的 `upstream {}` 顺序可复现：

[internal/configs/ingress.go:1154-1172](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1154-L1172) —— 注释明确说排序是为了「保持生成文件顺序稳定 + 单元测试可复现」。

模板里 upstream 是怎么渲染的：

[internal/configs/version1/nginx.ingress.tmpl:3-21](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl#L3-L21) —— `upstream {{$upstream.Name}} { ... }`，里面依次是 `zone`、`LBMethod`、`server <addr> max_fails=.. fail_timeout=..`、`keepalive`、`sticky cookie`。

#### 4.2.4 代码实践

**实践目标**：用一个真实 Ingress 验证 upstream 命名公式。

**操作步骤**：

1. 读 [examples/ingress-resources/complete-example/cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)：它的 `/tea` path 指向 `tea-svc:80`，host 是 `cafe.example.com`，Ingress 名 `cafe-ingress`，命名空间未填即 `default`。
2. 套用 `getNameForUpstream` 公式（namespace=`default`，ingressName=`cafe-ingress`，host=`cafe.example.com`，serviceName=`tea-svc`，port=80），手算 upstream 名。

**预期结果**：upstream 名应为 `default-cafe-ingress-cafe.example.com-tea-svc-80`。`/coffee` path 对应 `default-cafe-ingress-cafe.example.com-coffee-svc-80`，二者不同。

**需要观察的现象**：即使 Ingress 有多条 path 指向同一个 Service（同 host 同 port），由于名字公式相同，`map` 会自动去重，只生成一个 upstream（见 [ingress.go:544-550](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L544-L550) 的 `if _, exists := upstreams[upsName]; !exists` 判断）。这一现象可在仓库的快照测试 `internal/configs/configurator_test.go` 或模板快照里对照确认（待本地运行 `make test` 验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 OSS 模式下 `createUpstream` 先用 `NewUpstreamWithDefaultServer` 初始化，再用真实 Endpoints 覆盖，而不是「有 Endpoints 就填、没有就留空」？

**参考答案**：因为 NGINX OSS 的 upstream 必须至少有一个 server 指令才能通过 `nginx -t` 语法校验。如果一个 Service 暂时没有 Endpoints（Pod 还没起来），直接生成空 upstream 会导致整个配置加载失败，连带影响其他正常 Ingress。先用 `127.0.0.1:8181` 占位、让请求返回 502，是一种「隔离故障」的设计：单个 Service 的端点缺失只影响它自己，不会拖垮整个数据面。真实 Endpoints 存在时（`len(upsServers) > 0`）会覆盖掉占位符（[ingress.go:1100-1105](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1100-L1105)）。

**练习 2**：端点列表为什么要排序？

**参考答案**：Go 的 map 遍历顺序是随机的，而 Endpoints 切片如果顺序不定，每次生成的 `server a; server b;` 顺序就会变，导致配置文件内容抖动、不必要的 reload、以及快照测试不稳定（参见 `upstreamMapToSlice` 的注释，[ingress.go:1160-1163](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1160-L1163)）。排序后「相同输入 → 相同输出」，配置文件只在内容真正变化时才变化。

---

### 4.3 Server 与 Location 生成

#### 4.3.1 概念说明

NGINX 用 `server {}` 表示一个虚拟主机（按 `server_name` 即 host 区分），用 `location {}` 表示该虚拟主机下的一条 URL 匹配规则。映射到 Ingress：

- 一条 `Ingress.Spec.Rules[i]`（一个 host）→ 一个 `Server`。
- 该 rule 下的每条 `paths[j]` → 一个 `Location`。

`Server` 承载 host 级的配置（TLS、HSTS、HTTP/2、server-snippets、JWT/BasicAuth、各种 Policy），`Location` 承载 path 级的配置（proxy 超时、rewrite、websocket、proxy-buffering 等）。`Location` 通过 `proxy_pass` 指向 4.2 节生成的 `Upstream`。

#### 4.3.2 核心流程

`generateNginxCfg` 中每个 rule 的处理流程：

1. 跳过非法 host（`ValidHosts`）。
2. 判断是否是 default server（空 host）。
3. 构造 `Server` 结构体，大量字段直接从 `cfgParams`（注解/ConfigMap）拷贝。
4. 若是 default server，套用特殊端口与默认证书。
5. （非 minion 时）在 server 级生成 JWT/BasicAuth/ExternalAuth。
6. 遍历 `paths`：
   - 算 upstream 名、（不存在则）建 upstream。
   - 建 `Location`（`createLocation`），把 proxy 超时、rewrite、ssl 等填进去。
   - 若有限流注解，生成 `LimitReq` 挂到 location，并收集 `LimitReqZone`。
   - 追加到 `locations`。
7. 若没有根 `/` location 但有 `DefaultBackend`，合成一个 `/` location 兜底。
8. 把 `locations`、`healthChecks`、gRPC 标志挂到 server。

#### 4.3.3 源码精读

`Server` 结构体的构造——字段几乎都来自 `cfgParams`：

[internal/configs/ingress.go:384-423](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L384-L423) —— 这一大段把注解说过的字段（`HTTP2`、`SSLRedirect`、`HSTS*`、`ServerTokens`、`ServerSnippets`、`Ports` 等）原样搬到 `Server`。注意 `Allow/Deny/WAF/EgressMTLS/PoliciesErrorReturn` 来自 `policyCfg`（Policy CRD 翻译结果，见 [u4-l7](u4-l7-policy-config-generation.md)），说明 Ingress 也能挂 Policy。

default server 的特殊处理：

[internal/configs/ingress.go:425-435](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L425-L435) —— 空 host 时端口固定为 `DefaultHTTPListenerPort`/`DefaultHTTPSListenerPort`，证书指向 `DefaultServerSecretPath`，并允许 `SSLRejectHandshake`。

path 循环的核心——建 upstream、建 location、挂限流：

[internal/configs/ingress.go:536-653](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L536-L653) —— 逐行要点：
- [536](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L536) `upsName := getNameForUpstream(...)` 算名。
- [544-550](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L544-L550) 不存在才建 upstream（去重）。
- [554-555](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L554-L555) `createLocation` 建 location，参数包括 upstream、websocket、rewrite、ssl、grpc、pathType。
- [619-651](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L619-L651) 限流：若 `LimitReqRate != ""`，在 location 挂 `LimitReq`，并（若 zone 不存在）收集一条 `LimitReqZone`。

`createLocation`——把 location 级的 NGINX 指令参数装进结构体：

[internal/configs/ingress.go:1010-1040](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1010-L1040) —— 关键是 `ProxyPass` 由 `generateProxyPassProtocol(ssl) + "://" + upstream.Name` 拼出（如 `http://default-cafe-...-tea-svc-80`）。`ProxyConnectTimeout`、`ProxyReadTimeout`、`ClientMaxBodySize`、`ProxyBuffering` 等全部来自 `cfgParams`。`Path` 经 `generateIngressPath` 处理 pathType。

pathType → NGINX location 修饰符的转换：

[internal/configs/ingress.go:999-1008](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L999-L1008) —— `PathTypeExact` 会被加上 `= ` 前缀（对应 NGINX 的精确匹配 `location = /x`），其他类型（Prefix/ImplementationSpecific）不加前缀（前缀匹配）。

模板里 location 的渲染入口：

[internal/configs/version1/nginx.ingress.tmpl:235-237](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl#L235-L237) —— `location {{ makeLocationPath ... }} { set $service "..."; ... }`。`proxy_pass` 在 [nginx.ingress.tmpl:421](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl#L421) 渲染。

#### 4.3.4 代码实践

**实践目标**：追踪 `cafe-ingress.yaml` 的 `/tea` path 如何同时生成一个 `location` 和一个 `upstream`，并画出字段对应关系。

**操作步骤**：

1. 准备字段对应表（示例，基于 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml) 的 `/tea` path）：

   | Ingress 字段 / 来源 | 生成的 NGINX 内容 | 生成代码位置 |
   | --- | --- | --- |
   | `rule.host = cafe.example.com` | `server { server_name cafe.example.com; }` | [ingress.go:375,386](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L375-L386) |
   | `path.Path = /tea`, `PathType = Prefix` | `location /tea { ... }`（无修饰符） | [ingress.go:554](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L554) → `createLocation` → `generateIngressPath` |
   | `backend.Service.Name = tea-svc`, `port = 80` | upstream 名 `default-cafe-ingress-cafe.example.com-tea-svc-80` | [ingress.go:536](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L536) → `getNameForUpstream` |
   | `ingEx.Endpoints["tea-svc80"]` | `server <pod-ip:port> max_fails=.. ;` 列表 | [ingress.go:1090-1099](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1090-L1099) |
   | 注解 `proxy-connect-timeout`（若有） | `proxy_connect_timeout <值>;` | `cfgParams.ProxyConnectTimeout` → [createLocation L1015](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1015) |
   | location 内 | `proxy_pass http://default-cafe-...-tea-svc-80;` | [createLocation L1014](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1014) |

2. 在 `createLocation`（[ingress.go:1010](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1010-L1040)）里逐字段核对：`ProxyPass`、`ProxyConnectTimeout`、`ProxyReadTimeout`、`ClientMaxBodySize`、`Websocket`、`Rewrite`、`SSL`、`GRPC` 分别对应 `cfgParams` 的哪个字段。

**需要观察的现象**：location 的大部分字段是「直接搬运」`cfgParams`，没有复杂计算；唯一有逻辑的是 `ProxyPass`（按 ssl 决定协议）和 `Path`（按 pathType 加修饰符）。

**预期结果**：你能画出一条「Ingress path → location + upstream」的双向字段对照图，并指出哪些字段来自注解、哪些来自 Endpoints、哪些来自 pathType。

#### 4.3.5 小练习与答案

**练习 1**：如果一个 Ingress 的 rule 没有 `http` 字段（只有 host），会发生什么？

**参考答案**：代码在 [ingress.go:368-373](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L368-L373) 处理了这种情况：`if httpIngressRuleValue == nil { httpIngressRuleValue = &networking.HTTPIngressRuleValue{} }`。即建一个空的 path 列表，`Server` 仍然会生成（用于承载 TLS/Policy 等 host 级配置），但没有 location。注释「the code in this loop expects non-nil」解释了为何要兜底——避免后续 `range .Paths` 解引用 nil。

**练习 2**：为什么代码在 path 循环结束后还要检查 `rootLocation`（[ingress.go:655-693](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L655-L693)）？

**参考答案**：如果 Ingress 的所有 path 都不是 `/`，但又配了 `DefaultBackend`，那么访问根路径 `/` 时就没有匹配的 location。这段代码在「没有 `/` location 且有 DefaultBackend」时，用 DefaultBackend 合成一个 `/` location 作为兜底，保证根路径有响应。这是 Ingress 规范要求的「默认后端」语义的实现。

---

### 4.4 TLS、健康检查与认证

#### 4.4.1 概念说明

除了路由，Ingress 还承载三类「安全与可靠性」配置，它们在生成阶段被挂到 `Server`/`Location` 上：

- **TLS 终止**：`spec.tls` 声明某 host 用哪个 Secret 的证书做 HTTPS。`addSSLConfig` 负责把已校验的 Secret 路径写进 `server.ssl_certificate`。
- **主动健康检查**：NGINX Plus 主动探测 upstream 的健康（OSS 不支持）。NIC 复用 Pod 的 readiness/liveness `Probe` 作为健康检查参数，`createHealthCheck` 把它转成 `HealthCheck` 结构。
- **认证**：JWT（`nginx.org/jwt-key`）、BasicAuth（`nginx.org/basic-auth-secret`）、ExternalAuth（Policy）。这些分别由 `generateJWTConfig`、`generateBasicAuthConfig`、`resolveExternalAuth` 生成。

#### 4.4.2 核心流程

**TLS**：对每个 host，`addSSLConfig` 遍历 `ingress.spec.tls` 找到匹配该 host 的条目 → 取 `tlsSecret` 名 → 从 `secretRefs` 拿到已校验 Secret 的落盘路径 → 写进 `server.SSLCertificate/SSLCertificateKey`。若 Secret 类型不对或缺失，置 `SSLRejectHandshake=true`（拒绝握手）并告警。

**健康检查**：在 path 循环里，若 `cfgParams.HealthCheckEnabled` 且 `ingEx.HealthChecks` 里有对应的 `service+port`，就调 `createHealthCheck` 把 Pod Probe 转成 `HealthCheck`（interval/fails/passes/uri/headers），挂到 `server.HealthChecks[upsName]`。

**JWT**：非 minion 时，若注解配了 `jwt-key`，调 `generateJWTConfig` 从 `secretRefs` 取 JWK Secret 路径，生成 `JWTAuth`（key/realm/token）和可选的重定向 location（`@login_url_<ns>-<name>`）。注意即便 Secret 无效也会写路径，让 NGINX Plus 在运行期返回 500（fail-closed）。

**BasicAuth**：类似 JWT，从 `secretRefs` 取 htpasswd Secret 路径。

**ExternalAuth**：Policy 翻译出 `ExternalAuth` 后，`resolveExternalAuth` 为外部认证服务建一个专用 upstream（`createExternalAuthUpstream`）和若干内部 location（`generateIngressExternalAuthLocation`）。

#### 4.4.3 源码精读

`addSSLConfig`——TLS Secret 的匹配与校验：

[internal/configs/ingress.go:944-997](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L944-L997) —— 流程：遍历 `ingressTLS` 找 host（[952-960](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L952-L960)）→ 若 `tlsSecret` 非空，从 `secretRefs` 取路径，类型必须是 `kubernetes.io/tls`，否则 `rejectHandshake`（[969-983](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L969-L983)）→ 若没指定 Secret 但开了 wildcard，用通配证书（[984-985](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L984-L985)）→ 否则拒绝握手并告警。

`createHealthCheck`——把 Pod Probe 翻译成 NGINX Plus 健康检查：

[internal/configs/ingress.go:1114-1126](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1114-L1126) —— `hc.FailureThreshold → Fails`、`hc.PeriodSeconds → Interval`、`hc.SuccessThreshold → Passes`、`hc.HTTPGet.Path → URI`、`hc.HTTPGet.Scheme → Scheme`、`hc.TimeoutSeconds → TimeoutSeconds`。`Mandatory` 来自注解。

`generateJWTConfig`——注意 fail-closed 设计：

[internal/configs/ingress.go:734-772](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L734-L772) —— 关键注释（[753-754](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L753-L754)）：即使 Secret 类型错或无效，仍然把 `secretRef.Path` 写进 `jwtAuth.Key`。这样 NGINX Plus 在加载时发现 key 无效会返回 500，而不是「跳过认证放行」（后者是严重的安全漏洞）。这是「宁可不服务，也不无认证服务」的 fail-closed 取舍。

`generateBasicAuthConfig`——结构同 JWT：

[internal/configs/ingress.go:774-794](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L774-L794) —— Secret 类型必须是 `SecretTypeHtpasswd`，否则仍写路径（同样 fail-closed）。

`resolveExternalAuth`——为外部认证建 upstream + 内部 location：

[internal/configs/ingress.go:824-854](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L824-L854) —— 解析 service port、用 `createExternalAuthUpstream` 建 upstream（[798-819](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L798-L819)）、用 `generateIngressExternalAuthLocation` 建内部 location（`internal;`，只供 `auth_request` 子请求使用）。

#### 4.4.4 代码实践

**实践目标**：理解 TLS 的三种结果（正常、拒绝握手、通配证书）的触发条件。

**操作步骤**：

1. 阅读 [addSSLConfig](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L944-L997)，识别三种分支：
   - 分支 A：`tlsSecret` 非空且 Secret 有效 → `pemFile = secretRef.Path`，正常 TLS。
   - 分支 B：`tlsSecret` 非空但 Secret 类型错或无效 → `rejectHandshake = true`。
   - 分支 C：`tlsSecret` 为空但 `isWildcardEnabled` → 用 `pemFileNameForWildcardTLSSecret`。
   - 分支 D：`tlsSecret` 为空且未开 wildcard → `rejectHandshake = true` 并告警。
2. 对照 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)：它的 `tls` 段声明 host `cafe.example.com` 用 `secretName: tls-secret`。判断它会走哪个分支。

**需要观察的现象**：`addSSLConfig` 对「该 host 是否启用 TLS」的判断依据是 `ingress.spec.tls` 里有没有包含该 host 的条目，而不是单独的注解。也就是说 TLS 是「host 级」开关。

**预期结果**：cafe-ingress 走分支 A（假设 `tls-secret` 是合法的 `kubernetes.io/tls` 类型 Secret）。若把 `tls-secret` 改成一个 `Opaque` Secret，会走分支 B，NGINX 对 `cafe.example.com` 的 HTTPS 请求拒绝握手。若想确认这一行为，可待本地在集群中 apply 一个错误类型的 Secret 后请求观察（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `generateJWTConfig` 在 Secret 无效时仍然把路径写进配置，而不是跳过 JWT 配置？

**参考答案**：这是 fail-closed 安全设计（见 [ingress.go:753-759](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L753-L759) 的注释）。如果 Secret 无效就「跳过 JWT」，那么受保护的资源会变成「无认证可访问」，这是严重的安全降级。相反，写入一个无效 key 会让 NGINX Plus 在运行期返回 500——服务不可用，但绝不无认证放行。配置层只负责「把意图忠实翻译成配置」，Secret 是否有效由 secrets 子系统（[u6-l2](u6-l2-tls-and-secrets.md)）判断并通过告警反映，但不会削弱配置的安全性。

**练习 2**：`createHealthCheck` 复用的是哪种 K8s 资源作为健康检查参数来源？为什么这么做？

**参考答案**：复用 Pod 的 `readiness/liveness Probe`（`api_v1.Probe`，见 [ingress.go:49](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L49) 的 `HealthChecks map[string]*api_v1.Probe`）。原因有二：一是用户已经在 Deployment 里定义了 Probe，复用可避免重复配置；二是 Probe 描述的正是「应用自认为健康的条件」，与 NGINX 主动健康检查语义一致。注意这是 NGINX Plus 专属能力，OSS 不支持主动健康检查，所以只有 `cfgParams.HealthCheckEnabled`（且实际是 Plus）时才生成。

---

### 4.5 Mergeable Ingress 合并

#### 4.5.1 概念说明

标准 Ingress 有个痛点：一个 host 的所有 path 必须写在同一个 Ingress 里，不同团队无法各自管理自己的 path 子集。NIC 提供 **mergeable Ingress**（详见 [u4-l3](u4-l3-annotations-parsing.md) 的注解过滤）解决它：

- 一个 **master** Ingress（注解 `nginx.org/mergeable-ingress-type: master`）声明 host 和 host 级配置，**不写 path**。
- 多个 **minion** Ingress（注解 `nginx.org/mergeable-ingress-type: minion`）各写一批 path，**不写 host**（host 隐含来自 master）。

最终它们要合并成 **一个** `Server`。`generateNginxCfgForMergeableIngresses` 就是干这件事的。

#### 4.5.2 核心流程

1. 先 deepcopy master（因为要改它的注解），过滤 master 不该有的注解（`filterMasterAnnotations`）。
2. 用 master 调一次 `generateNginxCfg`（`isMinion=false`），拿到 master 的 `Server`，但**清空它的 locations**（只保留内部 auth location），保留 host 级配置。
3. 对每个 minion：
   - deepcopy、删掉它的 `DefaultBackend`（避免生成 `/`）、把 master 的可继承注解合并进 minion（`mergeMasterAnnotationsIntoMinion`）。
   - 用 minion 调一次 `generateNginxCfg`（`isMinion=true`），拿到 minion 的 `Server`（其实是「host 框 + minion 的 locations」）。
   - 把 minion 的 locations 抽出来，标记上 `MinionIngress`（供模板渲染 minion 名），合并进 master server。
4. 把所有 minion 的 upstreams、limitReqZones、maps、healthChecks、JWTRedirectLocations 累加进 master。
5. 返回「只含一个 Server」的 `IngressNginxConfig`。

注意 `isMinion` 标志在 `generateNginxCfg` 内部改变了大量行为：minion 把 JWT/BasicAuth/Policy 挂到 **location** 而非 server、egress mTLS 挂到 location、`AddHeaderInherit` 清空 server 级等（见 [ingress.go:437-440, 561-611](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L437-L440)）。

#### 4.5.3 源码精读

`generateNginxCfgForMergeableIngresses` 的主循环：

[internal/configs/ingress.go:1174-1232](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1174-L1232) —— master 处理：deepcopy（[1186](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1186)）、过滤注解（[1188](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1188)）、生成 master 配置（[1195-1206](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1195-L1206)）、取出 master server 并清空其 locations 但保留内部 auth location（[1215-1220](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1215-L1220)）。

minion 循环——合并 locations：

[internal/configs/ingress.go:1233-1347](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1233-L1347) —— 每个 minion：deepcopy（[1236](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1236)）、删 DefaultBackend（[1239](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1239)）、继承 master 注解（[1242](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1242)）、以 minion 身份生成（[1259-1271](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1259-L1271)）、把 minion 的 locations 合并进 master（[1281-1342](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1281-L1342)）。

合并时的几个关键细节：
- [1303](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1303) `loc.MinionIngress = &minionNginxCfg.Ingress`：给 location 打上「我来自哪个 minion」的标记，模板据此渲染 `# location for minion ns/name` 注释和 `$resource_name` 变量。
- [1293-1296](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1293-L1296) 若 minion 和 master 用同一个 ExternalAuth Policy，清掉 minion 的 location 级 ExternalAuth（避免重复 auth_request，靠 NGINX 指令继承）。
- [1311](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1311) `MergeProxySetHeaders`：proxy-set-headers 按「minion 优先于 master」合并。

最终只返回一个 Server：

[internal/configs/ingress.go:1354-1364](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1354-L1364) —— `Servers: []version1.Server{masterServer}`，把所有 minion 的产物累加进去。

模板里 minion location 的特殊渲染：

[internal/configs/version1/nginx.ingress.tmpl:241-245](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl#L241-L245) —— `{{- with $location.MinionIngress}}` 块渲染 minion 来源注释和 `$resource_name` 变量。

#### 4.5.4 代码实践

**实践目标**：理解「master 与 minion 各自调一次 `generateNginxCfg`，最终合成一个 server」的两段式结构。

**操作步骤**：

1. 在 [generateNginxCfgForMergeableIngresses](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1174-L1365) 里找出两次 `generateNginxCfg` 调用（master 一次、minion 一次），对比它们的 `NginxCfgParams`：
   - master：`isMinion: false`，`ingEx: ncp.mergeableIngs.Master`。
   - minion：`isMinion: true`，`ingEx: minion`，且传入 `mergeableIngs`（用于 ownerDetails 回溯到 master）。
2. 解释为什么 minion 要先 `minion.Ingress.Spec.DefaultBackend = nil`（[ingress.go:1239](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1239)）。

**需要观察的现象**：minion 的 `Server` 里的 `server_name` 与 master 相同（因为 minion 的 host 来自继承），但只有 locations 有用，host 级配置会被丢弃——所以合并时只取 minion 的 locations（[1282](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1282)），不取 minion 的 server。

**预期结果**：你能用一句话说清 minion 删 DefaultBackend 的原因——「`/` 根 location 是 master 级语义，若 minion 也声明 DefaultBackend 会与其它 minion 抢占 `/`，所以合并前强制清空，让 `/` 只能由 master 或 path 显式声明」。这一行为可在 [examples/ingress-resources/mergeable-ingress/](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/) 目录下的 mergeable 示例中对照确认（待本地查找确切示例文件）。

#### 4.5.5 小练习与答案

**练习 1**：合并时为什么要 deepcopy master 和 minion 的 Ingress 对象（[ingress.go:1186, 1236](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1186)）？

**参考答案**：因为合并过程会**修改** Ingress 对象——删 minion 的 DefaultBackend、把 master 注解合并进 minion、过滤某些注解。而 Ingress 对象来自控制器的共享缓存（[u3-l2](u3-l2-shared-informers.md) 的 Informer cache），直接改会污染缓存、影响其它处理路径、甚至引发并发问题。deepcopy 后只在副本上修改，原对象保持不变。这是「写时复制」的防御性编程。

**练习 2**：master 的 locations 在合并前被清空，但 `filterInternalLocations` 保留了内部 auth location（[ingress.go:1219](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L1219)）。为什么？

**参考答案**：master 级的 ExternalAuth Policy 会生成 `internal;` 的 auth 子请求 location（如 `/_external_auth/...`，见 [u4.4 resolveExternalAuth](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L824-L854)），这些 location 是 server 级 auth_request 指令的「基础设施」，必须保留在合并后的 server 里。如果一并清空，server 级的 Policy 认证就会失效。所以清空的是「业务 location」（master 本就不该有 path），保留的是「基础设施 location」。

---

## 5. 综合实践

把本讲的「Ingress path → location + upstream」链路完整走一遍，并对比普通 Ingress 与 mergeable Ingress 的产物差异。

**任务**：

1. **选一个示例 Ingress**：用 [cafe-ingress.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/ingress-resources/complete-example/cafe-ingress.yaml)（普通 Ingress，两条 path `/tea`、`/coffee`，带 TLS）。

2. **手推生成结果**（不运行，纯源码阅读）：
   - 写出 `/tea` 和 `/coffee` 各自的 upstream 名（套 `getNameForUpstream` 公式）。
   - 写出两个 location 的 `proxy_pass`（注意协议：cafe-ingress 带了 TLS，但 TLS 终止在 NGINX，到 upstream 是 `http` 还是 `https`？查 `isSSLEnabled` 和注解 `nginx.org/ssl-services`——cafe-ingress 没有该注解，所以是 `http`）。
   - 写出 `server_name`、`ssl_certificate`（来自 `tls-secret` 的落盘路径）。

3. **对照快照验证**：在仓库找 Ingress 配置生成的快照测试，例如：

   ```bash
   ls internal/configs/version1/__snapshots__/
   ```

   找到与 cafe 或类似 Ingress 对应的快照 `.snap` 文件，与你手推的 upstream 名、proxy_pass 对照（待本地运行确认确切快照文件名）。

4. **进阶**：假设把 cafe-ingress 拆成一个 master（只声明 host `cafe.example.com` 和 TLS）+ 一个 minion（声明 `/tea`、`/coffee`）。回答：
   - 合并后生成几个 `Server`？（答：1 个）
   - minion 的 location 上的 `MinionIngress` 字段会被模板渲染成什么？（答：`# location for minion <ns>/<name>` 注释 + `set $resource_name` 变量）

**预期产出**：一张字段对照表 + 一段对「普通 vs mergeable 产物差异」的说明。这一步把 4.2（upstream）、4.3（location）、4.4（TLS）、4.5（mergeable）四个最小模块串了起来。

> 说明：本实践是「源码阅读 + 手推」型，不要求真实集群。若要在真实集群验证，需先 `make build` 构建二进制、部署到测试集群、apply cafe-ingress 及其 Secret，再 `kubectl exec` 进 NGINX Pod 查看 `/etc/nginx/conf.d/` 下生成的 `.conf` 文件（待本地验证）。

## 6. 本讲小结

- **`IngressEx` 是信息完备的输入模型**：它把裸 Ingress 和所有周边资源（Endpoints、Probe、Secret、Policy）打包，让配置层「只执行不查 API」，分层干净。
- **upstream 靠确定性公式命名**：`namespace-ingress-host-service-port` 保证唯一且可复现，map 去重 + 排序保证生成顺序稳定；无 Endpoints 时用 `127.0.0.1:8181` 占位优雅降级。
- **Server 对应 host、Location 对应 path**：`generateNginxCfg` 双层循环生成它们；`createLocation` 把 proxy 超时/缓冲等从 `cfgParams` 直接搬运，`ProxyPass` 按 ssl 选协议、`Path` 按 pathType 加修饰符。
- **TLS、健康检查、认证各有专门构造函数**：`addSSLConfig`（host 级 TLS）、`createHealthCheck`（复用 Pod Probe，Plus 专属）、`generateJWTConfig`/`generateBasicAuthConfig`（fail-closed：Secret 无效仍写路径让 NGINX 返 500）。
- **mergeable Ingress 合并成单 Server**：master 提供 host 框架、minion 提供 locations，通过 `isMinion` 标志让同一份 `generateNginxCfg` 服务两种角色，合并时 deepcopy 防污染、保留内部 auth location。
- **生成结果是 `IngressNginxConfig`**：它是 [nginx.ingress.tmpl](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.ingress.tmpl) 的输入，由 `ExecuteIngressConfigTemplate` 渲染成 `.conf`，再由 Configurator 写盘 + reload。

## 7. 下一步学习建议

本讲讲完了 **Ingress（version1）** 的配置生成。建议接下来：

- **[u4-l5 VirtualServer → 配置（version2 http）](u4-l5-virtualserver-config-generation.md)**：对比 VirtualServer 的生成（`virtualserver.go`）。重点关注它如何用 `upstreamNamer`、split client 做流量切分——这是 Ingress 做不到的，也是 VirtualServer 存在的核心理由。
- **[u4-l7 Policy → 配置](u4-l7-policy-config-generation.md)**：本讲多次提到 `policyCfg`（`Allow/Deny/WAF/EgressMTLS/ExternalAuth`）来自 Policy 翻译。去读 `policy.go` 的 `generatePolicies`，理解这些字段是怎么来的。
- **[u4-l8 Go 模板渲染体系](u4-l8-template-rendering.md)**：本讲的产物 `IngressNginxConfig` 是怎么被 `.tmpl` 渲染的、OSS 与 Plus 模板有何差异、快照黄金测试如何保证输出稳定。
- **[u6-l2 TLS/SSL 与 Secret 处理](u6-l2-tls-and-secrets.md)**：本讲的 `addSSLConfig` 依赖 `secretRefs`，那里面的 Secret 是怎么被缓存、校验、落盘的，值得深入。
