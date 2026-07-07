# 实时 KV 前缀复用与检查点改写

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `ds4-server` 在收到一个「把整段对话重发一遍」的无状态请求时，按什么顺序尝试复用内存中的活 KV checkpoint（exact token-prefix → 渲染字节前缀 → 磁盘快照 → 冷 prefill）。
- 区分两套不同入口的前缀复用：请求开始时的 `generate_job`（复用旧 KV 跑新请求）与工具调用结束后的 `canonicalize_tool_checkpoint`（主动把活 checkpoint 改写成下一轮将渲染的字节）。
- 读懂 `ds4_session_common_prefix`、`ds4_session_rewrite_from_common`、`ds4_session_rewrite_requires_rebuild` 三个函数，并能用一句话解释「为什么末尾追加安全、中间改写必须重建」。
- 推断一个「客户端轻微改写历史」的请求会落到哪条路径。

## 2. 前置知识

本讲建立在 u2-l3 与 u7-l1 之上，先回顾两个关键事实：

- **checkpoint 与 checkpoint_valid**。`ds4_session` 内部用 `checkpoint`（KV 当前对应的 token 序列）和 `checkpoint_valid`（这份 KV 是否可信）刻画状态。`ds4_session_sync` 只在 checkpoint 是新 prompt 的严格前缀时增量评估后缀，否则整段重建（见 u2-l3）。
- **单活 KV session**。`ds4-server` 进程内只有一个 engine 和一个可变 `ds4_session`，全局唯一的 graph worker 串行推理。所有前缀复用的目的，就是让无状态客户端「重发整段对话」时不必每次从 token 0 重新 prefill（见 u7-l1）。

本讲要补充的新直觉是：**前缀复用的「前缀」有两种度量——token 级和字节级**。前者最便宜最精确，后者更宽容。两者都不命中时，才会退到磁盘或冷 prefill。

一个容易混淆的点：本讲的「rewrite/rebuild 判定」是一个**独立**的改写入口（在工具调用生成完之后跑），和请求开始时的前缀复用**不是同一段代码**，但它们共用同一组底层函数（`common_prefix`、`rewrite_from_common`）。讲义会严格区分这两个入口。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心。`ds4_session_common_prefix`（量前缀长度）、`ds4_session_rewrite_from_common`（改写活后缀）、`ds4_session_rewrite_requires_rebuild`（判定能否就地改写）都在这里。 |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 公共边界。声明 `ds4_session_rewrite_result` 枚举与上述三个函数。 |
| [ds4_server.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c) | 服务器。`generate_job`（请求入口的前缀复用链）、`live_text_prefix_prompt`（字节前缀命中）、`canonicalize_tool_checkpoint`（工具调用后的改写）都在这里。 |
| [ds4_kvstore.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c) | 磁盘 KV。`ds4_kvstore_byte_prefix_match`（字节前缀比较）、`ds4_kvstore_build_prompt_from_exact_prefix_and_text_suffix`（用活 token + 新文本拼 effective prompt）是字节级命中的实现底座。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 「Disk KV Cache」「Tool call handling and canonicalization」两节是本讲的设计说明。 |

## 4. 核心概念与源码讲解

### 4.1 token-prefix 检查（精确 token 前缀）

#### 4.1.1 概念说明

无状态客户端（典型如 OpenAI 风格的 agent）每次请求都把整段对话重新发一遍。如果服务器这次收到的 prompt token 序列，恰好是活 KV checkpoint token 序列的**严格前缀扩展**——也就是说「活 checkpoint 的每一个 token，都按相同顺序出现在新 prompt 的开头」——那么已经算好的 KV 一行都不用动，只要把新增的后缀 prefill 进去即可。

这是**最便宜、最精确**的命中：它直接比较 token id 数组，不需要任何解码或重新分词，O(n) 一次扫描就结束。服务器给它起的内部标签是 `memory-token`。

注意「token 前缀」要求逐 token 相等，这比「文本前缀」严格得多。模型在生成时采样的某个 token，被客户端解码成文本再重新分词后，可能裂成两个不同的 token（BPE 边界不同）。一旦发生这种「一字两 token」的错位，token 前缀就会在那个位置断开——这正是字节级比对（4.2）要兜的底。

#### 4.1.2 核心流程

请求入口 `generate_job` 在派发推理前，先按固定顺序尝试一系列「命中」。`memory-token` 是其中最朴素的一条：

```text
old_pos = 活 checkpoint 当前长度
common  = ds4_session_common_prefix(session, &req.prompt)   # 逐 token 比对
若 common == old_pos 且 req.prompt.len >= old_pos:
    cached = common            # 命中 memory-token
否则:
    继续尝试下一条命中（字节比对、磁盘、冷 prefill）
```

