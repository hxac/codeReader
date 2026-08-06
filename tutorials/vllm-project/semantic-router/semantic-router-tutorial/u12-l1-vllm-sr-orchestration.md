# vllm-sr 容器与运行时编排

## 1. 本讲目标

u1-l4 已经给出了 `vllm-sr` CLI 的三层结构（命令层 → 后端抽象层 → 生命周期层），并指出 `serve` 的业务大脑是 `_execute_serve`、唯一的环境分叉点是后端工厂 `_build_backend`。本讲把镜头推近到这套抽象的**实现细节**，回答四个问题：

1. CLI 是如何把零散的容器操作收拢成一处可调用的「桶（barrel）」的？
2. 同一套 `serve / stop / status / logs / dashboard` 命令，凭什么能同时驱动本地 Docker 和远端 Kubernetes？
3. 一次 `vllm-sr serve` 究竟按什么顺序、用什么命名规则拉起 Envoy、router、dashboard、可观测性、fleet-sim 这一整栈容器？
4. 这套栈的生命周期（启动等待、就绪探活、清理回滚）是如何被管理的？

学完后你应当能够：读懂 `core.start_vllm_sr` 的编排脉络、说清 `RuntimeStackLayout` 的命名与端口方案、并能解释 `DeploymentBackend` Protocol 如何让 Docker 与 K8s 两个后端互换。

## 2. 前置知识

- **容器与容器运行时**：本讲的「本地栈」由 Docker（或 Podman）容器组成，CLI 本质是在拼装 `docker run` 命令。你需要知道镜像（image）、容器（container）、网络（network）、端口映射（host:container）这几个概念。
- **Protocol（结构化类型）**：Python 的 `typing.Protocol` 定义的是「鸭子类型接口」——只要某个类的实例实现了协议要求的全部方法，就算实现了该协议，无需 `class Foo(Protocol)` 显式继承。本讲的 `DeploymentBackend` 就是一个 Protocol。
- **桶模块（barrel / facade module）**：一个 `.py` 文件本身不含逻辑，只做 `from X import Y` 然后 `__all__ = [...]`，把分散在多个子模块里的符号重新汇聚成单一导入入口。它是「门面（facade）」的常见实现手法。
- **Sidecar（边车）模式**：把一个辅助进程作为独立容器与主服务并列运行、共享网络与卷。本讲里 Jaeger/Prometheus/Grafana、fleet-sim、Redis/Postgres/Milvus 都是相对 router 的边车。
- **Envoy ExtProc 与 split 拓扑**：回顾 u4-l3，SR 通过 Envoy 的 External Processor（ExtProc）gRPC 接入数据面。本讲的「split」拓扑指 router 与 Envoy 各自跑在独立容器里、靠 Docker 网络互通，而非塞进同一个容器。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/vllm-sr/cli/container_cli.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_cli.py) | 容器操作的**桶模块**：再导出镜像、运行时、服务、启动四组函数 |
| [src/vllm-sr/cli/core.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py) | **生命周期层**：`start_vllm_sr / stop_vllm_sr / show_status / show_logs` 的编排内核 |
| [src/vllm-sr/cli/runtime_stack.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py) | **栈布局**：`RuntimeStackLayout` 派生命名与端口，支持栈名与端口偏移 |
| [src/vllm-sr/cli/k8s_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py) | **K8s 后端**：`DeploymentBackend` 的 Helm/kubectl 实现 |
| [src/vllm-sr/cli/deployment_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/deployment_backend.py) | **后端协议**：`DeploymentBackend` Protocol 与目标解析 |
| [src/vllm-sr/cli/container_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_backend.py) | **Docker 后端**：`DeploymentBackend` 的本地容器实现，薄封装 core |
| [src/vllm-sr/cli/container_start.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py) | 真正拼装并执行 router/envoy/dashboard 三条 `run` 命令 |
| [src/vllm-sr/cli/runtime_lifecycle.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py) | 启动横幅、就绪等待、可观测性/fleet-sim 边车、运行时摘要 |
| [src/vllm-sr/cli/runtime_topology.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_topology.py) | 拓扑模式解析（当前仅 `split`） |
| [src/vllm-sr/cli/commands/runtime.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/commands/runtime.py) | 命令层：`serve/status/logs/stop/dashboard` 与后端工厂 `_build_backend` |

