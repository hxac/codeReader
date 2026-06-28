# nginx.conf 骨架与模板体系

## 1. 本讲目标

上一讲（u6-l1）我们看清了「配置生成器」的总装：`Generate` 把 `dataplane.Configuration` 经 12 个 `executeFunc` 渲染成一组 `agent.File`，并按目标路径「分桶合并」。但有一个关键问题被刻意跳过了：**这堆生成的文件，NGINX 到底是怎么加载到的？谁第一个读？谁 include 谁？**

本讲就来回答它。读完本讲，你应当能够：

1. 说清静态 `nginx.conf` 为什么是一切配置的「入口」，以及它和生成文件之间的 **include 拓扑**。
2. 对照 `nginx.conf` 里的每一条 `include`，准确说出它拉入的目录、目录里的文件、以及生成这些文件的 `executeFunc` 与模板。
3. 掌握 `main` / `events` / `base http` 三套「骨架模板」各自的职责，以及它们如何用 Go `text/template` 组织。
4. 理解 **include 机制**：用户片段（snippets）、策略（policy）、授权 map 是如何被转换成一个个独立 `.conf` 文件、并被去重、再被骨架模板 `include` 进来的。

本讲聚焦「骨架层」，server/upstream/location 块内部的指令细节留给 u6-l3，stream 配置留给 u6-l4。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**NGINX 配置的「上下文（context）」层级。** 一份 NGINX 配置从外到内有几个固定上下文：

- **main 上下文**：最外层，放进程级指令，如 `load_module`、`worker_processes`、`pid`、`error_log`。
- **events 上下文**：`events { ... }` 块内，放连接级指令，如 `worker_connections`。
- **http 上下文**：`http { ... }` 块内，放所有 HTTP 相关指令、`server`、`upstream`、`map` 等。
- **stream 上下文**：`stream { ... }` 块内，放 TCP/UDP（L4）相关指令。

NGINX 启动时**先读主配置文件**（默认 `/etc/nginx/nginx.conf`），再顺着里面的 `include` 指令把别的文件「展开」到对应上下文里。`include` 不是函数调用，而是**文本展开**——被 include 的文件内容会被原样插入到 `include` 所在的位置和上下文。

**NGF 的核心设计：`nginx.conf` 是静态骨架，生成的配置全靠 include 拼装。** NGF **从不生成** `nginx.conf` 本身，它是一份手写、随镜像内置的静态文件。生成器只产出「被 include 的片段文件」。这样设计的好处是：主结构（四大上下文、stub_status 健康端点、hash 桶尺寸等基础设施）稳定不变，只有「业务相关的那部分」随集群状态动态重写，差异面更小、更安全。

**Go `text/template` 的基本动作。** NGF 用标准库 `text/template` 把 Go 结构体渲染成配置文本：

- `{{ .字段名 }}` 取字段值；
- `{{ if .字段 }}...{{ end }}` 条件渲染（字段为零值则跳过，这是 NGF 大量「按需生成指令」的基础）；
- `{{ range .切片 }}...{{ end }}` 遍历；
- `{{-` / `-}}` 的减号是**修剪空白**，用来去掉模板控制行留下的多余空行，让生成结果干净。

模板在包加载时用 `gotemplate.Must(...)` 一次性 `Parse` 成全局变量，运行时只 `Execute`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf) | **静态主配置骨架**，随镜像内置，永不被生成器改写。定义 main/events/http/stream 四大上下文与四条 `include`。 |
| [internal/controller/nginx/config/main_config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go) | 实现 main 上下文（`executeMainConfig`）与 events 上下文（`executeEventsConfig`）的执行函数，以及 Plus 的 mgmt 块生成。 |
| [internal/controller/nginx/config/main_config_template.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_template.go) | 三套模板的**文本常量**：`mainConfigTemplateText`、`eventsConfigTemplateText`、`mgmtConfigTemplateText`。 |
| [internal/controller/nginx/config/base_http_config.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config.go) | 实现 http 上下文「地基」（`executeBaseHTTPConfig`）：DNS resolver、map、健康检查 server、日志、OIDC、gzip 等。 |
| [internal/controller/nginx/config/base_http_config_template.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config_template.go) | http 地基的模板文本常量 `baseHTTPTemplateText`。 |
| [internal/controller/nginx/config/generator.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go) | 装配入口：定义目录常量、`executeFunc` 列表、以及把结果「按目标路径分桶合并」的 `executeConfigTemplates`。 |
| [internal/controller/nginx/config/includes.go](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go) | **include 机制**：把 snippet / policy / authz 转成 `shared.Include` 并去重的工具函数集合。 |

