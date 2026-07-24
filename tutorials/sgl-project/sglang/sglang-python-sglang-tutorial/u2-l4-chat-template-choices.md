# Chat 模板与 choices 选择策略

> 本讲是第 2 单元（前端语言层 `lang/`）第 4 篇，承接 u2-l1。
> u2-l1 讲清了前端四个原语 `function` / `gen` / `select` / `image`，并指出 `gen` 带 `choices` 时会退化为 `select`，默认比较方式是 `token_length_normalized`。
> u2-l3 进一步说明：解释器执行到 `SglSelect` 时会调 `self.backend.select(s, choices, temperature, choices_method)`，自研 `RuntimeEndpoint` 后端能拿到真实的 token 级 logprob，而第三方后端拿不到、只能退化。
> 本讲要回答两个紧随其后的问题：**①前端的「对话」是怎么拼出来的？`system()` / `user()` / `assistant()` 这些角色原语和 chat 模板是什么关系？②`select` 在多个候选里到底「凭什么」挑出一个？`token_length_normalized` 之外还有哪些 `choices_method`，它们的数学含义和代价各是什么？**

## 1. 本讲目标

学完本讲，你应当能够：

- 用一句话说清 `system()` / `user()` / `assistant()` 这三个角色原语返回的是什么对象，以及它们如何与 `ChatTemplate` 的 `role_prefix_and_suffix` 表配合，把结构化的多轮对话渲染成模型实际看到的扁平字符串。
- 区分 `ChatTemplate` 的两种渲染入口：`get_prompt(messages)`（一次性渲染一组消息）与解释器里逐条 `get_prefix_and_suffix(role, hist)`（边执行边拼接），并理解它们共用同一张前缀/后缀表。
- 解释 `select` 在后端拿到每个候选的 token 级 logprob 后，「归一化」是为了消除什么偏差，以及为什么默认的 `token_length_normalized` 用的是「平均对数概率」而非「总对数概率」。
- 区分三种内置 `choices_method`：`token_length_normalized`（平均似然）、`greedy_token_selection`（逐 token 贪心淘汰）、`unconditional_likelihood_normalized`（减去无条件似然，即 PMI 风格），并知道后者为何需要额外一次前向、属性 `requires_unconditional_logprobs` 为何存在。
- 亲手用 `user()` / `assistant()` 拼一段多轮对话，并对同一个 `select` 分别换两种 `choices_method` 跑一遍，观察并解释结果差异。

## 2. 前置知识

本讲假设你已经了解（来自 u2-l1 / u2-l2 / u2-l3）：

- **SglExpr 与惰性求值**：`gen` / `select` / 角色原语返回的都是表达式对象，真正执行发生在解释器阶段（u2-l1）。
- **`SglSelect` 与 `choices_method`**：`select(name, choices, temperature=0.0, choices_method=token_length_normalized)` 构造一个 `SglSelect`，其 `choices_method` 字段会被原样传给后端的 `select`（u2-l1）。
- **解释器 `StreamExecutor`**：执行到 `SglSelect` 调 `self.backend.select(...)`，把返回的 `ChoicesDecision.decision` 写回 `variables[name]`（u2-l2）。
- **后端契约**：`BaseBackend.select` 是抽象方法，`RuntimeEndpoint` 用 REST 拿真实 logprob 来算，`OpenAI` 等第三方后端拿不到 token logprob、`choices_method` 形同虚设（u2-l3）。

下面用两组类比建立直觉：

| 概念 | 直觉类比 | 关键点 |
| --- | --- | --- |
| 角色原语 `system/user/assistant` | 「**贴标签的便签纸**」 | 给一段文本打上角色标签，标签本身不是正文 |
| `ChatTemplate` | 「**信封格式模板**」 | 规定每个角色的抬头和落款怎么写，不同模型家族格式不同 |
| `role_prefix_and_suffix` | 「**抬头/落款对照表**」 | 一张 `{role: (prefix, suffix)}` 的字典，渲染的全部依据 |
| `choices_method` | 「**裁判评分规则**」 | 同样的候选分数，换一套评分规则可能选出不同的赢家 |

核心思路有两条线：**渲染线**（角色原语 → chat 模板 → 扁平 prompt 字符串）与**决策线**（候选 logprob → 归一化评分 → argmax 选出一个）。本讲 4.1–4.2 讲渲染线，4.3–4.4 讲决策线。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `python/sglang/lang/` 下：

