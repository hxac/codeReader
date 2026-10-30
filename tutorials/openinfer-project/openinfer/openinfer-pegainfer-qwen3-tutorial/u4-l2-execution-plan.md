# 调度器 Ⅱ：执行计划与投机门控

## 1. 本讲目标

上一讲(u4-l1)我们看到调度器如何用 `take_prefill_chunks` 把待处理请求装箱;本讲回答下一个问题:**这一步(step)到底要执行哪种前向?**

学完本讲你应该能:

1. 说出 `ExecutionPlan` 五个变体各自的产生条件,以及 `ExecutionArtifacts` 里装着什么。
2. 逐条推导 `speculation_decision_at` 的四道检查(含本次新增的 `max_draft_batch` 批宽门),解释「全有-or-全无」的门控哲学。
3. 区分三种「退回」:`Gated`(保住捕获的单 token verify)、宽批 `NotReady`(纯 decode、丢捕获)、未就绪 `NotReady`(同样纯 decode)。
4. 理解 `PEGAINFER_SPEC_MAX_BATCH` 负载门如何被读取(`OnceLock` 读一次)与校验(坏值拒绝启动)。
5. 解释为什么采样参数(temperature/top_k/top_p/min_p/seed)**不是**门控条件。

## 2. 前置知识

- **plan/result 相位边界**(u1-l1、u3-l1):调度器与执行器之间只交换纯数据——plan 借用输入、result 拥有输出,没有任何引擎句柄。本讲的 `ExecutionPlan`/`ExecutionArtifacts` 就是调度层内部同一思想的体现。
- **PendingRequest 与 ActiveRequestState**(u4-l1):`PendingRequest` 是还没 prefill 的请求,`ActiveRequestState` 是已在解码循环中的请求(携带 `lora_adapter`、`logprobs`、`params` 等字段)。本讲的门控大量读取这两个结构的字段。
- **投机解码的最小图景**:装载一个「草稿器」(drafter)后,引擎每步可以让草稿器先廉价地提出 K 个候选 token,再让目标模型一次前向同时验证这 K+1 个 token,接受最长正确前缀。细节在 u6 单元展开;本讲只把它当作「执行计划的第三种选择」。
- **能力位(capability bit)**:执行器通过 `ModelExecutor` 的若干只读方法向调度器报告自己的能力,例如 `speculative_enabled()`(装了草稿器吗)、`max_draft_batch()`(一轮最多给几个请求提草稿)。DFlash 不限(`None`),EAGLE-3 v1 只能服务单请求(`Some(1)`)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/scheduler/plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs) | 本讲核心:`ExecutionPlan`/`ExecutionArtifacts` 定义、`build_next_plan`、`SpeculationDecision`/`speculation_decision_at`、`spec_max_batch` 的读取,以及全部纯函数单测 |
| [src/scheduler.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs) | `runtime_plan`/`runtime_plan_at` 把 plan.rs 的纯函数接入调度循环 |
| [src/spec_env.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs) | `PEGAINFER_SPEC_*` 环境旋钮的解析、校验 |
| [src/scheduler/tests.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs) | 调度器层面的集成式单测(含负载门行为测试) |
| [src/scheduler/test_support.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs) | `FakeExecutor`:CPU 上模拟执行器的测试替身,提供 `with_speculative()`/`with_max_draft_batch()` 等投机旋钮 |
| [src/executor.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs) | `ModelExecutor` 能力位定义、`DraftMeta`(能力位的来源)、纯 decode 丢弃捕获的证据 |
| [src/executor/eagle3_lane.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs) 与 [src/eagle3.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs) | EAGLE-3 侧报告 `max_draft_batch = Some(EAGLE3_MAX_BATCH)` 的地方 |

> 上下文说明:相对上一版手册(HEAD `785d0487`),本次提交(`af9e40e1`)随 EAGLE-3 落地在 `speculation_decision_at` 里新增了 `max_draft_batch` 批宽门、新测试 `a_batch_wider_than_the_drafter_serves_decodes_plainly`,并把注释里的捕获资格函数从 DFlash 专属命名推广为 `prefill_capture_supported`(DFlash 与 EAGLE-3 共用)。

## 4. 核心概念与源码讲解

### 4.1 build_next_plan:批形成策略与 ExecutionPlan 五变体

#### 4.1.1 概念说明

调度线程每个 tick 都要做一个决定:**这一步算什么?** 答案是一个 `ExecutionPlan` 枚举值。它是最纯粹的「批形成策略」(batch-formation policy)——输入只有两个布尔量(`have_active`:有活跃请求吗;`pending`:有待 prefill 的请求吗)加上一个投机开关,输出完全由输入决定。正因为它是纯函数,测试才能把 2×2 真值表整个钉死。

