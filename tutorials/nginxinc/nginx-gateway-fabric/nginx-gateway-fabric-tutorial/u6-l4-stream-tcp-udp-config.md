# Stream 配置：TCP/UDP/TLS Passthrough

> 承接 [u6-l2 nginx.conf 骨架与模板体系](u6-l2-nginx-conf-and-templates.md) 与 [u6-l3 Servers、Upstreams 与 Locations 生成](u6-l3-servers-upstreams-locations.md)。前两讲把流量局限在 **http 上下文**：`server {}` 监听端口、`location {}` 按 path/header 路由、`upstream {}` 是后端池。但 Gateway API 不只有 HTTP——还有 **TCPRoute / UDPRoute / TLSRoute**，它们工作在第四层（传输层），没有「请求行 / 头 / 路径」这些 HTTP 概念。本讲就走进 NGINX 的另一个上下文——**stream 上下文**，看 NGF 如何把这三类 L4 路由翻译成 `stream {}` 里的 server / upstream / map。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 NGINX 的 **stream 上下文** 与 http 上下文的本质区别，以及 NGF 把 L4 配置统一落在哪一个生成文件里。
- 追踪一条 **TCPRoute / UDPRoute** 从 `dataplane.Configuration` 到 `stream server {}` + `stream upstream {}` 的完整生成路径，包括「多后端加权」时为什么会出现 `split_clients`。
- 说清 **TLS Passthrough** 与 **TLS Terminate** 两种模式的差异，以及 NGF 如何用 `ssl_preread` + `$ssl_preread_server_name` 这个 map 实现「不解密、只看 SNI」的按主机名路由。
- 能把一个 TCPRoute（端口 81 → coffee:8081）在脑子里映射成最终的 `server { listen 81; proxy_pass ...; }` + `upstream ... {}`。

## 2. 前置知识

