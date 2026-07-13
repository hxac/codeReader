# 操作系统抽象层 src/os/unix

## 1. 本讲目标

nginx 要在 Linux、FreeBSD、Solaris、Darwin、Windows 等多种操作系统上以同一份主体源码运行，而这些系统的系统调用名、参数、行为细节各不相同。本讲聚焦 `src/os/unix` 这一层，学完后你应当能够：

- 说出 `*_config.h` 与 `*_init.c` 两类文件的分工：一个管「编译期能力探测」，一个管「运行时初始化与 I/O 接口表选择」。
- 看懂 `ngx_os_io_t` 这张「I/O 接口表」如何把收发函数抽象成一组函数指针，让上层协议代码与具体系统调用解耦。
- 掌握 `ngx_files` / `ngx_socket` 系列宏如何把 `open/read/write/close`、`TCP_CORK/TCP_NOPUSH` 等差异封装成统一名字。
- 理解 `ngx_alloc` / `ngx_memalign` / `ngx_alloc_buf` 三种分配方式与「对齐」为什么对 I/O 很重要。
- 读懂 `ngx_readv_chain` / `ngx_writev_chain` / `ngx_linux_sendfile_chain` 三条 I/O 快路径，并能解释 nginx 在「读 socket」与「发静态文件」时分别选了哪条路、为什么。

本讲承接 [u4-l1](u4-l1-process-cycle.md) 已经建立的「master/worker 进程模型」。worker 进入事件循环后，每一次读写最终都会落到本讲这层抽象上。

## 2. 前置知识

阅读本讲前，建议你已经了解：

- **系统调用与标准库**：`open/read/write/close`、`recv/send`、`readv/writev`、`sendfile`、`malloc` 都是进程请求操作系统服务的入口。
- **用户态缓冲与内核态缓冲**：数据从磁盘到进程要经过内核页缓存，从进程到网卡要经过内核 socket 缓冲区。
- **内存对齐**：CPU 访问对齐地址更快；某些 I/O 模式（如 `O_DIRECT`）强制要求缓冲区起始地址按页大小对齐。
- **`ngx_pool_t` 内存池**：见 [u2-l1](u2-l1-memory-pool.md)，nginx 的对象大多从池里分配，随请求整体回收。
- **`ngx_buf_t` / `ngx_chain_t`**：见 [u2-l4](u2-l4-buf-and-output-chain.md)，是 nginx 数据流的基本单位；本讲反复出现 `in_file`、`temporary`、`pos/last` 等字段。

两个关键名词先在这里统一：

- **scatter-gather I/O（分散聚集 I/O）**：一次系统调用把多个不连续的内存区读入（scatter，`readv`）或写出（gather，`writev`），避免多次 syscall。
- **零拷贝（zero-copy）**：`sendfile` 把文件内容从内核页缓存直接送到 socket 缓冲区，中间不经过用户态内存，省掉一次 CPU 拷贝。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/os/unix/ngx_os.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_os.h) | 定义 I/O 接口表 `ngx_os_io_t`、`ngx_iovec_t`、`NGX_IOVS_PREALLOCATE`，声明跨平台 I/O 函数。 |
| [src/os/unix/ngx_posix_init.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_posix_init.c) | 所有 POSIX 系统共用的默认 `ngx_os_io` 与 `ngx_os_init`（取页大小、cacheline、RLIMIT_NOFILE 等）。 |
| [src/os/unix/ngx_linux_config.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_config.h) | Linux 专用的系统头文件包含与能力宏（`NGX_HAVE_SENDFILE64` 等）。 |
| [src/os/unix/ngx_linux_init.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_init.c) | Linux 专用的 `ngx_linux_io` 接口表与 `ngx_os_specific_init`（uname、把 `ngx_os_io` 换成 Linux 版）。 |
| [src/os/unix/ngx_files.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h) | 文件操作的跨平台宏（`ngx_open_file`→`open` 等）与类型定义。 |
| [src/os/unix/ngx_socket.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_socket.c) | 非阻塞设置、`tcp_nopush`/`tcp_push`（Linux 用 `TCP_CORK`，FreeBSD 用 `TCP_NOPUSH`）。 |
| [src/os/unix/ngx_alloc.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c) | `ngx_alloc`/`ngx_calloc`/`ngx_memalign`：裸 malloc 包装与对齐分配。 |
| [src/os/unix/ngx_alloc.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.h) | 分配函数声明、`ngx_free` 宏、页大小/cacheline 全局变量声明。 |
| [src/os/unix/ngx_recv.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c) | `ngx_unix_recv`：单缓冲区 `recv` 读。 |
| [src/os/unix/ngx_readv_chain.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c) | `ngx_readv_chain`：把一条 chain 的多个 buf 合并成 iovec 数组，一次 `readv` 读入。 |
| [src/os/unix/ngx_writev_chain.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c) | `ngx_writev_chain` / `ngx_output_chain_to_iovec` / `ngx_writev`：聚集写。 |
| [src/os/unix/ngx_linux_sendfile_chain.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c) | `ngx_linux_sendfile_chain`：Linux 上把文件 buf 用 `sendfile` 零拷贝送出，内存 buf 用 `writev` 送出。 |
| [src/core/ngx_buf.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h) | `ngx_alloc_buf` 宏、`ngx_buf_size`、`ngx_buf_in_memory` 等。 |
| [src/core/ngx_buf.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c) | `ngx_create_temp_buf`、`ngx_alloc_chain_link`、`ngx_chain_coalesce_file`、`ngx_chain_update_sent`。 |

## 4. 核心概念与源码讲解

### 4.1 操作系统抽象层的总体设计

#### 4.1.1 概念说明

如果把 nginx 比作一座大楼，`src/core` 是大楼的承重结构（内存池、字符串、buf、cycle），`src/event` 和 `src/http` 是楼层里的业务，那么 `src/os/unix` 就是地基：它把「Linux 怎么做、FreeBSD 怎么做、Solaris 怎么做」这些脏活全部收拢，对上只暴露一套统一的名字。

这层抽象要解决两类差异：

