# config.yaml v0.3 整体结构

## 1. 本讲目标

本讲是「配置体系」单元的第一篇。读完本讲，你应当能够：

- 说出 `config/config.yaml` 的 **七大顶层段落**（`version` / `listeners` / `providers` / `routing` / `entrypoints` / `recipes` / `global`）各自承担什么职责。
- 理解 **请求面（entrypoints）→ 配方（recipes / routing）→ 模型与插件** 的映射关系，也就是「客户端发来的虚拟模型名」是如何被翻译成「执行哪套路由策略」的。
- 知道 `global` 段集中托管了哪些 **全局控制项**（路由器、服务、存储、集成、模型目录）。
- 能够独立打开 `config/config.yaml`，定位任意一个段落，并统计 `routing` 段下的条目数量。

本讲只讲「结构与定位」，不深入某一段的内部细节（例如决策规则如何求值、投影如何计算）。那是后续 u3-l2（recipes）、u3-l3（配置加载与校验）以及第二单元「信号-投影-决策」各篇的任务。

## 2. 前置知识

本讲承接 u2-l1（信号-投影-决策心智模型）和 u1-l4（vllm-sr CLI 命令体系）。在继续之前，请确认你理解下面这些概念：

- **请求路由流水线**：一次请求进入路由器后，先抽取 **Signals（信号）**，再用 **Projections（投影）** 把竞争的信号协调成命名路由带，最后由 **Decisions（决策）** 用布尔规则选出一条路由并驱动模型与插件。这条流水线在 `config.yaml` 里就是 `routing` 段。
- **entrypoint（入口）与 recipe（配方）**：客户端在请求体里写的 `model` 字段（例如 `vllm-sr/auto`）是一个「虚拟模型名」，它本身不会到达任何后端，只是用来选择执行哪套路由策略。这套策略就叫一个 **recipe**。
- **Mixture-of-Models（MoM，混合模型）**：路由器承认「最好的模型是一个组合」，所以配置里会出现多个模型（`qwen3-8b`、`qwen3-32b` 等），由路由按需挑选。

如果你还不太熟悉 YAML：YAML 用 **缩进（空格，不是 Tab）** 表示层级，`key: value` 表示一个键值对，`- ` 开头表示列表的一项。本讲所有「第几层缩进」的描述都基于这个规则。

一句话回顾 u2-l1 的执行流程，它在配置里对应关系是：

```
entrypoint(请求面) → recipe(= routing 段：signals + projections + decisions) → select model(s) → apply plugins
```

## 3. 本讲源码地图

本讲只围绕两个真实文件展开：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `config/config.yaml` | v0.3 的「穷尽式规范参考配置」（exhaustive canonical reference config），包含了所有可配置项的样例 | 逐段拆解七大顶层段落，引用真实行号 |
| `config/README.md` | `config/` 目录的契约说明，解释每一段的设计意图与边界 | 用来核对段落职责、纠正「直觉性误解」 |

辅助参考（仅在需要确认 YAML 与 Go 结构的对应关系时点一下，不展开）：

- `src/semantic-router/pkg/config/config.go`：定义 `RouterConfig` 等 Go 结构，是 YAML 被解析后的运行时表示。
- `src/semantic-router/pkg/config/decision_config.go`：定义决策（Decision）结构，用来确认「插件挂在哪一层」。

> 说明：本讲引用的行号基于当前 HEAD `7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246`。配置文件会随版本演进，若你本地行号略有偏移，以段落标题（如 `routing:`）为准。

## 4. 核心概念与源码讲解

### 4.1 version + listeners + providers：版本号、接入面与模型货架

#### 4.1.1 概念说明

打开 `config/config.yaml`，最先看到的顶层键是 `version`、`listeners` 和 `providers`。它们回答三个最基础的问题：

- **`version`**：这份配置遵循哪个版本的 schema。v0.3 是当前主版本，它和旧的 `config/intelligent-routing/`、`config/memory-rag/` 等示例树已经不兼容（后者在 v0.3 被退役）。
- **`listeners`**：路由器对外提供服务的「接入面」——监听哪个地址、哪个端口、超时多久。注意：这是 **管理面/运行时** 的监听口，真正承载推理流量的 Envoy 监听口由 `deploy/local/envoy.yaml` 单独定义（见 u1-l4 提到的 Envoy 入口 8801）。
- **`providers`**：模型货架。它声明「路由器能调用哪些后端模型、怎么连、多少钱」。这是路由做「质量/成本/延迟」权衡的事实依据。

`providers` 内部分两块：

- **`providers.defaults`**：provider 范围的默认值，例如默认模型 `default_model` 和默认推理强度 `default_reasoning_effort`，以及「推理家族（reasoning_families）」——告诉路由器某类模型如何接收「是否思考」的开关。
- **`providers.models[]`**：每个具体模型的**后端接入细节**（endpoint、协议、权重、定价、外部模型 ID 等）。

