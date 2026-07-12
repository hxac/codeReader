# 事件-动作循环与 Action 接口

> 本讲对应单元 U9（C++ 推理引擎架构），承接 [u9-l1 引擎、ThreadedEngine 与 EngineState](u9-l1-engine-threaded-state.md)。
> 在 u9-l1 里我们已经知道：`ThreadedEngine` 是线程安全外壳，`EngineImpl` 是跑模型的内核，`EngineState` 是二者共享的可变状态，而 `Step()` 是引擎的「心跳」。
> 本讲就打开这个心跳——**每次 `Step()` 到底做了什么、由谁驱动、又是如何被拆成一个个可插拔的「动作（Action）」的**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `EngineActionObj` 这个抽象接口的形状，以及为什么 MLC 要把 prefill/decode/verify 这些事都建模成「动作」。
2. 画出 `EngineImpl::Step()` 的调度循环：它如何遍历 `actions_` 列表、为什么每步只跑一个动作、跑完之后由谁收尾。
3. 解释 `CreateEngineActions` 如何根据 `speculative_mode` / `disaggregation` 装配出不同优先级的动作链。
4. 说出 `action_commons` 里那几个「公共工具」各管什么事：后处理回调、抢占、采样、移除序列。
5. 把全部 11 个 Action 按「进入 / 解码 / 推测 / 校验 / 分离」五个阶段分类。

## 2. 前置知识

本讲是 C++ 源码精读，需要你先具备以下概念（均在 u9-l1 建立）：

- **EngineState**：引擎的运行期状态容器，持有 `waiting_queue`（待 prefill）、`running_queue`（可 decode）、每请求的 `request_states`、前缀缓存、metrics 等。它被声明为 `mutable`，**只允许唯一一个后台线程访问**。
- **ThreadedEngine 的后台线程模型**：前台接口只把请求/指令投递进队列；真正干活的 `Step()` 由后台线程驱动。本讲会落到具体那一行调用上。
- **请求的两级状态**：`Request`（不可变输入）与 `RequestState` / `RequestStateEntry` / `RequestModelState`（可变运行期状态，含 `committed_tokens`、`draft_output_tokens`、`status`）。动作的职责本质就是「读这些状态 → 调模型 → 写回这些状态」。
- **TVM Object 系统**：`EngineActionObj` 继承自 `tvm::ffi::Object`，`EngineAction` 是它的智能引用（`ObjectRef`）。不熟悉的读者只需理解成「面向对象的接口 + 句柄」即可。

几个用得上的小记号：本讲用「动作（Action）」专指 `EngineActionObj` 的某个子类实例；用「步（Step）」专指引擎的一次 `Step()` 调用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpp/serve/engine_actions/action.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action.h) | 声明 `EngineActionObj` 接口（只有一个核心方法 `Step`）与 `EngineAction` 句柄（列出全部 Action 的工厂方法）。 |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | `EngineImpl::Step()` 的调度循环——本讲的主角。 |
| [cpp/serve/engine_actions/action_commons.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.h) / [action_commons.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc) | 跨动作复用的公共工具：动作表工厂、步后处理、抢占、采样、移除序列。 |
| [cpp/serve/engine_actions/new_request_prefill.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc) | 「进入」阶段动作的典型实现，精读对象。 |
| [cpp/serve/engine_actions/batch_decode.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc) | 「解码」阶段动作，含显存不足时的抢占流程。 |
| [cpp/serve/engine_actions/auto_spec_decode.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc) | 「组合动作」：先决策、再委托子动作，展示 Action 可嵌套。 |
| [cpp/serve/threaded_engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc) | 后台循环里那一行 `background_engine_->Step()`，回答「谁在驱动」。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 EngineActionObj 接口**：动作长什么样、为什么这么设计。
- **4.2 Step 调度循环**：引擎如何遍历动作表、每步只跑一个、跑完怎么收尾。
- **4.3 action_commons 工具**：跨动作复用的公共函数（后处理、抢占、采样）。

### 4.1 EngineActionObj 接口

#### 4.1.1 概念说明

引擎在每个「步」里要回答一个问题：**这一步该干什么？** 候选答案至少有：

- 有新请求在 `waiting_queue` 排队 → 该 **prefill**（预填）它们；
- 有请求在 `running_queue` 里 → 该 **decode**（逐 token 解码）；
- 启用了推测解码 → 该 **draft**（起草）再 **verify**（校验）；
- 启用了分离式推理 → 该 **远程发送 / 准备接收 KV**；
- 启用了 grammar 约束 → 该 **jump-forward**。

一种朴素写法是在 `Step()` 里写一个巨大的 `if/else`，把上述所有逻辑、所有分支、所有模型调用全塞进去。MLC 没有这么做。它把**每一类「这一步可以做的事」抽象成一个对象**——`EngineActionObj`，它对外只暴露一个方法：

```cpp
virtual Array<Request> Step(EngineState estate) = 0;
```

