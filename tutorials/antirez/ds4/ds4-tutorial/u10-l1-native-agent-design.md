# 原生编码 Agent 设计

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `ds4-agent` 与 `ds4-server`（u7）在架构上的根本区别：**推理就在 agent 进程内，会话就是磁盘 KV 缓存本身**。
- 解释为什么在这种「KV-as-session」架构下，**KV mismatch 按构造不可能发生**——并能在源码里指出保证这一点的具体代码。
- 理解「垂直设计」的三个含义：系统提示与工具专为 DeepSeek V4 调、工具调用原生走 DSML 无 JSON 转换、低延迟体验主要受 prefill 速度约束。
- 掌握会话持久化命令族 `/save` `/list` `/switch` `/del` `/strip` `/new` 背后的机制：会话身份（SHA）如何生成、磁盘 `.kv` 文件如何写、加载时如何免 prefill 直接恢复、`/strip` 后如何用渲染文本重建。

## 2. 前置知识

本讲是 advanced 层，依赖你已经建立的两条认知（否则请先读对应讲义）：

- **u2-l3 Session 同步与前缀复用**：`ds4_session` 用 `checkpoint`（KV 当前对应的 token 序列）刻画状态；`ds4_session_common_prefix` 测量前缀长度；`ds4_session_sync` 在 checkpoint 是 prompt 的前缀时只增量评估后缀，否则整段重建。本讲会反复用到这两个原语。
- **u3-l3 分词器与聊天模板渲染**：文本→token 靠字节级 BPE 与聊天模板；DSML 是 DeepSeek V4 的工具调用文本格式，以全角竖线包裹的 `｜DSML｜` 标记表达，模型直接以 token 流生成。

另外有两点背景对照，理解了会事半功倍：

1. **对照 u7（HTTP 服务器）**。服务器是「无状态 API」：客户端每次把整段对话以 JSON 重发，服务器要重新渲染成 token、再用多级回退链（活 token 前缀 → 渲染字节比对 → 磁盘快照 → 冷 prefill）去复用 KV。这条链之所以复杂，正是因为「重渲染的字节」很难和「模型当初采样的字节」逐字节相等，于是在工具调用处会 KV 前缀失配，需要「精确 DSML 回放」「规范化」等大量机制（见 u7-l4、u7-l5）。
2. **`ds4-agent` 走了相反的路**。它干脆不做无状态 API：会话状态常驻进程，token 序列只追加、不重渲染，于是上面那套复杂机制统统不需要。本讲的核心就是论证这一句。

几个术语先约定：

| 术语 | 含义 |
|---|---|
| transcript | 会话的 token 序列（`ds4_tokens`），整个对话的唯一真相（single source of truth） |
| live session / 活会话 | 进程内的 `ds4_session`，持有 KV 缓存与当前 logits |
| KV-as-session | 「会话 = 磁盘 KV 缓存文件」的设计哲学 |
| sysprompt.kv | 系统提示的固定 KV 检查点，避免反复为系统提示付 prefill 代价 |
| stripped 会话 | 只保留渲染文本与标题、删掉重 KV payload 的 `.kv` 文件 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `ds4_agent.c` | agent 的全部实现：CLI 解析、UI 线程、worker 线程、会话保存/加载/切换/剥离、DSML 工具流式渲染、内建工具。约 1 万行，本讲只看其中「设计与生命周期」部分 |
| `ds4.h` | 引擎公共边界。本讲用到 `ds4_session_sync` / `ds4_session_common_prefix` / `ds4_session_tokens` / `ds4_session_eval` / `ds4_session_load_payload` 等签名 |
| `README.md` | `Native agent` 一节给出官方对「KV mismatch impossible by construction」「session 即磁盘 KV」等设计取舍的说明 |

> 本讲**不**展开内建工具（read/write/edit/bash/search 等）的参数与执行——那是 u10-l2 的主题；也不展开 web 搜索——那是 u10-l3。

## 4. 核心概念与源码讲解

### 4.1 进程内推理与 KV-as-session

#### 4.1.1 概念说明

大多数 LLM agent 系统是「客户端 + 服务端」两层：一个 agent 编排进程通过 HTTP/WebSocket socket 调一个推理服务。`ds4-agent` 的 README 一开头就点明它不是这样：

> the inference is controlled from within the agent itself, without socket/API boundaries, so the session is represented by the on-disk KV cache itself.

