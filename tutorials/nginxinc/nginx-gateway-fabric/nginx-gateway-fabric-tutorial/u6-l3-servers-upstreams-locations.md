# Servers、Upstreams 与 Locations 生成

> 承接 [u6-l2 nginx.conf 骨架与模板体系](u6-l2-nginx-conf-and-templates.md)。上一讲我们看到 `nginx.conf` 只是一张「永不改写的静态骨架」，真正的业务内容由四条 `include` 通配拉进 main/events/http/stream 上下文。本讲就钻进 http 上下文里最核心的两块内容——**server 块**与**upstream 块**，看 NGF 如何把领域模型 `dataplane.Configuration` 翻译成 NGINX 的 server/location/upstream 指令。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `servers.go` 如何把 `HTTPServers`/`SSLServers` 翻译成 NGINX `server {}` 块，以及 location 块的几种「生成路径」是如何按 HTTPRoute 的匹配条件分支的。
- 说清楚 `upstreams.go` 如何决定一个 upstream 的 **zone 大小、负载均衡方法、keepalive、state 文件**，并理解 OSS 与 NGINX Plus 在这条路径上的差异。
- 看懂「多后端加权」「无效后端引用」「无端点」三种特殊情况下，proxy_pass / upstream 分别退化成什么。
- 能把一条 HTTPRoute 的 `path` + `backendRefs`，在脑子里映射成最终的 `location {}` + `upstream {}` 配置。

## 2. 前置知识

- **NGINX 的 http 配置三件套**：`server {}` 决定监听哪个端口/主机名，`location {}` 决定一条请求路径怎么处理，`upstream {}` 是一组后端服务器的「命名池」，由 `proxy_pass http://<upstream名>` 引用。本讲只讲这三者的生成。
- **upstream zone**：NGINX 的 `zone <名字> <大小>;` 指令在 worker 间共享 upstream 的运行时状态（各 server 的连接数、负载），让多个 worker 用同一份负载统计做调度。没有 zone，每个 worker 各算各的，负载会不均。
- **keepalive（upstream 侧）**：`keepalive N;` 让 NGINX 与后端之间维持一个最多 N 条的持久连接缓存，避免每次请求都重新建连。它有一个硬性约束：**keepalive 指令必须出现在负载均衡方法之后**，否则 NGINX 报错。
- **split_clients**：NGINX 的 `split_clients <哈希种子> $<变量> { 百分比 值; ... }` 指令，按请求特征把流量按百分比切到不同分支。NGF 用它实现「多后端加权」。
- 本讲输入是 `dataplane.Configuration`（见 [u5-l4](u5-l4-dataplane-configuration.md)），输出是 `[]agent.File`（见 [u6-l1](u6-l1-config-generator-overview.md)）。本讲只覆盖其中的 `executeServers` 与 `createUpstreams` 两个执行函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/controller/nginx/config/servers.go` | 把 `HTTPServers`/`SSLServers` 翻译成 `[]http.Server`，并为每条 PathRule 生成 location；location 内的 proxy_pass、过滤器（认证/CORS/重写/镜像）都在这里装配 |
| `internal/controller/nginx/config/upstreams.go` | 把 `dataplane.Upstream` 翻译成 `http.Upstream`/`stream.Upstream`，决定 zone 大小、LB 方法、keepalive、state 文件 |
| `internal/controller/nginx/config/servers_template.go` | server/location 块的 Go `text/template` 文本 |
| `internal/controller/nginx/config/upstreams_template.go` | upstream 块的模板文本（http 与 stream 各一份） |
| `internal/controller/nginx/config/http/config.go` | `http.Server`/`http.Location`/`http.Upstream` 等渲染结构体定义 |
| `internal/controller/nginx/config/generator.go` | 总装：`getExecuteFuncs` 把上面两个执行函数挂进模板执行链 |
| `internal/controller/nginx/config/split_clients.go` | 加权后端所需的 `split_clients` 生成，以及 `backendGroupName`/`backendGroupNeedsSplit` |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 server 块生成**、**4.2 upstream 与 zone**、**4.3 location 匹配**。三者数据流关系如下：

```
dataplane.Configuration
   ├── HTTPServers / SSLServers ──► servers.go ──► http.Server{} ──► servers_template ──► http.conf
   │                                    │
   │                                    └── 每条 PathRule ──► location{}（proxy_pass 指向 upstream 名）
   │
   └── Upstreams ──► upstreams.go ──► http.Upstream{} ──► upstreams_template ──► http.conf（同文件，按 dest 分桶）
