# 请求行、头部与请求体解析

## 1. 本讲目标

上一讲（u6-l2）我们走通了 HTTP 请求的「生命周期」：连接建立 → 创建请求 → finalize 收尾，但把解析器和请求体读取当成了黑盒。本讲打开这个黑盒。读完本讲，你应当能够：

- 看懂 nginx 用**逐字符状态机**解析请求行（`GET /a/b HTTP/1.1`）的全部过程；
- 看懂头部行的解析，以及解析完后如何用哈希表把已知名头部（如 `Content-Length`）派发给对应处理函数；
- 理解一个请求体是**如何被读进来、分块拼接、必要时溢写到临时文件**的，重点掌握 `chunked` 请求体的处理；
- 在源码中独立追踪「一个 chunked POST 请求」从字节进入到落盘/交付处理函数的完整路径。

本讲结合真实源码，引用三个核心文件：`src/http/ngx_http_parse.c`（状态机解析）、`src/http/ngx_http_request.c`（解析的驱动与头部派发）、`src/http/ngx_http_request_body.c`（请求体读取）。

## 2. 前置知识

在进入源码前，先建立三个直觉。它们贯穿整篇讲义。

### 2.1 为什么是「逐字符状态机」而不是「整行扫描」

HTTP 报文是面向**流**的，一次 `recv` 可能只读到半个请求行、半个头部，也可能把请求行、若干头部、甚至请求体开头一次性全读进来。于是解析器必须满足两个条件：

1. **可中断、可续跑**：解析到一半缓冲区用完了，要把「当前解析到哪个状态」记下来，等下次数据来了接着解析。
2. **不复制数据**：尽量只在原缓冲区里移动指针 `pos`，把要保留的字段记成「指向缓冲区里某段区间」的指针，而不是拷贝一份。

nginx 的做法是把解析逻辑写成一个**有限状态机**：每个字符 `ch = *p` 喂给 `switch(state)`，根据当前状态和字符决定动作与下一个状态。状态保存在请求结构体 `r->state` 里（请求行/头部解析共用这一个字段，二者不会同时进行）。缓冲区用完后返回 `NGX_AGAIN`（表示「我还没解析完，再给我数据」），下次调用时从 `r->state` 续上。

> 术语提示：`NGX_AGAIN` 是本讲的「常客」，它永远表示「当前这一段没做完，等下一次」。在 u2-l4 里它代表背压信号，含义一脉相承。

### 2.2 头部是「边解析边派发」的

请求行解析完毕后，nginx 不先把所有头部收集成一个列表再处理，而是**每解析完一个头部行就立即处理它**：先压进通用列表 `r->headers_in.headers`，再用一张静态哈希表 `headers_in_hash` 查这个头是不是「已知名头」（Host、Content-Length、Connection 等）。如果是，立刻调用它专属的处理函数（如把 `Content-Length` 的数值解析出来存进 `content_length_n`）。

这张「名头表」本质上和 u3-l1 里讲的 `ngx_command_t` 指令表是同一个套路：用名字哈希快速匹配，匹配上后调一个回写函数，用 `offsetof` 把值写到 `headers_in` 结构体的固定字段。

### 2.3 请求体有「三层缓冲 + 一个过滤器链」

请求体的处理比请求行/头部复杂，因为它可能很大。nginx 用一条**过滤器链**（filter chain）把数据层层加工：

```
recv() 读到 socket 数据
   │
   ▼
request_body_filter（分发器：chunked 还是 content-length？）
   │
   ▼
chunked_filter / length_filter（剥掉传输编码，得到「裸体数据」）
   │
   ▼
top_request_body_filter = save_filter（把裸体数据挂到 rb->bufs 链表）
   │   内存缓冲放不下时
   ▼
write_request_body（写进临时文件）
```

请求体最终只会出现在两个地方之一（或两者都有）：内存里的 `ngx_chain_t` 链表 `rb->bufs`，或者磁盘上的临时文件 `rb->temp_file`。下游模块（如 `proxy`、`fastcgi`）通过 `r->request_body` 拿到它。

有了这三个直觉，我们开始逐个最小模块精读。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/http/ngx_http_parse.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c) | 所有「纯解析」状态机：请求行、头部行、URI、**chunked**。不涉及 I/O，只看缓冲区。 |
| [src/http/ngx_http_request.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c) | 解析的**驱动器**：什么时候调解析器、缓冲区不够怎么扩、头部解析完怎么派发、`Content-Length`/`Transfer-Encoding` 怎么归一化。 |
| [src/http/ngx_http_request_body.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c) | 请求体的**读取与缓存**：入口函数、主读循环、chunked/length 过滤器、临时文件写入。 |
| src/http/ngx_http.h | `ngx_http_chunked_t` 解析上下文结构。 |
| src/http/ngx_http_request.h | `ngx_http_request_body_t` 请求体结构、`ngx_http_request_t` 中的 `header_in` 缓冲等字段。 |

## 4. 核心概念与源码讲解

### 4.1 模块一：请求行解析状态机 `ngx_http_parse_request_line`

#### 4.1.1 概念说明

请求行是一行这样的文本：

```
GET /path/to/resource?x=1 HTTP/1.1\r\n
```

它由三段组成：方法（`GET`）、请求目标 URI（`/path?x=1`，也可能带 `http://host` 绝对形式）、HTTP 版本（`HTTP/1.1`）。状态机的任务就是把这些字符正确切分，并把结果以「指针区间」的形式记进请求结构体（如 `r->request_start`、`r->uri_start`、`r->http_major`）。

