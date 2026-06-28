# crypto 与 WebCrypto 模块

## 1. 本讲目标

学完本讲后，你应当能够：

- 区分 njs 里**名字都叫 crypto 的两套不同加密 API**：Node 风格的 `crypto` 模块（`createHash` / `createHmac`）与 Web 风格的全局 `crypto` 对象（`crypto.subtle`）。
- 读懂 `createHash` / `createHmac` 如何把 OpenSSL 的 `EVP_MD_CTX` / `HMAC_CTX` 包装成带 `update` / `digest` / `copy` 方法的对象，并理解两引擎（njs / QuickJS）为何各写一份。
- 看懂 WebCrypto 的 `SubtleCrypto` 操作表（digest / sign / verify / encrypt / decrypt / derive / generate / import / export / wrap / unwrap）以及它为何返回 `Promise`，并理解 `CryptoKey` 在内部如何表示。
- 搞清 OpenSSL 是一个**可选依赖**：`--no-openssl` 会让 `crypto` 与 `webcrypto` 两个模块都不再编入，而 `AES-KW`、`Ed25519`/`X25519` 等能力还取决于构建期对 OpenSSL 具体版本的探测。

本讲承接 u7-l1（fs 模块）建立的双引擎铁律：external/ 下「一个功能、两份实现」。在 fs 里我们见过同步/回调/Promise 三套写法与 `magic8` 编码；本讲把它推进到加密场景——Hash/HMAC 的「一个 C 函数同时服务两类对象」、WebCrypto 的「Promise 结果投递」，以及把这一切粘到不同 OpenSSL 版本上的 `njs_openssl.h` 兼容层。

## 2. 前置知识

在进入源码前，先用三段话补齐背景。

**密码学散列与 HMAC 的直觉。** 散列函数（如 SHA-256）把任意长度的字节压成固定长度的指纹（摘要），单向、抗碰撞。它的典型用法是「分批喂入」：先创建一个上下文，多次 `update(data)`，最后 `digest()` 一次性输出——OpenSSL 里就是 `EVP_MD_CTX` 这一套（`EVP_DigestInit_ex` → `EVP_DigestUpdate` → `EVP_DigestFinal_ex`）。HMAC（Hash-based Message Authentication Code）在散列基础上多了一把「密钥」，用来证明消息没被篡改且来自持密钥者，OpenSSL 用 `HMAC_CTX`（`HMAC_Init_ex` → `HMAC_Update` → `HMAC_Final`）。

**Node 的 crypto 与 Web 的 WebCrypto 为何是两套。** Node 早期的加密 API 是命令式的、同步的、面向流式的 `createHash('sha256').update(...).digest('hex')`。而浏览器后来标准化了一套面向 Promise 的 `crypto.subtle`（W3C WebCrypto API），用 `await crypto.subtle.digest('SHA-256', data)`，方法名统一（sign/verify/encrypt/...），密钥用不可变的 `CryptoKey` 对象表示。njs 同时提供了这两套：Node 风格的作为可 `import` 的 `crypto` 模块，Web 风格的作为全局 `crypto` 对象。

**OpenSSL 的版本碎片化。** OpenSSL 在 1.0.x → 1.1.x → 3.x 之间改了不少 API（如 `EVP_MD_CTX_create` 改名 `EVP_MD_CTX_new`、`HMAC_CTX` 变成不透明结构体），LibreSSL 又有自己的兼容策略。njs 想在一份代码里同时支持这些版本，于是用一组以 `njs_` 开头的宏/内联函数把差异抹平，集中在 `external/njs_openssl.h`。

> 前置概念提醒：外部对象（`njs_external_t` / 外部原型 `proto_id`）、`njs_module_t` / `qjs_module_t` 注册结构、`magic8`、Promise 的 jobs 队列（`njs_vm_enqueue_job`），这些都在 u5-l4、u6-l1、u6-l2、u4-l5 里讲过，本讲直接使用。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [external/njs_crypto_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c) | 内置 njs 引擎的 Node 风格 crypto 模块（`createHash`/`createHmac`） |
| [external/qjs_crypto_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c) | QuickJS 引擎的 Node 风格 crypto 模块（对等实现） |
| [external/njs_webcrypto_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c) | 内置 njs 引擎的 WebCrypto（全局 `crypto` / `crypto.subtle`） |
| [external/qjs_webcrypto_module.c](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_webcrypto_module.c) | QuickJS 引擎的 WebCrypto（对等实现） |
| [external/njs_openssl.h](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h) | 抹平 OpenSSL 多版本差异的兼容层（四个 .c 都 include 它） |
| [auto/openssl](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl) | 构建期探测 libcrypto、`AES-KW`、`Ed25519` 的特性检测脚本 |
| [auto/modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules) / [auto/qjs_modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules) | 把 crypto/webcrypto 的 njs_* 与 qjs_* 实现编进两个库 |
| [test/crypto.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/crypto.t.mjs) / [test/webcrypto/digest.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/webcrypto/digest.t.mjs) | 两套 API 的回归测试（含大量已知向量） |

## 4. 核心概念与源码讲解

本讲三个最小模块：**Node crypto（Hash/HMAC）**、**WebCrypto（subtle）**、**OpenSSL 依赖与构建**。

---

### 4.1 Node 风格 crypto：createHash / createHmac

#### 4.1.1 概念说明

