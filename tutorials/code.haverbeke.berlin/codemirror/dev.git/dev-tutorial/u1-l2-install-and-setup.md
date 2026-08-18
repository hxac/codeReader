# 从零跑起来：cm install 与多仓库装配

## 1. 本讲目标

学完本讲，你应该能够：

1. 逐行讲出 [bin/cm.js](../../bin/cm.js) 中 `install()` 的三阶段流程：逐包「克隆 / 更新 / 清理」→ `npm install` 装配依赖 → 重新加载包注册表并触发首次构建。
2. 解释 `start()` 入口里那条 `assertInstalled()` 守卫为什么把 `install` 和 `--help` 排除在外，以及文件顶部「不要提前 require node_modules」注释背后的模块加载时序问题。
3. 读懂 `run()` 这个 20 行不到的子进程封装：`execFileSync`、`cwd`、`stdio` 三元组、`stdout: "pipe"` 与 `"inherit"` 的区别。
4. 在一台新机器上用 `node bin/cm.js install [--ssh]` 把整个 CodeMirror 开发环境搭起来，并知道第二次运行它时会发生什么、有什么风险。

## 2. 前置知识

本讲不需要你已经读过任何 CodeMirror 包的源码，但需要下面几个基础概念。已经熟悉的读者可以快速扫一遍黑体词就进入第 3 节。

- **子进程（child process）**：一个程序启动另一个程序。Node.js 内置的 `child_process` 模块提供 `execFileSync(cmd, args, options)`——同步地执行 `cmd`，把 `args` 作为命令行参数传给它，**阻塞**直到子进程退出，然后返回其标准输出字符串。参数是逐个传递的数组，中间不经过任何 shell。
- **`process.argv`**：Node 进程的命令行参数数组。`argv[0]` 是 node 可执行文件，`argv[1]` 是脚本路径，`argv[2]` 开始才是用户敲的子命令（如 `install`），之后是其余参数。
- **`fs.existsSync(path)`**：同步判断文件或目录是否存在，返回布尔值，不存在时不抛错。
- **git 三板斧**：`git clone <url> <dir>` 把远端仓库克隆到目录；`git fetch origin main` 只下载远端 main 分支的最新提交（不改动工作区）；`git reset --hard FETCH_HEAD` 把当前分支和工作区**强制**对齐到刚 fetch 下来的提交——注意这会丢弃本地未提交的修改。
- **npm workspaces**：在根 `package.json` 里写 `"workspaces": ["*"]` 后，根目录下每个含 `package.json` 的子目录都会被 npm 视为一个「工作区包」：它们互相引用时不需要真的发布到 npm，依赖也会被统一提升（hoist）到根目录的 `node_modules`。上一讲已经提过本仓库用这个机制把克隆出来的三十多个包目录拼成一个逻辑 monorepo，本讲我们会看到 `install()` 是如何一步步把材料准备到位的。
- **npm 生命周期脚本与 `--ignore-scripts`**：npm 在安装各包时默认会执行它们 `package.json` 里声明的脚本（如 `prepare`）。`--ignore-scripts` 让 npm 跳过所有这些脚本，只装依赖。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [bin/cm.js](../../bin/cm.js) | 全仓库唯一的命令行入口，所有 `cm` 子命令的实现 | `start()` 入口、`assertInstalled()` 守卫、`run()` 子进程封装、`install()` 主流程 |
| [bin/packages.js](../../bin/packages.js) | 包注册表：36 个包的名单（`core` + `nonCore`）与 `Pkg` 模型 | `install()` 遍历的 `packages` 数组从哪来；`Pkg` 在包目录不存在时的行为 |
| [package.json](../../package.json) | 根包描述 | `workspaces: ["*"]` 如何让 `npm install` 完成「装配」 |
| [README.md](../../README.md) | 官方说明 | 安装、重建、dev 三段官方口径 |

提醒：本仓库刚克隆下来时只有上面这几个文件和 `demo/`、`bin/` 两个目录，**编辑器源码一个字都不在这里**——它们要等 `cm install` 把 36 个包仓库克隆进根目录后才出现。这也是本讲存在的意义。

## 4. 核心概念与源码讲解

### 4.1 入口与守卫：start()、assertInstalled() 与文件顶部的注释

#### 4.1.1 概念说明

`bin/cm.js` 是整个开发环境的驾驶舱：`packages`、`status`、`build`、`test`、`release`……所有命令都从这里分发。但它面临一个「先有鸡还是先有蛋」的问题——它自己依赖的工具（打包器、测试器）装在 `node_modules` 里，而 `node_modules` 要靠 `install` 命令创建。解决方案是两层设计：

