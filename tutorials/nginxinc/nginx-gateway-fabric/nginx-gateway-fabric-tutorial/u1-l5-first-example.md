# 运行第一个例子：路由一个应用

## 1. 本讲目标

前面四讲我们完成了三件事：知道 NGF 是什么（u1-l1）、看清了仓库结构（u1-l2）、学会了编译和本地运行（u1-l3）、把 NGF 装进了集群并验证 `GatewayClass` 已被认领（u1-l4）。但到目前为止，NGF 还只是一个「空跑」的控制面——它没有路由任何真实流量。

本讲用一个**完整可运行的最小例子**把前面学到的概念「通电」。学完后你应该能够：

1. 用 `cafe-example` 把一条 HTTP 请求**端到端**跑通：客户端 → NGINX → 后端应用。
2. 看懂一个 Gateway API 路由示例由哪三块「拼图」组成，以及它们之间靠哪些字段互相接线。
3. 掌握一套固定的验证方法：创建资源 → 看 Conditions → 拿到 NGINX 的 IP/端口 → 用 `curl --resolve` 发请求。
4. 学会在 `examples/` 目录里**按需检索**，遇到某种用法（TLS、TCP、限流、认证等）能立刻找到对应的示例。

## 2. 前置知识

本讲默认你已经完成 u1-l4，即：

- 集群里已经有一个可用的 NGF 控制面（控制面 Pod 正常 Running）。
- `kubectl get gatewayclass nginx` 的 `nginx` GatewayClass 的 `ACCEPTED` 状态为 `True`，意味着 NGF 已经认领了它。

我们还会用到 u1-l1 建立的三个概念：

| 概念 | 通俗解释 | 在本讲中的对应 |
| --- | --- | --- |
| **GatewayClass** | 「这类 Gateway 由谁来管」的声明 | `nginx`（已由 NGF 认领） |
| **Gateway** | 一个监听端口/协议的流量入口实例 | `cafe-example` 里的 `gateway`，监听 80 端口 |
| **HTTPRoute** | 「什么请求转发给谁」的规则表 | `cafe-routes.yaml` 里的 coffee / tea 规则 |

此外，几个会用到的 Kubernetes 基础术语：

- **Deployment**：管理一组相同的 Pod（这里是我们的「后端应用」）。
- **Service**：给一组 Pod 提供一个稳定的访问名和端口，后端 Pod 怎么变，Service 名字不变。
- **`curl --resolve`**：用命令行强行把某个域名解析到指定 IP，这样我们不需要真实 DNS 就能用域名访问 NGINX。

> 小提示：本讲里的 `cafe.example.com` 是个**不存在的假域名**，完全靠 `--resolve` 在本地把它指向 NGINX 的 IP。这是测试 Gateway API 的标准技巧。

## 3. 本讲源码地图

本讲主要围绕 `examples/cafe-example/` 这个官方示例展开，辅以两份文档和一份 NGINX 配置骨架：

| 文件 | 作用 |
| --- | --- |
| [examples/cafe-example/cafe.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe.yaml) | 后端应用：coffee / tea 两个 Deployment + Service |
| [examples/cafe-example/gateway.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/gateway.yaml) | Gateway：声明一个 80 端口的 HTTP 监听器 |
| [examples/cafe-example/cafe-routes.yaml](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe-routes.yaml) | HTTPRoute：把 `/coffee`、`/tea` 分别转发到两个后端 |
| [examples/cafe-example/README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/README.md) | 官方给出的端到端操作步骤与预期输出 |
| [docs/developer/quickstart.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md) | 开发者快速上手：从编译到 kind 部署到「运行示例」 |
| [README.md](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md) | 项目入口，含 examples 的版本对应表 |
| [internal/controller/nginx/conf/nginx.conf](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf) | NGINX 数据面的配置骨架，能看到生成的配置文件落在哪 |

最后一份 `nginx.conf` 不是你「写」的配置，而是 NGF 控制面生成配置、最终被 NGINX 加载的**落点**——我们会用它来观察「NGF 到底把路由翻译成了什么」。

