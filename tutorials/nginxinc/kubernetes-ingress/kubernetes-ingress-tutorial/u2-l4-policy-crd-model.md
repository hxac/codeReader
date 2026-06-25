# Policy CRD 与可复用策略模型

## 1. 本讲目标

在前几讲里，我们认识了路由资源（Ingress / VirtualServer / VSR / TransportServer）和它们的类型定义（`types.go`），也了解了 CRD 的两层校验（schema 层 + Go 层）。本讲聚焦一个「横切」资源——**Policy**——它本身不定义任何路由，却被 VirtualServer / VirtualServerRoute / Ingress 按名字「挂载」上来，用来统一表达访问控制、限流、认证、缓存等横切关注点。

学完本讲你应当能够：

- 说清 Policy 的 `PolicySpec` 结构，特别是「一个 Policy 只能表达一种策略」这条不变量是如何用代码表达和校验的。
- 理解 Policy 的引用机制：它**没有 selector 字段**，而是被宿主资源（VS/VSR 的 `policies` 字段、Ingress 的注解）**按名字引用**。
- 列出 Policy 支持的全部策略类型及其用途，并区分哪些需要 NGINX Plus、哪些受命令行 feature-gate 开关控制。
- 区分每种策略类型在 Ingress（v1）与 VirtualServer（v2）上的适用范围，并知道这个范围由哪个函数裁定。

## 2. 前置知识

- **CRD 的骨架**：所有 NIC 的 CRD 都遵循 `TypeMeta + ObjectMeta + Spec + Status` 的结构（见 u2-l2）。Policy 也不例外。
- **两层校验**（见 u2-l3）：第一层是 `types.go` 上的 kubebuilder 标记，由 `controller-gen` 生成成 OpenAPI/CEL schema，由 API Server 在准入时强制；第二层是 `pkg/apis/configuration/validation` 包里手写的 Go 校验，在控制器运行期执行，失败会把资源状态置为 `Rejected`。
- **互斥指针表达「枚举之一」**（见 u2-l2）：PolicySpec 用一排「互斥指针字段」（每个策略类型一个 `*T`）来表达「一个 Policy 只填一种策略」。本讲会看到这条不变量在 Go 校验层是如何被强制成「恰好填一个」的。
- **NGINX Plus 与 OSS**：NIC 同时支持开源 NGINX（OSS）和商业版 NGINX Plus，部分策略类型依赖 Plus 专有能力（如 JWT、OIDC）。本讲会标注每个类型的 Plus 依赖。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/apis/configuration/v1/types.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) | CRD 数据真相源：`Policy` / `PolicySpec` / `PolicyReference` / 各策略类型结构体定义 |
| [pkg/apis/configuration/validation/policy.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go) | Policy 的 Go 层校验：数据驱动的字段分发、「恰好一个策略」规则、各类型具体校验、feature-gate 判定 |
| [internal/configs/policy.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/policy.go) | 配置生成层：`IsPolicySupportedOnIngress()`——Ingress 策略适用范围的唯一真相源 |
| [internal/configs/ingress.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go) | Ingress 上策略引用的过滤（`filterIngressPolicyRefs`），把 Ingress 不支持的策略类型剔除 |
| [internal/configs/annotations.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go) | Ingress 引用 Policy 的注解常量 `nginx.org/policies` / `nginx.com/policies` |
| [internal/k8s/reference_checkers.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go) | 反向引用检查：判断一个 Policy 是否仍被某 VS/VSR/Ingress 引用（用于级联更新与删除判断） |
| [docs/crd/k8s.nginx.org_policies.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_policies.md) | Policy CRD 的用户文档（字段说明，由 schema 生成） |
| [examples/custom-resources/rate-limit/](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/rate-limit) | 一个完整的限流示例：Policy + VirtualServer + 后端应用 |

---

## 4. 核心概念与源码讲解

### 4.1 Policy spec 结构：一排互斥指针与「恰好一个」不变量

#### 4.1.1 概念说明

Policy 要解决的问题是**策略的复用与解耦**。在讲路由资源时（u2-l1）我们看到，VirtualServer 的 `routes` 描述「请求往哪转」，而「对这个路由施加什么横切策略」（限流、鉴权、访问控制……）则是另一类需求。如果把每种策略的字段都塞进 VirtualServer 的 spec，会让 VS 膨胀、难以复用——同一个限流规则可能要挂在十几个路由上。

