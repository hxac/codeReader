# ServePlan：引擎加载前就必须知道的服务形态

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `ServePlan` 四个字段——`scheduler_partition_count`、`prefill_only`、`lora_modules`、`auto_tool_call_parser`——各自的**消费点**：它们分别在 `main.rs` 的哪一行被读取、驱动了哪个 serve 函数。
2. 解释**为什么分区数必须在引擎加载完成之前就可推导**：HTTP 前端在引擎仍在加载权重时，就要按分区数注册 engine identity，并在引擎就绪后与实际分区数**对账**。
3. 理解 `resolve_tool_call_parser` 的三级优先级：**显式用户选择 > 线自带默认 > `Auto`**，以及为什么模型线要自己解析 `auto`（上游按模型路径的子串匹配）。
4. 读懂 `vllm/mod.rs` 里 `TransportMode::Bootstrapped` 的 `engine_count` 字段与两处「声明数 vs 实际数」校验，明白它们构成一道防带病服务的安全门。

本讲是单元二的收官：u2-l1 给出了 `ModelLine` trait 的 8 个方法，本讲精读其中最「面向前端」的一个——`serve_plan`。

## 2. 前置知识

本讲需要以下概念（前几讲已建立，这里只做一句话回顾加少量新术语）：

- **`ModelLine` trait 接缝**（u2-l1）：server 二进制只持有一列 `&'static dyn ModelLine`，从不 import 模型 crate 的选项类型。`serve_plan` 是 trait 的第 7 个方法，带默认实现。
- **`main()` 装配线**（u1-l3）：检测（detect）→ 三级校验 → `serve_plan` → `spawn_blocking` 加载引擎 → 选 serve 分支。本讲聚焦最后两步之间的那份数据。
- **引擎加载是分钟级操作**：权重读盘 → 搬上 GPU → 建调度器线程。所以 main 把它放进 `tokio::task::spawn_blocking`，让 HTTP 前端（tokenizer、chat 模板，约 1 秒）并行加载。新术语：
  - **engine identity（引擎身份）**：HTTP 前端协议层眼里「一台引擎」的注册单位。普通模型线是 1 个；数据并行（DP）模型线每个调度器分区一个。字段 `engine_count` 就是这个数量。
  - **调度器分区（scheduler partition）= 逻辑 DP rank**：一条模型线内部可能并行跑多个调度器，各自服务一部分请求。GLM5.2 的 EP8 就是 8 个逻辑 rank。
  - **P/D 分离（prefill/decode disaggregation）**：把预填充（prefill）和解码（decode）拆到不同实例上。`prefill_only` 为 true 的实例只做预填充，一个请求最多产出 1 个 token（其 KV 会传给 decode 侧）。
  - **LoRA 适配器与控制平面**：LoRA 是贴在基础模型上的小增量权重；「控制平面」指引擎暴露的运行时装载/卸载适配器的通道（`LoraClient`）。路由构建期就要用到它。
  - **tool-call parser（工具调用解析器）**：把模型输出文本里的工具调用片段解析成结构化 JSON 的解析器。`ParserSelection` 是它的选择类型：`Auto`、`None` 或显式名字（如 `qwen3_coder`）。
- **`impl Future<Output = Result<LaunchedEngine>>` 参数**：Rust 里 async 函数的参数可以是一个「将来才出结果的值」。serve 函数族接受的是**引擎的 Future**，而不是加载完的引擎——这正是「前端先行、引擎后到」在类型上的表达。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs) | **契约本体**：`ServePlan` 结构体、`resolve_tool_call_parser`、`Default` 实现、trait 方法 `serve_plan` 的默认实现（L286-L376） |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs) | **消费方**：调 `line.serve_plan(&ctx)` 拿到 plan，再按 plan 字段走三分支（L186、L198、L215-L279） |
| [pegainfer-frontend/src/vllm/mod.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs) | **serve 函数族**：`serve` / `serve_with_engine_count` / `serve_prefill_only_with_engine_count` / `serve_model_with_lora_routes`，以及它们共同汇聚到的装配函数 `serve_model_on_host_with_router_extension`（引擎计数对账在这里） |
| [pegainfer-glm52/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-glm52/src/model_line.rs) | 生产者实例 1：GLM5.2 用拓扑与 rank 范围推导分区数、声明 prefill-only（L211-L227），并配有单测（L477-L501） |
| [pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/model_line.rs) | 生产者实例 2：Qwen3 把 LoRA 开关折进 plan（L288-L294） |
| [pegainfer-qwen35/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen35/src/model_line.rs) | 生产者实例 3：Qwen3.5 声明线自带的 `auto` 解析器（L151-L161） |
| [pegainfer-frontend/src/engine/handle.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs) | 对账的「实际数」来源之一：`EngineHandle::scheduler_partition_count`（L169-L172） |

## 4. 核心概念与源码讲解

