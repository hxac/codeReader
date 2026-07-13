# 证书缓存与 OCSP Stapling

## 1. 本讲目标

在上一篇（u8-l1）里，我们把 OpenSSL 接入了 nginx 的事件模型：每条 TLS 连接在握手期间被多轮 `epoll` 唤醒，反复调用 `SSL_do_handshake`，握手成功后把 `c->recv/send` 换成加密版本。本讲回答两个紧随其后的问题：

- **证书从哪里来、怎么缓存？** 一个生产 nginx 可能在 `http {}` 里为成百上千个 `server_name` 各配一份证书，还可能用变量（`ssl_certificate /etc/certs/$server_name.pem;`）按请求动态选证书。如果每次握手都重新 `open + PEM_read` 一遍磁盘文件，性能和 fd 都吃不消。
- **OCSP Stapling 是什么、怎么自动刷新？** 浏览器要校验证书是否被吊销，传统做法是浏览器自己去查 CA 的 OCSP 响应服务器；而 OCSP Stapling 让 nginx 在握手时把「OCSP 响应」直接附带（staple，订书钉）给客户端，既保护隐私又减少往返。这要求 nginx 自己周期性地去拉取并校验 OCSP 响应。

学完本讲你应当能够：

- 说清 nginx 里有**两套** SSL 对象缓存（配置期全局缓存 vs. 运行期按 server 的缓存），以及它们各自的红黑树 + LRU 队列结构。
- 解释 `ssl_certificate` 写成变量时，证书是如何在握手回调里被求值、缓存并装到当前连接上的（动态证书与 SNI 的配合）。
- 描述 OCSP Stapling 的初始化（每张证书一个 `staple` 节点）、握手时的状态回调、以及后台异步拉取 OCSP 响应并定时刷新的完整流程。

## 2. 前置知识

### 2.1 OCSP 与 OCSP Stapling

- **证书吊销**：一张 X.509 证书在到期前可能因私钥泄露等原因被 CA 提前作废。客户端需要一种方式知道「这张证书是否仍有效」。
- **CRL（证书吊销列表）**：CA 定期发布一个被吊销证书的大列表，客户端下载整个列表来查。体积大、更新慢，已很少直接用。
- **OCSP（在线证书状态协议）**：客户端向 CA 的 OCSP 响应器发一个 HTTP 请求，问「这张证书现在有效吗？」，响应器返回 good / revoked / unknown。问题是：客户端每次都去问 CA 既慢又泄露用户访问的域名给 CA。
- **OCSP Stapling（OCSP 装订）**：改由服务器（nginx）自己去拉取并缓存 OCSP 响应，在 TLS 握手时通过 `CertificateStatus`（TLS 1.2）或 `status_request` 扩展把它「钉」在证书后面一起发给客户端。客户端无需再联系 CA。

OCSP 响应本身是 CA 用其私钥签名的 DER 编码数据，带一个 `nextUpdate` 有效期，所以 nginx 必须在它过期前重新拉取。

### 2.2 SNI 与动态证书（回顾 u8-l1）

- **SNI（Server Name Indication）**：客户端在 TLS 握手的 `ClientHello` 里带上目标域名，服务器据此选对应的证书与配置，实现「一个 IP:端口托管多个 HTTPS 站点」。
- nginx 在握手期间通过回调（`client_hello` 回调 / `servername` 回调）拿到 SNI 名字，切换到对应 `server` 块的 `SSL_CTX`。
- 当 `ssl_certificate` 的值**含变量**时，证书路径要等请求/SNI 到了才能确定，nginx 无法在配置阶段一次性装好，于是改用「证书回调」在握手期间动态加载。

### 2.3 复习两张关键结构（u8-l1 已建立）

- 配置层 `ngx_ssl_t`（内含 `SSL_CTX *ctx`），每个 `server` 一份；
- 连接层 `ngx_ssl_connection_t`（内含 `SSL *connection`），每条连接独占。

本讲新增的缓存与 stapling 状态主要挂在配置层的 `ngx_ssl_t` 上，而动态证书的「按连接装证书」发生在连接层。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [src/event/ngx_event_openssl_cache.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c) | SSL 对象缓存的核心实现：红黑树 + LRU 队列、配置期 `ngx_ssl_cache_fetch`、运行期 `ngx_ssl_cache_connection_fetch`、四类对象（cert/pkey/crl/ca）的 create/free/ref 回调表。 |
| [src/event/ngx_event_openssl_stapling.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c) | OCSP Stapling 全部逻辑：每张证书的 `staple` 节点初始化、握手状态回调、异步 OCSP 客户端（拉取/校验/解析/刷新）。 |
| [src/event/ngx_event_openssl.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c) | 调用缓存的两个上层入口：配置期 `ngx_ssl_certificate`、运行期 `ngx_ssl_connection_certificate`。 |
| [src/event/ngx_event_openssl.h](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h) | `ngx_ssl_t` 结构（含 `certs` 数组与 `staple_rbtree`）、缓存对象类型常量。 |
| [src/http/modules/ngx_http_ssl_module.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c) | HTTP 层指令：`ssl_certificate`/`ssl_certificate_key`/`ssl_certificate_cache`、变量证书检测与 cert_cb 注册、`ssl_stapling*` 指令。 |
| [src/http/ngx_http_request.c](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c) | 变量证书的 cert_cb `ngx_http_ssl_certificate`：在握手期间求值变量、调用连接级加载。 |

## 4. 核心概念与源码讲解

### 4.1 两套证书缓存：配置期全局缓存 vs. 运行期按 server 缓存

#### 4.1.1 概念说明

nginx 里「缓存 SSL 对象」这件事由同一个文件 `ngx_event_openssl_cache.c` 实现，但对外暴露了**两个入口**，服务于两种截然不同的场景：

