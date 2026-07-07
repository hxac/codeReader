# 工具调用：DSML、精确回放、规范化

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 **DSML**（DeepSeek 的工具调用文本格式）长什么样、为什么 ds4 让模型直接「说 DSML」而不是套一层 JSON。
2. 理解 ds4-server 用 **rax 基数树**维护的「tool id → 精确采样 DSML 块」回放映射，以及它为什么能让无状态 API 重发工具调用时 **KV 前缀不失配**。
3. 讲清「精确回放」失灵时的 **规范化（canonicalization）后备路径**，以及生成时对 **DSML 语法 token 强制贪婪解码**、对参数负载保留正常采样的分离策略。

本讲是服务器单元（u7）的第四篇，承接 u7-l2（端点路由）与 u7-l3（SSE 流式），并依赖 u2-l3（session 同步与前缀复用）。它专讲「工具调用」这条横跨协议解析、KV 复用、采样三层的链路。

## 2. 前置知识

- **工具调用（tool call / function call）**：LLM 不直接给答案，而是输出一段「请帮我调用工具 X，参数是 Y」的结构化指令，由宿主程序执行后把结果喂回，模型再继续。OpenAI/Anthropic 的 API 把这段指令表示成 JSON 对象；DeepSeek V4 则表示成一种叫 DSML 的文本。
- **无状态 API 与历史回放**：OpenAI/Anthropic 风格的 API 是无状态的——客户端每次请求都要把**整段对话历史**（含上一轮的工具调用与工具结果）重新发一遍。服务器必须把这些历史重新拼成 prompt token 序列。`ds4-server` 内部只有**一个可变的 `ds4_session`**（见 u7-l1），靠前缀复用来避免每轮都从零 prefill。
- **KV 前缀失配**：u2-l3 讲过，`ds4_session_common_prefix` 比较的是 token 序列前缀。如果服务器这次把同一段历史拼出的字节与上一轮「模型真实采样」的字节**哪怕差一个空格、一个转义符、一个 key 顺序**，token 序列前缀就会在工具调用处断开，后缀只能整段重新 prefill——这就是「失配」。
- **rax 基数树**：Redis 作者 Salvatore Sanfilippo 写的字符串→指针映射结构（[rax.h:1-29](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rax.h#L1-L29) 的 BSD 版权头写明出处），前缀相同的键共享前缀节点，查找/插入都是 O(键长)。ds4 把它直接搬进树（`#include "rax.h"`，[ds4_server.c:5](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5)）做工具记忆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ds4_server.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c) | HTTP 服务器主体。DSML 渲染、解析、工具记忆（rax）、规范化、语法贪婪解码全在这里。 |
| [rax.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rax.h) / [rax.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rax.c) | 借自 Redis 的基数树实现，提供 `raxNew`/`raxInsert`/`raxFind`/`raxRemove`/`raxFree`。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 「Tool call handling and canonicalization」与磁盘 KV 文件格式两节是本讲的设计说明书。 |

ds4_server.c 里相关代码跨度很大，建议按本讲的三个模块分别定位：模块 1 在 2024-2261 与 4214-4530；模块 2 在 7642-8182 与 8542-8581；模块 3 在 5220-5451、9821-9977、10391-10410。

## 4. 核心概念与源码讲解

### 4.1 DSML 工具调用文本格式

#### 4.1.1 概念说明

DeepSeek V4 不用 JSON，而是用一种类似 XML 的文本格式来表达工具调用，官方称为 **DSML**。它的标志是一对**全角竖线**包裹的标记 `｜DSML｜`（注意是 U+FF5C，不是普通 ASCII 竖线）。一个完整的工具调用块长这样：

```
<｜DSML｜tool_calls>
<｜DSML｜invoke name="bash">
<｜DSML｜parameter name="command" string="true">ls -la</｜DSML｜parameter>
</｜DSML｜invoke>
</｜DSML｜tool_calls>
```

为什么要用文本而不是 JSON？因为模型本质是「下一个 token 预测器」，对它来说 DSML 和它平时生成的自然语言/代码是同一种东西——**就是 token 流**。这样工具调用和正常回答可以共用同一条生成路径、同一套 KV 缓存，而不需要专门的「函数调用头」。代价是：宿主程序要把模型吐出的 DSML 文本**解析**回结构化的工具对象，再把客户端发回的 JSON 工具对象**渲染**回 DSML 文本塞进历史——这两个方向正是本模块与模块 2/3 的全部工作。

ds4-server 把 DSML 的语法骨架定义为一组宏（[ds4_server.c:4214-4227](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4214-L4227)）：

```c
#define DS4_DSML "｜DSML｜"
#define DS4_DSML_SHORT "DSML｜"          // 容错：模型偶尔漏掉第一个全角竖线
#define DS4_TOOL_CALLS_START "<" DS4_DSML "tool_calls>"
#define DS4_TOOL_CALLS_END   "</" DS4_DSML "tool_calls>"
#define DS4_INVOKE_START "<" DS4_DSML "invoke"
...
```

每个参数带一个关键属性 `string="true|false"`：`true` 表示参数体是**原始文本**（保留 `>`、`&`、`&&` 等字符原样，只对恰好等于闭标签的子串做保护）；`false` 表示参数体是 **JSON 字面量**（数字、布尔、数组、对象）。这个 `string` 标志后来在模块 3 里直接决定该不该强制贪婪采样。

#### 4.1.2 核心流程

服务器在每个带工具的请求里，先在系统提示里**教**模型怎么写 DSML（教它格式 + 强制要求严格遵循 schema），再把模型的回答**解析**回 `tool_calls` 结构。流程是：

1. **渲染系统提示**：把 `tools` 数组的 JSON schema 拼进一段固定的 DSML 教程文本（[ds4_server.c:2024-2046](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2024-L2046)），告诉模型「用 `string="true"` 传原始文本，其它类型用 JSON」。
2. **生成**：模型按教程吐出 DSML 文本（与普通文本走同一条 decode 循环）。
3. **解析**：`parse_generated_message_ex` 扫描回答，定位 `<｜DSML｜tool_calls>` 块，逐 invoke/parameter 抽出 name 与参数值，同时**原样记下整块字节**到 `calls->raw_dsml`（这就是模块 2 的「精确块」来源）。
4. **投影回 API**：把结构化结果翻译成 OpenAI 的 `tool_calls` / Anthropic 的 `tool_use` / Responses 的函数调用事件。

#### 4.1.3 源码精读

**教模型写 DSML 的系统提示片段**（[ds4_server.c:2027-2040](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2027-L2040)）：

```c
"You can invoke tools by writing a \"<｜DSML｜tool_calls>\" block like the following:\n\n"
"<｜DSML｜tool_calls>\n"
"<｜DSML｜invoke name=\"$TOOL_NAME\">\n"
"<｜DSML｜parameter name=\"$PARAMETER_NAME\" string=\"true|false\">$PARAMETER_VALUE</｜DSML｜parameter>\n"
...
"String parameters should be specified as raw text and set `string=\"true\"`. "
"Preserve characters such as `>`, `&`, and `&&` exactly; never replace ... with XML or HTML entity escapes. "
"Only if a string value itself contains the exact closing parameter tag `</｜DSML｜parameter>`, "
"write that tag as `&lt;/｜DSML｜parameter>` inside the value. "
"For all other types (numbers, booleans, arrays, objects), pass the value in JSON format and set `string=\"false\"`."
```

这段提示定下了 DSML 的两条转义规则，代码里的渲染函数严格遵守：

- `append_dsml_parameter_text`（原始文本）：**只**把恰好等于闭标签 `</｜DSML｜parameter>` 的子串里的 `<` 改写成 `&lt;`，其余字符（包括 `&`、`>`）原样保留（[ds4_server.c:2144-2155](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2144-L2155)）。
- `append_dsml_json_literal`（JSON 字面量）：同样只保护闭标签，但把 `<` 改写成 JSON 里的 `<`（[ds4_server.c:2175-2186](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2175-L2186)）。

**渲染时的「精确块优先」分叉**——这是全篇最关键的一行判断（[ds4_server.c:2242-2247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2242-L2247)）：

```c
static void append_dsml_tool_calls_text(buf *b, const tool_calls *calls) {
    if (!calls || calls->len == 0) return;
    if (calls->raw_dsml && calls->raw_dsml[0]) {
        buf_puts(b, calls->raw_dsml);   // 精确回放：逐字节抄模型当时采样的块
        return;
    }
    buf_puts(b, "\n\n<｜DSML｜tool_calls>\n");  // 否则才走规范化重建
    ...
}
```

只要 `calls->raw_dsml` 非空，渲染器就**逐字节抄写**模型当时采样的整块 DSML，完全不碰 JSON；只有当精确块缺失时，才退而用 `append_dsml_arg` 从 JSON 重建（[ds4_server.c:2188-2196](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2188-L2196)）。这个「逐字节 vs 重建」的分叉，就是模块 2（精确回放）与模块 3（规范化后备）的衔接点。

**解析时如何抓取精确块**（[ds4_server.c:4527-4528](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4527-L4528)）：

```c
const char *raw_block_end = p + strlen(tool_calls_end);
free(calls->raw_dsml);
calls->raw_dsml = xstrndup(raw_block_start, (size_t)(raw_block_end - raw_block_start));
```

`raw_block_start` 指向 `<｜DSML｜tool_calls>` 的开头，`raw_block_end` 指向闭标签 `</｜DSML｜tool_calls>` 之后——这之间（含两个标签）就是模型真实吐出的字节，被原样复制成 `raw_dsml`。

#### 4.1.4 代码实践

**实践目标**：用 `--dump-tokens` 观察 DSML 特殊标记如何被分词，并亲手比对「精确块」与「规范化重建」的差别。

**操作步骤**：

1. 阅读 [ds4_server.c:4214-4227](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4214-L4227)，记下 `DS4_TOOL_CALLS_START` 等六个标记的字符串。
2. 用 CLI 把含闭标签的文本喂给分词器（`｜DSML｜` 用全角竖线，可从源码复制）：
   ```sh
   ./ds4 --dump-tokens -p '</｜DSML｜tool_calls>'
   ```
3. 阅读 `append_dsml_parameter_text`（[ds4_server.c:2144-2155](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2144-L2155)），手动推演：若一个 `string="true"` 的参数值恰好是 `a </｜DSML｜parameter> b`，渲染后会变成什么字节序列。

**需要观察的现象**：
- `--dump-tokens` 输出里，闭标签开头的 `</` 与 `｜DSML｜` 会落在**不同 token**上（README「Debugging Notes」明确指出 DSML 闭标记以两个 token 起步：`</` 和 `｜DSML｜`，见 [README.md:1252-1254](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1252-L1254)）。
- 手动推演应得到 `a &lt;/｜DSML｜parameter> b`（只把那个 `<` 转义，`b` 前的原样空格保留）。

**预期结果**：你能解释「为什么 DSML 转义规则刻意只保护闭标签」——因为只有闭标签会让解析器提前结束参数体，其它字符（`&`、`>`）对 DSML 解析无意义，原样保留才能让文件内容/命令体逐字节复用 KV。若无法本地运行，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 DSML 的参数要区分 `string="true"` 和 `string="false"`？如果一律用 JSON 会有什么坏处？

**答案**：`string="true"` 让命令体、文件内容、代码等以**原始文本**进入 token 流，省掉 JSON 引号/转义的开销，也让这些长文本与模型预训练时见到的形式一致、更利于 KV 复用；`string="false"` 则把数字/布尔/数组等结构化值以 JSON 表达。若一律 JSON，长文本会被大量 `\"`、`\\n` 转义撑大，且模型采样这些转义符时容易出错，反而破坏可解析性。

**练习 2**：`DS4_DSML_SHORT`（`"DSML｜"`，少了第一个全角竖线）为什么也要定义？

**答案**：模型偶尔会把 `<｜DSML｜...>` 的第一个全角竖线漏掉，写成 `<DSML｜...>`。解析器（[ds4_server.c:4471-4489](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4471-L4489)）和生成期识别器（`dsml_syntaxes[]`，[ds4_server.c:5245-5261](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5245-L5261)）都把这种「短形式」作为容错接受，避免一次笔误毁掉整轮工具调用。

---

### 4.2 精确 DSML 回放映射（rax 基数树）

#### 4.2.1 概念说明

这是本讲的核心机制，解决一个尖锐的矛盾：

> 模型说 DSML，客户端却说 JSON。客户端下一轮会把上一轮的工具调用以 **JSON 对象**发回来。如果服务器把这个 JSON 重新渲染成 DSML，**渲染出的字节几乎不可能和模型当时真实采样的字节一模一样**——JSON 的 key 顺序、空格、转义都可能不同。于是渲染出的 prompt 前缀与活 KV checkpoint 对不上，前缀复用断在工具调用处，整段后缀要重 prefill。

ds4 的解法是「**精确回放**」：给每个工具调用分配一个**不可猜测的 API tool id**（如 `call_4f3a...`），并在内存里记住 `tool id → 模型当时采样的精确 DSML 字节块`。下一轮客户端把这个 id 原样发回时，渲染器不重新渲染，而是**直接把记住的字节块抄进去**。这样渲染字节 == 活 KV 字节，前缀复用命中，零额外 prefill。

这套记忆用 rax 基数树存储，因为：(a) 查找按 id 字符串，基数树 O(键长) 且前缀压缩省内存；(b) DSML 块本身也要去重（多个 id 可能指向同一块），需要第二个基数树按字节内容反查。

#### 4.2.2 核心流程

整个回放围绕一张双向映射的内存表 `tool_memory` 展开：

```
            by_id (rax: id字符串 → entry)         by_block (rax: dsml字节 → block)
            ┌──────────────────────────┐          ┌──────────────────────────┐
  put 时 →  │ "call_4f3a.." → entry_A  │          │ "\n\n<｜DSML｜.." → block_X│
            │ "call_9b1c.." → entry_B  │          └──────────────────────────┘
            └──────────────────────────┘                ▲
                       │  entry.block 指针               │  多个 entry 可共享一个 block（去重 + 引用计数）
                       └────────────────────────────────┘
            LRU 双向链表 (head ... tail) 按 stamp(clock) 排序，超限从 tail 淘汰
```

1. **生成结束**：解析出 `parsed_calls` 后，`assign_tool_call_ids` 给每个调用分配不可猜测 id（[ds4_server.c:8193-8203](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8193-L8203)），`tool_memory_remember` 把 `(id, raw_dsml)` 存进表（[ds4_server.c:8103-8112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8103-L8112)）。
2. **下一轮渲染前**：`tool_memory_attach_to_messages` 遍历客户端发来的历史，对每个工具调用按 id 查表；若全部 id 命中且指向同一块，就把那块字节挂回 `calls->raw_dsml`（[ds4_server.c:8129-8182](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8129-L8182)）。随后 `append_dsml_tool_calls_text`（4.1.3）走「逐字节抄写」分支。
3. **重启恢复**：进程重启后内存表清空，`kv_cache_restore_tool_memory_for_messages` 扫描磁盘 KV 文件里的 KTM 段，把历史中出现的 id 的映射重新装回（[ds4_server.c:8542-8581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8542-L8581)）。
4. **淘汰**：表有上限（默认 100000 个 id、512 MiB 字节），超限时从 LRU 链表尾端淘汰最久未用的（[ds4_server.c:7874-7880](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7874-L7880)）。

#### 4.2.3 源码精读

**`tool_memory` 结构：两张 rax + LRU 链表 + 字节/条目上限**（[ds4_server.c:7669-7680](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7669-L7680)）：

```c
typedef struct {
    rax *by_id;        // id 字符串 → tool_memory_entry
    rax *by_block;     // dsml 字节 → tool_memory_block（去重）
    tool_memory_entry *head, *tail;  // LRU 双向链表
    int entries, max_entries;
    size_t bytes, max_bytes;
    uint64_t clock;    // 单调递增的访问时间戳
} tool_memory;
```

`by_block` 的存在是为了**去重**：同一个 DSML 块（比如多次调用同一个 `bash` 且命令相同）只存一份字节，多个 `entry` 通过 `block` 指针共享它，靠引用计数 `refs` 管理生命周期。

**rax 的使用**——`tool_memory_init_locked` 建两张树（[ds4_server.c:7775-7780](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7775-L7780)）：

```c
static void tool_memory_init_locked(tool_memory *m) {
    if (m->by_id && m->by_block) return;
    m->by_id = raxNew();
    m->by_block = raxNew();
    ...
}
```

插入按字节内容建块用 `raxInsert(by_block, 字节, len, block, NULL)`（[ds4_server.c:7836](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7836)），按 id 建条目用 `raxInsert(by_id, id, len, entry, NULL)`（[ds4_server.c:7918](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7918)）。查找用 `raxFind`，找不到时返回哨兵 `raxNotFound`（[rax.h:268](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/rax.h#L268)），代码据此判空（[ds4_server.c:7882-7887](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7882-L7887)）：

```c
static tool_memory_entry *tool_memory_find_entry_locked(tool_memory *m, const char *id) {
    if (!m->by_id || !id || !id[0]) return NULL;
    void *v = raxFind(m->by_id, (unsigned char *)id, strlen(id));
    return v == raxNotFound ? NULL : v;
}
```

**不可猜测 id 的生成**：`random_tool_id` 用 16 字节随机数拼成 `call_` 或 `toolu_`（Anthropic 风格）前缀的十六进制串（[ds4_server.c:499-519](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L499-L519)）。分配时还要碰撞检测：新 id 必须既不在本批调用里、也不在已有记忆里（[ds4_server.c:8198-8201](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8198-L8201)）。不可猜测是安全前提——客户端只能「回放服务器发过的 id」，无法伪造一个 id 去窃取别的会话的 DSML 块。

**回放的核心：`tool_memory_attach_to_messages`**（[ds4_server.c:8143-8181](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8143-L8181)）：

```c
for (int i = 0; i < msgs->len; i++) {
    tool_calls *calls = &msgs->v[i].calls;
    if (calls->len == 0 || calls->raw_dsml) continue;   // 已有精确块就跳过
    ...
    for (int j = 0; j < calls->len; j++) {
        const char *dsml = tool_memory_lookup_locked(&s->tool_mem, calls->v[j].id, ...);
        if (!dsml) { exact = false; missing++; continue; }  // 任一 id 缺失 → 这条不能精确回放
        ...
    }
    if (exact && matched) {
        calls->raw_dsml = xstrdup(matched->dsml);   // 挂上精确块，渲染器就会逐字节抄写
        if (stats) { if (RAM) stats->mem++; else stats->disk++; }
    } else if (stats) {
        stats->canonical++; stats->missing_ids += missing;  // 记账：走了规范化后备
    }
}
```

注意「**全部 id 命中且指向同一块**」才精确回放；只要有一个 id 缺失或指向不同块，整条消息就降级为规范化（`stats->canonical++`）。`tool_replay_stats`（[ds4_server.c:536-541](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L536-L541)）用 `mem/disk/canonical/missing_ids` 四个计数器把「这一轮有多少工具调用走了精确回放、多少降级」记下来，便于 `--trace` 排查。

**淘汰**：`tool_memory_prune_locked` 在每次插入后检查，超过条目数或字节上限就从 LRU 链表尾端 `tool_memory_remove_entry_locked` 删（[ds4_server.c:7874-7880](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7874-L7880)）。访问时 `tool_memory_touch` 把条目移到链表头部并更新 `stamp`（[ds4_server.c:7798-7803](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7798-L7803)）。

#### 4.2.4 代码实践

**实践目标**：阅读测试 `test_tool_memory_max_ids_prunes_oldest`，亲手验证「LRU 淘汰导致精确回放降级为规范化」的现象，并据此回答本讲的主问题。

**操作步骤**：

1. 读测试 [ds4_server.c:14334-14362](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L14334-L14362)：把表上限设成 2，依次 put `call_a/call_b/call_c` 三条。
2. 推演：插入 `call_c` 后哪一条会被淘汰？为什么？
3. 构造一条只含 `call_a` 的历史消息，调用 `tool_memory_attach_to_messages`，断言 `stats.canonical == 1`、`stats.missing_ids == 1`。
4. 结合 `append_dsml_tool_calls_text`（4.1.3）回答：`raw_dsml == NULL` 时渲染会走哪条路？这条路与「模型当时采样的字节」是否一致？

**需要观察的现象**：
- 上限为 2 时，`call_a`（最久未访问）被淘汰，`by_id` 里只剩 `call_b/call_c`。
- 查 `call_a` 返回 NULL → `exact=false` → `raw_dsml` 保持 NULL → `stats.canonical++`。
- 渲染走规范化重建分支，**不再逐字节等于**模型原采样（除非 JSON→DSML 重建恰好一致，而这正是模块 3 要兜底的不确定性）。

**预期结果**：你能用自己的话讲清主问题的前半句——「精确回放把 id 当钥匙，把模型当时采样的 DSML 字节当值，下一轮原样抄进 prompt，使渲染字节与活 KV 字节逐字节相等，于是 `ds4_session_common_prefix` 命中、前缀复用不断、无需重建」；并指出降级条件是「id 被淘汰 / 进程重启未恢复 / `--disable-exact-dsml-tool-replay`」。

#### 4.2.5 小练习与答案

**练习 1**：为什么用两张 rax（`by_id` 和 `by_block`）而不是一张？

**答案**：`by_id` 服务于「客户端发来 id → 取出 DSML 块」这个主查询方向；`by_block` 服务于反向去重——put 时先按字节内容查 `by_block`，若同一块已存在就复用、引用计数 +1，避免相同 DSML 重复存字节。删条目时也要从 `by_block` 把引用计数减到 0 才真正释放块（[ds4_server.c:7845-7856](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7845-L7856)）。一张树无法同时按两种键高效查询。

**练习 2**：磁盘 KTM 段（[README.md:1070-1082](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1070-L1082)）只存「DSML 块出现在该缓存文件渲染文本里」的映射。为什么不存全部映射？

**答案**：精确回放只为「让渲染字节 == 活 KV 字节」服务。只有当某 DSML 块**确实出现在某个 KV 快照的渲染文本里**时，回放它才能对齐那个快照；不在任何快照文本里的块，重启后没有对应 KV 可对齐，存了也无用。所以 KTM 只存「有用」的映射，既省磁盘又保证加载后映射与可复用的快照一致。

---

### 4.3 规范化后备与语法贪婪解码

#### 4.3.1 概念说明

精确回放是第一道防线，但它会失灵：id 被淘汰、进程重启后磁盘也没恢复到、或用户显式 `--disable-exact-dsml-tool-replay`。这时 `raw_dsml` 为空，渲染器只能从 JSON **规范化重建** DSML。重建出的字节不一定等于模型当时的采样，于是又回到「前缀失配」的老问题。

ds4 给了两层兜底，方向完全不同：

1. **生成后的规范化（canonicalization）**：工具调用回合结束后，**主动**把活 checkpoint 改写成「下一轮客户端会渲染出的样子」，让未来的前缀复用重新对齐。这是对失配的**事后修复**。
2. **生成中的语法贪婪解码**：模型吐 DSML 时，把**语法结构**（标签、属性名、JSON 标点、闭标记）强制走 `temperature=0` 贪婪解码，保证工具调用**可解析**；但参数**负载**（字符串体、JSON 字符串值）保留请求的正常采样。这是对**可解析性**的**事前保证**。

两者目的不同：规范化保「KV 对齐」，语法贪婪保「文本能被解析器读懂」。

#### 4.3.2 核心流程

**规范化决策**（生成结束后）：

```
parsed_calls.len > 0 ?
  ├─ 精确回放开启 且 raw_dsml 非空 → should_canonicalize 返回 false → 不规范化
  │     （精确块已保证对齐，规范化反而会丢隐藏 reasoning 的采样状态）
  └─ 否则 → should_canonicalize 返回 true → canonicalize_tool_checkpoint：
        1. 用 prompt_text + 工具后缀拼出「下一轮将渲染的」canonical 文本
        2. tokenize 得 canonical token 序列
        3. 与活 checkpoint 比较 common 前缀
        4. 若活 checkpoint 的字节已与 canonical 字节一致 → 跳过（别用不同 BPE 拼写覆盖有效历史）
        5. 否则 ds4_session_rewrite_from_common 原地改写短后缀
        6. 若改写失败 → 回退更旧的磁盘 KV 快照 + 只重放后缀（rebuild）
```

**语法贪婪解码**（生成中，每个 token 采样前）：

```
decode 状态机把当前已生成文本归类为五态之一：
  OUTSIDE / STRUCTURAL / STRING_BODY / JSON_STRUCTURAL / JSON_STRING
  ├─ OUTSIDE：不在工具块里 → 正常采样
  ├─ STRUCTURAL / JSON_STRUCTURAL：在工具块里且是语法 → 强制 temperature=0（贪婪）
  └─ STRING_BODY / JSON_STRING：在工具块里且是参数负载 → 保留请求的正常采样
```

关键洞察：**语法必须确定**（否则标签拼错解析器读不懂），**负载允许创造**（否则长代码/文件体会因贪婪采样而重复退化）。

#### 4.3.3 源码精读

**规范化的开关判定**——`should_canonicalize_tool_checkpoint`（[ds4_server.c:9969-9977](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9969-L9977)）：

```c
static bool should_canonicalize_tool_checkpoint(const server *s, const tool_calls *calls) {
    if (!calls || calls->len == 0) return false;
    if (s && !s->disable_exact_dsml_tool_replay &&
        calls->raw_dsml && calls->raw_dsml[0])
    {
        return false;   // 精确块在手 → 不需要规范化
    }
    return true;        // 否则需要规范化兜底
}
```

调用点在生成结束、`tool_memory_remember` 之后（[ds4_server.c:10838-10897](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10838-L10897)）：先 `tool_memory_remember` 存精确块，再判定是否规范化。注意 Responses 端点（`API_RESPONSES`）**刻意跳过**规范化——它有 `previous_response_id` 协议把下一轮直接绑到活状态，不需要靠规范化对齐（[ds4_server.c:10885-10894](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10885-L10894) 的注释）。

**规范化主体**——`canonicalize_tool_checkpoint`（[ds4_server.c:9821-9864](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9821-L9864)）：

```c
buf_puts(&rendered, j->req.prompt_text);
buf_puts(&rendered, suffix_text);                       // 拼出下一轮将渲染的 canonical 文本
ds4_tokenize_rendered_chat(s->engine, rendered.ptr, &canonical);
const int live_len = ds4_session_pos(s->session);
const int common = ds4_session_common_prefix(s->session, &canonical);
if (common == live_len && canonical.len == live_len) goto done;  // 已对齐，跳过
...
ds4_session_rewrite_result rr =
    ds4_session_rewrite_from_common(s->session, &canonical, common, err, sizeof(err));
```

它先用 `render_tokens_text` 把活 checkpoint 的字节也渲染出来与 canonical 比对：**若字节已经一致就跳过**——因为 token 层面的规范化只会把「有效的采样历史」换成「同一段转录本的另一种 BPE 拼写」，得不偿失（[ds4_server.c:9838-9848](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9838-L9848) 的注释）。只有字节不一致、且 common 前缀已越过本轮 prompt 边界时，才调 `ds4_session_rewrite_from_common` 原地改写；改写失败再回退磁盘快照重建（`rebuild` 分支，[ds4_server.c:9900-9953](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9900-L9953)）。这里的「原地改写 vs 重建」判定正是 u2-l3 讲的 `ds4_session_rewrite_requires_rebuild`（`common < live_len` 表示中间被改写、需重建）。

**语法贪婪解码**——生成循环里每个 token 采样前（[ds4_server.c:10391-10410](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10391-L10410)）：

```c
dsml_decode_state dsml_state = j->req.kind == REQ_CHAT && j->req.has_tools ?
    dsml_tracker.decode : DSML_DECODE_OUTSIDE;
const bool in_tool_call = dsml_decode_state_is_tool(dsml_state);
...
float temperature = j->req.temperature;
...
if (in_tool_call && !dsml_decode_state_uses_payload_sampling(dsml_state)) {
    temperature = 0.0f;   // 语法结构 → 强制贪婪
}
int token = ds4_session_sample(s->session, temperature, top_k, top_p, min_p, &rng);
```

两个判定函数是分离的关键（[ds4_server.c:5445-5451](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5445-L5451)）：

```c
static bool dsml_decode_state_is_tool(dsml_decode_state state) {
    return state != DSML_DECODE_OUTSIDE;   // 只要进了工具块就算
}
static bool dsml_decode_state_uses_payload_sampling(dsml_decode_state state) {
    return state == DSML_DECODE_STRING_BODY || state == DSML_DECODE_JSON_STRING;  // 仅这两种是负载
}
```

五态枚举（[ds4_server.c:5220-5226](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5220-L5226)）里，`STRUCTURAL` 和 `JSON_STRUCTURAL` 是语法（强制贪婪），`STRING_BODY` 和 `JSON_STRING` 是负载（正常采样）。状态机 `dsml_decode_tracker_update` 是个「**宽容的识别器而非校验器**」——它只需判断下一个 token 属于语法还是负载，畸形 DSML 仍交给事后解析器处理（[ds4_server.c:5459-5462](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5459-L5462) 的注释明确这一点）。

**CLI 开关**：`--disable-exact-dsml-tool-replay` 关掉精确回放（强制全走规范化），`--tool-memory-max-ids N` 调整表上限（[ds4_server.c:11596-11598](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11596-L11598)、[ds4_server.c:7764](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7764)）。

#### 4.3.4 代码实践

**实践目标**：回答本讲主问题的后半句——「规范化路径何时作为后备」，并用 `--trace` 设计一个能观察到它的实验。

**操作步骤**：

1. 阅读 `should_canonicalize_tool_checkpoint`（[ds4_server.c:9969-9977](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9969-L9977)）与调用点（[ds4_server.c:10885-10897](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10885-L10897)），列出三种「会走规范化」的情形。
2. 设计对照实验（若本地可跑服务器）：
   - **基线**：正常发一个带 `tools` 的 `/v1/chat/completions`，模型产出工具调用；下一轮把工具结果连同历史发回。开 `--trace /tmp/t.txt`，在 trace 里找 `tool checkpoint canonicalized` 或 `tool replay` 事件。
   - **强制后备**：重启服务器并加 `--disable-exact-dsml-tool-replay`，重发同一轮历史。观察 trace 里是否出现规范化/rewrite 事件，以及 prefill 量是否变大。
3. 在 trace 里比对两种情形下「common 前缀长度 / live 长度 / 是否 rebuild」。

**需要观察的现象**：
- 基线（精确回放生效）：trace 显示 `replay` 命中、common 前缀几乎等于整段历史、几乎无额外 prefill。
- 强制后备（规范化生效）：trace 显示 `tool checkpoint canonicalized ... common=... live=...`，且当客户端轻微改写历史（如重排 JSON key）时会触发 `rewrite_from_common` 甚至 `rebuild`，prefill 量上升。

**预期结果**：你能讲清后半句——「规范化在以下情形作为后备：(a) `--disable-exact-dsml-tool-replay`；(b) id 不在记忆里（被淘汰/重启未恢复）；(c) `raw_dsml` 为空。它通过 `canonicalize_tool_checkpoint` 把活 checkpoint 改写成下一轮将渲染的字节，改不动就回退磁盘快照重建后缀」。无法本地运行服务器时，标注「待本地验证」，但仍可凭 trace 事件名与源码完成推演。

#### 4.3.5 小练习与答案

**练习 1**：为什么语法贪婪解码**只**作用于 `STRUCTURAL/JSON_STRUCTURAL`，而放过 `STRING_BODY/JSON_STRING`？

**答案**：语法 token（标签名、属性名、JSON 标点、闭标记）必须**确定**——稍有随机性就可能拼出解析器读不懂的标签，整轮工具调用作废。而参数负载（命令体、文件内容、编辑文本）往往是长文本，对它们强制 `temperature=0` 会触发贪婪采样的「重复退化」——模型反复输出相同片段。所以语法保可解析、负载保创造性，各取所需（[README.md:794-801](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L794-L801) 也强调这一点）。

**练习 2**：`canonicalize_tool_checkpoint` 在字节已经一致时为什么还要跳过 token 层的改写？

**答案**：同一段文本可能有多种合法的 BPE 分词（token 拼写）。活 checkpoint 里的 token 是模型**真实采样**出来的、且 KV 已对齐它们；如果仅因为 canonical 的 token 序列与之不同就改写，会把有效的采样历史换成另一套等价但不同的 token 拼写，既浪费（要重 prefill）又可能丢掉隐藏 reasoning 的采样状态。所以代码先用 `render_tokens_text` 把两边都降到字节层比对，字节一致就放过（[ds4_server.c:9838-9848](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9838-L9848)）。

## 5. 综合实践

把三个模块串起来，追踪一次完整的「工具调用 → 工具结果 → 继续」两轮交互在 ds4-server 内的字节/token 流转，并画出 KV 复用图。

**任务**：假设客户端用 `/v1/chat/completions` 发了带一个 `bash` 工具的请求，模型回了 DSML 工具调用；客户端执行后把工具结果连同历史（含上轮工具调用的 JSON 对象与 `call_xxx` id）再发一轮。请完成：

1. **第一轮（生成）**：标出模型吐出 DSML 时，`dsml_decode_tracker` 在标签处把 `temperature` 强制为 0（模块 3）；生成结束，`parse_generated_message_ex` 抓出 `raw_dsml`（模块 1），`assign_tool_call_ids` 分配 `call_xxx`，`tool_memory_remember` 存进 `by_id`/`by_block`（模块 2）。
2. **第二轮（渲染前）**：标出 `tool_memory_attach_to_messages` 按 `call_xxx` 查到精确块、挂回 `raw_dsml`；`append_dsml_tool_calls_text` 走「逐字节抄写」分支；于是渲染字节 == 活 KV 字节，`ds4_session_common_prefix` 命中，只增量 prefill 工具结果后缀。
3. **降级演练**：重做第二轮，但假设 `call_xxx` 已被 LRU 淘汰。画出 `raw_dsml == NULL` → 规范化重建 → `should_canonicalize` 为真 → `canonicalize_tool_checkpoint` 改写/重建的链路，并指出 prefill 量为何上升。
4. **验证**：用一句话回答本讲主问题（见下），并指出 `tool_replay_stats` 的 `mem/disk/canonical` 三个计数器在 (1)(2) 与 (3) 中分别怎么变。

**主问题（本讲实践任务的原题）**：为什么「精确回放」能避免无状态 API 重发工具调用时 KV 前缀失配？规范化路径何时作为后备？

**参考答案要点**：精确回放把「不可猜测 id」当钥匙、「模型当时采样的 DSML 字节」当值，下一轮原样抄进 prompt，使渲染字节与活 KV checkpoint 字节逐字节相等 → `ds4_session_common_prefix` 命中 → 前缀复用不断、无需重建。规范化作为后备的三种情形：`--disable-exact-dsml-tool-replay`、id 不在记忆（淘汰/重启未恢复）、`raw_dsml` 为空；此时 `canonicalize_tool_checkpoint` 主动把活 checkpoint 改写成下一轮将渲染的字节，改不动则回退磁盘快照重建后缀（u2-l3 的 `rewrite_requires_rebuild` 判定）。

## 6. 本讲小结

- **DSML 是 DeepSeek 的工具调用文本格式**（`<｜DSML｜tool_calls>...`），模型直接以 token 流生成，ds4 用宏定义其骨架（[ds4_server.c:4214-4227](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L4214-L4227)），转义规则只保护闭标签、其余字符原样保留。
- **核心分叉在 `append_dsml_tool_calls_text`**：`raw_dsml` 非空就逐字节抄模型采样、为空才从 JSON 规范化重建（[ds4_server.c:2242-2247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L2242-L2247)）。
- **精确回放用两张 rax**：`by_id`（id→条目）做主查询，`by_block`（字节→块）做去重，加 LRU 双向链表做淘汰，默认上限 100000 id / 512 MiB（[ds4_server.c:7669-7680](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7669-L7680)）。
- **不可猜测 id**（`random_tool_id`，16 字节随机）是精确回放的安全前提，且分配时做碰撞检测（[ds4_server.c:499-519](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L499-L519)、[ds4_server.c:8193-8203](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8193-L8203)）。
- **精确回放可持久化**：磁盘 KV 文件的 KTM 段存 id→DSML 映射，重启后 `kv_cache_restore_tool_memory_for_messages` 扫描恢复（[ds4_server.c:8542-8581](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8542-L8581)）。
- **规范化是后备**：精确块缺失时 `canonicalize_tool_checkpoint` 把活 checkpoint 改写成下一轮将渲染的字节，改不动则回退磁盘快照重建（[ds4_server.c:9821-9864](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9821-L9864)）；Responses 端点因有 `previous_response_id` 绑定而跳过。
- **语法贪婪解码**：生成中 DSML 语法 token 强制 `temperature=0`，参数负载保留正常采样，靠五态识别器区分（[ds4_server.c:10391-10410](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10391-L10410)、[ds4_server.c:5445-5451](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L5445-L5451)）。

## 7. 下一步学习建议

- 接着读 **u7-l5 实时 KV 前缀复用与检查点改写**，看 `ds4_session_common_prefix`、`ds4_session_rewrite_from_common`、`ds4_session_rewrite_requires_rebuild` 如何与本讲的规范化后备无缝衔接——本讲的 `canonicalize_tool_checkpoint` 正是它们的上层调用者。
- 回看 **u2-l3 Session 同步与前缀复用**，把「common/live_len/rebuild」三个量的语义与本讲的 `should_canonicalize` 判定对照，理解为什么「中间改写须重建、末尾追加安全」是贯穿两讲的同一原理。
- 若对磁盘侧感兴趣，读 **u8-l1 KV 缓存文件格式** 与 **u8-l2 KV store 策略与淘汰**，看 KTM 段（[README.md:1062-1089](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1062-L1089)）如何与 KV 快照的冷存/淘汰时机协同，让精确回放跨重启存活。
- 想看工具调用的「产品级」用法，读 **u10-l1 原生编码 Agent 设计** 与 **u10-l2 Agent 工具系统**——`ds4-agent` 直接用 DSML 原生解析工具（不走 JSON 转换），与本讲「服务器把 JSON 翻译回 DSML」形成对照，能加深你对「为什么 agent 架构下 KV mismatch 按构造不可能」的理解。
