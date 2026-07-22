# CLI 参数与 RouterConfig 构建

## 1. 本讲目标

在上一单元我们跑通了网关、看懂了源码目录的分层。本讲要回答一个更具体的问题：**我在命令行敲下去的一堆 `--worker-urls`、`--policy`、`--prefill` 是怎么变成程序内部那个配置对象的？**

学完本讲你应该能够：

- 看懂 `src/main.rs` 里用 `clap` 定义的 `CliArgs` 结构，以及它按 `help_heading` 划分的若干参数分组（Worker / Policy / PD / Discovery / Retry 等）。
- 跟着 `CliArgs::to_router_config` 这一个函数，理清「字符串形式的 CLI 参数」是如何被翻译成强类型的 `RoutingMode`、`PolicyConfig`、`ConnectionMode` 以及各种子配置的。
- 理解 `RouterConfig::builder()` 的链式构建（builder 模式）为何能做到「字段不重复、自动同步」，以及 `build()` 末尾的 `validate()` 校验。
- 弄懂 `--prefill <url> [bootstrap_port]` 这个**可选第二个值**为什么不能用 clap 直接表达，项目又是如何用「预扫描 + 过滤」的方式绕过这个限制的。
- 自己动手新增一个布尔型 CLI 参数，并把它一路接到 `RouterConfig` 里。

本讲只聚焦「CLI → 配置对象」这一段，**不**讨论配置对象之后怎么被 `server::startup` 消费（那是 u2-l3 的内容），也**不**逐字段解释每个配置的含义（那是 u2-l2 的内容）。

## 2. 前置知识

在进入源码前，先用大白话建立三个直觉。

### 2.1 什么是 CLI 参数解析

命令行程序启动时会收到一个字符串数组（`std::env::args()`），比如：

```
smg launch --worker-urls http://w1:8000 http://w2:8000 --policy round_robin
```

