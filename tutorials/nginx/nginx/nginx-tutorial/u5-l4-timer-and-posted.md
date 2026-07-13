# 定时器与 posted 事件

## 1. 本讲目标

本讲是第五单元「事件驱动核心」的第四篇。前面三讲我们弄清了三件事：

- 事件是什么（u5-l1，`ngx_event_t` 与 actions 接口表）；
- 事件从哪来（u5-l2，epoll 后端把就绪 fd 喂给事件）；
- 连接怎么建（u5-l3，accept 与 connection 管理）。

但还有两个问题没回答：

1. **nginx 怎么实现「超时」？** 一个请求 60 秒没传完就要被掐掉、一个 keepalive 连接 65 秒没活动就要关掉——这些「到点要做的事」由谁来管？
2. **拿到一批就绪事件后，先处理哪个、后处理哪个？** 如果一个 worker 正在处理一个又慢又大的上传请求，它会不会把别的 worker 想 accept 新连接的请求堵死？

本讲就回答这两个问题。学完后你应当掌握：

- nginx 用一棵**红黑树**管理所有定时器，能 \(O(\log n)\) 插入、\(O(1)\) 取出最早到期的那个；
- `ngx_event_add_timer` / `ngx_event_del_timer` 如何把一个事件挂上/摘下定时器，以及为什么有「懒删除」优化；
- `ngx_event_find_timer` / `ngx_event_expire_timers` 如何与事件主循环配合，决定 epoll 该等多久、以及醒来后让谁超时；
- `ngx_event_process_posted` 与 **accept / 普通** 两条 posted 队列的设计，理解它为什么能防止「连接饿死」。

## 2. 前置知识

阅读本讲前，你最好已经建立以下认知（均来自前面讲义）：

- **红黑树 `ngx_rbtree_t`（u2-l3）**：nginx 自带的平衡二叉搜索树，增删查 \(O(\log n)\)，用一个「哨兵节点」省去 NULL 判断。定时器版本（`ngx_rbtree_insert_timer_value`）的关键技巧是：节点 key 是毫秒时间戳，比较时把两值之差**转成有符号**再比，从而扛住 `ngx_current_msec` 在约 49 天（32 位）后的回绕。
- **缓存时间 `ngx_current_msec`（u2-l5）**：事件循环每轮用 `ngx_time_update()` 刷新的全局「当前单调毫秒」。定时器一律以它为基准，不调用 `gettimeofday`，避免系统调用开销。
- **事件 `ngx_event_t`（u5-l1）**：调度的原子单位。它内嵌了两个「容器节点」——`timer`（红黑树节点，用于排队等超时）和 `queue`（链表节点，用于进 posted 队列）。一个事件同时只能在一棵树或一条队列里，靠 `timer_set` / `posted` 两个标志位区分状态。
- **accept 互斥锁（u5-l1 提及、u5-l5 详讲）**：多 worker 共享监听套接字时，用一个全局自旋锁 `ngx_accept_mutex` 保证「同一时刻只有一个 worker 在 accept」，避免惊群。本讲的 posted 队列顺序就是为它服务的。

一句话复习：`ngx_event_t` 既是「I/O 就绪通知」的载体，也是「定时器到期通知」的载体，还是「posted 延迟执行」的载体——同一个结构，三种用途，靠内嵌节点和标志位切换。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/event/ngx_event_timer.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h) | 定时器对外接口与**内联函数**：`ngx_event_add_timer` / `ngx_event_del_timer`，常量 `NGX_TIMER_INFINITE` / `NGX_TIMER_LAZY_DELAY`。 |
| [src/event/ngx_event_timer.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c) | 定时器红黑树本体：`ngx_event_timer_init` 建树、`ngx_event_find_timer` 取最近到期、`ngx_event_expire_timers` 批量触发到期、`ngx_event_no_timers_left` 优雅退出时用。 |
| [src/event/ngx_event_posted.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h) | posted 队列的**宏**（`ngx_post_event` / `ngx_delete_posted_event`）与三条全局队列声明。 |
| [src/event/ngx_event_posted.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c) | posted 队列的实现：`ngx_event_process_posted` 依次回调、`ngx_event_move_posted_next` 把「下一轮」队列合并进普通队列。 |
| [src/event/ngx_event.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c) | 主循环 `ngx_process_events_and_timers`——把「取超时 → epoll → 处理 accept 队列 → 放锁 → 触发定时器 → 处理普通队列」串起来的总指挥（u5-l5 的主角，本讲引用它的顺序）。 |
| [src/event/modules/ngx_epoll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c) | epoll 后端在 `ngx_epoll_process_events` 里，按 `rev->accept` 把就绪事件**分流**到 accept 队列或普通队列，是两级队列的「入口」。 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **定时器红黑树**（find / expire）：数据怎么存、怎么取最早、怎么批量触发。
2. **挂载与摘除**（add / del）：单个事件如何进出树，以及「懒删除」优化。
3. **posted 三条队列**（process_posted / move_posted_next）：就绪事件为何不直接执行而要「投递」，accept 与普通队列的优先级。