`ds4_session_common_prefix` 本身极简——在两条 token 序列的公共前缀长度内逐个比较 id，遇到第一个不相等就停：

```c
int ds4_session_common_prefix(ds4_session *s, const ds4_tokens *prompt) {
    if (!s->checkpoint_valid) return 0;
    int n = s->checkpoint.len < prompt->len ? s->checkpoint.len : prompt->len;
    int i = 0;
    while (i < n && s->checkpoint.v[i] == prompt->v[i]) i++;
    return i;
}
```

`checkpoint_valid` 为假时直接返回 0：活 KV 已不可信，谈不上前缀复用。

#### 4.1.3 源码精读

请求入口 `generate_job` 的函数头有一段注释，把整条命中链的设计意图写得非常清楚——值得先读注释再看代码：

> 客户端以文本形式重发完整 prompt。worker 先试旧的精确 token 前缀命中，再试活 checkpoint 的渲染文本前缀命中，再试磁盘文本前缀重启快照，最后才是冷 prefill。

见 [ds4_server.c:9979-9990](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9979-L9990)。

`generate_job` 一进来就算 `common`：

```c
const int old_pos = ds4_session_pos(s->session);
const int common = ds4_session_common_prefix(s->session, &j->req.prompt);
```

见 [ds4_server.c:9994-9995](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9994-L9995)。

`memory-token` 命中判定本身只有一行（在前面若干条 API 专属命中都失败之后才轮到它）：

```c
} else if (cached == 0) {
    cached = common == old_pos && j->req.prompt.len >= old_pos ? common : 0;
    cache_source = cached > 0 ? "memory-token" : "none";
}
```

见 [ds4_server.c:10063-10066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10063-L10066)。两个条件缺一不可：

- `common == old_pos`：活 checkpoint 的**全部** token 都是新 prompt 的前缀（不是部分前缀——部分前缀意味着活 KV 末尾有 token 对不上，不能直接续）。
- `req.prompt.len >= old_pos`：新 prompt 至少和活 checkpoint 一样长（保证是「追加」而非「截断」）。

`common_prefix` 的引擎实现见 [ds4.c:26957-26963](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26957-L26963)。

命中后，`prompt_for_sync` 仍指向原始 `req.prompt`，随后由 `ds4_session_sync` 只 prefill 后缀 `[old_pos, prompt.len)`——这是 u2-l3 讲过的「checkpoint 是前缀就只评估后缀」的分支。

#### 4.1.4 代码实践

**实践目标**：确认「追加一轮对话」会命中 `memory-token`，并算出能省下多少 prefill。

**操作步骤**：

1. 在 [ds4_server.c:10064](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10064) 找到 `memory-token` 那一行，记下 `cache_source` 的取值。
2. 假设活 checkpoint 有 `old_pos = 1200` 个 token（一轮系统提示 + 一轮用户 + 一轮助手回答）。
3. 客户端原样回发这 1200 个 token 渲染出的文本，并在末尾**追加**一条新的用户消息，重新分词后 prompt 共 1380 个 token。
4. 假设重新分词没有发生 BPE 错位，`common` 仍是 1200。

**需要观察的现象**：`common == old_pos` 成立、`prompt.len (1380) >= old_pos (1200)` 成立 → `cached = 1200`，`cache_source = "memory-token"`。

**预期结果**：`ds4_session_sync` 只 prefill 后缀 180 个 token（1380 − 1200），首 token 延迟由「1380 个 token 的 prefill」缩成「180 个 token 的 prefill」。这正是单活 session 前缀复用的全部价值。

> 是否真的命中 `memory-token`、`common` 是否真等于 `old_pos`，取决于客户端文本能否被分词成与活 checkpoint 完全一致的 token——这点**待本地验证**（用 `--trace` 看 `cache_source` 字段最直接，见 README 末尾的 trace 说明）。

#### 4.1.5 小练习与答案

**练习 1**：若 `common = 1000` 而 `old_pos = 1200`，`memory-token` 会命中吗？为什么？

**参考答案**：不会。`common == old_pos` 不成立（1000 ≠ 1200），说明活 checkpoint 的第 1001 个 token 就和新 prompt 对不上，活 KV 末尾有 200 个 token 已经「悬空」，不能直接续。这条请求会落到后面的字节比对或更靠后的命中。

**练习 2**：为什么 `ds4_session_common_prefix` 在 `checkpoint_valid == false` 时直接返回 0？

