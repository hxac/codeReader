# server 入口：从 config.json 到引擎启动

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序说出 `pegainfer-server/src/main.rs` 中 `main()` 的六步流程：**注册 → CLI → 探测 → 校验 → launch → 按 ServePlan 分发**，并为每一步指出对应的源码行号。
2. 解释 `feature_gate_hint` 的工作原理：当 checkpoint 属于一个「存在但未编译」的模型家族时，server 如何给出「用 `--features xxx` 重建」的提示。
3. 解释为什么这个 server 二进制能做到「永不出现模型 crate 的选项类型」——它是纯分发（pure dispatch）的，所有模型专属的 flag、规则、选项类型都留在各自的模型 crate 里。

本讲承接 u1-l3（Workspace 全景）。那一讲你已经知道 `ModelLine` 是 server 与模型 crate 之间的接缝；本讲我们逐行精读 server 侧的这份接缝协议，以及 registry（注册表）一侧的实现。

## 2. 前置知识

### 2.1 trait object 与静态分发

Rust 的 `&'static dyn ModelLine` 是一个「trait 对象」：它指向任何一个实现了 `ModelLine` trait 的具体类型（比如 Qwen3 的 `Qwen3Line`），但调用者只通过 trait 方法名访问，看不到具体类型。server 拿着一列这样的对象，对它们逐个调用 `probe`、`launch` 等方法——这就是「纯分发」的实现基础。每个模型 crate 导出一个 `pub static MODEL_LINE`（全局静态实例），server 只引用这个静态值，不引用模型 crate 的任何其他类型。

### 2.2 cargo feature 门控回顾

u1-l2 讲过：`pegainfer-server` 的默认 feature 只有 `qwen3`，其他六条模型线（qwen35、gemma4、deepseek-v2-lite、kimi-k2、glm52、k3）都要 `--features` 显式开启。feature 开关在编译期生效：没开启的模型 crate 根本不参与编译，`use` 它的代码必须用 `#[cfg(feature = "...")]` 条件编译包起来，否则编译报错。

### 2.3 clap 的 Command / ArgMatches / FromArgMatches

clap 是 Rust 最常用的命令行解析库。三个核心概念：

- `clap::Command`：描述整个 CLI（有哪些 flag、默认值、帮助文本）。
- `ArgMatches`：一次实际解析的结果（用户给了哪些值）。
- `FromArgMatches` / `Args` derive：把 `ArgMatches` 中的一组字段反序列化成某个结构体（比如 `SharedArgs`）。

本讲的精妙之处在于：**七个模型线 + 一组共享 flag 的 CLI 定义被合并进同一个 `clap::Command`**，`--help` 会显示所有已编译模型线的全部 flag；但校验阶段会拒绝「不属于当前探测到的模型线」的 flag。

### 2.4 HuggingFace checkpoint 的 config.json

每个 HF 模型目录都有一个 `config.json`，其中 `model_type` 字段（如 `"qwen3"`、`"gemma4"`）标识模型家族，`architectures` 字段（如 `["Qwen3ForCausalLM"]`）标识具体架构。PegaInfer 靠这个文件在启动时判断「该用哪条模型线来服务这个目录」——**不需要用户在命令行里指明模型名**。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs) | server 二进制唯一入口：注册表组装、CLI 解析、config 探测、校验、launch、按 ServePlan 分发（共约 295 行） |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs) | `ModelLine` trait、`ModelLineRegistry`、`SharedArgs`、`ServePlan`、`DetectError` 的定义——server 与模型 crate 的接缝协议 |
| [pegainfer-qwen3/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs) | Qwen3 对 trait 的真实实现，用来看「模型侧如何履行协议」 |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml) | 七个模型 crate 全是 optional 依赖，feature 与之一一对应 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs) | `LaunchedEngine` 枚举定义（launch 的返回类型） |

---

## 4. 核心概念与源码讲解

### 4.1 pegainfer-server::main：注册与六步启动总流程

#### 4.1.1 概念说明

`pegainfer-server` 是一个「纯分发」二进制。文件开头的模块注释就是它的设计宣言：

> Every model-specific flag, rule, and option type lives in its model crate; this file only wires detection, CLI validation, and the serve path selection together.
> （每个模型专属的 flag、规则和选项类型都住在模型 crate 里；这个文件只把「探测、CLI 校验、服务路径选择」接线到一起。）

见 [pegainfer-server/src/main.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L1-L4)。

整个 main.rs 里你找不到任何 `Qwen3Cli`、`Qwen3LaunchOptions` 之类的模型选项类型。server 只认识三个 frontend 拥有的抽象：`ModelLine`（做什么）、`ModelLineRegistry`（有哪些）、`ServePlan`（怎么服务）。这就是「永不出现模型 crate 的选项类型」的字面含义——它不是巧合，而是依赖方向决定的：server 依赖 frontend（契约层），模型 crate 也依赖 frontend；server 与模型 crate 之间唯一的接触点是被 `#[cfg]` 包住的 `MODEL_LINE` 静态常量。

#### 4.1.2 核心流程

`main()` 的六步流程：

