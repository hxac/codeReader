# u5-l3 · 设计取舍复盘：纯分发二进制与契约迁移

## 1. 本讲目标

本讲是手册的收官篇，不再引入新的代码路径，而是把前四个单元走过的代码拉远，复盘支撑整个 `pegainfer-server` 的**三个架构决定**：

1. **server 只做纯分发**——一个约 300 行的二进制，不 import 任何模型 crate 的选项类型，新增模型线对 server 代码的改动只是注册表里的一行；
2. **`families()` 作为「必要的第二真相源」**——一份硬编码的身份表，为「被编译掉的模型线」补上检测提示，能力边界是 identity-only；
3. **两代引擎契约共存**——`LaunchedEngine` 枚举是带说明书的迁移脚手架，它写明了自己的删除条件。

学完本讲，你应该能：

- 论证纯分发二进制的收益清单与代价清单（收益不只是「少写代码」，还有编译期裁剪与错误语言的一致性；代价包括 `families()` 的重复与「trait 未表达的事 server 做不了」）；
- 界定 `feature_gate_hint` 的能力边界：它无法运行被编译掉的 probe，量化变体、绑词嵌入（tied embeddings）、YaRN 绳长外推这类「probe 会拒绝的变体」也会命中提示——提示只承诺身份归属，不承诺 checkpoint 可服务；
- 给出两代契约合并的判据：哪些模型线还压在 legacy 一侧、`KvPrefix` 为什么挡住删除、删除后 `main.rs` 与 `vllm/mod.rs` 里哪些分支会消失，以及 `ServePlan::default` 展开约定（CI 只编译三条线）为什么是契约演化的防漂移手段。

## 2. 前置知识

本讲建立在 u2-l3（注册表两本账）、u3-l1（两代契约地图）、u4-l1（serve 装配骨架）之上。在此之上补四个工程决策层面的概念：

- **真相源（source of truth）**：一份信息在仓库里被权威定义的位置。好的设计尽量只有一个真相源；当第二个真相源**不可避免**时（比如 `families()`），好的做法是承认它、写清它为什么必要、再用测试把两份真相**绑定**起来，让漂移（drift，两份拷贝各自演化后不再一致）在提交前被抓住。
- **identity-only（仅身份）**：检测分两层——**身份**（`model_type`、`architectures`，回答「这是哪个家族」）与**结构**（尺寸、量化、滑窗、RoPE 外推，回答「这个 checkpoint 我能不能服务」）。`families()` 表与多数 probe 只看身份；部分 probe（gemma4、kimi-k2）做结构校验。提示只建在身份层。
- **迁移脚手架（scaffolding）**：为平滑迁移而临时存在的结构。`LaunchedEngine` 枚举的文档注释里明确写着它将来会被删除——「会死的设计」也被当作设计的一部分来文档化，这是大型重构里非常值得学习的习惯。
- **编译可见性**：Rust 的 `#[cfg]` 让「代码没被编译」成为一等事实。CI 只编译部分 feature 组合时，**只有被编译的代码受编译器保护**；契约类型的演化（加字段、删变体）对未编译的 crate 是无声的。`ServePlan::default` 展开约定就是针对这一点的纪律。

再回顾三个贯穿本讲的旧结论：注册表构造期对 flag 的 id 与拼写做唯一性 panic（u2-l3）；`LaunchedEngine` 是迁移期 Handle/Stepped 二选一枚举，LoRA 分支只认 `Stepped`（u3-l1）；serve 函数族以「引擎 Future」为参数实现前端与引擎加载并发（u4-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `src/main.rs` | server 二进制：纯分发 | 模块文档、`model_lines()`、`families()`、`feature_gate_hint`、main 的三分支 |
| `Cargo.toml` | server 的依赖与 feature 表 | optional 依赖如何把「模型知识」挡在二进制之外 |
| `../pegainfer-frontend/src/model_line.rs` | 接缝：trait + 注册表 + SharedArgs | 模块文档的「No other server edits」、注册表自检、`ServePlan::default` 约定 |
| `../pegainfer-frontend/src/engine/mod.rs` | 引擎契约的两代分组地图 | 迁移边界的权威注释、扁平 re-export |
| `../pegainfer-frontend/src/engine/wiring.rs`（辅读） | `LaunchedEngine` 定义地 | 枚举文档的删除条件、两个 `From` 实现 |
| `../pegainfer-frontend/src/vllm/mod.rs`（辅读） | serve 装配骨架 | `engine_task` 对两变体各铸一桥的分派 |
| `../README.md` | 项目门面 | 架构边界表、「两代契约共存」的官方表述 |
| `../pegainfer-qwen3/src/config.rs`（辅读） | qwen3 的 probe | identity-only probe 的实例 |
| `../pegainfer-gemma4/src/probe.rs`、`../pegainfer-kimi-k2/src/config.rs`（辅读） | 结构校验型 probe | 「probe 会拒绝的变体」长什么样 |
| `../docs/subsystems/frontend/frontend-architecture.md`（辅读） | 前端架构文档 | 迁移顺序与删除清单的官方记录 |

> 提醒：本讲全部实践都是**源码阅读型与 grep 型**，不触达 CUDA，也不修改源码。

## 4. 核心概念与源码讲解

### 4.1 server 纯分发设计：把「模型的多样性」关进 trait 对象

#### 4.1.1 概念说明

「纯分发（pure dispatch）」指 server 二进制对模型世界的全部知识 = 一列 `&'static dyn ModelLine` trait 对象。它做的事只有四件：**检测模型（detect）→ 校验 CLI（三级 validate）→ 启动引擎（launch）→ 分发服务（serve）**。任何模型特有的 flag、规则、选项类型都住在模型 crate 里，server 从不点名。

这样设计的收益：

