# HTTP 前端装配：Config 逐项与路由扩展链

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐项说出 `serve_model_on_host_with_router_extension` 里那个完整的 `vllm_server::Config` 中与本教程相关的关键字段——`TransportMode::Bootstrapped`、`ListenerMode::BindTcp`、`tool_call_parser`、`stream_interval`、`disable_log_stats`——以及它们各自回答的装配问题。
2. 解释 `load_model_backends` 失败时为什么**只告警降级**（chat 请求整形关闭）而不终止服务：整形层与 server 各自加载一份后端，模型真坏由 server 报自己的错。
3. 描述路由扩展链的包裹顺序——最外层 `normalize_chat_requests`、内层 `mark_history`、最里层 `extend_router`——并论证 effort 规范化为什么必须在最外层。
4. 画出「CLI flag → SharedArgs 字段 → Config 字段」的传导链（至少 `--port` 与 `--tool-call-parser` 两条），并解释 echo-server 的 completion 文本为什么等于 prompt。
5. 跑通无 GPU 的 echo-server，用两条 curl 亲手观察装配结果。

本讲承接 u4-l1：上一讲立起了装配骨架（预绑定 socket、并发加载、计数对账、shutdown 级联），本讲 zoom 进骨架的后半段——`Config` 字面量（u4-l1 只点了两处）与路由扩展链那一行嵌套调用。

## 2. 前置知识

本讲需要以下概念（前几讲已建立的只做一句话回顾，新术语展开讲）：

- **装配骨架**（u4-l1）：`serve_model_on_host_with_router_extension` 是五个 serve 入口收拢到的唯一私有装配函数；它预绑定两个 unix socket、把引擎 Future 与 HTTP 前端并发跑、引擎 resolve 后对账起桥。本讲看它 B 线（前端时间线）的后半段。
- **`TransportMode::Bootstrapped`**（u4-l1）：上游 vLLM EngineCore 协议跑在 zmq 上；`Bootstrapped` 表示「监听 socket 已由别人绑好，我领养 fd」。它携带的两个 fd 与 `ready_timeout` 上一讲讲过，本讲把整个字段放进 Config 全景里。
- **`vllm_server::Config`**：上游 crate `vllm-server` 的启动配置结构体——一份「这台 HTTP 服务器是什么样」的完整声明。frontend 不改上游代码，只通过**逐项填这个结构体**来定制行为。字段名即上游 API，本项目对每个字段的选择都有注释说明。
- **axum 的 `Router` 与 `layer`**：axum 是 Rust 的 HTTP 框架；`Router` 是路由表。`router.layer(middleware)` 把**现有整个 router** 包进一层中间件洋葱——后包的在更外层，请求先穿过外层再到内层。`middleware::from_fn_with_state(state, async_fn)` 把一个普通 async 函数变成中间件，并携带一份状态（本讲里是测量器或别名表）。
- **`LoadedModelBackends`**：`vllm_chat::load_model_backends` 从模型目录加载的一组后端——chat 后端（模板渲染器）与文本后端（分词器）。它是两个请求整形层的原料。
- **`vllm_xargs`**：vLLM 请求体里一个预留的扩展参数对象（`"vllm_xargs": {...}`），随请求一路下行到引擎侧。本讲的 history 层把测量结果写进去，桥再把它翻译进 step 契约的 `Request`（u4-l3/u4-l4 展开）。
- **`ParserSelection`**（u2-l5）：工具调用输出解析器的选择——`Auto` / `None` / 显式名字。frontend 在 `mod.rs` L29 从上游 re-export 它，`SharedArgs` 与 `Config` 用的都是这一个类型。
- **SSE 流式响应**：Server-Sent Events，HTTP 长连接上以 `data: {...}\n\n` 帧逐段推送的流式协议；OpenAI 的 `stream: true` 走它。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [pegainfer-frontend/src/vllm/mod.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs) | **本讲主战场**：`load_model_backends` 调用与降级（L416-L435）、`Config` 逐项装配（L437-L491）、路由扩展链的嵌套调用（L493-L501） |
| [pegainfer-frontend/src/vllm/history.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs) | 路由链**内层**：`Boundary` 测量器（L95-L199）与 `mark_history` 中间件（L387-L450），测量结果写入 `vllm_xargs` |
| [pegainfer-frontend/src/vllm/reasoning_effort.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs) | 路由链**最外层**：effort 别名探测（L99-L164）与 `normalize_chat_requests` 中间件（L195-L234） |
| [pegainfer-frontend/examples/echo-server.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs) | 无 GPU 可跑的**活体标本**：完整的装配调用点（L222-L231）与 EchoScheduler（L99-L167），本讲实践的观察对象 |
| [pegainfer-frontend/src/vllm/wire.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/wire.rs) | **对照材料**：请求参数面的转换层——`unsupported_request_params`（L135-L201）体现与 Config 相同的「不静默吞掉无法表达的东西」原则 |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs) | CLI 链条起点：`SharedArgs` 的 `model_path` / `served_model_name` / `port` / `tool_call_parser`（L100-L120）与 `resolve_tool_call_parser`（L314-L322） |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs) | CLI 链条中转：L196-L199 提取四个共享值交给 serve 函数族 |

