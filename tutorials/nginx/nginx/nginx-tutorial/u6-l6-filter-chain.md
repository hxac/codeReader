# 过滤器链 header/body filter

## 1. 本讲目标

本讲是「HTTP 核心处理」单元里关于「响应如何输出」的核心一讲。前面几讲我们讲清楚了请求怎么被解析（u6-l3）、怎么走完 11 个 phase（u6-l4）、怎么选中 location（u6-l5）。这一讲回答最后一个问题：

> **content handler 产生响应之后，这些响应字节是怎么一层层加工、最终写到 socket 上的？**

读完本讲，你应当能够：

1. 说清 nginx 的两条过滤器链（header filter / body filter）是如何用「next 指针」串联起来的，以及为什么串联动作发生在 `postconfiguration` 阶段。
2. 看懂任何一个 filter 模块的 `init` 函数里那两行「`next = top; top = self`」在做什么。
3. 理解 `ngx_http_write_filter` 作为 body 链的终点，如何承担限速、攒包、真正调 `send_chain` 写 socket 的职责。
4. 理解 `ngx_http_copy_filter` 如何借助 `ngx_output_chain` 把「指向文件的 buf」读成「内存 buf」，让 gzip 等需要看到内容的 filter 能工作。
5. 理解 `ngx_http_header_filter` 作为 header 链的终点，如何把结构化的 `headers_out` 序列化成一行行 HTTP 响应头字节。
6. 画出一次静态文件响应从 content handler 到 socket 经过的所有 filter 节点顺序。

## 2. 前置知识

本讲假设你已经掌握以下内容（均来自前面讲义）：

- **`ngx_buf_t` 与 `ngx_chain_t`（u2-l4）**：nginx 数据流的原子单位是 buf，buf 用 chain 串成单链表。一个 buf 既可以表示内存数据（`pos..last`），也可以表示文件数据（`file_pos..file_last`，置 `in_file=1`）。本讲里 filter 之间传递的就是 `ngx_chain_t *`。
- **模块系统与 `postconfiguration` 回调（u3-l3）**：每个 HTTP 模块都可在解析完配置后被回调一次 `postconfiguration(cf)`，这是 filter 模块把自己「挂进链里」的唯一时机。
- **HTTP 框架与三层配置（u6-l1）**：`ngx_http_block` 是 `http{}` 块的装配线，它在末尾会按模块顺序逐个调用各模块的 `postconfiguration`。
- **请求生命周期（u6-l2）**：content handler 在 CONTENT 阶段生成响应，随后调用 `ngx_http_send_header(r)` 与 `ngx_http_output_filter(r, in)` 把响应送出——这两个函数正是过滤器链的入口。

一个直觉比喻：过滤器链就像一条**流水线**。content handler 把「半成品」（一个 chain）放到流水线起点，流水线上每一站（一个 filter）都对其进行一道加工（压缩、分块、加头部、复制文件到内存……），最后一站（write_filter）把它真正装车发走（写 socket）。每一站只关心自己的活，并把处理后的 chain 递给下一站。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/http/ngx_http.c` | 声明三个全局「链头」指针 `ngx_http_top_header_filter` / `ngx_http_top_body_filter` 等；包含按模块顺序调用 `postconfiguration` 的循环。 |
| `src/http/ngx_http_core_module.h` | 定义 filter 函数指针类型 `ngx_http_output_header_filter_pt` / `ngx_http_output_body_filter_pt`。 |
| `src/http/ngx_http_core_module.c` | 实现「入口函数」`ngx_http_send_header`（进 header 链）与 `ngx_http_output_filter`（进 body 链）。 |
| `src/http/ngx_http_write_filter_module.c` | **body 链终点**。攒包、限速、调 `c->send_chain` 真正写 socket。 |
| `src/http/ngx_http_header_filter_module.c` | **header 链终点**。把 `r->headers_out` 序列化成 HTTP 响应头字节。 |
| `src/http/ngx_http_copy_filter_module.c` | 文件 buf 与内存 buf 的桥接器，内部驱动 `ngx_output_chain`。 |
| `src/http/ngx_http_postpone_filter_module.c` | 为子请求（subrequest）排序输出，保证父请求在子请求之后发出。 |
| `auto/modules` | 用 `ngx_module_order` 列出 filter 模块的「期望顺序」，决定链的最终排布。 |

## 4. 核心概念与源码讲解

### 4.1 过滤器链的运行机制：top 指针、next 指针与栈式注册

#### 4.1.1 概念说明

nginx 的 HTTP 响应输出有**两条独立的链**：

- **header filter 链**：处理响应头（状态码、`Content-Type`、`Server` 等），由 `ngx_http_send_header(r)` 触发。
- **body filter 链**：处理响应体（chain of bufs），由 `ngx_http_output_filter(r, in)` 触发。

两条链结构完全对称，机制相同。每条链由若干 filter 模块串联而成，串联靠的是**两个指针**：

1. 一个**全局链头指针**：`ngx_http_top_header_filter`（header 链）、`ngx_http_top_body_filter`（body 链）。它是链的入口，入口函数（`ngx_http_send_header` / `ngx_http_output_filter`）只认这一个指针。
2. 每个 filter 模块**自己持有一个 static 的 next 指针**：记录「我下面那一站是谁」。它不暴露给别的模块，是文件私有的。

注意一个容易被忽视的细节：链**不是**一张全局共享的链表。除了链头是全局变量，其余每个节点的 `next` 都是各模块的 `static` 变量。于是整条链在源码里「看不见」——它是在 `postconfiguration` 阶段被一段段拼起来的。

#### 4.1.2 核心流程

filter 模块挂链的固定套路是：在 `postconfiguration` 回调里写两行：

```c
ngx_http_next_body_filter = ngx_http_top_body_filter;  // 1. 把当前链头存为自己的 next
ngx_http_top_body_filter  = ngx_http_my_filter;        // 2. 把自己设为新链头
```

这就是一个**栈的 push 操作**：每次有新 filter 注册，它都「插」到链头，把原来的链头压到自己的 `next`。

由于 `ngx_http_block` 在末尾**按 `ngx_modules[]` 的顺序**逐个调用 `postconfiguration`（顺序由 `auto/modules` 里的 `ngx_module_order` 决定），所以：

- 排在 `ngx_module_order` **越靠前**的 filter，越早被 push，最终被压在**链尾**（离 socket / header 终点更近）。
- 排在 **越靠后**的 filter，越晚被 push，最终位于**链头**（content handler 第一个调用它）。

```
注册顺序（postconfiguration 调用顺序）：
  write_filter  →  header_filter  →  chunked_filter  →  ...  →  copy_filter  →  range_body_filter

