# u6-l8 EAGLE-3 车道：单请求贪心草稿集成与门控

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `DraftModel` 枚举如何在 launch、executor、worker 三层把 DFlash 与 EAGLE-3 分派到 `WorkerCommand::LoadDflash` / `LoadEagle3` / `DropDraft` 三条命令上。
2. 解释 `eagle3_prefill_eligible` 为何在共用的「干净隐藏态」谓词之外，还要求贪心采样、整段 prompt 一次吃下、且 `chunk_start == 0`。
3. 理解 `EAGLE3_MAX_BATCH = 1` 如何通过 `DraftMeta.max_draft_batch` 能力位上报到调度器的 `speculation_decision_at`，让宽批与重叠请求整批退回纯解码。
4. 说明 `tests/common/lossless.rs` 的 `MARGIN_TOL` 容差如何用一个「regret 检查」同时服务 DFlash 与 EAGLE-3 两个投机门测试。

本讲是投机解码单元的最后一讲：u6-l1 建立了方法无关的事务核心，u6-l5 拆开了 EAGLE-3 草稿器的前向（捕获层换算、`prefill_prompt`/`chain_round`/`rebuild_after_verify`），u6-l7 讲了 DFlash 车道与投机旋钮。本讲把这些拼起来：EAGLE-3 的 **worker 侧车道** 如何嵌进 executor 的线程模型，以及调度门如何用能力位把「草稿器做不到的事」挡在投机路径之外。

## 2. 前置知识

- **车道（lane）与 worker 线程**（u3-l2）：`LocalQwen3Lane` 把一台 GPU 的全部可变状态（模型、KV/解码缓冲、草稿车道）打包后独占一条 worker 线程；executor 线程与 worker 之间用 crossbeam 通道做 RPC。回忆这条纪律：**草稿器的隐藏态不出车道**——调度器与草稿器之间只有纯 token 跨界。
- **投机解码事务**（u6-l1）：草稿器提出 \( K \) 个草稿 token，目标模型一次前向验证 \( K+1 \) 个位置，接受最长匹配前缀并附加一个奖励 token；贪心请求下这严格无损，因为每个 committed token 都是目标自己的 argmax。
- **能力位（capability bit）**：一个组件向调度器声明的「我能做到的上限」。本讲的主角 `max_draft_batch` 就是这样一个位：DFlash 报 `None`（任意批宽都能草稿），EAGLE-3 报 `Some(1)`（单轮只服务一个请求）。
- **贪心（greedy）采样**：温度为 0 时每步取 argmax，输出确定。EAGLE-3 v1 只支持贪心请求，后面会看到这个限制渗进了 prefill 资格判定。
- **regret（遗憾值）**：在某个分布里，「最优选项的 logprob」减去「实际所选选项的 logprob」的差。差越小，说明所选选项越接近并列最优；本讲的 lossless 判定用它区分「良性平局翻转」与「真 bug」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/speculative.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs) | 方法无关的投机核心；本讲关注 `DraftModel` 枚举与 `DraftStepItem`/`VerifyStepItem` 的 token 跨界契约 |
| [src/executor/eagle3_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs) | **本讲主角**：worker 侧 EAGLE-3 车道——`Eagle3LaneState`、装载、prefill 捕获、草稿执行、验证后重建 |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | executor 契约层：`load_draft_model` 分派、`WorkerCommand` 协议、verify 步的捕获层选择、`DraftMeta` 定义 |
| [src/executor/spec.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs) | 投机 draft/verify 的 executor 侧实现：ready 集校验、KV 预留事务、worker 分派 |
| [src/executor/dflash_prefill.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs) | 共用的 prefill 捕获谓词 `prefill_capture_supported` 与三态动作表 |
| [src/scheduler/plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs) | 调度门 `speculation_decision_at` 与 `max_draft_batch` 批宽门测试 |
| [src/scheduler/test_support.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs) | `FakeExecutor`（CPU 单测替身）的 `with_max_draft_batch` |
| [tests/eagle3_speculative_gate.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs) | EAGLE-3 门测试：贪心等价、预算边界、中断恢复、重叠回退（四组） |
| [tests/common/lossless.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs) | 两个投机门测试共享的无损性判定（`check_lossless` + `MARGIN_TOL`） |
| [src/lib.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs) | `launch` 把两条草稿路径折叠成互斥的单槽 `DraftModel` |
| [src/frontend_adapter.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs) | 构建后旋钮顺序：`set_no_prefix_cache` → `enable_decode_overlap` → `load_draft_model` |

## 4. 核心概念与源码讲解

### 4.1 DraftModel 三层分派：从 CLI 到 worker 车道

#### 4.1.1 概念说明

Qwen3 支持两种草稿器：DFlash（u6-l2~l4）与 EAGLE-3（u6-l5）。它们共享投机事务核心、目标隐藏态捕获和 ready 集，**只有 worker 车道不同**。为了不让「是哪种草稿器」这个信息散落各处，crate 用一个两值枚举 `DraftModel<'a>`（借 checkpoint 路径）把选择一路携带到真正需要分派的位置。

分派发生在三层：

1. **launch 层**（进程入口）：两个 CLI 字段折叠成互斥的单槽 `Option<DraftModel>`——两个都给直接拒绝。
2. **executor 层**（契约层）：`load_draft_model` 按 `DraftModel` 变体调用不同的 worker RPC；装载成功后把 `DraftMeta` 存进 `self.speculative`，从此 executor 只看 meta，不再关心是哪种草稿器。
3. **worker 层**（GPU 线程）：`LocalQwen3Lane` 上 `dflash` 与 `eagle3` 是互斥槽位，由 `WorkerCommand::LoadDflash` / `LoadEagle3` / `DropDraft` 三条命令驱动。

这个设计的关键收益：**方法差异被压缩到一个枚举 match 上**。事务核心（`speculative.rs`）、调度门、ready 集管理全部方法无关；新增第三种草稿器只需要加一个枚举变体、一条装载命令和一个车道。

#### 4.1.2 核心流程

```text
CLI: --dflash-draft-model-path / --eagle3-draft-model-path
        │
        ▼
launch（lib.rs）
  (Some, Some) → bail "cannot be loaded together"
  (Some, None) → Some(DraftModel::DFlash(path))
  (None, Some) → Some(DraftModel::Eagle3(path))
        │  还顺带校验：草稿器 × LoRA、草稿器 × decode-overlap 互斥
        ▼
start_engine_with_offload_inner → 构建目标模型（显存画像时按 draft 变体预留）
        │
        ▼
frontend_adapter: set_no_prefix_cache → enable_decode_overlap → load_draft_model(draft)
        │
        ▼
Qwen3Executor::load_draft_model
  DraftModel::DFlash  → primary.load_dflash(path)  ─┐ 都是 RPC：发送 WorkerCommand
  DraftModel::Eagle3  → primary.load_eagle3(path)  ─┘ {LoadDflash|LoadEagle3}, 阻塞等 DraftMeta
        │
        ▼
worker loop（executor.rs）→ lane.load_dflash / lane.load_eagle3
  装进 lane 的互斥槽位（dflash: Option<DFlashLaneState>, eagle3: Option<Eagle3LaneState>）
  返回 DraftMeta → executor 存 self.speculative = Some(meta)
  关前缀缓存 + 挂计数器

服务期的卸载/退休：DropDraft 命令同时清两个车道（drop_dflash_request + drop_eagle3_request）
```

