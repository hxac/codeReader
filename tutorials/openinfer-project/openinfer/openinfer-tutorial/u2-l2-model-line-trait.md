# ModelLine trait：新模型接入的分发协议

## 1. 本讲目标

上一讲（u2-l1）我们走完了 `pegainfer-server/src/main.rs` 的六步启动流程，把 `ModelLine` 当成一个黑盒「探测并启动引擎的东西」。本讲打开这个黑盒，学完后你应当能够：

1. 逐个说出 `ModelLine` trait 七个协议方法（外加一个身份方法）的语义、默认行为和调用时机。
2. 解释 `ModelLineRegistry::detect` 的「唯一认领者」规则：两条线同时认领同一份 `config.json` 时如何报 `Conflict`，零条线认领时 `NoMatch` 如何携带逐线拒绝原因。
3. 理解 consume-or-reject 校验：为什么用户显式提供的每个 flag 必须被「核心 flag / 该线消费的共享 flag / 该线私有 flag」三者之一接纳。
4. 对照 `pegainfer-qwen3/src/model_line.rs` 读懂一个真实模型线的完整实现，为将来接入自己的模型线（u10-l5）打下基础。

## 2. 前置知识

本讲假设你已读过 u2-l1（server 入口六步流程）。用通俗语言补几个本讲会反复出现的概念：

- **trait（Rust 特征）**：一组函数签名的集合。一个类型实现（`impl`）了 trait，就承诺提供这些函数。调用方只持有 `&dyn Trait`（动态派发的 trait 对象）时，不需要知道具体类型是什么——这正是 server 二进制「永不出现模型 crate 的选项类型」的技术基础。
- **`&'static dyn ModelLine`**：指向一个活了整个进程生命周期的 trait 对象。每个模型 crate 导出一个 `pub static MODEL_LINE`，server 用 `#[cfg(feature = "...")]` 按编译开关收集它们。
- **clap**：Rust 最流行的命令行解析库。用 `#[derive(Args)]` 结构体声明 flag，clap 自动生成 `--help`、解析与默认值。`clap::Command` 是命令的构建器，`ArgMatches` 是解析结果。
- **feature 门控**：workspace 级编译开关（如 `qwen3`、`glm52`）。未启用的模型 crate 根本不参与编译，所以运行时注册表里也没有它——这是 `feature_gate_hint` 存在的原因（u2-l1 已讲）。
- **consume-or-reject（消费或拒绝）**：本讲的核心校验策略。共享 flag 池子很大（`--tp-size`、`--kv-offload`……），但每条模型线只「消费」其中一个子集；检测出的模型线不消费的 flag 一旦被用户显式给出，启动直接报错——宁可失败，也不静默忽略。
- **id vs 拼写**：clap 里每个 flag 有一个内部 id（如 `tp_size`）和一个或多个命令行拼写（如 `--tp-size`）。注册表对两者分别查重，防止两条线用不同 id 注册同一个拼写。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs) | 接缝本体：`ModelLine` trait、`SharedArgs`、`LaunchContext`、`ServePlan`、`ModelLineRegistry`、`DetectError`/`CliError`，以及一组纯 CPU 可运行的注册表单测 |
| [pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs) | Qwen3 对 trait 的完整实现：`Qwen3Cli` 私有 flag、probe 双重门、跨 flag 规则、launch 选项组装 |
| [pegainfer-qwen3/src/config.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/config.rs) | `probe_config_json`：probe 的第二道门（architectures 校验） |
| [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs) | 唯一的 trait 消费方：`model_lines()` 收集、注册表构建、detect、三层校验、launch 与按 `ServePlan` 分发 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs) | `LaunchedEngine` 枚举（`Handle`/`Stepped` 两变体），`launch` 的返回类型 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**ModelLine**（协议本身）、**ModelLineRegistry**（协议的执行者）、**qwen3 model_line**（协议的真实实现样本）。

### 4.1 ModelLine trait：server 与模型 crate 之间的接缝

#### 4.1.1 概念说明

PegaInfer 有七条模型线，但 server 二进制只有一个。如果 server 直接 `use pegainfer_qwen3::...` 写死一条线，那么换模型就要改 server 代码——这违反了「共享基础设施、模型自持执行」的架构原则（u1-l1）。

解法是把「一条模型线在启动期需要做的所有事」抽象成一个 trait：

- 你是什么家族（`name`）？
- 这份 `config.json` 是不是你的（`probe`）？
- 你有哪些私有 CLI flag（`augment_cli`）？
- 你消费哪些共享 flag（`consumed_shared_args`）？你的 flag 之间有什么硬性依赖（`arg_requirements`）、跨 flag 规则（`validate`）？
- HTTP 前端在引擎加载完成前就需要知道什么（`serve_plan`）？
- 怎么把引擎启动起来（`launch`）？

[pegainfer-frontend/src/model_line.rs:1-12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L1-L12) 的模块文档把这个定位说得很直白——文件第一句就是「The seam between the server binary and a model crate」（server 二进制与模型 crate 之间的接缝），并明确：接入一条新模型线 = 在模型 crate 里实现 `ModelLine` + 在 server 的注册表里加一行，「除此之外 server 零改动：参数路由、consume-or-reject 校验、config 检测全部由 trait 派生」。

