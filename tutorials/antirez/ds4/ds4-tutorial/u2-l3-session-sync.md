# Session 同步与前缀复用

## 1. 本讲目标

上一讲（u2-l1）我们确立了 ds4 的两条公共边界：`ds4_engine`（已加载模型）和 `ds4_session`（一条可变推理时间线），并提到「调用方提供完整 token 前缀，由 `ds4_session_sync()` 决定是复用、增量扩展还是从零重建图状态」。

本讲就钻进这个「决定」本身。读完本讲你应当能够：

- 说清 `ds4_session` 内部那个 `checkpoint` 到底记录了什么，它和 KV 缓存、logits 是什么关系。
- 画出 `ds4_session_sync()` 的判定流程：什么情况下只增量评估后缀、什么情况下整段重建。
- 解释 `ds4_session_common_prefix()` 和 `ds4_session_sync()` 在「前缀」这件事上的分工差异。
- 用一句话说清 `ds4_session_rewrite_requires_rebuild()` 为什么把「在末尾追加」判为安全、把「在中间改写」判为必须重建。
- 区分 `invalidate / rewind / eval / pos` 这几个控制原语各自改了 session 的哪些字段。

本讲只盯住「同步与复用」这一条逻辑线，不展开 MLA 注意力、量化、Metal 图调度等内部细节——那些属于后续单元。

---

## 2. 前置知识

### 2.1 prefill 与 decode

大模型推理分两个节奏（u1-l5 已引入，这里复习）：

- **prefill（预填）**：把一段提示一次性「读进」KV 缓存。它决定首 token 延迟，吞吐量级是「每秒处理多少提示 token」。
- **decode（解码）**：在 KV 缓存已就绪的前提下，每次自回归生成一个 token。它决定生成速度。

关键点：**KV 缓存是 prefill 的产物**。如果同一份提示要被反复用到，重用 KV 就能跳过 prefill，直接进入 decode。本讲的核心就是「怎么判断 KV 还能不能用」。

### 2.2 token 前缀

ds4 里所有提示都用 `ds4_tokens` 表示，它就是一个整型 token 数组：

```c
typedef struct {
    int *v;
    int len;
    int cap;
} ds4_tokens;
```

（见 [ds4.h:43-47](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L43-L47)）

「前缀」就是两个 token 数组从下标 0 开始逐位相等的那一段。多轮对话里，新转录本通常是旧转录本的「严格扩展」（旧内容一字未改，只在后面追加新消息），这正是 KV 复用最理想的场景。

### 2.3 不透明指针与窄头

`ds4_session` 在 `ds4.h` 里只暴露成 `typedef struct ds4_session ds4_session;`（[ds4.h:60](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L60)），真正的字段定义藏在 `ds4.c` 里。CLI、服务器、agent 这些前端只通过 `ds4.h` 里那组函数操作 session，不依赖张量内部结构。本讲为了讲清同步逻辑，会「破开」这个不透明指针看几个关键字段，但调用方应当只用公共 API。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 公共边界。声明 `ds4_session_sync` / `common_prefix` / `rewrite_*` / `invalidate` / `rewind` / `eval` / `pos` 等函数，以及 `DS4_SESSION_SYNC_INTERRUPTED` 常量与 `ds4_session_rewrite_result` 枚举。 |
| [ds4.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c) | 引擎核心。`struct ds4_session` 定义、`ds4_session_sync` 主逻辑、前缀/改写判定、控制原语都在这里。 |
| [ds4_cli.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c) | CLI 前端。`run_chat_turn` 是「多轮对话追加消息时如何复用 KV」的最佳范例。 |
| [ds4_server.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c) | HTTP 服务器。`canonicalize_tool_checkpoint` 演示了「改写判定 + 磁盘快照回退」的真实用法，并附有改写判定的回归测试。 |

本讲引用的源码集中在 `ds4.c` 的 23000–27800 行区间（session 结构、payload、同步、采样、控制原语）。

---

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **前缀匹配与增量评估**——`checkpoint` 是什么、`ds4_session_sync` 怎么复用它、`common_prefix` 怎么测量它。
2. **rewrite 判定**——什么时候可以就地扩展、什么时候必须重建。
3. **会话控制原语**——`invalidate / rewind / eval / pos` 与协作式中断。

---

### 4.1 前缀匹配与增量评估

#### 4.1.1 概念说明：checkpoint 是 session 的「已算到哪」标记

一条 `ds4_session` 持有：一份活 KV 缓存（GPU 上是 Metal/CUDA/ROCm 图里的张量，CPU 上是 `cpu_cache`）、一份当前 logits、以及一个 token 向量 `checkpoint`。`checkpoint` 记录的是「当前 KV 缓存与 logits 究竟对应哪一段 token 序列」。

看真实结构（节选关键字段）：

