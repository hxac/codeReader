# 全局配置：ConfigMap 解析

## 1. 本讲目标

本讲承接 [u4-l1 Configurator：配置生成的中枢](u4-l1-configurator-overview.md)。上一讲我们知道了 `Configurator` 负责把内存模型里的资源翻译成 NGINX 配置；但那只是「资源级」配置（每个 Ingress / VirtualServer 生成各自的 server / upstream）。本讲要回答另一个问题：

> NGINX 自身的「全局配置」（如 `worker_processes`、日志格式、SSL 协议、负载均衡默认算法）从哪里来？谁把它翻译成 `nginx.conf`？

答案就是 **ConfigMap 解析子系统**。学完本讲你应该能够：

1. 说清一份 K8s ConfigMap 的 `data` 字典是如何被解析成 `ConfigParams` 结构体的。
2. 区分 `ConfigParams`、`StaticConfigParams`、`MGMTConfigParams` 三种配置数据结构的来源与职责。
3. 描述从 `ConfigMap.Data` 到最终 `nginx.conf` 的完整变换链：`ConfigMap → ConfigParams → MainConfig → nginx.tmpl → nginx.conf`。
4. 理解 zone-sync、OIDC、OpenTelemetry 这类「子块」为什么需要单独的解析函数。
5. 能够跟踪一个具体参数（如 `worker-processes`）从 ConfigMap 一路走到 `nginx.conf` 的每一跳。

---

## 2. 前置知识

阅读本讲前，请确认你已了解以下概念（这些在 u1、u3、u4-l1 中已建立）：

- **ConfigMap**：Kubernetes 内置资源，用一个 `data: map[string]string` 存放键值对配置。NIC 把它当作「全局参数表」来用。
- **`-nginx-configmaps` flag**：NIC 启动时通过这个 flag 指定「哪个 ConfigMap 是 NGINX 全局配置来源」（见 u1-l4）。它指向的 ConfigMap 由本讲的 `ParseConfigMap` 解析。
- **Configurator**：配置生成层的中枢（u4-l1）。本讲解析出的 `ConfigParams` 最终会挂到 `Configurator.CfgParams` 上，供它生成 `nginx.conf`。
- **`nginx.conf` 与 `MainConfig`**：NGINX 的主配置文件，由 `version1/nginx.tmpl` 渲染，其输入结构体是 `version1.MainConfig`。
- **NGINX 指令上下文**：`nginx.conf` 是分层的——最外层是 `main` 上下文，里面是 `events {}`、`http {}`、`stream {}`。本讲的「全局参数」绝大多数落在 `main` 或 `http` 上下文。

一句话直觉：**ConfigMap 是用户填的一张「选项表」，`ParseConfigMap` 是把这张表翻译成 Go 结构体的「查表员」，`GenerateNginxMainConfig` 再把这个结构体改造成模板能直接吃的 `MainConfig`。**

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `internal/configs/configmaps.go` | **核心**。`ParseConfigMap`（解析全局 ConfigMap）、`ParseMGMTConfigMap`（解析 mgmt ConfigMap）、`GenerateNginxMainConfig`（生成 MainConfig），以及 zone-sync / OIDC / OTel 子块解析函数。 |
| `internal/configs/config_params.go` | 三大数据结构定义：`ConfigParams`、`StaticConfigParams`、`MGMTConfigParams`，以及各自的默认值构造函数。 |
| `internal/configs/parsing_helpers.go` | 解析辅助函数：`GetMapKeyAsBool` / `GetMapKeyAsInt` / `GetMapKeyAsStringSlice`、`ParseSize`、`ParseTime` 等。 |
| `internal/k8s/configmap.go` | ConfigMap 的事件 handler 与 `syncConfigMap`，把解析结果交给 `updateAllConfigs`。 |
| `internal/k8s/controller.go` | `updateAllConfigs`：调用 `ParseConfigMap`，把结果挂到 Configurator。 |
| `internal/configs/configurator.go` | `UpdateConfig`：调用 `GenerateNginxMainConfig` + 模板渲染 + 写盘。 |
| `internal/configs/version1/config.go` | `MainConfig` 结构体定义（模板输入）。 |
| `internal/configs/version1/nginx.tmpl` | `main nginx.conf` 模板，本讲参数最终在这里被渲染成指令。 |

---

## 4. 核心概念与源码讲解

### 4.1 三层配置数据结构：ConfigParams / StaticConfigParams / MGMTConfigParams

#### 4.1.1 概念说明

在讲「怎么解析」之前，先要搞清楚「解析结果装在哪里」。NIC 的全局配置不是单一来源，而是来自**三个不同生命周期**的输入：

1. **命令行 flags**（启动时固定，运行期不可变）：比如是否开启 `nginx-status`、是否启用 TLS passthrough、是否启用 snippets。这些装在 `StaticConfigParams`。
2. **NGINX 全局 ConfigMap**（由 `-nginx-configmaps` 指定，运行期可改、会触发 reload）：比如 `worker-processes`、`error-log-level`、SSL 协议。这些装在 `ConfigParams`。
3. **mgmt ConfigMap**（仅 NGINX Plus，由 `-mgmt-configmap` 指定）：NGINX Plus 管理面的 license / 上报端点 / 代理。这些装在 `MGMTConfigParams`。

