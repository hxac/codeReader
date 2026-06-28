# njs 是什么：NGINX 的 JavaScript 引擎

## 1. 本讲目标

本讲是整本学习手册的起点。读完本讲，你应该能够：

1. 用自己的话说清楚 **njs（NGINX JavaScript）是什么**，以及它在 NGINX 生态里扮演什么角色。
2. 区分 njs 的**两种交付形态**：嵌入 NGINX 的动态模块（`ngx_http_js_module` / `ngx_stream_js_module`）与可独立运行的命令行工具（CLI，`build/njs`）。
3. 区分 njs 内置的**两套 JavaScript 引擎**：自 1.0.0 起被「弃用」的内置 njs 引擎（ES5.1 子集）与官方推荐的 QuickJS 引擎（ES2023）。
4. 理解 njs 的**运行模型**：JS 代码不会自己跑起来，必须由 NGINX 配置指令在请求处理流程的某个阶段触发它。

本讲不涉及 C 源码细节（那是后续单元的内容），而是建立一张「项目全景地图」，让你在深入内核之前先知道 njs 的整体形态。

## 2. 前置知识

本讲是零基础入门，但为了读得顺畅，最好先了解下面几个概念。不熟悉也没关系，文中会用通俗语言再解释一遍。

- **NGINX**：一个高性能的 Web 服务器 / 反向代理。它处理 HTTP 请求时，会依次经过若干「处理阶段（phase）」，例如 access（访问控制）阶段、content（生成响应内容）阶段。
- **动态模块（dynamic module）**：NGINX 支持在运行时通过 `load_module` 加载的 `.so` 共享库，无需重新编译 NGINX 本体就能扩展功能。njs 正是以这种形式分发的。
- **JavaScript 引擎**：把 JavaScript 源码编译并执行的运行时。浏览器里有 V8、SpiderMonkey；njs 则内置了一套自研引擎，并可选地切换到 QuickJS 引擎。
- **ECMAScript / ES 版本**：JavaScript 语言标准的代号。ES5.1（2011 年）是较早的稳定基线，ES2023 是较新的现代版本。语法能力（如 `class`、解构、`Map`）随版本递增。
- **指令（directive）**：NGINX 配置文件（`nginx.conf`）里的一条配置语句，例如 `js_content main.hello;`。

## 3. 本讲源码地图

本讲主要阅读项目里的「说明性文档」而非 C 代码，目的是先建立全局认知。涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| `README.md` | 面向用户的总说明：njs 是什么、如何安装、如何运行、Hello World 示例、CLI 用法。本讲引用它来确立项目定位与交付形态。 |
| `AGENTS.md` | 给贡献者/agent 的项目导航索引：一句话点明 njs 的交付形态与双引擎，并指向 `docs/agent/` 下的细分文档。 |
| `docs/agent/js-dev.md` | 「在 njs 里写 JavaScript」的开发指南：包含双引擎语言能力差异表、指令驱动运行模型、绑定 API 表面。本讲引用它来讲解双引擎与运行模型。 |

> 说明：本讲是入门全景篇，引用的是项目自身的文档源文件。后续单元（如 u2 起）才会进入 `src/` 下的 C 内核源码。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应三个层层递进的问题：

1. **njs 到底是什么、以什么形态存在？**（项目定位与交付形态）
2. **它有哪两套引擎，我该选哪套？**（双引擎选择与语言基线）
3. **JS 代码在 njs 里是怎么被触发执行的？**（指令驱动的运行模型）

### 4.1 项目定位与交付形态

#### 4.1.1 概念说明

一句话定义（这是本讲最该记住的一句话）：

> **njs 是一个 JavaScript 引擎，它被深度集成进 NGINX，让你能用熟悉的 JavaScript 语法扩展 NGINX 的内置能力。**

这个定义里有三个关键词：

- **JavaScript 引擎**：njs 的本质是一个能执行 JS 的运行时。
- **集成进 NGINX**：它的主要舞台是 NGINX，不是浏览器，也不是 Node.js。
- **扩展 NGINX 能力**：它的价值在于弥补 NGINX 配置语言（指令）表达能力不足的地方。

njs 解决的实际问题，`README.md` 列了三类典型场景：

