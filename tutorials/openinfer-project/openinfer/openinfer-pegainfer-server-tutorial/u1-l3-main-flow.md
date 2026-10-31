# main.rs 全景：从 CLI 到 serve 的启动主流程

## 1. 本讲目标

学完本讲，你应该能够：

1. 按顺序背出 `main()` 从进程入口到 serve 的全部阶段：日志初始化 → `ModelLineRegistry::new` → `build_command` → 解析 CLI → 读 `config.json` → `detect` → `validate_provided` → `shared.validate` → `line.validate` → `serve_plan` → `launch`（`spawn_blocking`）→ 三条 serve 分支 → `tracing::flush`。
2. 说出每个阶段失败时用户看到的错误形态，并能从一条报错反推出它发生在哪个阶段。
3. 解释引擎加载为什么放在 `tokio::task::spawn_blocking` 里与 HTTP 前端并行、LoRA 路径为什么例外地串行、Ctrl-C 为什么是「两段式」行为。
4. 定位 `tracing::flush` 被放在错误传播之前的原因。

本讲是整个手册的主干：后面四个单元（ModelLine 契约、引擎契约、vLLM 协议栈、扩展实践）都是在 `main()` 的某一步上向下展开的。

## 2. 前置知识

本讲只需要前两讲的概念，外加两个 Rust 异步运行时的基础名词：

- **trait object（`&'static dyn ModelLine`）**：模型线以「一个实现了 `ModelLine` trait 的静态实例」的形式暴露给 server。server 只持有 trait 引用，从不 import 任何模型 crate 的具体类型——这是「纯分发二进制」的根基（u1-l1 已建立）。
- **`anyhow::Result` 与上下文链**：`anyhow` 的错误可以一层层 `with_context` 包裹，最终打印成「外层说明: 内层说明: 最内原因」的链。启动失败几乎都是这类深层链，调用方不对其做分支处理。
- **`clap`**：命令行解析库。`get_matches()` 解析失败时**不是返回错误，而是直接打印错误 + usage 并以退出码 2 结束进程**——这解释了为什么 CLI 解析错误从不以 `anyhow` 形态出现。
- **`tokio::task::spawn_blocking`**：把一段**阻塞的同步代码**扔到专门的阻塞线程池执行，不占用异步 reactor 线程。与之相对，`tokio::spawn` 只适合永不阻塞的 async 任务。
- **`CancellationToken`**（`tokio_util`）：一个可克隆的「关停信号」。`cancel()` 一次，所有持有者监听的 `cancelled()` future 同时完成，用于把 Ctrl-C 广播给整棵服务树。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/main.rs` | server 二进制全部代码（339 行，含测试）：`model_lines()` 注册向量、`families()` 身份表、`feature_gate_hint()`、`main()` 主流程 |
| `../pegainfer-frontend/src/model_line.rs` | `ModelLine` trait、`ModelLineRegistry`（`build_command`/`detect`/`validate_provided`）、`SharedArgs`、`LaunchContext`、`ServePlan`、`provided_args` |
| `../pegainfer-frontend/src/engine/wiring.rs` | `LaunchedEngine` 枚举定义（`Handle` / `Stepped` 两代引擎交接包） |
| `tests/detect_gate.rs` | 驱动真实二进制过检测门的集成测试；本讲实践任务的 fixture 搭法来自这里 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 `model_lines()`**（main 的原料清单）、**4.2 `main()` 服务装配**（从注册表到 `serve_plan` 的校验链）、**4.3 引擎加载与 Ctrl-C 两段式**（`spawn_blocking`、三条 serve 分支、收尾 flush）。

### 4.1 `model_lines()`：main() 的原料清单

#### 4.1.1 概念说明

`main()` 做任何事之前，先要回答一个问题：**这个二进制里编进了哪些模型线？** 答案就是 `model_lines()` 返回的 `Vec<&'static dyn ModelLine>`。

每个模型 crate（如 `pegainfer-qwen3`）在自己的 `model_line` 模块里暴露一个静态 `MODEL_LINE` 实例；`model_lines()` 用 `#[cfg(feature = ...)]` 守卫把它们逐个列进向量。这个守卫表与 Cargo 的 feature 表严格镜像（u1-l2 讲过构建侧），未启用的行在编译期就被剔除——不是「运行时跳过」，而是**代码根本不存在于二进制里**。