```c
struct ds4_session {
    ds4_engine *engine;
    ds4_dist_session *distributed;
    ds4_gpu_graph graph;          // GPU 后端的活 KV / 图状态
    ds4_kv_cache cpu_cache;       // CPU 后端的活 KV
    token_vec checkpoint;         // KV 当前对应的 token 序列
    float *logits;                // 序列末位的预测分布
    ...
    uint32_t prefill_cap;
    int ctx_size;
    bool checkpoint_valid;        // KV/logits 是否与 checkpoint 一致
    bool mtp_draft_valid;
};
```

（[ds4.c:23259-23283](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L23259-L23283)）

理解两个字段就够了：

- **`checkpoint`**：一个 `token_vec`（即 `ds4_tokens` 的内部别名），存「KV 缓存里已经装了哪些 token」。
- **`checkpoint_valid`**：布尔标志，回答「KV 缓存和 logits 当前是否真的与 `checkpoint` 描述的序列一致」。一切复用逻辑都建立在这个标志之上。

为什么需要 `checkpoint_valid` 这个独立标志？因为很多操作会让 KV 与 `checkpoint` 脱钩：整段重建失败、decode 出错、被用户中断在半路……这些时候 `checkpoint.len` 可能还有值，但 KV 已经不可信。`checkpoint_valid=false` 就是在说「别信 checkpoint，下次必须从头来」。

`ds4_session_create` 用 `xcalloc` 分配并零初始化（[ds4.c:26046](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26046)），所以新 session 的 `checkpoint_valid` 是 `false`、`checkpoint.len` 是 0——首次 sync 必然走整段重建分支。

#### 4.1.2 核心流程：sync 的判定树

把 `ds4_session_sync(s, prompt, ...)` 的决策画成伪代码：

```
输入: s 当前 session 状态（checkpoint C, checkpoint_valid V）
     prompt P = [p_0, ..., p_{n-1}]

边界检查失败（空 prompt、超过 ctx_size）  → 报错返回 1
被协作式取消                                → 返回 DS4_SESSION_SYNC_INTERRUPTED

if 是分布式 session:    → 委托 ds4_dist_session_sync（见 u9 单元）

if 能复用:              V == true  且  n >= m  且  C == P[0:m]   # C 是 P 的前缀
    只评估后缀 P[m:n]
    （GPU 上：后缀长 >= resume_min 走「续 prefill」图，否则逐 token decode）
    把后缀追加进 checkpoint，V = true
    返回 0

否则（无法复用）:       # C 不是 P 的前缀，或 V==false
    V = false, checkpoint.len = 0
    重置后端 prefill 状态
    整段 prefill P，写满 KV
    checkpoint = P, V = true
    返回 0
```

复用判定里的「`C == P[0:m]`」就是严格的**整段前缀匹配**：旧的 checkpoint 必须是 prompt 的一个前缀（逐 token 相等），且 prompt 不比 checkpoint 短。写成数学语言，设 checkpoint 为 \(C=(c_0,\dots,c_{m-1})\)、prompt 为 \(P=(p_0,\dots,p_{n-1})\)，则可复用当且仅当：

\[
V \;\land\; (n \ge m) \;\land\; \bigl(\forall i\in[0,m):\; c_i = p_i\bigr)
\]

满足时只需评估后缀 \(P[m:n)\)，其余 KV 全部保留。

这个判定函数本身是 `ds4_tokens_starts_with`：

```c
bool ds4_tokens_starts_with(const ds4_tokens *tokens, const ds4_tokens *prefix) {
    if (prefix->len > tokens->len) return false;
    for (int i = 0; i < prefix->len; i++) {
        if (tokens->v[i] != prefix->v[i]) return false;
    }
    return true;
}
```

（[ds4.c:21786-21792](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21786-L21792)）—— 注意它要求 `prefix->len <= tokens->len`，所以调用处先单独判了 `prompt->len >= s->checkpoint.len` 再调它。

#### 4.1.3 源码精读：ds4_session_sync 的三个分支

公共声明上的注释把意图说得最清楚：

```c
/* Synchronize the live session to a full prompt token prefix.  If the current
 * checkpoint is a prefix, only the suffix is evaluated; otherwise the backend
 * state is refilled from scratch. */
int ds4_session_sync(ds4_session *s, const ds4_tokens *prompt, char *err, size_t errlen);
```

（[ds4.h:246-249](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L246-L249)）

实现入口先做边界检查、取消检查，再按「分布式 / CPU / GPU」分流（[ds4.c:26698-26716](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26698-L26716)）。我们重点看 GPU 分支，因为它最能体现「复用 vs 重建」的取舍。

**分支 A：可复用——只评估后缀**（[ds4.c:26772-26838](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26772-L26838)）

```c
if (s->checkpoint_valid &&
    prompt->len >= s->checkpoint.len &&
    ds4_tokens_starts_with(prompt, &s->checkpoint))
{
    s->mtp_draft_valid = false;
    const int suffix = prompt->len - s->checkpoint.len;
    const uint32_t resume_min = metal_graph_resume_prefill_min_tokens();
    if (suffix > 0 && (uint32_t)suffix >= resume_min) {
        // 后缀够长：用「续 prefill」layer-major 图一次性处理整段后缀
        bool ok = metal_graph_prefill_chunked_range(...,
                    (uint32_t)s->checkpoint.len, (uint32_t)suffix, ...);
        ...
        ds4_tokens_copy(&s->checkpoint, prompt);
        return 0;
    }
    // 后缀太短：逐 token decode 把它补进 KV
    for (int i = s->checkpoint.len; i < prompt->len; i++) {
        ...
        metal_graph_eval_token_raw_swa(..., (uint32_t)prompt->v[i],
                                       (uint32_t)s->checkpoint.len, ...);
        token_vec_push(&s->checkpoint, prompt->v[i]);
    }
    return 0;
}
```

