# SnippetsFilter 与认证过滤器

> 本讲属于专家层单元 u12「高级特性」。它承接 u8-l3（策略到 NGINX 指令的生成），但要讲清一个**关键边界**：SnippetsFilter 与 AuthenticationFilter 都不是 u8-l2 里讲的那种「带 `targetRefs` 的策略 CRD」，而是**挂在 HTTPRoute/GRPCRoute 的 `rules[].filters` 上的 ExtensionRef 过滤器**。它们走的是另一条接入路径，理解这条路径是本讲的核心。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 SnippetsFilter 如何把**任意 NGINX 指令**注入 `main` / `http` / `http.server` / `http.server.location` 四个上下文，并追踪它从 `HTTPRoute.rules[].filters` 到最终 `include` 指令的完整调用链。
- 区分 AuthenticationFilter 的三种认证类型（Basic / JWT / OIDC），知道它们各自映射到哪些 NGINX 指令、哪些需要 NGINX Plus。
- 评估这两种扩展的**安全约束**：为什么 SnippetsFilter 几乎不做内容校验而必须显式开关，为什么 AuthenticationFilter 要对 claim 值做严格正则校验。
- 独立部署一个 SnippetsFilter 示例，并用 `curl` 验证注入的指令确实生效。

## 2. 前置知识

本讲默认你已经掌握：

- **Gateway API 的 Route Filter 机制**：HTTPRoute/GRPCRoute 的 `rules[].filters` 是 Gateway API 内置的「请求处理扩展点」，标准类型有 `RequestHeaderModifier`、`RequestRedirect`、`URLRewrite`、`CORS` 等；当 `type: ExtensionRef` 时，则把语义外交给某个控制器的自定义资源。本讲的两个过滤器就是 NGF 对 `ExtensionRef` 的两种实现。
- **NGINX 上下文层级**（u6-l2 已讲）：`main > events > http > server > location` 是 NGINX 配置的嵌套作用域，指令只有写在合法上下文里才会被接受；`include <file>;` 在哪个上下文里写，被包含的内容就属于哪个上下文。
- **图节点三元组范式**（u5-l1）：`Source` + `Valid` + `Conditions`，错误不靠抛异常而靠把 `Conditions` 沉淀到节点、用 `Valid` 布尔位决定是否参与配置生成。
- **u8-l3 的策略生成框架**：组合式 `Generator`（`GenerateForMain/HTTP/Server/Location/InternalLocation`）。本讲的 SnippetsFilter 走的是**另一条非策略的链路**，但你会看到 SnippetsPolicy（一个独立 CRD）复用了这套框架，二者要分清。

术语速查：

| 术语 | 含义 |
|------|------|
| ExtensionRef | Route filter 的一种 `type`，把过滤语义委托给控制器自定义资源 |
| SnippetsFilter | NGF CRD，注入原始 NGINX 配置片段，挂在 Route rule 上 |
| SnippetsPolicy | NGF CRD（独立于 SnippetsFilter），同样注入片段，但用 `targetRefs` 附着到 Gateway，走 u8-l3 策略框架 |
| AuthenticationFilter | NGF CRD，配置 Basic/JWT/OIDC 认证，挂在 Route rule 上 |
| NGINX context | main / http / http.server / http.server.location 四个合法注入点 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [apis/v1alpha1/snippetsfilter_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetsfilter_types.go) | SnippetsFilter CRD 的 Go 类型定义与 kubebuilder 校验注解 |
| [apis/v1alpha1/authenticationfilter_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go) | AuthenticationFilter CRD 的类型定义（Basic/OIDC/JWT、Authorization） |
| [internal/controller/state/graph/snippets_filter.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go) | 图阶段：解析、校验 SnippetsFilter，提供 ExtensionRef 解析器 |
| [internal/controller/state/graph/authentication_filter.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go) | 图阶段：校验三种认证、Plus 门控、OIDC URI 冲突检测 |
| [internal/controller/state/graph/extension_ref_filter.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/extension_ref_filter.go) | `ExtensionRefFilter` 容器类型与两种过滤器的统一注册 |
| [internal/controller/state/dataplane/convert.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go) | 把图节点翻译成 dataplane 渲染模型（`convertSnippetsFilter`/`convertAuthenticationFilter*`） |
| [internal/controller/state/dataplane/configuration.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go) | `buildSnippetsForContext`（main/http 片段）与路由过滤器装配 `addExtensionRef` |
| [internal/controller/nginx/config/includes.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go) | 把片段转成 `include` 文件并按 server/location 归位 |
| [internal/controller/nginx/config/servers.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go) | 把片段 include 挂到 server/location、把认证挂到 location |
| [internal/controller/nginx/config/servers_template.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go) | 最终渲染 `include`、`auth_basic`、`auth_jwt`、`auth_oidc` 指令的模板 |
| [internal/controller/nginx/config/validation/auth_fields.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/validation/auth_fields.go) | 认证字段的正则校验（防指令注入的安全闸门） |
| [cmd/gateway/commands.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go) | `--snippets` 开关：SnippetsFilter 的运行时总闸（默认关） |

---

## 4. 核心概念与源码讲解

### 4.1 SnippetsFilter：把任意 NGINX 指令注入配置

#### 4.1.1 概念说明