### 4.1 ServePlan 定义：把「加载前事实」从模型线带出来

#### 4.1.1 概念说明

回忆 main() 的时间线：校验全部通过后，引擎加载（分钟级）与 HTTP 前端装配（秒级）**并行**进行。这带来一个结构性矛盾：

> HTTP 前端在引擎还没加载完时，就已经需要知道一些**只有模型线才知道**的事实——这个引擎有几个分区？是不是只做 prefill？要不要挂 LoRA 路由？工具解析器用哪个？

这些事实不能等引擎加载完再问，因为前端装配要先于引擎完成；但它们也不能由 server 猜，因为 server 是纯分发二进制，不认识任何模型。解法就是 `ServePlan`：**模型线在引擎加载开始之前，把「前端赖以装配的事实」一次性交给 server**。它解决的问题是「并行的两侧需要提前对齐服务形态」。

为什么这些事实「必须只从 CLI 与 config.json 推导」？因为 `serve_plan` 被调用的时刻（main.rs L186），唯一的输入就是 `LaunchContext`——模型路径、被认领的 config、共享 flag、合并解析的 matches。没有权重、没有 GPU、没有调度器。这是契约对实现者的硬约束：**分区数之类的值必须是 CLI 可推导的**。

#### 4.1.2 核心流程

```
main() 时间线（本讲关心的中段）

 ├─ line.validate(&ctx, &provided)        // 上讲：线内校验矩阵
 ├─ line.serve_plan(&ctx)  ────────────►  ServePlan { … }   ← 引擎尚未开始加载
 │        输入仅 LaunchContext（CLI + config.json）
 ├─ spawn_blocking( line.launch )  ───────────┐ 重量级：权重 → GPU（分钟级）
 │                                            │ 并行
 └─ serve 分支选择（读 plan 四字段）            │
      ├─ lora_modules = Some   → serve_model_with_lora_routes（先等引擎）
      └─ 否则 → 把「引擎 Future」交给
           ├─ prefill_only = true → serve_prefill_only_with_engine_count
           └─ 默认               → serve_with_engine_count
                 │
                 └─ HTTP 前端立刻加载 tokenizer/模板，按 engine_count 注册 N 个身份
                    引擎 resolve 后 → 对账（声明数 vs 实际数）→ 逐分区挂桥
```

`ServePlan` 的取值语义（谁生产、谁消费）：

| 字段 | 生产者（谁设置） | 消费者（谁读取） |
|------|------------------|------------------|
| `scheduler_partition_count` | 数据并行线（如 GLM5.2），默认 1 | main.rs 作为 `engine_count` 实参 → 前端注册身份数 + 对账 |
| `prefill_only` | P/D prefill 角色（GLM5.2 TP4），默认 false | main.rs 选 prefill-only serve 函数 |
| `lora_modules` | LoRA 线（Qwen3），默认 None | main.rs 选 LoRA serve 函数并预装适配器 |
| `auto_tool_call_parser` | 工具格式统一的线（Qwen3.5），默认 None | main.rs 在分支**之前**折算最终解析器 |

#### 4.1.3 源码精读

**① 结构体定义与文档注释**——这是理解整个设计的钥匙：

> [pegainfer-frontend/src/model_line.rs:L286-L309](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L286-L309)
> 这段定义了 `ServePlan`：「前端必须在引擎加载完成之前（且独立于加载）知道的东西」。文档注释还规定实现纪律：各线应使用 `..ServePlan::default()` 展开语法、只拼写自己设置的字段——因为 CI 只编译三条模型线，任何其他 crate 里的**穷尽式结构体字面量**在新增字段时会编译失败，而没有 CI 覆盖的 crate 就没人发现。

四个字段逐个看（行号同上）：

- `scheduler_partition_count: usize`（L294-L299）：注释明确说「HTTP 前端在**引擎仍在加载时**就按分区注册 engine identity，所以它必须只从 CLI flag 可推导；加载完成后与 `EngineHandle::scheduler_partition_count` 对账」。
- `prefill_only: bool`（L300-L301）：服务 prefill-only 路由契约（GLM5.2 TP4 P/D 的 prefill 角色）。
- `lora_modules: Option<Vec<LoraModule>>`（L302-L303）：`Some` 即开启 LoRA 路由并预装列出的适配器。注意 `Option` 语义：`Some(vec![])`（空的）也算「开」。
- `auto_tool_call_parser: Option<ParserSelection>`（L304-L308）：当用户把 `--tool-call-parser` 留在 `auto` 时，本线工具语法所对应的解析器。注释解释了动机：**上游 vLLM 的 `Auto` 按模型「路径」的子串匹配**——`Qwen3.8-27B` 目录名会被解析成 `qwen3_json`，而工具格式相同的线可以自己解析，让目录名不再说了算。

**② `resolve_tool_call_parser`：三级优先级**