这里有个**性能细节**：后缀该用哪种方式评估？阈值是 `metal_graph_resume_prefill_min_tokens()`，默认 **4** 个 token（可被环境变量 `DS4_METAL_RESUME_PREFILL_MIN` 覆盖，传非正值相当于禁用续 prefill）：

```c
static uint32_t metal_graph_resume_prefill_min_tokens(void) {
    const char *env = getenv("DS4_METAL_RESUME_PREFILL_MIN");
    ...
    return 4u;
}
```

（[ds4.c:21396-21409](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L21396-L21409)）

直觉是：为一个只有 1–3 个 token 的后缀启动一整张 layer-major prefill 图并不划算，不如直接逐 token decode。后缀越长，越值得走「续 prefill」。

**分支 B：不可复用——整段重建**（[ds4.c:26840-26894](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26840-L26894)）

```c
s->checkpoint_valid = false;
s->checkpoint.len = 0;
s->mtp_draft_valid = false;
metal_graph_reset_prefill_state(&s->graph);
// 再按 prompt 长度是否超过 prefill_cap 选 chunked 或 raw prefill
...
ds4_tokens_copy(&s->checkpoint, prompt);
s->checkpoint_valid = true;
return 0;
```

重建分支先把状态彻底作废（`checkpoint_valid=false`、`checkpoint.len=0`、重置 prefill 图状态），然后从零 prefill 整个 prompt。CPU 分支（[ds4.c:26717-26762](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26717-L26762)）逻辑同构，只是把 Metal 图换成 `prefill_layer_major_cpu` / `forward_token_raw_swa_cpu_decode_scratch`。

**为什么要先 `checkpoint_valid=false` 再重建？** 万一 prefill 中途失败，session 留下的就是一个诚实的「不可信」状态，下次调用不会被半截 KV 骗到。

#### 4.1.4 代码实践：多轮对话追加消息时如何避免重新 prefill

> **实践目标**：追踪 REPL 的一次「追加用户消息」，说清楚 session 为什么不必从 token 0 重新 prefill。

**操作步骤（源码阅读型实践）**：

1. 打开 [ds4_cli.c:1082-1120](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1082-L1120) 的 `run_chat_turn`。它维护一个 `chat->transcript`（已渲染的整段聊天转录本 token 序列）和一个活 `chat->session`。
2. 注意每一轮的做法：**不重建 session**，而是往 `transcript` 末尾追加新内容（[ds4_cli.c:1091-1093](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1091-L1093)）：
   ```c
   const int rollback_len = chat->transcript.len;
   ds4_chat_append_message(engine, &chat->transcript, "user", user_text);
   ds4_chat_append_assistant_prefix(engine, &chat->transcript, think_mode);
   ```
3. 然后它先自己量一下公共前缀（仅用于进度显示），再调 sync（[ds4_cli.c:1095-1112](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1095-L1112)）：
   ```c
   const int old_pos = ds4_session_pos(chat->session);
   const int common = ds4_session_common_prefix(chat->session, &chat->transcript);
   const int cached = common == old_pos && chat->transcript.len >= old_pos ? common : 0;
   const int suffix = chat->transcript.len - cached;
   ...
   int sync_rc = ds4_session_sync(chat->session, &chat->transcript, err, sizeof(err));
   ```

**应当观察到的现象（推理过程）**：

- 因为只是「追加」，旧 transcript 是新 transcript 的严格前缀，所以 `ds4_session_common_prefix` 返回值等于 `old_pos`（旧 checkpoint 全部命中）。
- `cached` 因此等于 `old_pos`，`suffix` 就是这一轮新追加的 token 数。
- `ds4_session_sync` 内部命中分支 A（`checkpoint_valid && prompt->len >= checkpoint.len && starts_with`），只对后缀做 prefill/decode。
- 结果：上一轮已 prefill 的部分被完整复用，**只有新消息 + assistant 前缀被实际计算**。

**预期结果**：第二轮起，你在终端看到的 prefill 进度条只覆盖新增 token 数，而不是整段历史。首 token 延迟与「新增 token 数」成正比，而非与「整段对话长度」成正比。

> ⚠️ 待本地验证：实际数值（进度条 token 数、首 token 延迟）取决于你的模型与机器；本实践只描述源码层面的因果关系，未替你运行命令。若你切了 `/think max` 或改了 `/ctx`，`repl_chat_apply_max_prefix` 会改写 transcript 结构导致前缀失配，此时 `cached` 会回落为 0、sync 走重建分支——这也是 REPL 切档后变慢的原因。

