# 草稿车道集成与投机调优旋钮

> 讲义 id：u6-l7 · 层级：advanced · 依赖：u6-l2（DFlash 草稿模型）、u6-l6（verify 前向缓冲与分段 CUDA Graph）

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 DFlash 草稿车道（lane）为什么必须与目标模型同住一条 worker 线程，以及「隐藏态不出车道」这条边界是如何在类型与借用结构上落实的。
2. 读懂 `dflash_prefill_action` 的三态决策表（MarkReady / KeepPending / Drop），并说出 `prefill_capture_supported` 在 af9e40e1 之后如何从 DFlash 专属谓词推广为两种草稿器（DFlash 与 EAGLE-3）共享的捕获资格判断。
3. 描述 `AutoHedge` 控制器的窗口化 explore-then-commit 设计：探索什么、按什么目标定价、如何提交、如何应对窗口污染与负载门打断。
4. 汇总全部投机旋钮（`PEGAINFER_SPEC_*` 环境变量与 `--dflash-draft-model-path` / `--eagle3-draft-model-path` CLI 标志）的作用面、默认值、读取时机与互斥规则，并理解 `spec_env` 为何对其中两个旋钮选择「坏值拒绝启动」而非静默回退。
5. 说明 `tests/common/lossless.rs` 抽出的共享无损性判定如何同时服务 DFlash 与 EAGLE-3 两个投机门测试。

## 2. 前置知识

- **车道（lane）与 worker 线程**（u3-l2）：`LocalQwen3Lane` 把一台 GPU 的全部可变状态打包，独占一条 worker 线程；executor 线程通过 `StepCommand`（算什么）与 `WorkerCommand`（控制面）两条通道与它通信。本讲的「草稿车道」就住在这个结构里。
- **投机解码事务**（u6-l1）：propose → verify → accept → commit/rollback。草稿/验证边界是**纯 token 跨界**——proposer 的隐藏态不越线。本讲讲的是「线内」的那一侧。
- **draft_ready_requests 就绪集**（u3-l1/u4-l2）：executor 侧维护的「哪些请求已捕获目标隐藏态、可以进入草稿轮」集合；调度器的 `speculation_decision_at` 全有-or-全无门控以它为输入。
- **hedge（对冲）与 DSpark**（u6-l4）：在 DFlash backbone 上叠加 Markov 头后，可以同时在多个「第二名」分支上 rollout 多条草稿链，验证阶段并行展开。链数 `C` 是个难拍脑袋定的性能参数——这正是 4.3 节自定价控制器的动机。
- **OnceLock 一次性读取**：所有 `PEGAINFER_SPEC_*` 旋钮每个进程只读一次环境变量，之后缓存为静态值——运行中改环境变量不会生效。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/executor/dflash_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs) | worker 侧 DFlash 车道：`DFlashLaneState`（草稿模型 + 每请求草稿态 + 批草稿 scratch + hedge 状态），以及 hedge 旋钮的 OnceLock 读取与 `batch_adaptive_chains` 公式 |
| [src/executor/dflash_prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs) | 捕获资格谓词（`prefill_capture_supported` / `dflash_prefill_can_capture`）与 prefill 后三态动作 `DFlashPrefillAction` |
| [src/executor/auto_hedge.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs) | `AutoHedge` 窗口化 explore-then-commit 自定价控制器（`PEGAINFER_SPEC_HEDGE_AUTO`） |
| [src/spec_env.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs) | `PEGAINFER_SPEC_MAX_BATCH` / `MAX_DRAFTS` 的解析、启动期校验与一次性读取 |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | 车道挂载点（`LocalQwen3Lane.dflash` / `.eagle3`）、`load_draft_model` 装载分派、`WorkerCommand::LoadDflash/LoadEagle3/DropDraft`、prefill 结果应用处的三态消费 |
| [src/speculative.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs) | 方法无关核心；`DraftModel` 枚举在此定义 |
| [src/model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs) / [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs) | CLI 互斥规则的两层设防；`spec_env::validate()` 的调用点 |
| [tests/common/lossless.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs) | 本次提交从 DFlash 门测试中抽出的共享无损性判定（regret 检查） |
| [tests/dflash_speculative_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/dflash_speculative_gate.rs) / [tests/eagle3_speculative_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs) | 两个共享 lossless 判定的投机门测试 |

## 4. 核心概念与源码讲解

### 4.1 DFlashLaneState：草稿车道为何与目标同线程

#### 4.1.1 概念说明

「草稿车道」是 worker 线程里紧挨着目标模型的一块私有状态：草稿模型本身、每个请求的草稿态、批草稿 scratch、hedge 链与自定价控制器。它必须与目标模型同线程，原因是**草稿 rollout 直接读目标的词嵌入、输出头和捕获到的隐藏态**——这些张量体积大且生命周期归目标 lane 所有，跨线程搬运既要拷贝又破坏「纯 token 跨界」的事务边界。模块文档把这个设计写得很直白：

> 隐藏态是车道私有的（`pending_context`），从不跨到调度器一侧。[src/executor/dflash_lane.rs:L1-L7](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L1-L7) 说明了这条边界。

af9e40e1 之后，`LocalQwen3Lane` 上有两个互斥的车道槽位——`dflash: Option<DFlashLaneState>` 与 `eagle3: Option<Eagle3LaneState>`，**至多一个被装载**（见 [src/executor.rs:L3410-L3414](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3410-L3414) 的字段注释）。方法无关的那一半（捕获、ready 集、KV 事务）由 `speculative.rs` 共享，只有「把捕获变成草稿」的 worker 车道因草稿器而异——[src/speculative.rs:L11-L16](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L11-L16) 的模块文档明确了这个划分。

