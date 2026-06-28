# 策略到 NGINX 指令的生成

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 NGF「策略」在数据流向中处于哪一环：即已经过校验、附着（attach）的 `Policy` 对象，最终是怎么变成一段 NGINX 配置文本的。
- 理解**复合策略生成器（CompositeGenerator）**的设计：为什么把多种策略各做成一个 `Generator`，再用一个组合器把它们扇出（fan-out）汇总。
- 读懂 `clientsettings`、`ratelimit`、`proxysettings`、`observability`、`snippetspolicy` 等策略生成器各自如何把 CRD 字段翻译成 NGINX 指令，并能区分它们各自落在哪个 NGINX 上下文（main / http / server / location）。
- 理解**策略校验器（Validator）**与复合校验器（CompositeValidator）的作用：为什么生成之前还要再校验一次，校验防的是什么。
- 具备扩展能力：能模仿现有生成器，为一个新策略字段新增一条「字段 → 指令」的翻译。

本讲承接 [u8-l2 自定义 CRD 与策略附着](u8-l2-crd-api-and-policy-attachment.md)（策略如何在图中被解析、附着到 Gateway/Route）与 [u6-l3 Servers、Upstreams 与 Locations 生成](u6-l3-servers-upstreams-locations.md)（server/location 块如何生成），回答最后一个问题：**附着好的策略，怎样进入最终的 NGINX 配置文件**。

## 2. 前置知识

### 2.1 NGINX 的上下文层级

NGINX 配置文件 `nginx.conf` 有四个主要的「上下文（context）」，层层嵌套：

```
main            # 最外层：全局参数、加载模块
└── events      # 连接相关
└── http        # HTTP/HTTPS 相关
    └── server  # 一个虚拟主机（对应一个监听 + 域名）
        └── location  # 一个 URL 路径的匹配块
└── stream      # L4（TCP/UDP）相关
```

一条 NGINX 指令只能写在特定的上下文里。例如 `limit_req_zone` 必须写在 `http` 上下文（声明一个限速桶），而 `limit_req` 必须写在 `http` / `server` / `location` 上下文（把桶应用到请求）。策略生成器的核心难点之一，就是**把一个策略字段，准确地放进它该去的上下文**。

### 2.2 Go `text/template`

NGF 用标准库 `text/template` 把结构体渲染成配置文本：

- `{{ .Field }}` 表示取当前对象的 `Field` 字段；
- `{{ if .Field }}...{{ end }}` 表示字段非零值时才渲染（这是「字段可选」的标配写法）；
- `{{ range $r := .Rules }}...{{ end }}` 表示遍历切片。

包加载时 `template.Must(...Parse(...))` 把模板编译成全局变量，渲染时由 `helpers.MustExecuteTemplate` 执行，渲染失败直接 panic（fail-fast）。

### 2.3 include 机制（承接 u6-l2）

NGF 的 `nginx.conf` 是静态骨架，靠 `include` 通配把可变内容拉进来。策略生成出来的不是直接嵌进 server/location 的文本，而是一个个独立的 `.conf` **文件**，再被转成 `include` 指令挂到对应上下文。这一点决定了策略生成器的产物形态：`[]policies.File`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/nginx/config/policies/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go) | 定义 `Generator` 接口、`CompositeGenerator` 组合器、`UnimplementedGenerator` 占位基类、`File`/`GenerateResultFiles` 产物类型。 |
| [internal/controller/nginx/config/policies/policy.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go) | 定义统一的 `Policy` 接口、`GlobalSettings`，以及公共的 `ValidateTargetRef`。 |
| [internal/controller/nginx/config/policies/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go) | 定义 `Validator` 接口、`CompositeValidator`（含 `NewManager` 注册器）、`ManagerConfig`。 |
| [internal/controller/nginx/config/policies/clientsettings/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go) | `ClientSettingsPolicy` 生成器：客户端相关指令（body 大小、keepalive 等）。 |
| [internal/controller/nginx/config/policies/clientsettings/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go) | `ClientSettingsPolicy` 校验器：防注入校验 + 冲突检测。 |
| [internal/controller/nginx/config/policies/ratelimit/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go) | `RateLimitPolicy` 生成器：`limit_req_zone`（http 上下文）+ `limit_req`（server/location 上下文）。 |
| [internal/controller/nginx/config/policies/ratelimit/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/validator.go) | `RateLimitPolicy` 校验器：rate/key 正则校验、冲突检测。 |
| [internal/controller/nginx/config/policies/proxysettings/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/proxysettings/generator.go) | `ProxySettingsPolicy` 生成器：代理缓冲/超时指令。 |
| [internal/controller/nginx/config/policies/observability/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go) | `ObservabilityPolicy` 生成器：OpenTelemetry `otel_*` 指令。 |
| [internal/controller/nginx/config/policies/snippetspolicy/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/snippetspolicy/generator.go) | `SnippetsPolicy` 生成器：按 NGINX 上下文注入原始片段。 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | 总配置生成器 `Generate`：在此实例化 `CompositeGenerator` 并驱动模板执行链。 |
| [internal/controller/nginx/config/includes.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go) | `createIncludesFromPolicyGenerateResult`：把策略产物 `[]policies.File` 转成 `[]Include`。 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | `createPolicyManager`：把每个策略的 `Validator` 按 GVK 注册进 `CompositeValidator`。 |