这正是 u1-l1 说的「纯分发」的第一现场：main.rs 里没有任何 `use pegainfer_qwen3::...` 的具体类型导入，只有这个 cfg 守卫向量。

#### 4.1.2 核心流程

```text
编译期：#[cfg(feature)] 逐行裁决 → 未启用的 &crate::model_line::MODEL_LINE 不参与编译
   ↓
运行期：model_lines() 组装 Vec<&'static dyn ModelLine>
   ↓
交给 ModelLineRegistry::new(model_lines())   ← main() 的第一条实质语句
```

默认构建（`default = ["qwen3"]`）下这个向量恰好一个元素；启用多个 feature 时向量变长，注册表会对所有线的 flag 做唯一性自检（深入讲解留给 u2-l3）。

#### 4.1.3 源码精读

注册向量本体（[src/main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L24-L43)）：每个条目一个 cfg 守卫，按字典序列出 8 条线（deepseek-v2-lite、gemma4、glm52、k3、kimi-k2、qwen3、qwen35、qwen4）。文件头的模块文档（[src/main.rs:L1-L4](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L1-L4)）一句话点明职责边界：**每个模型专属的 flag、规则、选项类型都住在模型 crate 里，本文件只把检测、CLI 校验、serve 路径选择串起来。**

这份向量随后进入注册表构造（[src/main.rs:L151](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L151)）。构造函数（[../pegainfer-frontend/src/model_line.rs:L503-L548](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L503-L548)）会：注册 `SharedArgs` 的全部 flag、逐线注册独占 flag 并断言 id/CLI 拼写全局唯一、收集跨 flag requires 规则。这里的失败是 `panic` 而非 `Result`——它是「开发者把注册表写错了」的编译期类错误，只是被推迟到了进程启动的第一毫秒；文档注释明确说明为什么 release 构建也要自己验（clap 的等价 schema 断言在 release 里被禁用）。

#### 4.1.4 代码实践

**实践目标**：亲手数出「当前构建下 `model_lines()` 会返回几条线」，并推演空注册表的行为。

**操作步骤**：

1. 在仓库根目录执行 `grep -rn "MODEL_LINE" --include="*.rs" crates模型目录` 式的检索（本仓库用 Grep 工具搜 `pub static MODEL_LINE`），列出每个模型 crate 暴露的静态实例。
2. 对照 `src/main.rs` L26-L41 的 8 个 cfg 守卫，画出「feature 名 → crate 名」表，标出默认构建（只含 `qwen3`）时哪些行存活。
3. 思考推演（不必真的编译）：若用 `--no-default-features` 构建出**一条线都没有**的二进制，对一个合法 qwen3 config 运行会发生什么？

**需要观察的现象 / 预期结果**：默认构建下向量只有 1 个元素（qwen3）；空注册表时 `detect` 会返回 `NoMatch` 且 `rejections` 列表为空——错误信息会退化成「没有任何已编译线认领」而没有逐线拒绝理由。这条推演的依据见 4.2.3 的 `detect` 源码（`rejections` 只由已编译线的 `probe` 拒绝时填充）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `model_lines()` 返回 `Vec<&'static dyn ModelLine>`（trait 对象向量），而不是一个枚举 `enum Line { Qwen3, KimiK2, ... }`？

**答案**：trait 对象让 server 二进制不依赖任何模型 crate 的具体类型——新增模型线对 main.rs 的唯一改动是注册向量加一行。若用枚举，每条线的独有 flag、校验规则都得在 server 侧 match，分发就不再「纯」了；而且未启用的线连类型都不该出现在 server 的依赖里。

**练习 2**：同时启用 `qwen3` 和 `qwen35` 两个 feature，对一个 `model_type = "qwen3"` 的 config 调 `detect`，结果是什么？会触发 `Conflict` 吗？

**答案**：返回 qwen3 这条线。`Conflict` 只在**两条线都认领**同一 config 时发生；这里 qwen3.5 的 `probe` 会拒绝，其拒绝理由进入 `rejections` 内部列表——但因为 detect 成功了，这份列表不会展示给用户。

**练习 3**：`model_lines()` 里如果两条线写了同一个 CLI 拼写，错误在什么时候、以什么形式暴露？

**答案**：进程启动时 `ModelLineRegistry::new` 里的 `claim_spelling` 断言直接 `panic`（「both define CLI spelling …」）。这是刻意的：把「注册表写错」从「用户跑到一半才撞上」提前到「二进制根本起不来」。

