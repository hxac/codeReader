# dev server 内部：esmoduleserve 与测试路由

## 1. 本讲目标

学完本讲，你应该能够：

1. 读懂 [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) 中 `startServer()` 的全部代码：一个约 25 行的函数如何同时提供「ES 模块按需编译服务」「静态文件服务」和「运行时生成的测试页」三种能力。
2. 说清 `moduleserver.handleRequest` 与 `serveStatic` 的**回退关系**：一个请求先由谁处理、处理不了交给谁、最终 404 从哪里发出。
3. 理解 `/test/` 路径如何在你访问它的那一刻调用 `gatherTests` 收集全部包的浏览器测试、再用 `testHTML` 拼出一张 mocha 测试页——这张页面上没有任何静态文件与之对应。
4. 掌握监听地址与 8090 端口的配置位置，以及 `OPEN` 环境变量、`--source-map` 选项各自影响什么、不影响什么。
5. 具备修改本仓库 dev server 的动手能力：换端口、加一条自定义路由。

本讲承接 u1-l4（你已经知道 `_m/` 是虚拟 URL 空间、demo 页面由它加载）和 u2-l2（你已经知道 `buildtool.watch` 负责在源码变化时重建各包 `dist/`），这次我们把镜头对准 HTTP 服务层本身。

## 2. 前置知识

### 2.1 Node 的 http 模块与「请求—响应」模型

Node 内置的 `http.createServer(callback)` 会返回一个服务器对象，每来一个 HTTP 请求就调用一次 `callback(req, resp)`：

- `req` 是请求对象，`req.url` 是路径加查询串（例如 `/test/?grep=view`）；
- `resp` 是响应对象，典型用法是 `resp.writeHead(状态码, 头对象)` 写头、`resp.end(内容)` 写正文并结束响应。

「路由」就是在这个回调里根据 `req.url` 决定调用哪段代码来填写 `resp`。本讲的 `startServer()` 就是一个手写的迷你路由。

### 2.2 为什么需要「模块服务器」