1. 文件顶部的 require 只用 Node 内置模块和本地文件，保证脚本在 `node_modules` 不存在时也能加载执行；
2. 入口处用 `assertInstalled()` 做**快速失败守卫**：除 `install` 和 `--help` 外，任何命令都先检查 36 个包目录是否已经克隆到位，缺了就直接报错退出，而不是让命令在半路以莫名其妙的方式失败。

#### 4.1.2 核心流程

`start()` 的执行过程可以概括为：

```text
读取 argv[2] 作为子命令 command
  ├─ command 不是 "install" 也不是 "--help"
  │    └─ assertInstalled()：逐个检查 36 个包目录是否存在
  │         └─ 任一缺失 → 打印提示到 stderr → process.exit(1)
  ├─ 取 argv[3..] 作为参数 args
  ├─ 在「命令名 → 函数」映射表里查 cmdFn
  │    └─ 查不到，或函数必填参数个数 > 实参数个数 → help(1) 打印用法并退出
  └─ new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))
       └─ 把同步/异步抛出的任何错误统一交给 error() 打印并退出
```

#### 4.1.3 源码精读

先看文件开头三行，这是理解整个装配流程的钥匙：

> [bin/cm.js:L3-L5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L3-L5)
>
> ```js
> // NOTE: Don't require anything from node_modules here, since the
> // install script has to be able to run _before_ that exists.
> const child = require("child_process"), fs = require("fs"), path = require("path"), {join} = path
> ```

这段注释直说了约束：**install 必须在 `node_modules` 存在之前就能运行**，所以顶层 require 只允许 `child_process`、`fs`、`path` 这类内置模块。配合下一行看：

> [bin/cm.js:L9-L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L9-L11)
>
> ```js
> const {loadPackages, nonCore} = require("./packages")
>
> let {packages, packageNames, buildPackages} = loadPackages()
> ```

顶层还 require 了本地文件 `./packages`——它是安全的，因为 [bin/packages.js:L1](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L1) 自身也只 require `fs` 和 `path`。而所有真正来自 `node_modules` 的依赖（`@marijn/buildtool`、`esmoduleserve`、`@marijn/testtool` 等）全部藏在函数体内**惰性加载**，例如 `build()` 里的 [bin/cm.js:L121](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L121)——它们只在 `install` 完成之后才有可能被调用到。

接下来是入口函数：

> [bin/cm.js:L13-L36](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L13-L36)
>
> ```js
> function start() {
>   let command = process.argv[2]
>   if (command && !["install", "--help"].includes(command)) assertInstalled()
>   let args = process.argv.slice(3)
>   let cmdFn = {
>     packages: listPackages,
>     status,
>     build,
>     // …（其余命令省略）…
>     run: runCmd,
>     "--help": () => help(0)
>   }[command]
>   if (!cmdFn || cmdFn.length > args.length) help(1)
>   new Promise(r => r(cmdFn.apply(null, args))).catch(e => error(e))
> }
```

几个关键点：

- **第 15 行的守卫**：白名单 `["install", "--help"]` 之外的任何命令都要先过 `assertInstalled()`。`install` 免检是显然的——它就是用来创造安装的；`--help` 免检则保证任何人在任何状态下都能查看用法。
- **`cmdFn.length` 校验**（第 34 行）：`Function.prototype.length` 返回函数**必填**参数的个数，带默认值的参数不计入。所以 `install(arg = null)` 的 `length` 是 0，`cm install` 一个参数都不带也不会被 `help(1)` 拦下。这个技巧的完整展开留给下一讲 u1-l3。
- **统一错误出口**（第 35 行）：命令函数在 `new Promise(r => r(cmdFn.apply(null, args)))` 的执行器里被调用，执行器里抛出的同步异常会让 Promise 直接变为 rejected，于是无论命令是同步抛错还是返回被 reject 的 Promise，都会落到 `error()`（[bin/cm.js:L59-L62](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L59-L62)）打印并以退出码 1 结束。

守卫本身只有 8 行：

> [bin/cm.js:L72-L79](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L72-L79)
>
> ```js
> function assertInstalled() {
>   for (let p of packages) {
>     if (!fs.existsSync(p.dir)) {
>       console.error(`module ${p.name} is missing. Did you forget to run 'cm install'?`)
>       process.exit(1)
>     }
>   }
> }
> ```