## 4. 核心概念与源码讲解

### 4.1 nginx.conf 骨架：静态主文件与四条 include

#### 4.1.1 概念说明

`nginx.conf` 是 NGINX 启动后读的**第一个文件**，是整棵配置树的根。NGF 的这份 `nginx.conf` 只有 55 行，刻意保持极简：它**只搭骨架、定上下文、留 include**，把所有可变内容都外包给生成器产出的片段文件。

理解它的关键是一句话：**静态骨架定义「在哪个上下文 include 哪个目录」，生成器负责「往那个目录里放什么文件」**。骨架是稳定的契约，生成器是动态的实现。

#### 4.1.2 核心流程

NGINX 加载这份 `nginx.conf` 时的展开顺序（注意 `include` 是按文件出现位置就地展开）：

```text
1. load_module ngx_http_js_module.so     ← main 上下文，静态、总是加载
2. include main-includes/*.conf          ← main 上下文，生成器填：otel/waf 的 load_module、error_log、main 片段、Plus 的 mgmt
3. worker_processes / pid                ← main 上下文，静态
4. events { include events-includes/*.conf }  ← events 上下文，生成器填：worker_connections
5. http {
     include mime.types                  ← 静态
     js_import ...                       ← 静态（njs 模块脚本）
     各类 hash 桶尺寸 / sendfile ...       ← 静态
     include conf.d/*.conf               ← http 上下文，生成器填：base http + servers + upstreams + maps + telemetry + plus-api
     server { stub_status }              ← 静态，NGINX 自身状态端点
   }
6. stream {
     各类 hash / log_format / access_log  ← 静态
     include stream-conf.d/*.conf        ← stream 上下文，生成器填：stream servers + upstreams + maps
   }
```

一个容易忽略的细节：**第 1、2 行的 `load_module` 必须排在最前**。NGINX 规定 `load_module` 只能出现在 main 上下文、且必须早于其它指令。所以静态的 `load_module`（第 1 行，JS 模块，总需要）紧跟一条 `include main-includes/*.conf`（第 2 行），把生成器按需产出的 `load_module`（otel、WAF 模块）也集中放到最前面，二者一起满足「load_module 必须在最前」的约束——这就是为什么 main 的 include 排在 `worker_processes` 之前。

#### 4.1.3 源码精读

先看静态骨架本身。[internal/controller/nginx/conf/nginx.conf:L1-L10](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L1-L10) 定义了 main 与 events 上下文：

```nginx
load_module modules/ngx_http_js_module.so;
include /etc/nginx/main-includes/*.conf;

worker_processes auto;

pid /var/run/nginx/nginx.pid;

events {
  include /etc/nginx/events-includes/*.conf;
}
```

第 1 行静态加载 JS 模块（`httpmatches.js`、`epp.js` 这两个 njs 脚本依赖它）；第 2 行把 main 上下文的可变部分（含按需的 otel/WAF `load_module`、`error_log`）交给生成器；第 9 行把 events 上下文整体交给生成器。

http 与 stream 上下文见 [internal/controller/nginx/conf/nginx.conf:L12-L55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L12-L55)。其中两条关键 include：

```nginx
http {
  ...
  include /etc/nginx/conf.d/*.conf;        # L31，http 上下文的全部业务配置
  ...
}
stream {
  ...
  include /etc/nginx/stream-conf.d/*.conf; # L54，stream 上下文的全部业务配置
}
```

注意骨架里还写死了两个「NGINX 自身用的、与 Gateway API 业务无关」的东西：[L13](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L13) 的 `include mime.types`，以及 [L33-L40](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L33-L40) 监听 unix socket 的 `stub_status` server。它们是基础设施，不随用户资源变化，所以固化在骨架里。

这四条 `include` 各自指向的目录，由生成器侧的常量一一对应，见 [internal/controller/nginx/config/generator.go:L32-L77](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L32-L77)：