这句话拆成两个断言：

1. **进程内推理**：推理（prefill、decode、采样）就在 agent 进程里直接发生，没有 socket、没有 HTTP、没有 JSON 序列化边界。
2. **会话即磁盘 KV**：一条「会话」在物理上就是一个 `.kv` 文件（即 u8-l1 讲过的 KVC 文件格式）。会话的权威状态不是某个数据库里的消息表，而是这个 KV 检查点本身。

这两个断言合起来就是 **KV-as-session**：你保存会话 = 把活 KV 缓存序列化落盘；恢复会话 = 把 KV 缓存反序列化回内存，**跳过 prefill**。

#### 4.1.2 核心流程

`ds4-agent` 是一个**单进程、双线程**的程序：

- **UI 线程**（main 所在线程）：拥有终端输入输出、跑 linenoise 行编辑器、解析斜杠命令。
- **worker 线程**：独占活 `ds4_session` 与 KV 状态，串行执行所有推理（prefill + decode + 采样）。

为什么把推理单独放到 worker 线程？因为 UI 必须在模型生成长文本时仍然能响应按键（中断、排队输入），而推理又是重活——用独立线程把两者解耦。文件头注释把这条设计写得很清楚：

[ds4_agent.c:40-48](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L40-L48) —— 注释说明 agent 故意是单进程：UI 线程拥有终端，worker 线程拥有活 DS4 session 与 KV 状态。

这两个线程通过一个共享结构 `agent_worker` 衔接，它把「进程内推理」所需的全部状态收拢在一起：

[ds4_agent.c:98-149](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L98-L149) —— `agent_worker` 结构体：持有 `engine`、`session`、`transcript`、`cache_dir`、`session_sha`、互斥锁/条件变量 `mu`/`cond`、唤醒管道 `wake_fd` 等。

注意其中三个关键字段的耦合关系：

- `ds4_session *session` —— 活会话（KV 缓存 + logits）。
- `ds4_tokens transcript` —— 会话的 token 序列（唯一真相）。
- `char session_sha[41]` —— 当前会话的身份（文件名）。

整个生命周期由 `main` 串起：

[ds4_agent.c:10215-10243](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10215-L10243) —— `main`：解析选项 → `ds4_engine_open`（u2-l1）打开引擎 → 安装 SIGINT 处理 → 进入 `run_agent`（交互）或 `run_agent_non_interactive`（一次性）→ 关闭引擎。

打开引擎之后，`run_agent` 会调用 `agent_worker_init`，它创建活会话、建缓存目录、起 worker 线程：

[ds4_agent.c:9421-9466](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9421-L9466) —— `agent_worker_init`：`ds4_session_create` 建活会话、`agent_default_cache_dir` 建 `~/.ds4/kvcache`、`pthread_create(&w->thread, NULL, worker_main, w)` 起 worker 线程。

worker 线程的主循环是一个「等条件变量 → 取命令 → 跑一轮」的循环：

[ds4_agent.c:8068-8123](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L8068-L8123) —— `worker_main`：启动时先建系统提示（`agent_worker_reset_to_sysprompt`），然后循环等待 `cmd_text`/`save_requested`/`compact_requested`/`power_requested`，有用户文本就 `worker_run_turn`。

UI 线程要发起一轮对话时，调用 `worker_submit` 把文本塞进 `w->cmd_text` 并唤醒 worker：

[ds4_agent.c:8163-8184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L8163-L8184) —— `worker_submit`：仅当 worker 空闲时才接收文本，复制进 `cmd_text`，状态置 `AGENT_WORKER_PREFILL`，发条件变量信号。

注意 `worker_submit` 是「忙时拒绝」的（`ok = ... w->status.state == AGENT_WORKER_IDLE ...`）：UI 不会把文本静默排队，而是让用户继续编辑，这样输入始终可改。这是「低延迟体验」的一个细节。

#### 4.1.3 源码精读：为什么 KV mismatch 按构造不可能

这是本讲的核心论证。它由两段代码共同保证。

**第一段：生成时双写。** 每采样出一个 token，worker 同时做两件事——把它喂进活会话（推进 KV），又把它追加进 transcript：

[ds4_agent.c:7530-7533](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7530-L7533) —— `worker_accept_generated_token` 的核心：先 `ds4_session_eval(w->session, token, ...)` 推进活会话 KV，紧接着 `ds4_tokens_push(&w->transcript, token)` 把**同一个 token id** 追加进 transcript。