执行完毕后,`execute_plan` 产出一个 `ExecutionArtifacts`——同样是无状态的纯数据,供下一讲(u4-l3)的 resolve 层翻译成 ledger 事件。

为什么值得单独一个枚举而不是在调度循环里 if/else?因为「这一步算什么」是全 crate 最容易被悄悄改坏的决策点:统一步(Unified)省一次前向,但有两个请求类别不能进;投机步(SpeculativeDecode)快,但要求整批就绪。把这些约束集中在一个纯函数里,才可能用真值表测试守住。

#### 4.1.2 核心流程

`build_next_plan` 的决策树(伪代码):

```
build_next_plan(have_active, pending, speculative):
    needs_prompt_logprobs = pending 里存在 prompt_logprobs.is_some() 的请求
    needs_capture         = speculative && pending 里存在(无 LoRA 且无 logprobs)的请求

    if pending 非空 && have_active && !needs_prompt_logprobs && !needs_capture:
        → Unified      # prefill 行与 decode 行拼进同一次前向、同一组 GEMM
    else if pending 非空:
        → Prefill      # 专用 prefill 步
    else if have_active:
        → Decode       # 纯解码步
    else:
        → None         # 空闲调度器,无事可做
```

前两格是 2×2 真值表(有/无 active × 有/无 pending),后两个条件是对 Unified 的**定向否决**——只有「既能 Unified 又不允许 Unified」的组合才会落到专用 Prefill。

注意 `needs_capture` 这个宽松超集:真正的捕获资格(`prefill_capture_supported`)还额外要求 `cached_tokens == 0` 且无 prompt logprobs;这里故意不查那么细。代价不对称:把一个不合格的请求多路由到专用 prefill,只损失一次融合机会;把一个合格的请求漏路由进 Unified,它的草稿上下文永远不会被捕获,投机对它**静默失效**——注释原话是「Never tighten this into the dangerous direction」。

#### 4.1.3 源码精读

`ExecutionPlan` 五个变体的定义——[src/scheduler/plan.rs:L22-L39](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L22-L39):`Prefill`/`Decode`/`SpeculativeDecode`/`GatedSpeculativeDecode`/`Unified`。特别注意 `GatedSpeculativeDecode` 变体上的文档注释(L29-L34):负载门触发时不提草稿,目标模型对**单 token span** 做一次 verify;捕获与 draft-ready 状态因此存活,负载降下来后下一轮立即恢复投机——而一次普通 decode 会丢掉捕获,只有重新 prefill 才能重建。这段注释是理解「为什么负载门要专门一个变体」的钥匙。

`ExecutionArtifacts` 四个变体——[src/scheduler/plan.rs:L41-L56](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L41-L56)。注意投机步的 artifacts 只保留 `verify` 结果:草稿结果在 `execute_plan` 内部就被消费掉,用来构造 verify 请求,不需要向下游传播。

决策树本体——[src/scheduler/plan.rs:L58-L90](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L58-L90):L63-L76 的注释解释了两个否决条件(prompt logprobs 需要全位置 logits,Unified 前向不计算;投机捕获同理);L70 计算 `needs_prompt_logprobs`;L77-L80 计算 `needs_dflash_capture`(局部变量仍沿用 dflash 旧名,但 L71-L72 的注释已指向推广后的 `prefill_capture_supported`,DFlash 与 EAGLE-3 共用该资格);L81-L89 是四路分支。

`execute_plan` 如何把计划落到 executor 调用——[src/scheduler/plan.rs:L92-L162](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L92-L162):投机步(L119-L136)是**每步两次 executor 调用**——先 `execute_speculative_draft` 提案 K 个 token,再 `execute_speculative_verify` 一次前向验证 K+1 span;两侧都按 `request_id` 排序保持对齐。gated 步(L137-L146)只调 verify 且 `gated: true`。

verify span 被钳到剩余输出预算——[src/scheduler/plan.rs:L238-L266](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L238-L266):`remaining = max_tokens - generated_count`,草稿 span 截断到 \( \min(K{+}1,\ \text{remaining}) \),防止一次长接受冲破 max_tokens;L253-L255 的 `debug_assert!` 说明「活跃请求必有剩余预算」是被 prove 过的不变量而非运行时条件。gated 步的单 token span 构造在 [src/scheduler/plan.rs:L268-L282](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L268-L282):每个请求只带已确认的 `last_token`。