为什么用状态机而不是 `sscanf` 之类？因为请求行里有大量合法变体：URI 可能是绝对路径、可能是绝对 URI（含 `http://host`）、可能是 `CONNECT host:port`；HTTP/0.9 甚至没有版本字段。状态机能用清晰的状态转移把这些分支表达出来，且天然支持「数据没读全」时的中断续跑。

#### 4.1.2 核心流程

请求行解析的状态转移大致如下（简化版）：

```
sw_start ──首字符是字母──> sw_method
                              │（遇到空格）
                              ▼
                       sw_spaces_before_uri
                              │（看到 '/'）           │（看到字母，可能是 schema http）
                              ▼                        ▼
                       sw_after_slash_in_uri      sw_schema → sw_schema_slash → ... → sw_host
                              │
                              ▼
                       sw_check_uri / sw_uri
                              │（遇到空格，URI 结束）
                              ▼
                       sw_http_09 ──'H'──> sw_http_H → HT → HTT → HTTP → '/' → sw_first_major_digit
                              │（CR/LF 表示 HTTP/0.9）
                              ▼
                       sw_major_digit ──'.'──> sw_first_minor_digit → sw_minor_digit
                              │（CR/LF）
                              ▼
                          解析完成（done）
```

方法的识别是一个巧妙优化：状态机在遇到空格时已经知道方法的长度（`p - m`），于是用一个 `switch(长度)` + 少量字符比较就能判定是 GET/POST/PUT/HEAD/PATCH 等等，避免逐个字符串比较。

#### 4.1.3 源码精读

**状态枚举**。整个状态机的全部状态集中在一个 `enum` 里，函数开头把它读进局部变量 `state`（每次 `for` 循环只读一次 `r->state`，结尾再回写，这是 C 里常见的性能习惯）：

[ngx_http_parse.c:108-139](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L108-L139) —— `ngx_http_parse_request_line` 的签名与状态枚举。注意从 `sw_start` 到 `sw_almost_done`，覆盖了方法、schema、host、URI、HTTP 版本号所有阶段。

**逐字符主循环**。这是所有 nginx 解析器的统一骨架：`for (p = b->pos; p < b->last; p++)` 逐字节前进，每个字节喂给 `switch(state)`：

[ngx_http_parse.c:143-146](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L143-L146) —— 主循环：在缓冲区 `[b->pos, b->last)` 区间内逐字节推进，`ch = *p`。

**方法识别**。`sw_start` 记下 `request_start`，转到 `sw_method` 累积字母；遇到空格时计算方法长度并查表：

[ngx_http_parse.c:163-281](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L163-L281) —— 方法识别：`switch (p - m)` 按长度分支。3 字符的 `GET`/`PUT`、4 字符的 `POST`/`HEAD`、5 字符的 `PATCH`、8 字符 `PROPFIND`、9 字符 `PROPPATCH` 都在这里。这正是「用长度快速分桶再精确比较」的优化。

**URI 开始判定**。方法后的空格之后，下一个字符决定 URI 形式：

[ngx_http_parse.c:290-311](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L290-L311) —— `sw_spaces_before_uri`：见到 `/` 进入 `sw_after_slash_in_uri`（普通路径）；见到字母则可能是绝对 URI 的 schema（如 `http://`），进入 `sw_schema`。

**HTTP 版本号识别**。URI 后空格遇到 `H`，开始逐字符校验 `HTTP/` 字样：

[ngx_http_parse.c:690-747](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L690-L747) —— `sw_http_H → HT → HTT → HTTP`，每个状态只接受唯一正确字符，否则 `INVALID_REQUEST`。见到 `/` 后转入数字解析。这是 nginx 防御「畸形版本号」的关键校验点。

**版本号数字解析与完成**。主版本号、点、次版本号逐位累积，存入 `r->http_major` / `r->http_minor`：

[ngx_http_parse.c:750-818](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L750-L818) —— `sw_first_major_digit` / `sw_major_digit` / `sw_first_minor_digit` / `sw_minor_digit`。注意 `r->http_major > 1` 直接返回 `NGX_HTTP_PARSE_INVALID_VERSION`——也就是说，nginx 在这里就拒掉了 HTTP/2.0、HTTP/3.0 这类写法（HTTP/2、HTTP/3 走的是完全不同的协议路径，不会用文本请求行）。

**完成处理**。`CR`/`LF` 触发 `goto done`，把版本号合成整数 `http_version = http_major * 1000 + http_minor`，重置状态机，返回 `NGX_OK`：

[ngx_http_parse.c:851-866](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L851-L866) —— `done:` 标签：合成 `http_version`，重置 `r->state = sw_start`，返回 `NGX_OK`。

**未完成则续跑**。循环正常结束（缓冲区读完但请求行还没结束）时，把当前进度写回请求，返回 `NGX_AGAIN`：

[ngx_http_parse.c:846-849](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L846-L849) —— 缓冲区耗尽：回写 `b->pos` 与 `r->state`，返回 `NGX_AGAIN`，下次接着解析。

#### 4.1.4 代码实践

**实践目标**：在源码层面手动「喂字符」走一遍请求行状态机，理解中断续跑。

**操作步骤**：

1. 想象缓冲区 `b` 里此刻只有 `GET / HT`（注意没有结尾，且 HTTP 只读到一半）。
2. 调用 `ngx_http_parse_request_line(r, b)`：
   - 从 `sw_start` 出发，识别出方法 `GET`（走 `case 3` 分支），到 `sw_spaces_before_uri`。
   - 见到 `/`，转 `sw_after_slash_in_uri`、再 `sw_check_uri`。
   - 见到空格，转 `sw_http_09`，见到 `H` 转 `sw_http_H`、`T`、`T`。