关键约束：这个 trait 定义在 `pegainfer-frontend`（契约半，不含任何 CUDA 类型），所以 server 可以依赖它而完全不碰 GPU 代码。

#### 4.1.2 核心流程

trait 一共 8 个方法：`name` 是身份元数据，其余 7 个是协议方法。其中 **`name` / `probe` / `launch` 必须实现**，另外 5 个有默认实现（默认 = 不加私有 flag、不消费任何共享 flag、无额外规则、默认 `ServePlan`）。

按启动时序排列（谁在什么时候调它）：

| # | 方法 | 启动期调用者 | 作用 | 默认实现 |
|---|------|------------|------|---------|
| 1 | `name()` | 注册表构建（查重）、日志、错误信息 | 家族名（如 `"Qwen3"`） | 无，必须实现 |
| 2 | `augment_cli(cmd)` | `ModelLineRegistry::new`（登记 flag 归属）与 `build_command`（拼真命令）各一次 | 把本线私有 flag 追加到 clap 命令 | 原样返回 `cmd` |
| 3 | `consumed_shared_args()` | 注册表构建（校验 id 合法）+ `validate_provided` | 声明本线读取哪些 `SharedArgs` flag id | 空切片 |
| 4 | `arg_requirements()` | 注册表构建（校验归属）+ `build_command`（绑 `requires_all`） | 声明「flag A 必须与 flag B 同现」的硬依赖 | 空切片 |
| 5 | `probe(config)` | `registry.detect(&config)` | 认领或拒绝一份 config.json，`Err(reason)` 带拒绝原因 | 无，必须实现 |
| 6 | `validate(ctx, provided)` | server 三层校验的第三层 | 本线专属的跨 flag 规则 | 恒 `Ok(())` |
| 7 | `serve_plan(ctx)` | `line.serve_plan(&ctx)`（launch 之前） | 前端在引擎加载完之前就需要知道的事实 | `ServePlan::default()` |
| 8 | `launch(ctx)` | `spawn_blocking` 里的 `line.launch(&ctx)` | 加载权重、spawn 调度线程、返回 `LaunchedEngine` | 无，必须实现 |

注意方法 2–4 在 **detect 之前**就对**所有已编译线**执行（拼 `--help` 不依赖检测结果），方法 5–8 只对**检测出的那一条线**执行。

数据流伪代码：

```
server: registry.detect(config)
        ├─ for line in 所有已编译线:
        │     match line.probe(config):
        │        Ok   → 认领者 +1（第 2 个 → Conflict）
        │        Err(r)→ rejections.push("<line>: {r}")
        └─ 0 个认领者 → NoMatch { model_type, architectures, rejections }
```

#### 4.1.3 源码精读

trait 定义本体在 [pegainfer-frontend/src/model_line.rs:292-338](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L292-L338)。逐段看：

**probe——唯一认领的入口**（[pegainfer-frontend/src/model_line.rs:296-299](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L296-L299)）：

```rust
/// Claim or reject a model directory by its parsed `config.json`.
/// Return `Err` with the reason when the architecture doesn't match;
/// exactly one registered line must accept a given config.
fn probe(&self, config: &serde_json::Value) -> Result<(), String>;
```

文档明确了契约：**恰好一条**已注册线必须接受给定 config。返回值刻意用 `Result<(), String>` 而非自定义错误——拒绝原因只是给人看的字符串，最终汇入 `NoMatch.rejections`。

**launch——接缝的出口**（[pegainfer-frontend/src/model_line.rs:333-337](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L333-L337)）：

```rust
/// Start the engine: spawn scheduler threads, build the handle with its
/// metadata (`with_kv_capacity`, `with_metrics_watch`, ...), return it.
/// Failures here are deep context chains (CUDA, weights, topology), not
/// something callers branch on — hence `anyhow`.
fn launch(&self, ctx: &LaunchContext<'_>) -> anyhow::Result<LaunchedEngine>;
```

注意返回类型用 `anyhow::Result` 而不是枚举：文档说清了理由——launch 的失败是「深上下文链」（CUDA、权重、拓扑），调用方不会对它做分支处理，只需要把错误链一路向上渲染给用户。这与 `probe`/`validate` 返回可分支的 `DetectError`/`CliError` 形成鲜明对比，是接口设计里「错误该不该被分支」的教科书式示范。

**launch 的输入——LaunchContext**（[pegainfer-frontend/src/model_line.rs:253-264](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L253-L264)）：

```rust
pub struct LaunchContext<'a> {
    pub model_path: &'a Path,
    pub config: &'a serde_json::Value,   // probe 接受的同一份值
    pub shared: &'a SharedArgs,          // 线只读自己消费的子集
    pub matches: &'a clap::ArgMatches,   // 用 from_arg_matches 取回私有 flag
}
```

第四个字段是点睛之笔：合并命令的完整解析结果都在这里，模型线用 `<Cli as clap::FromArgMatches>::from_arg_matches` 取回**自己那部分** flag——server 全程不知道这些类型的存在。

**launch 之前的先行事实——ServePlan**（[pegainfer-frontend/src/model_line.rs:266-289](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L266-L289)）：

