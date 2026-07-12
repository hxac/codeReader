# 请求生命周期与状态机

> 本讲对应单元 U9（C++ 推理引擎架构），承接 [u9-l2 事件-动作循环与 Action 接口](u9-l2-action-loop.md)。
> 在 u9-l2 里我们已经知道：引擎的每个「步（`Step`）」都由一组按优先级排序的「动作（Action）」驱动，动作的职责本质就是「**读请求状态 → 调模型 → 写回请求状态**」。
> 本讲就打开这个「请求状态」黑盒——**一个用户请求从进引擎到出结果，到底经历了哪些数据结构、状态如何流转、显存不够时又是怎么被「抢」回去的**。

## 1. 本讲目标

学完本讲，你应当能够：

1. 区分 `Request` 与 `RequestState` 这两层：前者是用户提交的**不可变**输入，后者是引擎为它维护的**可变**运行期状态。
2. 画出「请求 → `RequestState` → `RequestStateEntry`（树节点）→ `RequestModelState`（单模型视图）」这四层包含关系，并解释为什么 `n > 1`（多分支生成）时要建成一棵 `(n+1)` 个节点的树。
3. 说清 `committed_tokens` 与 `draft_output_tokens` 的差别——前者是已「敲定」的 token，后者是推测解码里小模型「起草」、等待大模型校验的临时 token。
4. 描述 `RequestStateStatus` 三态机 `kPending → kAlive → kFinished`，并把它们对应到 `waiting_queue` / `running_queue` / 出队的迁移上。
5. 解释显存不足时「抢占（preemption）」如何把一个运行中的请求**连 token 带 KV 一起回退**到 `waiting_queue` 队首，且不丢失已生成内容。

## 2. 前置知识

本讲是 C++ 源码精读，需要你先具备以下概念（在 u9-l1、u9-l2 建立）：

- **EngineState 的两个队列**：`waiting_queue` 存「还没开始 prefill」的请求，`running_queue` 存「可以继续 decode」的请求；两者加上 `request_states` 映射共同构成引擎的可变状态，且**只允许唯一一个后台线程访问**。
- **动作（Action）的四段式骨架**：判定与收集 → 调模型 → 采样 → 写回状态。本讲关注的正是第四段「写回状态」时被改写的那些字段，以及第一段「资源不足时抢占」如何反过来改写状态。
- **TVM Object 系统**：`RequestNode` / `RequestStateNode` 等继承自 `tvm::ffi::Object`，对应的 `Request` / `RequestState` 是智能引用（`ObjectRef`）。不熟悉可简单理解为「面向对象的接口 + 句柄」。
- **推测解码（speculative decoding）两段式**：小模型先「起草（draft）」若干候选 token，大模型再一次性「校验（verify）」，校验通过的 draft token 才会被「提交（commit）」。本讲不展开算法，只讲这些 token 存在哪。

一个贯穿全讲的关键直觉：**MLC 把「请求」拆成「不变的契约」和「可变的进度」两层**。这样请求对象本身可以安全地在节点间转发、重启（分离式推理会用到），而所有「生成到哪儿了」的易变信息都隔离在状态层。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [cpp/serve/request.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request.h) | 声明 `RequestNode`（不可变请求：id / inputs / prompt_tokens / generation_cfg）与 `Request` 句柄。本讲第一主角。 |
| [cpp/serve/request_state.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h) | 声明三层状态：`RequestModelStateNode`（单模型视图，含 committed/draft token）、`RequestStateEntryNode`（树节点）、`RequestStateNode`（整棵树），以及 `RequestStateStatus` 三态枚举。本讲核心。 |
| [cpp/serve/request_state.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc) | `CommitToken` / `AddDraftToken` / `RemoveAllDraftTokens` / `RollbackTokens` 等状态修改方法的实现。 |
| [cpp/serve/engine_state.h](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_state.h) | `running_queue` / `waiting_queue` / `request_states` 容器的定义——状态机迁移的「场地」。 |
| [cpp/serve/engine.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc) | `EngineImpl::AddRequest`：请求入队 + 创建初始 `RequestState`（含 `n>1` 时的建树逻辑）。 |
| [cpp/serve/engine_actions/batch_prefill_base.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_prefill_base.cc) | prefill 动作里 `kPending → kAlive` 的迁移点：把请求从 waiting 挪进 running。 |
| [cpp/serve/engine_actions/action_commons.cc](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc) | 步后处理里的「完成」逻辑（`kFinished` + 树自底向上收敛）与抢占函数 `PreemptLastRunningRequestStateEntry`。 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 Request：不可变的用户请求**——请求长什么样、为什么设计成不可变。
- **4.2 RequestModelState：单模型上的生成状态**——`committed_tokens` 与 `draft_output_tokens` 这两种 token 的家。
- **4.3 RequestState 与 RequestStateEntry：多分支生成的树结构**——`n > 1` 时为什么是 `(n+1)` 个节点的树。
- **4.4 状态机与抢占**——`kPending → kAlive → kFinished` 三态迁移，以及显存不足时的抢占回退。

