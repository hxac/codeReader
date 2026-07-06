# 服务器架构与线程模型

## 1. 本讲目标

学完本讲，你应当能够：

- 画出 `ds4-server` 从「监听端口 → 接受连接 → 解析请求 → 推理 → 写回 SSE」的完整线程结构，并指出每一步跑在哪条线程上。
- 说清楚为什么「请求解析并发、推理串行」——也就是客户端线程与单一 graph worker 的分工，以及二者之间用什么数据结构（FIFO 作业队列）和同步原语（互斥锁 + 条件变量）衔接。
- 解释「单活 KV session」这条核心约束：服务器进程内只有一条可变推理时间线（一个 `ds4_session`），所有无状态客户端靠**前缀复用**而非各自重算来提速。
- 理解 `--chdir` 的真正用途：让 Metal 内核等相对运行时文件能从项目树解析出来。

本讲是「HTTP 服务器」单元（u7）的第一篇，只讲**架构骨架与线程模型**，不展开各端点的请求解析细节（u7-l2）、SSE 流式（u7-l3）、工具调用（u7-l4）与前缀复用的字节级比对（u7-l5）。

## 2. 前置知识

在进入服务器代码前，请确认你已经掌握以下概念（它们在前置讲义中已建立）：

- **`ds4_engine` 与 `ds4_session` 的边界**（u2-l1）：engine 是「已加载模型」，进程级、基本只读；session 是「一条可变推理时间线」，对话级、持有 KV 缓存与 logits。本讲的关键就是：**整个服务器只持有一个 engine 和一个 session**。
- **prefill 与 decode**（u1-l5、u4-l3）：prefill 是一次性把提示填进 KV 缓存（决定首 token 延迟，吞吐量级大），decode 是自回归逐 token 生成。理解二者耗时量级不同，才能理解「长 prompt 客户端为何会拖慢整条队列」。
- **`ds4_session_sync` 与前缀复用**（u2-l3）：当 session 的 checkpoint 是新提示的严格前缀时，只增量评估后缀；否则整段重建。服务器正是靠这个机制让无状态客户端「重发整段对话」也不必从 token 0 重新 prefill。
- **POSIX 线程基础**：`pthread_create`、`pthread_mutex_t`、`pthread_cond_t`、`pthread_join`/`pthread_detach`，以及「生产者—消费者」用条件变量等待队列的经典写法。本讲会用到，但会顺带解释。

一句话复习：**服务器 = 把「一个 engine + 一个 session」用线程池和 HTTP 层包起来，对外假装成多并发服务。**

## 3. 本讲源码地图

本讲涉及的源码文件：

| 文件 | 角色 | 本讲用到的地方 |
|------|------|----------------|
| [ds4_server.c](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c) | HTTP 服务器主体，约 1.6 万行 | `struct server`、`struct job`、`worker_main`、`enqueue`/`dequeue`、`client_main`、`main`、`generate_job`、`parse_options` |
| [ds4.h](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h) | 引擎公共边界 | `ds4_session_create`、`ds4_session_sync`、`ds4_session_common_prefix` 等声明 |
| [README.md](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/README.md) | 项目向导 | 服务器章节对线程模型与单活 KV 的官方说明 |

阅读建议：先读 `main`（自顶向下看初始化顺序），再读 `worker_main` + `enqueue`/`dequeue`（看消费侧），最后读 `client_main`（看生产侧），三者合起来就是完整的线程模型。

## 4. 核心概念与源码讲解

### 4.1 线程模型：客户端线程 + 单一 graph worker

#### 4.1.1 概念说明

`ds4-server` 是一个「假并发」服务器。它对外能同时接很多 HTTP 连接，但**真正跑推理的只有一条线程**——graph worker。其余线程只负责读 HTTP、解析 JSON、把活儿排进队列、等结果、再写回 socket。

为什么这样设计？因为推理用的 KV 缓存与 GPU 图状态是**单一、可变、昂贵**的资源（见 u4-l2、u5-l2）。如果让多条线程同时碰 session，要么得加巨量细粒度锁，要么得复制几份 KV（内存爆炸）。ds4 选择了最简单也最安全的方案：**推理串行，解析并发**。官方在 README 里说得很直白：

> Request parsing and sockets run in client threads, but inference itself is serialized through one graph worker. The current server does not batch multiple independent requests together; concurrent requests wait their turn on the single live graph/session.

