# u3-l1 · 引擎契约总览：两代契约并存的地图

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出 `pegainfer-frontend/src/engine/` 模块的两代分组——新一代 **step 驱动契约**（step / ledger / request_lifecycle / metrics / wiring / driver / control）与迁移中的 **legacy 每 token 契约**（request / event / sink / kv / handle），并说出每个模块的核心类型与生产者/消费者角色。
2. 解释 `LaunchedEngine` 枚举为什么存在：它是迁移期 `ModelLine::launch` 返回值的「二选一」包装，`Handle` 变体装 legacy 引擎、`Stepped` 变体装 step 引擎；并说明 `pegainfer-server/src/main.rs` 的 LoRA 分支为什么只接受 `Stepped`。
3. 说出 `mod.rs` 的「扁平 re-export」约定：`pegainfer_frontend::engine::X` 这条路径不因内部分拆而改变，外部代码（例如 `main.rs`）从不需要知道某个类型实际住在哪个子模块文件里。

本讲是单元三的第一篇：先拿地图，再进 territory。后续各篇（legacy 线类型、step 线类型、账本、驱动循环、接线）都在这张地图上有明确坐标。

## 2. 前置知识

### 2.1 什么是「引擎契约」

在 [u1-l3](u1-l3-main-flow.md) 里我们走过了 `main()` 的装配线：`line.launch(&ctx)` 返回一个 `LaunchedEngine`，随后 main 按服务计划把它交给 vLLM 前端。这里的「引擎契约」就是 **frontend 与各模型 crate 之间的接口边界**：模型线把请求「吃进来」的方式、把生成结果「吐出去」的方式、以及引擎自我描述（几个调度器、多大容量）的方式。它相当于引擎的「插座形状」——插座由 frontend 定义，所有模型 crate 按同一形状造插头。

### 2.2 两代契约的直觉区别

- **legacy 每 token 契约**：一次请求的输出是一条**逐 token 的事件流**（`TokenEvent`：`Scheduled` → 若干 `Token` → 恰一个终态事件）。所有请求共享**一条**打好了请求标签的输出通道，前端按标签分拣（demux）。
- **step 驱动契约**：调度器每执行**一步**（一次 batch 前向），就产出**一条** `StepOutputs` 消息；其中每个被这一步触碰的请求至多带**一条扁平的** `RequestUpdate`——「这一步里发生在该请求上的一切」被压成一条记录，而不是一串事件。旧协议里「`Scheduled` 先于 token、终态恰一次」的顺序约定，在新协议里变成了**类型结构本身**：一条扁平记录根本无法表达「终态之后再出 token」。

### 2.3 生产者与消费者

本讲反复用这两个词：

- **生产者**：模型 crate 侧——实现 `Scheduler` trait 的调度器线程，它写账本、发步流、发布指标。
- **消费者**：frontend 侧——协议栈（vLLM 桥）拿 `SchedulerHandle` 提交请求、读步流、把结果翻译成 HTTP/SSE。

### 2.4 你还需要记得的两个 Rust 概念

- **枚举即 tagged union**：`LaunchedEngine` 是一个两变体枚举，每个变体携带不同的负载类型——这是 Rust 表达「迁移期二选一」的惯用法。
- **glob re-export**：`pub use wiring::*;` 把子模块的全部公开项搬进父模块命名空间，外部路径因此稳定。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `pegainfer-frontend/src/engine/mod.rs` | 契约的**目录页**：模块文档画出两代分组，13 个 `mod` 声明 + 13 条 `pub use` 扁平 re-export（全文仅 57 行） |
| `pegainfer-frontend/src/engine/wiring.rs` | 本讲主角类型所在地：`LaunchedEngine`、`Engine`、`EngineInfo`、`LiveScheduler`，以及 `scheduler_pair` / `SchedulerHandle` / `SchedulerBackend` / `MetricsPublisher` |
| `pegainfer-server/src/main.rs` | 消费者样本：`launch` 的返回值如何流进 serve 分支，LoRA 分支如何对 `Stepped` 做 let-else 模式匹配 |
| `pegainfer-frontend/src/vllm/mod.rs` | 辅助：装配处对 `LaunchedEngine` 两变体分派两代不同的桥 |
| `pegainfer-frontend/examples/echo-server.rs` | 辅助：一个**不含 GPU** 的完整 step 引擎样本，`launch` 返回 `Stepped` |
| 其余 11 个子模块（step/ledger/request_lifecycle/metrics/driver/control/request/event/sink/kv/handle/stop） | 本讲只读各自的文件头文档注释，正文留给后续讲义 |