```text
① 注册   model_lines() 收集所有已编译模型线的 &MODEL_LINE
         → ModelLineRegistry::new()（此刻完成 flag 归属冲突检查）
② CLI    registry.build_command() 合并共享 flag + 各线独占 flag
         → get_matches() 解析 → SharedArgs::from_arg_matches() 取共享值
         → provided_args() 记录用户显式提供过的 flag id
③ 探测   读 <model_path>/config.json → registry.detect() 找唯一认领者
         （NoMatch 时叠加 feature_gate_hint）
④ 校验   registry.validate_provided()  consume-or-reject
         shared.validate()              共享 flag 间的跨 flag 规则
         line.validate()                模型线自己的规则
         line.serve_plan()              产出前端需要的服务计划
⑤ launch  spawn_blocking 线程上执行 line.launch() → LaunchedEngine
         （阻塞加载权重期间，HTTP 前端分词器并行加载）
⑥ 分发   按 plan.lora_modules / plan.prefill_only 选择服务入口：
         LoRA 路由版 / prefill-only 版 / 常规版 vLLM 前端
```

#### 4.1.3 源码精读

**注册（①）**。`model_lines()` 用一个 `vec![]` 字面量装下七条线，每个元素都被 `#[cfg(feature = ...)]` 包住——未启用 feature 时那一行直接消失：

[pegainfer-server/src/main.rs:L24-L41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L24-L41)

```rust
fn model_lines() -> Vec<&'static dyn ModelLine> {
    vec![
        #[cfg(feature = "deepseek-v2-lite")]
        &pegainfer_deepseek_v2_lite::model_line::MODEL_LINE,
        // ...gemma4 / glm52 / k3 / kimi-k2 / qwen35 同理...
        #[cfg(feature = "qwen3")]
        &pegainfer_qwen3::model_line::MODEL_LINE,
    ]
}
```

注意返回类型是 `Vec<&'static dyn ModelLine>`——server 从这里开始就只握着 trait 对象了。与之对应，server 的 [Cargo.toml:L33-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L33-L46) 中七个模型 crate 全部是 optional 依赖，feature 名与 crate 一一对应，且 `default = ["qwen3"]`。

**main 前半段（②③④）**。从入口到产出 `ServePlan`：

[pegainfer-server/src/main.rs:L122-L162](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L122-L162)

```rust
#[tokio::main]
async fn main() -> anyhow::Result<()> {
    logging::init_default();
    pegainfer_core::tracing::init();

    let registry = ModelLineRegistry::new(model_lines());          // ①
    let cmd = registry.build_command(
        clap::Command::new("pegainfer")
            .version(env!("CARGO_PKG_VERSION"))
            .about("PegaInfer GPU inference server"),
    );
    let matches = cmd.clone().get_matches();                        // ②
    let shared = SharedArgs::from_arg_matches(&matches)...;
    let provided = provided_args(&matches, &cmd);

    let config_path = shared.model_path.join("config.json");
    let content = std::fs::read_to_string(&config_path)...;         // ③
    let config: serde_json::Value = serde_json::from_str(&content)...;

    let line = registry.detect(&config).map_err(|error| { ... })?;  // ③

    registry.validate_provided(line, &provided, &cmd)?;             // ④
    shared.validate(&provided)?;
    let ctx = LaunchContext { model_path: &shared.model_path, config: &config,
                              shared: &shared, matches: &matches };
    line.validate(&ctx, &provided)?;
    let plan = line.serve_plan(&ctx)?;
```

校验是三层递进的，每一层管不同的事：

| 层 | 调用 | 管什么 |
|----|------|--------|
| registry | `validate_provided` | 用户给的每个 flag 必须是：核心 flag / 该线消费的共享 flag / 该线自己的 flag，否则拒绝 |
| 共享层 | `shared.validate` | 所有消费共享 flag 的线都成立的规则（如 `--dump-graph-png` 要求 `--cuda-graph=true`） |
| 模型层 | `line.validate` | 该线独有的跨 flag 规则（如 Qwen3 的 `--batch-invariant` 与 LoRA 互斥） |

**launch（⑤）**。引擎加载放在 `spawn_blocking` 线程上，这样 tokio 异步运行时（以及后面要绑定的 HTTP 前端）不会被权重上传这类纯阻塞操作卡住：

[pegainfer-server/src/main.rs:L172-L188](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L172-L188)

```rust
let model_path = shared.model_path.clone();       // 先把要带走的值克隆出来
let served_model_name = shared.served_model_name.clone();
let port = shared.port;

let engine_load = tokio::task::spawn_blocking(move || -> anyhow::Result<LaunchedEngine> {
    let ctx = LaunchContext { model_path: &shared.model_path, config: &config,
                              shared: &shared, matches: &matches };
    line.launch(&ctx)
        .with_context(|| format!("failed to start {} engine", line.name()))
});
```

一个值得注意的细节：L172-L174 提前克隆了 `model_path` / `served_model_name` / `port`，因为 `move` 闭包会把 `shared`、`config`、`matches` 的所有权移进闭包（`spawn_blocking` 要求 `'static`），main 后半段还要用这三个值。

**分发（⑥）**。按 `ServePlan` 的两个字段走三条岔路（详见 4.4）：