- **新增模型线的改动面极小**：注册向量加一行 + `families()` 加一行 + 一个 Cargo feature。`main()` 本体、serve 装配、校验链一行不动。
- **编译期裁剪**：feature 未开启时整个模型 crate 不参与编译。出厂二进制（default = `qwen3`）里**物理上不存在**其他线的代码——这是 `#[cfg]` 给的免费瘦身，不需要任何运行时插件机制。
- **错误语言一致**：`launch` 失败是 `anyhow` 深层上下文链，server 只加一层上下文原样上抛（不分支、不翻译），于是所有模型线的启动错误对用户呈现同一种形态。
- **可测性**：注册表与 CLI 逻辑全部在 frontend crate，用 `StubLine` 就能测（u2-l3）；server 侧只需一个驱动真实二进制的 `detect_gate` 集成测试（u1-l4、u5-l2）。

代价同样真实，且后两个模块分别展开其中两条：

- **`families()` 重复**：被编译掉的线连 probe 都不在二进制里，身份表只能硬编码（→ 4.2）。
- **trait 表达力即 server 能力上限**：server 想在新阶段拿到新事实，就必须先在契约上加方法——`ServePlan` 正是这么长出来的（u2-l5）。
- **放弃穷尽匹配**：用 `dyn` trait 对象而不是枚举，意味着编译器不再强迫你「每加一条线就更新所有分派点」——注册表必须用**自建的两本账**（u2-l3）补回这份检查。

#### 4.1.2 核心流程

先看依赖方向（frontend 架构文档的原话是 "thin bin"）：

```text
pegainfer-frontend  ←  pegainfer-core  ←  各模型 crate  ←  pegainfer-server（薄二进制）
     │                                                        │
     │ 定义：ModelLine trait、SharedArgs、LaunchedEngine、        │ 只持有
     │       ModelLineRegistry、serve 函数族                     │ &'static dyn ModelLine
     └──────────────── 契约类型全部由 frontend 拥有 ──────────────┘
```

main() 的装配线（u1-l3 详走过）在本讲视角下重读一遍——注意每一步都只经过 trait 或 frontend 类型：

```text
ModelLineRegistry::new(model_lines())     # 唯一「点名」模型 crate 的地方（cfg 守卫）
  → registry.build_command(...)            # 共享 flag + 各线独占 flag 合并
  → 读 config.json → registry.detect       # 经 probe 认领，拿到 &dyn ModelLine
  → validate_provided / shared.validate / line.validate   # 全部经 trait
  → line.serve_plan(&ctx)                  # 经 trait 拿服务形态
  → spawn_blocking(line.launch(&ctx))      # 经 trait 拿 LaunchedEngine
  → 按 plan 字段选 serve 分支               # 分派依据是 ServePlan，不是模型名！
```

关键观察：**main 的三个 serve 分支（LoRA / prefill-only / 默认）分派的依据是 `plan.lora_modules`、`plan.prefill_only`、`plan.scheduler_partition_count` 这三个「服务形态事实」，而不是「这是 Qwen3 还是 GLM」**。模型身份在 detect 之后就退役了，后续全部用抽象事实驱动——这就是纯分发能保持 300 行的原因。

#### 4.1.3 源码精读

**证据一：模块文档自我声明。** [src/main.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L1-L4) 开宗明义："pure dispatch over the compiled-in `ModelLine`s. Every model-specific flag, rule, and option type lives in its model crate; this file only wires detection, CLI validation, and the serve path selection together."——这段注释就是本模块的「设计合同」。

**证据二：use 清单里没有任何模型 crate 的类型。** [src/main.rs:L6-L18](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L6-L18) 的全部导入是：`std::time::Instant`、`anyhow`、`clap::FromArgMatches`、`log`、`pegainfer_core::logging`、`pegainfer_frontend::engine::LaunchedEngine`、以及 `pegainfer_frontend::model_line` 下的六个契约项。**唯一出现的「模型知识」是 `SharedArgs`——而它定义在 frontend，不属于任何模型 crate**（这正是综合实践第 1 问的伏笔）。