- **L4 vs L7**：HTTP/HTTPS 是第七层（应用层）协议，有「方法、路径、头部、状态码」；TCP/UDP 是第四层（传输层），只是一串字节流 / 数据报，没有应用语义。NGINX 的 `stream {}` 模块专门代理这种「裸字节流」，不解析 HTTP。
- **NGINX 上下文**：`main > events > http` 和 `main > events > stream` 是两棵平行的子树。`http {}` 里能用的 `location`、`proxy_http_version` 等**不能**写进 `stream {}`，反之亦然。两边的指令集几乎不重叠。
- **`ssl_preread`**：NGINX stream 模块的一个能力——在 TLS 握手**之前**「偷看」ClientHello 里的 **SNI（Server Name Indication）**字段，拿到客户端想访问的主机名，但**不解密**后续流量。这是实现 TLS Passthrough（原样转发加密流量）按主机名路由的关键。
- **`pass` 指令**：`pass <目标>;` 把当前连接原封不动地交给另一个监听对象（通常是另一个 `listen` 的 unix socket），不经过任何代理改写。配合 map 可以做「按变量值转交连接」。
- 本讲输入仍是 [u5-l4](u5-l4-dataplane-configuration.md) 讲过的 `dataplane.Configuration`，输出仍是 [u6-l1](u6-l1-config-generator-overview.md) 讲过的 `[]agent.File`。区别只在于：这次我们关注的是 `Configuration` 里和 L4 相关的字段（`TCPServers` / `UDPServers` / `TLSServers` / `StreamUpstreams`），以及生成器里的三个 stream 执行函数。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/controller/nginx/conf/nginx.conf` | 静态骨架，定义 `stream {}` 上下文并用 `include /etc/nginx/stream-conf.d/*.conf` 拉入生成的 stream 配置 |
| `internal/controller/nginx/config/generator.go` | 总装：`getExecuteFuncs` 把三个 stream 执行函数（servers / upstreams / maps）挂进模板执行链 |
| `internal/controller/nginx/config/stream_servers.go` | 把 `TLSServers/TCPServers/UDPServers` 翻译成 `[]stream.Server`，是本讲最核心的文件 |
| `internal/controller/nginx/config/stream_servers_template.go` | stream server 块的 Go `text/template` 文本 |
| `internal/controller/nginx/config/sockets.go` | socket 命名与 `$dest<port>` 变量名的生成工具函数 |
| `internal/controller/nginx/config/upstreams.go` | `executeStreamUpstreams` / `createStreamUpstream`：把 `StreamUpstreams` 渲染成 `stream upstream {}` 块 |
| `internal/controller/nginx/config/upstreams_template.go` | stream upstream 块的模板文本 |
| `internal/controller/nginx/config/maps.go` | `createStreamMaps` / `addTLSServerToStreamMap`：为 TLS 端口生成 `$ssl_preread_server_name → socket` 的路由 map |
| `internal/controller/nginx/config/maps_template.go` | map 块模板（http 与 stream 共用，`hostnames;` 开关在此） |
| `internal/controller/nginx/config/stream/config.go` | `stream.Server` / `stream.Upstream` 等渲染结构体定义 |
| `internal/controller/state/dataplane/configuration.go` | `buildL4Servers` / `buildTLSServers` / `buildStreamUpstreams`：图的 L4 路由如何变成 `dataplane` 层的 `Layer4VirtualServer` 与 `Upstream` |
| `internal/controller/state/dataplane/types.go` | `Layer4VirtualServer` / `Layer4Upstream` 等中间表示类型 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 stream 上下文**（配置落在哪）、**4.2 L4 服务器生成**（TCP/UDP）、**4.3 TLS Passthrough / Terminate**（SNI 路由）。三者的数据流关系如下：

```
dataplane.Configuration
   ├── TCPServers / UDPServers ──► stream_servers.go ─┐
   │        (buildL4Servers 生成)                      │
   ├── TLSServers ───────────────► stream_servers.go ──┼─► stream.conf（streamConfigFile）
   │        (buildTLSServers 生成)                     │      ↑ 三个 executeFunc 按同一 dest 分桶合并
   ├── StreamUpstreams ─────────► upstreams.go ────────┤
   │        (buildStreamUpstreams 生成)                │
   └── TLSServers(再次) ────────► maps.go ─────────────┘
            (createStreamMaps 生成 $ssl_preread 路由 map)

最终：nginx.conf 的 stream {}  ──include──►  /etc/nginx/stream-conf.d/*.conf
```

注意一个关键点：**stream servers、stream upstreams、stream maps 三者的模板产物都写入同一个 `stream.conf`**（即 `streamConfigFile`），这和 [u6-l1](u6-l1-config-generator-overview.md) 讲过的「按 dest 分桶合并」完全一致——只是 http 上下文那个桶叫 `http.conf`，stream 上下文这个桶叫 `stream.conf`。

### 4.1 stream 上下文：L4 配置落在哪里

#### 4.1.1 概念说明

NGINX 的 `nginx.conf` 是一张静态骨架（[u6-l2](u6-l2-nginx-conf-and-templates.md)）。它同时声明了两棵上下文子树：

- `http { include /etc/nginx/conf.d/*.conf; }` —— 所有 HTTP/HTTPS 路由配置落进这里。
- `stream { include /etc/nginx/stream-conf.d/*.conf; }` —— 所有 TCP/UDP/TLS L4 配置落进这里。

NGF 的生成器对应产出两份主文件：`http.conf`（放进 `conf.d`）与 `stream.conf`（放进 `stream-conf.d`）。**L4 路由绝不会出现在 http 上下文，反之亦然**——这不是风格选择，是 NGINX 语法硬约束：`proxy_pass` 在 `http {}` 里要求 `http://` 前缀，在 `stream {}` 里则是裸 upstream 名；`ssl_preread`、`pass` 这些指令只存在于 stream 模块。

理解这一点后，本讲剩下两部分（TCP/UDP、TLS）本质上都是在回答一个问题：**「这串字节流该交给哪个后端？」**——只是 TCP/UDP 靠「端口」判别，TLS 还能多一个维度「SNI 主机名」。

#### 4.1.2 核心流程

L4 配置从图到文件的流程：

1. `BuildConfiguration` 把图里的 L4 路由加工成四个 `dataplane` 字段：`TCPServers`、`UDPServers`、`TLSServers`、`StreamUpstreams`。
2. 生成器 `getExecuteFuncs` 注册三个 stream 执行函数，它们都把产物写到同一个 `streamConfigFile`：
   - `executeStreamServers`（server 块）
   - `executeStreamUpstreams`（upstream 块）
   - `executeStreamMaps`（map 块，仅 TLS 端口需要）
3. 静态 `nginx.conf` 的 `stream {}` 用通配 `include /etc/nginx/stream-conf.d/*.conf` 把 `stream.conf` 拉进来生效。

#### 4.1.3 源码精读

先看骨架。`nginx.conf` 的 stream 上下文就是这几行：

[internal/controller/nginx/conf/nginx.conf:43-54](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L43-L54) —— 定义 `stream {}` 上下文，配 `stream-main` 日志格式，最后用 `include /etc/nginx/stream-conf.d/*.conf` 拉入生成的 stream 配置。

再看生成器如何把三件套挂上：

[internal/controller/nginx/config/generator.go:234-236](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L234-L236) —— `getExecuteFuncs` 返回的切片里，`g.executeStreamServers`、`g.executeStreamUpstreams`、`executeStreamMaps` 三个函数紧挨在一起。它们各自的 `executeResult.dest` 都是 `streamConfigFile`，所以会被 `executeConfigTemplates` 按 dest 分桶合并进同一份 `stream.conf`（分桶合并机制见 [u6-l1](u6-l1-config-generator-overview.md)）。

`executeStreamServers` 是入口：

[internal/controller/nginx/config/stream_servers.go:19-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L19-L40) —— 它组装出一个 `stream.ServerConfig`（含 `Servers`、`SplitClients`、`IPFamily`、`Plus`、`DNSResolver`、`GatewaySecretID`），渲染 `streamServersTemplate`，产物写到 `streamConfigFile`。注意它**同时**调 `createStreamServers`（出 server 列表）和 `createStreamSplitClients`（出加权所需的 split_clients），二者在同一个模板里渲染。

渲染用的结构体定义在这里：

[internal/controller/nginx/config/stream/config.go:8-19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream/config.go#L8-L19) —— `stream.Server` 是单个 stream server 的渲染模型。关键字段：`Listen`（监听串）、`ProxyPass`（代理到哪个 upstream）、`Target`（`pass` 指令的目标，用于 TLS 端口转交连接）、`SSLPreread`（是否开启 SNI 预读）、`IsSocket`（是否监听 unix socket 而非 TCP 端口）、`SSL`（Terminate 模式的证书配置）。

#### 4.1.4 代码实践

**实践目标**：确认「stream 配置只进 stream 上下文，与 http 隔离」这条铁律。

**操作步骤**：

1. 打开 [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf)，找到 `http {` 与 `stream {` 两个上下文块。
2. 对比两者的 `include`：http 拉的是 `conf.d/*.conf`，stream 拉的是 `stream-conf.d/*.conf`，两个目录互不重叠。
3. 在 `internal/controller/nginx/config/` 下全局搜索 `streamConfigFile` 的定义与赋值点，确认三个 stream 执行函数的 `dest` 全部指向它，没有任何 stream 产物写到 `httpConfigFile`。

**需要观察的现象**：所有 L4 相关的 `executeResult.dest` 都等于 `streamConfigFile`；所有 HTTP 相关的都等于 `httpConfigFile`；两类永不交叉。

**预期结果**：你会确认「上下文隔离」是靠「dest 常量分桶」在生成器层强制保证的——这正好呼应 [u6-l1](u6-l1-config-generator-overview.md) 讲过的「一个 dest 常量 = 一个桶」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ssl_preread on;` 不能写在 `http {}` 上下文里？

**参考答案**：`ssl_preread` 属于 stream 模块（`ngx_stream_ssl_preread_module`），它的指令只在 `stream {}` 上下文合法。http 上下文走的是 `ngx_http_ssl_module`，根本没有「握手前预读 SNI」这个能力——http 模块拿到 TLS 流量时已经在做终止（terminate）了。

**练习 2**：假如未来 NGF 要新增一种 L4 路由（比如 SCTP），生成器层需要改 `executeConfigTemplates` 的合并逻辑吗？

**参考答案**：不需要。只要新执行函数的 `executeResult.dest` 仍是 `streamConfigFile`，分桶合并逻辑会自动把它并入 `stream.conf`。新增落点才需要加新的 dest 常量。

### 4.2 L4 服务器生成：TCP/UDP 的 stream server 与 upstream

#### 4.2.1 概念说明

TCPRoute / UDPRoute 是最简单的 L4 路由：**「这个端口收到的字节流，原样转发到这个后端服务」**。它没有路径、没有主机名匹配（L4 看不见这些），唯一能用来区分流量的是 **端口** 和 **协议**（TCP 还是 UDP）。

NGF 把一条 TCPRoute 翻译成两个 NGINX 对象：

- 一个 `stream server {}`：`listen <端口>; proxy_pass <upstream名>;` —— 决定「在哪收、转给谁」。
- 一个 `stream upstream {}`：`upstream <名字> { server <ip>:<port>; ... }` —— 后端服务器的命名池。

需要注意两个工程细节：

1. **端口去重**：同一 `(端口, 协议)` 只能有一个 `listen`。如果多个路由指向同一端口（理论上 Gateway API 不允许同端口多 TCPRoute 冲突，但代码仍要防御），生成器用 `portProtoKey` 去重。
2. **多后端加权**：一条路由如果有多个 `backendRefs` 且带 `weight`，NGINX 的 `proxy_pass` 只能指向**一个** upstream，所以要用 `split_clients` 按权重把连接哈希到不同 upstream 变量上（和 [u6-l3](u6-l3-servers-upstreams-locations.md) HTTP 侧的加权思路一致）。

#### 4.2.2 核心流程

TCP/UDP 从图到配置的流程：

1. `buildL4Servers(gateway, TCP/UDP)`：遍历对应协议的 Listener 上的 L4Routes，把每个合法 `backendRef` 收集成 `Layer4Upstream{Name, Weight}`，组装成 `Layer4VirtualServer{Port, Upstreams}`。结果进 `conf.TCPServers` / `conf.UDPServers`。
2. `buildStreamUpstreams(...)`：把 L4 路由引用的每个 Service+Port 解析成端点，组装成 `dataplane.Upstream`（去重），结果进 `conf.StreamUpstreams`。
3. 生成器侧：
   - `executeStreamUpstreams` 把 `StreamUpstreams` 渲染成 `upstream {}` 块（带 `random two least_conn` LB、zone、Plus 的 state 文件）。
   - `executeStreamServers` → `processLayer4Servers` 把 `TCPServers/UDPServers` 渲染成 `server { listen <port>[ udp]; proxy_pass ...; }`。UDP 在 listen 后追加 ` udp`，加权时 proxy_pass 变成 `$backend_<port>` 并配套 `split_clients`。

#### 4.2.3 源码精读

先看图的 L4 服务器构建。`buildL4Servers` 按协议过滤 Listener、收集 backendRefs：

[internal/controller/state/dataplane/configuration.go:315-369](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L315-L369) —— 遍历 `gateway.Listeners`，只处理 `Protocol` 等于传入协议（TCP 或 UDP）且 `Valid` 的 Listener；对每条 L4Route 取 `GetBackendRefs()`，逐个造 `Layer4Upstream{Name: br.ServicePortReference(), Weight: br.Weight}`，组装成 `Layer4VirtualServer{Port: l.Source.Port, Upstreams: ...}`。注意 `Hostname: ""`——L4 不用主机名，注释也写明 `// Layer4 doesn't use hostnames`。

`BuildConfiguration` 在装配 `Configuration` 字面量时同时调 TCP 与 UDP 两遍：

[internal/controller/state/dataplane/configuration.go:144-153](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L144-L153) —— `TCPServers: buildL4Servers(logger, gateway, v1.TCPProtocolType)`、`UDPServers: buildL4Servers(logger, gateway, v1.UDPProtocolType)`、`StreamUpstreams: buildStreamUpstreams(...)` 三者并列装配。stream upstream 与 http upstream 是分开的两个字段（`Upstreams` vs `StreamUpstreams`），因为两者渲染模板不同。

stream upstream 的端点解析与 http 共用一套逻辑：

[internal/controller/state/dataplane/configuration.go:397-440](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L397-L440) —— `buildStreamUpstreams` 只认 `supportedProtocols`（TLS/TCP/UDP），对每条 L4Route 的每个 backendRef 调 `resolveUpstreamEndpoints` 取端点，用 `uniqueUpstreams` map 按 upstream 名去重。

再看生成器侧。`processLayer4Servers` 是 TCP/UDP server 生成的核心：

[internal/controller/nginx/config/stream_servers.go:109-178](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L109-L178) —— 关键逻辑：
- `protocolSuffix`：UDP 时为 `" udp"`，TCP 为空——这决定了 listen 串是 `listen 82;`（TCP）还是 `listen 53 udp;`（UDP）。
- `portProtoKey` 去重：同一 `(port, protocol)` 只生成一个 server，重复的直接 `continue`。
- **单后端**（`len(server.Upstreams) == 1`）：`proxy_pass = upstreamName`，但前提是该 upstream 在 `upstreams` map 里存在且有端点，否则记日志跳过（不生成 server）。
- **多后端**（加权）：`proxy_pass = "$backend_%d"`（如 `$backend_81`），并要求至少一个 upstream 有端点，否则跳过。真正的加权分流量由 `split_clients` 完成。
- 最终组装 `stream.Server{Listen: "<port>[ udp]", StatusZone: "<proto>_<port>", ProxyPass: ...}`。`StatusZone` 仅在 Plus 下渲染（见模板）。

加权所需的 split_clients 在这里生成：

[internal/controller/nginx/config/stream_servers.go:221-263](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L221-L263) —— `createSplitClientForL4Server` 计算总权重，除最后一个 upstream 外按 `percentOf(weight, total)` 向下取整分配百分比，最后一个拿剩余百分比（保证总和恰为 100），变量名固定为 `backend_<port>`，与上面 `proxy_pass $backend_<port>` 对应。

stream upstream 块的渲染（注意它和 http upstream 是**不同的模板**）：

[internal/controller/nginx/config/upstreams.go:74-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L74-L95) —— `executeStreamUpstreams` 调 `createStreamUpstreams` 把 `dataplane.Upstream` 翻译成 `stream.Upstream`，只保留**有端点**的 upstream。

stream upstream 的模板文本（对比 [u6-l3](u6-l3-servers-upstreams-locations.md) 的 http upstream 模板）：

[internal/controller/nginx/config/upstreams_template.go:49-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams_template.go#L49-L67) —— 模板固定输出 `random two least_conn;`（stream 侧没有 keepalive 指令，因为 L4 长连接语义不同）；Plus 下输出 `zone <name> <size>;` 与 `state <file>;`（无 resolve server 时），否则枚举 `server <addr>[:weight][ resolve];`。

stream server 块的模板（TCP/UDP 走的就是其中 `proxy_pass` 那段）：

[internal/controller/nginx/config/stream_servers_template.go:29-89](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers_template.go#L29-L89) —— `listen` 行根据 IP 家族与是否 socket 分别渲染 IPv4/IPv6；`proxy_pass` 段在 `$s.ProxyPass` 非空时输出；`status_zone` 受 `$.Plus` 守卫。TCP/UDP server 不带 `ssl` / `pass` / `ssl_preread`（这些字段为空）。

#### 4.2.4 代码实践

**实践目标**：为一个 TCPRoute 追踪它最终生成的 stream server 与 upstream 配置（本讲主任务）。

**操作步骤**：

1. 打开示例 [examples/tcp-routing/gateway.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/tcp-routing/gateway.yaml) 与 [examples/tcp-routing/tcp-routes.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/tcp-routing/tcp-routes.yaml)。
2. 锁定 `tcp-coffee` 这条路由：`parentRefs.sectionName: coffee`（对应 listener `coffee`，`protocol: TCP, port: 81`），`backendRefs: coffee:8081`。
3. 按 4.2.2 的流程在源码里走一遍：
   - `buildL4Servers` 会为 listener `coffee` 生成 `Layer4VirtualServer{Port: 81, Upstreams: [{Name: "<coffee 的 ServicePortReference>", Weight: 0}]}`，进 `conf.TCPServers`。
   - `buildStreamUpstreams` 会为 `coffee:8081` 解析端点，生成一个 `dataplane.Upstream`，进 `conf.StreamUpstreams`。
   - `processLayer4Servers`（[stream_servers.go:156-167](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L156-L167)）因只有 1 个 upstream，走单后端分支：`proxy_pass = "<coffee upstream 名>"`，组装出 `stream.Server{Listen: "81", ProxyPass: "<名>", StatusZone: "TCP_81"}`。
4. 根据模板推断最终 `stream.conf` 里应有形如下面的内容（**示例代码**，具体 upstream 名取决于 `ServicePortReference()` 的拼法，待本地验证）：

   ```nginx
   # 示例代码：依据源码逻辑推断，非仓库原文
   upstream <coffee_upstream_name> {
       random two least_conn;
       zone <coffee_upstream_name> 512k;   # Plus 下为 1m
       server 10.x.x.x:8081;
   }

   server {
       listen 81;                          # IPv6 时另有 listen [::]:81;
       status_zone TCP_81;                 # 仅 NGINX Plus
       proxy_pass <coffee_upstream_name>;
   }
   ```

**需要观察的现象**：端口 81 的 listen、`status_zone` 命名为 `TCP_81`、proxy_pass 直指 upstream 名、upstream 用 `random two least_conn` 而非 round-robin。

**预期结果**：你能用源码逻辑讲清「`port: 81` + `coffee:8081` → `listen 81` + `proxy_pass <upstream>` + `upstream { server <ep>:8081 }`」的完整对应关系。运行集群实际查看 `stream.conf` 属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：UDP server 与 TCP server 在生成的 `listen` 行上有什么差别？是哪行代码造成的？

**参考答案**：UDP 的 listen 多一个 ` udp` 后缀（如 `listen 53 udp;`），TCP 没有（`listen 81;`）。由 [stream_servers.go:117-120](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L117-L120) 的 `protocolSuffix` 决定，并在 [stream_servers.go:171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L171) 的 `Listen: fmt.Sprintf("%d%s", server.Port, protocolSuffix)` 拼进 listen 串。

**练习 2**：一条 TCPRoute 配了两个 `backendRefs` 且各自 `weight`，最终 proxy_pass 指向什么？为什么不能直接写两个 upstream 名？

**参考答案**：proxy_pass 变成 `$backend_<port>`（如 `$backend_81`），由 [stream_servers.go:138-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L138-L139) 决定。因为 NGINX 的 `proxy_pass` 只能指向**一个**目标（一个 upstream 名或一个变量），无法同时指向多个；所以用 `split_clients $connection $backend_<port> {...}` 按连接哈希、按权重百分比把流量分到不同 upstream 名，再把该变量作为 proxy_pass 目标。

**练习 3**：如果一个 TCP 引用的 Service 还没有任何就绪端点，`processLayer4Servers` 会生成 server 吗？

**参考答案**：不会。单后端分支检查 `len(u.Endpoints) > 0`，为空则记日志 `upstream not found or no endpoints` 并 `continue`，该端口不生成 server（[stream_servers.go:159-167](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L159-L167)）。多后端分支同理要求至少一个有端点。

### 4.3 TLS Passthrough 与 Terminate：用 SNI 路由加密流量

#### 4.3.1 概念说明

TLSRoute 处理的是 **TLS 加密流量**。Gateway API 的 TLS Listener 有两种模式（由 `tls.mode` 字段决定）：

- **Passthrough（透传）**：NGF **不**解密 TLS，把整段加密流量原样转给后端，由后端自己终止 TLS。好处是端到端加密、密钥不出后端。
- **Terminate（终止）**：NGF 在数据面**终止** TLS（用监听器配置的证书完成握手），解密后再以明文（或重新加密）转给后端。

Passthrough 的难点在于：**要在不解密的前提下，按主机名把流量路由到不同后端**。NGINX 的解法是 `ssl_preread`——在握手前偷看 ClientHello 里的 **SNI** 字段（客户端在明文阶段就会告诉服务器它想访问哪个主机名），据此选择后端，然后把**原始加密字节流**继续转交。整个过程 NGINX 始终看不到明文。

NGF 实现这一机制依赖四个关键 NGINX 能力：

| NGINX 能力 | 作用 |
| --- | --- |
| `ssl_preread on;` | 在端口级 server 上开启 SNI 预读，把 SNI 填进 `$ssl_preread_server_name` 变量 |
| `map $ssl_preread_server_name $dest<port> {...}` | 「主机名 → 目标 socket」的路由表，用 `hostnames;` 支持通配匹配 |
| `pass $dest<port>;` | 把整条连接（仍加密）原样转交给 map 选中的 socket |
| socket server 上的 `proxy_pass` / `ssl` | 真正的出口：Passthrough 直接 `proxy_pass` 加密流；Terminate 用 `ssl` 终止后再 `proxy_pass` |

换句话说，TLS 端口上有一个**「只看不摸」的端口级 server**（`ssl_preread` + `pass`），和若干**「真正干活」的 socket server**（每个主机名一个，Passthrough 或 Terminate）。map 是连接两者的路由表。

#### 4.3.2 核心流程

TLS 从图到配置的流程（与 TCP/UDP 多了一层「SNI 路由 map」）：

1. `buildTLSServers(gateway)`：遍历 TLS 协议的 Listener，每条 TLSRoute 的每个被接受主机名生成一个 `Layer4VirtualServer`；Passthrough 时 `SSL=nil`，Terminate 时 `SSL` 非空（含证书 KeyPairIDs）。结果进 `conf.TLSServers`。
2. `executeStreamMaps` → `createStreamMaps`：为每个 TLS 端口生成一个 `map $ssl_preread_server_name $dest<port> { hostnames; <主机名> <socket>; ... default <socket>; }`，把主机名映射到对应 socket。
3. `executeStreamServers` → `createStreamServers` 的 TLS 分支：
   - 对每个带主机名的 TLS server：生成一个 **socket server**——Passthrough 是 `listen <socket>; proxy_pass <upstream>;`；Terminate 是 `listen <socket>; ssl_certificate ...; proxy_pass <upstream>; proxy_ssl...;`。
   - 对每个 TLS 端口（去重后）：生成一个**端口级 server**——`listen <port>; ssl_preread on; pass $dest<port>;`。

socket 名与 `$dest` 变量名由三个工具函数生成：

- `getSocketNameTLS(port, hostname)`：Passthrough socket，如 `unix:/var/run/nginx/<hostname>-<port>.sock`。
- `getSocketNameTLSTerminate(port, hostname)`：Terminate socket，多了 `-terminate` 后缀。
- `getTLSPassthroughVarName(port)`：路由 map 的输出变量 `$dest<port>`。

#### 4.3.3 源码精读

先看 socket 命名工具：

[internal/controller/nginx/config/sockets.go:7-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/sockets.go#L7-L31) —— `SocketBasePath = "unix:/var/run/nginx/"`；`getSocketNameTLS` 拼出 `<base><hostname>-<port>.sock`（无 hostname 时为 `<port>.sock`）；`getSocketNameTLSTerminate` 多一个 `-terminate`；`getTLSPassthroughVarName` 产出 `$dest<port>`。这三种命名是「端口级 server → map → socket server」三方对接的契约。

再看图侧 `buildTLSServers`：

[internal/controller/state/dataplane/configuration.go:182-243](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L182-L243) —— 遍历 TLS Listener，用 `isTLSTerminateListener` 判断模式：Terminate 则 `ssl = buildSSL(l)`（带证书），Passthrough 则 `ssl` 保持 `nil`。每条 TLSRoute 的每个被接受主机名造一个 `Layer4VirtualServer{Hostname: <主机名>, Upstreams: [{Name: <backendRef>}], Port, SSL}`；若该 listener 没有任何路由匹配其 hostname，则造一个 `IsDefault` 的兜底 server。最后按 port / IsDefault / hostname 排序保证输出稳定。

模式判定函数：

[internal/controller/state/dataplane/configuration.go:174-177](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L174-L177) —— `isTLSTerminateListener`：`TLS` 非空且 mode 为空或为 `Terminate` 即终止模式；否则（`Passthrough`）`SSL` 留空，下游据此走 Passthrough 分支。

然后是生成器侧的 TLS 分支（`createStreamServers` 里 `conf.TLSServers` 那段）：

[internal/controller/nginx/config/stream_servers.go:62-99](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L62-L99) —— 对每个 TLSServer：
- 若 `server.SSL != nil`（Terminate）：调 `createTLSTerminateSocketServer` 生成一个带 ssl 证书的 socket server。
- 否则若有 upstream 且 hostname 非空（Passthrough）：直接造 socket server `Listen: getSocketNameTLS(port, hostname), ProxyPass: upstreamName, IsSocket: true`（[stream_servers.go:67-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L67-L81)），注意它**不**带 `ssl`——流量仍是加密的，只是 `proxy_pass` 把字节流转给后端。
- 随后（与上面 socket 无关地）按端口去重，为该端口生成**端口级 server**：`Listen: <port>, Target: getTLSPassthroughVarName(port)` 即 `$dest<port>`，`SSLPreread: true`（[stream_servers.go:91-98](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L91-L98)）。这就是「只看不摸」的入口。

Terminate 模式的 socket server 构造：

[internal/controller/nginx/config/stream_servers.go:267-314](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L267-L314) —— `createTLSTerminateSocketServer`：若是 `IsDefault` 且无 hostname，返回一个 `ssl_reject_handshake on` 的兜底 socket（拒绝不匹配的握手）；否则造 `Listen: getSocketNameTLSTerminate(port, hostname), SSL: buildStreamSSL(server.SSL), ProxyPass: upstreamName, ProxySSLVerify: ...`。即先 `ssl` 终止、再 `proxy_pass` 解密后的流量（如需对后端再加密，由 `ProxySSLVerify` 配 `proxy_ssl`）。

接着看路由 map 的生成。`createStreamMaps` 为每个 TLS 端口建一张 `$ssl_preread_server_name → socket` 表：

[internal/controller/nginx/config/maps.go:141-189](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L141-L189) —— 只有 `len(conf.TLSServers) > 0` 才生成 map（TCP/UDP 不需要）。它还顺手把 HTTPS（`SSLServers`）的同端口主机名也并进同一张表——这是因为 443 端口可能同时承载 TLSRoute（L4）和 HTTPS（L7），两者共用同一个 ssl_preread 入口，按 SNI 分流到各自的 socket（L4 走 TLS socket，L7 走 `getSocketNameHTTPS`）。无 default 时补一条 `default connectionClosedStreamServerSocket`。

把单个 TLS server 写进 map 的逻辑：

[internal/controller/nginx/config/maps.go:193-234](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L193-L234) —— `addTLSServerToStreamMap`：首次遇到某端口时建一张 `Source: "$ssl_preread_server_name", Variable: "$dest<port>", UseHostnames: true` 的 map；对每个有 hostname 的 server 追加 `<hostname> <socket>` 参数；`default` 参数只允许「无 hostname 的 Terminate 默认 server」认领，避免重复。

socket 到底是 Passthrough 还是 Terminate，由 `resolveTLSServerSocket` 决定：

[internal/controller/nginx/config/maps.go:237-259](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L237-L259) —— `server.SSL != nil` → `getSocketNameTLSTerminate`（终止 socket）；否则 → `getSocketNameTLS`（透传 socket）；既非默认又无有效 upstream → `emptyStringSocket`（`""`，让 NGINX 报 500 关闭连接）。

map 模板（注意 `hostnames;` 开关）：

[internal/controller/nginx/config/maps_template.go:3-14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps_template.go#L3-L14) —— `UseHostnames` 为真时输出 `hostnames;`，让 map 支持通配主机名（如 `*.example.com`）匹配，这正是 TLS 路由需要的。

最后看模板如何把上面三件事（端口级 server 的 `pass`、socket server 的 `proxy_pass`/`ssl`、`ssl_preread`）渲染出来：

[internal/controller/nginx/config/stream_servers_template.go:82-87](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers_template.go#L82-L87) —— `$s.Target` 非空时输出 `pass <target>;`（即端口级 server 的 `pass $dest<port>;`）。
[internal/controller/nginx/config/stream_servers_template.go:67-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers_template.go#L67-L81) —— `$s.ProxyPass` 非空时输出 `proxy_pass`，Terminate 还附带 `proxy_ssl` 校验链。
[internal/controller/nginx/config/stream_servers_template.go:85-87](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers_template.go#L85-L87) —— `$s.SSLPreread` 为真时输出 `ssl_preread on;`。

> 把 4.3 的三段串起来，一次 TLS Passthrough 请求的完整路径是：客户端连 8443 端口 → 端口级 server `ssl_preread on` 读出 SNI=`foo.example.com` → map `$dest8443` 把它解析成 `unix:/var/run/nginx/foo.example.com-8443.sock` → `pass` 把整条加密连接转给该 socket → socket server `proxy_pass <upstream>` 把加密流送到后端 → 后端自己终止 TLS。NGINX 全程不解密。

#### 4.3.4 代码实践

**实践目标**：动手验证「端口级 server + map + socket server」三件套如何协同，并区分 Passthrough 与 Terminate。

**操作步骤**：

1. 在源码里锁定三个对接契约：`getTLSPassthroughVarName(port)` 产出的 `$dest<port>`、端口级 server 的 `Target` 字段、map 的 `Variable` 字段。确认三者用的是**同一个**变量名（[stream_servers.go:95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L95) 与 [maps.go:206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L206)）。
2. 选一个 Passthrough 场景：假设 listener `tls-passthrough` 端口 8443、hostname `foo.example.com`、mode Passthrough、backendRef `app:8443`。按 4.3.2 流程推断生成的 `stream.conf` 应包含三块（**示例代码**，待本地验证）：

   ```nginx
   # 示例代码：依据源码逻辑推断
   # 1) 路由 map
   map $ssl_preread_server_name $dest8443 {
       hostnames;
       foo.example.com unix:/var/run/nginx/foo.example.com-8443.sock;
       default unix:/var/run/nginx/connection-closed-server.sock;
   }
   # 2) 端口级 server：只看不摸
   server {
       listen 8443;
       status_zone foo.example.com;          # 仅 Plus
       ssl_preread on;
       pass $dest8443;
   }
   # 3) socket server：透传加密流
   server {
       listen unix:/var/run/nginx/foo.example.com-8443.sock;
       proxy_pass <app_upstream_name>;
   }
   ```

3. 把 mode 改成 `Terminate`，重走一遍：socket 名应变成 `...-8443-terminate.sock`，socket server 应多出 `ssl_certificate /etc/nginx/secrets/<keypair>.pem;`（由 [stream_servers.go:301-308](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L301-L308) 的 `buildStreamSSL` 决定）。

**需要观察的现象**：Passthrough 的 socket server **没有**任何 `ssl_*` 指令（因为不解密），只有 `proxy_pass`；Terminate 的 socket server **有** `ssl_certificate`。两者共用同一个端口级 `ssl_preread` server 与同一张 map。

**预期结果**：你能说清「为什么 NGINX 能在不解密的前提下按主机名路由」——因为 SNI 在握手前的明文阶段就暴露在 `$ssl_preread_server_name` 里了。运行集群验证属「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：端口级 TLS server 的 `pass $dest8443;`、map 的输出变量 `$dest8443`、socket server 的 listen 地址，三者是怎么对上号的？

**参考答案**：三者由同一个工具函数 `getTLSPassthroughVarName(8443)` 统一产出 `$dest8443`。端口级 server 的 `Target` 用它做 `pass` 目标（[stream_servers.go:95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L95)）；map 的 `Variable` 用它做输出变量名（[maps.go:206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L206)）；map 参数的 `Result` 则是各主机名对应的 socket 地址，由 `resolveTLSServerSocket` 按模式产出。`pass` 读取 `$dest8443` 的当前值（即 map 选中的 socket），把连接转过去。

**练习 2**：Passthrough 和 Terminate 的 socket server，最大的区别在哪几条指令上？

**参考答案**：Passthrough socket 只有 `proxy_pass <upstream>`（转发加密流）；Terminate socket 额外有 `ssl_certificate`/`ssl_certificate_key`（终止 TLS），并可能带 `proxy_ssl` 系列指令（对后端再加密并校验）。模式判定源头是图侧 `isTLSTerminateListener`（`SSL` 是否为 nil），渲染分流在 [stream_servers.go:62-82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L62-L82) 与 `createTLSTerminateSocketServer`。

**练习 3**：如果一个 TLS 端口上没有任何匹配的主机名，请求会怎样？

**参考答案**：map 会命中 `default` 分支。若该端口有「无 hostname 的 Terminate 默认 server」，default 指向它的 `ssl_reject_handshake` socket（拒绝握手）；否则指向 `connectionClosedStreamServerSocket`（[maps.go:179-184](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L179-L184)），该 socket（模板 [stream_servers_template.go:91-94](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers_template.go#L91-L94)）`return "";` 告知「在监听但无服务」。

## 5. 综合实践

把本讲三个模块串起来，完成一个「混合端口」追踪任务。

**场景**：一个 Gateway 同时有：listener A（`protocol: TCP, port: 9000`，挂一条 TCPRoute 指向 `svc-a:5000`）、listener B（`protocol: TLS, port: 8443, tls.mode: Passthrough`，hostname `a.example.com`，挂一条 TLSRoute 指向 `svc-b:443`）。

**任务**：

1. 画出这两个端口各自会生成哪些 stream 配置块（server / upstream / map），分别属于 4.1/4.2/4.3 哪个模块的产物。
2. 指出 9000 端口的配置**没有** map、**没有** `ssl_preread`，而 8443 端口**三者都有**，并用源码说明为什么（提示：`createStreamMaps` 的 [maps.go:142-144](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L142-L144) 守卫，以及 `createStreamServers` 里 TCP/UDP 与 TLS 的分支差异）。
3. 写出两个端口最终的 listen / proxy_pass / pass / ssl_preread 指令（**示例代码**，upstream 名待本地验证），并解释为什么 TCP 端口的 server 直接 `proxy_pass`，而 TLS 端口要先 `ssl_preread` 再 `pass`。

**验收标准**：你能不看讲义，说清「TCP 靠端口一刀切、TLS 靠 SNI 精细分流」这条主线，以及 stream 上下文与 http 上下文在配置生成上为何是两套独立的桶。

## 6. 本讲小结

- NGINX 的 **stream 上下文**与 http 上下文平行、指令集不重叠；NGF 的 L4 配置统一落在 `streamConfigFile`（`stream.conf`），由静态 `nginx.conf` 的 `stream {}` 通过 `include /etc/nginx/stream-conf.d/*.conf` 拉入。
- 生成器对 stream 注册了**三个**执行函数——`executeStreamServers`、`executeStreamUpstreams`、`executeStreamMaps`——它们的 `dest` 都是 `streamConfigFile`，按 [u6-l1](u6-l1-config-generator-overview.md) 的分桶合并机制拼成一份 `stream.conf`。
- **TCP/UDP**（4.2）：一条路由翻译成一个 `stream server { listen <port>[ udp]; proxy_pass <upstream>; }` 加一个 `stream upstream { random two least_conn; zone ...; server <ep>; }`；多后端加权时 proxy_pass 退化为 `$backend_<port>` 并配套 `split_clients`；`(port, protocol)` 用 `portProtoKey` 去重。
- **TLS Passthrough/Terminate**（4.3）：每个 TLS 端口有一个「只看不摸」的端口级 server（`ssl_preread on; pass $dest<port>;`）加一张 `$ssl_preread_server_name → socket` 路由 map；每个主机名对应一个 socket server——Passthrough 直接 `proxy_pass` 加密流，Terminate 先 `ssl_certificate` 终止再 `proxy_pass`。模式由图侧 `isTLSTerminateListener` 决定。
- **三组对接契约**贯穿 TLS：`getTLSPassthroughVarName(port)` → `$dest<port>`（端口级 `pass` 目标 = map 输出变量）；`getSocketNameTLS` / `getSocketNameTLSTerminate` → socket 地址（map 参数的 Result = socket server 的 listen）；map 的 `hostnames;` 让通配主机名生效。
- L4 与 L7 在 8443 这类端口上还能**共存**：`createStreamMaps` 会把 HTTPS（`SSLServers`）的主机名并进同一张 ssl_preread map，按 SNI 分流到 L4 的 TLS socket 或 L7 的 HTTPS socket。

## 7. 下一步学习建议

- 本讲止于「配置生成出 `stream.conf`」。这些文件如何被打包、经 NGINX Agent 下发到数据面 Pod、如何触发 reload，请进入 **[u7-l1 NginxUpdater 与 gRPC Agent 通信](u7-l1-nginx-agent-updater-and-grpc.md)**。
- TLS 这条线还涉及后端 mTLS（BackendTLSPolicy），即 Terminate 之后「对后端再加密」的 `proxy_ssl` 校验链从何而来，建议阅读 **[u12-l3 TLS 安全：前端证书、后端 TLS 与 Agent mTLS](u12-l3-tls-and-certificates.md)**。
- 想看 stream 上下文更多边界行为（如 ExternalName 服务的 DNS resolver、proxy_protocol 改写客户端 IP），可对照 `stream_servers_template.go` 顶部 `DNSResolver` 与 `RewriteClientIP` 两段模板，并回看 [u5-l4](u5-l4-dataplane-configuration.md) 里 `BaseStreamConfig` 与 `RewriteClientIPSettings` 的构建。
- 若要新增一种 stream 行为，参照本讲三个 executeFunc 的「dest 都指向 `streamConfigFile`」约定新增一个执行函数即可，无需改动合并逻辑——这是 [u6-l1](u6-l1-config-generator-overview.md) 设计红利的直接体现。
