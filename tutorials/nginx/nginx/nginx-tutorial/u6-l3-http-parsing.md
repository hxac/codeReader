# 请求行、头部与请求体解析

## 1. 本讲目标

上一讲（u6-l2）我们把 HTTP 请求的生命周期从「连接 accept」一直讲到「响应完成回到 keepalive」，但故意把中间最硬核的一块——**如何把字节流变成结构化的请求对象**——当成黑盒留到了本讲。本讲就打开这个黑盒。

读完本讲，你应当能够：

1. 说清楚 nginx 用什么样的「逐字符状态机」把一行 `GET /a/b?x=1 HTTP/1.1\r\n` 拆成 method、URI、版本号，以及为什么这样做。
2. 说清楚每一个 `Host: example.com\r\n` 头部是如何被切分、做哈希、小写化并挂进 `headers_in.headers` 链表的。
3. 说清楚 Content-Length 模式与 chunked（分块传输）模式下，请求体分别是如何被读取、拼装、并在超限时溢写到临时文件的。
4. 独立跟踪一条带 chunked body 的 POST 请求，标注出每个 chunk 在源码里经过的关键函数。

本讲全部围绕三个最小模块展开：`ngx_http_parse_request_line`、`ngx_http_parse_header_line`、`ngx_http_read_client_request_body`。

## 2. 前置知识

在进入源码之前，先建立三个直觉。

**直觉一：HTTP 是「带换行的文本协议」，但网络给的是「乱序到达的字节流」。**
一次 `GET / HTTP/1.1` 可能一次性到达，也可能被 TCP 切成 `GE`、`T / HT`、`TP/1.1\r\n` 三段先后到达。因此解析器必须满足两个约束：**(a) 每收到一段就能往前推进一点，不能等全部到齐才开始；(b) 没到齐时要把「解析到哪一步」记下来，下一段来了接着干。** 这正是「状态机 + 可重入」的设计动机——nginx 把当前状态记在 `r->state` 里，每次调用从断点继续。

**直觉二：nginx 的解析器是「指针记账」式的，几乎不拷贝数据。**
解析请求行时，它只在 `r->request_start`、`r->uri_start`、`r->uri_end` 等字段里记下原始缓冲区里的起止指针，真正的 `r->uri.data` 往往就直接指向这块缓冲。理解了这一点，你才会明白为什么 nginx 要煞费苦心地维护 `large_client_header_buffers`——因为指针指向的缓冲不能被随意覆盖或释放。

**直觉三：请求头与请求体是「两套完全不同的机制」。**
请求头是**元数据**，量小且必须全部读完才能开始处理（否则不知道 Content-Length），所以走的是「读到缓冲 → 状态机切分 → 塞进链表」的同步路径；请求体是**载荷**，可能很大（上传文件几 GB），所以走的是「按需读取、内存装不下就落临时文件、读完回调业务模块」的异步、可溢写路径。两者在源码里分属不同文件，不要混为一谈。

> 名词速查：`\r\n` 是回车+换行（CRLF），HTTP 用它作行结束符；`CR=0x0D`，`LF=0x0A`。下文统一写作 CRLF。

## 3. 本讲源码地图

| 文件 | 职责 | 本讲用到的主要函数 |
| --- | --- | --- |
| `src/http/ngx_http_parse.c` | 纯解析逻辑：把字节流切成结构 | `ngx_http_parse_request_line`、`ngx_http_parse_header_line`、`ngx_http_parse_chunked` |
| `src/http/ngx_http_request.c` | 解析的**驱动者**：读 socket、调解析器、分发结果、扩展缓冲 | `ngx_http_process_request_line`、`ngx_http_process_request_headers`、`ngx_http_read_request_header`、`ngx_http_alloc_large_header_buffer`、`ngx_http_process_request_header` |
| `src/http/ngx_http_request_body.c` | 请求体的读取、过滤、临时文件落盘 | `ngx_http_read_client_request_body`、`ngx_http_do_read_client_request_body`、`ngx_http_request_body_filter`、`ngx_http_request_body_length_filter`、`ngx_http_request_body_chunked_filter`、`ngx_http_write_request_body`、`ngx_http_request_body_save_filter` |
| `src/http/ngx_http_request.h` | 关键结构体定义 | `ngx_http_request_body_t`、`ngx_http_headers_in_t` |
| `src/http/ngx_http.h` | chunked 上下文与顶层过滤钩子 | `ngx_http_chunked_t`、`ngx_http_top_request_body_filter` |

一个贯穿全讲的记忆点：**`ngx_http_parse_*.c` 只负责「看字符、挪指针、记状态」，真正「读网络、开缓冲、决定下一步做什么」的是 `ngx_http_request.c` 与 `ngx_http_request_body.c`。** 解析器是被动的，驱动者是主动的。

---

## 4. 核心概念与源码讲解

### 4.1 请求行解析状态机 ngx_http_parse_request_line

#### 4.1.1 概念说明

请求行（request line）就是 HTTP 请求的第一行，形如：

```
GET /path/to/file?name=value HTTP/1.1\r\n
```

它由三部分组成：**方法**（GET/POST/…）、**请求目标**（URI，可带 schema、host、port、query）、**HTTP 版本**。解析它的难点有三：

1. **方法名是变长的**：GET 是 3 字符，PROPPATCH 是 9 字符，不能假定长度。
2. **URI 形态多样**：最常见的是 `/path`（origin-form），但也可能是绝对地址 `http://host/path`（absolute-form，给正向代理用），甚至 `host:port`（CONNECT 方法用的 authority-form）。解析器要能分辨并切换到不同的子状态。
3. **到达是分段的**：如前置知识所述，必须可重入。

nginx 的解法是一个**显式枚举状态机**：把「正在解析请求行的哪一部分」定义为一组 `enum` 状态，主循环每读一个字符就 `switch(state)` 推进一次。

#### 4.1.2 核心流程

请求行状态机的状态迁移（简化版）如下：

```
sw_start ──大写字母──> sw_method
sw_method ──空格──> sw_spaces_before_uri ──'/'──> sw_after_slash_in_uri
                                                  （或字母──> sw_schema 走绝对地址分支）
sw_after_slash_in_uri / sw_check_uri / sw_uri ──空格──> sw_http_09
sw_http_09 ──'H'──> sw_http_H ──'T'──> sw_http_HT ──'T'──> sw_http_HTT ──'P'──> sw_http_HTTP
sw_http_HTTP ──'/'──> sw_first_major_digit ──数字──> sw_major_digit ──'.'──> sw_first_minor_digit
sw_first_minor_digit ──数字──> sw_minor_digit ──CR──> sw_almost_done ──LF──> done
```