这些字符串本身没有结构。**CLI 解析器**（本项目用 [`clap`](https://docs.rs/clap)）的任务是把这些字符串翻译成一个 Rust 结构体，让你能像访问普通字段一样拿到 `worker_urls`、`policy`。clap 还顺手帮你生成了 `--help` 文本、参数校验和友好的报错。

### 2.2 什么是 builder 模式

直接用 `RouterConfig { 一大堆字段 }` 构造一个有几十个字段的结构体很痛苦：你必须把每个字段都写一遍，少写一个就编译报错。builder 模式的做法是：

1. 先 `RouterConfig::builder()` 拿到一个「半成品」构造器，内部已经填好默认值。
2. 用一连串返回 `Self` 的方法（`.host(...).port(...).policy(...)`）只改你关心的字段，像流水线一样链式调用。
3. 最后 `.build()` 把半成品「封口」，产出真正的 `RouterConfig`。

每个方法都 `return self`，所以可以一直 `.` 下去。本项目的 builder 还有个小技巧：它内部**直接包着一个 `RouterConfig`**，而不是把所有字段在 builder 里再抄一遍——这点我们在 4.3 节细讲。

### 2.3 配置对象在程序里的位置

回顾 u1-l4 的分层：`config` 是最底层。`RouterConfig` 就是这个底层的「中心数据结构」，控制面、数据面、可靠性层后面几乎都要读它。所以「把 CLI 翻译成 `RouterConfig`」这一步，本质上是给整个网关的运行**注入参数**。理解了这条翻译链，后面看任何子系统的默认行为都能溯源到某个 CLI 参数。

> 名词速查：`RoutingMode`（路由模式：Regular / PrefillDecode / OpenAI）、`ConnectionMode`（连接方式：Http / Grpc）、`PolicyConfig`（负载均衡策略）。它们的具体含义在 u1-l3、u2-l2 详述，本讲只关心它们**如何被构造出来**。

## 3. 本讲源码地图

本讲只涉及两个核心文件，外加两个「引用型」文件用于补全类型定义：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/main.rs` | 二进制入口，定义所有 CLI 参数与转换逻辑 | `CliArgs` 结构、`to_router_config`、`parse_policy`、`parse_prefill_args`、`main()` |
| `src/config/builder.rs` | `RouterConfig` 的 builder 实现 | `RouterConfigBuilder`、`builder()`、`build_with_validation` |
| `src/config/types.rs`（引用） | `RouterConfig` / `RoutingMode` / `PolicyConfig` 的类型定义 | 看字段长什么样，理解 builder 在填什么 |
| `src/config/mod.rs`（引用） | `ConfigError` 错误类型 | 理解 `to_router_config` 返回的 `ConfigResult` |

记忆口诀：**「main.rs 负责收（CLI），builder.rs 负责装（RouterConfig），中间靠 to_router_config 翻译」**。

## 4. 核心概念与源码讲解

### 4.1 CliArgs：用 clap 定义命令行参数

#### 4.1.1 概念说明

`CliArgs` 是一个普通的 Rust 结构体，但它的每个字段都挂着 `#[arg(...)]` 属性，clap 的派生宏（`#[derive(Parser)]`）会读这些属性，自动生成「把命令行字符串填进这些字段」的代码。换句话说，**`CliArgs` 就是「所有命令行参数」的一份声明式清单**。你只要在这个结构体里加一个字段，`--help` 里就会多一行，解析逻辑也就自动有了。

#### 4.1.2 核心流程

clap 解析一次命令行的过程大致是：

1. 读取 `std::env::args()`（或我们传入的过滤后参数列表）。
2. 对每个 `--xxx`，在 `CliArgs` 的字段里找匹配的 `long` 名。
3. 按字段的类型（`String` / `u16` / `bool` / `Vec<String>` / `Option<...>`）取值：
   - `bool` 字段是「开关」，出现即 `true`。
   - `Option<T>` 字段不出现就是 `None`。
   - `Vec<T>` 字段可重复或吃多个值。
4. 找不到或类型不合 → 打印错误并退出；全部 OK → 返回填好的 `CliArgs`。

本项目还在 clap 之上套了一层 `Cli`/`Commands`，用来支持 `smg launch ...` 这种子命令写法（见 4.1.3）。

#### 4.1.3 源码精读

先看顶层的命令骨架。`Cli` 把「子命令」和「直接参数」两种用法合二为一：

[main.rs:82-121](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L82-L121) 定义了带 `#[command(name = "sglang-router", alias = "smg", alias = "amg")]` 的顶层 `Cli`。三个别名解释了为什么 `sgl-model-gateway`、`smg`、`amg` 三个二进制其实是同一份代码（见 u1-l2）。`#[command(args_conflicts_with_subcommands = true)]` 表示「要么用子命令 `launch`，要么直接传参数」，两者都行。`Commands::Launch` 就是 `smg launch ...` 的落地。

真正装着所有参数的是 `CliArgs`：

[main.rs:133-631](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L133-L631) 是一整个 `CliArgs` 结构体，字段非常多，但每个字段都长得很有规律。关键是每个 `#[arg(...)]` 里的 `help_heading = "..."`，它把参数在 `--help` 里**分组显示**。本讲的学习目标里提到的几个分组对应如下：

- Worker Configuration：`host`、`port`、`worker_urls`（[main.rs:135-146](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L135-L146)）。
- Routing Policy：`policy`、`cache_threshold`、`dp_aware`、`enable_igw` 等（[main.rs:148-195](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L148-L195)）。
- PD Disaggregation：`pd_disaggregation`、`decode`、`prefill_policy`、`decode_policy` 等（[main.rs:197-220](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L197-L220)）。
- Retry Configuration、Circuit Breaker、Health Checks、Service Discovery 等后续分组依此类推。

挑三个有代表性的字段看写法：

```rust
// main.rs:149-151 —— 字符串策略，带「限定取值」与默认值
#[arg(long, default_value = "cache_aware",
      value_parser = ["random", "round_robin", "cache_aware",
                      "power_of_two", "prefix_hash", "manual"],
      help_heading = "Routing Policy")]
policy: String,
```

`value_parser` 是一个白名单：clap 会拒绝不在列表里的值，所以 `--policy foo` 会直接报错。注意这里 `policy` 是 `String` 而不是枚举——字符串到 `PolicyConfig` 枚举的翻译推迟到了 `parse_policy`（见 4.2.3）。

```rust
// main.rs:189-195 —— 典型的 bool 开关
#[arg(long, default_value_t = false, help_heading = "Routing Policy")]
dp_aware: bool,
```

`bool` 字段默认 `false`，命令行里写 `--dp-aware` 就置 `true`，这正是本讲实践任务要模仿的形态。

```rust
// main.rs:202-204 —— 可重复出现的 Vec
#[arg(long, action = ArgAction::Append, help_heading = "PD Disaggregation")]
decode: Vec<String>,
```

`ArgAction::Append` 表示「每写一次 `--decode X` 就往数组里追加一个」，所以能写多个 `--decode`。

最后注意一个容易混淆的点：`CliArgs` 里**没有 `--prefill` 字段**。`--prefill` 的处理完全绕开了 clap，原因在 4.4 节专门讲。

#### 4.1.4 代码实践

这是本讲的第一个动手任务，目标小而可验证。

1. **实践目标**：给 `CliArgs` 新增一个布尔型参数 `--enable-access-log`，并确认它能出现在 `--help` 里。
2. **操作步骤**：
   - 打开 `src/main.rs`，在 `CliArgs` 里（比如 `json_log` 字段附近，[main.rs:255-266](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L255-L266)）仿照 `json_log` 加一个字段：
     ```rust
     /// Enable access logging for each request
     #[arg(long, default_value_t = false, help_heading = "Logging")]
     enable_access_log: bool,
     ```
   - 运行 `cargo run -p sgl-model-gateway -- --help`（或 `cargo run --quiet -- --help`）。
3. **需要观察的现象**：`--help` 输出的 `Logging` 分组里多出一行 `--enable-access-log`。
4. **预期结果**：能看到该参数即成功。此时它还不会被消费（会有 `field is never read` 之类的编译告警，属正常，下一节会接上）。
5. 说明：本步**不**需要改其它文件，因为只是「声明参数」。完整接到 `RouterConfig` 的进阶版本放在第 5 节综合实践。

> 提示：`cargo run -- --help` 中间的 `--` 是把后面的内容原样传给程序，而不是当成 cargo 的参数。

#### 4.1.5 小练习与答案

**练习 1**：`policy` 字段为什么用 `String` 而不是直接定义一个 `Policy` 枚举让 clap 解析？

**参考答案**：因为同一个策略字符串在 PD 模式下要被 `parse_policy` 复用三次（主策略、`prefill_policy`、`decode_policy`，见 4.2.3），而且不同策略还带不同的子参数（如 `cache_threshold`）。用一个纯字符串字段 + 一个独立的 `parse_policy` 方法，可以把「读哪个 CLI 子参数」的逻辑集中在 `parse_policy` 里，避免在 clap 层面把策略和它的子参数耦合死。

**练习 2**：`decode: Vec<String>` 用了 `ArgAction::Append`，而 `worker_urls: Vec<String>` 用的是 `num_args = 0..`（[main.rs:144-146](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L144-L146)）。两者在命令行写法上有什么区别？

**参考答案**：`num_args = 0..` 允许 `--worker-urls a b c` 一次吃多个值；`ArgAction:: Append` 则是 `--decode a --decode b --decode c` 每次一个、靠重复 flag 累加。两者都能得到 `Vec`，只是命令行语法不同。

---

### 4.2 to_router_config：从 CLI 到配置对象的翻译中枢

#### 4.2.1 概念说明

`CliArgs` 只是把字符串收齐了，但它本身**不是**程序运行时用的配置。真正被各子系统消费的是 `RouterConfig`（定义在 `src/config/types.rs`）。`CliArgs::to_router_config` 就是这两者之间的**翻译器**：它读 `CliArgs` 的字段，经过一系列判断和构造，产出一个 `RouterConfig`。

这是一个典型的「**边界翻译层**」设计：外部表现（CLI 字符串）和内部模型（强类型配置）解耦，将来即使要支持从配置文件 / 环境变量加载，也只需新写一个翻译入口，内部 `RouterConfig` 不用动。

#### 4.2.2 核心流程

`to_router_config` 的执行可以拆成 6 个阶段：

```
┌─────────────────────────────────────────────────────────────┐
│ 1. 决定 RoutingMode                                         │
│    backend==Openai ? OpenAI                                 │
│    : pd_disaggregation ? PrefillDecode                      │
│    : Regular                                                │
├─────────────────────────────────────────────────────────────┤
│ 2. 解析策略   policy = parse_policy(self.policy)            │
├─────────────────────────────────────────────────────────────┤
│ 3. 组装子配置 discovery / metrics / trace_config            │
├─────────────────────────────────────────────────────────────┤
│ 4. 推断连接方式  ConnectionMode（按 URL 前缀 grpc://）       │
├─────────────────────────────────────────────────────────────┤
│ 5. 选择历史后端  history_backend + 按需构造 oracle/...       │
├─────────────────────────────────────────────────────────────┤
│ 6. 链式 builder 填充全部字段 -> .build()（含 validate）      │
└─────────────────────────────────────────────────────────────┘
```

注意阶段 1 的判断顺序：**先看 `backend`，再看 `pd_disaggregation`**。这就是 u1-l3 里讲的「真正决定路由器构造的是 `to_router_config` 里的 `RoutingMode`，而不是启动横幅那行 Mode 文字」。

#### 4.2.3 源码精读

整段翻译在 [main.rs:905-1076](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L905-L1076)。我们按阶段拆。

**阶段 1：决定 `RoutingMode`**，见 [main.rs:909-926](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L909-L926)：

```rust
let mode = if matches!(self.backend, Backend::Openai) {
    RoutingMode::OpenAI { worker_urls: self.worker_urls.clone() }
} else if self.pd_disaggregation {
    RoutingMode::PrefillDecode {
        prefill_urls,
        decode_urls: self.decode.clone(),
        prefill_policy: self.prefill_policy.as_ref().map(|p| self.parse_policy(p)),
        decode_policy: self.decode_policy.as_ref().map(|p| self.parse_policy(p)),
    }
} else {
    RoutingMode::Regular { worker_urls: self.worker_urls.clone() }
};
```

`RoutingMode` 是个枚举，定义在 [types.rs:178-196](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L178-L196)，三个变体正好对应三种拓扑。注意 `PrefillDecode` 变体里的 `prefill_policy` / `decode_policy` 是 `Option<PolicyConfig>`——如果用户没单独指定，就留空，后面由 `RoutingMode::get_prefill_policy` 回退到主策略（见 [types.rs:215-235](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L215-L235)）。

**阶段 2：解析策略**，核心是 `parse_policy`，见 [main.rs:759-789](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L759-L789)：

```rust
fn parse_policy(&self, policy_str: &str) -> PolicyConfig {
    match policy_str {
        "random" => PolicyConfig::Random,
        "round_robin" => PolicyConfig::RoundRobin,
        "cache_aware" => PolicyConfig::CacheAware {
            cache_threshold: self.cache_threshold,
            balance_abs_threshold: self.balance_abs_threshold,
            // ...读取 self 上同名的 CLI 子参数
        },
        // ... power_of_two / prefix_hash / manual
        _ => PolicyConfig::RoundRobin,   // 兜底
    }
}
```

这就是 4.1.5 练习 1 答案的具体体现：`parse_policy` 是 `&self` 方法，所以能直接读 `self.cache_threshold` 等。同一个方法被主策略和 PD 的 prefill/decode 策略复用。

**阶段 4：推断连接方式**，见 [main.rs:738-745](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L738-L745) 与调用处 [main.rs:974-977](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L974-L977)：

```rust
fn determine_connection_mode(worker_urls: &[String]) -> ConnectionMode {
    for url in worker_urls {
        if url.starts_with("grpc://") || url.starts_with("grpcs://") {
            return ConnectionMode::Grpc { port: None };
        }
    }
    ConnectionMode::Http
}
```

`ConnectionMode` 定义在 [worker.rs:425](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/core/worker.rs#L425) 附近，有 `Http` 和 `Grpc { port }` 两个变体。注意 OpenAI 模式被**强制**成 `Http`（[main.rs:974-975](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L974-L975)），因为它本质是 HTTP 代理。

**阶段 5：历史后端**，见 [main.rs:979-1001](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L979-L1001)。它先把字符串 `"oracle"`/`"redis"` 等翻译成 `HistoryBackend` 枚举，再**按需**调用 `build_oracle_config` / `build_redis_config` 构造对应的连接配置。这些 `build_*` 方法内部会做校验，比如选了 oracle 却没给凭据，就会返回 `ConfigError::MissingRequired`（见 [main.rs:816-870](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L816-L870)）。这也是 u2-l2 要做的「非法配置」实验的入口。

**阶段 6：链式填充**，是本函数最长的一段，见 [main.rs:1003-1075](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1003-L1075)。它就是不停地 `.xxx(...)`，最后 `builder.build()`。这里出现了大量 `maybe_*` 方法（如 `.maybe_api_key`、`.maybe_metrics`），它们的语义是「如果是 `Some` 就设，`None` 就保持默认」——下节细讲。

最后，函数签名返回 `ConfigResult<RouterConfig>`（[main.rs:905-908](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L905-L908)），`ConfigResult` 就是 `Result<T, ConfigError>`，`ConfigError` 定义在 [mod.rs:8-25](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/mod.rs#L8-L25)，分 `ValidationFailed` / `InvalidValue` / `IncompatibleConfig` / `MissingRequired` 四类。所以「翻译失败」（如缺凭据）会被向上抛，最终在 `main()` 里用 `?` 转成进程退出。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，不修改代码，目标是把翻译链看穿。

1. **实践目标**：追踪 `--policy power_of_two` 这个字符串是如何变成 `RoutingMode` 之外、被填进 `RouterConfig.policy` 的。
2. **操作步骤**：
   - 在 [main.rs:928](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L928) 看到 `let policy = self.parse_policy(&self.policy);`。
   - 点进 `parse_policy`（[main.rs:770-772](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L770-L772)），看到 `"power_of_two" => PolicyConfig::PowerOfTwo { load_check_interval_secs: 5 }`。注意这里的 `5` 是**硬编码**的，不来自任何 CLI 参数——这是个值得记住的细节。
   - 回到 [main.rs:1005](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1005) 的 `.policy(policy)`，确认它被塞进 builder。
3. **需要观察的现象**：整条链路是「CLI 字符串 → `parse_policy` → `PolicyConfig` → builder `.policy()` → `RouterConfig.policy`」。
4. **预期结果**：你能用自己的话讲清楚「为什么 `power_of_two` 的 `load_check_interval_secs` 是 5 而不是某个 CLI 参数」——因为它在 `parse_policy` 里被写死了。如果想改成可配置，需要新增 CLI 参数并改 `parse_policy`。
5. 待本地验证：上述硬编码值的确切位置已由源码确认；若你本地 HEAD 不同，行号可能漂移。

#### 4.2.5 小练习与答案

**练习 1**：如果用户同时传了 `--backend openai` 和 `--pd-disaggregation`，会发生什么？

**参考答案**：走 OpenAI 分支（[main.rs:911](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L911) 的判断在前），`pd_disaggregation` 被忽略，`RoutingMode::OpenAI` 胜出。这是「先 backend 后 PD」的固定优先级。

**练习 2**：`determine_connection_mode` 只要看到**一个** `grpc://` URL 就整体返回 `Grpc`。这种「混合 URL」会有什么隐患？

**参考答案**：它是「任一 grpc 则全 grpc」的粗粒度推断，并不校验「所有 URL 协议是否一致」。如果用户混传了 `http://` 和 `grpc://`，连接模式会被定成 `Grpc`，HTTP 的 worker 后续可能无法正常通信。真正的细粒度校验在 `validate()` 阶段（u2-l2）。

---

### 4.3 RouterConfig::builder：链式构建与自动同步

#### 4.3.1 概念说明

阶段 6 那一长串 `.xxx(...)` 调用的目标，是 `RouterConfigBuilder`。它解决两个问题：

1. **易用性**：不用一次性写齐 `RouterConfig` 的全部几十个字段。
2. **同步性**：本项目的 builder 内部**直接持有一个 `RouterConfig`**（而不是把字段在 builder 里抄一份）。这意味着 `RouterConfig` 加字段时，builder 不需要跟着加同名私有字段——只有当你想为它提供一个便捷 setter 时才动 builder。源码注释把这叫做「eliminates field duplication and stays in sync automatically」。

builder 还提供了三档「封口」方式：`build()`（带校验）、`build_unchecked()`（不校验）、`build_with_validation(bool)`（可选）。

#### 4.3.2 核心流程

```
RouterConfig::builder()          // 1. 拿到带全默认值的 builder
    .mode(mode)                  // 2. 一连串 setter，每个 return self
    .policy(policy)
    .maybe_api_key(...)
    ...
    .build()                     // 3. 封口：读文件(mTLS/TLS/MCP) -> validate -> RouterConfig
```

封口时 `build_with_validation` 会做三件「需要读磁盘」的事：读 mTLS 证书、读服务端 TLS 证书、读 MCP 配置文件（[builder.rs:673-688](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L673-L688)），因为这些内容（PEM 字节、解析后的 YAML）不希望在 builder 链上到处传递，集中在 `.build()` 一次性处理。

#### 4.3.3 源码精读

先看 builder 的结构，注意它「包着一个 config」的设计：

[builder.rs:12-22](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L12-L22)：

```rust
#[derive(Debug, Clone, Default)]
pub struct RouterConfigBuilder {
    config: RouterConfig,            // ← 核心：直接持有 config
    // 只有「需要延迟到 build() 再读文件」的字段才单独存路径
    client_cert_path: Option<String>,
    client_key_path: Option<String>,
    ca_cert_paths: Vec<String>,
    server_cert_path: Option<String>,
    server_key_path: Option<String>,
    mcp_config_path: Option<String>,
}
```

所以绝大多数 setter 都极简，就是把值写进 `self.config.字段`，然后 `self`。看一个普通 setter 和一组「布尔便捷 setter」：

[builder.rs:158-166](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L158-L166)（普通 setter）：

```rust
pub fn host<S: Into<String>>(mut self, host: S) -> Self {
    self.config.host = host.into();
    self
}
```

[builder.rs:472-487](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L472-L487)（布尔 setter，注意「取反」技巧）：

```rust
/// Inverse of disable_retries field
pub fn retries(mut self, enable: bool) -> Self {
    self.config.disable_retries = !enable;
    self
}
/// Inverse of disable_circuit_breaker field
pub fn circuit_breaker(mut self, enable: bool) -> Self {
    self.config.disable_circuit_breaker = !enable;
    self
}
```

这里有个**很重要的细节**：`RouterConfig` 里存的是 `disable_retries: bool`（「是否关闭」），但 CLI 传进来的是「是否开启」（`!self.disable_retries`，见 [main.rs:1069](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1069)）。所以 builder 的 `.retries(bool)` 做了一次取反 `!enable` 来弥合这两种语义。`.circuit_breaker(bool)`、`.dp_aware(bool)`、`.igw(bool)` 同理（[builder.rs:472-492](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L472-L492)）。这种「正负逻辑」的转换是阅读本模块的一个小坑。

还有一组 `maybe_*` setter，语义是「Option 里是 Some 才设」：

[builder.rs:497-502](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L497-L502)：

```rust
pub fn maybe_api_key(mut self, key: Option<impl Into<String>>) -> Self {
    if let Some(k) = key {
        self.config.api_key = Some(k.into());
    }
    self
}
```

为什么需要它？因为 CLI 里 `api_key` 是 `Option<String>`，用户没传就是 `None`；直接用 `.api_key(None)` 会把默认值覆盖成 `None`，而 `maybe_api_key(None)` 会**保留默认**。`to_router_config` 里凡是 `Option` 类型的 CLI 字段几乎都走 `maybe_*`（[main.rs:1050-1073](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1050-L1073)）。

最后看封口与入口：

[builder.rs:794-804](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L794-L804) 是 `RouterConfig::builder()` 与 `to_builder()` 的定义；`builder()` 就是 `RouterConfigBuilder::new()`，而 `new()` 又是 `Self::default()`——因为 `RouterConfigBuilder` 派生了 `Default`，而它的 `config: RouterConfig` 字段也会用 `RouterConfig::default()`（[types.rs:503-558](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L503-L558)）。**所以 builder 的「全默认值」其实来自 `RouterConfig::default()`**，这是理解所有默认行为的根。

[builder.rs:673-688](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L673-L688) 是 `build_with_validation`：

```rust
pub fn build_with_validation(mut self, validate: bool) -> ConfigResult<RouterConfig> {
    self = self.read_mtls_certificates()?;
    self = self.read_server_certificates()?;
    self = self.read_mcp_config()?;
    let config: RouterConfig = self.into();   // 把 builder 变回 config
    if validate {
        config.validate()?;
    }
    Ok(config)
}
```

`self.into()` 能成立，是因为有 `From<RouterConfigBuilder> for RouterConfig` 实现（[builder.rs:788-792](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L788-L792)），它就是 `builder.config`。最后的 `config.validate()` 委托给 [types.rs:571-573](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L571-L573)，进而走到 `ConfigValidator::validate`（[validation.rs:7-45](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/validation.rs#L7-L45)）。

#### 4.3.4 代码实践

这是一个**阅读 + 跑测试型实践**。

1. **实践目标**：直观感受 builder 的「round-trip」（config → builder → 改一两个 → config）能力。
2. **操作步骤**：
   - 阅读 `src/config/builder.rs` 末尾的测试 `test_builder_from_existing_config`（[builder.rs:811-830](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L811-L830)）。它先用 builder 构一个 config，再 `.to_builder().port(4000).enable_metrics(...).enable_trace(...)` 改几个字段。
   - 运行该测试：`cargo test -p sgl-model-gateway --lib config::builder::tests::test_builder_from_existing_config`。
3. **需要观察的现象**：测试通过，说明 `to_builder()` 正确地把已有 config 转回 builder，改动后其余字段（如 `regular_mode` 的 worker_urls）被原样保留。
4. **预期结果**：测试绿。如果失败，多半是环境/依赖问题，与本讲逻辑无关。
5. 待本地验证：`cargo test` 子命令路径因 workspace 布局可能略有差异；若上述路径报「not found」，可改用 `cargo test --lib builder` 再过滤。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `RouterConfigBuilder` 要把证书「路径」单独存（`client_cert_path` 等），而不是在 setter 里直接读成字节？

**参考答案**：因为 builder 是链式、可被反复 `clone` 的，在中间环节就读文件会带来副作用、也容易出错。集中到 `.build()` 里一次性读（`read_mtls_certificates` 等），既保证「构造时有副作用」只发生在最终封口，也方便把「路径成对校验」（cert 和 key 必须同时给，见 [builder.rs:722-728](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L722-L728)）放在一起处理。

**练习 2**：`.retries(true)` 和 `.enable_retries()`（若存在）效果是否一致？为什么 CLI 层用 `disable_retries` 而 builder 提供 `.retries(bool)`？

**参考答案**：效果一致——都是把 `config.disable_retries` 置 `false`。`RouterConfig` 内部用「disable」语义（默认 false=启用），是为了让 `effective_retry_config` 等方法的「关闭时把 max_retries 置 1」逻辑更直观（[types.rs:602-609](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L602-L609)）；而 builder 的 `.retries(bool)` 用「enable」语义，是因为 CLI 层和人类更习惯「是否启用」的正向表达。两者靠一次 `!` 取反衔接。

---

### 4.4 `--prefill` 可选 bootstrap 端口的特殊解析

#### 4.4.1 概念说明

PD（Prefill/Decode）模式下，prefill 节点的命令行写法是：

```
--prefill http://prefill1:30001 9001     # 9001 是 bootstrap port
--prefill http://prefill2:30002           # 不带 bootstrap port
--prefill http://prefill3:30003 none      # 显式写 none
```

也就是说，`--prefill` 后面**第一个值是 URL（必有），第二个值是端口号（可选，还可写成 `none`）**。问题来了：clap 很难优雅地表达「一个 long flag 后面跟 1 或 2 个值，且第二个值可能是数字、可能是 `none`、也可能不存在」——因为 clap 一旦看到下一个 `--xxx` 就会认为当前参数结束。

项目的解决方案是：**绕开 clap，自己在 `main()` 开头手工预扫描 `std::env::args()`，把所有 `--prefill` 及其尾随的 1~2 个 token 抠出来，然后从原始参数里删掉这些 token，再把「干净」的参数交给 clap。**

#### 4.4.2 核心流程

```
原始 argv
   │
   ├──(A) parse_prefill_args()  手工扫描，收集 Vec<(url, Option<port>)>
   │
   ├──(B) 过滤循环  从 argv 中删掉所有 --prefill 及其 1~2 个尾随 token
   │
   └──(C) Cli::parse_from(filtered_args)  clap 只看到「没有 --prefill」的干净参数
```

三步之后，`prefill_urls` 作为普通参数传进 `to_router_config(prefill_urls)`（[main.rs:1271](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1271)）。

#### 4.4.3 源码精读

**步骤 A：预扫描**，见 [main.rs:24-53](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L24-L53)：

```rust
fn parse_prefill_args() -> Vec<(String, Option<u16>)> {
    let args: Vec<String> = std::env::args().collect();
    let mut prefill_entries = Vec::new();
    let mut i = 0;
    while i < args.len() {
        if args[i] == "--prefill" && i + 1 < args.len() {
            let url = args[i + 1].clone();
            let bootstrap_port = if i + 2 < args.len() && !args[i + 2].starts_with("--") {
                if let Ok(port) = args[i + 2].parse::<u16>() {
                    i += 1; Some(port)           // 数字 → Some(port)
                } else if args[i + 2].to_lowercase() == "none" {
                    i += 1; None                  // "none" → None（但仍消耗这个 token）
                } else {
                    None                          // 既非数字也非 none → 不消耗
                }
            } else {
                None                              // 下一个是 --flag 或越界 → 无端口
            };
            prefill_entries.push((url, bootstrap_port));
            i += 2;
        } else {
            i += 1;
        }
    }
    prefill_entries
}
```

关键判断在 `!args[i + 2].starts_with("--")`：它用「下一个 token 是不是以 `--` 开头」来判定「这是不是 bootstrap port」。这有个**边界情况**——如果有人把端口号写错了（既不是数字也不是 `none`），这个 token 既不会被当成 port 消费，也不会在步骤 B 里被删除，最终会作为「多余位置参数」留给 clap，可能导致 clap 报错。这是一个值得知道的小坑。

**步骤 B：过滤**，见 [main.rs:1205-1222](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1205-L1222)：

```rust
let mut filtered_args: Vec<String> = Vec::new();
while i < raw_args.len() {
    if raw_args[i] == "--prefill" && i + 1 < raw_args.len() {
        i += 2;                       // 跳过 --prefill 和 URL
        if i < raw_args.len()
            && !raw_args[i].starts_with("--")
            && (raw_args[i].parse::<u16>().is_ok() || raw_args[i].to_lowercase() == "none")
        {
            i += 1;                    // 再跳过可选的 port/none
        }
    } else {
        filtered_args.push(raw_args[i].clone());
        i += 1;
    }
}
```

注意步骤 B 的判定比步骤 A **更严格**：它要求第三个 token「能解析成 u16 **或** 等于 none」才删掉。这保证了步骤 A 和步骤 B 对「第三个 token 是不是 port」的判定一致，不会出现「A 当成 port 消费了、B 却没删」的不一致。

**步骤 C：交给 clap**，见 [main.rs:1224](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1224) 的 `Cli::parse_from(filtered_args)`。此时参数里已经没有 `--prefill`，clap 不会因为「不认识的第二个值」而困惑。

最后，`prefill_urls` 在 `to_router_config` 里被原样塞进 `RoutingMode::PrefillDecode { prefill_urls, .. }`（[main.rs:916-921](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L916-L921)）。`Option<u16>` 这个「可选端口」就这样一路保留到了配置对象里，供 PD 路由器后续使用（u4-l4 详述）。

#### 4.4.4 代码实践

这是一个**跟踪 + 思考型实践**，不改动代码。

1. **实践目标**：验证 `--prefill` 的「可选第二值」三态（数字 / `none` / 缺省）分别被解析成什么。
2. **操作步骤**：
   - 假想三条命令，分别对应三种写法：
     - `smg launch --pd-disaggregation --prefill http://p1:30001 9001 --decode http://d1:30002`
     - `smg launch --pd-disaggregation --prefill http://p1:30001 none --decode http://d1:30002`
     - `smg launch --pd-disaggregation --prefill http://p1:30001 --decode http://d1:30002`
   - 对每条，人肉模拟 `parse_prefill_args`（[main.rs:24-53](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L24-L53)）的执行，写出 `prefill_entries` 的值。
3. **需要观察的现象**：三种写法下 `parse_prefill_args` 返回值分别是 `[(url, Some(9001))]`、`[(url, None)]`、`[(url, None)]`。
4. **预期结果**：后两种（`none` 和 缺省）结果相同，都是 `None`，但区别在于 `none` 会**消耗**第三个 token（步骤 B 会把它删掉），而缺省根本没第三个 token。
5. 待本地验证：可临时在 `parse_prefill_args` 末尾加一行 `println!("prefill_entries = {:?}", prefill_entries);` 跑一次确认（记得验证后还原，不要提交）。

#### 4.4.5 小练习与答案

**练习 1**：为什么步骤 A（`parse_prefill_args`）和步骤 B（过滤）要写两遍几乎相同的「第三个 token 判定」逻辑，而不是合并？

**参考答案**：它们的职责不同。步骤 A 只读不写，目的是**提取** `(url, port)`；步骤 B 要**重写** argv 数组、把 `--prefill` 相关 token 删掉。两者的循环结构相似，但操作不同（一个 push 到结果，一个 push 到 filtered 跳过 prefill）。虽然逻辑有重复，但合并会让控制流变复杂；项目选择了「两段直白代码」换取可读性。

**练习 2**：如果用户写 `--prefill http://p1:30001 abc`（`abc` 既不是数字也不是 `none`），整条命令会怎样？

**参考答案**：步骤 A 里 `abc` 不被当成 port，`prefill_entries` 得到 `(url, None)`，且循环指针不消费 `abc`（[main.rs:39-41](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L39-L41)）。但步骤 B 的判定要求「u16 或 none」才删除，`abc` 不满足，于是 `abc` 留在 `filtered_args` 里交给 clap，clap 大概率因为「多余的位置参数」报错。所以这种写法会失败——这是该手工解析方案的一个已知粗糙处。

---

## 5. 综合实践

把 4.1.4 的「半成品」做完：**新增一个布尔型 CLI 参数，并把它一路接到 `RouterConfig`，最后用 `--help` 和一个单元测试验证。** 这个任务贯穿本讲三个最小模块（CliArgs → to_router_config → builder）。

以「`--enable-access-log`」为例（你也可以换成任意不存在的布尔开关）：

1. **在 `CliArgs` 声明参数**（`src/main.rs`，4.1.4 已完成）：
   ```rust
   #[arg(long, default_value_t = false, help_heading = "Logging")]
   enable_access_log: bool,
   ```

2. **给 `RouterConfig` 加字段**（`src/config/types.rs`，参考 [types.rs:99-101](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L99-L101) 的 `enable_wasm` 写法）：
   ```rust
   #[serde(default)]
   pub enable_access_log: bool,
   ```
   并在 `Default` 实现（[types.rs:503-558](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/types.rs#L503-L558)）里补一行 `enable_access_log: false,`。

3. **在 builder 加 setter**（`src/config/builder.rs`，仿照 [builder.rs:376-379](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L376-L379) 的 `enable_wasm`）：
   ```rust
   pub fn enable_access_log(mut self, enable: bool) -> Self {
       self.config.enable_access_log = enable;
       self
   }
   ```

4. **在 `to_router_config` 接线**（`src/main.rs` 的 builder 链，参考 [main.rs:1071](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1071) 的 `.enable_wasm(self.enable_wasm)`）：
   ```rust
   .enable_access_log(self.enable_access_log)
   ```

5. **验证**：
   - `cargo run -p sgl-model-gateway -- --help` 应显示 `--enable-access-log`。
   - `cargo build` 应不再有「field never read」告警。
   - 进阶：在 `src/config/builder.rs` 的 `tests` 模块加一个断言，确认 `.enable_access_log(true).build_unchecked().enable_access_log == true`（仿照 [builder.rs:833-846](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L833-L846)）。

6. **预期结果**：`--help` 出现新参数，编译干净，测试通过。完成后**务必还原所有改动**（本讲义要求不修改源码，本实践仅供学习）。

> 这个练习揭示了本项目「加一个配置项」的标准四步套路：**声明 CLI → 加 config 字段 → 加 builder setter → 在 to_router_config 接线**。后续你在网关里加任何可配置开关，都走这条路。

## 6. 本讲小结

- `CliArgs`（[main.rs:133-631](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L133-L631)）是用 clap 派生宏声明的「全量参数清单」，靠 `help_heading` 分组；加一个字段就等于加一个 CLI 参数。
- `CliArgs::to_router_config`（[main.rs:905-1076](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L905-L1076)）是 CLI 与内部模型之间的翻译层，按「backend → pd → regular」优先级决定 `RoutingMode`，并用 `parse_policy`、`determine_connection_mode` 等把字符串翻译成强类型枚举。
- `RouterConfig::builder()`（[builder.rs:794-804](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L794-L804)）采用「builder 内部直接持有 config」的设计，避免字段重复；默认值全部来自 `RouterConfig::default()`。
- builder 的 `.retries(bool)`/`.circuit_breaker(bool)` 等做了一次「正负逻辑取反」，`maybe_*` 系列 setter 保证 `None` 不覆盖默认值。
- `.build()` 在封口时集中读 mTLS/TLS/MCP 文件并跑 `validate()`（[builder.rs:673-688](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/config/builder.rs#L673-L688)）。
- `--prefill <url> [port]` 的「可选第二值」无法用 clap 表达，项目用「预扫描 `parse_prefill_args` + 过滤 argv + 再交给 clap」三步绕过（[main.rs:24-53](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L24-L53) 与 [main.rs:1205-1224](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1205-L1224)）。

## 7. 下一步学习建议

本讲只讲了「CLI 如何变成 `RouterConfig`」，但 deliberately 跳过了两件事：

- **`RouterConfig` 里那些字段到底什么含义、`validate()` 具体校验了什么** → 这是 **u2-l2《配置类型与校验》** 的主题，建议接着读 `src/config/types.rs` 全文和 `src/config/validation.rs`，并动手构造一个非法 config（如 oracle 缺凭据）观察 `ConfigError`。
- **`RouterConfig` 构建好之后，`server::startup` 是怎么消费它的** → 这是 **u2-l3《server::startup 启动编排》** 的主题，跟踪 `main()` 最后那行 `server::startup(server_config)`（[main.rs:1275](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1275)）。

如果想立刻加深本讲的理解，可以再读一遍 `CliArgs::to_server_config`（[main.rs:1078-1186](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/main.rs#L1078-L1186)）——它是 `to_router_config` 的姊妹方法，负责把那些「不属于 RouterConfig、而属于 ServerConfig」的参数（如 Prometheus、mesh、控制面认证）再装一遍，对照阅读能让你彻底看清 main.rs 的全貌。
