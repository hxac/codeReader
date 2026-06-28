# 二次开发：新增功能与扩展点

## 1. 本讲目标

本讲是整本学习手册的收官篇，面向想要**给 NGF 贡献代码或做二次开发**的读者。前面十二个单元我们已经把「NGF 是什么、怎么跑、源码怎么读」讲透了；本讲要回答的是一个更高阶的问题：

> 如果我想给 NGF 新增一个能力——例如一个新的策略 CRD——从写第一行 API 类型，到最终让数据面 NGINX 多出一条指令，到底要改哪些地方？按什么顺序改？改完怎么走完一个能被合并的 PR？

学完本讲，你应当能够：

- 画出 NGF「新增一个策略」的**完整改动链路图**，并说出每一层各自负责什么。
- 知道 NGF 用了哪些**代码生成工具**（controller-gen / counterfeiter / helm-schema 等），以及 `make generate-all` 这把「总钥匙」背后跑了什么。
- 说清楚 NGF 的 **PR 流程**：从认领 issue、fork、开分支、写测试，到 squash 合并的 16 步。
- 拿到一个新需求时，能独立列出**需要改动的文件清单**。

本讲是最「动手」的一讲，但它**不手把手教你写一个完整 CRD**——那需要几千行代码。本讲做的是「画地图 + 标路标」：把已有策略（以 `ClientSettingsPolicy` 为范本）的每一处接线点指给你看，让你知道每一颗螺丝拧在哪里。

## 2. 前置知识

本讲默认你已经读过的前置讲义：

- **u8-l2（自定义 CRD 与策略附着）**：知道配置类资源与策略类资源的区别、Direct/Inherited 附着模型、`policies.Policy` 接口。
- **u8-l3（策略到 NGINX 指令的生成）**：知道组合模式三件套（`Generator` 接口 / `CompositeGenerator` / `UnimplementedGenerator`）、`Validator`/`CompositeValidator`、字段→指令的三种范式。
- **u6-l3（Servers、Upstreams 与 Locations 生成）**：知道 `dataplane.Configuration` 如何被渲染成 `server`/`location` 块。

此外你需要一点背景知识：

- **CRD（CustomResourceDefinition）**：Kubernetes 让你自定义资源类型的方式。你写一个 Go struct，用 kubebuilder 注解标注，再用 `controller-gen` 工具生成对应的 CRD YAML 和 deepcopy 代码。
- **GVK（GroupVersionKind）**：Kubernetes 里每个资源类型的唯一身份证，例如 `gateway.nginx.org/v1alpha1, Kind=ClientSettingsPolicy`。NGF 的很多分发逻辑都靠 GVK 做路由。
- **scheme**：controller-runtime 里的一张「类型注册表」，告诉客户端「这个 GVK 对应哪个 Go struct」。没注册进 scheme 的类型无法被 watch 和缓存（见 u3-l2）。
- **kubebuilder 注解**：以 `// +kubebuilder:` 开头的注释，写在 Go 类型上方，`controller-gen` 会读取它们来生成 CRD 的校验规则（`validation`）、打印列（`printcolumn`）、策略标签（`policy=inherited`）等。

如果你对「控制器如何把资源变更变成事件」「事件如何被批处理」还不熟，建议先回头读 u4 系列。

## 3. 本讲源码地图

本讲横跨多个目录，因为「新增一个功能」本身就横跨多层。下表是本讲引用的关键文件及其在本讲中的角色：

| 文件 | 作用 |
| --- | --- |
| [docs/developer/implementing-a-feature.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md) | 官方「功能开发流程」文档，本讲 PR 流程模块的权威依据 |
| [Makefile](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile) | 代码生成入口，`generate` / `generate-crds` / `generate-all` 三个目标 |
| [apis/v1alpha1/clientsettingspolicy_types.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go) | 范本 CRD 的 Go 类型定义（含 kubebuilder 注解） |
| [apis/v1alpha1/register.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go) | 把类型注册进 scheme 的 `addKnownTypes` |
| [apis/v1alpha1/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/doc.go) | 包级 kubebuilder 注解，触发 deepcopy 生成 |
| [internal/framework/kinds/kinds.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/kinds/kinds.go) | 所有 Kind 的字符串常量集中地 |
| [internal/controller/manager.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go) | 控制面总装：scheme 注册、控制器注册、`createPolicyManager` |
| [internal/controller/state/change_processor.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go) | 变更处理器里把策略注册进对象存储 |
| [internal/controller/state/graph/policies.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go) | 图层策略处理总入口（解析/冲突/附着） |
| [internal/controller/nginx/config/policies/policy.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go) | `Policy` 接口定义（所有策略的抽象基类） |
| [internal/controller/nginx/config/policies/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go) | `Generator` 接口、`CompositeGenerator`、`UnimplementedGenerator` |
| [internal/controller/nginx/config/policies/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go) | `Validator` 接口、`CompositeValidator`、`NewManager` 注册器 |
| [internal/controller/nginx/config/policies/clientsettings/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go) | 范本策略的生成器（字段→模板→指令） |
| [internal/controller/nginx/config/policies/clientsettings/validator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go) | 范本策略的校验器 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | `Generate` 里把各策略生成器塞进 `CompositeGenerator` 的接线点 |

> 阅读建议：本讲的三个最小模块（功能开发链路 / 代码生成 / PR 流程）是**一条主干**的三个视角。建议先通读 4.1 建立全局链路图，再用 4.2 理解「为什么改完代码还要跑生成」，最后用 4.3 把改动交付出去。

## 4. 核心概念与源码讲解

### 4.1 功能开发链路

#### 4.1.1 概念说明

NGF 是一个**严格分层**的项目。回顾 u1-l2 建立的地图：`apis/` 是对外可见的 CRD 类型，`internal/controller/` 是产品逻辑，`internal/framework/` 是可复用框架。一条用户写的策略 YAML 要变成数据面 NGINX 的一条指令，必须**依次穿过每一层**，没有任何一层能「跳过」。

