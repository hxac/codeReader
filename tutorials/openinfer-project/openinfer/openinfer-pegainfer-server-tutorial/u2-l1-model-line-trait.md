# u2-l1 · ModelLine trait：server 与模型 crate 之间的接缝

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐个说出 `ModelLine` trait 的 **8 个方法**，并指出其中哪些没有默认实现（必选）、哪些可以整个省略（`augment_cli`、`consumed_shared_args`、`arg_requirements`、`validate`、`serve_plan` 共 5 个都有默认实现）。
2. 解释「接缝（seam）」的确切含义：`pegainfer-server` 二进制只持有 `&'static dyn ModelLine` 的列表，从头到尾**不 import 任何模型 crate 的选项类型**——新增一条模型线对 server 的改动只有「注册一行 + 一个 Cargo feature」。
3. 说出 `LaunchContext` 里 `launch` 和 `serve_plan` 各自能读到什么，以及 Qwen3 如何用 `FromArgMatches` 从中恢复自己的独占 CLI。
4. 理解 `launch` 为什么返回 `anyhow::Result<LaunchedEngine>`（深层上下文链，调用方只传播不分支），而 `probe`/`validate` 走的却是 `String`/`CliError` 这两类「调用方要分支」的错误。

本讲是单元二的地基：u2-l2（SharedArgs）、u2-l3（注册表）、u2-l4（Qwen3 完整实现）、u2-l5（ServePlan）都在本讲建立的 trait 面上继续展开。

## 2. 前置知识

本讲默认你已读过 u1-l3（main.rs 启动主流程）与 u1-l4（检测门）。用通俗语言补齐四个 Rust 概念：

- **trait 与 trait 对象（`dyn Trait`）**：trait 是一组方法签名的前置声明；`dyn ModelLine` 表示「某个实现了 `ModelLine` 的具体类型，运行时才知道是谁」。调用它的方法走动态分发（vtable），所以 server 不需要在编译期知道对面是 Qwen3 还是 Kimi-K2。
- **`&'static dyn ModelLine`**：指向一个活满整个进程生命周期的 trait 对象。模型 crate 用单元结构体（unit struct，如 `struct Qwen3Line;`，零大小的类型，不需要构造函数）做出 `pub static MODEL_LINE: Qwen3Line = Qwen3Line;` 这样的静态实例，再对它取引用，就免费得到 `&'static`。
- **clap 的三个角色**：`clap::Command` 描述整条命令行（有哪些 flag）；`clap::ArgMatches` 是一次解析的结果（按 arg id 存值）；`FromArgMatches`/`Args::augment_args` 让一个 `#[derive(Args)]` 的结构体既能「把自己的 flag 挂到别人的 Command 上」，也能「从合并后的 ArgMatches 里只取回自己的字段」。这个「挂上去 / 取回来」的对称能力，是本讲接缝设计的支柱。
- **thiserror 与 anyhow 的分工**：`thiserror` 生成带模板的枚举错误，供调用方 `match` 分支处理；`anyhow` 是类型擦除的错误链，只用于「带着上下文往上抛、最终打印给人看」的场景。本讲会看到这两种用法在同一个 trait 里各就各位。

| 错误通道 | 载体 | 谁来分支 |
| --- | --- | --- |
| clap 自身解析错误 | clap 的错误（退出码 2） | clap 框架 |
| 结构化枚举错误 | `DetectError` / `CliError`（thiserror） | server / 注册表，要按变体分支 |
| 深层失败链 | `anyhow::Result`（launch） | 没人分支，只传播并打印 |

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs) | **接缝本体**：`ModelLine` trait、`LaunchContext`、`DetectError`/`CliError`、`SharedArgs`、`ModelLineRegistry` 都住在这里 |
| [pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs) | 一条真实模型线的完整实现：8 个方法全用上 |
| [src/main.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs) | 接缝的消费方：纯分发二进制，只调 trait 方法 |
| [pegainfer-frontend/examples/echo-server.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs) | 无 GPU 的最小完整模型线（EchoLine），本讲综合实践的模板 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs) | `launch` 的返回类型 `LaunchedEngine`/`Engine` 在此定义（u3-l6 精读） |

## 4. 核心概念与源码讲解

### 4.1 接缝：server 只认识 `&'static dyn ModelLine`

#### 4.1.1 概念说明

「接缝」是两个编译单元之间暴露给彼此的**全部** API 面。PegaInfer 的设计纪律是：把这条接缝收窄到一个 trait——`ModelLine`。server 二进制对模型世界的全部认知就是一列 `&'static dyn ModelLine`，它：

- 不知道 Qwen3 有哪些选项类型（不 `use pegainfer_qwen3::...` 的任何选项结构体）；
- 不知道每条线怎么解析 CLI、怎么加载权重、怎么起调度器线程；
- 只会在固定的阶段调用固定的 trait 方法，把行为完全委托给对面。

收益是双向的：模型 crate 可以自由演化自己的选项结构（比如 Qwen3 把 `Qwen3Cli` 设为 crate 私有），只要 trait 面不变；server 则对所有模型线一视同仁——参数路由、consume-or-reject 校验、config 检测全部「从 trait 派生」，不需要为任何一条线写专用代码。

