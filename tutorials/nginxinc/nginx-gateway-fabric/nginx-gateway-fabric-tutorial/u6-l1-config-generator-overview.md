# 配置生成器总览：从 Configuration 到 []File

## 1. 本讲目标

上一讲（u5-l4）我们把领域模型 `graph.Graph` 翻译成了面向 NGINX 的中间表示 `dataplane.Configuration`。这一层结构是「按 K8s 资源语义」切分的（Servers、Upstreams、SSLKeyPairs……）。但 NGINX 实际能读的只有一坨 `nginx.conf` 文本文件。本讲就负责打通这「最后一公里」。

学完本讲，你应该能够：

- 说清 `Generator.Generate` 的整体流程：它把一份 `dataplane.Configuration` 拆成几大类产物（配置文本文件、密钥文件、bundle 文件），分别落到哪些目录。
- 理解 `executeConfigTemplates` 的「多步模板执行链」：12 个 `executeFunc` 各自渲染一段文本，再按「目标文件」合并成最终的 `http.conf` / `stream.conf` 等少数几个大文件。
- 解释生成出来的文件如何被组织成 `conf.d/`、`stream-conf.d/`、`main-includes/`、`secrets/` 等目录，并被静态的 `nginx.conf` 通过 `include` 串起来。
- 在源码里定位「servers、upstreams、maps 各自由哪个函数生成」。

本讲只讲「总装与目录组织」，不讲每个 server/upstream 块内部的指令细节——那是 u6-l2（nginx.conf 骨架）、u6-l3（servers/upstreams/locations）、u6-l4（stream）的主题。

## 2. 前置知识

阅读本讲前，最好已经建立以下认知（来自前置讲义）：

- **`dataplane.Configuration` 是什么**：它是「渲染模型」，已经与 Gateway API 解耦，按 NGINX 配置块而非 K8s 资源类型切分（u5-l4）。本讲就是它的消费者。
- **`agent.File` 是什么**：控制面下发给数据面 NGINX Agent 的文件单元，包含 `Meta`（文件名、hash、权限、大小）和 `Contents`（字节内容），经 gRPC 传输（u7）。
- **NGINX 的 `include` 机制**：NGINX 允许用 `include /path/*.conf;` 把一段配置从外部文件拉进某个上下文（main / events / http / stream / server / location）。理解这点对看懂「目录组织」至关重要。
- **NGINX 的上下文（context）**：`main`（顶层）、`events {}`、`http {}`、`stream {}`、`server {}`、`location {}`。不同指令只能出现在特定上下文，所以配置文件必须按上下文归类。
- **Go `text/template`**：NGF 用标准库 `text/template` 把结构体渲染成配置文本，模板字符串集中放在 `*_template.go` 文件里（如 `main_config_template.go`）。本讲只关注「谁在调用模板」，不深入模板语法。

一个贯穿全讲的关键直觉：**NGINX 的配置是「若干个文本文件」，而 `dataplane.Configuration` 是「一棵结构化的对象树」**。生成器的工作就是把对象树拍平、切片、按目标文件分桶，最后用模板逐桶渲染成文本。

## 3. 本讲源码地图

本讲聚焦两个文件，并以 `nginx.conf` 作为「靶子」理解目录组织：

| 文件 | 作用 |
| --- | --- |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | 生成器总装：`Generator` 接口、`Generate` 主流程、`executeConfigTemplates`、`getExecuteFuncs`，以及所有目标目录常量与密钥/bundle 文件生成函数。 |
| [internal/controller/nginx/config/includes.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go) | include 文件的去重与转换工具：把策略产物、SnippetsFilter、AuthZ 配置等转成 `shared.Include` 并按文件名去重。 |
| [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf) | 数据面 Pod 里那份**静态**主配置文件，定义了 `include` 各目录的入口。生成器产出的文件就是被它 include 进去的。 |

各 `execute*` 函数散落在同目录的专题文件里（`main_config.go`、`base_http_config.go`、`servers.go`、`upstreams.go`、`maps.go`、`split_clients.go`、`telemetry.go`、`plus_api.go`、`stream_servers.go`），本讲会点到为止。

## 4. 核心概念与源码讲解

### 4.1 Generator 接口

