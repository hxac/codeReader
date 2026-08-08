# Agent 运行时适配器（Anthropic / OpenAI）

## 1. 本讲目标

本讲承接 u7-l1（智能体 RL 路线图）。u7-l1 确立了一个根本原则：**slime 不为 agent 另起框架，而是把多轮工具/沙箱/环境反馈作为一类数据生成工作流，接入已有的「rollout → buffer → training」闭环**，且训练目标必须保持 token-based（string in / token out）。

但这里有一个现实的工程鸿沟：现成的 agent 运行时（如 Claude Code、Codex）说的是它们自己的网络协议（Anthropic Messages API、OpenAI Chat Completions API），它们用「消息/工具调用」对话，而 slime 训练要的是「策略模型实际采样的 token id + 对数概率」。**适配器（adapter）就是填这道鸿沟的 HTTP 中间层。**

学完本讲，你应当能够：

1. 说清楚 adapter 解决的核心矛盾：**消息进、采样 token 出**，以及它为何对 on-policy 训练至关重要（避免重分词）。
2. 读懂 `BaseAdapter` 的「模板方法」骨架——它把会话生命周期、轮次上限、在途任务簿记与「一轮流水线」`_run_turn` 写死，子类只填协议相关的钩子。
3. 理解 `AnthropicAdapter` 与 `OpenAIAdapter` 各自如何翻译协议、装配回复，以及一个微妙的不变量：**manager_message 必须与客户端回传的历史按字典相等匹配**，否则每轮都会 fork。
4. 掌握 `session_id` 作为 `X-SMG-Routing-Key` 的前缀缓存路由作用，以及 `finish_session` 如何把整条会话树线性化成带 `loss_mask` 与 `rollout_log_probs` 的可训练 `Sample`。

## 2. 前置知识

- **Sample 数据载体**（u3-l1）：slime 训练的基本单元是 `Sample`，关键字段包括 `tokens`、`response_length`、`loss_mask`（1=模型生成、0=prompt/环境文本）、`rollout_log_probs`（行为策略对数概率）。
- **rollout_log_probs 的来源**（u3-l2、u5-l3）：SGLang `/generate` 接口在 `return_logprob=True` 时会在 `meta_info` 里返回每个输出 token 的 `(logprob, token_id)`；这就是行为策略对数概率，后续用于 off-policy 修正（u6-l5 的 TIS）。
- **on-policy 与 string in / token out 契约**（u7-l1）：训练序列要用模型实际采样的 token，绝不能把环境返回的字符串重新分词（re-tokenize）拼回去，否则采样分布与训练序列不一致。
- **chat template**：HF 分词器提供 `apply_chat_template(messages, tools=..., tokenize=True)`，能把「消息列表 + 工具 schema」渲染成一段连续的 `input_ids`，这正是喂给 SGLang 的输入。
- **模板方法模式（Template Method）**：父类定义算法骨架，子类重写其中个别步骤。本讲的 `BaseAdapter` 就是典型例子。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| :--- | :--- |
| `slime/agent/adapters/common.py` | 通用基类 `BaseAdapter`、会话/轮次簿记、一轮流水线 `_run_turn`、调 SGLang 的 `call_sglang_generate`、`finish_session` 轨迹导出。协议无关。 |
| `slime/agent/adapters/anthropic.py` | `AnthropicAdapter`：Anthropic Messages 协议翻译、回复装配、SSE 流式渲染。 |
| `slime/agent/adapters/openai.py` | `OpenAIAdapter`：OpenAI Chat Completions 协议翻译、回复装配、SSE 流式渲染。 |
| `slime/agent/trajectory.py` | `TurnRecord`（adapter 与轨迹管理器之间的契约）、`TrajectoryManager`（按 sid 维护消息树、`get_trajectory` 线性化成 `Sample`）。 |
| `slime/agent/parsing.py` | `parse_model_output`：把解码出的原始文本拆成 reasoning/text/tool_uses。 |
| `examples/coding_agent_rl/generate.py` | 真实的 `custom_generate` 范例，展示 adapter 如何被实例化、用 aiohttp 起服务、`open_session → 外部 agent 调用 → finish_session`。 |

> 提示：`common.py` 是本讲的主干，anthropic.py 与 openai.py 是它的两个具体子类实现，trajectory.py 是它把 token 落地成训练样本所依赖的下游部件。

## 4. 核心概念与源码讲解

### 4.1 Adapter 通用基类：模板方法与「消息进、token 出」契约

#### 4.1.1 概念说明

`BaseAdapter` 是一个 HTTP 服务适配器，它对外的契约只有一句话：**消息进、采样 token 出**。具体而言：

- 外部 agent 客户端（如 Claude Code CLI）以为自己在和一个真正的 Anthropic/OpenAI 端点对话，于是不断发来 `messages` 历史；
- adapter 把这套协议历史翻译成 chat-template 消息、渲染成 `input_ids`、POST 给上游 SGLang `/generate`；
- SGLang 用 slime 当前这一轮的策略权重真正「生成」token，并顺带返回每个 token 的对数概率；
- adapter **直接捕获这些 token id 和 logprob**（不重分词），按 session 累积成一条轨迹，最终在 `finish_session` 时导出成可训练的 `Sample`。

为什么必须捕获「原始 token」而不是「文本」？因为 on-policy 训练要求训练序列里的 token 与策略实际采样的 token 逐位一致。如果把 SGLang 生成的文本解码成字符串、再让 chat template 重新分词拼回，分词边界会漂移（drift），采样分布就被污染了。adapter 通过 `return_logprob=True` 拿到「token id + logprob」二元组，从根本上规避了重分词。