```rust
pub struct ServePlan {
    pub scheduler_partition_count: usize,   // 调度分区（逻辑 DP rank）数
    pub prefill_only: bool,                 // GLM5.2 TP4 的 P/D prefill 角色
    pub lora_modules: Option<Vec<LoraModule>>, // Some 则启用 LoRA 路由
}
```

它回答「前端必须在引擎加载完成**之前**、且**独立于**引擎加载知道什么」：HTTP 前端要在引擎还在加载权重时就按分区注册引擎身份，所以 `scheduler_partition_count` 只能从 CLI flag 推导（launch 之后会与 `EngineHandle::scheduler_partition_count` 对账）。

**共享 flag 池——SharedArgs**（[pegainfer-frontend/src/model_line.rs:91-206](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L91-L206)）：一个 `#[derive(clap::Args)]` 结构体，装着所有「多于一条线共用」的 flag（`--model-path`、`--tp-size`、`--kv-offload`、`--decode-overlap` 等）。配套两个机制：

- `CORE_ARGS = ["model_path", "served_model_name", "port"]`（[pegainfer-frontend/src/model_line.rs:67](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L67)）——无论检测出哪条线都有效的核心 flag。
- `SharedArgs::validate`（[pegainfer-frontend/src/model_line.rs:219-240](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L219-L240)）——对所有消费方都成立的跨 flag 规则（如 `--dump-graph-png` 要求 `--cuda-graph=true`），与各线自己的 `validate` 分层。

#### 4.1.4 代码实践

**实践目标**：验证「trait 方法在启动时序中的调用顺序」不是纸上谈兵——用真实测试观察 `augment_cli`、`consumed_shared_args`、`probe` 被调用的效果。

`pegainfer-frontend` 不依赖任何 CUDA crate（可查其 `Cargo.toml`），它的 `model_line` 单测在纯 CPU 机器上可完整构建并运行。

**操作步骤**：

1. 运行注册表全套单测（无需 GPU、无需权重）：

   ```bash
   cargo test --release -p pegainfer-frontend --lib model_line
   ```

2. 打开 [pegainfer-frontend/src/model_line.rs:638-942](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L638-L942) 的 `tests` 模块，阅读 `StubLine`（一个最小 `ModelLine` 实现，probe 只比对 `model_type` 字符串，`launch` 是 `unreachable!`）。

3. 对照 `provided_args_reports_only_explicitly_set_flags` 测试（[pegainfer-frontend/src/model_line.rs:929-941](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L929-L941)）：解析 `["--tp-size", "2", "--line-a-flag"]` 后，`provided` 集合含 `tp_size` 和 `line_a_flag`，但**不含** `port`——尽管 `--port` 有默认值 8000。

**需要观察的现象**：测试全部通过；`provided` 只包含显式设置的 flag，默认值不算「提供」。这是 consume-or-reject 不误伤默认值的关键。

**预期结果**：`model_line` 模块下约 14 个测试全部通过（`detect_finds_the_unique_claimant`、`validate_provided_*` 系列、若干 `should_panic` 的注册表查重测试）。若你的环境连 Rust 工具链都没配好，此步骤「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `probe` 返回 `Result<(), String>` 而 `launch` 返回 `anyhow::Result<LaunchedEngine>`？

**参考答案**：`probe` 的 Err 会被 `detect` 汇总进 `NoMatch.rejections` 展示给用户、并参与 `Conflict`/`NoMatch` 的分支判断，它只需要人类可读的字符串，不需要结构化；`launch` 的失败是 CUDA/权重/拓扑等深层错误链，调用方（server）从不对它分支，只需向上传播并保留上下文，所以用 `anyhow`。

**练习 2**：`ServePlan` 为什么必须在 `launch` 之前、仅凭 CLI 就能算出来？

**参考答案**：server 把引擎加载放到 `spawn_blocking`，同时 HTTP 前端要立即开始加载分词器/模板并按 `scheduler_partition_count` 注册引擎身份（u2-l1 讲过这是「HTTP 前端与权重加载并行」的前提）。若 `ServePlan` 依赖 launch 的产物，这条并行链路就不成立；因此 launch 后还有一道与实际分区数的对账检查兜底。

**练习 3**：`LaunchContext.matches` 存在的意义是什么？为什么不直接把各线的私有 flag 解析好放进一个 `HashMap`？

**参考答案**：让 server 保持「纯分发」。若 server 要把 flag 解析成具体类型，就必须命名各线的选项类型，依赖方向被打破。`matches` 是类型擦除的解析结果，各线用自己的 `Cli` 结构体通过 `from_arg_matches` 取回强类型值——类型知识留在模型 crate 内部。

### 4.2 ModelLineRegistry：detect 的唯一认领与启动期自检

#### 4.2.1 概念说明

trait 只是协议，还需要一个「执行者」收集所有已编译线、执行协议并兜住边界情况。这就是 `ModelLineRegistry`（[pegainfer-frontend/src/model_line.rs:452-456](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L452-L456)）。

它解决三个问题：

