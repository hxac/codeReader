# Configurator：配置生成的中枢

## 1. 本讲目标

在前一单元里，我们已经看清控制器的「世界模型」`Configuration`：它裁决**谁赢得某个 host、谁被拒绝**，产出 `ResourceChange`。但「裁决该生成谁」并不等于「真的生成了配置」——把一个 `VirtualServerEx` 翻译成 `nginx.conf` 片段、写进 conf.d、再让 NGINX 重新加载，是另一段独立的旅程，而这段旅程的驾驶员就是 `Configurator`。

本讲结束后，你应当能够：

- 说清 `Configurator` 在五层架构中的位置与职责边界（它只「执行」，不「裁决」）。
- 描述任意一个资源入口方法（`AddOrUpdateIngress` / `AddOrUpdateVirtualServer` / `AddOrUpdateTransportServer`）内部的「生成 → 渲染 → 写文件」三段式。
- 理解它如何编排 v1（Ingress / main）与 v2（VirtualServer / TransportServer / OIDC）两套模板执行器。
- 解释 `isReloadsEnabled` 这个开关如何让控制器在启动期「累积写盘、最后一次性 reload」，以及为什么 NGINX Plus 能在端点变更时**免重载**。

## 2. 前置知识

阅读本讲前，你需要已经掌握（这些是前面讲义的结论，本讲直接承接，不再重复）：

- **资源模型**：VirtualServer / VirtualServerRoute / TransportServer / Policy 的 Go 类型与字段（u2-l2）。
- **控制器调度**：`sync` 按 Kind 分发，`processChanges` 拿着 `ResourceChange` 去调谐，传入的是已经组装好的「扩展资源」如 `VirtualServerEx`（u3-l5）。
- **内存模型**：`Configuration` 负责「生成谁、拒绝谁」，产出 `ResourceChange` 与 `ConfigurationProblem`（u3-l6）。
- **NGINX 指令常识**：`server` / `location` / `upstream` 是 http 上下文，`stream` 上下文用于四层；`.conf` 文件放在 conf.d 目录，NGINX 用 `include` 加载。
- **Go 模板**：`text/template` 把一个结构体渲染成文本，模板文件就是 `.tmpl`。

一句话定位：上一讲 `Configuration` 输出「决策」，本讲 `Configurator` 把决策**落地成磁盘上的配置文件并触发 NGINX 行为**。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/configs/configurator.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go) | 本讲主角。定义 `Configurator` 结构体、构造函数，以及全部资源入口方法（增/改/删、批量、端点更新）。 |
| [internal/configs/common.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/common.go) | 生成链路上的公共辅助，如 `escapeNginxString`——所有要写进 NGINX 配置的字符串都要先经过它（配置注入安全的落点，详见 u6-l1）。 |
| [internal/configs/version1/template_executor.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/template_executor.go) | v1 模板执行器：渲染 main 配置（`nginx.conf`）与 Ingress 配置。 |
| [internal/configs/version2/template_executor.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version2/template_executor.go) | v2 模板执行器：渲染 VirtualServer、TransportServer、OIDC、TLS Passthrough 映射。 |
| [internal/nginx/manager.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/nginx/manager.go) | `Manager` 接口与 `LocalManager` 实现：`CreateConfig` 真正写文件，`Reload` 真正触发重载。本讲只引用接口契约，进程细节留待 u5 单元。 |

> 心智模型：`Configurator` 是「翻译 + 编排」层，它自己**不直接**碰 NGINX 进程，而是通过持有 `nginx.Manager` 接口把副作用（写文件、重载、Plus API）委托出去。

## 4. 核心概念与源码讲解

### 4.1 Configurator 结构与它在架构中的位置

#### 4.1.1 概念说明

在五层架构（数据模型 → 校验 → 控制器 → **配置生成** → 进程管理）里，`Configurator` 正是第四层的核心类型。它的职责可以用一句话概括：

> 把 `Configuration`（世界模型）已经裁决过的扩展资源（`VirtualServerEx` 等），翻译成 NGINX 能识别的 `.conf` 文件，并在合适的时机让 NGINX 重新加载。

注意职责边界：`Configuration` 决定「**该不该**为这个资源生成配置、它有没有抢赢 host」；`Configurator` 只负责「**既然要生成，就把它生成出来并落地**」。`Configurator` 内部也维护一份自己的资源表（`ingresses` / `virtualServers` / `transportServers` 等），但这只是为了知道「磁盘上当前有哪些 `.conf` 文件、对应的资源长什么样」，它**不**做 host 冲突裁决——那是 `Configuration` 的事。

