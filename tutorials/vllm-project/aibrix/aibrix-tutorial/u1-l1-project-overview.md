# AIBrix 是什么：项目定位与整体架构

## 1. 本讲目标

本讲是整个 AIBrix 学习手册的第一篇，目标是让你在「不写一行代码、不深入任何模块」的前提下，建立起对项目的全局认识。读完本讲，你应该能够：

1. 用一句话说清 AIBrix 是什么、解决什么问题。
2. 说出 AIBrix 的四大子系统（控制平面、LLM 网关、运行时边车、分布式 KV Cache）各自的职责。
3. 看懂 README 里列出的核心特性，并能把每条特性对应到某个子系统。
4. 画一张组件关系图，标注各组件之间「谁调用谁、数据往哪个方向流」。
5. 知道接下来该从哪里开始深入（后续讲义的依赖关系）。

本讲只做「地图」级别的讲解，刻意不展开任何实现细节——那些留给后续单元。

## 2. 前置知识

本讲面向零基础读者，但有几个名词最好先有个印象，后面遇到不会卡住：

- **大语言模型（LLM）推理**：把一个训练好的模型（如 Llama、DeepSeek、Qwen）部署成服务，让它对输入的提示词（prompt）逐字生成回答。这个过程既吃显存（要加载几十上百 GB 的权重），又吃算力。
- **KV Cache**：推理时，模型会把已经算过的「键值对」缓存在显存里，避免重复计算。它是推理性能和显存占用的关键，AIBrix 有专门针对它的优化。
- **Kubernetes（K8s）**：一个容器编排系统，负责把一堆容器（Pod）调度到机器集群上运行、监控、扩缩容。AIBrix 是「云原生」的，意味着它把 K8s 当作运行地基。
- **自定义资源（CRD）**：K8s 允许你定义自己的资源类型（就像内置的 Deployment、Service 一样），写一个「控制器」去监听并处理它们。这是 AIBrix 控制平面的核心机制。
- **网关（Gateway）**：位于客户端和后端推理服务之间的一层，负责把请求转发到合适的后端，相当于「前台接待 + 调度员」。
- **边车（Sidecar）**：和主业务容器跑在同一个 Pod 里的辅助容器，帮主容器做监控、下载、管理等杂活。

后面遇到不熟悉的术语，本讲都会顺手解释。

## 3. 本讲源码地图

本讲涉及的「源码」以项目级文档和配置为主（因为是概览篇，不读实现）。下表给出每个文件的作用：

| 路径 | 作用 |
| --- | --- |
| [README.md](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/README.md) | 项目的门面文档：定位、核心特性、架构图、安装命令。本讲最重要的信息来源。 |
| [PROJECT](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/PROJECT) | kubebuilder 脚手架自动生成的项目元数据，记录了域名、API 分组和所有 CRD 类型。看清「控制平面管哪些资源」的关键。 |
| [docs/source/designs/architecture.rst](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/designs/architecture.rst) | 官方架构设计文档，把组件划分成「控制平面」和「数据平面」两部分。 |
| [docs/source/features/](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/features) | 各特性的使用文档目录（网关、运行时、KV Cache、自动伸缩等）。本讲会引用其中几篇。 |

> 说明：本讲不绑定任何具体 `.go` / `.py` 源码文件。从下一篇 `u1-l2` 开始，我们才会进入目录结构和真实代码。

---

## 4. 核心概念与源码讲解

### 4.1 项目定位与设计目标

#### 4.1.1 概念说明

先把最根本的问题回答清楚：**AIBrix 到底是什么？**

官方一句话定义（中文意译）是：

> AIBrix 是一个开源项目，旨在提供构建可扩展 GenAI（生成式 AI）推理基础设施所需的基础构件（building blocks）。它交付一套云原生方案，专门针对企业需求来部署、管理和伸缩大语言模型（LLM）推理。

拆开来看，有几个关键词：