主循环骨架（伪代码）：

```
state = r->state                      # 从上次断点恢复
for p = b->pos; p < b->last; p++:
    ch = *p
    switch(state):
        case sw_start:  r->request_start = p; ...
        case sw_method: ...
        ...
        case sw_almost_done: 若 ch==LF goto done
b->pos = p                            # 走完缓冲还没遇到行尾
r->state = state                      # 记下断点
return NGX_AGAIN                      # 告诉驱动者：还差数据，下次再来

done:
b->pos = p + 1                        # 越过 LF
r->http_version = major*1000 + minor  # 例如 1.1 -> 1001
r->state = sw_start
return NGX_OK
```

返回值有三类：
- `NGX_OK`：整行已解析完毕。
- `NGX_AGAIN`：缓冲用尽但还没到行尾，需要驱动者再读数据后重入。
- `NGX_HTTP_PARSE_INVALID_*`：遇到非法字符，直接拒绝（对应 400/414 等）。

#### 4.1.3 源码精读

函数签名与状态枚举定义。注意这 24 个状态覆盖了 origin-form、absolute-form、authority-form 三种 URI 形态：

[src/http/ngx_http_parse.c:107-139](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L107-L139) — 函数入口与 `enum state`。`state = r->state` 是「可重入」的关键：上次中断时的状态从这里恢复。

主循环逐字符推进。`for (p = b->pos; p < b->last; p++)` 只在**当前缓冲区**范围内扫描，扫完即停：

[src/http/ngx_http_parse.c:143-146](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L143-L146) — 主循环头。

方法解析很巧妙：先在 `sw_method` 状态下吃掉所有大写字母，遇到空格时根据**已读长度** `p - m` 直接跳进一个 `switch`，再用 `ngx_str3_cmp`/`ngx_str4cmp` 这类「按定长一次比较」的宏判定具体方法。这种「先按长度分流、再定长比对」的做法避免了逐字符回溯，编译器还会把 `switch` 优化成跳转表（见文件顶部注释 `/* gcc, icc, msvc and others compile these switches as an jump table */`）：

[src/http/ngx_http_parse.c:163-281](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L163-L281) — 方法名判定。例如长度 3 匹配 GET/PUT，长度 4 匹配 POST/HEAD 等。

URI 字符的快速分类用了一张 256 位的位图 `usual[]`：每一位代表一个 ASCII 字符是否为「普通、安全的 URI 字符」。判断只需 `usual[ch >> 5] & (1U << (ch & 0x1f))`，一次内存读 + 一次位与，比逐个 `case` 快得多。非普通字符（`.`、`%`、`/`、`#`、空格、CR、LF 等）才进入 `switch` 做特殊处理——例如遇到 `.`/`//` 会置 `r->complex_uri=1`，遇到 `%` 会置 `r->quoted_uri=1`，留给后续 `ngx_http_parse_complex_uri` 做完整的 URI 规范化：

[src/http/ngx_http_parse.c:17-37](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L17-L37) — `usual[]` 位图定义。

[src/http/ngx_http_parse.c:522-580](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L522-L580) — `sw_after_slash_in_uri` 状态：用位图分流，再对特殊字符置标志（`complex_uri`/`quoted_uri`/`plus_in_uri`）。

版本号解析是一串严格的状态：`H→T→T→P→/→主版本→.→次版本`，主版本大于 1 直接判 `NGX_HTTP_PARSE_INVALID_VERSION`（nginx 不支持 HTTP/2.x 走这条文本路径，HTTP/2 有独立的帧解析）：

[src/http/ngx_http_parse.c:729-818](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L729-L818) — `sw_http_HTTP` 到 `sw_minor_digit`，含版本号合法性校验。

收尾与返回。`done` 标签处把 `b->pos` 越过换行符、合成 `http_version`（`major*1000+minor`，例如 1.1 → 1001，0.9 → 9），并把状态机复位到 `sw_start`（为后续头部解析复用同一套 `r->state` 字段做准备）。HTTP/0.9 只允许 GET：

[src/http/ngx_http_parse.c:834-867](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L834-L867) — `sw_almost_done` 与 `done` 收尾。

需要强调：解析器**只记录指针**（`request_start`、`method_end`、`uri_start`、`uri_end`、`schema_start` 等），不分配、不拷贝。真正把这些指针组装成 `r->uri`、`r->method_name`、`r->args` 的是驱动函数 `ngx_http_process_request_line`（见 4.3）。

#### 4.1.4 代码实践

**实践目标**：用一个最小 C 程序，手工构造一段含请求行的缓冲，调用 `ngx_http_parse_request_line`，观察它如何填写 `r` 的各字段。

> 说明：nginx 没有为解析器提供开箱即用的独立测试入口（解析器深深嵌入 HTTP 框架，单独调用需构造大量上下文）。下面的程序是**示例代码**，目的是帮你看清字段填写逻辑；是否能在你的环境直接编译，取决于是否已 `auto/configure` 生成 `ngx_auto_config.h` 等头文件。

**操作步骤**（源码阅读型 + 思想实验）：

1. 打开 `src/http/ngx_http_parse.c`，定位 `ngx_http_parse_request_line`（L107）。
2. 假设输入缓冲 `b` 内容为 `"GET /a/b?x=1 HTTP/1.1\r\n"`，`b->pos` 指向首字符 `G`。
3. 用纸笔跟踪状态机，逐字符填表（示例代码，非项目原有）：

   ```c
   /* 示例代码：仅供理解字段填写，非 nginx 源码的一部分 */
   /*
    * 预期结果：
    *   r->request_start -> 'G'           r->method_end -> 'T'(GET 的 T)
    *   r->method        = NGX_HTTP_GET
    *   r->uri_start     -> '/'           r->uri_end     -> ' '(x=1 后的空格)
    *   r->args_start    -> 'x'           (问号后一位)
    *   r->http_major    = 1              r->http_minor  = 1
    *   r->http_version  = 1001
    *   b->pos           越过 '\n'
    *   返回值           = NGX_OK
    */
   ```

4. 再跟踪一个分两次到达的例子：第一次 `b` 只有 `"GET /a"`（无 CRLF），第二次 `"b?x=1 HTTP/1.1\r\n"`。验证第一次返回 `NGX_AGAIN` 且 `r->state` 会停在 `sw_check_uri`/`sw_uri` 一类状态，第二次从该状态续接并返回 `NGX_OK`。

**需要观察的现象**：
- 解析器从不分配内存，所有结果都是「指向 `b` 内部的指针」。
- 若缓冲在版本号中间耗尽，`r->state` 会停在某个 `sw_http_*` 状态，下次续接。