因此整个进程里有三种线程：

1. **主线程（main）**：初始化、监听、`accept` 连接、为每个连接派生一条客户端线程，并在退出时协调关闭。
2. **客户端线程（client_main）**：每条 HTTP 连接一条，解析请求后把作业塞进队列，然后阻塞等待 worker 完成。
3. **graph worker（worker_main）**：全局唯一，从队列里取作业、跑 `generate_job`（prefill + decode + 流式写回）、标记完成，循环往复。

#### 4.1.2 核心流程

整体是一个经典的「生产者—消费者」模型，生产者是客户端线程，消费者是唯一的 worker：

```
        ┌──────────────── main 线程 ────────────────┐
        │  signal / parse_options / chdir            │
        │  ds4_engine_open  →  engine (唯一)          │
        │  ds4_session_create → session (唯一)        │
        │  pthread_create(worker_main)   ← 启动 worker│
        │  listen_on(host, port)                     │
        │  while (!stop) {                           │
        │      fd = accept(lfd)                      │
        │      pthread_create(client_main, fd)  ← 每连接一线程
        │      pthread_detach(th)                    │
        │  }                                         │
        │  // 关闭：stopping=true → 召醒 worker → join
        └─────────────────────────────────────────────┘

  client 线程 A          client 线程 B          client 线程 C
   读 HTTP               读 HTTP                读 HTTP
   解析 JSON             解析 JSON              解析 JSON
   enqueue(job A) ──┐   enqueue(job B) ──┐    enqueue(job C) ──┐
   wait(j.cv)       │   wait(j.cv)       │    wait(j.cv)       │
                   ▼                   ▼                      ▼
              ┌──────────── 共享 FIFO 作业队列 (head→tail) ────────────┐
              └────────────────────────────────────────────────────────┘
                                          │ dequeue()
                                          ▼
                              ┌──── graph worker（唯一）────┐
                              │  generate_job(A) → 标记 done │
                              │  generate_job(B) → 标记 done │
                              │  generate_job(C) → 标记 done │
                              └──────────────────────────────┘
```

同步原语一览（都定义在 `struct server` 里）：

| 原语 | 保护对象 | 谁加锁 |
|------|----------|--------|
| `mu` + `cv` | 作业 FIFO 队列（`head`/`tail`）、`stopping` 标志 | worker（dequeue）与所有 client（enqueue） |
| `mu` + `clients_cv` | 活跃 client 计数 `clients` | main、client 线程（仅用于优雅关闭等待） |
| 每个 job 自带 `mu` + `cv` | 该 job 的 `done` 标志 | client（等待）与 worker（标记完成） |
| `tool_mu` | tool memory 与各类 live_tool_state | worker 与解析阶段的 client |
| `trace_mu` | trace 文件 | worker |

注意：**`session` 本身没有任何锁**。因为它只在 worker 这一条线程里被读写，天然互斥，无需保护——这是「单一 worker」架构带来的最大简化。

#### 4.1.3 源码精读

**核心数据结构 `struct server`** —— 整个服务器的共享状态都在这里：

[ds4_server.c:7713-7736](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7713-L7736) 定义了服务器结构体。注意三个关键点：① 只有一个 `ds4_session *session`（[L7715](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7715)）；② 作业队列用链表 `head`/`tail` 表示（[L7728-L7729](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7728-L7729)）；③ 一组互斥锁与条件变量保护队列与计数（[L7724-L7727](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7724-L7727)）。

**作业对象 `struct job`** —— 注意它是「栈上持有」的：

[ds4_server.c:7741-7748](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7741-L7748)。注释 [L7738-L7740](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7738-L7740) 解释了一个精巧的设计：job 由客户端线程在**栈上**分配，worker 在写完响应后才标记完成，因此 client 线程在 `done` 之前不会返回、栈帧不会失效，于是不需要为每个请求堆分配 job 对象。`fd`、`req`、`done`、自带的 `mu`/`cv`、链表指针 `next` 构成全部字段。

**入队 `enqueue`**（生产者侧）：

[ds4_server.c:11039-11050](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11039-L11050)。加锁 → 若正在关闭则拒绝（返回 false，client 会回 503）→ 否则把 job 追加到队尾 → `pthread_cond_signal(&s->cv)` 叫醒可能在睡眠的 worker → 解锁。这是教科书式的「往条件变量队列里推一个元素并 signal」。

