# 接受连接与 connection 管理

## 1. 本讲目标

本讲承接 u5-l1（事件模型总览）与 u5-l2（epoll 后端），回答一个具体问题：**当 epoll 告诉 nginx「某个监听套接字可读了」之后，nginx 是如何把这一次就绪变成一个可读、可写、带内存池、能被 HTTP/stream/mail 协议接管的「连接」对象的？** 以及连接用完之后如何回收。

学完后你应当掌握：

- 一条新 TCP 连接从 `accept()` 系统调用到分配 `ngx_connection_t` 的完整流程；
- 连接池的预分配与空闲链表机制（`ngx_get_connection` / `ngx_free_connection`）；
- `ngx_connection_t` 与其内嵌的读/写事件 `ngx_event_t` 如何绑定，以及为何新建连接的写事件一开始就是「就绪」的；
- 连接的被动关闭路径（`ngx_close_connection`）与资源紧张时的连接回收（`ngx_drain_connections`）。

## 2. 前置知识

阅读本讲前，你需要先建立以下概念（均在前置讲义中讲过）：

- **事件 `ngx_event_t`**：nginx 调度的原子单位，靠 `data` 指向归属对象、靠 `handler` 记回调、靠一串 1 位标志位表状态（u5-l1）。
- **`ngx_event_actions_t` 接口表**：epoll/kqueue 等后端把 `add/del/process` 等函数注册进这张表，上层用 `ngx_add_event` 等宏间接调用（u5-l1、u5-l2）。
- **边缘触发（ET）与 `NGX_USE_EPOLL_EVENT`**：epoll 默认边缘触发，就绪只通知一次，需读到 `EAGAIN` 为止（u5-l2）。
- **内存池 `ngx_pool_t`**：小块 bump 分配、整池回收，nginx 里几乎每个长期对象都挂在专属池上（u2-l1）。
- **`ngx_cycle_t`**：进程级全局上下文，承载配置、模块、监听端口、连接数组等（u3-l2）。
- **`accept_mutex` 互斥**：多 worker 下为避免惊群而加的锁，由 master 在主循环里尝试获取（u5-l1、u4-l3）。

一个关键直觉：nginx **不为每个连接现场 `malloc` 一个 `ngx_connection_t`**。worker 启动时一次性预分配好一整片连接数组与事件数组，accept 时只是从空闲链表上「摘」一个下来用，用完再「挂」回去。这就是 nginx 能在极高并发下保持稳定的根本原因之一。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/event/ngx_event_accept.c` | 接受新连接的主函数 `ngx_event_accept`，以及 accept 互斥辅助函数、刚接受连接的关闭函数 `ngx_close_accepted_connection`。 |
| `src/core/ngx_connection.c` | 连接池核心 `ngx_get_connection` / `ngx_free_connection`，被动关闭 `ngx_close_connection`，可复用连接管理 `ngx_reusable_connection`，资源回收 `ngx_drain_connections`。 |
| `src/core/ngx_connection.h` | `ngx_connection_t` 与 `ngx_listening_t` 结构体定义。 |
| `src/event/ngx_event.c` | `ngx_event_process_init`：worker 启动时预分配连接/事件数组、构造空闲链表、把监听套接字绑到连接上并装上 `ngx_event_accept`。 |
| `src/event/ngx_event.h` | `ngx_event_t` 结构体，含本讲要用到的 `instance`、`closed` 等标志位。 |
| `src/http/ngx_http.c` | HTTP 层把监听套接字的 `ls->handler` 设为 `ngx_http_init_connection`，完成「accept 之后交给谁」的交接。 |

## 4. 核心概念与源码讲解

### 4.1 连接池与 ngx_connection_t（ngx_get_connection / ngx_free_connection）

#### 4.1.1 概念说明

`ngx_connection_t` 是 nginx 对「一条已建立连接」的统一抽象。无论是客户端到 nginx 的 TCP 连接、nginx 到上游的连接，还是一个监听套接字本身，在 nginx 内部都是一个 `ngx_connection_t`。它把 fd、读写事件、内存池、对端地址、收发函数指针、统计编号等全部收拢在一个结构体里。

`ngx_listening_t` 则是「监听套接字」的抽象（`listen` 指令一行对应一个），它持有 fd、绑定地址、backlog，以及一个关键回调 `handler`——「accept 到新连接后，该把它交给谁」。每个 `ngx_listening_t` 自己也占一个 `ngx_connection_t`（用其 read 事件来感知「有新连接来了」）。

连接池的设计动机有二：

1. **避免高频 malloc**：每秒上万次 accept 不能每次都 `malloc(sizeof(ngx_connection_t))`。
2. **O(1) 取放**：用一个单链表把所有空闲连接串起来，取是从头摘、放是往头插，都是常数时间。

#### 4.1.2 核心流程

worker 启动时（`ngx_event_process_init`）一次性建好三片同等大小的数组，再串成空闲链表：

```text
worker 启动 (ngx_event_process_init)
  ├─ ngx_alloc connection_n 个 ngx_connection_t  → cycle->connections
  ├─ ngx_alloc connection_n 个 ngx_event_t        → cycle->read_events
  ├─ ngx_alloc connection_n 个 ngx_event_t        → cycle->write_events
  ├─ 把 connections[i].read  = &read_events[i]
  │           connections[i].write = &write_events[i]
  ├─ 反向用 data 指针把空闲连接串成单链表
  └─ cycle->free_connections = 链表头
     cycle->free_connection_n = connection_n