**参考答案**：`checkpoint_valid` 为假表示活 KV 已经不可信（比如上次 sync 失败、被 invalidate、或刚加载尚未对齐）。此时 checkpoint 里的 token 序列不能代表 KV 的真实状态，任何「前缀」都无意义，于是返回 0 让请求走更靠后的命中乃至冷 prefill。

---

### 4.2 字节比对（渲染文本 vs 解码 checkpoint）

#### 4.2.1 概念说明

token 前缀太严格：模型采样出的某个 token，被客户端解码成文字、再重新分词时，可能裂成两个 token。于是 token 序列在很早的位置就断开了，`memory-token` 错过一次本可以复用的机会。

字节比对是更宽容的第二道：把活 checkpoint 的 token **解码回文本**，再和「客户端这次渲染出的 prompt 文本」做**字节级前缀比较**。只要活 checkpoint 解码出的字节，是请求渲染文本的一个前缀，就算命中。服务器给它起的标签是 `memory-text`。

这里有一个关键原则：**活 graph 是权威（live graph is authoritative）**。命中后，服务器**不**去切片请求的 canonical prompt token（因为整段 prompt 的 BPE 可能在字节边界处发生跨边界合并，切片会得到错误的 token），而是：

- 前缀部分：**原样保留活 checkpoint 的 token**（模型当时真实采样的、KV 已经算好的那一串）；
- 后缀部分：只对请求文本中超出活字节的那一小段**重新分词**。

两段拼成 `effective_prompt`，再交给 `ds4_session_sync`。

#### 4.2.2 核心流程

```text
live_tokens = 活 checkpoint 的 token 序列
live_text   = 把 live_tokens 解码成文本，长度 live_text_len
若 byte_prefix_match(req.prompt_text, live_text):       # 请求文本以活字节为前缀
    effective_prompt = live_tokens                      # 权威前缀，原样保留
                     + tokenize_rendered(req.prompt_text + live_text_len)  # 只对新增文本分词
    cached = live_tokens.len
否则:
    memory-text 未命中，继续往下（磁盘 / 冷 prefill）
```

`byte_prefix_match` 的语义就是「prefix 是 text 的前缀」：

```c
return prefix_len <= text_len &&
       (prefix_len == 0 || memcmp(text, prefix, prefix_len) == 0);
```

#### 4.2.3 源码精读

`live_text_prefix_prompt` 是 `memory-text` 命中的完整实现。注意它的注释点明了「活 graph 是权威」这条原则：

```c
const ds4_tokens *live_tokens = ds4_session_tokens(s->session);
size_t live_text_len = 0;
char *live_text = render_tokens_text(s->engine, live_tokens, &live_text_len);
const size_t prompt_text_len = strlen(req->prompt_text);
if (!byte_prefix_match(req->prompt_text, prompt_text_len,
                       live_text, live_text_len))
{
    free(live_text);
    return 0;                       // 字节前缀不成立 → 未命中
}
/* ...活 graph 是权威：保留它的采样分词，只对之后的请求字节重新分词。
 * 复用 req->prompt 的 token 后缀是错的：整段 prompt 的 BPE 可能在字节边界处合并。*/
build_prompt_from_exact_prefix_and_text_suffix(
    s->engine, live_tokens, req->prompt_text + live_text_len,
    effective_prompt);
return live_tokens->len;
```

见 [ds4_server.c:8846-8871](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8846-L8871)。

`build_prompt_from_exact_prefix_and_text_suffix` 的实现底座在磁盘 KV 模块里，逻辑就是「拷贝权威前缀 + 用渲染聊天分词器 tokenize 后缀文本」：

```c
void ds4_kvstore_build_prompt_from_exact_prefix_and_text_suffix(
        ds4_engine *engine, const ds4_tokens *exact_prefix,
        const char *suffix_text, ds4_tokens *out) {
    ds4_tokens_copy(out, exact_prefix);
    ds4_tokens suffix = {0};
    /* 后缀可能以 <｜User｜> 或 </think> 这类聊天标记开头，
     * 所以用 rendered-chat 分词器，而不是纯文本 BPE。*/
    ds4_tokenize_rendered_chat(engine, suffix_text ? suffix_text : "", &suffix);
    tokens_append(out, &suffix);
    ds4_tokens_free(&suffix);
}
```

见 [ds4_kvstore.c:685-698](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L685-L698)。注释里特意强调后缀要用 `rendered_chat` 分词器（见 u3-l3 的渲染回扫），因为后缀开头往往是聊天控制标记。

`byte_prefix_match` 与底层 `ds4_kvstore_byte_prefix_match`：