1. **检测的确定性**：七条线的 `probe` 可能有重叠的判定条件（比如都检查 `model_type`），必须保证一份 config 恰好被一条线认领，否则「检测出 Qwen3 却启动了 Gemma4」这种事故无法排查。
2. **flag 归属的唯一性**：`--help` 是全量合并的（所有线的私有 flag + 共享 flag 一起展示），两条线若注册了同名 id 或同拼写 flag，clap 行为未定义。注册表在**构造期**就把这类冲突用 `panic!` 炸出来——宁可启动崩溃，不留运行期隐患。
3. **显式校验而非依赖 clap**：`ModelLineRegistry::new` 的文档（[pegainfer-frontend/src/model_line.rs:459-460](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L459-L460)）点明动机：「Validates CLI ownership explicitly because Clap's equivalent schema assertions are disabled in release builds」——clap 自己的 debug 断言在 release 构建里是关闭的，而本工程规定必须 `--release` 运行（u1-l2），所以归属校验必须自己做。

#### 4.2.2 核心流程

注册表的一生分三段：

```
【构造期：注册 + 自检（所有已编译线）】
ModelLineRegistry::new(lines)
 ├─ SharedArgs::augment_args → 以 owner=Shared 登记全部共享 flag 的 id 与拼写
 ├─ 校验 CORE_ARGS 三个 id 确实存在于 SharedArgs
 ├─ 对每条线 register_line:
 │    ├─ consumed_shared_args 里每个 id 必须是共享 flag → 否则 panic
 │    ├─ line.augment_cli(探测用命令) → 登记 own_ids
 │    └─ id 重复 / --长拼写重复(含 alias) / -短拼写重复 → panic
 ├─ 校验线名不重复（BTreeSet insert 检测）
 └─ arg_requirements: source 必须属于声明者、target 必须是共享或自己的 → assert

【命令期：拼全量 CLI】
build_command(cmd)
 └─ SharedArgs + 每条线 augment_cli 再执行一次
    + mut_args 把 arg_requirements 绑成 clap 的 requires_all

【运行期：检测 + 校验（只对胜出线）】
detect(config)      → 唯一认领者 / Conflict / NoMatch
validate_provided   → consume-or-reject
```

consume-or-reject 用集合语言表达就是一个包含关系：

\[ \text{provided} \;\subseteq\; \text{core} \;\cup\; \text{consumed}_{\,\text{line}} \;\cup\; \text{own}_{\,\text{line}} \]

其中 `provided` 是用户在命令行或环境变量里**显式设置**的 flag id 集合（`provided_args` 只保留 `ValueSource::CommandLine | EnvVariable` 的来源，见 [pegainfer-frontend/src/model_line.rs:596-616](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L596-L616)）。任一元素落在三个集合之外，就报 `CliError::UnconsumedFlag`。

#### 4.2.3 源码精读

**detect——唯一认领者算法**（[pegainfer-frontend/src/model_line.rs:524-556](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L524-L556)）：

```rust
pub fn detect(&self, config: &serde_json::Value)
    -> Result<&'static dyn ModelLine, DetectError> {
    let mut rejections = Vec::new();
    let mut claimed: Option<&'static dyn ModelLine> = None;
    for entry in &self.entries {
        match entry.line.probe(config) {
            Ok(()) => match claimed {
                None => claimed = Some(entry.line),
                Some(prev) => {
                    return Err(DetectError::Conflict {
                        first: prev.name(),
                        second: entry.line.name(),
                    });
                }
            },
            Err(reason) => rejections.push(format!("{}: {reason}", entry.line.name())),
        }
    }
    claimed.ok_or_else(|| { /* 组装 NoMatch */ })
}
```

这段代码只有 30 行，但语义精确：

- 顺序遍历，第一个 `Ok` 的线暂存为 `claimed`；**第二个** `Ok` 立即返回 `Conflict`，错误信息点名两条线（`first`/`second`），绝不「先到先得」地静默选一条。
- 每个 `Err` 的拒绝原因都收进 `rejections`。零认领时组装 `NoMatch`，把 `config.json` 的 `model_type` 和 `architectures` **原样**渲染出来（缺失则显示 `missing`），再把逐线拒绝原因用分号拼在后面——用户看到的是「这份 config 长什么样 + 每条已编译线为什么不要它」，可以直接判断是 feature 没编译还是 config 损坏。

对应的错误类型在 [pegainfer-frontend/src/model_line.rs:25-44](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L25-L44)：`DetectError::Conflict` 的信息是 `"model config claimed by both {first} and {second}"`，`DetectError::NoMatch` 的信息内嵌 `model_type=...`、`architectures=...` 和 `rejections` 列表。server 端（u2-l1 讲过）只对 `NoMatch` 分支追加 `feature_gate_hint`，其余原样渲染。

**构造期自检——RegistryClaims**（[pegainfer-frontend/src/model_line.rs:360-433](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L360-L433)）：`RegistryClaims` 维护两张表——`ids: BTreeMap<String, ArgOwner>` 和 `spellings: BTreeMap<String, ArgOwner>`。`claim_id`（L394-406）里有一个特别有意思的分支：

```rust
if let Some(previous) = self.ids.get(&id) {
    if *previous == ArgOwner::Shared {
        if let ArgOwner::Line(name) = owner {
            panic!(
                "model line {name} redeclares SharedArgs flag id {id:?}; \
                 list it in consumed_shared_args instead"
            );
        }
    }
    panic!("{previous} and {owner} both define flag id {id:?}");
}
```

