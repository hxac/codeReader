# 静态文件 content handler

## 1. 本讲目标

本讲以 nginx 自带的 `ngx_http_static_module` 为样本，讲清楚一个「静态文件服务」是如何作为 HTTP 请求处理流程的**终点**工作的。学完后你应当能够：

- 说清楚「content handler」在 11 个 phase 中的定位，以及它和 `proxy_pass` 这类模块的区别。
- 顺着 `ngx_http_static_handler` 一条主链，讲清楚请求 URI 是怎么变成磁盘文件路径、文件是怎么被打开并发给客户端的。
- 理解 `open_file_cache` 如何把昂贵的 `open()` + `stat()` 结果缓存起来，从而扛住海量静态请求。
- 解释 404、403、301 这些「特殊响应」是 nginx 主动生成的，并能定位到生成它们的源码函数。

本讲承接 u6-l4（phases 机制）和 u6-l6（过滤器链）。你将看到：CONTENT 阶段的「处理者」最终会把数据交给 u6-l6 讲过的 `ngx_http_output_filter`，从而走完整个过滤器链写出到 socket。

## 2. 前置知识

在进入源码前，先建立三点直觉。

**第一，HTTP 请求是一条流水线，content handler 是流水线最末端「真正干活」的人。**
回忆 u6-l4：一个请求要顺序穿过 11 个阶段（POST_READ → … → ACCESS → PRECONTENT → CONTENT → LOG）。前面那些阶段大多做的是「鉴权、改写、选 location」等准备工作，它们的 handler 通常返回 `NGX_DECLINED` 表示「我不处理，交给下一个」。只有走到 **CONTENT** 阶段，才真正有人把响应体生产出来——要么是静态文件模块读磁盘，要么是 `proxy_pass` 把请求转给后端。本讲的主角 `ngx_http_static_module` 就是 CONTENT 阶段里「读磁盘文件发出去」的那个处理者。

**第二，「静态文件」为什么需要单独讲？**
因为「把一个文件发给客户端」这件事听起来简单，真正做对、做快却涉及很多细节：URI 怎么映射成磁盘路径？文件不存在返回什么？文件其实是个目录怎么办？是普通文件还是设备文件？要不要支持断点续传（Range）？怎么知道文件被改过、要不要重新打开？这些问题在 `ngx_http_static_handler` 里都有对应的源码分支。

**第三，每次请求都 `open()` 一次文件太贵。**
Linux 上 `open()` + `fstat()` 两次系统调用，在每秒几万次静态请求的场景下是巨大开销。nginx 的解法是 `open_file_cache`：把「打开的 fd + 文件元信息（大小、修改时间、是否目录等）」连同**错误结果**（比如「这个文件不存在」）一起缓存，缓存命中时连一次 `open()` 都不用，直接复用上次的 fd。这是静态服务高性能的关键之一。

> 名词速查：`fd`（file descriptor，文件描述符，Linux 对打开文件的整数句柄）；`stat`（获取文件元信息的系统调用，nginx 里包装为 `ngx_file_info`）；`Content-Type`（响应头，告诉客户端文件类型，如 `text/html`）；`ETag`（响应头，文件版本的指纹，用于客户端缓存校验）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/http/modules/ngx_http_static_module.c` | 本讲主角。整个文件只有一个有效函数 `ngx_http_static_handler`（外加一个把自身注册进 CONTENT 阶段的 `ngx_http_static_init`）。 |
| `src/core/ngx_open_file_cache.c` / `.h` | open_file_cache 的实现。负责缓存「打开文件 + stat 结果」，被 static、index、gzip 等多个模块共用。 |
| `src/http/ngx_http_special_response.c` | 「特殊响应」生成器。404/403/500 等错误页、`error_page` 指令的内部重定向都在这里。 |
| `src/http/ngx_http_core_module.c` | 提供 `ngx_http_map_uri_to_path`（URI→磁盘路径映射）、`ngx_http_set_content_type`（类型推断）、以及 CONTENT 阶段的调度函数 `ngx_http_core_content_phase`。 |

## 4. 核心概念与源码讲解

### 4.1 content handler：CONTENT 阶段的「处理者」

#### 4.1.1 概念说明

u6-l4 讲过，CONTENT 阶段与其它阶段有一个根本区别：**它有「专属处理者」机制**。

普通阶段（如 ACCESS）里挂的所有 handler 是「轮流尝试」的：每个都跑一遍，谁返回 `NGX_DECLINED` 就换下一个。但 CONTENT 阶段不同——如果在 FIND_CONFIG 阶段选中的 location 上配置了 `clcf->handler`（比如某个模块通过指令注册了自己的内容处理函数），那么 `r->content_handler` 就会被设成它，CONTENT 阶段**只跑这一个**，跑完就结束请求。

如果 location 上没有专属 handler（即最朴素的 `location / { root /html; }`），那么 CONTENT 阶段就退化成「轮流尝试」模式：static、index、autoindex、try_files、dav 等候选模块依次试，谁返回非 `NGX_DECLINED` 谁就接管。`ngx_http_static_module` 正是这批「候选者」之一，且通常是第一个被尝试的。

#### 4.1.2 核心流程

CONTENT 阶段的调度函数 `ngx_http_core_content_phase` 做两件事：

1. 若 `r->content_handler` 非空，调用它并 `finalize`（结束请求）。
2. 否则取出当前 phase 的 handler 调用；若返回 `NGX_DECLINED` 则推进到下一个候选 handler。

#### 4.1.3 源码精读

CONTENT 阶段调度逻辑：[src/http/ngx_http_core_module.c:1292-1322](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1292-L1322)

```c
if (r->content_handler) {
    r->write_event_handler = ngx_http_request_empty_handler;
    ngx_http_finalize_request(r, r->content_handler(r));
    return NGX_OK;
}
...
rc = ph->handler(r);          // 轮流尝试 static / index / ...
if (rc != NGX_DECLINED) {
    ngx_http_finalize_request(r, rc);
    return NGX_OK;
}
/* rc == NGX_DECLINED  -> ph++; 推进到下一个候选 handler */
```

`r->content_handler` 的赋值发生在 FIND_CONFIG 阶段，由 `ngx_http_update_location_config` 完成：[src/http/ngx_http_core_module.c:1421-1423](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1421-L1423)

```c
if (clcf->handler) {
    r->content_handler = clcf->handler;
}
```

而 static 模块把自己挂进「轮流候选名单」是在配置后处理阶段，由 `ngx_http_static_init` 完成：[src/http/modules/ngx_http_static_module.c:281-297](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L281-L297)

```c
h = ngx_array_push(&cmcf->phases[NGX_HTTP_CONTENT_PHASE].handlers);
*h = ngx_http_static_handler;
```

这正是 u6-l4 讲过的「模块在 `postconfiguration` 钩子里把自己的 handler push 进某 phase 的 handlers 数组」的标准手法。

#### 4.1.4 代码实践

**实践目标**：确认 static 模块确实是 CONTENT 阶段的候选 handler，且在没有专属 content_handler 时才会被调用。

**操作步骤**：

1. 在 `src/http/modules/ngx_http_static_module.c:281` 的 `ngx_http_static_init` 处打上心智断点——它是模块注册点。
2. 写一个最小 `nginx.conf`：

   ```nginx
   events {}
   http {
       server {
           listen 8080;
           location / { root html; }
       }
   }
   ```

   注意这里 `location /` 没有任何会设 `clcf->handler` 的指令（没有 `proxy_pass`、没有 `stub_status`），因此 `r->content_handler` 为 NULL，CONTENT 阶段会进入「轮流尝试」分支，static 模块会被调用。

3. 用 `curl http://127.0.0.1:8080/index.html` 验证能拿到 html 目录下的 `index.html`。

