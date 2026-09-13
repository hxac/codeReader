# Workspace 全景：19 个 crate 的职责地图

## 1. 本讲目标

学完本讲，你应该能够：

- 逐个说出 workspace 里每个成员 crate 的职责，并按「前端/入口层、模型层、共享运行时、KV 基础设施」四层归类。
- 解释 `default-members = ["pegainfer-server"]` 与 server crate 上 `--features <模型线>` 的关系：为什么默认构建只编译一条依赖链，未编译的模型线如何被探测并提示重建。
- 解释 `pegainfer-server` 为什么能做到「纯分发」——它自己不含任何模型逻辑，只依赖 `pegainfer-frontend` 定义的 `ModelLine` 接缝。
- 掌握一套「某个功能在哪个 crate」的快速定位方法，今后阅读任何模块前先在心里把它放到这张地图上。

先澄清一个数字：讲义标题写「19 个 crate」，而当前 [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L3-L25) 的 `members` 数组实际有 **20 个条目** —— 19 个 `pegainfer-*` 原生 crate，外加 1 个从 NVIDIA dynamo 项目衍生（fork）来的 `kvbm/kvbm-logical`。标题里的 19 指原生 crate；本讲的地图覆盖全部 20 个成员。

## 2. 前置知识

本讲只需要两个 cargo 概念，u1-l2 已经铺垫过，这里复习并补全：

- **workspace（工作区）**：一个仓库里多个 crate 共享同一份 `Cargo.toml` 的依赖版本表和 lint 配置，但每个 crate 仍然是独立的编译单元，有自己的 `Cargo.toml` 与产物（库或二进制）。PegaInfer 用 workspace 统一管住几十个第三方依赖的版本，避免「模型 crate A 用 serde 1.0、模型 crate B 用 serde 2.0」这类撕裂。
- **feature（特性）与 optional dependency（可选依赖）**：cargo 允许把某个依赖声明为 `optional = true`，再用一个同名 feature 控制它是否被编译进来。PegaInfer 用这个机制实现「每个模型一条 feature」——不开启就完全不编译那条模型线的代码。

另外两个本讲会遇到的术语：