```

注意：servers 和 upstreams 的模板产物都写入同一个 `http.conf`（这是 [u6-l1](u6-l1-config-generator-overview.md) 讲过的「按 dest 分桶合并」），所以最终 `server {}` 块和 `upstream {}` 块会出现在同一份文件里。

### 4.1 server 块生成

#### 4.1.1 概念说明

NGINX 的一个 `server {}` 块对应一个「虚拟服务器」，由 `listen`（端口）+ `server_name`（主机名）+ 若干 `location {}` 组成。NGF 把一个 Gateway Listener 翻译成一个 `dataplane.VirtualServer`，再由 `servers.go` 翻译成一个 `http.Server`。

需要区分三类 server：

1. **业务 server**：带 hostname 和 locations，是真正处理流量的。
2. **default server（HTTP）**：`IsDefaultHTTP=true`，监听端口、不配 server_name，直接 `return 404`，用来兜住「没有 server_name 匹配」的请求。
3. **default server（SSL）**：`IsDefaultSSL=true`，用于 TLS 端口的兜底——没有证书匹配时，要么 `ssl_reject_handshake on` 直接拒绝握手，要么配默认证书。

#### 4.1.2 核心流程

`executeServers` 的总流程：

1. 调 `createServers(conf, ...)` 把 `HTTPServers` + `SSLServers` 翻译成 `[]http.Server`，同时产出一份 `httpMatchPairs`（给 NJS 模块用的匹配表）。
2. 包成 `http.ServerConfig`（带 IPFamily、Plus 标志、RewriteClientIP 设置）。
3. 用 `serversTemplate` 渲染成 server 块文本。
4. 把 `httpMatchPairs` 序列化成 `matches.json`（独立文件，由 NJS 的 httpmatches 模块加载）。
5. 把 server/location 上的 policy/snippet include 收集成独立 `.conf` 文件（`createIncludeExecuteResultsFromServers`）。

#### 4.1.3 源码精读

入口 `executeServers`，先建 server 列表、再渲染、再补 matches.json 与 include 文件：

[servers.go:L114-L153](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L114-L153) —— `executeServers`：渲染 server 块并产出 `http.conf` 与 `matches.json` 两个 dest。

`createServers` 遍历 `HTTPServers` 与 `SSLServers`，分别用 `createServer` 与 `createSSLServer` 翻译。注意它先把所有 SSL server 的端口收集进 `sharedTLSPorts`——这是为了让「同端口复用」的 SSL server 改走 unix socket 监听：

[servers.go:L167-L215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L167-L215) —— `createServers`：对 HTTP 用 `"0","1",...` 作 serverID，对 SSL 用 `"SSL_0",...`；若某 SSL server 的端口已被多个 SSL server 共用，则把它改成 socket 监听（`IsSocket=true`），避免端口冲突。

单个 HTTP server 的翻译（`createServer`）很直白：default server 直接返回 404 兜底块；业务 server 调 `createLocations` 生成 locations，再追加 policy/snippet 的 include：

[servers.go:L305-L346](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L305-L346) —— `createServer`：业务 server 装配 `ServerName` + `Locations` + `Includes`；SSL server（`createSSLServer`）在此基础上额外挂 `SSL` 证书指令与「misdirected request」（421）检测变量。

模板侧，业务 server 块长这样（节选自 `serversTemplateText`）：

[servers_template.go:L81-L143](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L81-L143) —— 业务 server 模板：根据是否 SSL 决定 `listen ... ssl`、渲染证书指令、`server_name`、（Plus 的）`status_zone`，以及 include 和 `set_real_ip_from`。

模板末尾还有两个**固定兜底 socket server**，这是理解 upstream 兜底行为的关键：

[servers_template.go:L331-L343](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L331-L343) —— 两个兜底 server：`nginx-503-server.sock` 返回 503（给「无端点」的 upstream 用），`nginx-500-server.sock` 返回 500（给「无效后端引用」的 upstream 用）。

> 这两个兜底块是**静态写死**的，每次生成都在。它们的存在解释了下一节 `upstreams.go` 里为何能放心地把失效 upstream 指向一个固定地址。

#### 4.1.4 代码实践

**实践目标**：验证「default server 兜底 404」与「SSL default server 拒绝握手」两种行为在模板里确实存在。

**操作步骤（源码阅读型）**：

1. 打开 `servers_template.go`，找到 `IsDefaultHTTP` 分支，确认它渲染出 `listen ... default_server` + `return 404;`。
2. 找到 `IsDefaultSSL` 分支，确认当 `$s.SSL` 为空时渲染 `ssl_reject_handshake on;`。
3. 在 `internal/controller/nginx/config/` 目录下搜 `IsDefaultHTTP: true`，找到构造 default server 的测试，对照看它生成的文本。

**需要观察的现象**：

- HTTP default server 没有 `server_name`，但有 `default_server` 标记——任何未匹配到业务 server_name 的请求都会落到这里并拿到 404。
- SSL default server 在没有证书时用 `ssl_reject_handshake on` 而非 `return`，因为它在 TLS 握手阶段就要拒绝（此时 HTTP 还没开始）。

**预期结果**：能用自己的话解释「为什么 HTTP 兜底用 `return 404` 而 SSL 兜底用 `ssl_reject_handshake`」——因为前者在 HTTP 层、后者必须在 TLS 握手层。本步骤不运行集群，属纯源码阅读。

### 4.2 upstream 与 zone

#### 4.2.1 概念说明

`upstream {}` 块是后端服务器池。NGF 在生成它时要决定五件事：

1. **zone 大小**：`zone <名> <大小>;`。OSS 固定 `512k`，Plus 固定 `1m`（Plus 共享状态更多，需要更大空间）。
2. **负载均衡方法**：默认 `random two least_conn`（注意：**不是** NGINX 默认的 round-robin）。可被 `UpstreamSettingsPolicy` 覆盖成 ip_hash / hash / round_robin 等。
3. **keepalive**：默认开启 16 条缓存连接（`keepalive 16;`），可被策略改写或设为 0 关闭。
4. **state 文件**：仅 Plus 用 `state /var/lib/nginx/state/<名>.conf;`，让上游 server 列表能被持久化、通过 Plus API 动态增删而不 reload。
5. **server 列表**：每个 endpoint 一条 `server <IP>:<port>;`。

#### 4.2.2 核心流程

`createUpstreams`（批量入口）→ 遍历每个 `dataplane.Upstream` 调 `createUpstream`（单个）→ 末尾再 append 一个「无效后端引用」专用 upstream。单个 upstream 的决策树：

```
zoneSize = OSS ? "512k" : "1m"
if Plus 且 该upstream无resolve server:
    stateFile = /var/lib/nginx/state/<名>.conf
    解析 SessionPersistence（sticky cookie）
