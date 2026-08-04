# 本地 Standalone 部署快速体验

## 1. 本讲目标

AIBrix 的「正式」部署形态是 Kubernetes（见上一讲 u1-l4）。但要真正跑起来一套 K8s + GPU + CRD 的环境，门槛并不低。本讲介绍另一条「捷径」：**Standalone（单机）部署**。

它位于 `deployment/standalone/` 目录下，用一份 `docker-compose.yml` 把 AIBrix 的数据平面（网关 + 推理引擎 + 元数据服务 + Redis）编排在一台机器上，**完全不需要 Kubernetes**。

学完本讲，你应当能够：

- 说出 Standalone 模式启动了哪些服务、它们各自扮演什么角色；
- 读懂 `docker-compose.yml` 的服务编排逻辑（依赖、健康检查、profile、锚点复用）；
- 读懂 `start.sh` 启动脚本做了哪些初始化与校验；
- 理解 Standalone 与 Kubernetes 两种部署形态的本质差异与各自的适用场景，并能解释为什么 Standalone 模式下网关插件要走 `--standalone` 分支。

## 2. 前置知识

在阅读本讲前，建议你已经了解：

- **容器与 Docker Compose**：知道 `docker compose up -d` 会按一份 YAML 定义同时拉起多个容器，并用一个自定义网络把它们连起来。容器之间用「服务名」当主机名互访。
- **Envoy 与 ext_proc**（上一讲与 u1-l1 已铺垫）：AIBrix 的网关不是独立 HTTP 服务器，而是一个被 Envoy 通过 External Processing（ext_proc）gRPC 协议调用的插件。Envoy 负责接收请求，把请求交给插件做「选哪个后端」的决策，再按插件返回的目标地址转发。
- **vLLM**：一个高性能的大模型推理引擎，对外暴露 OpenAI 兼容的 HTTP 接口（`/v1/chat/completions`、`/health` 等）。Standalone 模式直接用官方 `vllm/vllm-openai` 镜像作为推理后端。
- **CRD 与 K8s 控制平面**（上一讲）：知道在 K8s 模式下，AIBrix 靠控制器、CRD、Informers 来「发现」推理 Pod。本讲会告诉你 Standalone 模式如何用一个静态 YAML 文件替代这一整套发现机制。

一个关键直觉：**Standalone 不是 K8s 部署的「阉割版」，而是把同一个数据平面（网关 + 引擎）换了一种编排方式**。网关插件的二进制是同一个，只是通过一个 `--standalone` 开关切换了「后端从哪里来」。

## 3. 本讲源码地图

本讲聚焦 `deployment/standalone/` 目录，涉及以下文件：

| 文件 | 作用 |
| --- | --- |
| [deployment/standalone/docker-compose.yml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml) | 服务编排核心：定义 redis、metadata-service、envoy、vllm、prefill/decode 引擎、gateway 六类服务 |
| [deployment/standalone/start.sh](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh) | 启动脚本：参数解析、环境检查、按模式切换配置、拉镜像、起服务、轮询健康检查 |
| [deployment/standalone/README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md) | 使用说明、架构图、环境变量表、与 K8s 的对比 |
| [deployment/standalone/.env.example](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/.env.example) | 所有可配置环境变量的模板（端口、模型、GPU、路由算法等） |
| [deployment/standalone/configs/envoy.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/envoy.yaml) | Envoy 配置：监听 80 端口、挂载 ext_proc 过滤器、定义到各后端的 cluster |
| [deployment/standalone/configs/endpoints.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints.yaml) | 简单模式的后端清单（一个模型 → 一个 vllm 端点） |
| [deployment/standalone/configs/endpoints-pd.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints-pd.yaml) | P/D 解耦模式的后端清单（一个模型 → prefill + decode 两个角色） |
| [cmd/plugins/main.go](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go) | 网关插件入口：`--standalone` 开关在这里分流，决定用文件发现还是 K8s 发现 |

## 4. 核心概念与源码讲解

### 4.1 docker-compose 服务编排

#### 4.1.1 概念说明

`docker-compose.yml` 是 Standalone 模式的「总装图纸」。它把 AIBrix 数据平面的所有组件声明成一个个 **service（服务）**，Docker Compose 会为每个服务启动一个容器，并用一个共享的桥接网络 `aibrix-network` 把它们连起来。容器之间直接用服务名（如 `vllm`、`redis`、`gateway`）当主机名通信——这正是 `endpoints.yaml` 里写 `vllm:8000` 而不是写 IP 的原因。

