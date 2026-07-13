# HTTP 请求生命周期

## 1. 本讲目标

本讲承接 u6-l1「HTTP 模块框架与上下文」。u6-l1 讲的是 `http {}` 块在**配置阶段**如何被装配成 main/srv/loc 三层结构与 phase 引擎；本讲则进入**运行时阶段**，回答一个更具体的问题：

> 一条 TCP 连接被 accept 之后，nginx 是如何把它变成一个 HTTP 请求、一路推进到响应完成、最后又让连接回到「等待下一个请求」状态的？

学完后你应当掌握：

- 一条连接从 `ngx_http_init_connection` 到 `ngx_http_close_request` 的完整状态机推进过程。
- `ngx_http_request_t` 这个贯穿整个请求的核心结构体的关键字段及其用途。
- nginx 的**两层 handler 分发机制**：连接层 `c->read->handler` / `c->write->handler` 与请求层 `r->read_event_handler` / `r->write_event_handler` 的区别与协作。
- keepalive（长连接）下，一个请求结束后连接如何「重置」回等待状态以复用，以及 pipelined（流水线）请求如何被立即拾起。

## 2. 前置知识

在进入源码前，先建立三个直觉。后续源码精读都围绕它们展开。

### 2.1 事件驱动 = 「事件来了调一个 handler」

nginx worker 的事件循环（见 u5-l5 `ngx_process_events_and_timers`）本质上在做一件事：epoll 告诉我哪个 fd 可读/可写了，我就调用挂在这个 fd 上的**回调函数**。在 nginx 里，每个连接 `ngx_connection_t` 都带一个读事件 `c->read` 和一个写事件 `c->write`，它们各有一个 `handler` 函数指针。所谓「请求生命周期」，就是 nginx 在不同阶段不断**改写这些 handler 指针**，让同一个连接在不同时刻做不同的事。

### 2.2 一个连接承载多个请求（keepalive）

HTTP/1.1 默认长连接：一条 TCP 连接上可以连续发多个请求。所以 nginx 的数据结构分两层：

- **连接级**状态 `ngx_http_connection_t`（`hc`）：跨多个请求复用，记录这条连接的地址配置、SSL 标志、空闲 buffer 等。
- **请求级**状态 `ngx_http_request_t`（`r`）：一个请求一份，请求结束就销毁。

理解「`hc` 长存、`r` 短命」是理解 keepalive 的钥匙。

### 2.3 两层 handler

这是本讲最容易绕晕、也最关键的概念。请求处理期间存在两组 handler：

| 层次 | 字段 | 谁来调 | 何时设 |
|------|------|--------|--------|
| 连接层 | `c->read->handler` / `c->write->handler` | 事件循环（epoll）直接调 | 连接建立、进入处理、keepalive 等节点 |
| 请求层 | `r->read_event_handler` / `r->write_event_handler` | 连接层 handler 间接调 | 请求处理各阶段动态切换 |

进入 phase 处理后，`c->read->handler` 和 `c->write->handler` **都被设成同一个函数 `ngx_http_request_handler`**。它像一个「路由器」：epoll 一唤醒，先调它，它再看这次是读事件还是写事件，转而去调 `r->read_event_handler` 或 `r->write_event_handler`。这样请求只需改写自己的两个请求层 handler，就能「重新编程」读写事件的行为，而无需触碰连接层。第 4.3 节会给出完整的切换表。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/http/ngx_http_request.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c) | 请求生命周期的几乎全部实现：连接初始化、读请求、创建 request、解析驱动、终结、keepalive、lingering close |
| [src/http/ngx_http_request.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h) | `ngx_http_request_t`、`ngx_http_connection_t`、请求状态枚举等结构定义 |
| [src/http/ngx_http.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c) | 在配置阶段把监听端口的 `ls->handler` 注册为 `ngx_http_init_connection`（[L1824](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L1824)），是生命周期的入口接线点 |
| [src/http/ngx_http_core_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c) | `ngx_http_handler`——从「请求就绪」跨入 phase 引擎的那一步，并设定首个请求层 handler |

## 4. 核心概念与源码讲解

### 4.1 连接建立：ngx_http_init_connection

#### 4.1.1 概念说明

u5-l3 讲过，`ngx_event_accept` 接受一条新连接后，会调用监听套接字上的 `ls->handler` 把连接交给协议层。对 HTTP 而言，这个 handler 就是 `ngx_http_init_connection`。它的注册发生在配置阶段：

[src/http/ngx_http.c:1824](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L1824) —— `ls->handler = ngx_http_init_connection;`，把每个 HTTP 监听端口的入连接回调固定为它。

`ngx_http_init_connection` 要做的不是处理请求，而是**为这条连接搭好脚手架**：找出它命中的地址配置、挂上第一个读 handler、起一个「等首字节」定时器，然后立刻返回——把控制权交还事件循环，等待客户端真的发数据。

#### 4.1.2 核心流程

