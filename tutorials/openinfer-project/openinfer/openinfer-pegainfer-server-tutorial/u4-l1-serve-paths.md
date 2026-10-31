# serve 函数族：并发加载与装配骨架

## 1. 本讲目标

学完本讲，你应该能够：

1. 列出 `vllm` 模块对外暴露的 **serve 函数族**——`serve` / `serve_with_engine_count` / `serve_with_router_extension` / `serve_prefill_only_with_engine_count` / `serve_model_with_lora_routes`——并说出 `main.rs` 选中每一条的条件（LoRA → prefill-only → 默认）。
2. 解释它们共同汇聚到的私有装配函数 `serve_model_on_host_with_router_extension` 的骨架：**预绑定 unix socket fd**、把引擎包装成任务与 HTTP 前端**并发**跑、每分区一个 bridge 的 `JoinSet`、以及引擎失败时 `server_shutdown.cancel()` 的接线。
3. 说出**引擎计数对账**（声明数 vs 实际分区数）与 **30 分钟 `ready_timeout`** 各自的用意：前者是「宁可死不带病服务」的安全门，后者只兜底「真正挂死的加载」。
4. 画出非 LoRA 分支的两条并行时间线（`spawn_blocking` 引擎加载 vs vllm-server 的 tokenizer/模板加载），标出汇合点，并写全引擎加载失败时的**取消链**。

本讲是单元四的第一篇：u2-l5 讲清了「装配前必须知道什么」（`ServePlan`），u3-l1 给出了引擎契约的两代地图；本讲站在二者之上，看**服务如何真正跑起来**。

## 2. 前置知识

本讲需要以下概念（前几讲已建立的只做一句话回顾，新术语展开讲）：

- **`ServePlan`**（u2-l5）：模型线在引擎加载前交出的服务形态——`scheduler_partition_count`、`prefill_only`、`lora_modules`、`auto_tool_call_parser`。本讲的分支选择全部由它驱动。
- **`LaunchedEngine` 两代契约**（u3-l1）：迁移期二选一枚举——`Handle(EngineHandle)`（legacy 每 token 契约）与 `Stepped(Engine)`（step 契约交接包，含 `schedulers`、`EngineInfo`、`lora`）。装配骨架要对两种变体各做一次对账。
- **`spawn_blocking`**（u1-l3）：把分钟级的阻塞引擎加载丢到 tokio 的阻塞线程池，async 运行时继续跑别的任务。
- **`impl Future<Output = Result<LaunchedEngine>>` 参数**：serve 函数族接受的是「将来才出结果的引擎」，不是加载完的引擎。这是「前端先行、引擎后到」在类型上的表达（u2-l5 已见过）。
- **Unix domain socket 与文件描述符（fd）**：Unix socket 是只在本机内通信的套接字，用文件系统路径当地址。`UnixListener::bind(path)` 会创建监听套接字；`into_raw_fd()` 把它交出为裸整数（fd），放弃 Rust 侧的所有权——此后由**持有 fd 的一方**负责关闭。本讲里 frontend 预绑定两个 socket、把 fd 塞进配置交给 vllm-server。
- **`CancellationToken` 与 `child_token()`**：`tokio_util` 的协作式取消令牌。`child_token()` 从父 token 派生一个子 token：**取消父会级联取消所有子；取消子不影响父与兄弟**。本讲的 shutdown 级联全靠这棵小树。
- **`JoinSet`**：tokio 的「一组任务 + 逐个收尸」容器——spawn 进去的任务可以按完成顺序 `join_next()`，谁先失败就先看到谁。
- **zmq 与 `TransportMode::Bootstrapped`**：vLLM 上游的 EngineCore 协议跑在 zmq 消息上；`Bootstrapped` 表示「监听套接字已经由别人绑好，我直接领养 fd」，而不是自己再去 bind。桥的细节在 u4-l3 展开，本讲只把它当作「引擎注册」这条线的载体。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs) | **消费方**：按 `ServePlan` 走三分支，把引擎 Future 与 shutdown token 交给 serve 函数族（L215-L279） |
| [pegainfer-frontend/src/vllm/mod.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs) | **本讲主战场**：serve 函数族（L74-L229）、私有装配函数 `serve_model_on_host_with_router_extension`（L257-L520）、Ctrl-C 帮手（L543-L561） |
| [pegainfer-frontend/src/vllm/bridge.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs) | 预绑定 socket 的**地址来源**：`local_ipc_namespace`（建 `/tmp/pgi-*` 目录）与 `ipc_endpoint`（拼 `ipc://` URI），L1032-L1044 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs) | 对账的**对象**：`Engine` / `LiveScheduler` / `LaunchedEngine` 定义（L141-L168） |
| [pegainfer-frontend/examples/echo-server.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs) | 无 GPU 可跑的调用者：用 `std::future::ready` 把已就绪引擎包成 Future 喂给 `serve_with_engine_count`（L222-L231），本讲实践用它观察 IPC 目录 |

## 4. 核心概念与源码讲解

### 4.1 serve 函数族：五个入口，一副骨架

#### 4.1.1 概念说明

