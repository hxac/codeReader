# 事件模型总览 ngx_event

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `ngx_event_t` 这个结构体里每个关键字段的作用，以及 `active`、`ready`、`eof`、`error` 这些状态位分别在什么场景被置位。
- 理解 `ngx_event_actions_t` 这张函数指针表如何把「事件后端」（epoll / kqueue / poll / select …）的差异屏蔽掉，让上层用同一套 `ngx_add_event` / `ngx_process_events` 接口编程。
- 看懂 `events {}` 配置块从被解析、`use` 指令选定后端、到 `ngx_event_init_conf` 自动挑选后端、再到 worker 启动时 `ngx_event_process_init` 真正把事件机制跑起来的完整时序。

本讲是整个第五单元「事件驱动核心」的地基。它不深入任何一个后端（epoll 的细节留给 u5-l2，accept 与 connection 管理留给 u5-l3，定时器与 posted 队列留给 u5-l4，主循环留给 u5-l5），而是先把「事件」这个概念在 nginx 里到底长什么样、用什么接口操作讲清楚。

## 2. 前置知识

阅读本讲前，你需要已经建立以下认知（来自前四单元）：

- **master/worker 进程模型**（u4-l1）：master 不处理业务连接，真正处理请求的是 worker；每个 worker 跑在一个 `ngx_worker_process_cycle` 里，循环主体是 `ngx_process_events_and_timers`。
- **worker 的初始化**（u4-l1）：worker 在 `ngx_worker_process_init` 里会调用所有模块的 `init_process` 回调。事件机制的「在 worker 里跑起来」就发生在这之后。
- **模块系统**（u3-l3）：每个模块是一个 `ngx_module_t`，靠 `type` 区分种类（`NGX_CORE_MODULE` / `NGX_EVENT_MODULE` / `NGX_HTTP_MODULE` …），靠 `ctx` 携带类内回调表；`ctx_index` 是「类内编号」，用来索引本类模块的配置数组。
- **配置解析**（u3-l1、u3-l2）：`nginx.conf` 里的 `events {}` 是一个块指令，进入块时 `ngx_conf_parse` 会递归，切换 `cf->cmd_type` 为 `NGX_EVENT_CONF` 来限定哪些指令允许出现在块里。

如果你对「一个连接上有读、写两个事件」「事件就绪后回调一个 handler」这种事件驱动编程的基本模型完全陌生，建议先回顾 u4-l1 里关于 worker 事件循环的描述。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/event/ngx_event.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h) | 定义 `ngx_event_t`（事件本身）、`ngx_event_actions_t`（后端接口表）、`ngx_event_conf_t`（events 块配置）、`ngx_event_module_t`（事件模块 ctx）以及一堆 `NGX_USE_*` 后端能力标志。 |
| [src/event/ngx_event.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c) | 事件框架的实现：`events` 块指令处理、`use`/`worker_connections` 指令、`ngx_event_init_conf`、worker 侧的 `ngx_event_process_init`、主循环驱动 `ngx_process_events_and_timers`、读写事件注册辅助 `ngx_handle_read_event`/`ngx_handle_write_event`。 |
| [src/event/modules/ngx_epoll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c) | Linux epoll 后端。本讲只引用它作为「一个后端如何填充 `ngx_event_actions_t`」的例子，详细解析在 u5-l2。 |
| [src/core/ngx_core.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_core.h) | 定义事件 handler 的函数指针类型 `ngx_event_handler_pt`。 |
| [src/os/unix/ngx_recv.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c) | `ngx_unix_recv` 的实现，是观察 `eof`/`error`/`ready` 位如何被置位的最直接位置。 |

一句话定位：**`ngx_event.h` 是事件的「形状」，`ngx_event.c` 是事件的「骨架」，各 `ngx_*_module.c` 是填进骨架里的「肌肉」**。

## 4. 核心概念与源码讲解

### 4.1 ngx_event_t：一个事件长什么样

#### 4.1.1 概念说明

在 nginx 里，「事件」(event) 是一切调度的原子单位。一个 TCP 连接上有两个事件：一个读事件、一个写事件；一个监听套接字上有一个 accept 事件；一个定时器到期也是一个事件。事件驱动循环做的事情可以概括成一句话：

> 问内核「哪些 fd 就绪了」→ 拿到就绪事件列表 → 对每个事件调用它的 `handler` 回调。

所以一个事件需要承载三样信息：

1. **我是谁的事件**——用 `data` 指针指向所属的 `ngx_connection_t`（绝大多数情况）。
2. **我现在是什么状态**——用一串 1 位的标志位（`active`/`ready`/`eof`/`error`/`timedout` …）描述。
3. **就绪后该干什么**——用 `handler` 函数指针记录回调。

理解 `ngx_event_t` 的关键，是认识到它是一个**极轻量、被频繁复用**的结构体：nginx 不会为每个请求 new 一个事件，而是在 worker 启动时一次性分配 `worker_connections` 个 `ngx_connection_t`、同样数量的读事件和写事件（见 4.3 节），之后所有连接都在这组预分配的事件上循环使用。

#### 4.1.2 核心流程

一个事件从生到死的典型状态流转：

1. **创建**：worker 启动时预分配事件数组，初始 `closed=1`、`instance=1`（标记为「未启用」）。
2. **绑定**：连接被 accept 时，从空闲池取出一个 connection，它的 read/write 事件被填上 `data`、`log`、`handler`。
3. **注册到内核**：调用 `ngx_add_event`（底层走 epoll_ctl ADD），成功后 `active=1`。
4. **就绪**：内核通知 fd 可读/可写，`ngx_epoll_process_events` 把 `ready=1`，然后调用 `handler`。
5. **处理**：`handler` 里做实际 I/O。读到 EAGAIN 时 `ready=0`（暂时没数据）；读到 0 时 `eof=1`；读到 -1 且非 EAGAIN 时 `error=1`。
6. **注销**：连接关闭时 `ngx_del_event`（epoll_ctl DEL），`active=0`，事件归还空闲池。

整套流转的「状态机」就藏在那些 1 位标志位里——这也是为什么本讲要把每个位都讲清楚。

#### 4.1.3 源码精读

事件结构体的完整定义在 [src/event/ngx_event.h:30-138](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L30-L138)。我们逐段拆开看。

