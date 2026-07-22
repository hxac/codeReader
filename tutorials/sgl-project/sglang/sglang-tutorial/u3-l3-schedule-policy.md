# 调度策略：LPM/FCFS/LOF 与 PrefillAdder

## 1. 本讲目标

在 u3-l1 里我们看到了调度器的事件循环与 `get_new_batch_prefill` 这个「组 prefill 批」的入口；在 u3-l2 里我们认识了 `Req` 与 `ScheduleBatch` 这两个核心数据结构。本讲要回答一个更细的问题：**当 `waiting_queue` 里积压了很多请求时，调度器到底按什么顺序把它们挑出来组成新批？挑出来之后，又凭什么判断「显存够不够、能不能真的塞进这一批」？**

学完本讲，你应当能够：

- 说清「排序」与「准入」是两件分离的事，分别由 `SchedulePolicy` 与 `PrefillAdder` 负责。
- 区分 LPM、FCFS、LOF 等策略各自的排序键与取舍，理解为什么 LPM 能提升 RadixCache 命中率。
- 看懂 `PrefillAdder` 如何用 token/内存预算决定一条请求被接受（`CONTINUE`）、因显存不足被拒（`NO_TOKEN`）、还是因批次已满被暂停（`OTHER`）。
- 手工模拟一个含共享前缀的小队列在 LPM 下的排序结果，并说出哪条请求会因预算被拒。

## 2. 前置知识

- **waiting_queue 与 running_batch**：调度器维护两个核心队列。`waiting_queue` 装的是「还没开始 prefill」的请求；`running_batch` 装的是「已经 prefill 完、正在逐 token decode」的请求。本讲只关心如何从 `waiting_queue` 抽请求进 prefill 批。
- **前缀缓存命中（prefix match）**：RadixCache（基数树）会把历史请求的公共前缀 KV 缓存下来。一条新请求进来，如果它的开头和某段已缓存内容一致，这部分 token 就不用重新算，直接复用 KV。命中的 token 数越多，这一轮 prefill 需要真正计算的新 token 就越少。
- **`Req` 上的关键字段**（u3-l2 已讲）：`origin_input_ids`（输入 token）、`output_ids`（已生成 token）、`prefix_indices`（命中的缓存下标）、`num_matched_prefix_tokens`（命中前缀长度）、`last_node`（在基数树里命中的末端节点）、`sampling_params.max_new_tokens`（期望最多生成多少 token）。
- **page（页）**：KV 显存按页管理（`page_size` 个 token 一页），所以预算估算里经常出现「按页向上取整」和「每条请求额外预留一页」的开销。

一句话心智模型：**排序决定「先服务谁」，预算决定「这一批能装下谁」**。前者追求吞吐/命中率，后者防止 OOM。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [schedule_policy.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py) | **本讲主角**。定义策略枚举、`SchedulePolicy` 排序器、`PrefillAdder` 准入器。 |
| [scheduler.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py) | 调用方。`get_new_batch_prefill` 里先 `calc_priority` 排序，再 `new PrefillAdder(...)` 逐条准入。 |
| [schedule_batch.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py) | 提供 `Req` / `ScheduleBatch`，`Req` 上承载本讲用到的 `num_matched_prefix_tokens`、`extend_range` 等字段。 |
| [server_args.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py) | `schedule_policy` 这个 CLI 字段（默认 `"fcfs"`）的来源。 |

## 4. 核心概念与源码讲解

### 4.1 策略枚举与 SchedulePolicy 总览

#### 4.1.1 概念说明

「调度策略」要解决的问题是：**给定一个等待队列，按什么顺序尝试把它们加入 prefill 批？** 顺序之所以重要，是因为 prefill 批有显存上限，排在前面的请求会优先占用预算，排后面的可能这一轮进不来。不同策略本质上是在优化不同的目标：

- **命中率优先**：让和已有缓存最「像」的请求先跑，复用尽量多的 KV → 代表是 **LPM（Longest Prefix Match，最长前缀匹配）**。
- **公平优先**：谁先到谁先跑 → **FCFS（First Come First Serve）**。
- **吞吐优先**：让输出长的请求先跑，减少长请求长期占用资源 → **LOF（Longest Output First）**。