if UpstreamSettingsPolicy 指定了 ZoneSize:
    zoneSize = 策略值覆盖
chosenLBMethod = "random two least_conn"   ← 默认
if 策略指定了 LB 方法:
    chosenLBMethod = 策略值（hash / round_robin / ...）
keepAlive = processKeepAliveSettings(策略)   ← nil→默认16；0→关闭
if Endpoints 为空:
    server 列表 = [nginx-503-server.sock]   ← 返回 503
else:
    server 列表 = endpoints
```

#### 4.2.3 源码精读

批量入口 + 末尾兜底 upstream：

[upstreams.go:L133-L147](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L133-L147) —— `createUpstreams`：容量是「upstream 数 + 1」，那个 +1 留给最后的 `invalid-backend-ref` 兜底 upstream。

单个 upstream 的核心决策（zone/state/LB/keepalive 都在这里）：

[upstreams.go:L149-L230](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L149-L230) —— `createUpstream`：注意 zone/state 只在 Plus 下配 state，且 `resolve server` 的 upstream 不能配 state（它由 DNS 动态解析，不能用 Plus API 管理）；策略的 `ZoneSize` 会覆盖默认值；LB 方法里 `RoundRobin` 被翻译成空串（即「什么都不写 = NGINX 默认 round_robin」）。

keepalive 归一化逻辑（理解默认值的关键）：

[upstreams.go:L235-L249](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L235-L249) —— `processKeepAliveSettings`：`Connections==nil`（用户没配）→ 填默认 16；`Connections==0`（用户显式关闭）→ 返回不含 Connections 的结构体，模板就不会输出 `keepalive` 指令。

「无效后端引用」专用 upstream，指向 4.1.3 里的 500 socket：

[upstreams.go:L251-L261](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L251-L261) —— `createInvalidBackendRefUpstream`：名字固定 `invalid-backend-ref`，故意**不带 zone**（因为它永远只 proxy 到一个固定 socket，不需要共享状态），server 指向 `nginx-500-server.sock`。

zone 大小常量与默认 LB 方法：

[upstreams.go:L21-L38](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L21-L38) —— 常量：OSS http/stream 都是 `512k`，Plus 都是 `1m`；`defaultLBMethod = "random two least_conn"`。注释解释 512k 大约能撑 648 个 http upstream server。

模板侧，注意指令顺序：

[upstreams_template.go:L10-L47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams_template.go#L10-L47) —— http upstream 模板：顺序是 **LB 方法 → zone → sticky → state/servers → keepalive 系列**。开头的 FIXME 注释点明「keepalive 必须出现在 LB 方法之后」，这正是把 LB 方法放在最前的原因。

stream upstream 模板略有不同（写死了 `random two least_conn`，且支持 weight）：

[upstreams_template.go:L49-L67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams_template.go#L49-L67) —— stream upstream 模板：`weight=` 仅在权重 ≠0 且 ≠1 时输出。

#### 4.2.4 代码实践

**实践目标**：观察「默认 upstream」与「被 UpstreamSettingsPolicy 覆盖的 upstream」在 zone / keepalive 上的差异。

**操作步骤**：

1. 打开 `internal/controller/nginx/config/upstreams_test.go`，定位 `TestExecuteUpstreams_NginxOSS`。它构造了一组 upstream：有的不带策略（用默认），有的带 `UpstreamSettingsPolicy`（`ZoneSize=2m`、`KeepAlive.Connections=1`、`LoadBalancingMethod=IPHash`），有的把 `Connections` 设成 0（关闭 keepalive）。

   参考测试用例（节选自仓库现有测试）：

   ```go
   // upstreams_test.go 中存在如下用例（示例代码，非逐字复制）
   { Name: "up5-usp", Policies: []policies.Policy{ &ngfAPI.UpstreamSettingsPolicy{
       Spec: ngfAPI.UpstreamSettingsPolicySpec{
           ZoneSize: helpers.GetPointer[ngfAPI.Size]("2m"),
           KeepAlive: helpers.GetPointer(ngfAPI.UpstreamKeepAlive{
               Connections: helpers.GetPointer(int32(1)),
               Requests:    helpers.GetPointer(int32(1)),
           }),
           LoadBalancingMethod: helpers.GetPointer(ngfAPI.LoadBalancingTypeIPHash),
       },
   }}}
   ```

2. 在该测试里**只读地**跟踪三类用例各自渲染出的 upstream 文本：
   - 无策略 → `random two least_conn;` + `zone <名> 512k;` + `keepalive 16;`
   - 带 `ZoneSize=2m` + `Connections=1` + `IPHash` → `ip_hash;` + `zone <名> 2m;` + `keepalive 1;` + `keepalive_requests 1;`
   - `Connections=0` → 没有 `keepalive` 指令，但有 `keepalive_requests` 等。

3.（可选，需集群）在一个已部署 NGF 的环境里，给某个后端 Service 附着 `UpstreamSettingsPolicy` CRD，再进数据面 Pod 看 `/etc/nginx/conf.d/http.conf` 里对应 upstream 的变化。

**需要观察的现象**：

- 默认情况下**每个 upstream 都带 `random two least_conn;`**（不是 round-robin）和 `keepalive 16;`——这是 NGF 与原生 NGINX 默认值最容易被忽略的差异。
- `ZoneSize` 是唯一会被策略覆盖的字段；OSS 默认 512k，策略可改成 2m。

**预期结果**：能复述「NGF 默认 LB 方法 = random two least_conn，默认 keepalive = 16」并指出这两个值分别来自 `upstreams.go:37` 的 `defaultLBMethod` 与 `http/config.go:12` 的 `KeepAliveConnectionDefault`。若本地能跑测试，运行 `go test ./internal/controller/nginx/config/ -run TestExecuteUpstreams_NginxOSS -v` 应全绿；若不能跑，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `invalid-backend-ref` upstream 没有 `zone` 指令？

**参考答案**：见 `createInvalidBackendRefUpstream` 注释「ZoneSize is omitted since we will only ever proxy to one destination/backend」。它永远只指向固定的 `nginx-500-server.sock`，没有多 server 的负载统计需求，加 zone 纯属浪费共享内存。

**练习 2**：把 `UpstreamSettingsPolicy` 的 `LoadBalancingMethod` 设成 `RoundRobin`，生成的 upstream 里会出现 `round_robin;` 指令吗？

**参考答案**：不会。`createUpstream` 里对 `LoadBalancingTypeRoundRobin` 特判，把 `chosenLBMethod` 置空（`lbMethod = ""`），模板 `{{ if $u.LoadBalancingMethod }}` 就不输出该指令——因为 NGINX 不写 LB 方法时默认就是 round_robin，显式写反而多余。

### 4.3 location 匹配

#### 4.3.1 概念说明

`location {}` 是 NGINX 把「请求路径」分发给不同处理逻辑的最小单元。Gateway API 的 HTTPRoute 规则（path + method + headers + queryParams 匹配，外加各种 filter）必须被翻译成一组 location 块。这是整个 `servers.go` 里最复杂的部分。

NGF 的 location 生成有一条核心原则：**一个外部 location 不一定直接 proxy_pass，它可能只是个「分发器」**，把请求按匹配条件交给内部 location。`servers.go` 顶部那段大注释枚举了所有可能的「location 流」，值得通读。

#### 4.3.2 核心流程

`createLocations` 是总入口，它对每个 PathRule 判断走哪条分支：

```
对 server 的每条 PathRule:
    若 无HTTP匹配条件 且 无推理后端:
        → 直接生成「外部 location → proxy_pass」(updateExternalLocationsForRule)
    若 有HTTP匹配条件(method/headers/queryParams):
        → 生成「外部 redirect location」+ 若干「内部 location」
          外部 location 调 NJS(httpmatches.match) 选分支，再 rewrite 到内部 location
          内部 location 才真正 proxy_pass
    若 有推理后端(InferencePool):
        → 生成「inference location」链，调 NJS(epp.getEndpoint) 拿端点
