# master/worker 进程循环

> 本讲是「进程模型与操作系统抽象」单元的第一篇。在 u1-l4 里你已经看到 `main()` 走到最后会把控制权交给 `ngx_master_process_cycle` 或 `ngx_single_process_cycle`，但没有展开 master 进程到底在循环里干什么、worker 进程又是由谁、怎么被拉起来的。本讲就把这条「进程循环」的主线彻底走通——它既是 nginx 高可用的根基（reload、二进制升级、优雅停止都发生在这里），也是后续事件驱动（u5）、HTTP 请求处理（u6）的前置舞台。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 nginx 的进程身份有哪几种，并能在源码中找到对应的 `NGX_PROCESS_*` 常量与它们在哪里被设置。
2. 读懂 `ngx_master_process_cycle` 这个「信号驱动的状态机」：master 为什么长期睡在 `sigsuspend` 里、被信号唤醒后又如何根据一组 `sig_atomic_t` 标志位决定下一步动作。
3. 解释 `ngx_start_worker_processes` 如何通过 `ngx_spawn_process` 派生 worker、如何用「进程表 + channel」把 worker 纳入管理。
4. 读懂 `ngx_worker_process_cycle`：worker 在 `ngx_worker_process_init` 里做了哪些「降权、设亲和性、调 `init_process`」的准备工作，然后才进入「事件循环」。
5. 把 reload（SIGHUP）、二进制升级（SIGUSR2）、优雅停止（SIGQUIT）这三大运维操作对应到 master 与 worker 之间一整套「派生新进程 → 移交监听端口 → 通知旧进程退出」的协调步骤上。

---

## 2. 前置知识

本讲默认你已掌握 u1-l4（`main()` 全流程）和 u3-l2（`ngx_init_cycle` 装配线）。这里再补三个本讲反复用到的底层直觉。

### 2.1 进程身份：master 不是 worker

nginx 在生产环境默认以「一个 master 进程 + 若干 worker 进程」的方式运行。你执行 `nginx` 启动的那个进程，`fork` 出 worker 之后自己就变成 master。两者的分工是：

- **master**：以 root 身份运行，负责读取配置、打开监听端口（需要特权）、管理 worker 的生死、响应管理信号。它**不处理任何业务连接**。
- **worker**：以非特权用户（`user` 指令指定）运行，真正处理 HTTP/TCP 请求。worker 数量由 `worker_processes` 决定。