### 4.1 Request：不可变的用户请求

#### 4.1.1 概念说明

当用户通过 OpenAI 兼容 API 发来一条 `chat/completions` 请求，引擎在入口处会把它包装成一个 `Request` 对象。`Request` 描述的是**「用户想要什么」**这份契约，本身不随生成进度改变。它只携带四样东西：

- `id`：请求的唯一标识（字符串），用于在队列、状态映射、流式回调里追踪它。
- `inputs`：用户输入，是 `Array<Data>`——之所以是数组且元素类型是 `Data` 而非纯文本，是因为输入可能是**多模态**的（一段文本 + 若干张图片，见 `data.h`）。
- `prompt_tokens`：输入序列的等价 token 长度；`-1` 表示「还没 tokenize，长度未知」。
- `generation_cfg`：采样配置（temperature、top_p、max_tokens、`n` 等），即 `GenerationConfig`。

关键设计在头文件的注释里一句话点明：

> `Request is immutable and can be re-dispatched to another node and restart the request handling on the new one.`

「不可变」+「可转发到别的节点重启」——这是为**分离式推理（disaggregation）**留的口子：prefill 和 decode 可以落在不同节点上，请求对象需要能原样搬到下游节点。所以所有「生成到哪儿了」的易变信息都**不能**放在 `Request` 里，而要放到下一节的 `RequestState`。

> 小提示：这里的「不可变」是**逻辑约定**，不是 C++ `const` 强制。事实上 `prompt_tokens` 会在 tokenize 后被回填、`rstate`（指向状态的裸指针）会在入队时被挂上。但用户视角的 `id / inputs / generation_cfg` 一旦提交就不再变化。

#### 4.1.2 核心流程

一个 `Request` 的诞生与归位：

```text
用户 API 请求
   │  （json_ffi / serve 层组装）
   ▼
Request(id, inputs, generation_cfg)        ← prompt_tokens 可能仍为 -1
   │  EngineImpl::AddRequest
   │  ├─ Request::FromUntokenized → 把文本数据 tokenize，回填 prompt_tokens
   │  ├─ push 进 waiting_queue
   │  └─ 同时创建 RequestState（见 4.3），挂回 request->rstate
   ▼
此后 Request 本身不再变；所有进度写进 RequestState
```

注意 `Request` 和 `RequestState` 是**同时**进入引擎的：`AddRequest` 一边把 `Request` 推进 `waiting_queue`，一边构造好它的 `RequestState` 并登记进 `request_states` 映射。

#### 4.1.3 源码精读

