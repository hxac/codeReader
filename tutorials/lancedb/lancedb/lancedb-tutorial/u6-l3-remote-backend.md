# 远程后端：HTTP 客户端与重试

## 1. 本讲目标

本讲是「存储后端与架构」单元的收尾，承接 u6-l2 的 `Database` trait 与命名空间模型，专门讲解远程后端（`remote` 模块）这一套实现。

本地后端是「函数调用直达底层 Lance」，而远程后端是「把每一次 API 调用翻译成一次 HTTP 往返，发给 LanceDB Cloud / Enterprise 服务端」。学完本讲，你应当能够：

- 说清 `RemoteDatabase` / `RemoteTable` 如何把「建表、查表名、查询」这类高层操作转成具体的 HTTP 请求（方法、路径、请求体、头部）。
- 掌握 `RestfulLanceDbClient` 如何构造 `reqwest::Client`、注入认证头、生成请求 ID、并发送请求。
- 掌握重试策略：`RetryConfig` 的三套计数器（连接 / 读取 / 请求）、指数退避加抖动公式、`send_with_retry` 的重试循环，以及「写请求不在 5xx 上重试」的安全取舍。
- 理解 `ServerVersion` 如何通过响应头探测服务端能力，从而让同一份客户端代码兼容多个版本的服务端。
- 看懂 `RemoteTable` 如何封装句柄状态、注入读新鲜度头部、并对流式写入做「缓冲以支持重试」的处理。

本讲只覆盖一个最小模块：**remote**。

## 2. 前置知识

阅读本讲前，建议你先建立以下认知（前序讲义已铺垫）：

- **u6-l2**：`Database` trait 是「表集合」的统一契约，`Connection` 只是一层外壳、几乎所有方法都一行转发给内部的 `Arc<dyn Database>`。远程后端就是 `Database` 的三套实现之一（另两套是 `ListingDatabase` 与 `LanceNamespaceDatabase`）。
- **u2-l1 / u2-l2**：`connect(uri)` 在 `.execute()` 时按 URI 前缀分流，`db://` 开头走远程；`Table` 句柄持有 `Arc<dyn BaseTable>`，远程表对应 `RemoteTable`。
- **u2-l4**：自定义 `Error` 枚举与 `Result` 别名，远程模块新增了两个变体 `Error::Http` 与 `Error::Retry`（均受 `remote` feature 条件编译控制）。
- **u5-l3**：读一致性与读新鲜度。远程命名空间后端独有的「读自己的写」机制靠注入 `x-lancedb-min-timestamp` 头实现，本讲会落到具体源码。

补充两个本讲会用到的术语：

- **reqwest**：Rust 生态主流的异步 HTTP 客户端库。远程后端的所有网络收发都委托给它。
- **Arrow IPC Stream**：Apache Arrow 的一种线格式（`application/vnd.apache.arrow.stream`）。远程后端用它在 HTTP 请求/响应体里传输 `RecordBatch`，实现零拷贝跨语言（承接 u1-l4）。

## 3. 本讲源码地图

本讲涉及的关键文件都位于 `rust/lancedb/src/remote/`（模块入口是 `rust/lancedb/src/remote.rs`）：

| 文件 | 作用 |
| --- | --- |
| `remote.rs` | 模块入口，声明子模块、定义内容类型常量、公开导出 `ClientConfig`/`RetryConfig` 等。 |
| `remote/client.rs` | HTTP 客户端 `RestfulLanceDbClient`：构造 reqwest、认证头、`send` 与 `send_with_retry`、响应检查。 |
| `remote/db.rs` | `RemoteDatabase`：实现 `Database` trait，把表级操作翻译成 HTTP；定义 `ServerVersion`。 |
| `remote/retry.rs` | 重试核心：`RetryCounter` 计数器、`ResolvedRetryConfig`（带默认值的解析结果）。 |
| `remote/table.rs` | `RemoteTable`：实现 `BaseTable`，封装表状态、读新鲜度、可重试写入、能力探测分支。 |
| `remote/util.rs` | 工具函数：Arrow 流转 HTTP body、从响应头解析 `ServerVersion`。 |

## 4. 核心概念与源码讲解

### 4.1 远程后端架构与 HTTP 调用链

#### 4.1.1 概念说明

本地后端里，`NativeTable` 调用底层 `lance::Dataset` 就是普通的进程内函数调用，数据从不离开本机。远程后端则不同：客户端（你的应用）和服务端（LanceDB Cloud / Enterprise）是两个进程，中间隔着一层网络。`RemoteDatabase` 和 `RemoteTable` 的全部职责，就是**把高层 API 翻译成 RESTful HTTP 调用，再把 HTTP 响应翻译回 Rust 对象**。

为此远程后端引入了一条分层调用链：

