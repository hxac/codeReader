# 项目概览与定位：什么是 vLLM Semantic Router

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲后，你应该能够：

- 说清楚 vLLM Semantic Router（下文简称 **vLLM SR**）是什么、放在系统里的什么位置；
- 理解它要解决的五个现实痛点：**质量（quality）、成本（cost）、延迟（latency）、隐私（privacy）、安全（safety）**；
- 理解 **Mixture-of-Models（混合模型）** 这一核心理念，以及为什么要把路由逻辑从应用代码「下沉」到一个独立的控制面；
- 知道 **Envoy ExtProc（External Processor）** 是什么，为什么 vLLM SR 选择以它作为控制面的承载方式；
- 能复述项目驱动自身演进的 5 个核心研究问题，并对其中至少一个有自己的初步思考。

本讲以阅读项目自带的 README、文档首页与 `AGENTS.md` 为主，**不涉及任何编译或运行**，目标是为后续所有讲义建立一个共同的概念底座。

## 2. 前置知识

本讲对读者几乎没有硬性技术门槛，但如果你了解以下几点，理解会更顺畅：

- **大语言模型（LLM）推理**：给一段输入（prompt），模型产出一段输出（response）。一个请求通常包含「内容、上下文、用户、模型」这几类信息。
- **应用后端的常见架构**：客户端（client）发起请求，经过若干中间层，最终落到提供服务的后端（backend）。本讲会反复用到「客户端 / 后端」这对词。
- **Envoy**：一个高性能的可编程代理（proxy）。如果你没接触过也没关系，本讲第 4.3 节会用直觉化的语言解释它在 vLLM SR 里扮演的角色。
- **一句话术语对照**：

  | 术语 | 通俗解释 |
  | --- | --- |
  | routing（路由） | 为每个请求「选择走哪条路、用哪个模型」的决策过程 |
  | signal（信号） | 从请求/响应/用户/运行时中抽取出来的、可用于决策的事实 |
  | control plane（控制面） | 集中承载策略与决策逻辑、可被观察和配置的那一层 |

如果你对以上术语完全陌生也不必担心，本讲会随讲随解释。

## 3. 本讲源码地图

本讲涉及的「源码」其实是项目自身的**说明性文档**——它们是理解项目定位最权威、最原始的素材。

| 文件 | 作用 | 本讲用途 |
| --- | --- | --- |
| [README.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md) | 面向所有人的项目门面：一句话定位、痛点对照表、安装与新闻 | 理解项目定位与痛点 |
| [website/docs/intro.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md) | 文档站首页：研究导向的项目介绍，含 5 个研究问题、信号-投影-决策概览 | 理解 MoM 理念、研究问题、ExtProc 定位 |
| [AGENTS.md](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md) | 给开发代理（以及人类贡献者）的仓库入口与约束 | 理解一句话执行流程与仓库结构 |

> 提示：`website/docs/` 是项目唯一对外公开的文档树（见 `AGENTS.md` 的仓库地图），它是比 README 更系统、更深入的研究型介绍，后续多讲都会回到这里。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，正好对应项目定位的三个层次：

1. **4.1 项目定位**：它是什么、要解决谁的什么问题。
2. **4.2 Mixture-of-Models 理念**：为什么不能再依赖「单一模型」。
3. **4.3 Envoy ExtProc 控制面**：这套理念用什么工程形态落地。

### 4.1 项目定位

#### 4.1.1 概念说明

一句话：vLLM SR 是一个**面向异构 LLM 基础设施的可编程路由层（programmable routing layer）**。

「异构（heterogeneous）」指的是：现实里的模型后端千差万别——不同厂商、不同尺寸、部署在不同硬件（GPU/加速器/边缘/云）、跑在不同位置（本地/私有/公有云）、擅长不同任务。vLLM SR 的工作，就是在客户端和这些后端之间，为**每一个请求**选出（甚至组合出）最合适的「模型路径（model path）」。

它明确强调一点：要在**不把路由逻辑硬编码进应用代码**的前提下，同时改善五个维度——

> quality, cost, latency, privacy, and safety

