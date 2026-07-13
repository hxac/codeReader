# 事件主循环 process_events_and_timers

## 1. 本讲目标

本讲是第五单元「事件驱动核心」的收尾篇。前面四讲分别建立了事件骨架（u5-l1）、epoll 后端（u5-l2）、连接管理（u5-l3）、定时器与 posted 队列（u5-l4），但都把同一个函数当作黑盒来引用——`ngx_process_events_and_timers`。本讲打开这个黑盒。

读完本讲，你应当能够：

1. 说清 worker 进程「一轮循环」的完整调度顺序：算超时 → 抢 accept 锁 → 阻塞等事件 → 处理 accept 事件 → 放锁 → 触发定时器 → 处理普通事件。
2. 解释 accept 互斥锁（accept mutex）如何避免「惊群」，以及 `ngx_accept_disabled` 如何在 worker 之间做简单的连接负载均衡。
3. 理解 accept 事件、定时器、普通事件这三类工作为何要排成这样的优先级，而不是一股脑处理。

## 2. 前置知识

本讲默认你已经读完 u5-l1 到 u5-l4。为了衔接，先回顾几个关键概念：

- **worker 是一个事件循环**：master 用 `ngx_spawn_process` 派生出若干 worker（见 u4-l1/u4-l2），每个 worker 既不 fork 也不开线程，而是反复调用同一个函数处理事件。信号只负责「置标志位」，循环体在每轮之间检查这些标志位。
- **事件后端是一张函数指针表**：`ngx_event_actions`（u5-l1）里的 `process_events` 槽在 Linux 上指向 `ngx_epoll_process_events`（u5-l2）。上层用宏 `ngx_process_events` 间接调用它，屏蔽后端差异。
- **定时器是一棵红黑树**（u5-l4）：节点 key 是绝对毫秒时间戳，最小节点最早到期。
- **posted 队列分两级**（u5-l4）：`ngx_posted_accept_events`（accept 类）与 `ngx_posted_events`（普通类），分别用 `ngx_queue_t` 串成链表。
- **共享内存自旋锁 shmtx**（u4-l3）：一条原子变量做的进程间互斥锁，`ngx_shmtx_trylock` 非阻塞、`ngx_shmtx_lock` 阻塞、`ngx_shmtx_unlock` 释放。