它只检查 `p.dir`（即仓库根目录下与包同名的目录）是否存在，**不检查** `node_modules`。`packages` 的顺序来自 `packages.js` 里 `core` 在前、`nonCore` 在后的拼接（[bin/packages.js:L44](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L44)），而 `core` 的第一项是 `state`（[bin/packages.js:L3-L5](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L5)），所以全新克隆上第一个被点名的缺失包几乎总是 `state`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到守卫的「快速失败」，并确认哪些命令不受它影响。
2. **操作步骤**：
   - 在一个**尚未运行过 install** 的全新克隆里（本仓库的初始状态就是如此：根目录只有 `bin/`、`demo/` 和几个配置文件）执行 `node bin/cm.js status`。
   - 再执行 `node bin/cm.js packages` 和 `node bin/cm.js --help`。
3. **需要观察的现象**：第一条命令立刻报错；后两条正常输出。
4. **预期结果**（依据源码推导，待本地验证）：
   - `cm status` 在 stderr 打印 `module state is missing. Did you forget to run 'cm install'?` 并以退出码 1 退出；
   - `cm packages` 逐行打印 36 个包名——因为它走的是 `packages` 命令？不，`packages` **同样要过守卫**。请先自己想一想再往下读。
   
   答案：`packages` 也会触发 `assertInstalled()`，在未安装环境下同样报错退出。能正常工作的只有 `install` 和 `--help` 两条。这是本实践真正要让你踩到的认知点：**除这两条外，一切命令都以「先安装」为前提**。而 `cm packages` 的正常输出（36 行包名，从 `state` 到 `theme-one-dark`）要等 install 之后才能看到。
5. 本讲撰写环境与读者一样处于未安装状态，以上行为均为源码推导，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 15 行的白名单改成只剩 `["--help"]`（即 `install` 也要过守卫），会发生什么？

**答案**：死锁。`install` 是唯一能创建那 36 个包目录的命令，而守卫要求这些目录已存在才放行，于是谁也运行不了 `install`，环境永远无法建立。这就是「安装命令必须绕过安装检查」的普遍原则。

**练习 2**：`assertInstalled()` 为什么不顺便检查 `node_modules` 是否存在？

**答案**：这是「快速失败」与「职责分离」的取舍。包目录缺失是最常见、最容易被误解的错误形态（用户忘了 install），值得一条专属提示语；而 `node_modules` 缺失属于另一类故障，会在命令真正 require 工具时以 `MODULE_NOT_FOUND` 抛出，并被 `start()` 末尾的 `.catch(e => error(e))` 统一接住。守卫只覆盖最高频的坑，不做穷举。

**练习 3**：`install(arg = null)` 的 `Function#length` 是多少？`cm install a b c` 三个参数能通过第 34 行的个数校验吗？

**答案**：是 0（带默认值的参数不计入）。`0 > 3` 为假，能通过校验；但 `install()` 内部第 83 行会检查第一个参数是否为 `--ssh`，`a` 不是，于是 `help(1)` 退出。第二、三个参数根本没机会被消费。

### 4.2 run()：贯穿全文件的子进程封装

#### 4.2.1 概念说明

`cm.js` 里几乎所有真实动作（git、npm、rm、grep……）都不是用 Node 库实现的，而是直接调用外部命令行工具。`run()` 就是这些调用的统一封装：指定工作目录、决定输出去向（收集成字符串还是直接打到终端）、处理 Windows 差异。它只有三行，但 `install()` 的每一步都要经过它，读懂它才能读懂 install 的输出为什么长那样。

#### 4.2.2 核心流程

```text
run(cmd, args, wd = root, {shell = false, stdout = "pipe"} = {})
  └─ child.execFileSync(cmd, args, {
       shell,                     // 默认 false：不经过 shell，args 按字面量传递
       cwd: wd,                   // 子进程工作目录，默认是仓库根目录
       encoding: "utf8",          // 返回值是字符串而非 Buffer
       stdio: ["ignore", stdout, process.stderr]
     })                           // stdin 忽略；stdout 可配置；stderr 直通终端
  └─ 返回子进程的 stdout：
       stdout:"pipe"（默认）→ 收集成字符串并返回
       stdout:"inherit"       → 直接打到当前终端，返回值为空字符串
```

#### 4.2.3 源码精读

