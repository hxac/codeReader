# Dynamo 是什么：数据中心级推理编排层

## 1. 本讲目标

学完本讲，你应该能够：

1. **说清层次关系**：Dynamo 不是 SGLang / vLLM / TensorRT-LLM 的替代品，而是运行在它们**之上**的编排层（orchestration layer）。
2. **列举四大核心能力**：分离式服务（Disaggregated Prefill/Decode）、KV 感知路由（KV-Aware Routing）、KV 块管理器（KVBM）、Planner 自动扩缩。
3. **复述三面架构**：请求面（Request Plane）、控制面（Control Plane）、存储与事件面（Storage & Events Plane）各自负责什么、优化目标是什么。
4. **走通一笔请求的九步流程**（S1–S9），并知道每一步落在哪个面上。

本讲**不读一行复杂代码**，重点是建立正确的"心智地图"——后面 11 个单元的所有源码阅读，都要挂在这张地图上。

## 2. 前置知识

本讲是手册的第一篇，假设你没有读过 Dynamo 的任何代码。但下面几个概念最好先有个直觉：

| 术语 | 通俗解释 |
|------|----------|
| **推理引擎（inference engine）** | 真正"跑模型"的软件，比如 vLLM、SGLang、TensorRT-LLM。它负责在一次前向计算里把 token 变成 logits。 |
| **推理服务（serving）** | 把引擎包一层，提供 OpenAI 兼容的 HTTP API，处理并发、排队、流式输出。 |
| **prefill / decode** | 一次 LLM 推理分两段：**prefill** 是把整个输入 prompt 一次算完（计算密集，并行度高）；**decode** 是一个 token 一个 token往外蹦（访存密集，并行度低）。两者对硬件的"胃口"完全不同。 |
| **KV cache** | Transformer 推理时缓存的 Key/Value 中间结果。有了它，算第 N+1 个 token 时不必重算前 N 个。它非常大，通常以 GB 计，是推理系统最宝贵的资源。 |
| **TTFT / ITL** | Time To First Token（首 token 延迟）/ Inter-Token Latency（token 间隔延迟）。推理服务的两个核心 SLA 指标。 |
| **etcd / NATS** | 常见的分布式中间件：etcd 做键值存储与服务发现，NATS 做消息订阅。本讲只需要知道它们是"可选的基础设施"。 |
| **Rust / PyO3** | Dynamo 性能敏感部分用 Rust 写；Python 通过 PyO3 绑定调用 Rust。你不需要会写 Rust 才能读懂本讲。 |

一个值得先记住的对比：**单卡跑一个模型，用引擎就够了；跨多机协调一堆 GPU，才是 Dynamo 的主场。** README 里明确写了这句话（见下文源码精读）。

## 3. 本讲源码地图

本讲涉及的文件都在文档层，加上一个"代码锚点"用来把概念落到真实源码：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md) | 项目门面：定位、能力清单、快速开始、服务发现说明。读任何开源项目的第一站。 |
| [docs/fern/pages/developer-guide/knowledge-base/overview.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md) | 官方"总体架构"文档：提出三面架构、三个控制回路、K8s 映射。是本讲的理论核心。 |
| [docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md) | 官方请求流程图说明：把一笔分离式请求拆成 S1–S9 九步，并附 mermaid 源码。是本讲的流程核心。 |
| [lib/kv-router/src/worker_type.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs) | 代码锚点：`WorkerType` 枚举。用真实代码证明"prefill/decode 分离"不是文档口号，而是类型系统里的一等公民。 |
| [components/src/dynamo/frontend/__main__.py](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/__main__.py) | 代码锚点：`python -m dynamo.frontend` 的入口，只有 7 行。让你第一次看见"Frontend"对应的真实文件。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：README —— Dynamo 的定位与四大能力

#### 4.1.1 概念说明

README 回答的问题是：**这个项目为什么存在？**

一句话版本：**Dynamo 是开源的、数据中心规模的推理栈（datacenter-scale inference stack），它是推理引擎之上的编排层——它不取代 SGLang、TensorRT-LLM 或 vLLM，而是把它们变成一个协调的多节点推理系统。**

注意这个定位里的三层含义：