SnippetsFilter 解决的是一个「兜底扩展」问题：Gateway API 与 NGF 内置的策略 CRD 覆盖不了所有 NGINX 能力（比如 `limit_except`、`proxy_cache`、某些第三方模块指令）。SnippetsFilter 让集群运维**直接把一段原始 NGINX 配置文本**注入到生成的 `nginx.conf` 里，从而在不改 NGF 代码的前提下使用任意 NGINX 指令。

它的接入方式和 u8-l2 的策略 CRD **不同**：

- 策略 CRD（ClientSettingsPolicy 等）用 `targetRefs` 附着到目标，是「策略附着模型」。
- SnippetsFilter 是一个 **ExtensionRef 过滤器**，被 HTTPRoute/GRPCRoute 的 `rules[].filters` 通过 `extensionRef` 引用，语义上是「这条路由规则上的一个过滤器」。

一个 SnippetsFilter 可以同时声明最多 4 段片段，每段绑定一个 NGINX 上下文。关键约束：**每个上下文只能有一段**。

#### 4.1.2 核心流程

从用户 YAML 到最终 `include` 指令，SnippetsFilter 经历这样一条链路（与 u4/u5 的事件管线对接）：

```
HTTPRoute.rules[].filters[type=ExtensionRef, kind=SnippetsFilter]
        │  (Route 构图时，把 LocalObjectReference 交给解析器)
        ▼
graph.getSnippetsFilterResolverForNamespace   ← 命中则置 Referenced=true
        │
        ▼
graph.processSnippetsFilters + validateSnippetsFilter  ← 结构校验：上下文枚举、每上下文一段、value 非空
        │  产出 graph.SnippetsFilter{ Source, Snippets(map), Conditions, Valid, Referenced }
        ▼
dataplane.addExtensionRef → convertSnippetsFilter      ← server/location 片段
dataplane.buildSnippetsForContext                       ← main/http 片段（Gateway 级别）
        │
        ▼
config.createIncludeFromSnippet / createIncludesFrom{Server,Location}SnippetsFilters
        │  把片段内容写成 includes/<Name>.conf 文件
        ▼
servers_template.go:  server 块 / location 块里渲染  include <Name>;
```

要点：

- main/http 片段在 **Gateway 维度**收集（一个 Gateway 下所有被引用的 SnippetsFilter 汇总），落到 `BaseHTTPConfig.Snippets` 与 `Configuration.MainSnippets`。
- server/location 片段在**路由规则维度**收集，跟着每条 MatchRule 的过滤器走。
- 片段内容是**原样透传**的——NGF 不解析、不重写、几乎不校验内容（见 4.3）。

#### 4.1.3 源码精读

**① CRD 定义：四个上下文与「每上下文一段」约束**

`SnippetsFilterSpec` 用 kubebuilder 注解把约束焊进 CRD（由 API server 准入层强制）：