#### 4.1.2 核心流程

`providers` 段从「逻辑模型名」到「物理后端」的映射流程：

```
逻辑模型名(如 qwen3-8b)  ──providers.models[]──▶  backend_refs[]  ──▶  具体后端 endpoint
        │                                                        (可多个，带 weight 做加权)
        └──pricing──────────────────────────────────▶  成本数据(供成本感知路由消费)
```

也就是说，决策层（`routing.decisions`）只引用逻辑模型名 `qwen3-8b`；真正「去哪台机器、用什么协议、带什么 header」由 `providers.models[].backend_refs` 决定。这种 **逻辑名与物理后端分离** 的设计，让路由策略和部署拓扑可以独立变化。

#### 4.1.3 源码精读

`version` 与 `listeners` 在文件最开头：

```yaml
version: v0.3

listeners:
  - name: http-8899
    address: 0.0.0.0
    port: 8899
    timeout: 300s
```

见 [config/config.yaml:L1-L7](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1-L7) —— 第 1 行声明 v0.3；第 3–7 行声明一个名为 `http-8899`、监听 `0.0.0.0:8899`、超时 300s 的接入面。

`providers.defaults` 与 `providers.models`：

```yaml
providers:
  defaults:
    default_model: qwen3-8b
    default_reasoning_effort: medium
    reasoning_families:
      qwen3:
        type: chat_template_kwargs
        parameter: enable_thinking
      gpt:
        type: reasoning_effort
        parameter: reasoning.effort
  models:
    - name: qwen3-8b
      reasoning_family: qwen3
      provider_model_id: qwen3-8b-instruct
      api_format: openai
      pricing: { currency: USD, prompt_per_1m: 0.18, ... }
      backend_refs:
        - name: local-primary
          endpoint: 127.0.0.1:8000
          protocol: http
          weight: 80
        - name: remote-secondary
          base_url: https://api.example.com/v1
          weight: 20
```

见 [config/config.yaml:L9-L51](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L9-L51)。要点：

- `defaults.default_model: qwen3-8b`：没有显式选模型时的兜底。
- `reasoning_families`：`qwen3` 家族用 `chat_template_kwargs`（`enable_thinking`）传递思考开关；`gpt` 家族用 `reasoning_effort`。同一个「是否推理」的策略，不同后端用不同字段实现，这里做归一化。
- `models[].backend_refs`：一个逻辑模型可以挂 **多个后端**，每个带 `weight`，路由器据此做加权选择（`local-primary` 权重 80，`remote-secondary` 权重 20）。
- `pricing`：分别给出 prompt（输入）、cached_input（命中缓存的输入）、cache_write（写缓存）、completion（输出）四档单价，是成本感知路由的输入。

契约依据见 [config/README.md:L17-L19](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L17-L19)：`providers.defaults` 放 provider 级默认值；`providers.models[]` 直接放后端接入细节；`pricing` 支持四档费率，省略 `cache_write_per_1m` 时回退到 `prompt_per_1m`。

#### 4.1.4 代码实践

**实践目标**：在 `config/config.yaml` 里亲手定位 `providers` 段，并理解「逻辑模型 → 多后端」的映射。

**操作步骤**：

1. 打开 `config/config.yaml`，跳到第 9 行 `providers:`。
2. 数一下 `providers.models[]` 一共有几个逻辑模型（提示：以 4 空格缩进的 `- name:` 为一项）。
3. 找到 `qwen3-8b`，看它的 `backend_refs` 有几个后端、各自的 `weight` 是多少。
4. 找到 `qwen3-32b`，看它的 `api_format` 是什么、`backend_refs` 与 `qwen3-8b` 有何不同。

**需要观察的现象**：你会看到不同模型可以有不同的 `api_format`（`openai` vs `anthropic`）、不同的后端数量与权重。

**预期结果**（基于当前 HEAD，请以本地为准）：

- `providers.models[]` 共 **4 个**逻辑模型：`qwen3-8b`、`qwen3-32b`、`llava-omni`、`sdxl-image`（见 [L20-L96](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L20-L96)）。
- `qwen3-8b` 有 2 个后端（权重 80/20），`api_format: openai`；`qwen3-32b` 有 1 个后端（权重 100），`api_format: anthropic`。

#### 4.1.5 小练习与答案

**练习 1**：`providers.defaults.default_model` 与某个 decision 里显式写的 `model: qwen3-32b`，谁优先？
**答案**：显式声明的模型优先。`default_model` 只是「没有显式选择时」的兜底。

