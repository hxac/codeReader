# epoll 事件后端模块

## 1. 本讲目标

本讲是「事件驱动核心」单元的第二篇，在 u5-l1 建立的事件骨架（`ngx_event_t`、`ngx_event_actions_t` 接口、`ngx_event_flags` 能力声明）之上，钻进 Linux 下默认且最重要的事件后端——epoll。

读完本讲你应当能够：

- 说清 epoll 模块如何把自己「安装」成 nginx 的事件调度入口（actions 接口对接）。
- 跟读 `ngx_epoll_init`、`ngx_epoll_add_event`、`ngx_epoll_process_events` 三个核心函数，理解 `epoll_create`/`epoll_ctl`/`epoll_wait` 三个系统调用在 nginx 里的封装位置。
- 理解边缘触发（ET）下「读到 EAGAIN」的契约，以及 nginx 用 `instance` 位检测过期事件的技巧。
- 对比 epoll 与 poll 两个后端在 `process_events` 上的实现差异，讲清为什么 epoll 在大量连接时更高效。

## 2. 前置知识

在进入源码前，先建立三点直觉。

**第一，什么是 epoll。** epoll 是 Linux 提供的「I/O 多路复用」机制，和 `select`/`poll` 同类，但更适合海量连接。它把「关注哪些 fd」和「哪些 fd 就绪」分开维护：你用 `epoll_ctl` 提前把感兴趣的 fd 登记进内核的一棵红黑树，之后每次 `epoll_wait` 内核只把**真正就绪**的 fd 拷回给你，而不是像 `poll` 那样每次都把全部 fd 在用户态与内核态之间来回拷、再线性扫一遍。

**第二，边缘触发（Edge Triggered, ET）与水平触发（Level Triggered, LT）。**

- LT（水平触发）：只要 fd 上还有数据可读，每次 `epoll_wait` 都会再次通知你。
- ET（边缘触发）：只在状态**变化**的那一刻通知一次（比如从「无数据」变为「有数据」）。如果这次没读完，下一次 `epoll_wait` 不会再通知，数据会「卡住」直到下一次新数据到来。

因此 ET 模式要求 handler 在收到通知后必须「贪婪」地把数据一直读到返回 `EAGAIN`（即「暂时没数据了」）为止。nginx 在 Linux 上默认用 ET，换取更少的事件通知次数。

**第三，回顾 u5-l1 的两个抽象。** 上层不直接调 `epoll_ctl`，而是通过宏 `ngx_add_event`（展开为 `ngx_event_actions.add`）间接调用；后端的能力用 `ngx_event_flags` 的一组位（如 `NGX_USE_CLEAR_EVENT` 表示 ET）声明给上层。本讲就是看 epoll 如何填好这张 actions 表、如何声明这些能力位。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/event/modules/ngx_epoll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c) | epoll 后端的全部实现，本讲主线 |
| [src/event/modules/ngx_poll_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c) | poll 后端，综合实践用作对比参照 |
| [src/event/ngx_event.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h) | `ngx_event_actions_t` 接口、`ngx_event_flags` 能力位、`instance` 字段定义 |
| [src/event/ngx_event.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c) | `ngx_event_process_init` 在此把监听 fd 挂上 epoll |
| [src/core/ngx_connection.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c) | `ngx_get_connection` 在此翻转 `instance` 位 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：模块注册与 actions 接口对接、`ngx_epoll_init` 初始化、事件表示与 `data.ptr` 编码、`ngx_epoll_add_event`/`add_connection` 注册、`ngx_epoll_process_events` 主循环。

### 4.1 模块注册与 actions 接口对接

#### 4.1.1 概念说明

epoll 在 nginx 里是一个「事件模块」（模块类型 `NGX_EVENT_MODULE`）。和所有事件模块一样，它通过一个 `ngx_event_module_t` 上下文把自己「能做的事」登记出来，其中最关键的是一张名为 `actions` 的函数指针表。这张表就是 u5-l1 讲过的 `ngx_event_actions_t`——一个 10 槽的接口，包含 `add`/`del`/`enable`/`disable`/`add_conn`/`del_conn`/`notify`/`process_events`/`init`/`done`。

epoll 模块要做的，就是把自己的函数填进这张表；运行时上层通过宏 `ngx_add_event`、`ngx_process_events` 间接调用它们，从而与具体后端解耦。

#### 4.1.2 核心流程

1. 定义模块上下文 `ngx_epoll_module_ctx`，把 epoll 各函数填入 `actions`。
2. 在 `ngx_epoll_init` 末尾执行 `ngx_event_actions = ngx_epoll_module_ctx.actions;`，把 epoll 的实现「安装」成全局调度入口。
3. 上层此后调 `ngx_add_event(ev, ...)` 等宏，实际进入 epoll 的 `ngx_epoll_add_event`。

#### 4.1.3 源码精读