还有一个名词需要先建立直觉：**惊群（thundering herd）**。当多个 worker 都把同一个监听套接字加进各自的 epoll，一个新连接到来时，内核会唤醒所有 worker，但只有一个 `accept` 成功，其余得到 `EAGAIN` 白跑一趟。worker 越多，这种无效唤醒的 CPU 浪费越严重。accept 互斥锁就是 nginx 解决这个问题的经典手段（现代内核还有 `EPOLLEXCLUSIVE`、`SO_REUSEPORT` 等替代方案，但锁机制仍是源码里最完整的一条路径）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/event/ngx_event.c` | 定义本讲主角 `ngx_process_events_and_timers`，以及 accept mutex 的全局变量、worker 启动时是否开启 accept mutex 的判定 |
| `src/event/ngx_event_accept.c` | 定义 `ngx_trylock_accept_mutex`（抢/让锁）、`ngx_event_accept`（accept 时刷新负载均衡信号 `ngx_accept_disabled`） |
| `src/event/ngx_event_posted.c` | 定义 `ngx_event_process_posted`（排空一个 posted 队列）、`ngx_event_move_posted_next`（延迟写优化） |
| `src/event/ngx_event_timer.c` | 定义 `ngx_event_find_timer`（算最近超时）、`ngx_event_expire_timers`（触发到期定时器） |
| `src/os/unix/ngx_process_cycle.c` | 定义 worker 进程循环 `ngx_worker_process_cycle`，是 `ngx_process_events_and_timers` 的唯一长期调用者 |

四个宏/常量也值得关注，都在 `src/event/ngx_event.h`：

- `NGX_UPDATE_TIME`（值 1）、`NGX_POST_EVENTS`（值 2）：传给后端的标志位，见 [src/event/ngx_event.h:481-482](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L481-L482)。
- `ngx_process_events` 宏：展开为 `ngx_event_actions.process_events`，见 [src/event/ngx_event.h:400](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L400)。
- `NGX_TIMER_INFINITE`：表示「没有定时器、可以无限阻塞」，见 [src/event/ngx_event_timer.h:17](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L17)。

## 4. 核心概念与源码讲解

### 4.1 ngx_process_events_and_timers：一轮循环的完整调度顺序

#### 4.1.1 概念说明

worker 进程一旦初始化完毕，就进入一个无限循环，循环体核心只有一行——调用 `ngx_process_events_and_timers`。这个函数是 worker 的「心跳」：每被调用一次，worker 就完成「等待事件 → 处理一批事件 → 处理超时」这一轮工作，然后回到循环顶部检查信号标志位（`ngx_terminate`/`ngx_quit`/`ngx_reopen` 等）。

调用点在 worker 进程循环里，见 [src/os/unix/ngx_process_cycle.c:710-748](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L710-L748)。简化后是这个形状：

```c
/* src/os/unix/ngx_process_cycle.c: ngx_worker_process_cycle 的循环体 */
for ( ;; ) {
    if (ngx_exiting) {
        if (ngx_event_no_timers_left() == NGX_OK) {   // 优雅退出前确认无未完成定时器
            ngx_worker_process_exit(cycle);
        }
    }
    ngx_process_events_and_timers(cycle);              // 本讲主角，第 721 行
    if (ngx_terminate) { ... ngx_worker_process_exit(cycle); }   // 强制退出
    if (ngx_quit)     { ... 关监听端口、关空闲连接 ... }          // 优雅退出
    if (ngx_reopen)   { ... 重新打开日志 ... }
}
```

注意几个要点：

- 信号处理函数（u4-l2 的 `ngx_signal_handler`）**只置标志位**，真正的动作（关端口、重开日志）发生在两轮循环之间的检查里。这意味着 worker 最多延迟「一轮循环」的时间响应信号。
- 优雅退出（`ngx_quit`）依赖 `ngx_event_no_timers_left()`（见 [src/event/ngx_event_timer.c:99-126](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L99-L126)）：只有当所有剩余定时器都标记为 `cancelable`（可抛弃）时，worker 才允许真正退出，否则继续跑循环等它们自然完成。
- 单进程模式（`ngx_single_process_cycle`，[src/os/unix/ngx_process_cycle.c:297-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L297-L331)）和缓存管理进程（`ngx_cache_manager_process_cycle`，[src/os/unix/ngx_process_cycle.c:1121-1135](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1121-L1135)）也复用同一个 `ngx_process_events_and_timers`，只是它们不开 accept mutex。

#### 4.1.2 核心流程

`ngx_process_events_and_timers` 一次调用分成五个阶段，顺序固定：

1. **算超时 `timer` 与标志 `flags`**：决定这轮要在 `epoll_wait` 里最多阻塞多久、要不要顺带刷新时间缓存。
2. **抢 accept 锁**（若开启）：决定本轮是否由本 worker 负责接收新连接，并据此调整 `flags` 与 `timer`。
3. **处理延迟写队列**（若非空）：把上一轮因写缓冲满而推迟的事件提前到本轮，并强制 `timer=0` 不阻塞。
4. **阻塞等事件 `ngx_process_events`**：进入后端（epoll），阻塞至多有事件就绪或 `timer` 到期；后端会把就绪事件或直接处理、或投递到 posted 队列。
5. **按固定优先级善后**：先排空 accept 事件队列 → 释放 accept 锁 → 触发到期定时器 → 最后排空普通事件队列。

用伪代码描述这条主干（省略 `timer_resolution` 与延迟写分支）：

```
timer = 最近一个定时器的剩余时间
if 启用了 accept_mutex:
    if ngx_accept_disabled > 0:    # 本 worker 已经过忙，主动让出
        ngx_accept_disabled--
    else:
        ngx_trylock_accept_mutex()
        if 拿到锁: flags |= NGX_POST_EVENTS   # 让后端把事件投递、而非就地处理
        else:        timer = min(timer, accept_mutex_delay)  # 没拿到，尽快重试

记录时间戳 delta = ngx_current_msec
ngx_process_events(timer, flags)            # 阻塞等事件（可能就地处理普通事件）
delta = ngx_current_msec - delta            # 本轮事件处理耗时