[pegainfer-server/src/main.rs:L190-L252](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L190-L252)

```rust
let serve_result = if let Some(lora_modules) = plan.lora_modules {
    // 路线 A：LoRA 服务（需 step 引擎，顺序等待引擎加载完成）
    ...serve_model_with_lora_routes(engine, ..., port, ...)...
} else {
    // 路线 B/C：先包一层「引擎 + Ctrl-C 处理」的 future
    if plan.prefill_only {
        ...serve_prefill_only_with_engine_count(...)...   // 路线 B：prefill-only
    } else {
        ...serve_with_engine_count(...)...                // 路线 C：常规服务
    }
}
.context("vLLM frontend server failed")?;
```

最后无论成败都 `pegainfer_core::tracing::flush()`——失败的服务恰恰是「最后一批请求 span」最有价值的地方（[L254-L258](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L254-L258)）。

#### 4.1.4 代码实践：给六步流程做注释并画流程图（源码阅读型）

1. **实践目标**：把 `main()` 的六步流程内化成自己的 mental map，之后读任何模型线的启动日志都能对上号。
2. **操作步骤**：
   - 打开 `pegainfer-server/src/main.rs`，在自己的笔记（不要改源码）里抄下 `main()` 骨架，为每个步骤标注：起止行号、调用的方法、成功/失败各自走向哪里。
   - 画出流程图，必须包含至少 4 个提前退出的失败分支：config.json 读不到（L139）、detect 无认领者（L144）、`validate_provided` 拒绝 flag（L153）、`line.launch` 失败（L186）。
3. **需要观察的现象**：你会发现自己画的图呈「一条主干 + 一串前置失败出口」形状——所有失败都发生在 `launch` 之前或之中，`serve_result` 之后的路径只有成功/服务器错误两种。
4. **预期结果**：得到一张类似 4.1.2 流程图但含失败分支的完整版本；主干六步与源码行号一一对应。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `main()` 是 `async fn`，但权重加载却用 `spawn_blocking` 而不是 `.await` 一个异步函数？

**答案**：权重加载是「读 safetensors + 拷贝到 GPU 显存」的纯阻塞 I/O，没有可 `.await` 的异步点。若直接在异步任务里跑，会占住 tokio worker 线程；`spawn_blocking` 把它挪到专用的阻塞线程池，同时让 HTTP 前端（分词器、chat 模板）在异步侧并行加载。源码注释（L177-L179）明确说明了这个意图。

**练习 2**：`main.rs` 头部的 `#[global_allocator] static GLOBAL: tikv_jemallocator::Jemalloc`（[L20-L22](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L20-L22)）是干什么的？为什么只在非 msvc 目标上启用？

**答案**：它把进程默认分配器换成 jemalloc，长运行服务下能减少内存碎片、避免 glibc malloc 的 arena 膨胀；`tikv-jemallocator` 不支持 MSVC 工具链，所以用 `#[cfg(not(target_env = "msvc"))]` 排除 Windows/MSVC。

---

### 4.2 ModelLineRegistry：合并 CLI、唯一认领与 consume-or-reject

#### 4.2.1 概念说明

`ModelLineRegistry` 是 frontend 拥有的注册表，解决三个问题：

1. **CLI 归属**：七条线的独占 flag + 一组共享 flag 合并进一个 `clap::Command`，谁的 flag 归谁，重名/重拼写在构造时直接 panic。
2. **唯一认领（detect）**：给定 config.json，恰好一条线认领它；零个认领报 `NoMatch`，两个认领报 `Conflict`。
3. **consume-or-reject（validate_provided）**：用户显式提供的每个 flag，必须是「核心 flag / 当前线消费的共享 flag / 当前线自己的 flag」三者之一。

接缝协议的文档注释写得很直白（值得整段读）：

[pegainfer-frontend/src/model_line.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L1-L12)

> Onboarding a model line means implementing `ModelLine` in the model crate and adding the instance to the registry in the server binary. No other server edits.
> （接入一条模型线 = 在模型 crate 里实现 `ModelLine` + 在 server 的 `model_lines()` 里加一行。server 不需要任何其他修改。）

#### 4.2.2 核心流程

**flag 的三种身份**：

```text
CORE_ARGS（核心，所有线都收）
 ├─ model_path / served_model_name / port
SharedArgs（共享，每线声明自己消费哪个子集）
 ├─ cuda_graph / tp_size / kv_offload / decode_overlap / ...
 └─ 未被当前线消费却显式提供 → validate_provided 拒绝
Line 自己的独占 flag（经 augment_cli 合入）
 └─ 如 Qwen3 的 --enable-lora / --kv-page-size / --batch-invariant
```

**registry 构造期检查（`new`）**：

```text
for 每条线:
    检查 consumed_shared_args 里的 id 确实是 SharedArgs 的 id
    augment_cli 拿到该线的独占命令 → 每个 flag id / 长拼写 / 短拼写逐一"认领"
    任何 id 或拼写已被认领 → panic（这是编程错误，不是用户错误）
最后校验所有 arg_requirements 指向的 id 存在且归属合法
```

**detect 的决策**：