**需要观察的现象**：当请求落到 `location /` 时，static 模块接管并返回文件内容；如果把 `location /` 改成 `location / { proxy_pass http://backend; }`，则 `proxy_pass` 会设置 `clcf->handler`，`r->content_handler` 非空，static 模块**根本不会被调用**（CONTENT 阶段只跑专属 handler）。

**预期结果**：能口述「专属 content_handler 优先；没有时才轮流尝试 static/index 等」。

#### 4.1.5 小练习与答案

**练习 1**：为什么把 `proxy_pass` 和静态文件放在同一个 location 里，永远只会看到代理结果而看不到静态文件？
**答案**：`proxy_pass` 指令解析时会设置 `clcf->handler = ngx_http_proxy_handler`，于是 FIND_CONFIG 阶段把 `r->content_handler` 设成它。CONTENT 阶段调度函数（4.1.3 第一段代码）一旦发现 `r->content_handler` 非空就只跑它，不再轮流尝试 static 等候选 handler。

**练习 2**：static 模块在哪一步把自己注册进 CONTENT 阶段？
**答案**：在配置解析完成后的 `postconfiguration` 钩子 `ngx_http_static_init`（`ngx_http_static_module.c:281`）里，把 `ngx_http_static_handler` push 进 `cmcf->phases[NGX_HTTP_CONTENT_PHASE].handlers` 数组。

---

### 4.2 ngx_http_static_handler 的完整流程

#### 4.2.1 概念说明

`ngx_http_static_handler` 是本讲的核心。它接收一个已经准备好（头部已解析、location 已选定）的请求 `r`，要回答一个问题：**这个 URI 对应的文件在磁盘上是什么，怎么发给客户端？**

整个函数是一个**同步、线性、无回调**的过程——这很关键。它不需要异步等待（不像 `proxy_pass` 要等后端响应），因为读磁盘元信息是一次 `open()+stat()`，足够快；而真正大块的数据发送交给 u6-l6 的过滤器链（`write_filter` 会处理 `NGX_AGAIN` 背压）。所以 static handler 自己不挂起，一次跑完就返回。

#### 4.2.2 核心流程

把 handler 拆成 7 个有序步骤：

```text
1. 方法白名单：只认 GET/HEAD/POST，否则 405
2. URI 以 '/' 结尾？ -> 返回 NGX_DECLINED（交给 index 模块）
3. URI -> 磁盘路径：ngx_http_map_uri_to_path()
4. 打开文件（带缓存）：ngx_open_cached_file()
   ├── 失败：按 errno 映射成 404/403/500
   └── 成功：拿到 fd + size + mtime + is_dir 等
5. 是目录？ -> 设置 Location 头，返回 301（让客户端补斜杠重试）
6. 不是普通文件？ -> 404（避免暴露设备/管道文件）
   POST 请求？ -> 405
7. 组装响应头（200、Content-Length、Last-Modified、ETag、Content-Type）
   组装一个 in_file 的 buf -> ngx_http_output_filter() 送入过滤器链
```