| 维度 | 配置期全局缓存 | 运行期按 server 缓存 |
|------|----------------|----------------------|
| 入口函数 | `ngx_ssl_cache_fetch` | `ngx_ssl_cache_connection_fetch` |
| 谁调用 | `ngx_ssl_certificate`（解析静态 `ssl_certificate` 指令时） | `ngx_ssl_connection_certificate`（变量证书的 cert_cb 握手时） |
| 缓存实例 | 全局唯一的 `ngx_openssl_cache_module` 核心模块配置 | 每个 `server` 自己的 `sscf->certificate_cache`（由 `ssl_certificate_cache` 指令创建） |
| 生命周期 | 挂在 cycle 的内存池上，reload 时复用 | LRU，受 `max`/`valid`/`inactive` 控制 |
| 容量参数 | `max=0, valid=0, inactive=0`（不限容量，靠 reload 继承） | 用户配置：`max=N valid=T1 inactive=T2` |

为什么要分两套？

- **静态证书**（`ssl_certificate /path/a.pem;` 路径不含变量）只在配置阶段加载一次，装到 `SSL_CTX` 上，之后所有连接共享。这类对象缓存只需在 reload 时能「按 mtime/uniq 复用旧 cycle 已解析好的对象」即可，不需要 LRU 淘汰——因为数量在配置阶段就确定了。
- **动态证书**（`ssl_certificate /certs/$server_name.pem;` 路径含变量）路径在握手时才确定，可能成千上万种取值（每个 SNI 名字一个文件）。这时必须有 LRU + 有效期机制，否则缓存会无限膨胀、或长期持有已更换的旧证书。

#### 4.1.2 核心流程

两套缓存共用同一组底层数据结构，差别在「节点从哪个池分配、何时淘汰」：

```
缓存结构 ngx_ssl_cache_t
├── rbtree          红黑树：按 (hash, type, id) 三元组定位缓存节点
├── expire_queue    LRU 队列：按 accessed 时间排序，用于淘汰
├── max / valid / inactive   容量与有效期参数（仅运行期缓存用）
└── inheritable     是否允许 reload 时从 old_cycle 继承（仅全局缓存用）

缓存节点 ngx_ssl_cache_node_t
├── node.key        = murmur_hash2(id)       红黑树第一级键
├── id              (type, len, data)        缓存键：对象类型 + 路径/数据
├── type            指向 create/free/ref 回调表
├── value           已解析好的 OpenSSL 对象（X509 链 / EVP_PKEY / ...）
├── created/accessed
└── mtime / uniq    文件的修改时间与 inode 唯一号（用于判断是否要重读）
```

- **配置期 `ngx_ssl_cache_fetch`**：先在新 cycle 的红黑树里查；命中且无需失效就直接 `ref` 返回引用；未命中则尝试从 old_cycle 继承（要求文件 mtime/uniq 没变）；都没有才调 `create` 真正解析文件；最后插入红黑树。节点从 `cf->pool` 分配，随 cycle 长存。
- **运行期 `ngx_ssl_cache_connection_fetch`**：先查红黑树；命中则按 `valid` 有效期或文件 mtime/uniq 变化决定是否重读；未命中则 `create` 并插入，节点用 `ngx_alloc` 从堆分配；插入后做 LRU 淘汰。

#### 4.1.3 源码精读

缓存的创建入口 `ngx_ssl_cache_init`，按容量参数初始化红黑树、LRU 队列，并注册内存池销毁时的清理回调：

[src/event/ngx_event_openssl_cache.c:1119-1149](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L1119-L1149) —— `ngx_ssl_cache_init` 初始化 `rbtree` 与 `expire_queue`，记下 `max/valid/inactive`，并挂上 `ngx_ssl_cache_cleanup`。

四类对象的回调表，以「对象类型索引」为数组下标：

[src/event/ngx_event_openssl_cache.c:164-185](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L164-L185) —— `ngx_ssl_cache_types[]`：索引 0=cert、1=pkey、2=crl、3=ca，每项是 `{create, free, ref}` 三回调。类型索引常量定义在头文件中：

[src/event/ngx_event_openssl.h:226-231](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L226-L231) —— `NGX_SSL_CACHE_CERT=0`、`PKEY=1`、`CRL=2`、`CA=3`，外加一个高位标志 `NGX_SSL_CACHE_INVALIDATE=0x80000000`（与索引按位或，表示「强制重读，忽略缓存」）。

> 注意区分两组同名前缀的常量：`NGX_SSL_CACHE_CERT..CA`（头文件，对象**类别**）与 cache.c 内部的 `NGX_SSL_CACHE_PATH/DATA/ENGINE/STORE`（cache.c:17-20，缓存键的 **id.type** 子类型，表示「这个 id 是个文件路径 / 内联 data / engine URI / OSSL_STORE URI」）。前者是数组的下标，后者占 `id->type` 的 2 个 bit。

红黑树查找用「hash → type → 字节序 id」三级比较，逐级缩小范围，碰撞时 `ngx_memn2cmp` 兜底：

[src/event/ngx_event_openssl_cache.c:490-539](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L490-L539) —— `ngx_ssl_cache_lookup`：先比 `hash`，再比 `type` 指针，最后比 `id.data` 字节串。

#### 4.1.4 代码实践

1. **实践目标**：看清「全局缓存」这一份实例从哪里来。
2. **操作步骤**：阅读 `ngx_openssl_cache_module`（cache.c:148-161）这个 CORE 模块的定义，它的 `create_conf` 是 `ngx_openssl_cache_create_conf`（cache.c:1092-1105），里面调 `ngx_ssl_cache_init(cycle->pool, 0, 0, 0)`——也就是说全局缓存的 `max=valid=inactive=0`。再看指令表只有一个 `ssl_object_cache_inheritable`（cache.c:128-138），默认值为 1（cache.c:1108-1116）。
3. **需要观察的现象**：全局缓存没有任何容量参数，只有 `inheritable` 一个开关——印证「配置期缓存不靠 LRU 淘汰，靠 reload 继承」。
4. **预期结果**：你能在源码中确认全局缓存与 `ssl_certificate_cache` 指令创建的缓存是**两个独立**的 `ngx_ssl_cache_t` 实例。
5. 待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`NGX_SSL_CACHE_INVALIDATE = 0x80000000` 为什么要放在最高位、并与索引「按位或」而不是相加？
**答案**：合法的对象类别索引只有 0..3，占低位 2 bit；把「强制重读」标志放在最高位可与之共存于同一个 `index` 参数里，下游用 `index & NGX_SSL_CACHE_INVALIDATE` 取标志、`index & ~NGX_SSL_CACHE_INVALIDATE` 取索引（见 cache.c:206-207、321-322），互不干扰。