## 4. 核心概念与源码讲解

### 4.1 `vllm_server::Config` 逐项装配

#### 4.1.1 概念说明

u4-l1 讲骨架时把 L437-L491 的 `Config` 字面量一笔带过（只点了 `Bootstrapped` 与 `BindTcp` 两处）。本讲把它补全。这个结构体解决的问题是：**上游 `vllm-server` 是一个通用 HTTP 服务器，frontend 必须用一份完整的声明告诉它「这次要当什么样的服务器」**。

理解这份字面量的关键是分清三类字段：

1. **来自骨架的事实**——装配函数自己算出来的（fd、engine_count、ready_timeout、host/port）。
2. **来自 CLI 的选择**——经 `SharedArgs` → main.rs → serve 函数参数一路传进来的（port、tool_call_parser、model、served_model_name）。
3. **固定常量**——frontend 对上游某个能力点的刻意决定（`disable_log_stats: false`、`stream_interval: 1`、`manages_engine: false`……），每处都带注释说明为什么。

第三类最有教学价值：它们是本项目与上游的**契约点**——上游行为改了、或者本项目的决定改了，这些注释就是第一手依据。

#### 4.1.2 核心流程

Config 在 B 线（前端时间线）上的位置——u4-l1 流程图的后半段：

```
B 线：serve_with_engine_count(...)
  ├─ engine_count 检查 + 预绑定 socket + spawn engine_task     （u4-l1 已讲）
  ├─ load_model_backends(model_id, language_model_only=true)    L419-L433
  │     Ok(backends) ─→ Some(backends)
  │          ├─ probe_effort_aliases(model_id, &backends)        L434
  │          └─ boundary = Arc::new(Boundary::new(&backends))    L435
  │     Err(e) ─→ warn!("chat request shaping disabled: …") → None
  ├─ Config { …逐项装配… }                                       L437-L491
  └─ vllm_server::serve_with_router_extension(config, …, 链)    L493-L501
        加载 tokenizer + chat 模板（约 1s）→ 等引擎注册 → bind TCP
```

字段分组全景（值以代码为准，L437-L491）：

| 组 | 字段: 值 | 说明 |
|----|----------|------|
| 传输 | `transport_mode: Bootstrapped { input_listener_fd, output_listener_fd, engine_start_index: 0, engine_count, data_parallel_size: engine_count, ready_timeout: 30min }` | u4-l1 精读过：两个预绑定 fd + 引擎身份注册数 + 只兜底「真挂死」加载的超时 |
| 监听 | `listener_mode: BindTcp { host, port }` | TCP 侧声明；真正 bind 在引擎注册之后（「可达即就绪」） |
| 监听 | `grpc_listener_mode: None` | 只开 HTTP，不开 gRPC 面 |
| 生命周期 | `manages_engine: false` | 引擎由进程内桥自负盈亏，上游 server 不接管引擎进程生命周期 |
| 生命周期 | `shutdown_timeout: 10s` | 优雅关停的排水窗口 |
| 模型身份 | `model: model_id` | **加载标识**：server 用它自己再加载一份 tokenizer/模板 |
| 模型身份 | `served_model_name` | **对外名字**：`/v1/models` 与 completion 里 `model` 字段回显的名字（CLI `--served-model-name`） |
| 模型身份 | `revision: None` / `hf_overrides: default` / `generation_config: Auto` / `language_model_only: true` | 不钉版本、不覆写 config、生成参数按上游 Auto 语义取、只拉语言模型后端 |
| 解析器 | `tool_call_parser` | **CLI `--tool-call-parser` 的终点**（经 `resolve_tool_call_parser` 折算，见 4.1.3） |
| 解析器 | `reasoning_parser: ParserSelection::default()` | 推理解析器不单独配置 |
| 模板 | `renderer: default` / `chat_template: None` / `default_chat_template_kwargs: None` / `chat_template_content_format: default` / `limit_mm_per_prompt: {}` / `tool_strict_level: None` | 模板与渲染全用 checkpoint 自带的，CLI 不注入覆盖 |
| LoRA | `lora_modules: Vec::new()` | **刻意的空**：启动适配器走引擎侧 LoRA 通道，不走上游静态加载器（注释 L464-L465） |
| 运维 | `disable_log_stats: false` | 注释 L480-L482：上游把 scheduler-stats / request-event 记录器绑在这个 flag 上，而桥的 stats 正是 `/metrics` 导出的东西（u4-l6 承接） |
| 运维 | `stream_interval: NonZeroU32::MIN` | `NonZeroU32::MIN` 就是 1——流式下发粒度设为「每 1 个 token 推一个增量」 |
| 运维 | `keep_alive_timeout: DEFAULT_KEEP_ALIVE_TIMEOUT` | 上游默认的 keep-alive |
| 运维 | `max_logprobs: None` / `cors: default` / `api_keys: Vec::new()` / `profiler: None` / `tls: None` | 逐请求 logprobs 上限不在此设、无 API key 门、无 profiler 端点、明文 HTTP |
| 运维 | `api_server_options` | `enable_log_requests: true`、`enable_prompt_tokens_details: true`、其余（request-id 头、scale-out、逐请求指标、SSE keep-alive）全关 |