## 4. 核心概念与源码讲解

### 4.1 `engine/mod.rs`：按内聚性切分的模块地图

#### 4.1.1 概念说明

`engine` 模块曾经是一个大文件，如今被**按内聚性**（split by cohesion）拆成 13 个子模块。`mod.rs` 不含任何逻辑代码，只做两件事：

1. 用模块文档注释（`//!`）写出整张地图——两代契约各含哪些模块、每个模块一句话职责；
2. 用 `mod` + `pub use ...::*` 把所有子模块的公开项**扁平地**搬进 `pegainfer_frontend::engine` 命名空间。

为什么要拆？因为两代契约并存：新一代的 step 驱动类型与旧一代的每 token 类型必须分家存放，否则「哪些类型属于哪一代、哪些可以随迁移删除」会重新变得模糊。`mod.rs` 的文档就是这份归属声明。

#### 4.1.2 核心流程

先看地图的全貌（依据 `mod.rs` 的文档注释与各文件的真实 import 关系整理）：

```
pegainfer_frontend::engine（mod.rs，扁平 re-export）
│
├─ step 驱动契约（新一代；qwen3 已迁移，其余线跟进）
│    ├─ step              线类型：Request / RequestId / StepOutputs / RequestUpdate
│    ├─ ledger            RequestLedger：活请求账本，terminal 恰一次的执法者
│    ├─ request_lifecycle 账本之外的请求载体：RequestEnvelope / RequestControl / DeferredFinish
│    ├─ metrics           SchedulerMetrics：调度器每迭代的自画像快照
│    ├─ wiring            scheduler_pair 双端接线 + Engine/EngineInfo 交接包 + LaunchedEngine
│    ├─ driver            Scheduler trait + drive 轮询循环（每个模型线只写一次的公共循环）
│    └─ control           LoRA 适配器能力（LoraClient），刻意置身调度器契约之外
│
├─ legacy 每 token 契约（保留至其余模型线迁移完毕）
│    ├─ request / event   GenerateRequest 与 TokenEvent 事件流
│    ├─ sink              每请求 TokenSink，挂在共享打标通道上
│    ├─ kv                KV 前缀解析（KvPrefix）
│    └─ handle            EngineHandle：路由、负载上报、关停
│
└─ stop               未列入两代分组的共享词汇：StopPolicy / StopCause / EosPolicy
```

三处值得注意的**跨组依赖**（读各文件头部的 `use super::x` 即可验证）：

- `step.rs` 与 `ledger.rs` 都从 `event`（legacy 组）import 词汇类型 `FinishReason` / `TokenLogprob`；
- `wiring.rs` 从 `kv`（legacy 组）import `KvCapacity`；
- `stop.rs` 干脆不在文档的两代分组里——它是被 `step` 消费的共享词汇，不属于任何一代的协议主链。

所以这张图的分组标准是「**契约主链的归属**」，不是「被谁 import」：词汇类型（`FinishReason`、`KvCapacity`、`StopPolicy`）可以跨组复用，将来随迁移被删除的是事件流、共享通道与 `EngineHandle` 路由这些**协议本体**。

生产者/消费者视角的依赖方向（箭头 = import）：

```
driver ──→ wiring ──→ ledger ──→ request_lifecycle
   │          │          │
   │          ├──→ step ─┴─→ event（词汇）/ stop（词汇）
   │          ├──→ metrics
   │          ├──→ control（旁路：LoRA 能力）
   │          └──→ kv（仅 KvCapacity 词汇）
   └──→ metrics、step（QueuedRequest）

生产者（模型 crate）：实现 driver::Scheduler，写 ledger，经 wiring 的后端被驱动
消费者（协议栈）：     持 wiring::SchedulerHandle，submit 请求、take_steps 读步流、读 metrics
```

#### 4.1.3 源码精读

先看目录页的文档注释——注意第 3 行的自我标注「current; qwen3 is migrated, other lines follow」写于只有 qwen3 完成迁移之时，如今已有四条线返回 `Stepped`（见 4.2.3 的普查表），这是「文档注释会滞后、代码普查才是真相」的一个现成例子：