```
ngx_event_accept 取得新连接 c
        │  调 ls->handler(c)
        ▼
ngx_http_init_connection(c)
        │
        ├─ 1. 为连接分配连接级状态 hc = ngx_http_connection_t，挂到 c->data
        ├─ 2. 根据 c->listening->servers 找到本端口对应的 addr_conf
        │      （多地址端口要用 getsockname 区分，见下文）
        ├─ 3. hc->conf_ctx = default_server->ctx  （拿到默认虚拟主机的配置上下文）
        ├─ 4. 配置日志上下文（connection 号、log_error handler、action="waiting for request"）
        ├─ 5. 设第一个 handler：
        │      c->read->handler  = ngx_http_wait_request_handler
        │      c->write->handler = ngx_http_empty_handler   （写事件先空转）
        ├─ 6. 分支：SSL 端口 → rev->handler = ngx_http_ssl_handshake
        │         HTTP/3(QUIC) → ngx_http_v3_init_stream 直接 return
        │         PROXY protocol → 标记后仍走 wait_request_handler
        └─ 7. 若数据已就绪（deferred accept）：直接调 handler；否则
              ngx_add_timer(rev, client_header_timeout)   ← 等首字节超时
              ngx_reusable_connection(c, 1)               ← 标记可复用
              ngx_handle_read_event(rev, 0)               ← 把 read 挂上 epoll
```

注意第 7 步：连接此刻**没有数据**，init_connection 不读数据，只是登记「读就绪时调 `ngx_http_wait_request_handler`」并起一个 `client_header_timeout` 定时器。如果客户端在这个时间内不发任何字节，定时器触发就会关连接。

#### 4.1.3 源码精读

入口与连接级状态分配：[src/http/ngx_http_request.c:210-231](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L210-L231)。`hc` 从连接内存池 `c->pool` 分配（accept 时为每条连接建的池，见 u5-l3），并挂到 `c->data`。此刻 `c->data` 指向 `hc` 而非请求——因为请求还没诞生。

地址配置选择：[src/http/ngx_http_request.c:235-308](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L235-L308)。一个端口可能绑定多个地址（如同时监听 `127.0.0.1:80` 和 `*:80`），`naddrs > 1` 时需用 `ngx_connection_local_sockaddr` 取本端地址来区分，最终落到 `hc->addr_conf`；只有一个地址时直接取 `addrs[0].conf`。`hc->conf_ctx` 取该地址默认 server 的配置上下文，这是后续 `ngx_http_get_module_*_conf` 的入口。

挂第一个 handler：[src/http/ngx_http_request.c:327-349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L327-L349)。这段把 `rev->handler` 设为 `ngx_http_wait_request_handler`、`c->write->handler` 设为 `ngx_http_empty_handler`（空操作，写事件此刻无意义）；SSL 端口则改写 `rev->handler` 为 `ngx_http_ssl_handshake`，先做握手再回到读请求。

登记等待与定时器：[src/http/ngx_http_request.c:351-372](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L351-L372)。数据未就绪时调 `ngx_add_timer(rev, cscf->client_header_timeout)` 起超时，`ngx_reusable_connection(c, 1)` 把连接标记为「空闲可复用」（供 `ngx_drain_connections` 在资源紧张时回收，见 u5-l3），`ngx_handle_read_event` 把读事件挂上 epoll。

#### 4.1.4 代码实践

**目标**：看清 `init_connection` 是如何「只搭脚手架不读数据」的，并定位 SSL 与 HTTP/3 的分支点。

**操作步骤**：

1. 在 [src/http/ngx_http_request.c:210-372](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L210-L372) 通读 `ngx_http_init_connection`，标出三处 `rev->handler` 赋值（`wait_request_handler`、`ssl_handshake`、`v3_init_stream` 间接）。
2. 在 [src/http/ngx_http.c:1824](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L1824) 确认 `ls->handler` 的注册点，回溯它属于哪个函数（`ngx_http_add_listening` / `ngx_http_add_addrs6` 等），理解配置阶段如何把端口与 handler 绑定。
3. 用 `nginx -t` 校验一份含 `listen 443 ssl;` 与 `listen 8443 quic;` 的配置，对照源码说明两种端口会让 init_connection 走不同分支。

**需要观察的现象**：init_connection 全程没有 `c->recv` 调用——真正的读数据发生在下一步的 `ngx_http_wait_request_handler`。