关键是「同一个 token id」。token 是整数 id（来自词表），不是文本；这里不存在「先生成文本、再重新分词」的步骤，因此不可能出现「重渲染的字节和当初采样的字节不一致」。活会话的 KV 所对应的 token 序列，和 transcript 里的 token 序列，是被同一行代码同步写入的两份拷贝——它们必然逐 id 相等。

**第二段：保存时断言。** 当把会话落盘时，保存函数会做一个防御性检查：活会话的 token 序列必须和待保存的 transcript 逐 id 相等，否则拒绝保存：

[ds4_agent.c:3880-3884](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3880-L3884) —— `agent_kv_save_path` 开头：`ds4_session_tokens(w->session)` 取活会话的 token 序列，与传入的 `tokens`（transcript）用 `agent_tokens_equal` 比较，不等则报 `"live KV state does not match session transcript"` 并返回失败。

把两段串起来看：生成时双写保证了「活 KV token 序列 == transcript」，所以保存时的断言在正常路径上**永远成立**。这就是 README 说的：

> KV cache mismatch are impossible by construction, the current state is always the truth.

[README.md:521](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L521) —— 官方对「KV mismatch 按构造不可能」的说明。

**对照服务器（u7）的痛苦。** 在 u7-l4/u7-l5 里，服务器要面对的问题是：客户端用 JSON 重发整段历史，服务器重新渲染成 token，而重渲染出的 DSML 字节很难和模型当初采样的 DSML 逐字节相等，于是 `ds4_session_common_prefix` 在工具调用处断开、后缀要重 prefill。服务器为此发明了「精确 DSML 回放（rax 基数树）」「规范化改写 checkpoint」「语法 token 强制贪婪」等一大套机制。`ds4-agent` 因为是状态ful 进程内推理、只追加 token id、从不重渲染，**这整层复杂度都不存在**——这就是 KV-as-session 架构最大的工程红利。

#### 4.1.4 前缀复用：状态ful 不代表每次都从头 prefill

即便会话常驻，agent 也复用 KV。多轮对话里，每追加一条用户消息或工具结果，transcript 变长，worker 调用 `ds4_session_common_prefix` 测量「活会话已覆盖到哪里」，再让 `ds4_session_sync` 只 prefill 新后缀：

[ds4_agent.c:7698-7729](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7698-L7729) —— `worker_run_turn` 里每一轮工具循环的开头：`common = ds4_session_common_prefix(...)` 算前缀，`suffix = len - cached`，再 `ds4_session_sync` 只评估后缀。

这套语义正是 u2-l3 讲过的「checkpoint 是 prompt 的前缀时只增量评估后缀」。在 agent 里它几乎总能命中纯追加情形（因为对话只会往后长），所以前缀复用率极高、增量 prefill 很短。配合「活 KV == transcript」的不变量，这里永远不会走到 u2-l3 里那种「中间改写须重建」的分支。

#### 4.1.5 代码实践

> **实践目标**：在源码里亲眼追踪「KV mismatch 按构造不可能」这条断言的两端，确认它由双写保证。
>
> **操作步骤**：
> 1. 打开 `ds4_agent.c`，定位 `worker_accept_generated_token`（约 7523 行），确认它的前两行是 `ds4_session_eval(...)` 与 `ds4_tokens_push(&w->transcript, token)`——即「同一个 token id 同时进活会话与 transcript」。
> 2. 在文件里搜索 `worker_accept_generated_token` 的所有调用点（生成循环、`worker_force_generated_text`），确认**每条**让活会话前进的路径都经过这个函数、都同步 push 了 transcript，不存在「只 eval 不 push」或「只 push 不 eval」的旁路。
> 3. 定位 `agent_kv_save_path`（约 3873 行），读开头 `agent_tokens_equal(live, tokens)` 的断言与失败分支。
> 4. 在 `ds4.h`（约 278 行）确认 `ds4_session_tokens` 返回的就是活会话 checkpoint 对应的 token 序列。
>
> **需要观察的现象**：eval 与 push 在同一个函数里、紧挨着、操作同一个 `token` 变量；没有任何一条生成路径绕过这个配对。
>
> **预期结果**：你能用一句话回答——「因为每个推进 KV 的 token 都被同一行代码同步追加进 transcript，二者是被原子地一起写的两份相等拷贝，所以保存时的相等性断言恒成立」。如果你找到一条「推进 KV 却不同步 push transcript」的路径，那就是这条不变量被破坏的 bug。
>
> 待本地验证项：本实践是源码阅读型，无需运行；若要运行验证，可在带 GPU 的机器上 `./ds4-agent` 跑一轮后 `/save`，确认保存成功（即断言通过）。

