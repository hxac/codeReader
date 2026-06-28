# TypeScript 类型定义 ts/

## 1. 本讲目标

njs 是一个 JavaScript 引擎，但读者写的 `.js` 文件里用的 `r`、`s`、`ngx`、`Buffer`、`fs` 等 API，并不是标准 JS 的一部分——它们是 NGINX 注入的宿主对象或可导入模块（见 u8-l2、u7）。没有类型提示，这些 API 的拼写、参数、返回值很容易写错，而且错误往往要等到运行期才暴露。

本讲要解决的问题是：**njs 用什么来描述这些 API 的形状，让编辑器、`tsc` 能在写代码时就帮你查错？**

答案就在仓库的 `ts/` 目录——一套权威的 TypeScript 类型声明（`.d.ts`），以 `njs-types` 的名字发布到 npm。学完本讲，读者应能：

1. 理解 `ts/` 是**内置 njs 引擎与 QuickJS 两套引擎共用**的同一份 API 描述，与具体引擎无关；
2. 看懂 `ts/` 的文件如何按「三个入口 + 一份公共核心」组织，并能定位 `r`/`s`/`ngx`/各内建模块的类型签名；
3. 弄清 `njs-types` 这个 npm 包是怎么用 `package.json` + `tsconfig.json` 配置出来的，包括 `__VERSION__` 占位符如何被构建系统替换，以及如何用 `make ts_test` 跑类型回归。

## 2. 前置知识

- **什么是 `.d.ts`？** TypeScript 的「声明文件」。它只描述类型（函数签名、接口、字段），不含可执行逻辑。它让 TS 编译器（`tsc`）和编辑器知道某个全局变量或模块「长什么样」。
- **三斜杠指令 `/// <reference path="..." />`**：写在 `.d.ts` 顶部的特殊注释，作用是「把另一个声明文件也拉进来」。可以理解为声明文件之间的 `import`。
- **全局声明 vs 模块声明**：一个 `.d.ts` 里若没有顶层的 `import`/`export`，它就是「全局脚本」，里面 `declare const njs` 会变成真正的全局变量；若用 `declare module "fs" { ... }` 包起来，则是一个可 `import fs from 'fs'` 的模块声明。njs 两类都用。
- **ambient（环境）声明**：用 `declare` 关键字告诉编译器「这个名字存在、类型如下，实现由运行时提供」。njs 的 `r`、`ngx`、`Buffer` 都是 ambient 声明，真正的对象由 njs/NGINX 在运行期注入。
- 建议先读过 u8-l2（`ngx_http_js_module` 与 `r` 对象）和 u8-l3（`ngx_stream_js_module` 与 `s` 对象），这样你能把这里的类型与那两讲里的真实运行行为对应起来。

## 3. 本讲源码地图

| 文件 | 行数 | 作用 |
|---|---|---|
| `ts/index.d.ts` | 8 | 公共核心的「目录页」，用 8 条三斜杠指令把所有公共声明聚合到一起 |
| `ts/njs_shell.d.ts` | 11 | **入口 1**：njs CLI shell 环境（`console` 全局） |
| `ts/ngx_http_js_module.d.ts` | 590 | **入口 2**：NGINX HTTP 模块环境（`NginxHTTPRequest` 即 `r`） |
| `ts/ngx_stream_js_module.d.ts` | 235 | **入口 3**：NGINX Stream 模块环境（`NginxStreamRequest` 即 `s`） |
| `ts/ngx_core.d.ts` | 482 | HTTP/Stream 两入口共享的「`ngx` 系」声明：`Headers`/`Request`/`Response`/`ngx.fetch`/`ngx.shared` |
| `ts/njs_core.d.ts` | 648 | 引擎核心全局：`Buffer`、`TypedArray`、`njs`、`process`、定时器、`atob/btoa` |
| `ts/njs_webapi.d.ts` | 116 | Web API：`TextEncoder`/`TextDecoder` |
| `ts/njs_webcrypto.d.ts` | 358 | WebCrypto：全局 `crypto`、`crypto.subtle`、`CryptoKey` |
| `ts/njs_modules/*.d.ts` | — | 5 个可导入模块：`crypto`/`fs`/`xml`/`querystring`/`zlib` |
| `ts/package.json` | 31 | npm 包元信息（包名 `njs-types`、`types` 入口、依赖） |
| `ts/tsconfig.json` | 58 | 编译这套声明时用的 TS 配置（target/lib/strict） |
| `test/ts/test.ts` | 338 | 回归用例：一份「把所有类型都用一遍」的 `.ts` 程序 |
| `auto/make`、`auto/sources` | — | 构建/发布脚本：版本替换、打包、`ts_test` 目标 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**类型文件组织**（4.1）、**r/s API 类型**（4.2）、**njs-types 包**（4.3）。

