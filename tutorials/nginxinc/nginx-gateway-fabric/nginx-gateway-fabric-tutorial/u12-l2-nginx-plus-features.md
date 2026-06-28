# NGINX Plus 能力：动态上游、授权与用量上报

## 1. 本讲目标

NGINX Gateway Fabric（NGF）既能驱动开源版 NGINX（OSS），也能驱动商业版 **NGINX Plus**。两种模式下控制面走的是同一条事件管线（u4）、同一套配置生成器（u6）、同一套 Agent 下发链路（u7），但在「数据面后端变动」时，二者表现差异巨大：

- **OSS**：后端 Pod 每变一次，都要重写整份 NGINX 配置并触发一次 `reload`。
- **Plus**：后端 Pod 变动可以通过 NGINX Plus 的 API **原地修改 upstream server 列表**，**无需 reload**。

本讲学完后，读者应能：

1. 说清 Plus 模式下 `UpdateUpstreamServers` 如何走「NGINX Plus API 动态更新」路径，以及为什么这条路径能跳过 reload。
2. 看懂 Plus 独有的两段配置生成——`plus-api.conf`（Plus API/dashboard）与 `mgmt` 块（授权与用量上报）。
3. 理解 licensing 的两块拼图：`deployment_ctx.json`（标识安装）与 `usage_report`（用量回传），以及它们在 init 容器、handler、生成器中分别在哪里产生。

## 2. 前置知识

阅读本讲前，建议已经掌握：

- **NGINX 的 upstream 与 reload**：`upstream {}` 块里列后端地址；OSS 改后端必须 `reload`（重新读配置、重建 worker），高频变动会带来抖动。Plus 提供运行时 API，可在不 reload 的情况下增删 upstream server。
- **NGF 的下发链路**（u7-1、u7-2）：控制面经 NGINX Agent 用 gRPC 把配置推给数据面；`NginxUpdater` 是下发总入口，`Deployment` 按「Deployment 维度」组织配置文件，`broadcast` 负责「推清单 + Agent 反向拉文件 + 等待全部响应」。
- **配置生成器总览**（u6-1）：`GeneratorImpl.Generate` 把 `dataplane.Configuration` 渲染成 `[]agent.File`，`executeConfigTemplates` 用一组 `executeFunc` 按 dest 分桶合并。
- **controller 命令与 Plus flag**（u2-2）：控制面由 `--nginx-plus` 开启 Plus 模式，开启时还须提供 `--usage-report-secret` 等授权参数。

一个关键术语：**动态 upstream（dynamic upstream）**——指通过 NGINX Plus API 在运行时修改 upstream server 列表的能力，与之相对的是「改配置 + reload」的静态方式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `internal/controller/nginx/agent/agent.go` | `NginxUpdater` 实现，含 Plus 专属的 `UpdateUpstreamServers`（动态 upstream） |
| `internal/controller/nginx/agent/action.go` | `actionsEqual`：判断 Plus API 动作是否变化，是「跳过 reload」的裁判 |
| `internal/controller/nginx/agent/broadcast/broadcast.go` | 定义 `ConfigApplyRequest` 与 `APIRequest` 两种消息类型 |
| `internal/controller/nginx/config/generator.go` | `GenerateDeploymentContext` 序列化授权上下文；装配执行函数链 |
| `internal/controller/nginx/config/plus_api.go` / `plus_api_template.go` | 生成 `plus-api.conf`（Plus API 与 dashboard） |
| `internal/controller/nginx/config/main_config.go` / `main_config_template.go` | 生成 `mgmt` 块（授权 / usage_report / license_token） |
| `internal/controller/nginx/config/upstreams.go` / `upstreams_template.go` | upstream 块生成，含 Plus 专属的 zone 大小、state file、resolve 处理 |
| `internal/controller/licensing/collector.go` | `DeploymentContextCollector`：采集 Plus 授权上下文 |
| `internal/controller/config/config.go` | `UsageReportConfig`：用量上报参数 |
| `cmd/gateway/commands.go` | `buildUsageReportConfig`：Plus 下强制要求 usage-report-secret |
| `cmd/gateway/initialize.go` | init 容器里生成 `deployment_ctx.json` 的另一条路径 |

## 4. 核心概念与源码讲解

### 4.1 动态 upstream 更新：跳过 reload 的关键

#### 4.1.1 概念说明