**身份与归属字段**：

```c
struct ngx_event_s {
    void            *data;          // 通常指向所属 ngx_connection_t
    unsigned         write:1;       // 1=写事件, 0=读事件
    unsigned         accept:1;      // 1=监听套接字上的 accept 事件
    unsigned         instance:1;    // 用于检测 kqueue/epoll 的陈旧事件
    ...
    ngx_event_handler_pt  handler;  // 就绪回调
    ngx_log_t       *log;
    ngx_rbtree_node_t   timer;      // 嵌入式定时器树节点
    ngx_queue_t      queue;         // posted 队列链接
};
```

- `data` 是事件的「上下文」：handler 收到的只有 `ngx_event_t *ev` 一个参数，要知道自己在处理哪个连接，就从 `ev->data` 取。它一般指向 `ngx_connection_t`。
- `write` 区分读/写事件——一个连接的 `c->read` 和 `c->write` 是两个独立 `ngx_event_t`，共享同一个 fd，但靠这个位区分。
- `handler` 的类型是 `ngx_event_handler_pt`，定义在 [src/core/ngx_core.h:35](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_core.h#L35)：`typedef void (*ngx_event_handler_pt)(ngx_event_t *ev);`。注意它返回 `void`——事件处理是否「完成」要靠事件自身的标志位和后续逻辑判断，而不是返回值。
- `timer` 和 `queue` 是**嵌入式**容器节点（回顾 u2-l3 的「侵入式容器」）：事件不需要单独分配一个定时器节点或队列节点，结构体本身就内嵌了链接字段，可以直接挂进定时器红黑树和 posted 队列。

**状态标志位**（这是本讲的重点，对应实践任务）：

```c
unsigned         active:1;      // 已注册到内核
unsigned         disabled:1;    // 临时禁用（kqueue）
unsigned         ready:1;       // 内核报告就绪 / 可立即做 I/O
unsigned         oneshot:1;     // 一次性事件，通知后自动删除
unsigned         complete:1;    // aio 操作完成
unsigned         eof:1;         // 对端关闭 / 读到文件尾
unsigned         error:1;       // 发生错误
unsigned         timedout:1;    // 定时器到期
unsigned         timer_set:1;   // 已在定时器红黑树中
unsigned         delayed:1;     // 被推迟到下一轮处理
unsigned         pending_eof:1; // kqueue/epoll 报告的「待处理」eof
unsigned         posted:1;      // 已在某 posted 队列中
unsigned         closed:1;      // 事件/连接已关闭
unsigned         cancelable:1;  // 可取消（不阻止 worker 退出）
```

源码里每个位都带注释，例如 [src/event/ngx_event.h:40-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L40-L49) 对 `instance`、`active`、`ready` 的注释。读 nginx 源码时，这些行内注释往往是理解字段语义最权威的资料。

**四个关键位的精确置位场景**（这是本讲实践任务要你回答的问题，下面给出源码依据）：

| 标志位 | 置 1 的场景 | 源码依据 |
| --- | --- | --- |
| `active` | 事件被 `ngx_add_event` 成功注册到内核（epoll_ctl ADD/MOD 成功）后置 1；`ngx_del_event` 注销时置 0。 | epoll 后端在 `ngx_epoll_add_event` 里 `epoll_ctl` 成功后执行 `ev->active = 1`，见 [ngx_epoll_module.c:621-635](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L621-L635)；删除时 `ev->active = 0`。 |
| `ready` | 内核通知 fd 就绪时，`ngx_epoll_process_events` 检测到 `revents & EPOLLIN/EPOLLOUT` 且 `active`，置 `ready = 1`；I/O 调用遇到 EAGAIN 或读完/写满后置 0，表示「暂时不能再做 I/O」。 | 置 1 见 [ngx_epoll_module.c:883-921](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L883-L921)；置 0 见 `ngx_unix_recv` 里 `rev->ready = 0`，如 [ngx_recv.c:76-94](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c#L76-L94)。 |
| `eof` | `recv()` 返回 0（对端正常关闭连接）时置 1；kqueue 下 `pending_eof && available==0` 时也置 1。 | [ngx_recv.c:76-94](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c#L76-L94)（`n == 0` 分支里 `rev->eof = 1`）。 |
| `error` | I/O 系统调用返回 -1 且 errno 不是 `EAGAIN`/`EWOULDBLOCK`（真实错误，而非「暂时无数据」）时置 1；kqueue 下带 `kq_errno` 时置 1。 | [ngx_recv.c:193-200](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c#L193-L200)（错误分支里 `rev->error = 1`）。 |

这里有一个常被初学者忽略的细节：**`eof` 和 `error` 不是在 `process_events` 里置的，而是在后续真正做 I/O 的 `recv`/`send` 里置的**。`process_events` 只负责把 `ready` 置 1 并调用 handler；handler 内部调用 `ngx_unix_recv` 读数据时，才根据 `recv()` 的返回值判定是 eof 还是 error。理解这一点，才能看懂为什么 nginx 的错误处理总是「事件就绪 → 读一下 → 看返回值」的三步走。

另一个值得注意的位是 `instance`：epoll 的 `event.data.ptr` 里不仅存了 connection 指针，还把 `instance` 位编码进指针的最低位（[ngx_epoll_module.c:621](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L621)）。取出事件时再把这位拆出来和 `rev->instance` 比较，不等就说明这是「fd 被复用前的陈旧事件」，直接跳过（[ngx_epoll_module.c:844-854](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L844-L854)）。这是 epoll 模式下避免「使用已关闭 fd 的事件」的关键 trick，u5-l2 会展开。

#### 4.1.4 代码实践

**实践目标**：把 `ngx_event_t` 的关键字段列成表，并用自己的话写清楚 `active`、`ready`、`eof`、`error` 四个位分别在什么场景被置位。这是一道「源码阅读型实践」，目的是逼自己回到源码而不是凭印象。

**操作步骤**：

1. 打开 [src/event/ngx_event.h:30-138](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L30-L138)，把所有字段抄进一张表，分三列：字段名、C 类型、行内注释的中文翻译。
2. 对 `active`：在 [ngx_epoll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c) 里搜索 `ev->active =`，记录每次赋值发生的函数名和上下文（ADD、MOD、DEL）。
3. 对 `ready`：在 [ngx_epoll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c) 里搜索 `rev->ready = 1` 和 `wev->ready = 1`；再在 [ngx_recv.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c) 里搜索 `rev->ready = 0`，对比「谁置 1、谁置 0」。
4. 对 `eof` 和 `error`：在 [ngx_recv.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c) 里定位 `rev->eof = 1` 和 `rev->error = 1` 各自所在的 `recv()` 返回值分支。

**需要观察的现象**：

- `active` 的赋值只出现在后端模块（epoll/poll/…）的 add/del 函数里，不出现在 `ngx_event.c` 里——这印证了 `active` 是「与内核注册状态」的语义。
- `ready` 既有置 1（后端 process_events）也有置 0（I/O 包装函数），是个「会被反复翻转」的位。
- `eof`/`error` 都出现在 `recv()` 返回值的判定分支里，且 `error` 几乎总伴随一次 `ngx_set_socket_errno` 和 `ngx_connection_error` 调用。

**预期结果**：你会得到一张类似 4.1.3 节那张表的笔记，并且能口头复述：「`active` 由后端 add/del 维护，`ready` 由后端置 1、由 I/O 包装置 0，`eof`/`error` 由 I/O 包装根据 `recv` 返回值置位」。

**待本地验证**：如果你在本地用 `--with-debug` 编译并开了 `error_log ... debug_event`，可以在一次短连接的 GET 请求日志里追踪到 `ev->ready` 由 1 变 0、再到 `eof=1` 的完整序列，与本实践的结论对照。

#### 4.1.5 小练习与答案

**练习 1**：一个 `ngx_connection_t` 上为什么需要两个 `ngx_event_t`（read 和 write）而不是一个？  
**答案**：因为读和写是两件独立的事，就绪时机不同、handler 不同、状态也不同。客户端可能「可读但不可写」（比如对端发完数据但 TCP 接收窗口满），nginx 需要分别注册 `EPOLLIN` 和 `EPOLLOUT`、分别回调。用一个事件无法独立表达两边的就绪状态，所以拆成两个，靠 `write:1` 位区分。

**练习 2**：`ready` 位被置 0 之后，事件如何再次变得「可处理」？  
**答案**：`ready=0` 表示这次 I/O 暂时做不动（比如读到 EAGAIN）。由于 epoll 默认用边缘触发（`NGX_USE_CLEAR_EVENT`），nginx 不会自动再次被通知；上层会通过 `ngx_handle_read_event` 重新确认事件已注册（`active` 还在就什么都不做），等内核下次有新数据到达时，`ngx_epoll_process_events` 会再次把 `ready` 置 1 并调 handler。也就是说，「再次就绪」依赖内核的新通知，而不是 nginx 自己轮询。

---

### 4.2 ngx_event_actions_t：事件后端的统一接口

#### 4.2.1 概念说明

nginx 要在 Linux（epoll）、FreeBSD/macOS（kqueue）、老系统（poll/select）、Solaris（event ports / devpoll）、Windows（IOCP）等多种系统上跑，而这些系统提供的「多路复用」机制各不相同。如果业务代码直接调 `epoll_wait`，就绑死 Linux 了。

nginx 的解法和 u4-l4 的 OS 抽象层一脉相承：**定义一张函数指针表 `ngx_event_actions_t`，规定「事件后端必须实现哪些操作」，业务代码只通过这张表间接调用**。每个后端（epoll 模块、kqueue 模块……）各自实现这套函数，启动时把选中的后端的表赋给全局变量 `ngx_event_actions`，之后整个程序就统一用这张表干活。

这是一个典型的「接口与实现分离」设计，也叫做「后端可插拔」。它带来的好处是：HTTP 模块、stream 模块、SSL 模块……所有上层代码都写 `ngx_add_event(rev, ...)` 而不是 `epoll_ctl(...)`，换系统时上层一行不改。

#### 4.2.2 核心流程

后端接口表的工作机制可以画成这样：

```
        上层代码                     全局变量                     具体后端
  ┌─────────────────┐         ┌──────────────────┐         ┌──────────────────┐
  │ ngx_add_event   │  宏展开  │ ngx_event_actions│  指向    │ epoll 模块的     │
  │ ngx_del_event   │ ──────> │   .add / .del    │ ──────> │ ngx_epoll_add_..│
  │ ngx_process_    │         │   .process_events│         │ ngx_epoll_proc..│
  │   events        │         │   .init / .done  │         │ ...              │
  └─────────────────┘         └──────────────────┘         └──────────────────┘
```

关键三步：

1. **编译期**：每个后端模块在自己的 `ngx_event_module_t` 里填一张 `actions` 表。
2. **启动期**：`ngx_event_process_init` 找到被 `use` 选中的后端，调它的 `actions.init`；`init` 内部把本模块的 `actions` 表赋给全局 `ngx_event_actions`。
3. **运行期**：上层调用 `ngx_add_event` 等宏，宏展开成 `ngx_event_actions.add(...)`，实际跳到选中后端的实现。

#### 4.2.3 源码精读

接口表的类型定义在 [src/event/ngx_event.h:166-183](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L166-L183)：

```c
typedef struct {
    ngx_int_t  (*add)(ngx_event_t *ev, ngx_int_t event, ngx_uint_t flags);
    ngx_int_t  (*del)(ngx_event_t *ev, ngx_int_t event, ngx_uint_t flags);

    ngx_int_t  (*enable)(ngx_event_t *ev, ngx_int_t event, ngx_uint_t flags);
    ngx_int_t  (*disable)(ngx_event_t *ev, ngx_int_t event, ngx_uint_t flags);

    ngx_int_t  (*add_conn)(ngx_connection_t *c);
    ngx_int_t  (*del_conn)(ngx_connection_t *c, ngx_uint_t flags);

    ngx_int_t  (*notify)(ngx_event_handler_pt handler);

    ngx_int_t  (*process_events)(ngx_cycle_t *cycle, ngx_msec_t timer,
                                 ngx_uint_t flags);

    ngx_int_t  (*init)(ngx_cycle_t *cycle, ngx_msec_t timer);
    void       (*done)(ngx_cycle_t *cycle);
} ngx_event_actions_t;
```

理解这张表，把它分成四组：

| 分组 | 函数 | 作用 |
| --- | --- | --- |
| 事件级增删 | `add` / `del` | 把单个读/写事件注册到内核或从内核移除（对应 epoll_ctl 的 ADD/MOD/DEL）。 |
| 事件级启停 | `enable` / `disable` | 临时启用/禁用某事件，主要用于 kqueue 避免内核里频繁 malloc/free。多数后端直接复用 `add`/`del`。 |
| 连接级增删 | `add_conn` / `del_conn` | 一次性把一个连接的读、写两个事件都注册/移除（某些后端如 IOCP 更习惯以连接为单位）。 |
| 跨进程通知 | `notify` | 触发一个「跨 worker」的异步通知（基于 eventfd），让目标 worker 执行给定 handler。 |
| 主循环 | `process_events` | 阻塞等待内核事件就绪、把就绪事件的 `ready` 置位、调它们的 handler。这是 worker 主循环每轮都要调的核心。 |
| 生命周期 | `init` / `done` | worker 启动时初始化后端（如 `epoll_create`）、worker 退出时清理。 |

全局变量声明在 [src/event/ngx_event.h:186](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L186)：`extern ngx_event_actions_t ngx_event_actions;`，定义在 [ngx_event.c:44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L44)。

上层不是直接写 `ngx_event_actions.add(...)`，而是用一组宏，见 [src/event/ngx_event.h:400-408](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L400-L408)：

```c
#define ngx_process_events   ngx_event_actions.process_events
#define ngx_done_events      ngx_event_actions.done

#define ngx_add_event        ngx_event_actions.add
#define ngx_del_event        ngx_event_actions.del
#define ngx_add_conn         ngx_event_actions.add_conn
#define ngx_del_conn         ngx_event_actions.del_conn

#define ngx_notify           ngx_event_actions.notify
```

这样上层代码里写的是 `ngx_add_event(rev, NGX_READ_EVENT, NGX_CLEAR_EVENT)`，看着像函数调用，实际在预处理阶段就被替换成 `ngx_event_actions.add(rev, NGX_READ_EVENT, NGX_CLEAR_EVENT)`——一次指针间接调用，开销可以忽略。

**看一个真实后端怎么填这张表**：epoll 模块在 [ngx_epoll_module.c:179-200](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L179-L200) 把自己的函数填进 `ngx_event_module_t.actions`：

```c
static ngx_event_module_t  ngx_epoll_module_ctx = {
    &epoll_name,
    ngx_epoll_create_conf,
    ngx_epoll_init_conf,
    {
        ngx_epoll_add_event,        /* add    */
        ngx_epoll_del_event,        /* del    */
        ngx_epoll_add_event,        /* enable */
        ngx_epoll_del_event,        /* disable*/
        ngx_epoll_add_connection,   /* add_conn */
        ngx_epoll_del_connection,   /* del_conn */
        ngx_epoll_notify,           /* notify (eventfd) */
        ngx_epoll_process_events,   /* process_events */
        ngx_epoll_init,             /* init   */
        ngx_epoll_done,             /* done   */
    }
};
```

注意 `enable`/`disable` 直接复用了 `add`/`del`——epoll 没有 kqueue 那种「临时禁用但不删除」的能力，所以用增删近似实现。

「把表交给全局变量」这件事发生在 `init` 里，见 [ngx_epoll_module.c:369-377](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L369-L377)：

```c
ngx_event_actions = ngx_epoll_module_ctx.actions;

#if (NGX_HAVE_CLEAR_EVENT)
ngx_event_flags = NGX_USE_CLEAR_EVENT
#else
ngx_event_flags = NGX_USE_LEVEL_EVENT
#endif
                  |NGX_USE_GREEDY_EVENT
                  |NGX_USE_EPOLL_EVENT;
```

这里同时设置了 `ngx_event_flags`——这是另一组「后端能力标志」，定义在 [src/event/ngx_event.h:196-268](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L196-L268)。它和 `actions` 表的分工是：

- `actions` 表回答「**怎么做**」（调用哪个函数）。
- `ngx_event_flags` 回答「**这个后端有什么特性**」（要不要用边缘触发、要不要读到 EAGAIN、能不能低水位……）。

上层代码会根据 `ngx_event_flags` 决定行为。最典型的例子是 `ngx_handle_read_event`，见 [ngx_event.c:267-344](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L267-L344)：

```c
if (ngx_event_flags & NGX_USE_CLEAR_EVENT) {
    /* kqueue, epoll：边缘触发，注册一次即可，就绪后不必反复 add/del */
    if (!rev->active && !rev->ready) {
        if (ngx_add_event(rev, NGX_READ_EVENT, NGX_CLEAR_EVENT) == NGX_ERROR)
            return NGX_ERROR;
    }
    return NGX_OK;

} else if (ngx_event_flags & NGX_USE_LEVEL_EVENT) {
    /* select, poll：水平触发，就绪后必须 del，否则会被反复通知 */
    ...
}
```

这段代码完美体现了两套抽象的协作：`ngx_event_flags` 判断「我现在用的是哪种触发模式」，`ngx_add_event` 宏完成「实际去注册」。上层不关心底层是 epoll 还是 kqueue，只要 `NGX_USE_CLEAR_EVENT` 这一位被置，就走边缘触发的注册路径。

几个常用能力标志（[ngx_event.h:196-268](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L196-L268)）：

| 标志 | 含义 | 典型后端 |
| --- | --- | --- |
| `NGX_USE_LEVEL_EVENT` | 水平触发，需读/写完整数据 | select、poll |
| `NGX_USE_CLEAR_EVENT` | 边缘触发，只在状态变化时通知一次 | epoll、kqueue |
| `NGX_USE_GREEDY_EVENT` | 就绪后要一直读到 EAGAIN | epoll |
| `NGX_USE_KQUEUE_EVENT` | 支持 eof/errno/available 等 kqueue 特性 | kqueue |
| `NGX_USE_FD_EVENT` | 需要额外维护 fd→event 的索引表 | poll、/dev/poll |
| `NGX_USE_LOWAT_EVENT` | 支持低水位（NOTE_LOWAT） | kqueue |

#### 4.2.4 代码实践

**实践目标**：对比至少两个事件后端，看它们如何用不同实现填同一张 `ngx_event_actions_t` 表，体会「接口相同、实现各异」。

**操作步骤**：

1. 在 `src/event/modules/` 目录下列出所有后端模块文件（`ngx_epoll_module.c`、`ngx_poll_module.c`、`ngx_select_module.c`、`ngx_kqueue_module.c` 等）。
2. 对 epoll（[ngx_epoll_module.c:179-200](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L179-L200)）和 poll 两个模块，分别找到它们的 `ngx_event_module_t` 上下文结构，把 `actions` 表里每个槽位填的函数名抄下来。
3. 对比两边的 `process_events` 槽：epoll 填的是 `ngx_epoll_process_events`（底层 `epoll_wait`），poll 填的是 `ngx_poll_process_events`（底层 `poll`）。
4. 对比两边的 `init` 槽里设置的 `ngx_event_flags`：epoll 设了 `NGX_USE_CLEAR_EVENT`（边缘触发），poll 设的是 `NGX_USE_LEVEL_EVENT`（水平触发）。

**需要观察的现象**：

- 两个后端的 `actions` 表「形状」完全一样（同样的 10 个槽位），但每个槽填的函数名不同。
- 它们设置的 `ngx_event_flags` 不同，这正是 4.2.3 节里 `ngx_handle_read_event` 能据 `ngx_event_flags` 走不同分支的依据。

**预期结果**：你会直观看到「同一接口、多种实现」，并理解 nginx 为何能在不改动上层的前提下切换事件后端。

**待本地验证**：可在一台 Linux 机器上分别用默认（epoll）和强制 `use poll;` 编译运行，用 `nginx -V` 确认两者都编入了 poll 模块，再观察 `error.log` 启动行里 `using the "epoll" event method` 与 `using the "poll" event method` 的区别。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_add_event` 是一个宏而不是普通函数？  
**答案**：因为要直接展开成 `ngx_event_actions.add(...)` 这种「取函数指针表的字段再调用」的形式。写成宏可以避免多一层函数调用栈（虽然现代编译器可能内联普通函数，但宏保证语义上是直接的指针间接调用）。同时它让上层代码读起来像在调一个稳定的 API，隐藏了「其实是通过全局函数指针表分发」的实现细节。

**练习 2**：`actions` 表和 `ngx_event_flags` 为什么不合并成一个？  
**答案**：两者粒度不同。`actions` 描述「有哪些操作可调用」，是行为接口；`ngx_event_flags` 描述「后端具备哪些特性」，是能力声明。一个后端可能用相同的 `add` 函数但具备不同能力（比如 epoll 的 `add` 既支持边缘也支持水平，靠传入的 `NGX_CLEAR_EVENT`/`NGX_LEVEL_EVENT` flag 区分）。上层有时只需要知道「是不是边缘触发」这种能力信息而不需要真的调用 add，这时查 `ngx_event_flags` 即可，不必走函数指针。把两者分开让「问能力」和「做操作」各走各的路径，更清晰。

---

### 4.3 ngx_event_init_conf 与 events 块初始化

#### 4.3.1 概念说明

前两节讲了「事件是什么」和「用什么接口操作」。这一节回答一个流程问题：**`events {}` 配置块是怎么变成一个跑起来的事件机制的？**

这里涉及三个容易混淆的函数，先把它们的名字和定位说清楚：

| 函数 | 所属 | 调用时机 | 干什么 |
| --- | --- | --- | --- |
| `ngx_event_init_conf` | `ngx_events_module`（CORE 模块）的 ctx.init_conf | cycle 初始化末尾（配置解析后） | 校验：必须有 `events {}` 块；`worker_connections` 够不够分给所有监听套接字。 |
| `ngx_event_core_init_conf` | `ngx_event_core_module`（EVENT 模块）的 ctx.init_conf | `events {}` 块解析末尾 | 给 events 配置补默认值，**自动挑选事件后端**（epoll/kqueue/…）。 |
| `ngx_event_process_init` | `ngx_event_core_module` 的 init_process 回调 | 每个 worker 启动时 | 真正「把事件机制跑起来」：调后端 `init`、分配连接与事件数组、把监听 fd 注册进后端。 |

三者名字相近但分工不同，初学时常被绕晕。记住一条主线：**`init_conf` 系列负责「配置层」（解析完配置后填默认值/校验），`process_init` 负责「运行时层」（worker 启动时真正建数据结构）**。这和 u3-l3 讲的「配置层回调 vs 运行时层回调」是同一套分类。

#### 4.3.2 核心流程

`events {}` 从配置文本到跑起来的事件机制，经过这些阶段：

1. **解析 `events` 块指令** → 触发 `ngx_events_block`：
   - 数一下有多少个 EVENT 模块，给它们分配 `ctx_index`（`ngx_count_modules`）。
   - 为每个有 `create_conf` 的 EVENT 模块建空配置结构（字段先填 `NGX_CONF_UNSET*` 哨兵）。
   - 切换 `cf->cmd_type = NGX_EVENT_CONF`，递归 `ngx_conf_parse` 解析块内指令（`worker_connections`、`use`、`multi_accept` 等）。
   - 解析完，对每个 EVENT 模块调 `init_conf`——其中 `ngx_event_core_init_conf` 负责补默认值和选后端。
2. **cycle 初始化末尾** → `ngx_event_init_conf`：做跨模块的硬性校验（events 块存在性、连接数下限）。
3. **worker 启动** → `ngx_event_process_init`：
   - 根据 `master && worker_processes>1 && accept_mutex` 决定是否启用 accept 互斥。
   - 初始化 posted 队列和定时器红黑树。
   - 找到 `use` 选中的后端模块，调它的 `actions.init`（这一步把 `ngx_event_actions` 全局表填好、`ngx_event_flags` 设好）。
   - 分配 `worker_connections` 个 `ngx_connection_t`、读事件、写事件数组，把空闲连接串成空闲池。
   - 遍历所有监听套接字，给每个 `accept` 事件设 `handler = ngx_event_accept`，按需注册到后端。

#### 4.3.3 源码精读

**(1) `events` 块指令与 `ngx_events_block`**

`events` 是一个主配置块指令，定义在 [ngx_event.c:82-92](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L82-L92)：

```c
static ngx_command_t  ngx_events_commands[] = {
    { ngx_string("events"),
      NGX_MAIN_CONF|NGX_CONF_BLOCK|NGX_CONF_NOARGS,
      ngx_events_block,
      0, 0, NULL },
    ngx_null_command
};
```

`NGX_MAIN_CONF` 表示它只能出现在配置最外层，`NGX_CONF_BLOCK` 表示它带块。块指令的 set 回调 `ngx_events_block` 负责「进入块」，见 [ngx_event.c:986-1061](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L986-L1061)。它的关键动作（回顾 u3-l1、u3-l2 的配置解析机制）：

```c
ngx_event_max_module = ngx_count_modules(cf->cycle, NGX_EVENT_MODULE);
// 为每个 EVENT 模块建配置数组
*ctx = ngx_pcalloc(cf->pool, ngx_event_max_module * sizeof(void *));
// 切换 cmd_type，限定块内只允许 NGX_EVENT_CONF 指令
cf->module_type = NGX_EVENT_MODULE;
cf->cmd_type = NGX_EVENT_CONF;
rv = ngx_conf_parse(cf, NULL);      // 递归解析块内文本
// 解析完，对每个 EVENT 模块调 init_conf（含选后端）
```

注意 `cf->cmd_type = NGX_EVENT_CONF` 这一行——它就是 u3-l1 讲过的「块身份标签」。`worker_connections`、`use` 等指令都带 `NGX_EVENT_CONF` 标志，只有 `cmd_type` 匹配时才能被分发，从而保证这些指令只能写在 `events {}` 里。

**(2) `use` 指令：手动选后端**

`use` 指令的处理器 `ngx_event_use` 在 [ngx_event.c:1090-1150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1090-L1150)，核心逻辑是遍历所有 EVENT 模块，按名字匹配：

```c
for (m = 0; cf->cycle->modules[m]; m++) {
    if (cf->cycle->modules[m]->type != NGX_EVENT_MODULE) continue;
    module = cf->cycle->modules[m]->ctx;
    if (module->name->len == value[1].len
        && ngx_strcmp(module->name->data, value[1].data) == 0)
    {
        ecf->use = cf->cycle->modules[m]->ctx_index;  // 记下选中的模块的类内编号
        ecf->name = module->name->data;
        return NGX_CONF_OK;
    }
}
```

注意它存的是 `ctx_index`（类内编号）而不是 `index`（全局编号）。这呼应 u3-l3：`ctx_index` 用来在 EVENT 这一类模块的配置数组里定位。后续 `ngx_event_process_init` 就是靠 `ecf->use` 找到「该调哪个后端的 init」。

**(3) `ngx_event_core_init_conf`：自动选后端**

如果你不在配置里写 `use`，nginx 也要能挑一个后端。这是 `ngx_event_core_init_conf` 的职责，见 [ngx_event.c:1288-1373](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1288-L1373)。它的挑选顺序（编译期宏控制）：

```c
#if (NGX_HAVE_EPOLL)
    fd = epoll_create(100);          // 实际探测内核是否支持 epoll
    if (fd != -1) { (void) close(fd); module = &ngx_epoll_module; }
    else if (ngx_errno != NGX_ENOSYS) { module = &ngx_epoll_module; }
#endif
#if (NGX_HAVE_KQUEUE)
    module = &ngx_kqueue_module;     // kqueue 优先级高于 epoll（同编时）
#endif
#if (NGX_HAVE_SELECT)
    if (module == NULL) module = &ngx_select_module;  // 最后兜底
#endif
```

挑出 `module` 后，用 `ngx_conf_init_uint_value` 把它写进 `ecf->use`，并给 `connections`、`multi_accept`、`accept_mutex` 等补默认值（如 `accept_mutex_delay` 默认 500ms）。`ngx_conf_init_*` 宏的逻辑回顾 u3-l2：仅当字段仍是 `NGX_CONF_UNSET*` 哨兵时才赋默认值，所以用户在配置里显式写过的值不会被覆盖。

`(NGX_HAVE_EPOLL)` 这类宏由 `auto/configure` 在编译期探测生成（回顾 u1-l2），所以「能不能选 epoll」在编译时就定了；运行时的 `epoll_create(100)` 只是再做一次内核能力探测，应对「编译时支持但运行时内核被阉割」的边缘情况。

**(4) `ngx_event_init_conf`：跨模块硬校验**

这是 `ngx_events_module`（一个 CORE 模块）的 `init_conf`，在 [ngx_event.c:432-488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L432-L488)，做两件硬性检查：

```c
if (ngx_get_conf(cycle->conf_ctx, ngx_events_module) == NULL) {
    ngx_log_error(NGX_LOG_EMERG, ...,
                  "no \"events\" section in configuration");
    return NGX_CONF_ERROR;          // 没写 events {} 直接报错
}
if (cycle->connection_n < cycle->listening.nelts + 1) {
    // worker_connections 至少要 = 监听套接字数 + 1（留给 channel）
    return NGX_CONF_ERROR;
}
```

它和 `ngx_event_core_init_conf` 的区别要记牢：`core_init_conf` 在 `events {}` 块**内部**解析末尾跑，补默认值/选后端；`ngx_event_init_conf` 在整个 cycle **顶层**初始化末尾跑，做「events 块存不存在」「连接数够不够」这种跨层校验。`nginx -t` 报 `no "events" section` 就是这里抛的。

**(5) `ngx_event_process_init`：worker 里真正跑起来**

这是最重的一个函数，在 [ngx_event.c:635-948](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L635-L948)，是 `ngx_event_core_module` 的 `init_process` 回调（[ngx_event.c:185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L185)），即每个 worker 启动时执行一次。它的几段关键代码：

决定是否启用 accept 互斥（[ngx_event.c:649-656](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L649-L656)）：

```c
if (ccf->master && ccf->worker_processes > 1 && ecf->accept_mutex) {
    ngx_use_accept_mutex = 1;
    ...
} else {
    ngx_use_accept_mutex = 0;
}
```

调选中后端的 `init`（[ngx_event.c:679-696](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L679-L696)）——这就是 4.2 节说的「把 `ngx_event_actions` 全局表填好」的触发点：

```c
for (m = 0; cycle->modules[m]; m++) {
    if (cycle->modules[m]->type != NGX_EVENT_MODULE) continue;
    if (cycle->modules[m]->ctx_index != ecf->use) continue;   // 只找 use 选中的那个
    module = cycle->modules[m]->ctx;
    if (module->actions.init(cycle, ngx_timer_resolution) != NGX_OK) {
        exit(2);                                               // 后端 init 失败，worker 直接退
    }
    break;
}
```

预分配连接与事件数组（[ngx_event.c:754-800](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L754-L800)）：

```c
cycle->connections  = ngx_alloc(sizeof(ngx_connection_t) * cycle->connection_n, ...);
cycle->read_events  = ngx_alloc(sizeof(ngx_event_t) * cycle->connection_n, ...);
cycle->write_events = ngx_alloc(sizeof(ngx_event_t) * cycle->connection_n, ...);
// 把空闲 connection 串成链，read/write 事件各就各位
cycle->free_connections = next;
cycle->free_connection_n = cycle->connection_n;
```

这就是 4.1.1 节说的「worker 启动时一次性预分配 `worker_connections` 个事件」的来源。`worker_connections` 越大，能同时处理的连接越多，但内存占用也线性增长（每个连接一个 `ngx_connection_t` + 两个 `ngx_event_t`）。

把监听套接字挂上 accept 事件（[ngx_event.c:804-945](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L804-L945)）：

```c
ls = cycle->listening.elts;
for (i = 0; i < cycle->listening.nelts; i++) {
    c = ngx_get_connection(ls[i].fd, cycle->log);
    rev = c->read;
    rev->accept = 1;
    if (c->type == SOCK_STREAM) {
        rev->handler = ngx_event_accept;     // TCP 监听：handler 是 accept
    } else {
        rev->handler = ngx_event_recvmsg;    // UDP 监听：handler 是 recvmsg
    }
    ...
    if (ngx_use_accept_mutex) continue;      // 用互斥锁时这里不注册，等拿到锁再注册
    ngx_add_event(rev, NGX_READ_EVENT, 0);   // 注册到后端
}
```

注意最后两行：如果启用了 accept 互斥（多 worker 抢一把锁来 accept，避免惊群），监听事件在 worker 启动时**不会**立即注册，而是等 worker 在主循环里拿到 `ngx_accept_mutex` 后才注册。这是 u5-l5 主循环讲义的内容，这里先留个印象。

#### 4.3.4 代码实践

**实践目标**：追踪「`use epoll;` 这行配置文本」如何最终导致 worker 调用 `ngx_epoll_init`。把 4.3 节的流程在源码里走一遍。

**操作步骤**：

1. 在 [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf) 的 `events {}` 块里临时加一行 `use epoll;`（默认配置没有这行，靠自动选择）。
2. 在 [ngx_event.c:1090-1150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1090-L1150) 的 `ngx_event_use` 里确认：匹配到 `ngx_epoll_module` 后，`ecf->use` 被赋值为 epoll 模块的 `ctx_index`。
3. 跳到 [ngx_event.c:679-696](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L679-L696)，看 `ngx_event_process_init` 如何用 `ecf->use` 找到 epoll 模块并调用 `module->actions.init`。
4. 在 [ngx_epoll_module.c:322-379](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L322-L379) 的 `ngx_epoll_init` 里确认两件事：`epoll_create` 被调用、`ngx_event_actions = ngx_epoll_module_ctx.actions` 被执行。
5. 删掉 `use epoll;` 那行，改看 [ngx_event.c:1288-1373](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1288-L1373) 的自动选择路径，确认在 Linux 上同样会选中 `ngx_epoll_module`。

**需要观察的现象**：

- 一条 `use epoll;` 经过「文本 → `ngx_event_use` 设 `ecf->use` → `ngx_event_process_init` 读 `ecf->use` 调 `actions.init` → `ngx_epoll_init` 填全局表」四级传递，最终落到一次 `epoll_create` 系统调用。
- 自动选择路径与手动 `use` 路径最终汇合到同一个 `ecf->use` 字段——选后端只是填这个字段，真正使用它在 `process_init`。

**预期结果**：你能画出从配置文本到 `epoll_create` 的完整调用链，并说清「选后端」与「初始化后端」是分开在两个函数（`core_init_conf` 与 `process_init`）里完成的。

**待本地验证**：在 Linux 上分别用「不写 `use`」和「写 `use poll;`」启动 nginx，看 `error.log` 里 `using the "epoll" event method` 与 `using the "poll" event method` 的差异——这条日志由 [ngx_event.c:505-508](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L505-L508) 的 `ngx_event_module_init` 打印，`nginx -t` 时也会出现。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_event_init_conf` 和 `ngx_event_core_init_conf` 名字只差一个 `core`，它们到底有什么区别？  
**答案**：所属模块不同、调用层级不同、职责不同。`ngx_event_init_conf` 属于 `ngx_events_module`（一个 `NGX_CORE_MODULE`），是整个 cycle 顶层初始化末尾的跨模块校验（events 块存不存在、连接数够不够）。`ngx_event_core_init_conf` 属于 `ngx_event_core_module`（一个 `NGX_EVENT_MODULE`），是 `events {}` 块内部解析完后的「类内 init_conf」，负责补默认值和挑后端。前者管「全局有没有事件配置」，后者管「事件配置具体长什么样」。

**练习 2**：为什么选后端（设 `ecf->use`）和初始化后端（调 `actions.init`）要分在两个不同阶段，而不是在选中的瞬间立刻 init？  
**答案**：因为「选」发生在配置解析阶段（master 还没 fork worker），而 epoll_create 创建的 fd、`ngx_event_actions` 全局表、预分配的事件数组都是**每个 worker 私有**的运行时状态——每个 worker 需要自己的 epoll fd 和自己的事件数组。如果在 master 选后端时就 init，fork 出来的 worker 会共享同一个 epoll fd，无法独立调度。所以 nginx 把「选哪个后端」（配置层，master 做）和「把这个后端跑起来」（运行时层，每个 worker 各做一次）严格分开，这正对应 u3-l3 讲的配置层回调与运行时层回调之分。

**练习 3**：如果 `worker_connections` 设得比监听套接字数还小，会发生什么？  
**答案**：`ngx_event_init_conf` 里的 `cycle->connection_n < cycle->listening.nelts + 1` 判定为真，报 `worker_connections are not enough for N listening sockets` 并返回 `NGX_CONF_ERROR`，`nginx -t` 直接失败。加 1 是因为还要留一个连接给 master/worker 之间的 channel 通信。

---

## 5. 综合实践

把本讲三节的知识串起来，做一次「事件机制启动链路」的完整追踪。

**任务**：用默认 [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf)（`events { worker_connections 1024; }`）为对象，回答下面一串问题，每个问题都给出源码行号依据：

1. **配置解析**：`events` 块由哪个指令描述符定义？进入块时 `ngx_events_block` 做了哪三件关键事？（对应 [ngx_event.c:82-92](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L82-L92) 与 [ngx_event.c:986-1061](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L986-L1061)）
2. **选后端**：配置里没写 `use`，nginx 在 Linux 上靠哪个函数、用哪种探测选中了 epoll？`ecf->use` 存的是什么值？（对应 [ngx_event.c:1288-1373](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L1288-L1373)）
3. **校验**：`ngx_event_init_conf` 对 `worker_connections 1024` 做了什么硬性检查？（对应 [ngx_event.c:447-460](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L447-L460)）
4. **运行时初始化**：worker 启动后，`ngx_event_process_init` 如何用第 2 步的 `ecf->use` 找到 epoll 模块并调它的 `init`？（对应 [ngx_event.c:679-696](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L679-L696)）
5. **接口挂接**：epoll 的 `init` 内部哪一行把全局 `ngx_event_actions` 表填成了 epoll 的实现？同时设了哪些 `ngx_event_flags`？（对应 [ngx_epoll_module.c:369-377](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L369-L377)）
6. **数据结构**：worker 预分配了多少个 `ngx_event_t`？它们和 `ngx_connection_t` 是什么关系？（对应 [ngx_event.c:754-800](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L754-L800)）
7. **事件字段**：监听套接字的读事件被设了哪两个关键标志/字段（提示：`accept` 和 `handler`）？为什么此时如果启用了 accept 互斥就不立即 `ngx_add_event`？（对应 [ngx_event.c:825-941](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L825-L941)）

**交付物**：一张从「`events { worker_connections 1024; }` 文本」到「worker 拿着 epoll fd 准备 accept」的时序图或编号步骤清单，每步标注源码位置。

**预期结果**：完成这个综合实践后，你应该能用自己的话讲清：「nginx 的事件机制不是一上来就 `epoll_create`，而是先在配置层选好后端、记下编号，等每个 worker 启动时才按编号把后端真正跑起来、顺便把事件结构体预分配好」。这正是本讲三节内容的合力。

## 6. 本讲小结

- `ngx_event_t` 是 nginx 调度的原子单位，靠 `data` 指归属、靠一串 1 位标志位表状态、靠 `handler` 记回调；`timer` 与 `queue` 是嵌入式容器节点，事件无需额外分配即可挂进定时器树和 posted 队列。
- 四个关键状态位的置位场景：`active` 由后端 add/del 维护（注册到内核与否）；`ready` 由后端 `process_events` 置 1、由 I/O 包装（如 `ngx_unix_recv`）置 0；`eof` 在 `recv` 返回 0 时置 1；`error` 在 I/O 返回 -1 且非 EAGAIN 时置 1。后两者不在事件分发时置，而在真正做 I/O 时置。
- `ngx_event_actions_t` 是一张 10 槽的函数指针表，规定了事件后端必须实现的 add/del/process_events/init 等操作；上层用 `ngx_add_event` 等宏间接调用，宏展开成 `ngx_event_actions.add(...)`。每个后端模块各自填这张表，启动时把选中后端的表赋给全局 `ngx_event_actions`。
- `ngx_event_flags` 是与 `actions` 表正交的「能力声明」（边缘/水平触发、是否贪婪读、是否支持低水位等），上层（如 `ngx_handle_read_event`）据它走不同分支。
- `events {}` 块的初始化分三层：`ngx_events_block` 进块解析并给 EVENT 模块分配 `ctx_index`；`ngx_event_core_init_conf` 补默认值并选后端（存 `ecf->use`）；`ngx_event_init_conf` 做跨层硬校验。
- 真正把事件机制跑起来是 worker 启动时的 `ngx_event_process_init`：调后端 `init` 填全局表、预分配连接与事件数组、把监听 fd 挂上 `ngx_event_accept`。「选后端」与「初始化后端」刻意分在配置层和运行时层，因为每个 worker 需要独立的 epoll fd 和事件数组。

## 7. 下一步学习建议

本讲只搭了事件机制的「骨架」，接下来按依赖顺序深入：

- **u5-l2 epoll 事件后端模块**：进到 `ngx_epoll_module.c` 内部，看 `ngx_epoll_init`/`ngx_epoll_add_event`/`ngx_epoll_process_events` 的逐行实现，重点理解边缘触发下的读写处理与 `instance` 位防陈旧事件的 trick。本讲 4.1.3、4.2.3 多次引用了它，现在可以去读全文。
- **u5-l3 接受连接与 connection 管理**：本讲多次提到 `ngx_event_accept` 和 `ngx_get_connection`，下一讲讲清「accept 到分配 connection、绑定读写事件 handler」的全过程，以及连接池的复用机制。
- **u5-l4 定时器与 posted 事件**：本讲提到 `ngx_event_t` 内嵌的 `timer` 红黑树节点和 `queue` posted 队列节点，下一讲讲清它们如何被 `ngx_event_add_timer` / `ngx_post_event` 使用，以及 posted 队列为何要分 accept 与普通两级。
- **u5-l5 事件主循环 process_events_and_timers**：本讲 4.3.3 提到的 accept 互斥、posted 队列处理顺序，下一讲在 `ngx_process_events_and_timers` 里串成完整的「worker 一轮循环」。

建议在读 u5-l2 之前，先回到本讲的 4.1.4 与 4.3.4 两个实践，确认自己能独立找到 `ev->active`、`ev->ready` 的赋值点和 `ecf->use` 的传递链——这些是后续讲义反复用到的基础定位能力。