epoll 模块上下文与 actions 表见 [src/event/modules/ngx_epoll_module.c:179-200](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L179-L200)：注意 `add` 与 `enable` 都指向 `ngx_epoll_add_event`、`del` 与 `disable` 都指向 `ngx_epoll_del_event`，因为 epoll 里「新增」与「启用」在实现上等价（都是一次 `epoll_ctl`）。

```c
static ngx_event_module_t  ngx_epoll_module_ctx = {
    &epoll_name,
    ngx_epoll_create_conf,
    ngx_epoll_init_conf,
    {
        ngx_epoll_add_event,        /* add an event */
        ngx_epoll_del_event,        /* delete an event */
        ngx_epoll_add_event,        /* enable an event */
        ngx_epoll_del_event,        /* disable an event */
        ngx_epoll_add_connection,   /* add an connection */
        ngx_epoll_del_connection,   /* delete an connection */
        ngx_epoll_notify,           /* trigger a notify */
        ngx_epoll_process_events,   /* process the events */
        ngx_epoll_init,             /* init the events */
        ngx_epoll_done,             /* done the events */
    }
};
```

「安装」动作发生在 init 末尾，见 [src/event/modules/ngx_epoll_module.c:369-377](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L369-L377)：

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

这两行是整讲的「接线点」：`ngx_event_actions` 决定「调用谁」，`ngx_event_flags` 决定「这个后端有什么能力」。上层宏定义在 [src/event/ngx_event.h:400-408](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L400-L408)：

```c
#define ngx_process_events   ngx_event_actions.process_events
#define ngx_add_event        ngx_event_actions.add
#define ngx_add_conn         ngx_event_actions.add_conn
...
```

于是 `ngx_add_event(rev, NGX_READ_EVENT, 0)` 在 epoll 后端就变成 `ngx_epoll_add_event(rev, NGX_READ_EVENT, 0)`。

#### 4.1.4 代码实践

1. 实践目标：确认 epoll 是如何被「选中」并装上 actions 表的。
2. 操作步骤：在源码里跳到 `ngx_event.h` 中 `ngx_event_actions` 的 extern 声明（[src/event/ngx_event.h:186](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L186) 附近），再全局搜索 `ngx_event_actions =` 的赋值点，统计有几个事件后端模块会做这个赋值。
3. 观察现象：除了 epoll，`ngx_poll_module.c`、`ngx_select_module.c`、`ngx_kqueue_module.c` 等都会在各自 init 里赋值 `ngx_event_actions`。
4. 预期结果：理解「同一时刻只有一个后端的 actions 生效」，由 `events {}` 块里的 `use` 指令（u5-l1 讲过 `ecf->use`）决定哪个后端的 init 被调用。
5. 待本地验证：在不同 `use` 配置下用 `--with-debug` 编译后抓 `error_log debug events`，确认日志里出现的是对应后端的名字。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `add` 和 `enable` 可以指向同一个函数？

**参考答案**：epoll 用 `epoll_ctl(EPOLL_CTL_ADD)` 新增、`epoll_ctl(EPOLL_CTL_MOD)` 修改。对一个尚未注册的 fd，「启用」等价于「新增」；对一个已注册的 fd，nginx 在 `ngx_epoll_add_event` 内部用 `e->active` 判断该用 MOD 还是 ADD（见 4.4）。所以「启用」复用 `add_event` 的实现即可，不需要单独函数。

**练习 2**：如果把 `ngx_event_flags` 里的 `NGX_USE_GREEDY_EVENT` 去掉，epoll 还能正常工作吗？

**参考答案**：不能正常工作于 ET 模式。`NGX_USE_GREEDY_EVENT` 告诉上层「读到 EAGAIN 为止」，正是 ET 的硬性要求。去掉后上层读循环可能读一次就停，剩余数据在 ET 下不会再被通知，造成连接「卡死」。

### 4.2 初始化 ngx_epoll_init：创建 epoll fd 与声明能力

#### 4.2.1 概念说明

`ngx_epoll_init` 是 actions 表里的 `init` 槽，由每个 worker 在启动时（`ngx_event_process_init`）各调一次。它负责三件事：创建本 worker 私有的 epoll fd、分配就绪事件数组 `event_list`、安装 I/O 接口表与能力位。注意是「每个 worker 一份」——因为 epoll fd、`event_list` 都是进程私有的，worker 之间不共享。

#### 4.2.2 核心流程

1. 若全局 `ep == -1`（首次 init）：`epoll_create` 建 epoll fd；可选地初始化 eventfd（notify）、file AIO、探测 `EPOLLRDHUP`。
2. 按 `epcf->events`（`epoll_events` 指令，默认 512）分配/重分配 `event_list`。
3. `ngx_io = ngx_os_io;` 装上 OS 抽象层的读写函数表（u4-l4）。
4. 赋值 `ngx_event_actions` 与 `ngx_event_flags`，声明 ET + GREEDY + EPOLL。