**预期结果**：上述指针与返回值与跟踪一致。若你想真正运行验证，可参考 4.4 节末尾「编译验证」的思路，在 nginx 内部加一条 debug 日志后用 `curl` 触发——这比单独抽离解析器更现实。无法独立运行时，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nginx 用 `usual[]` 位图而不是一组 `if` 判断 URI 字符是否合法？
**答**：位图把 256 个字符的合法性判断压缩成「一次数组读 + 一次位与」，分支预测友好、缓存友好；而一长串 `if` 既慢又会撑大指令缓存。nginx 在解析热路径上普遍采用这种「查表代替分支」的手法。

**练习 2**：请求行 `CONNECT example.com:443 HTTP/1.1` 会走哪条与众不同的路径？
**答**：方法识别为 `NGX_HTTP_CONNECT` 后，状态会切到 `sw_spaces_before_host` → `sw_host` → `sw_port`，因为 authority-form 没有 path，URI 部分是 `host:port`。代码在 `sw_host_end`/`sw_port` 中专门对 `r->method == NGX_HTTP_CONNECT` 做了分支处理（见 [src/http/ngx_http_parse.c:398-400](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L398-L400)）。

---

### 4.2 头部解析状态机 ngx_http_parse_header_line

#### 4.2.1 概念说明

请求行之后是一组头部（headers），每个头部形如 `Name: Value\r\n`，头部整体以一个空行 `\r\n` 结束。与请求行相比，头部解析多出两个任务：

1. **计算名字的哈希**：nginx 要在解析的同时算出头部名的哈希值并小写化，以便后续用 `ngx_hash_find` 在 O(1) 内定位该头部的处理函数（例如 `Content-Length` 头有专门处理器去填 `content_length_n`）。
2. **识别头部结束**：遇到一个空行就意味着头部区结束，要返回一个特殊的 `NGX_HTTP_PARSE_HEADER_DONE` 通知驱动者「该进入请求体/请求处理阶段了」。

同样地，头部可能跨多个 TCP 分段到达，状态机必须可重入——而且 nginx 把「正在解析第几个字节的第几个状态」「哈希算到一半的中间值」「小写化缓冲写到哪」都存在 `r->state`、`r->header_hash`、`r->lowcase_index` 里，下次续接。

#### 4.2.2 核心流程

头部状态机的状态比请求行少得多：

```
sw_start ──首字符──> sw_name            （同时开始累计 hash、写 lowcase_header）
sw_name ──':'──> sw_space_before_value ──非空──> sw_value
sw_value ──CR──> sw_almost_done ──LF──> done（返回 NGX_OK，本头部完成）
sw_start ──CR──> sw_header_almost_done ──LF──> header_done（返回 HEADER_DONE，头部区结束）
```

哈希采用经典的「乘 31 累加」：

\[
\text{hash} = (((0 \times 31 + c_1) \times 31 + c_2) \times 31 + \dots)
\]

即每读一个小写字符 `c` 就执行 `hash = hash * 31 + c`（由宏 `ngx_hash(hash, c)` 完成）。用小写字符计算哈希，是为了让 `Host`、`HOST`、`host` 三个写法哈希相同——HTTP 头部名本就大小写不敏感。

返回值：
- `NGX_OK`：成功解析出一个完整头部（驱动者应保存它，然后循环再来）。
- `NGX_HTTP_PARSE_HEADER_DONE`：遇到空行，整个头部区结束。
- `NGX_AGAIN`：缓冲用尽，需要更多数据。
- `NGX_HTTP_PARSE_INVALID_HEADER`：非法字符（如头部名里出现控制字符）。

#### 4.2.3 源码精读

函数签名与 `lowcase[]` 查表。`lowcase[ch]` 直接给出字符的小写形式（非法字符返回 `\0`），这张表把「大小写转换」和「合法性判断」合二为一：

[src/http/ngx_http_parse.c:870-897](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L870-L897) — 函数签名与 `lowcase[]` 表。第三个参数 `allow_underscores` 决定头部名里的下划线是否被接受（对应配置 `underscores_in_headers on|off`）。

可重入状态恢复。注意解析器把跨调用的中间结果都存进请求对象：

[src/http/ngx_http_parse.c:899-904](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L899-L904) — `state`、`hash`、`i`（lowcase_index）三者从 `r` 恢复。

头部名解析与增量哈希。每读一个合法字符就 `hash = ngx_hash(hash, c)`、`lowcase_header[i++] = c`。`i &= (NGX_HTTP_LC_HEADER_LEN - 1)` 是一个截断保护：头部名超过 `NGX_HTTP_LC_HEADER_LEN` 时不再累加小写缓冲（但仍继续算哈希），避免越界：

[src/http/ngx_http_parse.c:962-984](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L962-L984) — `sw_name` 状态：累计哈希、处理下划线、遇到 `:` 切到取值状态。

头部值与行结束。`sw_value` 状态扫描到 CR 或 LF 即认为本行结束，记录 `header_end`：

[src/http/ngx_http_parse.c:1050-1068](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L1050-L1068) — `sw_value` 状态。

收尾三种返回。`done` 标签返回 `NGX_OK`（一个头部完成），`header_done` 标签返回 `NGX_HTTP_PARSE_HEADER_DONE`（空行，头部区结束）。缓冲耗尽则保存中间状态返回 `NGX_AGAIN`：

[src/http/ngx_http_parse.c:1122-1145](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L1122-L1145) — 三个收尾分支：`NGX_AGAIN`、`done→NGX_OK`、`header_done→NGX_HTTP_PARSE_HEADER_DONE`。

#### 4.2.4 代码实践

**实践目标**：理解「解析器产出的指针」如何被驱动者拼成一个 `ngx_table_elt_t`（键值对）。

**操作步骤**：

1. 打开 `src/http/ngx_http_parse.c`，看 `ngx_http_parse_header_line`（L870）。
2. 假设缓冲为 `"Host: example.com\r\n"`。跟踪并填表（示例代码）：

   ```c
   /* 示例代码：解析器产出，驱动者组装 */
   /* 解析器返回后，r 里的字段：
    *   r->header_name_start -> 'H'   r->header_name_end -> ':'
    *   r->header_start     -> 'e'   (example 的 e，跳过冒号和空格)
    *   r->header_end       -> '\r'
    *   r->header_hash      = hash("host")   （用小写算）
    *   r->lowcase_header   = "host..."
    *   r->lowcase_index    = 4
    * 返回值 NGX_OK
    */
   ```