u2-l5 结尾留下的问题是：`ServePlan` 的三个分支字段（`lora_modules`、`prefill_only`、`scheduler_partition_count`）到底喂给谁？答案是 `pegainfer_frontend::vllm` 里的一族公开函数。它们解决的问题是：**把「一个还没加载完的引擎」变成「一个 OpenAI 兼容的 HTTP 端点」**，并且在这件事上提供三种可定制的服务形态（普通、prefill-only、LoRA）。

这族函数的分工：

| 入口函数 | engine_count | 路由定制 | 谁在用 |
|----------|--------------|----------|--------|
| `serve` | 硬编码 1 | 无 | `pegainfer-sim` 的 main 与测试 |
| `serve_with_engine_count` | 参数 | 无 | **pegainfer-server 默认分支**、echo-server、sim 测试 |
| `serve_with_router_extension` | 参数 | 调用者给 `FnOnce(Router) -> Router` | crate 内被 prefill-only 复用 |
| `serve_prefill_only_with_engine_count` | 参数 | 挂 prefill-only 校验门 | **pegainfer-server prefill 分支** |
| `serve_model_with_lora_routes` | 硬编码 1 | 挂 LoRA 路由 + OpenAI 路由 | **pegainfer-server LoRA 分支** |

关键观察：前四个签名几乎一致——第一个参数都是 `engine: impl Future<Output = Result<LaunchedEngine>> + Send + 'static`。**传 Future 而不是传引擎**，就是并发加载的入口：HTTP 前端装配（秒级）不必等引擎（分钟级）。唯一的例外是 `serve_model_with_lora_routes`，它拿的是已经 resolve 的 `Engine`——原因见 4.1.3。

#### 4.1.2 核心流程

`main.rs` 非 LoRA / LoRA 两大分支的选择逻辑：

```
plan = line.serve_plan(&ctx)                     // u2-l5：加载前事实

if plan.lora_modules 是 Some:                    // LoRA 分支（串行）
    先 await 引擎加载完毕，且必须是 Stepped
    → serve_model_with_lora_routes(engine, …, lora_modules, port=…, count=1)
else:                                            // 非 LoRA 分支（并发）
    engine = 一个包装 Future：await 引擎 → 打日志 → 装 Ctrl-C 处理器
    if plan.prefill_only:
        → serve_prefill_only_with_engine_count(engine, …, count=partition 数)
    else:
        → serve_with_engine_count(engine, …, count=partition 数)

无论哪条分支返回，main 统一 .context("vLLM frontend server failed") 后上抛
```

函数族内部的收拢关系（谁调谁）：

```
serve ─────────────────┐
                        ├→ serve_with_engine_count ─┐
serve_prefill_only_… ─→ serve_with_router_extension ─┴→ serve_model_on_host
                                                          └→ serve_model_on_host_with_router_extension   ← 唯一的骨架
serve_model_with_lora_routes ─────────────────────────────────┘（自己拼好路由后直入骨架）
```

#### 4.1.3 源码精读

先看 `main.rs` 的三分支。LoRA 分支要求引擎**先**加载完：

[src/main.rs:L215-L237](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L237) — LoRA 分支：先 `engine_load.await` 拿到已就绪引擎，用 `let … else` 强制它是 `Stepped` 变体（`bail!("LoRA serving requires a step-driven engine")`），再调 `serve_model_with_lora_routes`；注意 `engine_count` 在这条路径上不出现——骨架内部固定为 1，shutdown token 由 `shutdown_token_from_ctrl_c()` 在此刻（引擎已就绪后）安装。

[src/main.rs:L238-L278](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L238-L278) — 非 LoRA 分支：这是本讲的主角。`shutdown` token 先建（L239）；`engine` 是一个**包装 Future**（L240-L253）——它先 `engine_load.await`，成功后打 `Engine loaded: elapsed_ms=…` 日志，**然后才**安装 Ctrl-C 处理器（注释 L247-L249 解释了为什么：阻塞加载不可取消，加载期间 SIGINT 保持默认的 kill 行为，引擎就绪后才切换成优雅关停）。随后 `plan.prefill_only` 二选一，把 `engine`（Future 本体，按值移动进调用）与 `plan.scheduler_partition_count` 交给 serve 函数。

[src/main.rs:L280-L286](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L280-L286) — 统一收口：整段 serve 调用被 `.context("vLLM frontend server failed")` 包住；`tracing::flush()` 在 `serve_result?` **之前**执行（u1-l3 讲过：失败的服务器恰恰最需要把最后一批请求 span 冲出去）。

再看 frontend 侧。最简入口 `serve` 的文档注释就是全族的宣言：

[pegainfer-frontend/src/vllm/mod.rs:L64-L94](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L64-L94) — 文档注释写明三件事：HTTP 前端（tokenizer、chat 模板）**立即**启动；引擎桥在 `engine` resolve 后**附着**；**HTTP 只在桥注册之后才 bind**——所以「端口可达」仍然等于「引擎就绪」。函数体只有一件事：以 `engine_count = 1` 转发 `serve_with_engine_count`。pegainfer-server 二进制不用它，用它的是 `pegainfer-sim`（CPU 参考实现）。

