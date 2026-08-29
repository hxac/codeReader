# 路由概览与 Python 入口：dynamo.router

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 Dynamo 中**三种路由部署形态**（frontend 内嵌 Rust 路由 / frontend 内 Python processor + 内嵌路由 / 独立路由进程），以及 `python -m dynamo.router` 属于哪一种、适合什么场景。
2. 追踪独立路由进程的启动链：`args.py` 解析参数 → `StandaloneRouterHandler` 初始化 → 注册 `generate` / `best_worker_id` / `get_overlap_scores` 三个端点。
3. 说出 `router_mode` 七个选项（round-robin / random / power-of-two / kv / direct / least-loaded / device-aware-weighted）各自的行为，并解释 `ROUTER_MODE_MAP` 如何把 CLI 拼写映射到 Rust 枚举 `RouterMode` 的属性名。
4. 弄清一个容易踩的坑：**独立 `dynamo.router` 并不接收 `--router-mode`**——`--router-mode` 属于 `dynamo.frontend` 与 worker 模型卡片广告的参数族，standalone router 固定走 KV 感知路由。
5. 解释 Python 侧的 `KvRouter` 对象如何穿过 PyO3 边界，最终落到 Rust 的 `RoutingHost`。

本讲是第 6 单元「KV 感知路由」的入口篇：先把「路由进程长什么样、参数从哪来」讲清楚，下一讲（u6-l2）才深入 Rust 路由核心的打分与调度。

## 2. 前置知识

### 2.1 什么是「路由」，Dynamo 在路由什么

一个推理服务背后往往有多张 GPU、多个 worker 进程。当一个请求到达时，**选哪个 worker 来处理**就是路由决策。最朴素的做法是轮询（round-robin），但它浪费了一个推理服务里最值钱的信息：**KV 缓存**。

回顾 u4-l3 讲过的块哈希数学：请求的 token 序列会被切成固定大小的块（block），每块算出一个 `BlockHash`，并且**相同前缀 ⇒ 相同前缀块哈希**。如果某个 worker 上周已经算过「这篇长文档 + 问题前半段」的注意力，那么 KV 缓存里就存着这些块的键值——同样的前缀再发过去，prefill 就可以跳过前缀部分的计算。

于是好的路由策略是：**把请求发给「缓存里已经有这段前缀」的 worker**。衡量「已有多少」的指标叫 **KV 重合度（overlap）**——请求的块哈希序列与 worker 已缓存块的交集块数。

### 2.2 router_mode 与它的两个消费方

`router_mode` 是一个字符串配置（CLI 形如 `--router-mode kv`，环境变量 `DYN_ROUTER_MODE`），描述「这一组请求用哪种选 worker 的策略」。它有两个不同的消费方，初学者常把它们混为一谈：

- **frontend 消费**：`python -m dynamo.frontend --router-mode kv`，frontend 进程内建路由器，按该模式选点。
- **worker 广告（advertisement）消费**：worker 启动时也可以带路由参数，这些参数写进它的 ModelDeploymentCard（模型部署卡，见 u4-l4），**只覆盖这一组 worker**，优先级高于 frontend 的全局设置。

而独立路由进程 `dynamo.router` 是第三种东西：它是一个**独立进程**，自己持有一个 KV 感知路由器，通过 Dynamo 运行时端点对外提供服务，**不提供 OpenAI 兼容 HTTP**。

### 2.3 ArgGroup 参数组机制（承接 u5-l1）

u5-l1 讲过 frontend 的 `FrontendConfig` 参数体系：用 `ArgGroup` 把一组参数定义成可复用的类，`add_argument` 统一封装「CLI 标志 + 环境变量 + 默认值」三件套，配置基类多继承组合，`from_cli_args` 填充。本讲的 `dynamo.router` 用同一套机制，只是组合的组不同——这正是「共享参数定义一次、两个组件复用」设计的受益者。

### 2.4 术语速查

| 术语 | 含义 |
|---|---|
| standalone router | 独立路由进程 `python -m dynamo.router`，本身不是 HTTP 服务器 |
| ModelDeploymentCard | worker 注册时附带的「名片」，含模型名、块大小、worker 类型、可带路由配置广告 |
| dp_rank | 数据并行秩；同一 worker 进程内多个引擎副本各有 rank |
| 事件面（event plane） | KV 事件等消息的传输通道，NATS 或 ZMQ（u3-l5） |
| 不透明信封 | 本讲指 `generate` 端点收发的普通 Python dict，路由器只认其中的固定字段名 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [components/src/dynamo/router/__main__.py](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py) | 独立路由进程入口：`StandaloneRouterHandler` + 三个端点 |
| [components/src/dynamo/router/args.py](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py) | 路由进程自己的参数（`--endpoint`、`--router-block-size`）+ 组装共享参数组 |
| [components/src/dynamo/common/configuration/groups/router_args.py](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py) | 共享路由参数组：`--router-mode`、`ROUTER_MODE_MAP`、`build_router_config` |
| [components/src/dynamo/common/configuration/groups/kv_router_args.py](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py) | 共享 KV 路由参数组：所有 `--router-*` 调优项 |
| [lib/bindings/python/rust/llm/kv.rs](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs) | PyO3 侧 `KvRouter` 类：从 Python 对象到 Rust `RoutingHost` |
| [components/src/dynamo/router/AGENTS.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/AGENTS.md) | 仓库内对该目录的架构说明：三种路由拓扑边界 |
| [docs/fern/pages/cli/kv-aware-routing/standalone-router.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md) | 官方 standalone router 使用文档（本讲实践的主要依据） |
| [components/src/dynamo/mocker/args.py](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/args.py) | mocker 假后端参数：默认 endpoint、块大小、worker 数 |

## 4. 核心概念与源码讲解

### 4.1 路由的三种部署形态与 standalone router 的定位

#### 4.1.1 概念说明

