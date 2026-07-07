# KV store 策略与淘汰

## 1. 本讲目标

u8-l1 讲清了 `.kv` 文件的**字节长什么样**；本讲接着回答两个更上层的问题：**什么时候往磁盘上写一个 `.kv` 文件**，以及**当磁盘预算吃紧时删掉哪一个**。读完本讲，你应该能够：

1. 说出 `ds4-server` 在生命周期里写检查点的**四种时机**（cold / continued / evict / shutdown），以及它们各自的触发条件和「原因码（reason code）」如何被写进 KVC 固定头第 5 字节。
2. 解释 cold / continued 保存为什么要先**裁掉一小段尾部 token**、再**向下对齐到 prefill chunk 边界（默认 2048）**，并能把这套边界对齐规则与压缩 KV 的 compressor frontier（u4-l2、u6-l1）串起来。
3. 读懂**淘汰评分公式** `score = (decayed_hits + 1) × tokens / file_size`，说清命中计数的 6 小时半衰期、anchor 原因的 2 倍保护、以及「被新检查点取代的 continued 路标」为什么是廉价牺牲品。
4. 把 README 的 `Disk KV Cache` 文字描述与 `ds4_kvstore.c` 的存/取/淘汰函数逐条对上号。

本讲只讲**策略层**：何时存、存多长、删谁。它不重复 u8-l1 的文件字节布局，也不展开 DS4 payload 内部序列化（那是 u8-l3）。

## 2. 前置知识

进入策略层前，先用几句话回收几个前置概念（细节见对应讲义）：

- **单活 KV session**：进程内同时只有一条活的 `ds4_session`。当无状态客户端重发整段对话、或换会话、或服务器重启时，旧 checkpoint 只能靠磁盘 `.kv` 文件续命（u7-l1）。
- **前缀复用的代价**：复用一个磁盘检查点，要求它的「渲染文本」正好是新提示的字节前缀（u8-l1）。一旦边界对不齐（最常见原因是 BPE 跨边界合并），前缀复用就断在半路，后缀要重 prefill（u7-l5）。
- **chunked prefill 与 compressor frontier**：长 prompt 被切成 chunk（默认 4096 / PRO 8192）逐块 prefill；每个 chunk 边界是压缩行最终化、KV checkpoint 推进、logit 写出的天然落点（u4-l2、u6-l1）。本讲会反复用到「边界 = 2048 的整数倍」这个事实。
- **reason byte**：KVC 固定头第 5 字节存「这份检查点是在什么情况下写的」（u8-l1），取值正是本讲的 7 个原因码之一。

一句话定位：**磁盘 KV 缓存是一份带预算、带淘汰的「前缀快照仓库」**——它必须在「写得多」与「写得巧」之间取舍：写得太碎会塞满磁盘且无法复用，写得太少又让重启/换会话失去续算机会。本讲就是这套取舍的源码。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ds4_kvstore.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h) | 定义 reason 枚举、`ds4_kvstore_options`（五个策略旋钮）、`ds4_kvstore`（含预算 `budget_bytes` 与游标 `continued_last_store_tokens`）、`ds4_kvstore_eviction_context`，以及常量 `DS4_KVSTORE_HIT_HALF_LIFE_SECONDS`、`DS4_KVSTORE_DEFAULT_MB`。 |
| [ds4_kvstore.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c) | 策略实现：默认值宏、`store_len`（裁剪+对齐）、`continued_store_target`/`note_store`（continued 节奏）、`entry_eviction_score`/`evict`（评分淘汰）、`store_live_prefix_text`（存盘主流程）、`maybe_store_continued`、`try_load_text`（含 consume-on-load）。本讲主战场。 |
| [ds4_server.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c) | 四种时机的**调用方**：cold 在请求 prefill 前（约 10196 行）、continued 在 prefill 进度回调与 sync 后（约 8797/10271 行）、evict 在加载磁盘快照替换活 session 前（约 10101 行）、shutdown 在干净退出前（约 11837 行）。 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | `Disk KV Cache` 一节（约 990–1172 行）给出四种时机、对齐裁剪、各旋钮的官方说明，是本讲的对照基准。 |

> 范围提示：`ds4_agent.c` 也复用同一套格式，但只用 `agent-system` / `agent-session` 两个 reason，策略由 agent 自己管（u10-l1）。本讲聚焦 server 的四个时机。

## 4. 核心概念与源码讲解

### 4.1 保存时机与原因码