ngx_event_process_posted(accept_events)     # ① 优先处理 accept 事件
if 持有 accept 锁: ngx_shmtx_unlock()       # ② 立刻放锁
ngx_event_expire_timers()                   # ③ 触发到期定时器
ngx_event_process_posted(events)            # ④ 最后处理普通事件
```

最关键的设计意图：**accept 锁必须尽快释放**。所以接收新连接（accept 事件）被排在最前面处理，处理完立刻放锁，把「重活」（定时器回调、请求处理）放到放锁之后。否则一个正在处理慢请求的 worker 会长时间霸占 accept 锁，导致新连接无人接收。

#### 4.1.3 源码精读

主角函数完整定义在 [src/event/ngx_event.c:194-264](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L194-L264)。分段精读：

**(a) 算超时与标志**——[src/event/ngx_event.c:200-217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L200-L217)：

```c
if (ngx_timer_resolution) {
    timer = NGX_TIMER_INFINITE;      /* 用 SIGALRM 周期性刷新时间，epoll 无限阻塞 */
    flags = 0;
} else {
    timer = ngx_event_find_timer();  /* 最近一个定时器的剩余毫秒 */
    flags = NGX_UPDATE_TIME;         /* 醒来后刷新时间缓存 */
}
```

`ngx_timer_resolution` 对应配置指令 `timer_resolution`（见 [src/event/ngx_event.c:512](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L512)）。两种模式的区别：

- **默认模式**：`timer = ngx_event_find_timer()`，即「睡到下一个定时器到期或事件到来，取早者」。`NGX_UPDATE_TIME` 让后端在 `epoll_wait` 返回后调一次 `ngx_time_update()` 刷新时间缓存（u2-l5），因为阻塞期间时间可能已经走了很多。
- `timer_resolution` 模式：让 `epoll_wait` 无限阻塞（只在有真实事件时返回），时间由一个独立的 `SIGALRM` 定时器周期性刷新（设置见 [src/event/ngx_event.c:698-723](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L698-L723)，handler 见 [src/event/ngx_event.c:622-631](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L622-L631)）。代价是定时器精度退化为 `timer_resolution` 毫秒，好处是减少 `gettimeofday` 调用。

`ngx_event_find_timer` 本身很简洁，见 [src/event/ngx_event_timer.c:32-50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L32-L50)：取红黑树最小节点的 `key - ngx_current_msec`，已过期则返回 0（立即返回，不阻塞）。

**(b) 抢 accept 锁**——[src/event/ngx_event.c:219-239](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L219-L239)：

```c
if (ngx_use_accept_mutex) {
    if (ngx_accept_disabled > 0) {
        ngx_accept_disabled--;              /* 本 worker 偏忙，跳过抢锁，逐步恢复 */
    } else {
        if (ngx_trylock_accept_mutex(cycle) == NGX_ERROR) {
            return;                          /* 启用 accept 事件失败，本轮作废 */
        }
        if (ngx_accept_mutex_held) {
            flags |= NGX_POST_EVENTS;        /* 拿到锁：让后端投递事件而非就地处理 */
        } else {
            if (timer == NGX_TIMER_INFINITE
                || timer > ngx_accept_mutex_delay) {
                timer = ngx_accept_mutex_delay;   /* 没拿到锁：至多睡 delay ms 再试 */
            }
        }
    }
}
```

这段是 4.2 的入口，这里只需记住它对 `flags`/`timer` 的两种修改：拿到锁就加 `NGX_POST_EVENTS`，没拿到就把阻塞上限压到 `ngx_accept_mutex_delay`（默认 500ms，见 [src/event/ngx_event.c:1370](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1370)）。

**(c) 延迟写队列**——[src/event/ngx_event.c:241-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L241-L244)：

```c
if (!ngx_queue_empty(&ngx_posted_next_events)) {
    ngx_event_move_posted_next(cycle);
    timer = 0;                               /* 有积压的延迟写，本轮不阻塞 */
}
```

这部分细节见 4.3。

**(d) 阻塞等事件并测耗时**——[src/event/ngx_event.c:246-253](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L246-L253)：

```c
delta = ngx_current_msec;
(void) ngx_process_events(cycle, timer, flags);   /* = ngx_epoll_process_events */
delta = ngx_current_msec - delta;                  /* 本轮事件处理实际耗时 */
```

`delta` 会被 debug 日志打印为 `"timer delta: %M"`。它衡量的是「从进入后端到后端返回并就地处理完所有未投递事件」的耗时——这是评估 worker 是否被慢请求拖累的关键观测点。

**(e) 固定优先级善后**——[src/event/ngx_event.c:255-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L255-L263)：

```c
ngx_event_process_posted(cycle, &ngx_posted_accept_events);  /* ① accept 优先 */
if (ngx_accept_mutex_held) {
    ngx_shmtx_unlock(&ngx_accept_mutex);                      /* ② 立刻放锁 */
}
ngx_event_expire_timers();                                    /* ③ 定时器 */
ngx_event_process_posted(cycle, &ngx_posted_events);          /* ④ 普通事件 */
```

四个动作的顺序就是本讲的「调度定律」，务必记牢。

#### 4.1.4 代码实践

**实践目标**：用 debug 日志亲眼看到 worker 一轮循环的真实顺序。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx（编译方式见 u1-l2）。
2. 准备最小配置 `nginx.conf`：

   ```nginx
   worker_processes  2;
   events { worker_connections 1024; }
   http {
       access_log off;
       server { listen 8080; location / { return 200 "hello\n"; } }
   }
   ```

3. 在 `error_log` 上开 debug：`error_log logs/error.log debug;`。
4. 启动 nginx，发一个请求：`curl http://127.0.0.1:8080/`。

**需要观察的现象**：在 `logs/error.log` 里，每一轮循环会留下这样一串 debug 信息（按时间顺序）：

- `timer delta: …`——`ngx_process_events_and_timers` 在 [src/event/ngx_event.c:252-253](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L252-L253) 打印的本轮耗时。
- `posted event …`——`ngx_event_process_posted` 在 [src/event/ngx_event_posted.c:29-30](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L29-L30) 每处理一个 posted 事件打印一条。
- `event timer del: …` / `event timer add: …`——定时器的增删（[src/event/ngx_event_timer.c:78-82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L78-L82) 与 [src/event/ngx_event_timer.h:80-82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L80-L82)）。

**预期结果**：你会看到这些日志按「timer delta → posted event（accept 类）→ event timer … → posted event（普通类）」的大致顺序成组出现，呼应 4.1.2 的五阶段。

