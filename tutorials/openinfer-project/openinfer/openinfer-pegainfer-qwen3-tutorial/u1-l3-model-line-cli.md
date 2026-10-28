# ModelLine：Qwen3 如何挂进服务器 CLI

## 1. 本讲目标

上一讲（u1-l1）我们把 `src/lib.rs` 的 25 个模块归了类，并留下一个悬念：服务器二进制 `pegainfer-server` 是纯派发（pure dispatch），它「从不命名任何模型 crate 的选项类型」，那 `pegainfer run --release -- --model-path models/Qwen3-4B` 这条命令里的每个 flag 是怎么变成引擎参数的？

学完本讲，你应该能够：

1. 说出 `ModelLine` trait 的 8 个 hook（`name` / `probe` / `augment_cli` / `consumed_shared_args` / `arg_requirements` / `validate` / `serve_plan` / `launch`）各自的职责，以及服务器启动过程中**调用每一个 hook 的准确时机**。
2. 读懂两个关键机制：`consumed_shared_args` 的「consume-or-reject」（共享 flag 按模型线认领）与 `arg_requirements` 的「按使用触发的依赖检查」。
3. 逐条读懂 `Qwen3Line::validate` 的跨标志互斥规则表（LoRA、batch-invariant、DFlash、decode-overlap、TP 的组合约束）。
4. 追踪 `Qwen3Cli` 的字段如何一路组装成 `Qwen3LaunchOptions`，最终进入 `crate::launch`。

本讲的三个最小模块：**Qwen3Cli**、**Qwen3Line::validate**、**Qwen3Line::launch**。

## 2. 前置知识

### 2.1 clap 的 derive 模式

