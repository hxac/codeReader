# 插件链架构与配置

## 1. 本讲目标

本讲是「插件链与扩展机制」单元的首篇。学完后你应当能够：

- 说出 Semantic Router（以下简称 SR）支持的 **13 种决策插件**（plugin）类型，以及它们各自的配置字段；
- 解释插件为什么是**挂在每条 decision 上的**（即「按 decision 启停」），而不是一个全局开关；
- 描述一次请求在**请求阶段**和**响应阶段**分别会按什么顺序触发哪些插件（即「链式处理」）；
- 结合 `router_replay` 插件，讲清楚它如何在请求阶段捕获请求体、在响应阶段捕获响应体，最终拼出一条完整的路由重放记录。

本讲承接 u5-l3（响应体处理与插件回调），把那里提到的「响应阶段插件回调按固定顺序执行」向上游延伸，补齐「插件是什么、怎么配、怎么被按决策启用、怎么串成链」的全貌。

## 2. 前置知识

在进入本讲前，你需要先建立以下几个心智模型（它们来自前面的讲义）：

- **decision / ROUTE**：SR 用布尔规则树（`WHEN`）匹配请求，命中的那条 decision 决定走哪个模型、开不开推理、启用哪些插件（见 u5-l2、u6-l1）。
- **ExtProc 四阶段**：Envoy 把一次请求切成「请求头 / 请求体 / 响应头 / 响应体」四段 gRPC 消息发给 SR（见 u4-l3、u5-l1）。
- **配置即决策**：`config.yaml` v0.3 的 `routing.decisions[]` 是路由主体，每条 decision 下可以挂一个 `plugins: []` 数组（见 u3-l1）。
- **响应阶段插件回调**：u5-l3 已经讲过，响应阶段会按固定顺序执行越狱检测、幻觉检测、记忆异步存储、告警汇总，并能把某些告警短路成 `block`。

如果你还没看过这几条，建议先快速过一遍 u5-l3 的「响应阶段插件回调」一节，本讲会反复用到那里的结论。

> 一个关键认知：在 SR 里，**「插件」不是独立可执行单元，而是挂在 decision 上的一段配置 + 一段在请求/响应阶段被条件性调用的处理逻辑**。理解了这一点，后面的一切都顺理成章。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `src/semantic-router/pkg/config/routing_surface_catalog.go` | 插件类型的**权威清单**与归一化、合法性判定 |
| `src/semantic-router/pkg/config/plugin_config.go` | 各插件的**配置结构体**、`DecisionPlugin` 载体与按类型解码的访问器 |
| `src/semantic-router/pkg/config/validator_plugin.go` | 启动期对插件配置的**语义校验**（类型是否受支持、字段是否合法） |
| `src/semantic-router/pkg/config/rag_plugin.go` | RAG 插件的配置结构体（独立成文件，字段最多） |
| `src/semantic-router/pkg/config/plugin_router_replay_support.go` | `router_replay` 的「全局默认 + 按决策覆盖」合并逻辑 |
| `src/semantic-router/pkg/extproc/processor_req_body_routing.go` | 请求阶段的**插件链编排**（system_prompt / memory / request_params 注入） |
| `src/semantic-router/pkg/extproc/req_filter_sys_prompt.go` / `req_filter_rag.go` | system_prompt 与 rag 插件的执行与门控 |
| `src/semantic-router/pkg/extproc/processor_res_body_pipeline.go` | 响应阶段的**插件链编排**（越狱/幻觉/记忆/告警/replay） |
| `src/semantic-router/pkg/extproc/recorder.go` | `router_replay` 的**请求阶段捕获**与**响应阶段回写** |
| `src/semantic-router/pkg/extproc/router_replay_setup.go` | 启动期为每个启用 replay 的决策构建 `Recorder` |
| `config/plugin/*/*.yaml` | 各插件的**配置片段示例**（不是完整 config，而是可粘进 decision 的片段） |

> ⚠️ 说明：本讲规格里提到的 `config/plugin/router-replay/README.md` 在当前仓库中**并不存在**。`config/plugin/router-replay/` 目录下只有一个示例文件 `debug.yaml`，整个 `config/plugin/` 目录树里也没有任何 `README.md`，只有各插件的 `.yaml` 片段。本讲只引用真实存在的文件，示例统一取自这些 `.yaml` 片段。

## 4. 核心概念与源码讲解

### 4.1 插件类型目录与配置结构

#### 4.1.1 概念说明

在 SR 里，「插件」是给一条 decision 增加副作用的扩展点：改写请求体、改写请求头、检索知识、检测幻觉、录制重放……每一种副作用对应一个 `type`。

理解插件配置，需要先分清三层东西：

1. **载体（carrier）**：`DecisionPlugin`，只有两个字段——`Type`（插件类型字符串）和 `Configuration`（一段被规范化成结构化字节的 payload，类型是 `*StructuredPayload`）。
2. **强类型结构体（typed struct）**：每个插件类型都有一个对应的 Go 结构体（如 `SystemPromptPluginConfig`），用来把 payload 解码成可读字段。
3. **访问器（accessor）**：`Decision` 上的 `GetXxxConfig()` 方法，负责「在当前 decision 的 plugins 里找到对应类型 → 解码成强类型结构体 → 返回」。