为什么这样拆？因为**「能不能在运行期热改」是一个本质属性**，必须用不同类型在编译期就区分开。flags 一旦进程启动就锁死（改了要重启 pod）；ConfigMap 可以随时 `kubectl apply` 改（改了触发一次 `updateAllConfigs` + reload）。把它们混在同一个结构体里，就很难保证「不该被 reload 影响的字段真的没被影响」。

还有一个容易混淆的点：`ConfigParams` 本身是一个**「大杂烩」**。它的注释写得很清楚：

> ConfigParams holds NGINX configuration parameters that affect the main NGINX config **as well as** configs for Ingress resources.

也就是说，它既装「全局 main 配置」（这些字段用 `Main` 前缀，如 `MainWorkerProcesses`），又装「资源级默认值」（这些字段没有 `Main` 前缀，如 `ProxyConnectTimeout`、`LBMethod`、`MaxFails`，它们会作为每个 Ingress/VirtualServer 生成配置时的兜底默认值）。`Main` 前缀就是用来标记「这个值属于 main nginx.conf」的约定。

#### 4.1.2 核心流程

三种结构体的分工可以画成：

```
                 ┌─────────────────────────┐
   CLI flags ──▶ │  StaticConfigParams     │  启动期固定
                 │  (nginx-status, tls-    │
                 │   passthrough, ...)     │
                 └────────────┬────────────┘
                              │
-nginx-configmaps ──▶ ┌───────┴─────────────┐
                       │   ConfigParams      │  运行期可改（本讲主角）
                       │  (Main* 全局字段    │
                       │   + 资源默认值)     │
                       └───────┬─────────────┘
                               │   ┌────────────────────────┐
-mgmt-configmap (Plus) ────────┼──▶│  MGMTConfigParams       │  仅 Plus
                               │   │  (license, endpoint...) │
                               │   └────────────┬───────────┘
                               │                │
                               ▼                ▼
                     GenerateNginxMainConfig(static, config, mgmt)
                               │
                               ▼
                          version1.MainConfig  ──▶  nginx.tmpl ──▶ nginx.conf
```

关键结论：**三者最终在 `GenerateNginxMainConfig` 里被「合并」成一个 `MainConfig`**，再交给模板渲染。`MainConfig` 是模板的「统一输入接口」，它屏蔽了「这个字段来自 flag 还是 ConfigMap」的差异。

#### 4.1.3 源码精读

**`ConfigParams` 结构体**（节选，关键是 `Main` 前缀字段与资源默认值字段并存）：

[internal/configs/config_params.go:12-16](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L12-L16) 定义了 `ConfigParams`。注意它的注释明确说了它同时服务 main config 与资源 config。其中 [internal/configs/config_params.go:54-59](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L54-L59) 是一组典型的 `Main` 前缀字段（`MainWorkerConnections`、`MainWorkerProcesses` 等），它们最终进入 `nginx.conf` 的 main 上下文；而像 `LBMethod`、`MaxFails`、`ProxyConnectTimeout` 这些无前缀字段（[internal/configs/config_params.go:30](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L30)、[L60-61](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L60-L61)、[L81](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L81)）是资源级默认值。

**默认值构造函数**——这点非常重要：`ConfigParams` 不是「零值起步」，而是先用 `NewDefaultConfigParams` 铺好一套安全默认值，再用 ConfigMap 的值覆盖。 [internal/configs/config_params.go:252-274](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L252-L274) 展示了这一点：比如 `MainWorkerProcesses` 默认是 `"auto"`、`ProxyConnectTimeout` 默认 `"60s"`、`ServerTokens` 默认 `"on"`。还要注意默认值会随 `isPlus` 变化（如 [L253-256](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L253-L256) 中 OSS 与 Plus 的 upstream zone size 不同）。

**`StaticConfigParams`**（来自 flags）：[internal/configs/config_params.go:154-185](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L154-L185)。注意这里全是「开关型」字段：`NginxStatus`、`TLSPassthrough`、`EnableSnippets`、`EnableOIDC`、`DynamicSSLReload` 等——它们决定 NGINX 的「能力开关」或「行为模式」，不适合运行期频繁切换。

**`MGMTConfigParams`**（仅 Plus）：[internal/configs/config_params.go:236-249](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L236-L249)。注意它内部嵌套了一个 `MGMTSecrets`（[L228-233](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L228-L233)），存放 license / 证书的 **Secret 名字**（不是内容本身）。

#### 4.1.4 代码实践

**实践目标**：体会「flag 来源」与「ConfigMap 来源」在代码里的物理隔离。

**操作步骤**：

1. 打开 `internal/configs/config_params.go`，分别在 `StaticConfigParams`（L154）和 `ConfigParams`（L12）两个结构体里各找一个字段。
2. 用 `grep -rn "EnableSnippets"` 在仓库内搜索，确认 `StaticConfigParams.EnableSnippets` 是从哪个 flag 赋值的（应追溯到 `cmd/nginx-ingress/flags.go`）。
3. 用 `grep -rn "MainWorkerProcesses"` 搜索，确认它只来自 ConfigMap 解析路径，不来自任何 flag。

**需要观察的现象**：`StaticConfigParams` 字段的赋值点全部集中在 `cmd/` 启动代码；而 `ConfigParams` 的 `Main*` 字段赋值点集中在 `internal/configs/configmaps.go` 的 `ParseConfigMap` 里。