代码里用一个全局变量 `ngx_process` 标记「我现在是哪种进程」，它的取值在 [src/os/unix/ngx_process_cycle.h:23-27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h#L23-L27) 定义：`SINGLE`、`MASTER`、`SIGNALLER`、`WORKER`、`HELPER`。

### 2.2 信号是 master 的「输入」

master 几乎不主动干活，它等信号。运维命令 `nginx -s reload` 本质上就是「读 pid 文件 → 向 master 发 SIGHUP」。信号到达后，内核打断 master 正在执行的代码，跳到 `ngx_signal_handler`，handler 只做一件最简单的事——把某个 `sig_atomic_t` 全局标志位置 1，然后返回。master 醒来后再去查这些标志位决定动作。这套「handler 只置位、主循环来响应」的模式避免了在信号处理函数里做复杂逻辑（信号处理函数能安全调用的函数很有限）。

nginx 关注的信号在 `src/core/ngx_config.h` 里被映射成名字宏，关键几个（非 LinuxThreads 模式）：

| 指令名 | 信号 | 宏 | 触发的标志位 |
|--------|------|-----|-------------|
| reload | HUP | `NGX_RECONFIGURE_SIGNAL` | `ngx_reconfigure = 1` |
| reopen | USR1 | `NGX_REOPEN_SIGNAL` | `ngx_reopen = 1` |
| stop | TERM | `NGX_TERMINATE_SIGNAL` | `ngx_terminate = 1` |
| quit | QUIT | `NGX_SHUTDOWN_SIGNAL` | `ngx_quit = 1` |
| — | USR2 | `NGX_CHANGEBIN_SIGNAL` | `ngx_change_binary = 1` |
| — | WINCH | `NGX_NOACCEPT_SIGNAL` | `ngx_noaccept = 1` |
| — | CHLD | SIGCHLD | `ngx_reap = 1` |

宏定义见 [src/core/ngx_config.h:60-71](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h#L60-L71)。哪些信号映射到哪个标志位，则由 [src/os/unix/ngx_process.c:318-467](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L318-L467) 的 `ngx_signal_handler` 完成（下一节精读）。

### 2.3 channel：master 与 worker 的「命令管道」

光用 `kill` 发信号只能传递「一个信号编号」，传达不了「新拉起的 worker 在进程表的第几格、它那一端的 channel fd 是几号」这种结构化信息。于是 nginx 在 `ngx_spawn_process` 里为每个子进程建一对 `socketpair`（`channel[0]` 给 master 写、`channel[1]` 给 worker 读），master 通过这条 channel 给 worker 下达 `NGX_CMD_QUIT` / `NGX_CMD_TERMINATE` / `NGX_CMD_REOPEN` 等命令。命令编号定义在 [src/os/unix/ngx_process_cycle.h:16-20](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h#L16-L20)。channel 的收发细节是 u4-l2 的主题，本讲你只需要知道「master 既会用信号、也会用 channel 两种途径指挥 worker」即可。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/os/unix/ngx_process_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c) | 本讲主战场。master 循环、worker 循环、派生 worker、收割子进程、cache 辅助进程都在这里。 |
| [src/os/unix/ngx_process_cycle.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h) | 进程身份常量、channel 命令常量、信号标志位的 `extern` 声明。 |
| [src/os/unix/ngx_process.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c) | `ngx_spawn_process`（fork 封装）、`ngx_signal_handler`（信号→标志位）、`ngx_processes[]` 进程表。 |
| [src/os/unix/ngx_process.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h) | `ngx_process_t` 进程表项结构、`NGX_PROCESS_*RESPAWN*` 派生类型常量。 |
| [src/core/ngx_config.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h) | 信号名宏（HUP/USR2/QUIT…）映射。 |
| [src/core/nginx.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c) | `ngx_exec_new_binary`——二进制升级时旧 master 派生新二进制的入口（u1-l4 已讲过接收端，本讲讲发送端如何被触发）。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：4.1 master 的信号状态机（`ngx_master_process_cycle`）、4.2 派生 worker 与 cache 辅助进程（`ngx_start_worker_processes`）、4.3 worker 的事件循环（`ngx_worker_process_cycle` + `ngx_worker_process_init`）、4.4 三大运维操作（reload / upgrade / graceful-stop）的协调。

### 4.1 ngx_master_process_cycle：信号驱动的状态机

#### 4.1.1 概念说明

`ngx_master_process_cycle` 是 master 进程的「全部人生」。它做三件事：

1. **屏蔽并准备好一组信号**，决定哪些信号可以打断 master；
2. **派生初始的一批 worker（和 cache 辅助进程）**；
3. **进入一个永不返回的 `for ( ;; )` 循环**，循环里只做一件事：睡在 `sigsuspend` 上等信号 → 醒来 → 查标志位 → 分发动作 → 再回去睡。

这是一种典型的「**信号驱动的状态机**」：master 本身没有「业务逻辑」，它的逻辑就是「收到什么信号，就进入哪个分支」。理解了这一点，那个长达一百多行的 `for` 循环就不再吓人——它只是一长串 `if (标志位) { 做对应的事 }`。

之所以要先把信号屏蔽掉、再用 `sigsuspend` 临时解除，是为了避免信号在 master 处理上一次信号的途中「插队」造成竞态。`sigsuspend(&set)` 是 POSIX 提供的原子操作：把进程的信号屏蔽字换成 `set`（这里是空集，即全部放开），然后睡眠，直到有信号到来、handler 跑完才返回；返回时屏蔽字自动恢复成原来的（即重新屏蔽）。这样 master 只在「睡眠时」才会被信号打断，处理标志位的代码段天然是「信号安全区」。

#### 4.1.2 核心流程

master 主循环的状态机可以画成：

```text
            ┌─────────────────────────────────────────┐
 start:  派生 worker_processes 个 worker + cache 辅助进程
            └──────────────────┬──────────────────────┘
                               ▼
        ┌──────────────────────────────────────────┐
 loop:  │ 设置 itimer（若正在强制终止）             │
        │ sigsuspend()  ← 在这里阻塞，等信号        │
        │ ngx_time_update()  ← 醒来刷新缓存时间     │
        └──────────────────┬───────────────────────┘
                           ▼
   ┌──── ngx_reap?  ──→ ngx_reap_children()：收割死进程，必要时 respawn
   │     (SIGCHLD)
   │
   ├──── 全死光 + (terminate||quit)? ──→ ngx_master_process_exit()：master 自己退
   │
   ├──── ngx_terminate? ──→ 给 worker 发 TERM（超时升级为 KILL），continue
   │
   ├──── ngx_quit?    ──→ 给 worker 发 QUIT + 关监听端口（优雅停），continue
   │
   ├──── ngx_reconfigure? ──→ reload 分支（见 4.4.1）
   │
   ├──── ngx_restart?  ──→ 重新派生 worker（升级回退用）
   │
   ├──── ngx_reopen?   ──→ 重开日志 + 让 worker 也重开
   │
   ├──── ngx_change_binary? ──→ exec 新二进制（升级，见 4.4.2）
   │
   └──── ngx_noaccept?  ──→ 让 worker 停止 accept（升级中停旧 worker）
                           │
                           └──→ 回到 loop
```

注意一个细节：master 的「强制终止」有一个指数退避的升级机制。当 `ngx_terminate` 被置位，master 先温和地给 worker 发 `NGX_TERMINATE_SIGNAL`（让它们立刻退出），并用一个 `delay` 计时器；每次 `SIGALRM` 到来 `delay` 翻倍，一旦 `delay > 1000` 毫秒仍未收完，就改发 `SIGKILL` 强杀。设初始 \( d_0 = 50 \) 毫秒，则 \( d_n = 50 \cdot 2^n \)。当 \( 50 \cdot 2^n > 1000 \)，即 \( 2^n > 20 \)，最小的 \( n = 5 \)（此时 \( d_5 = 1600 \) 毫秒）。也就是说大约经过 \( 50+100+200+400+800 = 1550 \) 毫秒的「温和期」之后，master 才会升级到 `SIGKILL`。

#### 4.1.3 源码精读

**第一步：屏蔽信号。** master 把自己关心的所有信号加入屏蔽集，调用 `sigprocmask(SIG_BLOCK, ...)` 暂时挡住它们；随后立刻把 `set` 清空，留给后面的 `sigsuspend` 使用。见 [src/os/unix/ngx_process_cycle.c:87-104](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L87-L104)：

```c
sigemptyset(&set);
sigaddset(&set, SIGCHLD);
...
sigaddset(&set, ngx_signal_value(NGX_SHUTDOWN_SIGNAL));
sigaddset(&set, ngx_signal_value(NGX_CHANGEBIN_SIGNAL));

if (sigprocmask(SIG_BLOCK, &set, NULL) == -1) { ... }

sigemptyset(&set);   // ← 注意：清空，供 sigsuspend 用
```

**第二步：设置进程标题。** master 把自己的 `ps` 名字改成 `master process /path/to/nginx ...`（带完整命令行），方便你 `ps -ef | grep nginx` 时一眼认出。见 [src/os/unix/ngx_process_cycle.c:107-125](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L107-L125)。

**第三步：派生初始 worker 与 cache 进程，然后进入循环。** 见 [src/os/unix/ngx_process_cycle.c:128-137](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L128-L137)：

```c
ccf = (ngx_core_conf_t *) ngx_get_conf(cycle->conf_ctx, ngx_core_module);

ngx_start_worker_processes(cycle, ccf->worker_processes, NGX_PROCESS_RESPAWN);
ngx_start_cache_manager_processes(cycle, 0);

ngx_new_binary = 0;
delay = 0; sigio = 0; live = 1;
```

`ccf->worker_processes` 就是 `nginx.conf` 里 `worker_processes` 那条指令的值（`auto` 在更早的 `ngx_set_worker_processes` 处已解析成具体数字）。`NGX_PROCESS_RESPAWN` 是派生类型，含义见 4.2.1。

**第四步：主循环——睡眠、刷新时间、分发。** 见 [src/os/unix/ngx_process_cycle.c:139-179](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L139-L179)：

```c
for ( ;; ) {
    if (delay) {                          // 强制终止时的退避定时器
        ...
        if (setitimer(ITIMER_REAL, &itv, NULL) == -1) { ... }
    }

    sigsuspend(&set);                     // ← master 在这里「睡觉等信号」
    ngx_time_update();                    // 醒来后刷新缓存时间（u2-l5）

    if (ngx_reap) {
        ngx_reap = 0;
        live = ngx_reap_children(cycle);  // 收割 SIGCHLD 通知的死亡子进程
    }

    if (!live && (ngx_terminate || ngx_quit)) {
        ngx_master_process_exit(cycle);   // 子进程全没了 → master 也走
    }
    ...
```

后面紧跟的就是一长串对应各标志位的 `if` 分支：`ngx_terminate`（[L181-201](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L181-L201)）、`ngx_quit`（[L203-209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L203-L209)）、`ngx_reconfigure`（[L211-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L211-L244)）、`ngx_restart`（[L246-252](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L246-L252)）、`ngx_reopen`（[L254-260](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L254-L260)）、`ngx_change_binary`（[L262-266](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L262-L266)）、`ngx_noaccept`（[L268-273](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L268-L273)）。reload 与 upgrade 两个分支是本讲重头，留到 4.4 详述。

> **配套阅读**：信号→标志位的「翻译表」在 [src/os/unix/ngx_process.c:340-406](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L340-L406)（`case NGX_PROCESS_MASTER` 段）。比如 HUP 在 L364-367 把 `ngx_reconfigure` 置 1，USR2 在 L374-391 把 `ngx_change_binary` 置 1。把这段和上面 master 循环对照看，就形成了完整的「信号 → 标志位 → 分支」闭环。

#### 4.1.4 代码实践

**实践目标**：把 master 循环的「睡眠—唤醒—分发」结构在源码里走一遍，确认 master 真的几乎不干活、只对标志位做反应。

**操作步骤**：

1. 打开 [src/os/unix/ngx_process_cycle.c:139-274](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L139-L274) 的主循环。
2. 数一下 `for ( ;; )` 里直接调用 `sigsuspend` 之外的「实质工作函数」有几个（答案应该是 `ngx_reap_children`、`ngx_signal_worker_processes`、`ngx_init_cycle`、`ngx_start_worker_processes`、`ngx_reopen_files`、`ngx_close_listening_sockets`、`ngx_exec_new_binary`、`ngx_master_process_exit` 这几类）。
3. 在每个 `if (ngx_xxx)` 分支旁边用注释写上「这条分支由哪个信号触发」。

**需要观察的现象**：你会看到 master 循环里**没有任何处理 HTTP 连接、读写 socket 的代码**——那些都在 worker 里。这印证了「master 不管业务」。

**预期结果**：你能画出本节 4.1.2 那张状态机图，并把每条边标注上对应的信号。这是后续理解 reload/upgrade 的基础。

#### 4.1.5 小练习与答案

**练习 1**：为什么 master 要先用 `sigprocmask` 屏蔽信号，循环里又用 `sigsuspend(&set)`（`set` 是空集）去解除？直接不屏蔽、让信号随时打断不行吗？

> **参考答案**：不屏蔽会让信号在「处理上一次信号的代码段」中间到达，造成标志位竞态（比如正在 `ngx_reap_children` 时又来 SIGCHLD）。`sigsuspend` 是原子的「换屏蔽字 + 睡眠」，保证 master 只在睡眠期间响应信号，处理标志位的代码段是信号安全区。

**练习 2**：master 收到 `SIGTERM` 后，`delay` 从 0 变成 50，之后每来一次 `SIGALRM` 翻倍。这个 `delay` 控制的是什么？

> **参考答案**：强制终止时 master 先温和地给 worker 发 `NGX_TERMINATE_SIGNAL`，`delay` 是两次催促之间的等待。`delay > 1000` 毫秒后升级为 `SIGKILL`。翻倍是为了「先给足时间优雅退，越来越不耐烦」。

**练习 3**：`live` 这个变量表示什么？为什么 `!live && (ngx_terminate || ngx_quit)` 时 master 才自己退出？

> **参考答案**：`live` 表示「是否还有活着的子进程」（由 `ngx_reap_children` 返回）。只有当所有子进程都已退出、且收到终止/关闭信号时，master 才调用 `ngx_master_process_exit` 自行退出，避免「master 先走、worker 成孤儿」。

---

### 4.2 ngx_start_worker_processes：派生 worker 与 cache 辅助进程

#### 4.2.1 概念说明

master 自己不 `fork`，它调用 `ngx_start_worker_processes`，后者循环 N 次调用 `ngx_spawn_process` 完成真正的 `fork`。理解这一节要抓住三件事：

**① 进程表 `ngx_processes[]`。** nginx 维护一个全局数组 `ngx_processes[NGX_MAX_PROCESSES]`（最多 1024 项，见 [src/os/unix/ngx_process.h:47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L47) 与 [L87](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L87)），每个表项是一个 `ngx_process_t`，记录该子进程的 pid、channel fd 对、入口函数指针、以及一组「是否需要拉起/是否刚拉起/是否在退出/是否已退出」的位域（[src/os/unix/ngx_process.h:22-36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L22-L36)）。master 对 worker 的所有管理都基于这张表。`ngx_last_process` 记录表已用到第几格。

**② 派生类型（respawn type）。** `ngx_spawn_process` 的最后一个参数是「派生类型」，它决定「这个子进程意外死亡后要不要自动重新拉起」。取值在 [src/os/unix/ngx_process.h:49-53](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L49-L53)：

| 常量 | 含义 |
|------|------|
| `NGX_PROCESS_RESPAWN` | worker 崩溃后自动重启（普通 worker 用这个） |
| `NGX_PROCESS_JUST_RESPAWN` | 自动重启 + 标记「刚拉起」（reload 时新 worker 用，避免被立刻发退出信号） |
| `NGX_PROCESS_NORESPAWN` | 崩溃不重启（cache loader 用，跑完一次就退） |
| `NGX_PROCESS_JUST_SPAWN` | 不重启 + 标记「刚拉起」 |
| `NGX_PROCESS_DETACHED` | 分离的（`ngx_execute` 执行外部命令用，不建 channel） |

**③ cache 辅助进程。** 除了 worker，nginx 还会按需派生两类「helper」进程：**cache manager**（周期性清理过期缓存）和 **cache loader**（启动时一次性把磁盘缓存元数据载入共享内存）。它们和 worker 一样由 `ngx_spawn_process` 派生、一样进入事件循环，但不处理业务连接，且派生类型不同。

#### 4.2.2 核心流程

`ngx_start_worker_processes(cycle, n, type)` 的逻辑极简：

```text
for i = 0 .. n-1:
    ngx_spawn_process(cycle, 入口=ngx_worker_process_cycle,
                      data=(void*)i, 名字="worker process", type)
    ngx_pass_open_channel(cycle)   // 把「我刚拉起的这个 worker」广播给所有兄弟 worker
```

`ngx_spawn_process` 内部（[src/os/unix/ngx_process.c:86-258](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L86-L258)）做四件事：

1. 在 `ngx_processes[]` 里找一个空闲槽位 `s`（`pid == -1` 的格子）。
2. `socketpair` 建一对 channel，设非阻塞、设 `FIOASYNC`/`F_SETOWN`（让 channel[0] 可读时给 master 发 SIGIO）、设 `FD_CLOEXEC`（`exec` 时自动关，避免泄漏给新二进制）。
3. `fork()`。子进程把 `ngx_pid` 改成自己的 getpid、把 `ngx_parent` 记成父进程 pid，然后调用入口函数 `proc(cycle, data)`——对 worker 来说就是 `ngx_worker_process_cycle`，从此再不返回。父进程继续往下。
4. 把 pid、入口函数、data、名字填进 `ngx_processes[s]`，按 `type` 设置 `respawn/just_spawn/detached` 位域。

`ngx_pass_open_channel`（[src/os/unix/ngx_process_cycle.c:395-428](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L395-L428)）则向**所有其它活着的子进程**发一条 `NGX_CMD_OPEN_CHANNEL` 消息，告诉它们「进程表第 `slot` 格多了一个 pid 为 `ch.pid` 的兄弟，它那一端 channel fd 是 `ch.fd`」。这样每个 worker 都持有一份完整的「兄弟 worker 通讯录」。

#### 4.2.3 源码精读

**派生 worker 的核心两行**，见 [src/os/unix/ngx_process_cycle.c:342-348](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L342-L348)：

```c
for (i = 0; i < n; i++) {
    ngx_spawn_process(cycle, ngx_worker_process_cycle,
                      (void *) (intptr_t) i, "worker process", type);
    ngx_pass_open_channel(cycle);
}
```

注意 `data` 传的是 `(void *) i`——worker 序号。worker 入口函数会把它还原成自己的编号 `ngx_worker = worker`，用来做 CPU 亲和性分配等（见 4.3）。

**派生类型如何落到表项位域**，见 [src/os/unix/ngx_process.c:220-251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L220-L251)：

```c
switch (respawn) {
case NGX_PROCESS_NORESPAWN:    ... respawn=0; just_spawn=0; detached=0; break;
case NGX_PROCESS_JUST_SPAWN:   ... respawn=0; just_spawn=1; detached=0; break;
case NGX_PROCESS_RESPAWN:      ... respawn=1; just_spawn=0; detached=0; break;
case NGX_PROCESS_JUST_RESPAWN: ... respawn=1; just_spawn=1; detached=0; break;
case NGX_PROCESS_DETACHED:     ... respawn=0; just_spawn=0; detached=1; break;
}
```

**cache 辅助进程的派生**，见 [src/os/unix/ngx_process_cycle.c:352-392](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L352-L392)：它先扫描 `cycle->paths`，只有当存在带 `manager` 回收回调的路径（比如配了 `proxy_cache_path`）才派生 cache manager；只有存在带 `loader` 回调的路径才派生 cache loader。cache loader 用 `NGX_PROCESS_NORESPAWN`/`JUST_SPAWN`（跑完即退、不重启），cache manager 用 `RESPAWN`/`JUST_RESPAWN`（长期存活、崩了重启）。这也是为什么「没配缓存的 nginx 在 `ps` 里看不到 cache manager 进程」。

> 这两类进程的主循环 `ngx_cache_manager_process_cycle` 在 [src/os/unix/ngx_process_cycle.c:1088-1136](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1088-L1136)，它们复用了 worker 的初始化与事件循环，只是入口事件不同——这是 u10-l1《共享文件缓存》的内容，本讲不展开。

#### 4.2.4 代码实践

**实践目标**：搞清楚一次启动会派生几个进程、各自是什么身份，建立「进程表」的直观感受。

**操作步骤**：

1. 编译并启动一个 nginx（设 `worker_processes 2;` 且**不**配任何 `*_cache_path`）。
2. 执行 `ps -ef | grep nginx`，观察进程列表。
3. 把 `nginx.conf` 改成 `worker_processes 4;` 并加一行 `proxy_cache_path /tmp/cache keys_zone=one:1m;`，`nginx -s reload` 后再 `ps` 一次。

**需要观察的现象**：第一次应看到「1 个 master + 2 个 worker」共 3 个进程；第二次应看到「1 个 master + 4 个 worker + 1 个 cache manager + 1 个 cache loader」。

**预期结果**：进程数量与 4.2.3 的派生逻辑吻合——worker 数 = `worker_processes`，cache 进程数取决于是否配了缓存路径。如果你开启了 `--with-debug` 编译，还可在 `error_log` 里看到 `ngx_start_worker_processes` 打印的 `start worker processes` 与每个 `start worker process <pid>` 日志。

**待本地验证**：不同发行版/容器里 `ps` 输出格式略有差异；cache loader 进程在加载完成后会自行 `exit(0)`（见 [ngx_cache_loader_process_handler](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1168-L1191) 的最后一行），所以你可能只在一开始那一瞬看到它。

#### 4.2.5 小练习与答案

**练习 1**：为什么普通 worker 用 `NGX_PROCESS_RESPAWN`，而 reload 时新拉起的 worker 用 `NGX_PROCESS_JUST_RESPAWN`？多出来的那个 `just_spawn` 位有什么用？

> **参考答案**：reload 时新 worker 与旧 worker 短暂并存。master 随后会调用 `ngx_signal_worker_processes` 给旧 worker 发退出信号；该函数会跳过 `just_spawn==1` 的进程（[L485-488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L485-L488)），从而避免「把刚拉起的新 worker 也一起杀掉」。下一次循环 `just_spawn` 被清零。

**练习 2**：`ngx_spawn_process` 给 channel fd 设置了 `FD_CLOEXEC`。这与二进制升级（`ngx_exec_new_binary`）有什么关系？

> **参考答案**：升级时旧 master 会 `fork`+`execve` 一个新二进制。`FD_CLOEXEC` 保证这些 channel fd 不会泄漏进新二进制进程——新二进制需要的只是监听 socket（通过 `NGINX` 环境变量显式传递），不需要旧 master 与旧 worker 之间的 channel。

---

### 4.3 ngx_worker_process_cycle：worker 的事件循环

#### 4.3.1 概念说明

worker 进程的入口是 `ngx_worker_process_cycle`（它就是 `ngx_spawn_process` 在子进程里调用的那个 `proc`）。它先做一次性的「**worker 初始化**」`ngx_worker_process_init`，然后进入自己的 `for ( ;; )` 事件循环。

初始化阶段的关键工作（按顺序）：

1. **设环境**、**改优先级**、**设 `RLIMIT_NOFILE`/`RLIMIT_CORE`** 等资源限制；
2. **降权**：如果 master 以 root 运行，worker 在这里 `setgid` + `initgroups` + `setuid` 切到 `user`/`group` 指令指定的非特权身份（这是 nginx 的安全基线——worker 才是处理网络数据的，绝不能带着 root 跑）；
3. **CPU 亲和性**：按 worker 编号绑定 CPU（`worker_cpu_affinity`）；
4. **解除信号屏蔽**：worker 要能响应信号；
5. **调用所有模块的 `init_process` 回调**：这是模块在「每个 worker 启动时」做初始化的统一钩子（比如事件模块在这里初始化 epoll）；
6. **关闭不需要的 channel fd**：每个 worker 只保留「自己那一端 channel[1]」和「向兄弟 worker 发消息用的 channel[0] 集合」；并把自己的 channel[1] 注册成一个读事件，handler 是 `ngx_channel_handler`。

初始化完成后，worker 进入循环，循环体极简：调用 `ngx_process_events_and_timers(cycle)` 处理一轮事件，然后检查 `ngx_terminate`/`ngx_quit`/`ngx_exiting`/`ngx_reopen` 等标志位做相应动作。`ngx_process_events_and_timers` 是整个事件驱动的入口，u5 单元会专门讲，本讲把它当成「worker 处理一轮网络事件」的黑盒即可。

#### 4.3.2 核心流程

```text
worker 进程（fork 出来的子进程）:
    ngx_process = NGX_PROCESS_WORKER          ← 标记身份
    ngx_worker = worker                        ← 记下自己的编号
    ngx_worker_process_init(cycle, worker):    ← 一次性准备
        降权 / 亲和性 / 解屏蔽信号 / 各模块 init_process / 注册 channel 读事件
    ngx_setproctitle("worker process")         ← ps 里看到的名字
    for ( ;; ):
        if ngx_exiting:                        ← 正在优雅退出
            if 没有遗留定时器:  ngx_worker_process_exit()
        ngx_process_events_and_timers(cycle)   ← ★ 处理一轮事件（u5 主题）
        if ngx_terminate:  ngx_worker_process_exit()   ← 立刻退
        if ngx_quit:                              ← 优雅退出
            ngx_exiting = 1
            关监听端口 / 关空闲连接 / 处理 posted 事件
            （标题改成 "worker process is shutting down"）
        if ngx_reopen:  重开日志文件
```

注意 worker 对 `ngx_quit`（优雅退出）的处理与 `ngx_terminate`（立即退出）不同：优雅退出时 worker **不会立刻走**，而是设 `ngx_exiting = 1`、关掉监听端口（不再接新连接）、关掉空闲连接，但**继续服务已经在处理的连接**，直到所有定时器都到期（`ngx_event_no_timers_left()` 返回 OK）才真正 `exit`。这就是「graceful」的含义。

#### 4.3.3 源码精读

**worker 入口与循环**，见 [src/os/unix/ngx_process_cycle.c:698-749](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L698-L749)：

```c
static void
ngx_worker_process_cycle(ngx_cycle_t *cycle, void *data)
{
    ngx_int_t worker = (intptr_t) data;

    ngx_process = NGX_PROCESS_WORKER;
    ngx_worker = worker;

    ngx_worker_process_init(cycle, worker);   // ← 一次性准备
    ngx_setproctitle("worker process");

    for ( ;; ) {
        if (ngx_exiting) {
            if (ngx_event_no_timers_left() == NGX_OK) {
                ngx_worker_process_exit(cycle);
            }
        }
        ngx_process_events_and_timers(cycle);  // ★ 事件驱动核心（u5）

        if (ngx_terminate) { ... ngx_worker_process_exit(cycle); }

        if (ngx_quit) {                         // 优雅退出
            ngx_quit = 0;
            ngx_setproctitle("worker process is shutting down");
            if (!ngx_exiting) {
                ngx_exiting = 1;
                ngx_set_shutdown_timer(cycle);
                ngx_close_listening_sockets(cycle);
                ngx_close_idle_connections(cycle);
                ngx_event_process_posted(cycle, &ngx_posted_events);
            }
        }
        if (ngx_reopen) { ngx_reopen_files(cycle, -1); }
    }
}
```

**初始化里的降权片段**，见 [src/os/unix/ngx_process_cycle.c:799-829](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L799-L829)：当 `geteuid() == 0`（即 master 是 root），worker 依次 `setgid` → `initgroups` → `setuid`，任一致命错误都 `exit(2)`。这就是为什么 worker 在 `ps` 里显示为 `nobody` 或你配置的用户。

**调用所有模块 `init_process` 的地方**，见 [src/os/unix/ngx_process_cycle.c:891-898](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L891-L898)：

```c
for (i = 0; cycle->modules[i]; i++) {
    if (cycle->modules[i]->init_process) {
        if (cycle->modules[i]->init_process(cycle) == NGX_ERROR) {
            exit(2);   // 任何模块 init_process 失败 → worker 直接挂
        }
    }
}
```

这段呼应 u3-l3：`init_process` 是 `ngx_module_t` 上的「进程运行时层回调」，对每个 worker 都会跑一次。事件模块（epoll）正是借这个钩子完成 `epoll_create` 与把监听 socket 加入 epoll 的（u5-l2 详述）。

**注册 channel 读事件**，见 [src/os/unix/ngx_process_cycle.c:929-935](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L929-L935)：把 master 往 worker 写命令用的那个 fd 注册为读事件，handler 设为 `ngx_channel_handler`。`ngx_channel_handler`（[L1000-1085](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1000-L1085)）读到 `NGX_CMD_QUIT` 就置 `ngx_quit=1`、读到 `NGX_CMD_TERMINATE` 就置 `ngx_terminate=1`——这就是 master 通过 channel 指挥 worker 的接收端。

> **对比单进程模式**：`ngx_single_process_cycle`（[L278-332](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L278-L332)）在调试用 `daemon off; master_process off;` 时启用，它把 master 与 worker 的职责合在一个进程里：自己调 `init_process`、自己跑 `ngx_process_events_and_timers`，没有 fork。生产环境不用。

#### 4.3.4 代码实践

**实践目标**：亲眼确认「worker 以非特权用户运行」并定位降权发生在哪一行。

**操作步骤**：

1. 以 root 启动 nginx，配置 `user nobody;`、`worker_processes auto;`。
2. `ps -eo pid,user,comm | grep nginx`，确认 master 是 root、worker 是 nobody。
3. 打开 [src/os/unix/ngx_process_cycle.c:799-829](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L799-L829)，对照 `setgid`/`setuid` 两行。

**需要观察的现象**：master 进程的 USER 列是 root，所有 worker 的 USER 列是 nobody（或你配的用户）。

**预期结果**：现象与 4.3.3 的降权逻辑一致。进一步可思考：为什么 nginx 把 `bind` 80 端口放在 master（root）做，而不是让 worker 自己 bind？因为 1024 以下端口需要特权——master 以 root 打开监听 fd，worker 继承这些已绑定好的 fd 即可，无需特权。这正是「master 管特权资源、worker 管业务」分工的体现。

#### 4.3.5 小练习与答案

**练习 1**：worker 收到 `ngx_quit`（优雅退出）后，为什么先设 `ngx_exiting = 1` 并关掉监听端口，而不是立刻 `exit`？

> **参考答案**：优雅退出的目标是「不再接新连接，但把手头正在处理的连接服务完」。关监听端口 = 不再 accept 新连接；继续留在循环里跑 `ngx_process_events_and_timers` 直到所有定时器（即所有待处理连接的超时监视）都到期（`ngx_event_no_timers_left` 返回 OK），才调用 `ngx_worker_process_exit`。这保证了在途请求不被中断。

**练习 2**：如果某个模块的 `init_process` 回调返回 `NGX_ERROR`，会发生什么？这合理吗？

> **参考答案**：worker 直接 `exit(2)`（[L893-896](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L893-L896)）。因为该 worker 已经处于「半初始化」状态，继续运行不安全；挂掉后 master 会因 `respawn` 把它重新拉起（见 4.1 的 `ngx_reap_children`）。合理：fail fast。

**练习 3**：`ngx_process_events_and_timers(cycle)` 是 worker 每轮循环唯一的核心调用。它如果阻塞很久会怎样？

> **参考答案**：那这个 worker 在此期间无法响应任何其它事件（包括 channel 命令、新连接、定时器），表现上就是「这个 worker 卡住、请求堆积」。这正是 nginx 事件驱动要求「每个 handler 都必须非阻塞、快速」的根本原因——u5 会展开。

---

### 4.4 reload / upgrade / graceful-stop 三大运维操作的协调

这三个操作是本讲的实践任务重点。它们都不是「一个函数搞定」，而是 master 与 worker 之间的一整套「派生 → 移交 → 通知退出」的协调。把它们拆开看，你会发现前面 4.1～4.3 的所有零件都在这里被组装起来。

#### 4.4.1 概念说明

- **reload（`nginx -s reload`，SIGHUP）**：重新读取配置。目标是「不丢连接、不停服」。做法是「先启新 worker、再退旧 worker」——新旧 worker 短暂并存，共享同一批监听 fd，新 worker 接管新连接，旧 worker 把在途请求处理完再走。
- **upgrade（`kill -USR2 <master>`）**：在线热升级 nginx 二进制。做法是「旧 master fork+exec 一个新二进制作为第二个 master」，新 master 继承监听 fd 并起自己的 worker；确认新版本没问题后，让旧 master 优雅退出，留下新版本独自运行。
- **graceful-stop（`nginx -s quit`，SIGQUIT）**：优雅停止。master 让所有 worker 进入优雅退出，自己等它们都走干净后再走。

#### 4.4.2 核心流程

**reload（master 侧，`ngx_reconfigure` 分支）**：

```text
if (ngx_new_binary == 0):              // 正常 reload（当前没有正在进行的二进制升级）
    cycle = ngx_init_cycle(cycle)      // ★ 重新解析配置 + 重新初始化（u3-l2）
    若失败 → 回退到旧 cycle，continue
    ngx_cycle = cycle                  // 整体切换到新 cycle
    重新读 ccf（worker_processes 可能变了）
    ngx_start_worker_processes(... NGX_PROCESS_JUST_RESPAWN)   // 起新 worker（标 just_spawn）
    ngx_start_cache_manager_processes(cycle, 1)
    ngx_msleep(100)                    // 给新 worker 一点时间起来
    live = 1
    ngx_signal_worker_processes(SHUTDOWN)   // 通知旧 worker 优雅退出（跳过 just_spawn 的新 worker）

else (ngx_new_binary != 0):            // 正在升级中又来 reload：特殊处理
    只按旧配置 respawn worker，不重新 init_cycle
    ngx_noaccepting = 0
```

关键点：新 worker 先起，监听端口是 cycle 里早就 `bind` 好的（被新旧 worker 共享，因为 `SO_REUSEPORT` 没开时它们 `accept` 同一个 socket）；旧 worker 在被通知 SHUTDOWN 后进入 4.3.2 的优雅退出流程。`just_spawn` 保证旧 worker 收到退出信号、新 worker 不会。

**upgrade（`ngx_change_binary` + `ngx_new_binary` 跟踪）**：

```text
master 收到 USR2 → ngx_change_binary = 1
master 循环:
    ngx_change_binary = 0
    ngx_new_binary = ngx_exec_new_binary(cycle, ngx_argv)
        ↑ 在 src/core/nginx.c:698
        作用: 把 pid 文件改名（pid → oldbin.pid）、把监听 fd 编进 NGINX 环境变量、fork+execve 新二进制
    此后: 旧 master 与新 master 同时存活，ngx_new_binary 记住新 master 的 pid

（用户验证新版本 OK 后）
    kill -WINCH 旧master  → 旧 worker 停止 accept（ngx_noaccept）但旧 master 还在
    kill -QUIT 旧master   → 旧 master 优雅退出，只剩新 master

（若新二进制崩溃）
    ngx_reap_children 发现死的是 ngx_new_binary → 把 oldbin.pid 改回 pid
    → ngx_new_binary = 0 → 旧 master 恢复为唯一 master（回退）
```

**graceful-stop（`ngx_quit` 分支）**：

```text
master 收到 QUIT → ngx_quit = 1
master 循环:
    ngx_signal_worker_processes(SHUTDOWN)   // 给每个 worker 发 NGX_CMD_QUIT
    ngx_close_listening_sockets(cycle)       // master 自己也关监听，不再接新连接
    continue（回去睡）
后续 SIGCHLD 不断到达 → ngx_reap_children 把 worker 一个个收掉
当 live == 0（worker 全没了）→ ngx_master_process_exit() → master 自己 exit(0)
```

#### 4.4.3 源码精读

**reload 的正常分支**，见 [src/os/unix/ngx_process_cycle.c:223-243](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L223-L243)：

```c
ngx_log_error(NGX_LOG_NOTICE, cycle->log, 0, "reconfiguring");
cycle = ngx_init_cycle(cycle);              // 重新解析配置
if (cycle == NULL) {
    cycle = (ngx_cycle_t *) ngx_cycle;      // 失败 → 回退，继续用旧 cycle
    continue;
}
ngx_cycle = cycle;
ccf = (ngx_core_conf_t *) ngx_get_conf(cycle->conf_ctx, ngx_core_module);
ngx_start_worker_processes(cycle, ccf->worker_processes, NGX_PROCESS_JUST_RESPAWN);
ngx_start_cache_manager_processes(cycle, 1);
ngx_msleep(100);                            // 给新 worker 起步时间
live = 1;
ngx_signal_worker_processes(cycle, ngx_signal_value(NGX_SHUTDOWN_SIGNAL));
```

注意 `ngx_init_cycle` 失败时**不会**让 nginx 挂掉，而是回退到旧 cycle——这就是「reload 配置出错，nginx 仍跑旧配置」的实现（呼应 u3-l2）。

**upgrade 触发处**，见 [src/os/unix/ngx_process_cycle.c:262-266](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L262-L266)：

```c
if (ngx_change_binary) {
    ngx_change_binary = 0;
    ngx_log_error(NGX_LOG_NOTICE, cycle->log, 0, "changing binary");
    ngx_new_binary = ngx_exec_new_binary(cycle, ngx_argv);
}
```

`ngx_exec_new_binary` 的实现（[src/core/nginx.c:698](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L698)）在 u1-l4 已详述：它把当前所有监听 fd 拼成 `NGINX=fd1;fd2;...` 塞进子进程环境，然后 `fork`+`execve` 拉起新二进制。新二进制启动时 `ngx_add_inherited_sockets` 读这个环境变量，把 fd「继承」过来——这就是零停机升级的钥匙。

**升级回退（新二进制崩了）**，见 [src/os/unix/ngx_process_cycle.c:617-637](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L617-L637)：`ngx_reap_children` 收割子进程时，若发现死掉的恰好是 `ngx_new_binary`，就把 `oldbin.pid` 改回 `pid`（恢复旧 master 的「正名」），清零 `ngx_new_binary`，必要时设 `ngx_restart=1` 让旧 master 把 worker 重新拉起来。

**graceful-stop 分支**，见 [src/os/unix/ngx_process_cycle.c:203-209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L203-L209)：

```c
if (ngx_quit) {
    ngx_signal_worker_processes(cycle, ngx_signal_value(NGX_SHUTDOWN_SIGNAL));
    ngx_close_listening_sockets(cycle);
    continue;
}
```

而 `ngx_signal_worker_processes`（[src/os/unix/ngx_process_cycle.c:431-530](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L431-L530)）会优先通过 channel 发 `NGX_CMD_QUIT`（[L446-462](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L446-L462)），channel 不通才退回 `kill` 发原始信号。worker 侧的 `ngx_channel_handler` 收到 `NGX_CMD_QUIT` 后置 `ngx_quit=1`（[L1047-1049](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1047-L1049)），于是 worker 进入 4.3.2 的优雅退出。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：把 reload 与 upgrade 的协调步骤在源码里完整走一遍，写出「关键步骤清单」。

**操作步骤**：

1. **reload 分支追踪**：
   - 在 [src/os/unix/ngx_process_cycle.c:211-244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L211-L244) 找到 `ngx_reconfigure` 分支。
   - 列出正常 reload（`ngx_new_binary == 0`）时的 5 个关键步骤对应的函数调用。
   - 解释 `ngx_msleep(100)`（[L239](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L239)）的作用，以及它为什么是「sleep 而不是同步等待」。

2. **upgrade 分支追踪**：
   - 在 [src/os/unix/ngx_process_cycle.c:262-266](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L262-L266) 找到 `ngx_change_binary` 分支。
   - 跳到 [src/core/nginx.c:698](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L698) 的 `ngx_exec_new_binary`，确认它做了「改名 pid 文件 + 编 NGINX 环境变量 + fork/execve」三件事。
   - 在 [src/os/unix/ngx_process_cycle.c:617-637](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L617-L637) 找到「新二进制崩溃 → 回退」的逻辑，说明它如何把 `oldbin.pid` 改回 `pid`。

3. **把两张「关键步骤清单」写下来**，格式示例：
   - **reload**：① `ngx_init_cycle` 重读配置 → ② 起新 worker（`JUST_RESPAWN`）→ ③ sleep 100ms → ④ 给旧 worker 发 SHUTDOWN → ⑤ 旧 worker 优雅退出。
   - **upgrade**：① 旧 master 收 USR2 → ② `ngx_exec_new_binary` 拉起新 master（带继承 fd）→ ③ 新 master 起自己的 worker → ④ 验证后 WINCH+QUIT 旧 master → ⑤ 仅新版本留存（或新版本崩了则回退）。

**需要观察的现象（若有运行环境）**：执行 `nginx -s reload` 时，`ps` 会瞬间看到「worker 数量翻倍」（新旧并存），随后旧 worker 标题变成 `worker process is shutting down` 并逐个消失。

**预期结果**：你能不看源码复述 reload 与 upgrade 各自的 5 步，并能指出 reload 的「新旧并存」与 upgrade 的「双 master 并存」的区别——前者是同二进制内 worker 换代，后者是两个不同二进制的 master 共存。

**待本地验证**：upgrade 涉及两份二进制文件（`sbin/nginx` 与 `sbin/nginx.old`）和 pid 文件改名，建议先在测试机完整跑一遍再下结论。

#### 4.4.5 小练习与答案

**练习 1**：reload 时如果新配置语法错误，nginx 会怎样？为什么不会挂？

> **参考答案**：`ngx_init_cycle` 返回 NULL，master 走 `cycle = (ngx_cycle_t *) ngx_cycle; continue;`（[L226-229](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L226-L229)），即丢弃新 cycle、继续用旧 cycle 运行。nginx 不会挂，业务不受影响——这正是 reload 的「失败安全」特性。（注意：`nginx -t` 可以在 reload 前预先发现这类错误。）

**练习 2**：为什么 reload 中新 worker 用 `NGX_PROCESS_JUST_RESPAWN`，而正常启动用 `NGX_PROCESS_RESPAWN`？

> **参考答案**：reload 后紧接着要 `ngx_signal_worker_processes(SHUTDOWN)` 通知旧 worker 退出。该函数会跳过 `just_spawn==1` 的进程（4.2.5 练习 1），从而保护刚起的新 worker 不被误杀。普通启动时没有「并存」场景，用普通 `RESPAWN` 即可。

**练习 3**：二进制升级时，新 master 是怎么拿到 80 端口监听 fd 的？它自己 `bind` 了吗？

> **参考答案**：没有自己 `bind`。旧 master 通过 `ngx_exec_new_binary` 把监听 fd 编进 `NGINX=fd1;fd2;...` 环境变量传给新二进制；新 master 启动时 `ngx_add_inherited_sockets` 解析这个变量，直接把这些已经 `bind`+`listen` 好的 fd 纳为己用。这是 u1-l4 详述的「继承套接字」机制，也是升级零停机的根本。

---

## 5. 综合实践

把本讲的 master 循环、worker 循环、reload 协调串起来，做一次「带 debug 日志的 reload 观察实验」。这个任务把 4.1～4.4 全部用上。

1. **编译带 debug 的 nginx**：`./auto/configure --with-debug ... && make`（构建系统见 u1-l2），配置 `error_log logs/error.log debug;` 与 `worker_processes 2;`。
2. **启动后发起一次 reload**：`nginx -s reload`。
3. **在 `logs/error.log` 里按时间顺序找出并标注以下事件**（每条对应源码一个位置）：
   - `signal 1 (SIGHUP) received` —— master 收到 reload 信号（来自 [ngx_signal_handler](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L444-L453)）。
   - `reconfiguring` —— master 进入 reload 分支（[L223](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L223)）。
   - `start worker processes` + 若干 `start worker process <pid>` —— 新 worker 被派生（[L340](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L340)）。
   - `gracefully shutting down` —— 旧 worker 收到 `NGX_CMD_QUIT` 进入优雅退出（[L730-731](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L730-L731)）。
   - `exiting` + `exit` —— 旧 worker 处理完在途连接后退出（[L714](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L714) 与 [ngx_worker_process_exit](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L939-L997)）。
4. **画一张时间线**：横轴是时间，纵轴是「master / 新 worker / 旧 worker」三条泳道，把上面的事件标到对应泳道。你应该能看到「新 worker 先起 → 旧 worker 标 shutting down → 旧 worker 逐个退」的清晰先后关系。
5. **进阶**：把 `worker_processes` 从 2 改成 4 再 reload，对比新旧 worker 总数的变化，验证你对 `JUST_RESPAWN` 与 `ngx_signal_worker_processes` 跳过逻辑的理解。

> 如果没有可运行的 Linux 环境，可改成「纯源码阅读型」实践：把上面 5 个日志关键字在 [src/os/unix/ngx_process_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c) 和 [ngx_process.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c) 里逐一 `grep` 出来，标注每条日志所在函数与触发条件，同样能完成时间线图。

---

## 6. 本讲小结

- master 进程是「信号驱动的状态机」：它长期睡在 `sigsuspend` 里，信号 handler 只置 `sig_atomic_t` 标志位，主循环醒来后按标志位分发动作。master **不处理任何业务连接**。
- 信号到标志位的映射在 `ngx_signal_handler` 里：HUP→`ngx_reconfigure`、USR2→`ngx_change_binary`、QUIT→`ngx_quit`、TERM→`ngx_terminate`、CHLD→`ngx_reap`、USR1→`ngx_reopen`、WINCH→`ngx_noaccept`。
- worker 由 `ngx_start_worker_processes` 经 `ngx_spawn_process` 派生，登记进全局进程表 `ngx_processes[]`；派生类型（`RESPAWN`/`JUST_RESPAWN`/`NORESPAWN`…）决定「崩了是否重启」与 reload 时是否被保护。
- worker 在 `ngx_worker_process_init` 里完成降权、设亲和性、解屏蔽信号、调各模块 `init_process`、注册 channel 读事件，之后才进入「每轮调 `ngx_process_events_and_timers`」的事件循环。
- reload = 先启新 worker（`JUST_RESPAWN`）+ sleep + 通知旧 worker 优雅退出，新旧 worker 共享监听 fd 实现零停机；配置失败则回退旧 cycle 不中断服务。
- upgrade = 旧 master 经 `ngx_exec_new_binary` 拉起带继承 fd 的新 master，双 master 并存，验证后优雅退旧 master；新二进制崩溃则由 `ngx_reap_children` 把 `oldbin.pid` 改回 `pid` 自动回退。

---

## 7. 下一步学习建议

- **向「下」深挖事件循环**：本讲反复出现的 `ngx_process_events_and_timers` 是 worker 的心脏，那是 u5 单元《事件驱动核心》的主题，建议接着读 u5-l1《事件模型总览》。
- **向「旁」补齐进程通信**：本讲对 channel（`socketpair` + `ngx_write_channel`/`ngx_read_channel`）只点到为止，完整收发、`ngx_signal_worker_processes` 的回退到 `kill` 逻辑在 u4-l2《进程派生、信号与通道通信》详述。
- **向「内」补齐共享状态**：`ngx_reap_children` 里「reload 不丢连接」还依赖共享内存在新旧 worker 间复用状态，那是 u4-l3《共享内存、slab 分配器与进程间锁》的内容。
- **配套阅读**：对照 nginx 官方文档的 [Controlling nginx](https://nginx.org/en/docs/control.html)（信号与升级）一节，把本讲的源码视角和官方运维视角互相印证。
