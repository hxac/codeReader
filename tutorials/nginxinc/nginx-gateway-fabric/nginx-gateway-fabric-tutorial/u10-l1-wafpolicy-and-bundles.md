# WAFPolicy 与 Bundle 模型

## 1. 本讲目标

本讲是「WAF（F5 App Protect）集成」单元的第一讲，回答一个问题：**NGF 是用什么数据结构来描述一条 WAF 策略、它的安全规则从哪里来？**

读完本讲，你应当能够：

- 读懂 `WAFPolicy` 这个 CRD 的字段结构，说清 `targetRefs`/`type`/`policySource`/`policyRef` 各自的作用。
- 区分四种 bundle（编译后的安全策略包）来源——HTTP、NIM、N1C、PLM——以及它们在「引用方式」和「鉴权方式」上的差异。
- 理解 PLM（Policy Lifecycle Manager）模式下 `APPolicy`/`APLogConf` 的 status 子资源类型，以及 NGF 如何从中取出 bundle 的下载地址与校验和。
- 说清 bundle 的 checksum 校验机制，以及拉取失败、校验失败时 NGF 写回的 Conditions（即「失败行为」）。

本讲只讲**数据模型与来源选择**；bundle 的实际拉取、轮询、下发的运行时链路是下一讲 `u10-l2` 的主题。

## 2. 前置知识

### 2.1 什么是 WAF 与「编译后的 bundle」

