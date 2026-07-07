# Agent 工具系统

## 1. 本讲目标

承接 u10-l1（原生编码 Agent 设计），本讲钻进 ds4-agent 的「手」——工具系统。学完本讲，你应该掌握：

- 模型如何用 DSML 文本直接表达一次工具调用，agent 如何把这段**流式字节**增量解析成结构化的 `agent_tool_call`；
- DSML 参数的两种来源（`string="true"` 原始文本 / `string="false"` JSON 字面量），以及七种「参数种类（param kind）」的分类逻辑与用途；
- 每个内建工具（read/more/write/list/edit/search/google_search/visit_page/bash/bash_status/bash_stop）的参数与执行函数做了什么；
- 工具调用在终端上如何被「边生成边可视化」——为什么 `read` 有专门的紧凑进度行、为什么 `edit` 的 old/new 会渲染成红绿 diff。

## 2. 前置知识

本讲假设你已经了解（在 u10-l1、u3-l3 建立）：

- **ds4-agent 是单进程双线程**：UI 线程管终端（linenoise），worker 线程独占一条活 `ds4_session`；
- **KV-as-session**：每个推进 KV 的 token 都被同一行代码同步追加进 transcript，工具结果也是普通 token（见 u10-l1）；
- **DSML**：DeepSeek V4 的工具调用文本格式，用全角竖线包裹的 `<｜DSML｜…>` 标记表达，模型直接以 token 流生成（见 u3-l3、u7-l4）。

本讲只关心「DSML 解析 → 工具执行 → 终端可视化」这条链，不涉及 KV 持久化与 agent 会话管理（见 u10-l1）。

## 3. 本讲源码地图

本讲涉及的源码几乎全部集中在 `ds4_agent.c`（约 1 万行，本讲覆盖其中三段）：

| 段落 | 行号区间 | 作用 |
|------|----------|------|
| 系统提示（教模型写 DSML + 工具 schema） | 约 707–946 | 告诉模型有哪些工具、参数、严格语法 |
| DSML 解析器 + 参数数据结构 | 约 207–249、1266–1543 | 把流式字节增量解析成 `agent_tool_calls` |
| 参数种类分类与颜色 | 约 251–2728、2862–3020 | 七种 param kind，驱动可视化 |
| 各工具执行函数 | 约 5462–6620 | read/more/write/list/edit/search/web |
| 工具分发 | 约 7143–7214 | 按名字派发到执行函数 |
| worker 主循环里调用工具 | 约 7850–7940 | 解析结果、执行、塞回 transcript |

`linenoise.h` 提供 `linenoiseEditSetStatus`/`linenoiseEditFeed` 等接口，是 UI 线程渲染工具进度行与状态行的底层；本讲在「可视化」一节会点到它的角色。

## 4. 核心概念与源码讲解

### 4.1 工具解析与参数种类

#### 4.1.1 概念说明

ds4-agent 不把工具调用翻译成 JSON 再喂回模型（这是 u7 服务器无状态 API 的做法），而是让模型用 DeepSeek V4 **原生**的 DSML 文本格式直接生成。一次工具调用的标准形态是：

```
<｜DSML｜tool_calls>
<｜DSML｜invoke name="read">
<｜DSML｜parameter name="path" string="true">/tmp/foo.c</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>
```

要点：

- `invoke` 的 `name` 属性是**工具名**；
- 每个 `parameter` 有一个 `name` 和一个 `string` 属性：`string="true"` 表示值是**原始文本**（按字节原样取，只对闭合标签做转义保护），`string="false"` 表示值是 **JSON 字面量**（数字、布尔）。

agent 的工作就是：在模型**流式吐出**这些字节的同时，**增量**地把它们解析成结构化的 `agent_tool_call`，再喂给对应的执行函数。

#### 4.1.2 核心流程

解析器是一个流式状态机 `agent_dsml_parser`，五个状态：

```
SEARCH ──(匹配到 <｜DSML｜tool_calls> 起始串)──▶ STRUCTURAL
STRUCTURAL ──(读到 <｜DSML｜invoke name=...>)──▶ STRUCTURAL（记录工具名）
STRUCTURAL ──(读到 <｜DSML｜parameter name=... string=...>)──▶ PARAM_VALUE
PARAM_VALUE ──(找到 </｜DSML｜parameter>)──▶ STRUCTURAL（把值存成参数）
STRUCTURAL ──(读到 </｜DSML｜invoke> 或 </｜DSML｜tool_calls>)──▶ DONE / 继续
```