## 4. 核心概念与源码讲解

### 4.1 容器 barrel：container_cli.py 的桶式再导出

#### 4.1.1 概念说明

随着 SR 的本地编排能力增长，容器操作函数逐渐散落到多个文件：管镜像的、管运行时（docker/podman 探测）的、管各类服务（建网络、起 jaeger/grafana/redis/postgres…）的、管整体启动的。如果让上层（`core.py`、`runtime_lifecycle.py`）各自去这些子模块里挑函数，会产生两个坏味道：导入路径四散、子模块边界被频繁穿透。

解法是引入一个**桶模块（barrel）** `container_cli.py`：它自己几乎不写逻辑，只做两件事——

1. 从各个子模块 `import` 需要对外暴露的函数；
2. 用 `__all__` 声明白名单。

这样上层只需 `from cli.container_cli import ...` 一行，就能拿到全部容器操作，桶把「哪些函数是公开 API」这件事集中到一处来治理。桶本身是无状态的，调用最终都落到原子子模块。

#### 4.1.2 核心流程

桶的四组来源与用途：

```
container_images      → get_container_image / get_fleet_sim_container_image  (解析/拉取镜像)
container_runtime     → container_image_exists / container_pull_image /
                        get_container_runtime / resolve path                 (运行时探测)
container_services    → container_create_network / container_exec / logs /
                        start_jaeger/prometheus/grafana/postgres/redis/fleet_sim /
                        stop / remove / status / load_openclaw_registry       (单个服务原子操作)
container_start       → container_start_vllm_sr                              (整体 run 编排)
```

桶把这四组汇成一个统一导入面，`__all__` 列出 25 个公开符号。

#### 4.1.3 源码精读

