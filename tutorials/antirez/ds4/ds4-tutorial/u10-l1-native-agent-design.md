# 原生编码 Agent 设计

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ds4-agent` 与「LLM + 外部客户端通过 HTTP API 调用」这类常见 agent 架构的本质区别。
- 解释什么是「进程内推理（in-process inference）」和「KV 即会话（KV-as-session）」，以及它们为什么带来低延迟。
- 理解为什么在这种架构下 **KV mismatch 按构造不可能发生**——这正是本讲的核心论点。
- 看懂 `ds4-agent` 如何把系统提示与 DSML 工具「垂直化（vertically）」地为 DeepSeek V4 Flash/PRO 量身设计。
- 掌握会话持久化命令 `/save`、`/list`、`/switch`、`/strip`、`/del` 背后的磁盘 KV 缓存机制。

本讲属于 **advanced** 层，是「原生 Agent、评测与基准」单元（u10）的第一篇。它承接 u2-l3（Session 同步与前缀复用）与 u3-l3（分词器与聊天模板渲染），并作为 u10-l2（Agent 工具系统）与 u10-l3（Agent web 搜索）的基础。

## 2. 前置知识

在进入本讲前，请确保你理解以下几个概念（都在前置讲义中讲过）：

- **prefill 与 decode**：一次性把提示填进 KV 缓存叫 prefill，决定首 token 延迟；之后自回归逐 token 生成叫 decode，决定生成速度（见 u1-l5）。
- **`ds4_session` 与前缀复用**：session 是一条可变推理时间线，持有 KV 缓存；`ds4_session_sync` 会用 token 前缀匹配，只对新增后缀做增量 prefill（见 u2-l3）。
- **token 序列**：模型真正消费的单位是 token id 序列，不是文本。文本要经过分词器（BPE）变成 token（见 u3-l3）。
- **聊天模板渲染**：`ds4_chat_append_message`、`ds4_tokenize_rendered_chat` 等函数把系统/用户/助手段落按 DeepSeek 聊天格式拼成 token 序列（见 u3-l3）。
- **DSML**：DeepSeek 的工具调用文本格式，用全角竖线标记 `<｜DSML｜tool_calls>`，模型以 token 流直接生成（见 u7-l4）。

一个关键对比心智模型：在 **`ds4-server`**（见 u7 单元）里，客户端通过无状态 HTTP API 重发整段对话文本，服务器要把文本重新分词，再与前缀复用 KV。这个「重新分词」步骤可能和模型当初采样的字节对不上，于是出现了 u7-l4 / u7-l5 讲的那一整套「精确回放 + 规范化 + 字节比对」机制。

本讲要回答的核心问题是：**如果 agent 把推理引擎直接嵌在自己进程里，根本不经过文本往返，上面这套复杂的修补机制还需要吗？**

答案是：**完全不需要。这就是 `ds4-agent` 的全部设计起点。**

## 3. 本讲源码地图

本讲主要涉及两个文件：

| 文件 | 作用 |
|------|------|
| `ds4_agent.c` | agent 的全部实现，约 1 万行。包含配置解析、双线程模型、生成循环、DSML 流式解析、工具执行、会话持久化、TUI 渲染等 |
| `README.md` | `Native agent` 一节给出了 agent 的设计动机与命令说明，是理解意图的最佳入口 |

`ds4_agent.c` 是一个自包含前端二进制：它链接引擎核心（`ds4.o` 等 CORE_OBJS）和辅助 `.o`（见 u1-l4），自己拥有 `main`，进程内持有一个 `ds4_engine`（已加载模型）和一个 `ds4_session`（活 KV 时间线）。

本讲会重点精读以下函数（行号均为当前 HEAD）：

- `main`、`run_agent`、`worker_main`、`worker_submit`：进程结构与双线程。
- `worker_run_turn`（生成循环）、`agent_worker_sync_tokens`：进程内推理与前缀复用。
- `agent_kv_save_path`：**KV mismatch 不可能** 的关键断言所在。
- `agent_append_system_prompt`、`agent_build_tools_prompt`：垂直化系统提示。
- `agent_worker_save_session_now`、`agent_session_identity_sha`、`agent_worker_reset_to_sysprompt`、`agent_worker_strip_session`：会话持久化。

## 4. 核心概念与源码讲解

### 4.1 进程内推理与 KV-as-session

#### 4.1.1 概念说明

大多数 agent 系统（如基于 OpenAI API 的编码助手）是「客户端—服务端」结构：

```
┌──────────────┐   HTTP/JSON    ┌──────────────┐
│  agent client │ ────────────▶ │  LLM 服务端  │
│ （你的终端）  │ ◀──────────── │ （推理引擎）  │
└──────────────┘   文本流式回   └──────────────┘
```

客户端每次发请求时，要把**整段对话历史**重新发成文本；服务端是无状态的，靠它自己想办法复用 KV 缓存。问题就出在「文本往返」上：文本 → token 的分词过程，可能与上次推理时的 token 序列不一致，导致 KV 缓存前缀对不上（这就是 u7-l5 讲的 KV mismatch）。

`ds4-agent` 反其道而行，README 一句话点明设计：

> the inference is controlled from within the agent itself, without socket/API boundaries, so the session is represented by the on-disk KV cache itself.

参考 [README.md:509-516](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L509-L516)。

这就是两个核心概念：

- **进程内推理（in-process inference）**：推理引擎（`ds4_engine` + `ds4_session`）和 agent 的用户界面跑在**同一个进程**里，中间没有 socket、没有 JSON、没有文本序列化。agent 把用户输入分词成 token 后，**直接**喂给 session，模型吐出的 token 也**直接**追加到 session 与 transcript（会话文本/词表记录）。
- **KV 即会话（KV-as-session）**：会话状态不是某个外部数据库里存的「消息列表」，而是 **活 KV 缓存本身**。KV 缓存记录了到当前为止所有 token 的注意力状态，它就是会话进度的唯一真相（single source of truth）。持久化时，把 KV 缓存连同 token 序列一起写盘（见 4.3）。

这套设计带来的直接红利（README 原文）：

- **低延迟**：显示文本、工具调用、开新会话几乎瞬时，瓶颈只在 prefill 速度。
- **prefill 实时进度条**：因为推理就在本进程，UI 线程能边算边显示进度。
- **无 DSML 转换**：工具调用直接用模型原生的 DSML token 流处理（见 4.2）。
- **KV mismatch 按构造不可能**：当前活状态永远是真相。
- **`/switch` 恢复完整 KV 会话不需要重新 prefill**。

参考 [README.md:518-523](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L518-L523)。

#### 4.1.2 核心流程

`ds4-agent` 进程内有**两个线程**，这是理解一切行为的关键：

```
┌─────────────────── agent 进程 ──────────────────────┐
│                                                      │
│   UI 线程（主线程）            worker 线程（后台）    │
│   ───────────────────         ────────────────────   │
│   linenoise 行编辑              拥有 session（活 KV）│
│   读用户输入                    拥有 transcript       │
│   渲染流式输出                  跑 prefill/decode    │
│   处理 /slash 命令              执行工具             │
│         │                            ▲               │
│         │ worker_submit(cmd)         │               │
│         └────── cmd_text ────────────┘               │
│                  （互斥锁+条件变量唤醒）             │
│         ◀────── out / status ────────                │
│           （worker 把生成字节写进缓冲）              │
└──────────────────────────────────────────────────────┘
```

1. **UI 线程**用 linenoise 读用户输入；普通文本被 `worker_submit` 交到 worker；斜杠命令（`/save` 等）则 UI 线程自己处理（必要时也委托 worker）。
2. **worker 线程**拿到 `cmd_text` 后进入一轮生成（`worker_run_turn`）：
   - 把用户文本用 `ds4_chat_append_message` 分词并追加到 `transcript`；
   - 调 `ds4_session_sync`，它内部用 `ds4_session_common_prefix` 找出 transcript 与活 KV 已对齐的前缀，**只对新增后缀增量 prefill**；
   - 进入 `while` 循环：采样一个 token → `ds4_session_eval` 吃进 → 渲染到屏幕 → 若遇到 DSML 工具调用则就地解析执行，把工具结果作为 `tool` 消息追加进 transcript，继续生成。
3. 生成出的 token 字节由 worker 写入 `w->out` 缓冲，UI 线程取出渲染到终端（带语法高亮、进度条）。

关键点：**transcript（token 序列）和活 KV 是同一个进程里、同一个线程里同步维护的两份状态**。每追加一个 token，两者同时前进，永远对齐——这就是「mismatch 按构造不可能」的根。

#### 4.1.3 源码精读

**(1) `agent_worker` 结构体：进程内的全部共享状态**

[ds4_agent.c:98-149](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L98-L149) 定义了 worker 的核心字段（节选关键部分）：

```c
typedef struct {
    ds4_engine *engine;          // 已加载模型，进程级、只读
    agent_config *cfg;
    ds4_session *session;        // 活 KV 时间线 —— 会话状态的真相
    ds4_tokens transcript;       // 当前 token 序列（与活 KV 对齐）
    char *cache_dir;             // ~/.ds4/kvcache，持久化目录
    char session_sha[41];        // 当前会话的稳定身份
    char *session_title;         // 首条用户提示派生的标题
    ...
    pthread_t thread;            // worker 线程
    pthread_mutex_t mu;
    pthread_cond_t cond;
    char *cmd_text;              // UI 线程→worker 的输入交接点
    char *out; size_t out_len;   // worker→UI 线程的输出缓冲
    agent_status status;
    ...
} agent_worker;
```

注意 `session` 与 `transcript` 并列存在：`session` 是图计算状态（KV 张量），`transcript` 是 token id 数组。两者由 worker 线程独占修改。

**(2) 双线程模型：UI 线程负责 IO，worker 线程独占推理**

文件顶部注释明确写出设计意图：

> The agent is intentionally a single process: the UI thread owns terminal input/output, while the worker thread owns the live DS4 session and KV state.

参考 [ds4_agent.c:44-47](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L44-L47)。

worker 线程的主循环 [ds4_agent.c:8068-8119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L8068-L8119)：它在条件变量上睡眠，等 UI 线程通过 `worker_submit` 投递 `cmd_text`，醒来后调用 `worker_run_turn(w, cmd)` 跑一整轮生成：

```c
while (true) {
    pthread_mutex_lock(&w->mu);
    while (!w->stop && !w->cmd_text && !w->save_requested &&
           !w->compact_requested && !w->power_requested)
        pthread_cond_wait(&w->cond, &w->mu);     // 等待被唤醒
    ...
    char *cmd = w->cmd_text;
    w->cmd_text = NULL;
    pthread_mutex_unlock(&w->mu);
    worker_run_turn(w, cmd);                      // 一轮推理 + 工具循环
    ...
}
```

UI 线程投递输入的入口 `worker_submit` [ds4_agent.c:8163-8184](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L8163-L8184)：它在锁内把文本存进 `cmd_text` 并 `pthread_cond_signal` 唤醒 worker。

**(3) 一轮生成的核心：transcript 即真相，只增量 prefill 后缀**

`worker_run_turn` 内层循环的开头 [ds4_agent.c:7698-7729](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7698-L7729) 是「进程内推理 + 前缀复用」的精华：

```c
const ds4_tokens *prompt_for_sync = &w->transcript;
int old_pos = ds4_session_pos(w->session);
int common = ds4_session_common_prefix(w->session, &w->transcript);
int cached = common == old_pos && w->transcript.len >= old_pos ? common : 0;
int suffix = prompt_for_sync->len - cached;
...
int sync_rc = ds4_session_sync(w->session, prompt_for_sync, err, sizeof(err));
```

这段逻辑直接复用了 u2-l3 讲的 session 同步原语：`common_prefix` 算出活 KV 已经覆盖到第几个 token，剩下的 `suffix` 才需要 prefill。因为 transcript 是 worker 自己一手维护的、与活 KV 严格对齐，所以这里 **`cached` 几乎总是等于上一轮的 `old_pos`，`suffix` 就是本轮新增的那一小段**——这就是低延迟的来源：用户发一句话，agent 只 prefill 这一句话的 token，而不是整段历史。

随后是采样循环 [ds4_agent.c:7782-7789](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7782-L7789)（伪代码）：

```c
while (generated < max_tokens && !worker_should_interrupt(w)) {
    int token = worker_sample_with_mode(w, cfg, greedy_sampling, &rng);
    // 把 token 喂回 session，KV 前进一格
    // 同时把 token 的文本写进 w->out 缓冲供 UI 渲染
    // 若 token 流构成 DSML 工具调用，就地解析并执行
}
```

注意「采样 → eval」都在 worker 线程内、对同一个 `session` 操作，token 一边被喂进 KV、一边被追加进 transcript，两者原子同步。

**(4) KV mismatch 按构造不可能：保存时的断言**

本讲的核心论点落在一个 `assert` 风格的检查上。`agent_kv_save_path`（把会话写盘）在写任何字节之前，先做这一步 [ds4_agent.c:3880-3884](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3880-L3884)：

```c
const ds4_tokens *live = ds4_session_tokens(w->session);
if (!agent_tokens_equal(live, tokens)) {
    snprintf(err, err_len, "live KV state does not match session transcript");
    return false;
}
```

这段代码读起来像一个普通的校验，但它表达的是整个架构的不变量（invariant）：

- `live` 是**活 KV 当前对应的 token 序列**（直接从 session 里取，`ds4_session_tokens`）。
- `tokens` 是 **transcript**（要保存的 token 序列）。
- 在 agent 架构里，这两个东西**永远应当逐 token 相等**，因为它们由同一个 worker 线程、在每一步生成里同时推进。

对比 `ds4-server`：服务端要面对「客户端用 JSON 文本重发历史」的场景，活 KV 的 token 序列与「重新分词得到的 token 序列」**天然可能不等**，所以才需要 u7-l5 那套逐级回退、字节比对、规范化改写。而 agent 因为没有文本往返这一步，`agent_tokens_equal` 这个检查**正常情况下恒为真**；它存在的意义不是「经常拦住错误」，而是**把架构不变量写成代码**——一旦它为假，意味着出了 bug（比如有人错误地改了 transcript），保存会立即失败而不是写出坏数据。这就是「按构造不可能（impossible by construction）」的精确含义：**架构本身保证它为真，代码用断言把这个保证钉死**。

README 把这点凝练成一句 [README.md:521](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L521)：

> KV cache mismatch are impossible by construction, the current state is always the truth.

#### 4.1.4 代码实践

**实践目标**：通过阅读 `main` → `run_agent` → `worker_main` → `worker_run_turn` 的调用链，亲眼确认「进程内推理」与「transcript/活 KV 同步前进」，并用自己的话解释为什么 KV mismatch 在此架构下不可能。

**操作步骤（源码阅读型实践）**：

1. 打开 [ds4_agent.c:10215](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10215)（`main`）。注意它只做三件事：解析配置 → `ds4_engine_open` 打开模型 → 调 `run_agent`。确认**整个进程只持有一个 engine**，没有 socket、没有 HTTP。
2. 跳到 [ds4_agent.c:9792](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9792)（`run_agent`）。`agent_worker_init` 会启动 worker 线程；之后主线程进入 linenoise 输入循环。
3. 找到用户输入分发的位置 [ds4_agent.c:10164](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10164)：普通文本走 `worker_submit(&worker, cmd)`，斜杠命令走各自的分支。
4. 读 worker 主循环 [ds4_agent.c:8086-8119](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L8086-L8119)，确认它从 `cmd_text` 取出输入后调 `worker_run_turn`。
5. 在 `worker_run_turn` 里定位前缀复用与采样循环 [ds4_agent.c:7698-7789](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7698-L7789)。
6. 最后读保存断言 [ds4_agent.c:3880-3884](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3880-L3884)。

**需要观察的现象（在你的笔记里写下）**：

- `ds4_session_sync` 拿到的 `prompt_for_sync` 是 `&w->transcript`，而不是任何「重新渲染的文本」。整个生成过程**没有任何一处把 token 序列重新解码成文本再分词**。
- 采样循环里，每生成一个 token，这个 token 既被 `ds4_session_eval` 喂进 KV、又（在循环体内）被追加进 transcript。两个状态在同一个临界区内、由同一个线程推进。
- 因此当 `agent_kv_save_path` 比较 `live` 与 `tokens` 时，二者必然来自同一段连续的、同步推进的历史。

**预期结果**：你能用一句话写出本讲练习任务要求的答案，例如：

> 在 `ds4-agent` 中，活 KV 的 token 序列与 transcript 由同一个 worker 线程在每一步生成里同步追加，二者之间没有任何文本往返或重新分词；保存时的 `agent_tokens_equal(live, tokens)` 检查在正常路径上恒为真，它把「活状态即真相」这一架构不变量钉死成代码。而 `ds4-server` 必须把客户端重发的 JSON 文本重新分词，重新分词结果可能与活 KV 不一致——正是这一步文本往返在 agent 里被消灭了，所以 mismatch 按构造不可能。

如果你没有可运行的硬件（agent 需要 GPU 与大模型），本实践为纯源码阅读型，**待本地验证**指的是「实际跑 agent 观察首 token 延迟」那一步——阅读部分本身可在任何环境完成。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `worker_run_turn` 里的 `ds4_session_common_prefix` 换成「每次都返回 0」（强制整段重建 prefill），功能上还能跑吗？会损失什么？

> **答案**：功能上仍能跑（`ds4_session_sync` 在前缀不匹配时会整段重建，见 u2-l3），但每轮对话都要对整段历史重新 prefill，首 token 延迟随对话变长而线性恶化，丧失了「进程内推理低延迟」这一核心优势。前缀复用正是低延迟的来源。

**练习 2**：为什么 agent 用两个线程而不是一个？把推理也放进主线程会怎样？

> **答案**：因为推理（尤其 prefill）耗时，且需要边算边把 token 流式渲染到屏幕、还要响应 Ctrl+C 协作式中断。若用单线程，prefill 期间既无法刷新终端、也无法读输入；双线程让 UI 线程始终能渲染与响应，worker 线程独占 KV 做长计算。注意「session 无锁」是可行的，正因为它只在 worker 线程被读写（见 u7-l1 的同类设计哲学）。

---

### 4.2 垂直设计：系统提示与原生 DSML 工具

#### 4.2.1 概念说明

README 用一个词概括 agent 的系统提示与工具设计：**vertically designed**——垂直化、专门化、为一个模型量身定制（[README.md:514-515](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L514-L515)）。

「垂直」对应「水平（通用）」：一个通用 agent 客户端要兼容 OpenAI、Anthropic 等多家协议，系统提示和工具描述得写得四平八稳、抽象；而 `ds4-agent` 只服务 DeepSeek V4 一个模型，于是它可以：

- 直接用模型的**原生 DSML 工具格式**做工具调用，**不做任何 JSON ↔ DSML 转换**（[README.md:520](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L520)）。这一点极其关键：在 `ds4-server` 里，要把客户端发来的 OpenAI 风格 `tools` JSON 翻译成模型能懂的 DSML 文本、再把模型生成的 DSML 翻译回 JSON（见 u7-l4）；agent 则完全省掉这个翻译层。
- 把系统提示写得**完全贴合**这个模型的分词习惯（例如示例里的全角竖线 `｜DSML｜` 要被分词成模型的专用 DSML 控制符）。
- 把工具行为（`read`、`edit`、`bash` 等）的细节参数（如 `read` 默认只读 500 行、`edit` 用 `[upto]` 锚点）直接焊进提示，针对编码任务反复打磨。

#### 4.2.2 核心流程

系统提示的构建链：

```
agent_worker_build_system_tokens
   ├── ds4_chat_begin                       // 起 BOS / 聊天头
   ├── ds4_chat_append_max_effort_prefix    // 仅 Think Max 模式
   └── agent_append_system_prompt
          ├── agent_build_tools_prompt      // 拼接三段常量字符串
          │     ├── agent_tools_prompt_intro
          │     ├── agent_tools_prompt_edit_line
          │     └── agent_tools_prompt_after_edit
          └── ds4_tokenize_rendered_chat     // 关键：按「渲染回扫」分词