1. **不重复造轮子**：引擎层的优化（算子、CUDA graph、批调度）交给 vLLM/SGLang/TRT-LLM。
2. **解决引擎解决不了的问题**：一台机器装不下的模型、一个集群里的多副本协同、prefill 和 decode 负载不匹配、KV cache 在 GPU/CPU/SSD 之间搬移。
3. **技术栈分工**：Rust 写性能敏感的运行时，Python 写后端接入和扩展。

README 还给了一个非常重要的"反向说明"——什么时候**不该**用 Dynamo：单 GPU 跑单模型时，引擎本身就够了。这句话能帮你避免把编排层硬塞进小场景。

#### 4.1.2 核心流程

README 把 Dynamo 的能力组织成一张"Core Capabilities"表。我们抽出其中最核心的四项（本手册后面各有一个完整单元 dedicated to 它们）：

```
能力                    做什么                                      为什么重要
─────────────────────────────────────────────────────────────────────────────────
Disaggregated P/D       把 prefill 和 decode 拆成可独立扩缩的 GPU 池   提升利用率，各自跑在适合自己的硬件上
KV-Aware Routing        按 worker 负载 + KV cache 重合度选 worker     避免重复 prefill，TTFT 快 2 倍
KV Block Manager(KVBM)  KV cache 在 GPU → CPU → SSD → 远端 间换入换出  突破显存限制，扩大有效上下文
Planner                 基于 SLA 的自动扩缩器，剖析负载并合理定容       在最低 TCO 下满足延迟目标
```

四大能力之间的因果链（这是理解 Dynamo 的关键，不是四个孤立功能）：

```
长上下文 + 高并发
   └─> KV cache 放不下 ──────────────┐
                                     ├─> 需要 KVBM（多层缓存）
   └─> prefill/decode 负载形态不同 ──┼─> 需要 P/D 分离（独立扩缩）
                                     ├─> 需要 KV 感知路由（把请求送到已有缓存的 worker）
                                     └─> 需要 Planner（按 SLA 动态调整各池大小）
```

另外，README 还说明了两种**请求路由拓扑**（request routing topologies）：

- **Dynamo 原生 Frontend 路由**：请求路径是 `client → Frontend → Router → workers`，适合本地开发和单集群。
- **Gateway API 路由（GAIE）**：请求路径是 `client → Gateway → EPP → Frontend sidecar (direct) → workers`，适合把策略、认证、限流放在集群边缘的 K8s 平台。

两条路径最终都落到同一套 worker 与 KV 路由能力上，只是"入口和路由边界"不同。

#### 4.1.3 源码精读

**定位句**。[README.md:36](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L36) 这一段是整个项目的自我定义：

> The open-source, datacenter-scale inference stack. Dynamo is the orchestration layer above inference engines — it doesn't replace SGLang, TensorRT-LLM, or vLLM, it turns them into a coordinated multi-node inference system.

**什么时候用 / 不用**。[README.md:51-59](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L51-L59) 列出五条使用场景（多 GPU/多节点协调、KV 感知路由、P/D 独立扩缩、SLA 自动扩缩、快速冷启动），最后一句是关键的反向说明：

> If you're running a single model on a single GPU, your inference engine alone is probably sufficient.

**核心能力表**。[README.md:95-107](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L95-L107) 是"Core Capabilities"表，除了上面四大能力，还列出了 ModelExpress（GPU 到 GPU 权重流式加载，冷启动快 7 倍）、Grove（拓扑感知的 K8s 编排）、AIConfigurator（模拟上万种部署配置）、Fault Tolerance（金丝雀健康检查 + 在途请求迁移）。

**两种路由拓扑**。[README.md:117-131](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L117-L131) 用表格对比了 Dynamo-native 与 GAIE 两种拓扑，并在 L127-128 给出两条请求路径的精炼写法。

**服务发现的真实依赖**。[README.md:259-276](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L259-L276) 这一段常被初学者忽略但极其实用：本地开发和 Kubernetes 部署**都不需要** etcd 和 NATS（K8s 用原生 CRD + EndpointSlices 做发现；本地传 `--discovery-backend file` 即可）。表里还注明：KV 感知路由本身不依赖 NATS，可用 `--no-router-kv-events` 走基于预测的路由。

