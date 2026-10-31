# u5-l2 · 从零接入一条新模型线：完整清单

## 1. 本讲目标

本讲是前四个单元的收官：把散落在 `ModelLine` trait（u2-l1）、注册表自检（u2-l3）、`Scheduler` 契约（u3-l5）、serve 函数族（u4-l1）里的知识，收拢成一份**可照着做的新模型线接入清单**。整份清单有官方参考实现——`pegainfer-frontend/examples/echo-server.rs`，一个不含 GPU、不含权重的完整模型线。

学完本讲，你应该能：

- 按顺序复述接入步骤：**实现 `ModelLine` → 注册进 `model_lines()` → Cargo feature → `families()` 行 + 单测 → detect_gate 式端到端测试**；
- 解释 echo-server.rs 为什么是官方参考：它把 probe + Scheduler + launch + 镜像 main 的服务流程全部装进一个无 GPU 的 example；
- 说出 detect_gate.rs 的双 fixture 设计——冻结 config 来自模型 crate（qwen4），tokenizer 来自 echo-model，两条断言路径都不触达 CUDA。

## 2. 前置知识

本讲假设你已读完 u2-l1（trait 接缝）、u2-l3（注册表两本账）、u3-l5（三方法调度器）与 u4-l1（serve 装配骨架）。在此之上补三个工程层面的概念：

- **example 即参考实现**：Cargo 的 `examples/` 目录通常放演示代码，但这个仓库把 echo-server.rs 当作「接入新线的活文档」维护——文件头注释明确写着 "It doubles as the reference for wiring a model line in"。它的价值在于：新线作者不需要读 8 个模型 crate 的差异，只需要读这 234 行的**最小完整实现**。
- **`CARGO_BIN_EXE_<name>` 环境变量**：cargo 为集成测试（`tests/` 目录）注入的编译期变量，值是本 crate 二进制的绝对路径。集成测试用它 `spawn` 真实二进制，这是「单元测试测函数、集成测试测接线」的分界线——main 里 detect → hint → launch 的**串联**只有跑起二进制才被覆盖。
- **`include_str!` + `env!("CARGO_MANIFEST_DIR")` 的 fixture 模式**：把 fixture 文件在编译期内嵌进测试二进制，路径用 `concat!(env!("CARGO_MANIFEST_DIR"), "/../<crate>/tests/<file>")` 跨 crate 引用。配合点是：fixture 有**唯一属主**（冻结 config 由 qwen4 crate 提交），消费方只引用、不复制——两份拷贝必然漂移。

再回顾两个贯穿全讲的旧结论：注册表构造期对 flag 的 id 与拼写做唯一性 panic（u2-l3）；`LaunchedEngine` 是迁移期 Handle/Stepped 二选一枚举，新线一律返回 `Stepped`（u3-l1）。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| `../pegainfer-frontend/examples/echo-server.rs` | **官方参考实现**：无 GPU 完整模型线 | `EchoLine`、`EchoScheduler`、镜像的 main |
| `../pegainfer-frontend/examples/echo-model/config.json` | echo 线的「模型」fixture | `model_type: "echo"` 被 probe 认领 |
| `src/main.rs` | server 二进制：纯分发 | `model_lines()`、`families()`、hint 单测 |
| `Cargo.toml` | server 的依赖与 feature 表 | optional 依赖、feature 转发模式 |
| `tests/detect_gate.rs` | 检测门集成测试 | 双 fixture、两条出厂路径断言 |
| `../pegainfer-qwen4/src/model_line.rs` | detect_gate 的被测对象 | 「认领后诚实拒绝」的 launch |
| `../pegainfer-qwen4/src/probe.rs`（辅读） | qwen4 身份常量 | `MODEL_TYPE`/`TEXT_MODEL_TYPE` 绑定 |
| `../pegainfer-frontend/src/engine/driver.rs`（辅读） | Scheduler trait 与 drive 循环 | `spawn_scheduler` 的用法 |
| `../pegainfer-frontend/src/engine/wiring.rs`（辅读） | step 引擎交接包 | `Engine`/`EngineInfo` 字段语义 |
| `../Cargo.toml`（辅读） | workspace 成员表 | 新 crate 需在此登记 |

> 提醒：echo-server 与 detect_gate 全程不触达 CUDA；frontend crate 本身是 CUDA-free 的。

## 4. 核心概念与源码讲解

### 4.1 EchoLine / EchoScheduler：一个不含 GPU 的完整模型线

#### 4.1.1 概念说明

接入新线前最自然的问题是：「我对 frontend 契约的**全部**义务到底是什么？」答案有两个极端：读 trait 定义（太抽象，看不到装配），或读 qwen3 的实现（太重，LoRA、P/D、显存预算全混在里面）。echo-server.rs 是刻意维护的中间态——**一条模型线的完整纵切面**：