> 读法：给我当前引擎状态 `estate`，我自己判断「这一步我有没有事干」；没事就返回空数组 `{}`，有事就**真去干**（调模型、跑采样、改状态），并把「我这一步处理了哪些请求」返回出来。

这是经典的 **事件-动作（event-action）/ 职责链（chain of responsibility）** 模型：

- **事件**＝引擎的一次 `Step()` 心跳 + 当前 `estate`（哪些队列非空、KV 还剩多少页、是否开了推测解码……）。
- **动作**＝一个 `EngineActionObj` 子类，它自己看状态决定要不要响应这次事件。

这样做的好处：

1. **可插拔、可组合**：新增一种推理模式（例如将来再加一种推测解码算法）只需新增一个 Action 文件，在 `CreateEngineActions` 里挂进列表，完全不碰 `Step()` 主循环。
2. **优先级即列表顺序**：动作在 `actions_` 数组里的先后顺序就是它们的优先级（详见 4.2）。
3. **可嵌套**：`AutoSpecDecode` 自己就是一个 Action，但它内部会调用别的 Action 的 `Step`——动作可以组合动作（4.2.3 会看到）。

#### 4.1.2 核心流程

任意一个具体 Action 的 `Step(estate)` 都遵循同一个「四段式」骨架：

```text
Step(estate):
  ① 判定与收集            —— 看状态，决定本步能否/要不要做，收集要处理的 rsentry
       若无事可做 → 返回 {} （让调度循环去找下一个动作）
       若资源不足 → 调 PreemptLastRunningRequestStateEntry 抢占别人腾地方
  ② 调模型函数            —— TokenEmbed → BatchPrefill / BatchDecode / BatchVerify
  ③ 采样                  —— LogitProcessor 修 logit → Sampler 采 token
  ④ 写回状态              —— CommitToken 到 mstates、更新 metrics
  返回「本步处理了的请求列表」
```

两个要点：

- **动作不直接回调用户**。动作只把新 token `CommitToken` 进 `RequestModelState`；真正「把 token 流式送回前端」是引擎在动作返回之后，由公共函数 `ActionStepPostProcess` 统一做的（4.3.2）。这把「计算」与「回调」解耦，避免每个动作都重复写回调逻辑。
- **「返回空」是合法且常见的语义**，它表示「这一步轮不到我」，调度循环会继续问下一个动作。

#### 4.1.3 源码精读

接口定义极其精简——整个抽象只有一个纯虚方法：