accept 一个新连接
  ├─ ngx_get_connection(s)   ← 从 free_connections 头部摘一个
  └─ ... 使用 ...
      └─ ngx_free_connection(c) ← 把 c 挂回 free_connections 头部
```

`connection_n` 来自配置指令 `worker_connections`（默认 512，生产常调到上万）。

#### 4.1.3 源码精读

**预分配与空闲链表构造**——三片数组在 [src/event/ngx_event.c:754-800](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L754-L800) 中分配并组装：

```c
cycle->connections =
    ngx_alloc(sizeof(ngx_connection_t) * cycle->connection_n, cycle->log);
...
cycle->read_events = ngx_alloc(sizeof(ngx_event_t) * cycle->connection_n,
                               cycle->log);
...
rev = cycle->read_events;
for (i = 0; i < cycle->connection_n; i++) {
    rev[i].closed = 1;
    rev[i].instance = 1;
}
```

注意 `rev[i].closed = 1; rev[i].instance = 1;`（[src/event/ngx_event.c:770-771](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L770-L771)）：初始时所有读事件都标记为「已关闭」，且 `instance` 位初始化为 1。`instance` 位的作用见下文 `ngx_get_connection`。

随后用 `data` 指针把连接**倒序**串成链表（[src/event/ngx_event.c:785-800](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L785-L800)）：

```c
i = cycle->connection_n;
next = NULL;
do {
    i--;
    c[i].data = next;          // data 在空闲时充当链表 next 指针
    c[i].read  = &cycle->read_events[i];
    c[i].write = &cycle->write_events[i];
    c[i].fd = (ngx_socket_t) -1;
    next = &c[i];
} while (i);
cycle->free_connections = next;
cycle->free_connection_n = cycle->connection_n;
```

这里有一个值得记住的技巧：`ngx_connection_t.data` 字段在「空闲」时被复用为链表的 `next` 指针，在「使用中」时则指向协议层各自的上下文（HTTP 请求、upstream 状态等）。一个字段两种用途，省去了单独的链表节点。

**取连接 `ngx_get_connection`**——[src/core/ngx_connection.c:1206-1269](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1206-L1269)，核心是「摘头节点 + 清零 + 重建关联」：

```c
ngx_drain_connections((ngx_cycle_t *) ngx_cycle);   // 资源紧张时先回收

c = ngx_cycle->free_connections;                    // 摘头
if (c == NULL) { ... "worker_connections are not enough" ... return NULL; }

ngx_cycle->free_connections = c->data;              // 头指针后移
ngx_cycle->free_connection_n--;

if (ngx_cycle->files && ngx_cycle->files[s] == NULL) {
    ngx_cycle->files[s] = c;                        // 可选的 fd→connection 映射
}

rev = c->read;
wev = c->write;

ngx_memzero(c, sizeof(ngx_connection_t));           // 整结构清零

c->read = rev;                                      // 恢复 read/write（清零时丢了）
c->write = wev;
c->fd = s;
c->log = log;

instance = rev->instance;                           // 关键：翻转 instance 位
ngx_memzero(rev, sizeof(ngx_event_t));
ngx_memzero(wev, sizeof(ngx_event_t));
rev->instance = !instance;
wev->instance = !instance;

rev->index = NGX_INVALID_INDEX;
wev->index = NGX_INVALID_INDEX;
rev->data = c;                                      // 事件反指归属连接
wev->data = c;
wev->write = 1;                                     // 标记这是写事件
```

这里有三个要点：

1. **`ngx_drain_connections` 前置回收**：取连接前先看看空闲是否紧张，紧张就强行关闭一些可复用的空闲连接（详见 4.4）。
2. **`files[s]` 映射可选**：只在 `NGX_USE_FD_EVENT`（如 eventport、/dev/poll）后端启用；Linux epoll 下 `cycle->files` 为 NULL，这步跳过。
3. **`instance` 位翻转**：这是为了检测「过期事件」。每次复用一个连接，其读写事件的 `instance` 位取反。后端（如 epoll）把这个位连同事件指针编码进内核返回的就绪通知里；当通知回来时，若通知里存的 `instance` 与事件当前的 `instance` 不一致，说明这个通知属于「上一任」连接（fd 已被关闭并复用给新连接），应丢弃。`instance` 位定义在 [src/event/ngx_event.h:38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L38)，编码细节属于 epoll 后端（u5-l2），本讲只需记住「`ngx_get_connection` 每次取连接都翻转 `instance`，是过期事件检测的一环」。

**放回连接 `ngx_free_connection`**——[src/core/ngx_connection.c:1272-1282](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1272-L1282)，就是「头插」：

```c
c->data = ngx_cycle->free_connections;
ngx_cycle->free_connections = c;
ngx_cycle->free_connection_n++;

