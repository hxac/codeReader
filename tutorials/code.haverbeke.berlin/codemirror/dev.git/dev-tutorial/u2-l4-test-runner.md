# 测试体系：cm test 与浏览器/Node 双轨测试

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `cm test` 的两条测试轨道——Node 轨道（`tests`）与浏览器轨道（`browserTests`）——各自的收集方式和消费位置。
2. 逐行读懂 [bin/cm.js](../bin/cm.js) 中 `test()` 函数的参数解析：`--no-browser`、`--firefox`、`--chrome`、`--grep` 分别在哪一行被消费，默认浏览器是如何补上的。
3. 会用 `npm run test`、`npm run test-node` 以及 `--grep` 过滤来跑测试，并理解 npm 传参时 `--` 分隔符的作用。
4. 说清楚浏览器测试页面为什么是「运行时生成」的，以及它对 `demo/test/` 下两个 mocha 符号链接的依赖。
5. 理解 `test()` 如何用进程退出码把测试结果汇报给 CI。

## 2. 前置知识

本讲承接 u2-l3（dev server 内部）。你已经知道 `startServer()` 用「`/test/` 正则分支 → esmoduleserve → serve-static」的级联回退处理请求。本讲把视角从 HTTP 层转到测试本身。

### 2.1 mocha

mocha 是浏览器和 Node 通用的 JavaScript 测试框架，提供 `describe`（分组）和 `it`（单条用例）这样的组织语法。CodeMirror 各包的测试代码都用 mocha 风格书写。根 [tsconfig.json:4-6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L4-L6) 里写了 `"types": ["mocha"]`，把 mocha 的全局类型注入 TypeScript 编译环境——所以各包 `test/` 目录下的 `.ts` 测试文件可以直接写 `describe`/`it`，不需要任何 import。

### 2.2 为什么有两条轨道

CodeMirror 的包分两类：`state`、`language` 这类纯逻辑包，测试不依赖浏览器；`view`、`commands` 这类要操作 DOM 的包，测试必须在一个真实（或足够真实）的浏览器环境里跑。于是测试系统分成两条轨道：

- **Node 轨道**：直接在 Node 进程里执行，快、无浏览器依赖。
- **浏览器轨道**：把测试文件拼进一张 mocha 页面，在浏览器里执行。

把一个测试文件归入哪条轨道的判定规则在 `@marijn/testtool` 内部，本仓库源码里看不到（装好依赖后可以读 `node_modules/@marijn/testtool` 确认，待本地验证）。但从本仓库两处调用方式可以确定：`gatherTests` 返回一个含 `tests` 和 `browserTests` 两个数组的对象。

### 2.3 进程退出码

程序退出时向操作系统返回一个整数：0 表示成功，非 0 表示失败。CI（持续集成）系统就是靠退出码判断「这轮测试过没过」的。[README.md:3](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L3) 顶部的 `TESTS` 状态徽章，正来自持续运行的测试结果。

### 2.4 npm run 的 `--` 分隔符