> [pegainfer-frontend/src/model_line.rs:L311-L323](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L311-L323)
> 这段实现优先级折算：`match requested { Auto => 线默认或仍为 Auto, explicit => explicit }`——显式用户选择原样通过；只有 `Auto` 才落入线自带默认的回退。

优先级可写成：

```
resolve(requested) =
    if requested ≠ Auto          → requested          （用户显式选择，最高）
    else if 线声明了默认          → 线的默认            （模型线知道自己）
    else                          → Auto               （留给上游按路径匹配，最低）
```

**③ `Default` 与 trait 默认实现**

> [pegainfer-frontend/src/model_line.rs:L325-L334](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L325-L334)
> 这段给出默认值：单分区、非 prefill-only、无 LoRA、无自动解析器——**绝大多数模型线要的全部**。

> [pegainfer-frontend/src/model_line.rs:L373-L376](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L373-L376)
> trait 方法 `serve_plan` 的默认实现直接返回 `ServePlan::default()`，所以普通模型线（DeepSeek-V2-Lite、Kimi-K2 等）**无需覆写**。

**④ 三个真实生产者**

GLM5.2——最复杂的一个，覆写 `scheduler_partition_count` 与 `prefill_only`：

> [pegainfer-glm52/src/model_line.rs:L211-L227](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-glm52/src/model_line.rs#L211-L227)
> 这段从 CLI 推导分区数：给了 `--glm52-ranks` 且拓扑不做张量复制 MoE 时，分区数 = `parse_rank_range(spec).len()`（部分舰队只托管自己的 rank）；否则 = `moe_topo.logical_rank_count()`。注意整个推导只碰 CLI 字符串，这正是「加载前可知」的约束在起作用；`--glm52-ranks` 非法时用 `CliError::rule` 当场拒绝。

Qwen3——只折 LoRA 开关：

> [pegainfer-qwen3/src/model_line.rs:L288-L294](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L288-L294)
> 这段把 `cli.enable_lora` 折成 `Some(cli.lora_modules.clone())`：`bool::then` 只看开关——`--enable-lora` 不带 `--lora-modules` 时得到 `Some(vec![])`，同样进入 LoRA 分支，只是启动时不预装任何适配器。

Qwen3.5——只声明自动解析器：

> [pegainfer-qwen35/src/model_line.rs:L151-L161](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen35/src/model_line.rs#L151-L161)
> 这段把 `auto_tool_call_parser` 固定为 `qwen3_coder`。注释复述了动机：两代模板（Qwen3.5/Qwen3.8）都用 Qwen Coder 工具语法，但上游 `Auto` 按路径子串会把 `Qwen3.8-27B` 目录错配成 `qwen3_json`——「线比目录名更懂自己」。

**⑤ 一个可直接对照的单测**：

> [pegainfer-glm52/src/model_line.rs:L477-L501](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-glm52/src/model_line.rs#L477-L501)
> 这个测试 `serve_plan_counts_partial_fleet_ranks` 用 u2-l3 介绍过的公开帮手 `parse_for_line` 解析 `--moe-topo ep16 --glm52-ranks 4..8`，手工构造 `LaunchContext` 后断言 `plan.scheduler_partition_count == 4` 且 `prefill_only == false`——纯内存验证，不碰 GPU。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`serve_plan` 的输入只有 CLI 与 config，输出可离线推算」。

**操作步骤**：

1. 打开三个生产者实现（上文链接 ④ 的三处），确认每个函数体内只出现 `cli(ctx)`、`ctx.shared` 与对 config 的读取，没有任何权重/GPU/调度器操作。
2. 笔算两个输入的 plan：
   - `--moe-topo ep16 --glm52-ranks 4..8 --glm52-rendezvous host:1`（GLM5.2）：`parse_rank_range("4..8")` 覆盖 4、5、6、7 共 4 个 rank → `scheduler_partition_count = 4`。
   - `--enable-lora`（Qwen3，不带 `--lora-modules`）：`lora_modules = Some(vec![])`，其余三字段全默认。
3. （可选，需 glm52 feature 构建链）运行单测：
   ```bash
   cargo test --release -p pegainfer-glm52 --features glm52 serve_plan_counts_partial_fleet_ranks --lib
   ```
   与步骤 2 的笔算互证。若本机没有该 feature 的构建环境（CUDA 工具链、sm_90a 时还需 TileLang Python），以源码阅读为准——**此命令待本地验证**。

**需要观察的现象**：`parse_rank_range("4..8")` 的结果是「含头不含尾」的区间 `[4, 8)`，长度 4——这正是单测断言 `assert_eq!(plan.scheduler_partition_count, 4)` 的来源。

**预期结果**：能不运行任何 GPU 代码，仅凭 CLI 字符串写出任意一组 flag 对应的 `ServePlan` 四字段值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ServePlan` 的文档注释要求各线用 `..ServePlan::default()` 而不是穷尽式字面量？

**参考答案**：`ServePlan` 定义在 frontend crate，而实现分散在各模型 crate。CI 只编译三条线；若某条未编译的线写了穷尽式字面量，frontend 给 `ServePlan` 新增字段时那条线会编译失败却无人发现，直到有人手工检查。`..Default::default()` 展开让新增字段自动取默认值，是防漂移的扩展约定。

**练习 2**：`--enable-lora` 但不传 `--lora-modules` 时，`plan.lora_modules` 是什么？走不走 LoRA 分支？

**参考答案**：`bool::then` 只看 `enable_lora`，所以是 `Some(vec![])`——走 LoRA 分支（`if let Some` 命中），只是启动时不预装适配器，之后可经运行时 LoRA 路由装载。`Option` 表达的是「能力开关」，空 `Vec` 仍是「开」。

**练习 3**：用户显式传 `--tool-call-parser qwen3_json` 时，Qwen3.5 的 `auto_tool_call_parser` 还生效吗？

**参考答案**：不生效。`resolve_tool_call_parser` 的 match 里 `explicit => explicit` 分支原样返回用户值，线自带默认只在 `requested == Auto` 时参与回退——显式选择优先级最高。

### 4.2 main() 的三分支选择：plan 字段如何决定 serve 路径

#### 4.2.1 概念说明

main() 拿到 plan 之后，用它做一个**静态的分派决策**：选哪条 serve 路径、传什么参数。这不是运行时的动态调度——从 plan 确定那一刻起，服务形态就定了。三个字段参与选择，优先级如下：

1. `lora_modules` 是 `Some` → LoRA 路径（**串行**：先等引擎加载完）。
2. 否则若 `prefill_only` → prefill-only 路径（并行 + 强制 `max_tokens=1` 的路由门）。
3. 否则 → 普通路径（并行）。

而 `auto_tool_call_parser` 不参与分支选择，它在**分支之前**就被 `resolve_tool_call_parser` 折算成最终解析器，随后传给所有 serve 函数。

为什么 LoRA 路径要串行、其他两条可以并行？因为 LoRA 路由的构建期就需要引擎的**控制平面**（`LoraClient`，运行时装卸适配器的通道）——路由要注册「列出/装载适配器」的端点，就得先拿到引擎。而普通路径的前端装配（tokenizer、模板）完全不依赖引擎内部，可以纯粹并行。

#### 4.2.2 核心流程

```
serve 分支选择（main.rs L215-L279）

if let Some(lora_modules) = plan.lora_modules {          // ① LoRA 分支（串行）
    先 engine_load.await（等引擎加载完）
    解构 LaunchedEngine::Stepped(engine)，Handle 则 bail
    max_model_len ← load_max_model_len(model_path) 或 4096
    shutdown ← shutdown_token_from_ctrl_c()（此时可立即挂 Ctrl-C）
    serve_model_with_lora_routes(engine, …, lora_modules, …)
} else {
    shutdown ← 新 CancellationToken（Ctrl-C 暂时保持默认 kill）
    engine = 一个 Future：等加载 → 打日志 → 此刻才挂 Ctrl-C
    if plan.prefill_only {                               // ② prefill-only 分支
        serve_prefill_only_with_engine_count(engine, …,
            plan.scheduler_partition_count, shutdown)
    } else {                                             // ③ 普通分支
        serve_with_engine_count(engine, …,
            plan.scheduler_partition_count, shutdown)
    }
}
```

值得注意的细节：LoRA 分支的 `engine_count` **不来自 plan**——`serve_model_with_lora_routes` 内部硬编码为 1（见下文源码 ③）；`max_model_len` 的来源也分叉：LoRA 分支在 main.rs 自己读 config（`None`→`unwrap_or(4096)` 回退），另外两分支传 `None` 交给 serve 函数族解析。Ctrl-C 行为同样分叉：串行分支引擎已就绪、可立即安装优雅关停；并行分支必须等引擎 resolve 后才安装（不可取消的阻塞加载期间 SIGINT 保持默认 kill）——这正是 u1-l3 讲过的「两段式 Ctrl-C」在分支维度的体现。

#### 4.2.3 源码精读

**① plan 的两个早期消费点**：

> [src/main.rs:L186](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L186)
> 这行调用 `line.serve_plan(&ctx)?`——注意它位于 `info!("Loading engine...")` 横幅（L189）**之前**，即 plan 的产出属于「校验期」，失败形态是 `CliError`，发生在任何昂贵操作之前。

> [src/main.rs:L198](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L198)
> 这行用 `plan.resolve_tool_call_parser(shared.tool_call_parser.clone())` 折算最终解析器——`auto_tool_call_parser` 的唯一消费点，在分支选择之前。

**② 三分支主体**：

> [src/main.rs:L215-L237](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L237)
> 这段是 LoRA 分支：`if let Some(lora_modules) = plan.lora_modules` 命中后**先 `engine_load.await`**（注释说明：路由构建时需要引擎控制平面，所以保持串行），随后用 let-else 解构 `LaunchedEngine::Stepped`，Handle 形态直接 `bail!("LoRA serving requires a step-driven engine")`；最后调 `serve_model_with_lora_routes`，Ctrl-C 用 `shutdown_token_from_ctrl_c()` 立即生效。

> [src/main.rs:L254-L265](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L254-L265)
> 这段是 prefill-only 分支：`plan.prefill_only` 为真时把引擎 Future 交给 `serve_prefill_only_with_engine_count`，`plan.scheduler_partition_count` 作为第 7 个实参（`max_model_len` 传 `None` 交由函数族从 config.json 解析）。

> [src/main.rs:L266-L278](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L266-L278)
> 这段是默认分支：`serve_with_engine_count`，同样以 `plan.scheduler_partition_count` 作 `engine_count`。两个并行分支的差别只有路由形态（是否强制 `max_tokens=1`）。

**③ LoRA 路径的 engine_count 硬编码**：

> [pegainfer-frontend/src/vllm/mod.rs:L207-L216](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L207-L216)
> 这段是 `serve_model_with_lora_routes` 的转发调用：它用 `std::future::ready(Ok(LaunchedEngine::Stepped(engine)))` 把已加载的引擎包成立即就绪的 Future，而 `engine_count` 实参硬编码为 `1`（L215）——LoRA 服务面不消费 plan 的分区数，这也解释了为什么唯一的 LoRA 线 Qwen3 把分区数留在默认值 1。

#### 4.2.4 代码实践

**实践目标**：亲手写出「plan 字段 → main.rs 具体行为」的完整映射表。

**操作步骤**：

1. 通读 [src/main.rs:L215-L279](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L279)，对每个 plan 字段回答三问：在哪一行被判断？命中后调用哪个函数？`plan.scheduler_partition_count` 最终变成了谁的实参？
2. 补上两个源码深挖点：LoRA 分支的 `engine_count` 从哪来（追到 `serve_model_with_lora_routes` 内部）；`auto_tool_call_parser` 在哪一行被消费（L198，注意它在三分支之前）。
3. 把结果整理成五行映射表（四个字段 + 一行「LoRA 分支下分区数的去向」）。

**需要观察的现象**：只有 `scheduler_partition_count` 会被当作数值参数传递；`prefill_only` 与 `lora_modules` 只做布尔/Option 判断，从不传值；`auto_tool_call_parser` 的消费点早于分支选择。

**预期结果**：与本讲 4.2.5 练习 1 的参考表逐行一致。

#### 4.2.5 小练习与答案

**练习 1**：给出完整的字段 → 行为映射表。

**参考答案**：

| plan 字段（取值） | main.rs 判定点 | 结果行为 |
|---|---|---|
| `lora_modules = Some(..)` | L215 `if let Some` | 串行等引擎 → 要求 `Stepped` → `serve_model_with_lora_routes`；`engine_count` 在函数内硬编码 1，**不消费**分区数 |
| `lora_modules = None` 且 `prefill_only = true` | L254 | `serve_prefill_only_with_engine_count(engine, …, plan.scheduler_partition_count, shutdown)` |
| `lora_modules = None` 且 `prefill_only = false` | L267 | `serve_with_engine_count(engine, …, plan.scheduler_partition_count, shutdown)` |
| `scheduler_partition_count` | L262 / L274 实参 | 成为 `engine_count`：前端注册的引擎身份数 + 加载后对账的声明数 |
| `auto_tool_call_parser` | L198（分支之前） | 经 `resolve_tool_call_parser` 折算最终解析器，传入所有 serve 函数 |

**练习 2**：Qwen3 默认构建（不开 LoRA）会走哪条分支？`engine_count` 是多少？

**参考答案**：Qwen3Line 的 `serve_plan` 除 LoRA 外全默认，所以不开 `--enable-lora` 时 `lora_modules = None`、`prefill_only = false`，走第三分支 `serve_with_engine_count`，`engine_count = 1`（默认分区数）。

**练习 3**：为什么 `auto_tool_call_parser` 的消费在分支之前，而不是塞进某条 serve 函数里？

**参考答案**：它是**解析器选择**而非路由形态，三条路径都需要同一个已折算的解析器（都接收 `tool_call_parser` 参数）。在 main 里折算一次，既避免三条路径各自处理优先级，也保证无论走哪条分支，用户显式选择 > 线默认 > `Auto` 的规则只有一份实现。

### 4.3 serve_with_engine_count：TransportMode::Bootstrapped 与引擎计数对账

#### 4.3.1 概念说明

`engine_count` 进入 vllm 模块后承担两个职责，一个是**先行的声明**，一个是**事后的对账**：

- **声明**：HTTP 前端启动时（引擎还在加载！）就要按 `engine_count` 注册 N 个 engine identity 并等待它们上线。协议栈的 `Config` 里 `TransportMode::Bootstrapped { engine_count, data_parallel_size, … }` 承载这个声明。
- **对账**：引擎 Future resolve 后，装配代码立刻比较「声明的 engine_count」与「引擎实际暴露的分区数」，不一致就取消整个服务并报错。

为什么对账失败要「宁可死」？因为此时 HTTP 端口可能已经在按 N 个身份的假设工作：请求会按 `engine_index ∈ [0, data_parallel_size)` 寻址，若实际分区数不同，轻则某些身份永远等不到注册（耗到 30 分钟超时），重则请求被路由到不存在的引擎下标。**声明与事实不一致是配置级 bug，早死好过带病服务。**

#### 4.3.2 核心流程

```
serve_with_engine_count(engine_future, …, engine_count, …)
  └─ serve_model_on_host → serve_model_on_host_with_router_extension
       │
       ├─ 【引擎 resolve 之前】
       │    ensure!(engine_count > 0)
       │    data_parallel_size = engine_count as u32
       │    预绑定 input.sock / output.sock 两个 unix listener fd
       │    Config { transport_mode: Bootstrapped { engine_count,
       │              data_parallel_size, ready_timeout: 30min }, … }
       │    vllm_server 启动：注册 N 个身份、加载 tokenizer/模板、等待注册完成
       │
       └─ 【engine_task：引擎 resolve 之后】
            match launched {
              Handle(handle)  → handle.scheduler_partition_count() ≠ engine_count?
                                 → cancel server + bail
              Stepped(engine) → engine.schedulers.len() ≠ engine_count?
                                 → cancel server + bail
              （一致）→ 为 0..engine_count 每分区 spawn 一座桥
                        （每桥携带 engine_index 与 data_parallel_size）
            }
```

顺带一提，最朴素的 [`serve`](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L74-L94) 只是 `serve_with_engine_count` 固定 `engine_count = 1` 的薄包装——单引擎是数据并行的特例。

#### 4.3.3 源码精读

**① 入口：`serve_with_engine_count`**

> [pegainfer-frontend/src/vllm/mod.rs:L100-L122](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L100-L122)
> 这段定义了「一个 HTTP 端点背后挂 `engine_count` 个前端可见引擎身份」的入口：数据并行线每分区一个身份，普通线走 `serve` 落回单引擎；它只做参数整理（解析 `max_model_len`）后转发给装配函数。

**② 装配早期：声明的三步**

> [pegainfer-frontend/src/vllm/mod.rs:L272-L282](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L272-L282)
> 这段在引擎 resolve 之前完成三件事：断言 `engine_count > 0`；把 `engine_count` 转成 u32 作为 `data_parallel_size`；预绑定两个 unix socket listener（vllm-server 以「领养已绑定 fd」的方式启动，桥可以在任何时刻连上来）。

> [pegainfer-frontend/src/vllm/mod.rs:L437-L449](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L437-L449)
> 这段构造协议栈 `Config` 的 transport 字段：`TransportMode::Bootstrapped { input_listener_fd, output_listener_fd, engine_count, data_parallel_size, ready_timeout: 30 分钟 }`。注释说明超时的语义：进程内桥在引擎 Future resolve 后注册，所以这个超时兜住的是**整个引擎加载**（多 GPU MoE 要几分钟、冷启动更久）；加载**失败**另有取消路径，超时只抓真正挂死的加载。

**③ 引擎 resolve 之后：两处对账**

> [pegainfer-frontend/src/vllm/mod.rs:L310-L318](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L310-L318)
> 这段是 legacy `Handle` 形态的对账：`handle.scheduler_partition_count()` 与 `engine_count` 不相等时先 `server_shutdown.cancel()` 再 `bail!`，错误文案点明「前端声明了 N 个引擎，解析出的 handle 实际暴露 M 个调度器分区」。

> [pegainfer-frontend/src/vllm/mod.rs:L336-L344](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L336-L344)
> 这段是新一代 `Stepped` 形态的对账：用 `engine.schedulers.len()` 做同样比较、同样取消并 bail——两代契约各有一条对账分支，语义一致。

**④ 对账的「实际数」从哪来**：

> [pegainfer-frontend/src/engine/handle.rs:L169-L172](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/handle.rs#L169-L172)
> 这段是 `EngineHandle::scheduler_partition_count` 的实现：直接返回 `metrics_watches.len()`——引擎在构造 handle 时为每个分区挂一个负载 watch，watch 的数量就是分区数。注意可见性是 `pub(crate)`：外部世界不该直接问 handle 要分区数，只有 vllm 模块的对账代码（同 crate）用它。

**⑤ 对账通过后：每分区一座桥**

> [pegainfer-frontend/src/vllm/mod.rs:L348-L360](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L348-L360)
> 这段（Stepped 分支）按 `engine.schedulers.into_iter().enumerate()` 为每个分区造一座 `SteppedEngineBridge`，携带 `engine_index as u32` 与 `data_parallel_size`——请求协议正是靠这对值把请求路由到正确分区的，所以**分区数错误不是性能问题，是寻址错误**。

#### 4.3.4 代码实践

**实践目标**：论证「如果把分区数推迟到引擎加载后才告知，vllm/mod.rs 里哪段代码会失去依据」。

**操作步骤**：

1. 定位三处依赖 `engine_count` **早于**引擎 resolve 的代码：L272-L274（`ensure!` 与 `data_parallel_size` 转换）、L437-L449（`TransportMode::Bootstrapped` 的 Config 构造）、以及 vllm_server 侧按身份数注册并等待的逻辑（`ready_timeout` 兜底）。
2. 再定位两处**晚于** resolve 的消费：L310-L318 与 L336-L344 的对账、L348-L360 每桥的 `engine_index`/`data_parallel_size`。
3. 做思想实验：假设删除 `ServePlan::scheduler_partition_count`，分区数只能从加载完成的引擎读出（`handle.scheduler_partition_count()` 或 `engine.schedulers.len()`）。逐段推演上面每一处代码的处境。
4. 把结论写成三行：Config 构造会怎样、HTTP 端口与身份注册会怎样、对账与桥构造会怎样。

**需要观察的现象**：`Config` 是在 `serve_model_on_host_with_router_extension` 里**一次性**构造并传给 `vllm_server::serve_with_router_extension`（L493）的——它的每个字段（包括 `engine_count`）都必须在调用前有值；而 HTTP 服务器启动后端口就开始接受连接，身份注册的等待由 `ready_timeout`（30 分钟）封顶。

**预期结果**：能明确说出：`TransportMode::Bootstrapped { engine_count, data_parallel_size, … }` 将没有输入可填——Config 无法构造，HTTP 前端无法决定注册几个身份；要么端口空等 30 分钟超时，要么先按猜测值注册、引擎就绪后再推翻。而两处对账检查会退化为「拿引擎自己的数跟引擎自己的数比」，永远相等、失去意义；每桥的 `engine_index`/`data_parallel_size` 寻址也随之悬空。参考答案见 4.3.5 练习 3。

#### 4.3.5 小练习与答案

**练习 1**：`serve` 与 `serve_with_engine_count` 是什么关系？

**参考答案**：`serve` 是 `serve_with_engine_count` 固定 `engine_count = 1` 的薄包装（L83-L93 直接转发）。单引擎是数据并行的退化特例，两套语义共用一条装配路径。

**练习 2**：`ready_timeout: 30 分钟` 兜住的是什么？引擎加载**失败**也靠它吗？

**参考答案**：兜住的是引擎加载的**时长**——进程内桥要等引擎 Future resolve 后才注册，多 GPU MoE 加载以分钟计、冷启动更久，所以给 30 分钟。加载**失败**不走超时：engine 任务在 `engine.await` 出错时会主动 `server_shutdown.cancel()`（L298-L304），让错误立刻上浮而不是挂到超时。超时只抓「既没成功也没失败」的真挂死。

**练习 3**：完整回答实践任务的思想实验——分区数推迟到加载后才告知，哪些代码失去依据？

**参考答案**：三处直接失去依据：(1) [vllm/mod.rs:L437-L449](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L437-L449) 的 `TransportMode::Bootstrapped { engine_count, data_parallel_size }` 无值可填，`Config` 构不出来，`vllm_server::serve_with_router_extension` 无法启动——HTTP 前端也就无法决定注册几个 engine identity；(2) [L272-L274](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L272-L274) 的 `ensure!(engine_count > 0)` 与 u32 转换没有输入；(3) 两处对账（L310-L318、L336-L344）退化为同值自比、永不触发，失去「声明 vs 事实」的安全门意义；连带 L348-L360 每座桥的 `engine_index`/`data_parallel_size` 寻址参数失去来源。若强行「先猜后改」，端口已按猜的身份数在监听，改口意味着要么超时死等、要么请求寻址到不存在的引擎——这正是 `ServePlan` 存在的理由：**把必须在加载前对齐的事实，强制建模为加载前可推导的 CLI 函数**。

## 5. 综合实践

**任务：为一条虚构的模型线 `MyLine` 设计 `serve_plan`，并手动模拟 main() 的分支选择。**

设定：MyLine 支持 `--my-dp N`（数据并行度，1 到 8）、`--my-prefill-only`（P/D prefill 角色）、`--my-lora name=path,...`（LoRA 模块）；它所有 checkpoint 的工具格式统一为 `qwen3_coder`。

要求完成三步：

1. **写 serve_plan 伪代码**（示例代码，非项目原有）：

   ```rust
   // 示例代码：MyLine::serve_plan 伪代码
   fn serve_plan(&self, ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> {
       let cli = cli(ctx);                       // 取回独占 flag
       let n = cli.my_dp;                        // 已有 value_parser 保证 1..=8
       Ok(ServePlan {
           scheduler_partition_count: n,
           prefill_only: cli.my_prefill_only,
           lora_modules: (!cli.my_lora.is_empty()).then(|| cli.my_lora.clone()),
           auto_tool_call_parser: Some(ParserSelection::Explicit("qwen3_coder".into())),
           ..ServePlan::default()                // 扩展约定：展开默认，勿穷尽拼写
       })
   }
   ```

   对照三个真实实现自查：分区数是否只从 CLI 推导（对照 GLM5.2 的 L211-L227）？LoRA 开关的 `Option` 语义是否想清楚了「空 Vec 算不算开」（对照 Qwen3 的 `enable_lora.then`）？

2. **模拟四组输入的分支选择**，仿照 4.2.2 的流程图写出每组会命中的 serve 函数与 `engine_count` 值：
   - `--my-dp 4`；
   - `--my-dp 4 --my-prefill-only`；
   - `--my-lora foo=/tmp/foo`；
   - `--my-lora foo=/tmp/foo --my-dp 4`（陷阱题：LoRA 分支下 `--my-dp 4` 的分区数去哪了？）。

3. **回答串联问题**：你的第 4 组输入暴露了什么设计张力？（提示：`serve_model_with_lora_routes` 硬编码 `engine_count = 1`，若 MyLine 真想同时支持 LoRA 与多分区，main.rs 的三分支结构需要什么改变？）

**参考结论**：第 4 组里 `lora_modules = Some(..)` 优先命中 LoRA 分支，`scheduler_partition_count = 4` 被静默忽略（不消费）；张力在于三分支把「路由形态」与「分区数」耦在了一个 if-else 里，LoRA 分支没有通道把分区数传下去——真实项目里 Qwen3 恰好单分区、GLM5.2 恰好无 LoRA，两个维度从未同时非默认，这是靠模型线自身约束维持的巧合，而不是类型系统保证的互斥。

## 6. 本讲小结

- `ServePlan` 是模型线在**引擎加载开始之前**交给 server 的服务形态声明，四个字段对应前端的四个装配决策；它只允许从 CLI 与 config.json 推导，这是并行架构强加的约束。
- main() 按字段三分支：`lora_modules = Some` → 串行的 `serve_model_with_lora_routes`（要求 `Stepped`，`engine_count` 函数内硬编码 1）；否则 `prefill_only` → `serve_prefill_only_with_engine_count`；默认 → `serve_with_engine_count`。后两者把 `scheduler_partition_count` 作为 `engine_count` 实参。
- `auto_tool_call_parser` 在分支之前经 `resolve_tool_call_parser` 折算，优先级为**显式用户选择 > 线自带默认 > `Auto`**；动机是上游 `Auto` 按模型路径子串匹配会错配目录名（Qwen3.8 → `qwen3_json`）。
- `engine_count` 的双重职责：**先行声明**（`TransportMode::Bootstrapped` 让 HTTP 前端在引擎加载时就注册 N 个身份，30 分钟 `ready_timeout` 只兜真挂死）与**事后对账**（`Handle` 比 `scheduler_partition_count()`、`Stepped` 比 `schedulers.len()`，不一致即取消整个服务）。
- `..ServePlan::default()` 的展开约定是防漂移手段：穷尽式字面量在未编译的模型线里会随字段新增而悄悄失效。
- 对账失败「宁可死」的根因：分区数是**寻址参数**（`engine_index`/`data_parallel_size`），声明与事实不一致意味着请求会被路由到不存在的引擎，是配置级 bug 而非性能问题。

## 7. 下一步学习建议

本讲收束了单元二（CLI 装配与模型线契约）。接下来进入单元三「引擎契约与请求生命周期」：

- **u3-l1（引擎契约总览）**：本讲多次出现的 `LaunchedEngine::Handle` / `::Stepped` 与 `engine.schedulers` 将在那里展开——两代契约并存的地图是理解 LoRA 分支「要求 Stepped」的前置。
- **u3-l6（接线与路由）**：本讲 4.3 里「每分区一座桥」的 `engine_index` 寻址，在 `EngineHandle` 的多分区路由（绑定 rank / 最低负载）中有完整的运行时故事。
- 若想先看协议栈一侧：**u4-l1（serve 函数族）** 会把本讲 4.3 只描了轮廓的 `serve_model_on_host_with_router_extension` 装配（预绑定 fd、bridges JoinSet、shutdown 级联）逐段放大。
- 源码延伸阅读：`pegainfer-frontend/src/vllm/mod.rs` 的 L284-L303（引擎失败时的取消链）与本讲 4.3 的对账代码连起来读，能看到「声明—对账—取消」的完整错误处理闭环。