每一步是 push（插到链头），所以最终调用顺序（content handler 视角）：
  range_body_filter  →  copy_filter  →  ...  →  chunked_filter  →  write_filter(socket)
   (链头/最先调)                                                    (链尾/真正发)
```

关键：**注册顺序与调用顺序正好相反**，这是栈式 push 的直接结果。理解这一点，是看懂后面所有 filter 行为的前提。

#### 4.1.3 源码精读

**(1) 三个全局链头指针的声明**

这三个全局变量最初都是 `NULL`，等各 filter 的 `postconfiguration` 把它们一点点填上。

[src/http/ngx_http.c:74-77](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L74-L77) — 声明 header / body / early_hints 三条链的链头指针。

**(2) filter 函数指针的类型定义**

[src/http/ngx_http_core_module.h:530-532](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.h#L530-L532) — header filter 只收 `r`，body filter 还多收一个 `chain`。

[src/http/ngx_http.h:196-198](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.h#L196-L198) — 用 `extern` 把链头指针暴露给所有 HTTP 模块。

**(3) 入口函数：进入 header 链 / body 链**

content handler 调 `ngx_http_send_header(r)` 即进入 header 链：

[src/http/ngx_http_core_module.c:1871-1890](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1871-L1890) — 做少量前置检查（`post_action`、防重复发头、`err_status` 改写），然后 `return ngx_http_top_header_filter(r)`，即调用链头。

调 `ngx_http_output_filter(r, in)` 即进入 body 链：

[src/http/ngx_http_core_module.c:1924-1943](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1924-L1943) — 记一条 debug 日志后 `ngx_http_top_body_filter(r, in)`，并注意：只要任一 filter 返回 `NGX_ERROR`，就把 `c->error` 置 1（后续 write_filter 一进来就拒绝）。

**这两个入口函数本身不「认识」任何具体 filter**，它们只调用链头指针。整条链是怎么拼起来的，由下一处代码决定。

**(4) 拼链的发动机：postconfiguration 循环**

[src/http/ngx_http.c:303-315](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http.c#L303-L315) — `ngx_http_block` 在初始化尾声，遍历 `cf->cycle->modules[]`，对每个 HTTP 模块调用其 `postconfiguration(cf)`。**顺序就是模块在数组里的顺序**，而模块顺序由构建期 `ngx_module_order` 决定。

**(5) 决定顺序的清单：ngx_module_order**

[auto/modules:148-176](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/auto/modules#L148-L176) — 这段就是 filter 模块的「期望顺序」。请记住其中几个关键位置：`write_filter`(156)、`header_filter`(157)、`chunked_filter`(158)、`gzip_filter`(162)、`postpone_filter`(163)、`headers_filter`(172)、`copy_filter`(173)、`range_body_filter`(174)、`not_modified_filter`(175)。

**(6) push 操作的样板代码（两行）**

看 copy_filter 的 init：

[src/http/ngx_http_copy_filter_module.c:389-396](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L389-L396) — `ngx_http_next_body_filter = ngx_http_top_body_filter; ngx_http_top_body_filter = ngx_http_copy_filter;`。先保存旧链头为自己的 next，再让自己成为新链头。

对照 chunked 与 gzip（它们同时挂 header 与 body 两条链，所以写四行）：

[src/http/modules/ngx_http_chunked_filter_module.c:336-340](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_chunked_filter_module.c#L336-L340) — chunked 同时 push 进 header 链与 body 链。

[src/http/modules/ngx_http_gzip_filter_module.c:1130-1134](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_gzip_filter_module.c#L1130-L1134) — gzip 同理。

**(7) next 指针是文件私有的**

注意 copy_filter 的 next 声明带 `static`：

[src/http/ngx_http_copy_filter_module.c:79](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L79) — `static ngx_http_output_body_filter_pt ngx_http_next_body_filter;`

[src/http/ngx_http_postpone_filter_module.c:51](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L51) — postpone 也有一份同名但独立的 `static` next。

每个 filter 都叫 `ngx_http_next_body_filter`，但因为都是 `static`，互不干扰——这正是「链不可见、却真实存在」的来源。

#### 4.1.4 代码实践

**实践目标**：亲手验证「注册顺序 = postconfiguration 调用顺序 = `ngx_module_order` 顺序」，并据此推断链的调用顺序。

**操作步骤**：

1. 打开 `auto/modules` 第 148–176 行的 `ngx_module_order`，把其中所有名字含 `filter` 的模块抄下来，保留原顺序。
2. 对每个 filter 模块，用 `Grep` 在它对应的 `.c` 文件里搜 `ngx_http_top_body_filter =` 与 `ngx_http_top_header_filter =`，确认它挂的是 header 链、body 链还是两条都挂。
3. 模拟 push 过程：准备一张纸，从 `ngx_module_order` 第一个 filter 开始，逐个执行「next = 当前top；top = 自己」，每步画出当前链的样子。

**需要观察的现象**：最先注册的 write_filter 最终在 body 链的最末（next 为空，是终点）；最后注册的 filter（如 range_body / not_modified）最终在链头。

**预期结果**：你应当得到与「综合实践（第 5 节）」一致的调用顺序图。注意区分「总是编译」与「需 `--with` 才编译」的 filter——后者在默认构建里不存在，跳过即可。

#### 4.1.5 小练习与答案

**练习 1**：为什么 nginx 用「全局链头 + 各模块 static next」而不是一张全局数组来组织 filter 链？

> **参考答案**：用静态链接的 filter 各自编译进二进制，彼此不该互相引用名字。每个模块只认 `ngx_http_top_*_filter`（全局链头）这一个外部符号，自己的 next 是私有 static，这样模块之间**零耦合**，新增/裁剪 filter 不必改动任何已有模块的源码——这是「模块化」在 filter 机制上的体现。

**练习 2**：如果两个 filter 在 `ngx_module_order` 里的相对顺序被调换，链的调用顺序会怎样变？

> **参考答案**：因为是栈式 push，相对顺序被调换后，它们在最终链里的相对位置也会**整体反转**。比如 A 在 B 之前注册则调用顺序是「B 先 A 后」；调换后变成「A 先 B 后」。这也是为什么 `ngx_module_order` 必须精心维护——顺序写错会导致 gzip 包在 chunked 外面等严重错误。

---

### 4.2 ngx_http_write_filter：body 链的终点，真正写 socket

#### 4.2.1 概念说明

`ngx_http_write_filter` 是 **body 链的终点**：所有 body filter 加工完数据，最终都汇到它这里，由它把数据真正写到 socket。注意它的 init 与众不同——它**没有**「先存 next 再设 top」的两步，而是直接把自己设为链头，因为它就是链的终点（next 永远为空）。

它同时承担三件实事：

1. **攒包**：把上游多个 filter 传来的零散 chain 拼到请求私有的 `r->out` 上，小于 `postpone_output`（默认 1460 字节）时先不发，攒够再发，提高 TCP 发送效率。
2. **限速**：实现 `limit_rate`，按已发送字节数与耗时算出本轮可发的额度，发多了就挂一个定时器延迟。
3. **真正写**：调用 `c->send_chain(c, r->out, limit)`，这个函数指针在连接初始化时被设为对应平台的发送链（如 Linux 上的 `ngx_linux_sendfile_chain`，见 u4-l4）。

#### 4.2.2 核心流程

write_filter 的执行步骤（伪代码）：

```
1. 若连接已出错(c->error) → 立即返回 NGX_ERROR
2. 扫描 r->out（上次没发完留下的旧 chain） + 本次入参 in（新 chain）
   —— 累加总大小 size，标记是否有 last_buf / flush / sync
   —— 顺带做合法性检查（零长度非特殊 buf、负长度 buf 报 ALERT）