`BaseAdapter` 用**模板方法模式**组织代码：它把「会话生命周期、轮次上限、在途任务簿记、调 SGLang、记录轨迹」这些协议无关的逻辑写死成 `_run_turn`，而把「路由注册、sid 提取、协议翻译、回复装配、HTTP 响应渲染」这些协议相关的步骤声明为钩子，留给 `AnthropicAdapter` / `OpenAIAdapter` 填充。

#### 4.1.2 核心流程

一次 agent turn 在 `_run_turn` 中的流水线（这是整个 adapter 的心脏）：

```
客户端 POST /v1/messages (或 /v1/chat/completions)
        │
        ▼
[1] body = await request.json()
[2] self._preprocess_body(body)          # 子类钩子：协议特定预处理(如折叠 mid-list system)
[3] sid = self._session_id(request, body)  # 子类钩子：提取会话 id
[4] 守卫: sid 是否已关闭? 是否超过 max_turns_per_sid?
[5] translated, tools_schema = self._translate(body)   # 子类钩子:协议→chat-template 消息
[6] prompt_ids = _render_token_ids(translated, tokenizer, tools=tools_schema)
            # apply_chat_template + tokenize=True → input_ids
[7] turn = await call_sglang_generate(prompt_ids, session, body, adapter=self, session_id=sid)
            # POST /generate, return_logprob=True → TurnRecord(output_ids, output_log_probs, finish_reason)
[8] decoded = tokenizer.decode(turn.output_ids)
    parsed = parse_model_output(decoded, tools_schema, tool_parser, reasoning_parser)
            # → ParsedModelOutput(reasoning, text, tool_uses, ill_formed)
[9] reply = self._build_reply(parsed, turn.finish_reason, translated, tools_schema)
            # 子类钩子:产出 Reply(manager_message, finish_reason, wire)
[10] response = await self._respond(...)   # 子类钩子:渲染并刷新 HTTP 响应(含 SSE 流式)
            # ⚠ 先刷新再记录:客户端中途断开 → 不记录这一轮
[11] self.manager.record_turn(sid, turn=turn, prompt_messages=translated,
                             response_message=reply.manager_message)
            # 把这一轮写进按 sid 维护的轨迹树
```

注意第 [10] 步的一个关键设计：**必须先把 HTTP 响应刷新给客户端，确认对方收到了，才把这一轮写进轨迹**。如果客户端在生成途中断开（`ConnectionResetError` / `CancelledError`），adapter 会返回 499 且**不记录这一轮**——因为「客户端从未收到的回复」不该进入训练数据。参见 [slime/agent/adapters/common.py:359-374](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L359-L374)。

调 SGLang 的核心在模块级函数 `call_sglang_generate`，它做的事很纯粹：拼 sampling params、设路由头、POST，然后从 `meta_info.output_token_logprobs` 里拆出 token id 和 logprob：

```python
# 每个元素是 (logprob, token_id) 二元组
output_token_logprobs = meta.get("output_token_logprobs") or []
output_ids = [x[1] for x in output_token_logprobs]
output_log_probs = [float(x[0]) for x in output_token_logprobs]
```

这两行就是「token 捕获」的全部秘密：行为策略对数概率和采样 token 一同被保留下来，后续直接写进 `Sample.rollout_log_probs`，全程不重分词。参见 [slime/agent/adapters/common.py:497-501](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L497-L501)。

#### 4.1.3 源码精读

`BaseAdapter` 的类定义与构造器，注意它接收 `tokenizer`、`sglang_url`、`tool_parser`、`reasoning_parser`，并在内部建一个 `web.Application` 与共享的 `TrajectoryManager`：

[slime/agent/adapters/common.py:127-175](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L127-L175) —— `BaseAdapter` 类与 `__init__`。其中 `self.store`（按 sid 存 `Session`）、`self.inflight`（按 sid 存在途 `asyncio.Task`）、`self.manager = TrajectoryManager(...)` 是三个核心状态。

子类必须实现的钩子（全部 `raise NotImplementedError`）：

[slime/agent/adapters/common.py:179-206](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L179-L206) —— `_register_routes` / `_session_id` / `_preprocess_body` / `_translate` / `_build_reply` / `_respond`。

子类还需设置几个**类属性**作为协议「形状」的声明（定义在 `BaseAdapter` 里是空元组/默认 logger）：

[slime/agent/adapters/common.py:134-139](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L134-L139) —— `logger`、`log_prefix`、`max_token_keys`（哪些请求体字段约束 `max_new_tokens`）、`stop_keys`（哪些字段携带 stop 序列）。这两个元组会被 `_sampling_params` 用到。

`_sampling_params` 把请求体里的采样参数翻译成 SGLang 的 sampling params，注意三个硬编码默认：`skip_special_tokens=False`、`spaces_between_special_tokens=False`、`no_stop_trim=True`——后两者保证 token 与训练序列边界严格对齐，不被引擎擅自裁剪：

[slime/agent/adapters/common.py:416-439](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L416-L439)。

`call_sglang_generate` 设路由头的逻辑（详见 4.4）：当 `session_id` 存在且不为 `"default"` 时，加 `X-SMG-Routing-Key` 头：

[slime/agent/adapters/common.py:470-484](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L470-L484) —— 注意请求体里 `"return_logprob": True`，这是 token 捕获得以成立的前提。

一轮流水线 `_run_turn` 全文：