这意味着「新增一个策略」不是改一个文件，而是改一条**链**上的每一环。这条链就是本讲所说的「功能开发链路」：

```
API 类型定义 ──► scheme 注册 ──► 控制器 watch ──► 变更存储
                                                      │
                                                      ▼
              NGINX 指令 ◄── 配置生成器 ◄── 图处理/附着/校验
                                                      ▲
                                                      │
                                              生成器/校验器接线
```

理解这条链的关键，是抓住一个反复出现的**设计范式**：NGF 几乎每一层都用「**接口 + 组合器 + 注册表**」三件套来容纳多种策略类型，而不是用 `switch case`。这样新增一种策略时，你只需要「**写一个新实现 + 在注册表里登记一次**」，而不需要改动任何已有策略的代码。下面我们会逐层看到这个范式：

| 层 | 接口 | 组合器/注册器 | 登记方式 |
| --- | --- | --- | --- |
| API 类型 | `client.Object` + `policies.Policy` | scheme `addKnownTypes` | 在 `register.go` 加一行 |
| 变更存储 | 通用 `objectStore` | `change_processor.go` 的 store 列表 | 在 store 列表加一段 |
| 图校验 | `policies.Validator` | `CompositeValidator`（`NewManager`） | 在 `createPolicyManager` 加一个 `ManagerConfig` |
| 图附着 | （内置在 `attachPolicies`） | `processPolicies` | 自动按 GVK 分发 |
| 配置生成 | `policies.Generator` | `CompositeGenerator` | 在 `Generate` 加一个生成器 |

> 这个「接口 + 组合器」范式是 u8-l3 已经讲透的内容，本讲只强调它在「新增功能」语境下的含义：**每一层都是开闭原则（对扩展开放，对修改封闭）的落地**。

#### 4.1.2 核心流程

我们以「新增一个名叫 `FooPolicy` 的简单策略」为例，把链路走一遍。每一站对应一个必须改动的位置：

1. **定义 API 类型**（`apis/v1alpha1/foopolicy_types.go`）：写 `FooPolicy` struct，带 `Spec`/`Status`/`TargetRef`，并在类型上方加 kubebuilder 注解（策略标签、校验规则）。范本是 `ClientSettingsPolicy`。
2. **注册进 scheme**（`apis/v1alpha1/register.go`）：在 `addKnownTypes` 里加 `&FooPolicy{}`、`&FooPolicyList{}`。不注册则无法被控制器 watch（呼应 u3-l2 的 scheme 前提）。
3. **登记 Kind 常量**（`internal/framework/kinds/kinds.go`）：加一个 `FooPolicy = "FooPolicy"` 常量。全仓的 Kind 字符串都集中在这里，避免散落的魔法字符串。
4. **让控制器 watch 它**（`internal/controller/manager.go` 的 `ctlrCfg`）：加一个注册表单元，指定 `objectType` 与 predicate（通常用 `GenerationChangedPredicate`）。
5. **登记变更存储**（`internal/controller/state/change_processor.go`）：加一段把该 GVK 映射到 `commonPolicyObjectStore`、配上 `isNGFPolicyRelevant` 谓词。否则该资源的变更不会被处理器捕获（呼应 u4-l4）。
6. **写校验器**（`internal/controller/nginx/config/policies/foo/validator.go`）：实现 `policies.Validator` 三个方法（`Validate`/`ValidateGlobalSettings`/`Conflicts`），并在 `createPolicyManager` 里用 `ManagerConfig` 注册进 `CompositeValidator`。
7. **写生成器**（`internal/controller/nginx/config/policies/foo/generator.go`）：嵌入 `UnimplementedGenerator`，只重写你需要的上下文方法，把 Spec 渲染成 NGINX 指令文本；在 `config/generator.go` 的 `Generate` 里塞进 `NewCompositeGenerator`。
8. **图附着**：通常**不需要改**——`processPolicies`/`attachPolicies` 按 GVK 与 targetRef 的 Kind 自动分发，只要你的策略实现了 `policies.Policy` 接口、targetRef 指向 Gateway/HTTPRoute/GRPCRoute/Service 之一，就会被自动附着。
9. **状态回写条件**（`internal/controller/state/graph/policies.go` 的 `addStatusToTargetRefs`）：如果你的策略要在目标资源上打「受某策略影响」的条件，加一个 `case kinds.FooPolicy` 分支，并在 `conditions` 包加对应工厂。
10. **写单元测试**：每一步都要配测试（详见 4.3）。

注意第 8 步——这是整个设计最优雅的地方：**图层的附着逻辑是策略无关的**。你不需要为每种新策略写一套「怎么挂到 Gateway 上」的代码，只要遵守 `policies.Policy` 契约，就免费获得了附着、继承、冲突裁决（u8-l2/u8-l3 讲过的那些能力）。

#### 4.1.3 源码精读

下面沿链路逐站给出真实源码。我们用 `ClientSettingsPolicy` 作为「已经走完整条链」的范本，反向追踪它的每一处接线。

**第 1 站：API 类型定义与 kubebuilder 注解。**

[apis/v1alpha1/clientsettingspolicy_types.go:8-27](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L8-L27) 定义了范本类型。注意类型上方那一串 `// +kubebuilder:` 注解——它们是给 `controller-gen` 看的指令：

- `+kubebuilder:object:root=true` + `+genclient`：表示这是个顶层 CRD，要生成客户端。
- `+kubebuilder:storageversion`：标记为存储版本（CRD 版本迁移用，见 `docs/developer/crd-versioning.md`）。
- `+kubebuilder:resource:categories=...,shortName=cspolicy`：kubectl 里的分类与短名。
- `+kubebuilder:metadata:labels="gateway.networking.g8s.io/policy=inherited"`：**最关键的一条**——它声明这是个 Inherited 附着策略（u8-l2 讲过的 Direct/Inherited 二分），上游 Gateway API 工具链和 NGF 自己都靠它识别语义。