#### 4.1.2 核心流程

从源码编译到服务启动，一条模型线经过这条链：

```text
Cargo feature（qwen3 / kimi-k2 / ...）
   └─> 模型 crate 被编入二进制，暴露 pub static MODEL_LINE
        └─> server 的 model_lines()：#[cfg(feature)] 守卫下 &MODEL_LINE 入列
             └─> ModelLineRegistry::new(lines)
                  ├─ 对每条线调 augment_cli()，收集独占 flag 并查重
                  ├─ 读 consumed_shared_args() / arg_requirements() 并校验
                  └─ build_command()：共享 flag + 所有线的独占 flag 合成一条 CLI
             └─> 运行时 registry.detect(config.json) → 恰好一条线 probe 认领
                  └─> validate / serve_plan / launch —— 全部经由 trait 方法分发
```

关键认知：**`--help` 输出与解析不依赖检测出的模型**。所有编译进来的线的独占 flag 都在命令行上；检测出的线只是「谁有权消费这些 flag」的裁决者（u2-l3 展开）。

#### 4.1.3 源码精读

接缝的契约写在 frontend 模块文档的第一句话里——server 二进制「做纯分发，从不点名模型 crate 的选项类型」：

[pegainfer-frontend/src/model_line.rs:1-12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L1-L12) —— 定义「模型线 = 引擎契约以南的一切：config 探测、自己的 CLI 旋钮、launch 启动调度器线程」；并明确接入新线 = 在模型 crate 实现 `ModelLine` + 在 server 注册表加一个实例，**没有其他 server 改动**。

server 侧的镜像承诺：

[src/main.rs:1-4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L1-L4) —— server 自述为「对编译进来的 `ModelLine` 做纯分发；每个模型专属的 flag、规则、选项类型都住在模型 crate 里」。

注册表的内容物就是 8 行 `#[cfg]` 守卫的静态引用：

[src/main.rs:24-43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L24-L43) —— `model_lines()` 返回 `Vec<&'static dyn ModelLine>`；只有对应 feature 打开的那一行被编入（default 构建只有 `qwen3` 行）。这就是 u1-l2 讲过的 feature 矩阵在接缝上的落点。

模型 crate 侧的对应物是一个三行的「入场券」：

[pegainfer-qwen3/src/model_line.rs:28-30](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L28-L30) —— `pub static MODEL_LINE: Qwen3Line = Qwen3Line;`：单元结构体的静态实例，天然满足 `&'static dyn ModelLine`，零运行时成本。该文件模块文档同时声明「server 二进制只看得见 `&MODEL_LINE`」。

无 GPU 版的同形实现：

[pegainfer-frontend/examples/echo-server.rs:57-62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L57-L62) —— EchoLine 用同样的 `static MODEL_LINE: EchoLine = EchoLine;` 模式，注释把它称为「每条新模型线的清单，最小版」。

#### 4.1.4 代码实践：数一数入场券

1. **实践目标**：确认「每条模型线一张同形入场券」这一规律，并说清接入新线时 server 要改几行。
2. **操作步骤**：
   - 在仓库根目录执行 `grep -rn "pub static MODEL_LINE" --include=model_line.rs`。
   - 对照 [src/main.rs:24-43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L24-L43) 的 8 个 feature 名，把命中文件与 feature 一一配对。
   - 打开 `Cargo.toml` 的 `[features]` 表，找到每个 feature 拉入的 optional 依赖。
3. **需要观察的现象**：`grep` 命中数应与 feature 数一致（deepseek-v2-lite、gemma4、glm52、k3、kimi-k2、qwen3、qwen35、qwen4 各一张）；每张「入场券」都是同一个三行形状。
4. **预期结果**：你能在白纸上写出接入一条新线的 server 侧全部改动：`Cargo.toml` 加一个 optional 依赖 + feature；`model_lines()` 加两行（`#[cfg(feature = "…")]` 与 `&…::model_line::MODEL_LINE`）。除此之外没有任何 server 文件需要动——参数路由与检测都从 trait 派生。
5. 本实践是纯阅读型，不需要构建。

#### 4.1.5 小练习与答案

**练习 1**：为什么注册表存 `&'static dyn ModelLine` 而不是 `Box<dyn ModelLine>` 或泛型 `Vec<L>`？

**答案**：模型线的静态实例是编译期常量（单元结构体 + `static`），活在整 个进程期，引用即可，无需堆分配；`'static` 生命周期还允许注册表、`LaunchContext`、错误消息（`&'static str` 的线名）随意复制引用而不引入生命周期参数。泛型 `Vec<L>` 则要求所有线是同一个具体类型，与「每 crate 一个类型」的现实冲突，也会为 server 引入单态化多条副本。

**练习 2**：server 如果直接 `use pegainfer_qwen3::Qwen3LaunchOptions` 会破坏什么？