传统做法是先把 TS 编译打包成一个 bundle，浏览器再加载这个大文件；改一行代码也要重新打包。开发期的另一种思路是：**浏览器直接以 ES 模块（`<script type=module>`）的方式按需加载一个个模块**，服务器在响应时把 `.ts` 即时编译成 `.js` 并改写 `import` 路径。这就是 [esmoduleserve](https://www.npmjs.com/package/esmoduleserve)（`esmoduleserve/moduleserver`）扮演的角色，u1-l4 里 `_m/demo.js` 这个「磁盘上不存在的文件」正是它提供的。

### 2.3 serve-static 与「静态文件服务」

[serve-static](https://www.npmjs.com/package/serve-static) 是 Express 作者出的一个小工具：给它一个根目录，它就能把目录下的文件按 URL 对外发布（访问 `/` 时默认返回该目录的 `index.html`）。它的 API 是回调式的：`serveStatic(req, resp, next)`，找不到文件时通过第三个参数 `next(err)` 把错误交还给你处理——本讲的 404 正是挂在这个回调上的。

### 2.4 符号链接（symlink）

符号链接是一个「内容为另一个路径」的特殊文件。本仓库 `demo/test/` 下的 `mocha.js`、`mocha.css` 就是两个 git 跟踪的符号链接，指向 `../../node_modules/mocha/` 下的同名文件——这个设计是理解 `/test/` 页面的关键，4.3 节详述。

### 2.5 mocha

mocha 是浏览器/Node 通用的 JavaScript 测试框架，提供 `describe`/`it` 这样的用例组织语法。CodeMirror 各包的浏览器测试最终由一张 mocha 页面驱动。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) | CLI 入口 | `startServer()`（L125-L151）与 `devserver()`（L153-L160）两个函数 |
| [demo/index.html](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html) | demo 页面 | L13 的 `_m/demo.js` 引用，即模块服务器的入口 |
| [demo/test/mocha.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.js#L1) | 符号链接 | 指向 `../../node_modules/mocha/mocha.js`，把 mocha 资产纳入静态服务范围 |
| [demo/test/mocha.css](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.css#L1) | 符号链接 | 指向 `../../node_modules/mocha/mocha.css`，作用同上 |
| [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) | 根包定义 | L13-L14 声明 `esmoduleserve`、`serve-static` 依赖；L7 的 `dev` 脚本 |
| [README.md](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md) | 项目说明 | L19-L21 对 dev server 行为的官方描述（8090 端口、`/test/` 地址） |

依赖来源一览（可直接在根 [package.json:11-17](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L11-L17) 验证）：

| 模块 | 声明位置 | 本讲用途 |
| --- | --- | --- |
| `esmoduleserve` | 根 devDependencies（`^0.2.0`） | ES 模块按需编译服务 |
| `serve-static` | 根 devDependencies（`^1.14.1`） | demo 目录静态文件服务 |
| `@marijn/buildtool` | **不在**根 devDependencies 中 | `watch()` 增量重建，是工具链的传递依赖（可用 `npm ls @marijn/buildtool` 验证，待本地验证） |
| `@marijn/testtool` | **不在**根 devDependencies 中 | `gatherTests`/`testHTML`，同为传递依赖（待本地验证） |

注意这两个 `@marijn/*` 工具都是**函数体内惰性 require**（分别出现在 `devserver()` 与 `startServer()` 里），延续了 u1-l2 讲过的「cm.js 顶层只 require Node 内置模块」的纪律。

## 4. 核心概念与源码讲解

### 4.1 devserver()：watch 调用与 --source-map 的去向

#### 4.1.1 概念说明

`devserver` 命令（映射表注册于 [bin/cm.js:21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L21)，帮助文本见 [bin/cm.js:45-46](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L45-L46)）是「自动重建」与「HTTP 服务」两件事的组合：先启动 `buildtool.watch` 监视源码变化并重建各包 `dist/`，再启动 HTTP 服务器。理解它的意义在于分清**两条并行的代码到浏览器路径**：

- 路径 A（watch）：包源码变化 → 重建该包 `dist/` 下的编译产物；
- 路径 B（模块服务器）：浏览器请求模块 → 服务器即时转译 `.ts`。

`maxDepth` 的存在让这两条路径衔接起来（见 4.2.3）。`--source-map` 是这条命令唯一的选项。

#### 4.1.2 核心流程

```text
npm run dev                        # 即 node bin/cm.js devserver（package.json L7）
  └─ devserver(...args)
       ├─ 组装 options：
       │    sourceMap: args 里是否有 '--source-map'
       │    再展开 buildhelper 的 options 作为默认值
       ├─ buildtool.watch(入口列表, [demo/demo.ts], options)   # 后台持续重建
       └─ startServer()                                        # 前台 HTTP 服务
```

`--source-map` 的去向一句话就能说清：它只进入传给 `watch()` 的 `options` 对象（影响编译产物是否带 source map），`startServer()` 完全接收不到它——HTTP 层没有任何行为分支与它相关。

#### 4.1.3 源码精读

[bin/cm.js:153-160](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L153-L160) 是 `devserver` 的全部实现：

```js
function devserver(...args) {
  let options = {
    sourceMap : args.includes('--source-map'),
    ...require("@codemirror/buildhelper/src/options").options
  }
  require("@marijn/buildtool").watch(buildPackages.map(p => p.main).filter(f => f), [join(root, "demo/demo.ts")], options)
  startServer()
}
```

逐行解读：

- **L153** `...args` 收集全部剩余参数。注意 rest 参数不计入函数的 `length` 属性，所以 u1-l3 讲过的「参数下限校验」对它恒通过——这正是需要接收任意标志的命令该有的签名。
- **L154-L157** 先把 `sourceMap` 布尔值放进 `options`，再展开 buildhelper 的 options。展开顺序有讲究：`...` 在后，会**覆盖**前面的同名键；也就是说若 buildhelper 的 options 里也定义了 `sourceMap`，命令行的设置会被抹掉。以当前 [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) 依赖的 buildhelper 版本而言其 options 是否含 `sourceMap` 需查 `node_modules/@codemirror/buildhelper/src/options.ts` 确认（待本地验证）。
- **L158** `watch()` 的第一个参数是 `buildPackages.map(p => p.main).filter(f => f)`——u2-l2 讲过 `buildPackages` 是 36 个包中 `main` 非空的 35 个，这里的 `filter(f => f)` 再兜一层底，把仍为 `null` 的入口（例如某个包目录被手工删掉后未重新 `loadPackages()`）剔除，避免把 `null` 喂给 watcher。第二个参数把 [demo/demo.ts](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts#L1-L11) 也纳入监视——所以改 demo 也会触发重建。
- **L159** `startServer()` 是同步调用，服务器启动后事件循环常驻，进程不会退出。

命令的对外入口有两处：脚本别名 [package.json:7](https://github.comcode.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L7)（`"dev": "node bin/cm.js devserver"`）与 README 的说明 [README.md:19-21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L19-L21)。

#### 4.1.4 代码实践：追踪 --source-map 的去向

一个纯源码阅读型实践（不需要启动服务器就能完成大半）：

1. **实践目标**：用静态证据确认 `--source-map` 只影响 watch、不影响 HTTP 层。
2. **操作步骤**：
   - 在仓库根执行 `grep -n "source-map\|sourceMap" bin/cm.js`。
   - 对每处命中，读它的上下文，判断这个值流向了哪个函数。
3. **需要观察的现象**：预期只有两处命中——帮助文本 [bin/cm.js:45](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L45) 和 `devserver()` 里的 `sourceMap : args.includes('--source-map')`（L155）。
4. **预期结果**：`startServer()` 中没有任何引用；结论「该选项与端口、路由无关」成立。
5. 进阶（待本地验证）：完成 `cm install` 后运行 `npm run dev -- --source-map`，对比不加该选项时的终端输出与各包 `dist/` 产物（是否多出 `.map` 文件）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `devserver` 的签名是 `...args`，而 `packages` 命令的签名可以是零参数列表？
**答案**：`devserver` 需要接收可变数量的标志（目前只有 `--source-map`，将来可能增加）；零参函数 `length` 为 0，任何多余参数都会因 `cmdFn.length > args.length` 不成立而放过——两者效果都允许任意参数，但 rest 参数把 args 收集成数组供 `includes` 消费，写法更直接。

**练习 2**：把 L154-L157 的展开顺序反过来（先 `...options` 再写 `sourceMap:`），行为会有什么不同？
**答案**：反过来后命令行标志的优先级更高：`sourceMap` 显式键写在后面会覆盖 buildhelper options 里的同名键。当前写法是「buildhelper 为准」，调换后是「命令行为准」。这是一个真实的优先级语义差异。

**练习 3**：`watch()` 为什么监视的是各包的入口 `main` 而不是各包的全部源文件？
**答案**：构建工具自身会从入口出发沿 import 递归找到全部依赖源文件（u2-l2 讲过的构建单元），watcher 只需要知道「每个构建的根」；把 `demo.ts` 单独追加进列表，是因为它不属于任何包，却是 demo 页面的构建根。

### 4.2 startServer()：模块服务、静态服务与回退链

#### 4.2.1 概念说明

`startServer()` 是本讲的主角。它要解决的问题：**一个端口（8090）上同时服务三种内容**——按需编译的 ES 模块、demo 目录的静态文件、运行时生成的测试页。Node 的 `http.createServer` 回调天然适合手写「先试 A，不行再试 B」的级联路由，这个 25 行的函数就是一个极简但生产可用的网关雏形。

#### 4.2.2 核心流程

```text
启动阶段（一次性）：
  serve = <仓库根>/demo                     # 服务根目录
  moduleserver = esmoduleserve 实例（root=demo, maxDepth=2）
  serveStatic  = serve-static 实例（root=demo，带 setHeaders 钩子）

每个请求 (req, resp)：
  ├─ req.url 匹配 /^\/test\/?($|\?)/ ？
  │    是 → gatherTests 收集测试 → testHTML 拼页面 → resp.end(html)   【4.3 节】
  │    否 → moduleserver.handleRequest(req, resp)
  │           ├─ 返回真值 → 该请求已被模块服务器响应，结束
  │           └─ 返回假值 → serveStatic(req, resp, _err => 404 "Not found")
  │                          ├─ 找到文件 → 静态响应
  │                          └─ 找不到 → 回调触发 → 404

监听：.listen(8090, OPEN 环境变量存在 ? 所有网卡 : 仅 127.0.0.1)
```

#### 4.2.3 源码精读

**第一段：装配两个服务实例**，见 [bin/cm.js:126-132](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L126-L132)：

```js
let serve = join(root, "demo")
let moduleserver = new (require("esmoduleserve/moduleserver"))({root: serve, maxDepth: 2})
let serveStatic = require("serve-static")(serve, {
  setHeaders(res, path) {
    if (/try\/mods\//.test(path)) res.setHeader("Access-Control-Allow-Origin", "*")
  }
})
```

- `serve` 是两者的共同根目录：`demo/`。
- `new (require(...))({...})` 这对括号是 JS 语法点：不加括号时 `new require("x")()` 的解析结果会不同；括号先求值 require 得到类，再对该类 `new` 并传入选项对象。
- `maxDepth: 2` 是模块服务器的「即时转译深度」：入口及其浅层依赖由服务器实时把 TS 转成 JS 并改写 import；更深的依赖则指向各包 `dist/` 里由 watch 持续重建的编译产物。这解释了 4.1 说的两条路径为何配合：**浅层走源码（改动秒级可见），深层走 dist（由 watch 兜底重建）**。该选项的精确语义可读本地 `node_modules/esmoduleserve/README.md` 确认（待本地验证）。
- `setHeaders` 是 serve-static 的钩子，在每个响应上追加头：路径匹配 `try\/mods\//` 时开 `Access-Control-Allow-Origin: *` 允许跨域。消费方在本仓库之外（为网页端试用环境从开发服务器拉取模式文件而设，具体使用方待确认）。顺带一提：这个回调的第二个参数名叫 `path`，**遮蔽**了顶部的 `path` 模块——在这个回调里不能用 `path.join`，这是读代码时容易踩的暗坑。

**第二段：级联路由**，核心是 [bin/cm.js:144-147](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L144-L147) 的 else 分支：

```js
moduleserver.handleRequest(req, resp) || serveStatic(req, resp, _err => {
  resp.statusCode = 404
  resp.end('Not found')
})
```

- `handleRequest` 的契约：请求属于模块服务器管辖（URL 位于其模块 URL 空间 `_m/` 下——demo 页面正是通过 [demo/index.html:13](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html#L13) 的 `<script type=module src="_m/demo.js">` 引用它的；本仓库没有传入前缀选项，`_m/` 即库的默认前缀）时，它处理并响应，返回真值；否则**不碰响应**，返回假值。
- `||` 短路构成回退：真值 → 整个表达式结束（serveStatic 不会执行）；假值 → 执行 `serveStatic(...)`。
- `serveStatic` 的第三个参数是「最终回调」：静态服务也找不到文件时被调用，参数名写成 `_err` 表示刻意忽略错误细节，统一回一个 200 之外的 `404 Not found` 纯文本。
- 于是日常访问的完整链条是：`http://localhost:8090/` → 不匹配 `/test/`、不属于 `_m/` → serveStatic 返回 `demo/index.html` → 浏览器解析到 `_m/demo.js` → 第二个请求命中模块服务器 → 即时编译 `demo.ts` 并递归服务依赖。

**第三段：监听地址**，见 [bin/cm.js:149-150](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L149-L150)：

```js
}).listen(8090, process.env.OPEN ? undefined : "127.0.0.1")
console.log("Dev server listening on 8090")
```

- 端口 `8090` 硬编码在 `listen` 调用里；README（[README.md:21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L21)）和帮助文本（L46）只是文档性描述。改端口必须改 `listen`，日志与文档建议同步改（综合实践会做一遍）。
- host 参数：设了 `OPEN` 环境变量则传 `undefined`（Node 默认监听所有网卡，局域网可访问），否则显式绑定回环地址 `127.0.0.1`——默认不对外暴露，这是个安全友好的默认值。

#### 4.2.4 代码实践：观察回退链与 404

1. **实践目标**：用 curl 验证「模块服务 → 静态服务 → 404」三级回退真实存在。
2. **操作步骤**（前提：已完成 `cm install` 并 `npm run dev` 启动服务器）：
   - `curl -s http://127.0.0.1:8090/ | head -5` —— 应命中 serveStatic 返回 `demo/index.html`；
   - `curl -s http://127.0.0.1:8090/_m/demo.js | head -5` —— 应命中模块服务器，看到转译后的 JS；
   - `curl -si http://127.0.0.1:8090/no-such-file` —— 应返回 `HTTP/1.1 404` 与正文 `Not found`；
   - 再开一个终端执行 `OPEN=1 npm run dev`（先停掉前一个），用 `ss -ltn | grep 8090` 对比两次的监听地址。
3. **需要观察的现象**：三类 URL 分别走不同分支；404 的正文正是一字不差的 `Not found`；`OPEN=1` 时监听地址从 `127.0.0.1:8090` 变为 `0.0.0.0:8090` 或 `*:8090`（取决于系统工具的显示）。
4. **预期结果**：如上。若 `ss` 不可用可换 `netstat -ltn`。（待本地验证）

#### 4.2.5 小练习与答案

**练习 1**：`handleRequest` 为什么能放心地把「不属于我」的请求留给 `serveStatic`？它必须遵守什么契约？
**答案**：契约是「返回假值前不得向 `resp` 写入任何内容」（不 writeHead、不 end）。只要守住这一点，后续处理器可以像请求从未到达过一样继续处理。这个「返回布尔值表达是否已处理」的模式是手写级联路由的核心。

**练习 2**：如果把 `moduleserver.handleRequest(req, resp) || serveStatic(...)` 改成先后都无条件调用，会发生什么？
**答案**：对模块 URL，`handleRequest` 已经 `end` 了响应，再调用 `serveStatic` 会向已结束的响应写头/写体，Node 会抛 `ERR_HTTP_HEADERS_SENT` 之类的错误（或打警告并静默失败，取决于版本）。短路 `||` 正是防这个的。

**练习 3**：为什么 404 逻辑放在 `serveStatic` 的回调参数里，而不是在 `serveStatic(...)` 调用之后紧接着写 `resp.statusCode = 404`？
**答案**：serve-static 是异步、回调风格的：函数立即返回时文件还没找到（甚至可能还在传输），「没找到」这个事实只能通过它的完成回调告知。若在调用后同步写 404，会在 serve-static 仍在处理时抢写响应。回调是这类异步 API 表达「兜底」的标准位置。

### 4.3 /test/ 请求分支：运行时生成 mocha 测试页

#### 4.3.1 概念说明

访问 `http://localhost:8090/test/` 得到的那张测试页，磁盘上**不存在对应文件**——它是每次请求时现场生成的：服务器调用测试工具收集全部包的浏览器测试文件，套进一个 mocha 骨架 HTML 里返回。这种「运行时生成」换来的是**永远新鲜**：新增测试文件、改动测试代码，刷新页面即生效，无需任何注册或清单维护。理解这个分支也就理解了 u2-l4 将要讲的 `cm test` 命令的浏览器侧一半——两者共用同一个 `gatherTests`。

#### 4.3.2 核心流程

```text
GET /test/ （或 /test、/test?...）
  ├─ 正则 /^\/test\/?($|\?)/ 匹配 req.url
  ├─ 惰性 require @marijn/testtool
  ├─ gatherTests(35 个包目录) → { tests, browserTests }   # 只用 browserTests
  ├─ 把绝对路径转成相对 demo/ 的路径（模块/静态服务都根植于 demo/）
  └─ testHTML(相对路径列表, {html: 标题 + 隐藏工作区 div}) → resp.end(HTML 字符串)
```

每次请求都重新执行 `gatherTests`——注意这意味着**收集发生在请求时刻**，而不是服务器启动时刻。

#### 4.3.3 源码精读

完整分支在 [bin/cm.js:134-142](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L134-L142)：

```js
if (/^\/test\/?($|\?)/.test(req.url)) {
  let runTests = require("@marijn/testtool")
  let {browserTests} = runTests.gatherTests(buildPackages.map(p => p.dir))
  resp.writeHead(200, {"content-type": "text/html"})
  resp.end(runTests.testHTML(browserTests.map(f => path.relative(serve, f)), {
    html: `<title>CM6 view tests</title>
<h1>CM6 view tests</h1>
<div id="workspace" style="opacity: 0; position: fixed; top: 0; left: 0; width: 20em;"></div>`
  }))
}
```

逐行解读：

- **L134 正则** `/^\/test\/?($|\?)/`：拆开看是「`/test` + 可选的 `/` + (字符串结束或 `?`)」。它精确匹配 `/test`、`/test/`、`/test?grep=...`；不匹配 `/testing`（`ing` 既非结束也非 `?`）、`/test/foo`。注意 `req.url` 含查询串，所以必须显式允许 `\?`，否则 `/test/?grep=x` 这类带参数的访问会漏进静态分支。
- **L135** `@marijn/testtool` 惰性加载，只有真的访问测试页才付出加载成本。
- **L136** `gatherTests(buildPackages.map(p => p.dir))` 扫描 35 个包目录收集测试，返回 `{tests, browserTests}` 两个数组；这里**只解构使用 `browserTests`**（Node 侧的 `tests` 留给 `cm test --no-browser`，见 [bin/cm.js:352-363](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L363) 的 `test()`，u2-l4 详讲）。
- **L138** `browserTests.map(f => path.relative(serve, f))`：收集到的是绝对路径，而模块服务器与静态服务都以 `demo/` 为根，转成相对路径后才能被页面以正确的 URL 引用。
- **L138-142 `testHTML` 的第二个参数**：`html` 选项注入页面的自定义片段——标题、一级标题，以及一个**视觉隐藏的 `#workspace` div**（`opacity: 0; position: fixed`）。这是 view 等包的浏览器测试需要的挂载容器：测试代码往里塞编辑器 DOM 做断言，又不干扰 mocha 的结果展示。一个 `20em` 宽的隐藏工作区，是「页面即测试夹具」思想的极简体现。

**mocha 资产为什么是两个符号链接**：[demo/test/mocha.js:1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.js#L1) 与 [demo/test/mocha.css:1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.css#L1) 这两个 git 跟踪的文件内容只有一行——分别是指向 `../../node_modules/mocha/mocha.js` 和 `../../node_modules/mocha/mocha.css` 的链接目标路径。原因：静态服务的根是 `demo/`，`node_modules` 在根之外够不着；测试页需要加载 mocha，于是用符号链接把 npm 依赖的 mocha「搬进」可服务的范围，且始终跟随 `cm install` 装下的具体版本，无需复制文件。`demo/` 下还有一个同技巧的 [demo/website](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/website) 符号链接指向 `../website/output/`（其目标是否存在于本地取决于其他仓库的检出与构建状态，待本地验证）。

#### 4.3.4 代码实践：亲眼确认测试页是「现做的」

1. **实践目标**：验证 `/test/` 页面没有磁盘文件对应、内容在请求时刻生成。
2. **操作步骤**：
   - `ls demo/test/` 并 `readlink demo/test/mocha.js` —— 前者应列出两个链接文件，后者打印 `../../node_modules/mocha/mocha.js`（这一步不需要启动服务器，符号链接本身就在仓库里）；
   - 启动 dev server 后执行 `curl -s http://127.0.0.1:8090/test/ | head -30`；
   - 在返回的 HTML 里搜索 `mocha`（`curl -s ... | grep -io mocha | head`），确认页面引用了 mocha 资产；
   - 再 `curl -s http://127.0.0.1:8090/testing` 与 `curl -s http://127.0.0.1:8090/test/foo`，观察它们是否落入 404。
3. **需要观察的现象**：`/test/` 返回一张引用了 mocha 与各包测试模块的 HTML；仓库里找不到任何与它对应的 HTML 文件；`/testing`、`/test/foo` 不是测试页。
4. **预期结果**：如上，直观建立「运行时生成」与「正则边界」两个概念。（curl 部分待本地验证）

#### 4.3.5 小练习与答案

**练习 1**：把正则换成 `req.url.startsWith("/test")` 会引入什么 bug？
**答案**：`/testing`、`/test-anything` 都会被误判成测试页请求；同时原正则允许的 `/test?grep=x`（无斜杠带查询）`startsWith` 也能匹配，这点倒是兼容。核心问题是前缀匹配放宽了边界，`/test/foo` 从 404/静态回退变成了测试页。

**练习 2**：为什么这个分支放在级联的最前面，而不是放在 `serveStatic` 之后？
**答案**：`demo/test/` 目录真实存在（装着两个符号链接），若静态服务先行，`/test/` 会被解析成目录请求并可能命中目录索引或 404，永远轮不到生成逻辑。动态路由优先于静态回退是这类网关的通用排序原则。

**练习 3**：如果某个包新增了一个浏览器测试文件，需要在哪里「注册」它才能出现在测试页上？
**答案**：哪里都不用改。`gatherTests` 在每次请求时扫描各包目录，新文件自动被发现——这正是运行时生成方案的核心收益（具体收集规则如目录名、文件名约定由 `@marijn/testtool` 决定，可在 `node_modules/@marijn/testtool` 里阅读确认，待本地验证）。

## 5. 综合实践

**任务**：给 dev server 换端口并新增一条 `/hello` 路由——一个改动触达本讲全部三个最小模块（watch 无关但需重启生效、路由级联、监听配置）。

1. **实践目标**：掌握端口与路由的修改方法，并验证级联路由的插入位置。
2. **操作步骤**：
   - 前提：已完成 `node bin/cm.js install`（`node_modules` 存在，mocha 符号链接可用）。
   - **换端口**：把 [bin/cm.js:149](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L149) 的 `.listen(8090, ...)` 改为 `.listen(8091, ...)`，同步把 L150 日志与 L46 帮助文本里的 `8090` 改成 `8091`（后两处不影响功能，但保持文档诚实）。
   - **加路由**：在 `/test/` 的 `if`（L134）**之前**插入一个分支（示例代码，非项目原有代码）：

     ```js
     if (req.url == "/hello") {
       resp.writeHead(200, {"content-type": "text/plain; charset=utf8"})
       resp.end("hello from cm devserver")
     } else if (/^\/test\/?($|\?)/.test(req.url)) {
     ```

     （原 `} else {` 保持不变，即整条链变成 `/hello` → `/test/` → 模块/静态回退。）
   - Ctrl-C 停掉旧进程，重新 `npm run dev`。
   - 验证：`curl -i http://127.0.0.1:8091/hello`（预期 `200` + `text/plain` + 正文）；`curl -I http://127.0.0.1:8091/`（预期静态返回 `demo/index.html`）；`curl -I http://127.0.0.1:8090/`（预期连接被拒绝，旧端口已无人监听）。
3. **需要观察的现象**：新端口生效、`/hello` 命中自定义分支、其余路径行为与改动前完全一致。
4. **预期结果**：如上。（待本地验证）
5. **收尾**：`git checkout -- bin/cm.js` 还原改动。这是本地实验代码——按 CONTRIBUTING 的规范，一个 PR 只含一个真实改动，这类练习不要混进提交。

## 6. 本讲小结

- `devserver()` 是「`buildtool.watch` 增量重建 + `startServer()` HTTP 服务」的组合；`--source-map` 只进入 watch 的 options，与 HTTP 层无关。
- `startServer()` 以 `demo/` 为根装配两个服务：esmoduleserve 实例（`root` + `maxDepth: 2`，浅层依赖即时转译、深层依赖指向 watch 重建的 dist）与 serve-static 实例（附带 `try/mods/` 路径的 CORS 钩子）。
- 路由是三级级联：`/test/` 正则分支 → `moduleserver.handleRequest`（真值即短路）→ `serveStatic`（找不到时经回调回 404 `Not found`）。
- `/test/` 页面在请求时刻由 `gatherTests` + `testHTML` 现场生成：只消费 `browserTests`、路径转相对 `demo/`、`html` 选项注入隐藏 `#workspace` 工作区；mocha 资产靠 `demo/test/` 下两个指向 `node_modules/mocha` 的 git 符号链接进入静态服务范围。
- 监听配置在 `.listen(8090, OPEN ? undefined : "127.0.0.1")`：端口硬编码，默认只绑回环地址，设 `OPEN` 才对局域网开放。

## 7. 下一步学习建议

本讲补齐了 dev server 的 HTTP 内部；下一讲 **u2-l4（测试体系：cm test 与浏览器/Node 双轨测试）**将沿本讲埋下的两条线继续：`test()` 命令（[bin/cm.js:352-364](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L364)）如何消费同一个 `gatherTests` 的 `tests`（Node 轨）与 `browserTests`（浏览器轨），以及 `--no-browser`、`--grep` 等参数的解析。继续阅读源码时，建议优先看本地 `node_modules/@marijn/testtool` 与 `node_modules/esmoduleserve` 的 README（若已 `cm install`），把本讲两处「待本地验证」的库行为补齐。