**预期结果**：你能用一句话说清「init_connection 之后，连接处于 rev 监听 + 定时器等待 + write 空转」的状态。运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_http_init_connection` 里 `c->write->handler` 要设成 `ngx_http_empty_handler` 而不是 NULL？

**参考答案**：事件循环不区分读写都会调 `handler`，若为 NULL 会在写事件就绪时解引用空指针崩溃。`ngx_http_empty_handler`（[src/http/ngx_http_request.c:3784](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3784)）是一个安全空操作，保证连接在「只需要读」的阶段对写事件免疫。

**练习 2**：`hc->addr_conf` 与 `hc->conf_ctx` 各代表什么？为何要先选 `addr_conf` 再取 `default_server->ctx`？

**参考答案**：`addr_conf` 是「这个监听地址」的配置（含 ssl/quic/proxy_protocol 标志与默认 server）；`conf_ctx` 是该地址默认虚拟主机的 HTTP 配置上下文（main/srv/loc 三层指针数组）。先定位地址才能确定默认 server，进而拿到它的配置上下文——具体的 server（虚拟主机）要等到解析出 `Host` 头后由 `ngx_http_set_virtual_server` 再替换。

---

### 4.2 读取请求与创建请求对象：ngx_http_wait_request_handler / ngx_http_create_request

#### 4.2.1 概念说明

`ngx_http_wait_request_handler` 是连接的「第一读」handler：客户端发了字节，epoll 唤醒，调它。它做两件事——**把数据读进缓冲区**，然后**创建 `ngx_http_request_t` 并把控制权交给解析器**。

`ngx_http_create_request` / `ngx_http_alloc_request` 则负责诞生那个贯穿请求全生命周期的 `ngx_http_request_t`：开请求私有内存池、把三层配置指针接到连接的 `conf_ctx`、初始化头部容器与变量数组、记录起始时间戳。这是 nginx「一个请求一个池」（见 u2-l1）的典型应用。

#### 4.2.2 核心流程

```
epoll 唤醒 → c->read->handler = ngx_http_wait_request_handler
        │
        ├─ 超时 / c->close → ngx_http_close_connection
        ├─ 准备缓冲区 b（首次 ngx_create_temp_buf，复用时重置）
        ├─ n = c->recv(c, b->last, size)
        │     ├─ NGX_AGAIN → 重挂读事件 + 起定时器，可顺手 pfree 掉空 buffer 省内存，return
        │     ├─ NGX_ERROR / n==0 → 关连接
        │     └─ 有数据：b->last += n
        ├─ （可选）PROXY protocol 解析
        ├─ （可选）HTTP/2 preface 探测 → ngx_http_v2_init
        └─ c->data = ngx_http_create_request(c)     ← 请求诞生
              rev->handler = ngx_http_process_request_line
              ngx_http_process_request_line(rev)    ← 进入解析状态机
```

解析状态机的推进（本讲只看交接点，状态机细节留待 u6-l3）：

```
ngx_http_process_request_line        ← 解析 "GET /uri HTTP/1.1"
   │  解析完请求行
   ├─ 若 HTTP/0.9（无头部）→ 直接 ngx_http_process_request(r)
   └─ 否则 rev->handler = ngx_http_process_request_headers
        ngx_http_process_request_headers(rev)     ← 逐行解析头部
            │  收到 NGX_HTTP_PARSE_HEADER_DONE（空行）
            └─ ngx_http_process_request_header(r) 做头部合法性收尾
               ngx_http_process_request(r)        ← 进入 phase 引擎