Standalone 模式一共有 6 类服务，对应 AIBrix 数据平面的不同角色：

| 服务 | 角色 | 对应 AIBrix 哪个子系统 |
| --- | --- | --- |
| `redis` | 共享状态存储（限流计数、会话、路由状态） | 网关的公共依赖 |
| `metadata-service` | 模型注册表与文件管理（`/v1/models`、`/v1/files`） | Python 运行时 |
| `envoy` | L7 入口代理，挂载 ext_proc 过滤器 | 网关的数据通道 |
| `gateway` | ext_proc gRPC 插件，做路由/限流/追踪 | 网关的大脑 |
| `vllm` | 单引擎推理后端（默认模式） | 推理引擎 |
| `prefill-engine` / `decode-engine` | Prefill/Decode 解耦的双引擎（P/D 模式） | 推理引擎 |

注意：**这里没有控制平面**。没有 operator、没有 CRD 控制器、没有 Webhook。Standalone 只编排「数据平面」，伸缩、LoRA 调度、分布式编排这些控制平面能力在这里不参与。这也是它「轻」的根本原因。

#### 4.1.2 核心流程

服务之间存在明确的**启动依赖（depends_on）**，形成一条健康检查驱动的启动链：

```text
redis (healthcheck: redis-cli ping)
   └─► metadata-service (healthcheck: /healthz，依赖 redis healthy)
   └─► gateway         (依赖 redis healthy，无健康检查：distroless 无 shell)
   └─► envoy           (依赖 metadata-service healthy + gateway started)
vllm / prefill / decode (推理引擎，独立启动，加载模型耗时最长)
```

可以把启动顺序形式化为一个偏序关系。设 \(S\) 为服务集合，\(H(s)\) 表示服务 \(s\) 通过健康检查，依赖关系 \(s \to t\)（\(s\) 必须先 healthy 才允许 \(t\) 视为就绪）。Envoy 作为入口，只有当其上游都就绪后，外部请求才不会被转发到尚未启动的后端。

Docker Compose 用两种机制实现这条链：

1. **`depends_on` + `condition: service_healthy`**：让一个服务等待另一个服务真正健康（而非 merely started）。
2. **`healthcheck`**：每个服务用一条命令自检（如 `redis-cli ping`、`curl /healthz`），Compose 据此判定 `service_healthy`。

此外，文件用了两个 **YAML 锚点（anchor）** 来消除重复：`x-common-gpu`（声明 GPU 资源需求）和 `x-healthcheck-defaults`（公共的 interval/timeout/retries），各服务用 `<<: *common-gpu` 合并复用。

最后还有 **profile（`profiles: ["pd"]`）** 机制：`prefill-engine` 和 `decode-engine` 带了 `profiles: ["pd"]`，默认 `docker compose up` 时**不会启动**它们；只有显式 `--profile pd` 才启用。于是同一份 YAML 通过 profile 同时描述了「单引擎」和「P/D 双引擎」两种拓扑。

#### 4.1.3 源码精读

**Redis 服务**——用官方 alpine 镜像，开启 AOF 持久化，限内存 256MB + LRU 淘汰，挂一个命名卷 `redis-data` 做持久化：

[deployment/standalone/docker-compose.yml:L32-L50](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L32-L50)

这段定义了网关做分布式限流、会话亲和、状态同步时所依赖的共享存储（与 u8-l4 Redis 状态同步、u7-l5 限流对应）。`restart: unless-stopped` 保证容器异常退出后会自愈。

**metadata-service 服务**——AIBrix 自家镜像，连 Redis，把推理引擎地址以环境变量 `INFERENCE_ENGINE_ENDPOINT: "http://vllm:8000"` 注入，对外提供模型注册与文件管理：

[deployment/standalone/docker-compose.yml:L56-L76](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L56-L76)

注意 `depends_on.redis.condition: service_healthy`——它必须等 Redis 真正能 `ping` 通才启动。

**Envoy 服务**——入口代理，把宿主机 80 端口映射到容器 80，把本地 `configs/envoy.yaml` 只读挂载进容器：

[deployment/standalone/docker-compose.yml:L82-L102](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L82-L102)

