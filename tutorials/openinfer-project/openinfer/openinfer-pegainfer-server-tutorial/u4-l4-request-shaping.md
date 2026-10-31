# u4-l4 · 请求整形：history 边界测量与 reasoning effort 规范化

## 1. 本讲目标

上一讲（u4-l2）我们看到 vLLM 装配骨架在路由外面包了三层洋葱，其中两层是「请求整形」中间层：`mark_history` 与 `normalize_chat_requests`。本讲把这两层拆开精读。学完后你应该能够：

1. **复述边界测量算法**：中间层如何只渲染最后一条消息、比较「带 / 不带 generation prompt」两次 tokenization 的公共前缀得到尾长 `T`，桥侧如何据此置 `history_tokens = prompt_len - T`。
2. **解释 `history_marks`（新者在先）的语义**：它为什么是「更早回合各自曾经的 `history_tokens`」，保留多快照的调度器如何用它从更早边界恢复下一轮。
3. **说明 effort 规范化为什么必须放在最外层**：边界测量要用「路由真正渲染用的那个 effort 值」去渲染，顺序错了 `T` 就可能量错。
4. 读懂这两层「读不懂就原样放行」的降级哲学，以及 `add_generation_prompt=false` / `continue_final_message=true` 时 `T=0`、整个 prompt 都是 history 的含义。

## 2. 前置知识

- **axum 中间层（middleware layer）**：axum 允许用 `router.layer(...)` 给一组路由外包一层处理函数；请求先穿过外层再到达路由，响应再穿出来。多层 `layer` 的包裹顺序是「后包的在外」——本讲的洋葱顺序就是靠调用顺序写出来的。
- **chat 模板渲染（chat template rendering）**：OpenAI 风格的 `/v1/chat/completions` 请求带一个 `messages` 数组；服务端要用模型自带的 Jinja 风格 chat 模板把它渲染成模型真正看到的 prompt 文本，再分词成 token 序列。模板最后通常还会追加一段「generation prompt」（如 Qwen3.8 的 `<|im_start|>assistant\n<think>\n`），意思是「轮到 assistant 说话了」。
- **`vllm_xargs` 侧信道**：请求 JSON 里的一个对象字段，不参与采样语义，专门用来把自定义键值对原样带到引擎侧（桥在 `sampling_params.extra_args` 里读它）。本讲测出的两个数就藏在这里过境。
- **前缀快照与恢复（快照续跑）**：u3-l3 介绍过 step 契约的 `Request`。其中 `history_tokens` / `history_marks` 是为「状态不能回滚的调度器」（如线性注意力 / recurrent 线）准备的前缀坐标：会话下一轮会逐字重复上一轮的历史，若在历史末尾存了状态快照，下一轮就能从快照处继续而不必重算 prefill。
- **公共前缀（LCP）**：两个序列逐位比较、取相同部分的最长长度。本讲的尾长 `T` 就是用 LCP 算出来的差值。
- **`reasoning_effort`**：OpenAI chat 请求的一个顶层字段，取值 `none|minimal|low|medium|high|xhigh|max`，控制「思考多用力」；它会作为模板变量进入 chat 模板渲染。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `../pegainfer-frontend/src/vllm/history.rs` | 本讲主角一：`Boundary` 测量器 + `mark_history` 中间层，测出 `generation_prompt_tokens`（尾长 T）与 `history_marks` 写入 `vllm_xargs` |
| `../pegainfer-frontend/src/vllm/reasoning_effort.rs` | 本讲主角二：启动期探测模板词表 + `normalize_chat_requests` 中间层，做 effort 别名改写 |
| `../pegainfer-frontend/src/engine/step.rs` | step 契约的 `Request`：`history_tokens` / `history_marks` 字段的权威文档注释 |
| `../pegainfer-frontend/src/vllm/bridge/stepped.rs` | 消费点：stepped 桥从 `extra_args` 读出 T 与 marks，置 `history_tokens = prompt_len - T` 并过滤 marks |
| `../pegainfer-frontend/src/vllm/mod.rs` | 装配点：加载 backends、构造 aliases 与 Boundary、决定两层洋葱的包裹顺序 |
| `../pegainfer-frontend/examples/echo-server.rs` | 无 GPU 的活体标本，本讲实践的载体 |

## 4. 核心概念与源码讲解

### 4.1 Boundary / history_tokens 测量

#### 4.1.1 概念说明

一个 chat prompt = 「渲染好的会话」+「模板的 generation prompt 尾巴」。同一会话的下一轮请求会把上一轮的回复**当作 history** 重新渲染——而 history 的渲染方式和 generation prompt 的读法不同，所以两轮 prompt 的最长公共前缀**在最后一条消息结束处截止**。一个状态不能回滚的调度器（recurrent 线）想服务下一轮，只能从这个边界上的快照恢复。