```text
OpenAI HTTP (/v1/completions)
      │  vLLM 引擎协议（zmq + msgpack）
      ▼
SteppedEngineBridge（u4-l3）
      │  scheduler_pair 双端
      ▼
Scheduler contract：submit / step / metrics（u3-l5）
      │  RequestLedger 记账（u3-l4）
      ▼
EchoScheduler —— 引擎被换成「把 prompt 吐回去」的玩具
```

从 HTTP 到调度器契约的**每一层都是生产代码**，只有最底层的「模型」是假的。这使它同时充当三样东西：接入参考、frontend 契约的回归测试床（u3-l5、u4-l2 的实践都用它）、以及本仓库无 GPU 环境下的开发入口。

模型线本身（`EchoLine`）遵循「最小清单」：`probe` 认领一个 `config.json` 身份，`launch` 起调度器——没有独占 CLI flag，不消费任何非核心共享 flag。这正是 u2-l1 讲过的「三个必选方法 + 五个默认实现」的最小实例。

#### 4.1.2 核心流程

echo-server 的 `main` 与 `pegainfer-server` 的 `main` 同构，只是把注册表缩减到一条线：

```text
ModelLineRegistry::new(vec![&MODEL_LINE])     # 只注册 echo 一条线
        │
        ▼ registry.build_command(clap::Command)   # 拼出含共享 flag 的 CLI
        ▼ 读 config.json → registry.detect        # probe 认领 model_type=="echo"
        ▼ validate_provided + shared.validate      # 与 server 完全相同
        ▼ LaunchContext → line.serve_plan          # 默认 ServePlan
        ▼ line.launch                              # 起 1 个 EchoScheduler
        ▼ vllm::serve_with_engine_count(...)       # 与 server 非 LoRA 分支同款
```

`EchoScheduler` 的逐步逻辑（一个纯内存的两队列状态机）：

1. **接纳**：排空 `queued`；先查 `ledger.is_aborted`（已取消则 `retire` 静默退役），再算 `echo_len = min(prompt_len, max_tokens)`，把 `prompt[..echo_len]` **倒序**存进 `pending`（尾弹即正序），`ledger.admit` 开活跃户；
2. **产出**：每个 running 请求每步 `pending.pop()` 弹一个 token，`ledger.push_tokens` 记账；弹尽则 `ledger.finish`，`FinishReason::Stop`（完整回声）或 `Length`（被 `max_tokens` 截短）；
3. **度量**：`metrics()` 报告 running/waiting 计数，其余字段走默认值。

每步只吐一个 token 是刻意的：让逐 token 的流式路径（SSE、桥的批处理、StatsTracker）保持温热，而不是把整个回答一次folded成一批。

#### 4.1.3 源码精读

**注册的静态实例。** server 二进制注册的是 `&'static dyn ModelLine`，所以每条线都以一个无名 `static` 暴露自己——echo 也不例外：