**预期结果**：你会清楚地看到两类字段在「赋值来源」上互不交叉——这正是「运行期可变 vs 启动期固定」在代码层的体现。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `worker-processes` 放在 `ConfigParams`，而 `nginx-status` 放在 `StaticConfigParams`？

> **参考答案**：`worker-processes` 来自 ConfigMap，用户可以随时 `kubectl apply` 修改并触发 reload，所以必须在运行期可变结构体 `ConfigParams` 里；`nginx-status` 来自命令行 flag，改它需要重启 pod，属于启动期固定配置，故放在 `StaticConfigParams`。

**练习 2**：`ConfigParams` 里所有以 `Main` 开头的字段（如 `MainWorkerProcesses`、`MainAccessLog`）有什么共同归宿？

> **参考答案**：它们都会被 `GenerateNginxMainConfig` 装进 `version1.MainConfig`，最终由 `nginx.tmpl` 渲染进 `nginx.conf` 的 main / http 上下文。没有 `Main` 前缀的字段（如 `ProxyConnectTimeout`、`LBMethod`）则作为 Ingress / VirtualServer 资源级默认值使用，不一定进 main 配置。

---

### 4.2 ParseConfigMap：把 ConfigMap 解析成 ConfigParams

#### 4.2.1 概念说明

`ParseConfigMap` 是本讲的主角函数。它接收一个 `*v1.ConfigMap`，返回一个填充好的 `*ConfigParams` 和一个 `bool`（表示配置是否完全合法）。

它的核心设计思想可以概括为三条：

1. **默认值兜底 + 覆盖式解析**：先 `NewDefaultConfigParams` 铺好默认值，然后对 ConfigMap 里**每一个存在的 key** 用 `if v, exists := cfgm.Data[key]; exists` 模式逐个尝试覆盖。没出现的 key 就保留默认值。这也是为什么 `deployments/common/nginx-config.yaml` 的 `data:` 段可以是空的——没有 key 就全部走默认值。

2. **best-effort（尽力而为）策略**：某个 key 的值非法时，NIC 不会让整个控制器崩溃，而是**记录一条 Warning Event、把 `configOk` 置 false、跳过这一个 key（保留默认值）**，继续解析其余 key。返回的 `cfgParams` 仍然可用（只是那个非法 key 用了默认值）。这保证了「一个手滑写错的 key 不会拖垮整个数据面」。

3. **类型安全的取值辅助函数**：ConfigMap 的 `data` 全是 `string`，但 NGINX 指令需要 bool / int / size / time 等不同类型。NIC 在 `parsing_helpers.go` 里提供了一组 `GetMapKeyAsXxx` 函数，统一封装「取值 + 类型转换 + 错误包装」，避免到处写重复的 `strconv` 代码。

#### 4.2.2 核心流程

`ParseConfigMap` 的主体是一个**长长的 if-存在-则解析 序列**，外加几个子块函数调用。伪代码如下：

```
func ParseConfigMap(ctx, cfgm, nginxPlus, hasAppProtect, hasAppProtectDos, hasTLSPassthrough, enableDirectiveAutoadjust, eventLog):
    cfgParams = NewDefaultConfigParams(ctx, nginxPlus)   # 1. 铺默认值
    configOk = true

    # 2. 逐 key 覆盖（每个 key 一种解析模式）
    if exists(cfgm, "server-tokens"):     # bool/字符串混合校验
    if exists(cfgm, "lb-method"):         # OSS/Plus 不同校验
    if exists(cfgm, "proxy-connect-timeout"):  # 直接字符串
    ...
    if exists(cfgm, "worker-processes"):  # int 或 "auto"
    ...
    # 3. 子块解析（逻辑较重，抽成独立函数）
    parseConfigMapZoneSync(...)            # Plus 专属
    parseConfigMapOIDC(...)                # OIDC token 超时/zone
    parseConfigMapOpenTelemetry(...)       # OTel 导出器

    # 4. 条件块（受 feature 开关控制）
    if hasAppProtect:    # 仅编译了 NAP WAF
        解析 app-protect-* 系列
    if hasAppProtectDos: # 仅编译了 NAP DoS
        解析 app-protect-dos-* 系列

    return cfgParams, configOk
```

注意 `ParseConfigMap` 接收一堆 `bool` 参数（`nginxPlus`、`hasAppProtect`、`hasAppProtectDos`、`hasTLSPassthrough`、`enableDirectiveAutoadjust`），它们是**运行期能力开关**，决定哪些 key 合法。比如 `resolver-addresses` 在 OSS 下会被判为「需要 NGINX Plus」而拒绝（[configmaps.go:547-556](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L547-L556)）。

#### 4.2.3 源码精读

**函数签名与默认值铺垫**：[internal/configs/configmaps.go:34-37](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L34-L37)。注意 `configOk := true` 是贯穿全函数的「合法性累计器」。

**经典解析模式 1：直接字符串赋值**（最简单）。以 `proxy-connect-timeout` 为例，[internal/configs/configmaps.go:82-84](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L82-L84) 直接把字符串塞进字段——因为 NGINX 时间字符串本身就是文本，无需类型转换。

**经典解析模式 2：bool 类型 + best-effort**。以 `http2` 为例，[internal/configs/configmaps.go:147-155](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L147-L155)：用 `GetMapKeyAsBool` 转换；失败时记 Event + 置 `configOk = false`，但**不 return**，继续解析后续 key。这就是 best-effort。