这是「能 `import` 进来的 `crypto` 模块」，API 形状照搬 Node：

```js
import crypto from 'crypto';

crypto.createHash('sha256').update('hello').digest('hex');
// '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'

crypto.createHmac('sha256', 'secret').update('hello').digest('hex');
```

它解决的问题：在 NGINX 配置或脚本里就地算一个摘要 / HMAC，比如校验上游签名、生成缓存键。`Hash` 与 `Hmac` 是两类对象，但它们的 `update` / `digest` 行为几乎一样——差别只在于底层用 `EVP_MD_CTX` 还是 `HMAC_CTX`。源码正是利用这一点，**用一套 C 函数同时服务两类对象**。

它**不是**全局对象，而是一个 ES 模块：`njs_crypto_init` 把它注册成名为 `crypto` 的可导入模块。注意这个名字与下一节的 WebCrypto 全局 `crypto` **重名但完全不同**，初学者最容易在这里踩坑。

#### 4.1.2 核心流程

一次 `createHash('sha256').update('A').update('B').digest('hex')` 的内部流程：

1. `createHash` 解析算法名 → 调 OpenSSL `EVP_get_digestbyname("sha256")` 得到 `const EVP_MD *md`。
2. 分配一个 `njs_digest_t{ EVP_MD_CTX *ctx }`，`EVP_DigestInit_ex(ctx, md, NULL)` 初始化。
3. 把 `njs_digest_t` 包成外部对象（`njs_vm_external_create`，原型 id = `njs_crypto_hash_proto_id`）返回给 JS。
4. 每次 `update(data)`：把 `data` 解码成字节，`EVP_DigestUpdate(ctx, ...)` 喂进去；返回 `this` 以支持链式调用。
5. `digest(enc)`：`EVP_DigestFinal_ex` 取出定长摘要字节，按 `enc`（hex/base64/base64url/buffer）编码后返回；同时 `EVP_MD_CTX_free(ctx)` 并把 `ctx` 置 NULL，防止「digest 后再 update」。

HMAC 流程完全对称，只是把 `EVP_*` 换成 `HMAC_*`，并在 `createHmac` 时多收一个 `key` 参数用于 `HMAC_Init_ex`。

关键技巧：`update` 和 `digest` 各只写**一个** C 函数，靠一个标志位区分 Hash / Hmac——内置引擎用 `magic8`（0=Hash，1=Hmac），QuickJS 用函数的 `magic` 整型参数。

#### 4.1.3 源码精读

**（1）算法名 → OpenSSL 摘要对象。** 不论你传 `md5`/`sha1`/`sha256`/`sha512`，njs 自己不维护算法表，而是直接问 OpenSSL：

[njs_crypto_module.c:568-593](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L568-L593) 用 `EVP_get_digestbyname()` 把字符串映射成 `EVP_MD *`，找不到就抛「not supported algorithm」。这意味着 njs 支持哪些算法完全取决于链接的 OpenSSL 编译进了哪些——这也是为什么测试里既测了 `md5` 也测了 `sha1`（见 [test/crypto.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/crypto.t.mjs)）。

**（2）digest 输出的编码表。** 四种编码各对应一个编码函数：

[njs_crypto_module.c:54-80](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L54-L80) 是 `njs_encodings[]`：`buffer`→`njs_buffer_digest`、`hex`→`njs_string_hex`、`base64`→`njs_string_base64`、`base64url`→`njs_string_base64url`。`njs_crypto_encoding`（[L596-L622](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L596-L622)）查这张表，省略参数时默认走第 0 项（`buffer`，返回 `Buffer`）。

**（3）createHash：建上下文 + 注册 cleanup。**

[njs_crypto_module.c:229-271](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L229-L271) 做三件事：分配 `njs_digest_t`、`njs_evp_md_ctx_new()` 建上下文、`EVP_DigestInit_ex` 初始化。注意 [L254-L262](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L254-L262) 调 `njs_mp_cleanup_add` 注册了一个池清理回调——这是 njs 引擎侧的内存回收方式（承接 u2-l3 内存池）：VM 销毁时 `njs_mp_destroy` 会跑这些 cleanup，调 `njs_crypto_cleanup_digest`（[L632-L641](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L632-L641)）释放 `EVP_MD_CTX`。最后 `njs_vm_external_create` 把 `njs_digest_t*` 打包成 JS 对象返回。

**（4）一个 update 服务两类对象（magic8）。** 这是本模块最值得读的一段：

[njs_crypto_module.c:274-360](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L274-L360) 是 `njs_hash_prototype_update`。看声明里的 `njs_index_t hmac` 参数——它就是声明表里写死的 `magic8`。在原型表 [njs_ext_crypto_hash[] L83-L134](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L83-L134) 里，Hash 的 `update` 标了 `.magic8 = 0`（[L100](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L100)）；在 [njs_ext_crypto_hmac[] L137-L179](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L137-L179) 里，Hmac 的 `update` 标了 `.magic8 = 1`（[L154](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L154)）。于是同一个 C 函数里 `if (!hmac) { EVP_DigestUpdate(...) } else { HMAC_Update(...) }`（[L344-L355](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L344-L355)），`digest` 函数 [L363-L438](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L363-L438) 同理。这正是 u7-l1 讲过的「用 magic 复用同一个 C 函数」在加密模块的应用。

