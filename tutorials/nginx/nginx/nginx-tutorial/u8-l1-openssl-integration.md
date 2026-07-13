# OpenSSL 集成 ngx_event_openssl

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 nginx 为什么不能「直接调用 OpenSSL 的阻塞 API」，以及它如何把 OpenSSL 塞进自己的事件驱动模型；
- 看懂 `ngx_ssl_create_connection`、`ngx_ssl_handshake`、`ngx_ssl_recv_chain` / `ngx_ssl_send_chain` 这四个核心函数，并解释 `SSL_ERROR_WANT_READ` / `SSL_ERROR_WANT_WRITE` 在非阻塞语义下的真正含义；
- 理解一次 TLS 握手是如何在 epoll 的反复唤醒下，分多轮才完成的；
- 掌握会话缓存（session cache）与会话票据（session ticket）的复用机制，以及 SNI（Server Name Indication）回调如何让一个监听端口服务多个证书。

本讲是第八单元「SSL/TLS 与现代协议」的第一篇，也是后续 HTTP/2（u8-l2）、HTTP/3 与 QUIC（u8-l3）以及证书缓存（u8-l4）的地基。

## 2. 前置知识

### 2.1 OpenSSL 是阻塞式 API，nginx 是非阻塞式架构

OpenSSL 提供的 `SSL_read` / `SSL_write` / `SSL_do_handshake` 看起来像普通的 `read` / `write`，但它们背后是一个完整的状态机：一次「读」操作可能在内部需要先「写」（发送握手报文），一次「写」操作也可能在内部需要先「读」（等待对端握手完成）。而且 OpenSSL 的这些调用默认是**阻塞**的——数据没就绪就一直挂着等。

nginx 的 worker 却是单线程、非阻塞、事件驱动的（见 u5 单元）。它绝对不能让任何一个连接把线程阻塞住。于是 nginx 必须做两件事：

1. 给 OpenSSL 换上非阻塞的「socket 生物」（BIO），并用 `SSL_get_error()` 判断调用是否需要等待；
2. 把「需要等待」翻译成 epoll 的「下次再来」（`NGX_AGAIN`），并挂上正确的读写事件。

这是本讲一切代码的根本出发点。

### 2.2 TLS 握手是多轮往返

一个完整的 TLS 1.2 握手至少需要 2 个 RTT（来回），TLS 1.3 需要 1 个 RTT。这意味着 `SSL_do_handshake()` 绝不可能在一次调用里完成——它会先发出 ClientHello/ServerHello，然后返回「我还需要读更多数据」。nginx 的 epoll 在对端数据到达时再次唤醒，worker 再调一次 `SSL_do_handshake()`，如此往复，直到返回 1 表示握手完成。

理解「握手 = 多次事件循环」是本讲的核心直觉。

### 2.3 名词速查

| 术语 | 含义 |
|------|------|
| `SSL_CTX` | OpenSSL 的「证书上下文」，一个 server 配置对应一个，持有证书、密钥、密码套件、会话缓存等。配置期建好，长期存在。 |
| `SSL` | OpenSSL 的「连接对象」，每条 TLS 连接一个，握手与加解密都在它上面进行。 |
| `SSL_ERROR_WANT_READ/WRITE` | OpenSSL 的非阻塞返回码：当前操作没完成，需要更多「读」或「写」事件。 |
| SNI | 客户端在 ClientHello 里带上目标域名，服务器据此选择证书。 |
| 会话复用 | 握手代价高，复用已有会话可省掉一次完整握手。 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/event/ngx_event_openssl.h` | 数据结构与函数声明：`ngx_ssl_t`、`ngx_ssl_connection_t`、协议掩码、各种 `ngx_ssl_*` 原型。 |
| `src/event/ngx_event_openssl.c` | core 层的 OpenSSL 集成实现：初始化、连接创建、握手、加密读写、会话缓存、错误处理。**本讲主角。** |
| `src/http/modules/ngx_http_ssl_module.c` | HTTP 层的 `ssl_*` 指令定义、`SSL_CTX` 的构建、SNI 与 ALPN 回调注册、会话缓存配置。 |
| `src/http/ngx_http_request.c` | HTTP 请求入口：检测到 HTTPS 连接后触发 `ngx_ssl_create_connection` + `ngx_ssl_handshake`，以及 SNI 回调 `ngx_http_ssl_servername`。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：4.1 总体架构与数据结构 → 4.2 `ngx_ssl_create_connection` → 4.3 `ngx_ssl_handshake`（含非阻塞重试）→ 4.4 `ngx_ssl_recv_chain` / `ngx_ssl_send_chain` → 4.5 会话复用与 SNI。

### 4.1 总体架构与关键数据结构

#### 4.1.1 概念说明

nginx 的 SSL 集成分两层：

- **配置层（长期）**：每个 `server {}` 块在配置解析时建一个 `SSL_CTX`，封装进 `ngx_ssl_t`。它持有证书、密钥、协议、密码套件、会话缓存等，整个 worker 生命周期里只读使用。
- **连接层（短期）**：每条 TLS 连接握手时，基于某个 `SSL_CTX` 创建一个 `SSL` 连接对象，封装进 `ngx_ssl_connection_t`，挂到 nginx 的 `ngx_connection_t` 上（`c->ssl`）。握手完成前后的状态、缓冲、OCSP 校验、early data 都记在这里。

两个结构的关键字段：

```c
struct ngx_ssl_s {
    SSL_CTX                    *ctx;          /* OpenSSL 证书上下文 */
    ngx_log_t                  *log;
    size_t                      buffer_size;  /* 发送缓冲大小，默认 16K */
    ngx_array_t                 certs;
    ngx_rbtree_t                staple_rbtree; /* OCSP stapling 用 */
    ngx_rbtree_node_t           staple_sentinel;
};