**练习 2**：全局缓存的节点从哪个内存池分配？运行期缓存的节点呢？为什么不同？
**答案**：全局缓存节点从 `cf->pool`（配置池，随 cycle 长存）分配（cache.c:281）；运行期缓存节点用 `ngx_alloc` 从堆分配、`ngx_free` 释放（cache.c:406、1207）。因为全局缓存随 cycle 生灭、靠池自动回收；运行期缓存要在运行中频繁插入淘汰，必须能独立 free 单个节点，故用堆。

### 4.2 配置期全局缓存：ngx_ssl_cache_fetch

#### 4.2.1 概念说明

当 `ssl_certificate` 写的是普通路径（不含变量），nginx 在配置阶段就把它解析好装到 `SSL_CTX`。`ngx_ssl_certificate`（openssl.c）把「读 PEM 文件成 X509 链」这件事委托给 `ngx_ssl_cache_fetch`，从而获得两个好处：

- **reload 不重复解析**：reload 时构造新 cycle，对同一文件路径，只要 mtime 和 inode 号没变，就直接复用旧 cycle 红黑树里已解析好的 `STACK_OF(X509)`，省掉昂贵的 PEM 解析。
- **跨指令共享**：多个 server 引用同一证书文件时，全局缓存按路径去重，只解析一次。

#### 4.2.2 核心流程

```
ngx_ssl_cache_fetch(cf, NGX_SSL_CACHE_CERT, &err, cert_path, NULL)
  ├── ngx_ssl_cache_init_key：判定 id.type（PATH / DATA / ENGINE / STORE）
  ├── cache = 全局 ngx_openssl_cache_module 配置
  ├── hash = murmur_hash2(id)
  ├── cn = ngx_ssl_cache_lookup(当前 cache, ...)        # 1) 查新 cycle
  │       命中且 !invalidate → type->ref() 返回引用
  ├── 若未命中：
  │       stat 文件得 mtime/uniq
  │       查 old_cycle 缓存（要求 inheritable && !invalidate）
  │         命中且 (uniq==cn->uniq && mtime==cn->mtime) → ref 旧值   # 2) reload 继承
  ├── 仍没有 → type->create() 真正读 PEM                                # 3) 解析
  └── 新建节点，插入新 cache 红黑树，ref() 返回
```

关键点：`ref` 回调负责「拿一个能独立持有引用的对象副本」。对证书链而言，`create` 解析出一条 `STACK_OF(X509)`，每次 fetch 返回一条 `sk_X509_dup` 的浅拷贝并对每张 X509 调 `X509_up_ref`，这样调用方 `free` 自己那份时不会影响缓存里的原件。

#### 4.2.3 源码精读

`ngx_ssl_cache_fetch` 主体——查新缓存 → 查旧缓存 → create → 插入：

[src/event/ngx_event_openssl_cache.c:188-303](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L188-L303) —— 注意 252-271 行的 old_cycle 继承分支：只有 `old_cache->inheritable` 为真且未强制失效时才查旧缓存；对文件类对象（非 DATA）还要求 `uniq == cn->uniq && mtime == cn->mtime`（cache.c:263-267），即「文件没换」才复用解析结果。

证书链的解析与引用计数：

[src/event/ngx_event_openssl_cache.c:573-647](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L573-L647) —— `ngx_ssl_cache_cert_create`：先 `PEM_read_bio_X509_AUX` 读叶子证书，再循环 `PEM_read_bio_X509` 读完整信任链，读到 `PEM_R_NO_START_LINE` 视为正常 EOF（cache.c:619-624）。

[src/event/ngx_event_openssl_cache.c:657-683](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L657-L683) —— `ngx_ssl_cache_cert_ref`：`sk_X509_dup` 浅拷贝链表后，对每张证书 `X509_up_ref`，保证调用方与缓存各持有一份独立引用。

上层调用方 `ngx_ssl_certificate`（处理静态 `ssl_certificate`）：

[src/event/ngx_event_openssl.c:474-544](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L474-L544) —— 用 `ngx_ssl_cache_fetch(cf, NGX_SSL_CACHE_CERT, ...)` 拿到证书链，`sk_X509_shift` 取叶子装到 `SSL_CTX`，并用 `X509_set_ex_data(x509, ngx_ssl_certificate_name_index, cert->data)`（openssl.c:512）把「证书文件名」贴到 X509 上——这个 ex_data 正是后面 stapling 回调反查证书归属的钥匙。

密钥与证书不匹配时的「强制重读」自愈：当证书和密钥文件更新不同步、缓存里是半新半旧的一对时，`SSL_CTX_use_PrivateKey` 会报 `X509_R_KEY_VALUES_MISMATCH`，nginx 把 `mask` 置为 `NGX_SSL_CACHE_INVALIDATE` 后 `goto retry`，强制缓存重读：

[src/event/ngx_event_openssl.c:608-627](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L608-L627) —— `mask == 0` 时遇到 key mismatch 就 `mask = NGX_SSL_CACHE_INVALIDATE; goto retry;`，最多重试一次。

#### 4.2.4 代码实践