#### 4.1.6 小练习与答案

**练习 1**：如果有人把 `worker_accept_generated_token` 里的 `ds4_tokens_push(&w->transcript, token)` 删掉，只保留 `ds4_session_eval`，表面上看模型照样能生成文本。这会破坏什么不变量？在哪个函数会先暴露？

> **答案**：会破坏「活 KV token 序列 == transcript」。后果是 transcript 滞后于活会话：下一轮 `ds4_session_common_prefix` 会以为前缀更短、把已经算过的 token 当后缀重 prefill；而 `/save` 时 `agent_kv_save_path` 的 `agent_tokens_equal(live, tokens)` 断言会失败，报 `"live KV state does not match session transcript"`，保存直接被拒。这正说明那条断言是这道不变量的守门员。

**练习 2**：`ds4-server`（u7）需要「精确 DSML 回放」和「规范化改写 checkpoint」，而 `ds4-agent` 完全不需要这两套机制。用一句话说清根本原因。

> **答案**：服务器面对的是无状态客户端用 JSON 重发历史、必须重新渲染成 token，重渲染字节可能与原采样字节不一致；agent 是状态ful 进程内推理，token id 只追加、从不重渲染，活 KV 与 transcript 恒等，根本没有「重渲染失配」这个问题。

### 4.2 垂直设计（Vertical Design）

#### 4.2.1 概念说明

README 用了一个词叫 **vertically designed for DeepSeek v4 Flash and PRO**——「垂直设计」。在这里它的意思是：agent 不是一个「能接任意模型的通用编排框架」，而是**专门为 DeepSeek V4 这一个模型**定制的。系统提示、工具格式、采样默认值、上下文预算，都只对着这一个模型调。

这跟 u1-l1 讲过的 ds4 总体哲学「窄而精」是一脉相承的：ds4 引擎本身就是「一次只死磕 DeepSeek V4」，agent 自然延续这个取向。

垂直设计在 agent 里落地为三件事：

1. **原生 DSML，无 JSON 转换**：模型直接用 DSML 文本格式产出工具调用，agent 在 token 流里就地解析，不与任何外部 JSON 工具协议互相转换。
2. **低延迟体验**：因为没有 socket/序列化往返，显示生成文本、触发工具调用、开新会话都几乎瞬时，延迟主要被 prefill 速度约束。
3. **一切为这个模型调优**：默认 think 模式、采样参数、上下文窗口都按 DeepSeek V4 的特性选定。

#### 4.2.2 核心流程：原生 DSML 的工具迭代

agent 的核心循环 `worker_run_turn` 处理一条用户消息，但**一条用户消息可能引发很多轮「assistant 生成 → 工具调用 → 工具结果 → 继续」**。编码 agent 天然会做很长的 read/edit/test 循环，所以这里**故意没有「工具调用次数上限」**——上下文压力、压缩（compaction）、用户 Ctrl+C、模型给出最终答案，才是真正的停止条件：

[ds4_agent.c:7635-7682](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7635-L7682) —— `worker_run_turn` 开头与工具循环注释：明确「transcript 是唯一真相」，DSML 段完成后结束当前 assistant 消息、把工具结果作为 tool 消息追加、再让模型继续，全程无客户端/服务端协议。

「无客户端/服务端协议」这句是关键。对比 u7 的服务器：客户端发 OpenAI/Anthropic 风格的 JSON，服务器要把它解析、渲染成 prompt token、再把模型产出的 DSML 投影回 OpenAI/Anthropic 的工具调用对象（u7-l4）。agent 把这整层「方言翻译」全砍掉了——它直接消费模型原生的 DSML token 流。

每个生成的 token 在喂进会话的同时，也被流式喂给一个渲染器，由渲染器识别 `<｜DSML｜tool_calls>` 等标记、把工具调用可视化地画到终端（这部分细节属于 u10-l2）。当一段 DSML 解析完成（`dsml.state == AGENT_DSML_DONE`），循环就退出当前生成、执行工具、把结果追加进 transcript，再进入下一轮：

