# Qwen3Executor 与 ModelExecutor 契约

## 1. 本讲目标

u1-l4 里我们站在**引擎契约**一侧看完了 `launch → start_qwen3 → Engine` 的装配链；u2-l2 又看清了权重侧的 `Qwen3Model`（只装权重与配置，不含请求状态）。本讲进入两者之间的那一层：**executor.rs**——Qwen3 真正"跑一步前向"的地方。学完本讲，你应该能够：

1. 列出 prefill / decode / unified **三组 plan→result 的对应关系**：每组 plan 里有什么字段、对应 result 里有什么字段、`RequestId` 如何原样贯穿。
2. 解释 `ModelExecutor` trait 为什么是 **scheduler 与模型本地 benchmark 共用的相位边界**——以及把执行面收窄成一个 trait 换来了什么。
3. 说出 `RequestId` **端到端不变量**的意义：为什么整个 crate 里不存在第二个 id 空间。
4. 说出新增的 `max_draft_batch` 如何成为 **scheduler 可见的草稿器能力位**：EAGLE-3 报 `Some(1)`、DFlash 报 `None`（不限），调度门据此把宽批退回纯解码。

> **版本说明（对应 HEAD af9e40e1）**：EAGLE-3 草稿器落地（commit af9e40e1）把一批 DFlash 专属命名**推广为通用草稿层**。如果你读的是旧博客或旧文档，对照表如下：
>
> | 旧名（785d0487 及以前） | 现名（af9e40e1） |
> | --- | --- |
> | `DFlashMeta` | `DraftMeta` |
> | `dflash_ready_requests` | `draft_ready_requests` |
> | `PrefillResult::dflash_context_captured_requests` | `draft_context_captured_requests` |
> | `execute_dflash_verify` | `execute_draft_verify` |
> | `Qwen3Executor::new(.., dflash_draft_path: Option<&str>, ..)` | `new(.., draft: Option<DraftModel<'_>>, ..)` |
>
> 同时 trait 新增了 `max_draft_batch` 钩子。本讲全部按现名讲解。

## 2. 前置知识

- **相位边界（phase boundary）**：u1-l1 建立过的概念——调度决策与 GPU 执行之间只交换**纯数据**（plan 进、result 出），不含引擎句柄、不发生回调。本讲要看的正是这条边界的两侧长什么样。
- **trait 作为"窄腰"（narrow waist）**：`ModelExecutor` 是一个只有 executor.rs 内部可见（`pub(crate)`）的 trait。真实实现是 `Qwen3Executor`（持有 GPU）；调度器测试里还有一个 `FakeExecutor`（[src/scheduler/test_support.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs)）。调度器写成 `Qwen3Scheduler<E: ModelExecutor>` 的泛型，于是**同一份调度逻辑**既能对着假执行器做 CPU 单测，也能对着真 GPU 跑生产。
- **能力位（capability bit）**：执行器向调度器暴露的"我能做什么"查询，比如 `max_decode_batch_size()`、`available_blocks()`。新成员 `max_draft_batch()` 也是一类能力位：它不是让调度器去"控制"草稿器，而是让**草稿器把自己的服务上限告诉调度器**，由调度门决定退不退回纯解码。
- **borrow 的 plan**：三组 Plan 结构体都带生命周期 `<'a>`，字段是 `&'a [StepItem]`——plan 只是对调度器已有数据的一段**借用视图**，复制成本几乎为零；而 Result 是拥有的 `Vec<...>`，因为 token 与 logprob 是这一步刚算出来的新事实。
- **投机解码最小背景**（u6 会展开）：草稿器（DFlash / EAGLE-3）先便宜地"猜" \( K \) 个 token，目标模型一次前向验证整个 span（长度 \( K+1 \)，含锚点），接受最长正确前缀。EAGLE-3 v1 只服务**单请求贪心**，这个限制就是通过 `max_draft_batch = Some(1)` 浮出水面的。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | 本讲主战场：StepItem、三组 Plan/Result、`ModelExecutor` trait、`Qwen3Executor`、`DraftMeta`、worker 通道协议（`StepCommand`/`WorkerCommand`/`WorkerStepOutcome`） |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs) | `pub mod runtime` 再导出层（L200-L222）：把 executor 的公开面立为 crate 的低层执行接口 |
| [src/scheduler/plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs) | 消费侧：`speculation_decision_at` 读取 `max_draft_batch` 的调度门（L192-L219）及其单测（L416-L430） |
| [src/executor/dflash_prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs) | 纯函数 `dflash_prefill_action`：`draft_context_captured_requests` 的消费判定表（含 CPU 单测） |
| [src/executor/eagle3_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs) | EAGLE-3 worker 车道：`load_eagle3` 构造 `DraftMeta { max_draft_batch: Some(1) }`（L55-L75） |
| [src/speculative.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs) | `DraftModel` 枚举（L33-L38）：按 checkpoint 路径在 DFlash / EAGLE-3 间二选一的纯标签 |
| [src/eagle3.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs) | `EAGLE3_CHAIN_LENGTH = 3`（L20）、`EAGLE3_MAX_BATCH = 1`（L24）两个常量 |
| [src/frontend_adapter.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs) | 消费侧全景：`Qwen3Scheduler<E: ModelExecutor>`（L221）——调度器对执行器的全部认知都来自这个 trait |
| [src/bin/qwen3_decode_context.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/bin/qwen3_decode_context.rs) | benchmark 消费侧：绕过调度器直接用 `runtime::Qwen3Executor` + Plan 类型驱动前向（L10-L15） |