```

最微妙的一步是 `ds4_tokenize_rendered_chat`：系统提示文本里包含 DSML 示例（如 `<｜DSML｜tool_calls>`），这些字面量的全角竖线必须被分词器识别成模型的**专用 DSML 控制符**，而不是几个普通字符。这正是 u3-l3 讲的「渲染回扫（rendered chat rescan）」路径——把已经渲染好的聊天文本逐字符扫一遍，遇到特殊标记就原子地切成对应 token。

生成时，工具调用的处理流程：

```
模型吐出 token 流
   └── agent_dsml_parser 流式解析
          ├── 普通 token → 渲染成正文
          └── 识别到 <｜DSML｜tool_calls> → 进入工具调用解析
                 ├── 解析 invoke name / parameter
                 ├── 解析完成后调用对应工具执行函数（read/write/edit/bash/...）
                 └── 把工具结果作为 tool 消息追加进 transcript，模型继续生成
```

注意：整个过程**没有任何 JSON**。工具名、参数名、参数值都是模型直接生成的 DSML 文本，agent 直接解析、直接执行。这就是「无 DSML 转换」的字面含义。

#### 4.2.3 源码精读

**(1) 工具提示三段拼接**

`agent_build_tools_prompt` 把三段 C 字符串常量拼成完整工具提示 [ds4_agent.c:948-958](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L948-L958)：

```c
static char *agent_build_tools_prompt(void) {
    const char *edit = agent_tools_prompt_edit_line;
    size_t a = strlen(agent_tools_prompt_intro);
    size_t b = strlen(edit);
    size_t c = strlen(agent_tools_prompt_after_edit);
    char *out = xmalloc(a + b + c + 1);
    memcpy(out, agent_tools_prompt_intro, a);
    memcpy(out + a, edit, b);
    memcpy(out + a + b, agent_tools_prompt_after_edit, c + 1);
    return out;
}
```

引言段 `agent_tools_prompt_intro` 直接「教」模型写 DSML [ds4_agent.c:711-717](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L711-L717)（节选）：

```
You have access to native DSML tools. Invoke tools by writing exactly this shape:

<｜DSML｜tool_calls>
<｜DSML｜invoke name="$TOOL_NAME">
<｜DSML｜parameter name="$PARAMETER_NAME" string="true|false">$PARAMETER_VALUE</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>
```

这里直接把 DSML 文法作为示例喂给模型——这是「垂直化」最直观的体现：不是抽象描述工具协议，而是给出这个模型能逐字吐出的精确格式。

**(2) 关键的分词路径选择**

`agent_append_system_prompt` 的注释把这个微妙点讲得很透 [ds4_agent.c:984-993](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L984-L993)：

```c
/* The built-in tool prompt is trusted DS4 control text.  Tokenize it like a
 * rendered chat prompt so the literal ｜DSML｜ markers in the examples become
 * the model's dedicated DSML token.  Do not apply that tokenizer to user
 * supplied -sys text: arbitrary user text containing <｜User｜>, <think>, or
 * ｜DSML｜ must remain plain content, not control tokens. */
static void agent_append_system_prompt(ds4_engine *engine, ds4_tokens *tokens,
                                       const char *extra) {
    char *tools_prompt = agent_build_tools_prompt();
    ds4_tokenize_rendered_chat(engine, tools_prompt, tokens);   // 走渲染回扫
    ...
}
```

这是 u3-l3「渲染回扫」的真实应用：**内置工具提示**是可信的 DS4 控制文本，所以用 `ds4_tokenize_rendered_chat`，让示例里的 `｜DSML｜` 变成模型的专用 DSML token；而**用户用 `-sys` 传进来的额外文本**则不能这么处理（否则用户文本里恰好出现的 `<｜User｜>` 会被误当控制符）。同一个分词器，对两种来源的文本采用两种策略——这是「垂直化」必须的精细控制。

**(3) 工具调用在生成流里就地解析**

在 `worker_run_turn` 的采样循环里，每来一个 token，`agent_dsml_parser` 就喂一个字节流式解析 [ds4_agent.c:7761-7766](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7761-L7766)：

```c
agent_dsml_parser dsml = {.state = AGENT_DSML_SEARCH};
agent_stream_renderer stream = {
    .renderer = &renderer,
    .parser = &dsml,
    ...
};
```

模型吐出的字节一边被渲染到屏幕、一边被 DSML 解析器消费；一旦一个完整的 `<｜DSML｜tool_calls>...</｜DSML｜tool_calls>` 段闭合，agent 就执行对应工具，把结果作为 `tool` 消息追加进 transcript，然后让模型继续生成。整条路径里没有 JSON 序列化、没有协议翻译。（具体每个工具的参数与执行函数，本讲不展开，留给 u10-l2。）

#### 4.2.4 代码实践

**实践目标**：体会「垂直化」与「无 DSML 转换」如何体现在系统提示的字节里。

**操作步骤（源码阅读型实践）**：

1. 读工具提示引言 [ds4_agent.c:707-725](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L707-L725)，注意它把 `read` 默认只读 500 行、`whole=true`、`continue_offset` 这些**具体到参数级别**的行为写进提示——这是通用 agent 不会做的精细度。
2. 读 `agent_append_system_prompt` 的注释与实现 [ds4_agent.c:984-993](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L984-L993)，对照 u3-l3 的「渲染回扫」概念，解释为什么内置提示用 `ds4_tokenize_rendered_chat` 而 `-sys` 用户文本不用。
3. 对比 `ds4-server`：回忆 u7-l4 里服务器要做 DSML ↔ JSON 的双向翻译。在本讲源码里**搜索 `json`**（`ds4_agent.c` 里工具调用路径几乎不出现 JSON），确认 agent 这条路径是纯 DSML 的。

**需要观察的现象**：

- 内置工具提示里 DSML 示例的全角竖线，经 `ds4_tokenize_rendered_chat` 后会成为模型专用 DSML token；这与模型在训练时学到的 DSML 完全对齐。
- 工具调用的解析、执行、结果回填，全部在 token/字节层面完成，没有「先把工具调用转成 JSON 对象，再转回文本」的往返。

**预期结果**：你能写出一句话，对比 agent 与 server 在工具调用上的处理差异：**server 要做 DSML↔JSON 翻译并保证重发时 KV 前缀对齐；agent 直接吃模型原生 DSML token 流，无翻译、无前缀失配风险**。

（本实践为源码阅读型，可在任何环境完成；若要实跑，需 GPU + 模型，待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：为什么系统提示里的 DSML 示例必须用全角竖线 `｜DSML｜` 而不是半角 `|DSML|`？

> **答案**：全角竖线是模型词表里 DSML 控制符的真实组成字节；`ds4_tokenize_rendered_chat` 的渲染回扫会把它原子识别成专用 token（见 u3-l3）。若用半角竖线，分词器认不出这是控制符，会把示例拆成普通字符 token，模型也就学不到「该用 DSML 格式」这个意图。这正体现了「垂直化」：连示例的字节都必须贴合目标模型词表。

**练习 2**：`-sys` 用户传入的文本为什么不能用 `ds4_tokenize_rendered_chat`？

> **答案**：因为用户文本是「内容」而非「控制指令」。若用渲染回扫，用户文本里恰好出现的 `<｜User｜>`、`<think>`、`｜DSML｜` 会被误切成控制 token，可能让模型把用户内容当成对话结构或工具调用，产生注入式歧义。所以用户文本走普通分词，保持纯内容语义。

---

### 4.3 会话持久化：/save /list /switch /strip

#### 4.3.1 概念说明

「KV 即会话」还有一个推论：**持久化一个会话 = 把 KV 缓存连同 token 序列一起写盘**。agent 把会话存成磁盘上的 `.kv` 文件（文件格式见 u8-l1 / u8-l3，本讲只讲 agent 特有的策略）。

README 列出了命令（[README.md:525-531](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L525-L531)）：

- `/save`：把当前会话存盘。
- `/list`：按最近更新时间列出已存会话。
- `/switch <sha>`：恢复某个会话——**因为是完整的 KV 检查点，恢复后无需重新 prefill**（[README.md:523](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L523)）。
- `/del <sha>`：删除某个会话。
- `/strip <sha>`：保留渲染文本与标题，但删掉沉重的 KV payload；以后 `/switch` 到被 strip 的会话时，靠对保存的文本重新 prefill 来重建 KV。

会话存放在 `~/.ds4/kvcache`（[README.md:525](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L525)）。

agent 的持久化策略与 `ds4-server` **故意不同**（见 [ds4_agent.c:3675-3687](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3675-L3687) 的注释）：

- **会话是显式保存（explicit saves only）**：不像 server 那样在 cold/continued/evict/shutdown 多个时机自动落盘（见 u8-l2），agent 只在用户敲 `/save`（或退出、切换前）时保存。这符合交互式 agent 的语义——用户掌控会话存档。
- **会话身份独立于渲染文本**：文件名是 `SHA1(title || created_at_le64)`，一旦会话有了标题和创建时间，**反复保存都写同一个文件名**，而 transcript/KV 内容可以一直变（[ds4_agent.c:3630-3632](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3630-L3632)）。对比 server：server 的文件名是渲染文本的 SHA1，文本一变文件名就变。

#### 4.3.2 核心流程

**会话身份（identity）的生成**：

```
首条用户提示  ──►  title（截断后的首句）
                    │