[pegainfer-frontend/src/vllm/mod.rs:L100-L122](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L100-L122) — `serve_with_engine_count`：pegainfer-server 默认分支的入口。它把 `model_path` 变字符串、host 固定 `"0.0.0.0"`、用 `resolve_max_model_len` 把 `None` 折算成 config 里的 `max_position_embeddings`（找不到则警告并兜底 4096，见 L529-L541），然后下沉到私有的 `serve_model_on_host`。

[pegainfer-frontend/src/vllm/mod.rs:L124-L156](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L124-L156) — `serve_with_router_extension`：通用的「在调用者的路由上服务」钩子。文档注释点明契约：引擎**拒绝**它不服务的东西，在请求到达引擎之前就以 400 挡下（prefill-only 契约就是这样实现的）。

[pegainfer-frontend/src/vllm/mod.rs:L158-L182](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L158-L182) — `serve_prefill_only_with_engine_count`：服务一个**要求 `max_tokens=1`** 的端点。整个「prefill-only 门」对骨架的全部定制，就是传入 `request_contract::prefill_only_routes` 这个路由改写函数（门的内部在 u4-l5 精读）。

[pegainfer-frontend/src/vllm/mod.rs:L184-L229](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L184-L229) — `serve_model_with_lora_routes`：签名与众不同——拿 `crate::engine::Engine`（不是 Future）。原因有二：其一，`engine.lora` 这个 `Option<LoraClient>` **就是能力本身**，没有铸出 LoRA 控制通道的引擎直接在这里报错（L198-L201）；其二，路由构建期就要用控制平面装载启动适配器（`load_startup_lora_modules`，L202）。但它没有另起炉灶：L208 用 `std::future::ready(Ok(LaunchedEngine::Stepped(engine)))` 把已就绪引擎包回一个立即 resolve 的 Future，复用同一副骨架，`engine_count` 固定 1；路由定制在 L217-L226 完成（LoRA 路由 + OpenAI 路由的合并，u4-l5 展开）。

[pegainfer-frontend/examples/echo-server.rs:L222-L231](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L222-L231) — 一个现成的调用范本：echo 线没有东西可加载，于是 `std::future::ready(Ok(engine))` 立即 resolve，`plan.scheduler_partition_count` 作 engine_count，`vllm::shutdown_token_from_ctrl_c()` 作 shutdown。**无 GPU、无权重**，本讲实践会用到它。

#### 4.1.4 代码实践：亲手数一遍调用点

1. **实践目标**：确认「谁在用哪个入口」，把 4.1.1 的表格从论断变成你亲眼验证过的事实。
2. **操作步骤**：
   - 在仓库根目录执行 `rg -n "vllm::(serve|serve_with_engine_count|serve_prefill_only_with_engine_count|serve_model_with_lora_routes|serve_with_router_extension)" --type rust`（或等价的 grep）。
   - 再看 `pegainfer-server/src/main.rs` L215-L279，把每个命中点归到三分支之一。
3. **需要观察的现象**：命中点分布在 `pegainfer-server/src/main.rs`（三个：L227 / L255 / L267）、`pegainfer-frontend/src/vllm/mod.rs`（族内部互调）以及 `pegainfer-sim/`（`src/main.rs` 与两个测试文件）。
4. **预期结果**：pegainfer-server 二进制只调用 `serve_model_with_lora_routes`、`serve_prefill_only_with_engine_count`、`serve_with_engine_count` 三个；`serve` 与 `serve_with_router_extension` 的 crate 外用户在 pegainfer-sim。若你的搜索结果与此不符，说明 HEAD 已变，讲义需要按新代码复核。

#### 4.1.5 小练习与答案

**练习 1**：serve 函数族的第一个参数为什么是 `impl Future<Output = Result<LaunchedEngine>>`，而不是直接传 `LaunchedEngine`？

**答案**：因为前端装配（tokenizer、chat 模板，约 1 秒）必须与引擎加载（权重上 GPU，分钟级）**并行**。传 Future 让骨架把「等待引擎」变成内部的一个任务（engine_task），调用方（main.rs）不必先阻塞等引擎再启服务。`serve_model_with_lora_routes` 是唯一例外——它的路由构建期就需要控制平面，所以拿已就绪的 `Engine`，再用 `std::future::ready` 包回 Future 复用骨架。

**练习 2**：`main.rs` 里 `plan` 的哪三个字段决定走哪条 serve？各自的判断顺序是什么？

**答案**：先看 `plan.lora_modules`（`Some` → LoRA 分支，串行等引擎、要求 `Stepped`）；否则看 `plan.prefill_only`（`true` → prefill-only 门）；否则走默认 `serve_with_engine_count`。非 LoRA 两条路径的 `engine_count` 都来自 `plan.scheduler_partition_count`。

**练习 3**：`serve` 这个最简入口在 pegainfer-server 里找不到调用，它为谁而存在？

**答案**：为 crate 外的普通（单引擎）使用者——`pegainfer-sim` 的 `main.rs` 与 `tool_call_roundtrip` 测试调用它；它把 `engine_count` 硬编码为 1 转发 `serve_with_engine_count`，让单引擎场景不必多传一个参数。

### 4.2 serve_model_on_host_with_router_extension 装配：预绑定 socket 与并发汇合

#### 4.2.1 概念说明