#### 4.1.3 源码精读

枚举本体在方法无关核心里定义，借的是 checkpoint 路径的生命周期：

[src/speculative.rs:33-38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L33-L38) 定义 `DraftModel<'a>` 两值枚举——「按 checkpoint 路径选择草稿器」，模块文档同时声明两个 proposer 共享捕获、ready 集与事务，只有 worker 车道不同。

launch 层的折叠与互斥：

[src/lib.rs:316-336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L316-L336) 把 `dflash_draft_model_path` 与 `eagle3_draft_model_path` 折叠成 `Option<DraftModel>`：两个都给则 bail「DFlash and EAGLE-3 drafters cannot be loaded together」；随后两条 ensure 拒绝「草稿器 × LoRA」与「草稿器 × decode-overlap」组合。

executor 层的分派点：

[src/executor.rs:1793-1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827) 是 `load_draft_model` 的完整流程：先 ensure 单卡拓扑、无 KV offload；再按 `DraftModel` 变体调用 `self.primary.load_dflash(...)` 或 `self.primary.load_eagle3(...)`（EAGLE-3 分支的日志直接写明「N drafts per round, greedy batch-1 only」）；装载成功后 `set_prefix_cache_enabled(false)`（投机捕获需要干净、未缓存的目标隐藏态，前缀缓存命中会跳过产生它们的那次前向）、挂 `SpecDecodeCounters`、把 meta 存进 `self.speculative`。

worker 命令协议的三条草稿命令：

[src/executor.rs:4262-4278](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4262-L4278) 定义 `WorkerCommand::LoadDflash`、`LoadEagle3`（都带回信通道，注释说明「必须在 worker 线程上构建，因为草稿器要读同址的目标模型」）与 `DropDraft`（请求退休或退回纯解码后清草稿态）。

[src/executor.rs:4416-4428](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4416-L4428) 是 worker loop 的处理分支：`LoadDflash`/`LoadEagle3` 各自调车道装载方法；注意 `DropDraft` **无条件调两个车道的 drop**——`drop_dflash_request(request_id)` 与 `drop_eagle3_request(request_id)` 都对空槽位是安全的 no-op，这样退休路径不必知道当前装的是哪种草稿器。

[src/executor.rs:4529-4540](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4529-L4540) 是 `RankWorker::load_eagle3` 的 RPC 形态：`channel::bounded(1)` 回信通道 + `recv()` 阻塞——与 u3-l2 讲过的「unbounded 命令通道 + bounded(1) 回信」协议一致，装载是启动期一次性操作，阻塞等待没有吞吐代价。

构建后旋钮顺序（frontend_adapter 侧）：

