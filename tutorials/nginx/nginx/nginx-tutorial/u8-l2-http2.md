# HTTP/2 实现解析

## 1. 本讲目标

本讲带你进入 nginx 的现代协议实现层——HTTP/2。读完本讲，你应当能够：

- 说清 HTTP/2 在一条 TCP 连接上同时跑多个请求的原理（帧、流、多路复用）。
- 跟着源码走通一条 HTTP/2 请求的完整路径：从 TLS ALPN 协商、连接前言（preface）校验、逐帧状态机解析，到创建 stream 并交回标准 HTTP 框架处理。
- 理解 HPACK 头部压缩的静态表、动态表（环形缓冲）、变长整数与 Huffman 编码。
- 看懂双层流量控制（连接级 + 流级窗口）如何在收发两端配合。
- 理解 v2 过滤器如何把 HTTP 核心的响应输出"翻译"成 HEADERS/DATA 帧。

本讲依赖你已经学过 [u6-l2 HTTP 请求生命周期](u6-l2-request-lifecycle.md)（请求对象、`ngx_http_finalize_request`、读/写 handler 的两层机制），以及 [u8-l1 OpenSSL 集成](u8-l1-openssl-integration.md)（TLS 握手、`ngx_connection_t`）。我们会反复用到这两讲建立的认知。

## 2. 前置知识

### 2.1 HTTP/1.1 的痛点

在 HTTP/1.1 里，浏览器为了快速加载一个网页，往往要为同一个域名同时开几十条 TCP 连接（连接池），原因有二：

- **队头阻塞（Head-of-Line Blocking）**：一条 keepalive 连接上的请求必须串行处理，前一个响应没回完，后一个就得等。
- **连接开销**：每条 TCP 连接都要经历三次握手、TLS 握手和 TCP 慢启动，开得多代价就大。

此外 HTTP/1.1 的头部是纯文本，且大量重复（`Cookie`、`User-Agent` 每次都几乎一样），浪费带宽。

### 2.2 HTTP/2 的五点改进

| 改进 | 一句话说明 |
|------|-----------|
| 二进制分帧层 | 把所有通信拆成带 9 字节头部的「帧(frame)」，不再按文本行解析 |
| 多路复用 | 一条 TCP 连接上并发多个「流(stream)」，每条流承载一个请求/响应 |
| HPACK 头部压缩 | 用静态表 + 动态表 + Huffman 压缩请求/响应头 |
| 流量控制 | 类似 TCP 窗口，但分「连接级」和「流级」两层 |
| 服务器推送 | 服务器可主动推送资源（nginx 默认禁用） |

需要特别记住：**HTTP/2 仍然跑在 TCP 上**。因此它解决了「HTTP 层队头阻塞」，却没解决「TCP 层队头阻塞」——一个 TCP 包丢失会卡住这条连接上所有流。这正是 HTTP/3 over QUIC 要解决的问题（见后续 [u8-l3](u8-l3-http3-quic.md)）。

### 2.3 状态机复习

nginx 解析 HTTP/1 的请求行与头部用的就是「逐字符、可重入的状态机」（见 u6-l3）。HTTP/2 的帧解析沿用完全相同的设计哲学：每收到一段字节就推进状态机，记下当前位置，下一段字节到来时续接。你会在源码里反复看到「不够就 `ngx_http_v2_state_save` 保存、够了就分发」的模式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `src/http/v2/ngx_http_v2.h` | 所有核心结构体（h2c、stream、out_frame）与帧常量定义 |
| `src/http/v2/ngx_http_v2.c` | 连接级状态机：帧解析、HPACK 解码、流管理、流量控制、请求体读取 |
| `src/http/v2/ngx_http_v2_table.c` | HPACK 静态表与动态表（环形缓冲）实现 |
| `src/http/v2/ngx_http_v2_filter_module.c` | v2 过滤器：把 HTTP 核心响应编码成 HEADERS/DATA 帧 |
| `src/http/v2/ngx_http_v2_encode.c` | HPACK 编码侧（变长整数、Huffman） |
| `src/http/v2/ngx_http_v2_module.c` | 模块定义与 `http2` 等指令 |
| `src/http/modules/ngx_http_ssl_module.c` | TLS ALPN 注册 `h2` |

> 约定：本讲引用源码时用 `[路径:L行](永久链接)`，行号基于当前 HEAD `18ccebb1a889eb6989c64754f4f9b2512d58a491`。

## 4. 核心概念与源码讲解

本讲拆成五个最小模块，对应大纲要求的三个模块（主帧处理、过滤器、HPACK），并把流量控制单列，因为它横跨收发两端、是理解性能的关键。

### 4.1 HTTP/2 协议背景、连接建立与帧层

#### 4.1.1 概念说明

一条 HTTP/2 连接的诞生分两步：

1. **协议协商**：客户端在 TLS 握手时通过 ALPN（Application-Layer Protocol Negotiation）告诉服务器「我想用 `h2`」。明文 HTTP/2（h2c）则不发 ALPN，而是直接在 TCP 上发一段固定的「连接前言（connection preface）」。
2. **前言校验**：连接前言是一段魔法字符串：

   ```
   PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n
   ```

   它由 `NGX_HTTP_V2_PREFACE_START`（`"PRI * HTTP/2.0\r\n"`）和 `NGX_HTTP_V2_PREFACE_END`（`"\r\nSM\r\n\r\n"`）拼成，定义在：

   - [src/http/v2/ngx_http_v2.h:410-413](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L410-L413) — HTTP/2 连接前言常量。

#### 4.1.2 核心流程

连接建立的源码路径是 `ngx_http_request.c → ngx_http_v2_init`：