**练习 2**：`reasoning_families` 解决了什么问题？
**答案**：不同后端族（qwen3 用 `enable_thinking`、gpt 用 `reasoning.effort`）用不同字段表达「是否推理/推理强度」。`reasoning_families` 把这种差异归一化，让上层的 `use_reasoning` / `reasoning_effort` 能跨模型通用。

---

### 4.2 routing：路由配置主体

#### 4.2.1 概念说明

`routing` 是 `config.yaml` 最大、最核心的段落（本 HEAD 下从第 98 行一直到第 1443 行，约占整个文件一半多）。它就是 u2-l1 讲的「信号-投影-决策」流水线在配置里的具象化：

- **`routing.strategy`**：全局选择策略（如 `priority`、`confidence`），决定多条路由命中时怎么排序。
- **`routing.modelCards[]`**：模型的 **语义元数据**（参数量、上下文窗口、能力、质量分、模态、标签、LoRA 适配器目录）。注意它和 `providers.models[]` 是两回事：`providers` 管「怎么连后端」，`modelCards` 管「这个模型擅长什么」，供选择算法与成本感知消费。
- **`routing.signals`**：信号层，从请求抽取事实（keyword/embedding/domain/complexity/context 等）。
- **`routing.projections`**：投影层，把竞争的信号协调成命名路由带（partition/score/mapping）。
- **`routing.decisions`**：决策层，每条 decision 是一条 ROUTE，含 WHEN 规则、候选模型、选择算法和 **插件**。

> ⚠️ **关键纠偏**：v0.3 里 **没有** 顶层 `routing.plugins` 段。插件是 **每条 decision 内部** 的 `plugins[]` 字段（即「路由级插件，route-local plugin」）。如果你在 `routing:` 下找 `plugins:` 会找不到——它嵌在每条 `decisions[].plugins` 里。这一点很重要，下一节的实践会专门验证它。

#### 4.2.2 核心流程

`routing` 段内部的数据流（承接 u2-l1）：

```
routing.signals        抽取信号(带 confidence / value)
        │
        ▼
routing.projections    partition(互斥分区) / score(加权和) / mapping(阈值分带)
        │               产出命名路由带，回写进 SignalConfidences
        ▼
routing.decisions      WHEN 布尔规则(引用 signal 或 projection) ──▶ 命中路由
        │                       │
        ├── modelRefs           选模型(引用 modelCards / providers 的逻辑名)
        ├── algorithm           选择算法(router_dc / automix / hybrid / static ...)
        └── plugins[]           路由级插件(header_mutation / response_jailbreak / image_gen ...)
```

decision 命中的判定由 `rules` 这棵布尔树决定，叶子是 `type: keyword|embedding|domain|complexity|projection|...` 的信号/投影引用，组合节点是 `AND/OR/NOT`。这部分在 u2-l4 与 u6-l1 详讲，本讲只需知道「decision = 一条带规则的路由」。

#### 4.2.3 源码精读

`routing` 段开头：声明全局策略和模型元数据。

```yaml
routing:
  strategy: priority
  modelCards:
    - name: qwen3-8b
      capabilities: [chat, reasoning, tools]
      quality_score: 0.83
      modality: ar
      tags: [default, fast]
    - name: qwen3-32b
      capabilities: [chat, reasoning, long-context]
      loras:
        - name: computer-science-expert
      quality_score: 0.96
      modality: ar
      tags: [premium, analysis]
```

见 [config/config.yaml:L98-L119](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L98-L119)。`modelCards` 的 `loras[]` 目录很重要：decision 可以通过 `lora_name` 引用某个适配器，但这个名字必须先在 `modelCards[].loras` 里声明（见 [config/README.md:L57](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L57)）。

`routing.projections` 三类操作（partition/score/mapping）：

```yaml
  projections:
    partitions:
      - name: support_intents
        semantics: exclusive
        temperature: 0.3
        members: [technical_support, account_management]
        default: technical_support
    scores:
      - name: request_difficulty
        method: weighted_sum
        inputs:
          - { type: embedding, name: technical_support, weight: 0.18 }
          - { type: complexity, name: needs_reasoning:hard, weight: 0.36 }
          ...
    mappings:
      - name: request_band
        source: request_difficulty
        method: threshold_bands
        outputs:
          - { name: support_fast, lte: 0.20 }
          - { name: support_balanced, gt: 0.20, lt: 0.45 }
          - { name: support_escalated, gte: 0.45 }
```

见 [config/config.yaml:L531-L588](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L531-L588)。这里 `request_difficulty` 这个 score 把多个信号加权求和成一个连续分数，`request_band` 这个 mapping 再把它切成三段命名带（`support_fast`/`support_balanced`/`support_escalated`），决策层就能写 `type: projection, name: support_escalated` 这样的 WHEN 条件。投影的三类操作的数学含义详见 u2-l3，本讲只看结构。