创建时间 created_at ─┤
                    ▼
        SHA1(title || created_at_le64)   ──►  <40hex>.kv   （稳定文件名）
```

**保存流程（`/save`）**：

```
agent_worker_save_session_now
   ├── agent_worker_sync_tokens        // 先确保活 KV 与 transcript 对齐
   ├── ds4_kvstore_render_tokens_text  // 渲染出文本（用于列表/历史/strip 重建）
   ├── agent_session_identity_sha      // 算稳定文件名
   └── agent_kv_save_path
          ├── assert: live KV == transcript   // 4.1 讲的不变量断言
          ├── ds4_session_stage_payload       // 把 KV 序列化成 DSV4 payload
          └── 写 KVC 文件：头 + 渲染文本 + payload + 标题 trailer
```

**加载/切换流程（`/switch`）**：

```
agent_worker_switch_session
   └── agent_kv_load_path
          ├── 读 KVC 头 + 渲染文本
          ├── 校验 model_id（Flash/Pro 不串台）
          ├── 校验渲染文本前缀匹配
          └── ds4_session_load_payload   // 直接恢复 KV，无需 prefill！
```

**strip 流程（`/strip`）**：

```
agent_worker_strip_session
   ├── 读出头 + 渲染文本 + 标题
   ├── 校验身份 SHA
   └── 重写文件：保留头/文本/标题，丢弃 payload（KV 字节）