if (ngx_cycle->files && ngx_cycle->files[c->fd] == c) {
    ngx_cycle->files[c->fd] = NULL;
}
```

注意 `ngx_free_connection` 只是把连接结构体归还到空闲链表，**并不关闭 fd、不销毁内存池**——那两件事由调用方（`ngx_close_connection` / `ngx_close_accepted_connection`）另行完成。职责分离是为了让「归还连接」这一步足够轻、足够安全。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，弄清一次 `ngx_get_connection` 究竟有没有触发系统 `malloc`，建立「连接结构体是预分配的」直觉。

**操作步骤**：

1. 打开 [src/core/ngx_connection.c:1206-1269](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1206-L1269) 的 `ngx_get_connection`。
2. 逐行标注其中所有可能分配内存的调用：`ngx_drain_connections`、`free_connections` 摘取、`ngx_memzero`、字段赋值。
3. 确认 `cycle->connections` / `read_events` / `write_events` 这三片数组的分配位置在 [src/event/ngx_event.c:754-778](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L754-L778)，发生在 worker 启动时而非 accept 时。

**需要观察的现象**：`ngx_get_connection` 全程没有任何 `ngx_alloc`/`ngx_palloc`/`malloc` 调用；它只是指针搬运与清零。

**预期结果**：取一个连接的代价是几次指针赋值加一次 `ngx_memzero`，零系统分配。真正的动态分配发生在 `ngx_event_accept` 拿到连接**之后**（为连接建内存池），那是下一节的内容。

#### 4.1.5 小练习与答案

**练习 1**：`ngx_connection_t.data` 字段在连接空闲时和使用时分别存什么？为什么要这样设计？

**参考答案**：空闲时 `data` 充当空闲链表的 `next` 指针（见 `ngx_event_process_init` 的 `c[i].data = next;` 与 `ngx_get_connection` 的 `ngx_cycle->free_connections = c->data;`）；使用时 `data` 指向协议层上下文（如 HTTP 的 `ngx_http_request_t`）。复用一个字段省去了为链表单独分配节点，也让「取/放」只需改一个指针。

**练习 2**：若 `worker_connections` 设为 1024，当前有 1020 个连接在使用，`ngx_get_connection` 会成功吗？会触发什么副作用？

**参考答案**：仍可能成功。`ngx_get_connection` 开头先调 `ngx_drain_connections`，当空闲连接数低于 `connection_n/16`（即 64）且存在可复用连接时，会强行关闭若干空闲 keepalive 连接腾出位置（见 4.4）。只有连回收后仍无空闲连接时，才打印 `"N worker_connections are not enough"` 并返回 NULL。

---

### 4.2 ngx_event_accept：接受新连接

#### 4.2.1 概念说明

`ngx_event_accept` 是**监听套接字读事件的 handler**。当 epoll 报告某个监听 fd 可读（即内核 accept 队列里有新连接）时，事件循环最终会调用这个函数。它的职责很纯粹：循环调用 `accept()` 把就绪连接全取出来，每个都包装成 `ngx_connection_t`，最后交给协议层（`ls->handler`）。

它不解析 HTTP、不做业务，是「传输层到 nginx 内部连接对象」的翻译器。

#### 4.2.2 核心流程

```text
ngx_event_accept(ev)           // ev 是监听套接字的读事件
  ├─ lc = ev->data;            // 监听连接
  ├─ ls = lc->listening;       // 对应的 ngx_listening_t
  ├─ ev->available = multi_accept ? 多次 : 1   // 决定循环多少轮
  └─ do {
       s = accept4(lc->fd, ...) 或 accept(...)   // 取一个新 fd
       ├─ 若 EAGAIN：return（本轮就绪已取完）
       ├─ 若 EMFILE/ENFILE：禁用本 worker 的 accept，return
       ├─ ngx_accept_disabled = connection_n/8 - free_connection_n  // 负载均衡信号
       ├─ c = ngx_get_connection(s)             // 摘一个空闲连接
       ├─ c->pool = ngx_create_pool(...)        // 建专属内存池
       ├─ c->sockaddr = ngx_palloc(...)         // 拷贝对端地址
       ├─ log = ngx_palloc(...)                 // 建专属日志
       ├─ 设非阻塞、装 recv/send 回调、填 start_time/number
       ├─ wev->ready = 1                        // 新连接立即可写
       ├─ (可选) ngx_add_conn(c)                // 非 epoll 后端整体注册
       └─ ls->handler(c)                        // 交给协议层（如 ngx_http_init_connection）
     } while (ev->available);