3. 把新 chain 链到 r->out 末尾
4. 攒包判定：若 不是last 且 不是flush 且 有入参 且 size < postpone_output → 返回 NGX_OK（攒着不发）
5. 若写事件被限速挂起(c->write->delayed) → 标 buffered，返回 NGX_AGAIN
6. 限速处理(limit_rate)：算本轮上限 limit；若已超额 → 设 delayed + add_timer，返回 NGX_AGAIN
7. sent = c->sent; chain = c->send_chain(c, r->out, limit);   ← 真正写 socket
8. 若限速：根据本轮新发字节数算 delay，必要时再挂定时器
9. 回收已发完的节点：从 r->out 头部一直回收到 chain 指向的未发节点；r->out = chain
10. 若 chain 非 NULL（还有没发完的）→ 标 NGX_HTTP_WRITE_BUFFERED，返回 NGX_AGAIN
11. 否则清 buffered 标志，返回 NGX_OK
```

第 7 步的返回值 `chain`：`send_chain` 返回的是「**还没发完、剩余的 chain**」。返回 `NULL` 表示全发完了；返回非空表示被限速或写缓冲满挡住了。

#### 4.2.3 源码精读

**整体函数**：

[src/http/ngx_http_write_filter_module.c:47-362](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L47-L362) — `ngx_http_write_filter` 全文。

**关键点①：攒包阈值 `postpone_output`**

[src/http/ngx_http_write_filter_module.c:211-221](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L211-L221) — 注释解释得很清楚：没有 last、没有 flush、有入参、且总大小小于 `postpone_output` 时，直接 `return NGX_OK` 把数据留在 `r->out` 里，不发。默认 `postpone_output` 是 1460（一个典型 TCP MSS），目的是避免发一堆极小的包。

**关键点②：被限速挂起时立即让出**

[src/http/ngx_http_write_filter_module.c:223-226](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L223-L226) — 若 `c->write->delayed`（之前限速挂的定时器还没到期），标 `NGX_HTTP_WRITE_BUFFERED` 并返回 `NGX_AGAIN`。这个 buffered 位会被上游 filter（如 copy_filter）看到，从而知道「下层还在缓冲，数据没真发出去」。

**关键点③：limit_rate 计算**

[src/http/ngx_http_write_filter_module.c:258-292](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L258-L292) — 这是 `limit_rate` / `limit_rate_after` 指令的真正实现处。核心思想：

本轮允许再发的额度（字节数）

\[
\text{limit} = \text{limit\_rate} \times (\text{ngx\_time}() - r\text{->start\_sec} + 1) - (c\text{->sent} - \text{limit\_rate\_after})
\]

即「按平均速率算到此刻总共可以发多少 − 已经发了多少（扣除起算免额）」。若 `limit <= 0` 说明已经发超前了，挂定时器延迟 `(-limit)*1000/limit_rate + 1` 毫秒。`sendfile_max_chunk` 则是给单次 `sendfile` 的上限，避免一个超大文件一次性占满 worker。

**关键点④：真正写 socket**

[src/http/ngx_http_write_filter_module.c:299](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L299) — `chain = c->send_chain(c, r->out, limit);`。`c->send_chain` 是连接上的函数指针，在 worker 初始化时被设为平台发送链（Linux 上是 `ngx_linux_sendfile_chain`）。它返回**未发完的 chain**。若返回 `NGX_CHAIN_ERROR` 则标 `c->error=1`。

**关键点⑤：回收已发节点 + 留尾**

[src/http/ngx_http_write_filter_module.c:338-349](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L338-L349) — 从 `r->out` 头部回收到 `chain` 之前，归还 chain 节点（注意是归还 chain link 结构，buf 本身由各 filter 的 tag 管理）；`r->out = chain` 保留未发部分；若 `chain` 非空则标 buffered 返回 `NGX_AGAIN`，否则返回 `NGX_OK`。`r->response_sent` 在 `last` 时置 1。

**关键点⑥：write_filter 是终点，init 不存 next**

[src/http/ngx_http_write_filter_module.c:365-371](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_write_filter_module.c#L365-L371) — `ngx_http_top_body_filter = ngx_http_write_filter;`，只有一行，没有「next = top」——因为它是链的起点（最先注册）也是终点（被压到链尾），它没有下一站。

#### 4.2.4 代码实践

**实践目标**：通过 debug 日志观察 write_filter 的攒包与限速行为。

**操作步骤**：

1. 用 `--with-debug` 编译 nginx（见 u10-l5），配置 `error_log /tmp/e.log debug_http;`。
2. 写一个最小配置：一个 server，根目录放一个 1MB 的文件 `big.bin`，加一行 `limit_rate 50k;`。
3. 用 `curl http://127.0.0.1/big.bin -o /dev/null` 拉取。
4. 在 `/tmp/e.log` 里 grep 关键字 `http write filter:`。