关键设计是**可以逐字节喂**：`agent_dsml_feed` 每收一个字节就追加到缓冲并调用 `agent_dsml_parse` 重跑；输入不完整时状态保持不变（等更多字节），输入畸形则切到 `AGENT_DSML_ERROR`，让模型拿到可重试的工具错误。

#### 4.1.3 源码精读

数据结构是三件套：

```c
typedef struct { char *name; char *value; bool is_string; } agent_tool_arg;
typedef struct { char *name; agent_tool_arg *args; int argc, argcap; } agent_tool_call;
typedef struct { agent_tool_call *v; int len, cap; } agent_tool_calls;
```

`agent_tool_call` 表示一次调用（名字 + 参数数组），`agent_tool_calls` 表示一个 DSML 块里可能有多次调用：[ds4_agent.c:L207-L224](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L207-L224)

参数种类的枚举有七种，纯粹是为「可视化」服务的语义标签：

```c
typedef enum {
    AGENT_TOOL_PARAM_NORMAL,
    AGENT_TOOL_PARAM_PATH,
    AGENT_TOOL_PARAM_OFFSET,
    AGENT_TOOL_PARAM_CONTENT,
    AGENT_TOOL_PARAM_DIFF_OLD,
    AGENT_TOOL_PARAM_DIFF_NEW,
    AGENT_TOOL_PARAM_BASH_COMMAND,
} agent_tool_param_kind;
```
[ds4_agent.c:L251-L259](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L251-L259)

分类函数 `agent_tool_param_kind_for(tool, param)` 用「工具名 + 参数名」双键查表：bash 的 `command` 命中 `BASH_COMMAND`、edit 的 `old`/`new` 命中 `DIFF_OLD`/`DIFF_NEW`、名字叫 `path/file/filename` 的命中 `PATH`、`line/start/end/count/timeout_sec` 等命中 `OFFSET`、`content/text` 命中 `CONTENT`，其余落到 `NORMAL`：[ds4_agent.c:L2700-L2718](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2700-L2718)

参数值的读取靠线性查找（参数少，O(argc) 无所谓）：[ds4_agent.c:L1290-L1296](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L1290-L1296)

参数值的捕获发生在 PARAM_VALUE → STRUCTURAL 的转换里：从 `param_value_start` 到闭合标签之间的一整段就是值，连同 `param_is_string` 一起存进去：[ds4_agent.c:L1445-L1454](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L1445-L1454)

`invoke` 与 `parameter` 开标签的识别（注意 `param_is_string` 来自 `string="true"`，决定值是否按原始文本处理）：[ds4_agent.c:L1481-L1504](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L1481-L1504)

教模型写 DSML 的系统提示在这两段：[ds4_agent.c:L707-L725](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L707-L725)（介绍形态、`string` 属性、read 分块规则），[ds4_agent.c:L758-L946](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L758-L946)（每个工具的 JSON schema + 规则）。

#### 4.1.4 代码实践

跟踪解析链（源码阅读型）：

1. **实践目标**：理解 DSML 字节如何变成 `agent_tool_calls`。
2. **操作步骤**：在 ds4_agent.c 打开 `agent_dsml_feed`（L1524）→ `agent_dsml_parse`（L1438）→ `agent_dsml_open_tag_is`/`agent_parse_attr`，跟随一个 `<｜DSML｜invoke name="edit">` 的字节如何把 `p->current.name` 填成 `"edit"`。
3. **需要观察的现象**：注意 PARAM_VALUE 状态只在「找到闭合 `</｜DSML｜parameter>`」时才落库（L1442-L1448），中间字节都先攒在 raw 缓冲里。
4. **预期结果**：你能说清「为什么值不是边读边存、而是等闭合标签一次性截取」——因为值里可能包含任意文本（含 `>`），必须靠显式闭合标签定位边界。
5. 由于本实践是静态源码阅读，不需要 GPU/GGUF 即可完成；若要观察真实字节流，需在本地机器运行 `./ds4-agent`（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PARAM_VALUE 状态需要单独存在，而不是在 STRUCTURAL 里顺便收集？

> **答案**：参数值是自由文本（文件内容、shell 命令），里面可能出现 `<`、`>`、换行等字符；只有显式闭合标签 `</｜DSML｜parameter>` 才是值的可靠边界（见 `agent_dsml_find_close_tag`，L1425）。把值收集拆成独立状态，能让「找闭合标签」成为唯一的值终止判定。