#### 4.1.1 概念说明

「生成器」是控制面里**唯一**把 `dataplane.Configuration` 变成 NGINX 文件的组件。它的输入是「领域无关的渲染模型」，输出是一组 `agent.File`（可立即下发给数据面）。

为什么要把生成器抽象成接口？注释写得很直白——`Generator` 接口「仅用于测试」：

> Generator generates NGINX configuration files. This interface is used for testing purposes only.

真实运行时用的是具体实现 `GeneratorImpl`；接口存在的目的是让上游（`eventHandler` / `NginxUpdater`）在单测里能用 counterfeiter 生成的 fake 替换掉真实生成器，从而隔离「图 → 配置」与「配置 → 文件」两段逻辑。这是一种典型的**依赖倒置**：上游依赖抽象，不依赖具体实现。

> 术语：**counterfeiter** 是 Go 社区的 mock 生成工具，源码顶部的 `//counterfeiter:generate . Generator` 指令会让它为 `Generator` 接口生成一个 `GeneratorFake`（u13-l1 会详讲 fake 体系）。

#### 4.1.2 核心流程

`Generator` 接口只有两个方法：

1. `Generate(configuration dataplane.Configuration) []agent.File` —— 主方法，把配置渲染成一组文件。
2. `GenerateDeploymentContext(depCtx dataplane.DeploymentContext) (agent.File, error)` —— 生成 NGINX Plus 授权（licensing）用的 `deployment_ctx.json`。

具体实现 `GeneratorImpl` 是个很轻的结构体，只持有三个「生成期固定、与单次配置无关」的字段：是否 Plus、用量上报配置、logger。**它不缓存任何配置状态**——每次 `Generate` 都是无状态的纯函数式转换，这是它能被反复调用、便于测试的前提。

#### 4.1.3 源码精读

接口定义与实现结构体（注意方法签名与字段）：

[internal/controller/nginx/config/generator.go:79-98](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L79-L98) —— 定义 `Generator` 接口（`Generate` + `GenerateDeploymentContext`）与 `GeneratorImpl` 结构体（`usageReportConfig` / `logger` / `plus` 三字段）。

构造函数把外部配置注入：

[internal/controller/nginx/config/generator.go:100-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L100-L111) —— `NewGeneratorImpl(plus, usageReportConfig, logger)` 在 `StartManager` 装配阶段被调用一次，之后长期复用。

`GenerateDeploymentContext` 体现了「生成器也承担一点 Plus 专属职责」：它把 `DeploymentContext` 序列化成 JSON，落到 `main-includes/deployment_ctx.json`，供 Plus 的 mgmt 块引用：

[internal/controller/nginx/config/generator.go:161-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L161-L180) —— 注意它被 `exported`（大写方法名），因为 init 容器的 `initialize` 命令也要复用它（u2-l3）。

#### 4.1.4 代码实践

**实践目标**：确认「接口仅为测试而存在、生产用具体实现」这条设计断言。

**操作步骤**：

1. 在 `generator.go` 顶部找到 `//counterfeiter:generate . Generator` 指令（[generator.go:27-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L27-L28)）。
2. 用 Grep 在仓库里搜索 `Generator` 接口的消费者：谁把生成器当字段持有？

```
Grep pattern: "Generator\b"  glob: "**/handler*.go"
```

**需要观察的现象**：你会看到 `eventHandler` 之类持有的是接口类型 `config.Generator`，而真正装配处（`manager.go`）调用的是 `NewGeneratorImpl(...)` 返回的具体类型。

