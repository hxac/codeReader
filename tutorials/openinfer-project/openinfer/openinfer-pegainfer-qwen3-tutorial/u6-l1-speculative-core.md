# 投机解码事务核心：propose/verify/accept

## 1. 本讲目标

学完本讲，你应该能够：

- 把投机解码复述成一个**对 decode 循环的乐观并发事务**：propose（提出 K 个草稿）→ verify（一次目标前向验证 K+1 个位置）→ accept（接受最长匹配前缀 + 1 个奖励 token）→ commit/rollback（KV 提交或回滚）。
- 解释贪心请求与采样请求为何共用同一个 `accept_prefix_match` 接受规则，而且不破坏输出分布。
- 理解草稿/验证边界为什么是一条**纯 token 跨界**——proposer 的隐藏态永远不越过这条线。
- 说出 `DraftModel` 枚举（DFlash / Eagle3）如何按 checkpoint 路径选择草稿器，以及两个 proposer 如何共享目标隐藏态捕获、`draft_ready_requests` ready 集与同一个 KV 事务，只差 worker 车道。

## 2. 前置知识

**投机解码（speculative decoding）的直觉。** 大模型一次 decode 步只产出一个 token，GPU 大量算力花在小批前向上，利用率很低。投机解码让一个便宜得多的「草稿器」（drafter）先一口气猜 K 个 token，然后目标模型用**一次**前向同时验证这 K 个猜再加上后续位置（共 K+1 个位置），把目标模型同意的前缀一口气提交。猜得准，一步就「白拿」多个 token；猜错了，提交的也仍是目标模型自己的答案——输出永远由目标模型裁定。

**乐观并发事务的类比。** 就像数据库里「先预留资源、乐观执行、验证后提交或回滚」：KV cache 先为完整的 K+1 span 预留块，前向算完后只提交被接受前缀的 KV，其余块归还池子；任何一步失败则整体回滚预留。本讲的 `executor/spec.rs` 就是这个事务的宿主。

**前缀匹配接受规则。** 记草稿为 \( d_1, \dots, d_K \)，目标模型在 K+1 个验证位置各产出一个「committed token」\( t_0, \dots, t_K \)（贪心请求是 argmax，采样请求是从目标分布采的样本）。接受长度 \( n \) 是最大的满足 \( d_i = t_{i-1} \)（对所有 \( i \le n \)）的前缀长度，提交序列为 \( d_1, \dots, d_n, t_n \)——要么是首个分歧处的修正 token，要么是全对时的奖励续写 token，长度恒在 \( 1 \) 到 \( K+1 \) 之间。设每个草稿独立命中概率为 \( p \)，则每轮提交 token 数的期望为：

\[
E[\text{committed}] = \sum_{i=0}^{K} p^i = \frac{1 - p^{K+1}}{1 - p}
\]

即使全猜错（\( p = 0 \)）也保底提交 1 个 token，所以投机解码最坏也只是「多花了一次草稿前向」。

**本讲在整册中的位置。** u3-l4 讲过 decode 批前向，u4-l2 讲过调度器的投机门控（`speculation_decision_at` 与 `max_draft_batch` 批宽门）。本讲深入两者之间的**方法无关核心**：不管草稿器是 DFlash（u6-l2）还是 EAGLE-3（u6-l5），验证、接受与 KV 事务都是本讲这一份代码。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/speculative.rs` | 方法无关核心：`DraftModel` 枚举、verify span 数据结构（`VerifyStepItem`/`VerifyPlan`/`VerifyRequestResult`）、draft 数据结构（`DraftStepItem`/`DraftPlan`）、接受规则（`accept_prefix_match`/`num_accepted`/`build_verify_results`）及其单测 |
| `src/executor/spec.rs` | 执行器侧的投机编排：乐观 KV 事务（schedule → forward → 校验 → commit/rollback）、终末截断 `truncate_after_terminal`、草稿派发的薄封装 |
| `src/executor.rs`（节选） | `DraftMeta` 与 `draft_ready_requests` 的归属、`load_draft_model` 的双分派、prefill 后 ready 集折入、verify 车道分派（dflash vs eagle3） |
| `src/executor/dflash_prefill.rs` | 捕获资格谓词与 prefill 后三态动作（`DFlashPrefillAction`），DFlash 与 EAGLE-3 共用 |
| `src/lib.rs`（节选） | `launch` 把两条草稿路径折叠成互斥的单槽 `Option<DraftModel>` |
| `../pegainfer-kv-cache/src/pool.rs`（节选） | KV 事务的四个底层原语：`schedule_speculative` / `speculative_view` / `apply_speculative` / `revert_schedule` |

## 4. 核心概念与源码讲解

### 4.1 VerifyStepItem 与纯 token 跨界

#### 4.1.1 概念说明

投机系统里有两个角色：**proposer**（草稿器，便宜、快）和 **verifier**（目标模型，慢但权威）。它们之间必须有一条清晰的接缝——本项目的选择是：接缝上只流动 **token id**，不流动隐藏状态。`VerifyStepItem` 就是这条接缝的载体：一个请求的验证 span，由「当前已确认的悬空 token + K 个草稿候选」组成，纯 token 构造。

为什么隐藏态不能跨界？因为 proposer 的隐藏态属于它自己的体系结构（DFlash 的 fc 层缓存、EAGLE-3 的 midlayer cache），一旦泄漏到验证侧，目标执行器就要理解每种草稿器的内部几何，方法无关性立刻瓦解。token 是两个体系唯一的公共语言。

#### 4.1.2 核心流程

一个投机步在 executor 与 worker 之间的数据流：

```
调度器 (u4-l2: speculation_decision_at 放行)
   │ VerifyPlan { requests: &[VerifyStepItem], sample_seed, gated }
   ▼