真值表测试——[src/scheduler/plan.rs:L452-L562](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L452-L562):`plan_selection_follows_active_and_pending_state` 把 2×2 表格、`prompt_logprobs=Some(5)`、`prompt_logprobs=Some(0)`、投机捕获路由、采样参数请求(temperature/min_p/seed 三种组合,L521-L550)、LoRA 请求(L551-L561)逐格断言。开头注释点明意图:「The plan selector is the whole batch-formation policy」——策略回归无法静默溜过。

#### 4.1.4 代码实践

1. **实践目标**:亲眼确认决策树与真值表测试一一对应,并理解两个「否决 Unified」的条件。
2. **操作步骤**:
   - 通读 [src/scheduler/plan.rs:L452-L562](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L452-L562) 的测试,为每条 `assert!` 在纸上标注它覆盖决策树的哪条边。
   - 运行:`cargo test --release -p pegainfer-qwen3 --lib plan_selection_follows_active_and_pending_state -- --nocapture`
3. **需要观察的现象**:测试通过;共 10 组断言(空闲/纯 decode/纯 prefill/Unified/两种 prompt_logprobs/投机捕获/三种采样参数/LoRA)。
4. **预期结果**:全部通过——该测试只构造纯数据结构,不需要 GPU。本讲所有 `--lib` 测试同理。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**:`prompt_logprobs=Some(0)` 表示不返回任何 top logprob,为什么也必须走专用 prefill 而不是 Unified?

**答案**:被评分 token 的 logprob 本身是一次全行 reduction(对整行 logits 求 logsumexp),所以哪怕 `Some(0)` 也需要**每个位置**的 logits;而 Unified 前向把 `all_position_logits` 传 `None`,根本不算这些值。测试注释([src/scheduler/plan.rs:L490-L500](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L490-L500))写明:「prompt_logprobs=0 still needs every position's logits」。

**练习 2**:`needs_dflash_capture` 只检查「无 LoRA 且无 logprobs」,比真实捕获资格宽松。为什么故意宽松而不是精确?

**答案**:两个方向的错误代价不对称。多路由一个不合格请求到专用 prefill,只是少融合一次(一次 GEMM 机会);漏路由一个合格请求进 Unified,它永远不会捕获草稿上下文、永远不进 ready 集,投机对它静默 no-op。宁可白做一次专用 prefill,不可静默丢功能——见 [src/scheduler/plan.rs:L71-L76](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L71-L76) 的注释。

**练习 3**:`build_next_plan(false, vec![], _)` 返回 `None` 意味着什么?

**答案**:调度器空闲——没有活跃请求也没有待处理请求,这一 tick 不产生任何执行计划。这是 2×2 真值表的第四格,由 [src/scheduler/plan.rs:L87-L88](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L87-L88) 的 `else { None }` 分支处理,测试 L454-L457 钉住它。

### 4.2 SpeculationDecision:全有-or-全无的三态门

#### 4.2.1 概念说明

投机解码是**批级**决定,不是每请求决定:要么整批一起「draft + verify」,要么整批一起退回。这就是「全有-or-全无」(all-or-nothing)——哪怕批里 7 个请求都就绪、1 个不就绪,这一步也是整批普通 decode,绝不跑「混合步」。理由是执行器的批前向按行对齐:verify span 的 KV 事务、图捕获形状都假设整批同构,按请求拆开会把这些优化全部打散。

三态:

| 状态 | 含义 | 这一步执行 |
| --- | --- | --- |
| `Speculate` | 整批就绪且未到负载门 | draft K 个 + verify K+1 span |
| `Gated` | 整批就绪但达到负载门 | 单 token verify(保住捕获) |
| `NotReady` | 有请求不就绪 / **批宽超过草稿器服务上限** / 未装草稿器 | 纯 decode(丢捕获) |

「门」(gate)在这里有两道,语义完全不同:

- **负载门**(`max_batch`,来自 `PEGAINFER_SPEC_MAX_BATCH`):运维策略,「并发太重时别再花算力提草稿」。它假设高峰是**暂时**的,所以用 `GatedSpeculativeDecode` 特殊处理——单 token verify 让捕获与 ready 态存活,负载回落立即恢复投机。
- **批宽门**(`max_draft_batch`,来自草稿器能力位):物理能力,「这个草稿器一轮只能服务 N 个请求」。EAGLE-3 v1 通过单 token scratch 走链式 rollout,只能服务 1 个请求(`EAGLE3_MAX_BATCH = 1`);DFlash 的批 scratch 不限(`None`)。批宽超标是**持续性**状态(并发本身就是持续的高于 1),所以直接 `NotReady` 整批纯解码,接受捕获失效的代价。