```

#### 4.2.3 源码精读

**读首字节**：[src/http/ngx_http_request.c:376-474](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L376-L474)。注意几个细节：缓冲区 `c->buffer` 首次用 `ngx_create_temp_buf` 创建（`client_header_buffer_size` 大小）；`NGX_AGAIN` 分支里若 `b->pos == b->last`（缓冲区空）会 `ngx_pfree` 掉内存，只为空闲连接省内存 footprint；`n == 0` 表示客户端发完就关，记日志后关连接。

**协议探测与创建请求**：[src/http/ngx_http_request.c:499-534](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L499-L534)。HTTP/2 通过比对 `NGX_HTTP_V2_PREFACE` 前缀探测，命中则 `ngx_http_v2_init` 走 h2 路径；否则 `c->data = ngx_http_create_request(c)` 让请求对象取代 `hc` 成为 `c->data`，再把 `rev->handler` 切到 `ngx_http_process_request_line` 并立即调用一次。

**`ngx_http_create_request`**：[src/http/ngx_http_request.c:537-566](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L537-L566)。它只是个薄封装：调 `ngx_http_alloc_request` 拿到 `r`，递增 `c->requests`，按 location 配置设连接日志，把日志上下文的 `request`/`current_request` 指向 `r`。

**`ngx_http_alloc_request`**：[src/http/ngx_http_request.c:569-668](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L569-L668) 是请求对象诞生的核心，值得逐行看：

- [L583](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L583) `ngx_create_pool(cscf->request_pool_size, ...)`：为这个请求开**私有内存池**，请求结束整池销毁。
- [L588](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L588) `r = ngx_pcalloc(pool, sizeof(ngx_http_request_t))`：请求结构体本身也从这个池分配。
- [L600-L602](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L600-L602) `r->main_conf/srv_conf/loc_conf = hc->conf_ctx->...`：把连接默认 server 的三层配置指针接到请求上——这就是 u6-l1 讲的三层 conf 在运行时的取用入口。
- [L604](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L604) `r->read_event_handler = ngx_http_block_reading`：**请求层读 handler 的初值**设为「阻塞读取」（暂时不读请求体），此时 `write_event_handler` 尚未设置（pcalloc 已清零）。
- [L606](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L606) `r->header_in = hc->busy ? hc->busy->buf : c->buffer`：请求头缓冲区，优先复用上一请求（pipelined）残留的 large buffer。
- [L645-L646](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L645-L646) `r->main = r; r->count = 1`：`main` 指向自己（标记「我是主请求，非子请求」），`count` 引用计数初值 1（见 4.3）。
- [L663](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L663) `r->http_state = NGX_HTTP_READING_REQUEST_STATE`：置请求状态为「正在读请求」。

**请求行解析后的交接**：[src/http/ngx_http_request.c:1204-1231](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1204-L1231)。HTTP/0.9 直接进 `ngx_http_process_request`；HTTP/1.x 把 `rev->handler` 切到 `ngx_http_process_request_headers` 继续解析头部。

**头部解析完成的交接**：[src/http/ngx_http_request.c:1554-1573](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1554-L1573)。收到 `NGX_HTTP_PARSE_HEADER_DONE`（空行）后，置 `http_state = NGX_HTTP_PROCESS_REQUEST_STATE`，调 `ngx_http_process_request_header` 做头部收尾校验，再调 `ngx_http_process_request(r)` 跨入 phase 引擎。

**`ngx_http_request_t` 关键字段**（定义在 [src/http/ngx_http_request.h:385-613](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L385-L613)），本讲需记住的：

| 字段 | 行 | 用途 |
|------|----|------|
| `signature` | 386 | 调试标记 `"HTTP"` |
| `connection` | 388 | 反指所属连接 `ngx_connection_t` |
| `ctx` / `main_conf` / `srv_conf` / `loc_conf` | 390-393 | 模块上下文数组 + 三层配置指针 |
| `read_event_handler` / `write_event_handler` | 395-396 | **请求层两个 handler**，本讲主角 |
| `pool` | 406 | 请求私有内存池 |
| `header_in` | 407 | 请求头读缓冲 |
| `headers_in` / `headers_out` | 409-410 | 请求/响应头部容器 |
| `method` / `http_version` | 418-419 | 解析出的方法与版本 |
| `main` / `parent` | 432-433 | 主请求 / 父请求（子请求用） |
| `phase_handler` / `content_handler` | 438-439 | phase 引擎游标与 content 处理者 |
| `count` / `subrequests` / `blocked` | 470-472 | 引用计数 / 子请求余量 / 阻塞计数 |
| `keepalive` / `lingering_close` | 546-547 | 是否长连接 / 是否需 lingering |

请求状态枚举 [src/http/ngx_http_request.h:157-169](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L157-L169) 定义了 `INITING/READING/PROCESS/.../WRITING_REQUEST/LINGERING_CLOSE/KEEPALIVE` 等，对应 `r->http_state` 的取值，是状态机的「自报家门」。

#### 4.2.4 代码实践

**目标**：看清「数据读取 → 请求诞生 → 解析驱动」的链路，并定位 `header_in` 缓冲区的来源。

**操作步骤**：

1. 在 [src/http/ngx_http_request.c:435](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L435) 找到 `c->recv` 调用，确认这是生命周期里**第一次真正读 socket**。
2. 跟踪 `ngx_http_create_request` → `ngx_http_alloc_request`，列出请求对象诞生时被初始化的字段（pool、三层 conf、main、count、read_event_handler、http_state）。
3. 在 [src/http/ngx_http_request.c:1141](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1141) 找到 `ngx_http_read_request_header`，理解解析器如何在「缓冲区有数据就解析、没有就再 recv」之间循环（`NGX_AGAIN` 重挂读事件）。

**需要观察的现象**：解析阶段（请求行/头部）的驱动完全靠 `rev->handler` 在 `process_request_line` 与 `process_request_headers` 之间切换，**此时 `r->read_event_handler` 还是 `ngx_http_block_reading`，并未参与解析**——它要等到进入 `ngx_http_process_request` 才被「激活」。

**预期结果**：你能解释为何解析阶段用的是连接层 handler 直接驱动，而 phase 阶段才启用两层分发。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`ngx_http_alloc_request` 里 `r->main = r; r->count = 1;` 各自的语义是什么？

**参考答案**：`r->main = r` 表示当前请求是主请求（top-level），子请求会把自己的 `main` 指向同一个主请求，使所有子/主请求共享一个 `main` 作为「请求族」代表。`count = 1` 是引用计数初值，每发起一个异步子操作（如读请求体、upstream）会 `count++`，每完成一个 `count--`，归零才允许真正释放请求（见 4.3 `ngx_http_close_request`）。

**练习 2**：为什么 `r->header_in` 要写成 `hc->busy ? hc->busy->buf : c->buffer`？

**参考答案**：keepalive 复用连接时，上一请求可能用 large header buffer 存了尚未处理的 pipelined 数据（挂在 `hc->busy`）。新请求必须从这些残留 buffer 继续解析，否则会丢数据；没有残留时退回连接的默认 `c->buffer`。

---

### 4.3 请求终结与 keepalive 回归：ngx_http_finalize_request

#### 4.3.1 概念说明

请求不可能无限跑下去。每个 handler、每个 phase 检查器、每个 content 处理者，做完自己的事都会调用 `ngx_http_finalize_request(r, rc)` 把「我这边完了，结果是 `rc`」汇报给框架。`finalize` 是一个**巨型分派器**：它根据 `rc` 的语义（`NGX_DONE`/`NGX_DECLINED`/`NGX_ERROR`/HTTP 状态码/0）和请求是否主请求、是否还有缓冲数据，决定下一步——继续 phase、生成错误页、写响应、还是收尾。

收尾时（`ngx_http_finalize_connection`）才是 keepalive 的决策点：响应发完且 `r->keepalive` 为真，就调 `ngx_http_set_keepalive` 把连接**重置回等待状态**；否则可能进入 lingering close（拖一段时间再关，让客户端能读到剩余响应）或直接关连接。

#### 4.3.2 核心流程

```
某 handler 调 ngx_http_finalize_request(r, rc)
        │
        ├─ rc == NGX_DONE         → ngx_http_finalize_connection（异步未完，如读 body）后 return
        ├─ rc == NGX_DECLINED     → 本阶段不处理，write_event_handler=core_run_phases，继续下一 phase
        ├─ rc 是错误/超时/客户端关 → ngx_http_terminate_request（跑 cleanup 链，强关）
        ├─ rc 是特殊 HTTP 码(>=300) → ngx_http_special_response_handler 生成错误页后再 finalize
        ├─ 子请求(r != r->main)   → 唤醒父请求 (post_request)
        └─ 主请求、rc==0、无缓冲：
              r->read_event_handler  = ngx_http_block_reading
              r->write_event_handler = ngx_http_request_empty_handler
              ngx_http_finalize_connection(r)
                    │
                    ├─ count != 1 / discard_body → 特殊处理或 close
                    ├─ r->keepalive && keepalive_timeout>0 → ngx_http_set_keepalive(r)
                    ├─ lingering 条件满足 → ngx_http_set_lingering_close(c)
                    └─ 否则 → ngx_http_close_request(r, 0)