```text
for entry in 注册表:
    match entry.line.probe(config):
        Ok(())  → 已有认领者？是 → 返回 Conflict{first, second}
                  否 → 记为认领者
        Err(原因) → 记入 rejections（"线名: 原因"）
循环结束无认领者 → NoMatch{model_type, architectures, rejections}
```

#### 4.2.3 源码精读

**ModelLine trait 全貌**。七个方法，两个有默认实现：

[pegainfer-frontend/src/model_line.rs:L292-L338](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L292-L338)

```rust
pub trait ModelLine: Send + Sync {
    fn name(&self) -> &'static str;
    fn probe(&self, config: &serde_json::Value) -> Result<(), String>;
    fn augment_cli(&self, cmd: clap::Command) -> clap::Command { cmd }
    fn consumed_shared_args(&self) -> &'static [&'static str] { &[] }
    fn arg_requirements(&self) -> &'static [ArgRequirement] { &[] }
    fn validate(&self, _ctx: &LaunchContext<'_>, _provided: &BTreeSet<String>)
        -> Result<(), CliError> { Ok(()) }
    fn serve_plan(&self, _ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> {
        Ok(ServePlan::default())
    }
    fn launch(&self, ctx: &LaunchContext<'_>) -> anyhow::Result<LaunchedEngine>;
}
```

唯一没有默认实现的是 `probe` 和 `launch`——**不探测就无法认领，不 launch 就无法服务**，这两者是硬性义务。`launch` 的文档注释点明它返回 `anyhow::Result` 的原因：失败是「深层上下文链（CUDA、权重、拓扑）」，调用者不做分支处理，只要错误链。

**detect 实现**：

[pegainfer-frontend/src/model_line.rs:L524-L556](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L524-L556)

```rust
pub fn detect(&self, config: &serde_json::Value)
    -> Result<&'static dyn ModelLine, DetectError> {
    let mut rejections = Vec::new();
    let mut claimed: Option<&'static dyn ModelLine> = None;
    for entry in &self.entries {
        match entry.line.probe(config) {
            Ok(()) => match claimed {
                None => claimed = Some(entry.line),
                Some(prev) => return Err(DetectError::Conflict { .. }),
            },
            Err(reason) => rejections.push(format!("{}: {reason}", entry.line.name())),
        }
    }
    claimed.ok_or_else(|| DetectError::NoMatch { model_type: render("model_type"),
                                                 architectures: render("architectures"),
                                                 rejections })
}
```

`NoMatch` 错误会把 `model_type`、`architectures` **原样渲染**（缺失则显示 `missing`），并附上每条已编译线的拒绝原因——这是排障时判断「为什么没认上」的第一手信息。

**consume-or-reject**：

[pegainfer-frontend/src/model_line.rs:L560-L585](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L560-L585)

```rust
pub fn validate_provided(&self, detected: &'static dyn ModelLine,
                          provided: &BTreeSet<String>, cmd: &clap::Command)
    -> Result<(), CliError> {
    ...
    for id in provided {
        if CORE_ARGS.contains(&id)
            || detected.consumed_shared_args().contains(&id)
            || entry.own_ids.contains(id) { continue; }
        return Err(CliError::UnconsumedFlag { flag: long_flag(cmd, id),
                                              line: detected.name() });
    }
    Ok(())
}
```

配套的 `provided_args`（[L596-L616](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L596-L616)）只收集「用户在命令行或环境变量里**显式设置过**」的 flag id——靠 clap 的 `value_source` 区分显式值与默认值，所以「没写就是默认值」永远不会触发拒绝。

**模型侧的真实实现（Qwen3）**。probe 先验 `model_type`，再把余下校验委托给 `crate::probe_config_json`（含 architectures 检查）：

[pegainfer-qwen3/src/model_line.rs:L124-L139](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L124-L139)

```rust
fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
    let model_type = config.get("model_type").and_then(serde_json::Value::as_str);
    if model_type != Some("qwen3") {
        return Err(format!("model_type {model_type:?} is not \"qwen3\""));
    }
    crate::probe_config_json(config).map_err(|error| error.to_string())
}
```

Qwen3 消费了 15 个共享 flag（[L141-L159](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L141-L159)），从 `cuda_graph`、`tp_size` 到 `decode_sm_pct`。对比之下：`dp_size` 不在其中——所以对 Qwen3 传 `--dp-size 8` 会在 `validate_provided` 被拒，报错 `--dp-size is not used by Qwen3`。

#### 4.2.4 代码实践：跑 registry 的单元测试（CPU 即可）

1. **实践目标**：用真实测试验证你对 detect / validate_provided / 冲突检查的理解。
2. **操作步骤**：
   - `pegainfer-frontend` 不依赖任何 CUDA crate（见其 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/Cargo.toml)），因此这些测试在任何有 Rust 工具链的机器上都能跑：

     ```bash
     cargo test --release -p pegainfer-frontend model_line -- --nocapture
     ```

   - 重点读三个测试（都在 [model_line.rs:L799-L941](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L799-L941)）：
     - `detect_finds_the_unique_claimant`：两条 StubLine，`model_b` 的 config 唯一命中 LineB；
     - `detect_conflict_names_both_lines`：两条线都认领 `model_b` → `Conflict`，错误消息同时含两个线名；
     - `validate_provided_rejects_unconsumed_shared_flag`：给只消费 `tp_size` 的 StubLine 传 `kv_offload` → 报 `--kv-offload is not used by LineA`。
