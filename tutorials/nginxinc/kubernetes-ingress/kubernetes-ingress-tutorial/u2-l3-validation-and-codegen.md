# CRD 校验与代码生成链路

> 所属单元：u2 资源模型与 CRD 定义 ｜ 难度：intermediate ｜ 依赖讲义：u2-l2（CRD 类型定义源码：types.go）

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 NIC 把「CRD 字段校验」拆成了**两层**——声明式的 OpenAPI/CEL schema 层与命令式的 Go 校验层——并解释为什么需要两层。
2. 读懂 `pkg/apis/configuration/validation/` 下校验函数的组织方式（Validator 结构体、`field.ErrorList`、可复用的格式校验辅助函数）。
3. 知道 `config/crd/bases/` 下那 12 份 CRD YAML 是从哪里来的，并能追踪一个 kubebuilder 标记（如 `Enum`、`Pattern`、`XValidation`）最终如何变成 schema 里的 `enum:` / `pattern:` / `x-kubernetes-validations:`。
4. 掌握「改了 `types.go` 之后必须执行的代码生成流程」：`make update-codegen` → `make update-crds`，以及 `verify-codegen` 如何防止生成产物与源码漂移。

## 2. 前置知识

在进入本讲前，先建立两个直觉。

### 2.1 Kubernetes 有两道校验关卡

当你执行 `kubectl apply -f cafe-virtual-server.yaml` 时，校验并不是「一处」发生的：

- **第一道：API Server 的准入（admission）校验**。API Server 会拿 YAML 去比对 CRD 里声明的 **OpenAPI v3 schema**（类型、必填、`enum`、`pattern`、`x-kubernetes-validations` 即 CEL 表达式）。不合法的对象**根本不会被存进 etcd**，`kubectl apply` 会直接报错。这一层是「声明式」的，由 Kubernetes 集群强制执行，与 NIC 进程无关。
- **第二道：控制器自己的语义校验**。即使对象通过了第一道、被存进了 etcd，NIC 拿到它后还会在 Go 代码里再做一次更深、更「懂业务」的校验（跨字段、跨资源、依赖运行期开关、解析 NGINX 特有格式）。不通过的资源会被 NIC **拒绝纳入内存模型**，并把它的 `.status.state` 置为 `Rejected`。

本讲的核心就是：**第一道由 kubebuilder 标记生成，第二道由 validation 包手写**，二者各管一段、互补存在。

### 2.2 field.ErrorList：Kubernetes 风格的「错误收集器」

NIC 的校验函数几乎都返回 `field.ErrorList`（来自 `k8s.io/apimachinery/pkg/util/validation/field`）。它的设计哲学是**一次性收集所有错误**，而不是遇到第一个就返回——这样用户能一次看全所有问题，而不是改一条、apply、再报下一条。

- `field.Invalid(path, value, msg)`：值非法。
- `field.Required(path, msg)`：必填字段缺失。
- `field.Forbidden(path, msg)`：当前上下文不允许该字段（例如「需要开启 cert-manager」）。
- `field.Duplicate(path, value)`：重复。
- `path`（`*field.Path`）用 `field.NewPath("spec").Child("host")` 之类构造，精确定位到出问题的字段，最终拼出类似 `spec.host` 这样的错误路径。

最后用 `allErrs.ToAggregate()` 把列表聚合成一个 `error` 返回。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/apis/configuration/validation/virtualserver.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go) | VirtualServer/VirtualServerRoute 的语义校验（运行期，Go） |
| [pkg/apis/configuration/validation/transportserver.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/transportserver.go) | TransportServer 的语义校验 |
| [pkg/apis/configuration/validation/common.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/common.go) | 跨资源复用的格式校验（路径、转义字符串、size/time、NGINX 变量） |
| [pkg/apis/configuration/v1/types.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) | CRD 类型真相源；`+kubebuilder:*` 标记是 schema 的「源头」 |
| [config/crd/bases/k8s.nginx.org_virtualservers.yaml](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/config/crd/bases/k8s.nginx.org_virtualservers.yaml) | 由标记生成的 VirtualServer CRD（OpenAPI schema） |
| [Makefile](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile) | `update-codegen` / `update-crds` / `verify-codegen` 三个目标的入口 |
| [hack/update-codegen.sh](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/hack/update-codegen.sh) | 调用 code-generator 生成 DeepCopy + typed client |
| [cmd/nginx-ingress/main.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go) | 用运行期 flag 构造各 Validator 的地方 |