#### 4.1.1 概念说明

「磁盘 KV 缓存」不是每次推理都写，而是在**四个有意义的时刻**写一份检查点。这四个时刻各有动机：

- **cold（冷存）**：一条新对话的第一段长 prompt prefill 完、**尚未开始生成**时，把这段「稳定的系统提示 + 第一个用户问题前缀」存下来。动机是——agent 客户端最常重发的就是这一大段固定前缀，存一次能让成百上千次后续请求都命中。
- **continued（续存）**：在一次很长的 prefill 或很长的生成过程中，每当活 session 自然推进到一个**绝对对齐的前沿**（默认约每 10240 token），就留一个「重启点」。动机是——长任务中途崩了/重启了，能从最近的续存点续算，而不必从 cold 点重跑一大段。
- **evict（换出存）**：当一个**无关的新请求**要加载磁盘快照、从而替换掉当前活 session 之前，先把当前活 checkpoint 落盘。动机是——内存里只有一条活 session，被换掉之前若不落盘，这条会话状态就永久丢了。
- **shutdown（关停存）**：服务器**干净退出**前，把当前活 checkpoint 落盘。动机同上，只是触发时机是关机而非换会话。

每个时刻写出的 `.kv` 文件，都在固定头第 5 字节记一个**原因码**（u8-l1）。原因码不只是日志装饰：它直接参与淘汰评分（见 4.3）。

#### 4.1.2 核心流程

四种时机的写入，最终都汇聚到同一个函数 `ds4_kvstore_store_live_prefix_text`（4.2 详述），只是传入的 `reason` 字符串不同。reason 字符串经 `ds4_kvstore_reason_code` 折成 `u8` 写进头：

| 字符串 reason | 枚举值 | 触发点（ds4_server.c） | 典型 `store_len` |
|------|------|------|------|
| `"cold"` | 1 | prefill 前，prompt 长度落在 `[min, cold_max]`（约 10196 行） | `store_len` 裁剪+对齐后的值 |
| `"continued"` | 2 | prefill 进度回调（约 9660/9701 行）与 sync 后（约 10271 行） | 命中绝对前沿的精确 token 数 |
| `"evict"` | 3 | 加载磁盘快照替换活 session 之前（约 10101 行） | 当前活 session 全长 |
| `"shutdown"` | 4 | 干净退出前（约 11837 行） | 当前活 session 全长 |
| `"agent-system"` / `"agent-session"` | 5 / 6 | ds4-agent（u10-l1），本讲不展开 | 会话全长 |

整体写入决策（伪代码）：

```
请求到来
 ├─ 若需要加载磁盘快照替换活 session：
 │     先 store_current(reason="evict")   # 救活当前会话
 ├─ prefill 新 prompt（chunk by chunk）
 │     ├─ 首块前若 prompt ∈ [min, cold_max]：
 │     │     计算 cold_store_len（裁剪+对齐）
 │     │     sync 到该前缀 → store(reason="cold")
 │     └─ 每个块/每步 decode 后：
 │           maybe_store_continued()        # 命中前沿才真正写
 ├─ 生成完成
 └─ 服务器收到关停信号：
       join worker → store_current(reason="shutdown")
```

#### 4.1.3 源码精读

**reason 枚举**定义在头文件，七个取值一一对应字符串：

[ds4_kvstore.h:20-28](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L20-L28) —— `ds4_kvstore_reason` 枚举：`UNKNOWN=0 / COLD=1 / CONTINUED=2 / EVICT=3 / SHUTDOWN=4 / AGENT_SYSTEM=5 / AGENT_SESSION=6`。

字符串到枚举的映射在：

[ds4_kvstore.c:174-183](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L174-L183) —— `ds4_kvstore_reason_code(reason)` 把 `"cold"`/`"continued"`/`"evict"`/`"shutdown"`/`"agent-system"`/`"agent-session"` 折成枚举值，不认识的字符串回落 `UNKNOWN`。

**cold 时机**在 server 主链路里是一段带保护的逻辑：

[ds4_server.c:10196-10249](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10196-L10249) —— 仅当 `cached==0`（前缀复用没命中）、`prompt_len >= min_tokens`、`cold_max_tokens>0` 且 `prompt_len <= cold_max_tokens` 时才考虑 cold 存；`cold_store_len` 取「聊天锚点（见 4.2.3）」与「`store_len` 裁剪值」中的较大者；随后把活 session `sync` 到这个前缀再以 `"cold"` 落盘。