## 4. 核心概念与源码讲解

### 4.1 三组 Plan：scheduler 交给 executor 的"一步"

#### 4.1.1 概念说明

一次引擎 tick 里，调度器决定"这一步做些什么"：给哪些新请求 prefill、让哪些活跃请求各前进一个 token、还是两者混在同一步。这个决定被装进三个纯数据结构之一——`PrefillPlan` / `DecodePlan` / `UnifiedPlan`。它们回答的问题是：**这一步要算谁的什么**，仅此而已。plan 里没有 KV 句柄、没有 CUDA stream、没有引擎对象；所有 GPU 细节都被关在 executor 门后。

三组 plan 共享一个设计：字段是**借用的 StepItem 切片 + 一个 `sample_seed`**。StepItem（`PrefillStepItem` / `DecodeStepItem`）是"一个请求在本步的输入描述"，调度器持有它们，plan 只是借走。

#### 4.1.2 核心流程

一步的完整旅程（自上而下）：

1. 调度器 `build_next_plan` 产出 `ExecutionPlan`，翻译成三组 plan 之一（u4-l2 展开）。
2. `Qwen3Executor::execute_prefill/decode/unified` 接住 plan：调度 KV 块、构造 `KvView`，把 plan 内容**降级**成 worker 线程的 `StepCommand`。
3. `run_step` 经 crossbeam 通道发给每个 rank 的 worker；主 rank 回 `WorkerStepOutcome`，其余 rank 只回 `Ack`（TP 场景，u3-l2 展开）。
4. worker 在 `execute_step_on_lane` 里跑真前向，产出对应的 `PrefillResult` / `DecodeResult` / `UnifiedResult`。
5. executor 把 result 应用回 KV 状态（`apply_prefill_result` / `apply_decode`），再把 result 原样返回给调度器 resolve 成 `StepEffects`（u4-l3 展开）。

#### 4.1.3 源码精读

**三组 plan 的全部定义**——[src/executor.rs:L709-L723](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L709-L723)：三个结构体加起来不到 15 行。`UnifiedPlan` 同时携带 prefill 与 decode 两组切片——它就是"混步"：prefill 行与 decode 行在**同一次 GEMM** 里处理（u3-l6 展开）。`sample_seed` 每步由调度器随机生成，保证采样可复现又不同步退化。

**`PrefillStepItem` 的关键字段**——[src/executor.rs:L86-L120](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L86-L120)：除了 `request_id` / `prompt_tokens` / `params` 这些显然项，注意四个"由 executor 回填"的字段——`cached_tokens`（前缀缓存命中，executor 匹配后写入）、`external_hit_tokens`（其中来自 CPU/P2P 外部侧的部分）、`chunk_start` / `chunk_tokens`（分块 prefill 的本轮区间）。也就是说 StepItem 是个**双向信封**：调度器填身份与预算，executor 回填实际发生的切片与命中。

**`DecodeStepItem`**——[src/executor.rs:L174-L181](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L174-L181)：极简——`request_id` + 上一步选出的 `token_id` + 采样参数 + 可选 logprobs/LoRA。decode 每请求每步只前进一个 token，所以不需要区间描述。