[slime/agent/adapters/common.py:318-393](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L318-L393) —— 末尾的 `finally` 块把当前 task 从 `self.inflight[sid]` 移除，保证 `shutdown_session` 能准确判断「是否还有在途请求」。

`TurnRecord` 是 adapter 与轨迹管理器之间的契约（一个 frozen dataclass）：

[slime/agent/trajectory.py:28-38](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L28-L38) —— 字段 `prompt_ids`、`output_ids`、`finish_reason`、`output_log_probs`、`ill_formed`。

#### 4.1.4 代码实践

**实践目标**：在不运行的前提下，把 `_run_turn` 的每一行标注成「协议翻译 / token 渲染 / SGLang 调用 / 解析 / 回复装配 / 响应刷新 / 轨迹记录」七个阶段，建立「一轮对话在 adapter 内部如何流动」的完整心智模型。

**操作步骤**：

1. 打开 [slime/agent/adapters/common.py:318-393](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L318-L393)。
2. 对照本节 4.1.2 的流水线伪代码，逐行给 `_run_turn` 的 `try` 块加注释，标明它属于哪个阶段、调用了哪个子类钩子或模块级函数。
3. 重点关注三处：第 [5] 步 `_translate` 依赖子类；第 [7] 步 `call_sglang_generate` 依赖 `return_logprob=True`；第 [10]-[11] 步「先响应后记录」的顺序。

**需要观察的现象**：你会发现整段流水线里，**真正「协议相关」的只有 5 个钩子调用点**（`_preprocess_body`、`_session_id`、`_translate`、`_build_reply`、`_respond`），其余全是协议无关的共享逻辑。这正是模板方法模式的价值。

**预期结果**：你能口头复述「一次 POST 请求从进 aiohttp 到被 `record_turn` 落库」的完整路径，并指出 token 与 logprob 在哪一步被捕获。

#### 4.1.5 小练习与答案

**练习 1**：`_sampling_params` 里为何要把 `no_stop_trim` 设为 `True`？

**参考答案**：`no_stop_trim=True` 让 SGLang 即使命中 stop 序列也**不裁剪**返回的 token，保证 adapter 拿到的 `output_ids` 与训练序列逐位一致。如果引擎擅自裁掉 stop token，那么「采样时记录的 token」和「训练用的 token」就会不一致，破坏 on-policy 正确性。

**练习 2**：`call_sglang_generate` 为何是「模块级函数」而非 `BaseAdapter` 的方法？看注释里给了什么理由。

**参考答案**：注释写明 `Module-level (not a method) so tests can monkeypatch it`（[common.py:452-453](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L452-L453)）。把它放在模块级，测试就能用 `monkeypatch` 替换掉真正的 HTTP 调用，从而在没有 SGLang 的情况下单测 adapter 的翻译/装配逻辑。

---

### 4.2 AnthropicAdapter：Messages 协议翻译与回复装配

#### 4.2.1 概念说明

`AnthropicAdapter` 让任何「以为自己在跟 Anthropic Messages API 对话」的客户端（典型是 Claude Code 风格的 agent）驱动 slime 的 SGLang 推理。它只实现 `BaseAdapter` 留下的 6 个钩子，把 Anthropic 线协议来回翻译：

- **入站翻译**：Anthropic 的消息用「content blocks」表达，如 `{"type":"text"}`、`{"type":"tool_use","input":{...}}`、`{"type":"tool_result"}`、`{"type":"thinking"}`。`_translate` 把它们压平成 chat-template 能识别的 `{role, content, tool_calls, reasoning_content}` 标准消息。
- **出站装配**：SGLang 生成并解析后，`_build_reply` 把 `ParsedModelOutput` 重新组装成 Anthropic 风格的 content blocks，并产出两个东西——给客户端看的「wire」回复（含 `toolu_*` id），以及给轨迹管理器看的 `manager_message`（规范化、丢弃 wire-only id）。

一个 Anthropic 特有的怪癖：`_preprocess_body` 会把「出现在消息列表中间的 system 消息」折叠进相邻的 user 消息，包成 `<system-reminder>` 文本块。原因是很多 chat template 拒绝 index>0 的 system 消息。

#### 4.2.2 核心流程

入站翻译 `_translate_messages` 的映射规则（pure function）：

```
Anthropic wire                        chat-template 消息
─────────────────────────────────     ──────────────────────────────
system (顶层或 role=system)        →  {role:"system", content:str}
user.text block                    →  {role:"user", content:text}
user.tool_result block             →  {role:"tool", content:str}     # 关键:工具结果变成 tool 角色
assistant.text block               →  content 拼接
assistant.thinking block           →  reasoning_content 拼接
assistant.tool_use block           →  tool_calls.append(tool_call_dict(name, input))
```

出站装配 `_build_reply_parts` 把一次生成拆成三段：

```
parsed.reasoning → {type:"thinking"} block
parsed.text      → {type:"text"} block
parsed.tool_uses → {type:"tool_use", id:"toolu_xxx", name, input} blocks
                ↓ 同时
            manager_message.tool_calls = [tool_call_dict(name, input)]   # 丢弃 id
stop_reason = "tool_use" | "max_tokens" | "end_turn"
```

注意一个关键细节：**`tool_call_dict` 把 `arguments` 保留为 dict 而非 JSON 字符串，并丢弃 wire-only 的 tool call id**。这关系到下一讲的「回放一致性」不变量，先记下结论：manager_message 必须与客户端下一轮回传的历史**按字典相等**匹配。

#### 4.2.3 源码精读

