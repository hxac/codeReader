# 源码目录结构与分层总览

## 1. 本讲目标

本讲是「源码阅读地图」的奠基篇。读完本讲，你应当能够：

- 说出 `src/` 下 `core`、`event`、`http`、`stream`、`mail`、`os` 各子目录分别承担什么职责。
- 理解 nginx 源码是「自底向上分层」的：协议层依赖事件层，事件层依赖基础设施层，基础设施层依赖操作系统抽象层，依赖方向不可倒置。
- 看懂 `auto/sources` 这个构建清单如何用纯文本描述「整个源码树的组织」，并能用它快速定位任意功能所在的目录。
- 拿到一个陌生的功能名词（例如「漏桶限流」「epoll 后端」「QUIC」），能立刻判断它的源码应该在哪个子目录下。

本讲**不深入任何单个文件的实现**——那是后续讲义的任务。本讲只解决一个问题：**建立源码全景图，让你在浩瀚的 C 文件里不再迷路。**

承接上一篇 [u1-l2 从源码构建与运行 nginx](u1-l2-build-and-run.md)：上一篇讲了 `auto/` 构建脚本如何把源码编译成 `objs/nginx` 二进制；本讲把视角从「怎么编译」转到「被编译的东西是怎么组织的」。

## 2. 前置知识

本讲需要你已具备以下认知（来自 [u1-l1 项目定位与核心概念](u1-l1-project-overview.md)）：

- **模块（module）**：nginx 的能力以模块为单位组织。模块分为静态模块（编译进二进制）和动态模块（编译成 `.so`，运行时加载）。
- **配置驱动**：nginx 的行为由配置文件决定，配置由编译进来的模块解释。解析入口是 `ngx_conf_parse`。
- **master/worker 进程模型**：master 管理配置和 worker，worker 以事件驱动方式处理请求。

此外需要一个直觉性的概念——**分层架构（layered architecture）**：

> 一个大型程序如果像一团乱麻，任何修改都牵一发动全身。分层的作用是规定「谁可以调用谁」：上层可以调用下层，下层不能反向调用上层。这样每一层都能独立替换。例如把 `src/os/unix` 换成 `src/os/win32`，上面的 `core`/`event`/`http` 几乎不用改。

你还需要知道一个 C 工程常识：源文件（`.c`）和头文件（`.h`）。nginx 约定：每个子系统的公开接口写在 `ngx_xxx.h`，实现写在 `ngx_xxx.c`。构建清单里成对出现 `XXX_DEPS`（头文件依赖）和 `XXX_SRCS`（源文件）就是这个原因。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 / 目录 | 作用 | 在本讲中的角色 |
| --- | --- | --- |
| `src/core/nginx.h` | 定义版本号等全局宏 | 「被所有层 include」的顶层头文件，证明 core 是地基 |
| `auto/sources` | 列出「总要编译」的核心/事件/OS 文件清单 | 本讲的**主索引**，源码分层的权威目录 |
| `auto/modules` | 按开关动态组装协议层模块，生成 `objs/ngx_modules.c` | 展示各层如何「拍平」成一个运行时数组 |
| `conf/nginx.conf` | 默认配置文件示例 | 展示配置块的 `events{}`/`http{}` 与源码层的对应 |
| `src/core/` | 基础设施（内存池、字符串、容器、日志、cycle……） | 最底层「标准库」 |
| `src/event/` | 事件驱动（事件框架、多路复用后端、SSL、QUIC……） | 性能核心 |
| `src/http/`、`src/stream/`、`src/mail/` | 三个平行协议子系统 | 协议层 |
| `src/os/unix/`、`src/os/win32/` | 操作系统抽象 | 跨平台胶水层 |

## 4. 核心概念与源码讲解

### 4.1 整体分层与构建清单全景

#### 4.1.1 概念说明

nginx 的源码不是按功能随意堆放的，而是严格分层。自顶向下（从离用户最近到离硬件最近）一共五层：

```
┌─────────────────────────────────────────────┐
│  协议层  src/http  src/stream  src/mail      │  解释具体协议(HTTP/TCP/UDP/邮件)
├─────────────────────────────────────────────┤
│  事件层  src/event                           │  事件驱动调度 + 多路复用后端
├─────────────────────────────────────────────┤
│  基础设施层  src/core                         │  内存池/字符串/容器/日志/cycle
├─────────────────────────────────────────────┤
│  操作系统抽象层  src/os/unix  src/os/win32    │  封装 OS 差异
└─────────────────────────────────────────────┘
        依赖方向：上层 → 下层（不可倒置）
```

**依赖方向**是理解整个源码树的钥匙：协议层会 `#include` 事件层和基础设施层的头文件，但反过来不行。这意味着你可以单独理解 `src/core` 而不需要懂 HTTP。

那么「哪些文件属于哪一层」这件事，写在哪里？答案就是构建清单 `auto/sources`。上一篇你已知道 nginx 的构建系统是手写 shell 脚本；本讲要强调的是：**`auto/sources` 不只是构建脚本，它本质上是源码树的「结构化目录」**——它用 shell 变量精确记录了每一层包含哪些文件。