> 待本地验证：不同 nginx 版本与配置下，是否开 `accept_mutex` 会影响 `accept mutex locked/failed` 日志是否出现（见 4.2）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_process_events_and_timers` 里要先 `ngx_event_process_posted(&ngx_posted_accept_events)` 再 `ngx_shmtx_unlock`，而把 `ngx_event_process_posted(&ngx_posted_events)` 放在最后？

**答案**：accept 锁的设计目标是「持有时间尽量短」。accept 事件（接收新连接）必须趁持锁时处理，因为只有持锁者监听了端口；处理完立刻放锁，让别的 worker 有机会接收。普通事件（已有连接的读写、请求处理）可能很慢，且不依赖 accept 锁，所以放到放锁之后，避免拖累锁的释放。

**练习 2**：`delta = ngx_current_msec - delta` 测量的是什么？为什么 `ngx_current_msec` 不需要加锁保护就能读？

**答案**：测量的是「进入后端 `ngx_process_events` 到它返回（含就地处理的所有未投递事件）」的耗时。`ngx_current_msec` 是单变量，每个 worker 只更新自己的副本（实际上是同一地址、但每个 worker 各写各的，互不读对方的中间态），且写者是 worker 自己、读者也是 worker 自己，天然单线程访问，无需锁（时间缓存机制见 u2-l5）。

---

### 4.2 ngx_trylock_accept_mutex：accept 互斥锁驯服惊群

#### 4.2.1 概念说明

当多个 worker 共享同一组监听端口时，「谁来 `accept`」是个问题。如果每个 worker 都把监听 fd 加进自己的 epoll，新连接到来会唤醒所有 worker（惊群）。accept 互斥锁的思路很简单：**同一时刻只允许一个 worker 把监听 fd 挂在 epoll 上**，这个 worker 由一把共享内存自旋锁（`ngx_accept_mutex`，u4-l3）竞争产生。

是否启用这把锁，在 worker 启动时的 `ngx_event_process_init` 里一次性判定，见 [src/event/ngx_event.c:649-656](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L649-L656)：

```c
if (ccf->master && ccf->worker_processes > 1 && ecf->accept_mutex) {
    ngx_use_accept_mutex = 1;
    ngx_accept_mutex_held = 0;
    ngx_accept_mutex_delay = ecf->accept_mutex_delay;
} else {
    ngx_use_accept_mutex = 0;
}
```

三个条件：master 模式、多于一个 worker、配置 `accept_mutex on;`。注意当前版本 `accept_mutex` 默认是 **off**（[src/event/ngx_event.c:1369](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1369)），因为现代 Linux 内核的 `EPOLLEXCLUSIVE`、`SO_REUSEPORT` 已经能在内核层缓解惊群（见 [src/event/ngx_event.c:921-937](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L921-L937)）。但锁机制是理解 nginx 进程协作最完整的一条路径，仍值得精读。

锁相关的全局变量都在 [src/event/ngx_event.h:460-465](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L460-L465)，核心几个：

- `ngx_accept_mutex`：shmtx 锁对象本身。
- `ngx_accept_mutex_held`：本 worker 当前是否持有锁（1 持有 / 0 未持有）。
- `ngx_accept_mutex_delay`：没抢到锁时，下次重试前的最长等待（默认 500ms）。
- `ngx_accept_disabled`：负载均衡信号，正值表示本 worker 偏忙、应主动让出抢锁机会。
- `ngx_accept_events`：标志位，表示持锁期间监听 fd 的 epoll 注册需要刷新。

#### 4.2.2 核心流程

`ngx_trylock_accept_mutex` 是一次「非阻塞」的抢锁尝试，逻辑分两支：

```
if ngx_shmtx_trylock(锁) 成功:          # 拿到锁
    if 本 worker 本来就持锁 且 无待刷新事件:
        直接返回 OK                       # 维持现状，零开销
    否则:
        ngx_enable_accept_events()        # 把监听 fd 加进 epoll
        ngx_accept_mutex_held = 1
else (没拿到锁):
    if 本 worker 之前持锁:                # 状态由「持锁」变「失锁」
        ngx_disable_accept_events()       # 把监听 fd 从 epoll 摘下
        ngx_accept_mutex_held = 0