executor/spec.rs: execute_speculative_verify_impl
   │ 预留 KV → StepCommand::SpeculativeVerify（携带 KvView）
   ▼
worker 车道 (dflash_lane / eagle3_lane 之外的共享验证代码)
   │ 目标 batch_prefill_into 一次前向 K+1 行
   │ select_step_tokens: 每行产出一个 committed token（argmax 或采样）
   │ build_verify_results: 逐请求接受 → truncate_after_terminal 归一化
   │ 按 eagle3/dflash 车道分别记录捕获的目标隐藏态
   ▼
VerifyResult → executor 提交/回滚 KV → 调度器 resolve
```

#### 4.1.3 源码精读

模块文档开宗明义地定义了这条边界——两个 proposer 的隐藏态都留在 proposer 内部，且都从 prefill/verify 捕获的目标隐藏态出发播种，共享捕获、ready 集与事务，只有把捕获变成草稿的 worker 车道不同：

- [src/speculative.rs:L1-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L1-L25) —— 模块级文档：投机解码是 decode 循环上的乐观并发事务（propose K、verify K+1、accept 最长前缀+1、commit/rollback）；draft/verify 边界是纯 token span，DFlash 与 EAGLE-3 共享验证、接受与 KV 事务。
- [src/speculative.rs:L40-L50](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L40-L50) —— `VerifyStepItem`：`token_ids[0]` 是已确认的最后一个 token，`token_ids[1..]` 是 K 个草稿；结构体里除 token 外只有 `params`（采样参数，verify 侧每行按各自请求的参数选 token）和 `stop_policy`（终末截断用），没有任何张量或隐藏态字段。
- [src/speculative.rs:L72-L83](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L72-L83) —— `VerifyPlan`：请求切片 + 每步种子 + `gated` 标志。`gated` 单独携带是因为「span 只有一个 token」可能有两个原因——负载门关闭了草稿（gated=true），或请求逼近输出预算被 span 钳制；只有这个标志说明了「为什么本轮没有草稿」，后面计数器据此不把门控轮记作草稿。
- [src/speculative.rs:L102-L117](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L102-L117) —— `DraftStepItem`：草稿请求只带 `current_token`（proposer 从它续写）与 `hedge_budget`（对冲链预算），**故意不带采样参数**——草稿 rollout 永远是 argmax，采样只发生在 verify 侧。这是「跨界纯 token」在 draft 方向的镜像。
- [src/speculative.rs:L85-L96](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L85-L96) —— `VerifyRequestResult`：`accepted_tokens` 恒为 `1..=K+1` 个 token（接受前缀 + 目标 posterior token，或提前被终末 token 截断）；`matched_draft_tokens` 是终末截断后保留的命中草稿数，专供接受率计数。

#### 4.1.4 代码实践

**实践目标**：用「字段清单」验证纯 token 边界。

1. 通读 `VerifyStepItem`、`DraftStepItem`、`VerifyRequestResult`、`DraftRequestResult` 四个结构体的全部字段。
2. 逐字段标注：这个字段是 token、标量配置，还是（假想中的）张量/隐藏态？
3. 运行单测确认这组纯数据结构的行为（无 GPU 也能跑，属 `--lib` 单测；预期结果**待本地验证**）：

```bash
cargo test --release -p pegainfer-qwen3 --lib speculative
```

**需要观察的现象**：四个结构体没有任何 `Tensor`、`CudaSlice`、hidden state 之类字段；测试输出里 `speculative::tests` 模块的 8 个用例与 `executor::spec::tests` 的 1 个用例全部通过。

**预期结果**：你会得出结论——跨界载体只有 `Vec<u32>` 的 token 和少量标量，proposer 内部状态（DFlash 的层缓存、EAGLE-3 的 `last_aux_hidden_states`）分别留在 `src/executor/dflash_lane.rs` 与 `src/executor/eagle3_lane.rs` 里。

#### 4.1.5 小练习与答案

**练习 1**：如果允许草稿器把隐藏态放进 `VerifyStepItem`，最先坏掉的是什么？

**答案**：方法无关性。验证侧的目标前向代码将不得不理解每种草稿器的隐藏态几何（维度、层号、归一化方式），新增一个 proposer 就要改动共享验证路径；同时 CUDA Graph 捕获的缓冲形状也会随草稿器种类膨胀。纯 token 边界让验证、接受、KV 事务与草稿器种类完全解耦——这正是本次提交能直接新增 EAGLE-3 而不动 `speculative.rs` 接受逻辑的原因。

**练习 2**：`VerifyPlan.gated` 为什么不并入 span 长度，而要单独一个布尔？

**答案**：见 [src/speculative.rs:L78-L82](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L78-L82) 的注释：逼近预算的请求 span 同样只有一个 token，但那是预算钳制而非「本轮没有草稿」；只有 `gated` 说明了原因，[src/executor/spec.rs:L195-L205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L195-L205) 据此跳过对门控轮的草稿计数（否则会虚增草稿数、稀释公布的接受率）。

### 4.2 accept 前缀规则：贪心与采样统一

#### 4.2.1 概念说明

接受规则是投机解码的灵魂。本项目只用一条规则服务**所有**请求：verify 前向在 K+1 个位置各产出一个 committed token——贪心请求是融合 argmax，非贪心请求是按目标分布的常规逐行采样——接受的前缀就是「与这些 committed token 相同的草稿前缀」。

采样请求为什么也无损？关键在于 committed token 的来源：它们本来就是目标分布在各位置的样本。接受判定只是决定「多少个已经从目标分布采出的 token 搭同一班车」，不改变任何被提交 token 的分布来源。模块文档（#512 采样验证机制，GLM5.2 在 #589 落地）明确：对确定性（one-hot）草稿，这就是无损投机采样。

#### 4.2.2 核心流程

```
输入: proposed = [d_1..d_K]        # K 个草稿
      target_tokens = [t_0..t_K]   # K+1 个 committed token