3. **需要观察的现象**：测试输出里 `detect_no_match_renders_identity_fields_verbatim` 会展示 `NoMatch` 如何把 `model_type=123`、`architectures="Foo"` 原样渲染进错误串。
4. **预期结果**：全部测试通过；能对照 `--nocapture` 的 panic 消息理解「重复 flag id / 重复 CLI 拼写」在 `RegistryClaims::claim_id / claim_spelling`（[L394-L413](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L394-L413)）处以 panic 拦截。

#### 4.2.5 小练习与答案

**练习 1**：为什么 flag 冲突（两条线定义同名 flag）用 `panic!` 而不是返回 `Result`？

**答案**：这是「编程错误」与「用户错误」的分界。flag 冲突意味着两个模型 crate 的开发者在合并 CLI 时撞了名，只可能在开发/CI 阶段暴露，此时 fail-fast 的 panic 比运行时错误更有用。而 `DetectError` / `CliError` 面向最终用户，需要可读的错误消息。另外注释（[L459-L460](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L459-L460)）指出：clap 自带的等价断言在 release 构建里是关闭的，所以 registry 必须自己做显式校验。

**练习 2**：`provided_args` 为什么要过滤掉「来自默认值的 flag」？

**答案**：`SharedArgs` 里几乎每个 flag 都有默认值（`cuda_graph` 默认 true、`port` 默认 8000……）。如果不区分来源，任何一次启动都会「显式提供」几十个 flag，consume-or-reject 就会大面积误杀。只有用户真正敲在命令行（或环境变量）里的 flag 才参与归属校验。

**练习 3**：看 Qwen3 的 `consumed_shared_args`（[pegainfer-qwen3/src/model_line.rs:L141-L159](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L141-L159)）：`dp_size` 为什么不在里面？

**答案**：`dp_size` 是为多 rank DP 拓扑的模型线（Kimi-K2、GLM5.2 的 DP8）准备的共享 flag。Qwen3 当前只支持单卡与 TP，不消费它；因此对 Qwen3 显式传 `--dp-size` 会被 consume-or-reject 拒绝，这正是防止「flag 静默无效」的机制。

---

### 4.3 DetectError 与 feature_gate_hint：编译外家族的重建引导

#### 4.3.1 概念说明

设想场景：你在默认构建（只有 qwen3）下，把一个 Gemma 4 的 checkpoint 目录传给 server。`detect` 会在已编译的线里逐个 probe，全部拒绝 → `NoMatch`。错误消息本身只列得出「已编译线」的拒绝原因（qwen3 说 model_type 不是 "qwen3"）——它**不可能知道** gemma4 线的存在，因为那行代码根本没编译进来。

`feature_gate_hint` 就是补这个缺口的：server 里维护一张「家族身份 → feature 名」的静态表。当 `NoMatch` 发生而 config 的 `model_type`（或嵌套 `text_config.model_type`）匹配表中某个**未编译**家族时，在错误后面追加一句人话提示。

#### 4.3.2 核心流程

```text
main:
  registry.detect(config) 失败
    ├─ 错误是 NoMatch？
    │    ├─ 是 → feature_gate_hint(config) 返回 Some？
    │    │        ├─ 是 → 错误信息拼接为 "<NoMatch 原文>; this looks like a
    │    │        │        gemma4 model; rebuild pegainfer-server
    │    │        │        with --features gemma4"
    │    │        └─ 否 → 原样返回 NoMatch（家族也未知，无从提示）
    │    └─ 否（Conflict）→ 原样返回（两条线都认领是 bug 级事件）
```

#### 4.3.3 源码精读

**main 侧的拼接点**：

[pegainfer-server/src/main.rs:L144-L151](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L144-L151)

```rust
let line = registry.detect(&config).map_err(|error| {
    if matches!(error, DetectError::NoMatch { .. }) {
        if let Some(hint) = feature_gate_hint(&config) {
            return anyhow::anyhow!("{error}; {hint}");
        }
    }
    anyhow::Error::from(error)
})?;
```

**hint 表本体**。七个家族各一条，`compiled` 字段用 `cfg!` 宏在编译期求值（与 `#[cfg]` 不同，`cfg!` 是表达式，可以参与运行时布尔逻辑）：

[pegainfer-server/src/main.rs:L49-L120](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L49-L120)

```rust
fn feature_gate_hint(config: &serde_json::Value) -> Option<String> {
    struct Family {
        feature: &'static str,
        model_types: &'static [&'static str],
        text_model_types: &'static [&'static str],
        compiled: bool,
    }
    ...
    let families = [
        Family { feature: "gemma4",
                 model_types: &["gemma4", "gemma4_unified"],
                 text_model_types: &["gemma4_text", "gemma4_unified_text"],
                 compiled: cfg!(feature = "gemma4") },
        Family { feature: "glm52", model_types: &["glm_moe_dsa"], ... },
        ...
    ];
    families.iter().find_map(|family| {
        (!family.compiled
            && (family.model_types.contains(&model_type)
                || family.text_model_types.contains(&text_model_type)))
        .then(|| format!("this looks like a {} model; rebuild pegainfer-server \
                          with --features {}", family.feature, family.feature))
    })
}
```