五个入口最终都沉到同一个私有函数 `serve_model_on_host_with_router_extension`（`serve_model_on_host` 只是给它传一个恒等闭包）。它是整个服务面的**装配车间**，解决三个问题：

1. **谁先拿到通信地址？** HTTP 前端与引擎桥要通过 zmq 上的两条 unix socket 通信。如果让 vllm-server 自己去 bind，桥就得等它；反过来也一样。解法是**装配函数自己先把两个 socket 绑好**，把裸 fd 交给 vllm-server「领养」——这扮演了上游 Python 部署里 supervisor 进程的角色。socket 从第一微秒起就存在，任何一方随时可连。
2. **两条时间线怎么汇合？** 引擎任务（等 Future resolve、起桥）与 vllm-server（加载 tokenizer/模板、等引擎注册）并发跑；汇合点是**桥连上预绑定 socket 完成「注册」**。vllm-server 在看到注册之前**不 bind TCP**——于是「端口可达 = 引擎就绪」这条语义就成立了。
3. **声明与事实不符怎么办？** 前端已经按 `ServePlan` 声明的分区数注册了 engine identity（请求寻址参数）；引擎 resolve 后如果实际分区数不同，**取消服务、拒绝带病上线**。

#### 4.2.2 核心流程

```
t0  main() 校验完毕（非 LoRA 分支）
    ├─ A 线（引擎）：spawn_blocking(line.launch)             main.rs L204
    │      权重读盘 → 搬上 GPU → 建调度器线程（分钟级）
    └─ B 线（前端）：serve_with_engine_count(...)             main.rs L267
           ├─ engine_count 合法性检查（>0 且装得进 u32）        mod.rs L272-L274
           ├─ 建 IPC 目录 + 预绑定 input.sock / output.sock    mod.rs L275-L282
           ├─ 派生 server_shutdown / bridge_shutdown 两个子 token mod.rs L290-L291
           ├─ spawn engine_task（内部等 A 线、对账、起桥）      mod.rs L292-L414
           ├─ load_model_backends（模板/分词，失败仅告警降级）   mod.rs L419-L433
           └─ vllm_server::serve_with_router_extension          mod.rs L493
                 加载 tokenizer + chat 模板（约 1s）
                 在预绑定 socket 上等引擎注册（≤ ready_timeout = 30 min）

t1  A 线 resolve → engine_task 醒来                             mod.rs L298
    ├─ 对账：声明的 engine_count vs 实际分区数                  mod.rs L311 / L337
    ├─ max_model_len 按 servable_limit 收紧                     mod.rs L319 / L346
    └─ 每分区 spawn 一个 bridge → 连预绑定 socket ＝「注册」      mod.rs L321-L333 / L348-L361

t2  B 线看到注册 → bind TCP 0.0.0.0:port      ← 端口此刻才可达（= 引擎就绪）
t3  请求流动：HTTP → zmq → bridge → 调度器（u4-l3 精读这段）
```

汇合点不在 `main.rs` 的任何 `.await` 上——main 的非 LoRA 分支把 Future 交出去之后，自己只在整段 serve 调用上 await 一次；真正的汇合发生在 engine_task 里的 `engine.await`（L298）与 vllm-server 的注册等待之间，由**预绑定 socket** 这个中立体撮合。

#### 4.2.3 源码精读

[pegainfer-frontend/src/vllm/mod.rs:L272-L282](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L272-L282) — 装配的第一步：`ensure!` 声明数必须为正且装得进 u32；然后 `local_ipc_namespace()` 建一次性目录、`ipc_endpoint()` 拼两个 `ipc://…` 地址，`UnixListener::bind` 各绑一个 socket 并立刻 `into_raw_fd()` 交出 fd。注释 L278-L280 说破了用意：vllm-server **领养已绑定的监听器**（上游 Python 部署里这是 supervisor 的角色）；在这里绑定意味着**桥可以在任意时刻连接**，而 server 拥有并负责关闭这些描述符。