同样是 id 冲突，「模型线重复声明了一个共享 flag」和「两条线撞了同一个 id」给出**不同**的 panic 信息——前者还附上修复指引（把它列进 `consumed_shared_args` 而不是重新声明）。错误信息指向修复动作，是这份代码一贯的风格。

**consume-or-reject——validate_provided**（[pegainfer-frontend/src/model_line.rs:558-586](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L558-L586)）：

```rust
for id in provided {
    let id = id.as_str();
    if CORE_ARGS.contains(&id)
        || detected.consumed_shared_args().contains(&id)
        || entry.own_ids.contains(id)
    {
        continue;
    }
    return Err(CliError::UnconsumedFlag {
        flag: long_flag(cmd, id),   // 把 id 翻译回 --长拼写，报错更友好
        line: detected.name(),
    });
}
```

三个集合依次检查，命中即豁免；否则报 `--{flag} is not used by {line}`。注意 `long_flag`（[pegainfer-frontend/src/model_line.rs:588-593](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L588-L593)）把内部 id 翻译回用户实际敲的 `--` 拼写，让报错可以直接复制回命令行。

**测试脚手架——parse_for_line**（[pegainfer-frontend/src/model_line.rs:623-636](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L623-L636)）：注册表为模型 crate 的单测暴露了一个辅助函数——对「共享 flag + 单条线的 flag」执行与 server 完全相同的解析与校验流程（构建注册表 → 解析 argv → `provided_args` → `validate_provided` → `shared.validate`），返回三元组 `(SharedArgs, ArgMatches, provided)`。文档注明它「不是 serving 入口」。qwen3 的 `validate` 规则测试全靠它驱动（见 4.3.4）。

#### 4.2.4 代码实践

**实践目标**：亲手模拟「两条模型线同时 probe 认领同一份 config」的 Conflict 场景，并解释报错如何产生。这是本讲规格中指定的实践。

**操作步骤**：

1. 阅读 `tests` 模块里的冲突双胞胎桩：`LINE_B` 与 `LINE_B_TWIN` 都认领 `model_type == "model_b"`（[pegainfer-frontend/src/model_line.rs:714-725](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L714-L725)）。

2. 阅读断言 Conflict 的测试（[pegainfer-frontend/src/model_line.rs:807-820](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L807-L820)）：

   ```rust
   #[test]
   fn detect_conflict_names_both_lines() {
       let registry = ModelLineRegistry::new(vec![&LINE_B, &LINE_B_TWIN]);
       let config = serde_json::json!({"model_type": "model_b"});
       let error = registry.detect(&config).map(ModelLine::name)
           .expect_err("two claimants");
       assert!(matches!(error, DetectError::Conflict { .. }));
       let message = error.to_string();
       assert!(message.contains("LineB") && message.contains("LineBTwin"), "{message}");
   }

3. 运行它并观察完整报错文本：

   ```bash
   cargo test --release -p pegainfer-frontend --lib detect_conflict -- --nocapture
   ```

4. （选做，阅读型）若想看运行期真实报错长什么样，可以在 `detect_conflict_names_both_lines` 测试里临时加一行 `println!("{message}")` 再跑；或者直接对照 `DetectError::Conflict` 的 `#[error(...)]` 格式串推导：输出应为 `model config claimed by both LineB and LineBTwin`。不要把改动提交——本手册不允许修改源码，读完还原即可。

**需要观察的现象**：测试通过；错误信息同时包含两条认领线的名字，即注册表拒绝在歧义下「随便选一条」。

**预期结果**：`detect_conflict_names_both_lines` 通过；同理可跑 `detect_no_match_renders_identity_fields_verbatim`（[pegainfer-frontend/src/model_line.rs:822-836](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L822-L836)）观察 `NoMatch` 如何把 `model_type=123`（非字符串也原样渲染）和每条线的拒绝原因拼进一条信息。若本机无法运行 cargo，「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么注册表的冲突检测用 `panic!`（构造期炸掉）而 detect 的歧义用返回 `Err`（运行期报错）？

**参考答案**：flag id/拼写冲突是**开发者错误**——出现在新增模型线时，一旦发生永远发生，任何输入都救不了，越早崩溃越好（且 clap 在 release 下不帮忙查）。config 认领歧义是**用户输入触发的运行期情况**（一份 config 恰好匹配两条线的判定条件），应当返回结构化错误让用户看到两条线的名字与拒绝原因，而不是崩溃。

**练习 2**：`provided_args` 为什么要过滤 `ValueSource` 只留 `CommandLine | EnvVariable`？

**参考答案**：consume-or-reject 的语义是「用户显式要求的 flag 必须被检测出的线认识」。默认值不是用户的意图表达——若把默认值也算「提供」，任何不消费 `cuda_graph` 的线都会因为 SharedArgs 里它的默认值 `true` 而启动失败，校验就失去了意义。

**练习 3**：`validate_provided` 如何从 `detected: &'static dyn ModelLine` 找回该线的 `own_ids`？

**参考答案**：注册表里每个 `LineEntry` 存了 `line`（trait 对象）与构造期登记的 `own_ids`；`validate_provided` 用 `std::ptr::eq(entry.line, detected)` 按指针相等找到对应 entry，再取其 `own_ids`（[pegainfer-frontend/src/model_line.rs:566-570](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L566-L570)）。`expect("detected line came from this registry")` 表明契约：传进来的线必须出自本注册表。

