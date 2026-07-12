# 时间缓存与日志系统

## 1. 本讲目标

nginx 在高并发下每秒要处理成千上万次请求，而每一次请求处理几乎都要做两件事：**知道现在几点**（算定时器超时、写日志时间戳、生成 Expires 头）和**留下痕迹**（错误日志、访问日志）。如果每次都现调系统调用 `gettimeofday` 现格式化时间、现拼字符串写日志，这两件「不起眼」的小事就会吃掉可观的 CPU。

本讲带你读懂 nginx 解决这两件事的两个核心子系统。学完后你应能：

1. 理解 nginx 的**时间缓存机制**：为什么把时间与多种格式化字符串预先算好放进全局变量，靠 `ngx_time_update` 周期性刷新、其余代码无锁读取；理解 `ngx_current_msec` 这个事件定时器的「心跳时钟」从何而来。
2. 掌握 `ngx_log_error` 这个贯穿全代码的**变参宏**：它如何用 `log_level` 做门控、再委托 `ngx_log_error_core` 把「时间戳 + 级别 + pid#tid + 连接号 + 用户消息 + errno + 上下文」拼成一行，并沿日志链写到多个目标。
3. 理解日志的**级别阈值**与**多目标后端**：`ngx_log_set_levels` 如何把配置里的 `error_log ... info;`、`debug_http` 翻译成数值，以及同一份日志为何能同时输出到文件、stderr、syslog、内存环形缓冲。

本讲是 u2 单元的收尾：它把内存池（u2-l1）、字符串（u2-l2）、buf/chain（u2-l4）都用上——时间戳是 `ngx_str_t`、日志缓冲是栈上数组、各种格式化用 `ngx_sprintf`。同时它又是事件循环（u5）与 HTTP 处理（u6）的隐性前置：定时器靠 `ngx_current_msec`，每条错误日志都带时间戳。

## 2. 前置知识

本讲默认你已学过：

- **内存池 `ngx_pool_t`**（u2-l1）：日志子系统在配置阶段用 `ngx_pcalloc(cf->pool, ...)` 分配 `ngx_log_t`，内存日志后端用 `ngx_pnalloc` 申请环形缓冲。
- **长度前缀字符串 `ngx_str_t`**（u2-l2）：缓存的各时间字符串都是 `volatile ngx_str_t`（`len` + `data`），日志级别名表也是 `ngx_str_t` 数组；格式化用 `ngx_sprintf` / `ngx_vslprintf` / `ngx_slprintf` 这一族函数。
- **容器**（u2-l3）：日志链是手写的单链表（`ngx_log_t::next`），理解链表遍历与插入即可。
- **buf/chain**（u2-l4）：本讲引用较少，只需知道 `ngx_cpymem` 这类内存拷贝原语。

三个概念提前点出，避免初学者卡壳：

- **缓存换系统调用**：`gettimeofday` / `clock_gettime` 虽快，但在每秒十万次请求下仍是开销；nginx 选择「事件循环每轮最多刷新一次时间，缓存成全局变量，业务代码直接读变量」。代价是时间精度为「事件粒度」（通常毫秒级），这对 Web 服务器足够。
- **多 slot 无锁读**：时间更新 rare、读 frequent。若读者正在拷贝一个时间字符串时写者覆盖了它，就会读到「半新半旧」的撕裂值。nginx 的解法是预分配 64 个 slot 轮转写入，写者永远写新 slot、读者读 `ngx_cached_time` 指向的旧 slot，二者极少撞同一个 slot。
- **日志是按级别排序的链表**：一条 `error_log` 指令产生一个 `ngx_log_t` 节点，多条指令串成链、按 `log_level` 从高（verbose）到低排；写日志时从链头开始写，一旦遇到级别不够的节点就 `break`，于是「一条 INFO 消息只进 debug 和 info 两个文件，不进 notice 文件」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/core/ngx_times.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.h) | `ngx_time_t` 结构、`ngx_time()` / `ngx_timeofday()` 宏、`ngx_current_msec` 与各 `ngx_cached_*_time` 字符串的外部声明 |
| [src/core/ngx_times.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c) | `ngx_time_init` / `ngx_time_update` / `ngx_time_sigsafe_update` / `ngx_monotonic_time` 的实现，64 slot 缓存与五种时间格式的预格式化 |
| [src/core/ngx_log.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h) | 日志级别宏、`ngx_log_t` 结构、`ngx_log_error` / `ngx_log_debug` 变参宏定义、`NGX_MAX_ERROR_STR` 上限 |
| [src/core/ngx_log.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c) | `ngx_log_error_core` 格式化与多目标写入主循环、`ngx_log_init`、`ngx_log_set_levels` 级别解析、`ngx_log_set_log` 多目标分发、`ngx_log_insert` 链表排序、内存日志 writer |

辅助调用点（用于理解「时间缓存何时刷新」「日志何时初始化」的真实时机）：

| 文件 | 作用 |
| --- | --- |
| [src/core/nginx.c:226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L226) | `main()` 中调用 `ngx_time_init()` 初始化时间缓存的现场 |
| [src/event/modules/ngx_epoll_module.c:804-806](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L804-L806) | epoll 后端在 `epoll_wait` 返回后调用 `ngx_time_update()` 刷新时间 |
| [src/event/ngx_event.c:200-217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L200-L217) | worker 主循环决定是否带 `NGX_UPDATE_TIME` 标志（即「这一轮要不要更新时间」） |

## 4. 核心概念与源码讲解

### 4.1 时间缓存：ngx_time_update 与 ngx_current_msec

#### 4.1.1 概念说明

nginx 在很多地方需要「现在的时间」，而且需要**不同格式**：