ngx_http_set_keepalive(r):
        ├─ 处理 pipelined 残留 buffer
        ├─ ngx_http_free_request(r, 0)        ← 销毁请求对象与它的池！r 不复存在
        ├─ c->data = hc                       ← 连接 data 回退到连接级状态
        ├─ wev->handler = ngx_http_empty_handler
        ├─ 若有 pipelined 数据：立即 create_request + rev->handler=process_request_line + post
        └─ 否则（真正的「回到等待」）：
              释放 buffer 内存省 footprint
              rev->handler = ngx_http_keepalive_handler   ← 等待下个请求的 handler
              c->idle = 1; ngx_reusable_connection(c, 1)
              ngx_add_timer(rev, keepalive_timeout)        ← keepalive 超时定时器

ngx_http_keepalive_handler(rev):  ← 客户端在长连接上发了新请求
        ├─ 超时(且未到 min_timeout)/close → ngx_http_close_connection
        ├─ recv
        │   ├─ NGX_AGAIN → 重挂读事件 + pfree 空 buffer，继续等
        │   ├─ n==0 / ERROR → 关连接
        │   └─ 有数据 → c->idle=0; create_request; rev->handler=process_request_line; 解析
```

关键认识：**keepalive 的「回到等待」= 销毁 `r` + 把 `c->data` 换回 `hc` + `rev->handler` 换成 `keepalive_handler` + 起一个 keepalive 定时器**。这与 `init_connection` 之后的初始状态（rev 监听 + 定时器等待）形似而 handler 不同，因为 keepalive 路径要额外处理 buffer 复用与 `c->idle` 标记。

#### 4.3.3 源码精读

**`ngx_http_process_request`——进入处理、激活两层分发**：[src/http/ngx_http_request.c:2118-2206](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2118-L2206)。这是连接 handler 切换的关键节点，[L2201-L2205](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2201-L2205)：

```c
c->read->handler  = ngx_http_request_handler;   // 连接层读 handler
c->write->handler = ngx_http_request_handler;   // 连接层写 handler（同一个！）
r->read_event_handler = ngx_http_block_reading; // 请求层读 handler
ngx_http_handler(r);                            // 跨入 phase 引擎
```

从此 epoll 一唤醒（无论读写）都进 `ngx_http_request_handler`，由它分发。`ngx_http_handler`（[src/http/ngx_http_core_module.c:841-880](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L841-L880)）还做两件事：据 `Connection` 头决定 `r->keepalive`（[L848-L860](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L848-L860)），并设 `r->write_event_handler = ngx_http_core_run_phases`（[L878](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L878)）——**请求层写 handler 的初值**就是 phase 引擎推进器。

**两层分发的路由器 `ngx_http_request_handler`**：[src/http/ngx_http_request.c:2577-2610](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2577-L2610)。核心就 [L2602-L2609](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2602-L2609)：

```c
if (ev->write) {
    r->write_event_handler(r);   // 写就绪 → 调请求层写 handler
} else {
    r->read_event_handler(r);    // 读就绪 → 调请求层读 handler
}
ngx_http_run_posted_requests(c); // 之后处理 posted 子请求
```

这就是「连接层 handler 依 ev->write 二选一调请求层 handler」的机制。`c->close` 为真时它会触发 `ngx_http_terminate_request` 强制收尾。

**`ngx_http_finalize_request` 分派**：[src/http/ngx_http_request.c:2670-2853](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2670-L2853)。重点几支：

- [L2682-L2685](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2682-L2685) `NGX_DONE`：异步操作（如读请求体）未完，调 `ngx_http_finalize_connection` 后返回。
- [L2691-L2696](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2691-L2696) `NGX_DECLINED`：本 handler 不处理，把 `write_event_handler` 重设为 `ngx_http_core_run_phases` 继续 phase。
- [L2702-L2713](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2702-L2713) 错误/超时/客户端关：跑 `post_action` 后 `ngx_http_terminate_request`。
- [L2715-L2740](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2715-L2740) 特殊 HTTP 码：调 `ngx_http_special_response_handler` 生成错误页，递归 finalize。
- **主请求正常完成** [L2814-L2853](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2814-L2853)：若有缓冲/挂起数据则 `ngx_http_set_write_handler` 继续写；否则在 [L2832-L2833](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2832-L2833) 把两个请求层 handler 重置为 `block_reading` / `empty_handler`，再 `ngx_http_finalize_connection(r)`。

**写响应 handler `ngx_http_set_write_handler` / `ngx_http_writer`**：[src/http/ngx_http_request.c:3004-3106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3004-L3106)。需要把响应写出去时，[L3009-L3014](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3009-L3014) 把请求状态切到 `NGX_HTTP_WRITING_REQUEST_STATE`，读 handler 设为 `ngx_http_test_reading`（监测客户端中途断开），写 handler 设为 `ngx_http_writer`。`ngx_http_writer`（[L3037-L3106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3037-L3106)）调 `ngx_http_output_filter(r, NULL)` 驱动过滤器链写数据，写不完就重挂写事件 + `send_timeout` 定时器，写完则把 `write_event_handler` 设为 `empty_handler` 再 finalize。

**keepalive 决策 `ngx_http_finalize_connection`**：[src/http/ngx_http_request.c:2924-3000](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2924-L3000)。注意 `r->main->count != 1` 时不走 keepalive（还有未完成的异步操作）；[L2972-L2986](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2972-L2986) `r->keepalive && keepalive_timeout > 0` 才 `ngx_http_set_keepalive`；否则按 `lingering_close` 配置走 lingering 或直接 `ngx_http_close_request`。

**keepalive 重置 `ngx_http_set_keepalive`**：[src/http/ngx_http_request.c:3277-3501](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3277-L3501)。这是「回到等待」的核心：

- [L3347-L3349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3347-L3349) `ngx_http_free_request(r, 0)` 销毁请求与它的池，`c->data = hc` 把连接 data 回退到连接级状态——请求没了，连接还在。
- pipelined 分支 [L3359-L3386](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3359-L3386)：缓冲区里还有下一个请求的数据，立即 `create_request` + `rev->handler = process_request_line` + `ngx_post_event`，下一轮就解析。
- 真正空闲分支 [L3446](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3446)：`rev->handler = ngx_http_keepalive_handler`；[L3481-L3484](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3481-L3484) `c->idle = 1; ngx_reusable_connection(c, 1)`；[L3496](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3496) `ngx_add_timer(rev, keepalive_timeout)`。同时还会 `ngx_pfree` 掉 buffer 内存，把空闲连接的内存占用压到最小。

**keepalive 等待 handler `ngx_http_keepalive_handler`**：[src/http/ngx_http_request.c:3505-3649](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3505-L3649)。客户端在长连接上发来新请求时被调：[L3588](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3588) `recv`；`NGX_AGAIN` 则重挂读事件并 `pfree` 空 buffer 继续等（[L3591-L3611](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3591-L3611)）；`n==0` 客户端关连接；有数据则 [L3636-L3648](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3636-L3648) `c->idle=0` + `create_request` + `rev->handler = process_request_line`——**与 4.2 的 wait_request_handler 殊途同归**，再次进入解析状态机，开启下一个请求的生命周期。

**收尾 `ngx_http_close_request` / `ngx_http_free_request`**：[src/http/ngx_http_request.c:3873-3901](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3873-L3901)。`close_request` 先 `r->count--`，**只有 `count` 归零且 `blocked` 为 0** 才真正 `free_request` + `close_connection`——这就是引用计数守护：任何异步操作没完成都关不掉请求。`free_request`（[L3906+](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3906)）跑 cleanup 链、写 access log、最后 `ngx_destroy_pool` 销毁请求池。

#### 4.3.4 代码实践

**目标**：掌握 finalize 的分派语义，理解 `count` 引用计数如何守护请求不被提前释放。

**操作步骤**：

1. 在 [src/http/ngx_http_request.c:2670-2853](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2670-L2853) 给 `ngx_http_finalize_request` 的每个 `rc` 分支标注「下一步去哪」。
2. 跟踪 `ngx_http_close_request`（[L3873](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3873)），找到 `r->count--` 与 `if (r->count || r->blocked) return;`，理解为何读请求体期间（`count > 1`）即使收到 finalize 也不会立刻释放。
3. 对照 [src/http/ngx_http_request.h:470-472](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L470-L472) 的 `count:16` / `subrequests:8` / `blocked:8` 位域，说明三者各自限制什么。

**需要观察的现象**：`count` 在 `alloc_request` 置 1，子请求/异步操作 `++`，完成时 `--`，归零才能释放——这是 nginx 处理「请求有多条并发进行中的子操作」的核心机制。

**预期结果**：你能解释「为什么 `ngx_http_read_client_request_body` 期间请求不会被释放」。运行结果待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`ngx_http_finalize_request(r, NGX_DECLINED)` 与 `ngx_http_finalize_request(r, NGX_OK)` 对主请求的后续处理有何不同？

**参考答案**：`NGX_DECLINED` 表示「当前 handler 不处理这个请求」，finalize 把 `write_event_handler` 重设为 `ngx_http_core_run_phases` 并继续推进 phase 引擎（让下一个 phase handler 试试），请求**继续活着**。`NGX_OK`（rc=0）表示「请求处理完成」，主请求走收尾路径——若有缓冲数据则 `set_write_handler` 继续写，否则重置 handler 并 `finalize_connection` 决定 keepalive/lingering/close。

**练习 2**：keepalive 复用连接时，`c->data` 经历了怎样的变化？为什么 `ngx_http_set_keepalive` 要先 `free_request` 再把 `c->data` 换回 `hc`？

**参考答案**：`c->data` 的变化是 `hc`（init_connection）→ `r`（create_request）→ `hc`（set_keepalive）。`free_request` 销毁请求对象与其私有内存池，此后 `r` 不再有效，必须把 `c->data` 换回长存的 `hc`，连接才能在「无请求」状态下安全等待下一个请求；若不换回，后续 `keepalive_handler` 取 `c->data` 会得到已释放的 `r`，构成悬垂指针。

---

## 5. 综合实践

本讲的核心实践任务是**标注 `read_event_handler` 与 `write_event_handler` 在请求生命周期各阶段的取值，并说明 keepalive 下连接如何回到等待状态**。这是把前三个最小模块串起来的总练习。

### 5.1 实践目标

构建一张完整的「handler 切换表」，覆盖从连接建立到 keepalive 回归的全过程，区分连接层与请求层两组 handler，并能据此解释 keepalive 的复用机制。

### 5.2 操作步骤

1. **建表**：按下表格式，对照源码逐行填入每个阶段四组 handler 的取值。第一列已给出阶段名，请补全后三列（连接层 `c->read->handler` / `c->write->handler`，请求层 `r->read_event_handler` / `r->write_event_handler`；请求未创建时写「无 r」）。

| 阶段 | `c->read->handler` | `c->write->handler` | `r->read_event_handler` | `r->write_event_handler` |
|------|----|----|----|----|
| ① init_connection 后（等首字节） | `ngx_http_wait_request_handler` | `ngx_http_empty_handler` | 无 r | 无 r |
| ② create_request 后（解析前） | `ngx_http_wait_request_handler` | `ngx_http_empty_handler` | ? | ? |
| ③ 解析请求行 | `ngx_http_process_request_line` | ? | `ngx_http_block_reading` | (未设) |
| ④ 解析头部 | ? | `ngx_http_empty_handler` | `ngx_http_block_reading` | (未设) |
| ⑤ process_request 进入 phase | `ngx_http_request_handler` | `ngx_http_request_handler` | ? | `ngx_http_core_run_phases` |
| ⑥ 需要写响应（set_write_handler） | `ngx_http_request_handler` | `ngx_http_request_handler` | `ngx_http_test_reading` | ? |
| ⑦ 主请求完成（finalize rc=0） | `ngx_http_request_handler` | `ngx_http_request_handler` | ? | `ngx_http_request_empty_handler` |
| ⑧ set_keepalive 后（空闲等待） | `ngx_http_keepalive_handler` | ? | 无 r（已销毁） | 无 r |
| ⑨ keepalive 收到新请求 | `ngx_http_process_request_line` | `ngx_http_empty_handler` | `ngx_http_block_reading` | (未设) |

2. **定位赋值点**：对表中每一处取值，在源码中找到对应的赋值行（本文 4.1–4.3 已给出大部分行号），写在该格旁边作为证据。例如 ⑤ 的请求层写 handler 来自 [ngx_http_core_module.c:878](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L878)。

3. **画 keepalive 回归图**：用箭头画出 `⑦ → ⑧ → ⑨ → ③` 的状态跳转，并在 ⑧ 处标出三个关键动作：`free_request`（销毁 r）、`c->data = hc`（回退到连接级）、`rev->handler = keepalive_handler` + `c->idle=1` + `add_timer(keepalive_timeout)`（回到等待）。

4. **（可选，本地验证）用 debug 日志佐证**：用 `--with-debug` 编译 nginx，在 `error_log` 里开 `debug_http`，用 `curl -k --http1.1 https://127.0.0.1/` 连续发两个请求（同一连接），在日志里搜 `http keepalive handler`、`http process request line`、`http finalize request`、`set http keepalive handler`，对照你画的图核对跳转顺序。