1. **ALPN 注册**：编译进 HTTP/2 模块后，nginx 在配置 SSL 时把 `h2` 加入 ALPN 协议列表，见 [src/http/modules/ngx_http_ssl_module.c:517-518](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L517-L518) — 服务器侧 ALPN 串里把 `"\x02h2"` 放在最前，TLS 握手期间客户端就能协商到 HTTP/2。
2. **前言探测**：无论是否经过 TLS，nginx 在读到一个连接的前几个字节时，会先比对它们是不是 HTTP/2 前言，见 [src/http/ngx_http_request.c:499-520](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L499-L520) — 若匹配 `NGX_HTTP_V2_PREFACE` 则调用 `ngx_http_v2_init(rev)` 把这条连接整个交给 HTTP/2 子系统，从此 HTTP/1 的请求行/头部解析路径对它「退场」。
3. **h2c 初始化**：`ngx_http_v2_init` 创建连接级上下文 `ngx_http_v2_connection_t`（简称 h2c），发送自己的 SETTINGS 帧，并主动发一个连接级 WINDOW_UPDATE 把接收窗口撑到最大。

帧头是固定 9 字节，定义在 [src/http/v2/ngx_http_v2.h:27](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L27)（`NGX_HTTP_V2_FRAME_HEADER_SIZE` = 9），布局如下：

```
+---------------------------------------------------+
|  Length (24 bit)  |Type(8)| Flags(8) |R| Stream ID (31) |
+---------------------------------------------------+
```

- **Length**：payload 字节数（不含 9 字节帧头本身）。
- **Type**：帧类型，取值见 [src/http/v2/ngx_http_v2.h:30-39](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L30-L39)，常用有 DATA(0)、HEADERS(1)、SETTINGS(4)、WINDOW_UPDATE(8)。
- **Flags**：标志位，见 [src/http/v2/ngx_http_v2.h:42-47](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L42-L47)，最关键的是 `END_STREAM`(0x01) 和 `END_HEADERS`(0x04)。
- **Stream ID**：31 bit，最高位保留。0 表示连接级帧（SETTINGS、PING、GOAWAY）。

#### 4.1.3 源码精读

`ngx_http_v2_init` 是整个 HTTP/2 连接的「开机」，我们逐段看 [src/http/v2/ngx_http_v2.c:203-294](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L203-L294)：

```c
h2c->send_window = NGX_HTTP_V2_DEFAULT_WINDOW;   // 65535
h2c->recv_window = NGX_HTTP_V2_MAX_WINDOW;       // 2^31-1
h2c->init_window = NGX_HTTP_V2_DEFAULT_WINDOW;
h2c->frame_size  = NGX_HTTP_V2_DEFAULT_FRAME_SIZE; // 16K
...
if (ngx_http_v2_send_settings(h2c) == NGX_ERROR) { ... }          // 发 SETTINGS
if (ngx_http_v2_send_window_update(h2c, 0,
        NGX_HTTP_V2_MAX_WINDOW - NGX_HTTP_V2_DEFAULT_WINDOW) ...) // 撑大接收窗口
h2c->state.handler = ngx_http_v2_state_preface;   // 状态机从 preface 开始
```

几个要点：

- nginx 一上来就把 **连接级接收窗口** 设到上限 `NGX_HTTP_V2_MAX_WINDOW`（`2^31-1`，见 [ngx_http_v2.h:49-50](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L49-L50)），并对端发一个 WINDOW_UPDATE 把它从默认的 65535 扩到最大——因为 nginx 不希望自己在「收」这个方向成为瓶颈。
- `h2c->state.handler` 是状态机的「当前要调的函数指针」，从 `state_preface` 起步，之后每解析完一帧就切换。

帧解析的「总入口」是 `ngx_http_v2_state_head`，见 [src/http/v2/ngx_http_v2.c:884-916](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L884-L916)：

```c
head = ngx_http_v2_parse_uint32(pos);
h2c->state.length = ngx_http_v2_parse_length(head);   // 高 24 位
h2c->state.flags  = pos[4];
h2c->state.sid    = ngx_http_v2_parse_sid(&pos[5]);    // 低 31 位
pos += NGX_HTTP_V2_FRAME_HEADER_SIZE;
type = ngx_http_v2_parse_type(head);
...
return ngx_http_v2_frame_states[type](h2c, pos, end);  // 按类型分发
```

分发用的函数指针表是核心设计，见 [src/http/v2/ngx_http_v2.c:186-197](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L186-L197)：

```c
static ngx_http_v2_handler_pt ngx_http_v2_frame_states[] = {
    ngx_http_v2_state_data,        /* DATA        0x0 */
    ngx_http_v2_state_headers,     /* HEADERS     0x1 */
    ngx_http_v2_state_priority,    /* PRIORITY    0x2 */
    ngx_http_v2_state_rst_stream,  /* RST_STREAM  0x3 */
    ngx_http_v2_state_settings,    /* SETTINGS    0x4 */
    ...
};
```

这种「用一个数组下标 = 帧类型，直接跳转到处理函数」的写法是 O(1) 分发，比 `switch` 更紧凑。若类型越界则记日志并 `state_skip` 跳过未知帧（前向兼容）。

整个读取由读事件 handler `ngx_http_v2_read_handler` 驱动，见 [src/http/v2/ngx_http_v2.c:334-455](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L334-L455)——它在一个 `do { ... } while(rev->ready)` 循环里反复 `recv`，把字节喂给 `h2c->state.handler`，直到缓冲消费完或返回 `NGX_AGAIN`。注意末尾有一行**洪水检测**：

```c
// src/http/v2/ngx_http_v2.c:434-438
if (h2c->total_bytes / 8 > h2c->payload_bytes + 1048576) {
    ngx_log_error(NGX_LOG_INFO, c->log, 0, "http2 flood detected");
    ngx_http_v2_finalize_connection(h2c, NGX_HTTP_V2_NO_ERROR);
    return;
}
```

如果收到的总字节数远超有效载荷（比如客户端疯狂发 PADDING），nginx 会判定为攻击并断开连接——这是 HTTP/2 共享 TCP 上防资源耗尽的关键保护。

#### 4.1.4 代码实践

**实践目标**：亲手让 nginx 走上 HTTP/2 路径，并验证 ALPN 协商。

**操作步骤**：

1. 确保按 u1-l2 编译时带 `--with-http_v2_module`（通常 `--with-http_ssl_module` 会一起带）。
2. 写一个最小配置（**示例配置**）：

   ```nginx
   server {
       listen 443 ssl;
       server_name localhost;
       ssl_certificate     cert.pem;
       ssl_certificate_key cert.key;
       http2 on;          # 新版指令（旧版写 listen ... http2）
   }
   ```