**答案**：破坏接缝的单向性：server 从「只依赖 trait」变成依赖具体模型 crate 的内部类型，于是每接入/修改一条线都要改 server 的多个位置；feature 矩阵也不再是「注册一行」而是一堆 `#[cfg]` 散布的分支。这正是模块文档（model_line.rs:1-12）明令避免的形态。

**练习 3**：`detect` 之后 server 拿到的 `line` 是什么类型？它还能不能知道这是「Qwen3」？

**答案**：`&'static dyn ModelLine`（见 [src/main.rs:168](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168)）。它只能通过 `line.name()` 拿到 `&'static str` 的家族名（用于日志与错误，如 [src/main.rs:188](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L188) 的启动横幅与 [src/main.rs:212](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L212) 的失败上下文），不能 downcast 回具体类型——server 也不需要。

### 4.2 ModelLine trait 定义：8 个方法、3 必选 5 默认

#### 4.2.1 概念说明

trait 是接缝的合同正文。8 个方法里只有 3 个没有默认实现（`name`、`probe`、`launch`），其余 5 个都有合理默认——于是一条「最简模型线」只需写三个方法（EchoLine 就是这么做的），而一条复杂线可以全部自己实现（Qwen3Line）。方法表如下：

| # | 方法 | 必选/默认 | 默认行为 | 调用时机（u1-l3 的阶段编号） |
| --- | --- | --- | --- | --- |
| 1 | `name()` | **必选** | — | 注册查重、日志横幅、错误消息、`--help` |
| 2 | `probe(config)` | **必选** | — | 阶段④ `registry.detect`：逐线试探 config.json |
| 3 | `augment_cli(cmd)` | 默认 | 原样返回 `cmd` | 阶段②/③ `build_command`：把独占 flag 挂上 CLI |
| 4 | `consumed_shared_args()` | 默认 | 空切片 | 注册时（查 id 合法性）+ 阶段⑤ `validate_provided` |
| 5 | `arg_requirements()` | 默认 | 空切片 | 注册时校验，并转成 clap 的 `requires_all` |
| 6 | `validate(ctx, provided)` | 默认 | `Ok(())` | 阶段⑤之后：线内跨 flag 冲突矩阵 |
| 7 | `serve_plan(ctx)` | 默认 | `Ok(ServePlan::default())` | 阶段⑥：引擎加载前告知前端服务形态 |
| 8 | `launch(ctx)` | **必选** | — | 阶段⑦ `spawn_blocking` 里：加载权重、起调度器线程 |

注意分工：`probe` 是身份检查（是不是我家的 config），尺寸/资源校验留给后面的 `validate` 与 `launch`；`validate` 是廉价的纯 CLI 规则（在昂贵的引擎加载之前），`launch` 才做重活。

#### 4.2.2 核心流程

一条线在 server 启动线上被触碰的顺序（与上表右列对应）：

```text
注册期（进程启动，一次性）
  augment_cli ×N     ← 把每条线的独占 flag 拼进同一条 CLI
  consumed_shared_args / arg_requirements ← 声明面，注册表据此自检

运行期（main 逐阶段）
  probe ×N           ← detect：恰好一条 Ok，其余 Err(reason)
  validate           ← 只对被检测出的线调用一次
  serve_plan         ← 同上；产出前端预装配需要的事实
  launch             ← spawn_blocking 中；返回 LaunchedEngine
```

#### 4.2.3 源码精读

必选的三个方法——合同里仅有的「必须由实现方签字」的部分：

[pegainfer-frontend/src/model_line.rs:336-344](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L336-L344) —— `name()` 返回 `&'static str` 家族名（日志、错误、`--help`，例如 `"Qwen3"`）；`probe()` 拿解析好的 `config.json`（`serde_json::Value`）做认领或拒绝，拒绝时返回 `Err(String)` 说明原因——这些字符串最终会出现在 `NoMatch` 错误里（见 4.4）。

[pegainfer-frontend/src/model_line.rs:378-382](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L378-L382) —— `launch()`：启动引擎（spawn 调度器线程、构建带元数据的 handle），返回 `anyhow::Result<LaunchedEngine>`。文档点明错误语义：失败是「深层上下文链（CUDA、权重、拓扑）」，调用方不会分支处理——所以用 `anyhow` 而不是枚举。

五个可省略的方法及其默认实现：

[pegainfer-frontend/src/model_line.rs:346-361](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L346-L361) —— `augment_cli` 默认把命令原样返回（本线无独占 flag）；`consumed_shared_args` 默认空（不消费任何共享 flag——用户显式给共享 flag 会被拒）；`arg_requirements` 默认空（无跨 flag 依赖；文档提醒把依赖声明在这里而非 clap 属性，因为 release 构建里 clap 的 schema 断言被禁用）。

[pegainfer-frontend/src/model_line.rs:363-376](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L363-L376) —— `validate` 默认无条件通过；`serve_plan` 默认 `ServePlan::default()`（单分区、非 prefill-only、无 LoRA、无默认工具解析器，见 [pegainfer-frontend/src/model_line.rs:325-334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L325-L334)）。

最小实现 vs 满配实现的对照：