## 4. 核心概念与源码讲解

### 4.1 复合策略生成器（CompositeGenerator）

#### 4.1.1 概念说明

NGF 有多种策略 CRD：`ClientSettingsPolicy`、`ProxySettingsPolicy`、`RateLimitPolicy`、`ObservabilityPolicy`、`SnippetsPolicy`、`WAFPolicy` 等（详见 u8-l2）。它们都要「翻译成 NGINX 指令」，但：

- 各自翻译的指令完全不同；
- 各自能落的 NGINX 上下文不同（有的只能进 `server`，有的能进 `http`、`main`）。

如果写一个巨大的 `switch policyType`，每加一种策略就要改这个大函数，违反开闭原则。NGF 的解法是**组合模式（Composite Pattern）**：

- 给每种策略定义一个**生成器**，实现统一的 `Generator` 接口；
- 再做一个**复合生成器 `CompositeGenerator`**，持有一组生成器；调用复合生成器时，它把请求**转发给内部每一个生成器**，再**把它们的产物拼接**起来返回。

这样一来，新增一种策略 = 新增一个生成器 + 在构造复合生成器时多塞一个，老代码零改动。

> 小知识：NGINX 配置里同一上下文可以写多条指令，所以「多个生成器的产物拼在一起」是合法的，只要各自的文件名不冲突。

#### 4.1.2 核心流程

策略生成的整体数据流（一个请求方向的横切关注点）：

```
dataplane.Configuration（含挂在各 server/location 上的 []Policy）
        │
        ▼
GeneratorImpl.Generate（总配置生成器）
        │  在此 new 一个 CompositeGenerator（持有 6 个策略生成器）
        ▼
CompositeGenerator.GenerateFor{Main|HTTP|Server|Location|InternalLocation}(pols)
        │  遍历内部每个策略生成器，逐个调用同名方法，拼接 []File
        ▼
各策略生成器（clientsettings / ratelimit / ...）
        │  类型断言过滤 + 模板渲染
        ▼
[]policies.File  ← 一组 {Name, Content} 配置片段文件
        │
        ▼
createIncludesFromPolicyGenerateResult → []Include
        │
        ▼
被 server/location/main/http 块以 include 引用，最终拼进 nginx.conf
```

注意：**同一个 `GenerateForXxx` 方法名对应「同一个 NGINX 上下文」**。例如 `GenerateForServer` 的产物会变成 server 块里的 `include`，`GenerateForLocation` 的产物会变成 location 块里的 `include`。生成器只需实现它「需要」的那几个上下文方法，其余用 `UnimplementedGenerator` 占位返回 `nil`。

#### 4.1.3 源码精读

**Generator 接口：五个方法对应五个上下文。**