#### 4.1.5 小练习与答案

**练习 1**：假设 `checkpoint = [10, 20, 30]`（valid），分别给出 `prompt = [10,20,30,40]`、`prompt = [10,20,30]`、`prompt = [10,20,40]` 时 sync 走哪条分支。

**参考答案**：
- `[10,20,30,40]`：分支 A，后缀 `[40]`，因 `1 < resume_min(4)` 走逐 token decode。
- `[10,20,30]`：分支 A，后缀长度 0，直接返回 0，不做任何计算（KV 已就绪）。
- `[10,20,40]`：`ds4_tokens_starts_with` 在下标 2 失败 → 分支 B，整段重建。

**练习 2**：为什么 `ds4_session_sync` 要在重建分支里**先**把 `checkpoint_valid` 置 false 再 prefill，而不是 prefill 成功后再置 true？

**参考答案**：为了失败安全。如果 prefill 中途出错（或被取消），session 必须停留在一个「诚实不可信」的状态（`checkpoint_valid=false`），这样下次调用不会被半截 KV 误导而错误命中分支 A。成功路径才在最后把 `checkpoint` 拷成 prompt 并置 `checkpoint_valid=true`。

---

### 4.2 rewrite 判定：何时不能就地改写

#### 4.2.1 概念说明：在末尾追加 vs 在中间改写

复用（4.1）只覆盖了「prompt 是 checkpoint 的严格扩展」这一种情形。但真实服务里还有另一种情形：**用户改写了历史**——比如把已生成的工具调用 DSML 规范化成另一种字节序列，导致新 prompt 与旧 checkpoint 共享一段前缀，但在某个中间位置之后分叉，且分叉点**不在** checkpoint 的末尾。

这种「在活尾部之前改写」能不能就地完成？答案是不能。源码注释说得很直白：

> A DS4 session checkpoint is more than a token vector: the backend state also contains raw SWA rows, compressed KV rows, indexer rows, and compressor frontiers. Replacing any part of the live tail requires restoring that whole frontier first. **Extending exactly at the live end is safe; rewriting behind it is not an in-place operation.**

（[ds4.c:26897-26903](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26897-L26903)）

直觉：KV 缓存不是一张「按 token 行独立写入」的表，而是一套**带前沿（frontier）的状态机**——压缩层有 compressor frontier、indexer 行、每层的压缩行计数器等。在末尾追加 token 时这些前沿自然向前推进；但要在中间替换 token，就得把所有前沿回退到改写点再重放，这等价于重建。所以 ds4 把「改写」一分为二：

- **改写点恰在活末尾**（`common == live_len`）：等价于追加，安全，直接走 sync。
- **改写点在活末尾之前**（`common < live_len`）：不能就地，报告需要重建。

> 术语：这里的 SWA = Sliding Window Attention（滑动窗口注意力），是 DeepSeek V4 raw KV 的组织方式；compressor / indexer frontier 属于压缩 KV 层。这些会在 u4-l2（KV 缓存设计）展开，本讲你只需知道「前沿一旦越过改写点就无法廉价回退」。

#### 4.2.2 核心流程：改写判定与三态结果

判定函数极其简洁：

```c
bool ds4_session_rewrite_requires_rebuild(int live_len, int canonical_len, int common) {
    if (live_len < 0 || canonical_len < 0 || common < 0) return true;
    if (common > live_len || common > canonical_len) return true;
    return common < live_len;
}
```

（[ds4.c:26904-26908](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26904-L26908)）

数学化（去掉非法输入的护栏后）：

\[
\text{rebuild} \iff \text{common} < \text{live\_len}
\]

即「公共前缀短于已有活长度」就要重建。只有 `common == live_len`（改写点正好在活末尾，等价于纯追加）才允许就地。`canonical_len`（规范化后 prompt 的长度）在这个谓词里只用于合法性检查，不直接参与大小比较——它在外层 `rewrite_from_common` 里决定后续是 sync 追加还是返回重建。

外层封装把结果归一成三态枚举：

```c
typedef enum {
    DS4_SESSION_REWRITE_ERROR = -1,
    DS4_SESSION_REWRITE_OK = 0,
    /* The live backend state cannot be rewritten safely in place.  The caller should
     * restore an older checkpoint if it has one, then sync to the prompt. */
    DS4_SESSION_REWRITE_REBUILD_NEEDED = 1,
} ds4_session_rewrite_result;
```

（[ds4.h:238-244](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L238-L244)）

#### 4.2.3 源码精读：rewrite_from_common 与服务器的回退链

`ds4_session_rewrite_from_common`（[ds4.c:26920-26955](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26920-L26955)）是把上面判定接进真实流程的桥：