3. 然后去 4.3 节看驱动者如何用这些指针 `h->key.data = r->header_name_start`、`h->value.data = r->header_start` 直接「指针指向缓冲」地构造出 `ngx_table_elt_t`，零拷贝。

**需要观察的现象**：哈希是用**小写**算的；`header_start` 自动跳过了 `:` 后的空格（由 `sw_space_before_value` 状态消费）。

**预期结果**：填表与跟踪一致。完整链路见 4.3 的源码精读。

#### 4.2.5 小练习与答案

**练习 1**：头部名里包含下划线（如 `X_Custom: 1`）会怎样？
**答**：取决于 `allow_underscores`（由 server 块的 `underscores_in_headers` 指令决定）。允许时下划线参与哈希、写入 `lowcase_header`；禁止时置 `r->invalid_header = 1`，但仍返回 `NGX_OK`——驱动者随后会根据 `ignore_invalid_headers` 决定是丢弃该头部还是报错（见 [src/http/ngx_http_parse.c:933-946](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L933-L946) 与 [src/http/ngx_http_request.c:1489-1498](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1489-L1498)）。

**练习 2**：为什么哈希要用小写字母累加，而原始 `key` 保留原大小写？
**答**：HTTP 头部名大小写不敏感，用小写算哈希保证 `Host` 与 `host` 哈希相同，能在 `headers_in_hash` 里命中同一处理函数；而保留原 `key` 是为了日志、透传（如反代时 `proxy_pass_header`）时维持客户端发来的原始写法，不丢失信息。

---

### 4.3 头部读取驱动与大缓冲扩展（ngx_http_request.c）

请求行/头部解析器是「被动看字符」的；真正「调它、读网络、存结果」的是 `ngx_http_request.c` 里的两个驱动函数。理解它们，才能把 4.1、4.2 的解析器拼进上一讲讲的生命周期。

#### 4.3.1 概念说明

驱动函数的核心是一个 `for(;;)` 循环，反复执行三步：

1. **读数据**：若缓冲里没有未处理的字节，就调 `ngx_http_read_request_header` 从 socket 读一段进 `r->header_in` 缓冲。
2. **调解析器**：把缓冲交给 `ngx_http_parse_request_line` 或 `ngx_http_parse_header_line`。
3. **按返回值分派**：`NGX_OK` → 保存结果、继续循环；`NGX_AGAIN` → 缓冲满了但还没解析完，需要扩容；`HEADER_DONE` → 头部区结束，进入请求处理；错误 → 返回 400。

这里有一个 nginx 特有的设计：**大请求头缓冲（large_client_header_buffers）**。初始只有一个较小的缓冲（通常 1K），如果请求行或某个头部特别长、把缓冲塞满了但还没解析完，nginx 会申请一个更大的缓冲，把「半成品」拷过去继续。这既支持超长头部，又让短请求只占很小内存。但缓冲数量和大小有上限（由 `large_client_header_buffers` 配置），超过就返回 414（URI 太长）或 431（头部太大）。

#### 4.3.2 核心流程

请求行驱动 `ngx_http_process_request_line` 的循环：

```
rc = NGX_AGAIN
for(;;):
    if rc == NGX_AGAIN:
        n = ngx_http_read_request_header(r)      # 必要时从 socket 读
        if n <= 0: break                          # NGX_AGAIN(等数据) / NGX_ERROR
    rc = ngx_http_parse_request_line(r, header_in)
    if rc == NGX_OK:
        组装 request_line / method_name / 调 ngx_http_process_request_uri
        rev->handler = ngx_http_process_request_headers  # 切到头部驱动
        ngx_http_process_request_headers(rev)            # 直接进入头部解析
        break
    if rc != NGX_AGAIN:                            # 解析错误
        finalize(400 / 414)
        break
    # rc == NGX_AGAIN 且缓冲已满 → 扩容
    if header_in->pos == header_in->end:
        rv = ngx_http_alloc_large_header_buffer(r, 1)
        if rv == DECLINED: finalize(414)           # URI 太长
```

头部驱动 `ngx_http_process_request_headers` 结构类似，多出「构造 `ngx_table_elt_t`、哈希查找处理器」两步。

#### 4.3.3 源码精读

请求行驱动的循环骨架与「成功后切到头部驱动」：

[src/http/ngx_http_request.c:1136-1232](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1136-L1232) — `ngx_http_process_request_line` 主循环。注意 L1148 调用解析器；L1150-1170 在 `NGX_OK` 时组装 `request_line`、`method_name` 并调 `ngx_http_process_request_uri`；L1228-1229 把连接的读事件 handler 切成 `ngx_http_process_request_headers` 并直接调用它（这是上一讲提到的「请求层改写 handler 来重新编程读写行为」的又一处实例）。

请求行解析「缓冲满 + 还没完成」时扩容，超过上限返回 414：

[src/http/ngx_http_request.c:1253-1271](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1253-L1271) — `NGX_AGAIN` 分支调 `ngx_http_alloc_large_header_buffer`，`NGX_DECLINED` 即「连大缓冲都装不下」，返回 `NGX_HTTP_REQUEST_URI_TOO_LARGE`（414）。

读取 socket 的底层函数。它先看缓冲里有没有未消费的字节（`header_in->last - header_in->pos`），有就直接返回让解析器处理；没有才真正 `c->recv`。返回 `NGX_AGAIN` 时挂上 `client_header_timeout` 定时器并注册读事件，然后退出——这正是「分段到达」时把控制权交回事件循环的地方：

[src/http/ngx_http_request.c:1598-1652](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1598-L1652) — `ngx_http_read_request_header`。注意 L1622-1626：读不到数据时设置超时定时器，体现了请求头读取的异步性。

头部驱动的核心：解析出一个头部后，**零拷贝**构造 `ngx_table_elt_t`，再用哈希查找其专属处理器。注意 `h->key.data`、`h->value.data` 直接指向 `r->header_in` 缓冲内部，最后补一个 `\0` 便于字符串处理：

[src/http/ngx_http_request.c:1482-1552](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1482-L1552) — 头部驱动主循环：调用 `ngx_http_parse_header_line`（L1482），构造 `ngx_table_elt_t`（L1511-1538），用 `ngx_hash_find` 在 `cmcf->headers_in_hash` 里查处理器并调用（L1540-1545）。

这套「哈希表分发到处理器」的机制是 nginx 头部处理的可扩展根基：每条预定义头部在一张表里登记了「名字 → 字段偏移 → 处理函数」。例如 `Content-Length` 用 `ngx_http_process_unique_header_line`（不允许重复），`Transfer-Encoding` 同样。表片段：