桶的全部内容就是导入与导出声明——这正是 barrel 的标志：[container_cli.py:1-29](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_cli.py#L1-L29) 把四个子模块的函数 `import` 进来，没有任何函数体。

随后用 `__all__` 固定公开白名单：[container_cli.py:31-56](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_cli.py#L31-L56)。注意 `__all__` 同时起到两个作用：声明「这些是稳定 API」，以及让 `from cli.container_cli import *` 只导入这些名字。任何不在 `__all__` 里的子模块内部函数都被视为私有。

`core.py` 顶部就是这个桶的典型消费者，一行导入拿到 11 个函数：[core.py:7-17](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L7-L17)。`runtime_lifecycle.py` 也走同一入口：[runtime_lifecycle.py:15-30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L15-L30)。

> 小结：桶不是「逻辑层」，而是「API 治理层」。它把易变的内部拆分（images/runtime/services/start）与稳定的调用契约（container_cli 这个名字）解耦——日后即便再把 services 拆成 services_net / services_obs，上层导入也无需改动。

#### 4.1.4 代码实践

**实践目标**：体会 barrel 把分散符号收拢的效果。

**操作步骤**：

1. 打开 `container_cli.py`，把 `__all__` 中任意一个名字（例如 `container_start_prometheus`，注意它确实在列表里）在 `__all__` 中临时划掉（仅本地阅读，不改文件提交）。
2. 在仓库内全局搜索 `container_start_prometheus` 的定义与调用方。
3. 观察它的**定义**在 `container_services.py`，而**调用方**全部经由 `container_cli` 或 `runtime_lifecycle`（它本身也走桶）。

**需要观察的现象**：定义点只有一个，但调用点的导入路径高度统一，都指向桶而非定义文件。

**预期结果**：你会确认「桶 = 单一导入入口」这一契约；若某天维护者把 `container_start_prometheus` 改名，只需同步桶与定义文件，调用方零改动（只要桶对外名字不变）。**待本地验证**：实际搜索结果取决于当前仓库快照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `container_cli.py` 里没有 `def` 出现任何函数？
**答案**：因为它是桶模块，职责是「再导出」而非「实现」。所有函数体都在 `container_images / container_runtime / container_services / container_start` 子模块里，桶只负责聚合公开符号与治理 `__all__` 白名单。

**练习 2**：如果新增一个存储后端（如 Qdrant）需要本地容器，按 barrel 模式应该改哪几处？
**答案**：在 `container_services.py` 新增 `container_start_qdrant`（原子操作）；在 `container_cli.py` 把它 `import` 进来并加入 `__all__`；再在 `storage_backends.py` 的供给逻辑里加一个分支调用它。上层编排（`core.py`）无需感知新名字。

---

### 4.2 后端抽象：DeploymentBackend Protocol 与 Docker/K8s 双实现

#### 4.2.1 概念说明

`vllm-sr` 的运行时类命令（`serve / stop / status / logs / dashboard`）需要在两种截然不同的目标上工作：

- **本地 Docker**：直接 `docker run` 起一组容器，靠 Docker 网络互通；
- **远端 Kubernetes**：用 `helm upgrade --install` 部署一个 Helm release，靠 Service/Ingress 暴露。

这两者的实现机制几乎没有交集，但用户面对的命令语义应当一致——`vllm-sr stop` 不论目标是 docker 还是 k8s，都该把栈拆掉。SR 用一个 `DeploymentBackend` **Protocol** 来定义这套共同契约：任何后端只要实现 `deploy / teardown / logs / status / get_dashboard_url / is_running` 六个方法，就能被同一套命令层驱动。命令层只认协议、不认实现，这就是 u1-l4 提到的「后端抽象层」。

#### 4.2.2 核心流程

```
用户命令 serve/status/logs/stop/dashboard
        │  apply_container_runtime_override(runtime)   # 仅 docker/podman 选择
        ▼
_build_backend(target, ...)          ← 唯一的环境分叉点（工厂）
        │
        ├── target == "k8s"  → K8sBackend(...)         （Helm + kubectl）
        └── target == "docker"(默认) → ContainerBackend()   （薄封装 core）
        │
        ▼
backend.deploy(...) / status(...) / logs(...) / teardown() / is_running() / get_dashboard_url()
```

工厂 `_build_backend` 是 u1-l4 强调的「唯一环境分叉点」：它把 `--target` 字符串解析后，惰性导入对应后端类并实例化。两条分支此后再无 `if target == ...` 出现——所有差异都封装在两个后端类内部。

#### 4.2.3 源码精读

**协议定义**：`DeploymentBackend` 是一个 Protocol，列出了六个方法签名：[deployment_backend.py:8-42](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/deployment_backend.py#L8-L42)。注意 `deploy` 用 `**kwargs: Any` 兜底，使 Docker 后端能接收 `source_config_file / runtime_config_file` 等 K8s 后端用不到的额外参数而不报错。同文件还定义了合法目标与默认值：[deployment_backend.py:45-46](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/deployment_backend.py#L45-L46)（`VALID_TARGETS = ("docker", "k8s")`，默认 docker），以及校验函数 [deployment_backend.py:49-62](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/deployment_backend.py#L49-L62)。

**工厂（环境分叉点）**：`_build_backend` 用 `resolve_target` 归一化字符串后，按值惰性导入并实例化：[commands/runtime.py:60-70](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/commands/runtime.py#L60-L70)。`# noqa: PLC0415` 注释表明「导入放在函数内是有意为之」——避免在用户只跑 docker 时无谓地加载 Helm/kubectl 相关依赖，也让 K8s 缺失的依赖不至于在启动期就崩。

**命令层如何复用同一模板**：以 `status` 为例，它先选后端再调 `backend.status(service)`：[commands/runtime.py:329-331](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/commands/runtime.py#L329-L331)。`logs`、`stop`、`dashboard`、`serve` 全部遵循「应用 runtime override → `_build_backend` → 调协议方法」的同款骨架，差异只在传给 `deploy` 的参数集合。

**Docker 实现（薄封装 core）**：`ContainerBackend` 把六个协议方法几乎一对一地委托给 `core` 模块的函数——`deploy` 委托 `start_vllm_sr`、`teardown` 委托 `stop_vllm_sr`、`logs/status` 委托 `show_logs/show_status`，`get_dashboard_url/is_running` 则查容器状态：[container_backend.py:15-74](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_backend.py#L15-L74)。它本身没有状态，是 core 的「协议适配器」。

**K8s 实现（Helm + kubectl）**：`K8sBackend.deploy` 走的是完全不同的路径——把 config.yaml 翻译成 Helm values、写文件，再 `helm upgrade --install ... --wait`：[k8s_backend.py:52-108](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py#L52-L108)。它有两个 Docker 后端没有的细节：

- **敏感环境变量落 Secret**：`_sync_env_secret` 把 `PASSTHROUGH_ENV_RULES` 中标记为敏感的变量写进一个 K8s Secret（`vllm-sr-env-secrets`），而非直接塞进 values：[k8s_backend.py:211-243](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py#L211-L243)。这是 K8s 的安全姿态——密钥不进 values 明文。
- **等 Pod 就绪**：`helm --wait` 之外再显式 `kubectl wait --for=condition=ready pod ...`：[k8s_backend.py:295-308](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py#L295-L308)。

#### 4.2.4 代码实践

**实践目标**：验证「同一命令、两个后端」的对称性。

**操作步骤**：

1. 在 [commands/runtime.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/commands/runtime.py) 中并排阅读 `serve`、`status`、`logs`、`stop`、`dashboard` 五个命令函数体。
2. 对每个命令，标记出三段固定结构：① `apply_container_runtime_override(runtime)`；② `_build_backend(target, ...)`；③ `backend.<method>(...)`。
3. 在 [k8s_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py) 与 [container_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_backend.py) 中分别找到这五个方法，对比实现差异。

**需要观察的现象**：五个命令的「骨架」高度同构；两个后端的同名方法实现完全不同（Helm 命令 vs `core` 函数），但都满足 Protocol 签名。

**预期结果**：你会确认 Protocol + 工厂消除了命令层的 `if target` 分支。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`serve` 命令传给 `backend.deploy` 的 `source_config_file` 与 `runtime_config_file`，在 K8s 后端里被用到了吗？
**答案**：没有。`K8sBackend.deploy` 的签名只接收 `config_file`，其余通过 `**kwargs` 吞掉。这两个参数是 Docker 后端（`ContainerBackend.deploy` → `start_vllm_sr`）特有的——它需要区分「用户原始 config」与「注入了 algorithm/platform 后的运行时 config」。Protocol 用 `**kwargs: Any` 兼容了这种「一个后端多收参数」的不对称。

**练习 2**：为什么 `_build_backend` 把 `import` 放在函数内部（惰性导入），而不是模块顶部？
**答案**：为了让「只用 docker 的用户」不必安装 Helm/kubectl 相关依赖，也让缺依赖不至于在 CLI 一启动就 import 失败。`# noqa: PLC0415` 正是标记这种「故意局部导入」。这同时缩短了 `--help` 等轻量路径的启动时间。

---

### 4.3 栈编排：RuntimeStackLayout 与 start_vllm_sr 生命周期编排

#### 4.3.1 概念说明

本模块是本讲的核心：`core.start_vllm_sr` 是真正「拉起一整栈容器」的函数，而 `RuntimeStackLayout` 是这栈的「命名与端口真相源」。

**RuntimeStackLayout 解决的问题**：一栈容器要互相引用名字（Envoy 要把 ExtProc 流量发往 router 容器、dashboard 要访问 router 的 API），还要把容器内固定端口（如 ExtProc 的 50051）映射到宿主机可能冲突的端口。SR 用一个 frozen dataclass 把「栈名 + 端口偏移」推演出全部容器名、网络名、服务内 URL、宿主机 URL。只要两个环境用不同的栈名或偏移，就能在同一台机器上并排跑两套栈而互不冲突。

**start_vllm_sr 解决的问题**：把启动切成有序的若干阶段——清场 → 建网 → 供存储 → 起可观测性 → 起 fleet-sim → 起 router/envoy/dashboard → 连网 → 等就绪 → 恢复 openclaw → 打摘要。每个阶段失败都要有明确处理。

#### 4.3.2 核心流程

`start_vllm_sr` 的阶段编排（建议对照时序阅读）：

```
resolve_runtime_stack()                 # 1. 推演命名/端口
resolve_runtime_topology(topology)      #    （当前仅 split 合法）
_load_runtime_config()                  # 2. 读 config.yaml 取 listeners
log_startup_banner()                    #    打印栈名/偏移/listener
for c in runtime_container_names:
    ensure_clean_runtime_container(c)   # 3. 清场：停掉并删除同名旧容器

_prepare_runtime_network()              # 4. 建共享网络；若 pull_policy==never 则确保本地镜像在
  └─ ensure_shared_network()
  └─ ensure_runtime_images_for_pull_policy()

_start_support_services()               # 5. 起支撑边车
  ├─ provision_storage_backends()       #    按 config 供 Redis/Postgres/Milvus
  ├─ start_observability_stack()        #    Jaeger + Prometheus + Grafana（可关）
  └─ start_fleet_sim_sidecar()          #    fleet-sim（除非指向外部 URL）

_start_runtime_containers()             # 6. 起核心三件套
  └─ container_start_vllm_sr()          #    逐条 docker run router → envoy → dashboard
        失败 → _cleanup_started_containers() 回滚已起容器

connect_runtime_container()             # 7. 把三件套挂到共享网络
maybe_finish_setup_mode()               #    setup 模式提前返回
_wait_and_verify_runtime()              # 8. 等就绪 + 校验未退出
  └─ wait_for_router_health()           #    轮询 router /ready（最长 1800s）
recover_openclaw_containers()           # 9. 恢复历史 openclaw 容器
log_runtime_summary()                   # 10. 打印端点与 curl 示例
```

`stop_vllm_sr` 是镜像式的反向流程：先断开 openclaw 容器，再依次停 runtime、fleet-sim、可观测性、存储容器，最后删网络。

#### 4.3.3 源码精读

**(1) RuntimeStackLayout：派生命名与端口**

这是一个 `@dataclass(frozen=True)`，集中持有全部容器名与端口：[runtime_stack.py:33-59](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L33-L59)。`frozen=True` 让它不可变，可安全地在多处共享。

它有两类派生属性，体现「容器内视角」与「宿主机视角」的区分：

- **服务内 URL**（容器间互访，用容器名作主机）：如 `router_api_service_url = http://<router_container_name>:8080`、`envoy_admin_service_url = http://<envoy_container_name>:9901`、`fleet_sim_service_url = http://<sim>:8000`：[runtime_stack.py:73-118](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L73-L118)。这些 URL 注入到各容器的环境变量里，让它们能在 Docker 网络内找到彼此。
- **宿主机 URL**（用户访问，用 `localhost` + 偏移端口）：如 `dashboard_url = http://localhost:<dashboard_port>`、`jaeger_ui_url`、`grafana_url`：[runtime_stack.py:61-102](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L61-L102)。

**命名与端口的推演规则**集中在 `resolve_runtime_stack`：默认栈名（`vllm-sr`）走短名，自定义栈名走 `<stack>-<service>` 前缀，所有端口 = 默认端口 + `port_offset`：[runtime_stack.py:172-233](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L172-L233)。栈名归一化用正则把非法字符替成 `-` 并拒绝「清洗后全空」的输入，以免无声地退化到默认栈：[runtime_stack.py:236-253](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L236-L253)；端口偏移必须非负：[runtime_stack.py:256-262](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L256-L262)。

端口默认值定义在 `consts.py`：[consts.py:43-50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/consts.py#L43-L50)。例如 ExtProc gRPC 用 50051、router 管理 API 用 8080、metrics 用 9190、dashboard 用 8700、fleet-sim 用 8810。`runtime_container_names` 只汇总 router/envoy/dashboard 三件套：[runtime_stack.py:148-158](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L148-L158)。

**(2) start_vllm_sr：阶段编排内核**

入口签名承接巨量参数（镜像名、拓扑、pull policy 等），这些都是命令层透传下来的：[core.py:137-151](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L137-L151)。函数体第一件事就是 `resolve_runtime_stack()` + `resolve_runtime_topology(topology)`：[core.py:153-159](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L153-L159)。注意拓扑当前只有 `split` 一种合法值——也就是说 router 与 envoy 必然是两个独立容器：[runtime_topology.py:13-30](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_topology.py#L13-L30)。

清场阶段对每个 runtime 容器调用 `ensure_clean_runtime_container`，停掉并删除同名旧实例，避免 `docker run` 因名字冲突失败：[core.py:163-164](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L163-L164)。`ensure_clean_runtime_container` 的实现见 [runtime_lifecycle.py:56-64](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L56-L64)。

支撑边车由 `_start_support_services` 统一调度，按「存储 → 可观测性 → fleet-sim」顺序起，并把 fleet-sim 的服务内 URL 回写到 `env_vars`：[core.py:230-260](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L230-L260)。其中存储后端是**配置驱动**的——`detect_required_backends` 读 `global.services.<key>.store_backend`，只起 config 真正需要的 Redis/Postgres/Milvus：[storage_backends.py:17-26](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/storage_backends.py#L17-L26)。可观测性栈把 Jaeger/Prometheus/Grafana 各起一个容器，并把它们的 service URL 注入 env：[runtime_lifecycle.py:75-112](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L75-L112)。fleet-sim 边车有一个「外部 URL 短路」：若设置了 `TARGET_FLEET_SIM_URL` 就跳过本地 sidecar：[runtime_lifecycle.py:115-150](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L115-L150)。

核心三件套的启动委托给 `_start_runtime_containers` → `container_start_vllm_sr`：[core.py:263-296](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L263-L296)。`start_vllm_sr` 检查返回码，非 0 即 `SystemExit(1)`：[core.py:192-211](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L192-L211)。

**(3) container_start_vllm_sr：拼装并逐条执行 run 命令**

这是真正「docker run」的地方。它先解析平台与 nofile 限制、准备挂载路径、渲染 split Envoy 配置，再由 `_runtime_container_specs` 构造 router/envoy/(dashboard) 三条命令：[container_start.py:42-133](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L42-L133)。`minimal=True`（即 `--minimal` 或 `DISABLE_DASHBOARD`）时只起 router+envoy：[container_start.py:244-250](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L244-L250)。

关键看点是**端口映射**——它揭示了每个容器对外暴露什么。router 容器映射三组端口（ExtProc gRPC 50051、metrics 9190、管理 API 8080）：[container_start.py:289-293](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L289-L293)。envoy 容器只映射 config 里声明的 listener 端口（推理流量入口），其 9901 管理端口不暴露到宿主机、仅容器内可达：[container_start.py:327-331](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L327-L331)。dashboard 映射 8700：[container_start.py:381](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L381)。

另一个关键点是 **split 拓扑的 Envoy 配置渲染**：`_render_split_envoy_config` 临时把 `ENVOY_EXTPROC_ADDRESS` / `ENVOY_ROUTER_API_ADDRESS` 设为 router 容器名，再生成 Envoy 配置，从而让 Envoy 的 ExtProc 集群指向 router 容器——这就是「split」二字在配置层的落点：[container_start.py:530-547](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L530-L547)。

**失败回滚**：任何一条 `docker run` 失败，都会调用 `_cleanup_started_containers` 把已经起来的容器按逆序停删，保证不残留半套栈：[container_start.py:120-125](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L120-L125)，实现见 [container_start.py:594-598](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L594-L598)。

**(4) 就绪等待与摘要**

`_wait_and_verify_runtime` 先 `wait_for_router_health` 轮询 router 容器内的 `/ready`（最长 1800s，每 2s 一次，期间实时回显 router 启动日志），再校验 router/envoy/dashboard 没有中途退出：[core.py:299-307](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L299-L307)。轮询的细节——状态非 running 立即报错、每 10 次打印剩余时间、超时打印全量日志——见 [runtime_lifecycle.py:209-258](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L209-L258)。就绪等待的超时被刻意设到 30 分钟（注释说明是为了等模型加载）：[consts.py:53-54](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/consts.py#L53-L54)。

最后 `log_runtime_summary` 打印全部端点（dashboard、各 listener、metrics、fleet-sim、存储、可观测性）和一段可直接复制的 curl 示例：[runtime_lifecycle.py:328-369](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_lifecycle.py#L328-L369)。

**(5) 停栈**

`stop_vllm_sr` 的对称流程：先收集全部受管容器的状态快照（`_managed_container_statuses`），全 absent 则直接返回；否则先断开 openclaw 注册表里的容器，再依次停删 runtime、fleet-sim、可观测性、存储四组容器，最后删网络：[core.py:310-357](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L310-L357)。分组停删由 `_stop_managed_container` 统一处理「not found 直接跳过」：[core.py:419-434](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L419-L434)。

> 小结：`start_vllm_sr` 把「一堆 docker 命令」升级成「有序、可回滚、可就绪检查」的编排；`RuntimeStackLayout` 则保证这套栈的名字与端口可推演、可并排。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 `vllm-sr serve` 的完整调用链，验证「命令 → 后端 → core → 桶 → 子模块」的穿透，并量化端口偏移的效果。

**操作步骤**：

1. **源码跟踪**（无需运行环境）。从 [commands/runtime.py:269-288](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/commands/runtime.py#L269-L288) 的 `serve` 入口出发，沿 `_execute_serve` → `backend.deploy` → `ContainerBackend.deploy` → `core.start_vllm_sr` → `container_start_vllm_sr`，画出一张调用链图，在每个节点旁标注「这一步发生了什么」。
2. **命名/端口推演**。对照 [runtime_stack.py:182-233](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py#L182-L233)，手算两种情形下的容器名与端口：
   - 情形 A：默认栈（`VLLM_SR_STACK_NAME` 未设、偏移 0）。
   - 情形 B：`VLLM_SR_STACK_NAME=ci` 且 `VLLM_SR_PORT_OFFSET=1000`。
   填表：router 容器名、envoy 容器名、共享网络名、router_port、api_port、dashboard_port。
3. （可选，需本地 Docker）用情形 B 的环境变量执行 `vllm-sr serve`，再用 `docker ps --format '{{.Names}}\t{{.Ports}}'` 对照你的推演表。

**需要观察的现象**：情形 A 的 router 容器名为 `vllm-sr-router-container`、网络名为 `vllm-sr-network`、router_port=50051；情形 B 的 router 容器名为 `ci-vllm-sr-router-container`、网络名为 `ci-vllm-sr-network`、router_port=50151、api_port=8180、dashboard_port=9700。

**预期结果**：两套栈可在同一台宿主机上并存且端口不冲突，这正是栈名 + 偏移设计的目的。步骤 3 的实跑结果**待本地验证**（取决于本地是否已构建镜像、`pull_policy=never` 时是否已 `make vllm-sr-dev`）。

#### 4.3.5 小练习与答案

**练习 1**：在 split 拓扑下，Envoy 是如何「找到」router 容器去发 ExtProc gRPC 调用的？
**答案**：通过容器名。`_render_split_envoy_config` 在渲染 Envoy 配置时，把 `ENVOY_EXTPROC_ADDRESS` 临时设为 `stack_layout.router_container_name`（如 `vllm-sr-router-container`），生成的 Envoy 配置里 ExtProc 集群就以该名字为主机。由于 router/envoy 都接在同一 Docker 网络上，DNS 把容器名解析成该容器的 IP。dashboard 侧也用 `ENVOY_EXTPROC_ADDRESS` / `TARGET_ROUTER_API_URL` 等环境变量拿到 router 容器名（见 [container_start.py:403-427](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_start.py#L403-L427)）。

**练习 2**：`start_vllm_sr` 为什么先起支撑边车（存储/可观测性/fleet-sim）再起 router/envoy，而不是反过来？
**答案**：因为 router 启动后（乃至健康检查期间）可能立即需要这些边车——例如 router 的语义缓存要连 Redis、记忆要连 Postgres/Milvus、可观测性要向 Jaeger/Prometheus 推送、fleet-sim 的 URL 要作为环境变量喂给 router。先起边车并回写它们的 service URL 到 `env_vars`，再起 router，router 启动时这些下游就已经在网络上可达。反过来起会让 router 在就绪等待期反复连接失败。

**练习 3**：`start_vllm_sr` 中 router 启动失败（`_start_runtime_containers` 返回非 0）会发生什么？
**答案**：在 `container_start_vllm_sr` 内部，任意一条 `docker run` 失败会触发 `_cleanup_started_containers`，把已起容器按逆序停删并返回错误码；随后 `start_vllm_sr` 的 [core.py:209-211](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/core.py#L209-L211) 捕获非 0 返回码，记录 stderr 并 `raise SystemExit(1)` 中止。注意此回滚只清核心三件套，先前已起的边车容器不会被这次失败联动清理（需 `vllm-sr stop` 统一收尾）。

## 5. 综合实践

**任务**：你是新接手 SR 本地编排的工程师，需要为「在同一台机器上跑一套主栈 + 一套 nightly 回归栈」设计运行参数，并验证 `vllm-sr` 既能本地 Docker 跑、也能远端 K8s 跑。

请完成以下三件事（前两件为源码阅读型，第三件可选实跑）：

1. **并排栈的参数设计**。对照 [runtime_stack.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/runtime_stack.py) 与 [consts.py:43-50](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/consts.py#L43-L50)，为两套栈分别写出 `VLLM_SR_STACK_NAME` 与 `VLLM_SR_PORT_OFFSET` 的取值，确保：
   - 两套栈的容器名、网络名互不重叠；
   - 两套栈映射到宿主机的全部端口（router 50051/9190/8080、dashboard 8700、fleet-sim 8810、jaeger 16686/4318、prometheus 9090、grafana 3000、redis 6379、postgres 5432、milvus 19530、各 listener）互不冲突。
   交付一张「栈 → 环境变量 → 关键端口」对照表。

2. **后端对称性核对**。打开 [deployment_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/deployment_backend.py)、[container_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/container_backend.py)、[k8s_backend.py](https://github.com/vllm-project/semantic-router/blob/e7cbab82503022721fe31f34b8408cb1c66c40d8/src/vllm-sr/cli/k8s_backend.py)，画一张表：协议的六个方法（`deploy/teardown/logs/status/get_dashboard_url/is_running`）分别在两个后端里委托给了谁（Docker：core/容器查询；K8s：helm/kubectl 命令）。重点标注 `deploy` 在两个后端里对「敏感环境变量」处理的差异（Docker 直接注入容器 env vs K8s 落 Secret）。

3. **（可选）实跑并截图**。在一台装好 Docker 的机器上，先用默认参数 `vllm-sr serve` 起主栈，等 `log_runtime_summary` 打印端点后，用 `docker ps` 截图；再用步骤 1 设计的 nightly 参数起第二套栈，用 `docker ps` 证明两套栈共存。最后依次 `vllm-sr stop`（注意：`stop` 默认作用于默认栈，需用对应栈名环境变量分别停两套）。

> 若不具备运行环境，至少完成 1、2 两步并在每一步标注「待本地验证」。

## 6. 本讲小结

- **container_cli.py 是桶模块**：它不含逻辑，只把 `container_images / container_runtime / container_services / container_start` 四组函数再导出并用 `__all__` 治理公开 API，让上层只用一处导入入口。
- **DeploymentBackend 是统一契约**：`deploy/teardown/logs/status/get_dashboard_url/is_running` 六个方法构成 Protocol；`_build_backend` 是唯一的环境分叉点，按 `--target` 惰性实例化 `ContainerBackend`（薄封装 core）或 `K8sBackend`（Helm+kubectl），命令层此后零分支。
- **RuntimeStackLayout 是命名/端口真相源**：由「栈名 + 端口偏移」推演全部容器名、网络名、服务内 URL（容器名作主机）与宿主机 URL（localhost+偏移），使多套栈可并排。
- **start_vllm_sr 是有序编排**：清场 → 建网 → 供存储 → 起可观测性 → 起 fleet-sim → 起 router/envoy/dashboard → 连网 → 等就绪 → 恢复 openclaw → 打摘要，失败有逆序回滚，就绪靠轮询 router `/ready`（最长 30 分钟）。
- **split 拓扑是当前唯一形态**：router 与 envoy 分处独立容器，靠 `_render_split_envoy_config` 把 ExtProc 集群地址写成 router 容器名、由 Docker 网络 DNS 解析。
- **stop_vllm_sr 是镜像反向流程**：分组（runtime/fleet-sim/可观测性/存储）依次停删，`not found` 自动跳过，最后删网络。

## 7. 下一步学习建议

- **走向部署产物**：本讲的 K8s 后端最终落到 `helm upgrade --install`，对应 chart 在 `deploy/helm/semantic-router`。建议接着读 **u12-l2（Helm / Kubernetes / 本地部署）**，看 chart 模板如何把 router/envoy/dashboard 编排成 K8s 资源。
- **走向 Operator/CRD**：若你想了解 K8s 上「声明式、自动调和」的部署方式（而非 Helm 一次性 install），接着读 **u12-l3（Operator 与 CRD）**，看 `deploy/operator` 如何用 SemanticRouter CRD 驱动 reconcile。
- **回到数据面**：本讲只讲了「把容器起起来」。容器内 router 进程自己的启动序列（选项解析 → 配置加载 → Registry → API Server → 模型下载 → ExtProc 起服 → 就绪）请回看 **u4-l1（main.go 启动序列）**，它解释了为什么 `wait_for_router_health` 要等那么久。
- **延伸阅读建议**：通读 `cli/container_run_command.py`（本讲未展开），看 `build_base_run_command` 与各 `append_*` 如何把 nofile 限制、host-gateway、自定义 DNS、挂载、端口、GPU 透传等拼成一条完整 `docker run`，这是把「编排」落到「命令行」的最后一公里。