问题在于：上游 vendored 的 chat 路由后面的任何代码都**看不到请求文本**，也就不知道这个边界在哪。于是这一层站在路由**前面**把边界量出来，经 `vllm_xargs` → 桥，最终落到 [`crate::engine::Request::history_tokens`](../pegainfer-frontend/src/engine/step.rs#L60)。

关键直觉：**不需要渲染整个会话**。凡是「generation prompt 追加在 messages 之后」的模板（frontend 服务的所有模板都是），尾长 T 只取决于最后一条消息。所以量法是本地的、按请求一次的：把**最后一条消息单独**渲染两遍——一遍带 generation prompt、一遍不带——两个 token 序列的公共前缀之后的差值就是 T。

[history.rs:L1-L24](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L1-L24) 这段模块文档把「为什么边界存在、为什么只渲染最后一条消息、T 如何过境」讲得一清二楚，是全模块最好的一手材料。

#### 4.1.2 核心流程

测量 T（`generation_prompt_tokens`）：

```text
输入: chat body (JSON)
1. chat_options(body):
     由 add_generation_prompt / continue_final_message 折算 GenerationPromptMode
     并带出 chat_template / reasoning_effort / chat_template_kwargs 等模板控制
2. 若该 mode 不产 generation prompt（NoGenerationPrompt / ContinueFinalAssistant）→ T = 0
3. last_message(body): 取 messages 的最后一条（读不出 → 整体放弃，None）
4. with    = tokenize(render([最后一条消息], mode = 原样))
   without = tokenize(render([最后一条消息], mode = NoGenerationPrompt))
5. shared = LCP(with, without)      # 逐 token 比较
6. T = |with| − shared
```

写成公式：

\[ T = |W| - \operatorname{LCP}(W, W') \]

其中 \(W\) 是带 generation prompt 的 tokenization，\(W'\) 是不带的。**跨界 token（straddling token）计入 T**——若某个 token 同时覆盖「历史结尾」和「generation prompt 开头」，公共前缀会提前一位断开，边界向历史方向退一个 token，绝不落进 generation prompt 里。

桥侧消费（`prompt_len` 只有桥知道，因为它看到的是完整 prompt 的 token id）：

\[ h = \text{history\_tokens} = L - T \qquad (L = |\text{prompt\_tokens}|) \]

`checked_sub` 保证 T 放不进整个 prompt 时得到 `None`（「一个 prompt 装不下的计数不是边界」）。

两个 T=0 的特例：`add_generation_prompt=false` 与 `continue_final_message=true` 都不渲染 generation prompt，于是 T=0、**整个 prompt 都是 history**——这正是 u4-l2 提过的「带/不带 generation prompt」两个渲染分支在测量侧的镜像。

降级规则：body 不是 JSON、没有 `messages`、最后一条消息渲染失败 → 原样放行，调度器看不到任何边界（`history_tokens = None`），路由自己会对它报错。

#### 4.1.3 源码精读

**携带 T 过境的键与快照条数上限**（[history.rs:L84-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L84-L92)）：定义了 `vllm_xargs` 的两个键 `generation_prompt_tokens` / `history_marks` 和最多测 8 个早期边界（`MARKS`）。

**测量器本体**（[history.rs:L95-L106](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L95-L106)）：`Boundary` 只持有路由自己的 renderer 和 tokenizer——**测量必须用与路由相同的渲染器和分词器**，否则量出来的边界对不上路由实际产出的 prompt。

**算法主函数**（[history.rs:L110-L130](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L110-L130)）：先由 `chat_options(body).add_generation_prompt()` 判定是否根本不产 generation prompt（是则直接 `Some(0)`）；再取最后一条消息，渲染两次，`zip + take_while` 数公共前缀，返回差值。注意第 117-123 行不带 generation prompt 的那次渲染只是把 `generation_prompt_mode` 换成 `NoGenerationPrompt`，**其余选项逐字段原样**——同一模板、同一 kwargs，只差尾巴。

**渲染管线**（[history.rs:L172-L198](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L172-L198)）：`tokens` 把单条消息包成切片去渲染（工具上下文用空默认——generation prompt 旁边这条消息长什么样不依赖 tools）；`render` 先 `renderer.render(...)`，若产物已是 `Prompt::TokenIds` 直接用，是文本则 `tokenizer.encode(&text, false)` 分词。任何一步 `Err` 都折叠成 `None`。

**mode 折算**（[history.rs:L203-L221](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L203-L221)）：`chat_options` 复刻路由对三个模板控制字段的解析——`(add_generation_prompt, continue_final_message)` 三分支折算 `GenerationPromptMode`；同时把 `reasoning_effort`（第 217 行）和 `chat_template_kwargs` 带进 `ChatOptions`。**这一行就是「effort 层必须在最外层」的物证**：测量器会读到 effort 值并用它渲染。

**中间层入口与异步纪律**（[history.rs:L390-L429](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L390-L429)）：`mark_history` 在有 `Boundary` 时挂中间层（backends 没加载就原样返回 router）；`mark_request` 只拦 `POST /v1/chat/completions`，先在 `BODY_LIMIT` 内缓冲 body，再第 414-419 行把测量丢进 `spawn_blocking`——注释点明：对 1M token 的长会话，渲染 + 分词最多 8 个前缀要花**秒级**，不能压在 async worker 上。改写成功后重写 `Content-Length`。

**写入 `vllm_xargs`**（[history.rs:L435-L450](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L435-L450)）：`mark_generation_prompt` 解析 body → 测 T（失败即整体放弃）→ 测 marks → 在 `vllm_xargs`（没有就建一个空对象）里**插入/覆盖**这两个键 → 重新序列化。注意：调用方自己带的这两个键会被覆盖——文档注释说得很直白，它们是「本服务器对自己模板的测量，不是请求参数」。

**桥侧置 `history_tokens`**（[stepped.rs:L391-L403](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L391-L403)）：桥从 `sampling_params.extra_args` 读出 `GENERATION_PROMPT_TOKENS`，用 `prompt_tokens.len().checked_sub(tail)` 得到 `history_tokens`。注释解释了为什么减法在桥侧做：chat 层只见过 prompt 的**文本**，只有桥看到完整 prompt 的 **token id 序列**，才知道 `L` 是多少。

**契约侧的权威定义**（[step.rs:L51-L67](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L51-L67)）：`Request::history_tokens` 的文档注释即语义基准——「渲染会话消息的前导 prompt token 数；其后跟随模板的 generation prompt。会话的下一轮逐字重复这些 token，所以在那里快照状态的调度器可以从快照服务那一轮。`None` 表示前端没测（一个 completion，或 chat 层读不出的 body）」。

#### 4.1.4 代码实践

**实践目标**：在一个受控的最小模板上手工算出 T，验证你对算法的理解，与仓库自带的单元测试对答案。

1. **操作步骤**：
   - 读 [history.rs:L467-L533](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L467-L533) 的测试脚手架：`Template` 把每条消息渲染成 `<role> content <end>`，结尾追加 ` <assistant> <think>`；`Words` 分词器把「每个空白分隔的词」当一个 token，token id 就是词长。
   - 手工推算 body `{"messages":[{"role":"user","content":"hi there"}]}`：
     - `with` = `<user> hi there <end> <assistant> <think>` → 6 个词；
     - `without` = `<user> hi there <end>` → 4 个词；
     - LCP = 4，T = 6 − 4 = **2**（`<assistant>` 与 `<think>`）。
   - 再推算跨界例 `content: "hi ~"`：模板对以 `~ <end>` 结尾的文本把尾巴直接黏上去（无空格），`with` 的最后一个词变成 `<end><assistant><think>` **一个** token；LCP 断在第 3 个词，T = 4 − 3 = **1**。
   - 运行 `cargo test -p pegainfer-frontend --lib vllm::history`（无 GPU 依赖）。
2. **需要观察的现象**：测试 `the_generation_prompt_is_the_tail_the_history_rendering_lacks` 断言 T=2；`a_token_straddling_the_boundary_is_counted_as_generation_prompt` 断言跨界例 T=1；`a_request_without_a_generation_prompt_is_all_history` 断言两个 T=0 特例。
3. **预期结果**：你的手工推算与断言一致，全部测试通过（结果待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么测量只渲染最后一条消息就是精确的？什么时候会不精确？
**答案**：对所有「generation prompt 追加在 messages 之后」的模板（frontend 服务的全部模板），最后一条消息旁边长什么样与更早的消息无关，所以两次渲染的差值只由它决定；只渲染一条消息把开销从「整个会话」降到「一条消息」。不精确的形态见模块文档第 41-43 行：若某模板在更长会话里把同一边界分词得不同，量出的数会对不上实际 prompt——代价只是一次快照 miss，没有别的。

**练习 2**：一个 token 跨在边界上时，边界向哪边退？为什么这样选？
**答案**：向历史方向退一位（跨界 token 计入 T，所以 `history_tokens = L − T` 变小）。这样保证快照点永远不落进 generation prompt 内部——快照必须停在「属于会话历史的 token」上，下一轮才会逐字重复它。

**练习 3**：调用方在 `vllm_xargs` 里自己塞了 `generation_prompt_tokens: 99` 会怎样？
**答案**：被本服务器的测量值覆盖（`mark_generation_prompt` 无条件 insert）。理由：这两个键是服务器对**自己模板**的测量，不是请求参数；测试 `the_mark_joins_the_callers_xargs_and_replaces_its_own_key`（[history.rs:L577-L586](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L577-L586)）同时验证了调用方其它键（如 `kv_transfer_params`）原样保留。

### 4.2 history_marks 测量

#### 4.2.1 概念说明

`history_tokens` 只给「本请求的会话历史」找到一个恢复点。但会话里**更早的每一轮**也各有自己的边界——每个「后面跟着 assistant 消息」的消息处，都结束过某个更早的请求。一个保留多个快照的调度器（如 pooled tier）可以从其中任何一个恢复下一轮，多一个候选就多一次命中前缀缓存的机会。

于是这一层顺手把这些更早边界也量出来，作为 `vllm_xargs.history_marks`，**新者在先**（newest first）。每个 mark 的定义：**到该消息为止的会话前缀，按不带 generation prompt 渲染后的 token 数**——恰好就是「当初携带那个轮次的请求」的 history_tokens（见 [history.rs:L32-L43](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L32-L43) 的模块文档）。

代价控制：只测最新的 `MARKS = 8` 个，且每个 mark 都要渲染一次「到该处为止的会话」——一条长 agent trace 只为少数几个边界付费，而不是每轮一次。容错：模板在更长会话里把某边界分词得不同，只会产出一个「没有快照落在其上」的 mark，代价是 miss，仅此而已。

#### 4.2.2 核心流程

```text
输入: chat body (JSON)
前置: 读出全部 messages（任何一条读不出 → marks 为空）
      读出请求自己的 tools / tool_choice / parallel_tool_calls
      （tools 与 tool choice 会渲染进会话，必须按原请求的样子渲染）
对 i 从 len−2 递减到 0（新者在先）:
  若 messages[i+1].role == assistant:        # 该消息之后开始了 assistant 回复 → 一个轮次在此结束
    turn   = messages[..=i]                   # 到该消息为止的会话前缀
    m      = |render(turn, NoGenerationPrompt, 本请求的 tool 上下文)|
    保留 m > 0
至多取 8 个（新者优先）
```

桥侧二次过滤（[stepped.rs:L406-L416](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L406-L416)）：只保留 \(0 < m < h\) 的 mark——**严格小于本请求的 `history_tokens`**。这与契约文档（step.rs 第 65 行 "strictly less than `history_tokens`"）一一对应：等于或超过当前历史的 mark 不可能是更早轮次的边界。

#### 4.2.3 源码精读

**mark 的判定规则**（[history.rs:L138-L168](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/history.rs#L138-L168)）：函数体完整实现了上面的流程。三个细节值得停一下：

- 第 139-147 行：先解析 `tool_choice`（`auto/none/required` 或具名 function，第 346-359 行的 `tool_choice` 帮手），任何读不动就返回空——与 T 的「只看最后一条」不同，marks 要渲染**整个前缀**，所以要求**每条**消息都可读（第 234-240 行的 `messages` 帮手）。
- 第 152-154 行：`(0..len-1).rev()` 加 `filter(messages[i+1] 是 assistant)`——「消息后面跟着 assistant 消息」即「一个轮次在此结束」；`.rev()` 保证新者在前。
- 第 155-158 行：渲染 mode 固定为 `NoGenerationPrompt`——早期轮次当初就是以「历史身份」被渲染的，不带尾巴；第 159-167 行：带上请求自己的 `ResolvedToolContext`（tools + choice + parallel）渲染 `turn`，`filter(m > 0)` 后 `take(MARKS)`。

**桥侧过滤与落位**（[stepped.rs:L404-L416](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/bridge/stepped.rs#L404-L416)）：读 `extra_args` 里的 marks 数组，逐个 `as_u64`，`filter(|&m| m > 0 && history_tokens.is_some_and(|h| m < h))`；随后第 443-444 行把它们填进 `Request { history_tokens, history_marks, .. }` 提交给调度器。

**契约定义**（[step.rs:L61-L67](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L61-L67)）：`history_marks: Vec<usize>`——「会话更早轮次结束的地方，新者在先：携带它们的那些请求的 `history_tokens`，由 chat 层从 messages 重新测出。每个都是更早轮次的快照可能被找到的位置，供一个把快照保留到产出它的实例之外的调度器（pooled tier）使用；严格小于 `history_tokens`。前端没测时为空。」

**测试样例**（[history.rs:L589-L600](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L589-L600)）：六条消息 `system s / user "a b" / assistant c / user d / assistant "e f" / user g`。两个边界：`user "a b"`（后跟 assistant c）与 `user d`（后跟 assistant "e f"）。新者在先：先测到 `user d` 为止的前缀 = `<system> s <end><user> a b <end><assistant> c <end><user> d <end>`，因 `<end>` 与下一角色黏连共 **10** 词；再测到 `user "a b"` 为止 = **6** 词。断言 `history_marks == [10, 6]`。

**带工具的样例**（[history.rs:L603-L612](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L603-L612)）：请求带 `tools: [grep]`，历史里含 assistant 的 tool_call 与 tool 响应。断言 `[11, 7]`：`<tools> grep <end>` 头也按请求自己的样子渲染进每个前缀——注释还点出「当初那个请求测到的 history 是 9（含 2 词的 generation prompt）」，而 mark 是**不带尾巴**重测的 7。

#### 4.2.4 代码实践

**实践目标**：亲手为一个三轮会话算出 marks 序列，理解「新者在先」与「不带尾巴」两条规则。

1. **操作步骤**：
   - 用 4.1.4 的 `Template`/`Words` 脚手架（同一分词规则：空白分词、`<end>` 与下一词黏连成一个 token），对下面这个 body 做手工推算：
     ```json
     {"messages":[
       {"role":"user","content":"a"},
       {"role":"assistant","content":"b"},
       {"role":"user","content":"c"},
       {"role":"assistant","content":"d"},
       {"role":"user","content":"e"}]}
     ```
   - 逐个写出候选边界（后面跟 assistant 的消息）、渲染每个前缀、数 token。
   - 对照测试 `the_earlier_turns_end_where_an_assistant_message_follows` 的思路检查你的方法。
2. **需要观察的现象**：你的推算应该得到两个 mark，新者在先，且每个都严格小于该请求的 `history_tokens`（本例 T=2，`history_tokens` = 总长 − 2）。
3. **预期结果**：两个 mark 分别为「到第一个 user 消息为止」与「到第二个 user 消息为止」的前缀长度，顺序为先大后小；推算过程待本地与单元测试核对（可自行在草稿测试里复用 `mark()` 帮手验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么「消息后面跟着 assistant 消息」就是轮次边界？
**答案**：一次 chat 请求的 messages 总是以「等待 assistant 回复」收尾；会话历史的形状必然是「…、某消息、assistant 回复、某消息、assistant 回复、…、最后一条消息（新请求）」。所以每个被 assistant 消息**紧跟**的消息，都是某个更早请求的最后一条消息——那个请求的历史就在它结束的地方。

**练习 2**：marks 为什么必须新者在先，而不是升序？
**答案**：两层理由。工程上，`take(MARKS)` 只保留最新的 8 个——长 agent trace 有几十个边界，对下一轮最有价值的是最近的那些（更可能已有快照、也更可能被后续会话复用）；语义上，契约文档把它定义为消费顺序：调度器从新到旧尝试恢复点，第一个命中的就是最深的可复用前缀。

**练习 3**：一个 mark 恰好等于本请求的 `history_tokens` 会怎样？
**答案**：被桥侧过滤掉（要求严格小于）。等于意味着「更早轮次的边界与本轮历史边界重合」——只有会话没有任何新增内容时才可能，此时它不提供任何额外的恢复点，保留它反而会让调度器做无意义的查找。

### 4.3 normalize_chat_requests：effort 别名规范化与洋葱顺序

#### 4.3.1 概念说明

OpenAI 的 `reasoning_effort` 词表是 `none|minimal|low|medium|high|xhigh|max`，但**具体 checkpoint 的 chat 模板可能只认更窄的词表**：Qwen3.8 的模板只接受 `xhigh|medium|low`，其它值直接 `raise_exception`——而 pinned 的 vendored 前端把这次 raise 变成 **500 而不是 400**。调用方发一个完全合法的 OpenAI 值 `high`，却得到服务器内部错误。

这一层的职责：在请求到达路由**之前**，把模板不认识的值改写成语义最近的别名（`high`/`max` → `xhigh`，`minimal` → `low`）。两条设计纪律让它不至于变成「拍脑袋映射」：

1. **改写集由被服务模板自己裁决**：启动时用同一渲染器对每个候选值渲染一个最小请求，只有「模板拒绝 from 且接受 to」的别名才启用。模板本来就接受 OpenAI 值的线（今天的所有非 Qwen3.8 线，包括 Kimi K3）一个改写都不启用，调用方的值逐字到达。
2. **探测有前置闸门**：只有 config.json（顶层或 `text_config`）带 `output_gate_type` 标记的 Qwen3.8 一类存档才值得探测，其它线零启动开销。

两条**故意不改写**的通道（见 [reasoning_effort.rs:L20-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L20-L26)）：显式的 `chat_template_kwargs.reasoning_effort`（调用方在直接对模板说话）与逐请求的 `chat_template` 覆盖（其词表启动探测从未量过）。两者保留完全控制权，代价是值越界时由它们自己承担拒绝。

**洋葱顺序**（[mod.rs:L493-L500](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L493-L500)）：`normalize_chat_requests(mark_history(extend_router(router)))`——effort 层包在 history 层**外面**。注释一句话点题：*"Effort normalization is outermost so the boundary measures the template under the value the route will render with."* 4.1.3 里我们看到边界测量的 `chat_options` 会读 `reasoning_effort` 并用它渲染；若 effort 层在内层，边界会在**改写前**的值下测 T——而模板的 generation prompt（如思考分支的开合）可以随 effort 变化，T 就可能量错。外层先改写，内层量到的才是路由真正要渲染的世界。

#### 4.3.2 核心流程

启动期（装配骨架里，见 [mod.rs:L416-L435](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L416-L435)）：

```text
load_model_backends(model_id)        # 失败仅 warn 降级：整形层整体关闭，服务照常
  ├─ probe_effort_aliases(model_id, backends)
  │    ├─ probe_warranted? 读 config.json（顶层或 text_config）找 output_gate_type 标记
  │    │    无标记 / 无本地 config（HF id）→ 空表，零改写
  │    ├─ 对每个候选值 {high, xhigh, max, minimal, low} 渲染一个最小请求
  │    └─ 别名 (from→to) 启用 ⟺ 模板拒绝 from ∧ 接受 to
  └─ Boundary::new(backends)         # 测量器；backends 为 None 时 mark_history 不挂
```

请求期（`normalize_request`，仅 `POST /v1/chat/completions`）：

```text
缓冲 body（上限 BODY_LIMIT = 128 MiB）
rewrite_reasoning_effort(body, aliases):
  body 不是 JSON / 无 reasoning_effort / 值非字符串 / 值不在别名表 → 原样返回
  body 带字符串型 chat_template 覆盖 → 原样返回（覆盖模板的词表不归我们猜）
  否则 reasoning_effort ← 别名目标值，重写 body
改写过 → 重写 Content-Length
放行给内层（history 测量层）
```

体读失败的错误契约（`body_read_error`）：超长独占 **413**，其它读失败保持 **400**，两者都用上游同款 OpenAI 风格 JSON 错误信封（`invalid_request_error`），保证状态类别与成因都活着。

#### 4.3.3 源码精读

**候选别名与探测值**（[reasoning_effort.rs:L69-L87](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L69-L87)）：`CANDIDATE_ALIASES = [("high","xhigh"), ("max","xhigh"), ("minimal","low")]`；`probe_values` 收齐所有源与目标去重——每个候选只凭**自己的**渲染判定武装，不从 config 想当然。

**入口与前置闸门**（[reasoning_effort.rs:L99-L147](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L99-L147)）：`probe_effort_aliases` 先过 `probe_warranted`——读模型目录的 config.json，在顶层或 `text_config` 下找 `output_gate_type`；目录都没有（HF id）不是错误，直接跳过。backends 没加载也只有 warn 没有 mapping——「没有可读的模板判定，就没有改写任何东西的正当性」。

**模板探测**（[reasoning_effort.rs:L152-L190](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L152-L190)）：`probe_template` 对每个候选值渲染一条 `probe_request`（单条 user 消息、`reasoning_effort` 塞进 `ChatOptions`），`is_ok()` 即「接受」；别名保留条件 `!accepts(from) && accepts(to)`。第 169-190 行的最小请求把其它字段全部放在「vendored 路由对纯文本 chat 的默认解析值」上——探测世界与请求世界同构。

**中间层与改写函数**（[reasoning_effort.rs:L195-L226](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/reasoning_effort.rs#L195-L226) 与 [L259-L285](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L259-L285)）：别名表为空时 `normalize_chat_requests` 原样返回 router（零开销）。`rewrite_reasoning_effort` 第 268-273 行的 `chat_template` 覆盖守卫注释值得逐字读：别名是**默认模板**的判定，逐请求覆盖是另一张有自己词表的模板——按默认模板的权威去猜覆盖模板，可能把覆盖本可接受的值改写成它反而拒绝的值。

**错误信封**（[reasoning_effort.rs:L233-L252](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L233-L252)）：`body_read_error` 复刻 `vllm_server` 的 `ApiError::to_error_response` 形状；`LengthLimitError` 独占 413，注释说明这层出现之前路由报的就是 400，非超长读失败保持该类别。

**探测的两组反例测试**（[reasoning_effort.rs:L427-L459](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L427-L459)）：`GUARDED_TEMPLATE`（Qwen3.8 原生守卫形状，只认 `xhigh|medium|low`）武装全部三条别名；`ACCEPTING_TEMPLATE`（带标记的 config 却用自定义模板接受 `low|high|max`）只武装 `minimal→low`——**标记只是「值得探测」，不是「必然改写」**；无标记或无本地 config 则探测整个跳过。

**洋葱装配点**（[mod.rs:L416-L435](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L416-L435) 与 [L493-L500](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/mod.rs#L493-L500)）：backends 由装配骨架加载一次（失败仅 warn、整形降级，u4-l2 讲过）；第 434-435 行在同一处派生 effort 别名表与 Boundary；第 496-499 行在 `serve_with_router_extension` 的回调里按「effort 最外、history 次外、用户 extend_router 最内」包洋葱。

#### 4.3.4 代码实践

**实践目标**：亲眼看到「别名由模板裁决」而不是由标记裁决。

1. **操作步骤**：
   - 读 [reasoning_effort.rs:L371-L377](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L371-L377) 的两个模板 fixture：`GUARDED_TEMPLATE`（守卫在 thinking 分支内，只认 `xhigh|medium|low`）与 `ACCEPTING_TEMPLATE`（接受 `low|high|max`）。
   - 手工推演两个模板各自的探测结果：对每个值 `{high, xhigh, max, minimal, low}` 判定接受/拒绝，再套用 `!accepts(from) && accepts(to)`。
   - 运行 `cargo test -p pegainfer-frontend --lib vllm::reasoning_effort`（无 GPU，fixture 在 tempdir 里自建）。
2. **需要观察的现象**：守卫模板武装 `[("high","xhigh"), ("max","xhigh"), ("minimal","low")]`；接受模板只武装 `[("minimal","low")]`；无标记 config 与不存在的 HF id 都得到空表。
3. **预期结果**：三个探测测试全部通过，与你的手算一致（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么探测只在带 `output_gate_type` 标记的存档上跑，而不是所有模型都跑一遍？
**答案**：省启动开销只是次要原因；更根本的是**正当性**：别名表的语义是「这个模板拒绝 from、接受 to」。绝大多数线的模板接受全部 OpenAI 值，探测结果必然是空表；标记把「已知会限制 effort 词表的这一代存档」圈出来，让其它线连一次渲染探测都不必付，也绝不冒误改写的险。

**练习 2**：请求带 `"chat_template": "{{ '' }}"` 且 `reasoning_effort: "high"`，会发生什么？
**答案**：原样放行，不改写（`rewrite_reasoning_effort` 的覆盖守卫）。理由：这张临时模板的词表启动探测从未量过——它可能接受 `high`，按默认模板的权威把 `high` 改成 `xhigh` 反而可能弄坏一个本来能渲染的请求。测试 `a_per_request_template_override_keeps_the_callers_effort_value`（[reasoning_effort.rs:L325-L340](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/reasoning_effort.rs#L325-L340)）同时验证了 `chat_template: null`（无覆盖）时改写照常生效。

**练习 3**：把两层洋葱反过来包（history 在外、effort 在内），哪个数量会错、怎么错？
**答案**：会错的是 T。边界测量的 `chat_options` 从 body 读 `reasoning_effort`（history.rs 第 217 行）并用它渲染两次；effort 在内层意味着测量发生在改写**之前**，模板实际会以改写后的值渲染（例如 thinking 分支的开合随 effort 变化，generation prompt 的 token 数随之不同）。测出的 T 对应的是调用方发来的值，而路由渲染用的是改写后的值——快照点落在错误的 token 上，下一轮从快照恢复就会差几个 token。

## 5. 综合实践

**任务：给 echo-server 发两轮对话，推算并（可选）观测第二轮的 `history_tokens` 与 `history_marks`。**

1. **准备**：从工作区根目录运行 `cargo run -p pegainfer-frontend --example echo-server`（无 GPU、无权重；模型 fixture 在 [examples/echo-model/](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/examples/echo-model/config.json#L1-L6)，其 config 无 `output_gate_type` 标记——顺带确认：这个部署的 effort 别名表必然为空，第一层洋葱实际是透传的）。
2. **第一轮**：`curl http://127.0.0.1:8000/v1/chat/completions -H 'content-type: application/json' -d '{"model":"echo","messages":[{"role":"user","content":"hello"}],"max_tokens":64}'`，记下 assistant 回复（echo 线把 prompt 吐回来）。
3. **第二轮**：构造 body：`messages = [第一轮 user, 第一轮 assistant 回复, 新的 user 消息]`，再次请求。
4. **手工推算**（依据 4.1/4.2 的算法，按结构推）：
   - 第二轮请求恰有一个历史边界（第一轮 user 消息后面跟着 assistant 消息），所以 `history_marks` 是**单元素**向量；
   - 该元素的值 =「到第一轮 user 为止的会话，不带 generation prompt 渲染后的 token 数」= 第一轮请求**当时的** `history_tokens`；
   - 第二轮的 `history_tokens` = 第二轮整条 prompt 的 token 数 − T（T 默认 mode 下 ≥ 1）；桥侧过滤后必有 `marks[0] < history_tokens`。
   - echo-model 无 `tokenizer_config.json`，chat 路由用 vendored 默认模板渲染——**具体数值无法纸面确定，待本地验证**。
5. **（可选）观测**：echo-server 不初始化日志后端，`debug!` 不会输出；想看到真实数字，可在自己检出的 [history.rs:L435-L450](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/vllm/history.rs#L435-L450) 的 `mark_generation_prompt` 里临时加一行 `eprintln!("T={tokens:?} marks={marks:?}")`，重跑 echo-server 发第二轮请求，观测后还原。观测值应满足第 4 步的结构断言。
6. **核验**：把推算结果对照 [step.rs:L51-L67](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-server/../pegainfer-frontend/src/engine/step.rs#L51-L67) 的两个字段注释逐句核对——你的每个数是否都能在注释里找到一句话为其背书。
7. **答问**：向 echo-server 发 `{"messages":[...],"add_generation_prompt":false}` 或最后一条是 assistant 且 `continue_final_message:true` 的请求时，T 是多少、`history_tokens` 是多少、这对快照恢复意味着什么？（答案：T=0，`history_tokens = L − 0 = L`，整个 prompt 都是 history——因为根本没有「即将生成的尾巴」，会话的下一轮会从 prompt 的**最后一个 token** 之后接着重复，快照点就在 prompt 末尾。）

## 6. 本讲小结

- **边界测量的形态**：chat 层在路由前面把最后一条消息单独渲染两遍（带/不带 generation prompt），\( T = |W| - \operatorname{LCP}(W, W') \)；跨界 token 计入 T，边界只向历史方向退。桥侧用只有它知道的整条 prompt 长度做 \( h = L - T \)（`checked_sub`，装不下即 `None`）。
- **marks 是「更早轮次的 history_tokens」**：每个被 assistant 消息紧跟的消息处结束过一个更早请求；到该处为止、按请求自己的工具上下文、不带尾巴渲染的 token 数即 mark。新者在先、至多 8 个、桥侧严格小于 `history_tokens` 才保留——供 pooled tier 从更早快照恢复。
- **测量用的是路由自己的 renderer 与 tokenizer**，mode/模板控制逐字段复刻路由解析；`add_generation_prompt=false` 与 `continue_final_message=true` 都使 T=0、整个 prompt 是 history。
- **读不懂就放行**：非 JSON、无 messages、渲染失败、backends 未加载——任何一层量不动都不报错，body 原样穿过，调度器看到 `None`/空，错误留给路由自己报。两个键是「服务器对自己模板的测量」，调用方带同名键会被覆盖。
- **effort 规范化由模板裁决**：启动探测渲染每个候选值，`!accepts(from) && accepts(to)` 才武装别名（`high/max→xhigh`、`minimal→low`）；`output_gate_type` 标记是「值得探测」的闸门而非改写命令；`chat_template_kwargs` 直连与逐请求 `chat_template` 覆盖两条通道故意不改写。
- **洋葱顺序是正确性约束**：effort 最外、history 次外——边界必须用「路由真正渲染用的值」量 T；测量与渲染在 `spawn_blocking` 里做，秒级渲染不压 async worker；改写后重写 `Content-Length`，超长独占 413、其它读失败保持 400，共用 OpenAI 错误信封。

## 7. 下一步学习建议

- **u4-l5（特殊服务面）**：看同一洋葱里最内层 `extend_router` 的另外两个形态——prefill-only 门与 LoRA 路由合并，理解「整形层」与「能力路由层」的分工。
- **u4-l6（可观测性）**：marks 命中与否最终体现在 prefix-cache 计数器上；去看 `SchedulerMetrics` 的 `prefix_cache` 累计计数如何从调度器走到 `/metrics`，把本讲的「快照 miss 只是 miss」落到数字上。
- **源码延伸**：带快照语义的调度器如何消费 `history_tokens`/`history_marks`，可从 `pegainfer-qwen3` 或 `pegainfer-qwen35`（Gated DeltaNet 线，正是「状态不能回滚」的动机来源）的调度器代码里 grep `history_tokens` 入手；`docs/models/qwen35/` 下有对应的模型线文档。