```
Connection / Table（对外句柄）
        │  委托
        ▼
RemoteDatabase / RemoteTable（翻译：操作 → HTTP 请求）
        │  调用
        ▼
RestfulLanceDbClient（构造请求、认证头、重试、发送）
        │  委托
        ▼
reqwest::Client（真正的网络 IO）
        │
        ▼
LanceDB 服务端
```

这条链上每一层都只做一件事：上层「说业务」，中层「说 HTTP」，底层「说网络」。这种分层让你可以在测试里用 mock 替换最底层的网络发送（见 4.1.4），而不必真的连上云端。

> 与 u6-l1 的呼应：远程后端不直接碰对象存储，它只跟服务端 HTTP API 对话；对象存储由服务端负责。所以 `remote` 模块里没有 `object_store`，只有 `reqwest`。

#### 4.1.2 核心流程

以「列出所有表名」为例，完整调用链是：

1. 用户调用 `conn.table_names().execute()`。
2. `Connection` 转发给 `RemoteDatabase::table_names`。
3. `table_names` 用 `client.get("/v1/table/")` 构造一个 GET 请求构建器。
4. 调用 `client.send_with_retry(req, None, true)` 发送（读请求，允许在 5xx 上重试）。
5. `send_with_retry` 内部循环：克隆请求 → 注入 `x-request-id` 与动态头 → `Sender::send` → 根据状态码决定成功 / 重试 / 失败。
6. 成功后 `check_response` 校验状态码，`parse_server_version` 从响应头读取服务端版本。
7. 把响应体 JSON 反序列化成 `ListTablesResponse`，返回表名列表。

建表、查表、查询都是同样的套路，只是 HTTP 方法、路径、内容类型不同。

#### 4.1.3 源码精读

模块入口声明了子模块与内容类型常量，并对外只导出配置类型（`RemoteDatabase`/`RemoteTable` 本身是 `pub(crate)`，用户拿不到）：