struct ngx_ssl_connection_s {
    ngx_ssl_conn_t             *connection;   /* 即 SSL*，OpenSSL 连接对象 */
    SSL_CTX                    *session_ctx;  /* 握手所用 ctx，会话复用要用 */
    ngx_int_t                   last;         /* 上次 recv 的结果缓存 */
    ngx_buf_t                  *buf;          /* 发送缓冲（开启 ssl_buffering 时） */
    ngx_connection_handler_pt   handler;      /* 握手完成/失败回调 */
    ngx_ssl_session_t          *session;
    ngx_connection_handler_pt   save_session;
    ngx_event_handler_pt        saved_read_handler;   /* WANT_WRITE 临时接管读事件 */
    ngx_event_handler_pt        saved_write_handler;  /* WANT_READ 临时接管写事件 */
    ...
    unsigned                    handshaked:1;          /* 握手是否完成 */
    unsigned                    handshake_rejected:1;  /* SNI 找不到 server */
    unsigned                    renegotiation:1;
    unsigned                    buffer:1;              /* 是否开启发送缓冲 */
    unsigned                    sendfile:1;            /* kTLS 零拷贝 */
    unsigned                    sni_accepted:1;        /* SNI 已选定 server */
    ...
};
```

参见 [src/event/ngx_event_openssl.h:104-113](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L104-L113)（`ngx_ssl_t`）与 [src/event/ngx_event_openssl.h:116-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L116-L152)（`ngx_ssl_connection_t`）。

> 注意两个字段：`saved_read_handler` / `saved_write_handler`。它们是本讲的「秘密武器」——当一次加密读操作内部需要写（`WANT_WRITE`）时，nginx 会临时把「写事件」的 handler 改成自己的内部函数，等写就绪后回调去续上读操作。4.3、4.4 会详细展开。

#### 4.1.2 核心流程：一次 HTTPS 请求经过 SSL 的完整链路

```
客户端 TCP 连接到达
  └─ ngx_event_accept (u5-l3) 分配 ngx_connection_t
      └─ ngx_http_init_connection 发现 addr_conf->ssl，挂 handler=ngx_http_ssl_handshake
          └─ ngx_http_ssl_handshake: MSG_PEEK 嗅探首字节
              ├─ 不是 TLS → 当普通 HTTP 处理
              └─ 是 TLS → ngx_ssl_create_connection + ngx_ssl_handshake
                  └─ SSL_do_handshake 一次调不完 → 返回 NGX_AGAIN，挂握手 handler
                      └─ epoll 反复唤醒 → ngx_ssl_handshake_handler → ngx_ssl_handshake
                          └─ n==1 握手完成 → 把 c->recv/send 换成 ngx_ssl_recv/write
                              └─ ngx_http_ssl_handshake_handler → 进入正常 HTTP 流程
```

关键点：握手前 `c->recv` 是系统 `recv`，握手完成后被替换成 `ngx_ssl_recv`（见 [src/event/ngx_event_openssl.c:2237-2240](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2237-L2240)）。此后所有 HTTP 数据收发都自动走加密通道，HTTP 核心层对此无感知。

#### 4.1.3 源码精读：全局初始化 ngx_ssl_init

`ngx_ssl_init` 在 master 启动早期调用一次，做两件事：初始化 OpenSSL 库、申请一批 `ex_data` 索引。`ex_data` 是 OpenSSL 提供的「在它自己的对象上挂自定义数据」的机制：

参见 [src/event/ngx_event_openssl.c:150-302](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L150-L302)。

其中最关键的一行：

```c
ngx_ssl_connection_index = SSL_get_ex_new_index(0, NULL, NULL, NULL, NULL);
```

这申请了一个「挂在 `SSL*` 对象上的槽位」。之后每次建连接时，nginx 把对应的 `ngx_connection_t*` 塞进这个槽（见 4.2），OpenSSL 回调（如 SNI）里就能用宏 `ngx_ssl_get_connection(ssl_conn)` 反查回 nginx 连接：

```c
#define ngx_ssl_get_connection(ssl_conn) \
    SSL_get_ex_data(ssl_conn, ngx_ssl_connection_index)
```

见 [src/event/ngx_event_openssl.h:313-314](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L313-L314)。这是 OpenSSL 回调能「找回 nginx 世界」的桥梁，4.5 的 SNI 回调就靠它。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：建立「两层结构」的整体印象。
2. **步骤**：打开 `src/event/ngx_event_openssl.h`，对照 L116-L152 的位域标志，逐一写下你猜测每个标志的置位时机（如 `handshaked`、`buffer`、`sni_accepted`、`try_early_data`）。
3. **观察**：随后阅读 4.2~4.5 时，回头核对你的猜测是否正确。
4. **预期结果**：能用自己的话说清「握手前后 `ngx_connection_t` 的哪些字段发生了变化」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ngx_ssl_t`（含 `SSL_CTX`）放在 server 配置里、而 `ngx_ssl_connection_t`（含 `SSL`）每条连接新建一个？

**答案**：`SSL_CTX` 是相对静态的「模板」（证书、密钥、密码套件、会话缓存策略），构造代价高、可被大量连接共享；`SSL` 是握手与加解密的「实例」，每条连接状态独立。让模板共享、实例独占，既省内存又避免连接间相互污染。

**练习 2**：`ngx_ssl_get_connection(ssl_conn)` 这个宏的作用是什么？没有它会怎样？

**答案**：它用 `ex_data` 从 OpenSSL 的 `SSL*` 反查 nginx 的 `ngx_connection_t*`。OpenSSL 的回调（SNI、ALPN、new_session 等）签名只给 `SSL*`，若没有这个桥梁，回调里就拿不到 nginx 连接上下文（日志、配置、所属 server），整套集成就无法实现。

---

### 4.2 ngx_ssl_create_connection：把 SSL 绑到一条连接