PegaInfer 的 CLI 用 [clap](https://docs.rs/clap/) 的 derive API 构建。你只需要懂四个词：

- **`#[derive(Args)]` + `augment_args(cmd)`**：把一个 struct 的字段「追加」成一条命令的 flag。`Qwen3Cli` 就是这样一个 struct；`augment_cli` hook 调 `Qwen3Cli::augment_args(cmd)` 把 Qwen3 专属 flag 合入服务器的合并命令。
- **`from_arg_matches(matches)`**：反向操作——从一次成功解析的结果里恢复出这个 struct 的值。`cli(ctx)` 辅助函数每次都这样从 `ctx.matches` 里「取回」Qwen3 视图。
- **命名转换**：字段 `kv_pd_miss_wait_ms` 自动映射为 flag `--kv-pd-miss-w-ms`（snake_case → kebab-case）。
- **`requires_all`**：给某个 arg 挂上「如果你被显式提供，那么这些 arg 也必须被提供」的约束，由 clap 在解析期执行。

### 2.2 「模型线」这个词

**ModelLine（模型线）= 一个模型家族的一切服务侧知识**：如何从 `config.json` 认出它、它有哪些专属 CLI flag、flag 之间怎么互斥、以及如何启动引擎。每条模型线在各自 crate 里实现 `ModelLine` trait，向服务器暴露一个静态实例。Qwen3 的实例是 `MODEL_LINE`，服务器在 feature `qwen3` 打开时把它注册进注册表：

- [src/model_line.rs:L27-L29](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L27-L29) — `pub static MODEL_LINE: Qwen3Line = Qwen3Line;` 与空的单元结构体 `Qwen3Line`。
- 服务器注册点：[pegainfer-server/src/main.rs:L36-L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/src/main.rs#L36-L37)，整个注册表见 [main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/src/main.rs#L24-L43)。

### 2.3 「显式提供」（provided）与默认值的区别

`provided_args` 收集的是**用户在命令行或环境变量里显式写过**的 flag id（看 `ValueSource` 是 `CommandLine` 还是 `EnvVariable`）。带默认值的 flag 若用户没写，就**不算 provided**。这个区分是本讲的钥匙——很多规则只应该约束「用户主动做了某件事」的情况，而不是惩罚安静的默认值。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `pegainfer-qwen3/src/model_line.rs` | 本讲主角：`Qwen3Line` 对 `ModelLine` 的全部实现、`Qwen3Cli` 定义、三个值解析器、tests 模块（含 `validate_argv` 测试夹具） |
| `pegainfer-qwen3/src/lib.rs` | 组装终点：`Qwen3LaunchOptions`、`Qwen3LoraOptions`、`Qwen3OffloadOptions` 系列选项结构体，以及真正的 `launch()` 启动策略 |
| `pegainfer-qwen3/src/config.rs` | `probe_config_json`：`probe` hook 委托的身份检查 |
| `pegainfer-frontend/src/model_line.rs` | trait 与机制的**定义侧**：`ModelLine`、`LaunchContext`、`SharedArgs`、`ModelLineRegistry`、`parse_for_line`（仅阅读，理解机制用） |
| `pegainfer-server/src/main.rs` | 消费侧：hook 的调用时序（仅阅读） |

按 u1-l1 的分层，`model_line` 属于「契约接入」层：它站在引擎契约（u1-l4 的 `frontend_adapter`）的上游，把原始 CLI 翻译成 crate 内部的选项类型。

## 4. 核心概念与源码讲解

### 4.1 ModelLine trait：模型线与服务器之间的接缝

#### 4.1.1 概念说明

`pegainfer-server` 二进制要同时支持 Qwen3、DeepSeek、Kimi 等模型线，但每条线的 flag、规则、启动方式完全不同。如果这些知识写进服务器，服务器就会 import 每个模型 crate 的选项类型，任何模型改动都要动服务器。

`ModelLine` trait 把这道墙立起来：**模型线向下拥有自己的一切 CLI 知识，服务器向上只看到 trait 方法**。frontend 模块的文档注释说得很直白——「接入一条模型线 = 在模型 crate 里实现 `ModelLine` + 在服务器注册表里加一行，服务器不需要任何其他修改：参数路由、consume-or-reject 校验、config 识别全部从 trait 派生」。

对 Qwen3 学习者来说，这个 trait 是理解「我的 flag 去了哪」的地图。

#### 4.1.2 核心流程

8 个 hook 按服务器启动的时序排列（这是本讲最重要的表，建议自己抄一遍）：

| 阶段 | 调用的 hook / 机制 | 服务器代码位置 | 做什么 |
|------|------------------|--------------|--------|
| 1. 构建注册表 | `name` / `consumed_shared_args` / `augment_cli` / `arg_requirements` | `ModelLineRegistry::new`（main.rs:151） | 声明式注册 + 一组 `assert!` 自检（flag id 不撞车、依赖指向真实存在的 id） |
| 2. 构建合并命令 | `augment_cli`（经注册表） | `build_command`（main.rs:152） | 共享 flag + 每条线的专属 flag 合成一条命令；`arg_requirements` 被翻译成 clap 的 `requires_all` |
| 3. 解析 argv | ——（clap 执行） | `get_matches`（main.rs:157） | **依赖规则在这里被强制执行**：给了 `--kv-pd-miss-w-ms` 却没给 `--kv-pd-vllm-seed` 会在这里报错 |
| 4. 读 config.json → 识别模型 | `probe` | `registry.detect`（main.rs:168） | 逐条线问「这是你吗」；恰好一条接受，冲突/无人认领都报错 |
| 5. consume-or-reject | `consumed_shared_args`（间接） | `validate_provided`（main.rs:177） | 每个显式提供的 flag 必须是核心 flag、本线认领的共享 flag、或本线专属 flag |
| 6. 共享 flag 间规则 | ——（`SharedArgs::validate`） | main.rs:178 | 所有线通用的规则（如 `--dump-graph-png` 需要 `--cuda-graph=true`） |
| 7. 本线跨标志规则 | `validate` | main.rs:185 | Qwen3 的互斥表（4.4 节） |
| 8. 服务计划 | `serve_plan` | main.rs:186 | 引擎加载**之前**前端就要知道的事（LoRA 路由、分区数） |
| 9. 启动引擎 | `launch` | main.rs:211（在 `spawn_blocking` 里） | 组装 `Qwen3LaunchOptions` → `crate::launch` → 返回 `LaunchedEngine` |

注意一个分层原则在时序里显形：**失败发生得越早越便宜**。依赖错误在阶段 3（argv 解析）就爆，互斥错误在阶段 7（还没加载权重），而启动策略里的深层 ensure（lib.rs）抢在「TP 模型加载 + 图预捕获」这种昂贵动作之前。

#### 4.1.3 源码精读

trait 定义（仅 8 个方法，两个有默认实现）：

```rust
pub trait ModelLine: Send + Sync {
    fn name(&self) -> &'static str;
    fn probe(&self, config: &serde_json::Value) -> Result<(), String>;
    fn augment_cli(&self, cmd: clap::Command) -> clap::Command { cmd }
    fn consumed_shared_args(&self) -> &'static [&'static str] { &[] }
    fn arg_requirements(&self) -> &'static [ArgRequirement] { &[] }
    fn validate(&self, _ctx: &LaunchContext<'_>, _provided: &BTreeSet<String>) -> Result<(), CliError> { Ok(()) }
    fn serve_plan(&self, _ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> { ... }
    fn launch(&self, ctx: &LaunchContext<'_>) -> anyhow::Result<LaunchedEngine>;
}
```

见 [pegainfer-frontend/src/model_line.rs:L337-L383](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L337-L383)。`launch` 是唯一没有默认实现、必须手写的「重」hook；`validate` 的注释明确说它「在注册表的 consume-or-reject 通过之后运行」——即阶段 7 在阶段 5 之后。

`LaunchContext` 是所有 hook 共享的只读上下文：

- [pegainfer-frontend/src/model_line.rs:L273-L284](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L273-L284) — 四个字段：`model_path`（模型目录）、`config`（已解析且被 `probe` 接受的 config.json）、`shared`（共享 flag 的解析值）、`matches`（完整合并命令的解析结果）。注释点明：模型线只能通过 `from_arg_matches` 从 `matches` 里**恢复自己的专属 flag**。

Qwen3 的 `probe` 是两段式的：

- [src/model_line.rs:L129-L135](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L129-L135) — 先做快速身份检查（`model_type == "qwen3"`），再委托给 `crate::probe_config_json` 做完整校验（`architectures` 必须包含 `Qwen3ForCausalLM`），见 [src/config.rs:L896-L911](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/config.rs#L896-L911)。两段式的意义：错误信息里第一段给出人话（`model_type "glm_moe_dsa" is not "qwen3"`），第二段给出精确的架构字段问题；注册表的 `detect` 会把每条线的拒绝理由拼进 `NoMatch` 错误给用户看。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：把 4.1.2 的时序表从「读到的」变成「验证过的」。
2. **操作步骤**：
   - 打开 [pegainfer-server/src/main.rs:L146-L213](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-server/src/main.rs#L146-L213)，从 `main()` 第一行开始，给每一行标注它对应表中哪个阶段。
   - 特别注意 main.rs:168 的 `detect`：若两条线都接受同一 config 会发生什么？读 [pegainfer-frontend/src/model_line.rs:L569-L601](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L569-L601) 的 `DetectError::Conflict` 分支回答。
   - 再看 main.rs:204-213：为什么 `launch` 被包在 `spawn_blocking` 里？（答案就在那段代码上方的注释里：引擎加载与 HTTP 前端并发。）
3. **需要观察的现象**：`launch` 是唯一在 tokio 阻塞线程里跑的 hook——其余全部在 `main()` 主线程、引擎加载之前完成。
4. **预期结果**：一张与 4.1.2 表逐行对应的、带 main.rs 行号的时序标注。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `probe` 返回 `Result<(), String>` 而不是 `anyhow::Result`？
**答案**：`probe` 的错误不是失败，而是**投票信息**。注册表要把每条线的拒绝理由（`rejections`）收集起来拼进 `DetectError::NoMatch` 展示给用户，所以用简单的 `String`；而 `launch` 的错误是真正的失败（CUDA、权重、拓扑的深层上下文链），调用方不分支处理，用 `anyhow`。

**练习 2**：服务器二进制为 `--help` 解析所有模型线的专属 flag（哪怕该线未被检测到），这样设计有什么好处？
**答案**：见 [pegainfer-frontend/src/model_line.rs:L550-L553](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L550-L553) 的注释——合并命令使 `--help` 和解析**不依赖检测到的模型**；检测（阶段 4）发生在解析（阶段 3）之后，如果命令只含被测线的 flag，`--dp-size`（Kimi 线的 flag）这类「拼写合法但给错了模型」的输入就无法被统一拒绝。

**练习 3**：`MODEL_LINE` 为什么是 `static` 而不是每次构造？
**答案**：注册表持有 `&'static dyn ModelLine`（见 [pegainfer-frontend/src/model_line.rs:L385-L388](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L385-L388)），需要与程序同生命周期的 trait 对象引用；`Qwen3Line` 是零字段单元结构体，静态实例零成本。

### 4.2 Qwen3Cli：Qwen3 专属 CLI 段

#### 4.2.1 概念说明

服务器的全部 flag 分三类：

1. **核心 flag**（所有线都收）：`--model-path`、`--served-model-name`、`--port`、`--tool-call-parser`，定义在 `CORE_ARGS`（[pegainfer-frontend/src/model_line.rs:L69-L74](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L69-L74)）。
2. **共享 flag**（多家模型线合用，各线声明自己读哪些）：`--tp-size`、`--kv-offload`、`--cuda-graph` 等，定义在 `SharedArgs`。
3. **专属 flag**（只有本线认识）：Qwen3 的就是 `Qwen3Cli` 的 12 个字段。

`Qwen3Cli` 解决的问题：**Qwen3 特有的服务开关需要一个声明式的家**——字段名即 flag 名，doc comment 即 `--help` 文案，默认值即服务默认。

#### 4.2.2 核心流程

`Qwen3Cli` 的 12 个字段按功能分组：

| 分组 | 字段（flag） | 默认 | 用途 |
|------|-------------|------|------|
| LoRA | `enable_lora`（`--enable-lora`） | false | 开启 LoRA 服务模式 |
| LoRA | `lora_modules`（`--lora-modules`） | 空 | 启动时预装的适配器（vLLM 风格 `name=path` / JSON） |
| LoRA | `max_loras`（`--max-loras`） | 1 | 常驻适配器数上限 |
| LoRA | `max_lora_rank`（`--max-lora-rank`） | 64 | 支持的 rank 上限（受限于合法值集合） |
| P/D 数据面 | `kv_p2p_flush_on_finish` | false | P 角色： Finished 事件前屏障 KV 落盘+注册 |
| P/D 数据面 | `kv_pd_vllm_seed` | 无 | vLLM 兼容哈希的 PYTHONHASHSEED |
| P/D 数据面 | `kv_pd_vllm_namespace` | 无 | vLLM prefill 对端的 8 位 hex 命名空间 |
| P/D 数据面 | `kv_pd_miss_wait_ms` | 5000 | 零命中等待窗口（毫秒） |
| 显存 | `gpu_memory_utilization` | crate 默认 | 显存预算比例 |
| 显存 | `kv_cache_memory_margin_mib` | crate 默认 | KV 定尺后的额外保留（MiB） |
| 显存 | `kv_page_size` | crate 默认 | KV 页大小（16 或 64） |
| 数值 | `batch_invariant` | false | 逐位一致的批不变服务 |

三个字段带**自定义值解析器**（在解析单个值时就拒绝垃圾输入，而不是等到启动）：

- `parse_max_lora_rank_arg`：rank 必须属于 `[1, 8, 16, 32, 64, 128, 256, 320, 512]`。
- `parse_pythonhashseed`：必须是 `[0, 4294967295]` 的十进制整数且不带 `+` 前缀——注释解释了动机：「一个空或畸形的 seed 会派生出永远匹配不上对端的合法键空间——配置错误必须在这里失败，而不是变成慢请求」。
- `parse_pegaflow_namespace`：恰好 8 个**小写** hex 字符。

见 [src/model_line.rs:L336-L374](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L336-L374)。

#### 4.2.3 源码精读

`Qwen3Cli` 的定义与两处值得咀嚼的注释：

```rust
// Qwen3-exclusive CLI flags. Shared flags (`--tp-size`, `--kv-offload`, …)
// live in `SharedArgs`. (Regular comment: a doc comment would override the
// command about via clap's augment.)
#[derive(ClapArgs)]
#[allow(clippy::struct_excessive_bools)] // independent CLI flags, not a state machine
struct Qwen3Cli {
```

- [src/model_line.rs:L31-L36](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L31-L36)。第一个注释是个 clap 冷知识：`augment_args` 会把 struct 的 **doc comment 提升为命令的 about 文本**，所以这里必须用普通注释——否则 Qwen3 的 flag 说明会覆盖掉 `pegainfer` 服务器自己的命令描述。第二个注释是对 clippy 的答辩：这些 bool 是彼此独立的开关，不是状态机的状态。

看一个典型的字段（doc comment 就是 `--help` 文案，`value_parser` 把校验提前到解析期）：

```rust
/// Zero-hit wait window for --kv-pd-vllm-seed mode, in milliseconds: ...
#[arg(long, default_value_t = 5000)]
kv_pd_miss_wait_ms: u64,
```

见 [src/model_line.rs:L78-L83](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L78-L83)——注意它有默认值 5000，**没有显式提供时不会触发任何依赖规则**（4.3 节的关键伏笔）。

`augment_cli` hook 只有一行，把 `Qwen3Cli` 合入命令：

```rust
fn augment_cli(&self, cmd: clap::Command) -> clap::Command {
    Qwen3Cli::augment_args(cmd)
}
```

见 [src/model_line.rs:L137-L139](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L137-L139)。

而各 hook 里反复出现的取回辅助函数：

```rust
fn cli(ctx: &LaunchContext<'_>) -> Qwen3Cli {
    Qwen3Cli::from_arg_matches(ctx.matches).expect("Qwen3Cli parses from the merged command")
}
```

见 [src/model_line.rs:L120-L122](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L120-L122)。`expect` 在这里是安全的：`ctx.matches` 来自一次**已经成功**的合并命令解析，从中恢复子集不可能失败——这是「前置不变量允许后续代码使用 expect」的一个微缩范例（4.5 节还会再见到一次更大的）。

#### 4.2.4 代码实践（源码阅读型）

1. **实践目标**：不运行程序，仅凭源码推出 `--help` 会展示的 Qwen3 专属 flag 清单。
2. **操作步骤**：
   - 逐字段抄下 `Qwen3Cli`（[src/model_line.rs:L34-L108](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L34-L108)），把字段名转成 kebab-case 的 flag 拼写。
   - 为每个字段找默认值：带 `default_value_t` 的直接抄；`Option` 字段默认 `None`（flag 缺省即未提供）；`lora_modules: Vec<_>` 默认空。
   - 对照已有的单测自检：tests 模块里 `parses_supported_max_lora_rank` / `rejects_unsupported_max_lora_rank` / `lora_default_rank_is_64`（[src/model_line.rs:L458-L474](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L458-L474)）验证了哪些默认值与合法值集合？
3. **需要观察的现象**：`Qwen3LoraOptions` 的常量（[src/lib.rs:L49-L52](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L49-L52)）是 CLI 默认值的唯一来源——`Qwen3Cli` 里没有硬编码 `64`，而是引用 `Qwen3LoraOptions::DEFAULT_MAX_LORAS`。
4. **预期结果**：一张 12 行的「flag → 拼写 → 默认值 → 值解析器」表，且默认值都能追溯到 `src/lib.rs` 里的常量或 `Option`/`Vec` 类型本身。

#### 4.2.5 小练习与答案

**练习 1**：`--max-lora-rank 7` 会在哪个阶段被拒绝？错误信息长什么样？
**答案**：在阶段 3（argv 解析）就被拒绝——`value_parser = parse_max_lora_rank_arg` 由 clap 对每个提供的值调用。信息形如 `--max-lora-rank must be one of: 1, 8, 16, ...`（[src/model_line.rs:L336-L348](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L336-L348)）。它根本到不了 `validate`，更到不了引擎加载。

**练习 2**：为什么 `parse_pegaflow_namespace` 连大写 hex 都拒绝？
**答案**：见 [src/model_line.rs:L362-L374](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L362-L374)——命名空间是**对端 connector 启动时打印的 8 位摘要**，用户要照抄它；大写变体意味着抄错了或改写了，与其在网络层静默查不到，不如在敲命令时就拒绝。

**练习 3**：`kv_pd_miss_wait_ms` 的 doc comment 说它「必须低于 executor 的 15 秒远取上限（启动时强制）」。为什么这条不在 CLI 层强制？
**答案**：15 秒上限属于 executor 的内部不变量（u5 单元会讲到 remote-fetch deadline）；CLI 层只持有毫秒数，引用一个别的模块的魔法数会造成跨层耦合。分层原则：**CLI 层管语法与组合，启动策略管跨模块语义**。

### 4.3 consumed_shared_args 与 arg_requirements：标志归属与依赖的机制

#### 4.3.1 概念说明

这两个机制回答两个正交的问题：

- **`consumed_shared_args`（consume-or-reject）**：共享 flag 池子里，**这条线读哪些**？没被认领的共享 flag 若被用户显式提供（检测到本线时），直接拒绝——防止「flag 看起来生效了其实被无视」这种最阴险的配置事故。例如 `--dp-size` 是 Kimi/GLM 线的共享 flag，Qwen3 没认领，于是有专门的测试 `rejects_other_lines_flags` 钉住 `--dp-size is not used by Qwen3` 的行为。
- **`arg_requirements`（按使用触发的依赖）**：本线的 flag **显式出现时**还必须带上谁？它与 clap 属性式 `requires` 的区别写在 trait 注释里：「在这里声明而不是放在 Clap 属性里，这样 release 构建也会校验每个被引用的 id」——注册表构建时用 `assert!` 验证每条依赖的源和目标都真实存在且归属正确（自己的或共享的），杜绝悬空引用。

#### 4.3.2 核心流程

两条通路汇合于注册表：

**认领通路**（构建期，一次性）：

```text
Qwen3Line::consumed_shared_args ──► RegistryClaims（id→归属 的映射表）
Qwen3Line::augment_cli           ─► 同一张表登记专属 flag
断言：认领的共享 id 必须存在；id 与拼写（含长短别名）不得撞车
```

**依赖通路**（构建期声明 + 解析期执行）：

```text
Qwen3Line::arg_requirements ──► requirements: BTreeMap<source, targets>（构建期，断言每个 id 归属正确）
                             ──► build_command 里 mut_args 给每个 source 挂 requires_all
                             ──► clap 在 get_matches 时强制：source 被显式提供而 target 缺席 → 解析错误
```

Qwen3 声明的四条依赖规则（[src/model_line.rs:L161-L171](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L161-L171)）：

| source（显式提供时…） | 必须同时提供 | 语义 |
|---------------------|-------------|------|
| `kv_p2p_flush_on_finish` | `kv_p2p_metaserver_addr` | flush 要注册到 MetaServer，没网格就没得注册 |
| `kv_pd_vllm_seed` | `kv_p2p_metaserver_addr` + `kv_pd_vllm_namespace` | vLLM 兼容查询要网格 + 命名空间 |
| `kv_pd_vllm_namespace` | `kv_pd_vllm_seed` | 命名空间只在 vLLM 兼容模式里有意义 |
| `kv_pd_miss_wait_ms` | `kv_pd_vllm_seed` | 等待窗口只属于 vLLM 兼容模式 |

再叠加共享层自己的 `SHARED_ARG_REQUIREMENTS`（[pegainfer-frontend/src/model_line.rs:L223-L232](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L223-L232)）：`kv_p2p_metaserver_addr` 还要求 `kv_offload` + `kv_p2p_advertise_addr` + `kv_p2p_nics`。所以一条**完整合法**的 vLLM 兼容 P/D 命令要一次带齐五个伴随 flag——依赖是会**级联**的。

关键语义：**默认值不触发**。`kv_pd_miss_wait_ms` 默认 5000，只有用户亲手写了 `--kv-pd-miss-w-ms` 才要求 seed。这就是 `provided_args` 里 `ValueSource` 过滤的意义（[pegainfer-frontend/src/model_line.rs:L641-L661](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L641-L661)）。

#### 4.3.3 源码精读

Qwen3 认领的 15 个共享 flag id：

```rust
fn consumed_shared_args(&self) -> &'static [&'static str] {
    &[
        "cuda_graph", "dump_graph_png", "device_ordinal", "tp_size",
        "kv_offload", "kv_offload_host_gib", "kv_offload_hugepages",
        "kv_p2p_metaserver_addr", "kv_p2p_advertise_addr", "kv_p2p_nics",
        "no_prefix_cache", "max_prefill_tokens",
        "dflash_draft_model_path", "decode_overlap", "decode_sm_pct",
    ]
}
```

见 [src/model_line.rs:L141-L159](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L141-L159)。逐个对应：CUDA 图（3 个）、单卡/TP 拓扑（2 个）、KV offload 与 P2P 网格（6 个）、前缀缓存开关、prefill 步预算、DFlash 草稿模型、双流重叠（2 个）。反过来读这张表，它就是「Qwen3 服务面」的目录——`--dp-size` 不在其中，所以被拒。

consume-or-reject 的执行点（注册表按 provided 集合逐个 id 检查三类归属）：

- [pegainfer-frontend/src/model_line.rs:L605-L630](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L605-L630) — `validate_provided`：`CORE_ARGS ∪ detected.consumed_shared_args ∪ entry.own_ids` 之外的任何 provided id → `CliError::UnconsumedFlag`（`--dp-size is not used by Qwen3`）。

依赖从声明到 clap 的翻译：

- [pegainfer-frontend/src/model_line.rs:L552-L564](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L552-L564) — `build_command` 末尾的 `mut_args` 闭包：遍历每个 arg，若其 id 是某条依赖的 source，则 `arg.requires_all(targets)`。注释还记录了一个工程细节：`mut_arg` 会重排参数，所以不能逐个调用而要用 `mut_args` 一次性处理，以免 `--help` 顺序被搅乱。

归属断言（release 也生效，因为它们是 `Registry::new` 里主动跑的 `assert!`，不依赖 clap 的 debug 断言）：

- [pegainfer-frontend/src/model_line.rs:L464-L477](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L464-L477) — `validate_requirement`：source 必须属于声明者自己；target 必须是共享的或同线的；不得自我依赖。配套测试 `registry_rejects_unknown_requirement_target`（同文件 L968-972）用一个悬空依赖的 StubLine 钉住这条断言。

#### 4.3.4 代码实践（本讲主实践：补一个拒绝用例）

1. **实践目标**：为 `arg_requirements` 里的一条规则（`--kv-pd-miss-w-ms` 依赖 `--kv-pd-vllm-seed`）补一个拒绝用例，并通过运行证明依赖确实由 clap 在解析期强制执行。

2. **操作步骤**：
   - 打开 [src/model_line.rs:L376-L393](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L376-L393)，读懂测试夹具 `validate_argv`：它调 `parse_for_line(&MODEL_LINE, argv)`（构造单线注册表 → 合并命令 → 解析 → consume-or-reject → 共享规则），把任何解析错误包成 `CliError::rule`，再构造 `LaunchContext` 并调 `MODEL_LINE.validate`。也就是说**它一条通路穿过了阶段 3/5/6/7**。
   - 在 tests 模块末尾（建议放在 `rejects_other_lines_flags` 之后）仿照现有用例风格添加：

     ```rust
     #[test]
     fn kv_pd_miss_wait_requires_vllm_seed() {
         let error = validate_argv(&["pegainfer", "--kv-pd-miss-w-ms", "1000"])
             .expect_err("--kv-pd-miss-w-ms without --kv-pd-vllm-seed should be rejected");
         assert!(
             error.to_string().contains("--kv-pd-vllm-seed"),
             "{error}"
         );
     }
     ```

   - 运行（无需 GPU，纯 CPU 单测；冷构建需编译 CUDA 依赖，耐心等待）：

     ```bash
     cargo test --release -p pegainfer-qwen3 --lib model_line
     ```

3. **需要观察的现象**：
   - 新用例通过，且**没有打印 panic**——错误是 `validate_argv` 主动 `expect_err` 捕获的。
   - 顺手观察已有的 `rejects_other_lines_flags`（[src/model_line.rs:L448-L456](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L448-L456)）与你的新用例**报错来源不同**：前者错误来自 `validate_provided`（`UnconsumedFlag`），你的来自 clap 解析（`parse_for_line` 里 `try_get_matches_from` 的 `?`）——证明依赖检查发生在更早的阶段 3。
   - 反向对照：把 argv 换成 `["pegainfer", "--kv-pd-miss-w-ms", "1000"]`（不带最后一个 flag 时失败）与只写 `["pegainfer"]`（默认值 5000 不触发依赖，通过到 `validate` 并 Ok）。

4. **预期结果**：`model_line` 过滤器下全部用例（原有 10 个 + 新增 1 个）通过。断言选用子串 `--kv-pd-vllm-seed` 是刻意的：clap 的完整错误文案（"the following required arguments were not provided: ..."）措辞随版本可能微调，但缺失 flag 的长拼写必然出现在其中。本讲义写作环境无可用构建缓存，未替你实际运行——待本地验证（机制本身已由源码核对：`build_command` 的 `requires_all` + clap 的 requires 语义）。

#### 4.3.5 小练习与答案

**练习 1**：把 `["pegainfer", "--kv-pd-vllm-seed", "42"]` 传给 `validate_argv`，会报什么错？为什么？
**答案**：报缺失 `--kv-pd-vllm-namespace`（和/或 `--kv-p2p-metaserver-addr`，取决于 clap 报告顺序）的解析错误。因为 `kv_pd_vllm_seed` 的依赖目标是两个；而就算补上 namespace，`kv_p2p_metaserver_addr` 还会级联要求 `kv_offload` + `advertise_addr` + `nics`（共享层规则）。一次合法的 vLLM 兼容启动要带齐全部伴随 flag（见 4.3.2）。

**练习 2**：为什么依赖规则不直接写成 clap 属性（`#[arg(requires = "...")]`）？
**答案**：见 trait 注释（[pegainfer-frontend/src/model_line.rs:L357-L361](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L357-L361)）：注册表在构建时（包括 release）用 `assert!` 校验每条依赖的 id 归属——source 属于本线、target 是共享或本线、不得自指；clap 自身的等价 schema 断言在 release 构建里是关闭的（`Registry::new` 的文档注释，L504-505）。集中声明让整套依赖可被机器审查。

**练习 3**：`provided` 集合为什么用 `BTreeSet<String>` 而不是 `HashSet`？
**答案**：确定性迭代顺序。`validate_provided` 按集合迭代逐个检查，报错时（以及服务器日志 main.rs:192 打印 `user-set flags {provided:?}`）BTreeSet 的字典序输出稳定，便于测试断言与日志比对；性能在这里无关紧要。

### 4.4 Qwen3Line::validate：跨标志互斥规则表

#### 4.4.1 概念说明

`validate` 是阶段 7 的 hook，处理** clap 语法管不了的语义**：flag 之间的组合约束。它运行在 consume-or-reject 与共享规则之后、引擎加载之前，输入是 `LaunchContext`（可读已解析的一切值）和 `provided` 集合（可判断「用户是否显式做过某事」），输出 `Ok(())` 或一条带人话解释的 `CliError::Rule`。

为什么要一张显式的互斥表？因为 Qwen3 的服务特性各自占用或锁定 GPU 资源的同一段：CUDA Graph 要指针稳定、LoRA 要在步间换权重、batch-invariant 要 chunk 边界逐位可复现、decode-overlap 要改流拓扑、TP 要均分 KV。**这些「我要独占某机制」的声明两两冲突**，validate 就是把这些冲突翻译成用户能看懂的拒绝理由，让错误发生在敲命令时而不是服务中途。

#### 4.4.2 核心流程

`validate` 的结构是三个顺序块（短路返回第一个命中）：

```text
块 1：LoRA 入场检查
  --lora-modules 非空 但 没开 --enable-lora        → 拒
  显式给了 --max-loras / --max-lora-rank 但没开    → 拒（默认值不罚）

块 2：图导出 × LoRA、重叠 × TP
  --dump-graph-png 且 --enable-lora                → 拒（LoRA 禁用 CUDA Graph，无图可导）
  --decode-overlap ≠ off 且 tp_size > 1            → 拒（TP 下没有 prefill/decode 重叠）

块 3：if batch_invariant { 五连拒 }  +  if dflash { 三连拒 }
```

完整规则矩阵（每行都对应 `validate` 里一段 `if`）：

| # | 组合 | 判定 | 源码给的理由 |
|---|------|------|-------------|
| 1 | `--lora-modules` 有值 + 未 `--enable-lora` | 拒 | 模块列表只在 LoRA 模式有意义 |
| 2 | 显式 `--max-loras`/`--max-lora-rank` + 未 `--enable-lora` | 拒 | 同上；注意用 `provided.contains` 判「显式」 |
| 3 | `--dump-graph-png` + `--enable-lora` | 拒 | LoRA 关闭 CUDA Graph，没有图可导 |
| 4 | `--decode-overlap` ≠ off + `tp_size > 1` | 拒 | 单卡专属；TP 下无 prefill/decode 重叠 |
| 5 | `--batch-invariant` + `--enable-lora` | 拒 | 二者互斥，一次只能开一个 |
| 6 | `--batch-invariant` + `--decode-overlap` | 拒 | 流覆盖会在运行时逼停 pinned GEMM |
| 7 | `--batch-invariant` + `--kv-offload` | 拒 | offload 保持前缀匹配开启；主机层命中会把 prompt 的 chunk 边界挪离请求本地网格 |
| 8 | `--batch-invariant` + `--dflash-draft-model-path` | 拒 | 二者互斥 |
| 9 | `--batch-invariant` + `tp_size > 1` | 拒 | 二者互斥 |
| 10 | `--batch-invariant` 且未 `--no-prefix-cache` | 拒 | **要求**而非互斥：前缀缓存命中同样会移动 chunk 边界 |
| 11 | `--dflash-draft-model-path` + `--enable-lora` | 拒 | 二者互斥 |
| 12 | `--dflash-draft-model-path` + `--kv-offload` | 拒 | 二者互斥 |
| 13 | `--dflash-draft-model-path` + `tp ≠ 1` | 拒 | DFlash 当前仅单卡 |

读表的两个技巧：**规则 7/10 的理由是同一条物理约束的两面**（batch-invariant 要求每个 prompt 的 prefill chunk 切分只取决于请求自身，任何外部来源的 KV 复用都会破坏这一点）；**规则 10 是「requires」语义**写在 `validate` 里而不是 `arg_requirements` 里，因为它要跨「专属 flag + 共享 flag」组合判断且理由需要解释，而 `arg_requirements` 只做无条件的伴随声明。

#### 4.4.3 源码精读

两种「用户提供」判定方式的对比，是 `validate` 里最耐读的细节：

```rust
if !cli.enable_lora && !cli.lora_modules.is_empty() {
    return Err(CliError::rule("--lora-modules requires --enable-lora"));
}
if !cli.enable_lora
    && (provided.contains("max_loras") || provided.contains("max_lora_rank"))
{
    return Err(CliError::rule(
        "--max-loras and --max-lora-rank require --enable-lora",
    ));
}
```

见 [src/model_line.rs:L180-L189](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L180-L189)。`lora_modules` 用**值非空**判定（空列表 = 没有意图，默认即空）；`max_loras`/`max_lora_rank` 用 **provided 判定**（它们有默认值，用户没写时值非默认地「存在」，但那不是用户的意图）。

batch-invariant 块的拒绝文案本身就是文档：

```rust
if shared.kv_offload {
    return Err(CliError::rule(
        "--batch-invariant is not supported with --kv-offload: offload keeps prefix matching \
         on (--no-prefix-cache only disables HBM retention there), and a host-tier prefix hit \
         shifts a prompt's chunk boundaries off the request-local grid",
    ));
}
```

见 [src/model_line.rs:L211-L217](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L211-L217)。注意它顺带教了用户一个反直觉事实：offload 模式下 `--no-prefix-cache` 只关 HBM 复用、不关匹配——所以「我已开了 no-prefix-cache」救不了这个组合。规则 10（L228-233）的非 offload 版本则解释另一半。

DFlash 块见 [src/model_line.rs:L235-L251](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L235-L251)。已有测试对这块的覆盖方式：`graph_png_dump_rejects_lora`（[src/model_line.rs:L432-L446](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L432-L446)）演示了「断言错误信息子串」的写法——测试锚定的是**面向用户的文案**，不是内部错误类型，这样重构错误类型不会碎一地测试。

#### 4.4.4 代码实践（源码阅读 + 预测验证型）

1. **实践目标**：不看运行结果，先纸面预测 `validate` 对若干 argv 的裁决，再用测试证据核对。
2. **操作步骤**：
   - 对下面每条 argv，写出「通过 / 拒绝 + 命中规则 #」：
     - a. `["pegainfer", "--batch-invariant"]`
     - b. `["pegainfer", "--batch-invariant", "--no-prefix-cache"]`
     - c. `["pegainfer", "--batch-invariant", "--no-prefix-cache", "--tp-size", "2"]`
     - d. `["pegainfer", "--enable-lora", "--max-lora-rank", "16"]`
     - e. `["pegainfer", "--max-loras", "2"]`
   - 核对：b/d 应通过（不命中任何规则）；a 命中规则 10；c 命中规则 9（batch-invariant 块在 DFlash 块之前，`tp_size > 1` 的检查在块内第 5 位）；e 命中规则 2。依据即 [src/model_line.rs:L200-L234](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L200-L234) 的判定顺序。
   - （可选，需本地跑）仿照 4.3.4 的方式为 a 与 c 各补一个 `validate_argv` 用例，断言错误信息分别包含 `requires --no-prefix-cache` 与 `not supported with --tp-size > 1`。
3. **需要观察的现象**：同一批 flag 的拒绝顺序由代码中的 `if` 排列决定——若一条 argv 同时命中多条规则，只有**最前面**那条的文案出现。
4. **预期结果**：五条预测全对；若补了用例，`cargo test --release -p pegainfer-qwen3 --lib model_line` 全绿。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：规则 3（dump-graph-png × LoRA）与阶段 6 的 `--dump-graph-png requires --cuda-graph=true`（共享规则）是什么关系？
**答案**：层层递进的三道闸：共享规则保证「要导图必须开图」；Qwen3 的规则 3 保证「LoRA 模式下连图都没有，更别提导出」；到了 `crate::launch`（lib.rs L279-289）还有第三道——`dump_graph_png` 存在时 `ensure!` CUDA Graph 已启用且无 LoRA。多道闸不是冗余：`validate` 只覆盖 CLI 路径，`lib::launch` 还服务不经 CLI 的库调用者。

**练习 2**：为什么规则 2 用 `provided.contains(...)` 而规则 1 用 `!cli.lora_modules.is_empty()`？换成对方的方式各会出什么问题？
**答案**：规则 1 若用 provided 判定，`--lora-modules` 是 `Vec` 且无法「提供了空值」，语义上没有歧义，值判空即意图判空，更直接。规则 2 若用值判定则永远为真（`max_loras` 有默认值 1，字段总是「有值」），会把没写这两个 flag 的普通启动全部拒绝——所以必须判 provided。原则：**有默认值的标量看 provided，无默认值的容器看值**。

**练习 3**：`--decode-overlap stream --tp-size 2` 会在阶段 7 被规则 4 拒掉。但 lib.rs 里还有一条 `decode-overlap is unsupported under tensor parallelism` 的 `ensure!`（L306-309）。为什么要说两遍？
**答案**：见该 ensure 上方的注释（[src/lib.rs:L304-L309](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L304-L309)）：load-bearing 的守卫在 `enable_decode_overlap`（executor 层），但在 `launch` 提前再拒一次，**省掉整个 TP 模型加载 + 图预捕获之后才失败**的代价。CLI 层的规则 4 则把失败再提前到权重加载之前。「同一不变量，三层设防，越早越便宜。」

### 4.5 Qwen3Line::launch：从 CLI 到 Qwen3LaunchOptions 的组装

#### 4.5.1 概念说明

`launch` 是 trait 的最后一个 hook（阶段 9，跑在服务器的 `spawn_blocking` 线程里），做一件事：**把两份解析结果（`Qwen3Cli` 视图 + `SharedArgs`）折叠加工成一份 `Qwen3LaunchOptions`，交给 `crate::launch`**。它自己不做任何真正的启动工作——启动策略（TP→设备映射、LoRA↔CUDA Graph 排斥、低层入口分发）归 `crate::launch` 所有，那是 u1-l4 的内容。

为什么不 let `launch` hook 直接拉起引擎？trait 文档注释给了一句架构原则的注脚：选项结构体的 doc comment 写着「**该策略与模型住在一起，而不是漏进服务器**」——`Qwen3Line::launch` 只做翻译，`crate::launch` 拥策略，两层都不会把模型知识泄漏给服务器。

#### 4.5.2 核心流程

组装管线（每一步都是纯翻译，无 I/O）：

```text
cli = cli(ctx)                    # 取回 Qwen3Cli 视图
shared = ctx.shared               # 取共享 flag

① offload 组装：
   shared.kv_offload?
     ├─ 否 → Qwen3OffloadOptions::disabled()
     └─ 是 → enabled(GiB→bytes) + hugepages
              ├─ metaserver+advertise 都在 → 叠加 .with_p2p(P2p{…, flush_on_finish})
              └─ seed 在 → 再叠加 .with_vllm_compat(VllmCompat{seed, namespace, miss_wait})

② lora 组装：enable_lora? → Some(Qwen3LoraOptions{max_loras, max_lora_rank}) : None

③ margin 换算：MiB → bytes（checked_mul，溢出给人话错误）

④ memory：Qwen3MemoryOptions::new(util, margin_bytes, page_size).validate()?

⑤ decode_overlap：CliDecodeOverlap → DecodeOverlap（resolve_decode_overlap）

⑥ crate::launch(model_path, Qwen3LaunchOptions{ …15 个字段… })
       └─ 返回 Engine → .map(LaunchedEngine::Stepped)
```

`Qwen3LaunchOptions` 的字段来源对照（这是本讲「追踪组装路径」学习目标的落点）：

| `Qwen3LaunchOptions` 字段 | 来源 | 备注 |
|--------------------------|------|------|
| `device_ordinal` | `shared.device_ordinal` | 单卡时用；TP 时被忽略 |
| `tp_size` | `shared.tp_size` | |
| `cuda_graph` | `shared.cuda_graph` | LoRA 时会被 lib::launch 强制关 |
| `dump_graph_png` | `shared.dump_graph_png` | |
| `offload` | ① 的产物 | 三层叠加的 builder 链 |
| `no_prefix_cache` | `shared.no_prefix_cache` | |
| `max_prefill_tokens` | `shared.max_prefill_tokens.unwrap_or(DEFAULT)` | 共享 flag 是 `Option`，缺省落到 crate 默认 |
| `memory` | ④ 的产物 | 唯一在组装期就 `validate()?` 的 |
| `lora` | ② 的产物 | `Option` 即开关 |
| `decode_overlap` | ⑤ 的产物 | 枚举换枚举，GreenCtx 带上 `decode_sm_pct` |
| `batch_invariant` | `cli.batch_invariant` | |
| `dflash_draft_model_path` | `shared.dflash_draft_model_path` | |

#### 4.5.3 源码精读

offload 的条件叠加是 `launch` 里最像「小语言」的一段：

```rust
let offload = if shared.kv_offload {
    let bytes = (shared.kv_offload_host_gib * f64::from(1u32 << 30)) as usize;
    let mut offload = Qwen3OffloadOptions::enabled(bytes);
    offload.use_hugepages = shared.kv_offload_hugepages;
    if let (Some(metaserver_addr), Some(advertise_addr)) = (
        shared.kv_p2p_metaserver_addr.clone(),
        shared.kv_p2p_advertise_addr.clone(),
    ) {
        offload = offload.with_p2p(Qwen3P2pOptions { /* … */ });
    }
    if let Some(seed) = cli.kv_pd_vllm_seed.clone() {
        offload = offload.with_vllm_compat(Qwen3VllmCompatOptions { /* … */ });
    }
    offload
} else {
    Qwen3OffloadOptions::disabled()
};
```

见 [src/model_line.rs:L266-L294](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L266-L294)。三个层次逐级可选：**主机层 KV（kv_offload）→ P2P 网格（metaserver+advertise 同时在才挂）→ vLLM 兼容（有 seed 才挂）**。`with_p2p`/`with_vllm_compat` 是 `[must_use]` 的 builder 方法（[src/lib.rs:L179-L189](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L179-L189)），漏接返回值编译器会提醒。

前置不变量换来的那个 `expect`：

```rust
if let Some(seed) = cli.kv_pd_vllm_seed.clone() {
    offload = offload.with_vllm_compat(Qwen3VllmCompatOptions {
        python_hash_seed: seed,
        namespace: cli
            .kv_pd_vllm_namespace
            .clone()
            .expect("clap requires kv_pd_vllm_namespace"),
        miss_wait: std::time::Duration::from_millis(cli.kv_pd_miss_wait_ms),
    });
}
```

见 [src/model_line.rs:L281-L290](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L281-L290)。`expect` 的安全性完全由 4.3 的依赖规则背书：`kv_pd_vllm_seed` 的 `arg_requirements` 保证 seed 出现时 namespace 必然在场（clap 已在解析期强制）——**这正是「声明式依赖」存在的意义：让下游代码敢于 expect**。

余下的直译段（margin 的防溢出换算 + 交给 crate::launch）：

```rust
let kv_cache_memory_margin_bytes = cli
    .kv_cache_memory_margin_mib
    .checked_mul(1 << 20)
    .ok_or_else(|| anyhow::anyhow!("--kv-cache-memory-margin-mib is too large: {}", …))?;
crate::launch(
    ctx.model_path,
    Qwen3LaunchOptions { /* 15 个字段一一落位 */ },
)
.map(LaunchedEngine::Stepped)
```

见 [src/model_line.rs:L299-L332](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L299-L332)。两处细节：MiB→bytes 用 `checked_mul` 并把溢出翻译成带 flag 名的人话错误（ usize 上的 `<<20` 乘法溢出在极端值下是真实风险，而这是 u2-l3 要讲的 sizing 防溢出哲学在 CLI 层的同款反射）；`LaunchedEngine::Stepped` 标明返回的是步进驱动引擎——前端如何 pump 它，正是 u1-l4 的主题。

`serve_plan` 顺带一提（阶段 8）：

```rust
fn serve_plan(&self, ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> {
    let cli = cli(ctx);
    Ok(ServePlan {
        lora_modules: cli.enable_lora.then(|| cli.lora_modules.clone()),
        ..ServePlan::default()
    })
}
```

见 [src/model_line.rs:L255-L261](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L255-L261)。它回答前端「引擎加载完之前就需要知道的事」：LoRA 路由开不开、预装哪些适配器。`..ServePlan::default()` 的写法也有讲究——frontend 的 `ServePlan` 文档注释说明：字面量全列会在未来加字段时弄坏其他模型 crate 的编译，所以约定只拼写自己设置的字段。

#### 4.5.4 代码实践（源码阅读型：画组装管线）

1. **实践目标**：给一条真实命令画出「flag → 选项结构体 → crate::launch」的字段级数据流图。
2. **操作步骤**：
   - 取这条（vLLM P/D decode 节点的典型启动）：

     ```bash
     pegainfer --model-path models/Qwen3-4B \
       --kv-offload --kv-offload-host-gib 16 \
       --kv-p2p-metaserver-addr http://10.0.0.2:50056 \
       --kv-p2p-advertise-addr 10.0.0.1:50057 \
       --kv-p2p-nics mlx5_0 \
       --kv-pd-vllm-seed 42 \
       --kv-pd-vllm-namespace 0a1b2c3d \
       --kv-pd-miss-w-ms 3000
   ```

   - 对命令里的每个 flag，沿 [src/model_line.rs:L263-L333](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L263-L333) 标注它最终落入 `Qwen3LaunchOptions` 的哪个字段、途经哪个中间结构（`Qwen3OffloadOptions` → `Qwen3P2pOptions` / `Qwen3VllmCompatOptions`）。
   - 再标注：这条命令缺了 `--kv-pd-vllm-namespace` 会怎样？（4.3 依赖规则在阶段 3 拒绝）`--kv-pd-miss-w-ms 20000` 会怎样？（CLI 层不拒——见 4.2 练习 3，启动时由 executor 的 15s 上限拦截，待 u5 验证。）
3. **需要观察的现象**：`Qwen3LaunchOptions` 里**没有任何字段**直接来自 `Qwen3Cli` 的 LoRA 值以外的默认常量——所有默认值要么在 `SharedArgs`（如 `cuda_graph=true`）、要么在 crate 常量（如 `DEFAULT_MAX_PREFILL_TOKENS`）、要么在选项结构体自己的 `Default`（如 `Qwen3MemoryOptions` 之外的 `Qwen3LoraOptions`）。
4. **预期结果**：一张约 15 条边的字段级数据流图；`miss_wait` 一条边要穿过 `Duration::from_millis` 换算。

#### 4.5.5 小练习与答案

**练习 1**：`launch` hook 里 `gpu_memory_utilization` 等值没有中间换算，为什么 `kv_cache_memory_margin_mib` 却要？
**答案**：单位。前者是无量纲比例（f64 直传）；后者是 MiB，要换算成 crate 内部统一单位字节，而 `usize × (1<<20)` 在理论极端值下会溢出——溢出后若静默回绕，会算出一个偏小的 margin，导致 KV 定尺侵占保留区，所以必须显式失败。对比 `kv_offload_host_gib` 的 GiB→bytes（L267）用 f64 乘法后 `as usize`：f64 乘法不溢出回绕，风险面不同。

**练习 2**：`resolve_decode_overlap` 把 `CliDecodeOverlap::Stream` 翻译成 `DecodeOverlap::SharedSm`——为什么 CLI 枚举和内部枚举不共用一个？
**答案**：CLI 枚举是**用户词汇**（`off`/`stream`/`green-ctx`，面向 `--help`，所有可重叠的模型线共用，见 [pegainfer-frontend/src/model_line.rs:L82-L92](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L82-L92)）；内部枚举是**实现词汇**（`Off`/`SharedSm`/`GreenCtx{decode_pct}`，携带参数，定义在 green_ctx 模块旁，见 [src/lib.rs:L86-L88](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L86-L88)）。翻译函数（[src/model_line.rs:L110-L118](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/model_line.rs#L110-L118)）正是两层词汇的官方字典；共享层还有配套规则「`--decode-sm-pct` 只在 green-ctx 模式可设」。

**练习 3**：`launch` hook 的签名返回 `anyhow::Result<LaunchedEngine>`，而 `validate` 返回 `Result<(), CliError>`。为什么错误类型不同？
**答案**：见 trait 文档注释（[pegainfer-frontend/src/model_line.rs:L377-L382](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L377-L382)）：`validate` 的错误要被服务器**区分对待**吗？不——但它们必须带稳定的、面向用户的规则文案（`CliError` 的两种变体 `UnconsumedFlag`/`Rule` 都是给终端用户的）；`launch` 的失败是 CUDA/权重/拓扑的深层上下文链，「调用方不对其分支——故用 anyhow」。

## 5. 综合实践

**任务：当一次「人肉 ModelLine」——为一个假想 flag `--qwen3-spec-boost N` 走完本讲全部四个机制。**

假设你要给 Qwen3 加一个投机解码加速旋钮（N 为草稿数）。不许改 frontend，只改 `pegainfer-qwen3`。请依次产出：

1. **`Qwen3Cli` 字段**（4.2）：写出字段定义——含 doc comment（`--help` 文案要说明默认值与含义）、`default_value_t`、以及是否需要 `value_parser`（提示：若 N 有合法区间，参照 `parse_max_lora_rank_arg` 写解析器并给出拒绝文案）。注意用普通注释还是 doc comment的取舍。
2. **依赖声明**（4.3）：该 flag 依赖投机解码开关（现实中是共享 flag `dflash_draft_model_path`）。写出一行 `arg_requirements` 条目；然后回答：如果把它声明成依赖**本线另一个专属 flag**，注册表的哪条断言保护你不写错 id？如果依赖一个**别线的专属 flag**呢？
3. **互斥规则**（4.4）：`--batch-invariant` 与投机解码已经互斥（规则 8）——你的新 flag 要加进哪一块？写出这条 `if` 与拒绝文案（文案要讲出**机制理由**，像规则 6/7 那样，而不是干巴巴的 "not supported"）。
4. **组装**（4.5）：在 `launch` hook 里加两行——把值翻译进 `Qwen3LaunchOptions`（先给该结构体加字段的方案也要写出来）。若默认值应落在 crate 常量上，指出放哪（参照 `DEFAULT_MAX_PREFILL_TOKENS` 的再导出方式，[src/lib.rs:L37](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L37)）。
5. **验证**（4.3/4.4 的实践迁移）：仿照 `kv_pd_miss_wait_requires_vllm_seed` 的写法，为「给了 `--qwen3-spec-boost` 却没开投机解码」补一个 `validate_argv` 拒绝用例；再为「互斥规则」补一个。跑 `cargo test --release -p pegainfer-qwen3 --lib model_line`。

**参考答案要点**（先自己做再对照）：
- 1：`value_parser` 应校验 N ∈ [1, 上限] 并复用 `Qwen3LoraOptions::supported_..._display()` 风格的文案拼接；doc comment 写默认与「仅在 DFlash 投机解码开启时生效」。
- 2：`("qwen3_spec_boost", &["dflash_draft_model_path"])`。依赖本线专属 flag 时 `validate_requirement` 检查 target_owner 是 `Shared` **或** 本线——两条断言都保你；依赖别线专属 flag 会被 `requires unknown or foreign argument id` 断言拦下（注册表构建即 panic，测试 `registry_rejects_unknown_requirement_target` 可证）。
- 3：加进 DFlash 块（L235-251）或 batch-invariant 块（规则 8 处已天然覆盖，因为你的 flag 依赖 dflash 而 dflash 已与 batch-invariant 互斥——**先想清楚是否真的需要新规则**，这是最佳答案：不需要，传递性已封死）。
- 4：`spec_boost: shared.dflash_draft_model_path.is_some().then_some(cli.qwen3_spec_boost)` 或直接透传让 lib::launch 判断；常量放 `src/spec_env.rs` 侧并经 lib.rs 再导出（真实代码里投机旋钮正是环境变量 + `spec_env` 校验的路线，u6-l7 会展开——你独立推导出的方案可以和它对照）。

## 6. 本讲小结

- **`ModelLine` 是模型线与服务器之间唯一的接缝**：服务器对 Qwen3 的全部了解 = `MODEL_LINE` 静态实例上的 8 个 hook；接入新模型线不需要动服务器的派发逻辑。
- **hook 的调用时序是分层的失败阶梯**：注册断言（构建期）→ clap 依赖（解析期）→ consume-or-reject → 共享规则 → `validate` 互斥 → `lib::launch` 的启动期 ensure——同一不变量可多层设防，失败越早越便宜。
- **`consumed_shared_args` 是「Qwen3 服务面」的目录**（15 个共享 flag），`arg_requirements` 是按使用触发、会级联的依赖声明；两者都以 provided（显式提供）为语义基准，默认值永不触发。
- **`validate` 的互斥规则表（13 条）不是任意的**：LoRA/批不变/DFlash/重叠/TP 各自锁定 GPU 机制的同一段资源；规则 7/10 共享同一条物理约束（外部 KV 复用会移动请求本地 chunk 网格）。
- **`launch` hook 是纯翻译层**：`Qwen3Cli` + `SharedArgs` → builder 链（offload 三层叠加）→ `Qwen3LaunchOptions` → `crate::launch`；策略归 crate，`expect` 的底气来自上游的声明式依赖。

## 7. 下一步学习建议

下一讲 **u1-l4「引擎契约与启动装配：frontend_adapter 全景」**顺着自己的尾巴往下走：本讲的 `launch` hook 把球交给了 `crate::launch`（lib.rs）与 `frontend_adapter::start_qwen3`——下一讲跟踪 `Engine`/`Scheduler`/`RequestLedger` 的职责边界、`spawn_scheduler` 的启动调用链，以及 `LaunchedEngine::Stepped` 里的 Engine 是如何被前端按步 pump 的。

继续阅读的建议路径：

1. [src/lib.rs:L263-L352](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-qwen3/src/lib.rs#L263-L352) 的 `crate::launch` 全文——数一数里面有多少道 `ensure!`，每道对应 CLI 层的哪条规则。
2. 另一条模型线的 `model_line.rs`（如 `pegainfer-deepseek-v2-lite/src/model_line.rs`）——对比它的 `consumed_shared_args` 与互斥规则，体会「同一机制，不同服务面」。
3. [pegainfer-frontend/src/model_line.rs:L497-L548](https://github.com/openinfer-project/openinfer/blob/785d04871df37ddfb3e1b1432ce544b94505a801/pegainfer-frontend/src/model_line.rs#L497-L548) 的 `ModelLineRegistry::new`——把 6 个 `should_panic` 测试与 6 条断言一一配对，理解注册表的自检面。