`routing.decisions` —— 拿一条「带规则 + 候选模型 + 算法 + 插件」的完整 decision 做样板：

```yaml
  decisions:
    - name: support_router_dc_route
      description: Embedding-driven support route that escalates only when ...
      priority: 150
      rules:
        operator: AND
        conditions:
          - { type: embedding, name: technical_support }
          - { type: projection, name: support_escalated }
      modelRefs:
        - { model: qwen3-8b, use_reasoning: false }
        - { model: qwen3-32b, use_reasoning: true }
      algorithm:
        type: router_dc
        router_dc: { temperature: 0.2, dimension_size: 384, min_similarity: 0.7, ... }
      plugins:
        - type: header_mutation
          configuration:
            add:    [{ name: X-Tenant-Tier, value: premium }]
            update: [{ name: X-Route-Source, value: semantic-router }]
            delete: [X-Debug-Trace]
```

见 [config/config.yaml:L957-L992](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L957-L992)。这条 decision 命中条件是「technical_support 嵌入命中 AND 投影 support_escalated 命中」；命中后在 `qwen3-8b`/`qwen3-32b` 间用 `router_dc` 算法选；并执行一个 `header_mutation` 插件改写请求头。**注意 `plugins` 嵌在 decision 内部（6 空格缩进），不在 `routing:` 顶层。**

「插件挂在 decision 上」的 Go 侧依据——`Plugins` 是 `Decision` 结构的字段，不是 `Routing` 的字段：

```go
// src/semantic-router/pkg/config/decision_config.go
type Decision struct {
    ...
    Plugins []DecisionPlugin `yaml:"plugins,omitempty"`
    ...
}
```