> [bin/cm.js:L64-L66](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L64-L66)
>
> ```js
> function run(cmd, args, wd = root, {shell = false, stdout = "pipe"} = {}) {
>   return child.execFileSync(cmd, args, {shell, cwd: wd, encoding: "utf8", stdio: ["ignore", stdout, process.stderr]})
> }
> ```

看 `install()` 里的两个典型调用点就能体会两种模式的分工：

> [bin/cm.js:L88-L89](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L88-L89)
>
> ```js
>       run("git", ["fetch", "origin", "main"], pkg.dir, {stdout: "inherit"})
>       run("git", ["reset", "--hard", "FETCH_HEAD"], pkg.dir, {stdout: "inherit"})
> ```

`git fetch` / `git reset` 的输出对用户是有价值的进度信息，但程序不需要读取它，所以用 `"inherit"` 让它实时流向终端。对比 `status()` 里的调用：

> [bin/cm.js:L112-L113](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L112-L113)
>
> ```js
>     let output = run("git", ["status", "-sb"], pkg.dir)
>     if (output != "## main...origin/main\n")
> ```

这里**程序要消费输出**（拿字符串和干净状态做全等比较），所以用默认的 `"pipe"`，由返回值拿回 stdout。（`status` 的完整逻辑属于 u2-l5，这里只看 `run` 的用法。）

Windows 分支出现在 npm 调用上：

> [bin/cm.js:L99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L99)
>
> ```js
>   run("npm", ["install", "--ignore-scripts"], root, {shell: process.platform == "win32", stdout: "inherit"})
> ```

在 Windows 上 `npm` 实际是 `npm.cmd` 批处理脚本，`execFileSync` 无法直接执行，必须借 `shell: true` 让 cmd.exe 代为解析。顺带注意：一旦启用 shell，`args` 会被拼接进 shell 命令行——参数内容就不再「按字面量」传递了，这也是使用 `run()` 时要留心的边界。

还有一点值得现在就知道：`shell` 默认为 `false` 意味着**通配符不会被展开**。glob 展开（把 `dist/*` 变成 `dist/a.js dist/b.js`）是 shell 的职责；不经 shell 时，`rm` 收到的就是字面量字符串 `dist/*`。这个事实会在 4.3 节的清理分支里变得很关键。

#### 4.2.4 代码实践

1. **实践目标**：亲手体会 `stdout: "pipe"` 与 `"inherit"` 的差别。
2. **操作步骤**：在任意目录执行下面两段**示例代码**（非项目代码）：

   ```bash
   # 第一段：默认 pipe，输出被收集成返回值，终端看不到 git 输出，只看到 === 包住的行
   node -e 'const cp = require("child_process")
   let out = cp.execFileSync("git", ["--version"], {encoding: "utf8", stdio: ["ignore", "pipe", process.stderr]})
   console.log("===", out.trim(), "===")'

   # 第二段：inherit，git 的输出直接打到终端，返回值是空字符串
   node -e 'const cp = require("child_process")
   let out = cp.execFileSync("git", ["--version"], {encoding: "utf8", stdio: ["ignore", "inherit", process.stderr]})
   console.log("=== 返回值长度:", out.length, "===")'
   ```

3. **需要观察的现象**：第一段终端只出现一行被 `===` 包住的版本号；第二段版本号出现在 `===` 之外（由 git 自己打印），且返回值长度为 0。
4. **预期结果**：如上。此实验不依赖本仓库，任何装了 node 和 git 的机器都可做（待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：`stdio` 三元组 `["ignore", stdout, process.stderr]` 各管什么？为什么 stderr 要直通终端？

**答案**：分别对应 stdin / stdout / stderr。stdin 被 ignore，因为这些工具都不需要交互输入；stderr 直通终端，让警告和错误（比如 `git reset --hard` 的提示）实时可见、不被程序吞掉；stdout 是唯一需要「按需选择」的通道——程序要读就用 `pipe`，纯展示就用 `inherit`。

**练习 2**：如果想让 `run()` 在子进程失败时不抛错而是返回 `null`，最小改动是什么？这样做的代价是什么？

**答案**：用 `try { ... } catch { return null }` 包住 `execFileSync` 即可。代价是丢失了错误信息与失败信号——事实上 `cm.js` 里的 `grep()` 就用 try/catch 吃掉非零退出码后主动 `process.exit(1)`（[bin/cm.js:L327-L331](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L327-L331)）。`execFileSync` 在子进程非零退出时会抛异常，这是同步 API 的默认行为，多数调用点恰恰依赖它「失败即中断」。