```

两个关键点：

1. **`ngx_shmtx_trylock` 是非阻塞的**（u4-l3）：拿不到立刻返回，不会让 worker 卡住。worker 本轮拿不到就压短 `timer`（见 4.1.3 的 (b)），下一轮再来试。
2. **「拿到锁」和「把监听 fd 挂上 epoll」是两件事**：抢到锁只是获得了「接收新连接的资格」，还要调 `ngx_enable_accept_events` 真正把监听 fd 加进 epoll，之后 `epoll_wait` 才会因新连接而唤醒本 worker。反之，失去锁要 `ngx_disable_accept_events` 摘下监听 fd，这样内核就不会再为它生成「连接就绪」事件——这正是避免惊群的关键。

**负载均衡信号 `ngx_accept_disabled`**：

每当成功 accept 一个连接，`ngx_event_accept` 会刷新它，见 [src/event/ngx_event_accept.c:139-140](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L139-L140)：

\[ d = \left\lfloor \frac{N}{8} \right\rfloor - F \]

其中 \(N\) 是 `cycle->connection_n`（本 worker 的连接总数，即 `worker_connections`），\(F\) 是 `free_connection_n`（当前空闲连接数）。\(d\) 即 `ngx_accept_disabled`。

- 当空闲连接充足（\(F > N/8\)），\(d < 0\)：本 worker「乐意」继续接收，每轮正常抢锁。
- 当空闲连接吃紧（\(F < N/8\)），\(d > 0\)：本 worker「偏忙」，主循环里的 `if (ngx_accept_disabled > 0) ngx_accept_disabled--;` 会**跳过抢锁**，并每轮把 \(d\) 减 1，直到重新转负。

这是一个无需通信的、平滑的负载均衡：忙的 worker 自动让出 accept 资格，空闲的 worker 自然多接收。

#### 4.2.3 源码精读

`ngx_trylock_accept_mutex` 完整定义见 [src/event/ngx_event_accept.c:344-379](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L344-L379)：

```c
ngx_int_t
ngx_trylock_accept_mutex(ngx_cycle_t *cycle)
{
    if (ngx_shmtx_trylock(&ngx_accept_mutex)) {           /* 非阻塞抢锁 */
        if (ngx_accept_mutex_held && ngx_accept_events == 0) {
            return NGX_OK;                                 /* 维持持锁，无变化 */
        }
        if (ngx_enable_accept_events(cycle) == NGX_ERROR) {/* 挂监听 fd */
            ngx_shmtx_unlock(&ngx_accept_mutex);
            return NGX_ERROR;
        }
        ngx_accept_events = 0;
        ngx_accept_mutex_held = 1;
        return NGX_OK;
    }

    if (ngx_accept_mutex_held) {                           /* 之前持锁，现在丢了 */
        if (ngx_disable_accept_events(cycle, 0) == NGX_ERROR) {
            return NGX_ERROR;
        }
        ngx_accept_mutex_held = 0;
    }
    return NGX_OK;
}
```

配套的两个辅助函数：

- `ngx_enable_accept_events`（[src/event/ngx_event_accept.c:382-404](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L382-L404)）：遍历所有监听套接字，对尚未 `active` 的调 `ngx_add_event` 加入 epoll。
- `ngx_disable_accept_events`（[src/event/ngx_event_accept.c:407-444](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L407-L444)）：对 `active` 的调 `ngx_del_event(..., NGX_DISABLE_EVENT)` 摘下。

再看主循环里消费 `ngx_accept_disabled` 的那段（已在 4.1.3 引用，[src/event/ngx_event.c:219-222](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L219-L222)）：正值跳过抢锁并自减，负值（或零）才进入 `ngx_trylock_accept_mutex`。

一个容易忽略的边界：当 `accept()` 因 fd 耗尽（`EMFILE`/`ENFILE`）失败时，`ngx_event_accept` 会主动把 `ngx_accept_disabled` 置 1（[src/event/ngx_event_accept.c:119-125](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L119-L125)），并放掉 accept 锁。这样下一个 worker 接手时，本 worker 因为 `disabled>0` 不会再立刻抢锁，避免在 fd 仍然耗尽时反复触发失败。

#### 4.2.4 代码实践

**实践目标**：对比 `accept_mutex on;` 与 `off;` 时，worker 接收连接的行为差异。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx，配置 `worker_processes 4;`。
2. **场景 A**：`events { accept_mutex on; }`，开 `error_log ... debug;`，启动后用 `ab -n 20000 -c 50 http://127.0.0.1:8080/` 制造大量短连接。
3. 用 `grep` 过滤日志里的 `accept mutex locked` 与 `accept mutex lock failed`（这两条 debug 分别在 [src/event/ngx_event_accept.c:349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L349) 与 [src/event/ngx_event_accept.c:367-368](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L367-L368)）。统计 4 个 worker 各自 `locked` 的次数。
4. **场景 B**：改为 `accept_mutex off;`（默认值），重复压测。观察是否还有 `accept mutex` 相关日志。

**需要观察的现象**：

- 场景 A：`locked` 与 `lock failed` 交替出现，且 4 个 worker 的 `locked` 次数大致均衡——锁在不同 worker 间轮转。
- 场景 B：完全没有 `accept mutex` 日志，所有 worker 都直接 accept（依赖内核的 `EPOLLEXCLUSIVE` 避免惊群）。

**预期结果**：场景 A 能直观看到「同一时刻基本只有一个 worker 持锁接收」，场景 B 看到锁机制被完全旁路。