3. 循环到 `b->last` 退出，走到 [ngx_http_parse.c:846](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L846) 返回 `NGX_AGAIN`，`r->state` 此时停在 `sw_http_HTT`（等下一个 `P`）。
4. 现在想象下一次 `recv` 补上了 `TP/1.1\r\n`。再次调用同一函数，从 `sw_http_HTT` 续跑，直到 `done` 返回 `NGX_OK`。

**需要观察的现象**：`r->state` 在两次调用之间充当「书签」，第二次调用无需重新解析已读过的部分。

**预期结果**：两次调用分别返回 `NGX_AGAIN` 和 `NGX_OK`，最终 `r->method == NGX_HTTP_GET`、`r->http_version == 1001`（1.1）、`r->uri_start` 指向缓冲区里的 `/`。

> 这是「源码阅读型实践」，无需编译运行；理解 `r->state` 的书签作用即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么方法识别用 `switch (p - m)` 按长度分桶，而不是依次 `ngx_strncmp` 比较每个方法名？

**答案**：长度已知且是 O(1) 获取的，先按长度分桶能立刻排除绝大多数候选；同一长度内的方法再用几个字符比较即可区分。这把最坏情况下的多次字符串比较压缩成了「一次减法 + 一次 switch + 一两次字符比较」，是热路径上的常见优化。

**练习 2**：如果客户端发来 `GET / HTTP/2.0\r\n`，状态机在哪一行返回什么错误码？

**答案**：在 [ngx_http_parse.c:757-759](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L757-L759)（或 `sw_major_digit` 的 777-779），主版本号超过 1 时返回 `NGX_HTTP_PARSE_INVALID_VERSION`。

---

### 4.2 模块二：头部行解析状态机 `ngx_http_parse_header_line`

#### 4.2.1 概念说明

请求行之后是若干头部行，每行形如 `Name: Value\r\n`，最后以一个空行（`CRLF`）结束整个头部。头部解析状态机要解决三件事：

1. 切出头部**名字**与**值**，并计算名字的**小写哈希**（用于后续查哈希表）；
2. 处理折叠行、非法字符、下划线策略；
3. 区分「一个头部行解析完」（`NGX_OK`）、「整个头部结束」（`NGX_HTTP_PARSE_HEADER_DONE`，即遇到了空行）、「还需要更多数据」（`NGX_AGAIN`）。

一个关键设计：解析器在累积名字字符时**同步把它转成小写并存进 `r->lowcase_header[]`**，同时滚动计算哈希 `r->header_hash`。这样解析完一个头部行时，小写名字和哈希值已经现成，免去下游再扫一遍。

#### 4.2.2 核心流程

```
sw_start ──字母/数字/连字符──> sw_name（累积名字，滚动算 hash、转小写）
                                  │（遇到 ':'）
                                  ▼
                            sw_space_before_value ──非空白──> sw_value
                                  │                              │（CR/LF）
                                  ▼                              ▼
                              sw_almost_done <──────────────── done（一个头部完成，返回 NGX_OK）
sw_start ──CR──> sw_header_almost_done ──LF──> header_done（空行，整个头部结束，返回 HEADER_DONE）
```

返回值语义：
- `NGX_OK`：解析完一个头部行，`r->header_name_start/..` 指向它；
- `NGX_HTTP_PARSE_HEADER_DONE`：遇到空行，整个头部结束；
- `NGX_AGAIN`：缓冲区不够，需要更多数据；
- `NGX_HTTP_PARSE_INVALID_HEADER`：非法字符。

#### 4.2.3 源码精读

**状态枚举**。相比请求行更简洁，因为头部行的结构更规整：

[ngx_http_parse.c:876-885](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L876-L885) —— 头部解析的全部状态。注意它复用了 `r->state`，但因为请求行先解析完，二者时序上不冲突。

**小写映射表**。这是一张精心构造的 256 字节查找表，把 ASCII 字母映射成小写，其余合法头部字符（`-`、数字）原样保留，非法字符映射为 `\0`：

[ngx_http_parse.c:889-897](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L889-L897) —— `lowcase[]` 表。一行 `switch` 替代查表会很慢；用表实现「小写 + 合法性」双重判断只需一次数组下标。

**续跑上下文**。和请求行类似，状态、哈希、小写缓冲索引都从请求结构体读出，结尾回写：

[ngx_http_parse.c:899-903](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L899-L903) —— 读出 `state`、`header_hash`、`lowcase_index`，进入逐字符循环。

**名字起始与首字符处理**。`sw_start` 既处理「新一个头部开始」，也处理「整个头部结束」（见到 CR/LF）：

[ngx_http_parse.c:909-960](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L909-L960) —— `sw_start`：记下 `header_name_start`；见到 CR 转 `sw_header_almost_done`、见到 LF 直接 `header_done`；否则查 `lowcase[ch]` 计算首字符哈希并存进 `lowcase_header[0]`。注意 `_` 的特殊处理（取决于 `allow_underscores` 配置）。

**名字累积**。`sw_name` 状态滚动计算哈希、把字符转小写存进 `lowcase_header`，直到遇到 `:`（名字结束）或异常字符：

[ngx_http_parse.c:962-1024](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L962-L1024) —— `sw_name`：`hash = ngx_hash(hash, c)` 滚动哈希、`lowcase_header[i++] = c` 同步小写；遇到 `:` 转 `sw_space_before_value`。注意对 IIS 重复的 `HTTP/1.1 ...` 行做了容错（`sw_ignore_line`）。