```c
bool ds4_kvstore_byte_prefix_match(const char *text, size_t text_len,
                                   const char *prefix, size_t prefix_len) {
    return prefix_len <= text_len &&
           (prefix_len == 0 || memcmp(text, prefix, prefix_len) == 0);
}
```

见 [ds4_kvstore.c:667-671](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L667-L671)，服务器侧薄封装见 [ds4_server.c:8621-8624](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8621-L8624)。

`generate_job` 调用它的位置（在 `memory-token` 之后）：

```c
if (cached == 0) {
    int text_cached = live_text_prefix_prompt(s, &j->req, &effective_prompt);
    if (text_cached > 0) {
        cached = text_cached;
        cache_source = "memory-text";
        prompt_for_sync = &effective_prompt;
    }
}
```

见 [ds4_server.c:10081-10088](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10081-L10088)。注意命中后 `prompt_for_sync` 改指向新拼出的 `effective_prompt`（而非原始 `req.prompt`），这正是「权威前缀 + 新后缀」要喂给 sync 的东西。

#### 4.2.4 代码实践

**实践目标**：构造一个「token 前缀断、字节前缀不断」的场景，验证 `memory-text` 如何救回一次复用。

**操作步骤**：

1. 读 [ds4_server.c:8846-8871](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8846-L8871) 的 `live_text_prefix_prompt`。
2. 假设模型在生成时把 `" don't"` 采成了**一个** token；客户端把它解码成文字 `" don't"` 再重新分词，BPE 把它切成**两个** token。于是 token 序列在这一位错位，`common` 在此处断开，`memory-token` 错过。
3. 但客户端回发的整段文字与活 checkpoint 解码出的文字**逐字节相同**（只是分词方式不同），末尾再追加一条新消息。

**需要观察的现象**：`byte_prefix_match(req.prompt_text, live_text)` 成立 → `memory-text` 命中。`effective_prompt` = 活 checkpoint 的原样 token（含那个「一字一 token」）+ 新追加消息的重新分词。

**预期结果**：尽管 token 前缀断了，服务器仍只 prefill 新追加的那段后缀，活 KV 的主体被完整复用。这就是 README 所说的「A rendered byte-prefix hit can still reuse the checkpoint and tokenize only the new suffix」（见 [README.md:1011-1016](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1011-L1016)）。

> 具体哪个 token 会被「一字两 token」取决于词表，是否真发生**待本地验证**；用 `--dump-tokens`（u3-l3）对比「模型采样序列解码再分词」与「原始序列」可定位错位点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `memory-text` 命中后要用 `effective_prompt`（活 token + 新后缀），而不是直接切片 `req.prompt` 的 token？

**参考答案**：因为整段 prompt 的 BPE 可能在「活字节结尾、新文本开头」这个字节边界处发生跨边界合并。直接切 `req.prompt` 会丢掉这种合并，得到的 token 序列与活 KV 不一致，前缀复用就断了。保留活 checkpoint 的权威 token、只对纯新文本重新分词，才能保证前缀 token 与已算好的 KV 逐 token 对齐。

**练习 2**：`byte_prefix_match(text, prefix)` 里 `prefix_len <= text_len` 这个条件解决了什么边界？

**参考答案**：它要求「活解码字节（prefix）不得超过请求文本（text）的长度」。若活 checkpoint 比请求还长（例如客户端截断了一段历史），活字节就不可能是请求的前缀，自然不命中——避免 `memcmp` 越界，也正确表达「追加才安全」的语义。

---

### 4.3 rewrite/rebuild 判定

#### 4.3.1 概念说明

前两节讲的是**请求开始时复用旧 KV**。本节讲另一个独立入口：**工具调用生成完之后，主动把活 checkpoint 改写成「下一轮客户端请求将会渲染出的字节」**，这叫**规范化（canonicalization）**。

为什么需要它？DeepSeek V4 用 DSML 文本格式发工具调用（见 u7-l4）。客户端下一轮不会把那段 DSML 原样发回，而是发归一化的 OpenAI/Anthropic JSON 工具对象。服务器再把这些 JSON 渲染回 DSML 时，哪怕只差一个空格、一个 key 顺序，渲染出的字节就不再等于模型当时采样的字节——于是下一轮 `memory-token` / `memory-text` 都会在工具调用处断开，整段后缀被迫重 prefill。

规范化的思路：**趁现在（活 KV 还在）就把活 checkpoint 对齐到下一轮将渲染的字节**。它先做一次「字节级」体检——如果活 checkpoint 解码出的字节已经等于将渲染的字节，说明只是 BPE 拼写不同、KV 本身没问题，那就**什么都不改**（避免把一份合法的采样历史换成另一种 BPE 拼写）。否则才尝试就地改写后缀。