Dynamo 的路由逻辑（打分、选点、记账）写在 Rust 里，但它可以**部署在三个不同的位置**。仓库在 [components/src/dynamo/router/AGENTS.md:9-53](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/AGENTS.md#L9-L53) 里把这三种拓扑写得很清楚：

1. **Integrated Rust frontend（内嵌 Rust 路由）**：`dynamo.frontend --router-mode kv`。请求路径是 `frontend → Rust OpenAIPreprocessor → 进程内 Rust RoutingHost/KvRouter → worker → Rust DeltaGenerator`。**没有路由 RPC 这一跳**——路由发生在 frontend 进程内部。
2. **frontend 内 Python chat processor**：`--dyn-chat-processor vllm|sglang`（u5-l3 讲过）。前后处理换成 Python，但路由**仍然**是 frontend 进程内的 Rust 路由器。
3. **Standalone 或自定义 Python 路由服务**：`frontend → 路由服务 RPC → 绑定层 KvRouter → worker`。路由器住在**另一个进程**里，例如 `python -m dynamo.router` 和 `python -m dynamo.thunderagent_router`。

为什么需要第 3 种？官方文档 [standalone-router.md:8-21](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L8-L21) 给出的场景是：手工分离式服务（手动编排 prefill 路由）、多层架构、自定义请求管线，或者**任何「选点逻辑要放在 HTTP frontend 之外」**的场合。反过来，普通的 `/v1/chat/completions` 服务用第 1 种就够了。

#### 4.1.2 核心流程

三种拓扑的对比可以用一张表概括：

| 拓扑 | 启动方式 | 路由发生在哪 | 有无跨进程路由 RPC | 典型场景 |
|---|---|---|---|---|
| 内嵌 Rust | `dynamo.frontend --router-mode kv` | frontend 进程内 | 无 | 标准聚合/分离式服务 |
| Python processor | `dynamo.frontend --dyn-chat-processor vllm` | frontend 进程内 | 无 | 复用 vLLM/SGLang 原生前后处理 |
| standalone | `python -m dynamo.router --endpoint ...` | 独立路由进程 | 有（客户端 → 路由端点） | 高级 prefill 路由、自定义管线 |

standalone router 的生命周期：

```text
python -m dynamo.router --endpoint <ns>.<comp>.<ep>
  └─ main() 用 uvloop 启动
      └─ worker(runtime)                          # @dynamo_worker() 装饰
          ├─ parse_args() → DynamoRouterConfig    # 参数解析 + validate()
          ├─ build_kv_router_config / build_aic_perf_config
          ├─ StandaloneRouterHandler(...)          # 持有配置
          │   └─ initialize()
          │       ├─ runtime.endpoint("<ns>.<comp>.<ep>").client()   # 等待 worker 出现
          │       └─ KvRouter(endpoint, block_size, config, ...)      # 建 KV 路由器
          └─ asyncio.gather(三个 serve_endpoint)   # 对外服务
```

#### 4.1.3 源码精读

先看入口文件的自我介绍。[__main__.py:4-13](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L4-L13) 的模块文档写明：这是一个 standalone KV-aware router 服务，用法是 `python -m dynamo.router --endpoint <namespace.component.endpoint> [args]`，可用于分离式服务（如路由到 prefill worker）或任何需要 KV 缓存感知路由的场合。

三种拓扑的原文描述在 [components/src/dynamo/router/AGENTS.md:11-53](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/AGENTS.md#L11-L53)：第 11 行起是「Integrated Rust frontend」（强调 `--router-mode kv` 用进程内 Rust 路由、无路由 RPC 跳），第 24 行起是「Python chat processor inside dynamo.frontend」（强调仍用内嵌 Rust 路由），第 39 行起是「Standalone or custom Python router service」（强调 `python -m dynamo.router` 在**另一个进程**持有绑定层 `KvRouter`）。

该文档还有一个对后续单元极重要的告诫（[AGENTS.md:55-57](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/AGENTS.md#L55-L57)）：**不要因为三种拓扑共享 `KvRouter` 逻辑，就推断元数据也会自动传播**。任何要跨「Python 序列化 / RPC / 进程边界」的值，都必须在请求和响应两个方向上有显式的传输路径。本讲 4.2 会看到 `generate` 端点如何手工搬运 `routing_data`——正是这条纪律的体现。

官方文档对三种形态的选择建议见 [standalone-router.md:14-21](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L14-L21)：frontend 内嵌路由负责常规 HTTP 服务路径（它拥有 `/v1/chat/completions` 并在进程内路由）；standalone 路由用于需要独立路由组件、面向特定 Dynamo endpoint 的场合，**它不提供 OpenAI 兼容 HTTP，客户端通过 Dynamo 运行时调用它**。

#### 4.1.4 代码实践

**实践目标**：用 `--help` 直接观察 standalone router 的参数面，验证「它没有 `--router-mode`」。

**操作步骤**：

1. 在装好 `ai-dynamo` 的环境里执行：

   ```bash
   python -m dynamo.router --help
   ```

2. 在输出的参数分组里找三个组：`Dynamo Router Options`、`KV Router Options`（以及 AIC 性能参数组）。
3. 用 grep 过滤，确认哪些前缀存在、哪些不存在：

   ```bash
   python -m dynamo.router --help 2>&1 | grep -oE '\-\-[a-z-]+' | sort -u
   ```

**需要观察的现象**：

- 出现 `--endpoint`、`--router-block-size`、`--serve-indexer`、`--router-kv-events`、`--load-aware` 等一批 `--router-*` 调优项。
- **不出现** `--router-mode`，也没有 `--router-session-affinity-ttl-secs`、`--active-decode-blocks-threshold`（这些属于 `RouterArgGroup`，见 4.3.3，standalone router 没有挂载这个组）。

**预期结果**：你会得到一个「全是 KV 调优项、没有模式选择」的参数面。这与源码一致：[args.py:93-127](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L93-L127) 的 `DynamoRouterArgGroup.add_arguments` 只注册了 `--endpoint`、`--router-block-size`、`--serve-indexer` 三个自有参数，再挂上 `KvRouterArgGroup` 与 `AicPerfArgGroup` 两个共享组——从头到尾没有出现 `RouterArgGroup`。

（具体输出以本机安装的版本为准，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：你的同事说「我在 `dynamo.router` 上加了 `--router-mode round-robin` 但好像没生效」。最可能的原因是什么？

**答案**：`dynamo.router` 根本不注册 `--router-mode` 这个参数。如果 argparse 没有报错，说明该标志被别的东西吃掉了（或他其实运行的是 `dynamo.frontend`）。standalone router 固定走 KV 感知路由；想要 round-robin 对照，要么在 frontend 上设 `--router-mode round-robin`，要么直接用 `endpoint.client()`（其默认路由模式就是 RoundRobin，见 4.4.3）。

**练习 2**：三种拓扑里，哪一种会产生「路由 RPC」这一网络跳？为什么这反而可能是有益的？

**答案**：第三种（standalone / 自定义路由服务）。前两种路由都发生在 frontend 进程内。独立成进程的好处是：路由可以独立扩缩、被多个 frontend 或自定义调度器复用、并且可以在不重启 frontend 的情况下单独升级/替换选点策略——代价是多一次跨进程往返和序列化。

**练习 3**：判断对错：「因为三种拓扑共用同一个 `KvRouter` Rust 类型，所以 `RequestTracker` 里的计时信息在 standalone 拓扑下也会自动到达 frontend。」

**答案**：错。这正是 [AGENTS.md:59-73](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/AGENTS.md#L59-L73) 明确警告的：`PreprocessedRequest.tracker` 是 `#[serde(skip)]` 的，`Arc<RequestTracker>` 无法跨 Rust→Python→Rust 或 RPC 边界自动携带；要传必须显式放进响应数据再由 frontend 合并（4.4.3 会看到 `inject_timing_from_tracker` 这个显式搬运点）。

### 4.2 独立路由进程的骨架：StandaloneRouterHandler 与三个端点

#### 4.2.1 概念说明

`StandaloneRouterHandler` 是路由进程的「业务类」。它做两件事：

1. **初始化**：把 `--endpoint` 字符串解析成三段路径，拿到 worker 端点的 client，并构造一个 `KvRouter`。
2. **对外提供三个能力**，各自注册成一个 Dynamo 运行时端点：

| 端点 | 行为 | 会不会真的转发请求 |
|---|---|---|
| `generate` | 选最佳 worker 并流式返回生成结果 | 会 |
| `best_worker_id` | 只回答「最佳 worker 是谁」，不动路由器状态 | 不会 |
| `get_overlap_scores` | 只回答「每个 worker 各层缓存命中多少块」 | 不会 |

三个端点对应三种使用姿态（[standalone-router.md:45-53](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L45-L53)）：让路由器代办（`generate`）；外部调度器想自己拍板（`best_worker_id`）；调度器只要原始的分重合度信号（`get_overlap_scores`）。

#### 4.2.2 核心流程

`generate` 的数据整形流程（**不透明信封**模式）：

```text
客户端送来的 dict
  │  取 routing（兼容旧的顶层 dp_rank）
  ├─ 组装 preprocessed_request（固定字段名的 dict）
  │    model / token_ids / stop_conditions / sampling_options /
  │    output_options / eos_token_ids / annotations / routing /
  │    router_config_override / prefill_result / bootstrap_info /
  │    extra_args / mm_processor_kwargs
  ├─ kv_router.generate_from_request(preprocessed_request)   # 进 Rust
  └─ 把 Rust 返回的每个 chunk 重新包成 llm_engine_output dict
       （token_ids / text / finish_reason / disaggregated_params /
         extra_args / engine_data / routing_data ...）
```

注意两个方向的 dict 字段名都是**跨语言硬契约**（呼应 u5-l3）：多写的字段会被静默丢弃，少写的字段在 Rust 反序列化时按缺省处理。

#### 4.2.3 源码精读

**初始化**。[__main__.py:54-81](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L54-L81)：先按 `.` 切分 endpoint 字符串并强制三段（[L57-L64](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L57-L64)），再取 client（[L67-L70](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L67-L70)），最后构造 `KvRouter`（[L72-L77](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L72-L77)）。注意 `worker_endpoint.client()` 会**等待** worker 实例出现——u3-l1 讲过，client 等不到实例会在 watch 循环里无限挂起，所以 standalone router 要在 worker 之后（或同时）启动。

**generate 的整形**。[__main__.py:98-101](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L98-L101) 处理路由提示：新格式是请求里的嵌套 `routing` 对象，旧格式是顶层 `dp_rank`，两者都没有就传 `None`（交给路由器自由选）。[__main__.py:103-117](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L103-L117) 逐字段搬进 `preprocessed_request`；[__main__.py:119-121](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L119-L121) 调 `generate_from_request` 拿到流。

**响应重包装**。[__main__.py:124-142](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L124-L142)：把 worker 输出包成 `LLMEngineOutput` 形状的 dict。特别注意 [L137-L140](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L137-L140) 的注释：`engine_data` 承载 routed_experts/prompt_logprobs，`routing_data` 承载 **worker_id**/token_ids/timing——两者都要转发，「这样它们才能在这个路由器之后存活」。这就是 4.1 提到的显式跨边界传输路径。

**两个只读端点**。[__main__.py:144-164](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L144-L164) 的 `best_worker_id` 调 `kv_router.best_worker(...)` 并 yield 出 `worker_id`（还拿到了 dp_rank 和 overlap_blocks 但丢弃）；[L166-L187](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L166-L187) 的 `get_overlap_scores` 透传 `token_ids`、可选的 `router_config_override`、`block_mm_infos`、`lora_name`、`include_shared`、`cache_namespace`，返回按 worker/dp_rank 分层（device/host/disk/shared）的命中块数。

**端点注册**。[__main__.py:241-267](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L241-L267)：三个端点分别注册在 `{namespace}.router.generate` / `.best_worker_id` / `.get_overlap_scores`（L241-L245），然后用 `asyncio.gather` 并发 `serve_endpoint`，都带 `graceful_shutdown=True` 和指标标签 `("service", "router")`（L251-L267）——这个标签会出现在 Prometheus 指标维度里（u12-l1 承接）。

#### 4.2.4 代码实践

**实践目标**：写一个最小客户端，同时使用三个端点中的两个只读端点，直观看到「路由器知道什么」。

**操作步骤**（依据官方示例 [standalone-router.md:72-100](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L72-L100) 改写，示例代码）：

```python
# probe_router.py —— 探测 standalone router 的两个只读端点
import asyncio
import uvloop

from dynamo.runtime import DistributedRuntime, dynamo_worker

PROMPT_TOKENS = [1, 2, 3, 4, 5, 6, 7, 8] * 8   # 64 个 token，凑出多个块

@dynamo_worker()
async def main(runtime: DistributedRuntime):
    ns = "dynamo"   # 与 router 的 --endpoint 第一段一致
    best = await runtime.endpoint(f"{ns}.router.best_worker_id").client()
    scores = await runtime.endpoint(f"{ns}.router.get_overlap_scores").client()
    await best.wait_for_instances()

    # 注意：best_worker_id 的处理函数签名是 (token_ids, ...)，
    # 所以请求体直接传 token 列表即可
    stream = await best.generate(PROMPT_TOKENS)
    async for resp in stream:
        print("best_worker_id =", resp.data())

    stream = await scores.generate({"token_ids": PROMPT_TOKENS})
    async for resp in stream:
        print("overlap_scores =", resp.data())

if __name__ == "__main__":
    uvloop.run(main())
```

**需要观察的现象**：`best_worker_id` 打印一个整数 worker id；`get_overlap_scores` 打印一个按 `(worker_id, dp_rank)` 组织、按缓存层分桶的命中块数结构。

**预期结果**：在还没有任何请求被路由过、worker 也没有 KV 事件进来时，overlap 全为 0，`best_worker_id` 返回的是「当前负载最轻」的 worker。跑几条相同前缀的 `generate` 之后再探测，overlap 应变为非零——具体字段形状以实际返回为准，待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `generate` 要把请求和响应各重新包一层 dict，而 `best_worker_id` 不用？

**答案**：`generate` 走的是完整的 `PreprocessedRequest` → 引擎 → `LLMEngineOutput` 管线，两端都是有固定字段名的结构化类型，Python 侧的 dict 必须逐字段对齐才能被 Rust 正确反序列化/再序列化；而 `best_worker_id` 只接收 token 列表、返回一个 worker id，负载极小，没有结构对齐的必要。

**练习 2**：如果客户端想把请求钉在某个特定 worker 上（不经路由器选点），它该往 `generate` 的请求 dict 里放什么？

**答案**：放 `routing` 提示。[__main__.py:98-101](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/__main__.py#L98-L101) 支持 `routing` 对象（或旧式顶层 `dp_rank`），它会进入 `preprocessed_request["routing"]`，最终对应 Rust `PreprocessedRequest` 的 `RoutingHints`（如 `backend_instance_id` 即 worker_id，见 4.4.3 的 `generate` 绑定）。

**练习 3**：`best_worker_id` 会不会因为被查询而改变路由器的内部状态（比如把这次查询记成一次负载）？

**答案**：不会。该端点调用 `kv_router.best_worker(token_ids, override, cache_namespace=...)` 时**没有传 `request_id`**，而在绑定层 [kv.rs:2250-2252](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2250-L2252) 里 `update_states = request_id.is_some()`，所以纯查询不记账。这正符合端点 docstring「does NOT actually route the request or update router states」。

### 4.3 参数体系：DynamoRouterConfig 与共享 ArgGroup（router_mode 与 ROUTER_MODE_MAP）

#### 4.3.1 概念说明

standalone router 的参数分三层：

1. **自有参数**（`args.py` 的 `DynamoRouterArgGroup`）：`--endpoint`（必填）、`--router-block-size`、`--serve-indexer`。
2. **共享 KV 调优参数**（`kv_router_args.py` 的 `KvRouterArgGroup`）：几十个 `--router-*` 项，frontend 与 router 复用同一份定义。
3. **不属于它的参数**（`router_args.py` 的 `RouterArgGroup`）：`--router-mode` 等。这一组归 frontend 与 worker 广告使用——但**映射逻辑 `ROUTER_MODE_MAP` 定义在这里**，是理解「Python 参数如何变成 Rust 枚举」的钥匙，所以本节一并精读。

`RouterArgGroup` 有个值得学习的设计细节：它的构造函数**强制**调用方显式给出 `default_router_mode`，不提供「安全的」默认值。原因写在类 docstring 里（见下面源码精读）：如果一个 worker 不小心继承了 frontend 形状的默认值 `round-robin`，它的模型卡片会**整体覆盖** frontend 的配置——一个忘了想清楚的操作，就会让 frontend 的任何模式被静默无视。把「忘了想」变成启动时的 `TypeError`，是很好的防御性设计。

#### 4.3.2 核心流程

参数从 CLI 到 Rust 的两条路径：

```text
路径 A（standalone router 自己）:
  CLI/环境变量 → DynamoRouterArgGroup + KvRouterArgGroup + AicPerfArgGroup
    → parse_args() → DynamoRouterConfig（多继承自 KvRouterConfigBase、AicPerfConfigBase）
    → validate()（格式/互斥/前置条件检查）
    → kv_router_kwargs() 只挑 _KV_ROUTER_FIELDS 里列出的字段
    → KvRouterConfig(**kwargs)          # PyO3 构造 Rust 配置对象

路径 B（frontend / worker 广告）:
  CLI/环境变量 → RouterArgGroup（--router-mode 必须显式默认值）
    → build_router_config(config)
        router_mode 字符串 --ROUTER_MODE_MAP--> Rust RouterMode 的属性名
        getattr(RouterMode, "KV") 等       → 枚举值
        仅当 mode == KV 才附带 KvRouterConfig
    → RouterConfig(mode, kv_config, **router_kwargs())
```

七种模式的语义（综合 [router_args.py:222-244](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L222-L244) 的 help 文本）：

| CLI 拼写 | Rust 枚举属性 | 行为 |
|---|---|---|
| `round-robin` | `RoundRobin` | 无状态轮询 |
| `random` | `Random` | 随机选 |
| `power-of-two` | `PowerOfTwoChoices` | 随机抽 2 个候选，选在途请求更少的那个 |
| `kv` | `KV` | KV 缓存重合度 + 负载感知（本单元主角） |
| `direct` | `Direct` | 不选点，按请求里的指定直发 |
| `least-loaded` | `LeastLoaded` | 选活跃请求最少的 worker |
| `device-aware-weighted` | `DeviceAwareWeighted` | 按 worker 设备类型（CPU/CUDA）加权 |

help 文本还注明：在分离式 prefill 场景下，`power-of-two` 与 `least-loaded` 会跳过 bootstrap 优化、退回同步 prefill 路径——这是 u7-l1 分离式服务的伏笔。

KV 模式的打分直觉（方向性模型，精确公式在 u6-l2 精读 Rust 打分代码）：路由器为每个候选 worker 估计一个「调整后 prefill 负载」，缓存命中等价于减负载：

\[ L'_i \;=\; s \cdot \max\!\Big(0,\; P_i - \big(\, c_{dev}\,\delta_i\,O^{dev}_i \;+\; w_{host}\,O^{host}_i \;+\; w_{disk}\,O^{disk}_i \,\big) \Big) \]

其中 \(P_i\) 是 worker \(i\) 的原始 prompt 侧负载（块数），\(O^{dev/host/disk}_i\) 是三个缓存层的重合块数，\(c_{dev}\) 即 `--router-kv-overlap-score-credit`（默认 1.0），\(w_{host}\)、\(w_{disk}\) 分别默认 0.75 与 0.25（「CPU 层命中值 75% 个设备块、磁盘层值 25%」），\(s\) 是 `--router-prefill-load-scale`。衰减因子 \(\delta_i\) 由 `--router-kv-overlap-score-credit-decay` 控制：当 worker 的活跃 prefill 负载超过「最轻的可选 worker」时，超出量按该速率折减设备层信用——按 help 文本「decay=1 时，每多一份请求当量的过量负载，信用减半」，即形如

\[ \delta_i = 2^{-\lambda \cdot \max(0,\; A_i - A_{\min})}, \qquad \lambda = \texttt{credit\_decay} \]

路由器选 \(L'\) 最小的 worker；`--router-temperature` 大于 0 时改为按 softmax 归一化后带温度采样（0 回退确定性）。

#### 4.3.3 源码精读

**自有参数注册**。[args.py:93-127](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L93-L127)：`--endpoint`（L97-L104，env `DYN_ROUTER_ENDPOINT`，help 里给了 `dynamo.prefill.generate` 的例子）、`--router-block-size`（L106-L114，默认 128，废弃别名 `--block-size`）、`--serve-indexer`（L116-L123，可否定布尔），然后在 L125-L127 把 `KvRouterArgGroup` 与 `AicPerfArgGroup` 挂进来。**没有 `RouterArgGroup`**——这就是 4.1.4 观察到的参数面形状的根源。

**配置类与校验**。[args.py:24-31](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L24-L31) `DynamoRouterConfig` 多继承两个共享基类，只新增 4 个自有字段。`validate()`（[L32-L85](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L32-L85)）做了一系列检查，其中三处最值得读：

- [L41-L52](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L41-L52)：endpoint 必须三段；namespace 优先取环境变量 `DYN_NAMESPACE`，否则用 endpoint 第一段；若 worker namespace（可能带 `-suffix`，见 [namespace.py:8-22](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/utils/namespace.py#L8-L22) 的 `get_worker_namespace`）与 endpoint 第一段不一致，**把 endpoint 改写为 worker namespace 开头**。这段是「DYN_NAMESPACE_WORKER_SUFFIX 支持多组 worker」的关键。
- [L77-L85](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L77-L85)：standalone router **拒绝** `--router-conditional-disagg`（条件分离只在 frontend 的分离式服务里运行）——又一个「这个参数不属于这个进程」的显式证据。
- [L58-L76](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L58-L76)：`--router-prefill-load-model=aic` 需要补齐 `--aic-backend/--aic-system/--aic-model-path` 且必须 `--router-track-prefill-tokens`。

**字段白名单跨边界**。[kv_router_args.py:31-69](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py#L31-L69) 用元组 `_KV_ROUTER_FIELDS` 列出全部要转发的字段，[L276-L280](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/kv_router_args.py#L276-L280) 的 `kv_router_kwargs()` 只挑这些字段拼 dict，供 `KvRouterConfig(**kwargs)` 直接解包——字段名与 Rust 构造参数 1:1。这与 u5-l1 讲过的 `EntrypointArgs` 白名单跨 PyO3 是同一手法：**跨语言边界只送显式列名的字段**。

**router_mode 的注册与「必须显式默认值」**。[router_args.py:141-169](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L141-L169) 的 `RouterArgGroup.__init__` 要求 `default_router_mode` 与 `include_frontend_only` 两个关键字参数；docstring（L143-L159）解释了原因：worker 的卡片会**整体替换** frontend 的路由配置，若 worker 组拿到 frontend 形状的默认值 `round-robin`，就会静默覆盖 frontend 的任何模式——所以「frontend 传历史默认 `round-robin`，worker 传 `None`（什么都不广告、继承 frontend）」，把疏忽变成启动错误。`--router-mode` 本体在 [L222-L244](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L222-L244) 注册，7 个 choice 与上表一一对应。

**ROUTER_MODE_MAP 与 build_router_config**。[router_args.py:300-309](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L300-L309) 是映射表本体——注意它映射到的是**属性名字符串**（`"kv" → "KV"`），不是枚举值本身，因为 `dynamo.llm` 是编译扩展，import 被刻意延迟。真正取枚举的动作在 [build_router_config:383-418](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L383-L418)：

- L394-L399：`config` 为 `None` 或 `router_mode` 为 `None` 时返回 `None`——语义是「卡片上不写 `router_config`，该组 worker 继承 frontend 的配置」。
- L401-L412：延迟 `from dynamo.llm import ...`（注释说明这是为了「导入后端参数定义时不拉起编译扩展」），查表拿属性名，`getattr(RouterMode, mode_attr)` 取枚举值；查不到就抛出带全部合法值的 `ValueError`。
- L413-L417：**只有 KV 模式才附带 `KvRouterConfig`**——注释写得很直白：给其他模式传 KV 调优参数意味着一堆永远没人读的配置。

worker 侧的入口是 [add_worker_router_arguments:325-334](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L325-L334) 与 [parse_worker_router_config:337-L348](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L337-L348)：后端引擎用 `parse_known_args` 把路由标志从自己的 argv 里摘出来、剩余参数原样传给引擎解析器——u8 各后端接入时会再遇到它。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `validate()` 的三个报错分支，把「参数校验」从抽象概念变成可复现实验。

**操作步骤**（示例代码中的命令都只做参数解析，不会真正连集群；`python -m dynamo.router` 会在校验失败时直接退出）：

```bash
# ① endpoint 缺失
python -m dynamo.router
# ② endpoint 格式不对（两段）
python -m dynamo.router --endpoint dynamo.generate
# ③ 条件分离不属于 standalone router
python -m dynamo.router --endpoint dynamo.backend.generate --router-conditional-disagg
# ④ aic 模式缺前置参数
python -m dynamo.router --endpoint dynamo.backend.generate --router-prefill-load-model aic
```

若想在不启动服务的情况下只测解析逻辑，也可以在 Python 里直接调（示例代码）：

```python
from dynamo.router.args import parse_args

for argv in ([], ["--endpoint", "dynamo.generate"],
             ["--endpoint", "dynamo.backend.generate", "--router-conditional-disagg"]):
    try:
        parse_args(argv)
        print(argv, "-> OK")
    except (ValueError, SystemExit) as e:
        print(argv, "-> 拒绝:", e)
```

**需要观察的现象**：四条命令分别报「endpoint is required」「Invalid endpoint format」「only supported by dynamo.frontend」「requires --aic-backend, ...」四类错误。

**预期结果**：错误信息与 [args.py:36-85](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L36-L85) 的四个分支一一对应；`parse_args` 的 Python 直调版本能在不启动运行时的情况下复现同样报错（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：`DYN_NAMESPACE_WORKER_SUFFIX=blue python -m dynamo.router --endpoint dynamo.backend.generate` 最终路由器盯的是哪个 endpoint？

**答案**：`dynamo-blue.backend.generate`。`get_worker_namespace("dynamo")` 会把后缀拼成 `dynamo-blue`（[namespace.py:19-L21](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/utils/namespace.py#L19-L21)），而 `validate()` 发现 worker namespace 与 endpoint 第一段不一致后会把 endpoint 改写为 worker namespace 开头（[args.py:50-L52](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/router/args.py#L50-L52)）。若不设 `DYN_NAMESPACE`，router 自己的三个服务端点也会注册在 `dynamo-blue.router.*` 下。

**练习 2**：为什么 `ROUTER_MODE_MAP` 存「属性名字符串」而不是直接存枚举对象？

**答案**：`RouterMode` 定义在编译扩展 `dynamo.llm`（PyO3）里。`router_args.py` 会被各后端在**定义参数阶段**导入，如果在模块顶层 `from dynamo.llm import RouterMode`，等于「只是想声明一下 CLI 参数也必须先加载 .so」。[build_router_config:401-L402](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L401-L402) 的注释明说了这一点，所以把 import 推迟到真正要构造 `RouterConfig` 的那一刻。

**练习 3**：一个 worker 用 `--router-mode random` 启动，frontend 用 `--router-mode kv` 启动。最终这组 worker 的请求按什么模式路由？如果把 worker 的 `--router-mode` 去掉呢？

**答案**：带 `random`：worker 卡片上的 `router_config` 整体覆盖 frontend 的全局配置，这一组按 `random` 路由（其他组仍按 frontend 的 `kv`）。去掉后：`build_router_config` 返回 `None`，卡片不带 `router_config`，该组继承 frontend 的 `kv`——这正是 `default_router_mode=None` 设计想要的行为（[router_args.py:151-L159](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L151-L159) 与 [L394-L399](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/common/configuration/groups/router_args.py#L394-L399)）。

### 4.4 KvRouter：从 Python 对象到 Rust RoutingHost

#### 4.4.1 概念说明

`__main__.py` 里 `from dynamo.llm import KvRouter, KvRouterConfig`——Python 侧的 `KvRouter` 不是纯 Python 实现，而是 PyO3 暴露的 Rust 类（u2-l2 讲过 wrapper+inner 模式）。它的 `inner` 是一个 `Arc<RoutingHost>`，也就是说：**Python 的 KvRouter 就是 Rust RoutingHost 的进程内句柄**。

`KvRouter.__init__` 做的四件事：

1. 从 endpoint 拿 client，建一个 **`RouterMode::KV` 的 PushRouter**（请求面推送路由器，u3-l4 讲过它的占用记账与故障检测）。
2. 通过 `create_kv_router_from_endpoint` 建**选点器**（真正的 KV 打分器），并用 `ModelManager` 保证 etcd/发现面注册。
3. 把两者组装成 `RoutingHost`（u6-l2 的主角）。
4. 可选挂上会话亲和（session affinity）TTL。

一个容易忽略的细节：`infer_metric_worker_type` 会**根据 endpoint 路径里是否含 "prefill" 字样**来推断这个路由器面向的 worker 角色（用于指标标签与角色语义），这是「命名约定影响行为」的一个真实例子。

#### 4.4.2 核心流程

```text
KvRouter(endpoint, block_size, kv_router_config, aic_perf_config?)
  │  (在 pyo3 的 Tokio 运行时上 block_on)
  ├─ endpoint.client()                        # 等待 worker 实例
  ├─ PushRouter::from_client(client, RouterMode::KV)
  ├─ create_kv_router_from_endpoint(...)
  │     ├─ ModelManager::new()
  │     ├─ infer_metric_worker_type(...)      # 按 endpoint 命名推断角色
  │     ├─ （仅当需要模型名/自定义策略时）阻塞等待 ModelDeploymentCard
  │     └─ model_manager.kv_chooser_for_with_worker_role(...)   # 建选点器
  └─ RsRoutingHost::new(push_router, kv_router, session_affinity_ttl)
        → Python 对象 KvRouter { inner: Arc<RoutingHost> }
```

之后 `generate_from_request` 的每次调用：Python dict → `depythonize` 成 Rust `PreprocessedRequest` → `RoutingHost::generate` → 流式 `LLMEngineOutput` → `pythonize` 回 Python dict。tracker 的 worker_id 与计时在**首帧/末帧**被显式注入 `routing_data`（呼应 4.1 的边界纪律）。

#### 4.4.3 源码精读

**类本体**。[kv.rs:1848-L1851](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1848-L1851)：`pub(crate) struct KvRouter { inner: Arc<RsRoutingHost> }`——`RsRoutingHost` 默认是 `llm_rs::kv_router::RoutingHost`（开启 `custom-policy` feature 时泛型换成自定义策略类型，L50-L57，u6-l5 承接）。

**构造函数**。[kv.rs:1984-L2070](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1984-L2070)：

- L1994-L1998 校验会话亲和 TTL（1 秒 ~ 31536000 秒，即一年）。
- L2030-L2046 释放 GIL 后 `block_on`：先 `endpoint.client()`，再 `PushRouter::from_client(client, RouterMode::KV)`——**这里硬编码了 KV 模式**，这就是「standalone router 永远是 KV 感知」在 Rust 侧的落点。注释也提醒：初始等待可以无限长，放 GIL 只让它可被监督、不可被取消。
- L2049-L2056 调 `create_kv_router_from_endpoint` 建选点器；L2058-L2063 组装 `RsRoutingHost`。

**选点器的诞生**。[create_kv_router_from_endpoint:1690-L1846](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1690-L1846) 信息量很大，抓三个点：

- [infer_metric_worker_type:1627-L1641](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1627-L1641)：namespace/component/endpoint 任一含 `prefill`（不区分大小写）或关闭了活跃块跟踪 → 判为 prefill 角色，否则 decode。这保持了 standalone 路由器历史上的指标分类习惯。
- [L1711-L1757](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1711-L1757)：**只有**需要模型名（远程/对外服务索引器）或需要自定义策略时才阻塞等 ModelDeploymentCard（上限 `DYN_ROUTER_MODEL_CARD_WAIT_SECS`，默认 600 秒）；普通路由器只做一次非阻塞的发现快照（用于 Eagle 语义）。
- [L1796-L1808](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1796-L1808)：最终 `model_manager.kv_chooser_for_with_worker_role(...)` 生成选点器并完成注册。

**三个入口方法**（与 4.2 的三个端点一一对应）：

- `generate_from_request`：[kv.rs:2190-L2220](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2190-L2220)。L2199-L2200 把 Python dict `depythonize` 成 `PreprocessedRequest`；L2203-L2210 若请求没带 tracker 就补一个（用于回收 worker 信息）；然后进入 `dispatch_request_to_stream`。
- 流转发与 tracker 注入：[kv.rs:1882-L1954](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1882-L1954) 的 `process_request_to_stream` 在后台任务里逐帧搬运，**首帧**注入 worker_id（L1903-L1905 调 [inject_worker_id_from_tracker:1855-L1866](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1855-L1866)），**终帧**（`finish_reason` 出现）记录计时并注入（L1923-L1928 调 [inject_timing_from_tracker:1868-L1877](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1868-L1877)）。两个函数的 docstring 都点明动机：「让数据在 Rust→Python→Rust 的路由路径上存活（数据能活，注解不能）」。
- `best_worker`：[kv.rs:2222-L2311](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2222-L2311)。核心调用是 `find_best_match_details_with_policy_class`（L2253-L2274），返回 `FindBestMatchOutcome::Routed { worker, overlap_blocks }` 或队列拒绝；L2286-L2307 是可选的 `update_indexer` 分支——当 KV 事件关闭或开启了 predict-on-route 时，把这次决策写进本地索引器。
- `get_overlap_scores`：[kv.rs:2376-L2419](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2376-L2419)，透传给选点器并 `pythonize` 返回分层命中结构。

**对照组：Client 的默认路由模式**。[lib.rs:1622-L1638](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/lib.rs#L1622-L1638)：`Endpoint.client()` 在未指定模式时 `router_mode.unwrap_or(RouterMode::RoundRobin)`（L1627）——综合实践中 round-robin 对照组就靠它。

**mocker 为什么适合做路由实验**：[lib/llm/src/mocker.rs:630-L709](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/llm/src/mocker.rs#L630-L709) 显示 Live Mocker 的每个 dp_rank 默认都会建 `KvEventPublisher` 并把 `KvCacheEventSink` 适配器接进引擎（L683-L707），KV 事件经 [engine_observations.rs:14-L58](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/mocker/src/engine_observations.rs#L14-L58) 从模拟引擎观测转成 Dynamo 协议（带 block_hash 与 tokens_hash）。也就是说，**mocker 会发布真实的 KV 事件**——这一点与 u1-l2 的 sample 后端（每条请求全新块哈希、永远不命中）截然不同，正是本讲综合实践能观察到「缓存亲和」的前提。

#### 4.4.4 代码实践

**实践目标**：不启动 standalone 进程，直接在自己的 Python 程序里持有 `KvRouter`（这是官方 router-examples 演示的用法，也是自定义路由服务的构建块）。

**操作步骤**（示例代码，改编自 [router-examples.md:66-80](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/router-examples.md#L66-L80)）：

```python
# embedded_kvrouter.py —— 在自己的进程里内嵌一个 KV 路由器
import asyncio
from dynamo.runtime import DistributedRuntime
from dynamo.llm import KvRouter, KvRouterConfig

async def main():
    loop = asyncio.get_running_loop()
    runtime = DistributedRuntime(loop, "file", "tcp")      # 本地零依赖：file 发现
    endpoint = runtime.endpoint("dynamo.backend.generate")

    router = KvRouter(
        endpoint=endpoint,
        block_size=64,                                     # 必须与 worker 的 --block-size 一致
        kv_router_config=KvRouterConfig(),
    )

    stream = await router.generate(
        token_ids=[1, 2, 3, 4] * 16,
        model="Qwen/Qwen3-0.6B",
    )
    async for chunk in stream:
        print(chunk)

asyncio.run(main())
```

**需要观察的现象**：构造 `KvRouter` 时若两个 mocker worker 尚未注册，调用会阻塞等待（对照 4.2.3 的 `client()` 挂起语义）；worker 就绪后流式打印 chunk。

**预期结果**：能看到与 standalone `generate` 端点等价的输出——因为 standalone router 的 `generate` 内部调的就是这个 `generate_from_request`。具体运行依赖本地环境（需安装 `ai-dynamo[mocker]` 并先起 worker），待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：standalone router 的请求面 PushRouter 用的是哪种 `RouterMode`？这个选择由谁决定、在哪一行？

**答案**：`RouterMode::KV`，硬编码在绑定层构造函数里，[kv.rs:2036-L2044](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2036-L2044)。它不由任何 CLI 参数决定——这就是「standalone router 没有 `--router-mode`」在 Rust 侧的对应物。

**练习 2**：为什么 `process_request_to_stream` 要在首帧注入 worker_id、在终帧注入 timing，而不是每帧都注？

**答案**：worker 归属在一次路由中不会变，首帧注一次即可；timing 只有在请求结束时才完整（终帧判定条件是 `finish_reason.is_some()`），所以放在终帧。每帧重复注入既浪费又会互相覆盖（[kv.rs:1900-L1928](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L1900-L1928)）。

**练习 3**：`KvRouter::new` 里为什么用 `py.allow_threads(|| runtime.block_on(...))` 而不是直接 `block_on`？

**答案**：构造过程可能长时间阻塞（等 worker、等模型卡，最长 600 秒）。持着 GIL 阻塞会让整个 Python 进程卡死；释放 GIL 后其他 Python 线程仍可运行（比如用来监督、关停），但注释明确说这只是「可监督」而非「可取消」（[kv.rs:2028-L2032](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/lib/bindings/python/rust/llm/kv.rs#L2028-L2032)）。

## 5. 综合实践

**任务**：起 2 个 mocker worker + 1 个 standalone KV 路由器，对比「round-robin 直连」与「KV 感知路由」在相同前缀请求下的 worker 命中分布。这是本讲规格里指定的实践，把 4.1~4.4 全部串起来。

### 步骤 1：准备（安装与环境）

```bash
pip install 'ai-dynamo[mocker]'          # mocker extra
export DYN_DISCOVERY_BACKEND=file        # 本地零依赖：文件发现（u1-l2）
export DYN_EVENT_PLANE=zmq               # file 发现默认配 ZMQ 事件面（u3-l5）
```

### 步骤 2：起两个 mocker worker（同一进程，两个实例）

mocker 的默认 endpoint 是 `dyn://dynamo.backend.generate`（[mocker/args.py:18-L20](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/args.py#L18-L20)），`--num-workers 2` 会在同一进程里建两个隔离 runtime 的 worker（[mocker/main.py:171-L231](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/main.py#L171-L231)），它们注册在**同一路径**下——按 u3-l2 的模型，同路径多实例就是负载均衡对象。

```bash
python -m dynamo.mocker \
  --model-path Qwen/Qwen3-0.6B \
  --block-size 64 \
  --num-workers 2
```

`--block-size 64`（[mocker/args.py:202-L208](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/components/src/dynamo/mocker/args.py#L202-L208)）必须在下一步原样传给路由器——官方文档特别提醒：standalone 路由器**不会**从 ModelDeploymentCard 推断块大小（[standalone-router.md:117-L120](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L117-L120)）。

### 步骤 3：起 standalone 路由器

```bash
python -m dynamo.router \
  --endpoint dynamo.backend.generate \
  --router-block-size 64
```

（这就是 [standalone-router.md:61-L64](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/cli/kv-aware-routing/standalone-router.md#L61-L64) 的示例命令。）

### 步骤 4：写对比客户端

示例代码（round-robin 对照组利用 `endpoint.client()` 的默认 RoundRobin 模式；KV 组走路由器的 `generate`，并从响应的 `routing_data` 读 worker_id）：

```python
# compare_routing.py
import asyncio
from collections import Counter

import uvloop
from dynamo.runtime import DistributedRuntime, dynamo_worker

PREFIX = [11, 22, 33, 44] * 16          # 64 token 相同前缀 = 64/64 块可复用（block_size=64）
TAILS = [[1], [2], [3], [4], [5], [6], [7], [8]]   # 每条请求换一个尾巴

REQUEST = {
    "model": "Qwen/Qwen3-0.6B",
    "stop_conditions": {"max_tokens": 4},
    "sampling_options": {},
    "output_options": {},
}

async def run_round_robin(runtime):
    client = await runtime.endpoint("dynamo.backend.generate").client()
    await client.wait_for_instances()
    hits = Counter()
    for tail in TAILS:
        req = {**REQUEST, "token_ids": PREFIX + tail}
        stream = await client.generate(req)
        async for resp in stream:
            if resp.is_error():
                raise RuntimeError(resp.comments())
            _ = resp.data()           # 消费流；round-robin 模式下不返回 worker_id
        # 直连路径拿不到 worker_id，改为计数「已发请求数」，分布靠第 5 步的
        # get_overlap_scores / best_worker_id 侧证
        hits["sent"] += 1
    return hits

async def run_kv_router(runtime):
    client = await runtime.endpoint("dynamo.router.generate").client()
    await client.wait_for_instances()
    hits = Counter()
    for tail in TAILS:
        req = {**REQUEST, "token_ids": PREFIX + tail}
        stream = await client.generate(req)
        async for resp in stream:
            if resp.is_error():
                raise RuntimeError(resp.comments())
            data = resp.data() or {}
            wid = (data.get("routing_data") or {}).get("worker_id")
            if wid is not None:
                hits[wid] += 1
    return hits

@dynamo_worker()
async def main(runtime: DistributedRuntime):
    print("round-robin 直连:", await run_round_robin(runtime))
    print("KV 路由:", await run_kv_router(runtime))
    best = await runtime.endpoint("dynamo.router.best_worker_id").client()
    stream = await best.generate(PREFIX)
    async for resp in stream:
        print("再次探测 best_worker_id =", resp.data())

if __name__ == "__main__":
    uvloop.run(main())
```

### 步骤 5：观察与记录

1. **KV 组的命中分布**：8 条同前缀请求，预期第一、二条落在（可能不同的）worker 上，此后绝大多数请求**集中到同一个 worker**——因为该 worker 的 KV 缓存里已经存了这段前缀的块，重合度最高、调整后负载最低。
2. **`best_worker_id` 探测**：跑完后再查，应稳定返回缓存了该前缀的那个 worker。
3. **round-robin 组的侧证**：直连路径的响应不带 worker_id（u3-l4 讲过：非 KV 模式不做这种注入），所以用 `get_overlap_scores` 侧证——round-robin 把前缀散到两个 worker 后，**两个**worker 都应出现非零重合；而 KV 组只有**一个** worker 非零。这个对比本身就是结论。

### 预期结论（待本地验证）

| 观察项 | round-robin 直连 | KV 感知路由 |
|---|---|---|
| 前 8 条同前缀请求的 worker 分布 | 约 4/4 均分 | 集中于 1 个 worker |
| `get_overlap_scores` 非零的 worker 数 | 2 | 1 |
| `best_worker_id`（跑完后） | 不稳定 | 稳定为缓存持有者 |

若 KV 组没有出现亲和，先检查三件事：块大小是否两边一致（64）、mocker 是否真的在发 KV 事件（看 worker 日志；4.4.3 的 `KvEventPublisher` 装配是否成功）、`--router-kv-events` 是否被关掉（默认开）。

## 6. 本讲小结

- Dynamo 有三种路由部署形态：frontend 内嵌 Rust 路由（`--router-mode kv`，无路由 RPC）、frontend 内 Python processor + 内嵌路由（`--dyn-chat-processor`）、独立路由进程（`python -m dynamo.router`，多一跳 RPC）。三者共享 Rust `KvRouter` 逻辑，但**不共享序列化与进程边界**——元数据跨界必须显式搬运。
- `StandaloneRouterHandler` 对外暴露三个端点：`generate`（真路由并流式转发）、`best_worker_id`（纯查询不记账）、`get_overlap_scores`（分层重合度原始信号）；请求与响应用**固定字段名的 dict 信封**跨 Python/Rust 边界。
- standalone router 的参数 = 3 个自有参数（`--endpoint` 必填三段路径、`--router-block-size` 默认 128、`--serve-indexer`）+ 共享的 `KvRouterArgGroup`/`AicPerfArgGroup`；`DynamoRouterConfig.validate()` 负责 endpoint 格式、namespace 规整（含 worker 后缀）与多项前置条件，并显式拒绝条件分离参数。
- `--router-mode` **不属于** standalone router：它由 `RouterArgGroup` 注册（默认值必须显式给出，防 worker 卡片静默覆盖 frontend），七种模式经 `ROUTER_MODE_MAP`（CLI 拼写 → 枚举属性名）在 `build_router_config` 里延迟 import 并 `getattr(RouterMode, ...)` 取枚举，且只有 KV 模式才附带 `KvRouterConfig`。
- Python 的 `KvRouter` 是 PyO3 薄壳，`inner` 即 `Arc<RoutingHost>`；构造时硬编码 `RouterMode::KV` 的 PushRouter + `ModelManager` 注册的选点器，tracker 的 worker_id/timing 在首帧/终帧显式注入 `routing_data` 以跨越 Rust→Python→Rust 边界。
- mocker 与 sample 的关键差别：mocker 默认为每个 dp_rank 接上 `KvEventPublisher` 发布真实 KV 事件，相同前缀会产生稳定命中——因此 mocker 是做 KV 路由实验的正确工具。

## 7. 下一步学习建议

本讲只回答了「路由进程长什么样、参数从哪来、Python 怎么摸到 Rust 路由器」。接下来：

1. **u6-l2（Rust 路由核心：routing_host 与调度）**：本讲反复出现的 `RoutingHost`、选点器、`find_best_match_details_with_policy_class`、KV 打分公式 \(L'_i\) 的精确形式，全部在 `lib/llm/src/kv_router/routing_host.rs` 与 `routing_host/builtin.rs`、`scheduler.rs` 里展开——这是下一讲的全部内容。
2. **u6-l3（KV 事件流）**：本讲把「mocker 会发 KV 事件」当成前提；事件如何从引擎经 publisher/batching/ZMQ 到达路由器，是下一下一讲的内容。
3. **先行阅读建议**：在进入 u6-l2 前，重读 u3-l4 的 `RouterMode` 分组（无状态轮询 / 负载感知 / 外部决策三组）与本讲的七模式表对照，会发现两处视角完全吻合；再浏览 [docs/fern/pages/developer-guide/knowledge-base/modular-components/router/router-guide.md](https://github.com/ai-dynamo/dynamo/blob/3d3cf16123241ce36adb71a83053097a3d8457b5/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/router-guide.md) 的部署形态总表，巩固三种拓扑的选型直觉。