NGF 的核心痛点是：Kubernetes 里后端 Pod 的增删改非常频繁（扩缩容、滚动更新、健康检查剔除）。如果每次后端变动都要重写整份 NGINX 配置并 `reload`，数据面就会频繁抖动、连接被打断。

NGINX Plus 提供了一条「旁路」：它暴露一个运行时 API，可以在不 `reload` 的前提下，直接增删某个 upstream 的 server 条目。NGF 在 Plus 模式下就利用这条旁路处理**纯后端变动**——这正是 `UpdateUpstreamServers` 的职责。

它的实现前提是 NGINX 的 **upstream 共享内存 zone** 与 **state file**：

- `zone <name> <size>;` 让该 upstream 的 server 列表常驻共享内存，运行时可被 API 改写。
- `state <file>;` 把动态改写后的 server 列表持久化到磁盘，使 NGINX 重启后能恢复，避免「API 改了、重启丢了」。

只有「不带 `resolve` 的 server」才能被 Plus API 动态管理；带 `resolve` 的 server 由 NGINX 自己做 DNS 解析，属「不可变」上游，强行动态改写会报 `UpstreamServerImmutable` 错误。因此 NGF 对这两类上游区别对待。

#### 4.1.2 核心流程

一次事件批次里，handler 处理完配置下发后会判断是否为 Plus 模式，若是则额外调用 `UpdateUpstreamServers`：

```
handler.sendNginxConfig (Plus 模式)
  ├─ generator.Generate(conf)            # 生成整份配置文件（OSS/Plus 都做）
  ├─ nginxUpdater.UpdateConfig(...)      # 推文件 + reload（OSS/Plus 都做）
  └─ nginxUpdater.UpdateUpstreamServers(deployment, conf)   # ★ Plus 专属
        ├─ 遍历 conf.Upstreams / conf.StreamUpstreams
        │     └─ 跳过带 resolve 的 upstream（不可动态改）
        │     └─ 每个 upstream 产出一条 NGINXPlusAction
        ├─ actionsEqual(旧 actions, 新 actions)? → 相同则直接 return（零 reload）
        ├─ 逐条把 action 包成 APIRequest 消息广播给 Agent
        │     └─ Agent 调用 Plus API 改写 upstream server（不 reload）
        └─ 把最新 actions 记到 deployment（供新接入的 Agent 补播）
```

注意：Plus 模式下 `UpdateConfig`（推配置 + reload）与 `UpdateUpstreamServers`（API 动态改）是**并存的**。前者负责「结构变化」（新增 upstream、改路由），后者负责「同一 upstream 内后端地址的变化」。本讲聚焦后者。

#### 4.1.3 源码精读

**入口：handler 在 Plus 模式下额外调用动态更新。**

handler 在下发配置后，判断 Plus 模式再调用动态更新（[internal/controller/handler.go:884-890](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L884-L890)）——`Generate` + `UpdateConfig` 是 OSS/Plus 共有，`UpdateUpstreamServers` 仅 Plus 执行。这就是 OSS 与 Plus 在「后端变动」路径上的分叉点。

**`NginxUpdater` 接口把两种下发都收口。**

`NginxUpdater` 接口有两个方法（[internal/controller/nginx/agent/agent.go:31-35](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L31-L35)）：`UpdateConfig`（推配置，u7-1 已讲）与 `UpdateUpstreamServers`（动态 upstream，本讲重点）。

**`UpdateUpstreamServers`：非 Plus 直接返回。**

开头第一道闸门——非 Plus 模式直接返回，整个动态更新逻辑对 OSS 不可见（[internal/controller/nginx/agent/agent.go:109-116](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L109-L116)）。

**构建 action 列表，跳过 resolve 上游。**

遍历 HTTP 与 Stream 两类 upstream，为每个（非 resolve）upstream 产出一条 `NGINXPlusAction`（[internal/controller/nginx/agent/agent.go:124-152](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L124-L152)）。跳过带 resolve server 的 upstream 是为了避免 Plus API 报 `UpstreamServerImmutable`。

**零 reload 的裁判：`actionsEqual`。**