**continued 时机**由 server 侧薄封装 `kv_cache_maybe_store_continued` 驱动：

[ds4_server.c:8797-8806](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L8797-L8806) —— 取活 session token 数，问 `continued_store_target` 该不该写，该写就以 `"continued"` 落盘并 `note_store` 记录进度。它在 prefill 进度回调（约 9660、9701 行）和每次 sync 之后（10271 行）被调用，所以「长 prefill」与「长生成」两条路径都会触发。

**evict 时机**：加载磁盘快照会顶掉活 session，所以先救活它：

[ds4_server.c:10097-10102](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10097-L10102) —— 当 `cached==0`（即没命中任何活/文本前缀）、`old_pos >= min_tokens` 时，先 `kv_cache_store_current(s, "evict")` 把当前活 checkpoint 落盘，再去 `kv_cache_try_load` 加载磁盘快照。

**shutdown 时机**在主线程关停流程里：

[ds4_server.c:11832-11838](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11832-L11838) —— `pthread_join(worker)` 确保 graph worker 停下后，若活 session token 数 `>= min_tokens`，以 `"shutdown"` 落盘再释放资源。

#### 4.1.4 代码实践

**实践目标**：在不开服务器的情况下，仅靠源码确认「四种 reason 各自唯一的写入入口」，建立「策略 = 字符串 reason 传参」的心智模型。

**操作步骤**：

1. 在 `ds4_server.c` 中搜索字符串 `"cold"`、`"continued"`、`"evict"`、`"shutdown"`，确认它们各自只出现在一处 `store_current` / `store_live_prefix` / `maybe_store_continued` 调用里。
2. 在 `ds4_kvstore.c` 中搜索 `reason)` 形参，确认所有写入最终都经 `ds4_kvstore_store_live_prefix_text`（4.2.3）。
3. 用 `grep -n "reason_code" ds4_kvstore.c` 找到 `reason_code` 把字符串折成 `u8` 的那一行，确认它会进 `fill_header` 的 `reason` 参数。

**需要观察的现象**：四个 reason 字符串在 server 里各只有**一个**写入入口；它们在 kvstore 层**汇流成同一个函数**，区别只是头里第 5 字节不同。

**预期结果**：你会得到一张「字符串 reason → server 行号 → 枚举值」的对照表，与本节 4.1.2 的表格一致。

**待本地验证**：若你的仓库 HEAD 与本讲不同，行号可能偏移，请以实际 `grep` 结果为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `evict` 和 `shutdown` 存的是「当前活 session 全长」，而 `cold` 存的是「裁剪+对齐后的前缀」？

**参考答案**：`evict`/`shutdown` 的目的是「救活当前会话状态，别让它随内存 session 消失」，所以越完整越好；`cold` 的目的是「给未来一堆重发同一前缀的请求当命中点」，所以要刻意停在**稳定且对齐**的前缀上（见 4.2），存全长反而会因为尾部那几个易漂移的 token 而频繁失配。

**练习 2**：`agent-system` 与 `agent-session` 这两个 reason 走的是不是同一份落盘代码？

**参考答案**：是。它们在 `ds4_agent.c` 里也调用 `ds4_kvstore` 的同一套存盘函数（u10-l1），区别仅在 reason 字符串与缓存键（系统提示按 sysprompt 路径、会话按标题哈希）。

---

### 4.2 对齐与裁剪

#### 4.2.1 概念说明

cold 与 continued 保存不直接用「当前活 session 的全长 token 数」，而是先做两道处理：

- **裁剪（trim）**：从尾部砍掉一小段（默认 32 token）。
- **对齐（align）**：把结果**向下**对齐到 prefill chunk 边界（默认 2048）的整数倍。

这两步共同保证：**存盘点 = 一个「天然 prefill chunk 边界」**。为什么必须如此？有两层原因，分别对应两种失效：

1. **BPE 边界合并**：分词器会把「前缀最后一个 token + 后缀第一个 token」合并成一个新 token。如果你精确缓存到位置 N，而未来某个请求的 prompt 在 N 附近因合并而 tokenize 出不同的 token，那么字节级前缀匹配会在 N 处断开，缓存作废。砍掉尾部 32 token 给出安全余量——把缓存点退到一个「远离边界」的位置。
2. **compressor frontier 半成品**：压缩 KV 的压缩行是在每个 `ratio` 块攒满后才最终化的（u4-l2）；而 prefill chunk 边界（2048）天然是这些块边界的高倍数。只有在 chunk 边界落盘，存的压缩行状态才与「从零冷 prefill 同一文本」**逐位相同**；否则你会存下一个「半攒满的 frontier」，它无法廉价回退、也无法与一次全新 prefill 对齐，复用时就出错（这也是 u7-l5 里「中间改写必须重建」的根因）。