**（5）createHmac。**

[njs_crypto_module.c:491-565](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L491-L565) 与 createHash 几乎镜像，多出来的是把第二个参数 `key` 解码成字节后传给 `HMAC_Init_ex(ctx, key.start, key.length, md, NULL)`（[L558](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L558)）。

**（6）模块注册。**

[njs_crypto_module.c:656-695](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L656-L695) 是 `njs_crypto_init`：先用 `njs_vm_external_prototype` 注册 Hash / Hmac / crypto 三张外部原型表（拿到 `proto_id`），再用 `njs_vm_external_create` 造一个 crypto 实例，最后 **`njs_vm_add_module(vm, "crypto", ...)`** 把它注册成可导入模块（[L688-L692](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L688-L692)）。模块结构体本身在 [L222-L226](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L222-L226)（`njs_module_t njs_crypto_module`，`.init = njs_crypto_init`）。

**（7）QuickJS 对等实现的关键差异。** `external/qjs_crypto_module.c` 行为一致，但机制不同：

- 声明表换成 QuickJS 的 `JSCFunctionListEntry`，用 `JS_CFUNC_MAGIC_DEF` 把 magic 传进 `int hmac` 参数（[qjs_crypto_module.c:84-98](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L84-L98)）。
- `update`/`digest` 的 magic 复用同样存在：[qjs_crypto_module.c:163-288](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L163-L288)。
- 内存回收**不用池 cleanup**，改用 QuickJS 的 **finalizer**：注册类 `qjs_hash_class`/`qjs_hmac_class`（[L101-L110](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L101-L110)）带 `.finalizer`，GC 时调 `qjs_hash_finalizer`/`qjs_hmac_finalizer`（[L485-L514](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L485-L514)）释放上下文。这是 u6-l1 讲过的「QuickJS 用类 id + finalizer」与 njs 引擎「外部对象 + 池 cleanup」的根本分工。
- 注册成 ES 模块：`qjs_crypto_init` 用 `JS_NewCModule`（[L612-L670](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L612-L670)），同时导出 `default` 与具名导出（[L78-L81](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_crypto_module.c#L78-L81)）。

> 两引擎共用同一份 `njs_openssl.h`：上下文创建/释放都走 `njs_evp_md_ctx_new()`/`njs_evp_md_ctx_free()`、`njs_hmac_ctx_new()`/`njs_hmac_ctx_free()`，不直接调 OpenSSL 原名。

#### 4.1.4 代码实践

**目标：** 用 Node 风格 crypto 算出 `sha256("hello")` 的十六进制摘要，并验证 HMAC。

**操作步骤：**

1. 构建带 OpenSSL 的 CLI（默认即开启，无需额外参数）：

```bash
./configure && make njs
```

2. 新建文件 `hash.mjs`：

```js
import crypto from 'crypto';

// 链式：createHash -> update -> digest
const h = crypto.createHash('sha256').update('hello').digest('hex');
console.log('sha256(hello) =', h);

// 分批 update 等价于一次性
const h2 = crypto.createHash('sha256');
h2.update('hel');
h2.update('lo');
console.log('split update =', h2.digest('hex'));

// HMAC
const m = crypto.createHmac('sha256', 'secret').update('hello').digest('hex');
console.log('hmac =', m);
```

3. 运行（两引擎都注册了 crypto 模块，均可）：

```bash
./build/njs hash.mjs            # 默认 njs 引擎
./build/njs -n QuickJS hash.mjs # QuickJS 引擎
```

**需要观察的现象：**

- 前两行摘要必须**完全相同**（分批 `update` 与一次性等价，证明 `EVP_DigestUpdate` 是累加的）。
- `sha256(hello)` 应为 `2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824`。
- 两引擎输出一致（业务逻辑相同，只是底层实现不同）。

**预期结果：** 见上。把 `digest('hex')` 改成 `digest()`（不传参数）应得到一个 `Buffer`；改成 `digest('base64url')` 应得到 URL 安全的 Base64。若传 `digest('xxx')` 应抛 `Unknown digest encoding`（对应 [njs_crypto_module.c:619](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_crypto_module.c#L619)）。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `njs_hash_prototype_update` 只写了一份，却能同时是 `Hash.prototype.update` 和 `Hmac.prototype.update`？

> **答：** 因为两张原型声明表里给同一个 C 函数配了不同的 `magic8`（Hash=0、Hmac=1），运行时该值作为 `hmac` 参数传入，函数内用 `if (!hmac) EVP_DigestUpdate(...) else HMAC_Update(...)` 分流。这是「一个 C 函数 + magic」复用模式的典型应用。

**练习 2：** `createHash` 之后为什么还要 `njs_mp_cleanup_add` 注册回调？不注册会怎样？

> **答：** `EVP_MD_CTX` 是 OpenSSL 用自己的分配器申请的资源，不在 njs 内存池管辖内。若不注册 cleanup，VM 销毁时池释放不会顺带释放它，就会泄漏。注册后，`njs_mp_destroy` 跑 cleanup 链时调 `njs_crypto_cleanup_digest` 释放上下文（QuickJS 侧则改用 finalizer 达到同样目的）。

**练习 3：** 如果用 `--no-openssl` 构建，`import crypto from 'crypto'` 会发生什么？

> **答：** `auto/modules` 在 `NJS_OPENSSL` 或 `NJS_HAVE_OPENSSL` 为假时不会把 `njs_crypto_module.c` 编入，模块根本不注册，`import` 会报找不到模块 `crypto`。详见 4.3 节。

---

### 4.2 WebCrypto：crypto.subtle 与 CryptoKey

#### 4.2.1 概念说明

WebCrypto 是浏览器标准的加密 API，njs 把它实现成**全局 `crypto` 对象**：

```js
// crypto 是全局对象，不用 import
const digest = await crypto.subtle.digest('SHA-256', data);  // ArrayBuffer
const uuid   = crypto.randomUUID();                          // 字符串
crypto.getRandomValues(buf);                                 // 填充随机字节

const key = await crypto.subtle.generateKey({name:'HMAC', hash:'SHA-256'},
                                            true, ['sign','verify']); // CryptoKey
const sig = await crypto.subtle.sign({name:'HMAC'}, key, data);
```

它有三个特征：**(a)** 操作挂在 `crypto.subtle`（`SubtleCrypto`）上；**(b)** 几乎所有操作都返回 `Promise`（异步语义）；**(c)** 密钥是封装好的 `CryptoKey` 对象，只暴露 `type / extractable / algorithm / usages` 四个只读属性，拿不到原始密钥字节（除非显式 `exportKey`）。

**关键命名陷阱：** 这里的全局 `crypto` 与 4.1 节「可 import 的 `crypto` 模块」**重名但是两回事**。一个是 `crypto.subtle`（WebCrypto，全局），一个是 `crypto.createHash`（Node 风格，模块）。它们的 init 函数分别用 `njs_vm_bind`（绑全局）和 `njs_vm_add_module`（加模块）注册，互不冲突地并存。

#### 4.2.2 核心流程

WebCrypto 内部用一张**算法注册表**统一描述「每个算法支持哪些用途、哪些密钥格式」。以 `digest` 为最简入口：

1. `crypto.subtle.digest('SHA-256', data)`：解析算法名 → 得到 `njs_webcrypto_hash_t` → `njs_algorithm_hash_digest` 映射到 `EVP_sha256()` → 一次性 `EVP_Digest()` 输出 → 包成 `ArrayBuffer` → 用 `njs_webcrypto_result` 包成 `Promise` 返回。
2. 涉及密钥的操作（sign/verify/encrypt/...）：先用 `importKey`/`generateKey` 造一个 `CryptoKey`（内部 `njs_webcrypto_key_t`，持有 `EVP_PKEY*` 或原始字节），再传给对应操作；操作前会校验「密钥的 usage 位掩码是否包含本次操作」「密钥的算法是否匹配」。
3. 所有结果统一走 `njs_webcrypto_result`：创建一个 Promise，把「resolve(结果) / reject(异常)」封装成一个 trampoline 作业，`njs_vm_enqueue_job` 入队，由宿主循环（CLI 或 NGINX）调 `njs_vm_execute_pending_job` 排空后才真正结算——这与 u4-l5 的 jobs 队列是同一套机制。

为什么强行包成 Promise？因为 WebCrypto 规范要求异步语义（浏览器里这些操作可能很慢，不能阻塞事件循环）。即便 njs 的实现其实是同步算完的，也要把结果**投递**到作业队列，以符合 `await` 语义。

#### 4.2.3 源码精读

**（1）算法注册表。** 这是理解整个 WebCrypto 的钥匙：

[njs_webcrypto_module.c:197-379](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L197-L379) 是 `njs_webcrypto_alg[]`，每个条目记录 `{算法名, usage 掩码, 支持的密钥格式, 是否 raw}`。比如 `HMAC`（[L240-L249](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L240-L249)）支持 `SIGN|VERIFY|GENERATE_KEY`，格式 `RAW|JWK`；`AES-KW`（[L290-L301](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L290-L301)）被 `#if (NJS_HAVE_AES_WRAP)` 包住——只有构建期探测到 OpenSSL 支持 `EVP_aes_128_wrap()` 才编入。同理 `Ed25519`/`X25519`（[L329-L355](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L329-L355)）受 `NJS_HAVE_ED25519` 守卫。这张表精确回答了「我的 njs 支持哪些 WebCrypto 算法」。

哈希名映射在 [L382-L388](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L382-L388)（`SHA-256`/`SHA-384`/`SHA-512`/`SHA-1`），曲线在 [L391-L396](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L391-L396)（`P-256`/`P-384`/`P-521`，值是 OpenSSL 的 `NID_*`）。

**（2）CryptoKey 的内部表示。** 算法之外的另一半是密钥：

[njs_webcrypto_module.c:64-96](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L64-L96) 是 `njs_webcrypto_key_t`：`alg` 指向上面的算法条目、`usage` 是用途位掩码、`extractable` 控制能否导出、`hash` 记哈希，联合体 `u` 里非对称密钥存 `EVP_PKEY *pkey` + 私/公标志 + 曲线，对称密钥（HMAC/AES）存原始字节 `njs_str_t raw`。对外它暴露为外部对象，四个只读属性由属性处理器实现：

[njs_webcrypto_module.c:654-699](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L654-L699) 是 `njs_ext_webcrypto_crypto_key[]`：`algorithm`/`extractable`/`type`/`usages` 各自挂一个 `njs_key_ext_*` 处理器（承接 u5-l4 的属性处理器机制），JS 侧只能读、不能改。

**（3）subtle 操作表。**

[njs_webcrypto_module.c:504-651](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L504-L651) 是 `njs_ext_subtle_webcrypto[]`：列出 `decrypt/deriveBits/deriveKey/digest/encrypt/exportKey/generateKey/importKey/sign/unwrapKey/verify/wrapKey` 共 12 个方法。注意 sign 与 verify 共用 `njs_ext_sign`（verify 的 `.magic8 = 1`，[L636](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L636)）；encrypt 与 decrypt 共用 `njs_ext_cipher`（[L514-L571](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L514-L571)）——又是 magic 复用。

**（4）digest：最简实现。**

[njs_webcrypto_module.c:2108-2155](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L2108-L2155) 是 `njs_ext_digest`：解析哈希 → `EVP_MD_size` 取输出长度 → 分配缓冲 → 一次性 `EVP_Digest()` → 包成 ArrayBuffer → `njs_webcrypto_result` 返回 Promise。哈希名到 `EVP_MD*` 的映射在 [njs_algorithm_hash_digest L5846-L5864](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L5846-L5864)（`EVP_sha256/sha384/sha512/sha1`）。

**（5）Promise 包装：njs_webcrypto_result。** 这是 WebCrypto「异步语义」的实现核心：

[njs_webcrypto_module.c:5936-5978](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L5936-L5978)：先用 `njs_vm_promise_create` 建一个 Promise 并拿到它的 resolve/reject 两个回调（存进 `arguments`）；再 `njs_vm_function_alloc` 造一个 `njs_promise_trampoline`（[L5920-L5933](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L5920-L5933)）作为作业函数，它的逻辑就是「调用 resolve 或 reject」；根据 `rc` 决定把结果（成功）或异常（失败）作为参数；最后 `njs_vm_enqueue_job` 把这个 trampoline 作业入队（[L5964](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L5964)），并立即把 Promise 返回给调用者。真正的 resolve/reject 要等宿主排空 jobs 队列才发生——这正是 u4-l5 讲的作业队列驱动 Promise。

**（6）usage 校验示例。** 看 `njs_ext_cipher` 如何拒绝误用：

[njs_webcrypto_module.c:807-825](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L807-L825) 先按 `encrypt` 标志取 `ENCRYPT` 或 `DECRYPT` 位（AES-KW 另算），若 `!(key->usage & mask)` 抛「key does not support encrypt/decrypt」；若 `key->alg != alg` 抛「不能用 X 算法操作 Y 类型的密钥」。这保证了「一个 `sign` 用的密钥不能拿去 `encrypt`」这类 WebCrypto 安全约束。

**（7）模块注册：绑全局 `crypto`。**

[njs_webcrypto_module.c:6084-L6121](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L6084-L6121) 是 `njs_webcrypto_init`：注册 CryptoKey 与 crypto 两张外部原型表，造一个 crypto 实例（含 `subtle` 子对象，见 [njs_ext_webcrypto[] L702-L746](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L702-L746)，`subtle` 是 `NJS_EXTERN_OBJECT` 挂 `njs_ext_subtle_webcrypto`），最后 **`njs_vm_bind(vm, "crypto", ...)`** 把它绑成全局变量（[L6115](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L6115)）。注意它用的是 `njs_vm_bind`（全局），**不是** 4.1 节 crypto 模块用的 `njs_vm_add_module`——这就是「全局 `crypto`」与「可 import 的 `crypto` 模块」并存且不冲突的原因。

**（8）QuickJS 对等实现。** `external/qjs_webcrypto_module.c` 同样成对：

- subtle 操作表 [qjs_webcrypto_subtle[] L511-L524](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_webcrypto_module.c#L511-L524)，CryptoKey 属性表 [L527-L534](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_webcrypto_module.c#L527-L534)。
- digest 实现 [qjs_webcrypto_digest L2412-L2450](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_webcrypto_module.c#L2412-L2450)，用 `qjs_new_array_buffer` 包字节。
- Promise 包装用 QuickJS 原生的 `qjs_promise_result`（搜全文件可见大部分操作都经它返回 Promise）。
- 注册时 `qjs_webcrypto_init`（[L5908-L5953](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/qjs_webcrypto_module.c#L5908-L5953)）既 `JS_SetPropertyStr(global, "crypto", ...)` 挂全局，又 `JS_NewCModule` 注册成可 import 的 `webcrypto` 模块；CryptoKey 用类 id `QJS_CORE_CLASS_ID_WEBCRYPTO_KEY`（[src/qjs.h:34](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/src/qjs.h#L34)）。

#### 4.2.4 代码实践

**目标：** 用 WebCrypto 算 `SHA-256("hello")`，与 4.1 节 Node crypto 的结果对照；再观察「digest 返回的是 Promise」。

**操作步骤：**

1. 新建 `digest.mjs`：

```js
async function main() {
  // crypto 是全局对象，crypto.subtle.digest 返回 Promise<ArrayBuffer>
  const ab = await crypto.subtle.digest('SHA-256', 'hello');
  console.log('webcrypto sha256(hello) =', Buffer.from(ab).toString('hex'));

  // 一个随机 UUID
  console.log('uuid =', crypto.randomUUID());
}
main();
```

2. 运行（两引擎都提供全局 `crypto`）：

```bash
./build/njs digest.mjs
./build/njs -n QuickJS digest.mjs
```

**需要观察的现象：**

- 输出应与 4.1 节 `crypto.createHash('sha256').update('hello').digest('hex')` **完全一致**：`2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824`。
- 注意必须 `await`：因为 `digest` 返回的是 Promise（由 `njs_webcrypto_result` 包装、经 jobs 队列结算）；CLI 会在脚本跑完后排空 pending job，所以 `main()` 里的 `await` 才能拿到值。
- 把算法名改成 `'XXX'` 应抛 `unknown hash name`（对应 digest 测试 [test/webcrypto/digest.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/webcrypto/digest.t.mjs) 第一个用例）。

**预期结果：** 见上。> 待本地验证：不同 njs 引擎对顶层 `await` 与「脚本结束后自动排空 jobs 队列」的支持细节可能不同；若 `main()` 不触发执行，可改用 `.then(console.log)` 或在 QuickJS 下用顶层 `await`。

#### 4.2.5 小练习与答案

**练习 1：** `crypto.subtle.digest` 明明是同步算完的，为什么还要包成 Promise？

> **答：** 为了符合 WebCrypto 规范的异步语义。`njs_webcrypto_result` 把结果封装成一个 trampoline 作业入队（`njs_vm_enqueue_job`），由宿主循环排空 jobs 时才 resolve。这让 njs 的行为与浏览器一致，也复用了 u4-l5 的 Promise 作业队列基础设施。

**练习 2：** `CryptoKey` 对象为什么不直接暴露密钥字节？怎么才能拿到？

> **答：** 出于安全：`CryptoKey` 只暴露 `type/extractable/algorithm/usages` 四个只读属性（由属性处理器实现，[L654-L699](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_webcrypto_module.c#L654-L699)），且只有 `extractable` 为真的密钥才能通过 `exportKey` 显式导出。这避免了密钥字节在 JS 里被随意读取或日志泄漏。

**练习 3：** 为什么 `sign` 和 `verify` 能共用同一个 `njs_ext_sign` 函数？

> **答：** 声明表里给 verify 配了 `.magic8 = 1`、sign 默认 0，函数内据此分流「签名」与「验签」路径（验签还要额外取出签名字节做比对）。和 Hash/Hmac 共用 update 是同一种 magic 复用套路。

---

### 4.3 OpenSSL 依赖：可选、可探测、跨版本兼容

#### 4.3.1 概念说明

crypto 与 webcrypto 两套 API 的底层都是 OpenSSL 的 libcrypto。但 OpenSSL 在 njs 里是**可选依赖**：

- 构建期可以用 `--no-openssl` 整体关闭，此时 crypto/webcrypto 两个模块都不编入（`import crypto` 与全局 `crypto.subtle` 都不可用）。
- 即便开启，某些算法（`AES-KW`、`Ed25519`/`X25519`）是否可用，取决于构建期对**当前 OpenSSL 版本**的特性探测。
- OpenSSL 跨版本（1.0.x / 1.1.x / 3.x / LibreSSL）API 不兼容，njs 用 `external/njs_openssl.h` 把差异藏在一组 `njs_*` 宏/内联函数后面，让四个 .c 文件只调统一的名字。

#### 4.3.2 核心流程

构建期从 `./configure --no-openssl`（或默认 `YES`）出发：

1. `auto/options` 读到 `NJS_OPENSSL`（默认 `YES`）。
2. `auto/openssl` 探测系统 libcrypto：先空库试编译，失败再加 `-lcrypto` 重试；成功则置 `NJS_HAVE_OPENSSL=YES`、记录 `NJS_OPENSSL_LIB` 并追加到 `NJS_LIB_AUX_LIBS`。
3. 同一脚本继续探测可选能力：`EVP_aes_128_wrap()` → `NJS_HAVE_AES_WRAP`；`EVP_PKEY_ED25519` 原始密钥接口 → `NJS_HAVE_ED25519`；并记录 `NJS_OPENSSL_VERSION`。
4. `auto/modules`（njs 引擎）与 `auto/qjs_modules`（QuickJS）在 `if [ $NJS_OPENSSL = YES -a $NJS_HAVE_OPENSSL = YES ]` 条件下，才把 `njs_crypto_module.c`、`njs_webcrypto_module.c`（及对应 `qjs_*`）加入编译清单。
5. 源码里 `#if (NJS_HAVE_AES_WRAP)` / `#if (NJS_HAVE_ED25519)` 守卫对应算法条目，运行时 `crypto.subtle` 暴露的算法集合就由这些宏决定。

#### 4.3.3 源码精读

**（1）configure 开关。**

[auto/options:21](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L21) 默认 `NJS_OPENSSL=YES`；[auto/options:57](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/options#L57) 提供 `--no-openssl` 把它置 `NO`（紧挨着 `--no-libxml2`/`--no-zlib`，三个可选依赖并列）。

**（2）libcrypto 探测 + 可选能力探测。**

[auto/openssl:6-79](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L6-L79)：第一段（[L9-L32](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L9-L32)）用 `auto/feature` 先后以空库、`-lcrypto` 探测 libcrypto；找到后（[L35-L77](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L35-L77)）再分别探测 `EVP_aes_128_wrap()`（→ `NJS_HAVE_AES_WRAP`，[L36-L45](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L36-L45)）、`EVP_PKEY_ED25519`（→ `NJS_HAVE_ED25519`，[L52-L61](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L52-L61)，注释说明 Ed25519 与 X25519 共用 Curve25519 实现、总是一起出现），以及 `OPENSSL_VERSION_TEXT`（[L63-L72](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/openssl#L63-L72)）。

**（3）模块编入的条件门。**

[auto/modules:10-22](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules#L10-L22)：`if [ $NJS_OPENSSL = YES -a $NJS_HAVE_OPENSSL = YES ]` 同时门控 `njs_crypto_module` 与 `njs_webcrypto_module`（与 libxml2/zlib 的写法完全平行）。QuickJS 侧 [auto/qjs_modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/qjs_modules) 用同样条件门控 `qjs_crypto_module` / `qjs_webcrypto_module`——这是双引擎铁律在构建层的体现。

**（4）跨版本兼容层 njs_openssl.h。**

[external/njs_openssl.h:14-27](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h#L14-L27) 统一 include 一组 OpenSSL 头；然后用 `#if (OPENSSL_VERSION_NUMBER >= 0x10100000L)` 之类的条件把新旧 API 对齐，例如：

- [L40-L46](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h#L40-L46)：`njs_evp_md_ctx_new/free` 在 1.1.x+ 映射到 `EVP_MD_CTX_new/free`，旧版映射到 `EVP_MD_CTX_create/destroy`。
- [L49-L77](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h#L49-L77)：`njs_hmac_ctx_new/free` 在旧版（`HMAC_CTX` 是栈上结构体）需手动 `OPENSSL_malloc` + `HMAC_CTX_init`。
- [L88-L107](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h#L88-L107)：`njs_bn_bn2binpad` 在旧版无此函数时手工补零。还有 LibreSSL 版本号伪装（[L30-L37](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/external/njs_openssl.h#L30-L37)）。

正因为有了这层封装，`njs_crypto_module.c` 与 `qjs_crypto_module.c` 里只出现 `njs_evp_md_ctx_new()`、`njs_hmac_ctx_new()`，不出现 OpenSSL 原名，一份代码就能在多个 OpenSSL 版本上编译。

#### 4.3.4 代码实践

**目标：** 亲手用 `--no-openssl` 关掉加密模块，观察现象，理解「可选依赖」对运行时 API 表面的影响。

**操作步骤：**

1. 重新配置并构建一个无 OpenSSL 的 CLI：

```bash
make clean || true
./configure --no-openssl && make njs
```

2. 新建 `probe.mjs`：

```js
// 探测两类 crypto 是否存在
console.log('typeof globalThis.crypto =', typeof globalThis.crypto);

try {
  const cr = await import('crypto');
  console.log('crypto module createHash =', typeof cr.default.createHash);
} catch (e) {
  console.log('import crypto failed:', e.message);
}
```

3. 运行：

```bash
./build/njs probe.mjs
```

**需要观察的现象：**

- `import('crypto')` 应失败（模块未注册），打印类似 `module not found`。
- 全局 `crypto`（WebCrypto）应不存在（`typeof ... === 'undefined'`）。
- 与 4.1/4.2 节「默认构建」下的输出形成鲜明对比。

**预期结果：** 见上。> 待本地验证：具体报错文案依引擎而异；如需恢复，重新 `./configure && make njs` 即可（默认 `NJS_OPENSSL=YES`）。本实践改编自 [auto/modules](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/modules) 的条件门逻辑。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `AES-KW` 在某些 njs 构建里不可用？

> **答：** `auto/openssl` 探测 `EVP_aes_128_wrap()`，仅当存在才定义 `NJS_HAVE_AES_WRAP`；而 `njs_webcrypto_alg[]` 里 `AES-KW` 条目被 `#if (NJS_HAVE_AES_WRAP)` 守卫。旧版 OpenSSL 没有这个函数，于是该算法不被编入。

**练习 2：** 四个 .c 文件都 include `njs_openssl.h` 而非直接 `<openssl/evp.h>` 等，好处是什么？

> **答：** `njs_openssl.h` 把 OpenSSL 跨版本（1.0.x/1.1.x/3.x/LibreSSL）的 API 差异封装成统一的 `njs_*` 宏/内联函数（如 `njs_evp_md_ctx_new`、`njs_hmac_ctx_new`、`njs_bn_bn2binpad`）。业务代码只调统一名字，一份源码能在多版本 OpenSSL 上编译，避免到处写 `#if`。

**练习 3：** 如果系统没装 libcrypto，`./configure` 默认（不带 `--no-openssl`）会怎样？

> **答：** `auto/openssl` 两次探测都失败，`NJS_HAVE_OPENSSL` 保持 `NO`。于是 `auto/modules`/`auto/qjs_modules` 的条件门不通过，crypto/webcrypto 不编入——效果等同于 `--no-openssl`，但其他模块（fs/querystring/buffer 等）照常构建。

---

## 5. 综合实践

把本讲三块内容串起来：**用同一个输入，分别用 Node crypto 与 WebCrypto 算 SHA-256 摘要，并验证它们相等；再用 HMAC 做一次签名/验签的完整往返。**

新建 `crypto_roundtrip.mjs`：

```js
import nodeCrypto from 'crypto';

async function main() {
  const msg = 'the quick brown fox';

  // (A) Node 风格：createHash
  const a = nodeCrypto.createHash('sha256').update(msg).digest('hex');

  // (B) Web 风格：crypto.subtle.digest（全局 crypto）
  const ab = await crypto.subtle.digest('SHA-256', msg);
  const b = Buffer.from(ab).toString('hex');

  console.log('node    :', a);
  console.log('webcrypto:', b);
  if (a !== b) throw Error('two implementations disagree!');

  // (C) HMAC 签名 + 验签往返（WebCrypto 全流程）
  const key = await crypto.subtle.generateKey(
    { name: 'HMAC', hash: 'SHA-256' }, true, ['sign', 'verify']);

  const sig = await crypto.subtle.sign({ name: 'HMAC' }, key,
                                       Buffer.from(msg));
  const ok = await crypto.subtle.verify({ name: 'HMAC' }, key, sig,
                                        Buffer.from(msg));
  console.log('hmac sign/verify ok =', ok, ' key.type =', key.type,
              ' usages =', key.usages.join(','));
}

main();
```

**操作：**

```bash
./configure && make njs
./build/njs crypto_roundtrip.mjs
./build/njs -n QuickJS crypto_roundtrip.mjs
```

**验证清单：**

1. (A) 与 (B) 两行 hex 必须相同——证明「Node crypto 的 `createHash`」与「WebCrypto 的 `subtle.digest`」虽然 API 形态完全不同、实现也分处两个文件，但底层都走 OpenSSL 的 `EVP_Digest`，结果一致。
2. `hmac sign/verify ok = true`——验证了 `generateKey → sign → verify` 的完整链路，且 `CryptoKey` 的 `type`（应为 `secret`）与 `usages`（应为 `sign,verify`）可读。
3. 两引擎结果一致——印证双引擎铁律：行为相同，实现成对。

**延伸阅读：** 对照回归测试 [test/crypto.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/crypto.t.mjs)（Node 风格，含 md5/sha1 的已知向量与各种 encoding）与 [test/webcrypto/digest.t.mjs](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/test/webcrypto/digest.t.mjs)（Web 风格，含 SHA-1/256/384/512 向量），把你的输出与测试里的 `expected` 字段逐一比对。

## 6. 本讲小结

- njs 有**两套名字都叫 crypto 的加密 API**：Node 风格的 `import crypto from 'crypto'`（`createHash`/`createHmac`，可导入模块）与 Web 风格的全局 `crypto` 对象（`crypto.subtle`，WebCrypto），前者用 `njs_vm_add_module` 注册，后者用 `njs_vm_bind` 绑全局，互不冲突。
- Node crypto 把 OpenSSL 的 `EVP_MD_CTX`/`HMAC_CTX` 包成带 `update`/`digest`/`copy` 的对象；`update`/`digest` 各只写一个 C 函数，靠 `magic8`（njs）/ `magic`（qjs）区分 Hash 与 Hmac——是 magic 复用模式的典型应用。
- WebCrypto 用一张算法注册表 `njs_webcrypto_alg[]` 统一描述每种算法支持的 usage 与密钥格式；`CryptoKey` 内部是 `njs_webcrypto_key_t`（持 `EVP_PKEY*` 或原始字节），对外只暴露四个只读属性。
- WebCrypto 的所有结果经 `njs_webcrypto_result` 包装成 Promise：建 Promise → 造 trampoline 作业 → `njs_vm_enqueue_job` 入队，由宿主排空 jobs 队列时结算，复用 u4-l5 的 Promise 基础设施。
- OpenSSL 是**可选依赖**：`--no-openssl` 或探测失败时 crypto/webcrypto 整体不编入；`AES-KW`、`Ed25519`/`X25519` 还受 `NJS_HAVE_AES_WRAP`/`NJS_HAVE_ED25519` 守卫；`njs_openssl.h` 抹平多版本 API 差异，让四个 .c 共用一套 `njs_*` 调用。
- 双引擎铁律在加密模块同样成立：`external/njs_*_module.c` 与 `external/qjs_*_module.c` 成对存在，业务逻辑相同、回收机制不同（njs 用池 cleanup，qjs 用 finalizer），由 `auto/modules`/`auto/qjs_modules` 在同一 OpenSSL 条件门下分别编入。

## 7. 下一步学习建议

- **进入 zlib/xml 等其余扩展模块（u7-l3）：** 它们与 crypto 一样遵循「双实现 + 可选依赖 + auto/feature 探测」的模式，读完本讲后可以快速类比掌握 `auto/libxml2`、`auto/zlib`。
- **回到 NGINX 集成（u8）：** 在 `js_content` 里调用 `crypto.subtle` 校验签名是常见用法；届时你会看到全局 `crypto` 在请求 VM 克隆里的可用性，以及 Promise 作业如何被 NGINX 事件循环排空。
- **深入 Promise 与作业队列（回顾 u4-l5）：** 本讲的 `njs_webcrypto_result` + `njs_promise_trampoline` 是 jobs 队列的典型消费者；想彻底弄懂「为何 `await` 能拿到值」应回到那一讲。
- **进阶构建与 ASan（u10-l3）：** 想亲手验证 `--no-openssl` 的编译差异、或用 `--address-sanitizer` 排查加密模块里的内存问题（`EVP_PKEY`/`HMAC_CTX` 的释放路径是 use-after-free 高发区），可参考该讲。