| 文件 | 角色 | 本讲关注点 |
| --- | --- | --- |
| [lang/chat_template.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/chat_template.py) | **对话模板** | `ChatTemplate` 数据类、`get_prefix_and_suffix` / `get_prompt`、注册表与按模型路径匹配 |
| [lang/choices.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py) | **选择策略** | `ChoicesSamplingMethod` 抽象、`TokenLengthNormalized` / `GreedyTokenSelection` / `UnconditionalLikelihoodNormalized` |
| [lang/api.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py) | **公共 API** | `system` / `user` / `assistant` 原语、`gen` 的 `choices` 退化、`select` 默认 `choices_method` |
| [lang/ir.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py) | **表达式对象** | `SglRoleBegin` / `SglRoleEnd` / `SglSelect` |
| [lang/interpreter.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py) | **解释器** | `_execute_role_begin` / `_execute_role_end`（渲染）、`_execute_select`（决策） |
| [lang/backend/runtime_endpoint.py](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py) | **自研后端** | `select` 如何取 logprob、按需取无条件 logprob、调用 `choices_method` |

一句话总览：`api.py` 提供角色原语与 `select` 入口，`ir.py` 把它们封装成表达式，`interpreter.py` 在执行期用 `chat_template.py` 的模板把角色渲染成文本、用 `choices.py` 的策略从候选里挑一个，`runtime_endpoint.py` 负责把算分所需的 logprob 从运行时取回来。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

- **4.1 角色原语**——`system` / `user` / `assistant` 如何造出带标签的表达式。
- **4.2 chat_template 渲染**——模板表如何驱动「扁平字符串」与「结构化消息」两种产物。
- **4.3 ChoicesSamplingMethod**——选择策略的统一抽象与三种内置实现。
- **4.4 token_length_normalized**——默认策略的数学含义，以及它和另两种策略的差异。

### 4.1 角色原语：给文本贴上角色标签

#### 4.1.1 概念说明

对话模型并不是把「用户说的话」和「助手说的话」原样拼在一起就能理解的。每个模型家族都有自己的对话格式：ChatML 用 `<|im_start|>user\n ... <|im_end|>\n`，Llama-2 用 `[INST] ... [/INST]`，Claude 用 `\n\nHuman: ` / `\n\nAssistant:`。这些格式差异由 **chat 模板** 承载。

前端不想让用户手写这些特殊 token，于是提供了三个角色原语：`sgl.system(expr)`、`sgl.user(expr)`、`sgl.assistant(expr)`。它们的作用只是「给一段文本打上角色标签」——具体这个标签渲染成什么特殊 token，由当前后端绑定的 `ChatTemplate` 决定。这样，同一份 `@function` 代码在换模型时不必改一个字符。

#### 4.1.2 核心流程

角色原语的实现非常薄。三者都走同一个内部函数 `_role_common`：

1. 调 `sgl.user("你好")` 时，`expr="你好"`，返回一个 `SglExprList([SglRoleBegin("user"), SglConstantText("你好"), SglRoleEnd("user")])`。
2. 若不传 `expr`（如 `sgl.assistant()`），则只产生 `SglRoleBegin` + `SglRoleEnd` 这一对「空角色」，常用来在 prompt 末尾「开一个 assistant 头」让模型续写。
3. 这些表达式被 `s += ...` 拼进程序，和 `gen` / `select` 一样是**惰性**的，真正执行发生在解释器阶段。

#### 4.1.3 源码精读

三个原语与公共实现（注意 `system`/`user`/`assistant` 只是给 `_role_common` 传了不同的角色名字符串）：

[lang/api.py:246-262](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L246-L262) — `_role_common`：有 `expr` 时夹在 `SglRoleBegin`/`SglRoleEnd` 中间，没有时只造一对空标签。

对应的两个表达式类只存了一个 `role` 字符串：