---

## 4. 核心概念与源码讲解

### 4.1 校验函数组织：Validator + field.ErrorList

#### 4.1.1 概念说明

NIC 的语义校验（第二道）按「每种资源一个 Validator」组织。每个 Validator 是一个**带配置的结构体**——它持有若干布尔开关（是否 Plus、是否启用了 DoS、是否启用了 cert-manager……），这些开关决定了「当前部署」下哪些字段才合法。

这一点非常关键：**有些校验规则依赖运行期配置**，而 OpenAPI schema 是静态的、编译期就定死的，它**无法知道**「这个集群有没有开 cert-manager」。于是这类「上下文相关」的规则只能放在 Go 校验层。

`VirtualServerValidator` 用了「函数选项（functional options）」模式来构造，默认全为 `false`，再按部署形态逐个打开。

#### 4.1.2 核心流程

```text
main.go 拿到运行期 flag（-enable-cert-manager 等）
        │
        ▼
NewVirtualServerValidator(IsPlus(...), IsCertManagerEnabled(...), ...)
        │  装配出一个带开关的 Validator
        ▼
传给 Configuration（内存模型）
        │
        ▼
sync 时取到 VS → Configuration.AddOrUpdateVirtualServer(vs)
        │
        ▼
virtualServerValidator.ValidateVirtualServer(vs)
        │  内部：validateVirtualServerSpec → 一串 validateXxx，收集成 field.ErrorList
        ▼
返回 nil  → 纳入内存模型
返回 err  → 从内存模型删除，并把 .status.state 置为 Rejected
```

#### 4.1.3 源码精读

Validator 结构体与选项构造：

```go
// VirtualServerValidator validates a VirtualServer/VirtualServerRoute resource.
type VirtualServerValidator struct {
	isPlus                       bool
	isDosEnabled                 bool
	isCertManagerEnabled         bool
	isExternalDNSEnabled         bool
	isDirectiveAutoadjustEnabled bool
}

// IsCertManagerEnabled modifies the VirtualServerValidator to set the isCertManagerEnabled option.
func IsCertManagerEnabled(cm bool) VsvOption {
	return func(v *VirtualServerValidator) {
		v.isCertManagerEnabled = cm
	}
}
```

这段定义了 Validator 的「配置面」：`pkg/apis/configuration/validation/virtualserver.go:46-103`。

入口函数把整个 spec 的校验拆成一段段，各自往同一个 `allErrs` 里追加：

```go
func (vsv *VirtualServerValidator) validateVirtualServerSpec(spec *v1.VirtualServerSpec, fieldPath *field.Path, namespace string) field.ErrorList {
	allErrs := field.ErrorList{}

	allErrs = append(allErrs, validateHost(spec.Host, fieldPath.Child("host"))...)
	allErrs = append(allErrs, vsv.validateTLS(spec.TLS, fieldPath.Child("tls"))...)
	allErrs = append(allErrs, validatePolicies(spec.Policies, fieldPath.Child("policies"), namespace)...)

	upstreamErrs, upstreamNames := vsv.validateUpstreams(spec.Upstreams, fieldPath.Child("upstreams"))
	allErrs = append(allErrs, upstreamErrs...)

	allErrs = append(allErrs, vsv.validateVirtualServerRoutes(spec.Routes, fieldPath.Child("routes"), upstreamNames, namespace)...)
	// ... dos / externalDNS / add-header-inherit
	return allErrs
}
```

完整见 [pkg/apis/configuration/validation/virtualserver.go:125-147](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L125-L147)。注意 `fieldPath.Child("host")` 这种链式路径，让错误最终能精确指向 `spec.host`。

校验结果如何反馈给用户？看内存模型里的调用点：

```go
func (c *Configuration) AddOrUpdateVirtualServer(vs *conf_v1.VirtualServer) ([]ResourceChange, []ConfigurationProblem) {
	// ...
	if !c.hasCorrectIngressClass(vs) {
		delete(c.virtualServers, key)
	} else {
		validationError = c.virtualServerValidator.ValidateVirtualServer(vs)
		if validationError != nil {
			delete(c.virtualServers, key)            // 校验失败 → 从内存模型剔除
		} else {
			c.balanceUpstreamProxies(vs.Spec.Upstreams)
			c.virtualServers[key] = vs               // 校验通过 → 纳入
		}
	}
```