最后补:
    OIDC 回调 location、默认根 location(= / → 404)、JWT JWKS 内部 location、外部鉴权内部 location
```

**path 类型到 location 路径的映射**（`createPath`）：

| PathType | 生成的 location 路径 | NGINX 语义 |
| --- | --- | --- |
| `Exact` | `= /path` | 精确匹配 |
| `Prefix` | `/path` | 前缀匹配 |
| `RegularExpression` | `~ ^/path` | 正则匹配 |

**关键边界：无尾斜杠的 Prefix 路径**会生成**两个** location。比如 PathPrefix `/coffee`（无尾斜杠），NGINX 的前缀匹配会把 `/coffeeXXX` 也匹配进去（不符合 Gateway API 语义），所以 NGF 额外生成一个精确 `= /coffee` 和一个 `/coffee/`（带尾斜杠的前缀），避免误匹配。

#### 4.3.3 源码精读

location 生成的总入口与三种分支：

[servers.go:L529-L623](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L529-L623) —— `createLocations`：核心是中间的 `switch`，三个 case 对应「直接 proxy」「HTTP 匹配走内部 location」「推理后端」三条路径；末尾追加 OIDC / 默认根 / JWKS / 外部鉴权 location。

那段枚举所有 location 流的大注释（强烈建议通读）：

[servers.go:L384-L525](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L384-L525) —— 用 NGINX 伪配置说明了 base case、HTTP 匹配、推理扩展（单/多后端、有无匹配条件）共 6 种 location 流。

path 到 location 路径的映射：

[servers.go:L2070-L2081](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L2070-L2081) —— `createPath`：三种 path 类型的映射；未知类型直接 `panic`（fail-fast）。

无尾斜杠 Prefix 的双 location 处理：

[servers.go:L1051-L1099](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1051-L1099) —— `initializeExternalLocations`：当 Prefix 路径无尾斜杠时，若已存在同路径的 Exact 或带斜杠 Prefix 就跳过（避免重复 location 导致 NGINX 报错），否则补一个 `path/` 和一个 `= path`。

单个 location 的装配（proxy_pass、过滤器都在这里）：

[servers.go:L1192-L1236](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1192-L1236) —— `updateLocation`：依次处理「无效 filter→500」「镜像路由」「snippet/auth/CORS filter」「重定向 filter（会 early return）」「URL 重写 filter」「镜像 filter」「代理设置（proxy_pass）」。顺序很重要，重定向会提前返回不再走 proxy_pass。

proxy_pass 与 proxy 头的装配：

[servers.go:L1542-L1597](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1542-L1597) —— `updateLocationProxySettings`：决定 `proxy_http_version`、基础 `proxy_set_header`（含 Host / X-Forwarded-*）、proxy_pass 协议（http/https/grpc/grpcs）、后端 TLS 校验。

proxy_pass 字符串的构造（理解多后端加权的关键）：

[servers.go:L1928-L1959](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1928-L1959) —— `createProxyPass`：单后端→`http://<upstream名>`；多后端（`backendGroupNeedsSplit`）→`http://$<变量名>`（由 split_clients 按权重分流）；推理后端→`http://$inference_backend_<名>`（由 NJS 填值）。