[pegainfer-frontend/examples/echo-server.rs:64-92](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L64-L92) —— EchoLine 只实现 3 个必选方法：`name()` 返回 `"Echo"`；`probe()` 认领 `model_type == "echo"`；`launch()` 用 `spawn_scheduler` 起一个 EchoScheduler 并打包成 `Engine`。注释明说「无独占 CLI flag、无核心之外的共享 flag」，所以其余 5 个方法全部吃默认。

[pegainfer-qwen3/src/model_line.rs:132-179](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L132-L179) —— Qwen3Line 满配：`name()` 是 `"Qwen3"`；`probe()` 先查 `model_type`，再委托 [pegainfer-qwen3/src/config.rs:895-910](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/config.rs#L895-L910) 的 `probe_config_json` 检查 `architectures` 含 `Qwen3ForCausalLM`；`augment_cli` 挂上 `Qwen3Cli`（LoRA、KV P2P/PD、显存预算、batch-invariant、eagle3 等独占 flag）；`consumed_shared_args` 列出 15 个消费的共享 flag；`arg_requirements` 声明 4 组跨 flag 依赖。

[pegainfer-qwen3/src/model_line.rs:181-294](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L181-L294) —— `validate` 是一张手写的冲突矩阵（u2-l4 逐条精读）；`serve_plan` 只在 `enable_lora` 时改写 `lora_modules` 字段，其余用 `..ServePlan::default()` 展开。

#### 4.2.4 代码实践：用 `--help` 与单测核对方法表

1. **实践目标**：把上表与你机器上的二进制/源码对上号。
2. **操作步骤**：
   - 运行 `cargo run --release -- --help`（default 构建，含 qwen3）。找出 Qwen3 的独占 flag（如 `--enable-lora`、`--batch-invariant`、`--eagle3-draft-model-path`）与共享 flag（如 `--tp-size`、`--kv-offload`），分别对应 trait 的哪个方法？
   - 运行 `cargo test --release -p pegainfer-frontend --lib model_line`，观察注册表自检类测试（如 `registry_rejects_duplicate_exclusive_flag_spellings`）与 detect 类测试（如 `detect_conflict_names_both_lines`）。
3. **需要观察的现象**：`--help` 里同时出现所有编译线的独占 flag（default 构建即 Qwen3 一家）；单测全部通过（需先能构建工作区，见 u1-l2；无 GPU 也可跑 `--lib`）。
4. **预期结果**：独占 flag 来自 `augment_cli`（挂 `Qwen3Cli`），共享 flag 来自 `SharedArgs`（frontend 定义，u2-l2 精读）；哪些共享 flag 允许与 Qwen3 同用由 `consumed_shared_args` 决定。若本机尚未配置 CUDA 工具链，第二小步的运行结果**待本地验证**，可改为阅读 [pegainfer-frontend/src/model_line.rs:683-987](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L683-L987) 的测试断言。

#### 4.2.5 小练习与答案

**练习 1**：一个模型线没有任何独占 flag、也不读任何共享 flag，它必须实现哪几个方法？

**答案**：恰好 3 个：`name`、`probe`、`launch`。EchoLine 就是这个形状（echo-server.rs:64-92）；`augment_cli`/`consumed_shared_args`/`arg_requirements`/`validate`/`serve_plan` 全走默认。

**练习 2**：`probe` 返回的 `Err(String)` 最后去了哪里？

**答案**：被 `registry.detect` 收进 `rejections` 向量，拼成 `<线名>: <原因>` 条目；若无任何线认领，整体变成 `DetectError::NoMatch` 的错误消息（见 4.4.3 对 [pegainfer-frontend/src/model_line.rs:569-601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L569-L601) 的精读）。

**练习 3**：`arg_requirements` 的文档说「声明在这里，而不是 Clap 属性里」，为什么？

**答案**：这些要求会被注册表收集并经 `build_command` 的 `mut_args` 转成 clap 的 `requires_all`（[pegainfer-frontend/src/model_line.rs:552-564](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L552-L564)）；同时注册表在构造时显式校验每个被引用的 id 存在且归属正确——因为 clap 自带的 schema 断言在 release 构建里是禁用的，纯 clap 属性写法在 release 下失去这层保护（`ModelLineRegistry::new` 的文档，[pegainfer-frontend/src/model_line.rs:503-506](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L503-L506)）。

### 4.3 LaunchContext：launch 与 serve_plan 读到的世界

#### 4.3.1 概念说明

`LaunchContext` 是 trait 方法 `validate`/`serve_plan`/`launch` 的统一只读入参——server 把「已经裁决完的世界」打包递过来。设计要点有两个：

1. **frontend 不解析模型线的 CLI**。`matches` 是**合并后**的完整命令解析结果，但 frontend 不从中取任何模型专属字段；每条线用自己的 `#[derive(Args)]` 结构体经 `FromArgMatches` **取回自己的那份**。类型知识留在模型 crate，接缝上只传中性的 `ArgMatches`。
2. **config 与 probe 看到的是同一份**。`config` 就是 `probe` 刚认领的那个 `serde_json::Value`，保证「检测时看到的世界」与「启动时读到的世界」一致。

#### 4.3.2 核心流程

`LaunchContext` 在 main 里被构造**两次**——一次给廉价校验用，一次给引擎加载用：

```text
main()
  ├─ LaunchContext { model_path, config, shared, matches }   // 栈上第一次（validate/serve_plan 用）
  ├─ line.validate(&ctx, &provided)？
  ├─ line.serve_plan(&ctx)?
  └─ spawn_blocking(move || {
        LaunchContext { … }                                    // 闭包内第二次（launch 用）
        line.launch(&ctx)
     })
```

第二次构造不是浪费：`spawn_blocking` 的闭包 `move` 走 `shared`/`config`/`matches` 的所有权，闭包里必须基于被移动的值重建借用；外层的 `ctx` 只活到 serve_plan 那一行。

#### 4.3.3 源码精读

[pegainfer-frontend/src/model_line.rs:273-284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L273-L284) —— 四个字段：`model_path`（模型目录）、`config`（probe 认领过的同一份 config.json）、`shared`（共享 flag；线只应读自己 `consumed_shared_args` 声明过的子集）、`matches`（合并命令的完整解析结果，线用 `<Cli as FromArgMatches>::from_arg_matches` 恢复独占 flag）。

server 侧的两处构造：

[src/main.rs:179-186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L179-L186) —— 第一次构造后立刻喂给 `line.validate` 与 `line.serve_plan`。注意此刻引擎还没开始加载——`serve_plan` 必须只凭 CLI 就能回答前端的问题（u2-l5 展开）。

[src/main.rs:201-213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L201-L213) —— 引擎加载（权重→GPU）放进 `spawn_blocking` 与 HTTP 前端并行；闭包内重建 `LaunchContext` 后调用 `line.launch(&ctx)`，并用 `with_context` 给失败挂上 `failed to start {线名} engine` 的一层说明——这就是 anyhow 用法：**加上下文、不分支**。

Qwen3 的「取回来」模式——模型 crate 私有 CLI 的恢复函数：

[pegainfer-qwen3/src/model_line.rs:128-130](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L128-L130) —— `cli(ctx)` 一行：`Qwen3Cli::from_arg_matches(ctx.matches)`。`Qwen3Cli` 是 crate 私有结构体（[pegainfer-qwen3/src/model_line.rs:35-37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L35-L37)），外界不可见；`validate`/`serve_plan`/`launch` 三个方法都先调 `cli(ctx)` 取回它。`expect` 安全的原因：能走到这里，说明合并命令已通过 clap 解析，`Qwen3Cli` 的字段必然齐全。

launch 的返回类型是两代契约的交接点：

[pegainfer-frontend/src/engine/wiring.rs:162-180](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L162-L180) —— `LaunchedEngine` 是迁移期的二选一：`Handle`（legacy 每 token 引擎）或 `Stepped`（新一代 step 驱动引擎的 `Engine` 交接包）。文档明说等所有线迁移完就删掉枚举、只留 `Engine`。`From` 实现让 `.into()` 与 `.map(LaunchedEngine::Stepped)` 都能自然书写。

[pegainfer-qwen3/src/model_line.rs:296-367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L296-L367) —— Qwen3 的 `launch`：从 `ctx.shared` 折叠 offload/P2P/显存选项，从 `cli(ctx)` 取独占 flag，组装 `Qwen3LaunchOptions` 调 `crate::launch`，最后 `.map(LaunchedEngine::Stepped)`——Qwen3 已迁移到 step 契约。对照 [pegainfer-frontend/examples/echo-server.rs:78-91](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L78-L91)：echo 直接构造 `Engine { schedulers, info, lora }` 再 `.into()`（走 `From<Engine>` 变成 `Stepped`）。

`Engine` 结构（launch 的实际产出物，u3-l6 精读）：

[pegainfer-frontend/src/engine/wiring.rs:141-160](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L141-L160) —— `Engine { schedulers: Vec<LiveScheduler>, info: EngineInfo, lora: Option<LoraClient> }`。文档称必填字段就是「接入清单」：引擎不报容量就显式写 `None`（`EngineInfo` 的两个 `Option` 字段，[pegainfer-frontend/src/engine/wiring.rs:182-190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/engine/wiring.rs#L182-L190)），不能「忘了写」；`lora` 的 `Option` 本身就是能力声明，没有独立 flag 与之打架。

#### 4.3.4 代码实践：追踪 ctx 的消费面

1. **实践目标**：数清 Qwen3 的 `launch` 从 `LaunchContext` 读了几样东西，验证「线只读自己声明的子集」。
2. **操作步骤**：
   - 通读 [pegainfer-qwen3/src/model_line.rs:296-367](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L296-L367)，把每个 `shared.` 与 `cli.` 出现的字段列成两列。
   - 对照 [pegainfer-qwen3/src/model_line.rs:149-167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L149-L167) 的 `consumed_shared_args` 列表，检查 `shared.` 列里的每个字段是否都在声明清单上（注意 `model_path` 是核心 flag、`port`/`served_model_name` 由 server 自己消费，不归线管）。
   - 再看 [src/main.rs:179-184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L179-L184) 与 [src/main.rs:204-213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L204-L213)，确认 ctx 的两次构造内容一致。
3. **需要观察的现象**：`shared.` 读到的字段（kv_offload、kv_offload_host_gib、tp_size、cuda_graph、decode_overlap、decode_sm_pct、dflash_draft_model_path、no_prefix_cache、max_prefill_tokens、device_ordinal、dump_graph_png……）都出现在 consumed 清单里；`cli.` 读到的字段全部来自 `Qwen3Cli`。
4. **预期结果**：两张表完全对得上——任何越界读取（读了未声明的共享 flag）即使写了也会被 `validate_provided` 在更早阶段拦住（用户根本无法显式提供该 flag）。
5. 本实践为源码阅读型，无需构建；结论可直接从两段源码交叉核对。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `LaunchContext` 不直接把解析好的 `Qwen3Cli` 传给线？

**答案**：因为 frontend 不能命名模型 crate 的类型——那会反转依赖方向、破坏接缝（见 4.1）。中性载体 `clap::ArgMatches` + 各线自己的 `FromArgMatches` 恢复（`cli(ctx)` 模式），让类型知识留在模型 crate 内部。

**练习 2**：`launch` 的错误为什么用 `anyhow` 而 `validate` 用 `CliError`？

**答案**：`validate` 的错误是**规则**，server 与测试要认出它们（qwen3 的测试就断言错误消息包含特定 flag 名，见 [pegainfer-qwen3/src/model_line.rs:483-500](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-qwen3/src/model_line.rs#L483-L500)）；`launch` 的失败是 CUDA/权重/拓扑的深层链，server 只会 `with_context` 一层再向上抛，永远不会 `match` 它——trait 文档（model_line.rs:378-382）原话是「不是调用方会分支的东西，故用 anyhow」。

**练习 3**：echo 的 `launch` 返回 `Ok(Engine { … }.into())`，`.into()` 把它变成了什么？

**答案**：经 `From<Engine> for LaunchedEngine` 变成 `LaunchedEngine::Stepped(engine)`（wiring.rs:176-180）。而 server 的 LoRA 分支会检查这个变体：`let LaunchedEngine::Stepped(engine) = launched else { bail!(…) }`（[src/main.rs:221-223](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L221-L223)）——LoRA 服务要求 step 驱动引擎。

### 4.4 DetectError 与 CliError：接缝上的错误语言

#### 4.4.1 概念说明

接缝上的错误分两族，各自服务一类调用方：

- **`DetectError`**：detect 阶段的裁决结果。两个变体——`Conflict`（两条线抢同一 config，注册表 bug 级别的事件）与 `NoMatch`（没有编译进来的线认领，用户给错了模型目录或没开 feature）。server **要分支**：只在 `NoMatch` 时附加「用 `--features X` 重建」的提示（u1-l4 的 `feature_gate_hint`）。
- **`CliError`**：CLI 级拒绝。两个变体——`UnconsumedFlag`（某显式提供的 flag 不属于被检测的线，由注册表的 consume-or-reject 产生）与 `Rule`（线内跨 flag 规则的散文说明；注释明说「规则是给人读的散文，不做分类学」）。

两者都用 `thiserror` 派生 `Display` 模板：错误形状是 API 的一部分，消息文案是模板渲染的结果，因此测试可以逐字断言。

#### 4.4.2 核心流程

```text
probe Err(reason) ×N ──collect──> rejections: Vec<"线名: reason">
     ├─ 恰好一条 Ok ──> 放行该线
     ├─ 第二条 Ok ───> DetectError::Conflict { first, second }   // 立即返回
     └─ 零条 Ok ─────> DetectError::NoMatch { model_type, architectures, rejections }
                          └─ server 分支：NoMatch → 尝试 feature_gate_hint 拼接

validate_provided / validate 的失败 ──> CliError
     ├─ UnconsumedFlag { flag, line }   // "--dp-size is not used by Qwen3"
     └─ Rule(String)                    // "--lora-modules requires --enable-lora"
```

#### 4.4.3 源码精读

[pegainfer-frontend/src/model_line.rs:24-46](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L24-L46) —— `DetectError`：`Conflict` 的模板点名双方；`NoMatch` 的模板把 `model_type`（原文或 `missing`）、`architectures`（原文或 `missing`）与全部 rejections 用 `; ` 连接渲染。字段文档说明 rejections 是「每条编译进来的线一条 `<line>: <reason>`」。

[pegainfer-frontend/src/model_line.rs:48-66](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L48-L66) —— `CliError`：`UnconsumedFlag` 的模板 `--{flag} is not used by {line}`；`Rule(String)` 直接透传散文；`CliError::rule(...)` 是便捷构造器，Qwen3 的 validate 里大量使用。

detect 的收集逻辑（4.2 练习 2 的答案落点）：

[pegainfer-frontend/src/model_line.rs:569-601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L569-L601) —— 逐线 `probe`：第一条 `Ok` 记为认领者，第二条 `Ok` 立即返回 `Conflict` 并点名 `first`/`second`；每条 `Err(reason)` 追加 `"{name}: {reason}"` 到 rejections；零认领则把 config 的两个身份字段原样渲染进 `NoMatch`（缺失字段渲染为 `missing`）。

server 对 `NoMatch` 的唯一分支：

[src/main.rs:168-175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L168-L175) —— `registry.detect(&config).map_err(...)`：仅当 `NoMatch` 且 `feature_gate_hint` 命中时，把提示拼在错误后（`"{error}; {hint}"`）；其余错误原样上抛。这是「错误形状是 API」的直接受益者：分支靠 `matches!(error, DetectError::NoMatch { .. })` 完成。

consume-or-reject 的出口：

[pegainfer-frontend/src/model_line.rs:605-630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L605-L630) —— `validate_provided`：每个用户显式提供的 flag id 必须是核心 flag（`CORE_ARGS`）、被检测线 `consumed_shared_args` 声明的共享 flag、或该线独占 flag 之一；否则返回 `UnconsumedFlag`，flag 的长拼写经 `long_flag` 从命令上反查。

单元测试如何把错误消息钉死成 API：

[pegainfer-frontend/src/model_line.rs:905-915](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L905-L915) —— 断言错误消息**逐字等于** `"--line-b-flag is not used by LineA"`：模板即契约。

#### 4.4.4 代码实践：亲手触发两种错误

1. **实践目标**：在真实二进制上看到 `NoMatch` 与 `UnconsumedFlag` 两种错误形态，并把每条消息对应回 thiserror 模板。
2. **操作步骤**：
   - **NoMatch**：运行 `cargo run --release -- --model-path ../pegainfer-frontend/examples/echo-model`（default 构建，只编入 Qwen3）。
   - **UnconsumedFlag**：建一个只含 `config.json` 的目录，内容为 `{"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}`（probe 只查这两项，见 config.rs:895-910），然后运行 `cargo run --release -- --model-path <该目录> --dp-size 8`。`dp_size` 是 SharedArgs 的 flag 但不在 Qwen3 的 consumed 清单里。
3. **需要观察的现象**：
   - 第一步：报错含 `no compiled-in model line claims this config`，且 rejections 里有形如 `Qwen3: model_type "echo" is not "qwen3"` 的条目；**没有** feature 提示（echo 不是已知家族）。
   - 第二步：报错消息是 `--dp-size is not used by Qwen3`，并且发生在 `=== pegainfer - Qwen3 (GPU) ===` 横幅**之前**——校验先于引擎加载（u1-l3 的阶段顺序）。
4. **预期结果**：两条消息都能逐字对应 [model_line.rs:34-37](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L34-L37) 与 [model_line.rs:54-55](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L54-L55) 的模板。具体终端输出（含 anyhow 的 `Error:` 前缀格式）**待本地验证**。
5. 若无 CUDA 工具链无法构建二进制，替代方案：运行 `cargo test --release -p pegainfer-frontend --lib model_line` 并阅读 `detect_no_match_renders_identity_fields_verbatim`（[model_line.rs:868-881](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/src/model_line.rs#L868-L881)）与 `validate_provided_rejects_another_lines_flag_by_long_name` 的断言。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Conflict` 被视为注册表 bug 而不是用户错误？

**答案**：两条线认领同一 config 意味着两条线的 `probe` 身份检查有交集——这是接入时犯的错（身份重叠），与当下运行的用户无关。detect 对它**立即**返回（不再继续试探其余线），且模板点名双方，方便定位（见 model_line.rs:577-584）。

**练习 2**：`NoMatch` 的 rejections 为什么有价值？举一个使用场景。

**答案**：它把「每条编译进来的线为什么拒绝」一次性摊开。例如用户把 GLM 的 config 目录指给 stock 构建：rejections 只有 `Qwen3: model_type "glm_moe_dsa" is not "qwen3"`，一眼可见「不是路径错了，而是这条线根本没编进来」；配合 server 的 `feature_gate_hint` 分支（main.rs:168-175）给出 `--features glm52` 重建建议（u1-l4 已精读）。

**练习 3**：`CliError::Rule(String)` 为什么不做更细的变体分类？

**答案**：变体注释直接回答：规则是「面向用户的解释，是散文而非分类学」（model_line.rs:56-58）。没有代码需要 `match` 具体规则——server 只负责把它打印出来；枚举爆炸不会带来任何分支收益。

## 5. 综合实践：从 EchoLine 到 ReverseLine

把本讲全部内容串成一个可编译的产物：以 echo-server.rs 为模板，做一条**倒序吐回 prompt** 的新模型线。全程无 GPU、无权重。

**任务**：实现 `ReverseLine`——`name()` 返回 `"Reverse"`，`probe()` 认领 `model_type == "reverse"`，`launch()` 返回最简 `Engine`（调度器照抄 EchoScheduler，只改出队方向）。

**步骤**（在你自己的 checkout 里做练习，不影响上游）：

1. **复制模板**：`cp pegainfer-frontend/examples/echo-server.rs pegainfer-frontend/examples/reverse-server.rs`（examples 目录下的新文件会被 cargo 自动发现）。
2. **新 fixture**：建 `pegainfer-frontend/examples/reverse-model/`，放入两份文件：
   - `config.json`（示例代码）：
     ```json
     {
       "model_type": "reverse",
       "architectures": ["ReverseModel"],
       "vocab_size": 256,
       "max_position_embeddings": 4096
     }
     ```
   - `tokenizer.json`：直接复制 `examples/echo-model/tokenizer.json`（字节级分词器，两条线通用）。
3. **改三处名字**：`FIXTURE_MODEL_DIR` 指向 `reverse-model`；`MODEL_LINE`/`EchoLine` 改名为 `ReverseLine`（`name()` 返回 `"Reverse"`）；`probe()` 认领 `"reverse"`。
4. **改一行出队方向**：EchoScheduler 在 admit 时 `pending.reverse()`，随后每步 `pop()`（弹尾部）——于是按 prompt 正序吐回。Reverse 只需**删掉 `pending.reverse()` 那一行**（[echo-server.rs:129-130](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L129-L130)）：不反转直接 `pop()` 弹尾，输出的就是逆序 token。同时把 `main` 里默认 served name 从 `"echo"` 改成 `"reverse"`。
5. **编译并运行**：`cargo run -p pegainfer-frontend --example reverse-server`，然后：
   ```bash
   curl http://127.0.0.1:8000/v1/completions \
     -H 'content-type: application/json' \
     -d '{"model": "reverse", "prompt": "hello, pegainfer", "max_tokens": 64}'
   ```
6. **回答关键问题**（本实践的验收点）：
   - 哪些方法**必须**实现？——`name`、`probe`、`launch` 三个（trait 中仅有的无默认实现方法；如果你只数出两个，检查是否漏了 `name`）。
   - 哪些方法省略了？——`augment_cli`、`consumed_shared_args`、`arg_requirements`、`validate`、`serve_plan` 共 5 个，全部吃默认（无独占 flag、不消费共享 flag、无跨 flag 规则、默认单分区服务计划）。
7. **观察与预期**：completion 文本应是 prompt 的**token 逆序**。fixture 分词器按字节切词，所以大致是字符串反转（`hello, pegainfer` ≈ `retnigep ,olleh`）；空格与多字节字符的精确拼接方式取决于 tokenizer 的 detokenize，具体输出**待本地验证**。
8. **进阶（可选）**：给 ReverseLine 加一个独占 flag `--reverse-uppercase`（在 `augment_cli` 里挂一个 `clap::Arg`，用 4.3 的 `from_arg_matches` 模式取回，调度器里把输出 token 转大写）——体会第 4 个方法何时需要从默认转为手写。

## 6. 本讲小结

- **接缝**：server 只持有 `Vec<&'static dyn ModelLine>`，从不 import 模型 crate 的选项类型；接入新线 = 模型 crate 实现 trait + server 注册一行 + 一个 feature。
- **trait 面**：8 个方法，3 必选（`name`/`probe`/`launch`），5 默认（`augment_cli`/`consumed_shared_args`/`arg_requirements`/`validate`/`serve_plan`）；EchoLine 是 3 方法最小实现，Qwen3Line 是 8 方法满配实现。
- **LaunchContext** 是 `validate`/`serve_plan`/`launch` 的只读世界：模型目录、probe 认领过的同一份 config、共享 flag、合并命令的 `ArgMatches`；模型线用 `FromArgMatches` 自取独占 flag（`cli(ctx)` 模式），类型知识不越过接缝。
- **launch 返回 `anyhow::Result<LaunchedEngine>`**：失败是 CUDA/权重/拓扑的深层链，调用方只 `with_context` 传播不分支；`LaunchedEngine` 是迁移期二选一（legacy `Handle` / 新一代 `Stepped`），Qwen3 与 echo 都返回 `Stepped`。
- **错误语言**：`DetectError`（`Conflict`/`NoMatch`）与 `CliError`（`UnconsumedFlag`/`Rule`）用 thiserror 模板固定消息形状，server 只对 `NoMatch` 分支附加 feature 提示；测试逐字断言消息，模板即 API。

## 7. 下一步学习建议

- **u2-l2（SharedArgs）**：本讲反复出现的 `shared` 字段与 `consumed_shared_args` 的完整清单——共享参数面如何定义、谁消费、跨 flag 规则分两层（clap `requires_all` 与手写 `validate`）。
- **u2-l3（ModelLineRegistry）**：本讲一笔带过的注册期自检（flag id/拼写唯一性 panic、detect 的收集逻辑、consume-or-reject）的完整机制。
- **u2-l4（Qwen3Line）**：把本讲的满配实现拆开精读——独占 flag 分组、`validate` 冲突矩阵的写法、`launch` 的选项组装。
- 想先跑点东西：回头把综合实践的 ReverseLine 跑起来，再运行 `cargo run -p pegainfer-frontend --example echo-server` 对照原版行为；`Scheduler` trait 本身（EchoScheduler 实现的三个方法）属于引擎契约，u3-l5 会正式开讲。