`npm run test --grep foo` 里的 `--grep foo` 会被 npm 当成传给 npm 自己的参数吞掉；必须写成 `npm run test -- --grep foo`，npm 才把 `--` 之后的部分原样拼到脚本命令末尾。本讲的实践会反复用到这个写法。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注点 |
|---|---|---|
| [bin/cm.js:352-364](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L364) | `test()` 命令实现 | 参数解析、双轨收集、退出码 |
| [bin/cm.js:133-149](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L133-L149) | `startServer()` 的请求处理 | `/test/` 分支对 `gatherTests` 的第二处消费 |
| [package.json:4-7](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L4-L7) | npm 脚本别名 | `test` 与 `test-node` 两个入口 |
| [demo/test/mocha.js:L1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.js#L1) | 符号链接 | 指向 `../../node_modules/mocha/mocha.js`，把 mocha 纳入静态服务 |
| [demo/test/mocha.css:L1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.css#L1) | 符号链接 | 指向 `../../node_modules/mocha/mocha.css`，作用同上 |
| [tsconfig.json:4-6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L4-L6) | TypeScript 配置 | `"types": ["mocha"]` 全局注入 mocha 类型 |
| [CONTRIBUTING.md:59-63](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L59-L63) | 贡献规范 | 测试放各包 `test/` 目录、`npm run test` 验证 |

## 4. 核心概念与源码讲解

### 4.1 test()：双轨测试的总入口

#### 4.1.1 概念说明

`cm test` 是「跑全部 35 个可构建包的测试」的统一入口。它自己不执行任何测试，而是扮演**编排层**：调用 `@marijn/testtool` 提供的三个能力——

1. `gatherTests(目录列表)`：扫描各包目录，把测试文件分成 `tests`（Node 轨道）和 `browserTests`（浏览器轨道）两个数组；
2. `runTests({tests, browserTests, browsers, grep})`：执行两条轨道，返回一个 Promise，resolve 值是「是否有失败」的布尔值；
3. （浏览器页面的生成 `testHTML` 由 `startServer()` 消费，见 4.2。）

注意 `test()` 的收集范围是 `buildPackages`——即入口 `main` 非空的 35 个包（承接 u2-l1：全量 36 包中唯一的非 TS 包 `legacy-modes` 被排除在外）。

#### 4.1.2 核心流程

```text
node bin/cm.js test [--no-browser] [--firefox] [--grep <pattern>]
        │
        ▼
start() 查映射表分发（cm.js:30），rest 参数使 test.length = 0，
参数个数校验恒通过（cm.js:34）
        │
        ▼
test(...args)（cm.js:352）
        │
        ├─ 惰性 require("@marijn/testtool")（353）
        ├─ gatherTests(35 个包目录) → { tests, browserTests }（354）
        ├─ for 循环逐个识别参数（356-361）
        │     --firefox → browsers.push("firefox")
        │     --chrome  → 期望 push("chrome")（注意：此处有笔误，见练习）
        │     --no-browser → noBrowser = true
        │     --grep <p>   → grep = args[++i]（吞掉下一个参数）
        ├─ 未指定浏览器且未禁用浏览器 → 默认补 "chrome"（362）
        └─ runTests({tests, browserTests, browsers, grep})（363）
              │  Promise<failed: boolean>
              ▼
        process.exit(failed ? 1 : 0)（363）→ 退出码交给 CI 判定
```

#### 4.1.3 源码精读

先看函数全文（这是 [bin/cm.js:352-364](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L364) 的全部内容，共 13 行）：

```js
function test(...args) {
  let runTests = require("@marijn/testtool")
  let {tests, browserTests} = runTests.gatherTests(buildPackages.map(p => p.dir))
  let browsers = [], grep, noBrowser = false
  for (let i = 0; i < args.length; i++) {
    if (args[i] == "--firefox") browsers.push("firefox")
    if (args[i] == "--chrome") browser.push("chrome")
    if (args[i] == "--no-browser") noBrowser = true
    if (args[i] == "--grep") grep = args[++i]
  }
  if (!browsers.length && !noBrowser) browsers.push("chrome")
  runTests.runTests({tests, browserTests, browsers, grep}).then(failed => process.exit(failed ? 1 : 0))
}
```

逐段拆解：

- **[bin/cm.js:353-354](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L353-L354)**：在函数体内才 `require("@marijn/testtool")`，随后立刻用 `buildPackages.map(p => p.dir)` 把 35 个包目录交给 `gatherTests`，一次性解构出两条轨道的测试文件数组。这个惰性 require 延续了文件顶部 [bin/cm.js:3-4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L4) 注释的约定：顶层只准用 Node 内置模块。
- **[bin/cm.js:355-361](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L355-L361)**：手写 for 循环解析参数，三个状态变量 `browsers`（数组）、`grep`、`noBrowser`（布尔）。四个 `if` 是并列关系而非 `else if`，且不认识的参数会被**静默忽略**——`cm test foo` 不会报错，等同默认行为。`--grep` 用 `args[++i]` 先自增再取值，配合循环本身的 `i++` 恰好跳过被消耗的模式串。
- **[bin/cm.js:358](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L358)**：这里写的是 `browser.push("chrome")`——单数。但整个文件声明的变量只有 `browsers`（复数），`browser` 并不存在，显式传 `--chrome` 会抛 `ReferenceError`，被 `start()` 的 [bin/cm.js:35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35) 捕获后走 `error()` 以退出码 1 结束。平时几乎没人踩到它，因为不传 `--chrome` 时默认逻辑本来就会补上 chrome（见下一行）。这是精读源码才能发现的真实细节。
- **[bin/cm.js:362](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L362)**：默认值规则——「既没选浏览器、也没禁用浏览器」才补 `chrome`。所以 `--no-browser` 的作用就是让 `browsers` 保持空数组，`runTests` 只跑 Node 轨道。
- **[bin/cm.js:363](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L363)**：`runTests` 的返回值是 Promise，resolve 出 `failed` 布尔，直接映射成进程退出码 0 或 1。CI 依赖这一行判定成败。

外围两处也值得对照：

- **[bin/cm.js:30](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L30)**：映射表里的 `test` 项（简写属性，指向下方函数声明）。由于 `test(...args)` 用 rest 参数，`test.length` 为 0，[bin/cm.js:34](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34) 的 `cmdFn.length > args.length` 恒为假——`cm test` 接受任意个数的参数。
- **[bin/cm.js:53](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L53)**：帮助文本只写了 `cm test [--no-browser]`。`--firefox`、`--chrome`、`--grep` 三个参数有实现无文档——再次印证 u1-l3 的结论：**映射表与实现才是权威清单，help 文本靠手工同步**。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 Node 轨道，再用 `--grep` 过滤，最后把每个参数的消费行号对上源码。

**操作步骤**（前提：已按 u1-l2 完成 `node bin/cm.js install`）：

1. 在仓库根目录运行：

   ```bash
   npm run test-node
   ```

   它展开就是 `node bin/cm.js test --no-browser`（见 4.3），即只跑 Node 轨道。

2. 观察输出的整体结构：按包分组的用例列表、每个用例的通过状态、末尾的汇总统计（具体格式由 testtool 决定，待本地验证）。

3. 加上 `--grep` 只跑名称匹配的用例（注意 `--` 分隔符）：

   ```bash
   npm run test-node -- --grep "selection"
   ```

   匹配不到任何用例时输出会明显变短，可借此确认过滤生效（mocha 的 grep 语义是按用例标题的正则匹配，具体行为待本地验证）。

4. 填写下面这张「参数消费对照表」，把左列的每个输入与 [bin/cm.js:352-364](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L364) 中的行号一一对应：

   | 输入 | 消费行 | 效果 |
   |---|---|---|
   | `--no-browser` | 359 | `noBrowser = true`，362 行不再补默认浏览器 |
   | `--firefox` | 357 | `browsers.push("firefox")` |
   | `--chrome` | 358 | 预期 push `"chrome"`；实际引用未定义变量（见 4.1.5 练习 1） |
   | `--grep <pattern>` | 360 | `grep = args[++i]`，吞掉下一个参数 |
   | 不传任何浏览器参数 | 362 | 默认 `browsers = ["chrome"]` |
   | 包目录列表 | 354 | `buildPackages.map(p => p.dir)` 作为 `gatherTests` 输入 |
   | 测试结果 | 363 | `failed` 布尔映射为退出码 0/1 |

5. 用退出码验证：`npm run test-node; echo "exit=$?"`，观察 `echo` 打出的值（成功应为 0，待本地验证）。

**需要观察的现象**：`--grep` 前后用例数量的变化；退出码与「有无失败」的对应关系。

**预期结果**：`--no-browser` 时全程无浏览器启动、速度快；`--grep` 只保留标题匹配的用例；退出码符合 0/1 语义。

#### 4.1.5 小练习与答案

**练习 1**：显式运行 `node bin/cm.js test --chrome` 会发生什么？为什么日常没人发现这个问题？

**答案**：[bin/cm.js:358](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L358) 引用了未声明的变量 `browser`（声明的是复数 `browsers`），抛出 `ReferenceError: browser is not defined`，该异常被 [bin/cm.js:35](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L35) 的 `.catch(e => error(e))` 捕获，打印错误并以退出码 1 结束（具体报错文案待本地验证）。日常没人踩到，是因为省略 `--chrome` 时 [362 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L362)的默认逻辑本来就会补上 chrome，显式传它的人极少。

**练习 2**：如果 `--grep` 是命令的最后一个参数（后面没有模式串），会发生什么？

**答案**：[360 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L360)的 `args[++i]` 越界取到 `undefined`，`grep` 为 `undefined` 并原样传给 `runTests`——读未初始化的数组元素不会抛错，效果等同于「没提供过滤条件」（具体是否完全等同不过滤，取决于 testtool 对 `undefined` 的处理，待本地验证）。

**练习 3**：为什么 `cm test` 后面接再多的参数也不会被 `start()` 的参数个数校验拦下？

**答案**：`test` 声明为 `function test(...args)`，rest 参数不计入函数的 `length` 属性，所以 `test.length === 0`；[bin/cm.js:34](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L34) 的判断 `cmdFn.length > args.length` 即 `0 > n`，对任意 `n` 恒为假。

### 4.2 浏览器轨道：startServer() 中的 gatherTests 调用与 mocha 资产

#### 4.2.1 概念说明

`gatherTests` 在本仓库有**两个消费者**：4.1 的 `test()`（自动跑）和本节的 `startServer()` `/test/` 分支（给人看）。后者在 u2-l3 已从 HTTP 路由角度讲过，本节换个视角：把它理解为浏览器轨道的「人工入口」——开发者访问 `http://localhost:8090/test/`，服务器**在请求到达的那一刻**收集全部浏览器测试、现场拼出一张 mocha 页面返回。

这张页面对 `demo/test/` 下两个符号链接有硬依赖：页面要加载 mocha 框架本身（`describe`/`it` 的实现）和它的样式表，而 dev server 的静态服务根目录是 `demo/`，mocha 装在 `node_modules` 里、本不在服务范围内。解决办法就是把 `node_modules/mocha` 里的两个文件以符号链接的形式「搬」进 `demo/test/`。

#### 4.2.2 核心流程

```text
浏览器请求 GET /test/
        │
        ▼
[bin/cm.js:134] URL 命中正则 /^\/test\/?($|\?)/
        │
        ▼
[135] 惰性 require @marijn/testtool
[136] gatherTests(35 个包目录) → 只解构 browserTests（丢弃 tests）
[138] browserTests.map(f => path.relative(serve, f))
      把绝对路径转成相对 demo/ 根的路径
[138-142] testHTML(路径列表, {html: 额外 HTML}) → 生成完整 mocha 页面
[137] 以 content-type: text/html 写回
        │
        ▼
浏览器解析页面，加载 mocha 资产：
  GET /test/mocha.js  → serve-static 在 demo/test/ 命中符号链接
                         → 读到 node_modules/mocha/mocha.js
  GET /test/mocha.css → 同上
```

#### 4.2.3 源码精读

- **[bin/cm.js:126-127](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L126-L127)**：`serve = join(root, "demo")` 是静态服务的根，`esmoduleserve` 实例也以它为根——这决定了 URL 空间与磁盘目录的对应关系：`/test/mocha.js` ↔ `demo/test/mocha.js`。
- **[bin/cm.js:133-142](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L133-L142)**：`/test/` 分支。注意 [136 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L136)只解构 `browserTests`——测试页只关心浏览器轨道；与 `test()` 不同，这里**每次请求都重新收集**，因此新增、删除测试文件后刷新页面即可生效，无需重启服务器。[138-142 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L138-L142)把 `path.relative(serve, f)` 得到的相对路径交给 `testHTML`，并通过 `html` 选项注入一个隐藏的 `#workspace` 容器（`opacity: 0; position: fixed`）——编辑器类测试需要一个真实 DOM 挂载点，但又不能让几十个测试编辑器铺满屏幕，所以藏起来。
- **[demo/test/mocha.js:L1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.js#L1)** 与 **[demo/test/mocha.css:L1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/demo/test/mocha.css#L1)**：两个符号链接，内容分别是目标路径 `../../node_modules/mocha/mocha.js` 和 `../../node_modules/mocha/mocha.css`。它们是 git 跟踪的文件——[.gitignore:1-5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/.gitignore#L1-L5) 只忽略了 `/node_modules` 和 `/demo/test/test.js*`（demo 自己的编译产物），没有忽略这两个链接。serve-static 解析 URL 时会跟随符号链接读到 `node_modules` 里的真身。测试页生成的 HTML 具体以什么标签引用这些资产，由 `testHTML` 内部决定（待本地验证），但可以确定的是：**不经这两个链接，mocha 就无法出现在静态服务的 URL 空间里**。
- **[bin/cm.js:144-148](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L144-L148)**：`/test/` 之外的其他请求走「模块服务 → 静态文件 → 404」级联（u2-l3 详述过，此处不重复）。`/test/mocha.js` 正是从这条级联的 `serveStatic` 一层拿到文件的。
- **[tsconfig.json:4-6](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/tsconfig.json#L4-L6)**：`"types": ["mocha"]` 与 `"typeRoots": ["./node_modules/@types"]` 配合，为所有包（含测试文件）注入 mocha 全局类型。这是「测试文件免 import 直接用 describe/it」的编译期保障，与 4.2 的运行期（页面加载 mocha）正好构成一体两面。

还要区分一件事：`cm test` 的浏览器轨道（`runTests` 自动驱动浏览器）**并不经过** `startServer()`——`test()` 里没有启动任何 HTTP 服务器，它直接把 `browserTests` 交给 testtool。自动驱动时测试页如何生成、浏览器如何拉起，都在 `@marijn/testtool` 内部（装好依赖后可阅读其源码确认，待本地验证）。本仓库里可见的两个消费者是各自独立的。

#### 4.2.4 代码实践

**实践目标**：亲眼确认「测试页是运行时生成的、mocha 资产是经符号链接服务的」。

**操作步骤**：

1. 启动 dev server（承接 u2-l3）：

   ```bash
   npm run dev
   ```

2. 浏览器打开 `http://localhost:8090/test/`，观察 mocha 页面：标题「CM6 view tests」、用例分组、页面视觉上没有大面积编辑器（它们挂在隐藏的 `#workspace` 里）。

3. 在另一个终端用 curl 看生成的 HTML（输出较长可接 `| head -40`）：

   ```bash
   curl -s http://localhost:8090/test/ | head -40
   ```

4. 再验证 mocha 资产确实可经静态服务取到：

   ```bash
   curl -sI http://localhost:8090/test/mocha.js | head -5
   curl -sI http://localhost:8090/test/mocha.css | head -5
   ```

5. 对照磁盘：`ls -l demo/test/` 应看到两个指向 `../../node_modules/mocha/` 的符号链接。

**需要观察的现象**：`/test/` 返回的是 HTML（`content-type: text/html`）；`/test/mocha.js`、`/test/mocha.css` 返回 200；`demo/` 目录下**不存在**与测试页对应的 HTML 文件。

**预期结果**：测试页无静态文件对应、每次请求现场生成；mocha 的两个文件能被 200 命中，且磁盘上的入口只是符号链接。第 3、4 步的具体响应头与 HTML 内容待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `/test/` 分支每次请求都重新调用 `gatherTests`，而不是在服务器启动时收集一次缓存起来？

**答案**：开发过程中的常见操作是新增、删除、重命名测试文件。每次请求重新收集（[136 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L136)）让这些变化刷新页面即可见，无需重启服务器；目录扫描的成本对开发场景可以忽略。这是「开发服务器」与「生产服务」在缓存策略上的典型差异。

**练习 2**：如果删掉 `demo/test/mocha.js` 这个符号链接，`/test/` 页面会怎样？

**答案**：页面本身仍能生成（HTML 的生成不依赖链接），但页面加载 mocha 脚本的请求会落到 [144-147 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L144-L147)的级联回退终点，得到 404「Not found」；`describe`/`it` 无定义，用例一条也跑不起来（具体报错形态待本地验证）。

**练习 3**：`tsconfig.json` 里的 `"types": ["mocha"]` 起什么作用？为什么数组里只有 mocha 一项？

**答案**：`types` 显式列出要注入的全局类型包，省去在各测试文件里 `import` mocha；同时 `types` 一旦显式给出，TypeScript 就不再自动包含 `typeRoots` 下所有 `@types` 包，只有列出的 mocha 会进入全局环境——这避免了无关全局类型的意外污染（承接 u2-l2：tsconfig 面向编译期）。

### 4.3 package.json 的 test 与 test-node 脚本：npm 侧入口

#### 4.3.1 概念说明

根 [package.json](../package.json) 的 `scripts` 是 npm 世界的入口别名层：`cm test` 是实现，`npm run test` 是给习惯 npm 的开发者和 CI 系统准备的门面。四个脚本里有两个属于本讲：

```json
"test": "node bin/cm.js test",
"test-node": "node bin/cm.js test --no-browser"
```

可以看出：**`test-node` 不是独立功能，只是 `cm test --no-browser` 的别名**——Node 轨道就是「禁用浏览器后的完整测试」。`prepare` 与 `dev` 两个脚本已在 u2-l2、u1-l4 讲过，不再重复。

#### 4.3.2 核心流程

```text
开发者 / CI 输入                 实际执行
──────────────────────────    ──────────────────────────────────
npm test                       node bin/cm.js test          （双轨，默认 chrome）
npm run test                   同上
npm run test-node              node bin/cm.js test --no-browser（仅 Node 轨道）
npm run test -- --grep foo     node bin/cm.js test --grep foo
npm run test-node -- --grep foo
                               node bin/cm.js test --no-browser --grep foo
```

npm 的规则：`npm run <script>` 后跟 `--`，其后的参数原样追加到脚本命令末尾；不加 `--` 的参数会被 npm 当作自己的选项处理而不传递。

#### 4.3.3 源码精读

- **[package.json:4](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L4)**：`"test": "node bin/cm.js test"`，无任何附加参数——于是 [362 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L362)的默认逻辑生效，跑双轨 + chrome。
- **[package.json:5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L5)**：`"test-node": "node bin/cm.js test --no-browser"`，把「仅 Node」固化为脚本名，等价于 `cm test` 的单参数子集。
- **[CONTRIBUTING.md:59-63](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L59-L63)**：贡献规范要求测试放在各包的 `test/` 目录（放进已有的 `test-*.js` 文件或新建文件），提交前用 `npm run test` 验证——即贡献流程的官方入口就是这两个脚本。
- **[README.md:21](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L21)**：README 在介绍 `npm run dev` 时顺带给出了浏览器测试的地址 `http://localhost:8090/test/`——即 4.2 的人工入口；[README.md:3](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L3) 的 `TESTS` 徽章则显示持续运行的测试状态，其成败判定的终点就是 [bin/cm.js:363](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L363) 的退出码。

#### 4.3.4 代码实践

**实践目标**：体会「别名层 + `--` 传参」的组合拳。

**操作步骤**：

1. 对比下面两条命令的展开结果（可先用 `--grep` 限定一个小范围减少耗时）：

   ```bash
   npm run test-node -- --grep "state"
   npm test -- --no-browser --grep "state"
   ```

2. 用 `npm run` 的 dry-run 查看脚本原样定义（不执行）：

   ```bash
   npm run --if-present test-node 2>&1 | head -3 || true
   ```

   （若你的 npm 版本支持 `npm run` 列表输出，也可直接观察；不同 npm 版本输出形式不同，待本地验证。）

3. 故意漏掉 `--` 再跑一次 `npm run test-node --grep "state"`，观察 npm 的报错或参数被吞的现象（具体表现随 npm 版本而异，待本地验证）。

**需要观察的现象**：步骤 1 两条命令应产生相同的测试输出——因为它们最终展开成同一个 `node bin/cm.js test ...` 命令行；步骤 3 中过滤条件未生效或 npm 报「未知选项」。

**预期结果**：确认 `test-node` 与 `test --no-browser` 完全等价、`--` 是追加参数的正确姿势。

#### 4.3.5 小练习与答案

**练习 1**：`npm run test -- --grep foo` 中间的 `--` 起什么作用？去掉会怎样？

**答案**：`--` 告诉 npm「之后的参数不是给你的，原样拼到脚本命令末尾」。去掉后 `--grep foo` 被 npm 当作传给自己的选项处理，不会到达 `cm test`，过滤不生效（或直接被 npm 报为未知选项，视版本而定）。

**练习 2**：`test-node` 脚本里已经写死了 `--no-browser`，想再加 `--grep` 过滤该怎么写？

**答案**：`npm run test-node -- --grep foo`。npm 先把脚本原文 `node bin/cm.js test --no-browser` 取出，再把 `--` 之后的参数追加到末尾，最终执行 `node bin/cm.js test --no-browser --grep foo`——多个参数在 [356-361 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L356-L361)的循环里被逐个识别、互不冲突。

**练习 3**：为什么 `test()` 必须把测试结果映射成进程退出码，而不能只打印一份汇总？

**答案**：`npm run test` 的调用方往往是 CI 或 shell 脚本，它们判断成败的通用协议是退出码（0 成功、非 0 失败），不会去解析人类可读的输出。[bin/cm.js:363](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L363) 的 `process.exit(failed ? 1 : 0)` 就是把布尔结果翻译成这个协议，README 顶部的测试状态徽章才能自动反映每轮结果。

## 5. 综合实践

**任务：双轨测试全链路观察报告。** 把本讲三条线串起来，产出一份自己的观察记录（文本文件即可，放在仓库外或 `dev-tutorial/` 之外的个人笔记目录，不要改动仓库）。

1. **准备**：确认 `node bin/cm.js packages` 能列出全部包、`cm install` 已完成（承接 u1-l2）。
2. **Node 轨道**：`npm run test-node` 全量跑一次，记录输出的分组方式（按包？按文件？）与汇总行；再用 `--grep` 缩小范围复跑一次，记录用例数变化。
3. **浏览器轨道（人工）**：`npm run dev` 起服务器，访问 `http://localhost:8090/test/`，`curl -s` 取回页面源码，找出：页面 `<title>`（应来自 [138-141 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L138-L141)注入的 HTML）、mocha 资产的引用 URL、隐藏 `#workspace` 容器。
4. **浏览器轨道（自动）**：`npm test` 跑一次默认 chrome 的双轨，对比它与步骤 2、3 的输出覆盖面。
5. **源码定位**：在报告里填写 4.1.4 的参数消费对照表，并为每一行附上 [bin/cm.js](../bin/cm.js) 的永久链接。
6. **思考延伸**：写出修复 [bin/cm.js:358](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L358) 笔误的最小 patch（一行：`browser` → `browsers`），并在自己的本地克隆上验证 `node bin/cm.js test --chrome` 修复前后行为差异。仅作练习，是否向项目提交请先阅读 [CONTRIBUTING.md](../CONTRIBUTING.md)（注意项目对贡献的既定规范）。

**验收标准**：报告能回答——两条轨道分别从哪里收集、分别在哪里被执行、结果如何变成退出码、mocha 资产如何进入 URL 空间。

## 6. 本讲小结

- `cm test` 是纯编排层：`gatherTests` 把 35 个可构建包的测试分成 `tests`（Node 轨道）与 `browserTests`（浏览器轨道），`runTests` 执行并把「是否有失败」的布尔映射为进程退出码（[bin/cm.js:352-364](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L352-L364)）。
- 参数解析是手写 for 循环：`--firefox`/`--chrome`/`--no-browser`/`--grep` 分别在 357-360 行被消费，未指定浏览器时默认补 chrome（362 行）；帮助文本只记录了 `--no-browser`，实现才是权威。
- [358 行](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L358)存在 `browser`/`browsers` 笔误：显式传 `--chrome` 会抛 ReferenceError，但因默认路径不经过该分支而长期无人察觉。
- `gatherTests` 有两个消费者：`test()`（自动跑双轨）与 `startServer()` 的 `/test/` 分支（每次请求重新收集、用 `testHTML` 现场生成 mocha 页面，仅供 Node 轨道之外的浏览器测试人工查看）。
- 浏览器测试页依赖 `demo/test/` 下两个 git 跟踪的符号链接把 `node_modules/mocha` 纳入静态服务的 URL 空间；`tsconfig` 的 `"types": ["mocha"]` 则从编译期免掉测试文件的 mocha import。
- `npm run test` 与 `npm run test-node` 只是 `cm test`（后者附 `--no-browser`）的别名；给它们追加参数必须用 `--` 分隔。

## 7. 下一步学习建议

- **下一讲 u2-l5** 将讲 `status`/`commit`/`push`/`run`/`grep` 这组多仓库工作流命令——其中 `cm grep` 是你在 35 个包源码里定位测试符号的好帮手（例如 `cm grep "browserTests"`）。
- 挑一个包（推荐 `state` 或 `view`）阅读其 `test/` 目录，对照 [CONTRIBUTING.md:59-63](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/CONTRIBUTING.md#L59-L63) 的规范，观察真实测试文件如何组织，并试着判断它属于哪条轨道。
- 装好依赖后阅读 `node_modules/@marijn/testtool` 的源码，验证本讲标注「待本地验证」的几点：轨道判定规则、`runTests` 如何驱动浏览器、`testHTML` 生成的页面如何引用 mocha 资产。