**练习 3**：为什么 `run("npm", ...)` 要判断 `process.platform == "win32"`，而 `run("git", ...)` 不用？

**答案**：Windows 上 git 通常安装为真正的可执行文件 `git.exe`，`execFileSync` 可以直接启动；而 npm 在 Windows 上是 `npm.cmd` 脚本，只能由 shell 解释执行，所以需要 `shell: true`。

### 4.3 install()：克隆、更新、清理、装配、构建

#### 4.3.1 概念说明

`install()` 要解决的问题：把「36 个彼此独立发版的 git 仓库」变成「一个可以统一构建、统一测试的开发环境」。它分三个阶段：

1. **逐包对齐**：遍历注册表里的每个包，目录已存在就同步到远端 main 并清理构建残留；不存在就从 code.haverbeke.berlin 克隆。
2. **依赖装配**：在仓库根目录跑一次 `npm install`，借助 workspaces 把 36 个包目录链接成逻辑 monorepo，依赖统一提升到根 `node_modules`。
3. **重新加载注册表并首次构建**：克隆完成后 `Pkg` 才能探测到各包的入口文件，所以要重新 `loadPackages()`，再调用 `build()` 打出各包的 `dist` 产物。

值得注意的是，这套「更新已有仓库」的逻辑相当新——2026 年 4 月的三个提交才把它塑造成现在的样子（见 4.3.3 末尾的表格）。

#### 4.3.2 核心流程

```text
install(arg = null)
  ├─ 解析 base：arg == "--ssh" → git@… SSH 地址；默认 → https 地址；其他参数 → help(1)
  ├─ 阶段一：for (pkg of packages)        # 36 次
  │    ├─ pkg.dir 已存在（重复安装）
  │    │    ├─ git fetch origin main        # 拉取远端 main
  │    │    ├─ git reset --hard FETCH_HEAD  # 强制对齐（丢弃本地修改！）
  │    │    └─ rm -f dist/* test/*.js       # 清理构建残留（见 4.3.5 练习 3）
  │    └─ pkg.dir 不存在（首次安装）
  │         └─ git clone base/<repo名>.git pkg.dir   # codemirror 包特例：仓库名是 basic-setup
  ├─ 阶段二：npm install --ignore-scripts   # 在仓库根目录，workspaces 生效
  └─ 阶段三：重新 loadPackages() 赋值回模块级变量 → build()
```

#### 4.3.3 源码精读

> [bin/cm.js:L81-L83](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L81-L83)
>
> ```js
> function install(arg = null) {
>   let base = arg == "--ssh" ? "git@code.haverbeke.berlin:codemirror/" : "https://code.haverbeke.berlin/codemirror/"
>   if (arg && arg != "--ssh") help(1)
> ```

函数只接受一个可选参数 `--ssh`，用来在 HTTPS 与 SSH 克隆地址之间切换（有 SSH key 的贡献者用后者更方便）；除此之外的任何参数都直接打印用法退出。所有包仓库都托管在 `code.haverbeke.berlin/codemirror/` 命名空间下。

**阶段一**的循环体是一个标准的 if/else 两分支：

> [bin/cm.js:L85-L96](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L85-L96)
>
> ```js
>   for (let pkg of packages) {
>     if (fs.existsSync(pkg.dir)) {
>       console.log(`${pkg.name} exists, updating to origin`)
>       run("git", ["fetch", "origin", "main"], pkg.dir, {stdout: "inherit"})
>       run("git", ["reset", "--hard", "FETCH_HEAD"], pkg.dir, {stdout: "inherit"})
>       run("rm", ["-f", "dist/*", "test/*.js"], pkg.dir)
>     } else {
>       console.log(`Checking out ${pkg.name}`)
>       let origin = base + (pkg.name == "codemirror" ? "basic-setup" : pkg.name) + ".git"
>       run("git", ["clone", origin, pkg.dir], root, {stdout: "inherit"})
>     }
>   }
> ```

- **更新分支**（目录已存在）：`fetch` + `reset --hard` 的组合把每个包仓库**强制同步**到远端 main。⚠️ 这是本命令最危险的一步：如果你在某个包里留有未提交的实验性修改，二次运行 `cm install` 会把它们无声抹掉。清理那行 `rm` 想删掉的是 `git reset` 碰不到的构建残留（`dist/` 产物与编译出的测试文件通常被各包 gitignore，属于 untracked 文件）。
- **克隆分支**（目录不存在）：注意第 93 行的特判——npm 包 `@codemirror/codemirror` 对应的 git 仓库叫 `basic-setup`，名字对不上，需要硬编码映射。
- 两个分支的 `git` 调用都带 `stdout: "inherit"`，所以克隆/更新的进度会实时滚动在终端上；而三行 `console.log`（`exists, updating to origin`、`Checking out …`）是 `cm install` 自己打的节拍器。