- **秒级时间戳** `time_t`：算缓存过期、Expires 头。
- **毫秒单调时钟**：事件定时器比较超时（详见 u5-l4），不能墙上时钟（墙上时钟会被 ntp 调整或回拨），必须用 `CLOCK_MONOTONIC`。
- **错误日志时间** `2024/05/01 12:00:00`。
- **HTTP 响应头日期** `Mon, 01 May 2024 04:00:00 GMT`（RFC 822）。
- **访问日志时间** `01/May/2024:12:00:00 +0800`（CLF）。
- **ISO 8601 时间** `2024-05-01T12:00:00+08:00`（供 `$time_iso8601` 变量）。
- **syslog 时间** `May  1 12:00:00`（RFC 3164）。

如果每条日志、每个响应头都现场调 `localtime` + `strftime` 拼这些字符串，开销惊人。nginx 的做法是：**事件循环每轮（或每次被信号唤醒）调用一次 `ngx_time_update`，把上述全部内容预先算好写进全局变量，业务代码直接读指针/宏**。由于这些字符串在一秒内不变，缓存命中率极高。

#### 4.1.2 核心流程

```
启动期:
  main() ── ngx_time_init() ── 设置各 cached_*_time.len ── ngx_time_update()

worker 每轮事件循环:
  ngx_process_events_and_timers()
    ├─ 若未开 timer_resolution: flags = NGX_UPDATE_TIME
    └─ 事件后端 process_events(timer, flags)
         └─ epoll_wait(...) 返回后:
              if (flags & NGX_UPDATE_TIME || ngx_event_timer_alarm)
                  ngx_time_update()        ← 每轮刷新一次

ngx_time_update() 内部:
  1. ngx_trylock(ngx_time_lock)        ← 多 worker 线程/信号互斥，拿不到就返回
  2. ngx_gettimeofday(&tv)             ← 拿墙上时钟 sec/msec
  3. ngx_current_msec = monotonic(sec, msec)   ← 拿单调毫秒时钟
  4. 若当前 slot 的 sec 没变 → 只更新 msec，解锁返回（同一秒的快路径）
  5. 否则 slot = (slot+1) % 64          ← 轮转到新 slot
  6. 在新 slot 写 sec/msec/gmtoff
  7. 用 ngx_gmtime + ngx_sprintf 预格式化 5 种字符串到新 slot
  8. ngx_memory_barrier()              ← 内存屏障，保证读者看到完整数据
  9. 把 ngx_cached_time 与各 *.data 指针「发布」到新 slot
 10. 解锁
```

读者侧完全无锁：`ngx_time()` 宏直接读 `ngx_cached_time->sec`，`ngx_current_msec` 直接读全局变量。撕裂值只可能发生在「读者正在读 slot X，写者恰好在 64 轮之后又写回 slot X」——而那需要读者被抢占超过 64 秒不调度，注释里明确说明了这一点。

#### 4.1.3 源码精读

缓存的时间原子单元 `ngx_time_t`：`sec`（墙上秒）、`msec`（毫秒）、`gmtoff`（相对 UTC 的分钟偏移，用于拼 `+0800`）。