#### 4.2.1 概念说明

`ngx_ssl_create_connection` 做的事很纯粹：给定一个配置好的 `ngx_ssl_t`（含 `SSL_CTX`）和一条刚 accept 的 `ngx_connection_t`，为它创建并绑定一个 `SSL` 连接对象。这是从「裸 TCP 连接」变成「待握手的 TLS 连接」的转折点。

`flags` 参数有两个标志：`NGX_SSL_BUFFER`（开启发送缓冲，把多个小写合并成一个大 TLS 记录）、`NGX_SSL_CLIENT`（作为客户端发起握手，用于 nginx 作反代后端时的 upstream SSL）。见 [src/event/ngx_event_openssl.h:220-221](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L220-L221)。

#### 4.2.2 核心流程

```
ngx_ssl_create_connection(ssl, c, flags)
  ├─ 1. ngx_pcalloc 一个 ngx_ssl_connection_t（sc）
  ├─ 2. 记 sc->buffer / sc->buffer_size / sc->session_ctx
  ├─ 3. SSL_new(ssl->ctx)        建 SSL 连接对象
  ├─ 4. SSL_set_fd(...)          绑到 c->fd
  ├─ 5. 分客户端/服务端：
  │      NGX_SSL_CLIENT → SSL_set_connect_state   (主动发起握手)
  │      否则          → SSL_set_accept_state     (等对端发起)
  │                     并设 SSL_OP_NO_RENEGOTIATION 禁止重协商
  ├─ 6. SSL_set_ex_data(... ngx_ssl_connection_index, c)  把 c 挂回 SSL*
  └─ 7. c->ssl = sc             让 nginx 连接持有 SSL 状态
```

#### 4.2.3 源码精读

参见 [src/event/ngx_event_openssl.c:2107-2158](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2107-L2158)，关键几行：

```c
sc->connection = SSL_new(ssl->ctx);          /* 2117-2132: 建连接对象并绑 fd */
if (sc->connection == NULL) { ... return NGX_ERROR; }

if (SSL_set_fd(sc->connection, c->fd) == 0) { ... return NGX_ERROR; }

if (flags & NGX_SSL_CLIENT) {
    SSL_set_connect_state(sc->connection);   /* 2139-2148: 客户端 vs 服务端 */
} else {
    SSL_set_accept_state(sc->connection);
    SSL_set_options(sc->connection, SSL_OP_NO_RENEGOTIATION); /* 禁重协商 */
}

if (SSL_set_ex_data(sc->connection, ngx_ssl_connection_index, c) == 0) {
    ...; return NGX_ERROR;                   /* 2150-2153: 反向桥梁 */
}

c->ssl = sc;                                 /* 2155: 双向持有 */
```

注意 4.1.3 申请的 `ngx_ssl_connection_index` 在这里被「写入」：第 2150 行把 nginx 连接 `c` 塞进 `SSL*`，于是 OpenSSL 任何回调里 `ngx_ssl_get_connection()` 都能找回 `c`。

#### 4.2.4 代码实践（源码阅读型）

1. **目标**：理解服务端模式与客户端模式的区别。
2. **步骤**：
   - 在 `src/http/ngx_http_request.c` 搜 `ngx_ssl_create_connection`，看 HTTPS 服务端调用时传的 flags（应为 `NGX_SSL_BUFFER`）。
   - 在 `src/http/ngx_http_upstream.c` 搜 `ngx_ssl_create_connection`，看反代后端调用时传的 flags（应含 `NGX_SSL_CLIENT`）。
3. **观察**：两边 flags 差异如何对应 `SSL_set_accept_state` / `SSL_set_connect_state`。
4. **预期结果**：能说清「同一个函数，flags 不同就走不同的握手发起方」。

#### 4.2.5 小练习与答案

**练习 1**：为什么服务端要设 `SSL_OP_NO_RENEGOTIATION`？

**答案**：TLS 重协商（renegotiation）存在已知安全风险（如 CVE-2009-3555 中间人攻击），且会让事件循环复杂化。nginx 在服务端建连接时直接禁掉，既堵漏洞又简化逻辑。

**练习 2**：`ngx_ssl_create_connection` 里没有任何 `connect()` 或 `SSL_do_handshake()`，它只「准备」不「执行」。为什么？

**答案**：它只完成「对象创建 + 状态标记 + 桥梁建立」，把连接摆到「准备好握手」的起跑线。真正的握手在 `ngx_ssl_handshake` 里异步进行——这是为了不阻塞 worker 事件循环。

---

### 4.3 ngx_ssl_handshake：非阻塞握手与多轮重试

这是本讲最核心、也最体现「OpenSSL 与事件驱动融合」的模块。

#### 4.3.1 概念说明

`SSL_do_handshake()` 在非阻塞 socket 上有三种典型返回：

| `SSL_get_error` 结果 | 含义 | nginx 的动作 |
|---|---|---|
| 返回 1（成功） | 握手完成 | 把 `c->recv/send` 换成加密版，回调上层 |
| `SSL_ERROR_WANT_READ` | 还需读更多对端数据 | 把读写 handler 都设成握手 handler，返回 `NGX_AGAIN` |
| `SSL_ERROR_WANT_WRITE` | 还需先发数据 | 同上（握手既可能要读也可能要写） |
| 其它 | 出错 | 走 `ngx_ssl_connection_error` |

关键直觉：**TLS 握手是双向的**。服务器在握手过程中既要收（ClientHello、ClientKeyExchange、Finished）也要发（ServerHello、Certificate、ServerHelloDone、Finished）。所以一次握手既可能 `WANT_READ` 也可能 `WANT_WRITE`，nginx 必须把读写两个事件都挂上同一个 handler，哪边就绪都继续推。

#### 4.3.2 核心流程