**练习 2**：`string="true"` 与 `string="false"` 在解析阶段实际产生了什么差别？

> **答案**：在 `agent_tool_call_add_arg` 里只把 `is_string` 布尔存进 `agent_tool_arg`（L1273-L1277），值字符串本身都按同样的字节截取。差别主要传给执行函数（决定 `agent_parse_int_default`/`agent_parse_bool_default` 如何解读）与可视化（见 4.3）。它是「语义提示」而非「不同的字节路径」。

### 4.2 工具执行函数

#### 4.2.1 概念说明

解析完一个 DSML 块得到 `agent_tool_calls`，agent 按 `name` 把每个 `agent_tool_call` 派发给对应的 C 函数。每个执行函数的契约统一：接收 `agent_worker *w` 和 `const agent_tool_call *call`，返回一段**文本**（malloc 出来的字符串）。这段文本会被作为 `tool` 角色消息追加进 transcript，成为模型下一轮能看到的「观察」。

错误处理也统一：所有执行函数返回的文本都以 `Tool error: ...` 开头表示失败，且**仍然追加进 transcript**——模型读到错误后可以重试。这比抛异常更简单，也与「工具结果就是普通 token」的哲学一致。

#### 4.2.2 核心流程

分发函数 `agent_execute_tool_call` 就是一串 `strcmp`：

```c
if (!strcmp(call->name, "read"))   return agent_tool_read(w, call);
if (!strcmp(call->name, "write"))  return agent_tool_write(w, call);
/* ... */
if (!strcmp(call->name, "bash"))   { /* 内联：fork 长任务 */ }
```

匹配不上则返回 `Tool error: unknown tool`。bash 比较特殊（它要 fork 一个可能长时间运行的任务），所以直接内联在分发函数里，而不是单独的 `agent_tool_bash`。

`agent_execute_tool_calls` 遍历块内所有调用，给每个结果加一行 `Tool result N (name):` 头，再拼成一条 `tool` 消息。worker 主循环拿到这一条消息后，先检查它是否放得下上下文（`agent_tool_result_fits_context`，预留 `AGENT_TOOL_RESULT_RESERVE_TOKENS = 1024` 个 token），放不下就先 compact（压缩历史），再用 `ds4_chat_append_message(engine, transcript, "tool", result)` 塞回去。

#### 4.2.3 源码精读

派发与 unknown tool 兜底：[ds4_agent.c:L7143-L7201](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7143-L7201)

多调用拼接：[ds4_agent.c:L7205-L7219](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7205-L7219)

worker 主循环里执行工具、上下文检查、回写 transcript：[ds4_agent.c:L7896-L7939](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7896-L7939)（注意 L7939 把结果以 `"tool"` 角色写进 transcript）

各工具一览（参数与行为）：

| 工具 | 参数 | 行为 | 源码 |
|------|------|------|------|
| `read` | path, start_line, max_lines, whole, raw | 按行号前缀读一段，默认 500 行，带 `continue_offset` 注释 | [L5728-L5737](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5728-L5737)、[L5654-L5718](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5654-L5718) |
| `more` | count | 续读上次 read 的下一块（用 worker 里缓存的 `more_path/more_next_line`） | [L5739-L5745](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5739-L5745) |
| `write` | path, content | `fopen("wb")` 整体覆盖写 | [L5747-L5774](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5747-L5774) |
| `list` | path | `opendir`+`lstat`，列最多 300 项，标 `d/l/-` | [L5776-L5811](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5776-L5811) |
| `edit` | path, old, new | 找到 old 的唯一匹配（支持 `[upto]` 锚定），原地替换 | [L6267-L6308](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6267-L6308) |
| `search` | query, path, mode, glob, context, max_results, case_sensitive | 字面/正则递归搜，返回 edit 友好的匹配 | [L6437-L6484](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6437-L6484) |
| `google_search` | query | 调 `ds4_web_google_search`（见 u10-l3） | [L6558-L6572](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6558-L6572) |
| `visit_page` | url | 调 `ds4_web_visit_page`，渲染 markdown 存临时文件 | [L6574-L6620](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6574-L6620) |
| `bash` | command, timeout_sec, refresh_sec | `fork`+`execl("/bin/sh","-c")`，可后台跑、可轮询 | [L7156-L7171](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7156-L7171)、[L6808-L6852](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6808-L6852) |
| `bash_status` | job, pid, refresh_sec | 查一个在跑 bash 任务的最新输出 | [L7173-L7190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7173-L7190) |
| `bash_stop` | job, pid, refresh_sec | 终止一个 bash 任务并拿最终输出 | [L7173-L7190](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7173-L7190) |