1. **编译期差异**：某个系统调用是否存在、某个头文件是否需要包含、某个常量叫什么名字。这些在编译时就必须决定。
2. **运行时差异**：同一个语义在不同系统上由不同函数实现（如「打包发送」Linux 用 `TCP_CORK`、FreeBSD 用 `TCP_NOPUSH`），且运行时还要探测内核版本、页大小等。

nginx 的解法是把这两类差异分别交给两类文件：

- `*_config.h`（如 `ngx_linux_config.h`、`ngx_freebsd_config.h`）：负责**包含系统头文件**与**条件编译**。它依赖 `auto/configure` 在编译前探测出的能力宏（写在 `objs/ngx_auto_config.h` 里，形如 `NGX_HAVE_SENDFILE64`、`NGX_HAVE_EPOLL`），用 `#if (NGX_HAVE_XXX)` 选择正确的声明。
- `*_init.c`（如 `ngx_linux_init.c`、`ngx_freebsd_init.c`）：负责**运行时初始化**，最重要的是构造一张「I/O 接口表」`ngx_os_io_t` 并赋值给全局变量 `ngx_os_io`，让上层通过函数指针调用，而不直接调系统调用。

#### 4.1.2 核心流程

整体装配顺序如下：

1. `auto/configure` 探测系统能力，生成 `objs/ngx_auto_config.h`，里面是一堆 `#define NGX_HAVE_XXX 1`。
2. 编译时，`ngx_<os>_config.h` 按 `NGX_HAVE_*` 包含对应系统头文件、声明 fallback。
3. 运行时启动：`ngx_init_cycle` → `ngx_os_init`（在 `ngx_posix_init.c`）→ 通用初始化（取 `ngx_pagesize`、`ngx_cacheline_size`、`RLIMIT_NOFILE`）。
4. `ngx_os_init` 调用 `ngx_os_specific_init`（在对应 `*_init.c`），它把系统专用的 `ngx_<os>_io` 表赋给全局 `ngx_os_io`。
5. 此后所有连接的 `recv` / `send_chain` 等都通过 `ngx_os_io` 间接调用，命中本系统最优实现。

用伪代码表示这张接口表：

```
ngx_os_io_t ngx_os_io = {
    recv         = <读一个 buf 的函数>,
    recv_chain   = <读一条 chain 的函数>,
    udp_recv     = <UDP 读>,
    send         = <写一个 buf 的函数>,
    udp_send     = <UDP 写>,
    udp_send_chain = <UDP 批量写>,
    send_chain   = <写一条 chain 的函数，零拷贝关键>,
    flags        = NGX_IO_SENDFILE 或 0,
};
```

#### 4.1.3 源码精读

先看接口表的类型定义。`ngx_os_io_t` 把四类收发操作各拆成「单 buf」与「chain」两种签名，全部用函数指针表达：