也就是说，路由逻辑不应该是散落在各处业务代码里的 `if/else`，而应该被收敛成一层可配置、可观测的策略。

#### 4.1.2 核心流程

从用户视角看，vLLM SR 在请求链路里做四件事：

1. **接收请求**：客户端照常发出一个 LLM 请求（例如 OpenAI 风格的 `/v1/chat/completions`）。
2. **抽取信号**：从请求体、响应、用户身份、运行时上下文中提炼出可用于决策的「事实」（信号）。
3. **做出决策**：依据信号 + 用户偏好 + 应用策略，选出本次请求该走哪条路由、用哪个（些）模型。
4. **转发并加工**：把请求送到选中的后端，并可在请求/响应两侧插入插件（缓存、安全检测等）。

用 `AGENTS.md` 里的一句官方浓缩版来描述，最贴切不过（它把上面四步压缩成一条端到端流水线）：

> vLLM SR 是一个 Envoy ExtProc 请求路由器；它把「面向请求的入口（entrypoint）」解析到一个隔离的「配方（recipe）」，求值该配方的信号与投影，应用其决策与算法，然后调用选中的后端与配方作用域内的插件。

伪代码化表示：

```
request → resolve entrypoint → recipe (signals + projections + decisions)
        → decide route → select backend(s) → apply plugins → response
```

#### 4.1.3 源码精读

**定位句（最重要的一句）**：README 用一句话定义了项目。

[README.md:L21](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L21-L21) —— 项目是「为异构 LLM 基础设施构建 Mixture-of-Models 系统的可编程路由层」，它「评估请求信号、用户偏好与应用策略，为每个请求选择或组合出正确的模型路径」。

紧接着的这句点明了**痛点边界**：

[README.md:L23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L23-L23) —— 「在不把路由逻辑硬编码进应用代码的前提下，改善质量、成本、延迟、隐私与安全。」

README 还用一张「现状对照表」把这层抽象要弥合的四个维度讲清楚了：

[README.md:L25-L30](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L25-L30) —— 四个维度的「Fragmented today（碎片化的现状）」对照「With vLLM SR（有了 SR 之后）」：

| Dimension（维度） | Fragmented today（碎片化现状） | With vLLM SR（有了 SR 之后） |
| --- | --- | --- |
| Models | 不同模型擅长不同工作 | 组合出个性化的模型路径 |
| Compute | GPU、加速器、边缘、云并存 | 跨异构算力做路由 |
| Location | 推理分布在边缘、私有、云 | 把数据留在它的边界之内 |
| Preference | 「最好」因人和负载而变 | 让每一个偏好都可执行 |

最后，`AGENTS.md` 给出最精炼的执行流水线描述：

[AGENTS.md:L5-L8](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L5-L8) —— 把「入口解析 → 配方求值 → 决策 → 调用后端与插件」串成一句话。这是后续讲义（U5 请求处理主链路）的纲领。

#### 4.1.4 代码实践

> 这是本讲第一项实践，属于「**源码阅读 + 用自己的话复述**」型，不需要任何运行环境。

1. **实践目标**：用自己的语言复述「vLLM SR 是什么、放在哪里、解决什么」。
2. **操作步骤**：
   - 打开 [README.md:L19-L32](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L19-L32) 的 `About` 段。
   - 逐字读一遍 L21 的定位句和 L23 的痛点句。
   - 对照 L25–L30 的对照表，找出你最在意的那一行。
3. **需要观察的现象**：注意官方措辞里反复出现的「programmable（可编程）」「compose（组合）」「without hard-coding（不硬编码）」——这正是它的定位关键词。
4. **预期结果**：你能写下一段 3–5 句话的自我陈述，回答两个问题：
   - SR 在「客户端」与「模型后端」之间处于什么位置？
   - 它声称同时改善的五个维度是哪五个？
5. 参考答案见 4.1.5。

#### 4.1.5 小练习与答案

**练习 1**：README 的对照表里，「Preference」这一行的「With vLLM SR」写的是什么？它体现了什么设计意图？

