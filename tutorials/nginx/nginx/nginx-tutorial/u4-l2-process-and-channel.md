# 进程派生、信号与通道通信

## 1. 本讲目标

在上一篇（u4-l1）里，我们已经知道 master 是一个「靠信号驱动的状态机」，worker 由 master 派生并在事件循环里干活，两者之间靠 **信号** 和 **channel 通道** 协调。本讲就钻进这两条通信干线的源码实现，读完之后你应当能够：

- 说清 `ngx_spawn_process` 是如何把一次 `fork()` 包装成「带进程表登记、带通道、带 respawn 类型」的派生原语；
- 说清 nginx 是如何注册信号、`ngx_signal_handler` 如何把一个 UNIX 信号翻译成内部 `sig_atomic_t` 标志位（并且 master 与 worker 对同一个信号有不同反应）；
- 说清 `ngx_write_channel` / `ngx_read_channel` 是怎样用一对 `socketpair` + `sendmsg`/`recvmsg` 在进程间既传命令、又传文件描述符的；
- 能够独立追踪一条 `nginx -s quit` 命令：从读 pid 文件、`kill`，到 master 经 channel 通知所有 worker 优雅退出的完整链路。

本讲是理解 reload、graceful stop、binary upgrade 这些运维动作「在内核层面到底发生了什么」的钥匙，也为后面事件循环（u5）和 worker 优雅退出铺路。

## 2. 前置知识

在进入源码前，先确认几个 POSIX 概念。如果你已经熟悉，可以跳到第 3 节。

- **`fork()`**：创建调用进程的一份拷贝（子进程）。父子两份代码随后各自从 `fork()` 的返回点继续往下跑，区别在返回值：父进程拿到子进程 pid，子进程拿到 0。nginx 用它派生 worker。
- **信号（signal）**：内核向进程投递的异步通知。常见如 `SIGTERM`、`SIGQUIT`、`SIGHUP`。进程可以用 `sigaction(2)` 注册一个处理函数，信号到达时中断当前执行流去跑它。信号处理函数里能安全做的事情很有限（很多 libc 函数非「异步信号安全」），所以 nginx 的做法是：**handler 只置一个标志位，真正的工作留给主循环做**。
- **`sig_atomic_t`**：一种「对单个赋值是原子的」整型，专门用来在信号处理函数与主循环之间共享标志位，避免读写撕裂。nginx 的 `ngx_quit`、`ngx_terminate`、`ngx_reap` 等全是这种变量。
- **`socketpair(AF_UNIX, SOCK_STREAM, 0, fd[2])`**：创建一对「互相连接」的 UNIX 字节流套接字。写到 `fd[0]` 的数据能从 `fd[1]` 读出，反之亦然。`fork()` 之后父子各持一端，就成了父子进程间的双向管道。nginx 给它起了个名字叫 **channel**。
- **`sendmsg` / `recvmsg` 与辅助数据（ancillary data, `SCM_RIGHTS`）**：普通的 `send`/`recv` 只能传字节流；而 `sendmsg` 可以附带「辅助数据」，其中 `SCM_RIGHTS` 类型能在两个进程间 **传递一个打开的文件描述符**（内核帮你做引用计数转移，不是传数字）。这是 nginx channel 能传递 `NGX_CMD_OPEN_CHANNEL`（顺带捎一个 fd）的关键。

一句话总结本讲的两条干线：**信号是「人 → master」的命令入口；channel 是「master → worker」的命令入口。** 二者最后都汇拢到一组 `sig_atomic_t` 标志位上，由主循环统一消费。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/os/unix/ngx_process.h` | 进程表条目 `ngx_process_t`、respawn 类型常量、全局变量（`ngx_processes[]`、`ngx_channel`、`ngx_process_slot`）的声明 |
| `src/os/unix/ngx_process.c` | `ngx_spawn_process`（派生）、`ngx_init_signals`（信号注册）、`ngx_signal_handler`（信号处理）、`ngx_os_signal_process`（被 `-s` 子命令调用，给指定 pid 发信号）、`ngx_process_get_status`（收尸） |
| `src/os/unix/ngx_channel.h` | channel 消息结构 `ngx_channel_t` 与四个 channel API 声明 |
| `src/os/unix/ngx_channel.c` | `ngx_write_channel`/`ngx_read_channel`（收发命令，含 fd 传递）、`ngx_add_channel_event`（把 channel 挂进事件循环）、`ngx_close_channel` |
| `src/os/unix/ngx_process_cycle.h` | `NGX_CMD_*` 命令常量、`NGX_PROCESS_*` 进程身份常量、全局 `sig_atomic_t` 标志位声明 |
| `src/os/unix/ngx_process_cycle.c` | master 侧的 `ngx_pass_open_channel`/`ngx_signal_worker_processes`/`ngx_reap_children`（发命令），worker 侧的 `ngx_channel_handler`（收命令）；以及 worker 启动时如何关闭兄弟通道、注册自己的 channel 事件 |
| `src/core/ngx_config.h` | nginx 信号名 → 实际信号编号的宏映射（`NGX_SHUTDOWN_SIGNAL` 等） |
| `src/core/ngx_cycle.c` | `ngx_signal_process`：读 pid 文件、解析 pid、转交 `ngx_os_signal_process` |
| `src/core/nginx.c` | `main()` 中 `-s` 选项解析、`ngx_init_signals` 调用点 |

## 4. 核心概念与源码讲解

### 4.1 进程派生 ngx_spawn_process

#### 4.1.1 概念说明

裸用 `fork()` 派生子进程很简单，但 nginx 需要在 fork 前后做大量记账：

- master 要维护一张 **进程表 `ngx_processes[]`**，记录每个派生出来的进程（pid、状态、它对应的 channel、回调函数、respawn 策略等）。reload、graceful stop、收尸全靠这张表。
- 每个被派生的进程要和 master 之间建一对 **channel 套接字**，作为后续下发命令的物理通道。
- 派生要区分 **respawn 类型**：这个进程崩了要不要自动拉起？这次是「普通派生」还是「刚派生（reload 时需要保护，别立刻又给它发关停信号）」？是不是「脱离的」（如升级时新 master，不该建通道）？

`ngx_spawn_process` 就是把这些都打包好的派生原语。它的签名：

```c
ngx_pid_t ngx_spawn_process(ngx_cycle_t *cycle, ngx_spawn_proc_pt proc,
                            void *data, char *name, ngx_int_t respawn);