[ngx_os.h:L19-L35](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_os.h#L19-L35) — 用 `typedef` 定义四类函数指针（`ngx_recv_pt`/`ngx_recv_chain_pt`/`ngx_send_pt`/`ngx_send_chain_pt`），再聚合成 `ngx_os_io_t` 结构体。`flags` 字段里的 `NGX_IO_SENDFILE`（[ngx_os.h:L16](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_os.h#L16)）标记本系统是否支持零拷贝 sendfile，上层据此决定是否把文件 buf 直接交给 `send_chain`。

通用 POSIX 初始化在 `ngx_posix_init.c`。它先给一张「最保守的默认表」，所有系统都能用（用 `writev_chain` 而非 sendfile）：

[ngx_posix_init.c:L22-L31](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_posix_init.c#L22-L31) — 默认 `ngx_os_io`，`send_chain` 指向 `ngx_writev_chain`，`flags` 为 0（不支持 sendfile）。这是兜底实现。

`ngx_os_init` 负责通用运行时探测，并调用系统专用初始化：

[ngx_posix_init.c:L43-L59](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_posix_init.c#L43-L59) — 先 `ngx_os_specific_init` 让具体系统覆盖 `ngx_os_io`；再用 `getpagesize()` 设置全局 `ngx_pagesize`、按 2 的幂算出 `ngx_pagesize_shift`、给 `ngx_cacheline_size` 兜底。这两个全局变量后面在 I/O 与对齐里反复用到。

Linux 专用初始化则在 `ngx_linux_init.c`，它构造了一张「启用 sendfile 的表」并替换默认表：

[ngx_linux_init.c:L16-L30](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_init.c#L16-L30) — `ngx_linux_io` 表：`recv` 用 `ngx_unix_recv`、`recv_chain` 用 `ngx_readv_chain`；当编译期探测到 `NGX_HAVE_SENDFILE` 时，`send_chain` 用 `ngx_linux_sendfile_chain` 并置 `NGX_IO_SENDFILE` 标志，否则退化回 `ngx_writev_chain`。

[ngx_linux_init.c:L33-L52](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_init.c#L33-L52) — `ngx_os_specific_init` 调 `uname` 记录内核版本，最后 `ngx_os_io = ngx_linux_io` 完成接口表切换。注意它不返回错误除非 `uname` 失败——sendfile 能力是编译期决定的，运行时无需再探。

编译期那一侧，看 `ngx_linux_config.h` 怎么处理 sendfile 的历史包袱。Linux 早期 `sendfile` 只支持 32 位偏移，后来才有 `sendfile64`：

[ngx_linux_config.h:L78-L83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_config.h#L78-L83) — 若探测到 `NGX_HAVE_SENDFILE64`，包含 `<sys/sendfile.h>`；否则自己声明旧版 `sendfile` 并定义 `NGX_SENDFILE_LIMIT` 为 2GB。同文件里 `_FILE_OFFSET_BITS 64`（[ngx_linux_config.h:L16](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_config.h#L16)）保证大文件偏移正确。这就是 `*_config.h` 的典型职责：把系统差异变成条件编译。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「接口表切换」这一步，确认运行时 `ngx_os_io.send_chain` 在 Linux 上指向 `ngx_linux_sendfile_chain`。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx（见 [u1-l2](u1-l2-build-and-run.md)），保证有 debug 日志宏。
2. 在 `src/os/unix/ngx_linux_init.c` 的 `ngx_os_specific_init` 里，`ngx_os_io = ngx_linux_io;` 这一行**之前**临时加一行日志（仅用于学习，验证后还原）：
   ```c
   ngx_log_error(NGX_LOG_NOTICE, log, 0,
                 "os_io switched: send_chain=%p", ngx_linux_io.send_chain);
   ```
3. 启动 nginx，查看 `error_log`。

**需要观察的现象**：日志里打印出一个函数指针地址；再对照 `nm objs/nginx | grep ngx_linux_sendfile_chain` 输出的地址，两者应当一致。

**预期结果**：确认 `ngx_os_io.send_chain` 在 Linux 上确实指向 `ngx_linux_sendfile_chain`。如果该地址与 `ngx_writev_chain` 的地址一致，说明编译时没启用 sendfile（不太可能在 Linux 上发生，可检查 `NGX_HAVE_SENDFILE` 是否定义）。

> 说明：本实践需要改源码加日志，属于「源码阅读型 + 临时插桩」实践，验证后请还原改动，不要提交。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_os_io` 要先在 `ngx_posix_init.c` 给一个默认表，再在 `*_init.c` 里覆盖？直接让每个系统自己定义不行吗？

**答案**：默认表提供了「最保守、所有 POSIX 系统都能跑」的兜底实现（`writev_chain`、不声明 sendfile 能力）。这样即使某个新系统没有写专用 `*_init.c`，nginx 也能跑起来，只是用不上 sendfile 等优化。这是「渐进增强」的设计：保底正确，再按系统叠加性能优化。

**练习 2**：`NGX_HAVE_SENDFILE` 是运行时探测的还是编译期决定的？从哪个文件能看出来？

**答案**：编译期决定。它由 `auto/configure` 在构建前探测并写入 `objs/ngx_auto_config.h`，编译时被 [ngx_linux_init.c:L23-L29](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_init.c#L23-L29) 的 `#if (NGX_HAVE_SENDFILE)` 用来在编译期二选一。运行时 `ngx_os_specific_init` 不再探测 sendfile，只切表。

### 4.2 跨平台文件与 socket 的宏抽象

#### 4.2.1 概念说明

文件与 socket 操作在不同系统上的差异，多数只是「名字不同、签名几乎相同」。对这类差异，nginx 用最轻量的手段——**宏映射**——把 nginx 自己的名字直接替换成对应系统的系统调用，零运行时开销。

典型例子：

- `ngx_open_file(name, mode, create, access)` → Linux 上就是 `open((const char*)name, mode|create, access)`。
- `ngx_close_file` → `close`，`ngx_delete_file` → `unlink`，`ngx_read_fd` → `read`。
- `ngx_file_size(sb)` → `(sb)->st_size`。
- 「TCP 打包发送」：Linux 用 `TCP_CORK`，FreeBSD 用 `TCP_NOPUSH`，nginx 统一叫 `ngx_tcp_nopush` / `ngx_tcp_push`。

宏抽象的好处是上层代码写 `ngx_open_file(...)`，编译器在不同平台上各自展开成正确的系统调用，无需函数调用开销，也无需 `#ifdef` 散落到业务代码里。

#### 4.2.2 核心流程

宏抽象的流程很简单：

1. `ngx_files.h` 里 `#define ngx_open_file(...) open(...)`。
2. 上层调用 `ngx_open_file`，预处理器在编译期就地展开。
3. 少数有行为差异的（如 `tcp_nopush`）写成真函数，放在 `ngx_socket.c`，按 `NGX_FREEBSD`/`NGX_LINUX` 条件编译分别实现。

#### 4.2.3 源码精读

看 `ngx_files.h` 里这些「薄到几乎透明」的宏。文件类型与基础宏：

[ngx_files.h:L16-L18](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h#L16-L18) — 把 `ngx_fd_t` 定义成 `int`、`ngx_file_info_t` 定义成 `struct stat`、`ngx_file_uniq_t` 定义成 `ino_t`。不同系统上这些底层类型不同（如 Windows 上 `ngx_fd_t` 是 `HANDLE`），统一名字让上层无感。

[ngx_files.h:L65-L66](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h#L65-L66) — `ngx_open_file` 宏展开成 `open`。注意它把 `mode|create` 合并，让调用方不必关心 `O_CREAT` 之类的拼装细节。

[ngx_files.h:L109](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h#L109) 与 [ngx_files.h:L113](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h#L113) — `ngx_close_file`→`close`、`ngx_delete_file`→`unlink`。每个宏旁边都有一个 `_n` 字符串宏（如 `ngx_close_file_n` 为 `"close()"`），专门用于日志里打印「失败的系统调用名」。

读文件用 `pread` 优先（定位 + 读取原子，且不移动文件指针，便于多 worker 并发读）：

[ngx_files.h:L122-L127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_files.h#L122-L127) — `ngx_read_file` 是真函数（不是宏），但它的「名字字符串」`ngx_read_file_n` 随 `NGX_HAVE_PREAD` 在 `"pread()"` 与 `"read()"` 间切换，这样日志能准确反映实际用的系统调用。

socket 相关的差异要复杂些，所以写成函数。看 `ngx_socket.c`：

[ngx_socket.c:L26-L34](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_socket.c#L26-L34) — `ngx_nonblocking` 在支持 `FIONBIO` 的系统上用 `ioctl(FIONBIO)`，注释解释了原因：`ioctl` 一次系统调用就能设非阻塞，而 `fcntl(F_SETFL, O_NONBLOCK)` 得先 `F_GETFL` 再 `F_SETFL` 两次。文件顶部 [ngx_socket.c:L12-L22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_socket.c#L12-L22) 的注释把这层取舍写得很清楚。

「TCP 打包发送」是 socket 抽象里最体现跨平台价值的地方：

[ngx_socket.c:L78-L99](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_socket.c#L78-L99) — Linux 分支用 `setsockopt(TCP_CORK)` 实现 `ngx_tcp_nopush`/`ngx_tcp_push`：`cork=1` 打开软木塞（内核暂缓发送，攒一批），`cork=0` 拔掉软木塞（把攒的数据一次发出）。FreeBSD 分支（[ngx_socket.c:L52-L73](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_socket.c#L52-L73)）用 `TCP_NOPUSH`，语义对应但不支持的系统（`#else` 分支）直接返回 0 当作无操作。上层只认 `ngx_tcp_nopush` 这一个名字。

#### 4.2.4 代码实践

**实践目标**：体会「宏就地展开」与「`_n` 字符串宏」的配合，理解日志里为什么会精确出现系统调用名。

**操作步骤**：

1. 在 `src/os/unix/ngx_files.h` 找到 `ngx_open_file`、`ngx_close_file`、`ngx_read_file_n` 三个定义。
2. 用 `grep -rn "ngx_open_file_n\|ngx_close_file_n" src/` 查看它们在哪些 `ngx_log_error` 里被用作失败提示。
3. 故意把 `conf/nginx.conf` 里的 `pid` 路径指向一个无写权限的目录，运行 `nginx -t`。

**需要观察的现象**：错误日志里会出现形如 `open() "/xxx/nginx.pid" failed (13: Permission denied)` 的字样。

**预期结果**：日志里的 `open()` 正是 `ngx_open_file_n` 展开的字符串。这说明宏抽象既统一了调用名，又保留了诊断所需的系统调用原名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ngx_open_file` 用宏实现，而 `ngx_tcp_nopush` 用函数实现？

**答案**：`ngx_open_file` 在各 POSIX 系统上签名完全一致，只是名字映射，宏展开零开销即可。`ngx_tcp_nopush` 在 Linux、FreeBSD 等系统上对应的 socket 选项不同（`TCP_CORK` vs `TCP_NOPUSH`），且涉及 `setsockopt` 的参数拼装，用函数 + 条件编译更清晰，也便于在不支持的系统上返回 0 兜底。

**练习 2**：`ngx_read_file` 为什么不直接做成宏，而是真函数？提示看它旁边那个 `_n` 宏。

**答案**：`ngx_read_file` 内部要根据 `NGX_HAVE_PREAD` 选择调用 `pread` 还是 `lseek+read`，并处理错误与偏移，逻辑较多，适合写成函数。旁边的 `ngx_read_file_n` 字符串宏随能力宏切换，让日志能准确报告「是 pread 失败还是 read 失败」，把诊断信息与实现解耦。

### 4.3 内存分配与对齐：ngx_alloc / ngx_memalign / ngx_alloc_buf

#### 4.3.1 概念说明

nginx 里有三类「分配内存」的需求，对应三种不同的原语：

1. **脱离内存池的裸分配**：比如分配一块独立的共享内存缓冲、或一块生命周期不由请求决定的大缓冲。这时用 `ngx_alloc` / `ngx_calloc`，它们就是带日志的 `malloc` 包装。
2. **对齐分配**：某些场景要求返回的地址按指定边界对齐（如页边界、cacheline 边界）。这对 `O_DIRECT` 直接 I/O、对避免多核 false sharing 很重要。这时用 `ngx_memalign`。
3. **从内存池分配结构体**：绝大多数 nginx 对象（包括 `ngx_buf_t` 本身）从请求的内存池分配，随池回收。这时用 `ngx_alloc_buf(pool)`，它其实只是 `ngx_palloc(pool, sizeof(ngx_buf_t))` 的宏别名。

「对齐」为什么对 I/O 重要？两条理由：

- 直接 I/O（`O_DIRECT`）要求用户缓冲区起始地址、长度、文件偏移都按块大小（通常是页大小）对齐，否则系统调用直接返回 `EINVAL`。
- cacheline 对齐能让不同 CPU 核心访问不同变量时不会因为共享同一 cacheline 而互相失效（false sharing），对多 worker 并发访问的字段尤其重要。

#### 4.3.2 核心流程

- `ngx_alloc(size, log)` → `malloc(size)`，失败记 `EMERG` 日志，成功记 `debug` 日志（带地址与大小）。
- `ngx_calloc(size, log)` → `ngx_alloc` 后 `ngx_memzero` 清零。
- `ngx_memalign(alignment, size, log)` → 优先 `posix_memalign`，否则 `memalign`；若平台两者都没有，则在头文件里退化成 `ngx_alloc`（不保证对齐）。
- `ngx_alloc_buf(pool)` → `ngx_palloc(pool, sizeof(ngx_buf_t))`，从池里拿一个 buf 结构体。

注意 `ngx_alloc_buf` 分配的是 `ngx_buf_t` 这个**结构体**，不是 buf 里指向的数据缓冲区。数据缓冲区由 `ngx_create_temp_buf` 单独 `ngx_palloc` 一块（见下文）。

#### 4.3.3 源码精读

裸分配与对齐分配都在 `ngx_alloc.c`：

[ngx_alloc.c:L17-L31](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c#L17-L31) — `ngx_alloc` 调 `malloc`，失败用 `NGX_LOG_EMERG`（最高级）报错，成功用 `NGX_LOG_DEBUG_ALLOC` 打 `malloc: %p:%uz`。这层包装的价值就是这两条日志：生产环境靠 EMERG 发现 OOM，调试时靠 debug 追踪每块内存。

[ngx_alloc.c:L34-L46](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c#L34-L46) — `ngx_calloc` 在 `ngx_alloc` 基础上 `ngx_memzero` 清零，等价于 `calloc` 但走 nginx 自己的日志路径。

对齐分配有两个实现，由能力宏二选一：

[ngx_alloc.c:L49-L69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c#L49-L69) — 优先用 `posix_memalign`（POSIX 标准，错误码通过返回值给出）。注意 `posix_memalign` 要求 `alignment` 是 2 的幂且为 `sizeof(void *)` 的倍数，调用方须保证。

[ngx_alloc.c:L71-L88](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.c#L71-L88) — 没有 `posix_memalign` 时回退到 `memalign`（glibc/Solaris 扩展，错误通过 errno 给）。

头文件里规定了「没有对齐能力时静默退化」的契约：

[ngx_alloc.h:L29-L37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.h#L29-L37) — 若 `NGX_HAVE_POSIX_MEMALIGN` 和 `NGX_HAVE_MEMALIGN` 都没定义，`ngx_memalign` 被宏定义为 `ngx_alloc`（忽略 alignment 参数）。这让上层可以无条件调用 `ngx_memalign` 而不用担心平台不支持——最坏情况只是没对齐。上方 [ngx_alloc.h:L22-L27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.h#L22-L27) 的注释列出了各平台能力。

`ngx_memalign` 真正被用到的地方是内存池本身——池的第一个大块就是按 `NGX_POOL_ALIGNMENT` 对齐分配的：

[ngx_palloc.c:L23](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_palloc.c#L23)（示例引用，非本讲文件）— `ngx_create_pool` 用 `ngx_memalign(NGX_POOL_ALIGNMENT, size, log)` 申请池的整块内存，保证池内后续小对象分配的起始地址自然对齐。这是「对齐分配」最关键的实际用处。

再看 buf 结构体的分配。`ngx_alloc_buf` 是个宏：

[ngx_buf.h:L144-L145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.h#L144-L145) — `ngx_alloc_buf(pool)` 展开为 `ngx_palloc(pool, sizeof(ngx_buf_t))`，`ngx_calloc_buf(pool)` 展开为 `ngx_pcalloc(...)`（清零版）。注意它只分配结构体，不含数据缓冲区。

数据缓冲区由 `ngx_create_temp_buf` 单独分配：

[ngx_buf.c:L12-L44](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L12-L44) — 先 `ngx_calloc_buf` 拿一个清零的 `ngx_buf_t`，再 `ngx_palloc(pool, size)` 拿一块数据缓冲，把 `start/pos/last/end` 指过去并置 `temporary=1`。这里分配数据缓冲用的是普通 `ngx_palloc`（不一定对齐），因为常规 socket 读写不要求页对齐；只有走直接 I/O 的路径才会改用 `ngx_memalign`。

最后看 chain 节点的复用机制，它体现了 nginx 对分配的吝啬：

[ngx_buf.c:L47-L65](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L47-L65) — `ngx_alloc_chain_link` 先看池的 `chain` 自由链（`pool->chain`）有没有可复用节点，有就直接摘下来返回，没有才 `ngx_palloc` 新建。这是 u2-l4 讲过的「chain 节点自由链」，能极大减少热点路径上的分配次数。

#### 4.3.4 代码实践

**实践目标**：用 `--with-debug` 编译的 nginx，观察 `ngx_alloc` 的 debug 日志，理解「裸分配」与「池分配」的区别。

**操作步骤**：

1. 按 [u1-l2](u1-l2-build-and-run.md) 用 `--with-debug` 编译 nginx。
2. 在 `nginx.conf` 里写 `error_log logs/error.log debug_alloc;`（`debug_alloc` 是 nginx 内置的 debug 点之一）。
3. 启动 nginx，用 `curl` 请求一次静态文件，然后停止。
4. 在 `logs/error.log` 里 grep `malloc:` 与 `posix_memalign:`。

**需要观察的现象**：日志里出现若干 `malloc: <addr>:<size>` 行，少量 `posix_memalign:` 行（多为内存池创建）。

**预期结果**：你会看到 nginx 在启动与处理请求过程中确实调用了裸 `malloc`/`posix_memalign`，并且每次都带地址与大小。注意普通请求级对象（buf、chain）大多走池分配，不会出现在 `malloc:` 日志里——它们在 u2-l1 的池内 bump 分配，不经过 `ngx_alloc`。

> 若你的系统未启用 `debug_alloc` 点，日志可能为空；这时可改用 `debug` 全开。行为差异属于「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_alloc_buf(pool)` 和 `ngx_create_temp_buf(pool, size)` 有什么区别？

**答案**：`ngx_alloc_buf` 只从池里分配 `ngx_buf_t` **结构体**本身（不包含数据缓冲，字段未初始化）。`ngx_create_temp_buf` 既分配结构体（用 `ngx_calloc_buf` 清零），又额外 `ngx_palloc` 一块 `size` 字节的数据缓冲，并把 `start/pos/last/end` 指向它、置 `temporary=1`，返回一个可直接写入的临时 buf。前者是「要个壳」，后者是「要个能装数据的壳」。

**练习 2**：假设某平台既没有 `posix_memalign` 也没有 `memalign`，调用 `ngx_memalign(4096, 100, log)` 会怎样？返回的地址一定 4096 对齐吗？

**答案**：根据 [ngx_alloc.h:L35](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_alloc.h#L35)，此时 `ngx_memalign` 被宏定义为 `ngx_alloc(size, log)`，即退化成普通 `malloc`，`alignment` 参数被忽略。返回地址**不保证** 4096 对齐（glibc `malloc` 对大块通常页对齐，对小块不保证）。因此依赖对齐的代码（如 `O_DIRECT`）在这种平台上无法正确工作——好在现代 Linux/FreeBSD 都有对齐原语，nginx 在配置阶段就会探测到。

### 4.4 高效 I/O 快路径：readv / writev / sendfile

#### 4.4.1 概念说明

这是本讲的重头戏。worker 处理请求时，数据搬运集中在两条路径：

- **读路径**：从 socket 把客户端发来的数据读进内存。一次请求可能要填满多个 buf（HTTP 头、请求体分块等），如果每个 buf 单独 `recv` 一次，syscall 次数会爆炸。`readv` 一次调用把数据 scatter 到多个不连续内存区，nginx 用 `ngx_readv_chain` 把一条 `ngx_chain_t` 直接喂给 `readv`。
- **写路径**：把响应发给客户端。响应常由「内存里的头」+「磁盘上的文件体」组成。内存部分用 `writev` 聚集写（`ngx_writev_chain`），文件部分用 `sendfile` 零拷贝（`ngx_linux_sendfile_chain`）。

三条快路径共享同一个核心思想：**把 chain 上相邻的 buf 合并（coalesce）成一个 iovec，尽量一次 syscall 搬完**。

关键数据结构是 `ngx_iovec_t`，它是 iovec 数组的「带元信息包装」：

[ngx_os.h:L64-L69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_os.h#L64-L69) — `iovs` 指向 iovec 数组、`count` 是当前已用的 iovec 数、`size` 是这些 iovec 累计字节数、`nalloc` 是数组容量。

预分配的 iovec 数组大小由 `NGX_IOVS_PREALLOCATE` 决定：

[ngx_os.h:L57-L61](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_os.h#L57-L61) — 若系统 `IOV_MAX > 64`，预分配 64 个 iovec（足够覆盖绝大多数 chain，且栈开销可控）；否则用 `IOV_MAX`（系统允许的最大值）。这些 iovec 直接**分配在栈上**（如 `struct iovec iovs[NGX_IOVS_PREALLOCATE];`），避免热点路径上的堆分配。

#### 4.4.2 核心流程

**读路径 `ngx_readv_chain`**：

```
1. 检查 kqueue/epollrdhup 的 eof/available 提示，提前返回 AGAIN 或 0
2. 遍历 chain，把每个 buf 的可写区间 [last, end) 填进 iovec：
   - 若本 buf 的 last 与上一个 buf 的 end 相邻（prev == buf->last），
     则并入同一个 iovec（只加 iov_len），实现合并
   - 否则新开一个 iovec
   - 受 limit 约束，超限就截断
3. 一次 readv(fd, iovs, nelts)
4. n == 0：对端关闭，置 eof
   n > 0：按事件后端更新 rev->ready / rev->available
  出错：EAGAIN→AGAIN，EINTR→重试，其它→ERROR
```

**写路径 `ngx_writev_chain`**：

```
1. 限幅 limit（不超过 SIZE_MAX - pagesize）
2. 循环：
   a. ngx_output_chain_to_iovec 把 chain 的内存 buf 合并进 iovec
      （碰到 in_file 的 buf 立即 break，文件交给 sendfile 路径）
   b. ngx_writev → writev 一次写出
   c. ngx_chain_update_sent 按已发字节数推进各 buf 的 pos
   d. 若本轮没发完且没到 limit，继续；否则返回剩余 chain
```

**sendfile 路径 `ngx_linux_sendfile_chain`**：

```
1. 限幅 limit（不超过 2G - pagesize）
2. 循环：
   a. ngx_output_chain_to_iovec 合并内存 buf 成 header iovec
   b. 若「header 非空 且 后面紧跟文件 buf」：设 TCP_CORK 攒包
   c. 若 header 为空且当前是文件 buf：合并相邻文件 buf，调 sendfile 零拷贝
      否则：调 writev 写内存 header
   d. update_sent 推进 buf；处理 EAGAIN/部分写重试
```

为什么 sendfile 要限幅到 2G？文件顶部注释解释了 Linux 各版本 `sendfile` 对 `count` 参数的限制历史：

[ngx_linux_sendfile_chain.c:L29-L46](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L29-L46) — 旧版 Linux 的 `sendfile` 在 32 位偏移、2G-1 上限、2.6.16 后静默截断到 `2G - pagesize` 等方面有种种坑，所以 nginx 定义 `NGX_SENDFILE_MAXSIZE = 2147483647`（2G-1）并主动留出一页余量。

#### 4.4.3 源码精读

**读路径**：`ngx_readv_chain` 的合并循环是核心：

[ngx_readv_chain.c:L86-L119](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L86-L119) — 遍历 chain，`n = buf->end - buf->last` 是当前 buf 剩余可写空间。若 `prev == chain->buf->last`（上一段连续），就把 `n` 累加到现有 `iov->iov_len`；否则 `ngx_array_push` 新开一个 iovec。`prev = chain->buf->end` 记下本段末尾，供下一段判断是否连续。`limit` 与 `nalloc` 双重截断防止越界与超额。

[ngx_readv_chain.c:L124-L125](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L124-L125) — 一次 `readv(c->fd, vec.elts, vec.nelts)` 把数据 scatter 进所有 iovec。

返回值处理体现「事件后端感知」：`n == 0` 表示对端关闭（[ngx_readv_chain.c:L127-L145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L127-L145)）；`n > 0` 时按 kqueue 的 `available`、`FIONREAD`、`EPOLLRDHUP` 分别更新 `rev->ready`（[ngx_readv_chain.c:L147-L229](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L147-L229)）；错误里 `EAGAIN` 与 `EINTR` 都转成 `NGX_AGAIN` 并在 `EINTR` 时重试（[ngx_readv_chain.c:L231-L243](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L231-L243)）。`ngx_unix_recv` 是它的「单 buf 简化版」，逻辑同构，可对照 [ngx_recv.c:L70-L94](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_recv.c#L70-L94)。

**写路径**：`ngx_writev_chain` 的主循环：

[ngx_writev_chain.c:L51-L103](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c#L51-L103) — 每轮先 `ngx_output_chain_to_iovec` 合并内存 buf，再 `ngx_writev` 写出，再 `ngx_chain_update_sent` 推进 pos。注意 [ngx_writev_chain.c:L62-L79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c#L62-L79) 的断言：`writev` 路径**不允许**出现 `in_file` 的 buf，碰到就报 ALERT 并 `ngx_debug_point()`——文件 buf 必须走 sendfile 路径，不能进 writev。

合并逻辑在 `ngx_output_chain_to_iovec`：

[ngx_writev_chain.c:L156-L172](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c#L156-L172) — 与读路径对称：`prev == in->buf->pos` 则并入现有 iovec，否则新开。`ngx_buf_special`（flush/sync 控制帧）跳过，`in_file` 直接 break 把控制权交回上层。

底层 `ngx_writev`：

[ngx_writev_chain.c:L181-L216](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c#L181-L216) — `eintr` 标签循环调 `writev`，`EAGAIN` 返回 `NGX_AGAIN`，`EINTR` 重试，其它错误置 `wev->error` 并报 `NGX_ERROR`。这是 nginx 处理慢速系统调用的标准范式：非阻塞 + EAGAIN 背压 + EINTR 自动重试。

**sendfile 路径**：`ngx_linux_sendfile_chain` 是三者里最复杂的，因为它要同时处理「内存 header」和「文件 body」两类 buf，还要协调 TCP_CORK：

[ngx_linux_sendfile_chain.c:L49-L82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L49-L82) — 入口先检查 `wev->ready`，再按 `NGX_SENDFILE_MAXSIZE - ngx_pagesize` 限幅，`header` 用栈上 `struct iovec headers[NGX_IOVS_PREALLOCATE]`。

[ngx_linux_sendfile_chain.c:L95-L158](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L95-L158) — TCP_CORK 协调：当「header 非空 且 紧跟文件 buf」时，若之前设过 `TCP_NODELAY`，先关掉它（CORK 与 NODELAY 互斥），再 `ngx_tcp_nopush(c->fd)` 打开 CORK。目的是让 HTTP 头和文件首块数据被打包成一批发送，减少小包数量。

[ngx_linux_sendfile_chain.c:L162-L198](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L162-L198) — 分支：若 header 为空且当前是文件 buf，用 `ngx_chain_coalesce_file` 合并相邻文件 buf（要求同 fd 且偏移连续），调 `ngx_linux_sendfile` 走零拷贝；否则（有内存 header）调 `ngx_writev` 写内存。`NGX_DONE` 表示 sendfile 被丢给线程池异步执行（见下）。

[ngx_linux_sendfile_chain.c:L209-L222](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L209-L222) — 关键的「部分写」处理：Linux 4.3+ 的 sendfile 可能随时被中断且不报错，所以只要 `send - prev_send != sent`（实际发的少于预期）就回退 `send` 并重试，直到拿到明确的 `EAGAIN`。

底层 `ngx_linux_sendfile`：

[ngx_linux_sendfile_chain.c:L231-L301](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L231-L301) — 若 `file->file->thread_handler` 非空（启用了线程池），转 `ngx_linux_sendfile_thread` 异步执行（[ngx_linux_sendfile_chain.c:L242-L248](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L242-L248)）；否则直接 `sendfile(c->fd, file->file->fd, &offset, size)`（[L261](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L261)）。`n == 0` 被当作「文件被截短」的异常（[L284-L295](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L284-L295)），因为 sendfile 不会无故返回 0。

文件 buf 的合并在 `ngx_chain_coalesce_file`：

[ngx_buf.c:L226-L268](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L226-L268) — 循环累加 `file_last - file_pos`，当被 `limit` 截断时还会把截断点向上对齐到页边界（`aligned = (pos+size+pagesize-1) & ~(pagesize-1)`），这是为直接 I/O 准备的：让每次 sendfile 的范围尽量落在整页上。合并条件包括「同 fd、偏移连续（`fprev == file_pos`）」。

已发字节数推进在 `ngx_chain_update_sent`：

[ngx_buf.c:L271-L314](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_buf.c#L271-L314) — 按已发 `sent` 依次推进每个 buf 的 `pos`（内存）或 `file_pos`（文件），发完一个就跳到下一个，部分发完就停。返回剩余未发的 chain 头。

#### 4.4.4 代码实践

**实践目标**：对比 `ngx_linux_sendfile_chain` 与 `ngx_readv_chain`，说明 nginx 在「发静态文件」与「读 socket」时分别选了哪条快路径、为什么。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx，`nginx.conf` 开 `error_log logs/error.log debug;`，配置一个最简静态站点：
   ```nginx
   events { worker_connections 1024; }
   http {
       server { listen 8080; root html; }
   }
   ```
2. 启动 nginx，`curl -v http://127.0.0.1:8080/index.html > /dev/null` 请求一次静态文件。
3. 在 `error.log` 里分别 grep `sendfile:` 与 `readv:` 两条 debug 关键字（它们来自 [ngx_linux_sendfile_chain.c:L258](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L258) 与 [ngx_readv_chain.c:L121](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_readv_chain.c#L121) 的 debug 日志）。
4. 再用 `curl -d 'hello' http://127.0.0.1:8080/` 发一个带请求体的 POST，观察 `readv:` 是否出现（读取请求体）。

**需要观察的现象**：

- 请求静态文件时：`readv:` 出现在读请求行/头部阶段（数据量小）；响应阶段出现 `sendfile: @<偏移> <字节数>`，把 `index.html` 内容零拷贝发出。
- POST 带请求体时：`readv:` 还会出现在读请求体阶段。

**预期结果 / 结论**：

- **读 socket**（请求行、头部、请求体）走 `ngx_readv_chain` → `readv`。原因：数据来自网络，必须进用户态内存供 HTTP 解析器处理，没有「文件」可零拷贝；`readv` 一次填满多个 buf，减少 syscall。
- **发静态文件**走 `ngx_linux_sendfile_chain` → `sendfile`。原因：文件内容已在内核页缓存，`sendfile` 直接从页缓存送到 socket，省掉一次「内核→用户→内核」的 CPU 拷贝，也不需要分配用户态缓冲。只有 HTTP 响应头（内存 buf）才用 `writev` 发出，且会和文件首块用 TCP_CORK 打包。

若 debug 日志未出现 `sendfile:` 行，检查是否编译时启用了 sendfile（Linux 上默认启用）、是否用了 `aio threads;`（会改走线程池 sendfile，debug 关键字变为 `linux sendfile thread`）。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_writev_chain` 里碰到 `in_file` 的 buf 会怎样？为什么这样设计？

**答案**：会报 `NGX_LOG_ALERT` 并 `ngx_debug_point()`，返回 `NGX_CHAIN_ERROR`（见 [ngx_writev_chain.c:L62-L79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_writev_chain.c#L62-L79)）。因为 `writev` 只能写内存区，不能直接写文件；文件 buf 必须由支持 sendfile 的 `send_chain`（如 `ngx_linux_sendfile_chain`）处理。`ngx_writev_chain` 是「不支持 sendfile 的兜底实现」，正常不会收到文件 buf，收到就说明上层路由出错，故用 ALERT 提示。

**练习 2**：为什么 `ngx_linux_sendfile_chain` 要在「有 header 且紧跟文件 buf」时设 TCP_CORK？

**答案**：HTTP 响应头在内存里、响应体在文件里。若不打包，头部可能先作为一个小 TCP 包发出，体随后再发，造成两个包。设 TCP_CORK 后内核会暂缓发送，等文件首块数据就绪后一起发出，减少包数量、提高网络效率。注释里还指出 CORK 与 NODELAY 互斥，所以设 CORK 前要先关掉之前可能设过的 NODELAY（[ngx_linux_sendfile_chain.c:L104-L132](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_linux_sendfile_chain.c#L104-L132)）。

**练习 3**：`ngx_chain_coalesce_file` 在被 `limit` 截断时为什么要对齐到页边界？

**答案**：为支持直接 I/O（`O_DIRECT`）。直接 I/O 要求读写范围按块（页）对齐，否则返回 `EINVAL`。当一次 sendfile 因 `limit` 不能发完整段时，把截断点向上取整到页边界，能让本次发送的文件范围落在整页上，既满足直接 I/O 约束，又便于下次从对齐点继续。普通缓冲 I/O 下这个对齐只是少发一点，不影响正确性。

## 5. 综合实践

把本讲四条主线串起来，做一次「从配置到 syscall」的完整追踪。

**任务**：配置一个静态站点，让一个 `curl` 请求的整个数据通路经过本讲讲过的每一个抽象层，并逐层在源码中定位。

**步骤**：

1. 按 [u1-l2](u1-l2-build-and-run.md) 用 `--with-debug` 编译 nginx，配置：
   ```nginx
   events { worker_connections 1024; }
   http {
       access_log off;
       error_log logs/error.log debug;
       server { listen 8080; root html; }
   }
   ```
2. 启动 nginx，`curl http://127.0.0.1:8080/index.html -o /dev/null`。
3. 打开 `logs/error.log`，按时间顺序找出以下 debug 行，并在源码里标注它来自哪一层：
   - `malloc:` / `posix_memalign:` —— 来自 `ngx_alloc` / `ngx_memalign`（4.3，内存分配与对齐，多为池创建）。
   - `readv: <n>, last:<size>` —— 来自 `ngx_readv_chain`（4.4，读请求行/头部）。
   - `sendfile: @<offset> <size>` 与 `sendfile: <n> of <size> @<offset>` —— 来自 `ngx_linux_sendfile`（4.4，零拷贝发文件）。
   - `writev: <n> of <size>` —— 来自 `ngx_writev`（4.4，发 HTTP 响应头）。
   - 若看到 `no tcp_nodelay` / `tcp_nopush` —— 来自 `ngx_linux_sendfile_chain` 的 TCP_CORK 协调（4.2 + 4.4）。
4. 画一张时序图：`curl 请求 → readv(读请求) → [解析、定位文件] → writev(发头) + sendfile(发体, TCP_CORK 打包) → curl 收到响应`。
5. 在图上标注每一步用到的 `ngx_os_io` 接口表字段（`recv_chain` / `send_chain`）和最终系统调用（`readv` / `writev` / `sendfile`）。

**验收标准**：

- 能指出 `ngx_os_io.send_chain` 在 Linux 上指向 `ngx_linux_sendfile_chain`，而它内部对内存 buf 调 `ngx_writev`、对文件 buf 调 `sendfile`。
- 能解释「读 socket 用 readv、发文件用 sendfile」的根本原因：读端数据必须进用户态供解析，写端文件已在内核可零拷贝。
- 能说出 TCP_CORK 在响应头+体场景下减少小包的作用。

## 6. 本讲小结

- `src/os/unix` 是 nginx 的「地基」：`*_config.h` 管编译期能力探测与系统头文件包含，`*_init.c` 管运行时初始化与 `ngx_os_io` 接口表切换，二者把系统差异对上完全屏蔽。
- `ngx_os_io_t` 把收发操作抽象成函数指针表，默认表（`ngx_posix_init.c`）保底用 `writev_chain`，Linux 表（`ngx_linux_init.c`）在编译期探测到 sendfile 后升级为 `ngx_linux_sendfile_chain`。
- `ngx_files` / `ngx_socket` 用宏把 `open/close/read` 等映射成统一名字（零开销），少数有行为差异的（`tcp_nopush`）写成函数按平台条件编译。
- `ngx_alloc` / `ngx_calloc` 是带日志的裸 malloc 包装；`ngx_memalign` 提供对齐分配，无能力时静默退化；`ngx_alloc_buf` 是从池分配 buf 结构体的宏，数据缓冲由 `ngx_create_temp_buf` 另行分配。
- 三条 I/O 快路径共享「合并相邻 buf 成 iovec」的思想：`ngx_readv_chain` 用 `readv` scatter 读 socket，`ngx_writev_chain` 用 `writev` gather 写内存 buf，`ngx_linux_sendfile_chain` 对文件 buf 用 `sendfile` 零拷贝、对内存 header 用 `writev` 并用 TCP_CORK 打包。
- 读 socket 选 readv（数据须进用户态解析），发静态文件选 sendfile（文件已在内核、可零拷贝），这是两条路径分工的根本原因。

## 7. 下一步学习建议

- **[u5-l1 事件模型总览 ngx_event](u5-l1-event-model.md)**：本讲的 `ngx_readv_chain` / `ngx_writev_chain` 是「怎么读写」，下一讲回答「什么时候读写」——事件驱动如何把这些 I/O 操作挂到 epoll 上按需触发。
- **[u5-l3 接受连接与 connection 管理](u5-l3-accept-and-connection.md)**：本讲反复出现的 `ngx_connection_t` 在那里被分配与绑定读写事件，能补全「连接 → 事件 → I/O」的完整链路。
- **[u6-l8 静态文件 content handler](u6-l8-static-content-handler.md)**：本讲的 sendfile 快路径在那里被上层静态文件模块调用，结合 `open_file_cache` 能看到「文件打开 → 信息缓存 → sendfile 输出」的完整业务语境。
- 进阶可阅读 `src/os/unix/ngx_darwin_sendfile_chain.c` 与 `ngx_freebsd_sendfile_chain.c`，对比不同系统 sendfile 实现的差异，体会 OS 抽象层「同接口、不同实现」的价值。