continued 保存则用一套对偶逻辑：它把「续存间隔」（默认 10000 token）**向上**对齐到 2048 的整数倍，得到约 10240，所以 continued 只在 10240、20480、30720… 这些绝对前沿落盘——与 cold 落在**同一张 2048 网格**上。

#### 4.2.2 核心流程

cold 的 `store_len`（裁剪+对齐）逻辑：

```
store_len(tokens):
    if tokens > min_tokens + trim:          # 544
        stable = tokens - trim              # 砍 32 尾
        if align > 0:
            stable -= stable % align        # 向下对齐到 2048
        if stable >= min_tokens:
            return stable
    return tokens                           # 太短就不处理
```

continued 的节奏由三步决定：

```
continued_step():
    step = continued_interval_tokens        # 10000
    step = ceil(step / align) * align       # 向上对齐到 2048 → 10240

continued_store_target(live_tokens):
    if live_tokens < min_tokens:        return 0
    if live_tokens % step != 0:         return 0   # 只在绝对前沿
    if live_tokens <= continued_last_store_tokens: return 0  # 只前进
    return live_tokens
```

数值演算（默认参数，min=512、trim=32、align=2048、continued_interval=10000）：

- cold 存一个 12000 token 的 prompt：`12000-32=11968`，`11968 % 2048 = 1728`，`stable = 11968-1728 = 10240`。落盘点 **10240**（正好等于一个 continued step）。
- continued 在 live=10240 时触发一次，live=11000 时不触发（`11000 % 10240 ≠ 0`），live=20480 时再触发一次。

两套对齐共享 2048 网格，所以 cold 落点一定是某个 continued 可能落点的子集——这让 cold 与 continued 检查点之间天然兼容。

#### 4.2.3 源码精读

默认值宏集中在一处，注释本身就解释了动机：

[ds4_kvstore.c:33-42](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L33-L42) —— `KV_CACHE_DEFAULT_MIN_TOKENS=512`、`COLD_MAX_TOKENS=30000`、`BOUNDARY_TRIM_TOKENS=32`、`BOUNDARY_ALIGN_TOKENS=2048`、`CONTINUED_INTERVAL_TOKENS=10000`。上方注释（35–39 行）明确说：「2048 对齐也匹配后端 prefill chunk 调度，使压缩行最终化与一次冷全量 prefill 完全相同」。

`ds4_kvstore_store_len` 是 cold 裁剪+对齐的全部实现，极短：

[ds4_kvstore.c:700-709](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L700-L709) —— `ds4_kvstore_store_len(tokens)`：`stable = tokens - trim; stable -= stable % align;` 若结果仍 `>= min_tokens` 则返回之，否则原样返回 `tokens`（不处理短前缀）。

cold 还有一个更聪明的「聊天锚点」优先策略：

[ds4_kvstore.c:711-728](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L711-L728) —— `ds4_kvstore_chat_anchor_pos` 找到「第一个 assistant 标记之前的最后一个 user 标记」位置，作为稳定聊天前缀的天然落点（系统提示 + 脚手架 user 段）。server 在 [ds4_server.c:10203-10207](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10203-L10207) 取 `max(anchor, store_len)`，能落在锚点就落锚点，否则退回 `store_len`。

continued 的「向上对齐 + 仅前沿 + 仅前进」三连：

[ds4_kvstore.c:730-748](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L730-L748) —— `kv_cache_continued_step` 用 `((step+align-1)/align)*align` 把 10000 向上拉到 10240；`ds4_kvstore_continued_store_target` 再用 `live_tokens % step != 0` 与 `<= continued_last_store_tokens` 两道闸确保只在「新的绝对前沿」落盘。

continued 的进度游标与 cold/continued 协调（防重复写）：

[ds4_kvstore.c:750-769](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L750-L769) —— `note_store` 把 `continued_last_store_tokens` 单调推进；`suppress_continued_store` 在 cold 落点恰好等于 continued 前沿时，**预先**把游标推到该点，避免紧接着的 prefill 进度回调把同一前缀再写一遍 `"continued"`；`restore_suppressed_continued` 在 cold 写失败时回滚游标。server 侧在 [ds4_server.c:10209-10247](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10209-L10247) 调用这对 suppress/restore。