1. **实践目标**：体会「reload 复用解析结果」如何取决于 mtime/uniq。
2. **操作步骤**：
   - 准备一个最小 HTTPS server，配 `ssl_certificate` 与 `ssl_certificate_key`。
   - 用 `--with-debug` 编译 nginx（见 u10-l5），`error_log` 开 `debug` 级别。
   - `nginx -s reload` 两次：第一次 reload 后**不要**改证书文件；第二次 reload 前 `touch` 一下证书文件（更新 mtime 但内容不变）。
3. **需要观察的现象**：第一次 reload（mtime 未变）时，debug 日志里不应出现重新读 PEM 的痕迹；`touch` 后的 reload 会因为 mtime 变化而触发重读。
4. **预期结果**：能在 debug 日志中看到 `update cached ssl object`（cache.c:375-376，运行期路径）或没有重读日志（配置期路径直接复用 old_cycle）的差异。
5. 待本地验证（具体日志格式取决于 OpenSSL/nginx 版本）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 old_cycle 继承分支里，对 `NGX_SSL_CACHE_DATA`（内联 `data:` 形式）类对象**不**校验 mtime/uniq，直接复用（cache.c:258-260）？
**答案**：内联 data 不是文件，没有 mtime/uniq 的概念；它的「内容」就是 id 本身的一部分（`data:...` 字符串），id 相同即内容相同，故无需、也无法用文件元数据判定是否变化，直接复用安全。

**练习 2**：`ngx_ssl_certificate` 里 `X509_set_ex_data(x509, ngx_ssl_certificate_name_index, cert->data)` 存的是 `cert->data`（指向配置里的路径字符串）。为什么存指针而不是拷贝？
**答案**：路径字符串来自配置池（`ngx_str_t *cert` 由 `ngx_conf_set_str_array_slot` 写入，随 cycle 长存），生命周期不短于 X509 对象，存指针零拷贝且不会悬空；后续 stapling 用它做日志输出与作为 OCSP 请求的标识，不需要修改。

### 4.3 动态证书与 SNI：cert_cb + ngx_ssl_cache_connection_fetch

#### 4.3.1 概念说明

当 `ssl_certificate` 含变量（典型用法：`ssl_certificate /etc/certs/$server_name.pem;`），路径在配置阶段无法确定，于是 nginx 走另一条路：

1. **配置阶段**（`ngx_http_ssl_module` 的 merge）：`ngx_http_ssl_compile_certificates` 检测到路径含变量，把它们编译成 `ngx_http_complex_value_t`（即 `sscf->certificate_values`），并**不**在此时装证书，而是注册一个 OpenSSL 「证书回调」`SSL_CTX_set_cert_cb(ctx, ngx_http_ssl_certificate, conf)`。
2. **握手阶段**：OpenSSL 在需要确定本连接用哪张证书时回调 `ngx_http_ssl_certificate`。此时 SNI 名字已知，回调函数对每条 `complex_value` 求值得到真实路径，再按路径加载证书装到**这条连接**上（`SSL_use_certificate`，注意是 `SSL_` 而非 `SSL_CTX_`）。
3. 由于路径可能成千上万，加载结果存到 `sscf->certificate_cache`（由 `ssl_certificate_cache` 指令创建的运行期 LRU 缓存）。

#### 4.3.2 核心流程

```
配置阶段(http_ssl_module.c:812-826):
  conf->certificate_values 非空?
    是 → SSL_CTX_set_cert_cb(ctx, ngx_http_ssl_certificate, conf)   // 安装回调，不装证书
    否 → ngx_ssl_certificates(...)                                  // 走 4.2 的静态路径

握手阶段:
  OpenSSL 触发 cert_cb → ngx_http_ssl_certificate(ssl_conn, sscf)
    ├── ngx_http_alloc_request(c)        // 临时建一个 request 上下文，仅为求值变量
    ├── for 每对 (cert_cv, key_cv):
    │     ngx_http_complex_value(r, cert_cv, &cert)   // 求值出真实路径，如 /etc/certs/a.com.pem
    │     ngx_http_complex_value(r, key_cv,  &key)
    │     ngx_ssl_connection_certificate(c, r->pool, &cert, &key,
    │                                     sscf->certificate_cache, sscf->passwords)
    │       ├── ngx_ssl_cache_connection_fetch(sscf->certificate_cache, CERT, ...)
    │       │     命中(valid 内/文件未变) → ref 返回；否则 create 重读；LRU 淘汰
    │       ├── SSL_use_certificate(c->ssl->connection, x509)   // 装到本连接
    │       └── SSL_use_PrivateKey(...)                          // 密钥同理，mismatch 则 invalidate 重试
    └── ngx_http_free_request(r, 0)     // 求值完毕即丢弃临时 request
```

要点：变量证书是在**握手期间**、对**当前连接**动态装载的；缓存按 server 共享、按 (类型,路径) 去重，并在 `valid` 有效期或文件变化时重读。

#### 4.3.3 源码精读

配置阶段：变量检测与 cert_cb 安装：

[src/http/modules/ngx_http_ssl_module.c:808-837](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L808-L837) —— `ngx_http_ssl_compile_certificates` 编译证书/密钥表达式；若结果 `conf->certificate_values` 非空，则 `SSL_CTX_set_cert_cb(conf->ssl.ctx, ngx_http_ssl_certificate, conf)`（http_ssl_module.c:818），否则走静态 `ngx_ssl_certificates`。

指令定义（注意 `ssl_certificate` 用的是 `str_array_slot`，允许写多份）：

[src/http/modules/ngx_http_ssl_module.c:99-118](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L99-L118) —— `ssl_certificate` / `ssl_certificate_key` / `ssl_certificate_cache` 三条指令；`ssl_certificate_cache` 由专门的 `ngx_http_ssl_certificate_cache` 解析。

`ssl_certificate_cache` 指令解析 `max= / valid= / inactive= / off`：

