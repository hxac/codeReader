# 程序启动入口 main() 全流程

> 本讲是「项目总览、构建与启动入口」单元的最后一篇。前面三讲你已经知道了 nginx 是什么、怎么从源码构建、源码目录如何分层。这一讲我们顺着 `main()` 这条主线，把整台机器「从上电到运转」的全过程走一遍，并学会回答两个常被问到的问题：`nginx -t` 到底干了什么？`nginx -s reload` 是谁在 reload？

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出 `main()` 的整体启动顺序，并能在 `src/core/nginx.c` 中找到每一步对应的代码。
2. 理解命令行参数是如何被解析（`ngx_get_options`）并落地到 `cycle` 的字段（`ngx_process_options`）。
3. 理解「继承监听套接字」机制（`ngx_add_inherited_sockets`），以及它为什么是实现「零停机二进制升级」的钥匙。
4. 解释 `nginx -t`、`nginx -V`、`nginx -s reload` 这三条命令分别在哪一行代码「短路返回」，背后走的是哪段逻辑。
5. 理解 `main()` 末尾的进程模型分支：单进程循环（`ngx_single_process_cycle`）与 master 进程循环（`ngx_master_process_cycle`）。

---

## 2. 前置知识

在进入源码之前，先建立三个直觉。如果你已经读过本单元的前三篇，可以把本节当作复习。

### 2.1 cycle 是什么

nginx 把「一次完整的运行实例所需要的一切全局状态」打包进一个巨型结构体 `ngx_cycle_t`。它包含：内存池、日志对象、配置上下文指针（`conf_ctx`）、监听端口数组（`listening`）、共享内存区链表、已加载模块表（`modules`）、配置文件路径等等。

可以把 `ngx_cycle_t` 理解成「这台 nginx 的当前世界」。启动时 `main()` 会创建它；reload 时会基于旧 cycle 创建一个新 cycle，校验通过后再整体替换；升级二进制时，新进程会带着旧进程移交过来的监听套接字重建一个全新的 cycle。本讲反复出现的 `cycle` 就是它。

> `ngx_cycle_t` 的内部细节是 u3-l2《cycle 生命周期》的主题，本讲你只需要把它当作「全局上下文容器」即可。

### 2.2 三种「进程身份」

nginx 用一个全局变量 `ngx_process` 标记当前进程要扮演的角色。取值定义在 `src/os/unix/ngx_process_cycle.h`：

| 宏 | 值 | 含义 |
|---|---|---|
| `NGX_PROCESS_SINGLE` | 0 | 单进程模式（一个进程干所有事） |
| `NGX_PROCESS_MASTER` | 1 | master 进程（管 worker，自己不处理请求） |
| `NGX_PROCESS_SIGNALLER` | 2 | 仅发信号的进程（`nginx -s`） |
| `NGX_PROCESS_WORKER` | 3 | worker 进程（master fork 出来后，worker 内部把自己标成这个） |