[cpp/serve/engine_actions/action.h:34-52](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action.h#L34-L52) 声明 `EngineActionObj`，其核心方法在第 42 行：

```cpp
class EngineActionObj : public Object {
 public:
  virtual Array<Request> Step(EngineState estate) = 0;
  ...
};
```

注释把契约说得很明白（第 26-33 行）：`Step` 读当前引擎状态、调模型函数（如 batched-prefill / batched-decode）、跑采样、再更新状态。

[cpp/serve/engine_actions/action.h:59-258](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action.h#L59-L258) 的 `EngineAction` 是它的句柄类，**用一组静态工厂方法把「支持哪些动作」全列了出来**。这些工厂就是全部 Action 的目录：

| 工厂方法（`EngineAction::Xxx`） | 对应文件 | 阶段 |
| --- | --- | --- |
| `NewRequestPrefill` | new_request_prefill.cc | 进入 |
| `EagleNewRequestPrefill` | eagle_new_request_prefill.cc | 进入（EAGLE） |
| `BatchJumpForward` | batch_jumpforward.cc | 解码辅助（grammar） |
| `BatchDecode` | batch_decode.cc | 解码 |
| `BatchDraft` | batch_draft.cc | 推测（起草） |
| `EagleBatchDraft` | eagle_batch_draft.cc | 推测（EAGLE 起草） |
| `BatchVerify` | batch_verify.cc | 校验 |
| `EagleBatchVerify` | eagle_batch_verify.cc | 校验（EAGLE/Medusa） |
| `AutoSpecDecode` | auto_spec_decode.cc | 推测/解码（组合决策） |
| `DisaggPrepareReceive` | disagg_prepare_recv.cc | 分离（接收方） |
| `DisaggRemoteSend` | disagg_remote_send.cc | 分离（发送方） |

> 注意：还有一个 `BatchPrefillBaseActionObj`（batch_prefill_base.cc），它是 `NewRequestPrefill` 与 `EagleNewRequestPrefill` 的**抽象基类**，把 prefill 的公共流程抽出来复用，但它本身不进动作表，所以不在「目录」里。

来看一个具体动作的四段式落地。[cpp/serve/engine_actions/new_request_prefill.cc:34-43](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L34-L43) 是「① 判定与收集」的入口：

```cpp
Array<Request> Step(EngineState estate) final {
  std::vector<PrefillInput> prefill_inputs;
  prefill_inputs = GetRequestStateEntriesToPrefill(estate);  // 从 waiting_queue 挑能 prefill 的
  if (prefill_inputs.empty()) {
    return {};          // ← 空返回：本步轮不到我，调度循环去问下一个动作
  }
  ...
```

接着第 ② 段调模型：[new_request_prefill.cc:141-152](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L141-L152) 对每个模型先 `TokenEmbed` 拿 embedding，再 `BatchPrefill` 出 logits：

```cpp
Tensor logits = models_[model_id]->BatchPrefill(embeddings, request_internal_ids, prefill_lengths);
```

第 ③ 段采样（[new_request_prefill.cc:166-171](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/new_request_prefill.cc#L166-L171)）：`InplaceUpdateLogits` → `ComputeProbsFromLogits`，再在第 248-251 行用 sampler 采样。第 ④ 段把采样结果写回（第 256-257 行 `UpdateRequestStateEntriesWithSampleResults`），最后返回 `processed_requests`（第 262-265 行）。

> **prefill 也采样**：注意 NewRequestPrefill 在 prefill 完成后**立刻采了第一个 token**（prefill 最后一个位置的 logit）。这就是为什么用户在 prefill 一结束就能拿到首 token——首 token 是在 prefill 这一步顺便采出来的，TTFT（time-to-first-token）因此不必等下一个 decode 步。

#### 4.1.4 代码实践

**实践目标**：用一个具体动作验证「四段式骨架」与「空返回」语义。

**操作步骤（源码阅读型）**：

1. 打开 [batch_decode.cc 的 Step](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L44-L201)。
2. 找到「① 判定与收集」：它在 [第 46-48 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L46-L48) 检查 `running_queue.empty()` 就 `return {}`——这就是「轮不到我」的空返回。
3. 找到「② 调模型」：[第 139-149 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L139-L149)，注意它有个聪明的分叉——当**每个请求都只解码一个 token** 时走 `BatchDecode` kernel，否则退回 `BatchPrefill` kernel（处理推测解码被拒后多 token 重算的情形）。
4. 找到「③ 采样」与「④ 写回」：[第 164-189 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L164-L189)。

**需要观察的现象**：`BatchDecode::Step` 末尾（[第 200 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L200)）`return estate->running_queue;`——只要 `running_queue` 非空进来，它就一定返回非空，因此 decode 永远不会「空返回逃走」（除非一开始队列就空）。这与 NewRequestPrefill 的「可能空返回」形成对照。

**预期结果**：你能把 `BatchDecode::Step` 的每一行归入四段式中的某一段，没有任何一行落在骨架之外。

#### 4.1.5 小练习与答案

**练习 1**：`EngineActionObj` 只暴露 `Step` 一个核心方法。为什么不让它再分出 `DecideCanRun()` 和 `Run()` 两个方法，让调度循环先问「能不能跑」再「跑」？

**参考答案**：因为「能不能跑」的判断本身就要读一遍状态，而「跑」又要再读一遍、且状态在两次调用之间可能被别的动作改动。合成一个 `Step` 让动作**原子地**「判断 + 执行」：看到自己该做就立刻做完，避免 TOCTOU（check-then-act 竞态）。代价是「轮不到我」时动作也得先读状态才知道要返回空——但这是单线程后台循环里很廉价的读。

**练习 2**：`NewRequestPrefill` 在 prefill 之后采样了首 token，那 `BatchDecode` 采样的又是什么？

**参考答案**：decode 采样的是**下一个** token。decode 的输入是「上一轮刚 commit 的最后一个 token」（见 [batch_decode.cc:110-113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L110-L113) 取 `committed_tokens` 的尾部），模型用它预测下一个位置的概率分布，采样新 token 再 commit。所以 prefill 给出第 1 个生成 token，之后每个 decode 步给出 1 个。

---

### 4.2 Step 调度循环

#### 4.2.1 概念说明

有了「动作」这个抽象，引擎主循环就变得极其简单：**拿着一张有序的动作表，从头问到尾，谁先说「我干了事」就听谁的，然后收工。** 这就是 `EngineImpl::Step()`。

关键设计决策有三个，理解了它们就理解了整个调度：

1. **每步只跑一个动作**。循环里一旦某个动作返回了非空的处理请求列表，立刻 `return`，不再问后面的动作。这一步就结束了。
2. **列表顺序就是优先级**。排在前面的动作先被问。所以「想提高某种行为的优先级」=「在 `CreateEngineActions` 里把它放前面」。
3. **动作表在引擎创建时一次性装配好**，运行期不增删（[engine.cc:494-497](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L494-L497) 调一次 `CreateEngineActions`，结果存在成员 [engine.cc:1009](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L1009) `Array<EngineAction> actions_;`）。装配依据是 `EngineConfig` 里的 `speculative_mode`、`spec_draft_length`、模型元数据里的 `disaggregation` 等开关。

#### 4.2.2 核心流程

`Step()` 的全部逻辑可以写成这样一段伪代码：

```text
Step():
    要求 request_stream_callback 已设置（否则报错）
    for action in actions_:                  # 按优先级遍历动作表
        processed = action.Step(estate)      # 让动作判断 + 执行
        if processed 非空:
            ActionStepPostProcess(processed) # 统一收尾：回调前端、清理已完成请求
            return                           # ← 每步只跑一个动作
    # 走到这里说明没有任何动作有事干
    断言 running_queue 为空                   # 否则违背内部不变量
```

「走到底」的合法情形只有一种：**两个队列都空，引擎真的无事可做**（`Empty()` 为真）。如果 `running_queue` 非空却没有动作响应，那一定是 bug——因为普通模式下 `BatchDecode` 总在表里，只要 `running_queue` 非空它就会响应。这个不变量被显式断言保护（[engine.cc:766-768](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L766-L768)）。

那么「谁在反复调用 `Step()`」？答案在 ThreadedEngine 的后台循环：[cpp/serve/threaded_engine.cc:186](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L186)

```cpp
if (background_engine_ != nullptr) {
  background_engine_->Step();
}
```

这与 u9-l1 讲的「单消费者后台线程」对上了：前台 `AddRequest` 只把请求塞进 `estate_->waiting_queue`，后台循环被唤醒后**不停调用 `Step()`**，每次 Step 跑一个动作，于是 prefill/decode/verify 就这样被轮流推进。一个请求的完整生命周期，就是后台循环在许多个 Step 里、由不同动作接力处理的结果。

#### 4.2.3 源码精读

主循环本体：[cpp/serve/engine.cc:749-769](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L749-L769)

```cpp
void Step() final {
  TVM_FFI_ICHECK(estate_->request_stream_callback_ != nullptr)
      << "The request stream callback is not set. Engine cannot execute.";
  for (EngineAction action : actions_) {
    Array<Request> processed_requests;
    {
      NVTXScopedRange nvtx_scope("Action step");
      processed_requests = action->Step(estate_);
    }
    if (!processed_requests.empty()) {
      ActionStepPostProcess(processed_requests, estate_, models_, tokenizer_,
                            estate_->request_stream_callback_,
                            engine_config_->max_single_sequence_length,
                            draft_token_workspace_manager_, trace_recorder_);
      return;
    }
  }
  TVM_FFI_ICHECK(estate_->running_queue.empty()) << "Internal assumption violated: ...";
}
```

逐行解读：

- 第 750-751 行：前置检查——回调必须已设。因为 `ActionStepPostProcess` 要用回调把 token 送回前端，没回调就跑等于白算。
- 第 752 行：`for (EngineAction action : actions_)`——按优先级遍历。
- 第 756 行：`action->Step(estate_)`——把状态交给动作，多态调用。
- 第 758-764 行：第一个返回非空的动作获胜 → 跑 `ActionStepPostProcess` 收尾 → **`return`**（每步只跑一个动作）。
- 第 766-768 行：走到底的不变量保护。

**动作表怎么来的？** [engine.cc:494-497](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L494-L497) 在 `EngineImpl::Create` 里调一次 `CreateEngineActions`，把所有依赖（models、logit_processor、sampler、tokenizer、draft_token_workspace_manager 等）注入进去，产出的有序列表存进 `actions_`。

**装配规则**见 [action_commons.cc:16-137](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L16-L137)。把关键分支整理成下表（顺序即优先级，从左到右降低）：

| 模式 | 装配出的动作表 |
| --- | --- |
| 普通模式（无推测、无分离） | `[NewRequestPrefill, BatchJumpForward, BatchDecode]` |
| 小模型推测（`spec_draft_length > 0`） | `[NewRequestPrefill, BatchDraft, BatchVerify]` |
| 自动推测（`spec_draft_length == 0`） | `[NewRequestPrefill, AutoSpecDecode([BatchDraft, BatchVerify], [BatchDecode])]` |
| EAGLE 推测 | `[EagleNewRequestPrefill, EagleBatchDraft, EagleBatchVerify]` |
| Medusa 推测 | `[EagleNewRequestPrefill, EagleBatchVerify]` |
| 分离式（无推测） | `[DisaggPrepareReceive, DisaggRemoteSend, NewRequestPrefill, BatchDecode]` |

> 读出三条规律：
>
> 1. **prefill 永远优先级最高**（除分离式动作外，`NewRequestPrefill` 总在最前）。这意味着「有新请求排队」时引擎会优先 prefill 新请求、把已 running 的 decode 往后让一步——这是为了压低首 token 延迟（TTFT）。
> 2. **分离式动作会被插到最前面**（[action_commons.cc:127-135](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L127-L135) 用 `actions.insert(actions.begin(), ...)`）。
> 3. **AutoSpecDecode 是「动作套动作」**：它自己是一个 `EngineActionObj`，但它的 `Step` 内部会再调子动作的 `Step`（见下面）。

**组合动作范例**：[auto_spec_decode.cc:28-49](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L28-L49)

```cpp
Array<Request> Step(EngineState estate) final {
  int num_running_rsentries = estate->GetRunningRequestStateEntries().size();
  if (num_running_rsentries == 0) { return {}; }
  estate->spec_draft_length = CalculateDraftLength(estate, num_running_rsentries);
  Array<Request> processed_requests;
  Array<EngineAction> actions =
      estate->spec_draft_length > 0 ? spec_decode_actions_ : batch_decode_actions_;
  for (EngineAction action : actions) {
    processed_requests = action->Step(estate);   # ← 委托给子动作
  }
  estate->spec_draft_length = 0;
  return processed_requests;
}
```

它先按当前 batch 大小算一个 `spec_draft_length`（[第 52-68 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/auto_spec_decode.cc#L52-L68)），draft 长度 >0 就走推测子链（`BatchDraft`+`BatchVerify`），==0 就退回普通 `BatchDecode`。有效 batch 大小是

\[
\text{effective\_batch\_size} = \text{num\_running\_rsentries} \times (\text{draft\_length} + 1)
\]

若它超过 `max_num_sequence` 就把 draft_length 强制清零（退回普通解码），保证不超容量。

> 注意：`AutoSpecDecode` 内部 `for` 跑子动作时**没有**「非空就 return」——它把 draft 和 verify 串起来**都跑掉**。这与外层引擎循环「每步只跑一个动作」并不矛盾：从外层看，这一步只跑了 `AutoSpecDecode` 一个动作；至于它内部如何编排子动作，是它自己的事。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「每步只跑一个动作」+「后台线程驱动」这两件事。

**操作步骤（源码阅读 + 运行验证型）**：

1. 在 [engine.cc:752-765](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L752-L765) 的 `for` 循环里，逻辑上给每个动作编号：你想追踪「每步到底跑了第几个动作」，可以在 `action->Step(estate_)` 调用前后各加一行 `std::cerr` 日志（仅作学习，勿提交），打印动作类名（可用 `action->GetTypeKey()`）。
2. 用 Python 起一个引擎发一条请求（参考 [examples/python/sample_mlc_engine.py](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/examples/python/sample_mlc_engine.py)），流式收 token。

**需要观察的现象**：

- 日志会按类似 `NewRequestPrefill → BatchDecode → BatchDecode → … → BatchDecode` 的序列反复出现——**每条日志对应一次 `Step()`，每次只有一个动作**。
- 收到首 token 的那一刻，日志里恰有一条 `NewRequestPrefill`（它在 prefill 里顺便采了首 token，见 4.1.3）。
- 请求结束后，`running_queue` 变空，后续 `Step()` 里三个动作都空返回，循环走到底、命中末尾断言的「合法空闲」分支（不报错）。

**预期结果**：你能从日志里数出「一条请求 = 1 次 prefill + N 次 decode」，N 与生成 token 数大致吻合。

> 若本地没有可运行的 GPU/模型，可降级为纯源码阅读：在 [threaded_engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc) 里找到 `RunBackgroundLoop`（[第 136 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/threaded_engine.cc#L136)），顺着它读到第 186 行的 `background_engine_->Step()`，画一张「后台循环 → Step → actions_ 列表 → 某动作 → ActionStepPostProcess」的调用链图即可。**待本地验证**：实际日志序列。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Step()` 末尾的断言只检查 `running_queue`，不检查 `waiting_queue`？

**参考答案**：因为「`waiting_queue` 非空但本步不 prefill」是**合法**的——例如当前显存都被 running 请求占满、`NewRequestPrefill` 评估后选择不动（不把新请求接入以避免超容量），于是在等待。此时 `BatchDecode` 会响应（running 非空），循环不会走到底。但若 `running_queue` 非空却无任何动作响应，那一定是 bug（BatchDecode 应当能处理），所以只对 `running_queue` 做不变量保护。

**练习 2**：把 `NewRequestPrefill` 放在 `BatchDecode` 之前，意味着「有新请求时优先 prefill 而不 decode」。这对延迟数字有什么影响？

**参考答案**：降低 TTFT（首 token 延迟）、可能略增 TPOT（每 token 延迟）。因为新请求 prefill 是一整段较重的计算，会推迟已 running 请求的下一步 decode；但换来新用户更快拿到首字。这是一种面向交互式负载的取舍。

---

### 4.3 action_commons 工具

#### 4.3.1 概念说明

动作的「四段式」里有不少逻辑是**跨动作共用**的，比如：

- 跑完一步后，把新 token 流式送回前端、清理已完成的请求——所有动作都需要；
- 显存不够时，抢占最低优先级的 running 请求——`BatchDecode`/`BatchDraft`/`BatchVerify` 都需要；
- 采样（logit 处理 + 采样）——几乎所有动作都需要；
- 从所有模型的 KV cache 里移除一个序列——抢占、abort、完成清理都需要。

把这些抽出来放进 `action_commons`，就避免每个动作文件各自抄一遍。`action_commons` 提供五件公共工具：

| 函数 | 作用 | 谁调用 |
| --- | --- | --- |
| `CreateEngineActions` | 按配置装配有序动作表 | 引擎 `Create`（仅一次） |
| `ActionStepPostProcess` | 步后处理：收集 delta、回调前端、清理已完成请求 | 引擎 `Step`（每次有动作跑完） |
| `PreemptLastRunningRequestStateEntry` | 抢占 `running_queue` 尾部的请求 | decode/draft/verify 动作在容量不足时 |
| `RemoveRequestFromModel` | 从所有模型移除某序列 | 抢占、abort、完成清理 |
| `ApplyLogitProcessorAndSample` | 修 logit + 采样 | 动作内部的采样段 |

#### 4.3.2 核心流程

**`ActionStepPostProcess`**（每次有动作跑完都调用）做四件事：

```text
ActionStepPostProcess(processed_requests):
  清空复用工作区 postproc_workspace
  for request in processed_requests:
      ① 取本步的 delta（新 token / 新前缀串），若有则入 callback 队列
      ② 若某分支判定完成（hit stop / 达 max_tokens），入 finished 列表
      ③ 把新 commit 的 token 通知前缀缓存 ExtendSequence（除最后一个未入 KV 的 token）
  ProcessFinishedRequestStateEntries(finished):   # 标记完成、爬父链、回收
  if callback 队列非空:
      request_stream_callback(delta_outputs)      # ← 一次性把本步所有请求的 delta 送回前端
```

**`PreemptLastRunningRequestStateEntry`**（显存不够时腾地方）做：

```text
PreemptLastRunningRequestStateEntry():
  取 running_queue 末尾请求（最低优先级），找它最后一条 alive 的 rsentry
  if 分离式: 直接 abort 该请求，返回
  把该 rsentry 状态 alive → pending
  清掉它的 draft token（释放 draft 槽位）
  把它已 commit 的 token 与原始 inputs 合并，作为「下次重新 prefill 的输入」
  从 KV/前缀缓存中移除它的序列，分配一个新的 internal_id（旧 KV 作废）
  从 running_queue 摘下；若不在 waiting_queue 则插到队首，等下一步重新 prefill
```

核心思想：**抢占不是丢弃，而是「回退到待 prefill 状态」**。被抢占请求已生成的 token 不丢（合并回 `inputs`），但它占的 KV cache 立即释放给更高优先级的请求；之后轮到 `NewRequestPrefill` 时它会从 `waiting_queue` 队首被重新 prefill（命中前缀缓存的话还能省一段）。

#### 4.3.3 源码精读

**步后处理**：[action_commons.cc:240-331](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L240-L331)。三段关键代码：

- 收集 delta 与完成判定（[第 254-320 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L254-L320)）：遍历每个请求的每个分支（`n>1` 时是多个子分支），用 `GetDeltaRequestReturn` 取出增量，决定是否触发回调、是否标记完成。
- 通知前缀缓存（[第 280-307 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L280-L307)）：把新 prefill 的输入与新 decode 的 token（**除最后一个，因为它还没写进 KV cache**）通过 `ExtendSequence` 登记进前缀缓存。
- 一次性回调（[第 326-330 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L326-L330)）：`request_stream_callback(estate->postproc_workspace.callback_delta_outputs)`。**注意是一次性把本步所有请求的 delta 打包送回**，而不是每请求一次——减少 FFI 跨界次数。

**完成清理**：[action_commons.cc:177-238](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L177-L238) 的 `ProcessFinishedRequestStateEntries`。它把完成的叶子 rsentry 标 `kFinished`、调 `RemoveRequestStateEntry` 释放 KV，然后**沿 `parent_idx` 往上爬**——只有当一个父节点的**所有子分支**都完成，父节点才算完成（这是 `n>1` 多分支生成的语义）。爬到根（`parent_idx == -1`）后，把请求从 `running_queue` 和 `request_states` 里移除，更新 metrics，并**总是**回送一个 usage 输出（前端依赖它判定终止）。

**抢占**：[action_commons.cc:333-427](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L333-L427)。读它时抓住这几行：

- [第 338 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L338)：取 `running_queue.back()`——最低优先级。
- [第 363 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L363)：`rsentry->status = RequestStateStatus::kPending;`——退回 pending。
- [第 379-401 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L379-L401)：把已 commit 的 token 合并进 `inputs`，作为下次 prefill 的输入（不丢工作成果）。
- [第 412-415 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L412-L415)：分配新 `internal_id`——旧序列的 KV 已作废，下次 prefill 相当于一条新序列（可能命中前缀缓存）。
- [第 417-424 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L417-L424)：从 running 摘下、插到 waiting 队首。

谁触发抢占？看 [batch_decode.cc:51-68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L51-L68)：

```cpp
while (!CanDecode(running_rsentries.size())) {
  if (estate->prefix_cache->TryFreeMemory()) continue;        // 先试软回收前缀缓存
  RequestStateEntry preempted =
      PreemptLastRunningRequestStateEntry(estate, models_, std::nullopt, trace_recorder_);
  if (preempted.same_as(running_rsentries.back())) {
    running_rsentries.pop_back();
  }
}
```

策略是「先软后硬」：先 `TryFreeMemory`（让前缀缓存释放一些可回收的旧序列），还不够才真抢占。`CanDecode` 的判据很简单——可用 KV 页数够不够（[batch_decode.cc:205-208](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_decode.cc#L205-L208)）：`num_rsentries <= GetNumAvailablePages()`。

**移除序列**：[action_commons.cc:139-145](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L139-L145)，对每个模型调 `model->RemoveSequence(req_internal_id)`——从 KV cache 删掉这条序列。

**采样公共函数**：[action_commons.cc:429-448](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L429-L448) 的 `ApplyLogitProcessorAndSample`，把「修 logit → 算概率 → top-p 重归一化 → 采样」打包。它区分「父配置」与「子配置」两组采样参数——这是为推测解码校验阶段准备的：用大模型（父）的概率分布校验、但按 draft 树（子）的位置采样。

#### 4.3.4 代码实践

**实践目标**：搞清「抢占」到底有没有丢工作成果。

**操作步骤（源码阅读型）**：

1. 打开 [PreemptLastRunningRequestStateEntry](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L333-L427)。
2. 聚焦 [第 379-401 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L379-L401)：它把 `committed_token_ids`（已生成的 token）追加到原 `request->inputs` 末尾，存进 `mstate->inputs`。
3. 再看它如何分配新 `internal_id`（[第 412-415 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L412-L415)）并把请求插回 `waiting_queue` 队首（[第 421-424 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L421-L424)）。

**需要观察的现象**：被抢占请求的 `num_prefilled_tokens` 被清零（[第 384 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L384)），`cached_committed_tokens` 归零（[第 403 行](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L403)）——也就是说**模型侧进度清零**，但**逻辑上的 token 不丢**（合并进了 inputs）。

**预期结果**：你能解释清楚——抢占后这条请求下一步进 `NewRequestPrefill` 时，会把「原始 prompt + 已生成 token」当作输入重新 prefill。若前缀缓存里还留着它的旧前缀，`MatchPrefixCache` 能命中、省掉一段重算（这正是前缀缓存在长上下文抢占场景下的价值）。

**待本地验证**：在显存压力大、触发抢占的负载下，用 `/stats` 或 metrics 观察被抢占请求的 prefill_tokens 是否在重新 prefill 后变大、decode 是否从中断处继续。

#### 4.3.5 小练习与答案

**练习 1**：`ActionStepPostProcess` 里，登记进前缀缓存时为什么**跳过最后一个 commit 的 token**？（提示在源码注释里）

**参考答案**：因为最后那个 token 是「本步刚采出来、写进 `committed_tokens` 但**还没写进 KV cache**」的——它要等下一步 decode/verify 时才被模型消费并落进 KV。若现在就把它登记进前缀缓存，前缀缓存里的序列会比 KV cache 多一个 token，造成两者不一致。

**练习 2**：抢占时 `PreemptLastRunningRequestStateEntry` 为什么要给序列分配一个**新的** `internal_id`，而不是保留旧 id？

**参考答案**：因为旧 id 对应的 KV cache 已经被释放（`RemoveRequestFromModel` 或 `RecycleSequence`），那条序列在模型里已经不存在了。保留旧 id 会让后续操作误以为它的 KV 还在。换新 id 等于声明「我是一条全新的、尚未进 KV 的序列」，下一步 prefill 时 `NewRequestPrefill` 会正常 `AddNewSequence`（或命中前缀缓存 `ForkSequence`）把它重新建起来。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这张 **「Action 全目录分类表」**。这是本讲的核心实践任务。

**任务**：对照 [action.h 的工厂方法清单](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action.h#L59-L258)（或每个动作对应 `.cc` 文件顶部注释），把全部 11 个 Action 按「**进入 / 解码 / 推测 / 校验 / 分离**」五阶段分类，并补全「输入队列」「是否采样」「典型装配模式」三列。

**参考答案表**（建议你先自己填，再对照）：

| Action | 阶段 | 主要读哪个队列 | 是否在本步采样 | 出现在的装配模式 |
| --- | --- | --- | --- | --- |
| `NewRequestPrefill` | **进入** | waiting_queue | 是（采首 token） | 全部非 EAGLE/Medusa 模式 |
| `EagleNewRequestPrefill` | **进入** | waiting_queue | 是 | EAGLE / Medusa |
| `BatchJumpForward` | **解码**（grammar 辅助） | running_queue | 否（只预测约束 token） | 普通模式 |
| `BatchDecode` | **解码** | running_queue | 是 | 普通模式 / 自动推测（作退路）/ 分离式 |
| `BatchDraft` | **推测** | running_queue | 是（起草） | 小模型推测 / 自动推测 |
| `EagleBatchDraft` | **推测** | running_queue | 是（起草） | EAGLE |
| `BatchVerify` | **推测**→**校验** | running_queue | 是（校验后采） | 小模型推测 / 自动推测 |
| `EagleBatchVerify` | **推测**→**校验** | running_queue | 是 | EAGLE / Medusa |
| `AutoSpecDecode` | **解码/推测**（决策编排） | running_queue | 否（委托子动作） | 自动推测 |
| `DisaggPrepareReceive` | **分离**（接收方） | waiting_queue | 否 | 分离式 |
| `DisaggRemoteSend` | **分离**（发送方） | waiting_queue | 否（仅 prefill+发送） | 分离式 |

**进阶子任务**（任选）：

1. 在 [action_commons.cc 的 CreateEngineActions](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L16-L137) 里，为上表每个 Action 标注它**实际出现在哪一行被构造**，验证「顺序即优先级」。
2. 选 EAGLE 模式，画出一次完整 Step 序列：`EagleNewRequestPrefill` →（之后每步）`EagleBatchDraft` → `EagleBatchVerify` 的循环，并标注哪一步产生了用户可见的 token。
3. 思考题：Medusa 模式里**没有** Draft 动作（只有 `EagleBatchVerify`），它靠什么产生候选 token？（提示：Medusa 的多颗「tree head」在模型内部一次性产出多步候选，verify 阶段直接校验，无需独立 draft 步——详见 u10-l4 推测解码动作链。）

> **待本地验证**：若你有 GPU，可用 `speculative_mode=eagle` 与 `=disable` 各起一个引擎，对照上面 Step 序列的日志差异。

## 6. 本讲小结

- **动作是引擎的「行为单元」**：`EngineActionObj` 把 prefill/decode/verify/disagg 等都抽象成同一个接口 `Step(estate) → Array<Request>`，遵循「判定 → 调模型 → 采样 → 写回」四段式骨架，无事可做时返回空数组。
- **`Step()` 是职责链**：引擎持有一张有序动作表 `actions_`，每次 `Step()` 从头问到尾，**第一个返回非空的动作获胜并执行，之后立刻 return——每步只跑一个动作**；走到底则要求 `running_queue` 必须为空。
- **列表顺序即优先级**：`CreateEngineActions` 按 `speculative_mode`/`disaggregation`/`spec_draft_length` 装配出不同动作链；`NewRequestPrefill` 几乎总在最前（压低 TTFT），分离式动作插在最前。
- **后台线程驱动心跳**：`ThreadedEngine::RunBackgroundLoop` 反复调用 `background_engine_->Step()`，前台只投递请求；一个请求的生命周期由许多个 Step、不同动作接力完成。
- **动作不回调、由 commons 收尾**：动作只把 token `CommitToken` 进状态；真正「流式送回前端 + 清理已完成请求」由 `ActionStepPostProcess` 统一做，并**一次性打包**调用回调减少 FFI 次数。
- **action_commons 还提供抢占与采样工具**：`PreemptLastRunningRequestStateEntry` 在显存不足时把最低优先级 running 请求「回退到 pending、保留已生成 token、释放 KV、插回 waiting 队首」；`AutoSpecDecode` 展示了动作可嵌套（动作套动作）。

## 7. 下一步学习建议

本讲把「动作接口 + 调度循环 + 公共工具」讲透了，但有意没展开两块细节：

1. **请求在被动作处理时的精细状态流转**（`waiting_queue` ↔ `running_queue` ↔ `finished`、`committed_tokens` vs `draft_output_tokens`、`status` 状态机、`n>1` 多分支树）——这是 [u9-l3 请求生命周期与状态机](u9-l3-request-lifecycle.md) 的主题，建议紧接着读。
2. **动作调到的那些模型函数**（`BatchPrefill`/`BatchDecode`/`BatchVerify`/`CreateKVCache`）如何由 `FunctionTable` 从 model lib 里加载——见 [u9-l4 模型运行时与 FunctionTable](u9-l4-model-runtime-functiontable.md)。
3. 推测解码与分离式的具体动作链（draft → verify → 接受/拒绝；prefill → 远程发送 KV → 接收 → decode）分别属于 [u10-l4 推测解码动作链](u10-l4-speculative-decoding.md) 与 [u12-l3 分离式推理](u12-l3-disaggregation.md)，可作为专家层的延伸。

建议的源码阅读顺序：先重读 [engine.cc 的 Step](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L749-L769) 与 [action_commons.cc 的 CreateEngineActions](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L16-L137) 巩固本讲，再进 u9-l3。