完整见 [internal/k8s/configuration.go:571-589](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L571-L589)。紧随其后，若 `validationError != nil`，会构造一个 `ConfigurationProblem{IsError: true, Reason: EventReasonRejected, ...}`（[internal/k8s/configuration.go:591-601](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configuration.go#L591-L601)），最终由 status updater 写回 `.status.state=Rejected`、`.status.reason=<错误信息>`。这就是「第二道校验」对用户的可见出口。

> 补充：除了内存模型这里，控制器在 sync 入口还会再调一次 `ValidateVirtualServer(vsNew)`（[internal/k8s/controller.go:4338](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L4338)），属于纵深防御。

`common.go` 里还有一批**跨资源复用**的 NGINX 格式校验，VirtualServer/TransportServer/Policy 都会调用，例如校验 NGINX 路径格式、转义字符串、size/time 格式：

```go
// ValidateEscapedString validates an escaped string.
func ValidateEscapedString(body string, examples ...string) error {
	if !escapedStringsFmtRegexp.MatchString(body) {
		msg := validation.RegexError(escapedStringsErrMsg, escapedStringsFmt, examples...)
		return errors.New(msg)
	}
	return nil
}
```

见 [pkg/apis/configuration/validation/common.go:21-28](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/common.go#L21-L28)。它是 NGINX 配置注入安全的第一道防线（u6-l1 会展开），这里只需记住：**凡是会进入 NGINX 配置的用户字符串，都要过格式校验**。

#### 4.1.4 代码实践：观察 Validator 的运行期开关

1. **目标**：理解「同一段校验代码，在不同部署形态下行为不同」。
2. **步骤**：
   - 打开 [cmd/nginx-ingress/main.go:279-286](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/cmd/nginx-ingress/main.go#L279-L286)，看 `NewVirtualServerValidator(...)` 的 5 个选项分别绑了哪些 flag。
   - 再打开 [pkg/apis/configuration/validation/virtualserver.go:220-235](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L220-L235) 的 `validateTLSCmFields`。
3. **现象**：当 `isCertManagerEnabled == false` 而用户在 `tls.cert-manager` 里填了内容时，会返回 `field.Forbidden(..., "field requires cert-manager enablement")`。
4. **预期结果**：你能说清「为什么这条规则必须放在 Go 层、不能放在 CRD schema 里」——因为 schema 是静态的，不知道某个 NIC 实例有没有开 `--enable-cert-manager`。
5. 待本地验证：若你有一个集群，可对比「带与不带 `-enable-cert-manager` 启动时」同一份带 cert-manager 字段的 VirtualServer，其 `.status.reason` 的差异。

#### 4.1.5 小练习与答案

- **练习 1**：`ValidateVirtualServer` 返回的是 `error`，但内部却用 `field.ErrorList`，二者如何衔接？
  - **答案**：内部各 `validateXxx` 收集 `field.ErrorList`，最后在 [virtualserver.go:106-109](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L106-L109) 用 `allErrs.ToAggregate()` 聚合成单个 `error` 返回，从而把「多错误一次性收集」对上层隐藏成「一个 error」。

- **练习 2**：`field.Forbidden`、`field.Invalid`、`field.Required` 三者在语义上分别适合什么场景？
  - **答案**：`Required` 用于必填却为空；`Invalid` 用于值格式/取值不合法（如 host 不是合法 DNS 名）；`Forbidden` 用于「值本身没格式问题，但当前上下文不允许」（如未开启某 feature 就用了对应字段）。

---

### 4.2 CEL / kubebuilder 标记校验：声明式 schema 层

#### 4.2.1 概念说明

第一道校验（API Server 侧）的「源头」不是 YAML，而是 `types.go` 里结构体字段上方的 **kubebuilder 标记**（以 `// +kubebuilder:` 开头的注释）。这些标记是给人读的 Go 注释，但会被工具 `controller-gen` 解析，**生成** CRD 的 OpenAPI v3 schema。

常见标记与生成的 schema 对应关系：

| kubebuilder 标记 | 生成的 schema 字段 | 含义 |
| --- | --- | --- |
| `+kubebuilder:validation:Enum=a;b;c` | `enum: [a, b, c]` | 只允许枚举值 |
| `+kubebuilder:validation:Pattern=^\d+[kKmM]?$` | `pattern: ^\d+[kKmM]?$` | 正则约束 |
| `+kubebuilder:validation:Optional` / `:Required` | 字段是否列在 `required:` | 必填/可选 |
| `+kubebuilder:validation:XValidation:rule="...",message="..."` | `x-kubernetes-validations:` | CEL 跨字段表达式 |

最后一项 **CEL（Common Expression Language）** 是关键升级：它让 schema 能表达**跨字段**的约束——这是单纯 `enum`/`pattern` 做不到的，而又不需要写 Go 代码。规则在 API Server 准入时由 kube-apiserver 内置的 CEL 引擎求值，对象不合法则 `kubectl apply` 直接失败。

#### 4.2.2 核心流程

```text
types.go 里的注释标记           （人写的真相源）
        │  controller-gen 解析
        ▼
config/crd/bases/*.yaml         （生成的 OpenAPI schema）
        │  kubectl apply CRD
        ▼
kube-apiserver 在准入时校验用户对象
        │  不合法 → 直接拒绝（对象不进 etcd）
        ▼
合法 → 存入 etcd → NIC 才看得到
```

#### 4.2.3 源码精读

以 **OIDC Policy** 的一个跨字段约束为例。规则语义是：「`trustedCertSecret` 只有在 `sslVerify == true` 时才能设置」。

真相源（types.go 里的标记注释）：

```go
// The OpenID Connect policy configures NGINX to authenticate client requests ...
// +kubebuilder:validation:XValidation:rule="(self.sslVerify == true) || (self.sslVerify == false && !has(self.trustedCertSecret))",message="trustedCertSecret can be set only if sslVerify is true"
OIDC *OIDC `json:"oidc"`
```

见 [pkg/apis/configuration/v1/types.go:802-804](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L802-L804)。注意 `self` 指「当前这个 OIDC 对象」，`has(self.trustedCertSecret)` 判断字段是否被设置。

`make update-crds` 之后，它被翻译成 Policy CRD 里的 CEL 段：

```yaml
type: object
x-kubernetes-validations:
- message: trustedCertSecret can be set only if sslVerify is true
  rule: (self.sslVerify == true) || (self.sslVerify == false && !has(self.trustedCertSecret))
```

见 [config/crd/bases/k8s.nginx.org_policies.yaml:716-719](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/config/crd/bases/k8s.nginx.org_policies.yaml#L716-L719)。`rule` 与 `message` 与标记里的内容逐字对应——这就是「标记 → schema」的实物映射。

> 这种「跨字段」约束之所以优先用 CEL 而不是 Go 校验，是因为它能在对象进 etcd **之前**就被拒绝，用户 `kubectl apply` 立刻看到错误，无需等控制器回写 status。

#### 4.2.4 代码实践：解读一条 CEL 规则

1. **目标**：学会读 `XValidation` 标记，并理解 `self` / `has()` 的语义。
2. **步骤**：阅读 [types.go:803](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L803) 这条规则，分别代入两种用户输入：
   - 输入 A：`sslVerify: true` 且设置了 `trustedCertSecret`。
   - 输入 B：`sslVerify: false` 且设置了 `trustedCertSecret`。
3. **现象/预期结果**：A 命中左半 `(self.sslVerify == true)` → 通过；B 左半为假、右半 `self.sslVerify == false && !has(...)` 中 `!has(...)` 为假 → 整条为假 → 校验失败，返回 message。结论：CEL 把「两个字段之间的依赖」表达成了一条布尔表达式。
4. 待本地验证：可在装好该 CRD 的集群里 `kubectl apply` 一份 B 形态的 OIDC Policy，观察 API Server 直接报错（而非等到 NIC 回写 status）。

#### 4.2.5 小练习与答案

- **练习**：CEL 的 `x-kubernetes-validations` 和 Go 校验层各有什么 schema 做不到、必须靠 Go 的场景？
  - **答案**：CEL 能做跨字段、同对象内的静态约束；但**跨资源引用**（如 Policy 引用的 Secret 是否存在、VS 的 host 是否和别人冲突）、**依赖运行期开关**（是否开了 cert-manager/Plus）、**调用 NGINX 格式解析**（ParseTime/ParseSize）这些，CEL 都无能为力，只能在 Go 校验层完成。

---

### 4.3 CRD YAML 生成：从标记到 OpenAPI schema

#### 4.3.1 概念说明

`config/crd/bases/` 下的 12 份 YAML **不是手写的**，而是 `controller-gen` 扫描 `pkg/apis/...` 的 `types.go` 后**生成**的产物。它们都带 `controller-gen.kubebuilder.io/version: v0.21.0` 注释作为生成标记。因此有一条铁律：**永远不要手改 `config/crd/bases/*.yaml`**，要改就改 `types.go` 的标记，再重新生成。

#### 4.3.2 核心流程：一个标记的完整旅程

以 VirtualServer 的 `add-header-inherit` 字段为例，它只允许 `on`/`off`/`merge` 三个值。

```text
types.go:67   +kubebuilder:validation:Enum=on;off;merge   （AddHeaderInherit 字段上方）
        │  controller-gen
        ▼
virtualservers.yaml:66-76
          add-header-inherit:
            description: ...
            enum: ["on", "off", merge]
            type: string
        │  kubectl apply CRD 后
        ▼
API Server 拒绝任何非 on/off/merge 的取值（apply 阶段即报错）
```

#### 4.3.3 源码精读

真相源（标记，分号分隔枚举值）：

```go
// Controls header inheritance behavior at the server level. Allowed values are: on, off, merge. ...
// +kubebuilder:validation:Enum=on;off;merge
AddHeaderInherit string `json:"add-header-inherit"`
```

见 [pkg/apis/configuration/v1/types.go:66-68](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L66-L68)。

生成产物（schema 里的 `enum`）：

```yaml
add-header-inherit:
  description: 'Controls header inheritance behavior at the server level.
    Allowed values are: on, off, merge. ...'
  enum:
  - "on"
  - "off"
  - merge
  type: string
```

见 [config/crd/bases/k8s.nginx.org_virtualservers.yaml:66-76](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/config/crd/bases/k8s.nginx.org_virtualservers.yaml#L66-L76)。注意 `description` 也来自标记上方的普通注释——controller-gen 连文档都一并生成了（`update-crd-docs` 目标还会据此生成 `docs/crd/*.md`）。

再看一个 `Pattern` 的例子——`client-body-buffer-size` 必须是「数字 + 可选 k/m 后缀」：

```go
// +kubebuilder:validation:Optional
// +kubebuilder:validation:Pattern=`^\d+[kKmM]?$`
```

见 [pkg/apis/configuration/v1/types.go:163-164](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L163-L164)，生成出 [virtualservers.yaml:1145](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/config/crd/bases/k8s.nginx.org_virtualservers.yaml#L1145) 的 `pattern: ^\d+[kKmM]?$`。

> 对比点（重要）：并非所有校验都能进 schema。例如 `tls.redirect.code` 只允许 301/302/307/308，这条规则**只存在于 Go 校验层**（`validRedirectStatusCodes` 与 `validateRedirectStatusCode`，[virtualserver.go:280-292](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L280-L292)），CRD schema 里**没有**对应的 `enum`。它的失败不会在 `kubectl apply` 时报，而是事后体现在 `.status.state=Rejected`。这正好印证了「两层各管一段」。

```go
var validRedirectStatusCodes = map[int]bool{
	301: true, 302: true, 307: true, 308: true,
}

func validateRedirectStatusCode(code int, fieldPath *field.Path) field.ErrorList {
	if _, ok := validRedirectStatusCodes[code]; !ok {
		return field.ErrorList{field.Invalid(fieldPath, code, "status code out of accepted range. accepted values are '301', '302', '307', '308'")}
	}
	return nil
}
```

#### 4.3.4 代码实践：追踪一个字段约束到 schema（本讲主实践）

1. **目标**：任选一个 VirtualServer 字段，说明它的 Go 校验拒绝哪些非法取值，并判断该约束**是否**、**如何**体现在 CRD schema 里。
2. **步骤（A：能进 schema 的约束）**：
   - 字段：`spec.add-header-inherit`。
   - 在 [types.go:66-68](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L66-L68) 找到 `Enum=on;off;merge`。
   - 在 [virtualservers.yaml:66-76](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/config/crd/bases/k8s.nginx.org_virtualservers.yaml#L66-L76) 找到对应的 `enum:`。
   - 结论：填 `merge-foo` 会被 **API Server** 在 `kubectl apply` 时直接拒绝。
3. **步骤（B：只存在于 Go 层的约束）**：
   - 字段：`spec.tls.redirect.code`。
   - 在 [virtualserver.go:280-292](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L280-L292) 看到只允许 301/302/307/308。
   - 在 `config/crd/bases/k8s.nginx.org_virtualservers.yaml` 里**搜不到** `redirect.code` 的 `enum`。
   - 结论：填 `418` 能通过 `kubectl apply`（对象进 etcd），但随后 NIC 会把 `.status.state` 置为 `Rejected`。
4. **需要观察的现象**：A 与 B 两类约束的**失败时机不同**——A 在 apply 阶段、B 在控制器回写 status 阶段。
5. 待本地验证：在有该 CRD 的集群里分别 apply 这两个非法值，确认 A 由 kubectl/api-server 报错、B 由 `kubectl get vs -o yaml` 的 `.status` 报错。

#### 4.3.5 小练习与答案

- **练习**：为什么 `redirect.code` 的枚举没有被写成 `+kubebuilder:validation:Enum`？把它改成 schema 枚举会有什么好处和坏处？
  - **答案**：可能是历史原因或希望保留「校验信息统一在 status 里暴露」的一致性。好处是能更早（apply 时）拒绝；坏处是需要改 types.go + 重新生成 CRD，且 schema 变更需要用户升级 CRD 才生效。一般原则：**能进 schema 的尽量进 schema**（更早失败、无需控制器参与）。

---

### 4.4 codegen 工作流：types.go → DeepCopy → client → CRD

#### 4.4.1 概念说明

改 `types.go` 不只是「加个字段」那么简单。Kubernetes 的 Go 类型有一套**强制的生成依赖**：

- 新增/修改结构体字段后，**DeepCopy 方法**（让对象可被安全复制）必须重新生成，否则编译报错（接口未实现）。
- **typed clientset / informer / lister / applyconfiguration** 也都从 types.go 派生，需要同步。
- **CRD YAML** 从标记派生，需要同步。

这些都是「DO NOT EDIT」的生成产物。Makefile 提供了两个目标把它们串起来，并规定了**不可颠倒的顺序**。

#### 4.4.2 核心流程

```text
你改了 pkg/apis/configuration/v1/types.go
        │
        ├── make update-codegen    （hack/update-codegen.sh）
        │     ├── kube::codegen::gen_helpers  → zz_generated.deepcopy.go
        │     └── kube::codegen::gen_client   → pkg/client/{clientset,informers,listers,applyconfiguration}
        │
        └── make update-crds        （controller-gen + kustomize）
              ├── controller-gen crd paths=./pkg/apis/... → config/crd/bases/*.yaml（12 份）
              ├── kustomize build config/crd            → deploy/crds.yaml
              ├── kustomize build .../app-protect-dos   → deploy/crds-nap-dos.yaml
              ├── kustomize build .../app-protect-waf   → deploy/crds-nap-waf.yaml
              └── update-crd-docs                      → docs/crd/*.md
```

#### 4.4.3 源码精读

`update-codegen` 目标只是调用一个脚本：

```makefile
.PHONY: update-codegen
update-codegen: ## Generate code
	./hack/update-codegen.sh
```

见 [Makefile:126-128](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L126-L128)。脚本内部用 `k8s.io/code-generator` 的 `kube_codegen.sh` 做两件事（[hack/update-codegen.sh](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/hack/update-codegen.sh)）：

```bash
kube::codegen::gen_helpers --boilerplate .../boilerplate.go.txt "${SCRIPT_ROOT}/pkg/apis"
kube::codegen::gen_client --with-watch --with-applyconfig \
    --output-dir "${SCRIPT_ROOT}/pkg/client" --output-pkg "${THIS_PKG}/pkg/client" \
    --boilerplate .../boilerplate.go.txt "${SCRIPT_ROOT}/pkg/apis"
```

- `gen_helpers` → 生成 `pkg/apis/configuration/v1/zz_generated.deepcopy.go`（每个类型实现 `DeepCopy()`）。
- `gen_client` → 生成 `pkg/client/` 下的 clientset、informers、listers（如 [pkg/client/listers/configuration/v1/virtualserver.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/client/listers/configuration/v1/virtualserver.go)）和 applyconfiguration（[pkg/client/applyconfiguration/configuration/v1/](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/client/applyconfiguration/configuration/v1/)）。

`update-crds` 目标用 `go run` 直接跑 controller-gen，再跑 kustomize 聚合成部署用的单文件：

```makefile
.PHONY: update-crds
update-crds: ## Update CRDs
	go run sigs.k8s.io/controller-tools/cmd/controller-gen crd paths=./pkg/apis/... output:crd:artifacts:config=config/crd/bases
	@kustomize version || ...
	kustomize build config/crd >deploy/crds.yaml
	kustomize build config/crd/app-protect-dos --load-restrictor='LoadRestrictionsNone' >deploy/crds-nap-dos.yaml
	kustomize build config/crd/app-protect-waf --load-restrictor='LoadRestrictionsNone' >deploy/crds-nap-waf.yaml
	$(MAKE) update-crd-docs
```

见 [Makefile:130-137](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L130-L137)。`controller-gen` 的 `paths=./pkg/apis/...` 就是它扫描标记的入口，`output:crd:artifacts:config=config/crd/bases` 指定把生成的 CRD 写到 bases 目录。

**防漂移机制**：`verify-codegen` 会把当前 `pkg/` 备份，重新跑一次 `update-codegen.sh`，再 `diff`——若有差异就报「out of date, 请运行 update-codegen.sh」并失败（见 [hack/verify-codegen.sh](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/hack/verify-codegen.sh)）。它甚至被挂进了默认的 `all` 目标：

```makefile
all: test lint verify-codegen update-crds debian-image
```

见 [Makefile:76-77](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/Makefile#L76-L77)。这意味着 CI 构建 `make all` 时，生成产物必须与源码严格一致。

**Makefile 目标速查表**：

| 目标 | 工具 | 产出 | 何时用 |
| --- | --- | --- | --- |
| `make update-codegen` | k8s code-generator | `zz_generated.deepcopy.go` + `pkg/client/*` | 改了 types.go 结构体后 |
| `make update-crds` | controller-gen + kustomize | `config/crd/bases/*.yaml` + `deploy/crds*.yaml` + `docs/crd/*.md` | 改了 types.go 标记后 |
| `make verify-codegen` | update-codegen + diff | 无（只校验） | CI 防漂移 |

**强制顺序**：先 `update-codegen`（补 DeepCopy/client，保证能编译），再 `update-crds`（生成 schema）。顺序反了会导致 controller-gen 跑在一个「DeepCopy 都没更新」的状态上。

#### 4.4.4 代码实践：跑一遍生成并观察 diff

1. **目标**：亲眼看到「改 types.go → 生成产物变化」的链路。
2. **步骤**：
   - 先确保工作区干净（`git status`）。
   - 在 [pkg/apis/configuration/v1/types.go:68](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L68) 的 `AddHeaderInherit` 上方，把 `Enum=on;off;merge` 改成 `Enum=on;off;merge;auto`（**仅本练习，做完请还原**）。
   - 依次运行 `make update-codegen` 再 `make update-crds`。
   - 运行 `git status` 与 `git diff config/crd/bases/k8s.nginx.org_virtualservers.yaml`。
3. **需要观察的现象**：
   - `zz_generated.deepcopy.go` 通常**不变**（因为没加字段，只改了注释标记）。
   - `config/crd/bases/k8s.nginx.org_virtualservers.yaml` 的 `enum` 列表里会多出一个 `- auto`。
   - `deploy/crds.yaml` 也会同步变化（kustomize 聚合产物）。
4. **预期结果**：你能指认出「标记改动」只影响 `update-crds` 的产物，而不影响 `update-codegen` 的产物——这正是两个目标职责分离的体现。
5. **收尾**：`git checkout -- .` 还原改动（本讲强调**不修改源码**，此步仅用于理解生成链路）。

> 注意：本环境未必装齐 controller-gen/kustomize/code-generator，若命令报缺工具，记为「待本地验证」即可，重点是读懂上面的 diff 关系。

#### 4.4.5 小练习与答案

- **练习 1**：如果你只加了字段却忘了跑 `update-codegen`，会发生什么？
  - **答案**：该类型缺少新生成字段的 DeepCopy 实现（或新类型没实现 `runtime.DeepCopyObject`），**编译直接报错**。这是 codegen 链路最直接的「强制力」。

- **练习 2**：`make all` 为什么把 `verify-codegen` 和 `update-crds` 都放进去？
  - **答案**：`verify-codegen` 保证 DeepCopy/client 与 types.go 一致（防有人手改生成代码或忘了提交），`update-crds` 保证 CRD YAML 是最新的——二者都是「生成产物必须与真相源同步」的守护。注意 `all` 里 `update-crds` 会**重新生成**（而非校验），属于发布前确保产物最新的做法。

---

## 5. 综合实践：为一个新字段设计「双层」校验

把本讲的四条线索串起来，完成下面这个贯穿性小任务。

**场景**：假设你要给 VirtualServer 的 `upstream` 加一个新字段 `loadBalancingAlgorithm`，要求它只能取特定值，并且其中某个值只在 NGINX Plus 下合法。请设计它的双层校验。

1. **schema 层（声明式）**：在 [pkg/apis/configuration/v1/types.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) 的该字段上方加 `+kubebuilder:validation:Enum=round_robin;least_conn;...`，覆盖**所有**可能取值（含 Plus 专属值）。运行 `make update-crds`，确认 `config/crd/bases/k8s.nginx.org_virtualservers.yaml` 出现对应 `enum`。这层保证「拼写错误」在 apply 阶段就被拒。
2. **Go 层（运行期）**：在 [pkg/apis/configuration/validation/virtualserver.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go) 的 `validateUpstreams` 链路里加一个分支：当取值是 Plus 专属值而 `vsv.isPlus == false` 时，返回 `field.Forbidden(...)`。参考现成的 `validateUpstreamLBMethod`（[virtualserver.go:349-366](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/validation/virtualserver.go#L349-L366)），它已经按 `isPlus` 分别调用 `ParseLBMethodForPlus` / `ParseLBMethod`。
3. **codegen 收尾**：若你**新增**了字段（不只是改标记），必须 `make update-codegen`（补 DeepCopy）再 `make update-crds`。
4. **自检**：画一张表，左列写「非法输入」，右列写「它在哪一层被拒（API Server 还是 NIC status）」。例如：拼写错的算法名 → schema 层；OSS 部署里用了 Plus 专属算法 → Go 层（status=Rejected）。

这个任务综合了 4.1（Validator 开关）、4.2（Enum/CEL 标记）、4.3（schema 映射）、4.4（codegen 顺序），做完你就掌握了「给 CRD 加字段并配齐校验」的完整闭环。

## 6. 本讲小结

- NIC 的 CRD 校验是**两层**：声明式的 OpenAPI/CEL schema（API Server 准入时强制，由 kubebuilder 标记生成）+ 命令式的 Go 校验（控制器运行期，手写在 `validation` 包）。
- Go 校验按「每种资源一个带开关的 Validator」组织，用 `field.ErrorList` 一次性收集所有错误；校验失败的资源被剔出内存模型并把 `.status.state` 置为 `Rejected`。
- kubebuilder 标记是 schema 的真相源：`Enum`→`enum:`、`Pattern`→`pattern:`、`XValidation`→`x-kubernetes-validations:`（CEL）。`config/crd/bases/*.yaml` 由 `controller-gen` 生成，禁止手改。
- 不是所有约束都能进 schema：跨资源引用、依赖运行期开关（Plus/cert-manager/DoS）、NGINX 格式解析只能留在 Go 层。
- 改 `types.go` 后必须 `make update-codegen`（DeepCopy + client）→ `make update-crds`（CRD + deploy + docs），顺序不可颠倒；`verify-codegen` 在 CI 中防漂移。

## 7. 下一步学习建议

- 下一讲 **u2-l4（Policy CRD 与可复用策略模型）** 会聚焦 Policy 这一最复杂的 CRD，本讲里你看到的 Policy CEL 校验（`x-kubernetes-validations`）和 `policy.go` 校验都将在那里展开。
- 若你想看「校验失败如何变成 status」，可直接读 [internal/k8s/status.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/status.go)（u3-l7 会系统讲解 leader 选举与 status 回写）。
- 若你对 NGINX 配置注入安全（`ValidateEscapedString` / `containsDangerousChars`）感兴趣，可预习 u6-l1。
- 想动手扩展 CRD 字段/注解/Policy 的完整工作流，见 u8-l1 / u8-l2 / u8-l3，它们正是以本讲的 codegen 链路为骨架。