[ds4_agent.c:7823-7826](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7823-L7826) —— 生成循环里检测 `dsml.state == AGENT_DSML_DONE` 即 `got_tool = true` 并 `break`，交给后续逻辑执行工具。

#### 4.2.3 默认值即调优证据

垂直设计最直接的证据是 `parse_options` 里写死的默认值——它们不是「安全通用值」，而是为 DeepSeek V4 选的：

[ds4_agent.c:512-528](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L512-L528) —— `parse_options` 的默认配置：默认模型 `ds4flash.gguf`、上下文 `ctx_size=100000`、最大生成 `n_predict=50000`、think 模式 `DS4_THINK_HIGH`、采样用 `DS4_DEFAULT_*` 常量。

`DS4_THINK_HIGH`（思考高强度）作为默认 think 模式，就是为 DeepSeek V4 这类推理模型定的——普通通用 agent 不会默认开启重型思考。README 也把「Everything is tuned for this model」列为优势之一：

[README.md:520-522](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L520-L522) —— 官方列出原生 agent 的优势：低延迟、prefill 进度条、无 DSML 转换、KV mismatch 不可能、为该模型全面调优。

#### 4.2.4 代码实践

> **实践目标**：用源码佐证「垂直设计」的三个侧面。
>
> **操作步骤**：
> 1. 在 `ds4_agent.c` 读 `parse_options` 的默认值（约 512 行），记下默认模型路径、`ctx_size`、`think_mode`，说明它们为何是「为 DeepSeek V4 选」而非「通用安全值」。
> 2. 读 `worker_run_turn` 的工具循环注释（约 7675-7681 行），确认「无客户端/服务端协议」「transcript 是唯一真相」这两句。
> 3. 对照 u7-l2 讲的服务器端点层（要解析 OpenAI/Anthropic/Responses 四种 JSON 方言），点出 agent 省掉了哪一整层。
>
> **需要观察的现象**：agent 里没有任何「把 JSON 工具调用翻译成 DSML」或「把 DSML 翻译成 JSON」的代码——模型说什么格式，agent 就直接消费什么格式。
>
> **预期结果**：你能列出垂直设计的三条具体表现：① 默认值针对 DeepSeek V4；② 工具调用原生 DSML 无 JSON 方言层；③ 无 socket 边界带来低延迟。

#### 4.2.5 小练习与答案

**练习 1**：README 说 agent「No DSML tool calling conversion」。请结合 u7-l4 解释：服务器为什么**必须**做 DSML 转换，而 agent 为什么**可以不做**？

> **答案**：服务器对外暴露的是 OpenAI/Anthropic 风格的 JSON 工具协议，客户端用 JSON 回发工具调用历史，服务器必须把 JSON 翻译成模型能续上的 DSML token（这就是 u7-l4 的「精确回放/规范化」要解决的失配问题）。agent 没有外部协议——它直接在终端里和用户交互、直接消费模型原生 DSML 流，根本没有 JSON 这一层，自然无需转换。

### 4.3 会话持久化：/save /list /switch /strip

#### 4.3.1 概念说明

KV-as-session 的另一半是「会话能落盘、能恢复」。agent 的会话存在一个固定目录：

[ds4_agent.c:3609-3617](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3609-L3617) —— `agent_default_cache_dir`：默认 `$HOME/.ds4/kvcache`。

每个会话是一个 `.kv` 文件，文件名是会话身份的 SHA。会话命令族在 `runtime_help` 里列得很全：

[ds4_agent.c:9367-9385](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9367-L9385) —— `runtime_help`：`/save` `/compact` `/list` `/switch` `/del` `/strip` `/history` `/power` `/new` `/quit`。

理解会话持久化，要先理解一个反直觉的设计决定：**会话身份（文件名）与 transcript 解耦**。文件名不是 transcript 的哈希，而是「标题 + 创建时间」的哈希。这样对话越长、transcript 越变，文件名却保持稳定——多次 `/save` 同一个会话会覆写同一个文件，而不是产生一堆新文件：

[ds4_agent.c:3630-3643](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3630-L3643) —— `agent_session_identity_sha`：对 `title || 创建时间戳` 取 SHA1；注释明说「身份刻意独立于渲染 transcript，重存保持同名」。

标题来自第一条用户消息（`agent_session_title_from_prompt`），创建时间是会话首次保存的时刻。

#### 4.3.2 核心流程：保存 = 序列化活 KV