```

- `proc`：子进程要执行的函数（如 `ngx_worker_process_cycle`）。
- `data`：传给 `proc` 的参数。
- `name`：进程名字（写日志用，如 `"worker process"`）。
- `respawn`：**既是「复用哪个槽位」（>=0 时表示在指定槽位上重启），又是「派生类型」（负值时是 5 种类型常量之一）**。一参两用，下面细讲。

#### 4.1.2 核心流程

`ngx_spawn_process` 的执行可以拆成五步：

1. **选槽位**。若 `respawn >= 0`，说明是在指定槽位上重启某个崩掉的进程，直接用 `s = respawn`；否则在 `ngx_processes[]` 里找第一个 `pid == -1` 的空槽。
2. **建 channel**（除非 `NGX_PROCESS_DETACHED`）：`socketpair` 建一对 fd，两端都设非阻塞，再给 `channel[0]` 设 `FIOASYNC`（异步信号驱动 I/O）和 `F_SETOWN`（把信号发给 master pid），两端都设 `FD_CLOEXEC`（`exec` 时自动关闭，避免泄漏给升级后的新二进制）。然后把 `channel[1]` 存到全局 `ngx_channel`（这一端会被子进程继承并使用）。
3. **记录槽位**：`ngx_process_slot = s`（子进程靠它知道「我是表里的第几号」）。
4. **fork**。子进程（`case 0`）里修正自己的 `ngx_pid`、`ngx_parent`，然后调用 `proc(cycle, data)`——这一去不再返回（worker 进入事件循环）。父进程继续往下。
5. **填表**：把 pid、回调、名字、按 `respawn` 类型展开的几个标志位（`respawn`/`just_spawn`/`detached`）写进 `ngx_processes[s]`，必要时推进 `ngx_last_process`。

用伪代码浓缩：

```
ngx_spawn_process(proc, data, name, respawn):
    s = (respawn >= 0) ? respawn : 第一个空槽
    if respawn != DETACHED:
        socketpair(channel)              # 建 channel
        nonblocking(channel[0]); nonblocking(channel[1])
        FIOASYNC(channel[0]); F_SETOWN(channel[0], master_pid)
        FD_CLOEXEC(channel[0]); FD_CLOEXEC(channel[1])
        ngx_channel = channel[1]         # 子进程将继承这一端
    ngx_process_slot = s
    pid = fork()
    if pid == 0:                         # 子进程
        ngx_parent = 老 pid; ngx_pid = getpid()
        proc(cycle, data)                # 进入 worker 循环，不返回
    # 父进程：填表
    ngx_processes[s].pid = pid
    按 respawn 类型设标志位
    if s == ngx_last_process: ngx_last_process++
    return pid