#### 4.1.2 核心流程

一次投机轮在车道侧的时序：

```text
executor 线程                                worker 线程（primary lane）
─────────────                                ─────────────────────
StepCommand::SpeculativeDraft{requests} ───▶ 按 lane.eagle3.is_some() 分派：
                                              ├─ EAGLE-3 → execute_eagle3_draft
                                              └─ 否则    → execute_dflash_draft
                                                   1. take() 整个车道出 self（借用拆分）
                                                   2. 把批内所有请求的 state 从 HashMap 取出
                                                   3. 批量 backbone 前向 → scratch.logits
                                                   4. 提案路径三选一：
                                                      selector / markov(+hedge 阶梯) / 朴素 argmax
                                                   5. state 放回 HashMap，车道放回 self
                                                   6. 产出 [anchor, draft_1..draft_K]（纯 token）
◀── WorkerStepOutcome::SpeculativeDraft(result)
StepCommand::SpeculativeVerify{...}     ───▶ execute_draft_verify（u6-l6 的分段图路径）
                                              验证后捕获目标隐藏态 → record_verify_dflash_context
                                              （含 auto hedge 控制器的 tick / break_window）
```

装载侧：`WorkerCommand::LoadDflash` / `LoadEagle3` / `DropDraft` 三条控制面命令（[src/executor.rs:L4262-L4278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4262-L4278)）——装载必须在 worker 线程执行，因为草稿模型构建要读同线程的目标模型。

#### 4.1.3 源码精读

**车道结构体**——[src/executor/dflash_lane.rs:L29-L58](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L29-L58) 定义 `DFlashLaneState`：`model`（草稿模型）、`requests`（每请求态的私有序表）、`scratch`（**车道级**批草稿缓冲，为整个 decode 批一次性分配，让稠密草稿算子整批跑一次而非每请求一次——u6-l2 讲过它）、验证/接受计数器、`hedge_blocks`、`auto` 控制器、`round_chains`，以及两 个「生效几何」字段 `hedge_cap` / `hedge_branch_positions`。

**生效几何只算一次**——[src/executor/dflash_lane.rs:L60-L102](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L60-L102) 的 `new` 在构造期就把原始环境值解析成 `spec_hedge_effective`（按 block_size 过滤分支位置）并记日志。注释点名了动机：位置解析器会**静默丢弃不可解析项**，若不在启动时把生效几何打进日志，基准测试可能在比自己配置更窄的阶梯上测量。草稿路径、验证分派、控制器此后只读这两个字段，**永不重读原始环境值**（[src/executor/dflash_lane.rs:L50-L57](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L50-L57) 的字段注释）。

**草稿轮主循环**——[src/executor/dflash_lane.rs:L286-L515](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L286-L515) 的 `execute_dflash_draft`。两个结构技巧值得注意：

1. `self.dflash.take()` 把车道整体借出（[L294-L299](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L294-L299)）：草稿前向借用 `dflash.model`/`dflash.scratch`，而朴素 argmax 路径要借用 `self.sample_scratch`，不 take 就撞 `self` 的借用冲突。
2. 所有请求的 state 先 `remove` 出 HashMap 再集体放回（[L300-L324](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L300-L324)），让批量前向能同时持有全部 `&mut`。

链数决策的优先级在 [L375-L400](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L375-L400)：`PEGAINFER_SPEC_HEDGE_KNEE` 的批自适应策略 **优先于** 固定链数与 auto 控制器；auto 模式下取 `controller.current_c().min(位置数)`。产出侧把 anchor 前置、按 `effective_drafts()` 截断（[L485-L510](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L485-L510)）——这正是 `DraftMeta.num_spec_tokens` 所说的「span 恒为 K+1，anchor-first/anchor-drop 已折进草稿数」。

**跨草稿器分派**——[src/executor.rs:L534-L541](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L534-L541)：`StepCommand::SpeculativeDraft` 按 `lane.eagle3.is_some()` 分派到 EAGLE-3 或 DFlash 车道，二者产出同构的 `DraftResult`。

**装载分派与强制关前缀缓存**——[src/executor.rs:L1786-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1786-L1827) 的 `load_draft_model` 接收 `DraftModel` 枚举（[src/speculative.rs:L33-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L33-L38)），先拒绝多 rank 与 KV offload，再按变体走 `load_dflash` / `load_eagle3`，最后 `set_prefix_cache_enabled(false)`。结构性约束在 [src/executor.rs:L1702-L1709](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1702-L1709)：`prefix_cache_enabled()` 返回 `requested && speculative.is_none()`——即使之后有人重新打开请求位，已装载的草稿器也让它保持关闭（前缀命中会跳过产出干净隐藏态的那次前向）。

**能力位上报**——`DraftMeta`（[src/executor.rs:L3372-L3388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3372-L3388)）新增 `max_draft_batch` 字段；trait 实现 [src/executor.rs:L2892-L2896](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2892-L2896) 把它透出。DFlash 装载时填 `None`（不限），EAGLE-3 装载时填 `Some(EAGLE3_MAX_BATCH)` 即 `Some(1)`（[src/executor/eagle3_lane.rs:L62-L67](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L62-L67)）。调度门 [src/scheduler/plan.rs:L200-L204](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L200-L204) 据此把宽批退回纯解码（u4-l2 已讲判定路径，u6-l8 讲 EAGLE-3 车道全貌）。

#### 4.1.4 代码实践