**预期结果**：理解「声明侧用接口、构造侧用实现」的依赖倒置。**待本地验证**：在你的 IDE 里对 `Generate` 的调用点做「Go to Implementation」，应只跳到 `GeneratorImpl.Generate` 一处。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GeneratorImpl` 不缓存上一份 `Configuration`？

**参考答案**：因为生成是无状态的纯转换。每次 `Generate` 都从输入完整重建文件集合，既便于并发安全（多副本热备都调它），也便于单测（输入决定输出，无需 setup/teardown）。代价是每次都全量重算，但这正是 u4 讲的「按批合并、整图重建」模式的自然延伸。

**练习 2**：`GenerateDeploymentContext` 为什么是导出方法（大写）？

**参考答案**：它要被**另一个二进制**复用——init 容器里的 `initialize` 命令（u2-l3）在 Plus 场景下也要生成同一份 `deployment_ctx.json`。导出方法让跨包（`cmd/gateway`）调用成为可能。

### 4.2 模板执行链

#### 4.2.1 概念说明

`Generate` 内部其实分成两大类产物，走两条不同的流水线：

1. **密钥 / bundle / 认证文件**（`generatePEM`、`generateCertBundle`、`generateWAFBundle`、`generateAuthFile` 等）：这些是「二进制式」的文件，内容直接来自配置里的字节，不走模板，每个文件单独一个 `agent.File`，路径由 ID 决定。
2. **NGINX 配置文本**（servers、upstreams、maps、split_clients、main/events/base-http/stream……）：这些是「模板渲染式」的，统一交给 `executeConfigTemplates` 处理。

本小节聚焦第 2 类，也就是「模板执行链」。

模板执行链的核心抽象是两个类型：

- `executeResult{dest, data}`：一次模板渲染的产物——「写到哪个文件 `dest`」+「内容字节 `data`」。
- `executeFunc func(configuration dataplane.Configuration) []executeResult`：一个执行单元，吃整份配置、吐出一组 `executeResult`。

**关键洞察**：一次 `executeFunc` 可以往**多个**目标文件写，而**多个** `executeFunc` 也可以往**同一个**目标文件写。生成器并不关心谁写谁，它只负责「按 `dest` 分桶合并」。这个设计让「往 `http.conf` 追加一段」变得极其自然。

#### 4.2.2 核心流程

`Generate` 的主流程用伪代码表示：

```
Generate(conf):
    files = []
    # 第一类：密钥文件（每个 SSL 证书对一个 PEM）
    for id, pair in conf.SSLKeyPairs:
        files.append(generatePEM(id, pair.Cert, pair.Key))

    # 构造「复合策略生成器」（把 6 种 policy 生成器组合成一个）
    policyGenerator = policies.NewCompositeGenerator(
        clientsettings, observability, snippetspolicy,
        proxysettings, ratelimit, waf,
    )

    # 第二类：模板执行链（本小节重点）
    files += executeConfigTemplates(conf, policyGenerator)

    # 第一类续：bundle / 证书包 / 认证文件
    for id, bundle in conf.WAF.WAFBundles:   files.append(generateWAFBundle(...))
    for id, bundle in conf.CertBundles:      files.append(generateCertBundle/CRLBundle(...))
    for id, data  in conf.AuthSecrets:       files.append(generateAuthFile(...))
    return files
```

注意一个细节：`httpUpstreams` 在进入执行链**之前**就被 `createUpstreams` 预先算好并传进 `getExecuteFuncs`。原因是 upstream 既要在 `executeUpstreams` 里渲染成 `upstream {}` 块，又要在生成 server 的 `keepAlive` 检查里被查询，所以提前算一次、复用两次（见 4.2.3）。

`executeConfigTemplates` 的内部用一个 `map[string][]byte fileBytes` 按 `dest` 分桶：

```
executeConfigTemplates(conf, generator):
    fileBytes = {}                       # dest -> 累积的字节
    httpUpstreams = createUpstreams(...)  # 预算 upstream
    for execute in getExecuteFuncs(...):  # 12 个 executeFunc
        for res in execute(conf):
            fileBytes[res.dest] += res.data   # 追加到同一个文件
    if plus: mgmtFiles = generateMgmtFiles(conf)
    # 把每个桶包成 agent.File（带 hash/权限/大小）
    files = [ makeFile(dest, bytes) for dest, bytes in fileBytes ]
    files += mgmtFiles
    return files