- [echo-server.rs:L57](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L57)：`static MODEL_LINE: EchoLine = EchoLine;` —— 单元结构体零开销，`&MODEL_LINE` 即可进注册向量。对比 qwen4 的 [model_line.rs:L12](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/model_line.rs#L12)（`pub static MODEL_LINE: Qwen4Line`），真实线的这个 static 是 `pub` 的——因为要被 server crate 跨 crate 引用。

**`ModelLine` 的最小实现。** 三个必选方法一目了然：

- [echo-server.rs:L64-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L64-L92)：完整的 `impl ModelLine for EchoLine`。
  - `name()` 返回 `"Echo"`——它会出现在 server 启动横幅 `=== pegainfer - Echo (GPU) ===` 与日志里，也是 detect_gate 集成测试断言 stderr 包含 `Qwen4` 的那条字符串的来源（[detect_gate.rs:L112](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L112)）。
  - `probe()` 只读 `config.get("model_type")`，等于 `"echo"` 才认领。这是 u1-l4 讲过的**身份检查**：不校验尺寸，几何校验留给后续阶段（qwen4 的 probe 例外地在 probe 里构建整个 `Config`，见 4.3.3）。
  - `launch()` 是唯一「干活」的方法：`spawn_scheduler("echo", EchoScheduler::default())` 起一个调度器线程，装进 `Engine` 交接包后 `.into()` 成 `LaunchedEngine::Stepped`。注意 [echo-server.rs:L84-L89](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L84-L89) 里两个 `None` 的显式语义（u3-l6）：`kv_capacity: None` = 无 KV 池可报、前端跳过 batch-fit 检查；`servable_len: None` = 不钳制最大长度、按模型上下文窗口校验；`lora: None` = 无 LoRA 能力，LoRA 路由根本不会挂载（u4-l5 的「Option 即能力」）。

**调度器状态机。** 状态只有两条 Vec，`RunningRequest` 的字段注释本身就是设计说明：

- [echo-server.rs:L99-L114](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L99-L114)：`queued: Vec<QueuedRequest>`（submit 攒下的待接纳）与 `running: Vec<RunningRequest>`；`pending` 注释写明「倒序存储为了廉价的弹出」，`truncated` 决定完结原因。
- [echo-server.rs:L117-L119](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L117-L119)：`submit` 只做所有权转移（`self.queued.push`）——u3-l5 的契约「裁决延迟到 step 零代价」在这里落地。
- [echo-server.rs:L121-L158](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L121-L158)：`step` 全文。接纳段（L122-L137）先 `is_aborted` 后 `admit`，顺序对应「取消检查要覆盖刚提交还没跑的请求」；产出段（L139-L156）每请求要么 `push_tokens` 留在 running，要么 `finish` 关户。**所有裁决都从 step 这一个发射点写账本**，`submit` 与 `metrics` 都不碰账本——这是 drive 循环能保证「一步一 commit」的前提。
- [echo-server.rs:L160-L166](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L160-L166)：`metrics` 只填 running/waiting 两个 gauge，其余用 `..SchedulerMetrics::default()` 展开——u2-l5 说过，`..Default::default()` 展开是防字段漂移的约定。

**镜像的 main。** echo 的 `main` 与 server 的 `main`（u1-l3）逐步同构，差异全在「echo 不需要的东西」：

- [echo-server.rs:L174-L233](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L174-L233)：全文。三点关键差异：① [L186-L189](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L186-L189) 用 `provided_args` 判断用户没给 `--model-path` 时默认指向 checked-in fixture——`provided` 的「出身语义」（u2-l2）在这里被用作「是否需要默认值」的判据；② [L210](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L210) 的 `launch` 直接同步调用——注释明说真实线这里藏着阻塞的权重加载，server 用 `spawn_blocking` 把它与 HTTP 前端装配并发（u4-l1）；③ 只有 `serve_with_engine_count` 一个 serve 分支，因为默认 `ServePlan` 没有 LoRA 模块也不是 prefill-only。

#### 4.1.4 代码实践

**实践目标**：跑通官方参考，确认「completion 文本 == prompt」这条回声性质，并观察 max_tokens 截短时的完结原因。

1. **操作步骤**（无需 GPU）：
   ```bash
   # 工作区任意目录
   cargo run -p pegainfer-frontend --example echo-server
   # 另开一个终端
   curl http://127.0.0.1:8000/v1/completions \
     -H 'content-type: application/json' \
     -d '{"model": "echo", "prompt": "hello, pegainfer", "max_tokens": 64}'
   # 再把 max_tokens 改成 3 重发一次
   ```
2. **需要观察的现象**：第一次响应的 `text` 是 `"hello, pegainfer"`；`finish_reason` 为 `stop`。第二次只回 3 个字符（按 tokenizer 的切法，ByteLevel BPE 下大致是 `"hel"`），`finish_reason` 变为 `length`——对应 [echo-server.rs:L148-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L148-L153) 的 `truncated` 分支。
3. **预期结果**：两次请求都成功；若加 `"stream": true`，能看到 token 逐个到达（每步一个 token 的设计在起作用）。
4. 具体输出文本待本地验证（tokenizer 的确切切分以本地运行为准）。

#### 4.1.5 小练习与答案

**练习 1**：`pending` 为什么要倒序存储？

**答案**：产出段用 `Vec::pop()` 从尾部弹出（[echo-server.rs:L144](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-server.rs#L144)），尾弹是 O(1)，头弹 `remove(0)` 是 O(n)。想让弹出顺序等于 prompt 的正序，就在接纳时先把切片 reverse——存的时候付一次 O(n)，之后每步免费。这也是本讲综合实践「Reverse 线」的支点：删掉那一行 reverse，出队方向就变了。

**练习 2**：`max_tokens: 0` 的请求会怎样？会 panic 吗？

**答案**：不会 panic。`echo_len = min(prompt_len, 0) = 0`，`pending` 为空，`truncated = 0 < prompt_len = true`（prompt 非空时）；接纳后第一步就走进 `finish` 分支，以 `FinishReason::Length` 完结、零 token 产出。账本侧完全合法：admit 后 finish 是正常关户路径（u3-l4）。

**练习 3**：echo 线的 `launch` 里 `lora: None`，如果用户给 server 加上 `--enable-lora` 会发生什么？

**答案**：什么都不会发生——`--enable-lora` 是 Qwen3Line 的独占 flag（u2-l4），echo 线根本没有这条 flag；即便某条线声明了 LoRA 模块，`Engine::lora` 为 `None` 时 LoRA 路由不会挂载、`serve_model_with_lora_routes` 也不会被走到（`Option` 即能力，u4-l5）。能力不在 flag 里，在交接包里。

### 4.2 server 侧的注册面：model_lines()、Cargo feature 与 families() 行

#### 4.2.1 概念说明

u5-l3 会复盘「纯分发二进制」的架构取舍，本节只看它的**操作面**：server 对一条新线的全部认知分散在三张表里，三张表必须同步演化：

| 表 | 位置 | 内容 | 为什么需要它 |
|---|---|---|---|
| 依赖/feature 表 | `Cargo.toml` | optional 依赖 + feature 名 | feature 决定二进制里有什么 |
| 注册向量 | `src/main.rs` `model_lines()` | cfg 守卫的 `&'static dyn ModelLine` 行 | 编译进来的线参与 detect |
| 身份表 | `src/main.rs` `families()` | feature 名 + probe 认领的 model_type + compiled 位 | 被编译掉的线连 probe 都不在二进制里，提示只能查硬编码表 |

第三张表的存在理由在 u1-l4 已论证（「必要的第二真相源」）：`feature_gate_hint` 服务于「线被编译掉」的情形，而那时**没有任何代码可以从该 crate 读到身份字符串**，只能硬编码。于是每接入一条线，`families()` 必须加一行，且要有测试把这一行和 crate 自己的身份常量**绑死**——否则两边各自漂移，提示就会撒谎。

#### 4.2.2 核心流程

完整的接入清单（本讲的中心产出，后面各节逐一对应源码）：

1. **模型 crate**：新建 `pegainfer-<line>`（或复用现有），实现 `ModelLine` 三必选 + 按需的五个默认方法；暴露 `pub static MODEL_LINE`；在根 `Cargo.toml` 的 `members` 登记。
2. **server 依赖**：`pegainfer-server/Cargo.toml` 加 optional 依赖；`[features]` 加 `"<line>" = ["dep:pegainfer-<line>"]`；若模型 crate 有同名内核 feature 还要转发（见 4.2.3）。
3. **注册**：`model_lines()` 加一行 `#[cfg(feature = "<line>")] &pegainfer_<line>::model_line::MODEL_LINE,`。
4. **身份表**：`families()` 加一行 `Family { feature, model_types, text_model_types, compiled: cfg!(...) }`；若 crate 暴露 `MODEL_TYPE` 常量，加身份绑定测试。
5. **hint 单测**：`#[cfg(not(feature = "<line>"))]` 门下断言提示包含 `--features <line>`，再加「已编译时沉默」「未知家族沉默」两个反向用例。
6. **检测门集成测试**：仿 `tests/detect_gate.rs`，用冻结 config + echo-model tokenizer 驱动真实二进制，锁住两条出厂路径。
7. **CI**：确保至少一条 CI 线用 `--features <line>` 编译，否则第 4、5 步的 cfg 门用例永远不编译（具体 CI 落点待确认——本仓库工作流配置不在本 crate 目录内）。

```text
用户: pegainfer --model-path models/NewModel
        │
   读 config.json ──► registry.detect（遍历编译进来的线）
        │                    │
        │             唯一认领 ──► validate/serve_plan/launch   ← 表 2（model_lines）
        │             NoMatch
        ▼                    ▼
   feature_gate_hint 查 families()                                ← 表 3
        │             （未编译的家族 → "rebuild with --features X"） ← 表 1 决定谁在场
```

#### 4.2.3 源码精读

**表 1：feature 表与两种写法。**

- [Cargo.toml:L18-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/Cargo.toml#L18-L26)：全部模型 crate 都是 `optional = true`——这是 feature 能把它们「按需拉入」的前提（u1-l2）。
- [Cargo.toml:L34-L48](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/Cargo.toml#L34-L48)：`[features]` 表呈现两种模式——`qwen4 = ["dep:pegainfer-qwen4"]` 只拉依赖（crate 默认 feature 即完整）；`gemma4 = ["dep:pegainfer-gemma4", "pegainfer-gemma4/gemma4"]` 还要**向 crate 内部同名 feature 转发**，因为那些 crate 把 CUDA 内核面也锁在自己的 feature 后面。新线接入时选哪种，取决于模型 crate 是否有内核 feature。

**表 2：注册向量。**

- [main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L24-L43)：`model_lines()` 全文——8 行 cfg 守卫对应 8 个 feature，按字典序排列。新线加一行即是「server 侧的全部改动」；向量字面量里的 `#[cfg]` 属性让这行在 feature 关闭时彻底消失（连类型检查都不参与）。

**表 3：身份表与绑定测试。**

- [main.rs:L45-L52](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L45-L52)：`Family` 结构——feature 名、probe 认领的顶层 `model_type` 集合、`text_config.model_type` 集合、编译位。
- [main.rs:L67-L118](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L67-L118)：`families()` 全表。注意 `compiled: cfg!(feature = "...")` 是**编译期求值**，所以同一份源码在 stock 与 enabled 构建里给出不同的表。
- [main.rs:L324-L337](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L324-L337)：绑定测试的范本——`qwen4_hint_identity_matches_the_line_crate` 在 `#[cfg(feature = "qwen4")]` 门下把 `families()` 的 qwen4 行与 [probe.rs:L16-L17](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/probe.rs#L16-L17) 的 `MODEL_TYPE`/`TEXT_MODEL_TYPE` 常量逐字断言相等。这正是清单第 4 步说的「绑死」：改一边不改另一边，测试立刻红。
- [main.rs:L295-L322](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L295-L322)：hint 单测的四个用例——已知未编译家族出提示（L295-L301）、`text_config` 身份也能命中（L303-L309）、已编译家族沉默（L311-L316）、未知家族沉默（L318-L322）。注意前两个用例挂在 `#[cfg(not(feature = ...))]` 下：它们断言的正是「线不在场」的行为。

**消费点。**

- [main.rs:L123-L144](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L123-L144)：`feature_gate_hint` 查顶层与 `text_config` 两个键，命中「未编译且身份匹配」的家族即返回 `rebuild with --features X` 提示。它只在 `DetectError::NoMatch` 时被接线（[main.rs:L168-L175](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L168-L175)）——Conflict 是注册表 bug，不配得到提示。

#### 4.2.4 代码实践

**实践目标**：在**草稿**（不改动真实源码，抄到自己的分支或临时补丁里）中完成清单第 2、3、4、5 步的 Reverse 线增量，并让它通过 `cargo check`。

以下为**示例代码**（非项目原有代码），按 4.2.3 的三个落点组织成一份 diff 式清单：

```rust
// ── pegainfer-server/Cargo.toml（示意）────────────────────────
// [dependencies] 追加：
//   pegainfer-reverse = { workspace = true, optional = true }
// [features] 追加：
//   reverse = ["dep:pegainfer-reverse"]

// ── src/main.rs（示意）────────────────────────────────────────
fn model_lines() -> Vec<&'static dyn ModelLine> {
    vec![
        // ...现有行...
        #[cfg(feature = "reverse")]
        &pegainfer_reverse::model_line::MODEL_LINE,   // 表 2：一行注册
    ]
}

fn families() -> [Family; 9] {                        // 表 3：数组长度 8→9
    [
        // ...现有行...
        Family {
            feature: "reverse",
            model_types: &["reverse"],
            text_model_types: &[],
            compiled: cfg!(feature = "reverse"),
        },
    ]
}

// ── src/main.rs tests（示意）──────────────────────────────────
#[cfg(not(feature = "reverse"))]
#[test]
fn hint_names_the_feature_for_reverse() {
    let config = serde_json::json!({"model_type": "reverse"});
    let hint = feature_gate_hint(&config).expect("reverse identity should hint");
    assert!(hint.contains("--features reverse"), "{hint}");
}
```

1. **操作步骤**：在独立分支应用上述草稿（模型 crate 可先用 4.3 与第 5 节产出的 Reverse 线）；`cargo check -p pegainfer-server` 验证 stock 构建编译通过；`cargo test -p pegainfer-server --lib` 应看到新 hint 测试通过。
2. **需要观察的现象**：不启用 feature 时 `cargo tree -p pegainfer-server` 里没有 pegainfer-reverse；`families()` 的数组长度类型 `[Family; 9]` 迫使你同时补齐行——少一行编译不过，这是刻意的防漂移设计。
3. **预期结果**：stock 构建下草稿测试绿；`--features reverse` 构建下该测试被 cfg 门编译掉（对应「已编译家族沉默」由 L311-L316 的模式补位）。
4. 实际运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `families()` 返回**定长数组** `[Family; 8]` 而不是 `Vec`？

**答案**：定长数组的长度写进类型，忘加行时 `[Family; 8]` 与 9 个元素的字面量类型不匹配，**编译期**报错；若返回 `Vec`，少一行只是运行期少一个家族，提示静默缺失。用类型系统当清单的 completion 检查，与 u2-l3「构造期 panic 管代码错」是同一个思想：代码错误要尽量左移到编译期。

**练习 2**：`compiled: cfg!(feature = "gemma4")` 为什么不用 `#[cfg]` 属性守卫整个 `Family` 行？

**答案**：两者的区别在「谁在场」。`#[cfg]` 守卫会让整行在 feature 关闭时**从表中消失**——而 `families()` 的全部意义就是描述**不在场**的线（`feature_gate_hint` 只对 `!compiled` 的行感兴趣）。`cfg!` 宏编译期求值成 `false` 但行仍在表里，正是「记录缺席者」需要的语义。

### 4.3 detect_gate 集成测试：双 fixture 与两条出厂路径

#### 4.3.1 概念说明

单元测试覆盖的是零件：probe 有自己的测试（[model_line.rs:L41-L47](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/model_line.rs#L41-L47)），hint 也有四个单测。但「main 读 config.json → registry.detect → NoMatch 时拼 hint / 认领后 launch 拒绝」这条**接线**只活在 `main` 函数里——不跑二进制就没有覆盖。`tests/detect_gate.rs` 的定位就写在文件头（[detect_gate.rs:L1-L11](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L1-L11)）：驱动真实二进制，断言两种**出厂行为**。

它的精妙处在 **双 fixture 设计**：

- **冻结 config 来自模型 crate**：`FROZEN_CONFIG` 内嵌自 `../pegainfer-qwen4/tests/frozen_config.json`——真实 checkpoint 的 config，由线 crate 提交、作为线 crate 的测试资产。注释明说「server 没有自己的 fixture，这道门就该见真家伙」。fixture 唯一属主，消费方 `include_str!` 引用。
- **tokenizer 来自 echo-model**：`TOKENIZER` 内嵌自 `../pegainfer-frontend/examples/echo-model/tokenizer.json`。为什么需要它？因为 **enabled 构建会走到 serve 路径**——HTTP 前端没有 tokenizer 会拒绝启动，届时 stderr 里是「缺 tokenizer」而非「拒绝服务」，测试就断言错了对象。这是「让目标错误真正浮出水面」的测试工程细节。

两条路径都被设计成**不触达 CUDA**：检测是纯 JSON 判断；enabled 路径的 `launch` 在引擎构建之前就 bail（见 4.3.3）。于是这个测试可以在无 GPU 的 CI 上跑。

#### 4.3.2 核心流程

```text
model_dir(tag): temp_dir/pegainfer-detect-gate-{tag}-{pid}
        │  写 config.json（FROZEN_CONFIG） + tokenizer.json（TOKENIZER）
        ▼
run_against(dir): spawn CARGO_BIN_EXE_pegainfer --model-path dir
        │  stdout → null；stderr → 文件（防管道塞死子进程）
        │  50ms 轮询 try_wait，超时 60s kill 并 panic
        ▼
┌─ #[cfg(not(feature = "qwen4"))] stock 用例 ──────────────────┐
│  断言: !status.success()                 （不许认领）          │
│  断言: stderr 含 "--features qwen4"       （提示在场）          │
└──────────────────────────────────────────────────────────────┘
┌─ #[cfg(feature = "qwen4")] enabled 用例 ─────────────────────┐
│  断言: !status.success()                 （不许服务）          │
│  断言: stderr 含 "Qwen4"                 （认领发生了）        │
│  断言: stderr 含 "not implemented"        （拒绝原因在场）      │
└──────────────────────────────────────────────────────────────┘
```

两个用例靠互斥的 cfg 门**分身于两种构建**：同一份测试文件，stock 构建编译前者、`--features qwen4` 构建编译后者，各自锁住一条出厂路径。

#### 4.3.3 源码精读

**fixture 常量与目录装配。**

- [detect_gate.rs:L22-L37](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L22-L37)：两个 `include_str!` 常量，注释分别解释了「真实 config」与「为什么还需要 tokenizer」——后者正是 4.3.1 分析的那段「否则会浮现错误的错误」。
- [detect_gate.rs:L39-L48](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L39-L48)：`model_dir` 把两个 fixture 写进带 pid 的临时目录，用后即删（用例里 `remove_dir_all`）。

**进程驱动器。**

- [detect_gate.rs:L57-L83](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L57-L83)：`run_against` 的三个工程决定——① stderr 落**文件**而非管道：管道若无人读满会阻塞子进程，文件不会；② 50ms 轮询 `try_wait` 而非阻塞 `wait`：超时才能介入；③ 超时 panic 时把已写的 stderr 打出来，让挂死路径也留下现场。60 秒超时（[L52](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L52)）注释明说只为兜住「永不返回的启动/取消路径」——二进制热身后两条路径都一秒内退出。

**两条断言。**

- [detect_gate.rs:L85-L99](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L85-L99)：stock 用例。断言失败消息本身就是文档——"the stock build must not claim a qwen4_exp config"。
- [detect_gate.rs:L101-L119](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L101-L119)：enabled 用例。三条断言合起来锁住完整叙事：**认领发生了（Qwen4），但服务被拒（not implemented），且整体失败**。

**被测的「诚实拒绝」。**

- [model_line.rs:L29-L31](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/model_line.rs#L29-L31)：qwen4 的 `launch` 一句 `anyhow::bail!("this qwen4_exp checkpoint is detected, but inference is not implemented yet")`——这条消息就是 enabled 用例断言的 `not implemented`。它示范了接入**早期**的一条线的正确姿势：probe 已经能认领（且 probe 里的 `Config::from_json` 顺带做了几何校验，[L23-L27](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/model_line.rs#L23-L27)），但推理未实现，于是 detect 放行、launch 拒绝——**宁可诚实失败，不带病服务**。文件头注释（[L1-L4](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-qwen4/src/model_line.rs#L1-L4)）把这一策略写成了该 crate 的第一句话。

#### 4.3.4 代码实践

**实践目标**：为 Reverse 线写一个 detect_gate 式测试（草稿），体会「双 fixture + 互斥 cfg 门」如何照搬。

以下为**示例代码**（非项目原有代码）：

```rust
// tests/reverse_detect_gate.rs（示意，存于 pegainfer-server/tests/）
const FROZEN_CONFIG: &str = r#"{"model_type": "reverse", "architectures": ["ReverseModel"]}"#;
// tokenizer 直接复用 echo-model 的（唯一属主）：
const TOKENIZER: &str = include_str!(concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../pegainfer-frontend/examples/echo-model/tokenizer.json"
));

#[cfg(not(feature = "reverse"))]
#[test]
fn stock_build_names_the_reverse_feature() {
    let dir = model_dir("reverse-stock");           // 照抄 detect_gate::model_dir
    let (status, stderr) = run_against(&dir);       // 照抄 detect_gate::run_against
    assert!(!status.success());
    assert!(stderr.contains("--features reverse"), "{stderr}");
}

#[cfg(feature = "reverse")]
#[test]
fn enabled_build_serves_the_reverse_line() {
    let dir = model_dir("reverse-enabled");
    let (status, _stderr) = run_against(&dir);      // Reverse 线能服务：此处断言方式
    // 需要改造：能服务的线不会退出，应改为 HTTP 探活后优雅关停，
    // 或让 launch 先拒绝（qwen4 模式）再断言拒绝原因。
    assert!(status.success());
}
```

1. **操作步骤**：把 `detect_gate.rs` 的 `model_dir`/`run_against` 两个帮手抄进新文件（或提取共用）；注意上面 enabled 用例的注释——**能服务的线不会自己退出**，`run_against` 的「等退出」模型对它不成立，要么走 qwen4 式「认领后拒绝」，要么改成起服务再探测端口后杀进程。这是照搬模板时最值得想清楚的一步。
2. **需要观察的现象**：stock 用例红/绿直接反映 4.2.4 草稿里 `families()` 行与 `feature_gate_hint` 的正确性；它等价于把 u1-l4 的手工实验自动化。
3. **预期结果**：stock 构建下 `cargo test -p pegainfer-server --test reverse_detect_gate` 通过。
4. 实际运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：enabled 用例为什么断言 stderr 含 `"Qwen4"`（line 的名字）而不是 `"qwen4"`（feature 名）？

**答案**：因为这条路径上**根本没人提起 feature**——feature 只活在构建系统里。enabled 构建里 detect 成功放行，stderr 里出现的是 server 启动日志与 `launch` 的 bail 消息，其中 `line.name()`（`"Qwen4"`）出现在 `=== pegainfer - Qwen4 (GPU) ===` 横幅与 `failed to start Qwen4 engine` 上下文里。断言名字证明「认领并走到了 launch」；而 feature 名出现在 stock 用例的 hint 里。两个字符串各属一条路径。

**练习 2**：`run_against` 把 stderr 重定向到文件而不是 `Stdio::piped()`，防的是什么故障？

**答案**：管道缓冲区有上限（通常 64KB）。若子进程写出超过上限而父进程迟迟不读，子进程的 `write` 会阻塞——进程不退出，测试在错误的地方超时。落文件则子进程永远不被反压，父进程退出后再一次性读回。同时 50ms 轮询 `try_wait` 让父进程在等待期间还能检查 deadline，超时 kill 后仍能把文件内容打进 panic 消息。

**练习 3**：如果把 `TOKENIZER` fixture 删掉，哪个用例会坏、怎么坏？

**答案**：只有 enabled 用例坏。没有 tokenizer，HTTP 前端在 serve 装配时拒绝启动，stderr 变成 tokenizer 缺失错误，`not implemented` 断言失败——但失败原因是「浮现了错误的错误」。stock 用例不受影响：它在 detect 阶段就退出了，根本走不到需要 tokenizer 的地方。这正是 fixture 注释（[detect_gate.rs:L29-L33](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/tests/detect_gate.rs#L29-L33)）强调的那段设计动机。

## 5. 综合实践

**任务**：把 echo-server.rs 复制改造成 **Reverse 线**——prompt 逆序逐 token 吐回——并完成接入清单的 server 侧草稿。这是贯穿本讲三块内容的闭环：4.1 给了模板，4.2 给了注册面增量，4.3 给了端到端断言。

### 第一步：Reverse 调度器（改出队方向）

复制与改造（**示例代码**，相对 echo-server.rs 的全部 diff）：

```rust
// pegainfer-frontend/examples/reverse-server.rs（由 echo-server.rs 复制）
static MODEL_LINE: ReverseLine = ReverseLine;

struct ReverseLine;

impl ModelLine for ReverseLine {
    fn name(&self) -> &'static str { "Reverse" }

    fn probe(&self, config: &serde_json::Value) -> Result<(), String> {
        let model_type = config.get("model_type").and_then(serde_json::Value::as_str);
        if model_type == Some("reverse") {
            Ok(())
        } else {
            Err(format!("model_type {model_type:?} is not \"reverse\""))
        }
    }
    // launch 与 echo 完全相同：spawn_scheduler("reverse", ReverseScheduler::default())
}

// ReverseScheduler 与 EchoScheduler 逐字段相同，唯一 diff 在接纳段：
let mut pending = Vec::from(&request.prompt_tokens[..echo_len]);
// pending.reverse();   ← 删掉这一行！
// 存正序 + 尾弹 ⇒ 产出顺序 = prompt 逆序
```

fixture：新建 `pegainfer-frontend/examples/reverse-model/`，`config.json` 写 `{"model_type": "reverse", "architectures": ["ReverseModel"], "vocab_size": 256, "max_position_embeddings": 4096}`（其余字段照抄 [echo-model/config.json](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-model/config.json#L1-L6)），`tokenizer.json` 直接复制 echo-model 的；`FIXTURE_MODEL_DIR` 指向新目录。

### 第二步：跑起来验证

1. `cargo run -p pegainfer-frontend --example reverse-server`（无需 GPU）。
2. 验证顺序：
   ```bash
   curl http://127.0.0.1:8000/v1/completions \
     -H 'content-type: application/json' \
     -d '{"model": "reverse", "prompt": "abcdef", "max_tokens": 64}'
   ```
3. **需要观察的现象**：`text` 为 `"fedcba"`（tokenizer 是 ByteLevel BPE，ASCII 输入下字节级反转即字符反转；请用 ASCII prompt——多字节 UTF-8 逆序字节会得到非法文本）。`finish_reason` 为 `stop`；把 `max_tokens` 降到 3，观察回文只剩 3 个字符且 `finish_reason` 变 `length`。
4. **预期结果**：输出顺序与 echo 相反，流式行为不变（仍每步一个 token）。
5. 具体输出文本待本地验证。

### 第三步：完成清单剩余项（草稿）

按 4.2.4 加 `families()` 行与 hint 单测、按 4.3.4 加检测门测试。若想走完「真实 crate」路线，则再补：把 Reverse 线挪进独立的 `pegainfer-reverse` crate（在根 `Cargo.toml` `members` 登记，[Cargo.toml:L3-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../Cargo.toml#L3-L26)），server 加 optional 依赖与 feature，`model_lines()` 注册——此时 `cargo run --release --features reverse -- --model-path examples/reverse-model` 应得到同一台服务器。

### 检查表（做完后自查）

- [ ] probe 只做身份认领，几何/尺寸校验要么不做、要么像 qwen4 那样在 probe 里显式构建 Config；
- [ ] `launch` 返回 `LaunchedEngine::Stepped`（新线不写 legacy Handle）；
- [ ] `serve_plan` 用 `..ServePlan::default()` 展开实现（防漂移，u2-l5）；
- [ ] 调度器只实现 `submit`/`step`/`metrics`，一切裁决从 `step` 写账本（u3-l5）；
- [ ] `families()` 行与 crate 身份常量有绑定测试；
- [ ] hint 有「未编译出提示、已编译沉默、未知家族沉默」三类用例；
- [ ] 检测门测试两条出厂路径都被某个构建编译并运行。

## 6. 本讲小结

- **接入清单**：实现 `ModelLine`（name/probe/launch 必选）→ server `Cargo.toml` optional 依赖 + feature → `model_lines()` 一行 → `families()` 一行 + 身份绑定测试 → hint 单测 → detect_gate 式集成测试；新线对 server 源码的全部改动就是注册向量与身份表各一行。
- **echo-server.rs 是官方参考**：一个无 GPU 的完整模型线纵切面——HTTP → vLLM 协议 → Scheduler 契约全是生产代码，只有引擎是「回声玩具」；它的 main 镜像 server main，差异集中在「echo 不需要的东西」（spawn_blocking、多 serve 分支、默认 model-path 的 provided 判断）。
- **`families()` 的定长数组与绑定测试**：`[Family; N]` 把「忘加行」变成编译错误，cfg 门下的常量相等断言把「行与 crate 漂移」变成测试失败——两张防漂移网。
- **detect_gate 的双 fixture 设计**：冻结 config 来自模型 crate（fixture 唯一属主），tokenizer 来自 echo-model（防「浮现错误的错误」），互斥 cfg 门让同一份测试在两种构建里各锁一条出厂路径，全程不触达 CUDA。
- **qwen4 示范了接入早期的诚实姿势**：probe 认领、launch 拒绝——宁可 "not implemented" 也不带病服务，并让集成测试锁住这条承诺。

## 7. 下一步学习建议

本讲补完后，单元五只剩最后一讲 **u5-l3（设计取舍复盘）**：它站在更高的视角回答三个「为什么」——纯分发二进制的代价与收益、`families()` 作为身份边界的过度承诺问题（量化变体也会命中提示）、两代引擎契约何时能删 `LaunchedEngine::Handle`。建议带着本讲清单去读：清单里每一处「为什么必须是三张表」「为什么定长数组」在 u5-l3 都有架构层的论证。

源码层面的延伸阅读：`../pegainfer-qwen3/src/model_line.rs`（8 方法满配的真实线，对照本讲的 3 方法最小实现）；`../pegainfer-qwen4/src/probe.rs`（probe 内做完整 Config 构建的反例，以及身份常量的定义处）；以及 `../pegainfer-frontend/src/engine/driver.rs` 的 `drive` 循环——如果你的 Reverse 线想加并发调度（比如每步吐多个请求的 token），那里是唯一需要重新理解的地方。