SGLang 把策略分成两大门派，用两个枚举表示。**缓存感知（Cache-Aware）** 的策略会查询 RadixCache 来排序；**缓存无关（Cache-Agnostic）** 的策略不查缓存，排序键与缓存无关。

#### 4.1.2 核心流程

`SchedulePolicy.calc_priority` 是唯一入口，它原地（in-place）对 `waiting_queue` 排序。整体流程是：

1. `_validate_and_adjust_policy`：把字符串策略名（如 `"lpm"`）转成枚举；若 tree_cache 被禁用，强制退化为 `FCFS`。
2. `_determine_active_policy`：若当前是 LPM 且队列长度超过 128，退化成 FCFS（前缀匹配+排序太贵，得不偿失）。
3. 对缓存无关策略，若 tree_cache 支持快速匹配，先给每条请求算一次 `num_matched_prefix_tokens`（仅为后续的负载快照用，不参与排序）。
4. 按当前策略调用对应的 `_sort_by_*` 静态方法排序。

#### 4.1.3 源码精读

两个枚举定义了全部可选策略（注意 LPM/DFS_WEIGHT 属于缓存感知，其余属于缓存无关）：

[schedule_policy.py:147-161](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L147-L161) — `CacheAwarePolicy`（LPM、DFS_WEIGHT）与 `CacheAgnosticPolicy`（FCFS、LOF、RANDOM、ROUTING_KEY）两个枚举。

`calc_priority` 的分派主体，先做策略调整与前缀匹配，再用 `if/elif` 选排序函数：

[schedule_policy.py:184-235](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L184-L235) — `calc_priority`：先为缓存无关策略补算 `num_matched_prefix_tokens`，再按 `policy` 分派到 `_sort_by_longest_prefix` / `_sort_by_longest_output` / `_sort_randomly` 等。

两个关键的「策略调整」函数：

[schedule_policy.py:237-259](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L237-L259) — `_determine_active_policy`（队列过长时把 LPM 退化为 FCFS）与 `_validate_and_adjust_policy`（tree_cache 禁用时强制 FCFS，并完成字符串→枚举的解析）。

注意 `ServerArgs.schedule_policy` 的默认值是 `"fcfs"`，且 CLI 限定了几种合法取值：

[server_args.py:737-751](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L737-L751) — `schedule_policy` 字段，`choices` 含 `lpm/random/fcfs/dfs-weight/lof/priority/routing-key`，默认 `"fcfs"`。可以用 `--schedule-policy lpm` 切换。

无论哪种缓存感知策略，都要先算出每条请求命中了多少前缀。这件重复的事被抽成 `match_prefix_for_req`：

[schedule_policy.py:92-144](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L92-L144) — `match_prefix_for_req`：调用 `tree_cache.match_prefix`，把结果写入 `req.prefix_indices`、`req.last_node`、`req.num_matched_prefix_tokens` 等字段。这些字段就是后续排序的依据。

> 提示：`_determine_active_policy` 里的 `128` 这个阈值是经验值——LPM 每轮都要对整条队列做前缀匹配再排序，复杂度高于 FCFS 的纯排序，队列很大时直接退化更划算。

#### 4.1.4 代码实践

实践目标：确认默认策略与切换入口。