> 待本地验证：`EPOLLEXCLUSIVE` 的支持依赖内核版本与 nginx 编译选项，若未启用，关闭 `accept_mutex` 可能出现明显的惊群（accept 失败/EAGAIN 增多）。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_trylock_accept_mutex` 返回 `NGX_ERROR` 时，主循环直接 `return`（[src/event/ngx_event.c:224-226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L224-L226)），本轮后续的 `ngx_process_events`、定时器、posted 事件都不处理了。这样会不会丢事件？

**答案**：不会丢，只是延后。`NGX_ERROR` 只在 `ngx_enable_accept_events`（把监听 fd 加入 epoll）失败时返回，属于资源类异常。本轮跳过后，下一轮循环会重新进入 `ngx_process_events_and_timers`，已就绪的连接事件仍在 epoll 就绪队列里（边缘触发下未读完的数据不会丢），定时器也仍在红黑树里。代价是这一轮不推进任何事件处理，是一种「宁可暂缓也不要在异常状态下继续」的保守策略。

**练习 2**：假设 `worker_processes 8`、`worker_connections 1024`，某 worker 当前有 980 个活跃连接（即空闲 44 个）。它的 `ngx_accept_disabled` 是多少？它会抢 accept 锁吗？

**答案**：\(N=1024\)，\(F=44\)，\(d = \lfloor 1024/8 \rfloor - 44 = 128 - 44 = 84 > 0\)。该 worker 本轮**不抢** accept 锁，并把 `ngx_accept_disabled` 自减为 83。要连续减 84 轮（期间若无新连接释放）才会重新参与抢锁。这正是「忙 worker 主动让出」的体现。

---

### 4.3 ngx_event_process_posted 与 ngx_event_expire_timers：posted 队列与定时器

#### 4.3.1 概念说明

主循环善后阶段的三个函数（`ngx_event_process_posted` 两次、`ngx_event_expire_timers` 一次）负责把「后端收集到但没就地处理的工作」真正执行掉。要理解它们，先弄清「事件为什么会进队列」。

后端 `ngx_epoll_process_events` 收到就绪事件后，有两种处理方式（由 4.1 提到的 `flags` 控制）：

- **就地处理**：直接调 `ev->handler(ev)`。默认模式（未持锁、单 worker）多走这条路。
- **投递处理**：把事件挂进 posted 队列，等 `ngx_process_events` 返回后再统一处理。当 `flags` 含 `NGX_POST_EVENTS`（即持有 accept 锁）时走这条路，目的是把可能很慢的 handler 推迟到放锁之后。

投递时按事件类型分流（u5-l2 详述）：accept 类事件进 `ngx_posted_accept_events`，普通事件进 `ngx_posted_events`。这就是为什么主循环要分别排空两个队列——它们对应不同的优先级。

至于定时器，它在事件模型里是「第三种唤醒源」。`epoll_wait` 的阻塞时长正是由「最近定时器」决定的（4.1.3 的 (a)），所以 `ngx_process_events` 返回后，可能已有定时器到期，需要 `ngx_event_expire_timers` 逐个触发。

#### 4.3.2 核心流程

**排空一个 posted 队列**——`ngx_event_process_posted`：

```
while 队列非空:
    q = 队首
    ev = 由 q 反查宿主 ngx_event_t          # 侵入式队列，offsetof 反推（u2-l3）
    从队列摘除 ev，置 ev->posted = 0
    ev->handler(ev)                          # 执行回调；回调内可能再次 post 自己
```

注意是「先摘除再调用」：handler 执行期间事件已不在队列，因此 handler 可以安全地再次 `ngx_post_event` 把自己重新挂回去（下一轮再处理），不会重复。

**触发到期定时器**——`ngx_event_expire_timers`：

```
for ( ;; ):
    node = 红黑树最小节点
    if node->key > ngx_current_msec:  return   # 最早的都没到期，停止
    ev = 由 node 反查宿主 ngx_event_t
    从红黑树删除 node，置 ev->timer_set = 0
    ev->timedout = 1                            # 关键：标记「这次是超时唤醒」
    ev->handler(ev)
```

`ev->timedout = 1` 是关键：很多 handler 同时被「I/O 就绪」和「超时」复用（同一个 `ev->handler`），它靠检查 `ev->timedout` 来区分「对端来数据了」还是「等数据等超时了」（如 u5-l1 所述）。

**延迟写队列 `ngx_posted_next_events`**——`ngx_event_move_posted_next`：

```
for 每个在 next_events 里的 ev:
    ev->ready = 1
    ev->available = -1                 # 边缘触发下「读到 EAGAIN」契约（u5-l2）
