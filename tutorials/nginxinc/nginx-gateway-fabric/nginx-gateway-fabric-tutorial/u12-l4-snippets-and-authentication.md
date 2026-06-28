# SnippetsFilter 与认证过滤器

## 1. 本讲目标

本讲聚焦 NGF 的两条「逃逸舱（escape hatch）」扩展机制——`SnippetsFilter` 与 `AuthenticationFilter`。学完本讲，你应当能够：

- 说清 `SnippetsFilter` 允许注入的四个 NGINX 上下文，以及它在「图→数据面→配置生成」三阶段如何被一步步翻译成 `include` 文件。
- 区分 `AuthenticationFilter` 的三种认证类型（Basic / JWT / OIDC），理解各自的字段、对 NGINX Plus 的依赖，以及它们最终落到哪些 NGINX 指令。
- 解释这两类过滤器在 ExtensionRef 绑定、特性开关（feature flag）、Plus 依赖与安全校验上的约束。
- 在一个 HTTPRoute 上通过 `SnippetsFilter` 注入一条自定义 NGINX 指令并验证生效。

## 2. 前置知识

在进入本讲前，你需要熟悉以下概念（它们都来自前置讲义）：

- **Gateway API 的 ExtensionRef 过滤器**。HTTPRoute/GRPCRoute 的 `spec.rules[].filters[]` 里，`type: ExtensionRef` 表示「把这块行为交给某个 CRD 去定义」。`SnippetsFilter` 与 `AuthenticationFilter` 都是 NGF 自定义的 ExtensionRef 目标。回顾 u5-l2 路由处理可加深理解。
- **Source + Valid + Conditions 节点三元组范式**。图里每个资源节点都带这三件套：`Source` 是原始 K8s 对象，`Valid` 是布尔位决定它能否参与配置生成，`Conditions` 是要回写进 status 的结论。回顾 u5-l1。
- **dataplane.Configuration 这层渲染模型**。图节点是按 K8s 资源类型切分的，而 `dataplane.Configuration` 是按 NGINX 配置块切分的中间表示，配置生成器只消费后者。回顾 u5-l4。
- **include 机制**。NGINX 的 `nginx.conf` 是静态骨架，可变内容靠 `include` 通配拉入；snippet/policy/auth 的产物是独立 `.conf` 文件，骨架只渲染一行 `include <Name>;`。回顾 u6-l2。
- **Plus 与 OSS 的能力差异**。JWT/OIDC 认证依赖 NGINX Plus 的 `ngx_http_auth_jwt_module` / `ngx_http_oidc_module`，OSS 不可用；Basic Auth 用的是 OSS 自带的 `ngx_http_auth_basic_module`。回顾 u12-l2。

两个术语先打个预防针：

- **ExtensionRefFilter**：NGF 图层里对「ExtensionRef 指向的过滤器」的统一包装结构，目前能装 `SnippetsFilter` 或 `AuthenticationFilter` 两种。
- **Snippet 的「上下文（context）」**：指 NGINX 配置文件里指令所处的层级（main / http / http.server / http.server.location），不同上下文允许的指令集合不同，把指令放错上下文 NGINX 会拒绝加载。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `apis/v1alpha1/snippetsfilter_types.go` | `SnippetsFilter` CRD 的类型定义、四个允许上下文枚举、CEL 校验。 |
| `apis/v1alpha1/authenticationfilter_types.go` | `AuthenticationFilter` CRD 的类型定义，Basic/JWT/OIDC 三套 spec 与互斥 CEL 校验。 |
| `internal/controller/state/graph/snippets_filter.go` | 图层处理 `SnippetsFilter`：校验、构建 `Snippets map`、解析 ExtensionRef。 |
| `internal/controller/state/graph/authentication_filter.go` | 图层处理 `AuthenticationFilter`：按 Type 分派校验、Plus 门控、Secret 解析、OIDC 绑定后校验。 |
| `internal/controller/state/graph/extension_ref_filter.go` | `ExtensionRefFilter` 包装结构、ExtensionRef 的 Group/Kind 合法性校验、按 Kind 构建解析器。 |
| `internal/controller/state/dataplane/convert.go` | 把图的 `SnippetsFilter`/`AuthenticationFilter` 翻译成 dataplane 渲染模型。 |
| `internal/controller/nginx/config/servers.go` | 在 location 上挂载 snippets include、Basic/JWT/OIDC 认证指令。 |
| `internal/controller/nginx/config/includes.go` | 把 snippet 转成 include 文件并按文件名去重。 |
| `internal/controller/config/config.go` | `SnippetsFilters` 与 `Snippets` 两个特性开关定义。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**4.1 SnippetsFilter 的允许上下文与注入方式**、**4.2 AuthenticationFilter 的三种认证类型**、**4.3 安全约束与 Plus 依赖**。