edit 的安全设计值得一提：`old` 文本必须**在文件里唯一匹配**，否则报错（防止误改）；`agent_preflight_edit_old` 甚至在 `new` 参数还没生成完时就先校验 `old` 能否定位，做到「流式预检」：[ds4_agent.c:L6203-L6227](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6203-L6227)

bash 的关键细节：子进程的 stdin 被重定向到 `/dev/null`（L6839-L6847），原因是「bash 工具不是交互式的」，不能让它继承 agent 的 linenoise 终端、在后台把终端从 raw 模式偷偷改回 cooked 模式。

#### 4.2.4 代码实践

列出每个工具的参数（对应本讲 practice_task）：

1. **实践目标**：把分发与参数清单整理成表，并解释 `AGENT_TOOL_PARAM_DIFF_OLD/NEW` 等参数种类的用途。
2. **操作步骤**：打开分发函数 [ds4_agent.c:L7143-L7201](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7143-L7201)，再跳到每个工具函数读 `agent_tool_arg_value(call, "...")` 调用，把参数名抄下来；与系统提示里的 JSON schema（[L758-L946](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L758-L946)）对照，两者应当一致。
3. **需要观察的现象**：参数名 schema 与执行函数读取的名字完全对应（例如 read 的 `start_line` 在 [L5731](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L5731) 与 schema [L849](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L849) 都叫 `start_line`）。
4. **预期结果**：得到一张 11 个工具的参数表（见 4.2.3 已给出），并理解 `AGENT_TOOL_PARAM_DIFF_OLD/NEW` 等种类**并非执行函数使用**，而是 4.3 的可视化用来决定把参数渲染成红绿 diff 的语义标签。
5. 这是纯源码阅读实践，无需运行；若想验证模型实际生成的参数，需在本地运行 `./ds4-agent`（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 edit 的 `old` 必须唯一匹配？如果文件里有两处相同文本会怎样？

> **答案**：会返回 `Tool error: ...`（见 `agent_edit_find_old_span` 的歧义判定）。强制唯一是为了安全——避免一次模糊替换改错地方；模型收到错误后会先 read 更多上下文，再用 `[upto]` 锚定或更长的唯一首尾来重试。

**练习 2**：bash 任务的输出为什么不直接 pipe 到 worker、而要先 `mkstemp` 一个临时文件？

> **答案**：bash 可能是长任务（后台跑、轮询），输出需要跨多次 `bash_status` 调用累积与增量返回；用临时文件（L6810-L6815）持久化全部输出，配合 job/pid 索引（`agent_bash_find_job`），才能支持「先启动、稍后再来取新输出」的异步模型。

### 4.3 工具调用可视化

#### 4.3.1 概念说明

工具执行（4.2）发生在工具**完全生成完之后**。但用户体验不能等到工具全部生成才一次性弹出结果——agent 在模型**边流式生成 DSML 字节时**，就把这些字节「翻译」成人类可读的终端进度渲染出来。这就是可视化层（`agent_tool_visualizer`）的职责。

它和解析器**共享同一份字节流**：解析器算出「现在在哪个工具的哪个参数」，可视化器据此决定怎么画。例如：

- `read` 工具走一条专门的紧凑进度行：`🛠️ Reading src/foo.c 1:500...`
- `bash` 走 `$ ls -la` 风格
- `edit` 的 `old`/`new` 参数渲染成红绿 diff（`- ` / `+ `）
- `write` 的 `content` 渲染成代码块

可视化**不影响** KV、不进 transcript（transcript 里存的是原始 DSML 字节），纯粹是 UI 侧的修饰。

#### 4.3.2 核心流程

```
worker 线程从 session 取一个 token
   ├─▶ 解析器 agent_dsml_feed(token)      ← 计算「当前工具/参数」
   └─▶ 流渲染器 renderer(token)           ← 把同一些字节画到终端
         └─ 若在 DSML 段：交给 agent_tool_visualizer
               ├─ 工具名第一次出现 → agent_tool_viz_tool（画 🛠️ 前缀 + 工具风格）
               ├─ 参数开始      → agent_tool_viz_param_begin（按 param_kind 决定画法）
               ├─ 参数字节      → agent_tool_viz_param_raw_byte（路径/code/普通）
               └─ 参数结束      → agent_tool_viz_param_end
```