#### 4.2.4 代码实践

**实践目标**：亲手算几个 cold / continued 落点，验证它们落在同一张 2048 网格上，并解释「为什么必须对齐到 prefill chunk 边界、为什么要裁尾」。

**操作步骤**：

1. 读 [ds4_kvstore.c:700-709](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L700-L709) 与 [ds4_kvstore.c:35-39](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L35-L39) 的注释。
2. 用默认参数手算：prompt 长度分别为 600、12000、30000 时，`store_len` 返回多少。
3. 手算 continued step，列出前三个会触发 continued 的 live token 数。
4. 回答两个问题：(a) 为什么 `stable % align` 必须等于 0，才能保证「存的压缩行 = 冷全量 prefill 的压缩行」？(b) 若不裁尾（trim=0），举一个「未来请求追加文本导致 BPE 跨界合并、前缀在缓存点断开」的具体场景。

**需要观察的现象**：cold 落点（10240、…）是 continued 落点（10240、20480、…）的子集；trim=32 让落点离「当前 prompt 末尾」至少隔 32 token。

**预期结果**：
- `store_len(600)`：`600 > 544`，`stable=568`，`568 % 2048 = 568`（不足一块），`stable = 0`，`0 < 512` → 返回原值 600（短前缀不强对齐）。
- `store_len(12000)`：`stable = 11968`，`11968 % 2048 = 1728`，返回 **10240**。
- `store_len(30000)`：`stable = 29968`，`29968 % 2048`：`14*2048=28672`，余 1296，返回 **28672**。
- continued step = 10240；触发点 10240、20480、30720（需 `>= min_tokens` 且 `% 10240 == 0`）。

**待本地验证**：你可以在 `ds4_server.c` 临时加一行日志打印 `store_len` 返回值（仅本地实验，勿提交），跑一条长 prompt 观察。

#### 4.2.5 小练习与答案

**练习 1**：continued step 为什么用「向上对齐」、而 cold `store_len` 用「向下对齐」？

**参考答案**：continued 的落点必须 ≤ 当前活 token 数（不能存还没 prefill 到的位置），所以从「间隔 10000」向上取到 10240 只是「规整化」前沿位置，真正的闸是 `live_tokens % step == 0`——只有 live 真的走到 10240 才写。cold 则是「在一段已经 prefill 完的 prompt 上挑一个稳定落点」，只能向下退到一个已有的 chunk 边界，所以向下对齐。

**练习 2**：把 `boundary_align_tokens` 调成 4096（而非 2048），continued step 会变成多少？cold 还能与 continued 落在同一网格吗？

**参考答案**：continued step = `ceil(10000/4096)*4096 = 3*4096 = 12288`；cold 仍向下对齐到 4096 倍数，二者依然共享 4096 网格，所以仍兼容。但要注意：4096 必须是后端实际 prefill chunk 的整数倍，否则「压缩行最终化与冷全量一致」的前提被破坏（u6-l1）。

---

### 4.3 淘汰评分

#### 4.3.1 概念说明

磁盘预算有限（默认 4096 MiB，`--kv-disk-space-mb`）。每次写入新检查点前，kvstore 估算新文件大小，若超预算就**先淘汰**旧文件腾位。删谁、留谁，由一个**评分函数** `ds4_kvstore_entry_eviction_score` 决定：**分数最低的先删**。

评分综合三件事：

1. **最近有多有用**：命中计数 `hits`。但「老命中」不能等同「新命中」——一周前的命中反映的是旧工作负载。于是用**半衰期衰减**：每过 6 小时（`DS4_KVSTORE_HIT_HALF_LIFE_SECONDS = 21600s`），`hits` 的「有效值」减半。
2. **每字节存了多少有用内容**：`tokens / file_size`，即「密度」。同样字节数下，缓存了更多 token 的高密度文件更值得留。
3. **是不是有意锚点**：cold / evict / shutdown 是人工/系统刻意留的锚点（不是单条对话的自动路标），给一个 2 倍软保护。

还有一条「防残留」规则：若某条 continued 检查点正好是新写入检查点的**严格前缀**（即「同一条路上的旧路标，马上要被新检查点取代」），则对其**大幅降权**，让它在预淘汰阶段被优先清掉——它对未来的复用价值已经被新检查点覆盖。

#### 4.3.2 核心流程