**参考答案**：写的是「Make every preference executable（让每一个偏好都可执行）」。它体现的意图是：不同用户/不同负载对「最好的模型」定义不同，SR 不是给所有人一个统一答案，而是把「偏好」本身变成一条可执行的路由策略，由配置驱动，而非写死在代码里。

**练习 2**：`AGENTS.md` 里那条一句话流程，出现了哪几个名词？（提示：entrypoint、recipe、signal、projection、decision、backend、plugin）请按顺序排列。

**参考答案**：entrypoint → recipe →（signal + projection）→ decision → backend + plugin。这条顺序就是后续 U5 主链路讲义的骨架。

---

### 4.2 Mixture-of-Models 理念

#### 4.2.1 概念说明

vLLM SR 的灵魂口号是 **"Make Your Mixture-of-Models Programmable."**（让你的混合模型可编程）。

**Mixture-of-Models（MoM，混合模型）** 的核心主张是：**没有「一个模型」能同时在质量、成本、延迟、隐私、安全上都做到最优**。现实里：

- 大模型质量高但贵且慢；
- 小模型便宜快，但复杂推理可能翻车；
- 有些模型擅长代码、有些擅长多语言、有些擅长安全合规；
- 有些请求需要本地部署（隐私），有些可以上云（成本）。

所以与其追问「哪个模型最好」，不如承认「**最好的模型是一个组合**」，并让路由层按请求的具体情况动态决定本次该用哪条组合路径。这正是文档首页开篇的那句信念：

> We believe Mixture-of-Models is the next-generation model architecture for heterogeneous LLM inference.
> （我们相信，面向异构 LLM 推理，混合模型是下一代模型架构。）

#### 4.2.2 核心流程

把「单一模型」升级到「混合模型」，关键是要有一套**把信号变成路由**的机制。intro.md 给出了三层结构（Signals → Projections → Decisions）：

1. **Signals（信号）**：从请求/响应/用户/运行时里抽取「事实」。项目维护了 **16 个信号族**。
2. **Projections（投影）**：把多个互相竞争的信号「协调」成一组命名的、可比的结果（命名路由带）。
3. **Decisions（决策）**：用 AND/OR 这类布尔策略规则在信号与投影之上求值，选出当前激活的路由与候选模型。

直觉化地理解，可以想象成：

\[ \text{request} \;\xrightarrow{\text{signals}}\; \text{facts} \;\xrightarrow{\text{projections}}\; \text{coordinated evidence} \;\xrightarrow{\text{decisions}}\; \text{route} \rightarrow \text{model(s)} \]

> 这条链路的具体源码（信号抽取、投影求值、决策引擎）会在 U2（核心概念模型）和 U6（决策引擎与模型选择）里深入展开，本讲只建立直觉。

#### 4.2.3 源码精读

**信念句**：intro.md 开篇即立 MoM 为核心理念。

[website/docs/intro.md:L8-L9](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L8-L9) —— 「我们相信 Mixture-of-Models 是面向异构 LLM 推理的下一代模型架构」，因此才「把信号与偏好转变成可执行的模型路径」。

**三层结构表**：intro.md 用一张表把 Signals / Projections / Decisions 的角色讲清楚。

[website/docs/intro.md:L35-L39](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L35-L39) —— Signals 抽取可复用的请求/安全/追问/偏好事实；Projections 协调互相竞争的匹配、产出命名路由带；Decisions 在其上做 AND/OR 策略选择。

[website/docs/intro.md:L41-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L41-L43) —— 「How it works」一句话总结整条链路：信号从请求中抽取 → 投影协调匹配证据 → 决策规则求值事实 → 选中的路由驱动插件与模型分发。

注意这里首次出现的 **16 个信号族名单**（authz、context、keyword、language、structure、complexity、domain、embedding、kb、modality、fact-check、jailbreak、pii、preference、reask、user-feedback）。你不必现在记住全部，U2-L2 会逐族讲解；这里只要建立一个印象：**SR 的「聪明」来自把多种信号协调起来，而不是靠单一来源判断**。

#### 4.2.4 代码实践