视觉差异几乎全由 `param_kind` 驱动：`DIFF_OLD`/`DIFF_NEW` 进代码块并打 `- `/`+ ` 前缀，`CONTENT` 进代码块，`PATH` 走颜色高亮，read 工具额外走 `read_style` 紧凑行。

终端底层是 linenoise：UI 线程用 `linenoiseEditSetStatus` 画输入框下方的状态/进度行，工具进度行则通过普通 stdout 写（彩色终端用 `\r\x1b[2K` 清当前行重画；非交互 stdout 模式禁用所有光标控制转义，以防污染管道）。

#### 4.3.3 源码精读

工具前缀与图标：[ds4_agent.c:L2768-L2777](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2768-L2777)（`agent_tool_viz_prefix` 给每个工具一个短前缀如 `read `/`$ `），[ds4_agent.c:L2779-L2804](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2779-L2804)（`agent_tool_viz_tool`：read 走专门的 `read_style`、bash 用青色、其余白色）。

参数开始时的画法分发（`agent_tool_viz_param_begin`）：[ds4_agent.c:L2941-L2987](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2941-L2987)——DIFF_OLD/NEW 进代码块，CONTENT 进代码块，其余按 `name=value` 行内画。

判断「哪些工具的哪些参数算代码体」：[ds4_agent.c:L2862-L2872](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2862-L2872)（write 的 content、edit 的 old/new/content 都当代码渲染）。

diff 的 `- `/`+ ` 前缀与红/绿颜色：[ds4_agent.c:L2874-L2886](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2874-L2886)

param kind → 颜色映射：[ds4_agent.c:L2720-L2728](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2720-L2728)（PATH 绿、OFFSET 黄、CONTENT 蓝、DIFF_OLD 红、DIFF_NEW 绿、BASH_COMMAND 加粗青）。

DSML 段开头清当前行（只对彩色交互终端）：[ds4_agent.c:L2748-L2758](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2748-L2758)