把整个 next_events 拼到 ngx_posted_events 尾部
清空 next_events
```

用途：当一个写事件因对端接收缓冲满而返回 `NGX_AGAIN`，继续在本轮重试是空转。把它丢进 `ngx_posted_next_events`，下一轮主循环开头（4.1.3 的 (c)）会把它搬进普通 posted 队列、并强制 `timer=0`，让它在「下一轮」而非「本轮」重试。这是 nginx 的延迟写优化。

#### 4.3.3 源码精读

`ngx_event_process_posted` 见 [src/event/ngx_event_posted.c:18-36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L18-L36)：

```c
void
ngx_event_process_posted(ngx_cycle_t *cycle, ngx_queue_t *posted)
{
    ngx_queue_t  *q;
    ngx_event_t  *ev;

    while (!ngx_queue_empty(posted)) {
        q = ngx_queue_head(posted);
        ev = ngx_queue_data(q, ngx_event_t, queue);   /* offsetof 反取宿主 */
        ngx_delete_posted_event(ev);                   /* 摘除 + posted=0 */
        ev->handler(ev);                               /* 执行；可能再次 post */
    }
}
```

`ngx_delete_posted_event` 是宏，见 [src/event/ngx_event_posted.h:31-37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h#L31-L37)：置 `posted=0` 并 `ngx_queue_remove`。配套的 `ngx_post_event` 宏（[src/event/ngx_event_posted.h:17-28](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h#L17-L28)）有幂等保护：已 `posted` 的事件不会重复入队。

`ngx_event_expire_timers` 见 [src/event/ngx_event_timer.c:53-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L53-L96)，核心循环：

```c
for ( ;; ) {
    root = ngx_event_timer_rbtree.root;
    if (root == sentinel) return;
    node = ngx_rbtree_min(root, sentinel);
    if ((ngx_msec_int_t) (node->key - ngx_current_msec) > 0) return; /* 未到期 */
    ev = ngx_rbtree_data(node, ngx_event_t, timer);
    ngx_rbtree_delete(&ngx_event_timer_rbtree, &ev->timer);
    ev->timer_set = 0;
    ev->timedout = 1;
    ev->handler(ev);
}
```

`ngx_event_find_timer`（决定 `epoll_wait` 阻塞上限）见 [src/event/ngx_event_timer.c:32-50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L32-L50)，与 `expire_timers` 是一对：前者算「还要等多久」，后者「把等够了的触发掉」。

`ngx_event_move_posted_next` 见 [src/event/ngx_event_posted.c:39-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L39-L60)，由主循环在 [src/event/ngx_event.c:241-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L241-L244) 调用。

#### 4.3.4 代码实践

**实践目标**：在 debug 日志里区分 accept 事件队列、定时器、普通事件队列的处理顺序。

**操作步骤**：

1. 沿用 4.1.4 的 debug 编译与配置，把 `worker_processes` 设为 `4`、`keepalive_timeout 65;`，开 `accept_mutex on;`。
2. 启动 nginx，发起一次请求 `curl http://127.0.0.1:8080/`，然后立刻 `Ctrl+C` 终止 curl（制造一次连接关闭）。
3. 在 `error.log` 中分别 grep 三类日志：
   - `grep "posted event"`：注意每条日志所属的 worker（pid），它们来自 [src/event/ngx_event_posted.c:29-30](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L29-L30)。
   - `grep "event timer del"` 与 `"event timer expire"`：来自 [src/event/ngx_event_timer.c:78-82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L78-L82)。
   - `grep "accept mutex locked"`：来自 [src/event/ngx_event_accept.c:349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L349)。
4. 按时间戳排序某一 worker 的日志片段。

**需要观察的现象**：在「持锁 worker」的一轮循环里，日志顺序大致是：

```
accept mutex locked          ← 抢到锁（4.2）
（epoll_wait 返回）
posted event ...             ← 先处理 accept 队列（接收新连接）
event timer del ...          ← 触发到期定时器
posted event ...             ← 再处理普通事件队列（读请求、写响应）
```

**预期结果**：能从日志里复现主循环「accept → 放锁 → 定时器 → 普通事件」的固定顺序，且非持锁 worker 不会有 `accept mutex locked`，其 `posted event` 也基本不含 accept 类。