`AnthropicAdapter` 类——只填钩子与类属性，别无他物：

[slime/agent/adapters/anthropic.py:39-75](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L39-L75)。注意类属性 `max_token_keys = ("max_tokens",)`、`stop_keys = ("stop_sequences",)`，这正是 Anthropic 协议里约束生成长度与 stop 序列的字段名。

`_session_id` 提取——Anthropic 的请求体里**没有** sid 提示，所以只能从 header 取（`Authorization: Bearer` 优先，其次 `X-Api-Key`，最后兜底 `"default"`）：

[slime/agent/adapters/anthropic.py:192-195](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L192-L195)。

入站翻译 `_translate_messages`：

[slime/agent/adapters/anthropic.py:81-120](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L81-L120) —— 注意 `tool_result` block 被映射成 `{role:"tool"}`（第 93-94 行），这是多轮工具调用能正确续上的关键。

出站装配 `_build_reply_parts`：

[slime/agent/adapters/anthropic.py:147-186](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L147-L186) —— 第 165 行给 wire block 生成 `toolu_*` id，第 168 行用 `tool_call_dict` 生成**不带 id** 的 manager 版本。

`tool_call_dict` 的规范定义（在 common.py）：

[slime/agent/adapters/common.py:110-118](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L110-L118) —— 注释解释了为何 `arguments` 必须是 dict：chat template 需要映射，且轨迹管理器用字典相等匹配历史。

折叠中间 system 的 `_fold_mid_list_system_into_user`：

[slime/agent/adapters/anthropic.py:291-350](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L291-L350)。

#### 4.2.4 代码实践

**实践目标**：用一个构造好的 Anthropic 请求体，手工模拟 `_translate_messages` 的输出，验证你理解了协议映射。

**操作步骤**：

1. 构造这样一个最小请求体（示例数据，非项目代码）：

   ```python
   body = {
       "system": "You are a coding agent.",
       "messages": [
           {"role": "user", "content": "List files in /tmp."},
           {"role": "assistant", "content": [
               {"type": "text", "text": "Sure."},
               {"type": "tool_use", "name": "bash", "input": {"cmd": "ls /tmp"}},
           ]},
           {"role": "user", "content": [
               {"type": "tool_result", "content": "a.txt\nb.txt"},
           ]},
       ],
       "tools": [{"name": "bash", "input_schema": {"type": "object"}}],
   }
   ```

