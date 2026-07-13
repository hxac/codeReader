# XSLT 过滤器与 XML 外部实体（XXE）安全

## 1. 本讲目标

本讲是 HTTP 过滤器链（u6-l6）的延伸专题。读完本讲你应该能够：

- 说清楚 `ngx_http_xslt_filter_module` 如何作为一条 HTTP body/header filter，在响应回传途中用 libxml2/libxslt 把 XML 响应整体转换后再吐给客户端。
- 理解 libxml2 的「推模式（push）解析器 + SAX 回调」模型，以及 nginx 是如何在每收到一块响应数据时增量喂数据进解析器、并在解析器上挂自定义回调的。
- 讲明白什么是 XXE（XML External Entity）攻击、它的危害，以及 nginx 当前 HEAD 默认启用的三层防护：`XML_PARSE_NONET` 禁止网络加载、`entityDecl` 回调剥离 systemId、关闭文档内 catalog。
- 读懂新增的 `xml_external_entities` 指令：它如何从 location 配置出发、经 create_conf / merge_conf 的哨兵机制、最终在 `ngx_http_xslt_sax_entity_decl` 里通过 `conf->external_entities` 决定是否剥离 systemId。

---

## 2. 前置知识

本讲假设你已读过 **u6-l6 过滤器链 header/body filter**，知道 nginx 有两条用 `next` 指针隐式串联的过滤器链（header filter 与 body filter），各自有一个全局链头指针 `ngx_http_top_header_filter` / `ngx_http_top_body_filter`，模块在 `postconfiguration` 阶段用「`next = top; top = self`」把自己 push 进链。如果你还不熟悉，请先补这一讲。

下面几个与 XML 相关的术语，初学者可能陌生，先建立直觉：

- **XML（可扩展标记语言）**：用标签描述结构化数据的文本格式，如 `<book><title>nginx</title></book>`。
- **XSLT（可扩展样式表语言转换）**：一种专门用来「把一份 XML 转换成另一份 XML/HTML/文本」的语言。可以类比为「XML 世界里的模板引擎」——你写一份 `.xsl` 样式表描述转换规则，处理器吃进源 XML、吐出结果。
- **DTD（文档类型定义）**：XML 里用来声明「这份文档允许哪些标签、属性、实体」的规则块，写在 `<!DOCTYPE ...>` 中。
- **实体（entity）**：XML 里的「宏/占位符」。`&name;` 会在解析时被替换成实体内容。实体可以来自内部字面量，也可以指向外部资源。
- **内部 DTD 子集（internal DTD subset）**：直接写在 XML 文档本体 `<!DOCTYPE ... [ ... ]>` 方括号里的 DTD 片段。攻击者可以把恶意外部实体声明塞在这里。
- **SAX（Simple API for XML）**：一种「事件驱动」的 XML 解析方式。解析器边读边触发回调（如「遇到一个开始标签」「遇到一个实体声明」），而不是先构建一棵完整 DOM 树。nginx 用的就是 SAX。
- **systemId / publicId**：外部实体的两类标识。`systemId` 是一个 URI（通常是文件路径或 URL，如 `file:///etc/passwd` 或 `http://evil.com/x.dtd`），解析器会**真的去加载它**；`publicId` 是一个抽象名字，需要通过系统上的 XML catalog 映射到本地文件才会被加载。

### 什么是 XXE 攻击

XXE 的核心危险是：**XML 解析器会替你「去加载」实体指向的外部资源**。如果服务器解析了用户/上游可控的 XML，攻击者只要在内部 DTD 子集里声明一个指向 `file:///etc/passwd` 的外部实体，再在文档体里引用它，解析器就会真的去读 `/etc/passwd` 并把内容塞进解析结果——于是本应只做「格式转换」的 XSLT 过滤器，就可能把服务器上的敏感文件内容回显给客户端（信息泄露），或用 `http://` 实体去请求内网地址（SSRF，服务端请求伪造），甚至让 worker 因加载超大/极慢的远程资源而长时间阻塞（DoS）。

正因为 nginx 的 XSLT 过滤器解析的是**上游响应**（往往不完全可信），这部分防护非常关键。本讲要解析的，就是 nginx 在最新代码里为此新增的一整套机制。