**出队 `dequeue`**（消费者侧）：

[ds4_server.c:11052-11065](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11052-L11065)。加锁 → `while (!s->head && !s->stopping) pthread_cond_wait(&s->cv, &s->mu);`（队空且未停机时睡眠等待）→ 醒来后若仍无 job（说明是 `stopping` 触发的唤醒）返回 NULL → 否则摘下队头、返回。注意这里的 `while` 而非 `if`：条件变量标准用法，防御虚假唤醒（spurious wakeup）。

**worker 主循环 `worker_main`**：

[ds4_server.c:11067-11079](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11067-L11079)。`for(;;)` 里：dequeue 一个 job → 若为 NULL（收到停机信号）则 `break` 退出 → 否则 `generate_job(s, j)` 跑完整个推理与响应写出 → 加 job 自己的锁、置 `j->done = true`、signal `j->cv` 叫醒正在等它的 client 线程。这一段是整个并发模型的「心脏」：**串行处理，一气呵成**。

**客户端线程 `client_main`**（生产者侧）：

[ds4_server.c:11247-11345](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11247-L11345)。它的职责分两段：

1. **解析段（并发）**：`read_http_request` 读请求（[L11254](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11254)），然后按路径分发到 `parse_anthropic_request` / `parse_chat_request` / `parse_responses_request` / `parse_completion_request`（[L11285-L11296](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11285-L11296)）。这段是 CPU/IO 密集但不碰 GPU，多条 client 线程可以真正并行。
2. **提交段（阻塞）**：在**栈上**构造 `job j`（[L11319-L11324](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11319-L11324)），`enqueue` 入队，然后 `while (!j.done) pthread_cond_wait(&j.cv, &j.mu);`（[L11335](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11335)）——**client 线程在此挂起，把执行权交给 worker**，直到 worker 跑完 `generate_job` 把 `done` 置真。最后销毁 job 的锁/条件变量、释放请求、`close(fd)`、`client_done(s)`。

**主线程 `main` 的派生逻辑**：

[ds4_server.c:11768-11816](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11768-L11816) 是线程派生核心：`pthread_create(&worker, NULL, worker_main, &s)` 启动**唯一**的 worker（[L11769](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11769)）；随后 `accept` 循环里，对每个连接 `s.clients++`（[L11803](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11803)）并 `pthread_create(&th, NULL, client_main, ca)`（[L11806](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11806)），紧接着 `pthread_detach(th)`（[L11815](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11815)）。detach 意味着 main 不主动 join client 线程，它们的资源在结束时自动回收；main 只通过 `s.clients` 计数器在关闭时等它们排空（见 [L11828-L11830](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11828-L11830)）。

#### 4.1.4 代码实践

**实践目标**：亲眼确认「请求解析并发、推理串行」这一架构，并推理出它对长 prompt 客户端的后果。

**操作步骤（源码阅读型）**：

1. 打开 [ds4_server.c:11067](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11067) 的 `worker_main`，确认它是一个 `for(;;)` 单线程循环，逐个 `generate_job`，中间没有任何 `fork` 或第二条 worker。
2. 打开 [ds4_server.c:11335](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11335)，确认 client 线程在 `while (!j.done) pthread_cond_wait(...)` 处**阻塞**——也就是说 client 提交作业后什么也不做，纯等。
3. 回答两个问题（写在笔记里）：
   - 为什么说「解析并发」？依据是 [L11806](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11806) 每连接一条 `client_main` 线程，它们各自的 `read_http_request` + `parse_*_request` 互不阻塞。
   - 为什么说「推理串行」？依据是全局只有一个 worker（[L11769](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11769)），且 `dequeue`/`generate_job` 是顺序执行。

**需要观察的现象 / 预期结论**：因为推理串行，**队头阻塞（head-of-line blocking）不可避免**。设想客户端 A 发了一个 8 万 token 的长 prompt（prefill 要跑十几秒），客户端 B 紧随其后发了一个只问「你好」的小 prompt——B 哪怕能 100% 命中缓存，也必须排在 A 的 `generate_job` 之后，等 A 整段 prefill（甚至 decode）跑完。对长 prompt 客户端意味着：**它的首 token 延迟 ≈ 它在队列里前面所有作业的推理耗时之和**，而不只是自己的 prefill 时间。这正是 README 所说「concurrent requests wait their turn on the single live graph/session」的工程后果。