见 [src/semantic-router/pkg/config/decision_config.go:L15](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/decision_config.go#L15)。这从代码层面确认了「路由级插件」的设计。

#### 4.2.4 代码实践

**实践目标**：亲手统计 `routing` 段下 `decisions`、`signals`、`projections` 各有多少条目，并验证「plugins 不是顶层段」。

**操作步骤**：

1. 打开 `config/config.yaml`，定位 `routing:`（第 98 行）到 `entrypoints:`（第 1444 行）之间就是整个 `routing` 段。
2. 统计 **decisions**：在 590–1443 行之间，数 4 空格缩进的 `- name:`（即每条 decision 的名字）有多少个。
3. 统计 **signals 信号族**：在 `routing.signals`（第 137 行）下，数 4 空格缩进的键（如 `keywords:`、`embeddings:`、`domains:` ...）有多少个。
4. 统计 **projections**：在 `routing.projections`（第 531 行）下，分别数 `partitions` / `scores` / `mappings` 各有几项。
5. 验证 **plugins**：在整个文件里搜索顶层是否有 `routing.plugins`，再统计有多少条 decision 内部带了 `plugins:` 块。

可以用下面这条命令（只读）辅助统计 decisions：

```bash
awk 'NR>=591 && NR<=1443 && /^    - name:/{c++} END{print c}' config/config.yaml
```

**需要观察的现象**：decisions 数量较多（二十多条）；signals 下有很多信号族；projections 只有寥寥几项；`routing.plugins` 这个顶层键 **不存在**，插件散布在部分 decision 内部。

**预期结果**（基于当前 HEAD，请以本地为准）：

- `routing.decisions`：**23 条**（见 [L590-L1443](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L590-L1443)）。
- `routing.signals` 信号族：**20 个**家族（`keywords`/`embeddings`/`domains`/`fact_check`/`user_feedbacks`/`reasks`/`preferences`/`language`/`context`/`structure`/`complexity`/`modality`/`role_bindings`/`jailbreak`/`pii`/`kb`/`conversation`/`events`/`metadata`/`classifiers`，见 [L137-L530](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L137-L530)）。注意：配置里的信号族数量可能多于 u2-l2 介绍的「16 个核心信号族」，因为参考配置为覆盖更多契约额外展出了若干族。
- `routing.projections`：**1 个 partition + 2 个 score + 1 个 mapping**（见 [L531-L588](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L531-L588)）。
- `plugins`：**没有**顶层 `routing.plugins`；有 **16 条** decision 内部携带了 `plugins:` 块（如 `header_mutation`、`response_jailbreak`、`image_gen` 等）。

> 这些数字会随版本变化，若你本地结果不同，以本地为准，并思考「为什么 reference 配置要把这些条目都列出来」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `modelCards` 和 `providers.models` 要分成两段，而不是合并？
**答案**：职责不同。`providers.models` 描述「怎么连后端」（endpoint、协议、定价、权重），是部署/运维视角；`modelCards` 描述「这个模型擅长什么」（能力、质量分、模态、标签、LoRA），是路由策略视角。分开后，改部署拓扑不必动路由策略，反之亦然。

**练习 2**：一条 decision 同时声明了 `modelRefs`（多个候选模型）和 `algorithm`（如 `router_dc`），二者关系是什么？
**答案**：`modelRefs` 给出 **候选集**，`algorithm` 决定 **从候选集里怎么选**。例如 `router_dc` 用查询-模型对比学习打分在候选里挑；若 `algorithm.type: static` 则直接用第一个候选。详见 u6-l2。

**练习 3**：如果我想给所有路由都加一个插件，在 `routing:` 顶层写 `plugins:` 行不行？
**答案**：不行。v0.3 没有 `routing.plugins` 顶层段，插件是 **路由级** 的（`decisions[].plugins`）。需要「全局默认」的插件行为（如 router_replay）由 `global.services.router_replay.enabled` 控制，路由级可单独 `enabled: false` 覆盖（见 [config/README.md:L37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L37)）。

---

### 4.3 entrypoints 与 recipes：请求面到配方的映射

#### 4.3.1 概念说明

前面两段（`providers`、`routing`）回答了「有哪些模型」和「怎么路由」。但还有一个关键问题：**客户端发来的请求，凭什么走这套 `routing`？**

答案就是 `entrypoints` 和 `recipes`：

- **`entrypoints`（入口/请求面）**：把客户端在请求体里写的「虚拟模型名」（如 `vllm-sr/exhaustive-default`）映射到一个 **recipe 名**。**这个名字永远不会到达后端**，它只用来选择执行哪套路由策略。
- **`recipes`（配方）**：命名路由策略的集合。每个 recipe 携带一个 `routing` 块（`signals`/`projections`/`decisions`/`strategy`），形状和顶层 `routing` 一样。

一个极其重要的约定：**顶层的 `routing` 段本身就是一个名为 `default` 的 recipe**。也就是说：

- `entrypoints` 里 `recipe: default` → 走顶层 `routing` 段；
- `entrypoints` 里 `recipe: privacy-first` → 走 `recipes[]` 里 `name: privacy-first` 的那个 `routing` 块。

#### 4.3.2 核心流程

从「客户端请求」到「选中配方」的映射：

```
客户端请求体: { "model": "vllm-sr/exhaustive-privacy", ... }
            │
            ▼
entrypoints:  model_names:["vllm-sr/exhaustive-privacy"] ──▶ recipe: privacy-first
            │
            ▼
recipes:  name: privacy-first ──▶ routing: { signals, decisions, strategy }
            │                          (与顶层 routing 同形状，但名字局部于本 recipe)
            ▼
执行该 recipe 的 信号→投影→决策 流水线
```

多配方（multi-recipe）让一个路由器实例同时服务多种互不相干的策略——例如「默认流量」走平衡配方，「隐私流量」走隐私配方，且 **信号/投影/决策的名字在 recipe 之间是局部的、不能跨 recipe 引用**，PII/越狱/authz 规则、算法、插件、缓存、重放、学习状态都互相隔离；而模型目录、providers、模型资产、服务/存储基础设施是 **共享** 的。

#### 4.3.3 源码精读

`entrypoints` 把虚拟模型名指到 recipe：

```yaml
entrypoints:
  - model_names: ["vllm-sr/exhaustive-default"]
    recipe: default
  - model_names: ["vllm-sr/exhaustive-privacy"]
    recipe: privacy-first
```

见 [config/config.yaml:L1444-L1448](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1444-L1448)。`vllm-sr/exhaustive-default` → `default`（即顶层 routing）；`vllm-sr/exhaustive-privacy` → `privacy-first`。

`recipes` 定义命名配方（每个带自己的 `routing`）：

```yaml
recipes:
  - name: privacy-first
    description: Keeps privacy-sensitive traffic on the local default-tier model.
    routing:
      strategy: confidence
      signals:
        keywords:
          - name: privacy_recipe_keywords
            ...
      decisions:
        - name: privacy_recipe_route
          priority: 100
          rules: { operator: AND, conditions: [{ type: keyword, name: privacy_recipe_keywords }] }
          modelRefs: [{ model: qwen3-8b, use_reasoning: false }]
          algorithm: { type: static }
```

见 [config/config.yaml:L1450-L1476](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1450-L1476)。注意 `privacy-first` 的 `routing` 块和顶层 `routing` 形状一致（`strategy`/`signals`/`decisions`），但 **没有** `modelCards`——模型目录是全局共享的。

契约依据见 [config/README.md:L33-L34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L33-L34)：「`entrypoints` 把请求面的虚拟模型名映射到命名路由配方，这些名字永不到达后端，只用来选择哪个路由配置处理请求」；「顶层 `routing` 配置就是 `default` 配方；每个 `recipes[].routing` 携带相同 profile 形状，但绝不含 `modelCards`，名字局部于一个配方，跨配方引用无效」。

#### 4.3.4 代码实践

**实践目标**：解释「entrypoints 如何把请求指到 recipe」，并对比顶层 `routing`（default）与 `recipes` 里配方的差异。

**操作步骤**：

1. 假设客户端发来一个请求，`model` 字段为 `vllm-sr/exhaustive-privacy`。
2. 在 `entrypoints`（第 1444 行）里找到这个虚拟模型名，确认它映射到哪个 recipe。
3. 跳到 `recipes`（第 1450 行），找到该 recipe 的 `routing` 块，观察它和顶层 `routing`（第 98 行）在结构上的相同与不同。
4. 回答：如果客户端写的是 `model: vllm-sr/exhaustive-default`，会走哪个 `routing` 块？为什么 `recipes` 里找不到 `name: default`？

**需要观察的现象**：`privacy-first` 配方有自己的 `signals`/`decisions`，和顶层完全不同；顶层 `routing` 没有 `name` 字段，但它就是 `default`。

**预期结果**：

- `vllm-sr/exhaustive-privacy` → recipe `privacy-first` → 走 `recipes[0].routing`（一个把隐私关键词导向本地 `qwen3-8b` 的简单策略）。
- `vllm-sr/exhaustive-default` → recipe `default` → 走 **顶层 `routing` 段**。`recipes[]` 里不重复声明 `default`，因为顶层 `routing` 本身就是 `default`，重复声明反而冲突。
- 结构差异：配方内的 `routing` 没有 `modelCards`（模型目录共享）；顶层 `routing` 有 `modelCards`。

> 待本地验证：用 `vllm-sr validate --config config/config.yaml` 校验配置合法性，确认 entrypoints/recipes 的映射能通过校验（校验机制详见 u3-l3）。

#### 4.3.5 小练习与答案

**练习 1**：为什么说 entrypoint 里的模型名「永远不会到达后端」？
**答案**：因为它只是路由器内部用来选配方的「别名」。真正发给后端的模型名是配方里 decision 的 `modelRefs[].model`（对应 `providers.models[]` 的逻辑名/`provider_model_id`）。客户端写 `vllm-sr/exhaustive-default` 时，后端收到的是 `qwen3-8b` 之类。

**练习 2**：能不能在 `privacy-first` 配方的 decision 里引用顶层 `routing` 定义的信号（比如 `technical_support`）？
**答案**：不能。信号、投影、决策的名字 **局部于一个配方**，跨配方引用无效（见 [config/README.md:L34](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L34)）。配方之间只有模型目录、providers、服务/存储基础设施是共享的。

---

### 4.4 global：全局控制项

#### 4.4.1 概念说明

如果说 `routing` 是「这一次请求怎么路由」，那么 `global` 就是「整个路由器实例怎么运行」。它集中托管五类路由器级别的控制项：

- **`global.router`**：路由器本身的运行参数——配置来源（`config_source`）、全局策略、自动路由的模型别名（`auto_model_names`）、流式 body 处理、跳过处理开关、**模型选择（model_selection）**、**在线学习（learning）** 等。
- **`global.services`**：各项服务子能力的开关与配置——API、response_api、observability、authz、ratelimit、management_api、startup_status、**router_replay（重放）** 等。
- **`global.stores`**：存储后端——semantic_cache（语义缓存）、memory（记忆）、vector_store（向量库）、tools（工具库）、looper 等。
- **`global.integrations`**：外部集成。
- **`global.model_catalog`**：路由器拥有的「模型目录」——embeddings、system、external、kbs（知识库）、modules（prompt_compression、hallucination_mitigation 等模型驱动模块）。

#### 4.4.2 核心流程

`global` 不参与单次请求的「信号→投影→决策」运算，而是为整条流水线 **提供基础设施与默认行为**：

```
global.router             ──▶  路由器运行模式(配置来源/自动别名/模型选择/学习)
global.services           ──▶  各服务开关(可观测/限流/鉴权/重放/管理API ...)
global.stores             ──▶  缓存/记忆/向量/工具后端
global.model_catalog      ──▶  嵌入/系统/外部模型/知识库/模块的统一目录
            │
            ▼
   被 routing.signals / routing.decisions.plugins / 选择算法 共享消费
```

一个典型的协作例子：`global.services.router_replay.enabled` 是 **路由器级** 的重放默认开关，开着时所有 decision 默认捕获重放，除非某条路由的 route-local 插件显式 `enabled: false`（见 [config/README.md:L37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L37)）。这正体现了「global 给默认值、routing/recipe 可覆盖」的分层。

#### 4.4.3 源码精读

`global.router` 的开头——配置来源、自动别名、模型选择、学习：

```yaml
global:
  router:
    config_source: file
    strategy: priority
    auto_model_name: exhaustive-reference
    auto_model_names: [vllm-sr/auto, auto, exhaustive-reference]
    streamed_body: { enabled: true, max_bytes: 1048576, timeout_sec: 15 }
    skip_processing: { enabled: false }
    model_selection:
      method: hybrid
      enabled: true
      router_dc: { temperature: 0.2, dimension_size: 384, min_similarity: 0.7, ... }
      automix: { verification_threshold: 0.8, max_escalations: 2, cost_aware_routing: true, ... }
      hybrid: { experience_weight: 0.4, router_dc_weight: 0.25, automix_weight: 0.25, cost_weight: 0.1, ... }
    learning:
      enabled: true
      adaptation: { enabled: true, candidate_set: decision, strategy: routing_sampling }
      protection: { enabled: true, scope: conversation, ... }
```

见 [config/config.yaml:L1478-L1559](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1478-L1559)。要点：

- `config_source: file` —— 稳态配置来自文件；K8s CRD 调和时则为 `kubernetes`（见 [config/README.md:L42](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L42)）。
- `auto_model_names` —— 进入「vLLM-SR 全自动路由」的请求模型别名（默认含 `vllm-sr/auto`、`auto`、`MoM`）；`auto_model_name` 是旧的单名兼容字段（见 [config/README.md:L43](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L43)）。
- `model_selection` —— 模型选择的 **全局默认**（`hybrid` 方法把 router_dc/automix/experience/cost 加权组合）。注意它和某条 decision 内的 `algorithm` 是「全局默认 vs 路由级覆盖」的关系。
- `learning.adaptation` / `learning.protection` —— 在基础决策算法之后叠加「在线模型选择学习」与「智能体连续性保护」（见 [config/README.md:L36](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L36)）。

`global` 的五类子段起点（方便你跳转定位）：

| 子段 | 起始行 | 含义 |
| --- | --- | --- |
| `global.router` | [L1479](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1479) | 路由器运行模式 |
| `global.services` | [L1561](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1561) | 服务开关（api/observability/authz/ratelimit/router_replay…） |
| `global.stores` | [L1730](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1730) | 存储后端（semantic_cache/memory/vector_store/tools/looper） |
| `global.integrations` | [L2033](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L2033) | 外部集成 |
| `global.model_catalog` | [L2096](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L2096) | 模型目录（embeddings/system/external/kbs/modules） |

契约总览见 [config/README.md:L35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/README.md#L35)：「`global.router`、`global.services`、`global.stores`、`global.integrations` 和 `global.model_catalog` 显式暴露路由器级别的覆盖」。

#### 4.4.4 代码实践

**实践目标**：在 `global` 段定位五类子段，并理解「全局默认 vs 路由级覆盖」。

**操作步骤**：

1. 跳到 `global:`（第 1478 行），依次定位 `router` / `services` / `stores` / `integrations` / `model_catalog` 五个子段。
2. 在 `global.router.model_selection` 里找到 `method`，记下默认选择方法。
3. 对比：顶层 `global.router.model_selection.method` 与某条 decision 的 `algorithm.type`（例如 4.2 里 `support_router_dc_route` 的 `router_dc`）。思考：当 decision 显式写了 `algorithm` 时，全局 `model_selection` 还起作用吗？
4. 在 `global.services` 里找到 `router_replay`，确认它的默认开关。

**需要观察的现象**：`global` 把「跨请求、跨路由」的运行时行为集中托管；很多开关都有「全局默认 + 局部覆盖」的成对设计。

**预期结果**（基于当前 HEAD）：

- 五类子段位置见上表。
- `global.router.model_selection.method: hybrid`（全局默认）。decision 显式写了 `algorithm` 时以 decision 的算法为准（路由级覆盖全局默认）。
- `global.services.router_replay.enabled` 默认开启时，所有 decision 默认捕获重放。

> 待本地验证：阅读 `global.stores.semantic_cache` 与 `global.model_catalog.modules` 的字段，体会「存储后端」与「模型驱动模块」为何要放在 global 而不是 routing 里（提示：它们是基础设施，被多个配方共享）。

#### 4.4.5 小练习与答案

**练习 1**：`global.router.config_source` 取 `file` 和 `kubernetes` 分别意味着什么？
**答案**：`file` 表示稳态配置来自本地文件（如这份 `config.yaml`）；`kubernetes` 表示由 K8s CRD 调和产生配置（见 Operator/CRD 单元 u12-l3）。穷尽参考配置用 `file`。

**练习 2**：为什么语义缓存（semantic_cache）放在 `global.stores` 而不是 `routing` 里？
**答案**：因为语义缓存是 **共享基础设施**，多个配方、多条路由都可能读写同一个缓存后端（内存/Milvus/Qdrant/Redis…）。把它放在 `global` 保证配置唯一、后端复用；而「这条路由是否启用缓存」由路由级行为（如 decision 的 `emits[].retention.drop`）控制。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「端到端定位」小任务：

**任务**：假设客户端发来一个请求，请求体里 `"model": "vllm-sr/exhaustive-default"`，且消息内容命中了 `technical_support` 嵌入信号并落到 `support_escalated` 投影带。请按顺序回答：

1. **入口**：这个虚拟模型名经 `entrypoints` 映射到哪个 recipe？对应哪个 `routing` 块？（→ 4.3）
2. **货架**：这条路由可能用到哪些逻辑模型？它们的物理后端和定价在哪一段声明？（→ 4.1）
3. **流水线**：请求会经过哪几层？命中的是哪条 decision？它用了什么选择算法？挂了什么插件？（→ 4.2）
4. **基础设施**：这条请求在响应阶段是否可能命中语义缓存、是否会被重放捕获？这些开关在哪一段？（→ 4.4）

**参考思路**（请先自己查，再对照）：

1. `vllm-sr/exhaustive-default` → recipe `default` → **顶层 `routing` 段**（[L98](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L98)）。
2. 命中 `support_router_dc_route`（[L957](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L957)），候选模型 `qwen3-8b` / `qwen3-32b`；它们的物理后端与定价在 `providers.models[]`（[L20-L60](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L20-L60)），语义元数据在 `routing.modelCards`（[L100-L119](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L100-L119)）。
3. 流水线：`signals`（technical_support 嵌入）→ `projections`（request_band 映射出 support_escalated）→ `decisions`（support_router_dc_route 命中）；算法 `router_dc`；插件 `header_mutation`。
4. 语义缓存在 `global.stores.semantic_cache`（[L1731](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1731)）；重放默认开关在 `global.services.router_replay`（[L1691](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/config/config.yaml#L1691)）。

完成后再运行一次校验确认你理解的映射无误：

```bash
vllm-sr validate --config config/config.yaml
```

> 待本地验证：校验命令的具体输出与可用子命令以本地 `vllm-sr validate --help` 为准（CLI 详见 u1-l4，校验机制详见 u3-l3）。

## 6. 本讲小结

- `config/config.yaml`（v0.3）有 **七大顶层段落**：`version`、`listeners`、`providers`、`routing`、`entrypoints`、`recipes`、`global`。
- `providers` 是 **模型货架**：`defaults` 放 provider 级默认，`models[]` 放「逻辑模型名 → 多后端 + 定价」的物理接入；`routing.modelCards` 则是模型的语义元数据，二者职责分离。
- `routing` 是 **路由主体**：`strategy` / `modelCards` / `signals` / `projections` / `decisions`。**插件不是顶层段**，而是嵌在每条 `decisions[].plugins` 里的「路由级插件」。
- `entrypoints` 把客户端的 **虚拟模型名** 映射到 `recipe`；**顶层 `routing` 就是 `default` 配方**，`recipes[]` 里定义其他命名配方，每个配方自带 `routing` 块（无 `modelCards`），名字局部于配方。
- `global` 集中托管路由器级基础设施：`router`（运行模式/模型选择/学习）、`services`（服务开关）、`stores`（缓存/记忆/向量/工具）、`integrations`、`model_catalog`，遵循「全局默认 + 路由级覆盖」的分层。
- 本讲只讲「结构与定位」；某一段的内部机制（决策求值、投影数学、配置加载与校验、热重载）在后续讲义展开。

## 7. 下一步学习建议

- **u3-l2 Recipes：可用的路由配方**：深入 `config/recipes/` 下 balance/accuracy/privacy 等配方的「四文件交付契约」（config.yaml + recipe.dsl + probes.yaml + README），把本讲的「recipe」概念落到具体可运行样例。
- **u3-l3 配置加载与校验**：看 Go 端如何把这份 YAML 解析成 `RouterConfig`、做规范化与语义校验，以及 `config.Replace` 如何支撑热重载/K8s 配置更新——回答「这份配置是如何被加载和校验的」。
- **第二单元（u2）回顾**：若你对 `signals/projections/decisions` 的数学与规则语义还不熟，回头读 u2-l2/u2-l3/u2-l4，再回来看 `routing` 段会有「通透」感。
- **u1-l4 回顾**：若想动手跑配置，回到 CLI 讲义，用 `vllm-sr validate` / `vllm-sr config` 实际操作这份 `config.yaml`。
