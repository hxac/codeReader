# 配置类型与校验

## 1. 本讲目标

上一讲（u2-l1）我们跟着 `CliArgs::to_router_config` 走完了「命令行字符串 → `RouterConfig` 对象」的翻译链，但当时刻意**没有**展开两件事：这个 `RouterConfig` 到底由哪些字段组成、它们各自的含义和默认值是什么；以及 `.build()` 末尾那句 `validate()` 到底在校验什么。

本讲就把这两块补齐。学完本讲你应该能够：

- 看懂 `RouterConfig` 这个「中心配置结构」的全部字段，并能说出关键字段（端口、超时、限流、连接池、TLS、tokenizer 缓存等）的**默认值**来自哪里。
- 区分两个正交维度：`RoutingMode`（Regular / PrefillDecode / OpenAI，描述**部署形态**）与 `ConnectionMode`（Http / Grpc，描述**和 worker 用什么协议通信**）。
- 说出可靠性三件套 `RetryConfig`、`CircuitBreakerConfig`、`HealthCheckConfig` 各自的字段语义，以及 `disable_retries` / `disable_circuit_breaker` 这两个「软开关」是如何通过 `effective_*_config()` 生效的。
- 理解 `HistoryBackend` 这个枚举为什么把 `oracle` / `postgres` / `redis` 三种外部后端设计成「**选了就必须附带凭据**」的可选字段。
- 读懂 `ConfigError` 的四个变体，并能沿着 `ConfigValidator::validate` 的调用链判断「一个非法配置会在哪一步、以哪种错误类型被拦下」。

本讲聚焦 `src/config/` 这三个文件的**类型与校验**，不涉及这些配置之后怎么被 `server::startup` 真正使用（那是 u2-l3）。

## 2. 前置知识

### 2.1 配置对象为什么重要

回顾 u1-l4 的分层：`config` 是金字塔的最底层，几乎没有内部依赖；而 `RouterConfig` 是这一层的「中心数据结构」。控制面、数据面、可靠性层后面几乎都要读它的字段。可以说：

> **`RouterConfig` 是整个网关运行的「参数总表」。** 理解了它的字段，就理解了网关「能调什么、默认怎么调」。

这也是为什么字段要设计得足够细——从端口、超时，到熔断阈值、历史存储后端，再到 TLS 证书字节、tokenizer 缓存开关——但绝大多数都有合理的默认值，让你「不配也能跑」。

### 2.2「强类型 + 显式校验」是 Rust 配置代码的两个习惯

很多语言里，配置校验散落在各处「用到的时候才发现错了」。这个项目用了两个 Rust 习惯来对抗这种混乱：

1. **强类型枚举**：像 `RoutingMode`、`PolicyConfig`、`HistoryBackend` 都是用 `enum` 表达的。非法字符串（比如 `history_backend = "mongo"`）在解析阶段就会被拒掉，根本进不了 `RouterConfig`。
2. **集中式 `validate()`**：所有「跨字段的、值域的、兼容性的」校验集中在 `ConfigValidator::validate` 一个函数里，并在 `builder.build()` 封口时统一执行。这意味着**一个校验没过，程序根本起不来**，而不是跑到一半才崩。

### 2.3 thiserror 与错误分类