Policy 的设计是：把**一种**策略独立成一个 CRD 对象，起个名字，再让 VS/VSR/Ingress 按名字引用它。这样同一个 Policy 可以被多处复用，修改一处即可全局生效。

这里有两个关键不变量，都体现在 `PolicySpec` 的结构上：

1. **一个 Policy 只能表达一种策略类型。** 你不能在一个 Policy 里同时写 `rateLimit` 和 `accessControl`——要两种策略就建两个 Policy。这是为了让每个策略对象语义单一、可独立校验、可独立替换。
2. **哪种策略由哪个非空指针字段决定。** `PolicySpec` 用一排「互斥指针」字段（`*AccessControl`、`*RateLimit`……）来表达「填了哪个就是哪种策略」。

> 注意：本讲的「selector」其实是规格描述里的用词。**PolicySpec 实际上没有 `selector` 字段**（它不像 Kubernetes NetworkPolicy 那样用 label selector 去选目标）。Policy 是被宿主资源**主动按名引用**的，目标范围由「谁引用了它」决定。这一点在 4.2 会展开，这里先记住：Policy 没有 selector。

#### 4.1.2 核心流程

一个 Policy 从 YAML 到生效，经历这样一条链路：

```text
用户写 Policy YAML（spec 里恰好填一个策略字段）
        │
        ▼
API Server 准入：执行 schema 层校验（types.go 上的 kubebuilder 标记 → CEL/OpenAPI）
        │  （跨字段约束如 OIDC 的 XValidation 在这里跑）
        ▼
控制器 sync：调用 validation 包的 ValidatePolicy() 跑 Go 层校验
        │  - policyFields() 表按字段分发
        │  - gateCheck 判 feature-gate（Plus / CLI 开关）
        │  - validatePolicySpec 强制「fieldCount == 1」
        ▼
校验通过 → 纳入内存模型；失败 → .status.state = Rejected
        │
        ▼
被 VS/VSR/Ingress 引用时，配置生成层把它翻译成 NGINX 指令
```

其中「恰好一个策略字段」是 Go 层强制的不变量，schema 层难以表达（CEL 可以，但项目选择在 Go 层用计数兜底）。

#### 4.1.3 源码精读

先看 `Policy` 类型本身和它的 kubebuilder 标注（短名 `pol`、带 status 子资源、是 storageversion）：

`Policy` 结构体与标注——[pkg/apis/configuration/v1/types.go:756-772](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L756-L772)：这段标注声明了 `shortName=pol`、`subresource:status`（允许控制器回写 `.status`）、`storageversion`（多个 API 版本共存时以此版本为准），以及两个 `printcolumn`（让 `kubectl get pol` 直接显示 State 和 Age）。

接着是核心的 `PolicySpec`——[pkg/apis/configuration/v1/types.go:787-815](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L787-L815)：注意它就是一排指针字段，每个字段对应一种策略。`ingressClassName` 用于把 Policy 路由到正确的控制器实例（见 4.2），其余 12 个字段是 12 种策略类型。文件顶部的注释明确写了「Only one policy (field) is allowed.」

「恰好一个」的强制逻辑在 Go 校验层。`validatePolicySpec` 遍历所有字段、对每个非空字段计数——[pkg/apis/configuration/validation/policy.go:169-200](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L169-L200)：关键在第 191 行 `if fieldCount != 1`，无论填了 0 个还是多于 1 个都会报错，错误消息还会根据 `cfg.IsPlus` 动态拼出合法字段清单（Plus 多出 `jwt`、`oidc`、`waf`）。

这段还顺带展示了「数据驱动的校验表」设计：`policyFields()` 返回一个 `policyFieldValidator` 切片，每项含 `isSet`（判断该字段是否填了）、`validate`（该字段的校验函数）、可选的 `gateCheck`（feature-gate 前置检查）。新增一种策略类型时，主要工作就是在这个表里加一项——这是后续 u8-l3「新增 Policy 类型」的扩展点。

#### 4.1.4 代码实践

**实践目标**：亲手验证「一个 Policy 只能填一种策略」这条不变量。

**操作步骤**：