[lang/ir.py:515-530](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/ir.py#L515-L530) — `SglRoleBegin` / `SglRoleEnd`：它们不携带任何文本，只标记「从这里开始/结束是某个角色」。

解释器遇到 `SglRoleBegin` 时，先处理「默认 system 提示」的自动插入，再用模板查出该角色的 `prefix` 拼到正文里：

[lang/interpreter.py:665-681](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L665-L681) — `_execute_role_begin`：若当前还没有任何消息且第一个角色不是 `system`、而模板又有 `default_system_prompt`，就自动补一条 system 消息（这正是 Qwen 等模型「即使不写 system 也会自带一句 You are a helpful assistant.」的来源）；随后把角色的 `prefix` 填进 `text_`。

> 小结：角色原语本身**不含任何格式信息**，格式完全来自 `ChatTemplate`。下一节就看模板表。

#### 4.1.4 代码实践

**实践目标**：确认角色原语只造表达式、不立即执行，并看清它们包裹的结构。

**操作步骤**：

1. 写一段不 `.run()` 的 `@function`，在函数体里 `s += sgl.user("你好")`，然后直接打印函数对象或用 `trace` 取出 IR。
2. 阅读上面的 `_role_common`，预测打印结果里会出现 `RoleBegin(user)`、`Constant('你好')`、`RoleEnd(user)` 三个节点。

**预期结果**：你应该看到一个由 `SglRoleBegin` / `SglConstantText` / `SglRoleEnd` 组成的表达式序列，而**看不到**任何 `<|im_start|>` 或 `[INST]` 之类的特殊 token——因为那些是渲染期才由模板注入的。若实际运行结果与此不符，以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`sgl.assistant()`（不传参数）常用在程序末尾，它的作用是什么？

> **参考答案**：它产生一对「空的 assistant 角色标签」。渲染时只会写入 assistant 的 `prefix`（例如 ChatML 的 `<|im_start|>assistant\n`），相当于把「轮到助手说话了」这个信号拼进 prompt，引导模型从这里开始续写，而不预先填入任何助手文本。

**练习 2**：为什么不把 `<|im_start|>user` 这种特殊 token 直接写在 `user()` 原语里？

> **参考答案**：因为不同模型家族的格式不同。把格式耦合进原语会导致换模型时必须改业务代码。当前设计把「角色语义」和「渲染格式」分离：原语只表达「这是 user 角色」，格式交给可替换的 `ChatTemplate`，从而同一份 `@function` 可跨模型复用。

### 4.2 chat_template 渲染：从角色到扁平字符串

#### 4.2.1 概念说明

`ChatTemplate` 是一个数据类，核心是一张 `role_prefix_and_suffix` 表：`{角色名: (前缀, 后缀)}`。渲染对话就是把每条消息套上它角色的「前缀 + 内容 + 后缀」依次拼接。除前缀后缀外，它还携带 `default_system_prompt`（默认系统提示）、`stop_str`（停止字符串）、`image_token` / `audio_token`（多模态占位符）和 `style`（目前有 `PLAIN` 与 `LLAMA2` 两种，后者对 Llama-2 的 system/user 嵌套有特殊处理）。

项目内置了几十种模板（`default`、`chatml`、`qwen`、`llama-2-chat`、`llama-3-instruct`、`deepseek-v3`、`gemma-it` 等），并通过两类机制查找：

- **按名字精确取**：`get_chat_template(name)`。
- **按模型路径自动匹配**：`get_chat_template_by_model_path(model_path)` 依次跑一批正则匹配函数，命中即返回对应模板，全部不命中则回退到 `default`。

#### 4.2.2 核心流程

模板有**两个渲染入口**，共用同一张表：

1. **一次性渲染 `get_prompt(messages)`**：给一个消息字典列表 `[{role, content}, ...]`，循环每条，按角色查 `(prefix, suffix)`，拼成 `prefix + content + suffix`。若某条 `system` 的 `content is None`，则用 `default_system_prompt` 兜底（仍为 `None` 就跳过）。这是独立工具方法（例如文件末尾 `__main__` demo 就用它打印 Llama-2 的渲染结果）。
2. **执行期逐条渲染 `get_prefix_and_suffix(role, hist_messages)`**：解释器在 `_execute_role_begin` / `_execute_role_end` 里分别取 `prefix` 和 `suffix`，边执行边拼到 `text_`，同时把结构化消息追加进 `messages_`。

两条线产物不同：`text_` 是模型真正吃进去的**扁平字符串**，`messages_` 是结构化的 `[{role, content}]` 列表（供 OpenAI vision 等需要结构化输入的后端使用）。

LLAMA2 风格的特殊处理（`get_prefix_and_suffix` 里 `style == ChatTemplateStyle.LLAMA2` 分支）：当 system 是第一条消息时，把 system 塞进 user 的 `[INST]` 包裹里；当 user 是第一条且有内容时，去掉 user 自己的前缀。这正是 Llama-2 把 system prompt 嵌进第一个 `[INST]` 的格式要求。

#### 4.2.3 源码精读

`ChatTemplate` 数据类与两个渲染方法：

[lang/chat_template.py:12-54](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/chat_template.py#L12-L54) — `ChatTemplate`：字段定义 + `get_prefix_and_suffix`（含 LLAMA2 特殊分支）+ `get_prompt`（一次性渲染，含 `content is None` 时回退 `default_system_prompt`）。

对比两个典型模板体会格式差异（同一张表、不同内容）：

[lang/chat_template.py:105-117](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/chat_template.py#L105-L117) — `chatml` 模板：三角色都用 `<|im_start|>{role}\n ... <|im_end|>\n`，`stop_str` 为 `<|im_end|>`。

[lang/chat_template.py:184-195](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/chat_template.py#L184-L195) — `llama-2-chat` 模板：user 用 `[INST] ... [/INST]`，`style=ChatTemplateStyle.LLAMA2`，触发上面那个嵌套分支。

按模型路径自动匹配（注册了一批正则匹配函数，逐个尝试）：

[lang/chat_template.py:73-78](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/chat_template.py#L73-L78) — `get_chat_template_by_model_path`：遍历 `matching_function_registry`，第一个返回非 `None` 的模板名生效，否则回退 `default`。

后端在初始化时据此选定模板（自研后端优先用显式名字，否则按模型路径匹配）：

[lang/backend/runtime_endpoint.py:49-54](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L49-L54) — `RuntimeEndpoint` 构造期选模板：给了 `chat_template_name` 就精确取，否则用 `/get_model_info` 返回的 `model_path` 去匹配。

执行期的逐条渲染（解释器用 `get_prefix_and_suffix` 取前后缀）：

[lang/interpreter.py:683-717](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L683-L717) — `_execute_role_end`：取出 `suffix` 拼到 `text_`，再把 `{role, content}` 追加进 `messages_`（无图时走普通 chat 格式，有图时走 OpenAI vision 多模态格式）。

> 小结：`ChatTemplate` 是「格式数据库」，角色原语是「取格式请求」，解释器是「执行渲染的引擎」。

#### 4.2.4 代码实践

**实践目标**：用 `user()` / `assistant()` 拼一段多轮对话，并对照模板表解释渲染结果。

**操作步骤**：

1. 起一个本地 sglang 服务（或用 `sglang.Engine`），模型选一个 ChatML 家族的（如 Qwen2.5）。
2. 写一个 `@function`：`s += sgl.system("你是一个翻译助手"); s += sgl.user("hello"); s += sgl.assistant("你好"); s += sgl.user("thanks")`，先 `trace` 再 `run`。
3. 在解释器的 `_execute_role_end` 处加一行 `print(self.text_)`，观察每追加一条消息后扁平字符串的增长。

**预期结果**：扁平字符串应形如

```
<|im_start|>system
你是一个翻译助手<|im_end|>
<|im_start|>user
hello<|im_end|>
<|im_start|>assistant
你好<|im_end|>
<|im_start|>user
thanks<|im_end|>
```

（具体 token 形态以所选模型模板为准；若模型非 ChatML 家族，前缀后缀会不同。）「待本地验证」：实际渲染串以本地 `print` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：`get_prompt` 和解释器里的逐条渲染，为什么会「共用同一张表却产出两种东西」？

> **参考答案**：`get_prompt` 是面向「已经有一组完整消息字典」的工具方法，一次性拼出扁平字符串（常用于调试 / 独立渲染）；解释器则是「边执行边生成」，每遇到一个角色原语就即时取前后缀拼到 `text_`，同时维护结构化 `messages_` 给需要结构化输入的后端（如 OpenAI vision）。两者都调用 `get_prefix_and_suffix` 查同一张 `role_prefix_and_suffix` 表，所以格式一致，但调用时机和附带产物不同。

**练习 2**：为什么 Qwen 即使你不写 `sgl.system(...)`，渲染出来的 prompt 里也会有一句 `You are a helpful assistant.`？

> **参考答案**：因为 `qwen` 模板的 `default_system_prompt="You are a helpful assistant."`，而 `_execute_role_begin` 在「还没有任何消息且首个角色不是 system」时会自动插入一条用 `default_system_prompt` 填充的 system 消息（见 4.1.3 引用的代码）。Llama-2 等模板 `default_system_prompt=None`，就不会自动补。

### 4.3 ChoicesSamplingMethod：候选选择的统一抽象

#### 4.3.1 概念说明

`select` 的本质是「让模型在几个候选字符串里挑一个」。它不像 `gen` 那样自由采样，而是**比较每个候选在当前上下文下的似然**，取最高者。但「似然高」有多种定义：是看总对数概率？平均对数概率？减去词频偏差后的「信息增益」？还是逐 token 贪心淘汰？这些就是 `choices_method`。

`ChoicesSamplingMethod` 是这套策略的统一抽象：每个策略都是一个可调用对象，接收同一组「原料」（候选列表 + 各种 logprob 数组），返回一个 `ChoicesDecision(decision, meta_info)`。`decision` 是胜出的候选字符串，`meta_info` 携带打分细节供调试。

关键设计点：抽象基类有一个属性 `requires_unconditional_logprobs`（默认 `False`）。只有需要「无条件似然」的策略（目前仅 `UnconditionalLikelihoodNormalized`）才把它设为 `True`，后端据此决定要不要**额外发一次前向请求**去算无条件 logprob。这是一个「能力声明 → 后端按需取数」的协作约定。

#### 4.3.2 核心流程

一次 `select` 的完整链路（以自研 `RuntimeEndpoint` 后端为例）：

1. 解释器 `_execute_select` 调 `backend.select(s, choices, temperature, choices_method)`。
2. 后端先 cache 公共前缀，再对每个候选 `c` 发一次「`s.text_ + c`、`max_new_tokens=0`、`return_logprob=True`」的请求，拿到每个候选的 `input_token_logprobs`（候选部分各 token 的对数概率）。
3. 后端用 `compute_normalized_prompt_logprobs` 把每个候选的 token logprob 平均成 `normalized_prompt_logprobs`（一个标量），并做 token healing 修正。
4. 若 `choices_method.requires_unconditional_logprobs` 为真，**再发一次**前向（用候选的 input_ids、不带上下文）取 `unconditional_token_logprobs`；否则置 `None`。
5. 把所有原料传给 `choices_method(...)`，它内部算分并返回 `ChoicesDecision`。
6. 解释器把 `decision` 写回 `variables[name]` 并拼进 `text_`。

#### 4.3.3 源码精读

抽象基类与决策结果容器：

[lang/choices.py:8-29](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L8-L29) — `ChoicesDecision`（装 `decision` + `meta_info`）与 `ChoicesSamplingMethod`（抽象 `__call__` + `requires_unconditional_logprobs` 属性，默认 `False`）。

后端如何按能力声明决定是否取无条件 logprob，并最终调用策略：

[lang/backend/runtime_endpoint.py:294-315](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L294-L315) — `select` 末段：`if choices_method.requires_unconditional_logprobs:` 才发额外前向取 `unconditional_token_logprobs`，否则 `None`；最后把全部原料传给 `choices_method(...)`。

解释器侧的入口（确认 `choices_method` 从 `SglSelect` 一路透传到后端）：

[lang/interpreter.py:647-658](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/interpreter.py#L647-L658) — `_execute_select`：`self.backend.select(self, expr.choices, expr.temperature, expr.choices_method)`，返回的 `decision` 写入 `variables` 与 `meta_info`。

`api.py` 里默认策略的绑定（`select` 与带 `choices` 的 `gen` 都默认 `token_length_normalized`）：

[lang/api.py:236-243](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L236-L243) — `select`：默认参数 `choices_method=token_length_normalized`。

[lang/api.py:102-108](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/api.py#L102-L108) — `gen` 带 `choices` 时退化为 `SglSelect`，未指定 `choices_method` 时同样回退到 `token_length_normalized`。

> 小结：策略对象是「纯函数式的评分器」，所有外部依赖（logprob）都由后端按 `requires_unconditional_logprobs` 声明提前备齐再喂给它。

#### 4.3.4 代码实践

**实践目标**：理解「能力声明 → 后端按需取数」这条协作链。

**操作步骤**：

1. 阅读 `runtime_endpoint.py` 的 `select`（[248-315](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L248-L315)），数一下它对运行时发了**几次** HTTP `/generate` 请求。
2. 分别用默认 `token_length_normalized` 和 `unconditional_likelihood_normalized` 写两个 `select`，对照 `requires_unconditional_logprobs` 推断后者会多触发一次前向。

**预期结果**：默认策略下 `select` 对运行时的请求次数主要取决于「缓存前缀 1 次 + 每个候选 1 次打分」（具体次数以源码与日志为准）；换成 `unconditional_likelihood_normalized` 后会**额外**多一组无条件 logprob 的前向请求。「待本地验证」：可在 `/generate` 路径或后端加日志统计实际请求次数。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `requires_unconditional_logprobs` 设计成策略对象的属性，而不是后端硬编码？

> **参考答案**：因为「是否需要无条件 logprob」是**策略本身**的能力需求，只有策略知道自己要不要用这份数据。把它声明为属性，后端只需在调策略前查一下这个布尔值，决定要不要多发一次前向——既避免了「所有策略都白算一次无条件 logprob」的浪费，也避免了「后端替策略猜测」的耦合。这是典型的「让消费者声明需求、让生产者按需供给」。

**练习 2**：`ChoicesDecision` 里除了 `decision` 为什么还要带 `meta_info`？

> **参考答案**：`meta_info` 把打分过程的关键中间量（如 `normalized_prompt_logprobs`、`input_token_logprobs`、无条件 logprob 等）一并返回，供调试、可解释性分析和单元测试断言使用。解释器会把它存进 `self.meta_info[name]`，让用户事后能复盘「为什么选了这个候选」。

### 4.4 token_length_normalized：默认策略与归一化的数学含义

#### 4.4.1 概念说明

`token_length_normalized` 是默认策略：**取「平均对数概率」最高的候选**。为什么要「平均」而不是「总和」？因为不同候选 tokenize 后长度不同，长的候选累积的对数概率天然更负（每多一个 token 就多乘一个 < 1 的概率），如果直接比总和，长候选永远吃亏。用「平均」（除以 token 数）就把长度差异抹平了，比较的是「平均每个 token 有多合理」。

另外两种内置策略各有侧重：

- `greedy_token_selection`：把所有候选补齐到同一长度（短缺的用自身平均 logprob 填充），然后**逐 token 列**比谁这一列最大，逐轮淘汰候选，直到只剩一个。它对「一个候选是另一个候选前缀」这种重叠情况更稳健。
- `unconditional_likelihood_normalized`：把每个 token 的上下文对数概率**减去**它的「无条件对数概率」（即没有任何上下文时这个词自身的常见度），再取平均。这等价于平均**逐点互信息 PMI**，能消除「某个候选只是因为用了高频常用词就得分高」的偏差，更接近「真正因为上下文才合理」的度量。代价是需要额外一次前向算无条件 logprob。

#### 4.4.2 核心流程与数学

设候选 \(c\) 在上下文下被切成 token \(t_1, \dots, t_n\)，记其在上下文条件下的对数概率为 \(\log P(t_i \mid \text{ctx})\)。

**token_length_normalized（默认）** 的打分：

\[
\text{score}(c) = \frac{1}{n}\sum_{i=1}^{n} \log P(t_i \mid \text{ctx})
\]

取 \(\arg\max_c \text{score}(c)\)。这正是后端 `compute_normalized_prompt_logprobs`（求和后除以个数）算出的 `normalized_prompt_logprobs`。

**unconditional_likelihood_normalized** 的打分：

\[
\text{score}_{\text{PMI}}(c) = \frac{1}{n}\sum_{i=1}^{n} \Big[\log P(t_i \mid \text{ctx}) - \log P(t_i \mid \text{uncond})\Big]
\]

其中 \(\log P(t_i \mid \text{uncond})\) 是「不带上下文」时该 token 的对数概率（由额外那次前向给出；首个 token 的无条件 logprob 若为 `None` 则按 0 处理）。减去它相当于扣除「词频先验」，衡量的是上下文带来的**信息增益**。

伪代码（默认策略）：

```
对每个候选 c:
    取 c 在上下文下的 token logprobs
    score[c] = mean(这些 logprobs)
返回 argmax(score) 对应的候选
```

#### 4.4.3 源码精读

默认策略实现（一行 `np.argmax`）：

[lang/choices.py:32-53](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L32-L53) — `TokenLengthNormalized`：`best_choice = choices[np.argmax(normalized_prompt_logprobs)]`，把打分细节塞进 `meta_info`；模块末尾导出单例 `token_length_normalized`。

逐 token 贪心策略（含补齐矩阵与淘汰循环）：

[lang/choices.py:56-107](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L56-L107) — `GreedyTokenSelection`：`_build_logprob_matrix` 把短缺候选用自身平均 logprob 填到 `max_tokens` 列宽，`_greedy_selection` 逐列保留该列最大者、淘汰其余，直到剩一个。

PMI 风格策略（声明需要无条件 logprob）：

[lang/choices.py:110-164](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/choices.py#L110-L164) — `UnconditionalLikelihoodNormalized`：`requires_unconditional_logprobs` 为 `True`；`_normalize_logprobs` 对每个候选算 `mean(input_logprob - unconditional_logprob)`（首个无条件 logprob 用 `or 0` 兜底），再 `argmax`。

后端侧的「平均对数概率」计算器（默认策略的原料来源）：

[lang/backend/runtime_endpoint.py:351-353](https://github.com/sgl-project/sglang/blob/d0b9689805232d8ab37789121cbc3b766b5c723e/python/sglang/lang/backend/runtime_endpoint.py#L351-L353) — `compute_normalized_prompt_logprobs`：`sum(x[0] for x in input_logprobs if x[0]) / len(values)`，即过滤 `None` 后取平均（对应上面公式里的 \(1/n\) 求和）。

> 小结：三种策略的差别全在「打分函数」——平均似然、逐列贪心、PMI——而把它们组装起来的外壳（取 logprob、token healing、按需取无条件 logprob、argmax 落地）是共享的。

#### 4.4.4 代码实践

**实践目标**：对同一个 `select`，分别用 `token_length_normalized` 与 `unconditional_likelihood_normalized`，观察并解释结果差异。

**操作步骤**：

1. 起本地 sglang 服务（自研 `RuntimeEndpoint` 后端，因为只有它能拿到 token 级 logprob）。
2. 写一个 `@function`，含一个 `select`：候选用一个「长但平庸」的选项和一个「短但贴切」的选项，例如 `choices=["非常非常非常常见的回答", "贴切"]`。
3. 第一遍用默认 `choices_method`（即 `token_length_normalized`）跑，打印 `state["ans"]` 和 `meta_info`。
4. 第二遍显式传 `choices_method=unconditional_likelihood_normalized`（需 `from sglang.lang.choices import unconditional_likelihood_normalized`），再跑一次。

**需要观察的现象**：

- 默认策略：候选分数是「平均每 token 对数概率」，长候选不会被长度惩罚。
- PMI 策略：扣除了词频先验，原本因「高频词多」而占便宜的长候选分数可能下降，两个策略可能选出不同结果。
- PMI 策略会比默认策略多触发一次「无条件 logprob」的前向请求（见 4.3.4）。

**预期结果**：两个 `choices_method` **可能**给出不同的 `decision`；即便相同，`meta_info` 里的打分数值也不同（PMI 版多了 `unconditional_token_logprobs` 与 `normalized_unconditional_prompt_logprobs` 字段）。「待本地验证」：具体选哪个候选、数值多少，取决于模型与上下文，以本地实际输出为准；本实践的重点是理解「同一组 logprob、不同打分规则 → 可能不同结论」，而非某个固定答案。

#### 4.4.5 小练习与答案

**练习 1**：如果 `select` 直接用「总对数概率」而非「平均对数概率」来比较，会有什么问题？

> **参考答案**：候选 tokenize 后长度不同，对数概率是负数累加，候选越长总和越负。直接比总和会让长候选系统性吃亏，几乎永远选最短的那个。除以 token 数（取平均）消除了长度差异，比较的是「平均每 token 的合理性」，更符合「哪个候选整体更贴合上下文」的直觉。这就是 `token_length_normalized` 名字里 normalized 的含义。

**练习 2**：`unconditional_likelihood_normalized` 为什么要「减去无条件对数概率」？用一个直觉例子说明。

> **参考答案**：因为有些词本身就很常见（如 "the"、"是的"），即使上下文不强相关，它们的条件对数概率也不会太低，会拉高候选分数。减去「无上下文时的对数概率」相当于扣除这部分「词频先验」，剩下的是「因为有了这个上下文才多出来的合理性」——即信息增益 / PMI。例如候选 A 全是高频常用词、候选 B 用了更贴切但稍少见的词：默认策略可能因 A 词词频高而偏向 A；PMI 策略扣除词频后更可能偏向真正贴合上下文的 B。

**练习 3**：`greedy_token_selection` 相比 `token_length_normalized`，在什么场景下结论可能不同？

> **参考答案**：当候选之间有「前缀重叠」时（例如 `["是", "是的", "是的，没问题"]`），`token_length_normalized` 各自独立算平均；而 `greedy_token_selection` 把它们补齐到同一列宽后**逐 token** 比较，短候选用自身平均 logprob 填充后续列。这样它能在「一个候选是另一个候选前缀」时做出更稳健的逐位比较，而非被平均抹平。两者结论在候选长度悬殊或有重叠时可能不同。

## 5. 综合实践

把本讲两条线串起来：**渲染线**（角色原语 → 模板 → 扁平 prompt）+ **决策线**（`select` → logprob → `choices_method`）。

任务：写一个「多轮对话 + 选择题」的小程序。

1. 用 `sgl.system` / `sgl.user` / `sgl.assistant` 构造一段两轮的多轮对话，主题自定（例如「请判断下面这句话的情感」）。
2. 在末尾用 `sgl.assistant()` 开一个助手头，紧接一个 `sgl.select(name="label", choices=["正面", "负面", "中性"])`。
3. 运行后打印 `state["label"]` 和对应的 `meta_info`（可在 `@function` 内通过 `s.meta_info["label"]` 读取，或查看解释器存储）。
4. 把 `select` 的 `choices_method` 分别换成默认、`greedy_token_selection`、`unconditional_likelihood_normalized`（后两者需从 `sglang.lang.choices` 导入），各跑一次，记录三者的 `decision` 与请求次数差异。
5. 用一段话解释：为什么在某些候选项组合下三者可能给出不同答案，并指出哪种策略额外多花了一次前向。

**验收标准**：

- 能正确用角色原语渲染出符合所选模型模板的扁平 prompt（通过在 `_execute_role_end` 加 `print(self.text_)` 验证）。
- 能说清三种 `choices_method` 的打分差异，以及 `requires_unconditional_logprobs` 如何影响后端请求次数。
- 「待本地验证」：具体 `decision` 与数值以本地实际运行为准。

## 6. 本讲小结

- 角色原语 `system` / `user` / `assistant` 只造「带角色标签的表达式」（`SglRoleBegin` / `SglRoleEnd`），不含任何格式信息；格式由 `ChatTemplate` 的 `role_prefix_and_suffix` 表决定，从而实现「角色语义」与「渲染格式」解耦。
- `ChatTemplate` 有两个渲染入口共用同一张表：`get_prompt(messages)` 一次性渲染扁平字符串，解释器则用 `get_prefix_and_suffix` 边执行边拼 `text_`、同时维护结构化 `messages_`；`default_system_prompt` 会在首条非 system 消息时被自动补成 system。
- `select` 的本质是「比较各候选在上下文下的似然取最高者」，原料是后端取回的 token 级 logprob；`choices_method` 是可替换的「评分规则」，返回 `ChoicesDecision(decision, meta_info)`。
- 默认策略 `token_length_normalized` 取「平均对数概率」最高者，用 \(1/n\) 求和消除候选长度差异；`greedy_token_selection` 逐 token 贪心淘汰；`unconditional_likelihood_normalized` 减去无条件似然（PMI 风格），需额外一次前向。
- `requires_unconditional_logprobs` 属性是「策略声明需求、后端按需取数」的协作约定，避免所有策略都白白多算一次无条件 logprob。
- 角色渲染与候选选择是前端两条相对独立的线：渲染线产出模型输入，决策线在 `select` 处把多个候选收敛成一个结果写回变量。

## 7. 下一步学习建议

- **回到运行时主链路**：本讲止步于前端 `lang/`。要理解 `select` 取 logprob 依赖的 `/generate`（`return_logprob`、`input_token_logprobs`）是如何在运行时算出来的，进入第 4 单元「调度核心与 Manager 管道」，尤其是 u4-l2（TokenizerManager）与 u4-l3（Scheduler 事件循环）。
- **采样与 logits**：`choices_method` 用的 logprob 来自模型 logits；若想理解 logprob 本身如何从 logits 计算与批量化，可预习第 5 单元（ModelRunner / ForwardBatch）与 u8-l6（采样参数与 logits 处理）。
- **结构化输出的近亲**：`select` 的「在候选中挑一个」与 u8-l4「结构化输出与约束解码」用文法 mask 限定下一个合法 token 是同一类思想的两种形态，学完本讲后对照阅读会有收获。
- **自定义策略**：若想实现自己的 `choices_method`，参照 `choices.py` 里三个类，继承 `ChoicesSamplingMethod`、实现 `__call__`，并按需覆写 `requires_unconditional_logprobs`，即可通过 `sgl.select(..., choices_method=你的策略)` 直接使用。