### 5.3 需要观察的现象

- 阶段 ②→③ 之间，`c->read->handler` 从 `wait_request_handler` 切到 `process_request_line`（[L532](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L532)），而请求层 `r->read_event_handler` 一直是 `block_reading`——证明解析阶段**只靠连接层 handler 驱动**。
- 阶段 ⑤ 之后，`c->read->handler` 与 `c->write->handler` 都是 `ngx_http_request_handler`，真正干活的是请求层 handler——证明 phase 阶段**启用两层分发**。
- 阶段 ⑦→⑧，请求层 handler 先被重置为 `block_reading`/`empty_handler`，随即 `r` 被销毁、`c->data` 换回 `hc`、`rev->handler` 换成 `keepalive_handler`——这是「回到等待」的完整动作。

### 5.4 预期结果

你应得到一张填满的 handler 切换表（参考答案见下），并能用一句话回答实践任务：「keepalive 下连接回到等待状态，靠的是 `set_keepalive` 销毁请求对象、把 `c->data` 换回 `hc`、把 `rev->handler` 设为 `keepalive_handler` 并起 keepalive 定时器；下个请求的字节到来时，`keepalive_handler` 再次 `create_request` 并切回 `process_request_line`，闭环回到解析阶段。」

**handler 切换表参考答案**：