```c
// 1. 校验 prompt 边界、checkpoint 有效、common 合法
// 2. 逐 token 确认 common 段确实和 checkpoint 一致
for (int i = 0; i < common; i++) {
    if (s->checkpoint.v[i] != prompt->v[i]) { ... return ERROR; }
}

// 3. 改写点正好在活末尾 → 委托 sync（走「追加」路径）
if (common == s->checkpoint.len) {
    return ds4_session_sync(s, prompt, err, errlen) == 0 ?
        DS4_SESSION_REWRITE_OK : DS4_SESSION_REWRITE_ERROR;
}

// 4. 否则判定是否需要重建
if (ds4_session_rewrite_requires_rebuild(s->checkpoint.len, prompt->len, common)) {
    snprintf(err, errlen, "rewrite needs rebuild: common=%d live=%d canonical=%d", ...);
    return DS4_SESSION_REWRITE_REBUILD_NEEDED;
}
```

注意它的注释（[ds4.c:26910-26919](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26910-L26919)）：这是为「解析生成出的工具调用」准备的——模型可能按合法但非字节相等的顺序吐出 DSML，下一次请求看到的规范化 prompt 与当前 KV 不完全一致。在真正能「在改写点恢复前沿快照」之前，任何在活末尾之前的替换都只报告需要重建，**不改动 session**。

那 REBUILD_NEEDED 之后谁来收拾？服务器。看 `canonicalize_tool_checkpoint`（[ds4_server.c:9821-9889](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9821-L9889)）的回退链：

```c
ds4_session_rewrite_result rr =
    ds4_session_rewrite_from_common(s->session, &canonical, common, err, sizeof(err));
if (rr == DS4_SESSION_REWRITE_OK) {
    // 就地规范化成功
} else if (rr == DS4_SESSION_REWRITE_REBUILD_NEEDED) {
    // 优先加载更旧的磁盘 KV 快照，而不是从 token 0 重放整段长对话
    int loaded = kv_cache_try_load_text(s, rendered.ptr, &effective, &path, NULL, false);
    if (loaded == 0) ds4_session_invalidate(s->session);
    const ds4_tokens *sync_prompt = loaded > 0 ? &effective : &canonical;
    ...
}
```

这条链体现了 ds4 的工程取向：**就地改写不可行时，优先用磁盘 KV 快照（u8 单元）兜底，再不得已才从零重放**。`ds4_session_invalidate`（4.3 节）正是用来强制下一次 sync 走重建的开关。

这条逻辑还有一份回归测试，把判定的边界写成了断言（[ds4_server.c:14718-14731](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L14718-L14731)）：

```c
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(19296, 19290, 19081));  // common < live → 重建
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(1024, 1030, 1000));     // common < live → 重建
TEST_ASSERT(ds4_session_rewrite_requires_rebuild(1024, 900, 900));       // common < live → 重建

TEST_ASSERT(!ds4_session_rewrite_requires_rebuild(1024, 1024, 1024));    // common == live → 安全
TEST_ASSERT(!ds4_session_rewrite_requires_rebuild(1024, 1100, 1024));    // common == live → 安全（追加）
```

对照公式 \(\text{rebuild} \iff \text{common} < \text{live\_len}\)：前三个 `common`（19081/1000/900）都小于 `live_len`（19296/1024/1024）→ 重建；后两个 `common == live_len == 1024` → 安全。注意第二条 `canonical_len=1030 > live_len=1024`，但因 `common==live_len` 仍判安全——这对应「在末尾追加 6 个规范化 token」的场景。

#### 4.2.4 代码实践：预测 rewrite_requires_rebuild 的输出

> **实践目标**：不读答案，先自己用公式预测，再对照源码断言验证对判定逻辑的掌握。

**操作步骤**：

1. 复习谓词：去护栏后 `rebuild = (common < live_len)`，护栏为「任一参数负 → true」「common > live_len 或 common > canonical_len → true」。
2. 对下表每行，写出 `true`（重建）或 `false`（安全）。

| live_len | canonical_len | common | 你的预测 |
|----------|---------------|--------|----------|
| 500      | 500           | 500    | ?        |
| 500      | 600           | 500    | ?        |
| 500      | 480           | 480    | ?        |
| 500      | 700           | 300    | ?        |
| 500      | 200           | 600    | ?        |

**预期结果（答案）**：
- `500,500,500` → `false`（common==live，纯相等，安全）。
- `500,600,500` → `false`（common==live，在末尾追加 100 个，安全）。
- `500,480,480` → `true`（common<live，改写点在末尾之前）。
- `500,700,300` → `true`（common<live）。
- `500,200,600` → `true`（护栏：common>canonical_len，非法）。

**应当观察的现象**：只要 `common` 严格小于 `live_len`，就必须重建——无论 `canonical_len` 是更长还是更短。这印证了「关键不是新旧长度比，而是改写点有没有越过活前沿」。

#### 4.2.5 小练习与答案

**练习 1**：服务器规范化工具调用时，为什么「REBUILD_NEEDED」之后不直接从 token 0 重放，而要先尝试 `kv_cache_try_load_text`？

**参考答案**：因为长对话从零 prefill 极其昂贵。ds4 把磁盘 KV 快照当作一等公民（u1-l1 的「KV 缓存即一等磁盘公民」），改写不可行时优先加载一个更旧但有效的快照，再只重放快照之后的后缀，代价远低于整段重放。