```
ngx_ssl_handshake(c)
  ├─ n = SSL_do_handshake(c->ssl->connection)
  ├─ if (n == 1):  握手完成
  │     ├─ ngx_handle_read/write_event 维持事件注册
  │     ├─ 替换 c->recv/send/recv_chain/send_chain 为加密版   ← 关键！
  │     ├─ c->read->ready = c->write->ready = 1
  │     ├─ (可选) kTLS 探测 → c->ssl->sendfile=1
  │     ├─ ngx_ssl_ocsp_validate（可能又异步返回 AGAIN）
  │     ├─ c->ssl->handshaked = 1
  │     └─ return NGX_OK
  ├─ sslerr = SSL_get_error(...)
  ├─ WANT_READ / WANT_WRITE:
  │     ├─ c->read->handler = c->write->handler = ngx_ssl_handshake_handler
  │     ├─ ngx_handle_read_event / ngx_handle_write_event
  │     └─ return NGX_AGAIN
  └─ 其它: ngx_ssl_connection_error(...) → return NGX_ERROR
```

握手 handler（epoll 唤醒时的入口）非常薄：

参见 [src/event/ngx_event_openssl.c:2547-2566](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2547-L2566)：

```c
ngx_ssl_handshake_handler(ngx_event_t *ev)
{
    c = ev->data;
    if (ev->timedout) { c->ssl->handler(c); return; }
    if (ngx_ssl_handshake(c) == NGX_AGAIN) { return; }   /* 还没好，继续等 */
    c->ssl->handler(c);                                   /* 好/坏都通知上层 */
}
```

注意 `c->ssl->handler`：这是上层（HTTP 层）在握手前设置的回调（`ngx_http_ssl_handshake_handler`），握手最终完成或失败都由它接管。这就是「core SSL 层不知道上层是谁、靠回调解耦」的设计。

#### 4.3.3 源码精读

**成功分支**：参见 [src/event/ngx_event_openssl.c:2223-2282](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2223-L2282)。核心是「换 I/O 函数」：

```c
if (n == 1) {
    ...
    c->recv = ngx_ssl_recv;
    c->send = ngx_ssl_write;
    c->recv_chain = ngx_ssl_recv_chain;
    c->send_chain = ngx_ssl_send_chain;     /* 2237-2240: 此后 HTTP 层透明走加密 */
    ...
    c->ssl->handshaked = 1;
    return NGX_OK;
}
```

**重试分支**：参见 [src/event/ngx_event_openssl.c:2288-2318](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2288-L2318)：

```c
if (sslerr == SSL_ERROR_WANT_READ) {
    c->read->ready = 0;
    c->read->handler = ngx_ssl_handshake_handler;
    c->write->handler = ngx_ssl_handshake_handler;   /* 读写都挂同一个 handler */
    if (ngx_handle_read_event(c->read, 0) != NGX_OK) { return NGX_ERROR; }
    if (ngx_handle_write_event(c->write, 0) != NGX_OK) { return NGX_ERROR; }
    return NGX_AGAIN;
}
/* WANT_WRITE 分支结构完全对称 */
```

「读写 handler 都设成 `ngx_ssl_handshake_handler`」是关键：因为握手期间，读事件就绪（对端来了 ClientHello）要推进握手，写事件就绪（可以发 ServerHello）也要推进握手——两者都只需再调一次 `SSL_do_handshake`。

#### 4.3.4 代码实践（结合配置 + 调试日志）

1. **目标**：亲眼看到一次 TLS 握手在 epoll 下被「切成多段」完成。
2. **操作步骤**：
   - 用 `--with-debug` 编译 nginx（见 u1-l2）。
   - 写一个最小 HTTPS 配置（**示例配置**）：
     ```nginx
     events {}
     http {
         server {
             listen 443 ssl;
             server_name localhost;
             ssl_certificate     cert.pem;
             ssl_certificate_key key.pem;
             ssl_protocols       TLSv1.2 TLSv1.3;
             error_log /tmp/ssl_debug.log debug;
             location / { return 200 "hello\n"; }
         }
     }
     ```
   - 用 `openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=localhost"` 生成自签证书。
   - 启动 nginx，用 `openssl s_client -connect 127.0.0.1:443` 发起一次连接。
3. **观察现象**：在 `/tmp/ssl_debug.log` 中 grep `SSL_do_handshake` 与 `SSL_get_error`。
4. **预期结果**：会看到形如 `SSL_do_handshake: -1` → `SSL_get_error: 2`（`SSL_ERROR_WANT_READ`）→ 若干轮后 `SSL_do_handshake: 1` 的序列。这直观证明了「一次握手 = 多次事件循环」。具体行数取决于 RTT 与协议版本，**待本地验证**。
5. **延伸**：用 `openssl s_client -tls1_3` 对比，TLS 1.3 的往返数更少，日志里 WANT_READ 出现次数应明显更少。

#### 4.3.5 小练习与答案

**练习 1**：握手成功后，nginx 立即把 `c->recv` 从系统 `recv` 换成 `ngx_ssl_recv`。HTTP 解析代码（u6-l3 的 `ngx_http_parse_*`）需要因此改动吗？

**答案**：不需要。HTTP 解析只调 `c->recv` / `c->recv_chain` 这组函数指针，并不知道底下是裸 socket 还是 TLS。这正是 nginx 用「函数指针表」抽象 I/O 的好处——SSL 层只要在握手完成时「换实现」，上层完全透明。

**练习 2**：为什么 `WANT_READ` 分支里，连写事件的 handler 也被设成了握手 handler？

**答案**：因为 TLS 握手是双向的，当前等待读数据的同时，可能下一个动作是要发数据（写就绪）；反之亦然。把读写两边都接上同一个推进函数，确保任意一侧就绪都能继续推握手状态机，避免漏掉唤醒。