1. **实践目标**：用纸面推演确认「车道所有权 + 纯 token 跨界」在代码里的落点。
2. **操作步骤**：
   - 通读 [src/executor/dflash_lane.rs:L286-L330](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L286-L330)，标出 `take()`、`remove()`、`insert` 三类所有权转移各自防的借用冲突。
   - 运行车道侧单测（无需 GPU）：
     ```bash
     cargo test --release -p pegainfer-qwen3 --lib hedge
     ```
3. **需要观察的现象**：`batch_adaptive_chains_picks_the_largest_bucket_aligned_count_under_the_knee` 与三个 `hedge_selection_*` 用例全部通过；它们只依赖 `BATCH_BUCKETS` 常量与纯函数，不需要 CUDA。
4. **预期结果**：4 个左右测试通过（待本地验证——本环境无 GPU/未执行，但该组测试是 `#[cfg(test)]` 纯 CPU 逻辑，无 GPU 也应通过）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `execute_dflash_draft` 不让调度器直接传「目标隐藏态」进来，而要车道自己在 prefill/verify 时捕获？

**答案**：因为草稿 rollout 读的是目标的词嵌入、输出头与捕获隐藏态，这些张量归目标 lane 所有且体积大；跨界搬运既拷贝昂贵，又破坏 speculative.rs 定下的「草稿/验证边界是纯 token 跨界」事务边界。隐藏态留在车道内（`pending_context`），调度器只见 token，这让事务核心与方法无关，也让 EAGLE-3 能直接换一条车道复用全部共享设施。

**练习 2**：`DFlashLaneState::new` 为什么要把 hedge 生效几何（`hedge_cap`/`hedge_branch_positions`）在构造期解析并打日志，而不是每次草稿轮重读环境变量？

**答案**：旋钮经 `OnceLock` 每进程只读一次，且位置解析器会静默丢弃不可解析条目并回退到位置 0——不在启动时把**生效**几何打进日志，基准测试可能在比自己以为更窄的阶梯上测量且无从发现。构造期一次性解析还保证草稿路径、验证分派、控制器三方读同一份几何，避免各自漂移（历史上「超出 block 的位置预留了永远用不上的页」正是两处漂移的产物）。

### 4.2 dflash_prefill_action：捕获资格与三态动作

#### 4.2.1 概念说明

投机解码的第一步是让草稿器拿到「种子」：目标模型在 prefill 时对每个 prompt token 产生的隐藏态。但不是每个 prefill 都能产出**干净**的隐藏态——LoRA 请求的隐藏态混入了适配器、前缀缓存命中跳过了部分前向、logprobs 评分改变了输出形状。`dflash_prefill.rs` 把「能不能捕」与「捕完怎么办」拆成两层谓词加一张三态表。

af9e40e1 的关键推广：原私有函数 `dflash_prefill_supported` 改名为 **`prefill_capture_supported`** 并提升为 `pub(super)`，文档注释从「捕获 DFlash 目标上下文」改为「产出干净目标隐藏态以播种草稿器（**DFlash 与 EAGLE-3 皆然**）」——EAGLE-3 车道的 `eagle3_prefill_eligible` 直接 import 它再叠加自己的贪心/整段约束（[src/executor/eagle3_lane.rs:L44-L53](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L44-L53)，详见 u6-l8）。

#### 4.2.2 核心流程

```text
prefill_capture_supported(req)          # 四条全过才算"干净"
  ├─ lora_adapter.is_none()             # LoRA 隐藏态混入适配器
  ├─ cached_tokens == 0                 # 前缀命中跳过了产生隐藏态的前向
  ├─ logprobs.is_none()                 # 补全 logprobs 改变输出形状
  └─ prompt_logprobs.is_none()          # 提示词评分同理
（注意：采样参数不是门 —— 隐藏态与采样无关，sampled-verify 使采样请求同样可投机）

dflash_prefill_can_capture(req, pending_exists)
  = prefill_capture_supported(req) && (chunk_start == 0 || pending_exists)
                                        # 首块，或前序块已捕获（无断链）

dflash_prefill_action(captured, completed):
  (true,  true ) → MarkReady     # 捕获完成且 prefill 结束 → 进 ready 集
  (true,  false) → KeepPending   # 捕获了但还有后续块 → 保持 pending
  (false, _    ) → Drop          # 不合格 → 丢弃陈旧 state
```

executor 侧消费点：prefill 结果回来后，对每个请求查 `draft_context_captured_requests` 是否包含它，套用三态表写 `draft_ready_requests` 集或发 `DropDraft` 命令清车道（[src/executor.rs:L2774-L2795](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2774-L2795)）。

#### 4.2.3 源码精读

- [src/executor/dflash_prefill.rs:L13-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L13-L20)——推广后的 `prefill_capture_supported`，四条合取。模块文档（[L1-L8](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L1-L8)）解释了为什么采样参数被有意排除在门外。
- [src/executor/dflash_prefill.rs:L22-L40](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L22-L40)——`dflash_prefill_can_capture` 的连续性检查（断链的 pending 缓冲不能用），与批级 `should_capture_dflash_prefill_context` 的 any() 语义：批内**任一**请求合格，这次 prefill 步就开启捕获（捕获是整批一次的前向副产品）。
- [src/executor/dflash_prefill.rs:L42-L62](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L42-L62)——`DFlashPrefillAction` 枚举与纯函数决策表；配套单测 `prefill_action_table`（[L64-L87](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L64-L87)）逐一断言四个组合。
- 捕获记录与防错——`record_prefill_dflash_context`（[src/executor/dflash_lane.rs:L127-L194](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L127-L194)）用 `ensure!` 钉死三条不变量：捕获行数必须等于调度 token 数、pending 长度必须等于 `chunk_start`（连续性）、不合格请求必须清 state。验证侧的 `record_verify_dflash_context`（[L199-L282](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L199-L282)）只把**接受前缀**的隐藏态追加进 pending（被拒草稿的续写是错的），并在轮末驱动 auto hedge 控制器（[L270-L280](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L270-L280)）。