> 提示：本讲引用的源码集中在单个文件 [src/http/modules/ngx_http_xslt_filter_module.c](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c)。该模块默认不编译，构建时需加 `--with-http_xslt_module`（见 [auto/options:247-248](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/auto/options#L247-L248)）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [src/http/modules/ngx_http_xslt_filter_module.c](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c) | XSLT 过滤器模块的全部实现：指令表、filter 注册、push 解析、SAX 回调、XXE 防护、样式表应用与输出。本讲所有源码引用都出自它。 |
| [auto/options](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/auto/options) | 编译开关 `--with-http_xslt_module` 的定义，决定该模块是否编入。 |
| [auto/lib/libxslt/conf](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/auto/lib/libxslt/conf) | 探测系统 libxml2/libxslt 库是否存在。 |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块，层层递进：

1. **XSLT 过滤器如何挂入 filter 链**——它什么时候介入、什么时候放行。
2. **增量解析 XML 响应**——push parser 与 SAX 回调的挂载点。
3. **XXE 攻击原理与三层防护**——`XML_PARSE_NONET`、剥离 systemId、关闭 catalog。
4. **`xml_external_entities` 指令与配置生命周期**——从指令到运行时判定。

### 4.1 XSLT 过滤器如何挂入 filter 链

#### 4.1.1 概念说明

XSLT 过滤器要做的事很特别：它要**等上游把整个 XML 响应都收齐**，才能做一次完整的解析 + 样式表转换，再把转换结果作为新响应体发给客户端。这与「收到一块就转发一块」的普通过滤器不同，因此它必须：

- 介入响应输出路径（挂进 header/body filter 链）。
- 把原始响应体**截留累积**起来，不立即下发。
- 判断「这个响应要不要处理」（看内容类型、看是否配了样式表、看是否 304）。

#### 4.1.2 核心流程

模块在 `postconfiguration`（即 `ngx_http_xslt_filter_init`）里把自己 push 进两条链：

```text
注册阶段（配置加载完毕时执行一次）:
  ngx_http_top_header_filter = ngx_http_xslt_header_filter   # 链头换成自己
  ngx_http_top_body_filter   = ngx_http_xslt_body_filter

运行阶段（每个响应）:
  header_filter:
    304?                 -> 放行（透传给下一个 header filter）
    没配样式表 or 类型不匹配? -> 放行
    已有 ctx?            -> 放行（避免重复处理）
    否则                 -> 建 ctx、标记 need_in_memory、禁 ranges，return NGX_OK（先压住响应头）
  body_filter:
    逐块 add_chunk 喂给 push parser
    收到 last_buf（最后一块）-> 整体应用样式表 -> 一次性下发
```

注意 header filter 返回 `NGX_OK` 而不是调用 `next_header_filter`：它故意「扣住」响应头，直到 body 全部转换完、在 `ngx_http_xslt_send` 里才调用下一个 header filter——因为转换后响应体长度变了，`Content-Length` 也要等转换完才能算。

#### 4.1.3 源码精读

**注册到 top filter 链**——经典的「`next = top; top = self`」两步，与 u6-l6 讲的过滤器链注册模式完全一致：

[src/http/modules/ngx_http_xslt_filter_module.c:1230-1240](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1230-L1240) —— 把自身 push 进 header/body 两条 filter 链的链头，并把旧链头存进模块私有的 `ngx_http_next_header_filter` / `ngx_http_next_body_filter`。

[src/http/modules/ngx_http_xslt_filter_module.c:213-214](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L213-L214) —— 这两个 `static` 变量就是各 filter 用来记住「下一个 filter」的私有指针。

**header filter 的放行判定**：

[src/http/modules/ngx_http_xslt_filter_module.c:217-255](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L217-L255) —— header filter 主体。关键判定有三处：

- 第 226-228 行：`304 Not Modified` 直接放行（304 没有响应体，无需转换）。
- 第 232-236 行：`conf->sheets.nelts == 0`（本 location 没配任何 `xslt_stylesheet`）或内容类型不在 `xslt_types` 白名单（默认仅 `text/xml`），直接放行。
- 第 244-252 行：通过判定后，建一个 `ngx_http_xslt_filter_ctx_t` 挂到请求上；第 251 行 `r->main_filter_need_in_memory = 1` 是关键——它要求上游 filter 把文件类 buf 读进内存（XSLT 要拿完整 XML 文本去解析，不能只给个 fd）；第 252 行 `r->allow_ranges = 0` 关掉范围请求，因为转换后内容已变。

**body filter 的截留与最终转换**：

[src/http/modules/ngx_http_xslt_filter_module.c:258-322](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L258-L322) —— body filter 主体。第 278-293 行循环把每块 buf 经 `ngx_http_xslt_add_chunk` 喂进 push parser；第 295-318 行在遇到 `last_buf`（响应最后一块）时，取出 parser 构建好的 `myDoc`，若 `wellFormed` 则调 `ngx_http_xslt_apply_stylesheet` 做转换并通过 `ngx_http_xslt_send` 一次性下发，否则记 `"not well formed XML document"` 错误并下发空（触发 500）。`ctx->done`（第 274 行）保证一个请求只转换一次。

#### 4.1.4 代码实践

**实践目标**：验证 XSLT 过滤器「只在配了样式表且类型匹配时介入」。

**操作步骤**（属源码阅读型实践，无需真正运行）：

1. 打开本讲的 [header_filter 源码](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L217-L255)。
2. 用一张表列出三个「放行条件」分别对应什么样的请求：
   - `304 Not Modified`（条件性请求）
   - 没配 `xslt_stylesheet` 或 `Content-Type` 不是 `text/xml`
   - 已经处理过（ctx 已存在）
3. 思考：如果一个 location 配了 `xslt_stylesheet`，但上游返回的是 `application/json`，过滤器会怎样？依据第 233 行 `ngx_http_test_content_type(r, &conf->types) == NULL` 给出结论。

**需要观察的现象**：放行路径都走 `ngx_http_next_header_filter(r)`，即把响应原样交给链中下一个 filter；只有「需要处理」的响应才会建 ctx 并扣住响应头。

**预期结果**：JSON 响应不会被转换——内容类型不匹配，过滤器透明放行。

#### 4.1.5 小练习与答案

**练习 1**：为什么 header filter 在「需要处理」时返回 `NGX_OK` 而不是立即调用下一个 header filter？

> **参考答案**：因为 XSLT 转换会改变响应体长度，`Content-Length` 必须等转换完成后才能计算。header filter 先把响应头「扣住」，等 body filter 收齐并转换后，在 `ngx_http_xslt_send`（[第 349-350 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L349-L350)）里设置 `content_length_n` 再调用下一个 header filter。

**练习 2**：`r->main_filter_need_in_memory = 1`（[第 251 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L251)）解决了什么问题？参考 u6-l6 的 `copy_filter` 讲解。

> **参考答案**：它要求上游（`copy_filter`）把文件类 buf 先读进内存再传下来。XSLT 解析器需要完整的 XML 文本来调用 `xmlParseChunk`，而不能只靠 fd + 区间去 sendfile（sendfile 不会经过用户态解析）。

---

### 4.2 增量解析 XML 响应：push parser 与 SAX 回调挂载

#### 4.2.1 概念说明

HTTP 响应是**流式**到达的，可能分成很多块；而 XML 又必须解析成完整文档才能做 XSLT 转换。libxml2 提供了「推模式解析器（push parser）」专门解决这个矛盾：你可以**分多次**把数据块喂给它（`xmlParseChunk`），它内部维护一个状态机，跨块续接解析，最后给你一棵完整的文档树。

而 SAX（Simple API for XML）是 push parser 触发事件的方式：解析过程中遇到特定语法结构时，它会调用一张「回调函数表（`xmlSAXHandler`）」里对应的函数。nginx 的做法是：**把这张表里几个关键槽位换成自己的回调**，从而在解析过程中「插手」实体的处理——这正是 XXE 防护的挂载点。

#### 4.2.2 核心流程

```text
ngx_http_xslt_add_chunk(r, ctx, buf):           # 每收到一块响应 buf 调一次
  if 第一次（ctx->ctxt == NULL）:
    1. xmlCreatePushParserCtxt() 创建解析上下文
    2. xmlCtxtUseOptions() 设置解析选项（含 XML_PARSE_NONET）  # <-- 防护层 1
    3. 替换 SAX 回调表：
         externalSubset  = ngx_http_xslt_sax_external_subset
         entityDecl      = ngx_http_xslt_sax_entity_decl          # <-- 防护层 2
         setDocumentLocator = NULL
         error/fatalError = ngx_http_xslt_sax_error
         _private = ctx                                            # 反向找回请求上下文
  xmlParseChunk() 把这块 buf 喂进去（解析器内部可能触发上面的回调）
```

关键点：SAX 回调表是「每个请求一份」的（挂在该请求的 parser context 上），且 nginx 用 `ctxt->sax->_private = ctx` 把自己的请求上下文塞进回调表的私有字段，这样回调被 libxml2 触发时，能从 `data` 参数反查回 nginx 的 `request` 与配置。

#### 4.2.3 源码精读

[src/http/modules/ngx_http_xslt_filter_module.c:385-426](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L385-L426) —— `ngx_http_xslt_add_chunk` 全貌。

[src/http/modules/ngx_http_xslt_filter_module.c:394-401](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L394-L401) —— 创建 push parser 并设置解析选项。其中 `XML_PARSE_NONET` 是 XXE 防护的**第一层**（见 4.3），禁止解析器通过网络加载任何外部资源。

[src/http/modules/ngx_http_xslt_filter_module.c:403-408](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L403-L408) —— 挂载自定义 SAX 回调，并把 `ctx` 挂到 `_private`。注意 `entityDecl` 这一行——它就是 4.3 节要精读的 XXE 防护**第二层**的入口。

[src/http/modules/ngx_http_xslt_filter_module.c:414-415](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L414-L415) —— `xmlParseChunk` 真正喂数据。第三个参数 `b->last_buf || b->last_in_chain` 是「这是否最后一块」的标志，传 1 会让解析器完成收尾（触发 `wellFormed` 判定等）。

> 名词解释：`xmlParserCtxtPtr` 是 libxml2 的「解析上下文」，封装了状态机与 SAX 回调表；`xmlSAXHandler`（即 `ctxt->sax`）就是那张回调函数表。

#### 4.2.4 代码实践

**实践目标**：理解「push parser 跨块续接」与「SAX 回调挂载」。

**操作步骤**（源码阅读型实践）：

1. 在 [body_filter](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L258-L322) 里确认：循环里每块都调 `ngx_http_xslt_add_chunk`，但只有第一次才会创建 parser（看 [add_chunk 第 392 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L392) 的 `if (ctx->ctxt == NULL)`）。
2. 追踪回调的反向寻址路径：`ngx_http_xslt_sax_entity_decl(data, ...)` 第 484 行 `xmlParserCtxtPtr ctxt = data;`，第 490 行 `ctx = ctxt->sax->_private;`，第 491 行 `r = ctx->request;`。说明 libxml2 触发回调时传入的 `data` 其实就是 parser context，nginx 借 `_private` 反查回请求。

**需要观察的现象**：回调函数签名第一个参数 `void *data` 在 libxml2 约定里就是 parser context；nginx 没有另开一个用户数据指针，而是复用了 `_private` 字段。

**预期结果**：每个回调都能拿到 `ctxt`，再经 `_private` 拿到 nginx 的 `ctx` 与 `r`，从而能读取 location 配置。

#### 4.2.5 小练习与答案

**练习 1**：为什么 parser 只在第一块响应时创建，而不是每块都创建？

> **参考答案**：push parser 的全部意义就是跨块续接——它内部维护解析状态机，`xmlParseChunk` 多次调用累积同一份文档树。若每块都新建 parser，会把一份完整 XML 切碎成多个无法解析的片段。

**练习 2**：`xmlParseChunk` 的第三个参数（[第 415 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L414-L415)）何时为 1？为 1 会触发什么？

> **参考答案**：当 `b->last_buf || b->last_in_chain`（响应最后一块）时为 1，表示「后续没有更多数据了」，解析器据此完成收尾并最终判定 `wellFormed`。body_filter 正是用这个标志判断「该整体应用样式表了」。

---

### 4.3 XXE 攻击原理与三层防护

#### 4.3.1 概念说明

回顾 4.2，外部实体分两种来源：

- **外部 DTD 子集**：`<!DOCTYPE root SYSTEM "http://...">` 里 `SYSTEM` 指向的外部 DTD 文件。nginx 自模块诞生起就在 `externalSubset` 回调里**完全跳过**它（替换成自己的、只挂本地 `xml_entities` 配置的 DTD），既为安全也为避免网络阻塞。
- **内部 DTD 子集里的外部实体**：`<!DOCTYPE root [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>` 方括号里声明的、带 `SYSTEM` 的实体。这部分在最新代码之前是会按声明加载的——这正是 XXE 的主战场。

nginx 当前 HEAD（提交 `4d0e620f9`、`017dbad85`）默认启用了**三层**防护，从外到内逐层收紧：

| 层 | 机制 | 挡住什么 | 在哪里 |
|---|---|---|---|
| 第 1 层 | `XML_PARSE_NONET` 解析选项 | 所有走 `http://`/`ftp://` 等网络的实体/DTD 加载（worker 不会被远程慢资源阻塞） | [第 400-401 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L400-L401) |
| 第 2 层 | `entityDecl` 回调剥离 systemId | 内部子集里带 `SYSTEM` 的实体直接加载本地文件（如 `file:///etc/passwd`） | [ngx_http_xslt_sax_entity_decl 第 480-535 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L480-L535) |
| 第 3 层 | 关闭文档内 catalog（`xmlCatalogSetDefaults(XML_CATA_ALLOW_GLOBAL)`） | libxml2 < 2.14.0 默认接受文档内 catalog，可能被滥用做 catalog 重定向 | [preconfiguration 第 1222-1224 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1222-L1224) |

提交 `017dbad85` 加了第 1 层，提交 `4d0e620f9` 加了第 2、3 层并新增 `xml_external_entities` 指令作为第 2 层的总开关。

#### 4.3.2 核心流程

第 2 层防护的核心是「**不阻止实体声明本身，而是把它的 `systemId` 抹掉**」。这样实体依然存在、文档依然 well-formed，但它失去了指向外部资源的能力。处理分三类：

```text
ngx_http_xslt_sax_entity_decl(data, name, type, publicId, systemId, content):
  if systemId 存在 且 conf->external_entities 为关（默认）:
    记 WARN 日志 "xslt filter external entity ignored: ..."
    按情况改写后转交默认实现 xmlSAX2EntityDecl:
      - 有 publicId:           保留 publicId，但 systemId 置空
      - 外部通用解析实体:       改成「内部通用实体」(内容为空)，彻底去外部化
      - 外部参数实体:           改成「内部参数实体」(内容为空)
    return                                                       # 关键：不再加载外部资源
  否则（无 systemId，或用户显式开启了 external_entities）:
    直接转交 xmlSAX2EntityDecl(... 原样 ...)                      # 正常声明
```

这个设计很巧妙：它**不破坏文档结构**（实体名还在，引用 `&xxe;` 仍有效，只是展开成空字符串），又**彻底切断文件/网络读取**。同时保留了一条「后门」——如果实体带 `publicId`，仍可通过系统上的 XML catalog（一个 publicId → 本地文件的映射表）解析，前提是管理员在系统层面预先配置了 catalog。这就是「关掉直接加载、保留受控的 catalog 解析」。

#### 4.3.3 源码精读

**第 1 层：`XML_PARSE_NONET`**

[src/http/modules/ngx_http_xslt_filter_module.c:400-401](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L400-L401) —— 解析选项 `XML_PARSE_NOENT|XML_PARSE_DTDLOAD|XML_PARSE_NONET|XML_PARSE_NOWARNING` 中的 `XML_PARSE_NONET` 禁止任何网络加载。提交说明指出：加载外部实体发生在解析响应期间，网络加载会长时间阻塞整个 worker；且 libxml2 自 2.13.0 起默认就禁网、2.15.0 起彻底移除——nginx 显式加上是为了在更老的 libxml2 上也安全。

**第 2 层：`ngx_http_xslt_sax_entity_decl` 剥离 systemId（本讲核心）**

[src/http/modules/ngx_http_xslt_filter_module.c:480-535](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L480-L535) —— XXE 防护的核心函数。逐段看：

- 第 490-491 行：从 `_private` 反查回 nginx 的 `ctx` 与请求 `r`。
- 第 499 行：**取得本次请求所在 location 的配置** `conf`，这是开关判定的数据来源。
- 第 501 行：核心判定 `if (systemId && !conf->external_entities)`——「实体带 systemId」且「配置未显式开启外部实体」时进入剥离分支。
- 第 511-516 行：记一条 `NGX_LOG_WARN` 告警 `"xslt filter external entity ignored: ..."`，这正是实践任务要观察的日志。
- 第 518-529 行：按实体形态分三种改写并转交默认实现 `xmlSAX2EntityDecl`：
  - 第 518-520 行：有 `publicId` 时保留 publicId、把 systemId 换成空串 `""`（走 catalog 解析路径）。
  - 第 522-524 行：`XML_EXTERNAL_GENERAL_PARSED_ENTITY`（外部通用解析实体，XXE 最常用的形态）改成 `XML_INTERNAL_GENERAL_ENTITY` 且内容为空。
  - 第 526-528 行：`XML_EXTERNAL_PARAMETER_ENTITY`（外部参数实体）改成 `XML_INTERNAL_PARAMETER_ENTITY` 且内容为空。
- 第 531 行：`return`——剥离后**不再**走默认的、会触发加载的逻辑。
- 第 534 行：否则（无 systemId 或用户开了开关）按原样 `xmlSAX2EntityDecl(...)`。

源码注释（[第 503-509 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L503-L509)）把意图说得很清楚：剥离 systemId 确保实体不能被直接加载，但带 publicId 的实体仍可借系统 XML catalog 解析。

**配套：`ngx_http_xslt_sax_external_subset`（外部 DTD 子集的处理）**

[src/http/modules/ngx_http_xslt_filter_module.c:429-477](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L429-L477) —— 这个回调处理 `<!DOCTYPE ... SYSTEM "...">` 指向的外部 DTD 子集。它**从不加载远端**，而是把 nginx 自己的本地 DTD（来自 `xml_entities` 指令预解析的 `conf->dtd`）挂到 `doc->extSubset`（第 476 行）。所以外部 DTD 子集这条路一直就是「被替换成本地 DTD」，与本次新增的内部子集防护互补。

**第 3 层：关闭文档内 catalog**

[src/http/modules/ngx_http_xslt_filter_module.c:1213-1227](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1213-L1227) —— `preconfiguration` 里调 `xmlInitParser()`、可选注册 exslt，并在第 1222-1224 行（仅当 `LIBXML_CATALOG_ENABLED` 且 `LIBXML_VERSION < 21400` 时）调 `xmlCatalogSetDefaults(XML_CATA_ALLOW_GLOBAL)`。提交说明解释：libxml2 在 2.14.0（2025-03-27）之前默认接受「文档内 catalog」（即 XML 文档里自带 catalog 指令），这可能被滥用，所以这里显式只允许「全局 catalog」（系统级、管理员受控），关掉文档内 catalog。第 25-27 行的条件包含 `#include <libxml/catalog.h>` 就是为这一步准备的。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手触发 XXE 防护，对比默认行为与 `xml_external_entities on;` 的差异，并用源码解释判定逻辑。

**操作步骤**：

1. 用 `--with-http_xslt_module` 编译 nginx（依赖系统装好 libxml2、libxslt；见 [auto/lib/libxslt/conf](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/auto/lib/libxslt/conf)）。

2. 准备一份样式表 `/etc/nginx/test.xsl`（示例代码，仅用于把 XML 转成纯文本展示实体展开结果）：

   ```xml
   <!-- 示例代码：把任意 XML 转成纯文本 -->
   <xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
     <xsl:output method="text"/>
     <xsl:template="/"><xsl:value-of select="."/></xsl:template>
   </xsl:stylesheet>
   ```

3. 配置一个返回 XML 的 location，并用上游（或 `return` / 静态文件）喂入**带恶意外部实体**的 XML：

   ```nginx
   # 示例配置
   location /xxe {
       xslt_stylesheet /etc/nginx/test.xsl;
       default_type text/xml;
       # 假设这里 proxy_pass 到一个返回下面 XML 的上游
   }
   ```

   构造的恶意 XML（内部 DTD 子集里声明外部实体）：

   ```xml
   <?xml version="1.0"?>
   <!DOCTYPE root [
     <!ENTITY xxe SYSTEM "file:///etc/passwd">
   ]>
   <root>&xxe;</root>
   ```

4. 第一次**不**加 `xml_external_entities` 指令（默认 off），请求 `/xxe`。

**需要观察的现象**：

- 响应体里 `&xxe;` 被展开成**空字符串**（不会出现 `/etc/passwd` 内容）。
- nginx 的 error_log（级别需包含 `warn`）出现一条：

  ```text
  ... [warn] ... xslt filter external entity ignored: "xxe" "" "file:///etc/passwd"
  ```

**预期结果**：第 2 层防护生效——`ngx_http_xslt_sax_entity_decl` 命中第 501 行 `systemId && !conf->external_entities`，记告警并把该外部通用解析实体改写成空的内部实体（第 522-524 行），文件读取被阻断。

5. 第二次在 location 里加 `xml_external_entities on;` 重新加载配置，再请求同样的 XML。

**需要观察的现象**：此时第 501 行条件不成立（`conf->external_entities` 为真），走第 534 行 `xmlSAX2EntityDecl` 原样处理。由于 `XML_PARSE_NONET` 仍生效，`file:///etc/passwd` 这类本地文件**仍可能被读取**（这正是「重新放开」带来的风险），但任何 `http://`/`ftp://` 网络实体仍被第 1 层挡住。

> ⚠️ 安全提示：`xml_external_entities on;` 会显著削弱防护，仅在你完全信任上游响应、且确实需要外部实体时才开启。官方提交信息明确说「处理不可信 XML 响应时仍不推荐开启，除非你已慎重考虑风险」。
>
> 本实践涉及读取 `/etc/passwd`，属敏感操作，建议在隔离的测试环境进行，不要在生产环境开启 `on`。若无法本地运行，请按上面源码行号做静态跟踪作为替代。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 2 层防护选择「剥离 systemId」而不是「直接丢弃整个实体声明」？

> **参考答案**：直接丢弃实体会让文档对它的引用 `&xxe;` 变成未定义实体，可能导致解析报错或文档不再 well-formed。改成「内部实体、内容为空」后，实体名依旧存在、引用仍合法（展开为空），既保证文档结构完整，又彻底切断了它指向外部资源的能力。带 publicId 的情况还保留了一条受控的 catalog 解析后门。

**练习 2**：三层防护分别挡住哪类攻击向量？如果只开启 `xml_external_entities on;`，还剩哪几层？

> **参考答案**：第 1 层（`XML_PARSE_NONET`）挡网络向量（SSRF、远程 DoS）；第 2 层（剥离 systemId）挡本地文件读取（信息泄露）；第 3 层（关文档内 catalog）挡 catalog 重定向滥用。开启 `on` 只关闭第 2 层，第 1、3 层仍生效，因此 `http://` 实体仍被挡，但 `file:///` 本地文件读取会重新成为风险。

**练习 3**：`ngx_http_xslt_sax_external_subset`（[第 429-477 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L429-L477)）与 `entity_decl` 防护的职责有何不同？

> **参考答案**：`external_subset` 处理的是 `<!DOCTYPE ... SYSTEM "...">` 指向的**外部 DTD 子集**——它用本地 `conf->dtd` 替换，从不加载远端；`entity_decl` 处理的是内部 DTD 子集 `<!DOCTYPE ... [ ... ]>` 里声明的**外部实体**——它剥离 systemId。两者覆盖了 XML 中两条不同的「加载外部资源」路径。

---

### 4.4 xml_external_entities 指令与配置生命周期

#### 4.4.1 概念说明

`xml_external_entities` 是提交 `4d0e620f9` 新增的一条指令，类型是 `FLAG`（`on`/`off`），作用域覆盖 main/srv/loc 三层，默认 `off`。它就是第 2 层防护的总开关，对应配置结构体里的 `external_entities` 字段。

这条指令从「配置文本」到「运行时影响行为」要经过完整的 nginx 配置生命周期，正好把 u3（配置解析与模块系统）里学过的机制串起来用一遍：

- **指令描述符 `ngx_command_t`**：声明指令名、类型、slot 函数、字段偏移。
- **create_conf**：建空配置时把字段初始化为「未设置」哨兵 `NGX_CONF_UNSET`。
- **merge_conf**：沿 http→server→location 树继承，未设置则取父值或硬编码默认 0。
- **运行时**：在 `entity_decl` 回调里用 `ngx_http_get_module_loc_conf` 取到当前请求的配置，读 `conf->external_entities` 做判定。

这正是 u3-l4 讲过的「offsetof 反射式赋值 + 哨兵 + merge 继承」套路的一个标准实例。

#### 4.4.2 核心流程

```text
配置阶段:
  create_loc_conf:  conf->external_entities = NGX_CONF_UNSET          # 哨兵
  解析到 "xml_external_entities on;":
    ngx_conf_set_flag_slot 用 offsetof 直接写到 conf->external_entities  # 反射赋值
  merge_loc_conf:
    ngx_conf_merge_value(conf->external_entities, prev->external_entities, 0)
    # 子未设(UNSET) -> 取父值；父也未设 -> 取默认 0

运行阶段:
  entity_decl 回调里:
    conf = ngx_http_get_module_loc_conf(r, module)                    # 取当前请求 location 的配置
    if (systemId && !conf->external_entities) { 剥离 }                # 判定
```

#### 4.4.3 源码精读

**字段定义**：

[src/http/modules/ngx_http_xslt_filter_module.c:60-68](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L60-L68) —— location 配置结构体，第 67 行 `ngx_flag_t external_entities;` 就是开关字段（与 `last_modified` 并列）。

**指令描述符**：

[src/http/modules/ngx_http_xslt_filter_module.c:136-141](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L136-L141) —— `xml_external_entities` 指令定义。关键点：

- 类型 `NGX_HTTP_MAIN_CONF|NGX_HTTP_SRV_CONF|NGX_HTTP_LOC_CONF|NGX_CONF_FLAG`：三个 HTTP 作用域都能写，且必须是 `on`/`off` 标志（`NGX_CONF_FLAG`）。
- set 函数 `ngx_conf_set_flag_slot`：直接复用 nginx 通用的 flag slot 解析函数（u3-l4 讲过）。
- `NGX_HTTP_LOC_CONF_OFFSET` + `offsetof(ngx_http_xslt_filter_loc_conf_t, external_entities)`：「写到哪个结构体的哪个字段」用编译期偏移固化，运行时以「基址 + 偏移」反射式定位。

**create_conf 设哨兵**：

[src/http/modules/ngx_http_xslt_filter_module.c:1153-1177](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1153-L1177) —— 第 1174 行 `conf->external_entities = NGX_CONF_UNSET;`，把字段初始化为哨兵，是后续 merge 能判断「用户到底有没有写过这条指令」的前提。

**merge_conf 继承默认值**：

[src/http/modules/ngx_http_xslt_filter_module.c:1180-1210](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1180-L1210) —— 第 1207 行 `ngx_conf_merge_value(conf->external_entities, prev->external_entities, 0);`，三段式：子未设取父、父也未设取默认 `0`（关闭）。这正是「默认安全」的落点——无论配置写没写，最终 `external_entities` 都有一个确定的、默认关闭的值。

**运行时读取**：

[src/http/modules/ngx_http_xslt_filter_module.c:499](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L499) —— `conf = ngx_http_get_module_loc_conf(r, ngx_http_xslt_filter_module);` 取当前请求命中 location 的配置；紧接着第 501 行 `if (systemId && !conf->external_entities)` 就是开关真正发挥作用的地方。配置阶段写进结构体的布尔值，在这里变成一次分支判定。

#### 4.4.4 代码实践

**实践目标**：验证 `xml_external_entities` 的「默认关闭 + 沿配置树继承」语义。

**操作步骤**（源码阅读型实践）：

1. 假设如下配置，推断每个 location 最终的 `external_entities` 值：

   ```nginx
   http {
       # http 层未写 xml_external_entities
       server {
           xml_external_entities on;          # server 层开启
           location /a { }                     # 继承 server -> 应为 on
           location /b { xml_external_entities off; }  # 显式关闭 -> off
       }
       server {
           # server 层也未写
           location /c { }                     # 继承链全 UNSET -> 取默认 0 -> off
       }
   }
   ```

2. 对照 [merge_conf 第 1207 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1207) 的 `ngx_conf_merge_value(conf->external_entities, prev->external_entities, 0)` 验证你的推断。

**需要观察的现象**：`/a` 为 on（继承 server），`/b` 为 off（显式），`/c` 为 off（全链未设取默认 0）。

**预期结果**：与上面一致。这验证了「哨兵 + 三段式 merge」让默认值始终是安全的 off，同时允许上层一次性开启、下层覆盖。

#### 4.4.5 小练习与答案

**练习 1**：如果 `create_conf` 里**不**把 `external_entities` 初始化为 `NGX_CONF_UNSET`，会发生什么？

> **参考答案**：`ngx_pcalloc` 已经把整个结构体清零，所以字段初值是 0，恰好等于「默认关闭」。但 `ngx_conf_merge_value` 依赖 `NGX_CONF_UNSET` 来判断「用户没写过」——若不设哨兵，server 层写的 `on` 将无法被 location 层「未写」时正确继承（因为 0 ≠ `NGX_CONF_UNSET`，会被当作「子已显式设为 off」而不取父值）。设哨兵是为了让 merge 继承链正确工作。

**练习 2**：指令描述符里为什么用 `ngx_conf_set_flag_slot` 而不是自己写一个 set 函数？

> **参考答案**：`on`/`off` 标志是 nginx 最常见的一类指令，通用 `ngx_conf_set_flag_slot` 已经能解析并把结果写进任意结构体的任意字段——靠的就是 `offsetof` 反射式定位（u3-l4）。复用它避免重复代码，符合 nginx「一套 slot 函数服务成百上千条指令」的设计。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一次「端到端追踪」：

**任务**：给定一条恶意 XML 响应（内部 DTD 子集里声明 `file:///` 与 `http://` 两类外部实体），请你：

1. **画出数据流**：从上游响应字节到达 body_filter → `ngx_http_xslt_add_chunk` 喂数据 → libxml2 触发 `entityDecl` 回调 → nginx 改写后回到解析 → 收齐后 `apply_stylesheet` → `ngx_http_xslt_send` 下发。
2. **标注三层防护各自拦截的位置**：在流程图上标出第 1 层（`XML_PARSE_NONET`，[第 400-401 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L400-L401)）、第 2 层（`entity_decl` 剥离 systemId，[第 501-531 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L501-L531)）、第 3 层（catalog，[第 1222-1224 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1222-L1224)）。
3. **回答三个问题**：
   - `file:///` 实体在默认配置下会被展开成什么？（空字符串）
   - `http://` 实体在 `xml_external_entities on;` 下会被加载吗？（不会，仍被 `XML_PARSE_NONET` 挡）
   - 若管理员想让某个带 publicId 的实体通过系统 catalog 解析，应该怎么做？（保持默认 off，在系统层配置 XML catalog，源码注释第 503-509 行说明了这条受控路径）

**交付物**：一张数据流图 + 三层防护标注 + 三个问题的答案。本实践以源码静态分析为主，若你有测试环境可结合 4.3.4 的运行实践验证结论。

---

## 6. 本讲小结

- `ngx_http_xslt_filter_module` 是一条 HTTP filter：在 [postconfiguration](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1230-L1240) 把自己 push 进 header/body 两条 filter 链，header filter 按内容类型与样式表是否配置决定放行或扣留，body filter 累积全部响应块后再做整体 XSLT 转换。
- 响应 XML 用 libxml2 **push parser** 跨块增量解析（[ngx_http_xslt_add_chunk](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L385-L426)），nginx 替换了其中的 **SAX 回调表**（`externalSubset`、`entityDecl` 等），并用 `_private` 反查回请求上下文——这是插入安全逻辑的挂载点。
- XXE 的危险在于解析器会替你加载外部资源（读本地文件、发网络请求）。nginx 默认用**三层防护**：`XML_PARSE_NONET` 禁网络、`entityDecl` 回调剥离 systemId 禁本地文件、关闭文档内 catalog 防 catalog 重定向。
- 核心 [ngx_http_xslt_sax_entity_decl](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L480-L535) 用 `if (systemId && !conf->external_entities)` 决定是否剥离：带 publicId 的改空 systemId、外部通用/参数实体改写为空的内部实体，既保住文档 well-formed 又切断外部加载。
- 新增的 `xml_external_entities` 指令是第 2 层的总开关，经「指令描述符 offsetof + create_conf 哨兵 + merge_conf 三段式」标准配置生命周期落地（[第 136-141、1174、1207 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L136-L141)），默认 off（默认安全），开启会显著削弱防护。
- 本讲是 u6-l6 过滤器链在一个「需要截留 + 增量解析 + 安全加固」的真实模块上的综合应用，也示范了「配置文本 → 结构体字段 → 运行时分支」的完整链路。

---

## 7. 下一步学习建议

- **横向对比其它 body filter**：阅读 `src/http/modules/ngx_http_sub_filter_module.c`（字符串替换）与 `src/http/modules/ngx_http_addition_filter_module.c`（前后追加 body），对比它们与 XSLT 过滤器在「是否需要整篇累积」「是否改 Content-Length」上的差异，加深对 filter 链设计模式的理解。
- **回到变量系统**：本讲的 `xslt_stylesheet` 参数支持 `ngx_http_complex_value_t`（见 [ngx_http_xslt_param](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L1069-L1108)），这依赖 u6-l7 讲的变量系统，可结合复习。
- **深入 libxml2/libxslt**：本讲只触及 SAX 回调与解析选项。若想真正理解 XSLT 转换细节，可阅读 `ngx_http_xslt_apply_stylesheet`（[第 573-710 行](https://github.com/nginx/nginx/blob/4d0e620f9ad4e81dc229ca423fbbf3c2e23b3f83/src/http/modules/ngx_http_xslt_filter_module.c#L573-L710)）并对照 libxslt 官方文档。
- **下一站 upstream**：XSLT 过滤器解析的是**上游响应**，若想理解「上游响应是怎么被收下来、再经 filter 链吐出去的」，请进入第七单元 u7-l1 upstream 框架，重点看 `ngx_event_pipe` 如何与 body filter 衔接。