**值累积**。`sw_value` 持续读到行尾（CR/LF）：

[ngx_http_parse.c:1050-1068](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L1050-L1068) —— `sw_value`：值中遇到空格转 `sw_space_after_value`（处理行内尾随空格），遇到 CR/LF 结束本行。

**三种结局**。循环结束后根据到达的标签返回不同码：

[ngx_http_parse.c:1130-1144](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L1130-L1144) —— `done:` 返回 `NGX_OK`（一个头部行完成），`header_done:` 返回 `NGX_HTTP_PARSE_HEADER_DONE`（整个头部结束）。二者都重置 `r->state = sw_start`，但只有 `header_done` 表示头部彻底结束。

#### 4.2.4 代码实践

**实践目标**：理解头部解析完后，nginx 如何把已知名头部派发给专属处理函数。

**操作步骤**：

1. 阅读 `ngx_http_process_request_headers` 中的核心循环，找到它如何调用本解析器：

   [ngx_http_request.c:1482-1483](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1482-L1483) —— `rc = ngx_http_parse_header_line(r, r->header_in, cscf->underscores_in_headers)`。`underscores_in_headers` 控制是否允许头部名含下划线，直接透传给解析器的 `allow_underscores` 参数。

2. 看 `rc == NGX_OK` 分支如何保存这个头部：

   [ngx_http_request.c:1511-1525](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1511-L1525) —— 把解析出的名字/值（以指针区间形式）压进通用列表 `r->headers_in.headers`，并把 `r->header_hash` 与小写键补齐。