#### 4.1.3 源码精读

**重点字段一：`tool_call_parser`——一条从 CLI 到 Config 的完整链。**

[pegainfer-frontend/src/model_line.rs:L114-L120](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L114-L120) — 链条起点：`SharedArgs.tool_call_parser`，CLI 拼写 `--tool-call-parser`，默认 `ParserSelection::Auto`。文档注释点出 `Auto` 的坑：**上游的 `auto` 按模型路径的子串匹配**，目录名不该决定解析器——所以模型线可以在 `ServePlan` 里自带解析。

[pegainfer-frontend/src/model_line.rs:L314-L322](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L314-L322) — `ServePlan::resolve_tool_call_parser` 的折算规则（u2-l5 讲过）：显式选择原样通过；`Auto` 则换成线自带的 `auto_tool_call_parser`（若有），否则保持 `Auto`。

[src/main.rs:L196-L199](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L196-L199) — 链条中转：main 在 spawn 引擎加载**之前**就把四个共享值（`model_path`、`served_model_name`、`tool_call_parser`（已折算）、`port`）克隆出来，随后按分支塞进 serve 函数参数。

[pegainfer-frontend/src/vllm/mod.rs:L457-L459](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L457-L459) — 链条终点：`tool_call_parser` 落进 Config；紧邻的 `reasoning_parser` 用默认值——本项目不单独配置推理解析器。

**重点字段二：`listener_mode`——声明与事实的分离。**

[pegainfer-frontend/src/vllm/mod.rs:L456](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L456) — `HttpListenerMode::BindTcp { host, port }` 只是「将来要在哪里 bind」的**声明**；结合 u4-l1 讲过的顺序（bind 排在引擎注册之后），「端口可达 = 引擎就绪」这条语义由字段值与上游时序共同保证。host 固定 `"0.0.0.0"`（`serve_with_engine_count` L115 传入），CLI 侧没有 host flag。

**重点字段三：`stream_interval`——一个值就是一个策略。**

[pegainfer-frontend/src/vllm/mod.rs:L487](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L487) — `NonZeroU32::MIN` 恒等于 1：流式下发粒度设为 1，即每产出一个 token 就推一个 SSE 增量，不做攒批发送。这与 echo-server 刻意「每步一个 token」的调度器（见 4.1.4 末尾）是配套的观察点。

**重点字段四：`disable_log_stats`——为 `/metrics` 留的生命线。**

[pegainfer-frontend/src/vllm/mod.rs:L480-L483](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L480-L483) — 注释写明因果：上游把 scheduler-stats 与 request-event 两类**指标记录器**绑在这个 flag 上，而**桥上报的 stats 正是 `/metrics` 导出的内容**，周期性 stats 日志行也随它而来。所以这里必须是 `false`（不禁用）——u4-l6 的整条可观测性链都踩在这一个值上。

**其余字段的两个代表样本。**

[pegainfer-frontend/src/vllm/mod.rs:L464-L466](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L464-L466) — `lora_modules: Vec::new()` 是**刻意的空**：启动适配器走引擎侧 LoRA 通道（`load_startup_lora_modules`，u4-l5 展开），绝不走上游的静态加载器。同一个能力、两条互斥的实现路线，用一行常量做了选择。

[pegainfer-frontend/src/vllm/mod.rs:L469](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L469) 与 [L421-L424](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L421-L424) — 对称的一对：Config 的 `language_model_only: true` 与 `load_model_backends` 选项里的 `language_model_only: true`——**两处加载**（整形层的与 server 自己的）都只拉语言模型后端，行为对齐。

**对照材料：Config 的「启动面」与 wire.rs 的「请求面」。**