```

被 strip 的会话再 `/switch` 时，因为没有 payload，会走「对保存的渲染文本重新 prefill」的路径重建 KV。

另外还有一个**启动优化**：`sysprompt.kv`——agent 把固定的系统/工具提示 KV 检查点存成一个固定名字的文件，下次启动若渲染文本仍匹配就直接 load，**省掉每次启动都要 prefill 一大段系统提示的开销**（[ds4_agent.c:4187-4191](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4187-L4191)）。文本变了就重建并覆盖。

#### 4.3.3 源码精读

**(1) 稳定会话身份**

会话身份刻意独立于渲染文本，注释 [ds4_agent.c:3630-3643](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3630-L3643)：

```c
/* Agent session IDs are intentionally independent from the rendered transcript:
 * once a session has a title and creation time, resaving it keeps the same file
 * name while the transcript and KV payload evolve. */
static void agent_session_identity_sha(const char *title, uint64_t created_at,
                                       char sha_out[41]) {
    size_t title_len = title ? strlen(title) : 0;
    agent_buf b = {0};
    agent_buf_append(&b, title ? title : "", title_len);
    uint8_t ts[8];
    agent_le_put64(ts, created_at);
    agent_buf_append(&b, (const char *)ts, sizeof(ts));
    ds4_kvstore_sha1_bytes_hex(b.ptr ? b.ptr : "", b.len, sha_out);
    free(b.ptr);
}
```

`title` 是首条用户提示派生的标题，`created_at` 是会话创建时间；二者都不随对话推进而变，所以文件名稳定。`created_at` 在反复保存中**被保留**（见 `agent_kv_save_path` [ds4_agent.c:3905-3906](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3905-L3906)），保证身份不漂移。

**(2) 保存 = 写一份完整 KV 检查点**

`agent_worker_save_session_now` [ds4_agent.c:4341-4395](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4341-L4395)（节选）：

```c
static bool agent_worker_save_session_now(agent_worker *w, char sha_out[41],
                                          int *tokens_out,
                                          char *err, size_t err_len) {
    if (!agent_worker_has_user_session(w)) { snprintf(err, err_len, "nothing to save"); return false; }
    if (agent_worker_sync_tokens(w, &w->transcript, false, err, err_len) != 0) return false;
    ...
    char sha[41];
    agent_session_identity_sha(w->session_title, w->session_created_at, sha);
    char *path = agent_kv_path_for_sha(w->cache_dir, sha);
    bool ok = agent_kv_save_path(w, path, &w->transcript, "agent-session",
                                 sha_out, w->session_title, w->session_created_at,
                                 err, err_len);
    if (ok) { memcpy(w->session_sha, sha, sizeof(w->session_sha)); ... }
    ...
}
```

注意三件事：

- 保存前先 `agent_worker_sync_tokens` 把活 KV 推进到与 transcript 完全对齐（保证 4.1 的不变量）。
- 路径由稳定身份决定，所以**同名文件被覆盖更新**，而不会因为对话变长就生成一堆新文件。
- 调用方要求模型必须 idle（`/save` 在 busy 时会被推迟到下一个安全点，见 `worker_request_save` + `worker_run_deferred_save`）。

**(3) 启动时复用系统提示 KV**

`agent_worker_reset_to_sysprompt` [ds4_agent.c:4192-4230](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L4192-L4230)：先渲染当前系统提示文本，尝试从固定路径 `sysprompt.kv` load；若文本仍匹配且 model_id 对得上就命中、跳过 prefill；否则重建并覆盖。这把「每次启动都要 prefill 一长串工具提示」的开销摊到了首次。

**(4) slash 命令分发**

UI 线程在 linenoise 循环里用字符串比较分发斜杠命令 [ds4_agent.c:10024-10147](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L10024-L10147)（节选）：

```c
} else if (!strcmp(cmd, "/save")) {
    if (busy) {
        worker_request_save(&worker);
        printf("save scheduled at next safe point\n");
    } else {
        char err[160] = {0};
        if (!agent_worker_save_session(&worker, err, sizeof(err)))
            printf("save failed: %s\n", err);
    }
} else if (!strcmp(cmd, "/list")) {
    agent_worker_list_sessions(&worker);
} ...
} else if (!strncmp(cmd, "/switch", 7) && ...) {
    ... agent_worker_switch_session(&worker, sha, AGENT_HISTORY_DEFAULT_TURNS, err, sizeof(err)) ...
} ...
} else if (!strncmp(cmd, "/strip", 6) && ...) {
    ... agent_worker_strip_session(&worker, sha_arg, sha, &tokens, err, sizeof(err)) ...
}
```

可识别的命令清单在 [ds4_agent.c:441-453](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L441-L453)，帮助文本在 [ds4_agent.c:9370-9376](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9370-L9376)。注意：**斜杠命令若在模型 busy 时下发，大多会被要求 idle**（`/save` 是例外，它会被推迟到安全点），因为它们要操作 session 状态，而 session 归 worker 独占。

**(5) strip：删 payload 留文本**

`agent_worker_strip_session` [ds4_agent.c:5249-5296](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5249-L5296)：读出头+渲染文本+标题，校验身份 SHA，然后重写文件——只保留头/文本/标题，丢弃沉重的 KV payload 字节。被 strip 的会话以后 `/switch` 时会因没有 payload 而走「对保存文本重新 prefill」的重建路径。这是「空间换时间」的旋钮：磁盘紧张时可以 strip 掉不常用会话的 KV，只留对话文本。

#### 4.3.4 代码实践

**实践目标**：理解 agent 会话文件的命名、内容与命令语义，能预测 `/save` 后再 `/switch` 回来时会发生什么。

**操作步骤（源码阅读型 + 可选运行）**：

1. 读 `agent_default_cache_dir` [ds4_agent.c:3609-3617](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3609-L3617) 与 `agent_kv_path_for_sha` [ds4_agent.c:3619-3624](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3619-L3624)，确认会话文件落在 `~/.ds4/kvcache/<40hex>.kv`。
2. 读身份函数 [ds4_agent.c:3633-3643](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3633-L3643)，弄清文件名由 `title + created_at` 决定、与对话内容无关。
3. 读 `agent_kv_save_path` 的开头 [ds4_agent.c:3873-3912](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L3873-L3912)：注意 session_identity 为真时用 `agent_session_identity_sha`，否则（如 sysprompt.kv）用渲染文本 SHA。这解释了「会话文件」与「sysprompt 引导文件」命名规则的区别。
4. （若有硬件）实跑：`./ds4-agent`，对话几轮，`/save`，然后 `ls ~/.ds4/kvcache/`，观察出现一个 `<40hex>.kv` 文件；再 `/list`、`/switch <sha前缀>`，观察恢复是瞬时（无 prefill 进度条）的。

**需要观察的现象**：

- 多次 `/save` 同一会话，`~/.ds4/kvcache` 里**文件数量不增加**（同名覆盖），但文件大小随对话变长而增长（payload 变大）。
- `/strip` 某会话后文件显著变小（payload 没了），再 `/switch` 它时会看到 prefill 进度条（重建 KV）。
- `/switch` 一个未 strip 的会话是瞬时的（直接 load payload，无 prefill）——这是「KV 即会话」最直观的体验。

**预期结果**：你能画出一张会话文件结构图：`KVC 头 + 渲染文本 + DSV4 payload（可被 strip 删除）+ 标题 trailer`，并解释每一部分分别服务于哪个命令（列表/历史用文本、恢复用 payload、命名用身份）。

（运行部分需 GPU + 模型，待本地验证；源码阅读部分任意环境可做。）

#### 4.3.5 小练习与答案

**练习 1**：为什么 agent 的会话文件名用 `SHA1(title||created_at)` 而不是像 server 那样用 `SHA1(渲染文本)`？

> **答案**：因为 agent 的会话是「同一个会话反复演进、显式覆盖保存」。若用渲染文本 SHA，对话每变一次文件名就变，会堆积一堆文件、`/save` 也无法更新旧档案。用 `title+created_at` 则身份稳定，反复保存覆盖同一文件，符合交互式 agent 的语义。server 则是无状态多客户端，每个不同对话天然是不同文本，用文本 SHA 正好作查找键。

**练习 2**：`/strip` 一个会话后，它的「对话内容」丢失了吗？

> **答案**：没有。strip 只删掉沉重的 KV payload 字节，**保留渲染文本与标题**。所以 `/list` 仍能看到它、`/history` 仍能渲染它的对话；只是 `/switch` 它时因为没有 payload，必须对保存的文本重新 prefill 来重建 KV。strip 是「牺牲恢复速度换磁盘空间」。

**练习 3**：`sysprompt.kv` 和普通会话 `.kv` 在命名与策略上有何不同？

> **答案**：`sysprompt.kv` 文件名固定（不是 SHA），内容是系统/工具提示的 KV 检查点，启动时若渲染文本匹配就 load、否则重建覆盖——它是启动加速用的引导缓存。普通会话用稳定身份 SHA 命名、显式 `/save` 才落盘、payload 随对话增长。两者都用同一套 KVC 文件格式与 payload 序列化，但策略层完全不同。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「架构对比」小任务。

**任务**：写一份不超过一页的对比表，对比 `ds4-agent`（进程内、KV-as-session）与 `ds4-server`（无状态 HTTP、文本往返）在以下维度上的差异，并标注每个结论对应的源码行号或讲义章节：

| 维度 | ds4-agent | ds4-server |
|------|-----------|------------|
| 推理发生处 | 进程内（`worker_run_turn` 直接调 `ds4_session_*`） | 独立 graph worker，HTTP 触发（u7-l1） |
| 会话状态真相 | 活 KV 本身 | 单活 KV checkpoint + 多级回退（u7-l5） |
| 客户端如何续接对话 | 不存在「客户端重发」，transcript 即真相 | 重发整段对话文本，服务器重新分词（u7-l2） |
| 工具调用格式 | 模型原生 DSML，无翻译 | DSML ↔ OpenAI/Anthropic JSON 翻译（u7-l4） |
| KV mismatch 可能性 | 按构造不可能（`agent_kv_save_path` 断言） | 可能，需精确回放/规范化补救（u7-l4/u7-l5） |
| 持久化时机 | 显式 `/save` | cold/continued/evict/shutdown 自动（u8-l2） |
| 文件命名 | `SHA1(title‖created_at)` 稳定 | `SHA1(渲染文本)` 随文本变（u8-l1） |

**操作步骤**：

1. 自己先填表，**不要**先看答案。
2. 填完后，逐行回到本讲与 u7、u8 讲义核对，修正记错的行号。
3. 最后，用一段话回答本讲练习任务的核心问题（为什么 KV mismatch 在 agent 架构下按构造不可能），要求同时提到：①同一个 worker 线程同步推进 transcript 与活 KV；②没有文本往返/重新分词；③`agent_kv_save_path` 的断言把这个不变量钉成代码。

**预期结果**：你能清晰地说出，agent 之所以能省掉 server 那一整套前缀复用补救机制，**不是因为它实现了更聪明的算法，而是因为它从架构上消灭了「文本往返」这个 mismatch 的唯一来源**。这是一个「架构选择消解了整个问题类别」的经典案例。

## 6. 本讲小结

- `ds4-agent` 把推理引擎嵌在**同一进程**内，UI 线程管终端 IO、worker 线程独占活 KV 与 transcript，中间没有 socket/JSON/文本往返。
- **KV 即会话**：活 KV 缓存本身就是会话状态的唯一真相；transcript（token 序列）与活 KV 由同一个 worker 线程在每一步生成里同步追加。
- **KV mismatch 按构造不可能**：因为没有「重新分词」这一步，`agent_kv_save_path` 里 `agent_tokens_equal(live, tokens)` 的断言在正常路径上恒为真——架构保证它为真，代码用断言把这个保证钉死。这对比 `ds4-server` 因文本往返而必须的一整套补救机制。
- 系统提示与 DSML 工具是**垂直化**为 DeepSeek V4 量身设计的：直接用模型原生 DSML token 流，无 JSON 翻译；内置工具提示经「渲染回扫」分词，让 DSML 示例成为模型专用控制符。
- 会话持久化用 `/save /list /switch /strip /del`：会话身份 `SHA1(title‖created_at)` 稳定（反复保存覆盖同一文件），`/switch` 直接 load 完整 KV 无需 prefill，`/strip` 删 payload 留文本、靠重 prefill 重建。
- agent 的持久化策略与 server **故意不同**：显式保存、稳定身份命名，对应交互式 agent 的语义。

## 7. 下一步学习建议

- **u10-l2（Agent 工具系统）**：本讲只点到 DSML 工具就地解析为止，下一讲会逐一拆解 `read`/`write`/`edit`/`bash`/`search` 等内建工具的参数种类（如 `AGENT_TOOL_PARAM_DIFF_OLD/NEW`）与执行函数，以及工具调用的可视化渲染。
- **u10-l3（Agent web 搜索）**：`google_search` 与 `visit_page` 如何通过 `ds4_web` 的 confirm/log/cancel 回调集成进 agent。
- **复习 u7-l4 / u7-l5**：想真正吃透「为什么 agent 能省掉那套机制」，最好回头对比 server 的 DSML 精确回放与字节比对——你会更深刻地理解「架构消灭问题」的含义。
- **阅读 u8-l1 / u8-l3**：本讲的 `.kv` 文件内部结构（KVC 头、DSV4 payload、frontier 序列化）在这两讲里有完整字节级拆解，是理解 `/switch` 如何免 prefill 的底层基础。
- **源码延伸**：读 `worker_run_turn` 的工具循环 [ds4_agent.c:7682-7789](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7682-L7789) 与上下文压缩 `agent_worker_compact_if_needed`，理解长编码会话如何通过 summarization 控制上下文增长。