```

#### 4.1.3 源码精读

进程表条目 `ngx_process_t` 与全局变量声明：[src/os/unix/ngx_process.h:L22-L36](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L22-L36)。注意 `channel[2]` 这对套接字就挂在每个表项里，`pid`、若干位域（`respawn`/`just_spawn`/`detached`/`exiting`/`exited`）描述进程状态。表容量 `NGX_MAX_PROCESSES` 为 1024：[src/os/unix/ngx_process.h:L47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L47)。

5 种 respawn 类型是负数常量，避免和「槽位编号（>= 0）」混淆：[src/os/unix/ngx_process.h:L49-L53](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.h#L49-L53)。

`ngx_spawn_process` 主体：[src/os/unix/ngx_process.c:L86-L258](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L86-L258)。几个要点对应如下：

- 选槽位（复用 vs 找空位）：[L94-L110](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L94-L110)。表满则返回 `NGX_INVALID_PID`。
- 建 channel：`socketpair` 之后两端设非阻塞：[L117-L143](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L117-L143)。
- `FIOASYNC` + `F_SETOWN(channel[0], ngx_pid)`：让 channel[0] 在有数据可读时给 master 投递 SIGIO，从而把 master 从 `sigsuspend` 里唤醒（worker 若往 channel 写，master 能立刻感知）：[L145-L158](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L145-L158)。
- 两端都 `FD_CLOEXEC`：[L160-L174](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L160-L174)，保证升级（`execve` 新二进制）时旧 channel 不泄漏进新进程。
- `ngx_channel = channel[1]`：[L176](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L176)。注意它在 fork 之前赋值——fork 后子进程继承这个全局变量，正好拿到「自己这端」。
- fork 与子进程分支：[L186-L204](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L186-L204)。子进程设好 `ngx_parent`/`ngx_pid` 后调用 `proc(cycle, data)`。
- 按 respawn 类型填标志位：[L220-L251](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L220-L251)。

5 种 respawn 类型展开后的语义对照：

| 常量 | 数值 | `respawn` | `just_spawn` | `detached` | 含义 |
| --- | --- | --- | --- | --- | --- |
| `NGX_PROCESS_NORESPAWN` | -1 | 0 | 0 | 0 | 普通派生，崩溃不自动重启（如 cache helper） |
| `NGX_PROCESS_JUST_SPAWN` | -2 | 0 | 1 | 0 | 本次「刚派生」，reload 时受保护、不被立即关停 |
| `NGX_PROCESS_RESPAWN` | -3 | 1 | 0 | 0 | 崩溃自动重启（worker 默认） |
| `NGX_PROCESS_JUST_RESPAWN` | -4 | 1 | 1 | 0 | 自动重启 + 刚派生（reload 时新 worker 用） |
| `NGX_PROCESS_DETACHED` | -5 | 0 | 0 | 1 | 脱离的，不建 channel（如升级的新二进制，见 `ngx_execute`） |

> `just_spawn` 的作用下一节就能看到：master 在 reload 时给老 worker 发关停命令会 **跳过** 带 `just_spawn` 的项，避免把刚拉起来的新 worker 又干掉。

#### 4.1.4 代码实践

**实践目标**：把 `ngx_spawn_process` 的「选槽位 → 建 channel → fork → 填表」四步在源码里走一遍，理解同一个函数如何同时服务「首次派生」和「重启某进程」两种场景。

**操作步骤**：

1. 打开 [src/os/unix/ngx_process.c:L86-L258](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L86-L258)。
2. 找到 `respawn >= 0` 分支（[L94-L95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L94-L95)）和 `else` 里找空槽的 `for`（[L98-L102](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L98-L102)）。思考：master 在 reload 后要「重启第 3 号 worker」，调用方会怎么传 `respawn`？答案在第 4.4 节的 `ngx_reap_children` 里。
3. 找到 `ngx_channel = ngx_processes[s].channel[1]`（[L176](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L176)）和 `ngx_process_slot = s`（[L183](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L183)），记住它们是「子进程在 fork 之前就抄到自己地址空间的全局变量」——这是子进程日后知道「我是谁、用哪端 channel」的依据。

**需要观察的现象 / 预期结果**：你应该能解释一句话——**「fork 之前在父进程里给全局变量赋值，等于在给即将出生的子进程预设身份。」** 这是 nginx 进程模型里反复出现的手法。本实践为源码阅读型，无需运行；若想运行，可在 4.4 节统一动手。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `F_SETOWN` 把 channel[0] 的属主设成 `ngx_pid`（master 自己），而不是设成将来的子进程 pid？

**答案**：channel[0] 留在 master 手里（master 往 channel[0] 写 = 给 worker 发命令；channel[1] 被子进程继承）。`FIOASYNC`+`F_SETOWN` 是「信号驱动 I/O」：当 **对端（worker）往 channel[1] 写了数据** 使 channel[0] 变可读时，内核要给「属主」发 SIGIO 通知。这个属主应该是 channel[0] 的持有者 master，所以设成 master pid，把 master 从 `sigsuspend` 唤醒。

**练习 2**：`ngx_processes[s].proc` / `.data` 只在 `respawn < 0`（即「首次派生」）时才被填写（见 [L215-L217](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L215-L217)），为什么重启某个进程时不用再填？

**答案**：因为重启是在 **同一个槽位 s** 上复活一个同职责的进程，它要用的是当初首次派生时记录的同一份 `proc`/`data`（同一个 worker 循环函数）。这些信息从未被清掉，重启分支（`respawn >= 0`）直接 `return pid`（[L211-L213](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L211-L213)），复用旧记录即可。

---

### 4.2 信号注册与处理 ngx_signal_handler

#### 4.2.1 概念说明

nginx 在启动早期会调一次 `ngx_init_signals`，把一张 **信号表 `signals[]`** 里列出的每个信号都用 `sigaction` 注册成同一个处理函数 `ngx_signal_handler`。这张表把三样东西绑在一起：

- `signo`：实际的信号编号（如 `SIGHUP`）；
- `signame`：可读名字（如 `"SIGHUP"`，写日志用）；
- `name`：**运维命令名**（如 `"reload"`）——这正是 `nginx -s reload` / `-s quit` / `-s stop` / `-s reopen` 那个字符串的来源。

`ngx_signal_handler` 收到信号后 **不做真正的业务**，只做两件事：根据「当前进程身份（master/worker）+ 信号编号」把某个 `sig_atomic_t` 标志位置 1；若是 `SIGCHLD`，顺手调 `ngx_process_get_status` 用 `waitpid` 收尸。真正的处理在主循环里看到标志位后才做。

为什么要在 handler 里区分 master 和 worker？因为 **同一个信号对不同身份的进程含义不同**。例如 `SIGHUP`（reload）对 master 意味着「重新读配置」，对 worker 则毫无意义（worker 不该自己 reload），handler 里直接 `ignore` 掉。

#### 4.2.2 核心流程

信号注册流程（启动期，一次性）：

```
ngx_init_signals(log):
    for sig in signals[]:
        sa.sa_sigaction = ngx_signal_handler   # 或 SIG_IGN（如 SIGPIPE）
        sa.sa_flags = SA_SIGINFO
        sigaction(sig->signo, sa)
```

信号到达后的处理流程：

```
ngx_signal_handler(signo):
    在 signals[] 里查到这个 signo 对应的表项（拿 signame 写日志）
    ngx_time_sigsafe_update()        # 信号安全地刷新一下缓存时间
    switch (ngx_process):            # 按当前进程身份分派
      case MASTER/SINGLE:
        SIGHUP    -> ngx_reconfigure = 1
        SIGUSR1   -> ngx_reopen = 1
        SIGQUIT   -> ngx_quit = 1
        SIGTERM/INT -> ngx_terminate = 1
        SIGWINCH  -> ngx_noaccept = 1
        SIGUSR2   -> ngx_change_binary = 1
        SIGCHLD   -> ngx_reap = 1
      case WORKER/HELPER:
        SIGQUIT   -> ngx_quit = 1
        SIGTERM/INT -> ngx_terminate = 1
        SIGUSR1   -> ngx_reopen = 1
        SIGHUP/SIGUSR2/SIGIO -> ignore
    if signo == SIGCHLD: ngx_process_get_status()   # waitpid 收尸
```

> 注意：上面的「SIGQUIT/SIGTERM」是默认 Linux 平台的映射；在 `NGX_LINUXTHREADS` 下 reopen/changebin 会换成别的信号。具体见下方源码。

#### 4.2.3 源码精读

信号名到编号的映射在配置头里：[src/core/ngx_config.h:L60-L71](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h#L60-L71)。nginx 用宏 `ngx_signal_value(NGX_RECONFIGURE_SIGNAL)` 经 `SIG##n` 拼成真正的 `SIGHUP`（宏链 [L54-L55](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h#L54-L55)）。默认（非 LINUXTHREADS）映射关系：