**练习 3**：`ngx_ssl_handshake_handler` 里的 `c->ssl->handler(c)` 在 `ngx_ssl_handshake` 返回 `NGX_AGAIN` 时不会被调用，只在握手「结束」（成功或失败）后调用。这样设计有什么好处？

**答案**：core SSL 层与上层（HTTP/stream/mail）彻底解耦——SSL 层不关心握手完成后要做什么，只通过一个回调指针通知「我好了/我失败了」，由上层决定是进入请求处理还是关闭连接。同一套 SSL 代码因此能服务 HTTP、stream、mail 等多种协议。

---

### 4.4 ngx_ssl_recv_chain / ngx_ssl_send_chain：加密读写与背压

握手完成后，所有数据收发都走这两个函数（及其底层 `ngx_ssl_recv` / `ngx_ssl_write`）。它们要解决两个难题：**SSL 层的数据分片**与**读操作内部可能要写**。

#### 4.4.1 概念说明

**难题一：SSL_read 可能「返回一部分」。** 一次 TCP `read` 给的字节，可能恰好包含半个 TLS 记录，OpenSSL 会缓存已解密的部分数据。nginx 必须循环 `SSL_read` 直到它说「没更多了」（返回 WANT_READ），否则会漏读。

**难题二：读可能触发写、写可能触发读。** TLS 在数据传输阶段也可能因为 key update、重协商（已禁）等原因，让一次 `SSL_read` 内部需要 `SSL_write`（返回 `WANT_WRITE`）。此时 nginx 的「读事件循环」里突然冒出一个「需要写」的需求，必须临时接管写事件。

`ngx_ssl_handle_recv` 就是专门处理 `SSL_read` 返回值的中枢：它把 OpenSSL 的 `WANT_READ/WANT_WRITE/EOF/ERROR` 翻译成 nginx 的 `NGX_AGAIN/NGX_DONE/NGX_ERROR`，并在 `WANT_WRITE` 时用 `saved_write_handler` 临时接管写事件。

#### 4.4.2 核心流程：读路径

```
ngx_ssl_recv_chain(c, cl, limit)         外层：沿 buf 链循环
  └─ ngx_ssl_recv(c, buf, size)          内层：循环 SSL_read
       ├─ n = SSL_read(...)
       ├─ c->ssl->last = ngx_ssl_handle_recv(c, n)   ← 翻译返回值并缓存
       ├─ last == NGX_OK: 累加，size 归零则返回 bytes
       ├─ last == NGX_AGAIN: 返回已读 bytes 或 NGX_AGAIN
       └─ last == NGX_DONE/ERROR: 置 eof/error，返回 0/NGX_ERROR

ngx_ssl_handle_recv(c, n):
  ├─ n > 0: 若之前 saved_write_handler 待恢复 → 恢复并 post 写事件；返回 NGX_OK
  ├─ WANT_READ: c->read->ready=0; 返回 NGX_AGAIN
  ├─ WANT_WRITE: 临时把写 handler 存进 saved_write_handler，
  │              写 handler 改成 ngx_ssl_write_handler; 返回 NGX_AGAIN
  ├─ 对端干净关闭 (ZERO_RETURN / 无错误): 返回 NGX_DONE
  └─ 其它: ngx_ssl_connection_error; 返回 NGX_ERROR
```

写路径 `ngx_ssl_write` 结构对称：`WANT_READ` 时用 `saved_read_handler` 接管读事件，读就绪时回调 `ngx_ssl_read_handler` 续上写操作。

#### 4.4.3 源码精读

**`ngx_ssl_recv_chain`**：参见 [src/event/ngx_event_openssl.c:2569-2629](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2569-L2629)。它就是沿 buf 链不断调 `ngx_ssl_recv`，注意 `!c->read->ready` 时提前返回（4.3 的背压信号）：

```c
n = ngx_ssl_recv(c, last, size);
if (n > 0) {
    last += n; bytes += n;
    if (!c->read->ready) { return bytes; }   /* SSL 层说「暂时没更多了」 */
    ...
}
```

**`ngx_ssl_recv` 的 SSL_read 循环**：参见 [src/event/ngx_event_openssl.c:2632-2757](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2632-L2757)。核心是「循环读到 SSL 层说没有为止」，并把结果交给 `ngx_ssl_handle_recv`：

```c
for ( ;; ) {
    n = SSL_read(c->ssl->connection, buf, size);
    if (n > 0) { bytes += n; }
    c->ssl->last = ngx_ssl_handle_recv(c, n);   /* 2674: 翻译并缓存 */
    if (c->ssl->last == NGX_OK) {
        size -= n;
        if (size == 0) { ...; return bytes; }
        buf += n; continue;
    }
    ...
}
```

注意 L2683-L2701 的 `c->read->available` 处理：SSL 层内部可能还缓存着已解密数据（内核没数据但 SSL 有），nginx 用负的 `available` 标记这种情况，并把读事件 post 到 `ngx_posted_next_events`，下一轮事件循环继续读——避免漏掉 SSL 内部缓冲的数据。

**`ngx_ssl_handle_recv` 的 WANT_WRITE 接管**：参见 [src/event/ngx_event_openssl.c:2959-2980](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2959-L2980)：

```c
if (sslerr == SSL_ERROR_WANT_WRITE) {
    c->write->ready = 0;
    if (ngx_handle_write_event(c->write, 0) != NGX_OK) { return NGX_ERROR; }
    /* 不设定时器：读事件已有定时器 */
    if (c->ssl->saved_write_handler == NULL) {
        c->ssl->saved_write_handler = c->write->handler;   /* 保存原 handler */
        c->write->handler = ngx_ssl_write_handler;          /* 临时接管 */
    }
    return NGX_AGAIN;
}
```

当写就绪时，`ngx_ssl_write_handler`（[L2997-L3007](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L2997-L3007)）简单地回调 `c->read->handler(c->read)`——把控制权交还给读路径继续 `SSL_read`。这就是「读操作内部需要写」的完整解法。