[pegainfer-frontend/src/engine/mod.rs:L1-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/mod.rs#L1-L29)
——模块文档：前半段（L3-L19）列出 step 驱动契约的 7 个模块及各自一句话职责；后半段（L21-L26）列出 legacy 契约的 4 个模块；L28-L29 是扁平 re-export 约定的原文：「Everything is re-exported flat here, so `pegainfer_frontend::engine::X` paths are unchanged by the split」。

[pegainfer-frontend/src/engine/mod.rs:L31-L43](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/mod.rs#L31-L43)
——13 个私有 `mod` 声明（control / driver / event / handle / kv / ledger / metrics / request / request_lifecycle / sink / step / stop / wiring）。注意它们都是**私有**模块：外部无法写 `pegainfer_frontend::engine::wiring::Engine` 这样的路径。

[pegainfer-frontend/src/engine/mod.rs:L45-L57](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/mod.rs#L45-L57)
——13 条 `pub use 子模块::*`，把每个子模块的全部公开项搬进父命名空间。效果：`pegainfer_frontend::engine::LaunchedEngine` 这条路径**永远成立**，无论该类型实际定义在 wiring.rs 还是未来再被挪动；消费者代码不随内部分拆而改动。证据就在消费者侧——[pegainfer-server/src/main.rs:L12](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L12) 直接 `use pegainfer_frontend::engine::LaunchedEngine;`，完全不感知 wiring.rs 的存在。

再抽查两个子模块的文件头，验证地图上「跨组复用词汇」的说法：

[pegainfer-frontend/src/engine/step.rs:L1-L18](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/step.rs#L1-L18)
——step 契约的文档开头即声明「一步一条 `StepOutputs`、每请求至多一条扁平 `RequestUpdate`」，并在 L15-L18 `use super::event::{FinishReason, TokenLogprob}` 与 `use super::stop::{StopCause, StopPolicy}`——新契约复用 legacy 组与共享组的词汇类型。

[pegainfer-frontend/src/engine/driver.rs:L1-L16](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L1-L16)
——driver 的文档说明「循环只写一次、对所有模型线生效，约定（排空顺序、每迭代一次 metrics 发布一次 commit、关停与致命处理）是代码而非各 crate 的纪律」；其 import（`ledger` / `metrics` / `step` / `wiring`）印证 4.1.2 依赖图中 driver 的位置。

#### 4.1.4 代码实践：亲手画一张模块依赖图

1. **实践目标**：不看任何二手描述，仅凭 `mod.rs` 的文档注释与各子模块文件头的 `use super::...` 语句，独立画出 engine 模块的依赖图，并给每个模块标注生产者/消费者角色。
2. **操作步骤**：
   1. 打开 `pegainfer-frontend/src/engine/mod.rs`，把两代分组抄成两列清单；
   2. 对 13 个子模块文件，逐个只看文件前 20 行，记录每条 `use super::x::Y`；
   3. 以模块为节点、import 为箭头画图（纸笔或任画图工具均可）；
   4. 给每个节点标注：生产者侧（模型 crate 实现/调用）、消费者侧（协议栈调用）、或双侧词汇。
3. **需要观察的现象**：图中应出现至少三条「跨组边」——`step → event`、`ledger → event`、`wiring → kv`；`stop` 节点只被 step 一侧引用；`driver` 只被 `wiring::spawn_scheduler` 与外部模型 crate 触达，不被 legacy 组引用。
4. **预期结果**：得到一张与 4.1.2 相同骨架的图。若你画出的 `handle.rs` 有指向 `step.rs` 的箭头，说明看漏了——handle 的 import 全部落在 legacy 组与 `crate::parallel`（见 [handle.rs:L9-L15](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L9-L15)）。

#### 4.1.5 小练习与答案

**练习 1**：`mod.rs` 的文档注释把 `stop` 模块排除在两代分组之外，为什么？

**答案**：分组标准是「契约主链的归属」。`stop` 里的 `StopPolicy` / `StopCause` / `EosPolicy` 是描述停止行为的**词汇类型**，被 step 契约的 `Request` 消费，但它本身既不是 step 协议的消息结构，也不属于 legacy 事件流——放进任何一组都会误导读者以为它会随那一代被删除。

**练习 2**：不打开 `wiring.rs`，解释为什么 `pegainfer_frontend::engine::EngineInfo` 是一条合法路径。

**答案**：`mod.rs` L57 的 `pub use wiring::*;` 把 wiring 模块的全部公开项（含 `EngineInfo`）搬进 `engine` 命名空间，而 `wiring` 本身是私有 `mod`。扁平 re-export 让外部路径只锚定在 `engine` 这一层。

**练习 3**：判断题——`step.rs` import 了 legacy 组 `event.rs` 的 `FinishReason`，所以新契约依赖旧契约、迁移无法完成。

**答案**：错。依赖的只是**词汇枚举**，不是协议本体。legacy 契约要删除的是 `TokenEvent` 事件流、共享打标通道（`sink`）与 `EngineHandle` 路由这些传输机制；`FinishReason`（Length/Stop/Error 三个词）与 `TokenLogprob` 是两代共用的数据形状，保留它们不妨碍删除旧协议。

### 4.2 `LaunchedEngine`：迁移期的二选一

#### 4.2.1 概念说明

`ModelLine::launch`（见 [u2-l1](u2-l1-model-line-trait.md)）负责把权重加载上 GPU、启动调度器线程，然后交回一个「引擎」。迁移期的麻烦在于：不同模型线交回的引擎分属两代契约——legacy 线交回 `EngineHandle`（每 token 事件流），step 线交回 `Engine`（步流交接包）。`LaunchedEngine` 就是把这两种返回值装进**同一个枚举**，让 `launch` 的签名在迁移期保持稳定：

```rust
pub enum LaunchedEngine {
    Handle(super::handle::EngineHandle),  // legacy 每 token 引擎
    Stepped(Engine),                       // step 驱动引擎
}
```

它的文档注释写明了命运：**每条模型线都迁移完成后，这个枚举将被删除，只留 `Engine`**。也就是说，`LaunchedEngine` 不是长期设计，而是一块带说明书的脚手架。

#### 4.2.2 核心流程

一枚 `LaunchedEngine` 的生命周期：

```
模型线 launch()                        server / 协议栈
     │
     ├─ legacy 线：crate::launch(...).map(LaunchedEngine::Handle)
     │   （deepseek-v2-lite / glm52 / kimi-k2）
     ├─ step 线：Engine{..}.map(LaunchedEngine::Stepped) 或 .into()
     │   （qwen3 / qwen35 / gemma4 / k3 / echo 示例）
     ├─ qwen4：launch 直接 bail（推理未实现），两个变体都不构造
     ▼
spawn_blocking 交回 main.rs
     ▼
┌─ LoRA 分支：let LaunchedEngine::Stepped(engine) = launched else { bail }   ← 只认 Stepped
└─ 其余分支：把 future 原样交给 serve_with_engine_count / serve_prefill_only_with_engine_count
        ▼
     vllm/mod.rs 装配处 match launched {
         Handle(handle)  → 每分区一座 LocalEngineBridge（legacy 桥）
         Stepped(engine) → 每调度器一座 SteppedEngineBridge（step 桥）
     }
```

#### 4.2.3 源码精读

[pegainfer-frontend/src/engine/wiring.rs:L162-L180](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L162-L180)
——`LaunchedEngine` 的定义与两个 `From` 实现。文档原文：「What `ModelLine::launch` returns during the contract migration... Deleted (in favor of [`Engine`] alone) once every model line is migrated.」两个 `From`（`From<EngineHandle>` 与 `From<Engine>`）让模型线可以用 `.into()` 完成装箱——这样将来删除 `Handle` 变体时，step 线的调用点一个字符都不用改。

[pegainfer-server/src/main.rs:L204-L213](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L204-L213)
——引擎加载放在 `spawn_blocking` 里，闭包返回 `anyhow::Result<LaunchedEngine>`：这是 server 二进制与两代契约唯一的「持有」点——它只搬运这个枚举，从不打开看里面是哪一代（LoRA 分支除外）。

[pegainfer-server/src/main.rs:L215-L223](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L215-L223)
——LoRA 分支的 let-else 模式匹配：`let LaunchedEngine::Stepped(engine) = launched else { anyhow::bail!("LoRA serving requires a step-driven engine"); };`。**LoRA 路由在构建期就需要引擎的控制平面**（`Engine::lora` 里的 `LoraClient`，见 4.3.3），而 legacy `EngineHandle` 根本没有这个字段——所以该分支不仅要求 `Stepped`，还必须串行等引擎加载完再建路由（对照 u1-l3 讲过的「LoRA 路径例外地串行」）。

[pegainfer-frontend/src/vllm/mod.rs:L309-L362](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L309-L362)
——装配处的分派点（本讲只看结构，桥的内部留给 u4-l3）：`Handle` 分支先向 `handle.scheduler_partition_count()` 对账（与前端声明的 `engine_count` 不符即取消服务并报错），然后每分区克隆句柄建一座 `LocalEngineBridge`，指标经 `handle.metrics_watch_for(engine_index)` 的 watch 通道取；`Stepped` 分支对账 `engine.schedulers.len()`，逐调度器把 `scheduler.handle` 交给 `SteppedEngineBridge`、把 `scheduler.join` 收进 `scheduler_joins` 留待关停时收割，容量信息来自 `engine.info`。

**全工作区变体普查**（构造点与模式匹配点，用 `rg -n "LaunchedEngine::"` 可复现）：

| 位置 | 变体 | 说明 |
|---|---|---|
| `pegainfer-qwen3/src/model_line.rs:366` | `Stepped` | qwen3 已迁移（mod.rs 文档点名的第一条） |
| `pegainfer-qwen35/src/model_line.rs:182` | `Stepped` | qwen35 已迁移 |
| `pegainfer-gemma4/src/model_line.rs:45` | `Stepped` | gemma4 已迁移 |
| `pegainfer-k3/src/model_line.rs:320` | `Stepped` | k3 已迁移（bring-up 中） |
| `pegainfer-deepseek-v2-lite/src/model_line.rs:32` | `Handle` | 仍走 legacy 契约 |
| `pegainfer-glm52/src/model_line.rs:281` | `Handle` | 仍走 legacy 契约 |
| `pegainfer-kimi-k2/src/model_line.rs:88` | `Handle` | 仍走 legacy 契约 |
| `pegainfer-qwen4/src/model_line.rs:29-L31` | 两者皆无 | `launch` 直接 `bail!("...inference is not implemented yet")`——检测得到、服务不了的诚实占位 |
| `pegainfer-frontend/examples/echo-server.rs:82-L90` | `Stepped`（经 `.into()`） | 无 GPU 的参考实现 |
| `pegainfer-frontend/src/vllm/mod.rs:208` | `Stepped` | 单引擎便捷路径的现成装箱 |
| `pegainfer-qwen35/tests/serving_tp2.rs:96` | `Stepped` | 集成测试里手工铸造 |
| `pegainfer-frontend/src/engine/wiring.rs:L170` / `L176` | `From` 实现 | 两个变体各自的铸造口 |
| `pegainfer-server/src/main.rs:L221` | 模式匹配 | LoRA 分支 let-else 只认 `Stepped` |
| `pegainfer-frontend/src/vllm/mod.rs:L310` / `L336` | 模式匹配 | 装配处分派两代桥 |

#### 4.2.4 代码实践：grep 出迁移状态

1. **实践目标**：亲手统计两变体在整个工作区的出现点，用一行数据回答「迁移走到哪了」。
2. **操作步骤**：
   1. 在仓库根目录运行：`rg -n "LaunchedEngine::" --type rust`（若无 ripgrep，用 `grep -rn "LaunchedEngine::" --include="*.rs" .`）；
   2. 把输出按「构造 `Handle` / 构造 `Stepped` / 模式匹配 / `From` 实现」四类分组；
   3. 数一数 8 条注册在 `model_lines()` 里的线（对照 [pegainfer-server/src/main.rs:L24-L43](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/src/main.rs#L24-L43)）各属哪类。
3. **需要观察的现象**：每条模型线恰好在其 `src/model_line.rs` 的 `launch` 里出现一次构造；`qwen4` 是唯一在 `launch` 里不构造任何变体的线；模式匹配只出现在 `main.rs` 与 `vllm/mod.rs` 两处。
4. **预期结果**：与 4.2.3 的普查表一致（本讲的表就是用同样的检索得到的）。据此可写出一句迁移状态总结，见下方综合实践的第 3 步。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LoRA 分支用 let-else 拒绝 `Handle`，而不是给 legacy 引擎也实现一个「不支持 LoRA」的运行时错误？

**答案**：`Engine::lora` 是 `Option<LoraClient>`，**Option 本身就是能力声明**（见 4.3.3）——step 引擎没有 LoRA 能力时是 `None`，无需报错。而 legacy `EngineHandle` 连这个字段都没有，无法在类型层面表达「可能有 LoRA」。let-else 把「legacy 引擎不能服务 LoRA 路由」变成启动期的一次性失败，早于任何请求到达，好过运行时每请求报错。

**练习 2**：echo-server 的 `launch` 写的是 `Ok(Engine { ... }.into())` 而不是 `Ok(LaunchedEngine::Stepped(...))`。这有什么好处？

**答案**：`.into()` 走的是 `From<Engine> for LaunchedEngine`。当迁移完成、`Handle` 变体与枚举本身被删除、`launch` 直接返回 `Engine` 时，echo-server 这行代码（以及所有 step 线的 `.map(LaunchedEngine::Stepped)` 之外的 `.into()` 调用点）可以少改或不改——装箱细节被收敛在 wiring.rs 的两个 `From` 里。

**练习 3**：`qwen4` 线的 `launch` 两个变体都不构造，这违反契约吗？

**答案**：不违反。`launch` 的返回类型是 `anyhow::Result<LaunchedEngine>`，`Err` 也是合法结果。[pegainfer-qwen4/src/model_line.rs:L29-L31](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen4/src/model_line.rs#L29-L31) 直接 `bail!("this qwen4_exp checkpoint is detected, but inference is not implemented yet")`——`probe` 能认领配置（几何可推导），但推理尚未实现，于是诚实地在加载期失败，而不是交回一个半成品引擎。

### 4.3 `Engine` / `EngineInfo` / `LiveScheduler`：step 引擎的交接包

#### 4.3.1 概念说明

`Stepped` 变体里装的是 `Engine`——模型线交回前端的**交接包**（handoff package）。它把「前端要开始服务所必需的全部事实」打进一个结构体：几台调度器（`schedulers`）、引擎自我描述（`info`）、有没有 LoRA 控制平面（`lora`）。wiring.rs 的文档强调它是「onboarding checklist（新引擎接入清单）」：**必填字段要么填值、要么显式填 `None`，不能「忘了填」**——因为字段不是 `Option` 套 `Option`，而是类型逼你做选择。

`LiveScheduler` 是「一台活着的调度器」：前端侧的 `SchedulerHandle` 加驱动线程的 `JoinHandle`。`EngineInfo` 则是两个 `Option` 字段的小结构，每个 `None` 都有明确的前端语义（跳过相应检查），不是「没填」。

#### 4.3.2 核心流程

```
模型线 launch（以 echo-server 为例）
  │  spawn_scheduler("echo", EchoScheduler::default())   ← 铸出 (SchedulerHandle, SchedulerBackend)，
  │                                                       驱动线程即刻开跑 drive 循环
  ▼
Engine {
    schedulers: vec![LiveScheduler { handle, join }],   ← 数量与含义由模型线决定（如 DP 副本数）
    info: EngineInfo { kv_capacity, servable_len },      ← 显式自我描述
    lora: Option<LoraClient>,                            ← Option 即能力
}
  ▼ .into() → LaunchedEngine::Stepped
  ▼
vllm 装配：解包 schedulers，逐台建 SteppedEngineBridge（拿 handle），
           收集 join 待关停时收割，info 的两个字段收紧 max_model_len / batch-fit 检查
```

#### 4.3.3 源码精读

[pegainfer-frontend/src/engine/wiring.rs:L141-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L141-L153)
——`Engine` 结构体。文档原文：「a handoff package the protocol stack dissolves at wiring time（协议栈在接线时溶解的交接包）」——装配完成后这个结构就不再整体存在，字段各奔前程：`schedulers` 被逐台拆给桥，`info` 被消费，`lora` 被路由层持有。`lora` 字段的注释点明设计：「The `Option` is the capability — no separate flag exists to disagree with it」——没有第二个布尔开关能与它矛盾。

[pegainfer-frontend/src/engine/wiring.rs:L155-L160](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L155-L160)
——`LiveScheduler`：`handle`（前端侧提交/读流/读指标的把手）加 `join`（驱动线程）。注释规定收割顺序：server 在关停时、**先等桥（连同 scheduler handles 及其 submit 发送端）被丢弃之后**才 join 驱动线程——否则线程还会消费早已无人投递的通道。对应装配处的收集动作在 [pegainfer-frontend/src/vllm/mod.rs:L348-L349](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L348-L349)。

[pegainfer-frontend/src/engine/wiring.rs:L182-L190](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L182-L190)
——`EngineInfo`：`kv_capacity: Option<KvCapacity>`（KV 池容量；`None` = 不报告，前端跳过 batch-fit 检查）与 `servable_len: Option<u32>`（最长可服务请求数；`None` = 协议栈的最大长度校验回退到模型上下文窗口）。两个字段的 `None` 都是**显式语义**而非缺省值。

[pegainfer-frontend/examples/echo-server.rs:L78-L91](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L78-L91)
——最小合法 `Engine` 的活样本：一台调度器、`kv_capacity: None`、`servable_len: None`、`lora: None`，`.into()` 装箱。注释自述「echo 引擎是一台不报告 KV 池、也不设自身可服务长度上限的调度器」——三个 `None` 各自行使 4.3.3 上一条所说的显式语义。这个例子无需 GPU 即可运行（u4-l2 会真正跑它）。

顺带一提 `Engine.schedulers` 的数量语义：wiring.rs 的模块文档（[L14-L17](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L14-L17)）写明「跑几台调度器、每台意味着什么（DP 副本还是别的），是模型线的决定；契约只搬运这个集合，不附加 rank 语义；跨调度器的摆放是前端策略」。这也解释了 u2-l5 里 `ServePlan::scheduler_partition_count` 必须在加载前可知：装配处要拿它与 `engine.schedulers.len()` 对账（[vllm/mod.rs:L337-L344](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L337-L344)），不符宁可取消服务。

#### 4.3.4 代码实践：读一个交接包的溶解

1. **实践目标**：跟踪 `Engine` 从铸造到「溶解」的完整路径，验证「handoff package」这个说法。
2. **操作步骤**：
   1. 读 echo-server.rs L78-L91，列出一个 `Engine` 的全部字段取值；
   2. 读 vllm/mod.rs L336-L362，逐行标注 `engine.schedulers` / `engine.info` / `scheduler.handle` / `scheduler.join` / `info.kv_capacity` / `info.servable_len` 各自流向谁；
   3. 用 `rg -n "\.lora" pegainfer-frontend/src/vllm/` 找出 `Engine::lora` 在装配之后被谁持有。
3. **需要观察的现象**：`match launched` 之后不再有任何代码持有整个 `Engine` 值——`into_iter()` 把 `schedulers` 拆散，`info` 被按字段读取，结构体本身在作用域结束即消失。
4. **预期结果**：得到一张「字段 → 最终持有者」的小表（如 `handle → SteppedEngineBridge`、`join → scheduler_joins → 关停收割`、`lora → LoRA 路由层`）。这是纯源码阅读实践，结论确定，无需「待本地验证」标注。

#### 4.3.5 小练习与答案

**练习 1**：`EngineInfo` 的两个字段为什么是 `Option`，而不是给个 0 或 `u32::MAX` 当哨兵值？

**答案**：`None` 承载的是「**不报告 / 不设限**」这一显式决定，且每个 `None` 各有 documented 的前端行为（跳过 batch-fit 检查、回退模型上下文窗口）。哨兵值则语义含糊：`kv_capacity = 0` 是「没有 KV 池」还是「没填」？类型系统逼着接入者二选一，这正是文档所说「an engine...cannot just forget」的含义。

**练习 2**：`scheduler.join` 为什么不放在 `SchedulerHandle` 里、而要单独作为 `LiveScheduler` 的字段？

**答案**：`SchedulerHandle` 是**前端日常使用**的把手（submit / take_steps / metrics），它甚至可能被克隆或移交给桥；而 `join` 只在**关停收割**时用一次，且必须等所有 handle 丢弃之后才能调用。把生命周期截然不同的两样东西分开，让「先丢 handle、后 join」的顺序约束在数据结构上可见。

**练习 3**：一个模型线想跑 4 个 DP 副本，`Engine` 该怎么填？前端怎么知道每台调度器对应哪个 DP rank？

**答案**：`schedulers` 放 4 个 `LiveScheduler` 即可——数量与含义是模型线的决定，契约不附加 rank 语义（wiring.rs 模块文档 L14-L17）。前端的对应关系靠**位置**：装配处按 `engine_index`（`enumerate()` 的下标）逐台建桥（vllm/mod.rs L348-L360），并与加载前声明的 `engine_count` 对账；至于「哪个 engine_index 映射到哪个逻辑 DP rank」，是前端策略，不是引擎契约的事。

## 5. 综合实践

把本讲三块内容合成一页《engine 契约迁移状态笔记》：

1. **画图**：完成 4.1.4 的模块依赖图，两代分组用不同颜色/框型区分，跨组边（`step → event`、`ledger → event`、`wiring → kv`）用虚线标注，每个模块注明生产者/消费者角色。
2. **普查**：完成 4.2.4 的 grep 统计，把 8 条模型线 + echo 示例 + 两处模式匹配点填进 4.2.3 那样的表。
3. **下结论**：用一句话回答「当前哪些路径还依赖 legacy 契约」。参考答案（可对照你自己的表校订）：**除 qwen4（检测到但 launch 诚实报错、不构造任何变体）外，qwen3 / qwen35 / gemma4 / k3 四条线已交回 `Stepped`，deepseek-v2-lite / glm52 / kimi-k2 三条线仍交回 legacy `Handle`，因此 legacy 契约仍活在「这三条线的 launch 装箱 + vllm 装配处的 `Handle` 分支 + LocalEngineBridge + EngineHandle 自身」这条路径上；`LaunchedEngine` 枚举将在三条线迁完后删除。**
4. **自检追问**：你的结论里如果漏了 `metrics` 的双通道事实（legacy 引擎用 watch 通道发指标、step 引擎写共享单元，见 [pegainfer-frontend/src/engine/metrics.rs:L16-L19](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L16-L19) 的文档注释），补上它——这是 legacy 路径的又一块活组织，u4-l6 会专门讲。

## 6. 本讲小结

- `engine` 模块**按内聚性**拆成 13 个子模块，`mod.rs` 的文档注释就是两代分组的权威地图：step 驱动契约（step / ledger / request_lifecycle / metrics / wiring / driver / control）与 legacy 每 token 契约（request / event / sink / kv / handle），`stop` 是不入组的共享词汇。
- **扁平 re-export 约定**：`mod.rs` 用 13 条 `pub use ...::*` 把所有公开项搬进 `pegainfer_frontend::engine` 命名空间，外部路径（如 `main.rs` 的 `use ...::engine::LaunchedEngine`）不因内部分拆而改变。
- **`LaunchedEngine` 是迁移期脚手架**：`Handle(EngineHandle)` 与 `Stepped(Engine)` 二选一，两个 `From` 实现收敛装箱细节；文档明言全部模型线迁移完毕后枚举删除、只留 `Engine`。
- **普查结果**：qwen3 / qwen35 / gemma4 / k3 已是 `Stepped`，deepseek-v2-lite / glm52 / kimi-k2 仍是 `Handle`，qwen4 的 `launch` 直接报错不构造任何变体；`mod.rs` 文档里「qwen3 is migrated」的表述已滞后于代码，是文档漂移的现成例子。
- **`Engine` 是交接包**：`schedulers`（数量与含义归模型线）、`info`（两个 `Option` 字段的显式语义）、`lora`（`Option` 即能力）；装配处对账 `schedulers.len()` 与前端声明的 `engine_count`，不符宁可取消服务。
- **LoRA 分支要求 `Stepped` 且串行**：路由构建期就要 `Engine::lora` 的控制平面，legacy `EngineHandle` 在类型上就没有这个字段。

## 7. 下一步学习建议

下一讲 [u3-l2](u3-l2-legacy-wire.md) 深入 legacy 半场的线类型：`GenerateRequest` 的字段、`TokenEvent` 事件族与 `TokenSink` 的共享打标通道——理解旧协议的约定（`Scheduled` 先于 token、终态恰一次）才能体会新协议为什么把约定做成类型。建议按此顺序读源码：`engine/request.rs` → `engine/event.rs` → `engine/sink.rs`，并留意 `handle.rs` 测试里手工铸造请求的辅助函数。之后 u3-l3 转入 step 半场的 `Request` / `StepOutputs` / `RequestUpdate`，两相对照收益最大。