**练习 2**：假设将来 ds4 实现了「在改写点精确恢复压缩 KV 前沿」，`ds4_session_rewrite_requires_rebuild` 的语义应如何变化？

**参考答案**：届时 `common < live_len` 也可以就地完成（回退前沿到 common、丢弃后缀、按新 prompt 重放），谓词可在「前沿可回退到 common」的条件下返回 `false`。目前因为没有这种回退能力，任何 `common < live_len` 都保守地返回 `true`。这正是源码注释「Until we have a real frontier snapshot at the rewrite point」指向的未竟之志。

---

### 4.3 会话控制原语

#### 4.3.1 概念说明：四个低电平开关

除了 `sync` 这个「大动作」，session 还提供一组细粒度原语，让前端精确控制状态机：

| 原语 | 作用 | 典型用途 |
|------|------|----------|
| `ds4_session_pos` | 读 `checkpoint.len`（当前位置） | 计算还能生成多少 token：`ctx - pos` |
| `ds4_session_eval` | 推进一个 token（decode 一步） | 生成循环里逐 token 前进 |
| `ds4_session_invalidate` | 把 KV 标记为不可信 | 强制下次 sync 整段重建 |
| `ds4_session_rewind` | 把 checkpoint 逻辑截断到 pos | 低电平截断（见 4.3.3 注意事项） |

此外还有协作式取消：前端注册一个 `cancel` 回调，`sync` 在安全边界检查它，命中就返回 `DS4_SESSION_SYNC_INTERRUPTED`（值 2，见 [ds4.h:65](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L65)），并保证停在一个合法前缀上。

#### 4.3.2 核心流程：每个原语动了哪些字段

| 原语 | `checkpoint_valid` | `checkpoint.len` | `mtp_draft_valid` | 其它 |
|------|--------------------|------------------|-------------------|------|
| `invalidate` | → `false` | → `0` | → `false` | KV/logits 不再可信 |
| `rewind(pos)` | **不变** | → `clamp(pos,0,len)` | → `false` | 仅逻辑截断 |
| `eval(token)` | → `true` | → `+1`（push token） | → `false` | 重算 logits |
| `pos()` | 只读 | 只读 | 只读 | 返回 `checkpoint.len` |

`eval` 的关键不变量：成功一步后 `checkpoint_valid` 一定为 `true`、`checkpoint` 末尾一定是刚评估的那个 token——这保证下一次 `sync` 能命中分支 A。

`invalidate` 与 `rewind` 的本质区别：`invalidate` 连 KV 带标志一起作废（下次必重建）；`rewind` 只缩短「逻辑长度」、不碰 `checkpoint_valid`，语义上是「我相信 KV 在 `[0,pos)` 段依然有效」。

#### 4.3.3 源码精读

**invalidate**——最干脆的「作废」：

```c
void ds4_session_invalidate(ds4_session *s) {
    s->checkpoint_valid = false;
    s->checkpoint.len = 0;
    s->mtp_draft_valid = false;
}
```

（[ds4.c:27768-27772](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27768-L27772)）

CLI 在切换 Think Max、改 `/ctx` 等会让 transcript 结构变化的操作后会调它（[ds4_cli.c:1033](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1033)、[ds4_cli.c:1037](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1037)），强制下一轮重建 KV。服务器在磁盘快照加载失败时也会调它（[ds4_server.c:9878](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9878)）。

**rewind**——逻辑截断：

```c
void ds4_session_rewind(ds4_session *s, int pos) {
    if (pos < 0) pos = 0;
    if (pos > s->checkpoint.len) pos = s->checkpoint.len;
    s->checkpoint.len = pos;
    s->mtp_draft_valid = false;
}
```

（[ds4.c:27774-27779](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27774-L27779)）

注意它**不**把 `checkpoint_valid` 置 false——它假定调用方确信 `[0,pos)` 段的 KV 仍然有效。这是一个暴露给前端、但目前在 ds4 自带的前端（CLI/服务器）里没有被调用的低电平原语（全仓只在此处定义、在 `ds4.h` 声明）。**安全提示**：对 GPU 后端的滑动窗口 raw KV，越过窗口的截断不一定能保证注意力正确，除非你确知后端状态；多数情况下更稳妥的高层做法是 `invalidate()` + `sync()`。

**eval / eval_internal**——decode 一步。`eval` 是薄包装：

```c
int ds4_session_eval(ds4_session *s, int token, char *err, size_t errlen) {
    return ds4_session_eval_internal(s, token, true, err, errlen);
}
```

（[ds4.c:27156-27158](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27156-L27158)）

GPU 分支核心（节选，[ds4.c:27125-27134](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27125-L27134)）：

```c
if (!metal_graph_eval_token_raw_swa(&s->graph, ..., (uint32_t)token,
                                    (uint32_t)s->checkpoint.len, s->logits))
{
    snprintf(err, errlen, "%s decode failed", ...);
    s->checkpoint_valid = false;   // 失败 → 作废
    return 1;
}
token_vec_push(&s->checkpoint, token);   // 成功 → checkpoint 增长
```