- **「构建块 / building blocks」**：AIBrix 不是「一个」单一软件，而是一组可以按需组合的组件。你可以只用网关，也可以只用 KV Cache，甚至可以把全套都用上。
- **「云原生」**：它以 Kubernetes 为运行地基，用 CRD + 控制器的模式管理推理工作负载。
- **「企业需求」**：强调成本（用便宜的异构 GPU）、稳定性（GPU 故障检测、SLO 保障）和多租户（限流、用户隔离）。
- **「LLM 推理」**：注意是「推理（serving）」而不是「训练」。它的目标是把训练好的模型高效地对外提供服务。

#### 4.1.2 核心流程

从「用户视角」看，一个典型的 AIBrix 部署包含三步：

1. **安装基础设施**：把 AIBrix 的依赖、CRD、控制器、网关等装进 K8s 集群。
2. **声明推理工作负载**：提交一个模型部署（或 CR），控制器自动创建对应的 Service、HTTPRoute，并把运行时边车注入进去。
3. **接收请求**：外部请求打到网关，网关根据路由策略挑一个合适的推理 Pod，把请求转过去；运行时边车负责指标采集和模型下载等杂活。

这三步背后分别站着「控制平面」「数据平面」「运行时」三股力量，我们下一节会展开。

#### 4.1.3 源码精读

项目定位直接写在 README 的开头：