这三层的关系是：YAML 里写 `{type, configuration}` → 载体 `DecisionPlugin` 存下来 → 运行时用访问器按需解码。**配置在加载期并不立即解码成强类型**，而是延迟到真正要用时才解码，这是 SR 一贯的「懒解码」风格。

#### 4.1.2 核心流程

一个插件配置从 YAML 到生效的流程：

```text
config.yaml: decisions[].plugins[]: { type, configuration }
        │  (加载期：作为 *StructuredPayload 原样存进 DecisionPlugin.Configuration)
        ▼
启动期校验 validator_plugin.validateDecisionPluginPayload
        │  ① IsSupportedDecisionPluginType(type)?     ← 查 routing_surface_catalog
        │  ② configuration 非空?
        │  ③ 用工厂构造对应结构体并 DecodeInto(/Strict)
        │  ④ 校验业务约束（如 fast_response 必须有 message）
        ▼
运行期（请求/响应阶段）
        │  decision.GetXxxConfig()  → decodeDecisionPlugin 泛型按需解码
        ▼
   命中本 decision 的请求才真正执行该插件逻辑
```

#### 4.1.3 源码精读

**插件的权威清单**集中在 `routing_surface_catalog.go`。受支持的 13 种类型既是常量，也是 `supportedDecisionPluginTypes` 切片的成员：