**需要观察的现象**：你会看到大量形如 `http write filter: l:0 f:0 s:NNNN` 的行（l=last, f=flush, s=size）。在限速生效时，还会看到 `http write filter limit NNNN` 与周期性的定时器挂起；`s` 会随攒包增长，到 `postpone_output` 或 flush 时才真正触发 send_chain。

**预期结果**：由于限速 50k，下载会明显分段；日志里 `write filter` 的返回值会在 `NGX_AGAIN`（chain 非空，被限速挡住）与 `NGX_OK`（本轮发完）之间交替。**待本地验证**（具体日志条数取决于内核 socket 缓冲与定时精度）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 write_filter 要在「size 小于 postpone_output 且非 last/flush」时主动不发送？

> **参考答案**：HTTP 响应经常是「状态行 + 头 + 一小块体」分多次产出，若每次有数据就立刻 write，会产生大量极小的 TCP 报文（tinygram），浪费带宽与 CPU。攒到约一个 MSS（1460 字节）再发，能显著提升吞吐。`last_buf`（响应结束）和 `flush`（强制刷新，如 chunked 边界）会绕过攒包立即发送，保证语义正确。

**练习 2**：`send_chain` 返回非空 chain 时，write_filter 设置了 `NGX_HTTP_WRITE_BUFFERED`。这个标志对上游 filter 有什么意义？

> **参考答案**：它告诉上游「数据还滞留在 write_filter 的 `r->out` 里没真正发出去」。对于需要按顺序、确认发送后才推进的 filter（例如要复用 buf 的 recycled 过滤器，或 upstream 的 event_pipe），看到该标志就知道不能继续往下塞新数据，必须等写事件再次就绪。这是 nginx 背压（backpressure）机制在 filter 层的体现。

---

### 4.3 ngx_http_copy_filter：用 ngx_output_chain 把文件读成内存 buf

#### 4.3.1 概念说明

很多 filter 想看到响应体的**实际内容**才能工作：gzip 要压缩它、charset 要转码、sub 要做文本替换、range 要截取字节范围。但 content handler（如静态文件模块）给出的 buf 往往是 **`in_file` 的**——只记录文件偏移，并不把内容读进内存。于是需要一个「翻译器」：当下游 filter 需要内存数据时，负责把文件内容按需读进临时内存 buf；当下游能直接吃文件 buf（开了 sendfile 且没人需要内存形态）时，则零拷贝透传。

这个翻译器就是 `ngx_http_copy_filter`。它的实现核心是复用 u2-l4 讲过的通用输出框架 `ngx_output_chain`——copy_filter 只是给 `ngx_output_chain` 准备好上下文（`ngx_output_chain_ctx_t`），把真正的「读文件 / 透传」决策交给 `ngx_output_chain`。

#### 4.3.2 核心流程

```
copy_filter(r, in):
  ctx = 从 r 的模块上下文取 ngx_output_chain_ctx_t
  若 ctx 为空（第一次进）:
     —— 分配并初始化 ctx：
        ctx->sendfile       = c->sendfile        // 连接是否支持 sendfile
        ctx->need_in_memory = 主/子请求有 filter 需要内存形态
        ctx->need_in_temp   = 有 filter 需要临时内存（如要改写）
        ctx->pool / bufs / tag / alignment       // 取自配置
        ctx->output_filter  = ngx_http_next_body_filter  // 下游
        ctx->filter_ctx     = r
  rc = ngx_output_chain(ctx, in)   // 真正的搬数据逻辑
  若 ctx->in 非空 → 标 NGX_HTTP_COPY_BUFFERED（还有没处理完的输入）
  否则清掉该标志
  return rc
```

`ngx_output_chain` 的内部规则（见 u2-l4）可简化为：

- 若入参 buf 已是内存形态，或下游能直接吃文件 buf（sendfile 且无人要求 `need_in_memory`）→ **直接透传**给 `ctx->output_filter`（即 next body filter）。
- 否则 → 把文件的一段读进一个临时内存 buf（用 `ctx->bufs` 配置的缓冲），再交给下游；读完一段、下游收一段，循环直到这个文件 buf 全部处理完。

这就解释了「为什么 gzip 开启时静态文件不再走 sendfile 零拷贝」：gzip 置了 `filter_need_in_memory`，copy_filter 因此被迫把文件读进内存供 gzip 压缩。

#### 4.3.3 源码精读

**整体函数**：

[src/http/ngx_http_copy_filter_module.c:82-158](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L82-L158) — `ngx_http_copy_filter` 全文。

**关键点①：首次进入时初始化上下文**

[src/http/ngx_http_copy_filter_module.c:96-123](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L96-L123) — `ctx` 没有就 `ngx_pcalloc` 一个，并填关键字段：

- `ctx->sendfile = c->sendfile`：连接级 sendfile 能力（在 worker/连接初始化时按 OS 与配置设定）。
- `ctx->need_in_memory = r->main_filter_need_in_memory || r->filter_need_in_memory`：任一上游 filter 声明「我要内存形态的数据」，就强制 copy 把文件读进内存。
- `ctx->need_in_temp = r->filter_need_temporary`：需要可写临时内存（filter 要改写内容时）。
- `ctx->output_filter = ngx_http_next_body_filter`、`ctx->filter_ctx = r`：把 copy 与下游 body 链衔接起来——`ngx_output_chain` 内部正是通过 `ctx->output_filter(ctx->filter_ctx, ...)` 调用 next body filter。
- `ctx->tag = &ngx_http_copy_filter_module`：u2-l4 讲过的 buf tag，用于按所有者回收 chain 节点。