### 4.1 SnippetsFilter：允许的上下文与注入方式

#### 4.1.1 概念说明

`SnippetsFilter` 是一条「直接往生成的 NGINX 配置里塞原始指令」的逃逸舱。当 Gateway API 的标准过滤器（如 RequestHeaderModifier、URLRewrite）或 NGF 策略 CRD 表达不了某个 NGINX 原生能力时，你可以用一段原始 NGINX 配置补上——例如 `limit_except`、`limit_conn`、自定义日志格式等。

它的设计有三条硬约束：

1. **只能在 HTTPRoute/GRPCRoute 的 ExtensionRef 里引用**，不是策略附着（没有 `targetRef`），而是「路由规则级」的过滤器。
2. **每个 SnippetsFilter 最多 4 条 snippet，每个 NGINX 上下文最多一条**，避免同一上下文里多条片段互相覆盖、顺序不可控。
3. **只允许四个上下文**：`main`、`http`、`http.server`、`http.server.location`，对应 NGINX 配置的四个层级。

#### 4.1.2 核心流程

一个 `SnippetsFilter` 从 YAML 到生效，经过「引用绑定 → 图校验 → dataplane 转换 → 配置生成」四步：

```text
HTTPRoute.rules[].filters[].extensionRef.name = "my-sf"
        │ (按 LocalObjectReference 在同 namespace 查找)
        ▼
graph.getSnippetsFilterResolverForNamespace  → ExtensionRefFilter{SnippetsFilter, Valid}
        │
        ▼
graph.processSnippetsFilters → validateSnippetsFilter
        │   (非空、value 非空、上下文合法、每上下文唯一)
        ▼
graph.SnippetsFilter{Snippets: map[context]string, Valid, Referenced}
        │
        ├── main/http 上下文 ──▶ Configuration.MainSnippets / BaseHTTPConfig.Snippets
        │                       (Gateway 级聚合，buildSnippetsForContext)
        │
        └── http.server/http.server.location 上下文 ──▶ dataplane.SnippetsFilter
                                                        {ServerSnippet, LocationSnippet}
        │
        ▼
config.includes.go: createIncludeFromSnippet → include/<Name>.conf (按 Name 去重)
config.servers.go:  在 location/server 上挂一行 include <Name>;
```

关键点：**main 与 http 这两个上下文是「全局」的**，会跨所有 server 生效，因此被单独收进 `Configuration` 顶层；**http.server 与 http.server.location 是「局部」的**，跟随引用它的路由规则挂到具体 server/location 上。

#### 4.1.3 源码精读

**类型定义与四上下文枚举**。`SnippetsFilterSpec.Snippets` 字段挂了三条 kubebuilder 校验：至少 1 条、至多 4 条、且用 CEL 表达式强制「每个上下文唯一」：