n ← 0
while n < K and d_{n+1} == t_n: n += 1     # 最长前缀
committed ← [d_1..d_n] ++ [t_n]            # 修正或奖励，1..=K+1 个
```

批量版 `build_verify_results` 把一次前向拍平的 committed token 列按请求顺序切成每请求的 K+1 列，逐请求套用同一规则，最后检查列恰好用尽（不多不少）。

#### 4.2.3 源码精读

- [src/speculative.rs:L145-L176](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L145-L176) —— `accept_prefix_match`：共享接缝。`debug_assert` 检查 verify 恰好多产出一列；先取 `num_accepted`，再拼接 `proposed[..n]` 与 `target_tokens[n]`（首个分歧处的修正，或全对时的奖励续写）。注释里说明了 `n <= proposed.len() < target_tokens.len()`，所以索引恒有效。
- [src/speculative.rs:L178-L186](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L178-L186) —— `num_accepted`：朴素的逐位前缀计数，即上面伪代码的 while 循环。
- [src/speculative.rs:L188-L227](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L188-L227) —— `build_verify_results`：批量接受。每个请求的 span 非空检查、列偏移越界检查、末尾「列恰好用尽」检查；`proposed = token_ids[1..]`（span 减去打头的已确认 token），`posterior = target_tokens[offset..end]`，两者在 `accept_prefix_match` 里汇合；`matched_draft_tokens = accepted_tokens.len() - 1`。
- [src/executor.rs:L4069-L4081](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4069-L4081) —— worker 验证侧：`select_step_tokens` 让 span 的每一行按**其所属请求**的参数选 token——argmax 行走融合路径，采样行走常规批量采样器（sampled-verify，#512）。这就是「贪心与采样统一」的生产端：committed token 的语义对两类请求同构。
- [src/speculative.rs:L229-L323](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L229-L323) —— 单测：全对加奖励（`accepts_full_run_plus_bonus`）、前缀后修正（`accepts_prefix_then_correction`）、首草稿即错只提交一个（`rejects_first_candidate_commits_one`）、空提案提交模型 token（`empty_proposal_commits_model_token`）、批量多请求按 span 切列（`batched_multi_request_splits_columns_by_span`）等 8 例。

#### 4.2.4 代码实践

**实践目标**：亲手跑通并注释接受规则的全部单测。

1. 运行（无 GPU 可跑，预期结果**待本地验证**）：

```bash
cargo test --release -p pegainfer-qwen3 --lib speculative -- --nocapture
```

2. 对每个用例写一行注释，格式如：「`accepts_prefix_then_correction`：proposed=[10,11,99]，argmax=[10,11,22,33] → n=2 → 提交 [10,11,22]；验证修正 token 来自分歧位置 t_2=22」。
3. 手工构造一个不在测试里的例子：proposed=[5,6,7]，target_tokens=[6,6,7,8]，先纸面推导 n 与提交序列，再写一个临时用例（**示例代码**，练习用，不要提交到仓库）验证你的推导。

**需要观察的现象**：`n` 恰是「逐位相等」的前缀长度；提交序列长度 = n+1；`matched_draft_tokens` = 提交长度 - 1。

**预期结果**：纸面推导与测试断言一致；你构造的例子中 n=1（d_1=5 ≠ t_0=6），提交 [6]——第一个草稿就错时只提交目标自己的 token。

#### 4.2.5 小练习与答案

**练习 1**：经典投机采样（Leviathan et al.）用「拒绝采样修正分布」保证随机草稿的无损性，本项目为什么不需要？

**答案**：因为草稿是确定性的（one-hot）——草稿 rollout 恒为 argmax（见 `DraftStepItem` 不带采样参数，[src/speculative.rs:L102-L106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L102-L106)），而被提交的每个 token 都由目标分布直接产生（argmax 或目标采样）。接受判定只决定「几个目标样本同乘一步」，不改变任何被提交 token 的分布，因此无需修正分布。模块文档（[src/speculative.rs:L18-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L18-L25)）把它表述为「对确定性草稿的无损投机采样」。

**练习 2**：若 `build_verify_results` 收到的 `target_tokens` 比「所有 span 之和」长，会发生什么？

**答案**：末尾的 `ensure!(offset == target_tokens.len(), "unused speculative target-token result columns ...")`（[src/speculative.rs:L221-L225](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L221-L225)）失败并报错——多列意味着前向产出的 committed token 与 span 布局不符，属于协议破损，宁可硬失败也不静默忽略。

### 4.3 KV 事务：executor/spec.rs 的乐观提交

#### 4.3.1 概念说明

验证前向要为完整 K+1 span 写 KV，但只有被接受前缀的 KV 能留下。`executor/spec.rs` 把这件事组织成一个事务：

- **预留（schedule）**：每请求按 span 长度预留 KV 块，任一失败则回滚此前全部预留——全有或全无。
- **执行（forward）**：构造 `StepCommand::SpeculativeVerify` 发给 worker；失败回滚全部预留。
- **校验（validate）**：结果条数、请求 id 一一对应、worker 已做终末截断（否则拒绝在任何 KV 提交前）。
- **提交（commit）**：逐请求 `apply_speculative(accepted_tokens)`——保留接受前缀的 KV，LIFO 释放其余草稿块；提交中途失败时只回滚**尚未提交**的请求（已提交者无法撤回）。
- **归一化（truncate）**：在第一个请求终末 token（stop token / EOS）后截断提交序列，保证不越过后停止边界；scheduler 稍后对同一触发 token 分类，产出协议层的 typed stop cause。

#### 4.3.2 核心流程

```
execute_speculative_verify_impl(plan):
  verify_round += 1                                   # 单调轮次号，诊断相关
  前置检查: 草稿器已装载 / span 非空 / 每请求 ∈ draft_ready_requests / RequestKv 存在
  scheduled = []
  for req in plan.requests:
      rkv.schedule_speculative(span_len)              # 预留整段 K+1
      失败 → revert(scheduled) → Err                  # 全有或全无
      scheduled.push(req)
  step = SpeculativeVerify { requests, kv_views(speculative_view), sample_seed, round, gated }
  outcome = run_step(step)  失败 → revert(scheduled) → Err
  校验: 条数一致 / id 对齐 / 无未截断的终末 span  → 违反则 revert(scheduled) → Err
  applied = []
  for r in result.requests:
      rkv.apply_speculative(r.accepted_tokens)        # 提交前缀，LIFO 释放其余
      失败 → revert(scheduled − applied) → Err        # 部分回滚：只退未提交者
      applied.push(r)
  save_sealed_blocks(...)                             # offload 密封块（若有）
  counters.observe_draft(K, matched)  （gated 轮跳过）