### 4.2 `main()` 服务装配：从注册表到 serve_plan 的校验链

#### 4.2.1 概念说明

`main()` 是一条**装配线**：原料（模型线）进场，依次通过建模、解析、检测、三级校验、计划产出，最后交给引擎加载与服务启动。它有两条贯穿始终的性质：

1. **每一步都可能失败退出**，且失败形态各不相同：clap 直接结束进程、`DetectError`/`CliError` 按 `thiserror` 模板渲染、文件与引擎错误按 `anyhow` 上下文链层层包裹。
2. **所有校验都发生在花大钱之前**。引擎加载（权重 → GPU）可能是整个启动里最耗时的一步；把 flag 规则、parser 拼写这类廉价检查全部排在它前面，意味着用户不会等五分钟权重加载完才发现自己打错了一个 flag。`SharedArgs::validate` 里对 `--tool-call-parser` 的检查注释把这层意图说得非常直白：「在为一个永远无法解析的请求花费引擎加载之前，先在这里检查」。

三级校验各管一段，职责互不重叠：

| 校验 | 位置 | 管什么 | 失败样例 |
| --- | --- | --- | --- |
| `registry.validate_provided` | frontend | consume-or-reject：用户显式给的每个 flag 必须是核心 flag、被本线消费的共享 flag、或本线独占 flag | `--xxx is not used by Qwen3` |
| `shared.validate` | frontend | 跨线成立的共享 flag 交互规则 | `--decode-sm-pct only applies with --decode-overlap=green-ctx` |
| `line.validate` | 模型 crate | 本线专属的跨 flag 冲突矩阵 | 各线自定文案 |

#### 4.2.2 核心流程

`main()` 全阶段一览（行号指向 `src/main.rs`，详见 4.2.3 的分段精读）：

| # | 阶段 | 位置 | 失败时用户看到什么 |
| --- | --- | --- | --- |
| 0 | 日志与 tracing 初始化 | L148-149 | 基本不失败 |
| 1 | 建注册表 | L151 | `panic`（注册表写错，开发期错误） |
| 2 | 合并 CLI 并解析 | L152-157 | clap 打印错误 + usage，**退出码 2**，不是 anyhow 错误 |
| 3 | 反序列化 `SharedArgs` | L158-159 | `invalid CLI args: …` |
| 4 | 收集显式提供的 flag | L160 | 不失败 |
| 5 | 读 + 解析 `config.json` | L162-166 | `failed to read <path>/config.json: …` / `failed to parse …: …` |
| 6 | `detect`（+ feature 提示） | L168-175 | `no compiled-in model line claims this config (…); rejections: …`，可能追加 `; this looks like a X model; rebuild … --features X`；或 `model config claimed by both A and B` |
| 7 | `validate_provided` | L177 | `--flag is not used by <Line>` |
| 8 | `shared.validate` | L178 | 规则文案，如 `--dump-graph-png requires --cuda-graph=true` |
| 9 | `line.validate` | L185 | 线内冲突文案 |
| 10 | `serve_plan` | L186 | `CliError` 文案 |
| 11 | `launch`（spawn_blocking） | L204-213 | `failed to start <Line> engine: <深层链>` |
| 12 | serve（三分支） | L215-280 | `vLLM frontend server failed: …` |
| 13 | flush + 传播 | L285-286 | （成功路径到此返回 `Ok(())`） |

伪代码形态：

```text
main():
  init_logging()
  registry = ModelLineRegistry::new(model_lines())          # 可能 panic
  cmd = registry.build_command(base)                        # 共享 flag + 各线独占 flag 合体
  matches = cmd.get_matches()                               # 失败 = 进程退出码 2
  shared = SharedArgs::from_arg_matches(matches)?
  provided = provided_args(matches, cmd)                    # 只留用户显式给的 id
  config = read & parse shared.model_path/config.json?
  line = registry.detect(config)?                           # 唯一认领者；NoMatch 时可追加 hint
  registry.validate_provided(line, provided, cmd)?          # 三级校验之一
  shared.validate(provided)?                                # 之二
  ctx = LaunchContext { model_path, config, shared, matches }
  line.validate(&ctx, provided)?                            # 之三
  plan = line.serve_plan(&ctx)?
  …进入 4.3 的引擎加载与服务阶段…
```