#### 4.2.2 核心流程

`speculation_decision_at` 的四道检查,**按代码顺序**:

```
speculation_decision_at(executor, active, max_batch):
    ① 未装草稿器 或 active 为空          → NotReady
    ② max_draft_batch = Some(limit) 且 |active| > limit → NotReady   # 本次新增
    ③ 存在请求:未捕获草稿上下文 / 带 LoRA / 带 logprobs → NotReady
    ④ max_batch > 0 且 |active| >= max_batch → Gated
    否则                                    → Speculate
```

要点:

- 检查 ② 在检查 ③ **之前**。两者都产出 `NotReady`,交换顺序不改变结果(观测等价);编排成「先问草稿器能不能服务这个批(能力位),再问每个请求准备好了没(逐请求状态)」是语义分层——能力位一次查询就短路,逐请求检查要遍历整个批并访问 ready 集。
- 检查 ④ 的比较是 `>=`:达到阈值即触发(`|active| = max_batch` 时就 Gated),测试 [src/scheduler/tests.rs:L788-L807](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L788-L807) 用 8 个请求、阈值 8/9 各验一次边界。
- `NotReady` 之后走 `build_next_plan(have_active=true, pending, speculative)`——pending 非空时仍优先 prefill(新请求要捕获草稿上下文,见 4.1);pending 为空才轮到 `Decode`。

#### 4.2.3 源码精读

三态枚举定义——[src/scheduler/plan.rs:L173-L184](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L173-L184)。`NotReady` 的文档注释本次扩为三种成因:「Some request is not draft-ready, the batch is wider than the drafter serves, or no drafter is loaded」。

门控函数本体——[src/scheduler/plan.rs:L192-L219](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L192-L219):L197-L199 检查 ①;L200-L205 检查 ②(新增的批宽门,`is_some_and(|limit| active.len() > limit)`);L206-L213 检查 ③ 的 `all_ready`(`speculative_request_ready && lora_adapter.is_none() && logprobs.is_none()`,三者都要);L214-L218 检查 ④ 分流 Gated/Speculate。函数上方 L186-L191 的注释专门说明**采样参数不是门**:#512 的 sampled-verify 让常规采样器跑在 verify 行上,temperature/top_k/top_p/min_p/seed 全表面都骑在投机路径里。

调度循环的接线——[src/scheduler.rs:L512-L552](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L512-L552):`runtime_plan` 每步读一次 `plan::spec_max_batch()` 再委托 `runtime_plan_at`;`runtime_plan_at` 按 `SpeculationDecision` 三态分流——`Speculate`/`Gated` 下 **pending 非空仍优先 prefill**(投机步只在无事可 prefill 时运行,两者永不同步混合),`NotReady` 落回 `build_next_plan`。阈值参数化的 `runtime_plan_at` 正是为了不碰进程级 env 就能测门控行为。

能力位的定义端——`ModelExecutor::max_draft_batch` 的 trait 默认与文档:[src/executor.rs:L834-L838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838)(「Most requests the loaded drafter proposes for in one round; `None` when it drafts any decode batch」);真实执行器的实现:[src/executor.rs:L2892-L2896](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2892-L2896),从 `DraftMeta.max_draft_batch` 透传。`DraftMeta` 是执行器装载草稿器后学到的东西——[src/executor.rs:L3372-L3388](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3372-L3388)。

