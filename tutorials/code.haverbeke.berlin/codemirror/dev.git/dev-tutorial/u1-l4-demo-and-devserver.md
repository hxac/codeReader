# 看见编辑器：demo 应用与 dev 服务器初体验

## 1. 本讲目标

前几讲我们一直在和命令行脚本打交道：装好了三十多个包仓库（`cm install`），读懂了 `cm.js` 的命令分发骨架。但 CodeMirror 终究是一个**在浏览器里运行的编辑器**，只看脚本看不到它的真面目。

本讲结束时，你应该能够：

1. 读懂 [demo/demo.ts](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts) 中 `EditorView` 的 `doc`、`extensions`、`parent` 三个配置项各自的作用。
2. 说清楚 `demo/index.html` 引用的 `_m/demo.js` 为什么在磁盘上**不存在**、它是从哪里来的。
3. 理解 `cm.js` 中 `devserver()` 与 `startServer()` 的分工：一个负责「变了就重建」，一个负责「请求来了给内容」。
4. 会用 `npm run dev` 启动 8090 端口的开发服务器，访问 demo 页面和 `/test/` 浏览器测试页面。

## 2. 前置知识

本讲需要几个新概念，用通俗语言先解释：

- **EditorView（编辑器视图）**：CodeMirror 6 的核心类，代表「一个活在页面上的编辑器实例」。它不是 HTML 标签，而是一个由 JS 创建、挂到某个 DOM 节点下的组件。创建它时传入的 `doc`（初始文本）、`extensions`（功能扩展列表）、`parent`（挂到哪个 DOM 节点）是最基本的三个配置。
- **扩展（extension）**：CodeMirror 6 把语法高亮、快捷键、历史记录等所有功能都做成可选的「扩展」，像插积木一样在 `extensions` 数组里组合。`basicSetup` 是官方预配好的一揽子常用扩展。
- **ES 模块与 `<script type=module>`**：现代浏览器原生支持的 JS 模块机制。HTML 里写 `<script type=module src="...">` 后，浏览器会请求该文件，并顺着其中的 `import` 语句继续请求依赖模块。
- **静态文件服务器**：把磁盘上的文件按 URL 原样返回的 HTTP 服务，例如请求 `/index.html` 就返回该文件的内容。
- **按需编译的模块服务器**：本仓库用的 `esmoduleserve` 库，除了返回文件，还能把 TypeScript 即时编译成浏览器可执行的 JS。它约定用一个 `_m/` 前缀的 URL 空间来暴露这些「虚拟模块」。

另外请确认你已经完成 [u1-l2](u1-l2-install-and-setup.md) 的 `node bin/cm.js install`：dev server 启动时会先经过 `assertInstalled()` 守卫检查各包目录是否存在（[bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)），没装好会直接报错退出。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [demo/demo.ts](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts) | demo 应用全部源码：创建一个带 JS 语法支持的编辑器，共 11 行 |
| [demo/index.html](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html) | demo 页面骨架：少量样式 + 一行模块加载 |
| [bin/cm.js](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js) | `devserver()`（L153-L160）与 `startServer()`（L125-L151）是本讲主角 |
| [package.json](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json) | `dev` 脚本别名与 `esmoduleserve`、`serve-static` 两个服务器依赖 |

一个值得先记住的事实：`demo/` 目录下**只有 `demo.ts` 和 `index.html` 两个文件**，没有任何构建产物。构建产物由 dev server 在内存中按需生成——这是理解全讲的钥匙。

## 4. 核心概念与源码讲解

### 4.1 demo.ts：11 行搭出一个编辑器

#### 4.1.1 概念说明

npm 上叫 `codemirror` 的那个包（回忆 u1-l2：它的源码实际住在 `basic-setup` 仓库）是官方提供的「全家桶」：一条 import 同时带出 `EditorView` 视图类和 `basicSetup` 常用扩展集。再配上 `@codemirror/lang-javascript` 包提供的 `javascript()` 语言扩展，就得到一个带语法高亮、自动补全、历史记录的 JS 编辑器。

demo.ts 还把创建好的实例挂到了 `window.view` 上——这不是业务需要，而是为了调试方便：打开浏览器控制台就能直接操作这个编辑器。

#### 4.1.2 核心流程

```text
import 编辑器组件
    ↓
new EditorView({
  doc:        初始文档内容（一个字符串）
  extensions: 功能扩展列表（basicSetup + 语言支持）
  parent:     挂载目标 DOM 节点
})
    ↓
(document.body 下出现一个可编辑的 .cm-editor 节点)
```