### 4.1 定时器红黑树：find_timer 与 expire_timers

#### 4.1.1 概念说明

「定时器」在生活中就是闹钟：你设一堆闹钟，每个都有一个响铃时间；你只关心**最早响的那个**，因为它决定你「最晚能睡到几点」。其它闹钟还没到点，不用管。

nginx 的定时器就是一棵存着所有「闹钟」的红黑树：

- 每个定时器是一个 `ngx_event_t`，它的到期时间存在内嵌红黑树节点 `ev->timer.key` 里，单位是**绝对毫秒时间戳**（`ngx_current_msec + 延迟`，不是相对延迟）。
- 红黑树按 key 排序，**最左下角的节点就是最早到期的那个**（红黑树的最小值，`ngx_rbtree_min`）。
- 一棵树里允许 key 重复（同一毫秒到期的多个定时器），无所谓——因为我们只关心最小值，重复的不影响「取最早」。

> 为什么用绝对时间戳而不是相对延迟？因为相对延迟每次插入都要遍历比较、且无法快速取「全局最早」。绝对时间戳让「取最早」=「取树最小值」= \(O(\log n)\)（沿左子树一路向下）。

整个 nginx（每个 worker）只有**一棵**全局定时器树：

```c
ngx_rbtree_t              ngx_event_timer_rbtree;
static ngx_rbtree_node_t  ngx_event_timer_sentinel;
```

见 [src/event/ngx_event_timer.c:13-14](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L13-L14)。`ngx_event_timer_sentinel` 是红黑树的哨兵（u2-l3 讲过，省 NULL 判断）。树的初始化发生在 worker 启动期：

```c
ngx_rbtree_init(&ngx_event_timer_rbtree, &ngx_event_timer_sentinel,
                ngx_rbtree_insert_timer_value);
```

见 [src/event/ngx_event_timer.c:22-29](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L22-L29)。注意第三个参数是 `ngx_rbtree_insert_timer_value`——这是**定时器专用**的插入比较函数，它和普通红黑树插入的唯一区别就是：比较两个 key 时，把差值先转成**有符号**再判断。这点至关重要，4.1.2 会展开。

#### 4.1.2 核心流程：取最早与批量到期

定时器在主循环里被两个函数驱动（它们的调用位置见 4.3）：

**① `ngx_event_find_timer`——「我最多能睡多久？」**

主循环在调 `epoll_wait` 之前要告诉内核「最多阻塞多久」。这个上限就是「最早定时器还剩多久到期」：

1. 树空（根 == 哨兵）→ 返回 `NGX_TIMER_INFINITE`（=-1），意为「没有定时器，epoll 可以无限阻塞直到有 I/O」。
2. 否则取树最小节点 `node`，它的到期时间 `node->key` 减去当前时间 `ngx_current_msec`，就是剩余等待时间。
3. 差值转有符号比较：若 `> 0` 返回该值；若 `<= 0` 返回 0（已经有定时器过期了，别睡了，立刻回来处理）。

核心一行：

```c
timer = (ngx_msec_int_t) (node->key - ngx_current_msec);
return (ngx_msec_t) (timer > 0 ? timer : 0);
```

见 [src/event/ngx_event_timer.c:32-50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L32-L50)。

**为什么必须转有符号？** 寫值是两个无符号毫秒相减。假设 `ngx_current_msec` 在第 49.7 天回绕归零，而某定时器的 key 还停留在回绕前的大值，直接做无符号减会得到一个巨大的正数（下溢），误判为「很久之后才到期」，导致该定时器永远不被触发。转成有符号后，正确的负差值会被识别为「已过期」。这就是 u2-l3 提到的「定时器用有符号差值比较处理 49 天回绕」。用数学语言：

\[
\text{diff} = ( \text{node}\!\to\!\text{key} - \text{ngx\_current\_msec} ) \ \text{按有符号解释}
\]

\[
\text{diff} > 0 \Rightarrow \text{尚未到期},\quad \text{diff} \le 0 \Rightarrow \text{已到期}
\]