两种草稿器如何各报一个能力位:DFlash 装载时构造 `DraftMeta { ..., max_draft_batch: None }`——[src/executor.rs:L3603-L3608](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L3603-L3608)(不限);EAGLE-3 车道装载时 `max_draft_batch: Some(EAGLE3_MAX_BATCH)`——[src/executor/eagle3_lane.rs:L62-L67](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L62-L67),常量定义在 [src/eagle3.rs:L20-L24](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/eagle3.rs#L20-L24):`EAGLE3_MAX_BATCH = 1`,注释说明「v1 drafts through a single-token scratch, so the scheduler plain-decodes any larger batch」。车道侧还有一道防御性 ensure([src/executor/eagle3_lane.rs:L147-L151](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor/eagle3_lane.rs#L147-L151)):即使有调用方绕过调度门,`> EAGLE3_MAX_BATCH` 也会直接报错。

「纯 decode 丢捕获」的证据——`draft_ready_requests` 字段文档:[src/executor.rs:L1019-L1022](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L1019-L1022)(请求在 prompt 捕获完草稿上下文时入集,「leaves on retire or a plain (non-speculative) decode」);`execute_decode` 里的实际丢弃逻辑:[src/executor.rs:L2859-L2866](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2859-L2866)(「A plain decode advances the sequence outside the speculative path, so any captured draft context is now stale — drop it」),Unified 的 decode 行同样镜像处理([src/executor.rs:L2980-L2992](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2980-L2992))。这解释了负载门为何特意绕开纯 decode:单 token verify 让序列在投机路径内前进,捕获不失效。

门控行为测试——不就绪请求绝不被负载门掩盖:[src/scheduler/plan.rs:L403-L415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L403-L415)(`a_request_without_draft_context_is_never_gated`:一个未捕获请求让整批 `NotReady`,而非 `Gated`);批宽门新测试:[src/scheduler/plan.rs:L417-L430](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L417-L430)(`a_batch_wider_than_the_drafter_serves_decodes_plainly`:能力位 1 时,批 1 → `Speculate`,批 2 → `NotReady`);调度层的 Gated 行为:[src/scheduler/tests.rs:L788-L807](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L788-L807) 与 gated 批优先 prefill:[src/scheduler/tests.rs:L809-L822](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L809-L822)。测试侧的能力位旋钮:`FakeExecutor::with_max_draft_batch`——[src/scheduler/test_support.rs:L194-L197](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L194-L197),trait 实现 [src/scheduler/test_support.rs:L310-L320](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/test_support.rs#L310-L320)。

#### 4.2.4 代码实践

1. **实践目标**:手工推导一个混合批的 `SpeculationDecision`,再用新增的批宽门测试验证理解。

2. **操作步骤**:

   **第一步,构造三行伪输入**(EAGLE-3 已装载,即 `speculative_enabled()=true`、`max_draft_batch()=Some(1)`;负载门未设,`max_batch=0`):

   | 请求 | lora_adapter | logprobs | 草稿上下文已捕获 | params |
   | --- | --- | --- | --- | --- |
   | R1 | `None` | `None` | 是 | 默认(temperature=0,贪心) |
   | R2 | `Some("svc-a")` | `None` | 是 | 默认 |
   | R3 | `None` | `Some(3)` | 是 | temperature=0.9, seed=42 |

   逐道检查推导:

   - ① `speculative_enabled()=true` 且 `active` 非空 → 通过;
   - ② `max_draft_batch=Some(1)`,`|active|=3 > 1` → **`NotReady`,在此短路**;
   - (即使没有批宽门)③ R2 带 LoRA、R3 带 logprobs,`all_ready=false` → 仍是 `NotReady`;
   - ④ 永远到不了。

   结论:这一步 `NotReady` → `runtime_plan_at` 落到 `build_next_plan(true, pending, true)`——pending 非空则先 prefill,否则纯 `Decode`,三个请求一起普通解码。

   **第二步,验证**。把推导与既有用例对照:`R2/R3` 这类「结构不就绪」对应 [src/scheduler/plan.rs:L403-L415](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L403-L415) 的思路(任一不就绪 → `NotReady`);批宽门对应新测试。运行:

   ```bash
   cargo test --release -p pegainfer-qwen3 --lib a_batch_wider_than_the_drafter_serves_decodes_plainly -- --nocapture
   ```

3. **需要观察的现象**:测试通过。它构造 `FakeExecutor::new(64, …).with_speculative().with_max_draft_batch(1)`——恰好就是上面伪输入的执行器侧状态;断言批 1 得 `Speculate`、批 2 得 `NotReady`。

4. **预期结果**:两行断言全部命中:`speculation_decision_at(&exec, &speculative_batch(1), 0) == Speculate`,`speculation_decision_at(&exec, &speculative_batch(2), 0) == NotReady`。纯 CPU 单测,无需 GPU。待本地验证。

5. **批宽超限的完整判定路径**(作业里要求说明的那条链):`active.len()=2 > EAGLE3_MAX_BATCH=1` → 检查 ② 命中 → `NotReady` → [src/scheduler.rs:L548-L551](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L548-L551) 落 `build_next_plan` → `Decode` → `execute_decode` 按 [src/executor.rs:L2859-L2866](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2859-L2866) 把两请求移出 `draft_ready_requests` 并丢弃 worker 侧草稿态。**输出无损**(纯解码就是基准路径),代价是捕获失效——并发降回 1 后,幸存请求的投机不会自动恢复,因为 ready 只在 prefill 捕获时建立。

6. **为什么采样参数不是门控条件**:门控的三道检查(①②③)全部是**结构条件**——「执行器能不能服务这个批」「每个请求的执行形态是否与投机路径兼容」(LoRA 会改权重、logprobs 需要 decode 行的逐 token 评分);而采样参数只描述「verify 接受后怎么从 logits 里取 token」。sampled-verify(注释里的 #512)让常规采样器直接跑在 verify 行上,所以 temperature/top_k/top_p/min_p/seed 的**整个表面**都骑在投机路径里——把任何采样参数设成门,都会无理由地把一部分请求挡在加速之外。[src/scheduler/plan.rs:L521-L550](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L521-L550) 的测试用三种采样参数组合专门钉住这一点(它们照样路由到 prefill 以捕获草稿上下文)。

#### 4.2.5 小练习与答案

**练习 1**:检查 ②(批宽门)放在检查 ③(逐请求就绪)之前,如果把两者交换,行为会变吗?为什么还要按现在这个顺序写?

**答案**:不会变——两者都返回 `NotReady`,短路点不同、终态相同(观测等价)。顺序是语义分层:② 是一次 O(1) 的能力位查询(「草稿器能不能服务这个宽度的批」),③ 是 O(|active|) 的遍历还要查 ready 集;先做便宜的、批级的检查,再做逐请求的检查,读代码时也先看到能力约束再看到状态约束。

**练习 2**:负载门触发(`Gated`)与批宽门触发(`NotReady`)都要「这一步少产 token」,为什么一个专门造了 `GatedSpeculativeDecode` 变体、另一个却甘愿走纯 `Decode` 丢捕获?

**答案**:两者的时间尺度不同。负载门是**暂时**高峰:运维设的阈值被并发穿过,下一步请求可能就完成了;单 token verify 让序列在投机路径内前进,捕获与 ready 态保留,负载回落立即恢复投机([src/scheduler/plan.rs:L29-L34](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L29-L34) 注释:「A plain decode would drop the capture, which only a fresh prefill rebuilds」)。批宽门是**持续**状态:并发本身高于草稿器上限(EAGLE-3 为 1),没有「等一下就恢复」的预期,保捕获没有意义,直接纯解码换简单性。

**练习 3**:`max_draft_batch()` 返回 `None` 与 `Some(usize::MAX)` 效果一样吗?

**答案**:对门控判定一样(两者都永不触发 `active.len() > limit`),但语义不同:`None` 是「草稿器不限批」这一**能力声明**——DFlash 的批 scratch 支持任意 decode 批宽,trait 默认值也是 `None`([src/executor.rs:L834-L838](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L834-L838));`Some(MAX)` 则是一个具体上限,只是大到现在够不着。用 `Option` 把「无此限制」表达为类型层面的缺失,而不是魔法数。

### 4.3 spec_env:PEGAINFER_SPEC_* 环境旋钮的读取与校验

#### 4.3.1 概念说明

投机解码有两个运维旋钮,都走环境变量:

- `PEGAINFER_SPEC_MAX_BATCH`:负载门阈值——活跃批达到 N 个请求时停止提草稿(就是 `speculation_decision_at` 的 `max_batch` 参数);`0`/未设 = 关。
- `PEGAINFER_SPEC_MAX_DRAFTS`:每步草稿提案数 K 的上限(修剪 `DraftMeta.num_spec_tokens`,在草稿器侧消费,本讲只关心它的校验)。

这个模块解决两件小事,但每件都有明确的设计立场:

1. **读一次**:`OnceLock` 缓存,进程内不再看环境——旋钮语义是「启动期配置」而非「运行时可调」。
2. **坏值拒绝启动**:拼写错误(如 `4x`)不让引擎带着一个「看起来设了、实际没生效」的杠杆起飞。模块文档的原话:「Silently falling back to off on a typo would turn a load lever into a no-op that looks armed」。

#### 4.3.2 核心流程

```
引擎启动(launch 等入口)
  └─ spec_env::validate()          # 逐旋钮 parse,坏值 → Err → 拒绝启动
        ↓ 引擎运行
调度器首次需要负载门阈值
  └─ plan::spec_max_batch()
        └─ OnceLock.get_or_init(spec_env::read(MAX_BATCH))   # 此后永远走缓存
```

`parse` 的归一化规则:未设 / 空白 / 显式 `0` 全部等价于 off(`0`);其余必须是非负整数,否则报错并带上变量名。

#### 4.3.3 源码精读

模块文档与两个常量——[src/spec_env.rs:L1-L12](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L1-L12):文档写明「read once per process. A malformed value is rejected when the engine starts」。

解析函数——[src/spec_env.rs:L14-L27](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L14-L27):`None` → `Ok(0)`;trim 后为空 → `Ok(0)`;`parse::<usize>()` 失败 → `bail!` 且错误信息含变量名(L25),用户一眼看到是哪个旋钮写错了。

运行期读取与启动期校验的分工——[src/spec_env.rs:L37-L51](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L37-L51):`read` 直接 `expect`(「spec knobs are validated before the engine starts」)——校验早已做过,运行期再失败就是破坏了不变量,panic 合理;`validate` 对两个旋钮各跑一次 `parse`,只验证不取值。调用点在引擎启动入口,如 [src/lib.rs:L443](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/lib.rs#L443)。

负载门的 `OnceLock` 读取端——[src/scheduler/plan.rs:L164-L171](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L164-L171):`spec_max_batch()` 首次调用初始化,此后零开销;文档注明 `0`/未设 = 关、坏值拒绝启动。调度器经 [src/scheduler.rs:L516-L522](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L516-L522) 的 `runtime_plan` 每步调用它。

单测——[src/spec_env.rs:L57-L64](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L57-L64):四种 off 形态(`None`/`""`/`"  "`/`"0"`)全部解析为 0;`"4x"` 的报错必须含变量名。

#### 4.3.4 代码实践

1. **实践目标**:验证「off 的四种形态」与「坏值带变量名报错」两条承诺。
2. **操作步骤**:
   - 阅读 [src/spec_env.rs:L57-L64](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L57-L64) 的断言;
   - 运行:`cargo test --release -p pegainfer-qwen3 --lib blank_means_off_and_a_bad_value_names_the_knob -- --nocapture`。
3. **需要观察的现象**:测试通过。
4. **预期结果**:通过;该测试直接调用 `parse`(不触碰真实进程环境),纯 CPU。待本地验证。
5. **延伸(选做)**:对照 [src/dflash.rs:L27-L32](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/dflash.rs#L27-L32) 看 `MAX_DRAFTS` 旋钮的另一处 `OnceLock` 消费点,确认两个旋钮共享同一套 parse/validate 而各持一份缓存。

#### 4.3.5 小练习与答案

**练习 1**:为什么 `read` 里敢用 `expect` 直接 panic,而不是返回 `Result`?

**答案**:因为 `validate` 在引擎启动时已经对同一批旋钮跑过完全相同的 `parse`(见 [src/spec_env.rs:L37-L43](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/spec_env.rs#L37-L43) 的注释)。运行期 `read` 再遇到坏值,只可能是有人绕过启动校验直接构造执行器——那是破坏了不变量,属于程序错误,快速失败比把错误往调度线程里传更诚实。

**练习 2**:把 `PEGAINFER_SPEC_MAX_BATCH=8` 在引擎运行到一半时 export 进环境,会生效吗?

**答案**:不会。`spec_max_batch` 用 `OnceLock` 读一次([src/scheduler/plan.rs:L168-L171](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs#L168-L171)),而且子进程的环境本就继承自启动时刻,运行中改父 shell 也不会传入。这两个旋钮是启动期配置,不是运行时旋钮。

## 5. 综合实践

**任务:为一个 EAGLE-3 引擎推演一个 5-tick 时间线的执行计划。**

设定:装载 EAGLE-3 草稿器(`speculative_enabled=true`,`max_draft_batch=Some(1)`),负载门 `PEGAINFER_SPEC_MAX_BATCH=4`(阈值 4)。请求到达与完成如下,所有请求都无 LoRA、无 logprobs:

| tick | 活跃批 | 待 prefill | 事件 |
| --- | --- | --- | --- |
| t0 | {A} | {} | A 刚 prefill 完,捕获完成 |
| t1 | {A} | {B} | B 到达 |
| t2 | {A, B} | {} | B prefill 完成(捕获完成) |
| t3 | {A, B} | {} | 持续解码 |
| t4 | {A} | {} | B 完成(retire),只剩 A |

请逐 tick 写出:① `speculation_decision_at` 的判定(哪道检查命中);② `runtime_plan_at` 产出的 `ExecutionPlan`;③ 该步执行后 `draft_ready_requests` 的变化。

**参考推演**(先自己做,再对照):

- **t0**:① 过①②(1 不大于 1),`all_ready=true`,阈值 4 未达 → `Speculate`;② `SpeculativeDecode`(draft 3 个 + verify 4 span);③ ready 集不变(speculative verify 在路径内前进)。
- **t1**:① 对**活跃批** {A} 判定为 `Speculate`,但 pending 非空 → ② `Prefill {B}`([src/scheduler.rs:L534-L540](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler.rs#L534-L540) 的 pending 优先规则);③ B 完成捕获后入 ready 集。注意即使负载门触发(`Gated`),pending 也同样优先——[src/scheduler/tests.rs:L809-L822](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/tests.rs#L809-L822) 钉的就是这条。
- **t2**:① 检查② 命中:`|active|=2 > 1` → `NotReady`;② pending 空 → `Decode`;③ **A、B 都被移出 ready 集**(纯 decode 丢弃捕获,[src/executor.rs:L2859-L2866](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/executor.rs#L2859-L2866))。
- **t3**:同 t2,继续纯 decode。
- **t4**:B 离开,`|active|=1` 不再超批宽门;但 A 的 ready 已在 t2 丢失,`all_ready=false` → 检查③ 命中 → 仍是 `NotReady` → 纯 decode,**A 的投机不再恢复**(ready 只在 prefill 捕获时建立)。

**思考题**:同一时间线换成 DFlash(`max_draft_batch=None`)并把并发改成 5 个请求,负载门(阈值 4)在第几tick 触发?触发后是哪个变体、ready 集如何变化?(答案:活跃批达到 4 的那一步检查④ 命中 → `Gated` → `GatedSpeculativeDecode`,单 token verify,**ready 集完整保留**;并发降回 3 后立即恢复 `Speculate`——这正是负载门与批宽门时间尺度差异的具体体现。)

## 6. 本讲小结

- `build_next_plan` 是全部批形成策略:2×2 真值表(Unified/Prefill/Decode/None)加两个对 Unified 的定向否决(prompt_logprobs 需要全位置 logits;投机捕获需要专用 prefill),并故意用宽松超集判定捕获路由——宁可多一次专用 prefill,不可静默丢投机。
- `SpeculationDecision` 三态全有-or-全无:投机是批级决定,一个不就绪请求让整批退回;四道检查按「未启用/空批 → 批宽能力位 → 逐请求就绪 → 负载门」排列,前两道都是 O(1) 短路。
- 本次新增的 `max_draft_batch` 批宽门把草稿器能力位变成调度可见:活动批宽超过上限即 `NotReady` 整批纯解码;EAGLE-3 报 `Some(1)`(单 token scratch 的 v1 限制),DFlash 报 `None`。
- `Gated` 与宽批 `NotReady` 的本质区别是时间尺度:负载门是暂时高峰,用单 token verify 保住捕获;批宽超限是持续状态,纯 decode 接受捕获失效(ready 集在 plain decode 时清空)。
- 采样参数不是门:sampled-verify 让常规采样器跑在 verify 行上,temperature/top_k/top_p/min_p/seed 全表面骑在投机路径里;门控只看结构条件(LoRA、logprobs、捕获、批宽)。
- `spec_env` 的两个旋钮读一次(`OnceLock`)、坏值拒绝启动:静默把打错的杠杆变成「看似设了实际没生效的 no-op」是不可接受的。

## 7. 下一步学习建议

- **u4-l3(resolve 与 StepEffects)**:本讲的 `ExecutionArtifacts` 产出后去哪?下一讲讲 executor 结果如何被解析成纯 `StepEffects` 写进 ledger——`SpeculativeDecode` 的 verify 结果如何变成「一次产多个 token」的事件。
- **u6-l1(投机解码事务核心)**:本讲把投机当作计划的第三种选择;u6-l1 展开 propose→verify→accept 的事务语义——KV 回滚、最长前缀+1 奖励、`DraftModel` 枚举与两个 proposer 共享的捕获/ready 集。
- **u6-l8(EAGLE-3 车道与门控)**:本讲只看到能力位 `Some(1)` 的消费端;u6-l8 讲 `eagle3_lane` 的生产端——装载时如何构造 `DraftMeta`、车道侧的防御性 ensure、以及 `tests/common/lossless.rs` 如何同时服务 DFlash 与 EAGLE-3 的无损性门测试。
- 想动手的话:给 [src/scheduler/plan.rs](https://github.com/openinfer-project/openinfer/blob/af9e40e12108141bb18f93188c82d2583df18509/pegainfer-qwen3/src/scheduler/plan.rs) 的 `speculation_decision_at` 写一个「批宽恰好等于上限」的边界单测(`active.len() == limit` 应该 `Speculate`,因为判定是 `>` 而非 `>=`),仿照 `a_batch_wider_than_the_drafter_serves_decodes_plainly` 的写法。