### 4.1 类型文件组织：三个入口与一份公共核心

#### 4.1.1 概念说明

njs 有三种「运行环境」，每种环境注入的全局对象不同：

- **njs CLI shell**（`build/njs`）：有 `console`、`njs`、`process`、各内建模块，但**没有** `r`/`s`（CLI 脱离 NGINX，见 u1-l3）。
- **NGINX HTTP 模块**（`js_content` 等）：除了上面的，还多了请求对象 `r`（`NginxHTTPRequest`）。
- **NGINX Stream 模块**（`js_preread` 等）：多了会话对象 `s`（`NginxStreamRequest`）。

如果用一个大杂烩文件把三套全局都声明成全局，那么在 CLI 里 `tsc` 也会以为 `r` 存在，反而掩盖错误。njs 的做法是**三个独立入口文件**，每个入口只声明自己那套环境的全局，公共部分抽到一个被三者共用的「核心」里。用户按自己代码运行的环境，**只引用其中一个入口**。

这一点在 README 里说得很明确：njs-types 提供三个入口，分别对应 shell、HTTP、Stream 三种环境。

#### 4.1.2 核心流程

文件之间的引用关系是一棵「核心被复用」的树：

```
                    index.d.ts  (公共核心目录页)
                  /  |  |  |  \  \
            njs_core njs_webapi njs_webcrypto  njs_modules/{crypto,fs,xml,querystring,zlib}
                ▲
       ┌────────┼─────────┐
 njs_shell.d.ts   ngx_http_js_module.d.ts   ngx_stream_js_module.d.ts
                       ▲                          ▲
                       └──── ngx_core.d.ts ───────┘   (两入口共享 ngx/fetch/shared)
```

- `index.d.ts` 不声明任何全局，只用 8 条三斜杠指令把核心声明都拉进来。
- 三个入口各自 `/// <reference path="index.d.ts" />`，从而继承全部公共核心；HTTP/Stream 两个入口额外引用 `ngx_core.d.ts` 拿到 `ngx` 系。
- 用户二选一（或三选一）：在 `.ts` 顶部写一条 `/// <reference path=".../ngx_http_js_module.d.ts" />`，或把它放进 `tsconfig.json` 的 `files` 里。

#### 4.1.3 源码精读

公共核心目录页——8 条三斜杠指令分别引入核心全局、Web API、WebCrypto 与 5 个内建模块：