而「能否就地改写」由一个极小的纯函数判定：`ds4_session_rewrite_requires_rebuild`。它的核心立论是：

> **末尾追加是安全的，回退到活末端之内的某个中间位置是不安全的。**

原因在 u4-l2 讲过：压缩 KV 的 compressor frontier（压缩前沿）是流式累积的、不可廉价回退。一旦要在活末端**之内**改写，raw SWA 行、压缩 KV 行、indexer 行、compressor frontier 全都越过了共享前缀，无法就地还原——只能要么恢复一个更旧的 checkpoint（磁盘），要么整段重建。

#### 4.3.2 核心流程

规范化入口 `canonicalize_tool_checkpoint`（仅在精确回放不可用时才跑，见 u7-l4）：

```text
canonical = tokenize(本轮 prompt_text + 工具调用后缀)         # 下一轮将渲染的 token
common    = ds4_session_common_prefix(session, &canonical)

若 common == live_len 且 canonical.len == live_len: 走 done    # 完全一致

# 字节级体检：活 KV 解码出的字节 == 将渲染的字节？
live_text = decode(session.checkpoint)
若 live_text == rendered_bytes: 走 done                        # 只是 BPE 拼写不同，KV 没问题

若 common < prompt.len: 走 done                                # 共享前缀太短，放弃改写

rr = ds4_session_rewrite_from_common(session, &canonical, common)
  ├─ common == checkpoint.len → ds4_session_sync(追加后缀) → REWRITE_OK
  └─ 否则 → rewrite_requires_rebuild?
              ├─ 是 → REBUILD_NEEDED
              └─ 否 → （当前实现到不了这里）

对 REBUILD_NEEDED:
  ├─ 先试磁盘 kv_cache_try_load_text(rendered_bytes)         # 找更旧但匹配的快照
  │    命中 → invalidate 活 session → 从磁盘 sync（只 replay 后缀）
  └─ 否则 → 从 canonical 整段 sync（replay 整条尾巴）
```

判定函数本身只有三行：

```c
bool ds4_session_rewrite_requires_rebuild(int live_len, int canonical_len, int common) {
    if (live_len < 0 || canonical_len < 0 || common < 0) return true;
    if (common > live_len || common > canonical_len) return true;
    return common < live_len;
}
```

用数学语言说，设活 checkpoint 长度为 \(L\)，canonical prompt 长度为 \(C\)，共享前缀长度为 \(k \):

- 追加安全（不需重建）：\(k = L\)，即 canonical 把整条活 checkpoint 当作前缀，只在末尾往后接。
- 必须重建：\(k < L\)，即 canonical 在活末端**之内**的某处就分叉了，活 KV 越过 \(k\) 的部分（raw/compressed/indexer 行与 compressor frontier）无法就地还原。

#### 4.3.3 源码精读

判定函数及其注释（注释把「为什么中间改写不行」讲得很透）见 [ds4.c:26897-26908](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26897-L26908)。注释明确：checkpoint 不只是 token 向量，后端状态还含 raw SWA 行、压缩 KV 行、indexer 行、compressor frontier；替换活末端之内的任何部分都要先还原整条 frontier，而「在活末端精确延伸是安全的，回退到它之后则不是就地操作」。

改写函数 `ds4_session_rewrite_from_common` 的两个关键分支：

```c
if (common == s->checkpoint.len) {
    return ds4_session_sync(s, prompt, err, errlen) == 0 ?
        DS4_SESSION_REWRITE_OK : DS4_SESSION_REWRITE_ERROR;
}

if (ds4_session_rewrite_requires_rebuild(s->checkpoint.len, prompt->len, common)) {
    snprintf(err, errlen, "rewrite needs rebuild: common=%d live=%d canonical=%d",
             common, s->checkpoint.len, prompt->len);
    return DS4_SESSION_REWRITE_REBUILD_NEEDED;
}
```

见 [ds4.c:26942-26951](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26942-L26951)。注意它**先**校验前缀 token 真的一致（[ds4.c:26935-26940](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26935-L26940)），再分两种情况：`common == checkpoint.len` 就是「纯追加」，直接 `ds4_session_sync`（落到 u2-l3 的前缀分支，只 prefill 后缀）；否则判定为需要重建，**不改动 session**直接返回 `REBUILD_NEEDED`。函数头注释点明：在拿到真正的 frontier 快照之前，任何活末端之内的替换都只报告「需要重建」而不动手；服务器仍可能在回退到完整 replay 之前，先找到一个更旧的磁盘 KV checkpoint（见 [ds4.c:26910-26919](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26910-L26919)）。