3. 启动 nginx，用 curl 协商：

   ```bash
   curl -vk --http2 https://localhost/ 2>&1 | grep -i "ALPN\|HTTP/2"
   ```

**需要观察的现象**：curl 输出里应出现 `ALPN: server accepted h2`，响应行是 `HTTP/2 200`。

**预期结果**：确认 TLS 握手期间 ALPN 选了 `h2`，且响应通过 HTTP/2 帧返回。若你看到 `HTTP/1.1`，多半是 `http2 on` 没生效或客户端不支持。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nginx 要在 `ngx_http_v2_init` 里主动发一个连接级 WINDOW_UPDATE？

> **答**：HTTP/2 默认的连接级流量控制窗口只有 65535 字节。nginx 把自己的 `recv_window` 直接初始化为上限 `2^31-1`，并通过 WINDOW_UPDATE 通知对端「我能收这么多」，否则在高吞吐下 nginx 侧窗口会成为吞吐瓶颈。

**练习 2**：`ngx_http_v2_frame_states[]` 用数组下标分发，而不是 `switch-case`，有什么好处和代价？

> **答**：好处是 O(1) 跳转、代码紧凑、新增帧类型只需加一行。代价是必须保证「数组下标」与「帧类型数值」严格一一对应（事实上协议规定帧类型从 0 连续编号，所以成立），且越界要单独判（`type >= NGX_HTTP_V2_FRAME_STATES`）。

---

### 4.2 多路复用流与 stream 生命周期

#### 4.2.1 概念说明

**stream（流）** 是 HTTP/2 的核心抽象：一条连接上并发存在的、由相同 Stream ID 标识的、双向有序的帧序列，承载一个「请求 → 响应」。规则：

- 客户端发起的流用**奇数** ID（1、3、5…），服务器推送用偶数。
- Stream ID **单调递增、不可复用**——一个 ID 用过就作废。
- ID 为 0 的「流」代表连接本身（SETTINGS、PING、GOAWAY 都走连接级）。

nginx 的关键设计技巧是 **fake connection（假连接）**：每条 HTTP/2 流都被包成一个看起来像 HTTP/1 连接的对象。这样 nginx 的 HTTP 核心（phases、过滤器链、请求体框架）几乎不用改，就能复用到 HTTP/2 上——核心代码依然以为自己在处理一条独立的、有自己 `ngx_connection_t` 的请求。

#### 4.2.2 核心流程

一条流的生命周期：

```
HEADERS帧 ──► state_headers ──► create_stream(建fake conn + request)
                                  │
                  解析HPACK头部块 ─┘
                                  │
                  state_header_complete ──► ngx_http_v2_run_request
                                  │             （进入标准HTTP phases）
            (若有请求体) DATA帧 ──► state_data ──► process_request_body
                                  │
              nginx 生成响应 ──► v2 filter ──► HEADERS帧 + DATA帧 回写
                                  │
                          END_STREAM 双向都到 ──► close_stream
```

收到 HEADERS 帧时，`state_headers` 做大量校验后再创建流，见 [src/http/v2/ngx_http_v2.c:1163-1376](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L1163-L1376)，关键校验：

- `sid % 2 == 0 || sid <= last_sid` → PROTOCOL_ERROR（必须是新奇数 ID）。
- `h2c->processing >= concurrent_streams` → 发 RST_STREAM 拒绝（`REFUSED_STREAM`），保护 nginx 不被过多并发流压垮。
- `depend == sid` → 自依赖，非法。

通过校验后调用 `ngx_http_v2_create_stream`，见 [src/http/v2/ngx_http_v2.c:2980-3070](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L2980-L3070)——它优先从 `h2c->free_fake_connections` 复用一个 fake connection（流结束后归还），否则新分配一个 `ngx_connection_t`，再调 `ngx_http_create_request(fc)` 建请求对象：

```c
// src/http/v2/ngx_http_v2.c:2991-2994  复用 fake connection
fc = h2c->free_fake_connections;
if (fc) {
    h2c->free_fake_connections = fc->data;
    ...
}
// src/http/v2/ngx_http_v2.c:3064-3066
r->http_protocol = "HTTP/2.0";
r->http_version = NGX_HTTP_VERSION_20;
```

注意 fake connection 的 `rev->handler` 被设成 `ngx_http_v2_close_stream_handler`——HTTP 核心代码以为自己在操作一条真连接的读写事件，但事件回调实际指向 stream 的关闭逻辑。

HPACK 头部解析完毕后，`state_header_complete` 调用 `ngx_http_v2_run_request` 把请求「交还」给标准 HTTP 框架（跑 phases），见 [src/http/v2/ngx_http_v2.c:1882-1920](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L1882-L1920)：

```c
if (stream) {
    ngx_http_v2_run_request(stream->request);   // 进入标准 HTTP 处理
}
```

若该请求带请求体（POST 等），后续 DATA 帧由 `state_data` 处理，见 [src/http/v2/ngx_http_v2.c:920-1056](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L920-L1056)——它先做流量控制检查（见 4.4），再调 `state_read_data` 把 payload 经 `ngx_http_v2_process_request_body` 喂给 HTTP 请求体框架（与 u6-l3 讲的 `ngx_http_read_client_request_body` 是同一套机制，只是数据来源从 socket 换成 HTTP/2 DATA 帧）。

#### 4.2.3 源码精读

连接级上下文 h2c 持有所有流的总状态，见 [src/http/v2/ngx_http_v2.h:124-171](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L124-L171)，关键字段：

- `processing`：当前活跃流数（用于并发上限）。
- `last_sid`：已见过的最大流 ID（保证单调）。
- `last_out`：待发送帧的输出链表（按优先级排序）。
- `streams_index`：流 ID → node 的哈希索引（O(1) 查找流）。

每条流对应 [src/http/v2/ngx_http_v2.h:188-225](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L188-L225) 的 `ngx_http_v2_stream_t`，关键字段：