1. **实践目标**：体会「单一模型不够用、需要组合」这一动机。
2. **操作步骤**：
   - 打开 [website/docs/intro.md:L28-L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L28-L43) 的 `Signal and Projection Routing` 小节。
   - 数一数 Signals 行列出了多少个信号族（应为 16 个）。
   - 想象一条真实请求（例如「用中文问一个医疗健康问题」），猜猜它可能触发哪几个信号族（domain? language? complexity?）。
3. **需要观察的现象**：你会看到同一类请求天然会同时触发多个信号，而它们之间可能「打架」（例如：domain=health 倾向用强模型，但 complexity=low 倾向用快模型）。
4. **预期结果**：你能用自己的话解释——为什么需要 Projections 这一层来「协调互相竞争的匹配」。
5. **待本地验证**：本练习无运行步骤，结论以你的思考为准。

#### 4.2.5 小练习与答案

**练习 1**：MoM 理念认为「没有单一模型能在所有维度都最优」。请结合「质量 / 成本 / 延迟」三维度，举一个直观例子说明这种取舍。

**参考答案**：例如一个 70B 的强模型能高质量地完成复杂推理（质量高），但每次调用贵（成本高）且首字延迟大（延迟高）；而一个 7B 小模型便宜快，却在复杂推理上容易出错。MoM 的做法是：对「简单闲聊」路由到小模型，对「复杂推理」路由到强模型，从而在整体上同时兼顾三个维度。

**练习 2**：intro.md 的三层结构是 Signals → Projections → Decisions。如果把「投影层」直接去掉、让决策直接读原始信号，会丢失什么能力？

**参考答案**：会丢失「协调互相竞争的匹配」的能力。投影层的作用是把多个可能冲突的信号归一化成一组命名的、可比的「路由带」，让决策层只面对已经协调好的结论；没有它，决策规则将不得不面对一堆原始的、量纲不一、甚至互相矛盾的信号，规则会变得脆弱且难维护。

---

### 4.3 Envoy ExtProc 控制面

#### 4.3.1 概念说明

理念再好，也要有一个**工程形态**来承载。vLLM SR 选择以 **Envoy 的 External Processor（`ext_proc`）** 来落地。

先解释三个词：

- **Envoy**：一个被广泛使用的高性能可编程代理。很多公司在它上面做流量治理。
- **External Processor / `ext_proc`**：Envoy 提供的一种扩展机制。它允许你把一个**外部 gRPC 服务**挂到 Envoy 的请求/响应处理流水线上，由这个外部服务在请求经过时「插手」——读 headers、读 body、修改请求、甚至决定把流量发往哪里。
- **Control Plane（控制面）**：在本项目语境里，指那层「集中承载路由与策略、可被观察和配置」的逻辑。

把这三者合起来，intro.md 的定义是：

> The project sits between clients and model backends as an Envoy External Processor (`ext_proc`), turning routing from ad hoc application logic into an observable, configurable control plane for multi-model systems.
> （项目以 Envoy External Processor 的形态，位于客户端与模型后端之间，把路由从临时性的应用逻辑，转变成一个可观察、可配置的多模型系统控制面。）

换句话说，**Envoy 负责把流量「搬」过来，vLLM SR 作为 ext_proc 负责「想清楚」**。这种分工的好处是：路由决策变成了一层独立的基础设施，而不是埋在某个业务服务里。

#### 4.3.2 核心流程

从部署形态看，请求的流向大致是：

```
client  →  Envoy (proxy)  →  [vLLM SR as ext_proc]  →  model backend(s)
                                ↑
                        这里做：信号抽取 / 投影 / 决策 / 插件
```

关键点：

1. **客户端无感**：客户端照常把请求发给一个标准的 LLM API 端点（如 `/v1/chat/completions`），Envoy 在前面接收。
2. **ext_proc 在中途介入**：Envoy 把请求的 headers / body 经由 gRPC 流（stream）交给 vLLM SR，SR 在这里完成信号抽取与决策。
3. **可观察 + 可配置**：因为路由逻辑集中在一层、且通过配置（recipe）驱动，所以「为什么这条请求走了这个模型」是可以被审计、被追踪、被热更新的。