## 4. 核心概念与源码讲解

### 4.1 示例资源结构：cafe-example 的三块拼图

#### 4.1.1 概念说明

一个能让 NGF 真正转发流量的最小示例，必然由三块「拼图」组成，缺一不可：

1. **后端应用**（Deployment + Service）：真正处理请求的 Pod，以及一个让 NGINX 能找到它们的 Service。
2. **Gateway**：流量入口，声明「我在哪个端口、用什么协议、接受哪些主机名」。
3. **HTTPRoute**：路由规则，声明「满足什么条件的请求，转发给哪个后端」。

为什么是三块而不是一块？这是 Gateway API **声明式 + 关注点分离**的设计：

- 后端应用的开发者只关心 Deployment/Service；
- 流量入口的负责人只关心 Gateway（端口、协议、域名）；
- 路由规则的负责人只关心 HTTPRoute（路径、头、后端）。

三者各自独立演进，再通过几个**关键字段**互相「接线」。cafe-example 就是这三块拼图的标准模板，后续所有示例（TLS、gRPC、TCP、限流…）几乎都是在这个骨架上加东西。

#### 4.1.2 核心流程

三块拼图的接线关系如下（箭头表示「引用 / 指向」）：

```
HTTPRoute                    Gateway                     后端应用
-----------                  ----------                  -----------
parentRefs: ────────────►    gatewayClassName: nginx      Service: coffee / tea
  name: gateway              listeners:
  sectionName: http          - name: http          ▲
hostnames:                   protocol: HTTP        │ backendRefs:
  cafe.example.com           hostname: *.example.com  name: coffee
rules:                                              port: 80
  /coffee ──────────────────────────────────────────┘
```

接线靠三个关键字段：

- HTTPRoute 的 `parentRefs.name` 指向 **Gateway 的名字**（`gateway`），表示「这条路由挂到哪个 Gateway 上」。
- HTTPRoute 的 `parentRefs.sectionName` 指向 **Gateway listener 的名字**（`http`），表示「挂到哪个监听器上」。这个名字必须和 Gateway 里 `listeners[].name` 一致，否则接线失败。
- HTTPRoute 的 `backendRefs.name` + `port` 指向 **后端 Service**，NGF 据此把 Service 后面的 Pod 解析成 NGINX upstream。
- Gateway 的 `gatewayClassName` 指向 **GatewayClass**（`nginx`），决定「这个 Gateway 由 NGF 来实现」——这正是 u1-l4 已经确认被认领的那一项。

#### 4.1.3 源码精读

**第一块拼图——后端应用。** `cafe.yaml` 用 `---` 分隔，定义了 coffee 和 tea 两组 Deployment + Service。先看 coffee 的 Deployment：