**`ngx_ssl_send_chain` 的发送缓冲**：参见 [src/event/ngx_event_openssl.c:3018-3203](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L3018-L3203)。函数开头有一段注释点明设计意图（[L3010-L3016](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L3010-L3016)）：OpenSSL 没有 `SSL_writev`，nginx 于是把多个小 buf 拷进一个 16K 缓冲再一次性 `SSL_write`，以减少 TLS 记录的开销（每条 TLS 记录都有头部与 MAC/AEAD 成本）。

```c
if (!c->ssl->buffer) {           /* 未开缓冲：逐 buf 直接写 */
    while (in) {
        n = ngx_ssl_write(c, in->buf->pos, in->buf->last - in->buf->pos);
        ...
    }
    return in;
}
/* 开缓冲：拷贝合并到 c->ssl->buf，再统一 SSL_write */
```

未发完时把剩余 buf 指针返回（`NGX_AGAIN` 语义），上层据此做背压（见 u2-l4 的 `ngx_output_chain`）。

**`ngx_ssl_write`**：参见 [src/event/ngx_event_openssl.c:3206-3314](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L3206-L3314)，结构与 `ngx_ssl_recv` 对称。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：理解「读内部需要写」这一反直觉现象。
2. **步骤**：
   - 在 `ngx_ssl_handle_recv` 的 `WANT_WRITE` 分支（L2959）旁注释：何时 `SSL_read` 会返回 `WANT_WRITE`？
   - 在 `ngx_ssl_write` 的 `WANT_READ` 分支（L3283）旁注释：何时 `SSL_write` 会返回 `WANT_READ`？
3. **观察**：这两个「交叉」分支正是 TLS 状态机在传输阶段的体现（如对端发 key update、或 TLS 1.3 的 post-handshake）。
4. **预期结果**：能用一句话说清「为什么 nginx 在读路径里要处理写事件」。

#### 4.4.5 小练习与答案

**练习 1**：`ngx_ssl_recv` 里为什么用一个 `for ( ;; )` 循环反复调 `SSL_read`，而不是调一次就返回？

**答案**：因为一次 `SSL_read` 可能只吐出 SSL 层已解密数据的一部分，内核 socket 可能没新数据但 SSL 内部缓冲还有。必须循环到 `SSL_read` 返回 `WANT_READ`（被 `ngx_ssl_handle_recv` 翻成 `NGX_AGAIN`）才能确认「真没数据了」，否则会漏读、让 HTTP 解析误以为请求体不完整。

**练习 2**：`ngx_ssl_send_chain` 开启 `NGX_SSL_BUFFER` 时，把数据拷进 16K 缓冲再发。这一步会带来延迟吗？nginx 如何避免？

**答案**：会——如果一直攒不满 16K 就不发，响应会卡住。nginx 的对策是：当遇到带 `last_buf` / `flush` 标志的 buf（[L3090-L3092](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L3090-L3092)）或上层显式 flush 时，立即把缓冲里的数据冲刷出去。HTTP 响应的最后一个 buf 总带 `last_buf`，所以响应不会被无限拖延。

**练习 3**：`c->ssl->last` 字段缓存了上一次 `ngx_ssl_handle_recv` 的结果。读它的两个早期分支（`NGX_ERROR` / `NGX_DONE`，L2643-L2653）有什么作用？

**答案**：一旦某次读判定为永久错误或对端关闭，后续的读请求无需再调 `SSL_read`，直接复用缓存结果返回 `NGX_ERROR` 或 0（eof）。这是一个「粘性」状态，避免在连接已坏时还反复触发昂贵的 OpenSSL 调用。

---

### 4.5 会话复用与 SNI：HTTP SSL 模块的集成

本模块回答学习目标里的第三点：会话缓存与 SNI 如何处理。这部分代码主要在 `src/http/modules/ngx_http_ssl_module.c` 和 `src/http/ngx_http_request.c`。

#### 4.5.1 概念说明

**会话复用（Session Reuse）**：完整 TLS 握手要做非对称运算（很贵）。若客户端带上之前会话的 ID 或票据，服务端可跳过大部分步骤，直接恢复会话。nginx 支持两种存储方式：

1. **内置/共享内存缓存（session cache）**：`ssl_session_cache` 指令。设 `shared:NAME:SIZE` 时跨 worker 共享（基于共享内存 + slab，见 u4-l3），key 是会话 ID。
2. **会话票据（session ticket）**：服务端把会话用密钥加密成票据发给客户端，客户端下次带回，服务端解密即恢复。无需服务端存状态。

**SNI（Server Name Indication）**：一个 `listen 443 ssl` 端口背后可能挂多个 server（多个证书）。客户端在 ClientHello 里带上域名，nginx 在握手**期间**（证书还没选定时）收到这个域名，据此查找匹配的 `server {}`、切换到对应的 `SSL_CTX`（证书）。这一切发生在 `SSL_do_handshake` 的回调里。

#### 4.5.2 核心流程：SNI 回调链

```
配置期 ngx_http_ssl_merge_srv_conf:
  ├─ ngx_ssl_create → SSL_CTX_new
  ├─ SSL_CTX_set_tlsext_servername_callback(ngx_http_ssl_servername)  ← 注册 SNI 回调
  ├─ SSL_CTX_set_alpn_select_cb(ngx_http_ssl_alpn_select)             ← 注册 ALPN 回调
  └─ ngx_ssl_session_cache → 注册 new/get/remove 会话回调

握手期 OpenSSL 解析 ClientHello，取出 servername:
  └─ 回调 ngx_http_ssl_servername(ssl_conn, ...)
       ├─ c = ngx_ssl_get_connection(ssl_conn)   ← 用 ex_data 反查 nginx 连接
       ├─ servername = SSL_get_servername(...)    ← 取客户端带的域名
       ├─ ngx_http_validate_host 校验
       ├─ ngx_http_find_virtual_server 查匹配的 server{}
       ├─ SSL_set_SSL_CTX 换到目标 server 的 SSL_CTX（即换证书！）
       └─ SSL_set_verify 等按目标 server 的配置调整
```