只要两次访问的时间戳跨度小于回绕周期的一半（约 24 天），这种有符号差值就能正确区分「未来」与「过去」。

**② `ngx_event_expire_timers`——「把所有已到期的闹钟都响掉」**

主循环从 epoll 醒来后调用它。逻辑是个 `for ( ;; )` 循环：

1. 树空 → 直接返回。
2. 取最小节点。若它的 `key - ngx_current_msec > 0`（还没到期）→ 返回（因为最小的一个都没到期，后面的更不会到期，这是红黑树排序保证的）。
3. 否则该定时器已到期：
   - 从树里删除该节点；
   - 置 `ev->timer_set = 0`（标志「已不在树里」）；
   - 置 `ev->timedout = 1`（**告诉 handler：你是被超时唤醒的，不是因为 I/O 就绪**）；
   - 调用 `ev->handler(ev)`——执行到期回调。
4. 回到第 1 步，继续处理下一个到期者。

见 [src/event/ngx_event_timer.c:53-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L53-L96)。

这里有一个精妙的细节：`ev->timedout = 1` 让同一个 handler 能区分「被 I/O 唤醒」和「被超时唤醒」。例如一个读事件的 handler 发现 `ev->timedout` 为真，就知道该返回 408（Request Timeout）并关连接，而不是继续读数据。

#### 4.1.3 源码精读

**取最早到期（决定 epoll 阻塞上限）**：

[src/event/ngx_event_timer.c:38-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L38-L49) —— 先判树空返回 `NGX_TIMER_INFINITE`，再取 `ngx_rbtree_min`，用有符号差值算剩余时间，负则归零。

**批量触发到期**：

[src/event/ngx_event_timer.c:61-95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L61-L95) —— 循环体。注意第 72 行的提前返回（最小节点都没到期）、第 76 行用 `ngx_rbtree_data(node, ngx_event_t, timer)` 由节点反取宿主事件（侵入式容器手法，u2-l3）、第 92 行置 `timedout`、第 94 行调 handler。

**优雅退出时的扫尾**：

[src/event/ngx_event_timer.c:99-126](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L99-L126) —— `ngx_event_no_timers_left` 遍历全树，只要还有任意一个 `cancelable == 0`（不可取消）的定时器就返回 `NGX_AGAIN`（「还有事没做完」）；只有剩下的全是 cancelable 时才返回 `NGX_OK`。worker 在 `ngx_exiting` 退出阶段据此决定「能不能走了」。`cancelable` 的典型例子是 keepalive 空闲定时器——worker 要退出时可以直接抛弃它；而一个正在传数据的请求的超时定时器则不可取消，必须等它真超时或真完成。

#### 4.1.4 代码实践

**实践目标**：验证「定时器按到期绝对时间排序，最小者最先触发」。

**操作步骤（源码阅读型）**：

1. 打开 [src/event/ngx_event_timer.c:53-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L53-L96)，确认 `ngx_event_expire_timers` 是「取最小 → 判到期 → 删节点 → 调 handler」的循环。
2. 设想三个定时器 A、B、C，到期绝对时间分别为 `now+100`、`now+30`、`now+200`（ms）。在脑中把它们插入红黑树，问自己：`ngx_event_find_timer` 会返回多少？答：30（B 最小）。
3. 假设 epoll 阻塞了 40ms 才醒来（有 I/O 发生），此时 `ngx_current_msec` 已推进 40。再问：`expire_timers` 会被触发的有谁？答：B（`now+30` 已小于 `now+40`）触发；A、C 仍在树里。
4. 用文本编辑器在 [src/event/ngx_event_timer.c:94](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L94) 的 `ev->handler(ev);` 上方加一行打印（**仅本地实验，勿提交**）：
   ```c
   ngx_log_error(NGX_LOG_WARN, ev->log, 0,
                 "TIMER FIRED key=%M timedout=%d", ev->timer.key, ev->timedout);
   ```

**需要观察的现象**：用 `--with-debug` 编译、配置 `worker_processes 1; keepalive_timeout 65;`，用 `curl` 发一个请求后保持连接不活跃，约 65 秒后 error_log 会打印 `TIMER FIRED`，且 `timedout=1`，证明 keepalive 超时正是走这条路径。

**预期结果**：日志里定时器触发的时间间隔与 `keepalive_timeout` 吻合，`timedout` 标志为 1。

> 若无法本地编译验证，请标注「待本地验证」后只完成源码阅读部分。

#### 4.1.5 小练习与答案