#### 4.2.3 源码精读

整个函数：[src/http/modules/ngx_http_static_module.c:48-278](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L48-L278)

**步骤 1，方法白名单**：[src/http/modules/ngx_http_static_module.c:63-69](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L63-L69)

```c
if (!(r->method & (NGX_HTTP_GET|NGX_HTTP_HEAD|NGX_HTTP_POST))) {
    return NGX_HTTP_NOT_ALLOWED;          // 405
}

if (r->uri.data[r->uri.len - 1] == '/') {
    return NGX_DECLINED;                  // 交给 index 模块处理目录
}
```

注意第二条：URI 以 `/` 结尾说明请求的是目录（如 `/`），static 不处理目录，返回 `NGX_DECLINED` 让 CONTENT 阶段的下一个候选（index 模块）去试。这是 4.1 讲的「轮流尝试」机制的体现。

**步骤 3，URI → 磁盘路径**：[src/http/modules/ngx_http_static_module.c:78-83](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L78-L83)

```c
last = ngx_http_map_uri_to_path(r, &path, &root, 0);
```

`ngx_http_map_uri_to_path` 是映射核心。它取出当前 location 的 `clcf->root`（如 `/usr/share/nginx/html`），拼接上 URI 去掉 location 前缀后的部分，得到完整磁盘路径。它还处理 `root` 与 `alias` 的差异、含变量的 root（用脚本引擎 `ngx_http_script_run` 求值）。映射主逻辑见 [src/http/ngx_http_core_module.c:1947-1977](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1947-L1977)：

```c
path->len = clcf->root.len + reserved + r->uri.len - alias + 1;
path->data = ngx_pnalloc(r->pool, path->len);
last = ngx_copy(path->data, clcf->root.data, clcf->root.len);
```

`alias` 的作用是「替换」而非「拼接」location 前缀：`root` 模式下 URI 完整保留在路径里，`alias` 模式下 location 匹配部分被替换掉。多出来的 `+1` 字节是为了存放结尾的 `'\0'`（C 字符串，给 `open()` 用）以及可能的补斜杠（见步骤 5）。

**步骤 4，带缓存地打开文件**：[src/http/modules/ngx_http_static_module.c:103-142](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L103-L142)

```c
if (ngx_open_cached_file(clcf->open_file_cache, &path, &of, r->pool)
    != NGX_OK)
{
    switch (of.err) {
    case NGX_ENOENT:  ... rc = NGX_HTTP_NOT_FOUND;       break;  // 404
    case NGX_EACCES:  ... rc = NGX_HTTP_FORBIDDEN;       break;  // 403
    default:          ... rc = NGX_HTTP_INTERNAL_SERVER_ERROR;   // 500
    }
    return rc;
}
```

`ngx_open_cached_file` 是 open_file_cache 的入口（详见 4.3）。它返回后，无论成败，结果都填进 `ngx_open_file_info_t of` 这个「输出参数」结构体：成功时 `of.fd/of.size/of.mtime/of.is_dir` 有效，失败时 `of.err` 有效。这里直接用 `of.err`（即 `errno`）把底层错误翻译成 HTTP 状态码——`ENOENT`（文件不存在）→ 404，`EACCES`（权限不足）→ 403，其它 → 500。

**步骤 5，目录的 301 重定向**：[src/http/modules/ngx_http_static_module.c:148-204](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L148-L204)

如果 `of.is_dir` 为真（比如请求 `/foo`，磁盘上 `/html/foo` 是个目录），nginx 不返回文件，而是构造一个 `Location: /foo/` 响应头，返回 **301 Moved Permanently**，让浏览器补上斜杠重新请求 `/foo/`——这一次才会走到 4.2.3 步骤 2 的 `DECLINED` 分支交给 index 模块找 `index.html`。

**步骤 6，普通文件检查 + POST 拒绝**：[src/http/modules/ngx_http_static_module.c:208-219](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L208-L219)

```c
if (!of.is_file) {           // 设备文件、命名管道等
    return NGX_HTTP_NOT_FOUND;
}
if (r->method == NGX_HTTP_POST) {
    return NGX_HTTP_NOT_ALLOWED;   // 405：POST 不能用于读静态文件
}
```

第一步防止把 `/dev/null` 这类特殊文件当静态资源返回；第二步对 POST 返回 405。注意方法白名单在步骤 1 允许了 POST 进来，但到这儿又拒绝——因为 nginx 的设计是：POST 到静态文件是语义错误（静态文件不支持写入），但 405 必须在确认目标确实是文件之后才返回（否则目录的 POST 应该走 301）。

**步骤 7，组装响应头与 body buf**：[src/http/modules/ngx_http_static_module.c:229-277](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L229-L277)

```c
r->headers_out.status = NGX_HTTP_OK;
r->headers_out.content_length_n = of.size;
r->headers_out.last_modified_time = of.mtime;

ngx_http_set_etag(r);            // 用 mtime+size 生成 ETag
ngx_http_set_content_type(r);    // 按扩展名推断 Content-Type（types{}）
r->allow_ranges = 1;             // 允许 Range 请求（断点续传）

b = ngx_calloc_buf(r->pool);
b->file = ngx_pcalloc(r->pool, sizeof(ngx_file_t));
...
b->file_pos = 0;
b->file_last = of.size;
b->in_file = b->file_last ? 1 : 0;     // 关键：数据来自文件而非内存
b->last_buf = (r == r->main) ? 1 : 0;  // 是否链表末尾
b->file->fd = of.fd;

out.buf = b;
out.next = NULL;
return ngx_http_output_filter(r, &out);   // 交给过滤器链（见 u6-l6）
```