#### 4.5.3 源码精读

**配置期注册 SNI 与 ALPN 回调**：参见 [src/http/modules/ngx_http_ssl_module.c:777-799](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L777-L799)：

```c
#ifdef SSL_CTRL_SET_TLSEXT_HOSTNAME
{
    static ngx_ssl_client_hello_arg cb = { ngx_http_ssl_servername };
    ngx_ssl_set_client_hello_callback(&conf->ssl, &cb);            /* client_hello 回调 */
    SSL_CTX_set_tlsext_servername_callback(conf->ssl.ctx,
                                           ngx_http_ssl_servername); /* SNI 回调 */
}
#endif
SSL_CTX_set_alpn_select_cb(conf->ssl.ctx, ngx_http_ssl_alpn_select, NULL); /* ALPN，选 h2 */
```

**SNI 回调实现**：参见 [src/http/ngx_http_request.c:884-958](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L884-L958)：

```c
int ngx_http_ssl_servername(ngx_ssl_conn_t *ssl_conn, int *ad, void *arg)
{
    c = ngx_ssl_get_connection(ssl_conn);            /* ex_data 反查 */
    if (c->ssl->handshaked) { *ad = SSL_AD_NO_RENEGOTIATION; ... }
    if (c->ssl->sni_accepted) { return SSL_TLSEXT_ERR_OK; }
    ...
    servername = SSL_get_servername(ssl_conn, TLSEXT_NAMETYPE_host_name);
    host.data = (u_char *) servername;
    rc = ngx_http_validate_host(&host, ...);
    rc = ngx_http_find_virtual_server(c, hc->addr_conf->virtual_names, &host, ...);
    ...
}
```

回调里 `ngx_http_find_virtual_server` 沿着该监听端口的虚拟主机表找到匹配 server，随后把连接的 `SSL_CTX` 切到那个 server 的配置——这就是「一个端口多证书」的实现原理。

**会话缓存配置**：`ssl_session_cache` 指令最终调 `ngx_ssl_session_cache`，参见 [src/event/ngx_event_openssl.c:4101-4171](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L4101-L4171)。核心是根据配置设 `SSL_CTX_set_session_cache_mode`，并在使用共享内存时注册自定义的 new/get/remove 回调（让 nginx 自己用 slab 管理会话，而非 OpenSSL 内部 malloc）：

```c
SSL_CTX_set_session_cache_mode(ssl->ctx, cache_mode);
...
if (shm_zone) {
    SSL_CTX_sess_set_new_cb(ssl->ctx, ngx_ssl_new_session);      /* 新会话存进共享内存 */
    SSL_CTX_sess_set_get_cb(ssl->ctx, ngx_ssl_get_cached_session); /* 取回会话 */
    SSL_CTX_sess_set_remove_cb(ssl->ctx, ngx_ssl_remove_session);  /* 过期清理 */
}
```

merge 阶段调用点见 [src/http/modules/ngx_http_ssl_module.c:911-941](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L911-L941)。

> 一个易混点：`NGX_SSL_NONE_SCACHE`（默认值，见 merge L911-L912）不是「关闭」，而是「假装支持但不真存」——为兼容某些客户端（注释里提到 Outlook Express）在 `SSL_SESS_CACHE_OFF` 下的怪行为。真正彻底关闭是 `ssl_session_cache none` 对应的 `NGX_SSL_NO_SCACHE`（[L4113-L4116](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L4113-L4116)）。

**HTTP 层触发握手**：`ngx_http_init_connection` 检测到 `addr_conf->ssl` 时，把读 handler 设为 `ngx_http_ssl_handshake`（[src/http/ngx_http_request.c:338-344](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L338-L344)）。后者用一个巧妙的 `MSG_PEEK` 嗅探首字节：若以 `0x16`（TLS handshake）或 `0x80`（SSLv2）开头才创建 SSL 连接，否则当普通 HTTP 处理（[L764-L815](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L764-L815)）。这使同一端口可同时接受 HTTP 与 HTTPS（配合 `ssl off` 与错误降级）。握手成功后由 `ngx_http_ssl_handshake_handler`（[L823-L879](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L823-L879)）切换到 `ngx_http_wait_request_handler`，正式开始读 HTTP 请求。

#### 4.5.4 代码实践（结合配置）

1. **目标**：观察会话复用与 SNI 的效果。
2. **操作步骤**：
   - 配置两个 server 共用 443、证书不同：
     ```nginx
     server { listen 443 ssl; server_name a.local; ssl_certificate a.pem; ssl_certificate_key a.key; ssl_session_cache shared:SSL:10m; ssl_session_timeout 10m; location / { return 200 "a\n"; } }
     server { listen 443 ssl; server_name b.local; ssl_certificate b.pem; ssl_certificate_key b.key; location / { return 200 "b\n"; } }
     ```
   - 用 `openssl s_client -connect 127.0.0.1:443 -servername a.local` 连两次：第一次完整握手，第二次应复用会话。
3. **观察现象**：
   - 第二次连接输出里出现 `Reused, ...`（TLS 1.2）或在 TLS 1.3 下票据恢复。
   - 用 `-servername b.local` 时，服务端返回 b 的证书（` openssl s_client ... | openssl x509 -noout -subject`）。
4. **预期结果**：会话复用让第二次握手的 debug 日志里 `SSL_do_handshake` 的往返次数明显减少；SNI 让同一端口按域名返回不同证书。**待本地验证**具体日志行数。

#### 4.5.5 小练习与答案