[src/semantic-router/pkg/config/routing_surface_catalog.go:L23-L35](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/routing_surface_catalog.go#L23-L35) —— 用常量集中声明 13 个插件类型字符串（`semantic-cache`/`system_prompt`/…/`tool_selection`）。

[src/semantic-router/pkg/config/routing_surface_catalog.go:L60-L74](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/routing_surface_catalog.go#L60-L74) —— `supportedDecisionPluginTypes` 是用于合法性判定的清单。注意 `tools` 也在此清单里，其常量 `DecisionPluginTools = "tools"` 定义在 `tools_plugin.go`。

> 这里的常量与清单是**唯一权威来源**。`DecisionPlugin` 结构体自己的注释也强调：「Type 是插件标识符；受支持的集合在 `routing_surface_catalog` 里登记，不在本结构体重复」。

此外有一个归一化小机制：旧写法 `semantic_cache`（下划线）会被映射成 `semantic-cache`（连字符）：

[src/semantic-router/pkg/config/routing_surface_catalog.go:L131-L146](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/routing_surface_catalog.go#L131-L146) —— `NormalizeDecisionPluginType` 查别名表归一化，`IsSupportedDecisionPluginType` 据此判定合法性。

**载体与访问器**在 `plugin_config.go`：

[src/semantic-router/pkg/config/plugin_config.go:L9-L17](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_config.go#L9-L17) —— `DecisionPlugin` 只有 `Type` 与 `Configuration`（`*StructuredPayload`）两个字段。

[src/semantic-router/pkg/config/plugin_config.go:L112-L126](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_config.go#L112-L126) —— `GetPlugin` 按类型在当前 decision 的 plugins 里线性查找；`HasPlugin` 复用它。注意查找前会先 `NormalizeDecisionPluginType`，所以下划线/连字符都能匹配。

[src/semantic-router/pkg/config/plugin_config.go:L224-L235](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_config.go#L224-L235) —— `decodeDecisionPlugin[T]` 是泛型访问器的统一实现：找到插件 → 解码成传入的结构体指针 → 解码失败返回 `nil`。所有 `GetXxxConfig()` 都只是给它套了个壳（如 `GetSystemPromptConfig`）。

各插件结构体字段举例（来自同一文件，仅列关键）：

[src/semantic-router/pkg/config/plugin_config.go:L55-L67](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_config.go#L55-L67) —— `SystemPromptPluginConfig`（`enabled`/`system_prompt`/`mode`）与 `HeaderMutationPluginConfig`（`add`/`update`/`delete` 三组 `HeaderPair`）。

[src/semantic-router/pkg/config/plugin_config.go:L91-L110](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_config.go#L91-L110) —— `RouterReplayPluginConfig`，字段较多，除了 `capture_request_body`/`capture_response_body`/`max_body_bytes`，还有为智能体场景设的 `max_tool_trace_bytes` 与 `max_tool_trace_steps`（防止失控的工具调用循环把进程撑爆 OOM）。

RAG 插件字段最多，单独成文件：

[src/semantic-router/pkg/config/rag_plugin.go:L6-L58](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/rag_plugin.go#L6-L58) —— `RAGPluginConfig`，含 `backend`（milvus/qdrant/external_api/mcp/openai/hybrid）、`similarity_threshold`、`top_k`、`injection_mode`（`tool_role`/`system_prompt`）、`on_failure`（skip/block/warn）等。

**配置示例**（来自 `config/plugin/`，每段都是可粘进某条 decision 的 `plugins[]` 的片段）：

```yaml
# config/plugin/system-prompt/expert.yaml  —— 注入专家 system 消息
plugin:
  type: system_prompt
  configuration:
    enabled: true
    mode: insert
    system_prompt: You are a domain expert. Answer precisely...
```

```yaml
# config/plugin/router-replay/debug.yaml  —— 录制路由重放，含 body 与工具链上限
plugin:
  type: router_replay
  configuration:
    enabled: true
    max_records: 10000
    capture_request_body: true
    capture_response_body: true
    max_body_bytes: 4096
    max_tool_trace_steps: 100
```

更多片段见 4.1.4 的实践。

#### 4.1.4 代码实践

**实践目标**：把 `config/plugin/` 下的片段与 `plugin_config.go` 里的结构体字段一一对应，建立「YAML 字段 ↔ Go 结构体字段 ↔ 语义」的映射。

**操作步骤**：

1. 打开 `config/plugin/request-params/budget-tier.yaml`，它声明了 `type: request_params`，字段有 `blocked_params`、`max_tokens_limit`、`max_n`、`strip_unknown`。
2. 在 `plugin_config.go` 里找到 `RequestParamsPluginConfig`（约 L42-L53），核对每个 YAML 键都能落到一个结构体字段上。
3. 打开 `config/plugin/response-jailbreak/strict.yaml`（`threshold: 0.85`、`action: block`），对照 `ResponseJailbreakPluginConfig`。
4. 打开 `config/plugin/rag/milvus.yaml`，对照 `RAGPluginConfig`，注意它多了 `backend_config`（一个嵌套对象，对应结构体里的 `*StructuredPayload`，因为不同 backend 形状不同，无法用固定字段表达）。

**需要观察的现象**：

- 除 `backend_config` 这类因后端差异而保留为「不透明 payload」的字段外，其余字段几乎都是一一对应的扁平映射。
- `rag` 的 `backend_config` 故意没有强类型，这正是 `StructuredPayload` 存在的意义——保留延迟解码的灵活性。

**预期结果**：你能为 `request_params`、`response_jailbreak`、`rag` 各写出一张「YAML 键 → Go 字段 → 作用」的三列表。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DecisionPlugin.Configuration` 用 `*StructuredPayload`（结构化字节）而不是直接用 `interface{}` 或某个大联合结构体？

**参考答案**：因为不同插件的配置形状差异很大（RAG 还有按 backend 变化的嵌套），用一个联合结构体会把所有字段塞在一起、耦合所有插件。`StructuredPayload` 把 payload 当作「规范化后的结构化字节」存下来，做到三件事：(1) 加载期零成本原样保存；(2) 启动校验期与运行期各自用需要的强类型按需 `DecodeInto`；(3) 跨 YAML/DSL/CRD 多个表面共用同一段字节表示。

**练习 2**：写出 `semantic_cache`（下划线）能被接受的原因。

**参考答案**：`IsSupportedDecisionPluginType` 先调 `NormalizeDecisionPluginType`，而 `pluginTypeAliases` 把 `"semantic_cache"` 映射成常量 `"semantic-cache"`，再拿归一化后的值去 `supportedDecisionPluginTypes` 里查，故下划线旧写法也能通过校验（见 routing_surface_catalog.go L110-L112、L131-L146）。

---

### 4.2 按 decision 启停：插件与决策的绑定

#### 4.2.1 概念说明

这是本讲最关键的一个设计：**插件不是全局开关，而是 decision 的私有配置**。

也就是说：

- 你不能在顶层写「全局启用幻觉检测」；
- 幻觉检测只对**声明了 `hallucination` 插件的那条 decision** 生效；
- 同一个 SR 里，A 决策可以开幻觉检测、B 决策可以不开，互不干扰。

这一点和 u3-l1 讲过的「插件为路由级、嵌在每条 `decisions[].plugins`，不存在顶层 plugins 段」完全一致。本模块讲的是它在**运行期**是怎么被严格执行的。

#### 4.2.2 核心流程

```text
请求进入 → 决策引擎选出「命中决策」→ ctx.VSRSelectedDecision = 命中的 *Decision
                                                  │
   请求/响应阶段的某个插件执行点：                  │
        读取 ctx.VSRSelectedDecision.GetXxxConfig()
            │
            ├── 命中决策没有声明该插件 → GetPlugin 返回 nil → 访问器返回 nil → 跳过
            └── 命中决策声明了该插件   → 解码 → 返回非 nil → 执行插件逻辑
```

核心判断只有一句：**`decision.GetPlugin(type) == nil` 就等于「这条决策没开这个插件」**。所有插件执行点都以这个访问器是否返回 `nil` 作为门控。

#### 4.2.3 源码精读

以 system_prompt 为例。执行入口先取命中决策，再取它的 system_prompt 配置，**任何一步为空就直接返回原 body 不改写**：

[src/semantic-router/pkg/extproc/req_filter_sys_prompt.go:L18-L37](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_sys_prompt.go#L18-L37) —— `addSystemPromptIfConfigured`：`decision := ctx.VSRSelectedDecision`；若 `decision == nil` 或 `GetSystemPromptConfig()` 返回 `nil` 或 prompt 为空，都直接返回原 body。这就是「按 decision 启停」。

RAG 的门控更完整，还叠加了业务条件：

[src/semantic-router/pkg/extproc/req_filter_rag.go:L403-L428](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/req_filter_rag.go#L403-L428) —— `resolveRAGPluginConfig`：依次判定「命中决策非空 → `GetRAGConfig()` 非空且 `Enabled` → `Backend` 非空 → `MinConfidenceThreshold` 满足」。任一不满足就返回 `(nil, false)`，调用方据此跳过 RAG。

> 注意门控里取的是 **`ctx.VSRSelectedDecision`**（命中决策），而不是「某条固定的决策」。这意味着：同一个配方里多条 decision 可以各自挂不同插件，请求只会触发它命中那条 decision 上的插件。这就是「按 decision 启停」的运行期体现。

**router_replay 的特殊合并**。绝大多数插件就是「有就是有、没有就是没有」，但 `router_replay` 多了一层「全局默认 + 按决策覆盖」：

[src/semantic-router/pkg/config/plugin_router_replay_support.go:L41-L77](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/config/plugin_router_replay_support.go#L41-L77) —— `EffectiveRouterReplayConfig`：先取 `DefaultRouterReplayPluginConfig()`（默认 `enabled=true`、捕获 body、`max_records=10000` 等，见 L16-L25），再叠加全局 `c.RouterReplay.Enabled`，若决策自身声明了 `router_replay` 插件，则把决策配置 `DecodeInto` 覆盖到 base 上；最终 `!base.Enabled` 返回 `nil`。

这条逻辑意味着 replay 有三种状态：全局开 + 决策不覆盖（用全局默认）、全局开 + 决策覆盖（用决策值）、全局关且决策没显式开（不录制）。

**启动期的「按决策建录制器」**。因为 replay 是按决策启停的，启动期要为每个启用 replay 的决策单独建一个 `Recorder`：

[src/semantic-router/pkg/extproc/router_replay_setup.go:L41-L65](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_replay_setup.go#L41-L65) —— `initializeIsolatedReplayRecorders` 遍历 `cfg.RoutingDecisionRefs()`，逐条取 `EffectiveRouterReplayConfig(decision)`，**返回 `nil` 就 `continue` 跳过**；命中的才用 `createReplayRecorder` 建录制器并以 `(recipe, decision)` 为键存进 map。

#### 4.2.4 代码实践

**实践目标**：验证「同一段配置里，插件只对声明它的决策生效」。

**操作步骤**：

1. 选 `config/recipes/balance/recipe.dsl`（或对应的 `config.yaml`），找到 2~3 条 decision，看它们的 `plugins` 各挂了什么（例如 `fast_qa` 可能挂 `semantic-cache`，而某条兜底 chat 决策可能挂 `router_replay`）。
2. 用 `vllm-sr validate --config config/recipes/balance/config.yaml` 校验通过（这条命令在 u3-l2 里讲过用法）。
3. 阅读上面的 `resolveRAGPluginConfig` / `addSystemPromptIfConfigured`，确认：即便全局有 RAG 能力，只要命中决策没声明 `rag`，`GetRAGConfig()` 就返回 `nil`，RAG 不会跑。

**需要观察的现象**：不同 decision 的 `plugins[]` 内容不同；某条 decision 没有某个插件类型是完全正常的，不会被校验器报错。

**预期结果**：你能用一句话回答「为什么 A 决策做了幻觉检测而 B 决策没有」——因为 B 的 `plugins[]` 里没有 `hallucination`，运行期 `GetHallucinationConfig()` 返回 `nil`。

**待本地验证**：是否每条配方都至少有一条 decision 不挂任何插件、纯做转发，可作为对照。

#### 4.2.5 小练习与答案

**练习 1**：如果想让「整个配方都默认录制 replay，但某一条决策关闭」，最少写多少配置？

**参考答案**：开启全局 `router_replay.enabled: true`（用 `DefaultRouterReplayPluginConfig` 的默认值即可，无需每条决策都写），然后只在要关闭的那条决策上挂 `router_replay` 插件并写 `enabled: false`。`EffectiveRouterReplayConfig` 会把决策的 `enabled:false` 解码覆盖到 base，最终返回 `nil`，该决策不录制。

**练习 2**：`GetPlugin` 在查找前为什么要先调 `NormalizeDecisionPluginType`？

**参考答案**：因为用户/旧配置可能用 `semantic_cache`（下划线）而受支持清单里是 `semantic-cache`（连字符）。归一化后比较，才能让两种写法都正确命中同一条插件配置，避免「配了却找不到」的静默错误。

---

### 4.3 链式处理：请求阶段与响应阶段的插件链

#### 4.3.1 概念说明

「链式处理」不是说有一个统一的 `PluginChain.Run()`，而是说：**请求阶段和响应阶段各有一串以固定顺序排列的、条件性的处理步骤，每一步都可能改写 body/header，或直接短路返回**。

把这些步骤串起来看，就是一条「请求进 → 一路改写 → 打到后端 → 响应回 → 一路检测/录制 → 回给客户端」的链。每一步是否启用，都由 4.2 的「按 decision 启停」决定。

#### 4.3.2 核心流程

**请求阶段**（在确定命中决策、选好模型之后，对请求体做改写）：

```text
modifyRequestBodyForAutoRouting(modifiedBody, decision, ...)  按顺序：
  1. 写入选中模型名（openAIRequest.Model = matchedModel）
  2. setReasoningModeToRequestBodyForProvider   ← reasoning 参数
  3. addSystemPromptIfConfigured                ← system_prompt 插件（按 decision）
  ---- 下面两步在调用方的不同分支里 ----
  4. injectMemoryMessages(ctx.MemoryContext)    ← memory 插件注入（若已检索到记忆）
  5. buildRequestParamsMutations                ← request_params 插件（剥离/校验参数）
createRoutingResponse：
  6. applyDecisionHeaderMutations               ← header_mutation 插件（add/update/delete 头）
另在 prepare 阶段：
  7. executeRAGPlugin                           ← rag 插件（检索+注入，按 decision）
  8. handleToolSelectionForRequest              ← tool_selection / tools 插件
  9. startRouterReplay                          ← router_replay 插件（捕获请求体）
```

> RAG 与记忆的「检索」发生在改写之前，而「注入」发生在改写链里；这样检索到的上下文才能被写进 body。`startRouterReplay` 放在改写完成之后，是为了把**改写后的最终请求体**录进去。

**响应阶段**（u5-l3 已讲过大部分，这里给出精确顺序，见源码）：

```text
handleNonStreamingResponseBody(responseBody, ctx)  按顺序：
  1. 解析 usage / 指标 / 更新缓存
  2. translateResponseBodyForClient   ← provider 归一化（OpenAI↔Anthropic / Response API）
  3. performResponseJailbreakDetection ← response_jailbreak 插件（可短路 block）
  4. performHallucinationDetection     ← hallucination 插件（可短路 block）
  5. scheduleResponseMemoryStore       ← memory 插件（异步存储，遇越狱跳过）
  6. markUnverifiedFactualResponse     ← 幻觉插件的「未核实事实」标记
  7. applyResponseWarnings             ← 把幻觉/未核实/越狱告警汇总进 x-vsr-response-warnings 头
  8. updateRouterReplayHallucinationStatus / attachRouterReplayResponse  ← router_replay 回写
```

#### 4.3.3 源码精读

**请求阶段编排**集中在 `modifyRequestBodyForAutoRouting`：

[src/semantic-router/pkg/extproc/processor_req_body_routing.go:L64-L115](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_routing.go#L64-L115) —— 注意 L94 调 `addSystemPromptIfConfigured`（system_prompt），L100-L105 用 `ctx.MemoryContext` 注入记忆，L107-L112 仅当 `GetRequestParamsConfig() != nil` 才做 request_params 改写。每一步都是「条件性」的。

[src/semantic-router/pkg/extproc/processor_req_body_routing.go:L160-L192](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body_routing.go#L160-L192) —— `createRoutingResponse` 在组装最终响应时调 `applyDecisionHeaderMutations`（L189），把 header_mutation 插件的 add/update/delete 应用到请求头。

请求体阶段的 `startRouterReplay` 调用点（auto 路由与 specified 模型路由各一处）：

[src/semantic-router/pkg/extproc/processor_req_body.go:L255-L265](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body.go#L255-L265) —— auto 路由分支：在写回 `ctx.RequestModel` 之后调 `startRouterReplay`，再处理工具选择与路由延迟记录。

[src/semantic-router/pkg/extproc/processor_req_body.go:L316-L328](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_req_body.go#L316-L328) —— specified 模型分支同样调 `startRouterReplay`，说明即便是客户端钉死模型的请求，只要命中决策开了 replay 也会录制。

**响应阶段编排**在 `handleNonStreamingResponseBody`，顺序非常清晰：

[src/semantic-router/pkg/extproc/processor_res_body_pipeline.go:L16-L47](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L16-L47) —— L33 `performResponseJailbreakDetection` 返回非 nil 即 `return`（短路 block）；L36 `performHallucinationDetection` 同理；L40 `scheduleResponseMemoryStore`；L43 `applyResponseWarnings`；L44-L45 更新 replay 的幻觉状态并把**响应体**回写给 replay 记录（`isFinal=true`）。

[src/semantic-router/pkg/extproc/processor_res_body_pipeline.go:L130-L162](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/processor_res_body_pipeline.go#L130-L162) —— `applyResponseWarnings` 把三类告警按固定顺序（幻觉 → 未核实事实 → 越狱）收集成 code 列表，写进单个 `x-vsr-response-warnings` 头（代码注释解释：越狱不改 body，所以它在序列里的位置只影响头的 code 顺序，不影响内容）。

#### 4.3.4 代码实践

**实践目标**：把请求阶段与响应阶段的插件执行顺序各画成一张时序图。

**操作步骤**：

1. 在 `processor_req_body_routing.go` 的 `modifyRequestBodyForAutoRouting` 里，依次标出 system_prompt、memory、request_params 三步的行号；
2. 在 `processor_req_body.go` 的 auto 路由分支里，标出 `startRouterReplay` 相对于 `handleToolSelectionForRequest` 的先后；
3. 在 `processor_res_body_pipeline.go` 的 `handleNonStreamingResponseBody` 里，标出越狱、幻觉、记忆、告警、replay 五步的先后，并用箭头标出哪两步「可短路 `return`」。

**需要观察的现象**：

- 请求阶段是「顺序改写 body/header」，每步把上一步的输出当输入（典型的 pipe）；
- 响应阶段是「先做能短路的检测（越狱/幻觉），再做不短路的副作用（记忆/告警/replay）」。

**预期结果**：两张时序图，并能指出「为什么越狱检测排在幻觉检测前面」——因为越狱是更严重的响应安全问题，且二者都可能短路，先查更重的问题可以让请求更早终止（具体优先级策略见 u8-l3 安全治理）。

**待本地验证**：流式响应（`handleStreamingResponseBody` 系列）的插件顺序是否与非流式一致——本讲只精读了非流式分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `startRouterReplay` 放在 `modifyRequestBodyForAutoRouting` 之后，而不是请求体一进来就调？

**参考答案**：放在改写之后，录制的是**最终发给后端的请求体**（已替换模型名、注入了 system_prompt / 记忆 / RAG 上下文、剥离了禁用参数），这样重放记录才有还原价值；若在最开始录，录到的是未经路由改写的原始 body。

**练习 2**：响应阶段 `performResponseJailbreakDetection` 与 `performHallucinationDetection` 都可能返回非 nil，这个非 nil 的语义是什么？

**参考答案**：返回非 nil 表示该插件决定**短路**——不再继续后续步骤，直接把这个 `*ProcessingResponse` 回给客户端（典型场景是 `action: block`，用一个错误/拦截响应替换原响应）。所以排在前面的越狱/幻觉检测一旦命中 block，后面的记忆存储、告警汇总就不会执行。

---

### 4.4 router_replay 的双阶段 body 捕获

#### 4.4.1 概念说明

`router_replay` 是 13 个插件里唯一**横跨请求与响应两个阶段**、且**记录带完整上下文**的插件。它的产物是一条「路由重放记录」（`RoutingRecord`），可以用来审计「这条请求当时为什么被路由到这个模型、信号/投影/决策是什么、进出 body 长什么样」。

它和语义缓存（u9-l3）的区别要分清：

- **语义缓存**：命中后**直接回包**，目的是省一次推理；
- **router_replay**：不影响请求走向，只是**旁路录制**，目的是可观测/审计/调试。

#### 4.4.2 核心流程

```text
请求阶段（startRouterReplay，幂等：仅首次有效）
  shouldStartRouterReplay?  ← ctx.RouterReplayPluginConfig.Enabled 且 ctx.RouterReplayID==""
       │ 是
       ▼
  resolveReplayRecorder(recipe, decision)  ← 按 (recipe,decision) 取专用 Recorder，回退全局
  configureReplayRecorder                  ← 把 capture_request_body / capture_response_body / 上限设进去
  buildReplayRoutingRecord                 ← 组装一条记录（信号/投影/决策元数据/请求体/Prompt/工具定义）
  persistReplayRecord → recorder.AddRecord ← 落盘（按 MaxBodyBytes 截断请求体），拿到 replayID 存进 ctx

响应阶段
  updateRouterReplayStatus       ← 写状态码、是否缓存命中、是否流式
  updateRouterReplayHallucinationStatus ← 写幻觉检测结果（若开了幻觉插件）
  updateRouterReplayUsageCost    ← 写 token 用量与成本
  attachRouterReplayResponse     ← recorder.AttachResponse(响应体)（按 MaxBodyBytes 截断）+ 更新工具链 + 标记 complete
```

两个关键点：

1. **幂等**：`startRouterReplay` 靠 `ctx.RouterReplayID == ""` 判定「还没录过」，所以即便一次请求链路上多个分支都调它（auto/specified/cache/looper…），也只会真正录制一次。
2. **结构化字段先于截断提取**：`Prompt` 与 `ToolDefinitions` 是在 body 被 `MaxBodyBytes` 截断**之前**从完整请求体里抽出来的，确保即使 raw body 被截，关键字段仍然完整。

#### 4.4.3 源码精读

**请求阶段捕获**：

[src/semantic-router/pkg/extproc/recorder.go:L102-L126](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L102-L126) —— `startRouterReplay`：`shouldStartRouterReplay` 门控 → 填充会话字段 → 取 Recorder → 设捕获策略 → 建记录 → 持久化。

[src/semantic-router/pkg/extproc/recorder.go:L128-L133](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L128-L133) —— `shouldStartRouterReplay` 的幂等判据：`RouterReplayPluginConfig` 非空且 `Enabled`，且 `ctx.RouterReplayID == ""`（尚未录过）。

[src/semantic-router/pkg/extproc/recorder.go:L250-L262](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L250-L262) —— `buildReplayRoutingRecord` 的尾部：先把 `ctx.OriginalRequestBody` 写进 `record.RequestBody`，再从完整请求体里抽 `Prompt` 与 `ToolDefinitions`（注释明确说是「在 `AddRecord` 截断之前」）。

[src/semantic-router/pkg/extproc/recorder.go:L346-L371](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L346-L371) —— `persistReplayRecord`：`recorder.AddRecord(record)` 落盘（这里发生按 `MaxBodyBytes` 截断），成功后把 `replayID` 与 Recorder 写回 `ctx`，并打印 `router_replay_start` 事件。

**响应阶段回写**：

[src/semantic-router/pkg/extproc/recorder.go:L398-L432](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/recorder.go#L398-L432) —— `attachRouterReplayResponse`：`recorder.AttachResponse(replayID, responseBody)` 存响应体（按 `MaxBodyBytes` 截断），合并工具链 `ToolTrace`，`isFinal` 时打印 `router_replay_complete` 事件。这就是「响应阶段捕获 body」的核心。

> 注意 4.3 里响应阶段编排的最后两步：`updateRouterReplayHallucinationStatus`（L44）与 `attachRouterReplayResponse(..., true)`（L45）。也就是说，响应体被回写给同一条 replay 记录，至此请求体 + 响应体 + 信号/投影/决策/成本都齐了，一条完整的重放记录闭合。

**捕获策略与上限**（来自 4.1 读过的配置字段，这里看它如何被注入 Recorder）：

[src/semantic-router/pkg/extproc/router_replay_setup.go:L104-L122](https://github.com/vllm-project/semantic-router/blob/7a77e1e1b1d5711d0b94e3c75bcea694a1d1f246/src/semantic-router/pkg/extproc/router_replay_setup.go#L104-L122) —— `createReplayRecorder`：`SetCapturePolicy(CaptureRequestBody, CaptureResponseBody, maxBodyBytes)` 决定是否抓 body 及抓多少；`SetMaxToolTraceBytes` / `SetMaxToolTraceSteps` 给结构化工具链字段单独设上限（独立于 `MaxBodyBytes`，避免 body 被截时工具链也跟着残缺）。

#### 4.4.4 代码实践

**实践目标**：解释 `router_replay` 如何在请求与响应两个阶段分别捕获 body，并拼成一条记录。

**操作步骤**：

1. 读 `startRouterReplay`（recorder.go L102-L126），确认它只做「请求侧」的事：建记录、写请求体、拿到 `replayID`；
2. 读 `attachRouterReplayResponse`（recorder.go L398-L432），确认它只做「响应侧」的事：用同一个 `replayID` 把响应体 `AttachResponse` 回去；
3. 在 `processor_res_body_pipeline.go` 的 `handleNonStreamingResponseBody` 里，定位 `attachRouterReplayResponse(ctx, finalBody, true)` 这一行，确认它在越狱/幻觉/记忆/告警之后，是响应链的最后一步之一；
4. 对照 `config/plugin/router-replay/debug.yaml`，把每个字段映射到代码里：`capture_request_body`/`capture_response_body` → `SetCapturePolicy`；`max_body_bytes` → `resolveReplayMaxBodyBytes`；`max_tool_trace_steps` → `SetMaxToolTraceSteps`。

**需要观察的现象**：

- 请求阶段只写不读响应；响应阶段只把响应体「补」到已存在的记录上；
- 即便 `capture_response_body: false`，记录里仍有信号/投影/决策/成本，只是没有响应体正文。

**预期结果**：你能画出 `replayID` 在 `ctx` 里的生命周期——请求阶段由 `startRouterReplay` 创建并写回 `ctx.RouterReplayID`/`ctx.RouterReplayRecorder`，响应阶段各 `updateRouterReplay*` 与 `attachRouterReplayResponse` 凭这两个字段找到同一条记录补全。

**待本地验证**：选一个支持 replay 的后端（`store_backend` 为 `memory` 时会打印「重启即丢失」的警告，见 router_replay_setup.go L180-L185），实际跑一条请求，从 API（u11-l1 会讲 apiserver）或日志里取出这条 replay 记录，确认 `request_body`/`response_body` 字段确实存在且被按 `max_body_bytes` 截断。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Prompt` 与 `ToolDefinitions` 要在 `AddRecord`（即 body 截断）之前提取？

**参考答案**：`AddRecord` 会按 `MaxBodyBytes` 截断 `RequestBody`，而 `Prompt`/`ToolDefinitions` 是从**完整**请求体里解析出的结构化字段，独立于 raw body。先提取再截断，可以保证即便 raw body 被截断到 4096 字节，结构化的 prompt 与工具定义仍然是完整的，重放时仍能还原语义（见 recorder.go L254-L260 的注释）。

**练习 2**：`max_tool_trace_steps` 这个上限是为哪种真实场景设计的？

**参考答案**：为「失控的智能体工具调用循环」设计——长会话里每个工具调用回合会产生一个 `ToolTraceStep`，若不加限制，一个无限循环（如某个 hermes 式死循环）会让单条记录的 tool trace 无限增长、把路由器进程撑到 OOM。超过上限时丢弃最老的 step（保留最近的）并置 `StepsTruncated` 标记（见 plugin_config.go L98-L109 的注释与 #1835）。

---

## 5. 综合实践

把本讲三件事——**插件目录、按决策启停、双阶段链式处理**——串起来做一个小任务。

**任务**：为 `balance` 配方里某条决策（例如 `fast_qa`）设计一个「最小插件组合」，让它同时具备：注入专家 system 消息、限制请求参数、录制重放。然后预测一次请求会按什么顺序被这些插件处理。

**步骤**：

1. 在该 decision 的 `plugins[]` 下粘三段配置，分别取自 `config/plugin/system-prompt/expert.yaml`、`config/plugin/request-params/budget-tier.yaml`、`config/plugin/router-replay/debug.yaml`（注意把 `plugin:` 顶层键改成 `plugins:` 数组元素，每段保留 `type` 与 `configuration`）。
2. 用 `vllm-sr validate --config <你的 config.yaml>` 校验，确认三段都被接受（即三个 `type` 都在 13 种受支持清单里）。
3. 写出预测的处理顺序：
   - 请求阶段：`addSystemPromptIfConfigured`（system_prompt）→ `buildRequestParamsMutations`（request_params）→ `applyDecisionHeaderMutations`（无 header_mutation，跳过）→ `startRouterReplay`（router_replay，捕获请求体）；
   - 响应阶段：`translateResponseBodyForClient` → 越狱/幻觉（未配置，跳过）→ `applyResponseWarnings`（无告警）→ `attachRouterReplayResponse`（router_replay，捕获响应体）。
4. 回答两个检验问题：
   - 如果把这套配置原样复制到**另一条**没有声明它们的决策上，会怎样？（答：那条决策不会被这些插件影响，因为运行期取的是各自命中决策的 `GetXxxConfig()`，为 `nil` 即跳过。）
   - 如果全局 `router_replay.enabled: false`，而这条决策显式写了 `router_replay` 插件 `enabled: true`，会录制吗？（答：会。`EffectiveRouterReplayConfig` 用决策配置覆盖全局，最终 `enabled=true` 返回非 nil。）

**待本地验证**：完整跑一条命中该决策的请求，确认日志里出现 `router_replay_start`（请求阶段）与 `router_replay_complete`（响应阶段）两个事件，且记录里同时含改写后的请求体与最终响应体。

## 6. 本讲小结

- SR 维护 **13 种受支持插件**，权威清单在 `routing_surface_catalog.go`，配置结构体与访问器在 `plugin_config.go`（RAG 因字段多单独成 `rag_plugin.go`）；`config/plugin/*/*.yaml` 是各插件的可粘贴片段示例。
- 插件**挂在每条 decision 的 `plugins[]` 上**，运行期靠「命中决策的 `GetXxxConfig()` 是否返回 `nil`」来门控——这就是「按 decision 启停」，不存在全局 plugins 段。
- 请求阶段是一条**顺序改写 body/header 的 pipe**（system_prompt → memory → request_params → header_mutation → … → router_replay 捕获请求体）；响应阶段是**先做可短路的检测（越狱/幻觉）、再做不短路的副作用（记忆/告警/replay 回写）**。
- `router_replay` 是唯一横跨两阶段的插件：请求阶段 `startRouterReplay` 建记录并写请求体（幂等、结构化字段先于截断提取），响应阶段 `attachRouterReplayResponse` 把响应体补回同一条记录，闭合出完整的重放审计轨迹。
- 启动校验（`validator_plugin.go`）保证插件类型受支持、`configuration` 非空、字段合法；其中 `semantic-cache` 与 `response_jailbreak` 用严格解码（拒绝未知字段），其余宽容解码。

## 7. 下一步学习建议

本讲把插件链的「骨架」讲完了，但每个插件的「肉」分散在各子系统里。建议按兴趣选读：

- **u10-l2（Prompt 压缩）**：插件链里 prompt 处理的进阶版——无 LLM 压缩的四信号打分。
- **u10-l3（工具检索与 MCP）**：本讲只点到 `tool_selection`/`tools` 插件的调用点，那里讲它们背后的语义工具库与 MCP 客户端。
- **u9-l3（语义缓存）**：本讲把 `semantic-cache` 当配置项带过，那里讲它命中即 `ImmediateResponse` 的完整机制，以及它为何和 RAG/memory 这类个性化插件互斥。
- **u11-l1（API Server 管理 API）**：想从外部查询/导出 `router_replay` 记录，要看 apiserver 暴露的相关端点。
- **u8-l3（PII 与越狱检测）**：本讲把 `response_jailbreak`/`hallucination` 当插件执行点，那里讲检测算法本身。