**这条边界的公共出口**——[src/lib.rs:L200-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L200-L222)：`pub mod runtime` 的文档只有三句话——"Low-level Qwen3 execution interface. This is the production phase boundary used by the Qwen3 scheduler and by model-local benchmarks. The root server should use `start_engine` instead." 它再导出 `Qwen3Executor`、全部 StepItem/Plan/Result、`RequestId` 与 `ConnectorLookupCounters`。benchmark 二进制（如 [src/bin/qwen3_decode_context.rs:L10-L15](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/bin/qwen3_decode_context.rs#L10-L15)）正是从这里拿类型，直接构造 `PrefillPlan`/`DecodePlan` 驱动前向——**与生产调度器走完全相同的入口**，这就是"benchmark 与 scheduler 共用相位边界"的字面意思。

#### 4.1.4 代码实践

1. **实践目标**：把三组 plan→result 的字段对应关系整理成一张速查表（这也是本讲综合实践的一部分）。
2. **操作步骤**：打开 [src/executor.rs:L709-L786](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L709-L786)，逐字段抄录三组 plan 与三组 result，标注每个字段的生产方（调度器 or executor）与消费方（resolve 层 or executor 自身）。
3. **需要观察的现象**：`DecodePlan` 与 `DecodeResult` 的字段几乎一一对应（token 进 token 出）；`PrefillPlan` 与 `PrefillResult` **不对称**——plan 里的 `chunk_budget` 在 result 里没有对应物，result 里的 `cached_tokens`/`completed` 在 plan 里也没有。
4. **预期结果**：一张 3×N 表格。`UnifiedPlan` 与 `UnifiedResult` 严格等于 prefill/decode 两组的并集（可对照 [src/executor.rs:L783-L786](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L783-L786) 验证）。
5. 本实践为纯源码阅读，无需运行；表格正确性以引用行号为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么三组 Plan 用借用（`&'a [...]`）而三组 Result 用拥有（`Vec<...>`）？

**答案**：plan 描述的是调度器**已经持有**的数据（StepItem 由调度器构造与维护），借用即可、复制浪费；result 承载的是这一步**刚产生**的新事实（采样出的 token、logprob、命中计数），必须由 executor 拥有并移交所有权，调度器随后把它 resolve 成 effects。

**练习 2**：`UnifiedPlan` 为什么不叫"混合 plan"之外的某个优化特例——它和分别调用一次 `execute_prefill` + 一次 `execute_decode` 有什么本质区别？

**答案**：分别调用是**两次**前向、两批 GEMM；`execute_unified` 把 prefill 行和 decode 行拼进**同一次**前向、同一批 GEMM（省 launch 与访存开销，且数值路径可控，u3-l6 讲 Pin 模式如何保持与纯 decode 同核）。所以它不是"两次调用的语法糖"，而是不同的执行路径。

**练习 3**：benchmark（qwen3_decode_context）绕过调度器直接用 plan 类型，为什么这反而让测量更可信？

**答案**：因为它驱动的是**生产代码的同一入口**（trait 方法 → `StepCommand` → 同一前向），测到的算子序列与真实服务一致；如果 benchmark 另写一条前向，测的就不是被服务的那条路径。

### 4.2 ModelExecutor trait：scheduler 可见的全部能力

#### 4.2.1 概念说明

`ModelExecutor` 是调度器对执行器的**全部认知**。调度器不知道 CUDA、不知道 KV 池实现、不知道草稿器结构；它看到的是一个问句集合（"还有多少空闲块？""这个 token 是停止词吗？""草稿器一轮最多服务几个请求？"）加上一组命令（"执行这个 plan"）。把接口收窄成 trait 的直接收益有三个：

1. **可测试**：`Qwen3Scheduler<E: ModelExecutor>` 可以对 `FakeExecutor`（[src/scheduler/test_support.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs)）做无 GPU 单测——本讲 4.5 的调度门测试就是这样跑的。
2. **可观测**：计数器（spec 接受率、connector 查找）作为 trait getter 暴露，metrics 路径不需要知道 executor 内部字段。
3. **演进受控**：给执行器加能力（如这次的 `max_draft_batch`）必须先过 trait 这一关，调度器侧的语义变化一目了然。

#### 4.2.2 核心流程

trait 的方法可以分成四组，调度器每个 tick 的典型用法：

```text
每个 tick:
  能力查询:  available_blocks() / max_decode_batch_size() / ...
             speculation_enabled() → max_draft_batch() → speculative_request_ready(id)
  构造 plan: （调度器内部纯逻辑，u4-l2）
  执行:      execute_prefill / execute_decode / execute_unified
             （投机启用时: execute_speculative_draft → execute_speculative_verify）
  应用结果:  result → StepEffects → ledger（u4-l3/u4-l4）
```

#### 4.2.3 源码精读

**trait 全貌**——[src/executor.rs:L788-L937](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L788-L937)。分组读：

- **能力查询**（L789-L795）：`block_size` / `max_request_blocks` / `max_context_tokens` / `max_decode_batch_size` / `available_blocks` / `is_stop_token` / `drop_request`——调度与准入的地基。
- **三组执行方法**（L797-L799）：唯一的 required 执行面；投机两方法（L801-L810）带默认 `bail!` 实现——不装草稿器的执行器自动拒绝。
- **投机状态**（L812-L838）：`speculative_enabled` / `spec_decode_counters` / `speculative_request_ready`，以及本次新增的 `max_draft_batch`（[L834-L838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838)）：

  ```rust
  /// Most requests the loaded drafter proposes for in one round; `None` when
  /// it drafts any decode batch.
  fn max_draft_batch(&self) -> Option<usize> {
      None
  }
  ```

  注意默认值是 `None`——语义是"**不限**"而不是"零"。`None` 让门直接放行（DFlash 的行为），`Some(1)` 表示宽于 1 的批必须退回纯解码（EAGLE-3 的行为）。
- **DeferredFinish 旁路**（[L903-L923](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L903-L923)）：`withholds_finishes()` 默认 `false`；只有 P/D prefill 角色（`flush_on_finish`）覆写它——`Finished` 事件不在本步随流发出，而是被扣到 `release_finished_events`，等 offload 落盘 + MetaServer 注册对 peer 可见后再投递（客户端把 HTTP 响应当作"KV 已就绪"信号）。真实实现在 [L2485-L2495](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2485-L2495)：屏障跑在 offload 运行时上，**调度线程从不等待**。配套的自由函数 `send_finished_events`（[L939-L945](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L939-L945)）只是逐个 `finish.send()`。每个 `DeferredFinish` 携带请求的**整份终态记录**（含 tokens），所以迟到投递不会与 step 流乱序。

**RequestId 端到端不变量**——[src/executor.rs:L68-L71](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L68-L71)：executor 直接再导出契约的 `RequestId`，文档注释一锤定音——"the wiring mints it at submit and every internal queue, effect, and KV key uses it unchanged — there is no second id space to map across"。意义：任何一个 effect、日志、KV 键里的 id 都能直接对应到客户端请求，调试时**不需要跨层翻译**；也意味着 id 一旦铸造就永不重写，追踪（tracing）与计量天然对齐。

**泛型消费侧**——[src/frontend_adapter.rs:L221](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L221)：`pub(crate) struct Qwen3Scheduler<E: ModelExecutor>`。调度器的每个方法拿到的都是 `&mut E` / `&E`，看不到 `Qwen3Executor` 的任何私有字段。

#### 4.2.4 代码实践

1. **实践目标**：确认 executor.rs 的 `--lib` 单测面——哪些 trait 周边逻辑无 GPU 即可回归。
2. **操作步骤**：阅读 [src/executor.rs:L3289-L3361](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3289-L3361) 的 `mod tests`，然后运行：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib
   ```

3. **需要观察的现象**：`a_dead_peer_is_seen_while_rank_zero_is_blocked`、`a_dead_rank_zero_is_reported_as_rank_zero`（通道死亡如何被 `await_step_responses` 察觉）、`lora_capacity_*` 三连（容量校验的拒绝/放行分支）全部通过且不触碰 GPU。
4. **预期结果**：测试汇总里 executor 模块的 5 个用例通过；它们验证的是 trait 之下的**通道与纯校验逻辑**，不是前向本身。（无 GPU 环境的运行结果：待本地验证。）
5. 若无 Rust 工具链，改为对照源码列出每个 `#[test]` 断言验证的字段（错误串 `max_loras=1`、`requested=adapter-b`、`already loaded` 分别对应哪个分支）。

#### 4.2.5 小练习与答案

**练习 1**：`execute_speculative_draft` / `execute_speculative_verify` 为什么是带默认 `bail!` 的可选方法，而三组执行方法是 required？

**答案**：三组 plan 是任何执行器都必须支持的基本面（trait 的存在意义）；投机解码是**装载态能力**——没装草稿器的执行器"不支持"是正常状态而不是实现遗漏，默认实现把"未实现"编码进类型系统，调用方只在 `speculative_enabled()` 为真时才应到达这里。

**练习 2**：`max_draft_batch` 的默认值为什么选 `None`（不限）而不是 `Some(0)`？

**答案**：默认值服务于"未覆写 = 无约束"的语义：DFlash 能给任意 decode 批出草稿，覆写反而多余；`Some(0)` 会把所有批都判 NotReady，等于全局关闭投机——那是 `speculative_enabled()` 的职责，不该由批次上限兼职。选 `None` 让"能力位"与"开关"两个概念各守其位。

**练习 3**：`withholds_finishes` / `release_finished_events` 这对钩子为什么放在 executor（而不是调度器）上？

**答案**：扣留的**条件**（offload 落盘 + MetaServer 注册是否对 peer 可见）只有 executor 知道——屏障跑在 offload 运行时上；而扣留的**后果**（终态事件迟到）由调度器/前端吸收。把"何时放行"放进 executor、"如何投递"留在契约层，调度线程才可能完全不等待（free-running）。

### 4.3 三组 Result 与 RequestId 的端到端贯穿

#### 4.3.1 概念说明

Result 是一步的**产出收据**：每个请求一行（`*RequestResult`），行内 `request_id` 开头。调度器拿到 result 后既不解析 GPU 状态也不回查 executor——它要的每个事实（token、logprob、命中了多少前缀、prompt 是否算完）都在收据里。这层"收据完备"是 u4-l3 resolve 层能做成纯函数的前提。

三组 result 与三组 plan 严格对应，且都遵循"每请求一行、按 id 对齐"的形态。`PrefillResult` 额外多出一个**批级字段** `draft_context_captured_requests`——它是投机解码在相位边界上唯一的"跨行"信息。

#### 4.3.2 核心流程

`draft_context_captured_requests` 的生命周期（本讲最重要的数据流）：

```text
prefill 步 (worker 线程):
  capture_requested = should_capture_dflash_prefill_context(...)
                   || should_capture_eagle3_prefill_context(...)   # 按已装载草稿器
  execute_prefill(.., capture_layer_ids) → (logits, .., captured_hidden)
  record_prefill_{eagle3|dflash}_context(..) → Vec<RequestId>      # 生产
  → PrefillResult { requests, draft_context_captured_requests }

executor 侧回填 (execute_prefill 第 4 步):
  for 每行 req_result:
    captured  = draft_context_captured_requests.contains(id)
    dflash_prefill_action(captured, completed) →
      MarkReady    → draft_ready_requests.insert(id)   # 整段 prompt 完成 + 已捕获
      KeepPending  → 保持（还有后续 chunk）
      Drop         → 移出 + 通知 worker 丢弃陈旧草稿态
  （后续 SpeculativeDraft/Verify 步只对 ready 集里的请求发生；
   一旦走了普通 decode 步，序列在投机路径外前进，ready 态作废 → 移除并 DropDraft）
```

#### 4.3.3 源码精读

**三个 result 容器**——[src/executor.rs:L771-L786](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L771-L786)：`PrefillResult { requests, draft_context_captured_requests }` 的文档注释写明——该列表"为空，除非启用投机解码；executor 在 prompt 完全 prefill 后把这些 id 折进 `draft_ready_requests` 集合"。

**每请求一行的字段**——[src/executor.rs:L740-L762](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L740-L762)（prefill 行）与 [L764-L769](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L764-L769)（decode 行）。注意 `cached_tokens: Option<usize>` 的三态设计：`None` = 根本没查（缓存关闭或 prompt 评分请求），`Some(0)` = 查了但没命中——/metrics 计数必须区分这两者，否则会把"没查"误报成"命中率低"。

**decode 行的组装函数**——[src/executor.rs:L206-L231](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L206-L231)：`build_decode_request_results` 把 logits 的第 `row_offset + i` 行与第 `i` 个请求配对，gather 选中 token 的 logprob，逐行装 `DecodeRequestResult { request_id, token, logprob }`。unified 路径在 [L395-L412](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L395-L412) 复用它，`row_offset = prefill_requests.len()`——decode 行接在 prefill 行后面拼进同一份 logits，**行序即请求序**，这就是 RequestId 之外的另一种对齐方式（id 用于身份，行偏移用于布局）。

**生产方**——[src/executor.rs:L286-L329](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L286-L329)：worker 的 prefill 臂。L288-L289 用"或"把两种草稿器的捕获意愿合并；L302-L314 按已装载的草稿器分派 `record_prefill_eagle3_context` / `record_prefill_dflash_context`；L328 把结果装进 `PrefillResult`。（另一个"空生产方"在 [L3635-L3665](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3635-L3665)：异步 prefill 解析永远返回空表，注释说明"split-concurrent prefill 不与草稿器同跑"。）

**消费方**——[src/executor.rs:L2774-L2795](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2774-L2795)：`execute_prefill` 第 4 步按 `dflash_prefill_action(captured, completed)` 三分支把 id 折进 `draft_ready_requests`；判定函数本体在 [src/executor/dflash_prefill.rs:L42-L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L42-L62)——一张 2×2 真值表。**消费的终点**在两处：trait 的 `speculative_request_ready`（[L2888-L2890](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2888-L2890)）只是查这个集合；普通 decode 步会把它作废（[L2859-L2867](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2859-L2867)，序列在投机路径外前进，捕获的隐藏态已陈旧）。

#### 4.3.4 代码实践

1. **实践目标**：跑通 `draft_context_captured_requests` 消费链上唯一的纯函数单测，并注释每个断言。
2. **操作步骤**：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib prefill_action_table -- --nocapture
   ```

   对照 [src/executor/dflash_prefill.rs:L64-L87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L64-L87) 的 `prefill_action_table`：四个断言分别覆盖 `(captured, completed)` 真值表的四格。
3. **需要观察的现象**：`(true,true)→MarkReady`、`(true,false)→KeepPending`、`(false,*)→Drop`——与 4.3.2 流程图里的三分支一一对应。
4. **预期结果**：1 个测试通过。说明：`build_decode_request_results` 本身依赖 GPU（内部走 device_ctx 的 gather 内核），**没有** CPU 单测——它的行为由 GPU 集成测试（如 tests/logprobs_contract.rs）覆盖，运行需权重：待本地验证。本实践以最贴近该消费链的纯函数单测替代，这正是本 crate"纯逻辑下沉到可测函数"的分层方式。
5. 再用 `Grep` 搜 `draft_context_captured_requests` 全仓出现点，亲手核对 4.3.3 列出的生产/消费方清单（scheduler 测试夹具里的 `Vec::new()` 是第三类出现点——假执行器不需要真捕获）。

#### 4.3.5 小练习与答案

**练习 1**：`cached_tokens` 与 `external_hit_tokens` 为什么都用 `Option<usize>` 而不是 `usize`？

**答案**：三态语义必需——`None` 表示"这次根本没查询"（缓存关闭 / prompt 评分请求），`Some(0)` 表示"查了，零命中"。metrics 的命中率分母只该数真正查询过的 token；用 `usize` 会把"没查"压扁成"零命中"，系统性低估命中率。

**练习 2**：普通 decode 步为什么要主动把请求移出 `draft_ready_requests` 并发 `DropDraft`？

**答案**：投机捕获的目标隐藏态对应"序列在某个位置"的快照；普通 decode 让序列在投机路径**之外**前进了一个 token，快照立刻陈旧。若不移除，下一轮草稿会从过期上下文出发，草稿质量崩塌甚至语义错误。移除 + 丢弃 worker 态保证"ready ⟹ 上下文新鲜"这一不变量。

**练习 3**：`PrefillResult` 为什么把 `draft_context_captured_requests` 做成**批级**字段，而不是给每个 `PrefillRequestResult` 加一个 bool？

**答案**：它语义上就是"本步捕获了哪些请求"的集合视图，消费者（execute_prefill 第 4 步）以 `contains` 查询；批级 `Vec<RequestId>` 让"零捕获"成为廉价的空表，也让字段可以保持 `pub(crate)`（不泄漏到 result 行的公共面），投机解码对 resolve 层几乎不可见。

### 4.4 ConnectorLookupCounters：外部侧查找的"只结算一次"账本

#### 4.4.1 概念说明

KV offload / P2P 共享开启后，请求的 prompt 前缀可能从**外部侧**（主机内存或远端 peer）恢复进 GPU。运维上需要回答："我们到底向外问了多少 token、命中了多少？"——这就是 `ConnectorLookupCounters`：两个累计计数器（`queries` / `hits`，单位都是 token）。

难点在于**记账口径**：一次查找的"宽度"在查询发出那一刻就定死了（prompt 减去当时本地已有的 KV），而请求可能在此之后排队、增长、甚至中途离开（被取消/拒绝/失败）。如果只在"首次 prefill 成功"时记账，中途离开的请求就把它的查询**带走**了——查询数被低估、命中率被高估。解法是"咨询标记 + 恰好一次结算"。

#### 4.4.2 核心流程

```text
查询发出:  connector_consulted.insert(id, 查询宽度)     # 打标记，记宽度
结算(唯一入口 settle_connector_lookup):
  出口 A: 首次 prefill 匹配完成 → settle(id, external_hits)
  出口 B: 跳过匹配的已咨询请求（prompt 评分）→ settle(id, 0)
  出口 C: 请求提前离开（drop_request）      → settle(id, 0)
  settle: remove 标记（消费掉，之后任何调用都是 no-op）
          queries += 宽度; hits += external_hits
读取:      trait getter connector_lookup_counters() → /metrics（frontend_adapter 侧消费）
```

#### 4.4.3 源码精读

**计数器定义**——[src/executor.rs:L725-L738](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L725-L738)：文档注释直说设计意图——"a lookup is settled exactly once, whether it reaches its first prefill or leaves before it... so a lookup is never lost with the request that made it"。`queries` 的口径是"查询发出时被问到的 prompt token 数"，`hits` 是"已提交的装载真正恢复、且首次 prefill 匹配上的部分"。

**唯一结算入口**——[src/executor.rs:L1661-L1673](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1661-L1673)：`settle_connector_lookup` 先 `remove` 咨询标记——**消费式结算**，天然幂等；取不到标记直接返回 `false`。三个出口：首次 prefill 的匹配分支（[L1956-L1958](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1956-L1958)）、跳过匹配的兜底（[L1965-L1968](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1965-L1968)）、提前 drop（[L2528](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2528)）。L1950-L1955 附近还区分了"外部恢复的块"与"本地命中"：只有 probe 上**越过 GPU 命中之外**持有的块才算跨过了 connector。

**为什么宽度记在 executor 而不是准入时重推导**——[src/executor.rs:L974-L987](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L974-L987)：`connector_consulted` 字段的注释解释——请求只被咨询一次，但可能在 deferred 停车期间本地缓存继续增长，"准入时的匹配宽度"已经不能描述实际发出的查询；所以宽度在查询那一刻定格。

**trait 暴露与实现**——trait 默认零值（[L823-L827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L823-L827)），`Qwen3Executor` 覆写为返回自己的账本（[L2467-L2469](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2467-L2469)）。

#### 4.4.4 代码实践

1. **实践目标**：验证"恰好一次结算"的三出口覆盖，画出记账状态机。
2. **操作步骤**：源码阅读型实践。从 `settle_connector_lookup`（[L1661](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1661)）出发，`Grep` 全部调用点，给每个调用点标注：触发条件、传入的 `external_hits`、若不结算会发生什么。
3. **需要观察的现象**：三个调用点分别位于 prefill 匹配分支、prompt 评分兜底、`drop_request`；没有任何路径能在结算后二次记账（`remove` 消费标记）。
4. **预期结果**：一张两态状态机图（未咨询 → 已咨询(带宽度) → 已结算），以及一句结论：**请求的任何退出路径都经过结算**。行为级验证在 tests/kv_offload_cpu_hit.rs（需权重，待本地验证）；无 GPU 时以源码推演为准。
5. 对照 [L725-L738](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L725-L738) 的文档注释核对你对 `queries`/`hits` 口径的理解。

#### 4.4.5 小练习与答案

**练习 1**：如果只在"首次 prefill 成功"时把查询计入 `queries`，测得的命中率会怎么偏？

**答案**：被取消/拒绝/装载失败的请求带着它们的查询离开，分母（queries）变小；这些请求的命中通常是 0，于是分子基本不变——命中率被**系统性高估**，离场率越高失真越大。

**练习 2**：`hits` 为什么定义为"已提交的装载恢复 **且首次 prefill 匹配上** 的 token"，而不是"connector 声称找到了的 token"？

**答案**：计数的目的是回答"外部恢复真的省了重算吗"。connector 声称命中但装载未提交、或提交了却没进首次匹配的部分，并没有省掉任何前向计算——把这笔算进 hits 会把"承诺"当"收益"。

**练习 3**：`ConnectorLookupCounters` 被放在 `pub mod runtime` 再导出（[src/lib.rs:L208](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L208)），但结算逻辑全在 executor 私有方法里。这种"数据公共、机制私有"的切分有什么好处？

**答案**：metrics/前端只消费两个累计数（公共面小而稳定）；而"何时结算、三个出口、消费式幂等"这些容易改错的机制细节全部私有，重构记账内部不影响任何消费者。

### 4.5 max_draft_batch：草稿器能力位如何变成调度门

#### 4.5.1 概念说明

两种草稿器的能力不同：DFlash 的批草稿缓冲按最宽 decode 桶设计，能给**任意宽**的批出草稿；EAGLE-3 v1 的 scratch 是单 token 形状（u6-l5 展开），一轮只服务 1 个请求。这个差异如果散落在调度器里（`if eagle3 { 批宽 == 1 才行 }`），调度代码就得认识每种草稿器——方向反了。正确做法是**能力上报**：执行器装载草稿器时把上限写进 `DraftMeta`，trait 用 `max_draft_batch()` 透出，调度门统一消费。于是新增第三种草稿器时调度器**零改动**（或只需认识新的上限语义）。

#### 4.5.2 核心流程

```text
装载 (crate::launch 折叠出 DraftModel::DFlash|Eagle3):
  Qwen3Executor::load_draft_model(draft)
    ├─ 校验: 单卡、无 offload
    ├─ worker: load_dflash → DraftMeta { max_draft_batch: None }      # 不限
    │        或 load_eagle3 → DraftMeta { max_draft_batch: Some(EAGLE3_MAX_BATCH=1) }
    ├─ set_prefix_cache_enabled(false)   # 装草稿强制关前缀缓存（u1-l4 的旋钮顺序）
    └─ speculative = Some(meta)

每步调度门 (speculation_decision_at):
  未启用投机 / 空批                      → NotReady
  max_draft_batch = Some(L) 且 批宽 > L  → NotReady   ← 新增的批宽门
  任一请求: 非 draft-ready / 有 LoRA / 有 logprobs → NotReady
  批宽 ≥ spec_max_batch (负载门)          → Gated
  否则                                   → Speculate
```

#### 4.5.3 源码精读

**`DraftMeta`：执行器学到的草稿器事实**——[src/executor.rs:L3372-L3388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bbf18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3372-L3388)：四个字段——`block_size`、`num_spec_tokens`（每轮草稿数 \( K \)，span 为 \( K+1 \)）、`max_position_embeddings`、`max_draft_batch`（文档直接指向 trait 方法）。它由 **worker 线程**在装载时构造（草稿模型在 worker 上构建，因为它要读同线程的目标模型嵌入与 head），再经通道回传。

**两条装载臂的差异**——DFlash 臂 [src/executor.rs:L3593-L3616](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3593-L3616) 构造 `max_draft_batch: None`；EAGLE-3 臂 [src/executor/eagle3_lane.rs:L55-L75](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L55-L75) 构造 `Some(EAGLE3_MAX_BATCH)`，常量定义在 [src/eagle3.rs:L22-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L22-L24)——注释写明"v1 drafts through a single-token scratch, so the scheduler plain-decodes any larger batch"。

**入口分派**——[src/executor.rs:L1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827)：`load_draft_model(draft: DraftModel<'_>)` 先拒绝多卡与 offload，再按枚举分派到 `load_dflash` / `load_eagle3`，日志分别报告"DFlash speculative decoding enabled"与"EAGLE-3 ... greedy batch-1 only"。`DraftModel` 枚举本体在 [src/speculative.rs:L33-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L33-L38)——两个带路径的变体，纯标签。worker 通道侧对应两条命令 `WorkerCommand::LoadDflash` / `LoadEagle3` 与统一的 `DropDraft`（[L4262-L4278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4262-L4278)）。

**trait 透出**——[src/executor.rs:L2892-L2896](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2892-L2896)：`self.speculative.as_ref().and_then(|meta| meta.max_draft_batch)`——未装载草稿器时自然返回默认 `None`（配合外层 `speculative_enabled()` 先行短路，语义无歧义）。

**调度门消费**——[src/scheduler/plan.rs:L192-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L192-L219)：`speculation_decision_at` 在"全有-or-全无"ready 检查**之前**先过批宽门（[L200-L205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L200-L205)）：`max_draft_batch().is_some_and(|limit| active.len() > limit)` 即 `NotReady`。注意 `SpeculationDecision::NotReady` 的文档（[L181-L183](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L181-L183)）已把"批宽超过草稿器服务上限"列为 NotReady 的成因之一——退回纯解码，**不是错误**，输出仍然正确（无损性由验证步保证，u6-l1 展开）。

**单测**——[src/scheduler/plan.rs:L416-L430](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L416-L430)：`a_batch_wider_than_the_drafter_serves_decodes_plainly` 用 `FakeExecutor::new(..).with_speculative().with_max_draft_batch(1)` 断言批宽 1 → `Speculate`、批宽 2 → `NotReady`。假执行器的该旋钮在 [src/scheduler/test_support.rs:L194-L195](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L194-L195)。

#### 4.5.4 代码实践

1. **实践目标**：跑通批宽门的 CPU 单测，并推演 batch=2 时的判定路径。
2. **操作步骤**：

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib a_batch_wider_than_the_drafter_serves_decodes_plainly -- --nocapture
   ```

3. **需要观察的现象**：测试通过。随后在纸面推演 batch=2：`speculative_enabled()=true` → `max_draft_batch()=Some(1)` 且 `2 > 1` → 直接 `NotReady`，**根本不会**走到逐请求 ready 检查。
4. **预期结果**：1 个测试通过（无 GPU 依赖）。若你无工具链，对照 L416-L430 源码人工验证两个断言。（运行输出：待本地验证。）
5. 延伸观察：把 `with_max_draft_batch(1)` 去掉（模拟 DFlash 的 `None`），按 L200-L205 推演同样输入应得 `Speculate` 或 `Gated`——体会 `None` 的"不限"语义。

#### 4.5.5 小练习与答案

**练习 1**：批宽门为什么放在逐请求 ready 检查**之前**？换个顺序有什么区别？

**答案**：批宽门是 \( O(1) \) 的批级判断，ready 检查是 \( O(\text{批宽}) \) 的逐请求查询（还查 executor 内部集合）。先便宜后昂贵是直接收益；语义上更重要的区别是：批超宽时**无论**各请求是否 ready，结论都只能是纯解码，先查 ready 会做无用功甚至误导（将来若加观测计数，会把"因批宽退回"误记成"因未就绪退回"）。

**练习 2**：EAGLE-3 装载后，一个 3 请求的活跃批会发生什么？输出还正确吗？

**答案**：批宽 3 > 上限 1 → `NotReady` → 整批走普通 decode（每步每请求 1 token）。输出**仍然正确**：投机解码的接受规则保证每步提交的 token 都来自目标分布（u6-l1），退回纯解码只是回到非投机的基线路径，没有任何损失——损失的只是加速。

**练习 3**：`DraftMeta` 为什么由 worker 线程构造，而不是 executor 线程装载完路径就填好？

**答案**：草稿模型必须在 worker 线程上构建——它要读**同线程**的目标模型（嵌入、lm_head）与设备上下文（u3-l2 的 cuBLAS 线程绑定）；`block_size`、`effective_drafts` 这些事实只有在真正装载时才从 checkpoint 收敛出来。executor 线程只是经通道把 `DraftMeta` 收回来存进 `speculative`，作为后续能力查询的依据。

## 5. 综合实践

制作一页 **"executor 相位边界速查表"**（这是本讲实践的完整形态，建议存进自己的笔记仓库）：

1. **三组 plan→result 对照**：为 prefill / decode / unified 各画一列，行包含——plan 的每个字段（标注：调度器填 or executor 回填）、result 的每个字段（标注：resolve 层消费 or executor 自用）、`RequestId` 出现的位置。数据来源限 [src/executor.rs:L709-L786](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L709-L786) 与 [L86-L204](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L86-L204)。
2. **RequestId 贯穿线**：从 `frontend_adapter` 的 submit 铸造点出发，列出同一个 id 依次经过的每个容器（PendingRequest → StepItem → Result 行 → StepEffects → ledger），每处给行号。
3. **`draft_context_captured_requests` 生产/消费标注**：生产方（worker prefill 臂的 `record_prefill_*`，含异步 prefill 的空表分支）与全部消费方（`execute_prefill` 的三分支折叠、`speculative_request_ready` 查询、decode 步的作废），各配永久链接。
4. **验证**：跑 `cargo test --release -p pegainfer-qwen3 --lib`（应全绿，无 GPU），并在表尾注明哪些断言（`prefill_action_table`、`a_batch_wider_than_the_drafter_serves_decodes_plainly`）为速查表的哪几行提供了机器证词。
5. 完成后自测：能否不翻源码回答——"混步里 decode 行的 logprob 从 logits 的哪一行来？"（答案：`row_offset + i`，`row_offset` = prefill 行数。）

## 6. 本讲小结

- 三组 **plan→result**（prefill / decode / unified）是 scheduler 与 benchmark 共用的**生产相位边界**：plan 是借用的纯数据，result 是拥有的新事实，`pub mod runtime`（[src/lib.rs:L200-L222](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L200-L222)）把这条边界立为 crate 的低层公共接口。
- **`ModelExecutor` trait 是调度器对执行器的全部认知**：能力查询 + 三组执行方法 + 投机/LoRA/offload/完成事件钩子；`Qwen3Scheduler<E: ModelExecutor>` 的泛型化让同一份调度逻辑可对 `FakeExecutor` 做 CPU 单测。
- **`RequestId` 端到端不变量**：submit 处铸造，此后每个队列、effect、KV 键原样使用——没有第二 id 空间，跨层追踪零翻译。
- **`PrefillResult::draft_context_captured_requests`**（本次由 `dflash_*` 推广改名）是投机解码在边界上的唯一批级字段：worker 生产、`execute_prefill` 折进 `draft_ready_requests`、普通 decode 步作废。
- **`ConnectorLookupCounters`** 用"咨询标记 + 消费式恰好一次结算"保证外部侧查找计数不随请求离场而丢失；三态 `Option` 区分"没查"与"零命中"。
- **`max_draft_batch` 能力位**（本次新增）：`DraftMeta` 装载时定格（DFlash `None` 不限、EAGLE-3 `Some(1)`），trait 透出，`speculation_decision_at` 的批宽门消费——宽批退回纯解码，无损性不受影响。

## 7. 下一步学习建议

本讲只看了边界两侧的**类型**，还没看执行器**内部怎么跑一步**。下一讲 **u3-l2（GPU 线程模型：RankWorker 与 LocalQwen3Lane）** 拆 `StepCommand` 如何经 crossbeam 通道到达 worker、`CublasThreadGuard` 如何绑定 cuBLAS 句柄线程、以及 `WorkerLost` 如何向调度器传播——本讲 4.5 出现的 `LoadDflash`/`LoadEagle3`/`DropDraft` 命令将在那里给出完整的消息时序。之后再按顺序进入三条前向（u3-l3 prefill、u3-l4 批量 decode、u3-l6 统一前向）。若你想先补调度消费侧，可跳读 u4-l2（执行计划与投机门控）——本讲的 `speculation_decision_at` 会在那里展开成完整的 `build_next_plan`。