[rust/lancedb/src/remote.rs:9-22](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote.rs#L9-L22) —— 注意 `retry` 是私有 `mod`，而 `client`/`db`/`table` 是 `pub(crate)`；`ARROW_STREAM_CONTENT_TYPE` 定义了传输 `RecordBatch` 流的 MIME 类型；只有 `ClientConfig`/`RetryConfig` 等配置类型被 `pub use` 出去。

`RemoteDatabase` 的结构体只持有四样东西：HTTP 客户端、表缓存、URI、命名空间相关的头部与上下文：

[rust/lancedb/src/remote/db.rs:193-203](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L193-L203) —— `client: RestfulLanceDbClient<S>` 是真正干活的角色，`table_cache` 是 moka 缓存（避免反复 describe 同一张表），`S: HttpSend` 是泛型参数（默认 `Sender`，测试里换成 `MockSender`）。

`table_names` 是「翻译 + 发送 + 解析」三段式的典型实现：

[rust/lancedb/src/remote/db.rs:444-482](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L444-L482) —— 带命名空间时走 `/v1/namespace/{id}/table/list`，否则走 `/v1/table/`；分页用 `limit` / `page_token` 查询参数；`send_with_retry` 拿到响应后 `check_response` 校验、`parse_server_version` 读版本，最后 `json::<ListTablesResponse>` 反序列化，并顺手把每张表缓存成 `RemoteTable`。

#### 4.1.4 代码实践

- **实践目标**：跑通一个远程「列表」的 mock 测试，确认调用链可工作，且不依赖真实云端。
- **操作步骤**：
  1. 在仓库根目录执行（需要 Rust 工具链，结果待本地验证）：
     ```bash
     cargo test --features remote -p lancedb remote::db::tests::test_table_names
     ```
  2. 对照阅读 [rust/lancedb/src/remote/db.rs:1013-1026](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L1013-L1026)：测试用 `Connection::new_with_handler` 注入一个闭包，闭包断言收到的是 `GET /v1/table/`，并返回 JSON `{"tables": ["table1", "table2"]}`。
- **需要观察的现象**：测试通过；断言说明了请求方法与路径正是 `table_names` 里 `client.get("/v1/table/")` 产生的。
- **预期结果**：`names == vec!["table1", "table2"]`。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `RemoteDatabase` 把表对象缓存进 `table_cache`？如果不在 `table_names` 里缓存会怎样？
  - **答案**：避免后续 `open_table` 重复发起 `/describe/` 请求；moka 缓存设了 300 秒 TTL、最大 10000 项（见 4.2.3）。不缓存则每次打开表都要一次额外的 HTTP 往返，延迟与配额消耗都会上升。
- **练习 2**：用户代码里能直接拿到 `RemoteDatabase` 类型吗？为什么？
  - **答案**：不能。它是 `pub(crate)`，用户只通过 `Connection`（持有 `Arc<dyn Database>`）间接使用。这强制所有访问走 `Database` trait 契约，保证本地与远程后端可互换。

### 4.2 HTTP 客户端：RestfulLanceDbClient 的构造与请求

#### 4.2.1 概念说明

`RestfulLanceDbClient` 是远程后端的「发动机」。它封装了一个 `reqwest::Client`，并负责三件事：

1. **构造客户端**：根据 `TimeoutConfig` 设置连接/读取超时，根据 `TlsConfig` 配置 mTLS 双向认证，根据 `host_override` 决定请求发往哪个域名。
2. **注入头部**：把 API Key、数据库名、用户 ID 等放进每个请求的默认头部，用于鉴权与路由。
3. **发送请求**：提供 `send`（不重试）和 `send_with_retry`（带重试）两个入口，并给每个请求打上唯一的 `x-request-id`。

#### 4.2.2 核心流程

构造一个客户端（`try_new`）的流程：

1. 从 `TimeoutConfig` 与对应环境变量解析出四档超时（整体 / 连接 / 读取 / 连接池空闲）。
2. 用 `reqwest::Client::builder()` 配置超时，若提供 `TlsConfig` 则加载客户端证书、私钥、CA 与主机名校验开关。
3. 把 `default_headers`、`user_agent` 塞进 builder，`build()` 出 `reqwest::Client`。
4. 解析 `host`：有 `host_override` 用之，否则拼成 `https://{db_name}.{region}.api.lancedb.com`。
5. 把 `RetryConfig` 经 `try_into()` 解析成 `ResolvedRetryConfig`（填入默认值），存进客户端。

发送请求时，`get`/`post` 先拼出 `{host}{uri}` 的完整地址，再按需追加 `delimiter` 查询参数；`send` 则构建请求、生成/提取请求 ID、应用动态头部、交给底层 `sender` 发出。

#### 4.2.3 源码精读

`ClientConfig` 汇总了超时、重试、User-Agent、额外头部、TLS、动态头部提供者等全部可配置项：

[rust/lancedb/src/remote/client.rs:51-74](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L51-L74) —— 注意 `header_provider: Option<Arc<dyn HeaderProvider>>`，这是一个扩展点，允许每次请求动态刷新头部（比如短期令牌）。`HeaderProvider` trait 定义在 [client.rs:44-48](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L44-L48)。

`try_new` 集中体现了「构造 reqwest 客户端」的全部细节：超时、mTLS、host 推导、重试配置解析：

[rust/lancedb/src/remote/client.rs:338-444](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L338-L444) —— 超时优先取 `TimeoutConfig` 字段，其次取环境变量（如 `LANCE_CLIENT_CONNECT_TIMEOUT`，整数秒）；mTLS 用 `Identity::from_pem` 加载证书+私钥，用 `add_root_certificate` 加载 CA；host 默认拼成云端的 `api.lancedb.com` 域名；最后 `client_config.retry_config.clone().try_into()?` 把用户可选配置转成带默认值的 `ResolvedRetryConfig`。

`default_headers` 负责把鉴权与路由信息塞进头部：

[rust/lancedb/src/remote/client.rs:452-535](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L452-L535) —— `x-api-key` 放 API Key；`region == "local"` 时改写 `HOST` 头；有 `host_override`（企业版）时加 `x-lancedb-database`；Azure 存储账号名走 `x-azure-storage-account-name`；用户 ID 经 `resolve_user_id()` 放进 `x-lancedb-user-id`。

`send` 是不带重试的发送路径，展示了「构建 → 请求 ID → 动态头部 → 发送」的标准四步：

[rust/lancedb/src/remote/client.rs:577-597](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L577-L597) —— `build_split()` 把 `RequestBuilder` 拆成 `reqwest::Client` 和 `Request`；`extract_request_id` 若发现已有 `x-request-id` 就复用，否则生成一个新的 UUID（见 [client.rs:714-723](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L714-L723)）；`err_to_http` 把 `reqwest::Error` 转成 `Error::Http`。

`get`/`post` 只负责拼地址与 `delimiter` 查询参数：

[rust/lancedb/src/remote/client.rs:537-555](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L537-L555) —— 默认分隔符是 `$`，只有非默认时才追加 `?delimiter=...`，保证默认路径的请求字节不变（重要：远程测试常按精确 URL 断言）。

> 顺带一提：`RemoteDatabase::try_new` 里建了一个 moka 表缓存，TTL 300 秒、容量 10000，正是 4.1.5 练习 1 提到的缓存——见 [rust/lancedb/src/remote/db.rs:309-312](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L309-L312)。

#### 4.2.4 代码实践

- **实践目标**：验证「构造客户端 → 注入头部」这一步，理解 User-Agent 与请求头的来源。
- **操作步骤**：
  1. 阅读 `default_headers`（[client.rs:452-535](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L452-L535)）与 `ClientConfig::Default`（[client.rs:94-107](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L94-L107)）。
  2. 在一个 mock 测试里（参考 `Connection::new_with_handler_and_config`）传入带 `extra_headers` 的 `ClientConfig`，在 handler 闭包里断言收到了你设的额外头部，以及 `user-agent` 以 `LanceDB-Rust-Client/` 开头。
- **需要观察的现象**：handler 能读到自定义头部与默认 User-Agent。
- **预期结果**：断言通过（具体运行结果待本地验证）。

#### 4.2.5 小练习与答案

- **练习 1**：连接超时 `connect_timeout` 既可以写代码设置，也能用环境变量，优先级如何？
  - **答案**：代码里显式传入的值优先；为 `None` 时才读 `LANCE_CLIENT_CONNECT_TIMEOUT` 环境变量（整数秒）；都没有则用默认 120 秒。逻辑见 [client.rs:322-336](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L322-L336) 与 `try_new` 中的 `unwrap_or_else(|| Duration::from_secs(120))`。
- **练习 2**：`HeaderProvider` 这个扩展点解决了什么问题？
  - **答案**：某些鉴权令牌是短期有效的，每次请求都要重新获取（比如刷新 OAuth token）。`HeaderProvider::get_headers` 是 `async` 的，允许在发送前异步拉取最新头部，从而支持动态凭证。

### 4.3 重试策略：RetryConfig、RetryCounter 与 send_with_retry

#### 4.3.1 概念说明

网络是不可靠的：连接可能被重置、响应可能超时、服务端可能短暂返回 503。一个好客户端必须在「可恢复的临时故障」上自动重试，在「永久故障」上尽快失败。LanceDB 的远程后端用一套精细的重试机制做到这点，核心三件套：

- **`RetryConfig`**：用户配置（全是 `Option`，表示「未设置则用默认」）。
- **`ResolvedRetryConfig`**：填好默认值后的解析结果，真正驱动重试。
- **`RetryCounter`**：每次请求一个计数器，分三类统计失败、判断是否放弃、计算下次退避时间。

一个关键设计：**三类失败分别有独立上限，但共享一个「放弃」判断**——连接失败、读取失败、请求失败任一类触顶，或它们累计触顶，就立即放弃。另一个关键安全取舍：**写操作不在 5xx 上重试**，因为重试一个已经到达服务端的写请求可能导致重复写入。

#### 4.3.2 核心流程

重试循环（`send_with_retry`）的逻辑：

```
准备：克隆一份请求以提取 request_id；新建 RetryCounter
loop:
    克隆请求（流式体则重新生成 body）
    注入 x-request-id、动态头部
    发送请求，得到 (status, response) 或 Err
    match:
        成功状态          → 返回 Ok(response)
        可重试状态码        → check_response 拿到错误 → increment_request_failures
        连接错误(is_connect) → increment_connect_failures
        超时/读体错误        → increment_read_failures
        其他错误            → 直接返回 Error::Http
    （上面三种 increment 内部会判断是否触顶，触顶则返回 Error::Retry）
    计算 next_sleep_time → sleep → 继续循环
```

退避公式（在自增失败计数**之后**计算）：

\[
\text{sleep} = \text{backoff\_factor} \times 2^{\text{request\_failures}} + \text{jitter}
\]

其中 \(\text{jitter} = \mathrm{rand}(0,1) \times \text{backoff\_jitter}\)。默认 `backoff_factor = 0.25`、`backoff_jitter = 0.25`。

#### 4.3.3 源码精读

`RetryConfig` 的每个字段都是 `Option`，并在文档里写明对应的环境变量与默认值：

[rust/lancedb/src/remote/client.rs:176-235](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L176-L235) —— 三套计数 `retries`/`connect_retries`/`read_retries` 默认都是 3；`statuses` 默认是 `[409, 429, 500, 502, 503, 504]`，并明确注释「写操作永不在 5xx 上重试，以免重复写入」。

`ResolvedRetryConfig` 的 `TryFrom` 把 `Option` 填成默认值，是把「用户可选」与「运行时必需」解耦的关键：

[rust/lancedb/src/remote/retry.rs:231-259](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/retry.rs#L231-L259) —— `retries.unwrap_or(3)` 等；状态码默认 `[409, 429, 500, 502, 503, 504]`，逐个 `StatusCode::from_u16` 解析。

`RetryCounter::check_out_of_retries` 是「放弃」的判定：三类计数任一触顶就返回 `Error::Retry`（携带完整的计数与上限信息，便于排错）：

[rust/lancedb/src/remote/retry.rs:28-51](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/retry.rs#L28-L51) —— 判定是 `>=`，对应的 `Error::Retry` 变体定义在 [error.rs:61-79](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/error.rs#L61-L79)，字段包括 `request_failures/max_request_failures`、`connect_failures/max_connect_failures`、`read_failures/max_read_failures`、`source`、`status_code`。

`increment_from_error` 按 `reqwest::Error` 的类别把失败分桶（连接 / 读取 / 请求）：

[rust/lancedb/src/remote/retry.rs:69-89](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/retry.rs#L69-L89) —— `is_connect()` 增连接计数，`is_body()`/`is_decode()` 增读取计数，其余增请求计数；最后统一 `check_out_of_retries`。

退避公式实现在 `next_sleep_time`：

[rust/lancedb/src/remote/retry.rs:103-119](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/retry.rs#L103-L119) —— `backoff_factor * 2.powi(request_failures)` 再加 `rand * backoff_jitter` 的抖动。

重试主循环 `send_with_retry` 把以上零件组装起来：

[rust/lancedb/src/remote/client.rs:603-690](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L603-L690) —— 开头先把 `statuses` 里非 5xx 的部分（`non_5xx_statuses`）单独筛出，这样 5xx 是否重试就由调用方传入的 `retry_5xx` 开关控制（读请求 `true`、写请求 `false`）；循环里 `try_clone()` 保证请求可重放（流式体用 `make_body` 闭包每次重新生成）；成功即返回，可重试错误增计数，连接/读取错误各走各的计数，其余错误直接 `Error::Http`。

下面用默认配置（`retries=3`、`backoff_factor=0.25`、`jitter=0`）推演一次连续失败（请按代码核对）：

| 第 N 次失败（已自增后） | `request_failures` | 退避 `0.25×2^n`（秒） | 是否继续（`>= 3` 即放弃） |
| --- | --- | --- | --- |
| 1 | 1 | 0.5 | 是 |
| 2 | 2 | 1.0 | 是 |
| 3 | 3 | —— | 否，返回 `Error::Retry` |

> 说明：`RetryConfig` 字段的文档注释描述的是「重试前」的序列（0.25/0.5/1.0…），而代码用的是**自增之后**的 `request_failures` 作为指数，因此实际退避是 0.5/1.0/…。追踪实际行为时以源码为准。

#### 4.3.4 代码实践

- **实践目标**：亲手触发一次重试耗尽，观察 `Error::Retry` 携带的信息。
- **操作步骤**：
  1. 运行（结果待本地验证）：
     ```bash
     cargo test --features remote -p lancedb remote::db::tests::test_retries -- --nocapture
     ```
  2. 对照 [rust/lancedb/src/remote/db.rs:971-1010](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L971-L1010)：mock handler 始终返回 `500 internal server error`，`table_names().execute()` 最终返回 `Error::Retry`，且断言 `request_failures == max_request_failures`、错误信息包含 `internal server error`。
- **需要观察的现象**：handler 被多次调用；同一个 `x-request-id` 在每次重试中保持不变（测试用 `OnceLock` 验证）。
- **预期结果**：测试通过，`request_failures` 与 `max_request_failures` 相等。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `table_names`（读）调用 `send_with_retry(req, None, true)`，而带请求体的 `add` 调用 `send_with_retry(req, Some(make_body), false)`？第三个参数 `retry_5xx` 的含义是什么？
  - **答案**：第三个参数控制「是否在 5xx 上重试」。读操作幂等，`true` 允许在 503 等临时服务端错误上重试；写操作非幂等，`false` 避免 5xx 时重发已到达服务端的写请求造成重复写入（见 [client.rs:229-232](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L229-L232) 的注释）。`make_body` 闭包则用于在每次重试时重新生成无法克隆的流式请求体。
- **练习 2**：连接错误和读取错误为什么要有独立的上限，而不是共用一个「总重试次数」？
  - **答案**：这两类故障性质不同——连接错误通常是网络/服务端短暂不可达，多试几次很可能恢复；读取错误（响应体损坏、解码失败）更可能持续。独立上限让客户端对连接问题更宽容。但它们又共享 `check_out_of_retries` 的全局判定，避免任一类失控无限重试。

### 4.4 ServerVersion：服务端能力探测

#### 4.4.1 概念说明

服务端会持续演进，新版本支持新功能（比如结构化全文检索、多向量、多部分写入）。如果客户端写死「调用方式」，就只能在最新服务端上工作；如果对每个新功能都要求用户升级客户端，体验很差。

LanceDB 的解法是**能力探测（capability detection）**：服务端在每个响应里通过 `phalanx-version` 头返回自己的语义化版本号；客户端解析后存进 `ServerVersion`，再在发请求前用 `support_xxx()` 方法判断「当前服务端是否支持某功能」，据此选择不同的请求体格式。这样同一份客户端能同时兼容多个版本的服务端。

#### 4.4.2 核心流程

1. 每次收到响应，`parse_server_version` 从 `phalanx-version` 头读取版本字符串。
2. `ServerVersion::parse` 用 `semver` 解析；缺失则用 `DEFAULT_SERVER_VERSION`（0.1.0）兜底。
3. `RemoteDatabase` 把解析出的版本塞进新建的 `RemoteTable`。
4. `RemoteTable` 在构造请求体时调用 `support_multivector()` / `support_structural_fts()` / `support_multipart_write()` 切换分支。

#### 4.4.3 源码精读

`ServerVersion` 用三个方法把版本号映射成布尔能力位，每引入一个需要客户端配合的新功能就加一个方法：

[rust/lancedb/src/remote/db.rs:51-82](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L51-L82) —— `DEFAULT_SERVER_VERSION = 0.1.0`；`support_multivector` 要求 `>= 0.2.0`、`support_structural_fts` 要求 `>= 0.3.0`、`support_multipart_write` 要求 `>= 0.4.0`。

`parse_server_version` 从响应头取出 `phalanx-version` 并解析，缺失则用默认版本：

[rust/lancedb/src/remote/util.rs:50-69](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/util.rs#L50-L69) —— 头部缺失时 `.unwrap_or_default()` 给出 `ServerVersion::default()`（0.1.0），保证老服务端也能工作。

能力位如何驱动请求体格式，以全文检索为例：

[rust/lancedb/src/remote/table.rs:668-677](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L668-L677) —— 新服务端（`support_structural_fts`）只发 `{"query": ...}`，老服务端则额外带上 `columns` 字段。这就是「同一客户端适配多版本服务端」的具体体现。

多部分写入（并行写）同样受版本门控：

[rust/lancedb/src/remote/table.rs:1822-1828](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L1822-L1828) —— 只有 `support_multipart_write()` 为真时，才允许 `parallelism > 1` 的并行写入，否则退化为单分区写入。

#### 4.4.4 代码实践

- **实践目标**：验证「缺失版本头时走默认版本」这条兜底路径。
- **操作步骤**：
  1. 阅读 `parse_server_version`（[util.rs:50-69](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/util.rs#L50-L69)）与 `ServerVersion::default`（[db.rs:55-59](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L55-L59)）。
  2. 观察现有的 mock 测试（如 `test_create_table`，[db.rs:1129-1151](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L1129-L1151)）返回的响应**不带** `phalanx-version` 头，因此建出来的 `RemoteTable` 持有默认版本 `0.1.0`，所有 `support_xxx()` 都返回 `false`。
- **需要观察的现象**：在不带版本头的 mock 下，任何 `support_*()` 都是 `false`，相关高级分支不会触发。
- **预期结果**：默认版本为 `0.1.0`（待本地用断言验证）。

#### 4.4.5 小练习与答案

- **练习 1**：如果想让 mock 测试覆盖「结构化全文检索」分支，该怎么做？
  - **答案**：在 mock handler 返回的响应里加上 `phalanx-version: 0.3.0`（或更高）头，使 `parse_server_version` 解析出版本 ≥ 0.3.0，从而 `support_structural_fts()` 返回 `true`，走 [table.rs:668](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L668) 的 `if` 分支。
- **练习 2**：为什么要用「能力探测」而不是「客户端发一个版本号给服务端」？
  - **答案**：客户端要决定的是「**我**该用哪种请求格式」，而这个决定依赖**服务端**能理解什么。因此需要服务端把它的能力告诉客户端。`phalanx-version` 头正是服务端自报家门的渠道。

### 4.5 RemoteTable：句柄封装、读新鲜度与可重试写入

#### 4.5.1 概念说明

`RemoteTable` 是远程后端对 `BaseTable` 契约（承接 u2-l2）的实现。相比 `NativeTable` 直接持有 `Dataset`，`RemoteTable` 持有的是 HTTP 客户端和一组**缓存在客户端的状态**：当前版本、schema（带后台刷新的缓存）、读新鲜度状态、所属分支。这些状态让远程表能在「无状态 HTTP」之上模拟出「有状态句柄」的体验，尤其是**读自己的写（read-your-writes）**。

本模块把三个看似分散的主题串起来：句柄状态封装、读新鲜度头部注入、以及流式写入如何支持重试。

#### 4.5.2 核心流程

读新鲜度头部注入（承接 u5-l3）的流程：

1. 每次**写**操作返回一个版本号，`RemoteTable` 把它记进 `FreshnessState.min_version`。
2. 每次**读**操作前，`snapshot_freshness_headers` 计算两个头部：
   - `x-lancedb-min-version`：取自 `min_version`，保证读到至少是自己刚写的版本。
   - `x-lancedb-min-timestamp`：取 `max(checkout_baseline, now - read_consistency_interval)`，让服务端绕过缓存、返回足够新的快照。
3. `post_read` 把这两个头部盖到 POST 请求上。

可重试写入的流程：

1. 普通读用 `send(req, true)`，内部走 `send_with_retry`。
2. 带流式请求体的写用 `send_streaming`：若不重试（或重试次数为 0），直接把 reader 转成 body 发出；若要重试，则**先把整批数据缓冲进内存**，再用闭包在每次重试时重新生成 body（因为流不能克隆）。

#### 4.5.3 源码精读

`RemoteTable` 的字段体现了「在无状态 HTTP 上模拟有状态句柄」的设计：

[rust/lancedb/src/remote/table.rs:247-262](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L247-L262) —— `version`/`location` 用 `RwLock` 缓存，`schema_cache` 是带 TTL 的后台刷新缓存（30 秒 TTL，5 秒刷新窗口，见 [table.rs:75-76](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L75-L76)），`freshness: Mutex<FreshnessState>` 驱动读新鲜度头部，`branch` 让句柄可绑定到某个分支。

`FreshnessState` 与头部常量是读新鲜度的核心：

[rust/lancedb/src/remote/table.rs:70-98](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L70-L98) —— `MIN_VERSION_HEADER = x-lancedb-min-version`、`MIN_TIMESTAMP_HEADER = x-lancedb-min-timestamp`；`min_version` 实现「读自己的写」，`checkout_baseline` 让 `checkout_latest()` 即使在没有 consistency interval 时也能强制服务端跳过旧缓存。

`compute_min_timestamp` 取「区间推导值」与「checkout 基线」的较大者：

[rust/lancedb/src/remote/table.rs:120-135](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L120-L135) —— `interval` 为 `None` 时返回 `None`（不强求新鲜度）；`Some(0)` 取 `now`（强一致）；`Some(d)` 取 `now - d`（最终一致）；再与 `checkout_baseline` 取 max。

`snapshot_freshness_headers` + `post_read` 把上述计算盖到每个读请求上：

[rust/lancedb/src/remote/table.rs:878-895](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L878-L895) —— 在调用时快照一次头部，保证同一次请求的多次重试用同一组新鲜度参数；`FreshnessHeaders::apply`（[table.rs:107-118](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L107-L118)）把时间戳格式化成 RFC3339。

`send` / `send_streaming` 展示了「读用简单重试、写用缓冲重试」的分流：

[rust/lancedb/src/remote/table.rs:484-519](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L484-L519) —— `send` 按 `with_retry` 开关选 `send_with_retry` 或 `send`；`send_streaming` 在需要重试时先 `buffer_reader` 把所有 batch 读进内存，再用 `make_body` 闭包在每次重试时 `clone` 一份 batch 重新编码——这是「流不可克隆」与「重试需要重放」之间的折中（代价是内存占用）。

#### 4.5.4 代码实践

- **实践目标**：跟踪一次远程建表的完整调用链，从用户 API 一路到 HTTP 发送。
- **操作步骤**：
  1. 运行（结果待本地验证）：
     ```bash
     cargo test --features remote -p lancedb remote::db::tests::test_create_table -- --nocapture
     ```
  2. 沿着调用链逐层定位源码：
     - 入口：`conn.create_table("table1", data).execute()` → `RemoteDatabase::create_table`，[db.rs:522-597](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L522-L597)。
     - 翻译：用 `stream_as_body`（[util.rs:45-48](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/util.rs#L45-L48)）把数据流转成 Arrow IPC 流式 body，`POST /v1/table/{id}/create/`，`mode` 查询参数，`content-type: application/vnd.apache.arrow.stream`。
     - 发送：建表用 `client.send`（非重试路径，见 [db.rs:537](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L537)），最终落到 `Sender::send`（[client.rs:279-287](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L279-L287)）。
- **需要观察的现象**：mock handler 断言收到 `POST`、路径 `/v1/table/table1/create/`、`content-type` 为 Arrow 流；返回 200 后 `table.name() == "table1"`。
- **预期结果**：测试通过（见 [db.rs:1129-1151](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L1129-L1151)）。

#### 4.5.5 小练习与答案

- **练习 1**：`send_streaming` 为什么在开启重试时要把整个 reader 缓冲进内存？代价是什么？
  - **答案**：`reqwest` 的流式 body 不可克隆，而 `send_with_retry` 每次重试都要 `try_clone()` 请求并重新生成 body。解决办法是先把所有 batch 读进 `Vec<RecordBatch>`，再用闭包每次 `clone` 一份重新编码（[table.rs:507-512](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L507-L512)）。代价是内存占用等于这批数据的大小；好处是写入也能享受重试容错。
- **练习 2**：建表时如果服务端返回 `400` 且响应体含 `already exists`，客户端会怎么处理？
  - **答案**：见 [db.rs:539-578](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L539-L578)：按 `mode` 分流——`Create` 返回 `Error::TableAlreadyExists`；`ExistOk` 改走 `open_table`；`Overwrite` 返回 `Error::Http`（理论上不该发生）。这是「错误码 → 语义化错误」的典型处理。

## 5. 综合实践

把本讲的知识串起来，完成一个**端到端调用链追踪 + 行为验证**的小任务（全程用 mock，不连真实云端）。

**任务**：用 mock HTTP 端点模拟一次「建表 → 列表 → 读取」的远程交互，并解释每一步在源码里的落点。

**建议步骤**：

1. **建表**：参考 `test_create_table`（[db.rs:1129-1151](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L1129-L1151)），用 `Connection::new_with_handler` 注入一个 handler：对 `POST /v1/table/my_t/create/` 返回 `200`，并在响应里加上 `phalanx-version: 0.4.0` 头。调用 `conn.create_table("my_t", data).execute()`。

2. **列表**：在同一个 handler 里对 `GET /v1/table/` 返回 `{"tables": ["my_t"]}`，调用 `conn.table_names().execute()`，确认返回 `["my_t"]`。

3. **能力探测**：因为 handler 返回了 `0.4.0`，新建的 `RemoteTable` 的 `support_multipart_write()` 应为 `true`。结合 [db.rs:71-82](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L71-L82) 解释：若随后对该表 `add` 大批数据，会走 [table.rs:1828](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/table.rs#L1828) 的多分区并行写入分支。

4. **重试行为**：把 handler 改成对 `GET /v1/table/` 始终返回 `503`，重跑 `table_names()`，预期得到 `Error::Retry`（读请求允许在 5xx 上重试，触顶后放弃），对照 4.3.4。

5. **画调用链**：在笔记里画出 `conn.table_names()` → `RemoteDatabase::table_names`（[db.rs:444](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/db.rs#L444)）→ `client.get`（[client.rs:537](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L537)）→ `send_with_retry`（[client.rs:603](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L603)）→ `Sender::send`（[client.rs:280](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L280)）→ `check_response`（[client.rs:732](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L732)）→ `parse_server_version`（[util.rs:50](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/util.rs#L50)）的完整链路。

> 本任务属于「源码阅读 + 本地验证型」实践：第 1–4 步需要你在本地测试模块（或独立 example）里编写 mock 测试并运行，具体结果待本地验证。注意：编写测试属于学习性探索，请勿提交到项目源码。

## 6. 本讲小结

- 远程后端是一条分层调用链：`Connection/Table` → `RemoteDatabase/RemoteTable`（翻译成 HTTP）→ `RestfulLanceDbClient`（构造/发送/重试）→ `reqwest::Client`（网络 IO）。
- `RestfulLanceDbClient::try_new` 集中处理超时、mTLS、host 推导与重试配置解析；`default_headers` 注入 `x-api-key`、`x-lancedb-database`、`x-lancedb-user-id` 等鉴权/路由头。
- 重试由三件套驱动：`RetryConfig`（用户可选）→ `ResolvedRetryConfig`（带默认值）→ `RetryCounter`（连接/读取/请求三类独立计数、共享放弃判定）。退避公式为 `backoff_factor × 2^request_failures + jitter`。
- 关键安全取舍：写操作不在 5xx 上重试（`retry_5xx=false`），避免重复写入；读操作允许在 5xx 上重试。
- `ServerVersion` 通过 `phalanx-version` 响应头做能力探测，让同一份客户端用 `support_multivector/support_structural_fts/support_multipart_write` 适配多个版本的服务端。
- `RemoteTable` 在无状态 HTTP 之上模拟有状态句柄：用 `FreshnessState` 注入 `x-lancedb-min-version`/`x-lancedb-min-timestamp` 实现「读自己的写」；`send_streaming` 通过缓冲整批数据让流式写入也能重试。

## 7. 下一步学习建议

本讲完结「存储后端与架构」单元，本地与远程两套后端的统一抽象至此讲完。接下来可以：

- **进入 u7 多语言绑定**：看 Python（PyO3）/ Node（napi-rs）/ Java 绑定如何复用本讲的远程客户端语义，尤其是 Python 同步 `Table` 与异步 `AsyncTable` 如何把远程 HTTP 调用包装成两套编程模型。
- **回看 u5-l3**：本讲 4.5 落到了 `x-lancedb-min-timestamp` 的具体实现，可与 u5-l3 的「读新鲜度」概念相互印证，形成闭环。
- **动手扩展**：尝试实现一个自定义 `HeaderProvider`（[client.rs:44-48](https://github.com/lancedb/lancedb/blob/448d5ec20ff06900635452da411668f55ae293e2/rust/lancedb/src/remote/client.rs#L44-L48)），在每次请求前异步刷新一个短期令牌，理解远程后端的扩展点设计。
- **阅读测试**：`rust/lancedb/src/remote/db.rs` 与 `table.rs` 末尾有大量 mock 测试，是理解「每个 HTTP 端点契约」的最佳资料，建议按端点逐个阅读。