2. 对照 [anthropic.py:81-120](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L81-L120) 与 [anthropic.py:123-141](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/anthropic.py#L123-L141)，在纸上写出 `_translate(body)` 返回的 `(translated, tools_schema)`。

**需要观察的现象**：assistant 的 `tool_use` block 是否变成了 `tool_calls`？`tool_result` 是否变成了 `{role:"tool"}`？

**预期结果**（待本地验证）：`translated` 应包含 4 条消息——`system`、`user`、`assistant(带 tool_calls)`、`tool`；`tools_schema` 是规范化后的 `[{"type":"function","function":{"name":"bash",...}}]`。

#### 4.2.5 小练习与答案

**练习 1**：Anthropic 协议里约束生成长度的字段是 `max_tokens`，stop 序列字段是 `stop_sequences`。如果客户端发来的是 `max_tokens=512`，这个值如何最终影响 SGLang？

**参考答案**：`max_token_keys=("max_tokens",)` 让 `_sampling_params` 把它读进 `sp["max_new_tokens"]`（取与默认 4096 的较小值），再经 `call_sglang_generate` 作为 `sampling_params` 传给 SGLang `/generate`，从而限制这一轮最多生成 512 个新 token。

**练习 2**：为什么 `_translate_messages` 要把 `tool_result` 映射成 `{role:"tool"}` 而不是 `{role:"user"}`？

**参考答案**：chat template 对 `tool` 角色有专门的渲染（通常插入工具调用结果标记），若当成普通 user 文本拼接，模板里上一轮的 `tool_calls` 会找不到对应的工具结果，破坏多轮工具对话的结构。

---

### 4.3 OpenAIAdapter：Chat Completions 协议与回放一致性不变量

#### 4.3.1 概念说明

`OpenAIAdapter` 与 `AnthropicAdapter` 是镜像关系：它说 OpenAI Chat Completions 协议（端点 `/v1/chat/completions`），让 OpenAI SDK / Codex 风格客户端驱动 slime。两者结构完全对称，本节聚焦它们的**差异点**，以及一个对所有 adapter 都成立、但在 OpenAI 这边最棘手的**回放一致性不变量**。

主要差异：

| 维度 | AnthropicAdapter | OpenAIAdapter |
| :--- | :--- | :--- |
| 端点 | `/v1/messages`、`/v1/messages/count_tokens` | `/v1/chat/completions` |
| `max_token_keys` | `("max_tokens",)` | `("max_completion_tokens","max_tokens","max_output_tokens")` |
| `stop_keys` | `("stop_sequences",)` | `("stop",)` |
| sid 来源 | 仅 header（Bearer / X-Api-Key） | header 优先，其次 body `metadata.session_id` / `user` |
| tool 参数形状 | `input`（dict） | `function.arguments`（**JSON 字符串**，需转 dict） |
| 角色别名 | — | `developer`→`system` |

**回放一致性不变量**（核心难点）：agent 客户端是**无状态**的——每轮请求都把完整历史（包括上一轮 adapter 返回的 assistant 消息）原样回传。轨迹管理器用 `_find_mount_point` 按 `child.message == msg`（字典相等）在消息树里找挂载点。这就要求 adapter 上一轮写进轨迹的 `manager_message`，必须与客户端这一轮回传的同一条 assistant 消息**字典相等**，否则匹配失败、每轮都 fork 成新分支（破坏轨迹连续性）。

问题在于：客户端回传时往往会**篡改**消息——剥掉 `reasoning_content`、丢掉多余的并行 tool_calls、把 content 从 null 改成空串。所以 adapter 不能简单地把 wire 回复原样存进轨迹，而要**预测客户端会回传成什么样**，按那个形状存。OpenAI 的 `_build_reply_parts` 就是用一连串注释把这套「为什么 manager_message 长这样」讲清楚了。

#### 4.3.2 核心流程

OpenAI 入站翻译的两个特殊处理：

```
1. _arguments_as_dict: function.arguments 可能是 JSON 字符串 → json.loads → dict
                       解析失败 → {"_raw_arguments": s}    # 保底,不崩
2. developer 角色 → 当作 system
3. tool 消息: 丢弃 tool_call_id (wire-only 关联字段,留着会破坏字典相等)
4. assistant.tool_calls: arguments 统一转 dict,丢弃 id
```

出站装配 `_build_reply_parts` 同时产出三样东西，其中 wire 和 manager **刻意不同**：

```
wire_message:        给客户端,严格符合 OpenAI 规范
                     - tool_calls[i].id = "call_xxx" (唯一关联 id)
                     - tool_calls[i].function.arguments = JSON 字符串
                     - 有 tool_calls 时 content = null

manager_message:     给轨迹管理器,要匹配客户端回传
                     - 无 reasoning_content          (有些客户端回传时会剥掉)
                     - 只保留第一个 tool_call         (有些客户端会丢弃并行多余的)
                     - 有 tool_calls 时 content = ""  (对应 wire 的 null)
```

#### 4.3.3 源码精读

`OpenAIAdapter` 类与类属性：

[slime/agent/adapters/openai.py:38-73](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L38-L73)。注意 `max_token_keys` 有三个候选（按优先级），因为不同 OpenAI 客户端用的字段名不同。

`_arguments_as_dict`——把 JSON 字符串参数安全转成 dict：

[slime/agent/adapters/openai.py:79-99](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L79-L99)。

入站翻译 `_translate_messages`（含 `developer→system`、丢弃 `tool_call_id` 等不变量注释）：

[slime/agent/adapters/openai.py:102-163](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L102-L163)。

**本节最重要的代码**——`_build_reply_parts` 里那段解释 manager_message 为何与 wire 不同的注释：

[slime/agent/adapters/openai.py:250-272](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L250-L272) —— 注释明确列出三条差异（无 reasoning_content、只留第一个 tool_call、tool_calls 时 content 置空），每条都标注了「needed to match the echo」。第 271-272 行用切片 `[:1]` 只保留首个 tool_call。

sid 解析（header 优先，body 兜底）：

[slime/agent/adapters/openai.py:287-290](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L287-L290)，其中 `sid_from_body` 见 [common.py:404-413](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L404-L413)（读 `metadata.session_id` 或 `user`）。

轨迹管理器里用字典相等找挂载点的代码（印证不变量的存在）：

[slime/agent/trajectory.py:352-368](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L352-L368) —— 第 361 行 `child.message == msg` 就是「字典相等」匹配；匹配不上就 break，导致后续消息挂到新分支（fork）。

#### 4.3.4 代码实践

**实践目标**：理解 wire 与 manager 两份消息为何必须不同，并验证「丢掉 reasoning_content / 只留首个 tool_call」确实是某些客户端的回传行为。

**操作步骤**：

1. 阅读 [openai.py:250-272](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L250-L272) 的注释，列出 `wire_message` 与 `manager_message` 的全部差异点。
2. 假设某客户端在回传上一轮 assistant 消息时，把 `reasoning_content` 整段删掉了。回答：如果 adapter 把 wire（含 reasoning_content）原样存进轨迹，`_find_mount_point` 会发生什么？

**需要观察的现象**：你会看到 adapter 不是「存我发给客户端的东西」，而是「存客户端会回传给我的东西」——这是一种面向消费者（consumer-driven）的契约设计。

**预期结果**：若存了含 reasoning_content 的 wire，客户端回传时没了这个字段，`child.message == msg` 失败 → fork。所以 manager_message 故意不带 reasoning_content。注意：reasoning token 的 id 仍保留在训练 token 里（只是文本层面从 manager_message 删掉），训练信号不丢。

#### 4.3.5 小练习与答案

**练习 1**：OpenAI 协议里 `tool_calls[i].function.arguments` 是 JSON 字符串，但 `tool_call_dict` 和 manager_message 里却要求是 dict。为什么不能存 JSON 字符串？

**参考答案**：两个理由——(1) chat template 渲染工具调用时需要的是映射（dict）而非字符串；(2) 轨迹管理器用字典相等匹配历史，而 JSON 字符串的键序不稳定（`{"a":1,"b":2}` 与 `{"b":2,"a":1}` 序列化后字符串不同但 dict 相等）。存 dict 让匹配与键序无关。参见 [openai.py:108-114](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/openai.py#L108-L114) 的不变量注释。

**练习 2**：`max_token_keys` 为何 OpenAI 有三个候选而 Anthropic 只有一个？

**参考答案**：OpenAI 生态里约束输出长度的字段名历经演变（`max_tokens` → `max_completion_tokens`，Responses API 又用 `max_output_tokens`），不同客户端用的字段不同，故按优先级列三个，`_sampling_params` 取第一个非空的。Anthropic 协议字段名稳定，只用 `max_tokens`。

---

### 4.4 session_id 路由与 finish_session 导出可训练轨迹

#### 4.4.1 概念说明

前面三节讲清了「一轮对话如何进、token 如何被捕获」。本节讲两个贯穿全程的关键机制：**session_id 的前缀缓存路由**，以及 **`finish_session` 如何把整条会话树线性化成可训练 `Sample`**。

**session_id 作为 `X-SMG-Routing-Key`**：多轮 agent 的核心特征是「长共享前缀」——每一轮请求都包含之前所有的对话历史，只有末尾新增。如果 SGLang 把同一会话的不同轮次路由到不同 worker，每个 worker 都得重新 prefill 整段历史，KV cache 完全无法复用，吞吐崩塌。解决办法是给同一 `session_id` 的请求打上路由键，让 router 把它**稳定地路由到同一个 worker**，从而命中前缀缓存（prefix cache）。adapter 在 `call_sglang_generate` 里设 `X-SMG-Routing-Key: session_id` 头实现这一点。

**finish_session 导出轨迹**：一次 agent 执行可能有很多轮、甚至因 subagent/compaction 分叉成多条路径。`finish_session(sid)` 做三件事：(1) 等待该 sid 的所有在途请求结束（`shutdown_session`）；(2) 调 `manager.get_trajectory(sid, base_sample, reward)` 把按 sid 维护的消息树**线性化**成一组 `Sample`；(3) 用自己持有的 tokenizer 给每个 Sample 解码出 `.response` 文本（轨迹管理器本身不持有 tokenizer）。最终返回的每个 `Sample` 都带上了完整的 token、loss_mask、rollout_log_probs 和 reward，可直接进训练。

#### 4.4.2 核心流程

`finish_session` 的三段式：

```
finish_session(sid, base_sample, reward, extra_metadata):
  ┌─[1] shutdown_session(sid)
  │       - self.closed.add(sid)              # 标记关闭,拒绝后续 straggler 请求
  │       - 取出 inflight[sid] 的在途 task
  │       - asyncio.wait(tasks, timeout) 等它们结束;超时则 cancel
  │
  ├─[2] session = self.store.pop(sid)
  │     samples = self.manager.get_trajectory(sid, base_sample, reward, extra_metadata,
  │                                            max_sample_tokens=session.max_context_tokens)
  │       # 线性化消息树: 每个 routing leaf → 一条或多条 Sample
  │       # 每个 Sample 的 reward 都是【完整的 reward】,不是平摊
  │
  └─[3] for s in samples:
          s.response = tokenizer.decode(s.tokens[-rlen:], skip_special_tokens=False)
        return samples
```

会话树的线性化（在 `TrajectoryManager.get_trajectory` 里）大致是：

```
对消息树的每个 routing_leaf:
    chain = leaf.path_from_root()          # 从根到叶的节点链
    把链上的 generated turns(assistant 且 turn 不为 None)装进 _SampleBuilder
        - 同一 builder 内 token 漂移小 → CLEAN/REALIGN 续接
        - 漂移过大 → FORK,开新 builder(产出新 Sample)
        - 兄弟叶共享的 turn 只在第一片叶训练,其余叶 loss_mask=0 重复(避免重复计数)
    builder.to_sample(...) → Sample(tokens, response_length, loss_mask, rollout_log_probs)
```

每个 `Sample` 的 `loss_mask` 长这样（一轮两轮的简化例子）：

```
[prompt...][assistant 回复1][工具结果][assistant 回复2]
  loss_mask: 000...0     111...1        000...0    111...1
             ↑不可训练    ↑可训练        ↑环境文本   ↑可训练
```

而 `rollout_log_probs` 只在可训练段填真实值，其余段填 0。

#### 4.4.3 源码精读

`X-SMG-Routing-Key` 头的设置：

[slime/agent/adapters/common.py:470-484](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L470-L484) —— 第 472 行 `headers = {"X-SMG-Routing-Key": session_id} if session_id and session_id != "default" else None`，只在有有效 sid 时设头。

`finish_session` 全文：

[slime/agent/adapters/common.py:245-276](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L245-L276) —— 注意第 271-275 行用 adapter 自己的 tokenizer 解码 `.response`（注释说「the manager is tokenizer-free, so the adapter that owns the tokenizer fills this in」）。

`shutdown_session`（drain 在途任务）：

[slime/agent/adapters/common.py:225-243](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L225-L243) —— 注意它用 `tasks[0].get_loop()` 拿到在途 task 所在的事件循环，再跨线程提交 drain 协程（adapter 的 HTTP 服务常跑在独立线程，见 4.4.5 与综合实践）。

`TrajectoryManager.get_trajectory` 线性化：

[slime/agent/trajectory.py:307-344](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L307-L344) —— 第 339-340 行 `for s in samples: s.reward = reward`，印证「每个 Sample 拿完整 reward，不平摊」（注释在第 321-322 行解释：so each trained turn carries the trajectory's outcome reward）。

`_SampleBuilder.append_turn`——按 loss_mask 拼接 token 与 logprob（CLEAN 与 REALIGN 两种续接方式）：

[slime/agent/trajectory.py:193-214](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L193-L214) —— prompt tail 一律 `loss_mask=0`，generated response 段 `loss_mask=int(trained)` 并带上 `output_log_probs`。

`to_sample` 产出最终 Sample（剥掉首轮 prompt，loss_mask/logprob 只覆盖 response 区）：

[slime/agent/trajectory.py:234-261](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L234-L261) —— 注意 `rollout_log_probs=logprobs[start:]`，这就是行为策略对数概率的最终落脚点。

#### 4.4.4 代码实践

**实践目标**：追踪一次 3 轮 agent 对话，理解 `finish_session` 最终产出几个 `Sample`、每个的 loss_mask 长什么样。

**操作步骤**：

1. 设想一个场景：客户端与 adapter 进行 3 轮对话，每轮都生成一个 assistant 回复，中间夹了 2 个 tool_result。中途没有 subagent 分叉、没有 compaction、token 漂移可忽略（全是 CLEAN 续接）。
2. 对照 [trajectory.py:456-477](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L456-L477)（`_split_chain_into_builders`）回答：这条链会装进几个 `_SampleBuilder`？
3. 对照 [trajectory.py:193-214](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/trajectory.py#L193-L214)，画出这个唯一 Sample 的 loss_mask 示意图（哪段 0、哪段 1）。

**需要观察的现象**：3 轮 CLEAN 续接 → 单个 builder → 单个 Sample，它的 token 序列是「首轮 prompt + 回复1 + 工具结果1 + 回复2 + 工具结果2 + 回复3」的拼接。

**预期结果**（待本地验证）：产出 1 个 Sample。loss_mask 形如 `0...0(prompt) | 1...1(回复1) | 0...0(工具结果1) | 1...1(回复2) | 0...0(工具结果2) | 1...1(回复3)`；`rollout_log_probs` 在三个 `1...1` 段为真实值，其余为 0。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `X-SMG-Routing-Key` 只在 `session_id != "default"` 时才设？

**参考答案**：`"default"` 是没有有效 sid 时的兜底值（多个不同会话都会落到它）。若给 `"default"` 也设路由键，所有这些无关会话会被强行路由到同一个 worker，既无法区分、又造成负载倾斜。只有真正的、稳定的 session_id 才值得做亲和路由。参见 [common.py:472](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/adapters/common.py#L472)。

**练习 2**：`get_trajectory` 给每个 Sample 都赋了完整的 `reward`，而不是除以 Sample 数。结合 u7-l1 的 fan-out 分摊原则，这里是否矛盾？

**参考答案**：不矛盾。u7-l1 讲的是「一次执行**人为**拆成 K 段、希望每段只承担 1/K 奖励」时，由用户的 `generate` 函数自己把 reward 除以 K。而 adapter 的 `get_trajectory` 产出的多个 Sample 通常来自会话树的**不同分支**（subagent/compaction fork），每条分支都是一条独立的可训练轨迹，理应各自承担完整结果奖励。是否分摊由调用方（custom_generate）决定，adapter 只提供「每条都给完整 reward」的默认。

---

## 5. 综合实践

**任务**：参照真实范例 [examples/coding_agent_rl/generate.py](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py)，用 `AnthropicAdapter` 写一个**最小** `custom_generate` 函数骨架，体现 `open_session → 外部 client 调用 → finish_session` 的完整生命周期，并说明可训练 token 从何而来。

### 背景与骨架

adapter 的 HTTP 服务需要跑在一个事件循环里。范例用一个单例 `_AdapterService` 在初始化时通过 `run_app_in_thread`（[slime/agent/aiohttp_threaded.py:46-98](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/slime/agent/aiohttp_threaded.py#L46-L98)）把 `adapter.app` 起在后台守护线程上。你的最小骨架（示例代码，非项目原有文件）如下：

```python
# 示例代码:一个最小的 custom_generate 骨架,展示 adapter 三段式生命周期
from slime.agent.adapters import AnthropicAdapter
from slime.agent.aiohttp_threaded import run_app_in_thread
from slime.utils.processing_utils import load_tokenizer
from slime.utils.types import Sample


class _Service:  # 单例:整个 rollout 进程只起一次 adapter 服务
    def __init__(self, args):
        self.tokenizer = load_tokenizer(args.hf_checkpoint, trust_remote_code=True)
        sglang_url = f"http://{args.sglang_router_ip}:{args.sglang_router_port}"
        self.adapter = AnthropicAdapter(
            tokenizer=self.tokenizer,
            sglang_url=sglang_url,
            tool_parser=getattr(args, "sglang_tool_call_parser", None) or None,
            reasoning_parser=getattr(args, "sglang_reasoning_parser", None) or None,
        )
        self.handle = run_app_in_thread(self.adapter.app, host="0.0.0.0", port=18001)
        self.adapter_url = f"http://127.0.0.1:{self.handle.port}"


async def generate(args, base_sample: Sample, sampling_params: dict, evaluation: bool = False):
    svc = _Service(args)
    sid = base_sample.session_id = "demo-session-001"   # 多轮 agent 用稳定 sid

    # [1] open_session: 注册一个会话,带上采样默认值
    svc.adapter.open_session(sid, sampling_defaults=sampling_params, max_context_tokens=8192)

    try:
        # [2] 外部 client 调用: 这一步通常是真正的 agent(Claude Code/Codex)在跑,
        #     它把 adapter_url 当成 Anthropic 端点,反复 POST /v1/messages。
        #     每一轮 adapter 都: 翻译→渲染 chat template→调 SGLang(return_logprob=True)
        #     →捕获 token id+logprob→record_turn 写进会话树。
        #     这里用一个占位函数表示"等 agent 跑完":
        await _run_external_agent(svc.adapter_url, sid, base_sample.prompt)

        # [3] finish_session: 把整条会话树线性化成可训练 Sample
        reward = await _compute_reward(base_sample)   # 由 --custom-rm-path 或这里给出
        samples = await svc.adapter.finish_session(
            sid,
            base_sample=base_sample,
            reward=reward,
        )
        return samples
    finally:
        await svc.adapter.drop_session(sid)   # 清理,idempotent


async def _run_external_agent(adapter_url, sid, prompt):
    ...  # 你的 agent 在这里跑(参考 coding_agent_rl 用 sandbox+harness)
```

### 你要回答的三个问题

1. **可训练 token 从何而来？** 追踪路径：外部 agent 每发一个请求 → adapter `_run_turn` → `call_sglang_generate`（`return_logprob=True`）→ 从 `meta_info.output_token_logprobs` 拆出 `(logprob, token_id)` → 装进 `TurnRecord` → `record_turn` → `finish_session` 时 `get_trajectory` 线性化 → 写进 `Sample.tokens` / `Sample.rollout_log_probs`。**全程没有重分词**。
2. **`open_session` 和 `finish_session` 为何要成对？** `open_session` 在 `self.store` 注册 sid 并设采样默认值；`finish_session` 先 drain 在途请求、再线性化轨迹、最后 `pop(sid)`。不成对会导致会话泄漏或轨迹丢失。注意 `finish_session` 是幂等的（二次调用返回 `[]`）。
3. **真实范例还多了什么？** 对比 [generate.py:182-283](https://github.com/THUDM/slime/blob/06ffdbe22be068b52f9ed0fc318c473f7030197e/examples/coding_agent_rl/generate.py#L182-L283)，真实版本还做了：用 E2B sandbox 启动隔离环境、`harness.run` 跑 Claude Code/Codex、`git_diff` + `run_evaluation` 算 reward、wall-clock 超时守护、`drop_session` 兜底清理。adapter 只是其中「token 捕获」那一环。

### 运行说明

本骨架依赖真实的 SGLang 服务、tokenizer 与一个外部 agent 客户端，**无法在无 GPU 环境完整运行**，属「源码阅读型实践」。重点是把三段式生命周期和 token 流向理清。若想验证 adapter 的纯翻译逻辑，可参考本讲 4.1.5 提到的：`call_sglang_generate` 是模块级函数，可用 `monkeypatch` 替换成返回假 `TurnRecord` 的桩，从而单测 `_translate` / `_build_reply`（**待本地验证**）。

## 6. 本讲小结

- **adapter 的核心契约是「消息进、采样 token 出」**：它是一个 HTTP 中间层，把 Anthropic/OpenAI 协议历史翻译成 chat-template 消息，调 SGLang `/generate`（`return_logprob=True`），直接捕获 `(logprob, token_id)` 而非重分词文本——这是 on-policy 训练正确性的根本保证。
- **`BaseAdapter` 用模板方法模式**：会话生命周期、轮次上限、在途簿记、`_run_turn` 一轮流水线都是协议无关的共享逻辑；子类只填 6 个钩子（`_register_routes`/`_session_id`/`_preprocess_body`/`_translate`/`_build_reply`/`_respond`）+ 几个类属性（`max_token_keys`/`stop_keys`）。
- **`AnthropicAdapter` 与 `OpenAIAdapter` 是镜像**：差异集中在端点、字段名（max_tokens vs max_completion_tokens）、sid 来源、tool 参数形状（dict vs JSON 字符串）和角色别名。
- **回放一致性不变量**：manager_message 必须与客户端回传的 assistant 消息按字典相等匹配（`_find_mount_point` 用 `==`），否则每轮 fork。OpenAI 侧尤其微妙——需主动剥掉 reasoning_content、只留首个 tool_call、tool_calls 时 content 置空，以贴合客户端的回传行为。
- **`session_id` 经 `X-SMG-Routing-Key` 做前缀缓存亲和**：同一会话稳定路由到同一 worker，多轮长前缀得以复用 KV cache。
- **`finish_session` 把会话树线性化成 `Sample`**：drain 在途 → `get_trajectory` 线性化（CLEAN/REALIGN 续接、FORK 开新 Sample、兄弟叶共享 turn 只训练一次）→ tokenizer 解码 `.response`；每个 Sample 带完整 reward、带 loss_mask（区分可训练/不可训练 token）与 rollout_log_probs。

## 7. 下一步学习建议

- **u7-l3（多样本 fan-out 与轨迹分段训练）**：本讲的 `finish_session` 会因 subagent/compaction 产生多个 `Sample`，下一讲专门讲这些兄弟样本如何共享 `rollout_id`、reward 如何分摊，以及 `_split_chain_into_builders` 的 fork 阈值如何决定切分粒度。
- **u5-l3（SGLang 引擎封装）**：adapter 的上游就是 SGLangEngine。建议回看 `return_logprob` 与 `output_token_logprobs` 在引擎侧如何产生，理解「token 捕获」的完整链路。
- **u8-l1 / u8-l2（SGLang 拓扑与 PD 分离）**：本讲的 `X-SMG-Routing-Key` 亲和路由在多轮 agent 上收益最大；想深入 `--router-policy consistent_hashing` 与 PD 分离对长尾 agent 的吞吐优化，可接着读这两讲。
- **源码延伸阅读**：`slime/agent/trajectory.py` 的 `classify_token_drift` / `_try_merge_assistant_rewrite` 处理了真实 agent 场景下的大量「重分词漂移」细节，是理解为何轨迹管理比「拼接 token」复杂得多的关键。
