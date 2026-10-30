# ModelLine 接入：probe、CLI 增补与服务计划

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 [`ModelLine`](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L337-L383) 这个 trait 是服务器二进制与模型 crate 之间的「缝」，以及服务器为什么能做到「纯分派、不认识任何模型的类型」。
2. 解释 `probe` 如何通过 `model_type` / `text_config.model_type` 认领 Qwen3.5 与 Qwen3.8（以及为什么 Qwen3.8 不需要一条新线）。
3. 追踪 `--qwen35-prefix-cache-mib`、`--max-batch`、`--qwen35-scheduler-policy` 三个专属旗标从 `augment_cli` 注册、到 `validate` 跨旗标校验、再到 `launch` 进入启动链的完整路径。
4. 说明 `serve_plan` 里 `auto_tool_call_parser` 解析为 `qwen3_coder` 的原因（这是一个真实的线上 bug 的防御）。
5. 参照内嵌 tests 的写法，自己新增一个「`--decode-overlap stream` 配超大 `--max-batch` 被拒绝」的单测并运行。

本讲是入门单元（u1）的最后一讲：u1-l3 的模块地图说 `pub mod model_line` 是 crate 三层公开面的「正门」，本讲就把这扇门整个打开。

## 2. 前置知识

先 recall 前三讲的两个结论：

- **混合架构 → 一条线两种注意力**（u1-l1）：Qwen3.5 是全注意力 + Gated DeltaNet 线性注意力的混合模型，Qwen3.8 与它共用同一条模型线。本讲会看到「共用一条线」在代码上到底意味着什么——probe 认领的是同一套身份字符串。
- **feature 门控**（u1-l2）：整个 crate 活在 `#![cfg(feature = "qwen35")]` 之后，`cargo test -p pegainfer-qwen35 --features qwen35 --lib` 这类命令需要构建期 Python + Triton 工具链。本讲的实践任务最后会用到这条命令。

再补充四个本讲要用到的 Rust / clap 概念：

- **trait 与 trait 对象**：`&'static dyn ModelLine` 是动态分派的 trait 对象。服务器持有 `Vec<&'static dyn ModelLine>`，运行时按 config.json 挑出一条线来调它的方法——服务器因此不需要为每条线泛型化，也不需要 `use` 任何模型 crate 的具体类型。
- **clap 的 derive 模式**：`#[derive(clap::Args)]` 的结构体可以用 `augment_args(cmd)` 把自己的字段注册成 CLI 旗标（字段名 `qwen35_prefix_cache_mib` 自动变成旗标 `--qwen35-prefix-cache-mib`）；`FromArgMatches` 则反过来，从一次解析结果中取回这个结构体。`#[derive(ValueEnum)]` 让枚举成为旗标的可选值集合。
- **「显式提供」与默认值的区别**：clap 里每个旗标都可能带默认值，但校验逻辑常常只关心**用户真正敲过**的旗标。frontend 的 [`provided_args`](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L641-L661) 用 `ValueSource::CommandLine | EnvVariable` 过滤出这个集合（`BTreeSet<String>`，存的是旗标 id）。
- **消费或拒绝（consume-or-reject）**：PegaInfer 的 CLI 设计纪律——每条被提供的旗标必须有明确归属：要么是核心旗标，要么是被当前模型线**声明消费**的共享旗标，要么是本线专属旗标。三者都不是 → 启动直接报错。这防止「用户传了个旗标但当前模型根本不理它」的静默失效。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs)（353 行） | **本讲主角**：`Qwen35Line` 对 `ModelLine` 的完整实现 + 12 个内嵌单测 |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs)（416 行） | `launch` 的目的地：`Qwen35LaunchOptions`、两个引擎枚举、启动函数族的纵深校验 |
| [src/config/mod.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs)（51 行） | `probe_config_json`：probe 的第二段身份检查 |
| [src/batch_decode_graph.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14-L28) | `MAX_BATCH = 64` 与 CUDA Graph 桶 `[1,2,4,8,16,32,64]`——`--max-batch` 的物理背景 |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs)（988 行） | trait 定义、`SharedArgs` 共享旗标池、`ModelLineRegistry` 注册表、测试助手 `parse_for_line` |
| [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L213) | 服务器主流程：注册 → 解析 → 检测 → 校验 → 启动，全部是分派 |
| [pegainfer-kernels/src/ops/linear.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L224) | `GEMM_LT_MAX_N = 32`：`--decode-overlap=stream` 批次上限的源头 |
| [docs/models/qwen35/support-qwen38.md](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md) | Qwen3.8 共线的实测记录；`auto_tool_call_parser` 决策的依据 |

阅读建议：本讲以 `src/model_line.rs` 为主线精读；frontend 的 trait 文件只读模块头注释、trait 定义、`SharedArgs`、`parse_for_line` 四处；server 的 `main.rs` 读主流程一段即可。

## 4. 核心概念与源码讲解

### 4.1 ModelLine trait 与 probe：一条模型线如何被认领

#### 4.1.1 概念说明

**「模型线」（model line）= 一个模型家族的全部服务知识**：怎么识别它的 config.json、它有哪些专属 CLI 旗标、哪些旗标组合非法、怎么把 CLI 参数翻译成引擎启动调用。这些东西如果写在服务器里，服务器就会越来越懂每个模型，最终变成一团 `if qwen3 ... else if glm52 ...`。

PegaInfer 的解法是把这一切下沉到模型 crate：frontend 只定义 trait（契约），服务器只做分派。frontend 模块头注释把这条缝说得很直白——服务器「never names a model crate's option types」（从不点名任何模型 crate 的选项类型），接入一条新模型线只需实现 `ModelLine` 并在服务器注册表里加一行（见 [pegainfer-frontend/src/model_line.rs:1-12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L1-L12) 的模块文档）。

**probe 是认领动作**：服务器拿到模型目录的 `config.json`（解析成 `serde_json::Value`），挨个问每条编译进来的线「这是你的吗？」。恰好一条线接受 → 检测成功；两条都接受 → `DetectError::Conflict`；没人接受 → `DetectError::NoMatch`（服务器还会查一张 family 表，提示「这看起来像 X 模型，请用 `--features X` 重新构建」，见 [pegainfer-server/src/main.rs:123-144](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L123-L144)）。