[apis/v1alpha1/clientsettingspolicy_types.go:54-57](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/clientsettingspolicy_types.go#L54-L57) 还示范了用 CEL `XValidation` 注解在 API server 准入层做字段级互斥校验（限制 targetRef 的 group/kind）。这些校验在资源进集群前就生效，比控制面里的 Go 校验更早。

**第 2 站：注册进 scheme。**

[apis/v1alpha1/register.go:33-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L33-L56) 的 `addKnownTypes` 把本组所有类型注册进 scheme。[第 39-40 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L39-L40) 就是 `ClientSettingsPolicy` 的登记处。新增策略时，你要在这里加 `&FooPolicy{}` 和 `&FooPolicyList{}` 两行。`AddToScheme`（[第 29 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L29)）随后被控制面 `init()` 调用，见下。

[internal/controller/manager.go:97-110](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L97-L110) 是控制面的 scheme 装配：`utilruntime.Must(ngfAPIv1alpha1.AddToScheme(scheme))` 把整组类型一次性挂上。只要你完成了第 2 站的注册，控制面就自动能认得你的新类型——**这一行通常不用改**。

**第 3 站：Kind 常量。**

[internal/framework/kinds/kinds.go:104-126](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/kinds/kinds.go#L104-L126) 把所有 NGF 自有 Kind 集中成常量。[第 107 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/kinds/kinds.go#L107) 的 `ClientSettingsPolicy = "ClientSettingsPolicy"` 是范本。新增策略在这里加一行常量，然后全仓用 `kinds.FooPolicy` 引用，杜绝拼写错误。

同文件 [第 128-142 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/kinds/kinds.go#L128-L142) 的 `MustExtractGVK` 是「从对象反查 GVK」的工具函数，校验器组合器就是靠它做路由的（见第 6 站）。

**第 4 站：控制器 watch。**

[internal/controller/manager.go:894-899](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L894-L899) 是 `ClientSettingsPolicy` 的控制器注册单元：`objectType` 指定 watch 谁，`WithK8sPredicate(GenerationChangedPredicate{})` 表示只在 `metadata.generation` 变化时入队（避免 status 写回触发无意义的事件，呼应 u4-l1）。新增策略在这里照抄一段即可。

**第 5 站：变更存储登记。**

[internal/controller/state/change_processor.go:232-235](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/change_processor.go#L232-L235) 把 `ClientSettingsPolicy` 的 GVK 映射到 `commonPolicyObjectStore`，并配上 `isNGFPolicyRelevant` 谓词（u4-l4 讲过：只有被引用的策略才算「相关」、才触发图重建）。新增策略照抄这段，否则它的变更会被处理器丢弃。

**第 6 站：校验器与组合校验器接线。**

[internal/controller/nginx/config/policies/validator.go:17-24](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L17-L24) 定义 `Validator` 接口——每个策略要实现三个方法：`Validate`（校验自身 spec）、`ValidateGlobalSettings`（校验对 NginxProxy 全局配置的依赖，如「必须开启 telemetry」）、`Conflicts`（判断两个同类策略是否冲突）。

[internal/controller/nginx/config/policies/validator.go:59-68](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L59-L68) 是 `CompositeValidator.Validate`：先用 `mustExtractGVK` 查出策略的 GVK，再到 map 里找对应校验器；**找不到就 panic**（`no validator registered for policy`）。这是 u8-l3 讲过的 fail-fast 守门——它逼着你在新增策略时**必须**登记校验器，否则进程崩溃。

登记发生在 [internal/controller/manager.go:467-507](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L467-L507) 的 `createPolicyManager`：它构造一个 `[]ManagerConfig`，每项是 `{GVK, Validator}`，最后交给 `policies.NewManager`（[第 506 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L506)）。[第 472-476 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L472-L476) 是 `ClientSettingsPolicy` 的登记——范本。注意 [第 499-504 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L499-L504) 的 `SnippetsPolicy` 用了 `if cfg.Snippets` 条件登记：**特性开关（feature flag）可以按需挂载策略校验器**，新增带开关的策略时可照此办理。

范本校验器实现见 [internal/controller/nginx/config/policies/clientsettings/validator.go:27-43](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L27-L43)：先校验 targetRef 的 group/kind 是否合法，再校验易注入字段（duration/size）。[第 54-59 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L54-L59) 的 `Conflicts` 是字段级冲突判定，服务 u8-l3 讲过的继承裁决。

**第 7 站：生成器与组合生成器接线。**

[internal/controller/nginx/config/policies/generator.go:10-21](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L10-L21) 定义 `Generator` 接口——五个方法对应五个 NGINX 上下文（main/http/server/location/internal-location）。新策略通常只需要其中一两个，其余靠 [第 97-119 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L97-L119) 的 `UnimplementedGenerator` 嵌入补齐（返回 `nil`），这样你**只写关心的上下文**就能满足接口。

[internal/controller/nginx/config/policies/generator.go:32-40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L32-L40) 定义 `CompositeGenerator`，`NewCompositeGenerator(generators ...Generator)`（[第 38 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L38)）用可变参数收下一组生成器。[第 65-73 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L65-L73) 的 `GenerateForServer` 示范了它如何遍历所有子生成器、拼接结果——这就是 u8-l3 讲过的「复合生成器只做转发与拼接，自身不生成指令」。

接线点在 [internal/controller/nginx/config/generator.go:132-139](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L132-L139)：`Generate` 方法在每次生成配置时构造 `policies.NewCompositeGenerator(clientsettings.NewGenerator(), observability.NewGenerator(), ...)`，把所有策略生成器塞进去。**新增策略时，你要在这一行加一个 `foo.NewGenerator()`**。注意它每次 `Generate` 都新建——生成器是无状态的（u6-l1 讲过 `GeneratorImpl` 持三字段，但策略生成器本身无状态）。

范本生成器见 [internal/controller/nginx/config/policies/clientsettings/generator.go:13-42](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L13-L42)：用 Go `text/template` 在包加载时 `Parse` 一个模板常量，[第 45-47 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L45-L47) 嵌入 `UnimplementedGenerator` 只重写 server/location 三个方法，[第 69-85 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/generator.go#L69-L85) 的 `generate` 把每个策略的类型断言成 `*ClientSettingsPolicy`、用 `helpers.MustExecuteTemplate` 渲染出指令文本。**这就是「字段→指令」的最简范式**（u8-l3 归类的第一种范式：直接用 Spec）。

**第 8 站：图附着（策略无关，通常不用改）。**

[internal/controller/state/graph/policies.go:582-658](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L582-L658) 的 `processPolicies` 是图层策略处理总入口。注意 [第 604-632 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L604-L632)：它按 targetRef 的 group/kind 分发（Gateway/HTTPRoute/GRPCRoute/Service），**完全不看策略本身是哪种 CRD**——只要实现了 `policies.Policy` 接口、targetRef 合法，就会被收进 `processedPolicies`。[第 641 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L641) 调用 `validator.Validate(policy)` 统一校验，[第 653 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L653) 的 `markConflictedPolicies` 做冲突裁决。

[internal/controller/state/graph/policies.go:254-281](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L254-L281) 的 `attachPolicies` 把处理好的策略按 targetRef.Kind 分发到 Gateway/Route/Service 节点——同样是策略无关的。**这意味着新策略免费继承了整套附着、祖先计数、`InvalidForGateways` 细粒度有效性机制**（u8-l2 讲过）。

`policies.Policy` 接口本身见 [internal/controller/nginx/config/policies/policy.go:16-21](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L16-L21)：它在 `client.Object` 基础上加了 `GetTargetRefs`/`GetPolicyStatus`/`SetPolicyStatus` 三个方法。你的 CRD 只要嵌入 `gatewayv1.PolicyStatus` 字段并实现这三个方法（通常由代码生成或手写 receiver 补齐），就自动满足接口。[第 33-60 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L33-L60) 还提供了现成的 `ValidateTargetRef` 工具函数，校验器可直接复用（范本校验器 [第 34 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/clientsettings/validator.go#L34) 就在用）。

**第 9 站：受影响条件（可选）。**

[internal/controller/state/graph/policies.go:889-925](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L889-L925) 的 `addStatusToTargetRefs` 用 `switch policyKind` 给目标资源打「受某策略影响」的条件（如 `ClientSettingsPolicyAffected`）。如果你的策略也需要这种条件，要在这里加一个 `case kinds.FooPolicy`，并在 `internal/controller/state/conditions/conditions.go` 加对应工厂（u8-l1 讲过 conditions 词典）。

> 小结：上面 9 站里，第 1、2、3、4、5、6、7 站是**必改**，第 8 站**通常不用改**（这是设计红利），第 9 站**可选**。这正是 NGF 架构的可扩展性所在。

#### 4.1.4 代码实践

**实践目标**：用一个已有策略 `ClientSettingsPolicy` 做一次「反向追踪」，验证上面 9 站的接线点都能在源码里找到，建立对链路的肌肉记忆。

**操作步骤**：

1. 在仓库根目录，用 Grep 搜索 `ClientSettingsPolicy` 在 `apis/`、`internal/framework/kinds/`、`internal/controller/manager.go`、`internal/controller/state/change_processor.go` 中的出现位置。
2. 记录每一处的文件路径与行号，对应到本讲 4.1.3 的 9 站。
3. 在 `internal/controller/nginx/config/policies/clientsettings/` 下打开 `generator.go` 与 `validator.go`，确认它们分别实现了 `policies.Generator` 与 `policies.Validator` 接口。
4. 在 `internal/controller/nginx/config/generator.go` 与 `internal/controller/manager.go`（`createPolicyManager`）里找到把它们登记进组合器的那两行。

**需要观察的现象**：

- 同一个类型名 `ClientSettingsPolicy` 在 7~8 个不同文件里各出现一次，每次承担一个不同职责——这正是「分层」的可读证据。
- `createPolicyManager` 与 `Generate` 里登记的顺序是策略「声明顺序」，不是执行顺序；执行由 GVK 路由决定。

**预期结果**：你能画出一张包含 9 个节点的链路图，每个节点标注文件路径与行号，且第 8 站（图附着）确认无需改动。

**待本地验证**：本实践是源码阅读型，不产生运行时输出；若想进一步验证，可参考 4.3 的综合实践。

#### 4.1.5 小练习与答案

**练习 1**：如果新增策略时**忘记**在 `createPolicyManager` 里登记校验器，会发生什么？

**参考答案**：当集群里出现该策略资源、图层 `processPolicies` 调用 `CompositeValidator.Validate` 时，[validator.go:62-65](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/validator.go#L62-L65) 会在 map 里找不到对应 GVK 的校验器而 `panic`，导致控制面进程崩溃。这是 fail-fast 设计，强制开发者必须登记。

**练习 2**：为什么图层的 `attachPolicies` 对新策略「通常不用改」？它的分发依据是什么？

**参考答案**：因为 `attachPolicies`（[policies.go:254-281](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/graph/policies.go#L254-L281)）按 **targetRef 的 group/kind** 分发，而不是按策略自身的 CRD 类型分发。只要新策略实现了 `policies.Policy` 接口、targetRef 指向 Gateway/HTTPRoute/GRPCRoute/Service 之一，就会被自动收进处理流程并附着到目标节点。

**练习 3**：`UnimplementedGenerator` 解决了什么问题？不用它会怎样？

**参考答案**：`Generator` 接口有五个方法（对应五个 NGINX 上下文），但一个具体策略通常只关心一两个。`UnimplementedGenerator`（[generator.go:97-119](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/generator.go#L97-L119)）把其余方法实现为返回 `nil`，让生成器嵌入它后**只需重写关心的上下文**就能满足接口。不用它的话，每个生成器都要手写五个方法，其中多数是空实现，造成大量样板代码。

---

### 4.2 代码生成

#### 4.2.1 概念说明

NGF 是一个**重度依赖代码生成**的项目。你手写的 Go 类型只是「真相之源」，大量周边产物——CRD 的 YAML、deepcopy 方法、测试用的 fake、API 参考文档、Helm chart 的 schema 与 README、部署清单——全都是由工具从你的类型与注解**自动生成**的。

这带来一条铁律：

> **改了 API 类型或注解之后，必须重新跑代码生成，否则生成的产物与源码不一致。**

NGF 用一个 Makefile 目标 `generate-all` 把所有生成器串成一把「总钥匙」。理解每个生成器产出什么、何时该跑，是二次开发的基本功。

#### 4.2.2 核心流程

NGF 的代码生成器及其产物如下表：

| 生成器 | Makefile 目标 | 触发指令 | 产物 | 何时跑 |
| --- | --- | --- | --- | --- |
| `go generate` | `generate` | 源码里的 `//go:generate` 注释 | counterfeiter 的 fake | 改了接口（带 `//counterfeiter:generate`）后 |
| `controller-gen` | `generate-crds` | `apis/` 下的 kubebuilder 注解 | CRD YAML + deepcopy | 改了 API 类型/注解后 |
| `helm-schema` | `generate-helm-schema` | `charts/` 的 values | Helm values schema | 改了 chart values 后 |
| `generate-manifests.sh` | `generate-manifests` | Helm chart | `deploy/` 单文件清单 | 改了 chart 后 |
| `gen-crd-api-reference-docs` | `generate-api-docs` | `apis/` | API 参考文档 | 改了 API 类型后 |
| `helm-docs` | `generate-helm-docs` | chart | Helm README | 改了 chart 后 |
| 总钥匙 | `generate-all` | 上述全部 + RBAC 校验 | 全部产物 | 提交前必跑 |

两个最常用的生成器需要展开讲：

**counterfeiter（fake 生成）**：NGF 的测试大量使用 fake 来隔离依赖（u13-l1 讲过）。fake 不是手写的，而是由 counterfeiter 根据接口定义自动生成。你在接口上方写一行 `//counterfeiter:generate . Generator`，跑 `make generate` 后，它会在 `<包>fakes/fake_<接口>.go` 产出 fake，提供 `XxxReturns`（控制行为）与 `XxxCallCount`（验证交互）三类能力。本讲涉及的 `Generator`、`Validator`、`Policy` 三个接口都有这个指令。

**controller-gen（CRD + deepcopy 生成）**：`make generate-crds` 跑 `controller-gen crd object paths=./apis/...`。其中：

- `crd`：读类型上方的 `// +kubebuilder:` 注解，生成 CRD 的 YAML（含 CEL 校验、打印列、shortName 等），落在 `config/crd/bases/`，再经 kustomize 拼成 `deploy/crds.yaml`。
- `object`：读 `doc.go` 里的 `+kubebuilder:object:generate=true`（[apis/v1alpha1/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/doc.go)），为每个类型生成 `DeepCopyInto`/`DeepCopy`/`DeepCopyObject` 方法，落在 `apis/v1alpha1/zz_generated.deepcopy.go`。**没有这些方法，类型就无法被 runtime 工作（scheme 要求实现 `runtime.Object`）**。

#### 4.2.3 源码精读

**总钥匙 `generate-all`。**

[Makefile:192-193](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L192-L193) 定义 `generate-all`，把六个生成目标加上 `verify-operator-rbac` 串成一个目标。提交 PR 前跑这一个命令即可保证所有产物同步。

**`go generate`。**

[Makefile:147-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L147-L149) 定义 `generate`，执行 `go generate ./...`。它会扫描全仓所有 `//go:generate` 注释——主要是 counterfeiter 指令。

[counterfeiter 指令范本见 policies 包](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L11-L15)：[policy.go:11](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L11) 的 `//go:generate go tool counterfeiter -generate` 是包级开关，[第 15 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/policy.go#L15) 的 `//counterfeiter:generate . Policy` 标记要为 `Policy` 接口生成 fake。新增一个带测试的接口时，照此加两行指令即可。

**`controller-gen`。**

[Makefile:151-155](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L151-L155) 定义 `generate-crds`：跑 `controller-gen crd object paths=./apis/...`，再用 `strip-crd-excludes.sh` 清理、用 kustomize 拼出 `deploy/crds.yaml`。

deepcopy 的触发根在 [apis/v1alpha1/doc.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/doc.go) 顶部的 `+kubebuilder:object:generate=true` 与 `+groupName=gateway.nginx.org`。新增类型后重跑 `generate-crds`，`apis/v1alpha1/zz_generated.deepcopy.go` 就会补上新类型的 deepcopy 方法。

**scheme 注册与 deepcopy 的关系。**

[apis/v1alpha1/register.go:33-56](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/apis/v1alpha1/register.go#L33-L56) 的 `addKnownTypes` 把类型登记进 scheme，但这些类型要能作为 `runtime.Object` 被序列化，**必须有 deepcopy 方法**。所以「登记进 scheme」与「生成 deepcopy」是配套的：前者你手写一行，后者由 controller-gen 自动产出。这也是为什么新增类型后必须跑 `generate-crds`——否则 deepcopy 缺失，scheme 注册会在运行时报错。

**官方说明。**

[docs/developer/quickstart.md:247-253](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L247-L253) 的 "Update all the generated files" 一节明确：跑 `make generate-all` 即可更新所有生成文件。这是官方对「何时跑生成」的标准答复。

#### 4.2.4 代码实践

**实践目标**：亲手跑一次 `make generate`，观察 counterfeiter 如何根据接口重新生成 fake，理解「源码即真相」。

**操作步骤**：

1. 在仓库根目录执行 `make generate`（需要 Go 工具链；参考 u1-l3 的构建环境）。
2. 用 `git status` 查看哪些 `zz_generated*.go`、`fake_*.go` 文件被触碰。
3. 任选一个接口（例如 `internal/controller/nginx/config/policies` 的 `Generator`），找到其 `//counterfeiter:generate . Generator` 指令，打开对应的 `policiesfakes/fake_generator.go`，确认它有 `GenerateForServerReturns`、`GenerateForServerCallCount` 等方法。
4. 故意删除一个 fake 文件，再跑一次 `make generate`，确认它被重新生成且内容一致。

**需要观察的现象**：

- `make generate` 之后，工作区应当**没有实质性变更**（因为代码本来就同步）；若有 diff，说明之前有人忘了跑生成。
- fake 文件都带 `// Code generated by counterfeiter. DO NOT EDIT.` 头部注释，禁止手改。

**预期结果**：理解 fake 与 deepcopy 都是「只读产物」，源码改了就要重跑生成；`git status` 干净是「生成已同步」的信号。

**待本地验证**：具体被触碰的文件列表取决于本地状态，请以实际 `git status` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：新增一个 CRD 类型后，如果只改了 `register.go` 登记、却没跑 `generate-crds`，会在哪个环节出问题？

**参考答案**：新类型缺少 `DeepCopyObject` 方法（由 controller-gen 的 `object` 标记生成），无法满足 `runtime.Object` 接口，于是 `scheme.AddKnownTypes` 在运行时序列化该类型时会报错；同时 CRD 的 YAML（`deploy/crds.yaml`）也不会更新，集群里根本没有这个 CRD 可用。

**练习 2**：`make generate` 和 `make generate-crds` 各自负责什么？为什么需要分开？

**参考答案**：`make generate`（[Makefile:147-149](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L147-L149)）跑 `go generate ./...`，主要产出 counterfeiter 的 fake；`make generate-crds`（[Makefile:151-155](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L151-L155)）跑 `controller-gen`，产出 CRD YAML 与 deepcopy。分开是因为它们由不同工具、不同触发指令驱动；但日常用 `make generate-all`（[第 192-193 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/Makefile#L192-L193)）一次跑全即可。

**练习 3**：为什么 fake 文件头部都写着 `DO NOT EDIT`？

**参考答案**：因为 fake 是 counterfeiter 根据接口定义自动生成的产物，手改的内容会在下次 `make generate` 时被覆盖。正确的做法是改接口定义（源码），再重跑生成，让 fake 自动同步。

---

### 4.3 PR 流程

#### 4.3.1 概念说明

NGF 是一个有严格贡献规范的开源项目。代码改对了≠能合并——你还得走完一套**标准化的 PR 流程**，包括认领 issue、fork、命名分支、写测试、更新文档、跑生成器、获得评审、最后 squash 合并。

这套流程的权威依据是官方文档 [docs/developer/implementing-a-feature.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md)。它列了 16 个步骤，本讲把它们归纳成四个阶段，并与前面讲的「功能开发链路」「代码生成」串起来。

#### 4.3.2 核心流程

NGF 的功能开发流程可归纳为四个阶段：

**阶段一：立项与分支（步骤 1-4）**

1. 先在 GitHub 上开一个 issue（或认领已有 issue），**在动手写代码前先讨论**——文档开头的 Note 明确要求。NGF 不接受没有 issue 的功能 PR。
2. fork 仓库，按 [branching-and-workflow](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/branching-and-workflow.md) 的命名规范开分支。
3. 阅读 [go-style-guide](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/go-style-guide.md)，熟悉代码风格。

**阶段二：写代码与测试（步骤 5-9）**

4. 按 4.1 的链路改代码。
5. **每个功能必须配单元测试**，覆盖正常与边界场景，并检查覆盖率（见 [testing 文档](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/testing.md)）。NGF 的测试用 fake 隔离依赖（u13-l1），新增接口要跑 counterfeiter 生成 fake。
6. 手动验证改动（在 kind 集群里跑通，参考 u1-l3/u1-l5）。
7. 更新相关文档：Gateway API 特性要更新兼容性文档、新用例要在 `examples/` 加示例、改了 CLI 要更新 cli-help 文档、改了 Helm values 要更新 Helm README。

**阶段三：生成与提交（步骤 10-13）**

8. 跑 `make lint`（参考 quickstart 的 linter 一节）。
9. **跑 `make generate-all`**（4.2 讲过），确保所有生成产物同步——这是 PR 能过 CI 的前提。
10. 向 `main` 分支开 PR。整个 `nginx-gateway-fabric` 组会被自动请求评审。
11. 与评审者协作拿到足够数量的 approval。

**阶段四：合并与收尾（步骤 14-16）**

12. 若改了产品遥测的数据点，要把生成的 `.avdl` schema 推到 schema registry，并手动验证数据成功上报（u11-l2 讲过遥测契约）。
13. **Squash and merge**：每个 PR 只合并一个 commit，commit 首行要带 PR 号（如 `Fix supported gateway conditions ... (#674)`）。squash 时不要夹带「修了个 typo」之类的代码评审痕迹。
14. 若是修 bug，流程同上，但多一条：**先用单元测试复现 bug**，再改代码让它通过（文档末尾 "Fixing a Bug" 一节）。

> 注意阶段三的「跑生成」与阶段二的「写测试」是**强制的**——CI 会校验生成产物是否同步、测试是否通过。跳过它们，CI 会直接红灯。

#### 4.3.3 源码精读

**16 步流程的权威清单。**

[docs/developer/implementing-a-feature.md:11-72](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L11-L72) 逐条列出从「认领 issue」到「squash 合并」的 16 步。其中与本讲最相关的是：

- [第 8 步（第 28-32 行）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L28-L32)：强制要求单元测试，并要求查看覆盖率报告。
- [第 12 步（第 52-55 行）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L52-L55)：要求跑 `go generate` 与更新生成清单——即 4.2 的两个目标。
- [第 15 步（第 62-65 行）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L62-L65)：改了遥测数据点要推 `.avdl` schema——这正是 u11-l2 讲的 Avro 契约。
- [第 16 步（第 66-72 行）](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L66-L72)：squash 合并规范，首行带 PR 号、不带评审痕迹。

**修 bug 的额外要求。**

[docs/developer/implementing-a-feature.md:74-81](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L74-L81) 的 "Fixing a Bug" 一节明确：所有 bug 修复**必须先用单元测试复现**，再改代码。这种「测试先行」的纪律防止回归。

**生成入口的官方说明。**

[docs/developer/quickstart.md:247-253](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L247-L253) 指出 `make generate-all` 是更新所有生成文件的官方手段，对应流程的阶段三第 9 步。

#### 4.3.4 代码实践

**实践目标**：把本讲的全部知识串起来——设计一个新的简单策略 CRD `FooPolicy`，列出从 API 定义到配置生成、再到 PR 提交的**完整文件改动清单**。这是本讲规格里要求的核心实践任务。

**操作步骤**：

1. 设定一个最小需求：`FooPolicy` 是一个 Direct 附着策略，targetRef 指向 HTTPRoute，spec 只有一个字段 `fooHeader: string`，生成的 NGINX 指令是 `add_header X-Foo <value>;`（落在 location 上下文）。
2. 按 4.1 的 9 站 + 4.2 的生成步骤 + 4.3 的 PR 步骤，逐项列出要新建/修改的文件。
3. 对每个文件，写明「新建还是修改」「改什么」。
4. 标注哪些步骤必须跑 `make generate-all`、哪些必须配测试。

**需要观察的现象**：你会发现一个「只加一个字段」的需求，实际上要动 10+ 个文件、跑两次生成器、写至少 3 个测试文件。这是大型分层项目的常态——也是 4.1 强调「分层」的代价与收益。

**预期结果**：得到一张清晰的改动清单（参考答案见下方「综合实践」）。

**待本地验证**：本实践不要求真正实现，只要求清单完整、可执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 NGF 要求「在写代码前先开 issue 讨论」？

**参考答案**：见 [implementing-a-feature.md 开头的 Note](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L6-L9)。先讨论能确保改动符合项目目标与架构方向，避免开发者花大量精力写出不符合预期的功能、或与已有计划冲突。重大架构改动还鼓励先开 draft PR 征求早期反馈。

**练习 2**：squash 合并时，commit 首行为什么必须带 PR 号、且不能夹带评审痕迹？

**参考答案**：见 [implementing-a-feature.md 第 66-72 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L66-L72)。带 PR 号是为了让 commit 历史能反查到原始讨论；不夹带评审痕迹（如「修了个 typo」）是因为 squash 后这些中间信息失去上下文、会污染历史——如果评审导致行为变化，应直接更新主 commit message 使其准确描述最终改动。

**练习 3**：修一个 bug 时，为什么要「先用单元测试复现」？

**参考答案**：见 [implementing-a-feature.md 第 74-81 行](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/implementing-a-feature.md#L74-L81)。先用测试复现，能证明你真正理解了 bug 的触发条件，改完之后测试由红转绿就是修复的证据，而这个测试留在仓库里永久防止同一 bug 回归。

## 5. 综合实践

**任务**：设计一个新的简单策略 CRD `FooPolicy`，产出从 API 定义到配置生成、再到 PR 提交的完整文件改动清单。

**需求设定**：

- `FooPolicy` 是一个 **Direct** 附着策略（不继承），targetRef 指向 `HTTPRoute`（同命名空间）。
- spec 唯一字段：`fooHeader: string`，要求 CEL 校验非空。
- 生成 NGINX 指令：`add_header X-Foo <fooHeader>;`，落在 **location** 上下文。
- 不依赖 NGINX Plus、不依赖 NginxProxy 全局配置。

**参考改动清单**（这是本讲的「答卷」，也是你真正动手时的脚手架）：

| # | 文件 | 新建/修改 | 改动内容 | 所属阶段 |
| --- | --- | --- | --- | --- |
| 0 | GitHub issue | 新建 | 描述需求、设计草案，先讨论 | PR 立项 |
| 1 | `apis/v1alpha1/foopolicy_types.go` | 新建 | `FooPolicy`/`FooPolicyList` struct，带 `+kubebuilder:metadata:labels="...policy=direct"`、`XValidation` 等注解 | API（4.1 第 1 站） |
| 2 | `apis/v1alpha1/register.go` | 修改 | `addKnownTypes` 加 `&FooPolicy{}`、`&FooPolicyList{}` | scheme（4.1 第 2 站） |
| 3 | `internal/framework/kinds/kinds.go` | 修改 | 加 `FooPolicy = "FooPolicy"` 常量 | Kind（4.1 第 3 站） |
| 4 | `internal/controller/manager.go` | 修改 | `ctlrCfg` 加 watch 单元（`GenerationChangedPredicate`）；确认 scheme 已含 v1alpha1 | 控制器（4.1 第 4 站） |
| 5 | `internal/controller/state/change_processor.go` | 修改 | store 列表加 FooPolicy GVK→`commonPolicyObjectStore`、谓词 `isNGFPolicyRelevant` | 存储（4.1 第 5 站） |
| 6 | `internal/controller/nginx/config/policies/foo/validator.go` | 新建 | 实现 `policies.Validator`（`Validate` 校验 targetRef 是 HTTPRoute；`ValidateGlobalSettings` 返 nil；`Conflicts` 同 spec 即冲突） | 校验（4.1 第 6 站） |
| 7 | `internal/controller/manager.go`（`createPolicyManager`） | 修改 | `ManagerConfig` 列表加 `{GVK, foo.NewValidator(validator)}` | 校验接线（4.1 第 6 站） |
| 8 | `internal/controller/nginx/config/policies/foo/generator.go` | 新建 | 嵌入 `UnimplementedGenerator`，重写 `GenerateForLocation`，用 `text/template` 渲染 `add_header X-Foo <v>;` | 生成（4.1 第 7 站） |
| 9 | `internal/controller/nginx/config/generator.go` | 修改 | `NewCompositeGenerator(...)` 列表加 `foo.NewGenerator()` | 生成接线（4.1 第 7 站） |
| 10 | 图附着 `policies.go` | **不改** | 验证 `processPolicies`/`attachPolicies` 自动按 targetRef 分发 | 设计红利（4.1 第 8 站） |
| 11 | `internal/controller/state/graph/policies.go`（`addStatusToTargetRefs`） | 可选修改 | 加 `case kinds.FooPolicy` + `conditions` 包加工厂（如需「受影响」条件） | 条件（4.1 第 9 站） |
| 12 | `internal/controller/nginx/config/policies/foo/validator_test.go` | 新建 | 校验器单元测试（合法/非法 targetRef、冲突场景） | 测试（4.3） |
| 13 | `internal/controller/nginx/config/policies/foo/generator_test.go` | 新建 | 生成器单元测试（断言产出含 `add_header X-Foo`） | 测试（4.3） |
| 14 | `internal/controller/state/change_processor_test.go` 或 graph 策略测试 | 修改/新建 | 端到端验证 FooPolicy 被捕获、附着、生成 | 测试（4.3） |
| 15 | 终端 | 运行 | `make generate`（counterfeiter 生成新 fake） | 生成（4.2） |
| 16 | 终端 | 运行 | `make generate-crds`（产出 CRD YAML + deepcopy） | 生成（4.2） |
| 17 | 终端 | 运行 | `make generate-all`（同步 Helm schema/manifests/api-docs）+ `make lint` + `make unit-test` | PR（4.3） |
| 18 | `examples/foo-policy/` | 新建 | 加一个用法示例（参考 u1-l5 的示例组织） | 文档（4.3） |
| 19 | PR | 新建 | 向 `main` 开 PR，描述 + 关联 issue，过 CI 后 squash 合并 | 合并（4.3） |

**完成后自检**：

- 跑 `make unit-test` 全绿、`make generate-all` 后 `git status` 干净。
- 部署到 kind（u1-l3），apply 一个 `FooPolicy` 指向某 HTTPRoute，curl 该路由，在数据面 Pod 的 `/etc/nginx/conf.d/*.conf` 里找到 `add_header X-Foo ...;` 指令。
- 给一个非法 targetRef（如指向 Service），确认策略 status 出现 `Accepted=False` 且原因合理。

**待本地验证**：本实践是设计型，上面是清单与自检方法；真实运行结果以本地 kind 集群为准。

## 6. 本讲小结

- **功能开发链路是分层的**：新增一个策略要依次穿过 API 类型→scheme→Kind 常量→控制器 watch→变更存储→校验器→生成器→（图附着，通常免改）→（条件，可选）共约 9 站，每站都有明确的接线点。
- **每一层都用「接口 + 组合器 + 注册表」三件套**：新增策略只需「写一个新实现 + 在注册表登记一次」，不必动已有策略代码——这是开闭原则的落地；未登记校验器会导致 `CompositeValidator` panic（fail-fast）。
- **图层附着是策略无关的设计红利**：`processPolicies`/`attachPolicies` 按 targetRef 的 group/kind 分发，新策略只要实现 `policies.Policy` 接口就免费获得附着、继承、冲突裁决。
- **代码生成是强制的**：CRD YAML 与 deepcopy 由 `controller-gen` 生成、测试 fake 由 counterfeiter 生成，改了 API 或接口后必须跑 `make generate-all` 同步，否则 CI 红灯、运行时缺 deepcopy 报错。
- **PR 流程有 16 步**：立项→写码与测试→生成与提交→合并收尾；核心纪律是「先开 issue 讨论、每个功能配单测、提交前跑 `generate-all`、squash 单 commit 带 PR 号」。
- **范本是最好的老师**：`ClientSettingsPolicy` 的 generator/validator 是「字段→指令」最简范本，新增策略时照抄它的结构与接线点即可。

## 7. 下一步学习建议

本讲是学习手册的收官，建议你用以下方式把知识转化为能力：

1. **动手实现综合实践里的 `FooPolicy`**：哪怕只跑到「单测全绿 + kind 里 curl 验证指令出现」，也会让你对整条链路的理解从「读过」升到「会写」。
2. **精读一个更复杂的策略生成器**：`ratelimit`（[internal/controller/nginx/config/policies/ratelimit/](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/policies/ratelimit/generator.go)）横跨 http 与 server/location 两个上下文、用「影子策略 + 注解」解决跨上下文 zone 声明，是进阶范本（u8-l3 有概述）。
3. **通读官方开发文档全集**：从 [design-principles.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/design-principles.md)、[crd-versioning.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/crd-versioning.md) 到 [go-style-guide.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/go-style-guide.md)，建立对项目架构取舍与代码规范的全局观。
4. **回头看整条数据流**：至此你已读完 u1→u13，建议重走一次「CLI→Manager→事件管线→图→配置生成→Agent 下发→状态/策略」的主干（u2→u8），这一次你会发现每个组件都「活」了起来——因为你知道了它们各自的可扩展点在哪里。
5. **给上游提一个真 PR**：从 `good first issue` 起步，走一遍本讲的 16 步流程，把这套方法论变成肌肉记忆。

恭喜你读完整本《NGINX Gateway Fabric 项目学习手册》。源码是活的，文档会过期，但分层架构与「接口 + 组合器」的设计范式是稳定的——带着这套框架去读任何控制器项目，你都能快速定位它的扩展点。