```

#### 4.3.3 源码精读

- [src/executor/spec.rs:L1-L6](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L1-L6) —— 模块文档：本模块拥有「调度 → 前向 → 接受/提交或回滚」的乐观 KV 事务与薄草稿派发；前向本身跑在 worker 车道上。
- [src/executor/spec.rs:L51-L71](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L51-L71) —— 前置检查。注意第二个 `ensure` 引用的正是本次提交改名的 `draft_ready_requests`——「draft state is ready」的错误文案也从 DFlash 推广为通用草稿器措辞。
- [src/executor/spec.rs:L73-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L73-L90) —— 全有或全无预留：循环内任何一次 `schedule_speculative` 失败都触发 `revert_speculative_schedules(&scheduled)` 整体归还。
- [src/executor/spec.rs:L92-L111](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L92-L111) —— 用 `speculative_view(span_len)` 构造覆盖「步后 KV 范围」的视图（页面行精确到本步），打包进 `StepCommand::SpeculativeVerify` 发给 worker；`run_step` 失败同样整体回滚。
- [src/executor/spec.rs:L112-L158](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L112-L158) —— 提交前校验：条数、id 对齐，以及「worker 必须已做终末截断」——若接受序列里终末 token 不在末位，说明 worker 归一化破损，在**任何 KV 提交之前**拒绝（宁可硬失败也不在 executor 里静默修复）。
- [src/executor/spec.rs:L160-L193](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L160-L193) —— 提交循环：`apply_speculative` 保留 `accepted_tokens` 的 KV 并 LIFO 释放被拒草稿块；中途失败只回滚未提交者（`applied` 之外）。`PEGAINFER_TEST_LOG` 存在时打 debug 日志（文案已从「DFlash commit」推广为「speculative commit」）。最后 `save_sealed_blocks` 把新密封块下沉 offload。
- [src/executor/spec.rs:L195-L205](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L195-L205) —— 接受率计数：只有 `num_draft_tokens > 0`（非门控轮）才 `observe_draft`。
- [src/executor/spec.rs:L21-L42](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L21-L42) —— `truncate_after_terminal`：找到第一个终末 token，截断其后内容，并把 `matched_draft_tokens` 钳到保留前缀内（触发 token 在接受草稿前缀内时它本身也是一个命中草稿）。
- [src/executor/spec.rs:L210-L235](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L210-L235) —— 草稿派发的薄封装：校验草稿器已装载且每请求 ready 后，把 `StepCommand::SpeculativeDraft` 交给 worker（车道内自行分派 dflash/eagle3）。
- [src/executor/spec.rs:L237-L253](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L237-L253) —— `revert_speculative_schedules`：逐请求独立撤销（LIFO 块纪律是序列内的、经 RAII 保证），逆序只为镜像调度顺序，注释明言「是装饰性的」。
- [../pegainfer-kv-cache/src/pool.rs:L462-L473](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kv-cache/src/pool.rs#L462-L473) 与 [L540-L558](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-kv-cache/src/pool.rs#L540-L558) —— 事务底层原语：`schedule_speculative` 预留连续位置、`apply_speculative` 提交接受前缀并 LIFO 释放被拒块、`revert_schedule` 归还「已调度未应用」的预留。

#### 4.3.4 代码实践

**实践目标**：纸面推演全部三条回滚路径，标出各自回滚的范围。

1. 画出 `execute_speculative_verify_impl` 的流程图，把每个 `revert_speculative_schedules` 调用点编号：①预留失败、②run_step 失败、③校验失败、④提交中途失败。
2. 对每个编号回答：回滚集合是 `scheduled` 全部，还是 `scheduled − applied`？
3. 核对 [src/executor/spec.rs:L262-L275](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L262-L275) 的单测 `explicit_stop_truncates_the_kv_commit_after_the_trigger`：`accepted_tokens=[5,7,8,9]`、stop=[7] → 截断为 `[5,7]`、`matched_draft_tokens` 3→2。运行它（预期结果**待本地验证**）：

```bash
cargo test --release -p pegainfer-qwen3 --lib truncate_after_terminal
```

**需要观察的现象**：①②③回滚 `scheduled` 全体（此时无人提交过）；④只回滚未进入 `applied` 的请求——注释明言已提交者不可撤回。

**预期结果**：一张「失败点 × 回滚范围 × 原因」三列表；理解为什么提交循环失败后引擎选择报错而不是继续（事务语义已被破坏，宁可让这一步失败）。

#### 4.3.5 小练习与答案

**练习 1**：为什么「worker 已做终末截断」的校验放在 KV 提交之前，而不是 executor 自己修一下？

**答案**：见 [src/executor/spec.rs:L139-L145](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L139-L145) 的注释——worker 在记录草稿上下文之前归一化，一个未截断的 span 意味着 worker 侧协议破损；静默修复会让已泄漏进草稿上下文的废弃后缀（u3-l4 的 verify 捕获路径）无声污染下一轮草稿与计数器。在提交前拒绝把破坏挡在 KV 状态被污染之前。

**练习 2**：`apply_speculative` 失败时，已成功提交的请求为什么不能撤销？

**答案**：`apply_speculative` 已经把接受前缀注册进序列并推进了 `kv_position`、LIFO 释放了被拒块——这些块可能已回到池中被别人分配。KV 事务没有跨请求的 undo 日志，所以提交循环采用「部分回滚未提交者 + 整步报错」的语义（[src/executor/spec.rs:L160-L190](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L160-L190)），由上层把错误上抛（单卡路径最终表现为步骤失败，见 u3-l2 的 WorkerLost 传播）。

### 4.4 DraftModel 枚举与共享捕获/ready 集

#### 4.4.1 概念说明

本次提交（785d0487 → af9e40e1）把投机栈从「DFlash 专属」推广为「通用草稿层」：

- 新增 `DraftModel` 枚举：`DFlash(&str)` / `Eagle3(&str)`，按 checkpoint 路径二选一。
- `dflash_ready_requests` 改名 `draft_ready_requests`：ready 集同时服务两种草稿器。
- 模块文档明确边界划分：两个 proposer 都从 **prefill/verify 捕获的目标隐藏态**出发播种，共享捕获管线、ready 集与 KV 事务，**只有把捕获变成草稿的 worker 车道不同**。

ready 集的生命周期是理解这条共享边界的关键：请求在 prefill 完成且捕获了干净目标上下文时进入集合；一旦走了普通（非投机）decode 步，捕获的上下文即过期，请求离开集合并丢弃 worker 侧草稿态。

#### 4.4.2 核心流程

```
launch (lib.rs)
  ├── 两条草稿路径互斥折叠 → Option<DraftModel>
  ▼