#### 4.2.3 源码精读

`ngx_epoll_init` 主体见 [src/event/modules/ngx_epoll_module.c:322-380](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L322-L380)。关键片段：

```c
if (ep == -1) {
    ep = epoll_create(cycle->connection_n / 2);
    if (ep == -1) { /* 报错返回 */ }
    /* 可选：notify / file AIO / test EPOLLRDHUP */
}

if (nevents < epcf->events) {
    if (event_list) { ngx_free(event_list); }
    event_list = ngx_alloc(sizeof(struct epoll_event) * epcf->events,
                           cycle->log);
}

nevents = epcf->events;
ngx_io = ngx_os_io;
ngx_event_actions = ngx_epoll_module_ctx.actions;
ngx_event_flags = NGX_USE_CLEAR_EVENT | NGX_USE_GREEDY_EVENT | NGX_USE_EPOLL_EVENT;
```

几个要点：

- `epoll_create(cycle->connection_n / 2)` 的 size 参数在新内核里已忽略（只要 >0），传 `connection_n/2` 只是历史习惯。
- `event_list` 是 `struct epoll_event` 数组，`epoll_wait` 把就绪事件填进来；容量由 `epoll_events` 指令控制，默认 512（见 [src/event/modules/ngx_epoll_module.c:1047](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L1047) 的 `ngx_conf_init_uint_value(epcf->events, 512)`）。
- 三个静态全局 `ep`/`event_list`/`nevents` 定义在 [第 133-135 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L133-L135)，是本 worker 的 epoll 全部状态。
- 能力位含义见 [src/event/ngx_event.h:196-251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L196-L251)：`NGX_USE_CLEAR_EVENT`（边缘触发）、`NGX_USE_GREEDY_EVENT`（需读到 EAGAIN）、`NGX_USE_EPOLL_EVENT`（是 epoll）。若编译期未探测到 `NGX_HAVE_CLEAR_EVENT`（极旧内核），退化为 `NGX_USE_LEVEL_EVENT`（水平触发）。

#### 4.2.4 代码实践