[ts/index.d.ts:1-8](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/index.d.ts#L1-L8) 这是整棵类型树的根：`index.d.ts` 自身不定义任何东西，只负责把 8 个声明文件聚合，任何引用它的入口都自动获得这些类型。

以 HTTP 入口为例，它通过两条引用继承核心 + `ngx` 系，再声明 HTTP 专属的 `NginxHTTPRequest`：

[ts/ngx_http_js_module.d.ts:1-2](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L1-L2) HTTP 入口先引用 `index.d.ts`（拿到 `Buffer`/`njs`/模块等公共核心），再引用 `ngx_core.d.ts`（拿到 `ngx`/`fetch`/`Headers`/`Request`/`Response`/`ngx.shared`）。文件其余部分就是 HTTP 专属的 `NginxHeadersIn`、`NginxVariables`、`NginxHTTPRequest` 等。

CLI shell 入口则更小，它只引用核心，再补一个 `console`：

[ts/njs_shell.d.ts:1-11](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/njs_shell.d.ts#L1-L11) `njs_shell.d.ts` 引用 `index.d.ts` 继承全部公共核心，然后 ambient 声明全局 `console`。注意 `Console` 接口里除了标准的 `log`，还有 njs 专属的 `dump`（对应 u1-l4 提到的 `njs.dump` 的对象版），这是「类型忠实记录运行期真实 API」的体现。

> 补充：`package.json` 的 `"types": "index.d.ts"` 指向的是核心目录页。这意味着 `import {} from 'njs-types'` 解析到核心（可导入模块、`Buffer` 等）；而要拿到 `r`/`s`/`ngx` 这些**全局** ambient 声明，必须显式引用对应入口文件（README 推荐放进 `tsconfig` 的 `files`）。两套用法分工明确：模块走包入口，全局走环境入口。

#### 4.1.4 代码实践

1. **实践目标**：用眼睛走一遍类型树的引用关系，确认「三个入口 + 一份公共核心」。
2. **操作步骤**：
   - 打开 `ts/index.d.ts`，数一下它引用了几个文件（应为 8 个）。
   - 分别打开 `njs_shell.d.ts`、`ngx_http_js_module.d.ts`、`ngx_stream_js_module.d.ts`，看每个入口第一行是不是都 `reference` 了 `index.d.ts`。
   - 注意 HTTP 与 Stream 两个入口都**额外**引用了 `ngx_core.d.ts`，而 shell 入口没有——这正说明 `ngx`/`fetch`/`shared` 是 NGINX 集成专属（CLI 没有 `ngx`）。
3. **需要观察的现象**：三个入口共享同一个 `index.d.ts`；只有 NGINX 两个入口才碰 `ngx_core.d.ts`。
4. **预期结果**：引用关系与 4.1.2 的树一致。**待本地验证**：你也可以用 `grep -n 'reference path' ts/*.d.ts` 一次性把所有引用关系打印出来核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么不在 `index.d.ts` 里直接把 `NginxHTTPRequest` 也声明成全局？
**答案**：因为 CLI shell 环境根本没有 `r` 对象（见 u1-l3：CLI 脱离 NGINX）。若在公共核心里声明 `r` 为全局，那么在 CLI 下 `tsc` 也会误以为 `r` 存在，反而放过本该报错的代码。把 `r` 放进 HTTP 专属入口，才能让类型检查忠实反映「当前环境到底有没有这个对象」。

**练习 2**：`ngx_core.d.ts` 里用了 `NjsFixedSizeArray` 这个类型，但它没在自己文件里 `reference` `njs_core.d.ts`，为什么不出错？
**答案**：因为 `NjsFixedSizeArray` 定义在 `njs_core.d.ts`（由 `index.d.ts` 间接拉入），而引用 `ngx_core.d.ts` 的 HTTP/Stream 入口同时也引用了 `index.d.ts`。声明文件之间的类型是「按编译单元全局可见」的，只要在同一个 `tsc` 调用里被一起加载即可，不必逐文件显式引用。

---

### 4.2 r/s API 类型：把请求/会话对象描述成接口

#### 4.2.1 概念说明

u8-l2 讲过，HTTP handler 收到的 `r` 是一个由 NGINX 注入的外部对象，挂满了 `headersIn`、`headersOut`、`variables`、`return()`、`subrequest()`、`readRequestText()` 等成员。u8-l3 讲过 Stream 的 `s` 有 `allow/deny/done`、`on/off`、`send` 等。这些成员在运行期由 C 代码（`ngx_http_js_ext_request[]` 等外部原型）提供。

类型层要做的，就是把这套「真实形状」**逐字段翻译成 TypeScript 接口**，让编辑器自动补全、`tsc` 检查参数类型。这样 `r.headersOut['Set-Cookie'] = ['a','b']` 能通过、而 `r.return('x')`（`return` 第一个参数必须是 `number`）会被标红。

#### 4.2.2 核心流程

描述一个宿主对象的套路是固定的：

1. **枚举已知键**：把常见的请求头（`Accept`、`Host`…）、nginx 变量（`remote_addr`、`uri`…）逐个写成可选字段，方便自动补全和拼写检查。
2. **加索引签名兜底**：nginx 变量是动态的（用户可自定义 `js_set` 变量），不可能穷举，于是用 `[prop: string]: string | undefined` 兜住所有未列出的键。
3. **方法用重载表达多种调用约定**：比如 `r.subrequest()` 既能传回调、也能返回 Promise、还能传 `detached` 选项——用多个同名签名（重载）精确描述。
4. **可空字段用 `?` + 联合类型**：比如 `requestText?: string`，因为请求体可能还没读到。

#### 4.2.3 源码精读

**请求头与变量：枚举 + 索引签名**。`NginxHeadersIn` 把常见请求头列成只读可选字段，最后一行用索引签名兜底：

[ts/ngx_http_js_module.d.ts:8-44](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L8-L44) `NginxHeadersIn` 枚举 `Accept`/`Host`/`Cookie` 等常用头（全部 `readonly` 且可选，因为请求不一定带），并用 `[prop: string]: string | undefined` 兜住任意头。注意出站头 `NginxHeadersOut`（L46-L81）的兜底类型是 `string | string[] | undefined`，且 `Set-Cookie` 被特别标成 `string[]`——这忠实反映了「响应头同一字段可有多值」的运行期行为。

变量同理，`NginxVariables`（L83-L234）列举了上百个 nginx 内建变量，并以 `[prop: string]: string | undefined` 收尾（L233），让自定义变量也能访问。

**宿主对象本体：`NginxHTTPRequest`**。这是 `r` 的类型，方法/属性最丰富：

[ts/ngx_http_js_module.d.ts:294](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L294) `interface NginxHTTPRequest` 定义了 `r` 的全部形状。它的成员对应 u8-l2 讲过的运行期行为：`args`/`headersIn`/`headersOut`/`variables` 是数据，`return`/`send`/`finish`/`internalRedirect` 是动作，`subrequest`/`readRequestText` 等是异步能力。

`r.return` 的签名（注意第一个参数是 `number`）：

[ts/ngx_http_js_module.d.ts:496](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L496) `return(status: number, body?: NjsStringOrBuffer): void`——状态码必须是数字，body 可选且可为字符串或 Buffer。类型层正是靠这个签名把 `r.return('x')` 这种错误挡在运行之前。`NjsStringOrBuffer` 是核心里定义的联合类型，见下文 4.3。

`r.readRequestText`（u9-l3 讲过的异步读体）：

[ts/ngx_http_js_module.d.ts:423](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L423) `readRequestText(): Promise<string>`——返回 Promise，resolve 出请求体字符串。这正是 u9-l3 讲的「JS 异步＝NGINX 事件循环驱动＋jobs 队列结算」在类型层的体现：它不阻塞、返回 Promise。同一组 `readRequest*` 还有 `ArrayBuffer`/`JSON`/`Form` 三个变体。

`r.subrequest` 的四个重载：

[ts/ngx_http_js_module.d.ts:539-543](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_http_js_module.d.ts#L539-L543) 四个重载精确描述 u9-l3 讲的调用约定：传 `{detached:true}` 返回 `void`；只传 uri 返回 `Promise<NginxHTTPRequest>`；传 options+回调或只传回调返回 `void`。TS 会按实参自动挑选匹配的重载，从而对「detached 与回调同时传」这种互斥用法（注释里标了 Warning）报错。

**Stream 会话对象：`NginxStreamRequest`**（即 `s`）：

[ts/ngx_stream_js_module.d.ts:98](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_stream_js_module.d.ts#L98) `interface NginxStreamRequest` 定义 `s` 的形状：`allow`/`deny`/`decline`/`done` 对应 u8-l3 的终止方式，`on`/`off`/`send` 对应 upload/download 事件机制。

`s.on` 用重载区分两种数据类型（u8-l3 讲过 string 与 Buffer 事件不能混用）：

[ts/ngx_stream_js_module.d.ts:167-170](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_stream_js_module.d.ts#L167-L170) 两个重载分别对应 `upload`/`download`（回调拿到 `string`）与 `upstream`/`downstream`（回调拿到 `Buffer`）。这样 `s.on('upload', (d: Buffer) => ...)` 会被标红，因为 `upload` 事件的数据是 string。

**`ngx` 全局与 fetch/shared**：HTTP/Stream 共用，定义在 `ngx_core.d.ts`：

[ts/ngx_core.d.ts:439](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/ngx_core.d.ts#L439) `fetch(init, options?): Promise<Response>`——`ngx.fetch` 返回 Promise（u9-l1）。它声明在 `NgxObject` 接口里，最后由 `declare const ngx: NgxObject`（L482）变成全局 `ngx`。同文件还定义了 `Headers`/`Request`/`Response`（L3/L88/L151）与共享字典 `NgxSharedDict<V>`（L263，对应 u9-l2 的 `ngx.shared`），后者用一个泛型 `V extends string | number` 区分 string 字典与 number 字典，并用条件类型让 `incr` 只在 `number` 字典上可用——这是类型层对「number 字典才能 `incr`」运行期约束的精确表达。

#### 4.2.4 代码实践

1. **实践目标**：在类型文件里亲手定位 `r.subrequest` 与 `r.readRequestText` 的签名，并读懂它们的含义。
2. **操作步骤**：
   - 打开 `ts/ngx_http_js_module.d.ts`，搜索 `subrequest(`，应定位到 L539-L543 的四个重载。
   - 搜索 `readRequestText`，应定位到 L423。
   - 对照 `test/ts/test.ts:54-63`（`r.subrequest('/uri', reply => ...)`、`await r.subrequest('/p/sub7')`）与 `test/ts/test.ts:73`（`await r.readRequestText()`），看真实调用是如何匹配到对应重载/签名的。
3. **需要观察的现象**：
   - `await r.subrequest('/p/sub7')` 匹配第 2 个重载（无 detached、无回调）→ 返回 `Promise<NginxHTTPRequest>`，故能 `await`。
   - `r.subrequest(Buffer.from('/p/sub5'), {detached:true})` 匹配第 1 个重载 → 返回 `void`（test.ts 把它赋给 `var vod: void`）。
   - `r.readRequestText()` 返回 `Promise<string>`，所以 `await` 出来是 `string`。
4. **预期结果**：每个真实调用都能找到唯一匹配的签名，且返回类型与用法一致。

#### 4.2.5 小练习与答案

**练习 1**：`NginxHeadersOut` 的索引签名是 `[prop: string]: string | string[] | undefined`，而 `NginxHeadersIn` 是 `[prop: string]: string | undefined`。为什么出站比入站多一个 `string[]`？
**答案**：因为响应头同一字段可以有多值（典型如 `Set-Cookie`），njs 允许把出站头设成数组；而请求头读进来时 njs 已按字段名合并成单个字符串。`Set-Cookie?: string[]`（L78）就是这一规则的具体体现。

**练习 2**：`NgxSharedDict<V>` 里 `incr` 的类型为什么写成 `V extends number ? (...) => number : never`？
**答案**：这是 TS 条件类型。当 `V` 是 `number`（即 number 字典）时 `incr` 是合法函数；否则（string 字典）类型为 `never`，调用 `incr` 会直接编译报错。这把 u9-l2 讲的「只有 `type=number` 的 zone 才能 `incr`」这一运行期约束，提前到编译期挡住。

---

### 4.3 njs-types 包：package.json、tsconfig 与构建发布

#### 4.3.1 概念说明

`ts/` 不只是仓库里的几个文件，它还是一个真正的 npm 包，包名 `njs-types`，发布在 npm registry。用户在自己的项目里 `npm install --save-dev njs-types` 就能拿到全套类型，然后 `tsc` 就能检查 njs 代码。这个包由两份配置定义：

- `package.json`：包名、版本、入口（`types` 字段）、发布哪些文件、依赖。
- `tsconfig.json`：用来**自检这套声明**的 TS 配置（target、lib、strict 开关），不是给用户用的。

此外，类型与 njs 引擎**同步发布**：版本号取自 `src/njs.h` 的 `NJS_VERSION`。但仓库里的 `package.json` 写的是占位符 `__VERSION__`，要在构建期才替换成真实版本。

#### 4.3.2 核心流程

发布与自检的流水线（由 `auto/make` 生成到 `build/Makefile`）：

```
src/njs.h:NJS_VERSION  ──(auto/make 读取)──►  NJS_VER / NJS_TYPES_VER
                                                        │
ts/package.json (version=__VERSION__) ──sed 替换──► build/ts/package.json (version="<ver>")
                                                        │
                                              npm pack → njs-types-<ver>.tgz
                                                        │
   ┌────────────────────────────────────────────────────┤
   ▼                                                     ▼
build/ts/  →  npm run lint (= tsc)            build/test/ts/  →  npm test (= tsc)
   自检 .d.ts 内部一致性                       用真实 .ts 程序回归类型
```

两条自检路径分工不同：
- `ts_lint`：在 `build/ts/` 里跑 `tsc`，检查 `.d.ts` 声明自身有没有内部矛盾（比如引用了不存在的类型）。
- `ts_test`：把打好的 `njs-types-<ver>.tgz` 装进 `build/test/ts/`，对 `test.ts`（一份把所有 API 都用一遍的程序）跑 `tsc`，验证「发布的包真的能让用户代码通过类型检查」。

源码清单则用 shell 的 `find` 动态收集（`auto/sources`）：

```bash
NJS_TS_SRCS=$(find ts/ -name "*.d.ts" -o -name "*.json")
NJS_TEST_TS_SRCS=$(find test/ts/ -name "*.ts" -o -name "*.json")
```

#### 4.3.3 源码精读

**包元信息**：

[ts/package.json:2-3](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/package.json#L2-L3) 包名 `njs-types`，版本字段却写成裸占位符 `__VERSION__`（不是合法 JSON 的字符串）。这是刻意留的「钩子」：仓库源码不能写死版本，真实版本在构建期由 sed 替换（见下文）。`"types": "index.d.ts"`（L12）声明包的主类型入口是核心目录页；`devDependencies` 只依赖 `typescript`（L28-L30），lint 脚本就是 `tsc`（L5-L6）。

**自检用的 tsconfig**：

[ts/tsconfig.json:3-4](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/tsconfig.json#L3-L4) `target: "es5"`、`module: "ES2015"`——这套声明的「语言基线」就是 ES5。这与 u6-l4 讲的「内置 njs 引擎是 ES5.1 子集」对齐：类型层假设最保守的引擎能力，保证写出的代码在两套引擎下都能被静态检查。注意 `lib` 数组（L5-L44）是按需逐项开启的（`ES2015.Core`、`ES2015.Promise`…），每项后面还标注了「since 0.x.x」的版本注释，说明每加一个 lib 都对应某个 njs 版本开始支持——这本身就是一份语言能力演进史。

[ts/tsconfig.json:45-53](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/tsconfig.json#L45-L53) `noEmit: true`（不产出 JS，只做类型检查）加全套 `strict` 开关。开 `strict` 意味着 njs 自己的类型声明必须经得起最严格的检查——这是保证发布质量的关键。

**版本替换与发布脚本**（构建系统生成）：

[auto/make:96-97](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L96-L97) `NJS_VER` 用 `grep NJS_VERSION src/njs.h` 从头文件里抠出版本字符串（当前为 `"1.0.1"`，见 `src/njs.h:14`），`NJS_TYPES_VER` 直接等于它。所以类型包版本永远跟着引擎版本走。

[auto/make:364-376](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L364-L376) 构建规则：把 `ts/` 复制到 `build/ts/`，用 `sed 's#__VERSION__#"$(NJS_TYPES_VER)"#'` 把占位符替换成带引号的真实版本，写成合法的 `build/ts/package.json`；再 `npm pack` 出 `njs-types-<ver>.tgz`，并把 hg/git 提交哈希写进 `COMMITHASH`（README 提到的溯源文件）。

[auto/make:378-397](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/auto/make#L378-L397) 三个目标：`ts`（仅复制+替换，产出可装的包目录）、`ts_lint`（在 `build/ts` 跑 `npm run lint` 自检声明）、`ts_test`（把 tgz 装进 `build/test/ts` 后 `npm test`，对 `test.ts` 做回归）。`ts_publish` 则负责 `npm publish`。

**核心联合类型**（被 r/s/ngx 到处复用）：

[ts/njs_core.d.ts:546-547](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/ts/njs_core.d.ts#L546-L547) `NjsStringOrBuffer` 与 `NjsBuffer` 是被 `r.log`、`r.return`、`ngx.fetch` 等广泛复用的联合类型。把它们抽到核心，避免在每个 API 上重复写一长串 `string | Buffer | DataView | TypedArray | ArrayBuffer`，是类型层 DRY 的典型手法。

#### 4.3.4 代码实践

1. **实践目标**：亲手用 `tsc` 对 `test/ts/test.ts` 做类型检查，验证 `ts/` 发布的类型真的可用；并体会 `__VERSION__` 占位符的作用。
2. **操作步骤**（推荐走构建系统的规范流程，需要 Node.js + npm + 联网拉 typescript）：
   - 在仓库根执行 `./configure`（生成 `build/Makefile`）。
   - 执行 `make ts_test`。它会依次：替换版本、打包 tgz、装进 `build/test/ts`、跑 `tsc`。
   - 也可单独 `make ts_lint` 只自检声明本身。
3. **需要观察的现象**：
   - `make ts_test` 退出码为 0、`tsc` 无报错输出 = `test.ts` 全部通过类型检查（它本就是「全 API 用一遍」的回归用例，理应通过）。
   - 想看它真能挡错：把 `build/test/ts/test.ts` 里某行改成错的（示例代码：把 `r.return(200, body)` 改成 `r.return('bad', body)`），再 `cd build/test/ts && npm test`，应看到「`Argument of type 'string' is not assignable to parameter of type 'number'`」之类的报错。改回后恢复通过。
4. **预期结果**：未篡改时 `tsc` 干净通过；篡改 `return` 第一参数后 `tsc` 报类型错误。
5. **关于占位符的一个坑（重要）**：仓库源码里的 `ts/package.json` 版本是 `__VERSION__`（非法 JSON），所以**直接** `cd test/ts && npm install` 会失败（npm 解析 `package.json` 时报错）。这正是为什么必须走 `make`（它会先 sed 替换）。若想脱离 `make` 手动验证：把 `ts/` 拷到临时目录，把 `"version": __VERSION__,` 手动改成 `"version": "1.0.1",`（取自 `src/njs.h:14` 的 `NJS_VERSION`），再 `npm install && npm run lint`。**待本地验证**：具体报错文案因 npm 版本而异，但「直接装会失败」这一行为可由源码确定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ts/tsconfig.json` 把 `target` 设成 `es5`，却又在 `lib` 里逐项加入 `ES2015`/`ES2017`/`ES2021`？
**答案**：`target` 控制的是「代码/类型假设的语法基线」，设成 `es5` 是为了与内置 njs 引擎（ES5.1 子集，见 u6-l4）对齐，保证写出的代码在最保守的引擎下也能用；`lib` 控制的是「哪些标准库类型声明可见」，逐项开启并标注 `since 0.x.x`，是为了精确反映「某个 njs 版本开始才支持该特性」。两者一个管语法、一个管库，互不矛盾。

**练习 2**：`ts/package.json` 里 `"files": ["**/*.d.ts", "COMMITHASH"]`，为什么发布的是 `.d.ts` 而不是 `.ts`？
**答案**：`.d.ts` 是纯类型声明，不含可执行逻辑，发布它就是为了让用户的 `tsc` 拿到类型提示，而不会带入任何 njs 自己的运行期实现（实现由引擎提供）。`COMMITHASH` 用于溯源（README 说明：可查到这个包对应上游哪个提交）。注意 `tsconfig.json` 并不在 `files` 里——它是给 njs 自己自检用的，不随包发布。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**给一个真实的 HTTP handler 补全类型，并用 `tsc` 验证**。

任务背景：下面这个 handler（示例代码）接收一个 JSON 请求体，发起一个子请求，再把结果返回。它没有类型标注，编辑器无法补全也无法查错。

```ts
// 示例代码：app.ts（待补类型）
async function handler(r) {
    const body = await r.readRequestJSON();          // r 无类型 → 编辑器不知道有这方法
    const reply = await r.subrequest('/upstream', {method:'POST', body: JSON.stringify(body)});
    r.headersOut['Content-Type'] = 'application/json';
    r.return(reply.status, reply.responseText ?? '{}');
}
```

要求：

1. 新建一个目录（如 `myapp/`），`npm init -y` 后 `npm install --save-dev njs-types typescript`。
2. 写 `myapp/tsconfig.json`，参照 `test/ts/tsconfig.json`：`target: ES5`、`module: es2015`、按需 `lib`、开 `strict`，并在 `files` 里放入口文件 `"./node_modules/njs-types/ngx_http_js_module.d.ts"`（因为这是 HTTP handler，需要 `r`）。
3. 把上面的 `app.ts` 放进去，给 `r` 标注类型 `NginxHTTPRequest`（入口声明了这个全局接口，可直接用），再 `npx tsc`。
4. 验证：未改错时应通过；故意把 `r.return(reply.status, ...)` 改成 `r.return('x')` 应报「`string` 不可赋给 `number`」。

完成这个练习，你会同时用到 4.1（选对 HTTP 入口）、4.2（`NginxHTTPRequest` 的 `readRequestJSON`/`subrequest`/`return` 签名）、4.3（`njs-types` 包与 `tsconfig` 配置）三个模块的知识。**待本地验证**：若网络不可用装不上 `typescript`，可退而用仓库自带的 `make ts_test`，并把上面 handler 追加进 `build/test/ts/test.ts` 再跑。

## 6. 本讲小结

- `ts/` 是 njs **两套引擎（内置 njs 与 QuickJS）共用**的同一份权威 API 描述，与具体引擎无关，只描述「形状」。
- 文件按**三个入口（`njs_shell.d.ts` / `ngx_http_js_module.d.ts` / `ngx_stream_js_module.d.ts`）+ 一份公共核心（`index.d.ts` 聚合）**组织，用户按运行环境只引用其一；HTTP/Stream 两入口额外共享 `ngx_core.d.ts`（`ngx`/`fetch`/`shared`）。
- `r`（`NginxHTTPRequest`）与 `s`（`NginxStreamRequest`）用 TS 接口逐字段翻译运行期宿主对象，靠「枚举已知键 + 索引签名兜底 + 方法重载」精确表达 `subrequest`、`readRequestText`、`on` 等多种调用约定。
- `njs-types` 是真 npm 包：`package.json` 的 `types` 指向核心 `index.d.ts`，`tsconfig.json` 以 `target: es5` 对齐最保守引擎基线并开 `strict` 自检。
- 版本号取自 `src/njs.h:NJS_VERSION`，仓库源码里写占位符 `__VERSION__`，由 `auto/make` 用 sed 替换后 `npm pack` 发布；`make ts_lint` 自检声明、`make ts_test` 用 `test/ts/test.ts` 做真实回归。
- 条件类型（`NgxSharedDict` 的 `incr`）、联合类型（`NjsStringOrBuffer`）等 TS 高级特性被用来把运行期约束（如「number 字典才能 incr」）提前到编译期。

## 7. 下一步学习建议

- **回到运行期对照**：重新读 u8-l2、u8-l3，把每个 `.d.ts` 里的字段/方法与 C 源码 `nginx/ngx_http_js_module.c` 的 `ngx_http_js_ext_request[]`、`nginx/ngx_stream_js_module.c` 的外部原型表逐个对应，体会「类型如何忠实记录 C 注入的真实 API」。
- **补 fetch/shared 的类型**：结合 u9-l1（`ngx.fetch`）、u9-l2（`ngx.shared`），精读 `ts/ngx_core.d.ts` 里的 `Request`/`Response`/`Headers`/`NgxSharedDict`，重点理解泛型 `NgxSharedDict<V>` 与条件类型的用法。
- **跑一次完整自检**：执行 `./configure && make ts_lint && make ts_test`，并尝试给 `test/ts/test.ts` 增删一行，观察 `tsc` 如何即时反映类型变化——这是日后给 njs 贡献新 API（同时改 C 实现与 `.d.ts`）时的标准验证动作。
- **延伸阅读**：官方 [njs TypeScript](https://nginx.org/en/docs/njs/typescript.html) 与 npm 上的 [`njs-types`](https://www.npmjs.com/package/njs-types) 页面，对照本讲理解的包结构。