> 待本地验证：日志密度与连接模式强相关，长连接场景下定时器（`keepalive_timeout`）会更显眼。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_event_process_posted` 里是「先 `ngx_delete_posted_event` 再 `ev->handler(ev)`」。如果反过来（先调 handler 再摘除），会有什么问题？

**答案**：若 handler 内部再次 `ngx_post_event(ev, q)`，由于此时 ev 还在原队列里（`posted=1`），`ngx_post_event` 的幂等判断会认为「已投递」而跳过入队（[src/event/ngx_event_posted.h:19-28](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h#L19-L28)），于是事件不会被重新调度，导致「还需要再次处理」的事件被静默丢弃。先摘除（`posted=0`）再调用，handler 重新 post 时才会真正入队。

**练习 2**：主循环里 `ngx_event_expire_timers()` 排在「accept 队列之后、普通事件队列之前」。为什么定时器要排在普通事件之前？

**答案**：定时器到期往往意味着「连接超时、需要清理」。先处理超时，可以把那些已经失效的连接回收掉，避免它们继续参与紧接着的普通事件处理（节省一次无谓的读写）。同时也保证超时语义及时生效——例如 `client_body_timeout` 到期应尽快终止请求，而不是排在排队的读写事件之后。

---

## 5. 综合实践

把本讲的三条主线串起来。**任务**：画出 `ngx_process_events_and_timers` 的完整调用顺序图，并分别标注「抢到 accept 锁」与「没抢到锁」两种情况下的处理差异。

### 步骤 1：阅读源码确认顺序

对照 [src/event/ngx_event.c:194-264](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L194-L264)，把每个动作对应的行号标在你的图上。

### 步骤 2：补全下面这张顺序图

下图为参考答案（accept mutex 开启场景）：

```
ngx_process_events_and_timers(cycle)
│
├─[1] 计算 timer / flags
│     • timer_resolution 开 → timer=INFINITE, flags=0
│     • 否则              → timer=find_timer(), flags=NGX_UPDATE_TIME
│
├─[2] accept mutex 处理（ngx_use_accept_mutex 为真时）
│     ├─ ngx_accept_disabled > 0 → 自减，跳过抢锁
│     └─ 否则 ngx_trylock_accept_mutex()
│            ├─【抢到锁】 ngx_accept_mutex_held=1, flags|=NGX_POST_EVENTS
│            └─【没抢到】 timer=min(timer, accept_mutex_delay=500ms)
│
├─[3] ngx_posted_next_events 非空 → move_posted_next(), timer=0
│
├─[4] delta=now;  ngx_process_events(timer, flags);  delta=now-delta
│     （后端 epoll_wait 阻塞；持锁时事件被投递而非就地处理）
│
├─[5] ngx_event_process_posted(&ngx_posted_accept_events)   ← accept 优先
│
├─[6] 若 ngx_accept_mutex_held → ngx_shmtx_unlock(&ngx_accept_mutex)  ← 立刻放锁
│
├─[7] ngx_event_expire_timers()                              ← 触发到期定时器
│
└─[8] ngx_event_process_posted(&ngx_posted_events)           ← 普通事件最后
```

### 步骤 3：标注两种情况的差异

把 `[2]` 节点放大，对比抢锁成功与失败的分支差异：

| 维度 | 抢到 accept 锁 | 没抢到 accept 锁 |
| --- | --- | --- |
| 监听 fd 是否在本 worker 的 epoll 上 | 是（`ngx_enable_accept_events`） | 否（若之前持锁则 `ngx_disable_accept_events` 摘下） |
| `flags` | 加上 `NGX_POST_EVENTS`，后端把就绪事件投递到队列 | 不加，后端可直接就地处理普通事件 |
| `timer` | 不变（由 find_timer 决定） | 压到 `accept_mutex_delay`（默认 500ms），尽快重试 |
| `[5]` accept 队列 | 非空（本 worker 负责接收新连接） | 通常为空 |
| `[6]` 放锁 | 执行（本 worker 持有过锁） | 跳过（`ngx_accept_mutex_held==0`） |

### 步骤 4（可选，运行验证）

用 `--with-debug` 编译，`accept_mutex on;` + `worker_processes 4;`，压测后从日志里复现上图：grep `accept mutex locked`/`lock failed` 对应 `[2]`，`posted event` 对应 `[5]`/`[8]`，`event timer del` 对应 `[7]`。

> 待本地验证：`timer delta` 的数值能反映 `[4]` 的耗时；若某 worker 长期 delta 很大，说明它被慢请求拖累，可结合本讲理解为何慢请求不会阻塞 accept（因为 accept 在 `[5]`、放锁在 `[6]`，都早于普通事件 `[8]`）。

## 6. 本讲小结

- `ngx_process_events_and_timers` 是 worker 事件循环的心跳，一次调用完成「算超时 → 抢锁 → 等事件 → accept 队列 → 放锁 → 定时器 → 普通队列」五阶段。
- 善后顺序固定为 **accept 事件 → 释放 accept 锁 → 到期定时器 → 普通事件**，核心目的是让 accept 锁持有时间最短、并优先接收新连接避免饿死。
- accept 互斥锁用一把共享内存自旋锁保证「同一时刻只有一个 worker 监听端口」，从源头消除惊群；`ngx_accept_disabled = connection_n/8 - free_connection_n` 让偏忙的 worker 主动让出抢锁机会，实现无通信的负载均衡。
- `NGX_POST_EVENTS` 标志让后端把就绪事件投递到队列而非就地处理，使持锁期间的回调延后到放锁之后执行；`NGX_UPDATE_TIME` 让后端在阻塞返回后刷新时间缓存。
- 定时器由红黑树驱动：`find_timer` 决定 `epoll_wait` 阻塞上限，`expire_timers` 触发到期者并置 `ev->timedout=1` 以区分超时唤醒与 I/O 就绪。
- `ngx_posted_next_events` + `ngx_event_move_posted_next` 是写缓冲满时的延迟写优化，把重试推迟到下一轮、避免空转。

## 7. 下一步学习建议

本讲讲清了「worker 一轮循环做什么」。至此第五单元事件核心全部完成，接下来进入第六单元 HTTP 核心处理。建议按以下顺序继续：

1. **u6-l1 HTTP 模块框架与上下文**：HTTP 层如何建立在事件层之上，`ngx_http_init_connection` 怎样成为新连接（本讲 `[5]` accept 出来的连接）的 handler。
2. **u6-l2 HTTP 请求生命周期**：一个连接被 accept 后，读事件 handler 如何在 `ngx_http_wait_request_handler` → 解析 → phases → finalize 之间切换——你会看到本讲的 posted 事件与定时器在真实请求中的具体用法（如 `client_header_timeout`）。
3. 若对性能调优感兴趣，可回头结合 **u10-l5 调试与性能分析**，用本讲提到的 `timer delta`、`accept mutex` 等 debug 日志定位 worker 的性能瓶颈。

阅读源码时，建议把 `src/event/ngx_event.c:194-264` 这一屏代码常备手边——它是理解 nginx 运行时行为最浓缩的一段。