**代码锚点：Frontend 的入口只有 7 行**。[components/src/dynamo/frontend/__main__.py:4-7](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/components/src/dynamo/frontend/__main__.py#L4-L7)

```python
from dynamo.frontend.main import main

if __name__ == "__main__":
    main()
```

这说明 README 里反复出现的 "Frontend" 不是一个抽象名词，它就是 `python3 -m dynamo.frontend` 启动的那个进程，真实实现在 `components/src/dynamo/frontend/main.py`（第 5 单元精读）。第一次读大仓库时，把文档名词钉到具体文件上，是防止迷路的最有效手段。

#### 4.1.4 代码实践

**实践目标**：确认你对"Dynamo vs 推理引擎"层次关系的理解，并验证本地开发的最低依赖。

**操作步骤**：

1. 打开 [README.md:63-71](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L63-L71) 的"Feature support at a glance"表。
2. 逐行读表格，回答：三大引擎（SGLang / TensorRT-LLM / vLLM）在 KVBM 这一行的支持状态分别是什么？（有一个是 🚧）
3. 再打开 [README.md:259-276](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L259-L276) 的服务发现表，抄下"Local Development"这一行的两个 ❌，以及它要求传入的参数。
4. （可选，需要 GPU 与 Docker）按 [README.md:139-155](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L139-L155) 的 Quick Start Option A 跑通第一条请求。

**需要观察的现象**（步骤 1–3 是纯阅读型实践，无条件要求）：

- 你会发现 Dynamo 的所有能力都是"跨引擎"的——这正是编排层的价值：能力写在 Dynamo 里，三个引擎都能用。
- 本地开发不需要先装 etcd，这大大降低了第 2 讲的运行门槛。

**预期结果**：能不查资料地回答"KVBM 在 SGLang 上是什么状态""本地开发要不要 NATS"。

第 4 步若本机无 GPU：**待本地验证**（第 2 讲会用无 GPU 的 mocker 方式补上运行体验）。

#### 4.1.5 小练习与答案

**练习 1**：同事说"我们打算用 Dynamo 替换 vLLM"。这句话哪里不对？

**答案**：层次错了。Dynamo 是推理引擎**之上**的编排层，不执行模型前向计算；实际跑模型的仍是 vLLM/SGLang/TRT-LLM。正确说法是"用 Dynamo 编排多个 vLLM 实例"。（依据 [README.md:36](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L36)）

**练习 2**：为什么"KV 感知路由"能降低 TTFT？用一句话说清因果。

**答案**：如果路由器把请求发给已经缓存了该 prompt 前缀 KV 的 worker，这部分 prefill 计算就可以跳过，首 token 自然更快。（依据 [README.md:100](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L100) "Eliminates redundant prefill computation — 2x faster TTFT"）

**练习 3**：README 列出的两个请求路由拓扑，分别适合什么场景？

**答案**：Dynamo-native Frontend 路由适合本地开发、单集群、Dynamo 自己拥有请求入口的场景；GAIE（Gateway API + EPP）适合标准化于 Gateway API 的 K8s 平台，或需要把策略/认证/限流/可观测放在集群边缘的场景。（依据 [README.md:123-128](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L123-L128)）

---

### 4.2 模块二：knowledge-base overview —— 三面架构

#### 4.2.1 概念说明

`overview.md` 是官方的"Overall Architecture"文档。它开篇就给出全篇最重要的论断：Dynamo 是一个后端无关（backend-agnostic）的分布式推理运行时，**围绕三个协作的关注点构建**：

1. 一条快的**请求路径（request path）**——负责 token 生成；
2. 一条响应灵敏的**控制路径（control path）**——负责扩缩与放置；
3. 一条有韧性的**状态路径（state path）**——负责 KV 复用与故障恢复。

文档后文把它们正式命名为三个"面"（plane）：

| 面 | 英文名 | 负责什么 | 包含组件 | 优化目标 |
|----|--------|----------|----------|----------|
| 请求面 | Request Plane | 请求/响应的执行 | Frontend、Router、Prefill workers、Decode workers | 低开销、持续 token 流 |
| 控制面 | Control Plane | 期望状态管理 | Planner、Dynamo Operator、Discovery、Grove、Model Express | 正确性、向目标容量收敛 |
| 存储与事件面 | Storage & Events Plane | 缓存状态的可见性与搬移 | KV Events、KVBM、NIXL | 缓存复用、跨 worker 交接效率 |

> **命名提示**：本手册任务描述里说的"状态面"就是文档里的 **Storage & Events Plane（存储与事件面）**，因为 KV cache 的"状态"正是通过这条面传播的。

为什么一定要"分面"？文档的"Why This Architecture Exists"一节列举了现代 LLM 服务的五个反复出现的瓶颈，并明确说：**Dynamo 通过把服务、控制、状态传播拆成显式的面和控制回路来应对这些约束。**

- **prefill/decode 失衡**：流量配比一变，GPU 就闲置（引 DistServe 论文）；
- **KV 重算**：路由不看缓存重合度时，TTFT 升高、算力浪费（引 DeepSeek 论文）；
- **内存压力**：长上下文 + 高并发超出 HBM 容量，必须多层缓存管理（引 KVBM/Mooncake/LMCache 等）；
- **动态需求**：静态资源配置假设被打破（引 AzureTrace）；
- **真实故障**：Pod 重启、分区、热点过载要求一等公民的恢复行为。

注意最后一条引出的设计哲学，这句是整个架构的底色：**"这个模型假设故障是常态（routine），而不是异常（exception）。"**

#### 4.2.2 核心流程

overview.md 除了"三个面"，还定义了"三个控制回路（control loops）"，可以理解为三个不同周期的 while 循环：

```
服务回路（毫秒~秒级）
  维持 frontend / router / prefill / decode 之间的低延迟请求执行

规划回路（秒~分钟级）
  Planner 消费运行时指标
     -> 计算出 prefill/decode 目标副本数
     -> 连接层把目标应用到运行时资源

韧性回路（事件驱动，故障时触发）
  健康检查发现坏 worker
     -> Discovery 依活性摘除过期 endpoint
     -> 优雅关闭排空在途请求
     -> 请求迁移/取消控制行为
     -> 过载时负载脱落（load shedding）防止级联崩溃
```

KV 感知路由的直觉可以粗略理解为"在候选 worker 上打分再取最优"：

\[ \text{score}(w) \approx f\big(\underbrace{\text{KV 重合度}(w, \text{req})}_{\text{状态面给的情报}},\ \underbrace{\text{负载}(w)}_{\text{请求面给的情报}}\big) \]

这是一个**示意公式**，用来说明两个面如何汇合到请求面的决策点上；真实实现（含归一化、惩罚项、各策略差异）在第 6 单元 `lib/llm/src/kv_router/routing_host/builtin.rs` 里精读。

文档最后还给了 **K8s 原生实现**的映射，预告了第 10 单元：

- Dynamo Operator 调和 `DynamoGraphDeployment` CRD；
- 可发现性由 `DynamoWorkerMetadata` + EndpointSlices 派生；
- Grove 把 worker 组建模为 `PodCliqueSet` / `PodClique`；
- prefill/decode 的独立弹性用 `PodCliqueScalingGroup` 的独立 `replicas` / `min` 表达。

#### 4.2.3 源码精读

**三个关注点的开篇定义**。[overview.md:8-14](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L8-L14)

> It is backend-agnostic (SGLang, TRT-LLM, vLLM, and others) and is built around three cooperating concerns: A fast **request path** … a responsive **control path** … a resilient **state path** …

这一段同时给出"backend-agnostic"这个词——它解释了为什么三大引擎都能接入。

**五个设计目标**。[overview.md:16-24](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L16-L24)：延迟稳定、GPU 效率、计算复用、运维韧性、部署可移植。后面读任何组件的设计取舍，都可以回来对照这五条。

**为什么需要这个架构**。[overview.md:26-36](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L26-L36)：五个瓶颈与对应的论文/项目引用。

**三个面的正式定义**。[overview.md:59-90](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L59-L90)。其中请求面（L59-68）、控制面（L70-80）、存储与事件面（L82-90）各是一小节，每节末尾都有一句"optimized for …"，这是判断"某段代码属于哪个面"的判定依据。

**端到端请求叙事**。[overview.md:92-104](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L92-L104) 用 9 句话概括一笔分离式请求（与 4.3 节的 S1–S9 对应），最后两步明确提到 KV Events 更新缓存可见性、KVBM 按压力换出/召回块——这两步就属于状态面。

**三个控制回路**。[overview.md:107-133](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L107-L133)：Serving / Planning / Resilience。韧性回路里列了健康检查、活性摘除、排空、迁移/取消、负载脱落五个机制——第 12 单元的故障容忍讲义会逐个对应到源码。

**实现模型**。[overview.md:199-203](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L199-L203)：Rust 写性能敏感运行时组件，Python 写后端集成与扩展，模块化子系统边界让 routing/planning/memory/transport 独立演进。这句话预告了第 3 讲的仓库三层结构。

**代码锚点：P/D 分离在类型系统里的样子**。[lib/kv-router/src/worker_type.rs:15-23](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs#L15-L23)

```rust
/// Processing stage a single worker handles.
#[derive(Copy, Clone, Debug, Eq, PartialEq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum WorkerType {
    Prefill,
    Decode,
    Encode,
    Aggregated,
}
```

这段代码说明：一个 worker 有且只有一个角色；`Aggregated` 表示 prefill 和 decode 在同一进程里（聚合模式），`Prefill`/`Decode`/`Encode` 表示分离拓扑中的一个阶段。注意 [lib/kv-router/src/worker_type.rs:6-8](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs#L6-L8) 的注释还说明"角色与模型的公开 API 面正交"——也就是多模态的 Encode 阶段与文本 API 无关。另外 [lib/llm/src/worker_type.rs:9](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/llm/src/worker_type.rs#L9) 只是把这份"权威定义"再导出给 `dynamo-llm` 用（避免循环依赖），这也是大型 Rust workspace 常见的组织手法。

#### 4.2.4 代码实践

**实践目标**：把三面架构从"文档概念"变成你能动手使用的分类工具。

**操作步骤**：

1. 逐行抄写 [overview.md:59-90](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L59-L90) 的三段，做成一张 4 列表格：`面 / 包含组件 / 优化目标 / 我的理解（一句话）`。
2. 对表中出现的每一个组件名词，在仓库里用 Glob 找到它对应的真实目录（例如 `Planner` → `components/src/dynamo/planner/`、`Operator` → `deploy/operator/`、`KVBM` → `lib/kvbm-*`）。找得到就填路径，找不到就写"待确认"。
3. 把 `lib/kv-router/src/worker_type.rs` 加入你的表格，思考它属于哪个面（提示：它被"路由主机和 worker 选择策略"使用）。

**需要观察的现象**：第 2 步会发现大多数名词都能在仓库里找到同名或近名目录——官方文档与代码命名高度一致，这是后续读源码的巨大便利。

**预期结果**：一张约 12 行的三面分类表，其中至少 8 行填出了真实仓库路径。本实践不需要运行任何服务。

#### 4.2.5 小练习与答案

**练习 1**："Prefill worker 把 KV cache 通过 NIXL 发给 Decode worker"这一步属于哪个面？

**答案**：存储与事件面（Storage & Events Plane）。NIXL 属于该面的组件，负责 KV/数据的高速搬移。（依据 [overview.md:82-90](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L82-L90)）

**练习 2**：`WorkerType::Aggregated` 和 `WorkerType::Prefill` 的本质区别是什么？

**答案**：`Aggregated` 在**一个进程**里同时做 prefill 和 decode（聚合模式）；`Prefill` 只做分离拓扑中的 prefill 一个阶段，decode 由别的 `Decode` worker 负责。（依据 [lib/kv-router/src/worker_type.rs:6-8](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs#L6-L8)）

**练习 3**：三个控制回路中，哪个回路的触发频率最高？各自的时间尺度大概是多少？

**答案**：服务回路最高（毫秒到秒级，每笔请求都在跑）；规划回路次之（秒到分钟级，按指标周期算副本数）；韧性回路是事件驱动（只在故障时触发）。（依据 [overview.md:107-133](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/overview.md#L107-L133)）

---

### 4.3 模块三：architecture-flow —— 一笔请求的九步（S1–S9）

#### 4.3.1 概念说明

`architecture-flow.md` 是一张图的文字说明：把**分离式模式**下一笔请求的完整旅程编号为 S1–S9，并用颜色分组：

| 颜色分组 | 步骤 | 内容 |
|----------|------|------|
| 🔵 主请求流（蓝） | S1、S2、S3 | 客户端发请求 → Frontend 预处理 → 路由到 prefill |
| 🟢 Prefill 流（绿） | S4、S5 | prefill worker 计算 KV → 返回 `disaggregated_params` 元数据 |
| 🟠 Decode 路由流（橙） | S6、S7 | 路由器注入元数据并选 decode worker → NIXL 直接 GPU 到 GPU 传 KV |
| 🟣 完成流（紫） | S8、S9 | decode 生成 token → 经 Frontend 后处理（detokenize）流回客户端 |
| 🔗 基础设施（虚线） | — | 服务发现、请求面传输、NATS KV 事件、Planner 指标与扩缩 |

理解这张图有三个关键洞察：

1. **Frontend 出现了两次**（S1 进、S9 出）：它不只是网关，还做 chat template、分词（S2）和 detokenization（S9）。它是有实际计算量的组件。
2. **路由器叫 PrefillRouter，且出现了两次**（S3 选 prefill、S6 选 decode）：在分离式拓扑里，"路由"是一个持续编排的过程，不是一次性的转发决定。
3. **KV 不经过中央存储**：prefill 与 decode worker 之间是点对点直接传输（NIXL），元数据（放在 `disaggregated_params` 里）走响应链路回传。这避免了共享存储瓶颈，且传输是非阻塞的——GPU 可以在 KV 传输的同时继续跑前向。

#### 4.3.2 核心流程

把 S1–S9 加上三个面的标注，得到本讲最核心的一张伪代码流程：

```
[请求面]  S1  Client --HTTP--> Frontend (OpenAI 兼容, 端口 8000)
[请求面]  S2  Frontend 预处理：套 chat template、分词、校验
[请求面]  S3  PrefillRouter 用 KV 感知路由或负载均衡 选一个 prefill worker

[请求面]  S4  Prefill worker 对输入 token 执行 prefill 计算，产生 KV cache
[状态面]      (KV 落在 prefill worker 的 GPU 显存里)
[请求面]  S5  Prefill worker 返回 disaggregated_params（后端相关的传输元数据）

[请求面]  S6  PrefillRouter 把 prefill 结果注入 decode 请求，选一个 decode worker
[状态面]  S7  Decode worker 与 prefill worker 协调，通过 NIXL 直接 GPU→GPU 传 KV

[请求面]  S8  Decode worker 用传来的 KV 逐个生成 token
[请求面]  S9  token 流经 Frontend 做后处理（detokenization）后发给 Client

（贯穿全程的虚线）
[控制面]      Frontend --> Planner：上报指标
[控制面]      Planner --> workers：下发扩缩命令
[控制面]      所有组件 --> Discovery（K8s 原生资源 或 etcd/file）：注册与发现
[状态面]      workers --> NATS（可选）：发布 KV 事件，供路由器更新缓存可见性
```

配套的三个技术要点（来自文档"Technical Implementation Details"一节）：

- **PrefillRouter 编排**：位于 Frontend 与 worker 之间；选 prefill worker 用"缓存重合度分数 + 负载"；把传输元数据注入 decode 请求。
- **NIXL**：用 NVLink、InfiniBand/UCX 或 PCIe 做高速 GPU 到 GPU 传输；不同后端的协调方式不同——SGLang 用 bootstrap 连接、TRT-LLM 用不透明状态、vLLM 用 block ID。
- **分离式 KV cache**：每个 worker 在自己 GPU 显存里维护本地 KV；无共享存储瓶颈；非阻塞传输让 GPU 前向与 KV 传输并行。

#### 4.3.3 源码精读

**主请求流 S1–S3**。[architecture-flow.md:9-15](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L9-L15)：HTTP 客户端把 API 请求发给 Frontend（OpenAI 兼容服务器，端口 8000）；Frontend 套模板、分词、校验；PrefillRouter 用 KV 感知路由或负载均衡选 prefill worker。

**Prefill 流 S4–S5**。[architecture-flow.md:16-21](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L16-L21)：prefill worker 执行 prefill 计算生成 KV cache；返回包含后端相关传输元数据的 `disaggregated_params`。

**Decode 路由流 S6–S7**。[architecture-flow.md:22-27](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L22-L27)：PrefillRouter 把 prefill 结果注入 decode 请求并路由到 decode worker；decode worker 与 prefill worker 协调，通过 NIXL 直接 GPU 到 GPU 传输 KV cache。

**完成流 S8–S9**。[architecture-flow.md:28-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L28-L33)：decode worker 用传来的 KV 生成 token；token 流经 Frontend 做 detokenization 后交付客户端。

**基础设施连接**。[architecture-flow.md:34-51](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L34-L51)：服务发现（K8s 上用 DynamoWorkerMetadata CRD + EndpointSlices，裸机用 etcd 或文件系统）；请求面默认 TCP，可用 `DYN_REQUEST_PLANE` 换 HTTP/NATS；NATS 连接是可选的（可用 `--no-router-kv-events` 关掉）；Planner 从 Frontend 收指标、向 workers 发扩缩命令。

**技术实现细节**。[architecture-flow.md:52-67](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L52-L67)：PrefillRouter 编排、NIXL 传输、分离式 KV cache 三小节（内容见上文 4.3.2 的三个要点）。

**mermaid 图源码**。[architecture-flow.md:69-192](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L69-L192)：这是图的原始定义，可以直接复制到任何支持 mermaid 的渲染器里看效果。读源码时值得注意 L120 与 L126-127 这两条边：

```
S5 -->|disaggregated_params| PrefillRouter
S7 -->|NIXL GPU-to-GPU| PrefillKVCache
PrefillKVCache -.->|Direct Transfer| DecodeKVCache
```

实线是请求/控制信息流，虚线是数据/基础设施流——图上的线型正好对应我们标注的"面"。

#### 4.3.4 代码实践

**实践目标**：不看讲义，独立从官方文档复现 S1–S9，并能回答"每一步在哪"。

**操作步骤**：

1. 打开 [architecture-flow.md:69-192](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L69-L192) 的 mermaid 源码，只读代码不读上文的编号说明。
2. 自己从 `Client --> S1` 开始，沿着箭头手工走一遍，写下你数出来的步骤总数。
3. 对照 [architecture-flow.md:9-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L9-L33) 的官方编号，检查你是否遗漏了 S5（返回元数据）或 S7（KV 传输）——这两步最容易被忽略。
4. 思考题：如果改为**聚合模式**（`WorkerType::Aggregated`），S4–S8 中哪些步骤会消失？（答案见下面练习 3）

**需要观察的现象**：mermaid 里的 `PrefillRouter` 节点同时连着 prefill 和 decode 两侧；`Discovery`/`NATS`/`Planner` 三个节点全部用虚线连入主流程。

**预期结果**：能在白纸上默画 9 步，并准确说出 S5 传的是"元数据"而不是 KV 数据本身、S7 才是真正的数据搬运。本实践是纯阅读型，无需运行环境。

#### 4.3.5 小练习与答案

**练习 1**：S5 和 S7 都和 KV 有关，它们传的东西有什么本质区别？

**答案**：S5 返回的是 `disaggregated_params`——描述"KV 在哪、怎么拿"的**元数据**（SGLang 是 bootstrap 连接信息、TRT-LLM 是不透明状态、vLLM 是 block ID）；S7 才是 KV 数据本身的**直接 GPU 到 GPU 传输**（经 NIXL）。（依据 [architecture-flow.md:20](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L20) 与 [architecture-flow.md:26](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L26)）

**练习 2**：为什么设计成"KV 在 worker 间点对点直传"而不是写到一个共享存储？

**答案**：避免共享存储成为瓶颈；传输是 worker 到 worker 直连；非阻塞传输让 GPU 前向计算与 KV 传输并行。（依据 [architecture-flow.md:64-67](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L64-L67)）

**练习 3**：在聚合模式（`WorkerType::Aggregated`）下，九步里哪些会消失或合并？

**答案**：S3 与 S6 合并为一次"选一个 worker"的路由决定；S5（返回传输元数据）与 S7（NIXL 跨 worker 传 KV）消失，因为 prefill 产生的 KV 就留在本进程的显存里，decode 直接使用；S4 与 S8 在同一 worker 内连续执行。S1、S2、S9 不变。（依据：`Aggregated` 的定义见 [lib/kv-router/src/worker_type.rs:6-8](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs#L6-L8)，分离步骤见 [architecture-flow.md:9-33](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md#L9-L33)；本题为推理题，具体代码路径在第 7 单元验证）

## 5. 综合实践

**任务**：画一张"一笔 `chat/completions` 请求的全链路组件图"，并标注每个元素所属的面。

**要求**：

1. **输入**：只允许对照 [README.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md) 与 [architecture-flow.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/architecture-flow.md) 两个文件来画，不要抄本讲义已经画好的图。
2. **形式**：文本/ASCII 图（或 mermaid），必须包含以下元素：
   - `Client`、`Frontend`、`PrefillRouter`、`Prefill Worker`、`Decode Worker`、`Prefill/Decode KV Cache`；
   - 基础设施元素 `Discovery`、`NATS（可选）`、`Planner`；
   - 每条边标注它携带的内容（如 `disaggregated_params`、`NIXL GPU-to-GPU`、`Metrics`）。
3. **标注**：给**每一个元素**打上面别标签——`[请求面]` / `[控制面]` / `[状态面]`；给**每条边**判断它是实线（请求/控制流）还是虚线（基础设施/数据流）。
4. **自查**：画完后回答三个问题——
   - a) 哪一步是 TTFT 的主要构成？（S4 的 prefill 计算 + S1–S3 的链路开销）
   - b) KV 数据只在哪条边上流动？（S7，prefill KV cache → decode KV cache）
   - c) 如果把 `NATS（可选）`这个节点删掉，图还能成立吗？（能——用 `--no-router-kv-events` 走基于预测的路由，见 [README.md:268](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/README.md#L268)）

**参考判定标准**（自查用）：

- Frontend 被画了**两次**参与（入口 S1/S2 与出口 S9）——很多人第一次只画一次；
- `disaggregated_params` 标在 S5 这条**返回**边上，而不是 S7 上；
- Discovery 用虚线连到几乎所有组件，而不是只连 Router；
- Planner 同时有入边（来自 Frontend 的指标）和出边（到 workers 的扩缩命令）。

**预期结果**：一张你自己画的、每步都标注了面别的九步组件图。把它保留好——第 2 讲跑通真实服务、第 6/7 讲读路由与 KV 传输源码时，你会反复回来修正它。

## 6. 本讲小结

- **Dynamo 是编排层，不是引擎**：它运行在 SGLang / TensorRT-LLM / vLLM 之上，把 GPU 集群变成一个协调的推理系统；单 GPU 单模型场景用引擎就够了。
- **四大核心能力是一个因果链**：长上下文与混合负载 → 需要 P/D 分离、KVBM、KV 感知路由、Planner，它们互相配合而不是并列的功能清单。
- **三面架构是全仓库的分类工具**：请求面（Frontend/Router/workers，优化低延迟）、控制面（Planner/Operator/Discovery，优化向目标收敛）、存储与事件面（KV Events/KVBM/NIXL，优化缓存复用）。
- **一笔分离式请求有九步**（S1–S9），其中 S5 传元数据、S7 才传 KV 数据；KV 在 worker 间点对点直传，没有中央存储。
- **两种路由拓扑**：Dynamo-native（`client → Frontend → Router → workers`）与 GAIE（`client → Gateway → EPP → Frontend sidecar (direct) → workers`）。
- **本地开发零外部依赖**：不需要 etcd 也不需要 NATS，`--discovery-backend file` 即可起步——这是下一讲能顺利跑起来的原因。

## 7. 下一步学习建议

**下一讲（u1-l2）**：五分钟跑起来 —— 用容器或 PyPI 安装方式启动 `python3 -m dynamo.frontend` 与一个 worker，用 curl 发出你的第一条 OpenAI 兼容请求，并观察相同前缀请求的 TTFT 差异。你已经知道本地开发要传 `--discovery-backend file`，届时会明白它的含义。

**继续阅读的源码/文档**（按推荐顺序）：

1. [docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/system-architecture/disaggregated-serving.md) —— 分离式服务的专门文档，本讲 S1–S9 的加深版。
2. [docs/fern/pages/developer-guide/knowledge-base/concepts/communication-planes/request-plane.md](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/docs/fern/pages/developer-guide/knowledge-base/concepts/communication-planes/request-plane.md) —— 请求面传输选项的细节（TCP 默认，`DYN_REQUEST_PLANE` 可换）。
3. [examples/backends/sample/launch/](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/examples/backends/sample/launch/agg.sh) —— `agg.sh` 与 `disagg.sh` 两个启动脚本，分别对应聚合模式与分离模式；下一讲的主角。
4. [lib/kv-router/src/worker_type.rs](https://github.com/ai-dynamo/dynamo/blob/2c4ab6cf9aae89b54286196c8d6c576c715e2f45/lib/kv-router/src/worker_type.rs) —— 再读一遍完整的 `WorkerType`（含 `default_selector_label`），注意"角色与 API 面正交"这句话。