- `request`：指向它持有的 HTTP 请求对象（fake connection 上的）。
- `send_window` / `recv_window`：流级流量控制窗口（signed，因为对端可能把窗口调成负）。
- `in_closed` / `out_closed`：请求方向 / 响应方向是否已收到/发出 END_STREAM。
- `preread`：请求体预读缓冲（HEADERS 帧后 DATA 帧可能紧跟着到达）。

请求体读取的入口是 [src/http/v2/ngx_http_v2.c:3948-4045](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L3948-L4045)——HTTP/2 里请求体不存在「Content-Length/chunked」的区别，因为它直接由一连串 DATA 帧组成，`END_STREAM` 标志即代表请求体结束。

#### 4.2.4 代码实践

**实践目标**：在真实浏览器里观察一条 HTTP/2 连接上的多路复用。

**操作步骤**：

1. 用上面的配置启动一个 HTTPS + HTTP/2 的 nginx，根目录放一个引用了多张图片的 `index.html`（**示例配置**）：

   ```html
   <img src="a.png"><img src="b.png"><img src="c.png"><img src="d.png">
   ```

2. 用 Chrome/Firefox 打开页面，按 F12 → Network → 勾选「Protocol」列。

**需要观察的现象**：所有图片请求的 Protocol 列都显示 `h2`，且它们的「Connection ID」或「Stream ID」不同但共用同一条 TCP/TLS 连接（可在 Waterfall 里看到它们并发下载而非排队）。

**预期结果**：多条流的请求与响应在同一连接上交错传输，体现多路复用。结合源码，你可以解释：每张图片的 GET 对应一个奇数 stream ID，nginx 在 `state_headers` 里为每条流各建一个 fake connection 和 `ngx_http_request_t`，再并发处理。

#### 4.2.5 小练习与答案

**练习 1**：为什么 nginx 要为每条 HTTP/2 流创建一个 fake connection，而不是直接复用真实连接？

> **答**：HTTP 核心框架（phases、过滤器、请求体、`ngx_http_finalize_request` 等）都围绕 `ngx_connection_t` 和 `ngx_http_request_t` 设计，假设「一个连接对应一个请求」。HTTP/2 一条连接上有 N 个并发请求，fake connection 让每个请求都拥有独立的 `ngx_connection_t`（独立的读写事件、独立的 sent 计数），从而 HTTP 核心代码零改动复用，只在 v2 过滤器层做「多流 → 一连接」的汇聚。

**练习 2**：`state_headers` 在什么情况下会拒绝新建流（发 RST_STREAM）？

> **答**：三种情况会以 `REFUSED_STREAM` 拒绝（见源码 1284-1310 行）：(1) 活跃流数已达 `concurrent_streams` 上限；(2) 单轮新流数超过 `2 * concurrent_streams`（防突发洪水）；(3) SETTINGS ACK 还没收到且流带请求体、`preread_size` 不足（防客户端在协商完成前塞数据）。

---

### 4.3 HPACK 头部压缩：静态表、动态表与变长整数

#### 4.3.1 概念说明

HPACK 用三件套压缩头部，目标是把重复的文本头部（如 `:method: GET`、`user-agent: ...`）压到几个字节：

1. **静态表（Static Table）**：协议预定义的 61 个常见头部，见 [src/http/v2/ngx_http_v2_table.c:20-82](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L20-L82)。比如索引 2 就是 `:method: GET`，引用时只需发 1 字节（`0x82`）。
2. **动态表（Dynamic Table）**：连接级、先进先出。本次请求新出现的字面头部可加入表，后续请求就能用索引引用。nginx 把它实现在一块固定 4096 字节的环形缓冲上。
3. **Huffman 编码**：对头部名/值的字符串再做 Huffman 压缩。

一个 HPACK 头部块是若干「头部表示」的序列，每项由首字节的高位区分类型（见 `state_header_block` 的分流）：

| 首字节模式 | 含义 |
|-----------|------|
| `1xxxxxxx`（prefix 7 位） | Indexed Header Field：完整头部直接来自静态/动态表 |
| `01xxxxxx`（prefix 6 位） | Literal with Incremental Indexing：字面量，且加入动态表 |
| `001xxxxx`（prefix 5 位） | Dynamic Table Size Update：调整动态表大小 |
| `0000/0001xxxx`（prefix 4 位） | Literal without/never Indexing |

其中整数使用「前置位 + 变长续字节」编码。设 prefix 有 N 位，\( m = 2^N - 1 \)，解码规则：

\[
v = (b_0 \mathbin{\&} m); \quad \text{若 } v < m \text{ 则结束；否则 } v = m + \sum_{i\ge 1} (b_i \mathbin{\&} 0x7f) \ll (7(i-1))
\]

每个续字节贡献低 7 位，最高位是「续位」（1 表示还有后续）。nginx 为防恶意大整数，把续字节上限锁死在 `NGX_HTTP_V2_INT_OCTETS = 4`（见 [ngx_http_v2.h:23](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L23)）。

#### 4.3.2 核心流程

解码一条头部表示的状态机（位于 `state_header_block` 与 `state_field_*` 系列）：

```
state_header_block ── 看首字节高位 ──► 决定类型 + parse_int(prefix)
        │
        ├─ indexed? ──► get_indexed_header(value) ──► process_header
        ├─ size_update? ──► table_size(value) ──► (调表后继续)
        └─ literal? ──► (name 可能索引) ──► state_field_len
                                              │
                              ┌───────────────┴───────────────┐
                       Huffman?                          raw
                       state_field_huff               state_field_raw
                              └───────────────┬───────────────┘
                                         process_header
```

#### 4.3.3 源码精读

**类型分流** 在 [src/http/v2/ngx_http_v2.c:1380-1476](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L1380-L1476)：

```c
ch = *pos;
if (ch >= (1 << 7)) {            /* indexed header field */
    indexed = 1; prefix = ngx_http_v2_prefix(7);
} else if (ch >= (1 << 6)) {     /* literal with incremental indexing */
    h2c->state.index = 1; prefix = ngx_http_v2_prefix(6);
} else if (ch >= (1 << 5)) {     /* dynamic table size update */
    size_update = 1; prefix = ngx_http_v2_prefix(5);
} else {                         /* literal (never/without) */
    prefix = ngx_http_v2_prefix(4);
}
value = ngx_http_v2_parse_int(h2c, &pos, end, prefix);
```