参考 [src/os/unix/ngx_process_cycle.h:23-26](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h#L23-L26)。

`main()` 启动时 `ngx_process` 默认是 0（SINGLE）；之后会根据配置和命令行动态调整。本讲末尾会看到这个调整发生在哪里。

### 2.3 配置驱动与命令行

nginx 的能力由「编译进来的模块」决定，行为由「配置文件」决定（u1-l1 已建立）。但除了配置文件，命令行参数（`-c`、`-p`、`-t`、`-s` 等）也会影响启动行为。`main()` 的前半段几乎全是在「把命令行和配置文件这两路输入准备好」，后半段才「真正开跑」。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，并少量引用它的下游被调函数所在文件。

| 文件 | 作用 | 本讲关注 |
|---|---|---|
| [src/core/nginx.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c) | 程序入口 `main()`、命令行解析、版本信息、继承套接字、核心模块定义 | 全文重点 |
| [src/core/nginx.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h) | 版本号、路径、`NGINX_VAR` 环境变量名等宏 | `NGINX_VAR` 宏 |
| [src/core/ngx_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c) | `ngx_cycle_t` 的创建与初始化 `ngx_init_cycle`、`ngx_signal_process` | `ngx_init_cycle` 与 `-s` 信号发送 |
| [src/core/ngx_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c) | 模块系统预初始化 | `ngx_preinit_modules` |
| [src/os/unix/ngx_process_cycle.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c) | master/worker 进程循环 | `ngx_master_process_cycle`、`ngx_single_process_cycle` |
| [src/os/unix/ngx_process_cycle.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.h) | 进程身份宏 | `NGX_PROCESS_*` |

> 本讲为了聚焦 `main()` 主线，不会深入 `ngx_init_cycle` 的内部（那是 u3-l2 的内容），也不会深入 master 循环内部（那是 u4-l1 的内容）。我们只关心它们在 `main()` 中「何时被调用、扮演什么角色」。

---

## 4. 核心概念与源码讲解

先把 `main()` 的全貌看一遍，再逐个最小模块拆解。

`main()` 位于 [src/core/nginx.c:196-388](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L196-L388)。它是一个超长的线性过程，**没有任何复杂分支**——就是一行接一行地初始化，任何一步失败就 `return 1`。理解它的最佳方式是把这段线性流程看成 6 个阶段：

```
阶段 A：命令行与最小环境准备
  ngx_debug_init → ngx_strerror_init → ngx_get_options
  → (若 -v/-V) ngx_show_version_info
  → ngx_time_init / ngx_regex_init / ngx_pid / ngx_log_init / ngx_ssl_init

阶段 B：建立第一个临时 cycle，把命令行落地
  init_cycle + ngx_create_pool → ngx_save_argv → ngx_process_options
  → ngx_os_init → ngx_crc32_table_init → ngx_slab_sizes_init

阶段 C：继承套接字 + 模块预初始化
  ngx_add_inherited_sockets → ngx_preinit_modules

阶段 D：创建真正的 cycle（解析配置、初始化模块、打开监听端口）
  cycle = ngx_init_cycle(&init_cycle)
  → (若 -t) 打印结果并 return 0
  → (若 -s) 发送信号并 return

阶段 E：进程化准备
  决定进程身份 → ngx_init_signals → ngx_daemon → ngx_create_pidfile
  → ngx_log_redirect_stderr → ngx_use_stderr = 0

阶段 F：进入进程循环
  if 单进程: ngx_single_process_cycle(cycle)
  else    : ngx_master_process_cycle(cycle)
```

下面四个小节，分别对应规格要求的四个最小模块：命令行处理、继承套接字、创建 cycle、进程循环分支。每节内部还会带上「三条短路路径（`-v` / `-t` / `-s`）」的精确定位。

---

### 4.1 命令行参数处理：ngx_get_options 与 ngx_process_options

#### 4.1.1 概念说明

当你敲下 `nginx -t -c /tmp/test.conf` 时，shell 把 `argc`/`argv` 交给 `main()`。但 `main()` 不会直接去用这些字符串，而是分两步：

1. **`ngx_get_options`**：纯粹地「扫描 argv，把识别到的开关写进一组文件级全局变量」。它不分配内存、不碰 cycle，职责单一。
2. **`ngx_process_options`**：把上一步解析出的 `-p`（prefix）、`-c`（配置文件）、`-e`（错误日志）、`-g`（额外指令）这些值，**落地到 `cycle` 结构体的对应字段**，并补齐默认值。

这种「先解析到全局变量，再用全局变量初始化结构体」的两段式，是 nginx 里很常见的写法，好处是解析逻辑与对象构造解耦，便于测试和复用。

#### 4.1.2 核心流程

`ngx_get_options` 的解析逻辑是教科书式的「getopt 手写版」：

```
对 argv[1..argc-1] 每个参数 p：
    要求第一个字符是 '-'，否则报错
    对 '-' 之后的每一个字符走 switch：
        '?' / 'h' → ngx_show_version=1, ngx_show_help=1
        'v'      → ngx_show_version=1
        'V'      → ngx_show_version=1, ngx_show_configure=1
        't'      → ngx_test_config=1
        'T'      → ngx_test_config=1, ngx_dump_config=1
        'q'      → ngx_quiet_mode=1
        'p'      → ngx_prefix = 下一个 token
        'e'      → ngx_error_log = 下一个 token
        'c'      → ngx_conf_file = 下一个 token
        'g'      → ngx_conf_params = 下一个 token
        's'      → ngx_signal = 下一个 token
                   若 signal ∈ {stop,quit,reopen,reload}:
                       ngx_process = NGX_PROCESS_SIGNALLER
                   否则报错
        default  → 报错
```

它支持「带参选项紧跟（`-pfoo`）」与「带参选项空格分隔（`-p foo`）」两种写法——这就是为什么每个 `case` 里都有 `if (*p) { ... } else if (argv[++i]) { ... }` 两种取值路径。

注意 `-s` 的特殊性：它不仅设置 `ngx_signal`，还会把进程身份直接改成 `NGX_PROCESS_SIGNALLER`（见 [src/core/nginx.c:920-927](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L920-L927)）。这意味着一个「只想发信号」的 nginx 调用，从一开始就注定不会进入正常的服务循环。

`ngx_process_options` 的职责更琐碎但很关键，它要解决一个现实问题：**很多路径在配置文件里是相对路径，那「相对」的基准是什么？** 答案就是 prefix。它会把 `-p` 给出的目录补上末尾 `/`，作为 `cycle->prefix` 和 `cycle->conf_prefix`；如果没有 `-p`，就用编译期 `NGX_PREFIX` 或当前工作目录。然后把 `-c` 的配置文件名拼成绝对路径存进 `cycle->conf_file`（没有 `-c` 就用默认的 `NGX_CONF_PATH`）。最后还有一个重要细节：如果是 `-t` 测试模式，它会把日志级别临时降到 `NGX_LOG_INFO`，避免测试时打印太多调试噪音。

#### 4.1.3 源码精读

`ngx_get_options` 的完整实现见 [src/core/nginx.c:801-944](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L801-L944)。关键片段（`-t` 与 `-s` 两条分支）：

```c
// src/core/nginx.c:835-842  ——  -t / -T 设置测试标志
case 't':
    ngx_test_config = 1;
    break;

case 'T':
    ngx_test_config = 1;
    ngx_dump_config = 1;
    break;
```

```c
// src/core/nginx.c:908-930  ——  -s 设置信号并把进程标记为 SIGNALLER
case 's':
    if (*p) {
        ngx_signal = (char *) p;
    } else if (argv[++i]) {
        ngx_signal = argv[i];
    } else {
        ngx_log_stderr(0, "option \"-s\" requires parameter");
        return NGX_ERROR;
    }

    if (ngx_strcmp(ngx_signal, "stop") == 0
        || ngx_strcmp(ngx_signal, "quit") == 0
        || ngx_strcmp(ngx_signal, "reopen") == 0
        || ngx_strcmp(ngx_signal, "reload") == 0)
    {
        ngx_process = NGX_PROCESS_SIGNALLER;
        goto next;
    }
```

`ngx_process_options` 把这些值落地到 cycle，见 [src/core/nginx.c:989-1090](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L989-L1090)。其中 `-t` 时降低日志级别的代码：

```c
// src/core/nginx.c:1085-1087
if (ngx_test_config) {
    cycle->log->log_level = NGX_LOG_INFO;
}
```

而所有被 `ngx_get_options` 写入的「文件级全局变量」就声明在 [src/core/nginx.c:183-190](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L183-L190)：

```c
static ngx_uint_t   ngx_show_help;
static ngx_uint_t   ngx_show_version;
static ngx_uint_t   ngx_show_configure;
static u_char      *ngx_prefix;
static u_char      *ngx_error_log;
static u_char      *ngx_conf_file;
static u_char      *ngx_conf_params;
static char        *ngx_signal;
```

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `ngx_get_options` 里每个 `case` 对应的真实输出。

**操作步骤**：

1. 用上一讲（u1-l2）的方法编译出一个 nginx 二进制（比如 `objs/nginx`）。
2. 依次运行下面四条命令，观察输出差异：
   ```bash
   ./objs/nginx -v
   ./objs/nginx -V
   ./objs/nginx -?
   ./objs/nginx -h
   ```
3. 再运行一条「组合」命令，确认带参选项的两种写法等价：
   ```bash
   ./objs/nginx -t -c /tmp/a.conf      # 空格分隔
   ./objs/nginx -tc /tmp/a.conf        # 字符紧跟（注意是 -t 后再跟 -c 紧跟形式）
   ```

**需要观察的现象**：

- `-v` 只打印一行版本；`-V` 还会打印 `configure arguments:`（即编译开关），对应 `ngx_show_configure` 标志。
- `-?` 和 `-h` 会额外打印一整段 `Usage:` 帮助文本，对应 `ngx_show_help` 标志。

**预期结果**：`-v` 的输出来自 [src/core/nginx.c:394](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L394) 的 `ngx_show_version_info()`，`-V` 多出的 configure 段来自同函数的 [src/core/nginx.c:454](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L454)。如果你没有编译好的二进制，这一步标注为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `-s reload` 之后，`main()` 不会进入 master 进程循环？

> **答案**：因为 `ngx_get_options` 在处理 `-s` 时直接把 `ngx_process = NGX_PROCESS_SIGNALLER`（[nginx.c:925](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L925)）。而 `main()` 在进入进程循环之前，会先在 [nginx.c:329-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L331) 检测到 `ngx_signal` 非空，于是 `return ngx_signal_process(...)` 直接退出，根本到不了末尾的分支。

**练习 2**：如果用户同时写了 `-t` 和 `-v`（`nginx -vt`），最终会怎样？

> **答案**：`ngx_get_options` 会同时设置 `ngx_test_config=1` 和 `ngx_show_version=1`。`main()` 在 [nginx.c:216-222](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L216-L222) 先打印版本信息，由于此时 `ngx_test_config` 为真，**不会**提前 return，而是继续往下走完整配置测试流程，最后在 [nginx.c:303-327](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L303-L327) 打印测试结果并 return 0。

---

### 4.2 继承监听套接字：ngx_add_inherited_sockets（平滑升级的钥匙）

#### 4.2.1 概念说明

设想一个生产场景：你有一个跑了很久的 nginx，监听着 80 和 443 端口，每秒处理上万请求。现在你想把它升级到新版本二进制。难点在于——**新进程不能重新 `bind(80)`，因为旧进程还占着这个端口；你又不能先停旧的，否则会有停机时间**。

nginx 的解法非常巧妙：让**旧进程把已经 `bind` 好的监听套接字的文件描述符（fd）编号，通过环境变量传给新进程**。新进程启动时，直接把这些现成的 fd 拿来用，于是新旧进程在同一小段时间内共享同样的监听端口，旧进程处理完存量连接后再退出——这就是「零停机二进制升级（binary upgrade）」。

负责在新进程一侧「接收」这些 fd 的函数，就是 `ngx_add_inherited_sockets`。负责在旧进程一侧「发送」这些 fd 的，是同文件里的 `ngx_exec_new_binary`。

承载 fd 列表的环境变量名是 `NGINX`，由宏 `NGINX_VAR` 定义，见 [src/core/nginx.h:22](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h#L22)：

```c
#define NGINX_VAR          "NGINX"
```

#### 4.2.2 核心流程

**接收端（新进程，`ngx_add_inherited_sockets`）**：

```
inherited = getenv("NGINX")
若为空 → 普通启动，直接返回 NGX_OK（最常见路径）
否则：
    初始化 cycle->listening 数组
    按分隔符 ':' 或 ';' 切分字符串，逐段 ngx_atoi 得到 fd 编号 s
    对每个 s：往 cycle->listening push 一个 ngx_listening_t，填 fd 和 inherited=1
    全局置 ngx_inherited = 1
    调用 ngx_set_inherited_sockets(cycle)：把继承来的 fd 重新设置
    （如 nonblocking、地址获取等），让它能正常进入事件循环
```

例如环境变量 `NGINX=6;7;8;` 表示继承了 fd 6、7、8 三个监听套接字。

**发送端（旧进程，`ngx_exec_new_binary`）** 在收到升级信号（SIGUSR2）时执行，核心是把当前所有监听 fd 拼成 `NGINX=fd1;fd2;...` 放进子进程环境，然后 `fork`+`exec` 一个新二进制：

```c
// src/core/nginx.c:728-736  ——  把每个监听 fd 编号追加到 NGINX 环境变量
p = ngx_cpymem(var, NGINX_VAR "=", sizeof(NGINX_VAR));

ls = cycle->listening.elts;
for (i = 0; i < cycle->listening.nelts; i++) {
    if (ls[i].ignore) {
        continue;
    }
    p = ngx_sprintf(p, "%ud;", ls[i].fd);
}
```

#### 4.2.3 源码精读

`ngx_add_inherited_sockets` 完整实现见 [src/core/nginx.c:459-516](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L459-L516)。普通启动时它几乎什么也不做——关键是开头这个判断：

```c
// src/core/nginx.c:466-470
inherited = (u_char *) getenv(NGINX_VAR);

if (inherited == NULL) {
    return NGX_OK;
}
```

绝大多数情况下 `getenv("NGINX")` 返回 NULL，函数立即返回，这就是「普通启动」。只有在新二进制被旧 master 通过 `ngx_exec_new_binary` 拉起来时，这个环境变量才会存在，函数才会进入解析分支：

```c
// src/core/nginx.c:482-504  ——  按 ':' 或 ';' 切分，把每个 fd 填进 listening 数组
for (p = inherited, v = p; *p; p++) {
    if (*p == ':' || *p == ';') {
        s = ngx_atoi(v, p - v);
        ...
        v = p + 1;

        ls = ngx_array_push(&cycle->listening);
        ...
        ngx_memzero(ls, sizeof(ngx_listening_t));

        ls->fd = (ngx_socket_t) s;
        ls->inherited = 1;
    }
}
...
ngx_inherited = 1;

return ngx_set_inherited_sockets(cycle);
```

`ngx_inherited` 这个全局标志后面还会用到——`main()` 在决定是否守护进程化时会检查它（见 4.4 节）。发送端 `ngx_exec_new_binary` 见 [src/core/nginx.c:697-798](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L697-L798)。

#### 4.2.4 代码实践（源码阅读型）

升级流程不容易在本地安全地复现（涉及给正在运行的 master 发 SIGUSR2），所以本节采用「源码阅读 + 推理」型实践。

**实践目标**：通过阅读源码，把「环境变量 → fd → listening 数组」这条数据流画出来。

**操作步骤**：

1. 打开 [src/core/nginx.c:697-798](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L697-L798) 的 `ngx_exec_new_binary`，找到 `ngx_sprintf(p, "%ud;", ls[i].fd)`（[L735](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L735)），确认它写入的是十进制 fd 编号加分号。
2. 打开 [src/core/nginx.c:466](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L466)，确认新进程用 `getenv(NGINX_VAR)` 读回同一个字符串。
3. 跟踪 `ls->fd = (ngx_socket_t) s;` 与 `ls->inherited = 1;`（[L502-503](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L502-L503)），注意 `inherited` 标志的意义。

**需要观察的现象**（纯推理）：假设旧进程把字符串 `"NGINX=8;9;"` 写进子进程环境，子进程 `ngx_add_inherited_sockets` 会在 `cycle->listening` 数组里 push 两个元素，`fd` 分别为 8 和 9，`inherited` 都为 1，并把全局 `ngx_inherited` 置 1。

**预期结果**：你能用自己的话讲清楚——为什么新进程不需要重新 `bind` 端口。因为 socket 是进程级的资源，fd 编号在 `fork`/`exec` 后通过环境变量显式传递，新进程直接拿这些已绑定好的 fd 用即可（`exec` 不会关闭未设 `FD_CLOEXEC` 的 fd）。

> 完整的升级时序（SIGUSR2 → fork 新 master → 旧 master 退场）属于 u4-l1《master/worker 进程循环》的内容，本节只聚焦 fd 的「交接」这一环。

#### 4.2.5 小练习与答案

**练习 1**：为什么用环境变量传递 fd，而不是用命令行参数或文件？

> **答案**：fd 编号必须在新进程自己的进程上下文里才有意义。`fork`/`execve` 时，子进程继承父进程的环境变量，且未关闭的 fd 默认也保留——两者天然配对。命令行参数能传同样的数字，但环境变量更隐蔽、不会被 `ps` 列出参数污染；文件则需要额外的磁盘 I/O。环境变量是 nginx 的选择。

**练习 2**：`ngx_inherited` 这个全局变量在 `main()` 后面会影响什么行为？

> **答案**：在 [nginx.c:349-359](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L349-L359)，`if (!ngx_inherited && ccf->daemon)` 才会执行 `ngx_daemon` 守护进程化。也就是说，通过继承启动的新进程会跳过一次 `ngx_daemon`（因为它已经是被 master fork 出来的、已经是守护形态了），并把 `ngx_daemonized = 1`。

---

### 4.3 创建并初始化 cycle：ngx_init_cycle

#### 4.3.1 概念说明

前面所有工作都在「搭脚手架」：解析命令行、初始化日志、接收继承套接字。真正「把配置文件读进来、把所有模块初始化好、把监听端口打开」的重头戏，发生在一个函数里——`ngx_init_cycle`。

`main()` 里调用它的那一行看似平淡：

```c
cycle = ngx_init_cycle(&init_cycle);
```

但它内部做了大量工作（这些细节是 u3-l2 的主题，本讲只列清单）：创建新的内存池、复制旧 cycle 的路径信息、调用 `ngx_conf_parse` **解析配置文件**、为每个模块调用 `create_conf`/`init_conf` 回调、注册共享内存、`ngx_open_listening_sockets` **打开监听端口**、`ngx_init_modules` 初始化模块。可以说，`ngx_init_cycle` 之前 nginx 还是「一个空壳进程」，它返回之后 nginx 才是「一个准备好服务的实例」。

#### 4.3.2 核心流程

`main()` 围绕 `ngx_init_cycle` 的处理：

```
1. ngx_preinit_modules(&init_cycle)        // 给模块编号 index、统计数量
2. cycle = ngx_init_cycle(&init_cycle)     // 真正创建并初始化
   if (cycle == NULL):
       if (ngx_test_config):
           打印 "configuration file ... test failed"
       return 1
3. if (ngx_test_config):                   // -t / -T 在这里短路
       打印 "configuration file ... test is successful"
       if (ngx_dump_config):  dump 出所有配置文件内容   // -T
       return 0
4. if (ngx_signal):                        // -s 在这里短路
       return ngx_signal_process(cycle, ngx_signal)
```

注意：**`ngx_init_cycle` 是「测试模式」和「信号模式」能够工作的前提**。即使是 `nginx -t`，也必须真正把配置解析一遍、把模块初始化一遍，才能判断配置「是否有效」。这正是为什么 `nginx -t` 不仅能查出语法错误，还能查出语义错误（比如 `init_conf` 阶段发现的非法值）。

#### 4.3.3 源码精读

模块预初始化在 [src/core/nginx.c:289-291](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L289-L291)，函数本体在 [src/core/ngx_module.c:25-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_module.c#L25-L39)（它给每个模块分配 `index`、记录模块名，并算出 `ngx_max_module`）。

`ngx_init_cycle` 的调用与失败处理见 [src/core/nginx.c:293-301](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L293-L301)：

```c
cycle = ngx_init_cycle(&init_cycle);
if (cycle == NULL) {
    if (ngx_test_config) {
        ngx_log_stderr(0, "configuration file %s test failed",
                       init_cycle.conf_file.data);
    }
    return 1;
}
```

`ngx_init_cycle` 的函数本体在 [src/core/ngx_cycle.c:38-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L38-L39)。其中两个对 `-t` 行为很关键的调用点：

- 解析配置文件：[src/core/ngx_cycle.c:286](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L286) 的 `ngx_conf_parse(&conf, &cycle->conf_file)`。
- 尝试打开监听端口：[src/core/ngx_cycle.c:632](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L632) 的 `ngx_open_listening_sockets(cycle)`。

> 也就是说，`nginx -t` 不仅解析配置、初始化模块，还会**实际尝试 `bind` 监听端口**。如果端口已被占用或权限不足，`-t` 同样会报错失败。这是很多人容易忽略的一点。

`-t` 成功后的短路返回见 [src/core/nginx.c:303-327](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L303-L327)；`-s` 的短路返回见 [src/core/nginx.c:329-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L331)。

#### 4.3.4 代码实践

**实践目标**：用 `nginx -t` 触发 `ngx_init_cycle` 的失败分支，观察报错从哪里来。

**操作步骤**：

1. 复制一份能正常工作的配置，故意把 `worker_processes` 改成一个非法值（比如 `worker_processes abc;`）。
2. 运行 `./objs/nginx -t -c /tmp/bad.conf`，观察输出。
3. 再造一个「语义错」：写一个 `http { server { listen 1.2.3.4:80; } }`（绑定一个本机没有的 IP），运行 `nginx -t`。
4. 最后恢复正确配置，运行 `nginx -t -T`（带 `-T`），观察它会 dump 出所有被 include 的配置文件内容。

**需要观察的现象**：

- 第 2 步：报错信息类似 `invalid value "abc"`，且最后会打印 `configuration file ... test failed`，这正是 [nginx.c:296](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L296)。
- 第 3 步：报 `bind() to ... failed`，因为 [ngx_cycle.c:632](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L632) 的 `ngx_open_listening_sockets` 失败。
- 第 4 步：会打印 `# configuration file xxx:` 后跟文件内容，对应 [nginx.c:309-324](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L309-L324) 的 dump 循环。

**预期结果**：所有错误信息都能在源码中找到出处；`nginx -t` 的退出码：成功为 0，失败为 1。如果本地未编译二进制，标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`nginx -t` 失败时，错误信息 `configuration file ... test failed` 是由 `ngx_init_cycle` 自己打印的，还是由 `main()` 打印的？

> **答案**：由 `main()` 打印，见 [nginx.c:294-300](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L294-L300)。`ngx_init_cycle` 只负责返回 NULL 表示失败，具体的「test failed」措辞由 `main()` 在检测到 `ngx_test_config` 时输出。而具体的语法/语义错误原因（如 `invalid value`）则是在 `ngx_init_cycle` 内部的解析过程中更早打印的。

**练习 2**：为什么说「`nginx -t` 通过了，并不代表 `nginx` 一定能正常启动」？

> **答案**：`-t` 时 `ngx_process_options` 把日志级别降到了 `NGX_LOG_INFO`（[nginx.c:1085-1087](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1085-L1087)），并且 `-t` 不会调用 `ngx_configure_listening_sockets`（[ngx_cycle.c:636-638](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L636-L638) 显示它只在非测试模式调用），也不会 daemon 化、不会进入事件循环。运行期才暴露的问题（如运行时内存不足、上游不可达、动态模块加载顺序）`-t` 无法发现。

---

### 4.4 进程模型分支：ngx_single_process_cycle 与 ngx_master_process_cycle

#### 4.4.1 概念说明

走到 `main()` 的最后几行，所有准备工作都已就绪：配置已加载、监听端口已打开、信号已注册、pid 文件已写入。现在的问题是——**这个进程接下来要干什么？**

答案取决于「进程身份」。`main()` 用一个简洁的 `if-else` 收尾：

```c
// src/core/nginx.c:380-385
if (ngx_process == NGX_PROCESS_SINGLE) {
    ngx_single_process_cycle(cycle);
} else {
    ngx_master_process_cycle(cycle);
}
```

- **`NGX_PROCESS_SINGLE`（单进程模式）**：当前进程自己既当 master 又当 worker，直接跑事件循环处理请求。通常用于调试，或在资源极度受限的嵌入式场景。可通过配置 `master_process off;` 触发。
- **`NGX_PROCESS_MASTER`（master 模式）**：当前进程只做「管理」——fork 出若干 worker 进程，自己进入一个信号驱动循环，监控 worker 存活、处理 reload/upgrade/stop 等控制信号，**自己完全不处理 HTTP 请求**。这是生产环境的默认模式。

注意 `NGX_PROCESS_SIGNALLER`（`-s`）不会走到这里——它在 [nginx.c:329-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L331) 就已经 return 了。

#### 4.4.2 核心流程

**进程身份的最终确定**（在进入循环之前）：

```
默认: ngx_process == NGX_PROCESS_SINGLE (0)

main() 在拿到 ccf 后:
if (ccf->master && ngx_process == NGX_PROCESS_SINGLE):
    ngx_process = NGX_PROCESS_MASTER     // 默认配置 master_process on → 升级为 master

(若 -s，早已在前面 return，不会到这里)
```

也就是说，**只要配置里 `master_process` 是开的（默认就是开）**，即使 `ngx_process` 初始值是 SINGLE，也会被升级成 MASTER。这就是为什么默认启动总是 master 模式。

**两种循环的对照**：

| 维度 | `ngx_single_process_cycle` | `ngx_master_process_cycle` |
|---|---|---|
| 谁处理请求 | 当前进程自己 | fork 出来的 worker |
| 是否 fork worker | 否 | 是（`ngx_start_worker_processes`） |
| 主循环形态 | 直接 `ngx_process_events_and_timers` | `sigsuspend` 等信号 |
| 典型用途 | 调试、嵌入式 | 生产环境 |
| 定义位置 | process_cycle.c:279 | process_cycle.c:74 |

#### 4.4.3 源码精读

身份升级的判定见 [src/core/nginx.c:337-341](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L337-L341)：

```c
ccf = (ngx_core_conf_t *) ngx_get_conf(cycle->conf_ctx, ngx_core_module);

if (ccf->master && ngx_process == NGX_PROCESS_SINGLE) {
    ngx_process = NGX_PROCESS_MASTER;
}
```

进入循环前的「进程化准备」一段（信号、守护化、pid 文件、日志重定向）见 [src/core/nginx.c:343-378](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L343-L378)。其中守护进程化有一个和继承套接字相关的细节（呼应 4.2 节）：

```c
// src/core/nginx.c:349-359
if (!ngx_inherited && ccf->daemon) {
    if (ngx_daemon(cycle->log) != NGX_OK) {
        return 1;
    }
    ngx_daemonized = 1;
}

if (ngx_inherited) {
    ngx_daemonized = 1;
}
```

最终的分支见 [src/core/nginx.c:380-385](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L380-L385)。

两种循环的函数本体都在 `src/os/unix/ngx_process_cycle.c`：

- **master 模式** [ngx_master_process_cycle，process_cycle.c:74](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L74)：先屏蔽一批信号（[L87-102](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L87-L102)），调用 `ngx_start_worker_processes` fork worker（[L130-131](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L130-L131)），然后进入 `for ( ;; ) { sigsuspend(&set); ... }` 的信号驱动循环（[L139-163](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L139-L163)）。master 把自己「挂起」等信号，被信号唤醒后再去 reap 子进程、reload 配置或退出。
- **单进程模式** [ngx_single_process_cycle，process_cycle.c:279](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L279)：调用各模块的 `init_process` 回调（[L288-295](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L288-L295)），然后直接进入 `for ( ;; ) { ngx_process_events_and_timers(cycle); ... }`（[L297-300](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/os/unix/ngx_process_cycle.c#L297-L300)）——这就是真正处理连接和请求的事件循环。

> `ngx_process_events_and_timers` 是事件驱动的核心，整个第 5 单元（事件驱动核心）都在讲它。本节你只需要知道：单进程模式自己跑它，master 模式则由它 fork 出的 worker 跑它。

#### 4.4.4 代码实践

**实践目标**：用配置切换 single / master 两种模式，观察进程数差异。

**操作步骤**：

1. 准备一个最小配置 `single.conf`：
   ```nginx
   master_process off;
   daemon off;
   events { worker_connections 16; }
   http { server { listen 8080; } }
   ```
   （`daemon off;` 让它前台运行，方便观察；`master_process off;` 强制单进程。）
2. 在一个终端运行 `./objs/nginx -c /tmp/single.conf`，在另一个终端执行 `ps -ef | grep nginx`，数一下有几个 nginx 进程。
3. Ctrl-C 停止。把 `master_process off;` 改成 `master_process on;`（或直接删掉该行），把 `worker_processes` 显式设为 2，再次启动，再次 `ps`，数进程数。

**需要观察的现象**：

- 第 2 步：只有 **1 个** nginx 进程（即 `ngx_single_process_cycle`），它既是 master 也是 worker。
- 第 3 步：会有 **1 个 master + 2 个 worker = 3 个** nginx 进程（master 不处理请求，2 个 worker 处理）。

**预期结果**：进程数差异直接印证了 `nginx.c:380-385` 的 `if-else` 分支。若本地未编译，标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：默认情况下 `ngx_process` 的初始值是 `NGX_PROCESS_SINGLE`，为什么生产环境跑起来却是 master 模式？

> **答案**：因为 [nginx.c:339-341](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L339-L341) 在进入循环前判断：只要 `ccf->master`（即配置 `master_process`，默认 on）为真且当前是 SINGLE，就升级为 MASTER。所以「SINGLE」这个初始值只是一个待定的中间状态。

**练习 2**：`nginx -s reload` 这条命令执行时，它自己会 fork worker 吗？

> **答案**：不会。`-s` 把进程标记为 `NGX_PROCESS_SIGNALLER`，`main()` 在 [nginx.c:329-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L331) 就 `return ngx_signal_process(...)` 退出了，根本到不了进程循环。真正的 reload（重新解析配置、fork 新 worker）是由**已经在运行的那个 master 进程**在收到 SIGHUP 后完成的（见下一讲 u4-l1）。

**练习 3**：`ngx_signal_process` 是如何找到「那个已经在运行的 master」的？

> **答案**：它读取 pid 文件（`ccf->pid`），从里面拿到 master 的 PID，再对该 PID 发送信号。完整实现见 [ngx_cycle.c:1096](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1096) 起的 `ngx_signal_process`：`ngx_open_file(ccf->pid)` → `ngx_read_file` → `ngx_atoi` 得到 pid → `ngx_os_signal_process(cycle, sig, pid)`。这也解释了为什么 pid 文件路径必须正确，否则 `nginx -s` 会找不到目标进程。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个综合任务（这就是本讲规格里要求的实践任务）。

### 任务：标注 main() 的启动步骤，并解释 `nginx -t` 与 `nginx -s reload`

**步骤 1——画一张 main() 的「步骤地图」**。打开 [src/core/nginx.c:196-388](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L196-L388)，按本讲第 4 节开头的 6 阶段划分（A 命令行与环境、B 临时 cycle、C 继承套接字+预初始化、D 创建 cycle、E 进程化准备、F 进程循环），在源码里给每个阶段标上起止行号，并写明该阶段调用的关键函数。

**步骤 2——追踪 `nginx -t`**。回答：从命令行到退出，`-t` 依次经过哪些关键调用？它为什么不会进入事件循环？

> 参考答案要点：
> - `ngx_get_options` 设置 `ngx_test_config=1`（[nginx.c:835-837](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L835-L837)）。
> - `ngx_process_options` 把日志级别降到 INFO（[nginx.c:1085-1087](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L1085-L1087)）。
> - 仍会调用 `ngx_init_cycle`，其中 `ngx_conf_parse`（[ngx_cycle.c:286](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L286)）解析配置、`ngx_open_listening_sockets`（[ngx_cycle.c:632](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L632)）尝试绑定端口。
> - 成功后在 [nginx.c:303-327](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L303-L327) 打印成功信息并 `return 0`，**在到达 [nginx.c:380-385](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L380-L385) 的进程循环之前就已退出**，因此不进入事件循环、不对外服务。

**步骤 3——追踪 `nginx -s reload`**。回答：这条命令本身做了 reload 吗？真正的 reload 发生在哪里？

> 参考答案要点：
> - `ngx_get_options` 设置 `ngx_signal="reload"` 并把 `ngx_process = NGX_PROCESS_SIGNALLER`（[nginx.c:920-927](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L920-L927)）。
> - `main()` 在 [nginx.c:329-331](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.c#L329-L331) 执行 `return ngx_signal_process(cycle, "reload")`。
> - `ngx_signal_process`（[ngx_cycle.c:1096](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_cycle.c#L1096)）读 pid 文件拿到正在运行的 master 的 PID，给它发送 reload 信号（SIGHUP），然后这条命令自己就退出了。
> - **真正的 reload（重新 `ngx_init_cycle` 解析配置、fork 新 worker、优雅关闭旧 worker）发生在那个已经在运行的 master 进程内部**，由 `ngx_master_process_cycle` 的信号循环处理。本讲不讲其内部细节，留给 u4-l1。

**步骤 4（可选，需本地环境）**：实际运行 `nginx -t` 和 `nginx -s reload`，用 `strace -f ./objs/nginx -t 2>&1 | grep -E 'open|bind|read'` 观察它打开/绑定了哪些资源，印证「`-t` 会真的尝试绑定端口」这一结论。如无本地环境，标注为「待本地验证」。

---

## 6. 本讲小结

- `main()` 是一段**线性、无复杂分支**的初始化流程，任何一步失败就 `return 1`；可按「命令行→临时 cycle→继承套接字→创建 cycle→进程化准备→进程循环」6 个阶段理解。
- 命令行解析分两步：`ngx_get_options` 把开关写进一组全局变量，`ngx_process_options` 再把它们落地到 `cycle` 的 `prefix`/`conf_file`/`error_log` 等字段并补默认值。
- `-s` 选项会直接把进程身份设为 `NGX_PROCESS_SIGNALLER`，注定不会进入正常服务循环。
- **继承监听套接字**（`ngx_add_inherited_sockets`）通过环境变量 `NGINX=fd1;fd2;...` 在新旧进程间传递已绑定的 fd，是实现零停机二进制升级的钥匙；普通启动时 `getenv("NGINX")` 为空，函数立即返回。
- `ngx_init_cycle` 是真正的「重头戏」：解析配置、初始化模块、打开监听端口；`nginx -t` 的本质就是「跑一遍 `ngx_init_cycle` 然后退出」，因此它不仅能查语法错误，还会实际尝试 `bind` 端口。
- `main()` 末尾按 `ngx_process` 分流：默认（`master_process on`）被升级为 `NGX_PROCESS_MASTER` 走 `ngx_master_process_cycle`；`master_process off` 时保持 `NGX_PROCESS_SINGLE` 走 `ngx_single_process_cycle`。`-v`/`-t`/`-s` 都在这之前就已短路返回。

---

## 7. 下一步学习建议

本讲把 `main()` 的「入口主线」走通了，但沿途调用的一些函数我们刻意只点了名、没展开。建议按下面的顺序继续：

1. **深入 `ngx_init_cycle`**——去看 u3-l2《cycle 生命周期》。那里会讲清楚配置是如何被 `ngx_conf_parse` 一步步解析、模块的 `create_conf`/`init_conf` 回调如何被调用、监听端口如何被打开。这是理解「配置如何变成运行时结构」的关键。
2. **深入配置解析器**——去看 u3-l1《配置文件解析器 ngx_conf_parse》，理解一条指令从文本到 `set` 回调的完整调用链。
3. **深入 master/worker 进程循环**——去看 u4-l1《master/worker 进程循环》，搞清楚 master 收到 SIGHUP（reload）、SIGUSR2（升级）、SIGQUIT（优雅停止）后分别如何协调 worker。本讲里「真正的 reload 发生在 master 内部」这句话，在那里会得到完整解释。
4. **动手实验（可选）**：在阅读 u4-l1 之后，回头尝试一次完整的二进制升级（给 master 发 SIGUSR2，观察新进程通过 `NGINX` 环境变量继承 fd），把本讲 4.2 节的理论变成可观察的现象。