| nginx 语义常量 | 实际信号 | 运维命令 `name` |
| --- | --- | --- |
| `NGX_RECONFIGURE_SIGNAL` | `SIGHUP` | `"reload"` |
| `NGX_REOPEN_SIGNAL` | `SIGUSR1` | `"reopen"` |
| `NGX_NOACCEPT_SIGNAL` | `SIGWINCH` | `""` |
| `NGX_TERMINATE_SIGNAL` | `SIGTERM` | `"stop"` |
| `NGX_SHUTDOWN_SIGNAL` | `SIGQUIT` | `"quit"` |
| `NGX_CHANGEBIN_SIGNAL` | `SIGUSR2` | `""` |

信号表本体：[src/os/unix/ngx_process.c:L39-L83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L39-L83)。重点看几条：reload 对应 `SIGHUP`（[L40-L43](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L40-L43)）、stop 对应 `SIGTERM`（[L55-L58](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L55-L58)）、quit 对应 `SIGQUIT`（[L60-L63](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L60-L63)）、reopen 对应 `SIGUSR1`（[L45-L48](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L45-L48)）。`SIGPIPE` 的 handler 是 `NULL`，故在 `ngx_init_signals` 里会被设成 `SIG_IGN`（[L80](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L80)）——写已关闭的 socket 不应让进程被杀。

`ngx_init_signals`：[src/os/unix/ngx_process.c:L284-L315](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L284-L315)。有 handler 就用 `SA_SIGINFO` 三参版本（这样 handler 能拿到 `siginfo->si_pid`，日志里能写「信号来自哪个 pid」），否则 `SIG_IGN`。它在 `main()` 里被调用：[src/core/nginx.c:L345-L347](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L345-L347)。

`ngx_signal_handler`：[src/os/unix/ngx_process.c:L318-L467](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L318-L467)。master/single 分支见 [L342-L406](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L342-L406)：`SIGQUIT` 置 `ngx_quit=1`（[L346-L349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L346-L349)）、`SIGTERM`/`SIGINT` 置 `ngx_terminate=1`（[L351-L355](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L351-L355)）、`SIGHUP` 置 `ngx_reconfigure=1`（[L364-L367](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L364-L367)）、`SIGCHLD` 置 `ngx_reap=1`（[L401-L403](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L401-L403)）。worker/helper 分支见 [L408-L442](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L408-L442)，注意 worker 对 `SIGHUP`/`SIGUSR2`/`SIGIO` 是 `action=", ignoring"`（[L434-L438](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L434-L438)）——只记日志、不动标志位。末尾若是 `SIGCHLD` 则调收尸函数：[L462-L464](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L462-L464)。