为什么要把这两者分开？因为「裁决」是纯内存、可重复、无副作用的计算；而「生成配置」涉及写文件和 reload，是**有副作用且昂贵**的操作。把二者解耦后，控制器可以在批量处理时先在 `Configuration` 里算清所有变更，再让 `Configurator` 一次性落地。

#### 4.1.2 核心流程

`Configurator` 处理一个资源时，遵循一条稳定的「**生成 → 渲染 → 写盘**」流水线：

```text
扩展资源 (如 VirtualServerEx)
   │
   ├─ 1. 生成：调用专门的生成器函数，把资源翻译成「模板输入结构体」
   │       如 GenerateVirtualServerConfig → version2.VirtualServerConfig
   │       （这一步把上游、location、TLS、策略等组装好，且做转义）
   │
   ├─ 2. 渲染：调用对应模板执行器，text/template 把结构体渲染成文本
   │       如 templateExecutorV2.ExecuteVirtualServerTemplate(&vsCfg) → []byte
   │
   ├─ 3. 写盘：通过 nginxManager.CreateConfig(name, content)
   │       把文本写到 conf.d/<name>.conf，并返回 configChanged
   │
   └─ 4. （可选）触发副作用：Reload 或 Plus API 动态更新
```

第 1、2、3 步是「纯生成」，没有 NGINX 副作用；第 4 步才真正改变 NGINX 运行时行为。这个划分是后续「reload 控制」能成立的基础。

#### 4.1.3 源码精读

先看结构体本身，理解它持有哪些「家当」：

[internal/configs/configurator.go:128-151](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L128-L151) — `Configurator` 结构体。关键字段可以分成四组：

- **委托对象**：`nginxManager nginx.Manager`（写文件与重载的出口）、`templateExecutor *version1.TemplateExecutor`、`templateExecutorV2 *version2.TemplateExecutor`（两套模板执行器）。
- **配置参数**：`staticCfgParams`、`CfgParams`、`MgmtCfgParams`——承载 ConfigMap 解析出的全局参数（详见 u4-l2）。
- **自己的资源表**：`ingresses`、`virtualServers`、`transportServers`、`mergeableIngresses`、`minions`、`tlsPassthroughPairs`，记录磁盘上当前有哪些配置文件及其来源资源。
- **运行期开关与可观测性**：`isPlus`、`isReloadsEnabled`（本讲重点）、`isPrometheusEnabled`、`labelUpdater`、`metricLabelsIndex`、`latencyCollector` 等。

注意结构体上方那段注释（L123-127）点出了 `isReloadsEnabled` 的设计意图：在 reload 未启用前，配置变更**只会写盘、不会重载、也不会走 Plus API**，从而让控制器能在启动期把配置逐条累积，最后一次性应用。

构造函数 `NewConfigurator` 把这些字段装配好，并把 reload 开关初始化为关闭：

[internal/configs/configurator.go:174-211](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L174-L211) — 注意 L208 的 `isReloadsEnabled: false`：**Configurator 一出生就是「只写不重载」状态**，必须等控制器显式 `EnableReloads()` 后才会真正 reload。

生成链路的「公共辅助」落在 common.go，其中 `escapeNginxString` 是最基础的安全函数：