| 骨架里的 include | 目录常量 | 通配匹配的生成文件 |
| --- | --- | --- |
| `main-includes/*.conf` (L2) | `mainIncludesFolder` | `main.conf`（+ 仅 Plus 的 `mgmt.conf`） |
| `events-includes/*.conf` (L9) | `eventsIncludesFolder` | `events.conf` |
| `conf.d/*.conf` (L31) | `httpFolder` | `http.conf`（+ 仅 Plus 的 `plus-api.conf`） |
| `stream-conf.d/*.conf` (L54) | `streamFolder` | `stream.conf` |

> 小陷阱：`deployment_ctx.json`、`matches.json` 虽然也落在这些目录下，但后缀是 `.json`，**不会被 `*.conf` 通配命中**——它们是被具体指令按绝对路径引用的，而非靠 include 拉入。这点在 4.3 节再展开。

#### 4.1.4 代码实践

**实践目标**：亲手验证「四条 include ↔ 四个目录常量 ↔ 哪些 executeFunc 写入」这张映射表，建立骨架与生成器的对应关系。

**操作步骤**：

1. 打开 [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf)，逐条圈出 4 处 `include`，记下它们所在的 NGINX 上下文（main / events / http / stream）。
2. 打开 [internal/controller/nginx/config/generator.go:L220-L239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L220-L239) 的 `getExecuteFuncs`，这是 12 个执行函数的总清单。
3. 对每个 executeFunc，跟踪它产出的 `executeResult.dest`，归类到上表的某个目录。提示：`executeMainConfig` 的 dest 是 `mainIncludesConfigFile`，`executeEventsConfig` 是 `eventsIncludesConfigFile`，`executeBaseHTTPConfig`/servers/upstreams/split_clients/maps/telemetry/plusAPI 都是 `httpConfigFile` 或 `plus-api.conf`，stream 三件套是 `streamConfigFile`。

**需要观察的现象**：你会发现 12 个 executeFunc **不是一一对应 4 个目录**，而是「多个函数写同一个 dest 文件」——这正是 u6-l1 讲过的「按 dest 分桶合并」。骨架的 4 条 include 拉的是「目录」，而生成器侧把同一上下文的多个片段**合并成少数几个 `.conf` 文件**放进去。

**预期结果**：你应能填出下表（答案见 4.1.5）。

**待本地验证**：若想看真实落盘结果，可在跑通的开发环境里 `kubectl exec` 进数据面 Pod，`ls /etc/nginx/main-includes /etc/nginx/events-includes /etc/nginx/conf.d /etc/nginx/stream-conf.d`，对照生成的文件名。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `nginx.conf` 第 2 行的 `include main-includes/*.conf` 必须排在 `worker_processes` 之前？

**参考答案**：因为被 include 进来的 `main.conf` 里含有 `load_module` 指令（按需加载 otel、WAF 模块），而 NGINX 要求所有 `load_module` 必须出现在 main 上下文的最前面、早于其它任何指令。把这条 include 放在最前，等价于把生成器按需产出的 `load_module` 与第 1 行静态的 `load_module` 一起集中到 main 上下文的开头，满足该约束。

**练习 2**：填出 4.1.4 中那张「include → 目录 → executeFunc」映射表。

**参考答案**：

| include（骨架行号） | 目录 | 写入该目录的 executeFunc |
| --- | --- | --- |
| `main-includes/*.conf` (L2) | `mainIncludesFolder` | `executeMainConfig`（+ 仅 Plus：`generateMgmtFiles`） |
| `events-includes/*.conf` (L9) | `eventsIncludesFolder` | `executeEventsConfig` |
| `conf.d/*.conf` (L31) | `httpFolder` | `executeBaseHTTPConfig`、`executeServers`、`executeUpstreams`、`executeSplitClients`、`executeMaps`、`executeTelemetry`（均并入 `http.conf`）+ 仅 Plus：`executePlusAPI`（`plus-api.conf`） |
| `stream-conf.d/*.conf` (L54) | `streamFolder` | `executeStreamServers`、`executeStreamUpstreams`、`executeStreamMaps`（均并入 `stream.conf`） |

### 4.2 main / events / base http 三套骨架模板的职责

#### 4.2.1 概念说明

骨架模板负责生成「每个上下文里那些**全局性、与具体路由无关**的指令」。它们和 u6-l3 将讲的 server/upstream 模板是搭档关系：骨架模板搭上下文地基，server/upstream 模板往地基上加业务块，最后合并进同一个 `http.conf`。