Envoy 同时 `depends_on` metadata-service（healthy）与 gateway（started）——这保证入口就绪时，元数据服务和路由插件都已在线。

**vLLM 单引擎服务（默认模式）**——这是最关键的推理后端。它请求 GPU（合并了 `*common-gpu` 锚点），把 HuggingFace 缓存目录挂进容器，启动命令里带了一长串 vLLM 参数：

[deployment/standalone/docker-compose.yml:L108-L139](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L108-L139)

几个要点：

- `CUDA_VISIBLE_DEVICES: ${VLLM_GPU:-0}` 决定用哪块 GPU；
- `command:` 用 vLLM 的命令行参数指定模型、张量并行度、最大上下文长度、GPU 显存利用率，并开启 `--enable-prefix-caching`（前缀缓存，与 u8-l1 路由对应）；
- 健康检查的 `start_period: 300s` 给了 5 分钟宽限——因为大模型加载到显存很慢；
- **注意它没有 `profiles`**，所以默认就会启动。

**P/D 解耦引擎（profile=pd）**——两个几乎镜像对称的服务，区别在 GPU 编号、显存利用率与最大并发序列数（prefill 给了较小的 `MAX_SEQS=64`，decode 给了较大的 `256`）：

[deployment/standalone/docker-compose.yml:L145-L177](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L145-L177) （prefill）

[deployment/standalone/docker-compose.yml:L179-L211](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L179-L211) （decode）

两者都带 `profiles: ["pd"]`，默认不启动。`PREFILL_GPU` 默认 0、`DECODE_GPU` 默认 1，即两块 GPU 分别承担 prefill 与 decode，对应 README 里的 PD 拓扑图。

**Gateway 插件服务**——AIBrix 的路由大脑，即 `cmd/plugins` 编译出的 `gateway-plugins` 二进制：

[deployment/standalone/docker-compose.yml:L218-L249](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L218-L249)

三个关键点：

1. `command:` 里带了 `--standalone` 和 `--endpoints-config=/etc/aibrix/endpoints.yaml`——这两个 flag 正是触发「文件发现」分支的开关（见 4.2.3）；
2. 环境变量 `AIBRIX_ROUTING_ALGORITHM` 决定路由算法（默认 `least_request`）；
3. **健康检查被故意设为 `["NONE"]`**——因为这是 distroless 镜像，没有 shell 和 curl，只能靠 Envoy 的 cluster 级 gRPC 健康检查来探活（见 `envoy.yaml` 的 `gateway_ext_proc` cluster）。

最后，**网络与卷**的定义：

[deployment/standalone/docker-compose.yml:L251-L260](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L251-L260)

所有服务挂在同一个 `aibrix-network` 桥接网络（固定子网 `172.28.0.0/16`），服务名即主机名；Redis 用命名卷 `redis-data` 持久化。

#### 4.1.4 代码实践

**实践目标**：把 `docker-compose.yml` 里「谁依赖谁」的拓扑亲手梳理一遍，验证你对服务编排的理解。

**操作步骤**：

1. 打开 [docker-compose.yml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml)。
2. 对每个服务，找到它的 `depends_on` 字段，记录它依赖哪个服务、依赖条件是 `service_healthy` 还是 `service_started`。
3. 对每个服务，找到它的 `healthcheck.test`，记录它用什么命令判定自己健康。
4. 列出 `gateway` 服务 `command:` 里的全部 flag，并标注哪些 flag 与「发现后端」相关。

**需要观察的现象（在纸面上推导）**：

- 如果 Redis 启动失败，哪些服务会因此无法启动？（应能推出 metadata-service、gateway、进而 envoy）
- 默认 `docker compose up` 时，哪几个服务**不会**启动？（应能推出 prefill-engine、decode-engine，因为它们带 `profiles: ["pd"]`）

**预期结果**：你应当得到一张包含 6 类服务、若干 `depends_on` 边的有向图，并能指出 gateway 的健康检查为何是 `["NONE"]`。