**练习 1**：`ssl_session_cache shared:SSL:10m` 里的 `shared` 为什么必须用共享内存？

**答案**：nginx 是多 worker 进程，每个 worker 独立处理连接。客户端第一次握手落在 worker A、第二次复用可能落在 worker B。会话状态只有放在所有 worker 都能访问的共享内存里，B 才能取到 A 存的会话。这正是 u4-l3「共享内存 + slab」的典型应用。

**练习 2**：SNI 回调发生在握手的哪个阶段？为什么证书选择必须在握手「期间」而不是握手「之后」？

**答案**：发生在 OpenSSL 解析完 ClientHello 之后、发送 Certificate 之前。因为服务端要在握手报文里把自己的证书发给客户端，证书必须在发送 Certificate 之前就选定。等到握手结束再选就来不及了——证书已经按默认 `SSL_CTX` 发出去了。所以 nginx 用 SNI 回调在握手「中途」切换 `SSL_CTX`。

**练习 3**：会话票据（session ticket）与会话缓存（session cache）相比，有什么优势？

**答案**：票据是「无状态」的——服务端把会话加密后交给客户端保管，自己不存任何状态，因此不占服务端内存、天然支持多 worker（每个 worker 只需共享票据加密密钥）。缺点是密钥泄露等于所有会话暴露，且票据密钥需定期轮换。两者 nginx 都支持，可并用。

## 5. 综合实践

把本讲知识串起来：**追踪一次 HTTPS 请求从 TCP accept 到第一个 HTTP 字节被解析的全过程，标注 SSL 层的每一次介入。**

1. 准备：按 4.3.4 配置一个 `--with-debug` 的 HTTPS server。
2. 用 `curl -k https://localhost/` 发起一次请求，同时 `tail -f` 调试日志。
3. 在一张图上标出以下事件，并写出对应的源码位置：
   - `ngx_event_accept` 接受连接（u5-l3）；
   - `ngx_http_init_connection` 把 handler 设为 `ngx_http_ssl_handshake`（`ngx_http_request.c:342`）；
   - `ngx_http_ssl_handshake` 用 `MSG_PEEK` 嗅探、识别 TLS、调 `ngx_ssl_create_connection`（`nginx.c` 的 SSL 新建）+ `ngx_ssl_handshake`；
   - 多轮 `SSL_do_handshake` / `SSL_get_error: 2`（WANT_READ），SNI 回调 `ngx_http_ssl_servername` 被触发（grep 日志里的 `SSL server name`）；
   - 最终 `SSL_do_handshake: 1`，`c->recv` 被换成 `ngx_ssl_recv`；
   - `ngx_http_ssl_handshake_handler` 切到 `ngx_http_wait_request_handler`；
   - `ngx_ssl_recv` → `SSL_read` 读出 HTTP 请求行。
4. 关掉 `ssl_session_cache`（设 `none`）再请求一次，对比握手往返次数，体会会话复用的价值。
5. 把你的图与 4.1.2 的流程图对照，修正理解偏差。

预期：你能用一条完整的「epoll 唤醒 → handler → SSL 调用 → 返回值翻译 → 下一次唤醒」的链路，解释 TLS 在 nginx 里是如何被「事件化」的。

## 6. 本讲小结

- nginx 用「配置层 `ngx_ssl_t`/`SSL_CTX` + 连接层 `ngx_ssl_connection_t`/`SSL`」两层结构，把 OpenSSL 的阻塞 API 改造成非阻塞、事件驱动的形式；`ex_data` 是 OpenSSL 回调反查 nginx 连接的桥梁。
- `ngx_ssl_create_connection` 只做「准备」：`SSL_new` + `SSL_set_fd` + 设 accept/connect 状态 + 建反向桥梁，不执行握手。
- `ngx_ssl_handshake` 的核心是处理 `WANT_READ/WANT_WRITE`：把读写两个事件的 handler 都设成 `ngx_ssl_handshake_handler`，epoll 反复唤醒、反复调 `SSL_do_handshake`，直到返回 1。成功后立刻把 `c->recv/send` 换成加密版，HTTP 层无感知。
- `ngx_ssl_recv` / `ngx_ssl_send_chain` 解决「SSL_read 分片」与「读内部可能要写」两个难题：循环读、用 `ngx_ssl_handle_recv` 翻译返回值，在 `WANT_WRITE` 时用 `saved_write_handler` 临时接管写事件；发送侧用 16K 缓冲合并多个小 buf 以降低 TLS 记录开销。
- 会话复用通过 `ssl_session_cache`（共享内存 + slab）与会话票据实现；SNI 在握手「期间」经回调 `ngx_http_ssl_servername` 切换 `SSL_CTX`，实现一个端口多证书。

## 7. 下一步学习建议

- **u8-l2 HTTP/2 实现解析**：HTTPS 握手完成后，`ngx_http_ssl_handshake_handler` 会检查 ALPN 是否协商出 `h2`（本讲 4.5.3 已埋下伏笔，见 `ngx_http_request.c:850-857`），据此进入 HTTP/2 流程。下一讲将展开 HTTP/2 的帧层与多路复用。
- **u8-l3 HTTP/3 与 QUIC**：QUIC 把传输层加密（TLS 1.3）和 UDP 传输合体，握手集成方式与本讲完全不同，可对比体会。
- **u8-l4 证书缓存与 OCSP Stapling**：本讲的 `ngx_http_ssl_certificate`（动态证书，按 SNI 选）与 OCSP 校验只是入口，下一讲深入 `ngx_event_openssl_cache.c` 与 `ngx_event_openssl_stapling.c`。
- **延伸阅读源码**：`ngx_ssl_shutdown`（[L3636](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L3636)）演示了优雅关闭 TLS 连接（双向 close_notify）的非阻塞处理，与握手的重试模式如出一辙，建议对照阅读以巩固本讲的「非阻塞重试」范式。