三套骨架模板各管一个上下文：

- **main 模板** → `main.conf`（main 上下文）：按需 `load_module`、`error_log`、main 片段 include。
- **events 模板** → `events.conf`（events 上下文）：只有 `worker_connections`。
- **base http 模板** → 并入 `http.conf`（http 上下文地基）：http2、DNS resolver、若干 `map`、健康检查 server、日志格式、网关证书、OIDC、gzip 等。

#### 4.2.2 核心流程

每个骨架 executeFunc 的套路高度一致：

```text
1. 收集 includes（来自用户 snippets、policy 生成结果、authz map）
2. 组装一个「模板数据结构」（mainConfig / 直接用 conf / httpConfig）
3. 用 helpers.MustExecuteTemplate(模板, 数据) 渲染出主文件字节
4. 再为每个 include 额外产出一个独立文件（executeResult）
5. 全部以 []executeResult 返回，交给 executeConfigTemplates 按 dest 分桶
```

关键在于第 1 步「收集 includes」与第 4 步「每个 include 一个文件」——这是 include 机制的体现，4.3 节详述。本节聚焦模板本身渲染出了什么。

#### 4.2.3 源码精读

**main 模板。** 模板文本见 [internal/controller/nginx/config/main_config_template.go:L3-L17](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_template.go#L3-L17)：

```go
const mainConfigTemplateText = `
{{ if .Conf.Telemetry.Endpoint -}}
load_module modules/ngx_otel_module.so;
{{ end -}}

{{ if .Conf.WAF.Enabled -}}
load_module modules/ngx_http_app_protect_module.so;
{{ end -}}

error_log stderr {{ .Conf.Logging.ErrorLevel }}{{ if eq .Conf.Logging.ErrorLogFormat "json" }} json{{ end }};

{{ range $i := .Includes -}}
include {{ $i.Name }};
{{ end -}}
`
```

读法：仅当配置了遥测 endpoint 时才 `load_module` 加载 otel 模块；仅当启用 WAF 时才加载 app_protect 模块；`error_log` 按用户设定的级别与格式输出到 stderr；最后把所有 main 级 include（片段、main 策略）逐条 `include` 进来。注意 `{{- ... -}}` 的减号在修剪空白，所以条件不成立时不会留下空行。

执行函数 [internal/controller/nginx/config/main_config.go:L36-L55](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L36-L55) 把片段和策略结果拼成 includes，装入 `mainConfig{Conf, Includes}`，渲染出 `main.conf`，再为每个 include 产出独立文件：

```go
includes := createIncludesFromSnippets(conf.MainSnippets)
policyIncludes := createIncludesFromPolicyGenerateResult(generator.GenerateForMain(conf.Policies))
includes = append(includes, policyIncludes...)
mc := mainConfig{Conf: conf, Includes: includes}
results := append(results, executeResult{
    dest: mainIncludesConfigFile,                                  // /etc/nginx/main-includes/main.conf
    data: helpers.MustExecuteTemplate(mainConfigTemplate, mc),
})
results = append(results, createIncludeExecuteResults(includes)...) // 每个 include 一个文件
```

**events 模板。** 最简单，[internal/controller/nginx/config/main_config_template.go:L19-L21](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_template.go#L19-L21) 只有一行指令：

```go
const eventsConfigTemplateText = `
worker_connections {{ .WorkerConnections }};
`
```

执行函数 [internal/controller/nginx/config/main_config.go:L57-L66](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L57-L66) 直接把整个 `conf`（`dataplane.Configuration`）当模板数据，取其 `WorkerConnections` 字段渲染成 `events.conf`：

```go
func executeEventsConfig(conf dataplane.Configuration) []executeResult {
    eventsData := helpers.MustExecuteTemplate(eventsConfigTemplate, conf)
    return []executeResult{{dest: eventsIncludesConfigFile, data: eventsData}}
}
```

`WorkerConnections` 默认 1024，由 `dataplane.DefaultWorkerConnections` 定义（[internal/controller/state/dataplane/configuration.go:L34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L34)），可经 `NginxProxy.WorkerConnections` 覆盖。

**base http 模板。** 三套里最丰富，负责 http 上下文的地基。数据结构是 [internal/controller/nginx/config/base_http_config.go:L53-L68](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config.go#L53-L68) 的 `httpConfig`，执行函数 [internal/controller/nginx/config/base_http_config.go:L76-L112](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config.go#L76-L112) 把 base http snippets、http 策略、authz map 三类来源拼成 includes 后渲染。模板文本 [internal/controller/nginx/config/base_http_config_template.go:L4-L179](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config_template.go#L4-L179) 输出几类内容，挑关键的看：

```nginx
{{- if .HTTP2 }}http2 on;{{ end }}            # 按需开启 HTTP/2

{{- if .DNSResolver }}                         # ExternalName 服务用的 DNS resolver
resolver ... ;
{{- end }}

map $http_host $gw_api_compliant_host { ... }  # 一组全局 map（Gateway API 合规 host、连接升级等）
map $http_upgrade $connection_upgrade { ... }
...

server {                                       # 健康检查 server，监听就绪探针端口
    listen {{ .NginxReadinessProbePort }};
    location = {{ .NginxReadinessProbePath }} { return 200; }
}

{{- if .AccessLog }} log_format / access_log ... {{ end }}  # 用户自定义日志格式

{{ range $i := .Includes -}}                   # base http 级 include（片段/策略/authz）
include {{ $i.Name }};
{{ end -}}

{{- range .OIDCProviders }} oidc_provider ... {{ end }}      # OIDC 提供者块
```

注意 base http 模板里也有一个 `server` 块——但它是**健康检查 server**（监听 `NginxReadinessProbePort`，命中探针路径返回 200），和 u6-l3 将讲的「业务虚拟 server」不是一回事。这类「地基级」、与具体路由无关的 server 留在 base http 模板里。

> 这三套模板在包加载时一次性 `Parse` 成全局变量，见 [internal/controller/nginx/config/main_config.go:L19-L23](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config.go#L19-L23)（main/mgmt/events）与 [internal/controller/nginx/config/base_http_config.go:L15](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/base_http_config.go#L15)（base http）。渲染统一走 [internal/framework/helpers/helpers.go:L85-L93](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/framework/helpers/helpers.go#L85-L93) 的 `MustExecuteTemplate`——渲染失败直接 `panic`，因为模板与数据结构都由开发者掌控，出错即视为编译期级别的 bug。

#### 4.2.4 代码实践

**实践目标**：追踪 `worker_connections` 这一个指令，从用户配置一路追到 `events.conf`，体会「一个 flag/字段如何穿过 dataplane 进入模板」。

**操作步骤**：

1. 在 `NginxProxy`（数据面参数 CRD）里设有 `workerConnections` 字段；阅读 [internal/controller/state/dataplane/configuration.go:L2475-L2485](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2475-L2485) 的 `buildWorkerConnections`，看它如何取值（无配置则回落到 `DefaultWorkerConnections`）。
2. 顺 [internal/controller/state/dataplane/configuration.go:L2529](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/state/dataplane/configuration.go#L2529) 看该值如何写入 `dataplane.Configuration.WorkerConnections`。
3. 回到本讲的 `executeEventsConfig`，确认模板 `{{ .WorkerConnections }}` 取的就是这个字段。

**需要观察的现象**：一个用户可调参数，其传递路径是 `NginxProxy → graph → dataplane.Configuration → 模板 → events.conf`，中间没有任何「生成器额外读 K8s」的环节——生成器只吃 `dataplane.Configuration` 这一个入参（呼应 u6-l1 的领域解耦）。

**预期结果**：默认 `worker_connections 1024;`；若把 `NginxProxy.workerConnections` 设为 2048，生成的 `events.conf` 应为 `worker_connections 2048;`。

**待本地验证**：运行既有单测可直接观察渲染结果：

```bash
go test ./internal/controller/nginx/config/ -run TestExecuteEventsConfig_WorkerConnections -v
```

该测试（见 [internal/controller/nginx/config/main_config_test.go:L252-L284](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_test.go#L252-L284)）断言了「设 2048 渲染出 `worker_connections 2048;`、默认渲染出 `worker_connections 1024;`」。

#### 4.2.5 小练习与答案

**练习 1**：静态 `nginx.conf` 第 1 行已经 `load_module ngx_http_js_module.so`，为什么 otel 和 WAF 的 `load_module` 却要放进**生成的** `main.conf`，而不是也写死在静态骨架里？

**参考答案**：JS 模块是 NGF 运行所必需的（`httpmatches.js`、`epp.js` 这两个 njs 脚本始终依赖它），所以静态总是加载。而 otel 模块仅在配置了遥测 endpoint 时才需要、WAF（app_protect）模块仅在启用 WAF 时才需要；若写死在骨架里，未启用这些特性的环境也会强制加载对应 `.so`，既浪费又可能在不含该模块的镜像里报错。放进生成的 `main.conf` 后，靠模板的 `{{ if }}` 按需产出，做到了「用到才加载」。

**练习 2**：base http 模板里出现的那个 `server { ... }` 块是做什么的？它和路由规则里的 server 是同一个东西吗？

**参考答案**：它是 NGF 控制面自己的**健康检查 server**——监听 `NginxReadinessProbePort`、对就绪探针路径返回 200，用于存活/就绪探测。它和 u6-l3 将讲的、由 HTTPRoute/Listener 翻译出来的业务虚拟 server 不是一回事；它属于「地基级、与路由无关」的设施，所以放在 base http 模板里，和 http2、resolver、map 等全局指令并列。

### 4.3 include 机制：片段与策略如何落地为独立文件

#### 4.3.1 概念说明

骨架模板里反复出现 `{{ range .Includes }} include {{ .Name }} {{ end }}`。这里的 `Includes` 不是把片段内容**内联**进 `main.conf`/`http.conf`，而是为每个片段生成一个**独立的 `.conf` 文件**，再用 `include` 指令按文件名引用。

为什么要「一个片段一个文件」而不是内联？三个原因：

1. **去重**：同一段策略/snippets 可能被多条路由复用，独立成文件后只需生成一份、多处 include。
2. **可哈希、可增量下发**：NGINX Agent 按文件做 hash 比对，未变的文件不必重传（呼应 u7 的下发机制）。
3. **隔离上下文**：片段可以精确放到正确的目录（main/http/stream/server/location）从而进入正确的 NGINX 上下文。

#### 4.3.2 核心流程

include 的产生与消费链路：

```text
来源（三类）
  ├─ 用户 snippets      （dataplane.Snippet：main / base http / server / location）
  ├─ policy 生成结果     （policies.File：由复合策略生成器产出）
  └─ authz map          （dataplane.AuthZConfig：鉴权用的 map 块）
        │
        ▼  createIncludesFrom* / createIncludeFrom*  （includes.go）
  统一成 []shared.Include{Name, Content}
        │
        ├─ 传入骨架模板的 Includes 字段 → 渲染出 "include <Name>;" 这一行
        └─ createIncludeExecuteResults(includes) → 每个 Include 产出一个 executeResult{dest: Name, data: Content}
        │
        ▼  executeConfigTemplates 按 dest 分桶合并
  每个 include 成为一个独立 .conf 文件，落入 includes/（或对应上下文目录）
```

`shared.Include` 是贯穿始终的载体，定义见 [internal/controller/nginx/config/shared/config.go:L36-L39](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/shared/config.go#L36-L39)：只有 `Name`（绝对路径文件名）与 `Content`（字节内容）两个字段。

#### 4.3.3 源码精读

**snippet → include。** [internal/controller/nginx/config/includes.go:L63-L69](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L63-L69) 把一个 dataplane snippet 包装成 include，文件名固定落到 `includes/` 目录：

```go
func createIncludeFromSnippet(snippet dataplane.Snippet) shared.Include {
    return shared.Include{
        Name:    includesFolder + "/" + snippet.Name + ".conf",
        Content: []byte(snippet.Contents),
    }
}
```

**批量收集 + 去重。** [internal/controller/nginx/config/includes.go:L192-L206](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L192-L206) 的 `createIncludesFromSnippets` 仅用于 main 与 http 片段（server/location 片段另有专门函数处理），并调用 `deduplicateIncludes` 去重：

```go
func createIncludesFromSnippets(snippets []dataplane.Snippet) []shared.Include {
    includes := make([]shared.Include, 0)
    for _, s := range snippets {
        includes = append(includes, createIncludeFromSnippet(s))
    }
    return deduplicateIncludes(includes)   // 以 Name 为键去重
}
```

去重逻辑见 [internal/controller/nginx/config/includes.go:L131-L148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L131-L148)，以 `Name` 为唯一键，**先到先得**——同名 include 只保留首次出现的内容。注释点明去重的现实场景：「同一段策略命中多个资源，或同一个 snippets filter 被多条路由规则引用」时会产生重复。

**policy → include。** [internal/controller/nginx/config/includes.go:L46-L61](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L46-L61) 把复合策略生成器返回的 `policies.File` 列表转成 include，同样落到 `includes/`：

```go
func createIncludesFromPolicyGenerateResult(resFiles []policies.File) []shared.Include {
    includes := make([]shared.Include, 0, len(resFiles))
    for _, file := range resFiles {
        includes = append(includes, shared.Include{
            Name:    includesFolder + "/" + file.Name,
            Content: file.Content,
        })
    }
    return includes
}
```

**include → executeResult。** 最后，[internal/controller/nginx/config/includes.go:L208-L221](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L208-L221) 把每个 include 直接拍平成一个 `executeResult`（dest 就是它的绝对路径文件名）：

```go
func createIncludeExecuteResults(includes []shared.Include) []executeResult {
    results := make([]executeResult, 0, len(includes))
    for _, inc := range includes {
        results = append(results, executeResult{dest: inc.Name, data: inc.Content})
    }
    return results
}
```

把这条链拼回 4.2 里 `executeMainConfig` 的最后两行就闭环了：模板渲染产出 `main.conf`（含若干 `include <Name>;` 行），`createIncludeExecuteResults(includes)` 再为每个 include 产出独立文件。最终这些 include 文件落进 `includes/` 目录——而 `includes/` **不在** `nginx.conf` 的四条通配 include 里，它是「数据目录」，靠 `main.conf`/`http.conf` 里那一条条具体的 `include /etc/nginx/includes/xxx.conf;` 被间接拉入。这正是 u6-l1 提到的「secrets/includes/bundles 是数据目录，不被通配 include，靠具体指令按绝对路径引用」。

> server/location 级的片段走的是另一条路：[internal/controller/nginx/config/includes.go:L18-L44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L18-L44) 的 `createIncludeExecuteResultsFromServers` 在所有 server/location 里收集 include 并**跨 server 去重**，再由 servers 执行函数一并产出。它们的 include 行出现在各 server/location 块内部，而非骨架模板里。

#### 4.3.4 代码实践

**实践目标**：验证「同一段策略/snippets 被多处引用时，只生成一个 include 文件、被多处 `include`」这一去重行为。

**操作步骤**：

1. 阅读 [internal/controller/nginx/config/includes.go:L131-L148](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L131-L148) 的 `deduplicateIncludes`，确认去重键是 `Name`、策略是「先到先得」。
2. 在仓库里检索该函数的调用方：`createIncludesFromSnippets`、`createIncludesFromLocationSnippetsFilters`、`createIncludesFromServerSnippetsFilters` 都调用了它。
3. 设想一个场景：同一个 `SnippetsFilter` 被两条 HTTPRoute 规则引用，各自会产出同名 server snippet——预测去重后最终生成几个文件。

**需要观察的现象**：无论被引用多少次，同名 include 只生成一份文件；多个引用点各自写一条 `include <同一个 Name>;`，NGINX 展开时读同一份文件。

**预期结果**：1 个 include 文件，多条 `include` 指令指向它。

**待本地验证**：可在 `internal/controller/nginx/config/` 下检索 `TestDeduplicateIncludes` 之类单测运行观察；或直接阅读 [internal/controller/nginx/config/includes.go:L18-L44](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/includes.go#L18-L44) 的 `createIncludeExecuteResultsFromServers` 注释，它已说明「跨 server/location 去重，确保每个唯一 include 只生成一个文件」。

#### 4.3.5 小练习与答案

**练习 1**：`deployment_ctx.json` 落在 `main-includes/` 目录下，为什么不会被骨架的 `include main-includes/*.conf` 拉入？

**参考答案**：因为通配模式是 `*.conf`，只匹配 `.conf` 后缀；`deployment_ctx.json` 后缀是 `.json`，不匹配。它是被 Plus 的 mgmt 块里一条具体指令 `deployment_context /etc/nginx/main-includes/deployment_ctx.json;`（见 [main_config_template.go:L32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/main_config_template.go#L32)）按绝对路径直接引用的，而非靠 include 通配拉入。同理 `conf.d/matches.json` 也不会被 `conf.d/*.conf` 命中。

**练习 2**：假设一段 snippets 同时被 3 条路由规则引用，去重后最终会生成几个 include 文件？`main.conf`（或 `http.conf`）里会出现几条 `include` 指令？

**参考答案**：生成 **1 个** include 文件（`deduplicateIncludes` 以 Name 为键先到先得）。至于 `include` 指令的条数取决于引用点：若是 main/base http 级片段，骨架模板里只渲染出引用它的那一处，通常 1 条；若是 server/location 级片段，则 3 条规则各自的 server/location 块里各出现 1 条 `include`，共 3 条指向同一个文件。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「骨架 ↔ 生成器 ↔ include」的完整对照。

**任务**：给静态 `nginx.conf` 的每一条 `include`，写出它拉入的目录、目录里会出现哪些文件、每个文件由哪个 executeFunc + 哪个模板产生，并解释这些文件为何能进入正确的 NGINX 上下文。

**建议步骤**：

1. 以 [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf) 为左列，列出 4 条 include + 它们的 NGINX 上下文。
2. 以 [internal/controller/nginx/config/generator.go:L220-L239](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L220-L239) 的 `getExecuteFuncs` 为右列，把 12 个 executeFunc 归类到 4 个目录（参考 4.1.5 的答案表）。
3. 对 main / events / base http 三个 executeFunc，进一步指出它们用的模板（`mainConfigTemplate` / `eventsConfigTemplate` / `baseHTTPTemplate`）与 dest 常量（`mainIncludesConfigFile` / `eventsIncludesConfigFile` / `httpConfigFile`）。
4. 解释「为什么 conf.d 下的 6 个 executeFunc 都能并进同一个 `http.conf`」：因为它们返回的 `executeResult.dest` 相同，被 `executeConfigTemplates` 的 `map[string][]byte` 按 dest 分桶合并（[generator.go:L182-L218](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/config/generator.go#L182-L218)）。
5. 最后补一句：哪些文件**不**被这 4 条通配 include 命中（`deployment_ctx.json`、`matches.json`、`includes/` 下的片段、`secrets/`、`bundles/`），分别靠什么被引用。

**验收标准**：你能不看资料，徒手画出「nginx.conf → 4 目录 → 文件 → executeFunc → 模板」的对照图，并说清每个生成文件落在哪个 NGINX 上下文。

## 6. 本讲小结

- `nginx.conf` 是**静态骨架**，随镜像内置、永不被生成器改写；它定义 main/events/http/stream 四大上下文，并用 **4 条 `include` 通配**把可变内容外包给生成器。
- 4 条 include 与 4 个目录常量一一对应：`main-includes`、`events-includes`、`conf.d`、`stream-conf.d`；生成器只往这些目录放 `.conf` 文件。
- 三套骨架模板各管一个上下文的地基：**main**（按需 `load_module`、`error_log`、main 片段）、**events**（`worker_connections`）、**base http**（http2、resolver、map、健康检查 server、日志、OIDC、gzip 等）。
- 模板在包加载时 `Parse` 成全局变量，运行时统一经 `helpers.MustExecuteTemplate` 渲染；渲染失败即 `panic`，视为编译期级 bug。
- **include 机制**把 snippet/policy/authz 转成独立 `.conf` 文件并去重（以 `Name` 为键、先到先得），骨架模板只渲染 `include <Name>;` 引用行；`includes/` 是「数据目录」，靠具体指令按绝对路径引用，不在 4 条通配 include 内。
- 多个 executeFunc 可写同一 dest 文件（如 6 个函数并入 `http.conf`），靠 `executeConfigTemplates` 的按 dest 分桶合并实现。

## 7. 下一步学习建议

本讲只讲了「骨架与地基」。接下来：

- **u6-l3（Servers、Upstreams 与 Locations 生成）**：下钻 `http.conf` 里由 `executeServers`/`executeUpstreams` 产出的业务块——虚拟 server、upstream（含 zone、keepalive）、location 路由匹配的生成细节。
- **u6-l4（Stream 配置：TCP/UDP/TLS Passthrough）**：下钻 `stream.conf` 的生成，看 L4 虚拟服务器、stream upstream、TLS passthrough（SNI）如何落地。
- 想理解这些生成的文件如何被推送到数据面并触发 reload，进入 **u7（数据面通信：通过 NGINX Agent 下发配置）**，重点看 Agent 如何按文件 hash 做增量下发——这与本讲「一个片段一个文件」的设计直接相关。