[src/frontend_adapter.rs:124-132](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/frontend_adapter.rs#L124-L132) 中的顺序有讲究：`set_no_prefix_cache` → `enable_decode_overlap` → `load_draft_model(draft)`。注释解释了原因——草稿器在目标模型就绪**之后**装载（它要读目标的 embedding/lm_head），且装载动作会强制关前缀缓存，所以必须排在 `set_no_prefix_cache` 之后；它的显存足迹在更早的画像期已按 draft 路径预留（见 4.2.3）。

#### 4.1.4 代码实践

**实践目标**：用 `rg` 亲手追踪「一条 `--eagle3-draft-model-path` 从 CLI 字段到 worker 槽位」的完整调用链，验证三层分派图。

**操作步骤**：

1. 在 crate 根目录运行：
   ```bash
   rg -n "eagle3_draft_model_path" src/
   rg -n "DraftModel::" src/ | head -30
   rg -n "LoadEagle3|load_eagle3|eagle3:" src/executor.rs
   ```
2. 按输出整理一张表：每行的文件、行号、属于哪一层（launch / executor / worker）。
3. 验证两个不变量：(a) `DropDraft` 处理分支同时调用了两个车道的 drop；(b) `load_draft_model` 在两个变体分支之后都执行了 `set_prefix_cache_enabled(false)`。

**需要观察的现象**：`eagle3_draft_model_path` 这个字段名只出现在 launch 层（`lib.rs` 的选项结构、折叠 match、`from_runtime` 系数传递）；一旦折成 `DraftModel::Eagle3(path)`，后续所有代码只见枚举变体，不再见 CLI 字段名——「CLI 知识不泄漏进执行层」。

**预期结果**：得到一张约 6~8 行的分派表，与 4.1.2 的流程图一一对应。本实践为纯源码阅读，无需 GPU（待本地验证的只有表格本身，无运行结果）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DropDraft` 命令不携带「哪种草稿器」的信息，而是同时清两个车道？

**答案**：因为 `dflash` 与 `eagle3` 在 `LocalQwen3Lane` 上是互斥槽位（launch 层已保证二选一），任意时刻至多一个非空；两个 drop 方法对空槽位都是安全 no-op（`if let Some(...)` 模式，见 [src/executor/eagle3_lane.rs:232-237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L232-L237)）。让退休路径无脑清两边，避免了「请求退休时还要查询当前装载的草稿器类型」这笔额外协议面——类型信息留在装载期的一次 match 里就够了。

**练习 2**：`StepCommand::SpeculativeDraft` 到达 worker 后如何决定走哪条车道？为什么这个判断是安全的？

**答案**：见 [src/executor.rs:534-541](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L534-L541)：`if lane.eagle3.is_some() { execute_eagle3_draft } else { execute_dflash_draft }`。安全性来自上游不变量——executor 侧 `execute_speculative_draft_impl`（[src/executor/spec.rs:210-235](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L210-L235)）先 ensure `self.speculative.is_some()` 才发命令，而 `speculative` 与车道槽位在 `load_draft_model` 里同时被置位；两车道互斥使得「非 eagle3 即 dflash」的 else 分支不会落在空车道上。

### 4.2 Eagle3LaneState 与装载：DraftMeta 能力位的诞生

#### 4.2.1 概念说明

`Eagle3LaneState` 是 EAGLE-3 车道的全部状态，与 u6-l7 讲过的 `DFlashLaneState` 同构：草稿模型 + 每 request 草稿态 + 复用的 scratch。它的三条纪律：

1. **与目标模型同线程**：草稿器读目标的 embedding、lm_head，还读目标在 prefill/verify 时捕获的隐藏态——这些隐藏态留在车道内，不出 worker 线程。
2. **scratch 一次分配跨步复用**：`Eagle3Scratch` 全部 `seq_len == 1`（u6-l5），这正是批宽上限为 1 的物理根源。
3. **装载即上报能力**：`load_eagle3` 返回的 `DraftMeta` 携带 `max_draft_batch: Some(EAGLE3_MAX_BATCH)`，把「单轮只服务一个请求」这个物理限制变成调度器可见的能力位。

`DraftMeta` 是「executor 装完草稿器后学到的关于它的事」：块大小、每轮草稿数、可缓存的最大位置、批宽上限。注意 `max_draft_batch` 是 `Option<usize>`——`None` 表示「任意批宽都行」（DFlash），`Some(n)` 表示「最多 n 个」。用 `Option` 而不是一个大数，让调度门可以写 `is_some_and(|limit| active.len() > limit)`，语义上「无限制」与「限制为 1」是两种不同的陈述。

#### 4.2.2 核心流程

```text
load_eagle3(draft_path) 在 worker 线程上执行：
  1. Eagle3DraftModel::from_safetensors_for_target(ctx, path, &target)
       （针对目标构建：复用目标的 embed/lm_head，见 u6-l5）
  2. aux_layers = aux_hidden_state_layers(target.num_hidden_layers)
       → [1, N/2-1, N-4]，36 层目标得 [1, 17, 32]
  3. scratch = model.new_scratch(ctx)   （单 token scratch）
  4. meta = DraftMeta {
         block_size:            EAGLE3_CHAIN_LENGTH + 1,   // verify span = γ+1 = 4
         num_spec_tokens:       EAGLE3_CHAIN_LENGTH,       // γ = 3
         max_position_embeddings: model.config.max_position_embeddings,
         max_draft_batch:       Some(EAGLE3_MAX_BATCH),    // Some(1) ★ 能力位
     }
  5. lane.eagle3 = Some(Eagle3LaneState { model, requests, scratch, aux_layers })
  → meta 沿 RPC 回到 executor，存进 self.speculative
  → 调度器经 ModelExecutor::max_draft_batch() 读到 Some(1)
```

#### 4.2.3 源码精读

车道状态本体：

[src/executor/eagle3_lane.rs:30-36](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L30-L36) 定义 `Eagle3LaneState`：`model`（草稿模型）、`requests: HashMap<RequestId, Eagle3RequestState>`（每请求草稿态）、`scratch`（跨步复用的单 token scratch）、`aux_layers: [usize; 3]`（目标的三层捕获索引）。文件头注释（[L1-L6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L1-L6)）写明它像 DFlash 车道一样住在 worker 线程上、隐藏态不出车道、草稿/验证边界是纯 token 跨界。

两个常量：

[src/eagle3.rs:17-24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L17-L24) 定义 `EAGLE3_CHAIN_LENGTH = 3`（每轮 γ=3 个草稿，verify span 为 γ+1；v1 是固定 top-1 **链**而非树）与 `EAGLE3_MAX_BATCH = 1`（v1 经单 token scratch 草稿，所以调度器对更大的批直接纯解码）。

装载与能力位铸造：

[src/executor/eagle3_lane.rs:57-75](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L57-L75) 是 `load_eagle3` 全文：构建草稿模型、算 `aux_layers`、建 scratch，然后填 `DraftMeta`——注意 `max_draft_batch: Some(EAGLE3_MAX_BATCH)` 这一行就是能力位的诞生处，与 DFlash 侧的 `max_draft_batch: None`（[src/executor.rs:3596-3608](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3596-L3608) 中 `load_dflash` 的 meta 构造）形成对照。

`DraftMeta` 结构与 trait 透传：

[src/executor.rs:3372-3388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3372-L3388) 定义 `DraftMeta`，四个字段各有文档；`max_draft_batch` 直接引用 trait 方法文档。

[src/executor.rs:834-838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838) 是 `ModelExecutor` trait 上 `max_draft_batch` 的默认实现（`None`）——**「已装载草稿器单轮最多为多少个请求提案；`None` 表示任意 decode 批都能草稿」**，这让 `FakeExecutor` 与未装草稿器的执行器自动获得「不限」语义。

[src/executor.rs:2892-2896](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2892-L2896) 是 `Qwen3Executor` 的实现：从 `self.speculative`（即 `DraftMeta`）里 `and_then(|meta| meta.max_draft_batch)` 透传——能力位在装载时一次性写定，服务期只读。

显存预留（承接 u1-l4 的「画像期预留」）：

[src/executor.rs:1391-1402](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1391-L1402) 在单卡路径上按 `DraftModel::Eagle3(path)` 分支调 `Eagle3MemoryReservation::from_path(path, max_prefill_tokens, EAGLE3_MAX_BATCH)`：草稿模型活在分页 KV 池之外，所以它的固定字节（权重 + 块 scratch）折进 margin，逐 token 的池缩放字节折进块预算——草稿器在目标之后装载却不 OOM 的原因在这里。

#### 4.2.4 代码实践

**实践目标**：对比两种草稿器的 `DraftMeta`，确认能力位差异并解释其物理根源。

**操作步骤**：

1. 阅读 [src/executor/eagle3_lane.rs:62-67](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L62-L67) 与 [src/executor.rs:3596-3608](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3596-L3608)，抄下两个 meta 的四个字段值。
2. 追问根源：`rg -n "seq_len" src/eagle3/forward.rs | head` 观察 `Eagle3Scratch` 相关分配的形状；再对照 `rg -n "DFlashBatchScratch" src/dflash.rs` 的块状 scratch。
3. 写三行结论：字段差异表 + 「为什么 DFlash 的批 scratch 能服务 32 请求而 EAGLE-3 不能」。

**需要观察的现象**：EAGLE-3 侧所有 scratch 维度以单 token 为单位（seq 维恒 1），而 DFlash 的批 scratch 以 block 为行、天然按批宽分配。

**预期结果**：DFlash：`max_draft_batch = None`、`block_size = effective_drafts() + 1`；EAGLE-3：`max_draft_batch = Some(1)`、`block_size = 4`（γ+1）、`num_spec_tokens = 3`。根源：EAGLE-3 的链式 rollout 逐 token 推进、scratch 只需单行，v1 没有为多请求并行行做形状泛化。（结论基于源码形状，无需 GPU。）

#### 4.2.5 小练习与答案

**练习 1**：`max_draft_batch` 用 `Option<usize>` 而非 `usize`（比如「0 表示不限」或 `usize::MAX`）有什么好处？

**答案**：`None` 语义上就是「无此限制」，不会与任何数值混淆；`0` 会被误读为「什么都服务不了」，`usize::MAX` 则让 `active.len() > limit` 永假、语义上能工作但把「设计上无限制」与「限制恰好极大」混为一谈。`Option` 还让 trait 默认实现（[src/executor.rs:834-838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838)）一行返回 `None` 即可表达「未装草稿器/不限」两种「无能力位」状态，配合 `is_some_and` 的门控写法（4.4.3）非常自然。

**练习 2**：如果未来 EAGLE-3 支持批宽 4，需要改哪些地方？（列举而非实现）

**答案**：至少四处——(1) `EAGLE3_MAX_BATCH` 常量或改为配置；(2) `Eagle3Scratch` 的形状泛化（单 token 行 → 批行），`chain_round` 的 scratch 布局；(3) `Eagle3MemoryReservation::from_path` 的批宽参数相应放大预留；(4) 门测试 `eagle3_overlapping_requests_fall_back_and_stay_lossless`（4.4.3）的断言需按新批宽重写。能力位 `Some(4)` 会自动让调度门放行批宽 ≤4 的批——这正是能力位设计的回报：调度侧无需改动。

### 4.3 草稿循环时序：prefill 捕获 → chain_round → verify → rebuild

#### 4.3.1 概念说明

本模块把 u6-l5 的三个前向入口（`prefill_prompt`、`chain_round`、`rebuild_after_verify`）放回它们被调用的完整时序里。核心是一条「捕获—草稿—验证—重建」循环：

1. **prefill 捕获**：调度器把整段 prompt 送目标模型 prefill；若请求符合 `eagle3_prefill_eligible`，车道在 prefill 的同时捕获目标三层的隐藏态，用它们为草稿器整段建 KV，并留下边界特征。
2. **草稿**：调度器发出 `SpeculativeDraft`，车道对每个 ready 请求执行 `chain_round`——从边界特征起跳，链式 rollout γ=3 个 argmax 草稿；verify span 是 `[current_token, draft_1, draft_2, draft_3]`。
3. **验证**：目标模型一次前向吃下 K+1=4 个位置，逐位置选出 committed token，`accept_prefix_match` 取最长匹配前缀 + 奖励 token（u6-l1）。
4. **重建**：`rebuild_after_verify` 按接受前缀对草稿 KV 做 teacher-force 重建，并把边界特征推进到最后一个 committed 位置——下一轮草稿从正确的锚点继续。

而这一切的守门人是 `eagle3_prefill_eligible`：**v1 只支持贪心请求，且 `prefill_prompt` 需要整段 prompt 一次吃下**。不满足的请求照常 prefill/decode，只是不投机。

为什么 eligibility 比共用谓词更严？三个条件各有来历：

- **贪心**：verify 侧的接受规则虽然对采样请求也成立（u6-l1），但 EAGLE-3 的草稿 rollout 是 argmax 链，v1 没有接采样请求的完整通路——先把范围收紧到贪心，保证「已支持的部分严格无损」。
- **整段（`chunk_start == 0` 且 `chunk_tokens == prompt_tokens.len()`）**：`prefill_prompt` 是「整段建草稿 KV」的入口；分块到达的 prompt 只能看到本块的隐藏态，u6-l5 的错位配对 \((f_j, e_{j+1})\) 要求完整序列，v1 没有做跨块续接。
- **`chunk_tokens >= 2`**：错位配对至少需要两个 token 才能产出一对。

#### 4.3.2 核心流程

一次投机请求的完整生命周期（单请求，贪心，prompt 长度 \( P \)）：

```text
┌─ 步骤 A：prefill（含捕获）────────────────────────────────────┐
│ executor.execute_prefill                                       │
│   capture_requested = should_capture_eagle3_prefill_context    │
│   目标前向，同时抓 aux_layers=[1,17,32] 的隐藏态（P 行）        │
│   record_prefill_eagle3_context：                              │
│     eligible? → max_cache_len = min(P + max_out + γ+1, mpe)    │
│                 new_request_state → prefill_prompt 整段建草稿 KV │
│   PrefillResult.draft_context_captured_requests = [req]        │
│ executor 侧：dflash_prefill_action(captured, completed)        │
│   → MarkReady → draft_ready_requests.insert(req)               │
└────────────────────────────────────────────────────────────────┘
          │ （调度门 Speculate，见 4.4）
          ▼
┌─ 步骤 B：投机循环（每轮两个 executor 调用）─────────────────────┐
│ B1 SpeculativeDraft（车道内）                                   │
│    chain_round(state, current_token, γ=3)                      │
│    → DraftResult{ token_ids = [current_token, d1, d2, d3] }    │
│ B2 SpeculativeVerify（目标模型）                                │
│    verify span 4 行 → 逐行选 committed token → 接受前缀+奖励     │
│    truncate_after_terminal → record_verify_eagle3_context：     │
│      rebuild_after_verify 按接受前缀重建草稿 KV、推进边界         │
│    → 提交 1..=4 个 token，回滚拒绝部分的 KV 预留                │
└────────────────────────────────────────────────────────────────┘
          │ （请求结束/退回纯解码/被 abort）
          ▼
   DropDraft → drop_eagle3_request(request_id)  清车道态
```

注意时序中的一个细节：`record_verify_eagle3_context` 发生在 `truncate_after_terminal` **之后**、结果返回 executor 主循环之前——终末截断丢弃的后缀不会泄漏进下一轮草稿上下文。

#### 4.3.3 源码精读

eligibility 谓词：

[src/executor/eagle3_lane.rs:44-53](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L44-L53) 定义 `eagle3_prefill_eligible`：`prefill_capture_supported(req) && req.params.is_greedy() && req.chunk_start == 0 && req.chunk_tokens == req.prompt_tokens.len() && req.chunk_tokens >= 2`。注释直言「v1 is greedy only, and `prefill_prompt` needs the whole prompt in one chunk; anything else decodes without speculation」。

共用的「干净隐藏态」谓词（从 DFlash 专属推广而来，承接 u6-l7）：

[src/executor/dflash_prefill.rs:13-20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L13-L20) 的 `prefill_capture_supported`：无 LoRA、无前缀缓存命中（`cached_tokens == 0`）、无 logprobs、无 prompt_logprobs。函数文档写明它同时服务「DFlash 和 EAGLE-3」。这四个条件共同保证捕获到的目标隐藏态是**干净的**——没有 LoRA 改写、没有缓存跳过的前向、没有为评分而改变的前向形状。

prefill 步骤里的捕获决策与记录：

[src/executor.rs:286-314](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L286-L314)：`capture_requested` 是两种草稿器 should_capture 的或；`capture_layer_ids` 来自 `lane.draft_capture_layer_ids()`（[src/executor.rs:3618-3625](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3618-L3625)，dflash 优先、否则 eagle3 的 aux_layers）；前向完成后按 `lane.eagle3.is_some()` 选择 `record_prefill_eagle3_context` 或 `record_prefill_dflash_context`——又是那一次枚举分派。

[src/executor/eagle3_lane.rs:86-137](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L86-L137) 是 `record_prefill_eagle3_context`：先做协议双向校验（无捕获结果却请求了/有结果却没请求都 bail）；核对捕获行数等于调度 token 数；然后逐请求处理——先 `requests.remove(&req.request_id)` 清掉旧态，eligible 的请求按 `max_cache_len = (prompt + max_output + γ + 1).min(max_position_embeddings)`（「为每个 committed token 加上一整轮越界余量」）建 `Eagle3RequestState` 并调 `prefill_prompt` 整段建草稿 KV；`token_offset` 沿批次推进，把共享的捕获张量切给各请求。

ready 集的三态驱动（executor 侧，方法无关）：

[src/executor.rs:2774-2795](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2774-L2795)：prefill 结果回来后，对每个请求查 `draft_context_captured_requests` 是否包含它，经 `dflash_prefill_action(captured, completed)` 得三态——`MarkReady`（捕获且完成 → 进 `draft_ready_requests`）、`KeepPending`（捕获但还有后续块 → 不进）、`Drop`（没捕获 → 移出 ready 并发 `DropDraft` 清 worker 态）。EAGLE-3 的整段约束意味着它的请求只会走 MarkReady 或 Drop。

草稿执行：

[src/executor/eagle3_lane.rs:140-179](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L140-L179) 是 `execute_eagle3_draft`：ensure 请求列表非空且 `≤ EAGLE3_MAX_BATCH`（车道侧的兜底防御——调度门本不该放行更宽的批，这里防御性再查一次）；对每个请求取 `Eagle3RequestState` 调 `chain_round(&target, state, scratch, req.current_token, EAGLE3_CHAIN_LENGTH)`；返回的 verify span 是 `[current_token, drafts…]`——锚在前，正是 `DraftRequestResult.token_ids` 的契约（[src/speculative.rs:134-139](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L134-L139)）。

verify 步的 EAGLE-3 参数选择：

[src/executor.rs:3988-4006](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3988-L4006)：`execute_draft_verify` 开头按车道选 `(capture_layer_ids, verify_span, max_batch)`——EAGLE-3 分支给 `(aux_layers, EAGLE3_CHAIN_LENGTH + 1, EAGLE3_MAX_BATCH)`；`VerifyGraphBuffers` 按 `max_batch=1`、span=4 预分配（u6-l6 的分段图缓冲）。

验证后的重建：

[src/executor.rs:4082-4104](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4082-L4104)：先对每个结果做 `truncate_after_terminal`（注释明确「在记录目标隐藏态之前应用请求策略，否则被终末处理丢弃的后缀会泄漏进下一轮草稿上下文」），再按 `self.eagle3.is_some()` 分派到 `record_verify_eagle3_context`。

[src/executor/eagle3_lane.rs:183-230](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L183-L230) 是 `record_verify_eagle3_context`：核对请求数与结果数、捕获行数与调度 token 数、id 对齐；然后逐请求调 `rebuild_after_verify(&target, state, captured_hidden, token_offset, &req.token_ids, result.matched_draft_tokens)`——按接受前缀（`matched_draft_tokens` 决定保留多少）teacher-force 重建草稿 KV 并把边界特征推进到最后 committed 位置（u6-l5 已拆解其内部）。

退休清理：

[src/executor/eagle3_lane.rs:232-237](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L232-L237) 的 `drop_eagle3_request`：从 HashMap 移除该请求的草稿态。调用源是 `WorkerCommand::DropDraft`（请求退休、或不eligible/退回纯解码时由 `DFlashPrefillAction::Drop` 与 decode 步的 stale 清理触发，见 [src/executor.rs:2860-2867](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2860-L2867)）。

#### 4.3.4 代码实践

**实践目标**：把 4.3.2 的时序图落到具体行号上，做成一张可复查的「调用点清单」。

**操作步骤**：

1. 对时序图的每个框，抄出「函数 → 文件:行号」：A 框至少 4 个调用点（should_capture、execute_prefill 的 capture_layer_ids 参数、record_prefill_eagle3_context、dflash_prefill_action）；B1 框 2 个（execute_eagle3_draft、chain_round 调用行）；B2 框 3 个（execute_draft_verify 的 eagle3 分支、truncate_after_terminal、record_verify_eagle3_context）。
2. 用 `rg -n "chain_round|prefill_prompt|rebuild_after_verify" src/` 核对 u6-l5 的三个前向入口在本讲的调用行号（应为 [src/executor/eagle3_lane.rs:124](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L124)、[L163-L169](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L163-L169)、[L219-L226](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L219-L226)）。
3. 在图上标注两处「三态动作」的落点：`MarkReady → draft_ready_requests.insert`（[src/executor.rs:2783-2785](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2783-L2785)）与 `Drop → drop_draft_request`（[src/executor.rs:2789-2792](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2789-L2792)）。

**需要观察的现象**：整条链上，调度器看到的数据只有 `PrefillResult.draft_context_captured_requests`、`DraftResult.token_ids`、`VerifyResult.accepted_tokens/matched_draft_tokens`——没有一处出现隐藏态张量的类型。

**预期结果**：一张 10 行左右的调用点清单，能回答「verify 之后、下一轮 draft 之前，草稿态在哪一行被修正」。纯源码阅读，无需 GPU。

#### 4.3.5 小练习与答案

**练习 1**：一个贪心请求的 prompt 长 6000 token，`max_prefill_tokens` 预算 4096，因此被切成两块 prefill。它会被 EAGLE-3 投机吗？走哪个三态动作？

**答案**：不会。第二块的 `chunk_start = 4096 ≠ 0`，且任何一块都不满足 `chunk_tokens == prompt_tokens.len()`（6000 > 4096），两块都不 eligible。第一块 prefill 时 `record_prefill_eagle3_context` 里 `eagle3.requests.remove` 后不重建状态，`draft_context_captured_requests` 不含它 → `dflash_prefill_action(false, completed=false)` = `Drop`，worker 侧草稿态被清；该请求全程纯解码。（这正是 `eagle3_output_budget_edges` 之外另一类「永远不投机」的请求。）

**练习 2**：`record_prefill_eagle3_context` 里 `max_cache_len` 取 `prompt + max_output + γ + 1` 再 `min(max_position_embeddings)`，为什么 γ+1 是「加一整轮」而不是 γ？

**答案**：草稿器要能为一轮投机存下「锚 + γ 个草稿」共 γ+1 个位置的 KV。若只留 γ 个位置，链的最后一步会写越界；锚 token 自身也要在草稿 KV 里占一行（verify span 的第一个位置）。而容量公式 \( k = \min(\gamma,\ L_{max} - C - 1) \)（u6-l5）在 `cached_len` 逼近上限时自动缩短链长，所以 `min(max_position_embeddings)` 封顶之外的越界由链长收缩兜底。

**练习 3**：为什么 `execute_eagle3_draft` 在车道内再查一次 `requests.len() <= EAGLE3_MAX_BATCH`，而不是信任调度门？

**答案**：纵深防御。调度门（4.4）是「应该」层——它依据 `max_draft_batch` 能力位决定整批是否投机；车道是「能够」层——scratch 的物理形状（单 token）决定了它无法服务更宽的批。若未来某个调用方（benchmark、低层直连 API）绕过调度门直接发 `SpeculativeDraft`，车道内的 ensure 会立刻 fail-fast，而不是让单 token scratch 被批数据踩踏后静默产出错误草稿。失败越早越便宜（u1-l3 的同一原则）。

### 4.4 max_draft_batch 门与无损性行为测试

#### 4.4.1 概念说明

能力位只是信息；让它起作用的是调度门。`speculation_decision_at` 是调度器在每个投机机会前的**全有或全无**判定：要么整批投机，要么整批纯解码——绝无「批内一半投机一半解码」的混合步。三态判定按顺序检查：

1. 草稿器装载且批非空，否则 `NotReady`；
2. **批宽门**：`max_draft_batch` 若为 `Some(limit)` 且 `active.len() > limit` → `NotReady`（本讲新增关注点：EAGLE-3 报 `Some(1)`，所以批宽 ≥2 直接纯解码）；
3. **逐请求就绪**：每个请求都在 ready 集中、无 LoRA、无 logprobs，否则 `NotReady`；
4. **负载门**：`PEGAINFER_SPEC_MAX_BATCH` 生效且批宽达到阈值 → `Gated`（单 token verify 保住捕获），否则 `Speculate`。

注意宽批退回 `NotReady`（纯解码）而不是 `Gated`（单 token verify）的原因：`Gated` 的语义是「请求还 ready，只是负载高，用单 token verify 维持草稿上下文等待恢复」；而宽批下 EAGLE-3 无法为所有请求服务，单 token verify 也没意义——干脆纯解码。捕获丢了就丢了：该请求下次独立 prefill 机会（新请求）会重新捕获。已捕获的请求在宽批期间被 decode 步的 stale 清理移出 ready 集（[src/executor.rs:2860-2867](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2860-L2867)：decode 步推进了序列，旧捕获作废）。

**为什么回退纯解码仍无损**：投机解码的输出无损性来自「每个 committed token 都是目标模型自己的选择」（u6-l1）。纯解码每步由目标 argmax 选 token，本来就是无损的基准路径；投机只是把多个位置的 argmax 合并进一次前向。回退纯解码 = 放弃加速、保住正确性——这正是门控的职责边界。

行为测试侧，`tests/eagle3_speculative_gate.rs` 的四组测试把这条边界钉死：贪心等价、输出预算边界、中断后恢复、重叠回退。它们共享 `tests/common/lossless.rs` 的判定：逐 token 对比 spec 与 baseline，遇到分歧时不立即判负，而是用**基线自己的 top-K 分布**计算 regret——spec 所选 token 距基线 argmax 的 logprob 差 ≤ `MARGIN_TOL = 0.20` 即视为良性平局翻转。这个容差要同时服务 DFlash 与 EAGLE-3 两个门测试，因为两者的 verify 都跑 prefill 注意力核（span 前向），而 baseline 跑 decode 核——两核在真实平局处分岔约 1 个 bf16 ULP。

#### 4.4.2 核心流程

`speculation_decision_at` 的判定阶梯（伪代码）：

```text
fn speculation_decision_at(executor, active, max_batch) -> Decision:
    if !executor.speculative_enabled() || active.is_empty():
        return NotReady                      # 没装草稿器 / 空批
    if executor.max_draft_batch().is_some_and(|limit| active.len() > limit):
        return NotReady                      # ★ 批宽门：EAGLE-3 时 limit=1
    all_ready = active.iter().all(|req|
        executor.speculative_request_ready(req.id)   # 捕获过且未作废
        && req.lora_adapter.is_none()                # LoRA 不投机
        && req.logprobs.is_none())                   # logprobs 不投机
    if !all_ready:
        return NotReady                      # 逐请求门
    if max_batch > 0 && active.len() >= max_batch:
        return Gated                         # 负载门：单 token verify
    return Speculate                         # 全绿：draft + verify
```

`check_lossless` 的判定阶梯：

```text
matched = base 与 spec 的最长公共 token 前缀长度
if matched == min(len(base), len(spec)):
    长度相等 → 100% 无损（Ok）；长度不等 → 报错（前缀相同但续写不同长）
在分歧点 matched：
    用（投机）引擎对 prompt + base[..matched] 做一次 prompt-scoring prefill，
    得到 prefill 核的参考分布 prefill_ref
    if prefill_ref.id == spec[matched]:
        Ok —— 分歧纯粹是 prefill 核 vs decode 核的既有差距
    regret = prefill_ref 中 argmax 的 logprob − spec 所选 token 的 logprob
    if regret 存在且 ≤ MARGIN_TOL(0.20):
        Ok —— 良性平局翻转
    else:
        Err —— 真 bug（spec 选了前向从未偏好的 token）
```

#### 4.4.3 源码精读

调度门本体：

[src/scheduler/plan.rs:192-219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L192-L219) 是 `speculation_decision_at` 全文。第 200-205 行就是批宽门：`executor.max_draft_batch().is_some_and(|limit| active.len() > limit)` → `NotReady`。函数文档（[L186-L191](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L186-L191)）强调全有或全无、采样参数不是门（sampled-verify 让全部采样面走投机路径）；`SpeculationDecision` 枚举（[L173-L184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L173-L184)）给 `NotReady` 的文档明确列出三种成因：「有请求未就绪 / 批宽超过草稿器服务上限 / 未装草稿器」。

批宽门的单测：

[src/scheduler/plan.rs:418-430](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L418-L430) `a_batch_wider_than_the_drafter_serves_decodes_plainly`：`FakeExecutor` 加 `with_max_draft_batch(1)` 后，批宽 1 → `Speculate`，批宽 2 → `NotReady`。这正是 EAGLE-3 形状在 CPU 上的复现——无需 GPU 就锁定了判定顺序（批宽门在逐请求就绪检查**之前**，见 L200 与 L206 的先后）。

FakeExecutor 的能力位注入：

[src/scheduler/test_support.rs:194-197](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L194-L197) 是 `with_max_draft_batch` builder；[L318-L320](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L318-L320) 是其 trait 实现——替身与真身走同一条 trait 缝，测试锁的就是真实现的契约。

EAGLE-3 门测试的组件与流程：

[tests/eagle3_speculative_gate.rs:82-109](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L82-L109) 的 `launch` 直接走 `pegainfer_qwen3::launch` 组装 `Qwen3LaunchOptions`——`eagle3_draft_model_path` 塞 `Some`，基线则传 `None` 且 `no_prefix_cache: true`（注释：投机引擎强制关前缀缓存，基线也关，两边走同一条冷 prefill 路径，避免缓存差异污染对比）。

四组测试：

- [tests/eagle3_speculative_gate.rs:179-228](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L179-L228) `eagle3_greedy_matches_plain_greedy`：5 条 prompt，基线贪心 vs 投机贪心，先断言 `proposed > 0 && accepted > 0`（**防静默退化为纯解码**——投机引擎从不草稿也算「输出正确」，但那是没有覆盖投机路径的假通过），再逐条 `check_lossless`。
- [tests/eagle3_speculative_gate.rs:233-260](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L233-L260) `eagle3_output_budget_edges_match_plain_greedy`：预算取 `[1,2,3,4,5,6,9]`——调度器把 verify span 夹到剩余预算，使请求以从 1 到全链的各种 span 长度收尾，`rebuild_after_verify` 在每种长度上都被执行到。
- [tests/eagle3_speculative_gate.rs:264-314](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L264-L314) `eagle3_abort_mid_speculation_keeps_the_engine_lossless`：受害请求 `ignore_eos` + 8192 预算，产 16 token 后 abort；断言引擎 2 秒内归零、之后新请求仍草稿且无损——DropDraft 清理路径的行为门。
- [tests/eagle3_speculative_gate.rs:319-379](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L319-L379) `eagle3_overlapping_requests_fall_back_and_stay_lossless`：第一个请求先独跑 8 token（`proposed > 0`），第二个加入后取一次 `metrics()` 快照断言 `num_running_reqs == 2`；随后断言重叠期间 `proposed == 0`（**宽批回退纯解码的可观测证据**：计数器不再增长）且两路输出都过 `check_lossless`。这一个测试同时钉住了 4.4.1 的两个论断：批宽 2 > limit 1 → 不草稿；回退后输出仍与基线一致。

共享的无损判定：

[tests/common/lossless.rs:21-28](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L21-L28) 定义 `LOGPROBS = 20`（基线请求的 top-K 宽度，保证真实平局时 spec 的选择落在集合内）与 `MARGIN_TOL = 0.20`（「典型 logit 量级下约 3 个 bf16 ULP，与 `hf_golden_gate` 的 MARGIN_TOL 呼应」）。文件头注释（[L1-L8](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L1-L8)）说明动机：verify 走 prefill 核、baseline 走 decode 核，两核差约 1 个 bf16 ULP，精确相等会在良性平局翻转上伪失败。

[tests/common/lossless.rs:74-84](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L74-L84) 的 `prefill_next`：用 prompt-scoring（`prompt_logprobs`）请求对共享上下文做一次 prefill，取出 **prefill 核**产出的下一 token 分布——这是 spec 所选 token 应当对齐的参考，因为 verify 路径跑的就是 prefill 核。

[tests/common/lossless.rs:93-216](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L93-L216) 的 `check_lossless` 主体：算 matched 前缀；前缀全同但长度不同 → Err（匹配前缀不能证明续写一致）；分歧点先打诊断日志（上下文解码、两边 token 序列）；然后三级判定——`prefill_ref.id == spec_id` 直接 Ok（[L162-L176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L162-L176)）；否则算 prefill 分布里的 regret，`≤ MARGIN_TOL` 视为良性平局（[L184-L201](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L184-L201)，注释解释第二层容差存在的原因：verify 逐 span 增量建 committed KV，参考 prefill 一次建成，两者差几个 ULP）；都不满足 → Err 报「真 spec bug」并附双分布的 regret（[L203-L216](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L203-L216)）。因为判定只依赖「committed token 是否出自目标前向偏好的分布」，它对 DFlash 与 EAGLE-3 一视同仁——**margin 容差服务的是「投机机制」而非某种草稿器**。

#### 4.4.4 代码实践

**实践目标**：(a) 推演 batch=2 时 `NotReady` 的判定路径；(b) 用真实测试文件核对「重叠回退仍无损」的证据链。

**操作步骤**：

1. **纸面推演**：构造 `FakeExecutor::new(64, ...).with_speculative().with_max_draft_batch(1)`（模拟 EAGLE-3 的 `speculative.is_some() + max_draft_batch=Some(1)`），`active` 为两个全就绪请求（无 LoRA、无 logprobs、都在 ready 集），`max_batch = 0`（负载门关）。走一遍 [src/scheduler/plan.rs:197-218](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L197-L218)：
   - L197：`speculative_enabled()` 真、批非空 → 过；
   - L200-205：`max_draft_batch() = Some(1)`，`active.len() = 2 > 1` → **返回 `NotReady`**。逐请求就绪检查（L206）根本没执行。
2. **运行单测**（无需 GPU）：
   ```bash
   cargo test --release -p pegainfer-qwen3 --lib a_batch_wider_than_the_drafter_serves
   cargo test --release -p pegainfer-qwen3 --lib speculation_decision
   ```
3. **核对证据链**：通读 [tests/eagle3_speculative_gate.rs:319-379](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L319-L379)，列出它用来证明「回退发生」与「回退无损」的两类断言各依赖哪个计数器/比较。
4. **写分析**（约 200 字）：为什么第二个请求加入后，第一个请求已捕获的草稿态不会让输出出错？

**需要观察的现象**：单测通过；`eagle3_overlapping_requests_fall_back_and_stay_lossless` 中 `proposed == 0` 的断言紧跟在 `num_running_reqs == 2` 的快照之后——「回退发生」由计数器停增证明，「无损」由 `check_lossless` 对两路输出证明。

**预期结果**：推演结论——batch=2 时在批宽门（L200-205）即返回 `NotReady`，早于逐请求就绪检查；分析要点：回退纯解码后每步 committed token 仍是目标 argmax（decode 核），与基线同核同路径；第一个请求的旧草稿态最多造成一次「浪费的捕获」，其 KV 预算按事务回滚（u6-l1），stale 捕获被 decode 步清理（[src/executor.rs:2860-2867](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2860-L2867)），从不参与 committed token 的选择，故无损。步骤 2 的单测在本机可直接验证（无 GPU 要求）；步骤 3 的集成测试需要 GPU + 权重，**待本地验证**（缺权重时按 `paths_or_skip` clean skip）。

#### 4.4.5 小练习与答案

**练习 1**：`eagle3_overlapping_requests_fall_back_and_stay_lossless` 为什么要用**一次** `engine.metrics()` 快照同时断言 `num_running_reqs == 2` 并取 spec 计数器，而不是两次分别读？

**答案**：见 [tests/eagle3_speculative_gate.rs:351-360](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L351-L360) 的注释「One snapshot for both checks, so no step can land between them」：引擎是异步驱动的，两次读之间可能插入新步——比如第二个请求恰好在两次读之间完成，批宽回到 1，投机恢复，`proposed` 又开始增长，于是「重叠期间不草稿」的断言就会在错误的时间窗上做。一次快照把两个观测原子化，钉住「批宽为 2 的那一刻计数器是这个值」。

**练习 2**：`check_lossless` 的第一级容差（`prefill_ref.id == spec_id` 即 Ok）与第二级（regret ≤ 0.20）分别吸收什么误差？为什么基线（decode 核）不能直接当参考？

**答案**：第一级吸收「prefill 核与 decode 核在真实平局上的分岔」——spec 的 verify 跑 prefill 核，若它忠实地复现了 prefill 核自己的贪心选择，分歧纯粹是基线 decode 核把平局解到了另一边。第二级吸收「verify 的增量 KV 与参考 prefill 的一次成型 KV 之间的几个 ULP」——两者都走 prefill 核，但 committed KV 一个逐 span 增量构建、一个整段构建，浮点归约顺序不同。基线 decode 核不能当参考，因为它与 verify 路径**不同核**：文件头注释（[tests/common/lossless.rs:69-73](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/common/lossless.rs#L69-L73)）明说 decode 核「会把平局解到另一边并放大差距」，用它做参考会把良性翻转误判为 bug。

**练习 3**：门测试为何坚持 `assert!(proposed > 0 && accepted > 0)`？去掉这条断言测试会变弱在哪里？

**答案**：无损性检查只对比「投机引擎的输出」与「基线输出」；若调度门有 bug 导致 EAGLE-3 引擎从不投机（比如批宽门写反成 `active.len() < limit`），输出仍会 100% 一致——测试「通过」却完全没有覆盖草稿/verify/重建路径。`proposed > 0` 证明草稿确实发生，`accepted > 0` 证明接受规则确实执行；加上 `eagle3_overlapping_requests_fall_back_and_stay_lossless` 里反向的 `assert_eq!(proposed, 0)`，正反两面把「何时投机、何时不投机」都钉在计数器上。这与门测试文件头（[tests/eagle3_speculative_gate.rs:1-9](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/tests/eagle3_speculative_gate.rs#L1-L9)）声明的「a run that should draft fails when it silently decodes plainly」完全一致。

## 5. 综合实践

**任务：为 EAGLE-3 车道写一份「一页纸运行手册」，并用 CPU 单测验证其中的门控行为。**

1. **分派链小节**：用 `rg -n "DraftModel::|LoadEagle3|DropDraft" src/` 抄出三层分派表（launch 折叠 → executor `load_draft_model` → worker 命令），每行标注文件:行号与所属层。
2. **时序图小节**：把 4.3.2 的「捕获 → chain_round → verify → rebuild」时序图重画成你自己的版本，每个箭头标上调用点行号（`record_prefill_eagle3_context` 的 `prefill_prompt` 调用、`execute_eagle3_draft` 的 `chain_round` 调用、`record_verify_eagle3_context` 的 `rebuild_after_verify` 调用），并在三个入口旁标注它们的关键前提（eligibility 五条件、`≤ EAGLE3_MAX_BATCH` 防御、`truncate_after_terminal` 先于记录）。
3. **门控小节**：写出 batch=2 的 `NotReady` 判定路径（哪一行返回、哪些检查被跳过），并实际运行：
   ```bash
   cargo test --release -p pegainfer-qwen3 --lib a_batch_wider_than_the_drafter_serves_decodes_plainly -- --nocapture
   ```
   记录输出。
4. **无损性论证小节**：引用 `check_lossless` 的两级容差与 `MARGIN_TOL = 0.20`，写 5~8 句话论证「重叠请求回退纯解码仍无损」——重点说明 committed token 的来源（目标 argmax）与旧草稿态的处置（KV 事务回滚 + stale 捕获清理）。
5. **（可选，需 GPU + `AngelSlim/Qwen3-4B_eagle3` 权重）**：运行
   ```bash
   PEGAINFER_TEST_MODEL_PATH=models/Qwen3-4B \
   PEGAINFER_EAGLE3_TEST_MODEL_PATH=models/Qwen3-4B_eagle3 \
   cargo test --release -p pegainfer-qwen3 --test eagle3_speculative_gate -- --nocapture
   ```
   对照 eprintln 的 `rounds/proposed/accepted/per_position` 输出，验证第 2、3 小节的时序与门控在真实引擎上的表现。无 GPU 时此步标注「待本地验证」即可。

产出物是一页 Markdown；它同时是本讲的复习提纲与后续调试 EAGLE-3 问题时的速查表。

## 6. 本讲小结

- **`DraftModel` 枚举三层分派**：launch 层把两条 CLI 草稿路径折叠成互斥单槽（`lib.rs` 的 match），executor 层 `load_draft_model` 按变体发 `WorkerCommand::LoadDflash/LoadEagle3`，worker 层 `LocalQwen3Lane` 的 `dflash`/`eagle3` 是互斥槽位；`DropDraft` 无条件清两边，类型信息只在装载期 match 一次。
- **`Eagle3LaneState` 与 `DFlashLaneState` 同构**：草稿模型 + 每 request 状态 + 复用 scratch，住在 worker 线程上；隐藏态不出车道，草稿/验证边界是纯 token。装载返回的 `DraftMeta` 把物理限制（`EAGLE3_MAX_BATCH = 1`、γ=3、span=4）铸成调度器可见的能力位 `max_draft_batch: Some(1)`。
- **`eagle3_prefill_eligible` 五条件**：共用的干净隐藏态谓词（无 LoRA、无缓存命中、无 logprobs/prompt_logprobs）+ 贪心 + 整段（`chunk_start == 0` 且全量）+ 至少 2 token；不满足的请求照常服务，只是不投机。
- **草稿循环时序**：prefill 捕获目标三层隐藏态（`[1, N/2-1, N-4]`）整段建草稿 KV → 三态动作驱动 `draft_ready_requests` → `chain_round` 产出 `[current, d1..d3]` → 目标 verify（truncate 先于记录）→ `rebuild_after_verify` 按接受前缀重建；请求退休走 `DropDraft`。
- **批宽门**：`speculation_decision_at` 在逐请求就绪检查**之前**先查 `max_draft_batch`——批宽 2 对 EAGLE-3 即 `NotReady` 整批纯解码；回退无损因为 committed token 恒为目标自己的选择，旧草稿态经 KV 事务回滚与 stale 清理处置。
- **共享无损判定**：`check_lossless` 以 prefill 核分布为参考、两级容差（同 argmax 直接 Ok；regret ≤ `MARGIN_TOL = 0.20` 视为良性平局），机制层面方法无关，同一份代码同时钉住 DFlash 与 EAGLE-3 的门测试；`proposed/accepted` 计数器保证「应投机而未投机」也算失败。

## 7. 下一步学习建议

本讲补完了投机解码单元的最后一块拼图。建议：

1. **收尾验证**：跑 `cargo test --release -p pegainfer-qwen3 --lib` 把 u4-l2、u6-l7 到本讲涉及的调度单测整体过一遍；有 GPU 时按第 5 节的可选步骤跑四组 EAGLE-3 门测试，观察 `per_position` 接受长度分布。
2. **下一单元**：进入 u7（LoRA 服务）——与本讲对照阅读尤其有收获：LoRA 与草稿器同样「独占 GPU 同段资源」，但一个走热加载注册表（u7-l2），一个走启动期装载；两者在 `speculation_decision_at` 的逐请求门里作为互斥条件再次相遇。
3. **源码延伸**：若想继续深挖投机栈，读 `src/executor/auto_hedge.rs`（u6-l7 的 explore-then-commit 控制器）并思考：EAGLE-3 的 `DraftStepItem.hedge_budget` 语义（[src/speculative.rs:107-117](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L107-L117)）在 EAGLE-3 车道里为何未被使用（`chain_round` 不接收它），这预示 EAGLE-3 的 hedge 尚未接线。