`RequestNode` 的字段定义与「不可变」注释：[cpp/serve/request.h:L28-L57](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request.h#L28-L57)——这段同时声明了 `id` / `inputs` / `prompt_tokens`（默认 `-1`）/ `generation_cfg`，以及用于回指状态的裸指针 `rstate`。

`Request` 句柄与「保持 id 不变的再 tokenize」工厂方法：[cpp/serve/request.h:L72-L86](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request.h#L72-L86)。`FromUntokenized` 的契约写得很清楚：返回一个「所有文本数据都已 tokenize、且 **id 保持不变**」的新请求——这正是「不可变 + 可重启」要求的体现。

入队与状态创建的衔接点在 `AddRequest`：[cpp/serve/engine.cc:L697-L727](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L697-L727)。第 698 行 `waiting_queue.push_back(request)`，第 720 行 `RequestState rstate = RequestState(...)`，第 726 行 `request->rstate = rstate.operator->()` 把状态回指挂到请求上——请求与状态在此刻绑定为伴生关系。

#### 4.1.4 代码实践

**实践目标**：确认 `Request` 的「契约」边界——哪些字段是用户给的、哪些是引擎回填的。

**操作步骤**（源码阅读型）：

1. 打开 [cpp/serve/request.h:L35-L70](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request.h#L35-L70)，列出 `RequestNode` 的全部公有字段。
2. 在 [cpp/serve/engine.cc:L668-L728](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L668-L728) 的 `AddRequest` 中，标出每个字段第一次被写入的位置。

**需要观察的现象**：`id` / `inputs` / `generation_cfg` 在 `AddRequest` 里**只读不写**；而 `prompt_tokens` 在第 681 行 `Request::FromUntokenized` 之后才被断言为 `!= -1`（即由引擎回填），`rstate` 在第 726 行被挂上。

**预期结果**：你会清楚地看到「用户三件套（id/inputs/generation_cfg）保持不变，引擎只回填长度和状态回指」这条边界。无需运行，结论可直接从源码读出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `prompt_tokens` 的默认值是 `-1` 而不是 `0`？

> **答**：因为请求在创建时可能还含有**未 tokenize 的文本数据**，此时总长度未知。用 `-1` 这个「非法长度」显式表达「未知」，区别于 `0`（空输入）。`AddRequest` 在 tokenize 后会断言它已变成有效值（[engine.cc:L681](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L681)）。

**练习 2**：`request->rstate` 为什么用裸指针 `Object*` 而不是智能引用 `RequestState`？

> **答**：为了避免**循环引用**导致内存泄漏——`RequestState` 内部又会回指 `Request`（见 4.3）。两边都用强引用会形成环，故其中一边退化为不持有所有权的裸指针（注释 L58-L59、L233-L237 都明说这一点）。

### 4.2 RequestModelState：单模型上的生成状态

#### 4.2.1 概念说明

`Request` 只说「想要什么」，不说「做到哪了」。「做到哪了」由状态层负责，而 `RequestModelState` 是状态层里**最细的一格**：它记录「**某个请求，在某一个模型上**，生成到哪儿了」。

为什么强调「某一个模型」？因为引擎可能同时用多个模型服务一个请求——最典型的就是**推测解码**：一个小模型负责起草，一个大模型负责校验。同一个请求在两个模型上的进度并不一样（小模型可能已经「想到」第 5 个 token，大模型才确认到第 3 个）。所以 MLC 选择「**按模型隔离状态**」，每个模型一份 `RequestModelState`。

`RequestModelState` 里最重要的两组字段，正好对应本讲标题里的两种 token：

- **`committed_tokens`**（`std::vector<SampleResult>`）：已经**敲定**的生成 token。「committed」表示这些 token 不会再变了，它们是会被流式返回给用户、会被写进 KV cache 的「正式产出」。
- **`draft_output_tokens`**（`std::vector<SampleResult>`）：推测解码里由小模型**起草**的候选 token。它们是「 tentative（暂定）」的——要等大模型 verify 通过后，其中一部分才会被「提升」进 `committed_tokens`，其余的被丢弃。

它还携带一组围绕这两种 token 的簿记字段：`inputs`（还没 prefill 的输入）、`num_prefilled_tokens`（已经 prefill 的数量）、`cached_committed_tokens`（已在 prefix cache 里的数量）、`appeared_token_ids`（已出现 token 及其出现次数，供重复惩罚用），以及推测解码专属的 `draft_token_slots` / `draft_token_parent_idx` / `draft_token_first_child_idx`（draft token 树的父子关系与显存槽位）。

#### 4.2.2 核心流程

两种 token 的写入/清除时机：

```text
普通解码 / prefill 采样出 token t
   │  RequestModelState::CommitToken(t)
   ▼
committed_tokens.push_back(t)        ← 正式产出，不可撤销（除非抢占回滚）
appeared_token_ids[t] += 1
num_tokens_for_next_decode += 1
grammar_matcher.AcceptToken(t)        ← 若启用了 grammar 约束

—— 推测解码分支 ——
小模型起草 token d，分配槽位 s、父节点 p
   │  RequestModelState::AddDraftToken(d, s, p)
   ▼
draft_output_tokens.push_back(d)     ← 临时产出
draft_token_slots / parent_idx / first_child_idx 同步登记

大模型 verify：通过的 draft token → CommitToken；其余 → 丢弃
显存不足 / 抢占 / 请求完成
   │  RequestModelState::RemoveAllDraftTokens()
   ▼
draft_* 全部清空，槽位归还 DraftTokenWorkspaceManager
```

一条铁律：**`CommitToken` 只动 `committed_tokens`，`AddDraftToken` 只动 `draft_output_tokens`**，二者井水不犯河水。draft token 想「转正」必须显式再调一次 `CommitToken`。

#### 4.2.3 源码精读

「按模型隔离状态」的设计意图，写在类注释里：[cpp/serve/request_state.h:L30-L37](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L30-L37)——明说「we use RequestModelState to store the state of a user request on a single model」。

`committed_tokens` 与「committed 即不再变」的定义：[cpp/serve/request_state.h:L52-L55](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L52-L55)。

`draft_output_tokens` 及其三件套簿记字段（slots / parent_idx / first_child_idx）：[cpp/serve/request_state.h:L72-L84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L72-L84)，注释 L70-L71 标明这些是「为推测解码预留、由小模型产出」。

`CommitToken` 的实现：[cpp/serve/request_state.cc:L56-L68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L56-L68)——push 进 `committed_tokens`、更新 `appeared_token_ids` 与 `num_tokens_for_next_decode`、推进 grammar 状态；注释特别强调「Does not effect the kv cache」（KV cache 的写回是 model 层的事，这里只改簿记）。

`AddDraftToken` 的实现：[cpp/serve/request_state.cc:L85-L96](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L85-L96)——登记 draft token 并维护父子链（`first_child_idx` 链表头）。

`RemoveAllDraftTokens` 的实现：[cpp/serve/request_state.cc:L98-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L98-L113)——清空四个并行数组，并把用过的槽位去重后回传给调用方释放。

#### 4.2.4 代码实践

**实践目标**：验证「commit 与 draft 两条写入路径互不干扰」。

**操作步骤**（源码阅读型 + 思维实验）：

1. 读 [request_state.cc:L56-L68](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L56-L68) 的 `CommitToken` 与 [request_state.cc:L85-L96](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L85-L96) 的 `AddDraftToken`，确认它们分别只 push 进 `committed_tokens` / `draft_output_tokens`。
2. 再读 [request_state.cc:L98-L113](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.cc#L98-L113) 的 `RemoveAllDraftTokens`，确认它**只清 draft 相关数组**，绝不碰 `committed_tokens`。
3. 思维实验：若小模型起草了 `[a, b, c]` 三个 draft token，大模型只接受 `a`、拒绝 `b` 之后的所有，引擎需要调用哪些方法才能让状态收敛到「committed 追加 a，draft 清空」？

**需要观察的现象**：`RemoveAllDraftTokens` 不接受「部分清除」参数，它是**全有或全无**——一旦要清就把整批 draft 清光。因此「接受 a、拒绝 bc」的实现必然是：先 `CommitToken(a)`，再 `RemoveAllDraftTokens()`。

**预期结果**：你能用一句话复现 verify 阶段的状态收敛动作：「对每个被接受的 draft token 调 `CommitToken`，最后 `RemoveAllDraftTokens` 一把清掉残留」。结论可直接从源码推出。**待本地验证**：若想看实际调用顺序，可在 `batch_verify.cc` 里加日志打印每次 `CommitToken` / `RemoveAllDraftTokens` 的调用栈。

#### 4.2.5 小练习与答案

**练习 1**：`CommitToken` 注释说「Does not effect the kv cache」。那 KV cache 是由谁、在什么时候写入的？

> **答**：由 **model 层**（`ModelObj` 的 `BatchPrefill` / `BatchDecode` / `BatchVerify`）在执行计算时写入分页 KV cache。`RequestModelState` 只做「逻辑簿记」（token 列表、出现次数），它与「物理 KV」是解耦的——这也是为什么抢占时能「保留 token、释放 KV」（见 4.4）。

**练习 2**：`draft_token_parent_idx` / `draft_token_first_child_idx` 维护的是一种什么结构？为什么 draft token 需要它，而 committed token 不需要？

> **答**：维护的是 draft token 的**树形父子关系**（speculative decoding 常用树/图状起草，多个候选 token 共享前缀）。committed token 是**线性序列**（一条时间线），所以只需一个 vector；draft token 是**一棵候选树**，需要父子链来表达「哪个候选接在哪个候选后面」。

### 4.3 RequestState 与 RequestStateEntry：多分支生成的树结构

#### 4.3.1 概念说明

4.2 解决了「单模型视图」，但还有两个上层问题：

1. 一个请求可能用到**多个模型**（推测解码），如何把它们的状态打包？
2. OpenAI API 的 `n` 参数允许一次请求**并行生成 `n` 条**回答，如何表达这种分叉？

MLC 用两层结构回答：

- **`RequestStateEntry`**（状态条目）：描述「**单条生成分支**」的状态。它持有一个 `Array<RequestModelState> mstates`——数组每一项对应一个模型（于是问题 1 在这一层解决：`mstates[0]` 是主模型、`mstates[1]` 是 draft 小模型……）。它还持有这条分支自己的 `rng`（随机数生成器）、`stop_str_handler`、`status`、`next_callback_token_pos` 等。
- **`RequestState`**（请求状态）：**一组** `RequestStateEntry` 的集合，即 `std::vector<RequestStateEntry> entries`。问题 2 在这一层解决：`n` 条并行回答就是 `n` 个子 entry。

而 `n > 1` 时的关键设计是：这些 entry **不是平铺的数组，而是一棵树**：

- 第 `0` 号 entry 是**根（root）**，代表请求的「公共 prompt 前缀」状态。
- `n` 条并行生成都挂在根下，成为根的 **child**。

所以 `n > 1` 时总共有 **`(n + 1)` 个 entry**（1 个根 + n 个子）。每个 entry 用 `parent_idx`（父节点在 vector 里的下标，根为 `-1`）与 `child_indices`（子节点下标列表）维护树的连接。头文件特别保证：**vector 从头到尾的顺序始终是这棵树的拓扑序**——即父节点一定出现在子节点之前，这样很多「先处理父、再处理子」的遍历可以直接按 vector 顺序线性扫。

为什么不直接用 `n` 个互相独立的 entry，而要建树？因为 `n` 条生成分支**共享同一个 prompt 前缀**——前缀只需 prefill 一次、KV 只需存一份（借助前缀缓存的 fork）。树结构天然表达了「共享前缀 + 各自发散」的语义；根节点承载共享部分，子节点承载各自的生成进度。

#### 4.3.2 核心流程

`n = 1`（最常见）与 `n > 1` 时的建树过程（发生在 `AddRequest` 里）：

```text
AddRequest:
  n = request->generation_cfg->n
  rsentries = []
  # ① 根节点：代表 prompt 前缀，分配 internal_id 与 rng_seed
  rsentries.emplace_back(request, num_models, id_manager.GetNewId(), rng_seed, ...)
  if n > 1:
      rsentries[0].child_indices.reserve(n)
      for i in 0..n-1:
          rsentries[0].child_indices.push_back(len(rsentries))
          # ② 第 i 个子分支：parent_idx=0，rng_seed 加偏移以保证各分支不同
          rsentries.emplace_back(request, num_models, GetNewId(),
                                 rng_seed + i + 1, ..., parent_idx=0)
  rstate = RequestState(rsentries, n, add_time_point)
  # 挂回指（避免循环引用，用裸指针）
  for rsentry in rstate.entries: rsentry->rstate = rstate
  request->rstate = rstate
```

\[ \text{entry 总数} = \begin{cases} 1, & n = 1 \\ n + 1, & n > 1 \end{cases} \]

每个 `RequestStateEntry` 内部又有一组 `mstates`（每模型一个 `RequestModelState`），形成四层包含：

```text
Request（不可变契约）
└─ RequestState（一棵树）
   └─ RequestStateEntry × (n+1)（树节点：一条分支）
      └─ RequestModelState × num_models（单模型视图：committed/draft token）
```

#### 4.3.3 源码精读

`RequestStateEntry`/`RequestState` 的设计动机与「`(n+1)` 个 entry、拓扑序」的完整说明：[cpp/serve/request_state.h:L150-L175](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L150-L175)——这段是理解整棵树的钥匙，务必精读。

`RequestStateEntryNode` 的树连接字段（`status` / `parent_idx` / `child_indices`）与每模型状态 `mstates`：[cpp/serve/request_state.h:L198-L217](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L198-L217)。

`RequestStateNode` 就是 `entries` 向量加 metrics 加后处理工作区：[cpp/serve/request_state.h:L278-L299](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L278-L299)。

建树代码：[cpp/serve/engine.cc:L704-L720](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L704-L720)——注意第 712 行 `rsentries[0]->child_indices.reserve(n)`、第 714 行把每个子的下标登记进根的 `child_indices`、第 715-717 行子节点构造时传入 `parent_idx=0`、且 `rng_seed + i + 1` 加偏移保证 n 条生成各不相同。

回指挂接（避免循环引用的裸指针）：[cpp/serve/engine.cc:L721-L726](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L721-L726)。

#### 4.3.4 代码实践

**实践目标**：把四层包含关系与 `n` 的关系在纸上画清楚。

**操作步骤**（源码阅读 + 画图）：

1. 设想一次 `n = 3`、且开启推测解码（2 个模型：主模型 + draft 小模型）的请求。
2. 对照 [engine.cc:L704-L720](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L704-L720) 算出：会创建几个 `RequestStateEntry`？每个 entry 的 `mstates` 数组长度是几？整棵树一共有几个 `RequestModelState` 对象？

**需要观察的现象**：建树时根节点的 `child_indices` 被填成 `[1, 2, 3]`，三个子的 `parent_idx` 全是 `0`，向量下标顺序 `0,1,2,3` 正好是拓扑序（根在前）。

**预期结果**：`n=3` → 4 个 entry（1 根 + 3 子）；每 entry 的 `mstates` 长度 = 2（主模型 + draft 模型）；共 `4 × 2 = 8` 个 `RequestModelState`。把这张图画出来即完成实践。无需运行。

#### 4.3.5 小练习与答案

**练习 1**：`n = 1` 时有几个 `RequestStateEntry`？它的 `parent_idx` 是什么？

> **答**：只有 **1** 个，它就是根节点，`parent_idx = -1`（无父）。此时没有分叉，树退化成单节点。

**练习 2**：为什么 vector 顺序必须是拓扑序（父在子前）？

> **答**：因为很多动作的后处理需要「先处理父节点、再处理子节点」（例如完成判定：一个父节点只有当所有 child 都 finished 时才能 finished，见 4.4.3）。拓扑序保证按 vector 线性扫描时，父一定先于子被访问，无需额外排序。

**练习 3**：`n` 条并行生成为什么各要不同的 `rng_seed`？

> **答**：若用同一个 seed，n 条分支从同一分布采样会得到**完全相同**的序列，失去「并行生成多条不同回答」的意义。代码用 `rng_seed + i + 1` 给每条分支独立 seed（[engine.cc:L715-L717](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L715-L717)）。

### 4.4 状态机与抢占

#### 4.4.1 概念说明

每个 `RequestStateEntry` 都带一个 `status` 字段，类型是三态枚举 `RequestStateStatus`：

```text
kPending  = 0   待处理：还没 prefill（或在抢占后被回退）
kAlive    = 1   活跃：已 prefill，正在 decode / 可继续 decode
kFinished = 2   已完成：达到停止条件，即将出队
```

这三个状态与 `EngineState` 的两个队列有大致（但非一一对应）的映射关系：

| status | 典型所在队列 | 含义 |
| --- | --- | --- |
| `kPending` | `waiting_queue` | 输入还没 prefill 完，等待 prefill 动作处理 |
| `kAlive` | `running_queue` | 已 prefill，参与 batch decode |
| `kFinished` | （即将从 `running_queue` 移除） | 命中 stop / 达到 max_tokens / 长度上限 |

注意「大致映射」的细节：一个请求（`Request`）在 `waiting_queue` 还是 `running_queue`，是**引擎级**的归属；而每个 `RequestStateEntry` 的 `status` 是**条目级**的。`n > 1` 时，同一请求的根 entry 与子 entry 可能处于不同 status——例如根（前缀）已 `kAlive`、某子分支还没 prefill 仍是 `kPending`。所以「请求进 running 队列」的判定是「**至少有一个** entry 转为 `kAlive`」。

#### 4.4.2 核心流程：正常生命周期

一个 `n = 1` 请求的完整状态流转：

```text
AddRequest:
   request 进 waiting_queue
   创建 1 个 entry，status = kPending（构造默认值）

NewRequestPrefill / BatchPrefill（prefill 动作）:
   ① 该 entry 的 status: kPending → kAlive
   ② 若该请求此前没有任何 alive entry → request 从 waiting_queue 挪进 running_queue
   ③ prefill 全部输入后，request 从 waiting_queue 移除

BatchDecode（decode 动作，每步一个 token）:
   每步 CommitToken 把新 token 追加进 committed_tokens
   检查停止条件（EOS / stop_str / max_tokens / 长度上限）

ActionStepPostProcess（步后处理）:
   命中停止条件的 entry: status → kFinished
   从 running_queue 移除、从 request_states 抹除、释放 KV
```

#### 4.4.3 核心流程：抢占（preemption）

当 decode/prefill 动作发现**显存不足以放下本批请求的 KV**时，它不会直接报错，而是调用 `PreemptLastRunningRequestStateEntry`——把 `running_queue` **队尾**（最低优先级）请求的**最后一个 `kAlive` entry**「抢」回去腾地方。抢占做的事很有讲究：

```text
PreemptLastRunningRequestStateEntry(estate, models, ...):
   req = running_queue.back()                      # 挑最低优先级
   找到该 req 最后一个 status==kAlive 的 entry（rsentry）
   rsentry->status = kAlive → kPending             # ① 状态回退
   for mstate in rsentry->mstates:
       mstate->RemoveAllDraftTokens(...)            # ② 清掉所有 draft token（白起草了）
       # ③ 关键：committed_tokens 不丢！把它们重新塞回 inputs，等下次重新 prefill
       把 committed_tokens 合并进 mstate->inputs
       mstate->num_prefilled_tokens = 0
   # ④ 释放该序列在模型里的 KV cache（或回收进 prefix cache）
   RecycleSequence / RemoveRequestFromModel
   # ⑤ 分配新的 internal_id（旧 KV 序列已废）
   if 该 entry 是根（preempt_rstate_idx==0）且整个请求已无任何 alive 分支:
       从 running_queue 移除
   if 该请求已完全不在任何队列:
       插回 waiting_queue 队首（优先重新调度）
```

要点：**抢占丢失的是 KV cache 和 draft token，但不丢失 committed_tokens**。被抢的请求带着「prompt + 已生成 token」重新回到 `waiting_queue` 队首，下次轮到它时会重新 prefill（理想情况下命中前缀缓存，开销不大）。这是一种用「时间换空间」的背压机制——宁可让某些请求慢一点，也不让整个 batch 崩掉。

#### 4.4.4 核心流程：完成与树的自底向上收敛

`n > 1` 时，完成的判定是**自底向上**的：一个叶子 entry finished 后，检查它的父节点——若父的**所有 child 都 finished**，父也置 finished 并释放，然后继续往上爬。根节点 finished 时，整个请求才从 `running_queue` 移除、从 `request_states` 抹除。这保证了「所有并行分支都结束，请求才算结束」。

#### 4.4.5 源码精读

三态枚举定义：[cpp/serve/request_state.h:L177-L182](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/request_state.h#L177-L182)。

`kPending → kAlive` 迁移 + 进 running 队列：[cpp/serve/engine_actions/batch_prefill_base.cc:L408-L422](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_prefill_base.cc#L408-L422)——第 408 行置 `kAlive`；第 413-418 行检查「此前是否已有 alive entry」，没有才 push 进 `running_queue`（避免重复入队）。

prefill 完成后从 waiting 移除：[cpp/serve/engine_actions/batch_prefill_base.cc:L444-L460](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_prefill_base.cc#L444-L460)——条件是「所有 entry 都非 pending 且无剩余 inputs」。

完成处理（置 `kFinished` + 自底向上爬树）：[cpp/serve/engine_actions/action_commons.cc:L184-L224](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L184-L224)——第 188 行叶子置 finished；第 194-215 行 while 循环向上检查「所有 child 是否都 finished」；第 217-224 行根 finished 后从 `running_queue` 与 `request_states` 移除。

抢占函数 `PreemptLastRunningRequestStateEntry`：[cpp/serve/engine_actions/action_commons.cc:L333-L427](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L333-L427)。关键行：
- L363 `rsentry->status = RequestStateStatus::kPending`（状态回退）；
- L365-L369 清 draft token 并释放槽位；
- L374-L401 把 `committed_tokens` 合并回 `inputs`（**这是「不丢已生成内容」的所在**）；
- L406-L410 释放/回收 KV；
- L412-L415 分配新 `internal_id`；
- L417-L424 视情况从 running 移除、插回 waiting 队首。

抢占函数的接口声明与契约说明：[cpp/serve/engine_actions/action_commons.h:L69-L84](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.h#L69-L84)。

#### 4.4.6 代码实践

**实践目标**：完整追踪一次「请求生命周期 + 一次抢占」的状态与队列变化，并定位 `draft_output_tokens` 在其中的角色。

**操作步骤**（源码阅读 + 画时间线）：

1. **正常生命周期时间线**：画一条横轴，标出下列时刻，并在每个时刻写出该请求的 `(所在队列, 某 entry 的 status, committed_tokens 长度)`：
   - T1：`AddRequest` 刚返回；
   - T2：`NewRequestPrefill` 把它 prefill 完；
   - T3：第一个 `BatchDecode` 跑完；
   - T4：命中 EOS，步后处理完成。
   对照源码：T1 看 [engine.cc:L697-L727](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L697-L727)；T2 看 [batch_prefill_base.cc:L408-L422](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/batch_prefill_base.cc#L408-L422) 与 L444-L460；T4 看 [action_commons.cc:L184-L224](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L184-L224)。

2. **抢占分支**：在 T3 之后另起一条「若显存不足」的支线，写出 `PreemptLastRunningRequestStateEntry` 执行后该请求的 `(所在队列, status, committed_tokens 长度, draft_output_tokens 长度)`。对照 [action_commons.cc:L333-L427](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L333-L427)。

3. **draft_output_tokens 的角色**：在 [action_commons.cc:L365-L369](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L365-L369) 确认抢占会清空 draft token；思考——为什么抢占时 draft token 直接丢弃，而 committed token 要保留？

**需要观察的现象**：

| 时刻 | 队列 | status | committed 长度 |
| --- | --- | --- | --- |
| T1（入队） | waiting_queue | kPending | 0 |
| T2（prefill 完） | running_queue | kAlive | 0 或 1（prefill 末尾常顺带采样首 token） |
| T3（decode 一步） | running_queue | kAlive | +1 |
| T4（命中 EOS） | 已移除 | kFinished | 不再增长 |
| T3 后抢占 | waiting_queue（队首） | kPending | **保留不变**，draft 清空 |

**预期结果**：你能讲清三件事——① 正常路径 `kPending→kAlive→kFinished` 与队列迁移的对应；② 抢占「**丢 KV 与 draft、留 committed**」的语义，以及它把请求放回 `waiting_queue` 队首而非队尾的原因（队首 = 尽快重调度，减少被抢请求的饥饿）；③ `draft_output_tokens` 是推测解码的「临时草稿」，尚未被大模型 verify 认可，故抢断时可直接丢弃、不必保留。结论可从源码直接读出，运行验证为「待本地验证」（可在 `action_commons.cc` 的抢占函数入口/出口加 `LOG_INFO` 打印 `request id / status / committed_tokens.size() / draft_output_tokens.size()` 实地观察）。

#### 4.4.7 小练习与答案

**练习 1**：被抢占的请求为什么被插回 `waiting_queue` 的**队首**而不是队尾？

> **答**：队首意味着下一次 prefill 动作会优先捡起它（[action_commons.cc:L421-L423](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L421-L423) 用 `insert(begin(), ...)`）。被抢的请求已经等了一阵子、可能已有用户在等结果，把它放队首可减少饥饿与尾延迟，是一种「补偿」。

**练习 2**：`n > 1` 时，如果 3 条分支里有 2 条已 finished、1 条还在 decode，根节点的 status 是什么？什么时候根才 finished？

> **答**：根仍是 `kAlive`（或其所处状态）。只有当**全部 3 个 child 都 finished** 时，后处理的 while 循环才会把根也置为 `kFinished` 并释放（[action_commons.cc:L194-L215](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L194-L215)），整个请求才从引擎移除。

**练习 3**：抢占时为什么必须给被抢的 entry 分配一个**新的** `internal_id`？

> **答**：因为旧 `internal_id` 对应的 KV cache 序列已被释放/回收（[action_commons.cc:L406-L410](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L406-L410)）。当请求重新 prefill 时，它要在模型里建立一条**全新**的 KV 序列，必须用一个新 id 来标识，避免与已失效的旧序列冲突（[action_commons.cc:L412-L415](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L412-L415)）。

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「**给一个请求写一生**」的追踪任务。

**场景**：用户发来一个 `n = 2`、开启推测解码（主模型 + 1 个 draft 小模型）、`temperature = 0.7`、`max_tokens = 50` 的请求；中途因显存紧张被抢占一次，最终两条分支先后命中 stop_str 结束。

**任务**：

1. **数据结构层**：算出 `AddRequest` 时会创建几个 `RequestStateEntry`、几把 `rng`、共几个 `RequestModelState`，并画出这棵 entry 树（标出每个节点的 `parent_idx` 与 `child_indices`）。对照 [engine.cc:L700-L720](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L700-L720)。
2. **状态机层**：为「分支 A」画一张状态迁移图，标出 `kPending → kAlive →（抢占）kPending → kAlive → kFinished` 的全部迁移，以及每次迁移时 `committed_tokens` 与 `draft_output_tokens` 的长度变化。标注每步迁移分别由哪个函数触发（`AddRequest` / `batch_prefill_base` 的 `kAlive` 赋值 / `PreemptLastRunningRequestStateEntry` / `ActionStepPostProcess`）。
3. **抢占语义层**：用一段话解释「为什么抢占后分支 A 的用户可见输出**不会丢**、但吞吐会暂时下降」——明确指出是哪个字段保证了「不丢」（`committed_tokens` 被合并回 `inputs`），是哪个动作导致了「吞吐下降」（重新 prefill + 可能未命中前缀缓存）。
4. **推测解码层**：解释分支 A 在被抢占那一刻，`draft_output_tokens` 里的内容为什么**必须**被清空，而不能「留着下次接着用」。提示：draft token 绑定了显存槽位（`draft_token_slots`）与 KV 树父子关系，而这些随 KV 释放一起失效了。

**验收标准**：你能不看讲义，对着一台没读过 MLC 源码的同事讲清「一个请求在引擎里是怎么从 `Request` 变成一棵 `RequestState` 树、每个节点上的 token 怎么增减、状态怎么跳转、被抢了为什么不丢」，并能在源码里指出每一步对应的函数与行号。

## 6. 本讲小结

- `Request` 是用户提交的**不可变契约**（`id` / `inputs` / `prompt_tokens` / `generation_cfg`），设计成不可变是为了能在节点间转发重启（分离式推理）。引擎只回填 `prompt_tokens` 与状态回指 `rstate`。
- 生成进度由状态层承载：`RequestState`（一棵树）→ `RequestStateEntry`（树节点＝一条生成分支）→ `RequestModelState`（单模型视图）。`n > 1` 时建 `(n+1)` 节点的树（1 根 + n 子），共享 prompt 前缀。
- `RequestModelState` 是两种 token 的家：`committed_tokens`（已敲定、不可撤销）与 `draft_output_tokens`（推测解码的临时草稿）。两者由 `CommitToken` / `AddDraftToken` 分别写入，井水不犯河水。
- `RequestStateStatus` 三态机 `kPending → kAlive → kFinished` 大致对应 `waiting_queue → running_queue → 出队`；条目级 status 与请求级队列归属是两层，`n>1` 时同请求各分支可处不同状态。
- 抢占是「时间换空间」的背压：显存不足时把队尾请求回退为 `kPending`、清空 draft、释放 KV，但**把 `committed_tokens` 合并回 inputs** 保留下来，插回 `waiting_queue` 队首优先重调度——丢的是 KV 和草稿，不丢用户产出。
- 完成判定自底向上：叶子先 finished，父节点当所有 child 都 finished 时才 finished，根 finished 时整个请求出队。

## 7. 下一步学习建议

本讲把「请求与状态」的数据结构讲透了，但还有几个相邻主题值得接着读：

1. **分页 KV cache 与前缀缓存**：本讲反复提到「释放/回收 KV」「fork 前缀」，但没讲 KV 到底怎么存储、前缀缓存怎么命中。这正是 [u10-l1 分页 KV 缓存模型接口](u10-l1-paged-kv-cache.md) 与 [u10-l2 前缀缓存与 Radix Tree](u10-l2-prefix-cache-radix-tree.md) 的主题——读完你会明白抢占时 `RecycleSequence` 和重新 prefill 时 `ForkSequence` 背后的物理结构。
2. **推测解码动作链**：本讲解释了 `draft_output_tokens` 是什么、存在哪，但「小模型怎么起草、大模型怎么 verify、接受的 token 怎么提交进 KV」要落到动作层，见 [u10-l4 推测解码动作链](u10-l4-speculative-decoding.md)。
3. **采样器**：`committed_tokens` 里的每个 `SampleResult` 是怎么从 logits 采样出来的、top-p 怎么处理、CPU 与 GPU 采样有何差别，见 [u10-l3 采样器：CPU 与 GPU](u10-l3-sampler.md)。
4. **想直接看代码**：建议按 `AddRequest`（[engine.cc:L668](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine.cc#L668)）→ `NewRequestPrefill` → `BatchDecode` → `ActionStepPostProcess`（[action_commons.cc:L62](https://github.com/mlc-ai/mlc-llm/blob/a2bcc5c86678b72a86b7aadc29b643a5ce63c747/cpp/serve/engine_actions/action_commons.cc#L62)）的顺序通读一遍，把本讲的状态迁移在脑中「跑」一遍。