1. 在 `examples/custom-resources/rate-limit/` 下复制一份 `rate-limit.yaml`，改名为 `bad-policy.yaml`。
2. 在 `spec` 下**同时**添加一个 `accessControl` 字段，使 spec 里既有 `rateLimit` 又有 `accessControl`：

   ```yaml
   # 示例代码：故意违反「恰好一个」不变量
   apiVersion: k8s.nginx.org/v1
   kind: Policy
   metadata:
     name: bad-policy
   spec:
     rateLimit:
       rate: 1r/s
       key: ${binary_remote_addr}
     accessControl:
       allow:
       - 0.0.0.0/0
   ```

3. 如果本地有集群和已安装的 NIC，执行 `kubectl apply -f bad-policy.yaml`。若无集群，则改为阅读 `validatePolicySpec` 的测试用例 `pkg/apis/configuration/validation/policy_test.go`（搜索「must specify exactly one」相关断言），确认测试期望该输入返回错误。

**需要观察的现象**：apply 被拒绝，或在测试中看到一条 `must specify exactly one of: ...` 的错误。

**预期结果**：因为 `fieldCount == 2`，`validatePolicySpec` 报错，资源无法生效。这印证了「一个 Policy 一种策略」由 Go 校验层强制。若无集群，**待本地验证**测试断言的精确措辞。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `PolicySpec` 用「一排指针字段」而不是「一个 `type` 字符串 + 一个 `config` map」来表达多种策略？

**参考答案**：指针字段让每种策略有**独立的强类型结构体**，编译期就能约束字段名与类型，IDE 与 kubebuilder 都能据此生成 schema；而 `type + map` 会丢失类型信息，校验只能靠运行期字符串匹配，容易出错且无法自动生成 CRD schema。

**练习 2**：`validatePolicySpec` 在 `fieldCount != 1` 时报错，为什么不写成 `fieldCount > 1`？

**参考答案**：因为「一个策略都不填」（`fieldCount == 0`）同样非法——一个 Policy 必须表达恰好一种策略。用 `!= 1` 同时覆盖了「填多了」和「没填」两种非法情况。

---

### 4.2 Policy 的引用机制：按名引用，而非 selector

#### 4.2.1 概念说明

上一节强调过：**Policy 没有 `selector` 字段**。它的目标范围完全由「谁引用了它」决定。这一点和很多 K8s 资源（如 NetworkPolicy、Service）用 label selector 选目标的做法不同，初学者容易误解。

引用分两种宿主：

- **VirtualServer / VirtualServerRoute**：通过 spec 里的 `policies` 字段引用，这是一个 `[]PolicyReference` 列表。VirtualServer 有**两层**引用点：
  - `spec.policies`：**server 级**，对整站生效。
  - `spec.routes[].policies`：**route（location）级**，会**覆盖**同类型的 server 级策略。
- **Ingress**：没有 `policies` 字段，改用**注解** `nginx.org/policies`（OSS 支持的策略类型）和 `nginx.com/policies`（Plus 策略类型），值是逗号分隔的 Policy 名。

引用对象 `PolicyReference` 很简单，只有名字和可选命名空间，因此支持**跨命名空间引用**。

#### 4.2.2 核心流程

以 VirtualServer 为例，引用解析过程是：

```text
VS spec.policies: [{name: rl-policy}]      （server 级）
VS spec.routes[].policies: [{name: ip-allow}] （route 级，覆盖同名同类型）
        │
        ▼
控制器 sync 时，把 Policy 名解析为内存中的 Policy 对象
  - namespace 缺省时取 VS 所在 namespace
  - 跨 namespace：PolicyReference.Namespace 指定
        │
        ▼
配置生成层把每个被引用 Policy 翻译成对应 NGINX 指令，
挂到 server 块或 location 块
```

反过来，当某个 Policy 自身发生变化（更新或删除）时，控制器需要知道「有哪些 VS/VSR/Ingress 引用了我，需要重新生成配置」。这就是 `reference_checkers.go` 里反向引用检查的用途（见源码精读）。

#### 4.2.3 源码精读

先看引用对象本身——`PolicyReference`——[pkg/apis/configuration/v1/types.go:113-119](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L113-L119)：只有 `Name`（必填）和 `Namespace`（可选，缺省取宿主 namespace）。注释也说明了：若引用的 Policy 不存在或非法，NGINX 会返回 500。