| 阶段 | `c->read->handler` | `c->write->handler` | `r->read_event_handler` | `r->write_event_handler` |
|------|----|----|----|----|
| ② | `ngx_http_wait_request_handler` | `ngx_http_empty_handler` | `ngx_http_block_reading` | (未设/0) |
| ③ | `ngx_http_process_request_line` | `ngx_http_empty_handler` | `ngx_http_block_reading` | (未设) |
| ④ | `ngx_http_process_request_headers` | `ngx_http_empty_handler` | `ngx_http_block_reading` | (未设) |
| ⑤ | `ngx_http_request_handler` | `ngx_http_request_handler` | `ngx_http_block_reading` | `ngx_http_core_run_phases` |
| ⑥ | `ngx_http_request_handler` | `ngx_http_request_handler` | `ngx_http_test_reading` | `ngx_http_writer` |
| ⑦ | `ngx_http_request_handler` | `ngx_http_request_handler` | `ngx_http_block_reading` | `ngx_http_request_empty_handler` |
| ⑧ | `ngx_http_keepalive_handler` | `ngx_http_empty_handler` | 无 r | 无 r |

> 说明：阶段 ⑥ 若请求设置了 `discard_body`，`r->read_event_handler` 为 `ngx_http_discarded_request_body_handler` 而非 `test_reading`（见 [L3011-L3013](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3011-L3013)）。