[internal/configs/common.go:5-11](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/common.go#L5-L11) — 对反斜杠和双引号转义。Configurator 调用的各类生成器（`generateNginxCfg`、`GenerateVirtualServerConfig` 等）在把用户字符串拼进 NGINX 指令前都会经过这类函数，防止用户值破坏配置语法（更完整的危险字符防护在 u6-l1 讲）。

#### 4.1.4 代码实践（源码阅读型）

**目标**：用「四组字段」给 Configurator 的家当做分类。

**步骤**：

1. 打开 [internal/configs/configurator.go:128-151](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L128-L151)。
2. 逐个字段判断它属于「委托对象 / 配置参数 / 资源表 / 运行期开关与可观测性」哪一组。
3. 特别留意 `isReloadsEnabled`，并问自己：如果它为 `false`，下面 4.4 节的 `Reload` 会发生什么？

**预期结果**：你会得到一张四列表格，并理解「为什么 Configurator 同时持有 `Configuration` 之外的资源表」——因为它要追踪磁盘文件状态，而 `Configuration` 追踪的是 host 裁决状态。

#### 4.1.5 小练习与答案

**练习 1**：`Configurator` 和 `Configuration` 都各自维护了一份 `virtualServers` 表，它们的作用一样吗？

> **答案**：不一样。`Configuration`（`internal/k8s/configuration.go`）的表用于 **host 裁决与冲突检测**，决定一个 VS 是否被授权生成配置；`Configurator` 的表用于 **追踪磁盘上 conf.d 里实际写了哪些 `.conf` 文件**。前者是「该不该」，后者是「写没写」。

**练习 2**：为什么 `NewConfigurator` 要把 `isReloadsEnabled` 初始化为 `false`？

> **答案**：让控制器在启动期能先把所有资源的配置文件**累积写盘**而不会每写一个就 reload 一次（否则启动期会有 N 次重载）。等缓存同步、状态算清后，控制器再 `EnableReloads()` 并一次性 reload（见 4.4.3）。

### 4.2 资源入口方法：统一的「生成 → 渲染 → 写盘」三段式

#### 4.2.1 概念说明

`Configurator` 对外暴露一组 `AddOrUpdateXxx` / `DeleteXxx` 方法，每一种路由资源（Ingress、Mergeable Ingress、VirtualServer、TransportServer）都对应一对。这些方法是控制器的 `processChanges`（u3-l5）真正调用的入口。

它们遵循一个高度一致的**双层模式**：

- **公共方法** `AddOrUpdateXxx`（大写开头）：调用同名的私有方法 `addOrUpdateXxx`（小写开头）做「纯生成 + 写盘」，**然后**调用 `Reload`。每个资源一次重载。
- **私有方法** `addOrUpdateXxx`：只做「生成 → 渲染 → 写盘」，**不重载**。

为什么要拆成两层？因为**批量路径**（`AddOrUpdateResources`、`UpdateConfig`）希望「写 N 个文件、只重载 1 次」。它们直接复用私有方法，把 N 次 reload 压缩成 1 次。如果重载逻辑焊死在私有方法里，就做不到这件事了。

#### 4.2.2 核心流程

以 Ingress 为例的调用关系：

```text
processChanges (u3-l5)
   └─ AddOrUpdateIngress (公共)            ← 每资源一次 reload
        ├─ addOrUpdateIngress (私有)        ← 纯生成 + 写盘，不 reload
        │    ├─ generateNginxCfg(...)       → version1.IngressNginxConfig（模板输入）
        │    ├─ templateExecutor.ExecuteIngressConfigTemplate(&cfg) → []byte
        │    ├─ nginxManager.CreateConfig(name, content)            → 写盘 + configChanged
        │    └─ cnf.ingresses[name] = ingEx                        → 更新自己的资源表
        └─ cnf.Reload(...)                  ← 触发重载（受 isReloadsEnabled 门控）
```

VirtualServer、TransportServer 的结构完全同构，只是生成器函数、模板执行器、写盘方法不同。VirtualServer 还多出「OIDC 子配置」与「weight 更新」两个分支（见 4.2.3）。

#### 4.2.3 源码精读

**Ingress 公共入口**——公共 / 私分离的最小样本：

[internal/configs/configurator.go:303-315](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L303-L315) — `AddOrUpdateIngress` 先调 `addOrUpdateIngress` 拿到 warnings，再调 `cnf.Reload`。注意错误都被 `fmt.Errorf("...: %w", err)` 包裹了上下文，方便排查是「生成」还是「重载」阶段出错。

**Ingress 私有三段式**——最清晰的「生成 → 渲染 → 写盘」样板：

[internal/configs/configurator.go:487-547](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L487-L547) — 关键四步：

1. L488 `updateApResources(ingEx)`：先把 WAF（App Protect）资源文件落盘——它们必须在模板渲染前就位。
2. L504-515 `generateNginxCfg(NginxCfgParams{...})`：把 `IngressEx` 翻译成 `version1.IngressNginxConfig`（含 upstream / location / TLS）。生成细节在 u4-l4。
3. L524 `ExecuteIngressConfigTemplate(&nginxCfg)`：渲染成文本。
4. L528 `cnf.nginxManager.CreateConfig(configName, content)`：写盘，返回 `configChanged`，表示这次内容是否真的变了。
5. L539 `cnf.ingresses[name] = ingEx`：更新自己的资源表，L543 `syncDefaultServerConfig()` 同步兜底默认 server 配置。

**VirtualServer 公共入口**——比 Ingress 多出两个分支：

[internal/configs/configurator.go:718-738](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L718-L738) — `AddOrUpdateVirtualServer` 先调私有方法拿到 `weightUpdates`；如果有 weight 更新（L725-727）会先 `EnableReloads()` 保证重载开关打开；reload 之后（L733-735）再用 `UpsertSplitClientsKeyVal` 通过 **Plus key-val API** 动态改流量权重——这是「免重载调权重」的入口。

**VirtualServer 私有三段式**——本讲实践任务的核心：

[internal/configs/configurator.go:740-802](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L740-L802) — 内部依次：

1. L742 `updateApResourcesForVs`：落盘 WAF 资源。
2. L744-750：遍历 `DosProtectedEx` 落盘 DoS 资源。
3. L754-756：`newVirtualServerConfigurator(...)` 建 VS 专属生成器，`GenerateVirtualServerConfig(...)` 产出 `version2.VirtualServerConfig`（详见 u4-l5）。
4. L757 `ExecuteVirtualServerTemplate(&vsCfg)`：渲染 VS 配置文本。
5. L767-776 **OIDC 分支**：如果该 VS 启用了 OIDC 策略，先用 `ExecuteOIDCTemplate` 渲染并 `CreateOIDCConfig` 写一份独立的 OIDC 子配置。注释（L762-766）解释了**写盘顺序**为何重要——在开启 `config-safety` 时，`ConfigRollbackManager.CreateConfig` 会立即跑 `nginx -t`，而 VS 模板里有 `include oidc-conf.d/oidc_<ns>_<vs>.conf;`，所以 OIDC 文件必须**先于** VS 配置落盘。
6. L778 `CreateConfig(name, content)`：写 VS 主配置；若 OIDC 变了则把 `changed` 置真（L782-784）。
7. L785 更新 `cnf.virtualServers`；L787-789 在启用指标时刷新 Prometheus 标签。
8. L791-800：若开启 `DynamicWeightChangesReload` 且有双路 split client，计算 `weightUpdates` 供公共方法做免重载更新。

**TransportServer 私有方法**——走 stream 上下文，写盘方法不同：

[internal/configs/configurator.go:909-950](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L909-L950) — L911 `generateTransportServerConfig`、L920 `ExecuteTransportServerTemplate`、L927 `CreateStreamConfig`（注意是 **Stream** 配置，写到 stream-conf.d）。L934-948 是 TLS Passthrough 的特殊处理：把它登记进 `tlsPassthroughPairs` 并调用 `updateTLSPassthroughHostsConfig()` 刷新「host → unix socket」映射文件。

**批量入口**——公共/私分离带来的收益：

[internal/configs/configurator.go:1003-1077](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1003-L1077) — `AddOrUpdateResources` 用 `updateResource` / `updateVSResource` 两个闭包循环调用**私有**方法，汇总 `configsChanged`，最后只在「有变更或调用方强制 reload」时 reload 一次（L1071-1075）。它的入参 `ExtendedResources` 就是四类资源的打包容器：

[internal/configs/configurator.go:89-95](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L89-L95) — `ExtendedResources` 把 `IngressExes`、`MergeableIngresses`、`VirtualServerExes`、`TransportServerExes` 装在一个结构里，正是 ConfigMap 全量重建与 Secret 批量更新时传入的载荷。

#### 4.2.4 代码实践（本讲主任务）

**目标**：精读 `AddOrUpdateVirtualServer`，列出它内部「依次调用的生成与写文件步骤」。

**步骤**：

1. 打开 [internal/configs/configurator.go:718-738](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L718-L738) 与它委托的 [internal/configs/configurator.go:740-802](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L740-L802)。
2. 画出从「拿到一个 `VirtualServerEx`」到「返回 warnings」的完整调用链，标注每一步属于「生成 / 渲染 / 写盘 / 副作用」哪一类。
3. 特别标记 OIDC 写盘与 VS 主配置写盘的**先后顺序**，并对照注释解释为什么是这个顺序。

**预期结果**（参考答案，行号见上）：

```text
AddOrUpdateVirtualServer (L718)
└─ addOrUpdateVirtualServer (L740)
   ├─ [写盘·前置] updateApResourcesForVs         → WAF 策略/日志文件 (L742)
   ├─ [写盘·前置] updateDosResource × DosProtected → DoS 资源文件 (L744-750)
   ├─ [生成]   newVirtualServerConfigurator + GenerateVirtualServerConfig → VirtualServerConfig (L754-756)
   ├─ [渲染]   ExecuteVirtualServerTemplate        → []byte VS 配置 (L757)
   ├─ [渲染+写盘·可选] OIDC: ExecuteOIDCTemplate + CreateOIDCConfig (L767-776)  ← 先写
   ├─ [写盘]   CreateConfig(name, content)         → conf.d/vs_<ns>_<name>.conf (L778)  ← 后写
   ├─ [内存]   cnf.virtualServers[name] = vsEx     (L785)
   ├─ [可观测] updateVirtualServerMetricsLabels    (L787-789, 条件)
   └─ [生成]   计算 weightUpdates (L791-800, 条件)
└─ [副作用] EnableReloads() (若有 weightUpdates, L725-727)
└─ [副作用] cnf.Reload(ReloadForOtherUpdate) (L729)
└─ [副作用·Plus] UpsertSplitClientsKeyVal × weightUpdates (L733-735, 免重载改权重)
```

**待本地验证**：上述顺序在 `config-safety=false` 时 OIDC 与 VS 写盘顺序是否仍然如此？请阅读 L762-766 注释确认（注释明确：无 config safety 时校验只在 `Reload()` 发生，顺序不影响正确性）。

#### 4.2.5 小练习与答案

**练习 1**：如果某天你要新增一种「每资源都需要的副作用」（比如每次更新都发一条审计日志），应该加在公共方法还是私有方法里？

> **答案**：取决于是否希望批量路径也触发。如果希望**每个资源都触发**，加在私有方法里（批量路径会随循环触发）；如果希望**每个批量只触发一次**，加在公共方法或批量方法里。当前 reload 正是后者：公共方法每资源一次，批量方法 `AddOrUpdateResources` 整批一次。

**练习 2**：`AddOrUpdateIngress` 与 `addOrUpdateIngress` 的返回值类型不同（`(Warnings, error)` vs `(bool, Warnings, error)`），多出来的 `bool` 是什么？

> **答案**：是 `configChanged`，表示这次写盘是否**真的改了内容**（由 `nginxManager.CreateConfig` 返回）。批量方法用它判断「是否需要 reload」——N 个资源全都没变时可以不重载。

### 4.3 v1 / v2 两套模板执行器的编排

#### 4.3.1 概念说明

`Configurator` 持有两个模板执行器：v1 的 `templateExecutor` 和 v2 的 `templateExecutorV2`。这个「version1 / version2」对应的是 NIC 历史上两代配置生成体系：

- **version1**：服务**标准 Ingress**（含 Mergeable Ingress）与**全局 main 配置**（`nginx.conf`）。
- **version2**：服务**自定义资源**——VirtualServer、TransportServer，以及衍生的 OIDC 子配置、TLS Passthrough 映射。

每个执行器内部都把 `.tmpl` 文件解析成 `*template.Template`，并提供「执行」方法：吃一个结构体、吐一段文本。`Configurator` 的工作就是**在合适的时机调用合适的执行器**，并对用户自定义模板做热替换/还原。

#### 4.3.2 核心流程

模板执行器的两件事：

```text
(1) 渲染：结构体 ──Execute*Template──▶ []byte 文本
(2) 模板管理：用户可在 ConfigMap 里提供自定义模板字符串，
    执行器支持 Update*Template(热替换) 与 UseOriginal*Template(还原默认)。
```

v1 与 v2 的方法对照：

| 资源 / 配置 | 执行器 | 渲染方法 | 模板文件 |
| --- | --- | --- | --- |
| main 配置（nginx.conf） | v1 | `ExecuteMainConfigTemplate` | version1/nginx.tmpl |
| Ingress | v1 | `ExecuteIngressConfigTemplate` | version1/nginx.tmpl（同文件不同段） |
| VirtualServer | v2 | `ExecuteVirtualServerTemplate` | version2/nginx.virtualserver.tmpl |
| TransportServer | v2 | `ExecuteTransportServerTemplate` | version2/nginx.transportserver.tmpl |
| OIDC 子配置 | v2 | `ExecuteOIDCTemplate` | version2/oidc.tmpl |
| TLS Passthrough 映射 | v2 | `ExecuteTLSPassthroughHostsTemplate` | 内联字符串模板 |

#### 4.3.3 源码精读

**v1 执行器**：

[internal/configs/version1/template_executor.go:10-15](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/template_executor.go#L10-L15) — 结构体持有「原始模板」与「当前模板」两份指针，正是为了支持「热替换 + 还原」。

[internal/configs/version1/template_executor.go:70-82](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/template_executor.go#L70-L82) — `ExecuteMainConfigTemplate` 与 `ExecuteIngressConfigTemplate` 都是「执行模板写入 `bytes.Buffer` 再返回字节」的薄封装。

**v2 执行器**：

[internal/configs/version2/template_executor.go:17-24](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version2/template_executor.go#L17-L24) — 比 v1 多了 `tlsPassthroughHostsTemplate`（内联字符串，见 L10-14）与 `oidcTemplate`。

[internal/configs/version2/template_executor.go:82-129](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version2/template_executor.go#L82-L129) — `ExecuteVirtualServerTemplate` / `ExecuteTransportServerTemplate` / `ExecuteOIDCTemplate` / `ExecuteTLSPassthroughHostsTemplate` 四个渲染方法，签名统一为「结构体 → `[]byte`」。

**Configurator 如何编排「用户自定义模板」**——这是执行器编排最完整的样本：

[internal/configs/configurator.go:1559-1613](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1559-L1613) — `UpdateConfig`（ConfigMap 驱动的全量重建）在生成前会逐个检查 `CfgParams.MainTemplate` / `IngressTemplate` / `VirtualServerTemplate` / `TransportServerTemplate`：非空就 `Update*Template` 热替换，为空就 `UseOriginal*Template` 还原默认。这样用户在 ConfigMap 里改了自定义模板能立刻生效，删掉又能回退。

#### 4.3.4 代码实践（源码阅读型）

**目标**：把「资源 → 执行器 → 渲染方法 → 模板文件」的对应关系整理成表。

**步骤**：

1. 在 `addOrUpdateIngress`（L524）、`addOrUpdateVirtualServer`（L757）、`addOrUpdateTransportServer`（L920）里找到各自的渲染调用。
2. 对照本节上面的表格，把每一行填全。
3. 验证：`ExecuteMainConfigTemplate` 在 `UpdateConfig` 里被调用（L1615），它的输入 `MainConfig` 来自 `GenerateNginxMainConfig`（L1614）。

**预期结果**：一张四列对照表，能解释「为什么 v1 / v2 是两个执行器」——因为 Ingress 与 VirtualServer 是两套独立演进的数据模型与模板。

#### 4.3.5 小练习与答案

**练习 1**：TLS Passthrough 的「host → unix socket」映射为什么用**内联字符串模板**而不是 `.tmpl` 文件？

> **答案**：因为它结构极简（只是 `host socket;` 的逐行映射，见 version2/template_executor.go:10-14），不值得单独建文件；执行器在 `NewTemplateExecutor` 里直接 `template.New("unixSockets").Parse(...)` 解析它（L40-43）。

**练习 2**：用户在 ConfigMap 里删掉了 `main-template`，配置会怎样？

> **答案**：下次 `UpdateConfig` 时 `CfgParams.MainTemplate` 为 nil，走 `UseOriginalMainTemplate()` 分支（L1579-1582），把 main 模板还原成启动时解析的原始版本，随后重新渲染 main 配置并 reload。

### 4.4 reload 控制：启动期累积、批量合并、Plus 免重载

#### 4.4.1 概念说明

「reload」是 NGINX Ingress Controller 里最昂贵的副作用之一——它要 fork 新 worker、加载新配置、等老 worker 退出（u5 单元会讲细节）。如果每来一个资源变更就 reload 一次，启动期和批量事件下会出现「reload 风暴」。`Configurator` 用一个布尔开关 `isReloadsEnabled` 把所有 reload 副作用统管起来，形成了三种关键模式：

1. **启动期累积**：reload 关闭，所有写盘都「哑火」，最后开闸一次性 reload。
2. **批量合并**：队列堆积时关闭 reload、逐条写盘，排空后开闸合并成一次 reload。
3. **Plus 免重载**：端点 / 权重变化时，用 Plus API 动态更新，根本不 reload。

#### 4.4.2 核心流程

reload 开关的生命周期：

```text
NewConfigurator           isReloadsEnabled = false  (启动即关闭)
   │
   ├─ 控制器启动：缓存同步 → 队列排空
   │     └─ CompleteStartup → EnableReloads() → updateAllConfigs()  ← 一次性 reload
   │
   ├─ 稳态：每个 AddOrUpdateXxx 公共方法各自 reload
   │
   └─ 队列又堆积（批量事件）：
         ├─ DisableReloads()         ← 关闸
         ├─ 逐条 addOrUpdateXxx（只写盘）
         └─ 排空后 EnableReloads() + ReloadForBatchUpdates()  ← 合并一次
```

Plus 免重载是另一条路径：`updateServersInPlus` / `UpsertSplitClientsKeyVal` 直接走 NGINX Plus 的 API / key-val，跳过写 `.conf` + reload，**同样受 `isReloadsEnabled` 门控**——启动期也不会真正下发。

#### 4.4.3 源码精读

**reload 开关与门控方法**：

[internal/configs/configurator.go:1521-1538](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1521-L1538) — `EnableReloads` / `DisableReloads` 翻转 `isReloadsEnabled`；`Reload` 在 L1533 先检查开关，**关闭时直接 `return nil`**——这就是「哑火」的实现。所有 reload 调用最终都汇流到这里。

**Plus 动态更新同样受门控**：

[internal/configs/configurator.go:1540-1554](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1540-L1554) — `updateServersInPlus` 与 `updateStreamServersInPlus` 都在 L1541 / L1549 检查 `isReloadsEnabled`，未开闸时不下发 Plus API。这保证启动期 Plus 端点更新也只是一次性补发，而不是逐条。

**批量合并 reload**：

[internal/configs/configurator.go:1698-1707](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1698-L1707) — `ReloadForBatchUpdates` 只在 `batchReloadsEnabled` 为真时才 reload，供控制器在批量排空后调用。

**ConfigMap 全量重建**——「累积写盘 + 末尾一次 reload」的最大样本：

[internal/configs/configurator.go:1559-1696](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1559-L1696) — `UpdateConfig` 先处理 dhparam、自定义模板、main 配置（L1565-1626），再循环为四类资源调用各自的**私有**方法（L1632-1681，只写盘不 reload），最后在 L1683 才 `Reload` 一次，L1687-1689 再补发 weight 更新。注意 L1563 `isRollbackManager` 分支：当使用 `ConfigRollbackManager`（u5-l3）时，单个资源配置失败不会整体失败，而是记进 `resourceErrors` 继续处理其余资源——这是「坏配置不拖垮全局」的容错点。

**控制器的开闸时机**（承接 u3-l1 / u3-l5）——看控制器怎么按这个开关编排：

启动期：[internal/k8s/controller.go:1276-1277](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1276-L1277) — `CompleteStartup` 算清 host 裁决后，`EnableReloads()` + `updateAllConfigs()` 一次性把累积的全部配置落地并 reload。`updateAllConfigs` 最终调用 [internal/k8s/controller.go:1096](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1096) 的 `configurator.UpdateConfig(resourceExes)`。

批量期：[internal/k8s/controller.go:1305-1314](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1305-L1314) — 队列排空后 `EnableReloads()`，按需 `updateAllConfigs()` 或 `ReloadForBatchUpdates()`。

**Plus 端点免重载路径**：

[internal/configs/configurator.go:1243-1275](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1243-L1275) — `UpdateEndpoints` 在 Plus 模式下，对每个 Ingress 先调私有方法重生成配置，再 `updatePlusEndpoints` 走 API 改端点；只要 API 调用全部成功（L1265 `!reloadPlus`），就 L1266-1268 直接返回**不 reload**。一旦某个 API 调用失败，回退到 reload（L1270）。这正是 u5-l4 要讲的「端点变更免重载」在配置生成侧的入口。

**底层写盘与 reload 的真实实现**（接口契约，进程细节见 u5）：

[internal/nginx/manager.go:193-195](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/nginx/manager.go#L193-L195) 与 [internal/nginx/manager.go:202-212](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/nginx/manager.go#L202-L212) — `CreateConfig` 写 `<conf.d>/<name>.conf`，先用 `configContentsChanged` 比对内容是否变化，再 `createFileAndWrite`，返回 `configChanged`。这就是 Configurator 拿到的那个 bool 的来源。

[internal/nginx/manager.go:361-386](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/nginx/manager.go#L361-L386) — `Reload` 递增 `configVersion`、写版本文件、执行 `nginx -s reload`，然后 `WaitForCorrectVersion` 轮询确认新 worker 真正生效（u5-l2 详解）。注意它**不看** Configurator 的开关——开关在 Configurator 层，manager 层只负责「让我 reload 我就 reload」。

#### 4.4.4 代码实践（源码阅读型）

**目标**：追踪「队列堆积 → 关闸写盘 → 排空开闸」的完整时序，把 Configurator 的开关与控制器的调度串起来。

**步骤**：

1. 在控制器里定位关闸点 [internal/k8s/controller.go:1184](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1184)（`DisableReloads`）与开闸点 L1307。
2. 追踪：关闸期间 `processChanges` 调用的 `AddOrUpdateVirtualServer` 内部 `Reload` 会怎样？（答：在 [configurator.go:1532-1538](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1532-L1538) 因 `isReloadsEnabled==false` 直接返回 nil。）
3. 排空后 L1307 `EnableReloads()`，再到 L1311 `ReloadForBatchUpdates` 真正 reload 一次。

**预期结果**：你能用一句话解释「为什么 NIC 在大量资源同时变更时不会 reload 风暴」——因为关闸期间 N 次 `AddOrUpdateXxx` 只写了 N 个文件、reload 全部哑火，排空后只 reload 一次。

**待本地验证**：`ReloadForBatchUpdates` 的参数 `batchReloadsEnabled`（L1700）由哪个 flag 控制？请在控制器中搜索 `enableBatchReload` 的来源确认（提示：与 `-enable-batch-reloads` 类启动 flag 相关）。

#### 4.4.5 小练习与答案

**练习 1**：`Reload()` 在 `isReloadsEnabled == false` 时返回 `nil`（无错误）。这种「静默成功」会不会掩盖问题？

> **答案**：不会掩盖配置错误，但会**延后**生效。关闸期间的配置写盘若内容非法，错误要到开闸后那次 reload（经 `nginx -t` / `WaitForCorrectVersion`）才暴露；若用 `ConfigRollbackManager`，则会在写盘时 `nginx -t` 当场发现并回滚（u5-l3）。设计上这是有意权衡：用「延后一次性校验」换取启动期不 reload。

**练习 2**：`UpdateEndpoints` 在 OSS（非 Plus）模式下还会「免重载」吗？

> **答案**：不会。L1256 `if cnf.isPlus` 才走 Plus API；OSS 模式下 `reloadPlus` 保持 false 但 `cnf.isPlus` 为假，L1265 的 `cnf.isPlus && !reloadPlus` 不成立，直接走到 L1270 `cnf.Reload(ReloadForEndpointsUpdate)`。所以 OSS 的端点变更**必须 reload**，这也是 Plus 在大规模端点漂移场景下的核心性能优势。

## 5. 综合实践

把本讲四块内容串起来，完成下面这个「**端到端调用链**」追踪任务：

> 场景：集群里一个 VirtualServer 引用了一个 ExternalName 类型的 Service，且控制器以 Plus 模式运行。现假设该 Service 的后端 EndpointSlice 发生了一次端点 IP 变更，被 EndpointSlice handler 入队，最终走到 `syncEndpoints`。

请完成：

1. **定位入口**：在控制器里找到端点变更最终调用 Configurator 的哪个方法（提示：`UpdateEndpointsForVirtualServers`，[configurator.go:1314-1345](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1314-L1345)）。
2. **画出生成链**：它如何复用 `addOrUpdateVirtualServer` 的「生成 → 渲染 → 写盘」三段式？
3. **判断是否 reload**：因为 ExternalName Service 在 [configurator.go:1484-1487](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1484-L1487)（Ingress 路径）与 VS 路径里会被跳过 Plus API 更新，结合 4.4 的结论，说明这次端点变更**会不会**触发 NGINX reload，为什么。
4. **写一句话总结**：用「裁决 → 生成 → 渲染 → 写盘 → reload/Plus API」五个动词，描述 Configurator 在整个数据流里负责的后三步。

**参考产出**：一份调用链图 + 一段「是否 reload 及理由」的判断说明。这个练习把「资源入口方法」「模板执行器编排」「reload 控制」三个最小模块拧到了同一条真实链路上。

## 6. 本讲小结

- `Configurator` 是五层架构中「**配置生成**」层的核心类型，职责是把 `Configuration` 裁决过的扩展资源翻译成 NGINX 配置文件并触发副作用；它**只执行不裁决**。
- 所有资源入口方法遵循**公共 / 私分离**的双层模式：公共 `AddOrUpdateXxx` = 私有 `addOrUpdateXxx`（生成 → 渲染 → 写盘）+ `Reload`；私有方法不 reload，从而让批量路径能把 N 次 reload 压成 1 次。
- 每个私有方法都是同构的**「生成 → 渲染 → 写盘」**三段式：生成器函数产出模板输入结构体 → 模板执行器渲染成文本 → `nginxManager.CreateConfig` 写盘并返回 `configChanged`。
- `Configurator` 编排 **v1**（Ingress + main）与 **v2**（VirtualServer / TransportServer / OIDC / TLS Passthrough）两套模板执行器，并支持用户自定义模板的热替换与还原。
- **reload 控制**是性能关键：`isReloadsEnabled` 开关让启动期「累积写盘、最后一次性 reload」、批量期「关闸写盘、排空合并 reload」、Plus 期「端点 / 权重变更走 API 免重载」——三种模式共用同一道闸门。
- 生成链路的安全基石之一是 `escapeNginxString`（common.go），用户字符串进入 NGINX 配置前都要转义；完整的危险字符防护见 u6-l1。

## 7. 下一步学习建议

本讲只讲了 Configurator「**怎么编排**」，但生成器内部「**怎么把字段翻译成 upstream / location**」尚未展开。建议按以下顺序继续：

- **u4-l2（ConfigMap 解析）**：搞清 `CfgParams` / `StaticConfigParams` 从何而来——Configurator 持有的全局参数是如何从 ConfigMap 解析进来的。
- **u4-l3（annotations 解析）**：Ingress 注解如何映射成 NGINX 指令，进入 `IngressNginxConfig`。
- **u4-l4 / u4-l5 / u4-l6**：分别精读 `generateNginxCfg`、`GenerateVirtualServerConfig`、`generateTransportServerConfig`——本讲里这些「生成器函数」是黑盒，这三讲把它们逐层打开。
- **u4-l7（Policy 配置生成）**：策略如何被翻译成 `limit_req` / `auth_jwt` 等指令。
- **u4-l8（模板渲染体系）**：深入 `.tmpl` 文件本身与快照黄金测试，理解「渲染」这一步的内部。
- **u5 单元（NGINX 进程管理）**：本讲里 `nginx.Manager` 始终是个「黑盒接口」，u5 会打开 `CreateConfig` / `Reload` / `WaitForCorrectVersion` / `ConfigRollbackManager` 的进程级实现。