[src/http/ngx_http_request.c:112-133](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L112-L133) — `Content-Length`、`Transfer-Encoding` 等头部的登记项，`offsetof` 把值直接写到 `headers_in` 结构体的对应字段。

头部区结束时的收尾。`HEADER_DONE` 触发 `ngx_http_process_request_header` 做整体验证（如 HTTP/1.1 必须带 Host、Content-Length 合法性、Transfer-Encoding 是否为 chunked 等），然后进入 `ngx_http_process_request` 开始 phase 处理：

[src/http/ngx_http_request.c:1554-1574](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1554-L1574) — `HEADER_DONE` 分支。

`Content-Length` 与 `Transfer-Encoding` 的最终裁决发生在 `ngx_http_process_request_header`。它把字符串的长度值解析成 `off_t` 存进 `r->headers_in.content_length_n`；若 `Transfer-Encoding: chunked` 则置 `r->headers_in.chunked = 1`——这两个字段是 4.4 请求体读取的**总开关**：

[src/http/ngx_http_request.c:2041-2085](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2041-L2085) — Content-Length 用 `ngx_atoof` 解析（L2041-2052）；Transfer-Encoding 仅 HTTP/1.1 允许、必须等于 `chunked`、且不能与 Content-Length 同时出现，满足则 `chunked=1`（L2054-2076）。

大缓冲扩展函数。`request_line` 参数区分是请求行还是头部在扩容；关键判定：若**单条**已解析内容超过一个大缓冲的大小，直接返回 `DECLINED`（不切分单条头部/请求行），否则从 `hc->free` 复用或新分配一个缓冲，把半成品拷过去：

[src/http/ngx_http_request.c:1655-1699](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1655-L1699) — `ngx_http_alloc_large_header_buffer` 头部。L1682-1687 是「单条超长即拒绝」的判定。

#### 4.3.4 代码实践

**实践目标**：用真实运行的 nginx 观察大缓冲与超长头部的行为。

**操作步骤**：

1. 用 4.4 节或 u1-l2 的方式从源码编译并运行 nginx，配置里加 `large_client_header_buffers 4 8k;`（4 个、每个 8KB，这是默认值）。
2. 用 `curl` 触发一个正常请求：`curl -v http://127.0.0.1/`，观察正常返回。
3. 触发「URI 太长」：`curl -v "http://127.0.0.1/$(printf 'a%.0s' {1..20000})"`。
4. 触发「头部太大」：`curl -v -H "X-Big: $(printf 'b%.0s' {1..20000})" http://127.0.0.1/`。

**需要观察的现象**：
- 第 3 步应返回 `414 Request-URI Too Large`，对应 `ngx_http_process_request_line` 中 `ngx_http_alloc_large_header_buffer` 返回 `NGX_DECLINED` 后的 `NGX_HTTP_REQUEST_URI_TOO_LARGE`。
- 第 4 步应返回 `431 Request Header Fields Too Large`，对应 `ngx_http_process_request_headers` 中同样的 `DECLINED` 路径（`NGX_HTTP_REQUEST_HEADER_TOO_LARGE`）。

**预期结果**：状态码与上面对应。若想看更细的内部路径，可在 `error_log` 加 `debug` 级别，会打印 `http alloc large header buffer` 等日志（见 [src/http/ngx_http_request.c:1665-1666](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1665-L1666)）。**待本地验证**具体状态码与日志。

#### 4.3.5 小练习与答案

**练习 1**：为什么 nginx 不一次性分配一个很大的缓冲来读请求头，而要用「小缓冲 + 按需扩展的大缓冲」？
**答**：绝大多数请求头很小（几百字节），用小缓冲能省内存、提高缓存命中率；只有少数长头部才触发大缓冲分配。若一律开大缓冲，海量并发下内存浪费严重。这是「为常见情况优化」的经典工程取舍。

**练习 2**：`ngx_http_process_request_header` 为什么要在所有头部解析完之后（而不是解析 `Content-Length` 那一刻）才校验？
**答**：头部顺序不保证——`Transfer-Encoding` 可能在 `Content-Length` 之前或之后到达。必须等全部头部收齐，才能可靠判断「两者是否同时出现」这种跨头部的约束。nginx 选择在 `HEADER_DONE` 时统一裁决。

---

### 4.4 请求体读取框架 ngx_http_read_client_request_body

#### 4.4.1 概念说明

请求头解析完后，`r->headers_in.content_length_n`（有 Content-Length 时）或 `r->headers_in.chunked`（分块传输时）已经就绪。但请求体**默认不会被自动读取**——大多数 handler（如静态文件）根本不需要请求体。只有当某个模块（如 `proxy`、`fastcgi`、`dav`）显式调用 `ngx_http_read_client_request_body(r, post_handler)` 时，nginx 才开始收请求体。这是一种**惰性、按需**的设计。

请求体读取有三个核心难点，nginx 都有专门设计：

1. **两种长度模式**：Content-Length 给出确切字节数；chunked 把数据切成带长度前缀的块，逐块到达，直到一个 `0\r\n\r\n` 终止。两者由 `ngx_http_request_body_filter` 分流到 `length_filter` 或 `chunked_filter`。
2. **内存 vs 临时文件**：体可能很大。nginx 先在内存缓冲（`client_body_buffer_size`，默认 8K/16K）里收；超过阈值就**溢写**到临时文件（`client_body_temp_path`），handler 最终拿到的是文件引用而非内存。这由 `ngx_http_request_body_save_filter` 与 `ngx_http_write_request_body` 协作完成。
3. **异步回调**：体可能分很多次到达。读取函数不能阻塞，每次读一点就交给过滤器，没读完就把控制权还给事件循环，读完后通过 `rb->post_handler` 回调通知业务模块「体好了」。

关键数据结构 `ngx_http_request_body_t`（请求体的运行时状态）：