**阶段二**的装配：

> [bin/cm.js:L98-L99](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L98-L99)
>
> ```js
>   console.log("Running npm install")
>   run("npm", ["install", "--ignore-scripts"], root, {shell: process.platform == "win32", stdout: "inherit"})
> ```

这一次 `npm install` 之所以能同时装好 36 个包的依赖，靠的是根 [package.json:L22-L24](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L22-L24) 里的 `"workspaces": ["*"]`：克隆完成后，根目录下的每个包目录都含自己的 `package.json`，npm 会把它们全部纳为工作区，包与包之间的 `@codemirror/*` 依赖直接以符号链接形式指向兄弟目录，第三方依赖统一提升到根 `node_modules`。`--ignore-scripts` 则跳过各包自己的 `prepare` 等生命周期脚本——因为构建由下一步的 `cm` 自己统一驱动，避免递归/重复构建。

**阶段三**的重新加载与构建：

> [bin/cm.js:L100-L102](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L100-L102)
>
> ```js
>   console.log("Building modules")
>   ;({packages, packageNames, buildPackages} = loadPackages())
>   build()
> ```

为什么必须重新 `loadPackages()`？看 `Pkg` 的构造函数就明白了：

> [bin/packages.js:L46-L58](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L46-L58)
>
> ```js
> class Pkg {
>   constructor(name) {
>     this.name = name
>     this.dir = join(__dirname, "..", name)
>     this.main = null
>     if (name != "legacy-modes" && fs.existsSync(this.dir)) {
>       let files = fs.readdirSync(join(this.dir, "src")).filter(f => /^[^.]+\.ts$/.test(f))
>       let main = files.length == 1 ? files[0] : files.includes("index.ts") ? "index.ts"
>           : files.includes(name.replace(/^(theme-|lang-)/, "") + ".ts") ? name.replace(/^(theme-|lang-)/, "") + ".ts" : null
>       if (!main) throw new Error("Couldn't find a main script for " + name)
>       this.main = join(this.dir, "src", main)
>     }
>   }
> }
> ```

（入口文件的三条探测规则将在 u2-l1 详讲，本讲只需要注意那个 `fs.existsSync(this.dir)` 前置条件。）模块加载时（[bin/cm.js:L11](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L11)）包目录还不存在，`fs.existsSync(this.dir)` 全部为假，于是**所有 `Pkg.main` 都是 `null`**，`buildPackages`（[bin/packages.js:L62-L67](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L62-L67) 中 `packages.filter(p => p.main)`）是空数组。克隆完成后重新加载，`main` 才被逐个探测出来，`build()` 才有东西可建：

> [bin/cm.js:L118-L123](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L118-L123)
>
> ```js
> async function build() {
>   console.info("Building...")
>   let t0 = Date.now()
>   await require("@marijn/buildtool").build(buildPackages.map(p => p.main), require("@codemirror/buildhelper/src/options").options)
>   console.info(`Done in ${((Date.now() - t0) / 1000).toFixed(2)}s`)
> }
> ```

这里的 `require("@marijn/buildtool")` 之所以敢写，正是因为它运行在阶段二的 `npm install` 之后——惰性 require 与顶部注释（4.1.3）首尾呼应。行首那个孤立的分号则是为了防止上一行的 `console.log(...)` 与以 `(` 开头的本行被解析成函数调用。

最后，`install()` 的现状是三个近期提交叠加的结果，读者可以自己用只读 git 命令考古（以下命令均可直接运行）：