1. 实践目标：验证「每个 worker 有自己的 epoll fd」。
2. 操作步骤：用 `--with-debug` 编译 nginx，配置 `worker_processes 2;` 与 `error_log logs/error.log debug_events;`，启动后 `kill -USR1` 重开日志，再 `cat logs/error.log | grep "epoll"`。
3. 观察现象：日志里两条 init 路径分别属于两个 worker，各自的 `epoll_create` 产生不同的 fd 编号。
4. 预期结果：确认 epoll fd 是进程私有、不跨 worker 共享；这也是后面 accept 互斥/惊群问题存在的根源。
5. 待本地验证：fd 编号与 worker 数量的对应关系。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_io = ngx_os_io;` 要放在事件后端的 init 里，而不是更早？

**参考答案**：`ngx_os_io` 是 OS 抽象层提供的 I/O 函数表（`recv`/`send`/`recv_chain`/`send_chain` 等，u4-l4 讲过）。把它放在事件后端 init 里，是为了确保「当前生效的事件后端」与「当前生效的 I/O 路径」在同一时刻被一致地安装——选了 epoll 就同时装上对应的 socket 读写实现，避免错配。

**练习 2**：`epoll_events` 指令把 `event_list` 调大（比如 4096）会带来什么影响？

**参考答案**：`event_list` 是 `epoll_wait` 的输出缓冲，调大允许一次 `epoll_wait` 取回更多就绪事件，高并发突发场景下减少 wait 次数；代价是常驻内存增大（每个 `struct epoll_event` 约 12 字节）。

### 4.3 事件表示：struct epoll_event、event_list 与 data.ptr 编码

#### 4.3.1 概念说明

这里要纠正一个容易产生的误解：**nginx 并没有定义 `ngx_epoll_event_t` 这个类型**。它直接复用内核的 `struct epoll_event`：

```c
struct epoll_event {
    uint32_t      events;   // 事件位掩码
    epoll_data_t  data;     // union: ptr / fd / u32 / u64
};
```

所以 epoll 模块里的「事件表示」就是 `struct epoll_event` 本身，外加 nginx 在 `data.ptr` 上做的一个精巧编码——把连接指针和一位 `instance` 标志打包进同一个指针。理解这个编码是看懂 `process_events` 里「过期事件检测」的前提。

#### 4.3.2 核心流程

1. 注册事件时：`ee.data.ptr = (连接指针 c) | (instance 位)`，把 instance 存进指针最低位。
2. 内核就绪后：`epoll_wait` 把 `struct epoll_event`（含原样回传的 `data.ptr`）填进 `event_list`。
3. 处理事件时：从 `data.ptr` 拆出 `instance` 与连接指针 `c`，用 `rev->instance != instance` 判断是否过期。

#### 4.3.3 源码精读

打包发生在 `ngx_epoll_add_event`，见 [src/event/modules/ngx_epoll_module.c:620-621](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L620-L621)：

```c
ee.events = events | (uint32_t) flags;
ee.data.ptr = (void *) ((uintptr_t) c | ev->instance);
```

为什么指针最低位能挪用？因为 `ngx_connection_t` 由分配器分配，至少 2 字节（实际 8/16 字节）对齐，最低位恒为 0，可用来存 1 位信息。`instance` 是 `ngx_event_t` 的 1 位字段，定义在 [src/event/ngx_event.h:37-38](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L37-L38)：

```c
/* used to detect the stale events in kqueue and epoll */
unsigned         instance:1;
```

`instance` 位在连接被复用时翻转，见 [src/core/ngx_connection.c:1252-1258](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_connection.c#L1252-L1258) 的 `ngx_get_connection`：

```c
instance = rev->instance;
ngx_memzero(rev, sizeof(ngx_event_t));
ngx_memzero(wev, sizeof(ngx_event_t));
rev->instance = !instance;
wev->instance = !instance;
```

拆包与过期检测发生在 `ngx_epoll_process_events`，见 [src/event/modules/ngx_epoll_module.c:837-854](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L837-L854)：

```c
c = event_list[i].data.ptr;
instance = (uintptr_t) c & 1;                  // 取最低位
c = (ngx_connection_t *) ((uintptr_t) c & (uintptr_t) ~1);  // 清掉最低位
rev = c->read;
if (c->fd == -1 || rev->instance != instance) {
    /* the stale event from a file descriptor that was just closed */
    continue;
}
```

为什么需要这套机制？因为 epoll 按 fd 注册，而 fd 是会被复用的小整数：一个连接关闭后，它的 fd 数字可能被分配给一个全新连接。但内核 epoll 队列里可能还残留旧 fd 的就绪事件（在 fd 关闭后内核会自动剔除，但「本轮 wait 已返回的事件」可能指向一个已被关闭又复用的 fd）。靠 `instance` 位的翻转，nginx 能识别出「这个事件的 instance 与当前连接的 instance 不符」，从而丢弃过期事件，避免把旧事件错派给新连接。

#### 4.3.4 代码实践

1. 实践目标：用一个最小例子体会「指针最低位编码」。
2. 操作步骤：阅读 `ngx_epoll_add_event` 与 `ngx_epoll_process_events` 中的打包/拆包两段；在纸上画出一个对齐指针 `0x55a1b2c3d4e0`，假设 `instance=1`，写出打包后的值与拆包后还原的连接指针。
3. 观察现象：打包后 `0x...e1`，拆包还原得到 `0x...e0`，instance=1。
4. 预期结果：确认「最低位不影响原指针指向的地址」，因为结构体对齐保证最低位原本就是 0。
5. 待本地验证：可写一段独立 C 代码（示例代码，非项目原有）：

```c
/* 示例代码：演示 data.ptr 最低位编码，非 nginx 原有 */
ngx_connection_t *c = get_connection();   /* 对齐指针，最低位为 0 */
unsigned instance = 1;
void *ptr = (void *)((uintptr_t)c | instance);

unsigned got_instance = (uintptr_t)ptr & 1;
ngx_connection_t *got_c = (ngx_connection_t *)((uintptr_t)ptr & (uintptr_t)~1);
/* 期望：got_instance == 1，got_c == c */
```

#### 4.3.5 小练习与答案

**练习 1**：如果 `ngx_connection_t` 的分配只保证 1 字节对齐（最低位可能为 1），这套编码会出什么问题？

**参考答案**：最低位可能本就是 1，打包时 `| instance` 无法区分「指针原低位 1」与「instance=1」，拆包时 `& ~1` 会把指针最低位强行清 0，得到一个错误的、偏移 1 字节的连接指针，导致崩溃。所以这套技巧依赖「结构体至少 2 字节对齐」，nginx 的分配器天然满足。

**练习 2**：`instance` 位为什么要在 `ngx_get_connection` 里翻转，而不是在 `ngx_free_connection` 里？

**参考答案**：连接被取出复用时才需要让「新一次注册」带上的 instance 与「上一次注册」不同，这样旧事件才会被判为过期。`ngx_get_connection` 正是「把空闲连接交给新 fd」的时刻，在此翻转 `instance` 最合适。

### 4.4 ngx_epoll_add_event / add_connection：epoll_ctl 的封装

#### 4.4.1 概念说明

`ngx_epoll_add_event` 把一个 `ngx_event_t`（读或写）登记进 epoll，底层是一次 `epoll_ctl` 系统调用。难点在于：epoll 是按 fd 注册的，而 nginx 把读、写分成两个 `ngx_event_t`。同一个 fd 上既关注读又关注写时，不能 ADD 两次（第二次会报错），必须用 MOD 把两边的事件位合并。`ngx_epoll_add_connection` 则是一个快捷方式：一次 `epoll_ctl(ADD)` 同时把读写都挂上，供 upstream 这种一开始就要双向读写的连接使用。

#### 4.4.2 核心流程

`ngx_epoll_add_event(ev, event, flags)`：

1. `c = ev->data`（事件归属连接）；确定本事件位 `events`（读 → `EPOLLIN|EPOLLRDHUP`，写 → `EPOLLOUT`）。
2. 看对端事件（读事件的对端是 `c->write`）是否已 active：是 → `op = EPOLL_CTL_MOD` 并把对端事件位 OR 进来；否 → `op = EPOLL_CTL_ADD`。
3. `ee.events = events | flags;`（flags 可含 `NGX_CLEAR_EVENT`=EPOLLET、`NGX_EXCLUSIVE_EVENT`=EPOLLEXCLUSIVE）。
4. `ee.data.ptr = c | ev->instance;`（4.3 的编码）。
5. `epoll_ctl(ep, op, c->fd, &ee)`，成功后 `ev->active = 1`。

`ngx_epoll_del_event` 的捷径：若 `flags & NGX_CLOSE_EVENT`，说明 fd 马上要关闭，内核会自动把 fd 从 epoll 剔除，所以只置 `ev->active=0` 直接返回，省一次 syscall。

#### 4.4.3 源码精读

`ngx_epoll_add_event` 见 [src/event/modules/ngx_epoll_module.c:578-639](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L578-L639)，核心是 ADD/MOD 的选择：

```c
if (event == NGX_READ_EVENT) {
    e = c->write; prev = EPOLLOUT;
    events = EPOLLIN|EPOLLRDHUP;        /* NGX_READ_EVENT 在 epoll 下的定义 */
} else {
    e = c->read; prev = EPOLLIN|EPOLLRDHUP;
    events = EPOLLOUT;
}