executor 构建: draft_enabled（ModelRuntimeConfig，u2-l3）
  + 预留草稿显存（DFlashMemoryReservation / Eagle3MemoryReservation）
  ▼
load_draft_model(DraftModel)
  ├── DraftModel::DFlash(path) → primary.load_dflash
  ├── DraftModel::Eagle3(path) → primary.load_eagle3
  ├── 副作用: 强制关闭前缀缓存（捕获需要未被缓存命中的干净隐藏态）
  └── speculative = Some(DraftMeta { num_spec_tokens, max_draft_batch, ... })
  ▼
请求生命周期:
  prefill 完成 + 捕获   → draft_ready_requests.insert   (MarkReady)
  prefill 未完成 + 捕获 → KeepPending（分块中间态）
  不合格（LoRA/前缀命中/logprobs）→ Drop（清 worker 草稿态）
  普通 decode 步        → remove + drop_draft_request（上下文过期）
  verify 后             → 车道各自重建草稿上下文（eagle3 / dflash 分派）
```

#### 4.4.3 源码精读

- [src/speculative.rs:L33-L38](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L33-L38) —— `DraftModel<'a>` 枚举：按 checkpoint 路径选择草稿器，两个变体各带路径字符串。这是本次新增的类型——草稿器选择从此是一个显式的单槽数据，而不是散落的 Option。
- [src/lib.rs:L316-L336](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L316-L336) —— `launch` 的折叠点：两条草稿路径同时提供即报错「DFlash and EAGLE-3 drafters cannot be loaded together」，否则产出单槽 `Option<DraftModel>`；随后与 LoRA、decode-overlap 做互斥检查。
- [src/executor.rs:L1793-L1827](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1793-L1827) —— `load_draft_model`：要求单 GPU 拓扑、无 KV offload；按枚举分派到 `load_dflash` / `load_eagle3`（worker 命令 `LoadDflash` / `LoadEagle3`，见 u3-l2）；装载后**强制关闭前缀缓存**——投机捕获需要每个 prompt token 干净、未缓存命中的目标隐藏态，前缀命中会跳过本应产出它们的前向。
- [src/executor.rs:L3372-L3388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3372-L3388) —— `DraftMeta`：装载后 executor 学到的草稿器事实——`num_spec_tokens`（每 verify 步最大草稿数 K，已折入 anchor 布局差异）、`max_position_embeddings`、`max_draft_batch`（能力位：DFlash 为 `None` 即不限批宽，EAGLE-3 为 `Some(1)`，u4-l2 的调度门据此把宽批退回纯解码）。
- [src/executor.rs:L1012-L1022](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1012-L1022) —— 字段定义：`speculative: Option<DraftMeta>`（Some 即投机激活）与改名后的 `draft_ready_requests: HashSet<RequestId>`（注释：prompt 带 captured target context 完成预填充时入集，retire 或普通 decode 时离集）。
- [src/executor.rs:L771-L777](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L771-L777) —— `PrefillResult.draft_context_captured_requests`（原名 `dflash_context_captured_requests`）：本步捕获了草稿目标上下文的请求集合，executor 在 prompt 完整预填充后把它们折入 ready 集。
- [src/executor.rs:L2774-L2795](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2774-L2795) —— prefill 后的三态折入：`dflash_prefill_action(captured, completed)` → `MarkReady`（入集）/ `KeepPending`（分块未完）/ `Drop`（离集并清 worker 草稿态）。
- [src/executor/dflash_prefill.rs:L13-L20](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/dflash_prefill.rs#L13-L20) —— `prefill_capture_supported`（原名 `dflash_prefill_supported`）：捕获资格谓词，注释明确「DFlash and EAGLE-3 alike」——无 LoRA、无前缀缓存命中、无任何 logprobs；采样参数无关（prompt 隐藏态与采样无关，sampled-verify 使捕获对采样请求同样有效）。
- [src/executor.rs:L2859-L2867](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2859-L2867) —— 普通 decode 步后：序列在投机路径之外推进，捕获的草稿上下文过期——从 ready 集移除并 `drop_draft_request` 清 worker 态。
- [src/executor.rs:L4092-L4106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4092-L4106) —— verify 后的车道分派点：同一个 `build_verify_results` + `truncate_after_terminal` 之后，按 `self.eagle3.is_some()` 分派 `record_verify_eagle3_context` 或 `record_verify_dflash_context`——接受与事务完全共享，只有「验证后捕获重建」分车道。这就是模块文档「only the worker lane differs」的具体落点。
- [src/executor.rs:L2884-L2896](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2884-L2896) —— `ModelExecutor` 实现面：`speculative_enabled`（`speculative.is_some()`）、`speculative_request_ready`（查 ready 集）、`max_draft_batch`（透出 `DraftMeta.max_draft_batch`）——这三个钩子正是 u4-l2 调度门的输入。

#### 4.4.4 代码实践

**实践目标**：验证改名后的 ready 集如何同时服务两种草稿器，并梳理其完整生命周期。

1. 在 crate 内搜索（**待本地验证**的具体输出，命令本身可直接运行）：

```bash
rg -n "draft_ready_requests" src/
rg -n "DraftModel::" src/
```

2. 把每个 `draft_ready_requests` 的命中点归入四类：**入集**（prefill MarkReady）、**离集-过期**（普通 decode）、**离集-退休**（`drop_request` 路径）、**查询**（`execute_speculative_*_impl` 的前置检查与 `speculative_request_ready`）。
3. 回答关键问题：这些代码里有多少处提到 `dflash` 或 `eagle3` 的名字？（预期：ready 集管理本身零提及——它只认 RequestId 与「捕获了目标上下文」这一事实；草稿器种类只在装载、显存预留、verify 后上下文重建三处分派。）

**需要观察的现象**：ready 集的每个操作点都与草稿器种类无关；`DropDraft`/`drop_draft_request` 是共享的清理命令（对 dflash/eagle3 车道通用）。

**预期结果**：一张「ready 集事件 × 触发时机 × 源码行号」表，以及一句结论：**改名的意义是让名字与真实职责对齐**——这个集合从 DFlash 私产变成了两种草稿器共享的调度事实。

#### 4.4.5 小练习与答案

**练习 1**：为什么装载草稿模型会强制关闭前缀缓存？

**答案**：[src/executor.rs:L1789-L1792](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1789-L1792) 的注释：投机捕获需要**每个 prompt token** 干净、未缓存的目标隐藏态，而前缀缓存命中会直接跳过本应产出这些隐藏态的前向——捕获出现空洞，草稿上下文不完整。这也解释了 u1-l3 里草稿器与前缀缓存相关的互斥规则。

**练习 2**：`DraftMeta.max_draft_batch` 对 DFlash 和 EAGLE-3 分别取什么值，调度器拿它做什么？

**答案**：DFlash 报 `None`（任何 decode 批宽都能出草稿，见 [src/executor.rs:L3605-L3607](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3605-L3607)）；EAGLE-3 报 `Some(1)`（EAGLE3_MAX_BATCH=1，仅单请求）。u4-l2 的 `speculation_decision_at` 把它当批宽能力门：活动批宽超过上限即 NotReady，整批退回纯解码（输出仍无损，u6-l8 展开）。

**练习 3**：两个草稿器在 verify 步之后各自的「重建」发生在哪里？

**答案**：[src/executor.rs:L4092-L4106](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L4092-L4106)——`record_verify_eagle3_context` 与 `record_verify_dflash_context` 在共享的接受+截断之后按已装载车道分派，从 verify 捕获的目标隐藏态重建下一轮草稿所需的上下文；EAGLE-3 侧对应 u6-l5 的 `rebuild_after_verify`。

## 5. 综合实践

**任务：把一条投机轮完整走一遍「纸面全息图」。**

构造如下场景：请求 R 的已确认悬空 token 是 100，DFlash 草稿器提出 K=3 个草稿 [101, 102, 999]，R 是**采样请求**（temperature>0）。目标 verify 前向在 4 个位置产出的 committed token 是 [101, 102, 55, 66]（每行都是按 R 的参数从目标分布采出的样本）。

请完成：

1. **数据装配**：写出 `VerifyStepItem.token_ids`、`VerifyPlan`（gated 取值及理由）。
2. **接受计算**：手工执行 `num_accepted` 与 `accept_prefix_match`，写出 `accepted_tokens` 与 `matched_draft_tokens`。
3. **事务映射**：把这轮对到 4.3 的事务流程上——`schedule_speculative` 预留几个位置的 KV？`apply_speculative` 提交几个、释放几个？若 `run_step` 在这一步失败，回滚集合是什么？
4. **无损性论证**：用一段话解释为什么 R 作为采样请求用同一接受规则不破坏其输出分布（提示：从 committed token 的来源切入，引用 [src/speculative.rs:L18-L25](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L18-L25) 的表述）。
5. **车道分派**：如果装载的是 EAGLE-3 而非 DFlash，上述 1-3 步有哪几步会变？哪几步不变？（提示：`eagle3_prefill_eligible` 要求贪心——本例的采样请求根本进不了 EAGLE-3 车道；但接受规则与 KV 事务一字不变。）

参考答案：1) token_ids=[100,101,102,999]，gated=false（本轮确实有草稿）；2) n=2，accepted=[101,102,55]，matched=2；3) 预留 4 个位置，提交 3 个（LIFO 释放 1 个），run_step 失败回滚 scheduled 全体；4) 每个 committed token 都是目标分布的样本，接受只决定多少个样本同乘一步；5) 步骤 2、3 完全不变（共享核心），变的只有「谁提出草稿、谁在 verify 后重建上下文」——采样请求在 EAGLE-3 的入口门（贪心）就被挡下。