**加权分流的数学**：多后端时，每个后端的流量百分比由 `split_clients.go` 的 `percentOf` 计算，用向下取整保证总和不超过 100：

\[
p = \frac{\lfloor \dfrac{w \times 100}{W} \times 100 \rfloor}{100}
\]

其中 \(w\) 是该 backend 的 weight，\(W\) 是同组所有 backend 的 weight 之和。用 floor 而非四舍五入，是为了避免几个百分比相加后超过 100% 导致 NGINX 校验失败。

keepalive 与 server 块的联动（解释 server 怎么知道 upstream 是否开了 keepalive）：

[servers.go:L2276-L2289](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L2276-L2289) —— `getConnectionHeader`：location 上的 `proxy_set_header Connection` 取值取决于后端 upstream 是否开了 keepalive——开了用 `$connection_keepalive`，没开用 `$connection_upgrade`。这是 server 生成依赖 upstream 生成结果的耦合点，由 `keepAliveChecker` 闭包传递。

模板侧 location 块（节选最关键的 proxy_pass 部分）：

[servers_template.go:L286-L321](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L286-L321) —— location 模板的尾部：渲染 `proxy_set_header`、`proxy_pass`、`proxy_pass_request_body/headers`、响应头增删、后端 TLS 校验指令。

#### 4.3.4 代码实践