if (e->active) {
    op = EPOLL_CTL_MOD;                 /* 对端已在 epoll，改为 MOD 合并 */
    events |= prev;
} else {
    op = EPOLL_CTL_ADD;                 /* 对端不在，新增 */
}

ee.events = events | (uint32_t) flags;
ee.data.ptr = (void *) ((uintptr_t) c | ev->instance);
epoll_ctl(ep, op, c->fd, &ee);
ev->active = 1;
```

`NGX_READ_EVENT`/`NGX_WRITE_EVENT` 在 epoll 下的定义见 [src/event/ngx_event.h:349-350](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.h#L349-L350)：`NGX_READ_EVENT = EPOLLIN|EPOLLRDHUP`，`NGX_WRITE_EVENT = EPOLLOUT`。读事件带上 `EPOLLRDHUP` 是为了在对端半关闭时能感知（结合 4.5 里的 `pending_eof`）。

`NGX_CLOSE_EVENT` 捷径见 [src/event/modules/ngx_epoll_module.c:651-660](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L651-L660)：

```c
if (flags & NGX_CLOSE_EVENT) {
    ev->active = 0;
    return NGX_OK;
}
```

`ngx_epoll_add_connection` 一次性注册读写，见 [src/event/modules/ngx_epoll_module.c:700-721](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L700-L721)：

```c
ee.events = EPOLLIN|EPOLLOUT|EPOLLET|EPOLLRDHUP;
ee.data.ptr = (void *) ((uintptr_t) c | c->read->instance);
epoll_ctl(ep, EPOLL_CTL_ADD, c->fd, &ee);
c->read->active = 1;
c->write->active = 1;
```

对比 poll：`ngx_poll_add_event`（[src/event/modules/ngx_poll_module.c:113-162](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c#L113-L162)）只是往 `event_list` 数组追加一个 `struct pollfd` 并记下下标 `ev->index`，**没有任何系统调用**。这是两种后端性能模型的根本差别之一：epoll 在注册期付出 `epoll_ctl` 的代价，换来 wait 期的高效；poll 注册零开销，但 wait 期要全量扫描。

#### 4.4.4 代码实践

1. 实践目标：看清「读写合并」如何省 syscall。
2. 操作步骤：跟踪一个 upstream 连接从建立到同时收发的全过程——先看 `ngx_epoll_add_connection` 一次性挂上读写（1 次 `epoll_ctl`），再对比「若用 `ngx_epoll_add_event` 分两次挂」会怎样。
3. 观察现象：用 `strace -e epoll_ctl -p <worker_pid>` 跟踪一个发请求的过程，统计 `epoll_ctl(EPOLL_CTL_ADD)` 与 `EPOLL_CTL_MOD` 的次数。
4. 预期结果：新连接首次注册是 1 次 ADD；之后若只调整某一侧事件位，出现的是 MOD 而非再次 ADD。
5. 待本地验证：实际 syscall 次数与并发模型相关，本地用 strace 确认。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ngx_epoll_add_event` 要先判断 `e->active`（对端事件是否已注册）？