[src/http/ngx_http_request.h:303-316](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.h#L303-L316) — 注意 `temp_file`（溢写文件）、`bufs`（已积累的体数据链）、`buf`（当前 recv 暂存缓冲）、`rest`（还差多少字节/下一次想读多少）、`chunked`（chunked 解析上下文）、`post_handler`（读完回调）。

#### 4.4.2 核心流程

整体调用层次：

```
ngx_http_read_client_request_body(r, post_handler)        # 入口（业务模块调用）
  ├─ 分配 rb，处理 preread（头部缓冲里可能已夹带了一段体）
  ├─ 分配 recv 暂存缓冲 rb->buf
  └─ ngx_http_do_read_client_request_body(r)              # 读取主循环
       └─ for(;;):
            内层循环: while rest>0:
                缓冲满 → ngx_http_request_body_filter(NULL) 清空
                n = c->recv(buf)
                ngx_http_request_body_filter(&out)        # 把收到的字节喂给过滤器
                    ├─ chunked? → chunked_filter           # 拆 chunk
                    └─ else     → length_filter            # 按 content_length_n 截断
                       └─ ngx_http_top_request_body_filter # = save_filter
                            └─ 累积到 rb->bufs；满则 ngx_http_write_request_body 落临时文件
            没读完 → 挂定时器 + 注册读事件，return NGX_AGAIN（交还事件循环）
            读完   → rb->post_handler(r) 回调业务模块
```

**Content-Length 模式**（`length_filter`）：第一次调用时 `rb->rest = content_length_n`；之后每收到一段，切下 `min(段长, rest)` 字节作为输出，`rest` 递减；`rest` 减到 0 时给输出打上 `last_buf` 标志，表示体结束。

**chunked 模式**（`chunked_filter`）更复杂，因为要逐块解析 `ngx_http_parse_chunked`：
- 它维护一个 `ngx_http_chunked_t` 上下文（`state`/`size`/`length`）跨调用续接，结构定义见 [src/http/ngx_http.h:64-68](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.h#L64-L68)。
- 每收到一段字节，调 `ngx_http_parse_chunked`：返回 `NGX_OK` 表示解析出一个 chunk（`ctx->size` 是该块数据字节数），把这块数据切出来累计进 `content_length_n`（nginx 会把 chunked 还原成「等价于带 Content-Length」的体）；返回 `NGX_DONE` 表示遇到了终止块 `0\r\n\r\n`，体结束；返回 `NGX_AGAIN` 表示数据不够，需要再读。
- `ctx->length` 是「预估完成整个流还需多少字节」，用来设定下一次 `recv` 的目标量。

#### 4.4.3 源码精读

入口函数。先做几件事：递增请求引用计数 `r->main->count++`（防止异步读取期间请求被释放，呼应上一讲的 count 机制）；分配 `rb`；处理 Expect: 100-continue；处理 **preread**——请求头读取时往往会「多读」一段字节进 `r->header_in`，这段恰好是请求体的开头，要先把这段喂给过滤器（这正是「待本地验证」的常见来源：小请求体可能仅靠 preread 就凑齐了）：

[src/http/ngx_http_request_body.c:31-86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L31-L86) — `ngx_http_read_client_request_body` 头部。L43 自增 count；L56 分配 rb；L82-86：既无 Content-Length 又非 chunked（如 GET）则直接回调，不读体。

preread 与小体快路径。若头部缓冲里夹带的体已足够（`rest <= 剩余空间`），直接把 `r->header_in` 当作 recv 缓冲，一次 `do_read` 读够即可，无需另开缓冲：

[src/http/ngx_http_request_body.c:102-147](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L102-L147) — preread 喂过滤器（L114）与小体快路径（L122-147）。

设置读事件 handler 并进入主循环。注意 L201-202 把请求层的 `read_event_handler` 设为 `ngx_http_read_client_request_body_handler`——后续体数据到达时，事件循环会经上一讲的「连接层 handler → ngx_http_request_handler 路由器 → 请求层 handler」链路回到这里：

[src/http/ngx_http_request_body.c:195-204](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L195-L204) — 分配 recv 暂存缓冲、设 handler、调 `do_read`。

读取主循环。内层 `while(rb->rest)` 反复 `c->recv` 收数据，每收一段就喂给 `ngx_http_request_body_filter`；缓冲满则先 `filter(NULL)` 刷出；没数据可读（`NGX_AGAIN`）就挂超时定时器并返回 `NGX_AGAIN` 把控制权交还事件循环。读完且 `last_saved` 后调 `rb->post_handler`：

[src/http/ngx_http_request_body.c:314-462](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L314-L462) — `ngx_http_do_read_client_request_body`。L377 是 `recv`；L405 把收到的缓冲喂过滤器；L435-445 是「还没就绪，挂定时器并返回 `NGX_AGAIN`」；L456-459 是「读完了，回调业务模块」。

过滤器分流。一行 `if` 决定走 chunked 还是 length：

[src/http/ngx_http_request_body.c:991-999](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L991-L999) — `ngx_http_request_body_filter` 分流器。

**length_filter**：初始化 `rest`，然后对每段输入按 `rest` 截断。关键细节：输出缓冲 `b` 与输入缓冲**共享内存**（`b->pos = cl->buf->pos`），是零拷贝；同时把输入缓冲的 `pos` 前移标记「已消费」：

[src/http/ngx_http_request_body.c:1003-1086](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1003-L1086) — L1016-1020 初始化 `rest = content_length_n`；L1065-1074 按 `rest` 截断并在耗尽时打 `last_buf`。

**chunked_filter**：核心是调 `ngx_http_parse_chunked` 拆 chunk。注意 L1159-1180 的小块拼装优化——若当前缓冲里已包含一个完整小块（≤128 字节），则把数据**拷贝**进上一个输出缓冲 `b`，从而「去帧化」把多个相邻 chunk 的数据拼到一起，省掉碎片缓冲；否则创建新缓冲引用这段数据。`content_length_n` 累计的是**解码后**的真实字节数（剥离了 chunk 尺寸行和 CRLF）：

[src/http/ngx_http_request_body.c:1090-1218](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1090-L1218) — L1136 调 `ngx_http_parse_chunked`；L1138-1218 处理一个 chunk；L1163/L1206/L1211 累加 `content_length_n`。

chunked 终止与「还要读多少」。`NGX_DONE` 表示终止块到达，置 `rest=0` 并打 `last_buf`；`NGX_AGAIN` 表示数据不足，用 `ctx->length`（解析器估算的剩余字节数）设定 `rb->rest`，指导下一次 `recv` 至少读到多少：

[src/http/ngx_http_request_body.c:1220-1253](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1220-L1253) — `NGX_DONE` 与 `NGX_AGAIN` 分支。

chunked 协议的状态机本身在 `ngx_http_parse_chunked`，结构与请求行解析器同构（逐字符 `switch(state)`）：`sw_chunk_start → sw_chunk_size → sw_chunk_data → sw_after_data → ... → sw_trailer`。`ctx->length` 在收尾处按当前状态估算「完成整个流还需的字节数」（含后续可能的 CRLF 与终止块）：

[src/http/ngx_http_parse.c:2217-2246](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2217-L2246) — `ngx_http_parse_chunked` 入口与状态枚举。

[src/http/ngx_http_parse.c:2427-2446](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_parse.c#L2427-L2446) — 收尾按状态估算 `ctx->length`（注意每种状态都预留了 `CRLF "0" CRLF CRLF` 共 7 字节的终止序列余量）。

**save_filter（顶层过滤器）与临时文件溢写**。过滤器链的终点是 `ngx_http_top_request_body_filter`，它被初始化为 `ngx_http_request_body_save_filter`（见 [src/http/ngx_http_core_module.c:3459](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L3459)）。它的职责是把过滤后的数据累积进 `rb->bufs` 链；当 recv 缓冲填满（`rb->buf->last == rb->buf->end`）时调 `ngx_http_write_request_body` 把已积累的数据刷到临时文件，腾出内存继续收；全部收完（`last_saved`）后，若存在临时文件，则用一个 `in_file` 的缓冲代表整份文件交给业务模块：

[src/http/ngx_http_request_body.c:1274-1334](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1274-L1334) — `save_filter` 累积 bufs、识别 `last_buf`。

[src/http/ngx_http_request_body.c:1340-1384](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1340-L1384) — 溢写与收尾逻辑：L1342-1346 缓冲满即落盘；L1355-1383 全部读完且存在临时文件时，用 `in_file` 缓冲代表整份体。

临时文件写入。首次调用时按 `client_body_temp_path` 创建临时文件，之后用 `ngx_write_chain_to_temp_file` 把 `rb->bufs` 链追加写入，写完清空链表（已落盘的内存可被 recv 缓冲复用）：

[src/http/ngx_http_request_body.c:547-628](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L547-L628) — `ngx_http_write_request_body`。L561-598 首次创建临时文件；L604 追加写入；L616-625 清空已写链表。

> 把 4.4 串起来的一句话：**recv 缓冲 `rb->buf` 是「暂存间」，过滤器链（length/chunked → save）是「分拣+打包流水线」，`rb->bufs` 是「成品库」，临时文件是「溢出仓库」**。小体只在「成品库」里（内存）；大体溢到「溢出仓库」，业务模块最终从仓库（文件）取货。

#### 4.4.4 代码实践（本讲核心实践）

**实践目标**：用真实运行的 nginx，追踪一个带 chunked body 的 POST 请求，说明每个 chunk 是如何被解析、拼装、并按需写入临时文件的。这是本讲规格指定的核心实践。

**操作步骤**：

1. 从源码编译并运行 nginx（参考 u1-l2）。要触发 `ngx_http_read_client_request_body`，最稳妥的是用 `proxy_pass` 转发到一个后端，或启用 dav 的 PUT——单用 `return` 不一定读请求体。一个可用的最小配置（转发到本地一个 echo 后端，或任意能收 POST 的端口）：

   ```nginx
   # nginx.conf 的 http{} 内
   server {
       listen 127.0.0.1:8080;
       location / {
           proxy_pass http://127.0.0.1:9000;   # 换成你自己的后端
       }
   }
   ```

   > 若没有后端，可只验证解析路径（请求会被 nginx 收完体后再尝试连后端，连不上返回 502，但**收体过程仍会发生**，debug 日志依旧可见）。**这一步是否能端到端成功，待本地验证。**

2. 开启 debug 日志以观察内部路径。在 `error_log` 后加 `debug`：

   ```nginx
   error_log /tmp/ng.log debug;
   ```

3. 用 `curl` 发一个 chunked 请求（`-T -` 配合管道会自动用 chunked）：

   ```bash
   printf 'first chunk part\nsecond chunk part\nthird chunk part\n' \
       | curl -v -X POST --data-binary @- -H "Transfer-Encoding: chunked" \
              http://127.0.0.1:8080/
   ```

   或者更明确地手写 chunked 报文（用 nc）：

   ```bash
   printf 'POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n' \
       | nc 127.0.0.1 8080
   ```

4. 在 `/tmp/ng.log` 里按顺序 grep 这些关键日志行，对照源码：

   ```bash
   grep -E 'http request body chunked filter|http body chunked buf|http client request body recv|http write client request body|http client request body rest' /tmp/ng.log
   ```

**需要观察的现象（逐 chunk 跟踪）**：

- 头部解析阶段，`Transfer-Encoding: chunked` 被识别，最终在 `ngx_http_process_request_header` 置 `r->headers_in.chunked = 1`（对应 [src/http/ngx_http_request.c:2076](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L2076)）。
- 业务模块调用 `ngx_http_read_client_request_body` 后，进入 `chunked_filter`，日志出现 `http request body chunked filter`（[src/http/ngx_http_request_body.c:1107-1108](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1107-L1108)）。
- 每收到一段字节，`ngx_http_parse_chunked` 把它拆成 chunk：先读尺寸行 `5\r\n`（`ctx->size=5`），再切出 5 字节数据 `hello`，累计进 `content_length_n`；遇到 `0\r\n\r\n` 返回 `NGX_DONE`，体结束。
- 若你发的体很小（如本例），整份体只进内存 `rb->bufs`，**不会**出现 `http write client request body`（临时文件写入）日志。
- 再发一个超过 `client_body_buffer_size` 的大 chunked 体（例如 `head -c 100000 /dev/urandom | base64` 用 chunked 发送），则会看到 `http write client request body` 日志，且临时目录下出现请求体文件。

**预期结果**：
- 小体：日志显示 chunk 被逐个解析、`content_length_n` 累计到真实字节数，无临时文件。
- 大体：内存缓冲填满后触发 `ngx_http_write_request_body`，数据落盘，`rb->bufs` 最终以一个 `in_file` 缓冲代表整份文件。
- 报文 `5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n` 最终被还原成 `hello world`（11 字节），`content_length_n == 11`。

**若无法运行**：请标注「待本地验证」，但仍应能据上述源码链接完整说明每个 chunk 的解析、拼装、溢写路径——这正是「源码阅读型实践」的目标。

#### 4.4.5 小练习与答案

**练习 1**：为什么 chunked 模式下 `content_length_n` 会从一个「未知」变成确定值？
**答**：chunked 协议本身不预先声明总长度，但 nginx 在 `chunked_filter` 里每解码一个 chunk 就把其数据字节数累加进 `content_length_n`（见 [src/http/ngx_http_request_body.c:1163](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1163) 等处）。这样收完时 `content_length_n` 就是体的真实长度，对业务模块而言「等同于带 Content-Length」，模块代码无需关心传输编码。

**练习 2**：recv 缓冲 `rb->buf` 与成品链 `rb->bufs` 是什么关系？为什么 `length_filter` 里输出缓冲能和 recv 缓冲共享内存？
**答**：`rb->buf` 是接收暂存间，反复用于 `recv`；`rb->bufs` 是已确认属于请求体、待业务模块消费的成品链。`length_filter` 产出的输出缓冲 `b` 把 `pos/last` 直接指向 `rb->buf` 内部那块刚收到的内存（零拷贝），同时把 `rb->buf->pos` 前移标记已消费。只要在 `rb->buf` 被复用（reset 后重新 recv）之前，那段内存已被 `save_filter` 写进临时文件（或体已读完不再 recv），共享就是安全的。这就是「内存装得下就零拷贝引用，装不下就落盘」的协作基础。

**练习 3**：如果客户端发了一个声称 100MB 的 chunk 尺寸行，nginx 会立刻分配 100MB 内存吗？
**答**：不会。`chunked_filter` 用 `client_max_body_size` 做总量校验（[src/http/ngx_http_request_body.c:1144-1157](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request_body.c#L1144-L1157)），超出直接返回 413；而实际接收只用固定大小的 recv 缓冲（`client_body_buffer_size`），满了就刷盘。nginx 绝不会按客户端声称的尺寸预分配内存。

---

## 5. 综合实践

把本讲三块知识串起来，做一次**完整的「字节到请求对象」跟踪**。

**任务**：构造一个一次性到达、包含请求行、两个头部、一段 chunked 请求体的完整 HTTP/1.1 报文，跟踪它在 nginx 内部从字节流变成「可被 phase 处理的请求 + 可被业务模块读取的请求体」的全过程，产出一张「函数调用 + 字段填写」的时间线表。

**输入报文**（你可以用 `nc` 发给本地 nginx）：

```
POST /upload?lang=zh HTTP/1.1\r\n
Host: 127.0.0.1\r\n
Transfer-Encoding: chunked\r\n
\r\n
4\r\n
Wiki\r\n
5\r\n
pedia\r\n
0\r\n
\r\n
```

**要求产出的时间线**（每行：阶段 → 函数 → 关键字段变化）：

| 阶段 | 函数（带源码行号链接） | 关键字段变化 |
| --- | --- | --- |
| 读请求行 | `ngx_http_process_request_line` → `ngx_http_parse_request_line` | `method=POST`、`uri_start=/upload`、`args_start=lang=zh`、`http_version=1001` |
| 读头部 1 | `ngx_http_process_request_headers` → `ngx_http_parse_header_line` | `header_hash=hash("host")`、构造 `ngx_table_elt_t{Host,127.0.0.1}` |
| 读头部 2 | 同上 | `header_hash=hash("transfer-encoding")`，处理器登记 `transfer_encoding` |
| 头部结束 | `HEADER_DONE` → `ngx_http_process_request_header` | 校验通过，置 `chunked=1` |
| 进入 phase | `ngx_http_process_request` | （下一讲 u6-l4 的 phases 主题） |
| 读请求体 | `ngx_http_read_client_request_body` → `chunked_filter` → `ngx_http_parse_chunked` | chunk `4`→`Wiki`；chunk `5`→`pedia`；终止块→`last_buf`；`content_length_n=9` |

**验证方法**：开 `debug_http` 日志，对照上表逐条 grep，确认每个阶段的日志与字段变化与你预测的一致。重点关注：URI 的 query 是如何被 `args_start` 切出来的（呼应 `ngx_http_process_request_uri`）、chunked 是如何在头部结束时被「开关化」的、以及 `Wikipedia` 这 9 个字节最终是如何从两个 chunk 拼成的。

> 提示：如果懒得手敲报文，理解到位的标志是——你能向别人讲清楚「为什么 `Transfer-Encoding: chunked` 必须等所有头部解析完才能确认」以及「为什么小请求体可能根本不触发临时文件写入」。

## 6. 本讲小结

- nginx 用**逐字符、可重入的状态机**解析请求行与头部（`ngx_http_parse_request_line` / `ngx_http_parse_header_line`），状态记在 `r->state`，跨 TCP 分段续接；解析器只挪指针、记状态，几乎零拷贝、零分配。
- 头部解析同时做**增量哈希**（`hash*31+小写字符`）和小写化，配合 `headers_in_hash` 实现「头部名 → 处理函数」的 O(1) 分发；头部区以空行结束，返回 `NGX_HTTP_PARSE_HEADER_DONE`。
- 真正「读 socket、调解析器、扩容缓冲、保存结果」的是 `ngx_http_request.c` 的驱动函数；**大请求头缓冲**机制让短请求省内存、长请求不爆掉，超限返回 414/431。
- 请求体**按需读取**：业务模块显式调 `ngx_http_read_client_request_body` 才开始收；Content-Length 走 `length_filter`，chunked 走 `chunked_filter`（内部再调 `ngx_http_parse_chunked` 状态机），二者都把结果汇入 `save_filter`。
- 请求体走「**内存缓冲优先、超限溢写临时文件**」策略：recv 暂存间 `rb->buf` → 过滤器分拣 → 成品链 `rb->bufs`；超过 `client_body_buffer_size` 由 `ngx_http_write_request_body` 落盘，业务模块最终拿到内存链或文件引用。
- 读取全程异步：没读够就挂超时定时器、返回 `NGX_AGAIN` 交还事件循环，读完后用 `rb->post_handler` 回调业务模块；`r->main->count++` 守护请求在异步期间不被释放。

## 7. 下一步学习建议

本讲讲清了「请求怎么被解析、请求体怎么被收齐」。自然的下一步是：

1. **u6-l4 请求处理阶段 phases 机制**：头部解析完、请求体（若需要）读完后，`ngx_http_process_request` 会驱动 11 个 phase（postread → … → content）。理解 phases 才能明白 access、rewrite、content 等模块在何时介入。
2. **u6-l5 location 匹配与配置合并**：请求行解析出的 URI 是如何被用来匹配 server/location 的，配置又是如何沿配置树合并的。
3. **u6-l6 过滤器链**：与请求体读取对称，响应也有一条过滤器链（header/body filter）。本讲的「过滤器」是请求体过滤器（`top_request_body_filter`），u6-l6 讲的是响应过滤器，两者结构相似、方向相反，对照阅读会很有收获。
4. **u7-l1 upstream 框架**：当业务模块是 `proxy_pass` 时，请求体收齐后还要转发给后端，upstream 框架会复用本讲建立的 `ngx_chain_t` / 临时文件机制。

建议继续精读的源码：`src/http/ngx_http_parse.c` 中尚未展开的 `ngx_http_parse_complex_uri`（URI 规范化）、`ngx_http_parse_status_line`（后端响应行解析，upstream 会用到），以及 `src/http/ngx_http_request_body.c` 的 `ngx_http_discard_request_body`（不需要体时的丢弃路径，与读取路径对称）。