[pegainfer-frontend/src/vllm/bridge.rs:L1032-L1044](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge.rs#L1032-L1044) — socket 地址从哪来：默认在 `/tmp`（可用环境变量 `PEGAINFER_IPC_DIR` 改道）下建 `pgi-<pid>-<uuid前8位>` 目录，socket 名固定 `input.sock` / `output.sock`。目录在装配收尾时整体删除（见 4.3.3 的 L518）。

[pegainfer-frontend/src/vllm/mod.rs:L290-L297](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L290-L297) — shutdown 树在这里成形：`shutdown.child_token()` 派生出 `server_shutdown`（管 HTTP server）与 `bridge_shutdown`（管桥）两个**子** token。父 token 由 main.rs 持有（Ctrl-C 后取消）；子 token 允许 engine_task **单独**杀 server 或单独杀桥而不惊动对方。注释 L284-L289 同时给出并发设计的全貌：vllm-server 花约 1 秒加载 tokenizer 和模板才去等引擎注册，双方互不等待；引擎失败时这个任务会取消 server，让错误浮出来而不是挂在注册等待上。

[pegainfer-frontend/src/vllm/mod.rs:L305-L308](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L305-L308) — 引擎 resolve 成功后的两件容器：`JoinSet` 装桥任务（每分区一个，产出 `(engine_index, run 结果)`），`scheduler_joins` 收 step 引擎的驱动线程句柄（桥都死了以后再收割，注释点明 stepped 引擎「拥有自己的调度器线程」）。

[pegainfer-frontend/src/vllm/mod.rs:L310-L335](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L310-L335) — **legacy `Handle` 变体的对账与起桥**：`handle.scheduler_partition_count()` 取实际分区数，与声明的 `engine_count` 不符就 `server_shutdown.cancel()` 后 `bail!`（错误原文点名两个数字）。对上了就按 `servable_len` 收紧 `max_model_len`，然后每个分区克隆一份 handle、拿 `metrics_watch_for(engine_index)` 的 watch，铸一个 `LocalEngineBridge` 丢进 JoinSet；最后 `drop(handle)` 交出引擎任务自己那份所有权。

[pegainfer-frontend/src/vllm/mod.rs:L336-L362](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L336-L362) — **step `Stepped` 变体的对账与起桥**：`engine.schedulers.len()` 对账（同样不符即取消 + bail）；`EngineInfo` 里的 `servable_len` 同样用于收紧长度；逐个调度器把 `scheduler.join` 压进 `scheduler_joins`、用 `scheduler.handle` 铸 `SteppedEngineBridge` 进 JoinSet。对 `Engine`/`LiveScheduler` 的结构不熟可回看 [wiring.rs:L141-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L141-L168)：`Engine` 是协议栈溶解的交接包，`LiveScheduler` 是「handle + 驱动线程句柄」。

[pegainfer-frontend/src/vllm/mod.rs:L416-L435](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L416-L435) — 与引擎并发的另一段装配：`load_model_backends` 给两个请求整形层（u4-l4 的 history 边界测量与 effort 别名）加载模板与分词器；**失败只告警降级**（chat 请求整形关闭），注释说明原因——整形层读的是同一份 checkpoint 模板，server 随后自己还会再加载一次，模型真坏了由 server 报自己的错。

[pegainfer-frontend/src/vllm/mod.rs:L437-L491](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L437-L491) — `vllm_server::Config` 的装配。本讲只看骨架相关的两处（其余字段 u4-l2 逐项精读）：L438-L449 的 `TransportMode::Bootstrapped` 携带**两个预绑定的 fd**、`engine_start_index: 0`、`engine_count`、`data_parallel_size: engine_count`，以及 `ready_timeout: Duration::from_mins(30)`——注释 L444-L447 说清了它的用意：**进程内桥在引擎 Future resolve 后才注册，所以这个超时兜住的是整场引擎加载**（多 GPU MoE 模型要几分钟、冷启动更久）；加载**失败**已经由 engine task 取消 server 上报了，超时只抓「真挂死」的加载。L456 的 `listener_mode: HttpListenerMode::BindTcp { host, port }` 则是 TCP 侧的监听模式——注意它只是声明，真正 bind 发生在 vllm-server 看到引擎注册之后。

[pegainfer-frontend/src/vllm/mod.rs:L493-L501](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L493-L501) — 启动 HTTP server：把 config、`server_shutdown` 和路由扩展链交给上游的 `vllm_server::serve_with_router_extension`。扩展链的包裹顺序是 `normalize_chat_requests(mark_history(extend_router(router)))`——effort 规范化在最外层、history 打标在内、调用者的定制在最里（u4-l2/u4-l4 展开）。这一行 `.await` 就是 B 线在装配函数里的驻留点：从启动一直驻留到 server 结束（被取消或自然退出）。

#### 4.2.4 代码实践：亲眼看到预绑定的 socket

1. **实践目标**：把「预绑定 unix socket + IPC 目录生命周期」从代码论断变成可观察的事实。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo run -p pegainfer-frontend --example echo-server`（无 GPU、无权重，模型是内置 fixture）。
   - 另开一个终端：`ls -la /tmp | grep pgi-`，应看到形如 `pgi-<pid>-<8位十六进制>` 的目录，内含 `input.sock` 与 `output.sock` 两个套接字文件。
   - 可选：`ss -x | grep pgi-`（或 `lsof -U | grep pgi-`）观察两个已建立监听的 unix socket。
   - 可选：`PEGAINFER_IPC_DIR=/tmp/myipc cargo run -p pegainfer-frontend --example echo-server`，确认目录改道到 `/tmp/myipc` 下。
   - 对 echo-server 按 Ctrl-C，再 `ls /tmp | grep pgi-` 确认目录已消失。
3. **需要观察的现象**：服务运行期间 IPC 目录与两个 socket 存在；进程退出后目录被删除。
4. **预期结果**：目录创建对应 `local_ipc_namespace()`（bridge.rs L1032-L1040），退出删除对应装配收尾的 `remove_dir_all(namespace)`（mod.rs L518）；`PEGAINFER_IPC_DIR` 改道对应 bridge.rs L1033-L1034 的 `env::var_os("PEGAINFER_IPC_DIR")` 分支。**待本地验证**（本讲义写作环境未运行该示例；若现象不符请按 HEAD 复核）。

#### 4.2.5 小练习与答案

**练习 1**：为什么要由装配函数预先 `bind` 两个 unix socket 再把 fd 交给 vllm-server，而不是让某一方自己绑？

**答案**：注释 L278-L280 点明这是上游 Python 部署里 **supervisor** 的角色。预先绑定后，socket 在任何一方启动前就存在——桥可以**在任意时刻**连接（不会因对端未绑而失败）；fd 的所有权也清晰：server 领养描述符并负责关闭。这把「谁先谁后」的时序问题从协议里拿掉了。

**练习 2**：`ready_timeout` 设 30 分钟，它捕获和不捕获的分别是什么场景？

**答案**：它只捕获「**真正挂死**的加载」——进程内桥要等引擎 Future resolve 后才注册，多 GPU MoE 模型加载以分钟计、冷启动更久，30 分钟是给正常慢加载留的余量（L444-L447 注释）。加载**失败**不走这条超时：engine task 会在 Err 分支立刻 `server_shutdown.cancel()`，让错误及时浮出。

**练习 3**：引擎计数对账失败时，为什么要先 `server_shutdown.cancel()` 再 `bail!`？

**答案**：因为此刻 vllm-server 可能已经在预绑定 socket 上等注册了。只 bail 不取消的话，engine_task 带着 Err 退出，而 server 会一直挂到 30 分钟超时。先取消让 server 以「被关闭」路径立刻返回，随后装配收尾 `engine_task.await` 拿到这个 bail 的错误并上报——错误与退出各得其所。

### 4.3 引擎任务与 shutdown 级联：失败如何浮出、线程如何收割

#### 4.3.1 概念说明

装配函数起三份并发工作：engine_task（tokio 任务）、若干 bridge（JoinSet 里的 tokio 任务）、vllm-server（装配函数本体驻留的任务）。它们由一棵 shutdown 树协调：

```
shutdown（父，main.rs 持有；引擎就绪后 Ctrl-C 取消）
 ├── server_shutdown（子）→ 取消 vllm-server（HTTP）
 └── bridge_shutdown（子）→ 取消全部桥
```

`child_token` 的语义保证了级联的方向性：**父死全场死；单个子死不牵连兄弟**。于是每个失败源都有明确的「拉闸范围」：

| 失败源 | 拉闸动作 | 谁负责 |
|--------|----------|--------|
| 引擎加载失败 | 只拉 `server_shutdown` | engine_task 的 Err 分支 |
| 某个桥失败/退出 | `server_shutdown` 与 `bridge_shutdown` 都拉 | engine_task 的收尸循环 |
| HTTP server 失败 | 不拉闸也能收敛：server 自己返回，装配收尾拉 `bridge_shutdown` | 装配函数 |
| Ctrl-C（引擎已就绪） | 父 token 取消，级联全场 | main.rs 的 Ctrl-C 处理器 |

另一个主题是**收割（reap）**：step 引擎的调度器线程是普通 OS 线程（u3-l5 的 `spawn_scheduler`），不是 tokio 任务。桥持有调度器 handle 而线程句柄留在 `scheduler_joins` 里，等桥全部消亡、提交通道断开、驱动循环排空退出后（u3-l5 讲过的「排空退出」），装配函数才 join 这些线程，把调度器 panic 显形为警告日志。

#### 4.3.2 核心流程

引擎加载失败的完整取消链（非 LoRA 分支）：

```
spawn_blocking 里的 line.launch 返回 Err                     main.rs L204-L213
  └─ 包装 Future resolve 为 Err
       └─ engine_task：server_shutdown.cancel()               mod.rs L300-L301
            │   （vllm-server 若在等注册，立即以被取消路径返回 Ok）
            └─ engine_task 返回 Err(error)                    mod.rs L302
装配函数收尾（vllm_server 的 await 已返回）：
  bridge_shutdown.cancel()   （空操作，桥从未起）              mod.rs L505
  engine_task.await → Ok(Err(engine_error))                   mod.rs L509-L513
  result.and(Err(engine_error))  ← 引擎错误顶替 server 的 Ok   mod.rs L513
  remove_dir_all(namespace)  ← IPC 目录善后                   mod.rs L518
main.rs：
  .context("vLLM frontend server failed")                     main.rs L280
  tracing::flush() 之后 serve_result? 上抛                    main.rs L285-L286
```

桥失败时的链（成功加载之后）：

```
JoinSet 某桥返回 Err / 未取消却 Ok / panic
  └─ 记 bridge_error，break 出收尸循环                        mod.rs L365-L389
       └─ server_shutdown + bridge_shutdown 双双取消            mod.rs L390-L392
            └─ 排空剩余桥（让它们中止请求、冲刷输出）            mod.rs L393-L394
  └─ spawn_blocking 里逐个 join scheduler_joins               mod.rs L399-L408
       （桥死 → handle 死 → 提交通道断 → 驱动排空退出，见 u3-l5）
  └─ 返回 Err，同上路径上抛
```

#### 4.3.3 源码精读

[pegainfer-frontend/src/vllm/mod.rs:L298-L304](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L298-L304) — engine_task 的第一拍：`engine.await`。`Err` 分支只做两件事——`server_shutdown.cancel()` 与 `return Err(error)`。注意它**不动** `bridge_shutdown`：此刻桥还没起，无需拉闸；也不动父 token：调用方（main）的 shutdown 语义不该被引擎失败改变，错误本身会终结一切。

[pegainfer-frontend/src/vllm/mod.rs:L365-L395](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L365-L395) — 桥的收尸循环，四类结果三种待遇：`Ok(())` 且桥已被取消 → 正常关闭，忽略；`Ok(())` 但没取消 → 「意外退出」，记为错误；`Err` → 记错误并附 `local vLLM engine {i} bridge failed` 上下文；`JoinError` → 桥任务 panic。命中任何错误就 break，然后**双双取消**两个 token，再 `while bridges.join_next().await.is_some() {}` 把剩余桥排空——注释说明这是为了让其他桥「中止它们的请求并冲刷输出」，即优雅地死，而不是被硬切。

[pegainfer-frontend/src/vllm/mod.rs:L396-L408](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L396-L408) — 调度器线程收割：注释给出完整的因果链「桥没了 → 分区 handle 没了 → 提交通道断开 → 驱动排空退出」，所以这里只需 join。join 放进 `spawn_blocking`（阻塞 join 不堵 async 运行时），任一线程 join 失败（panic）打 `scheduler thread panicked during shutdown` 警告。

[pegainfer-frontend/src/vllm/mod.rs:L502-L519](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L502-L519) — 装配收尾四部曲：① `bridge_shutdown.cancel()`（调用方已取消时是空操作）；② 若 server 失败而 engine_task 还没完，打警告 `HTTP server failed; waiting for the in-flight engine load to finish before exit` 并**等阻塞加载做完**——注释点明阻塞加载不可取消，直接退出会让运行时撕裂时阻塞线程还在写；③ `engine_task.await` 合并结果：`Ok(Ok(()))` 用 server 的结果；`Ok(Err(engine_error))` 用 `result.and(Err(engine_error))`——**引擎错误优先**（注释 L511-L513：server 看到的只是取消，返回 Ok，引擎错误才是值得报告的）；`Err(join_error)` 包装成 `engine startup task panicked`；④ 删掉 IPC 目录，无论成败。

[pegainfer-frontend/src/vllm/mod.rs:L543-L561](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L543-L561) — Ctrl-C 帮手 `cancel_token_on_ctrl_c` 与 `shutdown_token_from_ctrl_c`：文档注释给出关键警告——安装处理器会**替换** SIGINT 的默认 kill 行为，所以只有当 token 保护的东西真的能收场时才能装（main.rs 非 LoRA 分支因此把它放在引擎 Future resolve 之后才调用，L250）。这也是 u1-l3 讲过的「Ctrl-C 两段式」在 frontend 侧的落点。

#### 4.3.4 代码实践：触发一次引擎失败，核对取消链

1. **实践目标**：让引擎加载失败真实发生一次，把 4.3.2 的取消链与日志逐条对上。
2. **操作步骤**：
   - 准备一个「只有身份、没有引擎」的模型目录：`config.json` 写 `{"model_type": "qwen3"}` 并补一个可用的 `tokenizer.json`（u1-l4/u2-l4 的做法：可复制 `pegainfer-frontend/examples/echo-model/tokenizer.json`），**不放权重**。
   - 运行 `cargo run --release -- --model-path <该目录> --port 9000`。
   - 观察日志顺序，并另开终端确认 9000 端口**从未可达**（`curl http://127.0.0.1:9000/v1/models` 连不上）。
3. **需要观察的现象**：检测与校验通过、`Loading engine...` 之后 launch 失败；错误信息带 `failed to start qwen3 engine` 上下文，外层再包 `vLLM frontend server failed`；进程退出。
4. **预期结果**：对照源码，这次失败走的正是 4.3.2 的链——spawn_blocking 里 launch 报 Err → `server_shutdown.cancel()`（mod.rs L301）→ server 未等到注册即被取消、TCP 从未 bind → `engine_task.await` 拿到 Err → `result.and(Err(engine_error))`（L513）→ IPC 目录删除（L518）→ main 的 `.context("vLLM frontend server failed")`（main.rs L280）。「端口从未可达」验证了「可达即就绪」：bind 排在注册之后。**待本地验证**（需要能编译运行的本地环境；错误的具体措辞以运行为准）。

#### 4.3.5 小练习与答案

**练习 1**：`shutdown`、`server_shutdown`、`bridge_shutdown` 三个 token 的父子关系意味着什么？各自由谁取消？

**答案**：后两个都是 `shutdown.child_token()` 派生的子 token：取消父会级联取消两个子，取消子不影响父与兄弟。父由 main.rs 在引擎就绪后被 Ctrl-C 取消；`server_shutdown` 由 engine_task 在引擎失败或桥失败时取消；`bridge_shutdown` 由 engine_task 在桥失败时、或装配收尾统一取消。

**练习 2**：引擎加载失败时，vllm-server 返回的是 `Ok`，为什么 main 最终报的却是引擎错误？

**答案**：server 被取消时按正常关闭路径返回 Ok——取消不是它的错误。装配收尾 `engine_task.await` 得到 `Ok(Err(engine_error))`，用 `result.and(Err(engine_error))` 把 Ok 替换成引擎错误（L509-L513，注释明言「引擎错误才是值得报告的」），保证用户看到的是**根因**而不是「服务器被关了」这种表象。

**练习 3**：HTTP server 失败而引擎还在加载，进程会立刻退出吗？

**答案**：不会。`bridge_shutdown.cancel()` 对不可取消的阻塞加载无效；装配函数打警告 `HTTP server failed; waiting for the in-flight engine load to finish before exit` 后在 `engine_task.await` 上等加载做完（L504-L508）。这是刻意的：运行时撕裂时阻塞线程可能还在写，等它自然结束才安全。

## 5. 综合实践

把本讲三块内容串成一张图 + 一段链（纸面作业为主，可选实机验证）：

**任务：双时间线与取消链的全景图**

1. **画双时间线**。从 `main.rs` 非 LoRA 分支（L238-L278）出发，画两条并行时间线：
   - A 线（引擎）：`spawn_blocking`（L204）→ 权重上 GPU → 包装 Future resolve → engine_task 对账 → 每分区一桥 → 桥连预绑定 socket（注册）。
   - B 线（前端）：`serve_with_engine_count` → 建 IPC 目录与预绑定 socket（mod.rs L275-L282）→ `load_model_backends`（L419，秒级）→ vllm-server 加载 tokenizer/模板（约 1s）→ 等注册（≤30 min）→ bind TCP。
   - 在图上标出：**汇合点**是桥对预绑定 socket 的连接（不是 main 里任何 await）；**TCP 端口可达的时刻**在注册之后——写出这条「可达即就绪」语义的代码依据（mod.rs L64-L67 的文档注释 + L456 的 `BindTcp` 只是声明）。
2. **写取消链**。假设 A 线在 t1 失败（`line.launch` 返回 Err），从失败点开始写出完整传播链，每一跳给出文件与行号：包装 Future → engine_task L300-L302 → server 被取消返回 Ok → 收尾 L505-L518 → main.rs L280 的 context → L285-L286 的 flush 与上抛。再补一条对照链：若失败的不是引擎而是某个桥，拉闸范围有何不同（双双取消 + 排空 + 收割调度器线程，L390-L408）？
3. **可选实机验证**（待本地验证）：用 4.3.4 的 config-only 目录跑一次失败路径，核对日志与你的取消链是否一致；再用 echo-server（4.2.4）跑一次成功路径，确认 Ctrl-C 后 IPC 目录被清理——对应收尾的 `remove_dir_all`。

**验收标准**：不看讲义，能指着图说出「端口什么时候算 ready」「引擎错误为什么顶替 server 的 Ok」「为什么 Ctrl-C 处理器要等引擎就绪才装」。

## 6. 本讲小结

- serve 函数族五个入口收拢到唯一的私有骨架 `serve_model_on_host_with_router_extension`；`main.rs` 按 `ServePlan` 三分支选择：LoRA（串行、要求 `Stepped`）→ prefill-only → 默认 `serve_with_engine_count`，后两者的 `engine_count` 来自 `scheduler_partition_count`。
- 骨架**预绑定**两个 unix socket 并以 fd 交给 vllm-server 领养（supervisor 角色），使引擎桥与 HTTP 前端的并发不再有时序依赖；TCP 端口只在桥注册之后 bind，因此「端口可达 = 引擎就绪」。
- 引擎 resolve 后做**计数对账**：`Handle` 变体对 `scheduler_partition_count()`、`Stepped` 变体对 `schedulers.len()`，声明数与实际数不符就取消服务并 bail——请求寻址参数错配宁可死不带病服务。
- `ready_timeout = 30 min` 只兜底「真挂死」的加载；加载**失败**由 engine_task 立刻取消 `server_shutdown` 上报，二者各司其职。
- shutdown 是一棵 `child_token` 树：父（Ctrl-C）级联全场；引擎失败只拉 server；桥失败双拉并排空剩余桥、再收割调度器线程（桥死 → handle 死 → 提交通道断 → 驱动排空退出）。
- 收尾合并结果时**引擎错误优先**（`result.and(Err(engine_error))`），IPC 目录无论成败都删除；阻塞加载不可取消，server 先死时进程要等加载做完才退。

## 7. 下一步学习建议

本讲把骨架立起来了，但 HTTP 请求从进入到打中调度器还隔着三层，单元四接下来的三讲逐层下钻：

1. **u4-l2（HTTP 前端装配）**：本讲 L437-L491 的 `Config` 只点了两处，下一讲逐项精读全部字段，并展开路由扩展链 `normalize_chat_requests(mark_history(extend_router))` 的包裹顺序；实践是跑通 echo-server 的两条 curl。
2. **u4-l3（两代引擎桥）**：本讲反复出现的 `LocalEngineBridge` / `SteppedEngineBridge` 的内部——zmq dealer/push 双 socket 如何接上预绑定 listener、legacy 桥如何 demux 事件流、stepped 桥如何 1:1 翻译 `StepOutputs`。
3. **回看 u3-l5**：如果「桥死 → 提交通道断 → 驱动排空退出」这条因果链还不扎实，回去重读 `drive` 循环的退出条件——本讲的收割逻辑完全建立在它之上。