> 是否真正运行：本实践为「源码阅读型」，不需要启动容器即可完成推导。若你想在本地实跑，需具备 Docker + NVIDIA GPU 环境，模型加载耗时较长，属「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gateway` 服务的健康检查写成 `test: ["NONE"]`，而不是像 vllm 那样 `curl /health`？

**参考答案**：因为 gateway 用的是 distroless 镜像，里面没有 shell、curl 这类工具，无法执行健康检查命令。AIBrix 改由 Envoy 在 `gateway_ext_proc` cluster 上配置 gRPC 健康检查来探活，所以容器层面显式禁用了健康检查。

**练习 2**：同一份 `docker-compose.yml` 是如何同时支持「单引擎」和「P/D 双引擎」两种拓扑的？

**参考答案**：通过 Docker Compose 的 `profiles` 机制。`prefill-engine` 和 `decode-engine` 标注了 `profiles: ["pd"]`，默认 `docker compose up` 时被跳过；当用 `docker compose --profile pd up` 时才启用这两个服务，配合 `start.sh` 在 P/D 模式下停掉默认的 `vllm` 服务，从而切换拓扑。

---

### 4.2 启动脚本与配置

#### 4.2.1 概念说明

直接 `docker compose up -d` 也能跑，但 `start.sh` 在其之上加了一层「人性化」封装。它做的事情可以归纳为四类：

1. **参数解析**：支持 `--pd`（切 P/D 模式）、`--no-pull`（不拉最新镜像）、`-y`（跳过确认）、`-h`（帮助）。
2. **环境准备与校验**：检查 `.env` 是否存在（不存在则从 `.env.example` 复制）、检查 docker / docker compose / NVIDIA runtime 是否就绪、检查模型缓存目录。
3. **按模式切换配置**：根据是否 `--pd`，导出不同的 `ENDPOINTS_CONFIG` 与 `ROUTING_ALGORITHM` 环境变量，并拼出对应的 `--profile` 参数。
4. **编排 + 轮询就绪**：拉镜像、`docker compose up -d`、然后用一个 `wait_for_service` 函数轮询各服务的健康端点，直到全部就绪或超时。

理解 `start.sh` 的关键，是看清它**如何把 `--pd` 这个用户开关，翻译成 docker-compose 层面的 profile 与 gateway 层面的 endpoints 配置**。

#### 4.2.2 核心流程

`start.sh` 的主流程可以用下面的伪代码概括：

```text
解析命令行参数 (PD_MODE / PULL_IMAGES / SKIP_CONFIRM)
cd 到脚本所在目录
if 不存在 .env:
    cp .env.example .env      # 首次运行自动生成配置
    提示用户编辑关键变量 (MODEL_NAME / HF_TOKEN / GPU)
source .env                   # 把变量导入当前 shell
检查前置: docker / compose / nvidia runtime / 模型目录
打印配置摘要 (模型、GPU、端口)
if PD_MODE:
    export ENDPOINTS_CONFIG=./configs/endpoints-pd.yaml
    export ROUTING_ALGORITHM=pd
    PROFILES="--profile pd"
else:
    export ENDPOINTS_CONFIG=./configs/endpoints.yaml
    export ROUTING_ALGORITHM=${ROUTING_ALGORITHM:-least_request}