#### 4.2.3 源码精读

**第一段：准备与解析**（[src/main.rs:L146-L160](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L146-L160)）：`build_command` 以 `clap::Command::new("pegainfer")` 为底，注入 `CARGO_PKG_VERSION` 与 about 文案；注册表的 `build_command`（[../pegainfer-frontend/src/model_line.rs:L552-L564](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L552-L564)）先把 `SharedArgs` 挂上、再逐线 `augment_cli`，最后用 `mut_args` 把跨 flag requires 规则改写进每个 arg（注释解释了为什么不用 `mut_arg`：它会重排 `--help`）。注意 L157 的 `cmd.clone()`：`cmd` 稍后还要被 `provided_args`（过滤真实 arg）和 `validate_provided`（渲染长拼写）复用。

**第二段：读配置与检测**（[src/main.rs:L162-L175](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L162-L175)）：路径拼接 `shared.model_path.join("config.json")`，读与解析分别 `with_context`。`detect` 的判定逻辑在 frontend 侧（[../pegainfer-frontend/src/model_line.rs:L569-L601](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L569-L601)）：逐线跑 `probe`，第一个认领者胜出，第二个认领者立刻触发 `Conflict`；全部拒绝则汇总成带逐线拒绝理由的 `NoMatch`。main.rs 这段的增值在于：`NoMatch` 时尝试 `feature_gate_hint`，把「存在但被编译掉的家族」的重建建议拼进错误（`families()` 身份表与提示的边界在 u1-l4 专讲）。

**第三段：三级校验与计划**（[src/main.rs:L177-L186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L177-L186)）：