三个设计要点（注释 [L43-L48](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L43-L48) 写明了第一条）：

1. **身份串必须与各线 probe 的门保持同步**——这张表存在的原因恰恰是「那条线（连同它的 probe）被编译掉了」，所以身份字符串无法从 probe 派生，只能复制一份。这是有意的、不可消除的重复。
2. `text_model_types` 处理多模态风格 checkpoint 的嵌套写法（`config.text_config.model_type`），如 Gemma 4 unified 的 `gemma4_unified_text`。
3. 已编译的家族（`compiled == true`）不会出现在 hint 里——它的 probe 已经参与过 detect，若没认领说明 config 真有问题，提示重建是误导。

**单测直接演示了三种情形**（[L267-L294](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L267-L294)）：未编译的 glm52 身份给出 hint；嵌套 text_config 身份给出 hint；已编译的 qwen3 身份与完全未知的家族都返回 `None`。

#### 4.3.4 代码实践：亲手触发一次 feature-gate 提示

1. **实践目标**：观察「未启用 feature 的模型目录」启动时，错误提示如何一步步引导你重建。
2. **操作步骤**：
   - 伪造一个只需 config.json 的模型目录（探测发生在权重加载**之前**，不需要真实权重）：

     ```bash
     mkdir -p /tmp/fake-gemma4
     printf '{"model_type": "gemma4", "architectures": ["Gemma4ForCausalLM"]}' \
       > /tmp/fake-gemma4/config.json
     cargo run --release -- --model-path /tmp/fake-gemma4
     ```

   - 若机器没有 CUDA 工具链无法编译 server 二进制，退而求其次跑 hint 的单测（同样验证该函数逻辑）：

     ```bash
     cargo test --release -p pegainfer-server --bin pegainfer hint -- --nocapture
     ```

3. **需要观察的现象**：第一条命令的报错应包含三段信息：`NoMatch` 的身份渲染（`model_type="gemma4"`、`architectures=[...]`）、已编译线（Qwen3）的拒绝原因、以及结尾的 hint。hint 单测应 4 个全过（默认构建下 gemma4、glm52 均未编译，qwen3 已编译）。
4. **预期结果**：错误末尾出现 `this looks like a gemma4 model; rebuild pegainfer-server with --features gemma4`。之后再跑 `cargo run --release --features gemma4 -- --model-path /tmp/fake-gemma4`，错误会变成 launch 阶段的权重缺失类失败——说明探测已认领成功。（本实践在无 GPU 机器上运行到探测失败即止，不触碰 GPU；权重加载路径待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 `feature_gate_hint` 放在 server 的 main.rs，而不是 frontend 的 model_line.rs？

**答案**：hint 需要用 `cfg!(feature = "gemma4")` 查询 **pegainfer-server 自己的 feature**。frontend 是被所有模型 crate 和 server 共同依赖的下层库，它没有「server 开了哪些 feature」这个视角；而 main.rs 正是 feature 生效的编译单元。另外 hint 表本质上是在「补编译期被裁掉的 probe 的信息」，与 server 的 `model_lines()` 是同一层的两半。

**练习 2**：如果用户传了一个 `model_type: "qwen3"` 的目录，但用的是没开 qwen3 feature 的自定义构建，会发生什么？

**答案**：`detect` 对已编译的线逐个 probe 全部失败 → `NoMatch`；`feature_gate_hint` 查表发现 qwen3 家族 `compiled == false` 且 `model_type` 匹配 → 追加 `rebuild pegainfer-server with --features qwen3`。也就是说 hint 表对默认家族同样生效，不只是非默认家族。

---

### 4.4 ServePlan 与 LaunchedEngine：launch 与三条服务入口的分发

#### 4.4.1 概念说明

`launch` 之前，server 必须先拿到一份 `ServePlan`——「前端在引擎加载完成**之前**、且**独立于**引擎加载就必须知道的事实」。为什么不等引擎起来再问？因为 HTTP 前端要在引擎还在加载时就按分区注册引擎身份、绑定端口（u1-l4 讲过「端口可达即引擎就绪」的体验就靠它）。所以这些事实必须**只从 CLI flag 推导**：

[pegainfer-frontend/src/model_line.rs:L266-L289](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L266-L289)

```rust
pub struct ServePlan {
    /// 调度分区数（逻辑 DP rank）。前端在引擎仍加载时
    /// 就按此数量注册引擎身份 —— 必须只从 CLI 推导。
    pub scheduler_partition_count: usize,
    /// 只服务 prefill 路由契约（GLM5.2 TP4 P/D 预填充角色）。
    pub prefill_only: bool,
    /// Some 则启用 LoRA 路由并预载列出的适配器。
    pub lora_modules: Option<Vec<LoraModule>>,
}
```

`launch` 的返回类型 `LaunchedEngine` 是个二选一枚举（新一代契约与旧契约，详见 u3-l2）：