F5 NGINX App Protect（NAP）是 NGINX 的 Web 应用防火墙。用户用一份「策略」描述「检测/阻断哪些攻击签名」，但 NGINX 数据面并不能直接读懂这份人类可读的策略文件——它需要一份**编译后的 bundle**（一个 `.tgz` 二进制包，NGF 仓库里固定对应 NAP v5，版本号见 [internal/framework/waf/waf.go:9](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/waf.go#L9)）。

所以 WAF 的核心流程是：

> 人类写策略 → 某处编译成 bundle → NGF 把 bundle 拉到控制面 → 经 NGINX Agent 下发到数据面 NGINX。

本讲关注的就是这条链路最左端的「bundle 从哪里来」。

### 2.2 策略附着（Policy Attachment）回顾

本讲承接 `u8-l2` 的策略附着机制。`WAFPolicy` 是一个 **Inherited Attached Policy**（继承型附着策略，见类型注解 [apis/v1alpha1/wafpolicy_types.go:14](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L14)）。这意味着：

- 它通过 `targetRefs` 附着到 `Gateway`/`HTTPRoute`/`GRPCRoute`。
- 附着到 Gateway 时，策略会**向下继承**到该 Gateway 下的路由。

如果你还不熟悉 `targetRefs`、Direct/Inherited 的区别，建议先读 `u8-l2`。

### 2.3 关键缩写

| 缩写 | 全称 | 一句话理解 |
|------|------|-----------|
| NAP | NGINX App Protect | NGINX 的 WAF 引擎 |
| NIM | NGINX Instance Manager | 一个托管 WAF 编译的平台，对外提供 bundles API |
| N1C | F5 NGINX One Console | F5 的 SaaS 控制台，提供 security policies API |
| PLM | Policy Lifecycle Manager | 一个把策略编译成 bundle 并把产物写进 S3 的组件，同时管理 `APPolicy`/`APLogConf` CRD |
| bundle | —— | 编译后的 `.tgz` 安全策略包 |

## 3. 本讲源码地图

本讲涉及的源码文件按职责分为三类：

| 文件 | 作用 | 本讲角色 |
|------|------|---------|
| [apis/v1alpha1/wafpolicy_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go) | `WAFPolicy` CRD 的全部类型定义 | **核心**：WAFPolicy 结构、四种来源、校验/轮询配置 |
| [apis/waf/v1/types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/types.go) | PLM 管理的 `APPolicy`/`APLogConf` 的 status 子资源的轻量 Go 类型 | **核心**：PLM 状态类型、bundle 状态机 |
| [apis/waf/v1/conversion.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/conversion.go) | 把 unstructured 的 `APPolicy` status 反序列化成上面的类型 | 辅助：PLM status 的解析入口 |
| [internal/framework/waf/fetch/fetch.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go) | HTTP/NIM/N1C 的拉取实现与 checksum 计算 | 辅助：理解来源差异与校验落点（下一讲详讲） |
| [internal/framework/waf/fetch/s3/s3.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go) | PLM 的 S3 拉取实现 | 辅助：PLM 来源的鉴权与校验 |
| [internal/controller/state/conditions/conditions.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go) | WAF 策略相关 Conditions 的定义 | 辅助：失败行为如何对外暴露 |

> 注意：本讲引用的 fetch/s3/conditions 文件是为讲清「模型如何被使用」，它们的内部运行机制属于 `u10-l2`。

## 4. 核心概念与源码讲解

### 4.1 WAFPolicy 结构：CRD 定义与 Spec 字段

#### 4.1.1 概念说明

`WAFPolicy` 是 NGF 自定义的策略类 CRD（参见 `u8-l2` 关于「策略类资源 vs 配置类资源」的区分）。它的职责很单一：**声明一条 WAF 规则要保护谁（targetRefs）、规则包从哪来（type + 来源字段）、安全日志怎么打（securityLogs）**。

它本身不包含 WAF 规则的细节——规则细节在「别处」（HTTP URL、NIM、N1C 或 PLM 管理的 CRD）编译成 bundle，`WAFPolicy` 只负责「指路」。这种设计把 NGF 与具体的 WAF 编译/托管平台解耦：NGF 只关心「把 bundle 拉下来」，不关心「策略怎么写、怎么编译」。

#### 4.1.2 核心流程

一个 `WAFPolicy` 的逻辑结构可以画成：

```
WAFPolicy
├── spec.targetRefs        # 保护谁（Gateway / HTTPRoute / GRPCRoute，同 Kind）
├── spec.type              # bundle 从哪类来源取：HTTP / NIM / N1C / PLM
├── spec.policySource      # 非 CRD 来源配置（HTTP/NIM/N1C 用）
│   ├── httpSource / nimSource / n1cSource   # 三选一
│   ├── auth / tlsSecret / validation / polling / timeout / retryAttempts
├── spec.policyRef         # CRD 来源配置（PLM 用）
│   └── apPolicyRef        # 指向 PLM 管理的 APPolicy
└── spec.securityLogs[]    # 安全日志配置（可选，最多 32 条）
```

字段之间有严格的**互斥与配对**约束，这些约束不是运行期代码检查，而是用 CEL（Common Expression Language）写在 kubebuilder 注解里、由 Kubernetes 准入层在 API server 端直接拒绝（详见 `u2-l4` 的准入式校验）。关键约束有：

- `type` 与来源字段必须一一对应：HTTP 配 `httpSource`、NIM 配 `nimSource`、N1C 配 `n1cSource`、PLM 配 `policyRef.apPolicyRef`。
- `policySource` 与 `policyRef` 互斥：非 PLM 用前者，PLM 用后者，不能同时给。
- `targetRefs` 里的 Kind 必须全部相同（要么全是 Gateway，要么全是 HTTPRoute/GRPCRoute）。

#### 4.1.3 源码精读

**WAFPolicy 顶层类型**——标准 CRD 三段式（TypeMeta + ObjectMeta + Spec/Status），注意 `Status` 复用了 Gateway API 的 `gatewayv1.PolicyStatus`：

[WAFPolicy 顶层类型定义：apis/v1alpha1/wafpolicy_types.go:16-29](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L16-L29)

```go
// WAFPolicy is an Inherited Attached Policy. It provides a way to configure F5 WAF for NGINX
// for Gateways and Routes by referencing compiled WAF policy bundles. Bundles can be fetched directly from an
// HTTP/HTTPS URL (type: HTTP), from an NGINX Instance Manager instance (type: NIM), from an F5 NGINX One
// Console instance (type: N1C), or from a Policy Lifecycle Manager's S3-compatible storage (type: PLM).
type WAFPolicy struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec   WAFPolicySpec             `json:"spec"`
	Status gatewayv1.PolicyStatus    `json:"status,omitempty"`
}
```

这段类注释就是四种来源的「官方总纲」，建议背下来。类型上方的 kubebuilder 注解（第 8–13 行）还透露两个事实：`shortName=wgbpolicy`（所以 `kubectl get wgbpolicy` 能用）、`policy=inherited`（继承型）。

**WAFPolicySpec 与其 CEL 校验**——`targetRefs` 是数组（1–16 个）、`type` 是枚举、`policySource`/`policyRef` 都是指针（`omitempty`，可空）：

[WAFPolicySpec 及互斥/配对 CEL 校验：apis/v1alpha1/wafpolicy_types.go:42-87](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L42-L87)

其中最值得逐条读懂的是顶部那组 `XValidation`（第 42–47 行），它们用 CEL 表达了前述的互斥/配对规则。例如第 44 行：

```
self.type == 'PLM' || (has(self.policySource) && ((self.type == 'HTTP' && has(self.policySource.httpSource)) || ...))
```

翻译成人话：「type 是 PLM 时不需要 policySource；否则必须按 type 给对应的来源子字段」。这类 CEL 校验在 `kubectl apply` 阶段就会被 API server 挡下，根本到不了 NGF 控制面。

**TargetRefs 的 Kind 一致性校验**（第 58 行）用一行 CEL 强制所有 targetRefs 同 Kind：

```
self.all(t1, self.all(t2, t1.kind == t2.kind))
```

#### 4.1.4 代码实践

**实践目标**：亲手验证 WAFPolicy 的 CEL 校验确实在 API server 端生效，体会「模型即校验」。

**操作步骤**：

1. 在已部署 NGF（含 WAFPolicy CRD）的集群里，写一份「故意违反互斥规则」的 YAML（示例代码，非项目原有文件）：

```yaml
# 示例代码：type=NIM 却给了 httpSource，违反配对规则
apiVersion: gateway.nginx.org/v1alpha1
kind: WAFPolicy
metadata:
  name: bad-mismatch
spec:
  targetRefs:
  - group: gateway.networking.k8s.io
    kind: Gateway
    name: gateway
  type: NIM                 # 声明来源是 NIM
  policySource:
    httpSource:             # 却配了 HTTP 的字段 → 不匹配
      url: https://example.com/p.tgz
```

2. `kubectl apply -f bad-mismatch.yaml`。

**需要观察的现象**：apply 被拒绝，错误信息里应出现对应 CEL 的 `message`（如 "type must match the configured policy source"）。

**预期结果**：资源创建失败，NGF 控制面日志里**不会**出现任何关于这个 WAFPolicy 的记录——因为它压根没进 etcd。这说明这些约束是 API server 层的防线，与 NGF 运行逻辑无关。

> 待本地验证：若你本地没有带 WAF 的 NGF 集群，也可改为「源码阅读型实践」——把第 42–47 行的四条 CEL 规则逐条翻译成中文，并自己构造一份能通过、两份不能通过的 YAML。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `policySource` 和 `policyRef` 都设计成指针（`*PolicySource`/`*PolicyRef`）而不是普通值？

**参考答案**：因为它们是 `omitempty` 的可选字段，且彼此互斥。用指针才能在 CEL 里用 `has(self.policySource)` 区分「用户没设这个字段」与「用户设了空对象」；若用值类型，零值和「未设置」无法区分，互斥校验会失效。

**练习 2**：把 `targetRefs` 指向一个 `GRPCRoute` 和一个 `HTTPRoute` 会怎样？

**参考答案**：会被第 58 行的 CEL（`t1.kind == t2.kind`）在准入阶段拒绝，报 "All TargetRefs must be the same Kind"。一个 WAFPolicy 只能保护同一种 Kind 的资源。

---

### 4.2 四种 Bundle 来源：HTTP / NIM / N1C / PLM 与鉴权差异

#### 4.2.1 概念说明

`spec.type` 是一个四值枚举（[apis/v1alpha1/wafpolicy_types.go:89-92](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L89-L92)），决定 bundle 从哪来。四种来源可以分成两大类：

- **「直接拉 URL」类（HTTP / NIM / N1C）**：bundle 在一个 HTTP 服务后面，NGF 用 `policySource` 配置如何拉取，鉴权信息（凭据）放在 WAFPolicy 同命名空间的 Secret 里，经 `policySource.auth.secretRef` 引用。三者区别只在于「HTTP 协议的形态不同」。
- **「引用 CRD」类（PLM）**：bundle 不在普通 URL 后面，而在 PLM 的 S3 兼容存储里；PLM 把「bundle 编好了、放在 s3:// 哪里、校验和是多少」写进 `APPolicy` CRD 的 status。NGF 不在 WAFPolicy 里配 URL，而是用 `policyRef.apPolicyRef` 指向那个 CRD，S3 连接参数（endpoint/凭据/CA）走**集群级 CLI flag**（`--plm-storage-*`）。

这一分类是本讲最重要的直觉：**前三种是「WAFPolicy 自带鉴权去拉」，PLM 是「引用 CRD + 集群级 S3 配置去拉」**。

#### 4.2.2 核心流程

四种来源的拉取与鉴权对比：

```
┌──────────┬──────────────────────┬─────────────────────────┬──────────────────────────┐
│ 来源 type│ 引用方式              │ 鉴权方式                 │ bundle 校验和来源         │
├──────────┼──────────────────────┼─────────────────────────┼──────────────────────────┤
│ HTTP     │ policySource         │ Secret 的 username/      │ 自取 <url>.sha256 或       │
│          │ .httpSource.url      │ password（Basic）        │ 用户手填 expectedChecksum │
├──────────┼──────────────────────┼─────────────────────────┼──────────────────────────┤
│ NIM      │ policySource         │ Secret 的 token          │ NIM API 返回的 hash       │
│          │ .nimSource(url+name  │ （Bearer Token）         │ （自动校验）              │
│          │  /policyUID)         │                         │                          │
├──────────┼──────────────────────┼─────────────────────────┼──────────────────────────┤
│ N1C      │ policySource         │ Secret 的 token          │ N1C compile API 返回的    │
│          │ .n1cSource(url+ns+   │ （APIToken，非 Bearer）  │ hash（自动校验）          │
│          │  name/objID)         │                         │                          │
├──────────┼──────────────────────┼─────────────────────────┼──────────────────────────┤
│ PLM      │ policyRef            │ 集群级 --plm-storage-    │ APPolicy.status.bundle    │
│          │ .apPolicyRef(name)   │ credentials-secret       │ .sha256（来自 CRD status）│
│          │                      │ （S3 access/secret key） │                          │
└──────────┴──────────────────────┴─────────────────────────┴──────────────────────────┘
```

注意 N1C 与 NIM 虽然都用「Secret 里的 token」，但**鉴权头不同**：NIM 用标准的 `Authorization: Bearer <token>`，N1C 用 F5 私有的 `Authorization: APIToken <token>`。这个差异在 fetch 层的 `BundleAuth` 结构里用独立字段表达。

#### 4.2.3 源码精读

**枚举与四种来源常量**——每个常量的注释就是该来源的「身份证」：

[PolicySourceType 枚举与 HTTP/NIM/N1C/PLM 常量：apis/v1alpha1/wafpolicy_types.go:89-111](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L89-L111)

重点读 `PolicySourceTypeN1C`（第 101–105 行）和 `PolicySourceTypePLM`（第 107–110 行）的注释：N1C 「Authentication uses the APIToken scheme」、PLM 「Bundles are fetched from PLM's S3-compatible storage (SeaweedFS)」「Cluster-wide S3 connection parameters are configured via CLI flags (--plm-storage-*)」。这两条注释正是上一节对比表的依据。

**PolicySource（HTTP/NIM/N1C 共用容器）**——一个结构体容纳三种来源子字段（互斥）+ 公共的鉴权/TLS/校验/轮询配置：

[PolicySource 结构与「三选一」CEL：apis/v1alpha1/wafpolicy_types.go:113-178](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L113-L178)

第 115 行的 CEL `[has(self.httpSource), has(self.nimSource), has(self.n1cSource)].filter(x, x).size() == 1` 强制三种来源子字段恰好设一个。

**三种来源子结构的关键字段差异**：

- HTTP 最简单，只有一个 `URL`：[HTTPBundleSource: apis/v1alpha1/wafpolicy_types.go:235-244](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L235-L244)
- NIM 需要实例 URL + 策略标识（`policyName` 与 `policyUID` 二选一）：[NIMBundleSource: apis/v1alpha1/wafpolicy_types.go:246-276](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L246-L276)
- N1C 最复杂，要 URL + `namespace` + 策略标识（`policyName`/`policyObjectID` 二选一）+ 可选 `policyVersionID`：[N1CBundleSource: apis/v1alpha1/wafpolicy_types.go:329-372](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L329-L372)

**鉴权：BundleAuth**——一个 Secret 引用，但同一个 Secret 里的不同键对应不同鉴权方案：

[BundleAuth：apis/v1alpha1/wafpolicy_types.go:414-421](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L414-L421) 注释明确写出同一个 Secret 可含三类键：`username`/`password`（HTTP Basic）、`token`（NIM 的 Bearer、或 N1C 的 APIToken）。到底用哪种方案，取决于 `type`。

fetch 层把这种「一个 Secret 多种用法」落到了实处——内部 `BundleAuth` 有四个独立字段，发请求时按优先级选择鉴权头：

[fetch 层的 BundleAuth 与鉴权头选择：internal/framework/waf/fetch/fetch.go:80-87](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L80-L87) （结构定义）

[doGetWithHeaders 中按 APIToken/Bearer/Basic 选鉴权头：internal/framework/waf/fetch/fetch.go:1421-1429](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L1421-L1429)

```go
if auth != nil {
	switch {
	case auth.APIToken != "":
		req.Header.Set("Authorization", "APIToken "+auth.APIToken)   // N1C
	case auth.BearerToken != "":
		req.Header.Set("Authorization", "Bearer "+auth.BearerToken)  // NIM
	case auth.Username != "":
		req.SetBasicAuth(auth.Username, auth.Password)               // HTTP Basic
	}
}
```

这段 `switch` 正是「NIM 用 Bearer、N1C 用 APIToken、HTTP 用 Basic」对比表的代码依据。

**PLM 来源：APPolicyReference**——注意它只引用一个 CRD 名字（+ 可选命名空间），**没有 URL、没有鉴权字段**：

[APPolicyReference：apis/v1alpha1/wafpolicy_types.go:374-392](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L374-L392)

PLM 的鉴权在另一处——S3 凭据由集群级 CLI flag 指向的 Secret 提供，fetch 层的 S3 `Credentials` 用的是 access key/secret key：

[S3 凭据类型：internal/framework/waf/fetch/s3/s3.go:26-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L26-L31)

跨命名空间引用 `APPolicy` 时需要 ReferenceGrant 授权（类注释第 182–183 行说明），这与 `u5-l3` 的 ReferenceGrant 机制一致。

#### 4.2.4 代码实践

**实践目标**：为同一个 WAF 需求，分别用四种来源写出 `WAFPolicy`，并填出鉴权差异表。

**操作步骤**：

1. 阅读 `examples/waf-policy/` 下已有的三个示例（HTTP/NIM/N1C）和测试用例里的 PLM 示例：
   - [examples/waf-policy/wafpolicy-http.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/waf-policy/wafpolicy-http.yaml)
   - [examples/waf-policy/wafpolicy-nim.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/waf-policy/wafpolicy-nim.yaml)
   - [examples/waf-policy/wafpolicy-n1c.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/waf-policy/wafpolicy-n1c.yaml)
   - [tests/suite/manifests/waf-policy/wafpolicy-plm.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/waf-policy/wafpolicy-plm.yaml)

2. 对照四个示例，填写下面的鉴权差异表（示例代码，请自行补全）：

| 来源 | 关键 Spec 字段 | Secret 里的键 | 鉴权头 | 校验和来源 |
|------|---------------|--------------|--------|-----------|
| HTTP | `policySource.httpSource.url` | ? | ? | ? |
| NIM | ? | `token` | ? | NIM API hash |
| N1C | ? | ? | `APIToken` | ? |
| PLM | `policyRef.apPolicyRef.name` | （集群级 S3 Secret） | S3 access/secret key | ? |

**需要观察的现象**：四个示例里，只有 PLM 示例**没有** `policySource` 和 `auth` 字段；HTTP/NIM/N1C 三个示例的 `auth.secretRef.name` 各指向不同的凭据 Secret。

**预期结果**：你能用自己的话总结出「PLM 的鉴权与 bundle 位置都不在 WAFPolicy 里，而在集群级配置 + 被引用的 CRD status 里」这一关键差异。

#### 4.2.5 小练习与答案

**练习 1**：为什么 NIM 和 N1C 都用 Secret 里的 `token` 键，却要发不同的鉴权头？

**参考答案**：因为两个平台的 API 约定不同——NIM 遵循通用的 Bearer Token，N1C（F5 NGINX One Console）用 F5 自定义的 `APIToken` 方案。NGF 不能用同一个头，所以 fetch 层的 `switch`（fetch.go:1421）按 `APIToken`/`BearerToken`/`Username` 三个独立字段区分。

**练习 2**：一个 `WAFPolicy` 能不能同时从 HTTP 拉策略 bundle、从 NIM 拉日志 profile bundle？

**参考答案**：可以。`type` 和 `policySource` 决定的是**策略 bundle（policySource）**的来源；而 `securityLogs[].logSource` 是独立的，每条安全日志可以单独选 `defaultProfile`/`httpSource`/`nimSource`/`n1cSource`（见 [LogSource: apis/v1alpha1/wafpolicy_types.go:471-547](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L471-L547)）。所以策略来自 HTTP、日志来自 NIM 是合法组合。

**练习 3**：PLM 来源为什么不在 `WAFPolicy` 里配 URL？

**参考答案**：因为 PLM 模式下，bundle 的实际位置（S3 URI）是 PLM **编译完成后**才确定的，会动态写进 `APPolicy` 的 status。WAFPolicy 创建时根本不知道 URL，所以只能「引用 CRD」，由 NGF 运行时去读那个 CRD 的 status 拿 URL（详见 4.3）。

---

### 4.3 PLM 状态类型与 Bundle 校验、失败行为

#### 4.3.1 概念说明

本模块解决两个问题：（1）PLM 模式下，被引用的 `APPolicy`/`APLogConf` 的 status 长什么样、NGF 怎么读？（2）无论哪种来源，bundle 拉下来后怎么校验完整性、校验/拉取失败时 NGF 对外怎么表现？

先看 PLM 状态类型。`apis/waf/v1` 这个包很特别：它的 Go 类型**不是 controller-gen 管理的 CRD 类型**，而是专门用来「类型安全地解析 PLM 管理的 `APPolicy`/`APLogConf` 的 status 子资源」的轻量结构。换句话说，`APPolicy`/`APLogConf` 这两个 CRD 由 PLM 组件（不是 NGF）定义和管理，NGF 只是把它们当作 unstructured 对象读进来、再把 status 反序列化成 Go 结构体。

bundle 校验的核心是 **SHA-256 checksum**：每个 bundle 都有一个 64 位十六进制校验和，下载后必须比对，防止传输中被篡改或截断。不同来源的「校验和从哪来」不同（见 4.2 对比表），但比对逻辑统一。

#### 4.3.2 核心流程

**PLM status 的生命周期（bundle 状态机）**：

`BundleStatus.State` 是一个有限状态机，描述 PLM 编译 bundle 的进度：

```
            (PLM 接收策略)
                  │
                  ▼
             ┌─────────┐  编译排队
             │ pending │ ──────┐
             └─────────┘       │
                  │            ▼
                  │       ┌────────────┐ 编译中
                  └─────▶ │ processing │
                          └────────────┘
                                │
                ┌───────────────┼───────────────┐
                ▼                               ▼
          ┌─────────┐                     ┌─────────┐
          │  ready  │                     │ invalid │
          │(有location│                     │(编译失败)│
          │ +sha256) │                     └─────────┘
          └─────────┘
```

只有 `State == ready` 时，`BundleStatus` 才会带上 `Location`（S3 URI）和 `SHA256`，NGF 才会去 S3 拉取。其他状态（pending/processing/invalid）NGF 都无法拉取，对应不同的失败行为。

**bundle 校验与失败的行为映射**——校验和比对失败、拉取失败、bundle 未就绪，分别映射到不同的 Conditions：

| 场景 | Condition Type | Status | Reason |
|------|---------------|--------|--------|
| 所有引用解析成功 | ResolvedRefs | True | ResolvedRefs |
| 引用不存在/无效 | ResolvedRefs | False | InvalidRef |
| 跨命名空间未授权 | ResolvedRefs | False | RefNotPermitted |
| bundle 已下发数据面 | Programmed | True | Programmed |
| bundle 拉取失败 | Programmed | False | FetchError |
| checksum 校验失败 | Programmed | False | IntegrityError |
| bundle 尚未就绪（fail-closed） | Programmed | False | Pending |
| 拉取失败但用旧 bundle 兜底 | Programmed | True | StaleBundleWarning |
| PLM 模式但没配 `--plm-storage-url` | Accepted | False | Invalid |

#### 4.3.3 源码精读

**PLM CRD 的 API 组/版本/Kind 常量与 bundle 状态机**——这是整个 PLM 模式的「坐标系」：

[waf/v1 常量与 bundle 状态：apis/waf/v1/types.go:6-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/types.go#L6-L25)

注意 `Group = "appprotect.f5.com"`——这两个 CRD 属于 F5 App Protect 的 API 组，**不属于** NGF 的 `gateway.nginx.org` 组。`BundleStateReady/Pending/Processing/Invalid` 四个常量就是上面状态机的节点。

**APPolicyStatus / APLogConfStatus / BundleStatus**——`APPolicy` 和 `APLogConf` 的 status 结构完全对称，都内嵌一个 `*BundleStatus`：

[APPolicyStatus/APLogConfStatus/BundleStatus：apis/waf/v1/types.go:27-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/types.go#L27-L50)

`BundleStatus` 的四个字段就是 NGF 拉 PLM bundle 的全部输入：

- `Location`：S3 URI（如 `s3://bucket/path/bundle.tgz`），`ready` 状态下才有。
- `SHA256`：bundle 的校验和，下载后必须比对。
- `CompilerVersion`：编译器版本（诊断用）。
- `ObservedGeneration`：编译的是哪一版策略（用来判断 status 是否已反映最新 spec）。

**解析入口：ParseAPPolicyStatus / ParseAPLogConfStatus**——这两个函数把 unstructured 的 PLM CRD 转成上面的强类型 status：

[ParseAPPolicyStatus / ParseAPLogConfStatus：apis/waf/v1/conversion.go:10-30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/conversion.go#L10-L30)

它们内部调 [parseStatus（含 TypeMeta 校验 + FromUnstructured 反序列化）：apis/waf/v1/conversion.go:32-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/waf/v1/conversion.go#L32-L56)，先校验对象的 apiVersion/kind 是否真的是 `appprotect.f5.com/v1` 的 `APPolicy`/`APLogConf`，再把 status map 反序列化。这套「先验类型、再转结构」的写法保证了 NGF 不会误读别的资源。

**checksum 计算**——所有来源统一用这个函数算 bundle 的 SHA-256：

[ComputeChecksum：internal/framework/waf/fetch/fetch.go:1466-1470](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L1466-L1470)

```go
func ComputeChecksum(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
```

**HTTP 来源的 checksum 校验（两种模式）**——`BundleValidation` 提供两种互斥的校验方式：`expectedChecksum`（手填固定值）或 `verifyChecksum`（自动取 `<url>.sha256` 伴生文件）：

[BundleValidation 与互斥 CEL：apis/v1alpha1/wafpolicy_types.go:189-217](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L189-L217)

fetch 层在 `fetchHTTP` 里落实 `verifyChecksum`：取伴生 `.sha256` 文件、比对、不一致则返回 `nonTransientError`（不重试）：

[fetchHTTP 的 checksum 校验：internal/framework/waf/fetch/fetch.go:504-526](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go#L504-L526)

**S3（PLM）来源的 checksum 校验**——拉完 S3 对象后用 `expectedSHA256`（来自 APPolicy status）比对：

[s3.FetchBundle 的 checksum 校验：internal/framework/waf/fetch/s3/s3.go:102-113](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/s3/s3.go#L102-L113)

**失败行为：Conditions 工厂函数**——拉取失败、校验失败、未就绪分别对应独立的 Condition 构造器：

[WAF 条件类型与 Reason 常量：internal/controller/state/conditions/conditions.go:124-154](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L124-L154)

其中几个关键失败条件：

- 拉取失败 → [NewPolicyNotProgrammedBundleFetchError（Reason=FetchError）：conditions.go:1643-1651](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1643-L1651)
- 校验失败 → [NewPolicyNotProgrammedIntegrityError（Reason=IntegrityError）：conditions.go:1653-1661](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1653-L1661)
- 未就绪（fail-closed， withheld 配置下发）→ [NewPolicyNotProgrammedBundlePending（Reason=Pending）：conditions.go:1690-1700](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1690-L1700)
- 用旧 bundle 兜底 → [NewPolicyProgrammedStaleBundleWarning（Reason=StaleBundleWarning）：conditions.go:1678-1688](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1678-L1688)
- PLM 未配置 → [NewPolicyNotAcceptedPLMNotConfigured（Accepted=False, Invalid）：conditions.go:1713-1722](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/conditions/conditions.go#L1713-L1722)

注意 `NewPolicyNotProgrammedBundlePending` 的注释（第 1691–1692 行）点明了 NGF 的**fail-closed 姿态**：「The Gateway config push is withheld until the bundle is available」——bundle 没就绪时，对应的 Gateway 配置根本不会下发，宁可拒服务也不用未经校验的策略。

#### 4.3.4 代码实践

**实践目标**：把 PLM 的 bundle 状态机映射到 NGF 的 Conditions，建立「CRD status → NGF 行为」的完整心智模型。

**操作步骤**：

1. 阅读测试清单 [tests/suite/manifests/waf-policy/wafpolicy-plm-missing.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/waf-policy/wafpolicy-plm-missing.yaml) 与 [wafpolicy-missing-bundle.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/tests/suite/manifests/waf-policy/wafpolicy-missing-bundle.yaml)，这两个是「故意制造失败场景」的用例。

2. 假设一个 `APPolicy` 的 status 是下面这样（示例代码），判断 NGF 应写什么 Condition：

```yaml
# 示例代码：一个 APPolicy 的 status，请据此判断 NGF 行为
status:
  bundle:
    state: processing      # 情况 A：换成 ready / invalid 分别会怎样？
    location: ""
    sha256: ""
```

3. 针对三种 `state`（processing / ready / invalid），填写下表：

| APPolicy status.state | NGF 能否拉 bundle | 应写的 Condition（Type/Status/Reason） |
|----------------------|------------------|---------------------------------------|
| `processing` | ? | ? |
| `ready`（带 location+sha256） | ? | （拉成功后）Programmed/True/Programmed |
| `invalid` | ? | ? |

**需要观察的现象**：`state != ready` 时，`location`/`sha256` 为空，NGF 无 URL 可拉，只能进入「等待/失败」分支。

**预期结果**：`processing` → 无法拉取，写 `Programmed/False/Pending`（fail-closed， withhold 配置）；`ready` → 走 S3 拉取 + checksum 校验，成功写 `Programmed/True/Programmed`；`invalid` → 引用无效，写 `ResolvedRefs/False/InvalidRef` 或 `Programmed/False/FetchError`（取决于具体实现，待本地验证）。

> 待本地验证：上述 invalid 分支的确切 Condition 取决于 NGF 图层对 `state=invalid` 的判定路径，建议在带 WAF 的环境里实际 apply 一个指向不存在/无效 APPolicy 的 WAFPolicy 后 `kubectl describe wafpolicy` 确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `apis/waf/v1` 的类型注释特意强调「这些不是 controller-gen 管理的 CRD 类型」？

**参考答案**：因为 `APPolicy`/`APLogConf` 这两个 CRD 由外部的 PLM 组件定义和注册，NGF 只是它们的**消费者**。NGF 把它们当作 unstructured 对象读进来，再用这套轻量 Go 结构反序列化 status。如果用 controller-gen 管理，就会和 PLM 的 CRD 定义重复/冲突，且 NGF 不需要（也不应该）管理这两个 CRD 的生命周期。

**练习 2**：NIM/N1C 来源的 bundle 校验和与 HTTP 来源有什么本质区别？

**参考答案**：HTTP 来源的校验和要么用户手填（`expectedChecksum`）、要么去取伴生的 `.sha256` 文件（`verifyChecksum`，仅 HTTP 支持）；而 NIM/N1C 的校验和由**平台 API 本身返回**（NIM 的 `metadata.hash`、N1C compile 接口的 `hash`），NGF 自动校验、不需要用户配置，也不支持 `verifyChecksum`（见 [wafpolicy_types.go:46 的 CEL](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/wafpolicy_types.go#L46)）。

**练习 3**：`NewPolicyNotProgrammedBundlePending` 体现的 fail-closed 姿态，对生产安全意味着什么？

**参考答案**：意味着 bundle 没拉到/没校验通过时，NGF 宁可**不下发该 Gateway 的配置**（流量可能因此受影响），也绝不带上一份未经校验或缺失的 WAF 策略去接流量。这避免了「以为开了 WAF 其实没开」的假安全感，是安全系统的正确默认。

## 5. 综合实践

**任务**：你是某团队的平台工程师，需要为一个线上 Gateway 配置 WAF。团队同时拥有：(a) 一个自建的 bundle HTTP 服务器（内网，需 Basic 认证）；(b) 一个 NIM 实例；(c) 一个 N1C 控制台；(d) 一套 PLM + SeaweedFS。请你为这四种来源**各设计一份 `WAFPolicy`**，并产出一份给团队评审的「选型决策表」。

**要求**：

1. 每份 `WAFPolicy` 必须字段合法（通过 4.1 讲的 CEL 校验）：正确的 `type`、对应的来源字段、`targetRefs` 指向同一个 Gateway。
2. 每份都配一条安全日志：HTTP/NIM/N1C 来源的策略，日志分别用 `defaultProfile: log_illegal`（内置 profile，无需鉴权）；PLM 来源的日志用 `logRef.apLogConfRef` 引用一个 `APLogConf`。
3. 在决策表里对比四个方案在以下维度的差异：鉴权复杂度、bundle 时效性（静态 URL vs 平台托管 vs CRD status 驱动）、跨组件依赖、是否需要 ReferenceGrant、是否需要集群级 CLI flag。

**评审要点（参考答案思路）**：

- **HTTP**：最简单、最自主，但 bundle 时效性靠人工/CI 更新 URL 内容；需自管 bundle 服务器与 Basic 凭据 Secret。
- **NIM**：bundle 由平台托管、有版本（policyUID），适合已用 NIM 的团队；用 Bearer Token。
- **N1C**：SaaS 托管、最适合云上 F5 用户；用 APIToken，字段最复杂（namespace 必填）。
- **PLM**：bundle 位置由 CRD status 动态驱动、最「云原生」，但依赖外部 PLM 组件 + 集群级 `--plm-storage-*` 配置；跨命名空间引用需 ReferenceGrant。

> 这是源码阅读 + 设计型实践，无需真实集群即可完成；若有带 WAF 的 NGF 环境，可进一步 `kubectl apply` 验证 CEL 校验与 Conditions。

## 6. 本讲小结

- `WAFPolicy` 是一个 **Inherited Attached Policy** CRD，只负责「指路」：用 `targetRefs` 说保护谁、用 `type` 说 bundle 从哪类来源取、用 `securityLogs` 说日志怎么打；策略细节在 bundle 里，不在 CRD 里。
- 四种 bundle 来源分两类：**直接拉 URL**（HTTP/NIM/N1C，用 `policySource` + 同命名空间 Secret 鉴权）与**引用 CRD**（PLM，用 `policyRef.apPolicyRef` + 集群级 `--plm-storage-*` S3 配置）。
- 鉴权头随来源不同：HTTP 用 Basic、NIM 用 `Bearer`、N1C 用 `APIToken`、PLM 用 S3 access/secret key；fetch 层用一个 `switch` 落实这一差异。
- `apis/waf/v1` 是「轻量解析类型」，专门把 PLM 管理的 `APPolicy`/`APLogConf` 的 status 反序列化成 `BundleStatus`；其 `State` 是 pending→processing→ready/invalid 的状态机，只有 `ready` 才带 S3 `location` + `sha256`。
- bundle 完整性靠统一的 SHA-256 校验：HTTP 用 `expectedChecksum`/`verifyChecksum`（仅 HTTP 支持后者），NIM/N1C 用平台 API 返回的 hash 自动校验，PLM 用 APPolicy status 的 sha256 校验。
- 失败行为映射到 Conditions：拉取失败→FetchError、校验失败→IntegrityError、未就绪→Pending（fail-closed， withhold 配置下发）、用旧 bundle 兜底→StaleBundleWarning、PLM 未配置→Accepted/Invalid。

## 7. 下一步学习建议

本讲只讲了**数据模型与来源选择**——`WAFPolicy` 长什么样、bundle 从哪来。但 bundle 的**实际生命周期**（拉取、轮询、下发）还没展开。建议接着学习：

- **`u10-l2` Bundle 拉取、轮询与下发**：深入 [internal/framework/waf/fetch/](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/fetch/fetch.go) 的 `Fetcher` 接口与 NIM/N1C 的多步 API 调用链、[internal/framework/waf/poller/](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/waf/poller/manager.go) 的轮询 Manager，以及 bundle 变更如何经 `WAFBundleReconcileEvent` 触发图重建与数据面下发。
- **回顾 `u8-l1` Conditions 体系**：本讲的失败条件（FetchError/IntegrityError/Pending）都遵循 `u8-l1` 讲的 Type/Status/Reason/Message 四元组与 status queue 异步回写机制，对照阅读能加深理解。
- **回顾 `u5-l3` ReferenceGrant**：PLM 来源的跨命名空间 `APPolicy`/`APLogConf` 引用复用 `u5-l3` 的授权机制，可对照阅读。
- **通读设计提案**：[docs/proposals/nap-waf.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/proposals/nap-waf.md) 记录了 WAF 集成的设计动机与取舍，是把本讲各模块串起来的最佳文档。