## 6. 本讲小结

- 请求生命周期由一连串 **handler 改写**驱动：`init_connection` 挂 `wait_request_handler` → 读到数据后 `create_request` 诞生 `r` → `process_request_line`/`headers` 解析 → `process_request` 跨入 phase 引擎 → `finalize` 收尾 → keepalive 或关闭。
- `ngx_http_request_t` 是请求级核心结构，诞生于 `ngx_http_alloc_request`：开私有内存池、接三层配置、设 `main`/`count`、置初值 handler；它**短命**，请求结束即随池销毁，而连接级 `ngx_http_connection_t`（`hc`）**长存**跨多个请求。
- nginx 用**两层 handler**：连接层 `c->read/write->handler` 由 epoll 直接调，请求层 `r->read/write_event_handler` 由连接层 handler 间接调。进入 phase 后两者通过 `ngx_http_request_handler` 这个「路由器」衔接，请求靠改写自己的两个 handler 来重新编程读写行为。
- `ngx_http_finalize_request` 是巨型分派器，按 `rc` 语义决定继续 phase、生成错误页、写响应或收尾；`count` 引用计数守护请求在所有异步操作完成前不被释放。
- keepalive 的「回到等待」= `free_request` 销毁 `r` + `c->data` 换回 `hc` + `rev->handler` 设为 `keepalive_handler` + `c->idle=1` + keepalive 定时器；下个请求到来时 `keepalive_handler` 再 `create_request` 切回解析，形成闭环。
- pipelined 请求是 keepalive 的特例：缓冲区里已有下一个请求的数据，`set_keepalive` 不进等待态，而是立即 `create_request` + `process_request_line` + `post_event`，下一轮直接解析。

## 7. 下一步学习建议

- **u6-l3 请求行、头部与请求体解析**：本讲把解析器当黑盒（只看了 `process_request_line`/`headers` 的交接点），下一讲钻进 `ngx_http_parse_request_line`/`ngx_http_parse_header_line` 的状态机，以及 `ngx_http_read_client_request_body` 如何读请求体（涉及 `count++` 的典型异步场景）。
- **u6-l4 请求处理阶段 phases 机制**：本讲提到 `ngx_http_handler` 设 `r->write_event_handler = ngx_http_core_run_phases`，下一讲详解 11 个 phase 与 `NGX_OK/DECLINED/NEXT` 的语义，理解 `finalize(NGX_DECLINED)` 为何能继续推进 phase。
- **u6-l6 过滤器链**：本讲的 `ngx_http_writer` 调 `ngx_http_output_filter(r, NULL)` 驱动响应输出，下一讲拆解 `output_filter` 背后的 header/body filter 链。
- **延伸阅读**：可先读 `ngx_http_test_reading`（[L3138](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3138)）与 `ngx_http_lingering_close_handler`（[L3724](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L3724)），理解 keepalive 之外连接的另两种「善后」路径。