### 4.3 Qwen3Line 实战样本：一个真实模型线的完整实现

#### 4.3.1 概念说明

协议读完了，现在看一个全量实现。Qwen3 是默认开启、资料最全的模型线，它的 `model_line.rs` 是「接入新模型线」的标准模板（u10-l5 会直接复用这里的清单）。

这个文件回答四个问题：

- **probe 怎么写**：两道门——先比 `model_type`，再委托 crate 内部的 `probe_config_json` 校验 `architectures`。
- **私有 flag 怎么组织**：一个 `Qwen3Cli` 结构体装全部 Qwen3 专属 flag（LoRA、显存预算、KV 页大小、batch-invariant 等）。
- **跨 flag 规则放哪**：`validate` 里写本线规则（如 `--batch-invariant` 与前缀缓存/LoRA/投机解码的互斥）。
- **launch 怎么收尾**：把散落的 CLI 值组装成强类型 `Qwen3LaunchOptions`，调用 `crate::launch`，包成 `LaunchedEngine::Stepped`。

#### 4.3.2 核心流程

```
server: #[cfg(feature = "qwen3")] &pegainfer_qwen3::model_line::MODEL_LINE
        │  (pub static MODEL_LINE: Qwen3Line = Qwen3Line;  ← 零大小单元体的 'static)
        ▼
Qwen3Line 实现的七个协议方法
 ├─ probe: model_type=="qwen3"? → probe_config_json: architectures 含 "Qwen3ForCausalLM"?
 ├─ augment_cli: Qwen3Cli::augment_args(cmd)         ← 12 个私有 flag
 ├─ consumed_shared_args: 15 个共享 flag id
 ├─ arg_requirements: 4 条硬依赖（P2P/PD vLLM 相关）
 ├─ validate: LoRA / graph dump / decode-overlap / batch-invariant / dflash 规则
 ├─ serve_plan: enable_lora → Some(lora_modules)
 └─ launch: 组装 Offload/Lora/Memory Options → crate::launch → Stepped 引擎
```

#### 4.3.3 源码精读

**导出与类型**（[pegainfer-qwen3/src/model_line.rs:27-29](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L27-L29)）：

```rust
pub static MODEL_LINE: Qwen3Line = Qwen3Line;
pub struct Qwen3Line;
```

单元结构体 + `static`：`Qwen3Line` 没有任何字段，因为所有状态都从 `LaunchContext` 现场取。这让 server 可以拿 `&'static dyn ModelLine` 而无需构造。

**probe 的双重门**（[pegainfer-qwen3/src/model_line.rs:129-135](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L129-L135)）：

```rust
fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
    let model_type = config.get("model_type").and_then(serde_json::Value::as_str);
    if model_type != Some("qwen3") {
        return Err(format!("model_type {model_type:?} is not \"qwen3\""));
    }
    crate::probe_config_json(config).map_err(|error| error.to_string())
}
```