#### 4.2.4 代码实践

1. **实践目标**：验证三态决策表与 executor 消费点的对应关系。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-qwen3 --lib prefill_action_table -- --nocapture
   ```
   然后对照 [src/executor.rs:L2774-L2795](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2774-L2795)，给三个变体各找一行消费代码。
3. **需要观察的现象**：4 条断言全部通过；`MarkReady` 分支插入 `draft_ready_requests`，`Drop` 分支额外调用 `primary.drop_draft_request`。
4. **预期结果**：单测通过（纯 CPU 逻辑，无需 GPU）。手推一个 chunked 场景：prompt 分两块、两块都捕获成功 → 第一块 prefill 后 `(true,false)=KeepPending`，第二块后 `(true,true)=MarkReady`。

#### 4.2.5 小练习与答案

**练习 1**：一个带 `cached_tokens>0`（前缀缓存命中）的请求为什么必须 `Drop` 而不是「只捕后缀」？

**答案**：前缀命中意味着命中部分的目标前向被跳过，那段的隐藏态从未产生；草稿器需要的种子是**每个 prompt token** 的干净隐藏态。只有连续捕获（首块或 pending 存在的续块）才有效，断链的 pending 缓冲在 `record_prefill_dflash_context` 里会被 `pending_len == chunk_start` 的 ensure! 拒绝。这也解释了为什么装载草稿器会结构性关闭前缀缓存（4.1.3）。

**练习 2**：`prefill_capture_supported` 推广后，EAGLE-3 为什么还要在它之上叠加 `is_greedy() && chunk_start==0 && 整段` 三个条件？

**答案**：共享谓词回答的是「隐藏态干不干净」这个方法无关问题；EAGLE-3 v1 的前向实现有自己的额外假设——`prefill_prompt` 需要整段 prompt 一次建草稿 KV，链式 rollout 目前只支持贪心请求。所以 `eagle3_prefill_eligible = prefill_capture_supported ∧ 贪心 ∧ chunk_start==0 ∧ chunk 覆盖全 prompt ∧ ≥2 token`。共享的是「干净」语义，差异留在各自车道——这正是「共享捕获、ready 集与事务，只有 worker 车道不同」的落点。

### 4.3 auto hedge：窗口化 explore-then-commit 自定价

#### 4.3.1 概念说明

hedge 链数 `C` 是个尴尬的参数：最优值随流量形态漂移（难流量收敛到多链阶梯、易流量 `C=0` 最好），还受 CUDA Graph bucket 量化与 eager 惩罚影响，拍脑袋定死必然在部分负载下亏。`AutoHedge`（`PEGAINFER_SPEC_HEDGE_AUTO=1`）的答案是把链数交给一个**在线控制器**：周期性地把每个候选 `C ∈ {0..=Cmax}`（位置表的前缀）各跑一段「窗口」，按窗口内**实际交付的 committed tokens/sec** 打分，提交给中位数最优者；提交过期后重新探索，跟随负载漂移。

设计的两个聪明之处：

1. **优化目标就是业务目标本身**（每秒提交 token 数），于是 bucket 量化、eager 惩罚、host 开销全部被自动计价，不需要为它们建模。
2. **提交偏向便宜配置**：更大的链数必须以 `ADD_CHAIN_MARGIN = 1.03` 的优势击败当前最优才被选中——平坦地形上 argmax 是噪声，不确定性向少链方向消解。

#### 4.3.2 核心流程

```text
初始化: stats[Cmax+1] 个 CandidateStats; cur_c=0; window_rounds=WINDOW_ROUNDS(64)

每轮 verify 后 tick(executed_c, committed_tokens):
  Δt = 本轮与上轮 host 时间戳差
  if Δt > ROUND_GAP_CUTOFF(500ms): 清空当前窗口并返回   # prefill/入场停顿是调度边界，不计价
  if 本窗口首轮: window_c = executed_c
  elif executed_c ≠ window_c: 丢弃半成品窗口，重开       # 窗口只记"整窗同构"的轮
  累计 (tokens, secs); rounds_in_window += 1
  窗口满(64 轮): push 进 stats[window_c].windows，只留 MAX_WINDOWS(12) 个

探索: cur_c 走完 0..=Cmax 一圈记 1 cycle；EXPLORE_CYCLES(3) 圈后提交:
  best = 0; for c in 1..=Cmax: 若 median_rate(c) > best_rate × 1.03 → best = c
  committed = Some(best); commit_left = COMMIT_ROUNDS(4096)

提交期: current_c() 返回 committed；commit_left 耗尽 → 解除提交，
  cycles 清零，window_rounds 降为 REEXPLORE_WINDOW_ROUNDS(32) 重新探索

gated 轮（负载门退化为单 token verify）: break_window()——切断计时窗口，
  该区间永不被记到任何链数头上