返回值的枚举定义：

```c
typedef enum {
    DS4_SESSION_REWRITE_ERROR = -1,
    DS4_SESSION_REWRITE_OK = 0,
    /* 活后端状态无法就地安全改写。调用方应先恢复一个更旧的 checkpoint，
     * 再 sync 到 prompt。*/
    DS4_SESSION_REWRITE_REBUILD_NEEDED = 1,
} ds4_session_rewrite_result;
```

见 [ds4.h:238-244](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L238-L244)。

服务器侧的调用者 `canonicalize_tool_checkpoint` 处理三种结果。先做字节级体检（若活解码字节已等于将渲染字节，直接 `done`，不折腾 token）：

```c
char *live_text = render_tokens_text(s->engine, ds4_session_tokens(s->session), &live_text_len);
if (live_text_len == rendered.len &&
    (live_text_len == 0 || memcmp(live_text, rendered.ptr, live_text_len) == 0))
{
    /* graph 已经代表了下一轮将渲染的字节。token 级规范化只会把合法的采样历史
     * 换成同一转录的另一种 BPE 拼写。*/
    free(live_text);
    goto done;
}
```

见 [ds4_server.c:9838-9849](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9838-L9849)。

真正调改写函数的那段，以及 `REBUILD_NEEDED` 时的两级回退（先磁盘 `kv_cache_try_load_text`，再退到完整 replay）：

```c
ds4_session_rewrite_result rr =
    ds4_session_rewrite_from_common(s->session, &canonical, common, err, sizeof(err));
if (rr == DS4_SESSION_REWRITE_OK) {
    /* ...记一条 canonicalized 日志...*/
} else if (rr == DS4_SESSION_REWRITE_REBUILD_NEEDED) {
    /* 生成的 DSML 后缀与 canonical prompt 共享前缀，但生成的尾巴太大，
     * 无法在活的 raw-window 环里安全覆盖。优先用一个更旧的磁盘 checkpoint，
     * 而不是从 token 0 重放一长段对话。*/
    int loaded = kv_cache_try_load_text(s, rendered.ptr ? rendered.ptr : "",
                                        &effective, &path, NULL, false);
    if (loaded == 0) ds4_session_invalidate(s->session);
    const ds4_tokens *sync_prompt = loaded > 0 ? &effective : &canonical;
    /* ...ds4_session_sync(sync_prompt) 只 replay 后缀（磁盘命中）或整条尾巴...*/
}
```

见 [ds4_server.c:9858-9926](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9858-L9926)（`REBUILD_NEEDED` 分支主体在 [ds4_server.c:9869-9954](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9869-L9954)）。注意磁盘命中后 `ds4_session_invalidate` 把不可信的活 KV 作废，再由 sync 从磁盘快照续算；磁盘也没命中时 `sync_prompt = &canonical`，等于从 canonical 整段重建。

回归测试把判定函数的边界钉得很死：

```c
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(19296, 19290, 19081));  // common < live → 重建
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(1024, 1030, 1000));     // common < live → 重建
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(1024, 900, 900));       // common < live → 重建

TEST_ASSERT(!ds4_session_rewrite_requires_rebuild(1024, 1024, 1024));    // common == live → 追加
TEST_ASSERT(!ds4_session_rewrite_requires_rebuild(1024, 1100, 1024));    // common == live → 追加更长
```

见 [ds4_server.c:14718-14731](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L14718-L14731)。前三行都是 \(k < L\)（在活末端之内分叉）→ 必须重建；后两行都是 \(k = L\)（活 checkpoint 整段是 canonical 的前缀）→ 只追加、不重建。这与 4.3.2 的数学表述完全对应。

#### 4.3.4 代码实践

**实践目标**：用 `rewrite_requires_rebuild` 的判定逻辑，推断「客户端轻微改写历史」会走哪条路径。

**操作步骤**：

1. 设想客户端有一个三轮对话：系统提示 + 用户 A + 助手答 + 用户 B + 助手答，活 checkpoint 共 `old_pos = 2000` 个 token。
2. 客户端在**下一轮**做了两件事之一：
   - **(a) 纯追加**：原样回发这 2000 token 对应的文字，末尾追加新的「用户 C」。
   - **(b) 轻微改写**：把**最早**的「用户 A」里一个错别字改掉，其余不变，再追加「用户 C」。