- **crate 与 package**：在本文中混用，都指 `members` 列表里的一个条目（严格说 package 是目录 + `Cargo.toml`，crate 是它的编译产物）。
- **dynamo**：NVIDIA 开源的分布式推理框架（ai-dynamo/dynamo）。PegaInfer 从它那里衍生了一个逻辑块管理 crate（`kvbm-logical`），保留了 Apache-2.0 协议头。这不是运行时依赖 Python，只是代码层面的移植。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L1-L26) | workspace 根清单：`default-members`、20 个 `members`、共享依赖表与 lint 表 |
| [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L1-L49) | server crate 清单：7 个可选模型依赖与 feature 定义 |
| [pegainfer-server/src/main.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L1-L295) | 整个 server 的全部源码（单文件）：检测、校验、启动、分发 |
| [pegainfer-frontend/src/lib.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/lib.rs#L1-L17) | frontend 的模块总纲：契约半 + 协议半的分层声明 |
| [pegainfer-frontend/src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L1-L44) | server 与模型 crate 之间的 `ModelLine` 接缝定义 |
| 各 crate 的 `src/lib.rs` 开头文档 | 本讲大量引用它们的一句话自述（core、kernels、sample、bench、sim、kv 三件套、kvbm） |

## 4. 核心概念与源码讲解

### 4.1 workspace 骨架：members 清单与依赖表

#### 4.1.1 概念说明

一个大仓库怎么组织几十万行 Rust 代码？PegaInfer 的答案是：**一个 workspace、20 个各司其职的 crate、一条铁律——依赖只准自上而下**（server/frontend → 模型 crate → 共享运行时 → KV 基础设施），模型 crate 之间互不依赖。根 `Cargo.toml` 就是这张地图的「图例」：`members` 列出全部成员，`[workspace.dependencies]` 统一锁定第三方版本，`[workspace.lints]` 统一 lint 标准。

#### 4.1.2 核心流程

读根 `Cargo.toml` 的顺序：

1. 看 `default-members` —— 不带 `-p` 参数时 cargo 只构建谁。
2. 数 `members` —— 得到完整成员清单，按命名前缀分组。
3. 看 `[workspace.dependencies]` 里的 `path = ...` 条目 —— 这是成员间的内部依赖网。
4. 看注释 —— dynamo 衍生与 vLLM git 依赖的版本锁定理由都写在注释里。

#### 4.1.3 源码精读

members 清单本身就是一张分组表（注意最后一个条目的注释）：

[Cargo.toml:L2-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L2-L25) 声明 `default-members = ["pegainfer-server"]`，然后列出 20 个成员。第 23-24 行的注释明确标注 `kvbm/kvbm-logical` 是「dynamo kvbm (forked from upstream, Apache-2.0)」—— 它是仓库里唯一一个路径不在根目录、且非 `pegainfer-` 前缀的成员。

成员间的依赖全部通过 `path` 声明收敛到一处，例如：

[Cargo.toml:L117-L133](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L117-L133) 把 `pegainfer-bench`、`pegainfer-core`、`pegainfer-kernels` 等所有内部 crate 以 `path` 依赖登记进 workspace 依赖表。任何 crate 想引用兄弟 crate，写 `pegainfer-core = { workspace = true }` 即可，版本永远一致。

两个值得注意的外部依赖区块：

- [Cargo.toml:L44-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L44-L46) dynamo 的 git 依赖被锁在同一个 rev 上，注释解释了原因：不同 rev 的 dynamo crate 会让共享内部类型（`dynamo-tokens`）分裂成两个不同类型。
- [Cargo.toml:L182-L186](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L182-L186) 五个 `vllm-*` crate（chat、engine-core-client、server、text、tokenizer）来自 vLLM 仓库的 git rev —— 这就是 u1-l1 说的「协议与分词复用 vLLM 的 Rust frontend crates」的落点。

另外 [Cargo.toml:L29-L37](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L29-L37) 的 `[workspace.package]` 元信息（作者 NVIDIA、描述 Dynamo Inference Framework）是给 dynamo 移植 crate 用 `edition.workspace = true` 继承的，也侧面印证 kvbm-logical 的出身。

#### 4.1.4 代码实践

1. **实践目标**：不借助本讲正文，独立产出成员清单与分组。
2. **操作步骤**：打开根 `Cargo.toml`，把 `members` 的 20 个条目抄进表格；对每个条目进入其目录，读 `Cargo.toml` 的 `name` 与 `src/lib.rs`（或 `src/main.rs`）的第一段 `//!` 文档注释。
3. **需要观察的现象**：哪些 crate 有 `src/main.rs`（二进制 crate），哪些只有 `src/lib.rs`（库 crate）；`kvbm/kvbm-logical` 目录下是否有独立的 `CLAUDE.md` 与 SPDX 版权头。
4. **预期结果**：你会得到 3 个二进制/入口类 crate（`pegainfer-server`、`pegainfer-sim`，以及各模型 crate 内的 `src/bin/` 诊断工具）和一批库 crate 的清单；`kvbm/kvbm-logical/src/lib.rs` 第一行是 SPDX 版权声明，与其他 crate 明显不同。
5. 本实践是纯阅读任务，无需 GPU。

#### 4.1.5 小练习与答案

**练习 1**：`members` 里哪个条目的路径不在仓库根目录下？它从哪来？

> **答案**：`kvbm/kvbm-logical`。它是从 NVIDIA dynamo（ai-dynamo/dynamo，Apache-2.0）衍生而来的本地 fork，负责逻辑块生命周期管理（详见 4.5 节）。

**练习 2**：为什么所有 dynamo git 依赖必须锁在同一个 rev？

> **答案**：见 [Cargo.toml:L40-L43](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L40-L43) 的注释——同一 git checkout 的 crate 之间会统一共享内部依赖（如 `dynamo-tokens`），混用不同 rev 会让同一类型分裂成两个不兼容的类型实例。

**练习 3**：`default-members = ["pegainfer-server"]` 意味着在仓库根执行 `cargo build` 时会发生什么？

> **答案**：只构建 `pegainfer-server`（及其依赖闭包）。结合 server 的 `default = ["qwen3"]` feature，等价于只编译「server → frontend → qwen3 → core/kernels」这一条链，其余 6 条模型线完全不参与编译。

### 4.2 pegainfer-server：纯分发的入口 crate

#### 4.2.1 概念说明

`pegainfer-server` 是用户直接启动的那个二进制（产物名 `pegainfer`）。它的设计立场非常克制：**自己不含任何模型知识**——不解析任何模型的配置结构、不定义任何模型的 CLI 选项类型。它只做四件事：把编译进来的模型线收集成注册表、读 checkpoint 的 `config.json` 判断是哪条线、校验参数、调用那条线的 `launch` 并按其 `ServePlan` 选择 HTTP 服务入口。所有模型专属的东西（配置结构、CLI 旋钮、执行）都留在各自的模型 crate 里。

#### 4.2.2 核心流程

`main()` 的流程（u2-l1 会逐行精读，本讲只看分工）：

```text
logging 初始化
→ model_lines(): 用 #[cfg(feature)] 收集编译进来的模型线
→ ModelLineRegistry 组装 CLI（各线把自己的参数注入同一个 clap 命令）
→ 读 --model-path 下的 config.json
→ registry.detect(config): 恰好一条线认领 → 确定 line
   （无人认领时 feature_gate_hint 提示 "rebuild with --features X"）
→ 校验参数（consume-or-reject）
→ spawn_blocking 里 line.launch(ctx) 加载权重并启动引擎
→ 按 line.serve_plan(ctx) 的结果选择 vllm 服务入口并 await
```

#### 4.2.3 源码精读

server 的自我定位写在文件第一行的模块文档里：

[pegainfer-server/src/main.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L1-L4) 「pure dispatch over the compiled-in `ModelLine`s. Every model-specific flag, rule, and option type lives in its model crate; this file only wires detection, CLI validation, and the serve path selection together.」—— 每个模型专属的 flag、规则、选项类型都住在模型 crate 里，本文件只做检测、校验与服务路径选择的接线。

「编译进来的模型线」由 `model_lines()` 用条件编译收集：

[pegainfer-server/src/main.rs:L24-L41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L24-L41) 每个条目都是 `#[cfg(feature = "...")] &<模型crate>::model_line::MODEL_LINE`。默认只开 `qwen3`，所以这个 vec 里通常只有一项；开多少 feature，vec 就有多少项。这就是 workspace 级 feature 门控在 server 侧的落点。

feature 与可选依赖的对应关系在 server 自己的清单里：

[pegainfer-server/Cargo.toml:L33-L46](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L33-L46) 定义 feature 表：`default = ["qwen3"]`（注释写明「Qwen3-4B is the default model line: pure Rust + CUDA, no Python at build time」），其余六条 feature 各自拉起对应的可选依赖（[L18-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L18-L25) 的 `optional = true` 依赖）。注意 `qwen35 = ["dep:pegainfer-qwen35", "pegainfer-qwen35/qwen35"]` 这种写法：它同时把 feature **转发**进模型 crate 自身——Triton AOT 等更重的门控在模型 crate 内部还有一层。

未编译的模型线也能被「认出来」并给出重建提示：

[pegainfer-server/src/main.rs:L49-L120](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L49-L120) 的 `feature_gate_hint` 用一张静态家族表（`model_type` 字符串 → feature 名）匹配 config.json；L43-L48 的注释解释了为什么这张表必须手工维护：能认领该 config 的模型线（连同它的 probe）已经被编译掉了，这份重复无法从 probe 派生。文件末尾 [L263-L295](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L263-L295) 有一组单测守护这张表的正确性。

启动后的服务入口选择同样只看数据不看模型：

[pegainfer-server/src/main.rs:L190-L251](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L190-L251) 按 `plan` 的字段分三条路：`lora_modules` 有值走 LoRA 专用路由（要求 step 驱动引擎）、`prefill_only` 走预填充专用服务、否则走标准 `serve_with_engine_count`。注意所有服务函数都来自 `pegainfer_frontend::vllm` —— server 连 HTTP 栈都是借 frontend 的。

#### 4.2.4 代码实践

1. **实践目标**：验证「server 对未启用 feature 的模型给出可操作的提示」。
2. **操作步骤**：
   - 阅读上文引用的 `feature_gate_hint` 与其单测（无需 GPU）；
   - 有构建环境时，执行 `cargo test --release -p pegainfer-server` 运行这四个提示测试（未启用对应 feature 时它们各自生效）；
   - 有 GPU 环境时，用一个未编译模型线的 checkpoint（如 Gemma 4 的 `config.json` 含 `"model_type": "gemma4"`）启动 `pegainfer --model-path ...`。
3. **需要观察的现象**：默认构建下 `hint_names_the_feature_for_a_known_but_uncompiled_family` 通过（它 `#[cfg(not(feature = "glm52"))]` 门控）；真实启动时报错信息里应出现 `this looks like a gemma4 model; rebuild pegainfer-server with --features gemma4`。
4. **预期结果**：错误不是「unknown model」，而是精确到 feature 名的重建指引。无 GPU 时以单测与源码阅读为准（真实启动路径待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `main.rs` 里可以完全不出现 `pegainfer_qwen3::config::Config` 之类的模型类型？

> **答案**：因为模型线只以 `&'static dyn ModelLine` trait 对象的形式出现在 `model_lines()` 里；config 的解析发生在 `line.launch(&ctx)` 内部（模型 crate 里），server 只传原始的 `serde_json::Value` 与 `LaunchContext`（见 [main.rs:L155-L187](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/src/main.rs#L155-L187)）。

**练习 2**：`--features qwen35` 开启后，`model_lines()` 的返回值和编译产物分别发生什么变化？

> **答案**：`#[cfg(feature = "qwen35")]` 分支生效，vec 多出一个 `pegainfer_qwen35::model_line::MODEL_LINE`；同时 [pegainfer-server/Cargo.toml](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L46) 中 `qwen35` feature 会拉入可选依赖并转发 `pegainfer-qwen35/qwen35`（Triton AOT 门控），编译产物因此显著变大且需要构建期 Python + Triton。

**练习 3**：`pegainfer` 这个命令名是哪来的？

> **答案**：[pegainfer-server/Cargo.toml:L9-L11](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-server/Cargo.toml#L9-L11) 用 `[[bin]] name = "pegainfer"` 把 crate `pegainfer-server` 的二进制产物命名为 `pegainfer`（并设了 `default-run`）。

### 4.3 pegainfer-frontend：引擎契约与协议栈的合体

#### 4.3.1 概念说明

`pegainfer-frontend` 是整个仓库的「腰」：向北给 server 提供模型分发接缝，向南给模型 crate 提供必须实现的引擎契约，同时自己还带着把契约翻译成 OpenAI 兼容 HTTP 服务的协议栈。它内部分成两半：

- **契约半**（`engine`、`sampler`、`parallel`、`tracing_state`）：模型 crate 要实现的东西。关键约束是**契约里不出现任何 CUDA 类型**——模型 crate 可以完全不依赖 GPU 代码地理解契约。
- **协议半**（`vllm`，将来还有 `dynamo`）：把契约接到具体 HTTP/通信栈上，复用 vLLM 的 Rust crates 处理分词、chat 模板、SSE 流。

#### 4.3.2 核心流程

frontend 在一次服务启动中的位置：

```text
server main()
  └─ pegainfer_frontend::model_line   ← 接缝：detect / validate / launch
       └─ 模型 crate 实现 ModelLine::launch
            └─ 返回 LaunchedEngine（一个 frontend 定义的引擎句柄）
server 按 ServePlan 调 pegainfer_frontend::vllm::serve_*
  └─ 协议半：HTTP → 分词 → GenerateRequest → 引擎 → TokenEvent → SSE
```

#### 4.3.3 源码精读

整个 crate 的分层声明就是 `lib.rs` 顶部 9 行文档：

[pegainfer-frontend/src/lib.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/lib.rs#L1-L9) 「The contract half (`engine`, `sampler`, `parallel`, `tracing_state`) is what model crates implement against … **no CUDA types anywhere**. The protocol half (`vllm`, later `dynamo`) translates that contract to a concrete HTTP serving stack. `model_line` is the seam the server binary uses to dispatch.」

对应的模块声明在 [L11-L16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/lib.rs#L11-L16)：六个 `pub mod` 正好一组契约（engine、sampler、parallel、tracing_state）加一组协议（vllm）加一个接缝（model_line）。u3 整个单元会逐个精读 `engine` 下的文件。

`model_line` 接缝的约定同样以文档为准：

[pegainfer-frontend/src/model_line.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L1-L12) 「A model line is everything south of the engine contract … Onboarding a model line means implementing `ModelLine` in the model crate and adding the instance to the registry in the server binary. **No other server edits**」—— 接入新模型线时 server 侧只需要加一行注册，参数路由、consume-or-reject 校验、config 检测全部由 trait 派生。这条约定是 u10-l5「接入新模型线」实战的基础。

#### 4.3.4 代码实践

1. **实践目标**：用一条命令验证 frontend 在依赖图中的位置——它不依赖任何模型 crate，也不依赖 kernels。
2. **操作步骤**：在仓库根执行 `cargo tree -p pegainfer-frontend --depth 1`（`cargo tree` 只解析依赖图，不触发 kernels 的 `build.rs`，无 GPU 也能跑）。
3. **需要观察的现象**：输出里没有任何 `pegainfer-qwen3` 之类的模型 crate，也没有 `pegainfer-kernels`；能看到 `vllm-*` 系列 git 依赖。
4. **预期结果**：证实「契约不含 CUDA、协议栈复用 vLLM」的分层不是口号而是依赖事实。（具体输出树待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：契约半为什么坚持「no CUDA types anywhere」？

> **答案**：契约是前端与模型之间的唯一接缝。若契约里出现 CUDA 类型，frontend 就得依赖 kernels/GPU 运行时，无 GPU 的机器连 frontend 都编译不了，`pegainfer-sim`（纯 CPU）也无法复用同一契约跑通全链路（见 4.5 节 sim 的定位）。

**练习 2**：`model_line` 模块放在 frontend 而不是 server 里，有什么好处？

> **答案**：trait 定义与 `ModelLineRegistry` 的通用机制（detect 的唯一认领、参数注入）可以独立于 server 二进制被测试与复用；server 只是这个接缝的使用方，保持极薄（见 [model_line.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/model_line.rs#L1-L12) 的「No other server edits」）。

**练习 3**：`vllm` 模块与 workspace 依赖表里的 `vllm-server` 等 git crate 是什么关系？

> **答案**：`vllm-*` crates 是 vLLM 项目提供的 Rust 前端库（分词、HTTP、engine-core 协议），[Cargo.toml:L182-L186](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/Cargo.toml#L182-L186) 锁定 rev 引入；`pegainfer-frontend/src/vllm/` 则是 PegaInfer 用这些库把自家引擎契约接成 OpenAI 服务的胶水层。

### 4.4 pegainfer-core 与 pegainfer-kernels：共享运行时双子星

#### 4.4.1 概念说明

模型 crate 不必人人从零写 CUDA。共享运行时分两层：

- **`pegainfer-kernels` 是「构建与内核的主人」**：它拥有 `build.rs`（u1-l2 讲过的 nvcc/SM 探测/Triton AOT 流水线）、CUDA/C++ FFI 声明、原始内核的安全包装，以及张量与设备上下文类型。它是仓库里唯一与 nvcc 打交道的 crate。
- **`pegainfer-core` 是「模型侧的运行时 API」**：在 kernels 之上提供面向模型的门面——算子包装（`ops`）、权重加载（`weight_loader`）、CUDA Graph 状态机（`cuda_graph`）、CPU 侧 KV 池（`kv_pool`/`page_pool`）、RoPE、日志与 tracing。模型 crate 主要依赖 core，而不直接碰 kernels 的 FFI。

围绕它们还有四个小工具 crate：`pegainfer-sample`（共享采样策略）、`pegainfer-bench`（内核基准 harness）、`pegainfer-build`（构建期工具链发现，被 kernels 的 build.rs 使用）、`pegainfer-cupti`（CUPTI 硬件计数剖析的 FFI）。

#### 4.4.2 核心流程

一个典型模型 crate 的依赖栈：

```text
pegainfer-qwen3（模型 crate）
  ├─ pegainfer-frontend   （契约：要实现什么）
  ├─ pegainfer-core       （运行时门面：ops / weight_loader / cuda_graph / kv_pool）
  │     └─ pegainfer-kernels（FFI + 内核 + DeviceContext/GpuTensor + build.rs）
  ├─ pegainfer-sample     （采样策略）
  └─ pegainfer-kv-cache   （GPU 分页 KV）
```

#### 4.4.3 源码精读

两个 crate 的自述都很短：

[pegainfer-core/src/lib.rs:L1-L13](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-core/src/lib.rs#L1-L13) 一句话「Shared runtime API used by pegainfer model crates」，模块清单即职责清单：`cuda_graph`、`ffi`、`kv_pool`、`ops`、`page_pool`、`rope`、`tensor`、`weight_loader`、`tracing`、`logging`、`cpu_topology`。u4 单元会精读其中大半。

[pegainfer-kernels/src/lib.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kernels/src/lib.rs#L1-L12) 模块是 `ffi`、`ops`、`paged_kv`、`tensor`、`gpu_buffers`、`typed_ops`、`forward_pass`，以及 feature 门控的 `triton_cubin`（Triton AOT 产物的装载口）。注意它开了 nightly feature（`generic_const_exprs`）——常量泛型张量类型需要编译器支持。

采样层的独立性有清晰的分层理由，写在它自己的文档里：

[pegainfer-sample/src/lib.rs:L1-L19](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sample/src/lib.rs#L1-L19) 「the `.cu`/FFI and the low-level batch primitives live in `pegainfer-kernels` (the CUDA build owner); this crate owns the **policy** (greedy/non-greedy routing, logprob math) and the reusable scratch」—— 内核原语归 kernels，采样策略归 sample。文档甚至记录了 Kimi-K2 的例外（自持 greedy 路径），并说明它仍复用非贪心路径。这种「规则 + 已知例外」的文档风格值得学习。

基准 harness 同样模型无关：

[pegainfer-bench/src/lib.rs:L1-L10](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-bench/src/lib.rs#L1-L10) 「Model-agnostic kernel benchmarking harness」—— CUDA 事件计时循环、延迟统计（[L22-L31](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-bench/src/lib.rs#L22-L31) 的 `LatencyStats` 含 p50/p95/p99）与 `KernelCall` 日程的访问器都放这里，各模型 crate 只保留自己的测量提供者。

#### 4.4.4 代码实践

1. **实践目标**：验证「模型 crate 依赖 core 而非直接依赖 kernels 的 FFI」这一分层。
2. **操作步骤**：在 `pegainfer-qwen3/src/` 下用编辑器搜索 `pegainfer_kernels::` 与 `pegainfer_core::`，统计各自出现的文件数；再对 `pegainfer_kernels::ffi` 单独搜索一次。
3. **需要观察的现象**：`pegainfer_core::` 大量出现（executor、prefill、weights 等）；`pegainfer_kernels::` 集中出现在少数底层文件（张量/设备类型如 `DeviceContext`、`GpuWeight`），`ffi` 直接调用几乎为零。
4. **预期结果**：core 是模型侧主门面，kernels 只通过类型和 core 的再导出被间接触达。无 GPU 也可完成（纯文本搜索）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 FFI 与 build.rs 集中在 kernels 一个 crate，而不是每个模型 crate 自己编译 CUDA？

> **答案**：编译 CUDA 需要 nvcc 工具链发现、SM 探测、并行编译与产物归档（u1-l2 精读过），这套逻辑重且全局唯一；集中后其他 crate 的构建不碰 nvcc，feature 只需在 kernels 一处门控 AOT 子模块。

**练习 2**：`pegainfer-sample` 与 `pegainfer-kernels` 的分工边界是什么？

> **答案**：见 [pegainfer-sample/src/lib.rs:L15-L19](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sample/src/lib.rs#L15-L19)：`.cu`/FFI 与底层批量原语在 kernels；sample 持有策略（贪心/非贪心路由、logprob 数学）与可复用 scratch 缓冲。

**练习 3**：`pegainfer-build` 和 `pegainfer-cupti` 分别服务谁？

> **答案**：`pegainfer-build` 是构建期库，供 kernels 的 `build.rs` 做工具链/包发现（其 `find_package` 按 env → 默认路径探测，见 [pegainfer-build/src/lib.rs:L5-L10](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-build/src/lib.rs#L5-L10)）；`pegainfer-cupti` 是运行期 CUPTI range profiler 的 FFI 绑定（[pegainfer-cupti/src/lib.rs:L10-L24](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-cupti/src/lib.rs#L10-L24) 声明的 `pegainfer_cupti_profile_range`），服务性能剖析（u10-l3）。

### 4.5 全景拼图：模型 crate、KV 三件套与 dynamo fork

#### 4.5.1 概念说明

地图的最后三块：

- **七个模型 crate**：`pegainfer-qwen3`（默认）、`pegainfer-qwen35`、`pegainfer-gemma4`、`pegainfer-deepseek-v2-lite`、`pegainfer-kimi-k2`、`pegainfer-glm52`、`pegainfer-k3`。每个 crate 自持该模型线的配置、权重、调度器、prefill/decode 执行与测试——这就是 README 说的「let each model own its execution」。
- **KV 基础设施三件套**：`pegainfer-kv-cache`（GPU 分页 KV 池）、`pegainfer-kv-offload`（GPU↔主机/SSD/RDMA 换页桥）、`pegainfer-kv-store`（正在演进的统一读写编排层）。外加 dynamo 衍生的 `kvbm/kvbm-logical`（逻辑块生命周期）。
- **一个特殊入口**：`pegainfer-sim`——纯 CPU 的模拟推理服务器，实现同一引擎契约，用于无 GPU 环境跑通/压测前端全链路。

#### 4.5.2 核心流程

给一个「功能 → crate」的速查决策树：

```text
要找的东西是什么性质？
├─ HTTP / SSE / 分词 / 引擎请求类型     → pegainfer-frontend
├─ 某模型的行为（调度、前向、权重）     → pegainfer-<模型名>
├─ 通用 GPU 算子 / 权重加载 / Graph    → pegainfer-core（下沉内核在 pegainfer-kernels）
├─ 采样策略                            → pegainfer-sample
├─ KV 块的分配 / 前缀复用              → pegainfer-kv-cache（编排：kv-store）
├─ KV 落到 CPU/SSD/RDMA                → pegainfer-kv-offload
├─ 内核基准 / 剖析                     → pegainfer-bench / pegainfer-cupti
└─ 无 GPU 验证前端                     → pegainfer-sim
```

#### 4.5.3 源码精读

模型 crate 以默认的 qwen3 为例，`lib.rs` 的模块清单就是它的器官表：

[pegainfer-qwen3/src/lib.rs:L1-L25](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/lib.rs#L1-L25) `config`、`weights`、`prefill`、`batch_decode`、`scheduler`、`executor`、`frontend_adapter`、`model_line`、`lora`、`speculative`（dflash/dspark/eagle3）、`green_ctx`、`verify_graph`…… 一个模型线需要的一切都在这一个 crate 里，u5、u6 将按此解剖。它 import 的 `pegainfer_frontend::engine::{Engine, EngineLoadOptions, EpBackend}`（L33-L35）就是 4.3 节契约的消费者。

KV 三件套各自的 `lib.rs` 文档头精准界定了边界：

[pegainfer-kv-cache/src/lib.rs:L1-L20](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kv-cache/src/lib.rs#L1-L20) 导出 `KvBuffer`（GPU 侧 page-first 布局）、`BlockPool`/`KvBlockGuard`/`PrefixProbe`（逻辑块池）等，并且 **re-export 了 `kvbm_logical`**（L8-L9）—— 这是 dynamo fork 进入依赖链的入口。

[pegainfer-kv-offload/src/lib.rs:L1-L16](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kv-offload/src/lib.rs#L1-L16) 「In-process KV cache offload bridge between pegainfer and pegaflow」—— pegainfer 管 GPU 分页 KV 与逻辑前缀缓存，pegaflow 管更深的三层（host pinned memory、SSD、RDMA），`OffloadEngine` 是决定块何时在两者之间搬运的「大脑」。

[pegainfer-kv-store/src/lib.rs:L1-L8](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kv-store/src/lib.rs#L1-L8) 「the shared KV read/write orchestration layer」—— 每进程一个 `KvStore` 统一编排各 rank 的前缀缓存读与检查点写，设计动机指向 `docs/subsystems/kv-cache/design.md`（u7-l5 精读）。

dynamo fork 的身份与职责：

[kvbm/kvbm-logical/src/lib.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/kvbm/kvbm-logical/src/lib.rs#L1-L11) 保留 SPDX 版权头（NVIDIA，Apache-2.0），自述为「Logical block lifecycle management for KVBM」：类型安全的状态迁移、带去重的块注册表、active/inactive/reset 池、分布式协调事件管线。u7-l2 会深入它的类型状态块设计。

最后是 sim：

[pegainfer-sim/src/main.rs:L10-L14](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/main.rs#L10-L14) 命令自述「CPU-only simulated inference server for OpenAI/vLLM serving benchmarks」；它的 CLI 直接暴露 `base_ttft_ms`、`prefill_tokens_per_ms`、`tpot_ms` 等模拟参数（[L28-L38](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/main.rs#L28-L38)）。库侧 [pegainfer-sim/src/lib.rs:L6-L15](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L6-L15) import 的正是 frontend 契约类型（`Scheduler`、`RequestLedger`、`spawn_scheduler`……）—— 它是「契约不含 CUDA」的最大受益者。

#### 4.5.4 代码实践

1. **实践目标**：完成本讲主实践——20 个成员的职责总表。
2. **操作步骤**：
   - 对照 README 的架构边界表（[README.md:L173-L181](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L173-L181)）与本讲 4.1-4.5 节；
   - 为 `members` 的每个条目写一句职责说明（以各 crate `lib.rs`/`main.rs` 的文档头为准，不许抄别的博客）；
   - 加三列标注：层级（前端/模型/共享运行时/KV）、是否二进制入口、原生还是 dynamo 衍生。
3. **需要观察的现象**：写表过程中你会发现职责最难一句话说清的是 `pegainfer-kv-store`（因为它是演进中的统一层）——把这个疑问记下来，带着它进 u7。
4. **预期结果**：一张 20 行、四列的表。参考归类：前端/入口层 3 个（server、frontend、sim），模型层 7 个，共享运行时 6 个（core、kernels、sample、bench、build、cupti），KV 基础设施 4 个（kv-cache、kv-offload、kv-store、kvbm-logical——最后一个标注 dynamo fork）。

#### 4.5.5 小练习与答案

**练习 1**：`pegainfer-kv-cache` 与 `kvbm-logical` 是什么关系？

> **答案**：`kvbm-logical` 是 dynamo 衍生的逻辑块生命周期库；`pegainfer-kv-cache` 把它作为依赖并在自己的 `lib.rs` 里 re-export（`pub use kvbm_logical;` 与 `KvCacheEvent`，见 [L8-L9](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-kv-cache/src/lib.rs#L8-L9)），同时在它之上提供面向 PegaInfer 的 GPU 分页 KV 门面（`KvBuffer`/`BlockPool`/`KvCacheManager`）。

**练习 2**：为什么 `pegainfer-sim` 能在纯 CPU 上跑通完整前端？

> **答案**：它实现的是 frontend 的引擎契约（`Scheduler`/`RequestLedger` 等，无 CUDA 类型），用可配置的 TTFT/TPOT 参数伪造 token 产出（`SimulatedEngineConfig`，[pegainfer-sim/src/lib.rs:L24-L30](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-sim/src/lib.rs#L24-L30)），协议栈（vllm 模块）对此无感知。

**练习 3**：如果要找「Qwen3 解码一步调用了哪些 GPU 内核」，应该从哪个 crate 的哪个模块开始读？

> **答案**：模型侧从 `pegainfer-qwen3` 的 `batch_decode`/`executor` 模块入手（[pegainfer-qwen3/src/lib.rs:L1-L3](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-qwen3/src/lib.rs#L1-L3) 可见这些模块名），顺着 `pegainfer_core::ops` 门面下沉到 `pegainfer-kernels` 的 FFI；内核级计时则用 `pegainfer-bench` 的 harness（u10-l2 详述）。

## 5. 综合实践

**任务：三线索「crate 定位」接力 + 依赖方向验证。**

本讲反复强调「依赖只自上而下、模型线互不依赖」，现在亲手验证并固化地图：

1. **线索定位**（纯阅读，无 GPU）：不查本讲正文，分别定位以下三件事所在的 crate 与文件（只要求找到 crate 与文件名）：
   - 环境变量 `PEGAINFER_CUDA_SM` 被读取的位置；
   - `PrefixProbe`（前缀探测结果类型）的定义位置；
   - OpenAI `/v1/chat/completions` 的 HTTP 路由所在（提示：在本仓库里找它借用的库与胶水层）。
   参考落点：kernels 的构建脚本（u1-l2 已见过）、`pegainfer-kv-cache/src/pool.rs`、`pegainfer-frontend/src/vllm/`（HTTP 本体在 workspace 依赖的 `vllm-server` crate）。
2. **依赖方向验证**：执行 `cargo tree -p pegainfer-server --depth 2` 与 `cargo tree -p pegainfer-kv-cache --depth 1`，检查输出中是否存在任何「模型 crate 依赖另一个模型 crate」或「frontend 依赖模型 crate」的边。
3. **产出**：把 4.5.4 的 20 行职责表 + 三线索答案 + 依赖图结论合并成一份 `my-workspace-map.md` 笔记（个人学习笔记，不属于本教程目录）。

预期结果：三线索全部能只凭 crate 名与 `lib.rs` 文档头定位；`cargo tree` 证实地图上的箭头都是向下的。若某条线索花了超过十分钟，说明对应的 crate 职责一句话还没提炼准——回头改写那一行。

## 6. 本讲小结

- workspace 共 20 个成员 = 19 个 `pegainfer-*` crate + 1 个 dynamo 衍生的 `kvbm/kvbm-logical`（唯一非根目录、非 pegainfer 前缀的成员）。
- `pegainfer-server` 是纯分发二进制：模型逻辑全部在 `ModelLine` trait 之后，feature 决定 `model_lines()` 里有哪些线，`feature_gate_hint` 为未编译的家族给出精确重建提示。
- `pegainfer-frontend` 是仓库的腰：契约半（`engine` 等，无 CUDA 类型）+ 协议半（`vllm`，复用 vLLM 的 Rust crates）+ `model_line` 接缝。
- 共享运行时分层清晰：kernels 拥有构建与 FFI，core 是模型侧门面（ops/权重/Graph/KV 池），sample/bench/build/cupti 是策略、基准、构建、剖析四个专用工具。
- 七个模型 crate 各自全栈自持（config→weights→scheduler→executor→adapter），KV 三件套（kv-cache/kv-offload/kv-store）是跨模型的生命线。
- 阅读任何代码前先问「它在地图哪一层」——这能把陌生的几十万行仓库缩成一张四层表。

## 7. 下一步学习建议

- 下一讲（u2-l1）钻进 `pegainfer-server/src/main.rs` 的 `main()` 全流程：注册表、CLI 组装、detect 的唯一认领规则与 `ServePlan` 分发——本讲 4.2 节是它的预告片。
- 若你想先动手体验：跳到 u1-l4 用 `pegainfer-sim` 在无 GPU 环境发第一个请求，体会「契约不含 CUDA」的实际价值。
- 建议顺手通读 [README.md:L158-L182](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/README.md#L158-L182) 的 Architecture 一节与 [docs/index.md](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/docs/index.md) 的路由表，把本讲的地图与官方文档对齐。