#### 4.1.2 核心流程：从构建清单到运行时模块表

nginx 启动时，所有「模块」被组织进一个一维数组 `ngx_modules[]` 供 `main()` 遍历初始化。这个数组的生成过程揭示了分层的本质：

```text
auto/sources            定义「总要编」的三层：
   │                      CORE_MODULES / EVENT_MODULES / UNIX_SRCS(含 os)
   │
auto/modules            按开关动态追加「可选」协议层：
   │                      HTTP_MODULES  (当 --with-http 等启用)
   │                      MAIL_MODULES  (当启用 mail)
   │                      STREAM_MODULES(当启用 stream)
   │
   └── 把上述所有层的模块名拼接成一个 $modules 列表
        │
        └── 生成 objs/ngx_modules.c，其中：
              ngx_module_t *ngx_modules[] = { &ngx_core_module, ..., NULL };
        │
nginx 运行时            main() 遍历 ngx_modules[]，逐个调用 init_module
```

注意一个关键区分（上一篇已提及，这里给出依据）：

- **`auto/sources`** 只列「无条件总要编译」的层：`CORE`、`EVENT`、`OS`。这些是 nginx 能跑起来的最小集合。
- **`auto/modules`** 列「按配置开关决定是否编译」的层：`HTTP`、`MAIL`、`STREAM` 及其下属的可选模块。

#### 4.1.3 源码精读

先看「地基」头文件 `src/core/nginx.h`。它只定义版本号等极少数宏，是几乎所有 `.c` 文件都会间接 include 的顶层头文件——这正说明 core 位于依赖最底层：