3. 对照 [ds4_server.c:10063-10066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10063-L10066)（`memory-token`）、[ds4_server.c:8846-8871](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8846-L8871)（`memory-text`）与 [ds4.c:26904-26908](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26904-L26908)（rebuild 判定），分别推演两条路径。

**需要观察的现象**：

- **(a) 纯追加**：token 序列不变，`common = 2000 = old_pos`，`prompt.len > old_pos` → 命中 `memory-token`，`cached = 2000`，只 prefill 新增的「用户 C」后缀。
- **(b) 轻微改写**：改写发生在很靠前的「用户 A」。token 序列在那里就分叉，`common` 远小于 `old_pos`（比如 `common = 50`）。
  - `memory-token`：`common == old_pos` 不成立 → 不命中。
  - `memory-text`：`byte_prefix_match` 要求活解码字节是请求文本的前缀。但请求文本开头就是**改写后**的「用户 A」，活 checkpoint 解码出的是**改写前**的「用户 A」→ 不是前缀 → 不命中。
  - 于是走到 [ds4_server.c:10089-10095](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10089-L10095) 的 `live kv cache miss` 警告。
  - 若开了磁盘 KV 且 `old_pos >= min_tokens`，先 `kv_cache_store_current(s, "evict")` 把旧会话落盘（[ds4_server.c:10097-10102](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10097-L10102)）。
  - 再 `kv_cache_try_load` 在磁盘找「渲染文本是本次请求前缀」的 `.kv` 文件（[ds4_server.c:10103-10112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10103-L10112)）。本次请求以改写后的「用户 A」开头，旧快照以改写前的「用户 A」开头 → 不是前缀 → 磁盘也 miss。
  - 最终落到**冷 prefill**：从 token 0 重算整段。

**预期结果**：

| 场景 | common | memory-token | memory-text | disk-text | 最终 |
| --- | --- | --- | --- | --- | --- |
| (a) 纯追加 | 2000 | ✅ 命中 | — | — | 只 prefill 后缀 |
| (b) 改写早期历史 | ≈50 | ❌ | ❌ | ❌ | **冷 prefill 全段** |

结论一句话：**前缀复用只奖励「末尾追加」，惩罚「中间改写」**——因为 token、字节、磁盘三层都按「共享前缀」来命中，越靠前的改动越早打断前缀，复用价值归零。这也正是 u7-l4 要费大力气做「精确 DSML 回放」的根本动机：让工具调用那一轮的字节逐字节稳定，保住前缀不断。

> 实际请求会落到哪条命中取决于真实分词与磁盘状态，**待本地验证**（`--trace` 会打出 `cache_source` 与 cache miss 原因）。

#### 4.3.5 小练习与答案

**练习 1**：`ds4_session_rewrite_from_common` 在 `common == checkpoint.len` 时直接调 `ds4_session_sync`，为什么这是安全的就地操作？

**参考答案**：`common == checkpoint.len` 意味着 canonical prompt 把整条活 checkpoint 当作前缀，分叉点正好在活末端**之外**。这只是「在末尾往后接」，不涉及回退任何已写好的 raw/compressed/indexer 行，也不碰 compressor frontier——u4-l2 的流式压缩状态只往前累积，所以追加天然安全，直接交给 `ds4_session_sync` 走前缀分支即可。

**练习 2**：`canonicalize_tool_checkpoint` 在调用 `rewrite_from_common` 之前，先做了一次「活解码字节 == 将渲染字节」的体检（[ds4_server.c:9840-9848](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9840-L9848)）。如果省掉这次体检、直接做 token 级改写，会出什么问题？

**参考答案**：会出现「token 序列不同、但解码字节相同」的假失配——模型采样的某段文本与 canonical 重新分词的文本，文字完全一样、只是 BPE 切法不同。若直接做 token 级改写，会把一份合法的、KV 已算好的采样历史，换成同一转录的另一种 BPE 拼写，白白触发 rebuild/replay。字节级体检识别出这种情况并直接 `done`，避免无谓的重建。

**练习 3**：为什么 `REBUILD_NEEDED` 时服务器「优先找磁盘旧快照」而不是「直接整段 replay」？

**参考答案**：见 [ds4.c:26910-26919](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26910-L26919) 与 [ds4_server.c:9869-9878](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9869-L9878) 的注释：生成的 DSML 尾巴太大，无法在活的 raw-window 环里就地覆盖。但磁盘上可能存着一个「渲染文本也是本次请求前缀」的更旧 checkpoint——从它续算只需 replay 一小段后缀，远比从 token 0 重放一长段对话便宜。磁盘命中失败时才退到完整 replay。