评分公式（详见 4.3.3）综合成：

\[
\text{score} = (\text{effective\_hits} + 1)\;\cdot\;\frac{\text{tokens}}{\text{file\_size}}\;\cdot\;m_{\text{anchor}}\;\cdot\;m_{\text{superseded}}
\]

其中命中计数的半衰期衰减为：

\[
\text{effective\_hits} = \text{hits}\cdot 2^{-\Delta t / 21600\text{s}},\qquad \Delta t = \text{now} - \text{last\_used}
\]

两个乘性修正：

- **anchor 保护**：reason ∈ {cold, evict, shutdown} 时 \(m_{\text{anchor}} = 2.0\)，否则 \(1.0\)。
- **被取代的 continued 路标**：若该条目是 incoming 新检查点的严格前缀，\(m_{\text{superseded}} = 0.05 + 0.45\cdot h\)，其中 \(h = \text{effective\_hits}/(\text{effective\_hits}+1)\)。于是：从没被命中的路标（\(h=0\)）只剩 5% 分数（几乎必删）；命中很多的路标（\(h\to 1\)）最多也只剩 50% 分数（仍受罚，但近期命中保它一程）。否则 \(m_{\text{superseded}} = 1.0\)。

淘汰循环：

```
evict(extra_bytes):                       # extra = 即将写入的新文件大小
    refresh()                             # 重新扫目录建条目表
    total = Σ entry.file_size
    target = budget_bytes - extra_bytes
    while total > target and len > 0:
        victim = argmin  entry_eviction_score(...)   # 同分取 last_used 更早者
        unlink(victim.path); total -= victim.file_size
```

预算还有一道 1% 安全裕量（`kv_cache_budget_required`）：估算时给文件大小加 1% 向上取整，避免「刚写完就被立即判定超预算而删除」的抖动。

#### 4.3.3 源码精读

半衰期常量在头文件：