再看 VirtualServer 上的两个引用点：

- server 级 `Policies` 字段——[pkg/apis/configuration/v1/types.go:57](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L57)：`Policies []PolicyReference`。
- route 级 `Policies` 字段——[pkg/apis/configuration/v1/types.go:277-278](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L277-L278)，其注释明确写道「The policies override the policies of the same type defined in the spec of the VirtualServer」，即 route 级覆盖 server 级同类型策略。

Ingress 走注解路径——[internal/configs/annotations.go:14-18](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/annotations.go#L14-L18)：`nginx.org/policies`（OSS 类型）与 `nginx.com/policies`（Plus 类型）两个常量。

反向引用检查——[internal/k8s/reference_checkers.go:260-272](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L260-L272)：`IsReferencedByVirtualServer` 同时检查 `vs.Spec.Policies`（server 级）和每条 `route.Policies`（route 级），只要任一处引用了就返回 true。对比同文件中 Ingress 的版本——[internal/k8s/reference_checkers.go:241-254](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/reference_checkers.go#L241-L254)：Ingress 版本去解析 `nginx.org/policies` / `nginx.com/policies` 注解的逗号分隔列表，体现了「VS 用字段、Ingress 用注解」两种引用方式在代码里的对称实现。

#### 4.2.4 代码实践

**实践目标**：对比 VS 与 Ingress 两种引用写法，并理解跨命名空间引用。

**操作步骤**：

1. 打开示例 [examples/custom-resources/rate-limit/virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/rate-limit/virtual-server.yaml)，观察第 7-8 行 `policies: - name: rate-limit-policy`，这是 server 级引用。
2. 设想：若该 Policy 在另一个 namespace `team-a`，引用应改写为：

   ```yaml
   # 示例代码：跨命名空间引用 Policy
   policies:
   - name: rate-limit-policy
     namespace: team-a
   ```

3. 再设想同样语义用 Ingress 表达（注意 rateLimit 实际不支持 Ingress，这里仅看引用语法）：注解写法是 `nginx.org/policies: "rate-limit-policy"` 或跨命名空间 `nginx.org/policies: "team-a/rate-limit-policy"`（逗号分隔多个）。

**需要观察的现象**：VS 用结构化字段 `policies[].name/namespace`；Ingress 用单个字符串注解，靠逗号分隔、斜杠区分命名空间。

**预期结果**：你能说清两种引用方式在数据结构上的差异（结构化对象 vs 字符串注解），以及为什么跨命名空间引用对二者都成立。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 VirtualServer 在 `spec.policies` 引用了一个 RateLimit Policy，又在某条 `route.policies` 引用了另一个 RateLimit Policy，最终该 route 生效的是哪个？

**参考答案**：route 级生效。因为 `Route.Policies` 的语义是「覆盖 server 级同类型策略」（见 types.go:277-278 注释）。同一类型的策略在一个 location 上只能有一个来源，route 级优先。

**练习 2**：为什么 Policy 不设计成带 `selector` 去自动匹配 VirtualServer？

**参考答案**：带 selector 会引入「哪些 VS 被选中」的隐式、动态绑定，难以审计与排错（增删一个 label 就可能改变生效策略集）；按名引用是显式、可追踪的——在 VS spec 里能直接看到它挂了哪些策略。显式引用也更符合「策略是可复用、可命名对象」的设计初衷。

---

### 4.3 策略类型清单：12 种策略与各自的校验/门控

#### 4.3.1 概念说明

`PolicySpec` 里共有 **12 种**策略类型字段。每种都对应一个独立的强类型结构体（在 `types.go` 里定义）和一段独立的校验逻辑（在 `validation/policy.go` 里）。理解这个清单，是读懂后续配置生成（u4-l7）和认证深入（u6-l4）的前提。

每种策略有两个维度的「门槛」：

- **是否需要 NGINX Plus**：JWT、OIDC、WAF 依赖 Plus 专有模块；其余在 OSS 上也能用（部分功能，如 Cache 的 purge，仍需 Plus）。
- **是否受命令行 feature-gate 开关控制**：OIDC 需要 `-enable-oidc`、WAF 需要 `-enable-app-protect`、ExternalAuth 的 `authSnippets` 需要 `-enable-snippets`。

这些门控在 Go 校验层用 `gateCheck` 实现，未开启时直接拒绝（见源码精读）。

#### 4.3.2 核心流程

策略类型校验的统一流程在 `policyFields()` 表里被声明，校验时按表分发：

```text
ValidatePolicy(policy, cfg)               // 入口，cfg 携带 IsPlus/EnableOIDC/... 开关
   └─ validatePolicySpec
        ├─ for each field in policyFields():
        │     if field.isSet(spec):
        │        if field.gateCheck 存在: 跑门控（如 !IsPlus → 拒绝 jwt）
        │        跑 field.validate(...)    // 该类型的具体校验
        │        fieldCount++
        └─ if fieldCount != 1: 报错「恰好一个」
```

`cfg`（`PolicyValidationConfig`）是运行期开关的载体，控制器构造校验器时根据自身启动参数填入。

#### 4.3.3 源码精读

策略类型总表（字段名对应 `PolicySpec` 的 json tag）：

| json 字段 | Go 类型 | 用途 | 需 Plus? | feature-gate |
| --- | --- | --- | --- | --- |
| `accessControl` | `AccessControl` | 按客户端 IP 允许/拒绝 | 否 | 无 |
| `rateLimit` | `RateLimit` | 按 key（如客户端 IP）限流 | 否 | 无 |
| `basicAuth` | `BasicAuth` | HTTP Basic 认证（htpasswd） | 否 | 无 |
| `ingressMTLS` | `IngressMTLS` | 校验客户端证书（入站 mTLS） | 否 | 无 |
| `egressMTLS` | `EgressMTLS` | 上游 TLS 认证与证书校验（出站 mTLS） | 否 | 无 |
| `apiKey` | `APIKey` | 按 header/query 的 API Key 鉴权 | 否 | 无 |
| `cache` | `Cache` | 代理缓存（proxy cache） | 部分（purge 需 Plus） | 无 |
| `cors` | `CORS` | 跨域 CORS 响应头 | 否 | 无 |
| `externalAuth` | `ExternalAuth` | 外部鉴权服务（如 oauth2-proxy） | 否 | `authSnippets` 需 `-enable-snippets` |
| `jwt` | `JWTAuth` | JWT 鉴权（本地 Secret 或远程 JWKS） | **是** | 无 |
| `oidc` | `OIDC` | OpenID Connect 集成 | **是** | 需 `-enable-oidc` |
| `waf` | `WAF` | NGINX App Protect WAF | **是** | 需 `-enable-app-protect` |

「门控」的实现——以 JWT 为例——[pkg/apis/configuration/validation/policy.go:67-79](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L67-L79)：`gateCheck` 在 `!cfg.IsPlus` 时返回 `Forbidden` 错误并 `earlyReturn`（直接终止，不再跑后续字段校验）。OIDC 的门控更复杂——[pkg/apis/configuration/validation/policy.go:108-126](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L108-L126)：同时检查 `-enable-oidc` 和 `IsPlus` 两个条件。

每种策略的具体结构体定义都在 `types.go` 里。本讲以限流为例精读，其余类型在 u4-l7（策略翻译）和 u6-l4（认证深入）展开。`RateLimit` 结构体——[pkg/apis/configuration/v1/types.go:835-861](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L835-L861)：包含 `rate`（如 `1r/s`）、`key`（限流维度，如 `${binary_remote_addr}`）、`zoneSize`（共享内存区大小）、`burst`、`delay`、`noDelay`、`dryRun`、`rejectCode`、`scale`、`condition`（条件限流）等字段。

限流的具体校验——`validateRateLimit`——[pkg/apis/configuration/validation/policy.go:228-265](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L228-L265)：依次校验 `zoneSize`（必须大于 31k）、`rate`（必须匹配 `Nr/s` 或 `Nr/m`）、`key`（必须是允许的 NGINX 变量集合）、`rejectCode`（必须在 400–599）等。其中 `rate` 的格式由正则——[pkg/apis/configuration/validation/policy.go:884-900](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L884-L900)——强制为 `[1-9]\d*r/[sSmM]`（如 `16r/s`、`32r/m`）。

完整的字段级说明可对照用户文档——[docs/crd/k8s.nginx.org_policies.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/crd/k8s.nginx.org_policies.md)：该文档由 CRD schema 自动生成，与 `types.go` 一一对应。

#### 4.3.4 代码实践

**实践目标**：跟踪一种策略类型的「结构体 → 校验函数」对应关系。

**操作步骤**：

1. 选 `accessControl` 类型。先看它的结构体——[pkg/apis/configuration/v1/types.go:829-832](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L829-L832)：只有 `Allow []string` 和 `Deny []string`。
2. 在 `policyFields()` 表里找到它的条目——[pkg/apis/configuration/validation/policy.go:53-59](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L53-L59)：`validate` 指向 `validateAccessControl`。
3. 读 `validateAccessControl`——[pkg/apis/configuration/validation/policy.go:202-226](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L202-L226)。

**需要观察的现象**：`validateAccessControl` 内部也有一个「恰好一个」规则——`allow` 和 `deny` 只能填一个（第 221 行 `if fieldCount != 1`），并且每个 IP/CIDR 都用 `validateIPorCIDR` 校验。

**预期结果**：你应当能说出 AccessControl 拒绝哪些非法取值：同时填了 allow 和 deny、填了既不是 IP 也不是 CIDR 的字符串。这印证了「校验函数与结构体一一对应、且每种策略的约束各不相同」。

#### 4.3.5 小练习与答案

**练习 1**：用户在 OSS（非 Plus）环境写了一个带 `jwt` 字段的 Policy 并 apply，会发生什么？

**参考答案**：Go 校验层在 `policyFields()` 的 jwt 条目里 `gateCheck` 检测到 `!cfg.IsPlus`，返回 `Forbidden: jwt secrets are only supported in NGINX Plus` 并 `earlyReturn`。资源校验失败，状态被置为 Rejected，不会生效。

**练习 2**：`rateLimit.rate` 写成 `10/s` 会被接受吗？

**参考答案**：不会。`validateRate` 用正则 `[1-9]\d*r/[sSmM]` 校验，要求 `r/s` 或 `r/m` 单位。`10/s` 缺少 `r`，匹配失败，报错。合法写法是 `10r/s`。

---

### 4.4 策略适用范围：哪些策略能用在 Ingress 上

#### 4.4.1 概念说明

一个容易混淆的点是：**并非所有 12 种策略都能用在 Ingress 上**。虽然 Policy 的官方描述只提「为 VS/VSR 定义策略」，但实际上有 6 种策略也支持 Ingress（通过注解引用），另外 6 种只支持 VS/VSR。

这个「适用范围」由配置生成层的 `IsPolicySupportedOnIngress()` 函数单一裁定。它是一份**白名单**——只有列在里面的策略类型才会对 Ingress 生效。这个设计的原因是：Ingress 是标准 K8s 资源，能力受限，部分策略（如 RateLimit、OIDC）需要 VS 的结构化能力才能正确翻译，因此在 Ingress 上不支持。

记住一句话：**所有策略都支持 VS/VSR；只有白名单内的策略支持 Ingress。**

#### 4.4.2 核心流程

```text
策略类型           VS/VSR 支持?    Ingress 支持?
──────────────    ────────────    ─────────────
全部 12 种          是              仅白名单 6 种
                                     │
                                     ▼
                       IsPolicySupportedOnIngress(pol) 判定
                                     │
              ┌──────────────────────┴──────────────────────┐
              ▼                                               ▼
     支持：Ingress 注解引用的该 Policy 被翻译          不支持：filterIngressPolicyRefs
     进 Ingress 的 server/location 配置                把它从引用列表剔除并告警
```

#### 4.4.3 源码精读

适用范围的唯一真相源——`IsPolicySupportedOnIngress`——[internal/configs/policy.go:106-120](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/policy.go#L106-L120)：函数前的注释明确写道「This is the single source of truth for the Ingress policy allowlist」。它返回 true 的条件是 Policy 的 spec 填了以下任一字段：`AccessControl`、`CORS`、`ExternalAuth`、`IngressMTLS`、`EgressMTLS`、`WAF`。

据此可得到适用范围对照：

| 策略类型 | VS/VSR | Ingress |
| --- | --- | --- |
| accessControl | ✅ | ✅ |
| cors | ✅ | ✅ |
| externalAuth | ✅ | ✅ |
| ingressMTLS | ✅ | ✅ |
| egressMTLS | ✅ | ✅ |
| waf | ✅ | ✅（仍需 Plus + `-enable-app-protect`） |
| rateLimit | ✅ | ❌ |
| basicAuth | ✅ | ❌ |
| apiKey | ✅ | ❌ |
| cache | ✅ | ❌ |
| jwt | ✅ | ❌ |
| oidc | ✅ | ❌ |

Ingress 侧的过滤实现——`filterIngressPolicyRefs`——[internal/configs/ingress.go:140-182](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/ingress.go#L140-L182)：它在生成 Ingress 配置前，检查通过 `nginx.org/policies` 注解引用的每个 Policy，若该 Policy 属于「需要 Plus 注解（`nginx.com/policies`）」的类型却出现在 OSS 注解里，就告警并返回致命错误（让该 Ingress 返回 500）。控制器侧 `internal/k8s/policy.go:147` 也用 `IsPolicySupportedOnIngress(pol)` 做了同样的剔除判定——[internal/k8s/policy.go:141-147](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/policy.go#L141-L147) 的注释还提醒：将来若给 Ingress 新增策略类型支持，必须同步把该字段加进 `IsPolicySupportedOnIngress()`，否则不会生效。

#### 4.4.4 代码实践

**实践目标**：用代码验证「RateLimit 不支持 Ingress」。

**操作步骤**：

1. 打开 `internal/configs/policy.go:113-120` 的 `IsPolicySupportedOnIngress`，确认其返回表达式里**没有** `pol.Spec.RateLimit`。
2. 思考：若用户在一个 Ingress 上用注解 `nginx.org/policies: "rate-limit-policy"` 引用一个 RateLimit Policy，配置生成时会发生什么？
3. （可选）阅读 `internal/configs/policy_test.go` 中的 `TestIsPolicySupportedOnIngress`（约 4227 行起），看测试用例对各策略类型的期望值。

**需要观察的现象**：`IsPolicySupportedOnIngress` 对 RateLimit Policy 返回 false，于是控制器在为该 Ingress 解析策略引用时把它剔除，该限流策略在 Ingress 上不会生效（应改用 VirtualServer）。

**预期结果**：你能准确说出「RateLimit 只能用于 VS/VSR，不能用于 Ingress」这一结论的代码依据。完整测试断言的精确措辞**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `IsPolicySupportedOnIngress` 被设计成「单一真相源」，并在多处调用点都强调要同步？

**参考答案**：因为适用范围是一个易漂移的约束——若新增策略类型时忘了更新白名单，Ingress 侧会静默忽略该策略，行为难以察觉。把它收口到一个函数并加注释提醒，是为了让「新增 Ingress 支持策略」这一变更集中在一处完成，避免多处各自维护导致不一致。

**练习 2**：WAF 在表格里 Ingress 列是 ✅，但它又需要 Plus 和 `-enable-app-protect`。这两者矛盾吗？

**参考答案**：不矛盾。「适用范围」回答的是「该策略类型在 Ingress 上是否被允许翻译」，WAF 允许；而「Plus / feature-gate」回答的是「该策略是否被允许使用」。两个维度正交：WAF 通过了适用范围检查，但若运行环境不是 Plus 或没开 `-enable-app-protect`，会在校验层被 `gateCheck` 拒绝。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个完整的「编写 RateLimit Policy 并在 VirtualServer 中引用」的任务。

**任务**：为一个 web 应用创建限流策略：每个客户端 IP 每秒最多 1 个请求，超出部分进入突发队列（burst），并把它挂到一个 VirtualServer 上。

**操作步骤**：

1. **编写 Policy**。参考示例 [examples/custom-resources/rate-limit/rate-limit.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/rate-limit/rate-limit.yaml)，创建：

   ```yaml
   apiVersion: k8s.nginx.org/v1
   kind: Policy
   metadata:
     name: rate-limit-policy
   spec:
     rateLimit:
       rate: 1r/s              # 每秒 1 请求（格式由 validateRate 的正则强制）
       key: ${binary_remote_addr}  # 按客户端 IP 限流
       zoneSize: 10M           # 共享内存区，必须 > 31k
       burst: 5                # 允许 5 个突发请求排队
   ```

   对照本讲学到的校验规则自检：`rate` 匹配 `Nr/s` 格式、`zoneSize` 大于 31k、`key` 是允许的变量、且 spec 里**恰好**填了一个策略字段（`rateLimit`）。

2. **在 VirtualServer 中引用它**。参考 [examples/custom-resources/rate-limit/virtual-server.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/examples/custom-resources/rate-limit/virtual-server.yaml)，在 `spec.policies` 下按名引用：

   ```yaml
   apiVersion: k8s.nginx.org/v1
   kind: VirtualServer
   metadata:
     name: webapp
   spec:
     host: webapp.example.com
     policies:
     - name: rate-limit-policy   # server 级引用；PolicyReference 无 namespace → 同 namespace
     upstreams:
     - name: webapp
       service: webapp-svc
       port: 80
     routes:
     - path: /
       action:
         pass: webapp
   ```

3. **本地校验语法（无需集群）**。对照本讲的源码，逐条说明：
   - 为什么 `rate: 1r/s` 合法而 `rate: 1/s` 非法？（答：`validateRate` 的正则 `[1-9]\d*r/[sSmM]`）
   - 为什么 Policy 没有被 Ingress 引用的可能？（答：RateLimit 不在 `IsPolicySupportedOnIngress` 白名单）
   - 若把引用放到某条 `route.policies` 而非 `spec.policies`，语义有何不同？（答：变成 location 级，覆盖 server 级同类型策略）

4. **（有集群时）端到端验证**：`kubectl apply -f rate-limit.yaml -f virtual-server.yaml -f webapp.yaml`，然后用示例 README 描述的方法压测，观察超过 1r/s + burst 后请求被限流（默认返回 503，或你设置的 `rejectCode`）。

**预期结果**：你产出了一份语法正确、符合全部校验不变量的 Policy + VirtualServer 组合，并能用本讲学到的源码知识解释每一条字段为什么这么写。压测结果**待本地验证**。

## 6. 本讲小结

- **Policy 是横切策略的可复用 CRD**：它不定义路由，只表达「一种」策略（限流、鉴权、访问控制等），被 VS/VSR/Ingress 按名引用，实现策略与路由解耦。
- **一个 Policy 恰好表达一种策略**：`PolicySpec` 用一排互斥指针字段表达，Go 校验层 `validatePolicySpec` 用 `fieldCount != 1` 强制（[policy.go:191](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/policy.go#L191)）。
- **Policy 没有 selector，靠按名引用确定目标**：VS/VSR 用 `policies` 字段（分 server 级与 route 级，route 级覆盖 server 级同类型），Ingress 用 `nginx.org/policies` / `nginx.com/policies` 注解。
- **共 12 种策略类型**，各有独立结构体与校验函数；JWT/OIDC/WAF 需 NGINX Plus，OIDC/WAF/ExternalAuth.snippets 还受命令行 feature-gate 控制。
- **适用范围由 `IsPolicySupportedOnIngress` 单一裁定**：所有策略支持 VS/VSR，但只有 AccessControl/CORS/ExternalAuth/IngressMTLS/EgressMTLS/WAF 这 6 种支持 Ingress。
- **校验是两层叠加**：schema 层（kubebuilder 标记、CEL XValidation）管格式与跨字段约束，Go 层（`validation/policy.go`）管「恰好一个」、feature-gate、各类型语义校验——这是 u2-l3 的具体落地。

## 7. 下一步学习建议

- **接下来读 u3 单元**（控制器核心）：去看控制器是如何在 sync 时把 VS/VSR/Ingress 里的 `policies` 引用解析成内存中的 Policy 对象、Policy 变更时如何通过 `reference_checkers` 触发引用方重新生成配置。
- **再读 u4-l7（Policy → 配置翻译）**：本讲只讲到「Policy 的数据模型与校验」，u4-l7 会讲 `internal/configs/policy.go` 如何把每种 Policy 翻译成具体的 NGINX 指令（如 RateLimit → `limit_req_zone` / `limit_req`）。
- **深入认证策略读 u6-l4**：JWT/OIDC/mTLS/ExternalAuth 的配置生成与 njs 协作细节在那里展开。
- **想自己加一种新 Policy 类型**：直接读 u8-l3，它会给出从 `types.go` 加字段 → 加校验 → 加翻译 → 加文档 → 加测试的完整改动清单；本讲的 `policyFields()` 表和 `IsPolicySupportedOnIngress()` 就是其中两个必改点。