项目用 [`thiserror`](https://docs.rs/thiserror) 这个库为配置错误定义了一个 `ConfigError` 枚举，每个变体带一段 `#[error("...")]` 模板。这样做的好处是：**调用方既能 `match` 到精确的变体做分支处理，又能直接 `.to_string()` 拿到给人看的错误信息。** 本讲我们会反复看到校验代码根据「错在哪、为什么错」选择不同的变体返回，这正是「错误分类」的意义。

## 3. 本讲源码地图

本讲涉及三个文件，外加一个跨模块的类型引用：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/config/types.rs` | 所有配置**类型**的定义与默认值 | `RouterConfig`、`RoutingMode`、`PolicyConfig`、`RetryConfig`、`CircuitBreakerConfig`、`HealthCheckConfig`、`TokenizerCacheConfig` |
| `src/config/validation.rs` | 配置**校验器** `ConfigValidator` | `validate()` 总入口及各 `validate_*` 子校验 |
| `src/config/mod.rs` | config 模块根，定义错误类型 | `ConfigError`、`ConfigResult` |
| `src/config/builder.rs`（引用） | builder 的 `build` 封口 | `build_with_validation` 在哪一步调用 `validate()` |
| `src/core/worker.rs`（引用） | `ConnectionMode` 的定义 | 理解 `connection_mode` 字段的取值 |

记忆口诀：**「types.rs 管『有什么』（字段），validation.rs 管『对不对』（规则），mod.rs 管『错成什么样』（错误类型）。」**

## 4. 核心概念与源码讲解

### 4.1 RouterConfig：中心配置结构、字段语义与默认值

#### 4.1.1 概念说明

`RouterConfig` 是一个有四十多个字段的普通结构体，派生了 `Debug / Clone / Serialize / Deserialize`。后两个派生让它既能被 `serde_json` 序列化（用于 Python 绑定、配置文件往返），又能用 `..Default::default()` 做部分构造。它的字段大致可以分成六组：

| 字段组 | 代表字段 | 作用 |
| --- | --- | --- |
| 路由形态 | `mode`、`connection_mode`、`policy`、`enable_igw`、`dp_aware` | 决定「怎么部署、用什么协议、用什么策略选 worker」 |
| 服务能力 | `host`、`port`、`max_payload_size`、`request_timeout_secs` | 网关自身监听与请求约束 |
| 连接池 / 网络 | `pool_idle_timeout_secs`、`connect_timeout_secs`、`pool_max_idle_per_host`、`tcp_keepalive_secs` | 与 worker 的 HTTP 连接复用与保活 |
| 限流 / 排队 | `max_concurrent_requests`、`queue_size`、`queue_timeout_secs`、`rate_limit_tokens_per_second` | 并发控制与令牌桶（u6-l3 详述） |
| 可靠性 | `retry`、`circuit_breaker`、`health_check`、`disable_retries`、`disable_circuit_breaker` | 重试 / 熔断 / 健康检查（本讲 4.3） |
| 存储 / 安全 / 模型 | `history_backend`、`oracle/postgres/redis`、`server_cert/key`、`client_identity`、`ca_certificates`、`model_path`、`tokenizer_cache` 等 | 历史持久化、TLS、tokenizer |

注意几个字段的「特殊记号」是理解默认行为的关键：

- `#[serde(default = "函数名")]`：反序列化时如果 JSON 里没这个字段，就调用这个函数兜底。`types.rs` 顶部那几个 `DEFAULT_*` 常量与 `default_*` 函数就是为此存在的。
- `#[serde(skip)]`：序列化时**跳过**该字段。`server_cert`、`client_identity`、`ca_certificates`、`mcp_config` 这些「字节内容」字段都标了 `skip`，因为它们不应该被打印或回写进 JSON（既庞大又可能含密钥）。
- `max_concurrent_requests: i32`（带符号！）：默认 `-1` 表示「不限并发」。这是个很关键的取值约定，负数即关闭限流。

#### 4.1.2 核心流程

`RouterConfig` 的「生产路径」有三条，都汇到同一份默认值：

1. `RouterConfig::default()`：最权威的默认值来源，所有字段都在这里显式列出。
2. `RouterConfig::new(mode, policy)`：只指定 `mode` 和 `policy`，其余字段 `..Default::default()` 兜底——这是测试里最常用的构造方式。
3. `RouterConfig::builder()....build()`：u2-l1 讲的链式构建，封口时 `validate()`。

不管走哪条路，**没被显式赋值的字段一律来自 `Default` 实现**，所以读 `impl Default for RouterConfig` 这一坨，就能知道「不配置时网关的实际行为」。

字段读写还有一个贯穿全篇的小约定：`effective_retry_config()` / `effective_circuit_breaker_config()` 这两个方法负责把「软开关」翻译成「有效值」——

- `disable_retries = true` ⇒ 有效 `max_retries = 1`（重试一次 = 不重试）。
- `disable_circuit_breaker = true` ⇒ 有效 `failure_threshold = u32::MAX`（阈值无穷大 = 永不熔断）。

校验器校验的是**有效值**而不是原始值，这样 `disable_*` 开关就不会和「阈值必须 ≥ 1」之类的规则冲突。

#### 4.1.3 源码精读

`RouterConfig` 结构体定义，字段分块注释清楚，是本讲的「主表」：

[RouterConfig 结构体定义 — src/config/types.rs:L15-L102](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L15-L102)

> 这段定义了全部字段。重点看 `max_concurrent_requests: i32`（默认 `-1` 即不限）、带 `#[serde(skip)]` 的证书/密钥字段（不参与序列化）、以及 `disable_retries` / `disable_circuit_breaker` 两个软开关的注释。

「不配置时的实际行为」全在这份默认实现里：

[impl Default for RouterConfig — src/config/types.rs:L503-L558](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L503-L558)

> 默认 `port = 3001`、`max_payload_size = 512MB`、`request_timeout_secs = 1800`（30 分钟，给大模型加载留余量）、`policy = Random`、`max_concurrent_requests = -1`（不限）、`connection_mode = Http`。这些就是「开箱即用」的真实参数。

`new()` 与 `validate()` 是两个最常用的入口：

[RouterConfig::new 与 validate — src/config/types.rs:L560-L573](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L560-L573)

> `validate()` 只有一行：把活儿全转交给 `ConfigValidator::validate(self)`。这种「类型只负责持有数据，校验逻辑独立成 `ConfigValidator`」的写法，是为了让校验代码可独立测试（`validation.rs` 末尾有大量单元测试）。

软开关如何翻译成有效值：

[effective_retry_config / effective_circuit_breaker_config — src/config/types.rs:L602-L618](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L602-L618)

> 注意：校验器在 `validate()` 里调用的是 `effective_*`，所以「关掉重试」之后即便 `retry.max_retries` 被你写成了 `0`，校验也不会因此报错——因为有效值已被改写为 `1`。

#### 4.1.4 代码实践

**实践目标**：用「不写一行配置」的方式，搞清楚网关的默认参数。

**操作步骤**：

1. 打开 `src/config/types.rs` 的 `impl Default for RouterConfig`（上面那一段）。
2. 找一张纸，把下面几个字段的默认值抄下来：`port`、`max_payload_size`、`request_timeout_secs`、`worker_startup_timeout_secs`、`max_concurrent_requests`、`queue_size`、`policy`、`connection_mode`。
3. 打开 `src/config/types.rs` 末尾的 `#[cfg(test)] mod tests`，阅读 `test_router_config_default`（[types.rs:L630-L659](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L630-L659)），对照它的 `assert_eq!` 验证你抄的默认值是否正确。

**需要观察的现象**：`Default` 实现里的字面量与测试里的断言应当一一对应；任何不一致都说明你抄错了。

**预期结果**：你会得到一张「默认参数表」，并能解释为什么 `request_timeout_secs` 默认高达 1800 秒（提示：注释写的是「30 分钟 for large model loading」）。

#### 4.1.5 小练习与答案

**练习 1**：`max_concurrent_requests` 为什么用 `i32` 而不是 `u32`？

> **答案**：因为它要表达「关闭限流」这个语义，项目约定用 `-1` 表示不限并发。`u32` 无法表示负数，所以选了带符号的 `i32`。`rate_limit_tokens_per_second` 用 `Option<i32>` 也是同理——`None` 表示「沿用 `max_concurrent_requests`」。

**练习 2**：为什么 `server_cert`、`client_identity`、`ca_certificates` 都标了 `#[serde(skip)]`？

> **答案**：它们是「字节内容」（证书、私钥），既庞大又可能含敏感信息。`skip` 让它们在序列化（如打印配置、写回 JSON、传给 Python 绑定）时被忽略，避免泄露密钥或污染输出。这些字节是在 `builder.rs` 的 `read_mtls_certificates` / `read_server_certificates` 阶段从磁盘读进来的（见 u2-l1）。

---

### 4.2 RoutingMode 与 ConnectionMode：两个正交维度

#### 4.2.1 概念说明

u1-l3 讲过网关的「五种模式」其实是由**三个正交维度**组合出来的，本讲把它们落到类型层面：

- **`RoutingMode`**（路由模式）：描述**部署形态**，有三个变体。
  - `Regular { worker_urls }`：最普通的一群对等 worker。
  - `PrefillDecode { prefill_urls, decode_urls, prefill_policy, decode_policy }`：Prefill/Decode 分离部署，两组 worker 分别负责「预填充」和「解码」，且可以各自带独立策略。
  - `OpenAI { worker_urls }`：把网关当 OpenAI 兼容代理（u8-l1）。
- **`ConnectionMode`**（连接方式）：描述**和 worker 用什么协议通信**，定义在 `core/worker.rs`，只有两个变体：`Http`（默认）和 `Grpc { port: Option<u16> }`。

这两个维度是**正交**的：`RoutingMode` 管「worker 怎么分组」，`ConnectionMode` 管「和每组 worker 用什么协议」。`Regular + Http` 是最常见的组合（u1-l3 的快速启动就是它）；`Regular + Grpc` 则走全 Rust gRPC 数据面（u7 单元）。

`RoutingMode` 还内嵌了一组实用方法：`is_pd_mode()` 判断是否分离部署、`worker_count()` 数 worker 总数、`get_prefill_policy()` / `get_decode_policy()` 取「PD 模式下的有效策略」（没单独配就回退到主策略 `policy`）。这些方法在数据面选 worker 时会被反复调用。

#### 4.2.2 核心流程

PD 模式下「有效策略」的解析逻辑值得单独画一下：

```text
取 prefill 有效策略：
  如果 PrefillDecode.prefill_policy 是 Some(p)  → 用 p
  否则                                          → 回退到 config.policy（主策略）

取 decode 有效策略：
  如果 PrefillDecode.decode_policy 是 Some(p)  → 用 p
  否则                                          → 回退到 config.policy（主策略）
```

`worker_count()` 对三种模式的统计口径不同，这是校验器判断「worker 数量是否够用」的基础（比如 `power_of_two` 策略要求至少 2 个 worker）：

| 模式 | `worker_count()` |
| --- | --- |
| `Regular` | `worker_urls.len()` |
| `PrefillDecode` | `prefill_urls.len() + decode_urls.len()` |
| `OpenAI` | 恒为 `1`（单端点代理） |

#### 4.2.3 源码精读

`RoutingMode` 枚举用 `#[serde(tag = "type")]` 做了内部标签，序列化形如 `{"type":"regular","worker_urls":[...]}`：

[RoutingMode 枚举 — src/config/types.rs:L178-L196](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L178-L196)

> 注意 `PrefillDecode` 的 `prefill_urls: Vec<(String, Option<u16>)>` —— 第二个元素是可选的 **bootstrap port**（u4-l4 详述），这正是 u2-l1 讲的「`--prefill <url> [port]` 可选第二值」在类型层面的落点。

`RoutingMode` 的实用方法：

[RoutingMode impl（is_pd_mode / worker_count / get_*_policy） — src/config/types.rs:L198-L236](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L198-L236)

> `worker_count()` 对 `OpenAI` 恒返回 `1`——这点很关键，校验器据此知道 OpenAI 模式天然不满足「≥2 worker」的要求，所以 `power_of_two` 等策略在 OpenAI 模式下会被拦（见 4.5）。

`ConnectionMode` 定义在 core 层（不在 config 里），因为它和 `Worker` 抽象紧密耦合：

[ConnectionMode 枚举 — src/core/worker.rs:L425-L436](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L425-L436)

> `Http` 标了 `#[default]`，所以 `RouterConfig` 默认就是 HTTP 连接。`Grpc` 带一个可选 `port`，用于「gRPC 端口和 URL 里写的不一样」的情况。

`mode_type()` / `has_service_discovery()` 这类「便捷查询」方法也定义在 `RouterConfig` 上：

[mode_type 与 has_* 便捷方法 — src/config/types.rs:L575-L600](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L575-L600)

> 这些方法把「字段组合判断」封装成语义化方法，避免调用方到处写 `self.discovery.as_ref().is_some_and(|d| d.enabled)` 这种啰嗦表达式。

#### 4.2.4 代码实践

**实践目标**：通过阅读测试，验证 `worker_count()` 与 `get_*_policy()` 的回退行为。

**操作步骤**：

1. 阅读 `test_routing_mode_worker_count`（[types.rs:L751-L781](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L751-L781)），对照三种模式的断言。
2. 阅读 `test_pd_policy_fallback_none_specified`（[types.rs:L1331-L1365](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L1331-L1365)），看「prefill_policy 和 decode_policy 都是 None」时如何回退到主策略。
3. 运行：`cargo test --lib config::types::tests::test_pd_policy_fallback_ -- --nocapture`。

**需要观察的现象**：测试全部通过；`worker_count` 对 PD 模式是「prefill + decode」之和。

**预期结果**：你会确认「PD 模式没单独配策略时，两组都用主策略」这一回退语义。运行命令的具体耗时与输出格式待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：一个 `PrefillDecode` 配置有 2 个 prefill、3 个 decode，它的 `worker_count()` 是多少？如果给它配 `power_of_two` 作为 `prefill_policy`，校验能过吗？

> **答案**：`worker_count() = 5`。但能否过校验取决于 `validate_compatibility` 的「细分」规则——它要求 **prefill 这一组的 worker 数 ≥ 2**（不是总数）。本例 prefill 恰好 2 个，能过；如果只有 1 个 prefill 就会被拦（见 4.5 的 `test_validate_pd_mode_power_of_two_insufficient_workers`）。

**练习 2**：为什么 `ConnectionMode` 不放在 `src/config/` 里，而放在 `src/core/worker.rs`？

> **答案**：`ConnectionMode` 是 `Worker` 抽象的一部分（worker 自身就用这个字段描述自己的协议），core 是被各区域共享的底座。config 只是「引用」它作为一个配置维度。这也是 u1-l4 说的「config 在最底层、core 是跨区域底座」的体现——这里 `core::ConnectionMode` 被 `config::types` 复用。

---

### 4.3 可靠性三件套：RetryConfig、CircuitBreakerConfig、HealthCheckConfig

#### 4.3.1 概念说明

这三个子配置都嵌在 `RouterConfig` 里，对应可靠性层的三种机制（u6 单元会讲它们的运行时实现，本讲只讲**配置字段**）：

- **`RetryConfig`**（重试）：请求失败后重试几次、退避多久。带指数退避 + 抖动。
- **`CircuitBreakerConfig`**（熔断）：单个 worker 连续失败到一定程度就「拉闸」，停止往它转发，过一段时间再「半开」试探。
- **`HealthCheckConfig`**（健康检查）：周期性探活 worker 的健康端点，连续失败/成功若干次后翻转健康状态。

三者都遵循同样的设计：**字段集中在一个结构体 + 提供合理的 `Default`**，且都可以用「软开关」整体关掉。

#### 4.3.2 核心流程

重试的退避时间计算（这是本讲唯一需要一点数学的地方）。设第 `k` 次重试，原始退避 `D` 为：

\[
D_k = \min\big(\text{initial\_backoff\_ms} \cdot \text{backoff\_multiplier}^{\,k},\ \text{max\_backoff\_ms}\big)
\]

再叠加一个抖动因子 `j = jitter_factor`，实际等待时间：

\[
D'_k = D_k \cdot \big(1 + U[-j, +j]\big)
\]

其中 \(U[-j, +j]\) 是区间 \([-j, +j]\) 上的均匀随机数。抖动的目的是**避免一群客户端在同一时刻集体重试（thundering herd）**。这段公式正好写在 `RetryConfig.jitter_factor` 字段的注释里。

熔断器是一个三态状态机（运行时实现见 u6-l2，这里只讲配置驱动它的参数）：

```text
closed（正常转发）
  │ 失败数在 window_duration_secs 窗口内 ≥ failure_threshold
  ▼
open（熔断，拒绝转发）
  │ 经过 timeout_duration_secs
  ▼
half-open（放行少量请求试探）
  │ 连续成功 success_threshold 次 → closed
  │ 再次失败                    → open
```

`HealthCheckConfig` 则用 `failure_threshold` / `success_threshold` 控制健康状态的翻转阈值，用 `check_interval_secs` 控制探活周期，`endpoint` 指定探活路径（默认 `/health`），`disable_health_check` 是总开关。

#### 4.3.3 源码精读

`RetryConfig` 及其默认值（`max_retries = 5`、初始 50ms、上限 30s、倍率 1.5、抖动 0.2）：

[RetryConfig 与 Default — src/config/types.rs:L400-L426](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L400-L426)

> 注释里直接写了抖动公式 `D' = D * (1 + U[-j, +j])`，这是配置字段文档化的好习惯。

`CircuitBreakerConfig` 及其默认值（失败阈值 10、成功阈值 3、熔断 60s、窗口 120s）：

[CircuitBreakerConfig 与 Default — src/config/types.rs:L452-L470](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L452-L470)

`HealthCheckConfig` 及其默认值（失败 3 次、成功 2 次、超时 5s、间隔 60s、端点 `/health`）：

[HealthCheckConfig 与 Default — src/config/types.rs:L428-L450](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L428-L450)

> 三个结构体风格高度一致：字段 + `Default`。这种一致性让你一眼就能找到「默认行为」。

#### 4.3.4 代码实践

**实践目标**：用一个故意写坏的 `RetryConfig`，验证退避参数之间的约束关系。

**操作步骤**（阅读型实践，也可写成测试）：

1. 阅读 `validate_retry`（[validation.rs:L442-L479](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L442-L479)）。注意它有四条约束：`max_retries ≥ 1`、`initial_backoff_ms > 0`、`max_backoff_ms ≥ initial_backoff_ms`、`backoff_multiplier ≥ 1.0`、`jitter_factor ∈ [0,1]`。
2. 构造一个 `RetryConfig { max_backoff_ms: 10, initial_backoff_ms: 100, .. }`（上限比初值还小），把它塞进一个合法的 `RouterConfig`，调用 `validate()`。
3. 观察：应当返回 `InvalidValue { field: "retry.max_backoff_ms", reason: "Must be >= initial_backoff_ms" }`。

**需要观察的现象**：错误信息精确指出了「哪个字段、当前值、为什么不对」。

**预期结果**：你拿到一条 `InvalidValue` 错误，错误串为 `Invalid value for field 'retry.max_backoff_ms': 10 - Must be >= initial_backoff_ms`（由 thiserror 模板 `#[error("Invalid value for field '{field}': {value} - {reason}")]` 渲染）。完整运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：用户把 `--disable-retries` 打开了，但同时在配置文件里写了 `max_retries: 0`，`validate()` 会报错吗？

> **答案**：不会。因为 `validate()` 校验的是 `effective_retry_config()` 的返回值，而 `disable_retries = true` 会把有效 `max_retries` 改写成 `1`，`1 ≥ 1` 满足约束。这就是「软开关 + effective 校验」设计的妙处——它不会和「阈值必须 ≥ 1」的硬规则打架。

**练习 2**：`CircuitBreakerConfig` 的 `window_duration_secs` 和 `timeout_duration_secs` 分别用在哪？

> **答案**：`window_duration_secs` 是「统计失败次数的滑动窗口长度」（在窗口内累计失败达 `failure_threshold` 就 open）；`timeout_duration_secs` 是「open 状态持续多久后转入 half-open」。两者是不同阶段的时间参数，不能混用，所以校验器要求它们都 `> 0`。

---

### 4.4 HistoryBackend 与存储后端配置

#### 4.4.1 概念说明

`HistoryBackend` 是一个枚举，决定「会话历史（/v1/conversations 等）存在哪里」。它有五个变体：

| 变体 | 含义 | 是否需要额外凭据 |
| --- | --- | --- |
| `Memory` | 进程内内存（默认） | 否 |
| `None` | 不存历史 | 否 |
| `Oracle` | Oracle 数据库（ATP） | **是**，需要 `OracleConfig` |
| `Postgres` | PostgreSQL | **是**，需要 `PostgresConfig` |
| `Redis` | Redis | **是**，需要 `RedisConfig` |

`HistoryBackend` 本身定义在外部 crate `data-connector`（`Cargo.toml` 里 `data-connector = "=1.0.0"`），`config/types.rs` 通过 `pub use data_connector::{HistoryBackend, OracleConfig, PostgresConfig, RedisConfig};` 把它重导出进来。CLI 侧（`src/main.rs`）用一个字符串参数 `--history-backend` 配合 `value_parser = ["memory","none","oracle","postgres","redis"]` 限定取值，再翻译成枚举。

这里有一个贯穿全节的设计原则：**「选了外部后端就必须附带凭据」。** `RouterConfig` 里的 `oracle` / `postgres` / `redis` 三个字段都是 `Option<...>`，默认 `None`。校验器会在 `history_backend == Oracle` 但 `oracle` 是 `None` 时直接报「缺字段」。这是一种「跨字段一致性」校验——单个字段都对，但组合起来不合法。

#### 4.4.2 核心流程

Oracle 后端的校验流程最有代表性，它把「跨字段一致性」和「值域校验」串在了一起：

```text
validate() 主流程走到这一步：
  if history_backend == Oracle:
      if oracle is None  → MissingRequired { field: "oracle" }   # 选了但完全没给配置
      else validate_oracle(oracle):
          username 空            → MissingRequired { field: "oracle.username" }
          password 空            → MissingRequired { field: "oracle.password" }
          connect_descriptor 空  → MissingRequired { field: "oracle_dsn or oracle_tns_alias" }
          pool_min < 1           → InvalidValue { field: "oracle.pool_min", ... }
          pool_max < pool_min    → InvalidValue { field: "oracle.pool_max", ... }
          pool_timeout_secs == 0 → InvalidValue { field: "oracle.pool_timeout_secs", ... }
```

注意错误信息里用的是**用户可见的 CLI 参数名**（`oracle_dsn or oracle_tns_alias`）而不是内部字段名（`connect_descriptor`），这是因为这个字段在 CLI 侧由两个参数（`--oracle-dsn` / `--oracle-tns-alias`）之一提供——错误信息要贴近用户的使用方式。

#### 4.4.3 源码精读

`HistoryBackend` 等类型从 `data_connector` 重导出：

[重导出存储类型 — src/config/types.rs:L1-L8](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L1-L8)

> `OracleConfig` / `PostgresConfig` / `RedisConfig` 都来自外部 crate，本讲只把它们当「需要凭据的不透明结构体」对待。

`RouterConfig` 里三个可选后端字段，注释明确写了「Required when ...」：

[oracle / postgres / redis 可选字段 — src/config/types.rs:L67-L77](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L67-L77)

> 默认 `history_backend = Memory`（`default_history_backend()`），所以三个外部后端字段默认全是 `None`，开箱即用不需要任何数据库。

Oracle 校验链（跨字段一致性 + 值域）：

[validate_oracle — src/config/validation.rs:L47-L91](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L47-L91)

> 三个 `MissingRequired` 对应「凭据三件套」（用户名/密码/连接串），三个 `InvalidValue` 对应「连接池参数的值域」。`pool_max < pool_min` 这种「相对关系」错误也归到 `InvalidValue`。

主流程里触发 Oracle 校验的那一段：

[validate 中对 Oracle 的分支 — src/config/validation.rs:L31-L40](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L31-L40)

> 这就是本讲实践任务要触发的代码路径：`history_backend == Oracle` 且 `oracle.is_none()` ⇒ `MissingRequired { field: "oracle" }`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：构造一个非法的 `RouterConfig`（选了 Oracle 但没给凭据），调用 `validate()`，观察 `ConfigError` 的错误信息分类。

**操作步骤**：

1. 在 `src/config/validation.rs` 的 `#[cfg(test)] mod tests` 里，仿照现有测试新增一个测试函数（示例代码，非项目原有代码）：

   ```rust
   #[test]
   fn test_oracle_without_credentials_is_rejected() {
       // 选了 Oracle 后端，但不提供 oracle 配置
       let mut config = RouterConfig::new(
           RoutingMode::Regular { worker_urls: vec!["http://w1:8000".to_string()] },
           PolicyConfig::Random,
       );
       config.history_backend = HistoryBackend::Oracle;
       config.oracle = None;

       let result = ConfigValidator::validate(&config);
       assert!(result.is_err());

       // 观察错误分类：应当是 MissingRequired { field: "oracle" }
       let err = result.unwrap_err().to_string();
       assert!(err.contains("Missing required field: oracle"), "got: {err}");
   }
   ```

2. 再加第二个测试：提供 `oracle` 但用户名为空，期望错误变成 `Missing required field: oracle.username`。由于 `OracleConfig` 来自外部 crate，你可以先 `cargo doc --open --package data-connector`（或读 `bindings/python` 里的等价类型）确认字段名，再用 `OracleConfig { username: String::new(), password: "p".into(), connect_descriptor: "dsn".into(), .. }` 之类的写法构造。

3. 运行：`cargo test --lib config::validation::tests::test_oracle_without_credentials_is_rejected -- --nocapture`。

**需要观察的现象**：

- 第一个测试的错误串是 `Missing required field: oracle`（`ConfigError::MissingRequired` 变体）。
- 第二个测试的错误串是 `Missing required field: oracle.username`（同样是 `MissingRequired`，但 `field` 更细）。
- 对照 4.5 的错误分类表，确认这两个都属于「缺字段」类，而非「值域」或「不兼容」类。

**预期结果**：两个测试都通过，证明「跨字段一致性」校验确实把「选了外部后端但没给凭据」的情形拦在了启动前。`OracleConfig` 的精确字段名与默认构造方式待本地用 `cargo doc` 确认（它是外部 crate，字段集合以本地文档为准）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `oracle` / `postgres` / `redis` 是三个独立字段，而不是合并成一个 `enum StorageBackendConfig`？

> **答案**：为了让 serde 能把它们各自 `skip_serializing_if = "Option::is_none"` 地处理，并且让校验器能按 `history_backend` 的取值**只校验对应那一个**字段（选 Oracle 只查 `oracle`，不强迫你填 `redis`）。如果合并成枚举，反序列化和「按需校验」都会变复杂。

**练习 2**：错误信息里为什么写 `oracle_dsn or oracle_tns_alias`，而不是内部字段名 `connect_descriptor`？

> **答案**：因为 `connect_descriptor` 这个内部字段在 CLI 侧是由 `--oracle-dsn` 或 `--oracle-tns-alias`（外加 `--oracle-wallet-path`）两个参数之一提供的（见 `main.rs` 的 `resolve_oracle_connect_details`）。错误信息用用户实际敲的参数名，能让用户立刻知道该补哪个 flag，这是「错误信息贴近使用面」的设计。

---

### 4.5 ConfigError 与 validate() 校验链

#### 4.5.1 概念说明

`ConfigError` 是配置层唯一的错误类型，定义在 `src/config/mod.rs`，用 thiserror 派生。它有四个变体，分别对应四类「错法」：

| 变体 | 触发场景 | 模板 |
| --- | --- | --- |
| `ValidationFailed { reason }` | 通用 / 跨字段的「状态不对」（如 selector 为空、证书为空） | `Validation failed: {reason}` |
| `InvalidValue { field, value, reason }` | 单个字段的**值域**错（如端口为 0、阈值越界） | `Invalid value for field '{field}': {value} - {reason}` |
| `IncompatibleConfig { reason }` | 字段间**不兼容**（如 power_of_two 配了不足 2 个 worker、PD 的 decode 用了 bucket） | `Incompatible configuration: {reason}` |
| `MissingRequired { field }` | 缺必填项（如选了 Oracle 但没给凭据） | `Missing required field: {field}` |

`ConfigResult<T> = Result<T, ConfigError>` 是整个 config 层的返回类型。`validate()` 返回 `ConfigResult<()>`——只关心「过 / 不过」，过了就 `Ok(())`，不过就带着分类好的错误返回。

#### 4.5.2 核心流程

`ConfigValidator::validate` 是一条**顺序校验链**，前一步不过就直接 `?` 返回（短路）。它的调用顺序如下，这个顺序决定了「一个配置有多处问题时，你会先看到哪个错」：

```text
validate(config):
  1. validate_mode(mode)              # URL 格式、bootstrap 端口、PD 策略初查
  2. validate_policy(policy)          # 主策略参数值域
  3. validate_server_settings(config) # 端口/超时/限流/排队等基本值域
  4. (可选) validate_discovery        # 服务发现 selector 与模式兼容
  5. (可选) validate_metrics          # metrics 端口/主机
  6. (可选) validate_trace            # OTLP endpoint 格式 host:port
  7. validate_compatibility(config)   # mTLS、power_of_two worker 数、PD decode 不能用 bucket
  8. validate_retry(effective)        # 重试参数值域（校验有效值）
  9. validate_circuit_breaker(effective)  # 熔断参数值域（校验有效值）
 10. (条件) Oracle 校验                # history_backend==Oracle 时的凭据与连接池
 11. validate_tokenizer_cache         # L0/L1 缓存开关与容量
```

理解这个顺序有两个实际用处：**调试时知道下一个可能出错的地方**；以及**故意触发某个错时，要先把排在前面的项都配合法**（比如想看 Oracle 的错，就得先保证前面 1-9 步都不报错）。

#### 4.5.3 源码精读

`ConfigError` 四变体与 `ConfigResult` 类型别名：

[ConfigError 与 ConfigResult — src/config/mod.rs:L8-L27](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/mod.rs#L8-L27)

> 注意 `validation` 模块是 `pub(crate)` 的（`mod.rs` 第 3 行 `pub(crate) mod validation;`），外部拿不到 `ConfigValidator`，只能通过 `RouterConfig::validate()` 间接调用——这把校验实现藏起来，只暴露一个干净的公共入口。

校验链总入口（顺序就是 4.5.2 那张表）：

[ConfigValidator::validate — src/config/validation.rs:L6-L45](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L6-L45)

> 每一步都用 `?` 短路。`validate_retry` / `validate_circuit_breaker` 校验的是 `effective_*_config()` 的返回值（第 26-29 行），这是「软开关」能和硬约束共存的关键。

兼容性校验（`IncompatibleConfig` 的主要产地）：

[validate_compatibility — src/config/validation.rs:L553-L607](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L553-L607)

> 这里产出的都是 `IncompatibleConfig`：`power_of_two` 在非 IGW、非服务发现模式下要求 ≥2 worker；PD 模式的 `prefill_policy` 用 `power_of_two` 要求 ≥2 prefill；PD 模式的 `decode_policy` 不允许是 `bucket`。注意 `enable_igw = true` 会**直接 return Ok**（第 554-556 行），因为 IGW 多模型模式下 worker 是动态发现的，静态数量约束不适用。

URL 格式校验（最常见的 `InvalidValue` 来源之一）：

[validate_urls — src/config/validation.rs:L609-L650](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L609-L650)

> 三条规则：非空、必须以 `http://` / `https://` / `grpc://` 开头、`url::Url::parse` 能解析且 `host_str()` 非空。这条规则解释了为什么 worker URL 必须带协议前缀。

builder 在封口时调用 `validate()` 的那一行：

[build_with_validation — src/config/builder.rs:L673-L688](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L673-L688)

> 先读证书/TLS/MCP 文件（把字节塞进 config），再 `into()` 成 `RouterConfig`，最后 `if validate { config.validate()?; }`。`build()` 调 `build_with_validation(true)`，`build_unchecked()` 调 `build_with_validation(false)`——后者跳过校验，供「确定自己配对了」的测试或内部场景使用。

#### 4.5.4 代码实践

**实践目标**：用一个 `IncompatibleConfig` 错误，体会「字段都对、但组合不合法」这一类。

**操作步骤**：

1. 阅读现成测试 `test_validate_power_of_two_with_regular_mode`（[validation.rs:L812-L829](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L812-L829)）——它配 2 个 worker + power_of_two，是**合法**的。
2. 把其中一个 worker 删掉（只剩 1 个 worker），保持 `power_of_two` 策略，运行 `validate()`。
3. 观察：返回 `IncompatibleConfig { reason: "Power-of-two policy requires at least 2 workers" }`。

**需要观察的现象**：单个字段（`policy = power_of_two`、`worker_urls` 各自）都没有值域问题，错误来自**两者的组合**，所以归到 `IncompatibleConfig` 而不是 `InvalidValue`。

**预期结果**：错误串为 `Incompatible configuration: Power-of-two policy requires at least 2 workers`。运行结果待本地验证。

#### 4.5.5 小练习与答案

**练习 1**：如果我同时把端口设成 0、又选了 Oracle 但没给凭据，`validate()` 会报哪个错？

> **答案**：报端口错。因为 `validate_server_settings`（第 3 步）排在 Oracle 校验（第 10 步）前面，`port == 0` 会先以 `InvalidValue { field: "port", ... }` 短路返回。这就是理解校验顺序的实际价值——想知道先看到哪个错，就看哪步排在前面。

**练习 2**：`build_unchecked()` 和 `build()` 的区别是什么？什么场景该用前者？

> **答案**：`build()` 末尾执行 `validate()`，`build_unchecked()` 跳过。前者用于生产路径（任何用户输入都该校验）；后者用于「我已经从代码里保证了合法性」的内部场景，比如 `types.rs` 测试里大量用 `.build_unchecked()` 构造已知合法的配置去做别的事，省得每次都跑一遍校验。

---

## 5. 综合实践：一张「错误分类表」逆向工程

把本讲的知识串起来，做一个贯穿性练习：**用一组故意写坏的配置，把 `ConfigError` 的四个变体各触发一次，整理成一张「输入 → 错误变体 → 错误信息」对照表。**

**操作步骤**：

1. 在 `src/config/validation.rs` 的测试模块里新增一个测试，构造 **5 个** `RouterConfig`，分别对应下表，逐个调用 `validate()` 并 `println!` 出错误串（加 `--nocapture` 运行）：

   | 用例 | 构造方式 | 预期变体 | 预期错误串关键字 |
   | --- | --- | --- | --- |
   | A（合法基线） | 1 个 worker + Random | `Ok(())` | 无错误 |
   | B（缺字段） | A 基础上 `history_backend = Oracle`，`oracle = None` | `MissingRequired` | `Missing required field: oracle` |
   | C（值域） | A 基础上 `port = 0` | `InvalidValue` | `Invalid value for field 'port'` |
   | D（不兼容） | 1 个 worker + `power_of_two` 策略 | `IncompatibleConfig` | `Power-of-two policy requires at least 2 workers` |
   | E（URL 格式） | `worker_urls = vec!["invalid-url"]` | `InvalidValue` | `URL must start with http://, https://, or grpc://` |

2. 运行 `cargo test --lib config::validation::tests::<你的测试名> -- --nocapture`，把实际输出填进表格的「实际错误串」列。

3. **反思题**：把用例 B 的 `history_backend` 改成 `Postgres` 但仍不提供 `postgres` 配置，会触发同样的错误吗？（提示：`validate()` 主流程第 31-40 行**只**对 Oracle 做了 `MissingRequired` 检查，Postgres/Redis 的凭据校验由 `data_connector` 在实际建连时进行——这是一个值得记下的「校验粒度差异」。）

**需要观察的现象**：四种错误变体各自被触发一次，错误串与 thiserror 模板严格对应。

**预期结果**：你得到一张完整的「错误分类对照表」，并能据此判断任意一个配置错误会落在哪个变体、由校验链的第几步产出。用例 E 的精确报错文本、以及反思题里 Postgres 的实际行为待本地验证。

## 6. 本讲小结

- `RouterConfig` 是整个网关的「参数总表」，四十多个字段分成路由形态、服务能力、连接池、限流、可靠性、存储/安全/模型六组；**没显式赋值的字段一律来自 `impl Default`**，读那一坨就知道「开箱即用的真实行为」。
- 两个正交维度：`RoutingMode`（Regular / PrefillDecode / OpenAI）描述部署形态，`ConnectionMode`（Http / Grpc）描述通信协议；PD 模式的有效策略可通过 `get_prefill_policy` / `get_decode_policy` 回退到主策略。
- 可靠性三件套 `RetryConfig` / `CircuitBreakerConfig` / `HealthCheckConfig` 风格一致（字段 + `Default`）；`disable_retries` / `disable_circuit_breaker` 是软开关，通过 `effective_*_config()` 把「关闭」翻译成「阈值无效化」，且校验的是**有效值**。
- `HistoryBackend`（Memory/None/Oracle/Postgres/Redis）把外部后端设计成「**选了就必须附带凭据**」的可选字段，校验器据此做跨字段一致性检查。
- `ConfigError` 有四个变体——`ValidationFailed`（通用/跨字段状态）、`InvalidValue`（值域）、`IncompatibleConfig`（组合不兼容）、`MissingRequired`（缺必填）——`validate()` 是一条顺序短路的校验链，**顺序决定了多处问题时先看到哪个错**。

## 7. 下一步学习建议

- 本讲只讲了配置「有什么、对不对」，**没讲它怎么被消费**。下一讲 **u2-l3（server::startup 启动编排）** 会跟着 `server::startup` 看 `RouterConfig` 是如何驱动各子系统初始化的——你会看到这些字段真正「活」起来。
- 想理解可靠性三件套的**运行时实现**（重试的退避循环、熔断的三态机、健康检查的周期探活），请阅读 **u6-l1（重试执行器）**、**u6-l2（熔断器）**、**u3-l7（健康检查）**。
- 想深入各负载均衡策略（`PolicyConfig` 的八个变体）的字段含义与算法，请阅读 **u5 单元（负载均衡策略）**，本讲的 `validate_policy` 只是它们的「值域门卫」。
- 推荐继续精读的源码：`src/config/validation.rs` 末尾的测试模块（它是一份极好的「合法/非法配置样例库」），以及 `src/config/builder.rs` 的 `read_mtls_certificates` / `read_server_certificates`（理解证书字节字段是何时、如何被填充的）。