**运行验证（可选，待本地验证）**：在有模型与 GPU 的机器上启动 `./ds4-server`，用两个终端同时发 `curl` 请求（一个长 prompt、一个短 prompt，带 `--no-buffer` 观察 SSE 首字节时间）。预期短请求的首字节被长请求拖住。若无 GPU，可用 CPU 后端 `make cpu` 后 `./ds4-server --backend cpu` 做定性观察（速度很慢但能复现排队现象）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `pthread_create(&worker, ...)` 改成创建两个 worker，会发生什么？

**参考答案**：会出 bug。两个 worker 会同时进入 `generate_job`，操作**同一个** `s->session` 与 GPU 状态，而 session 没有任何锁保护，KV 缓存、checkpoint、logits 会被并发踩烂。要做多 worker，必须先给 session 加细粒度锁，或多份 session（内存翻倍）。ds4 选择单 worker 正是为了避开这个复杂性。

**练习 2**：`dequeue` 里为什么用 `while (!s->head && !s->stopping)` 而不是 `if`？

**参考答案**：防止 POSIX 条件变量的**虚假唤醒**——`pthread_cond_wait` 可能在没有 signal 的情况下返回。用 `while` 重新检查谓词，确保醒来时队列真的有货（或确实要停机），这是条件变量等待的标准写法。

**练习 3**：job 为什么可以放在 client 线程的**栈**上，而不必 `malloc`？

**参考答案**：因为 client 线程在 `enqueue` 之后立刻 `pthread_cond_wait(&j.cv, ...)` 阻塞，直到 worker 把 `j.done` 置真才继续；这期间栈帧始终有效。worker 通过 `j->fd` 直接把响应写回 socket，写完才 signal，所以不存在「client 已返回、job 栈已销毁、worker 还在用」的窗口。注释 [ds4_server.c:7738-7740](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L7738-L7740) 明确说明了这个设计。

### 4.2 单活 KV session：前缀复用与内存单 checkpoint

#### 4.2.1 概念说明

无状态的 HTTP API（OpenAI/Anthropic 风格）有一个固有特点：**客户端每次请求都把整段历史对话重发一遍**，服务器不该假设自己记得上一轮。但 DeepSeek V4 的 prefill 极贵（长上下文要算几万 token），如果每个请求都从 token 0 重新填 KV，成本不可接受。

ds4 的折中方案是：**进程内只保留一个活的 KV checkpoint（一个 session）**，靠 `ds4_session_sync` 的前缀复用能力（见 u2-l3）来吸收「重发整段对话」的开销。当新请求的提示恰好是当前 checkpoint 的前缀扩展时，只增量 prefill 后缀即可。README 把这一点说得很清楚：

> The server keeps one mutable backend/KV checkpoint in memory, so stateless clients that resend a longer version of the same prompt can reuse the shared prefix instead of pre-filling from token zero.

这条约束也带来一个限制：**由于内存只有一份活 KV，切换到完全不相关的新会话时，旧 checkpoint 只能靠磁盘 KV 缓存恢复**（见 u8）。README 也点明了：

> For RAM reasons there is currently only one live KV cache in memory. When a new unrelated session replaces it, the old checkpoint can only be resumed without re-processing if it was written to the disk KV cache.

#### 4.2.2 核心流程

`generate_job`（worker 处理一个作业的入口）的前缀复用决策是一条**多级回退链**，从最便宜、最精确的命中，逐级下探到最昂贵的冷 prefill：

```
generate_job(j):
  old_pos = session 当前 checkpoint 长度
  common  = ds4_session_common_prefix(session, j.prompt)   # 活 checkpoint 与新提示的公共前缀长度

  # 1. 协议级活状态续接（Responses / Anthropic / thinking 的可见文本或工具 id）
  cached = responses_live_visible_prefix_prompt(...)
  cached = responses_live_continuation_prompt(...)         # 工具结果直接续接
  cached = anthropic_live_continuation_prompt(...)
  cached = thinking_live_visible_prefix_prompt(...)
  cached = live_text_prefix_prompt(...)                     # 渲染字节级前缀命中

  # 2. 若活 checkpoint 命中（common == old_pos 且新提示是它的扩展），直接用 token 前缀
  if (cached == 0):
      cached = (common == old_pos && prompt.len >= old_pos) ? common : 0

  # 3. 都没命中：落盘当前 checkpoint（evict），再去磁盘 KV 找一个能恢复的快照
  if (cached == 0 && kv.enabled && old_pos >= min_tokens):
      kv_cache_store_current(s, "evict")
  # ... 磁盘命中则 load_snapshot 替换 session；否则冷 prefill

  # 最终都归结到一次 ds4_session_sync —— 它内部决定「增量评估后缀」还是「整段重建」
  ds4_session_sync(session, prompt_for_sync, ...)
  # 之后进入 decode 循环，逐 token 生成并 SSE 写回
```