**变长整数解析** 见 [src/http/v2/ngx_http_v2.c:2654-2706](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L2654-L2706)，核心就是上面那个公式：

```c
value = *p++ & prefix;                 // 先取 prefix 位
if (value != prefix) { return value; } // 没占满 prefix，就是最终值
for (shift = 0; p != end; shift += 7) {
    octet = *p++;
    value += (octet & 0x7f) << shift;  // 续字节低 7 位
    if (octet < 128) { return value; } // 最高位为 0 表示结束
}
```

返回值用负数表达「还没读完」（`NGX_AGAIN`/`NGX_DECLINED`），状态机据此决定是保存现场等更多字节、还是报压缩错误。

**字符串值解码**（Huffman 或 raw）见 [src/http/v2/ngx_http_v2.c:1480-1563](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L1480-L1563)：先读 1 位 Huffman 标志 + 7 位长度，再按标志走 `state_field_huff`（调 `ngx_http_huff_decode`）或 `state_field_raw`（原样拷贝）。

**动态表的环形缓冲** 是 HPACK 实现最精巧的部分，见 [src/http/v2/ngx_http_v2_table.c:188-299](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L188-L299)（`ngx_http_v2_add_header`）和取值 [src/http/v2/ngx_http_v2_table.c:103-185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L103-L185)（`ngx_http_v2_get_indexed_header`）。设计要点：

- `storage` 是固定 4096 字节缓冲（`NGX_HTTP_V2_TABLE_SIZE`，见 [ngx_http_v2_table.c:13](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L13)），写入到尾后绕回头部，形成环。
- `entries` 是指针数组，用 `added/deleted/reused % allocated` 做循环下标复用，避免数组搬移。
- 取值时若 name/value 跨越了缓冲尾部，要分两段拷贝（见 142-150 行的 `rest` 分支）。
- 表大小按 RFC 算法记费：每条头部占 `name.len + value.len + 32` 字节（32 是开销估计），见 [ngx_http_v2_table.c:302-332](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L302-L332) 的 `ngx_http_v2_table_account`——超限时从队首 evict 老条目。