[ds4_kvstore.h:11-13](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.h#L11-L13) —— `DS4_KVSTORE_FIXED_HEADER=48`、`DS4_KVSTORE_DEFAULT_MB=4096`、`DS4_KVSTORE_HIT_HALF_LIFE_SECONDS=6*60*60`（6 小时）。

评分相关的经验常数（含注释解释每条的存在理由）：

[ds4_kvstore.c:43-56](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L43-L56) —— `MIN_EFFECTIVE_HITS=0.01`（衰减到这点以下当 0）、`CONTINUED_PREFIX_MIN_FACTOR=0.05`、`CONTINUED_PREFIX_HIT_FACTOR=0.45`、`ANCHOR_REASON_SCORE_FACTOR=2.0`。

**评分函数本体**——本节核心：

[ds4_kvstore.c:532-559](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L532-L559) —— `ds4_kvstore_entry_eviction_score`：
- `effective_hits *= exp2(-elapsed / HIT_HALF_LIFE_SECONDS)`（539–547 行）实现半衰期衰减，低于 `MIN_EFFECTIVE_HITS` 归零；
- `score = (effective_hits + 1.0) * tokens / file_size`（548–549 行）；
- `kv_cache_reason_is_anchor` 为真则 `*= 2.0`（550–551 行）；
- `kv_cache_incoming_supersedes_continued` 为真则 `*= 0.05 + 0.45*h`（552–557 行）。
- 注意第 538 行 `(void)live;`：`live` 形参当前未使用，是为未来策略预留的 API 位。

anchor 判定与「被取代」判定：

[ds4_kvstore.c:526-530](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L526-L530) —— `kv_cache_reason_is_anchor`：reason 是 cold / evict / shutdown 之一即为锚点。

[ds4_kvstore.c:504-524](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L504-L524) —— `kv_cache_incoming_supersedes_continued`：被考察条目必须是 CONTINUED、其 `text_bytes < incoming.text_len`、模型 id 相同（必要时量化相同）、`incoming.ctx_size` 不更小，且**把 incoming 文本截到条目长度后做 SHA1，与条目 sha 相等**——也就是「incoming 确实是在这条 continued 之后继续往前走的」。

**淘汰循环**——找最低分受害者、删之、重复：

[ds4_kvstore.c:561-607](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L561-L607) —— `ds4_kvstore_evict`：`target = budget_bytes - extra_bytes`；while `total > target`，线性扫找最低分（同分取 `last_used` 更早者），`unlink` 后从表里移除并重算 total。注意 `if (extra_bytes > kc->budget_bytes) return`——单个文件就比整个预算大时直接放弃（不进入死循环）。

**1% 安全裕量**与「预算够不够」判定：

[ds4_kvstore.c:784-812](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L784-L812) —— `kv_cache_budget_required` 给文件大小加 `file_bytes/100`（向上取整）作裕量；`ds4_kvstore_file_size_fits` 在写入前与写完后各判一次（写前估算、写后实测），超预算则不 rename、删临时文件。

**预淘汰发生在写入主流程里**：

[ds4_kvstore.c:1044-1052](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1044-L1052) —— `store_live_prefix_text` 在估算出新文件大小后、写临时文件前，构造 `ds4_kvstore_eviction_context incoming` 并调用 `ds4_kvstore_evict(kc, live_tokens, est_file_bytes, &incoming)`，把 `incoming` 传进去正是为了让评分能识别「被取代的 continued」。

**启动时也淘汰一次**：

[ds4_kvstore.c:631](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L631) —— `ds4_kvstore_open` 末尾调一次 `ds4_kvstore_evict(kc, NULL, 0, NULL)`（`extra_bytes=0`、`incoming=NULL`），在进程启动就把超预算的旧文件清掉。

> 旁路：cold_max_tokens 还在**加载**侧起作用——若一次命中加载的检查点 `tokens > cold_max_tokens`，文件被**消费式删除**（`unlink`），视为一次性大检查点，不留在盘上积累。见 [ds4_kvstore.c:1321-1330](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L1321-L1330)。

#### 4.3.4 代码实践

**实践目标**：用纸笔算两条典型条目的评分，亲眼看清「anchor 保护」与「被取代路标降权」如何改变谁被删。

**操作步骤**：

1. 读 [ds4_kvstore.c:532-559](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_kvstore.c#L532-L559)，把公式抄下来。
2. 设两条条目（默认参数，file_size 用字节数，预算只够留一条）：
   - **A**：reason=cold，tokens=10240，file_size=60 MiB，hits=4，last_used 在 12 小时前。
   - **B**：reason=continued，tokens=10240，file_size=60 MiB，hits=0，last_used 刚刚；且 B 是当前 incoming 的严格前缀。
3. 分别算 A、B 的 `effective_hits`、`score`，判断谁被淘汰。
4. 再把 B 的 reason 换成 cold（其余不变），重算，看结论是否反转。

**需要观察的现象**：A 因半衰期衰减 + anchor 保护获得中等分数；B 因「无命中 + 被取代」被压到极低分数而被删；但若 B 也是 anchor，它的被取代惩罚仍在（最多 0.5×），所以 anchor 并非免死金牌。

**预期结果**（数值取近似，密度项 `tokens/file_size` 两者相同，故只看乘性因子）：
- A：`elapsed=43200s`，`effective_hits = 4·2^(-43200/21600) = 4·2^{-2} = 1.0`；非被取代；anchor → score ∝ `(1+1)·2.0 = 4.0`。
- B：hits=0 → `effective_hits=0`；`h=0` → 被取代乘子 `0.05`；reason=continued 非 anchor → score ∝ `(0+1)·1·0.05 = 0.05`。
- 结论：**B 被淘汰**（0.05 ≪ 4.0）。
- 若把 B 改成 cold（anchor）：score ∝ `1·2.0·0.05 = 0.1`，仍远低于 A 的 4.0，**B 仍被淘汰**——anchor 保护敌不过「被取代 + 零命中」。

**待本地验证**：因 `exp2` 与浮点细节，实际数值可能有微小出入，但量级差与排序结论稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么用「半衰期」而不是「固定时间窗」（比如「7 天前的命中一律清零」）？

**参考答案**：固定窗口会在边界处产生跳变——第 6 天 23:59 的命中算满分、7 天后秒清零，导致评分突变、淘汰行为抖动。半衰期是平滑的指数衰减，旧命中随时间**渐进**失去权重，既不让远古命中永久占位，也不产生跳变；6 小时的半衰期匹配「工作负载在一天内会显著变化」的直觉。

**练习 2**：incoming-supersedes 判定为什么要比 SHA1（`sha1(prefix)==entry.sha`），而不是简单比 token 数或文本长度？

**参考答案**：token 数或长度相等不保证「是同一条路的前缀」——两个完全不同的对话可能恰好在同一长度。只有「把 incoming 的渲染文本截到 entry 的长度，其 SHA1 等于 entry 的文件名 sha」才能证明 entry 的字节确是 incoming 的真前缀，即这条 continued 路标真的会被新检查点取代，降权才安全。

---

## 5. 综合实践

把三个最小模块串起来，做一次「完整的 cold 落盘 + 预淘汰」纸面推演。

**场景**：磁盘预算 4096 MiB，目录里已有一条旧检查点 **X**（reason=continued，tokens=8192，file_size=50 MiB，hits=0，last_used=1 小时前，且 X 不是任何新写入的前缀）。现在来了一条 12000 token 的新 prompt，前缀复用全部 miss（`cached==0`），`prompt_len <= cold_max_tokens`，活 session 已 prefill 完。

**任务**：

1. **算 cold 落点**：用 `store_len(12000)` 求落盘 token 数（应为 10240，见 4.2.4）。
2. **算新文件估算大小**：假设 payload 约 90 MiB，加 48+4 字节头与渲染文本，估算 `est_file_bytes`，再算 1% 裕量后的 `required`（4.3.3）。
3. **判是否需要预淘汰**：`target = budget - est_file_bytes`，现有 total（X 的 50 MiB）是否 > target？若不需要淘汰，X 留下；若需要，算 X 的 score 看它是否成为受害者。
4. **算 X 的 score**：reason=continued 非 anchor、非被取代（题目设定）、`elapsed=3600s`、`effective_hits=0` → score ∝ `(0+1)·1·1·(8192/file_size)`。
5. **回答**：在这套默认参数下，X 大概率会被保留还是被删？若把预算改成 100 MiB 呢？

**预期结论**：4096 MiB 预算下 total 远小于 target，无需淘汰，X 保留；预算改 100 MiB 时，total(50) + 新文件 required(≈90×1.01) > 100，需要淘汰，而 X 是唯一候选且 score 很低（零命中），会被删——这正体现了「评分低者优先删，但只在真不够时才删」。

**待本地验证**：payload 实际大小依赖模型与 ctx，本推演用假设值；可参考 u8-l3 的 payload 结构估算真实占比。

## 6. 本讲小结

- 磁盘 KV 缓存在**四个时机**写检查点：cold（首段长 prompt prefill 完、生成前）、continued（命中绝对对齐前沿）、evict（加载磁盘快照替换活 session 前）、shutdown（干净退出前），各自带一个写进 KVC 头第 5 字节的**原因码**。
- cold / continued 的落点都先**裁掉 32 尾 token**（防 BPE 跨界合并）、再**对齐到 2048 网格**（让压缩行最终化与一次冷全量 prefill 逐位一致）；continued step 把 10000 向上对齐到 10240，与 cold 共享同一张 2048 网格。
- cold 还会优先落在「第一个 assistant 标记前的最后一个 user 标记」这个**聊天锚点**；cold 与 continued 通过 `suppress/restore_continued_store` 协调，避免对同一前缀重复写。
- 淘汰用**评分最低者优先删**：`score = (decayed_hits+1)·tokens/file_size`，命中计数按 **6 小时半衰期**衰减；cold/evict/shutdown 这种 **anchor** 原因享 2 倍软保护；被新检查点取代的 **continued 路标**被压到 5%–50%，是廉价牺牲品。
- 预算带 **1% 安全裕量**，写入前后各判一次是否超预算；启动时也跑一次淘汰清掉历史超量文件；cold_max_tokens 在加载侧对超大检查点做**消费式删除**。

## 7. 下一步学习建议

- **u8-l3（会话 payload 序列化 DSV4）**：本讲反复提到「compressor frontier 必须在 chunk 边界最终化」，下一步正是去看 DSV4 头部那 13 个 `u32` 字段与逐层 raw/compressed/indexer KV 是怎么落成 `payload_bytes` 的——它能解释 4.3 里「file_size 到底由什么构成」。
- **u7-l5（实时 KV 前缀复用与检查点改写）**：本讲的 cold 裁剪/对齐是为了「让前缀能被复用」，而 u7-l5 讲的是「复用命中后如何处理增量后缀、以及中间改写为何要重建」——两者一前一后，合起来就是服务器磁盘 KV 的完整命中链。
- **继续阅读**：`ds4_kvstore.c` 的 `store_live_prefix_text`（923–1155 行）是本讲三条线索的汇流点，建议通读一遍，把临时文件 `.tmp` → `rename` 的原子写、失败回滚、日志点串起来；再对照 README 的 `Disk KV Cache` 一节（990–1172 行）做一次文字↔源码核对。