[internal/controller/nginx/config/policies/generator.go:10-21](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L10-L21) —— 每个方法接收一组策略 `[]Policy`（外加 server/location 等上下文信息），返回一组配置文件。注意 `//counterfeiter:generate . Generator` 注释：这是用 [counterfeiter](https://github.com/maxbrunsfeld/counterfeiter) 工具自动生成测试用 fake 的指令（呼应 u13-l1 测试体系）。

**产物类型 `File` 与 `GenerateResultFiles`。**

[internal/controller/nginx/config/policies/generator.go:23-30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L23-L30) —— `File{Name, Content []byte}` 就是一个配置片段文件，名字决定落盘文件名（也是去重键），内容是渲染好的 NGINX 指令文本。

**CompositeGenerator：转发 + 拼接。**

[internal/controller/nginx/config/policies/generator.go:32-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L32-L40) 定义组合器，`NewCompositeGenerator(generators ...Generator)` 用可变参数收下一组生成器。以 server 上下文为例：

[internal/controller/nginx/config/policies/generator.go:64-73](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L64-L73) —— 核心就一个 `for` 循环：对每个内部生成器调用 `GenerateForServer`，把返回的切片 `append` 进 `compositeResult`。五个上下文方法的实现结构完全一样，只是调用名不同。这正是组合模式的精髓：组合器自己不生成任何指令，只做转发与合并。

**UnimplementedGenerator：让生成器只实现「需要」的方法。**

[internal/controller/nginx/config/policies/generator.go:97-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L97-L119) —— Go 接口要求实现全部方法，但一个策略往往只关心 1~2 个上下文。`UnimplementedGenerator` 把五个方法都实现成「返回 `nil`（无产物）」，任何具体生成器**嵌入它**后就只需重写关心的方法，未重写的默认返回空。例如 `clientsettings.Generator` 只重写 server/location，其余自动是空。

**复合生成器在哪里被构造。**

[internal/controller/nginx/config/generator.go:132-141](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L132-L141) —— 总生成器 `Generate` 每次渲染配置时 new 一个 `CompositeGenerator`，把 6 个策略生成器（clientsettings / observability / snippetspolicy / proxysettings / ratelimit / waf）塞进去，然后传给模板执行链 `executeConfigTemplates`。注意 `observability.NewGenerator(conf.Telemetry)` 还额外接收了全局遥测配置（用于注入全局 span 属性），说明生成器可以是无状态的，也可以持有少量全局上下文。

**产物如何变成 include。**

[internal/controller/nginx/config/includes.go:47-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L47-L61) —— `createIncludesFromPolicyGenerateResult` 把每个 `File` 包成 `Include{Name: "includes/<文件名>", Content}`。这些 `Include` 随后被挂到对应 server/location/main 的 `Includes` 字段（见 u6-l3），最终在渲染时变成 `include includes/ClientSettingsPolicy_xxx.conf;` 一类的引用行。这就解释了「为什么策略产物是独立文件」：因为 include 机制要求被引用者是一个落盘文件。

> 呼应 u6-l1 的「按 dest 分桶合并」：策略生成的文件落在 `includes/` 数据目录，不被通配 include，而是被具体指令按文件名引用、先到先得去重。

#### 4.1.4 代码实践

**实践目标**：在源码阅读层面验证「复合生成器 = 转发 + 拼接」，并理解它在测试中如何被替换。

1. 打开 [internal/controller/nginx/config/policies/generator_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator_test.go)。
2. 阅读 `Context("Composite Generator")`：它构造了两个 `FakeGenerator`（counterfeiter 生成的假实现），分别让 `GenerateForServer` 返回不同的文件，然后断言 `generator.GenerateForServer(nil, http.Server{})` 返回的是**两个 fake 产物的拼接**（见 [generator_test.go:39-46](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator_test.go#L39-L46)）。
3. 同样阅读 `Context("Unimplemented Generator")`：断言嵌入 `UnimplementedGenerator` 后，未实现的方法返回 `nil`（[generator_test.go:67-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator_test.go#L67-L81)）。
4. 需要观察的现象：复合生成器的输出顺序 = 构造时传入生成器的顺序；这正是生产代码 `NewCompositeGenerator(clientsettings, observability, ...)` 顺序的含义。

**预期结果**：测试全绿，且你能解释「为什么 clientsettings 的 server 产物和 ratelimit 的 server 产物能同时出现在同一个 server 块里」——因为复合生成器把它们拼成了两个不同的 `.conf` 文件，各自被 `include` 进去。

#### 4.1.5 小练习与答案

**练习 1**：假设要新增一个策略 `MyPolicy`，它只在 `http` 上下文生成指令。你的生成器需要实现 `Generator` 接口的哪几个方法？其余方法怎么处理？

> **答案**：只需实现 `GenerateForHTTP`；其余四个方法（Main/Server/Location/InternalLocation）通过嵌入 `policies.UnimplementedGenerator` 自动获得返回 `nil` 的默认实现，无需手写。

**练习 2**：复合生成器把多个子生成器的产物拼接，会不会出现两个文件同名导致 include 覆盖？看源码判断。

> **答案**：去重发生在更上层——include 机制按 `Name` 先到先得（u6-l2 讲过 `includes.go` 的去重）。各策略生成器都把「策略类型 + namespace + name（+ 上下文后缀）」编进文件名（如 `ClientSettingsPolicy_ns_name.conf`），所以正常情况下不会撞名；ratelimit 更是显式按上下文加后缀（见 4.2.3）。

---

### 4.2 各类策略生成器

本节挑代表性的几种生成器，看它们各自如何「字段 → 指令」。所有生成器都遵循同一个套路：**类型断言过滤 → 取字段（带默认值）→ 模板渲染 → 包成 File**。

#### 4.2.1 ClientSettingsPolicy：最朴素的「字段可选」模板

**概念**：`ClientSettingsPolicy` 翻译的是与「客户端连接」相关的指令，如 `client_max_body_size`、`client_body_timeout`、`keepalive_timeout` 等。它落到 `server` / `location` / `internal location` 三个上下文。

**源码精读**：

[internal/controller/nginx/config/policies/clientsettings/generator.go:15-42](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L15-L42) —— 模板是「字段可选」的范本：每个指令都被 `{{- if .Body.MaxSize }}` 包住，只有该字段被用户设置了，才渲染对应的 NGINX 指令。注意 `keepalive_timeout` 的特殊处理（[第 34-40 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L34-L40)）：该指令格式是 `keepalive_timeout server [header]`，`header` 可选，所以模板分三种情况渲染。

[internal/controller/nginx/config/policies/clientsettings/generator.go:44-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L44-L67) —— `Generator` 嵌入 `UnimplementedGenerator`，只重写 server/location/internal-location 三方法，三者都调同一个 `generate`（因为 clientsettings 指令在这三个上下文都合法，渲染结果一致）。

[internal/controller/nginx/config/policies/clientsettings/generator.go:69-85](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L69-L85) —— `generate` 是核心循环：对每个策略做类型断言 `pol.(*ngfAPI.ClientSettingsPolicy)`（断言失败就 `continue`，跳过别的策略类型），然后把 **`csp.Spec` 整个**当作模板数据渲染（字段可选，无需中间结构），文件名按 `namespace/name` 编码。

> 这是生成器里最简单的一种：没有中间转换结构，直接拿 CRD Spec 喂模板。

#### 4.2.2 ProxySettingsPolicy：用中间结构做格式适配

**概念**：`ProxySettingsPolicy` 翻译代理缓冲/超时指令（`proxy_buffering`、`proxy_buffers`、`proxy_read_timeout` 等），落在 `http` / `location` / `internal location`。

**源码精读**：

[internal/controller/nginx/config/policies/proxysettings/generator.go:49-89](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/proxysettings/generator.go#L49-L89) —— 与 clientsettings 不同，它**先做一道转换** `getProxySettings(spec)`，把 CRD Spec 转成扁平的 `proxySettings` 结构。原因有两点：

1. **布尔转字符串**：CRD 里 `Buffering.Disable` 是 `*bool`，而 NGINX 指令要 `proxy_buffering on|off;`，需要把 `true→"off"`、`false→"on"`（Disable=true 表示关闭缓冲）。
2. **多字段拼一个指令**：`proxy_buffers` 需要把 `Buffers.Number` 和 `Buffers.Size` 拼成 `"4 8k"` 这样的字符串（[第 65-67 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/proxysettings/generator.go#L65-L67)）。

这说明一个通用规律：**当 CRD 字段与 NGINX 指令不是一一对应时，需要一层中间结构 `getXxxSettings()` 做格式适配**。

#### 4.2.3 RateLimitPolicy：一个策略横跨两个上下文 + 「影子策略」

**概念**：限速是策略生成里最复杂的一种。NGINX 限速用两个指令配合：

- `limit_req_zone` —— 声明一个限速桶（key + zone + rate），**必须写在 `http` 上下文**；
- `limit_req` —— 把桶应用到请求（zone + burst + nodelay 等），写在 `server` / `location` 上下文。

所以 `RateLimitPolicy` 生成器要**为一个策略生成两段配置、落在两个不同上下文**。更棘手的是：当策略附着到 Route（而非 Gateway）时，`limit_req_zone`（http 上下文，Gateway 级）和 `limit_req`（location 上下文，Route 级）必须分开。NGF 的解法是「影子策略（shadow policy）」——在 `dataplane` 层为 Route 策略复制一份只带 http 上下文注解的副本。

**源码精读**：

**两个模板分别管两个上下文。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:14-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L14-L35) —— `rateLimitHTTPTemplate` 生成 `limit_req_zone`（http 上下文），`rateLimitReqTemplate` 生成 `limit_req`（server/location 上下文），后者还附带 `limit_req_log_level`、`limit_req_status`、`limit_req_dry_run` 等可选指令。三个模板变量 `tmplHTTP`/`tmplServer`/`tmplLocation` 在包加载时编译（[第 37-41 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L37-L41)）。

**默认值常量。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:43-58](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L43-L58) —— zone 默认 `10m`、rate 默认 `100r/s`、key 默认 `$binary_remote_addr`。CRD 字段缺省时用这些默认值兜底。

**字段 → 中间结构 `getRateLimitSettings`，含 zone 命名。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:92-147](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L92-L147) —— 关键细节在第 139 行：`ZoneName = fmt.Sprintf("%s_rl_%s_rule%d", rlp.Namespace, rlp.Name, i)`。每个规则的 zone 名字用「namespace + `_rl_` + 策略名 + `_rule` + 序号」唯一编码，保证多条规则、多个策略的 zone 互不冲突。

**生成器只重写 HTTP/Server/Location 三方法，三者复用同一个 `generate`。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:159-172](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L159-L172) —— 三个方法分别传不同的模板（`tmplHTTP`/`tmplServer`/`tmplLocation`）调 `generate`，其余上下文靠 `UnimplementedGenerator` 返回空。

**核心分发逻辑 `generate` + 影子策略守卫。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:174-218](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L174-L218) —— 这是最值得读的一段：