所有这些 `sig_atomic_t` 标志位声明在 [src/os/unix/ngx_process_cycle.h:L49-L58](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h#L49-L58)（`ngx_quit`、`ngx_terminate`、`ngx_reap`、`ngx_reconfigure`、`ngx_reopen`、`ngx_change_binary`、`ngx_noaccept` 等），它们是 handler 与主循环之间的唯一契约。

收尸函数 `ngx_process_get_status`：[src/os/unix/ngx_process.c:L470-L561](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L470-L561)。它用 `waitpid(-1, &status, WNOHANG)` 循环非阻塞地把所有已死子进程回收（[L482-L518](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L482-L518)），把对应表项的 `exited` 置 1（[L524-L531](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L524-L531)），并区分「正常退出码」与「被信号杀死」分别记日志（[L533-L549](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L533-L549)）。

最后，`-s` 子命令最终落到 `ngx_os_signal_process`：它按「命令名字符串」查表，对指定 pid 执行 `kill`：[src/os/unix/ngx_process.c:L631-L648](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L631-L648)。

#### 4.2.4 代码实践

**实践目标**：理解「字符串命令 → 信号 → 标志位」这条映射链的源头。

**操作步骤**：

1. 打开信号表 [src/os/unix/ngx_process.c:L39-L83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L39-L83)，找到你日常用的 4 个命令名：`reload`、`reopen`、`stop`、`quit`。
2. 对照 [src/core/ngx_config.h:L60-L71](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_config.h#L60-L71)，把每个命令名 → nginx 语义常量 → 实际信号编号 → 在 master handler 里置的标志位，填成一张表。

**需要观察的现象 / 预期结果**：你会得到类似 `quit → NGX_SHUTDOWN_SIGNAL → SIGQUIT → ngx_quit=1` 的四列对应关系。这张表是后面 4.4 节「追踪 `nginx -s quit`」的解码本。

**待本地验证**：若想跑起来验证，可在编译好的 nginx 上用 `kill -QUIT $(cat logs/nginx.pid)` 与 `nginx -s quit` 对比，两者应等价（都是给 master 发 `SIGQUIT`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_signal_handler` 里要调用 `ngx_time_sigsafe_update()`（[L336](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L336)）？

**答案**：handler 里要写日志（见 [L444-L453](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L444-L453)），日志需要时间戳；而正常的 `ngx_time_update` 不是异步信号安全的。`ngx_time_sigsafe_update` 是专为信号上下文准备的「安全刷新缓存时间」版本（回顾 u2-l5 的时间缓存机制），保证 handler 里打日志用的是近似正确的时间。

**练习 2**：worker 收到 `SIGHUP` 会怎样？

**答案**：看 worker/helper 分支 [L434-L438](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L434-L438)：只把 `action` 设成 `", ignoring"` 写一行日志，**不置任何标志位**。reload 是 master 的职责，worker 不参与；这也是为什么 `nginx -s reload` 只给 master 发一次 `SIGHUP` 就够了。

---

### 4.3 通道通信 ngx_write_channel / ngx_read_channel

#### 4.3.1 概念说明

master 用信号告诉 worker「有事要做」，但信号能携带的信息量极少（只有信号编号）。nginx 需要 master 向 worker 传递 **结构化命令**——比如「第 5 号 worker 刚出生，它的 pid 是 X、channel 是 fd Y，你跟它也连上」。这件事信号做不到，于是有了 **channel**。

channel 建立在 4.1 节那对 `socketpair` 之上。但 nginx 没有直接用 `send`/`recv` 传字节流，而是用 `sendmsg`/`recvmsg`，目的是能携带 **辅助数据（ancillary data）**。具体地，`SCM_RIGHTS` 类型的辅助数据可以在两个进程之间 **传递一个已打开的文件描述符**——内核会把这个 fd 在接收进程里「重新注册」一个等效的新 fd。这正是 `NGX_CMD_OPEN_CHANNEL` 命令能捎带一个 fd 的关键：master 不只是告诉 worker「有个新兄弟」，而是直接把那个兄弟的 channel 端 fd 通过内核递过去。

channel 上流动的消息是一个固定结构 `ngx_channel_t`：

```c
typedef struct {
    ngx_uint_t  command;   // NGX_CMD_OPEN_CHANNEL / CLOSE_CHANNEL / QUIT / TERMINATE / REOPEN
    ngx_pid_t   pid;       // 相关进程 pid
    ngx_int_t   slot;      // 相关进程在 ngx_processes[] 里的槽位
    ngx_fd_t    fd;        // 要传递的 fd（仅 OPEN_CHANNEL 用，由辅助数据携带真正的 fd）
} ngx_channel_t;
```

命令常量定义在 [src/os/unix/ngx_process_cycle.h:L16-L20](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h#L16-L20)：

| 常量 | 值 | 含义 |
| --- | --- | --- |
| `NGX_CMD_OPEN_CHANNEL` | 1 | 通知：有个新进程加入了，带上它的 pid/slot/fd |
| `NGX_CMD_CLOSE_CHANNEL` | 2 | 通知：某个进程退出，把对应的 channel 关掉 |
| `NGX_CMD_QUIT` | 3 | 优雅关停（对应 `SIGQUIT`） |
| `NGX_CMD_TERMINATE` | 4 | 立即终止（对应 `SIGTERM`） |
| `NGX_CMD_REOPEN` | 5 | 重新打开日志（对应 `SIGUSR1`） |

#### 4.3.2 核心流程

**发送端** `ngx_write_channel(s, ch, size, log)`：

```
准备一个 iovec，把 ch 这个结构体作为普通数据
if ch->fd != -1:
    再准备一段辅助数据（cmsg），类型 SCM_RIGHTS，里面拷一份 ch->fd
    # 这样接收方会通过内核拿到一个等效的新 fd
sendmsg(s, msg)          # 一次系统调用同时送出「命令结构 + fd」
```

**接收端** `ngx_read_channel(s, ch, size, log)`：

```
recvmsg(s, msg)          # 同时取回「普通数据 + 辅助数据」
把普通数据回填到 ch
if ch->command == NGX_CMD_OPEN_CHANNEL:
    从辅助数据（SCM_RIGHTS）里取出内核帮我们注册的新 fd，存进 ch->fd
返回收到的字节数
```

注意 `NGX_CMD_OPEN_CHANNEL` 是唯一携带 fd 的命令；其他命令在发送时 `ch->fd` 设为 -1，于是不附加辅助数据（见 `ngx_write_channel` [L29-L32](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L29-L32)）。

#### 4.3.3 源码精读

`ngx_channel_t` 结构定义：[src/os/unix/ngx_channel.h:L17-L22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.h#L17-L22)。

`ngx_write_channel`：[src/os/unix/ngx_channel.c:L13-L92](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L13-L92)。关键点：

- 辅助数据用 `union { struct cmsghdr cm; char space[CMSG_SPACE(sizeof(int))]; }` 保证对齐与容量（[L24-L27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L24-L27)）。
- 仅当 `ch->fd != -1` 才填辅助数据：[L29-L54](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L29-L54)，`cmsg_type = SCM_RIGHTS`（[L41](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L41)）就是「传递访问权」的意思。注释里特意说明为何用 `ngx_memcpy` 而非 `*(int*)CMSG_DATA(...)` 赋值——为了规避 gcc 严格别名优化告警（[L43-L52](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L43-L52)）。
- 命令结构作为普通数据放在 iovec：[L71-L72](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L71-L72)。
- 一次 `sendmsg`：[L79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L79)。`EAGAIN` 时返回 `NGX_AGAIN`（非阻塞 socket 的背压信号，回顾 u2-l4）。

`ngx_read_channel`：[src/os/unix/ngx_channel.c:L95-L195](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L95-L195)。关键点：

- `recvmsg`：[L128](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L128)。`n == 0` 表示对端关闭（[L140-L143](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L140-L143)）；收到的字节数小于一个 `ngx_channel_t` 视为出错（[L145-L149](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L145-L149)）。
- 只有 `NGX_CMD_OPEN_CHANNEL` 才去解析辅助数据、取出 fd 存进 `ch->fd`：[L153-L173](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L153-L173)。
- 若发生截断（`MSG_TRUNC|MSG_CTRUNC`）记告警：[L175-L178](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L175-L178)。

把 channel 接入事件循环的 `ngx_add_channel_event`：[src/os/unix/ngx_channel.c:L198-L240](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L198-L240)。它把 fd 包装成一个 `ngx_connection_t`、把读事件的 handler 设成调用方指定的函数（worker 侧是 `ngx_channel_handler`），并打上 `c->read->channel = 1` 标记（[L219-L220](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L219-L220)）——这个标记在 worker 退出前检查「是否有遗留 socket」时用来识别「这是 channel、不是业务连接，可以忽略」（见 [ngx_worker_process_exit 的 L957](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L957)）。

#### 4.3.4 代码实践

**实践目标**：理解「为什么 nginx 非要用 `sendmsg` 而不是普通 `write`」。

**操作步骤**：

1. 打开 [src/os/unix/ngx_channel.c:L29-L54](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L29-L54)，观察辅助数据只在 `ch->fd != -1` 时才附加。
2. 在 master 发命令的三个函数里（见 4.4 节）分别找到它们给 `ch.fd` 设的值：`ngx_pass_open_channel` 设成一个真实 fd，`ngx_signal_worker_processes` 设成 -1，`ngx_reap_children` 设成 -1。思考：为什么只有「通知新进程加入」需要传 fd，而「关停/重开日志」不需要？

**需要观察的现象 / 预期结果**：你会得出结论——fd 传递的代价（准备 cmsg、内核引用计数转移）并不便宜，nginx 只在真正需要时（worker 之间也要互通 channel 时）才用 `SCM_RIGHTS`。普通命令就是一条 16 字节左右的结构体消息。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_write_channel` 返回 `NGX_AGAIN` 表示什么？调用方（master 侧）怎么处理？

**答案**：`sendmsg` 因 socket 缓冲区满而返回 `EAGAIN`（非阻塞 socket），`ngx_write_channel` 把它翻译成 `NGX_AGAIN`（[L83-L85](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L83-L85)）。master 侧的调用点（如 `ngx_pass_open_channel` [L423](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L423) 留有 `/* TODO: NGX_AGAIN */` 注释）目前并未重试——因为 channel 消息极小、worker 持续在事件循环里读，实践中几乎不会满。

**练习 2**：`ngx_read_channel` 为什么在校验 `n < sizeof(ngx_channel_t)` 后直接返回 `NGX_ERROR`（[L145-L149](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_channel.c#L145-L149)）？

**答案**：channel 是面向「整条 `ngx_channel_t` 消息」的，`sendmsg` 一次写一条、`recvmsg` 一次读一条（UNIX 域 SOCK_STREAM 上辅助数据与数据边界对齐）。收到不足一条说明协议错乱，没有「半条消息可处理」的语义，只能当错误丢弃并关闭这条 channel（见 4.4 节 handler 里 `n == NGX_ERROR` 时 `ngx_close_connection`）。

---

### 4.4 命令在 master/worker 之间的流转

#### 4.4.1 概念说明

有了 spawn（4.1）、信号（4.2）、channel（4.3）三块积木，本节把它们拼起来，看 master 如何 **下发命令**、worker 如何 **接收并执行命令**。这一节是上一讲 u4-l1「master 是信号驱动的状态机」的具体落地：master 主循环看到 `ngx_quit` 等标志位后，并不会直接去关 worker，而是先 `ngx_signal_worker_processes` 把意图通过 channel 告诉 worker，再由 worker 各自优雅退出。

master 侧有三个发命令的函数：

- `ngx_pass_open_channel`：每派生一个新进程（worker / cache manager）后调用，告诉 **所有其他已存在进程**「新兄弟来了，记住它的 pid/slot/fd」。这是 worker 之间也能互通的根基。
- `ngx_signal_worker_processes(signo)`：master 想让 worker 做某件事时调用。它先把信号翻译成对应命令（`SIGQUIT→NGX_CMD_QUIT`、`SIGTERM→NGX_CMD_TERMINATE`、`SIGUSR1→NGX_CMD_REOPEN`），优先走 channel 发给每个 worker；**只有 channel 发送失败时才退化成 `kill` 信号**。
- `ngx_reap_children`：master 在 `ngx_reap`（有子进程死了）时调用，`waitpid` 收尸后，对每个已死进程发 `NGX_CMD_CLOSE_CHANNEL` 让其他存活进程清掉对应 channel。

worker 侧只有一个入口：`ngx_channel_handler`。它在 worker 启动时被注册成 channel 读事件的 handler，epoll 一旦报告 channel 可读就触发，循环 `ngx_read_channel` 读出每条命令，按 `command` 分派：`NGX_CMD_QUIT` 置 `ngx_quit=1`、`NGX_CMD_TERMINATE` 置 `ngx_terminate=1`、`NGX_CMD_REOPEN` 置 `ngx_reopen=1`、`NGX_CMD_OPEN_CHANNEL`/`NGX_CMD_CLOSE_CHANNEL` 维护本进程内的 `ngx_processes[]` 镜像。

#### 4.4.2 核心流程

**master 启动一个新 worker 的命令广播**：

```
ngx_start_worker_processes(...):
    for 每个要起的 worker:
        ngx_spawn_process(worker_cycle)          # 派生 + 建 channel
        ngx_pass_open_channel(cycle)             # 向所有已有进程广播 OPEN_CHANNEL
```

**master 想让 worker 优雅退出**（master 主循环里看到 `ngx_quit`）：

```
ngx_master_process_cycle 主循环:
    sigsuspend()                  # 等信号
    if ngx_quit:
        ngx_signal_worker_processes(SIGQUIT)     # 优先 channel 发 NGX_CMD_QUIT
        ngx_close_listening_sockets()            # master 自己关监听端口（不再接新连接）
```

`ngx_signal_worker_processes` 内部：

```
把 signo 翻译成 command（SIGQUIT->NGX_CMD_QUIT 等），ch.fd = -1
for 每个非 detached、非 just_spawn、非已 exiting 的子进程:
    if command != 0:
        if ngx_write_channel(它的 channel[0], &ch) == NGX_OK:
            标记它 exiting = 1
            continue
    # channel 发送失败才退化成 kill 信号
    kill(它的 pid, signo)
    标记它 exiting = 1
```

**worker 接收命令**：

```
ngx_channel_handler (epoll 报告 channel 可读时触发):
    for:
        n = ngx_read_channel(channel_fd, &ch)
        if n == NGX_ERROR: 关闭 channel 连接; return
        if n == NGX_AGAIN: return          # 暂时没有完整消息
        switch ch.command:
          NGX_CMD_QUIT     -> ngx_quit = 1
          NGX_CMD_TERMINATE-> ngx_terminate = 1
          NGX_CMD_REOPEN   -> ngx_reopen = 1
          NGX_CMD_OPEN_CHANNEL -> 记下 ch.slot 的 pid 与 channel[0]=ch.fd
          NGX_CMD_CLOSE_CHANNEL-> 关闭 ch.slot 的 channel[0]
```

注意一个精妙之处：worker 自己的 `ngx_processes[]` 表本来只在 fork 时继承了 master 当时的快照。fork 之后新出生的兄弟进程，worker 是通过接收 `NGX_CMD_OPEN_CHANNEL` 来 **增量更新** 自己这张表的（`ngx_pass_open_channel` 把新兄弟的 fd 通过 `SCM_RIGHTS` 递过来）。这就是为什么 channel 必须具备「传 fd」的能力。

#### 4.4.3 源码精读

master 广播新进程：`ngx_pass_open_channel` [src/os/unix/ngx_process_cycle.c:L396-L428](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L396-L428)。命令是 `NGX_CMD_OPEN_CHANNEL`，`ch.fd` 设为该新进程的 `channel[0]`（[L403-L406](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L403-L406)）——这个 fd 会通过 `SCM_RIGHTS` 真正传递给接收方。循环跳过自己和无效槽位，给其余每个进程 `ngx_write_channel`（[L408-L427](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L408-L427)）。

master 通知 worker：`ngx_signal_worker_processes` [src/os/unix/ngx_process_cycle.c:L431-L530](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L431-L530)。命令翻译 switch（[L446-L462](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L446-L462)）只认 `SHUTDOWN`/`TERMINATE`/`REOPEN` 三种，其余 `command=0`。遍历子进程时跳过 detached、pid==-1、just_spawn、exiting+shutdown 的项（[L481-L494](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L481-L494)）。优先 channel（[L496-L507](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L496-L507)），失败才 `kill`（[L512-L524](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L512-L524)）。注意只有非 `REOPEN` 才标 `exiting=1`（reopen 不是退出，[L501-L503](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L501-L503)、[L526-L528](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L526-L528)）。

master 主循环里 graceful quit 的分支：[src/os/unix/ngx_process_cycle.c:L203-L209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L203-L209)——`ngx_signal_worker_processes(SHUTDOWN)` 之后立刻 `ngx_close_listening_sockets`，让 master 不再接新连接。

master 收尸与清 channel：`ngx_reap_children` [src/os/unix/ngx_process_cycle.c:L534-L608](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L534-L608)。命令是 `NGX_CMD_CLOSE_CHANNEL`（[L543](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L543)），对已退出的槽位关闭本地的 channel（[L566](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L566)），并向其余存活进程广播（[L588-L589](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L588-L589)）。

worker 侧启动时整理 channel：先关掉所有兄弟进程的 `channel[1]` 端（worker 只需要通过自己的 `channel[1]` 跟 master 说话），再关掉自己的 `channel[0]`（worker 持有 `channel[1]`，监听它）：[src/os/unix/ngx_process_cycle.c:L900-L923](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L900-L923)。随后把自己的 channel 注册成读事件，handler 设为 `ngx_channel_handler`：[L929-L935](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L929-L935)。

worker 侧接收与分派：`ngx_channel_handler` [src/os/unix/ngx_process_cycle.c:L1001-L1085](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1001-L1085)。循环 `ngx_read_channel`（[L1018](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1018)），出错就摘掉连接（[L1022-L1030](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1022-L1030)），`NGX_AGAIN` 就返回等下次（[L1038-L1040](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1038-L1040)），命令分派 switch 见 [L1045-L1083](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1045-L1083)。

#### 4.4.4 代码实践

**实践目标**：把「master 发 `NGX_CMD_QUIT` → worker 收到后置 `ngx_quit=1`」这条链在源码里对上号。

**操作步骤**：

1. 打开 `ngx_signal_worker_processes` 的命令翻译（[L446-L462](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L446-L462)），确认 `SIGQUIT` → `NGX_CMD_QUIT`。
2. 打开 worker 的 `ngx_channel_handler` 分派（[L1045-L1083](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1045-L1083)），确认 `NGX_CMD_QUIT` → `ngx_quit = 1`（[L1047-L1049](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1047-L1049)）。
3. 思考：worker 的 `ngx_quit` 被置 1 后，谁来消费它？答案是 worker 的 `ngx_worker_process_cycle` 主循环——它会在事件循环每一轮检查 `ngx_quit`/`ngx_terminate`，从而停止 accept、优雅收尾。这部分代码在下一篇 u5（事件循环）里细讲。

**需要观察的现象 / 预期结果**：你应当能用一句话讲清「为什么 master 明明可以直接 `kill(worker, SIGQUIT)`，却还要先绕一道 channel？」——因为 channel 命令是 **结构化的、与信号语义解耦的**，且 `NGX_CMD_OPEN_CHANNEL` 这类命令信号根本表达不了；统一走 channel 让 master→worker 的控制平面只有一条管道、一种格式。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_signal_worker_processes` 里 `if (ngx_processes[i].just_spawn)` 分支（[L485-L488](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L485-L488)）的作用是什么？

**答案**：reload 时，master 先用 `NGX_PROCESS_JUST_RESPAWN` 拉起一批新 worker（它们的 `just_spawn=1`），再给老 worker 发关停命令。这个分支保证 **刚出生的新 worker 不会被同一次 `ngx_signal_worker_processes` 误杀**——遇到 `just_spawn` 就把它清零并 `continue` 跳过。结合 4.1 节的 `just_spawn` 标志，这就是「先启新、再退旧」的安全实现。

**练习 2**：worker 收到 `NGX_CMD_OPEN_CHANNEL` 后做了什么（[L1059-L1067](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1059-L1067)）？为什么必须这么做？

**答案**：它把消息里 `ch.slot` 对应表项的 `pid` 和 `channel[0]` 更新成 `ch.pid` 与 `ch.fd`（后者是 master 通过 `SCM_RIGHTS` 递过来的真实 fd）。必须这么做，是因为 worker 的 `ngx_processes[]` 是 fork 时的快照，fork 之后才出生的兄弟进程它一开始并不知道；只有靠接收 `NGX_CMD_OPEN_CHANNEL` 增量补齐，worker 才能在需要时（例如某些需要 worker 间协作的场景）找到正确的兄弟 channel。

---

## 5. 综合实践

**任务**：追踪一条 `nginx -s quit` 命令，从你在终端敲下它，到所有 worker 优雅退出、master 自己退出的完整路径。把沿途经过的关键函数与代码行号串起来。

**完整调用链**（请对照源码逐段确认）：

1. **命令行解析**。`main()` 调 `ngx_get_options`，`-s quit` 被解析：`ngx_signal = "quit"`，并把进程身份设成 `NGX_PROCESS_SIGNALLER`（一个「只发信号就退出」的临时身份）：[src/core/nginx.c:L910-L929](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L910-L929)。
2. **短路分支**。`main()` 发现 `ngx_signal` 非空，立即调 `ngx_signal_process` 后 return，不会进入任何进程循环：[src/core/nginx.c:L329-L330](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L330)。
3. **读 pid 文件**。`ngx_signal_process` 打开 `logs/nginx.pid`，读出 master 的 pid：[src/core/ngx_cycle.c:L1096-L1145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1096-L1145)（`ngx_atoi` 解析 pid 是 u2-l2 学过的知识）。
4. **名字 → 信号 → kill**。转交 `ngx_os_signal_process(cycle, "quit", pid)`，它查表得 `"quit"` 对应 `SIGQUIT`，对 master 执行 `kill(pid, SIGQUIT)`：[src/os/unix/ngx_process.c:L631-L648](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L631-L648)。**这个临时 signaller 进程的使命到此结束，退出。**
5. **master 收信号**。运行中的 master 进程被 `SIGQUIT` 中断，`ngx_signal_handler` 在 master 分支把 `ngx_quit = 1`：[src/os/unix/ngx_process.c:L346-L349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process.c#L346-L349)。
6. **master 主循环消费 `ngx_quit`**。master 从 `sigsuspend` 醒来，进入 `if (ngx_quit)` 分支：调用 `ngx_signal_worker_processes(SIGQUIT)` 并 `ngx_close_listening_sockets`：[src/os/unix/ngx_process_cycle.c:L203-L209](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L203-L209)。
7. **master 通过 channel 通知 worker**。`ngx_signal_worker_processes` 把 `SIGQUIT` 翻成 `NGX_CMD_QUIT`，给每个 worker `ngx_write_channel`：[src/os/unix/ngx_process_cycle.c:L448-L449](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L448-L449)、[L497-L498](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L497-L498)。失败才退化成 `kill`（[L512](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L512)）。
8. **worker 收 channel 命令**。worker 的 `ngx_channel_handler` 被 epoll 触发，`ngx_read_channel` 读出 `NGX_CMD_QUIT`，把 worker 自己的 `ngx_quit = 1`：[src/os/unix/ngx_process_cycle.c:L1047-L1049](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L1047-L1049)。
9. **worker 优雅退出**。worker 事件循环看到 `ngx_quit`，停止接受新连接、把已有连接处理完后退出（具体在 u5 讲）。
10. **master 收尸**。每个 worker 退出触发 master 收到 `SIGCHLD` → `ngx_reap = 1` → `ngx_reap_children` 用 `waitpid` 收掉，并广播 `NGX_CMD_CLOSE_CHANNEL`：[src/os/unix/ngx_process_cycle.c:L534-L608](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L534-L608)。
11. **master 自己退出**。当 `live == 0`（没有存活子进程）且 `ngx_quit` 仍为真，master 调 `ngx_master_process_exit`：[src/os/unix/ngx_process_cycle.c:L177-L179](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L177-L179)。

**动手验证（待本地验证）**：

- 编译并启动 nginx，用 `tail -f logs/error.log` 观察日志；
- 执行 `nginx -s quit`，应在日志里依次看到：
  - signaller 进程的 `signal process started`；
  - master 的 `signal 3 (SIGQUIT) received`；
  - 每个 worker 的 `gracefully shutting down`；
  - 最终 master 的退出日志。
- 可对照 `kill -QUIT $(cat logs/nginx.pid)` 验证它与 `nginx -s quit` 等价（区别仅在于「谁来发这次 kill」：`-s` 是新起的 signaller 进程发，手敲 `kill` 是 shell 发）。

## 6. 本讲小结

- nginx 用 **两条平行的控制平面** 协调多进程：「信号」承载「人 → master」的命令（信息量小），「channel」承载「master → worker」的命令（结构化、可传 fd）。
- `ngx_spawn_process` 把裸 `fork()` 包装成带 **进程表登记 + socketpair channel + respawn 类型** 的派生原语；`respawn` 参数一值两用：正数是「在此槽位重启」，负数是 5 种派生类型。
- `ngx_signal_handler` **只置 `sig_atomic_t` 标志位、不做业务**，并按进程身份（master/worker）对同一信号做不同反应；真正的处理交给主循环。`SIGCHLD` 还会顺手 `waitpid` 收尸。
- 信号 → 标志位的映射源头是一张 `signals[]` 表，它同时把运维命令名（`reload`/`quit`/`stop`/`reopen`）与信号编号、handler 绑在一起；`ngx_os_signal_process` 负责 `nginx -s <name>` 时的 `kill`。
- channel 用 `sendmsg`/`recvmsg` + `SCM_RIGHTS` 辅助数据，能在传「命令结构 `ngx_channel_t`」的同时 **传递一个文件描述符**，这是 `NGX_CMD_OPEN_CHANNEL` 让 worker 增量感知新兄弟进程的关键。
- master 优先用 channel 下发 `NGX_CMD_QUIT/TERMINATE/REOPEN`，channel 失败才退化成 `kill`；worker 在 `ngx_channel_handler` 里把命令翻译回 `ngx_quit` 等标志位，于是「信号」与「channel」最终汇拢到 **同一组标志位**，由各自的主循环统一消费。

## 7. 下一步学习建议

- **下一篇 u5-l1（事件模型总览）**：worker 把自己的 channel 注册成读事件后，是怎么被 epoll 调度到 `ngx_channel_handler` 的？这就要进入 `ngx_event_t` 与事件主循环了。
- **u5-l5（事件主循环）**：`ngx_process_events_and_timers` 是 worker 每一轮跑的核心，它会检查 `ngx_quit`/`ngx_terminate`，与本讲「worker 收到 `NGX_CMD_QUIT` 后置标志位」直接衔接。
- **延伸阅读**：想看 master 如何在 reload 时用 `NGX_PROCESS_JUST_RESPAWN` + `ngx_signal_worker_processes` 实现「先启新 worker、再退老 worker」，可重读 [ngx_process_cycle.c 的 master 循环 L211-L244](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L211-L244)，并结合本讲的 respawn 类型表理解。
- **操作系统基础补充**：若对 `SCM_RIGHTS` 传 fd 的内核机制感兴趣，可阅读 Linux 手册 `cmsg(3)` 与 `unix(7)`，对照本讲 `ngx_write_channel`/`ngx_read_channel` 理解「辅助数据」的物理含义。