[pegainfer-frontend/src/engine/wiring.rs:L152-L167](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L152-L167)

```rust
pub enum LaunchedEngine {
    Handle(super::handle::EngineHandle),   // legacy：EngineHandle
    Stepped(Engine),                       // 新：step 驱动的 Engine
}
```

#### 4.4.2 核心流程

```text
line.serve_plan(ctx) ──► ServePlan
line.launch(ctx)  ──► LaunchedEngine（Handle | Stepped）

main 的分发逻辑（对 plan 三个字段做决策）:
  plan.lora_modules == Some?
    ├─ 是 → 等 launch 完成后取 Stepped 变体（否则 bail），
    │       serve_model_with_lora_routes(...)
    └─ 否 → 构造 CancellationToken + 「加载完成后才接管 Ctrl-C」的 engine future
            plan.prefill_only?
              ├─ 是 → serve_prefill_only_with_engine_count(..., partition_count, ...)
              └─ 否 → serve_with_engine_count(..., partition_count, ...)
```

LoRA 路径必须顺序等引擎就绪，因为路由构建需要引擎的控制平面；非 LoRA 路径里引擎 future 与 HTTP 前端初始化并行——前端在引擎句柄「产生之前」就开始准备，句柄一到即接线。

#### 4.4.3 源码精读

**Qwen3 的 serve_plan**：只在开启 LoRA 时填 `lora_modules`，其余用默认值（partition_count=1、prefill_only=false）：

[pegainfer-qwen3/src/model_line.rs:L255-L261](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L255-L261)

```rust
fn serve_plan(&self, ctx: &LaunchContext<'_>) -> Result<ServePlan, CliError> {
    let cli = cli(ctx);
    Ok(ServePlan {
        lora_modules: cli.enable_lora.then(|| cli.lora_modules.clone()),
        ..ServePlan::default()
    })
}
```

**main 的三条岔路**：

[pegainfer-server/src/main.rs:L190-L252](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L190-L252)

LoRA 路（先顺序等待，再强制 Stepped 变体）：

```rust
let serve_result = if let Some(lora_modules) = plan.lora_modules {
    let launched = engine_load.await.context("engine loader thread panicked")??;
    let LaunchedEngine::Stepped(engine) = launched else {
        anyhow::bail!("LoRA serving requires a step-driven engine");
    };
    ...
    pegainfer_frontend::vllm::serve_model_with_lora_routes(
        engine, ..., lora_modules, port, max_model_len,
        pegainfer_frontend::vllm::shutdown_token_from_ctrl_c()).await
```

常规路（并行 + 延迟接管 Ctrl-C）。注释（[L221-L224](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L221-L224)）解释了一个微妙取舍：阻塞加载无法取消，所以在引擎起来之前 SIGINT 保持默认的「杀进程」行为，起来之后才切换为优雅关闭：

```rust
let engine = async move {
    let handle = engine_load.await.context("engine loader thread panicked")??;
    info!("Engine loaded: elapsed_ms={}", start.elapsed().as_millis());
    pegainfer_frontend::vllm::cancel_token_on_ctrl_c(&shutdown);
    anyhow::Ok(handle)
};
if plan.prefill_only {
    ...serve_prefill_only_with_engine_count(engine, &model_path, ..., port, None,
                                            plan.scheduler_partition_count, shutdown).await
} else {
    ...serve_with_engine_count(engine, &model_path, ..., port, None,
                               plan.scheduler_partition_count, shutdown).await
}
```

注意两条常规入口都把 `plan.scheduler_partition_count` 传了进去——这就是 `ServePlan` 存在的意义：**前端凭计划先行注册，引擎随后才到**。

#### 4.4.4 代码实践：对照三种 ServePlan 形态（源码阅读型）