`/save` 的路径很短：UI 线程在 worker 空闲时直接调 `agent_worker_save_session`：

[ds4_agent.c:10024-10032](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10024-L10032) —— `/save` 命令分发：忙时改用 `worker_request_save`（延迟到下一个安全点），空闲时直接 `agent_worker_save_session`。

`agent_worker_save_session` 先确认 worker 空闲（否则拒绝，因为活 KV 正在被改），再调 `agent_worker_save_session_now` 计算身份、得到 `<sha>.kv` 路径，最终落盘：

[ds4_agent.c:4397-4407](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4397-L4407) —— `agent_worker_save_session`：`worker_is_idle` 守门，调 `agent_worker_save_session_now`。

真正的写盘在 `agent_kv_save_path`，它产出一个标准的 KVC 文件（u8-l1 讲过的格式）：固定头 + 渲染文本 + DS4 payload + 可选标题 trailer。核心步骤是先把活 KV 序列化成 staging 临时区域量出字节数，再原子地 `rename` 到目标路径：

[ds4_agent.c:3914-3974](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3914-L3974) —— `agent_kv_save_path` 的写盘：`ds4_session_stage_payload` 把活 KV 序列化到 staging，写 KVC 头/渲染文本/DS4 payload/标题 trailer，`mkstemp`+`rename` 原子落盘。

这里又一次用到 4.1.3 的不变量：函数开头先断言 `agent_tokens_equal(live, tokens)`——落盘的 KV 必然对应 transcript，绝不会存出「KV 与文本对不上」的坏文件。

#### 4.3.3 核心流程：切换 = 免 prefill 恢复 KV

`/switch <sha前缀>` 是 KV-as-session 最能体现价值的地方：恢复一个会话时，**直接把 KV payload 反序列化回活会话，跳过 prefill**。长会话的 prefill 本来要花几秒到几十秒，这里变成一次文件读：

[ds4_agent.c:10087-10107](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10087-L10107) —— `/switch` 命令分发：先用 `agent_maybe_save_before_leaving_session` 保当前会话，再 `agent_worker_switch_session`。

[ds4_agent.c:5364-5423](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5364-L5423) —— `agent_worker_switch_session`：`agent_kv_load_path` 加载，把 `loaded` tokens 赋给 `w->transcript`，恢复标题/创建时间/SHA，状态置空闲。注释点出 stripped 会话会触发重建。

加载的核心在 `agent_kv_load_path`，它做三重校验（model_id、quant_bits、身份 SHA）后，走两条路之一：

[ds4_agent.c:3771-3869](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3771-L3869) —— `agent_kv_load_path`：读 KVC 头/渲染文本/标题；校验 model_id 与 quant_bits 与当前引擎一致；`payload_bytes==0` 时走「重新分词+prefill」重建，否则 `ds4_session_load_payload` 直接反序列化 KV；末尾再校验加载后 token 数与头部一致。

两条路：

- **正常会话**（`payload_bytes != 0`）：`ds4_session_load_payload` 把逐层 KV 张量与 compressor frontier 直接灌回活会话——免 prefill。
- **stripped 会话**（`payload_bytes == 0`）：用 `ds4_tokenize_rendered_chat` 把保存的渲染文本重新分词，再 `agent_worker_sync_tokens` 做一次完整 prefill 重建。

注意 model_id 校验：换个模型族（Flash ↔ PRO）去加载旧会话会被拒，因为 KV 是按模型布局存的，跨模型不兼容。

#### 4.3.4 核心流程：/strip 与 /list

`/strip <sha前缀>` 是个「瘦身」操作：保留渲染文本与标题 trailer，删掉重的 KV payload（把头部 `payload_bytes` 写成 0）。这样 `.kv` 文件从可能几十上百 MB 缩到几 KB，代价是下次 `/switch` 要重 prefill：

[ds4_agent.c:10126-10146](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10126-L10146) —— `/strip` 命令分发。

[ds4_agent.c:5322-5336](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5322-L5336) —— `agent_worker_strip_session` 写新头时把 payload 字节（`fill_header` 最后一个参数）置 `0`，保留文本与标题。

`/list` 扫描缓存目录，**按 model_id 过滤**（只列当前模型族的会话），按最近更新时间排序，显示 SHA、标题、年龄、token 数、文件大小、是否 stripped：