## 6. 本讲小结

- 投机解码被建模为 decode 循环上的**乐观并发事务**：propose K → verify K+1 → accept 最长前缀+1 → commit 接受前缀 KV / rollback 全部预留；`executor/spec.rs` 是事务宿主，预留全有或全无、提交部分回滚。
- 接受规则只有一条 `accept_prefix_match`，**贪心与采样统一**：committed token 分别是融合 argmax 或目标分布样本，接受只决定「几个目标样本同乘一步」，对确定性草稿这就是无损投机采样。
- 草稿/验证边界是**纯 token 跨界**：`VerifyStepItem`/`DraftStepItem` 不含任何隐藏态，草稿 rollout 恒为 argmax（采样只在 verify 侧），proposer 内部状态不出车道。
- 新增 `DraftModel` 枚举按 checkpoint 路径二选一；`launch` 折叠成单槽并做互斥（LoRA、decode-overlap、双草稿器）。
- 两个 proposer **共享捕获管线、ready 集与事务**，只有 worker 车道不同：`dflash_ready_requests` 改名 `draft_ready_requests`、`prefill_capture_supported` 推广、verify 后按 `eagle3.is_some()` 分派上下文重建。
- 装载草稿器强制关闭前缀缓存（捕获需要干净的目标隐藏态）；`DraftMeta.max_draft_batch` 把草稿器批宽能力透给 u4-l2 的调度门。

## 7. 下一步学习建议

- **u6-l2（DFlash 草稿模型）**：看共享事务的一个具体 proposer——`DFlashDraftModel` 的结构与装载，理解「从捕获的目标隐藏态播种」在 DFlash 侧的几何。
- **u6-l5（EAGLE-3 草稿器）**：看另一个 proposer 的 `prefill_prompt` / `chain_round` / `rebuild_after_verify`，特别是它如何消费本讲的 verify 捕获。
- **u6-l8（EAGLE-3 车道与门控）**：`eagle3_prefill_eligible` 的贪心+整段约束、`max_draft_batch` 批宽门的端到端判定路径。
- **u6-l6（verify 图）**：本讲事务中 `run_step` 背后的 verify 前向为何分段捕获 CUDA Graph、attention 为何留在图外。
- 继续阅读顺序建议：先读 [src/speculative.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/speculative.rs#L1-L25) 的模块文档与全部单测，再带着「每个调用点谁负责回滚」的问题精读 [src/executor/spec.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/spec.rs#L1-L6)。