- [第 189-191 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L189-L191)：**影子策略守卫**。如果是「http-context-only」的影子策略，那么在 `tmplServer` 分支里直接 `continue` 跳过——因为 Route 级策略的 `limit_req_zone` 副本不该在 server 级再生成 `limit_req`（那会错误地限速整个 server 上所有路由）。注释里讲得很清楚。
- [第 195-207 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L195-L207)：**按上下文选文件名后缀**。http 上下文下，普通策略后缀 `gateway`、影子策略后缀 `internal_http`；server 后缀 `gateway_server`；location 后缀 `route`。这保证同一策略在不同上下文生成的文件名不撞。

**影子策略标记。**

[internal/controller/nginx/config/policies/ratelimit/generator.go:222-228](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go#L222-L228) —— `isShadowPolicy` 通过注解 `nginx.org/internal-annotation-http-context-only=true` 判断（常量定义在 [internal/controller/state/dataplane/configuration.go:58-60](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L58-L60)）。这个注解是 `dataplane` 层构建配置时打上去的（[configuration.go:2247](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2247)），用于把「Route 策略也要在 http 上下文建 zone」这个跨上下文需求，编码成一个生成器能识别的标记。

#### 4.2.4 ObservabilityPolicy：按 location 类型分发不同模板

**概念**：`ObservabilityPolicy` 翻译 OpenTelemetry `otel_*` 指令（详见 u11-l3）。它的特点是：**外部 location 和内部 location 用不同模板**。

**源码精读**：

[internal/controller/nginx/config/policies/observability/generator.go:79-114](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L79-L114) —— `GenerateForLocation` 内部用闭包 `buildTemplate` 统一渲染逻辑，但根据 `location.Type` 选不同模板：外部 location 用完整模板（含 `otel_trace`），内部 location 的「外部重定向」用精简模板 `tmplExtRedirect`（只含 `otel_trace` + `otel_trace_context`）。这是 u6-l3 讲过的「外部 location → 内部 location 重定向」模式在策略层的体现。

[internal/controller/nginx/config/policies/observability/generator.go:142-163](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/observability/generator.go#L142-L163) —— `getStrategy` 把 CRD 的采样策略（`parent`/`ratio`）翻译成 NGINX 能理解的 `otel_trace` 参数（`$otel_parent sampled` / `on` / `off` / 比例变量名），是「字段语义 → 指令参数」的典型转换。

> 该生成器持有 `telemetryConf dataplane.Telemetry`（构造时传入），用于把 **NginxProxy 全局配置的 span 属性**（`GlobalSpanAttributes`）注入到每个策略的产物里——这是「策略局部配置 + 全局配置合并」的实例，呼应 u8-l2 的 `EffectiveNginxProxy` 合并思想。

#### 4.2.5 SnippetsPolicy：唯一实现全部五个上下文的生成器

**概念**：`SnippetsPolicy`（实验特性）允许用户直接写原始 NGINX 片段，并指定注入到哪个上下文（main/http/server/location）。因此它是唯一一个实现了全部 `GenerateFor{Main|HTTP|Server|Location|InternalLocation}` 五个方法的生成器。

**源码精读**：

[internal/controller/nginx/config/policies/snippetspolicy/generator.go:40-63](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/snippetspolicy/generator.go#L40-L63) —— 五个方法分别对应五种 `NginxContext` 常量，最后都调统一的 `generate(pols, context)`。

[internal/controller/nginx/config/policies/snippetspolicy/generator.go:65-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/snippetspolicy/generator.go#L65-L111) —— 关键在第 77-80 行：**按 snippet 的 `Context` 字段过滤**，只把声明了当前上下文的片段渲染出来；第 89-102 行按上下文选不同的注释模板和文件名前缀（`SnippetsPolicy_main_`、`SnippetsPolicy_http_`、`SnippetsPolicy_server_`、`SnippetsPolicy_location_`）。

> SnippetsPolicy 因为直接放用户写的文本，安全风险高，所以受 `--snippets` 特性开关控制（[manager.go:499-504](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L499-L504) 里只有开启 snippets 才注册其校验器），详见 u12-l4。

#### 4.2.6 两个例外：WAFPolicy 与 UpstreamSettingsPolicy

并非所有策略都走「生成器产出 include 文件」这条路：

- **WAFPolicy**：有生成器（`waf/generator.go`），但 WAF 的主体（bundle）是另一套独立下发机制（u10），生成器只负责 WAF 相关的少量指令。
- **UpstreamSettingsPolicy**：根本**不实现 `Generator` 接口**，而是实现一个 `Processor`。

[internal/controller/nginx/config/policies/upstreamsettings/processor.go:9-34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/upstreamsettings/processor.go#L9-L34) —— `UpstreamSettingsPolicy` 修改的是 **upstream 块的属性**（zone 大小、负载均衡方法、keepalive 连接数），这些不是独立指令、而是直接改变 upstream 的渲染参数。所以它用 `Processor` 把策略**合并成一个 `UpstreamSettings` 结构**，再由 u6-l3 的 `upstreams.go` 在生成 upstream 块时读取这些参数。它不进复合生成器，因此在 [config/generator.go:132-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L132-L139) 的 `NewCompositeGenerator` 列表里看不到它。

> 结论：**「指令型」策略走 Generator（产出 include 文件），「属性型」策略走 Processor（改 upstream 渲染参数）**。设计策略扩展时先想清楚它属于哪一类。

#### 4.2.7 代码实践（本节综合实践）

**实践目标**：以 `ProxySettingsPolicy` 为模板，把一个已有的 CRD 字段翻译成 NGINX 指令，跑通「字段 → 中间结构 → 模板 → 文件」全链路。**这是源码阅读 + 本地修改型实践，不修改提交、不运行 NGF。**

1. 打开 [internal/controller/nginx/config/policies/proxysettings/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/proxysettings/generator.go)。
2. 假设你想新增「`ProxyReadTimeout` 之外，再支持 `proxy_next_upstream` 指令」。按现有套路：
   - 在 `proxySettings` 结构体加一个字段 `ProxyNextUpstream string`；
   - 在模板 `proxySettingsTemplate` 末尾加一段：
     ```
     {{- if .ProxyNextUpstream }}
     proxy_next_upstream {{ .ProxyNextUpstream }};
     {{- end }}
     ```
   - 在 `getProxySettings` 里从 CRD Spec 取值赋给新字段（若 CRD 暂无该字段，先写死一个示例值做验证）。
3. 需要观察的现象：用 `go test ./internal/controller/nginx/config/policies/proxysettings/...` 跑现有测试，确认你的改动没破坏既有渲染；再为新增指令加一个最小的表驱动测试用例。
4. 预期结果：测试通过，且你能解释「为什么这里需要中间结构 `proxySettings` 而不是直接用 CRD Spec」（因为存在布尔→字符串、单位拼接等适配）。
5. 若本地无 Go 环境，明确写「待本地验证」，但应能口头画出改动涉及的三处：结构体、模板、取值函数。

#### 4.2.8 小练习与答案

**练习 1**：为什么 `ClientSettingsPolicy` 可以直接拿 `csp.Spec` 当模板数据，而 `ProxySettingsPolicy` 和 `RateLimitPolicy` 都要先转成中间结构？

> **答案**：clientsettings 的每个 CRD 字段与 NGINX 指令**一一对应**且格式一致（值直接拿来用），所以 Spec 即模板数据。proxysettings 有布尔→on/off、多字段拼一个指令的适配；ratelimit 有 zone 命名、默认值兜底，都需要中间结构做转换。

**练习 2**：`RateLimitPolicy` 的 `generate` 函数里，为什么 `tmplServer && isHTTPContextOnly` 要 `continue`？

> **答案**：影子策略是 Route 级策略在 http 上下文建的 zone 副本。若它在 server 上下文再生成 `limit_req`，会错误地把限速应用到整个 server（即所有路由）上，且该 server 可能本就有自己的 `limit_req`。所以必须跳过。

---

### 4.3 策略校验 Validator

#### 4.3.1 概念说明

策略生成器把 CRD 字段直接写进 NGINX 配置文本，这带来一个**注入风险**：如果某个字符串字段（如 ratelimit 的 `Key`）里混入分号、换行、`}`，就可能让生成的 NGINX 配置被破坏或被注入额外指令。因此：

- CRD 的 OpenAPI / CEL 校验（kubebuilder 注解，如 [ratelimitpolicy_types.go:149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/ratelimitpolicy_types.go#L149) 的 `Pattern`）是第一道闸门，但表达力有限；
- NGF 在**图层 + 生成前**再补一道**代码层校验（Validator）**，专门查那些「CRD 校验管不了、但又与安全或语义强相关」的东西：防注入格式、跨字段依赖、多策略冲突。

与生成器对称，校验也用**组合模式**：每种策略一个 `Validator`，由 `CompositeValidator`（即 `Manager`）按 GVK 分发。

> 呼应 u8-l2：策略在图中经过 `ValidateGlobalSettings`（依赖 NginxProxy 全局配置）与冲突裁决（`markConflictedPolicies`）后，`Valid` 的策略才进入本讲的生成器。校验器就是这些裁决的底层引擎。

#### 4.3.2 核心流程

```
策略 Policy 对象
        │
        ▼
CompositeValidator（按 GVK 路由）
        │  mustExtractGVK(policy) → 查 map[GVK]Validator
        ▼
具体 Validator.Validate(policy) → []conditions.Condition
        │  1) ValidateTargetRef：group/kind 是否合法
        │  2) validateSettings：防注入格式校验（调 genericValidator）
        ▼
返回 []Condition（空 = 合法；非空 = NewPolicyInvalid）
        │
        ▼
（图中）沉淀进策略节点的 Conditions，Valid=false 的不参与生成
```

三个核心方法各有分工：

- `Validate`：校验单条策略自身的 spec（含 targetRef 合法性 + 防注入）。
- `ValidateGlobalSettings`：校验策略对**全局设置**（`GlobalSettings`，如 WAF/Telemetry 是否开启）的依赖。
- `Conflicts(a, b)`：判断两条同 kind 策略是否在**同一目标上设置了重叠字段**（用于 u8-l2 的冲突裁决）。

#### 4.3.3 源码精读

**Validator 接口与 CompositeValidator。**

[internal/controller/nginx/config/policies/validator.go:17-24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L17-L24) —— 三方法接口。`CompositeValidator` 持有 `validators map[GVK]Validator` 与一个 `mustExtractGVK`（[第 27-30 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L27-L30)）。

**注册器 NewManager + ManagerConfig。**

[internal/controller/nginx/config/policies/validator.go:32-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L32-L56) —— `NewManager(mustExtractGVK, configs ...ManagerConfig)` 把每个 `{GVK, Validator}` 注册进 map。注释点明它实现了 `validation.PolicyValidator` 接口，是图层校验入口能调用的统一门面。

**三个分发方法：未注册即 panic（fail-fast）。**

[internal/controller/nginx/config/policies/validator.go:59-95](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L59-L95) —— `Validate` / `ValidateGlobalSettings` / `Conflicts` 都是同一套：先用 `mustExtractGVK` 拿 GVK，查 map，查不到就 **`panic`**。这是 fail-fast 设计：若一个策略进来了却没有注册校验器，说明代码装配有漏洞（漏注册），宁可崩也不要让未校验的策略流入生成器。三方法各自按需把请求转给具体校验器。

**在 manager 里把所有策略的校验器注册进去。**

[internal/controller/manager.go:467-507](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L467-L507) —— `createPolicyManager` 是注册的总入口：为 6 种策略各构造一个 `Validator`，用 `mustExtractGVK(&XxxPolicy{})` 取 GVK，包成 `ManagerConfig` 注册；`SnippetsPolicy` 额外受 `cfg.Snippets` 开关控制（[第 499-504 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L499-L504)）。注意多数 `NewValidator(validator)` 都接收一个 `genericValidator`——那是复用的通用 NGINX 格式校验器（校验 duration、size 等），下文细讲。

#### 4.3.4 防注入校验实例：clientsettings 与 ratelimit

**公共的 targetRef 校验。**

[internal/controller/nginx/config/policies/policy.go:33-60](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L33-L60) —— `ValidateTargetRef` 用 `slices.Contains` 检查 group/kind 是否在支持列表内，不支持就返回 `field.NotSupported` 错误。所有策略校验器都复用它，避免重复。

**clientsettings 校验器：调 genericValidator 校验 duration/size。**

[internal/controller/nginx/config/policies/clientsettings/validator.go:27-43](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L27-L43) —— `Validate` 两步：先 `ValidateTargetRef`（支持 Gateway/HTTPRoute/GRPCRoute），再 `validateSettings`。

[internal/controller/nginx/config/policies/clientsettings/validator.go:86-120](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L86-L120) —— `validateSettings` 是防注入核心：对 `Body.Timeout`、`Body.MaxSize` 等字段，调 `genericValidator.ValidateNginxDuration` / `ValidateNginxSize` 检查是否是合法的 NGINX 时长/大小格式。注释点明设计意图（[第 84-85 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L84-L85)）：**只校验易注入字段，其余字段交给 CRD 校验**。第 166-180 行还有一个跨字段依赖校验：`keepalive_timeout` 的 `header` 依赖 `server`（header 可选但 server 必须先有），这是 CEL 表达不了的跨字段语义。

**ratelimit 校验器：正则校验 rate 与 key。**

[internal/controller/nginx/config/policies/ratelimit/validator.go:17-33](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/validator.go#L17-L33) —— 两个正则：`rateStringFmt`（`^\d+r/[sm]$`，如 `10r/s`、`500r/m`）和 `limitReqKeyFmt`（禁止空格和会终止 NGINX 解析的字符 `;{}` 等，防注入）。注意 key 的正则与 CRD 的 `Pattern` 注解（[ratelimitpolicy_types.go:149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/ratelimitpolicy_types.go#L149)）**同源**——CRD 闸门和代码闸门用同一条规则，双保险。

[internal/controller/nginx/config/policies/ratelimit/validator.go:93-154](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/validator.go#L93-L154) —— `validateSettings` 遍历每条 rule，对 `ZoneSize` 调 `ValidateNginxSize`、对 `Rate` 调 `validateNginxRate`（正则）、对 `Key` 调 `validateLimitReqKey`（正则），任一非法都收集成 `field.Invalid` 错误。

#### 4.3.5 冲突检测实例：Conflicts

**概念**：Gateway API 策略允许「继承」（inherited，见 u8-l2）。一个 Route 可能同时被「Gateway 级继承下来的策略」和「直接附着的策略」命中。若两者改了**同一个字段**，就冲突——必须裁决（u8-l2 的优先级裁决）。

**源码精读**：

[internal/controller/nginx/config/policies/clientsettings/validator.go:54-82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L54-L82) —— `Conflicts` 逐字段判断：只有当两条策略**都设了同一个子字段**（如都设了 `Body.Timeout`）才算冲突。这是「字段级粒度」的冲突判定，而非「整条策略级」——两个策略只要改的是不同字段就能共存。

[internal/controller/nginx/config/policies/ratelimit/validator.go:65-89](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/validator.go#L65-L89) —— ratelimit 同理：`DryRun`/`LogLevel`/`RejectCode` 三个全局字段，两条策略都设了同一个才算冲突。

> `Conflicts` 的结果供 u8-l2 的 `markConflictedPolicies` 按优先级裁决，被裁决为「冲突且优先级低」的策略会被打上 `Conflicted` 条件、不参与生成。

#### 4.3.6 代码实践

**实践目标**：理解校验器与生成器的对称关系，并能为一个新字段补上对应的防注入校验。

1. 阅读 [internal/controller/nginx/config/policies/clientsettings/validator_test.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator_test.go)（用 counterfeiter 生成的 fake genericValidator 替换真实依赖，呼应 u13-l1）。
2. 假设你在 4.2.7 给 ProxySettingsPolicy 新增了 `proxy_next_upstream` 字段，该字段值是 NGINX 指令参数。问自己：这个字段需要防注入校验吗？
3. 操作：若该字段允许任意字符串，则需在 [proxysettings/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/proxysettings/validator.go) 里加一条正则校验（仿 ratelimit 的 `validateLimitReqKey`），并加一个冲突判定分支（仿 clientsettings 的 `bodyConflicts`）。
4. 需要观察的现象：故意构造一个含 `;` 或换行的非法值，确认校验器返回 `NewPolicyInvalid` 条件，且生成器不会被调用（因为策略 `Valid=false`）。
5. 预期结果：能画出「非法值 → 校验器返回 Condition → 图标 Valid=false → 生成器跳过 → NGINX 配置不受污染」这条安全链路。若本地无法运行，写「待本地验证」。

#### 4.3.7 小练习与答案

**练习 1**：为什么 `CompositeValidator` 查不到校验器要 `panic` 而不是返回一个「合法」的默认值？

> **答案**：返回「合法」会让未校验的策略静默流入生成器，可能把恶意/非法字段写进 NGINX 配置（注入风险）。`panic` 是 fail-fast：装配漏注册是开发期 bug，应在第一时间暴露，绝不上线。

**练习 2**：`ValidateGlobalSettings` 在 clientsettings/ratelimit 里都返回 `nil`（[clientsettings/validator.go:46-51](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L46-L51)）。什么样的策略才需要它？

> **答案**：依赖 NginxProxy 全局开关的策略。例如 ObservabilityPolicy 的 tracing 依赖 `TelemetryEnabled`、WAFPolicy 依赖 `WAFEnabled`（见 [policy.go:24-30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L24-L30) 的 `GlobalSettings`）。这类策略在全局开关关闭时应被标为无效，故需 `ValidateGlobalSettings` 这一道。

---

## 5. 综合实践

**任务**：完整追踪一条 `ClientSettingsPolicy` 从 CRD 到 NGINX 配置文本的全链路，并把「校验—生成—挂载」三段串起来。这是贯穿本讲（4.1~4.3）的综合性源码阅读实践。

**步骤**：

1. **CRD 字段**：读 [apis/v1alpha1/clientsettingspolicy_types.go:39](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L39) 的 `ClientSettingsPolicySpec`，列出它有哪些字段（Body、KeepAlive 等），哪些带 kubebuilder 校验注解（第一道闸门）。
2. **校验**：读 [clientsettings/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go)，标出 `Validate` 调了哪些校验（targetRef、duration、size、跨字段 header→server）。在 [manager.go:467-507](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L467-L507) 找到它被 `NewManager` 注册的位置。
3. **生成**：读 [clientsettings/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go)，写出「字段 → 模板 → File」的映射，尤其 `keepalive_timeout` 的三态渲染。在 [config/generator.go:132-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L132-L139) 找到它被塞进 `CompositeGenerator` 的位置。
4. **挂载**：读 [includes.go:47-61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L47-L61)，写出 `File → Include` 的转换；再到 [servers.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go)（搜索 `GenerateForServer`）找到 server 块把该 include 挂上去的调用点。
5. **产出**：画一张时序图，标出 `ClientSettingsPolicy` CRD → `Validator.Validate` → `CompositeValidator` 路由 → 图 `Valid=true` → `CompositeGenerator.GenerateForServer` → `clientsettings.generate` → `File` → `createIncludesFromPolicyGenerateResult` → server 块 `include` 这条完整链路上每个函数所在的文件与行号。

**预期结果**：你能不查资料地向别人讲清「我写一个 ClientSettingsPolicy，NGF 怎么保证它合法、又怎么把它的字段变成 NGINX 的 `keepalive_timeout` 指令」。若某段链路无法本地验证（如实际 reload NGINX），标注「待本地验证」。

## 6. 本讲小结

- **复合模式是骨架**：`Generator` 接口 + `CompositeGenerator`（转发+拼接）+ `UnimplementedGenerator`（占位基类）三件套，让新增策略只动一处、老代码零改动；产物是 `[]policies.File`，经 `createIncludesFromPolicyGenerateResult` 转成 include 挂到对应上下文。
- **五个方法 = 五个上下文**：`GenerateFor{Main|HTTP|Server|Location|InternalLocation}` 分别对应 main/http/server/location/内部 location；一个生成器只需重写它关心的上下文。
- **字段 → 指令有三种范式**：直接用 Spec（clientsettings，一一对应）、经中间结构适配（proxysettings 的布尔/拼接、ratelimit 的 zone 命名/默认值）、按 location 类型分发模板（observability）。
- **ratelimit 是最复杂的案例**：一个策略横跨 http（`limit_req_zone`）与 server/location（`limit_req`）两个上下文，用「影子策略 + 注解」解决 Route 级策略的跨上下文 zone 声明问题。
- **「指令型」vs「属性型」**：指令型策略走 Generator（产 include 文件），属性型策略（UpstreamSettingsPolicy）走 Processor（改 upstream 渲染参数），设计扩展时先分类。
- **校验器与生成器对称**：`Validator` + `CompositeValidator`（按 GVK 路由）+ `NewManager` 注册；防注入校验（正则 + genericValidator）是生成前的安全闸门，`Conflicts` 字段级冲突判定服务于 u8-l2 的继承裁决；未注册校验器即 `panic`（fail-fast）。

## 7. 下一步学习建议

- **向下游**：读 u7-l1/u7-l2，看生成出的 `[]agent.File`（含策略 include）如何被 NginxUpdater 经 gRPC 下发到数据面 Agent。
- **向深挖安全**：读 u12-l4（SnippetsFilter / 认证过滤器），理解 NGF 对「用户直接写 NGINX 片段」这类更高风险扩展的安全约束；SnippetsPolicy 校验器是其前哨。
- **向可观测**：读 u11-l3（OpenTelemetry 链路追踪），把本讲的 `ObservabilityPolicy` 生成器与 telemetry 模板、全局 span 属性注入连起来看。
- **向二次开发**：读 u13-l3（二次开发），把本讲的「新增策略字段 → CRD + 校验器 + 生成器 + 测试」四件套作为新增 CRD 的标准改动模板。
- **动手建议**：尝试真正新增一个最小策略 CRD（一个字符串字段 → 一条 NGINX 指令），贯穿「API 类型定义 → manager 注册校验器 → 写生成器并塞进 CompositeGenerator → 加测试」，作为本讲学习的毕业练习。