```

`multi_accept` 由指令 `multi_accept on;` 控制：开启后一次 epoll 唤醒会尽可能多地 accept，直到 `EAGAIN`；默认关闭时 `ev->available` 为 0，循环只执行一次（`do{}while(0)`）。但 kqueue 后端用 `ev->available` 记录内核报告的就绪连接数，可精确循环。

#### 4.2.3 源码精读

**入口与监听连接定位**——[src/event/ngx_event_accept.c:20-56](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L20-L56)：

```c
lc = ev->data;          // 监听套接字自己也是一个 ngx_connection_t
ls = lc->listening;     // 取它对应的 ngx_listening_t
ev->ready = 0;          // 消费掉这次就绪
```

`ev->data` 指向监听连接，监听连接的 `listening` 反指 `ngx_listening_t`。这一双向引用在 `ngx_event_process_init` 里建立（见 4.3）。

**accept 循环与 accept4 优先**——[src/event/ngx_event_accept.c:58-78](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L58-L78)：

```c
do {
    socklen = sizeof(ngx_sockaddr_t);
#if (NGX_HAVE_ACCEPT4)
    if (use_accept4) {
        s = accept4(lc->fd, &sa.sockaddr, &socklen, SOCK_NONBLOCK);
    } else {
        s = accept(lc->fd, &sa.sockaddr, &socklen);
    }
#else
    s = accept(lc->fd, &sa.sockaddr, &socklen);
#endif

    if (s == (ngx_socket_t) -1) {
        err = ngx_socket_errno;
        if (err == NGX_EAGAIN) {        // 就绪连接已取完
            return;
        }
        ...
```

`accept4` 是 Linux 扩展，能在一个系统调用里同时完成 accept 与置非阻塞（`SOCK_NONBLOCK`），省去后续 `fcntl`。nginx 用一个 `static use_accept4` 标志试探：若内核不支持（返回 `ENOSYS`），自动回退到 `accept` 并清除 `ngx_inherited_nonblocking`（见 [src/event/ngx_event_accept.c:89-97](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L89-L97)）。

**EMFILE/ENFILE 的自保**——这是生产环境里非常关键的一段，[src/event/ngx_event_accept.c:112-130](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L112-L130)：

```c
if (err == NGX_EMFILE || err == NGX_ENFILE) {
    if (ngx_disable_accept_events((ngx_cycle_t *) ngx_cycle, 1) != NGX_OK) {
        return;
    }

    if (ngx_use_accept_mutex) {
        if (ngx_accept_mutex_held) {
            ngx_shmtx_unlock(&ngx_accept_mutex);
            ngx_accept_mutex_held = 0;
        }
        ngx_accept_disabled = 1;            // 让本 worker 暂时不再抢锁
    } else {
        ngx_add_timer(ev, ecf->accept_mutex_delay);  // 没用互斥锁就定时重试
    }
}
```

当 fd 耗尽（进程或系统级打开文件数达到上限），accept 会失败并返回 `EMFILE`/`ENFILE`。若不做处理，边缘触发下会陷入「epoll 一直报就绪 → accept 一直失败」的死循环把 CPU 打满。nginx 的对策是：**立即把本 worker 的监听事件从 epoll 摘掉**（`ngx_disable_accept_events`），并在开启 accept 互斥时释放锁、置 `ngx_accept_disabled = 1` 让自己暂时退出竞争；未开互斥时则挂一个定时器，到点再把监听事件加回来重试。

**负载均衡信号**——[src/event/ngx_event_accept.c:139-140](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L139-L140)：

```c
ngx_accept_disabled = ngx_cycle->connection_n / 8
                      - ngx_cycle->free_connection_n;
```

这是一个为多 worker 负载均衡服务的量。写成公式：

\[
\textit{ngx\_accept\_disabled} = \left\lfloor \frac{\textit{connection\_n}}{8} \right\rfloor - \textit{free\_connection\_n}
\]

当空闲连接充裕（\( \textit{free\_connection\_n} > \textit{connection\_n}/8 \)）时它为**负**，worker 在主循环里会积极去抢 accept 锁；当空闲连接不足总量的 1/8 时它变**正**，worker 主动放弃抢锁，把新连接让给更空闲的兄弟 worker。这是 nginx 不靠集中式调度器、仅靠一个本地计数就实现 worker 间连接负载均衡的精巧设计（具体消费在 u5-l5 的 `ngx_process_events_and_timers`）。

**摘连接 + 建池**——[src/event/ngx_event_accept.c:142-181](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L142-L181)：

```c
c = ngx_get_connection(s, ev->log);
if (c == NULL) {
    ngx_close_socket(s);          // 连接结构体拿不到，关掉 fd 了事
    return;
}
...
c->pool = ngx_create_pool(ls->pool_size, ev->log);   // 每连接一池
...
c->sockaddr = ngx_palloc(c->pool, socklen);          // 拷对端地址
ngx_memcpy(c->sockaddr, &sa, socklen);

log = ngx_palloc(c->pool, sizeof(ngx_log_t));        // 每连接一日志
```

这里体现了 nginx 的资源模型：**一个连接 = 一个内存池**。连接生命周期内所有小对象（地址、日志、HTTP 请求结构、头部数组…）都从这个池分配，连接关闭时 `ngx_destroy_pool` 一次性回收，无需逐个 free（u2-l1）。

#### 4.2.4 代码实践

**实践目标**：统计接受一条新连接在 `ngx_event_accept` 内部触发的内存分配次数，区分「系统 malloc」与「池内 bump 分配」。

**操作步骤**：

1. 打开 [src/event/ngx_event_accept.c:142-299](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L142-L299)，从 `ngx_get_connection` 之后逐行找分配点。
2. 对照 u2-l1 的内存池实现，判断每个 `ngx_palloc`/`ngx_pnalloc` 是否真的触发 `malloc`（小块不触发，大块或首块触发）。
3. 注意 `ngx_get_connection` 本身不分配（4.1 已确认），`ngx_create_pool` 内部有一次 `ngx_memalign`/`malloc`。

**需要观察的现象**：对一次 `accept`，应能数出 1 个 `ngx_create_pool`、1 个 `ngx_palloc`（sockaddr）、1 个 `ngx_palloc`（log）、条件性 1 个 `ngx_pnalloc`（addr_text，仅当 `ls->addr_ntop` 为真）。

**预期结果**：**真正触发系统 `malloc` 的只有 1 次**（`ngx_create_pool` 创建池的首块）；sockaddr、log、addr_text 三次都是池内 bump 分配，不触发 `malloc`；`ngx_connection_t` 及其读写事件来自 worker 启动时预分配的数组，分摊到本次 accept 为 0 次 malloc。结论：**nginx 接受一条连接的动态分配代价恒为 1 次 malloc**（不含协议层后续分配），这与「每连接预分配结构体」的设计直接相关。结果待本地用调试器或计数钩子验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_event_accept` 在 `accept()` 返回 `EAGAIN` 时直接 `return` 而不是 `break` 出循环？

**参考答案**：`EAGAIN` 表示内核 accept 队列已空、本轮就绪事件已被消费完，没有更多连接可取，函数使命完成，直接返回。`break` 与 `return` 在此处效果相近，但 `return` 更明确地表达「本次回调结束」语义；同时函数末尾还有 `ngx_reorder_accept_events(ls)`（EPOLLEXCLUSIVE 下的重排，[src/event/ngx_event_accept.c:338-340](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L338-L340)），`return` 跳过它是合理的——没有成功 accept 就无需重排。

**练习 2**：`ngx_accept_disabled` 为正时，worker 会做什么？为什么这能实现负载均衡？

**参考答案**：`ngx_accept_disabled` 为正意味着本 worker 空闲连接不足总量的 1/8、比较繁忙。在主循环 `ngx_process_events_and_timers` 里，worker 据此**跳过尝试获取 accept 互斥锁**（u5-l5），从而不再接受新连接，把它们让给更空闲的 worker。每个 worker 只看自己的本地计数就达成全局近似均衡，无需进程间通信。

---

### 4.3 读写事件的绑定与协议交接

#### 4.3.1 概念说明

`ngx_get_connection` 把连接结构体交出来时，其读写事件已被清零并重新绑定到该连接（`rev->data = c; wev->data = c;`）。但「这个连接来了数据该调用谁」——即 `rev->handler` / `wev->handler`——还没设。设置 handler 是协议层的事：HTTP 层会把它设成 `ngx_http_process_request_line` 之类，stream 层会设成自己的处理函数。

`ngx_event_accept` 在 accept 完成后做的最后一件大事，就是调用 `ls->handler(c)`，把连接「交接」给协议层。这个 `ls->handler` 在配置阶段就被设好：HTTP 模块把它设为 `ngx_http_init_connection`。

#### 4.3.2 核心流程

```text
配置阶段（ngx_http_init_listening）
  └─ ls->handler = ngx_http_init_connection     // HTTP 交接点

worker 启动（ngx_event_process_init）
  └─ 监听连接的 rev->handler = ngx_event_accept // accept 入口
     rev->accept = 1                            // 标记这是 accept 事件

运行时一次就绪
  └─ ngx_event_accept
     ├─ accept() → 新 fd s
     ├─ c = ngx_get_connection(s)               // rev/wev 已绑定到 c
     ├─ wev->ready = 1                          // 写就绪
     └─ ls->handler(c)  ==  ngx_http_init_connection(c)
        └─ 协议层接管：设 rev->handler = 读请求行、挂定时器、加读事件到 epoll
```

#### 4.3.3 源码精读

**监听事件装上 accept handler**——[src/event/ngx_event.c:813-895](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L813-L895)，worker 启动时为每个监听套接字分配连接并装 handler：

```c
c = ngx_get_connection(ls[i].fd, cycle->log);   // 监听套接字也占一个连接
...
c->listening = &ls[i];
ls[i].connection = c;                            // 双向引用

rev = c->read;
rev->log = c->log;
rev->accept = 1;                                 // 关键标志：这是 accept 事件
...
if (c->type == SOCK_STREAM) {
    rev->handler = ngx_event_accept;             // 装上 accept handler
}
```

`rev->accept = 1` 让事件循环知道这个读事件是「监听套接字可读」，需要在处理时优先对待（posted accept 队列优先级高于普通事件，见 u5-l4）。`ls[i].connection = c` 与 `c->listening = &ls[i]` 建立双向引用，正是 4.2 里 `ngx_event_accept` 开头 `lc = ev->data; ls = lc->listening;` 能拿到监听结构的原因。

**HTTP 交接点**——[src/http/ngx_http.c:1824](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L1824)：

```c
ls->handler = ngx_http_init_connection;
```

一行赋值，定义了「accept 之后交给谁」。

**accept 末尾的交接与写就绪**——[src/event/ngx_event_accept.c:249-330](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L249-L330)：

```c
rev = c->read;
wev = c->write;

wev->ready = 1;                  // 新建 TCP 连接立即可写
...
rev->log = log;
wev->log = log;
...
c->number = ngx_atomic_fetch_add(ngx_connection_counter, 1);  // 全局唯一编号
c->start_time = ngx_current_msec;                            // 起始时刻（用于耗时统计）
...
if (ngx_add_conn && (ngx_event_flags & NGX_USE_EPOLL_EVENT) == 0) {
    if (ngx_add_conn(c) == NGX_ERROR) { ... }     // 非 epoll：整体注册读写事件
}
...
ls->handler(c);                  // 交接给协议层（ngx_http_init_connection）
```

两个要点：

1. **`wev->ready = 1` 的含义**：一条刚 accept 出来的 TCP 连接，对端已完成三次握手，发送缓冲区必然可写，所以 nginx 直接把写事件标记为就绪，省去一次 epoll_wait 才能得知「可写」的往返。读事件**不**标就绪——因为对端还没发数据，读了也是 `EAGAIN`。例外是 `deferred_accept`（Linux `TCP_DEFER_ACCEPT`）：内核已确认有数据到达才通知 accept，此时 `rev->ready = 1`（[src/event/ngx_event_accept.c:258-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L258-L263)）。

2. **`ngx_add_conn` 在 epoll 下被跳过**：条件 `(ngx_event_flags & NGX_USE_EPOLL_EVENT) == 0` 在 Linux epoll 上为假（epoll 设置了 `NGX_USE_EPOLL_EVENT` 标志，u5-l2），所以这段整体注册的代码不执行。epoll 走的是「按需加事件」——协议层（如 `ngx_http_init_connection`）稍后只把**读**事件加进 epoll，写事件等到真正要写时才加。而 poll/kqueue 等后端需要一次性把读写事件都注册进去，故走 `ngx_add_conn` 分支。

**`c->number` 与 `c->start_time`**：`number` 是跨 worker 的全局递增连接编号（用原子 `fetch_add`，[src/event/ngx_event_accept.c:277](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L277)），用于日志里 `*N` 标识连接；`start_time` 记录连接起始毫秒，供后续 keepalive 超时、请求耗时统计使用。

#### 4.3.4 代码实践

**实践目标**：把「监听套接字读事件 → `ngx_event_accept` → `ls->handler` → 协议层」这条链在源码里走通，确认 handler 的两次装配点。

**操作步骤**：

1. 在 [src/event/ngx_event.c:894-895](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L894-L895) 确认监听读事件的 `handler = ngx_event_accept`。
2. 在 [src/event/ngx_event_accept.c:330](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L330) 确认末尾调用 `ls->handler(c)`。
3. 在 [src/http/ngx_http.c:1824](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L1824) 确认 `ls->handler = ngx_http_init_connection`。
4. 打开 `ngx_http_init_connection`（在 `src/http/ngx_http_request.c`），观察它如何把 `c->read->handler` 设成读请求行的函数、如何挂上 post_accept_timeout 定时器。

**需要观察的现象**：handler 的设置分两处——`ngx_event_accept` 是「监听事件」的 handler（传输层），`ls->handler`（即 `ngx_http_init_connection`）是「新连接」的 handler（协议层）。两者通过 `ls->handler(c)` 这一行衔接。

**预期结果**：能画出「epoll 就绪 → `ngx_event_accept` → `ls->handler(c)` → `ngx_http_init_connection` → 设 `rev->handler`、挂定时器、`ngx_add_event(rev, NGX_READ_EVENT)`」的完整调用链。

#### 4.3.5 小练习与答案

**练习 1**：为什么新建连接的 `wev->ready = 1` 而 `rev->ready` 默认为 0？

**参考答案**：刚 accept 的 TCP 连接，对端已完成握手，发送缓冲区必然可写，标记写就绪可避免「想写却要先等一次 epoll_wait」。但读端是否就绪取决于对端有没有发数据，accept 本身不保证有数据可读（除非用了 `TCP_DEFER_ACCEPT`），所以读事件不标就绪，留给 epoll 在数据到达时再通知。

**练习 2**：在 Linux epoll 下，新连接的写事件是什么时候被加进 epoll 的？

**参考答案**：accept 时不加（`ngx_add_conn` 分支被 `NGX_USE_EPOLL_EVENT` 条件跳过）。写事件采用「延迟注册」策略：只有当协议层真正需要写数据、且 `ngx_writev_chain` 返回 `NGX_AGAIN`（发送缓冲区满）时，才会把写事件加进 epoll，等内核通知可写后再继续写。这避免了大量连接长期注册写事件带来的内核开销。

---

### 4.4 ngx_close_connection：连接的关闭与回收

#### 4.4.1 概念说明

连接的关闭比建立更复杂，因为要处理「事件可能正挂在 epoll / 定时器 / posted 队列里」的并发状态。`ngx_close_connection` 是运行时关闭**已建立**连接的通用入口（区别于 `ngx_close_accepted_connection`，后者只关「刚 accept、还没交接给协议层」的半成品连接）。

nginx 还有一套「被动回收」机制：当连接池将耗尽时，`ngx_drain_connections` 会主动关闭一些空闲的 keepalive 连接腾位置。这与「可复用连接」队列 `reusable_connections_queue` 配合，构成 nginx 在高并发下的内存/连接压力释放阀。

#### 4.4.2 核心流程

```text
ngx_close_connection(c)                       // 主动关闭一条已建立连接
  ├─ 若 fd 已是 -1：报 "already closed"，return
  ├─ 从定时器摘除 read/write 事件
  ├─ 从 epoll 摘除连接（ngx_del_conn 或逐个 del_event）
  ├─ 从 posted 队列摘除 read/write 事件
  ├─ c->read->closed = 1; c->write->closed = 1   // 标记，防过期事件再被处理
  ├─ ngx_reusable_connection(c, 0)                // 移出可复用队列
  ├─ ngx_free_connection(c)                       // 归还连接结构体到空闲链表
  ├─ fd = c->fd; c->fd = -1
  ├─ 若 c->shared：return（共享连接不关 fd）
  └─ ngx_close_socket(fd)                         // 真正 close()

资源紧张时（ngx_get_connection 开头）
  └─ ngx_drain_connections
     ├─ 若 free_connection_n > connection_n/16 或无可复用连接：return
     ├─ 取最多 32 个（或 reusable/8）可复用连接
     └─ 置 c->close = 1，调 c->read->handler(rev)   // 让协议层走优雅关闭
```

#### 4.4.3 源码精读

**`ngx_close_connection` 主体**——[src/core/ngx_connection.c:1285-1370](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1285-L1370)：

```c
if (c->fd == (ngx_socket_t) -1) {
    ngx_log_error(NGX_LOG_ALERT, c->log, 0, "connection already closed");
    return;
}

if (c->read->timer_set) {
    ngx_del_timer(c->read);             // 摘定时器
}
if (c->write->timer_set) {
    ngx_del_timer(c->write);
}

if (!c->shared) {
    if (ngx_del_conn) {
        ngx_del_conn(c, NGX_CLOSE_EVENT);     // epoll：一次删整个连接
    } else {
        if (c->read->active || c->read->disabled) {
            ngx_del_event(c->read, NGX_READ_EVENT, NGX_CLOSE_EVENT);
        }
        if (c->write->active || c->write->disabled) {
            ngx_del_event(c->write, NGX_WRITE_EVENT, NGX_CLOSE_EVENT);
        }
    }
}

if (c->read->posted)  { ngx_delete_posted_event(c->read);  }   // 摘 posted
if (c->write->posted) { ngx_delete_posted_event(c->write); }

c->read->closed = 1;
c->write->closed = 1;                  // 关键：防过期事件

ngx_reusable_connection(c, 0);          // 移出可复用队列
log_error = c->log_error;
ngx_free_connection(c);                 // 归还结构体
fd = c->fd;
c->fd = (ngx_socket_t) -1;

if (c->shared) {
    return;                             // 共享连接不关 fd
}

if (ngx_close_socket(fd) == -1) { ... }  // 真正 close()
```

关闭顺序很有讲究：**先从所有调度机构（定时器、epoll、posted 队列）摘除，再标记 `closed = 1`，最后才 `close(fd)`**。`closed = 1` 配合 4.1 讲的 `instance` 位，构成双重保险：即便 epoll 里仍有该 fd 的残留事件（fd 复用导致的过期事件），handler 也会因 `closed` 或 `instance` 不匹配而丢弃它，避免把事件错派给新连接。

注意 `ngx_free_connection` 在 `ngx_close_socket` **之前**——连接结构体先归还，fd 后关闭。而内存池的销毁不在这里做（`ngx_close_connection` 不碰 `c->pool`），内存池由协议层在 finalize 时销毁，职责分离。

**半成品连接的关闭 `ngx_close_accepted_connection`**——[src/event/ngx_event_accept.c:498-520](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_accept.c#L498-L520)，用于 accept 过程中出错、连接还没交接给协议层就需要清理的场景：

```c
ngx_free_connection(c);
fd = c->fd;
c->fd = (ngx_socket_t) -1;
if (ngx_close_socket(fd) == -1) { ... }
if (c->pool) {
    ngx_destroy_pool(c->pool);          // 这里销毁池
}
```

它比 `ngx_close_connection` 简单得多——因为这时连接还没注册进 epoll、没挂定时器、没进 posted 队列，所以无需那些摘除动作，直接「归还结构体 → 关 fd → 销毁池」三步即可。对比两个函数能看出：关闭的复杂度全部来自「连接已被各调度机构引用」这一事实。

**被动回收 `ngx_drain_connections`**——[src/core/ngx_connection.c:1404-1458](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1404-L1458)：

```c
if (cycle->free_connection_n > cycle->connection_n / 16
    || cycle->reusable_connections_n == 0)
{
    return;                             // 空闲够 / 无可复用：不回收
}
...
n = ngx_max(ngx_min(32, cycle->reusable_connections_n / 8), 1);

for (i = 0; i < n; i++) {
    if (ngx_queue_empty(&cycle->reusable_connections_queue)) {
        break;
    }
    q = ngx_queue_last(&cycle->reusable_connections_queue);
    c = ngx_queue_data(q, ngx_connection_t, queue);
    c->close = 1;
    c->read->handler(c->read);          // 触发协议层优雅关闭
}
```

当空闲连接数跌破总量的 1/16 且存在「可复用连接」时，从可复用队列尾部取若干个（最多 32，最少 1），置 `c->close = 1` 后调用其读 handler。这并非粗暴 `close(fd)`，而是让协议层走它自己的优雅关闭流程（如 HTTP 的 lingering close），从而把连接结构体释放回池。

**可复用连接队列 `ngx_reusable_connection`**——[src/core/ngx_connection.c:1373-1401](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1373-L1401)：

```c
if (c->reusable) {
    ngx_queue_remove(&c->queue);
    cycle->reusable_connections_n--;
}
c->reusable = reusable;
if (reusable) {
    ngx_queue_insert_head(... &cycle->reusable_connections_queue, &c->queue);
    cycle->reusable_connections_n++;
}
```

协议层把一条空闲 keepalive 连接标记为「可复用」（`ngx_reusable_connection(c, 1)`），就是把它插到这个队列头部；`ngx_drain_connections` 从尾部取，即优先关闭「最老」的空闲连接。`ngx_connection_t.queue` 字段（[src/core/ngx_connection.h:170](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.h#L170)）就是为这个侵入式链表准备的节点（侵入式链表的概念见 u2-l3）。

#### 4.4.4 代码实践

**实践目标**：制造连接池压力，观察 `ngx_drain_connections` 的回收行为，把它与源码对应起来。

**操作步骤**：

1. 编译一个带 `--with-debug` 的 nginx（构建方式见 u1-l2）。
2. 写一个最小配置，把 `worker_connections` 调到很小（如 32），开一个 HTTP server，`keepalive_timeout 65;`。
3. 用压测工具发起超过 32 条并发长连接，例如 `ab -k -c 64 -n 1000 http://127.0.0.1/`。
4. 把 `error_log` 级别调到 `warn`，观察日志。

**需要观察的现象**：当并发连接数逼近 `worker_connections` 上限时，error_log 里应出现类似 `"N worker_connections are not enough, reusing connections"` 的告警（来自 [src/core/ngx_connection.c:1420-1423](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1420-L1423)），且每秒至多一条（`connections_reuse_time` 节流）。随后部分空闲 keepalive 连接被强制关闭，新连接得以建立。

**预期结果**：日志告警与源码 `ngx_drain_connections` 的触发条件（`free_connection_n <= connection_n/16`）完全对应；调大 `worker_connections` 后告警消失。具体日志条数与压测环境相关，待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_close_connection` 为什么要在 `ngx_close_socket(fd)` 之前先把事件从定时器、epoll、posted 队列里逐一摘除？

**参考答案**：fd 关闭后，内核可能把它复用给后续新建连接。若不先摘除，残留的定时器回调或 posted 事件仍可能带着旧的 `ngx_event_t` 指针被调用，造成 use-after-free 或把事件错派给新连接。先摘除再加 `closed = 1` 标记（配合 `instance` 位），确保即便有漏网事件也会被安全丢弃。

**练习 2**：`ngx_close_accepted_connection` 与 `ngx_close_connection` 为何要分两个函数？

**参考答案**：`ngx_close_accepted_connection` 处理的是「刚 accept、尚未交接给协议层」的连接——它还没注册进 epoll、没挂定时器、没进 posted 队列，所以只需「归还结构体 + 关 fd + 销毁池」三步。`ngx_close_connection` 处理的是「已运行的」连接，必须先从各调度机构摘除。分两个函数避免了给半成品连接做一堆无意义的摘除检查，也让职责更清晰。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「端到端追踪 + 压力观察」：

1. **配置**：用 u1-l2 的方法编译 nginx，配置一个监听 8080 的 HTTP server，`worker_connections 64;`，`error_log logs/error.log warn;`，`keepalive_timeout 30s;`。
2. **静态追踪**：在源码里用笔走通这条链——`ngx_event_process_init`（预分配池、装 `ngx_event_accept`）→ epoll 就绪 → `ngx_event_accept`（`accept4` → `ngx_get_connection` → `ngx_create_pool` → `ls->handler`）→ `ngx_http_init_connection`。在每一步旁边标注它对应的源码行号与本讲的哪一节。
3. **内存计数**：按 4.2.4 的方法，确认接受一条连接在 `ngx_event_accept` 内只触发 1 次系统 `malloc`（`ngx_create_pool`）。
4. **压力观察**：用 `ab -k -c 100 -n 2000 http://127.0.0.1:8080/` 制造超过 64 并发的长连接，观察 error.log 是否出现 `worker_connections are not enough, reusing connections`，并把它对应到 `ngx_drain_connections`（4.4）。
5. **关闭追踪**：压测结束后，跟踪一条 keepalive 连接超时被关闭的路径，确认它经过 `ngx_close_connection` 的「摘定时器 → 摘 epoll → 标 closed → `ngx_free_connection` → `ngx_close_socket`」顺序。

完成后，你应当能用一张图把「连接从无到有、从有到回收」的完整生命周期与源码位置一一对应。

## 6. 本讲小结

- nginx 在 worker 启动时一次性预分配 `connection_n` 个 `ngx_connection_t` 与等量读写事件，串成空闲链表；`ngx_get_connection` 摘头、`ngx_free_connection` 头插，取放均 O(1) 且零系统分配。
- `ngx_event_accept` 是监听套接字读事件的 handler，循环 `accept4`/`accept` 取出就绪连接，每条连接建一个专属内存池（`ngx_create_pool`），这是 accept 路径上唯一的系统 `malloc`。
- 新连接的写事件直接标 `wev->ready = 1`（刚握手完必可写），读事件默认不标就绪；`ngx_event_accept` 末尾调 `ls->handler(c)`（HTTP 层即 `ngx_http_init_connection`）完成向协议层交接。
- `ngx_accept_disabled = connection_n/8 - free_connection_n` 这个本地量为多 worker 负载均衡服务；EMFILE/ENFILE 时 nginx 会摘掉本 worker 的监听事件以防 ET 死循环。
- `ngx_close_connection` 严格按「摘定时器 → 摘 epoll → 摘 posted → 标 `closed` → 归还结构体 → 关 fd」顺序执行，`closed` 位与 `instance` 位共同防御 fd 复用导致的过期事件。
- 资源紧张时 `ngx_drain_connections` 从可复用连接队列尾部取若干空闲 keepalive 连接，置 `c->close = 1` 触发协议层优雅关闭，腾出连接结构体。

## 7. 下一步学习建议

- **u5-l4（定时器与 posted 事件）**：本讲多次提到「从定时器/posted 队列摘除事件」，下一讲将讲清定时器红黑树与两级 posted 队列的运作，补全连接在调度机构里的全貌。
- **u5-l5（事件主循环 `ngx_process_events_and_timers`）**：理解 `ngx_accept_disabled` 与 `ngx_trylock_accept_mutex` 如何在主循环里被消费，把本讲的负载均衡信号接到完整调度链上。
- **u6-l2（HTTP 请求生命周期）**：从 `ngx_http_init_connection`（本讲的交接终点）继续往下，看协议层如何设置 `rev->handler` 并推进请求状态机。
- 重读 `src/core/ngx_connection.c` 中 `ngx_connection_local_sockaddr`、`ngx_tcp_nodelay`、`ngx_connection_error` 等辅助函数，它们在后续 HTTP/upstream 讲义中会被反复调用。