**练习 1**：如果定时器红黑树里有上百万个节点，`find_timer` 的开销是多少？为什么这不会成为性能瓶颈？

**参考答案**：\(O(\log n)\)，因为取最小值是沿左子树一路向下，路径长度等于树高 \(\log_2 n\)。即便百万节点，树高也仅约 20，开销可忽略。而且 nginx 每个 worker 的活跃定时器数量通常等于活跃连接数（每连接 1~2 个），远到不了百万级。

**练习 2**：`ngx_event_expire_timers` 里第 72 行，为什么「最小的节点都没到期」就能 `return`，而不用遍历整棵树？

**参考答案**：红黑树是按 key 升序排列的二叉搜索树，最小节点的 key 全树最小。最小者未到期（`key > now`）意味着所有节点都未到期（它们的 key 都 ≥ 最小值 > now），所以无需再查。

---

### 4.2 添加与删除定时器：add_timer / del_timer 与 lazy 优化

#### 4.2.1 概念说明

谁会把一个事件挂上定时器？答案是**协议层**：HTTP 模块在读请求前调 `ngx_event_add_timer(rev, client_header_timeout)`，upstream 模块在连后端前调 `ngx_event_add_timer(..., proxy_connect_timeout)`，keepalive 在连接闲置时挂上 `keepalive_timeout`。它们的共同点是「我希望这件事在 N 毫秒内完成，否则把我唤醒去处理超时」。

`add_timer` / `del_timer` 是**内联函数**（写在头文件里，`static ngx_inline`），因为它们调用极其频繁，内联可省掉函数调用开销。

两个标志位是理解它们的关键：

- `ev->timer_set`：1 表示「该事件的 timer 节点当前在红黑树里」，0 表示不在。防止重复插入或重复删除。
- `ev->timedout`：由 `expire_timers` 在触发时置 1，由 handler 自行清 0。

#### 4.2.2 核心流程

**`ngx_event_add_timer(ev, timer)`**（`timer` 是**相对延迟**，如 60000 表示 60 秒）：

1. 算绝对到期时间：`key = ngx_current_msec + timer`。
2. **如果该事件已经在树里**（`ev->timer_set` 为真）——这是「重新设定超时」的常见场景，比如客户端慢慢发请求体，每收到一段数据就把超时往后推：
   - 算新旧 key 的差值 `diff = key - ev->timer.key`（有符号）；
   - 若 `|diff| < NGX_TIMER_LAZY_DELAY`（300ms）→ **直接返回，什么都不做**（懒优化，见 4.2.3）；
   - 否则先 `ngx_del_timer(ev)` 把旧的摘掉，再插入新的。
3. 设置 `ev->timer.key = key`，插入红黑树，置 `timer_set = 1`。

见 [src/event/ngx_event_timer.h:50-87](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L50-L87)。

**`ngx_event_del_timer(ev)`**：从树里删节点，置 `timer_set = 0`。DEBUG 模式下还会把节点的 left/right/parent 清空，方便断言查悬空指针。

见 [src/event/ngx_event_timer.h:31-47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L31-L47)。

> 注意 `del_timer` 不会清 `timedout` 标志，也不会调 handler——它只是「把闹钟撤了」。如果你撤得早，handler 永远不会被调。

#### 4.2.3 「懒删除」优化：为什么容忍 300ms 误差

[ngx_event_timer.h:19](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L19) 定义了常量：

```c
#define NGX_TIMER_LAZY_DELAY  300
```

它的作用在 `add_timer` 里：当一个事件已在树里、且新设定的到期时间与旧值相差不到 300ms 时，**跳过删旧插新**，沿用旧的定时器。

为什么值得这么做？考虑一个高速连接：客户端在传一个几百 MB 的上传请求体，nginx 每收到一个 TCP 报文就把「读超时」往后推 60 秒。如果每次推送都做一次「红黑树删除 + 插入」，那就是两次 \(O(\log n)\) 操作；而报文可能每毫秒就来一个，开销惊人。

懒删除的洞察是：**超时精度本身不需要很高**。一个 60 秒的超时，差 300ms 触发，用户根本无感；但省下的红黑树操作对高速连接是数量级的收益。所以 nginx 主动用「最多 300ms 的精度损失」换「大幅减少树操作」。

这是典型的「用可控的精度换性能」的工程取舍，源码里的注释也写明了这一点：

```c
/*
 * Use a previous timer value if difference between it and a new
 * value is less than NGX_TIMER_LAZY_DELAY milliseconds: this allows
 * to minimize the rbtree operations for fast connections.
 */
```