if PULL_IMAGES: docker compose $PROFILES pull
if PD_MODE: 停掉默认 vllm 服务
docker compose $PROFILES up -d
轮询等待: redis → metadata → envoy → vllm(或 prefill+decode) → gateway
打印服务状态与访问端点
```

这里有一个**重要的设计要点**：`start.sh` 通过 `export` 把 `ENDPOINTS_CONFIG` 和 `ROUTING_ALGORITHM` 设为环境变量，而这两个变量正是 `docker-compose.yml` 里 gateway 服务所引用的：

- `${ENDPOINTS_CONFIG:-./configs/endpoints.yaml}` 决定挂载哪个 endpoints 文件；
- `${ROUTING_ALGORITHM:-least_request}` 决定 `AIBRIX_ROUTING_ALGORITHM` 环境变量。

也就是说，`start.sh` 和 `docker-compose.yml` 通过**环境变量契约**解耦：脚本负责「选模式」，compose 文件负责「按模式拼参数」。这也是为什么你既可以跑 `./start.sh --pd`，也可以直接 `docker compose --profile pd up -d`——前者只是帮你把后者的参数和配置都备好了。

#### 4.2.3 源码精读

**参数解析**——标准的 `while + case` 循环，把 `--pd` 映射成 `PD_MODE=true`：

[deployment/standalone/start.sh:L30-L76](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L30-L76)

**`.env` 自动生成与导入**——首次运行时从模板复制，然后用 `set -a; source .env; set +a` 把变量导出为环境变量（`set -a` 让此后所有赋值都自动 export）：

[deployment/standalone/start.sh:L101-L124](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L101-L124)

**前置检查**——校验 docker、docker compose v2、NVIDIA Container Runtime、模型缓存目录。注意 NVIDIA runtime 检查失败时只是 `!` 警告，不 `exit`（因为理论上可以 CPU 跑小模型，虽不推荐）：

[deployment/standalone/start.sh:L130-L170](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L130-L170)

**按模式切换配置**——这是脚本的核心逻辑分支，把 `--pd` 翻译成 endpoints 文件、路由算法：

[deployment/standalone/start.sh:L212-L230](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L212-L230)

**起服务 + P/D 模式下停掉默认 vllm**——因为 P/D 模式下不希望单引擎 `vllm` 也在跑，所以显式 `stop` 并 `rm` 它（用 `2>/dev/null || true` 容错，因为默认模式时它可能本来就一起起了）：

[deployment/standalone/start.sh:L255-L265](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L255-L265)

**轮询等待就绪**——`wait_for_service` 函数用 `curl -sf` 反复探测一个 URL，直到成功或超时。注意 vLLM 的超时给到了 600 秒（10 分钟），因为模型加载很慢：

[deployment/standalone/start.sh:L276-L320](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L276-L320)

**endpoints 配置文件**——简单模式只有一个模型指向 `vllm:8000`：

[deployment/standalone/configs/endpoints.yaml:L11-L15](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints.yaml#L11-L15)

P/D 模式则把同一模型组织成一个 `roleset`，分别列出 prefill 和 decode 端点：

[deployment/standalone/configs/endpoints-pd.yaml:L14-L21](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints-pd.yaml#L14-L21)

这两个文件就是网关在 Standalone 模式下的「后端清单」——它替代了 K8s 模式下的 Informers 发现机制。

**网关入口的 `--standalone` 分支**——最后把视线拉到 Go 代码。`docker-compose.yml` 里 gateway 服务带的 `--standalone` 和 `--endpoints-config` 两个 flag，在 `cmd/plugins/main.go` 里触发分流：

[cmd/plugins/main.go:L68-L70](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L68-L70) 定义了这两个 flag；

[cmd/plugins/main.go:L85-L88](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L85-L88) 校验：standalone 模式下 `--endpoints-config` 是必填的；

[cmd/plugins/main.go:L90-L96](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L90-L96) Redis 在 standalone 模式下是**可选的**（缺失只告警，不致命），而在 K8s 模式下是必需的；

[cmd/plugins/main.go:L123-L126](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L123-L126) 真正的分流点：standalone 用 `discovery.NewStaticProvider(endpointsConfig)`（基于文件的静态发现），否则走 K8s 客户端 + Informers（动态发现）。

这一段是理解「同一份网关二进制、两种部署形态」的钥匙：**`--standalone` 切换的不是路由算法，而是「后端 Pod 从哪里被发现」**。

#### 4.2.4 代码实践

**实践目标**：追踪 `./start.sh --pd` 这一条命令，看它最终如何改变 gateway 容器看到的配置。

**操作步骤**：

1. 阅读 [start.sh 的模式切换分支](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/start.sh#L212-L230)，确认 `--pd` 会 `export` 哪两个变量、分别赋什么值。
2. 回到 [docker-compose.yml 的 gateway 服务](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/docker-compose.yml#L218-L249)，确认这两个变量分别被用于 `${ENDPOINTS_CONFIG:-...}`（挂载文件）和 `${ROUTING_ALGORITHM:-...}`（注入 `AIBRIX_ROUTING_ALGORITHM` 环境变量）。
3. 打开 [endpoints-pd.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints-pd.yaml)，确认它列出了 prefill 与 decode 两个角色的端点。
4. 打开 [cmd/plugins/main.go:L123-L126](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/cmd/plugins/main.go#L123-L126)，确认这个 endpoints 文件最终被 `discovery.NewStaticProvider` 读取。

**需要观察的现象（推导）**：

- 当 `ROUTING_ALGORITHM=pd` 时，gateway 容器的环境变量 `AIBRIX_ROUTING_ALGORITHM` 会变成什么？
- 此时挂载进容器的 endpoints 文件是哪一份？它描述的是单端点还是 prefill+decode 双角色？

**预期结果**：你应当能画出一条完整的传递链：`--pd`（命令行） → `export ENDPOINTS_CONFIG/ROUTING_ALGORITHM`（shell 变量） → `${...}` 插值（compose 文件） → 容器环境变量与挂载卷 → gateway 二进制读取（Go 代码）。

> 是否真正运行：本实践为「调用链追踪型」，纯阅读即可完成。若本地有 GPU 想实跑 `./start.sh --pd`，需两块 GPU，属「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`start.sh` 为什么在 P/D 模式下要显式 `docker compose stop vllm`？

**参考答案**：因为 `vllm` 服务没有 `profiles` 标注，默认 `docker compose up` 时它本就会被启动。即便用 `--profile pd`，Compose 也只是「额外」启用 pd 服务，并不会自动排除默认服务。如果不手动停掉 `vllm`，它会和 prefill/decode 引擎同时占用 GPU 0，造成冲突。所以脚本在 P/D 模式下先 `stop` 并 `rm` 掉 `vllm`。

**练习 2**：`cmd/plugins/main.go` 中，为什么 standalone 模式下 Redis 缺失只告警而不退出，K8s 模式下却 `klog.Fatal`？

**参考答案**：Standalone 定位是单机开发/测试，应当尽量「能跑就跑」。Redis 主要支撑分布式限流、用户认证、多副本状态同步等增强能力，缺失时这些功能降级即可（告警提示），核心的单机路由仍可工作。而 K8s 模式面向生产多副本，Redis 是多 gateway 副本协调状态的硬依赖，缺失会导致状态不一致，因此直接致命退出。

---

### 4.3 Standalone 适用场景与与 K8s 部署的差异

#### 4.3.1 概念说明

理解了「怎么部署」，还要理解「什么时候该用它」。Standalone 与 Kubernetes 两种形态不是互相替代的产品，而是面向不同场景的两种编排方式，背后跑的**数据平面（网关 + 引擎）几乎一样**。

关键差异在三个维度：

1. **有没有控制平面**：Standalone 只有数据平面，没有 operator/CRD/Webhook，因此**没有自动伸缩、没有 LoRA 调度控制器、没有分布式编排控制器**。这些都是 K8s 控制平面的职责。
2. **后端怎么被发现**：K8s 模式靠 Informers 监听带 `model.aibrix.ai/name` 标签的 Pod（动态、随伸缩变化）；Standalone 靠一个静态 `endpoints.yaml` 文件（固定，改后端要改文件并重启 gateway）。
3. **多节点与高可用**：Standalone 是单机单 Compose，无法跨节点；K8s 天然多节点、可滚动升级、可自愈。

#### 4.3.2 核心流程

README 给出了一张清晰的对比表，把两种形态的差异归纳为六个维度：

| 维度 | Docker Compose（Standalone） | Kubernetes |
| --- | --- | --- |
| 搭建复杂度 | 简单 | 复杂 |
| 多节点 | 否 | 是 |
| 自动伸缩 | 否 | 是 |
| 服务发现 | 静态（endpoints.yaml） | 动态（Informers + 标签） |
| 负载均衡 | Envoy | Gateway API |
| 适用场景 | 开发、单节点 | 生产、多节点 |

可以把「选哪种形态」建模为一个简单决策：设需求集合 \(R\)，若 \(R\) 包含「多节点」「自动伸缩」「生产高可用」中任一项，则选 K8s；否则若仅为本地开发/测试/单 GPU 体验，选 Standalone。

形式化地，定义权重 \(w_\text{multi} = \mathbf{1}[\text{需多节点}]\)、\(w_\text{scale} = \mathbf{1}[\text{需自动伸缩}]\)、\(w_\text{ha} = \mathbf{1}[\text{需生产高可用}]\)，则

\[
\text{choose K8s} \iff w_\text{multi} + w_\text{scale} + w_\text{ha} \ge 1
\]

否则选 Standalone。这不是项目代码里的公式，只是一个帮你做技术选型的直觉判断（**示例模型**，非项目实现）。

#### 4.3.3 源码精读

README 开头一句点明了 Standalone 的定位：

[deployment/standalone/README.md:L1-L3](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L1-L3) ——「Simplified single-node AIBrix deployment without Kubernetes complexity. Perfect for development, testing, and single-GPU/multi-GPU inference workloads.」

它的特性清单也呼应了「只保留数据平面」的事实：

[deployment/standalone/README.md:L5-L12](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L5-L12)

注意这里**没有**列出自动伸缩、LoRA 高密度调度、分布式编排等控制平面特性——因为这些在 Standalone 下不参与。

两种模式各自的架构图，把「Envoy 是唯一入口、其他服务并列其后」的拓扑画得很清楚：

[deployment/standalone/README.md:L65-L85](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L65-L85) （Simple Mode）

[deployment/standalone/README.md:L87-L107](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L87-L107) （P/D Mode）

正式的对比表与「生产请用 Helm」的建议：

[deployment/standalone/README.md:L270-L284](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L270-L284)

README 还提供了一条**完全脱离 Docker 的本地开发路径**——用 mock vLLM server + 本地编译的 gateway-plugins + 本地 Envoy，专门给「想调试网关插件但不碰 GPU」的开发者。注意它要求往 `/etc/hosts` 加 `127.0.0.1 gateway vllm metadata-service`，因为 `envoy.yaml` 用的是 Docker 服务名作主机名：

[deployment/standalone/README.md:L286-L358](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L286-L358)

这一段把 Standalone 的「开发友好」属性发挥到极致——连 Docker 都可以不要。

#### 4.3.4 代码实践

**实践目标**：判断几种典型需求应该选 Standalone 还是 K8s，并给出依据。

**操作步骤**：

1. 阅读 [README 的对比表](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/README.md#L270-L284)。
2. 针对下面三个场景，分别给出选择与一句理由：
   - 场景 A：在你的笔记本上试用 AIBrix，跑一个 1.5B 小模型，验证 `/v1/chat/completions` 能通。
   - 场景 B：公司要在生产环境部署，要求根据 QPS 自动扩缩推理副本。
   - 场景 C：你想给 AIBrix 的网关贡献一个新的路由算法，需要快速调试 ext_proc 逻辑，手头没有 GPU。

**需要观察的现象（思考）**：

- 场景 A 和 C 都不需要 K8s，但选的具体子路径不同（A 用 docker-compose，C 用 README 里的「无 Docker 本地开发」路径）。
- 场景 B 必须用 K8s + Helm，因为自动伸缩是控制平面能力，Standalone 没有。

**预期结果**：

| 场景 | 选择 | 理由 |
| --- | --- | --- |
| A | Standalone（docker-compose 简单模式） | 单机体验，无需伸缩/多节点 |
| B | Kubernetes + Helm chart | 需要自动伸缩，属控制平面能力 |
| C | Standalone 的「无 Docker 本地开发」路径 | 调试网关插件，mock server 替代真 GPU |

> 是否真正运行：本实践为「选型判断型」，无需运行任何命令。

#### 4.3.5 小练习与答案

**练习 1**：Standalone 模式下，如果想让 gateway 路由到两个不同的 vLLM 引擎（而不是一个），最少要改哪些地方？

**参考答案**：改 [endpoints.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/endpoints.yaml)，在同一个模型的 `endpoints` 列表下再加一个端点（如 `vllm2:8000`），并在 `docker-compose.yml` 里新增一个对应的 vLLM 服务容器（服务名与 endpoints 里一致）。然后重启 gateway 容器让它重新读取文件。注意：这是静态发现，新增/下线后端都要改文件，不像 K8s 那样自动感知。

**练习 2**：为什么 Standalone 模式仍然保留了 Redis？去掉它行不行？

**参考答案**：Redis 在网关里承担分布式限流、API Key 认证、会话亲和、多副本状态同步等职责（对应 u7-l5、u8-l3、u8-l4）。单机单 gateway 副本时，去掉 Redis 这些增强功能会降级（`cmd/plugins/main.go` 里会告警「rate limiting and user auth disabled」），但基础路由仍能工作。所以技术上「能去掉」，但会失去限流与认证能力，不推荐。

---

## 5. 综合实践

**综合任务**：为 Standalone 模式绘制一份完整的「请求生命周期 + 服务依赖」总图，把本讲三个最小模块的知识串起来。

具体要求：

1. **画服务依赖图**：以 `redis / metadata-service / gateway / envoy / vllm` 为节点，用箭头标出 `depends_on` 关系（标注条件是 `service_healthy` 还是 `service_started`）。
2. **画一次 `/v1/chat/completions` 请求的数据流**：从 Client → Envoy（80）→ ext_proc 过滤器 → gateway 插件（50052，做路由决策）→ 返回 `target-pod` → Envoy 转发到 `vllm_backend` cluster（vllm:8000）→ 响应回流。可参考 [envoy.yaml 的 ext_proc 配置](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/envoy.yaml#L159-L178) 与 [路由表](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/envoy.yaml#L62-L100)。
3. **标注配置传递链**：在图旁写出 `start.sh` 的 `--pd` 是如何一路传到 gateway 二进制的 `discovery.NewStaticProvider` 的（参考 4.2.4 的追踪）。
4. **写一句选型结论**：根据这张图，说明 Standalone 缺失了 K8s 模式下的哪一类能力（提示：控制平面），并指出这会影响哪些 AIBrix 特性（自动伸缩、LoRA 调度、分布式编排）。

**验收标准**：

- 依赖图中能正确反映「envoy 等 metadata-service healthy + gateway started」；
- 数据流图中能指出「路由决策发生在 ext_proc 阶段、由 gateway 插件完成」；
- 配置链中能写出至少三个传递环节（命令行 → shell export → compose 插值 → 容器环境变量/挂载 → Go 代码读取）；
- 选型结论能联系到本课程后续单元（u3 自动伸缩、u4 模型适配、u5 分布式编排）。

> 是否真正运行：本任务为「文档绘制 + 源码追踪型」，不要求启动集群。若你想真正发一个请求验证数据流，可在具备 GPU 的机器上 `./start.sh` 后用 README 里的 `curl /v1/chat/completions` 示例，属「待本地验证」。

## 6. 本讲小结

- Standalone 部署位于 `deployment/standalone/`，用一份 `docker-compose.yml` 把 AIBrix 的**数据平面**（redis、metadata-service、envoy、gateway、vllm / prefill+decode）编排在一台机器上，**不需要 Kubernetes**。
- 它**只编排数据平面，不含控制平面**——因此没有自动伸缩、LoRA 调度控制器、分布式编排控制器，这些是 K8s 控制平面的职责。
- 服务之间存在 `depends_on` + `healthcheck` 驱动的启动链；同一份 YAML 用 `profiles: ["pd"]` 同时描述「单引擎」和「P/D 双引擎」两种拓扑。
- `start.sh` 在 compose 之上加了参数解析、环境校验、按模式切换配置、轮询就绪四层封装；它通过 `export ENDPOINTS_CONFIG/ROUTING_ALGORITHM` 与 compose 文件达成环境变量契约。
- 网关二进制是同一个，靠 `cmd/plugins/main.go` 里的 `--standalone` 开关分流：Standalone 走 `discovery.NewStaticProvider`（文件发现），K8s 走 Informers（动态发现）；且 Standalone 下 Redis 可选、K8s 下必需。
- 选型上：本地开发/测试/单 GPU 体验用 Standalone；需要多节点、自动伸缩、生产高可用时用 Kubernetes + Helm。

## 7. 下一步学习建议

本讲让你把 AIBrix 的**数据平面**在单机上跑通（或至少在纸面上跑通）。接下来建议：

- **进入控制平面**：学 [u2-l1 控制器管理器入口与启动流程](u2-l1-controller-manager-entry.md)，理解 K8s 模式下 operator 是如何注册和启动的——这是 Standalone 故意省略的那一层。
- **理解后端发现的对偶**：本讲提到的 `discovery.NewStaticProvider`（静态文件发现）将在 [u6 Pod 发现、模型画像与输出预测](u6-l2-discovery-and-profiling.md) 中与 K8s 的动态 Informers 发现形成完整对照。
- **深入网关内部**：本讲把 gateway 当作黑盒（一个 ext_proc 插件），后续 [u7 LLM 网关核心](u7-l1-gateway-extproc-entry.md) 会打开这个黑盒，讲解 Envoy ExtProc 协议与路由决策的具体实现。
- **如需继续阅读源码**：建议先把 [envoy.yaml](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/deployment/standalone/configs/envoy.yaml) 的 listener/filter/cluster 三段读熟，它是最直观的「Envoy + ext_proc」入门样例。