1. 在 [server_args.py:737-751](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/server_args.py#L737-L751) 看到 `schedule_policy` 默认是 `"fcfs"`。
2. 运行 `python -m sglang.srt.server_args --help`（或 `sglang serve --help`）找到 `--schedule-policy` 一项，确认 `choices` 列表与本讲枚举一致。
3. 待本地验证：用 `--schedule-policy lpm` 启动一次服务（小模型即可），在日志里搜索是否有前缀缓存相关输出。

预期结果：CLI 帮助里能看到 `--schedule-policy`，且合法值与源码 `choices` 一致。若本地没有 GPU，第 3 步标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果用户传了 `--schedule-policy lpm`，但部署时关掉了 tree_cache（`disable_radix_cache`），最终生效的是哪种策略？

**答案**：`_validate_and_adjust_policy` 检测到 `tree_cache.disable` 为真，会把 LPM 强制改成 `CacheAgnosticPolicy.FCFS`。即「缓存不可用时，LPM 无意义，退化成先来先服务」。

**练习 2**：为什么 `_determine_active_policy` 在队列长度 > 128 时把 LPM 退化成 FCFS？

**答案**：LPM 要先对整条队列逐条做 `match_prefix` 再按命中长度排序，开销远大于 FCFS 的纯排序；队列特别大时，排序本身可能比省下的 KV 计算还贵，得不偿失，因此退化为更轻量的 FCFS。

---

### 4.2 三大策略族精读：LPM / FCFS / LOF（及其它）

#### 4.2.1 概念说明

这一节打开 `calc_priority` 分派出去的各个 `_sort_by_*` 方法，看清每种策略到底拿 `Req` 上的哪个字段当排序键。理解了排序键，就能预测队列顺序。下面以三个「主流」策略为主，其余作为补充。

- **LPM**：排序键是命中前缀长度，命中越多越靠前。直觉：让最「省」的请求先跑，把缓存红利最大化。
- **FCFS**：排序键是到达时间戳，先到先跑。可叠加 `priority` 优先级（数值越大越优先，默认）。
- **LOF**：排序键是 `max_new_tokens`，输出越长的越靠前。直觉：长输出请求占用 decode 时间久，先把它送进 pipeline，避免长尾。

#### 4.2.2 核心流程

排序键一览（都用 Python 列表的 `sort(key=...)`，原地修改 `waiting_queue`）：

| 策略 | 排序键（升序看符号） | 含义 |
| --- | --- | --- |
| FCFS | `(priority*sign, wait_queue_entry_time)` | 先按优先级，再按到达时间 |
| LPM | `-num_matched_prefix_tokens`（被降权者取 `+inf`） | 命中前缀越长越靠前 |
| LOF | `-max_new_tokens` | 输出越长越靠前 |
| RANDOM | 随机洗牌 | 均匀打散 |
| DFS_WEIGHT | 基数树子树权重 + DFS 序 | 聚合同分支请求 |
| ROUTING_KEY | running 批中 routing_key 出现频次 | 把「同 key」请求聚合 |

LPM 里有个「批内前缀缓存（in-batch prefix caching）」优化值得单独说：如果队列里**多条请求都和已有缓存匹配很少、但彼此之间共享同一段前缀**，那么只先跑其中一条去「填」缓存，比同时跑全部更划算（其余的下一轮就能大量命中）。`_compute_prefix_matches` 用一棵模拟基数树 `waiting_queue_radix_tree` 来检测这种情况，并给「重复者」打上 `temporary_deprioritized` 标记，排序时把它们推到队尾。

#### 4.2.3 源码精读

FCFS（含可选优先级），排序键是 `(priority*sign, 到达时间)`：

[schedule_policy.py:368-378](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L368-L378) — `_sort_by_priority_and_fcfs`：`priority_sign` 决定是高优先级先（默认 `-1`）还是低优先级先。

LPM 的核心：按 `-num_matched_prefix_tokens` 排序，被降权的请求取 `float("inf")` 沉到队尾：

[schedule_policy.py:311-322](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L311-L322) — `_sort_by_longest_prefix`：命中前缀越长，`-num_matched_prefix_tokens` 越小，越靠前。

LPM 之前先算前缀匹配 + 批内去重逻辑：

[schedule_policy.py:261-309](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L261-L309) — `_compute_prefix_matches`：逐条算命中；对命中较短的请求插一棵模拟树，若已有足够多同类请求（≥ `IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD`，默认 32）就把当前请求加入 `temporary_deprioritized`。

两个阈值常量（环境变量可调）：

[schedule_policy.py:73-86](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L73-L86) — `IN_BATCH_PREFIX_CACHING_CHECK_THRESHOLD`（默认 32，命中长度 ≤ 此值才检查批内去重）与 `IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD`（默认 32，同类请求达到此数则降权）。

LOF，按 `-max_new_tokens` 排序，可选叠加优先级：

[schedule_policy.py:346-361](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L346-L361) — `_sort_by_longest_output`。

ROUTING_KEY，统计 running 批中各 `routing_key` 频次，把等待队列里「同 key」请求排前面，便于复用热数据：

[schedule_policy.py:380-411](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L380-L411) — `_sort_by_routing_key`：`sort_key` 对「命中热 key」返回 `(0, -count, key)`，其余返回 `(1, 0, key)`，从而把热 key 请求整体提前。

#### 4.2.4 代码实践

实践目标：手工模拟 LPM 排序，体会「命中前缀长度决定顺序」。

设 RadixCache 里已有一段公共前缀 `系统提示词`（对应 token 序列长 50）。当前 `waiting_queue` 有 3 条请求：

| rid | 输入内容（示意） | 命中前缀长度 `num_matched_prefix_tokens` |
| --- | --- | --- |
| A | `系统提示词` + 问题1 | 50 |
| B | `系统提示词` + 问题1 + 追问（共 80 token）| 80 |
| C | 完全不同的话题开头 | 0 |

操作步骤：

1. 套用 `_sort_by_longest_prefix` 的键 \(-\text{num\_matched\_prefix\_tokens}\)。
2. 计算：A → \(-50\)，B → \(-80\)，C → \(0\)。
3. 升序排列：\(-80 < -50 < 0\)。

需要观察的现象与预期结果：排序后顺序为 **B → A → C**。即命中前缀最长的 B 最先被尝试加入 prefill 批，从而最大化复用已有 KV、减少真正要算的 token。这正是 LPM 提升吞吐的关键。本步骤为源码阅读型推导，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：在上一节的场景里，如果用的是 FCFS 且 A 比 B 先到达，顺序会变成什么？这会带来什么差异？

**答案**：FCFS 按 `wait_queue_entry_time` 排序，顺序为 **A → B → C**。差异在于：A 命中 50、B 命中 80，FCFS 不看命中长度，可能让「命中率更低」的请求先占用 prefill 预算，整体复用的 KV 比 LPM 少。

**练习 2**：`_compute_prefix_matches` 里「批内去重」要满足哪两个阈值条件，才会把一条请求降权？

**答案**：① 该请求对已有缓存的命中长度 `len(r.prefix_indices) ≤ IN_BATCH_PREFIX_CACHING_CHECK_THRESHOLD`（默认 32），才会去查模拟树；② 在模拟树里命中长度 `≥ IN_BATCH_PREFIX_CACHING_DEPRIORITIZE_THRESHOLD`（默认 32），说明已有足够多同类请求，当前这条被降权。

---

### 4.3 PrefillAdder：准入与预算评估

#### 4.3.1 概念说明

排序只是决定了「先服务谁」，但**一条请求能不能真的进入这一轮 prefill 批，还要看显存预算够不够**。这件事由 `PrefillAdder` 负责。可以把 `PrefillAdder` 想成一个「逐条审账的会计」：调度器把排好序的队列交给它，它对每条请求算一笔账——这条请求的 prefill 输入 + 预计输出 + 页对齐开销，加起来还装得下吗？装得下就收进 `can_run_list`，并把预算相应扣减；装不下就返回一个拒收原因，调度器据此停止本轮添加。

拒收分两类原因，由枚举 `AddReqResult` 表达：

- `NO_TOKEN`：KV 显存预算真的用光了（再收就 OOM）。
- `OTHER`：输入 token 预算 / 分块预算用完，或批次已达上限，或 prefill delayer 建议本轮别再加。

#### 4.3.2 核心流程

准入一轮的总流程（在 `get_new_batch_prefill` 里）：

1. 调度器 `new PrefillAdder(...)`，传入页大小、tree_cache、KV 池分配器、running 批、`new_token_ratio`、`max_prefill_tokens`（输入预算）、`chunked_prefill_size`（分块预算）等。
2. 若有上一轮没跑完的分块请求，先 `add_chunked_req` 续跑。
3. 遍历排好序的 `waiting_queue`，对每条调用 `add_one_req`。
4. `add_one_req` 内部算 `total_tokens = cand_extend_input_len + max_new + page_size`，比对各项预算，返回 `AddReqResult`：
   - 若 `total_tokens >= rem_total_tokens` → 返回 `NO_TOKEN`。
   - 若输入预算或分块预算耗尽 / 批次已满 → 返回 `OTHER`。
   - 否则收进 `can_run_list`，调 `_update_prefill_budget` 扣减预算，返回 `budget_state()`（通常是 `CONTINUE`）。
5. 一旦返回值 ≠ `CONTINUE`，调度器 `break` 结束本轮添加，并把 `batch_is_full` 置位。

总预算的核心公式（`rem_total_tokens` 属性）：

\[
\text{rem\_total\_tokens} = \underbrace{\text{available\_size}}_{\text{池中空闲}} + \underbrace{\text{evictable\_size}}_{\text{可淘汰回收}} - \underbrace{\text{rem\_total\_token\_offset}}_{\text{已承诺给本轮的占用}}
\]

即「当前可用 + 还能从缓存里淘汰回收的 − 已经答应给别人的」。单条请求的占用开销：

\[
\text{offset} += \text{extend\_input\_len} + \text{max\_new\_tokens} + \underbrace{\text{page\_size}}_{\text{页对齐预留}} + \text{mamba\_gap\_reserve}
\]

其中 `max_new_tokens` 会被 `CLIP_MAX_NEW_TOKENS`（默认 4096）截断，避免「申请生成 1 万 token」的请求把预算吓得过于保守。

#### 4.3.3 源码精读

拒收原因枚举：

[schedule_policy.py:435-438](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L435-L438) — `AddReqResult`：`CONTINUE`（继续加）、`NO_TOKEN`（没 token 了）、`OTHER`（其它原因停止）。

`max_new_tokens` 估算的截断常量（只截断估算、不改变真正的停止条件）：

[schedule_policy.py:65-71](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L65-L71) — `CLIP_MAX_NEW_TOKENS`（默认 4096），防止服务器对长输出请求过度保守。

总预算属性 `rem_total_tokens`（按池类型分四种情况，常规情况 = `available_size + evictable_size − offset`）：

[schedule_policy.py:565-587](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L565-L587) — `rem_total_tokens` 属性：根据 `is_all_swa / is_hybrid_swa / is_hybrid_ssm_cache` 选不同的「空闲 + 可淘汰」组合，再减去 `rem_total_token_offset`。

`budget_state()` 把当前预算汇总成一个 `AddReqResult`，是「收下一条请求后」的最终判定：

[schedule_policy.py:685-706](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L685-L706) — `budget_state`：先查总 token 预算（含 SWA / Mamba 专池）是否 `<= 0` → `NO_TOKEN`；再查输入预算、分块预算等 → `OTHER`；都满足 → `CONTINUE`。

`_update_prefill_budget` 收下一请求后扣减各预算（含页开销、SWA、分块、DLLM 等多条账）：

[schedule_policy.py:708-751](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L708-L751) — `_update_prefill_budget`：把 `extend_input_len + max_new_tokens + page_size + mamba_gap_reserve` 累加进 `rem_total_token_offset` / `cur_rem_token_offset`，并扣减输入/分块/SWA/DLLM 等预算。

准入主入口 `add_one_req`，先算 `total_tokens` 并做 KV 预算闸门：

[schedule_policy.py:1012-1048](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L1012-L1048) — `add_one_req` 开头：`total_tokens = cand_extend_input_len + max_new + page_size`，若 `total_tokens >= rem_total_tokens` 直接返回 `NO_TOKEN`；之后还有 SWA 池、输入预算、分块等多道闸门。

调度器一侧的调用现场（先排序、再 `new PrefillAdder`、循环 `add_one_req`）：

[scheduler.py:2914-2914](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2914-L2914) — `self.policy.calc_priority(self.waiting_queue, running_batch)`：排序。

[scheduler.py:2931-2947](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2931-L2947) — `new PrefillAdder(...)`：把页大小、tree_cache、KV 分配器、`new_token_ratio`、`max_prefill_tokens`、`chunked_prefill_size` 等传入构造准入器。

[scheduler.py:3001-3036](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3001-L3036) — 循环里 `adder.add_one_req(req, ...)`；返回值 `!= CONTINUE` 时 `break`，若是 `NO_TOKEN` 则把 `batch_is_full = True`。

#### 4.3.4 代码实践

实践目标：理解 PrefillAdder 因「预算」拒收请求的两类原因。

沿用 4.2.4 排好序的队列 **B → A → C**，并假设：

- 总预算 `rem_total_tokens = 120`，`page_size = 1`。
- B：`cand_extend_input_len = 10`（命中 80 后剩余要算的），`max_new_tokens = 50`。
- A：`cand_extend_input_len = 40`，`max_new_tokens = 50`。
- C：`cand_extend_input_len = 60`，`max_new_tokens = 60`。

操作步骤（用 `total_tokens = cand_extend_input_len + max_new + page_size` 逐条扣账）：

1. B：`total = 10 + 50 + 1 = 61`，`61 < 120` 且 `< rem_total_tokens` → 收下。扣减后 `rem_total_tokens ≈ 120 − 61 = 59`。
2. A：`total = 40 + 50 + 1 = 91`，`91 >= 59`（`total_tokens >= rem_total_tokens`）→ 返回 `NO_TOKEN`。
3. 因返回 `NO_TOKEN`，调度器 `break`，C 本轮不被尝试。

需要观察的现象与预期结果：

- **B 被收进 `can_run_list`，A 因 KV 预算不足返回 `NO_TOKEN`，C 连尝试的机会都没有**（排在 A 之后，循环已 break）。
- 若把场景换成「KV 预算充足但输入 token 预算 `rem_input_tokens` 已耗尽」，则 `budget_state()` 会返回 `OTHER`（输入预算耗尽分支），同样停止添加。
- 本实践为源码阅读型推导，无需运行命令；若要在真实服务里观察，可在本地用大 prefill 输入压满显存后查看日志中 batch 大小受限的现象，标注「待本地验证」。

> 关键直觉：`NO_TOKEN` 是「显存真的不够」的硬限制；`OTHER` 是「这一轮的策略性上限」（输入/分块预算或批大小限制）到了。两者都会让调度器停止本轮添加，但语义不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `add_one_req` 里 `max_new_tokens` 要用 `CLIP_MAX_NEW_TOKENS`（4096）截断后再算预算，而不是用用户申请的真实值？

**答案**：用户可能申请 `max_new_tokens=100000`，若按真实值预留，单条请求就会把预算几乎吃光，导致服务器过度保守、吞吐骤降。截断到 4096 只影响「预算估算」，不改变真正的停止条件（请求仍可生成到未截断的上限），是在「不过度保守」与「不轻易 OOM」之间的折中。参见源码 [schedule_policy.py:65-71](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py#L65-L71) 的注释。

**练习 2**：`AddReqResult.NO_TOKEN` 与 `AddReqResult.OTHER` 都会让调度器停止本轮添加，调度器对它们的后续处理有什么不同？

**答案**：看调度器 [scheduler.py:3010-3018](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3010-L3018)：`NO_TOKEN` 时会把 `running_batch.batch_is_full = True`（显存真的满了，后续轮也先别急着加，除非有 HiCache 等情况下会看是否已有可服务请求）；`OTHER` 只是本轮停。两者都 `break` 退出添加循环。

## 5. 综合实践

把「排序」与「准入」串起来，完整模拟一轮 `get_new_batch_prefill`。

设 RadixCache 中已有公共前缀 `P`（长度 30）。`waiting_queue` 有 3 条请求，到达顺序为 R1 → R2 → R3：

| rid | 输入命中前缀 | `cand_extend_input_len`（命中后要算的新输入） | `max_new_tokens` |
| --- | --- | --- | --- |
| R1 | 命中 30（公共前缀 P） | 20 | 40 |
| R2 | 命中 50（P + 更多公共内容） | 10 | 100 |
| R3 | 命中 0 | 60 | 50 |

设 `page_size = 1`，总预算 `rem_total_tokens = 130`，输入预算 `rem_input_tokens` 足够大（不触发 `OTHER`），关闭分块（`rem_chunk_tokens = None`）。

任务：

1. **排序阶段**：分别写出 LPM 与 FCFS 两种策略下，`waiting_queue` 被重排后的顺序。
2. **准入阶段**：在 LPM 顺序下，用 `total = cand_extend_input_len + max_new + page_size` 逐条扣减 `rem_total_tokens`，判断每条返回 `CONTINUE / NO_TOKEN / OTHER` 中的哪一个，最终 `can_run_list` 里有哪些请求。
3. **对比**：如果把策略换成 FCFS，`can_run_list` 的成员会变吗？为什么这说明 LPM 在「显存紧张 + 有共享前缀」时更优？

参考答案：

1. LPM 按 \(-\text{命中长度}\) 排：R2(50) → R1(30) → R3(0)，即 **R2 → R1 → R3**。FCFS 按到达时间排：**R1 → R2 → R3**。
2. LPM 下逐条扣账（`max_new` 不超过 4096，无需截断）：
   - R2：`total = 10 + 100 + 1 = 111 < 130` → `CONTINUE`，收下；剩余 `130 − 111 = 19`。
   - R1：`total = 20 + 40 + 1 = 61 >= 19` → `NO_TOKEN`，拒收并 `break`。
   - R3：循环已 break，不尝试。
   - 最终 `can_run_list = [R2]`。
3. FCFS 下：
   - R1：`total = 20 + 40 + 1 = 61 < 130` → `CONTINUE`，剩余 `69`。
   - R2：`total = 10 + 100 + 1 = 111 >= 69` → `NO_TOKEN`，`break`。
   - 最终 `can_run_list = [R1]`。
   - 对比：两种策略本轮都只收下 1 条（预算紧张所致），但 **LPM 选中的 R2 命中前缀更长（50 > 30）**，真正要算的新 token 更少（`cand_extend_input_len` 10 < 20），单位显存换到的「已就绪 KV」更多——这正是 LPM 在共享前缀场景下提升吞吐与命中率的核心收益。

## 6. 本讲小结

- 调度从 `waiting_queue` 抽请求分两步：**`SchedulePolicy.calc_priority` 排序**（决定先服务谁）+ **`PrefillAdder` 准入**（决定这一批装得下谁）。
- 策略分两派：缓存感知（LPM、DFS_WEIGHT，查 RadixCache）与缓存无关（FCFS、LOF、RANDOM、ROUTING_KEY，不查缓存）；tree_cache 禁用或队列过长（>128）时 LPM 会退化成 FCFS。
- LPM 按「命中前缀长度」降序排，命中越长越优先，最大化 KV 复用；FCFS 按到达时间（可叠加优先级）；LOF 按 `max_new_tokens` 降序。
- LPM 还带「批内前缀去重」：多条短命中但彼此共享前缀的请求，只先跑一条去填缓存，其余降权，下轮再大量命中。
- `PrefillAdder` 用 `rem_total_tokens = available + evictable − offset` 这套预算逐条审账；`max_new_tokens` 按 `CLIP_MAX_NEW_TOKENS`（4096）截断以免过度保守。
- 准入结果用 `AddReqResult` 表达：`NO_TOKEN`（显存不足）/ `OTHER`（输入或分块预算、批上限等策略性限制）/ `CONTINUE`（继续）；非 `CONTINUE` 即停止本轮添加。

## 7. 下一步学习建议

- 本讲只讲了 prefill 批如何从 `waiting_queue` 组成。**decode 批如何维护、KV 不足时如何回缩（retract）** 见 u3-l1 的 `update_running_batch` 与 `retract_decode`，建议对照阅读。
- **CPU-GPU 重叠（overlap）** 与 `PrefillAdder`/批结果如何流式回吐的关系，见 u3-l4。
- LPM 的命中率根基是 RadixCache 的前缀匹配；想深入「命中长度怎么算、树怎么插怎么淘汰」，进入 **u4-l1（RadixAttention 与基数树缓存）** 与 **u4-l3（前缀缓存接口与淘汰策略）**。
- `chunked_prefill_size` 在本讲表现为 `rem_chunk_tokens` 这道分块预算闸门；完整的 chunked prefill 机制与吞吐权衡见 **u7-l2（Chunked Prefill 与吞吐优化）**。