[pegainfer-frontend/src/vllm/wire.rs:L135-L146](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/wire.rs#L135-L146) — `unsupported_request_params` 的文档写着同一原则的请求侧版本：**拒绝前端无法忠实表达的请求参数，而不是静默忽略**（`min_tokens`、penalty 族、`min_p` 越界、`ec_transfer_params` 都被点名拒绝）。Config 逐项显式 + wire 逐项拒绝，合成这个服务面「不撒谎」的性格。这也回答了一个自然疑问：`max_logprobs: None`（启动面不设上限）并不妨碍请求带 logprobs——那是逐请求参数，由 wire 侧的 `requested_logprobs`（[wire.rs:L203-L209](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/wire.rs#L203-L209)）下降到引擎。

#### 4.1.4 代码实践：Config 字段溯源

1. **实践目标**：把 Config 的三十多个字段按「骨架事实 / CLI 选择 / 固定常量」分类，验证 4.1.2 的分组不是拍脑袋。
2. **操作步骤**：
   - 在仓库根目录执行 `rg -n "shared\.(port|tool_call_parser|served_model_name|model_path)" pegainfer-server/src/main.rs`，确认 CLI 值进入 serve 函数的全部入口（应命中 L196-L199 与各 serve 调用点）。
   - 对照 [mod.rs:L437-L491](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L437-L491)，给每个字段标注三类之一；拿不准的读它旁边的注释。
   - 特别检查：`rg -n "NonZeroU32::MIN" pegainfer-frontend/src` 验证 `stream_interval` 的写法，并写出它的数值。
3. **需要观察的现象**：能从 CLI 直达 Config 的字段极少（只有 `model`、`served_model_name`、`port`、`tool_call_parser` 四条链）；`transport_mode` 的 fd 与 engine_count 是骨架算的；其余全是带注释的常量决定。
4. **预期结果**：得到一张三列分类表；能回答「为什么 `api_keys` 是空的也能对外服务」（没有鉴权门是当前默认，部署侧需自行加层）与「`max_logprobs: None` 时请求怎么还带 logprobs」（逐请求参数走 wire 侧转换，见 4.1.3 对照材料）。

#### 4.1.5 小练习与答案

**练习 1**：`model` 与 `served_model_name` 都是「模型名字」，区别是什么？

**答案**：`model`（L451）是**加载标识**——上游 server 与整形层都用它定位模型目录、加载 tokenizer 和模板；`served_model_name`（L455）是**对外名字**——`/v1/models` 列出的、completion 响应里 `model` 字段回显的名字。CLI 只能改后者（`--served-model-name`）；前者来自 `--model-path`。

**练习 2**：如果把 `disable_log_stats` 改成 `true`，最直接损失什么？

**答案**：按注释 L480-L482，上游绑在这个 flag 上的 scheduler-stats / request-event 指标记录器会关闭，而**桥上报的 stats 正是 `/metrics` 导出的内容**——等于关掉了 u4-l6 整条可观测性链的出口，外加周期性 stats 日志行也没了。

**练习 3**：`stream_interval: NonZeroU32::MIN` 为什么不直接写 `1`？

**答案**：字段类型是 `NonZeroU32`——上游用类型表达「必须至少为 1」（0 会让流式永不更新）。`NonZeroU32::MIN` 是这个类型上表达 1 的惯用写法，语义与 `1` 等价，但类型对齐、不依赖隐式转换。

### 4.2 `load_model_backends`：整形层的模板加载与告警降级

#### 4.2.1 概念说明

Config 装配之前，装配函数先做了一次**自己的**后端加载（L419-L435）。它服务的是两个请求整形层（u4-l4 的主角，本讲只看它们的装载与挂载）：

- **history 边界测量**（`Boundary`）：需要 chat 渲染器 + 分词器，逐请求测量会话边界，结果经 `vllm_xargs` 下行；
- **reasoning effort 别名**（`EffortAliases`）：需要渲染器，启动时探测模板认哪些 effort 词。

这次加载失败时**只 warn 一声、置 `None` 继续跑**——chat 请求整形关闭，但服务照常起。为什么敢降级？因为**同一份后端 server 马上还会自己再加载一次**：整形层读的是「这台服务器要给 chat 请求附加什么增强」，server 读的是「没有它我连请求都答不了」的底线。模型真坏时，server 自己的加载会报自己的错、服务起不来——降级不是遮羞布，只是承认**两个加载器是两份代码，可能在边缘场景上一个成功一个失败**，不该让增强层的边缘失败杀死整个服务。

#### 4.2.2 核心流程

```
load_model_backends(&model_id, { language_model_only: true, ..default })   L419-L427
   │
   ├─ Ok(backends) ─→ backends = Some(backends)
   │      ├─ effort_aliases = probe_effort_aliases(&model_id, backends.as_ref())   L434
   │      │      ├─ 无 output_gate_type 标记 → 空表（探测直接跳过）
   │      │      ├─ 有标记但 backends=None → warn 后空表
   │      │      └─ 正常 → 逐候选值渲染探测，只保留 from 拒/to 收的别名
   │      └─ boundary = Some(Arc::new(Boundary::new(&backends)))                    L435
   │
   └─ Err(error) ─→ warn!("chat request shaping disabled: backends for {model_id}
                          did not load: {error}")
                      backends = None → effort_aliases 空、boundary=None          L428-L433

两个 None/空 各自独立地让 4.3 的对应中间件变成直通：
   boundary=None → mark_history 原样返回 router
   aliases 空    → normalize_chat_requests 原样返回 router
（/v1/completions 与其他路由从头到尾都不经过这两层。）
```

#### 4.2.3 源码精读

[pegainfer-frontend/src/vllm/mod.rs:L416-L435](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L416-L435) — 全段精读。注释 L416-L418 先立原则：「两个请求整形层读的是被服务 checkpoint 的模板与分词器，**在这里加载一次**；server 随后加载它自己的那份，模型真坏时报它自己的错」。`match` 的 Ok 臂把 backends 存成 `Some`，Err 臂只 `warn!`（错误原文带在日志里）后落 `None`。L434-L435 随即派生出两个整形层的装载：`probe_effort_aliases` 与 `Arc<Boundary>`。注意 `Boundary::new` 拆开 backends 拿渲染器与分词器（[history.rs:L100-L106](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L100-L106)）——它们**只在这里加载这一次**，中间件每次请求用的都是这份缓存。

[pegainfer-frontend/src/vllm/reasoning_effort.rs:L99-L115](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L99-L115) — `probe_effort_aliases` 的三重门：`probe_warranted` 先查 checkpoint 的 `output_gate_type` 标记（[L121-L147](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L121-L147)，只有 Qwen3.8 一代的存档带它），没有就整表关掉——**其他线连启动开销都不付**；有标记但 backends 是 `None`（即 4.2 的降级路径），warn 后返回空表；正常路径才逐候选值用**与路由同一个渲染器**渲染探测。这解释了「降级」的第二个粒度：整体加载失败是一种关，配置标记不在是更常见的关。

[pegainfer-frontend/src/vllm/reasoning_effort.rs:L149-L164](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L149-L164) — 探测算法本体：对每个候选值渲染一个最小请求，别名 `from -> to` 只有在「模板拒绝 from 且接受 to」时才启用——**激活与否由模板自己作证，绝不从 config 猜**。候选表只有三条（`high→xhigh`、`max→xhigh`、`minimal→low`，L72-L73）。

#### 4.2.4 代码实践：观察一次降级

1. **实践目标**：亲眼看到 `warn!("chat request shaping disabled: …")` 这行日志，并核对「server 随后报自己的错」的注释承诺。
2. **操作步骤**：
   - 建一个临时目录 `broken-model/`，只放一个 `config.json`（抄 `pegainfer-frontend/examples/echo-model/config.json` 的内容，`model_type` 保持 `"echo"`），**不放 `tokenizer.json`**。
   - 运行 `cargo run -p pegainfer-frontend --example echo-server -- --model-path <broken-model 的绝对路径>`。
   - 观察日志顺序。
3. **需要观察的现象**：先出现 `chat request shaping disabled: backends for … did not load: …`（frontend 侧的降级告警），随后 server 自己的加载也失败并报出**它自己的**错误（措辞以运行为准），进程退出。
4. **预期结果**：两条错误一前一后，正对应注释的两半——「整形层加载一次、失败仅告警」与「server 随后自己加载、真坏时报自己的错」。这个合成场景里模型确实坏了所以服务起不来；**降级设计针对的是两个加载器行为分叉的边缘场景**（一份成功一份失败），此时服务继续、只是 chat 整形关闭。**待本地验证**（需要能编译运行的本地环境）。

#### 4.2.5 小练习与答案

**练习 1**：降级发生时，`/v1/completions` 受影响吗？

**答案**：不受。两个整形中间件都只拦 `POST /v1/chat/completions`（`CHAT_COMPLETIONS_PATH`，reasoning_effort.rs L63），其他请求原样直通；降级只是让 chat 请求不再携带边界测量与 effort 改写。

**练习 2**：effort 别名探测为什么要用「与路由同一个渲染器」渲染探测请求，而不是读 config 里的某个字段？

**答案**：因为要回答的问题是「**这份模板**渲染时认不认这个值」，唯一权威是模板本身的 verdict。config 的标记（`output_gate_type`）只用来决定「要不要探测」（省启动开销），从不直接给出答案——注释明言「enabled is decided per deployment by `probe_effort_aliases`, never assumed from the config alone」。

**练习 3**：`Boundary` 为什么用 `Arc` 包起来？

**答案**：它要作为 axum 中间件的 **state**（`from_fn_with_state`）在并发请求间共享，而 `Boundary` 本身是不可变的（渲染器与分词器的动态分发句柄）——`Arc` 共享 + 不可变内容正好匹配「一次加载、每请求只读」的用法（mod.rs L435 与 history.rs L390-L392）。

### 4.3 路由扩展链：三层洋葱的包裹顺序

#### 4.3.1 概念说明

Config 装好后的最后一击是把「路由扩展链」交给上游：

```rust
vllm_server::serve_with_router_extension(config, server_shutdown, move |router| {
    reasoning_effort::normalize_chat_requests(
        history::mark_history(extend_router(router), boundary),
        effort_aliases,
    )
})
```

这行嵌套读起来由内向外：**最里层是调用者的定制 `extend_router`，往外一层是 history 打标 `mark_history`，最外层是 effort 规范化 `normalize_chat_requests`**。请求进入时则从外向内穿：effort 改写 → 边界测量 → 定制路由 → vLLM 上游路由。

这个顺序不是随意的。`mark_history` 要**用路由真正渲染时的值**去测量模板输出——如果 effort 会被改写（`high → xhigh`），测量必须发生在改写**之后**（更外层先执行），否则测出的是一个从未发生过的渲染。注释一句话点破：effort 规范化在最外层，**让边界测量读到路由将要渲染用的那个值**。

三层各自的可降级性也值得一提：`extend_router` 由调用者决定（默认分支干脆是恒等闭包）；`mark_history` 在 `boundary=None` 时直通；`normalize_chat_requests` 在别名表为空时直通——三层洋葱在 echo-server 上实际只有一层是活的（见 4.3.4）。

#### 4.3.2 核心流程

一个 chat 请求穿过洋葱的完整路径：

```
POST /v1/chat/completions（JSON body）
   │
   ▼ ① normalize_chat_requests（最外层）            reasoning_effort.rs L195-L200
   │    aliases 空 → 整层直通（echo-server 即此态）
   │    非 POST /v1/chat/completions → 直通
   │    缓冲 body（上限 128 MiB）→ 按别名表改写 reasoning_effort
   │    改写过 → 修 Content-Length → 放行
   │
   ▼ ② mark_history（内层）                        history.rs L390-L395
   │    boundary=None → 直通（4.2 的降级路径）
   │    非 POST /v1/chat/completions → 直通
   │    spawn_blocking 里测量（长会话渲染要秒级，不能占 async worker）
   │    写 vllm_xargs.generation_prompt_tokens / history_marks → 修 Content-Length
   │
   ▼ ③ extend_router（最里层，调用者的定制）          mod.rs L493-L499 的实参
   │    默认分支：恒等（serve_model_on_host L252）
   │    prefill-only：挂 max_tokens=1 校验门（u4-l5）
   │    LoRA：合并 LoRA 路由 + OpenAI 路由（u4-l5）
   │
   ▼ ④ vLLM 上游路由
        渲染 chat 模板 → EngineCoreRequest → zmq → 桥（u4-l3）
```

#### 4.3.3 源码精读

[pegainfer-frontend/src/vllm/mod.rs:L493-L501](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L493-L501) — 链的铸型处：`vllm_server::serve_with_router_extension` 拿 config、`server_shutdown` 与一个 `FnOnce(Router) -> Router` 闭包；闭包体就是那行三层嵌套。注释（L494-L495）给出顺序的唯一理由：**effort 规范化在最外层，使边界测量在路由将要渲染用的值下进行**。

[pegainfer-frontend/src/vllm/reasoning_effort.rs:L195-L226](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L195-L226) — 最外层的实现：`normalize_chat_requests` 在别名表为空时**原样返回 router**（一个零开销直通）；否则挂 `from_fn_with_state` 中间件。`normalize_request` 先做方法与路径过滤（只有 `POST /v1/chat/completions` 被拦），再缓冲 body（`BODY_LIMIT` = 128 MiB，L67——与 LoRA 路由同限：**要改写的 body 必须整体进内存，超限请求宁可拒绝不可截断**），改写成功后**重写 `Content-Length` 头**再放行——body 长度变了，头不重写下游就解析错了。

[pegainfer-frontend/src/vllm/history.rs:L387-L395](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L387-L395) — 内层的挂载：`mark_history` 按 `boundary` 是否存在二选一——`Some` 挂中间件，`None` 原样返回。文档注释点明这就是 4.2 降级的落点：「backends 没加载起来的 server 原样服务这个 router」。

[pegainfer-frontend/src/vllm/history.rs:L397-L429](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L397-L429) — `mark_request` 的三段式：同样的方法/路径过滤与 body 缓冲；然后**把测量丢进 `spawn_blocking`**（注释 L412-L413：「对 1M token 的会话渲染并 tokenize 最多 MARKS 个前缀要花秒级，不能占 async worker」）；最后若 body 变了同样修 `Content-Length`。测量细节（怎么算 `T`、marks 怎么取）是 u4-l4 的主课。

[pegainfer-frontend/src/vllm/history.rs:L431-L450](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L431-L450) — `mark_generation_prompt`：把测得的两个数写进 body 的 `vllm_xargs`（键名常量 `GENERATION_PROMPT_TOKENS` / `HISTORY_MARKS`，L84-L89；最多保留 `MARKS = 8` 个更早边界，L92）。文档注释特别声明：**调用方自带的这两个键会被覆盖**——「它们是本服务器对**自己模板**的测量值，不是请求参数」。这也是「为什么必须在最外层改写 effort」的另一面：测量只认最终下行的 body。

[pegainfer-frontend/src/vllm/mod.rs:L251-L253](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L251-L253) — 最里层的默认形态：`serve_model_on_host` 给骨架传的 `extend_router` 就是恒等闭包 `|router| router`——普通服务面没有任何定制路由，洋葱只有外面两层。

[pegainfer-frontend/src/vllm/mod.rs:L207-L227](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L207-L227) — 最里层的最复杂形态：LoRA 分支的 `extend_router` 在路由内层合并 LoRA 专用路由与带适配器校验的 OpenAI 路由（`merge` + `fallback_service`，u4-l5 精读）。注意它被包在 history 与 effort 两层**之内**——LoRA 路由改变的是「哪些路由存在」，而 chat 整形对所有 chat 请求一视同仁，两层在外不冲突。

#### 4.3.4 代码实践：数一遍洋葱的三个传入点

1. **实践目标**：确认 `extend_router` 这个最内层钩子的全部传入者，并弄清 echo-server 上洋葱哪几层是活的。
2. **操作步骤**：
   - 在仓库根目录执行 `rg -n "with_router_extension" pegainfer-frontend/src pegainfer-server/src`，把命中点分成两组：frontend 自己的装配函数（mod.rs L129/L143/L242/L257/L493）与上游调用（mod.rs L493）。
   - 逐一核对传给骨架 L257 的每个闭包：L252（恒等）、L179（`request_contract::prefill_only_routes`）、L217-L226（LoRA 合并）。
   - 回答：echo-server（fixture 的 config.json 没有 `output_gate_type`、tokenizer 正常加载）上，三层各是什么状态？
3. **需要观察的现象**：`extend_router` 恰有三个传入形态（恒等 / prefill 门 / LoRA 合并）；echo-server 走 `serve_with_engine_count` → 恒等定制。
4. **预期结果**：echo-server 上的洋葱——① effort 规范化**直通**（别名表空：探测被 `probe_warranted` 跳过）；② history 打标**活跃**（tokenizer 加载成功，`boundary` 是 `Some`，chat 请求会被测量并写入 xargs，只是对 echo 行为无感）；③ 定制**恒等**。三条 curl 里只有 chat 请求会穿过全部三层。

#### 4.3.5 小练习与答案

**练习 1**：把嵌套改写成 `mark_history(normalize_chat_requests(extend_router(router)), boundary)`（effort 移到内层）会出什么问题？

**答案**：边界测量会读到**未被改写的** `reasoning_effort`（比如 `high`），用它渲染模板测量 `T`；而真正到达路由的 body 已被内层改写成 `xhigh`——路由用 `xhigh` 渲染出的 prompt 与测量的 prompt 不同，`history_tokens = prompt_len - T` 就建立在错误的 `T` 上。顺序注释（L494-L495）防的就是这个。

**练习 2**：两个中间件改完 body 为什么都必须自己修 `Content-Length`？

**答案**：axum/tower 的中间件拿到的是已解析的 `Request`，body 被整体换成新 `Bytes` 后，`parts.headers` 里的 `Content-Length` 还是旧长度——下游按头读定长 body 会截断或错位。所以两处都在改写后 `headers.insert(CONTENT_LENGTH, …)`（reasoning_effort.rs L217-L222、history.rs L420-L425）。

**练习 3**：`mark_history` 的测量为什么放在 `spawn_blocking`，而 effort 改写不用？

**答案**：effort 改写只做 JSON 解析与一次字段替换（微秒级）；history 测量要**渲染并 tokenize** 最后一条消息与最多 `MARKS` 个会话前缀——注释明说 1M token 的会话要花秒级，放在 async worker 上会把整个 runtime 的其他任务饿死，所以丢进阻塞线程池（history.rs L412-L419）。

## 5. 综合实践

把三块内容串成一个**无 GPU 的活体实验 + 一份映射笔记**：

**任务：跑通 echo-server，用两条 curl 验证装配结果**

1. **启动**：在仓库根目录运行 `cargo run -p pegainfer-frontend --example echo-server`（文件头注释 L11-L14 的官方运行方式；模型是内置 fixture，无 GPU、无权重）。看到 `serving the echo model on port 8000 …` 即就绪（echo-server.rs L218-L221）。
2. **打 /v1/completions**（文件头注释 L18-L21 的原文 curl）：

   ```bash
   curl http://127.0.0.1:8000/v1/completions \
     -H 'content-type: application/json' \
     -d '{"model": "echo", "prompt": "hello, pegainfer", "max_tokens": 64}'
   ```

   预期（注释 L22 明言）：**completion 文本就是 prompt 原文** `"hello, pegainfer"`。对照源码解释：前端把 prompt 分词成 token id 下行；EchoScheduler 对每个请求取 `echo_len = min(prompt_tokens.len(), max_tokens)`，把 `prompt[..echo_len]` 反转存为待发队列（[echo-server.rs:L127-L130](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L127-L130)），此后每步弹出一个 token 经账本 `push_tokens` 上行（[L144-L145](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L144-L145)），发完按是否被 `max_tokens` 截短选 `Stop`/`Length` 结束（[L146-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L146-L153)）。引擎吐回的就是 prompt 的 token id，前端再分词还原——而 fixture 的分词器是字节级（vocab 256），一来一回文本不变。
3. **打流式 /v1/chat/completions**（示例代码，文件头未给 chat 的 curl；fixture 的 config.json 没有 chat 模板，路由如何渲染待本地验证）：

   ```bash
   curl -N http://127.0.0.1:8000/v1/chat/completions \
     -H 'content-type: application/json' \
     -d '{"model": "echo", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 64, "stream": true}'
   ```

   观察逐段到达的 SSE `data:` 帧（对应 `stream_interval: 1` 的逐 token 粒度）；并注意这个请求**穿过了完整的洋葱**（effort 直通 → history 打标活跃 → 恒等定制）。
4. **整理映射笔记**：仿照 4.1.3 的 `--tool-call-parser` 链，写出至少两条「CLI flag → SharedArgs 字段 → serve 参数 → Config 字段」的完整链：
   - `--port`：`SharedArgs.port`（默认 8000）→ main.rs L199 → serve 参数 `port` → `listener_mode: BindTcp { host: "0.0.0.0", port }`（L456）；
   - `--tool-call-parser`：`SharedArgs.tool_call_parser` → `plan.resolve_tool_call_parser` 折算（显式 > 线默认 > Auto）→ serve 参数 → `tool_call_parser`（L457）；
   - 可选加练：`--served-model-name` → `served_model_name`（L455）、`--model-path` → `model`（L451）。
5. **验收标准**：不看讲义能回答——completion 为什么等于 prompt（token 级回声 + 字节级分词器）；`--port 9000` 从 CLI 到 TCP bind 经过了哪四个源码位置；chat 请求与 completions 请求在洋葱上的路径差在哪。

## 6. 本讲小结

- `Config` 是 frontend 对上游 `vllm-server` 的完整声明，字段分三类：骨架事实（fd、engine_count、ready_timeout）、CLI 选择（`model`、`served_model_name`、`port`、`tool_call_parser` 四条链）、带注释的固定常量（`disable_log_stats: false`、`stream_interval: 1`、`manages_engine: false` 等）。
- `load_model_backends` 给两个 chat 请求整形层加载模板与分词器，失败**只告警降级**：整形层是增强，server 随后自己再加载一份、模型真坏时报自己的错——降级针对的是两个加载器可能的行为分叉，不是遮羞布。
- effort 别名表**由模板自己作证**：`output_gate_type` 标记只决定要不要探测，激活与否看「模板拒绝 from 且接受 to」的渲染 verdict；其他线零启动开销。
- 路由扩展链是三层洋葱：最外 `normalize_chat_requests`、内层 `mark_history`、最里 `extend_router`（恒等 / prefill 门 / LoRA 合并三种形态）；effort 必须最外，因为边界测量要读到**路由将要渲染用的值**。
- 两个中间件只拦 `POST /v1/chat/completions`，共享同一套纪律：128 MiB 缓冲上限（宁拒不截断）、改写后重写 `Content-Length`；history 测量因秒级渲染放进 `spawn_blocking`。
- echo-server 是本讲的活体标本：completion 等于 prompt（token 级回声），chat 请求穿过全链而 effort 层直通、history 层活跃。

## 7. 下一步学习建议

1. **u4-l3（两代引擎桥）**：本讲到「渲染 chat 模板 → EngineCoreRequest → zmq」为止；下一讲进桥的内部——zmq dealer/push 双 socket 如何接上本讲的预绑定 fd、legacy 桥怎么 demux 事件流、stepped 桥怎么把 `StepOutputs` 1:1 翻译成 `EngineCoreOutputs`，以及 `vllm_xargs` 里的两个测量值怎么落进 `Request`。
2. **u4-l4（请求整形）**：本讲只装了测量器；下一讲精读测量算法本身——`T` 的带/不带 generation prompt 双渲染差分、`history_marks` 的更早边界取法，以及 `history_tokens = prompt_len - T` 在桥侧的推导。
3. **回看 u2-l5**：`tool_call_parser` 的折算规则（显式 > 线默认 > Auto）与「上游 `auto` 按路径子串匹配」的坑在 `SharedArgs` 的文档注释里——如果本讲的链条起点已经模糊，回去重读那一讲。