#### 4.1.2 核心流程

服务器一次启动的分派序列（对照 [pegainfer-server/src/main.rs:146-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L186)）：

```text
1. ModelLineRegistry::new(model_lines())     构建注册表；构建期即校验旗标归属（重名/重拼写直接 panic）
2. registry.build_command(...)               共享旗标 + 每条线的专属旗标 → 一条完整命令（--help 不依赖检测结果）
3. 解析 argv                                 → matches + provided（显式提供的旗标集合）
4. 读 <model_path>/config.json               → serde_json::Value
5. registry.detect(&config)                  挨个 probe；NoMatch 时附加 feature 提示 → 得到唯一 line
6. registry.validate_provided(line, ...)     消费或拒绝：不归属的旗标报 UnconsumedFlag
   shared.validate(&provided)                共享旗标之间的通用规则
7. line.validate(&ctx, &provided)            本线跨旗标规则（4.3 详讲）
   line.serve_plan(&ctx)                     引擎加载前前端就要知道的事实（4.4 详讲）
8. line.launch(&ctx)（阻塞线程）              进入模型 crate 的启动链（4.4 详讲）
```

注意顺序：**检测先于一切校验**——必须先知道是哪条线，才知道该用哪套规则去校验。

#### 4.1.3 源码精读

**（1）trait 契约：七个方法，三个有默认实现**。[pegainfer-frontend/src/model_line.rs:337-383](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L337-L383)：

| 方法 | 作用 | Qwen35Line 是否覆盖 |
|---|---|---|
| `name()` | 家族名，出现在日志、错误信息、`--help` | ✅ 返回 `"Qwen3.5"`（[src/model_line.rs:75-77](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L75-L77)） |
| `probe(config)` | 按 config.json 认领或拒绝 | ✅（下文详讲） |
| `augment_cli(cmd)` | 追加本线专属旗标 | ✅（4.2 详讲） |
| `consumed_shared_args()` | 本线读取哪些共享旗标 | ✅（4.2 详讲） |
| `arg_requirements()` | 旗标间的无条件依赖 | ❌ 用默认空（依赖关系简单，靠 `validate` 表达） |
| `validate(ctx, provided)` | 跨旗标规则 | ✅（4.3 详讲） |
| `serve_plan(ctx)` / `launch(ctx)` | 服务计划 / 启动引擎 | ✅ / ✅（4.4 详讲） |

`validate` 与 `launch` 之间靠 [`LaunchContext`](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L273-L284) 传递四个字段：`model_path`（模型目录）、`config`（probe 已接受的同一份 config.json）、`shared`（共享旗标解析结果）、`matches`（完整命令的解析结果——线用它取回自己的专属旗标）。

**（2）probe 的两段式结构**。[src/model_line.rs:79-89](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L79-L89)：

```rust
fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
    let model_type = config.get("model_type").and_then(serde_json::Value::as_str);
    let text_model_type = config
        .get("text_config")
        .and_then(|text| text.get("model_type"))
        .and_then(serde_json::Value::as_str);
    if model_type != Some("qwen3_5") && text_model_type != Some("qwen3_5_text") {
        return Err(format!("model_type {model_type:?} is not \"qwen3_5\""));
    }
    crate::probe_config_json(config).map_err(|error| error.to_string())
}
```

