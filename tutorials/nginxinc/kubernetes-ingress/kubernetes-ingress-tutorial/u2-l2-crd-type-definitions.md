# CRD 类型定义源码：types.go

## 1. 本讲目标

本讲带你进入 NGINX Ingress Controller（以下简称 NIC）的「数据模型层」——也就是 `pkg/apis/configuration/v1/types.go` 这个文件。它是整个项目对外的 API 契约：用户用 `kubectl apply` 提交的 YAML，最终都会被反序列化成这里的 Go 结构体。

学完本讲，你应该能够：

1. 读懂 `types.go` 里每一个 CRD（Custom Resource Definition）的 Go 结构体，看懂字段上的 `json` tag 与 `// +kubebuilder:...` 注释。
2. 理解这些注释（kubebuilder marker）如何驱动 CRD YAML 的自动生成与字段校验。
3. 在脑中建立一条完整链路：**用户 YAML → Go 类型（本讲）→ 校验 → 控制器内存模型 → NGINX 配置**，明白本讲处于这条链路的「源头」位置。
4. 动手定位 `VirtualServerSpec`，说出它的每个字段会落到最终 NGINX 配置里的 upstream 还是 location。

> 本讲只讲「数据是怎么定义的」，不讲「控制器怎么监听、怎么翻译」。那些内容在 u3（控制器）和 u4（配置生成）单元。

---

## 2. 前置知识

在开始前，请确保你了解以下概念（u2-l1 已建立这些认知，这里只做最小回顾）：

- **CRD（Custom Resource Definition）**：Kubernetes 允许你「自定义一种新的资源类型」。NIC 就是靠 CRD 引入了 `VirtualServer`、`TransportServer`、`Policy` 等标准 Kubernetes 没有的资源。
- **GVK（Group / Version / Kind）**：每个资源类型由三段定位。本项目的所有 CRD 都属于 Group `k8s.nginx.org`，Version `v1`，Kind 例如 `VirtualServer`。
- **Spec 与 Status**：几乎所有 Kubernetes 资源都分成两部分——`spec` 是用户声明的「期望状态」，`status` 是控制器写回的「实际状态」。
- **NGINX 配置的最小单元**：`upstream`（一组后端服务器，做负载均衡）和 `location`（一条 URL 匹配规则，决定请求转发到哪个 upstream）。理解这两个词，是看懂「字段如何影响配置」的前提。
- **json tag**：Go 结构体字段后 `` `json:"host"` `` 这样的标注，决定 YAML/JSON 字段名到 Go 字段的映射。

> 小提示：Go 里 CRD 的结构体定义和普通结构体没有本质区别，区别只在那些 `// +kubebuilder:...` 注释——它们是写给「代码生成器」看的指令，不是给 Go 编译器看的。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [pkg/apis/configuration/v1/types.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) | **本讲主角**。全部 CRD 的 Go 结构体定义，项目数据真相源。 |
| [pkg/apis/configuration/v1/doc.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/doc.go) | 包级注释，声明 API group 与 deepcopy 生成开关。 |
| [pkg/apis/configuration/v1/register.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/register.go) | 把所有 Kind 注册到 runtime Scheme，声明 GroupVersion=v1。 |
| [pkg/apis/configuration/group_info.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/group_info.go) | 定义 `GroupName = "k8s.nginx.org"`。 |
| [pkg/apis/configuration/v1/zz_generated.deepcopy.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/zz_generated.deepcopy.go) | 自动生成的深拷贝代码，**禁止手改**。 |
| [docs/developer/architecture.md](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/docs/developer/architecture.md) | 五层架构说明，把本讲定位在「Data Model」层。 |

本项目共定义 5 个 CRD Kind：`VirtualServer`、`VirtualServerRoute`、`TransportServer`、`GlobalConfiguration`、`Policy`。本讲会逐一拆解它们在 `types.go` 里的定义。

---

## 4. 核心概念与源码讲解

### 4.1 kubebuilder 标注与 CRD 类型骨架

#### 4.1.1 概念说明

`types.go` 是一个「纯定义文件」——它只描述数据长什么样，**不包含任何业务逻辑**。架构文档对此有一条硬性不变量：

> Data model（`types.go`）**禁止 import** `internal/configs` 或 `internal/k8s`。它是纯粹的 API 定义，没有业务逻辑。

这种「纯净」很重要：它意味着这份定义既能被控制器用来解析用户 YAML，也能被代码生成器读取、自动产出 CRD 安装清单和客户端代码。要让生成器「看懂」Go 结构体，靠的就是字段和类型上方那些以 `// +` 开头的注释，称为 **marker（标注）**。两类 marker 最关键：

- `// +k8s:...`：指导 Kubernetes 官方代码生成器（deepcopy-gen、client-gen）。
- `// +kubebuilder:...`：指导 controller-gen，决定 CRD YAML 里的 schema、scope、status 子资源、`kubectl get` 列等。

理解了 marker，你就能反向预测「改一个字段会在 CRD YAML 和客户端里产生什么变化」。

#### 4.1.2 核心流程

一个 CRD 类型从定义到可用，经过这样的流水线：