这里出现了 u2-l4 讲过的 `ngx_buf_t`：它用 `in_file=1` 声明「这块数据在磁盘上」，只记 `fd` 和 `[file_pos, file_last)` 区间，**不把文件内容读进内存**。真正的磁盘→网络搬运由过滤器链里的 `copy_filter` → `write_filter` → `ngx_linux_sendfile_chain`（见 u4-l4）通过 `sendfile()` 零拷贝完成。这就是静态文件服务的「快路径」。

`ngx_http_set_content_type` 会根据请求 URI 的扩展名（如 `.html`）在 location 的 `types {}` 映射表里查到 `text/html` 写进响应头，定义在 [src/http/ngx_http_core_module.c:1635](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_core_module.c#L1635)。

最后 `ngx_http_output_filter` 是过滤器链入口（u6-l6 讲过），它最终走到 `write_filter` 真正写 socket。

#### 4.2.4 代码实践

**实践目标**：用源码追踪一次 `GET /index.html`，验证「URI → 磁盘路径 → 打开文件 → in_file buf → 过滤器链」这条主链。

**操作步骤**：

1. 准备测试文件：在 nginx 的 `html/` 目录下放一个 `index.html`，内容随意。
2. 配置 4.1.4 里的 `nginx.conf`，确保 `location /` 没有 `proxy_pass`。
3. 用 `--with-debug` 编译 nginx（构建方式见 u1-l2），并配置：

   ```nginx
   error_log logs/error.log debug_http;
   ```

4. 启动 nginx，执行 `curl -v http://127.0.0.1:8080/index.html`。
5. 在 `logs/error.log` 里搜索以下 debug 关键字，按出现顺序对应步骤：
   - `http filename:` —— 对应步骤 3，打印 `ngx_http_map_uri_to_path` 算出的磁盘路径。
   - `http static fd:` —— 对应步骤 4，`ngx_open_cached_file` 成功后打印的文件描述符。
   - `http dir` —— 仅当目标是目录时出现（可改请求 `/` 触发，但会先 DECLINED）。

**需要观察的现象**：

- debug 日志里 `http filename:` 应为绝对路径，如 `/usr/share/nginx/html/index.html`。
- 响应头应包含 `Content-Type: text/html`、`Content-Length`、`Last-Modified`、`ETag`、`Accept-Ranges: bytes`。
- 若把 `index.html` 改名为 `index.html.bak`，再次请求 `/index.html`，日志会出现 `open() ".../index.html" failed (2: No such file or directory)`，最终响应为 404——对应步骤 4 的 `NGX_ENOENT` 分支。

**预期结果**：能画出「URI → path → of.fd → in_file buf → output_filter」这条链，并能解释每个 HTTP 响应头分别是哪一行代码设的。

> 待本地验证：debug 关键字的确切大小写与格式以你编译出的版本日志为准；本实践未替你运行命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 static handler 允许 POST 进入（步骤 1），后面又对 POST 返回 405（步骤 6）？
**答案**：方法白名单先粗筛掉 PUT/DELETE 等无关方法；但 405 必须在确认目标「确实是文件」之后再返回——如果目标是目录，POST 应当先走 301 补斜杠，而不是直接 405。所以步骤 1 放行 POST，步骤 5 处理目录，步骤 6 确认是文件后才对 POST 返回 405。

**练习 2**：`b->in_file = 1` 之后，文件内容什么时候被真正读出来？
**答案**：在 static handler 里**不读**。它只把 `fd` 和区间记进 buf，然后 `ngx_http_output_filter` 把 buf 推进过滤器链。最终是过滤器链里的 `write_filter` 调用连接的 `send_chain`（Linux 上即 `ngx_linux_sendfile_chain`），用 `sendfile()` 系统调用把文件数据从内核页缓存直接送到 socket，全程不经过用户态内存。

---

### 4.3 open_file_cache：把 open()+stat() 缓存起来

#### 4.3.1 概念说明

`ngx_open_cached_file` 是 static、index、gzip_static 等多个模块共用的「打开文件」基础设施。它的核心思想：**把「文件名」到「打开结果」的映射缓存起来**，缓存的不只是成功的 fd，还包括 stat 元信息，甚至**失败的结果**（如「这个文件不存在」），因为 404 同样昂贵——不缓存的话每次不存在的请求都要 `open()` 一次。

缓存项 `ngx_cached_open_file_t` 用**红黑树**按文件名（的 crc32 哈希）组织以支持快速查找，同时挂在一条 **LRU 队列**（`expire_queue`）上以支持按「最近未使用时间」淘汰。这是 u2-l3 讲过的「红黑树 + 队列」组合的经典用法。

#### 4.3.2 核心流程

`ngx_open_cached_file` 的判定流程：

```text
入参：cache（缓存池，可空）、name（文件名）、of（输入期望+输出结果）
若 cache == NULL：
    直接 ngx_open_and_stat_file() 真打开，不缓存（用于未配 open_file_cache 的场景）
否则：
    hash = crc32(name)
    file = ngx_open_file_lookup(cache, name, hash)   # 红黑树查找
    if 命中 且 (未过期 且 uniq 没变)：
        直接把 file 里的 fd/size/mtime/... 复制进 of   # 零次 open()
        goto found
    if 命中 但可能过期：
        重新 ngx_open_and_stat_file() 校验，按需更新
    if 未命中：
        新建缓存项，ngx_open_and_stat_file() 真打开，插入红黑树+队列
```

真正「打开文件 + 取 stat」的底层函数是 `ngx_open_and_stat_file`：先尝试用 `fstat` 复用已开 fd（`of.uniq` 没变就跳过），否则 `open()` + `fstat()`，并处理目录（开了立刻 close，因为目录不需要 fd）、read-ahead、directio 等细节。

#### 4.3.3 源码精读

**缓存入口 `ngx_open_cached_file`**：[src/core/ngx_open_file_cache.c:143-210](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_open_file_cache.c#L143-L210)

```c
of->fd = NGX_INVALID_FILE;
of->err = 0;

if (cache == NULL) {
    ...
    rc = ngx_open_and_stat_file(name, of, pool->log);   // 不缓存路径
    return rc;
}
...
hash = ngx_crc32_long(name->data, name->len);
file = ngx_open_file_lookup(cache, name, hash);          // 红黑树查找

if (file) {
    file->uses++;
    ngx_queue_remove(&file->queue);   // 命中即提升到 LRU 最新（稍后重新插入尾部）
    ...
}
```

注意 `cache == NULL` 分支（[src/core/ngx_open_file_cache.c:159-198](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_open_file_cache.c#L159-L198)）：当 location 没配 `open_file_cache` 指令时，`clcf->open_file_cache` 为 NULL，每次请求都真 `open()`——这就是为什么生产环境推荐显式开启 `open_file_cache`。

**红黑树查找 `ngx_open_file_lookup`**：[src/core/ngx_open_file_cache.c:1187-1223](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_open_file_cache.c#L1187-L1223)

```c
node = cache->rbtree.root;
sentinel = cache->rbtree.sentinel;

while (node != sentinel) {
    if (hash < node->key)      { node = node->left;  continue; }
    if (hash > node->key)      { node = node->right; continue; }

    /* hash == node->key：用文件名本身再比较，解决哈希碰撞 */
    file = (ngx_cached_open_file_t *) node;
    rc = ngx_strcmp(name->data, file->name);
    if (rc == 0) return file;
    node = (rc < 0) ? node->left : node->right;
}
return NULL;
```

树的 key 是 crc32 哈希值（O(log n)），当两个文件名哈希相同（碰撞）时，再用 `ngx_strcmp` 比较字符串本身决定走左还是右子树——这是一种把「整数比较」和「字符串比较」组合的查找方式。

**真正打开文件 `ngx_open_and_stat_file`**：[src/core/ngx_open_file_cache.c:840-933](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_open_file_cache.c#L840-L933)

```c
if (of->fd != NGX_INVALID_FILE) {            // 已有缓存的 fd
    if (ngx_file_info_wrapper(...) == NGX_FILE_ERROR) ...
    if (of->uniq == ngx_file_uniq(&fi)) goto done;   // uniq 没变，复用 fd
}
...
fd = ngx_open_file_wrapper(name, of, NGX_FILE_RDONLY|NGX_FILE_NONBLOCK, ...);
...
if (ngx_is_dir(&fi)) {
    ngx_close_file(fd);                       // 目录：开了立刻关，不留 fd
    of->fd = NGX_INVALID_FILE;
} else {
    of->fd = fd;
    /* read-ahead / directio 处理 */
}
```

注意它用 `NGX_FILE_NONBLOCK` 打开——注释解释这是为了不在 FIFO 等特殊文件上阻塞（对普通文件无效，但安全）。`of->uniq`（通常是 inode 号）是「文件是否被替换」的指纹：uniq 没变就认为文件没动过，直接复用缓存 fd，省掉 `open()`。

**缓存结构定义**：[src/core/ngx_open_file_cache.h:91-99](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/core/ngx_open_file_cache.h#L91-L99)

```c
typedef struct {
    ngx_rbtree_t       rbtree;       // 按名字哈希组织
    ngx_rbtree_node_t  sentinel;
    ngx_queue_t        expire_queue; // LRU 淘汰队列
    ngx_uint_t         current;      // 当前缓存项数
    ngx_uint_t         max;          // open_file_cache max= 上限
    time_t             inactive;     // open_file_cache valid= 失效秒数
} ngx_open_file_cache_t;
```

`max` 对应配置 `open_file_cache max=N`（缓存项数上限），`inactive` 对应 `open_file_cache inactive=T`（多久没访问就淘汰）。static handler 里 `of.valid`、`of.min_uses`、`of.errors`、`of.events` 这些字段（[ngx_http_static_module.c:90-97](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_static_module.c#L90-L97)）正是从 location 的 `open_file_cache_valid` 等配置读出来的。

#### 4.3.4 代码实践

**实践目标**：观察 open_file_cache 命中与未命中时的系统调用差异，验证缓存确实省掉了 `open()`。

**操作步骤**：

1. 准备两份配置对比。

   配置 A（不开缓存）：

   ```nginx
   location / { root html; }
   ```

   配置 B（开缓存）：

   ```nginx
   location / {
       root html;
       open_file_cache max=1000 inactive=20s;
       open_file_cache_valid 30s;
       open_file_cache_min_uses 1;
   }
   ```

2. 两次都启动 nginx，用 `strace -p <worker_pid> -e trace=open,openat,stat,newfstatat` 跟踪 worker 进程的系统调用。
3. 连续请求同一个 URL 5 次：`for i in 1 2 3 4 5; do curl -s http://127.0.0.1:8080/index.html -o /dev/null; done`。

**需要观察的现象**：

- 配置 A：每次请求都能看到一次 `openat(.../index.html, O_RDONLY|O_NONBLOCK)` + 一次 `fstat`，共 5 组。
- 配置 B：通常只有第一次请求有 `openat`，后面 4 次因为命中缓存而**没有** `openat`（uniq 没变直接复用 fd）。

**预期结果**：能口述「配置 B 通过 `ngx_open_file_lookup` 红黑树命中后复用 fd，省掉了重复 `open()` 系统调用」。这正是高并发静态服务的性能要点。

> 待本地验证：strace 输出形态因 glibc/内核版本而异（可能是 `openat` 也可能是 `newfstatat`），以你机器为准。

#### 4.3.5 小练习与答案

**练习 1**：open_file_cache 为什么连「文件不存在」这种失败结果也要缓存？
**答案**：因为对一个不存在文件的请求，nginx 也要 `open()` 一次才能知道它不存在。如果不缓存失败结果，攻击者反复请求不存在的 URL 就会持续制造 `open()` 系统调用。缓存失败（`file->err` 字段）后，命中时直接复用上次的错误码，连 `open()` 都不用，返回 404 的代价和命中正常文件一样低。开关由 `of.errors`（配置 `open_file_cache_errors on`）控制。

**练习 2**：`ngx_open_file_lookup` 用红黑树的 key 是什么？哈希碰撞时怎么处理？
**答案**：key 是文件名的 crc32 哈希值（`ngx_crc32_long` 算出）。哈希碰撞（两个不同文件名哈希相同）时，用 `ngx_strcmp` 比较文件名本身，按字符串大小决定走左子树还是右子树，从而在碰撞链上做精确匹配。

---

### 4.4 特殊响应：404/403 等错误页是怎么生成的

#### 4.4.1 概念说明

当 static handler 返回 `NGX_HTTP_NOT_FOUND`（404）或 `NGX_HTTP_FORBIDDEN`（403）时，**它并没有自己生成那个 HTML 错误页**——它只是返回了一个错误码。真正把错误码变成「带 HTML body 的 HTTP 响应」的，是 `ngx_http_special_response_handler`。

这个函数在请求结束流程（`ngx_http_finalize_request` 收到错误码时）被调用。它要处理三件事：

1. **`error_page` 指令的内部重定向**：如果你配了 `error_page 404 /custom-404.html`，那么遇到 404 时它会把请求内部重定向到那个 URI，重新走一遍 phase（从而返回你的自定义页面）。
2. **从错误码查到内置错误页**：nginx 内置了一组 HTML 片段（`ngx_http_error_pages` 数组），按错误码索引。
3. **拼装并发送**：把「错误页正文 + 尾部（含 nginx 版本信息）」组成 body，设 `Content-Type: text/html`，经 `ngx_http_send_header` + 过滤器链发出。

#### 4.4.2 核心流程

```text
ngx_http_special_response_handler(r, error)
 ├── 记 r->err_status = error
 ├── 若配了 error_pages 且未在 error_page 递归中：
 │      遍历 clcf->error_pages 找匹配状态码 -> ngx_http_send_error_page() 内部重定向
 ├── 否则：把 error 映射成 ngx_http_error_pages[] 的下标 err
 │      4XX -> error - 400 + NGX_HTTP_OFF_4XX
 │      5XX -> error - 494 + NGX_HTTP_OFF_5XX  （注意 nginx 私有 494~499 占了一段）
 └── ngx_http_send_special_response(r, clcf, err)
        ├── 按 server_tokens 选尾部（带版本 / 不带版本 / build 版本）
        ├── content_length_n = 错误页正文 + 尾部长度
        ├── Content-Type = text/html
        ├── ngx_http_send_header()
        └── 组 buf（正文 + 尾部）-> output_filter
```

#### 4.4.3 源码精读

**入口 `ngx_http_special_response_handler`**：[src/http/ngx_http_special_response.c:424-532](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L424-L532)

关键两段。第一段，`error_page` 内部重定向：[src/http/ngx_http_special_response.c:464-477](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L464-L477)

```c
if (!r->error_page && clcf->error_pages && r->uri_changes != 0) {
    if (clcf->recursive_error_pages == 0) {
        r->error_page = 1;            // 防止重定向后再次匹配 error_page 死循环
    }
    err_page = clcf->error_pages->elts;
    for (i = 0; i < clcf->error_pages->nelts; i++) {
        if (err_page[i].status == error) {
            return ngx_http_send_error_page(r, &err_page[i]);
        }
    }
}
```

`r->error_page` 标志位和 `r->uri_changes` 计数器共同防止无限递归——内部重定向会重新跑 phase，如果自定义错误页又触发错误，没有这两道保护就会死循环。

第二段，错误码到下标的映射：[src/http/ngx_http_special_response.c:501-529](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L501-L529)

```c
} else if (error >= NGX_HTTP_BAD_REQUEST && error < NGX_HTTP_LAST_4XX) {
    /* 4XX */
    err = error - NGX_HTTP_BAD_REQUEST + NGX_HTTP_OFF_4XX;
} else if (error >= NGX_HTTP_NGINX_CODES && error < NGX_HTTP_LAST_5XX) {
    /* 49X, 5XX */
    err = error - NGX_HTTP_NGINX_CODES + NGX_HTTP_OFF_5XX;
    ...
}
return ngx_http_send_special_response(r, clcf, err);
```

**内置错误页表 `ngx_http_error_pages`**：[src/http/ngx_http_special_response.c:348-420](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L348-L420)

这是一组 `ngx_str_t` 数组，按下标紧凑排布所有错误页正文片段。注意它用「哨兵 + 偏移」管理稀疏状态码空间：

```c
ngx_null_string,                     /* 201, 204 */
#define NGX_HTTP_LAST_2XX  202
#define NGX_HTTP_OFF_3XX   (NGX_HTTP_LAST_2XX - 201)
ngx_string(ngx_http_error_301_page),
...
ngx_string(ngx_http_error_400_page),   /* 400 在数组里的下标 */
ngx_string(ngx_http_error_401_page),   /* 401 */
...
ngx_string(ngx_http_error_404_page),   /* 404 */
```

`NGX_HTTP_OFF_4XX`（[src/http/ngx_http_special_response.c:366](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L366)）定义为 `(NGX_HTTP_LAST_3XX - 301 + NGX_HTTP_OFF_3XX)`，即「400 在数组中的下标」。所以 `err = error - 400 + NGX_HTTP_OFF_4XX` 正好把 HTTP 状态码 400/401/402… 映射到数组里对应的页。未被占用的码（如 417~428）用 `ngx_null_string` 占位，运行时遇到就退化为「空 body 响应」。nginx 自定义的 494（请求头过大）、495~497（HTTPS 相关）也挤在这张表里（[src/http/ngx_http_special_response.c:402-407](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L402-L407)），所以 5XX 段的下标要减去 `NGX_HTTP_NGINX_CODES`（494）而非 500。

**发送函数 `ngx_http_send_special_response`**：[src/http/ngx_http_special_response.c:681-768](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_special_response.c#L681-L768)

```c
if (clcf->server_tokens == NGX_HTTP_SERVER_TOKENS_ON) {
    tail = ngx_http_error_full_tail;     // 带完整版本号
} else { ... tail = ngx_http_error_tail; }   // 只写 "nginx"

r->headers_out.content_length_n = ngx_http_error_pages[err].len + len;
ngx_str_set(&r->headers_out.content_type, "text/html");
...
ngx_http_send_header(r);
...
b->memory = 1;
b->pos = ngx_http_error_pages[err].data;     // 正文
b->last = ngx_http_error_pages[err].data + ngx_http_error_pages[err].len;
out[0].buf = b; out[0].next = &out[1];
...
b->pos = tail; b->last = tail + len;         // 尾部
out[1].buf = b;
```

注意 `server_tokens off` 的作用就在这里：它切换 `tail` 指针，让错误页底部只显示 `nginx` 而不是带具体版本号（`nginx/1.x.x`）——这是安全加固的常见做法，避免暴露版本。响应体被拆成 `[正文, 尾部]` 两个内存 buf（`b->memory=1`），用 `out[0]`、`out[1]` 这条短 chain 送进过滤器链，和 4.2 里送文件用的是同一个 `ngx_http_output_filter` 入口，只是这次数据在内存而非文件。

#### 4.4.4 代码实践

**实践目标**：用 `error_page` 指令验证「内部重定向」分支，并用 `server_tokens` 观察尾部切换。

**操作步骤**：

1. 配置：

   ```nginx
   http {
       server {
           listen 8080;
           server_tokens off;
           root html;
           location / {
               # 不存在的文件会触发 404
           }
           error_page 404 /404.html;
           location = /404.html {
               internal;            # 仅允许内部重定向访问
               root html;
           }
       }
   }
   ```

2. 在 `html/404.html` 写一段自定义内容，如 `custom not found`。
3. 请求一个不存在的路径：`curl -v http://127.0.0.1:8080/nope.html`。

**需要观察的现象**：

- 响应是 404，但 body 是 `custom not found` 而非内置错误页——证明走了 4.4.3 第一段的 `error_page` 内部重定向分支，请求被重新定向到 `/404.html` 又跑了一遍 phase。
- 响应头里 `Server: nginx`（不带版本号）——证明 `server_tokens off` 让 `ngx_http_send_special_response` 选择了不带版本的尾部。
- 把 `error_page 404 /404.html;` 删掉再请求，body 会变回 nginx 内置的 `<html>...404 Not Found...`，来自 `ngx_http_error_404_page`。

**预期结果**：能解释「static handler 返回 404 → finalize 调用 special_response_handler → 命中 error_page 则内部重定向，否则用内置页」这条链，并说清 `server_tokens` 影响的是错误页尾部的版本号显示。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ngx_http_special_response_handler` 里要有 `r->uri_changes != 0` 这个判断？
**答案**：`error_page` 内部重定向会让请求以新 URI 重新跑一遍 phase。如果自定义错误页本身又触发错误（比如 `/404.html` 也不存在），就会再次进入 `special_response_handler`，可能无限递归。`r->uri_changes` 是 nginx 给每次请求固定的内部重定向次数上限（默认 10），耗尽就不再重定向，强制用内置错误页，从而打破死循环。

**练习 2**：nginx 状态码 494（请求头过大）并不在标准 HTTP 的 4XX 范围，它在 `ngx_http_error_pages` 表里是怎么安放的？
**答案**：nginx 把自己私有的 494~499 紧接在标准 4XX 之后、500 之前放进同一张 `ngx_http_error_pages` 数组（见 `src/http/ngx_http_special_response.c:402-407`），并定义宏 `NGX_HTTP_NGINX_CODES = 494`。映射时 5XX/49X 段用 `err = error - NGX_HTTP_NGINX_CODES + NGX_HTTP_OFF_5XX` 计算下标，从而让 494、500、501… 都能正确落到对应表项。

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「带缓存的静态文件服务 + 自定义错误页」端到端追踪。

**配置**（在 u1-l2 的构建产物基础上）：

```nginx
worker_processes 1;
events { worker_connections 1024; }
http {
    server {
        listen 8080;
        server_tokens off;

        location / {
            root html;
            open_file_cache max=1000 inactive=20s;
            open_file_cache_valid 30s;
            open_file_cache_errors on;
        }

        error_page 404 /404.html;
        location = /404.html { internal; root html; }
    }
}
```

**任务**：

1. 请求 `GET /index.html`（存在）—— 在 debug 日志中找出 `http filename:`、`http static fd:` 两行，确认 4.2 主链；在 strace 中确认第二次请求没有 `openat`（4.3 缓存命中）。
2. 请求 `GET /missing.html`（不存在）—— 观察响应是带 `custom not found` 的 404（4.4 的 `error_page` 重定向分支）；再临时删除 `error_page` 指令，观察 body 变成内置 404 页（`ngx_http_error_404_page`）。
3. 请求 `GET /index.html` 但用 `POST` 方法 —— 观察返回 405（4.2 步骤 6 的 `NGX_HTTP_NOT_ALLOWED`）。
4. 把 `html/index.html` 改名为目录或在它前面放一个同名目录，请求 `GET /index.html` —— 观察是否走 4.2 步骤 5 的目录 301 重定向（取决于具体形态，目录请求通常先在步骤 2 因无尾斜杠 DECLINED，这里主要验证你对各分支的理解）。

**输出要求**：为上述每个请求，写出「请求方法+路径 → 命中 4.2 的哪一步 → 最终状态码 → body 来源（磁盘文件 / 内置错误页 / 自定义错误页）」一行结论。

> 待本地验证：部分现象（如 debug 日志关键字、strace 系统调用名）依赖你的编译选项与运行环境，请以实际观察为准。

## 6. 本讲小结

- `ngx_http_static_module` 是 CONTENT 阶段的「候选处理者」之一；仅当 location 没有专属 `content_handler`（如 `proxy_pass`）时，它才会被「轮流尝试」调用。
- `ngx_http_static_handler` 是一条线性、同步的主链：方法白名单 → URI 映射磁盘路径 → 带缓存打开文件 → 目录则 301、非普通文件则 404、POST 则 405 → 组装 `in_file` 的 buf → `ngx_http_output_filter` 送入过滤器链。
- 它不把文件内容读进内存，只记 `fd` 和区间，真正的磁盘→网络搬运由过滤器链末端的 `sendfile()` 完成，这是静态服务的快路径。
- `open_file_cache` 用「红黑树（按 crc32 哈希查找）+ LRU 队列」缓存「open+stat 结果」，且连失败结果（404）也缓存；命中时省掉 `open()` 系统调用。
- static handler 只返回错误码，真正生成 HTML 错误页的是 `ngx_http_special_response_handler`：它先处理 `error_page` 内部重定向，否则用 `ngx_http_error_pages[]` 内置页 + 可选尾部（受 `server_tokens` 影响）拼装响应。
- 错误码到内置页下标的映射用「哨兵 + 偏移」紧凑管理稀疏状态码空间，nginx 私有的 494~499 也挤在同一张表里。

## 7. 下一步学习建议

- **index / autoindex 模块**：本讲多次提到 static 返回 `NGX_DECLINED` 后由 index 模块接管目录请求。建议阅读 `src/http/modules/ngx_http_index_module.c` 与 `ngx_http_autoindex_module.c`，对照 4.1 的「轮流尝试」机制理解它们如何与 static 协作。
- **gzip_static / try_files**：它们同样是 CONTENT 阶段候选 handler，且都复用了本讲的 `ngx_open_cached_file`。阅读 `ngx_http_gzip_static_module.c` 可以看到「文件类型判断 + 缓存打开」的另一种用法。
- **过滤器链实战（u6-l6）**：本讲的 `in_file` buf 最终走进过滤器链。结合 u6-l6 的 `copy_filter`/`write_filter`，完整理解「buf 标志位如何决定数据走内存路径还是 sendfile 路径」。
- **写自己的 content handler（u10-l4）**：当你想自己实现一个返回固定内容或动态内容的 location，参考本讲的骨架（注册 phase、设置 `headers_out`、组 buf、调 `output_filter`），后续 u10-l4 会给出一个完整实战。