**参考答案**：epoll 一个 fd 只能有一条注册项。若对端读/写事件已经注册过（`e->active` 为真），本次只能用 `EPOLL_CTL_MOD` 把新事件位合并进去；否则才用 `EPOLL_CTL_ADD` 新增。直接对已注册 fd 再 ADD 会返回 `EEXIST` 错误。

**练习 2**：`NGX_CLOSE_EVENT` 捷径省下的那一次 `epoll_ctl(DEL)`，依赖什么内核行为？

**参考答案**：依赖「fd 关闭时内核自动把它从所有 epoll 实例中移除」。所以 nginx 知道马上要 `close(fd)` 时，就不必再显式 `epoll_ctl(DEL)`，省一次 syscall。前提是确实会关闭该 fd。

### 4.5 ngx_epoll_process_events：epoll_wait 与边缘触发下的读写分发

#### 4.5.1 概念说明

这是 worker 事件循环的心脏。每个循环周期，上层（`ngx_process_events_and_timers`，u5-l5 会专讲）调一次 `ngx_process_events`，在 epoll 后端就是 `ngx_epoll_process_events`。它阻塞在 `epoll_wait` 上等就绪事件，返回后逐个分发：置 `ready` 标志、决定立即调用 handler 还是投递到 posted 队列。ET 模式的「读到 EAGAIN」契约，正是通过这里置的 `available = -1` 传给上层读循环的。

#### 4.5.2 核心流程

1. `events = epoll_wait(ep, event_list, nevents, timer);`（timer 是最近定时器到期时间，无定时器时为 `NGX_TIMER_INFINITE`）。
2. 必要时 `ngx_time_update()` 刷新缓存时间。
3. 错误处理：`EINTR` 若由定时器闹钟引起则正常返回；`events==0` 且非无限超时是正常超时。
4. `for (i = 0; i < events; i++)` 只遍历就绪项（\(O(k)\)，\(k\) 为就绪数）：
   - 拆 `data.ptr` 得连接与 instance，做过期事件检测。
   - `EPOLLERR|EPOLLHUP` 时把 `EPOLLIN|EPOLLOUT` 都置上，让 handler 去读到错误。
   - 读就绪：`rev->ready=1; rev->available=-1;`；按 `NGX_POST_EVENTS` 决定投递队列或直接调 `rev->handler`。
   - 写就绪：同理处理 `wev`。
5. 返回，控制权交回上层去处理 posted 队列与定时器。

#### 4.5.3 源码精读

`ngx_epoll_process_events` 见 [src/event/modules/ngx_epoll_module.c:783-936](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L783-L936)。关键片段：

```c
events = epoll_wait(ep, event_list, (int) nevents, timer);
err = (events == -1) ? ngx_errno : 0;

if (flags & NGX_UPDATE_TIME || ngx_event_timer_alarm) {
    ngx_time_update();              // 顺手刷新缓存时间（u2-l5）
}
...
for (i = 0; i < events; i++) {      // 只遍历就绪项
    c = event_list[i].data.ptr;
    instance = (uintptr_t) c & 1;
    c = (ngx_connection_t *) ((uintptr_t) c & (uintptr_t) ~1);
    rev = c->read;
    if (c->fd == -1 || rev->instance != instance) {
        continue;                   // 过期事件丢弃
    }

    revents = event_list[i].events;
    if (revents & (EPOLLERR|EPOLLHUP)) {
        revents |= EPOLLIN|EPOLLOUT;   // 出错也触发读写，让 handler 处理
    }

    if ((revents & EPOLLIN) && rev->active) {
        if (revents & EPOLLRDHUP) { rev->pending_eof = 1; }
        rev->ready = 1;
        rev->available = -1;        // -1：未知可读量，须读到 EAGAIN（ET 契约）
        if (flags & NGX_POST_EVENTS) {
            queue = rev->accept ? &ngx_posted_accept_events : &ngx_posted_events;
            ngx_post_event(rev, queue);
        } else {
            rev->handler(rev);
        }
    }

    wev = c->write;
    if ((revents & EPOLLOUT) && wev->active) {
        wev->ready = 1;
        if (flags & NGX_POST_EVENTS) {
            ngx_post_event(wev, &ngx_posted_events);
        } else {
            wev->handler(wev);
        }
    }
}
```

三个要点：

- **只遍历就绪项**：循环上界是 `events`（就绪数），不是 `nevents`（总容量）。这是 epoll 相对 poll 的核心优势，数学上是从 \(O(n)\)（\(n\) 为总连接数）降到 \(O(k)\)（\(k\) 为就绪数）。
- **`available = -1` 是 ET 契约**：表示「不知道还有多少可读」，上层读循环（如 `ngx_unix_recv`）会一直读到返回 `EAGAIN` 才停。这正是 `NGX_USE_GREEDY_EVENT` 能力位告诉上层要做的事。
- **`NGX_POST_EVENTS` 与 accept 队列**：当 worker 持有 accept 互斥锁时，上层会传入 `NGX_POST_EVENTS`（见 [src/event/ngx_event.c:228-229](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L228-L229)），此时不立即调 handler，而是把事件投递到队列，等锁内统一处理；其中 accept 事件进高优先级的 `ngx_posted_accept_events`，普通事件进 `ngx_posted_events`（u5-l4 专讲 posted 队列）。