[apis/v1alpha1/snippetsfilter_types.go:38-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetsfilter_types.go#L38-L47) — `Snippets` 字段的 `MinItems=1`、`MaxItems=4` 与 `XValidation`「Only one snippet allowed per context」。这段 CEL 规则 `self.all(s1, self.exists_one(s2, s1.context == s2.context))` 在 API server 准入层就被强制，重复上下文的对象直接被拒绝创建。

四上下文的合法取值由枚举类型钉死：

[apis/v1alpha1/snippetsfilter_types.go:61-79](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/snippetsfilter_types.go#L61-L79) — `NginxContext` 枚举，取值严格限定为 `main`/`http`/`http.server`/`http.server.location`。

**图层校验**。即使 CEL 在准入层挡了一遍，图层的 `validateSnippetsFilter` 仍兜底再校验一遍（因为 NGF 不假设准入一定开启），重点在「上下文合法」与「每上下文唯一」：

[internal/controller/state/graph/snippets_filter.go:120-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L120-L149) — `switch snippet.Context` 只放行四个合法上下文，其余记 `field.NotSupported`；用 `usedContexts` map 检测重复上下文。校验失败时返回一个 `NewSnippetsFilterInvalid` 条件，节点 `Valid=false`。

**解析 ExtensionRef 引用**。HTTPRoute 的 ExtensionRef 经解析器查到同 namespace 的 `SnippetsFilter` 并标记 `Referenced=true`：

[internal/controller/state/graph/snippets_filter.go:30-52](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L30-L52) — `getSnippetsFilterResolverForNamespace` 先校验 `ref.Group == gateway.nginx.org` 且 `ref.Kind == SnippetsFilter`，再按 namespace/name 查表，命中即置位 `Referenced`。`Referenced` 后续决定 main/http snippet 是否落盘（见 `buildSnippetsForContext`）。

**dataplane 转换**。`convertSnippetsFilter` 只挑出 `http.server` 与 `http.server.location` 两个上下文塞进 `dataplane.SnippetsFilter`：

[internal/controller/state/dataplane/convert.go:175-196](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L175-L196) — 分别取出 ServerSnippet 与 LocationSnippet，用 `createSnippetName` 生成唯一文件名。

main/http 上下文不走这里，而是走 `buildSnippetsForContext`，在 Gateway 级聚合：

[internal/controller/state/dataplane/configuration.go:2357-2385](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2357-L2385) — 只把 `Valid && Referenced` 的过滤器里对应上下文的 snippet 收集起来；文件名由 `createSnippetName` 统一生成。

**配置生成**。location 片段在 `updateLocation` 里被转成 include 挂到 location 上：

[internal/controller/nginx/config/servers.go:1215](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1215) — `location.Includes = append(..., createIncludesFromLocationSnippetsFilters(filters.SnippetsFilters)...)`。

[internal/controller/nginx/config/includes.go:153-167](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L153-L167) — `createIncludesFromLocationSnippetsFilters` 把每个 `LocationSnippet` 包成 `include/<name>.conf`，并按文件名去重。同一个 `SnippetsFilter` 被多条路由引用时只生成一份文件——这正是 include 机制的核心收益。

#### 4.1.4 代码实践

**实践目标**：复现 `examples/snippets` 的最小用法，为一个 HTTPRoute 注入 `limit_except` 指令，只允许 GET 方法访问 `/coffee`。

**操作步骤**（待本地验证；前提是已按 u1-l4 用开启 snippets 的部署变体装好 NGF）：

1. 部署示例后端与网关：`kubectl apply -f examples/snippets/app.yaml -f examples/snippets/gateway.yaml`。
2. 创建 `SnippetsFilter`：`kubectl apply -f examples/snippets/limit-except-sf.yaml`。该过滤器只声明了一段 `http.server.location` 上下文的 `limit_except GET { deny all; }`，尚未与任何路由关联。
3. 部署引用它的 HTTPRoute：`kubectl apply -f examples/snippets/httproutes.yaml`，其中 `/coffee` 规则的 `filters` 用 `ExtensionRef` 指向 `kind: SnippetsFilter, name: limit-except-sf`。
4. 取网关地址 `GW_IP`，分别用 GET 与 POST 请求 `/coffee`。

**需要观察的现象**：

- `kubectl get snippetsfilter limit-except-sf` 的 status `Accepted` 条件为 `True`。
- GET `/coffee` 返回 200（被转发到后端）。
- POST `/coffee` 返回 403（`limit_except` 直接由 NGINX 拒绝，未到达后端）。

**预期结果**：POST 被 NGINX 在 location 层用 403 拦截，证明 snippet 已被注入到生成的 location 配置中。若返回 405 或 503，多半是 snippet 未生效或过滤器 `Accepted=False`，应回查 status。

> 注意：`SnippetsFilter` 需要开启特性开关。`SnippetsFilters`（仅 SnippetsFilter）或更宽的 `Snippets`（同时含 SnippetsPolicy）二选一即可，见 4.3.3。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 `SnippetsFilter` 同时声明了两条 `context: http.server.location` 的 snippet，会发生什么？
**答案**：API server 的 CEL 校验（`exists_one`）会在创建/更新时直接拒绝该对象；即便跳过准入，图层 `validateSnippetsFilter` 的 `usedContexts` 检测也会把它判为 `Valid=false` 并回写 `Accepted=False/Invalid`。

**练习 2**：为什么 main/http 上下文的 snippet 不像 server/location 那样挂在具体 location 上？
**答案**：main/http 是进程级与 http 块级的全局指令（如 `limit_conn_zone`、`worker_*`），不属于任何具体 server/location；它们被 `buildSnippetsForContext` 聚合到 `Configuration.MainSnippets` 与 `BaseHTTPConfig.Snippets`，由骨架的 `include` 在对应上下文一次性拉入。

---

### 4.2 AuthenticationFilter：三种认证类型

#### 4.2.1 概念说明

`AuthenticationFilter` 把「请求认证」做成一个 ExtensionRef 过滤器。它的 `spec.type` 是一个三值枚举——`Basic` / `OIDC` / `JWT`，决定了请求必须通过哪种身份核验才能到达后端：

| 类型 | 底层 NGINX 模块 | 需要 Plus | 关键字段 |
| --- | --- | --- | --- |
| Basic | `ngx_http_auth_basic_module`（OSS 自带） | 否 | `secretRef`（含 `auth` 键的 htpasswd 数据）+ `realm` |
| JWT | `ngx_http_auth_jwt_module` | **是** | `source: File\|Remote`、可选 `authorization` claim 规则 |
| OIDC | `ngx_http_oidc_module` | **是** | `issuer`/`clientID`/`clientSecretRef`、可选 `logout`/`session`/`authorization` |

三种类型互斥：CRD 用 CEL 规则强制「指定了哪个 `type`，就只能填对应的 spec 子结构」，杜绝 Basic 与 OIDC 字段混填。

#### 4.2.2 核心流程

```text
HTTPRoute.rules[].filters[].extensionRef.name = "basic-auth"  (kind: AuthenticationFilter)
        │
        ▼
graph.getAuthenticationFilterResolverForNamespace → ExtensionRefFilter{AuthenticationFilter, Valid}
        │
        ▼
graph.processAuthenticationFilters → validateAuthenticationFilter
        │   switch spec.type:
        │     Basic ──▶ 解析 secretRef (含 "auth" 键)
        │     JWT   ──▶ 先判 isPlus；再按 File/Remote 解析；可选 claim 校验
        │     OIDC  ──▶ 先判 isPlus；校验 issuer/URL/secretRef/CA/CRL；URI 冲突检测在绑定后做
        │
        ▼
graph.AuthenticationFilter{Source, Valid, Conditions, Referenced}
        │
        ▼
dataplane.convertAuthenticationFilter → 按 Type 产出 Basic / JWT / OIDC 渲染字段
        │
        ▼
config.servers.go: updateLocationAuthenticationFilter
        ├── Basic ──▶ location.AuthBasic{Realm, File}  (htpasswd 文件单独下发)
        ├── JWT   ──▶ location.AuthJWT{Realm, KeyCache, Leeway, Remote/File, AuthZ}
        └── OIDC  ──▶ location.AuthOIDC{ProviderName, AuthZ}  + 独立 callback/logout location
```

OIDC 比 Basic/JWT 复杂得多：它不只是「在 location 上加一条指令」，还要为每个 provider 额外生成 callback、logout、front-channel logout 等独立 location，且这些 location 共享 NGINX 的路径空间，因此绑定到具体 hostname 后还要做 URI 冲突检测。

#### 4.2.3 源码精读

**类型与互斥 CEL**。`AuthenticationFilterSpec` 头部挂了一长串 `XValidation`，确保 `type` 与对应字段严格配对：

[apis/v1alpha1/authenticationfilter_types.go:38-67](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L38-L67) — 例如 `type Basic requires spec.basic to be set`、`type OIDC must not set spec.jwt`。这些规则让「错填字段」在准入层就失败，而不是等到图层才发现。`AuthType` 枚举见 [apis/v1alpha1/authenticationfilter_types.go:71-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/authenticationfilter_types.go#L71-L81)。

**图层分派校验**。`validateAuthenticationFilter` 用 `switch af.Spec.Type` 把三种类型分到不同校验路径：

[internal/controller/state/graph/authentication_filter.go:107-150](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L107-L150) — JWT 与 OIDC 分支第一件事就是判 `!isPlus`，不满足立刻返回 `Valid=false` 并附条件「JWT/OIDC Authentication requires NGINX Plus.」；Basic 分支则解析 secretRef。

**Plus 门控**（JWT 侧示例）：

[internal/controller/state/graph/authentication_filter.go:115-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L115-L119) — `if !isPlus { ... "JWT Authentication requires NGINX Plus." }`。OIDC 侧对称处理见 [authentication_filter.go:136-140](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L136-L140)。

**Secret 解析与 htpasswd 弃用提示**。Basic 认证引用的 Secret 必须含 `auth` 键（`secrets.AuthKey`），`resolveAuthenticationFilterSecret` 先解析该键，再调 `resolveHtPasswdSecret` 对老式 `type: nginx.org/htpasswd` 的 Secret 发「已弃用」的 Accepted-with-message 条件：

[internal/controller/state/graph/authentication_filter.go:260-318](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L260-L318) — 注意 `Valid` 仍为 `true`（只是告警），保证老 Secret 在过渡期仍可用。

**OIDC 绑定后校验**。OIDC 的两条强约束无法在过滤器自身校验，必须等它被绑到路由、知道挂到哪些 hostname 后才能判，因此单独放在 `validateOIDCFilters`：

[internal/controller/state/graph/authentication_filter.go:485-494](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L485-L494) — 一是对非 HTTPS 监听器上的 OIDC 直接判无效（OIDC 回调必须走 HTTPS，见 `collectOIDCFilterInfo` 中 `hasNonHTTPSAttachment`）；二是对同一 hostname 上 logout / front-channel logout / path-only redirect URI 做冲突检测（三者共享同一 NGINX 路径空间）。

**dataplane 转换**。`convertAuthenticationFilter` 同样按 Type 分派，并明确「无效过滤器不转换，只留条件」：

[internal/controller/state/dataplane/convert.go:198-219](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/convert.go#L198-L219) — `if filter == nil || !filter.Valid { return result }`，这是「Valid=false 的资源不参与配置生成」范式的典型体现。

**配置生成**。`updateLocationAuthenticationFilter` 把三种类型分别挂到 location 的 `AuthBasic`/`AuthJWT`/`AuthOIDC` 字段：

[internal/controller/nginx/config/servers.go:1238-1266](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1238-L1266) — Basic 侧用 `dataplane.GenerateAuthBasicFileID` 算出 htpasswd 文件名并写 `AuthBasic{Realm, File}`，最终渲染成 `auth_basic "<realm>"; auth_basic_user_file <file>;`。

#### 4.2.4 代码实践

**实践目标**：复现 `examples/basic-authentication`，用 Basic 类型的 `AuthenticationFilter` 给 `/coffee` 路由加密码保护。

**操作步骤**（待本地验证，OSS 即可，无需 Plus）：

1. `kubectl apply -f examples/basic-authentication/cafe.yaml -f examples/basic-authentication/gateway.yaml`。
2. `kubectl apply -f examples/basic-authentication/basic-auth.yaml`，它先建一个 `type: Opaque` 的 Secret，`data.auth` 是 `htpasswd -bn user1 password1` 的 base64；再建 `AuthenticationFilter`，`spec.type: Basic`，`basic.secretRef.name: basic-auth`，`realm: "Restricted basic-auth"`。
3. `kubectl apply -f examples/basic-authentication/cafe-routes.yaml`，`/coffee` 规则的 `filters` 用 `ExtensionRef` 指向 `kind: AuthenticationFilter, name: basic-auth`；`/tea` 不加过滤器。
4. 取 `GW_IP`，分别访问 `/coffee` 与 `/tea`。

**需要观察的现象**：

- 不带凭据访问 `/coffee` 返回 401，响应头含 `WWW-Authenticate: Basic realm="Restricted basic-auth"`。
- `curl -u user1:password1 http://<GW_IP>/coffee -H 'Host: cafe.example.com'` 返回 200。
- 访问 `/tea` 无需凭据直接 200。

**预期结果**：`/coffee` 被 `auth_basic` 拦截，证明 Basic 过滤器已渲染成 NGINX 指令；`/tea` 不受影响，证明过滤器是路由规则级、不会泄漏到其他规则。

#### 4.2.5 小练习与答案

**练习 1**：把一个 `AuthenticationFilter` 的 `spec.type` 写成 `Basic`，却同时填了 `spec.oidc`，会怎样？
**答案**：准入层 CEL 规则 `type Basic must not set spec.oidc` 直接拒绝该对象。这与「图层兜底校验」形成双保险——即使关掉准入，图层 `switch` 也只走 Basic 分支，OIDC 字段被忽略。

**练习 2**：在一个纯 HTTP（非 HTTPS）监听器上挂 OIDC 过滤器，结果如何？
**答案**：过滤器自身校验通过，但绑定后 `validateOIDCFilters` 发现它挂在非 HTTPS 监听器上，会追加条件 `OIDC authentication requires an HTTPS listener` 并置 `Valid=false`，进而经 `propagateInvalidOIDCFiltersToRouteRules` 把对应路由规则的 `ResolvedRefs` 标为无效，数据面对该规则返回 500。

---

### 4.3 安全约束与 Plus 依赖

#### 4.3.1 概念说明

这两个过滤器都允许用户「伸手进 NGINX 配置或读取 Secret」，因此安全约束是设计的核心：

- **ExtensionRef 的 Group/Kind 白名单**：只有 `Group=gateway.nginx.org` 且 `Kind ∈ {SnippetsFilter, AuthenticationFilter}` 的扩展引用才会被解析，其余一律忽略，防止任意 CRD 被当成过滤器。
- **同 namespace 解析**：两个解析器都只在过滤器所在 namespace 内查找（`LocalObjectReference`），跨命名空间引用不被支持，与 ReferenceGrant 的跨 namespace 授权模型（u5-l3）刻意区分。
- **特性开关门控**：`SnippetsFilter` 受 `SnippetsFilters`/`Snippets` flag 控制（默认关闭），未开启时控制器根本不 watch 该 CRD，引用也解析不到；`AuthenticationFilter` 则默认注册、无 flag 门控。
- **Plus 能力门控**：JWT/OIDC 在图层显式判 `isPlus`，OSS 集群直接判无效，避免生成了数据面无法加载的指令。
- **claim 名脱敏冲突检测**：JWT/OIDC 的 claim 名会转成 NGINX 变量名，`-`/`.`/`/` 会被替换成 `_`，`realm_access/roles` 与 `realm_access_roles` 会撞成同一个变量，图层提前检测并拒绝。

#### 4.3.2 核心流程

两类约束落在两个不同阶段：

```text
引用合法性 (extension_ref_filter.go)
   └─ Group == gateway.nginx.org ?  Kind ∈ {SnippetsFilter, AuthenticationFilter} ?
        否 ──▶ 忽略（不解析，记路由 ResolvedRefs 无效）
        是 ──▶ 按解析器在同 namespace 查表

过滤器有效性 (snippets_filter.go / authentication_filter.go)
   ├─ SnippetsFilter:  上下文合法 + 每上下文唯一
   └─ AuthenticationFilter:
        ├─ Plus 门控 (JWT/OIDC)
        ├─ Secret 解析 (Basic/File/Remote/OIDC client/CA/CRL)
        ├─ claim 名脱敏冲突 (validateJWTAuthorization)
        └─ OIDC 绑定后: HTTPS 监听器 + URI 冲突 (validateOIDCFilters)
```

#### 4.3.3 源码精读

**ExtensionRef 白名单**。`validateExtensionRefFilter` 强制 Group 与 Kind 合法：

[internal/controller/state/graph/extension_ref_filter.go:42-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/extension_ref_filter.go#L42-L56) — `ref.Group != ngfAPI.GroupName` 记 `NotSupported`；`switch ref.Kind` 只放行 `SnippetsFilter` 与 `AuthenticationFilter`。`ExtensionRefFilter` 这个联合结构本身见 [extension_ref_filter.go:13-23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/extension_ref_filter.go#L13-L23)。

**特性开关门控 SnippetsFilter**。控制器的注册受 flag 控制：

[internal/controller/manager.go:764-773](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L764-L773) — 只有 `cfg.SnippetsFilters || cfg.Snippets` 时才注册 `SnippetsFilter` 控制器。flag 定义在 [internal/controller/config/config.go:60-63](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L60-L63)：`SnippetsFilters`（仅 SnippetsFilter）与 `Snippets`（SnippetsFilter + SnippetsPolicy）。`AuthenticationFilter` 无此门控，始终注册（见 [manager.go:919-921](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L919-L921)）。

**claim 名脱敏冲突检测**。这是 JWT/OIDC 授权里最隐蔽的安全点——两个不同的 claim 名可能撞成同一个 NGINX 变量，导致授权判断静默出错：

[internal/controller/state/graph/authentication_filter.go:181-258](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/authentication_filter.go#L181-L258) — `validateJWTAuthorization` 用 `sanitizeClaimNameForVariable` 把 claim 名里的 `-`/`.`/`/` 替换成 `_`，再用 `globalSanitizedNames` map 检测跨规则冲突；命中即记一条详细错误（指出哪两个 claim 名撞成了哪个变量）。脱敏函数与数据面的 `sanitizeVariablePrefix` 保持同源，确保「图层判定」与「实际渲染」一致。

**条件回写**。两个过滤器都复用统一的 `Accepted` 条件类型与 `Accepted/Invalid` reason：

[internal/controller/state/conditions/conditions.go:1289-1298](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1289-L1298) — `NewSnippetsFilterInvalid`；AuthenticationFilter 对应在 [conditions.go:1311-1320](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1311-L1320)。失败结论沉淀进节点 `Conditions`，再由 status updater（u8-l1）异步写回 CRD status，供 `kubectl get` 查看。

#### 4.3.4 代码实践

**实践目标**：用源码阅读验证「未开启 snippets 特性开关时，SnippetsFilter 引用解析不到」这一安全行为。

**操作步骤**（源码阅读型实践，无需部署）：

1. 打开 `internal/controller/manager.go`，定位 4.3.3 引用的注册块（`SnippetsFilters || Snippets`）。
2. 设想两种部署：A 只开了 `SnippetsFilters`，B 两个都没开。
3. 追踪：B 部署里 `SnippetsFilter` 控制器未注册 → ChangeProcessor 的 store 不持有该类型 → `getSnippetsFilterResolverForNamespace` 因 `len(snippetsFilters) == 0` 在 [snippets_filter.go:35-37](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/snippets_filter.go#L35-L37) 直接返回 `nil` → 该 HTTPRoute 的 ExtensionRef 解析为空。

**需要观察的现象**：在 B 部署下，引用了 SnippetsFilter 的 HTTPRoute 不会被拒绝，但其 `SnippetsFilter` 字段静默为空，snippet 不会出现在生成的配置里。

**预期结果**：理解「特性开关是能力闸门、CRD 存在性是数据闸门」的双层防护——即便有人误创建了 SnippetsFilter YAML，未开 flag 的集群也不会注入任何 snippet，从架构上杜绝了未授权的配置注入。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `SnippetsFilter` 需要 feature flag 而 `AuthenticationFilter` 不需要？
**答案**：snippet 允许注入任意 NGINX 指令，是有显著安全与稳定性风险的「高权力」能力，因此默认关闭、需运维显式开启；而 `AuthenticationFilter` 只暴露一组受控的、语义明确的指令（auth_basic/auth_jwt/oidc），风险可控，故默认注册。

**练习 2**：claim 名 `user.role` 与 `user_role` 同时出现在两个 authorization rule 里，会发生什么？
**答案**：两者经 `sanitizeClaimNameForVariable` 都变成 `user_role`，`validateJWTAuthorization` 检测到冲突，记一条 `field.Invalid`，过滤器判 `Valid=false` 并回写 `Accepted=False/Invalid`，避免授权静默错判。

## 5. 综合实践

设计一个把本讲三块内容串起来的小任务：**为一个 HTTPRoute 同时加「Basic 认证」与「只允许 GET」**。

要求：

1. 部署 `examples/basic-authentication` 的后端、网关、Secret、`AuthenticationFilter`、HTTPRoute。
2. 新写一个 `SnippetsFilter`（参考 `limit-except-sf.yaml`），`context: http.server.location`，值为 `limit_except GET { deny all; }`。
3. 修改 `/coffee` 那条 HTTPRoute 规则的 `filters`，让它**同时**包含两个 ExtensionRef 过滤器：先 `AuthenticationFilter/basic-auth`，再 `SnippetsFilter/<你的名字>`。
4. （需开启 snippets 特性开关的部署变体，见 4.3.3。）
5. 验证矩阵：
   - 无凭据 GET `/coffee` → 401（认证拦截）。
   - 带正确凭据 GET `/coffee` → 200。
   - 带正确凭据 POST `/coffee` → 403（`limit_except` 拦截）。

思考题（待本地验证后回答）：两个 ExtensionRef 过滤器在生成的 location 里的 `include` 顺序，是否会因为 `updateLocation` 中 `createIncludesFromLocationSnippetsFilters` 与 `updateLocationAuthenticationFilter` 的调用先后而影响 `auth_basic` 与 `limit_except` 的实际执行顺序？结合 [servers.go:1215-1216](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/servers.go#L1215-L1216) 给出你的判断。

## 6. 本讲小结

- `SnippetsFilter` 是一条把原始 NGINX 指令塞进生成配置的逃逸舱，严格限定四个上下文（main/http/http.server/http.server.location）且每个上下文唯一；main/http 全局聚合，server/location 跟随路由规则，最终都经 include 机制按文件名去重落盘。
- `AuthenticationFilter` 用 `spec.type`（Basic/OIDC/JWT）三选一表达认证，CEL 强制类型与字段互斥；Basic 走 OSS 自带的 `auth_basic`，JWT/OIDC 依赖 NGINX Plus 并在图层显式判 `isPlus`。
- 两类过滤器都走 ExtensionRef 绑定，由 `validateExtensionRefFilter` 做 Group/Kind 白名单，由同 namespace 解析器解析并标记 `Referenced`，无效时沉淀进 `Conditions` 并置 `Valid=false`。
- 安全约束分双层：`SnippetsFilter` 受 `SnippetsFilters`/`Snippets` 特性开关门控（默认关闭），`AuthenticationFilter` 默认注册；JWT/OIDC 的 claim 名脱敏冲突与 OIDC 的 HTTPS 监听器/URI 冲突在图层兜底校验。
- 从图到配置的链路完全沿用既有范式：图节点三元组（Source/Valid/Conditions）→ dataplane 渲染模型 → servers.go 在 location 上挂指令/include，体现 NGF 扩展机制的统一骨架。

## 7. 下一步学习建议

- 若想理解「策略附着式」的 snippet（本讲提到的 `SnippetsPolicy`），可阅读 `internal/controller/state/graph/policies.go` 与 `examples/snippets/limit-conn-sp.yaml`，对比它与 `SnippetsFilter` 在绑定模型（targetRef vs ExtensionRef）上的差异，并回顾 u8-l2/u8-l3。
- 若要新增一类 ExtensionRef 过滤器，重点改三处：`extension_ref_filter.go` 的 Kind 白名单与联合结构、新建 `<kind>.go` 的 `process*`/解析器、`dataplane/convert.go` 的转换函数，参考本讲两个过滤器的实现即可作为模板。
- 外部认证（`ExternalAuthFilter`，对应 `auth_request`）是认证体系的另一条路，可对照阅读 `internal/controller/nginx/config/servers.go` 中 `updateLocationExternalAuthFilter` 与 `examples/external-authentication`，理解它与 `AuthenticationFilter` 的边界。