若新旧 action 列表逐条深度相等，则直接 `return`，连广播都不发（[internal/controller/nginx/agent/agent.go:154-156](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L154-L156)）。这是「相同后端集合 → 零 reload」的核心。深度比较逻辑见 `actionsEqual`（[internal/controller/nginx/agent/action.go:8-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/action.go#L8-L31)），它逐条比对 upstream 名、server 数量、每个 server 的字段（最终落到 `structsEqual`/`valuesEqual`）。

**逐条广播为 `APIRequest` 消息。**

每条 action 包成 `broadcast.NginxAgentMessage{Type: APIRequest, ...}` 发给 Agent（[internal/controller/nginx/agent/agent.go:158-170](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L158-L170)）。注意这里用的是 `APIRequest` 而非 `ConfigApplyRequest`——广播层正是用这两种消息类型区分「改配置（要 reload）」与「调 Plus API（不 reload）」（[internal/controller/nginx/agent/broadcast/broadcast.go:237-241](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L237-L241)）。

**带重试的发送：等 NGINX reload 完。**

`sendRequest` 用 `wait.PollUntilContextCancel` 在 5 秒超时内每 500ms 重试一次，注释点明原因：「reload 后 NGINX 还没完全就绪」（[internal/controller/nginx/agent/agent.go:230-257](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L230-L257)）。这说明动态 upstream 往往紧跟一次配置下发（结构变化触发的 reload），需要等 reload 落地后 API 才可用。

**后端列表序列化：排序以减少无谓变动。**

`buildUpstreamServers` 把每个 endpoint 编码成 `{"server": "<addr>:<port>"}`，并对结果排序（[internal/controller/nginx/agent/agent.go:196-228](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L196-L228)）。排序保证「同一组后端，无论发现顺序如何，序列化结果一致」，否则 `actionsEqual` 会误判变化、触发多余下发。无 endpoint 时退化为一个 503 server。

**生成器侧的配套：zone 大小、state file、resolve。**

配置生成时，Plus 的 stream upstream 会得到更大的 zone（`1m` vs OSS 的 `512k`），并在「无 resolve server」时设置 state file 持久化动态列表（[internal/controller/nginx/config/upstreams.go:97-131](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams.go#L97-L131)）。模板里 `state` 与 `resolve` 的渲染见 [internal/controller/nginx/config/upstreams_template.go:54-62](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/upstreams_template.go#L54-L62)：有 state 才输出 `state`，有 resolve 才输出 `resolve`。

#### 4.1.4 代码实践：对比 OSS 与 Plus 的后端变动路径

**实践目标**：通过追踪源码，说清「同样的后端 Pod 变动，OSS 与 Plus 各走哪条路、为什么 Plus 能不 reload」。

**操作步骤**（源码阅读型实践）：

1. 打开 `internal/controller/handler.go:884-890`，确认 `UpdateUpstreamServers` 只在 `h.cfg.plus` 为真时调用。问自己：OSS 模式下后端变动只能走哪一行？
2. 打开 `internal/controller/nginx/agent/agent.go:154-156` 的 `actionsEqual` 早退。问自己：如果后端集合没变，OSS 会发生什么（提示：回到 `UpdateConfig`，它靠文件 hash/version 判断是否有变化，u7-2 讲过）？
3. 对比两条广播消息：`ConfigApplyRequest`（[broadcast.go:238-239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L238-L239)）会触发 Agent 写文件并 reload；`APIRequest`（[broadcast.go:240-241](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L240-L241)）触发 Agent 调 Plus API。
4. 打开 `internal/controller/nginx/config/upstreams.go:100-107`，看 Plus stream upstream 为什么「有 resolve 就不设 state file」。

**需要观察的现象**（在源码中验证，非实跑）：

- OSS 路径：后端变 → handler 只调 `UpdateConfig` → 改 upstream 块文本 → Agent reload。
- Plus 路径：后端变 → handler 同时调 `UpdateConfig`（结构若没变，文件 hash 不变，零 reload）+ `UpdateUpstreamServers`（用 Plus API 改 zone 内 server 列表，零 reload）。

**预期结果**：

- 能用一句话回答「为什么 Plus 能不 reload」：**因为后端地址变动被收敛成一次 NGINX Plus API 调用（直接改写 upstream 共享内存 zone），而非一次配置文件重写+reload；且当后端集合未变时 `actionsEqual` 直接早退，连 API 调用都省了。**

> 说明：本实践为「源码阅读型」，因为复现 Plus 动态 upstream 需要 NGINX Plus 授权镜像与License，无法在纯本地 OSS 环境实跑。可结合 `internal/controller/handler_test.go` 中 `UpdateUpstreamServersCallCount` 的断言（[handler_test.go:509,525](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler_test.go#L509)）验证调用次数的期望——待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `UpdateUpstreamServers` 要跳过带 `resolve` server 的 upstream？

**参考答案**：带 `resolve` 的 server 由 NGINX 自身做 DNS 解析、地址会动态变化，属于「不可变（immutable）」上游。若用 Plus API 强行改写会报 `UpstreamServerImmutable` 错误。所以这类 upstream 不走动态 API 路径（见 `agent.go:129,144` 的跳过逻辑），也不设 state file（见 `upstreams.go:104`）。

**练习 2**：`buildUpstreamServers` 末尾为什么要对 servers 排序？不排序会怎样？

**参考答案**：排序是为了让「同一组后端，无论 EndpointSlice 返回顺序如何，序列化结果都一致」。`actionsEqual` 据此判断是否变化；若不排序，后端集合不变但顺序变了会被误判为「变了」，触发多余的 API 调用（虽不 reload，但产生无谓流量与状态抖动）。

**练习 3**：`sendRequest` 为什么要带重试（`agent.go:235` 注释）？

**参考答案**：动态更新通常紧跟在 `UpdateConfig` 触发的 reload 之后，而 reload 后 NGINX 可能还没完全就绪，此时调 Plus API 可能失败。所以用 5 秒超时、500ms 间隔的重试，等 reload 落地后再成功下发动态 upstream 变更。

### 4.2 Plus API 与 mgmt 块：NGINX Plus 管理面的配置生成

#### 4.2.1 概念说明

动态 upstream 需要一个「能被调用的 Plus API」。NGF 在生成的 NGINX 配置里为 Plus 暴露两段内容：

- **Plus API 与 dashboard**（`plus-api.conf`）：一个走 unix socket 的可写 API（供 Agent 动态改 upstream），一个走 8765 端口的只读 dashboard（供人查看），并按 `AllowedAddresses` 做 IP 白名单。
- **mgmt 块**（`mgmt.conf`）：NGINX Plus 的 `mgmt {}` 上下文，承载授权令牌（`license_token`）、用量上报（`usage_report`）、部署上下文（`deployment_context`）以及上报链路的 TLS 配置。

这两段是 Plus 专属：OSS 模式下根本不会生成。判断「是否生成」的开关分别是 `conf.NginxPlus.AllowedAddresses != nil` 与 `g.plus`。

#### 4.2.2 核心流程

```
generator.Generate(conf)
  └─ executeConfigTemplates(conf, policyGenerator)
        ├─ 一组 executeFunc（含 executePlusAPI）按 dest 分桶合并
        └─ if g.plus: generateMgmtFiles(conf)   # 单独产出 mgmt 相关文件
              ├─ 写 license token Secret 文件
              ├─ 可选写 CA / client 证书文件
              ├─ GenerateDeploymentContext(conf.DeploymentContext) → deployment_ctx.json
              └─ 渲染 mgmt.conf
```

`plus-api.conf` 落在 `conf.d/`，被静态 `nginx.conf` 的 `include /etc/nginx/conf.d/*.conf;`（http 上下文）拉入；`mgmt.conf` 落在 `main-includes/`，被 `include /etc/nginx/main-includes/*.conf;`（main 上下文）拉入。目录与 NGINX 上下文的对应关系在 u6-1 已讲。

#### 4.2.3 源码精读

**`executePlusAPI`：用 AllowedAddresses 是否为 nil 当开关。**

`AllowedAddresses == nil` 时返回 nil（即不生成 `plus-api.conf`），否则渲染模板写入 `plus-api.conf`（[internal/controller/nginx/config/plus_api.go:12-25](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/plus_api.go#L12-L25)）。注释明确：`AllowedAddresses` 为空意味着「不是 Plus」，不生成。

**`plus-api.conf` 模板：可写 socket + 只读 dashboard。**

模板定义了两个 server：一个监听 unix socket（`api write=on`，供 Agent 动态改 upstream），一个监听 8765（`api write=off`，dashboard，按 `AllowedAddresses` 白名单 allow/deny）（[internal/controller/nginx/config/plus_api_template.go:3-28](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/plus_api_template.go#L3-L28)）。

**`AllowedAddresses` 的来源：NginxProxy CRD，默认 127.0.0.1。**

`buildNginxPlus` 默认给 `["127.0.0.1"]`，若 `NginxProxy.NginxPlus.AllowedAddresses` 有配置则覆盖（[internal/controller/state/dataplane/configuration.go:2502-2522](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2502-L2522)）。注意：这里是判断 `AllowedAddresses != nil`（即用户显式设了），而非判空切片——所以默认值 `["127.0.0.1"]` 也会生成 plus-api。

**`generateMgmtFiles`：Plus 专属，OSS 直接返回 nil。**

第一行 `if !g.plus { return nil }`（[internal/controller/nginx/config/main_config.go:81-84](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L81-L84)）。注意它还会 panic 检查 license token 是否就位（line 88），属 fail-fast 防御。

**mgmt 文件：token / CA / client 证书 / deployment_ctx + 渲染 mgmt.conf。**

依次把 license token、可选 CA、可选 client 证书写成 Secret 文件，再调 `GenerateDeploymentContext` 写 `deployment_ctx.json`，最后把 `mgmtConf` 渲染成 `mgmt.conf`（[internal/controller/nginx/config/main_config.go:100-171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L100-L171)）。token/CA/证书内容来自 `conf.AuxiliarySecrets`（由图层的 `PlusSecrets` 注入，见 4.3）。

**`mgmt` 模板：usage_report / license_token / deployment_context / TLS。**

模板渲染 `mgmt {}` 块，含 `usage_report endpoint`、`resolver`、`license_token`、`deployment_context`，以及 `ssl_verify`/`ssl_trusted_certificate`/`ssl_certificate`/`enforce_initial_report` 等可选项（[internal/controller/nginx/config/main_config_template.go:23-47](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_template.go#L23-L47)）。`deployment_context` 路径写死为 `/etc/nginx/main-includes/deployment_ctx.json`。

**执行函数链注册 plus API。**

`getExecuteFuncs` 把 `executePlusAPI` 作为最后一条执行函数纳入链路（[internal/controller/nginx/config/generator.go:220-239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L220-L239)）；而 mgmt 文件由 `executeConfigTemplates` 末尾的 `if g.plus` 单独调用（[internal/controller/nginx/config/generator.go:198-201](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L198-L201)）——因为 mgmt 文件不是「按 dest 分桶合并」的模板产物，而是独立文件。

#### 4.2.4 代码实践：让 plus-api 与 mgmt 在模板里「现身」

**实践目标**：通过单元测试断言，理解 plus-api.conf 的生成条件与内容。

**操作步骤**（阅读测试型实践）：

1. 打开 `internal/controller/nginx/config/plus_api_test.go`，看 `TestExecutePlusAPI`（[plus_api_test.go:13-38](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/plus_api_test.go#L13-L38)）：它给两个地址 `127.0.0.1`、`25.0.0.3`，断言模板里出现 `allow 127.0.0.1;`、`allow 25.0.0.3;` 各一次，以及 `listen 8765;`、`api write=on;` 等。
2. 看 `TestExecutePlusAPI_EmptyNginxPlus`（[plus_api_test.go:40-50](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/plus_api_test.go#L40-L50)）：`NginxPlus{}`（nil 地址）时结果为 `BeNil()`——验证「OSS 不生成」。

**需要观察的现象**：把 `AllowedAddresses` 换成自定义 CIDR（如 `10.0.0.0/8`），按模板逻辑应在 dashboard server 块输出 `allow 10.0.0.0/8;`。

**预期结果**：能根据测试断言画出 plus-api.conf 的两段 server 结构，并解释为什么 `NginxPlus{}` 时返回 nil。

> 说明：可直接 `go test ./internal/controller/nginx/config/ -run TestExecutePlusAPI` 验证（OSS 环境即可，不依赖 Plus 镜像）。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `executePlusAPI` 用 `AllowedAddresses != nil` 而不是 `g.plus` 作为生成开关？

**参考答案**：`executeConfigTemplates` 里 plus-api 是模板执行链的一员（按 dest 合并），它需要一个「由配置驱动」的开关而非「由生成器实例字段驱动」。`AllowedAddresses` 在 `buildNginxPlus` 中默认非 nil（`["127.0.0.1"]`），所以只要走到了 Plus 配置就会生成；而 OSS 的默认配置 `GetDefaultConfiguration` 里 `NginxPlus: NginxPlus{}`（[configuration.go:2527](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2527)）是 nil，自然不生成。

**练习 2**：plus-api.conf 里的 unix socket server 和 8765 端口 server 各自 `api write` 取值不同，为什么？

**参考答案**：unix socket（`nginx-plus-api.sock`）是供 Agent 内部调用的「可写」API（`write=on`），动态改 upstream 需要写权限；8765 端口是给人看 dashboard 的「只读」入口（`write=off`），配合 IP 白名单 `allow/deny all` 降低被外部篡改的风险。

### 4.3 Licensing 与用量上报：deployment context 与 usage report

#### 4.3.1 概念说明

NGINX Plus 是商业软件，需要授权才能运行。NGF 在 Plus 模式下要为每个数据面 NGINX 准备两类授权材料：

1. **deployment context（部署上下文）**：一份 JSON，标识「这是谁、装在哪个集群」。含四个字段：
   - `integration`：固定 `"ngf"`，标识来源是 NGF。
   - `installation_id`：NGF Pod 的 UID。
   - `cluster_id`：kube-system 命名空间的 UID（集群级唯一标识）。
   - `cluster_node_count`：集群节点数（用于按规模计费）。
2. **usage report（用量上报）**：mgmt 块里的 `usage_report endpoint=...`，让 NGINX Plus 周期性地把用量数据回传到 NGINX 的授权服务器，配合 `license_token`（JWT 令牌）完成鉴权。

这两者一起构成 Plus 的「授权 + 计费」闭环。NGF 控制面负责**采集并生成**这些材料，真正上报是数据面 NGINX 自己做的。

值得注意：deployment context 有**两条产生路径**——init 容器（启动时一次性写盘，见 u2-3）与 handler（每次构建配置时注入，控制面运行期）。本讲聚焦后者。

#### 4.3.2 核心流程

```
启动期装配 (manager.go)
  └─ NewDeploymentContextCollector({PodUID, K8sClientReader, ...})

每次事件批次 (handler.sendNginxConfig, Plus 模式)
  └─ cfg := BuildConfiguration(...)
  └─ cfg.DeploymentContext = getDeploymentContext(ctx)   # ★ 注入
        └─ deployCtxCollector.Collect(ctx)
              ├─ Integration = "ngf", InstallationID = PodUID
              └─ telemetry.CollectClusterInformation → ClusterID + NodeCount
  └─ generator.Generate(cfg)
        └─ generateMgmtFiles
              ├─ GenerateDeploymentContext(cfg.DeploymentContext) → deployment_ctx.json
              └─ 渲染 mgmt.conf（含 usage_report endpoint, license_token, ...）

CLI 校验 (controller 命令)
  └─ buildUsageReportConfig：Plus 下强制要求 usage-report-secret
```

把 deployment context 从 `Configuration` 字段一路带到生成器，是典型的「依赖经显式传参传递」（u5-4 讲过的渲染模型惯例）。

#### 4.3.3 源码精读

**`Collector` 接口与实现。**

`Collector` 接口只有一个方法 `Collect`（[internal/controller/licensing/collector.go:19-22](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing/collector.go#L19-L22)）；`DeploymentContextCollector` 是其实现（[collector.go:37-48](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing/collector.go#L37-L48)）。接口化是为了可测试（counterfeiter 生成 fake）。

**`Collect`：填 integration/installationID，再采集集群信息。**

`Collect` 先填 `Integration: "ngf"` 与 `InstallationID: PodUID`，再调 `telemetry.CollectClusterInformation` 取 `ClusterID` 与 `NodeCount`（[internal/controller/licensing/collector.go:51-66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing/collector.go#L51-L66)）。注意它复用了 telemetry 包的集群信息采集（u11-2 讲过）——授权计量与产品遥测共享同一份「集群画像」采集逻辑。

**`DeploymentContext` 类型：JSON 标签决定字段名。**

四个字段都带 `json` tag，因为最终要 marshal 成 `deployment_ctx.json`（[internal/controller/state/dataplane/types.go:836-845](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/types.go#L836-L845)）。除 `Integration` 外都用指针（`omitempty`），表示「可能采集失败、允许缺失」。

**handler 把 deployment context 注入配置。**

handler 在构建配置后调 `getDeploymentContext`（仅 Plus），并把结果赋给 `cfg.DeploymentContext`（[internal/controller/handler.go:297-302](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L297-L302)）。`getDeploymentContext` 在非 Plus 时返回空结构（[handler.go:1027-1034](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L1027-L1034)）。

**生成器把 deployment context 序列化为文件。**

`GenerateDeploymentContext` 把 `DeploymentContext` marshal 成 JSON，包成 `agent.File` 落到 `/etc/nginx/main-includes/deployment_ctx.json`（[internal/controller/nginx/config/generator.go:163-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L163-L180)）。注释点明它「也被 init 容器使用」——`GenerateDeploymentContext` 是导出方法，init 容器直接复用。

**init 容器：另一条产生 deployment context 的路径。**

init 容器里 `initialize` 在 Plus 模式下构造 `DeploymentContext`（podUID + clusterUID + integration），调同一个 `GenerateDeploymentContext` 写盘（[cmd/gateway/initialize.go:46-59](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/initialize.go#L46-L59)）。注意 init 容器用的是 `clusterUID`（来自 downward API），而运行期 collector 用的是 API server 查到的 `ClusterID`——两条数据源，目的都是拿到「集群唯一标识」。

**`UsageReportConfig`：用量上报的全部参数。**

`UsageReportConfig` 含 SecretName（服务端凭据）、ClientSSLSecretName、CASecretName、Endpoint、Resolver、SkipVerify、EnforceInitialReport（[internal/controller/config/config.go:143-159](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/config/config.go#L143-L159)）。这些字段最终喂给 mgmt 模板（见 4.2.3）。

**CLI 校验：Plus 必须提供 usage-report-secret。**

`buildUsageReportConfig` 在 `SecretName` 为空时报错「usage-report-secret is required when using NGINX Plus」（[cmd/gateway/commands.go:636-650](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L636-L650)）。这是 u2-4 讲过的「条件必填」校验——开 Plus 就必须带授权 Secret，否则进程启动失败。

**装配：collector 在 manager 里被创建。**

`StartManager` 用 `cfg.GatewayPodConfig.UID` 作为 installationID 创建 collector（[internal/controller/manager.go:202-206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/manager.go#L202-L206)），并把它与 `&cfg.UsageReportConfig` 一并注入 handler 与 generator。

#### 4.3.4 代码实践：追踪 deployment context 的「采集 → 注入 → 落盘」全链路

**实践目标**：把 deployment context 从「在哪个文件采集」到「最终写成哪个 JSON 文件」串成一条完整调用链。

**操作步骤**（调用链追踪型实践）：

1. **采集**：读 `internal/controller/licensing/collector.go:51-66`，确认四个字段（integration/installationID/clusterID/nodeCount）分别来自哪里。问自己：为什么复用 `telemetry.CollectClusterInformation`？
2. **注入**：读 `internal/controller/handler.go:297-302`，确认 `cfg.DeploymentContext = depCtx` 这一步把采集结果挂到渲染模型上。
3. **落盘**：读 `internal/controller/nginx/config/main_config.go:152-157`（`GenerateDeploymentContext` 调用）+ `generator.go:163-180`（序列化），确认最终文件名是 `/etc/nginx/main-includes/deployment_ctx.json`。
4. **引用**：读 `main_config_template.go:32`，确认 mgmt 块里 `deployment_context /etc/nginx/main-includes/deployment_ctx.json;` 指向的就是上一步的文件。

**需要观察的现象**：deployment context 的 `installation_id` 来自 Pod UID（每次 Pod 重建会变），`cluster_id` 来自 kube-system namespace UID（集群级稳定）。想清楚这个差异对「授权计量」意味着什么。

**预期结果**：能画出一条链路：`manager 装配 collector → handler.Collect → Configuration.DeploymentContext → generateMgmtFiles → GenerateDeploymentContext → deployment_ctx.json → nginx.conf mgmt 块引用`。

> 说明：本实践为源码追踪型。`go test ./internal/controller/licensing/...` 可验证 collector 的采集逻辑（需 envtest 或 fake client 提供集群信息）——待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：deployment context 有 init 容器和 handler 两条产生路径，为什么不只留一条？

**参考答案**：init 容器在 Pod 启动时一次性写盘，保证「NGINX 首次启动就有 deployment context 可用」（否则首启时授权检查可能失败）；handler 在运行期每次构建配置时重新采集并下发，保证「集群信息变化（如节点扩容导致 node_count 变化）后能更新」。两条路径互补：init 兜底首启，handler 维持新鲜。两者复用同一个导出方法 `GenerateDeploymentContext`。

**练习 2**：`buildUsageReportConfig` 为什么强制要求 `SecretName`？没有它会怎样？

**参考答案**：Plus 的用量上报需要服务端凭据（license/JWT 相关 Secret）才能向 NGINX 授权服务器鉴权。没有这个 Secret，mgmt 块无法配置 `license_token`，NGINX Plus 会因无法完成授权而拒绝工作。所以 CLI 层在装配前就 fail-fast 拒绝启动（u2-4 的「条件必填」）。

**练习 3**：为什么 `DeploymentContext` 的字段大多用指针 + `omitempty`？

**参考答案**：因为这些字段依赖运行期采集（API server 查询、downward API），采集可能部分失败。用指针 + `omitempty` 允许「缺哪个字段就不序列化哪个」，而不是写空字符串/0，避免 JSON 里出现误导性的占位值；同时保持 schema 的前向兼容（字段可选）。

## 5. 综合实践

**任务**：以「一次后端 Pod 扩缩容」为场景，画出 OSS 与 Plus 两条完整路径，并标注每一步涉及的源码位置。

要求：

1. 在图上标出分叉点：handler 的 `if h.cfg.plus`（[handler.go:888-890](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/handler.go#L888-L890)）。
2. OSS 路径：标注 `UpdateConfig`（[agent.go:88-105](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L88-L105)）→ 文件 hash 变化 → reload。
3. Plus 路径：标注 `UpdateUpstreamServers`（[agent.go:109-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L109-L180)）→ `actionsEqual` 判等（[action.go:8-31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/action.go#L8-L31)）→ `APIRequest` 广播（[broadcast.go:240-241](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L240-L241)）→ Plus API 改 upstream zone（零 reload）。
4. 在图边补注授权侧：deployment context 的采集（[collector.go:51-66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing/collector.go#L51-L66)）与 mgmt 块生成（[main_config.go:81-171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L81-L171)）如何为 Plus 提供运行前提。
5. 写一段总结：用「reload 次数」和「数据面是否需要读新配置文件」两个维度，量化 OSS 与 Plus 在高频后端变动下的差异。

> 说明：本综合实践为文档/源码梳理型，无需运行 Plus 集群。产出是一张带源码行号引用的对比图与一段量化分析。

## 6. 本讲小结

- **动态 upstream 是 Plus 的核心红利**：`UpdateUpstreamServers`（[agent.go:109-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/agent.go#L109-L180)）把「后端地址变动」从「改配置 + reload」变成「调 Plus API 改 upstream zone」，且 `actionsEqual`（[action.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/action.go)）让「后端集合未变」时连 API 调用都省掉——这是 Plus 能不 reload 的根因。
- **两种广播消息区分两条路径**：`ConfigApplyRequest`（写文件 + reload）与 `APIRequest`（调 Plus API，不 reload）（[broadcast.go:237-241](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/agent/broadcast/broadcast.go#L237-L241)）。
- **Plus 管理面有两段专属配置**：`plus-api.conf`（可写 socket + 只读 dashboard，[plus_api.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/plus_api.go)）与 `mgmt` 块（[main_config.go:81-171](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L81-L171)），OSS 模式下均不生成。
- **授权闭环 = deployment context + usage report**：collector 采集安装/集群信息（[collector.go:51-66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/licensing/collector.go#L51-L66)），handler 注入、生成器落盘成 `deployment_ctx.json`，mgmt 块的 `usage_report`/`license_token` 完成回传与鉴权。
- **deployment context 有 init 容器与 handler 两条互补路径**，复用同一个导出方法 `GenerateDeploymentContext`（[generator.go:163-180](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L163-L180)）。
- **Plus 是「条件必填」的开关**：开 `--nginx-plus` 就必须提供 `--usage-report-secret`，CLI 层 fail-fast 拒绝（[commands.go:636-650](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/cmd/gateway/commands.go#L636-L650)）。

## 7. 下一步学习建议

- **u12-l3（TLS 安全）**：本讲提到 mgmt 块的 `ssl_certificate`/`ssl_trusted_certificate` 是上报链路的 mTLS，下一讲会系统讲前端 TLS、BackendTLSPolicy 与 Agent mTLS，与本讲的授权 mTLS 形成完整 TLS 图景。
- **延伸阅读**：`internal/controller/nginx/config/upstreams.go` 的 HTTP upstream 生成（zone/keepalive/state），对照 4.1 的动态 upstream，理解「可被 API 改写」的 upstream 在配置文本里长什么样。
- **动手验证**：若能拿到 NGINX Plus 镜像，用 `examples/` 里的 nginx-plus 部署变体（u1-4）部署，触发后端扩缩容，观察 NGINX 日志里是否**没有** reload 记录、而 `plus-api` 访问日志里有 upstream server 增删——直接印证本讲的「零 reload」结论。