1. **实践目标**：建立「CLI flag → ServePlan 字段 → 服务入口」的映射表。
2. **操作步骤**：
   - 读 Qwen3 的 `serve_plan`（上文 L255-L261），确认：`--enable-lora` → `lora_modules=Some`；无该 flag → 全默认。
   - 在仓库里 grep `prefill_only`（提示：GLM5.2 的 model_line 会按 P/D 角色置位它），找到第二条模型线的对照实现，记下它依据哪个独有 flag 决定 `prefill_only` 与 `scheduler_partition_count`。
   - 用 `grep -n "LaunchedEngine::" pegainfer-qwen3/src/model_line.rs` 确认 Qwen3 的 launch 返回的是哪个变体（答案在 [pegainfer-qwen3/src/model_line.rs:L332](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/model_line.rs#L332)）。
3. **需要观察的现象**：三条服务入口函数名与 `ServePlan` 三个字段的对应关系会清晰浮出：`lora_modules` ↔ `serve_model_with_lora_routes`；`prefill_only` ↔ `serve_prefill_only_with_engine_count`；默认 ↔ `serve_with_engine_count`；`scheduler_partition_count` 同时喂给后两者。
4. **预期结果**：产出一张三行映射表（ServePlan 形态 → 服务入口函数 → 哪个模型线/flag 会触发）。若想运行验证，可在 GPU 机器上分别用 `--enable-lora --lora-modules name=path` 与裸启动对比启动日志。（GLM5.2 侧结论待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `serve_plan` 是 trait 的独立方法，而不是 `launch` 返回值的一部分？

**答案**：时序。前端需要「分区数」等事实来**在引擎加载期间**注册引擎身份、准备路由；而 `launch` 要等权重搬到 GPU 才返回。若计划藏在 launch 的结果里，前端就必须串行等待，加载期并行初始化（以及长权重加载时的快速失败）就没了。trait 文档（[L266-L268](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L266-L268)）明确要求这些事实「must be derivable from CLI flags alone」。

**练习 2**：LoRA 路径里 `let LaunchedEngine::Stepped(engine) = launched else { bail!(...) }` 这行防御什么？

**答案**：`LaunchedEngine` 有 Handle（legacy）与 Stepped（新契约）两个变体。LoRA 路由的动态适配器控制依赖新契约的控制平面，legacy 引擎给不了；若某条线在 LoRA 模式下返回了 legacy 变体，这行会在启动时立刻报「LoRA serving requires a step-driven engine」，而不是服务中途出现莫名其妙的路由失效。

---

## 5. 综合实践：「启动决策日志」实验

设计意图：用三种输入驱动同一个二进制，验证你对六步流程中每一步「失败即退出」行为的理解。

**准备**：三份最小 config.json（探测失败不需要真实权重）：

```bash
mkdir -p /tmp/lab-qwen3 /tmp/lab-gemma4
printf '{"model_type": "qwen3", "architectures": ["Qwen3ForCausalLM"]}' > /tmp/lab-qwen3/config.json
printf '{"model_type": "gemma4", "architectures": ["Gemma4ForCausalLM"]}' > /tmp/lab-gemma4/config.json
```

**三次启动并记录**（有 CUDA 工具链的机器；无需 GPU，因为三次都不会走到 GPU 初始化之后）：

1. `cargo run --release -- --model-path /tmp/lab-qwen3` → 预期：探测成功，失败发生在 `launch` 阶段（缺 tokenizer/权重文件）。
2. `cargo run --release -- --model-path /tmp/lab-gemma4` → 预期：`detect` 失败，错误含 NoMatch 身份渲染 + Qwen3 拒绝原因 + `--features gemma4` hint。
3. `cargo run --release -- --model-path /tmp/lab-qwen3 --dp-size 8` → 预期：探测成功，`validate_provided` 失败，报 `--dp-size is not used by Qwen3`。

**产出**：一张四列表格——输入 / 停在第几步（①-⑥）/ 错误消息关键片段 / 该错误由哪一层的哪个函数产生（main / detect / feature_gate_hint / validate_provided / launch）。完成后再回头看你自己画的 4.1 流程图，每条失败出口都应被这次实验命中过一次。（无工具链环境下，第 2、3 类行为可分别由 4.3.4 的 hint 单测和 4.2.4 的 frontend 单测等价覆盖；完整三例待本地验证。）

## 6. 本讲小结

- `pegainfer-server/src/main.rs` 是**纯分发**入口：六步流程（注册→CLI→探测→校验→launch→按 ServePlan 分发），每一步失败即带上下文退出。
- server 只持有 `&'static dyn ModelLine` trait 对象与 frontend 拥有的类型（`ServePlan`/`LaunchedEngine`/`DetectError`），**永不出现模型 crate 的选项类型**——这是依赖方向保证的，不是风格约定。
- `ModelLineRegistry` 在构造期用 `RegistryClaims` 检查 flag id/拼写冲突（panic 级），运行期做三件事：合并所有线的 CLI、`detect` 唯一认领、`validate_provided` 的 consume-or-reject（只看用户显式提供的 flag）。
- 探测失败分两类：`Conflict`（两条线都认领）原样上抛；`NoMatch` 会叠加 `feature_gate_hint`——一张「家族身份→feature 名」静态表，为被编译掉的家庭指出 `--features` 重建路径（该表与各线 probe 的身份串是**有意的、不可派生的重复**）。
- `ServePlan`（partition_count / prefill_only / lora_modules）必须在 launch 前从 CLI 单独推导，让 HTTP 前端与权重加载并行；main 据此在 `serve_model_with_lora_routes` / `serve_prefill_only_with_engine_count` / `serve_with_engine_count` 三条入口中分发。

## 7. 下一步学习建议

下一讲 **u2-l2「ModelLine trait：新模型接入的分发协议」**会换到模型侧视角：以 `pegainfer-qwen3/src/model_line.rs` 为主线，逐个方法看一次完整实现——特别是 `validate` 里十多条跨 flag 规则和 `launch` 里如何组装 `Qwen3LaunchOptions`。本讲已读过该文件的方法签名与 serve_plan，正好作为热身。

继续深挖的两个方向：

- 想看 registry 全部单测：[pegainfer-frontend/src/model_line.rs:L638-L941](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L638-L941)，StubLine 的写法是理解 trait 契约的最小样例。
- 想提前理解 `LaunchedEngine::Stepped` 背后的新契约：u3-l2「step 驱动的 Scheduler trait 与 RequestLedger」会展开 `Engine` 与调度器驱动循环。