**实践目标**：把一条具体 HTTPRoute 在脑子里映射成 location + upstream 配置。

**操作步骤（源码阅读 + 推演）**：

1. 假设有这样一条 HTTPRoute（示例，非仓库文件）：

   ```yaml
   # 示例代码：仅用于说明映射关系
   spec:
     parentRefs: [{ name: my-gateway, sectionName: http }]
     hostnames: ["cafe.example.com"]
     rules:
       - matches:
           - path: { type: PathPrefix, value: /coffee }
         backendRefs:
           - name: coffee-svc
             port: 80
   ```

2. 在源码里逐步推演它会经过哪些函数：

   - `createPath`（PathPrefix `/coffee` 无尾斜杠）→ 因为是「无尾斜杠 Prefix」，会进 `initializeExternalLocations` 的双 location 分支。
   - 该规则**没有** method/header/queryParam 匹配，`needsInternalLocationsForMatches` 返回 false → 走「直接 proxy」分支（`updateExternalLocationsForRule`）。
   - `createProxyPass`：单后端 → `proxy_pass http://<coffee-svc 对应的 upstream 名>;`
   - `coffee-svc` 对应的 upstream 由 `createUpstream` 生成：默认 `random two least_conn; zone <名> 512k; keepalive 16; server <podIP>:80;`