第二道门在 [pegainfer-qwen3/src/config.rs:512-527](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/config.rs#L512-L527)：要求 `architectures` 数组包含 `"Qwen3ForCausalLM"`。为什么分两层？`model_type` 是粗筛（也是 `feature_gate_hint` 静态表依据的字段），`architectures` 是细筛（挡掉同 family 但不同任务头的 checkpoint）。粗筛失败时的错误信息更简短，细筛失败指出确切的 architecture 要求。

**私有 flag——Qwen3Cli**（[pegainfer-qwen3/src/model_line.rs:34-108](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L34-L108)）：`#[derive(ClapArgs)]` 结构体，包含 `--enable-lora`、`--lora-modules`、`--max-loras`、`--max-lora-rank`、`--kv-p2p-flush-on-finish`、`--kv-pd-vllm-seed`、`--kv-pd-vllm-namespace`、`--kv-pd-miss-wait-ms`、`--gpu-memory-utilization`、`--kv-cache-memory-margin-mib`、`--kv-page-size`、`--batch-invariant` 共 12 个 Qwen3 专属 flag。文件头注释（L31-33）特意说明：共享 flag 住在 `SharedArgs`，这里只放独占的。

**消费的共享 flag**（[pegainfer-qwen3/src/model_line.rs:141-159](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L141-L159)）：`consumed_shared_args` 返回 15 个 id（`cuda_graph`、`dump_graph_png`、`device_ordinal`、`tp_size`、五个 kv_offload/kv_p2p 系列、`no_prefix_cache`、`max_prefill_tokens`、`dflash_draft_model_path`、`decode_overlap`、`decode_sm_pct`）。反过来说：在 `SharedArgs` 里但**不**在此列的 flag（如 `dp_size`）对 Qwen3 是非法的——测试 `rejects_other_lines_flags` 验证的正是 `--dp-size 8` 会被拒绝（见 4.3.4）。

**validate 的跨 flag 规则**（[pegainfer-qwen3/src/model_line.rs:173-253](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L173-L253)）：先经 `cli(ctx)` 助手（L120-122，用 `from_arg_matches` 取回 `Qwen3Cli`）拿到强类型值，然后一串早退规则。挑一段典型——`--batch-invariant` 的六条互斥（L200-234），其中与 `--no-prefix-cache` 的互斥理由写得非常清楚：

```rust
if !shared.no_prefix_cache {
    return Err(CliError::rule(
        "--batch-invariant requires --no-prefix-cache; prefix-cache hits move a prompt's chunk \
         boundaries off the request-local grid, so batch-invariant prefill cannot be provided",
    ));
}
```

注意这些 `validate` 规则不是 clap 属性而是手写代码——因为它们要跨「私有 flag + 共享 flag」且需要解释性错误信息（这是 trait 文档说的「rules are prose, not a taxonomy」，见 [pegainfer-frontend/src/model_line.rs:53-57](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L53-L57)）。而**无条件**的 flag 依赖（A 出现则 B 必须出现）优先用 `arg_requirements` 声明，让 clap 在解析层就报错。

**serve_plan**（[pegainfer-qwen3/src/model_line.rs:255-261](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L255-L261)）：Qwen3 只有 LoRA 一个非默认项——`enable_lora` 为真时返回预加载的 `lora_modules`，其余用默认（单分区、非 prefill-only）。

**launch 的组装**（[pegainfer-qwen3/src/model_line.rs:263-333](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L263-L333)）：三段式——

1. `--kv-offload` 开启时把 GiB 换算成字节、按需叠 P2P 与 vLLM 兼容选项，组装 `Qwen3OffloadOptions`；关闭则 `disabled()`（L266-294）。
2. LoRA 选项与显存边界的乘法溢出检查（L295-307）。
3. 全部塞进 `Qwen3LaunchOptions` 调 `crate::launch(...)`，成功后 `.map(LaunchedEngine::Stepped)`（L308-332）。

`LaunchedEngine` 是个两变体枚举（[pegainfer-frontend/src/engine/wiring.rs:152-155](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L152-L155)）：`Handle`（旧契约引擎）与 `Stepped`（step 驱动引擎，u3-l2 的主题）。Qwen3 属于新一代，所以包 `Stepped`。

#### 4.3.4 代码实践

**实践目标**：用 qwen3 自己的 `model_line` 单测验证 consume-or-reject 与 probe 的真实行为——这是「读协议」到「信协议」的一步。

**操作步骤**：

1. 构建+运行 qwen3 的 model_line 测试（**编译需要 CUDA 工具链**：`pegainfer-qwen3` 依赖 `pegainfer-kernels`，build.rs 会探测 SM 目标；无 GPU 机器请先设 `PEGAINFER_CUDA_SM=120` 之类的值，见 u1-l2。这些测试本身不触碰 GPU 设备，也不需要权重文件）：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib model_line
   ```

2. 重点读三个测试（都在 [pegainfer-qwen3/src/model_line.rs:376-475](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L376-L475)）：
   - `probe_accepts_qwen3_identity` / `probe_rejects_bad_architectures`（L395-405）：验证两道门。
   - `rejects_other_lines_flags`（L448-456）：给 Qwen3 传 `--dp-size 8`，断言报错包含 `is not used by Qwen3`。
   - `graph_png_dump_rejects_lora`（L432-446）：经 `validate_argv` 助手（L382-393，内部就是 `parse_for_line` + `MODEL_LINE.validate`）验证 `--dump-graph-png` 与 `--enable-lora` 的互斥。

3. 对照 `validate_argv`（L382-393）注意它与 server 的对应关系：`parse_for_line` 干的就是 server 里 `build_command → get_matches → provided_args → validate_provided → shared.validate` 那一段，最后再补 `line.validate`——即 server 三层校验的**完整重演**。

**需要观察的现象**：`rejects_other_lines_flags` 失败信息为 `--dp-size is not used by Qwen3`——共享 flag 池里的「别人家的 flag」被点名拒绝，而不是被静默忽略。

**预期结果**：模块内约 10 个测试全部通过。若机器没有 CUDA 工具链无法编译 qwen3，改为纯阅读并跑 4.1.4 的 frontend 测试替代，本步骤「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`--dp-size` 在 `SharedArgs` 里存在且有文档注释，为什么对 Qwen3 是非法 flag？这套「拒绝」比「静默忽略」好在哪里？

**参考答案**：`dp_size` 只被 Kimi-K2 / GLM5.2 等数据并行的线消费，不在 Qwen3 的 `consumed_shared_args` 里，也不是其私有 flag；用户显式给出时 `validate_provided` 报 `UnconsumedFlag`。静默忽略的坏处：用户以为开了 DP8 实际单卡跑，「配置生效了」的假象在生产里是严重事故；显式拒绝把误解消灭在启动时刻。

**练习 2**：Qwen3 的 `arg_requirements` 里 `("kv_pd_vllm_seed", &["kv_p2p_metaserver_addr", "kv_pd_vllm_namespace"])` 与 `validate` 里的规则，分别由谁在什么阶段强制？

**参考答案**：`arg_requirements` 由 `build_command` 绑成 clap 的 `requires_all`（[pegainfer-frontend/src/model_line.rs:512-519](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L512-L519)），在**命令行解析期**报错（且注册表构造期已 assert 过 id 归属合法，release 构建也生效）；`validate` 的规则由 server 在解析后调用 `line.validate(&ctx, &provided)` 执行（[pegainfer-server/src/main.rs:161](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L161)），适合需要解释性长文案或跨层判断的规则。

**练习 3**：为什么 `Qwen3Line` 是无字段的单元结构体？

**参考答案**：trait 的所有输入都经由 `LaunchContext`（model_path/config/shared/matches）在调用时传入，实现本身无状态；无字段使 `pub static MODEL_LINE: Qwen3Line = Qwen3Line` 成为可能——一个编译期常量地址，server 无需任何构造逻辑即可取得 `&'static dyn ModelLine`。

## 5. 综合实践

把本讲三个模块串起来，产出两份可复核的文档（这就是本讲规格指定的实践任务）：

**任务 A：ModelLine 方法启动时序表。**

以 [pegainfer-server/src/main.rs:122-261](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L122-L261) 的 `main()` 为线索，对照 `ModelLineRegistry::new`（[pegainfer-frontend/src/model_line.rs:461-503](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L461-L503)）、`build_command`（L507-519）、`detect`（L524-556）、`validate_provided`（L558-586），亲手填一张表：

| 启动步骤（main.rs 行号） | 被调用的 trait/注册表方法 | 作用于哪些线 |
|---|---|---|
| `ModelLineRegistry::new`（L127） | `name` / `augment_cli` / `consumed_shared_args` / `arg_requirements` | 全部已编译线 |
| `build_command`（L128） | `augment_cli`（第二次） | 全部已编译线 |
| `registry.detect`（L144） | `probe` | 全部已编译线 |
| `validate_provided`（L153） | `consumed_shared_args` | 仅检测出的线 |
| `line.validate`（L161） | `validate` | 仅检测出的线 |
| `line.serve_plan`（L162） | `serve_plan` | 仅检测出的线 |
| `line.launch`（L186，spawn_blocking 内） | `launch` | 仅检测出的线 |

表里特意留了一个思考点：`augment_cli` 在启动期被执行**两次**（一次为登记归属、一次为拼真命令）——确认你在表里区分了这两处。

**任务 B：Conflict 场景推演。**

假设新同事给注册表加了一条 `qwen3_twin` 线，其 `probe` 误写成也认领 `model_type == "qwen3"`。写出：(a) 用户用 Qwen3-4B 目录启动时会看到什么错误（格式对照 `DetectError::Conflict` 的 `#[error]` 串）；(b) 这个错误在 `detect` 的哪一行产生（给出行号）；(c) 为什么注册表拒绝「取第一条匹配」的宽松策略。然后用 4.2.4 的 `detect_conflict_names_both_lines` 测试验证你对 (a) 的推导。

**预期结果**：时序表能覆盖 7 个协议方法的所有调用点；Conflict 推演与测试输出一致（信息形如 `model config claimed by both Qwen3 and <第二条线名>`，产生于 [pegainfer-frontend/src/model_line.rs:534-539](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L534-L539) 的 `Err` 返回）。运行部分若环境不可用，「待本地验证」。

## 6. 本讲小结

- `ModelLine` 是 server 与模型 crate 之间的唯一接缝：8 个方法（`name` 元数据 + `probe`/`augment_cli`/`consumed_shared_args`/`arg_requirements`/`validate`/`serve_plan`/`launch` 七个协议方法），接入新模型线 = 实现它 + 在 server 注册表加一行，server 零其他改动。
- `detect` 的唯一认领规则：恰好一条线 `probe` 成功才放行；两条成功报 `Conflict`（点名双方），零条报 `NoMatch`（原样渲染 `model_type`/`architectures` 并附逐线拒绝原因）。
- consume-or-reject：显式提供的 flag 必须落在 core ∪ 该线 consumed ∪ 该线 own 三集合之并集内，否则 `--xxx is not used by <line>`；默认值不算「提供」。
- 注册表构造期用 `panic!`/`assert` 自检 flag id、拼写、线名与依赖归属——因为 release 构建下 clap 的等价断言被关闭，而工程必须 `--release` 运行。
- `ServePlan` 是「前端在引擎加载完成前就要知道的事实」，只能从 CLI 推导，支撑 HTTP 前端与权重加载并行。
- Qwen3 的实现展示了标准写法：probe 双重门（model_type + architectures）、`Qwen3Cli` 私有 flag、15 个消费的共享 flag、手写跨 flag 规则、launch 组装强类型 options 后包成 `LaunchedEngine::Stepped`。

## 7. 下一步学习建议

本讲结束，「server 如何选模型、如何校验 CLI」已经闭环。下一讲 u3-l1《旧契约：EngineHandle 与 TokenEvent 流》将顺着 `launch` 的返回值往下走：`LaunchedEngine` 背后的引擎契约——`GenerateRequest` 怎么提交、`TokenEvent` 怎么回流、`TokenSink` 如何支持取消。建议预习时通读 [pegainfer-frontend/src/engine/](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/) 目录下的 `request.rs`、`event.rs`、`sink.rs`、`handle.rs` 四个文件；若你想提前看「新契约」，可对照 `step.rs` 与 `ledger.rs`（u3-l2 的主角）。再往后，u10-l5 会把本讲的 trait 当作接入新模型线的 checklist 复用——到时候回来翻 4.3 的四段式结构即可。