**关键点②：把活交给 ngx_output_chain**

[src/http/ngx_http_copy_filter_module.c:145-152](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L145-L152) — `rc = ngx_output_chain(ctx, in);` 之后，依 `ctx->in`（还没处理完的输入链）是否为空，设置/清除 `NGX_HTTP_COPY_BUFFERED`。这个 buffered 位让请求主循环知道「copy 这层还囤着数据没吐给下游」，从而在写事件就绪时重新驱动它。

**关键点③：AIO / 线程池异步读文件**

[src/http/ngx_http_copy_filter_module.c:124-134](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L124-L134) — 当配置了 `aio on` 或 `aio threads`，ctx 会装上 `aio_handler` 或 `thread_handler`。这样 `ngx_output_chain` 读文件时走异步路径，读未完成时请求被挂起（`r->main->blocked++`、`r->aio=1`），完成后由 `ngx_http_copy_aio_event_handler` / `ngx_http_copy_thread_event_handler` 重新驱动 `r->write_event_handler`。这是 nginx 用线程池做大文件异步读、避免阻塞 worker 的关键接缝点。

**关键点④：copy 在 body 链中的位置（注册顺序）**

[src/http/ngx_http_copy_filter_module.c:389-396](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_copy_filter_module.c#L389-L396) — 两行 push。结合 `ngx_module_order`（copy 在 173 行，靠后），copy_filter 位于 body 链**靠近链头**的位置——content handler 吐出的 `in_file` buf 会很快到达 copy_filter，由它决定「透传」还是「读进内存」。这样所有「靠后于 copy」的、需要内存数据的 filter（gzip/charset/sub/…）拿到的就一定是内存 buf 了。

#### 4.3.4 代码实践

**实践目标**：观察 copy_filter 在「sendfile 开启」与「需要内存（gzip）」两种情况下的不同行为。

**操作步骤**：

1. 准备一个静态文件 `a.txt`（内容随便），debug 日志打开。
2. 配置一：
   ```nginx
   location /a { root /usr/share/nginx/html; sendfile on; }
   ```
   用 `curl --compress http://127.0.0.1/a/a.txt`（但客户端不声明 gzip，服务端也没开 gzip）拉取，grep 日志 `http copy filter:`。
3. 配置二（在同一 location 加 gzip）：
   ```nginx
   gzip on; gzip_types text/plain;
   ```
   再用 `curl --compress -H 'Accept-Encoding: gzip' ...` 拉取，再次 grep 日志。
4. 在 `nginx -V` 输出里确认 `--with-http_gzip_module`（默认就有）。

**需要观察的现象**：

- 配置一：由于无 filter 需要 `need_in_memory` 且 sendfile 可用，copy_filter 基本透传，文件内容不进用户态内存，走 sendfile 零拷贝。
- 配置二：gzip 把 `r->filter_need_in_memory` 置位，copy_filter 因此把文件读进内存 buf 再交给 gzip；日志里 `http copy filter:` 出现频次明显增多，对应「按块读取」。

**预期结果**：两次拉取都成功返回正确内容；日志差异体现在 copy_filter 的触发次数与是否伴随 `ngx_output_chain` 的「读文件」路径。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：copy_filter 与 `ngx_output_chain` 是什么关系？为什么 copy_filter 本身代码这么短？

> **参考答案**：copy_filter 是一个「适配层」：它负责把 HTTP 请求相关的参数（sendfile 能力、是否需要内存、pool、bufs、tag、下游 filter）填进一个通用的 `ngx_output_chain_ctx_t`，然后把真正的「文件↔内存搬移 + 背压」决策完全交给 u2-l4 讲过的通用框架 `ngx_output_chain`。所以 copy_filter 的主体只是「建上下文 + 调 `ngx_output_chain` + 维护 buffered 标志」，搬运逻辑被复用，避免重复造轮子。

**练习 2**：为什么开了 gzip 之后，静态文件传输就不再走 sendfile 零拷贝了？这个「开关」在源码哪里？

> **参考答案**：gzip filter 需要看到原始字节才能压缩，于是在请求上置 `r->filter_need_in_memory=1`。copy_filter 初始化时读这个标志到 `ctx->need_in_memory`（`ngx_http_copy_filter_module.c:110-111`），`ngx_output_chain` 据此**禁止透传文件 buf**、改为把文件读进内存 buf。sendfile 只能从文件直接发到 socket、不经过用户态，自然就无法让 gzip 插手，于是该路径被关掉。这是「功能性 filter」与「性能优化（零拷贝）」之间权衡的典型例子。

---

### 4.4 ngx_http_header_filter：header 链的终点，序列化响应头

#### 4.4.1 概念说明

`ngx_http_header_filter` 是 **header 链的终点**，与 write_filter 之于 body 链地位对称。它的职责单一而明确：把请求里**结构化**的响应头（`r->headers_out`：状态码、`Content-Type`、`Content-Length`、`Server`、`Date`、`Last-Modified`、用户自定义 headers 列表……）**序列化**成一段符合 HTTP/1.x 文本的字节流（`HTTP/1.1 200 OK\r\nServer: ...\r\n...\r\n\r\n`），装进一个 buf，然后调用 `ngx_http_write_filter` 把它发出去。

它的 init 同样只有一行（直接设为链头），因为它是 header 链最先注册、最终被压到链尾的终点。

#### 4.4.2 核心流程

```
ngx_http_header_filter(r):
  1. 若 r->header_sent 已置 → 直接返回（防重复，见下）
  2. r->header_sent = 1
  3. 子请求(r != r->main) / HTTP/0.9 → 不发头，返回 OK
  4. HEAD 方法 → 置 header_only（只发头不发体）
  5. 根据 r->headers_out.status 选状态行字符串（200→"200 OK" 等）
  6. 累加要分配的缓冲长度 len：状态行 + Server + Date + Content-Type
     + Content-Length + Last-Modified + Location + Connection + 各自定义头 …
  7. 分配一个临时 buf：ngx_create_temp_buf(r->pool, len)
  8. 按顺序往 buf 里 memcpy / sprintf 各字段，末尾追加 \r\n\r\n
  9. 若 header_only → 给 buf 置 last_buf（这是响应最后一个 buf）
 10. out.buf = b; return ngx_http_write_filter(r, &out);  ← 直接进 body 链终点
```

注意第 10 步：header_filter 作为 header 链终点，**不调 `next_header_filter`，而是直接调 `ngx_http_write_filter`**——也就是说响应头字节和响应体字节最终汇入同一个 write_filter、同一个 socket。

#### 4.4.3 源码精读

**整体函数**：

[src/http/ngx_http_header_filter_module.c:160-629](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L160-L629) — `ngx_http_header_filter` 全文。

**关键点①：防重复发送**

[src/http/ngx_http_header_filter_module.c:176-184](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L176-L184) — 先看 `r->header_sent`，已发就直接返回；否则置 1。子请求（`r != r->main`）不发自己的头（头由主请求统一发）。这与入口函数 `ngx_http_send_header` 里那段「header already sent」的 ALERT 检查形成双重保险（入口处防「content handler 重复调 send_header」，filter 内防「链上多次到达」）。

**关键点②：先算长度再一次性分配**

[src/http/ngx_http_header_filter_module.c:208-440](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L208-L440) — 这一大段全部是在**累加 `len`**：状态行长度、`Server`（按 `server_tokens` 决定是 `nginx` 还是带版本）、`Date`（用全局缓存时间 `ngx_cached_http_time`）、`Content-Type`（含可能的 `; charset=`）、`Content-Length`（仅当 `content_length_n >= 0`）、`Last-Modified`、`Location`（若是内部 `/` 路径且 `absolute_redirect` 则补成绝对 URL）、`Connection: keep-alive/close/upgrade`、可选的 `Transfer-Encoding: chunked`、`Vary: Accept-Encoding`，最后遍历 `r->headers_out.headers` 链表把每个 `hash != 0` 的自定义头也算上。**先算总长、再分配一次**，避免反复 realloc，这是 nginx 高性能编码的常见手法。

**关键点③：分配 buf 并填充**

[src/http/ngx_http_header_filter_module.c:442-446](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L442-L446) — `b = ngx_create_temp_buf(r->pool, len)`，随后用 `ngx_cpymem` / `ngx_copy` / `ngx_sprintf` 把前面算好长度的各字段逐段写入（`b->last` 不断前移）。状态行从 `ngx_http_status_lines[]` 表按状态码下标取出（表见 `:58-136`）。

**关键点④：收尾，交给 write_filter**

[src/http/ngx_http_header_filter_module.c:616-628](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L616-L628) — 追加最后的 `\r\n`（空行表示头结束）；记录 `r->header_size`；若 `header_only` 给 buf 置 `last_buf`；构造单节点 `out` 链，`return ngx_http_write_filter(r, &out)`。这一行印证了「header 链终点直通 body 链终点」。

**关键点⑤：header_filter 是终点，init 不存 next**

[src/http/ngx_http_header_filter_module.c:734-741](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_header_filter_module.c#L734-L741) — `ngx_http_top_header_filter = ngx_http_header_filter;`，同样只有一行，没有「next = top」。它顺便把 `ngx_http_top_early_hints_filter` 也设上（103 Early Hints 响应的专用链终点）。

#### 4.4.4 代码实践

**实践目标**：用 debug 日志验证 header_filter 产出的响应头字节，并与 `curl -i` 看到的实际响应头逐行对照。

**操作步骤**：

1. debug 编译，`error_log ... debug_http;`。
2. 配置一个静态 location，`server_tokens off;`。
3. `curl -i http://127.0.0.1/` 抓取响应头。
4. 在日志里 grep `NGX_LOG_DEBUG_HTTP` 且由 header_filter 打印的那条（源码 `:613-614` 用 `%*s` 打印了整段头）。

**需要观察的现象**：日志里能看到完整序列化后的响应头文本（`HTTP/1.1 200 OK\r\nServer: nginx\r\nDate: ...\r\n...`），与 `curl -i` 的输出一致；`Server` 因 `server_tokens off` 显示为 `nginx`（不带版本）。

**预期结果**：响应头顺序大致为「状态行 → Server → Date → Content-Type → Content-Length → Last-Modified → Connection → 自定义头 → 空行」，与源码填充顺序吻合。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：header_filter 为什么先花一两百行累加 `len`，而不是边写边扩容？

> **参考答案**：响应头是一段连续文本，一次性分配能保证内存池里是一块连续内存，写入时只需 `b->last` 前移、零额外分配、零拷贝。若边写边扩容，要么频繁 realloc（nginx 内存池小块不支持高效 realloc），要么用 chain 拼接（多一次节点分配与指针维护）。先算总长是「以一段纯计算换一次精确分配」的经典取舍。

**练习 2**：`r->header_sent` 与 `ngx_http_send_header` 里的「header already sent」检查，二者各自防的是什么？

> **参考答案**：`ngx_http_send_header` 里的检查防的是**调用方错误**——content handler 不该对同一请求调用两次 send_header，若发生则打 ALERT；header_filter 内 `r->header_sent` 检查防的是**链路重复**——比如内部重定向或特殊响应流程可能让 header 链被多次进入，此时静默返回 `NGX_OK`，保证响应头只发送一次。两者一硬（报错）一软（幂等），共同守住「头只发一次」这条不变量。

---

### 4.5 ngx_http_postpone_filter：为子请求排序输出

#### 4.5.1 概念说明

nginx 支持**子请求（subrequest）**：一个请求可以在处理过程中派生子请求（如 SSI `include`、`addition` 模块追加前后内容）。子请求的响应必须**按正确顺序**拼到客户端：通常父请求的输出要等到它所有子请求输出之后才能发，否则顺序会乱。

`ngx_http_postpone_filter` 就是负责这件事的 body filter。它夹在 body 链里（位置在 `ngx_module_order` 第 163 行，比 copy 靠前、比 chunked 靠后），用请求上的 `r->postponed` 链表暂存「还没轮到的」输出，并用 `c->data` 指针记录「当前真正在往 socket 发的那个请求」，从而把多个请求的输出按树形顺序串行化。

#### 4.5.2 核心流程

```
postpone_filter(r, in):
  若 r 是「内存子请求」(subrequest_in_memory) → 把输出收进内存返回
  若 r != c->data（当前活跃请求不是我）:
     —— 把 in 暂存进 r->postponed 链表（add），直接返回 OK
        （我还没轮到，先存着，等轮到我时再发）
  否则（我就是当前活跃请求）:
     若我没有 postponed 待办:
        —— 直接把 in 透传给 next body filter（以 r->main 的身份）
     否则:
        —— 把 in 也 add 进 postponed
        —— 循环处理 postponed 队列：若是「待唤醒的子请求」则切换 c->data 到它
           并 post 它重新跑；若是「暂存的输出」则发给 next filter
```

核心是用 `c->data`（连接当前服务的请求）与 `r->postponed`（暂存链）两个状态，把「谁先发、谁后发」编排清楚。没有子请求时，postpone_filter 几乎透明：`r->postponed == NULL` 且 `r == c->data`，直接 `ngx_http_next_body_filter(r->main, in)` 透传。

#### 4.5.3 源码精读

**整体函数**：

[src/http/ngx_http_postpone_filter_module.c:54-138](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L54-L138) — `ngx_http_postpone_filter` 全文。

**关键点①：无子请求时的快路径**

[src/http/ngx_http_postpone_filter_module.c:88-95](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L88-L95) — `r->postponed == NULL` 时，若有数据或连接有缓冲，就 `ngx_http_next_body_filter(r->main, in)` 透传。注意传的是 `r->main`（主请求）——所有子请求的输出最终都以主请求身份流向后续 filter，保证下游看到的是统一的请求上下文。

**关键点②：暂存未轮到的输出**

[src/http/ngx_http_postpone_filter_module.c:69-86](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L69-L86) — 当 `r != c->data`（当前活跃请求是别人），说明「我」的输出不该现在发，调 `ngx_http_postpone_filter_add` 把 in 挂到 `r->postponed` 链尾，返回 OK。

**关键点③：唤醒子请求**

[src/http/ngx_http_postpone_filter_module.c:103-117](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L103-L117) — 遍历 `r->postponed`，若某项是「待唤醒的子请求」（`pr->request` 非空），就把 `c->data` 切到该子请求，并 `ngx_http_post_request` 让它重新跑。这就是「先发子请求、再发父请求」的实现：父请求在 postpone 这一层把控制权让给子请求。

**关键点④：注册位置**

[src/http/ngx_http_postpone_filter_module.c:252-259](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L252-L259) — 两行 push。它有自己独立的 `static ngx_http_next_body_filter`（`src/http/ngx_http_postpone_filter_module.c:51`），与 copy_filter 的同名 static 互不相干。

#### 4.5.4 代码实践

**实践目标**：阅读 `ngx_http_postpone_filter_add` 与主函数，弄清「父请求输出被暂存、子请求被优先唤醒」的状态切换。

**操作步骤**：

1. 阅读 [src/http/ngx_http_postpone_filter_module.c:141-177](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_postpone_filter_module.c#L141-L177)（`postpone_filter_add`）与 `:103-117` 的唤醒循环。
2. 画一张状态图：`c->data` 在「父请求 A → 子请求 B → 父请求 A」之间如何切换；`A->postponed` 链表如何随着 B 完成、A 续发而变化。
3. （可选）配置 `ngx_http_addition_module`（`add_before_body` / `add_after_body`）触发子请求，用 debug 日志 grep `http postpone filter` 观察实际切换。

**需要观察的现象**：日志里会看到 postpone filter 在父请求与子请求之间来回切换 `c->data`，最终输出顺序是「子请求内容 → 父请求内容」或 addition 配置的顺序。

**预期结果**：客户端收到的响应体是按子请求在前/后正确拼接的完整内容。**待本地验证**（addition 模块需 `--with-http_addition_module`，默认未开）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 postpone_filter 透传时用 `r->main` 而不是 `r`？

> **参考答案**：后续 filter（copy、write 等）以及连接本身（`c->data`、`c->sent`、限速状态）都是以**主请求**为锚点的。子请求只是逻辑上的派生，真正占用网络连接、维护发送进度的是主请求。所以所有子请求的输出都必须「以主请求身份」流向下游，否则各 filter 的请求上下文会错乱。`r->main` 就是这条连接的根请求。

**练习 2**：如果完全没有子请求，postpone_filter 几乎不做任何加工，为什么 nginx 还要把它编进默认链？

> **参考答案**：因为「是否有子请求」要到运行时才知道（SSI、addition、内部 subrequest 都可能在配置里启用），而 filter 链在配置加载后就固定了。postpone_filter 是「为可能的子请求预留的编排层」：无子请求时走 `r->postponed == NULL` 的快路径，开销极小（一次指针判断 + 一次透传）；一旦出现子请求，它立即承担起排序职责。这是「用很小的常驻开销换取能力完备性」的设计。

---

## 5. 综合实践

**任务**：画出一次「GET 静态文件」响应从 content handler 到最终写 socket 经过的**所有 filter 节点顺序**，并说明每个节点的职责。

**背景信息**：静态文件 content handler（`ngx_http_static_handler`）在末尾做两件事——

[src/http/modules/ngx_http_static_module.c:255](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L255) 调 `ngx_http_send_header(r)`（触发 header 链）；

[src/http/modules/ngx_http_static_module.c:264](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L264) 把 buf 标记为 `in_file`（响应体是一个文件 buf）；

[src/http/modules/ngx_http_static_module.c:277](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L277) 调 `ngx_http_output_filter(r, &out)`（触发 body 链）。

**操作步骤**：

1. 用 `nginx -V` 记录本次构建启用了哪些 filter 模块（重点看是否有 `gzip`、`charset`、`ssi`、`sub`、`addition`、`slice`、`v2`、`v3` 等）。
2. 对照 `auto/modules` 的 `ngx_module_order`（`:148-176`），按第 4.1 节的栈式 push 方法，分别推出**本次构建实际的 header 链**与**body 链**调用顺序。
3. 为链上每个节点写一句话职责。

**参考答案（以「默认开启 gzip、charset、ssi，HTTP/1.1，无 v2/v3/slice/sub/addition/image/xslt」的典型构建为例）**：

Header 链（content handler 调 `ngx_http_send_header` → `top_header_filter`）：

```
not_modified_filter   检查 If-Modified-Since/If-None-Match，命中则改 304
        ↓
headers_filter        应用 add_header / expires / 设置 trailers
        ↓
charset_filter        添加/处理 Content-Type 的 charset
        ↓
ssi_filter(若启用)    SSI 相关头部处理
        ↓
gzip_filter           加 Vary: Accept-Encoding，必要时调整 Content-Encoding
        ↓
range_header_filter   处理 Range 请求头
        ↓
chunked_filter        若是 chunked 则不在此发，仅置 r->chunked
        ↓
header_filter  ★终点  把 headers_out 序列化成 HTTP/1.1 头字节
        ↓
ngx_http_write_filter → send_chain → socket   （头字节先发出去）
```

Body 链（content handler 调 `ngx_http_output_filter` → `top_body_filter`），此时入参是 `in_file` 的 buf：

```
range_body_filter     若是 Range 请求，截取对应字节范围
        ↓
copy_filter   ★关键   in_file buf → 由 ngx_output_chain 决定：
                      sendfile 可用且无人需要内存 → 透传文件 buf
                      否则 → 把文件按块读进内存 buf
        ↓
headers(trailers)_filter  处理 chunked trailers
        ↓
ssi_filter(若启用)    对内存 buf 做 SSI 包含
        ↓
charset_filter        对内存 buf 做字符集转码
        ↓
postpone_filter       无子请求 → 以 r->main 透传
        ↓
gzip_filter           若 Accept-Encoding: gzip → 压缩（此时数据已在内存）
        ↓
chunked_filter        若 r->chunked → 给每段加 chunk 长度前缀
        ↓
write_filter  ★终点   攒包 + 限速 + 调 c->send_chain → socket
```

**职责小结**：

| filter | 职责 |
| --- | --- |
| not_modified | 条件请求→304 |
| headers / addition | 追加自定义响应头、expires、trailer |
| charset / ssi / sub | 改写内存中的响应体内容 |
| gzip / gunzip | 压缩/解压（需要内存数据，故在 copy 之后才生效） |
| range | 字节范围 |
| chunked | 分块编码封装 |
| **copy** | 文件 buf ↔ 内存 buf 的桥接（驱动 `ngx_output_chain`） |
| **postpone** | 子请求输出排序（无子请求则透明） |
| **write** | 终点：攒包、限速、真正写 socket |
| **header** | 终点：序列化响应头 |

**关键洞察**：

- 静态文件的 body 一开始是「文件 buf」，先经过 range、再到达 copy。copy 之前的 filter（range）可以基于偏移工作，不必读内容；copy 之后的 filter（gzip/charset/sub）需要看到字节内容，所以 copy 必须在它们之前完成「文件→内存」的转换——这正是 copy_filter 在 `ngx_module_order` 中排在它们后面的原因。
- header_filter 与 write_filter 是两条链各自的终点，但 header_filter 产出的头字节最终也流进 write_filter，与体字节走同一个 socket 发送。

**待本地验证**：你的实际链取决于 `nginx -V` 的输出；若启用了 v2/v3，链头会被 v2/v3 filter 替换（HTTP/2/3 不走 header_filter 序列化，而是自己组帧）。

## 6. 本讲小结

- nginx 有**两条对称的过滤器链**：header filter（`ngx_http_send_header` 进入）与 body filter（`ngx_http_output_filter` 进入），各自由一个全局链头指针 `ngx_http_top_header_filter` / `ngx_http_top_body_filter` 标识入口。
- 链靠「**全局链头 + 每个 filter 私有的 static next 指针**」隐式串联；filter 在 `postconfiguration` 里用「`next = top; top = self`」两行把自身 push 进链，因而是**栈式注册、注册顺序与调用顺序相反**，整体顺序由 `auto/modules` 的 `ngx_module_order` 决定，由 `ngx_http_block` 末尾的 postconfiguration 循环（`ngx_http.c:303-315`）落实。
- `ngx_http_write_filter` 是 **body 链终点**，负责攒包（`postpone_output`）、限速（`limit_rate`）和真正调 `c->send_chain` 写 socket，未发完则置 `NGX_HTTP_WRITE_BUFFERED` 返回 `NGX_AGAIN`，是背压的源头。
- `ngx_http_copy_filter` 是文件 buf 与内存 buf 的**桥接器**，核心是把上下文交给通用框架 `ngx_output_chain`；当下游有 filter 声明 `need_in_memory`（如 gzip）时，它被迫把文件读进内存，从而关掉 sendfile 零拷贝路径。
- `ngx_http_header_filter` 是 **header 链终点**，把结构化的 `r->headers_out` 先算总长、再一次性序列化成 HTTP/1.x 头字节，最后直接交给 write_filter 发出。
- `ngx_http_postpone_filter` 用 `c->data` 与 `r->postponed` 为子请求排序输出；无子请求时走快路径几乎透明。

## 7. 下一步学习建议

- **u6-l7 变量系统**：`ngx_http_header_filter` 与 `add_header`（headers_filter）大量依赖变量（`$server_name`、`$msec` 等），下一讲讲变量如何注册与惰性求值。
- **u7-l1 upstream 框架**：反向代理的响应体来自后端，会经过同一套 body filter 链；理解了 copy/write/postpone，再看 upstream 的 event_pipe（u7-l5）就顺理成章。
- **u8-l2 / u8-l3 HTTP/2、HTTP/3**：它们用各自的 `v2/v3_filter` 替换链头，不走 header_filter 的文本序列化，而是直接组二进制帧——这是理解「为什么 HTTP/2 下 add_header 行为略有不同」的钥匙。
- **u10-l4 自定义模块实战**：本讲的「两行 push」正是自定义 body/header filter 模块的注册套路，写一个返回固定 JSON 的 filter 会让你彻底吃透这套机制。