UI 线程用 linenoise 画状态行（与本讲相关的衔接点）：[ds4_agent.c:L9130-L9135](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L9130-L9135)，底层接口 `linenoiseEditSetStatus` 见 [linenoise.h:L108-L109](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/linenoise.h#L108-L109)。

#### 4.3.4 代码实践

观察 read 与 edit 的渲染差异（运行型，需本地机器）：

1. **实践目标**：直观感受 param_kind 如何改变终端输出。
2. **操作步骤**：在彩色终端跑 `./ds4-agent`，让它读一个大文件（触发 `🛠️ Reading path 1:500...`），再让它改一处代码（触发红绿 diff）。
3. **需要观察的现象**：read 是单行紧凑进度；edit 的 old 行带红色 `- `、new 行带绿色 `+ `。
4. **预期结果**：与 [ds4_agent.c:L2862-L2872](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2862-L2872) 的「代码体」判定一一对应。
5. 若无 GGUF/无 GPU 无法运行，则改为**源码阅读型实践**：在 [ds4_agent.c:L2941-L2987](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2941-L2987) 跟踪 `param_kind == AGENT_TOOL_PARAM_DIFF_OLD` 分支，说明它如何进入 code-block 渲染并打 `- ` 前缀——渲染效果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么可视化层完全不碰 transcript 与 KV？

> **答案**：transcript 的权威内容是模型生成的**原始 DSML 字节**（这样下一轮前缀复用、磁盘 KV 持久化都精确）。可视化只是把这些字节在终端「再画一遍」给人看，属于纯输出修饰；若把渲染后的红绿 diff 写进 transcript，反而会和模型生成的字节不一致、破坏 KV-as-session 的恒等性（见 u10-l1）。

**练习 2**：非交互（管道/重定向）模式下，可视化做了什么降级？

> **答案**：禁用所有光标控制转义（`\r\x1b[2K`、颜色），只在行首需要时输出普通换行（L2748-L2758）。这样 `./ds4-agent` 的输出可以被管道捕获而不混入乱码转义序列。

## 5. 综合实践

把三节串起来：用源码阅读追踪「一次 edit 调用」的完整生命周期。

任务：假设模型生成了如下 DSML：

```
<｜DSML｜tool_calls><｜DSML｜invoke name="edit"><｜DSML｜parameter name="path" string="true">a.c</｜DSML｜parameter><｜DSML｜parameter name="old" string="true">int x;</｜DSML｜parameter><｜DSML｜parameter name="new" string="true">int x = 0;</｜DSML｜parameter></｜DSML｜invoke></｜DSML｜tool_calls>
```

请按顺序回答并定位源码：

1. **解析**：这些字节如何让 `p->current.name` 变成 `"edit"`、三个参数如何被 `agent_tool_call_add_arg` 落库？（定位 [ds4_agent.c:L1481-L1504](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L1481-L1504)、[ds4_agent.c:L1445-L1454](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L1445-L1454)）
2. **可视化**：在 PARAM_VALUE 期间，可视化器如何判断 `old`/`new` 应渲染成红绿 diff？（定位 [ds4_agent.c:L2700-L2718](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2700-L2718) 的分类 + [ds4_agent.c:L2862-L2872](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2862-L2872) 的代码体判定 + [ds4_agent.c:L2874-L2886](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L2874-L2886) 的 `- `/`+ ` 前缀）
3. **执行**：解析完成后 `agent_execute_tool_call` 如何派发到 `agent_tool_edit`？`agent_edit_find_old_span` 如何保证 `int x;` 唯一匹配、`agent_apply_file_splice` 如何改盘？（定位 [ds4_agent.c:L7151](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7151)、[ds4_agent.c:L6267-L6308](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L6267-L6308)）
4. **回写**：返回的文本如何经过 `agent_tool_result_fits_context` 检查与 `ds4_chat_append_message("tool", ...)` 进 transcript，从而成为下一轮 KV 前缀的一部分？（定位 [ds4_agent.c:L7896-L7939](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_agent.c#L7896-L7939)）

**预期结果**：你能画出一条「字节 → 结构 → 终端画面 → 文件改动 → transcript token」的完整数据流，并指出每个环节的代码位置。这一条流就是 ds4-agent「原生工具」之所以不需要 JSON 转换、也不需要 u7 精确回放机制的根本原因——工具调用从一开始就是 KV 里的 token。

## 6. 本讲小结

- DSML 让模型用**原生文本格式**直接生成工具调用，agent 用流式状态机（SEARCH → STRUCTURAL → PARAM_VALUE）把字节增量解析成 `agent_tool_call`，参数值的边界靠显式闭合标签、靠 `string="true/false"` 区分原始文本与 JSON 字面量。
- 参数有七种「param_kind」（NORMAL/PATH/OFFSET/CONTENT/DIFF_OLD/DIFF_NEW/BASH_COMMAND），由「工具名 + 参数名」分类，主要驱动可视化与颜色，是语义标签而非不同的执行路径。
- 11 个内建工具（read/more/write/list/edit/search/google_search/visit_page/bash/bash_status/bash_stop）统一契约：返回一段文本，错误也以 `Tool error:` 文本回传，并被当作 `tool` 角色消息塞进 transcript。
- 执行发生在工具完全生成后，由 `agent_execute_tool_call` 用 `strcmp` 派发；工具结果进 transcript 前先做上下文容量检查，放不下先 compact。
- 可视化与解析共享同一份字节流，但**只画给人看、不碰 transcript/KV**，靠 param_kind 决定 read 紧凑行、bash `$ ` 前缀、edit 红绿 diff、write 代码块等画法，非交互模式降级为纯文本。
- 工具调用从生成就同时是 KV 里的 token，所以 ds4-agent 天然不需要 u7 服务器的精确 DSML 回放与规范化机制。

## 7. 下一步学习建议

- **u10-l3 Agent web 搜索**：深入 `google_search`/`visit_page` 背后的 `ds4_web.{c,h}`，看 confirm/log/cancel 三个回调如何在 agent 里驱动「首次 web 调用要问用户授权启动 Chrome」。
- 想理解工具结果如何影响 KV 与磁盘持久化，回顾 **u10-l1**（KV-as-session）与 **u8-x**（KVC 文件格式、payload 序列化）。
- 想理解「为什么服务器需要精确回放而 agent 不需要」，对照 **u7-l4**（DSML、精确回放、规范化）——本讲的工具执行是 agent 版的对应物，因为进程内推理而大幅简化。
- 进阶练习：仿照某个执行函数（如 `agent_tool_list`）的契约，在源码阅读层面设计一个新工具（例如 `glob`）需要改的三处——系统提示 schema、`agent_tool_param_kind_for` 分类、`agent_execute_tool_call` 派发——不动源码，只画改动图。