3. 看它如何用哈希查找名头表并派发：

   [ngx_http_request.c:1540-1545](https://github.com/nginx/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1540-L1545) —— `ngx_hash_find(&cmcf->headers_in_hash, h->hash, h->lowcase_key, h->key.len)` 找到该头部的描述项 `hh`，调用 `hh->handler(r, h, hh->offset)` 把它写到 `r->headers_in` 的对应字段。

   > 注意：这里给的正确链接是：
   > [ngx_http_request.c:1540-1545](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1540-L1545)

4. 最后看 `rc == NGX_HTTP_PARSE_HEADER_DONE` 分支如何转入请求处理：

   [ngx_http_request.c:1554-1574](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1554-L1574) —— 空行到达，整个头部解析完毕：调用 `ngx_http_process_request_header` 做归一化校验，再 `ngx_http_process_request(r)` 进入 phase 引擎。

**需要观察的现象**：每解析完一个头部就立即派发，而不是攒齐所有头部再处理。这正是「边解析边处理」的体现。

**预期结果**：一个请求带 `Host`、`Content-Length`、`User-Agent` 三个头部时，解析器会被调用四次（前三次返回 `NGX_OK`，第四次空行返回 `HEADER_DONE`），每次 `NGX_OK` 都触发一次哈希查找与可能的 handler 调用。

#### 4.2.5 小练习与答案

**练习 1**：为什么解析头部名字时要**同步**计算哈希并存小写形式，而不是等解析完整个名字再算？

**答案**：因为解析器本来就是逐字符前进的，每个字符顺带做一次 `hash = ngx_hash(hash, c)` 与 `lowcase_header[i] = c` 几乎零额外开销；而解析完后再回头扫描一次名字会重复遍历缓冲区。把工作「捎带」在必经的逐字符循环里是 nginx 解析器的一贯风格。

**练习 2**：客户端发了一个头部 `X-Custom: hi\r\n`（非标准头）。它会经过 `headers_in_hash` 派发吗？

**答案**：会查找，但 `ngx_hash_find` 找不到匹配项，`hh` 为 `NULL`，于是 `if (hh && ...)` 条件不成立，不调用任何 handler。该头部仍然保留在 `r->headers_in.headers` 通用列表里，下游模块可以遍历它取用。

---

### 4.3 模块三：请求体读取与缓存 `ngx_http_read_client_request_body`

#### 4.3.1 概念说明

请求行和头部解析完之后，nginx 知道了请求体是否存在、有多大（`Content-Length`）或是否分块（`Transfer-Encoding: chunked`）。但 nginx **不会自动读请求体**——大多数 handler（比如返回静态文件）根本不需要请求体。必须由某个模块主动调用 `ngx_http_read_client_request_body(r, post_handler)` 才会触发读取。这是 nginx 的一个重要设计：请求体「按需读取」。

请求体读取要解决四个难题：

1. **可能很大**：不能一次性读进内存。nginx 用 `client_body_buffer_size` 控制内存缓冲上限，超出则溢写到 `client_body_temp_path` 下的临时文件。
2. **可能分块（chunked）**：要先用 `ngx_http_parse_chunked` 把分块编码剥掉，还原成连续数据。
3. **可能跨多次 `recv`**：要支持「读了点、没读完、等下次数据再来」的异步模式。
4. **请求体开头可能已经在头部缓冲区里**：因为头部和请求体共用连接的读缓冲，头部解析完时 `r->header_in` 里可能已经多读了一截请求体（称为 **preread**）。这部分要优先消化。

请求体结构体 `ngx_http_request_body_t` 把这些状态都收拢在一起：

[ngx_http_request.h:303-316](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L303-L316) —— `rb->temp_file`（临时文件）、`rb->bufs`（数据链表，最终交付给下游）、`rb->buf`（当前接收缓冲）、`rb->rest`（还剩多少字节没读）、`rb->free`/`rb->busy`（chain 节点回收链）、`rb->chunked`（chunked 解析上下文）、`rb->last_saved`（是否已收到末尾标志）。

#### 4.3.2 核心流程

请求体读取的整体流程：

```
模块调 ngx_http_read_client_request_body(r, post_handler)
        │
        ├─ 无请求体（无 Content-Length 且非 chunked）→ 直接调 post_handler，返回
        │
        ├─ 处理 preread（头部缓冲区里已多读的请求体）
        │
        ├─ 分配 rb->buf（内存接收缓冲，大小取 min(rest, client_body_buffer_size)）
        │
        └─ ngx_http_do_read_client_request_body（主读循环）
                │
                ├─ recv 读 socket → rb->buf
                │
                ├─ request_body_filter（分发）
                │       ├─ chunked → chunked_filter（剥分块）
                │       └─ 非chunked → length_filter（按 Content-Length 截断）
                │              │
                │              ▼
                │       save_filter（挂到 rb->bufs，必要时 write_request_body 落盘）
                │
                └─ rb->rest == 0 且 last_saved → 调 post_handler(r)，完成
```

**chunked 编码格式回顾**（RFC 7230）：

\[
\text{chunked-body} = \underbrace{\text{chunk-size}}_{\text{十六进制}}\;[\text{chunk-ext}]\;\text{CRLF}\;\underbrace{\text{chunk-data}}_{\text{size 字节}}\;\text{CRLF}\;\cdots\;\underbrace{0}_{\text{终止块}}\;\text{CRLF}\;\text{CRLF}
\]

每个 chunk 以十六进制长度开头，最后以一个长度为 0 的块终止。`ngx_http_parse_chunked` 就是用来逐字节解析这个格式的状态机，它返回三种值：`NGX_OK`（解析出一个 chunk 的长度，可以读数据了）、`NGX_DONE`（读到 0 长度终止块，整个体结束）、`NGX_AGAIN`（数据不够，等下次）。

#### 4.3.3 源码精读

**入口函数与无体短路**。入口先做一系列前置判断，最关键的是「没有请求体就立刻回调」：

[ngx_http_request_body.c:32-86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L32-L86) —— 分配 `rb`、设 `post_handler`；若 `content_length_n < 0` 且非 chunked，说明没有请求体，直接调 `post_handler(r)` 返回。这是「请求体按需读取」的源头——不主动读，只有模块调本函数才读。

**preread 处理**。头部缓冲区里可能已经多读了请求体开头，优先消化：

[ngx_http_request_body.c:102-147](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L102-L147) —— 把 `r->header_in` 里 `pos` 之后的多读部分当做一个 chain 喂给 `request_body_filter`。如果非 chunked 且剩余体量能整个塞进 `header_in`，干脆复用 `header_in` 当接收缓冲，省一次分配。

**分配接收缓冲并进入主循环**。若请求体较大，按 `client_body_buffer_size` 分配独立临时缓冲：

[ngx_http_request_body.c:173-204](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L173-L204) —— 取 `size = clcf->client_body_buffer_size`（再 *1.25 留余量），若非 chunked 且 `rest < size` 就只分配 `rest` 大小；`ngx_create_temp_buf` 建接收缓冲；设 `read_event_handler = ngx_http_read_client_request_body_handler`（后续 socket 可读时由它重入本流程），然后进入 `ngx_http_do_read_client_request_body`。

> 术语衔接：这里的 `r->read_event_handler` 正是 u6-l2 讲的「请求层 handler」机制。请求体读取期间，epoll 唤醒的是 `ngx_http_read_client_request_body_handler`，它再调主读循环——请求通过改写自己的 handler 来重新编程读写行为。

**主读循环**。这是请求体读取的心脏，一个 `for(;;)` 套一个 `for(;;)`：

[ngx_http_request_body.c:295-418](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L295-L418) —— 内层循环：若 `rb->buf` 满则先 flush（调 filter 把数据下推，必要时清空缓冲复用）；计算本次想读 `size = min(剩余缓冲, rest)`；`c->recv` 读 socket；读到的数据 `rb->buf->last += n`，然后喂给 `request_body_filter`。`recv` 返回 `NGX_AGAIN`（socket 暂时无数据）则跳出内层，进入外层的「挂等待」逻辑。

**recv 与背压**：

[ngx_http_request_body.c:377-397](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L377-L397) —— `n = c->recv(...)` 读 socket；`NGX_AGAIN` 跳出（等下次 epoll 唤醒）；`n == 0`（客户端提前断开）或 `NGX_ERROR` 返回 `NGX_HTTP_BAD_REQUEST`；正常则推进 `last`、累加 `request_length`，并把缓冲喂给 filter 链。

**完成与回调**：

[ngx_http_request_body.c:448-461](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L448-L461) —— `rest == 0` 退出循环后：处理 pipelined 请求头残留，删超时定时器，把 `read_event_handler` 改回 `ngx_http_block_reading`（恢复「阻塞读取」状态，等待 phase 引擎后续决定），最后调 `rb->post_handler(r)` 通知调用方「请求体就绪」。

**过滤器分发**。`recv` 拿到的原始字节要先剥掉传输编码：

[ngx_http_request_body.c:990-999](https://github.com/nginx/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L990-L999) —— `chunked` 走 `chunked_filter`，否则走 `length_filter`。

> 正确链接：[ngx_http_request_body.c:990-999](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L990-L999)

**length_filter**。按 `Content-Length` 把接收到的数据切成「属于本请求体」的部分：

[ngx_http_request_body.c:1002-1086](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1002-L1086) —— 首次调用时 `rb->rest = content_length_n`（确定要读多少）；遍历输入 chain，把每个 buf 按 `rest` 截断成新 buf 节点（`last_buf` 标记最后一个），交给 `top_request_body_filter`（即 save_filter）。剩余 `rest == 0` 时还会附带一个空 `last_buf` 节点表示结束。

**chunked_filter**。逐 chunk 解析并拼接。这是本讲的核心：

[ngx_http_request_body.c:1089-1120](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1089-L1120) —— 首次调用时分配 `ngx_http_chunked_t` 上下文（[ngx_http.h:64-68](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.h#L64-L68) 定义其 `state`/`size`/`length`），把 `content_length_n` 置 0（chunked 模式下这个值会随每个 chunk 累积，表示已收到的裸数据总量）。

[ngx_http_request_body.c:1136-1218](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1136-L1218) —— 对每个输入 buf 调 `ngx_http_parse_chunked` 解析；`rc == NGX_OK`（解析出一个 chunk 长度）时，校验 `client_max_body_size`，然后把 chunk 数据「拼接」：小 chunk（≤128 且当前 buf 够长）直接 memmove 进上一个 buf（消除 chunk 边界，让下游看到连续数据），大 chunk 则新建一个 buf 节点引用原缓冲区对应区间，并推进 `cl->buf->pos` 跳过这段数据。

[ngx_http_request_body.c:1220-1261](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1220-L1261) —— `rc == NGX_DONE`（读到 0 长度终止块）：`rb->rest = 0`，附加一个 `last_buf` 节点；`rc == NGX_AGAIN`（一个 chunk 没读完，数据不够）：按 `chunked->length` 设置下次期望读多少，跳出等更多数据；其余返回 `NGX_HTTP_BAD_REQUEST`（非法 chunked）。

**chunked 解析状态机本体**。它和请求行解析器结构完全一致，逐字符状态机：

[ngx_http_parse.c:2217-2236](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2217-L2236) —— `ngx_http_parse_chunked` 签名与状态枚举：`sw_chunk_start → sw_chunk_size → sw_chunk_extension → sw_chunk_data → sw_after_data → sw_last_chunk_extension → sw_trailer → ...`。

[ngx_http_parse.c:2255-2321](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2255-L2321) —— `sw_chunk_start`/`sw_chunk_size`：把十六进制字符累积成 `ctx->size`（如 `"a"` → 10）。`ctx->size == 0` 表示这是终止块，转 `sw_last_chunk_extension`；否则见到 CR/`;` 转 `sw_chunk_extension`。

[ngx_http_parse.c:2339-2341](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2339-L2341) —— `sw_chunk_data`：设置 `rc = NGX_OK` 并 `goto data`，告诉调用方「长度已知，接下来 `ctx->size` 字节就是数据」。

[ngx_http_parse.c:2427-2473](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2427-L2473) —— `data:` 标签：根据当前状态估算「还能读多少字节」存进 `ctx->length`，用于 chunked_filter 设置 `rb->rest`（决定下一次 `recv` 上限）。`done:` 返回 `NGX_DONE`（终止块）。

**save_filter 与落盘**。剥完编码的数据最终在这里落地：

[ngx_http_request_body.c:1273-1387](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1273-L1387) —— 把输入 chain 追加到 `rb->bufs`；遇到 `last_buf` 置 `rb->last_saved = 1`。关键落盘判定在 [1355-1384](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1355-L1384)：若 `rb->temp_file` 已存在或配置了 `request_body_in_file_only`，则调 `ngx_http_write_request_body` 把 `rb->bufs` 写进临时文件，并把 `rb->bufs` 替换成一个 `in_file` 的 buf 节点（下游从此通过文件偏移读数据）。

**write_request_body（真正写文件）**：

[ngx_http_request_body.c:547-628](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L547-L628) —— 首次调用时按 `clcf->client_body_temp_path` 创建 `ngx_temp_file_t`；之后用 `ngx_write_chain_to_temp_file` 把 `rb->bufs` 写出，更新 `temp_file->offset`，并把已写完的 chain 节点回收。这就是「请求体溢写到临时文件」的实现。

> 衔接 u2-l4：`ngx_write_chain_to_temp_file` 内部最终走的是 `ngx_writev_chain` 那类 gather 写路径，与 u4-l4 讲的 OS 抽象层 I/O 一致。

#### 4.3.4 代码实践

**实践目标**：结合源码，追踪一个 chunked POST 请求体在内存中的「拼接」过程，并理解何时溢写到临时文件。

**操作步骤**（源码阅读型，无需编译）：

1. 假设客户端发送如下请求体（chunked）：

   ```
   5\r\nHello\r\n6\r\n World\r\n0\r\n\r\n
   ```

   即两个 chunk：`Hello`（5 字节）和 ` World`（6 字节），最后是 0 长度终止块。

2. 第一次 `recv` 读到 `5\r\nHello\r\n6\r\n`：`chunked_filter` 调 `ngx_http_parse_chunked`：
   - 解析出 chunk size = 5（`sw_chunk_size` 累积 `ctx->size = 5`）。
   - 返回 `NGX_OK`，进入 `sw_chunk_data`。
   - filter 把 `Hello` 这 5 字节作为新 buf 节点挂入 `out`，推进 `pos`，`content_length_n += 5`（已收裸数据 5 字节），`ctx->size = 0`。
   - 接着遇到 `6\r\n`：解析出第二个 chunk size = 6，但此时缓冲区里没有这 6 字节数据了，`parse_chunked` 返回 `NGX_AGAIN`，filter 据此设 `rb->rest`，跳出等待更多数据。

3. 第二次 `recv` 读到 ` World\r\n0\r\n\r\n`：再次 `parse_chunked`，得到剩余 6 字节数据，新建第二个 buf 节点；然后解析到 `0` 长度块，`parse_chunked` 返回 `NGX_DONE`，filter 置 `rb->rest = 0` 并附加 `last_buf` 节点。

4. `save_filter` 把这两个 buf 节点追加进 `rb->bufs`，发现 `last_saved`，请求体读取完成，`post_handler` 被回调。

5. **何时落盘**？如果上面的 `Hello World`（11 字节）远小于 `client_body_buffer_size`（默认 8K/16K），数据只留在 `rb->bufs` 内存链表里，不碰临时文件。但如果请求体累积超过内存缓冲上限（见 `do_read_client_request_body` 中 `rb->buf->last == rb->buf->end` 的 flush 分支，[320-364](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L320-L364)），`save_filter` 会调 `write_request_body` 把内存链表刷到临时文件。

**需要观察的现象**：小 chunk（≤128 字节）会被 `memmove` 拼进上一个 buf，从而在 `rb->bufs` 里消除 chunk 边界；下游模块看到的是连续的 `Hello World`，而非两段。

**预期结果**：`r->request_body->bufs` 是一条含 11 字节 `Hello World` 数据的 chain；`r->headers_in.content_length_n == 11`（chunked 模式下累积出来的等效长度）；`r->request_body->temp_file == NULL`（因未超内存上限）。

> 想本地验证：用 `curl -v -X POST -H 'Transfer-Encoding: chunked' -d 'Hello World' http://localhost/` 发请求，配合 `error_log` 的 `debug_http` 级别，可在日志里看到 `http body chunked buf`、`http client request body recv N` 等调试行，与本讲描述一一对应。

#### 4.3.5 小练习与答案

**练习 1**：chunked_filter 里那段「小 chunk 直接 memmove 进上一个 buf」（[1159-1180](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1159-L1180)）的目的是什么？

**答案**：消除 chunk 边界、合并相邻数据，减少 `rb->bufs` 里的节点数。如果不合并，每个 chunk 都会产生一个 buf 节点，导致下游处理（如 proxy 转发）要遍历大量碎片节点。把 ≤128 字节的小 chunk 直接拼进上一个 buf，能在几乎零成本的前提下显著降低碎片化。注意它有前置条件：上一个 buf 存在、chunk 足够小、且当前输入 buf 里剩余数据 ≥ chunk size（即数据是连续可拷的）。

**练习 2**：一个请求同时带了 `Content-Length: 100` 和 `Transfer-Encoding: chunked`，nginx 会怎样？

**答案**：会在 `ngx_http_process_request_header` 里被拒掉。见 [ngx_http_request.c:2063-2074](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2063-L2074)：两者同时存在返回 `NGX_HTTP_BAD_REQUEST`，因为 RFC 规定有 chunked 时 Content-Length 必须被忽略，但同时出现通常意味着客户端实现错误或攻击，nginx 选择直接拒绝。

**练习 3**：为什么 `chunked_filter` 第一次调用时把 `rb->rest` 设成 `large_client_header_buffers.size` 而不是请求体大小？

**答案**：因为 chunked 模式下请求体总大小事先未知（要等读完所有 chunk 才知道）。nginx 用 `rest` 表示「下一次想从 socket 读多少」，初始给一个合理的批量值（借用 `large_client_header_buffers.size` 作为缓冲粒度），读到一批就解析一批 chunk、累加 `content_length_n`，逐步逼近真实大小。这与 length 模式下 `rest` 一开始就等于 `content_length_n` 形成对比。

## 5. 综合实践

把三个最小模块串起来，完成本讲指定的综合实践：**追踪一个带 chunked body 的 POST 请求，说明每个 chunk 是如何被解析、拼接并在必要时写入临时文件的。**

### 任务描述

构造一个 chunked POST 请求，要求请求体足够大（比如 1 MB）以触发临时文件落盘，然后从源码层面画出它「从字节进入 nginx 到落盘/交付」的完整调用链。

### 操作步骤

1. **准备一个会读取请求体的后端**。nginx 默认的静态文件 handler 不读请求体。要让请求体被读取，需要配置一个会调用 `ngx_http_read_client_request_body` 的模块，例如 `proxy_pass` 到一个后端：

   ```nginx
   # nginx.conf（示例配置，需本地 nginx 实例）
   http {
       server {
           listen 8080;
           client_body_buffer_size 1k;      # 故意调小，便于触发落盘
           client_body_temp_path /tmp/nginx_body;
           location / {
               proxy_pass http://127.0.0.1:9000;
           }
       }
   }
   ```

   > 把 `client_body_buffer_size` 调到 1k，1 MB 的请求体一定会溢写。

2. **发起 chunked 请求**（待本地验证具体命令）：

   ```bash
   # 用 yes 生成大文本，按 chunked 编码发送
   { printf '5\r\nHello\r\n'; head -c 1000000 /dev/zero | tr '\0' 'x' | chunked_encode; printf '0\r\n\r\n'; } | curl -v -X POST -H 'Transfer-Encoding: chunked' --data-binary @- http://localhost:8080/
   ```

   上述 `chunked_encode` 仅为示意，实际可用 `curl --data-binary` 配合 `--header 'Transfer-Encoding: chunked'`；或用 `python3` 写一个发送 chunked 的脚本。**待本地验证**。

3. **开启 debug 日志追踪调用链**。在 `error_log` 加 `debug_http`（需 `--with-debug` 编译），然后观察一次请求产生的关键日志行，并与源码对应：

   | 日志行 | 源码位置 | 含义 |
   | --- | --- | --- |
   | `http process request line` | [ngx_http_request.c:1126](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1126) | 开始解析请求行 |
   | `http request line: "POST / HTTP/1.1"` | [ngx_http_request.c:1158](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1158) | 请求行解析完成 |
   | `http header: "..."` | [ngx_http_request.c:1547](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1547) | 每个头部解析完 |
   | `http request body chunked filter` | [ngx_http_request_body.c:1107](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1107) | chunked 过滤器首次初始化 |
   | `http chunked byte: XX s:N` | [ngx_http_parse.c:2250](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2250) | chunked 状态机逐字节 |
   | `http client request body recv N` | [ngx_http_request_body.c:379](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L379) | 每次 recv 读到的字节数 |
   | `a client request body is buffered to a temporary file` | [ngx_http_request_body.c:573](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L573) | 触发了落盘 |

4. **画出完整调用链**（参考答案）：

   ```
   client bytes
     → ngx_http_process_request_line  (parse_request_line 状态机)
     → ngx_http_process_request_headers (parse_header_line 状态机，逐头派发)
     → ngx_http_process_request_header  (归一化 Content-Length/TE，本例置 chunked=1)
     → ngx_http_process_request → phases → proxy handler
     → proxy 调 ngx_http_read_client_request_body(r, post_handler)
         → ngx_http_do_read_client_request_body (主读循环)
             → recv() → rb->buf
             → ngx_http_request_body_filter
                 → chunked_filter → ngx_http_parse_chunked (逐 chunk 解析 + 拼接)
                     → top_request_body_filter = save_filter
                         → 追加到 rb->bufs
                         → 内存超限 → ngx_http_write_request_body → 临时文件
     → rest==0, last_saved → post_handler(r) → proxy 转发到后端
   ```

### 预期结果

- 请求行与头部被逐字符状态机解析，头部逐个派发；
- `chunked_filter` 把每个 chunk 的长度行解析掉、数据拼接进 `rb->bufs`，`content_length_n` 从 0 累积到真实大小；
- 由于 `client_body_buffer_size` 调小，请求体中途触发 `write_request_body`，`/tmp/nginx_body` 下出现临时文件；
- 最终 `r->request_body->bufs` 是一个 `in_file` 节点，指向该临时文件，`proxy` 模块据此把文件内容转发给后端。

## 6. 本讲小结

- **请求行解析**（`ngx_http_parse_request_line`）是一个 30+ 状态的逐字符状态机，用「先按长度分桶再字符比较」识别方法，逐字符校验 `HTTP/x.y`，通过 `r->state` 支持中断续跑；返回 `NGX_OK`/`NGX_AGAIN`/错误码三态。
- **头部解析**（`ngx_http_parse_header_line`）同样是状态机，边解析边滚动计算小写哈希、把名字转小写，省去下游二次扫描；返回 `NGX_OK`（一个头完成）/`NGX_HTTP_PARSE_HEADER_DONE`（空行，整个头结束）/`NGX_AGAIN`。
- 头部**边解析边派发**：每解析完一个头，先压进 `r->headers_in.headers`，再用 `headers_in_hash` 查名头表，命中则调专属 handler（如 `Content-Length` → `content_length_n`），体现「名字哈希 + offsetof 反射」的 nginx 通用套路。
- **请求体按需读取**：nginx 不会自动读体，模块主动调 `ngx_http_read_client_request_body` 才触发；无体则直接回调。
- 请求体经**三层过滤器链**处理：`request_body_filter` 分发 → `chunked_filter`/`length_filter` 剥传输编码 → `save_filter` 落到 `rb->bufs` 或临时文件；`chunked_filter` 用 `ngx_http_parse_chunked` 状态机逐 chunk 解析，小 chunk 用 `memmove` 拼接消除边界。
- **背压与异步**贯穿全程：`recv` 返回 `NGX_AGAIN` 时挂回读事件，`rest` 追踪剩余字节，`last_saved` 标记完成，完成后调 `post_handler` 通知调用方——这是 nginx 异步 I/O 的标准范式。

## 7. 下一步学习建议

- **请求体如何被消费**：本讲止于「请求体就绪并回调 `post_handler`」。建议接着读 u7 的 upstream 讲义，看 `proxy`/`fastcgi` 模块如何把 `r->request_body`（内存 chain 或临时文件）转发给后端。
- **phases 引擎**：本讲提到头部解析完后进入 `ngx_http_process_request` → phase 引擎。这正是 u6-l4「请求处理阶段 phases 机制」的内容，建议紧接着学，理解 content 阶段如何选择 handler（即谁会调用 `read_client_request_body`）。
- **过滤器链的对称性**：本讲的请求体过滤器链（`top_request_body_filter`）与 u6-l6 将讲的响应过滤器链（`top_header_filter`/`top_body_filter`）是镜像设计，可对比学习。
- **HTTP/2、HTTP/3 的请求体**：本讲的 `chunked_filter`/`length_filter` 只处理 HTTP/1.x。HTTP/2、HTTP/3 的请求体走完全不同的帧/流路径（`ngx_http_v2_read_request_body` 等，见 `ngx_http_request_body.c:88-100` 的分流），留待 u8 现代协议讲义展开。