```text
types.go (Go 结构体 + marker)
   │
   ├─ make update-codegen  ──► zz_generated.deepcopy.go（深拷贝）
   │                        ──► pkg/client/（typed clientset / informers / listers）
   │
   └─ make update-crds     ──► config/crd/bases/*.yaml（CRD 安装清单，含 schema）
```

换句话说：**你只改 `types.go`，其余文件都由工具再生**。这也是为什么本文件是「数据真相源」——它改了，下游全要跟着重新生成（详见 u2-l3）。

每个 CRD 类型都遵循同一个骨架：

```text
type XxxServer struct {
    metav1.TypeMeta      // 内联：apiVersion / kind
    metav1.ObjectMeta    // 内联：name / namespace / labels ...
    Spec   XxxSpec       // 期望状态（用户填写）
    Status XxxStatus     // 实际状态（控制器回写）
}
type XxxServerList struct { ... Items []XxxServer }   // 列表类型，kubectl 会用到
```

API group 的归属由 `doc.go` 和 `group_info.go` 固定。

#### 4.1.3 源码精读

**① 声明 API group 与 deepcopy 生成开关**——包级 marker 写在 `doc.go`：

[doc.go:L1-L4](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/doc.go#L1-L4)：`// +k8s:deepcopy-gen=package` 让生成器为本包每个类型生成深拷贝；`// +groupName=k8s.nginx.org` 声明本包属于该 API group。

```go
// +k8s:deepcopy-gen=package
// +groupName=k8s.nginx.org

// Package v1 is the v1 version of the API.
package v1
```

[group_info.go:L4-L5](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/group_info.go#L4-L5)：定义 `GroupName = "k8s.nginx.org"`，所有 CRD 共享这个 group。

**② 注册 5 个 Kind 到 Scheme，并固定 Version=v1**：

[register.go:L31-L47](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/register.go#L31-L47)：`addKnownTypes` 把 `VirtualServer`、`VirtualServerRoute`、`TransportServer`、`GlobalConfiguration`、`Policy`（及各自的 List）注册进 Scheme，这样控制器才能把它们当作 `runtime.Object` 解析。

```go
func addKnownTypes(scheme *runtime.Scheme) error {
	scheme.AddKnownTypes(
		SchemeGroupVersion,
		&VirtualServer{},
		&VirtualServerList{},
		// ... VSR / TS / GC / Policy 及其 List
		&Policy{},
		&PolicyList{},
	)
	metav1.AddToGroupVersion(scheme, SchemeGroupVersion)
	return nil
}
```

`SchemeGroupVersion` 在 register.go 顶部被定义为 `{Group: "k8s.nginx.org", Version: "v1"}`，这就是 GVK 里「GV」两段的来源。

**③ CRD 类型的统一骨架——以 `VirtualServer` 为例**：

[types.go:L36-L42](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L36-L42)：`TypeMeta`+`ObjectMeta`+`Spec`+`Status` 四件套。其余 4 个 CRD 都是同样的骨架。

```go
type VirtualServer struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VirtualServerSpec `json:"spec"`
	Status VirtualServerStatus `json:"status"`
}
```

**④ 驱动生成的关键 marker——`VirtualServer` 上方的注释块**：

[types.go:L23-L33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L23-L33)：这一组 marker 直接决定生成的 CRD YAML 长什么样。

```go
// +genclient                                          // 生成 typed clientset
// +k8s:deepcopy-gen:interfaces=k8s.io/apimachinery/pkg/runtime.Object  // 让它成为 runtime.Object
// +kubebuilder:validation:Optional                     // 默认所有字段都可选
// +kubebuilder:resource:shortName=vs                   // kubectl get vs
// +kubebuilder:subresource:status                      // status 是独立子资源，可单独更新
// +kubebuilder:printcolumn:name="State",...JSONPath=`.status.state`  // kubectl get 的列
```

下表把常见 marker 和它在生成产物里的作用对应起来（这是本讲最重要的「字典」）：

| Marker | 作用 | 在哪里能看到效果 |
| --- | --- | --- |
| `+genclient` | 生成该类型的 typed client | `pkg/client/clientset/versioned/` |
| `+k8s:deepcopy-gen:interfaces=...runtime.Object` | 让类型实现 `runtime.Object`，可注册进 Scheme | 能被 `addKnownTypes` 接受 |
| `+kubebuilder:validation:Optional` | 该类型字段默认全部可选（缺省即零值） | CRD schema 里字段不加 `required` |
| `+kubebuilder:resource:shortName=vs` | 资源短名 | `kubectl get vs` 生效 |
| `+kubebuilder:subresource:status` | 声明 status 子资源 | CRD YAML 的 `subresources.status` |
| `+kubebuilder:printcolumn:...` | `kubectl get` 自定义列 | 终端表格里的列 |
| `+kubebuilder:storageversion` | 多版本时的「存储版本」 | CRD 的 `storage: true` |
| `+kubebuilder:validation:Enum=on;off;merge` | 枚举校验 | schema 的 `enum` |
| `+kubebuilder:validation:Pattern=...` | 正则校验 | schema 的 `pattern` |
| `+kubebuilder:validation:XValidation:rule=...` | CEL 跨字段校验 | schema 的 `x-kubernetes-validations` |

例如 [types.go:L67-L68](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L67-L68) 里 `AddHeaderInherit` 字段上的 `+kubebuilder:validation:Enum=on;off;merge`，会让 CRD 拒绝这三个值以外的取值——校验发生在 API Server，请求根本进不到控制器。

#### 4.1.4 代码实践

**实践目标**：把 marker 和生成产物对应起来，确认「改 `types.go` 真的会改变 CRD YAML」。

**操作步骤**：

1. 打开 [types.go:L23-L33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L23-L33)，记下 `+kubebuilder:resource:shortName=vs` 和 `+kubebuilder:printcolumn:name="Host"`。
2. 打开生成产物 `config/crd/bases/k8s.nginx.org_virtualservers.yaml`，搜索 `shortNames:` 与 `jsonPath: .spec.host`。
3. 对比两处，确认 `vs` 这个短名、`Host` 这一列，都源于 marker。

**需要观察的现象**：marker 注释里的内容，几乎一字不差地出现在生成的 YAML 对应字段里。

**预期结果**：你会看到 marker 是 CRD YAML 的「源头」，改 marker 后跑 `make update-crds`，YAML 就跟着变。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `VirtualServer` 上方要写 `+kubebuilder:subresource:status`？如果不写会怎样？
**答案**：声明 `status` 是子资源后，控制器可以「只更新 status 而不触发 spec 的 watch」、且普通用户即使没有改 spec 的权限也能被授权改 status。不写则 status 会和 spec 混在同一个资源版本里，无法独立更新。

**练习 2**：`+kubebuilder:validation:Optional` 写在类型上，和写在字段上的 `+kubebuilder:validation:Required` 是什么关系？
**答案**：类型级的 `Optional` 设定「缺省宽松」的总基调（字段不写 required）；某个字段若必须提供，再在字段上单独加 `Required` 覆盖。这是「先松后紧」的常见写法。

---

### 4.2 VirtualServer 与 VirtualServerRoute 结构

#### 4.2.1 概念说明

`VirtualServer`（简称 VS）是 NIC 最核心的 L7 路由资源。它的设计哲学（u2-l1 已讲）是：把 **upstream 提升为一等命名对象**，路由（route）通过名字去引用它，从而天然支持流量切分、内容路由、多动作。这与标准 Ingress「把后端 service 内联在 path 里」形成鲜明对比。

`VirtualServerRoute`（简称 VSR）则是 VS 的「子路由片段」——它只声明 `upstreams` 和 `subroutes`，再被某个 VS 通过 `route` 字段按名字装配进来，用于跨团队、跨命名空间的模块化复用。它的 `host` 必须与引用它的 VS 一致。

#### 4.2.2 核心流程

VS 的逻辑结构可以画成一张「服务器 + 路由表」：

```text
VirtualServer
├─ host: cafe.example.com        ──► 生成 NGINX server 块
├─ tls: { secret }               ──► server 块的 ssl 配置
├─ upstreams: [ {name, service, port, ...} ]  ──► 生成 NGINX upstream 块（按 name）
└─ routes: [ {path, action/splits/matches} ]  ──► 生成 location 块
        └─ action.pass: "tea"    ──► location 用 proxy_pass 引用名为 tea 的 upstream
```

关键关系：**`upstreams` 决定 upstream 块，`routes` 决定 location 块**；route 里的 `action.pass` / `splits` / `matches` 用 upstream 的 `name` 把两者连起来。这是本讲综合实践要回答的核心问题。

#### 4.2.3 源码精读

**① `VirtualServerSpec`——VS 的全部声明能力**：

[types.go:L45-L75](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L45-L75)：VS 的 spec。下面只摘关键字段并标注它影响 upstream 还是 location：

```go
type VirtualServerSpec struct {
	IngressClass string              `json:"ingressClassName"` // 选哪个控制器实例处理（不直接产生配置）
	Host         string              `json:"host"`             // → server 块的 server_name
	Listener     *VirtualServerListener `json:"listener"`      // → 引用 GlobalConfiguration 的 listener
	TLS          *TLS                `json:"tls"`              // → server 块 ssl 配置
	Policies     []PolicyReference   `json:"policies"`         // → server 级策略指令
	Upstreams    []Upstream          `json:"upstreams"`        // ★→ 生成 upstream 块
	Routes       []Route             `json:"routes"`           // ★→ 生成 location 块
	HTTPSnippets string              `json:"http-snippets"`    // → http 上下文原样片段
	ServerSnippets string            `json:"server-snippets"`  // → server 上下文原样片段
	// ...
}
```

**② `Upstream`——一个 upstream 块的全部参数**：

[types.go:L122-L133](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L122-L133)：`Name` 是被 route 引用的「钥匙」；`Service`+`Port` 决定后端；其余字段（`LBMethod`、`MaxFails`、`HealthCheck` 等）映射成 upstream 块里的 NGINX 指令。

```go
type Upstream struct {
	Name        string `json:"name"`        // upstream 名（route.pass 引用它）
	Service     string `json:"service"`     // 后端 Service 名
	Subselector map[string]string `json:"subselector"`
	Port        uint16 `json:"port"`        // Service 端口
	LBMethod    string `json:"lb-method"`   // → load-balancing 指令
	// ... fail-timeout / max-fails / keepalive / healthCheck / slow-start ...
}
```

**③ `Route`——一条路由规则，对应一个 location**：

[types.go:L274-L298](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L274-L298)：`Path` 决定 location 的匹配方式（前缀/正则/精确）；`Action`/`Splits`/`Matches` 三选一决定 location 内部行为。

```go
type Route struct {
	Path    string   `json:"path"`    // → location 的路径匹配
	Action  *Action  `json:"action"`  // 默认动作（pass/redirect/return/proxy）
	Splits  []Split  `json:"splits"`  // 流量切分（≥2 份，权重和=100）
	Matches []Match  `json:"matches"` // 内容路由（按 header/cookie 匹配）
	Route   string   `json:"route"`   // 引用一个 VSR 来提供本 route
	// ...
}
```

**④ `Action`——四选一的动作**：

[types.go:L301-L310](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L301-L310)：`Pass` 是最常用的——按 upstream 名转发。

```go
type Action struct {
	Pass     string          `json:"pass"`     // 转发到某 upstream（按 name）
	Redirect *ActionRedirect `json:"redirect"` // 重定向
	Return   *ActionReturn   `json:"return"`   // 直接返回固定响应
	Proxy    *ActionProxy    `json:"proxy"`    // 带改写的代理
}
```

**⑤ `Split`——流量切分的最小单元**：

[types.go:L372-L377](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L372-L377)：每个 `Split` 持有一个 `Weight`（0..100）和一个 `Action`。多个 split 的权重之和必须是 100，配置生成时会渲染成 NGINX 的 `split_clients`（u4-l5 详讲）。

```go
type Split struct {
	Weight int     `json:"weight"` // 权重，0..100
	Action *Action `json:"action"` // 该权重对应的动作
}
```

**⑥ `VirtualServerRoute`——被装配的子路由**：

[types.go:L512-L521](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L512-L521)：VSR 的 spec 极简，只有 `IngressClass`、`Host`、`Upstreams`、`Subroutes`。注意它复用的是同一个 `Upstream` 和 `Route` 类型——所以「子路由片段」与「主路由」在数据结构上完全一致，只是入口不同。

```go
type VirtualServerRouteSpec struct {
	IngressClass string     `json:"ingressClassName"`
	Host         string     `json:"host"`      // 必须与引用它的 VS 相同
	Upstreams    []Upstream `json:"upstreams"` // 复用 Upstream 类型
	Subroutes    []Route    `json:"subroutes"` // 复用 Route 类型
}
```

> 对比记忆：VS 自带 `host`+`tls`+`routes`，是「完整的服务器」；VSR 只有 `host`+`upstreams`+`subroutes`，是「待装配的零件」。

#### 4.2.4 代码实践

**实践目标**：定位 `VirtualServerSpec`，亲手把字段归类到 upstream / location。

**操作步骤**：

1. 在 [types.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go) 里跳到 `VirtualServerSpec`（L45）。
2. 针对每个字段问自己一个问题：「这个字段最终是影响 NGINX 的 upstream 块，还是 location 块，还是 server 块，还是别的？」
3. 重点看 `Upstreams`（→ upstream 块）与 `Routes`（→ location 块），并进入 `Route.Action.Pass` 看它如何用 upstream 的 `Name` 把二者连起来。

**需要观察的现象**：你会发现 VS 的字段天然分成三类——server 级（`host`/`tls`/`server-snippets`）、upstream 级（`upstreams`）、location 级（`routes`）。

**预期结果**：你能画出「`spec.upstreams[].name` ← `spec.routes[].action.pass`」这条引用链。

#### 4.2.5 小练习与答案

**练习 1**：一个 route 同时定义了 `action` 和 `splits`，会怎样？
**答案**：`splits` 用于流量切分，`action` 是默认动作。当配置了 `splits` 时，请求按权重分发到各 split 的 action；二者描述的是「同一 location 内的不同分发策略」，实际生成时由配置层决定优先级（详见 u4-l5）。字段语义上 `splits` 要求至少 2 份、权重和为 100。

**练习 2**：VSR 的 `Host` 为什么必须和引用它的 VS 一致？
**答案**：VSR 只是 VS 的子路由片段，它本身不生成独立的 server 块，而是被合并进 VS 的 server 里。host 不一致会导致「这个片段属于哪个虚拟主机」无法确定，因此校验层会拒绝（u2-l3 详讲）。

---

### 4.3 TransportServer 结构

#### 4.3.1 概念说明

`TransportServer`（简称 TS）负责 **L4（四层）负载均衡**，处理 TCP/UDP/TLS Passthrough 流量。它和 VS 的关键差异是：四层没有「HTTP 路径」概念，路由退化为「listener 收到连接 → 转给某个 upstream」。

正因为四层端口是**全局稀缺资源**（一个端口只能被一个 server 监听），TS 不能像 VS 那样随便用默认 80/443，而**必须显式引用一个在 `GlobalConfiguration` 里登记过的 listener**。这是 TS 结构上最显眼的特点。

#### 4.3.2 核心流程

```text
TransportServer
├─ listener: { name, protocol }     ──► 引用 GlobalConfiguration 的某个 listener（决定在哪监听）
├─ host                              ──► SNI / 主机标识
├─ upstreams: [ {name, service, port, ...} ]  ──► 生成 stream upstream 块
└─ action: { pass: "name" }         ──► stream server 把连接转发给该 upstream
```

注意 TS 的 `Action` 只有一种动作 `Pass`——四层没有 redirect/return。

#### 4.3.3 源码精读

**① TS 的 marker——比 VS 多了 `storageversion`**：

[types.go:L601-L609](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L601-L609)：结构与 VS 的 marker 块几乎一样（`shortName=ts`、`subresource:status`），额外有 `+kubebuilder:storageversion`，表示这是该资源当前的存储版本。

**② `TransportServerSpec`——四层声明**：

[types.go:L621-L642](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L621-L642)：注意 `Listener` 是**非指针的值类型**（必填），`Action` 只有一个。

```go
type TransportServerSpec struct {
	IngressClass string                 `json:"ingressClassName"`
	TLS          *TransportServerTLS    `json:"tls"`
	Listener     TransportServerListener `json:"listener"` // ★必填：引用 GC 的 listener
	Host         string                 `json:"host"`
	Upstreams    []TransportServerUpstream `json:"upstreams"` // ★→ stream upstream
	Action       *TransportServerAction `json:"action"`       // ★只有 pass
	// serverSnippets / streamSnippets / upstreamParameters / sessionParameters ...
}
```

**③ `TransportServerListener`——引用 listener 的方式**：

[types.go:L650-L655](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L650-L655)：`Name` 必须能在某个 `GlobalConfiguration` 的 listeners 里找到，`Protocol` 描述协议（TCP/UDP/TLS_PASSTHROUGH 等）。

```go
type TransportServerListener struct {
	Name     string `json:"name"`     // 指向 GlobalConfiguration 的 listener 名
	Protocol string `json:"protocol"` // 协议
}
```

**④ `TransportServerUpstream`——四层 upstream**：

[types.go:L658-L679](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L658-L679)：和 VS 的 `Upstream` 相比，字段更少（四层没有 proxy 缓冲、proxy_next_upstream 等 HTTP 概念），核心是 `Name`/`Service`/`Port`/`LoadBalancingMethod`/`HealthCheck`/`Backup`。

```go
type TransportServerUpstream struct {
	Name        string `json:"name"`
	Service     string `json:"service"`
	Port        int    `json:"port"`
	FailTimeout string `json:"failTimeout"`
	MaxFails    *int   `json:"maxFails"`
	MaxConns    *int   `json:"maxConns"`
	HealthCheck *TransportServerHealthCheck `json:"healthCheck"`
	LoadBalancingMethod string `json:"loadBalancingMethod"`
	Backup      string `json:"backup"`
	// ...
}
```

**⑤ `TransportServerAction`——唯一动作 Pass**：

[types.go:L732-L735](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L732-L735)：四层只能「把连接/datagram 转给某 upstream」，没有 redirect/return。

```go
type TransportServerAction struct {
	Pass string `json:"pass"` // 转给某个 upstream（按 name）
}
```

#### 4.3.4 代码实践

**实践目标**：对比 TS 与 VS，理解四层为何更「瘦」。

**操作步骤**：

1. 并排打开 [VirtualServerSpec](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L45-L75) 与 [TransportServerSpec](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L621-L642)。
2. 列出 VS 有而 TS 没有的字段（如 `routes`、`gunzip`、`http-snippets`），并思考为什么。
3. 确认 TS 的 `Action` 只有 `Pass`，而 VS 的 `Action` 有四种。

**需要观察的现象**：TS 没有 `Routes`——四层没有「路径」可路由。

**预期结果**：你得出结论「TS = listener + upstreams + 单一 pass 动作」，比 VS 简单一截。

> 待本地验证：若你手头有集群，可 `kubectl explain virtualserver.spec` 与 `kubectl explain transportserver.spec` 对比字段，效果更直观。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TS 必须引用 GlobalConfiguration 的 listener，而 VS 可以不填 listener？
**答案**：VS 默认用全局的 80/443 HTTP/HTTPS listener，多个 VS 可以共享（按 host 区分）。但四层端口是全局稀缺资源，一个端口不能被两个 TS 同时监听，因此必须先在 GlobalConfiguration 登记 listener，再由 TS 显式引用，控制器据此做端口冲突检测。

**练习 2**：TS 的 `Action` 为什么没有 `redirect` / `return`？
**答案**：四层工作在 TCP/UDP 流级别，没有 HTTP 状态码和 Location 头这些概念，无法构造重定向或固定响应，只能把连接转给后端。

---

### 4.4 Policy 结构

#### 4.4.1 概念说明

`Policy` 是 NIC 把「施加在路由上的策略」做成强类型 CRD 的产物（u2-l1 已讲其定位）。它的设计有一个非常巧妙的约束：**一个 Policy 资源只表达一种策略**。

这个约束体现在 `PolicySpec` 的结构上：它有十几个策略字段，但它们是**互斥的指针**——你应该只填其中一个。靠什么来强制？靠注释约定 + 校验层（u2-l3），类型本身用「一排可选指针」来表达「多选一」。

Policy 只服务于 VS/VSR（不服务于 Ingress），但每种策略类型在 Ingress 上是否可用，由配置层的 `IsPolicySupportedOnIngress` 判定（u4-l7）。

#### 4.4.2 核心流程

```text
Policy
└─ spec:
   ├─ ingressClassName            （选哪个控制器实例）
   ├─ accessControl:  *AccessControl   ┐
   ├─ rateLimit:      *RateLimit       │
   ├─ jwt:            *JWTAuth         │  ← 这十几个字段互斥，
   ├─ basicAuth:      *BasicAuth       │     只应填一个
   ├─ oidc:           *OIDC            │
   ├─ ...                              ┘
```

被引用方式：VS/VSR 在 `policies` 字段里用 `PolicyReference{name, namespace}` 按名引用（见 [types.go:L114-L119](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L114-L119)）。

#### 4.4.3 源码精读

**① `PolicySpec`——「多选一」的核心结构**：

[types.go:L787-L815](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L787-L815)：注释明说「Only one policy (field) is allowed」。下面是全部策略类型字段（每一个都是一个独立的强类型结构体指针）：

```go
type PolicySpec struct {
	IngressClass string `json:"ingressClassName"`
	AccessControl *AccessControl `json:"accessControl"` // IP 白/黑名单
	RateLimit     *RateLimit     `json:"rateLimit"`     // 限流
	JWTAuth       *JWTAuth       `json:"jwt"`           // JWT 认证
	BasicAuth     *BasicAuth     `json:"basicAuth"`     // HTTP Basic 认证
	IngressMTLS   *IngressMTLS   `json:"ingressMTLS"`   // 客户端证书校验
	EgressMTLS    *EgressMTLS    `json:"egressMTLS"`    // 上游 mTLS
	OIDC          *OIDC          `json:"oidc"`          // OpenID Connect
	WAF           *WAF           `json:"waf"`           // WAF（App Protect）
	APIKey        *APIKey        `json:"apiKey"`        // API Key 鉴权
	Cache         *Cache         `json:"cache"`         // 代理缓存
	CORS          *CORS          `json:"cors"`          // 跨域
	ExternalAuth  *ExternalAuth  `json:"externalAuth"`  // 外部认证（如 oauth2-proxy）
}
```

**② 最简单的策略——`AccessControl`**：

[types.go:L829-L832](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L829-L832)：只有 `Allow`/`Deny` 两个 IP 列表，会翻译成 NGINX 的 `allow`/`deny` 指令。

```go
type AccessControl struct {
	Allow []string `json:"allow"`
	Deny  []string `json:"deny"`
}
```

**③ 较复杂的策略——`RateLimit`**：

[types.go:L835-L861](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L835-L861)：`Rate`、`Key`、`Burst`、`ZoneSize` 等字段会翻译成 `limit_req_zone` 与 `limit_req` 指令（u4-l7 详讲）。

**④ 跨字段 CEL 校验的实例——OIDC**：

[types.go:L803-L804](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L803-L804)：OIDC 字段上有一条 `XValidation`，用 CEL 表达式约束「只有 `sslVerify=true` 时才允许设 `trustedCertSecret`」。这种跨字段约束单靠字段级 marker 表达不了，必须用 CEL。

```go
// +kubebuilder:validation:XValidation:rule="(self.sslVerify == true) || (self.sslVerify == false && !has(self.trustedCertSecret))",message="trustedCertSecret can be set only if sslVerify is true"
OIDC *OIDC `json:"oidc"`
```

类似的还有 CORS 的 [types.go:L1224](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L1224)（`allowOrigin` 为 `*` 时禁止 `allowCredentials=true`）和 Cache 的 [types.go:L1106](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L1106)。这些 CEL 规则会被 controller-gen 翻译成 CRD schema 的 `x-kubernetes-validations`，由 API Server 在准入时强制执行。

#### 4.4.4 代码实践

**实践目标**：体会「一个 Policy 只表达一种策略」的结构设计。

**操作步骤**：

1. 打开 [PolicySpec](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L787-L815)。
2. 数一数有多少个策略字段（答案：12 个，加上 `ingressClassName`）。
3. 想象：若用户在一个 Policy 里同时填了 `rateLimit` 和 `jwt`，会发生什么？（提示：这不是 schema 能拦的，要靠校验层。）

**需要观察的现象**：所有策略字段都是**指针**（`*RateLimit`），意味着「不填就是 nil」。

**预期结果**：你理解了「指针 + 互斥约定」是 Go 表达「多选一」的惯用法。

#### 4.4.5 小练习与答案

**练习 1**：为什么策略字段用指针 `*RateLimit` 而不是值 `RateLimit`？
**答案**：指针能区分「用户没填（nil）」和「用户填了零值」。对于「多选一」语义，必须能判断某个策略到底有没有被启用，所以必须用指针。

**练习 2**：OIDC 的「sslVerify 为 false 时不能设 trustedCertSecret」这条约束，为什么用 CEL 而不是普通 `Pattern`？
**答案**：`Pattern` 只能校验单个字符串字段的格式；而这条规则涉及 `sslVerify` 和 `trustedCertSecret` **两个**字段的联动关系，属于跨字段约束，必须用能引用 `self.xxx` 的 CEL 表达式（`XValidation`）。

---

### 4.5 GlobalConfiguration 与 Listener

#### 4.5.1 概念说明

`GlobalConfiguration`（简称 GC）的角色很纯粹：它是一张 **listener（监听器）注册表**，登记「NIC 在哪些端口、用什么协议监听」。它本身不路由任何流量，只提供「端口资源池」，供 VS（自定义 HTTP/HTTPS listener）和 TS（四层 listener）按名字引用。

这是 u2-l1 讲过的解耦：**GC 负责「在哪监听」，VS/TS 负责「监听到之后怎么处理」**。

#### 4.5.2 核心流程

```text
GlobalConfiguration
└─ spec:
   └─ listeners: [ {name, protocol, port, ipv4, ipv6, ssl} ]
                          │
                          ▼ 被引用
   TransportServer.spec.listener.name  ──► 必须在此注册表中
   VirtualServer.spec.listener.http    ──► 必须在此注册表中
```

此外，代码里还内置了一个特殊的 listener 名，用于 TLS Passthrough：

[types.go:L17-L20](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L17-L20)：`TLSPassthroughListenerName = "tls-passthrough"` 与对应协议常量，省去用户为 TLS passthrough 手动登记 listener。

#### 4.5.3 源码精读

**① GC 的 marker——带 `storageversion` 但无 status 子资源**：

[types.go:L555-L559](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L555-L559)：注意 GC **没有** `+kubebuilder:subresource:status`——它不需要控制器回写状态，因为它只是一份静态注册表。

**② `GlobalConfigurationSpec`——极简**：

[types.go:L569-L572](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L569-L572)：只有一个 `Listeners` 列表。

```go
type GlobalConfigurationSpec struct {
	Listeners []Listener `json:"listeners"`
}
```

**③ `Listener`——一个监听端口的定义**：

[types.go:L575-L588](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L575-L588)：`Name` 唯一、`Protocol`（HTTP/TCP/UDP/TLS_PASSTHROUGH 等）、`Port`、可选的 `IPv4`/`IPv6` 绑定地址、`Ssl` 开关。

```go
type Listener struct {
	Name     string `json:"name"`     // 唯一名，被 VS/TS 引用
	Protocol string `json:"protocol"` // 协议
	Port     int    `json:"port"`     // 监听端口
	IPv4     string `json:"ipv4"`
	IPv6     string `json:"ipv6"`
	Ssl      bool   `json:"ssl"`
}
```

**④ 引用闭环**：TS 的 [TransportServerListener.Name](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L650-L655) 与 VS 的 [VirtualServerListener](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L77-L83)（`http`/`https` 两个字段），都必须指向某个 GC 里登记过的 listener 名。引用校验在控制器层完成（u3-l6 的 reference_checkers）。

#### 4.5.4 代码实践

**实践目标**：把 GC 与 TS 的引用关系串起来。

**操作步骤**：

1. 读 [Listener](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L575-L588)，理解一个 listener 的完整字段。
2. 跳到 [TransportServerListener](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L650-L655)，确认它的 `Name` 就是引用 GC listener 的钥匙。
3. 在示例目录找一份真实用法对照（如 `examples/custom-resources/transport-server-tls-passthrough/`），看 GC 与 TS 如何成对出现。

**需要观察的现象**：GC 是「登记」，TS 是「引用」，二者通过 `name` 字符串耦合。

**预期结果**：你能说清「为什么改了 GC 的 listener 名，所有引用它的 TS 都会失效」。

> 待本地验证：若要确认示例路径是否存在，可在仓库内 `ls examples/custom-resources/` 查看。

#### 4.5.5 小练习与答案

**练习 1**：GC 为什么不需要 status 子资源？
**答案**：GC 是静态注册表，控制器不对它做「调谐后回写状态」的动作（VS/VSR/TS/Policy 才有 Valid/Invalid 状态）。所以它的 marker 块里没有 `+kubebuilder:subresource:status`。

**练习 2**：如果把两个 Listener 的 `Port` 设成一样会怎样？
**答案**：同一端口被两个 listener 占用会导致 NGINX 启动失败。这类冲突在控制器层（端口冲突检测）或 NGINX 配置校验阶段被发现并阻止，资源会被标记为 Invalid。

---

## 5. 综合实践

本讲的核心任务是：**在 `types.go` 中定位 `VirtualServerSpec`，列出它的主要字段，并标注每个字段会影响生成的 NGINX upstream 还是 location**。这个任务把本讲所有模块串起来——它要求你同时理解「字段定义（4.2）」、「marker 如何驱动 schema（4.1）」，并预告「配置生成（u4）」。

### 实践目标

亲手建立「YAML 字段 → Go 类型 → NGINX 配置单元」的映射表。

### 操作步骤

1. **打开真相源**：跳到 [VirtualServerSpec (types.go:L45-L75)](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L45-L75)。
2. **逐字段归类**：把 `VirtualServerSpec` 的字段填进下表（已给出示例行，请补全）：

   | 字段 | json tag | 影响的 NGINX 配置单元 | 说明 |
   | --- | --- | --- | --- |
   | `Host` | `host` | server 块（server_name） | 决定虚拟主机域名 |
   | `TLS` | `tls` | server 块（ssl_certificate 等） | TLS 终止配置 |
   | `Upstreams` | `upstreams` | **upstream 块** | 每个 Upstream 生成一个 upstream 块 |
   | `Routes` | `routes` | **location 块** | 每个 Route 生成一个 location |
   | `Policies` | `policies` | server/location 级指令 | 引用 Policy CRD |
   | `Listener` | `listener` | server 块的 listen 指令 | 引用 GlobalConfiguration |
   | ... | ... | ... | 请补全 `HTTPSnippets`、`ServerSnippets`、`Gunzip` 等 |

3. **追踪引用链**：进入 `Route`（[L274](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L274-L298)）→ `Action.Pass`（[L303](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L301-L310)），确认它如何用 upstream 的 `Name`（[L124](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L122-L133)）把 location 与 upstream 连起来。
4. **用 marker 自检**：挑一个字段（如 `AddHeaderInherit` 的 `+kubebuilder:validation:Enum`，[L67-L68](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/pkg/apis/configuration/v1/types.go#L67-L68)），在生成的 `config/crd/bases/k8s.nginx.org_virtualservers.yaml` 里找到对应的 `enum`，确认 marker→schema 的映射。

### 需要观察的现象

- `Upstreams` 决定「有哪些后端池」（upstream 块），`Routes` 决定「URL 怎么分发到这些池」（location 块）。
- 二者靠 **name 字符串** 解耦：route 不内联 service，而是用 `action.pass: <upstream-name>` 引用。

### 预期结果

产出一张完整的「VirtualServerSpec 字段 → 配置单元」映射表，并能画出下面这条链路：

```text
spec.upstreams[].name  ◄──引用──  spec.routes[].action.pass
     │                                   │
     ▼                                   ▼
  upstream 块                        location 块
```

> 进阶（可选）：对比 `TransportServerSpec`，体会「四层没有 routes、只有 listener+upstream+pass」的结构差异——这是本讲 4.2 与 4.3 的对照收获。

---

## 6. 本讲小结

- `pkg/apis/configuration/v1/types.go` 是整个项目的**数据真相源**：5 个 CRD（VS / VSR / TS / GC / Policy）的 Go 结构体全在这里，且文件禁止 import 业务层，保持纯净。
- 所有 CRD 共享 `TypeMeta + ObjectMeta + Spec + Status` 骨架；API group 为 `k8s.nginx.org`、version 为 `v1`，由 `doc.go`、`group_info.go`、`register.go` 三件套固定。
- **marker 是写给代码生成器的指令**：`+kubebuilder:...` 决定 CRD YAML 的 schema、短名、status 子资源、`kubectl get` 列；`+k8s:...` 决定 deepcopy 与 client 生成。改 `types.go` 后必须跑 `make update-codegen` 与 `make update-crds`。
- **VirtualServer**：`upstreams` → upstream 块，`routes` → location 块，二者靠 `action.pass` 用 upstream 的 `name` 连接；`splits`/`matches` 提供流量切分与内容路由。
- **TransportServer**：四层，结构更瘦——必须引用 GlobalConfiguration 的 listener，action 只有 `pass`。
- **Policy**：用「一排互斥指针」表达「一个 Policy 只表达一种策略」；复杂跨字段约束用 CEL `XValidation` 表达。

---

## 7. 下一步学习建议

本讲只定义了数据「长什么样」，还没讲「怎么校验」和「怎么用」。建议按顺序继续：

1. **u2-l3（CRD 校验与代码生成链路）**：看 `pkg/apis/configuration/validation/` 如何在控制器层做语义校验（如 VSR 的 host 必须与 VS 一致、Policy 只能填一个字段），以及 `make update-codegen`/`update-crds` 如何把 `types.go` 变成下游产物。
2. **u4-1 / u4-5（配置生成）**：看 `internal/configs/virtualserver.go` 如何把本讲的 `VirtualServerSpec` 翻译成真实的 NGINX upstream/location——本讲的映射表会在那里得到验证。
3. **u8-l1（扩展实践：新增一个 CRD 字段）**：当你想亲手加一个字段时，本讲的 marker 字典就是你的检查清单。

> 一个好的自测：合上本讲，能不能闭眼说出「VS 的哪个字段生成 upstream、哪个生成 location」？如果能，说明你已经建立了从用户 YAML 到 NGINX 配置的心智模型。