3. 写出你预期该规则在 `/etc/nginx/conf.d/http.conf` 中生成的两段配置（location 双块 + upstream 块）。

**需要观察的现象**：

- 因为 `/coffee` 是无尾斜杠 Prefix，你应预期看到**两个** location：`= /coffee` 和 `/coffee/`。
- location 里应带 `proxy_set_header Connection "$connection_keepalive";`（因为 upstream 默认开了 keepalive 16）。

**预期结果**：手写出的配置里，location 用前缀/精确两条覆盖，upstream 带 `random two least_conn` 与 `keepalive 16`。若不确定具体 upstream 名格式，可对照 `backendGroupName`（[split_clients.go:L221-L234](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/split_clients.go#L221-L234)）确认单后端时取 `b.UpstreamName`。

#### 4.3.5 小练习与答案

**练习 1**：如果两条 HTTPRoute 规则分别配 `PathPrefix /tea` 和 `Exact /tea`，会生成几个 location？会不会冲突？

**参考答案**：`/tea`（Prefix 无尾斜杠）本应生成 `= /tea` + `/tea/`，但 `initializeExternalLocations` 检测到已存在同路径的 `Exact`（`exactPathExists`），于是跳过 `= /tea`，只补 `/tea/`。最终是 `= /tea`（来自 Exact 规则）+ `/tea/`（来自 Prefix 规则）两条，不冲突——这正是去重逻辑存在的意义，否则两条 `= /tea` 会让 NGINX reload 失败。

**练习 2**：`needsInternalLocationsForMatches` 在什么情况下返回 true？

**参考答案**：见 [servers.go:L963-L969](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L963-L969)。两种情况：MatchRules 多于 1 条；或只有 1 条但该 match 不是「纯 path 匹配」（即带了 method/headers/queryParams）。一旦为 true，外部 location 就退化成「调 NJS 选分支」的 redirect location，真正的 proxy_pass 落在内部 location。

## 5. 综合实践

**任务**：在源码里追踪一条「带 method 匹配 + 两个加权后端」的 HTTPRoute 规则，画出它从 `dataplane.Configuration` 到最终 `http.conf` 文本的完整生成路径，并指出三个最小模块各自的贡献点。

**具体步骤**：

1. 构造一个假想规则（示例，非仓库文件）：

   ```yaml
   # 示例代码
   rules:
     - matches:
         - path: { type: PathPrefix, value: /api }
           method: POST
       backendRefs:
         - name: svc-a
           port: 80
           weight: 7
         - name: svc-b
           port: 80
           weight: 3
   ```

2. 分别在三个模块里定位它会经过的函数：
   - **server 块（4.1）**：`createLocations` → 因带 method 匹配，走 `createInternalLocationsForRule`，生成 1 个外部 redirect location（`js_content httpmatches.redirect`）+ 1 个内部 location。
   - **location 匹配（4.3）**：`createProxyPass` → 两个后端 → `backendGroupNeedsSplit` 为 true → proxy_pass 形如 `http://$<变量名>`；`split_clients` 按 `percentOf(7,10)=70.00` 与 `percentOf(3,10)=30.00` 生成分流块。
   - **upstream（4.2）**：`createUpstreams` 为 svc-a、svc-b 各生成一个 upstream（各自 `random two least_conn` + `zone 512k` + `keepalive 16`），末尾再补 `invalid-backend-ref` 兜底。

3. 用一张图把「server → location → split_clients → upstream」串起来，标注每段由哪个文件的哪个函数产出。

**验收标准**：能说清「为什么带 method 匹配时外部 location 不直接 proxy_pass」「两个后端时 proxy_pass 为什么是变量而不是固定 upstream 名」「upstream 的 zone/keepalive 默认值从哪来」这三个问题——它们分别对应本讲的三个最小模块。

## 6. 本讲小结

- `servers.go` 把 `HTTPServers`/`SSLServers` 翻译成 `http.Server`，渲染出业务 server、default server（HTTP `return 404`、SSL `ssl_reject_handshake`）以及两个固定兜底 socket（503/500）。
- `upstreams.go` 对每个 upstream 决定 zone（OSS 512k / Plus 1m，可被策略覆盖）、LB 方法（默认 `random two least_conn`）、keepalive（默认 16，0 则关闭）、state 文件（仅 Plus）。失效情况退化成 503（无端点）/500（无效引用）。
- location 生成按「是否带匹配条件 / 是否推理后端」分三条路径：直接 proxy、HTTP 匹配走 NJS 内部 location、推理走 EPP 内部 location；无尾斜杠 Prefix 会拆成双 location 并做去重。
- 多后端加权用 `split_clients`，百分比用 `percentOf` 向下取整保证总和 ≤ 100；proxy_pass 变成 `$变量`。
- server 与 upstream 的耦合点是 `keepAliveChecker`：location 的 `Connection` 头取值取决于后端 upstream 是否开了 keepalive。
- 所有 server/upstream 模板产物按 dest 分桶合并进同一份 `http.conf`，由 [u6-l1](u6-l1-config-generator-overview.md) 的总装负责。

## 7. 下一步学习建议

- **stream 上下文**：本讲的 stream upstream 模板只是 http 版本的「精简版」。要完整看 TCP/UDP/TLS Passthrough 如何生成 `stream {}` 块，进入 [u6-l4 Stream 配置：TCP/UDP/TLS Passthrough](u6-l4-stream-tcp-udp-config.md)。
- **策略生成器**：本讲多次提到 location/server/upstream 上的 `Includes`（policy/snippet）。这些 include 文件由 `internal/controller/nginx/config/policies/` 下的复合生成器产出，详见 [u8-l3 策略到 NGINX 指令的生成](u8-l3-policy-generation.md)。
- **配置下发**：本讲产出的 `[]agent.File` 如何经 NGINX Agent 写到数据面 Pod，进入 [u7-l1 NginxUpdater 与 gRPC Agent 通信](u7-l1-nginx-agent-updater-and-grpc.md)。
- **动手验证**：想真实看到生成的 `http.conf`，可参考 [u1-l5 运行第一个例子](u1-l5-first-example.md) 的端到端流程部署 cafe-example，进数据面 Pod 查看 `/etc/nginx/conf.d/http.conf`，对照本讲的 server/upstream/location 三类块逐一印证。