---

## 5. 综合实践

把三个模块串起来，画出 `ds4-server` 处理一个无状态 chat 请求的**完整前缀复用决策图**，并标注每个判定点对应的源码行。

**任务**：

1. 从 [ds4_server.c:9991](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9991) 的 `generate_job` 入口出发，按代码顺序列出所有「命中尝试」，每条标注：判定条件、命中的 `cache_source` 标签、命中后 `prompt_for_sync` 指向什么。
2. 在图上单独标出 `memory-token`（[ds4_server.c:10064](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10064)）与 `memory-text`（[ds4_server.c:10082](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10082)）这两条，说明它们各自用「token」还是「字节」度量前缀。
3. 在图末尾画一条「全 miss」分支，写出它依次做的事：`cache miss` 警告 → `kv_cache_store_current("evict")` 落盘旧会话 → `kv_cache_try_load` 找磁盘前缀 → 冷 prefill。
4. 在图旁边补一个**对照框**：`canonicalize_tool_checkpoint`（[ds4_server.c:9821](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9821)）是另一个独立入口，说明它在「何时」跑（工具调用生成完之后）、用 `rewrite_from_common` 做什么、`REBUILD_NEEDED` 时如何两级回退。

**自检问题**（答案在 4.3.4）：若客户端只把最早一条用户消息改了一个字，你的决策图会把请求导到哪个叶子？为什么 `memory-token`、`memory-text`、`disk-text` 三层全部落空？

> 提示：三层命中都基于「共享**前缀**」。改得越靠前，前缀断得越早；改在最开头，三层前缀同时归零，必然冷 prefill。

## 6. 本讲小结

- `ds4-server` 对无状态「重发整段对话」请求，按 **exact token-prefix（`memory-token`）→ 渲染字节前缀（`memory-text`）→ 磁盘文本前缀（`disk-text`）→ 冷 prefill** 的顺序逐级尝试命中，越靠前越便宜。
- `memory-token` 用 `ds4_session_common_prefix` 逐 token 比较，命中条件是 `common == old_pos && prompt.len >= old_pos`——只奖励「纯追加」。
- `memory-text` 把活 checkpoint 解码成文本再做字节前缀比较，能救回「一字两 token」式的 token 失配；命中后**活 graph 是权威**：保留活 token、只对新增文本重新分词，避免 BPE 跨边界合并错位。
- **请求入口的前缀复用**（`generate_job`）与**工具调用后的规范化改写**（`canonicalize_tool_checkpoint`）是两个独立入口，但共用 `common_prefix` / `rewrite_from_common` 底层函数。
- `ds4_session_rewrite_requires_rebuild` 的全部逻辑就是 `common < live_len`：**末尾追加安全（\(k = L\)）、中间改写必须重建（\(k < L\)）**，根因是压缩 KV 的 compressor frontier 不可廉价回退。
- `REBUILD_NEEDED` 时服务器**不改动 session**，而是两级回退：先找「渲染文本也是本次请求前缀」的磁盘旧快照只 replay 后缀，磁盘也 miss 才从 canonical 整段重建。
- 一句话总览本讲义覆盖的最小模块：**token-prefix 检查（精确 token 前缀）、字节比对（渲染文本 vs 解码 checkpoint）、rewrite/rebuild 判定（canonicalize + requires_rebuild）**。

## 7. 下一步学习建议

- 本讲的磁盘命中 `kv_cache_try_load` 与 `kv_cache_store_current("evict")` 只是「调用点」；磁盘 KV 的**文件格式**（KVC 头、渲染文本、DS4 payload、KTM 段）与**保存/淘汰策略**（cold/continued/evict/shutdown、对齐裁剪、命中半衰期）在 u8（磁盘 KV 缓存与序列化）单元详解，建议接着读 **u8-l1 磁盘 KV 缓存文件格式** 与 **u8-l2 KV store 策略与淘汰**。
- 本讲的规范化改写是 u7-l4「精确 DSML 回放」失灵时的**后备路径**；若你还没读，回头读 **u7-l4 工具调用：DSML、精确回放、规范化** 能补全「为什么要费大力气保住前缀不断」的动机。
- 想理解「为什么 compressor frontier 不能廉价回退」这一根本约束，读 **u4-l2 KV 缓存设计：滑动窗口 + 压缩/indexer**。
- 想验证本讲的路径推断，最快的工具是 `ds4-server --trace`（见 [README.md:1258](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md#L1258) 附近），它会写出渲染后的 prompt 与每次请求的 cache 决策（含 `cache_source` 与 cache miss 原因）。