> ext_proc 与 Envoy 之间的具体 gRPC 交互（headers → request body → response body 的阶段回调）会在 U4-L3（ExtProc gRPC 服务）里结合 `pkg/extproc` 源码精读。本讲只要记住「SR 是挂载在 Envoy 上的外部处理器」即可。

#### 4.3.3 源码精读

**形态定义句**：intro.md 明确了项目的工程载体。

[website/docs/intro.md:L14-L16](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L14-L16) —— 项目以 Envoy External Processor 的形态位于客户端与模型后端之间，把路由「从 ad hoc（临时拼凑）的应用逻辑，转变成一个可观察、可配置的控制面」。

**仓库印证**：`AGENTS.md` 的仓库地图里，第一条目录就直指 ExtProc。

[AGENTS.md:L28](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L28-L28) —— `src/semantic-router/` 是「Go 路由器、Envoy ExtProc server、路由运行时与 API」。这说明 ExtProc server 的实现本体就在这里。

完整仓库地图，帮你建立全局坐标系（后续讲义都会回到这张地图）：

[AGENTS.md:L26-L41](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L26-L41) —— 各顶层目录的职责，例如 `src/vllm-sr/`（Python CLI 与本地栈编排）、`config/`（规范配置与 recipe）、`candle-binding` 等（推理绑定）、`dashboard/`（管理面板）、`deploy/`（部署产物）、`e2e/`（端到端测试）、`tools/`（构建/开发/发布工具）、`website/`（唯一公开文档树）。

> 一个值得注意的工程约定：`AGENTS.md` 强调仓库根目录「只放仓库级契约、社区元数据与工具要求的入口文件」，**不允许**新增根目录的 catch-all 文件。这就是为什么你看不到根目录的 `docs/` 或 `scripts/`——文档在 `website/`，工具在 `tools/`。这个边界会在 U1-L2（仓库结构）详细讲。

#### 4.3.4 代码实践