[ds4_agent.c:5011-5070](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5011-L5070) —— `agent_worker_list_sessions`：`opendir` 扫描、`ds4_kvstore_sha_hex_name` 认 `.kv` 文件名、`e.model_id == model_id` 过滤、`qsort` 按 recency 排序、打印。

#### 4.3.5 系统提示检查点：sysprompt.kv

会话持久化里还有一个不起眼但重要的优化：**系统提示本身也被存成一个固定检查点 `sysprompt.kv`**。系统提示（含工具描述）很长，每个新会话都要 prefill 它一次很浪费。于是 agent 把它存成固定文件，新会话直接加载、跳过 prefill；只有当渲染出的系统提示文本变了（比如换了 `--system`）才重建：

[ds4_agent.c:4192-4252](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4192-L4252) —— `agent_worker_reset_to_sysprompt`：先尝试 `agent_kv_load_path` 加载 `sysprompt.kv`；命中就跳过 prefill；未命中才同步 prefill 并把结果存回 `sysprompt.kv`。注释指出该文件 Flash/PRO 共用，靠 model_id 校验区分。

`/new` 命令就是回到这个系统提示检查点、开一个全新空会话：

[ds4_agent.c:10077-10086](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10077-L10086) —— `/new` 命令分发：先保存当前会话，再 `agent_worker_reset_to_sysprompt`。

#### 4.3.6 代码实践

> **实践目标**：把 `/save` → `/list` → `/switch` → `/strip` → `/switch` 这条会话生命周期在源码里走一遍，理解每一步动了 `.kv` 文件的哪一段。
>
> **操作步骤**：
> 1. 在 `ds4_agent.c` 定位四个命令的 dispatch（`/save`≈10024、`/list`≈10037、`/switch`≈10087、`/strip`≈10126）。
> 2. 跟进 `agent_worker_save_session_now`（≈4341）→ `agent_kv_save_path`（≈3873），确认写出的文件结构是「固定头 + 渲染文本 + DS4 payload + 标题 trailer」，且身份 SHA 来自 `title+创建时间`（≈3633）而非 transcript。
> 3. 跟进 `agent_worker_switch_session`（≈5364）→ `agent_kv_load_path`（≈3771），确认正常会话走 `ds4_session_load_payload`（免 prefill），stripped 会话（`payload_bytes==0`）走重新分词 + prefill。
> 4. 跟进 `agent_worker_strip_session`（≈5249），确认它把 payload 写成 0、保留文本与标题。
>
> **需要观察的现象**：保存时身份独立于 transcript（文件名稳定）；加载时正常会话免 prefill、stripped 会话才 prefill；model_id 不匹配会被拒。
>
> **预期结果**：你能画出一张表：命令 → 触发函数 → 对 `.kv` 文件的操作（写/读 payload / 清零 payload）→ 是否需要 prefill。
>
> 待本地验证项：若本地有模型，可 `./ds4-agent` 跑两轮后 `/save`、`/list` 记下 SHA、`/strip <SHA>`、再 `/switch <SHA>`，观察终端打印 `rebuilt from text` 与 `rebuilt from rendered text...`（对应 5383 行），即验证 stripped→prefill 重建路径。

#### 4.3.7 小练习与答案

**练习 1**：为什么 agent 的会话文件名用「标题+创建时间」的 SHA，而不是 transcript 的 SHA？如果改用 transcript SHA 会出什么问题？

> **答案**：为了让同一个会话在多次 `/save` 时覆写同一个文件（对话越长 transcript 越变，但文件名稳定，便于 `/list` `/switch` 用稳定 ID 追踪）。若改用 transcript SHA，每追加一轮对话文件名就变，`/save` 会不断产生新文件而非更新，`/switch <旧SHA>` 也会指向过时快照——会话身份会被对话内容「冲掉」。

**练习 2**：`/strip` 把一个会话从 80 MB 缩到几十 KB。代价是什么？这个代价在什么场景下可以接受？

> **答案**：代价是下次 `/switch` 该会话时必须把保存的渲染文本重新分词、完整 prefill 一次来重建 KV（不能免 prefill 直接恢复）。可接受的场景：你想长期归档很多旧会话、磁盘吃紧，而这些会话短期内不会频繁打开——用 `/strip` 把它们压成纯文本存档，需要时再付一次 prefill 代价换回来。

**练习 3**：`agent_kv_load_path` 为什么要校验 `model_id` 与 `quant_bits`？跨模型加载一个旧 `.kv` 会怎样？