这条链的**前提**是 session 全局唯一且只在 worker 线程访问，所以 `generate_job` 可以放心地查询、改写 `s->session`，无需任何 session 锁。这也是「单活 session」与「单 worker」两个设计互为因果的地方：单 worker 让单 session 无锁可行；单 session 又让多 worker 没必要。

#### 4.2.3 源码精读

**session 的创建——全局唯一**：

[ds4_server.c:11727-11738](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11727-L11738)。main 里 `ds4_session_create(&session, engine, cfg.ctx_size)` 只调用**一次**，然后 `s.session = session` 赋给服务器结构体。整个进程生命周期内，`s->session` 这个指针指向的对象就是唯一的活 KV 时间线。

**`generate_job` 的前缀探测**：

[ds4_server.c:9991-9995](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9991-L9995)。函数开头先取 `old_pos`（当前 checkpoint 长度），再用 `ds4_session_common_prefix(s->session, &j->req.prompt)` 算出新提示与活 checkpoint 的公共前缀长度 `common`。这两个数构成了后续所有缓存判定的基础。

**协议活状态优先（注释非常关键）**：

[ds4_server.c:10008-10013](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10008-L10013) 的注释点明 Responses API 的设计意图：一个「通过可见转录本或工具 call id 与上一轮活输出绑定」的请求，不必证明精确 token 前缀匹配就能续接。这之所以成立，正是因为 session 全局唯一——上一轮的活前沿还在原地。

**活 checkpoint 命中——最便宜的快路径**：

[ds4_server.c:10063-10066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10063-L10066)。当所有协议级续接都没命中时，退而检查 `common == old_pos && j->req.prompt.len >= old_pos`：即「活 checkpoint 恰好是新提示的前缀」——这正是 `ds4_session_sync` 能做**增量评估**（只 prefill 后缀）的情形，所以记为 `memory-token` 命中。

**落盘当前 checkpoint 再找磁盘快照**：

[ds4_server.c:10097-10102](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10097-L10102)。若活 KV 没命中，且当前 checkpoint 足够长、磁盘 KV 开启，则先 `kv_cache_store_current(s, "evict")` 把当前会话存盘——否则一旦后续命中一个更旧前缀的磁盘快照替换掉 session，较新的会话状态会被悄悄丢弃。这是「内存只有一份活 KV」这一约束的直接补偿机制。

**最终归一为 `ds4_session_sync`**：