**多 worker 下 epoll 与监听套接字的关系**：监听 fd 的挂载方式见 `ngx_event_process_init`，[src/event/ngx_event.c:880-942](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L880-L942)。有三种模式：

1. `ngx_use_accept_mutex` 开启时：监听 fd 平时**不挂**在 epoll 上，由拿到 accept 锁的 worker 临时挂上（`ngx_trylock_accept_mutex`），避免所有 worker 同时被唤醒争抢 accept（惊群）。
2. 支持 `EPOLLEXCLUSIVE` 且 `worker_processes > 1` 时：用 `ngx_add_event(rev, NGX_READ_EVENT, NGX_EXCLUSIVE_EVENT)` 把监听 fd 以「独占唤醒」方式挂上，内核只唤醒一个 worker（[第 921-935 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L921-L935)）。
3. 单 worker 或上述都不适用：直接 `ngx_add_event(rev, NGX_READ_EVENT, 0)` 挂上。

监听事件的 handler 被设成 `ngx_event_accept`（[第 881/895 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L881-L895)），即读就绪时由 `ngx_event_accept` 去 `accept()` 新连接（u5-l3 专讲）。

#### 4.5.4 代码实践（本讲核心实践：epoll 与 poll 的 process_events 对比）

1. 实践目标：对比 `ngx_epoll_process_events` 与 `ngx_poll_process_events` 的实现，讲清 epoll 在大量连接时为何更高效。
2. 操作步骤：
   - 读 `ngx_poll_process_events`（[src/event/modules/ngx_poll_module.c:238-401](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c#L238-L401)），重点看 `poll(event_list, nevents, timer)` 调用与随后的 `for (i = 0; i < nevents && ready; i++)` 线性扫描。
   - 读 `ngx_poll_add_event`（[第 113-162 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c#L113-L162)），看它如何往数组追加 `struct pollfd`、用 `ev->index` 记下标、删除时用末尾元素填洞（[第 198-223 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c#L198-L223)）。
   - 对照 `ngx_poll_init` 里 `ngx_event_flags = NGX_USE_LEVEL_EVENT|NGX_USE_FD_EVENT;`（[第 98 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_poll_module.c#L98)）。
3. 观察现象（填表）：

   | 维度 | poll | epoll |
   | --- | --- | --- |
   | 注册开销 | 无 syscall，往数组追加 | 一次 `epoll_ctl`，内核建红黑树节点 |
   | wait 调用 | `poll(event_list, nevents, …)` 每次全量拷贝全部 fd 进出内核 | `epoll_wait(ep, …)` 只拷贝就绪项 |
   | 就绪扫描 | `for i in 0..nevents` 全量 \(O(n)\) | `for i in 0..events` 只遍历就绪 \(O(k)\) |
   | 触发模式 | LT（`NGX_USE_LEVEL_EVENT`） | ET（`NGX_USE_CLEAR_EVENT`）+ GREEDY |
   | 连接定位 | 需 `ngx_cycle->files[fd]` 反查表（`NGX_USE_FD_EVENT`） | `data.ptr` 直接带连接指针，无需反查 |
   | 删除 | 末尾填洞保持紧凑 | `epoll_ctl(DEL)` 或关 fd 自动剔除 |

4. 预期结果：能口述「为什么 epoll 在大量连接时更高效」——核心两点：(a) wait 后只扫描就绪项 \(O(k)\) 而非全量 \(O(n)\)；(b) wait 不再每次把全部 fd 拷进拷出内核，注册信息常驻内核红黑树。
5. 待本地验证：可写一段 benchmark（示例代码，非项目原有），分别用 `poll` 与 `epoll` 监听 1 万个空闲 fd、只让 1 个就绪，对比单次 `poll()`/`epoll_wait()` 返回后遍历的耗时。

#### 4.5.5 小练习与答案

**练习 1**：`ngx_epoll_process_events` 里 `if (revents & (EPOLLERR|EPOLLHUP)) revents |= EPOLLIN|EPOLLOUT;` 这一句的意图是什么？

**参考答案**：当 fd 出错或对端挂起时，内核返回 `EPOLLERR`/`EPOLLHUP`。nginx 无法直接处理这类裸错误位，于是把它们映射成「可读 + 可写」，让既有的读/写 handler 被触发；handler 在实际 `recv`/`send` 时会拿到 0（对端关闭）或 -1（错误），从而走正常的关闭/错误处理路径。

**练习 2**：为什么写事件 `wev` 在分发前还要再做一次 `c->fd == -1 || wev->instance != instance` 检测？

**参考答案**：读事件处理（`rev->handler`）可能在本次循环里就关闭了连接并复用了连接槽，等到处理同一 `epoll_event` 的写半部分时，`wev` 可能已指向一个全新的连接。再次做 instance 比对，可防止把写事件错派给新连接，是对 stale event 检测的二次保险。

**练习 3**：`rev->available = -1` 与 `NGX_USE_GREEDY_EVENT` 是什么关系？

**参考答案**：`NGX_USE_GREEDY_EVENT` 是后端向**上层**声明的能力位——「我（epoll ET）要求你把 I/O 做到 EAGAIN」；`rev->available = -1` 是后端在**具体事件**上向上层传达的信号——「本次就绪的可读量未知，请读到 EAGAIN」。两者配合保证 ET 模式下数据不被遗漏。

## 5. 综合实践

把本讲知识串起来：跟踪一次「客户端发起 HTTP 请求、worker 用 epoll 接收」的完整事件路径。

1. 准备：`--with-debug` 编译 nginx，配置 `worker_processes 1;`（先排除 accept 互斥干扰），`error_log logs/error.log debug_events;`，`listen 8080;`。
2. 启动后发起一次 `curl http://127.0.0.1:8080/`，抓取日志。
3. 在日志与源码中对照确认以下链条：
   - worker 启动时 `ngx_epoll_init` 创建 `ep`、装上 actions 与 `NGX_USE_CLEAR_EVENT|NGX_USE_GREEDY_EVENT|NGX_USE_EPOLL_EVENT`。
   - `ngx_event_process_init` 把监听 fd 用 `ngx_add_event(rev, NGX_READ_EVENT, 0)` 挂上 epoll，handler 设为 `ngx_event_accept`。
   - 主循环调 `ngx_process_events` → `ngx_epoll_process_events` → `epoll_wait` 返回监听 fd 就绪。
   - `data.ptr` 拆包、instance 校验通过，置 `rev->ready=1`、`rev->available=-1`，调 `ngx_event_accept`。
4. 把 `worker_processes` 改成 4，重复请求，观察日志里出现 accept 互斥或 `EPOLLEXCLUSIVE` 相关的行为差异。
5. 产出：一张从 `epoll_wait` 返回到 `ngx_event_accept` 被调用的调用图，标注每一步对应的源码行号；并用一段话说明 ET 模式下为何 `ngx_event_accept` 内部会循环 `accept` 到 `EAGAIN`（提示：结合 `ev->available` 与 `NGX_USE_GREEDY_EVENT`）。

## 6. 本讲小结

- epoll 模块通过 `ngx_epoll_module_ctx.actions` 把自己的函数注册进 `ngx_event_actions`，上层用 `ngx_add_event`/`ngx_process_events` 宏间接调用，与具体后端解耦。
- `ngx_epoll_init` 为每个 worker 创建私有的 `ep` fd 与 `event_list`，并用 `ngx_event_flags` 声明 ET + GREEDY + EPOLL 三项能力。
- nginx 复用内核的 `struct epoll_event`（不存在 `ngx_epoll_event_t`），把连接指针与 1 位 `instance` 打包进 `data.ptr` 最低位，用于检测 fd 复用导致的过期事件。
- `ngx_epoll_add_event` 用 ADD/MOD 的选择把读写事件合并到同一条 epoll 注册项上；`NGX_CLOSE_EVENT` 捷径依赖「fd 关闭时内核自动剔除」省一次 syscall。
- `ngx_epoll_process_events` 只遍历就绪项 \(O(k)\)，置 `available=-1` 落实 ET「读到 EAGAIN」契约，并按 `NGX_POST_EVENTS` 决定立即调用还是投递队列。
- 相比 poll 的全量扫描 \(O(n)\) 与每次全量 fd 拷贝，epoll 把就绪集维护在内核、wait 期只取就绪项，这是它在海量连接下更高效的根本原因。

## 7. 下一步学习建议

- 下一篇 **u5-l3 接受连接与 connection 管理**：钻进 `ngx_event_accept`，看 accept 返回的新 fd 如何分配 `ngx_connection_t`、绑定读写事件并挂上 epoll，本讲提到的 `instance` 翻转与 `available` 会在那里被实际使用。
- 之后 **u5-l4 定时器与 posted 事件**：理解本讲反复出现的 `ngx_posted_accept_events`/`ngx_posted_events` 两级队列的优先级与处理时机。
- 再之后 **u5-l5 事件主循环**：把 `ngx_process_events_and_timers` 拼起来，看清 accept 互斥锁、`NGX_POST_EVENTS`、posted 队列、定时器四者的调度顺序，本讲的 `ngx_epoll_process_events` 是其中的一个环节。
- 延伸阅读：对照 `src/event/modules/ngx_kqueue_module.c`（BSD/macOS 后端），体会不同 OS 后端如何实现同一套 `ngx_event_actions_t` 接口，加深对「接口与实现分离」的理解。