```

> 术语：**分桶合并（bucket-by-dest）** 是本讲最重要的机制——`servers`、`upstreams`、`maps`、`split_clients`、`telemetry` 五个执行函数都把结果写到**同一个** `httpConfigFile`，最终拼成一份 `http.conf`。下一小节（4.3）会解释为什么这样设计。

`getExecuteFuncs` 返回的 12 个执行函数按固定顺序排列：

| # | executeFunc | 落点 dest | 所属 NGINX 上下文 |
| --- | --- | --- | --- |
| 1 | `newExecuteMainConfigFunc` | `main-includes/main.conf` | main |
| 2 | `executeEventsConfig` | `events-includes/events.conf` | events |
| 3 | `newExecuteBaseHTTPConfigFunc` | `conf.d/http.conf` | http |
| 4 | `newExecuteServersFunc` | `conf.d/http.conf` + `conf.d/matches.json` | http |
| 5 | `newExecuteUpstreamsFunc` | `conf.d/http.conf` | http |
| 6 | `executeSplitClients` | `conf.d/http.conf` | http |
| 7 | `executeMaps` | `conf.d/http.conf` | http |
| 8 | `executeTelemetry` | `conf.d/http.conf` | http |
| 9 | `executeStreamServers` | `stream-conf.d/stream.conf` | stream |
| 10 | `executeStreamUpstreams` | `stream-conf.d/stream.conf` | stream |
| 11 | `executeStreamMaps` | `stream-conf.d/stream.conf` | stream |
| 12 | `executePlusAPI` | `conf.d/plus-api.conf` | http（仅 Plus） |

表中的 `dest` 常量值见 4.3.3。注意第 3~8 行全都指向 `http.conf`——这就是「分桶合并」的体现。

#### 4.2.3 源码精读

`Generate` 主流程——三段式：密钥 → 模板链 → bundle/auth：

[internal/controller/nginx/config/generator.go:125-159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L125-L159) —— 注意开头注释强调「调用方必须先校验配置」，否则会把恶意配置写进 NGINX。这是与 u8 策略校验（`validator`）的安全契约。

`executeConfigTemplates`——分桶合并的核心：

[internal/controller/nginx/config/generator.go:182-218](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L182-L218) —— `httpUpstreams` 在第 188 行提前算好；第 191-196 行循环执行 12 个函数并按 `dest` 追加字节；第 199-201 行仅 Plus 时额外生成 mgmt 文件；第 204-214 行把每个桶封成 `agent.File`（`Hash` 由 `filesHelper.GenerateHash` 计算，供 Agent 做增量下发比对）。

`getExecuteFuncs`——12 个执行函数的固定顺序表：

[internal/controller/nginx/config/generator.go:220-239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L220-L239) —— 顺序即表 4.2.2。注意 `newExecuteServersFunc`、`executeStreamServers`、`executeStreamUpstreams` 是 `GeneratorImpl` 的方法（带 `g.`），因为它们需要读 `g.plus`；其余是包级函数。

`keepAliveChecker` 的复用——解释「为什么 upstream 要提前算」：

[internal/controller/nginx/config/upstreams.go:43-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L43-L57) —— 它把 upstream 列表建成一个 map，供 `getConnectionHeader` 在生成 location 时查询「这个 upstream 是否启用了 keepalive」，从而决定写 `Connection: $connection_keepalive` 还是 `Connection: $connection_upgrade`。

几个典型执行函数的落点（用于练习对答案）：

- `executeUpstreams` 落 `httpConfigFile`：[upstreams.go:65-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L65-L72)
- `executeMaps` 落 `httpConfigFile`：[maps.go:33-52](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L33-L52)
- `executeServers` 落 `httpConfigFile` 且额外产出 `httpMatchVarsFile`（matches.json）：[servers.go:114-153](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L114-L153)
- `executeStreamServers` 落 `streamConfigFile`：[stream_servers.go:19-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/stream_servers.go#L19-L40)
- `executeTelemetry` 仅当配置了 endpoint 才返回结果，否则返回 `nil`：[telemetry.go:12-23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/telemetry.go#L12-L23)

#### 4.2.4 代码实践

**实践目标**：在 `Generate` 中找到「生成 servers、upstreams、maps 各自对应的执行函数」。

**操作步骤**：

1. 打开 [generator.go:220-239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L220-L239)，对照 `getExecuteFuncs` 的返回切片。
2. 逐个跳转到实现：
   - **servers** → `g.newExecuteServersFunc`（第 229 行）→ [servers.go:105](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L105) → `executeServers`。
   - **upstreams** → `newExecuteUpstreamsFunc`（第 230 行）→ [upstreams.go:59](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L59) → `executeUpstreams`。
   - **maps** → `executeMaps`（第 232 行，直接是包级函数，不经 `newXxx` 包装）→ [maps.go:33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/maps.go#L33)。
3. 在每个函数里找到 `dest:` 字段，记录它写到哪个文件。

**需要观察的现象**：三个函数的 `dest` **完全相同**，都是 `httpConfigFile`（即 `/etc/nginx/conf.d/http.conf`）。

**预期结果**：你会直观看到「servers + upstreams + maps（+ split_clients + telemetry）被拼进同一个 `http.conf`」。这正是分桶合并机制。

**进阶**：再追一个**流式**对照——`executeStreamServers` / `executeStreamUpstreams` / `executeStreamMaps` 三者的 `dest` 都是 `streamConfigFile`，说明 stream 上下文也走同样的分桶合并，只是桶换成了 `stream.conf`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 servers/upstreams/maps 不各自生成独立文件（如 `servers.conf`、`upstreams.conf`、`maps.conf`），而要挤进一个 `http.conf`？

**参考答案**：两方面原因。（1）**NGINX 上下文约束**：`upstream {}`、`map {}`、`server {}`、`split_clients {}` 都必须出现在 `http {}` 上下文内。只要它们最终都在被 `http {}` include 的文件里即可，至于是分一个文件还是合一个文件，NGINX 不关心。（2）**工程取舍**：合成少数几个大文件（`http.conf`、`stream.conf`）便于 Agent 做 hash 比对与增量下发——一个文件变了一个 hash，Agent 就知道要重写它；若拆成几百个小文件，比对与传输开销都更大。注意「分桶」是按 dest 合并，不是按 NGINX 指令类型合并。

**练习 2**：`executeTelemetry` 在什么情况下返回 `nil`？返回 `nil` 后会发生什么？

**参考答案**：当 `conf.Telemetry.Endpoint == ""`（即用户没开启 OpenTelemetry tracing）时返回 `nil`。`executeConfigTemplates` 的循环对 `nil` 结果自然跳过（内层 `for _, res := range results` 不迭代），于是 `http.conf` 里就不会出现 otel 配置块。这是一种「按需生成」模式，避免空配置污染输出。

### 4.3 文件目录组织

#### 4.3.1 概念说明

生成器产出的几十个 `agent.File` 最终要落到数据面 Pod 的 `/etc/nginx/` 下。NGINX 启动时只读一个主配置 `/etc/nginx/nginx.conf`，**这份文件是静态的、写死的**，不在生成器产物里。它的作用是「骨架 + include 入口」：用若干条 `include /etc/nginx/<dir>/*.conf;` 把生成器动态产出的文件拉进各个上下文。

所以「目录组织」要回答的问题是：**生成器把哪类配置写到哪个目录，才能让静态 nginx.conf 的 include 把它放进正确的 NGINX 上下文？**

答案是一张「目录 ↔ 上下文」映射表：

| 目录 | 常量 | include 进的上下文 | 典型内容 |
| --- | --- | --- | --- |
| `/etc/nginx/main-includes/` | `mainIncludesFolder` | main | `load_module`、main 片段、Plus 的 mgmt 块 |
| `/etc/nginx/events-includes/` | `eventsIncludesFolder` | events | `worker_connections` 等 |
| `/etc/nginx/conf.d/` | `httpFolder` | http | servers/upstreams/maps/split_clients/otel/plus-api/matches.json |
| `/etc/nginx/stream-conf.d/` | `streamFolder` | stream | stream servers/upstreams/maps |
| `/etc/nginx/secrets/` | `secretsFolder` | （被指令按绝对路径引用） | PEM 证书/私钥、CA 证书包、CRL、auth 文件 |
| `/etc/nginx/includes/` | `includesFolder` | （被 server/location 的 include 引用） | 策略片段、SnippetsFilter 片段、AuthZ map |
| `/etc/app_protect/bundles/` | `appProtectBundleFolder` | （被 WAF 指令引用） | WAF bundle（.tgz） |

> 术语：**secrets/includes/bundles 目录**与上述四个「上下文目录」不同——它们不被 nginx.conf 的顶层 `include` 拉入，而是被**具体指令**按绝对路径引用（如 `ssl_certificate /etc/nginx/secrets/xxx.pem;`、`include /etc/nginx/includes/yyy.conf;`）。所以这些文件的内容是「数据」而非「NGINX 语法片段」。

#### 4.3.2 核心流程

把「生成器产物」与「静态 nginx.conf」串起来的链路：

```
静态 nginx.conf（镜像内写死）
├─ main 上下文:  include /etc/nginx/main-includes/*.conf;
│                ├─ main.conf        ← executeMainConfig
│                ├─ mgmt.conf        ← generateMgmtFiles (仅 Plus)
│                └─ deployment_ctx.json ← GenerateDeploymentContext
├─ events {} :   include /etc/nginx/events-includes/*.conf;
│                └─ events.conf      ← executeEventsConfig
├─ http {} :     include /etc/nginx/conf.d/*.conf;
│                ├─ http.conf        ← servers + upstreams + maps
│                │                     + split_clients + base_http + otel
│                ├─ matches.json     ← executeServers (NJS 用的路由匹配表)
│                └─ plus-api.conf    ← executePlusAPI (仅 Plus)
│                 （server/location 内部还会 include /etc/nginx/includes/*.conf）
└─ stream {} :   include /etc/nginx/stream-conf.d/*.conf;
                 └─ stream.conf      ← stream servers + upstreams + maps
```

要点：

1. **「一个 dest 常量 = 一个桶」**：生成器用一组 `const`（`httpConfigFile`、`streamConfigFile`、`mainIncludesConfigFile`…）统一管理所有落点路径。新增一种落点只需加一个常量并在某个 `executeFunc` 里引用它，无需改 `executeConfigTemplates` 的合并逻辑。
2. **密钥类文件绕过执行链**：`generatePEM` 等直接在 `Generate` 里 append，文件名由 ID 拼成（如 `<id>.pem`、`<id>.crt`），权限是 `SecretFileMode`（更严格），而配置文本文件是 `RegularFileMode`。
3. **include 去重**：很多 server/location 会引用**同一个** include 文件（比如同一条策略挂在多条路由上）。`includes.go` 负责按文件名去重，保证「一个 unique include 只生成一个文件」。

#### 4.3.3 源码精读

所有目录与文件路径常量集中定义在 generator.go 顶部——这是理解目录组织的「单一信息源」：

[internal/controller/nginx/config/generator.go:31-77](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L31-L77) —— 注释里那句「Volumes here also need to be added to our crossplane ephemeral test container」提醒：这些路径同时是测试容器要挂载的卷，改路径要同步改测试基建。

静态 nginx.conf 骨架——四条 `include` 即四个上下文入口：

[internal/controller/nginx/conf/nginx.conf:1-55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L1-L55) —— 注意第 2 行 `include /etc/nginx/main-includes/*.conf;`、第 9 行 events 内的 include、第 31 行 `include /etc/nginx/conf.d/*.conf;`（http）、第 54 行 `include /etc/nginx/stream-conf.d/*.conf;`（stream）。生成器的所有 `dest` 常量值必须落在这四个通配符能匹配到的目录里，否则不会被加载。

密钥类文件生成（绕过执行链）——以 PEM 为例：

[internal/controller/nginx/config/generator.go:241-260](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L241-L260) —— `generatePEMFileName` 用 `filepath.Join(secretsFolder, string(id)+".pem")` 拼路径；权限 `SecretFileMode`。证书包、CRL、auth 文件、WAF bundle 走同构的 `generate*` 函数（[generator.go:262-325](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L262-L325)）。

include 去重——把 server/location 上的重复 include 收敛成唯一文件：

[internal/controller/nginx/config/includes.go:18-44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L18-L44) —— `createIncludeExecuteResultsFromServers` 遍历所有 server 及其 location 的 `Includes`，用 `map[string][]byte`（以 include 文件名为 key）天然去重，再转成 `executeResult`。注释点明：重复来自「一条策略作用于多条路由」或「一个 SnippetsFilter 挂在多条规则上」。

策略产物转 include——复合策略生成器的输出统一落到 `includesFolder`：

[internal/controller/nginx/config/includes.go:47-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L47-L61) —— `createIncludesFromPolicyGenerateResult` 把策略生成的 `policies.File` 前缀加上 `includesFolder + "/"`，变成 `/etc/nginx/includes/<name>`。这些文件随后被 server/location 用 `include` 指令引用。

#### 4.3.4 代码实践

**实践目标**：验证「生成器的每个 `dest` 常量都能被静态 nginx.conf 的某条 `include` 通配符匹配到」，建立目录组织的全局心智图。

**操作步骤**：

1. 打开 [generator.go:31-77](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L31-L77)，列出所有 `*ConfigFile` / `*Folder` 常量值。
2. 打开 [nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf)，找出四条 `include` 通配符。
3. 建一张映射表，把每个 `dest` 常量归到「被哪条 include 匹配 / 被指令按绝对路径引用」两类之一。

**需要观察的现象**：

- `httpConfigFile`（`/etc/nginx/conf.d/http.conf`）、`httpMatchVarsFile`（`/etc/nginx/conf.d/matches.json`）、`nginxPlusConfigFile`（`/etc/nginx/conf.d/plus-api.conf`）都能被 `include /etc/nginx/conf.d/*.conf;` 匹配——注意 `matches.json` 虽然后缀是 `.json` 但它不是被 include 的，而是被 NJS 模块按路径读取的（它落在 conf.d 只是为方便统一下发，不被 include 加载）。
- `secretsFolder`、`includesFolder`、`appProtectBundleFolder` 不在任何 `include` 通配符里——它们靠具体指令引用。

**预期结果**：你得到一张「dest 常量 → nginx.conf include 行 / 绝对路径引用」的对照表，彻底看清目录组织。**待本地验证**：用 `grep -n "include" internal/controller/nginx/conf/nginx.conf` 确认只有 4 条 include。

**进阶（源码阅读型）**：在 `base_http_config.go` 里找 `executeBaseHTTPConfig` 的 `dest`（[base_http_config.go:76-112](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config.go#L76-L112)），确认它也是 `httpConfigFile`——于是 `http.conf` 实际由 base_http + servers + upstreams + maps + split_clients + telemetry **六个**执行函数共同拼成。这解释了为什么 `http.conf` 是最长、最复杂的生成文件。

#### 4.3.5 小练习与答案

**练习 1**：`matches.json` 的 `dest` 是 `/etc/nginx/conf.d/matches.json`，落在 `conf.d/` 下，但它会被 `include /etc/nginx/conf.d/*.conf;` 加载吗？为什么？

**参考答案**：不会被加载。`include *.conf` 只匹配 `.conf` 后缀，`matches.json` 不匹配。它放在 conf.d 只是为了和 http 配置一起被 Agent 下发与管理；实际是由 NJS 模块（`httpmatches.js`）在运行时按文件路径读取的——`servers.go` 里 `routeMatch` 结构的注释明确说它「stored as a key-value pair in /etc/nginx/conf.d/matches.json」并被 NJS 模块查询。

**练习 2**：如果想新增一种「只在 main 上下文生效的 NGINX 片段」，应该把它写到哪个目录？要走 `executeConfigTemplates` 还是直接 append？

**参考答案**：应写到 `mainIncludesFolder`（`/etc/nginx/main-includes/`），因为它会被 nginx.conf 第 2 行的 `include /etc/nginx/main-includes/*.conf;` 拉进 main 上下文。如果它是模板渲染式的配置文本，应新写一个 `executeFunc` 并加进 `getExecuteFuncs` 的切片（让它走分桶合并）；如果它是纯数据文件（如 JSON），则像 `generateMgmtFiles`/`GenerateDeploymentContext` 那样在 `Generate`/`executeConfigTemplates` 里直接 append 成 `agent.File`。

## 5. 综合实践

**任务**：画一张「从 `dataplane.Configuration` 到数据面 `/etc/nginx/` 落盘文件」的完整数据流图，并用源码行号标注每一段。

**要求覆盖**：

1. **三类产物的分流**：在 `Generate`（[generator.go:125-159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L125-L159)）里标出「密钥文件 / 模板执行链 / bundle+auth」三段。
2. **模板执行链的分桶**：在 `executeConfigTemplates`（[generator.go:182-218](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L182-L218)）里画出 12 个 `executeFunc` 如何按 `dest` 合并到 `http.conf` / `stream.conf` / `main.conf` / `events.conf` / `plus-api.conf` / `matches.json` 六个桶。
3. **桶 → 目录 → nginx.conf include**：把每个桶的 `dest` 常量值（[generator.go:31-77](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L31-L77)）连到 [nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf) 的对应 `include` 行，并标注它进入哪个 NGINX 上下文（main/events/http/stream）。
4. **密钥类文件的特殊路径**：标出 `secrets/`、`includes/`、`app_protect/bundles/` 三个目录不被 include、而是被指令按绝对路径引用。

**验收**：能用一句话回答「为什么 servers、upstreams、maps 三个执行函数的 `dest` 完全相同，最终却不冲突」——即分桶合并机制；并能解释「静态 nginx.conf 一行都不用生成器写，生成器只填充它 include 进来的目录」这一架构取舍的好处（生成器无需关心 main/events/http/stream 的骨架，只管往对应目录放文件）。

> 进阶可选：阅读 [generator_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator_test.go) 的 `TestGenerate`，观察它如何构造一份 `dataplane.Configuration` 并断言生成的 `[]agent.File` 的文件名集合，理解生成器的「输入→输出」可单测性。

## 6. 本讲小结

- **生成器是「Configuration → []agent.File」的唯一桥梁**：`Generator` 接口仅为测试而抽象，生产用无状态的 `GeneratorImpl`（只持 `plus`/`usageReportConfig`/`logger`）。
- **`Generate` 三段式**：先写密钥 PEM，再走模板执行链 `executeConfigTemplates`，最后写 bundle/cert/auth 文件；密钥类绕过模板链、直接 append。
- **模板执行链 = 12 个 `executeFunc`**：每个函数吃整份配置、吐 `[]executeResult{dest, data}`；`executeConfigTemplates` 用 `map[string][]byte` **按 dest 分桶合并**。
- **分桶合并是核心机制**：servers/upstreams/maps/split_clients/base_http/telemetry 六个函数都写 `http.conf`，最终拼成一份大文件；stream 侧同理拼成 `stream.conf`。
- **目录 ↔ NGINX 上下文严格对应**：`main-includes`→main、`events-includes`→events、`conf.d`→http、`stream-conf.d`→stream；这四个目录被静态 `nginx.conf` 的四条 `include` 通配符拉入对应上下文。
- **`secrets`/`includes`/`bundles` 是「数据目录」**：不被 include，而是被具体指令（`ssl_certificate`、`include`、WAF 指令）按绝对路径引用；includes.go 负责把策略/SnippetsFilter 产物按文件名去重后落到 `includes/`。

## 7. 下一步学习建议

本讲只讲了「总装与目录组织」。要理解生成出来的配置文本里**到底写了什么 NGINX 指令**，建议按以下顺序继续：

- **u6-l2 nginx.conf 骨架与模板体系**：深入静态 `nginx.conf` 与 main/events/base http 模板的配合，理解 `executeMainConfig`/`executeEventsConfig`/`executeBaseHTTPConfig` 渲染出的具体指令。
- **u6-l3 Servers、Upstreams 与 Locations 生成**：下钻 `executeServers`/`executeUpstreams` 的模板与 `server{}`/`upstream{}`/`location{}` 块细节（zone、keepalive、负载均衡、location 匹配）。
- **u6-l4 Stream 配置：TCP/UDP/TLS Passthrough**：下钻 `executeStreamServers`/`executeStreamUpstreams`/`executeStreamMaps`，理解 stream 上下文与 TLS passthrough（SNI）。
- **u8-l3 策略到 NGINX 指令的生成**：理解本讲出现的 `policies.NewCompositeGenerator` 如何把 6 种策略生成器组合，并通过 `GenerateForMain/HTTP/Server/Location` 在执行链各处注入 include。
- **u7-l1 NginxUpdater 与 gRPC Agent 通信**：本讲产出的 `[]agent.File` 会交给 `NginxUpdater.UpdateConfig` 经 gRPC 下发——这是下游接力点。