[apis/v1alpha1/snippetsfilter_types.go:37-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetsfilter_types.go#L37-L57) —— `Snippets` 数组限制 1~4 条，并用一条 CEL `XValidation` 强制「同一 context 不得重复」。

上下文是固定枚举，对应 NGINX 的四个嵌套作用域：

[apis/v1alpha1/snippetsfilter_types.go:59-79](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetsfilter_types.go#L59-L79) —— `NginxContext` 枚举 `main`/`http`/`http.server`/`http.server.location`。

**② ExtensionRef 容器：两种过滤器共用一个类型**

`ExtensionRefFilter` 是一个「联合体」，用两个指针字段承载两种过滤器，`Valid` 表示解析结果是否可用：

[internal/controller/state/graph/extension_ref_filter.go:13-23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/extension_ref_filter.go#L13-L23) —— `SnippetsFilter` 与 `AuthenticationFilter` 各占一个指针位。

`validateExtensionRefFilter` 在 Route 构图时校验 `extensionRef` 的 `group`/`kind`，只认这两种 kind：

[internal/controller/state/graph/extension_ref_filter.go:46-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/extension_ref_filter.go#L46-L56) —— switch 只放行 `SnippetsFilter` 与 `AuthenticationFilter`。

**③ 图阶段：解析 + 校验 + 转 map**

`getSnippetsFilterResolverForNamespace` 返回一个闭包，Route 构图时按命名空间查 SnippetsFilter，命中即标记 `Referenced = true`：

[internal/controller/state/graph/snippets_filter.go:30-52](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L30-L52) —— 注意它在 39 行硬匹配 `ref.Group == ngfAPI.GroupName && ref.Kind == kinds.SnippetsFilter`，group/kind 不符直接返回 `nil`（不报错，留给别的过滤器或最终判 Invalid）。

`processSnippetsFilters` 是入口，对每个 SnippetsFilter 跑 `validateSnippetsFilter`：

[internal/controller/state/graph/snippets_filter.go:54-82](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L54-L82) —— 校验失败也照常进图，但 `Valid=false` 且带上 `Conditions`，这正是 u5-l1 的「无效也进图、错误沉淀为条件」范式。

`validateSnippetsFilter` 只做**结构校验**：value 非空、context 属于四个枚举值、每个 context 只出现一次。**它不校验 value 的具体内容**：

[internal/controller/state/graph/snippets_filter.go:95-158](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L95-L158) —— 注意 120-138 行的 switch 把未知 context 收进 `allErrs`，140-149 行用 `usedContexts` map 检测重复 context。没有对 `snippet.Value` 的正则/词法校验。

校验通过后，`createSnippetsMap` 把切片转成「context → value」的 map，方便后续按上下文取片段：

[internal/controller/state/graph/snippets_filter.go:84-93](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L84-L93)。

**④ 转成渲染模型：server/location vs main/http**

server/location 片段由 `convertSnippetsFilter` 提取，分别塞进 `ServerSnippet` / `LocationSnippet`：

[internal/controller/state/dataplane/convert.go:175-196](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L175-L196) —— 片段名由 `createSnippetName` 生成，形如 `SnippetsFilter_http.server_<ns>_<name>`。

main/http 片段在 Gateway 维度单独处理，`buildSnippetsForContext` 遍历该 Gateway 下所有「有效且被引用」的 SnippetsFilter，取出指定上下文的片段：

[internal/controller/state/dataplane/configuration.go:2357-2385](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2357-L2385) —— 2368 行的 `!filter.Valid || !filter.Referenced` 过滤很关键：未被任何 Route 引用的 SnippetsFilter 不会进配置（避免孤儿片段落盘）。

**⑤ 变成 include 文件并渲染指令**

`createIncludeFromSnippet` 把片段写成 `includes/<Name>.conf` 文件（`includesFolder` 是数据目录，靠绝对路径引用，不在 u6-l2 讲的通配 include 内）：

[internal/controller/nginx/config/includes.go:63-69](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L63-L69)。

`createIncludesFromServerSnippetsFilters` / `createIncludesFromLocationSnippetsFilters` 分别从 server / location 维度收集片段，并用 `deduplicateIncludes` 去重（同一个 SnippetsFilter 被多条路由规则引用时只生成一个文件）：

[internal/controller/nginx/config/includes.go:150-190](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L150-L190)。

`createLocations` 把 location 片段 include 挂到 location：

[internal/controller/nginx/config/servers.go:1215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1215) —— `location.Includes = append(location.Includes, createIncludesFromLocationSnippetsFilters(filters.SnippetsFilters)...)`。

最终模板在 location 块里渲染 `include`：

[servers_template.go:167-169](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L167-L169) —— location 内 `{{ range $i := $l.Includes }} include {{ $i.Name }};`；server 块的同理在 [servers_template.go:141-143](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L141-L143)。

#### 4.1.4 代码实践

> 目标：部署官方 `examples/snippets`，用 SnippetsFilter 给 `/coffee` 路径注入 `limit_except GET { deny all; }`，验证非 GET 请求被拒绝。

**操作步骤（待本地验证）**

1. 用 snippets 变体部署 NGF（SnippetsFilter 默认关闭，必须显式开启，见 4.3）：

   ```bash
   # 假设已按 u1-l4/u1-l5 建好 kind 集群
   helm install ngf ./charts/nginx-gateway-fabric \
     --create-namespace -n nginx-gateway \
     -f examples/helm/snippets/values.yaml
   ```

   `examples/helm/snippets/values.yaml` 会把 `--snippets` 开关打开（开启后才会处理 SnippetsFilter 与 SnippetsPolicy）。

2. 部署示例（后端应用 + Gateway + SnippetsFilter + HTTPRoute）：

   ```bash
   kubectl apply -f examples/snippets/app.yaml
   kubectl apply -f examples/snippets/gateway.yaml
   kubectl apply -f examples/snippets/limit-except-sf.yaml   # SnippetsFilter
   kubectl apply -f examples/snippets/httproutes.yaml        # coffee 路由引用了它
   ```

3. 取 Gateway IP/端口（参考 u1-l5 的四步法）：

   ```bash
   GW_IP=$(kubectl get gateway -n default gateway -o jsonpath='{.status.addresses[0].value}')
   ```

**需要观察的现象**

- SnippetsFilter 的 `Accepted` 状态为 `True`：`kubectl get snippetsfilter limit-except-sf -o yaml | grep -A3 conditions`。
- 在数据面 Pod 里查看生成的 location 配置，确认注入了 include：

  ```bash
  kubectl exec -n nginx-gateway <nginx-pod> -- cat /etc/nginx/includes/SnippetsFilter_http.server.location_default_limit-except-sf.conf
  # 应看到: limit_except GET { deny all; }
  ```

**预期结果**

- `GET /coffee` 正常 200。
- `POST /coffee` 被注入的 `limit_except` 拦截，返回 403（NGINX 对 `deny all` 的响应）。
- `/tea`（未引用 SnippetsFilter）不受影响，POST 也正常。

> 若 SnippetsFilter 状态为 `Accepted=False` 或注入指令未出现，先确认 `--snippets` 是否开启：`kubectl -n nginx-gateway deploy <ngf-deploy> -o yaml | grep -- '--snippets'`。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `limit_except` 片段挪到 `http.server` 上下文，会怎样？

**答案**：`limit_except` 是 location 级指令，写在 `http.server` 里会触发 NGINX 配置语法错误。此时 `nginx -t`（由 Agent 在 reload 时执行）会失败，数据面保持上一个有效配置，控制面回写错误状态。这正说明 SnippetsFilter 不校验指令合法性——合法性由 NGINX 自己兜底。

**练习 2**：同一个 SnippetsFilter 里写两段 `context: http.server.location` 的片段，会发生什么？

**答案**：CRD 的 CEL `XValidation`（`snippetsfilter_types.go:44`）会在 API server 准入阶段直接拒绝创建；即使绕过，`validateSnippetsFilter`（`snippets_filter.go:140`）也会判其 `Valid=false` 并回写 `Accepted=False`。两道闸门叠加。

**练习 3**：一个 SnippetsFilter 创建后没有任何 HTTPRoute 引用它，它的片段会进 `nginx.conf` 吗？

**答案**：不会。`buildSnippetsForContext`（`configuration.go:2368`）与 `getSnippetsFilterResolverForNamespace`（`snippets_filter.go:48`）都要求 `Referenced=true`，未被引用的 SnippetsFilter 不产生任何配置输出。

---

### 4.2 AuthenticationFilter：Basic / JWT / OIDC 三种认证

#### 4.2.1 概念说明

AuthenticationFilter 为路由规则配置**请求认证**。它同样是 ExtensionRef 过滤器（`extensionRef.kind: AuthenticationFilter`），同样挂在 `rules[].filters` 上。它的 `spec.type` 是一个三选一的枚举：

| type | 认证机制 | 对应 NGINX 模块/指令 | 是否需要 NGINX Plus |
|------|----------|----------------------|----------------------|
| `Basic` | HTTP Basic Auth（用户名/密码） | `ngx_http_auth_basic_module`：`auth_basic` / `auth_basic_user_file` | 否（OSS 可用） |
| `JWT` | JSON Web Token | `ngx_http_auth_jwt_module`：`auth_jwt` / `auth_jwt_key_file` / `auth_jwt_key_request` | 是 |
| `OIDC` | OpenID Connect | `ngx_http_oidc_module`：`auth_oidc` / `oidc_provider` | 是 |

之所以 JWT/OIDC 需要 Plus，是因为 `auth_jwt` 与 `auth_oidc` 指令只存在于 NGINX Plus 的商业模块里，OSS 镜像没有。这个依赖在图阶段被强制校验（见 4.2.3）。

此外，JWT 与 OIDC 都支持可选的 `Authorization` 字段，做 **claim 级别的细粒度授权**（不仅认证身份，还要求 token 里的某些 claim 满足规则），这会额外生成 `auth_jwt_require` 与一系列 `map`/`proxy_set_header`。

#### 4.2.2 核心流程

```
HTTPRoute.rules[].filters[type=ExtensionRef, kind=AuthenticationFilter]
        │
        ▼
graph.getAuthenticationFilterResolverForNamespace   ← 命中置 Referenced=true
        │
        ▼
graph.processAuthenticationFilters
   └─ validateAuthenticationFilter
        ├─ Basic:  resolveAuthenticationFilterSecret (校验 Secret 含 auth 键)
        ├─ JWT:    isPlus 门控 → File/Remote Secret 解析 → 可选 Authorization 校验
        └─ OIDC:   isPlus 门控 → validateOIDC (字段正则 + Secret 解析 + logout URI 校验)
        │  产出 graph.AuthenticationFilter{ Source, Conditions, Valid, Referenced }
        ▼
dataplane.convertAuthenticationFilter
   ├─ Basic → AuthBasic (从 referencedSecrets 取出 htpasswd 数据)
   ├─ JWT   → AuthJWT   (File/Remote + AuthZConfig)
   └─ OIDC  → AuthOIDC  (OIDCProvider + AuthZConfig)
        │
        ▼
config.updateLocationAuthenticationFilter  → location.AuthBasic / AuthJWT / AuthOIDC
        │
        ▼
servers_template.go 渲染 auth_basic / auth_jwt / auth_oidc 指令
```

OIDC 还有一段**绑定后**的二次校验：`validateOIDCFilters` 在路由绑定到 Listener 之后，检查「OIDC 必须挂在 HTTPS 监听器」以及「同一 hostname 上 callback/logout URI 不能冲突」。这是因为 OIDC 的回调路径会变成真实的 NGINX location，冲突会导致行为不确定。

#### 4.2.3 源码精读

**① CRD 类型：type 与 spec 字段的互斥**

`AuthenticationFilterSpec` 用一组 CEL `XValidation` 把「type 与对应字段必须配对、且不得设置其它字段」焊死：

[apis/v1alpha1/authenticationfilter_types.go:36-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L36-L67) —— 例如 `type != 'Basic' || !has(self.jwt)` 强制 Basic 不得设置 jwt 字段。`type` 枚举见 [authenticationfilter_types.go:69-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L69-L81)。

BasicAuth 很简单：一个 Secret 引用（装 htpasswd 数据）加一个 realm：

[apis/v1alpha1/authenticationfilter_types.go:83-92](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L83-L92)。

OIDCAuth 字段最多（issuer、clientID、clientSecret、redirectURI、logout、session、CA、CRL、Authorization 等）：

[apis/v1alpha1/authenticationfilter_types.go:100-195](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L100-L195) —— 注意 `ClientSecretRef` 与 `Issuer`/`ClientID` 是必填，`CACertificateRefs` 限制最多 1 个。

JWTAuth 区分 File（本地 JWKS Secret）/ Remote（远程 JWKS URI）两种 key 来源，用 CEL 强制二选一：

[apis/v1alpha1/authenticationfilter_types.go:271-315](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L271-L315)。

**② Plus 门控：JWT/OIDC 在 OSS 上直接判 Invalid**

`validateAuthenticationFilter` 是核心调度器，按 `spec.type` 分发；JWT 与 OIDC 分支**首行就检查 `isPlus`**：

[internal/controller/state/graph/authentication_filter.go:107-150](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L107-L150) —— 116-119 行（JWT）与 137-140 行（OIDC）在 `!isPlus` 时立即返回 `Valid=false`，错误信息明确写「requires NGINX Plus」。`isPlus` 来自 controller 启动时的 `--nginx-plus` flag（u2-l2），一路透传到图构建。

**③ Secret 解析：Basic 与 JWT-File 走同一个函数**

Basic 与 JWT-File 都要解析一个装认证数据的 Secret，复用 `resolveAuthenticationFilterSecret`，它要求 Secret 含 `auth` 键（`secrets.AuthKey`）：

[internal/controller/state/graph/authentication_filter.go:260-287](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L260-L287) —— 注意 284-286 行的 FIXME：还有一个向下兼容的 htpasswd 类型 Secret 检测（`resolveHtPasswdSecret`），命中会给一个「Accepted 但已废弃」的告警条件。

OIDC 的 Secret 解析更细：client secret（`client-secret` 键）、可选 CA（`ca.crt`）、可选 CRL（`ca.crl`）各自校验：

[internal/controller/state/graph/authentication_filter.go:396-444](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L396-L444)。

**④ 转成渲染模型**

`convertAuthenticationFilter` 按 type 分发；**无效过滤器直接返回空结构**（graph 阶段已回写条件）：

[internal/controller/state/dataplane/convert.go:198-219](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L198-L219) —— 205 行 `!filter.Valid` 提前返回，避免把半截配置喂给模板。

Basic 转换从 `referencedSecrets`（图阶段已校验、handler 阶段已收集的 Secret 快照）取出 htpasswd 字节：

[internal/controller/state/dataplane/convert.go:221-243](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L221-L243)。

**⑤ 模板渲染：三条认证指令**

`servers_template.go` 在 location 块里按字段是否存在分别渲染三类指令：

- Basic（OSS 可用）：[servers_template.go:171-174](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L171-L174) —— `auth_basic "<realm>"; auth_basic_user_file <file>;`
- OIDC（Plus）：[servers_template.go:176-189](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L176-L189) —— `auth_oidc <provider>;` 加可选 `auth_jwt_require` 与 `proxy_set_header`。
- JWT（Plus）：[servers_template.go:191-212](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L191-L212) —— `auth_jwt "<realm>";` 再按 File/Remote 渲染 `auth_jwt_key_file` 或 `auth_jwt_key_request`。

**⑥ OIDC 绑定后二次校验：HTTPS 与 URI 冲突**

`validateOIDCFilters` 在路由绑定 Listener 之后跑一遍，先把挂到非 HTTPS 监听器的 OIDC 过滤器判 Invalid（OIDC 回调依赖 HTTPS）：

[internal/controller/state/graph/authentication_filter.go:485-494](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L485-L494)。

`checkOIDCURIConflictsForHostname` 用 `claimedPaths` map 检测同一 hostname 上 logout/frontChannel/redirect URI 的冲突（三类 URI 共享同一个 location 路径空间），先到先得，冲突者判 Invalid：

[internal/controller/state/graph/authentication_filter.go:661-706](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L661-L706)。

#### 4.2.4 代码实践

> 目标：部署 `examples/basic-authentication`，给 `/coffee` 启用 Basic 认证，验证未带凭据返回 401、带正确凭据返回 200。Basic 是 OSS 可用项，最适合做入门实践。

**操作步骤（待本地验证）**

1. 部署 NGF（默认 OSS 即可，无需 Plus、无需 `--snippets`）。
2. 部署示例：

   ```bash
   kubectl apply -f examples/basic-authentication/cafe.yaml          # 后端
   kubectl apply -f examples/basic-authentication/gateway.yaml
   kubectl apply -f examples/basic-authentication/basic-auth.yaml   # Secret + AuthenticationFilter
   kubectl apply -f examples/basic-authentication/cafe-routes.yaml  # /coffee 引用了 basic-auth
   ```

3. 取 `GW_IP`（同 u1-l5）。

**需要观察的现象**

- AuthenticationFilter 状态 `Accepted=True`：
  `kubectl get authenticationfilter basic-auth -o jsonpath='{.status}'`。
- `/coffee` 不带凭据 → 401 Unauthorized，响应头含 `WWW-Authenticate: Basic realm="Restricted basic-auth"`。
- `/coffee` 带正确凭据（示例 Secret 里是 `user1/password1`）→ 200：
  `curl -s -o /dev/null -w '%{http_code}\n' -u user1:password1 --resolve cafe.example.com:80:$GW_IP http://cafe.example.com/coffee`
- `/tea`（未引用认证过滤器）→ 不需要凭据，直接 200。

**预期结果**

- 带 `user1:password1`：200。
- 带错误密码：401。
- 不带 `Authorization` 头：401。
- `/tea` 不受影响。

> 若想实践 JWT/OIDC，需要 NGINX Plus 镜像（参考 `examples/helm/nginx-plus`），并在 OSS 集群上验证它们会被判 `Accepted=False`、reason 提示「requires NGINX Plus」。

#### 4.2.5 小练习与答案

**练习 1**：在 OSS（非 Plus）模式下创建一个 `type: JWT` 的 AuthenticationFilter 并被路由引用，它的状态会是什么？

**答案**：`Accepted=False`，condition message 为「JWT Authentication requires NGINX Plus.」（`authentication_filter.go:117`）。路由侧也会因为引用了无效过滤器而得到 `ResolvedRefs` 相关条件。

**练习 2**：为什么 OIDC 过滤器挂到 HTTP（非 HTTPS）监听器上会被判无效？

**答案**：OIDC 的授权码流程依赖浏览器重定向与 cookie 传递会话，必须走 HTTPS；`validateOIDCFilters` → `hasNonHTTPSAttachment`（`authentication_filter.go:509`）检测到非 HTTPS 绑定就把过滤器判 Invalid，并经 `propagateInvalidOIDCFiltersToRouteRules` 把对应路由规则也标记为 filter 无效，避免数据面静默跳过认证。

**练习 3**：JWT 的 File 与 Remote 两种 key 来源，在生成的 NGINX 指令上有什么区别？

**答案**：File 来源渲染 `auth_jwt_key_file <file>;`（从 Secret 取 JWKS 落盘本地），Remote 来源渲染 `auth_jwt_key_request <internal-path>;`（NGINX 运行时向远程 JWKS URI 拉取，见模板 [servers_template.go:193-197](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L193-L197)）。两者互斥，由 CRD 的 CEL 强制。

---

### 4.3 安全约束与 Plus 依赖

#### 4.3.1 概念说明

SnippetsFilter 与 AuthenticationFilter 都是「把外部输入变成 NGINX 指令」的通道，但二者对**安全性**的处理方式截然相反，这是本讲最重要的对比：

- **SnippetsFilter = 信任作者，原样透传**。它只做结构校验（context 合法、每上下文一段、value 非空），**完全不校验 value 内容**。因为它的设计目标就是「注入任意 NGINX 指令」，任何内容过滤都会削弱其能力。代价是：谁能创建 SnippetsFilter，谁就能往 `nginx.conf` 里写任何东西（包括 `alias` 越权读文件、`proxy_pass` 到内网、关掉安全指令等）。因此它必须**显式开关**，并且通常配合 RBAC 严格控制创建权限。

- **AuthenticationFilter = 零信任，逐字段正则校验**。它的字段值最终会拼进 NGINX 指令字符串（例如 claim value 会进 `map` 的匹配值、OIDC issuer 会进 `oidc_provider`）。如果不校验，攻击者可以用 `;` 或 `$` 注入额外指令或变量展开。因此 NGF 对几乎所有会进入指令的字符串字段都套了正则白名单。

这个对比可以用一张表概括：

| 维度 | SnippetsFilter | AuthenticationFilter |
|------|----------------|----------------------|
| 内容校验 | 无（仅结构） | 逐字段正则白名单 |
| 默认开关 | 默认**关**（`--snippets`） | 默认**开**（CRD 注册即用） |
| Plus 依赖 | 无 | JWT/OIDC 需 Plus，Basic 需 OSS |
| 风险等级 | 高（任意指令注入） | 低（输入受限） |
| 失败兜底 | `nginx -t` 失败 → 不 reload | 校验失败 → `Valid=false`，不生成指令 |

#### 4.3.2 核心流程

**SnippetsFilter 的安全模型**：

```
用户 value ──(无内容校验)──> 写成 includes/<Name>.conf ──> include 进 nginx.conf
                                  │
                                  ▼
                       Agent 执行 nginx -t（reload 前）
                                  │
                       ┌──────────┴──────────┐
                     通过                    失败
                       │                       │
                  apply 配置          保持旧配置 + 回写错误状态
```

即「**合法性由 NGINX 自己兜底**」。NGF 不懂 NGINX 语法，所以把校验职责外包给 `nginx -t`。这与你将在 u13-l1 看到的「fail-closed」思想一致：宁可不更新配置，也不写入可能崩溃的配置。

**AuthenticationFilter 的安全模型**：

```
claim value / issuer / URL ──> 正则白名单 ──> 通过才进指令
                                  │
                       ┌──────────┴──────────┐
                     通过                    失败
                       │                       │
                生成指令               Valid=false + Accepted=False
```

#### 4.3.3 源码精读

**① SnippetsFilter 的「无内容校验」与显式开关**

再强调一次，`validateSnippetsFilter`（4.1.3 ③）对 `snippet.Value` 不做任何正则或词法检查。开关在 controller 命令行：

[cmd/gateway/commands.go:570-577](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L570-L577) —— `--snippets` 默认 `false`。其上方的 `--snippets-filter`（[commands.go:560-568](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L560-L568)）是旧开关，已标记 deprecated，新开关 `--snippets` 同时启用 SnippetsFilter 与 SnippetsPolicy。

> 安全含义：在多租户集群里，若放开 SnippetsFilter 的 RBAC 创建权限且开启了 `--snippets`，任何能创建该 CRD 的用户都能影响数据面 NGINX 的全局行为。生产部署应仅授予可信运维，这也是它默认关闭的原因。

**② AuthenticationFilter 的 claim 值正则：防指令注入**

claim value 最终会进入 NGINX `map` 指令的匹配值里，所以必须禁止 `;`（指令分隔）、`#`（注释）、`$`（变量展开）、`{}`（块）等危险字符：

[internal/controller/nginx/config/validation/auth_fields.go:151-156](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/validation/auth_fields.go#L151-L156) —— `authZSafeValueFmt` 是一个「取反」正则，只放行不含这些字符的值。

claim name 也有限制（字母/数字/下划线/横线/斜杠）：

[internal/controller/nginx/config/validation/auth_fields.go:144-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/validation/auth_fields.go#L144-L149) —— `authZSafeNameFmt`。

OIDC 的 issuer/configURL 强制 HTTPS 且禁 `;`/`$`：

[internal/controller/nginx/config/validation/auth_fields.go:22-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/validation/auth_fields.go#L22-L28) —— `oidcHTTPSURLFmt`。

**③ claim name 清洗后的碰撞检测：防「静默误授权」**

这是一个隐蔽但极重要的安全点。NGINX 变量名只允许 `[a-zA-Z0-9_]`，所以 claim name 里的 `-`/`.`/`/` 会被替换成 `_`。这导致 `realm_access/roles` 与 `realm_access_roles` 两个不同的 claim 会**映射到同一个 NGINX 变量**，进而导致授权判断错乱（可能把本该拒绝的请求放行）。`validateJWTAuthorization` 主动检测这种碰撞：

[internal/controller/state/graph/authentication_filter.go:181-250](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L181-L250) —— 用 `globalSanitizedNames` map 记录每个清洗后的变量名，发现两个不同 claim name 清洗后相同就报错。清洗函数 `sanitizeClaimNameForVariable` 与数据面的 `sanitizeVariablePrefix` 保持同源：

[internal/controller/state/graph/authentication_filter.go:256-258](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L256-L258) —— 把 `-`/`.`/`/` 统一替换为 `_`。

> 这类「校验侧与渲染侧必须同源」的约束，与 u3-l2 讲过的「cache transform 保留键集合与业务读取键常量同源」是同一类正确性前提。

**④ Plus 依赖的两处体现**

- 图阶段硬门控：`validateAuthenticationFilter` 里 JWT/OIDC 的 `isPlus` 检查（4.2.3 ②），OSS 上直接 `Valid=false`。
- 模板渲染：`auth_jwt`/`auth_oidc` 指令只有 Plus 镜像里存在，OSS 镜像即便收到配置也会在 `nginx -t` 阶段失败。两道闸门叠加，确保 OSS 用户不会误用。

#### 4.3.4 代码实践

> 目标：亲手对比两种扩展对「危险输入」的处理差异——一个被原样接受，一个被严格拒绝。

**操作步骤（源码阅读型 + 待本地验证）**

1. **SnippetsFilter 侧**：创建一个内容含 `;` 与变量展开的片段，例如：

   ```yaml
   apiVersion: gateway.nginx.org/v1alpha1
   kind: SnippetsFilter
   metadata: { name: risky-sf }
   spec:
     snippets:
       - context: http.server.location
         value: |
           set $x $arg_y; # 注释；多写一条指令也行
           rewrite ^ /internal last;
   ```

   先用 `kubectl apply --dry-run=server` 看是否被 API server 接受（结构合法就会被接受），再观察其 `Accepted` 状态与数据面 `nginx -t` 结果。

2. **AuthenticationFilter 侧**：构造一个 claim value 含 `;` 或 `$` 的 JWT Authorization：

   ```yaml
   spec:
     type: JWT
     jwt:
       realm: test
       source: File
       file: { secretRef: { name: jwks } }
       authorization:
         require: Any
         rules:
           - claims:
               - name: role
                 values: ["admin; auth_jwt off"]   # 试图注入
   ```

   观察 API server 或 NGF 是否拒绝。

**需要观察的现象**

- SnippetsFilter：结构合法即被 API server 接受；`Accepted=True`；是否真正生效取决于 `nginx -t`（合法则 reload 生效，语法错则保持旧配置并回写错误）。
- AuthenticationFilter：claim value `admin; auth_jwt off` 命中 `authZSafeValueFmt` 的禁用字符，被 `validateJWTAuthorization`（`authentication_filter.go:227`）判为 `Valid=false`，`Accepted=False`，message 指明含非法字符。

**预期结果**

- SnippetsFilter：内容**不被 NGF 过滤**，证明它是「信任作者」模型。
- AuthenticationFilter：危险值**被正则挡在门外**，证明它是「零信任」模型。

> 结论：把任意用户输入交给 SnippetsFilter 时务必谨慎；在需要暴露给不可信用户的场景，优先用 AuthenticationFilter 这类「输入受限」的扩展。

#### 4.3.5 小练习与答案

**练习 1**：为什么 SnippetsFilter 默认关闭，而 AuthenticationFilter 默认开放？

**答案**：因为 SnippetsFilter 能注入**任意** NGINX 指令，等价于对数据面拥有近乎完全的控制权，属于高风险能力，必须由运维显式 `--snippets` 开启并配合严格 RBAC。AuthenticationFilter 的输入受正则白名单约束、且只生成固定的几条认证指令，风险可控，故默认开放。

**练习 2**：假设 NGF 没有做 claim name 清洗碰撞检测（即删掉 `globalSanitizedNames` 那段逻辑），可能出现什么安全后果？

**答案**：两个清洗后相同的 claim name（如 `a/b` 与 `a_b`）会映射到同一个 NGINX 变量，导致后写的覆盖先写的，授权 `map` 的判定结果可能背离用户意图——本该只允许 `role=admin` 的策略可能因为变量碰撞而放行 `role=user`，造成**越权访问**。这正是为什么校验要主动探测这种碰撞。

**练习 3**：OIDC 的 issuer 字段为什么强制 HTTPS 且禁止 `;`/`$`？

**答案**：issuer 会进入 `oidc_provider` 指令。`$` 会触发 NGINX 变量展开，`;` 会截断指令、注入新指令（指令注入攻击）；强制 HTTPS 则保证与 IdP 的通信加密。`oidcHTTPSURLFmt`（`auth_fields.go:26`）同时覆盖这两点。

---

## 5. 综合实践

> **任务**：为一个已有的 HTTPRoute 同时配置「Basic 认证」与「自定义速率限制片段」，把它们串联起来，并解释二者在 NGF 内部的处理差异。

**背景**：你有一个 `cafe.example.com/coffee` 路由。需求：

1. 只有提供正确用户名密码的请求才能访问（用 AuthenticationFilter Basic）。
2. 即便认证通过，也要对 `/coffee` 做 IP 级别的连接数限制（用 SnippetsFilter 注入 `limit_conn`）。

**步骤（待本地验证）**

1. 开启 snippets 并部署 NGF：`-f examples/helm/snippets/values.yaml`。
2. 部署 `examples/basic-authentication` 的 Secret + AuthenticationFilter（basic-auth）。
3. 新建一个 SnippetsFilter，向 `http.server.location` 注入速率限制：

   ```yaml
   apiVersion: gateway.nginx.org/v1alpha1
   kind: SnippetsFilter
   metadata: { name: limit-conn-sf }
   spec:
     snippets:
       - context: http.server.location
         value: |
           limit_conn_zone $binary_remote_addr zone=addr:10m;
           limit_conn addr 1;
   ```

   （注意：`limit_conn_zone` 实际属于 http 上下文，这里仅为练习示意；正确做法是把 `limit_conn_zone` 放 `http` 上下文、`limit_conn` 放 location 上下文，分两个 snippet。请据此自行修正。）

4. 修改 HTTPRoute，让 `/coffee` 规则的 `filters` 同时引用两个 ExtensionRef：

   ```yaml
   filters:
     - type: ExtensionRef
       extensionRef: { group: gateway.nginx.org, kind: AuthenticationFilter, name: basic-auth }
     - type: ExtensionRef
       extensionRef: { group: gateway.nginx.org, kind: SnippetsFilter, name: limit-conn-sf }
   ```

5. 验证：
   - 不带凭据 → 401（认证先拦截）。
   - 带凭据但并发多个连接 → 部分 503（`limit_conn` 生效）。
   - 在数据面 Pod 内 `cat` 生成的 location 配置，确认 `auth_basic` 与 `include .../SnippetsFilter_....conf` 同时出现，顺序为 include 在前、auth_basic 在后（对应模板 [servers_template.go:167-174](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers_template.go#L167-L174)）。

**思考题**：两个过滤器都通过 `ExtensionRefFilter` 容器（`extension_ref_filter.go:13`）承载，但它们的校验严格度天差地别。请用本讲 4.3 的对比表向团队说明：为什么把「速率限制」做成 SnippetsFilter 可以接受，而把「认证」也做成 SnippetsFilter（让用户自己写 `auth_basic` 片段）是个坏主意。

> 参考方向：认证涉及 Secret 数据解析、htpasswd 文件落盘、与 NGINX 指令的精确拼接，这些用结构化 CRD + 模板（AuthenticationFilter）才能安全可预测地完成；而速率限制等「纯指令」需求，SnippetsFilter 的原样透传反而最灵活。选择哪种扩展机制，本质上是在「灵活性」与「安全性/可校验性」之间做取舍。

## 6. 本讲小结

- SnippetsFilter 与 AuthenticationFilter 都是**挂在 HTTPRoute/GRPCRoute 的 `rules[].filters` 上的 ExtensionRef 过滤器**，由 `ExtensionRefFilter` 容器（`extension_ref_filter.go`）统一承载，**不是** u8-l2 那种带 `targetRefs` 的策略 CRD。
- SnippetsFilter 把任意 NGINX 指令注入 `main`/`http`/`http.server`/`http.server.location` 四个上下文；main/http 片段在 Gateway 维度收集（`buildSnippetsForContext`），server/location 片段在路由规则维度收集，最终都变成 `includes/<Name>.conf` 并由 `include` 指令拉入。
- AuthenticationFilter 支持 Basic（OSS）/ JWT / OIDC（后两者需 Plus），分别渲染 `auth_basic`、`auth_jwt`、`auth_oidc` 指令；Plus 依赖在图阶段用 `isPlus` 硬门控，OIDC 还在绑定后做 HTTPS 与 URI 冲突的二次校验。
- **安全模型相反**：SnippetsFilter 只做结构校验、内容原样透传、默认 `--snippets` 关闭（信任作者 + 显式开关）；AuthenticationFilter 对几乎所有进指令的字段做正则白名单（零信任），还主动检测 claim name 清洗碰撞以防越权。
- 两者都沿用图节点三元组范式（Source + Valid + Conditions），无效者仍进图并回写 `Accepted=False`，由 `Valid` 控制是否参与配置生成，符合 u5-l1 的整体设计。

## 7. 下一步学习建议

- **继续 u12 单元**：u12-l3（TLS 安全）与本讲互为补充——认证管「你是谁」，TLS 管「传输可信」，二者经常组合使用。
- **回到 u8-l3 对照**：现在重读 u8-l3 的策略生成框架，你会更清楚 SnippetsPolicy（走 `Generator.GenerateForServer/Location`）与 SnippetsFilter（走 ExtensionRef → include）是两条不同的注入链路，但最终都落到 `includes/` 目录的 `.conf` 文件。
- **u13-l1（测试体系）**：本讲的两个过滤器都有完整的图测试（`authentication_filter_test.go`、`snippets_filter_test.go`）与配置生成测试，是学习「如何为校验逻辑和模板渲染写单测」的好素材。
- **延伸阅读**：仓库内 `docs/proposals/advanced-nginx-extensions.md` 记录了 SnippetsFilter/SnippetsPolicy 的设计动机与安全考量，`docs/proposals/nginx-extensions.md` 含认证过滤器的设计讨论，适合想深入了解取舍的读者。