**证据三：`model_lines()` 是模型 crate 被点名的唯一位置。** [src/main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L24-L43) 每行一个 `#[cfg(feature = ...)]` 守卫的 `&<crate>::model_line::MODEL_LINE`。feature 关掉，这一行连同 crate 一起从编译中消失。配合 [Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/Cargo.toml#L34-L48) 的 feature 表（每个 feature = 一个 `optional` 依赖，见 [Cargo.toml:L18-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/Cargo.toml#L18-L26)），「注册一行」与「编译一个 crate」被绑成同一个开关。

**证据四：接缝文档承诺「No other server edits」。** [../pegainfer-frontend/src/model_line.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L1-L12) 写明接缝的设计意图："The server binary holds a feature-gated `ModelLineRegistry` and does pure dispatch — it never names a model crate's option types. Onboarding a model line means implementing `ModelLine` in the model crate and adding the instance to the registry in the server binary. **No other server edits**: argument routing, consume-or-reject validation, and config detection all derive from the trait."——注意这里的成本核算视角：参数路由、consume-or-reject、配置检测这三块逻辑**从 trait 派生**，所以它们永远不需要随模型线增长而修改。

**证据五：serve 分支按 plan 分派。** [src/main.rs:L215-L279](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L279)：LoRA 分支看 `plan.lora_modules`（L215），prefill-only 分支看 `plan.prefill_only`（L254），默认分支把 `plan.scheduler_partition_count` 当 `engine_count` 传给 `serve_with_engine_count`（L267-L277）。三个分支里没有一次 `line.name()` 匹配。另见 [src/main.rs:L188](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L188)：模型名只用于日志横幅。

**证据六：README 的边界表背书。** [../README.md:L173-L181](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../README.md#L173-L181) 给 server 的职责恰好一句话："Detect the model, validate its CLI, and launch the selected engine"；总原则在 [../README.md:L31](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../README.md#L31)："Each model owns its scheduler, state, and kernels; serving and KV infrastructure are shared."

#### 4.1.4 代码实践

**实践目标**：用静态审计亲自验证「纯分发」不是注释口号，而是可 grep 的事实；并量化「新增一条模型线」的真实改动面。

**操作步骤**（在 `pegainfer-server/` 目录下）：

1. 数行数并审导入：`wc -l src/main.rs`；再 `grep -n "^use " src/main.rs`——确认导入清单只含 frontend / core / 标准库与工具 crate。
2. 找模型 crate 的全部出现点：`grep -rn "pegainfer_qwen3\|pegainfer_glm52\|pegainfer_gemma4" src/`——预期**只**命中 `src/main.rs` 的 `model_lines()`（cfg 守卫行）。
3. 对照 [Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/Cargo.toml#L34-L48) 写出「新增模型线改动面」清单：`Cargo.toml` 两处（optional 依赖 + feature 行）、`src/main.rs` 两处（注册行 + `families()` 行）、模型 crate 一个 `model_line.rs`。据此回答：`main()` 函数本体要改几行？（答案：0。）

**需要观察的现象**：grep 的命中点数量之少（个位数）；`use` 清单里没有任何 `pegainfer_<model>::` 前缀。

**预期结果**：步骤 2 在当前 HEAD 应只命中 `src/main.rs` 第 26-41 行之间的 cfg 守卫行（每条线一行）。若未来 grep 命中了 `model_lines()` 之外的 src 文件，就是纯分发被破坏的信号。

**待本地验证**：`cargo tree -p pegainfer-server --no-default-features` 是否显示零个模型 crate 依赖（需要可编译的环境；逻辑上应当成立，因为模型 crate 全是 optional）。

#### 4.1.5 小练习与答案

**练习 1**：server 为什么用 `Vec<&'static dyn ModelLine>` 而不是定义一个 `enum Line { Qwen3, Glm52, ... }`？

**参考答案**：三个理由。(1) **类型擦除**：`dyn` 让 server 持有异构列表而不必知道每个变体的负载类型——枚举会把每条线的句柄类型暴露给 server，破坏「不 import 选项类型」。(2) **编译期裁剪**：cfg 守卫的 vec 元素随 feature 增减；枚举的变体集是硬编码的，除非也给每个变体加 cfg，而那会让「未启用变体的 match 臂」问题重新出现。(3) 代价是失去穷尽匹配——注册表因此必须自建两本账做唯一性自检（u2-l3），这是用检查换来的灵活性。

**练习 2**：`main()` 里哪几行代码「同时适用于所有模型线，却又不知道任何模型线的存在」？各举两例。

**参考答案**：分派点本身。例如 [src/main.rs:L185](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L185) 的 `line.validate(&ctx, &provided)?` 与 [src/main.rs:L211](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L211) 的 `line.launch(&ctx)`——通过 trait 对象调用，静态分派到 whichever 线被认领。这就是「分发」的字面含义。

**练习 3**：如果明天要给 server 加一个「启动后打印模型自述文案」的需求，且每条线的文案不同，纯分发架构下正确的做法是什么？

**参考答案**：在 `ModelLine` trait 上加一个带默认实现的 `fn banner(&self) -> String`（或塞进已有的名称/日志路径），由各线覆盖；server 只调用 `line.banner()`。错误做法是在 server 里 match `line.name()`——那会把模型身份重新引进 server，注册一行不再足够，纯分发的改动面承诺破产。（现实中的对照物：`ServePlan` 就是「server 需要新事实 → 契约长出新方法」的实例，见 u2-l5。）

### 4.2 families()：必要的第二真相源与 identity-only 边界

#### 4.2.1 概念说明

检测门的困境（u1-l4 已见过现象，这里讲**为什么无解**）：当用户拿一个 GLM5.2 的 checkpoint 去跑出厂二进制（只编了 qwen3）时，`registry.detect` 只能返回 NoMatch——因为 glm52 的 probe 根本没被编译进二进制，**没有任何代码可以询问**。但用户值得一句「你可能需要 `--features glm52` 重建」的提示。

于是 server 硬编码一张身份表 `families()`：feature 名、该线 probe 认领的 `model_type`（含 `text_config.model_type`）身份、以及本二进制是否编译了它。这就是「**必要的**第二真相源」——第一真相源是各模型 crate 自己的 probe，但提示恰恰要服务「第一真相源不在场」的场景，所以这份重复**不可消除**，只能：

1. **缩小重复的内容**：表里只存身份字符串，不存结构规则——重复面越小，漂移风险越小；
2. **绑定**：用测试把表行与 crate 常量锁在一起（qwen4 行有 `qwen4_hint_identity_matches_the_line_crate` 绑定测试）；
3. **声明能力边界**：文档注释明确提示是 **identity-only**——它不能运行被编译掉的 probe，所以「probe 会拒绝的变体」（量化姊妹版、绑词嵌入、YaRN 绳长外推）也会命中提示。它只报出「该用哪个 feature 重建」，**不承诺重建后这个 checkpoint 能服务**。

#### 4.2.2 核心流程

提示的完整决策链（发生在 `main()` 的 detect 错误处理里）：

```text
registry.detect(&config)
  ├─ Ok(line)            → 正常放行，提示逻辑不参与
  ├─ Conflict {..}       → 直接报错（注册表 bug，u2-l3）
  └─ NoMatch {..} ───────┐
                          ▼
              feature_gate_hint(&config)
                model_type        = config["model_type"]        （缺失当 ""）
                text_model_type   = config["text_config"]["model_type"]（缺失当 ""）
                在 families() 里找第一个满足
                    !family.compiled                         ← cfg! 编译期已定
                    && (model_types ∋ model_type || text_model_types ∋ text_model_type)
                的行 → "this looks like a {feature} model;
                         rebuild pegainfer-server with --features {feature}"
                          │
                          ▼
              拼接在 NoMatch 错误消息之后，一次性上抛（不二次尝试）
```

identity-only 边界可以用一张对照表表达——左列是提示**知道**的，右列是（编译后的）probe 才知道的：

| families() 表（提示的知识） | 编译后的 probe（真实裁决） |
|---|---|
| `model_type` 字符串 | `model_type` + `architectures` |
| `text_config.model_type` 字符串 | 结构校验：gemma4 的滑窗/层型/MoE 配置；kimi-k2 的全部维度常量 |
| （不知道） | 量化配置、绑词嵌入、YaRN 等变体特征 |

防漂移的三道闸：`families()` 返回**定长数组** `[Family; 8]`（少写一行要过不了类型检查——加第九条线必须显式改长度）；`compiled` 用 `cfg!` 在编译期求值（表行对本二进制恒真）；qwen4 行的绑定测试 + `detect_gate` 集成测试锁行为（u5-l2）。

#### 4.2.3 源码精读

**表与边界的权威注释。** [src/main.rs:L54-L66](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L54-L66)："This is **a second source of truth by necessity**: the hint exists for the case where the line — and therefore its `probe` — is compiled out, so these strings cannot be read from the line crate... The hint is **identity-only**: it cannot run a compiled-out probe, so it also fires for a variant that probe would refuse — a quantized sibling (its `quantization_config`), tied embeddings, a YaRN rope. It names the feature to rebuild with; **it is not a promise that the checkpoint is servable**."——综合实践第 2 问要求「依据 families() 的文档注释作答」，答案的全部要点都在这四行里。

**表本体。** [src/main.rs:L67-L118](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L67-L118)：八行 `Family`，每行 `compiled: cfg!(feature = "...")`。注意这是**编译期**替换成 `true`/`false` 字面量——同一份源码编出的每个二进制，表内容都对自己诚实。

**提示函数只读两个键。** [src/main.rs:L123-L144](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L123-L144)：`feature_gate_hint` 读 `model_type` 与 `text_config.model_type` 各一次，`find_map` 找第一个「未编译且身份命中」的行。没有尺寸、没有量化、没有 `architectures`——identity-only 是实现层面的克制，不是口号。

**接线点：只在 NoMatch 时询问。** [src/main.rs:L168-L175](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L168-L175)：`registry.detect` 的 `map_err` 里 `matches!(error, DetectError::NoMatch { .. })` 才调 hint，且只把提示**拼接**在错误后（`; {hint}`），不做第二次检测。

**行为被四个单测锁住。** [src/main.rs:L295-L322](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L295-L322)：未编译家族给提示（glm52）、`text_config` 身份也给提示（gemma4）、已编译家族静默（qwen3）、未知家族静默（`frobnicate_lm`）。绑定测试在 [src/main.rs:L324-L337](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L324-L337)：qwen4 行与 crate 自己的 `MODEL_TYPE`/`TEXT_MODEL_TYPE` 常量逐字相等。

**「probe 会拒绝的变体」长什么样（超出身份的裁决）。** 两个真实例子：

- gemma4 的 probe 是 fail-closed 的结构校验：[../pegainfer-gemma4/src/probe.rs:L1-L19](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-gemma4/src/probe.rs#L1-L19) 注释写明 "identity plus the structural facts"；[L64-L75](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-gemma4/src/probe.rs#L64-L75) 会**拒绝** E4B 边缘系列配置（`sliding_window=512` 等）。于是在一个没编 gemma4 的构建里，一个 `model_type=gemma4` 的 E4B config 会得到「rebuild with --features gemma4」——而重建后 probe 照样拒绝它。这就是「过度承诺」的具体形态。
- kimi-k2 的 probe 逐项校验全部维度常量：[../pegainfer-kimi-k2/src/config.rs:L43-L79](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-kimi-k2/src/config.rs#L43-L79)（注释："every dimension matches the constants this crate is compiled against"）。任何尺寸不同的姊妹 checkpoint 都会被 probe 拒绝，但在未编译提示层，`model_type=kimi_k25` 身份就足以触发重建建议。

**对照：qwen3 的 probe 是 identity-only。** [../pegainfer-qwen3/src/config.rs:L894-L910](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/config.rs#L894-L910)：只查 `model_type == "qwen3"` 与 `architectures` 含 `Qwen3ForCausalLM`，注释明言 "size and shape validation belong to the config loader"。含义有二：(a) 量化版 Qwen3 的 `model_type`/`architectures` 与 BF16 版完全相同，所以 probe 会**认领**它；(b) 在出厂构建里 qwen3 是编译进来的（`!family.compiled` 为假），提示对它**永远静默**——量化版 checkpoint 的失败发生在后面的引擎加载（错误形态是 `failed to start Qwen3 engine` 的上下文链）。综合实践第 2 问的完整答案必须包含这一层。

#### 4.2.4 代码实践

**实践目标**：亲手跑一遍提示逻辑的四条单测路径，并用一个「probe 会拒绝但提示会给」的构造 config 体会过度承诺。

**操作步骤**：

1. 在 `pegainfer-server/` 下运行单测（不需要 GPU，只需要能编译本 crate 及其依赖）：
   ```bash
   cargo test -p pegainfer-server --lib hint -- --nocapture
   ```
   对照 [src/main.rs:L295-L322](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L295-L322) 的四个用例逐个核对测试名。（待本地验证：本机无 CUDA 工具链时默认 feature 的 qwen3 依赖可能编译失败，可改用 `cargo test -p pegainfer-server --no-default-features --lib hint` 跑未编译家族的两条用例。）
2. 纸面推演（无需运行）：构造 `{"model_type": "gemma4", "text_config": {"model_type": "gemma4_text", "sliding_window": 512, "attention_k_eq_v": false}}`，走一遍 [src/main.rs:L123-L144](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L123-L144) 的逻辑，写出出厂构建（未编 gemma4）下 detect 的完整错误消息。
3. 再推演：把同一份 config 用于 `--features gemma4` 构建，写出此时 detect 的结果（提示？认领？拒绝？）。

**需要观察的现象**：步骤 2 的消息里同时出现「NoMatch 的逐线拒绝原因」与「--features gemma4 提示」两段，由 `; ` 连接；步骤 3 里提示消失，取而代之的是 gemma4 probe 的结构拒绝（E4B）。

**预期结果**：步骤 2 = `no compiled-in model line claims this config (...); rejections: Qwen3: ...; this looks like a gemma4 model; rebuild pegainfer-server with --features gemma4`（措辞以 [../pegainfer-frontend/src/model_line.rs:L34-L37](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L34-L37) 与 [src/main.rs:L138-L141](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L138-L141) 的模板为准）；步骤 3 = 提示不出现（家族已编译），probe 因 E4B 结构拒绝，detect 仍是 NoMatch 但**没有** hint 后缀——「重建后诚实拒绝」。

#### 4.2.5 小练习与答案

**练习 1**：`families()` 为什么不能从模型 crate 导入常量，让两份真相合并成一份？

**参考答案**：因为提示的服务对象恰恰是「该 crate 未被编译」的场景。一旦 import 了 crate 的常量，这个依赖就把 crate 拉进编译——出厂二进制就得带上全部八条线，feature 裁剪失效。重复是编译裁剪的直接代价，故称「by necessity」。可行的缓解只剩缩小重复面（只复制身份字符串）+ 绑定测试（qwen4 行）。

**练习 2**：`compiled: cfg!(feature = "glm52")` 为什么必须用 `cfg!` 宏而不是运行时检查？

**参考答案**：`cfg!` 在编译期把表达式替换为布尔字面量，且**不需要**该 feature 对应的代码存在（与 `#[cfg]` 属性同源的求值）；换成任何运行时机制（比如查询已注册线）都会退化成「只能看见编译进来的线」，而提示需要知道的是「哪些线**没**被编译」。表行在本二进制里因此是静态事实，无运行时成本。

**练习 3**：给一个 `families()` 会给出「过度承诺」提示的具体 config，并说明它在哪个意义上过度。

**参考答案**：例：`{"model_type": "kimi_k25", "text_config": {"model_type": "kimi_k2", "hidden_size": 999, ...}}`（尺寸与 kimi-k2 crate 编译常量不符）。在未编 kimi-k2 的构建里，身份命中 → 提示 `--features kimi-k2`；但重建后 [kimi-k2 的 probe](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-kimi-k2/src/config.rs#L43-L79) 会因 `hidden_size` 不匹配拒绝。过度的意义：提示承诺的只是「身份属于这个家族」，用户读到的却是「重建就能跑」——按 [src/main.rs:L66](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L66) 的原话，"it is not a promise that the checkpoint is servable"。

### 4.3 LaunchedEngine 迁移边界：共存、删除条件与防漂移

#### 4.3.1 概念说明

u3-l1 已经画过两代契约的地图；本讲换一个角度——**这块脚手架什么时候拆、拆的时候要带走什么**。

`LaunchedEngine` 是迁移期的二选一枚举：`Handle(EngineHandle)` 装 legacy 每 token 契约，`Stepped(Engine)` 装 step 契约交接包。它的文档注释把「删除条件」写进了类型定义：**每条模型线都迁移到 step 驱动后，枚举被删除，只留 `Engine`**。两个为删除而做的设计伏笔：

- **两个 `From` 实现**（`From<EngineHandle>` 与 `From<Engine>`）让模型线用 `.into()` 装箱——删除 `Handle` 变体时，step 线的装箱代码不因类型更名而连锁修改；
- **server 几乎不打开这个枚举**：`main()` 只搬运它（`spawn_blocking` 的返回类型），唯一的解构点是 LoRA 分支的 let-else。分派逻辑集中在 `vllm/mod.rs` 的 `engine_task` 一处。删除时改动面被类型系统天然限定。

**删除的前提**（frontend 架构文档记录的顺序）：先迁 glm52（多调度器试点，带上 P/D 与 EP 的需求），再迁 kimi-k2、deepseek-v2-lite，然后删除 legacy 契约模块与 `LaunchedEngine::Handle`。另有一条硬前提：**`KvPrefix` 前缀解析（`submit_resolved`）目前只存在于 legacy 路径**——要么随迁移在 step 契约上重新安家，要么明确放弃，否则 handle.rs 删不干净。

同一个模块里还有一条与「契约演化」相关的纪律：**`ServePlan` 的 `..default()` 展开约定**。契约类型会被不进 CI 的模型 crate 构造，而 CI 只编译三条线（qwen3、qwen35、deepseek-v2-lite），所以在其他 crate 里写**穷尽字面量**构造 `ServePlan`，下一次给契约加字段时那个 crate 无声地坏掉，直到有人手工编译它——约定因此是「用 `..ServePlan::default()` 展开，只拼写你设置的字段」。这是「编译可见性」问题的通用解法，与 `LaunchedEngine` 的删除属于同一主题：**契约演化时，谁受编译器保护、谁不受**。

#### 4.3.2 核心流程

当前（HEAD `a474b264`）的全工作区变体普查——七条已注册线（qwen4 在 launch 里直接报错，不算返回方）：

| 模型线 | launch 返回 | 证据 |
|---|---|---|
| qwen3 | `Stepped` | [../pegainfer-qwen3/src/model_line.rs:L366](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L366) |
| qwen35 | `Stepped` | [../pegainfer-qwen35/src/model_line.rs:L182](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen35/src/model_line.rs#L182) |
| gemma4 | `Stepped` | [../pegainfer-gemma4/src/model_line.rs:L45](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-gemma4/src/model_line.rs#L45) |
| k3 | `Stepped` | [../pegainfer-k3/src/model_line.rs:L320](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-k3/src/model_line.rs#L320) |
| deepseek-v2-lite | `Handle` | [../pegainfer-deepseek-v2-lite/src/model_line.rs:L32](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-deepseek-v2-lite/src/model_line.rs#L32) |
| glm52 | `Handle` | [../pegainfer-glm52/src/model_line.rs:L281](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-glm52/src/model_line.rs#L281) |
| kimi-k2 | `Handle` | [../pegainfer-kimi-k2/src/model_line.rs:L88](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-kimi-k2/src/model_line.rs#L88) |

删除的依赖图（顺序即前提）：

```text
删除 LaunchedEngine::Handle（以及枚举本身）
  ├── 前提 1：glm52 迁到 step 契约（多调度器试点：P/D 角色、EP 需求）
  ├── 前提 2：kimi-k2 迁到 step 契约
  ├── 前提 3：deepseek-v2-lite 迁到 step 契约
  ├── 前提 4：KvPrefix / submit_resolved（仅存在于 legacy 路径）安家或删除
  └── 然后删除：
        engine/{request,event,sink,kv,handle}.rs        （legacy 模块）
        vllm/bridge.rs 的 LocalEngineBridge             （legacy 桥；BridgeLink 留给 stepped 桥）
        wiring.rs 的枚举与 From<EngineHandle>
        main.rs LoRA 分支的 let-else                     （见 4.3.3）
        vllm/mod.rs engine_task 的 Handle 臂             （见 4.3.3）
        legacy 的 watch 通道 metrics 路径                （u4-l6：DP 拓扑声明改由 EngineInfo 承担）
```

#### 4.3.3 源码精读

**枚举定义与删除条件。** [../pegainfer-frontend/src/engine/wiring.rs:L162-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L162-L168)：文档注释 "What `ModelLine::launch` returns during the contract migration... **Deleted (in favor of `Engine` alone) once every model line is migrated.**"。[L170-L180](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L170-L180) 是两个 `From` 实现——为「将来只留 `Engine`」预留的机械降级路径。

**两代分组的权威地图。** [../pegainfer-frontend/src/engine/mod.rs:L1-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/mod.rs#L1-L29)：step 驱动组（step/ledger/request_lifecycle/metrics/wiring/driver/control）与 legacy 组（[L21-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/mod.rs#L21-L26)：request/event/sink/kv/handle，注释 "kept until the remaining model lines migrate"）。顺带一个诚实的观察：文档首行写 "qwen3 is migrated, other lines follow"，而普查显示已有四条线迁移——**迁移期的注释是快照，不是实时状态**；权威答案永远是 launch 构造点的 grep 普查（这正是 4.3.4 的实践）。另外 [L28-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/mod.rs#L28-L29) 的扁平 re-export 约定（`pegainfer_frontend::engine::X` 路径不随内部分拆而变）让「删除五个模块」对消费者几乎无感——路径稳定是迁移能渐进进行的胶水。

**server 侧唯一的解构点。** [src/main.rs:L215-L223](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L223)：LoRA 分支 `let LaunchedEngine::Stepped(engine) = launched else { anyhow::bail!("LoRA serving requires a step-driven engine"); };`。删除 `Handle` 后：let-else 退化为普通绑定，这条错误消息（以及「LoRA 不支持 legacy 引擎」这个错误类别）整体消失。**注意 main 的另外两个 serve 分支不打开枚举**——它们把 `engine_load`（返回 `LaunchedEngine` 的 Future）原样传给 serve 函数族，删除枚举时这两条分支只是类型签名跟着改。

**装配侧的分派点。** [../pegainfer-frontend/src/vllm/mod.rs:L309-L335](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L309-L335) 是 `Handle` 臂：对账用 `handle.scheduler_partition_count()`、逐分区铸 `LocalEngineBridge`、`metrics_watch_for` 挂 legacy watch 通道、`drop(handle)`；[L336-L363](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L336-L363) 是 `Stepped` 臂：对账用 `engine.schedulers.len()`、收集 `scheduler.join`、逐调度器铸 `SteppedEngineBridge`。删除后整个 `match launched` 塌缩为 Stepped 臂的函数体——计数对账**不会**消失（它防的是「声明分区数与实际不符」的带病上线，u4-l1），消失的只是 legacy 的取数通道。

**`ServePlan::default` 展开约定。** [../pegainfer-frontend/src/model_line.rs:L286-L292](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L286-L292)："Lines should build their plan with `..ServePlan::default()` and spell only what they set: **CI compiles three model lines**, so an exhaustive literal in any other crate breaks on the next field added here and nothing catches it until that crate is checked by hand." 默认值本体在 [L325-L334](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L325-L334)。

**官方迁移记录。** [../docs/subsystems/frontend/frontend-architecture.md:L114-L116](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../docs/subsystems/frontend/frontend-architecture.md#L114-L116)（Next step）：先 glm52、再 kimi-k2/deepseek-v2-lite、然后删 legacy 模块与 `LaunchedEngine::Handle`；[L57-L59](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../docs/subsystems/frontend/frontend-architecture.md#L57-L59) 补充了 `KvPrefix`/`submit_resolved` 只在 legacy 路径存在这一硬前提。[../README.md:L182](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../README.md#L182) 是面向用户的表述："The step-based contract and legacy `EngineHandle` contract currently coexist."

#### 4.3.4 代码实践

**实践目标**：亲手做出本讲 4.3.2 的普查表，并把「删除 Handle」翻译成一份具体的 diff 清单（只读，不改码）。

**操作步骤**（在仓库根目录）：

1. 普查全部变体出现点并分类：
   ```bash
   grep -rn "LaunchedEngine::" --include="*.rs" pegainfer-frontend/src pegainfer-qwen3/src pegainfer-qwen35/src pegainfer-gemma4/src pegainfer-k3/src pegainfer-deepseek-v2-lite/src pegainfer-glm52/src pegainfer-kimi-k2/src pegainfer-qwen4/src pegainfer-server/src
   ```
2. 给每个命中点贴标签：**构造点**（`.map(LaunchedEngine::Stepped)` / `.map(LaunchedEngine::Handle)`，在模型 crate）、**分派点**（`match launched` / let-else，在 vllm/mod.rs 与 main.rs）、**定义点**（wiring.rs 的枚举与两个 From）、**测试点**（frontend 与 server 的测试代码）。
3. 写「删除 diff 清单」：按 4.3.2 的依赖图，列出每个前提对应要改的文件；对照步骤 1 的标签，统计哪些命中点属于「随删除消失」、哪些属于「随删除改写」。
4. 回答自检：删除 `Handle` 后，`main()` 的三个 serve 分支里有几个需要改逻辑？（提示：分派依据是 `ServePlan` 字段，不是枚举变体。）

**需要观察的现象**：`LaunchedEngine::Handle` 的非测试命中点应当只有三处构造点（deepseek-v2-lite/glm52/kimi-k2 各一）加一处分派点（vllm/mod.rs 的 Handle 臂）；`::Stepped` 的构造点恰好覆盖普查表的四条 step 线加 vllm/mod.rs 的 `serve_model_with_lora_routes` 转发（`std::future::ready(Ok(LaunchedEngine::Stepped(engine)))`）。

**预期结果**：步骤 4 的答案是**零个分支需要改逻辑**——LoRA 分支的 let-else 退化成普通绑定（形态变化、逻辑不变），prefill-only 与默认分支只随签名更名。这就是「server 只搬运枚举、按 plan 分派」在删除时刻兑现的红利。

**待本地验证**：若想进一步验证，可在**自己的分支**上删掉 wiring.rs 的 Handle 变体后跑 `cargo check -p pegainfer-frontend`，观察编译错误逐条对应你的 diff 清单（本讲不修改源码，此步可选）。

#### 4.3.5 小练习与答案

**练习 1**：两个 `From` 实现对「删除 Handle」有什么实际帮助？没有它们会怎样？

**参考答案**：`From<Engine>` 让 step 线可以用 `.into()` 装箱，`launch` 的返回类型从 `LaunchedEngine` 改回 `Engine` 时，这类调用点零修改；`From<EngineHandle>` 在删除时整体移除即可。没有它们，每条线都得手写 `LaunchedEngine::Stepped(engine)`（如 qwen3 现在的 `.map(LaunchedEngine::Stepped)`），删除时七处构造点都要同步改写——伏笔把「连锁修改」降为「局部删除」。

**练习 2**：为什么 `KvPrefix`（KV 前缀解析）是删除 legacy 契约的硬前提之一？

**参考答案**：因为它是**只存在于 legacy 路径的功能**：`KvPrefix` 的 rank 绑定语义与 `submit_resolved` 都长在 handle.rs/kv.rs 上（u3-l6、u5-l1），而 step 契约的调度器目前只能拒绝 `Request` 能表达的东西（含非空 `kv_transfer_params`）。直接删 handle.rs 等于无声地砍掉跨实例前缀复用能力；必须先决定「在 step 契约上重新安家」还是「明确放弃」，否则删除是功能回退而非清理。（依据：[frontend-architecture.md:L57-L59](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../docs/subsystems/frontend/frontend-architecture.md#L57-L59)。）

**练习 3**：`ServePlan::default` 展开约定防的是什么漂移？它与「删除 Handle」的共同主题是什么？

**参考答案**：防的是**编译可见性**漂移——CI 只编译三条线，未进 CI 的模型 crate 若用穷尽字面量构造 `ServePlan`，契约加字段时它们不会编译失败，坏掉是静默的，直到有人手工编译（依据 [model_line.rs:L286-L292](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L286-L292) 的原文）。共同主题：契约演化时，只有被编译的代码受编译器保护；`..default()` 展开、`From` 预留、扁平 re-export，都是把「演化冲击波」限制在受保护范围内的手段。

## 5. 综合实践

**任务**：写一页设计笔记（Markdown，放在你自己的笔记目录，不进仓库），回答三个问题。以下是题目解析与自查要点——先自己写，再对照。

**第 1 问：若把 `SharedArgs` 拆进各模型 crate，注册表的哪些自检会失效或变贵？**

自查要点（对照 u2-l3 的两本账）：

- **单一共享注册消失**：今天 `SharedArgs::augment_args` 在 [ModelLineRegistry::new](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L506-L515) 里以 `ArgOwner::Shared` 注册**恰好一次**。拆散后每个 crate 一份拷贝，`claim_id` 的「`SharedArgs` 与线撞 id」专用 panic（[L439-L451](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L439-L451)，"list it in consumed_shared_args instead"）会在**合法的共享拷贝之间**误触发；要豁免就得给账本开「按设计重复」的口子——唯一性不变式被设计本身削弱。
- **值级漂移不可见**：账本只比 id 与拼写，不比值、默认值与 value_parser。今天一份定义，漂移不可能；N 份拷贝后，A 线的 `kv_offload_host_gib` 默认 8.0、B 线 4.0，账本一声不吭。要把检查买回来就得跨 crate 比对值——**变贵**的那部分。
- **CORE_ARGS 与 consumed 检查失去靶子**：[CORE_ARGS 断言](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L510-L514) 与 [register_line 的 consumed 断言](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L480-L495) 都对着「共享命令」这本账；拆散后要么删检查（自检失效），要么对 N 份拷贝各查一遍（变贵且仍不查值）。
- **consume-or-reject 的分类学退化**：[validate_provided](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L603-L630) 的三分法（核心 / 被消费共享 / 线独占）依赖共享集合存在；拆散后「本线不消费的共享 flag」与「别线的 flag」不可区分，错误消息失去「shared-but-unconsumed」这一类。
- **依赖表撞车**：`SHARED_ARG_REQUIREMENTS`（[L223-L232](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L223-L232)）与各线 `arg_requirements` 收进同一张 `requirements` 表，[L539-L543](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L539-L543) 的「同一 source 声明两次要求」断言会让两份拷贝的同名边直接 panic——需要先去重，机制又变贵一层。

**第 2 问：一个 `model_type=qwen3` 的量化变体在 stock 构建下会得到什么提示？这个提示在什么意义上「过度承诺」？**

自查要点（依据 [families() 文档注释](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L54-L66)）：

- **直接答案：得不到任何提示，而且不需要提示。** stock 构建 = default = `qwen3` 已编译：提示的先决条件 `!family.compiled`（[L133-L136](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L133-L136)）对 qwen3 恒假；同时 qwen3 的 probe 是 identity-only（[config.rs:L894-L910](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen3/src/config.rs#L894-L910)，量化变体的 `model_type`/`architectures` 与 BF16 版相同），所以 detect 直接认领，运行进入引擎加载，量化权重的失败（若发生）以 `failed to start Qwen3 engine` 的 anyhow 链出现。
- **「过度承诺」是提示路径的性质，不是这个场景的性质**：对**被编译掉**的家族（如 stock 下的 gemma4/kimi-k2），提示无法运行那个 probe，于是「probe 会拒绝的变体」——量化姊妹版（`quantization_config`）、绑词嵌入、YaRN——也会命中身份、得到 `--features X` 建议（具体反例见 4.2.3 的 gemma4 E4B 与 kimi-k2 尺寸校验）。提示承诺的只是「身份属于该家族、该 feature 值得试」；用户可能读成「重建后一定能跑」。按文档原话："It names the feature to rebuild with; it is not a promise that the checkpoint is servable."
- 加分项：指出这是一笔**愿打的交易**——宁可给出可能落空的重建建议（成本低、多数场景正确），也不愿让用户面对一段没有方向感的 NoMatch 堆栈。

**第 3 问：删除 `LaunchedEngine::Handle` 需要哪些前提？当前 `main.rs` 与 `vllm/mod.rs` 里哪些分支会随之消失？**

自查要点（对照 4.3.2/4.3.3）：

- 前提：glm52 → kimi-k2 → deepseek-v2-lite 依次迁到 step 契约（架构文档记录的顺序）；`KvPrefix`/`submit_resolved` 在 step 契约上安家或明确放弃；legacy watch 通道的 metrics 路径由共享单元 `MetricsPublisher` 全面接管（u4-l6）。
- `main.rs` 消失/退化的分支：LoRA 分支的 let-else（[L221-L223](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L221-L223)）退化为普通绑定，`"LoRA serving requires a step-driven engine"` 错误类别消失；**其余 serve 分支零逻辑改动**（分派依据是 `ServePlan` 字段）。
- `vllm/mod.rs` 消失的分支：`engine_task` 里 `match launched` 的整个 `Handle` 臂（[L310-L335](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L310-L335)）——含 `handle.scheduler_partition_count()` 对账、逐分区 `LocalEngineBridge`、`metrics_watch_for`、`drop(handle)`；`match` 塌缩为 Stepped 臂的直体（计数对账保留，改读 `engine.schedulers.len()`）。
- 连带删除：wiring.rs 的枚举与 `From<EngineHandle>`、engine 的五个 legacy 模块及其 re-export、bridge.rs 的 `LocalEngineBridge`。
- 加分项：指出「server 侧几乎零改动」本身就是 4.1 纯分发设计的验收——迁移的全部冲击被接缝吸收在 frontend 内部。

## 6. 本讲小结

- **纯分发**：server 对模型世界的知识 = 一列 `&'static dyn ModelLine`；新增模型线的 server 改动只有注册行 + `families()` 行 + feature。代价是 `families()` 的必要重复、trait 表达力即上限、以及用注册表两本账买回被 `dyn` 放弃的穷尽匹配。
- **必要的第二真相源**：提示服务的场景恰恰是「probe 不在场」，所以身份表只能硬编码、不可合并；缓解手段是缩小重复面（identity-only）、绑定测试与定长数组。提示只承诺身份归属，不承诺 checkpoint 可服务——量化/绑词/YaRN 变体会命中一个「probe 本会拒绝」的建议。
- **迁移边界**：`LaunchedEngine` 是写明删除条件的脚手架；删除前提是三条 Handle 线全部迁移 + `KvPrefix` 落定；server 侧唯一解构点（LoRA let-else）与装配侧唯一分派点（engine_task 的 Handle 臂）是删除时的全部改动面。
- **编译可见性**：CI 只编译三条线，未编译 crate 的契约构造必须用 `..default()` 展开——契约演化的冲击必须限制在受编译器保护的范围内，`From` 预留与扁平 re-export 同属此类伏笔。
- **注释是快照**：engine/mod.rs 说「qwen3 已迁移」，现实是四条线已迁移——迁移期的文档注释会滞后，权威状态来自对构造点的 grep 普查。

## 7. 下一步学习建议

本讲是手册最后一篇，三条继续深入的路线：

1. **跟踪迁移本身**：读 [../docs/subsystems/frontend/frontend-architecture.md](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../docs/subsystems/frontend/frontend-architecture.md)（两代契约的权威文档）与 [../docs/roadmap/direction.md](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../docs/roadmap/direction.md)（模型自有引擎的设计动机）；关注 glm52 的 step 契约迁移 PR——它同时是「多调度器 + P/D + EP」的试点，是本讲 4.3 全部抽象的实战场。
2. **亲手体验契约**：跑 u5-l2 的 Reverse 线综合实践（或 `cargo run -p pegainfer-frontend --example echo-server`），再读 `pegainfer-sim` crate（CPU-only 的 step 契约参考实现，配套教程见 `openinfer-pegainfer-sim-tutorial/`）——「实现一条线」是对本手册五个单元的最终检验。
3. **把自己变成贡献者**：从 `docs/index.md` 的路由表出发找一个能改进的角落；记住本仓库的 AI 辅助贡献纪律——改动必须服务于一条可命名的生产不变量，并提供真实证据（E2E、同上下文 A/B 或模型评测）。