```

窗口速率用**中位数**而非求和 pooling：一个被污染的窗口（图预热、拖尾请求）无法像总和那样左右提交。每个候选保留最近 12 个窗口且不加权——长提交期后现任者的窗口全是新数据、挑战者是旧数据，这**有意**偏向再次探索现任者。

与车道其余策略的关系：`PEGAINFER_SPEC_HEDGE_KNEE` 的批自适应策略**优先于**控制器（4.1.3 已见分派顺序）；控制器只在 DFlash/DSpark 车道存在（`spec_hedge_auto() && hedge_cap > 0` 才构造，[src/executor/dflash_lane.rs:L84-L88](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L84-L88)）。

批自适应链数的几何约束（控制器之外的另一条路）：

\[ \text{spans} = b(1+C), \quad \text{spans} \times (\text{block}+1) \le \text{knee\_rows}, \quad \text{spans} \in \text{BATCH\_BUCKETS} \]

即扩展后的验证批既要留在 roofline 免费区（权重带宽受限、加行近乎免费）内，又要落在已捕获的 CUDA Graph bucket 上——两个 bucket 之间的跨度数会退 eager，把 hedge 收益吐回去。取满足双约束的最大 `C`；都不满足则 `C=0` 淡回基线。分数剩余（如 8 请求共容 4 条额外链）**故意不花**：唯一的中间跨度都在 bucket 外。

#### 4.3.3 源码精读

- [src/executor/auto_hedge.rs:L1-L15](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs#L1-L15)——模块文档，逐条陈述上面的设计动机（含「目标即业务目标」与窗口污染防御）。
- [src/executor/auto_hedge.rs:L20-L39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs#L20-L39)——全部常量：`WINDOW_ROUNDS=64`、`EXPLORE_CYCLES=3`、`COMMIT_ROUNDS=4096`、`REEXPLORE_WINDOW_ROUNDS=32`、`ROUND_GAP_CUTOFF=500ms`、`ADD_CHAIN_MARGIN=1.03`、`MAX_WINDOWS=12`，每个都带理由注释。
- [src/executor/auto_hedge.rs:L41-L63](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs#L41-L63)——`CandidateStats::median_rate`：过滤零时长窗口、排序取中位。
- [src/executor/auto_hedge.rs:L65-L115](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs#L65-L115)——控制器结构、`current_c()`（提交期返回 committed 否则返回 cur_c）、`break_window()`（gated 轮切断窗口：把它记到 `C=0` 头上会给无 hedge 候选喂入门控吞吐并错误推进探索/提交计数器）。
- [src/executor/auto_hedge.rs:L117-L218](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/auto_hedge.rs#L117-L218)——`tick` 全逻辑：间隔切断、窗口同构检查、记账、提交走查（从最便宜候选向上，需超 `ADD_CHAIN_MARGIN` 才替换）、提交过期与重探索降窗。
- [src/executor/dflash_lane.rs:L602-L640](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L602-L640)——`batch_adaptive_chains` 的公式实现与「分数剩余不花」的文档；车道侧消费在 [L375-L400](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L375-L400)。
- 控制器与验证结果的接缝——`record_verify_dflash_context` 轮末（[src/executor/dflash_lane.rs:L270-L280](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L270-L280)）：gated 则 `break_window()`，否则 `tick(round_chains, Σ committed)`——注意喂的是 `round_chains`（**实际执行**的链数，容量守卫可能压低它）而非请求值，配合 `clear_round_chains()`（[L667-L673](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L667-L673)）在验证侧退回朴素通过时修正账本。

#### 4.3.4 代码实践

1. **实践目标**：不依赖 GPU 验证控制器的定性行为。
2. **操作步骤**：
   ```bash
   cargo test --release -p pegainfer-qwen3 --lib auto_hedge -- --nocapture
   ```
3. **需要观察的现象**：六个用例覆盖六种行为——探索后提交最优、提交过期进重探索、长间隔切断窗口、守卫降级轮记到实际配置、平坦地形落便宜端、gated 区间永不被计价。
4. **预期结果**：`explores_all_candidates_then_commits_to_best`、`commit_expires_into_reexploration`、`long_gaps_sever_the_window`、`guard_disabled_rounds_are_booked_to_executed_config`、`flat_landscape_settles_cheap`、`break_window_never_charges_the_gated_interval` 全部通过（纯逻辑测试，无 GPU 即可运行；具体通过数待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `break_window` 要在 gated 轮调用，而不是把 gated 轮记作 `C=0` 的正常轮？

**答案**：gated 轮是单 token verify（负载门触发），它根本不在 hedge 的配置空间里。若记作 `C=0`，会把门控期的吞吐喂给无 hedge 候选当成绩，同时推进探索/提交计数器——控制器会在错误的证据上做决定。切断窗口让该区间既不计价也不推进，回来后的第一个 tick 只重新锚定时间戳。

**练习 2**：手推 `AutoHedge::new(3)` 在「所有 C 每轮都交付 25 个 token」的平坦地形上最终提交到哪个 C？为什么？

**答案**：提交到 `C=0`。每个候选的窗口速率完全相同，提交走查从 `C=0`（中位速率 best）开始，任何更大 C 的速率都不会超过 `best_rate × 1.03`，于是 best 停在 0。这正是 `ADD_CHAIN_MARGIN` 的设计意图：平坦地形上 argmax 是噪声，不确定性向更便宜的配置消解（对应单测 `flat_landscape_settles_cheap`）。

**练习 3**：`batch_adaptive_chains(2, 7, 96, 7, BATCH_BUCKETS)` 返回什么？（block=7、knee=96 行、最多 7 链）

**答案**：span=8 行；`b(1+C)×8 ≤ 96 ⇒ b(1+C) ≤ 12`。b=2 时 `2(1+C) ∈ {2,4,8}`（bucket 集内 ≤12 的值），最大为 8 ⇒ `C=3`——注意不是免费区内最大的 C=5（10 跨度不在 bucket 上）。这是单测 `batch_adaptive_chains_picks_the_largest_bucket_aligned_count_under_the_knee` 的第二个断言。

### 4.4 spec_env 校验与投机旋钮全景

#### 4.4.1 概念说明

`spec_env.rs` 只管两个旋钮：`PEGAINFER_SPEC_MAX_BATCH`（负载门）与 `PEGAINFER_SPEC_MAX_DRAFTS`（草稿数截断）。它们共享一套刻意的语义：unset / 空白 / 显式 `0` 都是关；**任何无法解析的值在引擎启动时点名报错**，而不是静默当作关。理由写在模块文档里：静默回退会让一个「负载杠杆」变成看起来已布防的 no-op。

有趣的是对照：hedge 家族旋钮（`HEDGE`/`HEDGE_POSITIONS`/`HEDGE_AUTO`/`HEDGE_KNEE`）的文档行为恰是「unparsable = off」的静默回退（各自 OnceLock 里 `.ok().unwrap_or(0)` 一类解析），只是把**生效**几何打进启动日志补偿可见性（4.1.3）。差别在于两类旋钮的失配代价：MAX_BATCH/MAX_DRAFTS 是服务运营者按容量规划拧的负载闸，拧错了引擎还在跑、只是行为错了且难以察觉——所以必须当场拒绝；hedge 是纯性能实验旋钮，回退到 off 是安全的保守态。

#### 4.4.2 核心流程

```text
引擎入口（lib.rs 两个 start 函数）
  └─ spec_env::validate()            # 先于一切装载：坏值 → 启动失败，报错含变量名
       └─ 对 MAX_BATCH、MAX_DRAFTS 各跑 parse(env_value(name))
  ……（模型装载、执行器构建）……