[ds4_server.c:10251](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10251)。无论前面走了哪条路径，最后都会调一次 `ds4_session_sync(s->session, prompt_for_sync, ...)`。`ds4_session_sync` 的契约（见 u2-l3、[ds4.h:246-249](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4.h#L246-L249)）是：若 checkpoint 是新提示的前缀则只评估后缀，否则整段重建。也就是说，服务器把「是否命中」的判定上移到了 `generate_job`（为了配合磁盘/协议级回退），而把「增量 vs 重建」的底层执行交给 `ds4_session_sync`。

#### 4.2.4 代码实践

**实践目标**：理解「重发整段对话」为何不会每次都从 token 0 prefill。

**操作步骤（源码阅读型）**：

1. 在 [ds4_server.c:9995](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9995) 确认每个请求开头都算 `common = ds4_session_common_prefix(...)`。
2. 在 [ds4_server.c:10063-10066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10063-L10066) 确认：只要活 checkpoint 是新提示前缀，就标记 `memory-token` 命中，后续 `ds4_session_sync` 只评估后缀。
3. 设想一个三轮对话场景：客户端每次发「sys + 第1轮 + 第2轮 + 第3轮问题」，三轮的提示互为前缀扩展。回答：第二轮请求时 `common` 等于多少？需要 prefill 哪一段？

**预期结果**：第二轮请求时 `common = 第一轮结束时 checkpoint 长度`，只需增量 prefill「第二轮新增内容」。第三轮同理，每次只 prefill 增量。这就是「无状态 API + 单活 session + 前缀复用」三者配合省下 prefill 开销的全部秘密。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `s->session` 不需要一把专用的锁？

**参考答案**：因为只有 graph worker 这一条线程会调用 `ds4_session_*` 读写 session。客户端线程只做 HTTP 解析和队列操作，从不直接碰 session。「单 worker」让「单 session」天然互斥，省去了细粒度锁及其带来的死锁/竞态风险。

**练习 2**：如果两个完全不相关的客户端交替发请求（A 会话、B 会话、A 会话……），每次都会命中活 KV 吗？

**参考答案**：不会。第一次切到 B 时，活 KV 里还是 A 的 checkpoint，B 的提示不是 A 的前缀，活 checkpoint 不命中。`generate_job` 会先 `kv_cache_store_current(s, "evict")` 把 A 存盘，再尝试从磁盘恢复 B（如果之前存过），否则冷 prefill。这就是「内存一份活 KV、磁盘做会话间恢复」的设计——频繁切换不相关会话会反复触发 evict/restore，效率不高，但正确。

**练习 3**：`ds4_session_common_prefix` 返回的 `common` 与 `old_pos` 满足什么关系时，`ds4_session_sync` 一定走「增量评估后缀」而非「整段重建」？

**参考答案**：当 `common == old_pos`（活 checkpoint 完全落在公共前缀内）且 `prompt.len >= old_pos`（新提示是 checkpoint 的扩展）时，session 的 checkpoint 是新提示的严格前缀，`ds4_session_sync` 只评估后缀。这正是 [ds4_server.c:10064](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L10064) 判定的条件。

### 4.3 运行时文件解析：--chdir 与相对路径

#### 4.3.1 概念说明

ds4 的 Metal 后端有一个特殊之处（见 u5-l2）：`metal/*.metal` 内核源码**不参与 `make` 编译**，而是在进程启动时被拼成大源码、由 Metal 运行时即时编译。这意味着运行 `ds4-server` 的进程必须在能找到 `metal/*.metal` 的工作目录里——否则 Metal 后端启动会失败。

问题来了：用户可能从任意目录启动 `ds4-server`（比如从 `/tmp` 或某个 agent 工作目录）。为了让「相对运行时文件」始终能从项目树解析，ds4 提供了 `--chdir` 选项：在打开引擎之前先 `chdir` 到项目根目录。README 在两处都强调了这一点：

> Use `--chdir /path/to/ds4` when launching `ds4-server` from another directory, so relative runtime files such as `metal/*.metal` resolve from the project tree.

#### 4.3.2 核心流程

```
main:
  cfg = parse_options(argc, argv)          # --chdir <dir> → cfg.chdir_path
  if (cfg.chdir_path):
      chdir(cfg.chdir_path)                 # 改变进程工作目录（失败则退出）
  ds4_engine_open(engine, cfg.engine)       # 此后引擎打开模型 + Metal 运行时编译 metal/*.metal
                                            #           ↑ 相对路径相对的是 chdir 后的目录
```

关键点：`chdir` 发生在 `ds4_engine_open` **之前**。因此引擎打开模型文件、Metal 后端扫描 `metal/` 目录、ROCm 后端 `#include` 的头文件（编译期已嵌入，不涉及）等所有「相对路径」操作，都以 chdir 后的工作目录为基准。

注意区分两类「相对路径」：

| 路径类型 | 何时解析 | `--chdir` 是否影响 |
|----------|----------|-------------------|
| `metal/*.metal` 运行时内核源码 | 进程启动时（Metal 后端初始化） | 是，必须在 chdir 后能找到 |
| `-m/--model` 指定的 GGUF 路径 | `ds4_engine_open` 时 | 是，若用相对路径则相对 chdir 后目录 |
| `--kv-disk-dir` 磁盘 KV 目录 | 服务器运行期持续读写 | 是，若用相对路径则相对 chdir 后目录 |
| `--trace` trace 文件 | 服务器启动时打开 | 是 |

#### 4.3.3 源码精读

**选项解析**：

[ds4_server.c:11569-11570](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11569-L11570)。`--chdir` 取一个参数存入 `c.chdir_path`。该字段定义在 `server_config` 结构体里（[ds4_server.c:11396](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11396)）。

**应用 chdir（顺序至关重要）**：

[ds4_server.c:11705-11710](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11705-L11710)。`parse_options` 之后、`ds4_engine_open`（[L11713](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11713)）之前，执行 `chdir(cfg.chdir_path)`；失败则打印错误并 `return 1` 退出。把这一步放在引擎打开之前，正是为了让随后所有相对路径解析都落在项目树里。

**默认模型路径**：

[ds4_server.c:11515-11516](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11515-L11516)。默认 `model_path = "ds4flash.gguf"`（一个相对路径）。若用户不带 `-m` 启动，引擎会去当前工作目录找 `ds4flash.gguf`——这进一步说明为什么从别的目录启动时需要 `--chdir`（或显式 `-m 绝对路径`）。

#### 4.3.4 代码实践

**实践目标**：验证 `--chdir` 的作用顺序与失败行为。

**操作步骤（源码阅读型 + 推演）**：

1. 打开 [ds4_server.c:11705-11713](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11705-L11713)，确认 `chdir` 在 `ds4_engine_open` 之前。
2. 设想用户执行 `cd /tmp && /path/to/ds4-server`（不带 `--chdir`、不带 `-m`）：追踪 `model_path` 默认值 `ds4flash.gguf`，引擎会在 `/tmp/ds4flash.gguf` 找模型——大概率找不到而报错；即便模型存在，Metal 后端还会在 `/tmp/metal/*.metal` 找内核源码，也会失败。
3. 改为 `cd /tmp && /path/to/ds4-server --chdir /path/to/ds4`：`chdir` 把工作目录切到项目树，模型与 `metal/*.metal` 都能被相对路径找到。

**需要观察的现象 / 预期结果**：`--chdir` 让「从任意目录启动」成为可能，且对用户透明——它只是进程级的 `chdir`，之后所有相对路径自然指向项目树。运行验证（待本地验证）：在有 Metal 后端的 macOS 上分别带/不带 `--chdir` 从 `/tmp` 启动，对比启动日志（不带会报找不到 `metal/*.metal` 或模型文件）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `chdir` 必须在 `ds4_engine_open` 之前，而不能之后？

**参考答案**：`ds4_engine_open` 内部会 mmap 模型文件、并（对 Metal 后端）在进程启动时即时编译 `metal/*.metal` 内核——这些操作都用相对路径。若 `chdir` 在其后，引擎打开时工作目录还没切，相对路径解析会失败。顺序错了，`--chdir` 就形同虚设。

**练习 2**：用户既可以用 `--chdir /path/to/ds4`，也可以用 `-m /abs/path/model.gguf`。二者解决的是同一个问题吗？

**参考答案**：不完全是。`-m` 只解决模型文件路径；但 Metal 后端还需要 `metal/*.metal` 内核源码，这部分没有单独的命令行选项，只能靠工作目录。因此从非项目目录启动 Metal 后端时，`--chdir` 是更彻底的解法（同时覆盖模型与内核），`-m` 只覆盖模型。CPU 后端不依赖 `metal/*.metal`，所以用 `-m` 绝对路径就足够。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「架构追踪」任务：

**场景**：你在 `/home/me/work` 目录下启动服务器：`/opt/ds4/ds4-server --chdir /opt/ds4 --port 8000 --kv-disk-dir ./kv`。随后两个客户端几乎同时各发一个请求：客户端 A 发一个 5 万 token 的长 prompt，客户端 B 发一个 100 token 的短 prompt。

**任务**：用一张时序图（或编号步骤）回答以下问题，每步都要引用本讲讲过的源码位置作为依据：

1. main 线程做了哪些初始化？`--chdir` 在哪一步生效，`./kv`（相对路径）最终落在哪个真实目录？（提示：[ds4_server.c:11705-11710](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11705-L11710)、`--chdir` 对其后所有相对路径生效）
2. A 和 B 各自的 client 线程分别在哪一步并发、哪一步开始阻塞？阻塞在哪条条件变量上？（提示：解析段并发 [ds4_server.c:11285-11296](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11285-L11296)，阻塞在 `j.cv` [ds4_server.c:11335](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11335)）
3. worker 先处理谁？假设 A 的 enqueue 早于 B（FIFO），B 的首 token 最早在什么时刻出现？（提示：串行处理 [ds4_server.c:11067-11079](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L11067-L11079)）
4. 处理 A 时，`generate_job` 算出的 `common` 大概率是多少？此时活 KV 是空的（刚启动），A 会走哪条路径？（提示：[ds4_server.c:9991-10066](https://github.com/antirez/ds4/blob/80ebbc396aee40eedc1d829222f3362d10fa4c6c/ds4_server.c#L9991-L10066)，冷 prefill）
5. 处理 B 时，活 KV 里已经是 A 的 5 万 token checkpoint。B 的 100 token 提示是它的前缀吗？B 会命中 `memory-token` 快路径吗？如果不会，B 会发生什么？（提示：除非 B 提示恰为 A 提示前缀，否则不命中；可能触发 evict 把 A 存盘再冷 prefill B）

**参考结论要点**：
1. main 完成 signal→parse→`chdir("/opt/ds4")`→`engine_open`→`session_create`→启动 worker→listen。`./kv` 因为是 chdir **之后**解析的相对路径，落在 `/opt/ds4/kv`。
2. A、B 的 `read_http_request`+`parse_*_request` 并发；各自 `enqueue` 后都在 `while(!j.done) pthread_cond_wait(&j.cv,&j.mu)` 阻塞。
3. FIFO 下 worker 先处理 A；B 的首 token 最早出现在 A 的整个 `generate_job`（5 万 token prefill + 其 decode）完成之后——典型的队头阻塞。
4. 启动初 `old_pos = 0`，`common = 0`，A 走冷 prefill（`ds4_session_sync` 整段重建）。
5. 一般 B 不是 A 的前缀（不同对话），`common == old_pos` 不成立，B 不命中 `memory-token`；此时会先把 A 的 checkpoint 以 `evict` 原因存入 `/opt/ds4/kv`，再为 B 冷 prefill。这恰好印证了 README「内存一份活 KV、磁盘做会话间恢复」的设计。

## 6. 本讲小结

- `ds4-server` 是「假并发」服务器：**请求解析并发（每连接一条 client 线程），推理串行（全局唯一 graph worker）**，二者用一个 FIFO 作业队列 + 条件变量衔接。
- 作业对象 `job` 由 client 线程在**栈上**持有，client 在 `pthread_cond_wait(&j.cv, ...)` 阻塞直到 worker 写完响应置 `j.done`，因此无需堆分配、无需 session 锁。
- worker 的 `worker_main` 是单线程 `for(;;)` 循环：`dequeue → generate_job → 标记 done`。全局只有一个 worker 是因为只有一个 session——两个设计互为因果。
- 进程内**只有一个活的 `ds4_session`**（单 KV checkpoint）。`generate_job` 用一条多级回退链（协议活状态 → 活 token 前缀 → 磁盘快照 → 冷 prefill）做前缀复用，让无状态客户端「重发整段对话」也不必每次从 token 0 prefill。
- `session` 本身无锁，因为它只在 worker 线程被读写；锁只用于作业队列、client 计数、per-job 完成信号、tool memory 与 trace 文件。
- `--chdir` 在 `ds4_engine_open` **之前**执行进程级 `chdir`，让 `metal/*.metal` 等相对运行时文件、默认模型 `ds4flash.gguf`、`--kv-disk-dir` 等相对路径都从项目树解析。
- 串行推理带来**队头阻塞**：一个长 prompt 请求会拖住其后所有排队请求的首 token 延迟，ds4 目前不做 continuous batching。

## 7. 下一步学习建议

本讲建立的是「骨架」。接下来按依赖顺序深入：

- **u7-l2 OpenAI/Anthropic/Responses 端点**：看 client 线程解析段里 `parse_chat_request` 等如何把 JSON 映射成 `request req`，以及四类端点路由。
- **u7-l3 SSE 流式输出与 thinking 模式**：看 `generate_job` 在 prefill/decode 过程中如何向 socket 写 SSE 事件、如何处理长 prefill 时的 keepalive。
- **u7-l5 实时 KV 前缀复用与检查点改写**：本讲只点到 `common` 与多级回退，u7-l5 会展开 token 前缀检查、渲染字节比对、`ds4_session_rewrite_from_common` 与 rebuild 判定的字节级细节。
- **u8 磁盘 KV 缓存与序列化**：本讲提到「内存一份活 KV、磁盘做会话间恢复」，u8 讲 KVC 文件格式、四种保存时机与淘汰评分。
- 若想理解 `ds4_session_sync` 增量 vs 重建的底层判定，回看 **u2-l3**；想理解 prefill 为何昂贵，回看 **u4-l2 / u6-l1**。