- 在请求到达上游服务器之前，做**复杂的访问控制和安全检查**（比如校验 JWT、做细粒度鉴权）；
- **改写响应头**（增删改 `headersOut`）；
- 编写**灵活的、异步的内容处理器与过滤器**（比如用 `ngx.fetch()` 转发并改写响应）。

#### 4.1.2 核心流程

njs 以两种「交付形态」存在，理解这一点非常重要：

```
                  njs 项目（一个代码仓库）
                         │
         ┌───────────────┴────────────────┐
         ▼                                ▼
  形态 A：嵌入 NGINX                形态 B：独立 CLI
  （两个动态模块）                  （build/njs 可执行文件）
         │                                │
   ┌─────┴─────┐                          │
   ▼           ▼                          ▼
ngx_http_   ngx_stream_            交互式 REPL / 跑脚本
js_module   js_module              用于测试与学习 JS 语法
（处理 HTTP）（处理 TCP/UDP）      （没有 r / s 等 NGINX 对象）
```

- **形态 A（嵌入 NGINX）**：njs 编译成两个动态模块 `ngx_http_js_module`（处理 HTTP）和 `ngx_stream_js_module`（处理 TCP/UDP 流）。这是 njs 的「主战场」，能拿到请求对象 `r` 或会话对象 `s`。
- **形态 B（独立 CLI）**：njs 也能编出一个名为 `build/njs` 的独立命令行程序。它脱离 NGINX 运行，适合用来**学习和调试 JS 语法**，但**没有** `r`、`s`、`ngx.fetch` 这些 NGINX 专属对象。

> 初学者常误以为「CLI 就是 njs 的全部」。其实 CLI 只是方便测试的副产品；njs 真正的用途是形态 A——在 NGINX 里跑 JS。

#### 4.1.3 源码精读

`README.md` 的开头第一段给出了 njs 最权威的一句话定位——它是一个**动态模块**，且推荐用 QuickJS 引擎跑现代 JS：

[README.md:7](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L7) — 点明 njs 是 NGINX 的动态模块，用熟悉的 JS 语法扩展功能，推荐 QuickJS（ES2023）。

紧接着的「How it works」一节，明确说 njs 以**两个动态模块**形式提供，并列出它能做的三类事（访问控制、改响应头、异步内容处理）：

[README.md:38-44](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L38-L44) — 「provided as two dynamic modules」（两个动态模块）并列出三类典型用法。

`AGENTS.md` 用最精炼的几行总结了两种交付形态与双引擎并存：