[src/http/modules/ngx_http_ssl_module.c:1061-1150](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/modules/ngx_http_ssl_module.c#L1061-L1150) —— 默认 `inactive=10s, valid=60s`，`max` 必填；最终 `ngx_ssl_cache_init(cf->pool, max, valid, inactive)` 创建按 server 的缓存存入 `sscf->certificate_cache`。设为 `off` 时 `sscf->certificate_cache = NULL`，表示不缓存（每次握手都重读）。

握手阶段的 cert_cb：

[src/http/ngx_http_request.c:1042-1106](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/http/ngx_http_request.c#L1042-L1106) —— `ngx_http_ssl_certificate`：先 `ngx_http_alloc_request(c)` 造一个临时请求只为求值变量（request.c:1057），对每对证书/密钥 `complex_value` 求值出路径（request.c:1072、1079），再 `ngx_ssl_connection_certificate(... sscf->certificate_cache ...)`（request.c:1086-1088），最后 `ngx_http_free_request`（request.c:1095）。

连接级证书加载，调用运行期缓存：

[src/event/ngx_event_openssl.c:636-730](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.c#L636-L730) —— `ngx_ssl_connection_certificate`：用 `ngx_ssl_cache_connection_fetch(cache, pool, NGX_SSL_CACHE_CERT, ...)` 拿链（openssl.c:651），`SSL_use_certificate`（openssl.c:666）装到**连接**而非 ctx；密钥同理（openssl.c:693、706）。注意 711-720 同样有 mismatch → invalidate 重试的自愈逻辑。

运行期缓存的核心 `ngx_ssl_cache_connection_fetch`：

[src/event/ngx_event_openssl_cache.c:306-451](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L306-L451) —— 与配置期版本的关键差异：
- cache.c:330-332：若 `cache == NULL`（即 `ssl_certificate_cache off`），直接 `create` 返回，不缓存；
- cache.c:347-349：命中且 `now - cn->created <= cache->valid` 直接复用（有效期未过）；
- cache.c:353-370：PATH 类对象即便过了 `valid`，也会先 `ngx_file_info` 比较 mtime/uniq，没变就不重读（避免无谓解析）；
- cache.c:434-438：插入后调 `ngx_ssl_cache_expire` 做 LRU 淘汰，`cache->current >= cache->max` 时强制淘汰。

LRU 淘汰逻辑——从队尾（最久未访问）开始，超过 `inactive` 时长才删：

[src/event/ngx_event_openssl_cache.c:542-570](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_cache.c#L542-L570) —— `ngx_ssl_cache_expire`：取 `ngx_queue_last`（最旧），`now - accessed > inactive` 则 `node_free` 并 `current--`。

#### 4.3.4 代码实践

1. **实践目标**：用变量化 `ssl_certificate` 按 `$server_name` 选证书，并验证握手时走 cert_cb + 运行期缓存。
2. **操作步骤**：
   - 准备两套自签证书（仅用于本地实验），分别命名 `a.test.pem`/`a.test.key`、`b.test.pem`/`b.test.key`，放在 `/etc/certs/`。
   - 写最小配置（示例代码，非项目原有）：
     ```nginx
     events { worker_connections 1024; }
     http {
         server {
             listen 443 ssl;
             server_name a.test b.test;
             ssl_certificate     /etc/certs/$server_name.pem;
             ssl_certificate_key /etc/certs/$server_name.key;
             ssl_certificate_cache max=100 valid=60 inactive=5;
         }
     }
     ```
   - `nginx -t` 校验后启动；用 `openssl s_client -connect 127.0.0.1:443 -servername a.test` 与 `-servername b.test` 各连一次，看返回证书 CN。
3. **需要观察的现象**：两次连接返回不同证书；同一 server_name 第二次连接不应重新读盘（命中 `certificate_cache`）。开 `--with-debug` 后可在日志看到 `ssl cert: "/etc/certs/a.test.pem"`（request.c:1076-1077）与命中/未命中缓存的差异。
4. **预期结果**：成功按 SNI 返回对应证书；`a.test` 第二次连接更快（缓存命中）。
5. 待本地验证（需要本地 DNS 解析或 `/etc/hosts` 把 a.test/b.test 指向 127.0.0.1，并需 root 放证书）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ngx_http_ssl_certificate` 里要 `ngx_http_alloc_request` 又很快 `ngx_http_free_request`？这个临时 request 的作用是什么？
**答案**：变量的求值（`ngx_http_complex_value`）依赖 `ngx_http_request_t` 上下文——`$server_name` 等变量的 `get_handler` 需要从 `r` 里取值。但证书回调发生在握手早期、还没有真正的 HTTP 请求，所以临时构造一个 `r` 仅用于求值，求完即弃，避免污染后续真实请求的状态。

**练习 2**：`ssl_certificate_cache` 设为 `off`（即 `sscf->certificate_cache = NULL`）时，每次握手会发生什么？性能上有何影响？
**答案**：`ngx_ssl_cache_connection_fetch` 在 cache.c:330-332 检测到 `cache == NULL` 直接调 `create`，即每次握手都重新 `open + PEM_read` 证书和密钥文件。功能正确但代价高（磁盘 I/O + PEM 解析 + fd 占用），所以只在证书数量极少或调试时才关缓存。

### 4.4 OCSP Stapling：初始化、回调与异步刷新

#### 4.4.1 概念说明

OCSP Stapling 的目标是：握手时把一份**有效**的 OCSP 响应随证书一起发给客户端。难点在于 OCSP 响应有有效期，nginx 必须自己周期性去 CA 的 OCSP 响应器拉取、校验签名、并在过期前刷新。整个过程分三段：

- **初始化（配置阶段）**：为每张证书建一个 `ngx_ssl_stapling_t` 节点，找到它的签发者（issuer）与 OCSP 响应器 URL（从证书 AIA 扩展或 `ssl_stapling_responder` 指令取），登记进 `ssl->staple_rbtree` 红黑树（以 X509 指针为键）。
- **握手回调（运行时）**：OpenSSL 通过 `SSL_CTX_set_tlsext_status_cb` 注册的 `ngx_ssl_certificate_status_callback` 在客户端请求状态时被调，从红黑树查到当前证书的 `staple`，若有有效响应就附带给客户端，同时**懒触发**后台刷新。
- **后台刷新（运行时）**：`ngx_ssl_stapling_update` 在响应接近过期时启动一个**异步 OCSP 客户端**（自己发 HTTP 请求到响应器），拿到响应校验后更新 `staple->staple`，并安排下一次刷新时间。

此外还有一个独立功能 `ssl_ocsp`（客户端侧 OCSP 校验），它与 stapling 共用 OCSP 客户端代码但用途相反——用于校验**客户端**证书，本讲只聚焦 stapling。

#### 4.4.2 核心流程

```
配置阶段:
  ngx_ssl_stapling(cf, ssl, file, responder, verify)
    for 每张 ssl->certs[k]:
       ngx_ssl_stapling_certificate(...)
         ├── 新建 staple，node.key = (uintptr_t)cert   # 以 X509 指针为键
         ├── 插入 ssl->staple_rbtree
         ├── 取 issuer：先在 extra_chain 里找，找不到用 X509_STORE 查
         ├── 若给了 ssl_stapling_file → 直接从文件读 OCSP 响应（DER），valid=∞
         └── 否则取 responder URL：
               ssl_stapling_responder 指令优先；否则从证书 AIA 扩展取
    SSL_CTX_set_tlsext_status_cb(ctx, ngx_ssl_certificate_status_callback)

握手时（客户端带 status_request 扩展）:
  ngx_ssl_certificate_status_callback
    ├── cert = SSL_get_certificate(ssl_conn)
    ├── staple = ngx_ssl_stapling_lookup(ssl, cert)   # 按 X509 指针查红黑树
    ├── 若 staple->staple.len && staple->valid >= now:
    │     OPENSSL_malloc + memcpy 一份响应           # OpenSSL 会自己 free，故拷贝
    │     SSL_set_tlsext_status_ocsp_resp(ssl_conn, p, len)
    └── ngx_ssl_stapling_update(staple)              # 懒触发后台刷新

后台刷新:
  ngx_ssl_stapling_update(staple)
    若 host 为空 / 正在 loading / refresh 未到 → return
    staple->loading = 1
    ctx = ngx_ssl_ocsp_start()        # 建独立内存池的异步 OCSP 客户端
    填入 cert/issuer/chain/responder/resolver...
    ctx->handler = ngx_ssl_stapling_ocsp_handler
    ngx_ssl_ocsp_request(ctx)         # 解析域名 → connect → 发请求 → 读响应 → 校验

  ngx_ssl_stapling_ocsp_handler(ctx)   # 响应回来
    ngx_ssl_ocsp_verify(ctx) 校验签名与状态
    status == GOOD → 拷贝响应到 staple->staple，staple->valid = ctx->valid
    计算 refresh = max(min(valid-300, now+3600), now+300)
    staple->loading = 0
```

刷新时刻的计算是本模块的精髓（stapling.c:744）：

\[
t_{\text{refresh}} \;=\; \max\!\bigl(\min(\text{valid}-300,\; \text{now}+3600),\; \text{now}+300\bigr)
\]

含义：「在响应过期前 5 分钟刷新；但若离过期还很远，最多隔 1 小时也要刷一次；若响应很快就要过期（不到 10 分钟），至少间隔 5 分钟再刷，避免频繁请求」。三个边界分别由 `valid-300`、`now+3600`、`now+300` 决定。

#### 4.4.3 源码精读

配置层 `ngx_ssl_t` 里挂着 stapling 的红黑树：

[src/event/ngx_event_openssl.h:104-113](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl.h#L104-L113) —— `struct ngx_ssl_s` 含 `certs` 数组（所有已加载证书）与 `staple_rbtree`/`staple_sentinel`。stapling 以 X509 指针为键挂在每 server 的这棵树上。

初始化主入口，遍历每张证书：

[src/event/ngx_event_openssl_stapling.c:198-218](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L198-L218) —— `ngx_ssl_stapling`：对 `ssl->certs` 每张证书调 `ngx_ssl_stapling_certificate`，最后 `SSL_CTX_set_tlsext_status_cb(ssl->ctx, ngx_ssl_certificate_status_callback)` 注册握手回调。

单张证书的 staple 节点构造：

[src/event/ngx_event_openssl_stapling.c:221-296](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L221-L296) —— `ngx_ssl_stapling_certificate`：`staple->node.key = (ngx_rbtree_key_t) cert`（stapling.c:242，把 X509 指针当键），插入红黑树；记下 `ssl_ctx`、`timeout=60000`、`cert`、`name`（从证书 ex_data 取文件名，stapling.c:262-263）；分支：给了 `file` 走文件模式，否则依次找 issuer、responder。

`ssl_stapling_file` 模式——直接从本地 DER 文件读静态 OCSP 响应，`valid` 设为最大值（永不过期，只能靠手动换文件）：

[src/event/ngx_event_openssl_stapling.c:299-363](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L299-L363) —— `d2i_OCSP_RESPONSE_bio` 读 DER，`i2d_OCSP_RESPONSE` 重新编码拷贝到 `ngx_alloc` 的缓冲，`staple->valid = NGX_MAX_TIME_T_VALUE`（stapling.c:353）。

issuer 查找——先在 extra chain 里 `X509_check_issued` 找签发者，找不到再用 `X509_STORE` 查，都没有则 `NGX_DECLINED`（该证书跳过 stapling）：

[src/event/ngx_event_openssl_stapling.c:366-447](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L366-L447) —— 注意 stapling.c:430-436，找不到 issuer 时记 WARN 日志并返回 `NGX_DECLINED`，对应配置提示 `"ssl_stapling" ignored, issuer certificate not found`。

responder URL 解析——指令优先，否则从证书 AIA 扩展 `X509_get1_ocsp` 取，要求 `http://` 前缀，用 `ngx_parse_url` 拆成 host/uri/port：

[src/event/ngx_event_openssl_stapling.c:450-544](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L450-L544) —— stapling.c:463 `X509_get1_ocsp(staple->cert)` 从证书取 AIA 里的 OCSP URL。

握手状态回调：

[src/event/ngx_event_openssl_stapling.c:574-628](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L574-L628) —— `ngx_ssl_certificate_status_callback`：`SSL_get_certificate` 取当前证书（stapling.c:592），`ngx_ssl_stapling_lookup` 查红黑树（stapling.c:601）；若有有效响应（`staple->staple.len && staple->valid >= ngx_time()`），`OPENSSL_malloc`+`ngx_memcpy` 拷贝一份交给 `SSL_set_tlsext_status_ocsp_resp`（stapling.c:612-620，拷贝是因为 OpenSSL 会自行 free 这块内存）；最后无条件调 `ngx_ssl_stapling_update`（stapling.c:625）。

懒触发后台刷新：

[src/event/ngx_event_openssl_stapling.c:655-696](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L655-L696) —— `ngx_ssl_stapling_update`：三个短路条件——`host.len==0`（没有 responder，如纯文件模式）、`loading`（已有刷新在进行）、`refresh >= now`（还没到刷新时刻）（stapling.c:660-661）；否则置 `loading=1`，`ngx_ssl_ocsp_start` 建异步客户端，填入证书与 responder 信息，`handler = ngx_ssl_stapling_ocsp_handler`，调 `ngx_ssl_ocsp_request`。

异步 OCSP 客户端——自带独立内存池，仿 HTTP/1 请求：

[src/event/ngx_event_openssl_stapling.c:1301-1336](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L1301-L1336) —— `ngx_ssl_ocsp_start`：`ngx_create_pool(2048, log)` 建临时池，OCSP 客户端的所有状态（连接、缓冲、响应）都挂在这个池上，完成后整池销毁。

[src/event/ngx_event_openssl_stapling.c:1398-1455](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L1398-L1455) —— `ngx_ssl_ocsp_request`：先 `ngx_ssl_ocsp_create_request` 构造 OCSP 请求体，若有 resolver 则异步解析 responder 域名（stapling.c:1411-1449），解析完（或直接有地址）走 `ngx_ssl_ocsp_connect`。后续 connect→write→read→parse 均由事件驱动，与 u8-l1 描述的非阻塞模式一致。

响应回来后的校验与刷新时间计算：

[src/event/ngx_event_openssl_stapling.c:699-755](https://github.com/nginx/nginx/blob/18ccebb1a889eb6989c64754f4f9b2512d58a491/src/event/ngx_event_openssl_stapling.c#L699-L755) —— `ngx_ssl_stapling_ocsp_handler`：`ngx_ssl_ocsp_verify` 校验响应签名与状态（stapling.c:709），要求 `status == V_OCSP_CERTSTATUS_GOOD`（stapling.c:713）；把响应拷贝到 `staple->staple`（用 `ngx_alloc` 而非池，因为要跨请求长存，stapling.c:722-735）；`staple->valid = ctx->valid`；stapling.c:744 计算下一次刷新时刻 `refresh = ngx_max(ngx_min(ctx->valid - 300, now + 3600), now + 300)`；失败时（error 分支）`refresh = now + 300`，5 分钟后重试。

#### 4.4.4 代码实践

1. **实践目标**：开启 OCSP Stapling，跟踪从初始化、握手附带到后台刷新的完整链路。
2. **操作步骤**：
   - 用一张**真实**的 Let's Encrypt 类证书（其证书链含 AIA OCSP URL，且签发者在 `ssl_trusted_certificate`/chain 里能找到），配置：
     ```nginx
     server {
         listen 443 ssl;
         server_name your.domain;
         ssl_certificate     fullchain.pem;
         ssl_certificate_key privkey.pem;
         ssl_stapling on;
         ssl_stapling_verify on;
         resolver 8.8.8.8 valid=300s;     # 后台刷新 OCSP 需要解析 responder 域名
     }
     ```
   - `nginx -t` 启动后，用 `openssl s_client -connect your.domain:443 -status` 连接，观察输出里的 `OCSP Response Status`。
   - 开 `--with-debug`，`error_log` 设 `debug`，重启 nginx。
3. **需要观察的现象**：
   - 首次连接可能尚无 staple（响应还在后台拉取），`-status` 显示 `no OCSP response`；等待几十秒后再次连接应显示 `OCSP Response Status: successful`。
   - debug 日志中可看到 `SSL certificate status callback`（stapling.c:587）、`ssl ocsp request`（stapling.c:1403）、`requesting certificate status`（stapling.c:1333 action）等。
4. **预期结果**：握手能附带有效 OCSP 响应；日志显示后台 OCSP 客户端周期性发起请求。
5. 待本地验证（必须用真实可解析的域名与有效证书；自签证书通常没有 AIA OCSP URL，会触发 stapling.c:464-470 的 `ignored` 告警）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ngx_ssl_certificate_status_callback` 里把 OCSP 响应交给 OpenSSL 前要用 `OPENSSL_malloc` 拷贝一份（stapling.c:612-618），而不是直接传 `staple->staple.data`？
**答案**：OpenSSL 在握手结束/连接释放时会自行 `OPENSSL_free` 这块缓冲。若直接传缓存内部指针，缓存里的响应会被 free 掉导致悬空与重复释放。拷贝一份把所有权移交 OpenSSL，缓存原件不受影响。

**练习 2**：stapling 红黑树为什么用「X509 指针」作键（stapling.c:242、639），而不是用证书的序列号或指纹？
**答案**：握手回调里 `SSL_get_certificate` 拿到的就是配置阶段装进去的那张 X509 对象指针，两者天然相等，O(1) 即可关联到 staple；用序列号/指纹则需要额外计算与字符串比较，既慢又无必要。前提是同一 X509 对象在 cycle 生命周期内地址不变——nginx 通过 `ssl->certs` 数组持有这些 X509（见 openssl.c:543）保证不被释放。

**练习 3**：刷新时刻公式 `max(min(valid-300, now+3600), now+300)`（stapling.c:744）三段分别防什么？
**答案**：`valid-300` 争取在过期前 5 分钟刷新；`now+3600` 限制即便证书有效期很长，最多 1 小时也刷一次（及时感知吊销）；`now+300` 保证至少间隔 5 分钟，避免响应短暂失败时无限重试把 OCSP 响应器打爆。

## 5. 综合实践

把本讲四个模块串起来：在一个 `server` 上同时启用**变量化动态证书**与 **OCSP Stapling**，观察它们如何协作。

设计如下任务（示例配置，非项目原有）：

```nginx
events { worker_connections 1024; }
http {
    resolver 8.8.8.8 valid=300s;

    server {
        listen 443 ssl;
        server_name a.domain b.domain;

        # 4.3：变量化证书，按 SNI 选文件
        ssl_certificate     /etc/certs/$ssl_server_name.pem;
        ssl_certificate_key /etc/certs/$ssl_server_name.key;
        ssl_certificate_cache max=200 valid=120 inactive=10;

        # 4.4：开启 stapling（每张动态证书的 staple 都按 X509 指针登记）
        ssl_stapling on;
        ssl_stapling_verify on;
        ssl_trusted_certificate /etc/certs/ca-bundle.crt;
    }
}
```

请回答并验证：

1. **协作点 1（4.3 + 4.4）**：变量证书是在握手 cert_cb 里、对**每条连接**用 `SSL_use_certificate` 临时装载的。那么 stapling 的红黑树 `ssl->staple_rbtree` 是按配置阶段的 `ssl->certs` 建立的，握手时的动态证书并不在 `ssl->certs` 里——这是否意味着动态证书**无法**享受 stapling？
   - 提示：对照 stapling.c:198-218，`ngx_ssl_stapling` 只对 `ssl->certs` 里的证书建 staple；而变量证书走的是 `ngx_ssl_connection_certificate`（openssl.c:636），不进入 `ssl->certs`。
   - 结论待本地验证：官方实现中，OCSP stapling 与「变量化 ssl_certificate」的组合存在已知局限——stapling 主要服务于静态证书。请通过实际测试（`openssl s_client -status`）确认在你的版本下动态证书是否能拿到 staple，并据此理解「以 X509 指针为键」这一设计的前提是「证书对象在配置阶段就稳定存在」。

2. **协作点 2（4.2 + 4.3）**：把 `ssl_certificate_cache` 改成 `off`，用 `ab` 或 `wrk` 对 HTTPS 压测，对比 `max=200 valid=120` 时的 QPS 与 `error_log` 中读盘日志频次，量化运行期缓存的收益。

3. **协作点 3（4.4 刷新公式）**：拿到一份 OCSP 响应后，记下其 `nextUpdate`（即 `valid`），用本讲的刷新公式手算 `refresh` 时刻，再在 debug 日志里核对下次实际发起 OCSP 请求的时间是否吻合（容许几分钟误差）。

> 这三个子任务分别考察「两套缓存的边界」「运行期缓存性能收益」「stapling 刷新调度」，需要真实证书与域名，部分结论须标注「待本地验证」。

## 6. 本讲小结

- nginx 有**两套** SSL 对象缓存：配置期全局缓存（`ngx_ssl_cache_fetch`，挂 `ngx_openssl_cache_module`，靠 reload 继承、无 LRU）与运行期按 server 缓存（`ngx_ssl_cache_connection_fetch`，`ssl_certificate_cache` 创建，带 `max/valid/inactive` 的 LRU），二者共用红黑树 + LRU 队列与 create/free/ref 回调表。
- 静态 `ssl_certificate` 走配置期路径，证书在配置阶段装到 `SSL_CTX`，并用 `X509_set_ex_data` 贴上文件名作为后续反查的钥匙；reload 时按 `mtime/uniq` 复用旧 cycle 解析结果。
- 含变量的 `ssl_certificate` 走运行期路径：配置阶段只编译 `complex_value` 并注册 cert_cb；握手时 `ngx_http_ssl_certificate` 求值路径，经 `ngx_ssl_connection_certificate` 把证书装到**当前连接**，结果进运行期 LRU 缓存。
- OCSP Stapling 以 X509 指针为键把每张证书的 `staple` 挂在 `ssl->staple_rbtree` 上；初始化找 issuer 与 responder URL，握手回调附带有效响应并懒触发后台刷新。
- 后台 OCSP 客户端自带独立内存池、完全异步（resolver→connect→write→read→verify），刷新时刻由公式 `max(min(valid-300, now+3600), now+300)` 决定，兼顾「过期前刷新」「最多 1 小时」「至少间隔 5 分钟」。

## 7. 下一步学习建议

- **缓存与 reload 的交互**：回看 u3-l2（cycle 生命周期）和 u4-l1（reload 流程），结合本讲的 `old_cache` 继承分支，理解「为何 ssl 缓存能在 reload 时不丢、不重解析」。
- **会话复用与共享内存**：u8-l1 提到的 `ssl_session_cache` 共享内存，与本讲的 per-process 证书缓存是不同机制；可阅读 `ngx_event_openssl_session.c` 对比「跨 worker 共享」与「每 worker 独享」两种缓存策略。
- **OCSP 客户端侧校验**：`ssl_ocsp` 指令（用于校验客户端证书）与本讲的 stapling 共用 `ngx_ssl_ocsp_*` 异步客户端代码，阅读 stapling.c:807 起的 `ngx_ssl_ocsp` 与 `ngx_ssl_ocsp_cache_*`（带共享内存缓存），体会同一套 HTTP 客户端如何服务两种用途。
- **HTTP/3 与 QUIC 的证书路径**：u8-l3 的 QUIC 有独立的 TLS 栈（BoringSSL/QUIC API），证书加载与 stapling 的接入方式与 TLS 不同，可作为进阶对比阅读。