**经典解析模式 3：带语义校验的字符串**。以 `worker-processes` 为例（本讲实践任务的主角），[internal/configs/configmaps.go:455-463](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L455-L463)。它有一个特殊处理：`GetMapKeyAsInt` 会把字符串 `"auto"` 当成非法 int 解析失败，所以代码特意加了 `cfgm.Data["worker-processes"] != "auto"` 的判断——只要值不是 `"auto"` 才把解析错误当真。最终合法值（数字或 `"auto"`）被原样赋给 `cfgParams.MainWorkerProcesses`。

**取值辅助函数**：在 [internal/configs/parsing_helpers.go:22-33](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L22-L33)（`GetMapKeyAsBool`）、[L36-47](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L36-L47)（`GetMapKeyAsInt`）、[L82-88](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L82-L88)（`GetMapKeyAsStringSlice`）。它们都遵循 `(value, exists, error)` 三返回值约定：`exists` 区分「key 不存在」与「key 存在但值非法」，error 里统一带上资源 Kind/Namespace/Name 方便定位。`ParseSize`（[parsing_helpers.go:250-257](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L250-L257)）和 `ParseTime`（[L210-227](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/parsing_helpers.go#L210-L227)）则用正则校验 NGINX 的 size / time 语法。

**子块与条件块的调用点**：[internal/configs/configmaps.go:501-509](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L501-L509) 调用 zone-sync 与 OIDC 子块；[L631-634](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L631-L634) 调用 OTel 子块；[L636](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L636) 和 [L723](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L723) 分别用 `hasAppProtect` / `hasAppProtectDos` 门控两段条件块。

**返回**：[internal/configs/configmaps.go:743](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L743) `return cfgParams, configOk`。

#### 4.2.4 代码实践

**实践目标**：理解「非法 key 不致命」的 best-effort 行为。

**操作步骤**：

1. 打开 `internal/configs/configmaps_test.go`，搜索 `ParseConfigMap`，找到针对某个 key 写「故意非法值」的测试用例（例如把 `http-redirect-code` 设成 `999`，或把 `client-body-buffer-size` 设成 `abc`）。
2. 阅读该用例的断言：它断言 `configOk == false`，**同时**断言 `cfgParams` 里**其他**字段的值仍然被正确解析。

**需要观察的现象**：即便有一个 key 非法，函数也没有 panic 或返回 nil，而是返回了一个「该非法 key 保持默认值、其余 key 正常」的 `cfgParams`，外加 `configOk = false`。

**预期结果**：你会看到测试断言同时包含 `configOk` 为 false 和某个不相关字段值正确——这正是 best-effort 的可验证证据。（具体用例编号与行号：可参考 [internal/configs/configmaps_test.go:473](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps_test.go#L473) 附近的 table-driven 用例。）

#### 4.2.5 小练习与答案

**练习 1**：`ParseConfigMap` 返回的第二个 `bool`（`configOk`）是什么含义？当它为 `false` 时，返回的 `*ConfigParams` 还能用吗？

> **参考答案**：`configOk` 表示「是否存在被校验拒绝的 key」。为 `false` 时表示至少有一个 key 非法被忽略。返回的 `*ConfigParams` **仍然可用**——非法 key 保留了默认值，合法 key 已正常填充（best-effort）。

**练习 2**：`worker-processes: "auto"` 这个值为什么需要特殊处理？如果不特殊处理会怎样？

> **参考答案**：`GetMapKeyAsInt` 用 `strconv.Atoi` 解析，`"auto"` 不是数字会返回 error。不特殊处理的话，`"auto"` 这个合法的 NGINX 值会被当成非法而丢弃。所以代码加了「只要值等于 `"auto"` 就放行」的特判（[configmaps.go:456](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L456)）。

**练习 3**：`GetMapKeyAsBool` 为什么返回 `(bool, bool, error)` 三个值而不是 `(bool, error)`？

> **参考答案**：需要区分三种状态：「key 不存在」「key 存在且解析成功」「key 存在但解析失败」。第二个 bool（`exists`）区分前两种，error 区分后两种。如果只返回两个值，就无法区分「用户没设这个 key」和「用户设了但写错了」。

---

### 4.3 GenerateNginxMainConfig：从 ConfigParams 到 nginx.conf

#### 4.3.1 概念说明

解析出的 `ConfigParams` 还不能直接喂给模板——模板的输入是 `version1.MainConfig`，它和 `ConfigParams` 字段名、字段集合都不一样（`MainConfig` 是纯模板视图，字段名对应 NGINX 指令语义；`ConfigParams` 是解析视图，带 `Main` 前缀）。中间需要一个**适配函数** `GenerateNginxMainConfig` 把三者（`StaticConfigParams` + `ConfigParams` + `MGMTConfigParams`）合并、改造成 `MainConfig`。

这一步还做了一件 ConfigMap 解析做不了的事：**派生计算**。有些 `nginx.conf` 指令的值不是用户直接给的，而是根据环境算出来的——比如 zone-sync 的集群域名是「headless service 名 + POD_NAMESPACE」拼出来的。这种「派生字段」在 `GenerateNginxMainConfig` 里完成。

#### 4.3.2 核心流程

完整链路（本讲最核心的一张图）：

```
ConfigMap.Data["worker-processes"] = "16"
        │  (1) ParseConfigMap 逐 key 解析
        ▼
ConfigParams.MainWorkerProcesses = "16"
        │  (2) controller.updateAllConfigs 挂载
        ▼
Configurator.CfgParams.MainWorkerProcesses = "16"
        │  (3) UpdateConfig 调用 GenerateNginxMainConfig 合并三源
        ▼
MainConfig.WorkerProcesses = "16"
        │  (4) templateExecutor.ExecuteMainConfigTemplate 渲染
        ▼
nginx.tmpl:  worker_processes  {{.WorkerProcesses}};   →   "worker_processes  16;"
        │  (5) nginxManager.CreateMainConfig 写盘 + reload
        ▼
/etc/nginx/nginx.conf  里出现  worker_processes  16;
```

这 5 跳就是「一个 ConfigMap key 如何变成 nginx.conf 里一行指令」的全部路径。本讲实践任务（4.3.4）就是要你亲手把这条链走一遍。

#### 4.3.3 源码精读

**入口：`updateAllConfigs` 调用 `ParseConfigMap` 并挂载结果**。[internal/k8s/controller.go:1038-1058](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1038-L1058)：先 `NewDefaultConfigParams`（L1040），ConfigMap 存在时调 `ParseConfigMap`（L1048）覆盖，再把结果赋给 `lbc.configurator.CfgParams`（L1057）。这里也调了 `ParseMGMTConfigMap`（L1051，仅 Plus）。

**触发：`syncConfigMap` → `updateAllConfigs`**。[internal/k8s/configmap.go:82-134](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L82-L134) 是 ConfigMap 的 sync 函数。注意它的几个细节：L100-103 特判了 `external-status-address`——这是一个**控制器自身行为参数**（外部 LB 地址回写），不是 NGINX 指令，所以不进 `ParseConfigMap`，而是直接交给 `statusUpdater`。L120-128 是启动期/批量期的跳过优化（与 u3 讲的 `isNginxReady` / `batchSyncEnabled` 衔接）。最终在 L133 调 `lbc.updateAllConfigs()` 全量重生所有配置。

**合并：`GenerateNginxMainConfig`**。[internal/configs/configmaps.go:1174-1202](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1174-L1202)。签名 `GenerateNginxMainConfig(staticCfgParams, config, mgmtCfgParams)`——三源合并的入口。注意 L1194-1201 的 `zoneSyncConfig` 派生计算：`Domain` 字段由 `config.ZoneSync.Domain`、环境变量 `POD_NAMESPACE` 拼成 headless service 域名（`%s-hl.%s.svc.cluster.local`）。这就是「派生字段」的典型例子。

**字段搬运：把 ConfigParams.MainWorkerProcesses 搬到 MainConfig.WorkerProcesses**。[internal/configs/configmaps.go:1203-1287](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1203-L1287) 是巨大的结构体字面量，逐字段搬运。其中 [L1248](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1248) `WorkerProcesses: config.MainWorkerProcesses` 就是去掉 `Main` 前缀的关键一步。你可以对比看到：来自 `staticCfgParams` 的字段（如 `NginxStatus` L1217、`TLSPassthrough` L1241）和来自 `config` 的字段（如 `WorkerProcesses`）在这里被一视同仁地塞进同一个 `MainConfig`。

**模板渲染与写盘**。[internal/configs/configurator.go:1614-1619](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1614-L1619)：在 `UpdateConfig`（[L1559](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1559)）里，L1614 生成 `mainCfg`，L1615 用模板执行器渲染成文本 `mainCfgContent`，L1619 调 `nginxManager.CreateMainConfig` 写盘。

**模板侧**：`MainConfig` 结构体定义在 [internal/configs/version1/config.go:289](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/config.go#L289)（`type MainConfig struct`），其中 `WorkerProcesses string` 在 [L337](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/config.go#L337)。模板 [internal/configs/version1/nginx.tmpl:2](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.tmpl#L2) 第一行就是 `worker_processes  {{.WorkerProcesses}};`。Plus 变体 [internal/configs/version1/nginx-plus.tmpl:2](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx-plus.tmpl#L2) 完全一致（OSS 与 Plus 的 main 模板头部相同，CLAUDE.md 提醒过两者要同步）。

**快照证据**：[internal/configs/version1/__snapshots__/template_test.snap:3](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/__snapshots__/template_test.snap#L3) 的黄金快照里就有 `worker_processes  auto;`——这是默认值渲染出来的真实输出，可作旁证。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：完整追踪 `worker-processes` 从 ConfigMap 到 `nginx.conf` 的 5 跳路径。

**操作步骤**（源码阅读型实践，全程只读不改源码）：

1. **第 1 跳（ConfigMap key）**：假设你在 `-nginx-configmaps` 指向的 ConfigMap 里加了 `worker-processes: "16"`。打开 [configmaps.go:455-463](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L455-L463)，确认 key `"worker-processes"` 被读取，合法值赋给 `cfgParams.MainWorkerProcesses`。
2. **第 2 跳（挂载到 Configurator）**：跳到 [controller.go:1048](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1048) 看 `ParseConfigMap` 调用，再到 [controller.go:1057](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1057) 看结果赋给 `lbc.configurator.CfgParams`。
3. **第 3 跳（合并三源）**：进 [configurator.go:1614](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1614) `GenerateNginxMainConfig`，在 [configmaps.go:1248](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1248) 确认 `config.MainWorkerProcesses` 被搬进 `MainConfig.WorkerProcesses`（去掉了 `Main` 前缀）。
4. **第 4 跳（模板渲染）**：在 [configurator.go:1615](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1615) 看模板执行，再到 [nginx.tmpl:2](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/nginx.tmpl#L2) 看 `{{.WorkerProcesses}}` 占位符，脑内替换成 `16` → 得到 `worker_processes  16;`。
5. **第 5 跳（写盘）**：在 [configurator.go:1619](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configurator.go#L1619) 看 `CreateMainConfig` 把渲染文本写入 `nginx.conf`，随后 reload（详见 u5-l1）。

**需要观察的现象**：每跳都能在源码里找到一个明确的「赋值 / 搬运」动作，且字段名从 `MainWorkerProcesses` → `WorkerProcesses` → `{{.WorkerProcesses}}` 一路对应。

**预期结果 / 验证方式**：

- 打开黄金快照 [template_test.snap:3](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/version1/__snapshots__/template_test.snap#L3)，能看到默认情况下渲染出 `worker_processes  auto;`（对应 `NewDefaultConfigParams` 里 `MainWorkerProcesses: "auto"`）。这证明第 4 跳的模板替换确实生效。
- 若本地有 Go 环境，可运行 `make build` 后用 `--dry-run` 思路无法直接看 main 配置；更可靠的是阅读 `internal/configs/configurator_test.go` 与 `version1/template_test.go` 中的渲染断言。**实际跑通端到端（在真实集群 apply ConfigMap 并 dump pod 内 `/etc/nginx/nginx.conf`）属于待本地验证**——本实践定位为源码追踪，目标是让你能凭源码画出 5 跳链路图。

> 提示：如果你想真的改一个全局参数观察行为，可以在 `deployments/common/nginx-config.yaml` 的 `data:` 段加一行 `worker-processes: "8"`，apply 后进入 NIC pod 执行 `cat /etc/nginx/nginx.conf | head`，应看到 `worker_processes  8;`。这一步依赖一个可用的集群，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`GenerateNginxMainConfig` 的三个入参分别来自哪里？

> **参考答案**：`staticCfgParams` 来自启动期命令行 flags（`StaticConfigParams`）；`config`（`*ConfigParams`）来自 `-nginx-configmaps` ConfigMap 的解析；`mgmtCfgParams` 来自 `-mgmt-configmap` ConfigMap 的解析（仅 NGINX Plus，可为 nil）。

**练习 2**：为什么需要一个 `GenerateNginxMainConfig` 适配函数，而不是让模板直接吃 `ConfigParams`？

> **参考答案**：`ConfigParams` 是「解析视图」（带 `Main` 前缀、混杂资源默认值），`MainConfig` 是「模板视图」（字段名贴合 NGINX 指令、只含 main 配置相关字段）。适配层还负责合并三个来源、计算派生字段（如 zone-sync 域名），让模板保持简单。这是「解析职责」与「渲染职责」的分离。

**练习 3**：`syncConfigMap` 里的 `external-status-address` 为什么不走 `ParseConfigMap`？

> **参考答案**：因为它不是一条 NGINX 指令，而是「控制器把哪个外部地址回写到 Ingress status」的控制器自身行为参数。它不该进 `nginx.conf`，所以直接交给 `statusUpdater`（[configmap.go:100-103](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/configmap.go#L100-L103)），与 NGINX 配置生成路径解耦。

---

### 4.4 子块解析：zone-sync / OIDC / OpenTelemetry / MGMT

#### 4.4.1 概念说明

回看 `ParseConfigMap`，大部分 key 是「单行 if-exists」就能搞定。但有几组 key **逻辑较重**或**字段间有依赖**，如果都堆在主函数里会非常臃肿（主函数已被标了 `//nolint:gocyclo`）。于是它们被抽成独立的「子块解析函数」：

- **zone-sync 子块**：NGINX Plus 的多副本状态同步。一组 key（`zone-sync`、`zone-sync-port`、`zone-sync-resolver-*`）之间有强依赖（必须先启用 `zone-sync` 才能用其他 key），且仅 Plus 可用。
- **OIDC 子块**：OpenID Connect 的 token zone 超时/大小。10 个结构高度同构的 key（都是「ParseTime 或 ParseSize + 赋值」），用 `parseStringField` 帮助函数消除样板。
- **OpenTelemetry 子块**：OTel 导出器配置，字段间有跨字段约束（header-name 和 header-value 必须同时设或同时不设；设了其他 otel 字段就必须设 endpoint）。
- **MGMT 子块**：独立成另一个函数 `ParseMGMTConfigMap`，因为它来自**另一个** ConfigMap（`-mgmt-configmap`），且仅 Plus。

子块的共同特征是：**字段间有语义关联，需要跨字段校验**，而单 key 的 if-exists 模式表达不了这种关联。

#### 4.4.2 核心流程

子块函数都遵循相似的「校验失败即提前返回 error，主函数据此把 `configOk` 置 false」模式：

```
parseConfigMapZoneSync(l, cfgm, cfgParams, eventLog, nginxPlus) → error
parseConfigMapOIDC(l, cfgm, cfgParams, eventLog)               → error
parseConfigMapOpenTelemetry(l, cfgm, cfgParams, eventLog)      → error
ParseMGMTConfigMap(ctx, cfgm, eventLog) → (*MGMTConfigParams, warnings, error)
```

主函数里对前三个的调用是（[configmaps.go:501-509](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L501-L509)）：

```
_, err := parseConfigMapZoneSync(...); if err != nil { configOk = false }
err = parseConfigMapOIDC(...);          if err != nil { configOk = false }
...
_, otelErr := parseConfigMapOpenTelemetry(...); if otelErr != nil { configOk = false }
```

注意子块返回 error 但**不中断主流程**——只是把 `configOk` 拉低，与单 key 的 best-effort 策略一致。

#### 4.4.3 源码精读

**zone-sync 子块**（依赖 + Plus 门控的典型）：[internal/configs/configmaps.go:829-926](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L829-L926)。看几个要点：L836-843 在 OSS 下直接拒绝（`requires NGINX Plus`）；L846-852 校验「设了 `zone-sync-port` 但没启用 `zone-sync`」这种跨字段依赖；L869 与 L888、L907 给 port / resolver / valid 设默认值（`zoneSyncDefaultPort=12345`、`kubeDNSDefault`、`"5s"`，常量定义在 [L24-29](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L24-L29)）。结果填进 `cfgParams.ZoneSync`（结构体定义在 [config_params.go:200-207](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L200-L207)）。

**OIDC 子块**（用帮助函数消除样板）：[internal/configs/configmaps.go:747-826](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L747-L826)。10 个字段全是 `parseStringField(cfgm, key, parseFunc, assignFunc, suggestion, ...)` 的调用，区别仅在 key 名、用 `ParseTime` 还是 `ParseSize`、赋给哪个字段。这个帮助函数 [configmaps.go:1320-1332](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1320-L1322) 把「取值 → 校验 → 报错 → 赋值」四步参数化了——这是一种**柯里化（curry）风格**的复用：固定 cfgm/suggestion/logger，只让 key 和赋值闭包变化。OIDC 结果填进 `cfgParams.OIDC`（[config_params.go:210-225](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L210-L225)）。

**OpenTelemetry 子块**（跨字段约束的典型）：[internal/configs/configmaps.go:929-1016](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L929-L1016)。看 L982-990：header-name 和 header-value 必须「同时设或同时不设」，否则两者清零并报错；L992-994：只要设了 endpoint 就自动开启 `MainOtelLoadModule`（派生开关）；L996-1009：设了其他 otel 字段却没设 endpoint 则全部清零。这种「多字段联动」是单 key 模式做不到的。OTel 状态填进 `cfgParams.MainOtel*` 字段（[config_params.go:41-46](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L41-L46)）。

**MGMT 子块**（独立 ConfigMap + 独立函数）：[internal/configs/configmaps.go:1043-1171](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1043-L1171)。注意它与前三者不同：返回三元组 `(*MGMTConfigParams, warnings, error)`，且 L1049-1054 对 `license-token-secret-name` 做**强制校验**（缺失即 return error，这是 hard-fail 而非 best-effort，因为 Plus 没有 license 就没法运行管理面）。interval 字段（L1110-1140）有完整的范围校验（`minimumInterval=60`、`maximumInterval=86400`，常量在 [L25-26](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L25-L26)）。结果结构体 `MGMTConfigParams` 定义在 [config_params.go:236-249](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/config_params.go#L236-L249)。

#### 4.4.4 代码实践

**实践目标**：体会「跨字段约束」为何必须用子块函数而非单 key if-exists。

**操作步骤**：

1. 打开 [configmaps.go:982-990](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L982-L990)，这是 OTel 的 header-name/header-value 联动校验。
2. 思考：如果用主函数里的单 key if-exists 模式分别解析这两个 key，能否表达「必须同时存在或同时不存在」这个约束？
3. 在 `internal/configs/configmaps_test.go` 里搜索 `otel-exporter-header`，找到只设 name 不设 value 的测试用例，看它断言 `configOk == false` 且两个字段都被清空。

**需要观察的现象**：校验逻辑需要**同时读取两个字段**才能判断合法性——这正是子块函数存在的理由。

**预期结果**：你会确认这种跨字段约束无法用「每个 key 独立 if-exists」表达，必须集中在一个函数里处理。具体测试用例位置：参考 [configmaps_test.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps_test.go) 中 OTel 相关的 table-driven 用例（待本地确认具体行号）。

#### 4.4.5 小练习与答案

**练习 1**：`zone-sync` 相关 key 为什么需要 NGINX Plus？OSS 下会怎样？

> **参考答案**：zone sync（共享内存 zone 在多副本间同步）是 NGINX Plus 的商业特性，OSS 没有。OSS 下 `parseConfigMapZoneSync` 会在 [L838-843](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L838-L843) 报「requires NGINX Plus」并返回 error，主函数据此把 `configOk` 置 false。

**练习 2**：`parseStringField` 这个帮助函数解决的是什么问题？

> **参考答案**：OIDC 有 10 个结构同构的字段（取值 → 用 ParseTime/ParseSize 校验 → 赋值 → 失败时按统一格式报错）。`parseStringField` 用「传入 key + parseFunc + assignFunc 闭包」的方式把这套样板参数化，避免重复写 10 段几乎相同的代码。

**练习 3**：`ParseMGMTConfigMap` 的 `license-token-secret-name` 缺失时是 best-effort 还是 hard-fail？为什么？

> **参考答案**：hard-fail（[L1051-1054](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1051-L1054) 直接 return error）。因为 Plus 的管理面（usage report / agent）没有 license token 根本无法工作，不像某个 NGINX 指令写错还能用默认值兜底。这体现了「可兜底」与「不可缺失」两类校验的区别。

---

## 5. 综合实践

**任务**：自己挑一个本讲没细讲的全局参数（建议 `error-log-level` 或 `keepalive-timeout`），完整复现「ConfigMap → nginx.conf」的追踪，并产出一表格。

**操作步骤**：

1. 在 [configmaps.go](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go) 的 `ParseConfigMap` 里找到你选的 key 的解析 if-exists 块，确认它用的是哪种解析模式（直接字符串 / GetMapKeyAsBool / GetMapKeyAsInt / ParseSize 等）。
2. 确认它写入 `ConfigParams` 的哪个字段。
3. 在 `GenerateNginxMainConfig`（[L1203-1287](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1203-L1287)）里找到该字段被搬进 `MainConfig` 的哪一行。
4. 在 `internal/configs/version1/nginx.tmpl`（OSS）和 `nginx-plus.tmpl`（Plus）里搜索对应的 `{{.字段名}}`，确认**两个模板都渲染了它**（这是 CLAUDE.md 强调的「OSS/Plus 模板必须同步」不变量）。
5. 检查 `internal/configs/version1/__snapshots__/template_test.snap` 里是否有该指令的默认输出。

**产出**：填一张「5 跳追踪表」，每跳给出文件名、行号、字段名/占位符、一句话说明。例如对 `error-log-level`：

| 跳 | 文件 | 行号 | 字段/占位符 | 说明 |
| --- | --- | --- | --- | --- |
| 1 解析 | configmaps.go | 300-302 | `cfgParams.MainErrorLogLevel` | 直接字符串赋值 |
| 2 挂载 | controller.go | 1057 | `CfgParams` | 赋给 Configurator |
| 3 合并 | configmaps.go | 1208 | `ErrorLogLevel` | 搬进 MainConfig |
| 4 渲染 | nginx.tmpl | 11 | `{{.ErrorLogLevel}}` | `error_log stderr {{.ErrorLogLevel}};` |
| 5 写盘 | configurator.go | 1619 | — | CreateMainConfig |

（行号请以你实际阅读时为准；本表给出的是阅读起点。）

**预期结果**：你能不依赖运行环境，仅凭源码读出任意一个全局参数的完整落地路径——这正是「看懂 ConfigMap 解析子系统」的判据。

---

## 6. 本讲小结

- NIC 的全局配置有三个生命周期不同的来源：`StaticConfigParams`（flags，启动期固定）、`ConfigParams`（`-nginx-configmaps`，运行期可改）、`MGMTConfigParams`（`-mgmt-configmap`，仅 Plus）。三者最终在 `GenerateNginxMainConfig` 合并成 `MainConfig`。
- `ConfigParams` 是「大杂烩」：`Main` 前缀字段进 main nginx.conf，无前缀字段是 Ingress/VirtualServer 的资源级默认值。
- `ParseConfigMap` 的核心模式是「`NewDefaultConfigParams` 铺默认值 + 逐 key if-exists 覆盖 + best-effort（非法 key 记 Event、置 configOk=false、跳过，但不中断）」。
- 完整链路是 5 跳：`ConfigMap.Data → ConfigParams → Configurator.CfgParams → MainConfig → nginx.tmpl → nginx.conf`。`worker-processes` 是贯穿这条链的最佳示例。
- 类型转换统一封装在 `parsing_helpers.go` 的 `GetMapKeyAsBool/Int/StringSlice` 与 `ParseSize/Time` 中，遵循 `(value, exists, error)` 三返回值约定。
- 字段间有依赖或跨字段约束的 key（zone-sync / OIDC / OTel / MGMT）被抽成独立子块函数；`parseStringField` 用柯里化风格消除 OIDC 的样板代码。

---

## 7. 下一步学习建议

本讲只讲了「全局 ConfigMap」如何变成 `nginx.conf` 的 main 部分。要补全配置生成全景，建议继续：

- **u4-l3 Ingress 注解（annotations）解析**：ConfigMap 是「全局默认」，而 `nginx.org/*` 注解是「每个 Ingress 资源级别」的覆盖。两者都用 `ConfigParams` 作为汇合点，对比阅读能加深理解。
- **u4-l8 Go 模板渲染体系**：本讲只到 `nginx.tmpl` 的占位符，下一讲会讲 `TemplateExecutor` 如何加载/渲染/热替换 OSS 与 Plus 两套 `.tmpl`，以及快照黄金测试如何保证输出稳定。
- **u5-l1 nginx.Manager 与进程生命周期**：本讲第 5 跳的 `CreateMainConfig` 之后是 `Reload`，下一单元会讲 reload 的版本递增与触发细节。
- 如果你对「派生字段」感兴趣，可回头细读 `GenerateNginxMainConfig` 里 zone-sync 域名（[L1197](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/configs/configmaps.go#L1197)）如何由 `POD_NAMESPACE` 拼出，并追溯 `config.ZoneSync.Domain` 在 [controller.go:1059](https://github.com/nginxinc/kubernetes-ingress/blob/b678c44eb3c059c880f28383bf91f07f7cb9ab7c/internal/k8s/controller.go#L1059) 的赋值来源。