首次使用处（OnceLock 惰性读，每进程一次）
  ├─ plan::spec_max_batch()          # scheduler/plan.rs 的负载门
  └─ dflash::MAX_DRAFTS 截断         # effective_drafts()
```

`read()` 内部是 `.expect("spec knobs are validated before the engine starts")`——把「已校验」变成硬不变量：校验漏掉某个入口属于 bug 而非用户输入错误。

#### 4.4.3 源码精读

- [src/spec_env.rs:L14-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L14-L27)——`parse`：三种「关」的形态归一为 0，解析失败 `bail!` 且报错文本含变量名（配套单测 [L57-L65](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L57-L65) 断言了这点）。
- [src/spec_env.rs:L29-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L29-L43)——非 UTF-8 值也点名拒绝；`read` 的 expect 语义。
- [src/spec_env.rs:L45-L51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L45-L51)——`validate()` 遍历两个旋钮；调用点在 [src/lib.rs:L442-L443](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L442-L443) 与 [L488-L489](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L488-L489)——两个引擎入口函数的第一行。
- 负载门的读取与语义——[src/scheduler/plan.rs:L164-L171](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L164-L171)（`spec_max_batch`）；`MAX_DRAFTS` 的读取在 [src/dflash.rs:L29-L32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/dflash.rs#L29-L32)。
- hedge 家族的 OnceLock 读取——[src/executor/dflash_lane.rs:L524-L665](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L524-L665)：`spec_hedge_auto`（恰 `"1"` 开启）、`spec_hedge_effective`（单一几何源）、`hedge_pages_per_chain`（scratch KV 计页的单一公式，`block_size.div_ceil(page_size) + 1` 饱和加，防回绕少预留）、`spec_hedge_cap`（clamp 64）、`spec_hedge_knee`、`spec_hedge_positions`（排序去重截 16、默认 `[0]`）。
- **CLI 互斥的两层设防**——第一层 `model_line.rs`（启动最早期、报错优先级即块序）：`--batch-invariant × --dflash-draft-model-path`（[L226-L230](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L226-L230)）；DFlash 与 LoRA / kv-offload / tp≠1（[L243-L259](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L243-L259)）；`--eagle3-draft-model-path` 的冲突表循环（dflash / LoRA / kv-offload / decode-overlap / batch-invariant，[L260-L284](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L260-L284)）。第二层 `lib.rs`：两个草稿路径折叠为单槽 `DraftModel`、同给即 bail（[L316-L326](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L316-L326)）、草稿 × LoRA、草稿 × decode-overlap（[L327-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L327-L336)）；batch-invariant 对**任意**草稿器的拒绝文案已推广为 speculative（[L560-L566](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L560-L566)——理由：Pin 预热只覆盖基础投影，草稿器的 fc/MLP GEMM 未被钉死会在 Pin 下于 GEMM 边界 bail）。

#### 4.4.4 代码实践

1. **实践目标**：亲眼确认两类旋钮的失配行为差异——一个拒绝启动，一个静默回退但日志可见。
2. **操作步骤**：
   - 先跑现有校验单测，确认「报错含变量名」的契约：
     ```bash
     cargo test --release -p pegainfer-qwen3 --lib spec_env -- --nocapture
     ```
   - 再用 grep 核对旋钮全集：`rg -n "PEGAINFER_SPEC" src/`，把命中逐条填进第 5 节矩阵的「作用面」列——应恰好命中 6 个环境旋钮，没有遗漏也没有多余。
   - 补一个非法值拒绝单测变体（见第 5 节第二步的示例代码，需在你自己的分支上完成；本讲义未改动源码）。
3. **需要观察的现象**：`blank_means_off_and_a_bad_value_names_the_knob` 通过；rg 的命中全部落在 `spec_env.rs`、`dflash.rs`、`scheduler/plan.rs`、`executor/dflash_lane.rs` 四个文件。
4. **预期结果**：单测通过（纯 CPU）；矩阵填完后与第 5 节参考答案逐行对照（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`PEGAINFER_SPEC_MAX_BATCH=8x`（多了个 x）的引擎会发生什么？`PEGAINFER_SPEC_HEDGE=8x` 呢？

**答案**：前者**拒绝启动**，报错文本含 `PEGAINFER_SPEC_MAX_BATCH: expected a non-negative integer, got "8x"`——validate() 在引擎入口第一行执行。后者静默视为 0（hedge 关闭）——`spec_hedge_cap` 的解析是 `.ok().unwrap_or(0)`，文档明写「unparsable = off」；启动日志里不会出现 "spec hedge on" 行，这是发现它没生效的线索。

**练习 2**：为什么 `read()` 里用 `expect` 直接 panic 是可接受的设计？

**答案**：因为两个引擎入口函数（`start_engine_with_offload` / `start_engine_with_lora_control`）都在第一行调用 `validate()`，任何合法路径到达 `read()` 时值必然已通过解析。此时 panic 表达的是「不变量被打破」（某个入口漏了校验），属于程序 bug 而非用户输入，fail-loud 恰是想要的行为。

**练习 3**：`--eagle3-draft-model-path` 与 `--dflash-draft-model-path` 同时给的报错出自哪一层？为什么两层都要写？

**答案**：正常 CLI 启动下第一层 `model_line.rs` 的 eagle3 冲突表先报（`--eagle3-draft-model-path is not supported with --dflash-draft-model-path`）。第二层 `lib.rs` 的折叠 match（`(Some(_), Some(_)) → bail!("DFlash and EAGLE-3 drafters cannot be loaded together")`）兜住绕过 CLI、直接构造 `Qwen3LaunchOptions` 的调用方（测试、嵌入式启动器）。同一不变量多层设防、失败越早越便宜——u1-l3 建立的原则在这里再次出现。

## 5. 综合实践

**任务：制作「旋钮 × 作用面 × 默认值 × 互斥项」矩阵，补一个非法值拒绝单测，并解释共享无损性判定。**

### 第一步：旋钮矩阵（参考答案）

| 旋钮 / 标志 | 作用面 | 默认 | 读取时机 | 互斥 / 限制 |
|---|---|---|---|---|
| `PEGAINFER_SPEC_MAX_DRAFTS` | [dflash.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/dflash.rs#L29-L32) `effective_drafts`：每轮保留的草稿数 K 截断 | 0=不截断 | OnceLock；**坏值拒绝启动** | 仅在装载草稿器后有意义 |
| `PEGAINFER_SPEC_MAX_BATCH` | [plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L164-L171) 负载门：活动批 ≥ n 时该步不投机（Gated） | 0=off | 同上 | 与草稿器能力位 `max_draft_batch` 叠加判定 |
| `PEGAINFER_SPEC_HEDGE` | [dflash_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L568-L584) hedge 每轮最多 Hedged 请求数，clamp 64 | 0=off | OnceLock；unparsable=off（启动日志可见生效几何） | 需 Markov 头（DSpark 草稿器） |
| `PEGAINFER_SPEC_HEDGE_POSITIONS` | [dflash_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L642-L665) 分支位置表（排序去重截 16） | `[0]` | 同上 | 位置需 `< block_size` 才实际生效 |
| `PEGAINFER_SPEC_HEDGE_AUTO` | [dflash_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L524-L532) 自定价控制器开关（恰 `1`） | off | 同上 | 需 `HEDGE>0` 且有生效位置 |
| `PEGAINFER_SPEC_HEDGE_KNEE` | [batch_adaptive_chains](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L586-L640) 批自适应链数（免费区行数） | 0=off | 同上 | **优先于**固定链数与 auto 控制器 |
| `--dflash-draft-model-path` | 装载 DFlash 草稿器（共享 flag，[model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L243-L259)） | None | CLI 解析 | LoRA、kv-offload、batch-invariant、tp≠1；与 EAGLE-3 互斥；装载即关前缀缓存 |
| `--eagle3-draft-model-path` | 装载 EAGLE-3 草稿器（Qwen3 专属 flag，[model_line.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/model_line.rs#L260-L284)） | None | CLI 解析 | dflash、LoRA、kv-offload、decode-overlap、batch-invariant、tp≠1 |

自己动手时建议以 grep 逐行核对：`rg -n "PEGAINFER_SPEC" src/` 应恰好命中上表全部旋钮，没有遗漏也没有多余。

### 第二步：补一个非法值拒绝单测

仿照 [src/spec_env.rs:L57-L65](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L57-L65) 的 `blank_means_off_and_a_bad_value_names_the_knob`，在自己的工作副本中给 `spec_env.rs` 的 tests 模块加一个变体（示例代码，非项目原有）：

```rust
#[test]
fn negative_and_whitespace_padded_values_are_rejected_by_name() {
    for bad in ["-1", " 8 ", "0x10"] {
        let error = parse(MAX_BATCH, Some(bad)).unwrap_err().to_string();
        assert!(error.contains(MAX_BATCH), "{bad}: {error}");
    }
}
```

注意 `" 8 "` 会先被 trim 再解析，所以它实际是**合法**值 8——把它放进坏值表会失败；这个「陷阱」正好逼你读 [L15-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L15-L27) 的 trim 逻辑后修正用例。运行：

```bash
cargo test --release -p pegainfer-qwen3 --lib spec_env
```

预期两个测试通过（待本地验证）。（本讲义未修改任何源码；该练习需要你在自己的分支上完成。）

### 第三步：解释共享无损性判定

阅读 [tests/common/lossless.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs)（af9e40e1 从 `dflash_speculative_gate.rs` 抽出），写一段说明，必须覆盖：

1. **为什么精确匹配是错误的门**（[L1-L8](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L1-L8)）：verify 走 prefill 注意力核、朴素解码走 decode 核，两者差约 1 bf16 ULP，近平局处 argmax 翻转后两条贪心序列完全分岔——精确断言会**伪失败**。
2. **regret 判定的三段逻辑**（[check_lossless，L93-L216](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L93-L216)）：取首个分歧点（此刻两序列上下文仍相同，比较有效）→ 用 `prefill_next` 重算共享上下文的 **prefill 核**参考分布 → spec 选的 token 若等于 prefill argmax 则是核间隙翻转（非 bug）；否则若它距 prefill argmax 的 regret ≤ `MARGIN_TOL=0.20`（约 3 个 bf16 ULP）则是良性近平局；再否则（落在 top-K 之外或明显更差）才是真投机 bug。
3. **谁在用它**：[dflash_speculative_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/dflash_speculative_gate.rs#L70-L74) 与 [eagle3_speculative_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L37-L41) 现在 import 同一份 `common::lossless::{LOGPROBS, Step, check_lossless, generate, to_steps}`——两个草稿器的方法不同，但「贪心投机必须无损、且无损的定义必须容忍核间隙」这个判定完全共享。这正是 u6-l1「只有 proposer 不同」在测试层的镜像。
4. 顺带记录：DFlash 门测试的 `launch_options` 现在需要显式填 `eagle3_draft_model_path: None`（[dflash_speculative_gate.rs:L115-L139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/dflash_speculative_gate.rs#L115-L139)）——第二草稿器加入后启动选项结构体多了一个槽位。

两个门测试本身需要 GPU + 双份权重（`PEGAINFER_TEST_MODEL_PATH` + `PEGAINFER_DFLASH_TEST_MODEL_PATH` / `PEGAINFER_EAGLE3_TEST_MODEL_PATH`），缺权重时按 u1-l2 的模式 clean skip，不是失败。

## 6. 本讲小结

- **草稿车道与目标同线程**是所有权与边界的双重选择：草稿 rollout 直接读目标的嵌入/输出头/捕获隐藏态，隐藏态留在车道内（`pending_context` 私有），调度器只见纯 token——`LocalQwen3Lane` 上 `dflash`/`eagle3` 两个互斥槽位是这条边界在类型上的落实。
- **`prefill_capture_supported` 已从 DFlash 专属推广为两种草稿器共享的「干净隐藏态」谓词**（无 LoRA、无前缀命中、无两种 logprobs；采样参数故意不是门），EAGLE-3 在其上叠加贪心与整段约束；prefill 后的三态动作表 MarkReady/KeepPending/Drop 由 executor 统一消费，写入 `draft_ready_requests` 就绪集。
- **`AutoHedge` 是窗口化 explore-then-commit 控制器**：按候选 C 的 64 轮窗口、以业务目标本身（committed tokens/sec 的中位数）定价，3 圈探索后向便宜方向偏置（×1.03 裕量）提交，4096 轮后重探索；500ms 间隔切断窗口、gated 轮 break_window、守卫降级轮记实际执行链数，三道防污染。
- **旋钮语义分两档**：`MAX_BATCH`/`MAX_DRAFTS` 走 `spec_env` 的「坏值点名拒绝启动」（入口第一行 validate，read 用 expect 把已校验升格为不变量）；hedge 家族是「unparsable=off」的实验旋钮，靠启动日志的生效几何补偿可见性；批自适应 `KNEE` 优先于 auto 控制器，链数受「免费区行数 ∧ CUDA Graph bucket」双约束。
- **CLI 互斥双层设防**：model_line 在启动最早期拒绝（含 EAGLE-3 的冲突表循环），lib.rs 折叠双草稿路径为单槽 `DraftModel` 兜住程序化构造；装载任一草稿器结构性关闭前缀缓存，batch-invariant 的拒绝文案已推广为 speculative。
- **`tests/common/lossless.rs` 把无损性判定抽成共享模块**：regret 检查容忍 prefill/decode 核的 1-ULP 平局翻转，同时服务 DFlash 与 EAGLE-3 两个门测试——测试层与运行时层同步完成了「共享事务、可换 proposer」的重构。

## 7. 下一步学习建议

- **u6-l8（EAGLE-3 车道与门控）**：本讲的直接续篇——`Eagle3LaneState` 如何复用 `prefill_capture_supported` 与 `DropDraft` 命令，`max_draft_batch=Some(1)` 如何在 `speculation_decision_at` 里让宽批退回纯解码，以及 `eagle3_speculative_gate.rs` 四组行为测试如何使用本讲的 lossless 判定。
- 若想再深一层验证理解：对照 [src/executor/dflash_lane.rs:L696-L710](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_lane.rs#L696-L710) 的 `select_hedged_requests`，解释「draft 与 verify 从同一列表选取」如何防止采样请求或预算饥饿请求烧掉一个 verify 侧会拒绝的 hedge 槽位——这是 u6-l4 hedge 语义在车道侧的收口。
- 回望 u4-l2 的 `speculation_decision_at`，把本讲的 `max_draft_batch` 门、`draft_ready_requests`（由本讲三态表驱动）与负载门三者的检查顺序画成一张判定流程图，作为投机子系统的总复习。