[AGENTS.md:3-10](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md#L3-L10) — 总结 njs 以「独立 CLI + 两个 NGINX 模块 + 两套可选引擎」四种姿态存在。

#### 4.1.4 代码实践

**实践目标**：用自己的话复述 njs 的定位，确认你真的理解了「两种交付形态」。

**操作步骤**：

1. 打开并通读 [README.md:1-66](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L1-L66)（项目横幅、How it works、JavaScript engines 三节）。
2. 打开并通读 [AGENTS.md:1-44](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md#L1-L44)。
3. 在纸上用自己的话写出 njs 的**三句话定义**，要求每句分别覆盖：① 它本质上是什么；② 它以什么形态交付；③ 它解决什么问题。

**需要观察的现象**：你应该能注意到 README 与 AGENTS.md 对「交付形态」的描述是高度一致的——都强调「CLI + 两个 NGINX 模块」。如果发现两边说法矛盾，说明你读漏了，回去重读。

**预期结果**：三句话定义里应至少包含「JavaScript 引擎」「NGINX 动态模块」「访问控制 / 改写响应 / 异步内容处理」这类关键词。

> 本步骤无需运行任何命令，属于源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：njs 的「独立 CLI」和「NGINX 动态模块」分别适合用来做什么？为什么不能在 CLI 里直接用 `r.return()`？

**参考答案**：CLI（`build/njs`）脱离 NGINX 运行，适合学习和调试 JS 语法本身（如 `console.log`、数组方法、Promise）。`r` 是 NGINX 在处理 HTTP 请求时才注入给 JS 的请求对象，CLI 没有 NGINX 请求上下文，因此不存在 `r`，也就无法调用 `r.return()`。

**练习 2**：说出 njs 能解决的两类典型 NGINX 场景。

**参考答案**：例如「在请求到达上游前做访问控制 / 安全检查」和「异步生成或改写响应内容（含响应头）」。

### 4.2 双引擎选择与语言基线

#### 4.2.1 概念说明

njs 项目的命名里藏着一个小坑：**「njs」既指整个项目，也指它自研的那套内置 JavaScript 引擎**。为了不混淆，本手册做如下约定：

- **njs（项目）**：整个 NGINX JavaScript 项目。
- **njs 引擎 / 内置引擎**：项目自研的、历史悠久的 JavaScript 引擎。
- **QuickJS 引擎**：由 Bellard 开发的、njs 后来引入并推荐的另一套引擎。

二者**可互换**，通过 `js_engine` 指令在 NGINX 配置里、或 `-n` 选项在 CLI 里切换。关键事实（务必记住）：

| 引擎 | 语言基线 | 状态 |
|---|---|---|
| **QuickJS** | ES2023 | ✅ **推荐**，新代码首选 |
| **njs（内置）** | ES5.1 strict + 精选 ES6+ 子集 | ⚠️ 自 1.0.0 起**弃用**，仅用于维护旧代码 |

为什么要特别强调这一点？因为两套引擎的**语法能力差距很大**：QuickJS 是完整的 ES2023，能跑 `class`、`Map`、解构、`...` 展开；而内置 njs 引擎是 ES5.1 严格变体加一小撮 ES6 扩展，这些现代语法**不支持**。把为 QuickJS 写的现代代码丢进内置 njs 引擎，会直接报语法错误。

#### 4.2.2 核心流程

引擎选择的判定流程可以用下面的伪代码描述：

```
你要写或运行一段 JS：
  ├── 是新代码吗？
  │     └── 是 → 默认选 QuickJS（js_engine qjs; 或 CLI 的 -n QuickJS）
  ├── 只是在维护旧的、难以移植的 njs 引擎代码？
  │     └── 是 → 保留 njs 引擎（默认或 -n njs）
  └── 代码要跨两引擎可移植？
        └── 避开任一引擎的「专属特性」，写双方都支持的子集
```

切换引擎有两个入口：

- **NGINX 配置**：在 `http` / `server` / `location`（HTTP）或 `stream` / `server`（Stream）里写 `js_engine qjs;`（QuickJS）或 `js_engine njs;`（内置）。
- **CLI**：`./build/njs -n QuickJS script.js` 或 `./build/njs -n njs script.js`（注意 QuickJS 需要构建时链接了 QuickJS 库）。

#### 4.2.3 源码精读

`README.md` 的「JavaScript engines」一节是关于双引擎最权威的用户侧说明：

[README.md:60-66](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L60-L66) — 正式介绍两套「可互换」的引擎，并通过 `js_engine` 指令选择。

其中两行各自点明了一台引擎：

[README.md:63](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L63) — QuickJS（推荐），ES2023，用 `js_engine qjs;` 启用。

[README.md:64](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L64) — 内置 njs 引擎（自 1.0.0 起弃用），ES5.1 严格变体 + 精选 ES6+ 扩展。

`AGENTS.md` 给开发者的语言基线提示同样关键——它直接告诉你「新代码默认 QuickJS」：

[AGENTS.md:52-56](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md#L52-L56) — 「Default to the QuickJS engine」，并给出两引擎的语言基线（QuickJS=ES2023，njs=ES5.1 strict + 精选 ES6+）。

要看两引擎**具体语法能力差异**，最清晰的来源是 `docs/agent/js-dev.md` 的「Engine differences at a glance」对照表：

[docs/agent/js-dev.md:38-59](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L59) — 逐项对比 `class`、生成器、解构、`Map/Set`、`BigInt`、非默认 import 等特性在两引擎下的支持情况。

例如该表里：`class`、生成器（`function*`/`yield`）、解构、`Map/Set/WeakMap/WeakSet`、`BigInt`、`Proxy`、`Reflect`、非默认 import（`import {x}`）等，**只在 QuickJS 支持，njs 引擎为 ✗**。这恰好说明为什么新代码应首选 QuickJS。

`AGENTS.md` 还点出一个重要的工程现实——双引擎意味着「双份代码」：

[AGENTS.md:32-34](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md#L32-L34) — 「Dual engine = dual code」：大多数扩展模块同时维护 `njs_*.c` 和 `qjs_*.c` 两份实现，改一边要同步改另一边。

#### 4.2.4 代码实践

**实践目标**：亲眼看见两套引擎的语法能力差异，而不是只看文档表格。

**操作步骤**：

1. （前置）按 [README.md:300-310](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L300-L310) 构建出 `build/njs`：`./configure && make`（产物在 `build/njs`）。
2. 用默认的内置 njs 引擎跑一段现代语法，例如 `class`：

   ```bash
   ./build/njs -c 'class A {}; console.log("ok")'
   ```

3. 切到 QuickJS 引擎再跑一次（**待本地验证**：需要构建时链接了 QuickJS 库，见 [README.md:329-338](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L329-L338)）：

   ```bash
   ./build/njs -n QuickJS -c 'class A {}; console.log("ok")'
   ```

4. 再各跑一行 `typeof Map` 对比（参考 [docs/agent/js-dev.md:302-305](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L302-L305)）：

   ```bash
   ./build/njs -c 'console.log(typeof Map)'
   ./build/njs -n QuickJS -c 'console.log(typeof Map)'
   ```

**需要观察的现象**：在内置 njs 引擎下，`class` 语法应报语法错误（或 `typeof Map` 为 `'undefined'`）；在 QuickJS 下则正常执行（`typeof Map` 为 `'function'`）。这与你刚刚读到的差异表一致。

**预期结果**：`class` 与 `Map` 在 QuickJS 下可用、在 njs 引擎下不可用——正好印证 [docs/agent/js-dev.md:38-59](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L38-L59) 表格里 `class ✗`、`Map/Set ✗`（njs 列）的结论。

> 若本地尚未链接 QuickJS，第 3、4 步会报缺引擎相关错误，这本身也说明 QuickJS 是「可选、需单独链接」的——属正常现象。

#### 4.2.5 小练习与答案

**练习 1**：为什么官方说「新代码应默认 QuickJS」？给出一个具体的语法理由。

**参考答案**：QuickJS 是 ES2023，支持 `class`、生成器、解构、`Map/Set`、`BigInt` 等现代语法；而内置 njs 引擎是 ES5.1 子集，这些都不支持。新代码用现代写法更自然，而现代写法只在 QuickJS 下能跑。

**练习 2**：如果一段 JS 既要能在 njs 引擎跑、又要能在 QuickJS 跑，应该避开哪些特性？

**参考答案**：避开 `class`、`function*`/`yield`、解构赋值、`...` 展开、`Map/Set/WeakMap/WeakSet`、`BigInt`、`Proxy`、`Reflect`、非默认 `import`（`import {x}`）等 njs 引擎不支持、而 QuickJS 独有的特性；也不要用任一引擎的专属绑定（见 4.3 节）。

### 4.3 指令驱动的运行模型

#### 4.3.1 概念说明

这是初学者最容易踩的第二个坑：**在 njs 里，JavaScript 代码不会自己跑起来。**

在浏览器或 Node.js 里，你写一行 `console.log("hi")`，文件一加载就执行了——它们有「入口脚本」、有事件循环、能自启动。njs **不是这样**：

- njs 里**没有**自动执行的主脚本；
- **没有**后台线程；
- **没有**「加载即运行」的启动逻辑。

njs 里的每一段 JS 函数，都是因为**某条 NGINX 配置指令把它绑定到了请求处理的某个阶段**，然后 NGINX 在那个阶段调用了它，它才执行。换句话说：**是 NGINX 在驱动 JS，而不是 JS 在驱动自己。**

#### 4.3.2 核心流程

用一个简化的时序来理解「指令如何驱动 JS」：

```
1. NGINX 启动，读取 nginx.conf
2. 遇到 js_import main from hello.js;     ← 加载 JS 文件、定义函数（此时函数体不执行）
3. 一个 HTTP 请求到来
4. 请求进入某个 location，其配置里有 js_content main.hello;
5. NGINX 在「content 阶段」调用 main.hello，并把请求对象 r 作为参数传入
6. 函数体执行：r.return(200, "Hello world!\n")
7. 函数返回，请求结束
```

关键点：第 2 步只是「定义」，第 5 步才「执行」。把 `js_content` 换成别的指令（如 `js_access`、`js_set`、`js_header_filter`、`js_body_filter`、`js_periodic`），就绑到了不同的阶段，暴露的上下文对象也不同（HTTP 多为 `r`，Stream 多为 `s`）。

#### 4.3.3 源码精读

`docs/agent/js-dev.md` 用一段加重的文字把这个「反直觉」的运行模型讲得很清楚——这是本模块最重要的一句话：

[docs/agent/js-dev.md:64-72](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L64-L72) — 明确声明「JS code in njs does not run on its own」，并说明唯一的近例外是 `js_periodic`（且其触发源仍是 NGINX 的定时器）。

随后该文档用一张表列出 HTTP 模块各指令分别绑到哪个阶段、暴露什么对象、如何结束——这就是「指令驱动」的具体落点：

[docs/agent/js-dev.md:77-86](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L77-L86) — HTTP 指令表：`js_content`（content 阶段，`r`）、`js_access`（access 阶段，`r`）、`js_header_filter`/`js_body_filter`（响应过滤，`r`）、`js_set`（变量求值，`r`）、`js_periodic`（定时器，无请求）。

`AGENTS.md` 同样用一句精炼的话强调了这一点：

[AGENTS.md:57-59](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md#L57-L59) — 「Nginx drives the engine, not the JS」，并列出 HTTP 侧的指令入口（`js_content`、`js_access`、`js_header_filter`、`js_body_filter`、`js_set`、`js_periodic`）。

最直观的例子是 README 里的 Hello World。先看 JS 侧（一个普通函数，接收 `r`，调用 `r.return`）：

[README.md:167-173](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L167-L173) — `example.js`：定义 `hello(r)` 并 `export default {hello}`。注意它本身不会执行，只是被导出等待调用。

再看 NGINX 侧（用 `js_path` 设路径、`js_import` 导入、`js_content` 在 `location /` 里把函数绑到 content 阶段）：

[README.md:178-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L178-L201) — `nginx.conf`：`js_path` + `js_import main from http/hello.js` + `location / { js_content main.hello; }`，正是「指令驱动」的最小可运行样例。

最后，CLI 形态之所以「没有 `r`/`s`」，正是因为它脱离了 NGINX 的请求流程，没有指令触发、也就没有请求上下文注入：

[README.md:208-211](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L208-L211) — CLI 工具「独立运行」，所以 HTTP/Stream 等 NGINX 专属对象在它的运行时里不可用。

#### 4.3.4 代码实践

**实践目标**：通过精读 Hello World 配置，理解「JS 函数定义」与「指令触发执行」是分离的两件事。

**操作步骤**：

1. 打开 [README.md:167-173](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L167-L173)（`example.js`）和 [README.md:178-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L178-L201)（`nginx.conf`）。
2. 在 `nginx.conf` 里找出三件事分别由哪条指令完成：
   - 告诉 NGINX 去哪里找 JS 文件 → ________
   - 把 JS 文件导入为一个模块 → ________
   - 在某 `location` 的 content 阶段调用某个函数 → ________
3. 对照 [docs/agent/js-dev.md:77-86](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md#L77-L86) 的指令表，回答：如果把 `js_content main.hello;` 改成 `js_access main.hello;`，这个函数会从「content 阶段」挪到哪个阶段？终止方式需要怎么改？

**需要观察的现象**：你会发现 `example.js` 里**没有任何**「调用 `hello()`」的语句——函数只是被定义和导出。真正触发它执行的是 `nginx.conf` 里的 `js_content` 指令。这正是「指令驱动」的体现。

**预期结果**：

- 三条指令依次是 `js_path`、`js_import`、`js_content`。
- 改成 `js_access` 后，函数会在 **access 阶段**执行；按表格，access 阶段用 `r.return(403|...)` 来拒绝，否则放行——所以 `hello` 里 `r.return(200, ...)` 的语义在 access 阶段就不再是「输出内容」，这点需要重写。

> 本步骤为配置阅读型实践，无需运行 NGINX。若你想真正跑起来，需先构建 NGINX 并加载 `ngx_http_js_module`，这属于后续单元 u8 的内容。

#### 4.3.5 小练习与答案

**练习 1**：在 njs 里，为什么你在 JS 文件顶层写一行 `console.log("boot")`，请求到来时它不一定会打印？

**参考答案**：因为 njs 的 JS 不会自启动；模块加载时执行的是模块顶层代码（模块作用域），而具体的业务函数只有在被 `js_content`/`js_access` 等指令绑定的阶段被 NGINX 调用时才执行。顶层代码何时跑，取决于 NGINX 何时加载该模块（每个 worker 加载一次）。

**练习 2**：说出 HTTP 模块里至少三个「把 JS 函数绑到请求阶段」的指令，并各说一句它们各自的作用。

**参考答案**：
- `js_content`：在 content 阶段生成响应内容（替代上游）；
- `js_access`：在 access 阶段做访问控制，可 `r.return(403)` 拒绝；
- `js_set`：把一个 NGINX 变量绑到一个 JS 函数，求值时同步调用。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「全景复述」小任务：

1. **定位**：用三句话写出 njs 是什么（覆盖：本质、交付形态、解决的问题）。
2. **引擎**：填一张小表，写出内置 njs 引擎与 QuickJS 引擎各「支持」和「不支持」的一项语言特性，并注明哪个是官方推荐。

   | 特性 | 内置 njs 引擎 | QuickJS 引擎 |
   |---|---|---|
   | 例：`class` | 不支持 | 支持 |
   | （自填） | … | … |
3. **运行模型**：阅读 [README.md:167-201](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md#L167-L201) 的 Hello World，画一张时序图，标出「JS 函数被定义」「请求到达」「指令触发执行」「函数调用 `r.return`」四个节点，并用自己的话解释「为什么 JS 不会自己跑起来」。

完成这个小任务后，你就建立了进入下一讲（u1-l2：源码目录结构与构建系统总览）所需的全局认知。

## 6. 本讲小结

- **njs 是一个集成进 NGINX 的 JavaScript 引擎**，用 JS 语法扩展 NGINX，典型场景是访问控制、改写响应头、异步内容处理。
- njs 有**两种交付形态**：嵌入 NGINX 的两个动态模块（`ngx_http_js_module` / `ngx_stream_js_module`），以及可独立运行的 CLI（`build/njs`）；CLI 没有 `r`/`s` 等 NGINX 对象。
- njs 内置**两套可互换引擎**：推荐的 **QuickJS**（ES2023）与自 1.0.0 起弃用的**内置 njs 引擎**（ES5.1 子集）；新代码默认 QuickJS。
- 两引擎语法能力差距大：`class`、`Map/Set`、解构、生成器、`BigInt` 等只在 QuickJS 支持。
- njs 是**指令驱动**的运行模型：JS 不会自启动，必须由 `js_content`/`js_access`/`js_set` 等指令绑到请求处理的某个阶段，由 NGINX 触发执行。
- 「双引擎 = 双份代码」：扩展模块通常同时维护 `njs_*.c` 与 `qjs_*.c` 两份实现。

## 7. 下一步学习建议

本讲建立了 njs 的全景认知。下一讲 **u1-l2《源码目录结构与构建系统总览》** 会带你走进仓库本身，理清 `src/`（内核）、`external/`（扩展）、`nginx/`（集成）、`ts/`（类型）、`test/`（测试）、`auto/`（构建脚本）各目录的职责，以及基于 shell 的自研构建系统如何工作。

建议继续阅读的源码/文档：

- [README.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/README.md) 的「Building from source」一节，为下一讲的构建系统做铺垫。
- [AGENTS.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/AGENTS.md) 全文，熟悉项目的任务分流（引擎开发 vs JS 开发）。
- [docs/agent/js-dev.md](https://github.com/nginx/njs/blob/f078f14372ee789ea1435f35672407c13917b5e7/docs/agent/js-dev.md) 的「Runtime model」与「How to test」两节，作为本讲 4.3 节的延伸。