| 提交 | 日期 | 变化 |
| --- | --- | --- |
| `git show 7d87d755` | 2026-04-16 | 「已存在的目录」从**跳过并警告**（`Skipping cloning of …`）改为 fetch + `reset --hard` 主动同步；`run()` 为此新增 `stdout` 选项；npm install 加 `--ignore-scripts` |
| `git show 641c7fdf` | 2026-04-16 | 更新分支新增 `run("rm", ["dist/*", "test/*.js"], pkg.dir)` 清理构建残留 |
| `git show c93d50b6` | 2026-04-16 | 给 rm 加 `-f`，目录里没有 dist/产物时不报错 |

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：完整走一遍环境装配，把 `install()` 源码里的每个 `console.log` 与终端输出一一对应。
2. **操作步骤**：
   1. 确认环境：node 16+（[README.md:L9](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/README.md#L9) 的要求）、git、npm 可用，且能访问 `code.haverbeke.berlin`。
   2. 在全新克隆的仓库根目录执行 `node bin/cm.js install`。需要较长的网络时间与磁盘空间（36 个仓库），建议把输出重定向留存：`node bin/cm.js install 2>&1 | tee install-log.txt`（`tee` 为示例命令）。
   3. 安装完成后执行 `node bin/cm.js packages`，把输出的 36 行与 [bin/packages.js:L3-L42](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L3-L42) 的 `core`（12 项）与 `nonCore`（24 项）清单逐行对照，确认顺序一致（`listPackages` 按 `all = core.concat(nonCore)` 的顺序输出，见 [bin/cm.js:L106-L108](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L106-L108)）。
   4. （可选，进阶）**确认无未提交修改后**再运行一次 `node bin/cm.js install`，观察「更新分支」的输出。
3. **需要观察的现象**：首次安装时输出按三类动作推进——
   - `Checking out state`、`Checking out view`……每个包一行（对应 [L92](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L92)），夹杂 git clone 自身的进度输出（`stdout: "inherit"` 的效果）；
   - `Running npm install` 一行（对应 [L98](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L98)）后是 npm 的安装日志；
   - `Building modules` 一行（对应 [L100](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L100)），随后是 `Building...` 与 `Done in …s`（来自 `build()`）。
   
   第二次运行时，三类动作中的第一类会变成 `xxx exists, updating to origin`（对应 [L87](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L87)）。
4. **预期结果**：`cm packages` 输出与 `packages.js` 清单完全一致且顺序相同；根目录多出 36 个包目录与 `node_modules`；各包目录下出现 `dist/` 构建产物。若你此刻没有网络或不想克隆，可改为**源码阅读型实践**：执行上面表格里的三条 `git show` 命令，画出 install 更新分支改造前后的两张流程图。本讲未替读者执行过 install，以上输出形态均由源码推导，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `@codemirror/codemirror` 包要从 `basic-setup.git` 克隆？这个信息写在源码的哪里？

**答案**：npm 包名与 git 仓名不一致，第 93 行用三元表达式做了硬编码映射：`base + (pkg.name == "codemirror" ? "basic-setup" : pkg.name) + ".git"`。`codemirror` 包是预置扩展组合（basic setup）的聚合入口，仓库沿用了历史名称 `basic-setup`。

**练习 2**：同事说他改了 `state` 包里的源码但还没 commit，然后运行了 `cm install`，他的修改还在吗？

**答案**：不在了。他的 `state` 目录已存在，走更新分支，`git reset --hard FETCH_HEAD` 会把工作区强制对齐远端 main，未提交的修改被丢弃。这也是为什么更新分支之后还要 `rm` 清理——`reset --hard` 连 untracked 的构建残留都不会碰。

**练习 3**（辨析题）：第 90 行 `run("rm", ["-f", "dist/*", "test/*.js"], pkg.dir)` 没有传任何选项，`run()` 默认 `shell: false`。那么 `dist/*` 里的 `*` 会被展开成 dist 目录下的所有文件吗？

**答案**：不会。glob 展开是 shell 的职责；`execFileSync` 在 `shell: false` 时把 `"dist/*"` 作为**字面量**参数传给 `rm`，rm 自己不做通配。因此这一行实际尝试删除的是「名为 `*` 的文件」，通常不存在，而 `-f`（c93d50b6 加入）恰好把「不存在」的报错也静默了。换句话说，这条清理命令的**意图**（清掉构建残留）与**实际效果**（几乎恒为无操作）之间存在偏差——提交信息 "Clean installed packages in cm install" 表达的是意图。若要真正生效，需要 `shell: true` 或改用 `fs.rmSync(dir, {recursive: true})` 之类的 Node API。这道题的训练点在于：读子进程代码时必须时刻意识到「参数经过了谁的手」。

**练习 4**：删掉第 101 行的 `;({packages, packageNames, buildPackages} = loadPackages())` 会怎样？

**答案**：模块加载时（第 11 行）计算出的 `buildPackages` 是空数组（所有 `Pkg.main` 为 `null`），`build()` 会以空列表调用 `@marijn/buildtool`——什么都不构建，且因为 `build()` 不报错，问题会以「看似成功的静默失败」呈现。重新加载正是为了让 `Pkg` 在目录存在的前提下重新探测入口文件。

## 5. 综合实践

**任务：给安装流程写一份「日志—源码对照手册」。**

1. **准备**：按 4.3.4 完成一次全新安装并用 `tee` 留存完整日志。
2. **标注**：在日志里用三种颜色/标记分别标出 `cm install` 自己打印的行（`Checking out …` / `exists, updating to origin` / `Running npm install` / `Building modules`）、子进程经 `stdout: "inherit"` 直通的行（git、npm 的输出）和 `build()` 打印的行（`Building...` / `Done in …s`）。每一类旁边注明它对应 [bin/cm.js:L81-L103](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/cm.js#L81-L103) 的哪一行。
3. **对照注册表**：把 `cm packages` 的 36 行输出贴在 `packages.js` 的 `core`/`nonCore` 两个数组旁边，验证 `all = core.concat(nonCore)` 的拼接顺序；再用 `ls` 数一下根目录实际的包目录数量是否恰为 36（提示：`legacy-modes` 也在其中，但它的 `Pkg.main` 恒为 `null`，想一想它为什么被排除在 `buildPackages` 之外——答案在 [bin/packages.js:L51](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/bin/packages.js#L51) 的条件里）。
4. **验证装配**：`ls node_modules/@codemirror` 观察哪些包被 workspaces 符号链接到了兄弟目录、哪些是被真正安装的第三方依赖；对照根 [package.json:L11-L17](https://github.com/code.haverbeke.berlin/codemirror/dev.git/blob/912443365be503d6898c3543955882e7516e4294/package.json#L11-L17) 的 `devDependencies`，解释两者的差集从何而来。
5. **收尾**：从日志中摘出三条你认为「如果不读源码就完全无法理解」的输出行，写下它们各自的出处行号。这份手册将是你后续学习 u2-l2 构建流水线时的第一手材料。

若无法完成真实安装，替代方案：用 `git show 7d87d755`、`git show 641c7fdf`、`git show c93d50b6` 三个只读命令重走本讲的演变史，输出三张 install 流程图（改造前 / 中间态 / 现状）并配文字说明每一步动机。

## 6. 本讲小结

- `bin/cm.js` 顶层只 require Node 内置模块与本地 `packages.js`，一切 `node_modules` 依赖都藏在函数体内惰性加载——这是 `install` 能在 `node_modules` 存在前运行的基石。
- `start()` 用「命令→函数」映射表分发子命令，除 `install` 与 `--help` 外都要先过 `assertInstalled()` 的包目录存在性检查；错误统一汇入 `error()`。
- `run()` 是三行的 `execFileSync` 封装：`cwd` 定工作目录，`stdout: "pipe"` 收集返回值、`"inherit"` 直通终端，`shell` 仅在 Windows 跑 npm 时打开——而不开 shell 时通配符不会被展开。
- `install()` 三阶段：逐包「已存在则 fetch + `reset --hard` + 清理 / 不存在则 clone（`codemirror`→`basic-setup` 特例）」→ `npm install --ignore-scripts` 借 workspaces 完成 monorepo 装配 → 重新 `loadPackages()` 探测入口后 `build()`。
- 重新加载注册表是必须的：首次加载时包目录尚不存在，所有 `Pkg.main` 为 `null`。
- 二次运行 `cm install` 会用 `reset --hard` 抹掉包内未提交修改，动手前务必确认工作区干净。

## 7. 下一步学习建议

- **下一讲 u1-l3《命令行骨架》**：本讲只顺带提了 `cmdFn.length` 参数个数校验和 `help()` 的用法文本，下一讲会把 `start()` 的分发机制、`Function#length` 技巧和错误出口完整拆开——你将在那里练习给 cm 新增一个子命令。
- **u1-l4《demo 与 dev 服务器》**：环境装好后，跟着 `npm run dev` 把 8090 端口的开发服务器跑起来，看看你刚构建出的包在浏览器里长什么样。
- **延伸阅读源码**：[bin/packages.js](../../bin/packages.js) 全文只有 67 行，值得现在通读；`Pkg` 的入口文件探测规则（唯一 ts 文件 > `index.ts` > 与包同名的文件）将在 u2-l1 详细展开。
- 如果你完成了综合实践，建议把日志对照手册保留好——u2-l2 讲构建流水线时，`install()` 末尾那个 `build()` 调用就是那边的起点。