**编码侧**（写响应头时）在 `ngx_http_v2_encode.c`，见 [src/http/v2/ngx_http_v2_encode.c:17-40](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_encode.c#L17-L40)：先尝试 Huffman（`ngx_http_huff_encode`），变短就用，否则原样存。变长整数的编码 `ngx_http_v2_write_int` 在 [src/http/v2/ngx_http_v2_encode.c:43-62](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_encode.c#L43-L62)，是解码的逆过程。

#### 4.3.4 代码实践

**实践目标**：直观看到 HPACK 把请求头压成了几个字节。

**操作步骤**：

1. 用 nghttp 或 curl 发一个 HTTP/2 请求并打印发送的原始字节：

   ```bash
   nghttp -v https://localhost/ 2>&1 | head -40
   ```

   或没有 nghttp 时：

   ```bash
   curl --http2 -v -o /dev/null https://localhost/ 2>&1 | grep -A2 "^> "
   ```

**需要观察的现象**：第一次请求会看到类似 `[HEADERS]` 块带着少量字节的「魔法」数据（如 `82 86 84 41 8a ...`），而不是文本的 `:method: GET`。其中 `82` 就是静态表索引 2（`:method: GET`）。

**预期结果**：对比同样请求用 HTTP/1.1 发送时头部有几百字节的明文，HTTP/2 的 HEADERS 块通常只有几十字节。结合源码，你能解释 `82` 是怎么来的：`0x82 = 0x80(indexed 标志) | 2(静态表索引)`，`ngx_http_v2_indexed(2)` 宏展开即此值（见 [ngx_http_v2.h:370](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L370)）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 nginx 把动态表实现成「固定 4096 字节环形缓冲 + 指针数组」，而不是用 `ngx_array_t` 或 `ngx_pool_t`？

> **答**：动态表是「先进先出」且总大小受 SETTINGS_HEADER_TABLE_SIZE 限制的有界结构，环形缓冲天然适合——写入绕回、evict 只需移动 `deleted` 指针，无需搬移数据；指针数组用模运算循环复用槽位，避免反复 `malloc/free`。这比线性数组扩容 + 数据搬移高效得多，且天然适配 4096 字节上限。

**练习 2**：变长整数里，如果 prefix 位全 1（值 == prefix）但后续没有任何续字节，nginx 怎么处理？

> **答**：见 `parse_int` 在循环结束后判断：若 `end - start >= state.length` 返回 `NGX_ERROR`（长度耗尽仍没收齐，属非法），若已读到 4 字节上限返回 `NGX_DECLINED`（整数过大，按压缩错误拒绝），否则返回 `NGX_AGAIN` 表示「需要更多字节，保存现场下次续读」。

---

### 4.4 流量控制：连接级与流级双层窗口

#### 4.4.1 概念说明

HTTP/2 的流量控制是一套「信用制滑动窗口」，思路与 TCP 类似，但分层：

- **连接级窗口**：限制这条连接上**所有 DATA 帧**的未确认字节数。
- **流级窗口**：限制**单条流**上 DATA 帧的字节数。

每发一个 DATA 帧，对应层窗口减去 size；收到 WINDOW_UPDATE 帧则加上 increment。窗口归零时发送方暂停，等对端「给额度」。窗口上限是：

\[
W_{\max} = 2^{31} - 1 = 2147483647
\]

定义在 [ngx_http_v2.h:49](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L49)（`NGX_HTTP_V2_MAX_WINDOW`）。双层窗口的意义：连接级防总流量过载，流级防某条流独占带宽。

#### 4.4.2 核心流程

**接收侧（nginx 作为接收方，读客户端的 DATA 帧）**，在 `state_data`，见 [src/http/v2/ngx_http_v2.c:968-1035](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L968-L1035)：

```c
if (size > h2c->recv_window) {            // 连接级窗口检查
    return connection_error(FLOW_CTRL_ERROR);
}
h2c->recv_window -= size;
if (h2c->recv_window < MAX_WINDOW / 4) {  // 低于 1/4 就补满
    send_window_update(h2c, 0, MAX_WINDOW - h2c->recv_window);
    h2c->recv_window = MAX_WINDOW;
}
...
if (size > stream->recv_window) {         // 流级窗口检查
    terminate_stream(FLOW_CTRL_ERROR);
}
stream->recv_window -= size;
```

nginx 用「低于上限 1/4 就补满」的策略，避免每个 DATA 帧都回一个 WINDOW_UPDATE。

**发送侧（nginx 往外发响应 DATA 帧）**，核心在 v2 过滤器的 `send_chain` 和 `flow_control`，见 [src/http/v2/ngx_http_v2_filter_module.c:1408-1427](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1408-L1427)：

```c
if (stream->send_window <= 0) {
    stream->exhausted = 1;       // 流级窗口耗尽
    return NGX_DECLINED;
}
if (h2c->send_window == 0) {
    ngx_http_v2_waiting_queue(h2c, stream);  // 连接级窗口耗尽，进等待队列
    return NGX_DECLINED;
}
```

切 DATA 帧时同时扣减两个窗口，见 [src/http/v2/ngx_http_v2_filter_module.c:1144-1150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1144-L1150) 与 [1241-1243](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1241-L1243)：

```c
if (limit == 0 || limit > h2c->send_window)  limit = h2c->send_window;
if (limit > stream->send_window)             limit = stream->send_window;  // 取较小
...
h2c->send_window    -= frame_size;
stream->send_window -= frame_size;
```

**收到客户端的 WINDOW_UPDATE** 时唤醒被阻塞的流，见 [src/http/v2/ngx_http_v2.c:2396-2482](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L2396-L2482)：

```c
stream->send_window += window;
if (stream->exhausted) {
    stream->exhausted = 0;
    wev = stream->request->connection->write;
    wev->active = 0; wev->ready = 1;
    if (!wev->delayed) { wev->handler(wev); }   // 重新触发该流的写
}
```

注意 `window == 0` 是协议错误（白给信用），窗口加到溢出（超过 `MAX_WINDOW`）也是错误。

#### 4.4.3 源码精读

窗口字段定义在两个结构体里：h2c 的 `send_window`/`recv_window`/`init_window` 在 [ngx_http_v2.h:138-140](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L138-L140)，stream 的 `send_window`(signed)/`recv_window` 在 [ngx_http_v2.h:199-200](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L199-L200)。注释特别指出 `send_window` 用 `ssize_t`（有符号），因为对端用 SETTINGS_INITIAL_WINDOW_SIZE 调整初始窗口时，可能把在途流的窗口「调负」，见 `state_settings_params` 对 `INIT_WINDOW_SIZE_SETTING` 的处理（[ngx_http_v2.c:2214-2226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L2214-L2226)）和 `ngx_http_v2_adjust_windows` 的差量调整。

连接级窗口耗尽时，`waiting_queue` 把流按优先级（rank/rel_weight）插入等待队列，见 [src/http/v2/ngx_http_v2_filter_module.c:1430-1458](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1430-L1458)，等连接级窗口恢复后按优先级唤醒——高优先级流的帧先发。

#### 4.4.4 代码实践

**实践目标**：观察「窗口耗尽 → 流暂停 → 窗口恢复 → 流恢复」的完整周期。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx（见 u10-l5），配置 `error_log /tmp/h2.log debug_http;`。
2. 下载一个大静态文件（远大于客户端接收窗口），用 curl 慢速拉取：

   ```bash
   curl --http2 --limit-rate 10k -o /dev/null https://localhost/bigfile.bin &
   ```

3. 实时看日志：

   ```bash
   tail -f /tmp/h2.log | grep -E "windows|WINDOW_UPDATE|exhausted"
   ```

**需要观察的现象**：日志里反复出现形如 `http2:N windows: conn:X stream:Y` 的行，连接级/流级 `send_window` 从大数递减到接近 0，然后出现 `WINDOW_UPDATE` 后又跳回。

**预期结果**：由于客户端慢速读取，它的接收窗口很快填满，nginx 的 `send_window` 被压到 0；流被挂入 `waiting` 队列；客户端消费数据后回 WINDOW_UPDATE，nginx 唤醒流继续发。**待本地验证**：确切的窗口数值取决于网络与客户端实现，重点是看到「窗口增减」与「等待/唤醒」的对应关系。

#### 4.4.5 小练习与答案

**练习 1**：为什么 nginx 的流级 `send_window` 用有符号 `ssize_t`？

> **答**：对端可通过 SETTINGS 帧的 `INITIAL_WINDOW_SIZE` 动态调整所有「活跃流」的初始窗口。这个调整是对「从新初始窗口中扣除已发送量」的重新计算，结果可能为负（流已发送量超过新初始窗口）。用有符号类型能正确表达这种负窗口，发送逻辑里 `send_window <= 0` 即暂停发送，等后续 WINDOW_UPDATE 把它补回正。

**练习 2**：连接级窗口和流级窗口哪个先成为瓶颈？nginx 如何处理？

> **答**：取决于场景。流级窗口归零时，仅该流暂停（`exhausted=1`），其他流不受影响；连接级窗口归零时，**所有**流都不能发 DATA，nginx 把它们按优先级挂入 `h2c->waiting` 队列，等连接级 WINDOW_UPDATE 到来后按优先级依次唤醒。两者都返回 `NGX_DECLINED` 让 `send_chain` 暂停并返回未发完的 chain。

---

### 4.5 v2 过滤器：响应输出与 HTTP 核心对接

#### 4.5.1 概念说明

v2 过滤器（`ngx_http_v2_filter_module`）是 HTTP 核心 filter chain 在 HTTP/2 连接上的**适配器**。它实现两个钩子：

- **header_filter**：把 `ngx_http_request_t::headers_out` 编码成一个 HEADERS 帧。
- **send_chain**（替代 `write_filter` 的出口）：把响应体切成多个 DATA 帧。

它通过 `postconfiguration` 把自身 push 进 `ngx_http_top_header_filter` 链。但只有 `r->stream` 非空（即确实是 HTTP/2 请求）时才接管，否则透传给 next filter——因此同一个 nginx 可同时服务 HTTP/1 与 HTTP/2。

这套设计与 u6-l6 讲的 header/body filter 链完全一致，只是过滤器把「写 socket」换成了「构造 HTTP/2 帧并入队」。

#### 4.5.2 核心流程

**响应头编码**（`ngx_http_v2_header_filter`），见 [src/http/v2/ngx_http_v2_filter_module.c:106-640](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L106-L640)：

1. 状态码用静态表索引压缩。常见状态码 1 字节搞定，见状态码分支 [166-210](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L166-L210)：

   ```c
   case NGX_HTTP_OK:
       status = ngx_http_v2_indexed(NGX_HTTP_V2_STATUS_200_INDEX);  // 0x84，1 字节
       break;
   ```

   非常见状态码用 `inc_indexed`（引用 `:status` 名字 + 字面值），见 [436-443](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L436-L443)。

2. `server`、`date`、`content-type`、`content-length`、`last-modified`、`location` 等都用 `inc_indexed(索引) + Huffman 值` 编码，见 [445-585](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L445-L585)。注意 `server: nginx` 这种固定串被预先 Huffman 编码成常量数组 `nginx[5]`（[123 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L123)），直接拷贝省去运行时编码。

3. 其余自定义头部用 `literal without indexing`（`*pos++ = 0`）+ name/value 各自 Huffman 编码。

4. 编码好的字节块交给 `create_headers_frame` 包帧头。若字节块超过 `frame_size`，自动拆成「HEADERS + 若干 CONTINUATION」，见 [src/http/v2/ngx_http_v2_filter_module.c:842-945](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L842-L945)，只有最后一帧带 `END_HEADERS` 标志。

**响应体切片**（`send_chain`），见 [src/http/v2/ngx_http_v2_filter_module.c:1062-1284](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1062-L1284)：

1. 先做流量控制检查（4.4），窗口允许才切帧。
2. 把上游传来的 chain 按 `chunk_size`（`http2_chunk_size` 指令）和窗口取较小值，切成多个 DATA 帧，每帧用 `ngx_http_v2_filter_get_data_frame` 包帧头。
3. 最后一个带 `last_buf` 的 buf 所在帧带 `END_STREAM`，见 [src/http/v2/ngx_http_v2_filter_module.c:1354](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1354)。
4. 每帧入队时调 `ngx_http_v2_queue_frame`，按流优先级插入（见下）。

**帧的优先级调度**：`ngx_http_v2_queue_frame` 不是简单尾插，而是按 `node->rank` 和 `rel_weight` 把帧插入合适位置，见 [src/http/v2/ngx_http_v2.h:243-266](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.h#L243-L266)：

```c
for (out = &h2c->last_out; *out; out = &(*out)->next) {
    if ((*out)->blocked || (*out)->stream == NULL) break;   // blocked/控制帧优先
    if ((*out)->stream->node->rank < frame->stream->node->rank
        || (rank 相等 && rel_weight >= ...))
        break;
}
frame->next = *out; *out = frame;
```

高优先级流的 DATA 帧排在低优先级之前；但 `blocked` 帧（如 HEADERS）总是先发，保证响应头先于响应体。

**实际写 socket** 在 `ngx_http_v2_send_output_queue`，见 [src/http/v2/ngx_http_v2.c:508-620](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L508-L620)：它把 `h2c->last_out` 链表上所有帧的 chain 串成一条大 chain，调 `c->send_chain` 一次写出，再逐个调 `frame->handler`（`headers_frame_handler`/`data_frame_handler`）回收已发完的帧、更新 `out_closed` 标志，见 [src/http/v2/ngx_http_v2_filter_module.c:1646-1669](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1646-L1669) 的 `handle_frame`。

#### 4.5.3 源码精读

过滤器注册（把自己挂进 filter 链）在 [src/http/v2/ngx_http_v2_filter_module.c:1774-1784](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1774-L1784) 的 `ngx_http_v2_filter_init`——这正是 u6-l6 讲的「`next = top; top = self`」两行注册法。

`header_filter` 开头有一个关键早退，体现了「适配器」身份，见 [src/http/v2/ngx_http_v2_filter_module.c:137-141](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L137-L141)：

```c
stream = r->stream;
if (!stream) {
    return ngx_http_next_header_filter(r);  // 非 HTTP/2，交给下一个过滤器
}
```

DATA 帧构造见 [src/http/v2/ngx_http_v2_filter_module.c:1320-1405](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1320-L1405) 的 `ngx_http_v2_filter_get_data_frame`，它还做了一层**洪水保护**：单个连接的 frame 结构体数超过 10000 即判攻击（[1336-1352 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L1336-L1352)）。

#### 4.5.4 代码实践

**实践目标**：通过修改 `server_tokens` 观察响应头编码长度的变化，体会 HPACK + Huffman 的压缩效果。

**操作步骤**：

1. 用 nghttp 抓取响应头原始字节（nghttp 会解码显示）：

   ```bash
   # 第一次：server_tokens on（默认）
   nghttp -v -H 'accept-encoding: gzip' https://localhost/ 2>&1 | grep -iE "HEADERS|server"
   ```

2. 在 nginx.conf 的 `http` 块加 `server_tokens off;`，`nginx -s reload` 后再抓一次。

**需要观察的现象**：`server_tokens on` 时 `server` 头是完整的 nginx 版本号（如 `nginx/1.x.y`）；`off` 时是裸的 `nginx`。结合源码，后者直接拷贝预编码好的 5 字节常量 `nginx[5]`（[123 行](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L123)），前者则要 Huffman 编码版本字符串。

**预期结果**：响应 HEADERS 块字节数在 `server_tokens off` 下更小。**待本地验证**：具体字节数取决于你的版本号长度，但方向应一致。

#### 4.5.5 小练习与答案

**练习 1**：为什么 HEADERS 帧入队用 `queue_blocked_frame`（设 `blocked=1`），而 DATA 帧用 `queue_frame`？

> **答**：HTTP/2 要求「响应头必须先于响应体到达」。HEADERS 帧设 `blocked=1` 后，`queue_frame` 的优先级排序逻辑遇到 blocked 帧就停止扫描、把新 blocked 帧插在它之前，从而保证所有控制/头部帧在 DATA 帧之前发送。DATA 帧才参与按 rank/rel_weight 的优先级排序。

**练习 2**：同一个 nginx 进程同时处理 HTTP/1 和 HTTP/2 请求，v2 过滤器如何避免干扰 HTTP/1 的响应？

> **答**：`header_filter` 一开始就判断 `if (!stream) return ngx_http_next_header_filter(r);`——只有 HTTP/2 请求才有 `r->stream`，HTTP/1 请求直接透传给下一个过滤器。同理 `send_chain` 也是按 `r->stream` 分流。这是一种「按请求协议自适应」的过滤器，无需为两种协议跑两套 nginx。

---

## 5. 综合实践

把本讲的知识串起来，做一个「HTTP/2 请求全链路追踪」任务。

**任务**：用一个带请求体的 POST 请求，结合 debug 日志，画出从客户端字节进入到响应字节离开的完整源码路径。

**步骤**：

1. `--with-debug` 编译 nginx，配置 `error_log logs/debug.log debug_http;`，配置一个接受 POST 的 location（可 proxy_pass 到后端，或用内置模块）。
2. 用 curl 发一个带小 JSON 体的 POST：

   ```bash
   curl --http2 -X POST -d '{"hello":"world"}' -v https://localhost/api 2>&1 | tee /tmp/curl.log
   ```

3. 在 `logs/debug.log` 里按顺序定位以下关键行（关键词见括号）：

   | 阶段 | 日志关键词 | 对应源码位置 |
   |------|-----------|-------------|
   | 连接前言校验 | `http2 preface verified` | [ngx_http_v2.c:877-878](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L877-L878) |
   | HEADERS 帧解析 | `http2 HEADERS frame sid:` | [ngx_http_v2.c:1247](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L1247) |
   | HPACK 取索引头部 | `http2 get indexed` | [ngx_http_v2_table.c:117](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_table.c#L117) |
   | DATA 帧收请求体 | `http2 DATA frame` | [ngx_http_v2.c:958](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L958) |
   | 生成响应头 | `http2 output header: ":status:` | [ngx_http_v2_filter_module.c:432](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2_filter_module.c#L432) |
   | 帧发出 | `http2 frame out:` | [ngx_http_v2.c:540](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/v2/ngx_http_v2.c#L540) |

4. 画出调用链：`state_preface → state_head → state_headers → state_header_block → state_field_* → process_header → header_complete → run_request →（HTTP phases）→ v2 header_filter → create_headers_frame → send_chain → filter_get_data_frame → send_output_queue → c->send_chain`。

**预期结果**：你能用一条完整的源码链路解释「POST 一个 JSON 到 HTTPS+HTTP/2 的 nginx，数据是怎么进出主机的」，并能指出每个阶段在哪个文件、哪一行。这条链路覆盖了本讲全部五个最小模块。

## 6. 本讲小结

- HTTP/2 通过 **二进制分帧 + 多路复用** 在一条 TCP 连接上并发多条流，nginx 用一张函数指针表 `ngx_http_v2_frame_states[]` 按 9 字节帧头的类型做 O(1) 分发。
- nginx 为每条 HTTP/2 流创建一个 **fake connection**，让标准 HTTP 框架（phases、过滤器、请求体）零改动复用，只在 v2 过滤器层做「多流 → 一连接」的汇聚。
- **HPACK** 用静态表（61 项预定义）+ 动态表（4096 字节环形缓冲）+ Huffman 压缩头部；变长整数限制在 4 个续字节内防恶意大整数；首字节高位决定头部表示类型。
- **流量控制** 分连接级和流级双层窗口，接收侧「低于 1/4 上限即补满」减少 WINDOW_UPDATE 频率，发送侧窗口耗尽则按优先级挂入 `waiting` 队列，收到 WINDOW_UPDATE 后唤醒。
- **v2 过滤器** 是 HTTP 核心 filter chain 的适配器：`header_filter` 把 `headers_out` 编码成 HEADERS 帧（状态码用静态表索引压到 1 字节），`send_chain` 把响应体切成 DATA 帧并按优先级 `queue_frame` 调度。
- nginx 在收发两侧都有 **洪水保护**：收侧检查「总字节/8 > 有效载荷+1M」、发侧限制单连接 frame 结构体数 < 10000，防共享 TCP 上资源耗尽。

## 7. 下一步学习建议

- **继续阅读 [u8-l3 HTTP/3 与 QUIC](u8-l3-http3-quic.md)**：HTTP/2 留下的 TCP 层队头阻塞由 QUIC 解决。对比 `src/http/v2/` 与 `src/http/v3/`、`src/event/quic/`，你会发现 HTTP/3 复用了本讲的 stream/HPACK/过滤器思路，但传输层从 epoll+TCP 换成了用户态 QUIC 栈。
- **回顾 [u7-l1 upstream 框架](u7-l1-upstream-framework.md)**：HTTP/2 的响应输出（HEADERS/DATA 帧 + 双层窗口）与 upstream 的 event_pipe 缓冲有相似的「背压」思想，可对照阅读。
- **动手实验**：参考 [u10-l4 编写自定义模块](u10-l4-write-custom-module.md)，写一个挂在 HTTP/2 请求上的 content handler，观察 `r->stream`、`r->connection`（fake connection）与真实连接的关系，加深对「fake connection」设计的理解。
- **深入源码**：若对优先级调度感兴趣，可精读 `ngx_http_v2_set_dependency` / `ngx_http_v2_node_children_update`（`ngx_http_v2.c` 中依赖树维护），理解 rank/rel_weight 是如何从 PRIORITY 帧构建出来的。