> **答案**：KV 缓存是按模型层/head 布局与量化档存的，跨模型族（Flash↔PRO）或跨量化档（2bit↔4bit）的 KV 在字节布局上不兼容，强行加载会得到错乱的注意力状态。所以加载时校验 `model_id`/`quant_bits` 与当前引擎一致，不一致就拒绝（报「written for a different model/quantization」），避免静默出错。

## 5. 综合实践

把本讲三个最小模块串成一个端到端的「会话生命周期追踪」任务。

**任务**：假设你要向一位新同事解释「为什么 `ds4-agent` 不需要 u7 服务器那套精确回放/规范化机制，却仍能做到会话断点续传」，请完成下面这份「证据链卡片」。

1. **架构对比图**：画两张极简方框图。
   - 服务器：`客户端 --HTTP/JSON--> ds4-server [engine + 单活 session]`，标注「重渲染 → 字节可能失配 → 需精确回放/规范化」。
   - agent：`终端 <-> ds4-agent [engine + 活 session + transcript]`，标注「进程内、token id 只追加、活 KV==transcript」。
2. **不变量证据**：引用 `worker_accept_generated_token`（7530-7533）与 `agent_kv_save_path`（3880-3884）两段代码，说明「双写 → 相等性断言恒成立」。
3. **持久化证据**：引用 `agent_session_identity_sha`（3633，身份独立于 transcript）、`agent_kv_save_path`（落盘结构）、`agent_kv_load_path`（3771，免 prefill 恢复 vs stripped 重建），说明会话如何落盘与恢复。
4. **一句话总结**：用一句话回答本讲的核心问题——「为什么 KV mismatch 在该架构下按构造不可能」。

**验收标准**：你的卡片里每条结论都能对应到本讲给出的具体源码行号；架构对比图能清楚点出 agent 砍掉了服务器的哪一整层（JSON 方言翻译 + 重渲染失配修复）。

待本地验证项：若有 GPU 机器，可实际跑 `./ds4-agent`，做 `/save`→`/list`→`/switch`→`/strip`→`/switch`，把终端输出贴进卡片作为运行证据。

## 6. 本讲小结

- `ds4-agent` 是**单进程双线程**：UI 线程管终端，worker 线程独占活 `ds4_session` 与 KV 状态，推理就在进程内，没有 socket/API 边界。
- **KV-as-session**：一条会话在物理上就是一个 `.kv` 文件（KVC 格式）；保存=序列化活 KV，恢复=反序列化回内存、跳过 prefill。
- **KV mismatch 按构造不可能**：每个推进 KV 的 token 都被 `worker_accept_generated_token` 同一行代码同步追加进 transcript，活 KV token 序列与 transcript 恒等；`agent_kv_save_path` 的相等性断言只是这道不变量的守门员。
- 前缀复用仍存在（`ds4_session_common_prefix` + `ds4_session_sync` 只 prefill 后缀），但因为只追加不改写，几乎总命中纯追加路径。
- **垂直设计**三表现：默认值针对 DeepSeek V4；工具调用原生 DSML、无 JSON 方言转换层；无 socket 边界带来低延迟。
- 会话命令族 `/save` `/list` `/switch` `/del` `/strip` `/new` 背后：会话身份=标题+创建时间的 SHA（稳定文件名）、`sysprompt.kv` 是系统提示固定检查点、stripped 会话删 payload 留文本、切换时按 model_id/quant_bits 校验。

## 7. 下一步学习建议

- **u10-l2 Agent 工具系统**：本讲刻意没展开内建工具（read/write/edit/bash/search 等）的 DSML 参数解析、执行与终端可视化。下一讲钻进工具分发与 `AGENT_TOOL_PARAM_*` 参数种类。
- **u10-l3 Agent web 搜索**：`ds4_web.c` 的 `google_search`/`visit_page` 与 `confirm/log/cancel` 回调如何在 agent 里集成。
- **回看 u7-l4 / u7-l5**：现在你理解了 agent 的「无失配」架构，再去读服务器的「精确 DSML 回放 + 规范化改写」会非常通透——那是为无状态 API 不得不补上的复杂度。
- **u8-l1 / u8-l3**：本讲的 `.kv` 文件格式与 DS4 payload 序列化细节在那两讲里有完整的字节级拆解，可作为本讲 4.3 节的深读材料。