[src/core/nginx.h:12-14](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/nginx.h#L12-L14) —— 定义 nginx 版本号。当前版本为 `1.31.3`（开发版，`nginx_version` 数值 `1031003`）。本系列讲义全部基于此版本。

接着看本讲的主索引 `auto/sources`。它用 shell 变量把 core 层的依赖文件一行行列出：

[auto/sources:6](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L6) —— `CORE_MODULES`：core 层只有三个「框架级」模块——`ngx_core_module`（核心）、`ngx_errlog_module`（错误日志）、`ngx_conf_module`（配置解析）。这是 nginx 最不可或缺的三件套。

[auto/sources:10-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L10-L45) —— `CORE_DEPS`：core 层全部头文件清单。从 `nginx.h`、`ngx_config.h` 到 `ngx_palloc.h`（内存池）、`ngx_buf.h`、`ngx_rbtree.h`（红黑树）、`ngx_cycle.h`、`ngx_conf_file.h`……这正是后续 u2 单元要逐个精读的清单。

事件层的清单同样在这里（事件层也是「总要编」的）：

[auto/sources:86-88](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L86-L88) —— `EVENT_MODULES` 只有 `ngx_events_module` 和 `ngx_event_core_module` 两个框架模块；`EVENT_INCS` 则把头文件搜索路径扩展到 `src/event`、`src/event/modules`、`src/event/quic` 三个子目录。

[auto/sources:163-185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L163-L185) —— `UNIX_SRCS`：注意它的值是 `"$CORE_SRCS $EVENT_SRCS" 再拼上 src/os/unix/ 下的文件`。这一行直接展示了「层的组合」——Unix 版本的源文件集合 = core 层 + event 层 + os 抽象层。

而协议层（HTTP/MAIL/STREAM）不在 `auto/sources` 里，它们在 `auto/modules` 里按开关组装。HTTP 的清单开头是这样：

[auto/modules:59-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L59-L60) —— `HTTP_MODULES=` 置空，只有当 `if [ $HTTP = YES ]` 成立时才开始填充。mail 与 stream 同理（[auto/modules:1007](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1007) 的 `MAIL_MODULES=`、[auto/modules:1088](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1088) 的 `STREAM_MODULES=`）。

最后，所有层的模块名被「拍平」进一个数组。这是 `auto/modules` 末尾生成 `objs/ngx_modules.c` 的代码：

[auto/modules:1553-1578](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1553-L1578) —— 用 `for mod in $modules` 循环，把每层的每个模块名写成 `extern ngx_module_t <名>;` 声明，再写入 `ngx_module_t *ngx_modules[] = { &<名>, ..., NULL };`。这一段是「分层」到「运行时一维表」的最终汇聚点。

配置文件侧，分层也清晰可辨。默认配置 `conf/nginx.conf` 里：

[conf/nginx.conf:12-14](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf#L12-L14) —— `events { worker_connections 1024; }` 块由 **事件层** 的 `ngx_events_module` / `ngx_event_core_module` 解释（对应 `src/event`）。

[conf/nginx.conf:17](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf#L17) —— `http { ... }` 块由 **协议层** 的 `ngx_http.c` 框架解释（对应 `src/http`）。

也就是说：**配置文件里的每个顶层块，几乎都对应源码树里的一层。** 这是「配置驱动」与「源码分层」的天然对应关系。

#### 4.1.4 代码实践

实践目标：验证「配置块 ↔ 源码层」的对应关系。

操作步骤：

1. 打开 [conf/nginx.conf](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/conf/nginx.conf)，找到两个顶层块 `events {}` 与 `http {}`。
2. 想一想：如果有人写一个 `stream {}` 块，应该由哪个源码子目录解释？哪个目录解释 `mail {}`？（提示：看子目录名。）
3. 在仓库根目录执行 `ls src/`，确认 `stream` 与 `mail` 子目录确实存在。
4. 执行 `grep -n "NGINX_VERSION" src/core/nginx.h`，确认本讲义引用的版本号与你本地一致。

需要观察的现象：配置块的名称（`events`/`http`/`stream`/`mail`）与 `src/` 子目录名一一对应。

预期结果：你会发现 nginx 的「配置语法」和「源码分层」是同一套词汇表——这是它架构一致性的体现。

待本地验证：第 4 步的版本号输出（应为 `1.31.3`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `HTTP_MODULES` 定义在 `auto/modules` 里，而 `CORE_MODULES` 定义在 `auto/sources` 里？

**参考答案**：`core` 层是 nginx 能运行的最小必备集合，无条件总要编译，所以放进 `auto/sources`（「总要编」的清单）；而 `http` 层是可选的协议层，是否编译取决于 configure 开关（如 `--without-http`），所以放进 `auto/modules`（「按开关组装」的清单）。

**练习 2**：`ngx_modules[]` 数组里既有 core 模块也有 http 模块，它是「分层」的还是「扁平」的？

**参考答案**：运行时是**扁平**的——一个一维指针数组，不保留层信息。分层只存在于「编译期」的构建清单（`auto/sources`/`auto/modules`）和「源码目录」中；一旦编译完成，所有模块都被拍平进 `ngx_modules[]`，由 `main()` 统一遍历。

**练习 3**：`src/core/nginx.h` 里没有 `#include` 任何 http 相关头文件，这说明了什么？

**参考答案**：说明依赖方向是单向的——core 不依赖 http。这正是分层的意义：你可以不编译 http 模块（`--without-http`），core 依然完整。

### 4.2 src/core：一切的基础设施

#### 4.2.1 概念说明

`src/core` 是 nginx 的「自研标准库」。它不处理任何网络协议，只提供上层都需要的通用工具：

- **内存管理**：内存池（`ngx_palloc`）、slab 分配器（`ngx_slab`）、共享内存。
- **基础类型**：长度前缀字符串 `ngx_str_t`（`ngx_string`）、时间缓存（`ngx_times`）。
- **容器**：动态数组（`ngx_array`）、链表（`ngx_list`）、双向队列（`ngx_queue`）、红黑树（`ngx_rbtree`）、哈希表（`ngx_hash`）、radix 树（`ngx_radix_tree`）。
- **数据流**：缓冲区 `ngx_buf_t` 与输出链（`ngx_buf`、`ngx_output_chain`）。
- **框架骨架**：全局上下文 `ngx_cycle_t`（`ngx_cycle`）、配置解析器（`ngx_conf_file`）、模块系统（`ngx_module`）、连接抽象（`ngx_connection`）。
- **辅助**：日志（`ngx_log`）、DNS 解析器（`ngx_resolver`）、文件信息缓存（`ngx_open_file_cache`）、syslog（`ngx_syslog`）。

这些都属于 u2 单元的精读对象。本讲只需建立「core = 标准库」的印象，并记住：**core 不 include 任何 event/http 头文件**。

#### 4.2.2 核心流程：CORE_DEPS 的内部分组

`auto/sources` 的 `CORE_DEPS` 列出了 core 的全部头文件，可大致分为四组：

```text
CORE_DEPS (auto/sources:10-45)
├── 全局配置:  nginx.h, ngx_config.h, ngx_core.h
├── 基础设施:  ngx_log.h, ngx_palloc.h, ngx_times.h
├── 容器类型:  ngx_array.h, ngx_list.h, ngx_hash.h, ngx_queue.h,
│             ngx_rbtree.h, ngx_radix_tree.h, ngx_rwlock.h, ngx_slab.h
├── 数据/IO:   ngx_buf.h, ngx_string.h, ngx_parse.h, ngx_parse_time.h,
│             ngx_inet.h, ngx_file.h
└── 框架骨架:  ngx_connection.h, ngx_cycle.h, ngx_conf_file.h,
              ngx_module.h, ngx_resolver.h, ngx_open_file_cache.h, ...
```

#### 4.2.3 源码精读

[auto/sources:10-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L10-L45) —— `CORE_DEPS`：core 层头文件总清单。把它当作 core 子目录的「目录页」来读。

[auto/sources:48-83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L48-L83) —— `CORE_SRCS`：core 层源文件（`.c`）总清单。注意第一项是 `src/core/nginx.c`——它就是整个程序的 `main()` 所在文件，下一篇 u1-l4 会精读。

一个重要的「跨层入口」值得注意：core 里有一个 `nginx.c`，它包含了 `main()`，而 `main()` 会初始化所有层。也就是说，**程序的入口点在 core 层，但它会向上调用协议层注册的模块回调**。这并不破坏分层——分层约束的是「头文件依赖方向」，而不是「函数调用方向」。

#### 4.2.4 代码实践

实践目标：把 core 的文件按职责分类，建立肌肉记忆。

操作步骤：

1. 执行 `ls src/core/`，把列出的文件名与 `auto/sources:48-83` 的 `CORE_SRCS` 对照，确认一致。
2. 给每个文件贴一个标签，归入下列五类之一：**内存**、**容器**、**字符串/解析**、**数据流/IO**、**框架骨架**。例如：
   - `ngx_palloc.c` → 内存
   - `ngx_rbtree.c` → 容器
   - `ngx_string.c` → 字符串/解析
   - `ngx_buf.c` → 数据流/IO
   - `ngx_cycle.c` → 框架骨架
3. 数一下每类各有多少文件。

需要观察的现象：core 里「容器」和「框架骨架」类文件占比很大，这正是 nginx「自研一切」风格的体现。

预期结果：你会得到一张 core 内部职责分布表，这就是后续 u2 单元的阅读顺序建议。

#### 4.2.5 小练习与答案

**练习 1**：如果要找 nginx 的「红黑树」实现，应该看哪个文件？

**参考答案**：`src/core/ngx_rbtree.c`（实现在 `.c`，接口在 `ngx_rbtree.h`）。红黑树在 core 层，被定时器（事件层）等广泛使用。

**练习 2**：`ngx_cycle.c` 属于 core，但它管理的 `ngx_cycle_t` 里包含所有模块的配置。这是否违反「core 不依赖协议层」？

**参考答案**：不违反。`ngx_cycle_t` 通过**泛型指针数组**（`void **conf`）持有各模块配置，core 只负责「分配和遍历这些指针」，并不需要知道 http 配置结构体的具体内容。这是 C 语言里实现「框架不依赖业务」的常用手法。

### 4.3 src/event：事件驱动核心

#### 4.3.1 概念说明

nginx 高性能的秘诀在 `src/event`：它用「事件驱动 + 非阻塞 I/O + 多路复用」替代了「一个连接一个线程」的传统模型。一个 worker 进程能同时处理数万连接，全靠这一层。

`src/event` 内部又分四个部分：

- **事件框架**：`ngx_event.c`（事件抽象与主循环）、`ngx_event_timer.c`（定时器，基于红黑树）、`ngx_event_posted.c`（延迟处理队列）、`ngx_event_accept.c`（接受新连接）。
- **多路复用后端**：在子目录 `src/event/modules/`，按操作系统提供不同实现——Linux 用 `ngx_epoll_module.c`，FreeBSD/macOS 用 `ngx_kqueue_module.c`，还有通用的 `poll`/`select`。
- **SSL/TLS 集成**：`ngx_event_openssl*.c`，把 OpenSSL 的阻塞式握手改造成事件驱动。
- **QUIC/HTTP3 传输**：在子目录 `src/event/quic/`，nginx 自带的 QUIC 协议栈。
- **辅助**：`ngx_event_connect.c`（主动发起连接，upstream 用）、`ngx_event_pipe.c`（大块响应缓冲）。

#### 4.3.2 核心流程：后端的选择

事件框架是固定的（`ngx_events_module` + `ngx_event_core_module`），但具体用哪个多路复用后端取决于 OS 和配置：

```text
configure 阶段：检测系统能力
   ├── Linux  → 默认编译 ngx_epoll_module
   ├── FreeBSD/Darwin → 默认编译 ngx_kqueue_module
   └── 通用   → 总会编译 ngx_poll_module / ngx_select_module 作为兜底

运行时：events { use epoll; }  或自动选择一个可用的后端
```

#### 4.3.3 源码精读

[auto/sources:86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L86) —— `EVENT_MODULES="ngx_events_module ngx_event_core_module"`：事件框架本身只有两个模块。其余多路复用后端是单独的可选模块。

[auto/sources:90-103](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L90-L103) —— `EVENT_DEPS` 与 `EVENT_SRCS`：事件框架的头文件和源文件。注意 `EVENT_INCS`（[auto/sources:88](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L88)）把 `src/event/modules` 和 `src/event/quic` 都加进了搜索路径。

[auto/sources:106-127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L106-L127) —— 这一段罗列了所有可选的多路复用后端模块（`select`/`poll`/`kqueue`/`epoll`/`devpoll`/`eventport`/`iocp`）。每个后端都形如 `EPOLL_MODULE=ngx_epoll_module` 加 `EPOLL_SRCS=src/event/modules/ngx_epoll_module.c`。configure 会按目标 OS 启用其中若干个。

注意 `ngx_event_openssl.c` 和整个 `src/event/quic/` 目录虽物理上在 `src/event`，但它们的模块是**可选**的（受 `--with-http_ssl_module`、`--with-http_v3_module` 等开关控制），所以不在 `EVENT_MODULES` 里，而是由 `auto/modules` 按开关追加。这再次印证了「物理目录」与「编译清单」两个维度的区别。

#### 4.3.4 代码实践

实践目标：识别事件后端模块与 OS 的对应关系。

操作步骤：

1. 执行 `ls src/event/modules/`，列出全部后端文件。
2. 执行 `ls src/event/quic/ | head`，观察 QUIC 子目录的文件规模（它是一个完整的传输协议栈，文件很多）。
3. 对照 [auto/sources:106-127](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L106-L127)，把每个后端文件名与其支持的平台连起来：
   - `ngx_epoll_module.c` → Linux
   - `ngx_kqueue_module.c` → FreeBSD / macOS
   - `ngx_iocp_module.c` → Windows
   - `ngx_poll_module.c` / `ngx_select_module.c` → 几乎所有 Unix
4. 思考：为什么 `ngx_win32_select_module.c` 和 `ngx_win32_poll_module.c` 单独存在？

需要观察的现象：同一类后端（如 select）有 Unix 版和 Win32 版两套实现。

预期结果：你会理解「事件层」既要适配**多路复用机制差异**（epoll vs kqueue），又要适配**操作系统差异**（unix vs win32）。

#### 4.3.5 小练习与答案

**练习 1**：在一个 Linux 服务器上编译 nginx，`src/event/modules/` 里哪个后端最可能被实际启用？

**参考答案**：`ngx_epoll_module.c`。epoll 是 Linux 上性能最好的多路复用机制，configure 在 Linux 上会默认启用它。

**练习 2**：定时器（`ngx_event_timer.c`）放在事件层而不是 core 层，合理吗？

**参考答案**：合理。定时器是「事件驱动调度」的一部分——它依赖 core 提供的红黑树，但「到期检查并触发回调」是事件循环的职责，所以归入事件层。这体现了「core 提供原料，event 提供机制」的分工。

### 4.4 src/http、src/stream、src/mail：三个平行协议层

#### 4.4.1 概念说明

协议层是离用户最近的层。nginx 有**三个平行的协议子系统**，它们彼此独立，却都复用同一套 core + event 基础：

| 子系统 | 目录 | 处理的协议 | 典型用途 |
| --- | --- | --- | --- |
| HTTP | `src/http/` | HTTP/1.1、HTTP/2、HTTP/3 | Web 服务器、反向代理、API 网关 |
| Stream | `src/stream/` | TCP、UDP（四层） | 四层负载均衡、TCP/UDP 代理 |
| Mail | `src/mail/` | SMTP、POP3、IMAP | 邮件代理与认证前置 |

三者结构高度相似，都遵循「**框架 + 模块**」模式：
- 一个**框架**（如 `ngx_http.c`、`ngx_stream.c`、`ngx_mail.c`）负责监听端口、接收连接、驱动请求/会话生命周期。
- 一组**功能模块**（如 `ngx_http_proxy_module.c`、`ngx_stream_proxy_module.c`）提供具体能力（代理、限流、日志……）。

**目录组织上的一个差异**值得注意：`src/http/` 把功能模块集中放在子目录 `src/http/modules/`；而 `src/stream/` 和 `src/mail/` 把所有文件**平铺**在各自根目录下，没有 `modules/` 子目录。原因是 HTTP 模块数量极多（60+），需要单独子目录整理；stream/mail 模块较少，平铺即可。

#### 4.4.2 核心流程：协议层模块清单的组装

与 core/event 不同，协议层的模块清单不在 `auto/sources`，而在 `auto/modules` 里按开关动态组装：

```text
auto/modules
├── if [ $HTTP   = YES ]; then  HTTP_MODULES=...    (约 60+ 模块)
├── if [ $MAIL   = YES ]; then  MAIL_MODULES=...    (邮件相关)
└── if [ $STREAM = YES ]; then  STREAM_MODULES=...  (stream 相关)
        │
        └── 最终都拼进 $modules，写入 objs/ngx_modules.c
```

#### 4.4.3 源码精读

[auto/modules:59-60](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L59-L60) —— HTTP 模块清单的开头：只有 `if [ $HTTP = YES ]` 成立才填充 `HTTP_MODULES`。

[auto/modules:1007](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1007) 与 [auto/modules:1088](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L1088) —— mail、stream 同理，各自有独立开关。

HTTP 子目录内部再分四块（你可用 `ls src/http/` 验证）：
- **框架与核心**：`ngx_http.c`（模块组织）、`ngx_http_request.c`（请求生命周期）、`ngx_http_parse.c`（协议解析）、`ngx_http_upstream.c`（反向代理框架）、`ngx_http_variables.c`（变量系统）、各 `*_filter_module.c`（过滤器链）。
- **功能模块**：`src/http/modules/`，包含 `ngx_http_proxy_module.c`、`ngx_http_static_module.c`、`ngx_http_limit_req_module.c`、`ngx_http_ssl_module.c` 等 60+ 模块。
- **HTTP/2**：`src/http/v2/`（帧处理、HPACK、过滤器）。
- **HTTP/3**：`src/http/v3/`（基于 QUIC，与 `src/event/quic/` 配合）。

[auto/sources:260](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L260) 与 [auto/sources:262-263](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L262-L263) —— 即便在「总要编」的 `auto/sources` 里，也单独定义了 `HTTP_FILE_CACHE_SRCS`（文件缓存）和 `HTTP_HUFF_SRCS`（HPACK 的 Huffman 编解码），供 `auto/modules` 在启用 HTTP 时取用。

stream 与 mail 的文件结构可由 `ls src/stream/`、`ls src/mail/` 直接看到：stream 有 `ngx_stream.c`（框架）、`ngx_stream_proxy_module.c`（TCP/UDP 代理）、`ngx_stream_upstream*.c`（负载均衡）等；mail 有 `ngx_mail.c`（框架）、`ngx_mail_smtp_handler.c` / `ngx_mail_pop3_handler.c` / `ngx_mail_imap_handler.c`（三协议 handler）、`ngx_mail_auth_http_module.c`（外部认证）、`ngx_mail_proxy_module.c`（转发）。

#### 4.4.4 代码实践

实践目标：体会三个协议子系统的「平行结构」。

操作步骤：

1. 执行 `ls src/http/modules/ | wc -l`、`ls src/stream/ | wc -l`、`ls src/mail/ | wc -l`，比较三者规模。
2. 在三个目录里分别找出「框架主文件」（即名字形如 `ngx_<协议>.c` 的文件）：
   - `src/http/ngx_http.c`
   - `src/stream/ngx_stream.c`
   - `src/mail/ngx_mail.c`
3. 在三个目录里分别找出「代理模块」（处理「转发到后端」的核心）：
   - `src/http/modules/ngx_http_proxy_module.c`
   - `src/stream/ngx_stream_proxy_module.c`
   - `src/mail/ngx_mail_proxy_module.c`
4. 观察：三个协议都有「proxy 模块」，它们的命名高度一致。

需要观察的现象：三个子系统像三个「同构」的小框架，只是协议不同。

预期结果：你会得出一个强有力的导航规则——**看到 `ngx_http_*` 去 `src/http`，看到 `ngx_stream_*` 去 `src/stream`，看到 `ngx_mail_*` 去 `src/mail`**。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_http_v3_module` 依赖 QUIC 协议栈。QUIC 的实现文件在哪个目录？

**参考答案**：在 `src/event/quic/`（传输与加密层），而 HTTP/3 的语义层在 `src/http/v3/`。HTTP/3 是「HTTP 语义跑在 QUIC 传输上」，所以代码横跨 `src/http/v3` 与 `src/event/quic` 两个目录。

**练习 2**：为什么 HTTP 有 `modules/` 子目录，而 stream/mail 没有？

**参考答案**：纯粹因为 HTTP 模块数量多（60+），需要子目录组织；stream/mail 模块少，平铺在根目录已足够清晰。这是工程上的便利，不是架构差异。

**练习 3**：`ngx_http_upstream.c`（反向代理框架）在 `src/http/`，但 stream 也有 `ngx_stream_upstream.c`。这说明 upstream 是「HTTP 专属」的吗？

**参考答案**：不是。upstream（「与后端通信」的框架）是一个**跨协议的通用模式**，http 和 stream 各自实现了一份。这也提示你：很多 HTTP 里的概念在 stream 里能找到对应物。

### 4.5 src/os/unix 与 src/os/win32：操作系统抽象层

#### 4.5.1 概念说明

最底层是操作系统抽象层。它的使命是：**用一套统一的函数名，掩盖 Linux/FreeBSD/Solaris/Darwin/Windows 之间的系统调用差异**，让上面三层（core/event/协议层）几乎不用关心自己跑在哪个 OS 上。

例如，「发送数据」在不同 OS 上是不同系统调用：

| 抽象接口（core 看到的） | Linux 实现 | Windows 实现 |
| --- | --- | --- |
| 读 socket | `ngx_readv_chain`（readv） | `ngx_wsarecv_chain`（WSARecv） |
| 写 socket | `ngx_writev_chain`（writev） | `ngx_wsasend_chain`（WSASend） |
| 零拷贝发文件 | `ngx_linux_sendfile_chain`（sendfile） | （Windows 无对应） |

`src/os` 下只有两个子目录：`unix/` 与 `win32/`，分别对应两套平台实现。

#### 4.5.2 核心流程：Unix 下的「平台变体」

即便同在 `src/os/unix/` 下，不同 Unix 也有差异。nginx 用「**通用文件 + 平台特化文件**」组合处理：

```text
src/os/unix/
├── 通用部分（所有 Unix 共用）
│     ngx_files.c, ngx_socket.c, ngx_readv_chain.c, ngx_writev_chain.c,
│     ngx_process.c, ngx_process_cycle.c, ngx_channel.c, ...
└── 平台特化（按 OS 二选一/多选一）
      Linux:    ngx_linux_config.h + ngx_linux_init.c + ngx_linux_sendfile_chain.c
      FreeBSD:  ngx_freebsd_config.h + ngx_freebsd_init.c + ngx_freebsd_sendfile_chain.c
      Solaris:  ngx_solaris_config.h + ngx_solaris_init.c + ngx_solaris_sendfilev_chain.c
      Darwin:   ngx_darwin_config.h + ngx_darwin_init.c + ngx_darwin_sendfile_chain.c
```

每个平台的 `*_config.h` 是 configure 探测出来的「这个 OS 有哪些能力」的头文件，`*_init.c` 在进程启动时做平台初始化，`*_sendfile_chain.c` 是该平台特有的高效 I/O 实现。

#### 4.5.3 源码精读

[auto/sources:132-151](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L132-L151) —— `UNIX_DEPS`：Unix 抽象层的头文件，注意它 = `$CORE_DEPS $EVENT_DEPS` 再拼上 `src/os/unix/` 的头文件。这一行再次展示「层叠加」。

[auto/sources:163-185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L163-L185) —— `UNIX_SRCS`：Unix 抽象层源文件，同样是 `$CORE_SRCS $EVENT_SRCS` 再拼 os 文件。注意里面有 `ngx_process_cycle.c`（master/worker 进程循环，u4 单元精读）、`ngx_channel.c`（进程间通信）、`ngx_linux_*` 之外的所有通用 IO。

[auto/sources:196-212](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L196-L212) —— 四个 Unix 平台变体的定义：`FREEBSD_*`、`LINUX_*`、`SOLARIS_*`、`DARWIN_*`，各自有 `config.h`、`init.c`、`sendfile_chain.c` 三件套。

[auto/sources:215-254](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L215-L254) —— `WIN32_DEPS` 与 `WIN32_SRCS`：Windows 平台的平行实现，含 `ngx_wsarecv.c`、`ngx_wsasend.c`、`ngx_win32_init.c` 等。注意 `WIN32_SRCS` 同样以 `$CORE_SRCS $EVENT_SRCS` 开头——core/event 层是 Windows 与 Unix **共用**的，只有 os 层分叉。

#### 4.5.4 代码实践

实践目标：对比 Unix 与 Windows 两套 os 抽象，理解「core 共用、os 分叉」。

操作步骤：

1. 执行 `ls src/os/unix/` 与 `ls src/os/win32/`，对比两个目录。
2. 找出「接收数据」在两套实现里的对应文件：
   - Unix：`src/os/unix/ngx_recv.c`、`src/os/unix/ngx_readv_chain.c`
   - Windows：`src/os/win32/ngx_wsarecv.c`、`src/os/win32/ngx_wsarecv_chain.c`
3. 观察 `auto/sources` 中 `UNIX_SRCS`（[行 163-185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L163-L185)）和 `WIN32_SRCS`（[行 235-254](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L235-L254)）的**开头**：两者都以 `$CORE_SRCS $EVENT_SRCS` 打头。

需要观察的现象：两个清单前半段完全相同（core + event），只有后半段（os 部分）不同。

预期结果：你会直观理解「为什么 nginx 能跨平台」——因为跨平台的代码集中在 core/event，平台差异被隔离在 os 层。

#### 4.5.5 小练习与答案

**练习 1**：在 Linux 上，nginx 发送静态文件时走哪条高效 I/O 路径？

**参考答案**：`src/os/unix/ngx_linux_sendfile_chain.c`（对应 `auto/sources:202` 的 `LINUX_SENDFILE_SRCS`）。它封装 Linux 的 `sendfile` 系统调用，实现零拷贝。

**练习 2**：master/worker 进程循环（`ngx_process_cycle.c`）在 `src/os/unix/`，那 Windows 版本在哪？

**参考答案**：在 `src/os/win32/ngx_process_cycle.c`（见 `auto/sources:253` 的 `WIN32_SRCS` 末尾）。进程模型在两个平台上各有一份实现，因为 fork 与 Windows 的进程/线程模型差异很大。

**练习 3**：为什么 `ngx_process_cycle.c` 不放在 `src/core`，让两个平台共用？

**参考答案**：因为「如何派生进程、如何处理信号」高度依赖 OS：Unix 用 `fork` + 信号，Windows 用完全不同的机制。这些差异无法用统一接口掩盖，所以进程循环只能按平台分别实现，放在 os 层。

## 5. 综合实践

本讲的核心实践任务是：**对照 `auto/sources` 与 `auto/modules` 的清单，绘制一张「src 子目录 → 职责」对照表**。请按下列步骤完成：

1. **列出全部子目录**：在仓库根目录执行 `ls src/`，得到 `core event http misc os stream mail` 七项（其中 `os/` 下还有 `unix/`、`win32/`）。

2. **逐目录填表**：为每个子目录填写四列——「职责」「所属层」「在哪个清单里」「代表性文件」。可参考下表骨架（请你自己补全「代表性文件」列）：

   | 子目录 | 职责 | 所属层 | 在哪个清单里 |
   | --- | --- | --- | --- |
   | `src/core` | 基础设施（内存/容器/日志/cycle） | 基础设施层 | `auto/sources` 的 `CORE_*` |
   | `src/event` | 事件驱动 + 多路复用后端 + SSL + QUIC | 事件层 | `auto/sources` 的 `EVENT_*` |
   | `src/http` | HTTP/1、HTTP/2、HTTP/3 协议 | 协议层 | `auto/modules` 的 `HTTP_MODULES` |
   | `src/stream` | TCP/UDP 四层代理 | 协议层 | `auto/modules` 的 `STREAM_MODULES` |
   | `src/mail` | SMTP/POP3/IMAP 邮件代理 | 协议层 | `auto/modules` 的 `MAIL_MODULES` |
   | `src/os/unix` | Unix 系统抽象 | OS 抽象层 | `auto/sources` 的 `UNIX_*` |
   | `src/os/win32` | Windows 系统抽象 | OS 抽象层 | `auto/sources` 的 `WIN32_*` |
   | `src/misc` | 辅助/第三方示例（如 perftools） | （附加） | （由 `auto/modules` 按开关） |

3. **验证清单**：对照 [auto/sources:48-83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L48-L83)（`CORE_SRCS`）核对：你列在 `src/core` 行的代表性文件，是否都出现在这个清单里？如果出现，说明你定位对了层。

4. **画依赖图**：用箭头画出五层之间的依赖方向（协议 → 事件 → 基础设施 → OS 抽象），并标注「依赖方向不可倒置」。

5. **自测导航能力**：让同伴（或自己）随机说一个 nginx 功能词，例如「gzip 压缩」「漏桶限流」「HPACK」「accept 互斥锁」「slab 分配器」，你应能在 5 秒内说出它属于哪个子目录：
   - gzip → `src/http/modules/ngx_http_gzip_filter_module.c`
   - 漏桶限流 → `src/http/modules/ngx_http_limit_req_module.c`
   - HPACK → `src/http/v2/ngx_http_v2_table.c`
   - accept 互斥锁 → `src/event/ngx_event.c`（配合 `src/core/ngx_shmtx.c`）
   - slab 分配器 → `src/core/ngx_slab.c`

如果第 5 步你能全部答对，本讲的目标就达成了。

## 6. 本讲小结

- nginx 源码自底向上分五层：**OS 抽象（os）→ 基础设施（core）→ 事件驱动（event）→ 协议（http/stream/mail）**，依赖方向单向，不可倒置。
- `auto/sources` 是「总要编译」的清单（core/event/os），`auto/modules` 是「按开关组装」的清单（http/mail/stream 及可选模块），两者最终在 `auto/modules:1553-1578` 拍平成运行时的 `ngx_modules[]` 数组。
- `src/core` 是自研标准库：内存池、字符串、容器、buf、日志、cycle、配置解析、模块系统都在这里，`nginx.c` 含 `main()`。
- `src/event` 是性能核心：事件框架 + 多路复用后端（epoll/kqueue/...）+ SSL + QUIC，后端按 OS 选择。
- 协议层有三个**平行同构**的子系统 http/stream/mail，命名规则一致（`ngx_http_*`/`ngx_stream_*`/`ngx_mail_*`），都复用 core+event。
- `src/os/unix` 与 `src/os/win32` 是两套 OS 抽象，让 core/event 在两平台共用；Unix 下再按 Linux/FreeBSD/Solaris/Darwin 分变体。

## 7. 下一步学习建议

建立了源码全景图之后，建议按依赖自底向上的顺序深入：

1. **下一篇 [u1-l4 程序启动入口 main() 全流程](u1-l4-main-startup.md)**：精读 `src/core/nginx.c` 的 `main()`，看程序如何从命令行解析一路走到 master 进程循环。这是把「静态目录结构」变成「动态启动流程」的第一步。
2. **u2 单元（core 基础设施）**：按本讲 [auto/sources:10-45](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/sources#L10-L45) 的 `CORE_DEPS` 顺序，逐个精读内存池、字符串、容器、buf、时间日志——这是读懂一切上层源码的前提。
3. **辅助目录速览**：本讲聚焦 `src/`。构建脚本 `auto/` 已在 [u1-l2](u1-l2-build-and-run.md) 介绍；配置示例 `conf/` 将在 u3 单元（配置解析）深入；`docs/`（文档源，XML 格式）和 `misc/`（perftools 等附加模块）可在需要时再查阅。
4. **导航习惯**：从现在起，每次遇到一个 nginx 模块名，先按 `ngx_<层>_*` 的命名判断它属于哪一层，再去对应目录找文件——这个习惯会让你在 nginx 源码里的阅读效率提升一个数量级。