[src/core/ngx_times.h:16-20](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.h#L16-L20) — `ngx_time_t` 结构：三个字段。它会被放进 64 个 slot 的数组里轮转。

读者侧用的宏与外部变量——这是全代码读时间的入口：

[src/core/ngx_times.h:34-37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.h#L34-L37) — `ngx_cached_time` 是指向当前 slot 的 `volatile ngx_time_t *`；`ngx_time()` 宏等价于 `ngx_cached_time->sec`（拿当前秒）；`ngx_timeofday()` 拿整个结构指针。二者都是无锁读。

五种预格式化字符串与单调毫秒时钟的外部声明：

[src/core/ngx_times.h:39-49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.h#L39-L49) — `ngx_cached_err_log_time`（错误日志用）、`ngx_cached_http_time`（响应头 Date 用）、`ngx_cached_http_log_time`（访问日志 CLF 用）、`ngx_cached_http_log_iso8601`（`$time_iso8601` 用）、`ngx_cached_syslog_time`（syslog 用）都是 `volatile ngx_str_t`；`ngx_current_msec` 是事件定时器的心跳时钟。注释点明 `ngx_current_msec` 是「自某个未指定起点以来的毫秒数、截断为 `ngx_msec_t`、专用于事件定时器」。

整段机制的设计注释——理解「无锁读」安全性的关键：

[src/core/ngx_times.c:15-22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L15-L22) — 时间可被信号处理器或多线程更新，更新操作 rare 故持 `ngx_time_lock`；读操作 frequent 故无锁，从当前 slot 取值。线程只有在「被抢占时正在拷贝、且随后超过 `NGX_TIME_SLOTS` 秒没被调度」才会读到撕裂值。

64 slot 的轮转缓冲与五种字符串的静态存储：

[src/core/ngx_times.c:24-58](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L24-L58) — `NGX_TIME_SLOTS = 64`；`cached_time[64]` 是 64 份 `ngx_time_t`；五种 `cached_*_time[64][sizeof(...)]` 是 64 份预格式化字符串缓冲，每份按各自格式的固定长度开栈。`slot` 是当前写入下标，写者递增、读者不直接用。这种「固定 64 份、覆盖式轮转」是 lock-free RC（read-copy）的极简实现。

启动初始化——设长度、指向 slot 0、立刻刷一次：

[src/core/ngx_times.c:65-77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L65-L77) — `ngx_time_init`：先把五种字符串的 `len` 设为对应格式长度减 1（去尾 `\0`），令 `ngx_cached_time = &cached_time[0]`，再调 `ngx_time_update()` 首次填充。它被 `main()` 在 [src/core/nginx.c:226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L226) 调用，是时间系统就绪的标志。

核心更新函数——本子系统的「心脏」，分四段读：

[src/core/ngx_times.c:80-107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L80-L107) — **第一段（加锁 + 取时钟 + 同秒快路径）**：先 `ngx_trylock`，拿不到说明已有别处正在更新，直接返回；拿到后 `ngx_gettimeofday` 取墙上时钟，`ngx_monotonic_time` 算单调毫秒存入 `ngx_current_msec`。若当前 slot 的 `sec == sec`（同一秒内再次更新），只刷新 `msec` 就解锁返回——这是高频命中的快路径，避免每毫秒都重格式化字符串。

[src/core/ngx_times.c:109-128](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L109-L128) — **第二段（轮转 slot + 写 GMT 时间）**：`slot` 在 `[0, 63]` 间循环递增；在新 slot 写 `sec/msec`；`ngx_gmtime(sec, &gmt)` 把秒拆成 GMT 的年月日时分秒（纯算术，详见后文），再用 `ngx_sprintf` 拼 `Mon, 28 Sep 1970 06:00:00 GMT` 到 `cached_http_time[slot]`。

[src/core/ngx_times.c:130-180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L130-L180) — **第三段（算时区 + 拼本地时间的四种格式）**：用三种平台宏之一拿到 `gmtoff`（相对 UTC 的分钟偏移），据此把 GMT 转成本地时间 `tm`，然后分别拼 `cached_err_log_time`（`%4d/%02d/%02d %02d:%02d:%02d`）、`cached_http_log_time`（`01/May/2024:12:00:00 +0800`，注意末尾 `+0800` 由 `gmtoff/60` 与 `gmtoff%60` 拼出）、`cached_http_log_iso8601`（带 `T` 与 `+08:00`）、`cached_syslog_time`（`May  1 12:00:00`）。

[src/core/ngx_times.c:182-192](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L182-L192) — **第四段（内存屏障 + 发布）**：`ngx_memory_barrier()` 确保前面所有写操作对其它 CPU 可见后，才把 `ngx_cached_time` 与五个 `*.data` 指针指向新 slot。读者此刻起读到的是完整一致的新值。最后解锁。注意「先填数据、再发布指针」的顺序正是无锁正确性的关键。

单调毫秒时钟——事件定时器的真时间源：

[src/core/ngx_times.c:195-209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L195-L209) — `ngx_monotonic_time`：若平台支持 `CLOCK_MONOTONIC`（Linux 默认支持），用 `clock_gettime(CLOCK_MONOTONIC)` 取单调时钟，避免墙上时钟被 ntp/手动调整导致定时器乱序；返回 `sec*1000 + msec`。这个值赋给 `ngx_current_msec`，是红黑树定时器（u2-l3、u5-l4）比较超时的基准。

信号安全版本——为什么需要一个「阉割版」更新：

[src/core/ngx_times.c:39-46](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L39-L46) 与 [src/core/ngx_times.c:214-269](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L214-L269) — 注释说明 `localtime` / `localtime_r` **不是异步信号安全**的，不能在信号处理器里调用；幸好夏令时偏移一年只变两次，所以信号处理器里只更新 `err_log_time` 与 `syslog_time`（用预先缓存的 `cached_gmtoff`），其余格式留到主循环再更新。`ngx_time_sigsafe_update` 就是这个阉割版，仅在 `NGX_WIN32` 之外编译。

#### 4.1.4 代码实践

**实践目标**：调用 `ngx_time_update` 刷新缓存，观察 `ngx_current_msec` 与 `ngx_cached_err_log_time` 的变化，直观感受「时间是被缓存的全局变量」。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码）。它演示「更新前 → 更新 → 间隔 → 再更新」的时间变化：

```c
/* 示例代码：观察时间缓存（非项目原有代码，需在 nginx 编译环境内链接） */
#include <ngx_config.h>
#include <ngx_core.h>

void demo_time(void)
{
    ngx_time_init();                 /* 首次填充 ngx_current_msec 与各 cached 字符串 */

    ngx_msec_t m1 = ngx_current_msec;
    ngx_str_t  t1 = ngx_cached_err_log_time;   /* 值拷贝 volatile 结构 */

    /* 模拟「忙一会」——真实代码里这是处理了一轮事件 */
    /* ... */

    ngx_time_update();               /* 事件循环每轮都会调它 */

    ngx_msec_t m2 = ngx_current_msec;
    ngx_str_t  t2 = ngx_cached_err_log_time;

    /* m2 >= m1；若跨过整秒，t1 与 t2 的字符串内容不同 */
    ngx_log_error(NGX_LOG_INFO, ngx_cycle->log, 0,
                  "msec %M -> %M, time \"%V\" -> \"%V\"",
                  m1, m2, &t1, &t2);
}
```

2. 对照真实调用时机：worker 主循环在 [src/event/ngx_event.c:200-217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event.c#L200-L217) 决定 `flags = NGX_UPDATE_TIME`，epoll 后端在 [src/event/modules/ngx_epoll_module.c:804-806](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L804-L806) 于 `epoll_wait` 返回后据此调 `ngx_time_update()`。也就是说：**每处理完一轮 I/O 事件，时间就刷新一次**。

**需要观察的现象**：

- 不调 `ngx_time_update` 时，`ngx_current_msec` 与 `ngx_cached_err_log_time` 保持 `ngx_time_init` 时的初值不变。
- 调用后 `m2 >= m1`；若两次调用跨过整秒边界，`t1`、`t2` 字符串不同（如 `12:00:00` → `12:00:01`）。
- 在真实 nginx 中，由于 worker 每轮事件循环都刷新，连续两次读 `ngx_time()` 在同一轮内必然相等（同 slot），跨轮才可能变化。

**预期结果**：日志输出形如 `msec 12345 -> 12789, time "2024/05/01 12:00:00" -> "2024/05/01 12:00:01"`。本例依赖 nginx 进程全局量（`ngx_cycle` 等），**运行结果待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nginx 要分「墙上时钟」与「单调时钟」两套，而不是统一用 `gettimeofday`？

**答案**：事件定时器比较的是「距离现在还有多少毫秒」，需要单调递增的时钟。墙上时钟（`gettimeofday` / `CLOCK_REALTIME`）会被 ntp 校时或管理员手动 `date` 调整，甚至可能回拨，导致「已设置 5 秒后超时的定时器」计算出现负差或永不过期。`CLOCK_MONOTONIC` 保证单调递增、不受墙上时间调整影响，所以 `ngx_current_msec` 用 [src/core/ngx_times.c:195-209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L195-L209) 的单调时钟。而日志时间戳、Expires 头需要人类可读的真实日期，必须用墙上时钟——两者用途不同，故并存。

**练习 2**：`ngx_time_update` 里「同秒快路径」（`tp->sec == sec`）有什么意义？

**答案**：在很高的事件频率下，事件循环可能在一秒内运行成百上千轮。若每轮都重新 `ngx_gmtime` + 拼 5 种字符串，就是巨大浪费。由于这些字符串精度到秒，同一秒内只需在第一次进入新秒时格式化一次；后续同秒的更新只刷新 `msec` 字段就解锁返回（[src/core/ngx_times.c:103-107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L103-L107)）。这把格式化开销从「每轮一次」降到「每秒一次」。

---

### 4.2 日志核心：ngx_log_error 宏与 ngx_log_error_core

#### 4.2.1 概念说明

nginx 的日志调用遍布全代码，形如 `ngx_log_error(NGX_LOG_ERR, r->connection->log, err, "open() \"%s\" failed", path);`。它有两个关键设计：

1. **宏做门控、函数做格式化**：`ngx_log_error` 是一个变参宏，它先判断 `log->log_level >= level`，不达标就**整条调用被编译期/运行期短路掉**，连参数都不求值——这是 debug 日志默认编译进去却几乎零开销的秘诀。达标才调用真正的 `ngx_log_error_core` 去格式化。
2. **一条消息可能写到多个目标**：`ngx_log_t` 是一个链表节点，一条日志可能挂多个节点（文件、stderr、syslog…）。`ngx_log_error_core` 把消息格式化进一个栈上缓冲 `errstr[2048]` 一次，然后沿链表逐个写出，避免重复格式化。

#### 4.2.2 核心流程

```
ngx_log_error(level, log, err, fmt, args...)          ← 宏
  └─ if (log->log_level >= level):                     ← 门控，不达标直接不调用
       ngx_log_error_core(level, log, err, fmt, args)

ngx_log_error_core(level, log, err, fmt, args):
  ┌─ 在栈上开 errstr[2048]
  │ 1. 拷贝 ngx_cached_err_log_time           ← 时间戳前缀（来自 4.1 的缓存）
  │ 2. 拼 " [级别名] "                          ← err_levels[level]
  │ 3. 拼 "pid#tid: "                          ← 进程/线程标识
  │ 4. 若 log->connection: 拼 "*连接号 "
  │ 5. 记 msg = p；vslprintf 用户 fmt+args      ← 用户消息主体
  │ 6. 若 err!=0: 拼 " (errno: strerror)"       ← 系统错误码翻译
  │ 7. 若非 debug 且有 handler: 调 log->handler ← 模块附加上下文
  │ 8. 追加换行
  └─ while (log):                                 ← 沿日志链写
       若 log->log_level < level && !debug_connection: break  ← 链按级别降序，不够就停
       若 log->writer:  log->writer(...)          ← 自定义后端(syslog/memory)
       否则若磁盘未满: ngx_write_fd(log->file->fd, errstr, len)  ← 写文件
       log = log->next
  若 level<=WARN 且此前没写过 stderr: 再向 stderr 补一行 "nginx: [级别] msg"
```

最终一条错误日志长这样（每个字段都能在上面对上）：

```
2024/05/01 12:00:00 [error] 1234#5678: *9 open() "/html/x" failed (2: No such file or directory)
└── 时间戳 ──┘ └级别┘ └pid#tid┘ └连接┘ └──── 用户消息 ────┘ └──── errno 翻译 ────┘
```

#### 4.2.3 源码精读

九个级别宏——注意**数值越小越严重**，`debug`(8) 最大（最宽松）：

[src/core/ngx_log.h:16-24](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h#L16-L24) — `STDERR=0, EMERG=1, ALERT=2, CRIT=3, ERR=4, WARN=5, NOTICE=6, INFO=7, DEBUG=8`。门控 `log_level >= level` 的语义是：`log_level` 是「阈值」，越高越宽松（记录越多）。配置写 `error_log /path warn;` 即 `log_level=5`，则只记录 `level<=5` 的消息（emerg..warn），info/debug 被丢弃。

debug 子类位——debug 还细分到模块：

[src/core/ngx_log.h:26-42](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h#L26-L42) — debug 用**位掩码**而非数值：`DEBUG_CORE=0x10, ALLOC=0x20, MUTEX=0x40, EVENT=0x80, HTTP=0x100, MAIL=0x200, STREAM=0x400`。`NGX_LOG_DEBUG_ALL=0x7ffffff0` 是「全部 debug 位」。所以 `error_log /path debug_http;` 只开 HTTP 那一位，`debug;` 则等价于 `debug_all`（见 4.3）。

日志对象结构——日志链节点 + 两个可插拔回调：

[src/core/ngx_log.h:45-76](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h#L45-L76) — `ngx_log_s`：`log_level`（阈值）、`file`（指向 `ngx_open_file_t`，含 fd）、`connection`（连接号，写入日志的 `*N`）、`disk_full_time`（磁盘满时一秒内跳过写，避免阻塞）、`handler`（模块附加上下文回调，签名 `u_char *(*)(log, buf, len)`）、`data`（handler 上下文）、`writer` + `wdata`（自定义输出后端，签名 `void (*)(log, level, buf, len)`）、`action`（当前动作描述字符串）、`next`（日志链下一个节点）。`NGX_COMPAT_BEGIN/END` 是为二进制兼容预留的填充。

日志单行最大长度：

[src/core/ngx_log.h:79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h#L79) — `NGX_MAX_ERROR_STR = 2048`。`ngx_log_error_core` 在栈上开 `u_char errstr[2048]`，超长会被 `ngx_slprintf` 截断。

变参宏门控——debug 日志零开销的根基：

[src/core/ngx_log.h:84-96](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.h#L84-L96) — `ngx_log_error(level, log, ...)` 展开为 `if ((log)->log_level >= level) ngx_log_error_core(level, log, __VA_ARGS__)`；`ngx_log_debug(level, log, ...)` 用位与 `&` 判断 debug 位。门控放在宏里，意味着级别不够时**连格式化参数都不求值**——`ngx_log_debug3(..., expensive_call(), ...)` 在未开 debug 时连 `expensive_call()` 都不会执行。（无变参宏的编译器走 [src/core/ngx_log.c:215-226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L215-L226) 的函数版本，门控在函数内，开销略高。）

格式化与写入主循环——日志子系统的「心脏」，分两段读：

[src/core/ngx_log.c:95-156](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L95-L156) — **格式化段**：`errstr[2048]` 在栈上；先 `ngx_cpymem` 拷贝 `ngx_cached_err_log_time`（4.1 缓存的时间戳，**这就是为什么日志调用依赖时间缓存已初始化**）；拼 ` [%V] `（用 [src/core/ngx_log.c:75-85](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L75-L85) 的 `err_levels[level]` 级别名）；拼 `%P#%Tid`（pid#tid）；若有连接号拼 `*%uA`；记 `msg = p`（用户消息起点，供后面 stderr 回显用）；`ngx_vslprintf` 格式化用户 `fmt+args`；若 `err` 非零调 `ngx_log_errno` 拼 `(errno: strerror)`；若非 debug 且有 `handler` 调它追加上下文；最后换行。

[src/core/ngx_log.c:158-210](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L158-L210) — **写入段**：沿 `while(log)` 遍历日志链——若 `log->log_level < level && !debug_connection` 则 `break`（链按 `log_level` 降序排，遇到不够宽的节点就停，见 [src/core/ngx_log.c:676-707](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L676-L707) 的 `ngx_log_insert`）；若该节点有 `writer`（syslog/memory 后端）调之；否则检查 `disk_full_time`（同一秒内磁盘满则跳过一次写，注释解释 FreeBSD softupdates 下写满盘可能长时间阻塞），再 `ngx_write_fd` 写文件；若写到 `ngx_stderr` 记 `wrote_stderr=1`。循环结束后，若该消息级别 `<= WARN` 且此前没写过 stderr，再向 stderr 补一行 `nginx: [级别] msg`——这就是你在终端看到 `nginx: [emerg] ...` 的来源。

级别名表——注意它把 `0` 号槽留空：

[src/core/ngx_log.c:75-90](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L75-L90) — `err_levels[]` 第 0 项是 `ngx_null_string`（占位，因为 `STDERR=0` 不是真正的日志级别），1..8 依次是 `emerg..debug`；`debug_levels[]` 是 7 个 debug 子类名 `debug_core..debug_stream`，顺序与位掩码 `0x10..0x400` 一一对应。`ngx_log_set_levels` 与 `ngx_log_error_core` 都靠这两张表做名字↔数值映射。

#### 4.2.4 代码实践

**实践目标**（本讲规格指定的核心实践）：初始化内存池与日志，调用 `ngx_log_error(NGX_LOG_INFO, ...)` 输出一行日志到 stderr，并打印 `ngx_current_msec`。

**操作步骤**：

1. 阅读下面「示例代码」（非 nginx 原有代码）。注意三个关键顺序：先 `ngx_time_init`（否则时间戳前缀为空）、再用空字符串调 `ngx_log_init` 拿到 fd 指向 stderr 的日志、最后**手动抬高 `log_level`**——因为 `ngx_log_init` 默认 `log_level=NOTICE(6)`，而 `INFO=7`，不抬高的话 INFO 消息会被门控丢弃。

```c
/* 示例代码：最小化日志与时间演示（非项目原有代码） */
#include <ngx_config.h>
#include <ngx_core.h>

int main(void)
{
    /* 1. 初始化时间缓存：填充 ngx_current_msec 与 ngx_cached_err_log_time。
     *    ngx_log_error_core 会拷贝 ngx_cached_err_log_time 作时间戳前缀，
     *    不先初始化则该前缀为空。 */
    ngx_time_init();

    /* 2. 初始化全局日志。第二个参数传空字符串 → nlen==0 →
     *    ngx_log_file.fd = ngx_stderr，直接输出到 stderr，不打开文件。 */
    ngx_log_t *log = ngx_log_init(NULL, (u_char *) "");

    /* 3. 抬高阈值：ngx_log_init 默认 log_level = NGX_LOG_NOTICE(6)，
     *    而 NGX_LOG_INFO = 7 > 6，门控 (6>=7) 为假会丢弃。
     *    改成 INFO 才能让本例的 INFO 消息输出。 */
    log->log_level = NGX_LOG_INFO;

    /* 4. 输出一行 INFO 日志：core 会自动加 时间戳 + [info] + pid#tid 前缀。
     *    %M 是 ngx_msec_t 的格式符，这里把缓存的单调毫秒时钟打进去。 */
    ngx_log_error(NGX_LOG_INFO, log, 0,
                  "tutorial: hello, current_msec=%M", ngx_current_msec);

    return 0;
}
```

2. 验证门控：把第 3 步注释掉（保持默认 `NOTICE`），重新运行，应观察到**没有任何 INFO 输出**——直观体会 `log->log_level >= level` 门控的效果。

3. 对照真实初始化顺序：nginx 在 [src/core/nginx.c:226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L226) 先 `ngx_time_init()`，随后 `ngx_log_init()` 建立初始日志（在 `ngx_init_cycle` 之前用 stderr），与本例顺序一致。

**需要观察的现象**：

- stderr 输出形如：`2024/05/01 12:00:00 [info] 1234#1234: tutorial: hello, current_msec=123456789`。
- 输出首段的时间戳来自 `ngx_cached_err_log_time`（4.1 证明它确实被缓存了）。
- 注释掉第 3 步后无输出，证明门控 `log_level >= level` 生效。
- 若把第 4 步的 `NGX_LOG_INFO` 改成 `NGX_LOG_ERR(4)`，则 `NOTICE(6) >= 4` 成立，即使不抬高水平也会输出。

**预期结果**：抬高 `log_level` 后看到一行带 `[info]` 前缀的日志；不抬高则无输出。本例依赖 nginx 编译环境与进程全局量（`ngx_log_pid` 等），**运行结果待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_log_error` 要做成宏而不是普通函数？

**答案**：为了**门控短路能省掉参数求值**。宏展开后是 `if (log->log_level >= level) ngx_log_error_core(...)`，级别不够时整个 `ngx_log_error_core` 不被调用，连传给它的实参（如 `ngx_log_debug3(NGX_LOG_DEBUG_HTTP, ..., "%d %d %d", a(), b(), c())` 里的 `a()/b()/c()`）都不会求值。若做成普通函数，C 语义要求调用前先求值全部实参，debug 日志的开销就跑不掉了。代价是宏有 `if` 无 `do{}while(0)`，所以 `ngx_log_error(...)` 后不能直接跟 `else`（会悬空）——nginx 源码里调用日志宏后都跟分号、不接 else。

**练习 2**：`ngx_log_error_core` 里的 `while(log)` 循环为什么能 `break` 提前退出？会不会漏写某个目标？

**答案**：不会漏，前提是日志链**按 `log_level` 降序排列**（由 [src/core/ngx_log.c:676-707](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L676-L707) 的 `ngx_log_insert` 保证）。降序意味着阈值高的（更 verbose，如 debug=8）在前、阈值低的（如 notice=6）在后。对一条 `level=L` 的消息，`log_level >= L` 的节点全在链前段，一旦遇到 `log_level < L` 的节点就可 `break`——后面的一定都不够宽。所以一条 INFO(7) 消息会写进 debug(8)、info(7) 两个目标，在 notice(6) 目标前停住，恰好正确。`debug_connection` 是个例外位：即使整体阈值不够，若该连接被标记为「debug 连接」，也会强制继续写。

---

### 4.3 日志级别解析与多目标后端：ngx_log_set_levels 与 error_log

#### 4.3.1 概念说明

用户侧的日志配置全靠一条指令 `error_log`：

```
error_log /var/log/nginx/error.log notice;
error_log /var/log/nginx/debug.log debug_http;
error_log syslog:server=10.0.0.1 info;
error_log stderr;
```

这条指令做了两件事：**选目标**（文件/stderr/syslog/memory）和**选级别**（一个普通级别名 + 任意多个 debug 子类名）。nginx 把它实现为一个 core 模块 `ngx_errlog_module` 的指令（[src/core/ngx_log.c:34-67](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L34-L67)），其处理函数 `ngx_error_log` → `ngx_log_set_log` 负责分发目标、`ngx_log_set_levels` 负责解析级别，最后 `ngx_log_insert` 把新日志节点按级别插入链表。

#### 4.3.2 核心流程

```
error_log 指令 (ngx_error_log)
  └─ ngx_log_set_log(cf, &new_log):
       1. 按第一参数(目标)分发：
          - "stderr"        → 开空文件名 → fd=ngx_stderr
          - "memory:SIZE"   → 分配环形缓冲，挂 writer=ngx_log_memory_writer（仅 DEBUG 编译）
          - "syslog:..."    → 解析 syslog 参数，挂 writer=ngx_syslog_writer
          - 其它(路径)      → 打开文件
       2. ngx_log_set_levels(cf, new_log)   ← 解析后续级别参数
       3. ngx_log_insert(head, new_log)      ← 按级别降序插入日志链
```

`ngx_log_set_levels` 的规则：

| 配置形式 | 结果 `log_level` |
| --- | --- |
| `error_log /path;`（仅路径，无级别） | `NGX_LOG_ERR`（4，默认） |
| `error_log /path notice;` | `NOTICE`（6） |
| `error_log /path debug;` | `DEBUG_ALL`（所有 debug 位） |
| `error_log /path debug_http;` | `DEBUG` \| `DEBUG_HTTP`（`0x8` \| `0x100`） |
| `error_log /path info debug_http;` | 报错：普通级别与 debug 子类不能混用 |

注意：`error_log /path;` 的默认级别是 `ERR`（4），而 `ngx_log_init` 在配置加载**之前**用的初始级别是 `NOTICE`（6）——这是两个不同阶段的默认值。

#### 4.3.3 源码精读

指令与模块定义——日志是 core 模块：

[src/core/ngx_log.c:34-67](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L34-L67) — `error_log` 指令标志 `NGX_MAIN_CONF|NGX_CONF_1MORE`（至少一个参数，即目标路径），处理函数 `ngx_error_log` 只是转调 `ngx_log_set_log`；模块类型 `NGX_CORE_MODULE`，无 init/process 回调——日志模块只贡献指令，不参与运行期初始化。

级别解析——把名字翻成数值：

[src/core/ngx_log.c:478-538](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L478-L538) — `ngx_log_set_levels`：若 `nelts==2`（只有指令名 + 目标，无级别参数）→ `log_level = NGX_LOG_ERR` 直接返回（默认级别）；否则从第 2 个参数起遍历：先在 `err_levels[1..8]` 里找普通级别名，命中则 `log_level = n`（且不允许重复设普通级别）；再在 `debug_levels[]` 里找 debug 子类名，命中则 `log_level |= d`（位或上对应位，且不允许「已有普通级别再叠 debug」或反之）；都找不到则报「invalid log level」。末尾特判：若 `log_level == NGX_LOG_DEBUG`（即用户写了裸 `debug`），扩成 `NGX_LOG_DEBUG_ALL`（全开）。这就是 `ngx_log_error_core` 门控用的 `log_level` 的来源。

多目标分发——一条指令支持四种后端：

[src/core/ngx_log.c:552-673](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L552-L673) — `ngx_log_set_log`：按第一参数前缀分发——`"stderr"` 设空文件名（`fd` 落到 `ngx_stderr`，并置 `log_use_stderr=1`）；`"memory:SIZE"` 仅在 `NGX_DEBUG` 编译下有效，`ngx_parse_size` 解析大小，分配环形缓冲 `ngx_log_memory_buf_t`，挂 `writer = ngx_log_memory_writer`、`wdata = buf`（非 debug 编译报「built without debug support」）；`"syslog:..."` 调 `ngx_syslog_process_conf` 解析 server/facility/severity 等，挂 `writer = ngx_syslog_writer`、`wdata = peer`（syslog 细节见 u10-l3）；其它当文件路径，`ngx_conf_open_file` 打开。最后调 `ngx_log_set_levels` 解析级别、`ngx_log_insert` 入链。注意「目标决定 `file` 或 `writer`」、而「级别只写进 `log_level`」——二者正交。

链表按级别降序插入——保证 4.2 的 `break` 正确：

[src/core/ngx_log.c:676-707](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L676-L707) — `ngx_log_insert`：若新节点级别比链头高，用一个「交换内容」的技巧——把新节点插到头之后、再把它和头的内容整体交换，从而保持链头地址不变（cycle 里很多地方持有头地址）、又让级别最高的排在头；否则沿链找到第一个级别比新节点低的，插到它前面；都没有就挂尾。最终链严格按 `log_level` 降序，这是 `ngx_log_error_core` 写入循环能提前 `break` 的前提。

内存日志 writer——环形缓冲的写入：

[src/core/ngx_log.c:712-739](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L712-L739) — `ngx_log_memory_writer`：用 `ngx_atomic_fetch_add` 原子地领一块写入位偏移 `written % size`，把 `buf` 拷进环形缓冲（跨尾回绕时分两段 `memcpy`）。这是 `error_log memory:16k debug;` 的实现，仅 debug 构建可用，常用于「想抓日志又不想落盘」的场景。它正是 `ngx_log_error_core` 里 `if (log->writer) { log->writer(log, level, errstr, p-errstr); }` 这条分支的真实落点。

#### 4.3.4 代码实践

**实践目标**：用真实 nginx 配置多目标 `error_log`，验证级别阈值与多后端分流。

**操作步骤**：

1. 在测试 nginx.conf 的 `main` 顶层加三条 `error_log`：

```nginx
# 文件 A：记录到 info 级别
error_log /tmp/a_info.log info;
# 文件 B：只记录 error 及更严重
error_log /tmp/b_error.log error;
# 控制台：notice 级别
error_log stderr notice;
```

2. `nginx -t` 校验配置，`nginx` 启动，然后触发一条 INFO 消息（如请求一个不存在的 location 走默认处理，或调高某模块日志）。也可直接 `kill -USR1` 触发日志重开。
3. 分别查看 `/tmp/a_info.log` 与 `/tmp/b_error.log` 的内容差异。

**需要观察的现象**：

- INFO 消息**只出现在 `a_info.log`**，不出现在 `b_error.log`（`b` 的阈值 `ERR=4` < `INFO=7`，门控丢弃）。
- `[error]` 及更严重的消息**两个文件都出现**（两个阈值都 ≥4）。
- stderr 因 `notice` 阈值较高，会显示更多信息（含 notice/info）。
- 对应到源码：这正是 [src/core/ngx_log.c:161-165](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L161-L165) 的 `if (log->log_level < level) break` 在起作用——链按级别降序排，写完高阈值节点遇到 `error` 节点时就停。

**预期结果**：INFO 只进 info 文件，error 进两个文件，级别阈值与链表顺序的预测与现实一致。**具体日志条数待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`error_log /path;`（不带级别）和 `error_log /path debug;` 的默认行为分别是什么？

**答案**：不带级别时，[src/core/ngx_log.c:484-487](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L484-L487) 因 `nelts==2` 直接设 `log_level = NGX_LOG_ERR`（4）——只记录 error 及以上。写裸 `debug` 时，经 [src/core/ngx_log.c:533-535](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L533-L535) 特判扩成 `NGX_LOG_DEBUG_ALL`，全开 7 个 debug 子类。两者都不要和「配置加载前 `ngx_log_init` 的 `NOTICE` 默认值」混淆——那是启动早期、尚未解析配置时的临时级别。

**练习 2**：为什么 `error_log memory:16k debug;` 在普通（非 `--with-debug`）构建下会报错？

**答案**：内存日志后端专供 debug 场景「抓全量日志但不落盘」，其 writer `ngx_log_memory_writer` 与缓冲结构都被包在 `#if (NGX_DEBUG)` 内（[src/core/ngx_log.c:585-642](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L585-L642) 的 `#else` 分支直接报 "nginx was built without debug support"）。普通构建没有 `NGX_DEBUG` 宏，相关代码不编译，故指令无法生效。要用它必须 `./configure --with-debug` 重新编译（见 u10-l5）。

---

## 5. 综合实践

把本讲的时间缓存（4.1）、日志门控（4.2）、级别与多目标（4.3）串起来，完成一个「真实运行 + 行为解释」任务：

**背景**：你打开任意一份 nginx 错误日志，第一行形如 `2024/05/01 12:00:00 [error] 1234#5678: *9 ...`。这行里同时藏着本讲的全部三个子系统。

**任务**：

1. **时间戳从哪来**：对照 [src/core/ngx_log.c:117-118](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L117-L118)（`ngx_cpymem(errstr, ngx_cached_err_log_time.data, ...)`）与 [src/core/ngx_times.c:150-155](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L150-L155)（`cached_err_log_time` 的 `%4d/%02d/%02d %02d:%02d:%02d` 格式），解释行首的 `2024/05/01 12:00:00` 是**事件循环上一轮 `ngx_time_update` 预格式化好的缓存字符串**，而非写日志时现算。追问：若该日志时间戳与你的手表差了几秒，可能的原因是什么？（提示：worker 是否长时间没有 I/O 事件、或开了 `timer_resolution`。）

2. **级别与 pid/连接号**：对照 [src/core/ngx_log.c:120-128](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L120-L128)，解释 `[error]` 来自 `err_levels[4]`、`1234#5678` 是 pid#tid、`*9` 是连接号（来自 `log->connection`）。把测试配置的 `error_log` 级别从 `info` 改成 `warn`，重启，复现同一个请求，确认原本出现的 `[info]` 行消失了——用门控 `log_level >= level` 解释。

3. **多目标分流**：配置 `error_log /tmp/a.log info;` 与 `error_log /tmp/b.log error;` 两条，复现一个会同时产生 info 和 error 的场景（如访问一个配置错误的 location）。验证 4.3.4 的预测：info 行只进 `a.log`，error 行两个文件都有。再追加一条 `error_log syslog:server=127.0.0.1 info;`（用本机一个 UDP 监听端口接收，或暂略），对照 [src/core/ngx_log.c:644-656](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L644-L656) 说明为何 syslog 目标走 `writer` 回调而非 `ngx_write_fd`。

**预期产出**：一张「日志行字段 × 来源函数/全局变量 × 所属子系统」的三列表（时间戳 → `ngx_cached_err_log_time`/4.1；级别 → `err_levels[]`/4.2；目标分流 → `log_level` 阈值 + 链表降序/4.3）。这张表是你后续排查 nginx 问题（看懂错误日志、调整级别、接 syslog）时的「日志速查卡」，也是 u10-l3（访问日志与 syslog 输出）与 u10-l5（调试日志）的直接前置。

## 6. 本讲小结

- nginx 用**时间缓存**避免频繁系统调用：`ngx_time_update` 在事件循环每轮刷新一次（[src/event/modules/ngx_epoll_module.c:804-806](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/modules/ngx_epoll_module.c#L804-L806)），把秒/毫秒与五种格式化字符串写进 64 个轮转 slot；读者用 `ngx_time()` 宏与 `ngx_current_msec` 全局变量**无锁读取**，撕裂值在「读者被抢占超 64 秒」这种不现实场景外不会发生。
- `ngx_current_msec` 来自 `CLOCK_MONOTONIC` 单调时钟（[src/core/ngx_times.c:195-209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L195-L209)），是事件定时器的心跳；同秒快路径（[src/core/ngx_times.c:103-107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L103-L107)）把格式化开销降到每秒一次。
- `ngx_log_error` 是**变参宏做门控**（`log->log_level >= level` 才调 core）、`ngx_log_error_core` **做格式化与多目标写入**：在栈上 `errstr[2048]` 里依次拼「缓存时间戳 + `[级别]` + pid#tid + 连接号 + 用户消息 + errno + handler 上下文」，再沿日志链逐个写出。
- 日志链**按 `log_level` 降序**排列（[src/core/ngx_log.c:676-707](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_log.c#L676-L707)），写入循环遇到阈值不够的节点就 `break`，从而一条消息只进「级别足够宽」的目标；`log_level` 越高越宽松（debug=8 最宽），debug 还细分为 7 个模块位掩码。
- `error_log` 指令由 `ngx_log_set_log` 按目标分发（文件/stderr/syslog/memory 四后端）、`ngx_log_set_levels` 解析级别，二者正交；syslog 与 memory 后端通过 `writer` 回调输出（`ngx_write_fd` 之外的分支），这是日志「多目标可插拔」的落点。
- 时间与日志存在**隐性依赖**：`ngx_log_error_core` 直接拷贝 `ngx_cached_err_log_time` 作时间戳，所以 `ngx_time_init` 必须在任何日志输出之前完成——`main()` 里 `ngx_time_init`（[src/core/nginx.c:226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L226)）早于 cycle 初始化正是为此。

## 7. 下一步学习建议

- 本讲的 `ngx_current_msec` 是 **u5-l4 定时器与 posted 事件** 的直接前置：红黑树定时器用它比较超时、`ngx_event_find_timer` / `ngx_event_expire_timers` 全靠它驱动。学完那里你会理解「时间缓存为什么必须单调、为什么精度是事件粒度」。
- 本讲的 `ngx_time_update` 调用点（`NGX_UPDATE_TIME` 标志）将在 **u5-l5 事件主循环 process_events_and_timers** 完整闭合：`timer_resolution` 模式下用 SIGALRM 唤醒刷新、普通模式下每轮带标志刷新，两种策略在那里讲清。
- 本讲的 `ngx_log_error` 宏与日志链是 **u10-l3 访问日志与 syslog 输出** 的基础：访问日志复用 `ngx_vslprintf` 变量求值，syslog 后端即本讲的 `ngx_syslog_writer`。
- 本讲的 `--with-debug` 与 debug 子类位掩码将在 **u10-l5 调试与性能分析** 展开：`error_log /path debug_http;` 如何只开 HTTP 那一位、debug 日志如何零开销编译进二进制。
- 若想看时间缓存的「发布」机制在并发下的更多细节，可在学完 u4-l3（共享内存与原子操作）后回看 [src/core/ngx_times.c:182-192](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_times.c#L182-L192) 的 `ngx_memory_barrier` 与 `ngx_trylock`，对照原子/屏障语义加深理解。