- **第一段是「或」**：`model_type` 和 `text_config.model_type` 只要有一个像 Qwen3.5，就继续往下查；两个都不像才立刻拒绝（这条拒绝理由会进入 `NoMatch` 错误的 rejections 列表）。
- **第二段是「与」**：[src/config/mod.rs:24-51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/mod.rs#L24-L51) 的 `probe_config_json` 要求三件事**全部**成立——`model_type == "qwen3_5"`、`architectures` 包含 `"Qwen3_5ForConditionalGeneration"`、`text_config.model_type == "qwen3_5_text"`。它的文档注释明确划界：「身份检查归 probe，尺寸和形状校验归 config loader」——probe 只认门牌号，不验户型。

两段式的实际效果：一个 `model_type: "qwen3_5"` 但 architectures 写错的 config，第一段放行、第二段给出精确的「architectures must contain Qwen3_5ForConditionalGeneration」；一个完全外来的 config 则在第一段就拿到简洁的「not qwen3_5」。

**（3）Qwen3.8 为什么不需要新线**。实测记录（[docs/models/qwen35/support-qwen38.md:35](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L35)）：Qwen3.8-27B 的 config.json 报告的身份三元组与 Qwen3.5 **逐字段相同**（`model_type: qwen3_5` / `Qwen3_5ForConditionalGeneration` / `qwen3_5_text`），文本塔几何也一致——所以 probe、权重、内核、调度器、服务器接线全部原样通过。它真正的差异在加载器（bf16 标量）和聊天模板，与本讲的 CLI 层无关。**probe 认领的是身份，不是版本号**：对它来说 Qwen3.5 和 Qwen3.8 是同一个身份的两次落地。

**（4）内嵌测试已经钉住了 probe 行为**。[src/model_line.rs:218-228](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L218-L228) 的 `probe_accepts_qwen35_identity` 用一个三字段齐全的 JSON 断言接受——这就是「合法 config.json 长什么样」的可执行文档。

#### 4.1.4 代码实践

**实践：手工推演一次 detect，验证「恰好一条线认领」的必要性。**

1. **实践目标**：理解 detect 的冲突语义，以及为什么 probe 的错误信息会出现在用户面前。
2. **操作步骤**：
   - 读 [pegainfer-frontend/src/model_line.rs:569-601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L569-L601) 的 `detect`：注意它怎么收集 rejections、怎么在第二个认领者出现时返回 `Conflict`。
   - 读 [pegainfer-server/src/main.rs:168-175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168-L175)：服务器怎么把 `NoMatch` 转成带 feature 提示的错误。
   - 纸面构造三个 config.json：`(a)` `model_type: "qwen3"`；(b)`model_type: "qwen3_5", architectures: []`；(c) `model_type: "qwen3_5_text"`（顶层就写错的）。
3. **需要观察的现象**：对每个 config，推演出 detect 的最终结果与错误信息文本。
4. **预期结果**：(a) 第一段拒绝，`not "qwen3_5"`（若二进制编译了 qwen3 线则被那条线认领——这正是 `families()` 表里两行各自的身份）；(b) 第一段放行、第二段拒绝「architectures must contain ...」；(c) 第一段的条件是 `model_type != "qwen3_5" && text != "qwen3_5_text"`，顶层 `model_type` 为 `"qwen3_5_text"` 时两个条件都不满足「像 Qwen3.5」的判据……实际上 (c) 两个都不匹配所以第一段拒绝。若想验证，可对照 `probe` 的布尔条件逐项代入（待本地用单测验证你的推演）。

#### 4.1.5 小练习与答案

**练习 1**：一个 config.json 有 `model_type: "qwen3_5"` 但 `architectures: ["Qwen3NextForCausalLM"]`，probe 的结果是什么？

答案：第一段通过（`model_type` 匹配），第二段 `probe_config_json` 拒绝，错误信息为「Qwen3.5 architectures must contain Qwen3_5ForConditionalGeneration」。身份字段之间是「与」关系。

**练习 2**：detect 为什么要求**恰好**一条线认领？两条都认领会发生什么？

答案：认领的线决定后续一切：旗标归属校验用它的 `consumed_shared_args` 与专属旗标，校验规则用它的 `validate`，启动用它的 `launch`。两条线都认领时这套决策无法进行，所以 `detect` 返回 `DetectError::Conflict`，把两条线的名字都写进错误信息（frontend 有对应单测 `detect_conflict_names_both_lines`）。

**练习 3**：Qwen3.8-27B 放进 `--model-path`，服务器日志里 `line.name()` 会打出什么？为什么不是 "Qwen3.8"？

答案：打出 `Qwen3.5`。因为 Qwen3.8 的 config.json 身份三元组与 Qwen3.5 相同，被同一条 `Qwen35Line` 认领；`name()` 是线的名字，不是 checkpoint 的名字。

### 4.2 CLI 参数增补：augment_cli 与 consumed_shared_args

#### 4.2.1 概念说明

服务器的完整命令行由三层旗标拼成：

| 层 | 定义处 | 例子 | 归属校验 |
|---|---|---|---|
| 核心旗标 | frontend `CORE_ARGS`（[L68-74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L68-L74)） | `model_path`、`port`、`served_model_name`、`tool_call_parser` | 所有线都接受 |
| 共享旗标 | frontend `SharedArgs`（[L98-221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L98-L221)） | `tp_size`、`cuda_graph`、`decode_overlap`、`kv_offload`、… | 只有所在线**声明消费**的子集被接受 |
| 线专属旗标 | 各模型 crate 的 `augment_cli` | `--qwen35-prefix-cache-mib`、`--max-batch`、`--qwen35-scheduler-policy` | 本线专属 |

`SharedArgs` 是「多条线都可能用」的旗标池（例如 Qwen3 和 Qwen3.5 都支持 `--decode-overlap`，所以它放共享池）；每条线用 `consumed_shared_args` 声明自己读哪几个。**提供一条本线不消费的共享旗标不是「被忽略」，而是启动失败**——这就是消费或拒绝。

Qwen3.5 声明消费 7 个共享旗标（[src/model_line.rs:95-105](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L95-L105)）：`device_ordinal`、`tp_size`、`cuda_graph`、`no_prefix_cache`、`max_prefill_tokens`、`decode_overlap`、`decode_sm_pct`。共享池里的其余旗标（如 `kv_offload`、`dp_size`、`dump_graph_png`、`dflash_draft_model_path`）对 Qwen3.5 都是「不认识」。

还有一个值得注意的**类型边界**：CLI 侧的枚举（`CliQwen35SchedulerPolicy`）与引擎侧的枚举（[src/lib.rs:83-90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L83-L90) 的 `Qwen35SchedulerPolicy`）是两个类型，中间靠一个 `resolve()` 手工翻译。这让 clap 的 `ValueEnum` 不泄漏进引擎、引擎枚举不依赖 clap——两侧可以独立演化，翻译点是显式的、可审查的。

#### 4.2.2 核心流程

一个专属旗标的一生：

```text
注册：ModelLineRegistry::new → line.augment_cli(cmd) → Qwen35Cli::augment_args(cmd)
      （registry 同时登记每个旗标的 id 与拼写，冲突即 panic —— release 构建里
        clap 自己的 schema 断言被关掉，所以注册表亲自做这件事）
使用：build_command 合成完整命令 → 用户 argv 解析成 matches
取回：line.cli(ctx) → Qwen35Cli::from_arg_matches(ctx.matches)
      （augment_cli 注册过这些 id，解析已成功，所以 from_arg_matches 的
        Err 分支用 expect 直接终止 —— 不可能到达）
校验：validate / launch 里读 cli 字段（4.3、4.4）
```

#### 4.2.3 源码精读

**（1）三个专属旗标的定义**。[src/model_line.rs:24-39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L24-L39)：

```rust
#[derive(ClapArgs)]
struct Qwen35Cli {
    /// Per-rank GPU budget in MiB for joint prefix snapshots; zero disables reuse.
    #[arg(long, default_value_t = 0)]
    qwen35_prefix_cache_mib: usize,
    /// Decode-batch capacity, 1..=64. Qwen3.5 internally rounds allocation to
    /// the next graph bucket but admits only this many scheduler slots; defaults to 64.
    #[arg(long)]
    max_batch: Option<usize>,
    /// Qwen3.5 prefill/decode scheduler policy. Defaults to `off`; `auto` is
    /// opt-in and currently single-GPU only.
    #[arg(long, value_enum, default_value_t = CliQwen35SchedulerPolicy::Off)]
    qwen35_scheduler_policy: CliQwen35SchedulerPolicy,
}
```

三个设计点都藏在文档注释里：

- `qwen35_prefix_cache_mib` 默认 0 = 关闭前缀缓存；大于 0 是「每 rank 快照显存预算（MiB）」。它对应的 `--no-prefix-cache` 是**共享**旗标——两者矛盾时由 4.3 的规则 2 拒绝（u6 会讲缓存本身）。
- `max_batch` 是 `Option<usize>`：CLI 层不设默认值，缺省语义（= 64）在 validate 与 launch 两处统一用 `unwrap_or(crate::MAX_DECODE_BATCH)` 补齐。注释还预告了「调度槽 vs 物理桶」的分离：内部按桶 `[1,2,4,8,16,32,64]`（[src/batch_decode_graph.rs:14](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/batch_decode_graph.rs#L14)）向上取整分配，但调度器只准入 `max_batch` 个槽（u4-l3 详讲）。
- `qwen35_scheduler_policy` 用 `value_enum` 接受 `off | auto`，默认 `off`；`auto` 的单 GPU 限制同样由 4.3 的规则 6 把守。

**（2）CLI 枚举与引擎枚举之间的显式翻译**。[src/model_line.rs:41-58](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L41-L58)：`CliQwen35SchedulerPolicy::resolve()` 把 `Off/Auto` 逐一映射到 crate 的 `Qwen35SchedulerPolicy::Off/Auto`。同理 `resolve_decode_overlap`（[L64-72](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L64-L72)）把共享的 `CliDecodeOverlap`（`off | stream | green-ctx`）翻译成引擎的 `Qwen35DecodeOverlap`（`Off | SharedSm`），并把 Qwen3.5 不支持的 `green-ctx` 当场拒绝。

**（3）augment_cli 与取回**。[src/model_line.rs:60-62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L60-L62) 的 `cli(ctx)` 是 `validate` 和 `launch` 共用的取回入口：

```rust
fn cli(ctx: &LaunchContext<'_>) -> Qwen35Cli {
    Qwen35Cli::from_arg_matches(ctx.matches).expect("Qwen35Cli parses from the merged command")
}
```

`expect` 在这里不是冒险：`augment_cli` 已经把这些 id 注册进命令、整条 argv 已解析成功、注册表保证了 id 无冲突，所以 `from_arg_matches` 的错误分支实际不可达。

**（4）注册表在构建期就把旗标归属查死**。`ModelLineRegistry::new`（[pegainfer-frontend/src/model_line.rs:506-548](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L506-L548)）的注释点明动机：release 构建里 clap 等价的 schema 断言被禁用，所以注册表显式登记每个旗标 id 与拼写的归属，任何两条线（或线与共享池）撞 id、撞 `--spelling`、消费不存在的共享 id、声明悬空的依赖，都会在启动的第一行代码 panic——而不是等用户敲出某个旗标才炸。frontend 的内嵌测试用一组 StubLine 把这些 panic 场景全部钉住了。

**（5）消费或拒绝的执行点**。[pegainfer-frontend/src/model_line.rs:605-630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L605-L630) 的 `validate_provided` 逐个检查 provided 集合：核心旗标 ∨ 被消费的共享旗标 ∨ 本线专属旗标，三者都不是则返回 `CliError::UnconsumedFlag`，错误形如 `--kv-offload is not used by Qwen3.5`。

#### 4.2.4 代码实践

**实践：找出「Qwen3.5 不消费的共享旗标」并预测报错。**

1. **实践目标**：把消费或拒绝从概念变成可预言的行为。
2. **操作步骤**：
   - 对照 [SharedArgs 定义](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L98-L221)，列出全部共享旗标 id，划掉 Qwen3.5 消费的 7 个，剩下的就是「提供了即报错」清单（至少有 `kv_offload`、`kv_offload_host_gib`、`dp_size`、`dump_graph_png`、`dflash_draft_model_path`）。
   - 在 [src/model_line.rs:192-203](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L192-L203) 的 `validate_argv` 助手基础上，纸面推演 `["pegainfer", "--kv-offload"]` 会走到哪一步失败（提示：`parse_for_line` 内部就会调 `validate_provided`，早于 `line.validate`）。
3. **需要观察的现象**：失败发生在共享旗标校验层而不是 Qwen35 的 `validate` 层；错误信息是 `UnconsumedFlag` 的格式。
4. **预期结果**：报错文本为 `--kv-offload is not used by Qwen3.5`。可用一个临时单测验证（写法参考综合实践），待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `--decode-overlap` 放在共享池而 `--qwen35-scheduler-policy` 是专属旗标？

答案：多条线支持同一语义的重叠模式（Qwen3 支持 `off|stream|green-ctx`，Qwen3.5 支持 `off|stream`），共享池让它们的拼写与帮助文本统一、由各线的 `resolve`/`validate` 决定接受哪些值；而调度策略目前只有 Qwen3.5 有，专属旗标避免污染其他线的命令面。

**练习 2**：`--max-batch 5` 会被接受吗？物理上分配多大的批次？

答案：被接受（1..=64 内，内嵌测试 `accepts_non_bucket_scheduler_max_batch` 钉住）。物理分配向上取整到桶 8，但调度器只准入 5 个槽——「调度槽数」与「物理桶容量」是两个量，这正是旗标文档注释里写明的语义（#470 引入的分离）。

**练习 3**：如果引擎侧给 `Qwen35SchedulerPolicy` 新增一个变体 `Paced`，CLI 会自动出现 `--qwen35-scheduler-policy paced` 吗？

答案：不会。CLI 枚举是独立的 `CliQwen35SchedulerPolicy`，`resolve()` 是唯一翻译点；引擎加变体后必须显式改 CLI 枚举与 `resolve`，否则新策略无法从命令行到达。这个「不自动」正是类型边界的价值：CLI 契约的每次变化都是显式的。

### 4.3 validate：跨旗标规则组成的校验链

#### 4.3.1 概念说明

三层校验各管一段：**clap 管格式**（值能不能解析成 `usize`）、**注册表管归属**（旗标是不是这条线的）、**`validate` 管语义组合**（每个旗标单看都合法，合在一起是否矛盾）。第三层就是 [src/model_line.rs:107-149](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L107-L149) 的 `ModelLine::validate`。

Qwen3.5 的 `validate` 是一条**有序规则链**：六条检查按固定顺序执行，第一条命中即返回。顺序本身就是信息——它决定了用户看到哪条错误信息。注意签名里的 `_provided`（未使用）：Qwen3.5 的规则读的是旗标**值**而非「是否提供」，基于提供与否的规则放在共享层（如 `SharedArgs::validate` 要求 `--decode-sm-pct` 只能配合 `green-ctx` 出现，[frontend L248-252](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L248-L252)）。

#### 4.3.2 核心流程

`validate` 的决策树（条件按代码顺序）：

```text
输入: cli（Qwen35Cli） + ctx.shared（共享旗标）

R1  resolve_decode_overlap:
      green-ctx           → 拒绝 "supports --decode-overlap=off|stream only"
      off / stream        → Off / SharedSm
R2  qwen35_prefix_cache_mib > 0 且 no_prefix_cache
                          → 拒绝 "contradictory"
R3  max_batch（若提供）∉ 1..=64（MAX_DECODE_BATCH）
                          → 拒绝 "must be in 1..=64"
R4  overlap ≠ Off 且 max_batch（缺省补 64）> 32（MAX_SHARED_SM_DECODE_BATCH）
                          → 拒绝 "requires --max-batch <= 32"
R5  tp_size > 1 且 overlap ≠ Off
                          → 拒绝 "single-GPU only"
R6  tp_size > 1 且 policy = Auto
                          → 拒绝 "single-GPU only"（TP 固定 off 策略）
全部通过 → Ok(())
```

每条规则都有内嵌测试钉住：R2 ← `rejects_contradictory_prefix_cache_flags`、R3 ← `rejects_zero_scheduler_max_batch`、R4 ← `rejects_stream_overlap_default_max_batch` 与 `accepts_shared_stream_overlap`（边界 32 恰好通过）、R1 ← `rejects_green_context_overlap`、R5/R6 ← `accepts_tp_size` / `rejects_tp_auto_scheduler_policy`。

#### 4.3.3 源码精读

**（1）R4 为什么是 32**。上限常量在 [src/lib.rs:44-51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L44-L51)：

```rust
/// Current safe Qwen3.5 Shared-SM overlap admission cap.
///
/// Decode buckets above this value fall back to the prefill cuBLAS handle today.
/// Replaying decode while async prefill concurrently calls that same handle from
/// another stream relies on undocumented single-handle multi-stream behavior.
pub const MAX_SHARED_SM_DECODE_BATCH: usize = ops::GEMM_LT_MAX_N;
```

即 `GEMM_LT_MAX_N = 32`（[pegainfer-kernels/src/ops/linear.rs:224](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kernels/src/ops/linear.rs#L224)）。语义是：`stream` 重叠模式让 decode 在模型流重放图、prefill 在第二条流跑 chunk；大于 32 的解码桶今天仍会回落到 prefill 的 cuBLAS handle，「另一条流并发调用同一个 handle」依赖 cuBLAS 未公开的多流行为——所以在更大的桶拥有独立的 per-stream handle 之前，重叠模式被限制在 ≤ 32（u4-l4 详讲重叠本身）。

**（2）R4 的缺省补齐**。[src/model_line.rs:127-135](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L127-L135)：`let max_batch = cli.max_batch.unwrap_or(crate::MAX_DECODE_BATCH);`——用户没写 `--max-batch` 时按 64 校验。所以 `--decode-overlap stream` 单独出现必被 R4 拒绝（64 > 32），必须显式配 `--max-batch 32` 或更小。这不是缺陷而是刻意的保守：默认配置不允许悄悄进入「未公开 cuBLAS 行为」的区域。

**（3）规则顺序的可见后果**。`--tp-size 2 --decode-overlap stream`（不带 `--max-batch`）报的是 R4 的「requires --max-batch <= 32」而不是 R5 的「single-GPU only」——因为 R4 先执行且缺省 64 已违规。想看到 R5 的信息需要再配 `--max-batch 16`。读错误信息定位规则时要意识到这一点。

**（4）一个跨层死角的推理：`--decode-sm-pct` 与 Qwen3.5**。Qwen3.5 把 `decode_sm_pct` 列入 consumed（R5 之前它是 green-ctx 专属参数），但：提供它且 overlap 非 green-ctx → 共享层 `SharedArgs::validate` 拒绝（「only applies with green-ctx」）；提供它且 green-ctx → R1 拒绝（Qwen3.5 不支持 green-ctx）。两条路都死，`--decode-sm-pct` 实际无法与 Qwen3.5 一起使用。把它列入 consumed 的收益是错误信息更贴切（「参数只属于 green-ctx」而非干巴巴的「not used by Qwen3.5」）——归属声明也承担「解释」职责。

**（5）测试助手 `validate_argv` 为什么可信**。[src/model_line.rs:192-203](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L192-L203) 调用的 `parse_for_line`（[frontend L668-681](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L668-L681)）内部走的是与服务器完全相同的路径：单线注册表 → `build_command` → 解析 → `validate_provided` → `shared.validate`，最后才补上 `line.validate`。因此内嵌单测验证的就是生产分派链本身，不是简化替身。

#### 4.3.4 代码实践

**实践：纸面推演一条 argv 过六关。**

1. **实践目标**：不运行代码，准确预测 `validate` 对给定 argv 的判定。
2. **操作步骤**：对下面五条 argv，按 4.3.2 的决策树逐条执行，写出命中的规则编号与错误信息关键词：
   - `["pegainfer", "--qwen35-prefix-cache-mib", "256"]`
   - `["pegainfer", "--max-batch", "65"]`
   - `["pegainfer", "--decode-overlap", "stream", "--max-batch", "16", "--tp-size", "2", "--cuda-graph=false"]`
   - `["pegainfer", "--decode-sm-pct", "20"]`
   - `["pegainfer", "--decode-overlap", "stream", "--max-batch", "32", "--qwen35-scheduler-policy", "auto"]`
3. **需要观察的现象**：每条 argv 停在哪条规则；注意第三条会先过 R4（16 ≤ 32）再被 R5 拦下；第四条停在共享层而非 `validate`。
4. **预期结果**：依次为：全部通过（R1–R6 无一命中，`no_prefix_cache` 未提供）；R3（65 > 64）；R5（TP + overlap；注意 `--cuda-graph=false` 在 validate 层不参与任何规则——TP 下的 eager 启动是合法组合，内嵌测试 `accepts_tp_size` 恰用此参数组合；「拒绝 `--cuda-graph=false`」只发生在单 GPU 分支的启动链，即 u1-l3 4.1 见过的那条 `ensure!`）；共享层拒绝（decode_sm_pct 非 green-ctx）；全部通过（auto 策略 + stream + 32 批次是合法组合，内嵌测试 `accepts_auto_policy_with_overlap` 钉住）。第五条恰说明 R6 只限制「TP + auto」，不限制「overlap + auto」。推演结果待本地用单测验证（写法见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：`--decode-overlap stream`（不带 `--max-batch`）为什么被拒绝？

答案：R4 先把缺省 `max_batch` 补成 `MAX_DECODE_BATCH = 64`，64 > `MAX_SHARED_SM_DECODE_BATCH = 32`，触发「requires --max-batch <= 32; got 64」。内嵌测试 `rejects_stream_overlap_default_max_batch` 断言错误信息同时含 `max-batch` 和 `32`。

**练习 2**：把 R4 和 R5 的顺序对调，用户可见的行为会变吗？

答案：会。`--tp-size 2 --decode-overlap stream`（无 `--max-batch`）现在报 R5 的「single-GPU only」而不是「requires --max-batch <= 32」。两条信息都「对」，但引导用户的修复方向不同——规则顺序是产品决策，不只是实现细节。

**练习 3**：`--max-batch 33`（不带 overlap）合法吗？`33` 不是桶值会有问题吗？

答案：合法。R3 只要求 1..=64；桶值约束不存在于 CLI 层——物理分配向上取整到桶 64，调度器准入 33 个槽（对照 `accepts_non_bucket_scheduler_max_batch` 接受 5 的先例）。

### 4.4 serve_plan 与 launch：从 CLI 到引擎启动链

#### 4.4.1 概念说明

两个收尾方法回答两个不同的问题：

- **`serve_plan`：引擎还没加载，前端就需要知道什么？** 服务器要在引擎加载完成前注册 HTTP 身份、决定路由（LoRA、prefill-only、分区数）、解析工具调用语法——这些都只能从 CLI 派生。`ServePlan`（[frontend L293-334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L293-L334)）有四个字段，Qwen3.5 只设置其中一个：`auto_tool_call_parser`。
- **`launch`：把 CLI 世界翻译成引擎世界。** `LaunchContext` 里的字符串与旗标在这里变成 `Qwen35LaunchOptions`（[src/lib.rs:121-133](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L121-L133)）加上两个引擎枚举，进入启动函数族。同时，CLI 层校验过的规则在库边界**再校验一遍**——纵深防御，因为库函数也能被测试和其他 crate 直接调用，绕过 CLI。

#### 4.4.2 核心流程

`launch` 之后的完整调用链（单 GPU 与 TP 在最深处分叉，u1-l3 的 4.1 已见过这个分叉）：

```text
ModelLine::launch                                  [model_line.rs:163-183]
  ├── 组装 Qwen35LaunchOptions（max_batch / max_prefill_tokens 缺省补齐）
  ├── resolve_decode_overlap（第二次翻译；green-ctx 在此转成 anyhow 错误）
  └── launch_with_options_policy_and_overlap       [lib.rs:166-188]
        ├── device_ordinals()  校验 tp_size >= 1；tp=1 → [device_ordinal]，否则 0..tp_size
        ├── 组装 EngineLoadOptions（seed=42、EpBackend::Nccl、设备表）
        └── start_engine_with_capacity_policy_and_overlap   [lib.rs:225-308]
              ├── 再校验 max_batch ∈ 1..=64            （与 R3 同款）
              ├── 再校验 SharedSm ⇒ max_batch <= 32     （与 R4 同款）
              ├── prefix_cache_mib × 1MiB 溢出检查
              ├── 多设备（TP）分支:
              │     拒绝 overlap / auto 策略 → scheduler::start_tp_with_capacity
              └── 单 GPU 分支:
                    强制 enable_cuda_graph（"Qwen3.5 decode always captures CUDA Graphs"）
                    → weights::Qwen35Model::from_safetensors（加载权重）
                    → scheduler::start_with_capacity_and_policy
```

#### 4.4.3 源码精读

**（1）serve_plan：一条注释里藏着一个线上 bug**。[src/model_line.rs:151-161](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L151-L161)：

```rust
fn serve_plan(&self, _ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> {
    Ok(ServePlan {
        // Both generations' templates request the Qwen Coder tool syntax,
        // but upstream `Auto` matches the model *path* by substring: a
        // `Qwen3.8-27B` directory resolves to the JSON `qwen3_json` parser
        // (`docs/models/qwen35/support-qwen38.md`). The line knows better
        // than the directory name.
        auto_tool_call_parser: Some(ParserSelection::Explicit("qwen3_coder".to_string())),
        ..Default::default()
    })
}
```

完整因果链（实测记录见 [docs/models/qwen35/support-qwen38.md:180-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/docs/models/qwen35/support-qwen38.md#L180-L186)）：

1. Qwen3.5 和 Qwen3.8 两代模型的聊天模板都用 **Qwen Coder 工具调用语法**。
2. 但 `--tool-call-parser auto` 的上游实现是按**模型路径子串**匹配：路径含 `qwen3.5` 才选 `qwen3_coder`，只含 `qwen3` 的路径落到 `qwen3_json`。
3. 一个名为 `Qwen3.8-27B` 的目录因此被解析成 `qwen3_json`——工具调用块不被解析，原样落进 `content` 字段（实测观察到的错误行为）。
4. 修复：模型线在 `ServePlan` 里自己声明 `auto → qwen3_coder`，「线比目录名更了解自己」。用户**显式**指定的 parser（包括 `none`）永远优先——[frontend L311-323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L311-L323) 的 `resolve_tool_call_parser` 只在请求为 `Auto` 时才回退到线的默认。

这一行为被 [src/model_line.rs:329-352](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L329-L352) 的 `auto_tool_call_parser_resolves_to_the_coder_parser` 双向钉住：`Auto` 必须变成 `qwen3_coder`，显式 `qwen3_xml` 必须原样存活。

**（2）launch 的参数翻译**。[src/model_line.rs:163-183](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L163-L183)：三个输入源在此汇合——专属旗标（`qwen35_prefix_cache_mib`、`max_batch`）、消费的共享旗标（`device_ordinal`、`tp_size`、`cuda_graph`、`max_prefill_tokens`）、两个 `resolve` 的产物（策略与重叠模式）。缺省补齐点：`max_batch.unwrap_or(MAX_DECODE_BATCH)`、`max_prefill_tokens.unwrap_or(DEFAULT_MAX_PREFILL_TOKENS)`。最后 `.map(LaunchedEngine::Stepped)` 把引擎包进 frontend 契约的枚举——Qwen3.5 返回的是 step 驱动型引擎。

**（3）库边界的纵深防御**。[src/lib.rs:234-249](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L234-L249) 把 R3、R4 原样再查一遍；[L253-262](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L253-L262) 把 R5、R6 再查一遍。这不是冗余：`start_engine*` 函数族是公开库 API，测试（如 [lib.rs:345-415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/lib.rs#L345-L415) 的三个 `*_before_loading_model` 测试）和外部 crate 都能绕过 CLI 直接调用。两层校验的分工：**CLI 层给出用户友好的快失败（连引擎都还没开始构建），库层是不变量的权威边界**。lib 测试名里的 `before_loading_model` 强调这些检查发生在任何权重加载之前——非法参数不该浪费一次显存初始化。

**（4）`ServePlan` 为什么用 `..Default::default()`**。frontend 的 [ServePlan 文档](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L293-L309) 解释了原因：CI 会编译多条模型线，任何 crate 里写全字段的花括号字面量都会在 frontend 给 `ServePlan` 加字段的那天悄悄过时——只拼自己设置的字段，新增字段自然落入默认值。

#### 4.4.4 代码实践

**实践：写出 launch 调用链的函数序列并标注校验点。**

1. **实践目标**：把 4.4.2 的流程图与真实代码逐行对上，确认每一步的存在。
2. **操作步骤**：
   - 从 [src/model_line.rs:163](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L163) 出发，依次打开 `launch_with_options_policy_and_overlap`（lib.rs L166）、`start_engine_with_capacity_policy_and_overlap`（lib.rs L225），抄下每个 `anyhow::ensure!` 的行号与条件。
   - 数一数：同一条「SharedSm ⇒ max_batch ≤ 32」规则在几个文件几个位置出现？（model_line.rs 的 R4、lib.rs 的 L244-249，共两处。）
3. **需要观察的现象**：CLI 层与库层的错误信息措辞差异（前者面向用户、含 `--max-batch` 旗标名；后者面向库调用者、解释 cuBLAS handle 的技术原因）。
4. **预期结果**：得到一张「规则 × 检查点 × 错误措辞」的对照表；能回答「为什么两处都要有」。纯阅读任务，结论可直接自源码确认，无需运行。

#### 4.4.5 小练习与答案

**练习 1**：用户显式传 `--tool-call-parser qwen3_xml`，最终生效的是哪个 parser？

答案：`qwen3_xml`。`resolve_tool_call_parser` 对非 `Auto` 的请求原样返回，线的默认只接管 `Auto`。内嵌测试显式断言了「an explicit name is the caller's to make」。

**练习 2**：为什么 `serve_plan` 要在引擎加载**之前**被调用？举个它包含的事实的例子。

答案：因为 HTTP 前端要在引擎还在加载时就开始装配（注册引擎身份、按 `max_model_len` 建路由），这些决定不能等权重上完。例子：`scheduler_partition_count`（每分区一个对外引擎身份）必须从 CLI 派生并在加载后与引擎实测值核对；Qwen3.5 设置的 `auto_tool_call_parser` 则决定 `/v1/completions` 的工具语法解析器。

**练习 3**：`launch` 里 `resolve_decode_overlap` 的错误为什么要再 `map_err` 一次转成 `anyhow`？

答案：`resolve_decode_overlap` 返回 frontend 的 `CliError`（供 `validate` 使用），而 `launch` 的签名返回 `anyhow::Result<LaunchedEngine>`——trait 约定 launch 阶段的失败是「深上下文链」（CUDA、权重、拓扑），调用方不按类型分派，所以统一转成 anyhow。同一个函数被两个错误体系消费，边界处翻译。

## 5. 综合实践

**任务：新增单测 `rejects_stream_overlap_explicit_oversized_max_batch`，钉住 R4 的「显式超大批次」分支。**

现有测试覆盖了 R4 的两个端点：缺省批次被拒（`rejects_stream_overlap_default_max_batch`，64 > 32）与边界恰好通过（`accepts_shared_stream_overlap`，32 ≤ 32）。**尚未覆盖**的是「用户显式传了一个 33..=64 之间的值」——这个分支能证明错误信息里的 `got N` 报的是用户显式提供的值而非缺省值。

### 实践目标

为 R4 补上第三个数据点，同时完整走一遍「预测 → 写测试 → 运行 → 红绿验证」的流程。

### 操作步骤

**第 1 步：先预测。** 不看测试结果，填完这张表（答案在 4.3 已全部推演过）：

| 命令行（省略 `pegainfer` 前缀） | 判定 | 命中规则 |
|---|---|---|
| `--decode-overlap stream --max-batch 40` | ？ | ？ |
| `--decode-overlap stream --max-batch 32` | 通过 | R4 边界 |
| `--decode-overlap stream` | 拒绝 | R4（缺省补 64） |
| `--decode-overlap green-ctx` | 拒绝 | R1 |
| `--max-batch 0` | 拒绝 | R3 |
| `--tp-size 2 --decode-overlap stream --max-batch 16` | 拒绝 | R5 |

**第 2 步：写测试。** 打开 [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L262-L272) 的 tests 模块，在 `rejects_stream_overlap_default_max_batch` 之后插入（**示例代码**，风格对照现有测试）：

```rust
#[test]
fn rejects_stream_overlap_explicit_oversized_max_batch() {
    let error = validate_argv(&[
        "pegainfer",
        "--decode-overlap",
        "stream",
        "--max-batch",
        "40",
    ])
    .expect_err("Qwen3.5 should reject stream overlap with an explicit oversized max-batch");
    let message = error.to_string();
    assert!(message.contains("max-batch"), "{message}");
    assert!(
        message.contains(&crate::MAX_SHARED_SM_DECODE_BATCH.to_string()),
        "{message}"
    );
}
```

选 `40` 的理由：它在 1..=64 内（先通过 R3，证明后续命中的确是 R4）、不是桶值（顺带覆盖「非桶调度批次」的输入形态）、且错误信息 `... requires --max-batch <= 32; got 40` 里的 `got 40` 能区分「显式值」与「缺省值」两个分支。如果想更进一步，可以再断言 `message.contains("40")`。

**第 3 步：运行。**

```bash
cargo test --release -p pegainfer-qwen35 --features qwen35 --lib
# 或只跑本模块：
cargo test --release -p pegainfer-qwen35 --features qwen35 --lib -- model_line
```

**第 4 步（可选）：红绿验证。** 把 `40` 临时改成 `32` 重跑——测试应当失败（`accepts_shared_stream_overlap` 早已证明 32 合法），证明断言真的在检查重叠上限而不是「碰巧通过」。改回 `40` 恢复绿色。

**第 5 步（可选扩展）**：按任务说明的替代方案，仿照 [rejects_green_context_overlap](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L275-L279) 自己复写一个变体（例如同时断言信息含 `off|stream` 与 `Qwen3.5`），体会 R1 的错误措辞。

### 需要观察的现象

- 新测试与既有 12 个 model_line 测试全部通过。
- 测试是纯主机侧逻辑：不加载权重、不初始化 CUDA（`validate_argv` 只做参数解析与规则判定）。
- 但**构建**仍需 `--features qwen35` 拉起的 Triton AOT 工具链（u1-l2 讲过的构建期 Python 依赖）。

### 预期结果

第 1 步表格第一行的答案：**拒绝，R4**（40 > 32，错误信息含 `max-batch`、`32`、`got 40`）。测试运行通过即证实预测。本环境未执行上述命令（构建需要 Triton AOT 工具链），**待本地验证**；预测本身可由 [src/model_line.rs:119-135](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L119-L135) 的代码直接推得，不依赖运行。

## 6. 本讲小结

- **ModelLine 是缝，服务器是纯分派**：trait 把「识别 config、专属旗标、跨旗标规则、服务计划、启动」全部下沉到模型 crate（[src/model_line.rs:74-184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/model_line.rs#L74-L184)）；服务器二进制只持有 `Vec<&'static dyn ModelLine>` 做注册、检测、校验、启动四步转发。
- **probe 认身份不认版本**：两段式（外层 `model_type`/`text_config.model_type` 之「或」+ 内层三字段之「与」），Qwen3.8 因身份三元组与 Qwen3.5 逐字段相同而被同一条线认领——「共用一条线」在代码上就是 probe 的四个字符串常量。
- **旗标三层归属 + 消费或拒绝**：`--qwen35-prefix-cache-mib`、`--max-batch`、`--qwen35-scheduler-policy` 专属，7 个共享旗标被声明消费，其余共享旗标提供即报 `--xxx is not used by Qwen3.5`；注册表在构建期用 panic 查死 id/拼写冲突。
- **validate 是有序规则链**：六条检查（green-ctx 不支持、前缀缓存矛盾、批次 1..=64、重叠批次 ≤ 32、TP 拒重叠、TP 拒 auto）顺序执行、首错即返；32 的上限来自 cuBLAS 单 handle 多流的未公开行为风险（`GEMM_LT_MAX_N`）。
- **serve_plan 修过一个真实 bug**：上游 `auto` 按路径子串选 parser，`Qwen3.8-27B` 目录会落到 `qwen3_json` 导致工具调用不被解析；线声明 `auto → qwen3_coder`，显式选择仍然优先。
- **纵深防御**：同一批规则在 CLI 的 `validate` 与库的 `start_engine_with_capacity_policy_and_overlap` 各查一遍——前者面向用户快失败，后者是不变量的权威边界（库可被绕过 CLI 直接调用）；内嵌测试经 `parse_for_line` 走的正是生产分派链。

## 7. 下一步学习建议

入门单元到此完成：你已经知道这条线是什么（u1-l1）、怎么构建（u1-l2）、代码怎么组织（u1-l3）、怎么被服务器接入（本讲）。下一讲 **u2-l1「config.json 的解析与校验」**顺着 probe 的第二段往里走：精读 [src/config/model.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen35/src/config/model.rs)，看 `RawConfig` 如何经 `TryFrom` 一次性校验成合法的 `Config35`——本讲 probe 只验了门牌号，u2 开始验户型。

进入 u2 前的两个轻量准备：

1. 通读 [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs) 的 frontend 侧内嵌测试（StubLine 家族那一段）——你会看到「注册表用 panic 当断言」的完整测试面，与本讲 4.2 的归属机制互为印证。
2. 完成 4.3.4 的推演表后，把 5 的表格留着：u5-l1 讲调度器时我们会回到 `max_batch` / `max_prefill_tokens` 这两个值真正被谁消费，届时你会看到 CLI 层这两个数字在调度器里的最终形态。