[examples/cafe-example/cafe.yaml:L1-L19](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe.yaml#L1-L19) —— coffee Deployment，镜像 `nginxdemos/nginx-hello:plain-text` 监听容器 8080 端口。

紧跟着的 coffee Service 把容器 8080 端口暴露成 Service 的 80 端口：

[examples/cafe-example/cafe.yaml:L21-L32](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe.yaml#L21-L32) —— coffee Service，`port: 80`、`targetPort: 8080`，`selector: app: coffee`。

> 关键点：HTTPRoute 里 `backendRefs` 引用的是这个 Service 的 **名字 `coffee` 和端口 `80`**，而不是 Pod 的 IP。Service 的 `selector` 才负责找到真正的 Pod。tea 的结构和 coffee 完全一样（L34–L65）。

**第二块拼图——Gateway。** 整个 `gateway.yaml` 只有 11 行：

[examples/cafe-example/gateway.yaml:L1-L11](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/gateway.yaml#L1-L11) —— Gateway `gateway`，`gatewayClassName: nginx`，一个名为 `http` 的监听器：80 端口、HTTP 协议、主机名通配 `*.example.com`。

注意监听器的 `hostname: "*.example.com"`——它**只接受匹配 `*.example.com` 的主机名**。这就是后面「改主机名就被 404」练习的根因。

**第三块拼图——HTTPRoute。** `cafe-routes.yaml` 定义两条路由。看 coffee 路由：

[examples/cafe-example/cafe-routes.yaml:L1-L18](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe-routes.yaml#L1-L18) —— HTTPRoute `coffee`：`parentRefs` 指向 Gateway `gateway` 的 `http` 监听器；`hostnames: cafe.example.com`；规则是路径前缀 `/coffee` 转发到 Service `coffee:80`。

tea 路由（L19–L37）几乎相同，唯一区别是匹配方式：

[examples/cafe-example/cafe-routes.yaml:L30-L34](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/cafe-routes.yaml#L30-L34) —— tea 用的是 `type: Exact`，即精确匹配 `/tea`；而 coffee 用 `PathPrefix`，即前缀匹配 `/coffee`。

> 这是个很好的对比：`PathPrefix /coffee` 会匹配 `/coffee`、`/coffee/latte`、`/coffee/anything`；`Exact /tea` 只匹配 `/tea` 这一个路径。

#### 4.1.4 代码实践

**实践目标**：在动手 apply 之前，先用「读 YAML」的方式把三块拼图的接线关系理清，避免盲跑。

**操作步骤**：

1. 打开 `examples/cafe-example/` 下的三个文件。
2. 用笔或注释标出这三条「线」：
   - HTTPRoute `parentRefs.name`（`gateway`）→ Gateway `metadata.name`（`gateway`）；
   - HTTPRoute `parentRefs.sectionName`（`http`）→ Gateway `listeners[].name`（`http`）；
   - HTTPRoute `backendRefs.name`+`port`（`coffee:80`）→ Service `metadata.name`+`spec.ports[].port`（`coffee:80`）。
3. 把 `cafe-routes.yaml` 里 tea 的 `type: Exact` 改成 `type: PathPrefix`（**仅在本地理解时改，apply 前改回**），预测：改完后访问 `/tea/123` 会怎样？

**需要观察的现象 / 预期结果**：三条接线字段完全对得上；改 `Exact`→`PathPrefix` 后，`/tea/123` 这类子路径也应被转发（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 coffee HTTPRoute 的 `sectionName: http` 改成一个不存在的名字（比如 `https`），会发生什么？

**参考答案**：该 HTTPRoute 无法挂载到 Gateway 的任何监听器，NGF 会在 HTTPRoute 的 status 里写入 `Accepted=False`（或路由无法生效的 Condition），`/coffee` 请求不会被转发。

**练习 2**：coffee 和 tea 两个后端用的是同一个镜像 `nginxdemos/nginx-hello:plain-text`，那 `curl` 时如何区分请求到底打到了 coffee 还是 tea？

**参考答案**：靠返回体里的 `Server name:` 字段（里面带 Pod 名，如 `coffee-xxx` / `tea-xxx`）。这就是 cafe README 里验证步骤的判据。

---

### 4.2 端到端验证方法：从创建到 curl

#### 4.2.1 概念说明

「示例能跑」不等于「我会验证示例」。很多初学者 apply 完资源就直接 curl，遇到 404 / 连接拒绝却不知道问题出在哪一步。本模块给出一套**固定的四步验证法**，每一步都有明确的「成功信号」和「失败信号」，排查时可以逐级回退。

这套方法的依据是 NGF 官方在 `cafe-example/README.md` 里给出的步骤——它本身就是一份经过验证的「黄金路径」，我们把它拆成可观测的检查点。

#### 4.2.2 核心流程

四步验证法的伪代码：

```
Step 1  apply 后端应用         → 成功信号: kubectl get pods 全部 Running
Step 2  apply Gateway          → 成功信号: NGF 拉起 NGINX Pod + Service；
                                  取到 NGINX 的 GW_IP / GW_PORT
Step 3  apply HTTPRoute        → 成功信号: HTTPRoute / Gateway 的 Conditions 正常
Step 4  curl --resolve 发请求  → 成功信号: 返回体里 Server name 是 coffee/tea 的 Pod
                                  失败信号: 404（主机名/路径没匹配）
```

其中 Step 2 是最容易卡住的一步：因为「创建 Gateway 会让 NGF 反过来去 provision 一个 NGINX 数据面」（这一点 u1-l4 提到 NGF 按 Gateway 管理数据面，Provisioner 的细节在后续 u9 专题讲解）。所以你必须**先拿到 NGINX Service 的 IP 和端口**，`curl` 才有目标。

#### 4.2.3 源码精读

官方 `cafe-example/README.md` 给出的就是这套黄金路径。部署后端与配置路由的步骤：

[examples/cafe-example/README.md:L12-L53](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/README.md#L12-L53) —— 依次：apply `cafe.yaml` 看 Pod Running；apply `gateway.yaml` 后 NGF 会 provision NGINX Pod 与 Service，并保存 `GW_IP`/`GW_PORT`；再 apply `cafe-routes.yaml`。

> 注意 L42–L47 强调：创建 Gateway 后要**保存 NGINX Service 的 IP 和端口到 `GW_IP`/`GW_PORT`**——这两个变量后面 `curl` 会用。

测试步骤与预期输出：

[examples/cafe-example/README.md:L55-L79](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/cafe-example/README.md#L55-L79) —— 用 `curl --resolve cafe.example.com:$GW_PORT:$GW_IP ...` 访问 `/coffee` 和 `/tea`，预期返回 `Server address` + `Server name`。

这里要重点理解 `--resolve` 的作用：它在本地把 `cafe.example.com` 解析到 `$GW_IP`，等价于「临时改了一条 DNS」。这样请求会带上 `Host: cafe.example.com` 头发到 NGINX，从而被 Gateway 监听器的主机名匹配命中。

**怎么观察 NGF 到底把路由翻译成了什么 NGINX 行为？** 看 NGINX 数据面的配置骨架：

[internal/controller/nginx/conf/nginx.conf:L31-L31](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/internal/controller/nginx/conf/nginx.conf#L31-L31) —— HTTP 配置统一落在 `/etc/nginx/conf.d/*.conf`。

也就是说，控制面把 HTTPRoute 翻译成的 `server` / `location` / `upstream` 指令，最终就写在 NGINX Pod 的 `/etc/nginx/conf.d/` 下（L54 的 `include /etc/nginx/stream-conf.d/*.conf` 则对应 TCP/UDP 的 stream 上下文，留给 u6-l4）。所以验证「NGF 真的生效了」可以多一招：`kubectl exec` 进 NGINX Pod，`cat /etc/nginx/conf.d/*.conf`，亲眼看到 coffee/tea 对应的 upstream。这正是「观察 NGF 如何把路由翻译成 NGINX 行为」的落点。

#### 4.2.4 代码实践（本讲的主实践）

**实践目标**：把 cafe-example 跑通，用 `curl` 验证请求被正确转发到后端，并能用多种手段确认「是 NGF 在生效」。

**操作步骤**：

```shell
# 前置：已按 u1-l4 装好 NGF，gatewayclass nginx 已 Accepted

# Step 1：部署后端应用（coffee + tea 的 Deployment/Service）
kubectl apply -f examples/cafe-example/cafe.yaml
kubectl -n default get pods        # 预期：coffee / tea 两个 Pod Running

# Step 2：创建 Gateway（NGF 会拉起 NGINX 数据面 Pod 和 Service）
kubectl apply -f examples/cafe-example/gateway.yaml

# 取到 NGINX Service 的 IP 和端口（以 NodePort 部署为例）
export GW_IP=<节点的 IP 或 NGINX Service 的 ExternalIP>   # 待本地验证具体取值
export GW_PORT=<NGINX Service 暴露的端口>                  # 待本地验证具体取值

# Step 3：创建 HTTPRoute
kubectl apply -f examples/cafe-example/cafe-routes.yaml

# Step 4：发请求验证
curl --resolve cafe.example.com:$GW_PORT:$GW_IP http://cafe.example.com:$GW_PORT/coffee
curl --resolve cafe.example.com:$GW_PORT:$GW_IP http://cafe.example.com:$GW_PORT/tea
```

**需要观察的现象 / 预期结果**：

- Step 4 的 `/coffee` 请求返回类似 `Server address: 10.x.x.x:80` 和 `Server name: coffee-xxxxx`；`/tea` 返回 `tea-xxxxx`（具体地址与 Pod 名**待本地验证**，格式参考 README L65–L78）。
- 额外验证一（看条件）：`kubectl get gateway gateway -o yaml`，确认 listeners 的 Conditions 无报错。
- 额外验证二（看生成的 NGINX 配置）：

  ```shell
  # 找到 NGINX 数据面 Pod（运行 nginx 镜像的那个），名字待本地验证
  kubectl exec -it <nginx-pod> -n <nginx-gateway 命名空间> -- cat /etc/nginx/conf.d/*.conf
  ```

  预期能看到 `cafe.example.com` 对应的 `server` 块，以及指向 coffee/tea Service 的 `upstream`（具体文件名与内容**待本地验证**）。

**排查清单**（curl 失败时按序检查）：

| 现象 | 可能原因 | 检查 |
| --- | --- | --- |
| 连接被拒绝 | `GW_IP`/`GW_PORT` 取错，或 NGINX Pod 没 Ready | `kubectl get pods -n <nginx-gateway>` |
| `404 Not Found` | 主机名没匹配（HTTPRoute 的 hostname 与请求 Host 不一致） | 核对 `--resolve` 的域名与 HTTPRoute `hostnames` |
| `404` 且路径不对 | 路径匹配方式不匹配（如用 `Exact /tea` 却请求 `/tea/1`） | 核对 `rules[].matches[].path.type` |

#### 4.2.5 小练习与答案

**练习 1**：README 第 5 节提到，把 Gateway 监听器 hostname 改成 `bar.example.com` 后，原来的 `curl` 会返回 404。结合本模块的知识，解释为什么是 404 而不是连接错误。

**参考答案**：连接本身是通的（请求到达了 NGINX，所以不是连接拒绝）。但因为 Gateway 监听器主机名变成 `bar.example.com`，而请求的 Host 头仍是 `cafe.example.com`，NGINX 找不到匹配该主机名的 `server` 块，于是返回默认的 404。

**练习 2**：为什么不直接用 IP 访问，而一定要带 `--resolve cafe.example.com:...`？

**参考答案**：因为路由匹配同时依赖 Host 头。如果直接用 IP 访问，请求的 Host 头会是 IP 而非 `cafe.example.com`，无法命中监听器主机名匹配，会被 404。`--resolve` 让我们既用 IP 连接、又带正确的 Host 头。

---

### 4.3 examples 目录导航：按需找示例

#### 4.3.1 概念说明

`examples/` 是 NGF 最被低估的「文档」。它不是给初学者的玩具，而是**覆盖了 NGF 几乎所有用法的可运行清单**：每种 Gateway API 资源、每种 NGF 自有 CRD、每种过滤器，都有对应目录。学会在这个目录里检索，比翻长文档快得多。

cafe-example 只是入口；真正的价值在于：当你想知道「NGF 怎么做 TLS 终止 / TCP 路由 / 限流 / 跨命名空间引用」时，你能立刻定位到对应目录，照着它的三块拼图改。

#### 4.3.2 核心流程

`examples/` 下的每个子目录都是一个独立示例，结构高度统一，基本都包含：

- `README.md`：步骤说明与预期输出（黄金路径）；
- 一个或多个 `*.yaml`：后端应用、Gateway、Route、以及 NGF CRD（视示例而定）。

可按「用途」把目录分成四类来记忆：

| 类别 | 代表目录 | 解决的问题 |
| --- | --- | --- |
| **HTTP 路由基础** | `cafe-example`、`advanced-routing`、`traffic-splitting`、`cross-namespace-routing`、`externalname-service` | 最常见的 7 层路由、流量切分、跨命名空间、外部名 Service |
| **按协议路由** | `grpc-routing`、`tcp-routing`、`udp-routing`、`https-termination`、`secure-backend` | gRPC、TCP/UDP（stream 上下文）、TLS 终止、后端 TLS |
| **过滤器 / 扩展** | `http-request-header-filter`、`http-response-header-filter`、`cors-filter`、`basic-authentication`、`external-authentication`、`snippets` | 请求/响应头改写、CORS、认证、原生 NGINX 片段注入 |
| **NGF 策略 CRD** | `client-settings-policy`、`proxy-settings-policy`、`rate-limit-policy`、`upstream-settings-policy`、`waf-policy` | NGF 自有 CRD，对应 NginxProxy/各类 Policy |

> 还有 `helm/`（部署变体，u1-l4 已讲）和 `tcp-routing` / `udp-routing`（stream 上下文，对应 nginx.conf 的 `stream-conf.d`）。

#### 4.3.3 源码精读

项目 README 明确把 `examples` 列为上手路径之一，并用一张表区分「正式 release」与「edge」对应的示例版本：

[README.md:L25-L30](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L25-L30) —— Getting Started 第 3 步：「Deploy various examples」。

[README.md:L48-L51](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/README.md#L48-L51) —— 示例与文档按版本对应：生产用 latest release（当前 README 标注 2.6.5），尝鲜用 edge（main 分支）。

> 注意版本对应关系：如果你用的是某个 release 版的镜像，应对照**同一版本**的 `examples`，避免示例里用到尚未发布的特性。

开发者快速上手文档也把「运行示例」作为验证 NGF 是否正常的手段：

[docs/developer/quickstart.md:L210-L212](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/docs/developer/quickstart.md#L210-L212) —— 「Run Examples」：部署完后用 examples 验证 NGF 运行正常。

作为「按协议路由」的一个对照，可以看 `tcp-routing` 的 Gateway——它演示了 stream 上下文（TCP）的监听器，和 HTTP 的写法有明显区别：

[examples/tcp-routing/gateway.yaml:L1-L13](https://github.com/nginxinc/nginx-gateway-fabric/blob/5504d0d7de1706d1bda281281fbb0753a7b32806/examples/tcp-routing/gateway.yaml#L1-L13) —— TCP 监听器用 `protocol: TCP`，并通过 `allowedRoutes.kinds` 限定只接受 `TCPRoute`。对比 cafe 的 HTTP 监听器，能直观看出「协议不同，监听器写法不同」。

#### 4.3.4 代码实践

**实践目标**：从 `examples/` 里挑一个本讲没讲过的示例，独立完成「读结构 → 跑通 → 验证」。

**操作步骤**：

1. 在 `examples/` 下选一个目录，例如 `traffic-splitting`（流量按权重切分，是 HTTPRoute 的高级用法）。
2. 用 4.1 的方法拆解它的三块拼图：后端是什么、Gateway 监听什么、Route 规则是什么、是否多出 NGF CRD。
3. 仿照 4.2 的四步验证法把它跑起来。
4. 用 `curl` 多次发请求，观察流量是否按权重分配到不同后端（返回的 `Server name` 在不同 Pod 间分布）。

**需要观察的现象 / 预期结果**：能识别出该示例相对 cafe-example 多用了哪些字段（如 `backendRefs` 带权重）；多次 curl 看到 `Server name` 按大致比例分布（具体比例**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：如果你要做「HTTPS 后端（后端也是 HTTPS，需要信任后端 CA）」，应该参考哪个示例？

**参考答案**：`secure-backend`（结合 BackendTLSPolicy，用于后端 mTLS / 信任后端 CA）；前端 TLS 终止则参考 `https-termination`。两者的「TLS」落在链路的不同位置。

**练习 2**：`rate-limit-policy` 和 `client-settings-policy` 这类目录，对应的是 Gateway API 标准资源，还是 NGF 自有 CRD？

**参考答案**：是 NGF 自有 CRD（Policy 类，如 `ClientSettingsPolicy`），属于 u8 会讲的「策略附着（Policy Attachment）」。它们是 NGF 在 Gateway API 之上的扩展。

---

## 5. 综合实践

把本讲三块拼图和验证方法串起来，完成一个**改造任务**：

> 在 cafe-example 基础上，新增第三个后端 `juice`，并新增一条 HTTPRoute 规则，把 `/juice` 路径转发到 `juice` 后端，要求「能通过 `curl` 看到 `juice-xxxxx` 的返回」。

要求：

1. 仿照 `cafe.yaml` 里 coffee 的写法，新增 `juice` 的 Deployment + Service（镜像同样用 `nginxdemos/nginx-hello:plain-text`）。
2. 在 `cafe-routes.yaml` 里新增一条 HTTPRoute 规则（自选 `PathPrefix` 还是 `Exact`，并说明理由）。
3. 判断：Gateway 需要改吗？为什么？（提示：监听器主机名、端口、协议都没变 → 不需要改。这正是三块拼图分离的好处。）
4. 用 4.2 的四步验证法跑通，并 `curl http://cafe.example.com:$GW_PORT/juice` 验证。
5. 进阶：`kubectl exec` 进 NGINX Pod，确认 `/etc/nginx/conf.d/*.conf` 里出现了 juice 对应的 upstream（具体文件名**待本地验证**）。

完成这个任务，你就真正掌握了「从示例读懂结构、改动、验证」的完整闭环。

## 6. 本讲小结

- 一个 NGF 路由示例由**三块拼图**组成：后端应用（Deployment+Service）、Gateway（流量入口）、HTTPRoute（路由规则），靠 `gatewayClassName`、`parentRefs.name/sectionName`、`backendRefs` 互相接线。
- cafe-example 是标准模板：`sectionName` 必须匹配监听器名；`PathPrefix` vs `Exact` 决定路径匹配方式；`hostnames` 与监听器 `hostname` 通配匹配决定哪些主机名放行。
- 端到端验证有**固定四步法**：apply 后端 → apply Gateway 并取 `GW_IP`/`GW_PORT` → apply HTTPRoute → `curl --resolve` 验证；每步都有成功/失败信号，失败时按排查清单逐级回退。
- 「观察 NGF 把路由翻译成了什么」的落点是 NGINX Pod 的 `/etc/nginx/conf.d/*.conf`（HTTP）与 `/etc/nginx/stream-conf.d/*.conf`（TCP/UDP）。
- `examples/` 目录按「HTTP 路由基础 / 按协议路由 / 过滤器扩展 / NGF 策略 CRD」四类组织，是最高效的用法检索入口；示例版本要与所用 NGF 版本对应。

## 7. 下一步学习建议

本讲为止，你已经能在「运行中的 NGF」上把一个 HTTP 路由跑通。但这一切背后，控制面到底**怎么**把一个 HTTPRoute 变成 NGINX 配置，还是黑盒。从下一单元（u2）开始，我们离开 `examples/` 和部署视角，进入**源码视角**：

- **u2-l1～u2-l4**：从 CLI 入口 `cmd/gateway/main.go` 出发，看清控制面启动时如何装配、如何校验参数。
- 想顺带理解 stream 上下文（本讲提到的 `stream-conf.d`），可以预读 u6-l4（TCP/UDP/TLS passthrough 配置生成）。
- 想理解「创建 Gateway 会 provision 出 NGINX Pod」这一步，预读 u9-l1（Provisioner）。

一句话建议：本讲之后，请带着「`/coffee` 这条路由在源码里走了哪条链路」这个问题，进入 u2。