#### 4.1.3 源码精读

先看导入部分，这里引出了两个包仓库：

[demo/demo.ts:L1-L2](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts#L1-L2) —— 从 `codemirror` 包导入视图类与基础扩展集，从 `@codemirror/lang-javascript` 导入语言构造函数。前者由 `basic-setup` 仓库提供（见 [bin/cm.js:L93](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L93) 中 `pkg.name == "codemirror" ? "basic-setup" : pkg.name` 的特判），后者由 `lang-javascript` 仓库提供。

再看主体，三行配置一个编辑器：

[demo/demo.ts:L4-L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts#L4-L11) —— 创建 `EditorView` 实例并挂到 `window` 上。逐项拆解：

| 配置项 | 值 | 含义 |
| --- | --- | --- |
| `doc` | `'console.log("Hello world")'` | 初始文档内容，纯字符串 |
| `extensions` | `[basicSetup, javascript()]` | 先装基础扩展集，再装 JS 语言支持（高亮、缩进规则等） |
| `parent` | `document.body` | 编辑器 DOM 挂载到页面 body 下 |

`(window as any).view =` 中的 `as any` 是 TypeScript 断言：`window` 上本来没有 `view` 属性，断言绕过类型检查，换来控制台里可以直接敲 `view.state.doc.toString()` 之类的调试命令。

#### 4.1.4 代码实践

**实践目标**：在浏览器控制台里亲手操作这个编辑器实例，验证 `window.view` 的用途。

**操作步骤**：

1. 先完成本讲综合实践中的 `npm run dev` 启动，打开 `http://localhost:8090`。
2. 打开浏览器开发者工具的控制台（F12 → Console）。
3. 依次输入：
   - `view.state.doc.toString()` —— 读出当前文档全文
   - `view.dispatch({changes: {from: 0, insert: "// 注释\n"}})` —— 在文档开头插入一行
   - `view.dom` —— 查看编辑器的 DOM 节点，确认它在 `body` 下

**需要观察的现象**：第 2 组命令执行后，页面上编辑器第一行立即出现插入的文本；`view.dom` 的父节点是 `body`。

**预期结果**：三条命令都有输出，且 `dispatch` 后页面所见内容与 `view.state.doc.toString()` 一致。待本地验证（编辑器的具体行为依赖各包构建是否完成）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `parent: document.body` 删掉，编辑器还能出现在页面上吗？

**答案**：不能。`parent` 是把编辑器接入文档树的挂载点，没有它 `EditorView` 依然会被创建（实例存在、`window.view` 也能访问），但它的 DOM 不插入页面，用户什么都看不到。CodeMirror 也支持先创建、后手动调用 `view.mount(parent)` 的用法，但本 demo 走的是最简单的直接挂载。

**练习 2**：`extensions` 数组里 `basicSetup` 不带括号、`javascript()` 带括号，为什么？

**答案**：`basicSetup` 本身就是一个「已经打包好的扩展数组」（一个值），直接放进去即可；`javascript()` 是**构造函数**，调用后才返回语言扩展。前者是成品，后者是工厂。

**练习 3**：demo.ts 没有写一行 HTML，页面标题「CM6 demo」和编辑器的 300px 高度是从哪来的？

**答案**：来自 `demo/index.html`——标题是它的 `<title>` 标签，高度是其中 `.cm-editor { height: 300px; ... }` 这条 CSS。demo.ts 只负责生成编辑器结构，页面骨架和样式都在 HTML 里（见下一模块）。

### 4.2 index.html 与 `_m/demo.js`：一个不存在的文件

#### 4.2.1 概念说明

浏览器不能直接运行 TypeScript。`demo/index.html` 却加载了 `_m/demo.js`——而 `demo/` 目录下根本没有 `_m/` 这个目录（可用 `ls demo/` 验证，只有 `demo.ts` 和 `index.html`）。

谜底在 dev server：`esmoduleserve` 库约定了一个**虚拟 URL 空间** `_m/`。浏览器请求 `_m/demo.js` 时，服务器把磁盘上的 `demo/demo.ts` 即时编译成 JS 返回；返回内容里的 `import "codemirror"` 等语句也被改写成 `_m/` 形式的 URL，浏览器顺着这些 URL 再请求，服务器继续按需编译对应的模块（各包构建出的 dist 文件）。也就是说：**整条模块依赖链都是服务器现场翻译出来的**，磁盘上始终没有 `_m` 目录。

#### 4.2.2 核心流程

```text
浏览器                          dev server (8090)
  │  GET /                          │
  │◀── demo/index.html（静态文件）── │  serve-static 提供
  │
  │  GET /_m/demo.js                │
  │◀── 编译 demo.ts 得到的 JS ───── │  esmoduleserve 按需编译
  │     其中 import 被改写为 _m/..  │
  │
  │  GET /_m/<依赖模块>  ……递归直到全部加载
```

#### 4.2.3 源码精读

[demo/index.html:L13](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html#L13) —— 整个页面的脚本加载只有这一行：`<script type=module src="_m/demo.js"></script>`。`type=module` 触发浏览器的 ES 模块加载机制，`_m/demo.js` 就是对 `demo/demo.ts` 的虚拟编译产物 URL。

[demo/index.html:L6-L9](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html#L6-L9) —— 页面全部样式：给 `.cm-editor`（EditorView 生成的根节点 class）定 300px 高度和边框，给 `.cm-scroller`（内容滚动区）开滚动。这说明 demo 的呈现层极薄：结构靠 demo.ts，外观靠这几行 CSS。

[demo/index.html:L1-L4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/index.html#L1-L4) —— doctype、字符集、视口和标题，一个最小化 HTML5 骨架（连 `<head>`/`<body>` 标签都省略了，浏览器会自动补全）。

对应的服务端配置在 [bin/cm.js:L127](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L127) —— `new (require("esmoduleserve/moduleserver"))({root: serve, maxDepth: 2})`：以 `demo/` 目录为服务根创建模块服务器；`maxDepth: 2` 允许它解析位于服务根之外的模块——这是必须的，因为 `demo.ts` 依赖的各包 dist 和 `node_modules` 都在 `demo/` 的上一级目录（具体语义可查阅 esmoduleserve 文档，待确认）。

#### 4.2.4 代码实践

**实践目标**：亲眼确认 `_m/demo.js` 是服务器生成的编译产物，不是磁盘文件。

**操作步骤**：

1. 在仓库根目录执行 `ls demo/`，确认输出只有 `demo.ts` 和 `index.html`，没有 `_m`。
2. 启动 dev server（见 4.4 或综合实践）。
3. 另开终端执行 `curl -s http://localhost:8090/_m/demo.js`。
4. 再执行 `curl -s http://localhost:8090/ | head -20`，对照 index.html 原文。

**需要观察的现象**：第 3 步返回的是**纯 JavaScript**——`import` 语句的目标已变成 `_m/` 开头的 URL，`(window as any)` 的类型断言已被擦除；第 4 歏返回的 HTML 与磁盘上的 `demo/index.html` 一致。

**预期结果**：`_m/demo.js` 能被 curl 正常获取（HTTP 200），内容为编译后的 JS；而磁盘上找不到这个文件。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 index.html 不直接写 `<script type=module src="demo.ts">`？

**答案**：浏览器不认识 TypeScript，也不会做模块名解析（`import "codemirror"` 这种裸模块名在浏览器里无法解析成 URL）。必须有一个服务器端环节把 TS 编译成 JS、把裸模块名改写成可请求的 URL——这正是 `_m/` 空间存在的意义。

**练习 2**：`demo/index.html` 里 `.cm-editor` 这个 class 名是谁定的？

**答案**：是 `EditorView` 自己——它创建的 DOM 根节点固定带 `cm-editor` class（内部结构还有 `cm-scroller` 等一系列 `cm-` 前缀 class）。index.html 的 CSS 只是「搭便车」对这些既定 class 做样式定制。

### 4.3 devserver()：先起监视器，再起服务器

#### 4.3.1 概念说明

`npm run dev` 背后就是 `cm.js` 的 `devserver` 子命令。它做两件事，顺序固定：

1. **启动文件监视**：盯着所有包的入口文件和 `demo/demo.ts`，任何源码变化就自动重建各包的 `dist` 产物。这就是 README 说的「代码变化时自动重建」。
2. **启动 HTTP 服务器**：提供上一模块讲的 `_m/` 模块服务和静态文件服务。

两件事各自由一个库承担：重建交给 `@marijn/buildtool` 的 `watch()`，服务交给 `startServer()`（下一模块）。

#### 4.3.2 核心流程

```text
npm run dev
  → node bin/cm.js devserver
    → assertInstalled() 守卫检查（start() 中，非 install/--help 命令都要过）
    → devserver(...args)
        1. 组装 options：--source-map 标志 + buildhelper 默认选项
        2. buildtool.watch(所有包入口 main 列表, [demo/demo.ts], options)
        3. startServer()   ← 监听 8090，进程常驻
```

#### 4.3.3 源码精读

[package.json:L7](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L7) —— `"dev": "node bin/cm.js devserver"`：`npm run dev` 只是 `cm devserver` 的别名，没有额外魔法（四个脚本与 cm 子命令的对应关系见 u1-l1）。

[bin/cm.js:L153-L160](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L153-L160) —— `devserver` 的全部实现，逐行看：

- L154-L157：`options` 对象。`sourceMap: args.includes('--source-map')` 解析可选的 source map 开关；随后展开 `@codemirror/buildhelper/src/options` 的默认构建选项。注意 `...` 展开写在 `sourceMap` **之后**，因此默认选项里若也含 `sourceMap` 键会覆盖前面的解析结果——这里依赖默认选项不含该键才生效（待确认默认选项的具体内容）。
- L158：`require("@marijn/buildtool").watch(...)`。第一个参数是 `buildPackages.map(p => p.main).filter(f => f)`——所有待构建包的入口文件（`filter` 剔除探测不到入口的包）；第二个参数是**额外**监视的文件列表，把本仓库自己的 `demo/demo.ts` 也纳入。之后源码一变，`watch` 就增量重建对应的 `dist`。
- L159：`startServer()` 起 HTTP 服务。`watch` 与服务器从此并行常驻，进程不退出。

[bin/cm.js:L21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L21) 与 [bin/cm.js:L45-L46](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L45-L46) —— 命令在映射表中的注册项，以及 help 文本里的对应说明（`cm devserver [--source-map]`，_start a dev server on port 8090_）。注意 `devserver(...args)` 用 rest 参数接收任意个参数，所以 `--source-map` 不会被参数个数校验拦截（回忆 u1-l3：rest 参数不计入 `length`）。

一个容易混淆的点：`watch` 重建的是**各包的 `dist` 产物**，而 `demo.ts` 本身不需要构建——它是被 `esmoduleserve` 在浏览器请求时**即时编译**的。所以「改 demo.ts」刷新即可见，「改包源码」则要等 `watch` 重建完 dist 才生效。

#### 4.3.4 代码实践

**实践目标**：验证 `--source-map` 选项从命令行到 `watch` 的完整传递路径。

**操作步骤**：

1. 通读 [bin/cm.js:L153-L160](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L153-L160)，在纸上写下 `--source-map` 经过哪几个变量最终进入 `watch` 的第三个参数。
2. 启动 `node bin/cm.js devserver --source-map`，再用浏览器开发者工具的 Sources 面板查看 `_m/demo.js` 是否关联了 source map（能跳回 TS 源码即生效）。
3. 对比不加该参数重启后的效果。

**需要观察的现象**：加选项时 Sources 面板里能看到 TS 原文件（或提示 source map 可用）；不加时只能看到编译后的 JS。

**预期结果**：传递路径为 `process.argv` → `args` → `args.includes('--source-map')` → `options.sourceMap` → `watch` 第三参。运行现象待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：`devserver()` 里为什么不像 `install()` 那样在函数体内才 `require` 依赖？

**答案**：`install` 必须能在 `node_modules` 尚不存在时运行，所以 cm.js 顶层只 require Node 内置模块、工具库一律函数内惰性加载（u1-l2 讲过）。`devserver` 运行时依赖必然已安装——`assertInstalled()` 已确认所有包目录存在，`npm install` 也已跑过——所以在哪 require 都行；它沿用函数内 require 只是保持全文件风格一致。

**练习 2**：`watch` 的第一个参数为什么要 `.filter(f => f)`？

**答案**：`buildPackages` 里个别包可能探测不出入口文件（`p.main` 为 `null`/空），`filter(f => f)` 把这些假值剔掉，避免把无效路径交给 `watch` 引发报错。这是 JavaScript 里「过滤假值」的惯用简写。

**练习 3**：把 `startServer()`（L159）和 `watch(...)`（L158）两行对调位置，程序行为会变吗？

**答案**：基本不变。两者都是启动异步常驻任务：`watch` 注册文件监视后立即返回，`startServer` 里 `listen` 也是异步生效。对调后服务器先监听、监视器后启动，极端情况下启动初期的一次源码变化可能晚极短时间才被监视到，实际无感。

### 4.4 startServer()：一个请求，三层去处

#### 4.4.1 概念说明

`startServer()` 是一个手写的迷你路由器。每个进入 8090 端口的 HTTP 请求，按优先级依次尝试三条路径：

1. **`/test/` 路由**：URL 匹配 `/test` 或 `/test/`（可带查询串）时，动态生成浏览器测试页面。
2. **模块服务**：`_m/` 空间的请求交给 `esmoduleserve` 的 `handleRequest`，按需编译返回。
3. **静态文件**：其余请求交给 `serve-static`，从 `demo/` 目录原样返回文件；再找不到就 404。

`handleRequest` 的返回值是分流开关：它返回真值表示「已处理」，短路后面的静态服务；返回假值才轮到 `serve-static`。

#### 4.4.2 核心流程

```text
请求 req.url
  ├─ 匹配 /^\/test\/?($|\?)/ ──► gatherTests 收集各包测试 → testHTML 生成页面 → 返回
  └─ 否则
       ├─ moduleserver.handleRequest(req, resp) 返回真 ──► 已响应（_m/ 模块），结束
       └─ 返回假 ──► serveStatic 找 demo/ 下的文件返回
                        └─ 也失败 ──► 404 "Not found"
```

#### 4.4.3 源码精读

[bin/cm.js:L125-L127](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L125-L127) —— 定下两个根基：`serve = join(root, "demo")` 是静态服务与模块服务的共同根目录；`moduleserver` 以此根创建，`maxDepth: 2` 允许模块解析走出 `demo/` 去够各包与 `node_modules`（见 4.2.3）。

[bin/cm.js:L128-L132](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L128-L132) —— 创建 `serve-static` 静态文件服务。`setHeaders` 回调对路径匹配 `try/mods/` 的响应额外加 `Access-Control-Allow-Origin: *`，允许别的站点跨域引用这些模块——推测是为 CodeMirror 的在线试用页准备的（用途待确认，行为以代码为准）。

[bin/cm.js:L133-L148](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L133-L148) —— 用 Node 内置 `http` 模块创建服务器，回调里就是三层分发：

- [bin/cm.js:L134-L142](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L134-L142) —— 第一层：正则 `/^\/test\/?($|\?)/` 匹配测试页 URL。命中时用 `@marijn/testtool` 的 `gatherTests` 从**所有包目录**收集浏览器测试（`buildPackages.map(p => p.dir)`），再用 `testHTML` 现场拼接出一页 HTML——`html` 参数里那个隐藏的 `#workspace` div（`opacity: 0`）是给需要真实 DOM 的测试用的隐形工作区。注意：这个页面**不存在于磁盘**，每次请求都重新生成。
- [bin/cm.js:L144-L147](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L144-L147) —— 第二、三层：`moduleserver.handleRequest(req, resp) || serveStatic(req, resp, callback)`。`||` 短路实现回退；`serveStatic` 的第三个参数是「最终回调」，走到它就意味着连静态文件都没找到，于是回 404 `Not found`。

[bin/cm.js:L149-L150](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L149-L150) —— `listen(8090, process.env.OPEN ? undefined : "127.0.0.1")`：固定监听 8090 端口；默认只绑定本机回环地址 `127.0.0.1`（外网不可访问，安全默认值）；设置了 `OPEN` 环境变量则 host 为 `undefined`，Node 会监听所有网络接口，供局域网/容器外访问。随后打印 `Dev server listening on 8090`。

依赖来源：[package.json:L11-L17](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L11-L17) —— `esmoduleserve`（模块服务）与 `serve-static`（静态服务）都在根 `package.json` 的 devDependencies 里，`http` 则是 Node 内置模块，零依赖可用。

#### 4.4.4 代码实践

**实践目标**：用 curl 逐一验证三层分发各自的行为。

**操作步骤**：

1. 启动 dev server 后，依次执行：
   - `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/` （静态层：index.html）
   - `curl -s http://localhost:8090/_m/demo.js | head -5` （模块层：编译产物）
   - `curl -s http://localhost:8090/test/ | head -20` （测试路由：动态 HTML）
   - `curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8090/no-such-file` （404 回退）
2. 记录每个请求命中的是哪一层。

**需要观察的现象**：四个请求分别命中静态层、模块层、`/test/` 路由、404 回退；`/test/` 返回的 HTML 里能看到测试标题「CM6 view tests」，磁盘上却搜不到这个文件。

**预期结果**：状态码依次为 200、200、200、404。待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：URL `/testfoo` 会被第一层拦截吗？`/test/mocha.js` 呢？

**答案**：都不会。正则 `/^\/test\/?($|\?)/` 要求 `/test` 之后要么直接结束、要么只有一个 `/` 再结束，要么跟 `?` 查询串。`/testfoo` 的 `test` 后面跟的是字母，`/test/mocha.js` 的 `test` 后面还有路径段，二者都不匹配，落入模块/静态层处理。

**练习 2**：为什么 `/test/` 分支不需要写 `resp.statusCode = 200`？

**答案**：它用了 `resp.writeHead(200, {"content-type": "text/html"})`，在写头部时直接指定了状态码。这是 Node `http.ServerResponse` 的另一种（较老的）写法，与 `statusCode` 赋值等价。

**练习 3**：默认只绑 `127.0.0.1` 有什么好处？什么场景需要 `OPEN=1`？

**答案**：回环地址只有本机能访问，避免开发服务器把源码服务暴露给局域网/公网（静态服务可读任意 `demo/` 下文件，暴露面越小越安全）。当服务跑在容器或远程机器里、需要从外部网络访问时，用 `OPEN=1 node bin/cm.js devserver` 让它监听所有接口。

## 5. 综合实践

把本讲三个模块串成一个完整闭环（即本讲规格指定的实践任务）：

1. **启动**：确认 `cm install` 已完成，然后在仓库根目录执行 `npm run dev`，等待输出 `Dev server listening on 8090`。
2. **访问 demo**：浏览器打开 `http://localhost:8090`，应看到一个初始内容为 `console.log("Hello world")` 的编辑器（对应 [demo/demo.ts:L5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/demo.ts#L5) 的 `doc`）。
3. **修改并观察自动重建**：把 `doc` 换成一段自己写的代码并保存，例如：

   ```ts
   doc: 'function greet(name) {\n  return `Hello, ${name}!`\n}\n',
   ```

   （此为示例代码）保存后刷新浏览器（`demo.ts` 属即时编译，刷新即可见；若同时改了某个包的 `src`，则要等终端里 `watch` 输出重建完成再刷新）。
4. **顺手在控制台**敲 `view.state.doc.toString()`，确认读到的就是你刚写入的文本（衔接 4.1 的实践）。
5. **访问测试页**：打开 `http://localhost:8090/test/`，确认能加载出动态生成的测试页面（衔接 4.4 的第一层路由）；页面具体能跑多少测试取决于各包构建状态。
6. **收尾**：Ctrl-C 停掉服务器，执行 `git diff demo/demo.ts` 查看自己第 3 步的改动，然后 `git checkout -- demo/demo.ts` 还原（不要把练习改动留在工作区，`install` 等命令对未提交改动不友好）。

## 6. 本讲小结

- `demo/demo.ts` 用 11 行完成编辑器搭建：`doc` 定初始文本、`extensions` 组合 `basicSetup` 与 `javascript()`、`parent` 指定挂载点，实例挂到 `window.view` 供控制台调试。
- `demo/index.html` 通过 `<script type=module src="_m/demo.js">` 加载一个**磁盘上不存在**的文件——`_m/` 是 `esmoduleserve` 的虚拟 URL 空间，浏览器每请求一个模块，服务器就把对应的 TS/dist 即时编译返回。
- `devserver()` 是「watch + startServer」两步：`@marijn/buildtool.watch` 监视所有包入口与 `demo.ts`、变化即重建各包 dist；`startServer()` 监听 8090 提供服务。
- `startServer()` 是三层迷你路由：`/test/` 正则命中的请求动态生成测试页，其余先给 `moduleserver.handleRequest`（用返回值短路），再回退 `serve-static` 托管的 `demo/` 静态文件，最后 404。
- 监听地址固定 8090，默认绑 `127.0.0.1`，设置 `OPEN` 环境变量才对外；`--source-map` 是 `devserver` 唯一的命令行选项。

## 7. 下一步学习建议

本讲你只是「看见」了编辑器和 dev server 的外表。接下来两条路：

- 想搞懂**这台服务器内部**的更多细节（`/test/` 页面的测试从哪来、mocha 资产如何被引用、如何改端口加路由），进入 [u2-l3 dev server 内部](u2-l3-devserver-internals.md)。
- 想先弄清**包注册表**——`buildPackages`、`p.main`、`p.dir` 这些本讲反复出现的变量到底怎么来的——进入 [u2-l1 包注册表：packages.js 与 Pkg 模型](u2-l1-package-registry.md)。

在此之前，也可以打开某个包仓库（如 `lang-javascript/`）看看它的 `src/` 与 `test/` 目录，建立「demo 引用的扩展都住在这些兄弟目录里」的直觉。