见 [src/event/ngx_event_timer.h:58-76](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L58-L76)。

#### 4.2.4 源码精读

**add_timer 全貌（含懒优化）**：[src/event/ngx_event_timer.h:50-87](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L50-L87)。
- 第 56 行算绝对 key；
- 第 58-76 行是「已在树里」的重新设定分支，第 68 行判 `ngx_abs(diff) < NGX_TIMER_LAZY_DELAY`；
- 第 84 行真正插入。

**del_timer 全貌**：[src/event/ngx_event_timer.h:31-47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.h#L31-L47)。第 38 行 `ngx_rbtree_delete`，第 46 行 `ev->timer_set = 0`。

#### 4.2.5 代码实践

**实践目标**：体会懒优化在「反复续期」场景下的效果。

**操作步骤（源码阅读型 + 待本地验证）**：

1. 在 [src/http/ngx_http_request.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c) 中搜索 `ngx_event_add_timer` 的调用点（如读请求头、读请求体阶段），观察每次收到数据后都会「续期」读超时。
2. 设想 `client_header_timeout = 60s`，客户端每 50ms 发一个字节（慢速攻击）。若无懒优化，每个字节都触发一次树删插；有懒优化后，只有当累计偏移超过 300ms 才真正改树。粗算：1 秒内 20 次到达，至多 1 次树操作（而非 20 次）。
3. （可选，待本地验证）把 `NGX_TIMER_LAZY_DELAY` 临时改成 0，用 `wrk` 或 `ab` 压测一个大文件上传，对比 CPU 占用——理论上树操作变多、CPU 升高。

**需要观察的现象**：懒优化开启时 `event timer add/delete` 的 debug 日志条数明显少于关闭时。

**预期结果**：相同吞吐下，懒优化版本的 `rbtree` 操作次数大幅下降。**待本地验证**具体数值。

#### 4.2.6 小练习与答案

**练习 1**：`ngx_event_add_timer` 传入的 `timer` 是相对值还是绝对值？最终存进红黑树节点的 key 又是什么？

**参考答案**：参数 `timer` 是相对延迟（如 60000ms）。函数内部把它加上 `ngx_current_msec` 转成绝对到期时间戳 `key`，再存进 `ev->timer.key`。这样全树才能统一按绝对时间排序、取最小值。

**练习 2**：一个事件的 handler 被调用了，它怎么知道自己是因为「I/O 就绪」被调，还是因为「超时」被调？

**参考答案**：看 `ev->timedout`。若是 `ngx_event_expire_timers` 触发的，它在调 handler 前会把 `ev->timedout` 置 1（见 [ngx_event_timer.c:92](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L92)）；若是 epoll 发现 fd 就绪触发的，`timedout` 保持 0。handler 据此走不同分支（超时则关连接返回错误，就绪则继续读写）。

---

### 4.3 posted 三条队列：process_posted 与 accept 优先

#### 4.3.1 概念说明

「posted」在英文里是「投递、暂存」的意思。一个事件被 **post**，就是「不立刻执行它的 handler，而是先把它丢进一个队列，等会儿统一处理」。

为什么要「等会儿」？因为 nginx 在拿到 accept 互斥锁的那一小段时间里，要**尽快处理完 accept、尽快放锁**，让别的 worker 也能 accept。如果在持锁期间还顺便去处理一个个又慢又长的 HTTP 请求，锁就会被一个 worker 长期独占，别的 worker 干着急却接不了新连接——这就是所谓的「连接饿死」。

posted 队列就是为这个目标服务的「暂存区」。nginx 实际维护**三条**全局队列（见 [src/event/ngx_event_posted.c:13-15](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L13-L15)）：

| 队列 | 用途 | 处理时机（主循环中） |
|------|------|----------------------|
| `ngx_posted_accept_events` | **accept 类就绪事件**（监听套接字可读 = 有新连接） | epoll 返回后**最先**处理，且仍在持锁期间 |
| `ngx_posted_events` | **普通就绪事件**（已建连的 socket 可读/可写） | **放锁之后**才处理 |
| `ngx_posted_next_events` | **本轮先别处理、推迟到下一轮**的事件（典型：写就绪但缓冲区满，先让一让） | 下一轮循环**最开头**合并进 `ngx_posted_events` |

> 说明：本讲学习目标强调「accept / 普通两级队列」，这是 posted 设计的核心。`ngx_posted_next_events` 是较晚加入的「延迟写」优化，作为第三个补充。三者共用同一套 post 宏与 `ngx_event_process_posted` 处理函数。

#### 4.3.2 核心流程：投递、分流、处理

**(a) 投递：两个宏**

`ngx_post_event(ev, q)` 把事件 `ev` 挂到队列 `q` 尾部。它是宏，关键是「幂等」：靠 `ev->posted` 标志保证同一个事件不会被重复入队（见 [src/event/ngx_event_posted.h:17-28](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h#L17-L28)）。

`ngx_delete_posted_event(ev)` 把事件移出队列并清标志（见 [src/event/ngx_event_posted.h:31-37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.h#L31-L37)）。

**(b) 分流：epoll 在投递时选哪条队列**

这是「两级」的关键所在。epoll 后端处理就绪事件时，依据 `rev->accept` 标志分流：

```c
if (flags & NGX_POST_EVENTS) {
    queue = rev->accept ? &ngx_posted_accept_events
                        : &ngx_posted_events;
    ngx_post_event(rev, queue);
} else {
    rev->handler(rev);   /* 不持锁时直接执行 */
}
```

见 [src/event/modules/ngx_epoll_module.c:894-902](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L894-L902)。

- `rev->accept` 为真的事件是**监听套接字**的读事件（在 [src/event/ngx_event.c:828](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L828) 初始化监听连接时被置为 1），它可读意味着「有新连接等 accept」→ 进 accept 队列。
- 其它读/写事件（已建连 socket 的数据）→ 进普通队列。
- `NGX_POST_EVENTS` 这个 flags 只有在 worker **持有 accept 锁**时才被置位（见 4.3.3）。没持锁时，epoll 直接调 handler，根本不进队列——队列只在「需要先放锁再处理」时才启用。

**(c) 处理：`ngx_event_process_posted`**

统一的处理函数，对任意一条队列都一样：循环取队头事件 → 移出队列（`ngx_delete_posted_event`）→ 调 `ev->handler(ev)`，直到队列空。

```c
while (!ngx_queue_empty(posted)) {
    q = ngx_queue_head(posted);
    ev = ngx_queue_data(q, ngx_event_t, queue);
    ngx_delete_posted_event(ev);
    ev->handler(ev);
}
```

见 [src/event/ngx_event_posted.c:18-36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L18-L36)。

注意一个细节：handler 内部**可能**又往同一条队列里 post 新事件（比如 accept 出新连接后，新连接的某些事件被 post）。因为用的是「先取队头再删除、再调 handler」的模式，且 `ngx_queue_head` 每次重新取，所以新加入的事件也会在本轮被处理掉，不会漏。

**(d) 推迟：`ngx_event_move_posted_next`**

每轮循环最开头，如果 `ngx_posted_next_events` 非空，就把里面的事件标为 `ready=1, available=-1`（即「假装」又就绪了），然后整体拼接到 `ngx_posted_events` 尾部，清空自己。谁会往 next 里投？写过滤器在 socket 发送返回 `NGX_AGAIN`（发送缓冲区满）时，把写事件 post 到 next，意思是「这轮先别发了，下轮循环开头再来」。

见 [src/event/ngx_event_posted.c:39-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_posted.c#L39-L60)。典型调用点如 [src/http/ngx_http_write_filter_module.c:335](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L335)。

#### 4.3.3 主循环里的顺序：为什么是这样排

把上面所有零件拼起来，看 `ngx_process_events_and_timers` 的关键 9 行（这是 u5-l5 的主角，本讲只看顺序）：

```c
(void) ngx_process_events(cycle, timer, flags);   /* ① epoll_wait + 投递/直执 */

ngx_event_process_posted(cycle, &ngx_posted_accept_events); /* ② 先处理 accept */

if (ngx_accept_mutex_held) {
    ngx_shmtx_unlock(&ngx_accept_mutex);          /* ③ 放 accept 锁 */
}

ngx_event_expire_timers();                        /* ④ 触发到期定时器 */

ngx_event_process_posted(cycle, &ngx_posted_events); /* ⑤ 最后处理普通事件 */
```

见 [src/event/ngx_event.c:248-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L248-L263)。

这个顺序就是「为什么分两个队列」的全部答案：

1. **② 在 ③ 之前**：accept 类事件在**仍持锁**时处理。accept 一个新连接很快（就是几次系统调用），所以持锁时间极短。
2. **⑤ 在 ③ 之后**：普通事件（处理 HTTP 请求、读写后端）在**放锁之后**才处理。这些可能很慢，但此时锁已经还给全局，别的 worker 可以立刻去 accept。
3. **④ 夹在中间**：定时器到期处理放在 accept 之后、普通事件之前。这样既不会抢 accept 的优先级，也保证超时（比如掐掉卡死的请求）能及时发生。

如果不分两个队列、把所有就绪事件混在一起在持锁期间处理，那么只要队首是个慢请求，accept 锁就被独占到这个慢请求处理完——此期间全集群没有 worker 能接新连接，新到的连接只能在内核 backlog 里排队，backlog 满了就被丢弃。这就是「连接饿死」。两级队列用「先快速 drain accept、再放锁、再慢慢处理普通事件」彻底避开了它。

> 补充：在 `ngx_process_events_and_timers` 开头还有处理 `ngx_posted_next_events` 的逻辑（[ngx_event.c:241-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L241-L244)），并因此把 `timer` 设为 0（不阻塞），确保推迟到本轮的写事件能被及时处理。

#### 4.3.4 代码实践：回答本讲的核心问题

**实践目标**：用自己的话讲清「为什么 accept 与普通事件分两个 posted 队列」，并用源码证据支撑。

**操作步骤（源码阅读型）**：

1. 打开 [src/event/ngx_event.c:219-239](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L219-L239)，找到「持锁时 `flags |= NGX_POST_EVENTS`」的代码，确认：只有拿到 accept 锁的 worker 才会走「投递而非直接执行」的路径。
2. 打开 [src/event/modules/ngx_epoll_module.c:894-898](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L894-L898)，确认分流依据是 `rev->accept`。
3. 打开 [src/event/ngx_event.c:255-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L255-L263)，确认「accept 队列 → 放锁 → 定时器 → 普通队列」的顺序。
4. 画出时序图（文字版即可）：
   ```
   持锁 worker 一轮循环：
     epoll_wait 返回（就绪事件已投递到两条队列）
     → drain ngx_posted_accept_events   （快速 accept 完所有新连接）
     → ngx_shmtx_unlock(accept_mutex)    （立刻放锁）
     → ngx_event_expire_timers           （处理超时）
     → drain ngx_posted_events           （慢慢处理 HTTP 请求；此时别的 worker 已能 accept）
   ```

**需要观察的现象**：在时序图上能清楚看到「处理普通 HTTP 请求」发生在「放锁」之后——这就是不饿死的关键。

**预期结果**：你能用两句话回答「为什么分两个队列」——「accept 锁是全局独占的，必须尽快释放；把耗时的普通事件处理推迟到放锁之后，让 accept 路径保持短平快，从而任何 worker 都不会被一个慢请求堵住接不了新连接。」

#### 4.3.5 小练习与答案

**练习 1**：epoll 后端在没有拿到 accept 锁的 worker 上，就绪事件会进 posted 队列吗？

**参考答案**：不会进（绝大多数情况）。因为 `flags` 里没有 `NGX_POST_EVENTS`，[ngx_epoll_module.c:900-901](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L900-L901) 走 `else` 分支直接调 `rev->handler(rev)`。posted 队列主要服务于「持锁 worker」——它需要先把 accept 事件集中处理掉再放锁。

**练习 2**：`ngx_post_event` 宏为什么要先判断 `!(ev)->posted`？

**参考答案**：为了**幂等**。同一个事件可能被多次 post（例如同一轮 epoll 里一个连接既被标可读又被标可写，或不同代码路径都试图投递它）。靠 `ev->posted` 标志，只有在「尚未入队」时才真正 `ngx_queue_insert_tail`，避免同一事件在队列里出现多次导致 handler 被重复调用或队列损坏。

**练习 3**：`ngx_posted_next_events` 解决什么问题？为什么处理它之后要把主循环的 `timer` 置 0？

**参考答案**：它解决「写就绪但发送缓冲区满（`NGX_AGAIN`）」的场景：与其空转重试，不如把写事件推迟到下一轮循环开头再处理。处理它之后 `timer` 置 0（见 [ngx_event.c:241-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L241-L244)），是为了让随后的 `epoll_wait` 立刻返回（不阻塞），保证这个被推迟的写事件能被及时重试，而不是等到下一次有 I/O 才被带出来。

---

## 5. 综合实践

把本讲的定时器和 posted 队列串起来，做一次「一轮 worker 循环」的纸上推演。

**场景**：单 worker，持 accept 锁。当前时刻 `ngx_current_msec = 100000`。红黑树里有三个定时器：

- T1：key = 100050（还剩 50ms 到期，某请求的读超时）
- T2：key = 100200（还剩 200ms，keepalive 超时）
- T3：key = 100010（还剩 10ms，另一个请求的超时）

请按以下步骤推演并写出每步的依据源码行：

1. **进入主循环**。调 `ngx_event_find_timer`，返回值是多少？为什么？（依据 [ngx_event_timer.c:45-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L45-L49)）
2. **`ngx_process_events`（epoll_wait）阻塞至多多久**？设它阻塞 15ms 后因「监听套接字可读 + 某已建连 socket 可读」返回。`ngx_current_msec` 现在是多少？
3. **epoll 处理就绪事件**：因为持锁，`flags` 含 `NGX_POST_EVENTS`。监听套接字的读事件（`rev->accept == 1`）被 post 到哪条队列？已建连 socket 的读事件被 post 到哪条队列？（依据 [ngx_epoll_module.c:894-898](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L894-L898)）
4. **处理 accept 队列** → **放锁**（依据 [ngx_event.c:255-259](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L255-L259)）。
5. **`ngx_event_expire_timers`**：此刻哪些定时器会被触发？handler 被调时 `ev->timedout` 是几？（依据 [ngx_event_timer.c:72-94](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_timer.c#L72-L94)）
6. **处理普通队列**（依据 [ngx_event.c:263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L263)）。

**参考答案**：
1. 返回 10（T3 最小，剩 10ms）。
2. 至多阻塞 10ms；题设 15ms 不会发生（最多 10ms 就会被定时器叫醒）。若强行按「某时刻醒来且已推进 15ms」理解，`ngx_current_msec = 100015`。
3. 监听套接字 → `ngx_posted_accept_events`；已建连 socket → `ngx_posted_events`。
4. 先 drain accept 队列（accept 新连接），再 `ngx_shmtx_unlock` 放锁。
5. T3（key=100010 < 100015）触发；T1 还没（100050 > 100015），T2 更没。`ev->timedout = 1`。
6. 最后才处理那条已建连 socket 的读事件（即真正的 HTTP 请求处理），且此时锁已释放。

> 这个推演把「取最近定时器决定阻塞时长 → 分流投递 → accept 优先放锁 → 触发超时 → 慢活最后干」一气贯通，是本讲全部知识的综合。

## 6. 本讲小结

- nginx 用**一棵全局红黑树**管理所有定时器，节点 key 是**绝对毫秒时间戳**，最小者即最早到期；树初始化用 `ngx_rbtree_insert_timer_value`，靠**有符号差值**比较扛 49 天回绕。
- `ngx_event_find_timer` 取「最近到期还剩多久」，作为 `epoll_wait` 的阻塞上限；`ngx_event_expire_timers` 在醒来后循环触发所有已到期者，置 `ev->timedout = 1` 让 handler 区分超时与 I/O 就绪。
- `ngx_event_add_timer` / `del_timer` 是内联函数；**懒优化** `NGX_TIMER_LAZY_DELAY = 300ms` 让频繁续期的高速连接省去大量红黑树删插操作，用可控精度损失换性能。
- posted 机制把「就绪但不急」的事件**暂存到队列**统一处理，核心是 **accept / 普通** 两条队列：accept 类（`rev->accept == 1`，监听套接字）优先且在持锁期处理，普通类在**放锁之后**才处理。
- 这个顺序保证 accept 锁的持有时间极短，避免一个慢请求独占锁导致**连接饿死**；`ngx_posted_next_events` 则是「写缓冲满就推迟到下一轮」的延迟写优化。
- worker 优雅退出时，`ngx_event_no_timers_left` 靠 `cancelable` 标志判断「剩下的定时器是否都可以抛弃」，决定能否安全退出。

## 7. 下一步学习建议

- **u5-l5 事件主循环 process_events_and_timers**：本讲多次引用的 `ngx_process_events_and_timers` 是它的主角。下一讲会把「timer_resolution 定时精度」「accept 互斥锁的获取/让出」「posted/expire 的完整调用顺序」完整串成 worker 一轮循环的调度图，补齐本讲故意留作黑盒的 `ngx_trylock_accept_mutex` 细节。
- **横向对照**：回看 u5-l2 的 `ngx_epoll_process_events`，结合本讲的分流逻辑，确认你对「epoll 就绪 → post/直执」两条出口的理解；再看 u5-l3 的 `ngx_event_accept`，确认 accept handler 正是被 post 到 accept 队列、随后在持锁期被 drain 的那个。
- **协议层印证**：进入第六单元后，留意 HTTP 请求处理（u6-l2）是如何反复调用 `ngx_event_add_timer` 续期、并在 handler 里检查 `ev->timedout` 返回 408 的——那将是本讲定时器机制的直接消费者。