[README.md:1-3](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/README.md#L1-L3) —— 这三行给出了 AIBrix 最权威的一句话定位：开源、提供构建可扩展 GenAI 推理基础设施的基础构件、云原生、面向企业。

> 关键词：**open-source**（开源）、**building blocks**（基础构件，而非单一产品）、**cloud-native**（云原生 / K8s 原生）、**LLM inference**（大模型推理）。

而安装方式（Quick Start）也印证了「云原生 + 可组合」这两个特征。README 给出了两套安装命令：nightly（直接从仓库 `config/` 目录用 kustomize 安装）和 stable（从 release 下载现成 YAML）：

[README.md:51-64](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/README.md#L51-L64) —— 注意它把安装拆成了三步：先装「依赖」、再装「CRD」、最后装「组件」。这种分层（dependency → crd → components）正是云原生项目典型的做法，下一篇 `u1-l4` 会专门讲为什么 CRD 要和 operator 分开装。

#### 4.1.4 代码实践

> **实践目标**：用自己的话复述 AIBrix 的定位，避免照抄原文。

操作步骤：

1. 打开 [README.md:1-3](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/README.md#L1-L3)。
2. 用 100 字以内的中文，向一个完全不懂技术的朋友解释「AIBrix 是干嘛的」（提示：可以类比「它像一个专门给 AI 大模型服务的运维管家」）。
3. 对照你写的话，检查是否覆盖了三个要点：① 开源云原生；② 管的是「推理」而不是「训练」；③ 由多个可组合的组件构成。

需要观察的现象 / 预期结果：

- 你应该能在不依赖任何术语的情况下把这三点说清楚。如果卡在某个术语上，说明那个术语值得在第 2 节再复习一遍。

（本实践为阅读理解型，无需运行任何命令，也「待本地验证」只针对你的理解是否到位。）

#### 4.1.5 小练习与答案

**练习 1**：AIBrix 管的是模型「训练」还是「推理」？为什么这一点很重要？

> **参考答案**：管的是**推理（serving）**——把已训练好的模型高效地对外提供生成服务。这一点重要，因为推理和训练对资源的需求完全不同：推理更关心延迟、吞吐、显存里的 KV Cache、请求路由和按需扩缩容，而这些恰恰是 AIBrix 优化的对象。

**练习 2**：README 说 AIBrix 提供「building blocks（基础构件）」，而不是「一个成品」。请举一个组合使用的例子。

> **参考答案**：例如某个团队只想要 AIBrix 的「分布式 KV Cache」能力，它可以作为**独立组件**单独使用，不必安装整个 AIBrix 栈（KV Cache 文档明确写了这一点）。又或者只用网关做请求路由。这就是「可组合」的含义。

---

### 4.2 四大子系统架构总览

#### 4.2.1 概念说明

AIBrix 体量很大，一上来全看会迷路。最有效的办法是先把项目拆成几个**相对独立**的子系统，单独理解，再看它们怎么协作。

结合代码语言和职责，AIBrix 可以清晰地分成**四大子系统**：

| 子系统 | 实现语言 | 代码所在位置 | 一句话职责 |
| --- | --- | --- | --- |
| **① 控制平面（Control Plane）** | Go | `cmd/controllers/`、`pkg/controller/`、`pkg/webhook/`、`api/` | 用 CRD + 控制器管理模型部署、自动伸缩、LoRA、分布式编排等，是整个系统的「大脑」。 |
| **② LLM 网关（Gateway）** | Go | `cmd/plugins/`、`pkg/plugins/gateway/` | 基于 Envoy 的请求入口，做请求路由、限流、状态同步，是「前台接待 + 调度员」。 |
| **③ 运行时边车（AI Runtime）** | Python | `python/aibrix/aibrix/runtime/` 等 | 跑在每个推理 Pod 旁边的辅助容器，做指标标准化、模型下载、引擎生命周期管理。 |
| **④ 分布式 KV Cache** | Python + C++/CUDA | `python/aibrix_kvcache/` | 提供跨节点、跨引擎的 KV Cache 复用（L1/L2 两级缓存），减少重复计算。 |

官方架构文档把前两个 Go 子系统归为「控制平面 / 数据平面」两个层面，后两个 Python 子系统则是被它们驱动的「执行体」。我们沿用这套划分，并补充代码组织视角。

#### 4.2.2 核心流程

四个子系统的协作关系可以用下面这张「文字流程图」表示（箭头方向 = 调用 / 数据流方向）：

```
            ┌──────────────────────────── 控制平面（Go） ────────────────────────────┐
            │  控制器管理器 (aibrix-controller-manager)                                  │
            │   ├─ PodAutoscaler 控制器      ──→ 伸缩推理副本数                         │
            │   ├─ ModelAdapter 控制器       ──→ 管理 LoRA 适配器                       │
            │   ├─ RayClusterFleet/ReplicaSet ──→ 分布式推理编排                        │
            │   ├─ StormService/RoleSet/PodSet ──→ Prefill/Decode 拓扑编排             │
            │   └─ Webhook                   ──→ 注入运行时边车、校验 CR               │
            └────────────────────────────────────────────────────────────────────────┘
                                       │ 创建/删除工作负载、注入边车
                                       ▼
   外部请求 ──▶ ┌──────── 网关（Go，Envoy ExtProc）────────┐    Redis（状态同步/限流）
               │  aibrix-gateway-plugins                    │ ◀──────────────▶
               │   ├─ 路由算法（least-load/prefix-cache/pd…）│
               │   ├─ 限流 / 排队（RPM/TPM、simple/slo）     │
               │   └─ 选择 target-pod，转发请求              │
               └──────────────────┬─────────────────────────┘
                                  │ HTTP 转发到选中的 Pod
                                  ▼
            ┌──── 推理 Pod ─────────────────────────────────────────────┐
            │  vLLM / SGLang 等推理引擎容器（真正算模型）                  │
            │  + aibrix-runtime 边车（Python） ◀── 控制平面激活/下载模型   │
            │     ├─ 指标采集与标准化  ──▶ 暴露给 网关 & 伸缩器           │
            │     └─ 模型下载（HF/S3/TOS）                              │
            └───────────────────────────────────────────────────────────┘
                                  │ 跨节点复用 KV Cache
                                  ▼
            ┌──── 分布式 KV Cache（Python + CUDA）──────────────────────┐
            │  L1 本地缓存  ◀──未命中──▶  L2 远程缓存（跨引擎/跨节点）    │
            └───────────────────────────────────────────────────────────┘
```

要点解读：

- **控制平面**不直接处理用户请求，它通过创建/删除工作负载、注入边车、配置路由来「摆好棋局」。
- **网关**是用户请求的唯一入口，它和 **Redis** 双向通信，用来在多个网关副本之间同步状态、做分布式限流。
- **运行时边车**和推理引擎跑在同一个 Pod 里，向上把指标暴露给网关和伸缩器，向下帮控制平面下载模型、管理引擎生命周期。
- **分布式 KV Cache** 是一个相对独立的优化层，让多个推理 Pod 能复用已经算好的 KV，省显存、省算力。

#### 4.2.3 源码精读

官方架构文档明确把组件分成「控制平面」和「数据平面」两个层面：

[docs/source/designs/architecture.rst:16](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/designs/architecture.rst#L16) —— 这一句是理解全局的钥匙：「AIBrix 包含**控制平面**组件和**数据平面**组件。控制平面负责模型元数据注册、自动伸缩、模型适配器注册、策略执行；数据平面提供可配置的请求分发、调度与服务组件。」

控制平面具体包含哪些组件：

[docs/source/designs/architecture.rst:18-29](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/designs/architecture.rst#L18-L29) —— 列出了 6 个控制平面组件：Model Adapter(LoRA) 控制器、RayClusterFleet、LLM 专用自动伸缩、GPU Optimizer、AI Engine Runtime、加速器故障诊断工具。

数据平面包含哪些组件：

[docs/source/designs/architecture.rst:31-37](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/designs/architecture.rst#L31-L37) —— 列出了 2 个数据平面组件：Request Router（请求路由器，执行公平策略、TPM/RPM 限流、工作负载隔离）和 Distributed KV Cache Runtime（跨节点低延迟缓存）。

> 注意这里有个细节：架构文档把「AI Engine Runtime」归到控制平面（作为一种管理组件），而我们按「代码语言 + 进程」把它独立成「运行时边车」子系统。两种分法不矛盾——架构文档按「管理层 / 执行层」分，我们额外按「代码组织」分。学代码时，按进程和代码包来理解会更顺手。

控制平面「管哪些资源」最权威的清单在 `PROJECT` 文件里（这是 kubebuilder 脚手架生成的元数据）：

[PROJECT:13-89](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/PROJECT#L13-L89) —— 这一段 `resources` 列表枚举了 AIBrix 定义的全部 CRD：`PodAutoscaler`（autoscaling 组）、`ModelAdapter`（model 组），以及 `orchestration` 组下的 `RayClusterReplicaSet`、`RayClusterFleet`、`KVCache`、`StormService`、`RoleSet`。每一项都标注了是否启用控制器、是否带 webhook。这是后续单元「CRD 数据模型」「控制器框架」的入口清单。

而运行时边车的职责，在 runtime 特性文档里说得很直白：

[docs/source/features/runtime.rst:145-158](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/features/runtime.rst#L145-L158) —— 讲「指标标准化」：不同推理引擎暴露的指标各不相同，运行时边车会把它们统一成一套标准指标，方便网关和自动伸缩器消费。这就是「运行时」作为「执行体」向上提供数据的核心职责。

#### 4.2.4 代码实践

> **实践目标**：把「四大子系统」和「真实运行的 Pod」对上号。

操作步骤：

1. 阅读网关特性文档中给出的「集群 Pod 清单」示例：
   [docs/source/features/gateway-plugins.rst:432-438](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/features/gateway-plugins.rst#L432-L438)
2. 这段示例列出了 `aibrix-system` 命名空间下运行的全部 Pod。
3. 把每个 Pod 归类到四大子系统中的某一个（或「外部依赖」）。

需要观察的现象 / 预期结果：

预期归类如下（建议你先自己填，再对答案）：

| Pod | 归属 |
| --- | --- |
| `aibrix-controller-manager` | ① 控制平面 |
| `aibrix-gateway-plugins` | ② LLM 网关 |
| `aibrix-gpu-optimizer` | ① 控制平面（GPU Optimizer 组件） |
| `aibrix-kuberay-operator` | 外部依赖（KubeRay，用于分布式推理编排） |
| `aibrix-metadata-service` | ① 控制平面（模型元数据服务） |
| `aibrix-redis-master` | 外部依赖（Redis，供网关做状态同步/限流） |

注意：③运行时边车和④KV Cache 不会以独立 Pod 出现在 `aibrix-system`——运行时边车是**注入到每个推理 Pod 里**的 sidecar 容器，KV Cache 则是另一个部署形态。这点正好印证了「按进程看」和「按组件看」的差异。

（本实践为文档阅读型，预期结果如上；如果你有本地集群，可以执行 `kubectl get pods -n aibrix-system` 实际对照，否则「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：用户发起的推理请求，是先经过「控制平面」还是「网关」？

> **参考答案**：先经过**网关**。控制平面不直接处理用户请求，它只在后台管理工作负载（创建 Pod、注入边车、配置路由）。用户请求 → 网关（路由、限流）→ 推理 Pod（含运行时边车 + 推理引擎）。

**练习 2**：运行时边车（AI Runtime）和推理引擎是什么关系？为什么需要它？

> **参考答案**：运行时边车是和推理引擎跑在**同一个 Pod**里的**辅助容器**。推理引擎（如 vLLM）只懂「算模型」，而「采集并标准化指标、从 HF/S3/TOS 下载模型权重、对外暴露统一的管理 API」这些杂活交给边车。这样引擎可以保持专注，控制平面也只需对接一套统一的边车接口，而不必适配每种引擎的差异。

**练习 3**：网关为什么需要和 Redis 双向通信？

> **参考答案**：因为网关通常会水平扩容出多个副本（多副本才能扛住高流量）。多个副本之间需要看到**一致**的状态——比如某个 Pod 当前在途请求数、某个用户的限流计数、会话亲和（session-affinity）的绑定关系。Redis 就是用来在多副本间共享和同步这些状态的。这正是后续「Redis 状态同步」讲义要解决的问题。

---

### 4.3 核心特性矩阵

#### 4.3.1 概念说明

README 在「Key Features」里列出了 8 条核心特性。初学者容易把它们看成「一锅粥」，其实每条特性都**主要落在某个子系统**上。把它们整理成「特性 → 子系统 → 说明」的矩阵，是建立全局认知最快的方法。

#### 4.3.2 核心流程

特性与子系统的对应关系（核心特性矩阵）：

| 核心特性（README 原文意译） | 主要归属子系统 | 解决的问题 |
| --- | --- | --- |
| 高密度 LoRA 管理（High-Density LoRA Management） | ① 控制平面（ModelAdapter 控制器） | 一个 Pod 上挂载多个 LoRA 适配器，省显存、提密度。 |
| LLM 网关与路由（LLM Gateway and Routing） | ② LLM 网关 | 把请求分发到合适的模型/副本，支持十几种路由策略。 |
| LLM 应用级自动伸缩（LLM App-Tailored Autoscaler） | ① 控制平面（PodAutoscaler） | 基于 KV Cache 利用率、推理感知指标做秒级伸缩。 |
| 统一 AI 运行时（Unified AI Runtime） | ③ 运行时边车 | 指标标准化、模型下载、引擎管理的统一 sidecar。 |
| 分布式推理（Distributed Inference） | ① 控制平面（RayClusterFleet 等） | 跨多节点编排大模型推理。 |
| 分布式 KV Cache（Distributed KV Cache） | ④ 分布式 KV Cache | 跨节点、跨引擎复用 KV Cache。 |
| 成本高效的异构服务（Cost-efficient Heterogeneous Serving） | ① 控制平面（GPU Optimizer） | 混合不同型号 GPU 推理以降本，同时保 SLO。 |
| GPU 硬件故障检测（GPU Hardware Failure Detection） | ① 控制平面（加速器诊断工具） | 主动探测 GPU 硬件问题，提升容错。 |

#### 4.3.3 源码精读

这 8 条特性的原始清单：

[README.md:33-40](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/README.md#L33-L40) —— README「Key Features」小节，列出了全部 8 条特性。本节上面的矩阵就是把它们逐一归类后的结果。

其中「LLM 网关与路由」这条特性，对应的「路由策略」非常丰富，是网关子系统最大的看点。网关特性文档把它们分成了几大类：

[docs/source/features/gateway-plugins.rst:154-189](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/features/gateway-plugins.rst#L154-L189) —— 列出了通用负载均衡（random / least-request / least-latency / throughput / power-of-two…）、KV-cache 感知（prefix-cache / prefix-cache-preble）、公平性（vtc-basic）、SLO 感知（slo 系列）、以及专门的 prefill-decode 解耦（pd）等路由策略。

> 这一段在告诉我们：网关不只是「随机转发」，它对 LLM 的工作负载特性（KV Cache、token 公平性、SLO、Prefill/Decode 分离）有深入理解。这也是为什么后续有整整两个单元（单元 7、单元 8）专门讲路由。

而「自动伸缩」这条特性的核心卖点，是「秒级、推理感知」，而不是 Kubernetes 原生 HPA 那种分钟级的 CPU/内存伸缩：

[docs/source/designs/architecture.rst:25](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/designs/architecture.rst#L25) —— 描述 LLM 专用自动伸缩「利用 KV Cache 利用率和推理感知指标，实时、秒级地动态优化资源分配」。

#### 4.3.4 代码实践

> **实践目标**：把「特性」和「文档位置」连起来，为后续学习建立索引。

操作步骤：

1. 打开特性文档目录：[docs/source/features/](https://github.com/vllm-project/aibrix/blob/774f93f88ba3fd942489c7d2ec9db415fbb90b55/docs/source/features)
2. 该目录下每个 `.rst` 文件对应一个特性主题，例如：
   - `gateway-plugins.rst` → 网关与路由
   - `runtime.rst` → 运行时边车
   - `kvcache-offloading.rst` → 分布式 KV Cache
   - `autoscaling/autoscaling.rst` → 自动伸缩
   - `lora-dynamic-loading.rst` → LoRA 动态加载
   - `pd-disaggregation.rst` → Prefill/Decode 解耦
3. 任选你最感兴趣的 2 条特性，打开对应文档，各读开头 20 行，记下「它解决了什么具体痛点」。

需要观察的现象 / 预期结果：

- 你会发现每个特性文档都以「它解决什么问题」开头，正好印证了 4.3.2 矩阵里「解决的问题」一列。
- 你应该能对这 2 条特性各写出一句话的痛点描述。

（本实践为目录浏览 + 阅读型，预期结果如上；无需运行命令。）

#### 4.3.5 小练习与答案

**练习 1**：「分布式推理」和「分布式 KV Cache」是同一回事吗？

> **参考答案**：不是。「分布式推理」指**把一个超大模型的计算拆到多张卡/多个节点**上协同完成（靠 RayClusterFleet 等编排）；「分布式 KV Cache」指**把已经算好的 KV Cache 在节点之间复用**，避免重复计算。前者是「算」的分布式，后者是「缓存」的分布式。

**练习 2**：自动伸缩（PodAutoscaler）和 Kubernetes 原生 HPA 有什么本质区别？

> **参考答案**：原生 HPA 通常基于 CPU/内存等通用指标，粒度粗、反应慢（分钟级）；AIBrix 的 PodAutoscaler 是**LLM 感知**的，基于 KV Cache 利用率、在途请求数、token 吞吐等推理专属指标，能做**秒级**伸缩。代价是它需要运行时边车采集并标准化这些指标。

**练习 3**：把「prefix-cache 路由」这条策略，归类到核心特性矩阵的哪一行？

> **参考答案**：归到「LLM 网关与路由」。它属于网关子系统里「KV-cache 感知」类路由——优先把请求发给已经持有该 prompt 前缀 KV Cache 的 Pod，以提高缓存命中、减少重复计算。

---

## 5. 综合实践

> **综合任务**：用自己的话画出 AIBrix 的组件关系图，标注每个组件的职责与通信方向。

这是本讲唯一一个「必须动手」的任务，它把前面三个模块的知识串起来。

### 操作步骤

1. 准备一张纸（或任意画图工具）。
2. 画出以下节点（每个写上职责）：
   - **客户端（Client）**
   - **控制平面**（含控制器管理器、Webhook、GPU Optimizer、元数据服务）
   - **LLM 网关**（aibrix-gateway-plugins）
   - **Redis**
   - **推理 Pod**（含推理引擎容器 + 运行时边车容器）
   - **分布式 KV Cache**（L1 本地 / L2 远程）
   - **外部依赖**：KubeRay、模型存储（HuggingFace/S3/TOS）
3. 用带方向的箭头连接它们，每条箭头标注「数据/调用内容」。至少包含：
   - 客户端 → 网关（推理请求）
   - 网关 ↔ Redis（状态同步、限流计数）
   - 网关 → 推理 Pod（路由后转发）
   - 控制平面 → 推理 Pod（创建工作负载、注入边车、激活/下载模型）
   - 运行时边车 → 网关 / 伸缩器（标准化指标）
   - 推理 Pod ↔ 分布式 KV Cache（KV 复用）
   - 运行时边车 → 模型存储（下载权重）
4. 在图上用四种颜色（或四种边框）区分四大子系统。

### 需要观察的现象 / 预期结果

- 你应该得到一张类似本讲 4.2.2 节「文字流程图」的结构图。
- 检查清单（全部能答「是」即达标）：
  - [ ] 图里能看出「控制平面不直接处理用户请求」？
  - [ ] 网关是用户请求的唯一入口？
  - [ ] Redis 同时和网关关联（用于状态同步/限流）？
  - [ ] 运行时边车画在了推理 Pod **内部**（作为 sidecar）？
  - [ ] 分布式推理（KubeRay）和分布式 KV Cache 被画成了两个不同的东西？

如果你卡在某条连接上，回到 4.2.2 的流程图对照即可。本任务为理解型，无标准运行命令；图本身的正确性由上面的检查清单验证（「待本地验证」仅指你是否能看着自己的图回答这些检查项）。

---

## 6. 本讲小结

- **AIBrix 是什么**：开源、云原生的 GenAI 推理基础设施，提供一组可组合的「构建块」，面向企业需求部署、管理、伸缩 LLM 推理。
- **四大子系统**：① Go 控制平面（控制器 + CRD + Webhook）、② Go LLM 网关（Envoy ExtProc + 路由 + 限流）、③ Python 运行时边车（指标标准化 + 模型下载 + 引擎管理）、④ Python + CUDA 分布式 KV Cache（L1/L2 两级缓存）。
- **控制平面 vs 数据平面**：控制平面在后台管理工作负载（摆棋局），数据平面（网关）是用户请求的唯一入口（前台接待 + 调度）。
- **运行时边车**是和推理引擎同 Pod 的辅助容器，向上暴露标准化指标，向下帮控制平面下载模型、管理引擎。
- **8 条核心特性**都能一一对应到某个子系统，把它们整理成矩阵是建立全局认知的关键。
- **安装分三步**（dependency → crd → components），CRD 与 operator 分离是云原生项目的典型做法（下一篇详解）。

## 7. 下一步学习建议

本讲建立了「地图」，接下来要走进「地形」。建议按以下顺序继续：

1. **下一篇 `u1-l2`（仓库目录结构与多语言代码组织）**：把本讲的「四大子系统」落实到真实的目录结构上，搞清 `cmd/`、`pkg/`、`api/`、`python/` 各装了什么。这是读任何代码前的必修课。
2. **随后 `u1-l3`（构建系统与 Makefile）** 和 **`u1-l4`（K8s 部署与 CRD 安装）**：动手把项目跑起来。
3. 在入门单元（单元 1、单元 2）走完之前，**不要**急着跳到路由算法或 KV Cache 实现——先把控制器框架和 CRD 模型（单元 2）弄懂，那是整个 Go 侧代码的共同骨架。
4. 如果你想先获得感性体验，可以跳读 `u1-l5`（Standalone 本地部署），用 docker-compose 脱离 K8s 快速跑一遍。

记住本讲的「四大子系统 + 核心特性矩阵」这张地图——后续每一篇讲义，你都可以拿它来定位「我现在在哪、接下来去哪」。