- consume-or-reject 的实现（[../pegainfer-frontend/src/model_line.rs:L605-L630](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L605-L630)）：对 `provided` 里每个 id，命中 `CORE_ARGS`（[L69-L74](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L69-L74)：model_path / served_model_name / port / tool_call_parser）、本线 `consumed_shared_args`、或本线独占 id 之一才放行。
- `provided_args`（[../pegainfer-frontend/src/model_line.rs:L641-L661](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L641-L661)）：用 `ValueSource` 过滤，只保留来自命令行或环境变量的 id——默认值填充的 flag 不算「用户提供」，因此不会被拒。这是 consume-or-reject 公平性的关键。
- `SharedArgs::validate`（[../pegainfer-frontend/src/model_line.rs:L237-L260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L237-L260)）：三条规则（dump-graph-png 需要 cuda-graph、device-ordinal 在 TP 下报错、decode-sm-pct 只在 green-ctx 模式有效）加 parser 拼写预检。注意它与 clap `requires_all` 的分工：一部分跨 flag 依赖（如 `kv_offload_host_gib` 需要 `kv_offload`，见 [L223-L232](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L223-L232)）由 clap 在解析期强制，另一部分语义规则在这里手写——所以两类错误的「措辞来源」不同。
- `LaunchContext`（[../pegainfer-frontend/src/model_line.rs:L274-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L274-L284)）：`validate` / `serve_plan` / `launch` 三个方法共享的只读上下文——模型目录、已解析 config、共享 flag、完整 matches（各线用它把自己的独占 flag 反序列化回来）。
- `serve_plan` 产出的 `ServePlan`（[../pegainfer-frontend/src/model_line.rs:L293-L334](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/model_line.rs#L293-L334)）：调度分区数、prefill-only、LoRA 模块、auto 工具解析器——「前端在引擎加载完成前就需要知道的事实」。它的消费点全部在 4.3 讲的 serve 分支里。

#### 4.2.4 代码实践

**实践目标**：用三次真实运行，把三类错误形态分别钉死在流程图的不同阶段上。

**操作步骤**：

1. 先造一个最小的 qwen3 模型目录（做法完全仿照集成测试的 `model_dir` 帮手，[tests/detect_gate.rs:L39-L48](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L39-L48)）：
   ```bash
   mkdir -p /tmp/u1l3/qwen3-mini
   echo '{"model_type": "qwen3"}' > /tmp/u1l3/qwen3-mini/config.json
   cp ../pegainfer-frontend/examples/echo-model/tokenizer.json /tmp/u1l3/qwen3-mini/
   ```
2. 运行 A（阶段 5 失败）：`cargo run --release -- --model-path /tmp/u1l3/does-not-exist`。
3. 运行 B（阶段 2 失败，clap 层）：`cargo run --release -- --kv-offload-host-gib 8`（只给 host-gib 不给 `--kv-offload`）。
4. 运行 C（阶段 8 失败，`shared.validate` 层）：`cargo run --release -- --model-path /tmp/u1l3/qwen3-mini --decode-sm-pct 50`。运行前先确认 `decode_sm_pct` 确实在 Qwen3Line 的消费清单里（它在，见 [../pegainfer-qwen3/src/model_line.rs:L149-L167](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L149-L167)，L165），否则会先撞上阶段 7 的 UnconsumedFlag。

**需要观察的现象**：

- 运行 A：`failed to read /tmp/u1l3/does-not-exist/config.json` 开头的 anyhow 错误，不出现任何模型线名。
- 运行 B：clap 风格的 `error: the following required arguments were not provided` + usage 块，进程退出码 2（可用 `echo $?` 验证），**没有** anyhow 上下文链。
- 运行 C：通过检测后（可看到带 Qwen3 字样的日志），报 `--decode-sm-pct only applies with --decode-overlap=green-ctx`——这正是 `SharedArgs::validate` L248-252 的原文。

**预期结果**：三条错误措辞来源各异（文件 IO 上下文 / clap usage / CliError 规则文案），恰好对应流程图的三个不同阶段。若你的环境没有 GPU 也不影响本实践：三次失败都发生在引擎加载之前，不触达 CUDA。

#### 4.2.5 小练习与答案

**练习 1**：为什么运行 B 的错误不带 anyhow 上下文、退出码还是 2？

**答案**：`kv_offload_host_gib` 的依赖写在 `SHARED_ARG_REQUIREMENTS`，经 `build_command` 的 `requires_all` 变成 clap 的 schema 一部分；`get_matches()` 解析失败时 clap 自己打印错误与 usage 并以退出码 2 结束进程，错误根本没有变成 Rust 值流到 `main` 的 `?` 链里。

**练习 2**：把 `--tool-call-parser qwen3_coder错别字` 传进来，会在哪个阶段失败？为什么不在 clap 层拦？

**答案**：在阶段 8（`SharedArgs::validate` 末尾的 `validate_parser_overrides`）失败，文案为 `invalid --tool-call-parser: …`。显式 parser 名由 `FromStr` 逐字接受，clap 无法区分「合法字符串」与「注册表里真有这个名字」，只有 parser 注册表能判——注释还点明了把它前置的动机：别为一个永远无法解析的请求白花一次引擎加载。

**练习 3**：`provided_args` 为什么要用 `ValueSource` 过滤？不过滤会怎样？

**答案**：不过滤的话，`SharedArgs` 里所有带默认值的 flag（port、cuda_graph、decode_sm_pct…）都会进 `provided`，而默认值不属于「用户的决定」。consume-or-reject 会拿着一堆用户从未写过的 flag 去报 `--decode-sm-pct is not used by <Line>`，整个机制失去意义；同时 `shared.validate` 里「provided.contains(...) 才报 device-ordinal/TP 冲突」的语义也会被默认值污染。

### 4.3 引擎加载与 Ctrl-C 两段式：spawn_blocking 与三条 serve 分支

#### 4.3.1 概念说明

校验链走完，`main()` 打出两条 info 日志（线名、`Loading engine...`）后进入启动的重头戏。这里有一个贯穿的设计问题：**引擎加载（权重 → GPU）是漫长的阻塞过程，而 HTTP 前端（tokenizer、chat 模板）的准备也是一段独立工作——串行还是并行？**

答案是「默认并行、LoRA 例外」：

- **并行**：引擎加载丢进 `spawn_blocking`，主异步任务同时启动 vLLM 前端。前端虽然先跑，但**只在引擎注册之后才 bind 端口**，所以「端口可达 = 服务就绪」的语义不变——用户不会连上一个还没有引擎的端口。
- **LoRA 例外**：LoRA 路由在**构建 router 时**就需要引擎的控制平面（装载适配器的通道），没法等加载完成后再补——所以这条路径串行地先 `await` 引擎，再建路由，并且要求引擎是新一代 step 驱动形态（`LaunchedEngine::Stepped`），拿到旧形态（`Handle`）直接 bail。

**Ctrl-C 两段式**是同一个权衡的另一半：阻塞的引擎加载**无法被取消**（权重传到一半只能硬停）。与其让 Ctrl-C 在加载期触发一段无人消费的优雅关停逻辑，不如让 SIGINT 保持默认行为（进程直接被杀）；引擎就绪之后才挂接 `cancel_token_on_ctrl_c`，切换成优雅关停。一句话：**能取消的阶段才谈优雅，不能取消的阶段干脆诚实。**

最后是收尾细节：`tracing::flush()` 被放在 `serve_result?` **之前**。请求级的 trace span 还缓冲在进程里；恰恰是服务失败的时刻，最后一批 span 最有诊断价值。flush 先行，错误传播随后，tracing 未启用时 flush 是空操作。

#### 4.3.2 核心流程

普通 / prefill-only 分支的双时间线：

```text
t0   spawn_blocking 线程: line.launch(ctx)          tokio 异步侧: serve*_with_engine_count(...)
       权重 → GPU、建调度器线程、铸造引擎句柄        tokenizer / chat 模板加载；
                                                      按 plan.scheduler_partition_count 预注册 engine identity
t1   engine_load.await 返回                            （端口始终未 bind，等引擎注册）
       打印 "Engine loaded: elapsed_ms=…"
       cancel_token_on_ctrl_c(&shutdown)  ← Ctrl-C 从「默认杀」切换为「优雅关停」
t2   ────────────── HTTP 服务循环 ──────────────
     任意时刻失败/关停 → engine 任务被取消 → shutdown 级联

LoRA 分支（串行）：
engine_load.await → 解构 LaunchedEngine::Stepped（Handle 则 bail）
  → load_max_model_len（失败退化 4096）
  → serve_model_with_lora_routes(…, shutdown_token_from_ctrl_c())
```

三分支的选择规则（由 `ServePlan` 字段驱动）：

| 优先级 | 条件 | 走哪条 serve |
| --- | --- | --- |
| 1 | `plan.lora_modules` 为 `Some` | `serve_model_with_lora_routes`（串行分支） |
| 2 | `plan.prefill_only == true` | `serve_prefill_only_with_engine_count` |
| 3 | 其余 | `serve_with_engine_count` |

#### 4.3.3 源码精读

**并行加载的装配**（[src/main.rs:L196-L213](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L196-L213)）：注意 L196-199 先克隆了 `model_path`、`served_model_name`、解析好的 `tool_call_parser`、`port`——因为原始的 `shared`/`config`/`matches` 会被 move 进闭包，闭包内部还要重建一个 `LaunchContext` 给 `launch` 用。L201-203 的注释是并行设计的自述：「引擎加载跑在阻塞线程上，让 HTTP 前端（tokenizer、chat 模板）并发加载；前端只在引擎注册后 bind，所以就绪语义不变」。`launch` 的失败被包上 `failed to start {line.name()} engine` 上下文。

**LoRA 串行分支**（[src/main.rs:L215-L237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L215-L237)）：`engine_load.await` 后紧跟**两个问号**——第一个解 `JoinResult`（阻塞线程 panic → `engine loader thread panicked`），第二个解 `anyhow::Result`（launch 失败）。然后 `let … else` 解构：`Stepped(engine)` 拿到引擎，`Handle` 则 `bail!("LoRA serving requires a step-driven engine")`。两代引擎的交接包定义在 [../pegainfer-frontend/src/engine/wiring.rs:L165-L180](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-frontend/src/engine/wiring.rs#L165-L180)：`LaunchedEngine` 是二选一枚举，`Handle`（legacy 每 token 契约）或 `Stepped`（新一代 step 驱动契约），u3-l1 会展开这张地图。LoRA 分支的关停用的是另一个帮手 `shutdown_token_from_ctrl_c()`（直接返回一个挂好 Ctrl-C 的 token），而非两段式。

**非 LoRA 分支与两段式 Ctrl-C**（[src/main.rs:L238-L279](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L238-L279)）：先造一个 `CancellationToken`；`engine` 是一个 async 块——await 加载结果、打印耗时、**然后才**调用 `cancel_token_on_ctrl_c` 挂接 Ctrl-C。L247-249 的注释把两段式讲透：「阻塞加载无法取消，所以 SIGINT 在引擎就绪前保持默认的 kill 行为；就绪后才切换到优雅关停」。这个 `engine` future 连同 `shutdown` token 一起交给 `serve_prefill_only_with_engine_count`（L254-265，`plan.prefill_only` 为真）或 `serve_with_engine_count`（L266-277，默认），两者都吃 `plan.scheduler_partition_count` 作为 engine 计数——这就是 4.2 里 `ServePlan` 文档说的「前端在引擎仍在加载时就要按分区注册 engine identity」的消费现场（u4-l1 详讲装配内部）。

**收尾**（[src/main.rs:L280-L288](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L280-L288)）：整个三分支表达式的结果被 `.context("vLLM frontend server failed")` 包一层；L282-284 的注释解释 flush 的位置——「在 runtime 拆掉前导出最后一批请求 span；错误传播前也要 flush——失败的服务器恰恰是最后缓冲 span 最要紧的地方；tracing 未启用时是空操作」；然后才是 `serve_result?`。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「校验链全部通过、进入引擎加载阶段」的日志证据，并理解端口为何从未 bind。

**操作步骤**：

1. 复用 4.2.4 造好的 `/tmp/u1l3/qwen3-mini` 目录（config.json + tokenizer.json）。
2. 运行：`RUST_LOG=info cargo run --release -- --port 9000 --model-path /tmp/u1l3/qwen3-mini`。
3. 等待进程退出（我们的迷你 config 缺少真实权重与完整字段，`launch` 注定失败）。
4. 另开终端重跑一遍，并在日志出现 `Loading engine...` 后立刻按 Ctrl-C，观察进程是否立刻终止（对比：真实模型加载完成后按 Ctrl-C 会走优雅关停日志——后者需要真实权重，本环境无法演示）。

**需要观察的现象**：

- stderr 出现 `=== pegainfer - Qwen3 (GPU) ===` 与 `Loading engine...`——这两行（[src/main.rs:L188-L189](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/src/main.rs#L188-L189)）是「已通过全部三级校验与 serve_plan」的分界证据。
- 最终错误以 `failed to start Qwen3 engine` 开头（`spawn_blocking` 闭包里的 with_context），后面跟深层原因链（缺权重字段 / CUDA 不可用等，取决于环境，具体尾文**待本地验证**）。
- 全程没有任何「listening on …」类日志——9000 端口从未 bind：非 LoRA 分支里前端在引擎注册前不 bind，而引擎加载失败在先。
- 若看不到 info 行，检查是否带了 `RUST_LOG=info`（默认日志级别可能低于 info，具体由 `logging::init_default` 决定）。

**预期结果**：把观察到的日志逐条对应回 4.2.2 流程表的阶段：两条 info = 阶段 10 已过；错误头 `failed to start Qwen3 engine` = 阶段 11；进程退出码非 0，且错误在阶段 12 的 `vLLM frontend server failed` 上下文之前就已经终结了 serve（引擎 future 先失败，前端随之被取消）。

#### 4.3.5 小练习与答案

**练习 1**：为什么引擎加载用 `spawn_blocking` 而不是 `tokio::spawn`？

**答案**：`launch` 是同步阻塞代码（权重 IO、CUDA 初始化、线程创建），可能连续阻塞几十秒到几分钟。`tokio::spawn` 的任务跑在数量有限的异步 worker 线程上，一个长期阻塞的任务会把 reactor 饿死，HTTP 前端的并发加载直接停摆；`spawn_blocking` 专用阻塞线程池，阻塞不影响异步侧。

**练习 2**：LoRA 分支里 `engine_load.await.context("engine loader thread panicked")??` 为什么有两个 `?`？

**答案**：`engine_load` 是 `JoinHandle<anyhow::Result<LaunchedEngine>>`。`await` 得到 `Result<anyhow::Result<LaunchedEngine>, JoinError>`：第一个 `?`（配合 context）解 JoinError——阻塞线程 panic 的情形；第二个 `?` 解内层 anyhow——`launch` 本身失败的情形。两种失败的语义完全不同，所以分开报。

**练习 3**：把 `pegainfer_core::tracing::flush()` 挪到 `serve_result?` 之后（即错误已经 return 之后）会损失什么？

**答案**：失败路径上最后缓冲的一批请求 span 永远导不出去——`?` 直接把错误 return 出 `main`，进程随即退出。注释明说「失败的服务器恰恰是最后缓冲 span 最要紧的地方」：崩溃诊断正需要这些数据。flush 在前、传播在后，才能保证成功与失败两条路径都不丢 span。

**练习 4**：为什么 LoRA 分支不采用「前端先起、引擎后到」的并行模式？

**答案**：LoRA 路由在 router **构建期**就需要引擎控制平面（LoraClient 要拿引擎铸造的控制通道去装载适配器，u4-l5 详讲），路由树没法在引擎缺席时先搭好再补；所以这条路径必须先 `await` 引擎、再建路由、再起服务，天然串行，并且要求 `LaunchedEngine::Stepped`（控制平面只有 step 引擎铸造）。

## 5. 综合实践

把三个模块串起来，产出一份**「main() 全阶段流程图 + 错误形态标注 + 实测对照」**的完整笔记：

1. **画流程图**：以 4.2.2 的阶段表为骨架，画一张纵向流程图。每个阶段框标注三件事：入口条件、成功去向、失败时用户看到的错误形态（clap usage / anyhow 上下文链 / thiserror 模板 / panic）。在阶段 11 处分叉出双时间线（阻塞线程 vs 异步侧），在阶段 12 处画三分支（LoRA 串行 / prefill-only / 普通）与 Ctrl-C 两段式的切换点。
2. **实测对照**：完成 4.2.4 的三次运行（阶段 5 / 阶段 2 / 阶段 8）与 4.3.4 的引擎加载运行（阶段 11），把每条真实输出粘贴到流程图对应框的旁边。无 GPU 环境完全够用——四次失败全部发生在 CUDA 之前或在其边缘。
3. **自检问答**：合上讲义回答——(a) 一条 `--xxx is not used by Qwen3` 的错误对应哪个阶段？由哪段代码产生？(b) 为什么 `--port 9000` 的运行从未占用端口？(c) `Loading engine...` 这行日志在代码里的位置为什么是「校验全过」的分界证据？
4. **进阶（可选）**：阅读 [tests/detect_gate.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-server/tests/detect_gate.rs#L1-L11) 的模块文档，思考它为什么选择驱动**真实二进制**而不是单元测试各组件——用你画好的流程图解释「main 读取 config.json → detect → hint → launch 拒绝」这条接线为什么只有端到端才覆盖得到。

## 6. 本讲小结

- `main()` 是一条十余阶段的装配线：注册表 → CLI 合并解析 → 读 config → detect（+ feature 提示）→ 三级校验（consume-or-reject、共享规则、线内矩阵）→ serve_plan → 引擎加载 → 三条 serve 分支 → flush → 传播。
- 每个阶段的失败形态不同：clap 以退出码 2 直接结束进程；`DetectError`/`CliError` 按 thiserror 模板渲染；文件与引擎错误是 anyhow 上下文链（`failed to read …` → `failed to start Qwen3 engine: …` → `vLLM frontend server failed`）；注册表自检错误是启动期 panic。
- 所有廉价校验都排在昂贵的引擎加载之前——「别为必败请求花一次引擎加载」是源码注释里明写的动机。
- 引擎加载放 `spawn_blocking` 与 HTTP 前端并行；前端只在引擎注册后 bind 端口，就绪语义不变；LoRA 路由因构建期就要控制平面而串行，且要求 `LaunchedEngine::Stepped`。
- Ctrl-C 是两段式：加载期（不可取消）保持默认 kill 行为，引擎就绪后才挂优雅关停 token。
- `tracing::flush()` 在错误传播之前执行，保证失败路径上最后一批请求 span 不丢。

## 7. 下一步学习建议

- **下一讲（u1-l4）**顺着本讲阶段 6 往下钻：`detect` 的三种结果、`families()` 身份表为什么是「必要的第二真相源」、`feature_gate_hint` 的能力边界，以及 `detect_gate.rs` 如何端到端验证这道门。
- 之后进入单元二：`ModelLine` trait 的 8 个方法（u2-l1）正是本讲阶段 6、9、10、11 背后的接口契约；`SharedArgs` 与消费子集（u2-l2）解释阶段 7-8 的全部分类学。
- 想提前看 serve 分支内部，可跳读 `../pegainfer-frontend/src/vllm/mod.rs` 里 `serve_with_engine_count` 的签名与文档；系统讲解在 u4-l1。
- 引擎契约（`LaunchedEngine` 两代形态、账本、驱动循环）是单元三的主题，本讲只消费了它的枚举定义。