1. **实践目标**：通过仓库地图，定位「ExtProc 控制面」的代码本体与配套子系统。
2. **操作步骤**：
   - 打开 [AGENTS.md:L26-L41](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/AGENTS.md#L26-L41)。
   - 在一张纸上（或笔记里）画出「客户端 → Envoy → ext_proc（`src/semantic-router/`）→ 模型后端」的草图，并把每个顶层目录贴到草图的合适位置。
3. **需要观察的现象**：你会注意到这套系统是**多语言、多组件**的——Go 写路由内核、Python 做 CLI 与编排、Rust/C 做推理绑定、React/Go 做面板。ExtProc 内核只是其中一环。
4. **预期结果**：你能说出，一个请求从进入 Envoy 到被某后端处理，至少经过了哪几个仓库子目录对应的组件。
5. **待本地验证**：纯阅读型实践，无需运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 vLLM SR 选择作为 Envoy 的 ext_proc，而不是写一个独立的全功能网关？

**参考答案**：因为这样可以把「搬流量」和「想路由」解耦。Envoy 已经擅长连接管理、负载均衡、mTLS、可观测性等基础能力；SR 只需要在它的流水线里以外部处理器身份注入「信号→决策」的智能，而不必重复造一个网关。这也让 SR 能复用用户现有的 Envoy 基础设施，并保持客户端协议（OpenAI 风格 API）不变。

**练习 2**：根据 `AGENTS.md` 的仓库地图，`website/` 目录被特别标注为「the only public documentation tree」。这对一个想读项目文档的学习者意味着什么？

**参考答案**：意味着要找权威、面向公众的说明，应优先看 `website/`（即文档站，本讲用到的 `intro.md` 就在这里），而不是在仓库根目录或别处随意找 `.md`。这也解释了为什么本系列讲义频繁引用 `website/docs/` 下的文件。

---

## 5. 综合实践

> 本讲的综合实践把三个模块串起来：定位 → 理念 → 工程形态。这是本讲**唯一需要提交「成品」**的任务。

**任务**：写一段 150–250 字的中文小短文（可用列表形式），要求同时覆盖以下三点：

1. **位置**：vLLM SR 在「客户端」与「模型后端」之间的什么位置？它以什么工程形态存在？（提示：Envoy External Processor / 控制面）
2. **三类现实痛点**：从 README 提到的 quality / cost / latency / privacy / safety 五项中，挑**三项**，各用一句话说明 SR 如何应对。
3. **研究问题**：从 intro.md 列出的 5 个核心研究问题中，挑**一个**，用自己的话尝试给出你的初步回答或思路。

**写作提示与素材定位**：

- 位置与形态：参考 [website/docs/intro.md:L14-L16](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L14-L16)。
- 痛点：参考 [README.md:L23](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L23-L23) 与 [README.md:L25-L30](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/README.md#L25-L30)。
- 研究问题（5 个）：参考 [website/docs/intro.md:L20-L26](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/website/docs/intro.md#L20-L26)，它们分别是：
  1. 如何从请求、响应、用户与运行时上下文中**捕获缺失的信号**？
  2. 如何把这些信号**组合**成稳健的路由与策略决策？
  3. 多个模型如何作为**一个系统协作**，而非孤立端点？
  4. 如何作为实用的 **token 经济**来优化延迟、花费与工具使用？
  5. 如何在不割裂服务栈的前提下加入**安全、反馈与可观测性**？

**评判标准（自检清单）**：

- [ ] 是否明确写出「位于客户端与模型后端之间」「以 Envoy ext_proc 形态存在」？
- [ ] 是否挑了至少三项痛点并各给了一句话？
- [ ] 是否选了一个研究问题并给出自己的回答？
- [ ] 全程**没有**把路由逻辑说成「写死在应用里」——而是「可配置、可观察的控制面」？

> 这是纯阅读+写作任务，**待本地完成**；本讲不要求运行任何命令。

## 6. 本讲小结

- vLLM SR 是面向异构 LLM 基础设施的**可编程路由层**，位于客户端与模型后端之间，为每个请求选择/组合出正确的模型路径。
- 它要在**不硬编码路由逻辑进应用**的前提下，同时改善 **quality / cost / latency / privacy / safety** 五个维度。
- 灵魂理念是 **Mixture-of-Models**：承认「最好的模型是一个组合」，并靠 **Signals → Projections → Decisions** 三层把信号变成可执行的路由。
- 工程形态是 **Envoy External Processor（`ext_proc`）控制面**：Envoy 负责搬流量，SR 负责做智能决策，从而把路由从「临时应用逻辑」变成「可观察、可配置」的基础设施层。
- 项目用 5 个核心研究问题（信号捕获、信号组合、模型协作、token 经济、安全可观测）驱动自身演进，理解它们有助于把握后续源码的设计动机。
- 仓库是**多语言 monorepo**：Go 路由内核（`src/semantic-router/`）、Python CLI/编排（`src/vllm-sr/`）、推理绑定、面板、部署、E2E 各居其位，公开文档只在 `website/`。

## 7. 下一步学习建议

本讲只建立了概念底座，**还没有真正进入代码**。建议按以下顺序继续：

1. **下一讲 U1-L2《仓库结构与目录组织》**：对照 `AGENTS.md` 与 `tools/agent/docs/repo-map.md`，把本讲末尾的仓库地图展开成详细的目录职责表，并标出「高变更风险区」。这是后续所有源码讲义的导航基础。
2. **随后 U1-L3 / U1-L4**：学会用 Makefile 本地构建、用 `vllm-sr` CLI 跑起来，让 SR 从「概念」变成「你能启动的进程」。
3. **概念深化 U2**：在进入 Go 源码之前，先用 U2 把 Signals / Projections / Decisions / Routes / Models 的心智模型彻底建立起来——本讲只点到为止的 16 个信号族，会在 U2-L2 逐族展开。
4. **建议精读的源码（后续）**：等进入 U4 后，重点读 `src/semantic-router/pkg/extproc/server.go` 与 `router.go`，验证本讲关于「Envoy 通过 ext_proc 调用 SR」的描述。

> 学习节奏建议：本系列讲义遵循「先概念后源码、先全局后内核」。如果你刚接触这类系统，**不要急着跳到 U4/U5 的源码**，先在 U1–U3 把概念和运行方式吃透，后续源码阅读会顺畅很多。