和 sync 一样遵循「失败即作废」原则：decode 出错就把 `checkpoint_valid` 置 false，避免半截 KV 被复用。

**pos**——一行：

```c
int ds4_session_pos(ds4_session *s) {
    return s->checkpoint.len;
}
```

（[ds4.c:27781-27783](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L27781-L27783)）

CLI 用它算「还能生成多少」：`room = ds4_session_ctx(session) - ds4_session_pos(session)`（[ds4_cli.c:468](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L468)）。

**协作式取消**——前端注册回调：

```c
static bool ds4_session_cancelled(ds4_session *s) {
    return s && s->cancel && s->cancel(s->cancel_ud);
}
```

（[ds4.c:26184-26186](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26184-L26186)）

`ds4_session_sync` 在 prefill/decode 的循环边界检查它；命中时**保留** `checkpoint_valid=true`（见 [ds4.c:26803-26808](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.c#L26803-L26808)），意味着中断后 session 停在一个合法的 token 前缀上，下次可以继续。这正是 u2-l2 提到的「Ctrl+C 把 KV 停在合法前缀」的实现基础。声明处的注释（[ds4.h:229-231](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L229-L231)）也强调：取消只在「checkpoint 未变 或 代表一个合法 token 前缀」的安全边界被检查。

> 区分两种进度回调：`ds4_session_set_progress` 上报的 `prefill_chunk` 是**真实 prefill 边界**；而 `ds4_session_set_display_progress`（[ds4.h:226-228](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L226-L228)）只是 UI 用、可能在 chunk 内部细碎上报，**不能**当作持久 KV checkpoint 边界。

#### 4.3.4 代码实践：状态迁移表预测

> **实践目标**：给一串操作，预测每步之后 `pos()` 与「下次 sync 走哪条分支」。

**操作步骤**：设 `ctx_size` 足够大，初始为新 session。逐步执行下表左侧操作，填右侧。

| 步骤 | 操作 | `pos()` | `checkpoint_valid` | 下次 sync(prompt=A) 走哪条分支？ |
|------|------|---------|--------------------|----------------------------------|
| 0 | 新建 session | 0 | false | ? |
| 1 | `sync([A,B,C])` 成功 | 3 | true | ? |
| 2 | `eval(D)` 成功 | 4 | true | ? |
| 3 | `invalidate()` | ? | ? | ? |
| 4 | `sync([A,B,C,D,E])` 成功 | ? | ? | ? |
| 5 | `rewind(2)` | ? | ? | ? |

其中 `A=[A,B,C,D,E]`（即每步给定的 prompt 都视作 `[A,B,C,D,E]`，token 字面用字母示意）。

**预期结果（答案）**：
- 步骤 0：`pos=0`，valid=false，sync 走**重建分支**（valid 为假）。
- 步骤 1：`pos=3`，valid=true，sync([A,B,C,D,E])：checkpoint `[A,B,C]` 是其前缀 → 走**分支 A（续 prefill 后缀 [D,E]）**。
- 步骤 2：`pos=4`（`[A,B,C,D]`），valid=true，sync：`[A,B,C,D]` 是 `[A,B,C,D,E]` 前缀 → 分支 A，后缀 `[E]`。
- 步骤 3：`invalidate` → `pos=0`，valid=false，sync 走**重建分支**。
- 步骤 4：`pos=5`，valid=true，sync([A,B,C,D,E])：完全相等，后缀 0 → 分支 A 但**不做任何计算**直接返回。
- 步骤 5：`rewind(2)` → `pos=2`，valid 仍 true，sync([A,B,C,D,E])：`[A,B]` 是其前缀 → 分支 A，后缀 `[C,D,E]`。

**应当观察的现象**：步骤 5 揭示了 `rewind` 的「逻辑截断」性质——valid 不变、pos 缩到 2，于是 sync 把 `[C,D,E]` 当后缀重算。但如前所述，对 GPU 滑动窗口后端，这种 rewind 的 KV 正确性需要后端配合；生产路径更常用 invalidate+sync。

> ⚠️ 待本地验证：步骤 5 在真实 GPU 后端上是否产生与「从 [A,B] 重新 prefill」逐位一致的 logits，取决于 raw SWA 窗口在截断点是否仍持有必要行；本表只描述 session 字段层面的迁移。

#### 4.3.5 小练习与答案

**练习 1**：`ds4_session_invalidate` 和 `ds4_session_rewind(0)` 都会让 `pos()` 变成 0，它们等效吗？

**参考答案**：不等效。`invalidate` 把 `checkpoint_valid` 置 false，下次 sync 必然整段重建；`rewind(0)` 只把 `checkpoint.len` 置 0 但**保留** `checkpoint_valid`，语义上声称「KV 在空序列处有效」。前者是「我不信任 KV」，后者是「我相信 KV 可以从空开始重放」。在当前实现里，rewind 后 sync 会因 `checkpoint.len==0` 走「后缀 == 整个 prompt」的分支 A（等价于一次完整 prefill），但这条路径与 invalidate+sync 的语义动机不同。

**练习 2**：为什么 `ds4_session_sync` 在被取消时返回 `DS4_SESSION_SYNC_INTERRUPTED`（2）而不是普通的错误码 1？

**参考答案**：错误码 1 表示「失败、KV 不可信」，调用方应当作废重来；中断码 2 表示「在安全边界停下、KV 停在合法前缀」。区分两者让前端（如 CLI 的 Ctrl+C）可以「原地继续」而不必丢弃已算的 prefill——只需下次用相同/更长的 prompt 再 sync 一次即可续上。

---

## 5. 综合实践

把三个模块串起来。假设你正在为一个新前端写一个简单的多轮对话循环，请用本讲学到的原语设计状态迁移，**只读源码、不写真代码**：

**场景**：用户连续发三条消息，第二条发完后你发现需要把第一条的措辞改一个字（模拟服务器工具调用规范化）。

**任务**：

1. 画出三条消息里每次「用户输入 → 你调用的 session 原语 → sync 走的分支 → 代价（prefill 哪些 token）」的表格。
2. 对「改措辞」那一步，明确写出你会调用 `ds4_session_common_prefix` 得到 `common`，再用 `ds4_session_rewrite_from_common` 还是直接 `invalidate`+`sync`，并说明理由。
3. 指出在哪一步 `ds4_session_pos` 与 `ctx_size` 一起决定了「还能生成多少 token」。

**参考思路**：

- 第一条消息：新 session，`sync(prompt1)` 走重建分支，prefill 全部 prompt1，`pos=len1`。
- 第二条消息：`transcript = prompt1 + msg2`，`sync(transcript)` 走分支 A，只 prefill msg2 后缀。
- 「改措辞」：新 prompt 与旧 checkpoint 共享前缀直到改动点，改动点在 `live_len` 之前 → `common < live_len` → `rewrite_from_common` 返回 `REBUILD_NEEDED`。新前端若无磁盘 KV 快照可回退，最简单稳妥的就是 `invalidate()` + `sync(new_prompt)` 走整段重建（若有快照则优先加载快照再 sync 后缀，仿照服务器 `canonicalize_tool_checkpoint`）。
- 「还能生成多少」：每次生成前 `room = ctx_size - pos()`，生成循环里 `eval` 每步让 `pos` +1，直到 `room` 用尽或命中 EOS。

把这张表和你的设计写进笔记，对照 `run_chat_turn`（[ds4_cli.c:1082-1210](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_cli.c#L1082-L1210)）与 `canonicalize_tool_checkpoint`（[ds4_server.c:9821-9889](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9821-L9889)）检查你的设计是否与真实前端一致。

---

## 6. 本讲小结

- `ds4_session` 用 `checkpoint`（token 向量）+ `checkpoint_valid`（布尔）这对字段刻画「KV 当前对应哪段序列、可不可信」，一切复用都围绕它展开。
- `ds4_session_sync` 是「复用 vs 重建」的总开关：checkpoint 是 prompt 的前缀就只评估后缀（后缀≥4 走续 prefill 图、否则逐 token decode），否则整段重建；重建前先把状态作废，失败安全。
- `ds4_session_common_prefix` 供前端**测量**公共前缀长度（用于进度显示与改写判定），与 `sync` 内部用的「严格前缀判定」`ds4_tokens_starts_with` 互补。
- `ds4_session_rewrite_requires_rebuild` 用一行 `common < live_len` 表达「在末尾追加安全、在中间改写必须重建」，根因是压缩 KV 的前沿一旦越过改写点就无法廉价回退。
- `invalidate / rewind / eval / pos` 是四个低电平原语，分别作废、逻辑截断、单步前进、读取位置；它们对 `checkpoint_valid` 的不同处理决定了下次 sync 的走向。
- 协作式取消返回 `DS4_SESSION_SYNC_INTERRUPTED`（2）而非错误 1，保证中断停在合法 token 前缀、可原地续算——这是 CLI Ctrl+C 行为的实现基础。

---

## 7. 下一步学习建议

本讲只讲了「同步算法」，没有展开被同步的对象本身。建议接下来：

- **u3-l1（GGUF 内存映射加载）**：看 `model_open` 如何把权重 mmap 进来——session 共享的 engine 权重就是这里加载的。
- **u4-l2（KV 缓存设计）**：本讲反复提到的 raw 滑动窗口、compressed KV、compressor/indexer frontier 在这里展开，理解之后你会明白为什么「中间改写」如此昂贵。
- **u7-l5（实时 KV 前缀复用与检查点改写）**：服务器如何在无状态 HTTP 上把本讲的 `common_prefix` + `rewrite_from_common` + 磁盘快照组合成一条工业级复用链。
- **u8 单元（磁盘 KV 缓存与序列化）**：`rewrite_from_common` 回退链里那个 `kv_cache_try_load_text` 的底层文件格式与淘汰策略。

如果想在源码里继续扎根，可直接顺着本讲的链接读 `ds4.c` 第 27065 行起的 `eval_internal`（decode 主路径）与第 26897 行起的改写注释块——它们是本讲两条主线的自然延伸。
